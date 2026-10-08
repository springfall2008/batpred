"""Connect live rate calculation to observed price history without changing planning."""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ha import run_async
from rate_periods import period_time


def observation_time(base):
    """Use the unrounded configured clock, with an explicit UTC result."""
    instant = getattr(base, "now_utc_real", None) or base.now_utc
    return (instant + timedelta(minutes=base.args.get("clock_skew", 0))).astimezone(timezone.utc)


def current_periods(base, supplied, now, direction):
    """Keep supplied intervals, using configured slots for long price validity."""
    periods = []
    for item in supplied:
        start, end = period_time(item.get("start")), period_time(item.get("end"))
        origin = item.get("bounds", "provider")
        if start is not None and origin == "cadence":
            # A gap between source starts is not evidence of a longer billing slot.
            end = start + timedelta(minutes=max(1, int(base.plan_interval_minutes)))
            origin = "configured"
        if start is not None and end is not None and start <= now < end and item.get("kind") == "interval":
            periods.append({"start": start, "end": end, "source_id": item.get("source_id", direction), "boundary_origin": origin})
    if periods:
        # Duplicate metadata rows must not create several copies of one period.
        return list({(item["start"], item["end"]): item for item in periods}.values())
    width = max(1, int(base.plan_interval_minutes))
    local = now.astimezone(ZoneInfo(base.args.get("timezone", "Europe/London")))
    midnight = datetime.combine(local.date(), datetime.min.time(), tzinfo=local.tzinfo).astimezone(timezone.utc)
    elapsed = int((now - midnight).total_seconds() // 60)
    start = midnight + timedelta(minutes=(elapsed // width) * width)
    validity = next((item for item in supplied if period_time(item.get("start")) is not None and period_time(item["start"]) <= now and (item.get("end") is None or now < period_time(item["end"]))), {})
    return [{"start": start, "end": start + timedelta(minutes=width), "source_id": validity.get("source_id", direction), "boundary_origin": "configured"}]


def prepare_history_inputs(base, supplied_import, supplied_export, now):
    """Prepare current parents and collectors for actually applicable overrides."""
    origin = base.midnight_utc.astimezone(timezone.utc)
    result = {}
    for direction, supplied in (("import", supplied_import), ("export", supplied_export)):
        periods = current_periods(base, supplied, now, direction)
        minutes = {int((now - origin).total_seconds() // 60)}
        result[direction] = {"periods": periods, "trace": {"minutes": minutes, "operations": []}}
    return result


def price_segments(rates, origin, start, end, source):
    """Expand existing minute prices into bounded UTC runs, leaving gaps unknown."""
    origin = origin.astimezone(timezone.utc)
    first = math.floor((start - origin).total_seconds() / 60)
    last = math.ceil((end - origin).total_seconds() / 60)
    segments = []
    for minute in range(first, last):
        value = rates.get(minute)
        if value is None or not math.isfinite(float(value)):
            continue
        left, right = max(start, origin + timedelta(minutes=minute)), min(end, origin + timedelta(minutes=minute + 1))
        if left >= right:
            continue
        if segments and segments[-1]["end"] == left and segments[-1]["rate"] == float(value):
            segments[-1]["end"] = right
        else:
            segments.append({"start": left, "end": right, "rate": float(value), "sources": [source]})
    return segments


def manual_operations(inputs, origin):
    """Keep actual bounds and ordering of the parser's observed operations."""
    origin = origin.astimezone(timezone.utc)
    return [{"start": origin + timedelta(minutes=item["start"]), "end": origin + timedelta(minutes=item["end"]), "rate": item["rate"], "kind": item["kind"], "source": item["source"]} for item in inputs["trace"]["operations"]]


def observe_source_offers(base, now, export_saving_enabled=False):
    """Retain actually active source operations once, with their full bounds."""
    history = base.rate_history
    zone = base.args.get("timezone", "Europe/London")
    component = base.components.get_component("axle") if base.components else None
    entity = base.get_arg("axle_session", None, indirect=False)
    managed = bool(getattr(component, "managed_mode", False))
    metadata = getattr(component, "managed_price_periods", None)
    if entity and component is None:
        managed = base.get_state_wrapper(entity_id=entity, attribute="managed_mode") is True
        metadata = base.get_state_wrapper(entity_id=entity, attribute="managed_price_periods")
    axle_rows = getattr(base, "axle_sessions", [])
    if managed:
        current_metadata = [row for row in metadata or [] if period_time(row.get("start_time"), zone) is not None and period_time(row.get("end_time"), zone) is not None and period_time(row["start_time"], zone) <= now < period_time(row["end_time"], zone)]
        axle_rows = current_metadata or axle_rows
    grouped = {}
    groups = [(getattr(base, "octopus_saving_slots", []), "saving", 1), (getattr(base, "octopus_free_slots", []), "free", 2), (axle_rows, "axle_managed" if managed else "axle", 3)]
    for rows, source, stage in groups:
        for row in rows:
            start = period_time(row.get("start_time", row.get("start")), zone)
            end = period_time(row.get("end_time", row.get("end")), zone)
            if source == "saving" and row.get("state") and (start is None or end is None):
                start = base.midnight_utc.astimezone(timezone.utc) + timedelta(minutes=(base.minutes_now // 30) * 30)
                end = start + timedelta(minutes=30)
            if start is None or end is None or not start <= now < end:
                continue
            value = row.get("pence_per_kwh") if stage == 3 else row.get("rate")
            directions = ("import", "export") if source != "free" else ("import",)
            if source == "saving" and not export_saving_enabled:
                directions = ("import",)
            if source == "axle" and row.get("import_export") not in ("import", "export"):
                continue
            if source == "axle" and row.get("import_export") == "import":
                directions = ("import",)
            kind = "minimum" if source == "free" else "increment"
            for direction in directions:
                identity = "{}:{}:{}:{}:{}".format(source, direction, start.isoformat(), end.isoformat(), kind)
                if value is None:
                    if managed and stage == 3:
                        history.withdraw_offer(identity, now)
                    continue
                try:
                    rate = float(value)
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(rate):
                    continue
                if source == "axle" and row.get("import_export") == "import":
                    rate = -rate
                offer = {"direction": direction, "start": start, "end": end, "kind": kind, "rate": rate, "source": source, "stage": stage, "identity": identity, "sticky": source != "axle_managed"}
                if identity in grouped:
                    previous = grouped[identity]["rate"]
                    offer["rate"] = previous + rate if kind == "increment" else min(previous, rate)
                grouped[identity] = offer
    for offer in grouped.values():
        history.retain_offer(offer, now)


def history_reservations(base, car_n):
    """Reserve accepted price-only cap allocations without restoring dispatches."""
    history = getattr(base, "rate_history", None)
    if history is None:
        return []
    origin = base.midnight_utc.astimezone(timezone.utc)
    result = {}
    for record in history.records.values():
        for evidence in record.get("allocations", record.get("confirmations", [])):
            if evidence.get("car_n") != car_n or not evidence.get("cap_bucket"):
                continue
            start, bucket = period_time(evidence["start"]), period_time(evidence["cap_bucket"])
            if start is None or bucket is None:
                continue
            slot_start = int((start - origin).total_seconds() // 60)
            zone = ZoneInfo(base.args.get("timezone", "Europe/London"))
            day_offset = (bucket.astimezone(zone).date() - base.midnight_utc.date()).days
            result[slot_start] = {"slot_start": slot_start, "day_offset": day_offset, "rate": evidence["rate"]}
    return list(result.values())


def record_iog_confirmation(base, car_n, period_end, observed_at):
    """Keep fresh car telemetry only when the price path allocated its cheap block."""
    history = getattr(base, "rate_history", None)
    if history is None or getattr(base, "rate_history_replay", False):
        return False
    sensor = getattr(base, "dynamic_load_car_sensors", {}).get(car_n)
    if not sensor:
        return False
    entities = base.get_arg("octopus_intelligent_slot", [], indirect=False)
    entities = entities if isinstance(entities, list) else [entities]
    if car_n < len(entities) and "kraken" in str(entities[car_n]).lower():
        return False
    end = period_end.astimezone(timezone.utc)
    start = end - timedelta(minutes=30)
    now = observed_at.astimezone(timezone.utc)
    if base.car_charging_now_value(base.get_state_wrapper(sensor, required_unit="W")) is not True:
        return False
    raw = base.get_state_wrapper(sensor, raw=True)
    raw = raw if isinstance(raw, dict) else {}
    measured_at = period_time(raw.get("last_reported") or raw.get("last_updated") or raw.get("last_changed"))
    if getattr(base, "car_slot_owner", None) == "ohme" and base.components:
        ohme = base.components.get_component("ohme")
        if ohme:
            measured_at = period_time(ohme.last_updated_time())
    if measured_at is None or not start <= measured_at < end or measured_at > now or now - measured_at > timedelta(minutes=5):
        return False
    scopes = getattr(base, "rate_history_iog_eligibility", {}).get(car_n, [])
    if not any(period_time(scope["start"]) <= measured_at < period_time(scope["end"]) for scope in scopes):
        return False
    origin = base.midnight_utc.astimezone(timezone.utc)
    allocation_origin = getattr(base, "rate_history_iog_allocation_origin", None)
    if allocation_origin is None or allocation_origin.astimezone(timezone.utc) != origin:
        return False
    slot_start = int((start - origin).total_seconds() // 60)
    allocation = getattr(base, "rate_history_iog_allocations", {}).get(car_n, {}).get(slot_start)
    if allocation is None:
        return False
    bucket = origin + timedelta(minutes=allocation["day_offset"] * 1440 + 720)
    accepted = history.confirm_iog(start, end, allocation["rate"], now, source_id=str(entities[car_n]) if car_n < len(entities) else "iog", car_n=car_n, cap_bucket=bucket.isoformat())
    if accepted:
        base.rate_history_snapshot = history.snapshot()
        storage = base.components.get_component("storage") if base.components else None
        if storage:
            run_async(history.save_dirty(storage))
    return accepted


def record_metered_dispatches(base, now):
    """Accept direct Octopus delivery rows confined to one billing half-hour.

    Ohme's elapsed schedules and generic compatible entity attributes are not
    metered evidence. A multi-half-hour energy total does not prove delivery in
    each intersected block, so only a single-block completed row qualifies here.
    """
    component = base.components.get_component("octopus") if base.components else None
    if component is None or not hasattr(component, "get_intelligent_devices"):
        return
    entities = base.get_arg("octopus_intelligent_slot", [], indirect=False)
    entities = entities if isinstance(entities, list) else [entities]
    origin = base.midnight_utc.astimezone(timezone.utc)
    for device_id, device in component.get_intelligent_devices().items():
        entity = component.get_entity_name("binary_sensor", "intelligent_dispatch", index=component.device_id_to_index_suffix(device_id))
        for car_n, configured in enumerate(entities):
            if configured != entity:
                continue
            for dispatch in device.get("completed_dispatches", []):
                if dispatch.get("source") in ("BOOST", "bump-charge"):
                    continue
                start, end = period_time(dispatch.get("start")), period_time(dispatch.get("end"))
                try:
                    delivered = float(dispatch.get("charge_in_kwh"))
                except (TypeError, ValueError):
                    continue
                if start is None or end is None or not math.isfinite(delivered) or delivered <= 0 or not start < end <= now:
                    continue
                slot_start = math.floor((start - origin).total_seconds() / 1800) * 30
                block_start = origin + timedelta(minutes=slot_start)
                block_end = block_start + timedelta(minutes=30)
                if end > block_end:
                    continue
                allocation = getattr(base, "rate_history_iog_allocations", {}).get(car_n, {}).get(slot_start)
                if allocation is None:
                    continue
                bucket = origin + timedelta(minutes=allocation["day_offset"] * 1440 + 720)
                base.rate_history.confirm_iog(block_start, block_end, allocation["rate"], now, source_id=entity, car_n=car_n, cap_bucket=bucket.isoformat())


def update_rate_history(base, inputs, io_rates, automatic_import, automatic_export, now):
    """Observe a live calculation and materialise accounting views separately."""
    history = base.rate_history
    history.retention_days = max(2, int(base.get_arg("rate_retention_days", 2)))
    # Evidence can refer to a just-finished half-hour. Admit it before closure.
    record_metered_dispatches(base, now)
    for car_n, end in getattr(base, "dynamic_load_car_confirmed", {}).items():
        record_iog_confirmation(base, car_n, end, now)
    history.close_elapsed(now)
    observe_source_offers(base, now, export_saving_enabled=inputs["export"].get("saving_enabled", False))
    for direction, automatic, plain in (("import", automatic_import, io_rates), ("export", automatic_export, base.rate_export_base)):
        item = inputs[direction]
        if not automatic:
            continue
        for parent in item["periods"]:
            start, end = parent["start"], parent["end"]
            profile = price_segments(automatic, base.midnight_utc, start, end, "effective")
            plain_profile = price_segments(plain, base.midnight_utc, start, end, "tariff")
            baseline = price_segments(base.rate_import_no_io, base.midnight_utc, start, end, "no_io") if direction == "import" else None
            offers = history.offers_for(direction, start, end)
            history.observe(direction, [parent], profile, manual_operations(item, base.midnight_utc), now, baseline_segments=baseline, base_segments=plain_profile, offer_ids=[offer["identity"] for offer in offers])
    premium = any(slot.get("average", 0) > base.rate_import.get(slot.get("start"), 0) and slot.get("kwh", 0) > 0 for slots in getattr(base, "car_charging_slots", []) for slot in slots if 0 <= slot.get("start", -1) < base.minutes_now)
    local_date = now.astimezone(ZoneInfo(base.args.get("timezone", "Europe/London"))).date()
    history.set_context(local_date, base.metric_standing_charge, premium=premium)
    origin = base.midnight_utc.astimezone(timezone.utc)
    zone = ZoneInfo(base.args.get("timezone", "Europe/London"))
    today_start = datetime.combine(local_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    yesterday_start = datetime.combine(local_date - timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    base.rate_history_calendar_day_minutes = int((today_start - yesterday_start).total_seconds() // 60)
    base.rate_history_calendar_origin_valid = origin == today_start
    lower, upper = origin - timedelta(days=2), max(now, origin + timedelta(minutes=base.minutes_now + base.plan_interval_minutes))
    for direction in ("import", "export"):
        view = getattr(base, "rate_" + direction).copy()
        coverage = set()
        for segment in history.lookup(direction, lower, upper):
            left, right = period_time(segment["start"]), period_time(segment["end"])
            for minute in range(math.ceil((left - origin).total_seconds() / 60), math.ceil((right - origin).total_seconds() / 60)):
                view[minute] = segment["rate"]
                coverage.add(minute)
        setattr(base, "rate_history_" + direction, view)
        setattr(base, "rate_history_coverage_" + direction, coverage)
    base.rate_history_no_io = base.rate_import_no_io.copy()
    for segment in history.lookup("import", lower, upper, baseline=True):
        left, right = period_time(segment["start"]), period_time(segment["end"])
        for minute in range(math.ceil((left - origin).total_seconds() / 60), math.ceil((right - origin).total_seconds() / 60)):
            base.rate_history_no_io[minute] = segment["rate"]
    base.rate_history_standing_charge = {}
    base.rate_history_car_premium_present = {}
    for day_offset in (-1, 0):
        date_key = (local_date + timedelta(days=day_offset)).isoformat()
        context = history.accounting_context.get(date_key)
        if context:
            base.rate_history_standing_charge[day_offset * 1440] = context["standing_charge"]
            base.rate_history_car_premium_present[day_offset * 1440] = context["car_premium_present"]
    base.rate_history_accounting_enabled = True
    base.rate_history_snapshot = history.snapshot()
    storage = base.components.get_component("storage") if base.components else None
    if storage:
        run_async(history.save_dirty(storage))

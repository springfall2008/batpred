# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Half-hourly billing from the Kraken measurements API (Octopus, EDF, E.ON)
# -----------------------------------------------------------------------------

"""Read what Octopus/Kraken actually billed, half-hour by half-hour.

The Kraken GraphQL ``measurements`` query returns every half-hour's metered kWh with a breakdown
into the supplier's rate buckets (on Intelligent Octopus Go: Home Standard/Low Rate and EV
Standard/Low Rate) and the standing charge apportioned across the day. This module turns that into
a 7-day cache of slots, published as ``sensor.predbat_billing`` so the Plan page's History view can show
the real bill next to Predbat's own cost.

Bucket labels have changed on live accounts before, so they are never matched by name: whatever
labels arrive are stored and summed. Statistics are classified by their ``type`` enum only, and a
half-hour whose buckets do not add up to its metered kWh is dropped rather than shown - a wrong
figure is worse than none.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BILLING_DAYS = 7
BILLING_REFRESH_MINUTES = 180
BILLING_ENTITY_SUFFIX = "billing"
BILLING_SLOT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BILLING_LEGS = ("import", "export")
# A past day is read again each refresh until it is complete, but once it is this many days old one more read settles it:
# suppliers publish within a day or two, so a half-hour still missing by then (or a meter that publishes nothing) is not coming
BILLING_SETTLE_DAYS = 3
# The measurements query asks for Europe/London days, and the supplier spreads each London day's standing charge over its half-hours
BILLING_TZ = ZoneInfo("Europe/London")

# The statistic type that carries the standing charge; every other type is a rate bucket.
STANDING_CHARGE_TYPE = "STANDING_CHARGE_COST"

# The Kraken measurements query, one local day at a time. Same shape as octopus.MEASUREMENTS_QUERY
# plus a reading direction: an export MPAN returns no half-hours at all unless GENERATION is asked for.
BILLING_MEASUREMENTS_QUERY = """query {{
  account(accountNumber: "{account_id}") {{
    properties {{
      measurements(
        first: 50
        utilityFilters: [{{electricityFilters: {{readingFrequencyType: THIRTY_MIN_INTERVAL, marketSupplyPointId: "{mpan}", readingDirection: {direction}}}}}]
        startAt: "{start_at}"
        endAt: "{end_at}"
        timezone: "Europe/London"
      ) {{
        edges {{
          node {{
            value
            unit
            ... on IntervalMeasurementType {{
              startAt
              endAt
            }}
            metaData {{
              statistics {{
                type
                label
                value
                costInclTax {{ estimatedAmount costCurrency }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}"""
BILLING_DIRECTIONS = {"import": "CONSUMPTION", "export": "GENERATION"}

# Billing whose average rate is outside this multiple of Predbat's own is not shown: a units or VAT mix-up, not a bill
BILLING_PLAUSIBLE_RATE_RATIO = (0.2, 5.0)

# A half-hour where Predbat recorded no cost and no import while the meter shows more than this is a gap in
# Predbat's recording (it was down, or the recorder missed it), not a billing difference
BILLING_UNRECORDED_KWH = 0.05

# How far a half-hour's bucket kWh may stray from its metered kWh before it is not trusted.
BUCKET_KWH_TOLERANCE = 0.01


def billing_entity_id(prefix):
    """The billing sensor's entity id for a Predbat prefix: sensor.predbat_billing by default."""
    return "sensor." + prefix + "_" + BILLING_ENTITY_SUFFIX


def _to_float(value):
    """Return value as a float, or None when it is missing or not a number."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slot_key(stamp):
    """Turn an API timestamp into the cache's UTC slot key, or None when it can't be parsed."""
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return when.astimezone(timezone.utc).strftime(BILLING_SLOT_FORMAT)


def _parse_node(node):
    """Parse one measurements node into a slot dict, or None when it can't be trusted."""
    kwh = _to_float(node.get("value"))
    statistics = (node.get("metaData") or {}).get("statistics") or []
    if kwh is None:
        return None
    standing = 0.0
    rows = []
    for stat in statistics:
        cost = _to_float(((stat.get("costInclTax") or {}).get("estimatedAmount")))
        if stat.get("type") == STANDING_CHARGE_TYPE:
            standing += cost or 0.0
            continue
        stat_kwh = _to_float(stat.get("value"))
        if cost is None and (stat_kwh is None or stat_kwh > 0):
            # Energy with no price yet: leave the half-hour out so the day is read again, rather than bill it at 0p
            return None
        rows.append((stat.get("label") or stat.get("type") or "unknown", stat_kwh, cost or 0.0))
    buckets = {}
    for label, bucket_kwh, cost in rows:
        if bucket_kwh is None:
            # Export's GENERATION_VALUE row, and EDF's TOU_BUCKET_COST import rows (one per half-hour, verified live
            # 6 Nov 2026), carry no kWh of their own; it is the node's, but only when it is the sole bucket - otherwise
            # there is no telling how the kWh split.
            if len(rows) != 1:
                return None
            bucket_kwh = kwh
        if bucket_kwh == 0 and cost == 0:
            continue
        bucket = buckets.setdefault(label, {"kwh": 0.0, "cost": 0.0})
        bucket["kwh"] += bucket_kwh
        bucket["cost"] += cost
    if abs(sum(bucket["kwh"] for bucket in buckets.values()) - kwh) > BUCKET_KWH_TOLERANCE:
        return None
    return {"kwh": kwh, "cost": sum(bucket["cost"] for bucket in buckets.values()), "standing": standing, "buckets": buckets}


def parse_measurements(response_data):
    """Parse a measurements response into {slot_key: slot}, all costs in pence.

    Returns None for a failed read (response_data None) so the caller can tell it apart from a
    read that succeeded with nothing published yet, which is an empty dict. Half-hours that can't
    be trusted are left out.
    """
    if response_data is None:
        return None
    slots = {}
    for prop in ((response_data.get("account") or {}).get("properties")) or []:
        for edge in ((prop.get("measurements") or {}).get("edges")) or []:
            node = (edge or {}).get("node") or {}
            key = _slot_key(node.get("startAt"))
            slot = _parse_node(node) if key else None
            if slot is not None:
                slots[key] = slot
    return slots


def day_slot_starts(day, tz):
    """Return the UTC start of every half-hour in local date day: 48, or 46/50 on a clock change."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    end = datetime(following.year, following.month, following.day, tzinfo=tz).astimezone(timezone.utc)
    starts = []
    while start < end:
        starts.append(start)
        start += timedelta(minutes=30)
    return starts


def _window_days(now_utc, tz):
    """The local dates billing covers: the BILLING_DAYS days before today, then today."""
    today = now_utc.astimezone(tz).date()
    return [today - timedelta(days=offset) for offset in range(BILLING_DAYS, -1, -1)]


def _day_complete(slots, day, tz):
    """True when every half-hour of local date day is in slots."""
    return all(start.strftime(BILLING_SLOT_FORMAT) in slots for start in day_slot_starts(day, tz))


def merge_slots(cache, leg, new_slots, now_utc, tz):
    """Return a copy of cache with new_slots written under leg and anything before the window dropped."""
    oldest = _window_days(now_utc, tz)[0]
    cutoff = datetime(oldest.year, oldest.month, oldest.day, tzinfo=tz).astimezone(timezone.utc).strftime(BILLING_SLOT_FORMAT)
    merged = {name: dict(slots) for name, slots in (cache or {}).items()}
    merged.setdefault(leg, {}).update(new_slots or {})
    for name in merged:
        merged[name] = {key: slot for key, slot in merged[name].items() if key >= cutoff}
    return merged


def days_to_fetch(cache, leg, now_utc, tz, settled=()):
    """Return the local dates in the window that are not yet complete for leg, leaving out settled ones (YYYY-MM-DD)."""
    slots = (cache or {}).get(leg) or {}
    return [day for day in _window_days(now_utc, tz) if day.isoformat() not in settled and not _day_complete(slots, day, tz)]


def _complete_until(slots):
    """End of the last half-hour in the unbroken run from the oldest cached one, as a UTC slot key."""
    if not slots:
        return None
    keys = sorted(slots)
    when = datetime.strptime(keys[0], BILLING_SLOT_FORMAT).replace(tzinfo=timezone.utc)
    while when.strftime(BILLING_SLOT_FORMAT) in slots:
        when += timedelta(minutes=30)
    return when.strftime(BILLING_SLOT_FORMAT)


def _sum_buckets(slots):
    """Total each bucket label's kWh and cost across slots."""
    totals = {}
    for slot in slots:
        for label, bucket in slot["buckets"].items():
            total = totals.setdefault(label, {"kwh": 0.0, "cost": 0.0})
            total["kwh"] = round(total["kwh"] + bucket["kwh"], 4)
            total["cost"] = round(total["cost"] + bucket["cost"], 4)
    return totals


def _day_summary(cache, legs, day, tz):
    """Summarise one local day's billing, or None when it has no import billing at all."""
    keys = [start.strftime(BILLING_SLOT_FORMAT) for start in day_slot_starts(day, tz)]
    imports = [cache.get("import", {})[key] for key in keys if key in cache.get("import", {})]
    if not imports:
        return None
    exports = [cache.get("export", {})[key] for key in keys if key in cache.get("export", {})]
    import_complete = len(imports) == len(keys)
    export_complete = len(exports) == len(keys)
    # With an export meter, a day without all its export (not published, or the read failed) would give a wrong net cost
    export_expected = legs.get("export") is not None
    return {
        "complete": import_complete and (export_complete or not export_expected),
        "import_cost": round(sum(slot["cost"] for slot in imports), 4),
        "export_cost": round(sum(slot["cost"] for slot in exports), 4) if export_complete else None,
        "standing": round(sum(slot["standing"] for slot in imports), 4),
        "import_kwh": round(sum(slot["kwh"] for slot in imports), 4),
        "export_kwh": round(sum(slot["kwh"] for slot in exports), 4) if export_complete else None,
        "buckets": _sum_buckets(imports),
        "export_buckets": _sum_buckets(exports) if export_complete else {},
        "complete_until": _complete_until({key: True for key in keys if key in cache.get("import", {})}),
    }


def build_billing_sensor(cache, legs, supplier, now_utc, tz):
    """Build the sensor.predbat_billing state and attributes from the cache.

    The state is the most recent complete day's net energy cost in pence (import less export,
    standing charge excluded), or None when no day is complete. legs records which directions are
    being read: True read fine, False the last read failed, None not configured.
    """
    cache = cache or {}
    days = {}
    for day in _window_days(now_utc, tz):
        summary = _day_summary(cache, legs, day, tz)
        if summary:
            days[day.isoformat()] = summary
    state = None
    for name in sorted(days, reverse=True):
        if days[name]["complete"]:
            state = round(days[name]["import_cost"] - (days[name]["export_cost"] or 0.0), 2)
            break
    attributes = {
        "friendly_name": "Billing ({})".format(supplier),
        "unit_of_measurement": "p",
        "icon": "mdi:receipt-text",
        "supplier": supplier,
        "legs": dict(legs),
        "complete_until": _complete_until(cache.get("import") or {}),
        "slots": {leg: dict(cache.get(leg) or {}) for leg in BILLING_LEGS if cache.get(leg)},
        "days": days,
    }
    return state, attributes


def _zone(tz):
    """Return tz as a zoneinfo timezone; components may hold a name or a pytz zone."""
    if isinstance(tz, ZoneInfo):
        return tz
    return ZoneInfo(str(tz))


class BillingReader:
    """Keeps the 7-day billing cache for one supplier account: fetches, persists and publishes it.

    owner is the component (OctopusAPI or KrakenAPI); its log, storage, local_tz, prefix and
    dashboard_item are used. Storage is read through the owner each time because it is a lazy
    property that may not be available when the component is constructed.
    """

    def __init__(self, owner, storage_app, storage_name):
        """Create an empty reader; call load() to restore the cache from storage."""
        self.owner = owner
        self.storage_app = storage_app
        self.storage_name = storage_name
        self.cache = {}
        self.legs = {"import": None, "export": None}
        self.fetched_at = None
        # The meters the cache was read from; a different meter (or account) starts the cache afresh
        self.mpans = None
        # Per leg, the days (YYYY-MM-DD) read when already BILLING_SETTLE_DAYS old, which are not read again
        self.settled = {leg: [] for leg in BILLING_LEGS}

    async def load(self):
        """Restore the cache, legs and last fetch time from storage, if any were saved."""
        storage = self.owner.storage
        data = await storage.load(self.storage_app, self.storage_name) if storage else None
        if not isinstance(data, dict):
            return
        self.cache = data.get("slots") or {}
        self.legs = data.get("legs") or self.legs
        self.mpans = data.get("mpans")
        settled = data.get("settled") or {}
        self.settled = {leg: list(settled.get(leg) or []) for leg in BILLING_LEGS}
        fetched_at = data.get("fetched_at")
        if isinstance(fetched_at, str):
            try:
                fetched_at = datetime.fromisoformat(fetched_at)
            except ValueError:
                fetched_at = None
        # A stamp without a timezone can't be compared with now; treat it as never fetched
        self.fetched_at = fetched_at if isinstance(fetched_at, datetime) and fetched_at.tzinfo else None

    async def save(self):
        """Persist the cache through the Storage component."""
        storage = self.owner.storage
        if not storage:
            return
        blob = {"slots": self.cache, "legs": self.legs, "mpans": self.mpans, "settled": self.settled, "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None}
        await storage.save(self.storage_app, self.storage_name, blob, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(days=BILLING_DAYS * 2))

    def due(self, now_utc):
        """True when the billing has never been read, or was last read BILLING_REFRESH_MINUTES ago."""
        if self.fetched_at is None:
            return True
        return now_utc - self.fetched_at >= timedelta(minutes=BILLING_REFRESH_MINUTES)

    async def refresh(self, query_fn, mpans, now_utc):
        """Read every day in the window that is not complete yet, for each configured leg.

        mpans maps "import"/"export" to (account_id, mpan), or None when that leg isn't configured.
        query_fn(query, context) returns the GraphQL data, or None for a failed read; a failed read
        marks the leg False and leaves what was cached alone. A day read once it is BILLING_SETTLE_DAYS old
        is settled and not read again, however much of it arrived. Returns True when new slots arrived.
        """
        tz = _zone(self.owner.local_tz)
        changed = False
        window = [day.isoformat() for day in _window_days(now_utc, tz)]
        settle_by = (now_utc.astimezone(tz).date() - timedelta(days=BILLING_SETTLE_DAYS)).isoformat()
        meters = {leg: (mpans.get(leg) or (None, None))[1] for leg in BILLING_LEGS}
        if self.mpans is not None and self.mpans != meters:
            self.owner.log("Billing: meters changed, discarding the billing read for the previous meters")
            self.cache = {}
            self.settled = {leg: [] for leg in BILLING_LEGS}
        self.mpans = meters
        for leg in BILLING_LEGS:
            settled = [name for name in self.settled.get(leg) or [] if name in window]
            target = mpans.get(leg)
            if not target:
                self.legs[leg] = None
                self.settled[leg] = settled
                continue
            account_id, mpan = target
            ok = True
            for day in days_to_fetch(self.cache, leg, now_utc, tz, settled):
                starts = day_slot_starts(day, tz)
                query = BILLING_MEASUREMENTS_QUERY.format(
                    account_id=account_id,
                    mpan=mpan,
                    direction=BILLING_DIRECTIONS[leg],
                    start_at=starts[0].strftime(BILLING_SLOT_FORMAT),
                    end_at=(starts[-1] + timedelta(minutes=30)).strftime(BILLING_SLOT_FORMAT),
                )
                slots = parse_measurements(await query_fn(query, "billing-{}-{}".format(leg, day.isoformat())))
                if slots is None:
                    ok = False
                    break
                changed = changed or bool(slots)
                self.cache = merge_slots(self.cache, leg, slots, now_utc, tz)
                if day.isoformat() <= settle_by:
                    settled.append(day.isoformat())
                    if not _day_complete(self.cache.get(leg) or {}, day, tz):
                        self.owner.log("Warn: Billing: the {} billing for {} on MPAN ...{} is still incomplete after {} days, so it won't be read again".format(leg, day.isoformat(), str(mpan)[-4:], BILLING_SETTLE_DAYS))
            self.settled[leg] = settled
            self.legs[leg] = ok
            if not ok:
                self.owner.log("Warn: Billing: could not read the {} billing for MPAN ...{}, keeping what was already read".format(leg, str(mpan)[-4:]))
        self.cache = merge_slots(self.cache, "import", {}, now_utc, tz)
        self.fetched_at = now_utc
        await self.save()
        return changed

    def sensor(self, supplier, now_utc):
        """Return (state, attributes) for sensor.predbat_billing from the cache."""
        return build_billing_sensor(self.cache, self.legs, supplier, now_utc, _zone(self.owner.local_tz))

    def publish(self, supplier, now_utc, app):
        """Publish sensor.predbat_billing, unless there is no import billing to show yet."""
        if not self.cache.get("import"):
            return
        state, attributes = self.sensor(supplier, now_utc)
        self.owner.dashboard_item(billing_entity_id(self.owner.prefix), state=state, attributes=attributes, app=app)


# Rate tags Predbat puts on a minute (fetch.py/octopus.py/axle.py rate_replicate) that mean the
# bill will not match its rate: a saving/VPP event pays separately, an override isn't the tariff.
BILLING_EVENT_LABELS = {"saving": "Saving event", "free": "Free electricity", "axle": "Axle event", "manual": "Manual rate", "user": "Manual rate", "increment": "Rate adjustment"}


def _pounds(pence):
    """Pence to the currency's major unit, as the plan JSON's cost fields are."""
    return round(pence / 100.0, 2)


def _row_slot_keys(view_midnight_utc, slot_minute, plan_interval_minutes):
    """The 30-minute billing slot keys inside one plan row, by UTC instant."""
    start = view_midnight_utc + timedelta(minutes=slot_minute)
    return [(start + timedelta(minutes=offset)).astimezone(timezone.utc).strftime(BILLING_SLOT_FORMAT) for offset in range(0, max(plan_interval_minutes, 30), 30)]


def _half_hours_in_day(key, counts):
    """How many half-hours the London day holding slot key has (48, or 46/50 on a clock change); counts caches it per day."""
    day = datetime.strptime(key, BILLING_SLOT_FORMAT).replace(tzinfo=timezone.utc).astimezone(BILLING_TZ).date()
    if day not in counts:
        counts[day] = len(day_slot_starts(day, BILLING_TZ))
    return counts[day]


def _row_event(events, slot_minute, plan_interval_minutes):
    """The display label for the first known event inside a row, or None."""
    for minute in range(slot_minute, slot_minute + plan_interval_minutes):
        label = BILLING_EVENT_LABELS.get(events.get(minute))
        if label:
            return label
    return None


def attach_billing(plan_json, billing, view_midnight_utc, plan_interval_minutes, predbat_slot_cost, standing_pence, events, import_only, predbat_import_kwh, predbat_export_kwh):
    """Add the billed cost, Diff and a day summary to a History view's plan JSON, in place.

    billing is the sensor.predbat_billing attributes dict. predbat_slot_cost maps each row's slot_minute
    to Predbat's own cost for it in pence, already without the standing charge (net, or import-only when
    import_only), and predbat_import_kwh / predbat_export_kwh map it to Predbat's recorded grid kWh (or are
    None). Predbat's daily standing charge (standing_pence) and the billed one are reported separately.

    Every row gets the billing fields, so the Billed and Diff columns show even where nothing is billed yet:
    - a row whose every half-hour is billed (and its export too, when export is read) shows Billed and Diff
      and counts in the summary;
    - a row whose import is billed but whose export hasn't been published yet shows the import charge,
      flagged billing_pending_export, with no Diff, and is left out of the summary - no net figure is claimed;
    - any other row shows no billing.
    Billing whose rates don't match Predbat's is not shown at all. The summary is only added when at least
    one row is fully billed. Returns plan_json; it is left untouched when there is no billing to match.
    """
    slots = (billing or {}).get("slots") or {}
    imports = slots.get("import") or {}
    if not plan_json or not imports or plan_interval_minutes % 30 != 0:
        # Rows shorter than (or not a multiple of) the half-hourly bill can't be matched to it
        return plan_json
    exports = {} if import_only else (slots.get("export") or {})
    # An export meter whose billing hasn't arrived (or failed to read) means no net bill for that row yet
    export_required = not import_only and ((billing.get("legs") or {}).get("export") is not None)
    events = events or {}
    day_half_hours = {}
    updates = []
    totals = {"predbat": 0.0, "billed": 0.0, "standing_predbat": 0.0, "standing_billed": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "predbat_import_kwh": 0.0, "predbat_export_kwh": 0.0, "rate_cost": 0.0, "rate_kwh": 0.0, "predbat_rate_cost": 0.0}
    for row in plan_json.get("rows") or []:
        slot_minute = row.get("slot_minute")
        if slot_minute is None:
            continue
        keys = _row_slot_keys(view_midnight_utc, slot_minute, plan_interval_minutes)
        billed_pence = None
        buckets = None
        full = False
        pending_export = False
        if all(key in imports for key in keys):
            full = not export_required or all(key in exports for key in keys)
            pending_export = not full
            row_exports = exports if full else {}
            billed_pence = 0.0
            buckets = {}
            for key in keys:
                billed_pence += imports[key]["cost"] - (row_exports[key]["cost"] if key in row_exports else 0.0)
                if row.get("import_rate") is not None:
                    # The plausibility check is on the import bill alone, so a row waiting for its export still counts
                    totals["rate_cost"] += imports[key]["cost"]
                    totals["rate_kwh"] += imports[key]["kwh"]
                    totals["predbat_rate_cost"] += imports[key]["kwh"] * row["import_rate"]
                if full:
                    totals["standing_billed"] += imports[key]["standing"]
                    # Predbat's daily standing charge, counted for the billed half-hours only, so a part-billed day compares like with like,
                    # and spread over the day's real half-hours as the supplier does, so a 23 or 25 hour day still adds up to one charge
                    totals["standing_predbat"] += standing_pence / _half_hours_in_day(key, day_half_hours)
                    totals["import_kwh"] += imports[key]["kwh"]
                    totals["export_kwh"] += row_exports[key]["kwh"] if key in row_exports else 0.0
                for label, bucket in imports[key]["buckets"].items():
                    total = buckets.setdefault(label, {"kwh": 0.0, "cost": 0.0})
                    total["kwh"] += bucket["kwh"]
                    total["cost"] += bucket["cost"]
                for label, bucket in row_exports[key]["buckets"].items() if key in row_exports else []:
                    total = buckets.setdefault("Export " + label, {"kwh": 0.0, "cost": 0.0})
                    total["kwh"] += bucket["kwh"]
                    total["cost"] -= bucket["cost"]
            buckets = {label: {"kwh": round(value["kwh"], 3), "cost": _pounds(value["cost"])} for label, value in buckets.items()}
        predbat_pence = predbat_slot_cost.get(slot_minute)
        if full and predbat_pence is not None and abs(predbat_pence) < 0.005 and predbat_import_kwh is not None and abs(predbat_import_kwh.get(slot_minute, 0.0)) < 0.001:
            if sum(imports[key]["kwh"] for key in keys) > BILLING_UNRECORDED_KWH:
                predbat_pence = None
        diff = None
        if full and predbat_pence is not None:
            diff = _pounds(billed_pence - predbat_pence)
            totals["predbat"] += predbat_pence
            totals["billed"] += billed_pence
        if full:
            totals["predbat_import_kwh"] += (predbat_import_kwh or {}).get(slot_minute, 0.0)
            totals["predbat_export_kwh"] += (predbat_export_kwh or {}).get(slot_minute, 0.0)
        updates.append((row, None if billed_pence is None else _pounds(billed_pence), buckets, diff, _row_event(events, slot_minute, plan_interval_minutes), full, pending_export))
    if not updates:
        return plan_json
    plausible = True
    if totals["rate_kwh"] > 0.5 and totals["predbat_rate_cost"] > 0:
        ratio = totals["rate_cost"] / totals["predbat_rate_cost"]
        plausible = BILLING_PLAUSIBLE_RATE_RATIO[0] <= ratio <= BILLING_PLAUSIBLE_RATE_RATIO[1]
    for row, billed, buckets, diff, event, full, pending_export in updates:
        # Billing that doesn't match the rates leaves the columns empty rather than showing a wrong figure
        row["billed"] = billed if plausible else None
        row["billed_buckets"] = buckets if plausible else None
        row["billing_diff"] = diff if plausible else None
        row["billing_pending_export"] = pending_export and plausible
        row["billing_event"] = event
    if not plausible or not any(update[5] for update in updates):
        return plan_json
    last_billed = max(index for index, update in enumerate(updates) if update[5])
    missing_slots = sum(1 for update in updates[:last_billed] if not update[5])
    billed_until = (view_midnight_utc + timedelta(minutes=updates[last_billed][0]["slot_minute"] + plan_interval_minutes)).astimezone(timezone.utc).strftime(BILLING_SLOT_FORMAT)
    plan_json["billing_summary"] = {
        "predbat_cost": _pounds(totals["predbat"]),
        "billed_cost": _pounds(totals["billed"]),
        "diff": _pounds(totals["billed"] - totals["predbat"]),
        "standing_predbat": _pounds(totals["standing_predbat"]),
        "standing_billed": _pounds(totals["standing_billed"]),
        "import_kwh": [None if predbat_import_kwh is None else round(totals["predbat_import_kwh"], 2), round(totals["import_kwh"], 2)],
        "export_kwh": [None if predbat_export_kwh is None else round(totals["predbat_export_kwh"], 2), None if import_only or not exports else round(totals["export_kwh"], 2)],
        "billed_until": billed_until,
        "missing_slots": missing_slots,
        "export_pending_slots": sum(1 for update in updates if update[6]),
        "import_only": bool(import_only),
        "supplier": billing.get("supplier"),
    }
    return plan_json

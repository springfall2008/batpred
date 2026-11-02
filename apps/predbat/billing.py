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
a 7-day cache of slots, published as ``predbat.billing`` so the Plan page's History view can show
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

# How far a half-hour's bucket kWh may stray from its metered kWh before it is not trusted.
BUCKET_KWH_TOLERANCE = 0.01


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
        rows.append((stat.get("label") or stat.get("type") or "unknown", _to_float(stat.get("value")), cost or 0.0))
    buckets = {}
    for label, bucket_kwh, cost in rows:
        if bucket_kwh is None:
            # Export's GENERATION_VALUE row carries no kWh of its own; it is the node's, but only
            # when it is the sole bucket - otherwise there is no telling how the kWh split.
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


def days_to_fetch(cache, leg, now_utc, tz):
    """Return the local dates in the window that are not yet complete for leg."""
    slots = (cache or {}).get(leg) or {}
    return [day for day in _window_days(now_utc, tz) if not _day_complete(slots, day, tz)]


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
    # A day whose export is expected but not all published would give a net cost that is wrong
    export_expected = legs.get("export") is True
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
    """Build the predbat.billing state and attributes from the cache.

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

    async def load(self):
        """Restore the cache, legs and last fetch time from storage, if any were saved."""
        storage = self.owner.storage
        data = await storage.load(self.storage_app, self.storage_name) if storage else None
        if not isinstance(data, dict):
            return
        self.cache = data.get("slots") or {}
        self.legs = data.get("legs") or self.legs
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
        blob = {"slots": self.cache, "legs": self.legs, "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None}
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
        marks the leg False and leaves what was cached alone. Returns True when new slots arrived.
        """
        tz = _zone(self.owner.local_tz)
        changed = False
        for leg in BILLING_LEGS:
            target = mpans.get(leg)
            if not target:
                self.legs[leg] = None
                continue
            account_id, mpan = target
            ok = True
            for day in days_to_fetch(self.cache, leg, now_utc, tz):
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
            self.legs[leg] = ok
            if not ok:
                self.owner.log("Warn: Billing: could not read the {} billing for MPAN ...{}, keeping what was already read".format(leg, str(mpan)[-4:]))
        self.cache = merge_slots(self.cache, "import", {}, now_utc, tz)
        self.fetched_at = now_utc
        await self.save()
        return changed

    def sensor(self, supplier, now_utc):
        """Return (state, attributes) for predbat.billing from the cache."""
        return build_billing_sensor(self.cache, self.legs, supplier, now_utc, _zone(self.owner.local_tz))

    def publish(self, supplier, now_utc, app):
        """Publish predbat.billing, unless there is no import billing to show yet."""
        if not self.cache.get("import"):
            return
        state, attributes = self.sensor(supplier, now_utc)
        self.owner.dashboard_item(self.owner.prefix + "." + BILLING_ENTITY_SUFFIX, state=state, attributes=attributes, app=app)

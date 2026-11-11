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

import asyncio
import random
import traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from utils import str2time

BILLING_DAYS = 7
BILLING_REFRESH_MINUTES = 180
# With plan_random_delay set (Predbat.com runs many accounts behind one address), the first read after a start is held
# back by up to this many seconds, and each day's query after the first waits up to BILLING_DAY_PACE_SECONDS, so a
# fleet restarted together doesn't send its whole first fill to Kraken at once
BILLING_FIRST_READ_DELAY_SECONDS = 30 * 60
BILLING_DAY_PACE_SECONDS = 60
BILLING_ENTITY_SUFFIX = "billing"
BILLING_SLOT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
BILLING_LEGS = ("import", "export")
# A past day is read again each refresh until it is complete, but once it is this many days old one more read settles it:
# import is published within a day or two but export can lag several days behind it, so a half-hour still missing by then
# (or a meter that publishes nothing) is not coming
BILLING_SETTLE_DAYS = 5
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

# Some events are billed at the full rate in the half-hours and refunded afterwards as an account credit: Octopus
# bills a Weekend Happy Hour's import at the normal rate and posts a "Weekend Happy Hour" credit a day or two later
# (verified live 10 Nov 2026). The credit says when it was posted, not which event it pays for.
BILLING_CREDITS_QUERY = """query {{
  account(accountNumber: "{account_id}") {{
    transactions(first: 50) {{
      edges {{
        node {{
          __typename
          id
          postedDate
          createdAt
          title
          amounts {{ gross }}
        }}
      }}
    }}
  }}
}}"""
# Credit titles (lower case, matched as a substring) that pay back an event's billed import
BILLING_CREDIT_TITLES = ("happy hour",)
# Credits are kept this long, so one posted a few days after its event is still there to match
BILLING_CREDIT_DAYS = BILLING_DAYS * 2
# A credit is only matched to an event that started at most this many days before it was posted (Octopus posts
# them a day or two after), so an older event's credit can't be pinned on a recent one
BILLING_CREDIT_LAG_DAYS = 4
# A credit is only matched to an event whose billed import it is within this ratio of, so a credit that pays
# for an event outside the billing window isn't pinned on a different one
BILLING_CREDIT_RATIO = (0.5, 2.0)


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


def billing_slot_key(stamp):
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
            key = billing_slot_key(node.get("startAt"))
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


def parse_credits(response_data, now_utc):
    """Parse a transactions response into the event credits posted in the last BILLING_CREDIT_DAYS, newest last.

    Each is {"id", "posted" (YYYY-MM-DD), "created", "title", "amount" (pence)}. Returns None for a failed read
    (response_data None or no transactions in it) so the caller keeps what it had.
    """
    if response_data is None:
        return None
    transactions = (response_data.get("account") or {}).get("transactions")
    if not isinstance(transactions, dict):
        return None
    oldest = (now_utc - timedelta(days=BILLING_CREDIT_DAYS)).date().isoformat()
    credits = []
    for edge in transactions.get("edges") or []:
        node = (edge or {}).get("node") or {}
        title = str(node.get("title") or "")
        amount = _to_float((node.get("amounts") or {}).get("gross"))
        posted = str(node.get("postedDate") or "")
        if node.get("__typename") != "Credit" or amount is None or amount <= 0 or not posted or posted < oldest:
            continue
        if not any(match in title.lower() for match in BILLING_CREDIT_TITLES):
            continue
        credits.append({"id": str(node.get("id")), "posted": posted, "created": str(node.get("createdAt") or ""), "title": title, "amount": amount})
    return sorted(credits, key=lambda credit: (credit["posted"], credit["created"], credit["id"]))


def _event_slot_keys(event):
    """The half-hour slot keys an event covers, from its start (rounded down to a half-hour) up to its end; [] when it can't be read."""
    try:
        start = datetime.strptime(event["start"], BILLING_SLOT_FORMAT).replace(tzinfo=timezone.utc)
        end = datetime.strptime(event["end"], BILLING_SLOT_FORMAT).replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return []
    start = start.replace(minute=(start.minute // 30) * 30, second=0)
    keys = []
    while start < end:
        keys.append(start.strftime(BILLING_SLOT_FORMAT))
        start += timedelta(minutes=30)
    return keys


def match_credits(credits, events, import_slots, tz):
    """Pair each credit with the event it pays for; returns the events as dicts with their billed import and credit.

    events are {"start", "end"} UTC slot keys. A credit doesn't say which event it pays for, so each one, oldest
    first, goes to the event not yet paid whose billed import is closest to it, among those that started on the
    day it was posted or up to BILLING_CREDIT_LAG_DAYS before, and whose billed import it is within BILLING_CREDIT_RATIO of. An event whose
    half-hours aren't all billed has no billed figure and takes no credit. Each returned event is
    {"start", "end", "billed" (pence or None), "credit" (pence or None), "posted" (YYYY-MM-DD or None)}.
    """
    import_slots = import_slots or {}
    matched = []
    for event in sorted(events or [], key=lambda item: item.get("start") or ""):
        keys = _event_slot_keys(event)
        if not keys:
            continue
        billed = round(sum(import_slots[key]["cost"] for key in keys), 4) if all(key in import_slots for key in keys) else None
        day = datetime.strptime(keys[0], BILLING_SLOT_FORMAT).replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
        matched.append({"start": event["start"], "end": event["end"], "billed": billed, "credit": None, "posted": None, "day": day})
    for credit in credits or []:
        best = None
        try:
            earliest = (date.fromisoformat(credit["posted"]) - timedelta(days=BILLING_CREDIT_LAG_DAYS)).isoformat()
        except ValueError:
            continue
        for event in matched:
            if event["credit"] is not None or not event["billed"] or event["day"] > credit["posted"] or event["day"] < earliest:
                continue
            ratio = credit["amount"] / event["billed"]
            if not BILLING_CREDIT_RATIO[0] <= ratio <= BILLING_CREDIT_RATIO[1]:
                continue
            if best is None or abs(event["billed"] - credit["amount"]) < abs(best["billed"] - credit["amount"]):
                best = event
        if best is not None:
            best["credit"] = credit["amount"]
            best["posted"] = credit["posted"]
    return matched


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


def build_billing_sensor(cache, legs, supplier, now_utc, tz, credits=None, credit_events=None):
    """Build the sensor.predbat_billing state and attributes from the cache.

    The state is the most recent complete day's net energy cost in pence (import less export,
    standing charge excluded), or None when no day is complete. legs records which directions are
    being read: True read fine, False the last read failed, None not configured. credit_events are the
    events (UTC slot keys) whose import is refunded later as one of credits; they are published with the
    credit matched to each (match_credits()), and each day's matched credits are totalled in its summary.
    The state is the bill itself, before any credit.
    """
    cache = cache or {}
    events = match_credits(credits, credit_events, cache.get("import"), tz) if credit_events else []
    days = {}
    for day in _window_days(now_utc, tz):
        summary = _day_summary(cache, legs, day, tz)
        if summary:
            day_events = [event for event in events if event["day"] == day.isoformat()]
            if day_events:
                summary["credits"] = round(sum(event["credit"] or 0.0 for event in day_events), 4)
                summary["credits_pending"] = sum(1 for event in day_events if event["credit"] is None)
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
    if events:
        oldest = min(days) if days else None
        attributes["credit_events"] = [{key: event[key] for key in ("start", "end", "billed", "credit", "posted")} for event in events if oldest and event["day"] >= oldest]
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
        # Set by the owner from plan_random_delay: hold back the first read and pace the day queries in the background
        self.random_delay = False
        # When the first read after this start may go ahead, picked on the first due() check while random_delay is on
        self.first_read_after = None
        # The background read started by read() while random_delay is on, so a second one isn't started over it
        self.task = None
        # Event credits read from the account (parse_credits()), and the account to read them from, or None not to
        self.credits = []
        self.credits_account = None
        # Set by the owner: the events ({"start", "end"} UTC slot keys) whose import is refunded as a credit
        self.credit_events = []

    async def load(self):
        """Restore the cache, legs and last fetch time from storage, if any were saved."""
        storage = self.owner.storage
        data = await storage.load(self.storage_app, self.storage_name) if storage else None
        if not isinstance(data, dict):
            return
        self.cache = data.get("slots") or {}
        self.legs = data.get("legs") or self.legs
        self.credits = data.get("credits") or []
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
        blob = {"slots": self.cache, "legs": self.legs, "mpans": self.mpans, "settled": self.settled, "credits": self.credits, "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None}
        await storage.save(self.storage_app, self.storage_name, blob, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(days=BILLING_DAYS * 2))

    def running(self):
        """True while a background read started by read() hasn't finished."""
        return self.task is not None and not self.task.done()

    def due(self, now_utc):
        """True when the billing has never been read, or was last read BILLING_REFRESH_MINUTES ago.

        With random_delay on, the first read after a start also waits until a random moment up to
        BILLING_FIRST_READ_DELAY_SECONDS after the first check, and nothing is due while a read is running.
        """
        if self.running():
            return False
        if self.random_delay:
            if self.first_read_after is None:
                delay = random.uniform(0, BILLING_FIRST_READ_DELAY_SECONDS)  # nosec B311 - jitter, not security
                self.first_read_after = now_utc + timedelta(seconds=delay)
                self.owner.log("Billing: holding the first billing read back by a random {:.1f} minutes".format(delay / 60))
            if now_utc < self.first_read_after:
                return False
        if self.fetched_at is None:
            return True
        return now_utc - self.fetched_at >= timedelta(minutes=BILLING_REFRESH_MINUTES)

    async def read(self, query_fn, mpans, now_utc, supplier, app):
        """Refresh the billing and publish it.

        With random_delay on the refresh paces its day queries, which can take many minutes, so it runs
        as a background task instead and publishes when it finishes; the owner's run() carries on meanwhile.
        """
        if not self.random_delay:
            await self.refresh(query_fn, mpans, now_utc)
            self.publish(supplier, now_utc, app)
            return
        self.task = asyncio.ensure_future(self._paced_read(query_fn, mpans, now_utc, supplier, app))

    async def _paced_read(self, query_fn, mpans, now_utc, supplier, app):
        """The background half of read(): a paced refresh then a publish, logging anything that goes wrong."""
        try:
            await self.refresh(query_fn, mpans, now_utc, pace_seconds=BILLING_DAY_PACE_SECONDS)
            self.publish(supplier, datetime.now(timezone.utc), app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.owner.log("Error: Billing: the background billing read failed: {}".format(e))
            self.owner.log("Error: " + traceback.format_exc())

    def stop(self):
        """Cancel a background read that is still running, when the owner stops."""
        if self.running():
            self.task.cancel()

    async def refresh(self, query_fn, mpans, now_utc, pace_seconds=0):
        """Read every day in the window that is not complete yet, for each configured leg.

        mpans maps "import"/"export" to (account_id, mpan), or None when that leg isn't configured.
        query_fn(query, context) returns the GraphQL data, or None for a failed read; a failed read
        marks the leg False and leaves what was cached alone. A day read once it is BILLING_SETTLE_DAYS old
        is settled and not read again, however much of it arrived. With pace_seconds, each query after the
        first waits a random 0 to pace_seconds first. With credits_account set, the account's event credits are
        read too (one query; a failed read keeps the ones already read). Returns True when new slots or credits arrived.
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
        queried = False
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
                if pace_seconds and queried:
                    await asyncio.sleep(random.uniform(0, pace_seconds))  # nosec B311 - jitter, not security
                queried = True
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
        if self.credits_account:
            credits = parse_credits(await query_fn(BILLING_CREDITS_QUERY.format(account_id=self.credits_account), "billing-credits"), now_utc)
            if credits is None:
                self.owner.log("Warn: Billing: could not read the account's credits, keeping what was already read")
            else:
                changed = changed or credits != self.credits
                self.credits = credits
        self.fetched_at = now_utc
        await self.save()
        return changed

    def sensor(self, supplier, now_utc):
        """Return (state, attributes) for sensor.predbat_billing from the cache."""
        return build_billing_sensor(self.cache, self.legs, supplier, now_utc, _zone(self.owner.local_tz), self.credits, self.credit_events)

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


def _parse_event_time(value):
    """An Axle event's start or end time string as an aware datetime, or None when it can't be read."""
    if not isinstance(value, str):
        return None
    try:
        # As load_axle_slot() reads them, so the same events are found here
        return str2time(value)
    except (ValueError, TypeError):
        return None


def parse_axle_events(axle_events):
    """The Axle VPP events (the axle_session sensor's current and past events) as (start, end, import_export, pence_per_kwh) tuples.

    Events that can't be read, or have no price or direction, are dropped; a zero price is kept, as load_axle_slot() keeps it.
    """
    parsed = []
    for event in axle_events or []:
        if not isinstance(event, dict):
            continue
        start = _parse_event_time(event.get("start_time"))
        end = _parse_event_time(event.get("end_time"))
        direction = event.get("import_export")
        pence = _to_float(event.get("pence_per_kwh"))
        if start and end and start < end and direction in ("import", "export") and pence is not None:
            parsed.append((start, end, direction, pence))
    return parsed


def saving_session_events(joined_events, octopoints_per_penny, default_rate_pence):
    """The joined Octopus saving sessions (the octopus_saving_session sensor's joined_events) as Predbat priced them, for the History view.

    Each is {start, end, pence_per_kwh, octopoints_per_kwh, rewarded_octopoints, id}, with the rate fetch_octopus_sessions() adds to the
    import and export rates: the event's Octopoints per kWh over octopus_saving_session_octopoints_per_penny, or default_rate_pence (the
    octopus_saving_session_rate setting) when the event reports none. A Weekend Happy Hour is free import, not a saving session, and an
    event Predbat priced at nothing added nothing, so both are left out, as are events that can't be read.
    """
    events = []
    per_penny = _to_float(octopoints_per_penny) or 0.0
    for event in joined_events or []:
        if not isinstance(event, dict) or event.get("event_type") == "WEEKEND_HAPPY_HOUR":
            continue
        start = _parse_event_time(event.get("start"))
        end = _parse_event_time(event.get("end"))
        points = _to_float(event.get("octopoints_per_kwh"))
        if points is not None and per_penny > 0:
            pence = points / per_penny
        elif points is None and (_to_float(default_rate_pence) or 0.0) > 0:
            pence = _to_float(default_rate_pence)
            points = pence * per_penny
        else:
            continue
        if start and end and start < end and pence > 0:
            events.append(
                {
                    "start": event.get("start"),
                    "end": event.get("end"),
                    "pence_per_kwh": pence,
                    "octopoints_per_kwh": points,
                    "rewarded_octopoints": _to_float(event.get("rewarded_octopoints")),
                    "id": event.get("id"),
                }
            )
    return events


def parse_saving_events(saving_events):
    """saving_session_events() entries as (start, end, pence_per_kwh, octopoints_per_kwh, rewarded_octopoints, id); unreadable ones are dropped."""
    spans = []
    for event in saving_events or []:
        if not isinstance(event, dict):
            continue
        start = _parse_event_time(event.get("start"))
        end = _parse_event_time(event.get("end"))
        pence = _to_float(event.get("pence_per_kwh"))
        points = _to_float(event.get("octopoints_per_kwh"))
        if start and end and start < end and pence is not None and points is not None:
            spans.append((start, end, pence, points, _to_float(event.get("rewarded_octopoints")), event.get("id") or event.get("start")))
    return spans


def _row_axle(axle_events, row_start, row_end):
    """The Axle premiums Predbat priced into one row: (export event pence/kWh, import event pence/kWh), each weighted by how much of the row the event covers, or None when no event overlaps it.

    A premium can be negative (a managed price curve can go below zero) or the two can cancel, so being inside an event is not judged by the premiums' sign.
    The premium is spread over the row's kWh by time, which is exact for events on the half-hour boundaries the rows use.
    """
    export_premium = 0.0
    import_premium = 0.0
    inside = False
    row_seconds = (row_end - row_start).total_seconds()
    for start, end, direction, pence in axle_events:
        overlap = (min(end, row_end) - max(start, row_start)).total_seconds()
        if overlap <= 0:
            continue
        inside = True
        if direction == "export":
            export_premium += pence * overlap / row_seconds
        else:
            import_premium += pence * overlap / row_seconds
    return (export_premium, import_premium) if inside else None


def _row_axle_at_start(axle_events, row_start):
    """The Axle premium Predbat added to the import rate at a row's first minute, where the row's import_rate is read: an export event's pence/kWh less an import event's."""
    premium = 0.0
    for start, end, direction, pence in axle_events:
        if start <= row_start < end:
            premium += pence if direction == "export" else -pence
    return premium


def attach_billing(plan_json, billing, view_midnight_utc, plan_interval_minutes, predbat_slot_cost, standing_pence, events, import_only, predbat_import_kwh, predbat_export_kwh, axle_events=None, saving_events=None):
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
    A row inside one of billing's credit_events (an event billed at the full rate and refunded later as a credit) is
    flagged billing_credit "credited" or "pending" (no credit posted yet), and the summary's diff takes off the credits
    of the events in its fully billed rows.
    A fully billed row inside one of axle_events (Axle VPP events, paid by Axle and not on the bill) is flagged billing_axle with an
    estimate of what Predbat priced in for Axle there, and its Diff and the summary's diff are after those estimates: Predbat
    adds an export event's pence/kWh to both its export and import rates (the import part is a planning cost, not a charge) and
    takes an import event's off its import rate, while the bill carries the supplier's rates alone.
    A fully billed row inside one of saving_events (saving_session_events(): joined Octopus saving sessions, rewarded in Octopoints
    rather than money) is flagged billing_saving with the Octopoints Predbat priced in there. Predbat adds the session's pence/kWh to both
    rates, so that value is put back on Predbat's cost, which the row's Diff and the summary then compare at the tariff's rates alone;
    the points are reported apart, with what Octopus has awarded for the sessions once it posts it.
    Billing whose rates don't match Predbat's is not shown at all. The summary is only added when at least
    one row is fully billed. Returns plan_json; it is left untouched only when billing holds no import half-hours at
    all or rows can't be matched to half-hours - a view with nothing billed in it still gets the (empty) fields.
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
    # Each half-hour of an event that is refunded as a credit, mapped to its event
    credit_slots = {}
    for credit_event in billing.get("credit_events") or []:
        for key in _event_slot_keys(credit_event):
            credit_slots[key] = credit_event
    credited = {}
    axle = parse_axle_events(axle_events)
    saving = parse_saving_events(saving_events)
    # As export-direction premiums, which is how Predbat prices a saving session: on both rates, in pence and in points
    saving_pence = [(start, end, "export", pence) for start, end, pence, _, _, _ in saving]
    saving_points = [(start, end, "export", points) for start, end, _, points, _, _ in saving]
    saving_seen = {}
    day_half_hours = {}
    updates = []
    totals = {"predbat": 0.0, "billed": 0.0, "standing_predbat": 0.0, "standing_billed": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "predbat_import_kwh": 0.0, "predbat_export_kwh": 0.0, "rate_cost": 0.0, "rate_kwh": 0.0, "predbat_rate_cost": 0.0}
    axle_total = 0.0
    saving_total = 0.0
    saving_points_total = 0.0
    for row in plan_json.get("rows") or []:
        slot_minute = row.get("slot_minute")
        if slot_minute is None:
            continue
        keys = _row_slot_keys(view_midnight_utc, slot_minute, plan_interval_minutes)
        row_start = view_midnight_utc + timedelta(minutes=slot_minute)
        premiums = _row_axle(axle, row_start, row_start + timedelta(minutes=plan_interval_minutes))
        axle_row = premiums is not None
        row_end = row_start + timedelta(minutes=plan_interval_minutes)
        saving_premiums = _row_axle(saving_pence, row_start, row_end)
        saving_row = saving_premiums is not None
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
                if row.get("import_rate") is not None and key not in credit_slots:
                    # The plausibility check is on the import bill alone, so a row waiting for its export still counts. A credited
                    # event's half-hours are left out: they are billed at the full rate where Predbat priced them free. An Axle row's
                    # import rate is checked without the Axle premium Predbat added to it, which the bill doesn't carry
                    # ... and a saving session's likewise
                    import_rate = row["import_rate"] - (_row_axle_at_start(axle, row_start) if axle_row else 0.0) - (_row_axle_at_start(saving_pence, row_start) if saving_row else 0.0)
                    totals["rate_cost"] += imports[key]["cost"]
                    totals["rate_kwh"] += imports[key]["kwh"]
                    totals["predbat_rate_cost"] += imports[key]["kwh"] * import_rate
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
        row_import_kwh = (predbat_import_kwh or {}).get(slot_minute, 0.0)
        row_export_kwh = (predbat_export_kwh or {}).get(slot_minute, 0.0)
        diff = None
        axle_pence = None
        saving_row_points = None
        if full and predbat_pence is not None:
            if axle_row and predbat_import_kwh is not None:
                # Axle pays the event's pence/kWh on what Predbat recorded crossing the meter in the event's direction; the export
                # event's premium on import is Predbat's planning cost, not a charge, so it is given back. Without Predbat's recorded
                # kWh there is nothing to estimate from, so the row isn't flagged
                export_premium, import_premium = premiums
                axle_pence = export_premium * (row_export_kwh - row_import_kwh) + import_premium * row_import_kwh
                axle_total += axle_pence
            if saving_row and predbat_import_kwh is not None:
                # What Predbat priced the session at on what crossed the meter, put back on its cost: Octopus pays in points, not on the bill
                saving_value = saving_premiums[0] * (row_export_kwh - row_import_kwh)
                saving_row_points = _row_axle(saving_points, row_start, row_end)[0] * (row_export_kwh - row_import_kwh)
                predbat_pence += saving_value
                saving_total += saving_value
                saving_points_total += saving_row_points
                for start, end, _, _, rewarded, event_id in saving:
                    if start < row_end and end > row_start:
                        saving_seen[event_id] = rewarded
            # A row's Diff is after its Axle estimate, as the summary's is
            diff = _pounds(billed_pence - (axle_pence or 0.0) - predbat_pence)
            totals["predbat"] += predbat_pence
            totals["billed"] += billed_pence
        if full:
            totals["predbat_import_kwh"] += row_import_kwh
            totals["predbat_export_kwh"] += row_export_kwh
        # A row longer than a half-hour can hold parts of two adjacent events, so each half-hour is credited to its own event
        row_credit_events = [credit_slots[key] for key in keys if key in credit_slots]
        if full:
            for key in keys:
                credit_event = credit_slots.get(key)
                if credit_event is not None:
                    # Each event once, by its start, with the share of its billed import that went into the totals above, so
                    # its credit is only taken off the bill it pays for
                    entry = credited.setdefault(credit_event["start"], {"event": credit_event, "counted": 0.0})
                    if predbat_pence is not None:
                        entry["counted"] += imports[key]["cost"]
        credit_state = None
        if row_credit_events and billed_pence is not None:
            credit_state = "pending" if any(credit_event.get("credit") is None for credit_event in row_credit_events) else "credited"
        updates.append((row, None if billed_pence is None else _pounds(billed_pence), buckets, diff, _row_event(events, slot_minute, plan_interval_minutes), full, pending_export, credit_state, axle_pence, saving_row_points))
    if not updates:
        return plan_json
    plausible = True
    if totals["rate_kwh"] > 0.5 and totals["predbat_rate_cost"] > 0:
        ratio = totals["rate_cost"] / totals["predbat_rate_cost"]
        plausible = BILLING_PLAUSIBLE_RATE_RATIO[0] <= ratio <= BILLING_PLAUSIBLE_RATE_RATIO[1]
    for row, billed, buckets, diff, event, full, pending_export, credit_state, axle_pence, saving_row_points in updates:
        # Billing that doesn't match the rates leaves the columns empty rather than showing a wrong figure
        row["billed"] = billed if plausible else None
        row["billed_buckets"] = buckets if plausible else None
        row["billing_diff"] = diff if plausible else None
        row["billing_pending_export"] = pending_export and plausible
        # An Axle or saving session row's estimate explains the difference its event label would warn of
        row["billing_event"] = None if (axle_pence is not None or saving_row_points is not None) and plausible else event
        row["billing_credit"] = credit_state if plausible else None
        row["billing_axle"] = _pounds(axle_pence) if plausible and axle_pence is not None else None
        row["billing_saving"] = round(saving_row_points) if plausible and saving_row_points is not None else None
    if not plausible or not any(update[5] for update in updates):
        return plan_json
    last_billed = max(index for index, update in enumerate(updates) if update[5])
    # Rows showing nothing before the last fully billed one; rows waiting for their export show their import and are counted apart
    missing_slots = sum(1 for update in updates[:last_billed] if not update[5] and not update[6])
    credits = 0.0
    for entry in credited.values():
        credit = entry["event"].get("credit") or 0.0
        event_billed = entry["event"].get("billed") or 0.0
        credits += credit * min(1.0, entry["counted"] / event_billed) if event_billed > 0 else 0.0
    billed_until = (view_midnight_utc + timedelta(minutes=updates[last_billed][0]["slot_minute"] + plan_interval_minutes)).astimezone(timezone.utc).strftime(BILLING_SLOT_FORMAT)
    plan_json["billing_summary"] = {
        "predbat_cost": _pounds(totals["predbat"]),
        "billed_cost": _pounds(totals["billed"]),
        # Credits paid back later for events billed at the full rate count against the bill
        # ... and Axle pays its events separately
        "diff": _pounds(totals["billed"] - credits - axle_total - totals["predbat"]),
        "credits": _pounds(credits),
        "axle": _pounds(axle_total),
        "axle_slots": sum(1 for update in updates if update[8] is not None),
        # Saving sessions: Predbat's cost above leaves out the price it gave them, estimated here in Octopoints (and in pence), with the
        # Octopoints Octopus has awarded for the sessions in these rows and how many it hasn't posted yet
        "saving_slots": sum(1 for update in updates if update[9] is not None),
        "saving_points": round(saving_points_total),
        "saving_value": _pounds(saving_total),
        "saving_awarded": round(sum(rewarded for rewarded in saving_seen.values() if rewarded is not None)),
        "saving_awarded_pending": sum(1 for rewarded in saving_seen.values() if rewarded is None),
        "credits_pending": sum(1 for entry in credited.values() if entry["event"].get("credit") is None),
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

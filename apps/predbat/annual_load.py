# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Load profile sources for the annual prediction tool.

Produces the forward cumulative kWh series that Predbat consumes as
``load_forecast`` when ``load_forecast_only`` is set, so no synthetic backwards
history has to be fabricated.
"""

import base64
import calendar
from datetime import date, datetime, timedelta

import aiohttp

from annual_http import fetch_json
from annual_profiles import DAY_BAND_SLOTS, MONTH_WEIGHTS, NIGHT_BAND_SLOTS, SHAPE_TILT_FRACTION, half_hour_shape

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_SLOT = 30


def tilt_shape(shape_values, direction):
    """Move energy between the night and day bands, preserving the total exactly.

    ``direction`` is one of "night" (move day energy into the night band), "day"
    (the reverse), or "flat" (no change). The amount moved is
    ``SHAPE_TILT_FRACTION`` of the source band's own energy, so the transfer can
    never exceed what is available. Energy is taken from and added to individual
    slots in proportion to their existing share of their band, which keeps the
    within-band shape intact.
    """
    if direction == "flat":
        return list(shape_values)

    if direction == "night":
        source_slots, dest_slots = DAY_BAND_SLOTS, NIGHT_BAND_SLOTS
    elif direction == "day":
        source_slots, dest_slots = NIGHT_BAND_SLOTS, DAY_BAND_SLOTS
    else:
        raise ValueError("Unknown load shape '{}', expected night, day or flat".format(direction))

    tilted = list(shape_values)
    source_total = sum(tilted[slot] for slot in source_slots)
    dest_total = sum(tilted[slot] for slot in dest_slots)
    if source_total <= 0 or dest_total <= 0:
        return tilted

    moved = source_total * SHAPE_TILT_FRACTION
    for slot in source_slots:
        tilted[slot] -= moved * (tilted[slot] / source_total)
    for slot in dest_slots:
        tilted[slot] += moved * (shape_values[slot] / dest_total)

    # Push any floating-point residue into the largest slot so the total stays exact
    residue = sum(shape_values) - sum(tilted)
    largest = max(range(len(tilted)), key=lambda index: tilted[index])
    tilted[largest] += residue
    return tilted


class LoadProfileSource:
    """Base class for a source of daily household load profiles."""

    def daily_kwh(self, day):
        """Return the total household kWh for the given date."""
        raise NotImplementedError

    def minute_profile(self, day):
        """Return a list of 1440 per-minute kWh values for the given date, or None if unavailable."""
        raise NotImplementedError


class SyntheticLoadProfile(LoadProfileSource):
    """Load profile synthesised from an annual kWh total and a shape preference.

    Monthly weights are normalised across the specific year's day counts so the
    twelve monthly totals sum to exactly ``annual_kwh``.
    """

    def __init__(self, annual_kwh, shape, year):
        """Build the synthetic profile for one calendar year."""
        self.annual_kwh = float(annual_kwh)
        self.shape = shape
        self.year = year
        self.slot_shape = tilt_shape(half_hour_shape(), shape)

        weighted_days = 0.0
        for month in range(1, 13):
            days_in_month = calendar.monthrange(year, month)[1]
            weighted_days += MONTH_WEIGHTS[month - 1] * days_in_month
        self.base_daily_kwh = (self.annual_kwh / weighted_days) if weighted_days > 0 else 0.0

    def daily_kwh(self, day):
        """Return the total household kWh for the given date."""
        return self.base_daily_kwh * MONTH_WEIGHTS[day.month - 1]

    def minute_profile(self, day):
        """Return a list of 1440 per-minute kWh values for the given date."""
        total = self.daily_kwh(day)
        profile = []
        for slot_value in self.slot_shape:
            per_minute = (total * slot_value) / MINUTES_PER_SLOT
            profile.extend([per_minute] * MINUTES_PER_SLOT)
        return profile


def build_load_forecast(source, start_day, days):
    """Build the cumulative kWh series Predbat reads as ``load_forecast``.

    Keys are absolute minutes from midnight on ``start_day``. Predbat differences
    consecutive entries via ``get_from_incrementing(..., backwards=False)``, so
    the series must be cumulative and must include the final boundary minute
    ``days * 1440`` for the last minute to be readable.

    Days for which the source has no data contribute zero and are skipped by the
    caller, which is responsible for logging the gap.
    """
    forecast = {0: 0.0}
    running = 0.0
    for day_offset in range(days):
        day = start_day + timedelta(days=day_offset)
        profile = source.minute_profile(day)
        if profile is None:
            profile = [0.0] * MINUTES_PER_DAY
        base = day_offset * MINUTES_PER_DAY
        for index, value in enumerate(profile):
            running += value
            forecast[base + index + 1] = running
    return forecast


OCTOPUS_API_BASE = "https://api.octopus.energy/v1"
SLOTS_PER_DAY = 48


def parse_consumption_results(results, log=None):
    """Turn raw Octopus consumption rows into complete per-day half-hourly kWh lists.

    Only days with all 48 slots present, each written exactly once, are returned.
    A partially reported day is omitted entirely rather than returned short,
    because a half-populated day looks like genuinely low consumption and would
    silently understate the bill.

    The autumn clock-change day gets 50 real half-hourly readings (local
    01:00-02:00 happens twice), but the slot index only has 48 possible values,
    so the second pair of readings would silently overwrite the first and leave
    an undercounted-but-"complete" day. Any day where a slot is written more than
    once is therefore discarded too, exactly like a partial day, so the autumn
    transition is handled symmetrically with the spring one (which already comes
    in short at 46/48 and is rejected as partial).
    """
    by_day = {}
    duplicate_slot_days = set()
    for row in results or []:
        start = row.get("interval_start")
        consumption = row.get("consumption")
        if start is None or consumption is None:
            continue
        try:
            stamp = datetime.strptime(start[:16], "%Y-%m-%dT%H:%M")
        except (ValueError, TypeError):
            continue
        slot = stamp.hour * 2 + (1 if stamp.minute >= 30 else 0)
        day = stamp.date()
        if day not in by_day:
            by_day[day] = [None] * SLOTS_PER_DAY
        if by_day[day][slot] is not None:
            duplicate_slot_days.add(day)
            continue
        by_day[day][slot] = float(consumption)

    complete = {}
    for day, slots in by_day.items():
        if day in duplicate_slot_days:
            if log:
                log("Warn: Annual: Octopus discarded {} - a half-hourly slot was reported more than once, likely a clock-change day".format(day))
            continue
        if all(value is not None for value in slots):
            complete[day] = slots
    return complete


class OctopusConsumptionLoadProfile(LoadProfileSource):
    """Load profile taken from the account's real half-hourly Octopus consumption.

    The meter series already includes any EV charging, which is why the config
    layer rejects an Octopus key alongside a separate car charging figure.
    """

    def __init__(self, api_key, account_id, log, storage=None, fallback=None):
        """Set up the Octopus consumption source, optionally backed by a fallback profile."""
        self.api_key = api_key
        self.account_id = account_id
        self.log = log
        self.storage = storage
        self.fallback = fallback
        self.consumption = {}
        self.missing_days = set()
        self.mpan = None
        self.serial = None

    def _auth_header(self):
        """Return the HTTP Basic auth header Octopus expects, API key as username."""
        token = base64.b64encode("{}:".format(self.api_key).encode("utf-8")).decode("utf-8")
        return {"Authorization": "Basic {}".format(token), "accept": "application/json", "user-agent": "predbat/1.0"}

    async def _get_json(self, session, url):
        """Fetch and decode one JSON page, returning None on any failure."""
        return await fetch_json(url, self.log, "Octopus consumption request", self._auth_header(), 30, session=session)

    async def resolve_meter(self, session):
        """Resolve the account's MPAN and meter serial. Returns True on success."""
        data = await self._get_json(session, "{}/accounts/{}/".format(OCTOPUS_API_BASE, self.account_id))
        if not data:
            return False
        for prop in data.get("properties", []) or []:
            for point in prop.get("electricity_meter_points", []) or []:
                if point.get("is_export"):
                    continue
                meters = point.get("meters", []) or []
                if point.get("mpan") and meters:
                    self.mpan = point["mpan"]
                    self.serial = meters[-1].get("serial_number")
                    if self.serial:
                        self.log("Annual: Octopus resolved MPAN {} meter {}".format(self.mpan, self.serial))
                        return True
        self.log("Warn: Annual: Octopus account {} has no usable electricity import meter".format(self.account_id))
        return False

    async def fetch(self, year):
        """Download a calendar year of half-hourly consumption. Returns True on success."""
        cache_key = "consumption_{}_{}".format(self.account_id, year)
        if self.storage:
            cached = await self.storage.load("annual", cache_key)
            if isinstance(cached, dict) and cached:
                self.consumption = {date.fromisoformat(key): value for key, value in cached.items()}
                self.log("Annual: Octopus consumption for {} loaded from cache, {} days".format(year, len(self.consumption)))
                return True

        async with aiohttp.ClientSession() as session:
            if not await self.resolve_meter(session):
                return False

            url = "{}/electricity-meter-points/{}/meters/{}/consumption/?period_from={}-01-01T00:00Z&period_to={}-01-01T00:00Z&page_size=25000&order_by=period".format(OCTOPUS_API_BASE, self.mpan, self.serial, year, year + 1)
            rows = []
            pages = 0
            fetch_failed = False
            while url and pages < 40:
                data = await self._get_json(session, url)
                if not data or "results" not in data:
                    fetch_failed = True
                    break
                rows += data["results"]
                url = data.get("next", None)
                pages += 1

        # A run only reaches its natural end when `url` runs out on its own. If a page request
        # failed, or the safety cap was hit while pages remained, `rows` is a truncated
        # download - it must not be parsed, used, or cached as if it were complete.
        if fetch_failed or url:
            self.log("Warn: Annual: Octopus consumption download for {} did not complete, discarding the partial download rather than caching it".format(year))
            return False

        self.consumption = parse_consumption_results(rows, log=self.log)
        if not self.consumption:
            self.log("Warn: Annual: Octopus returned no complete days of consumption for {}".format(year))
            return False

        self.log("Annual: Octopus consumption for {} downloaded, {} complete days".format(year, len(self.consumption)))
        if self.storage:
            await self.storage.save("annual", cache_key, {day.isoformat(): slots for day, slots in self.consumption.items()}, format="json")
        return True

    def daily_kwh(self, day):
        """Return the total household kWh for the given date, recording a missing day like minute_profile does."""
        slots = self.consumption.get(day)
        if slots is not None:
            return sum(slots)
        self.missing_days.add(day)
        if self.fallback:
            return self.fallback.daily_kwh(day)
        return 0.0

    def minute_profile(self, day):
        """Return 1440 per-minute kWh values, falling back or returning None when the day is missing."""
        slots = self.consumption.get(day)
        if slots is None:
            self.missing_days.add(day)
            if self.fallback:
                return self.fallback.minute_profile(day)
            return None
        profile = []
        for slot_value in slots:
            profile.extend([slot_value / MINUTES_PER_SLOT] * MINUTES_PER_SLOT)
        return profile

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

import calendar
from datetime import timedelta

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

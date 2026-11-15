# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Learning the battery power curves from history.

- battery_charge_power_curve / battery_discharge_power_curve: the SoC taper, from full-rate charges and exports (find_charge_curve).
- battery_temperature_charge_curve: the cold-battery limit, from full-rate charges below BATTERY_TEMPERATURE_CURVE_SOC_MAX (find_battery_temperature_samples
  and the learn/merge/overlay functions below).

BatteryCurve is a mixin of Inverter, so the scanners read the inverter's own settings and history; the module-level functions are pure.
"""

from datetime import datetime, timedelta

from const import (
    BATTERY_POWER_CURVE_CHARGE_TOP,
    BATTERY_POWER_CURVE_DISCHARGE_BOTTOM,
    BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN,
    BATTERY_POWER_CURVE_HALF_LIFE_DAYS,
    BATTERY_POWER_CURVE_MIN_DAYS,
    BATTERY_POWER_CURVE_MIN_MINUTES,
    BATTERY_TEMPERATURE_CURVE_HALF_LIFE_DAYS,
    BATTERY_TEMPERATURE_CURVE_LIMIT_FRACTION,
    BATTERY_TEMPERATURE_CURVE_MAX_AGE_DAYS,
    BATTERY_TEMPERATURE_CURVE_MIN_DAY_MINUTES,
    BATTERY_TEMPERATURE_CURVE_MIN_DAYS,
    BATTERY_TEMPERATURE_CURVE_MIN_MINUTES,
    BATTERY_TEMPERATURE_CURVE_MIN_POWER,
    BATTERY_TEMPERATURE_CURVE_SOC_MAX,
    BATTERY_TEMPERATURE_CURVE_BINDING_MARGIN,
    BATTERY_TEMPERATURE_CURVE_TEMP_FALLBACK,
    BATTERY_TEMPERATURE_CURVE_TEMP_MAX,
    EXPORT_LIMIT_DEFAULT_W,
    MINUTE_WATT,
)
from utils import calc_percent_limit, dp0, dp1, dp2, dp4, find_battery_temperature_cap, is_entity_id, minute_data, minute_data_state

# Largest SoC jump (percentage points) between consecutive readings that the charge curve learner
# accepts as genuine. Wide enough for inverters that report SoC in 5% steps; a larger jump is a
# glitch (e.g. a stray 0% reading) and would otherwise stamp one sample across a huge SoC range.
CHARGE_CURVE_MAX_SOC_STEP = 10


def charge_day(now_utc, minutes_ago, local_tz=None):
    """
    The day a minute of history counts towards for evidence: the local noon-to-noon day, named by the date of the noon
    that starts it, so an overnight charge (e.g. 23:30-05:30) is one day rather than two either side of midnight.

    Args:
        now_utc: the time minutes_ago is measured back from
        minutes_ago: how far back the minute is
        local_tz: the install's time zone (pytz), or None for UTC

    Returns:
        "YYYY-MM-DD"
    """
    when = now_utc - timedelta(minutes=minutes_ago)
    if local_tz is not None:
        when = when.astimezone(local_tz)
    return (when - timedelta(hours=12)).strftime("%Y-%m-%d")


def _temperature_sample_minutes(day_bins):
    """
    Total charging minutes held in one day of battery temperature samples ({temperature: [sum_of_c, minutes]}).
    """
    return sum(entry[1] for entry in day_bins.values())


def clean_battery_temperature_samples(data):
    """
    Turn battery temperature samples read back from storage into {day: {int temperature: [float sum_of_c, int minutes]}}.

    Storage round-trips through JSON, so the temperature keys come back as strings; anything malformed is dropped
    rather than trusted, so a corrupt cache can only lose what was learned, never poison the curve.
    """
    cleaned = {}
    if not isinstance(data, dict):
        return cleaned
    for day, day_bins in data.items():
        try:
            datetime.strptime(str(day), "%Y-%m-%d")
        except ValueError:
            continue
        if not isinstance(day_bins, dict):
            continue
        clean_bins = {}
        for temp, entry in day_bins.items():
            try:
                temp = int(temp)
                rate_sum, minutes = float(entry[0]), int(entry[1])
            except (ValueError, TypeError, IndexError, KeyError):
                continue
            if minutes > 0 and rate_sum >= 0 and temp != 0 and -20 <= temp <= BATTERY_TEMPERATURE_CURVE_TEMP_MAX:
                clean_bins[temp] = [rate_sum, minutes]
        if clean_bins:
            cleaned[str(day)] = clean_bins
    return cleaned


def clean_power_curve_samples(data):
    """
    Turn battery power curve samples read back from storage into {day: {int soc_percent: [float sum, int steps, float minutes]}}.

    As for the temperature samples, anything malformed is dropped rather than trusted.
    """
    cleaned = {}
    if not isinstance(data, dict):
        return cleaned
    for day, day_points in data.items():
        try:
            datetime.strptime(str(day), "%Y-%m-%d")
        except ValueError:
            continue
        if not isinstance(day_points, dict):
            continue
        clean_points = {}
        for point, entry in day_points.items():
            try:
                point = int(point)
                value_sum, steps, minutes = float(entry[0]), int(entry[1]), float(entry[2])
            except (ValueError, TypeError, IndexError, KeyError):
                continue
            if 0 <= point <= 100 and steps > 0 and value_sum >= 0 and minutes >= 0:
                clean_points[point] = [value_sum, steps, minutes]
        if clean_points:
            cleaned[str(day)] = clean_points
    return cleaned


def merge_daily_samples(stored, scanned, today, max_age_days, covered_from_day=None):
    """
    Merge {day: {key: [sum, count, ...]}} samples from a fresh history scan into the stored ones, dropping days over max_age_days old.

    Each scan re-reads the whole history window, so a day is replaced rather than added to - adding would count
    the same minutes again every scan. A day the scan covered completely (after covered_from_day, the day the scanned
    history starts part way through) is replaced by what the scan found, or removed when it found nothing that day:
    the scan is the up-to-date view of it, so a filter that now leaves more out (a newly learned temperature curve,
    say) reaches the stored days too. The partly covered day, and every day when covered_from_day is not known, is only
    replaced when the scan holds at least as much (the count, entry[1]). Days before the scan are kept as stored.

    Args:
        stored: the stored samples
        scanned: one scan's samples
        today: datetime.date the ages are measured from
        max_age_days: days kept
        covered_from_day: "YYYY-MM-DD", the day the scan's history begins part way through, or None

    Returns:
        A new merged dictionary; stored and scanned are not modified.
    """
    merged = {day: {key: list(entry) for key, entry in day_bins.items()} for day, day_bins in stored.items()}
    for day, day_bins in scanned.items():
        fully_covered = covered_from_day is not None and day > covered_from_day
        if fully_covered or day not in merged or _temperature_sample_minutes(day_bins) >= _temperature_sample_minutes(merged[day]):
            merged[day] = {key: list(entry) for key, entry in day_bins.items()}
    if covered_from_day is not None:
        for day in list(merged.keys()):
            if day > covered_from_day and day not in scanned:
                del merged[day]
    for day in list(merged.keys()):
        age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
        if age > max_age_days:
            del merged[day]
    return merged


def merge_battery_temperature_samples(stored, scanned, today, max_age_days=BATTERY_TEMPERATURE_CURVE_MAX_AGE_DAYS, covered_from_day=None):
    """
    Merge the samples from a fresh history scan into the stored samples, and drop days older than max_age_days.

    Each scan re-reads the whole history window, so a day is replaced rather than added to - adding would count
    the same minutes again every scan. The oldest day of a window is only partly covered (the window starts part way
    through it), so a scanned day only replaces a stored one when it holds at least as many charging minutes.

    Args:
        stored: {day: {temperature: [sum_of_c, minutes]}} as kept in storage
        scanned: the same shape, from Inverter.find_battery_temperature_samples()
        today: datetime.date the ages are measured from
        max_age_days: days kept

    Returns:
        A new merged dictionary; stored and scanned are not modified.
    """
    return merge_daily_samples(stored, scanned, today, max_age_days, covered_from_day)


def learn_battery_temperature_curve(samples, today, min_minutes=BATTERY_TEMPERATURE_CURVE_MIN_MINUTES, half_life_days=BATTERY_TEMPERATURE_CURVE_HALF_LIFE_DAYS, min_days=BATTERY_TEMPERATURE_CURVE_MIN_DAYS, rate_scaling=1.0):
    """
    Work out a battery_temperature_charge_curve from stored samples.

    Each whole degree's rate is the mean of its charging minutes, with every day weighted down by its age
    (halving every half_life_days) so recent behaviour wins. A degree is only learned once it has min_minutes of
    charging behind it, from at least min_days different days (each with BATTERY_TEMPERATURE_CURVE_MIN_DAY_MINUTES
    or more), so one charge cannot set the rate for a whole winter - including a night whose recorded charge rate
    did not match what the inverter was really doing.

    The rates are divided by rate_scaling (battery_rate_max_scaling) here rather than when the samples are recorded:
    the plan multiplies the temperature cap by that scaling, so the cap is the rate seen divided by the scaling in use
    now, and a scaling changed later applies to every stored day alike.

    Args:
        samples: {day: {temperature: [sum_of_c, minutes]}}
        today: datetime.date the ages are measured from
        rate_scaling: battery_rate_max_scaling

    Returns:
        ({temperature: rate in C}, {temperature: minutes}) for the learned degrees only
    """
    rate_scaling = rate_scaling if rate_scaling and rate_scaling > 0 else 1.0
    weighted_sum = {}
    weighted_minutes = {}
    minutes = {}
    days = {}
    for day, day_bins in samples.items():
        age = max((today - datetime.strptime(day, "%Y-%m-%d").date()).days, 0)
        weight = 0.5 ** (age / half_life_days) if half_life_days > 0 else 1.0
        for temp, (rate_sum, count) in day_bins.items():
            weighted_sum[temp] = weighted_sum.get(temp, 0.0) + rate_sum * weight
            weighted_minutes[temp] = weighted_minutes.get(temp, 0.0) + count * weight
            minutes[temp] = minutes.get(temp, 0) + count
            if count >= BATTERY_TEMPERATURE_CURVE_MIN_DAY_MINUTES:
                days[temp] = days.get(temp, 0) + 1
    curve = {}
    curve_minutes = {}
    for temp in sorted(minutes):
        if minutes[temp] >= min_minutes and days.get(temp, 0) >= min_days and weighted_minutes[temp] > 0:
            curve[temp] = round(weighted_sum[temp] / weighted_minutes[temp] / rate_scaling, 2)
            curve_minutes[temp] = minutes[temp]
    return curve, curve_minutes


def power_curve_evidence(samples, today, rate_scaling=1.0):
    """
    The stored SoC curve samples as chart points, one per day and SoC percent, on the curve's own scale.

    A point's value is that day's mean step fraction divided by rate_scaling, as learn_charge_curve() does in auto mode
    (and as the plan reads a curve set in apps.yaml). enough is False for an SoC percent learn_charge_curve() drops for
    too little evidence: data from fewer than BATTERY_POWER_CURVE_MIN_DAYS days or under BATTERY_POWER_CURVE_MIN_MINUTES.

    Args:
        samples: {day: {soc_percent: [sum of the step fractions, steps, minutes]}}
        today: datetime.date the ages are measured from
        rate_scaling: battery_rate_max_scaling (or _discharge)

    Returns:
        [{"soc": int, "value": float, "age_days": int, "enough": bool}] sorted by SoC
    """
    rate_scaling = rate_scaling if rate_scaling and rate_scaling > 0 else 1.0
    days, minutes = {}, {}
    for day_points in (samples or {}).values():
        for point, entry in day_points.items():
            days[point] = days.get(point, 0) + 1
            minutes[point] = minutes.get(point, 0) + entry[2]
    points = []
    for day, day_points in (samples or {}).items():
        age = max((today - datetime.strptime(day, "%Y-%m-%d").date()).days, 0)
        for point, (value_sum, steps, _minutes) in day_points.items():
            if steps > 0:
                enough = days[point] >= BATTERY_POWER_CURVE_MIN_DAYS and minutes[point] >= BATTERY_POWER_CURVE_MIN_MINUTES
                points.append({"soc": int(point), "value": round(value_sum / steps / rate_scaling, 3), "age_days": age, "enough": enough})
    return sorted(points, key=lambda item: (item["soc"], item["age_days"]))


def temperature_curve_evidence(samples, today, rate_scaling=1.0):
    """
    The stored temperature curve samples as chart points, one per day and degree, in C on the curve's own scale.

    A point's value is that day's mean rate divided by rate_scaling, as learn_battery_temperature_curve() does. enough is
    False for a degree it does not learn: under BATTERY_TEMPERATURE_CURVE_MIN_MINUTES in all, or fewer than
    BATTERY_TEMPERATURE_CURVE_MIN_DAYS days with BATTERY_TEMPERATURE_CURVE_MIN_DAY_MINUTES or more.

    Args:
        samples: {day: {temperature: [sum_of_c, minutes]}}
        today: datetime.date the ages are measured from
        rate_scaling: battery_rate_max_scaling

    Returns:
        [{"temperature": int, "value": float, "age_days": int, "enough": bool}] sorted by temperature
    """
    rate_scaling = rate_scaling if rate_scaling and rate_scaling > 0 else 1.0
    days, minutes = {}, {}
    for day_bins in (samples or {}).values():
        for temp, (_rate_sum, count) in day_bins.items():
            minutes[temp] = minutes.get(temp, 0) + count
            if count >= BATTERY_TEMPERATURE_CURVE_MIN_DAY_MINUTES:
                days[temp] = days.get(temp, 0) + 1
    points = []
    for day, day_bins in (samples or {}).items():
        age = max((today - datetime.strptime(day, "%Y-%m-%d").date()).days, 0)
        for temp, (rate_sum, count) in day_bins.items():
            if count > 0:
                enough = minutes[temp] >= BATTERY_TEMPERATURE_CURVE_MIN_MINUTES and days.get(temp, 0) >= BATTERY_TEMPERATURE_CURVE_MIN_DAYS
                points.append({"temperature": int(temp), "value": round(rate_sum / count / rate_scaling, 4), "age_days": age, "enough": enough})
    return sorted(points, key=lambda item: (item["temperature"], item["age_days"]))


def overlay_battery_temperature_curve(configured, learned):
    """
    Lay a learned battery temperature charge curve over the configured one.

    The learned curve replaces the configured one over the whole range it covers, from the coldest to the warmest
    learned degree: a degree with no data of its own inside that range is interpolated in a straight line from the
    learned degrees either side, as this battery's own neighbours are better evidence than a curve written for
    batteries in general. Outside that range the configured curve still applies, and below the coldest learned degree
    (down to 1) a degree the configured curve does not set takes the coldest learned rate, so there is no hole that
    falls back to the 20 degree value and plans an uncapped charge in the cold - a colder battery is never expected
    to charge faster. 0 is never filled, because an exact 0 is how several integrations report a missing reading.

    When the warmest learned degree is 20 or above, every warmer degree up to BATTERY_TEMPERATURE_CURVE_TEMP_MAX that
    the configured curve does not set takes its rate too. A warmer battery is not expected to charge slower, so this is
    at worst cautious until warmer charges are seen, and it is how a battery that never reaches its rated charge rate
    at any temperature (a BMS or inverter limit below battery_rate_max) is modelled below 80% SoC. Below 20 nothing is
    filled upwards, as a cold-weather rate says nothing about how fast the battery charges once warm.

    Args:
        configured: battery_temperature_charge_curve from apps.yaml, {int: float}
        learned: {int: float} from learn_battery_temperature_curve()

    Returns:
        The combined curve, a new dictionary.
    """
    curve = dict(configured)
    if not learned:
        return curve
    curve.update(learned)
    temps = sorted(learned)
    coldest = temps[0]
    for temp in range(1, coldest):
        if temp not in configured:
            curve[temp] = learned[coldest]
    for lower, upper in zip(temps, temps[1:]):
        for temp in range(lower + 1, upper):
            if temp != 0:
                fraction = (temp - lower) / (upper - lower)
                curve[temp] = round(learned[lower] + (learned[upper] - learned[lower]) * fraction, 2)
    warmest = temps[-1]
    if warmest >= BATTERY_TEMPERATURE_CURVE_TEMP_FALLBACK:
        for temp in range(warmest + 1, BATTERY_TEMPERATURE_CURVE_TEMP_MAX + 1):
            if temp not in configured:
                curve[temp] = learned[warmest]
    return curve


class BatteryCurve:
    """Mixin for Inverter: scans the inverter's history for the battery power curves."""

    def find_charge_curve(self, discharge):
        """
        Learn the battery charge (or discharge) power curve, the rate against SoC%, from full-rate cycles in the history window.

        find_charge_curve_samples() scans the history and learn_charge_curve() turns what it found into a curve; auto mode
        keeps the scanned samples in storage between scans (Fetch.battery_power_curve_update), so its evidence builds up
        over weeks rather than only the history window.

        Args:
            discharge: True for the discharge curve

        Returns:
            {soc_percent: fraction of the maximum rate}, or {} when no curve could be learned
        """
        samples = self.find_charge_curve_samples(discharge)
        if not samples:
            return {}
        return self.learn_charge_curve(samples, discharge)

    def curve_fleet(self):
        """
        The inverters whose combined history the curve learners read.

        The plan models every inverter as one battery - soc_max and the rated rates summed, the SoC curve and the
        temperature cap applied to the combined SoC% - so with more than one inverter the curves are learned from the
        combined battery the same way, from inverter 0 (the one fetch_inverter_data() runs the learners on). Anything
        else (a single inverter, or one not part of the base's inverter list, as in tests) is learned on its own.
        """
        inverters = getattr(self.base, "inverters", None) or []
        if self.id == 0 and getattr(self.base, "num_inverters", 1) > 1 and len(inverters) == self.base.num_inverters and inverters[0] is self:
            # An inverter with no battery (soc_max 0, e.g. a PV-only one) adds nothing to the combined battery
            fleet = [inverter for inverter in inverters if inverter is self or (inverter.soc_max or 0) > 0]
            if len(fleet) > 1:
                return fleet
        return [self]

    def record_curve_scan_coverage(self, kind, minutes):
        """
        Note the evidence day a scan's history begins part way through, for merge_daily_samples(): every later day was
        covered completely, so the scan's view of it replaces the stored one.

        Args:
            kind: "charge", "discharge" or "temperature"
            minutes: how many minutes back the scan's history reached
        """
        coverage = self.__dict__.setdefault("curve_scan_coverage", {})
        coverage[kind] = charge_day(self.base.now_utc, max(minutes - 1, 0), getattr(self.base, "local_tz", None)) if minutes > 0 else None

    def load_curve_history(self, discharge, purpose, fleet=None):
        """
        Load the SoC, written rate, battery power and Predbat status history the curve learners scan, for the whole fleet.

        Each inverter's own soc (soc_percent, else soc_kw), charge_rate (discharge_rate) and battery_power (with its
        battery_power_invert) are read and combined: the SoC in kWh and the battery power are summed, and a minute counts
        as written at full rate only when every inverter was written at 95% or more of its own maximum - the combined
        rate series is then the fleet's maximum, otherwise 0, so the learners' full-rate test is unchanged. A single
        inverter's series are its own.

        Args:
            discharge: True to read discharge_rate rather than charge_rate
            purpose: what is being learned, for the log ("battery charge curve")
            fleet: the inverters to read, curve_fleet() when not given. When another inverter in it lacks a setting or its
                history, the learners fall back to this inverter alone (as before multi-inverter learning) rather than stop.

        Returns:
            {"soc_kwh", "rate", "battery_power", "status": {minute: value}, "min_len", "soc_max", "max_power" (W),
            "export_limit_w", "inverters"}, or None (after logging which setting or history is missing).
        """
        if fleet is None:
            fleet = self.curve_fleet()
        days = self.base.max_days_previous
        rate_name = "discharge_rate" if discharge else "charge_rate"
        predbat_status_sensor = self.base.prefix + ".status"
        predbat_status_data = self.base.get_history_wrapper(entity_id=predbat_status_sensor, days=days, required=False)

        def to_minutes(entries, scale=1.0, required_unit=None):
            """Minute series (minutes before now) from one entity's history."""
            values, _ = minute_data(
                entries,
                days,
                self.base.now_utc,
                "state",
                "last_updated",
                backwards=True,
                clean_increment=False,
                smoothing=False,
                divide_by=1.0,
                scale=scale,
                required_unit=required_unit,
                can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
            )
            return values

        per_inverter = []
        for inverter in fleet:
            if "soc_percent" in self.base.args:
                soc_sensor = self.base.get_arg("soc_percent", indirect=False, index=inverter.id)
                soc_percent_sensor = True
                soc_label = "soc_percent"
            else:
                soc_sensor = self.base.get_arg("soc_kw", indirect=False, index=inverter.id)
                soc_percent_sensor = False
                soc_label = "soc_kw"
            rate_sensor = self.base.get_arg(rate_name, indirect=False, index=inverter.id)
            battery_power_sensor = self.base.get_arg("battery_power", indirect=False, index=inverter.id)
            battery_power_invert = self.base.get_arg("battery_power_invert", False, index=inverter.id)
            if not (soc_sensor and rate_sensor and battery_power_sensor):
                missing_settings = [setting for setting, entity_id in [(soc_label, soc_sensor), (rate_name, rate_sensor), ("battery_power", battery_power_sensor)] if not entity_id]
                if inverter is not self:
                    self.log("Info: Inverter {} {} not set in apps.yaml, learning the {} from inverter {} alone".format(inverter.id, ", ".join(missing_settings), purpose, self.id))
                    return self.load_curve_history(discharge, purpose, fleet=[self])
                self.log("Warn: Inverter {} Cannot find {} (settings missing), {} not set in apps.yaml".format(inverter.id, purpose, ", ".join(missing_settings)))
                return None
            battery_power_sensor = battery_power_sensor.replace("number.", "sensor.")  # Workaround as old template had number.
            self.log("Inverter {} Looking for {} with sensors {}, {}, {} and {}".format(inverter.id, purpose, soc_sensor, rate_sensor, predbat_status_sensor, battery_power_sensor))
            soc_data = self.base.get_history_wrapper(entity_id=soc_sensor, days=days, required=False)
            rate_data = self.base.get_history_wrapper(entity_id=rate_sensor, days=days, required=False)
            battery_power_data = self.base.get_history_wrapper(entity_id=battery_power_sensor, days=days, required=False)
            history_checks = [(soc_label, soc_sensor, soc_data), (rate_name, rate_sensor, rate_data), ("battery_power", battery_power_sensor, battery_power_data), ("predbat.status", predbat_status_sensor, predbat_status_data)]
            if not all(data for _, _, data in history_checks):
                no_history = ["{} ({})".format(setting, entity_id) for setting, entity_id, data in history_checks if not data]
                if inverter is not self and predbat_status_data:
                    self.log("Info: Inverter {} has no history for {}, learning the {} from inverter {} alone".format(inverter.id, ", ".join(no_history), purpose, self.id))
                    return self.load_curve_history(discharge, purpose, fleet=[self])
                self.log("Warn: Inverter {} Cannot find {} (missing history), no history found for {} - check the entity name in apps.yaml and that Home Assistant is recording its history".format(inverter.id, purpose, ", ".join(no_history)))
                self.log("Warn: Inverter {} Sensor history record counts: {}".format(inverter.id, ", ".join("{} {}".format(setting, len(data[0]) if data else 0) for setting, entity_id, data in history_checks)))
                return None
            if soc_percent_sensor:
                # A percent is already of usable capacity, and soc_max already includes battery_scaling (#6183)
                soc_kwh = {minute: dp4(value * inverter.soc_max / 100.0) for minute, value in to_minutes(soc_data[0], required_unit="%").items()}
            else:
                soc_kwh = to_minutes(soc_data[0], scale=inverter.battery_scaling, required_unit="kWh")
            battery_power = to_minutes(battery_power_data[0], required_unit="W")
            if battery_power_invert:
                battery_power = {minute: -value for minute, value in battery_power.items()}
            max_power = (inverter.battery_rate_max_discharge if discharge else inverter.battery_rate_max_charge) * MINUTE_WATT
            per_inverter.append({"soc_kwh": soc_kwh, "rate": to_minutes(rate_data[0], required_unit="W"), "battery_power": battery_power, "max_power": max_power})

        status = minute_data_state(predbat_status_data[0], days, self.base.now_utc, "state", "last_updated")
        for minute in status:
            if "," in status[minute]:
                # If there are multiple statuses take the first one
                status[minute] = status[minute].split(",")[0].strip()
        min_len = min([len(status)] + [min(len(series["soc_kwh"]), len(series["rate"]), len(series["battery_power"])) for series in per_inverter])
        fleet_max_power = int(sum(series["max_power"] for series in per_inverter))
        if len(per_inverter) == 1:
            series = per_inverter[0]
            soc_kwh, rate, battery_power = series["soc_kwh"], series["rate"], series["battery_power"]
        else:
            soc_kwh, rate, battery_power = {}, {}, {}
            for minute in range(0, min_len):
                if all(minute in series["soc_kwh"] for series in per_inverter):
                    soc_kwh[minute] = dp4(sum(series["soc_kwh"][minute] for series in per_inverter))
                battery_power[minute] = sum(series["battery_power"].get(minute, 0) for series in per_inverter)
                rate[minute] = fleet_max_power if all(series["rate"].get(minute, 0) >= series["max_power"] * 0.95 for series in per_inverter) else 0
        export_limits = [inverter.export_limit * MINUTE_WATT for inverter in fleet]
        export_limit_w = EXPORT_LIMIT_DEFAULT_W if any(limit >= EXPORT_LIMIT_DEFAULT_W for limit in export_limits) else sum(export_limits)
        return {
            "soc_kwh": soc_kwh,
            "rate": rate,
            "battery_power": battery_power,
            "status": status,
            "min_len": min_len,
            "soc_max": sum(inverter.soc_max for inverter in fleet),
            "max_power": fleet_max_power,
            "export_limit_w": export_limit_w,
            "inverters": len(fleet),
        }

    def find_charge_curve_samples(self, discharge):
        """
        Scan the history window for full-rate charge (or discharge) steps through each SoC%.

        Each SoC step is the mean power over the minutes the SoC spent there, while Predbat was Charging (Exporting) with
        the rate written at 95% or more of the maximum, as a fraction of the maximum (after battery_loss). Minutes held at
        the temperature cap, or for discharge at the export limit, are left out. With more than one inverter the combined
        battery is scanned (see load_curve_history()), as the plan applies the curve to it.

        Args:
            discharge: True for the discharge curve

        Returns:
            {day: {soc_percent: [sum of the step fractions, steps, minutes]}} keyed by charge_day(); {} when the history
            holds no full-rate cycle; None when the settings or their history are missing, or the export limit cannot be
            told apart (so stored samples are left as they are).
        """
        curve_type = "discharge" if discharge else "charge"
        curve_label = "empty" if discharge else "full"
        history = self.load_curve_history(discharge, "battery {} curve".format(curve_type))
        if history is None:
            return None
        soc_kwh = history["soc_kwh"]
        charge_rate = history["rate"]
        battery_power = history["battery_power"]
        predbat_status = history["status"]
        min_len = history["min_len"]
        soc_max = history["soc_max"]
        max_power = history["max_power"]
        max_power_scaled = max_power * 0.95
        samples = {}
        self.record_curve_scan_coverage(curve_type, min_len)
        self.log(
            "Inverter {} Looking for {} curve{}, have found {} days of history data, max days {}".format(
                self.id, curve_type, " across {} inverters".format(history["inverters"]) if history["inverters"] > 1 else "", dp1(min_len / 60 / 24.0), self.base.max_days_previous
            )
        )

        soc_percent = {}
        for minute in range(0, min_len):
            soc_percent[minute] = calc_percent_limit(soc_kwh.get(minute, 0), soc_max)

        temperature_limited = self.find_temperature_limited_minutes(discharge, min_len, battery_power, soc_max, max_power)
        if discharge:
            export_limited = self.find_export_limited_minutes(min_len, max_power_scaled, history["export_limit_w"])
            if export_limited is None:
                return None
            temperature_limited = temperature_limited | export_limited

        if discharge:
            search_range = range(5, 20, 1)
        else:
            search_range = range(99, 85, -1)

        # Find 100% end points. The exact-match checks below ("Charging" / "Exporting",
        # "Discharging") deliberately exclude "Cross-charging" minutes - during genuine
        # cross-charging another inverter is simultaneously drawing/feeding power at the
        # same time, so this inverter's battery_power reading isn't a clean single-inverter
        # charge/discharge sample and would corrupt the learned curve if included.
        for data_point in search_range:
            for minute in range(1, min_len):
                # Start trigger is when the SoC just increased above the data point
                previous_point = soc_percent.get(minute - 1, 0)
                if (
                    not discharge
                    and previous_point > data_point
                    and soc_percent.get(minute, 0) == data_point
                    and predbat_status.get(minute - 1, "") == "Charging"
                    and predbat_status.get(minute, "") == "Charging"
                    and predbat_status.get(minute + 1, "") == "Charging"
                    and charge_rate.get(minute - 1, 0) >= max_power_scaled
                    and charge_rate.get(minute, 0) >= max_power_scaled
                    and battery_power.get(minute, 0) < 0
                ) or (
                    discharge
                    and previous_point < data_point
                    and soc_percent.get(minute, 0) == data_point
                    and predbat_status.get(minute - 1, "") in ["Exporting", "Discharging"]
                    and predbat_status.get(minute, "") in ["Exporting", "Discharging"]
                    and predbat_status.get(minute + 1, "") in ["Exporting", "Discharging"]
                    and charge_rate.get(minute - 1, 0) >= max_power_scaled
                    and charge_rate.get(minute, 0) >= max_power_scaled
                    and battery_power.get(minute, 0) > 0
                ):
                    total_power = 0
                    total_count = 0
                    # Find a period where charging was at full rate and the SoC just drops below the data point
                    for target_minute in range(minute, min_len):
                        this_soc = soc_percent.get(target_minute, 0)
                        if not discharge and (predbat_status.get(target_minute, "") != "Charging" or charge_rate.get(target_minute, 0) < max_power_scaled or battery_power.get(target_minute, 0) >= 0):
                            break
                        if discharge and (predbat_status.get(target_minute, "") not in ["Exporting", "Discharging"] or charge_rate.get(target_minute, 0) < max_power_scaled or battery_power.get(target_minute, 0) <= 0):
                            break
                        if target_minute in temperature_limited:
                            # A cold battery (or, discharging, the export limit) was holding the rate down, which is not the SoC curve
                            break

                        if (discharge and (this_soc > data_point)) or (not discharge and (this_soc < data_point)):
                            if total_power == 0:
                                # No power data, so skip this data point
                                break
                            if target_minute not in soc_kwh or abs(previous_point - data_point) > CHARGE_CURVE_MAX_SOC_STEP or abs(data_point - this_soc) > CHARGE_CURVE_MAX_SOC_STEP:
                                # Missing or implausible SoC reading at either end of the step, skip this sample (#5259)
                                break
                            if discharge:
                                this_soc -= 1
                            else:
                                this_soc += 1
                            # So the power for this data point average has been stored, it's possible we spanned more than one data point
                            # if not all SoC %'s are represented for this battery size
                            from_soc = soc_kwh.get(minute, 0)
                            to_soc = soc_kwh.get(target_minute, 0)
                            soc_charged = from_soc - to_soc
                            average_power = total_power / total_count
                            charge_curve = round(min(average_power / max_power / self.base.battery_loss, 1.0), 2)
                            if self.base.debug_enable:
                                self.log(
                                    "Curve Percent: {}-{} at {} took {} minutes charged {} curve {} average_power {}".format(
                                        data_point,
                                        this_soc,
                                        self.base.time_abs_str(self.base.minutes_now - minute),
                                        total_count,
                                        round(soc_charged, 2),
                                        charge_curve,
                                        average_power,
                                    )
                                )
                            # Store data points
                            if discharge:
                                store_range = range(this_soc, previous_point, -1)
                            else:
                                store_range = range(this_soc, previous_point)

                            # Evidence behind each point: the day it was seen on, and its share of the step's minutes
                            step_day = charge_day(self.base.now_utc, minute, getattr(self.base, "local_tz", None))
                            step_minutes = total_count / max(len(store_range), 1)
                            day_points = samples.setdefault(step_day, {})
                            for point in store_range:
                                entry = day_points.setdefault(point, [0.0, 0, 0.0])
                                entry[0] += charge_curve
                                entry[1] += 1
                                entry[2] += step_minutes

                            break
                        else:
                            # Store data for this minute of the period, so total_power/total_count
                            # below is the mean power across the whole SoC step rather than the
                            # reading at the single minute the step was triggered on
                            total_power += abs(battery_power.get(target_minute, 0))
                            total_count += 1
        if samples:
            for day_points in samples.values():
                for entry in day_points.values():
                    entry[0] = dp4(entry[0])
                    entry[2] = dp2(entry[2])
            return samples
        self.log(
            "Info: Cannot find battery {} curve (no full rate {} cycle to {} found in history), battery may not have been fully charged/discharged at max rate recently - this is normal in cost-optimised operation".format(
                curve_type, curve_type, curve_label
            )
        )
        return {}

    def learn_charge_curve(self, samples, discharge):
        """
        Turn charge (or discharge) curve samples - from one scan, or built up in storage - into a curve.

        Each SoC point is the mean of its steps, each day weighted down by its age (halving every
        BATTERY_POWER_CURVE_HALF_LIFE_DAYS). A point needs data from BATTERY_POWER_CURVE_MIN_DAYS days and
        BATTERY_POWER_CURVE_MIN_MINUTES minutes, and the curve must reach BATTERY_POWER_CURVE_CHARGE_TOP% (charge) or
        BATTERY_POWER_CURVE_DISCHARGE_BOTTOM% (discharge); otherwise nothing is returned.

        In auto mode the curve is returned as learned, relative to the rated maximum and the battery_rate_max_scaling the
        plan applies; otherwise it is rescaled to a maximum of 1.0 for apps.yaml, with the scaling logged to set alongside.

        Args:
            samples: {day: {soc_percent: [sum of the step fractions, steps, minutes]}}
            discharge: True for the discharge curve

        Returns:
            {soc_percent: fraction of the maximum rate}, or {} when no curve could be learned
        """
        curve_type = "discharge" if discharge else "charge"
        today = datetime.strptime(charge_day(self.base.now_utc, 0, getattr(self.base, "local_tz", None)), "%Y-%m-%d").date()
        weighted_sum = {}
        weighted_count = {}
        final_curve_days = {}
        final_curve_minutes = {}
        for day, day_points in samples.items():
            age = max((today - datetime.strptime(day, "%Y-%m-%d").date()).days, 0)
            weight = 0.5 ** (age / BATTERY_POWER_CURVE_HALF_LIFE_DAYS)
            for point, (value_sum, count, minutes) in day_points.items():
                weighted_sum[point] = weighted_sum.get(point, 0.0) + value_sum * weight
                weighted_count[point] = weighted_count.get(point, 0.0) + count * weight
                final_curve_days[point] = final_curve_days.get(point, 0) + 1
                final_curve_minutes[point] = final_curve_minutes.get(point, 0) + minutes
        final_curve = {point: dp2(weighted_sum[point] / weighted_count[point]) for point in weighted_sum if weighted_count[point] > 0}
        if final_curve:
            self.log("Inverter {} {} curve before adjustment is: {} ({} days of samples)".format(self.id, curve_type, final_curve, len(samples)))

            # Minimum evidence: a point seen on one day only, or for a few minutes, is one charge's (or export's)
            # noise, and the curve is kept and reused once learned, so such points are dropped. The curve must then
            # still reach the end it describes, the steepest part, rather than extrapolate up (or down) to it.
            weak = sorted(point for point in final_curve if final_curve_days.get(point, 0) < BATTERY_POWER_CURVE_MIN_DAYS or final_curve_minutes.get(point, 0) < BATTERY_POWER_CURVE_MIN_MINUTES)
            for point in weak:
                del final_curve[point]
            if weak:
                self.log("Inverter {} {} curve: dropped SoC points {} with data from fewer than {} days or under {} minutes".format(self.id, curve_type, weak, BATTERY_POWER_CURVE_MIN_DAYS, BATTERY_POWER_CURVE_MIN_MINUTES))
            reaches_end = bool(final_curve) and (min(final_curve) <= BATTERY_POWER_CURVE_DISCHARGE_BOTTOM if discharge else max(final_curve) >= BATTERY_POWER_CURVE_CHARGE_TOP)
            if not reaches_end:
                self.log(
                    "Info: Inverter {} Not enough evidence for the battery {} curve yet: it needs full-rate data on {} or more days {} {}% SoC - this is normal in cost-optimised operation".format(
                        self.id, curve_type, BATTERY_POWER_CURVE_MIN_DAYS, "down to" if discharge else "up to", BATTERY_POWER_CURVE_DISCHARGE_BOTTOM if discharge else BATTERY_POWER_CURVE_CHARGE_TOP
                    )
                )
                return {}

            # Find info for gap filling
            auto = self.base.battery_discharge_power_curve_auto if discharge else self.base.battery_charge_power_curve_auto
            found_required = False
            if discharge:
                # Down to 0, not 4: below the lowest learned point the plan would otherwise read an unset SoC as 1.0, so
                # a battery tapering towards empty was planned at full rate again for its last few percent
                fill_range = range(0, 21, 1)
                value = min(final_curve.values())
            else:
                value = max(final_curve.values())
                fill_range = range(85, 101)

            # Pick the first value point for fill below
            for point in fill_range:
                if point in final_curve:
                    value = final_curve[point]
                    break
            if auto and not discharge and self.base.battery_temperature_charge_curve:
                # With a temperature curve in use, auto mode claims a limit only where one was seen: below the first
                # learned point the charge was either not seen at full rate or held at the temperature cap, which that
                # curve models. Without one nothing would model the level there, so the first point's value carries down
                value = 1.0

            # Fill gaps
            for point in fill_range:
                if point not in final_curve:
                    final_curve[point] = value
                    found_required = True
                else:
                    value = final_curve[point]

            if found_required:
                # Work out the maximum power to use as a scale factor
                rate_scaling = max(final_curve.values()) if final_curve else 0

                if rate_scaling > 0 and auto:
                    # Auto mode uses the curve as learned, relative to the rated maximum (and the scaling the plan already
                    # applies), not rescaled to its own maximum: a battery that only ever reaches 65% of its rated rate is
                    # then planned at 65% rather than told it charges at full rate until the taper (#6189)
                    user_scaling = self.base.battery_rate_max_scaling_discharge if discharge else self.base.battery_rate_max_scaling
                    user_scaling = user_scaling if user_scaling > 0 else 1.0
                    for index in final_curve:
                        final_curve[index] = round(min(final_curve[index] / user_scaling, 1.0), 2)
                elif rate_scaling > 0:
                    # Scale curve to 1.0, for entering in apps.yaml together with the battery_rate_max_scaling logged below
                    for index in final_curve:
                        final_curve[index] = round(final_curve[index] / rate_scaling, 2)

                # If we have the correct data then output it
                if rate_scaling > 0:
                    if discharge:
                        text = "  battery_discharge_power_curve:\n"
                    else:
                        text = "  battery_charge_power_curve:\n"
                    keys = sorted(final_curve.keys())
                    keys.reverse()
                    first = True
                    for key in keys:
                        if (final_curve[key] < 1.0 or first) and final_curve[key] > 0.0:
                            text += "    {} : {}\n".format(key, final_curve[key])
                            first = False
                    if auto:
                        self.log("Info: Inverter {} curve automatically computed as:\n".format(self.id) + text)
                        return final_curve
                    self.log("Info: Inverter {} {} curve can be entered into apps.yaml or set to auto:\n".format(self.id, curve_type) + text)
                    rate_scaling = round(rate_scaling, 2)
                    if discharge:
                        if rate_scaling != self.base.battery_rate_max_scaling_discharge:
                            self.log("Info: Inverter {} Consider setting in HA: input_number.battery_rate_max_scaling_discharge: {} - currently {}".format(self.id, rate_scaling, self.base.battery_rate_max_scaling_discharge))
                    else:
                        if rate_scaling != self.base.battery_rate_max_scaling:
                            self.log("Info: Inverter {} Consider setting in HA: input_number.battery_rate_max_scaling: {} - currently {}".format(self.id, rate_scaling, self.base.battery_rate_max_scaling))
                    return final_curve
                else:
                    self.log("Info: Inverter {} Found incorrect battery {} curve (was 0), maybe try again when you have more data.".format(self.id, curve_type))
            else:
                self.log("Warn: Inverter {} Found incomplete battery {} curve (no data points), maybe try again when you have more data.".format(self.id, curve_type))
        return {}

    def find_temperature_limited_minutes(self, discharge, min_len, battery_power, soc_max, max_power):
        """
        Find the minutes of history where the battery was held at its temperature cap.

        find_charge_curve() learns the high-SoC taper from full-rate charges (or the low-SoC tail-off from full-rate
        discharges), and a cold battery held back by its BMS looks exactly like a taper, so those minutes are left out.
        A minute is limited when find_battery_temperature_cap() at its temperature, with the configured (or learned)
        battery_temperature_charge_curve / battery_temperature_discharge_curve, is below
        BATTERY_TEMPERATURE_CURVE_LIMIT_FRACTION of the rated rate AND the battery was delivering at that cap (within
        BATTERY_TEMPERATURE_CURVE_BINDING_MARGIN). A cap just under the rated rate - a learned warm rate a few percent
        short of it from losses or noise - is not a limit, or every full-rate minute would be left out. A minute delivering
        clearly less than the cap was held back by something else - the SoC taper this is learning - so it is kept,
        which matters once warm degrees are learned: a battery that never reaches its rated rate has a cap below full
        rate at every temperature, and testing the cap alone would leave nothing to learn the taper from.
        The cap is compared in the units battery_power is recorded in (multiplied by battery_loss and the
        battery_rate_max_scaling the plan applies to it). Readings of exactly 0 are treated as missing, as several
        integrations report a missing temperature as 0.

        Args:
            discharge: True to use the discharge temperature curve
            min_len: minutes of history being searched
            battery_power: {minute: W} as load_curve_history() read it (sign already corrected, summed over the fleet)
            soc_max: the (combined) battery size the cap is worked out for, kWh
            max_power: the (combined) rated rate, W

        Returns:
            A set of minute indexes (minutes before now); empty when there is no temperature curve or history.
        """
        temperature_curve = self.base.battery_temperature_discharge_curve if discharge else self.base.battery_temperature_charge_curve
        temperature_sensor = self.base.get_arg("battery_temperature_history", indirect=False)
        if not temperature_curve or not temperature_sensor or soc_max <= 0:
            return set()
        temperature_data = self.base.get_history_wrapper(entity_id=temperature_sensor, days=self.base.max_days_previous, required=False)
        if not temperature_data:
            return set()
        temperature, _ = minute_data(
            temperature_data[0],
            self.base.max_days_previous,
            self.base.now_utc,
            "state",
            "last_updated",
            backwards=True,
            clean_increment=False,
            smoothing=False,
            divide_by=1.0,
            scale=1.0,
            can_modify_history=True,  # history is not accessed after this point
        )
        battery_loss = self.base.battery_loss if self.base.battery_loss > 0 else 1.0
        rate_scaling = self.base.battery_rate_max_scaling_discharge if discharge else self.base.battery_rate_max_scaling
        rate_scaling = rate_scaling if rate_scaling > 0 else 1.0
        limit_w = max_power * BATTERY_TEMPERATURE_CURVE_LIMIT_FRACTION
        limited = set()
        cap_cache = {}
        for minute in range(0, min_len):
            temp = temperature.get(minute, None)
            if temp is None or temp == 0:
                continue
            temp_idx = int(min(max(temp, -20), BATTERY_TEMPERATURE_CURVE_TEMP_MAX))
            if temp_idx not in cap_cache:
                cap_cache[temp_idx] = find_battery_temperature_cap(temp_idx, temperature_curve, soc_max, float("inf")) * MINUTE_WATT * battery_loss * rate_scaling
            cap_w = cap_cache[temp_idx]
            if cap_w < limit_w and abs(battery_power.get(minute, 0)) >= cap_w * BATTERY_TEMPERATURE_CURVE_BINDING_MARGIN:
                limited.add(minute)
        if limited:
            self.log("Inverter {} Leaving {} temperature-limited minutes out of the battery {} curve".format(self.id, len(limited), "discharge" if discharge else "charge"))
        return limited

    def find_export_limited_minutes(self, min_len, max_power_scaled, export_limit_w=None):
        """
        Find the minutes of history where the export limit, not the battery, held back a discharge.

        While exporting, the battery supplies the house load plus the grid export, so once the export reaches
        export_limit the inverter turns the battery down and the discharge rate drops for a reason that has nothing to
        do with the SoC. A minute counts as limited when grid_power (positive for export) was at or above
        BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN of export_limit.

        Args:
            min_len: minutes of history being searched
            max_power_scaled: the rate in W a minute must reach to count as full rate
            export_limit_w: the (combined) export limit in W; this inverter's own when not given

        Returns:
            A set of minute indexes (minutes before now): empty when no export limit is set, or when there is no
            grid_power history and the limit is above the full rate (so it cannot be what held the battery back).
            None when the limit is below the full rate and there is no grid_power history to tell the minutes apart,
            as the discharge curve cannot then be learned safely.
        """
        if export_limit_w is None:
            export_limit_w = self.export_limit * MINUTE_WATT
        if export_limit_w >= EXPORT_LIMIT_DEFAULT_W:
            return set()
        grid_sensor = self.base.get_arg("grid_power", indirect=False, index=self.id)
        grid_data = None
        if grid_sensor and is_entity_id(grid_sensor):
            grid_data = self.base.get_history_wrapper(entity_id=grid_sensor, days=self.base.max_days_previous, required=False)
        if not grid_data:
            if export_limit_w < max_power_scaled:
                self.log("Info: Inverter {} Cannot find battery discharge curve, export_limit {}W is below the discharge rate and there is no grid_power history to tell which exports it limited".format(self.id, dp0(export_limit_w)))
                return None
            return set()
        grid_power, _ = minute_data(
            grid_data[0],
            self.base.max_days_previous,
            self.base.now_utc,
            "state",
            "last_updated",
            backwards=True,
            clean_increment=False,
            smoothing=False,
            divide_by=1.0,
            scale=1.0,
            required_unit="W",
            can_modify_history=True,  # history is not accessed after this point
        )
        invert = self.base.get_arg("grid_power_invert", False, index=self.id)
        threshold = export_limit_w * BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN
        limited = set()
        for minute in range(0, min_len):
            power = grid_power.get(minute, None)
            if power is None:
                continue
            if invert:
                power = -power
            if power >= threshold:
                limited.add(minute)
        if limited:
            self.log("Inverter {} Leaving {} export-limited minutes out of the battery discharge curve".format(self.id, len(limited)))
        return limited

    def find_battery_temperature_samples(self):
        """
        Scan the recent history for minutes that show how fast the battery accepts charge at each temperature.

        A minute counts when Predbat was Charging, the charge rate was written at (nearly) its maximum, the battery
        was taking charge and the SoC was below BATTERY_TEMPERATURE_CURVE_SOC_MAX, so the high-SoC taper (which
        battery_charge_power_curve models) cannot be mistaken for a cold battery. The rate the battery actually
        took is recorded in C (kW per kWh of soc_max), the unit battery_temperature_charge_curve uses, as seen:
        battery_rate_max_scaling is divided out when the curve is learned (learn_battery_temperature_curve()), so a
        scaling changed later applies to the stored days too.
        Degrees up to BATTERY_TEMPERATURE_CURVE_TEMP_MAX are learned, a warmer reading counting as that degree, as the
        cap looks it up. With more than one inverter the combined battery is scanned (load_curve_history()), as the plan
        applies the cap to it.

        Returns:
            {day: {temperature: [sum_of_c, minutes]}} keyed by charge_day() (YYYY-MM-DD) and the whole degree
            the temperature truncates to (as find_battery_temperature_cap() looks it up), or None when the
            sensors or their history are missing.
        """
        temperature_sensor = self.base.get_arg("battery_temperature_history", indirect=False)
        if not temperature_sensor or self.soc_max <= 0:
            self.log("Warn: Inverter {} Cannot learn the battery temperature charge curve, battery_temperature_history must be set in apps.yaml".format(self.id))
            return None
        history = self.load_curve_history(False, "battery temperature charge curve")
        if history is None:
            return None
        days = self.base.max_days_previous
        temperature_data = self.base.get_history_wrapper(entity_id=temperature_sensor, days=days, required=False)
        if not temperature_data:
            self.log("Warn: Inverter {} Cannot learn the battery temperature charge curve, no history found for temperature ({})".format(self.id, temperature_sensor))
            return None
        temperature, _ = minute_data(
            temperature_data[0],
            days,
            self.base.now_utc,
            "state",
            "last_updated",
            backwards=True,
            clean_increment=False,
            smoothing=False,
            divide_by=1.0,
            scale=1.0,
            can_modify_history=True,  # history is not accessed after this point
        )
        soc_kwh = history["soc_kwh"]
        charge_rate = history["rate"]
        battery_power = history["battery_power"]
        status = history["status"]
        soc_max = history["soc_max"]
        self.record_curve_scan_coverage("temperature", min(history["min_len"], len(temperature)))

        max_power = history["max_power"]
        min_charge_rate = max_power * 0.95
        min_battery_power = max(max_power * BATTERY_TEMPERATURE_CURVE_MIN_POWER, 1.0)
        battery_loss = self.base.battery_loss if self.base.battery_loss > 0 else 1.0
        local_tz = getattr(self.base, "local_tz", None)
        samples = {}
        for minute in range(0, min(history["min_len"], len(temperature))):
            if status.get(minute, "") != "Charging":
                continue
            if charge_rate.get(minute, 0) < min_charge_rate:
                continue
            power = battery_power.get(minute, 0)
            if power >= 0 or abs(power) < min_battery_power:
                # Not charging, or so little that the charge has finished rather than been limited
                continue
            soc_value = soc_kwh.get(minute, None)
            if soc_value is None:
                continue
            if soc_value / soc_max * 100.0 >= BATTERY_TEMPERATURE_CURVE_SOC_MAX:
                continue
            temp = temperature.get(minute, None)
            if temp is None:
                continue
            temp = int(min(max(temp, -20), BATTERY_TEMPERATURE_CURVE_TEMP_MAX))
            if temp == 0:
                # The 0 entry is the fallback for every reading at or below 0, including the exact 0 several integrations
                # report for a missing reading, so it is never learned (anything between -1 and 1 truncates to it)
                continue
            rate_c = abs(power) / 1000.0 / battery_loss / soc_max
            day = charge_day(self.base.now_utc, minute, local_tz)
            day_bins = samples.setdefault(day, {})
            entry = day_bins.setdefault(temp, [0.0, 0])
            entry[0] += rate_c
            entry[1] += 1

        for day_bins in samples.values():
            for entry in day_bins.values():
                entry[0] = dp4(entry[0])
        self.log("Inverter {} Battery temperature curve: found {} charging minutes below {}% SoC in {} days of history".format(self.id, sum(entry[1] for day_bins in samples.values() for entry in day_bins.values()), BATTERY_TEMPERATURE_CURVE_SOC_MAX, days))
        return samples

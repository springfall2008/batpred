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
Scanning the battery history for the rate grid evidence (battery_rate_grid.py): full-rate charges and forced exports,
cut into 5-minute segments of SoC, battery temperature and rate (find_rate_grid_samples).

BatteryCurve is a mixin of Inverter, so the scanners read the inverter's own settings and history; the module-level functions are pure.
"""

from datetime import timedelta

from const import (
    BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN,
    BATTERY_RATE_GRID_EDGE_MINUTES,
    BATTERY_RATE_GRID_FULL_RATE,
    BATTERY_RATE_GRID_MIN_POWER,
    BATTERY_RATE_GRID_RUN_MINUTES,
    BATTERY_RATE_GRID_SEGMENT_MINUTES,
    EXPORT_LIMIT_DEFAULT_W,
    MINUTE_WATT,
)
from utils import average_temperature_minutes, battery_temperature_entities, dp0, dp2, dp4, is_entity_id, minute_data, minute_data_state


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


class BatteryCurve:
    """Mixin for Inverter: scans the inverter's history for the battery power curves."""

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
            # Every inverter, as fetch_inverter_data() sums every one into the battery the plan models - one whose soc_max
            # reads 0 included, as leaving it out would measure a site-wide battery_power against the rest alone (#6226)
            return list(inverters)
        return [self]

    def record_curve_scan_coverage(self, kind, minutes):
        """
        Note the evidence day a scan's history begins part way through, for merge_daily_samples(): every later day was
        covered completely, so the scan's view of it replaces the stored one.

        Args:
            kind: "charge", "discharge", "temperature" or "temperature_discharge"
            minutes: how many minutes back the scan's history reached
        """
        coverage = self.__dict__.setdefault("curve_scan_coverage", {})
        coverage[kind] = charge_day(self.base.now_utc, max(minutes - 1, 0), getattr(self.base, "local_tz", None)) if minutes > 0 else None

    def load_curve_history(self, discharge, purpose, fleet=None, days=None):
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
            days: how many days of history to read, max_days_previous when not given

        "max_power" is the full rate the history was written at: battery_rate_max_charge for charge (inverter_limit_charge
        applied), and for discharge battery_rate_max_export, as the discharge evidence is forced exports and those run at
        inverter_limit_export rather than the discharge rating. "rated_power" is the rating the plan scales the grid by
        (battery_rate_max_charge, battery_rate_max_discharge).

        Returns:
            {"soc_kwh", "rate", "battery_power", "status": {minute: value}, "min_len", "soc_max", "max_power" (W),
            "rated_power" (W), "export_limit_w", "inverters"}, or None (after logging which setting or history is missing).
        """
        if fleet is None:
            fleet = self.curve_fleet()
        days = days or self.base.max_days_previous
        # A window longer than days_previous is a one-off read: kept out of the history cache, which would otherwise hold
        # (and refresh every cycle) that much history of each sensor from then on
        tracked = days <= self.base.max_days_previous
        rate_name = "discharge_rate" if discharge else "charge_rate"
        predbat_status_sensor = self.base.prefix + ".status"
        predbat_status_data = self.base.get_history_wrapper(entity_id=predbat_status_sensor, days=days, required=False, tracked=tracked)

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
                pad=False,  # only the history that exists: padding the window before it would count as evidence and as coverage
            )
            return values

        def counted_in_site_total(inverter):
            """
            True when this inverter's battery_power is a 0 or blank rather than a sensor, on any inverter but the first: the
            documented way to say the first one's sensor is the site total (an EMS reporting for the whole site, #6226).
            None means no entry at all, a setting that is missing.
            """
            value = self.base.get_arg("battery_power", indirect=False, index=inverter.id)
            return inverter is not self and value is not None and not is_entity_id(value)

        # With a site-total battery_power the first inverter's power is the whole fleet's, so it can only be learned against
        # the whole fleet's rate: falling back to the first inverter alone would divide the site's power by one inverter's
        # maximum, so a missing setting or history on another inverter stops the learning instead
        site_total_fleet = any(counted_in_site_total(inverter) for inverter in fleet)

        def fall_back(inverter, reason):
            """Learn from this inverter alone when another lacks a setting or history, unless the power is a site total."""
            if site_total_fleet:
                self.log("Warn: Inverter {} {}, and inverter {}'s battery_power is the site total so the {} cannot be learned from it alone - not learning it".format(inverter.id, reason, self.id, purpose))
                return None
            self.log("Info: Inverter {} {}, learning the {} from inverter {} alone".format(inverter.id, reason, purpose, self.id))
            return self.load_curve_history(discharge, purpose, fleet=[self], days=days)

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
            # This inverter's power is already counted in the first one's site-total sensor, so it is not a missing setting
            site_total = counted_in_site_total(inverter)
            if not (soc_sensor and rate_sensor and (battery_power_sensor or site_total)):
                missing_settings = [setting for setting, entity_id in [(soc_label, soc_sensor), (rate_name, rate_sensor), ("battery_power", battery_power_sensor or site_total)] if not entity_id]
                if inverter is not self:
                    return fall_back(inverter, "{} not set in apps.yaml".format(", ".join(missing_settings)))
                self.log("Warn: Inverter {} Cannot find {} (settings missing), {} not set in apps.yaml".format(inverter.id, purpose, ", ".join(missing_settings)))
                return None
            if site_total:
                battery_power_sensor = "0 (counted in inverter {}'s sensor)".format(self.id)
            else:
                battery_power_sensor = battery_power_sensor.replace("number.", "sensor.")  # Workaround as old template had number.
            self.log("Inverter {} Looking for {} with sensors {}, {}, {} and {}".format(inverter.id, purpose, soc_sensor, rate_sensor, predbat_status_sensor, battery_power_sensor))
            soc_data = self.base.get_history_wrapper(entity_id=soc_sensor, days=days, required=False, tracked=tracked)
            rate_data = self.base.get_history_wrapper(entity_id=rate_sensor, days=days, required=False, tracked=tracked)
            history_checks = [(soc_label, soc_sensor, soc_data), (rate_name, rate_sensor, rate_data), ("predbat.status", predbat_status_sensor, predbat_status_data)]
            if not site_total:
                battery_power_data = self.base.get_history_wrapper(entity_id=battery_power_sensor, days=days, required=False, tracked=tracked)
                history_checks.insert(2, ("battery_power", battery_power_sensor, battery_power_data))
            if not all(data for _, _, data in history_checks):
                no_history = ["{} ({})".format(setting, entity_id) for setting, entity_id, data in history_checks if not data]
                if inverter is not self and predbat_status_data:
                    return fall_back(inverter, "has no history for {}".format(", ".join(no_history)))
                self.log("Warn: Inverter {} Cannot find {} (missing history), no history found for {} - check the entity name in apps.yaml and that Home Assistant is recording its history".format(inverter.id, purpose, ", ".join(no_history)))
                self.log("Warn: Inverter {} Sensor history record counts: {}".format(inverter.id, ", ".join("{} {}".format(setting, len(data[0]) if data else 0) for setting, entity_id, data in history_checks)))
                return None
            if soc_percent_sensor:
                # A percent is already of usable capacity, and soc_max already includes battery_scaling (#6183)
                soc_kwh = {minute: dp4(value * inverter.soc_max / 100.0) for minute, value in to_minutes(soc_data[0], required_unit="%").items()}
            else:
                soc_kwh = to_minutes(soc_data[0], scale=inverter.battery_scaling, required_unit="kWh")
            # A site-total inverter adds no power of its own (the first inverter's sensor holds it) but its SoC, written rate
            # and rated maximum still count, so the site's power is measured against the whole fleet's rate
            battery_power = {} if site_total else to_minutes(battery_power_data[0], required_unit="W")
            if battery_power_invert:
                battery_power = {minute: -value for minute, value in battery_power.items()}
            if discharge:
                max_power = (inverter.battery_rate_max_export or 0) * MINUTE_WATT
                rated_power = inverter.battery_rate_max_discharge * MINUTE_WATT
            else:
                max_power = rated_power = inverter.battery_rate_max_charge * MINUTE_WATT
            per_inverter.append({"soc_kwh": soc_kwh, "rate": to_minutes(rate_data[0], required_unit="W"), "battery_power": battery_power, "max_power": max_power, "rated_power": rated_power, "site_total": site_total})

        # Not padded (nor the series above): a window longer than the history (28 days asked, Home Assistant keeping 10) must
        # stop where the history does, or the padding would count as evidence and as coverage, and the per-day merge would
        # remove stored days the scan never saw. min_len below is then the shortest real history.
        status = minute_data_state(predbat_status_data[0], days, self.base.now_utc, "state", "last_updated", pad=False)
        for minute in status:
            if "," in status[minute]:
                # If there are multiple statuses take the first one
                status[minute] = status[minute].split(",")[0].strip()
        min_len = min([len(status)] + [min(len(series["soc_kwh"]), len(series["rate"]), *([] if series["site_total"] else [len(series["battery_power"])])) for series in per_inverter])
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
            "rated_power": int(sum(series["rated_power"] for series in per_inverter)),
            "export_limit_w": export_limit_w,
            "inverters": len(fleet),
        }

    def find_export_limited_minutes(self, min_len, max_power_scaled, export_limit_w=None, days=None):
        """
        Find the minutes of history where the export limit, not the battery, held back a discharge.

        While exporting, the battery supplies the house load plus the grid export, so once the export reaches
        export_limit the inverter turns the battery down and the discharge rate drops for a reason that has nothing to
        do with the SoC. A minute counts as limited when grid_power (positive for export) was at or above
        BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN of export_limit. Such a minute is still evidence: it shows the battery was
        not held back by its SoC or temperature, so find_rate_grid_samples() records it as no battery limit seen.

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
        grid_power = self.load_grid_power_minutes(days)
        if grid_power is None:
            if export_limit_w < max_power_scaled:
                self.log("Info: Inverter {} Cannot find battery discharge rate grid evidence, export_limit {}W is below the discharge rate and there is no grid_power history to tell which exports it limited".format(self.id, dp0(export_limit_w)))
                return None
            return set()
        threshold = export_limit_w * BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN
        limited = set()
        for minute in range(0, min_len):
            power = grid_power.get(minute, None)
            if power is None:
                continue
            if power >= threshold:
                limited.add(minute)
        if limited:
            self.log("Inverter {} Recording {} export-limited minutes as no battery limit in the battery discharge curve".format(self.id, len(limited)))
        return limited

    def load_grid_power_minutes(self, days=None):
        """
        This inverter's grid_power history by minute before now, in W with export positive (grid_power_invert applied).

        Returns None when grid_power is not set or has no history.
        """
        grid_sensor = self.base.get_arg("grid_power", indirect=False, index=self.id)
        grid_data = None
        if grid_sensor and is_entity_id(grid_sensor):
            days = days or self.base.max_days_previous
            grid_data = self.base.get_history_wrapper(entity_id=grid_sensor, days=days, required=False, tracked=days <= self.base.max_days_previous)
        if not grid_data:
            return None
        grid_power, _ = minute_data(
            grid_data[0],
            days or self.base.max_days_previous,
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
            pad=False,  # only the history that exists, so the scan stops where it does
        )
        if self.base.get_arg("grid_power_invert", False, index=self.id):
            grid_power = {minute: -power for minute, power in grid_power.items()}
        return grid_power

    def load_battery_temperature_minutes(self, days):
        """
        The battery temperature history by minute before now, or None when battery_temperature_history is not set, has
        no history, or only ever reads exactly 0 (how several integrations report a missing reading).
        """
        series = []
        # One sensor or a list of them (one per inverter): the combined battery's temperature is their average, the same
        # one the plan reads (fetch_battery_temperature)
        for temperature_sensor in battery_temperature_entities(self.base.get_arg("battery_temperature_history", indirect=False)):
            temperature_data = self.base.get_history_wrapper(entity_id=temperature_sensor, days=days, required=False, tracked=days <= self.base.max_days_previous)
            if not temperature_data:
                continue
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
                pad=False,  # only the history that exists, so the scan stops where it does
            )
            series.append(temperature)
        temperature = average_temperature_minutes(series)
        if not any(value != 0 for value in temperature.values()):
            return None
        return temperature

    def find_rate_grid_samples(self, discharge, days=None):
        """
        Scan the recent history for the rate grid evidence: runs at the full written rate, cut into 5-minute segments.

        A minute counts when Predbat was Charging (or, for discharge, in a forced export - status Exporting), the rate
        was written at BATTERY_RATE_GRID_FULL_RATE of the maximum or more, the battery moved at BATTERY_RATE_GRID_MIN_POWER
        of the maximum or more in that direction (less is a charge or export that did not happen, not a limit), the
        export was not held back by export_limit, the export (or charge import) reached the grid - required for discharge,
        checked for charge when there is grid_power history - and the
        temperature reading is not exactly 0 (a missing reading) on an install that has a temperature sensor.
        Runs of BATTERY_RATE_GRID_RUN_MINUTES or more lose BATTERY_RATE_GRID_EDGE_MINUTES at each end (ramp up and
        down) and the rest is cut into centred BATTERY_RATE_GRID_SEGMENT_MINUTES segments, each averaged into one sample.

        The rate is recorded as learn_charge_curve() recorded it - battery power / rated maximum / battery_loss - with
        battery_rate_max_scaling (or _discharge) divided out, so it is relative to the rated rate. The full rate is the
        rate the history was written at (load_curve_history() "max_power"), so inverter_limit_charge, and for the forced
        exports inverter_limit_export, set what counts as full rate (the smaller of the cap and the rating). Predbat writes the
        export cap as the battery's discharge rate, so when the cap is below the discharge rating a minute where the battery
        reached the cap was held back by the cap rather than the battery: it is recorded as 1.0 (no battery limit seen)
        rather than as cap / rating, which would read as a curve dip. One where the battery stayed below the cap had the
        battery as the limit and is recorded in kW against the discharge rating. A minute whose grid export reached
        export_limit is recorded as 1.0 the same way (find_export_limited_minutes). When the cap is above the rating the rate is
        still recorded against the discharge rating (above 1.0 when the battery beat it, capped at BATTERY_RATE_GRID_MAX by
        the fit), so the house-load ceiling is the kW measured. With more than one
        inverter the combined battery is scanned (load_curve_history()).

        When nothing is found but the battery mostly moved the wrong way in minutes that otherwise qualified (charging
        during a full-rate export, discharging during a full-rate charge, with the grid showing the export or import),
        a warning points at the sign of battery_power (battery_power_invert), the usual cause.

        Args:
            discharge: True for the discharge evidence
            days: how many days of history to read, max_days_previous when not given

        Returns:
            {day: [[soc_percent, temperature or None, rate], ...]} keyed by charge_day(), or None when the history is missing.
        """
        purpose = "battery discharge rate grid" if discharge else "battery charge rate grid"
        days = days or self.base.max_days_previous
        if discharge and sum((inverter.battery_rate_max_export or 0) for inverter in self.curve_fleet()) <= 0:
            # An export cap of 0 (inverter_limit_export) means no forced exports, so there is no evidence to find: skip the history
            # read, and return no evidence rather than None (missing history) so the scan is not retried every hour
            self.log("Inverter {} {}: inverter_limit_export is 0, so there are no forced exports to learn from".format(self.id, purpose))
            # No coverage: a coverage left from an earlier scan would make the merge drop the stored days after it as "found nothing"
            self.record_curve_scan_coverage("grid_discharge", 0)
            return {}
        history = self.load_curve_history(discharge, purpose, days=days)
        if history is None:
            return None
        min_len = history["min_len"]
        temperature = self.load_battery_temperature_minutes(days)
        if temperature is not None:
            min_len = min(min_len, len(temperature))
        max_power = history["max_power"]
        soc_max = history["soc_max"]
        # The rate is recorded against the rating the plan scales the grid by, whatever the export cap
        reference_power = history.get("rated_power", max_power)
        if reference_power <= 0 or soc_max <= 0:
            return None
        if max_power <= 0:
            # Learning from this inverter alone (load_curve_history() fell back to it) and its export cap is 0: no forced exports
            self.record_curve_scan_coverage("grid_discharge", 0)
            return {}
        # Under the rating (an export cap below it) a minute where the battery reached the cap is not a battery limit
        capped = discharge and max_power < reference_power
        # Full rate is the written cap, or the rating when the cap is above it: a rate written at the rating or more asks the
        # battery for all it can give
        full_power = min(max_power, reference_power)
        min_rate = full_power * BATTERY_RATE_GRID_FULL_RATE
        min_power = max(full_power * BATTERY_RATE_GRID_MIN_POWER, 1.0)
        limited = set()
        grid_power = self.load_grid_power_minutes(days)
        if grid_power is not None:
            min_len = min(min_len, len(grid_power))
        if discharge:
            limited = self.find_export_limited_minutes(min_len, min_rate, history.get("export_limit_w"), days=days)
            if limited is None:
                return None
            if grid_power is None:
                # A forced export the inverter ignored (the battery only covering the house) looks like a slow battery, and
                # only the grid export can tell them apart
                self.log("Info: Inverter {} Cannot gather {} evidence, grid_power has no history to tell a forced export from the battery covering the house".format(self.id, purpose))
                return None
        battery_loss = self.base.battery_loss if self.base.battery_loss > 0 else 1.0
        scaling = self.base.battery_rate_max_scaling_discharge if discharge else self.base.battery_rate_max_scaling
        scaling = scaling if scaling > 0 else 1.0
        local_tz = getattr(self.base, "local_tz", None)
        self.record_curve_scan_coverage("grid_discharge" if discharge else "grid_charge", min_len)
        # Minutes that would have qualified but for the battery moving the other way, and ones that moved the right way
        direction_counts = {"wrong": 0, "right": 0}

        def minute_value(minute):
            """(soc %, temperature or None, rate) for a qualifying minute, else None."""
            status = str(history["status"].get(minute, ""))
            if discharge and not status.startswith("Exporting"):
                return None
            if not discharge and status != "Charging":
                return None
            if history["rate"].get(minute, 0) < min_rate:
                return None
            power = history["battery_power"].get(minute, 0)
            if abs(power) < min_power:
                return None
            if grid_power is not None:
                # The export (or, charging, the import) must reach the grid: otherwise the command was ignored and the battery
                # was covering the house, or charging from PV alone, rather than running at its limit
                grid = grid_power.get(minute, 0)
                if (grid < min_power) if discharge else (grid > -min_power):
                    return None
            if power < 0 if discharge else power > 0:
                # The battery moved the other way during a full-rate charge (export): counted for the battery_power sign warning
                direction_counts["wrong"] += 1
                return None
            direction_counts["right"] += 1
            soc = history["soc_kwh"].get(minute, None)
            if soc is None:
                return None
            temp = None
            if temperature is not None:
                temp = temperature.get(minute, None)
                if temp is None or temp == 0:
                    return None
            delivered = abs(power) / battery_loss / scaling
            if minute in limited or (capped and delivered >= max_power * BATTERY_RATE_GRID_FULL_RATE):
                # The grid export reached export_limit, or the battery reached the export cap written as its discharge rate: the
                # limit, not the battery's SoC or temperature, held it back
                return (soc / soc_max * 100.0, temp, 1.0)
            return (soc / soc_max * 100.0, temp, delivered / reference_power)

        samples = {}
        found = 0
        run = []  # (minute, value), minute indexes count back from now so a run is consecutive descending minutes
        for minute in range(0, min_len + 1):
            value = minute_value(minute) if minute < min_len else None
            if value is not None:
                run.append((minute, value))
                continue
            if len(run) >= BATTERY_RATE_GRID_RUN_MINUTES:
                interior = run[BATTERY_RATE_GRID_EDGE_MINUTES : len(run) - BATTERY_RATE_GRID_EDGE_MINUTES]
                count = len(interior) // BATTERY_RATE_GRID_SEGMENT_MINUTES
                offset = (len(interior) - count * BATTERY_RATE_GRID_SEGMENT_MINUTES) // 2
                for index in range(count):
                    piece = interior[offset + index * BATTERY_RATE_GRID_SEGMENT_MINUTES : offset + (index + 1) * BATTERY_RATE_GRID_SEGMENT_MINUTES]
                    soc = sum(item[1][0] for item in piece) / len(piece)
                    temp = None if temperature is None else round(sum(item[1][1] for item in piece) / len(piece), 2)
                    rate = sum(item[1][2] for item in piece) / len(piece)
                    day = charge_day(self.base.now_utc, piece[len(piece) // 2][0], local_tz)
                    samples.setdefault(day, []).append([dp2(soc), temp, dp4(rate)])
                    found += 1
            run = []
        if not found and direction_counts["wrong"] >= BATTERY_RATE_GRID_RUN_MINUTES and direction_counts["wrong"] > direction_counts["right"]:
            self.log(
                "Warn: Inverter {} {}: in {} minutes of full-rate {} battery_power showed the battery {} instead, so battery_power probably has the wrong sign (Predbat expects it negative while charging, positive while discharging) - check battery_power_invert".format(
                    self.id,
                    purpose,
                    direction_counts["wrong"],
                    "export" if discharge else "charge",
                    "charging" if discharge else "discharging",
                )
            )
        self.log("Inverter {} {}: found {} full-rate {}-minute segments in {} days of history{}".format(self.id, purpose, found, BATTERY_RATE_GRID_SEGMENT_MINUTES, days, "" if temperature is not None else " (no battery temperature)"))
        return samples

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------


"""Tariff comparison engine.

Runs optimisation scenarios across different energy tariffs to compare
costs, enabling users to evaluate whether switching tariffs would reduce
their energy bills. Loads tariff definitions from YAML and generates
per-tariff plan visualisations.
"""

import os
import math
from datetime import datetime
from const import MINUTE_WATT
from utils import dp0, dp2
import yaml
import copy

# TODO:
# 1. Update web UI to show a chart of historical differences
# 2. Allow change to start/end comparison points e.g. tomorrow or today
# 3. Consider Octopus API key to access current tariff info and switch links


# An empty 'config:' line means no overrides; any other non-mapping value can't be applied
EMPTY_COMPARE_CONFIG = (None, "", [], {})


def check_compare_list(compare_list):
    """Split compare_list into the entries that can be compared and a message per skipped entry (#5472).

    Each kept entry is a copy with its id and name as stripped strings, and its config either a
    mapping of overrides or absent, so run_all() and fetch_config() can rely on that shape.
    """
    if not compare_list:
        return [], []
    if not isinstance(compare_list, list):
        return [], ["compare_list must be a list of tariffs, each with an id and a name"]

    valid = []
    problems = []
    seen_ids = set()
    for index, entry in enumerate(compare_list):
        problem = None
        entry_id = ""
        if not isinstance(entry, dict):
            problem = "must be a dictionary with an id and a name"
        else:
            # Results, sensor names and the web page are all keyed by the id as a string, so an
            # unquoted numeric id in apps.yaml must not reach them as an int
            entry_id = "" if entry.get("id") is None else str(entry["id"]).strip()
            # A numeric name would likewise break publish_data() building the friendly name
            entry_name = "" if entry.get("name") is None else str(entry["name"]).strip()
            if not entry_id:
                problem = "requires a non-empty 'id'"
            elif entry_id in seen_ids:
                problem = "has a duplicate id '{}'".format(entry_id)
            elif not entry_name:
                problem = "('{}') requires a non-empty 'name'".format(entry_id)
            elif entry.get("config") not in EMPTY_COMPARE_CONFIG and not isinstance(entry["config"], dict):
                # run_all() and fetch_config() look overrides up by key; a list of them raised TypeError
                # there and ended the whole comparison, and any other value was never a usable override
                problem = "('{}') has a 'config' that isn't a set of 'setting: value' overrides".format(entry_id)
        if problem:
            problems.append("compare_list entry {} {}".format(index + 1, problem))
            continue
        seen_ids.add(entry_id)
        checked = dict(entry, id=entry_id, name=entry_name)
        if checked.get("config") in EMPTY_COMPARE_CONFIG:
            checked.pop("config", None)
        valid.append(checked)
    return valid, problems


def valid_compare_list(compare_list, log=None):
    """Return the compare_list entries that can be compared, logging a warning for each one skipped (#5472)."""
    valid, problems = check_compare_list(compare_list)
    if log:
        for problem in problems:
            log("Warn: Compare, {}, skipping it".format(problem))
    return valid


# What Compare.run_all() saves before comparing tariffs and restores afterwards: every tariff fetches, plans and
# predicts on the live instance. The plan state is what calculate_plan() writes that outlives the run - leaving
# plan_preclip as the last tariff's plan once made the first re-plan after midnight score the wrong plan as the
# plan in force. Add any new plan state calculate_plan() keeps across runs here.
COMPARE_PLAN_STATE = (
    "charge_window_best",
    "charge_limit_best",
    "export_window_best",
    "export_limits_best",
    "plan_preclip",
    "plan_made_by_version",
    "end_record",
    "plan_valid",
    "plan_last_updated",
    "plan_last_updated_minutes",
    "rate_best_cost_threshold_charge",
    "rate_best_cost_threshold_export",
    "io_confirmed_tiebreak",
    # The live plan's PV10 and PV90 series behind its scenario tooltips (#5871)
    "predict_soc_best10",
    "predict_metric_best10",
    "predict_soc_best90",
    "predict_metric_best90",
)
# Battery and inverter values a tariff can override, reset after each tariff as well as at the end
COMPARE_HARDWARE_STATE = ("soc_max", "battery_rate_max_charge", "battery_rate_max_charge_dc", "battery_rate_max_discharge", "battery_rate_max_export", "inverter_limit")
COMPARE_RESTORED_STATE = (
    COMPARE_PLAN_STATE
    + COMPARE_HARDWARE_STATE
    + (
        "forecast_plan_hours",
        "forecast_minutes",
        "forecast_days",
        "manual_charge_times",
        "manual_export_times",
        "manual_freeze_charge_times",
        "manual_freeze_export_times",
        "manual_demand_times",
        "manual_all_times",
        "cost_today_sofar",
        "carbon_today_sofar",
        "iboost_today",
        "iboost_plan",
        "import_today_now",
        "export_today_now",
        "octopus_intelligent_charging",
        "soc_kw",
        # Rebuilt by each tariff's fetch, which the live plan's Prediction must not inherit
        "io_confirmed_credit",
    )
)
# Car state the comparison rebuilds per tariff, deep-copied as the lists are changed in place
COMPARE_RESTORED_COPIES = (
    "car_charging_plan_smart",
    "car_charging_limit",
    "car_charging_limit_model",
    "car_charging_soc",
    "car_charging_battery_size",
    "car_charging_slots",
    "car_charging_now_slots",
)


class Compare:
    """Tariff comparison engine.

    Runs optimisation scenarios across different energy tariffs loaded
    from YAML configuration, generating per-tariff plans and cost
    comparisons to evaluate tariff switching decisions.
    """

    def __init__(self, my_predbat):
        self.pb = my_predbat
        self.log = self.pb.log
        # Live cycle's saving-minute sets and pre-saving snapshots, captured by run_all() with the base rates
        self.live_saving_state = None
        self.config_root = self.pb.config_root
        self.dashboard_item = self.pb.dashboard_item
        self.currency_symbols = self.pb.currency_symbols
        self.prefix = self.pb.prefix
        self.comparisons = {}
        self.load_yaml()

    def fetch_config(self, tariff):
        """
        Fetch configuration overrides
        """
        config = tariff.get("config", {})
        for key in config:
            item = self.pb.config_index.get(key)
            if item:
                self.log("Compare, override {} to {}".format(key, config[key]))
                item["value"] = config[key]
            else:
                self.log("Warn: Compare, config item {} not found".format(key))
        self.pb.fetch_config_options()

    def fetch_rates(self, tariff, rate_import_base, rate_export_base, io_adjusted_base=None, rate_import_match=None, rate_export_match=None):
        """
        Install the tariff's rates on the instance and return True if they match the user's existing tariff.

        rate_import_match/rate_export_match are the live tariff without saving session, free electricity,
        Axle or Intelligent dispatch stamps - what a refetched rate card is checked against (#5180)
        """
        pb = self.pb
        if io_adjusted_base is None:
            io_adjusted_base = pb.io_adjusted

        # Reset threshold to automatic
        pb.rate_low_threshold = 0
        pb.rate_high_threshold = 0

        # Reset rates to base
        pb.rate_import = copy.deepcopy(rate_import_base)
        pb.rate_export = copy.deepcopy(rate_export_base)
        # Both sides start as the live cycle's rates. Each branch below that installs this tariff's own
        # rates for a side records it here, so the checks further down don't depend on object identity
        # (which a copying rate_replicate() or rate source would silently break)
        import_replaced = False
        export_replaced = False

        # Intelligent Octopus dispatch markers go with the import rates they came from (#5286)
        pb.io_adjusted = {}

        # The base rates are the live cycle's, so the live saving-minute sets and pre-saving snapshots
        # captured by run_all() still describe them (same cycle, same minute offsets). Start from those;
        # a side whose rates this tariff replaces has them cleared below.
        live_saving = self.live_saving_state or {}
        pb.rate_import_saving_minutes = set(live_saving.get("import_minutes", set()))
        pb.rate_export_saving_minutes = set(live_saving.get("export_minutes", set()))
        pb.rate_import_pre_saving = dict(live_saving.get("import_pre_saving", {}))
        pb.rate_export_pre_saving = dict(live_saving.get("export_pre_saving", {}))

        # Fetch rates from Octopus Energy API
        if "rates_import_octopus_url" in tariff:
            # Fixed URL for rate import
            import_url = pb.resolve_arg("rates_import_octopus_url", tariff["rates_import_octopus_url"], indirect=False)
            pb.rate_import = pb.download_octopus_rates(import_url)
            import_replaced = True
        elif "metric_octopus_import" in tariff:
            # Octopus import rates
            entity_id = pb.resolve_arg("metric_octopus_import", tariff["metric_octopus_import"], indirect=False)
            if entity_id:
                pb.rate_import = pb.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")
                import_replaced = True
            else:
                self.log("Warn: Compare tariff {} bad Octopus entity id {}".format(tariff.get("id", ""), entity_id))
        elif "metric_energidataservice_import" in tariff:
            # Energi Data Service import rates
            entity_id = pb.resolve_arg("metric_energidataservice_import", tariff["metric_energidataservice_import"], indirect=False)
            if entity_id:
                pb.rate_import = pb.fetch_energidataservice_rates(entity_id, adjust_key="is_intelligent_adjusted")
                import_replaced = True
            else:
                self.log("Warn: Compare tariff {} bad Energidata entity id {}".format(tariff.get("id", ""), entity_id))
        elif "metric_stromligning_import_today" in tariff or "metric_stromligning_import_tomorrow" in tariff:
            # Strømligning import rates
            entity_id_today = pb.resolve_arg("metric_stromligning_import_today", tariff.get("metric_stromligning_import_today"))
            entity_id_tomorrow = pb.resolve_arg("metric_stromligning_import_tomorrow", tariff.get("metric_stromligning_import_tomorrow"))
            if entity_id_today or entity_id_tomorrow:
                pb.rate_import = pb.fetch_stromligning_rates(entity_id_today, entity_id_tomorrow, adjust_key="is_intelligent_adjusted")
                import_replaced = True
            else:
                self.log("Warn: Compare tariff {} bad Strømligning entity ids".format(tariff.get("id", "")))
        elif "rates_import" in tariff:
            pb.rate_import = pb.basic_rates(tariff["rates_import"], "rates_import", include_manual_api=False)
            import_replaced = True
        else:
            self.log("Using existing rate import data")
        if not import_replaced:
            pb.io_adjusted = copy.deepcopy(io_adjusted_base)

        if "rates_export_octopus_url" in tariff:
            # Fixed URL for rate export
            pb.rate_export = pb.download_octopus_rates(pb.resolve_arg("rates_export_octopus_url", tariff["rates_export_octopus_url"], indirect=False))
            export_replaced = True
        elif "metric_octopus_export" in tariff:
            # Octopus export rates
            entity_id = pb.resolve_arg("metric_octopus_export", tariff["metric_octopus_export"], indirect=False)
            if entity_id:
                pb.rate_export = pb.fetch_octopus_rates(entity_id)
                export_replaced = True
            else:
                self.log("Warn: Compare tariff {} bad Octopus entity id {}".format(tariff.get("id", ""), entity_id))
        elif "metric_energidataservice_export" in tariff:
            # Energi Data Service export rates
            entity_id = pb.resolve_arg("metric_energidataservice_export", tariff["metric_energidataservice_export"], indirect=False)
            if entity_id:
                pb.rate_export = pb.fetch_energidataservice_rates(entity_id, adjust_key="is_intelligent_adjusted")
                export_replaced = True
            else:
                self.log("Warn: Compare tariff {} bad Energidata entity id {}".format(tariff.get("id", ""), entity_id))
        elif "metric_stromligning_export_today" in tariff or "metric_stromligning_export_tomorrow" in tariff:
            # Strømligning export rates
            entity_id_today = pb.resolve_arg("metric_stromligning_export_today", tariff.get("metric_stromligning_export_today"))
            entity_id_tomorrow = pb.resolve_arg("metric_stromligning_export_tomorrow", tariff.get("metric_stromligning_export_tomorrow"))
            if entity_id_today or entity_id_tomorrow:
                pb.rate_export = pb.fetch_stromligning_rates(entity_id_today, entity_id_tomorrow)
                export_replaced = True
            else:
                self.log("Warn: Compare tariff {} bad Strømligning entity ids".format(tariff.get("id", "")))
        elif "rates_export" in tariff:
            pb.rate_export = pb.basic_rates(tariff["rates_export"], "rates_export", include_manual_api=False)
            export_replaced = True
        else:
            self.log("Using existing rate export data")

        # A tariff that supplied its own rates for a side makes the live saving minutes for that side
        # stale: they are offsets into the live tables, and would map unrelated minutes of the new tariff
        # back to live "base" rates. A side left on the live rates keeps them.
        if import_replaced:
            pb.rate_import_saving_minutes = set()
            pb.rate_import_pre_saving = {}
        if export_replaced:
            pb.rate_export_saving_minutes = set()
            pb.rate_export_pre_saving = {}

        # Predicted future rates (futurerate) belong to the live tariff, so only a side that is the live
        # tariff gets them: one left on the live rates, or one whose own published rates are the live ones
        import_future = (not import_replaced) or self.is_live_published(pb.rate_import, rate_import_match if rate_import_match else rate_import_base)
        export_future = (not export_replaced) or self.is_live_published(pb.rate_export, rate_export_match if rate_export_match else rate_export_base)

        if pb.rate_import:
            pb.rate_scan(pb.rate_import, print=False)
            pb.rate_import, pb.rate_import_replicated = pb.rate_replicate(pb.rate_import, pb.io_adjusted, is_import=True, use_future=import_future)
            if "rates_import_override" in tariff:
                # A kept pre-saving snapshot must carry the same override, as fetch_sensor_data()'s does
                if pb.rate_import_saving_minutes and pb.rate_import_pre_saving:
                    pb.rate_import_pre_saving = pb.override_session_rates(pb.rate_import, pb.rate_import_pre_saving, pb.rate_import_saving_minutes, tariff["rates_import_override"], "rates_import_override", {}, True, include_manual_api=False)
                pb.rate_import = pb.basic_rates(tariff["rates_import_override"], "rates_import_override", pb.rate_import, pb.rate_import_replicated, include_manual_api=False)
            pb.rate_scan(pb.rate_import, print=True)

        # Replicate and scan export rates
        if pb.rate_export:
            pb.rate_scan_export(pb.rate_export, print=False)
            pb.rate_export, pb.rate_export_replicated = pb.rate_replicate(pb.rate_export, is_import=False, use_future=export_future)
            if "rates_export_override" in tariff:
                if pb.rate_export_saving_minutes and pb.rate_export_pre_saving:
                    pb.rate_export_pre_saving = pb.override_session_rates(pb.rate_export, pb.rate_export_pre_saving, pb.rate_export_saving_minutes, tariff["rates_export_override"], "rates_export_override", {}, False, include_manual_api=False)
                pb.rate_export = pb.basic_rates(tariff["rates_export_override"], "rates_export_override", pb.rate_export, pb.rate_export_replicated, include_manual_api=False)
            pb.rate_scan_export(pb.rate_export, print=True)

        # Set rate thresholds
        if pb.rate_import or pb.rate_export:
            pb.set_rate_thresholds()

        # Find discharging windows
        if pb.rate_export:
            pb.high_export_rates, export_lowest, export_highest = pb.rate_scan_window(pb.rate_export, 5, pb.rate_export_cost_threshold, True)
            # Update threshold automatically
            if pb.rate_high_threshold == 0 and export_lowest <= pb.rate_export_max:
                pb.rate_export_cost_threshold = export_lowest

        # Find charging windows
        if pb.rate_import:
            # Find charging window
            pb.find_low_rate_windows()

        # Compare to see if rates changes. A side this tariff fetched its own rates for never carries the live
        # cycle's events, so it is checked against the live tariff without them - otherwise an active saving
        # session or dispatch makes the user's own tariff look different (#5180). A side left on the live
        # rates is checked against them as they are, so only this tariff's override can make it differ.
        import_match = rate_import_match if (import_replaced and rate_import_match) else rate_import_base
        export_match = rate_export_match if (export_replaced and rate_export_match) else rate_export_base
        for minute in range(pb.minutes_now, pb.forecast_minutes + pb.minutes_now):
            if dp2(pb.rate_import.get(minute, 0)) != dp2(import_match.get(minute, 0)):
                self.log("Compare rate import is different, minute {} changed from {} to {}".format(minute, import_match.get(minute, 0), pb.rate_import.get(minute, 0)))
                return False
            if dp2(pb.rate_export.get(minute, 0)) != dp2(export_match.get(minute, 0)):
                self.log("Compare rate export is different, minute {} changed from {} to {}".format(minute, export_match.get(minute, 0), pb.rate_export.get(minute, 0)))
                return False
        return True

    def is_live_published(self, rates, live_rates):
        """
        True if every rate this tariff publishes from now on equals the live tariff's rate for that minute,
        i.e. it is the live tariff refetched rather than a different one
        """
        minutes_now = self.pb.minutes_now
        overlap = [minute for minute in rates if minute >= minutes_now and minute in live_rates]
        return bool(overlap) and all(dp2(rates[minute]) == dp2(live_rates[minute]) for minute in overlap)

    def apply_hardware_overrides(self, tariff, my_predbat):
        """
        Apply optional hardware overrides from a tariff definition to model
        a different battery size, charge rate, or inverter limit.

        Supported tariff keys (all optional):
          override_soc_max_kwh                  - battery usable capacity in kWh
          override_battery_rate_max_charge_kw   - max charge rate in kW (also scales battery_rate_max_charge_dc proportionally)
          override_battery_rate_max_discharge_kw - max discharge rate in kW
          override_battery_rate_max_export_kw   - max discharge rate during export windows in kW (follows the discharge
                                                  override only when one applied successfully; otherwise the real
                                                  hardware export rate is kept)
          override_inverter_limit_kw            - AC inverter output limit in kW

        The prediction uses battery_rate_max_export, not battery_rate_max_discharge, once an
        export window is active, so a discharge override on its own left export slots pinned at
        the real hardware rate (issue #4895). The discharge override therefore carries the export
        rate with it unless an explicit export override is given, which lets export-limited
        hardware still be modelled. The grid export connection limit (export_limit) is deliberately
        left alone - it belongs to the property, not to the modelled inverter.

        These keys replace the fleet-wide totals that execute.py sums across all inverters, so
        on a multi-inverter system enter the combined figure, not a single inverter's rating.
        """
        if "override_soc_max_kwh" in tariff:
            try:
                value = float(tariff["override_soc_max_kwh"])
                if not math.isfinite(value):
                    raise ValueError
                my_predbat.soc_max = value
                # Clamp starting SoC to new capacity
                my_predbat.soc_kw = min(my_predbat.soc_kw, my_predbat.soc_max)
                self.log("Compare, override soc_max to {:.2f} kWh (soc_kw clamped to {:.2f} kWh)".format(value, my_predbat.soc_kw))
            except (ValueError, TypeError):
                self.log("Warn: Compare tariff {} override_soc_max_kwh value '{}' is not numeric, skipping".format(tariff.get("id", ""), tariff["override_soc_max_kwh"]))

        if "override_battery_rate_max_charge_kw" in tariff:
            try:
                value = float(tariff["override_battery_rate_max_charge_kw"])
                if not math.isfinite(value):
                    raise ValueError
                new_ac = value * 1000 / MINUTE_WATT
                # Scale the DC charge rate by the same ratio so hybrid/DC-coupled PV modelling stays consistent
                if my_predbat.battery_rate_max_charge > 0:
                    my_predbat.battery_rate_max_charge_dc = my_predbat.battery_rate_max_charge_dc * new_ac / my_predbat.battery_rate_max_charge
                my_predbat.battery_rate_max_charge = new_ac
                self.log("Compare, override battery_rate_max_charge to {:.2f} kW (battery_rate_max_charge_dc scaled to {:.2f} kW)".format(value, my_predbat.battery_rate_max_charge_dc * MINUTE_WATT / 1000))
            except (ValueError, TypeError):
                self.log("Warn: Compare tariff {} override_battery_rate_max_charge_kw value '{}' is not numeric, skipping".format(tariff.get("id", ""), tariff["override_battery_rate_max_charge_kw"]))

        discharge_overridden = False
        if "override_battery_rate_max_discharge_kw" in tariff:
            try:
                value = float(tariff["override_battery_rate_max_discharge_kw"])
                if not math.isfinite(value):
                    raise ValueError
                my_predbat.battery_rate_max_discharge = value * 1000 / MINUTE_WATT
                self.log("Compare, override battery_rate_max_discharge to {:.2f} kW".format(value))
                discharge_overridden = True
            except (ValueError, TypeError):
                self.log("Warn: Compare tariff {} override_battery_rate_max_discharge_kw value '{}' is not numeric, skipping".format(tariff.get("id", ""), tariff["override_battery_rate_max_discharge_kw"]))

        export_overridden = False
        if "override_battery_rate_max_export_kw" in tariff:
            try:
                value = float(tariff["override_battery_rate_max_export_kw"])
                if not math.isfinite(value):
                    raise ValueError
                my_predbat.battery_rate_max_export = value * 1000 / MINUTE_WATT
                self.log("Compare, override battery_rate_max_export to {:.2f} kW".format(value))
                export_overridden = True
            except (ValueError, TypeError):
                self.log("Warn: Compare tariff {} override_battery_rate_max_export_kw value '{}' is not numeric, skipping".format(tariff.get("id", ""), tariff["override_battery_rate_max_export_kw"]))

        # Without this the modelled hardware discharges at the override rate everywhere except
        # export windows, which keep the real hardware's export rate and so understate export
        # earnings (issue #4895). A bad explicit export value also falls through to here when a
        # discharge override applied, so a typo can't silently reinstate the real hardware rate -
        # but with no discharge override there is nothing to fall back to and the real hardware
        # export rate is kept (pinned by T20).
        if discharge_overridden and not export_overridden:
            my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
            self.log("Compare, override battery_rate_max_export to {:.2f} kW (following the discharge rate override)".format(my_predbat.battery_rate_max_export * MINUTE_WATT / 1000))

        if "override_inverter_limit_kw" in tariff:
            try:
                value = float(tariff["override_inverter_limit_kw"])
                if not math.isfinite(value):
                    raise ValueError
                my_predbat.inverter_limit = value * 1000 / MINUTE_WATT
                self.log("Compare, override inverter_limit to {:.2f} kW".format(value))
            except (ValueError, TypeError):
                self.log("Warn: Compare tariff {} override_inverter_limit_kw value '{}' is not numeric, skipping".format(tariff.get("id", ""), tariff["override_inverter_limit_kw"]))

    def run_scenario(self, end_record):
        my_predbat = self.pb

        pv_step = my_predbat.pv_forecast_minute_step
        pv10_step = my_predbat.pv_forecast_minute10_step
        load_step = my_predbat.load_minutes_step
        load10_step = my_predbat.load_minutes_step10

        my_predbat.calculate_plan(recompute=True, debug_mode=False, publish=False)

        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
            my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=end_record, save="compare"
        )
        cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10 = my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            True,
            end_record=end_record,
        )
        # Work out value of the battery at the start end end of the period to allow for the change in SoC
        metric_start, battery_value_start = my_predbat.compute_metric(end_record, my_predbat.soc_kw, my_predbat.soc_kw, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        metric_end, battery_value_end = my_predbat.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, 0, 0, 0, 0, 0, 0)

        # Subtract the start metric from the end metric to avoid credit for the current battery level re-based on the new tariff
        metric = metric_end - metric_start
        html, raw_plan = my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, end_record, publish=False)

        result_data = {
            "cost": dp2(cost),
            "cost10": dp2(cost10),
            "import_kwh": dp2(import_kwh_battery + import_kwh_house),
            "import_kwh10": dp2(import_kwh_battery10 + import_kwh_house10),
            "export_kwh": dp2(export_kwh),
            "export_kwh10": dp2(export_kwh10),
            "soc_start": dp2(my_predbat.soc_kw),
            "soc": dp2(soc),
            "soc10": dp2(soc10),
            "soc_min": dp2(soc_min),
            "soc_min10": dp2(soc_min10),
            "battery_cycle": dp2(battery_cycle),
            "battery_cycle10": dp2(battery_cycle10),
            "metric": dp2(metric),
            "metric_keep": dp2(metric_keep),
            "metric_keep10": dp2(metric_keep10),
            "final_iboost": dp2(final_iboost),
            "final_iboost10": dp2(final_iboost10),
            "final_carbon_g": dp0(final_carbon_g),
            "final_carbon_g10": dp0(final_carbon_g10),
            "battery_value_start": dp2(battery_value_start),
            "battery_value_end": dp2(battery_value_end),
            "metric_real": dp2(metric_end),
            "end_record": end_record,
        }
        for item in result_data:
            result_data[item] = dp2(result_data[item])
        result_data["html"] = html
        result_data["raw"] = raw_plan
        result_data["date"] = my_predbat.now_utc.strftime("%Y-%m-%d %H:%M:%S")
        result_data["best"] = False

        return result_data

    def run_single(self, tariff, rate_import_base, rate_export_base, end_record, debug=False, fetch_sensor=True, car_charging_slots=None, start_soc=None, io_adjusted_base=None, rate_import_match=None, rate_export_match=None):
        """
        Compare a single energy tariff with the current settings and report results
        """
        if car_charging_slots is None:
            car_charging_slots = []
        my_predbat = self.pb
        name = tariff.get("name", None)
        tariff_id = tariff.get("id", "")
        if not name:
            self.log("Warn: Compare tariff name not found")
            return None
        self.log("Compare Tariff: {}".format(name))
        self.fetch_config(tariff)

        if fetch_sensor:
            self.pb.fetch_sensor_data(save=False)

        # Override starting SoC so each tariff comparison begins from a fair baseline
        # rather than the live (mid-run) inverter SoC
        if start_soc is not None:
            my_predbat.soc_kw = start_soc

        # Apply optional hardware overrides (battery size, charge rate, inverter limit)
        self.apply_hardware_overrides(tariff, my_predbat)

        # Fetch rates
        try:
            existing_tariff = self.fetch_rates(tariff, rate_import_base, rate_export_base, io_adjusted_base, rate_import_match, rate_export_match)
        except ValueError as e:
            self.log("Warn fetching rates during comparison of tariff {}: {}".format(tariff, e))
            return {}

        # Reset totals
        my_predbat.cost_today_sofar = 0
        my_predbat.carbon_today_sofar = 0
        my_predbat.iboost_today = 0
        my_predbat.import_today_now = 0
        my_predbat.export_today_now = 0

        # Change to a fixed 48 hour plan
        my_predbat.forecast_plan_hours = 48
        my_predbat.forecast_minutes = my_predbat.forecast_plan_hours * 60
        my_predbat.forecast_days = my_predbat.forecast_plan_hours / 24

        # Clear manual times to avoid users overrides
        my_predbat.manual_charge_times = []
        my_predbat.manual_export_times = []
        my_predbat.manual_freeze_charge_times = []
        my_predbat.manual_freeze_export_times = []
        my_predbat.manual_demand_times = []
        my_predbat.manual_all_times = []
        my_predbat.octopus_intelligent_charging = False

        self.recompute_iboost()
        self.recompute_car_charging(car_charging_slots)

        self.log("Running scenario for tariff: {}".format(name))
        result_data = self.run_scenario(end_record)
        result_data["existing_tariff"] = existing_tariff
        result_data["name"] = name
        self.log("Scenario complete for tariff: {}, cost {}{}, metric {}{}".format(name, result_data["cost"], self.currency_symbols[1], result_data["metric"], self.currency_symbols[1]))
        if debug:
            with open("compare_{}.html".format(tariff_id), "w") as f:
                f.write(result_data["html"])
        return result_data

    def select_best(self, compare_list, results):
        """
        Recommend the best tariff
        """
        best_selected = ""
        best_metric = 9999999999

        for compare in compare_list:
            tariff_id = compare.get("id", "")
            result = results.get(tariff_id, {})
            if result:
                metric = result.get("metric", best_metric)
                if metric < best_metric:
                    best_metric = metric
                    best_selected = tariff_id

        for result_id in results:
            results[result_id]["best"] = result_id == best_selected

        self.log("Compare, best tariff: {} metric {}{}".format(best_selected, best_metric, self.currency_symbols[1]))

    def get_comparison(self, tariff_id):
        """
        Get comparisons
        """
        return self.comparisons.get(tariff_id, {})

    def load_yaml(self):
        """
        Load comparisons from yaml
        """
        filepath = self.config_root + "/comparisons.yaml"
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                try:
                    data = yaml.safe_load(f)
                    if data:
                        # str() keys: a numeric id saved before ids were normalised (#5472) would
                        # otherwise break publish_data() and never match its compare_list entry again
                        self.comparisons = {str(tariff_id): result for tariff_id, result in (data.get("comparisons") or {}).items()}
                except yaml.YAMLError as exc:
                    self.log("Error loading comparisons: {}".format(exc))

        if self.comparisons:
            compare_list = valid_compare_list(self.pb.get_arg("compare_list", []))
            self.select_best(compare_list, self.comparisons)
            self.publish_data()

    def save_yaml(self):
        """
        Save comparisons to yaml
        """
        filepath = self.config_root + "/comparisons.yaml"
        save_data = {}
        save_data["comparisons"] = self.comparisons

        with open(filepath, "w") as f:
            f.write(yaml.dump(save_data, default_flow_style=False))

    def publish_data(self):
        """
        Publish comparison data to HA
        """
        for tariff_id in self.comparisons:
            result = self.get_comparison(tariff_id)

            if result:
                cost = result.get("cost", 0)
                # str(): a numeric name saved before names were normalised (#5472) must not crash startup
                name = str(result.get("name", ""))
                attributes = {
                    "friendly_name": "Compare " + name,
                    "state_class": "measurement",
                    "unit_of_measurement": "p",
                    "icon": "mdi::compare-horizontal",
                }
                for item in result:
                    if item == "raw":  # skip writing raw compare plan data to the compare entity
                        continue

                    value = result[item]
                    if item != "html":
                        attributes[item] = value

                entity_id = self.prefix + ".compare_tariff_" + tariff_id
                self.dashboard_item(
                    entity_id,
                    state=cost,
                    attributes=attributes,
                )
                result["entity_id"] = entity_id

    def publish_only(self):
        """
        Update HA sensors only
        """
        compare_list = valid_compare_list(self.pb.get_arg("compare_list", []))
        if not compare_list:
            return

        self.select_best(compare_list, self.comparisons)
        self.publish_data()

    def recompute_iboost(self):
        """
        Recompute iBoost plan
        """
        my_predbat = self.pb

        if my_predbat.iboost_enable and (((not my_predbat.iboost_solar) and (not my_predbat.iboost_charging)) or my_predbat.iboost_smart):
            my_predbat.iboost_plan = my_predbat.plan_iboost_smart()
        else:
            my_predbat.iboost_plan = []

    def recompute_car_charging(self, car_charging_slots):
        """
        Recompute car charging plan
        """
        my_predbat = self.pb

        my_predbat.car_charging_slots = [[] for car_n in range(my_predbat.num_cars)]
        # The live cycle's modelled car_charging_now slots were judged against the live car plan, which is
        # rebuilt here for the compared tariff - so drop them rather than layer them on top of it
        my_predbat.car_charging_now_slots = [[] for car_n in range(my_predbat.num_cars)]
        # Compare re-plans car charging on the rate-based path (plan_car_charging), where the real
        # fill clamp must hold - drop any model-facing limit override left by the live IOG fetch (#4967)
        my_predbat.car_charging_limit_model = None

        for car_n in range(my_predbat.num_cars):
            total_car_kwh = 0
            if len(car_charging_slots) > car_n and car_charging_slots[car_n]:
                for car_slot in car_charging_slots[car_n]:
                    total_car_kwh += car_slot.get("kwh", 0)
            if total_car_kwh > 0:
                my_predbat.car_charging_soc[car_n] = 0
                my_predbat.car_charging_limit[car_n] = total_car_kwh
                my_predbat.car_charging_battery_size[car_n] = 100
            else:
                my_predbat.car_charging_soc[car_n] = my_predbat.car_charging_battery_size[car_n]
                my_predbat.car_charging_limit[car_n] = my_predbat.car_charging_battery_size[car_n]

            if my_predbat.car_charging_planned[car_n] or my_predbat.car_charging_now[car_n]:
                self.log("Re-plan car {} for charging to {}".format(car_n, my_predbat.car_charging_limit[car_n]))
                my_predbat.car_charging_plan_smart[car_n] = True
                my_predbat.car_charging_slots[car_n] = my_predbat.plan_car_charging(car_n, my_predbat.low_rates_tariff)

            if my_predbat.car_charging_planned[car_n] and my_predbat.car_charging_exclusive[car_n]:
                break

    def run_all(self, debug=False, fetch_sensor=True):
        """
        Compare a comparison in prices across multiple energy tariffs and report results
        take care not to destroy the state of the system for the primary settings
        """
        compare_list = valid_compare_list(self.pb.get_arg("compare_list", []), log=self.log)
        if not compare_list:
            return

        results = self.comparisons

        my_predbat = self.pb

        # Everything the comparison changes on the live instance, saved here and put back in the finally below
        saved = {name: getattr(my_predbat, name) for name in COMPARE_RESTORED_STATE}
        saved.update({name: copy.deepcopy(getattr(my_predbat, name)) for name in COMPARE_RESTORED_COPIES})

        # Final reports, cut end_record back to 24 hours to ignore the dump at end of day
        end_record = int((my_predbat.minutes_now + 24 * 60 + 29) / 30) * 30 - my_predbat.minutes_now

        # Save baseline rates
        rate_import_base = copy.deepcopy(self.pb.rate_import)
        rate_export_base = copy.deepcopy(self.pb.rate_export)
        io_adjusted_base = copy.deepcopy(self.pb.io_adjusted)
        # The same rates before any saving session, free electricity, Axle or dispatch stamp, to recognise
        # the existing tariff by (#5180) - taken now, as fetch_sensor_data() in run_single() rebuilds them.
        # rate_export_base is not reset in a cycle with no export rates, so it is only trusted when this cycle has some;
        # an empty match falls back to the live table in fetch_rates(), as the import side already does
        rate_import_match = copy.deepcopy(self.pb.rate_import_no_io)
        rate_export_match = copy.deepcopy(self.pb.rate_export_base) if self.pb.rate_export else {}
        self.live_saving_state = {
            "import_minutes": set(self.pb.rate_import_saving_minutes),
            "export_minutes": set(self.pb.rate_export_saving_minutes),
            "import_pre_saving": dict(self.pb.rate_import_pre_saving),
            "export_pre_saving": dict(self.pb.rate_export_pre_saving),
        }

        # Midnight SOC fallback for tariffs with no prior result
        soc_midnight_fallback = my_predbat.soc_kwh_history.get(my_predbat.minutes_now, my_predbat.soc_kw)
        today_date = datetime.now().strftime("%Y-%m-%d")

        # Snapshot config values for all keys that any tariff may override via fetch_config().
        # Done once before the loop so the original values are captured before any tariff mutates them.
        config_snapshot = {}
        for tariff in compare_list:
            for key in tariff.get("config", {}):
                if key not in config_snapshot:
                    item = my_predbat.config_index.get(key)
                    if item is not None:
                        config_snapshot[key] = item.get("value")

        self.log("Starting comparison of tariffs")

        try:
            for tariff in compare_list:
                tariff_id = tariff["id"]
                prior = results.get(tariff_id, {})
                if prior:
                    prior_date = prior.get("date", "")[:10]
                    if prior_date == today_date:
                        # Same day: reuse the same starting SoC so repeated runs stay consistent
                        start_soc = prior.get("soc_start", soc_midnight_fallback)
                    else:
                        # New day: carry forward yesterday's predicted ending SoC as today's start
                        start_soc = prior.get("soc", soc_midnight_fallback)
                else:
                    # First ever run for this tariff: start from actual midnight SoC
                    start_soc = soc_midnight_fallback
                self.log("Compare tariff {} starting SoC: {:.2f} kWh".format(tariff_id, start_soc))
                # Each tariff plans from scratch: scored against the previous tariff's plan, the keep-or-switch threshold
                # in calculate_plan() could leave one tariff's result as another tariff's plan
                my_predbat.plan_valid = False
                try:
                    result_data = self.run_single(
                        tariff,
                        rate_import_base,
                        rate_export_base,
                        end_record,
                        debug=debug,
                        fetch_sensor=fetch_sensor,
                        car_charging_slots=saved["car_charging_slots"],
                        start_soc=start_soc,
                        io_adjusted_base=io_adjusted_base,
                        rate_import_match=rate_import_match,
                        rate_export_match=rate_export_match,
                    )
                    if result_data is not None:
                        results[tariff_id] = result_data
                finally:
                    # Restore hardware settings after each tariff so overrides don't bleed into the next tariff;
                    # in a finally so a raised run_single() can't leave compare values applied to the live instance
                    for name in COMPARE_HARDWARE_STATE:
                        setattr(my_predbat, name, saved[name])
                    # Restore config: overrides after each tariff too, for the same reason - otherwise a
                    # tariff's config override stays applied to config_index for every subsequent tariff,
                    # even ones with no config block of their own (issue #4156)
                    if config_snapshot:
                        for key, orig_value in config_snapshot.items():
                            item = my_predbat.config_index.get(key)
                            if item is not None:
                                item["value"] = orig_value
                        my_predbat.fetch_config_options()
                # Save and update comparisons as we go so it is updated in HA
                self.select_best(compare_list, results)
                self.comparisons = results
                self.save_yaml()
                self.publish_data()
        finally:
            try:
                # Restore config values overridden by any tariff's fetch_config() call
                if config_snapshot:
                    for key, orig_value in config_snapshot.items():
                        item = my_predbat.config_index.get(key)
                        if item is not None:
                            item["value"] = orig_value
                    my_predbat.fetch_config_options()
            finally:
                # Restore original settings, last so they win over fetch_config_options(), and even if it raised
                for name in COMPARE_RESTORED_STATE + COMPARE_RESTORED_COPIES:
                    setattr(my_predbat, name, saved[name])
                # A re-plan requested while the comparison ran (a setting or override changed) must still happen
                my_predbat.plan_valid = saved["plan_valid"] and not my_predbat.update_pending
                my_predbat.io_adjusted = io_adjusted_base

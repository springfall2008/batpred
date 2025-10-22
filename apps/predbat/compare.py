# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import os
from datetime import datetime
from utils import dp0, dp2
import yaml
import copy

# TODO:
# 1. Update web UI to show a chart of historical differences
# 2. Allow change to start/end comparison points e.g. tomorrow or today
# 3. Consider Octopus API key to access current tariff info and switch links


class Compare:
    def __init__(self, my_predbat):
        self.pb = my_predbat
        self.log = self.pb.log
        self.config_root = self.pb.config_root
        self.dashboard_item = self.pb.dashboard_item
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

    def fetch_rates(self, tariff, rate_import_base, rate_export_base):
        pb = self.pb

        # Reset threshold to automatic
        pb.rate_low_threshold = 0
        pb.rate_high_threshold = 0

        # Reset rates to base
        pb.rate_import = copy.deepcopy(rate_import_base)
        pb.rate_export = copy.deepcopy(rate_export_base)

        # Fetch rates from Octopus Energy API
        if "rates_import_octopus_url" in tariff:
            # Fixed URL for rate import
            import_url = pb.resolve_arg("rates_import_octopus_url", tariff["rates_import_octopus_url"], indirect=False)
            pb.rate_import = pb.download_octopus_rates(pb.resolve_arg("rates_import_octopus_url", tariff["rates_import_octopus_url"], indirect=False))
        elif "metric_octopus_import" in tariff:
            # Octopus import rates
            entity_id = pb.resolve_arg("metric_octopus_import", tariff["metric_octopus_import"])
            if entity_id:
                pb.rate_import = pb.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")
            else:
                self.log("Warn: Compare tariff {} bad Octopus entity id {}".format(tariff.get("id", ""), entity_id))
        elif "metric_energidataservice_import" in tariff:
            # Octopus import rates
            entity_id = pb.resolve_arg("metric_energidataservice_import", tariff["metric_energidataservice_import"])
            if entity_id:
                pb.rate_import = pb.fetch_energidataservice_rates(entity_id, adjust_key="is_intelligent_adjusted")
            else:
                self.log("Warn: Compare tariff {} bad Energidata entity id {}".format(tariff.get("id", ""), entity_id))
        elif "rates_import" in tariff:
            pb.rate_import = pb.basic_rates(tariff["rates_import"], "rates_import")
        else:
            self.log("Using existing rate import data")

        if "rates_export_octopus_url" in tariff:
            # Fixed URL for rate export
            pb.rate_export = pb.download_octopus_rates(pb.resolve_arg("rates_export_octopus_url", tariff["rates_export_octopus_url"], indirect=False))
        elif "metric_octopus_export" in tariff:
            # Octopus export rates
            entity_id = pb.resolve_arg("metric_octopus_export", tariff["metric_octopus_export"])
            if entity_id:
                pb.rate_export = pb.fetch_octopus_rates(entity_id)
            else:
                self.log("Warn: Compare tariff {} bad Octopus entity id {}".format(tariff.get("id", ""), entity_id))
        elif "metric_energidataservice_export" in tariff:
            # Octopus import rates
            entity_id = pb.resolve_arg("metric_energidataservice_export", tariff["metric_energidataservice_export"])
            if entity_id:
                pb.rate_export = pb.fetch_energidataservice_rates(entity_id, adjust_key="is_intelligent_adjusted")
            else:
                self.log("Warn: Compare tariff {} bad Energidata entity id {}".format(tariff.get("id", ""), entity_id))
        elif "rates_export" in tariff:
            pb.rate_export = pb.basic_rates(tariff["rates_export"], "rates_export")
        else:
            self.log("Using existing rate export data")

        if pb.rate_import:
            pb.rate_scan(pb.rate_import, print=False)
            pb.rate_import, pb.rate_import_replicated = pb.rate_replicate(pb.rate_import, pb.io_adjusted, is_import=True)
            if "rates_import_override" in tariff:
                pb.rate_import = pb.basic_rates(tariff["rates_import_override"], "rates_import_override", pb.rate_import, pb.rate_import_replicated)
            pb.rate_scan(pb.rate_import, print=True)

        # Replicate and scan export rates
        if pb.rate_export:
            pb.rate_scan_export(pb.rate_export, print=False)
            pb.rate_export, pb.rate_export_replicated = pb.rate_replicate(pb.rate_export, is_import=False)
            if "rates_export_override" in tariff:
                pb.rate_export = pb.basic_rates(tariff["rates_export_override"], "rates_export_override", pb.rate_export, pb.rate_export_replicated)
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
            pb.low_rates, lowest, highest = pb.rate_scan_window(pb.rate_import, 5, pb.rate_import_cost_threshold, False)
            # Update threshold automatically
            if pb.rate_low_threshold == 0 and highest >= pb.rate_min:
                pb.rate_import_cost_threshold = highest

        # Compare to see if rates changes
        for minute in range(pb.minutes_now, pb.forecast_minutes + pb.minutes_now):
            if dp2(pb.rate_import.get(minute, 0)) != dp2(rate_import_base.get(minute, 0)):
                self.log("Compare rate import is different, minute {} changed from {} to {}".format(minute, rate_import_base.get(minute, 0), pb.rate_import.get(minute, 0)))
                return False
            if dp2(pb.rate_export.get(minute, 0)) != dp2(rate_export_base.get(minute, 0)):
                self.log("Compare rate export is different, minute {} changed from {} to {}".format(minute, rate_export_base.get(minute, 0), pb.rate_export.get(minute, 0)))
                return False
        return True

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
        # Work out value of the battery at the start end end of the period to allow for the change in SOC
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
        result_data["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_data["best"] = False

        return result_data

    def run_single(self, tariff, rate_import_base, rate_export_base, end_record, debug=False, fetch_sensor=True, car_charging_slots=[]):
        """
        Compare a single energy tariff with the current settings and report results
        """
        my_predbat = self.pb
        name = tariff.get("name", None)
        tariff_id = tariff.get("id", "")
        if not name:
            self.log("Warn: Compare tariff name not found")
            return None
        self.log("Compare Tariff: {}".format(name))
        self.fetch_config(tariff)

        if fetch_sensor:
            self.pb.fetch_sensor_data()

        # Fetch rates
        try:
            existing_tariff = self.fetch_rates(tariff, rate_import_base, rate_export_base)
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

        self.recompute_car_charging(car_charging_slots)

        self.log("Running scenario for tariff: {}".format(name))
        result_data = self.run_scenario(end_record)
        result_data["existing_tariff"] = existing_tariff
        result_data["name"] = name
        self.log("Scenario complete for tariff: {} cost {} metric {}".format(name, result_data["cost"], result_data["metric"]))
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

        self.log("Compare, best tariff: {} metric {}".format(best_selected, best_metric))

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
                        self.comparisons = data.get("comparisons", {})
                except yaml.YAMLError as exc:
                    self.log("Error loading comparisons: {}".format(exc))

        if self.comparisons:
            compare_list = self.pb.get_arg("compare_list", [])
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
                name = result.get("name", "")
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
        compare_list = self.pb.get_arg("compare_list", [])
        if not compare_list:
            return

        self.select_best(compare_list, self.comparisons)
        self.publish_data()

    def recompute_car_charging(self, car_charging_slots):
        """
        Recompute car charging plan
        """
        my_predbat = self.pb

        my_predbat.car_charging_slots = [[] for car_n in range(my_predbat.num_cars)]

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
                self.log("Re-plan car {} for charing to {}".format(car_n, my_predbat.car_charging_limit[car_n]))
                my_predbat.car_charging_plan_smart[car_n] = True
                my_predbat.car_charging_slots[car_n] = my_predbat.plan_car_charging(car_n, my_predbat.low_rates)

            if my_predbat.car_charging_planned[car_n] and my_predbat.car_charging_exclusive[car_n]:
                break

    def run_all(self, debug=False, fetch_sensor=True):
        """
        Compare a comparison in prices across multiple energy tariffs and report results
        take care not to destroy the state of the system for the primary settings
        """
        compare_list = self.pb.get_arg("compare_list", [])
        if not compare_list:
            return

        results = self.comparisons

        my_predbat = self.pb

        save_forecast_plan_hours = my_predbat.forecast_plan_hours
        save_forecast_minutes = my_predbat.forecast_minutes
        save_forecast_days = my_predbat.forecast_days
        save_manual_charge_times = my_predbat.manual_charge_times
        save_manual_export_times = my_predbat.manual_export_times
        save_manual_freeze_charge_times = my_predbat.manual_freeze_charge_times
        save_manual_freeze_export_times = my_predbat.manual_freeze_export_times
        save_manual_demand_times = my_predbat.manual_demand_times
        save_manual_all_times = my_predbat.manual_all_times
        save_charge_window_best = my_predbat.charge_window_best
        save_export_window_best = my_predbat.export_window_best
        save_export_limits_best = my_predbat.export_limits_best
        save_charge_limit_best = my_predbat.charge_limit_best
        save_charge_limit_percent_best = my_predbat.charge_limit_percent_best
        save_cost_today_sofar = my_predbat.cost_today_sofar
        save_carbon_today_sofar = my_predbat.carbon_today_sofar
        save_iboost_today = my_predbat.iboost_today
        save_import_today_now = my_predbat.import_today_now
        save_export_today_now = my_predbat.export_today_now
        save_octopus_intelligent_charging = my_predbat.octopus_intelligent_charging
        save_car_charging_plan_smart = copy.deepcopy(my_predbat.car_charging_plan_smart)
        save_car_charging_limit = copy.deepcopy(my_predbat.car_charging_limit)
        save_car_charging_soc = copy.deepcopy(my_predbat.car_charging_soc)
        save_car_charging_battery_size = copy.deepcopy(my_predbat.car_charging_battery_size)
        save_car_charging_slots = copy.deepcopy(my_predbat.car_charging_slots)

        # Final reports, cut end_record back to 24 hours to ignore the dump at end of day
        end_record = int((my_predbat.minutes_now + 24 * 60 + 29) / 30) * 30 - my_predbat.minutes_now

        # Save baseline rates
        rate_import_base = copy.deepcopy(self.pb.rate_import)
        rate_export_base = copy.deepcopy(self.pb.rate_export)

        self.log("Starting comparison of tariffs")

        for tariff in compare_list:
            result_data = self.run_single(tariff, rate_import_base, rate_export_base, end_record, debug=debug, fetch_sensor=fetch_sensor, car_charging_slots=save_car_charging_slots)
            results[tariff["id"]] = result_data
            # Save and update comparisons as we go so it is updated in HA
            self.select_best(compare_list, results)
            self.comparisons = results
            self.save_yaml()
            self.publish_data()

        # Restore original settings
        my_predbat.forecast_plan_hours = save_forecast_plan_hours
        my_predbat.forecast_minutes = save_forecast_minutes
        my_predbat.forecast_days = save_forecast_days
        my_predbat.manual_charge_times = save_manual_charge_times
        my_predbat.manual_export_times = save_manual_export_times
        my_predbat.manual_freeze_charge_times = save_manual_freeze_charge_times
        my_predbat.manual_freeze_export_times = save_manual_freeze_export_times
        my_predbat.manual_demand_times = save_manual_demand_times
        my_predbat.manual_all_times = save_manual_all_times
        my_predbat.charge_window_best = save_charge_window_best
        my_predbat.export_window_best = save_export_window_best
        my_predbat.export_limits_best = save_export_limits_best
        my_predbat.charge_limit_best = save_charge_limit_best
        my_predbat.charge_limit_percent_best = save_charge_limit_percent_best
        my_predbat.cost_today_sofar = save_cost_today_sofar
        my_predbat.carbon_today_sofar = save_carbon_today_sofar
        my_predbat.iboost_today = save_iboost_today
        my_predbat.import_today_now = save_import_today_now
        my_predbat.export_today_now = save_export_today_now
        my_predbat.octopus_intelligent_charging = save_octopus_intelligent_charging
        my_predbat.car_charging_limit = save_car_charging_limit
        my_predbat.car_charging_soc = save_car_charging_soc
        my_predbat.car_charging_battery_size = save_car_charging_battery_size
        my_predbat.car_charging_slots = save_car_charging_slots
        my_predbat.car_charging_plan_smart = save_car_charging_plan_smart

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import re
from datetime import datetime, timedelta
from config import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2
import copy

class Compare:
    def __init__(self, my_predbat):
        self.pb = my_predbat
        self.log = self.pb.log

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
            pb.rate_import = pb.download_octopus_rates(tariff["rates_import_octopus_url"])
        elif 'rates_import' in tariff:
            pb.rate_import = pb.basic_rates(tariff['rates_import'], "rates_import")
        else:
            self.log("Using existing rate import data")

        if "rates_export_octopus_url" in tariff:
            # Fixed URL for rate export
            pb.rate_export = pb.download_octopus_rates(tariff["rates_export_octopus_url"])
        elif 'rates_export' in tariff:
            pb.rate_export = pb.basic_rates(tariff['rates_export'], "rates_export")
        else:
            self.log("Using existing rate export data")

        if pb.rate_import:
            pb.rate_scan(pb.rate_import, print=False)
            pb.rate_import, pb.rate_import_replicated = pb.rate_replicate(pb.rate_import, pb.io_adjusted, is_import=True)
            if 'rates_import_override' in tariff:
                pb.rate_import = pb.basic_rates(tariff["rates_import_override"], "rates_import_override", pb.rate_import, pb.rate_import_replicated)
            pb.rate_scan(pb.rate_import, print=True)

        # Replicate and scan export rates
        if pb.rate_export:
            pb.rate_scan_export(pb.rate_export, print=False)
            pb.rate_export, pb.rate_export_replicated = pb.rate_replicate(pb.rate_export, is_import=False)
            if 'rates_export_override' in tariff:
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

    def run_scenario(self, end_record):
        my_predbat = self.pb

        pv_step = my_predbat.pv_forecast_minute_step
        pv10_step = my_predbat.pv_forecast_minute10_step
        load_step = my_predbat.load_minutes_step
        load10_step = my_predbat.load_minutes_step10

        my_predbat.calculate_plan(recompute=True, debug_mode=False, publish=False)

        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
           my_predbat.charge_limit_best, 
           my_predbat.charge_window_best, 
           my_predbat.export_window_best, 
           my_predbat.export_limits_best, 
           False, 
           end_record=end_record, 
           save="compare"
        )
        cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10 = my_predbat.run_prediction(
           my_predbat.charge_limit_best, 
           my_predbat.charge_window_best, 
           my_predbat.export_window_best, 
           my_predbat.export_limits_best, 
           True, 
           end_record=end_record, 

        )
        metric, battery_value = my_predbat.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh)
        html = my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, end_record, publish=False)

        result_data = {
            "cost": cost,
            "cost10": cost10,
            "import": import_kwh_battery + import_kwh_house,
            "import10": import_kwh_battery10 + import_kwh_house10,
            "export": export_kwh,
            "export10": export_kwh10,
            "soc": soc,
            "soc10": soc10,
            "soc_min": soc_min,
            "soc_min10": soc_min10,
            "battery_cycle": battery_cycle,
            "battery_cycle10": battery_cycle10,
            "metric": metric,
            "metric_keep": metric_keep,
            "metric_keep10": metric_keep10,
            "final_iboost": final_iboost,
            "final_iboost10": final_iboost10,
            "final_carbon_g": final_carbon_g,
            "final_carbon_g10": final_carbon_g10,
            "end_record": end_record,
        }
        for item in result_data:
            result_data[item] = dp2(result_data[item])
        result_data["html"] = html
        result_data["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return result_data

    def run_single(self, tariff, rate_import_base, rate_export_base, end_record, debug=False, fetch_sensor=True):

        """
        Compare a single energy tariff with the current settings and report results
        """
        name = tariff.get("name", None)
        if not name:
            self.log("Warn: Compare tariff name not found")
            return None
        self.log("Compare Tariff: {}".format(name))
        self.fetch_config(tariff)
        if fetch_sensor:
            self.pb.fetch_sensor_data()
        self.fetch_rates(tariff, rate_import_base, rate_export_base)
        self.log("Running scenario for tariff: {}".format(name))
        result_data = self.run_scenario(end_record)
        self.log("Scenario complete for tariff: {} cost {} metric {}".format(name, result_data["cost"], result_data["metric"]))
        if debug:
            with open("compare_{}.html".format(name), "w") as f:
                f.write(result_data['html'])
        return result_data


    def run_all(self, debug=False, fetch_sensor=True):
        """
        Compare a comparison in prices across multiple energy tariffs and report results
        take care not to destroy the state of the system for the primary settings
        """        
        compare = self.pb.get_arg("compare", [])
        if not compare:
            return

        results = {}

        my_predbat = self.pb

        save_forecast_plan_hours = my_predbat.forecast_plan_hours
        save_forecast_minutes = my_predbat.forecast_minutes
        save_forecast_days = my_predbat.forecast_days
        
        my_predbat.forecast_plan_hours = 48
        my_predbat.forecast_minutes = my_predbat.forecast_plan_hours * 60
        my_predbat.forecast_days = my_predbat.forecast_plan_hours / 24

        # Final reports, cut end_record back to 24 hours to ignore the dump at end of day
        end_record = int((my_predbat.minutes_now + 24 * 60 + 29) / 30) * 30 - my_predbat.minutes_now

        # Save baseline rates
        rate_import_base = copy.deepcopy(self.pb.rate_import)
        rate_export_base = copy.deepcopy(self.pb.rate_export)

        self.log("Starting comparison of tariffs")
        for tariff in compare:
            result_data = self.run_single(tariff, rate_import_base, rate_export_base, end_record, debug=debug, fetch_sensor=fetch_sensor)
            results[tariff["name"]] = result_data
            self.pb.comparisons = results
        
        self.pb.comparisons_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Restore original settings
        my_predbat.forecast_plan_hours = save_forecast_plan_hours
        my_predbat.forecast_minutes = save_forecast_minutes
        my_predbat.forecast_days = save_forecast_days

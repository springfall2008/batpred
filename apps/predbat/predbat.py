# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
import os
import sys
from datetime import datetime, timedelta
import traceback
import gc

# from memory_profiler import profile

IS_COMPILED = getattr(sys, "frozen", False)

IS_APPDAEMON = False

# Import AppDaemon or our standalone wrapper
try:
    import appdaemon.plugins.hass.hassapi as hass

    IS_APPDAEMON = True
except:
    import hass as hass

    IS_APPDAEMON = False

import pytz
import requests
import asyncio

THIS_VERSION = "v8.25.0"

# fmt: off
PREDBAT_FILES = ["predbat.py", "config.py", "prediction.py", "gecloud.py","utils.py", "inverter.py", "ha.py", "download.py", "unit_test.py", "web.py", "web_helper.py", "predheat.py", "futurerate.py", "octopus.py", "solcast.py","execute.py", "plan.py", "fetch.py", "output.py", "userinterface.py", "energydataservice.py", "alertfeed.py", "compare.py", "db_manager.py", "db_engine.py", "plugin_system.py", "ohme.py", "components.py"]
# fmt: on

from download import predbat_update_move, predbat_update_download, check_install

# Only do the self-install/self-update logic if we are NOT compiled.
if not IS_COMPILED:
    # Sanity check the install and re-download if corrupted
    if not check_install():
        print("Warn: Predbat files are not installed correctly, trying to download them")
        files = predbat_update_download(THIS_VERSION)
        ...
        sys.exit(1)
    else:
        print("Predbat files are installed correctly for version {}".format(THIS_VERSION))
else:
    # In compiled mode, we skip the entire self-update logic
    print("Running in compiled mode; skipping local file checks and auto-update.")

from config import (
    TIME_FORMAT,
    PREDICT_STEP,
    RUN_EVERY,
    INVERTER_TEST,
    CONFIG_ROOTS,
    CONFIG_REFRESH_PERIOD,
    CONFIG_ITEMS,
    APPS_SCHEMA,
)
from prediction import reset_prediction_globals
from utils import minutes_since_yesterday, dp1, dp2, dp3
from predheat import PredHeat
from octopus import Octopus
from energydataservice import Energidataservice
from solcast import Solcast
from gecloud import GECloud
from components import Components
from execute import Execute
from plan import Plan
from fetch import Fetch
from output import Output
from userinterface import UserInterface
from alertfeed import Alertfeed
from compare import Compare
from plugin_system import PluginSystem


class PredBat(hass.Hass, Octopus, Energidataservice, Solcast, GECloud, Alertfeed, Fetch, Plan, Execute, Output, UserInterface):
    """
    The battery prediction class itself
    """

    def download_predbat_releases_url(self, url):
        """
        Download release data from github, but use the cache for 2 hours
        """
        # Check the cache first
        now = datetime.now()
        if url in self.github_url_cache:
            stamp = self.github_url_cache[url]["stamp"]
            pdata = self.github_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (120 * 60):
                self.log("Using cached GITHub data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        try:
            r = requests.get(url)
        except Exception:
            self.log("Warn: Unable to load data from Github URL: {}".format(url))
            return []

        try:
            pdata = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Unable to decode data from Github URL: {}".format(url))
            return []

        # Save to cache
        self.github_url_cache[url] = {}
        self.github_url_cache[url]["stamp"] = now
        self.github_url_cache[url]["data"] = pdata

        return pdata

    def download_predbat_releases(self):
        """
        Download release data
        """
        global PREDBAT_UPDATE_OPTIONS
        auto_update = self.get_arg("auto_update")
        url = "https://api.github.com/repos/springfall2008/batpred/releases"
        data = self.download_predbat_releases_url(url)
        self.releases = {}
        if data and isinstance(data, list):
            found_latest = False
            found_latest_beta = False

            release = data[0]
            self.releases["this"] = THIS_VERSION
            self.releases["latest"] = "Unknown"
            self.releases["latest_beta"] = "Unknown"

            for release in data:
                if release.get("tag_name", "Unknown") == THIS_VERSION:
                    self.releases["this_name"] = release.get("name", "Unknown")
                    self.releases["this_body"] = release.get("body", "Unknown")

                if not found_latest and not release.get("prerelease", True):
                    self.releases["latest"] = release.get("tag_name", "Unknown")
                    self.releases["latest_name"] = release.get("name", "Unknown")
                    self.releases["latest_body"] = release.get("body", "Unknown")
                    found_latest = True

                if not found_latest_beta:
                    self.releases["latest_beta"] = release.get("tag_name", "Unknown")
                    self.releases["latest_beta_name"] = release.get("name", "Unknown")
                    self.releases["latest_beta_body"] = release.get("body", "Unknown")
                    found_latest_beta = True

            self.log("Predbat {} version {} currently running, latest version is {} latest beta {}".format(__file__, self.releases["this"], self.releases["latest"], self.releases["latest_beta"]))
            PREDBAT_UPDATE_OPTIONS = ["main"]
            this_tag = THIS_VERSION
            new_version = False

            # Find all versions for the dropdown menu
            for release in data:
                prerelease = release.get("prerelease", True)
                tag = release.get("tag_name", None)
                if tag:
                    if prerelease:
                        full_name = tag + " (beta) " + release.get("name", "")
                    else:
                        full_name = tag + " " + release.get("name", "")
                    PREDBAT_UPDATE_OPTIONS.append(full_name)
                    if this_tag == tag:
                        this_tag = full_name
                if len(PREDBAT_UPDATE_OPTIONS) >= 25:
                    break

            # Update the drop down menu
            item = self.config_index.get("update", None)
            if item:
                item["options"] = PREDBAT_UPDATE_OPTIONS
                item["value"] = None

            # See what version we are on and auto-update
            if this_tag not in PREDBAT_UPDATE_OPTIONS:
                this_tag = this_tag + " (?)"
                PREDBAT_UPDATE_OPTIONS.append(this_tag)
                self.log("Autoupdate: Currently on unknown version {}".format(this_tag))
            else:
                if self.releases["this"] == self.releases["latest"]:
                    self.log("Autoupdate: Currently up to date")
                elif self.releases["this"] == self.releases["latest_beta"]:
                    self.log("Autoupdate: Currently on latest beta")
                else:
                    latest_version = self.releases["latest"] + " " + self.releases["latest_name"]
                    if auto_update:
                        self.log("Autoupdate: There is an update pending {} - auto update triggered!".format(latest_version))
                        self.download_predbat_version(latest_version)
                    else:
                        self.log("Autoupdate: There is an update pending {} - auto update is off".format(latest_version))
                        new_version = True

            # Refresh the list
            self.expose_config("update", this_tag)
            self.expose_config("version", new_version, force=True)

        else:
            self.log("Warn: Unable to download Predbat version information from github, return code: {}".format(data))
            self.expose_config("version", False, force=True)

        return self.releases

    def unit_conversion(self, entity_id, state, units, required_unit, going_to=False):
        """
        Convert state to the required unit if necessary
        """

        if not required_unit:
            return state

        if not units:
            units = self.ha_interface.get_state(entity_id=entity_id, default="", attribute="unit_of_measurement")
        if not units:
            return state

        units = str(units).strip().lower()
        required_unit = str(required_unit).strip().lower()

        try:
            state = float(state)
        except (ValueError, TypeError):
            pass

        # Swap order for going to conversion
        if going_to:
            units, required_unit = required_unit, units

        if isinstance(state, float) and units and required_unit and units != required_unit:
            if units.startswith("k") and not required_unit.startswith("k"):
                # Convert kWh to Wh
                state *= 1000.0
                units = units[1:]  # Remove 'k' from units
            elif not units.startswith("k") and required_unit.startswith("k"):
                # Convert Wh to kWh
                state /= 1000.0
                required_unit = required_unit[1:]  # Remove 'k' from units
            elif units.startswith("m") and not required_unit.startswith("m"):
                # Convert mW to W
                state /= 1000.0
            elif not units.startswith("m") and required_unit.startswith("m"):
                # Convert W to mW
                state *= 1000.0

            if units != required_unit:
                self.log("Warn: unit_conversion - Units mismatch for {}: expected {}, got {} after conversion".format(entity_id, required_unit, units))
        return state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None):
        """
        Wrapper function to get state from HA
        """
        if not self.ha_interface:
            self.log("Error: get_state_wrapper - No HA interface available")
            return None

        # Entity with coded attribute
        if entity_id and "$" in entity_id:
            entity_id, attribute = entity_id.split("$")

        state = self.ha_interface.get_state(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh)
        state = self.unit_conversion(entity_id, state, None, required_unit)

        return state

    def set_state_wrapper(self, entity_id, state, attributes={}, required_unit=None):
        """
        Wrapper function to get state from HA
        """
        if not self.ha_interface:
            self.log("Error: set_state_wrapper - No HA interface available")
            return False

        state = self.unit_conversion(entity_id, state, None, required_unit, going_to=True)
        return self.ha_interface.set_state(entity_id, state, attributes=attributes)

    def call_service_wrapper(self, service, **kwargs):
        """
        Wrapper function to call a HA service
        """
        if not self.ha_interface:
            self.log("Error: call_service_wrapper - No HA interface available")
            return False
        return self.ha_interface.call_service(service, **kwargs)

    def get_services_wrapper(self):
        """
        Wrapper function to get services from HA
        """
        if not self.ha_interface:
            self.log("Error: get_services_wrapper - No HA interface available")
            return False
        return self.ha_interface.get_services()

    def get_history_wrapper(self, entity_id, days=30, required=True):
        """
        Wrapper function to get history from HA
        """
        if not self.ha_interface:
            self.log("Error: get_history_wrapper - No HA interface available")
            return None

        self.log("Getting history for {} for the last {} days".format(entity_id, days))

        history = self.ha_interface.get_history(entity_id, days=days, now=self.now_utc)
        self.log("Got history for {}".format(entity_id))

        if required and (history is None):
            self.log("Error: Failure to fetch history for {}".format(entity_id))
            raise ValueError
        else:
            return history

    def time_now_str(self):
        """
        Return time now as human string
        """
        return (self.midnight + timedelta(minutes=self.minutes_now)).strftime("%H:%M:%S")

    def time_abs_str(self, minute):
        """
        Return time absolute as human string
        """
        return (self.midnight + timedelta(minutes=minute)).strftime("%m-%d %H:%M:%S")

    def reset(self):
        """
        Init stub
        """
        reset_prediction_globals()
        self.text_plan = "Computing please wait..."
        self.prediction_cache_enable = True
        self.base_load = 0
        self.db_manager = None
        self.plan_debug = False
        self.arg_errors = {}
        self.ha_interface = None
        self.fatal_error = False
        self.components = None
        self.CONFIG_ITEMS = copy.deepcopy(CONFIG_ITEMS)
        self.comparison = None
        self.predheat = None
        self.predbat_mode = "Monitor"
        self.soc_kwh_history = {}
        self.html_plan = "<body><h1>Please wait calculating...</h1></body>"
        self.unmatched_args = {}
        self.define_service_list()
        self.stop_thread = False
        self.solcast_api_limit = None
        self.solcast_api_used = None

        # Solcast API request metrics for monitoring
        self.solcast_requests_total = 0
        self.solcast_failures_total = 0
        self.solcast_last_success_timestamp = None

        # Forecast.solar API request metrics for monitoring
        self.forecast_solar_requests_total = 0
        self.forecast_solar_failures_total = 0
        self.forecast_solar_last_success_timestamp = None
        self.currency_symbols = self.args.get("currency_symbols", "£p")
        self.pool = None
        self.watch_list = []
        self.restart_active = False
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""
        self.inverters = []
        self.manual_charge_times = []
        self.manual_export_times = []
        self.manual_freeze_charge_times = []
        self.manual_freeze_export_times = []
        self.manual_demand_times = []
        self.manual_all_times = []
        self.manual_api = []
        self.manual_import_rates = {}
        self.manual_export_rates = {}
        self.manual_load_adjust = {}
        self.config_index = {}
        self.dashboard_index = []
        self.dashboard_index_app = {}
        self.dashboard_values = {}
        self.prefix = self.args.get("prefix", "predbat")
        self.current_status = None
        self.previous_status = None
        self.had_errors = False
        self.plan_valid = False
        self.plan_last_updated = None
        self.plan_last_updated_minutes = 0
        self.plugin_system = None
        self.calculate_plan_every = 5
        self.prediction_started = False
        self.update_pending = True
        self.midnight_utc = None
        self.difference_minutes = 0
        self.minutes_to_midnight = 0
        self.days_previous = [7]
        self.days_previous_weight = [1]
        self.forecast_days = 0
        self.forecast_minutes = 0
        self.soc_kw = 0
        self.soc_percent = 0
        self.soc_max = 10.0
        self.battery_temperature = 20
        self.end_record = 24 * 60 * 2
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.predict_iboost_best = {}
        self.predict_metric_best = {}
        self.metric_min_improvement = 0.0
        self.metric_min_improvement_export = 0.1
        self.metric_min_improvement_export_freeze = 0.1
        self.metric_min_improvement_plan = 2.0
        self.metric_battery_cycle = 0.0
        self.metric_battery_value_scaling = 1.0
        self.metric_future_rate_offset_import = 0.0
        self.metric_future_rate_offset_export = 0.0
        self.metric_inday_adjust_damping = 1.0
        self.metric_standing_charge = 0.0
        self.metric_self_sufficiency = 0.0
        self.iboost_value_scaling = 1.0
        self.rate_import = {}
        self.rate_export = {}
        self.rate_gas = {}
        self.rate_slots = []
        self.low_rates = []
        self.high_export_rates = []
        self.cost_today_sofar = 0
        self.carbon_today_sofar = 0
        self.octopus_slots = []
        self.car_charging_slots = []
        self.reserve = 0
        self.reserve_current = 0
        self.battery_loss = 1.0
        self.battery_loss_discharge = 1.0
        self.inverter_loss = 1.0
        self.inverter_hybrid = True
        self.inverter_soc_reset = False
        self.inverter_set_charge_before = True
        self.best_soc_min = 0
        self.best_soc_max = 0
        self.best_soc_margin = 0
        self.best_soc_keep = 0
        self.best_soc_keep_weight = 0.5
        self.rate_min = 0
        self.rate_min_minute = 0
        self.rate_min_forward = {}
        self.rate_max = 0
        self.rate_max_minute = 0
        self.rate_export_cost_threshold = 99
        self.rate_import_cost_threshold = 99
        self.rate_best_cost_threshold_charge = None
        self.rate_best_cost_threshold_export = None
        self.rate_average = 0
        self.rate_export_min = 0
        self.rate_export_min_minute = 0
        self.rate_export_max = 0
        self.rate_export_max_minute = 0
        self.rate_export_average = 0
        self.rate_gas_min = 0
        self.rate_gas_max = 0
        self.rate_gas_average = 0
        self.rate_gas_min_minute = 0
        self.rate_gas_max_minute = 0
        self.set_soc_minutes = 5
        self.set_window_minutes = 5
        self.debug_enable = False
        self.import_today = {}
        self.import_today_now = 0
        self.export_today = {}
        self.export_today_now = 0
        self.pv_today = {}
        self.pv_today_now = 0
        self.pv_power = 0
        self.load_power = 0
        self.battery_power = 0
        self.grid_power = 0
        self.io_adjusted = {}
        self.current_charge_limit = 0.0
        self.charge_limit = []
        self.charge_limit_percent = []
        self.charge_limit_best = []
        self.charge_limit_best_percent = []
        self.charge_window = []
        self.charge_window_best = []
        self.car_charging_battery_size = [100]
        self.car_charging_limit = [100]
        self.car_charging_soc = [0]
        self.car_charging_soc_next = [None]
        self.car_charging_rate = [7.4]
        self.car_charging_loss = 1.0
        self.export_window = []
        self.export_limits = []
        self.export_limits_best = []
        self.export_window_best = []
        self.battery_rate_max_charge = 0
        self.battery_rate_max_discharge = 0
        self.battery_rate_max_charge_scaled = 0
        self.battery_rate_max_discharge_scaled = 0
        self.battery_rate_min = 0
        self.battery_rate_max_scaling = 1.0
        self.battery_rate_max_scaling_discharge = 1.0
        self.charge_rate_now = 0
        self.discharge_rate_now = 0
        self.car_charging_hold = False
        self.car_charging_manual_soc = False
        self.car_charging_threshold = 99
        self.car_charging_energy = {}
        self.octopus_intelligent_charging = False
        self.octopus_intelligent_ignore_unplugged = False
        self.octopus_intelligent_consider_full = False
        self.notify_devices = ["notify"]
        self.octopus_url_cache = {}
        self.futurerate_url_cache = {}
        self.ge_url_cache = {}
        self.github_url_cache = {}
        self.load_minutes = {}
        self.load_minutes_now = 0
        self.load_minutes_age = 0
        self.battery_capacity_nominal = False
        self.releases = {}
        self.balance_inverters_enable = False
        self.balance_inverters_charge = True
        self.balance_inverters_discharge = True
        self.balance_inverters_crosscharge = True
        self.balance_inverters_threshold_charge = 1.0
        self.balance_inverters_threshold_discharge = 1.0
        self.load_inday_adjustment = 1.0
        self.set_read_only = True
        self.metric_cloud_coverage = 0.0
        self.future_energy_rates_import = {}
        self.future_energy_rates_export = {}
        self.load_scaling_dynamic = {}
        self.battery_charge_power_curve = {}
        self.battery_charge_power_curve_auto = False
        self.battery_discharge_power_curve = {}
        self.battery_discharge_power_curve_auto = False
        self.computed_charge_curve = False
        self.computed_discharge_curve = False
        self.isCharging = False
        self.isCharging_Target = 0
        self.isExporting = False
        self.isExporting_Target = 0
        self.savings_today_predbat = 0.0
        self.savings_today_predbat_soc = 0.0
        self.savings_today_pvbat = 0.0
        self.savings_today_actual = 0.0
        self.cost_yesterday_car = 0.0
        self.cost_total_car = 0.0
        self.yesterday_load_step = {}
        self.yesterday_pv_step = {}
        self.rate_import = {}
        self.rate_import_replicated = {}
        self.rate_export = {}
        self.rate_export_replicated = {}
        self.rate_slots = []
        self.io_adjusted = {}
        self.low_rates = []
        self.high_export_rates = []
        self.octopus_slots = []
        self.cost_today_sofar = 0
        self.carbon_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.pv_today = {}
        self.load_minutes = {}
        self.load_minutes_age = 0
        self.load_forecast = {}
        self.load_forecast_array = []
        self.pv_forecast_minute = {}
        self.pv_forecast_minute10 = {}
        self.load_scaling_dynamic = {}
        self.carbon_intensity = {}
        self.carbon_history = {}
        self.carbon_enable = False
        self.iboost_enable = False
        self.iboost_gas = False
        self.iboost_solar = False
        self.iboost_solar_excess = False
        self.iboost_gas_export = False
        self.iboost_smart = False
        self.iboost_smart_min_length = 30
        self.iboost_on_export = False
        self.iboost_prevent_discharge = False
        self.iboost_smart_threshold = 0
        self.iboost_rate_threshold = 9999
        self.iboost_rate_threshold_export = 9999
        self.iboost_plan = []
        self.iboost_energy_subtract = True
        self.iboost_running = False
        self.iboost_running_full = False
        self.iboost_running_solar = False
        self.last_service_hash = {}
        self.count_inverter_writes = {}
        self.rate_slots = []
        self.io_adjusted = {}
        self.low_rates = []
        self.high_export_rates = []
        self.octopus_slots = []
        self.cost_today_sofar = 0
        self.carbon_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.pv_today = {}
        self.load_minutes = {}
        self.load_minutes_age = 0
        self.battery_temperature_charge_curve = {}
        self.battery_temperature_discharge_curve = {}
        self.battery_temperature_history = {}
        self.battery_temperature_prediction = {}
        self.alerts = []
        self.alert_active_keep = {}
        self.alert_cache = {}
        self.calculate_tweak_plan = False
        self.set_charge_low_power = False
        self.set_export_low_power = False
        self.config_root = "./"
        self.inverter_can_charge_during_export = True

        for root in CONFIG_ROOTS:
            if os.path.exists(root):
                self.config_root = root
                break
        self.config_root_p = self.config_root
        self.log("Config root is {}".format(self.config_root))

    def update_time(self, print=True):
        """
        Update the current time/date
        """
        self.local_tz = pytz.timezone(self.args.get("timezone", "Europe/London"))
        skew = self.args.get("clock_skew", 0)
        if skew:
            self.log("Warn: Clock skew is set to {} minutes".format(skew))
        self.now_utc_real = datetime.now(self.local_tz)
        now_utc = self.now_utc_real + timedelta(minutes=skew)
        now = datetime.now() + timedelta(minutes=skew)
        now = now.replace(second=0, microsecond=0, minute=(now.minute - (now.minute % PREDICT_STEP)))
        now_utc = now_utc.replace(second=0, microsecond=0, minute=(now_utc.minute - (now_utc.minute % PREDICT_STEP)))

        self.now_utc = now_utc
        self.now = now
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        self.minutes_to_midnight = 24 * 60 - self.minutes_now
        self.log("--------------- PredBat - update at {} with clock skew {} minutes, minutes now {}".format(now_utc, skew, self.minutes_now))

    # @profile
    def update_pred(self, scheduled=True):
        """
        Update the prediction state, everything is called from here right now
        """
        status_extra = ""
        self.had_errors = False
        self.dashboard_index = []

        self.update_time()
        self.save_current_config()

        # Check our version
        self.download_predbat_releases()

        if self.get_arg("template", False):
            self.log("Error: You have not completed editing the apps.yaml template, Predbat cannot run. Please comment out 'Template: True' line in apps.yaml to start Predbat running")
            self.record_status("Error: Template Configuration, remove 'Template: True' line in apps.yaml to start predbat running", had_errors=True)
            return

        self.expose_config("active", True)
        self.fetch_config_options()
        self.fetch_sensor_data()
        self.fetch_inverter_data()

        recompute = False
        if not scheduled or not self.plan_valid:
            self.log("Will recompute the plan as it is invalid")
            recompute = True
        else:
            plan_age = self.now_utc - self.plan_last_updated
            plan_age_minutes = plan_age.seconds / 60.0
            self.log("Plan was last updated on {} and is now {} minutes old".format(self.plan_last_updated, dp1(plan_age_minutes)))

        # Calculate the new plan (or re-use existing)
        recompute = self.calculate_plan(recompute=recompute)

        # Publish rate data
        self.publish_rate_and_threshold()

        # Execute the plan, re-read the inverter first if we had to calculate (as time passes during calculations)
        if recompute:
            self.fetch_inverter_data()
        status, status_extra = self.execute_plan()

        # If the plan was not updated, and the time has expired lets update it now
        if not recompute:
            plan_age = self.now_utc - self.plan_last_updated
            plan_age_minutes = plan_age.seconds / 60.0

            if (plan_age_minutes + RUN_EVERY) > self.calculate_plan_every:
                self.log("Will recompute the plan as it is now {} minutes old and will exceed the max age of {} minutes before the next run".format(dp1(plan_age_minutes), self.calculate_plan_every))
                # Calculate an updated plan, fetch the inverter data again and execute the plan
                self.calculate_plan(recompute=True)
                self.fetch_inverter_data()
                status, status_extra = self.execute_plan()
            else:
                self.log("Will not recompute the plan, it is {} minutes old and max age is {} minutes".format(dp1(plan_age_minutes), self.calculate_plan_every))

        # iBoost solar diverter model update state, only on 5 minute intervals
        if self.iboost_enable and scheduled:
            if self.iboost_energy_today:
                # If we have a realtime sensor just use that data
                self.iboost_next = self.iboost_today
            elif recompute and (self.minutes_now >= 0) and (self.minutes_now < self.calculate_plan_every):
                # Reset at midnight
                self.iboost_next = 0
            # Save next iBoost model value
            self.expose_config("iboost_today", self.iboost_next)
            self.log("iBoost model today updated to {}".format(self.iboost_next))

        # Update register writes counter
        previous_inverter_writes = self.load_previous_value_from_ha(self.prefix + ".inverter_register_writes")
        try:
            previous_inverter_writes = int(previous_inverter_writes)
        except (ValueError, TypeError):
            previous_inverter_writes = 0
        for id in self.count_inverter_writes.keys():
            previous_inverter_writes += self.count_inverter_writes[id]
            self.count_inverter_writes[id] = 0

        self.dashboard_item(
            self.prefix + ".inverter_register_writes",
            state=dp2(previous_inverter_writes),
            attributes={
                "friendly_name": "Total register writes (all inverters)",
                "state_class": "measurement",
                "unit_of_measurement": "writes",
                "icon": "mdi:counter",
            },
        )
        self.log("Total inverter register writes now {}".format(previous_inverter_writes))

        if self.calculate_savings:
            # Get current totals
            savings_total_predbat = self.load_previous_value_from_ha(self.prefix + ".savings_total_predbat")
            try:
                savings_total_predbat = float(savings_total_predbat)
            except (ValueError, TypeError):
                savings_total_predbat = 0.0

            savings_total_pvbat = self.load_previous_value_from_ha(self.prefix + ".savings_total_pvbat")
            try:
                savings_total_pvbat = float(savings_total_pvbat)
            except (ValueError, TypeError):
                savings_total_pvbat = 0.0

            savings_total_soc = self.load_previous_value_from_ha(self.prefix + ".savings_total_soc")
            try:
                savings_total_soc = float(savings_total_soc)
            except (ValueError, TypeError):
                savings_total_soc = 0.0

            savings_total_actual = self.load_previous_value_from_ha(self.prefix + ".savings_total_actual")
            try:
                savings_total_actual = float(savings_total_actual)
            except (ValueError, TypeError):
                savings_total_actual = 0.0

            if self.num_cars > 0:
                cost_total_car = self.load_previous_value_from_ha(self.prefix + ".cost_total_car")
                try:
                    cost_total_car = float(cost_total_car)
                except (ValueError, TypeError):
                    cost_total_car = 0.0
            else:
                cost_total_car = 0

            # Increment total at midnight for next day
            if (self.minutes_now >= 0) and (self.minutes_now < self.calculate_plan_every) and scheduled and recompute:
                savings_total_predbat += self.savings_today_predbat
                savings_total_pvbat += self.savings_today_pvbat
                savings_total_soc = self.savings_today_predbat_soc
                savings_total_actual += self.savings_today_actual
                cost_total_car += self.cost_yesterday_car

            self.dashboard_item(
                self.prefix + ".savings_total_predbat",
                state=dp2(savings_total_predbat),
                attributes={
                    "friendly_name": "Total Predbat savings",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "pounds": dp2(savings_total_predbat / 100.0),
                    "icon": "mdi:cash-multiple",
                },
            )
            self.dashboard_item(
                self.prefix + ".savings_total_soc",
                state=dp2(savings_total_soc),
                attributes={
                    "friendly_name": "Predbat savings, yesterday SoC",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-50",
                },
            )
            self.dashboard_item(
                self.prefix + ".savings_total_actual",
                state=dp2(savings_total_actual),
                attributes={
                    "friendly_name": "Actual total energy cost",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "pounds": dp2(savings_total_actual / 100.0),
                    "icon": "mdi:cash-multiple",
                },
            )
            self.dashboard_item(
                self.prefix + ".savings_total_pvbat",
                state=dp2(savings_total_pvbat),
                attributes={
                    "friendly_name": "Total Savings vs no PV/Battery system",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "pounds": dp2(savings_total_pvbat / 100.0),
                    "icon": "mdi:cash-multiple",
                },
            )
            if self.num_cars > 0:
                self.dashboard_item(
                    self.prefix + ".cost_total_car",
                    state=dp2(cost_total_car),
                    attributes={
                        "friendly_name": "Total car cost (approx)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "pounds": dp2(cost_total_car / 100.0),
                        "icon": "mdi:cash-multiple",
                    },
                )

        # Car SoC increment
        if scheduled:
            for car_n in range(self.num_cars):
                if (car_n == 0) and self.car_charging_manual_soc:
                    self.log("Car charging Manual SoC current is {} next is {}".format(self.car_charging_soc[car_n], self.car_charging_soc_next[car_n]))
                    if self.car_charging_soc_next[car_n] is not None:
                        self.expose_config("car_charging_manual_soc_kwh", dp3(self.car_charging_soc_next[car_n]))

        # Holiday days left countdown, subtract a day at midnight every day
        if scheduled and self.holiday_days_left > 0 and self.minutes_now < RUN_EVERY:
            self.holiday_days_left -= 1
            self.expose_config("holiday_days_left", self.holiday_days_left)
            self.log("Holiday days left is now {}".format(self.holiday_days_left))

        if self.debug_enable and not IS_APPDAEMON:
            self.create_debug_yaml()

        if self.had_errors:
            self.log("Completed run status {} with Errors reported (check log)".format(status))
        else:
            self.log("Completed run status {}".format(status))
            self.record_status(
                status,
                debug="best_charge_limit={} best_charge_window={} best_export_limit= {} best_export_window={}".format(self.charge_limit_best, self.charge_window_best, self.export_limits_best, self.export_window_best),
                notify=True,
                extra=status_extra,
            )
        self.expose_config("active", False)
        self.save_current_config()

        # Call plugin update hooks
        if self.plugin_system:
            self.plugin_system.call_hooks("on_update")

        if self.comparison:
            if (scheduled and self.minutes_now < RUN_EVERY) or self.get_arg("compare_active", False):
                # Compare tariffs either when triggered or daily at midnight
                self.expose_config("compare_active", True)
                self.comparison.run_all()
                self.expose_config("compare_active", False)
            else:
                # Otherwise just update HA sensors to prevent then expiring
                self.comparison.publish_only()
        else:
            self.expose_config("compare_active", False)

        gc.collect()

    async def async_download_predbat_version(self, version):
        """
        Sync wrapper for async download_predbat_version
        """
        return await self.run_in_executor(self.download_predbat_version, version)

    def download_predbat_version(self, version):
        """
        Download a version of Predbat from GitHub

        Args:
            version (str): The version of Predbat to download.

        Returns:
            bool: True if the download and update were successful, False otherwise.
        """
        if version == THIS_VERSION:
            self.log("Warn: Predbat update requested for the same version as we are running ({}), no update required".format(version))
            return

        self.log("Update Predbat to version {}".format(version))
        self.expose_config("version", True, force=True, in_progress=True)

        files = predbat_update_download(version)
        if files:
            # Kill the current threads
            self.log("Kill current threads before update")
            self.stop_thread = True
            if self.pool:
                self.log("Warn: Killing current threads before update...")
                try:
                    self.pool.close()
                    self.pool.join()
                except Exception as e:
                    self.log("Warn: Failed to close thread pool: {}".format(e))
                    self.log("Warn: " + traceback.format_exc())
                self.pool = None

            # Notify that we are about to update
            self.call_notify("Predbat: update to: {}".format(version))

            # Perform the update
            self.log("Perform the update.....")
            if predbat_update_move(version, files):
                self.log("Update to version {} completed".format(version))
                return True
            else:
                self.log("Warn: Predbat update failed to move Predbat version {}".format(version))
                return False
        else:
            self.log("Warn: Predbat update failed to download Predbat version {}".format(version))
        return False

    def validate_is_int(self, value):
        """
        Validate that a value is an integer
        """
        try:
            value = int(value)
        except (ValueError, TypeError):
            return False
        return True

    def validate_is_float(self, value):
        """
        Validate that a value is a float
        """
        try:
            value = float(value)
        except (ValueError, TypeError):
            return False
        return True

    def validate_is_boolean(self, value):
        """
        Validate that a value is a boolean switch
        """
        if isinstance(value, str) and value.lower() in ["on", "off"]:
            return True

        try:
            value = bool(value)
        except (ValueError, TypeError):
            return False
        return True

    def validate_config(self):
        """
        Uses APPS_SCHEMA to validate the self.args configuration read from apps.yaml
        """
        errors = 0
        self.arg_errors = {}
        for name in APPS_SCHEMA:
            spec = APPS_SCHEMA[name]
            required = spec.get("required", False)
            expected_type = spec.get("type", "string")

            # Check required
            if required and name not in self.args:
                self.log("Warn: Validation of apps.yaml found missing configuration item '{}'".format(name))

            # Check type
            if name in self.args:
                value = self.get_arg(name, indirect=False)
                expected_types = expected_type.split("|")
                allowed = spec.get("allowed", None)
                entries = spec.get("entries", None)
                required_entries = None
                matches = False
                if entries is not None:
                    if isinstance(entries, str):
                        required_entries = self.get_arg(entries, 0, indirect=False)
                    else:
                        required_entries = int(entries)

                    if isinstance(value, list):
                        if len(value) < required_entries:
                            self.log("Warn: Validation of apps.yaml found configuration item '{}' has {} entries, expected {} based on {}".format(name, len(value), required_entries, entries))
                            self.arg_errors[name] = "Invalid number of entries, expected {}".format(required_entries)
                            errors += 1
                            continue
                    elif required_entries > 1:
                        self.log("Warn: Validation of apps.yaml found configuration item '{}' is not a list but requires {} entries based on {}".format(name, required_entries, entries))
                        self.arg_errors[name] = "Invalid type, expected list"
                        errors += 1
                        continue

                for expected_type in expected_types:
                    if expected_type == "none":
                        if self.get_arg(name, indirect=False) is None:
                            matches = True
                            break
                    elif expected_type == "integer" or expected_type == "integer_list":
                        if expected_type == "integer" and isinstance(value, int):
                            value = [value]
                        elif expected_type == "integer_list":
                            value = self.get_arg(name, [], indirect=False)

                        if isinstance(value, list):
                            matches = True
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]
                            for item in value:
                                if not self.validate_is_int(item):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not an integer".format(name, item))
                                    self.arg_errors[name] = "Invalid type, expected integer item {}".format(item)
                                    errors += 1
                                    break
                                if not spec.get("zero", True) and int(item) == 0:
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' is zero".format(name))
                                    self.arg_errors[name] = "Invalid value, expected non-zero integer item {}".format(item)
                                    errors += 1
                                    break
                    elif expected_type == "float" or expected_type == "float_list":
                        if expected_type == "float" and (isinstance(value, float) or isinstance(value, int)):
                            value = [value]
                        elif expected_type == "float_list":
                            value = self.get_arg(name, [], indirect=False)

                        if isinstance(value, list):
                            matches = True
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]
                            for item in value:
                                if not self.validate_is_float(item):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not a float".format(name, item))
                                    self.arg_errors[name] = "Invalid type, expected float item {}".format(item)
                                    errors += 1
                                    break
                    elif expected_type == "string" or expected_type == "string_list":
                        if expected_type == "string" and isinstance(value, str):
                            value = [value]
                        elif expected_type == "string_list":
                            value = self.get_arg(name, [], indirect=False)

                        if isinstance(value, list):
                            matches = True
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]
                            for item in value:
                                if not isinstance(item, str):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not a string".format(name, item))
                                    self.arg_errors[name] = "Invalid type, expected string item {}".format(item)
                                    errors += 1
                                    break

                                if spec.get("empty", False) and not item:
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' is empty".format(name))
                                    self.arg_errors[name] = "Invalid value, expected non-empty string"
                                    errors += 1
                                    break

                                if allowed and item not in allowed:
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' value {} is not in allowed list {}".format(name, item, allowed))
                                    self.arg_errors[name] = "Invalid value {}, expected one of {}".format(item, allowed)
                                    errors += 1
                                    break
                    elif expected_type == "boolean" or expected_type == "boolean_list":
                        if expected_type == "boolean" and self.validate_is_boolean(value):
                            value = [value]
                        elif expected_type == "boolean_list":
                            value = self.get_arg(name, [], indirect=False)

                        if isinstance(value, list):
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]
                            matches = True
                            for item in value:
                                if not self.validate_is_boolean(item):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not a boolean".format(name, item))
                                    self.arg_errors[name] = "Invalid type, expected boolean item {}".format(item)
                                    errors += 1
                                    break
                    elif expected_type == "integer" or expected_type == "integer_list":
                        if expected_type == "integer" and isinstance(value, int):
                            value = [value]
                        elif expected_type == "integer_list":
                            value = self.get_arg(name, [], indirect=False)

                        if isinstance(value, list):
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]

                            matches = True
                            for item in value:
                                if not self.validate_is_int(item):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not an integer".format(name, item))
                                    self.arg_errors[name] = "Invalid type, expected integer item {}".format(item)
                                    errors += 1
                                    break

                                if allowed and item not in allowed:
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' value {} is not in allowed list {}".format(name, item, allowed))
                                    self.arg_errors[name] = "Invalid value {}, expected one of {}".format(item, allowed)
                                    errors += 1
                                    break

                    elif expected_type == "dict" or expected_type == "dict_list":
                        if expected_type == "dict" and isinstance(value, dict):
                            value = [value]
                        elif expected_type == "dict_list":
                            value = self.get_arg(name, [], indirect=False)
                        if isinstance(value, list):
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]

                            matches = True
                            for item in value:
                                if not isinstance(item, dict):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not an dict".format(name, item))
                                    self.arg_errors[name] = "Invalid type, element {} expected dict".format(item)
                                    errors += 1
                                    break
                    elif expected_type == "int_float_dict":
                        if isinstance(value, dict):
                            matches = True
                            for key in value:
                                if not self.validate_is_int(key):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} value {} is not a int => float".format(name, key, value))
                                    self.arg_errors[name] = "Invalid element key {} expected int".format(key)
                                    errors += 1
                                    break
                                if not self.validate_is_float(value[key]):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} value {} is not a int => float".format(name, key, value))
                                    self.arg_errors[name] = "Invalid element key {} value {}, expected int => float".format(key, value[key])
                                    errors += 1
                                    break
                    elif expected_type == "sensor_list" or expected_type == "sensor":
                        sensor_type = spec.get("sensor_type", None)

                        if expected_type == "sensor" and isinstance(value, str):
                            value = [value]
                        elif expected_type == "sensor_list":
                            value = self.get_arg(name, [], indirect=False)

                        if isinstance(value, list):
                            # Auto trim to length
                            if required_entries is not None and len(value) > required_entries:
                                value = value[:required_entries]

                            matches = True
                            for sensor in value:
                                sensor_types = []
                                if sensor_type:
                                    sensor_types = sensor_type.split("|")

                                if "action" in sensor_types and isinstance(sensor, str) and "." in sensor:
                                    # Allow action sensors
                                    continue
                                if ("integer" in sensor_types or "float" in sensor_types) and self.validate_is_int(sensor) and not spec.get("modify", False):
                                    # Allow fixed integer values
                                    continue
                                if "float" in sensor_types and self.validate_is_float(sensor) and not spec.get("modify", False):
                                    # Allow fixed float values
                                    continue
                                if "string" in sensor_types and isinstance(sensor, str) and not spec.get("modify", False) and not "." in sensor:
                                    # Allow fixed string values
                                    continue

                                if not isinstance(sensor, str):
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not a valid entity_id (must be a string)".format(name, sensor))
                                    self.arg_errors[name] = "Invalid entity_id in element {}".format(sensor)
                                    errors += 1
                                    break
                                if "." not in sensor:
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not a valid entity_id (must contain a dot)".format(name, sensor))
                                    self.arg_errors[name] = "Invalid entity_id in element {}".format(sensor)
                                    errors += 1
                                    break

                                if spec.get("modify", False):
                                    prefix = sensor.split(".")[0]
                                    if prefix not in ["switch", "select", "input_number", "number", "time", "input_number", "input_datetime"]:
                                        if sensor.startswith("sensor.predbat_"):
                                            # We can ignore predbat generated sensors as they are control placeholders
                                            pass
                                        else:
                                            self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} can not be modified".format(name, sensor))
                                            self.arg_errors[name] = "Invalid entity_id in element {}, can not be modified".format(sensor)
                                            errors += 1
                                            break

                                state = self.get_state_wrapper(sensor)
                                if state is None:
                                    self.log("Expected types {} state {}".format(sensor_types, state))
                                    if "none" in sensor_types:
                                        # Allow None values for sensors
                                        continue
                                    else:
                                        self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} returned value None".format(name, sensor))
                                        self.arg_errors[name] = "Invalid value None in element {}".format(sensor)
                                        errors += 1
                                        break

                                validated = False
                                if "float" in sensor_types and self.validate_is_float(state):
                                    validated = True
                                if "integer" in sensor_types and self.validate_is_int(state):
                                    validated = True
                                if "boolean" in sensor_types and self.validate_is_boolean(state):
                                    validated = True
                                if "dict" in sensor_types and isinstance(state, dict):
                                    validated = True
                                if "list" in sensor_types and isinstance(state, list):
                                    validated = True
                                if "string" in sensor_types and isinstance(state, str):
                                    validated = True

                                if not validated:
                                    self.log("Warn: Validation of apps.yaml found configuration item '{}' element {} is not a valid type {}".format(name, sensor, sensor_type))
                                    self.arg_errors[name] = "Invalid type in element {}, expected {}".format(sensor, sensor_type)
                                    errors += 1
                                    break

                    if matches:
                        break
                if not matches:
                    self.log("Warn: Validation of apps.yaml found configuration item '{}' is not of type '{}' value was {}".format(name, expected_type, value))
                    self.arg_errors[name] = "Invalid type, expected {}".format(expected_type)
                    errors += 1
        if errors:
            self.log("Error: Validation of apps.yaml found {} configuration errors".format(errors))
        else:
            self.log("Validation of apps.yaml was successful")

        return errors

    def is_running(self):
        """
        Check if the app is running
        """
        if self.stop_thread:
            return False
        if not self.dashboard_index:
            return False
        if self.fatal_error:
            return False

        if not self.ha_interface:
            return False

        if self.components:
            if not self.components.is_all_alive():
                return False

        # Read predbat.status
        predbat_error = self.get_state_wrapper("predbat.status", attribute="error", default=True)
        if predbat_error is None or predbat_error:
            return False
        predbat_last_updated = self.get_state_wrapper("predbat.status", attribute="last_updated", default=None)
        if predbat_last_updated is None:
            return False

        try:
            predbat_last_updated = datetime.fromisoformat(predbat_last_updated)
        except ValueError:
            return False

        # Check if the last updated time is within the last 6 minutes
        if (datetime.now() - predbat_last_updated).total_seconds() > 360:
            return False
        return True

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        self.pool = None
        if "hass_api_version" not in self.__dict__:
            self.hass_api_version = 1
        self.log("Predbat: Startup {} hass version {}".format(__name__, self.hass_api_version))
        self.update_time(print=False)
        run_every = RUN_EVERY * 60
        now = self.now

        try:
            self.reset()
            self.update_time(print=False)

            # Start all sub-components
            self.components = Components(self)
            self.components.start()

            if not self.ha_interface:
                raise ValueError("HA interface not found")

            # Initialize plugin system and discover plugins
            self.init_plugin_system()

            # Printable config root
            self.config_root_p = self.config_root
            slug = self.ha_interface.get_slug()
            if slug:
                # and use slug name to determine printable config_root pathname when writing debug info to the log file
                self.config_root_p = "/addon_configs/" + slug

            self.log("Config root is {} and printable config_root_p is now {}".format(self.config_root, self.config_root_p))

            self.ha_interface.update_states()
            self.auto_config()
            self.load_user_config(quiet=False, register=True)
            self.validate_config()
            self.comparison = Compare(self)
        except Exception as e:
            self.log("Error: Exception raised {}".format(e))
            self.log("Error: " + traceback.format_exc())
            self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
            raise e

        # Run every N minutes aligned to the minute
        seconds_now = (now - self.midnight).seconds

        # Calculate next run time to exactly align with the run_every time
        seconds_offset = seconds_now % run_every
        seconds_next = seconds_now + (run_every - seconds_offset)
        next_time = self.midnight + timedelta(seconds=seconds_next)
        self.log("Predbat: Next run time will be {} and then every {} seconds".format(next_time, run_every))

        # First run is now
        self.update_pending = True

        # And then every N minutes
        if not INVERTER_TEST:
            self.run_every(self.run_time_loop, next_time, run_every, random_start=0, random_end=0)
            self.run_every(self.update_time_loop, datetime.now(), 15, random_start=0, random_end=0)
        else:
            self.update_time_loop(None)

        # Balance inverters
        run_every_balance = self.get_arg("balance_inverters_seconds", 60)
        if run_every_balance > 0:
            self.log("Balance inverters will run every {} seconds (if enabled)".format(run_every_balance))
            seconds_offset_balance = seconds_now % run_every_balance
            seconds_next_balance = seconds_now + (run_every_balance - seconds_offset_balance) + 15  # Offset to start after Predbat update task
            next_time_balance = self.midnight + timedelta(seconds=seconds_next_balance)
            self.run_every(self.run_time_loop_balance, next_time_balance, run_every_balance, random_start=0, random_end=0)

        # Predheat
        predheat = self.args.get("predheat", {})
        if predheat:
            self.predheat = PredHeat(self)
            self.predheat.initialize()

    async def terminate(self):
        """
        Called once each time the app terminates
        """
        self.log("Predbat terminating")
        self.stop_thread = True
        if self.components:
            await self.components.stop()

        await asyncio.sleep(0)
        if hasattr(self, "pool"):
            if self.pool:
                try:
                    self.pool.close()
                    self.pool.join()
                except Exception as e:
                    self.log("Warn: Failed to close thread pool {}".format(e))
                    self.log("Warn: " + traceback.format_exc())
                self.pool = None
        self.log("Predbat terminated")

    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        if not self.ha_interface or (not self.ha_interface.websocket_active and not self.ha_interface.db_primary):
            self.log("Error: HA interface not active and db_primary is {}".format(self.ha_interface.db_primary))
            self.fatal_error = True
            raise Exception("HA interface not active")

        self.check_entity_refresh()
        if self.update_pending and not self.prediction_started:
            self.update_pending = False
            self.prediction_started = True
            self.load_user_config()
            self.validate_config()
            try:
                self.update_pred(scheduled=False)
                self.create_entity_list()
            except Exception as e:
                self.log("Error: Exception raised {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False

    def check_entity_refresh(self):
        """
        Check if we need to refresh the config entities with HA
        """
        # Check if we need to refresh the config entities with HA
        config_refresh = self.get_state_wrapper(entity_id=self.prefix + ".config_refresh")
        config_refresh_stamp = None
        if config_refresh:
            try:
                config_refresh_stamp = datetime.strptime(config_refresh, TIME_FORMAT)
            except (ValueError, TypeError):
                config_refresh_stamp = None

        age = CONFIG_REFRESH_PERIOD
        if config_refresh_stamp:
            tdiff = self.now_utc - config_refresh_stamp
            age = tdiff.seconds / 60 + tdiff.days * 60 * 24
            if age >= CONFIG_REFRESH_PERIOD:
                self.log("Info: Refresh config entities due to their age of {} minutes".format(age))
                self.update_pending = True
        else:
            self.log("Info: Refresh config entities as config_refresh state is unknown")
            self.update_pending = True

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.ha_interface or (not self.ha_interface.websocket_active and not self.ha_interface.db_primary):
            self.log("Error: HA interface not active")
            self.fatal_error = True
            raise Exception("HA interface not active")

        self.check_entity_refresh()
        if not self.prediction_started:
            was_update_pending = self.update_pending
            config_changed = False
            self.prediction_started = True
            self.update_pending = False

            if was_update_pending:
                self.ha_interface.update_states()
                self.load_user_config()
                self.validate_config()
                config_changed = True

            try:
                self.update_pred(scheduled=True)
            except Exception as e:
                self.log("Error: Exception raised {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
                raise e
            finally:
                self.prediction_started = False
            if config_changed:
                self.create_entity_list()
            self.prediction_started = False

    def run_time_loop_balance(self, cb_args):
        """
        Called every N second for balance inverters
        """
        if self.get_arg("template", False):
            return

        if not self.prediction_started and self.balance_inverters_enable and not self.set_read_only:
            try:
                self.balance_inverters()
            except Exception as e:
                self.log("Error: Exception raised {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
                raise e

    def init_plugin_system(self):
        """
        Initialize the plugin system and discover plugins
        """
        try:
            self.log("Initializing plugin system")
            self.plugin_system = PluginSystem(self)

            # Discover and load plugins
            self.plugin_system.discover_plugins()

            # Call initialization hooks
            self.plugin_system.call_hooks("on_init")

        except Exception as e:
            self.log("Warning: Failed to initialize plugin system: {}".format(e))
            self.plugin_system = None

    def register_hook(self, hook_name, callback):
        """
        Register a hook callback (convenience method for plugins)
        """
        if self.plugin_system:
            self.plugin_system.register_hook(hook_name, callback)

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
import re
import time
import math
import sys
from datetime import datetime, timedelta
import hashlib
import traceback

# Import AppDaemon or our standalone wrapper
try:
    import adbase as ad
    import appdaemon.plugins.hass.hassapi as hass
except:
    import hass as hass

import pytz
import requests
import yaml
from multiprocessing import Pool, cpu_count, set_start_method
import asyncio
import json

THIS_VERSION = "v8.8.1"

# fmt: off
PREDBAT_FILES = ["predbat.py", "config.py", "prediction.py", "gecloud.py","utils.py", "inverter.py", "ha.py", "download.py", "unit_test.py", "web.py", "predheat.py", "futurerate.py", "octopus.py", "solcast.py","execute.py", "plan.py", "fetch.py", "output.py", "userinterface.py"]
# fmt: on

from download import predbat_update_move, predbat_update_download, check_install

# Sanity check the install and re-download if corrupted
if not check_install():
    print("Warn: Predbat files are not installed correctly, trying to download them")
    files = predbat_update_download(THIS_VERSION)
    if files:
        print("Downloaded files, moving into place")
        predbat_update_move(THIS_VERSION, files)
    else:
        print("Warn: Failed to download predbat files for version {}, it may not exist or you may have network issues".format(THIS_VERSION))
    sys.exit(1)
else:
    print("Predbat files are installed correctly for version {}".format(THIS_VERSION))

from config import (
    TIME_FORMAT,
    PREDICT_STEP,
    RUN_EVERY,
    INVERTER_TEST,
    CONFIG_ROOTS,
    CONFIG_REFRESH_PERIOD,
)
from prediction import reset_prediction_globals
from utils import minutes_since_yesterday, dp1, dp2, dp3, dp4
from ha import HAInterface
from web import WebInterface
from predheat import PredHeat
from octopus import Octopus
from solcast import Solcast
from gecloud import GECloud
from execute import Execute
from plan import Plan
from fetch import Fetch
from output import Output
from userinterface import UserInterface


class PredBat(hass.Hass, Octopus, Solcast, GECloud, Fetch, Plan, Execute, Output, UserInterface):
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
                if len(PREDBAT_UPDATE_OPTIONS) >= 10:
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

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False):
        """
        Wrapper function to get state from HA
        """
        return self.ha_interface.get_state(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh)

    def set_state_wrapper(self, entity_id, state, attributes={}):
        """
        Wrapper function to get state from HA
        """
        return self.ha_interface.set_state(entity_id, state, attributes=attributes)

    def call_service_wrapper(self, service, **kwargs):
        """
        Wrapper function to call a HA service
        """
        return self.ha_interface.call_service(service, **kwargs)

    def get_services_wrapper(self):
        """
        Wrapper function to get services from HA
        """
        return self.ha_interface.get_services()

    def get_history_wrapper(self, entity_id, days=30, required=True):
        """
        Wrapper function to get history from HA
        """
        history = self.ha_interface.get_history(entity_id, days=days, now=self.now_utc)

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
        self.predheat = None
        self.soc_kwh_history = {}
        self.html_plan = "<body><h1>Please wait calculating...</h1></body>"
        self.unmatched_args = {}
        self.define_service_list()
        self.stop_thread = False
        self.solcast_api_limit = None
        self.solcast_api_used = None
        self.currency_symbols = self.args.get("currency_symbols", "£p")
        self.pool = None
        self.watch_list = []
        self.restart_active = False
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""
        self.manual_charge_times = []
        self.manual_export_times = []
        self.manual_freeze_charge_times = []
        self.manual_freeze_export_times = []
        self.manual_demand_times = []
        self.manual_all_times = []
        self.manual_api = []
        self.config_index = {}
        self.dashboard_index = []
        self.dashboard_values = {}
        self.prefix = self.args.get("prefix", "predbat")
        self.current_status = None
        self.previous_status = None
        self.had_errors = False
        self.expert_mode = False
        self.plan_valid = False
        self.plan_last_updated = None
        self.plan_last_updated_minutes = 0
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
        self.soc_max = 10.0
        self.end_record = 24 * 60 * 2
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.predict_iboost_best = {}
        self.predict_metric_best = {}
        self.metric_min_improvement = 0.0
        self.metric_min_improvement_export = 0.0
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
        self.set_soc_minutes = 30
        self.set_window_minutes = 30
        self.debug_enable = False
        self.import_today = {}
        self.import_today_now = 0
        self.export_today = {}
        self.export_today_now = 0
        self.pv_today = {}
        self.pv_today_now = 0
        self.pv_power = 0
        self.load_power = 0
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
        self.isExporting = False
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
        self.pv_forecast_minute = {}
        self.pv_forecast_minute10 = {}
        self.load_scaling_dynamic = {}
        self.carbon_intensity = {}
        self.carbon_history = {}
        self.iboost_enable = False
        self.iboost_gas = False
        self.iboost_gas_export = False
        self.iboost_smart = False
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

        self.config_root = "./"
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

    def update_pred(self, scheduled=True):
        """
        Update the prediction state, everything is called from here right now
        """
        status_extra = ""
        self.had_errors = False
        self.dashboard_index = []

        self.update_time()
        self.save_current_config()

        self.expose_config("active", True)

        # Check our version
        self.download_predbat_releases()

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

            cost_total_car = self.load_previous_value_from_ha(self.prefix + ".cost_total_car")
            try:
                cost_total_car = float(cost_total_car)
            except (ValueError, TypeError):
                cost_total_car = 0.0

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

        if self.debug_enable:
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

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        self.pool = None
        self.log("Predbat: Startup {}".format(__name__))
        self.update_time(print=False)
        run_every = RUN_EVERY * 60
        now = self.now

        try:
            self.reset()
            self.log("Starting HA interface")
            try:
                self.ha_interface = HAInterface(self)
            except ValueError as e:
                self.log("Error: Exception raised {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
                raise e
            self.web_interface = None
            self.web_interface_task = None
            self.log("Starting web interface")
            self.web_interface = WebInterface(self)
            self.web_interface_task = self.create_task(self.web_interface.start())

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
        except Exception as e:
            self.log("Error: Exception raised {}".format(e))
            self.log("Error: " + traceback.format_exc())
            self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
            raise e

        # Catch template configurations and exit
        if self.get_arg("template", False):
            self.log("Error: You still have a template configuration, please edit apps.yaml or restart AppDaemon if you just updated with HACS")
            self.record_status("Error: You still have a template configuration, please edit apps.yaml or restart AppDaemon if you just updated with HACS")

            # before terminating, create predbat dashboard for new users
            try:
                self.create_entity_list()
            except Exception as e:
                self.log("Error: Exception raised {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
                raise e

            return

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
        if self.web_interface:
            await self.web_interface.stop()

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
        self.check_entity_refresh()
        if self.update_pending and not self.prediction_started:
            self.prediction_started = True
            self.ha_interface.update_states()
            self.load_user_config()
            self.update_pending = False
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

        # Database tick
        self.ha_interface.db_tick()

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.prediction_started:
            config_changed = False
            self.prediction_started = True
            self.ha_interface.update_states()
            self.check_entity_refresh()

            if self.update_pending:
                self.load_user_config()
                config_changed = True

            self.update_pending = False
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
        if not self.prediction_started and self.balance_inverters_enable and not self.set_read_only:
            try:
                self.balance_inverters()
            except Exception as e:
                self.log("Error: Exception raised {}".format(e))
                self.log("Error: " + traceback.format_exc())
                self.record_status("Error: Exception raised {}".format(e), debug=traceback.format_exc())
                raise e

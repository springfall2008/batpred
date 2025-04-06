# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import math
from datetime import datetime, timedelta
from config import THIS_VERSION, TIME_FORMAT, PREDICT_STEP
from utils import dp0, dp2, dp3, calc_percent_limit
from prediction import Prediction


class Output:
    def publish_car_plan(self):
        """
        Publish the car charging plan
        """
        plan = []
        postfix = ""
        for car_n in range(self.num_cars):
            if car_n > 0:
                postfix = "_" + str(car_n)
            if not self.car_charging_slots[car_n]:
                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_car_charging_slot" + postfix,
                    state="off",
                    attributes={"planned": plan, "cost": None, "kWh": None, "friendly_name": "Predbat car charging slot" + postfix, "icon": "mdi:home-lightning-bolt-outline"},
                )
                self.dashboard_item(
                    self.prefix + ".car_charging_start" + postfix,
                    state="",
                    attributes={
                        "friendly_name": "Predbat car charge start time car" + postfix,
                        "timestamp": None,
                        "minutes_to": self.forecast_minutes,
                        "state_class": None,
                        "unit_of_measurement": None,
                        "device_class": "timestamp",
                        "icon": "mdi:table-clock",
                    },
                )
            else:
                window = self.car_charging_slots[car_n][0]
                if self.minutes_now >= window["start"] and self.minutes_now < window["end"]:
                    slot = True
                else:
                    slot = False

                time_format_time = "%H:%M:%S"
                car_startt = self.midnight_utc + timedelta(minutes=window["start"])
                car_start_time_str = car_startt.strftime(time_format_time)
                minutes_to = max(window["start"] - self.minutes_now, 0)
                self.dashboard_item(
                    self.prefix + ".car_charging_start" + postfix,
                    state=car_start_time_str,
                    attributes={
                        "friendly_name": "Predbat car charge start time car" + postfix,
                        "timestamp": car_startt.strftime(TIME_FORMAT),
                        "minutes_to": minutes_to,
                        "state_class": None,
                        "unit_of_measurement": None,
                        "device_class": "timestamp",
                        "icon": "mdi:table-clock",
                    },
                )

                total_kwh = 0
                total_cost = 0
                for window in self.car_charging_slots[car_n]:
                    start = self.time_abs_str(window["start"])
                    end = self.time_abs_str(window["end"])
                    kwh = dp2(window["kwh"])
                    average = dp2(window["average"])
                    cost = dp2(window["cost"])

                    show = {}
                    show["start"] = start
                    show["end"] = end
                    show["kwh"] = kwh
                    show["average"] = average
                    show["cost"] = cost
                    total_cost += cost
                    total_kwh += kwh
                    plan.append(show)

                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_car_charging_slot" + postfix,
                    state="on" if slot else "off",
                    attributes={
                        "planned": plan,
                        "cost": dp2(total_cost),
                        "kwh": dp2(total_kwh),
                        "friendly_name": "Predbat car charging slot" + postfix,
                        "icon": "mdi:home-lightning-bolt-outline",
                    },
                )

    def publish_rates_export(self):
        """
        Publish the export rates
        """
        window_str = ""
        if self.high_export_rates:
            window_n = 0
            for window in self.high_export_rates:
                rate_high_start = window["start"]
                rate_high_end = window["end"]
                rate_high_average = window["average"]
                rate_high_minutes_to_start = max(rate_high_start - self.minutes_now, 0)
                rate_high_minutes_to_end = max(rate_high_end - self.minutes_now, 0)

                if window_str:
                    window_str += ", "
                window_str += "{}: {} - {} @ {}".format(window_n, self.time_abs_str(rate_high_start), self.time_abs_str(rate_high_end), rate_high_average)

                rate_high_start_date = self.midnight_utc + timedelta(minutes=rate_high_start)
                rate_high_end_date = self.midnight_utc + timedelta(minutes=rate_high_end)

                time_format_time = "%H:%M:%S"

                if window_n == 0:
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_start",
                        state=rate_high_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next high export rate start",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                            "minutes_to": rate_high_minutes_to_start,
                            "rate": dp2(rate_high_average),
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_end",
                        state=rate_high_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_end_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next high export rate end",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                            "minutes_to": rate_high_minutes_to_end,
                            "rate": dp2(rate_high_average),
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_cost",
                        state=dp2(rate_high_average),
                        attributes={
                            "friendly_name": "Next high export rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
                    )
                    in_high_rate = self.minutes_now >= rate_high_start and self.minutes_now <= rate_high_end
                    self.dashboard_item(
                        "binary_sensor." + self.prefix + "_high_rate_export_slot",
                        state="on" if in_high_rate else "off",
                        attributes={"friendly_name": "Predbat high rate slot", "icon": "mdi:home-lightning-bolt-outline"},
                    )
                    high_rate_minutes = (rate_high_end - self.minutes_now) if in_high_rate else (rate_high_end - rate_high_start)
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_duration",
                        state=high_rate_minutes,
                        attributes={"friendly_name": "Next high export rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
                    )
                if window_n == 1:
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_start_2",
                        state=rate_high_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 high export rate start",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                            "rate": dp2(rate_high_average),
                            "minutes_to": rate_high_minutes_to_start,
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_end_2",
                        state=rate_high_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_high_end_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 high export rate end",
                            "state_class": "timestamp",
                            "icon": "mdi:table-clock",
                            "rate": dp2(rate_high_average),
                            "minutes_to": rate_high_minutes_to_end,
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".high_rate_export_cost_2",
                        state=dp2(rate_high_average),
                        attributes={
                            "friendly_name": "Next+1 high export rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
                    )
                window_n += 1

        if window_str:
            self.log("High export rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.high_export_rates:
            self.log("No high rate period found")
            self.dashboard_item(
                self.prefix + ".high_rate_export_start",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next high export rate start",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_end",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next high export rate end",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_cost",
                state=dp2(self.rate_export_average),
                attributes={
                    "friendly_name": "Next high export rate cost",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
            self.dashboard_item(
                "binary_sensor." + self.prefix + "_high_rate_export_slot",
                state="off",
                attributes={"friendly_name": "Predbat high export rate slot", "icon": "mdi:home-lightning-bolt-outline"},
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_duration",
                state=0,
                attributes={"friendly_name": "Next high export rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
            )
        if len(self.high_export_rates) < 2:
            self.dashboard_item(
                self.prefix + ".high_rate_export_start_2",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 high export rate start",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_end_2",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 high export rate end",
                    "device_class": "timestamp",
                    "icon": "mdi:table-clock",
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                },
            )
            self.dashboard_item(
                self.prefix + ".high_rate_export_cost_2",
                state=dp2(self.rate_export_average),
                attributes={
                    "friendly_name": "Next+1 high export rate cost",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )

    def publish_rates_import(self):
        """
        Publish the import rates
        """
        window_str = ""
        # Output rate info
        if self.low_rates:
            window_n = 0
            for window in self.low_rates:
                rate_low_start = window["start"]
                rate_low_end = window["end"]
                rate_low_average = window["average"]
                rate_low_minutes_to_start = max(rate_low_start - self.minutes_now, 0)
                rate_low_minutes_to_end = max(rate_low_end - self.minutes_now, 0)

                if window_str:
                    window_str += ", "
                window_str += "{}: {} - {} @ {}".format(window_n, self.time_abs_str(rate_low_start), self.time_abs_str(rate_low_end), rate_low_average)

                rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
                rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)

                time_format_time = "%H:%M:%S"
                if window_n == 0:
                    self.dashboard_item(
                        self.prefix + ".low_rate_start",
                        state=rate_low_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next low rate start",
                            "minutes_to": rate_low_minutes_to_start,
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": dp2(rate_low_average),
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_end",
                        state=rate_low_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_end_date.strftime(TIME_FORMAT),
                            "minutes_to": rate_low_minutes_to_end,
                            "friendly_name": "Next low rate end",
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": dp2(rate_low_average),
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_cost",
                        state=dp2(rate_low_average),
                        attributes={
                            "friendly_name": "Next low rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
                    )
                    in_low_rate = self.minutes_now >= rate_low_start and self.minutes_now <= rate_low_end
                    self.dashboard_item(
                        "binary_sensor." + self.prefix + "_low_rate_slot",
                        state="on" if in_low_rate else "off",
                        attributes={"friendly_name": "Predbat low rate slot", "icon": "mdi:home-lightning-bolt-outline"},
                    )
                    low_rate_minutes = (rate_low_end - self.minutes_now) if in_low_rate else (rate_low_end - rate_low_start)
                    self.dashboard_item(
                        self.prefix + ".low_rate_duration",
                        state=low_rate_minutes,
                        attributes={"friendly_name": "Next low rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
                    )
                if window_n == 1:
                    self.dashboard_item(
                        self.prefix + ".low_rate_start_2",
                        state=rate_low_start_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_start_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 low rate start",
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": dp2(rate_low_average),
                            "minutes_to": rate_low_minutes_to_start,
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_end_2",
                        state=rate_low_end_date.strftime(time_format_time),
                        attributes={
                            "date": rate_low_end_date.strftime(TIME_FORMAT),
                            "friendly_name": "Next+1 low rate end",
                            "device_class": "timestamp",
                            "state_class": None,
                            "rate": dp2(rate_low_average),
                            "minutes_to": rate_low_minutes_to_end,
                            "icon": "mdi:table-clock",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".low_rate_cost_2",
                        state=rate_low_average,
                        attributes={
                            "friendly_name": "Next+1 low rate cost",
                            "state_class": "measurement",
                            "unit_of_measurement": self.currency_symbols[1],
                            "icon": "mdi:currency-usd",
                        },
                    )
                window_n += 1

        self.log("Low import rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.low_rates:
            self.log("No low rate period found")
            self.dashboard_item(
                self.prefix + ".low_rate_start",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next low rate start",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_end",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next low rate end",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_cost",
                state=self.rate_average,
                attributes={"friendly_name": "Next low rate cost", "state_class": "measurement", "unit_of_measurement": self.currency_symbols[1], "icon": "mdi:currency-usd"},
            )
            self.dashboard_item(
                self.prefix + ".low_rate_duration",
                state=0,
                attributes={"friendly_name": "Next low rate duration", "state_class": "measurement", "unit_of_measurement": "minutes", "icon": "mdi:table-clock"},
            )
            self.dashboard_item("binary_sensor." + self.prefix + "_low_rate_slot", state="off", attributes={"friendly_name": "Predbat low rate slot", "icon": "mdi:home-lightning-bolt-outline"})
        if len(self.low_rates) < 2:
            self.dashboard_item(
                self.prefix + ".low_rate_start_2",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 low rate start",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_end_2",
                state="undefined",
                attributes={
                    "date": None,
                    "friendly_name": "Next+1 low rate end",
                    "device_class": "timestamp",
                    "state_class": None,
                    "minutes_to": self.forecast_minutes,
                    "rate": None,
                    "icon": "mdi:table-clock",
                },
            )
            self.dashboard_item(
                self.prefix + ".low_rate_cost_2",
                state=self.rate_average,
                attributes={"friendly_name": "Next+1 low rate cost", "state_class": "measurement", "unit_of_measurement": self.currency_symbols[1], "icon": "mdi:currency-usd"},
            )

    def adjust_symbol(self, adjust_type):
        """
        Returns an HTML symbol based on the adjust rate type.

        Parameters:
        - adjust_type (str): The type of adjustment.

        Returns:
        - symbol (str): The symbol corresponding to the adjust_type.
        """
        symbol = ""
        if adjust_type:
            if adjust_type == "offset":
                symbol = "? &#8518;"
            elif adjust_type == "future":
                symbol = "? &#x2696;"
            elif adjust_type == "user":
                symbol = "&#61;"
            elif adjust_type == "increment":
                symbol = "&#177;"
            elif adjust_type == "saving":
                symbol = "&dollar;"
            else:
                symbol = "?"
        return symbol

    def get_html_plan_header(self, plan_debug):
        """
        Returns the header row for the HTML plan.
        """
        html = ""
        html += "<tr>"
        html += "<td><b>Time</b></td>"
        if plan_debug:
            html += "<td><b>Import {} (w/loss)</b></td>".format(self.currency_symbols[1])
            html += "<td><b>Export {} (w/loss)</b></td>".format(self.currency_symbols[1])
        else:
            html += "<td><b>Import {}</b></td>".format(self.currency_symbols[1])
            html += "<td><b>Export {}</b></td>".format(self.currency_symbols[1])
        html += "<td><b>State</b></td><td></td>"  # state can potentially be two cells for charging and exporting in the same slot
        html += "<td><b>Limit %</b></td>"
        if plan_debug:
            html += "<td><b>PV kWh (10%)</b></td>"
            html += "<td><b>Load kWh (10%)</b></td>"
            html += "<td><b>Clip kWh</b></td>"
        else:
            html += "<td><b>PV kWh</b></td>"
            html += "<td><b>Load kWh</b></td>"
        if plan_debug and self.load_forecast:
            html += "<td><b>XLoad kWh</b></td>"
        if self.num_cars > 0:
            html += "<td><b>Car kWh</b></td>"
        if self.iboost_enable:
            html += "<td><b>iBoost kWh</b></td>"
        html += "<td><b>SoC %</b></td>"
        html += "<td><b>Cost</b></td>"
        html += "<td><b>Total</b></td>"
        if self.carbon_enable:
            html += "<td><b>CO2 g/kWh</b></td>"
            html += "<td><b>CO2 kg</b></td>"
        html += "</tr>"
        return html

    def publish_html_plan(self, pv_forecast_minute_step, pv_forecast_minute_step10, load_minutes_step, load_minutes_step10, end_record, publish=True):
        """
        Publish the current plan in HTML format
        """
        plan_debug = self.plan_debug
        mode = self.predbat_mode
        if self.set_read_only:
            mode += " (read only)"
        if self.debug_enable:
            mode += " (debug)"
        html = "<table>"
        html += "<tr>"
        html += "<td colspan=10> Plan starts: {} last updated: {} version: {} previous status: {} mode: {}</td>".format(self.now_utc.strftime("%Y-%m-%d %H:%M"), self.now_utc_real.strftime("%H:%M:%S"), THIS_VERSION, self.current_status, mode)
        config_str = f"best_soc_min {self.best_soc_min} best_soc_max {self.best_soc_max} best_soc_keep {self.best_soc_keep} carbon_metric {self.carbon_metric} metric_self_sufficiency {self.metric_self_sufficiency}"
        html += "</tr><tr>"
        html += "<td colspan=10> {}</td>".format(config_str)
        html += "</tr>"
        html += self.get_html_plan_header(plan_debug)
        minute_now_align = int(self.minutes_now / 30) * 30
        end_plan = min(end_record, self.forecast_minutes) + minute_now_align
        rowspan = 0
        in_span = False
        start_span = False
        for minute in range(minute_now_align, end_plan, 30):
            minute_relative = minute - self.minutes_now
            minute_relative_start = max(minute_relative, 0)
            minute_start = minute_relative_start + self.minutes_now
            minute_relative_end = minute_relative + 30
            minute_end = minute_relative_end + self.minutes_now
            minute_relative_slot_end = minute_relative_end
            minute_timestamp = self.midnight_utc + timedelta(minutes=(minute_relative_start + self.minutes_now))
            rate_start = minute_timestamp
            rate_value_import = dp2(self.rate_import.get(minute, 0))
            rate_value_export = dp2(self.rate_export.get(minute, 0))
            charge_window_n = -1
            export_window_n = -1
            in_alert = True if self.alert_active_keep.get(minute, 0) > 0 else False

            import_cost_threshold = self.rate_import_cost_threshold
            export_cost_threshold = self.rate_export_cost_threshold

            if self.rate_best_cost_threshold_charge:
                import_cost_threshold = self.rate_best_cost_threshold_charge
            if self.rate_best_cost_threshold_export:
                export_cost_threshold = self.rate_best_cost_threshold_export

            show_limit = ""

            for try_minute in range(minute_start, minute_end, PREDICT_STEP):
                charge_window_n = self.in_charge_window(self.charge_window_best, try_minute)
                if charge_window_n >= 0 and self.charge_limit_best[charge_window_n] == 0:
                    charge_window_n = -1
                if charge_window_n >= 0:
                    break

            for try_minute in range(minute_start, minute_end, PREDICT_STEP):
                export_window_n = self.in_charge_window(self.export_window_best, try_minute)
                if export_window_n >= 0 and self.export_limits_best[export_window_n] == 100:
                    export_window_n = -1
                if export_window_n >= 0:
                    break

            start_span = False
            if in_span:
                rowspan = max(rowspan - 1, 0)
                if rowspan == 0:
                    in_span = False

            if charge_window_n >= 0 and not in_span:
                charge_end_minute = self.charge_window_best[charge_window_n]["end"]
                discharge_intersect = -1
                for try_minute in range(minute_start, charge_end_minute, PREDICT_STEP):
                    discharge_intersect = self.in_charge_window(self.export_window_best, try_minute)
                    if discharge_intersect >= 0 and self.export_limits_best[discharge_intersect] == 100:
                        discharge_intersect = -1
                    if discharge_intersect >= 0:
                        break
                if discharge_intersect >= 0:
                    charge_end_minute = min(charge_end_minute, self.export_window_best[discharge_intersect]["start"])

                rowspan = int((charge_end_minute - minute) / 30)
                if rowspan > 1 and (export_window_n < 0):
                    in_span = True
                    start_span = True
                    minute_relative_end = self.charge_window_best[charge_window_n]["end"] - minute_now_align
                else:
                    rowspan = 0

            if export_window_n >= 0 and not in_span:
                export_end_minute = self.export_window_best[export_window_n]["end"]
                rowspan = int((export_end_minute - minute) / 30)
                start = self.export_window_best[export_window_n]["start"]
                if start <= minute and rowspan > 1 and (charge_window_n < 0):
                    in_span = True
                    start_span = True
                    minute_relative_end = self.export_window_best[export_window_n]["end"] - minute_now_align
                else:
                    rowspan = 0

            pv_forecast = 0
            load_forecast = 0
            pv_forecast10 = 0
            load_forecast10 = 0
            extra_forecast_array = [0 for i in range(len(self.load_forecast_array))]
            for offset in range(minute_relative_start, minute_relative_slot_end, PREDICT_STEP):
                pv_forecast += pv_forecast_minute_step.get(offset, 0.0)
                load_forecast += load_minutes_step.get(offset, 0.0)
                pv_forecast10 += pv_forecast_minute_step10.get(offset, 0.0)
                load_forecast10 += load_minutes_step10.get(offset, 0.0)
                id = 0
                for xload in self.load_forecast_array:
                    for step in range(PREDICT_STEP):
                        extra_forecast_array[id] += self.get_from_incrementing(xload, offset + self.minutes_now + step, backwards=False)
                    id += 1

            pv_forecast = dp2(pv_forecast)
            load_forecast = dp2(load_forecast)
            pv_forecast10 = dp2(pv_forecast10)
            load_forecast10 = dp2(load_forecast10)

            extra_forecast = ""
            extra_forecast_total = 0
            for value in extra_forecast_array:
                if extra_forecast:
                    extra_forecast += ", "
                extra_forecast += str(dp2(value))
                extra_forecast_total += value

            soc_percent = calc_percent_limit(self.predict_soc_best.get(minute_relative_start, 0.0), self.soc_max)
            soc_percent_end = calc_percent_limit(self.predict_soc_best.get(minute_relative_slot_end, 0.0), self.soc_max)
            soc_percent_end_window = calc_percent_limit(self.predict_soc_best.get(minute_relative_end, 0.0), self.soc_max)
            soc_min = self.soc_max
            soc_max = 0
            for minute_check in range(minute_relative_start, minute_relative_end + PREDICT_STEP, PREDICT_STEP):
                soc_min = min(self.predict_soc_best.get(minute_check, 0), soc_min)
                soc_max = max(self.predict_soc_best.get(minute_check, 0), soc_max)
            soc_percent_min = calc_percent_limit(soc_min, self.soc_max)
            soc_percent_max = calc_percent_limit(soc_max, self.soc_max)
            soc_min_window = self.soc_max
            soc_max_window = 0
            for minute_check in range(minute_relative_start, minute_relative_end + PREDICT_STEP, PREDICT_STEP):
                soc_min_window = min(self.predict_soc_best.get(minute_check, 0), soc_min_window)
                soc_max_window = max(self.predict_soc_best.get(minute_check, 0), soc_max_window)
            soc_percent_min_window = calc_percent_limit(soc_min_window, self.soc_max)
            soc_percent_max_window = calc_percent_limit(soc_max_window, self.soc_max)

            soc_change = self.predict_soc_best.get(minute_relative_slot_end, 0.0) - self.predict_soc_best.get(minute_relative_start, 0.0)
            metric_start = self.predict_metric_best.get(minute_relative_start, 0.0)
            metric_end = self.predict_metric_best.get(minute_relative_slot_end, metric_start)
            metric_change = metric_end - metric_start

            soc_sym = ""
            if abs(soc_change) < 0.05:
                soc_sym = "&rarr;"
            elif soc_change >= 0:
                soc_sym = "&nearr;"
            else:
                soc_sym = "&searr;"

            state = soc_sym
            state_color = "#FFFFFF"
            if minute in self.manual_demand_times:
                state += " &#8526;"

            pv_color = "#BCBCBC"
            load_color = "#FFFFFF"
            extra_color = "#FFFFFF"
            rate_color_import = "#FFFFAA"
            rate_color_export = "#FFFFFF"
            soc_color = "#3AEE85"
            pv_symbol = ""
            split = False

            if soc_percent < 20.0:
                soc_color = "#F18261"
            elif soc_percent < 50.0:
                soc_color = "#FFFF00"

            if pv_forecast >= 0.2:
                pv_color = "#FFAAAA"
                pv_symbol = "&#9728;"
            elif pv_forecast >= 0.1:
                pv_color = "#FFFF00"
                pv_symbol = "&#9728;"
            elif pv_forecast == 0.0:
                pv_forecast = ""

            pv_forecast = str(pv_forecast)
            if plan_debug and pv_forecast10 > 0.0:
                pv_forecast += " (%s)" % (str(pv_forecast10))

            if load_forecast >= 0.5:
                load_color = "#F18261"
            elif load_forecast >= 0.25:
                load_color = "#FFFF00"
            elif load_forecast > 0.0:
                load_color = "#AAFFAA"

            if extra_forecast_total >= 0.5:
                extra_color = "#F18261"
            elif extra_forecast_total >= 0.25:
                extra_color = "#FFFF00"
            elif extra_forecast_total > 0.0:
                extra_color = "#AAFFAA"

            load_forecast = str(load_forecast)

            if plan_debug and load_forecast10 > 0.0:
                load_forecast += " (%s)" % (str(load_forecast10))

            if rate_value_import <= 0:  # colour the import rate, blue for negative, then green, yellow and red
                rate_color_import = "#74C1FF"
            elif rate_value_import <= import_cost_threshold:
                rate_color_import = "#3AEE85"
            elif rate_value_import > (import_cost_threshold * 1.5):
                rate_color_import = "#F18261"

            if rate_value_export >= (1.5 * export_cost_threshold):
                rate_color_export = "#F18261"
            elif rate_value_export >= export_cost_threshold:
                rate_color_export = "#FFFFAA"

            had_state = False

            if charge_window_n >= 0:
                limit = self.charge_limit_best[charge_window_n]
                target = limit
                if "target" in self.charge_window_best[charge_window_n]:
                    target = self.charge_window_best[charge_window_n]["target"]

                limit_percent = calc_percent_limit(target, self.soc_max)
                if limit > 0.0:
                    if limit == self.reserve:
                        state = "FrzChrg&rarr;"
                        state_color = "#EEEEEE"
                        limit_percent = soc_percent
                    elif limit_percent <= soc_percent_min_window:
                        state = "HoldChrg&rarr;"
                        state_color = "#34DBEB"
                    else:
                        state = "Chrg&nearr;"
                        state_color = "#3AEE85"

                    if self.charge_window_best[charge_window_n]["start"] in self.manual_charge_times:
                        state += " &#8526;"
                    elif self.charge_window_best[charge_window_n]["start"] in self.manual_freeze_charge_times:
                        state += " &#8526;"
                    show_limit = str(limit_percent)
                    had_state = True
                    if plan_debug:
                        show_limit += " ({})".format(str(calc_percent_limit(limit, self.soc_max)))
            else:
                if export_window_n >= 0:
                    start = self.export_window_best[export_window_n]["start"]
                    if start > minute:
                        soc_change_this = self.predict_soc_best.get(max(start - self.minutes_now, 0), 0.0) - self.predict_soc_best.get(minute_relative_start, 0.0)
                        if soc_change_this >= 0:
                            state = " &nearr;"
                        elif soc_change_this < 0:
                            state = " &searr;"
                        else:
                            state = " &rarr;"
                        state_color = "#FFFFFF"
                        show_limit = ""
                        had_state = True

            if export_window_n >= 0:
                limit = self.export_limits_best[export_window_n]
                target = limit
                if "target" in self.export_window_best[export_window_n]:
                    target = self.export_window_best[export_window_n]["target"]

                if limit == 99:  # freeze exporting
                    if not had_state:
                        state = ""
                    if state:
                        state += "</td><td bgcolor=#AAAAAA>"  # charging and freeze exporting in same slot, split the state into two
                        split = True
                    else:
                        state_color = "#AAAAAA"
                    state += "FrzExp&rarr;"
                    show_limit = ""  # suppress displaying the limit (of 99) when freeze exporting as its a meaningless number
                elif limit < 100:
                    if not had_state:
                        state = ""
                    if state:
                        state += "</td><td bgcolor=#FFFF00>"  # charging and exporting in the same slot, split the state into two
                        split = True
                    else:
                        state_color = "#FFFF00"
                    if limit > soc_percent_max_window:
                        state += "HoldExp&searr;"
                    else:
                        state += "Exp&searr;"
                    show_limit = str(dp2(target))

                    if limit > int(limit):
                        # Snail symbol
                        state += "&#x1F40C;"

                    if plan_debug:
                        show_limit += " ({})".format(dp2(limit))

                if self.export_window_best[export_window_n]["start"] in self.manual_export_times:
                    state += " &#8526;"
                elif self.export_window_best[export_window_n]["start"] in self.manual_freeze_export_times:
                    state += " &#8526;"

            # Alert
            if in_alert:
                state = "&#9888;" + state

            # Import and export rates -> to string
            adjust_type = self.rate_import_replicated.get(minute, None)
            adjust_symbol = self.adjust_symbol(adjust_type)
            if adjust_symbol:
                rate_str_import = "<i>%02.02f %s</i>" % (rate_value_import, adjust_symbol)
            else:
                rate_str_import = "%02.02f" % (rate_value_import)

            if plan_debug:
                rate_str_import += " (%02.02f)" % (rate_value_import / self.battery_loss / self.inverter_loss + self.metric_battery_cycle)

            if charge_window_n >= 0:
                rate_str_import = "<b>" + rate_str_import + "</b>"

            adjust_type = self.rate_export_replicated.get(minute, None)
            adjust_symbol = self.adjust_symbol(adjust_type)
            if adjust_symbol:
                rate_str_export = "<i>%02.02f %s</i>" % (rate_value_export, adjust_symbol)
            else:
                rate_str_export = "%02.02f" % (rate_value_export)

            if plan_debug:
                rate_str_export += " (%02.02f)" % (rate_value_export * self.battery_loss_discharge * self.inverter_loss - self.metric_battery_cycle)

            if export_window_n >= 0:
                rate_str_export = "<b>" + rate_str_export + "</b>"

            # Total cost at start of slot, add leading minus if negative
            if metric_start >= 0:
                total_str = self.currency_symbols[0] + "%02.02f" % (metric_start / 100.0)
            else:
                total_str = "-" + self.currency_symbols[0] + "%02.02f" % (abs(metric_start) / 100.0)

            # Cost predicted for this slot
            if metric_change >= 10.0:
                cost_str = "+%d %s " % (int(metric_change), self.currency_symbols[1])
                cost_str += " &nearr;"
                cost_color = "#F18261"
            elif metric_change >= 0.5:
                cost_str = "+%d %s " % (int(metric_change), self.currency_symbols[1])
                cost_str += " &nearr;"
                cost_color = "#FFFF00"
            elif metric_change <= -0.5:
                cost_str = "-%d %s " % (int(abs(metric_change)), self.currency_symbols[1])
                cost_str += " &searr;"
                cost_color = "#3AEE85"
            else:
                cost_str = " &rarr;"
                cost_color = "#FFFFFF"

            # Car charging?
            if self.num_cars > 0:
                car_charging_kwh = self.car_charge_slot_kwh(minute_start, minute_end)
                if car_charging_kwh > 0.0:
                    car_charging_str = str(car_charging_kwh)
                    car_color = "FFFF00"
                else:
                    car_charging_str = ""
                    car_color = "#FFFFFF"

            # iBoost
            iboost_amount_str = ""
            iboost_color = "#FFFFFF"
            if self.iboost_enable:
                iboost_slot_end = minute_relative_slot_end
                iboost_amount = self.predict_iboost_best.get(minute_relative_start, 0)
                iboost_amount_end = self.predict_iboost_best.get(minute_relative_slot_end, 0)
                iboost_amount_prev = self.predict_iboost_best.get(minute_relative_slot_end - PREDICT_STEP, 0)
                if iboost_amount_prev > iboost_amount_end:
                    # Reset condition, scale to full slot size as last 5 minutes is missing in data
                    divide_by = max(minute_relative_slot_end - PREDICT_STEP - minute_relative_start, PREDICT_STEP)
                    iboost_change = (iboost_amount_prev - iboost_amount) * (minute_relative_slot_end - minute_relative_start) / divide_by
                else:
                    iboost_change = max(iboost_amount_end - iboost_amount, 0.0)
                if iboost_change > 0:
                    iboost_color = "#FFFF00"
                    iboost_amount_str = str(dp2(iboost_amount)) + " (+" + str(dp2(iboost_change)) + ")"
                else:
                    if iboost_amount > 0:
                        iboost_amount_str = str(dp2(iboost_amount))

            if self.carbon_enable:
                # Work out carbon intensity and carbon use
                carbon_amount = self.predict_carbon_best.get(minute_relative_start, 0)
                carbon_amount_end = self.predict_carbon_best.get(minute_relative_slot_end, carbon_amount)
                carbon_change = carbon_amount_end - carbon_amount
                carbon_change = dp2(carbon_change)
                carbon_intensity = dp0(self.carbon_intensity.get(minute_relative_start, 0))

                if carbon_intensity >= 450:
                    carbon_intensity_color = "#8B0000"
                elif carbon_intensity >= 290:
                    carbon_intensity_color = "#FF0000"
                elif carbon_intensity >= 200:
                    carbon_intensity_color = "#FFA500"
                elif carbon_intensity >= 120:
                    carbon_intensity_color = "#FFFF00"
                elif carbon_intensity >= 40:
                    carbon_intensity_color = "#90EE90"
                else:
                    carbon_intensity_color = "#00FF00"

                carbon_str = str(dp2(carbon_amount / 1000.0))
                if carbon_change >= 10:
                    carbon_str += " &nearr;"
                    carbon_color = "#FFAA00"
                elif carbon_change <= -10:
                    carbon_str += " &searr;"
                    carbon_color = "#00FF00"
                else:
                    carbon_str += " &rarr;"
                    carbon_color = "#FFFFFF"

            # Work out clipped
            clipped_amount = self.predict_clipped_best.get(minute_relative_start, 0)
            clipped_amount_end = self.predict_clipped_best.get(minute_relative_slot_end, clipped_amount)
            clipped_change = clipped_amount_end - clipped_amount
            clipped_change = dp2(clipped_change)
            clipped_str = str(clipped_change)
            clipped_color = "#FFFFFF"
            if clipped_change >= 0.1:
                clipped_color = "#FFAA00"
            elif clipped_change >= 0.01:
                clipped_color = "#FFFF00"

            # Table row
            html += '<tr style="color:black">'
            html += "<td bgcolor=#FFFFFF>" + rate_start.strftime("%a %H:%M") + "</td>"
            html += "<td bgcolor=" + rate_color_import + ">" + str(rate_str_import) + " </td>"
            html += "<td bgcolor=" + rate_color_export + ">" + str(rate_str_export) + " </td>"
            if start_span:
                if split:  # for slots that are both charging and exporting, just output the (split cell) state
                    html += "<td "
                else:  # otherwise (non-split slots), display the state spanning over two cells
                    html += "<td colspan=2 "
                html += "rowspan=" + str(rowspan) + " bgcolor=" + state_color + ">" + state + "</td>"
                html += "<td rowspan=" + str(rowspan) + " bgcolor=#FFFFFF> " + show_limit + "</td>"
            elif not in_span:
                if split:
                    html += "<td "
                else:
                    html += "<td colspan=2 "
                html += "bgcolor=" + state_color + ">" + state + "</td>"
                html += "<td bgcolor=#FFFFFF> " + show_limit + "</td>"
            html += "<td bgcolor=" + pv_color + ">" + str(pv_forecast) + pv_symbol + "</td>"
            html += "<td bgcolor=" + load_color + ">" + str(load_forecast) + "</td>"
            if plan_debug:
                html += "<td bgcolor=" + clipped_color + ">" + clipped_str + "</td>"
            if plan_debug and self.load_forecast:
                html += "<td bgcolor=" + extra_color + ">" + str(extra_forecast) + "</td>"
            if self.num_cars > 0:  # Don't display car charging data if there's no car
                html += "<td bgcolor=" + car_color + ">" + car_charging_str + "</td>"
            if self.iboost_enable:
                html += "<td bgcolor=" + iboost_color + ">" + iboost_amount_str + " </td>"
            html += "<td bgcolor=" + soc_color + ">" + str(soc_percent) + soc_sym + "</td>"
            html += "<td bgcolor=" + cost_color + ">" + str(cost_str) + "</td>"
            html += "<td bgcolor=#FFFFFF>" + str(total_str) + "</td>"
            if self.carbon_enable:
                html += "<td bgcolor=" + carbon_intensity_color + ">" + str(carbon_intensity) + " </td>"
                html += "<td bgcolor=" + carbon_color + "> " + str(carbon_str) + " </td>"
            html += "</tr>"
        html += "</table>"
        html = html.replace("£", "&#163;")

        if publish:
            self.dashboard_item(self.prefix + ".plan_html", state="", attributes={"html": html, "friendly_name": "Plan in HTML", "icon": "mdi:web-box"})
            self.html_plan = html

        return html

    def publish_rates(self, rates, export, gas=False):
        """
        Publish the rates for charts
        Create rates/time every 30 minutes
        """
        rates_time = {}
        for minute in range(-24 * 60, self.minutes_now + self.forecast_minutes + 24 * 60, 30):
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            rates_time[stamp] = dp2(rates[minute])

        if export:
            self.publish_rates_export()
        elif gas:
            pass
        else:
            self.publish_rates_import()

        if export:
            self.dashboard_item(
                self.prefix + ".rates_export",
                state=dp2(rates[self.minutes_now]),
                attributes={
                    "min": dp2(self.rate_export_min),
                    "max": dp2(self.rate_export_max),
                    "average": dp2(self.rate_export_average),
                    "threshold": dp2(self.rate_export_cost_threshold),
                    "results": self.filtered_times(rates_time),
                    "friendly_name": "Export rates",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
        elif gas:
            self.dashboard_item(
                self.prefix + ".rates_gas",
                state=dp2(rates[self.minutes_now]),
                attributes={
                    "min": dp2(self.rate_gas_min),
                    "max": dp2(self.rate_gas_max),
                    "average": dp2(self.rate_gas_average),
                    "results": self.filtered_times(rates_time),
                    "friendly_name": "Gas rates",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
        else:
            self.dashboard_item(
                self.prefix + ".rates",
                state=dp2(rates[self.minutes_now]),
                attributes={
                    "min": dp2(self.rate_min),
                    "max": dp2(self.rate_max),
                    "average": dp2(self.rate_average),
                    "threshold": dp2(self.rate_import_cost_threshold),
                    "results": self.filtered_times(rates_time),
                    "friendly_name": "Import rates",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                },
            )
        return rates

    def today_cost(self, import_today, export_today, car_today, load_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_cost_import = 0
        day_cost_export = 0
        day_cost_nosc = 0
        day_import = 0
        day_export = 0
        day_car = 0
        day_cost_car = 0
        day_energy = 0
        day_load = 0
        day_energy_export = 0
        day_energy_total = 0
        day_cost_time = {}
        day_cost_time_import = {}
        day_cost_time_export = {}
        car_cost_time = {}
        day_carbon_time = {}
        carbon_g = 0

        hour_cost = 0
        hour_cost_import = 0
        hour_cost_export = 0
        hour_cost_car = 0
        hour_energy = 0
        hour_energy_export = 0
        hour_energy_import = 0
        hour_energy_car = 0
        hour_load = 0
        hour_carbon_g = 0

        # Work out change in battery value
        battery_level_now = self.soc_kwh_history.get(0, 0)
        battery_level_hour = self.soc_kwh_history.get(60, 0)
        battery_level_midnight = self.soc_kwh_history.get(self.minutes_now, 0)
        battery_change_hour = battery_level_now - battery_level_hour
        battery_change_midnight = battery_level_now - battery_level_midnight
        rate_min = self.rate_min_forward.get(self.minutes_now, self.rate_min) / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
        rate_export_min = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min
        rate_forward = max(rate_min, 1.0, rate_export_min)
        value_increase_hour = battery_change_hour * rate_forward * self.metric_battery_value_scaling
        value_increase_day = battery_change_midnight * rate_forward * self.metric_battery_value_scaling

        self.log(
            "Battery level now {} -1hr {} midnight {} battery value change hour {} day {} rate_forward {}".format(
                dp2(battery_level_now), dp2(battery_level_hour), dp2(battery_level_midnight), dp2(value_increase_hour), dp2(value_increase_day), dp2(rate_forward)
            )
        )

        for minute_back in range(60):
            minute = self.minutes_now - minute_back
            energy_import = self.get_from_incrementing(import_today, minute_back)
            load_energy = self.get_from_incrementing(load_today, minute_back)

            if car_today:
                energy_car = self.get_from_incrementing(car_today, minute_back)
            else:
                energy_car = 0

            if export_today:
                energy_export = self.get_from_incrementing(export_today, minute_back)
            else:
                energy_export = 0

            hour_energy += energy_import - energy_export
            hour_energy_import += energy_import
            hour_energy_export += energy_export
            hour_energy_car += energy_car

            hour_load += load_energy

            if self.rate_import:
                hour_cost += self.rate_import[minute] * energy_import
                hour_cost_import += self.rate_import[minute] * energy_import
                hour_cost_car += self.rate_import[minute] * energy_car

            if self.rate_export:
                hour_cost -= self.rate_export[minute] * energy_export
                hour_cost_export -= self.rate_export[minute] * energy_export

            if self.carbon_enable:
                hour_carbon_g += self.carbon_history.get(minute_back, 0) * energy_import
                hour_carbon_g -= self.carbon_history.get(minute_back, 0) * energy_export

        self.log(
            "Hour energy {} import {} export {} car {} load {} cost {} import {} export {} car {} carbon {} kG".format(
                dp2(hour_energy),
                dp2(hour_energy_import),
                dp2(hour_energy_export),
                dp2(hour_energy_car),
                dp2(hour_load),
                dp2(hour_cost),
                dp2(hour_cost_import),
                dp2(hour_cost_export),
                dp2(hour_cost_car),
                dp2(hour_carbon_g / 1000.0),
            )
        )

        for minute in range(self.minutes_now):
            # Add in standing charge
            if (minute % (24 * 60)) == 0:
                day_cost += self.metric_standing_charge

            minute_back = self.minutes_now - minute - 1
            energy = 0
            car_energy = 0
            energy = self.get_from_incrementing(import_today, minute_back)
            load_energy = self.get_from_incrementing(load_today, minute_back)

            if car_today:
                car_energy = self.get_from_incrementing(car_today, minute_back)

            if export_today:
                energy_export = self.get_from_incrementing(export_today, minute_back)
            else:
                energy_export = 0
            day_energy += energy
            day_energy_export += energy_export
            day_energy_total += energy - energy_export
            day_load += load_energy

            day_import += energy
            day_car += car_energy
            if self.rate_import:
                day_cost += self.rate_import[minute] * energy
                day_cost_import += self.rate_import[minute] * energy
                day_cost_nosc += self.rate_import[minute] * energy
                day_cost_car += self.rate_import[minute] * car_energy

            day_export += energy_export
            if self.rate_export:
                day_cost -= self.rate_export[minute] * energy_export
                day_cost_nosc -= self.rate_export[minute] * energy_export
                day_cost_export -= self.rate_export[minute] * energy_export

            if self.carbon_enable:
                carbon_g += self.carbon_history.get(minute_back, 0) * energy
                carbon_g -= self.carbon_history.get(minute_back, 0) * energy_export

            if (minute % 5) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = dp2(day_cost)
                car_cost_time[stamp] = dp2(day_cost_car)
                day_cost_time_import[stamp] = dp2(day_cost_import)
                day_cost_time_export[stamp] = dp2(day_cost_export)
                day_carbon_time[stamp] = dp2(carbon_g)

        day_pkwh = self.rate_import.get(0, 0)
        day_car_pkwh = self.rate_import.get(0, 0)
        day_import_pkwh = self.rate_import.get(0, 0)
        day_export_pkwh = self.rate_export.get(0, 0)
        day_load_pkwh = self.rate_import.get(0, 0)
        hour_pkwh = self.rate_import.get(0, 0)
        hour_pkwh_import = self.rate_import.get(0, 0)
        hour_pkwh_car = self.rate_import.get(0, 0)
        hour_pkwh_export = self.rate_export.get(0, 0)
        hour_load_pkwh = self.rate_import.get(0, 0)

        if day_load > 0:
            day_pkwh = (day_cost_nosc - value_increase_day) / day_load
            day_load_pkwh = day_cost_nosc / day_load
        if day_car > 0:
            day_car_pkwh = day_cost_car / day_car
        if day_import > 0:
            day_import_pkwh = day_cost_import / day_import
        if day_export > 0:
            day_export_pkwh = day_cost_export / day_export
        if hour_load > 0:
            hour_pkwh = (hour_cost - value_increase_hour) / hour_load
            hour_load_pkwh = hour_cost / hour_load
        if hour_energy_import > 0:
            hour_pkwh_import = hour_cost_import / hour_energy_import
        if hour_energy_export > 0:
            hour_pkwh_export = hour_cost_export / hour_energy_export
        if hour_energy_car > 0:
            hour_pkwh_car = hour_cost_car / hour_energy_car

        load_cost_day = day_pkwh * day_load
        load_cost_hour = hour_pkwh * hour_load

        self.dashboard_item(
            self.prefix + ".cost_today",
            state=dp2(day_cost),
            attributes={
                "results": self.filtered_times(day_cost_time),
                "friendly_name": "Cost so far today (since midnight)",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "energy": dp2(day_energy_total),
                "energy_import": dp2(day_import),
                "energy_export": dp2(day_export),
                "energy_car": dp2(day_car),
                "energy_load": dp2(day_load),
                "cost_energy": dp2(day_cost_nosc),
                "cost_load": dp2(load_cost_day),
                "cost_import": dp2(day_cost_import),
                "cost_export": dp2(day_cost_export),
                "cost_car": dp2(day_cost_car),
                "carbon": dp2(carbon_g / 1000.0),
            },
        )
        self.dashboard_item(
            self.prefix + ".ppkwh_today",
            state=dp2(day_pkwh),
            attributes={
                "friendly_name": "Cost today in p/kWh",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "p/kWh": dp2(day_pkwh),
                "p/kWh_car": dp2(day_car_pkwh),
                "p/kWh_import": dp2(day_import_pkwh),
                "p/kWh_export": dp2(day_export_pkwh),
                "p/kWh_load": dp2(day_load_pkwh),
                "p/kWh_forward": dp2(rate_forward),
                "battery_now": dp2(battery_level_now),
                "battery_midnight": dp2(battery_level_midnight),
                "battery_value_change": dp2(value_increase_day),
            },
        )
        self.dashboard_item(
            self.prefix + ".cost_hour",
            state=dp2(hour_cost),
            attributes={
                "friendly_name": "Cost in last hour",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "energy": dp2(hour_energy),
                "energy_import": dp2(hour_energy_import),
                "energy_export": dp2(hour_energy_export),
                "energy_car": dp2(hour_energy_car),
                "energy_load": dp2(hour_load),
                "cost_energy": dp2(hour_cost),
                "cost_import": dp2(hour_cost_import),
                "cost_export": dp2(hour_cost_export),
                "cost_car": dp2(hour_cost_car),
                "cost_load": dp2(load_cost_hour),
                "carbon": dp2(hour_carbon_g / 1000.0),
            },
        )
        self.dashboard_item(
            self.prefix + ".ppkwh_hour",
            state=dp2(hour_pkwh),
            attributes={
                "friendly_name": "Cost in p/kWh",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "p/kWh": dp2(hour_pkwh),
                "p/kWh_car": dp2(hour_pkwh_car),
                "p/kWh_import": dp2(hour_pkwh_import),
                "p/kWh_export": dp2(hour_pkwh_export),
                "p/kWh_load": dp2(hour_load_pkwh),
                "p/kWh_forward": dp2(rate_forward),
                "battery_now": dp2(battery_level_now),
                "battery_hour": dp2(battery_level_hour),
                "battery_value_change": dp2(value_increase_hour),
            },
        )
        if self.num_cars > 0:
            self.dashboard_item(
                self.prefix + ".cost_today_car",
                state=dp2(day_cost_car),
                attributes={
                    "results": self.filtered_times(car_cost_time),
                    "friendly_name": "Car cost so far today (approx)",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                    "energy": dp2(day_car),
                    "p/kWh": dp2(day_car_pkwh),
                },
            )
        if self.carbon_enable:
            self.dashboard_item(
                self.prefix + ".carbon_today",
                state=dp2(carbon_g),
                attributes={
                    "results": self.filtered_times(day_carbon_time),
                    "friendly_name": "Carbon today so far",
                    "state_class": "measurement",
                    "unit_of_measurement": "g",
                    "icon": "mdi:carbon-molecule",
                },
            )
        self.dashboard_item(
            self.prefix + ".cost_today_import",
            state=dp2(day_cost_import),
            attributes={
                "results": self.filtered_times(day_cost_time_import),
                "friendly_name": "Cost so far today import",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "energy": dp2(day_import),
                "p/kWh": dp2(day_import_pkwh),
            },
        )
        self.dashboard_item(
            self.prefix + ".cost_today_export",
            state=dp2(day_cost_export),
            attributes={
                "results": self.filtered_times(day_cost_time_export),
                "friendly_name": "Cost so far today export",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "energy": dp2(day_export),
                "p/kWh": dp2(day_export_pkwh),
            },
        )
        self.log(
            "Today's energy import {} kWh export {} kWh total {} kWh cost {} {} import {} {} export {} {} carbon {} kg".format(
                dp2(day_energy),
                dp2(day_energy_export),
                dp2(day_energy_total),
                dp2(day_cost),
                self.currency_symbols[1],
                dp2(day_cost_import),
                self.currency_symbols[1],
                dp2(day_cost_export),
                self.currency_symbols[1],
                dp2(carbon_g / 1000.0),
            )
        )
        return day_cost, carbon_g

    def publish_export_limit(self, export_window, export_limits, best):
        """
        Create entity to chart export limit

        Args:
            export_window (list): List of dictionaries representing the export window.
            export_limits (list): List of export limits in percent.
            best (bool): Flag indicating whether to push as base or as best

        Returns:
            None
        """
        export_limit_time = {}
        export_limit_time_kw = {}

        export_limit_soc = self.soc_max
        export_limit_percent = 100
        export_limit_first = False
        prev_limit = -1

        for minute in range(0, self.forecast_minutes + self.minutes_now, 5):
            window_n = self.in_charge_window(export_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window_n >= 0 and (export_limits[window_n] < 100.0):
                soc_perc = export_limits[window_n]
                soc_kw = (soc_perc * self.soc_max) / 100.0
                if not export_limit_first:
                    export_limit_soc = soc_kw
                    export_limit_percent = export_limits[window_n]
                    export_limit_first = True
            else:
                soc_perc = 100
                soc_kw = self.soc_max
            if prev_limit != soc_perc:
                export_limit_time[stamp] = soc_perc
                export_limit_time_kw[stamp] = dp2(soc_kw)
            prev_limit = soc_perc

        export_start_str = ""
        export_end_str = ""
        export_start_date = None
        export_end_date = None
        export_average = None
        export_start_in_minutes = self.forecast_minutes
        export_end_in_minutes = self.forecast_minutes

        if export_window and (export_window[0]["end"] < (24 * 60 + self.minutes_now)):
            export_start_minutes = export_window[0]["start"]
            export_end_minutes = export_window[0]["end"]
            export_average = export_window[0].get("average", None)
            export_start_in_minutes = max(export_start_minutes - self.minutes_now, 0)
            export_end_in_minutes = max(export_end_minutes - self.minutes_now, 0)

            time_format_time = "%H:%M:%S"
            export_startt = self.midnight_utc + timedelta(minutes=export_start_minutes)
            export_endt = self.midnight_utc + timedelta(minutes=export_end_minutes)
            export_start_str = export_startt.strftime(time_format_time)
            export_end_str = export_endt.strftime(time_format_time)
            export_start_date = export_startt.strftime(TIME_FORMAT)
            export_end_date = export_endt.strftime(TIME_FORMAT)

        if best:
            self.dashboard_item(
                self.prefix + ".best_export_limit_kw",
                state=dp2(export_limit_soc),
                attributes={
                    "results": self.filtered_times(export_limit_time_kw),
                    "friendly_name": "Predicted export limit kWh best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-charging",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_export_limit",
                state=export_limit_percent,
                attributes={
                    "results": self.filtered_times(export_limit_time),
                    "rate": export_average,
                    "friendly_name": "Predicted export limit best",
                    "state_class": "measurement",
                    "unit_of_measurement": "%",
                    "icon": "mdi:battery-charging",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_export_start",
                state=export_start_str,
                attributes={
                    "minutes_to": export_start_in_minutes,
                    "timestamp": export_start_date,
                    "friendly_name": "Predicted export start time best",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": export_average,
                },
            )
            self.dashboard_item(
                self.prefix + ".best_export_end",
                state=export_end_str,
                attributes={
                    "minutes_to": export_end_in_minutes,
                    "timestamp": export_end_date,
                    "friendly_name": "Predicted export end time best",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": export_average,
                },
            )
        else:
            self.dashboard_item(
                self.prefix + ".export_limit_kw",
                state=dp2(export_limit_soc),
                attributes={
                    "results": self.filtered_times(export_limit_time_kw),
                    "friendly_name": "Predicted export limit kWh",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-charging",
                },
            )
            self.dashboard_item(
                self.prefix + ".export_limit",
                state=export_limit_percent,
                attributes={
                    "results": self.filtered_times(export_limit_time),
                    "rate": export_average,
                    "friendly_name": "Predicted export limit",
                    "state_class": "measurement",
                    "unit_of_measurement": "%",
                    "icon": "mdi:battery-charging",
                },
            )
            self.dashboard_item(
                self.prefix + ".export_start",
                state=export_start_str,
                attributes={
                    "minutes_to": export_start_in_minutes,
                    "timestamp": export_start_date,
                    "friendly_name": "Predicted export start time",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": export_average,
                },
            )
            self.dashboard_item(
                self.prefix + ".export_end",
                state=export_end_str,
                attributes={
                    "minutes_to": export_end_in_minutes,
                    "timestamp": export_end_date,
                    "friendly_name": "Predicted export end time",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": export_average,
                },
            )

    def publish_charge_limit(self, charge_limit, charge_window, charge_limit_percent, best=False, soc={}):
        """
        Create entity to chart charge limit

        Parameters:

        - charge_limit (list): List of charge limits in kWh
        - charge_window (list): List of charge window dictionaries
        - charge_limit_percent (list): List of charge limit percentages
        - best (bool, optional): Flag indicating if we publish as base or as best
        - soc (dict, optional): Dictionary of the predicted SoC over time

        """
        charge_limit_time = {}
        charge_limit_time_kw = {}
        prev_perc = -1

        for minute in range(0, self.forecast_minutes + self.minutes_now, 5):
            window = self.in_charge_window(charge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window >= 0:
                soc_perc = charge_limit_percent[window]
                soc_kw = charge_limit[window]
            else:
                soc_perc = 0
                soc_kw = 0

            # Convert % of charge freeze to current SoC number
            if soc_perc == self.reserve_percent:
                offset = int((minute - self.minutes_now) / 5) * 5
                soc_kw = soc.get(offset, soc_kw)

            if prev_perc != soc_perc:
                charge_limit_time[stamp] = soc_perc
                charge_limit_time_kw[stamp] = soc_kw
            prev_perc = soc_perc

        charge_limit_first = 0
        charge_limit_percent_first = 0
        charge_average_first = None
        charge_start_str = ""
        charge_end_str = ""
        charge_start_date = None
        charge_end_date = None
        charge_start_in_minutes = self.forecast_days * 24 * 60
        charge_end_in_minutes = self.forecast_days * 24 * 60

        if charge_limit and charge_window[0]["end"] <= (24 * 60 + self.minutes_now):
            charge_limit_first = charge_limit[0]
            charge_limit_percent_first = charge_limit_percent[0]
            charge_start_minutes = charge_window[0]["start"]
            charge_end_minutes = charge_window[0]["end"]
            charge_average_first = charge_window[0].get("average", None)
            charge_start_in_minutes = max(charge_start_minutes - self.minutes_now, 0)
            charge_end_in_minutes = max(charge_end_minutes - self.minutes_now, 0)

            time_format_time = "%H:%M:%S"
            charge_startt = self.midnight_utc + timedelta(minutes=charge_start_minutes)
            charge_endt = self.midnight_utc + timedelta(minutes=charge_end_minutes)
            charge_start_str = charge_startt.strftime(time_format_time)
            charge_end_str = charge_endt.strftime(time_format_time)
            charge_start_date = charge_startt.strftime(TIME_FORMAT)
            charge_end_date = charge_endt.strftime(TIME_FORMAT)

        if best:
            self.dashboard_item(
                self.prefix + ".best_charge_limit_kw",
                state=dp2(charge_limit_first),
                attributes={
                    "results": self.filtered_times(charge_limit_time_kw),
                    "friendly_name": "Predicted charge limit kWh best",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-charging",
                },
            )
            self.dashboard_item(
                self.prefix + ".best_charge_limit",
                state=charge_limit_percent_first,
                attributes={
                    "results": self.filtered_times(charge_limit_time),
                    "friendly_name": "Predicted charge limit best",
                    "state_class": "measurement",
                    "unit_of_measurement": "%",
                    "icon": "mdi:battery-charging",
                    "rate": charge_average_first,
                },
            )
            self.dashboard_item(
                self.prefix + ".best_charge_start",
                state=charge_start_str,
                attributes={
                    "timestamp": charge_start_date,
                    "minutes_to": charge_start_in_minutes,
                    "friendly_name": "Predicted charge start time best",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": charge_average_first,
                },
            )
            self.dashboard_item(
                self.prefix + ".best_charge_end",
                state=charge_end_str,
                attributes={
                    "timestamp": charge_end_date,
                    "minutes_to": charge_end_in_minutes,
                    "friendly_name": "Predicted charge end time best",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": charge_average_first,
                },
            )
        else:
            self.dashboard_item(
                self.prefix + ".charge_limit_kw",
                state=dp2(charge_limit_first),
                attributes={
                    "results": self.filtered_times(charge_limit_time_kw),
                    "friendly_name": "Predicted charge limit kWh",
                    "state_class": "measurement",
                    "unit_of_measurement": "kWh",
                    "icon": "mdi:battery-charging",
                },
            )
            self.dashboard_item(
                self.prefix + ".charge_limit",
                state=charge_limit_percent_first,
                attributes={
                    "results": self.filtered_times(charge_limit_time),
                    "friendly_name": "Predicted charge limit",
                    "state_class": "measurement",
                    "unit_of_measurement": "%",
                    "icon": "mdi:battery-charging",
                    "rate": charge_average_first,
                },
            )
            self.dashboard_item(
                self.prefix + ".charge_start",
                state=charge_start_str,
                attributes={
                    "timestamp": charge_start_date,
                    "minutes_to": charge_start_in_minutes,
                    "friendly_name": "Predicted charge start time",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": charge_average_first,
                },
            )
            self.dashboard_item(
                self.prefix + ".charge_end",
                state=charge_end_str,
                attributes={
                    "timestamp": charge_end_date,
                    "minutes_to": charge_end_in_minutes,
                    "friendly_name": "Predicted charge end time",
                    "device_class": "timestamp",
                    "state_class": None,
                    "unit_of_measurement": None,
                    "icon": "mdi:table-clock",
                    "rate": charge_average_first,
                },
            )

    def record_status(self, message, debug="", had_errors=False, notify=False, extra=""):
        """
        Records status to HA sensor
        """
        if not extra:
            extra = ""

        self.current_status = message + extra
        if notify and self.previous_status != message and self.set_status_notify:
            self.call_notify("Predbat status change to: " + message + extra)
            self.previous_status = message

        self.dashboard_item(
            self.prefix + ".status",
            state=message,
            attributes={
                "friendly_name": "Status",
                "detail": extra,
                "icon": "mdi:information",
                "last_updated": str(datetime.now()),
                "debug": debug,
                "version": THIS_VERSION,
                "error": (had_errors or self.had_errors),
            },
        )

        if had_errors:
            self.log("Warn: record_status {}".format(message + extra))
        else:
            self.log("Info: record_status {}".format(message + extra))

        if had_errors:
            self.had_errors = True

    def load_today_comparison(self, load_minutes, load_forecast, car_minutes, import_minutes, minutes_now, step=5):
        """
        Compare predicted vs actual load
        """
        load_total_pred = 0
        load_total_pred_now = 0
        car_total_pred = 0
        car_total_actual = 0
        car_value_pred = 0
        car_value_actual = 0
        actual_total_now = 0
        actual_total_today = 0
        import_ignored_load_pred = 0
        import_ignored_load_actual = 0
        load_predict_stamp = {}
        load_actual_stamp = {}
        load_predict_data = {}
        total_forecast_value_pred = 0
        total_forecast_value_pred_now = 0

        for minute in range(0, 24 * 60, step):
            import_value_today = 0
            load_value_today = 0
            load_value_today_raw = 0

            if minute < minutes_now:
                for offset in range(step):
                    import_value_today += self.get_from_incrementing(import_minutes, minutes_now - minute - offset - 1)
                    load_value_today, load_value_today_raw = self.get_filtered_load_minute(load_minutes, minutes_now - minute - 1, historical=False, step=step)

            import_value_pred = 0
            forecast_value_pred = 0
            for offset in range(step):
                import_value_pred += self.get_historical(import_minutes, minute - minutes_now + offset)
                forecast_value_pred += self.get_from_incrementing(load_forecast, minute + offset, backwards=False)

            if self.load_forecast_only:
                load_value_pred, load_value_pred_raw = (0, 0)
            else:
                load_value_pred, load_value_pred_raw = self.get_filtered_load_minute(load_minutes, minute - minutes_now, historical=True, step=step)

            # Add in forecast load
            load_value_pred += forecast_value_pred
            load_value_pred_raw += forecast_value_pred

            # Ignore periods of import as assumed to be deliberate (battery charging periods overnight for example)
            car_value_actual = load_value_today_raw - load_value_today
            car_value_pred = load_value_pred_raw - load_value_pred
            if minute < minutes_now and import_value_today >= load_value_today_raw:
                import_ignored_load_actual += load_value_today
                load_value_today = 0
                import_ignored_load_pred += load_value_pred
                load_value_pred = 0

            # Only count totals until now
            if minute < minutes_now:
                load_total_pred_now += load_value_pred
                car_total_pred += car_value_pred
                actual_total_now += load_value_today
                car_total_actual += car_value_actual
                actual_total_today += load_value_today
                total_forecast_value_pred_now += forecast_value_pred
            else:
                actual_total_today += load_value_pred

            load_total_pred += load_value_pred
            total_forecast_value_pred += forecast_value_pred

            load_predict_data[minute] = load_value_pred

            # Store for charts
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            load_predict_stamp[stamp] = dp3(load_total_pred)
            load_actual_stamp[stamp] = dp3(actual_total_today)

        difference = 1.0
        if minutes_now >= 180 and actual_total_now >= 1.0 and actual_total_today > 0.0:
            # Make a ratio only if we have enough data to consider the outcome
            difference = 1.0 + ((actual_total_today - load_total_pred) / actual_total_today)

        # Work out divergence
        if not self.calculate_inday_adjustment:
            difference_cap = 1.0
        else:
            # Apply damping factor to adjustment
            difference_cap = (difference - 1.0) * self.metric_inday_adjust_damping + 1.0

            # Cap adjustment within 1/2 to 2x
            difference_cap = max(difference_cap, 0.5)
            difference_cap = min(difference_cap, 2.0)

        self.log(
            "Today's load divergence {} % in-day adjustment {} % damping {}x".format(
                dp2(difference * 100.0),
                dp2(difference_cap * 100.0),
                self.metric_inday_adjust_damping,
            )
        )
        self.log(
            "Today's predicted so far {} kWh with {} kWh car/iBoost excluded and {} kWh import ignored and {} forecast extra.".format(
                dp2(load_total_pred_now),
                dp2(car_total_pred),
                dp2(import_ignored_load_pred),
                dp2(total_forecast_value_pred_now),
            )
        )
        self.log(
            "Today's actual load so far {} kWh with {} kWh Car/iBoost excluded and {} kWh import ignored.".format(
                dp2(actual_total_now),
                dp2(car_total_actual),
                dp2(import_ignored_load_actual),
            )
        )

        # Create adjusted curve
        load_adjusted_stamp = {}
        load_adjusted = actual_total_now
        for minute in range(0, 24 * 60, step):
            if minute >= minutes_now:
                load = load_predict_data[minute] * difference_cap
                load_adjusted += load
                minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                load_adjusted_stamp[stamp] = dp3(load_adjusted)

        self.dashboard_item(
            self.prefix + ".load_inday_adjustment",
            state=dp2(difference_cap * 100.0),
            attributes={
                "damping": self.metric_inday_adjust_damping,
                "friendly_name": "Load in-day adjustment factor",
                "state_class": "measurement",
                "unit_of_measurement": "%",
                "icon": "mdi:percent",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_energy_actual",
            state=dp3(actual_total_today),
            attributes={
                "results": self.filtered_times(load_actual_stamp),
                "friendly_name": "Load energy actual (filtered)",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_energy_predicted",
            state=dp3(load_total_pred),
            attributes={
                "results": self.filtered_times(load_predict_stamp),
                "today": self.filtered_today(load_predict_stamp),
                "friendly_name": "Load energy predicted (filtered)",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_energy_adjusted",
            state=dp3(load_adjusted),
            attributes={
                "results": self.filtered_times(load_adjusted_stamp),
                "today": self.filtered_today(load_adjusted_stamp),
                "friendly_name": "Load energy prediction adjusted",
                "state_class": "measurement",
                "unit_of_measurement": "kWh",
                "icon": "mdi:percent",
            },
        )

        return difference_cap

    def get_load_divergence(self, minutes_now, load_minutes):
        """
        Work out the divergence between peak and average load over the next period
        """
        load_total = 0
        load_count = 0
        load_min = 99999
        load_max = 0
        look_over = 60 * 8

        for minute in range(0, look_over, PREDICT_STEP):
            load, load_raw = self.get_filtered_load_minute(load_minutes, minute, historical=True, step=PREDICT_STEP)
            load *= 1000 * 60 / PREDICT_STEP
            load_total += load
            load_count += 1
            load_min = min(load_min, load)
            load_max = max(load_max, load)
        load_mean = load_total / load_count
        load_diff_total = 0
        for minute in range(0, look_over, PREDICT_STEP):
            load = 0
            load, load_raw = self.get_filtered_load_minute(load_minutes, minute, historical=True, step=PREDICT_STEP)
            load *= 1000 * 60 / PREDICT_STEP
            load_diff = abs(load - load_mean)
            load_diff *= load_diff
            load_diff_total += load_diff

        load_std_dev = math.sqrt(load_diff_total / load_count)
        if load_mean > 0:
            load_divergence = load_std_dev / load_mean / 2.0
            load_divergence = min(load_divergence, 1.0)
        else:
            self.log("Warn: Load mean is zero, unable to calculate divergence!")
            load_divergence = 0

        self.log("Load divergence over {} hours mean {} W, min {} W, max {} W, std dev {} W, divergence {}%".format(look_over / 60.0, dp2(load_mean), dp2(load_min), dp2(load_max), dp2(load_std_dev), dp2(load_divergence * 100.0)))
        if self.metric_load_divergence_enable:
            return dp2(load_divergence)
        else:
            return None

    def set_charge_export_status(self, isCharging, isExporting, isDemand):
        """
        Reports status on charging/exporting to binary sensor
        """
        self.dashboard_item("binary_sensor." + self.prefix + "_charging", state="on" if isCharging else "off", attributes={"friendly_name": "Predbat is charging", "icon": "mdi:battery-arrow-up"})
        self.dashboard_item(
            "binary_sensor." + self.prefix + "_exporting",
            state="on" if isExporting else "off",
            attributes={"friendly_name": "Predbat is force exporting", "icon": "mdi:battery-arrow-down"},
        )
        self.dashboard_item("binary_sensor." + self.prefix + "_demand", state="on" if isDemand else "off", attributes={"friendly_name": "Predbat is in demand mode", "icon": "mdi:battery-arrow-up"})

    def calculate_yesterday(self):
        """
        Calculate the base plan for yesterday
        """
        yesterday_load_step = self.step_data_history(self.load_minutes, 0, forward=False, scale_today=1.0, scale_fixed=1.0, base_offset=24 * 60 + self.minutes_now)
        yesterday_pv_step = self.step_data_history(self.pv_today, 0, forward=False, scale_today=1.0, scale_fixed=1.0, base_offset=24 * 60 + self.minutes_now)
        yesterday_pv_step_zero = self.step_data_history(None, 0, forward=False, scale_today=1.0, scale_fixed=1.0, base_offset=24 * 60 + self.minutes_now)
        minutes_back = self.minutes_now + 1

        # Get yesterday's SOC
        try:
            soc_yesterday = float(self.get_state_wrapper(self.prefix + ".savings_total_soc", default=0.0))
        except (ValueError, TypeError):
            soc_yesterday = 0.0

        # Shift rates back
        past_rates = self.history_to_future_rates(self.rate_import, 24 * 60)
        past_rates_export = self.history_to_future_rates(self.rate_export, 24 * 60)

        # Assume user might charge at the lowest rate only, for fix tariff
        charge_window_best = []
        rate_low = min(past_rates.values())
        combine_charge = self.combine_charge_slots
        self.combine_charge_slots = True
        if min(past_rates.values()) != max(past_rates.values()):
            charge_window_best, lowest, highest = self.rate_scan_window(past_rates, 5, rate_low, False, return_raw=True)
        self.combine_charge_slots = combine_charge
        charge_limit_best = [self.soc_max for c in range(len(charge_window_best))]
        self.log("Yesterday basic charge window best: {} charge limit best: {}".format(charge_window_best, charge_limit_best))

        # Get Cost yesterday
        cost_today_data = self.get_history_wrapper(entity_id=self.prefix + ".cost_today", days=2, required=False)
        if not cost_today_data:
            self.log("Warn: No cost_today data for yesterday")
            return
        cost_data = self.minute_data(cost_today_data[0], 2, self.now_utc, "state", "last_updated", backwards=True, clean_increment=False, smoothing=False, divide_by=1.0, scale=1.0)
        cost_data_per_kwh = self.minute_data(cost_today_data[0], 2, self.now_utc, "p/kWh", "last_updated", attributes=True, backwards=True, clean_increment=False, smoothing=False, divide_by=1.0, scale=1.0)
        cost_yesterday = cost_data.get(minutes_back, 0.0)
        cost_yesterday_per_kwh = cost_data_per_kwh.get(minutes_back, 0.0)

        cost_today_car_data = self.get_history_wrapper(entity_id=self.prefix + ".cost_today_car", days=2, required=False)
        if not cost_today_car_data:
            cost_today_car_data = {}
            cost_data_car = {}
            cost_yesterday_car = 0
            cost_data_car_per_kwh = 0
            cost_car_per_kwh = 0
        else:
            cost_data_car = self.minute_data(cost_today_car_data[0], 2, self.now_utc, "state", "last_updated", backwards=True, clean_increment=False, smoothing=False, divide_by=1.0, scale=1.0)
            cost_data_car_per_kwh = self.minute_data(cost_today_car_data[0], 2, self.now_utc, "p/kWh", "last_updated", attributes=True, backwards=True, clean_increment=False, smoothing=False, divide_by=1.0, scale=1.0)
            cost_yesterday_car = cost_data_car.get(minutes_back, 0.0)
            cost_car_per_kwh = cost_data_car_per_kwh.get(minutes_back, 0.0)

        # Save state
        self.dashboard_item(
            self.prefix + ".cost_yesterday",
            state=dp2(cost_yesterday),
            attributes={
                "friendly_name": "Cost yesterday",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
                "p/kWh": dp2(cost_yesterday_per_kwh),
            },
        )
        if self.num_cars > 0:
            self.dashboard_item(
                self.prefix + ".cost_yesterday_car",
                state=dp2(cost_yesterday_car),
                attributes={
                    "friendly_name": "Cost yesterday car",
                    "state_class": "measurement",
                    "unit_of_measurement": self.currency_symbols[1],
                    "icon": "mdi:currency-usd",
                    "p/kWh": dp2(cost_car_per_kwh),
                },
            )

        # Save step data for debug
        self.yesterday_load_step = yesterday_load_step
        self.yesterday_pv_step = yesterday_pv_step

        # Save state
        minutes_now = self.minutes_now
        midnight_utc = self.midnight_utc
        forecast_minutes = self.forecast_minutes
        cost_today_sofar = self.cost_today_sofar
        import_today_now = self.import_today_now
        export_today_now = self.export_today_now
        pv_today_now = self.pv_today_now
        carbon_today_sofar = self.carbon_today_sofar
        soc_kw = self.soc_kw
        car_charging_hold = self.car_charging_hold
        iboost_energy_subtract = self.iboost_energy_subtract
        load_minutes_now = self.load_minutes_now
        soc_max = self.soc_max
        rate_import = self.rate_import
        rate_export = self.rate_export
        iboost_enable = self.iboost_enable
        num_cars = self.num_cars

        # Fake to yesterday state
        self.minutes_now = 0
        self.cost_today_sofar = 0
        self.import_today_now = 0
        self.export_today_now = 0
        self.carbon_today_sofar = 0
        self.midnight_utc = self.midnight_utc - timedelta(days=1)
        self.forecast_minutes = 24 * 60
        self.pv_today_now = 0
        self.soc_kw = soc_yesterday
        self.car_charging_hold = False
        self.iboost_energy_subtract = False
        self.load_minutes_now = 0
        self.rate_import = past_rates
        self.rate_export = past_rates_export
        self.iboost_enable = False
        self.num_cars = 0

        # Simulate yesterday
        self.prediction = Prediction(self, yesterday_pv_step, yesterday_pv_step, yesterday_load_step, yesterday_load_step)
        (
            metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
        ) = self.run_prediction(charge_limit_best, charge_window_best, [], [], False, end_record=(24 * 60), save="yesterday")
        # Add back in standing charge which will be in the historical data also
        metric += self.metric_standing_charge

        # Work out savings
        saving = metric - cost_yesterday
        self.log(
            "Yesterday: Predbat disabled was {}p vs real {}p saving {}p with import {} export {} battery_cycle {} start_soc {} final_soc {}".format(
                dp2(metric),
                dp2(cost_yesterday),
                dp2(saving),
                dp2(import_kwh_house + import_kwh_battery),
                dp2(export_kwh),
                dp2(battery_cycle),
                dp2(soc_yesterday),
                dp2(final_soc),
            )
        )
        self.savings_today_predbat = saving
        self.savings_today_predbat_soc = final_soc
        self.savings_today_actual = cost_yesterday
        self.cost_yesterday_car = cost_yesterday_car

        # Save state
        self.dashboard_item(
            self.prefix + ".savings_yesterday_predbat",
            state=dp2(saving),
            attributes={
                "import": dp2(import_kwh_house + import_kwh_battery),
                "export": dp2(export_kwh),
                "battery_cycle": dp2(battery_cycle),
                "soc_yesterday": dp2(soc_yesterday),
                "final_soc": dp2(final_soc),
                "actual_cost": dp2(cost_yesterday),
                "predicted_cost": dp2(metric),
                "friendly_name": "Predbat savings yesterday",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
            },
        )

        # Simulate no PV or battery
        self.soc_kw = 0
        self.soc_max = 0

        self.prediction = Prediction(self, yesterday_pv_step_zero, yesterday_pv_step_zero, yesterday_load_step, yesterday_load_step)
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction([], [], [], [], False, end_record=24 * 60)
        # Add back in standing charge which will be in the historical data also
        metric += self.metric_standing_charge

        # Work out savings
        saving = metric - cost_yesterday
        self.savings_today_pvbat = saving
        self.log("Yesterday: No Battery/PV system cost predicted was {}p vs real {}p saving {}p with import {} export {}".format(dp2(metric), dp2(cost_yesterday), dp2(saving), dp2(import_kwh_house + import_kwh_battery), dp2(export_kwh)))

        # Save state
        self.dashboard_item(
            self.prefix + ".savings_yesterday_pvbat",
            state=dp2(saving),
            attributes={
                "import": dp2(import_kwh_house + import_kwh_battery),
                "export": dp2(export_kwh),
                "battery_cycle": dp2(battery_cycle),
                "actual_cost": dp2(cost_yesterday),
                "predicted_cost": dp2(metric),
                "friendly_name": "PV/Battery system savings yesterday",
                "state_class": "measurement",
                "unit_of_measurement": self.currency_symbols[1],
                "icon": "mdi:currency-usd",
            },
        )

        # Restore state
        self.minutes_now = minutes_now
        self.midnight_utc = midnight_utc
        self.forecast_minutes = forecast_minutes
        self.cost_today_sofar = cost_today_sofar
        self.import_today_now = import_today_now
        self.export_today_now = export_today_now
        self.carbon_today_sofar = carbon_today_sofar
        self.pv_today_now = pv_today_now
        self.soc_kw = soc_kw
        self.car_charging_hold = car_charging_hold
        self.iboost_energy_subtract = iboost_energy_subtract
        self.load_minutes_now = load_minutes_now
        self.soc_max = soc_max
        self.rate_import = rate_import
        self.rate_export = rate_export
        self.iboost_enable = iboost_enable
        self.num_cars = num_cars

    def publish_rate_and_threshold(self):
        """
        Publish energy rate data and thresholds
        """
        # Find discharging windows
        if self.rate_export:
            if self.rate_best_cost_threshold_export:
                self.rate_export_cost_threshold = self.rate_best_cost_threshold_export
                self.log("Export threshold used for optimisation was {}p".format(self.rate_export_cost_threshold))
            self.publish_rates(self.rate_export, True)

        # Find charging windows
        if self.rate_import:
            if self.rate_best_cost_threshold_charge:
                self.rate_import_cost_threshold = self.rate_best_cost_threshold_charge
                self.log("Import threshold used for optimisation was {}p".format(self.rate_import_cost_threshold))
            self.publish_rates(self.rate_import, False)

        # And gas
        if self.rate_gas:
            self.publish_rates(self.rate_gas, False, gas=True)

    def log_option_best(self):
        """
        Log options
        """
        opts = ""
        opts += "mode({}) ".format(self.predbat_mode)
        opts += "calculate_export_oncharge({}) ".format(self.calculate_export_oncharge)
        opts += "set_export_freeze_only({}) ".format(self.set_export_freeze_only)
        opts += "set_discharge_during_charge({}) ".format(self.set_discharge_during_charge)
        opts += "combine_charge_slots({}) ".format(self.combine_charge_slots)
        opts += "combine_export_slots({}) ".format(self.combine_export_slots)
        opts += "combine_rate_threshold({}) ".format(self.combine_rate_threshold)
        opts += "best_soc_min({} kWh) ".format(self.best_soc_min)
        opts += "best_soc_max({} kWh) ".format(self.best_soc_max)
        opts += "best_soc_keep({} kWh) ".format(self.best_soc_keep)
        opts += "inverter_loss({} %) ".format(int((1 - self.inverter_loss) * 100.0))
        opts += "battery_loss({} %) ".format(int((1 - self.battery_loss) * 100.0))
        opts += "battery_loss_discharge ({} %) ".format(int((1 - self.battery_loss_discharge) * 100.0))
        opts += "inverter_hybrid({}) ".format(self.inverter_hybrid)
        opts += "metric_min_improvement({} p) ".format(self.metric_min_improvement)
        opts += "metric_min_improvement_export({} p) ".format(self.metric_min_improvement_export)
        opts += "metric_min_improvement_export_freeze({} p) ".format(self.metric_min_improvement_export_freeze)
        opts += "metric_battery_cycle({} p/kWh) ".format(self.metric_battery_cycle)
        opts += "metric_battery_value_scaling({} x) ".format(self.metric_battery_value_scaling)
        if self.carbon_enable:
            opts += "metric_carbon({} p/Kg) ".format(self.carbon_metric)
        self.log("Calculate Best options: " + opts)

    def history_to_future_rates(self, rates, offset):
        """
        Shift rates from the past into a future array
        """
        future_rates = {}
        for minute in range(0, self.forecast_minutes):
            future_rates[minute] = rates.get(minute - offset, 0.0)
        return future_rates

    def window_as_text(self, windows, percents, ignore_min=False, ignore_max=False):
        """
        Convert window in minutes to text string
        """
        txt = "[ "
        first_window = True
        for window_n in range(len(windows)):
            window = windows[window_n]
            percent = percents[window_n]
            average = window["average"]

            if ignore_min and percent == 0.0:
                continue
            if ignore_max and percent == 100.0:
                continue

            if not first_window:
                txt += ", "
            first_window = False
            start_timestamp = self.midnight_utc + timedelta(minutes=window["start"])
            start_time = start_timestamp.strftime("%d-%m %H:%M:%S")
            end_timestamp = self.midnight_utc + timedelta(minutes=window["end"])
            end_time = end_timestamp.strftime("%d-%m %H:%M:%S")
            txt += start_time + " - "
            txt += end_time
            txt += " @ {}{} {}%".format(dp2(average), self.currency_symbols[1], dp2(percent))
        txt += " ]"
        return txt

    def dashboard_item(self, entity, state, attributes, app=None):
        """
        Publish state and log dashboard item
        """
        self.set_state_wrapper(entity_id=entity, state=state, attributes=attributes)
        if app:
            self.dashboard_index_app[entity] = app
        else:
            if entity not in self.dashboard_index:
                self.dashboard_index.append(entity)
        self.dashboard_values[entity] = {}
        self.dashboard_values[entity]["state"] = state
        self.dashboard_values[entity]["attributes"] = attributes

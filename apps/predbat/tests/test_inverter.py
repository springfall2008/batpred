# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import json
import copy
import yaml
import os
from datetime import datetime, timedelta
from utils import calc_percent_limit
from tests.test_infra import TestHAInterface
from predbat import PredBat
from const import MINUTE_WATT, INVERTER_MAX_RETRY_REST
from inverter import Inverter


def dummy_sleep(seconds):
    """
    Dummy sleep function
    """
    pass


class DummyRestAPI:
    def __init__(self):
        self.commands = []
        self.rest_data = {}
        self.queued_rest = []

    def queue_rest_data(self, data):
        # print("Queue rest data {}".format(data))
        self.queued_rest.append(copy.deepcopy(data))

    def clear_queue(self):
        self.queued_rest = []

    def dummy_rest_postCommand(self, url, json):
        """
        Dummy rest post command
        """
        # print("Dummy rest post command {} {}".format(url, json))
        self.commands.append([url, json])

    def dummy_rest_getData(self, url):
        if url == "dummy/runAll":
            if self.queued_rest:
                self.rest_data = self.queued_rest.pop(0)
            # print("Dummy rest get data {} returns {}".format(url, self.rest_data))
            return self.rest_data
        elif url == "dummy/readData":
            # print("Dummy rest get data {} returns {}".format(url, self.rest_data))
            return self.rest_data
        else:
            return None

    def get_commands(self):
        commands = self.commands
        self.commands = []
        return commands


def test_disable_charge_window(test_name, ha, inv, dummy_rest, prev_charge_start_time, prev_charge_end_time, prev_enable_charge, minutes_now, has_charge_enable_time=True, has_inverter_time_button_press=False, expect_inverter_time_button_press=False):
    """
    Test the disable charge window function
    """
    failed = False
    print("Test: {} has_charge_enable_time={} has_inverter_time_button_press={} prev_enable_charge={}".format(test_name, has_charge_enable_time, has_inverter_time_button_press, prev_enable_charge))

    inv.rest_data = None
    inv.inv_time_button_press = has_inverter_time_button_press
    inv.inv_has_charge_enable_time = has_charge_enable_time
    ha.dummy_items["select.charge_start_time"] = prev_charge_start_time
    ha.dummy_items["select.charge_end_time"] = prev_charge_end_time
    ha.dummy_items["switch.scheduled_charge_enable"] = "on" if prev_enable_charge else "off"
    ha.dummy_items["switch.inverter_button"] = "off"

    inv.disable_charge_window()

    if not has_charge_enable_time and ha.get_state("select.charge_start_time") != "00:00:00":
        print("ERROR: Charge start time should be 00:00:00 got {}".format(ha.get_state("select.charge_start_time")))
        failed = True
    if not has_charge_enable_time and ha.get_state("select.charge_end_time") != "00:00:00":
        print("ERROR: Charge end time should be 00:00:00 got {}".format(ha.get_state("select.charge_end_time")))
        failed = True
    if ha.get_state("switch.scheduled_charge_enable") != "off":
        print("ERROR: Charge enable should be off got {}".format(ha.get_state("switch.scheduled_charge_enable")))
        failed = True
    if ha.get_state("switch.inverter_button") != ("on" if expect_inverter_time_button_press else "off"):
        print("ERROR: Inverter time button press should be {} got {}".format("on" if expect_inverter_time_button_press else "off", ha.get_state("switch.inverter_button")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Timeslots"] = {}
    inv.rest_data["Timeslots"]["Charge_start_time_slot_1"] = prev_charge_start_time
    inv.rest_data["Timeslots"]["Charge_end_time_slot_1"] = prev_charge_end_time
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Enable_Charge_Schedule"] = "enable" if prev_enable_charge else "disabled"
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = "00:00:00"
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = "00:00:00"
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = "disabled"

    print("REST Mode")
    inv.disable_charge_window()
    print("After disable charge window")
    rest_command = dummy_rest.get_commands()
    charge_start_time = "00:00:00"
    charge_end_time = "00:00:00"
    if (prev_charge_start_time != charge_start_time or prev_charge_end_time != charge_end_time) and not has_charge_enable_time:
        expect_data = [["dummy/setChargeSlot1", {"start": charge_start_time[0:5], "finish": charge_end_time[0:5]}]]
    else:
        expect_data = []
    if prev_enable_charge and has_charge_enable_time:
        expect_data.append(["dummy/enableChargeSchedule", {"state": "disable"}])

    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True
    return failed


def test_adjust_charge_window(
    test_name,
    ha,
    inv,
    dummy_rest,
    prev_charge_start_time,
    prev_charge_end_time,
    prev_enable_charge,
    charge_start_time,
    charge_end_time,
    minutes_now,
    short=False,
    has_inverter_time_button_press=False,
    expect_inverter_time_button_press=False,
    expect_charge_start_time_minutes=None,
    expect_charge_end_time_minutes=None,
):
    """
    test:
        inv.adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
    """
    failed = False
    print("Test: {}".format(test_name))

    inv.rest_data = None
    inv.inv_time_button_press = has_inverter_time_button_press
    ha.dummy_items["select.charge_start_time"] = prev_charge_start_time[:5] if short else prev_charge_start_time
    ha.dummy_items["select.charge_end_time"] = prev_charge_end_time[:5] if short else prev_charge_end_time
    ha.dummy_items["switch.scheduled_charge_enable"] = "on" if prev_enable_charge else "off"
    ha.dummy_items["switch.inverter_button"] = "off"
    charge_start_time_tm = datetime.strptime(charge_start_time, "%H:%M:%S")
    charge_end_time_tm = datetime.strptime(charge_end_time, "%H:%M:%S")

    inv.adjust_charge_window(charge_start_time_tm, charge_end_time_tm, minutes_now)

    if short:
        expect_charge_start_time = charge_start_time[:5]
        expect_charge_end_time = charge_end_time[:5]
    else:
        expect_charge_start_time = charge_start_time
        expect_charge_end_time = charge_end_time

    if ha.get_state("select.charge_start_time") != expect_charge_start_time:
        print("ERROR: Charge start time should be {} got {}".format(expect_charge_start_time, ha.get_state("select.charge_start_time")))
        failed = True
    if ha.get_state("select.charge_end_time") != expect_charge_end_time:
        print("ERROR: Charge end time should be {} got {}".format(expect_charge_end_time, ha.get_state("select.charge_end_time")))
        failed = True
    if ha.get_state("switch.scheduled_charge_enable") != "on":
        print("ERROR: Charge enable should be on got {}".format(ha.get_state("switch.scheduled_charge_enable")))
        failed = True
    if ha.get_state("switch.inverter_button") != ("on" if expect_inverter_time_button_press else "off"):
        print("ERROR: Inverter time button press should be {} got {}".format("on" if expect_inverter_time_button_press else "off", ha.get_state("switch.inverter_button")))
        failed = True

    # Verify charge_start_time_minutes and charge_end_time_minutes if expected values provided
    if expect_charge_start_time_minutes is not None:
        if inv.charge_start_time_minutes != expect_charge_start_time_minutes:
            print("ERROR: charge_start_time_minutes should be {} got {}".format(expect_charge_start_time_minutes, inv.charge_start_time_minutes))
            failed = True
    if expect_charge_end_time_minutes is not None:
        if inv.charge_end_time_minutes != expect_charge_end_time_minutes:
            print("ERROR: charge_end_time_minutes should be {} got {}".format(expect_charge_end_time_minutes, inv.charge_end_time_minutes))
            failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Timeslots"] = {}
    inv.rest_data["Timeslots"]["Charge_start_time_slot_1"] = prev_charge_start_time
    inv.rest_data["Timeslots"]["Charge_end_time_slot_1"] = prev_charge_end_time
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Enable_Charge_Schedule"] = "enable" if prev_enable_charge else "disable"
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = charge_start_time
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = charge_end_time
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = "enable"

    inv.adjust_charge_window(charge_start_time_tm, charge_end_time_tm, minutes_now)
    rest_command = dummy_rest.get_commands()
    if prev_charge_start_time != charge_start_time or prev_charge_end_time != charge_end_time:
        expect_data = [["dummy/setChargeSlot1", {"start": charge_start_time[0:5], "finish": charge_end_time[0:5]}]]
    else:
        expect_data = []
    if prev_enable_charge != True:
        expect_data.append(["dummy/enableChargeSchedule", {"state": "enable"}])

    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True
    return failed


def test_adjust_reserve(test_name, ha, inv, dummy_rest, prev_reserve, reserve, expect_reserve=None, reserve_min=4, reserve_max=100, reserve_percent=None):
    """
    Test
       inv.adjust_reserve(self, reserve):
    """
    failed = False
    if expect_reserve is None:
        expect_reserve = reserve

    # reserve_percent simulates the current planning floor (may differ from reserve_min for
    # inverters where inv_has_reserve_soc is False, e.g. SolisCloud where the sensor can be "stuck" high)
    if reserve_percent is None:
        reserve_percent = reserve_min
    inv.reserve_percent = reserve_percent
    inv.reserve_min = reserve_min
    inv.reserve_max = reserve_max

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    ha.dummy_items["number.reserve"] = prev_reserve
    inv.adjust_reserve(reserve)
    if ha.get_state("number.reserve") != expect_reserve:
        print("ERROR: Reserve should be {} got {}".format(expect_reserve, ha.get_state("number.reserve")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Battery_Power_Reserve"] = prev_reserve
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"]["Battery_Power_Reserve"] = expect_reserve

    inv.adjust_reserve(reserve)
    rest_command = dummy_rest.get_commands()
    if prev_reserve != expect_reserve:
        expect_data = [["dummy/setBatteryReserve", {"reservePercent": expect_reserve}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_force_export(test_name, ha, inv, dummy_rest, prev_start, prev_end, prev_force_export, prev_discharge_target, new_start, new_end, new_force_export, has_inv_time_button_press=False, expect_inv_time_button_press=False):
    """
    Test
       inv.adjust_reserve(self, reserve):
    """
    failed = False

    print("Test: {} - non-REST".format(test_name))

    if new_start is None:
        new_start = prev_start
    if new_end is None:
        new_end = prev_end

    prev_mode = "Timed Export" if prev_force_export else "Eco"
    new_mode = "Timed Export" if new_force_export else "Eco"
    prev_force_export = "on" if prev_force_export else "off"
    export_schedule_discharge = "on" if new_force_export else "off"

    # Non-REST Mode
    inv.rest_data = None
    inv.reserve_precent = 4
    inv.inv_has_charge_enable_time = False
    inv.ge_inverter_mode = True
    inv.rest_v3 = True
    inv.inv_time_button_press = has_inv_time_button_press

    if inv.ge_inverter_mode and not new_force_export:
        expect_start = prev_start
        expect_end = prev_end
    else:
        expect_start = new_start
        expect_end = new_end

    new_discharge_target = inv.reserve_precent if new_force_export else prev_discharge_target

    ha.dummy_items["select.discharge_start_time"] = prev_start
    ha.dummy_items["select.discharge_end_time"] = prev_end
    ha.dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"] = prev_force_export
    ha.dummy_items["number.discharge_target_soc"] = prev_discharge_target
    ha.dummy_items["select.inverter_mode"] = prev_mode
    ha.dummy_items["switch.inverter_button"] = "off"

    new_start_timestamp = datetime.strptime(new_start, "%H:%M:%S")
    new_end_timestamp = datetime.strptime(new_end, "%H:%M:%S")
    inv.adjust_force_export(new_force_export, new_start_timestamp, new_end_timestamp)

    if ha.get_state("sensor.predbat_GE_0_scheduled_discharge_enable") != export_schedule_discharge:
        print("ERROR: scheduled discharge enable should be {} got {}".format(export_schedule_discharge, ha.get_state("sensor.predbat_GE_0_scheduled_discharge_enable")))
        failed = True
    if ha.get_state("select.discharge_start_time") != expect_start:
        print("ERROR: Discharge start time should be {} got {}".format(new_start, ha.get_state("select.discharge_start_time")))
        failed = True
    if ha.get_state("select.discharge_end_time") != expect_end:
        print("ERROR: Discharge end time should be {} got {}".format(new_end, ha.get_state("select.discharge_end_time")))
        failed = True
    if ha.get_state("number.discharge_target_soc") != new_discharge_target:
        print("ERROR: Discharge target soc should be {} got {}".format(new_discharge_target, ha.get_state("number.discharge_target_soc")))
        failed = True
    if ha.get_state("select.inverter_mode") != new_mode:
        print("ERROR: Inverter mode should be {} got {}".format(new_mode, ha.get_state("select.inverter_mode")))
        failed = True
    if ha.get_state("switch.inverter_button") != ("on" if expect_inv_time_button_press else "off"):
        print("ERROR: Inverter button press should be {} got {}".format("on" if expect_inv_time_button_press else "off", ha.get_state("switch.inverter_button")))
        failed = True

    print("Test: {} - REST".format(test_name))
    # REST Mode
    inv.rest_api = "dummy"
    inv.reserve_precent = 4
    inv.inv_has_charge_enable_time = False

    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Enable_Discharge_Schedule"] = prev_force_export
    inv.rest_data["Control"]["Mode"] = prev_mode
    inv.rest_data["Timeslots"] = {}
    inv.rest_data["Timeslots"]["Discharge_start_time_slot_1"] = prev_start
    inv.rest_data["Timeslots"]["Discharge_end_time_slot_1"] = prev_end
    inv.rest_data["raw"] = {}
    inv.rest_data["raw"]["invertor"] = {}
    inv.rest_data["raw"]["invertor"]["discharge_target_soc_1"] = prev_discharge_target

    dummy_rest.clear_queue()
    dummy1 = copy.deepcopy(inv.rest_data)

    dummy1["raw"]["invertor"]["discharge_target_soc_1"] = inv.reserve_precent if new_force_export else prev_discharge_target
    if new_discharge_target != prev_discharge_target:
        dummy_rest.queue_rest_data(dummy1)

    dummy1["Timeslots"]["Discharge_start_time_slot_1"] = new_start
    dummy1["Timeslots"]["Discharge_end_time_slot_1"] = new_end
    if prev_start != expect_start or prev_end != expect_end:
        dummy_rest.queue_rest_data(dummy1)

    dummy1["Control"]["Mode"] = new_mode
    dummy1["Control"]["Enable_Discharge_Schedule"] = export_schedule_discharge
    if prev_mode != new_mode:
        dummy_rest.queue_rest_data(dummy1)

    dummy_rest.rest_data = copy.deepcopy(dummy1)

    new_start_timestamp = datetime.strptime(new_start, "%H:%M:%S")
    new_end_timestamp = datetime.strptime(new_end, "%H:%M:%S")

    print("Inv prev mode {} new mode {}".format(prev_mode, new_mode))
    print(dummy_rest.rest_data)
    print(inv.rest_data)
    inv.adjust_force_export(new_force_export, new_start_timestamp, new_end_timestamp)

    rest_command = dummy_rest.get_commands()
    expect_data = []
    if new_discharge_target != prev_discharge_target:
        expect_data.append(["dummy/setDischargeTarget", {"dischargeToPercent": int(new_discharge_target), "slot": 1}])

    if prev_start != expect_start or prev_end != expect_end:
        expect_data.append(["dummy/setDischargeSlot1", {"start": expect_start[0:5], "finish": expect_end[0:5]}])

    if prev_mode != new_mode:
        expect_data.append(["dummy/setBatteryMode", {"mode": new_mode}])

    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_charge_rate(test_name, ha, inv, dummy_rest, prev_rate, rate, expect_rate=None, discharge=False):
    """
    Test the adjust_inverter_mode function
    """
    failed = False
    if expect_rate is None:
        expect_rate = rate

    print("Test: {} prev_rate {} rate {} expect_rate {}".format(test_name, prev_rate, rate, expect_rate))

    # Non-REST Mode
    inv.rest_data = None
    inv.rest_api = None
    entity = "number.discharge_rate" if discharge else "number.charge_rate"
    entity_percent = "number.discharge_rate_percent" if discharge else "number.charge_rate_percent"
    expect_percent = int(expect_rate * 100 / inv.battery_rate_max_raw)
    ha.dummy_items[entity] = prev_rate
    ha.dummy_items[entity_percent] = int(prev_rate * 100 / inv.battery_rate_max_raw)
    if discharge:
        inv.adjust_discharge_rate(rate)
    else:
        inv.adjust_charge_rate(rate)
    if ha.get_state(entity) != expect_rate:
        print("ERROR: Inverter rate should be {} got {}".format(expect_rate, ha.get_state(entity)))
        failed = True
    if ha.get_state(entity_percent) != expect_percent:
        print("ERROR: Inverter rate percent should be {} got {} - rate {} max_rate_raw {}".format(expect_percent, ha.get_state(entity_percent), rate, inv.battery_rate_max_raw))
        failed = True

    # REST Mode
    rest_entity = "Battery_Discharge_Rate" if discharge else "Battery_Charge_Rate"
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"][rest_entity] = prev_rate
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"][rest_entity] = expect_rate

    rest_command = dummy_rest.get_commands()
    if rest_command:
        print("ERROR Previous was command was not cleared, started with:".format(rest_command))
        failed = True

    if discharge:
        inv.adjust_discharge_rate(rate)
    else:
        inv.adjust_charge_rate(rate)

    rest_command = dummy_rest.get_commands()
    if prev_rate != expect_rate:
        print("Prev_rate {} expect_rate {}".format(prev_rate, expect_rate))
        if discharge:
            expect_data = [["dummy/setDischargeRate", {"dischargeRate": expect_rate}]]
        else:
            expect_data = [["dummy/setChargeRate", {"chargeRate": expect_rate}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_inverter_mode(test_name, ha, inv, dummy_rest, prev_mode, mode, expect_mode=None):
    """
    Test the adjust_inverter_mode function
    """
    failed = False
    if expect_mode is None:
        expect_mode = mode

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    ha.dummy_items["select.inverter_mode"] = prev_mode
    inv.adjust_inverter_mode(True if mode == "Timed Export" else False, False)
    if ha.get_state("select.inverter_mode") != expect_mode:
        print("ERROR: Inverter mode should be {} got {}".format(expect_mode, ha.get_state("select.inverter_mode")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Mode"] = prev_mode
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"]["Mode"] = expect_mode

    inv.adjust_inverter_mode(True if mode == "Timed Export" else False, False)
    rest_command = dummy_rest.get_commands()
    if prev_mode != expect_mode:
        expect_data = [["dummy/setBatteryMode", {"mode": expect_mode}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_ge_eco_toggle(test_name, ha, inv, prev_eco_state, force_export, expect_eco_state=None):
    """
    Test adjust_inverter_mode with inv_has_ge_eco_toggle=True (e.g. GivEnergy Cloud / GEC inverter type).

    The eco toggle is a switch entity. When force_export=True the switch is turned off (eco disabled),
    when force_export=False the switch is turned on (eco enabled).
    """
    failed = False
    if expect_eco_state is None:
        expect_eco_state = "off" if force_export else "on"

    print("Test: {} prev_eco={} force_export={} expect_eco={}".format(test_name, prev_eco_state, force_export, expect_eco_state))

    # Save original state so we can restore after the test
    orig_has_ge_eco_toggle = inv.inv_has_ge_eco_toggle
    orig_has_ge_inverter_mode = inv.inv_has_ge_inverter_mode
    orig_inverter_mode_arg = inv.base.args.get("inverter_mode")

    # Configure inverter for GEC-style eco toggle
    inv.inv_has_ge_eco_toggle = True
    inv.inv_has_ge_inverter_mode = False
    inv.rest_data = None
    inv.rest_api = None

    # Point inverter_mode arg at a switch entity (the eco toggle)
    inv.base.args["inverter_mode"] = "switch.enable_eco_mode"
    ha.dummy_items["switch.enable_eco_mode"] = prev_eco_state

    inv.adjust_inverter_mode(force_export, False)

    actual_state = ha.get_state("switch.enable_eco_mode")
    if actual_state != expect_eco_state:
        print("ERROR: ECO toggle switch should be '{}' got '{}'".format(expect_eco_state, actual_state))
        failed = True

    # Restore original inverter state
    inv.inv_has_ge_eco_toggle = orig_has_ge_eco_toggle
    inv.inv_has_ge_inverter_mode = orig_has_ge_inverter_mode
    inv.base.args["inverter_mode"] = orig_inverter_mode_arg

    return failed


def test_adjust_ge_eco_toggle_missing_entity(test_name, inv, force_export, inverter_mode_arg, expect_warning):
    """
    Test GE eco-toggle adjust_inverter_mode when inverter_mode entity is missing.

    inverter_mode_arg:
      - "unset": remove inverter_mode from args (expect warning)
      - None: explicitly set inverter_mode to None (expect no warning)
    """
    failed = False
    print("Test: {} force_export={}".format(test_name, force_export))

    orig_has_ge_eco_toggle = inv.inv_has_ge_eco_toggle
    orig_has_ge_inverter_mode = inv.inv_has_ge_inverter_mode
    orig_has_inverter_mode_arg = "inverter_mode" in inv.base.args
    orig_inverter_mode_arg = inv.base.args.get("inverter_mode")

    inv.inv_has_ge_eco_toggle = True
    inv.inv_has_ge_inverter_mode = False
    inv.rest_data = None
    inv.rest_api = None
    if inverter_mode_arg == "unset":
        inv.base.args.pop("inverter_mode", None)
    else:
        inv.base.args["inverter_mode"] = inverter_mode_arg
    log_messages = []
    orig_log = inv.base.log
    orig_inv_log = inv.log
    inv.base.log = lambda msg, *args, **kwargs: log_messages.append(str(msg))
    inv.log = lambda msg, *args, **kwargs: log_messages.append(str(msg))

    try:
        inv.adjust_inverter_mode(force_export, False)
    except Exception as exc:
        print("ERROR: adjust_inverter_mode should not raise when inverter_mode entity is missing, got {}".format(exc))
        failed = True

    inv.inv_has_ge_eco_toggle = orig_has_ge_eco_toggle
    inv.inv_has_ge_inverter_mode = orig_has_ge_inverter_mode
    inv.base.log = orig_log
    inv.log = orig_inv_log
    if orig_has_inverter_mode_arg:
        inv.base.args["inverter_mode"] = orig_inverter_mode_arg
    else:
        inv.base.args.pop("inverter_mode", None)
    warning_found = any("No entity_id for ECO Toggle" in msg for msg in log_messages)
    if warning_found != expect_warning:
        print("ERROR: expected warning {} got {}".format(expect_warning, warning_found))
        failed = True

    return failed


def test_adjust_battery_target(test_name, ha, inv, dummy_rest, prev_soc, soc, isCharging, isExporting, expect_soc=None, has_inv_time_button_press=False, expect_button_press=False):
    """
    Test the adjust_battery_target function
    """
    failed = False
    if expect_soc is None:
        expect_soc = soc

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    inv.inv_time_button_press = has_inv_time_button_press
    ha.dummy_items["number.charge_limit"] = prev_soc
    ha.dummy_items["switch.inverter_button"] = "off"
    inv.adjust_battery_target(soc, isCharging=isCharging, isExporting=isExporting)
    if ha.get_state("number.charge_limit") != expect_soc:
        print("ERROR: Charge limit should be {} got {}".format(expect_soc, ha.get_state("number.charge_limit")))
        failed = True

    # Check button was pressed if expected (for Fox inverters)
    button_state = ha.get_state("switch.inverter_button")
    expected_button_state = "on" if expect_button_press else "off"
    if button_state != expected_button_state:
        print("ERROR: Button state should be {} got {}".format(expected_button_state, button_state))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Target_SOC"] = prev_soc
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"]["Target_SOC"] = expect_soc
    # enableChargeTarget always enables (True), so set Enable_Charge_Target to "enable" so it passes on first try
    dummy_rest.rest_data["Control"]["Enable_Charge_Target"] = "enable"

    inv.adjust_battery_target(soc, isCharging=isCharging, isExporting=isExporting)
    rest_command = dummy_rest.get_commands()
    if expect_soc != prev_soc:
        expect_data = [
            ["dummy/enableChargeTarget", {"state": "enable"}],
            ["dummy/setChargeTarget", {"chargeToPercent": expect_soc}],
        ]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_battery_target_charge_limit_enable(test_name, my_predbat, ha, inv, prev_soc, soc, expect_soc, expect_enable_written):
    """
    Test that adjust_battery_target writes the charge_limit_enable switch when charge_limit_enable is in args.
    """
    failed = False
    print("Test: {}".format(test_name))

    inv.rest_data = None
    inv.inv_time_button_press = False
    ha.dummy_items["number.charge_limit"] = prev_soc
    ha.dummy_items["switch.charge_limit_enable"] = "off"
    my_predbat.args["charge_limit_enable"] = "switch.charge_limit_enable"

    inv.adjust_battery_target(soc, isCharging=True, isExporting=False)

    actual_soc = ha.get_state("number.charge_limit")
    if actual_soc != expect_soc:
        print("ERROR: {}: charge_limit should be {} got {}".format(test_name, expect_soc, actual_soc))
        failed = True

    enable_state = ha.get_state("switch.charge_limit_enable")
    expected_enable_state = "on" if expect_enable_written else "off"
    if enable_state != expected_enable_state:
        print("ERROR: {}: charge_limit_enable should be {} got {}".format(test_name, expected_enable_state, enable_state))
        failed = True

    # Clean up so remaining tests are not affected
    del my_predbat.args["charge_limit_enable"]

    return failed


def test_rest_enable_charge_target(test_name, ha, inv, dummy_rest, enable, expect_commands, queued_rest_states=None, initial_state=None):
    """
    Test rest_enableChargeTarget: verifies the correct REST command is issued and the retry logic works.

    initial_state: the Enable_Charge_Target value already in inv.rest_data before the call.
                   Defaults to the opposite of enable so the guard fires.
    queued_rest_states: list of Enable_Charge_Target values to return on successive runAll calls.
    When None, the dummy_rest.rest_data is pre-set to the expected state so it passes on the first try.
    """
    failed = False
    print("Test: {}".format(test_name))

    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    # Start with the opposite state by default so the guard always fires
    if initial_state is None:
        initial_state = "disable" if enable else "enable"
    inv.rest_data["Control"]["Enable_Charge_Target"] = initial_state

    if queued_rest_states is not None:
        for state in queued_rest_states:
            data = {"Control": {"Enable_Charge_Target": state}}
            dummy_rest.queue_rest_data(data)
    else:
        dummy_rest.rest_data = {"Control": {"Enable_Charge_Target": "enable" if enable else "disable"}}

    inv.rest_enableChargeTarget(enable)

    rest_commands = dummy_rest.get_commands()
    if json.dumps(expect_commands) != json.dumps(rest_commands):
        print("ERROR: {}: rest commands should be {} got {}".format(test_name, expect_commands, rest_commands))
        failed = True

    dummy_rest.clear_queue()
    return failed


def test_inverter_self_test(test_name, my_predbat):
    """
    Test the inverter self test function.
    """
    failed = 0

    print("**** Running Test: {} ****".format(test_name))
    # Call self test - doesn't really check much as such except the code isn't dead
    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    dummy_rest.rest_data = {}
    dummy_rest.rest_data["Control"] = {}
    dummy_rest.rest_data["Control"]["Target_SOC"] = 99
    dummy_rest.rest_data["Control"]["Mode"] = "Eco"
    dummy_rest.rest_data["Control"]["Battery_Power_Reserve"] = 4.0
    dummy_rest.rest_data["Control"]["Battery_Charge_Rate"] = 1100
    dummy_rest.rest_data["Control"]["Battery_Discharge_Rate"] = 1500
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = "enable"
    dummy_rest.rest_data["Control"]["Enable_Discharge_Schedule"] = "enable"
    dummy_rest.rest_data["Timeslots"] = {}
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = "00:30:00"
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = "22:00:00"
    dummy_rest.rest_data["Timeslots"]["Discharge_start_time_slot_1"] = "01:00:00"
    dummy_rest.rest_data["Timeslots"]["Discharge_end_time_slot_1"] = "02:30:00"
    dummy_rest.rest_data["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"]["SOC_kWh"] = 1.0
    dummy_rest.rest_data["Power"]["Power"]["Battery_Power"] = 100
    dummy_rest.rest_data["Power"]["Power"]["PV_Power"] = 200
    dummy_rest.rest_data["Power"]["Power"]["Load_Power"] = 300

    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData)
    inv.sleep = dummy_sleep
    inv.self_test(my_predbat.minutes_now)
    rest = dummy_rest.get_commands()
    repeats = INVERTER_MAX_RETRY_REST  # configurable number of repeats
    expected = []

    # Define the command patterns (each repeated INVERTER_MAX_RETRY_REST times due to the retry loop).
    # Enable_Charge_Target is not set in the mock rest_data so enableChargeTarget exhausts all retries,
    # same as setChargeTarget exhausts retries because Target_SOC stays at 99.
    commands = [
        ["dummy/enableChargeTarget", {"state": "enable"}],
        ["dummy/setChargeTarget", {"chargeToPercent": 100}],
        ["dummy/setChargeRate", {"chargeRate": 215}],
        ["dummy/setChargeRate", {"chargeRate": 0}],
        ["dummy/setDischargeRate", {"dischargeRate": 220}],
        ["dummy/setDischargeRate", {"dischargeRate": 0}],
        ["dummy/setBatteryReserve", {"reservePercent": 100}],
        ["dummy/setBatteryReserve", {"reservePercent": 6}],
        ["dummy/enableChargeSchedule", {"state": "disable"}],
        ["dummy/setChargeSlot1", {"start": "23:01", "finish": "05:01"}],
        ["dummy/setChargeSlot1", {"start": "23:00", "finish": "05:00"}],
        ["dummy/setDischargeSlot1", {"start": "23:00", "finish": "23:01"}],
        ["dummy/setBatteryMode", {"mode": "Timed Export"}],
    ]

    # Generate expected list with repeats
    for command in commands:
        for _ in range(repeats):
            expected.append(command)
    if json.dumps(expected) != json.dumps(rest):
        print("ERROR: Self test should be {} got {}".format(expected, rest))
        failed = True
    return failed


def test_inverter_rest_template(
    test_name,
    my_predbat,
    filename,
    assert_soc_max=9.52,
    assert_soc=0,
    assert_voltage=52,
    assert_inverter_limit=3600,
    assert_battery_rate_max=2600,
    assert_serial_number="Unknown",
    assert_pv_power=0,
    assert_load_power=0,
    assert_charge_start_time_minutes=0,
    assert_charge_end_time_minutes=0,
    assert_charge_enable=False,
    assert_discharge_start_time_minutes=0,
    assert_discharge_end_time_minutes=0,
    assert_discharge_enable=False,
    assert_pause_start_time_minutes=0,
    assert_pause_end_time_minutes=0,
    assert_nominal_capacity=9.52,
    assert_battery_temperature=0,
):
    failed = False
    print("**** Running Test: {} ****".format(test_name))
    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    # Remove inverter_limit and export_limit from config to test REST data parsing
    if "inverter_limit" in my_predbat.args:
        del my_predbat.args["inverter_limit"]
    if "export_limit" in my_predbat.args:
        del my_predbat.args["export_limit"]

    dummy_rest.rest_data = {}
    with open(filename, "r") as file:
        dummy_rest.rest_data = json.load(file)

    my_predbat.restart_active = True
    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData, quiet=False)
    inv.sleep = dummy_sleep

    inv.update_status(my_predbat.minutes_now)
    my_predbat.restart_active = False

    if assert_soc_max != inv.soc_max:
        print("ERROR: SOC Max should be {} got {}".format(assert_soc_max, inv.soc_max))
        failed = True
    if assert_soc != inv.soc_kw:
        print("ERROR: SOC should be {} got {}".format(assert_soc, inv.soc_kw))
        failed = True
    if assert_voltage != inv.battery_voltage:
        print("ERROR: Voltage should be {} got {}".format(assert_voltage, inv.battery_voltage))
        failed = True
    if assert_inverter_limit != inv.inverter_limit * MINUTE_WATT:
        print("ERROR: Inverter limit should be {} got {}".format(assert_inverter_limit, inv.inverter_limit * MINUTE_WATT))
        failed = True
    # Verify export_limit defaults correctly from REST data when config unset (should be 99999.0 / MINUTE_WATT = 1.66665)
    if inv.export_limit * MINUTE_WATT < 99999.0:
        print("ERROR: Export limit should default to 99999 W (1.66665 kW/min) when unset, got {} W ({} kW/min)".format(inv.export_limit * MINUTE_WATT, inv.export_limit))
        failed = True
    if assert_battery_rate_max != inv.battery_rate_max_raw:
        print("ERROR: Battery rate max should be {} got {}".format(assert_battery_rate_max, inv.battery_rate_max_raw))
        failed = True
    if assert_serial_number != inv.serial_number:
        print("ERROR: Serial number should be {} got {}".format(assert_serial_number, inv.serial_number))
        failed = True
    if assert_pv_power != inv.pv_power:
        print("ERROR: PV power should be {} got {}".format(assert_pv_power, inv.pv_power))
        failed = True
    if assert_load_power != inv.load_power:
        print("ERROR: Load power should be {} got {}".format(assert_load_power, inv.load_power))
        failed = True
    if assert_charge_start_time_minutes != inv.charge_start_time_minutes:
        print("ERROR: Charge start time should be {} got {}".format(assert_charge_start_time_minutes, inv.charge_start_time_minutes))
        failed = True
    if assert_charge_end_time_minutes != inv.charge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(assert_charge_end_time_minutes, inv.charge_end_time_minutes))
        failed = True
    if assert_charge_enable != inv.charge_enable_time:
        print("ERROR: Charge enable should be {} got {}".format(assert_charge_enable, inv.charge_enable_time))
        failed = True
    if assert_discharge_start_time_minutes != inv.discharge_start_time_minutes:
        print("ERROR: Discharge start time should be {} got {}".format(assert_discharge_start_time_minutes, inv.discharge_start_time_minutes))
        failed = True
    if assert_discharge_end_time_minutes != inv.discharge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(assert_discharge_end_time_minutes, inv.discharge_end_time_minutes))
        failed = True
    if assert_discharge_enable != inv.discharge_enable_time:
        print("ERROR: Discharge enable should be {} got {}".format(assert_discharge_enable, inv.discharge_enable_time))
        failed = True
    if assert_nominal_capacity != inv.nominal_capacity:
        print("ERROR: Nominal capacity should be {} got {}".format(assert_nominal_capacity, inv.nominal_capacity))
        failed = True
    if assert_battery_temperature != inv.battery_temperature:
        print("ERROR: Battery temperature should be {} got {}".format(assert_battery_temperature, inv.battery_temperature))
        failed = True

    return failed


def test_inverter_update(
    test_name,
    my_predbat,
    dummy_items,
    expect_charge_start_time,
    expect_charge_end_time,
    expect_charge_enable,
    expect_discharge_start_time,
    expect_discharge_end_time,
    expect_discharge_enable,
    expect_battery_power,
    expect_pv_power,
    expect_load_power,
    expect_soc_kwh,
    soc_percent=False,
    expect_battery_capacity=10.0,
    has_charge_enable_time=True,
    has_discharge_enable_time=True,
):
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    midnight = datetime.strptime("00:00:00", "%H:%M:%S")
    charge_start_time_minutes = (datetime.strptime(expect_charge_start_time, "%H:%M:%S") - midnight).total_seconds() / 60
    charge_end_time_minutes = (datetime.strptime(expect_charge_end_time, "%H:%M:%S") - midnight).total_seconds() / 60
    discharge_start_time_minutes = (datetime.strptime(expect_discharge_start_time, "%H:%M:%S") - midnight).total_seconds() / 60
    discharge_end_time_minutes = (datetime.strptime(expect_discharge_end_time, "%H:%M:%S") - midnight).total_seconds() / 60

    if charge_end_time_minutes < charge_start_time_minutes:
        if charge_end_time_minutes < my_predbat.minutes_now:
            charge_end_time_minutes += 60 * 24
        else:
            charge_start_time_minutes -= 60 * 24

    if discharge_end_time_minutes < discharge_start_time_minutes:
        if discharge_end_time_minutes < my_predbat.minutes_now:
            discharge_end_time_minutes += 60 * 24
        else:
            discharge_start_time_minutes -= 60 * 24

    my_predbat.args["givtcp_rest"] = None
    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep

    inv.charge_start_time_minutes = -1
    inv.charge_end_time_minutes = -1
    inv.charge_enable_time = False
    inv.discharge_start_time_minutes = -1
    inv.discharge_end_time_minutes = -1
    inv.discharge_enable_time = False
    inv.battery_power = 0
    inv.pv_power = 0
    inv.load_power = 0
    inv.soc_kw = 0
    inv.inv_has_charge_enable_time = has_charge_enable_time
    inv.inv_has_discharge_enable_time = has_discharge_enable_time

    print("Test: Update Inverter")

    dummy_items["select.charge_start_time"] = expect_charge_start_time
    dummy_items["select.charge_end_time"] = expect_charge_end_time
    dummy_items["select.discharge_start_time"] = expect_discharge_start_time
    dummy_items["select.discharge_end_time"] = expect_discharge_end_time
    dummy_items["sensor.battery_power"] = expect_battery_power
    dummy_items["sensor.pv_power"] = expect_pv_power
    dummy_items["sensor.load_power"] = expect_load_power
    dummy_items["switch.scheduled_charge_enable"] = "on" if expect_charge_enable else "off"
    dummy_items["switch.scheduled_discharge_enable"] = "on" if expect_discharge_enable else "off"
    dummy_items["number.discharge_target_soc"] = 4
    dummy_items["sensor.battery_capacity"] = expect_battery_capacity
    dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"] = "on" if expect_discharge_enable else "off"
    dummy_items["switch.inverter_button"] = "off"
    print("sensor.predbat_GE_0_scheduled_discharge_enable = {}".format(dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"]))
    if not has_discharge_enable_time:
        dummy_items["switch.scheduled_discharge_enable"] = "n/a"

    if soc_percent:
        dummy_items["sensor.soc_kw"] = -1
        dummy_items["sensor.soc_percent"] = calc_percent_limit(expect_soc_kwh, expect_battery_capacity)
        if "soc_kw" in my_predbat.args:
            del my_predbat.args["soc_kw"]
        my_predbat.args["soc_percent"] = "sensor.soc_percent"
    else:
        dummy_items["sensor.soc_kw"] = expect_soc_kwh
        dummy_items["sensor.soc_percent"] = -1
        my_predbat.args["soc_kw"] = "sensor.soc_kw"
        if "soc_percent" in my_predbat.args:
            del my_predbat.args["soc_percent"]

    inv.update_status(my_predbat.minutes_now)
    if not has_charge_enable_time:
        if charge_start_time_minutes == charge_end_time_minutes:
            expect_charge_enable = False
        else:
            expect_charge_enable = True

    if not has_discharge_enable_time:
        if discharge_start_time_minutes == discharge_end_time_minutes:
            expect_discharge_enable = False
        else:
            expect_discharge_enable = True
        print("Set expect_discharge_enable to {}".format(expect_discharge_enable))

    if not expect_charge_enable:
        charge_start_time_minutes = 0
        charge_end_time_minutes = 0

    if charge_end_time_minutes < my_predbat.minutes_now:
        charge_start_time_minutes += 24 * 60
        charge_end_time_minutes += 24 * 60
    if discharge_end_time_minutes < my_predbat.minutes_now:
        discharge_start_time_minutes += 24 * 60
        discharge_end_time_minutes += 24 * 60

    if inv.charge_start_time_minutes != charge_start_time_minutes:
        print("ERROR: Charge start time should be {} got {} ({})".format(charge_start_time_minutes, inv.charge_start_time_minutes, dummy_items["select.charge_start_time"]))
        failed = True
    if inv.charge_end_time_minutes != charge_end_time_minutes:
        print("ERROR: Charge end time should be {} got {}".format(charge_end_time_minutes, inv.charge_end_time_minutes))
        failed = True
    if inv.charge_enable_time != expect_charge_enable:
        print("ERROR: Charge enable should be {} got {}".format(expect_charge_enable, inv.charge_enable_time))
        failed = True
    if inv.discharge_start_time_minutes != discharge_start_time_minutes:
        print("ERROR: Discharge start time should be {} got {}".format(discharge_start_time_minutes, inv.discharge_start_time_minutes))
        failed = True
    if inv.discharge_end_time_minutes != discharge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(discharge_end_time_minutes, inv.discharge_end_time_minutes))
        failed = True
    if inv.discharge_enable_time != expect_discharge_enable:
        print("ERROR: Discharge enable should be {} got {}".format(expect_discharge_enable, inv.discharge_enable_time))
        failed = True
    if inv.battery_power != expect_battery_power:
        print("ERROR: Battery power should be {} got {}".format(expect_battery_power, inv.battery_power))
        failed = True
    if inv.pv_power != expect_pv_power:
        print("ERROR: PV power should be {} got {}".format(expect_pv_power, inv.pv_power))
        failed = True
    if inv.load_power != expect_load_power:
        print("ERROR: Load power should be {} got {}".format(expect_load_power, inv.load_power))
        failed = True
    if inv.soc_kw != expect_soc_kwh:
        print("ERROR: SOC kWh should be {} got {}".format(expect_soc_kwh, inv.soc_kw))
        failed = True

    # REST Mode

    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    dummy_rest.rest_data = {}
    dummy_rest.rest_data["Control"] = {}
    dummy_rest.rest_data["Control"]["Target_SOC"] = 99
    dummy_rest.rest_data["Control"]["Mode"] = "Eco"
    dummy_rest.rest_data["Control"]["Battery_Power_Reserve"] = 4.0
    dummy_rest.rest_data["Control"]["Battery_Charge_Rate"] = 1100
    dummy_rest.rest_data["Control"]["Battery_Discharge_Rate"] = 1500
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = "enable" if expect_charge_enable else "disable"
    dummy_rest.rest_data["Control"]["Enable_Discharge_Schedule"] = "enable" if expect_discharge_enable else "disable"
    dummy_rest.rest_data["Timeslots"] = {}
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = expect_charge_start_time
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = expect_charge_end_time
    dummy_rest.rest_data["Timeslots"]["Discharge_start_time_slot_1"] = expect_discharge_start_time
    dummy_rest.rest_data["Timeslots"]["Discharge_end_time_slot_1"] = expect_discharge_end_time
    dummy_rest.rest_data["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"]["SOC_kWh"] = expect_soc_kwh
    dummy_rest.rest_data["Power"]["Power"]["Battery_Power"] = expect_battery_power
    dummy_rest.rest_data["Power"]["Power"]["PV_Power"] = expect_pv_power
    dummy_rest.rest_data["Power"]["Power"]["Load_Power"] = expect_load_power
    # Calculate Grid_Power from energy balance: Grid = Load - PV - Battery
    dummy_rest.rest_data["Power"]["Power"]["Grid_Power"] = expect_load_power - expect_pv_power - expect_battery_power
    dummy_rest.rest_data["Invertor_Details"] = {}
    dummy_rest.rest_data["Invertor_Details"]["Battery_Capacity_kWh"] = expect_battery_capacity
    dummy_rest.rest_data["raw"] = {}
    dummy_rest.rest_data["raw"]["invertor"] = {}
    dummy_rest.rest_data["raw"]["invertor"]["discharge_target_soc_1"] = 4
    dummy_items["sensor.soc_kw"] = -1
    dummy_items["sensor.battery_capacity"] = -1

    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData)
    inv.sleep = dummy_sleep

    print("Test: Update Inverter - REST")
    inv.update_status(my_predbat.minutes_now)

    if inv.charge_start_time_minutes != charge_start_time_minutes:
        print("ERROR: Charge start time should be {} got {} ({})".format(charge_start_time_minutes, inv.charge_start_time_minutes, dummy_items["select.charge_start_time"]))
        failed = True
    if inv.charge_end_time_minutes != charge_end_time_minutes:
        print("ERROR: Charge end time should be {} got {}".format(charge_end_time_minutes, inv.charge_end_time_minutes))
        failed = True
    if inv.charge_enable_time != expect_charge_enable:
        print("ERROR: Charge enable should be {} got {}".format(expect_charge_enable, inv.charge_enable_time))
        failed = True
    if inv.discharge_start_time_minutes != discharge_start_time_minutes:
        print("ERROR: Discharge start time should be {} got {}".format(discharge_start_time_minutes, inv.discharge_start_time_minutes))
        failed = True
    if inv.discharge_end_time_minutes != discharge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(discharge_end_time_minutes, inv.discharge_end_time_minutes))
        failed = True
    if inv.discharge_enable_time != expect_discharge_enable:
        print("ERROR: Discharge enable should be {} got {}".format(expect_discharge_enable, inv.discharge_enable_time))
        failed = True
    if inv.battery_power != expect_battery_power:
        print("ERROR: Battery power should be {} got {}".format(expect_battery_power, inv.battery_power))
        failed = True
    if inv.pv_power != expect_pv_power:
        print("ERROR: PV power should be {} got {}".format(expect_pv_power, inv.pv_power))
        failed = True
    if inv.load_power != expect_load_power:
        print("ERROR: Load power should be {} got {}".format(expect_load_power, inv.load_power))
        failed = True
    if inv.soc_kw != expect_soc_kwh:
        print("ERROR: SOC kWh should be {} got {}".format(expect_soc_kwh, inv.soc_kw))
        failed = True
    if inv.soc_max != expect_battery_capacity:
        print("ERROR: SOC Max should be {} got {}".format(expect_battery_capacity, inv.soc_max))
        failed = True

    my_predbat.args["soc_kw"] = "sensor.soc_kw"

    return failed


def test_auto_restart(test_name, my_predbat, ha, inv, dummy_items, service, expected, active=False, set_system_notify=None, expect_notify=False):
    print("**** Running Test: {} ****".format(test_name))
    failed = 0
    prev_service_store_enable = ha.service_store_enable
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.restart_active = active

    my_predbat.args["auto_restart"] = service

    # Set notification config if specified
    if set_system_notify is not None:
        my_predbat.expose_config("set_system_notify", set_system_notify, quiet=True)

    failed = 1 if not active else 0
    try:
        inv.auto_restart("Crashed")
    except Exception as e:
        failed = 0
        if str(e) != "Auto-restart triggered":
            print("ERROR: Auto-restart should be triggered got {}".format(e))
            failed = 1

    result = ha.get_service_store()

    # Check for notification if expected
    notify_found = False
    for call in result:
        if call[0].startswith("notify"):
            notify_found = True
            break

    if expect_notify and not notify_found:
        print("ERROR: Expected notification but none was sent")
        failed = 1
    elif not expect_notify and notify_found:
        print("ERROR: Did not expect notification but one was sent")
        failed = 1

    # Filter out notifications when checking expected service calls
    result_filtered = [call for call in result if not call[0].startswith("notify")]

    if json.dumps(expected) != json.dumps(result_filtered):
        print("ERROR: Auto-restart service should be {} got {}".format(expected, result_filtered))
        failed = 1
    ha.service_store_enable = prev_service_store_enable
    return failed


def test_call_adjust_charge_immediate(test_name, my_predbat, ha, inv, dummy_items, soc, repeat=False, freeze=False, clear=False, stop_discharge=False, charge_start_time="00:00:00", charge_end_time="23:55:00", no_freeze=False):
    """
    Tests;
        def adjust_charge_immediate(self, target_soc, freeze=False)
    """
    failed = False
    ha.service_store_enable = True
    if clear:
        ha.service_store = []

    print("**** Running Test: {} ****".format(test_name))

    my_predbat.args["charge_start_service"] = "charge_start"
    my_predbat.args["charge_stop_service"] = "charge_stop"
    if not no_freeze:
        my_predbat.args["charge_freeze_service"] = "charge_freeze"
    else:
        my_predbat.args["charge_freeze_service"] = None
    my_predbat.args["discharge_start_service"] = "discharge_start"
    my_predbat.args["discharge_stop_service"] = "discharge_stop"
    my_predbat.args["discharge_freeze_service"] = "discharge_freeze"
    my_predbat.args["charge_rate"] = "number.charge_rate"
    my_predbat.args["device_id"] = "DID0"
    if "charge_rate_percent" in my_predbat.args:
        del my_predbat.args["charge_rate_percent"]

    dummy_items["select.charge_start_time"] = charge_start_time
    dummy_items["select.charge_end_time"] = charge_end_time
    dummy_items["number.charge_rate"] = 1101

    power = 1101

    inv.adjust_charge_immediate(soc, freeze=freeze)
    result = ha.get_service_store()
    expected = []

    if repeat:
        pass
    elif soc == inv.soc_percent or freeze:
        if stop_discharge:
            expected.append(["discharge_stop", {"device_id": "DID0"}])
        expected.append(["charge_freeze", {"device_id": "DID0", "target_soc": soc, "power": power}])
    elif soc > 0 and (inv.has_target_soc or soc > inv.soc_percent):
        if stop_discharge:
            expected.append(["discharge_stop", {"device_id": "DID0"}])
        expected.append(["charge_start", {"device_id": "DID0", "target_soc": soc, "power": power}])
    else:
        expected.append(["charge_stop", {"device_id": "DID0"}])
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: Adjust charge immediate - charge service should be {} got {}".format(expected, result))
        failed = True

    ha.service_store_enable = False
    return failed


def test_call_adjust_export_immediate(test_name, my_predbat, ha, inv, dummy_items, soc, repeat=False, freeze=False, clear=False, charge_stop=False, discharge_start_time="00:00:00", discharge_end_time="23:55:00", no_freeze=False):
    """
    Tests;
        def adjust_export_immediate(self, target_soc, freeze=False)
    """
    failed = False
    ha.service_store_enable = True
    if clear:
        ha.service_store = []

    print("**** Running Test: {} ****".format(test_name))

    my_predbat.args["charge_start_service"] = "charge_start"
    my_predbat.args["charge_stop_service"] = "charge_stop"
    my_predbat.args["charge_freeze_service"] = "charge_freeze"
    my_predbat.args["discharge_start_service"] = "discharge_start"
    my_predbat.args["discharge_stop_service"] = "discharge_stop"
    if not no_freeze:
        my_predbat.args["discharge_freeze_service"] = "discharge_freeze"
    else:
        my_predbat.args["discharge_freeze_service"] = None
    my_predbat.args["device_id"] = "DID0"
    power = int(inv.battery_rate_max_discharge * MINUTE_WATT)

    dummy_items["select.discharge_start_time"] = discharge_start_time
    dummy_items["select.discharge_end_time"] = discharge_end_time

    inv.adjust_export_immediate(soc, freeze=freeze)
    result = ha.get_service_store()
    expected = []

    if repeat:
        pass
    elif freeze or soc == inv.soc_percent:
        if charge_stop:
            expected.append(["charge_stop", {"device_id": "DID0"}])
        expected.append(["discharge_freeze", {"device_id": "DID0", "target_soc": soc, "power": power}])
    elif soc < inv.soc_percent:
        if charge_stop:
            expected.append(["charge_stop", {"device_id": "DID0"}])
        expected.append(["discharge_start", {"device_id": "DID0", "target_soc": soc, "power": power}])
    else:
        if charge_stop:
            expected.append(["charge_stop", {"device_id": "DID0"}])
        else:
            expected.append(["discharge_stop", {"device_id": "DID0"}])
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: Adjust export immediate - discharge service should be {} got {}".format(expected, result))
        failed = True

    ha.service_store_enable = False
    return failed


def test_call_service_template(test_name, my_predbat, inv, service_name="test", domain="charge", data={}, extra_data={}, clear=True, repeat=False, service_template=None, expected_result=None, twice=True):
    """
    tests
        def call_service_template(self, service, data, domain="charge", extra_data={})
    """
    failed = False

    print("**** Running Test: {} ****".format(test_name))

    ha = my_predbat.ha_interface
    ha.service_store_enable = True
    service_call = service_name + "_service"

    if service_template:
        my_predbat.args[service_name] = service_template
    else:
        my_predbat.args[service_name] = service_call

    if clear:
        my_predbat.last_service_hash = {}

    inv.call_service_template(service_name, data, domain=domain, extra_data=extra_data)
    if repeat:
        expected = []
    else:
        expected = [[service_call, data]] if (expected_result is None) else expected_result

    result = ha.get_service_store()
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: {} service should be {} got {} - 1".format(service_name, json.dumps(expected), json.dumps(result)))
        failed = True

    if twice:
        inv.call_service_template(service_name, data, domain=domain, extra_data=extra_data)
        expected = []
        result = ha.get_service_store()
        if json.dumps(expected) != json.dumps(result):
            print("ERROR: {} service should be {} got {} - 2".format(service_name, expected, result))
            failed = True

    ha.service_store_enable = False
    return failed


def test_charge_window_none_illegal_time(test_name, my_predbat, dummy_items):
    """
    Test charge window handling when time is illegal (e.g., 'unknown')
    This should result in None after time_string_to_stamp and trigger safe defaults
    """
    failed = False
    print(f"**** Running Test: {test_name} ****")

    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep
    inv.inv_has_charge_enable_time = True

    # Set illegal time value that will cause time_string_to_stamp to return None
    dummy_items["select.charge_start_time"] = "unknown"
    dummy_items["select.charge_end_time"] = "unknown"
    dummy_items["switch.scheduled_charge_enable"] = "on"

    inv.update_status(my_predbat.minutes_now)

    # Should set safe defaults
    if inv.charge_enable_time != False:
        print(f"ERROR: {test_name} - charge_enable_time should be False, got {inv.charge_enable_time}")
        failed = True
    if inv.charge_start_time_minutes != my_predbat.forecast_minutes:
        print(f"ERROR: {test_name} - charge_start_time_minutes should be {my_predbat.forecast_minutes}, got {inv.charge_start_time_minutes}")
        failed = True
    if inv.charge_end_time_minutes != my_predbat.forecast_minutes:
        print(f"ERROR: {test_name} - charge_end_time_minutes should be {my_predbat.forecast_minutes}, got {inv.charge_end_time_minutes}")
        failed = True
    if inv.track_charge_start != "00:00:00":
        print(f"ERROR: {test_name} - track_charge_start should be '00:00:00', got {inv.track_charge_start}")
        failed = True
    if inv.track_charge_end != "00:00:00":
        print(f"ERROR: {test_name} - track_charge_end should be '00:00:00', got {inv.track_charge_end}")
        failed = True

    return failed


def test_charge_window_none_value(test_name, my_predbat, dummy_items):
    """
    Test charge window handling when value is None from get_arg returning None
    This happens when the entity exists but returns None
    """
    failed = False
    print(f"**** Running Test: {test_name} ****")

    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep
    inv.inv_has_charge_enable_time = True
    inv.rest_api = None

    # Set entities to return None (entity exists but value is None)
    dummy_items["select.charge_start_time"] = None
    dummy_items["select.charge_end_time"] = None
    dummy_items["switch.scheduled_charge_enable"] = "on"

    inv.update_status(my_predbat.minutes_now)

    # Should set safe defaults
    if inv.charge_enable_time != False:
        print(f"ERROR: {test_name} - charge_enable_time should be False, got {inv.charge_enable_time}")
        failed = True
    if inv.charge_start_time_minutes != my_predbat.forecast_minutes:
        print(f"ERROR: {test_name} - charge_start_time_minutes should be {my_predbat.forecast_minutes}, got {inv.charge_start_time_minutes}")
        failed = True
    if inv.charge_end_time_minutes != my_predbat.forecast_minutes:
        print(f"ERROR: {test_name} - charge_end_time_minutes should be {my_predbat.forecast_minutes}, got {inv.charge_end_time_minutes}")
        failed = True
    if inv.track_charge_start != "00:00:00":
        print(f"ERROR: {test_name} - track_charge_start should be '00:00:00', got {inv.track_charge_start}")
        failed = True
    if inv.track_charge_end != "00:00:00":
        print(f"ERROR: {test_name} - track_charge_end should be '00:00:00', got {inv.track_charge_end}")
        failed = True

    return failed


def test_discharge_window_none_illegal_time(test_name, my_predbat, dummy_items):
    """
    Test discharge window handling when time is illegal (e.g., 'unknown')
    This should result in None after time_string_to_stamp and trigger safe defaults
    """
    failed = False
    print(f"**** Running Test: {test_name} ****")

    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep
    inv.inv_has_discharge_enable_time = True
    inv.inv_has_ge_inverter_mode = False

    # Set illegal time value that will cause time_string_to_stamp to return None
    dummy_items["select.discharge_start_time"] = "unknown"
    dummy_items["select.discharge_end_time"] = "unknown"
    dummy_items["switch.scheduled_discharge_enable"] = "on"

    inv.update_status(my_predbat.minutes_now)

    # Should set safe defaults
    if inv.discharge_enable_time != False:
        print(f"ERROR: {test_name} - discharge_enable_time should be False, got {inv.discharge_enable_time}")
        failed = True
    if inv.discharge_start_time_minutes != 0:
        print(f"ERROR: {test_name} - discharge_start_time_minutes should be 0, got {inv.discharge_start_time_minutes}")
        failed = True
    if inv.discharge_end_time_minutes != 0:
        print(f"ERROR: {test_name} - discharge_end_time_minutes should be 0, got {inv.discharge_end_time_minutes}")
        failed = True
    if inv.track_discharge_start != "00:00:00":
        print(f"ERROR: {test_name} - track_discharge_start should be '00:00:00', got {inv.track_discharge_start}")
        failed = True
    if inv.track_discharge_end != "00:00:00":
        print(f"ERROR: {test_name} - track_discharge_end should be '00:00:00', got {inv.track_discharge_end}")
        failed = True

    return failed


def test_charge_window_invalid_format_time(test_name, my_predbat, dummy_items):
    """
    Test charge window handling when time has an invalid format (e.g., '14:70:00')
    This should result in None after time_string_to_stamp and trigger safe defaults, not a crash
    """
    failed = False
    print(f"**** Running Test: {test_name} ****")

    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep
    inv.inv_has_charge_enable_time = True

    # Set invalid time value that will cause time_string_to_stamp to return None (minutes out of range)
    dummy_items["select.charge_start_time"] = "14:70:00"
    dummy_items["select.charge_end_time"] = "14:70:00"
    dummy_items["switch.scheduled_charge_enable"] = "on"

    inv.update_status(my_predbat.minutes_now)

    # Should set safe defaults
    if inv.charge_enable_time != False:
        print(f"ERROR: {test_name} - charge_enable_time should be False, got {inv.charge_enable_time}")
        failed = True
    if inv.charge_start_time_minutes != my_predbat.forecast_minutes:
        print(f"ERROR: {test_name} - charge_start_time_minutes should be {my_predbat.forecast_minutes}, got {inv.charge_start_time_minutes}")
        failed = True
    if inv.charge_end_time_minutes != my_predbat.forecast_minutes:
        print(f"ERROR: {test_name} - charge_end_time_minutes should be {my_predbat.forecast_minutes}, got {inv.charge_end_time_minutes}")
        failed = True
    if inv.track_charge_start != "00:00:00":
        print(f"ERROR: {test_name} - track_charge_start should be '00:00:00', got {inv.track_charge_start}")
        failed = True
    if inv.track_charge_end != "00:00:00":
        print(f"ERROR: {test_name} - track_charge_end should be '00:00:00', got {inv.track_charge_end}")
        failed = True

    return failed


def test_discharge_window_invalid_format_time(test_name, my_predbat, dummy_items):
    """
    Test discharge window handling when time has an invalid format (e.g., '14:70:00')
    This should result in None after time_string_to_stamp and trigger safe defaults, not a crash
    """
    failed = False
    print(f"**** Running Test: {test_name} ****")

    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep
    inv.inv_has_discharge_enable_time = True
    inv.inv_has_ge_inverter_mode = False

    # Set invalid time value that will cause time_string_to_stamp to return None (minutes out of range)
    dummy_items["select.discharge_start_time"] = "14:70:00"
    dummy_items["select.discharge_end_time"] = "14:70:00"
    dummy_items["switch.scheduled_discharge_enable"] = "on"

    inv.update_status(my_predbat.minutes_now)

    # Should set safe defaults
    if inv.discharge_enable_time != False:
        print(f"ERROR: {test_name} - discharge_enable_time should be False, got {inv.discharge_enable_time}")
        failed = True
    if inv.discharge_start_time_minutes != 0:
        print(f"ERROR: {test_name} - discharge_start_time_minutes should be 0, got {inv.discharge_start_time_minutes}")
        failed = True
    if inv.discharge_end_time_minutes != 0:
        print(f"ERROR: {test_name} - discharge_end_time_minutes should be 0, got {inv.discharge_end_time_minutes}")
        failed = True
    if inv.track_discharge_start != "00:00:00":
        print(f"ERROR: {test_name} - track_discharge_start should be '00:00:00', got {inv.track_discharge_start}")
        failed = True
    if inv.track_discharge_end != "00:00:00":
        print(f"ERROR: {test_name} - track_discharge_end should be '00:00:00', got {inv.track_discharge_end}")
        failed = True

    return failed


def test_discharge_window_none_value(test_name, my_predbat, dummy_items):
    """
    Test discharge window handling when value is None from get_arg returning None
    This happens when the entity exists but returns None
    """
    failed = False
    print(f"**** Running Test: {test_name} ****")

    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep
    inv.inv_has_discharge_enable_time = True
    inv.inv_has_ge_inverter_mode = False
    inv.rest_api = None

    # Set entities to return None (entity exists but value is None)
    dummy_items["select.discharge_start_time"] = None
    dummy_items["select.discharge_end_time"] = None
    dummy_items["switch.scheduled_discharge_enable"] = "on"

    inv.update_status(my_predbat.minutes_now)

    # Should set safe defaults
    if inv.discharge_enable_time != False:
        print(f"ERROR: {test_name} - discharge_enable_time should be False, got {inv.discharge_enable_time}")
        failed = True
    if inv.discharge_start_time_minutes != 0:
        print(f"ERROR: {test_name} - discharge_start_time_minutes should be 0, got {inv.discharge_start_time_minutes}")
        failed = True
    if inv.discharge_end_time_minutes != 0:
        print(f"ERROR: {test_name} - discharge_end_time_minutes should be 0, got {inv.discharge_end_time_minutes}")
        failed = True
    if inv.track_discharge_start != "00:00:00":
        print(f"ERROR: {test_name} - track_discharge_start should be '00:00:00', got {inv.track_discharge_start}")
        failed = True
    if inv.track_discharge_end != "00:00:00":
        print(f"ERROR: {test_name} - track_discharge_end should be '00:00:00', got {inv.track_discharge_end}")
        failed = True

    return failed


def test_force_export_unchanged_times_HM_format(test_name, ha, inv):
    """
    Regression test for GS_fb00 (Solis) 'count register writes 0' bug.

    When force_export=True, the H M format time entities and the scheduled_discharge_enable
    switch should be written on every PredBat cycle — even when the times are unchanged and
    HA already shows the switch as 'on'.  Before the fix, adjust_force_export skipped all
    writes when times were identical, resulting in 0 register writes and the inverter never
    actually exporting.

    The button (schedule_write_button) must also be pressed so the Solis hardware applies
    the time-slot schedule.
    """
    failed = False
    print("Test: {}".format(test_name))

    # Simulate GS_fb00 / H M format configuration
    inv.rest_data = None
    inv.inv_charge_time_format = "H M"
    inv.inv_time_button_press = True

    export_time = "07:58:00"
    export_end = "09:02:00"

    # Pre-set all discharge entities to the *same* time that will be written,
    # and the switch to "on" — this is the "stable state" where the bug triggered.
    ha.dummy_items["select.discharge_start_time"] = export_time
    ha.dummy_items["select.discharge_end_time"] = export_end
    ha.dummy_items["time.discharge_start_hour"] = export_time
    ha.dummy_items["time.discharge_end_hour"] = export_end
    ha.dummy_items["switch.scheduled_discharge_enable"] = "on"
    ha.dummy_items["number.discharge_target_soc"] = inv.reserve_percent
    ha.dummy_items["select.inverter_mode"] = "Timed Export"
    ha.dummy_items["switch.inverter_button"] = "off"

    inv.base.args["discharge_start_time"] = "select.discharge_start_time"
    inv.base.args["discharge_end_time"] = "select.discharge_end_time"
    inv.base.args["discharge_start_hour"] = "time.discharge_start_hour"
    inv.base.args["discharge_end_hour"] = "time.discharge_end_hour"
    inv.base.args["scheduled_discharge_enable"] = "switch.scheduled_discharge_enable"

    before_writes = inv.count_register_writes

    ts = datetime.strptime(export_time, "%H:%M:%S")
    te = datetime.strptime(export_end, "%H:%M:%S")
    inv.adjust_force_export(True, ts, te)

    # Time entities must be written even though times are unchanged (H M format always writes)
    if ha.dummy_items.get("time.discharge_start_hour") != export_time:
        print(f"ERROR: {test_name}: discharge_start_hour should still be {export_time} got {ha.dummy_items.get('time.discharge_start_hour')}")
        failed = True
    if ha.dummy_items.get("time.discharge_end_hour") != export_end:
        print(f"ERROR: {test_name}: discharge_end_hour should still be {export_end} got {ha.dummy_items.get('time.discharge_end_hour')}")
        failed = True

    # Register write count must have increased by at least 2 (discharge_start_hour + discharge_end_hour)
    if inv.count_register_writes < before_writes + 2:
        print(f"ERROR: {test_name}: count_register_writes should have increased by at least 2, was {before_writes} now {inv.count_register_writes}")
        failed = True

    # The schedule_write_button must be pressed so the Solis hardware applies the schedule
    if ha.dummy_items.get("switch.inverter_button") != "on":
        print(f"ERROR: {test_name}: inverter_button (schedule_write_button) should be 'on' (pressed), got {ha.dummy_items.get('switch.inverter_button')}")
        failed = True

    return failed


def test_time_entity_hour_write(test_name, ha, inv, dummy_rest, direction, new_start, new_end):
    """
    Test that when *_start_hour / *_end_hour args resolve to time.* entities the full
    time string is written (not just the integer hour component) and that no TypeError
    is raised.

    direction: "discharge" or "charge"
    """
    failed = False
    print("Test: {} direction={}".format(test_name, direction))

    inv.rest_data = None
    inv.inv_charge_time_format = "H M"

    new_start_ts = datetime.strptime(new_start, "%H:%M:%S")
    new_end_ts = datetime.strptime(new_end, "%H:%M:%S")

    if direction == "discharge":
        # Wire up time entities for discharge slot
        ha.dummy_items["select.discharge_start_time"] = "00:00:00"
        ha.dummy_items["select.discharge_end_time"] = "00:00:00"
        ha.dummy_items["time.discharge_start_hour"] = "00:00:00"
        ha.dummy_items["time.discharge_end_hour"] = "00:00:00"
        ha.dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"] = "off"
        ha.dummy_items["number.discharge_target_soc"] = inv.reserve_percent
        ha.dummy_items["select.inverter_mode"] = "Eco"
        ha.dummy_items["switch.inverter_button"] = "off"

        inv.base.args["discharge_start_time"] = "select.discharge_start_time"
        inv.base.args["discharge_end_time"] = "select.discharge_end_time"
        inv.base.args["discharge_start_hour"] = "time.discharge_start_hour"
        inv.base.args["discharge_end_hour"] = "time.discharge_end_hour"
        # No discharge_start_minute / discharge_end_minute – those args are absent

        try:
            inv.adjust_force_export(True, new_start_ts, new_end_ts)
        except TypeError as e:
            print("ERROR: TypeError raised: {}".format(e))
            return True

        if ha.dummy_items.get("time.discharge_start_hour") != new_start:
            print("ERROR: discharge_start_hour time entity should be {} got {}".format(new_start, ha.dummy_items.get("time.discharge_start_hour")))
            failed = True
        if ha.dummy_items.get("time.discharge_end_hour") != new_end:
            print("ERROR: discharge_end_hour time entity should be {} got {}".format(new_end, ha.dummy_items.get("time.discharge_end_hour")))
            failed = True
        if ha.dummy_items.get("select.discharge_start_time") != new_start:
            print("ERROR: discharge_start_time select entity should be {} got {}".format(new_start, ha.dummy_items.get("select.discharge_start_time")))
            failed = True
        if ha.dummy_items.get("select.discharge_end_time") != new_end:
            print("ERROR: discharge_end_time select entity should be {} got {}".format(new_end, ha.dummy_items.get("select.discharge_end_time")))
            failed = True

    else:  # charge
        ha.dummy_items["select.charge_start_time"] = "00:00:00"
        ha.dummy_items["select.charge_end_time"] = "00:00:00"
        ha.dummy_items["time.charge_start_hour"] = "00:00:00"
        ha.dummy_items["time.charge_end_hour"] = "00:00:00"
        ha.dummy_items["switch.scheduled_charge_enable"] = "off"
        ha.dummy_items["switch.inverter_button"] = "off"

        inv.base.args["charge_start_time"] = "select.charge_start_time"
        inv.base.args["charge_end_time"] = "select.charge_end_time"
        inv.base.args["charge_start_hour"] = "time.charge_start_hour"
        inv.base.args["charge_end_hour"] = "time.charge_end_hour"

        try:
            inv.adjust_charge_window(new_start_ts, new_end_ts, inv.base.minutes_now)
        except TypeError as e:
            print("ERROR: TypeError raised: {}".format(e))
            return True

        if ha.dummy_items.get("time.charge_start_hour") != new_start:
            print("ERROR: charge_start_hour time entity should be {} got {}".format(new_start, ha.dummy_items.get("time.charge_start_hour")))
            failed = True
        if ha.dummy_items.get("time.charge_end_hour") != new_end:
            print("ERROR: charge_end_hour time entity should be {} got {}".format(new_end, ha.dummy_items.get("time.charge_end_hour")))
            failed = True
        if ha.dummy_items.get("select.charge_start_time") != new_start:
            print("ERROR: charge_start_time select entity should be {} got {}".format(new_start, ha.dummy_items.get("select.charge_start_time")))
            failed = True
        if ha.dummy_items.get("select.charge_end_time") != new_end:
            print("ERROR: charge_end_time select entity should be {} got {}".format(new_end, ha.dummy_items.get("select.charge_end_time")))
            failed = True

    return failed


def test_input_datetime_charge_window(test_name, ha, inv, dummy_rest, direction, new_start, new_end):
    """
    Regression test for issue #4048: when charge_start_time / discharge_start_time are
    input_datetime entities, write_and_poll_option must call input_datetime/set_datetime
    (not the non-existent input_datetime/set_value service).
    """
    failed = False
    print("Test: {} direction={}".format(test_name, direction))

    inv.rest_data = None
    inv.inv_charge_time_format = "H M"

    new_start_ts = datetime.strptime(new_start, "%H:%M:%S")
    new_end_ts = datetime.strptime(new_end, "%H:%M:%S")

    if direction == "charge":
        ha.dummy_items["input_datetime.charge_start_time"] = "00:00:00"
        ha.dummy_items["input_datetime.charge_end_time"] = "00:00:00"
        ha.dummy_items["switch.scheduled_charge_enable"] = "off"
        ha.dummy_items["switch.inverter_button"] = "off"

        inv.base.args["charge_start_time"] = "input_datetime.charge_start_time"
        inv.base.args["charge_end_time"] = "input_datetime.charge_end_time"
        inv.base.args.pop("charge_start_hour", None)
        inv.base.args.pop("charge_end_hour", None)
        inv.base.args.pop("charge_start_minute", None)
        inv.base.args.pop("charge_end_minute", None)

        try:
            inv.adjust_charge_window(new_start_ts, new_end_ts, inv.base.minutes_now)
        except TypeError as e:
            print("ERROR: TypeError raised: {}".format(e))
            return True

        if ha.dummy_items.get("input_datetime.charge_start_time") != new_start:
            print("ERROR: charge_start_time should be {} got {}".format(new_start, ha.dummy_items.get("input_datetime.charge_start_time")))
            failed = True
        if ha.dummy_items.get("input_datetime.charge_end_time") != new_end:
            print("ERROR: charge_end_time should be {} got {}".format(new_end, ha.dummy_items.get("input_datetime.charge_end_time")))
            failed = True
    else:
        ha.dummy_items["input_datetime.discharge_start_time"] = "00:00:00"
        ha.dummy_items["input_datetime.discharge_end_time"] = "00:00:00"
        ha.dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"] = "off"
        ha.dummy_items["number.discharge_target_soc"] = inv.reserve_percent
        ha.dummy_items["select.inverter_mode"] = "Eco"
        ha.dummy_items["switch.inverter_button"] = "off"

        inv.base.args["discharge_start_time"] = "input_datetime.discharge_start_time"
        inv.base.args["discharge_end_time"] = "input_datetime.discharge_end_time"
        inv.base.args.pop("discharge_start_hour", None)
        inv.base.args.pop("discharge_end_hour", None)
        inv.base.args.pop("discharge_start_minute", None)
        inv.base.args.pop("discharge_end_minute", None)

        try:
            inv.adjust_force_export(True, new_start_ts, new_end_ts)
        except TypeError as e:
            print("ERROR: TypeError raised: {}".format(e))
            return True

        if ha.dummy_items.get("input_datetime.discharge_start_time") != new_start:
            print("ERROR: discharge_start_time should be {} got {}".format(new_start, ha.dummy_items.get("input_datetime.discharge_start_time")))
            failed = True
        if ha.dummy_items.get("input_datetime.discharge_end_time") != new_end:
            print("ERROR: discharge_end_time should be {} got {}".format(new_end, ha.dummy_items.get("input_datetime.discharge_end_time")))
            failed = True

    return failed


def test_rest_battery_capacity_fallback(test_name, my_predbat):
    """
    Verify that when V3 REST data omits Battery_Capacity_kWh and battery_nominal_capacity,
    nominal_capacity and soc_max fall back to the soc_max value configured in apps.yaml.
    """
    failed = False
    print("**** Running Test: {} ****".format(test_name))
    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    if "inverter_limit" in my_predbat.args:
        del my_predbat.args["inverter_limit"]
    if "export_limit" in my_predbat.args:
        del my_predbat.args["export_limit"]

    # Load the real V3 fixture and strip battery capacity fields to simulate an inverter
    # that doesn't report Battery_Capacity_kWh or battery_nominal_capacity via REST.
    with open("cases/rest_v3.json", "r") as f:
        rest_v3_data = json.load(f)

    orig_serial = "EA2303G082"
    new_serial = "GW2347G084"
    rest_v3_data[new_serial] = rest_v3_data.pop(orig_serial)
    rest_v3_data["raw"]["invertor"]["serial_number"] = new_serial
    rest_v3_data["Stats"]["GivTCP_Version"] = "3.1.6"

    del rest_v3_data[new_serial]["Battery_Capacity_kWh"]
    del rest_v3_data["raw"]["invertor"]["battery_nominal_capacity"]

    dummy_rest.rest_data = rest_v3_data

    my_predbat.restart_active = True
    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData, quiet=False)
    inv.sleep = dummy_sleep
    inv.update_status(my_predbat.minutes_now)
    my_predbat.restart_active = False

    # sensor.soc_max = 10.0 in dummy_items, so the fallback should produce nominal_capacity 10.0
    expected_nominal = 10.0
    if inv.nominal_capacity != expected_nominal:
        print("ERROR: nominal_capacity should be {} (apps.yaml fallback) got {}".format(expected_nominal, inv.nominal_capacity))
        failed = True
    if inv.soc_max != expected_nominal:
        print("ERROR: soc_max should be {} (apps.yaml fallback) got {}".format(expected_nominal, inv.soc_max))
        failed = True

    return failed


def test_inverter_time_handling(my_predbat, dummy_items):
    """Verify inverter clock-skew detection handles a stale/unavailable cloud time source correctly.

    When the cloud time source is unavailable (e.g. GivEnergy API access denied because a
    GivEnergy Premium subscription is now required), PredBat must treat it as "no reading" and
    skip skew detection rather than misreporting it as inverter clock skew and triggering an
    auto-restart loop. A genuinely skewed inverter clock must still be detected.
    """
    failed = False
    print("**** Running Test: inverter_time_handling ****")

    def _no_sleep(self, seconds):
        """No-op sleep so construction never really blocks, even if an auto-restart path is hit."""
        return None

    saved_time = dummy_items.get("sensor.inverter_time")
    saved_sleep = Inverter.sleep
    Inverter.sleep = _no_sleep
    try:
        # Case 1: cloud time source unavailable -> skip skew detection, no auto-restart.
        dummy_items["sensor.inverter_time"] = "unavailable"
        my_predbat.ha_interface.dummy_items = dummy_items
        my_predbat.current_status = ""
        my_predbat.restart_active = False
        inv = Inverter(my_predbat, 0)
        if inv.inverter_time is not None:
            print("ERROR: unavailable inverter time should resolve to None, got {}".format(inv.inverter_time))
            failed = True
        if my_predbat.restart_active:
            print("ERROR: unavailable inverter time must not trigger an auto-restart")
            failed = True
        if "skew" in (my_predbat.current_status or "").lower():
            print("ERROR: unavailable inverter time must not be reported as clock skew, status={}".format(my_predbat.current_status))
            failed = True

        # Case 2: a genuinely skewed clock (2 hours ahead) must still be detected.
        skew_time = (my_predbat.now_utc + timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%S%z")
        dummy_items["sensor.inverter_time"] = skew_time
        my_predbat.ha_interface.dummy_items = dummy_items
        my_predbat.current_status = ""
        my_predbat.restart_active = True  # suppress real restart side-effects; record_status still fires
        Inverter(my_predbat, 0)
        if "skew" not in (my_predbat.current_status or "").lower():
            print("ERROR: a 2-hour clock skew should still be detected, status={}".format(my_predbat.current_status))
            failed = True
    finally:
        Inverter.sleep = saved_sleep
        dummy_items["sensor.inverter_time"] = saved_time
        my_predbat.ha_interface.dummy_items = dummy_items
        my_predbat.current_status = ""
        my_predbat.restart_active = False

    if not failed:
        print("**** Test inverter_time_handling PASSED ****")
    return failed


def run_inverter_tests(my_predbat_dummy):
    """
    Test the inverter functions
    """
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = False
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.minutes_now = 12 * 60  # Pin to noon so time-dependent tests are deterministic
    my_predbat.ha_interface.history_enable = True

    failed = False
    print("**** Running Inverter tests ****")
    ha = my_predbat.ha_interface

    time_now = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    dummy_items = {
        "number.charge_rate": 1100,
        "number.discharge_rate": 1500,
        "number.charge_rate_percent": 100,
        "number.discharge_rate_percent": 100,
        "number.charge_limit": 100,
        "select.pause_mode": "Disabled",
        "sensor.battery_capacity": 10.0,
        "sensor.battery_soc": 0.0,
        "sensor.soc_max": 10.0,
        "sensor.soc_kw": 1.0,
        "select.inverter_mode": "Eco",
        "sensor.inverter_time": time_now,
        "switch.restart": False,
        "select.idle_start_time": "00:00",
        "select.idle_end_time": "00:00",
        "sensor.battery_power": 5.0,
        "sensor.pv_power": 1.0,
        "sensor.load_power": 2.0,
        "number.reserve": 4.0,
        "switch.scheduled_charge_enable": "off",
        "switch.scheduled_discharge_enable": "off",
        "select.charge_start_time": "01:11:00",
        "select.charge_end_time": "02:22:00",
        "select.discharge_start_time": "03:33:00",
        "select.discharge_end_time": "04:44:00",
        "sensor.predbat_GE_0_scheduled_discharge_enable": "off",
        "number.discharge_target_soc": 4,
        "switch.inverter_button": False,
    }
    my_predbat.ha_interface.dummy_items = dummy_items
    my_predbat.args["auto_restart"] = [{"service": "switch/turn_on", "entity_id": "switch.restart"}]
    my_predbat.args["givtcp_rest"] = None
    my_predbat.args["inverter_type"] = ["GE"]
    my_predbat.args["schedule_write_button"] = "switch.inverter_button"
    for entity_id in dummy_items.keys():
        arg_name = entity_id.split(".")[1]
        my_predbat.args[arg_name] = entity_id

    failed |= test_inverter_time_handling(my_predbat, dummy_items)

    failed |= test_inverter_update(
        "update1",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="02:22:00",
        expect_charge_enable=False,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=5.0,
        expect_pv_power=1.0,
        expect_load_power=2.0,
        expect_soc_kwh=6.0,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update1b",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="02:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=5.0,
        expect_pv_power=1.0,
        expect_load_power=2.0,
        expect_soc_kwh=6.0,
        has_charge_enable_time=False,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update1c",
        my_predbat,
        dummy_items,
        expect_charge_start_time="02:11:00",
        expect_charge_end_time="01:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="04:33:00",
        expect_discharge_end_time="03:44:00",
        expect_discharge_enable=True,
        expect_battery_power=5.0,
        expect_pv_power=1.0,
        expect_load_power=2.0,
        expect_soc_kwh=6.0,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update2",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="23:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        soc_percent=True,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update3",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="23:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update4a",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="01:11:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="03:33:00",
        expect_discharge_enable=False,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update4b",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="01:11:00",
        expect_charge_enable=False,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="03:33:00",
        expect_discharge_enable=False,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        has_charge_enable_time=False,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_rest_template(
        "rest1",
        my_predbat,
        filename="cases/rest_v2.json",
        assert_soc_max=9.523,
        assert_soc=3.333,
        assert_pv_power=10,
        assert_load_power=624,
        assert_charge_start_time_minutes=1410,
        assert_charge_end_time_minutes=1770,
        assert_discharge_start_time_minutes=1380,
        assert_discharge_end_time_minutes=1441,
        assert_discharge_enable=False,
        assert_charge_enable=True,
        assert_nominal_capacity=9.5232,
        assert_battery_temperature=15.3,
    )
    if failed:
        return failed
    failed |= test_inverter_rest_template(
        "rest2",
        my_predbat,
        filename="cases/rest_v3.json",
        assert_voltage=53.65,
        assert_battery_rate_max=3600,
        assert_serial_number="EA2303G082",
        assert_soc=7.62,
        assert_pv_power=247.0,
        assert_load_power=197.0,
        assert_charge_start_time_minutes=1440,
        assert_charge_end_time_minutes=1440,
        assert_discharge_start_time_minutes=1445,
        assert_discharge_end_time_minutes=1531,
        assert_discharge_enable=True,
        assert_nominal_capacity=9.52,
        assert_battery_temperature=25.0,
    )
    if failed:
        return failed

    failed |= test_rest_battery_capacity_fallback("rest_capacity_fallback", my_predbat)
    if failed:
        return failed

    my_predbat.args["givtcp_rest"] = None
    dummy_rest = DummyRestAPI()
    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData)
    inv.sleep = dummy_sleep
    inv.update_status(my_predbat.minutes_now)
    my_predbat.inv = inv

    failed |= test_adjust_force_export("adjust_force_export1", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, 4, "11:00:00", "11:30:00", False, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export2", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, 4, "11:00:00", "11:30:00", True, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export3", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, 10, "11:00:00", "11:30:00", True, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export4", ha, inv, dummy_rest, "00:11:00", "01:12:12", True, 10, "11:00:00", "11:30:00", True, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export5", ha, inv, dummy_rest, "00:11:00", "01:12:12", True, 4, "11:00:00", "11:30:00", False, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export6", ha, inv, dummy_rest, "11:00:00", "11:30:00", True, 4, "11:00:00", "11:30:00", True, has_inv_time_button_press=True, expect_inv_time_button_press=False)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export7", ha, inv, dummy_rest, "11:00:00", "11:30:00", True, 4, "11:00:00", "11:30:00", False, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export8", ha, inv, dummy_rest, "11:00:00", "11:30:00", False, 4, "11:00:00", "11:30:00", True, has_inv_time_button_press=True, expect_inv_time_button_press=True)
    if failed:
        return failed

    failed |= test_adjust_battery_target("adjust_target50", ha, inv, dummy_rest, 0, 50, True, False, 50, has_inv_time_button_press=True, expect_button_press=True)
    failed |= test_adjust_battery_target("adjust_target0", ha, inv, dummy_rest, 10, 0, True, False, 4, has_inv_time_button_press=True, expect_button_press=True)
    failed |= test_adjust_battery_target("adjust_target100", ha, inv, dummy_rest, 99, 100, True, False, 100, has_inv_time_button_press=True, expect_button_press=True)
    failed |= test_adjust_battery_target("adjust_target100r", ha, inv, dummy_rest, 100, 100, True, False, 100, has_inv_time_button_press=True, expect_button_press=False)  # No change, no button press
    failed |= test_adjust_battery_target("adjust_target0x", ha, inv, dummy_rest, 50, 0, False, True, 50, has_inv_time_button_press=False, expect_button_press=False)  # No button press feature
    if failed:
        return failed

    # Test rest_enableChargeTarget directly: enable=True passes first try
    failed |= test_rest_enable_charge_target(
        "rest_enable_charge_target_enable",
        ha,
        inv,
        dummy_rest,
        enable=True,
        expect_commands=[["dummy/enableChargeTarget", {"state": "enable"}]],
    )
    # enable=False passes first try (default Enable_Charge_Target is "disable")
    failed |= test_rest_enable_charge_target(
        "rest_enable_charge_target_disable",
        ha,
        inv,
        dummy_rest,
        enable=False,
        expect_commands=[["dummy/enableChargeTarget", {"state": "disable"}]],
    )
    # Retry logic: first response is wrong, second is correct — expect 2 commands
    failed |= test_rest_enable_charge_target(
        "rest_enable_charge_target_retry",
        ha,
        inv,
        dummy_rest,
        enable=True,
        expect_commands=[
            ["dummy/enableChargeTarget", {"state": "enable"}],
            ["dummy/enableChargeTarget", {"state": "enable"}],
        ],
        queued_rest_states=["disable", "enable"],
    )
    # Guard no-op: already in the target state, no command sent
    failed |= test_rest_enable_charge_target(
        "rest_enable_charge_target_noop",
        ha,
        inv,
        dummy_rest,
        enable=True,
        expect_commands=[],
        initial_state="enable",
    )
    if failed:
        return failed

    # Test charge_limit_enable switch is written when charge limit changes
    failed |= test_adjust_battery_target_charge_limit_enable("charge_limit_enable_written", my_predbat, ha, inv, 50, 80, 80, True)
    if failed:
        return failed

    # Test charge_limit_enable switch is NOT written when charge limit is unchanged
    failed |= test_adjust_battery_target_charge_limit_enable("charge_limit_enable_no_write", my_predbat, ha, inv, 80, 80, 80, False)
    if failed:
        return failed

    failed |= test_adjust_inverter_mode("adjust_mode_eco2", ha, inv, dummy_rest, "Eco", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco3", ha, inv, dummy_rest, "Eco (Paused)", "Eco", "Eco (Paused)")
    failed |= test_adjust_inverter_mode("adjust_mode_export1", ha, inv, dummy_rest, "Eco (Paused)", "Timed Export", "Timed Export")
    failed |= test_adjust_inverter_mode("adjust_mode_export2", ha, inv, dummy_rest, "Timed Export", "Timed Export", "Timed Export")
    if failed:
        return failed

    # Test GE eco toggle mode (inv_has_ge_eco_toggle=True, used by GEC inverter type)
    # force_export=False -> eco switch "on"; force_export=True -> eco switch "off"
    failed |= test_adjust_ge_eco_toggle("eco_toggle_enable_eco", ha, inv, "off", False, "on")
    failed |= test_adjust_ge_eco_toggle("eco_toggle_force_export", ha, inv, "on", True, "off")
    failed |= test_adjust_ge_eco_toggle("eco_toggle_no_change_eco", ha, inv, "on", False, "on")
    failed |= test_adjust_ge_eco_toggle("eco_toggle_no_change_export", ha, inv, "off", True, "off")
    failed |= test_adjust_ge_eco_toggle_missing_entity("eco_toggle_missing_entity_unset_enable", inv, False, "unset", True)
    failed |= test_adjust_ge_eco_toggle_missing_entity("eco_toggle_missing_entity_unset_export", inv, True, "unset", True)
    failed |= test_adjust_ge_eco_toggle_missing_entity("eco_toggle_missing_entity_none_enable", inv, False, None, False)
    failed |= test_adjust_ge_eco_toggle_missing_entity("eco_toggle_missing_entity_none_export", inv, True, None, False)
    if failed:
        return failed

    failed |= test_adjust_charge_rate("adjust_charge_rate1", ha, inv, dummy_rest, 0, 200.1, 200)
    if failed:
        return failed
    failed |= test_adjust_charge_rate("adjust_charge_rate2", ha, inv, dummy_rest, 0, 100, 0)
    if failed:
        return failed
    failed |= test_adjust_charge_rate("adjust_charge_rate3", ha, inv, dummy_rest, 200, 0, 0)
    failed |= test_adjust_charge_rate("adjust_charge_rate4", ha, inv, dummy_rest, 100, 0, 100)
    failed |= test_adjust_charge_rate("adjust_charge_rate5", ha, inv, dummy_rest, 200, 210, 200)
    if failed:
        return failed

    failed |= test_adjust_charge_rate("adjust_discharge_rate1", ha, inv, dummy_rest, 0, 250.1, 250, discharge=True)
    failed |= test_adjust_charge_rate("adjust_discharge_rate2", ha, inv, dummy_rest, 250, 0, 0, discharge=True)
    failed |= test_adjust_charge_rate("adjust_discharge_rate3", ha, inv, dummy_rest, 200, 210, 200, discharge=True)
    if failed:
        return failed

    failed |= test_adjust_reserve("adjust_reserve1", ha, inv, dummy_rest, 4, 50, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve2", ha, inv, dummy_rest, 50, 0, 4, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve3", ha, inv, dummy_rest, 20, 100, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve4", ha, inv, dummy_rest, 20, 100, 98, reserve_min=4, reserve_max=98)
    failed |= test_adjust_reserve("adjust_reserve5", ha, inv, dummy_rest, 50, 0, 0, reserve_min=0, reserve_max=100)
    if failed:
        return failed

    failed |= test_adjust_charge_window("adjust_charge_window1", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "00:00:00", "00:00:00", my_predbat.minutes_now, has_inverter_time_button_press=True, expect_inverter_time_button_press=False)
    failed |= test_adjust_charge_window("adjust_charge_window2", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "00:00:00", "23:00:00", my_predbat.minutes_now, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    failed |= test_adjust_charge_window("adjust_charge_window3", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "00:00:00", "23:00:00", my_predbat.minutes_now, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    failed |= test_adjust_charge_window("adjust_charge_window4", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "01:12:00", "23:12:00", my_predbat.minutes_now, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    failed |= test_adjust_charge_window("adjust_charge_window5", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "01:12:00", "23:12:00", my_predbat.minutes_now, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    failed |= test_adjust_charge_window("adjust_charge_window6", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "01:12:00", "23:12:00", my_predbat.minutes_now, short=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    failed |= test_adjust_charge_window("adjust_charge_window7", ha, inv, dummy_rest, "00:11:00", "00:12:00", True, "00:11:00", "00:12:00", my_predbat.minutes_now, short=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=False)
    failed |= test_adjust_charge_window("adjust_charge_window7", ha, inv, dummy_rest, "00:11:00", "00:12:00", False, "00:11:00", "00:12:00", my_predbat.minutes_now, short=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)

    # Test midnight-spanning windows - verify charge_start_time_minutes and charge_end_time_minutes are calculated correctly
    # Normal window (end > start): no adjustment, minutes_now=600 (10:00), start=11:00 (660), end=14:00 (840)
    failed |= test_adjust_charge_window(
        "adjust_charge_window_normal",
        ha,
        inv,
        dummy_rest,
        "00:00:00",
        "00:00:00",
        True,
        "11:00:00",
        "14:00:00",
        600,
        has_inverter_time_button_press=True,
        expect_inverter_time_button_press=True,
        expect_charge_start_time_minutes=660,
        expect_charge_end_time_minutes=840,
    )

    # Midnight span - past midnight but before end: start=23:00 (1380), end=02:00 (120), minutes_now=60 (01:00)
    # Since end (120) > minutes_now (60), start should be moved back: start=-60 (23:00 - 24*60), end=120
    failed |= test_adjust_charge_window(
        "adjust_charge_window_midnight_before_end",
        ha,
        inv,
        dummy_rest,
        "00:00:00",
        "00:00:00",
        True,
        "23:00:00",
        "02:00:00",
        60,
        has_inverter_time_button_press=True,
        expect_inverter_time_button_press=True,
        expect_charge_start_time_minutes=-60,
        expect_charge_end_time_minutes=120,
    )

    # Midnight span - end has passed: start=23:00 (1380), end=02:00 (120), minutes_now=600 (10:00)
    # Since end (120) < minutes_now (600), end should be moved forward: start=1380, end=120+1440=1560
    failed |= test_adjust_charge_window(
        "adjust_charge_window_midnight_end_passed",
        ha,
        inv,
        dummy_rest,
        "00:00:00",
        "00:00:00",
        True,
        "23:00:00",
        "02:00:00",
        600,
        has_inverter_time_button_press=True,
        expect_inverter_time_button_press=True,
        expect_charge_start_time_minutes=1380,
        expect_charge_end_time_minutes=1560,
    )

    # Window entirely in the past: start=01:00 (60), end=02:00 (120), minutes_now=600 (10:00)
    # Since end (120) < minutes_now (600), both should be moved forward: start=60+1440=1500, end=120+1440=1560
    failed |= test_adjust_charge_window(
        "adjust_charge_window_past_window",
        ha,
        inv,
        dummy_rest,
        "00:00:00",
        "00:00:00",
        True,
        "01:00:00",
        "02:00:00",
        600,
        has_inverter_time_button_press=True,
        expect_inverter_time_button_press=True,
        expect_charge_start_time_minutes=1500,
        expect_charge_end_time_minutes=1560,
    )

    if failed:
        return failed

    failed |= test_disable_charge_window("disable_charge_window1", ha, inv, dummy_rest, "01:12:00", "23:12:00", True, my_predbat.minutes_now, has_charge_enable_time=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    if failed:
        return failed
    failed |= test_disable_charge_window("disable_charge_window2", ha, inv, dummy_rest, "01:12:00", "23:12:00", False, my_predbat.minutes_now, has_charge_enable_time=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=False)
    if failed:
        return failed
    failed |= test_disable_charge_window("disable_charge_window3", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, my_predbat.minutes_now, has_charge_enable_time=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=True)
    if failed:
        return failed
    failed |= test_disable_charge_window("disable_charge_window4", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, my_predbat.minutes_now, has_charge_enable_time=True, has_inverter_time_button_press=True, expect_inverter_time_button_press=False)
    if failed:
        return failed

    failed |= test_call_service_template("test_service_simple1", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"})
    failed |= test_call_service_template("test_service_simple2", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False, repeat=True)
    failed |= test_call_service_template("test_service_simple3", my_predbat, inv, service_name="test_service", domain="discharge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False)
    failed |= test_call_service_template("test_service_simple4", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False, repeat=True)
    failed |= test_call_service_template("test_service_simple5", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data2"}, clear=False, repeat=False)
    failed |= test_call_service_template("test_service_simple6", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra_dummy": "data2"}, clear=False, repeat=False)
    if failed:
        return failed

    failed |= test_call_service_template(
        "test_service_complex1",
        my_predbat,
        inv,
        service_name="complex_service",
        domain="charge",
        data={"test": "data"},
        extra_data={"extra": "extra_data"},
        service_template={"service": "funny", "dummy": "22", "extra": "{extra}"},
        expected_result=[["funny", {"dummy": "22", "extra": "extra_data"}]],
        clear=False,
    )
    failed |= test_call_service_template(
        "test_service_complex2", my_predbat, inv, service_name="complex_service", domain="charge", data={"test": "data"}, extra_data={"extra": "extra_data"}, service_template={"service": "funny", "dummy": "22", "extra": "{extra}"}, clear=False, repeat=True
    )
    failed |= test_call_service_template(
        "test_service_complex3",
        my_predbat,
        inv,
        service_name="complex_service",
        domain="charge",
        data={"test": "data"},
        extra_data={"extra": "extra_data"},
        service_template={"service": "funny", "dummy": "22", "extra": "{extra}", "always": True},
        expected_result=[["funny", {"dummy": "22", "extra": "extra_data"}]],
        clear=False,
        repeat=False,
        twice=False,
    )

    service_template = """
charge_start_service:
  - service: select.select_option
    entity_id: select.inverter1_work_mode
    option: "Force Charge"
  - service: select.select_option
    entity_id: select.inverter2_work_mode
    option: "Force Charge"
"""
    decoded_template = yaml.safe_load(service_template)
    print("Decoded template: {}".format(decoded_template))
    failed |= test_call_service_template(
        "test_service_complex4",
        my_predbat,
        inv,
        service_name="charge_start_service",
        domain="charge",
        service_template=decoded_template.get("charge_start_service"),
        expected_result=[["select/select_option", {"entity_id": "select.inverter1_work_mode", "option": "Force Charge"}], ["select/select_option", {"entity_id": "select.inverter2_work_mode", "option": "Force Charge"}]],
        clear=False,
        repeat=False,
        twice=False,
    )

    if failed:
        return failed

    my_predbat.args["extra"] = "42"
    failed |= test_call_service_template(
        "test_service_complex4",
        my_predbat,
        inv,
        service_name="complex_service",
        domain="charge",
        data={"test": "data"},
        extra_data={"extra": "extra_data"},
        service_template={"service": "funny", "dummy": "22", "extra": "{extra}"},
        expected_result=[["funny", {"dummy": "22", "extra": "extra_data"}]],
        clear=True,
    )

    dummy_yaml = """
    service: select.select_option
    entity_id: "select.solaredge_i1_storage_command_mode"
    option: "Charge from Solar Power and Grid"
    always: true
    """
    decoded_yaml = yaml.safe_load(dummy_yaml)

    for repeat in range(2):
        failed |= test_call_service_template(
            "test_service_complex5",
            my_predbat,
            inv,
            service_name="charge_start_service",
            domain="charge",
            data={"test": "data"},
            extra_data={"extra": "extra_data"},
            service_template=decoded_yaml,
            expected_result=[["select/select_option", {"entity_id": "select.solaredge_i1_storage_command_mode", "option": "Charge from Solar Power and Grid"}]],
            clear=False,
            repeat=False,
            twice=False,
        )

    inv.soc_percent = 49

    if failed:
        return failed

    inv.has_target_soc = True

    failed |= test_call_adjust_charge_immediate("charge_immediate1", my_predbat, ha, inv, dummy_items, 100, clear=True, stop_discharge=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate2", my_predbat, ha, inv, dummy_items, 0)
    failed |= test_call_adjust_charge_immediate("charge_immediate3", my_predbat, ha, inv, dummy_items, 0, repeat=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate4", my_predbat, ha, inv, dummy_items, 50)
    failed |= test_call_adjust_charge_immediate("charge_immediate5", my_predbat, ha, inv, dummy_items, 49)
    failed |= test_call_adjust_charge_immediate("charge_immediate6", my_predbat, ha, inv, dummy_items, 49, repeat=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate6", my_predbat, ha, inv, dummy_items, 49, charge_start_time="00:00:00", charge_end_time="11:00:00")
    failed |= test_call_adjust_charge_immediate("charge_immediate7", my_predbat, ha, inv, dummy_items, 50, freeze=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate8", my_predbat, ha, inv, dummy_items, 50, freeze=False, no_freeze=True)
    if failed:
        return failed

    failed |= test_call_adjust_export_immediate("export_immediate1", my_predbat, ha, inv, dummy_items, 100, repeat=True)
    failed |= test_call_adjust_export_immediate("export_immediate3", my_predbat, ha, inv, dummy_items, 0, repeat=False, charge_stop=True)
    failed |= test_call_adjust_export_immediate("export_immediate4", my_predbat, ha, inv, dummy_items, 50)
    failed |= test_call_adjust_export_immediate("export_immediate5", my_predbat, ha, inv, dummy_items, 49)
    failed |= test_call_adjust_export_immediate("export_immediate6", my_predbat, ha, inv, dummy_items, 49, repeat=True)
    failed |= test_call_adjust_export_immediate("export_immediate6", my_predbat, ha, inv, dummy_items, 49, discharge_start_time="00:00:00", discharge_end_time="09:00:00")
    failed |= test_call_adjust_export_immediate("export_immediate7", my_predbat, ha, inv, dummy_items, 50, freeze=True)
    failed |= test_call_adjust_export_immediate("export_immediate8", my_predbat, ha, inv, dummy_items, 50, freeze=False, no_freeze=True)
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart0",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service=None,
        expected=[],
        active=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart1",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service", "addon": "adds"},
        expected=[["restart_service", {"addon": "adds"}]],
        expect_notify=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart("auto_restart2", my_predbat, ha, inv, dummy_items, service=[{"command": "service", "service": "restart_service"}], expected=[["restart_service", {}]], expect_notify=True)
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart3",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service"},
        expected=[],
        active=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart4",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service", "entity_id": "switch.restart"},
        expected=[["restart_service", {"entity_id": "switch.restart"}]],
        expect_notify=True,
    )
    if failed:
        return failed

    os.system("touch tmp1234")
    failed |= test_auto_restart(
        "auto_restart5",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "shell": "rm tmp1234"},
        expected=[],
    )
    if failed:
        return failed
    if os.path.exists("tmp1234"):
        print("ERROR: File should be deleted")
        failed = True

    # Test notification config for auto_restart
    failed |= test_auto_restart(
        "auto_restart_notify_default",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service"},
        expected=[["restart_service", {}]],
        expect_notify=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart_notify_enabled",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service"},
        expected=[["restart_service", {}]],
        set_system_notify=True,
        expect_notify=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart_notify_disabled",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service"},
        expected=[["restart_service", {}]],
        set_system_notify=False,
        expect_notify=False,
    )
    if failed:
        return failed

    # Test charge window None handling
    failed |= test_charge_window_none_illegal_time("charge_window_illegal_time", my_predbat, dummy_items)
    if failed:
        return failed

    failed |= test_charge_window_invalid_format_time("charge_window_invalid_format_time", my_predbat, dummy_items)
    if failed:
        return failed

    failed |= test_charge_window_none_value("charge_window_none_value", my_predbat, dummy_items)
    if failed:
        return failed

    # Test discharge window None handling
    failed |= test_discharge_window_none_illegal_time("discharge_window_illegal_time", my_predbat, dummy_items)
    if failed:
        return failed

    failed |= test_discharge_window_invalid_format_time("discharge_window_invalid_format_time", my_predbat, dummy_items)
    if failed:
        return failed

    failed |= test_discharge_window_none_value("discharge_window_none_value", my_predbat, dummy_items)
    if failed:
        return failed

    # Tests for time.* entities used for discharge/charge hour config (GS_fb00 firmware)
    failed |= test_time_entity_hour_write("time_entity_discharge_hour1", ha, inv, dummy_rest, "discharge", "22:30:00", "23:59:00")
    if failed:
        return failed
    failed |= test_time_entity_hour_write("time_entity_discharge_hour2", ha, inv, dummy_rest, "discharge", "00:00:00", "06:30:00")
    if failed:
        return failed
    failed |= test_time_entity_hour_write("time_entity_charge_hour1", ha, inv, dummy_rest, "charge", "01:00:00", "05:30:00")
    if failed:
        return failed
    failed |= test_time_entity_hour_write("time_entity_charge_hour2", ha, inv, dummy_rest, "charge", "23:00:00", "23:59:00")
    if failed:
        return failed

    # Regression tests for issue #4048: input_datetime entities must use set_datetime not set_value
    failed |= test_input_datetime_charge_window("input_datetime_charge1", ha, inv, dummy_rest, "charge", "12:30:00", "16:00:00")
    if failed:
        return failed
    failed |= test_input_datetime_charge_window("input_datetime_discharge1", ha, inv, dummy_rest, "discharge", "22:00:00", "23:59:00")
    if failed:
        return failed

    # Regression test: GS_fb00 H M format must write time entities and press button even when times are unchanged
    failed |= test_force_export_unchanged_times_HM_format("force_export_unchanged_HM_format", ha, inv)
    if failed:
        return failed

    failed |= test_inverter_self_test("self_test1", my_predbat)
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
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
from datetime import datetime
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
    inv.has_inverter_time_button_press = has_inverter_time_button_press
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


def test_adjust_reserve(test_name, ha, inv, dummy_rest, prev_reserve, reserve, expect_reserve=None, reserve_min=4, reserve_max=100):
    """
    Test
       inv.adjust_reserve(self, reserve):
    """
    failed = False
    if expect_reserve is None:
        expect_reserve = reserve

    inv.reserve_percent = reserve_min
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

    inv.adjust_battery_target(soc, isCharging=isCharging, isExporting=isExporting)
    rest_command = dummy_rest.get_commands()
    if expect_soc != prev_soc:
        expect_data = [["dummy/setChargeTarget", {"chargeToPercent": expect_soc}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_inverter_self_test(test_name, my_predbat):
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

    # Define the command patterns
    commands = [
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


def test_auto_restart(test_name, my_predbat, ha, inv, dummy_items, service, expected, active=False):
    print("**** Running Test: {} ****".format(test_name))
    failed = 0
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.restart_active = active

    my_predbat.args["auto_restart"] = service

    failed = 1 if not active else 0
    try:
        inv.auto_restart("Crashed")
    except Exception as e:
        failed = 0
        if str(e) != "Auto-restart triggered":
            print("ERROR: Auto-restart should be triggered got {}".format(e))
            failed = 1

    result = ha.get_service_store()
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: Auto-restart service should be {} got {}".format(expected, result))
        failed = 1
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

    failed |= test_adjust_inverter_mode("adjust_mode_eco1", ha, inv, dummy_rest, "Timed Export", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco2", ha, inv, dummy_rest, "Eco", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco3", ha, inv, dummy_rest, "Eco (Paused)", "Eco", "Eco (Paused)")
    failed |= test_adjust_inverter_mode("adjust_mode_export1", ha, inv, dummy_rest, "Eco (Paused)", "Timed Export", "Timed Export")
    failed |= test_adjust_inverter_mode("adjust_mode_export2", ha, inv, dummy_rest, "Timed Export", "Timed Export", "Timed Export")
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
        expected=[["restart_service", {"addon": "adds"}], ["notify/notify", {"message": "Auto-restart service restart_service called due to: Crashed"}]],
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart2", my_predbat, ha, inv, dummy_items, service=[{"command": "service", "service": "restart_service"}], expected=[["restart_service", {}], ["notify/notify", {"message": "Auto-restart service restart_service called due to: Crashed"}]]
    )
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
        expected=[["restart_service", {"entity_id": "switch.restart"}], ["notify/notify", {"message": "Auto-restart service restart_service called due to: Crashed"}]],
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

    failed |= test_inverter_self_test("self_test1", my_predbat)
    return failed

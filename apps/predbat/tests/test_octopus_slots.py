# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
from tests.test_infra import reset_rates
from utils import dp2
import json


def run_load_octopus_slot_test(testname, my_predbat, slots, expected_slots, consider_full, car_soc, car_limit, car_loss):
    """
    Run a test for load_octopus_slot
    octopus_slots = load_octopus_slots(self, octopus_slots, octopus_intelligent_consider_full)
    """
    failed = False
    print("**** Running Test: load_octopus_slot {} ****".format(testname))
    my_predbat.octopus_slots = slots
    my_predbat.octopus_intelligent_consider_full = consider_full
    my_predbat.car_charging_soc[0] = car_soc
    my_predbat.car_charging_limit[0] = car_limit
    my_predbat.car_charging_loss = car_loss

    result = my_predbat.load_octopus_slots(slots, consider_full)
    if json.dumps(result) != json.dumps(expected_slots):
        print("Test: {} failed consider_full: {} car_soc: {} car_limit: {} car_loss: {} minutes_now: {}".format(testname, consider_full, car_soc, car_limit, car_loss, my_predbat.minutes_now))
        print("ERROR: Slots should be:\n\n{}\ngot:\n{}".format(expected_slots, result))
        print("\nOriginal slots {}".format(slots))
        failed = True
    return failed


def run_load_octopus_slots_tests(my_predbat):
    """
    Test for load octopus slots


    slots are in format:

    - start: '2025-01-30T00:00:00+00:00'
      end: '2025-01-30T00:30:00+00:00'
      charge_in_kwh: -2.56
      source: null
      location: AT_HOME

    """
    failed = 0

    TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

    slots = []
    slots2 = []
    slots3 = []
    slots4 = []
    slots5 = []
    slots6 = []
    expected_slots = []
    expected_slots2 = []
    expected_slots3 = []
    expected_slots4 = []
    expected_slots5 = []
    expected_slots6 = []
    expected_slots7 = []
    now_utc = my_predbat.now_utc
    now_utc = now_utc.replace(minute=5, second=0, microsecond=0, hour=14)
    my_predbat.minutes_now = int((now_utc - my_predbat.midnight_utc).total_seconds() / 60)
    midnight_utc = my_predbat.midnight_utc

    reset_rates(my_predbat, 10, 5)
    my_predbat.rate_min = 4
    my_predbat.car_charging_rate = [5.0]

    # Created 8 slots in total in the next 16 hours
    soc = 2.0
    soc2 = 2.0
    for i in range(8):
        start = now_utc + timedelta(minutes=i * 60)
        start_plus_15 = start + timedelta(minutes=15)
        start_minus_30 = start - timedelta(minutes=30)
        end = start + timedelta(minutes=60)
        prev_soc = soc
        prev_soc2 = soc2
        soc += 5
        soc2 += 2.5
        slots.append({"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": -5, "source": "null", "location": "AT_HOME"})
        slots5.append({"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "source": "null", "location": "AT_HOME"})
        slots2.append({"start": start.strftime(TIME_FORMAT) if i >= 1 else start_plus_15.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": -5, "source": "null", "location": "AT_HOME"})
        slots3.append({"start": start.strftime(TIME_FORMAT) if i >= 1 else start_minus_30.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": -5 if i >= 1 else -5 * 1.5, "source": "null", "location": "AT_HOME"})
        slots6.append({"start": start.strftime(TIME_FORMAT) if i >= 1 else start_minus_30.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "source": "null", "location": "AT_HOME"})
        minutes_start = int((start - midnight_utc).total_seconds() / 60)
        minutes_end = int((end - midnight_utc).total_seconds() / 60)
        expected_slots.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0, "average": 4, "cost": 20.0, "soc": 0.0})
        expected_slots7.append({"start": minutes_start, "end": minutes_end, "kwh": my_predbat.car_charging_rate[0], "average": 4, "cost": my_predbat.car_charging_rate[0] * 4, "soc": 0.0})
        expected_slots2.append({"start": minutes_start, "end": minutes_end, "kwh": 0.0, "average": 4, "cost": 0.0, "soc": 0.0})
        expected_slots3.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0 if soc <= 12.0 else 0.0, "average": 4, "cost": 20.0 if soc <= 12.0 else 0.0, "soc": min(soc, 12.0)})
        if prev_soc2 < 10.0 and soc2 >= 10.0:
            extra_minutes = int(30 / (5 * 0.5) * 1)
            expected_slots4.append({"start": minutes_start, "end": minutes_start + extra_minutes, "kwh": 1.0, "average": 4, "cost": 1 * 4.0, "soc": min(soc2, 10.0)})
            expected_slots4.append({"start": minutes_start + extra_minutes, "end": minutes_end, "kwh": 5.0 if soc <= 20.0 else 0.0, "average": 4, "cost": 20.0 if soc <= 20.0 else 0.0, "soc": min(soc2, 10.0)})
        else:
            expected_slots4.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0 if soc <= 20.0 else 0.0, "average": 4, "cost": 20.0 if soc <= 20.0 else 0.0, "soc": min(soc2, 10.0)})
        if i >= 1:
            expected_slots5.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0, "average": 4, "cost": 4 * 5.0, "soc": 10})
        else:
            expected_slots5.append({"start": minutes_start - 30, "end": minutes_end, "kwh": 5 * 1.5, "average": 4, "cost": 4 * 5.0 * 1.5, "soc": 7.5})

    # Extra test
    start = now_utc - timedelta(minutes=121)
    end = now_utc + timedelta(minutes=25)
    minutes_start = int((start - midnight_utc).total_seconds() / 60)
    minutes_end = int((end - midnight_utc).total_seconds() / 60)
    slots4.append({"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": -20, "source": "null", "location": "AT_HOME"})
    expected_slots6.append({"start": minutes_start, "end": minutes_end, "kwh": 20.0, "average": 4, "cost": 20.0 * 4, "soc": 20.0})

    failed |= run_load_octopus_slot_test("test1", my_predbat, slots, expected_slots, False, 2.0, 0.0, 1.0)

    # Misalign the start time by 15 minutes
    expected_slots[0]["start"] += 15
    failed |= run_load_octopus_slot_test("test1b", my_predbat, slots2, expected_slots, False, 2.0, 0.0, 1.0)

    failed |= run_load_octopus_slot_test("test2", my_predbat, slots, expected_slots2, True, 2.0, 0.0, 1.0)
    failed |= run_load_octopus_slot_test("test3", my_predbat, slots, expected_slots3, True, 2.0, 12.0, 1.0)
    failed |= run_load_octopus_slot_test("test4", my_predbat, slots, expected_slots4, True, 2.0, 10.0, 0.5)
    failed |= run_load_octopus_slot_test("test5", my_predbat, slots3, expected_slots5, False, 0, 10, 1.0)
    failed |= run_load_octopus_slot_test("test6", my_predbat, slots4, expected_slots6, False, 0, 100, 1.0)
    failed |= run_load_octopus_slot_test("test7", my_predbat, slots5, expected_slots7, False, 2.0, 0.0, 1.0)
    failed |= run_load_octopus_slot_test("test8", my_predbat, slots6, expected_slots5, False, 0, 10, 1.0)
    if failed:
        return failed

    today_string = my_predbat.midnight_utc.strftime("%Y-%m-%dT")
    today_tz_offset = my_predbat.midnight_utc.strftime("%z")
    today_tz_offset = today_tz_offset[:3] + ":" + today_tz_offset[3:]
    print("today tz_offset {}".format(today_tz_offset))
    sample_bad = [
        {"charge_in_kwh": -1.29, "end": today_string + "15:00:00" + today_tz_offset, "location": "AT_HOME", "source": None, "start": today_string + "14:30:00" + today_tz_offset},
        {"charge_in_kwh": -3.17, "end": today_string + "15:30:00" + today_tz_offset, "location": "AT_HOME", "source": None, "start": today_string + "15:00:00" + today_tz_offset},
        {"charge_in_kwh": -3.18, "end": today_string + "16:00:00" + today_tz_offset, "location": "AT_HOME", "source": None, "start": today_string + "15:30:00" + today_tz_offset},
        {"charge_in_kwh": -3.14, "end": today_string + "16:30:00" + today_tz_offset, "location": "AT_HOME", "source": None, "start": today_string + "16:00:00" + today_tz_offset},
        {"charge_in_kwh": -7.47, "end": today_string + "17:30:00" + today_tz_offset, "location": None, "source": "smart-charge", "start": today_string + "16:26:00" + today_tz_offset},
        {"charge_in_kwh": -3.0, "end": today_string + "18:00:00" + today_tz_offset, "location": None, "source": "smart-charge", "start": today_string + "17:30:00" + today_tz_offset},
        {"charge_in_kwh": -3.5, "end": today_string + "03:00:00" + today_tz_offset, "location": None, "source": "smart-charge", "start": today_string + "02:30:00" + today_tz_offset},
        {"charge_in_kwh": -3.5, "end": today_string + "05:30:00" + today_tz_offset, "location": None, "source": "smart-charge", "start": today_string + "05:00:00" + today_tz_offset},
        {"end": today_string + "18:00:00" + today_tz_offset, "start": today_string + "17:30:00" + today_tz_offset, "source": "null", "location": "AT_HOME"},
    ]

    loaded_slots = my_predbat.load_octopus_slots(sample_bad, False)
    expected_loaded = "[{'start': 870, 'end': 900, 'kwh': 1.29, 'average': 4, 'cost': 5.16, 'soc': 1.29}, {'start': 900, 'end': 930, 'kwh': 3.17, 'average': 4, 'cost': 12.68, 'soc': 4.46}, {'start': 930, 'end': 960, 'kwh': 3.18, 'average': 4, 'cost': 12.72, 'soc': 7.640000000000001}, {'start': 960, 'end': 990, 'kwh': 3.14, 'average': 4, 'cost': 12.56, 'soc': 10}, {'start': 990, 'end': 1050, 'kwh': 7.47, 'average': 4, 'cost': 29.88, 'soc': 10}, {'start': 1050, 'end': 1080, 'kwh': 3.0, 'average': 4, 'cost': 12.0, 'soc': 10}]"
    if str(loaded_slots) != expected_loaded:
        print("ERROR: Loaded slots should be {}\ngot {}".format(expected_loaded, loaded_slots))
        failed = True

    if failed:
        return failed

    print("**** Checking car_charge_slot_kwh ****")
    my_predbat.car_charging_slots[0] = expected_slots5
    my_predbat.num_cars = 1

    print("Test car_charge_slot_kwh test1")
    charge = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now, my_predbat.minutes_now + 30)
    expected_charge = dp2(5 / 60 * 30)
    if charge != expected_charge:
        print("ERROR: Car charge slot kWh should be {} got {}".format(expected_charge, charge))
        failed = True
    print("Test car_charge_slot_kwh test2")

    expected_charge = dp2(5 / 60 * 30)
    charge = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now + 30, my_predbat.minutes_now + 60)
    if charge != expected_charge:
        print("ERROR: Car charge slot kWh should be {} got {}".format(expected_charge, charge))
        failed = True

    print("Test car_charge_slot_kwh test3")
    expected_charge = dp2(5 / 60 * 15)
    charge = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now + 15, my_predbat.minutes_now + 30)
    if charge != expected_charge:
        print("ERROR: Car charge slot kWh should be {} got {}".format(expected_charge, charge))
        failed = True

    print("Test car_charge_slot_kwh test4")
    my_predbat.car_charging_slots[0] = expected_slots6
    expected_charge = dp2((20 / (121 + 25)) * 25)
    charge = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now, my_predbat.minutes_now + 25)
    if charge != expected_charge:
        print("ERROR: Car charge slot kWh should be {} got {}".format(expected_charge, charge))
        print("Slots {} start {} end {}".format(my_predbat.car_charging_slots[0], my_predbat.minutes_now, my_predbat.minutes_now + 25))
        failed = True

    print("Test car_charge_slot_kwh test5")
    my_predbat.car_charging_slots[0] = expected_slots5
    expected_charge = dp2(25 * 5.0 / 60)
    charge = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now, my_predbat.minutes_now + 25)
    if charge != expected_charge:
        print("ERROR: Car charge slot kWh should be {} got {}".format(expected_charge, charge))
        print("Slots {} start {} end {}".format(my_predbat.car_charging_slots[0], my_predbat.minutes_now, my_predbat.minutes_now + 25))
        failed = True

    print("Test car_charge_slot_kwh test6")
    my_predbat.car_charging_slots[0] = expected_slots5
    expected_charge = dp2(5 * 5.0 / 60)
    charge = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now + 25, my_predbat.minutes_now + 30)
    if charge != expected_charge:
        print("ERROR: Car charge slot kWh should be {} got {}".format(expected_charge, charge))
        print("Slots {} start {} end {}".format(my_predbat.car_charging_slots[0], my_predbat.minutes_now, my_predbat.minutes_now + 25))
        failed = True

    my_predbat.car_charging_slots[0] = []
    my_predbat.num_cars = 0

    return failed

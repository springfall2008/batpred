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
Rate read-back for an inverter with no charge_rate/discharge_rate entity (#3311).

A script-driven "power" inverter (the Solax SX4 template) is sent its rate as {power} in
charge_start_service / discharge_start_service and has no rate register to read back. These tests
build that inverter from the template's own inverter: block and check the rate it reads back, and the
{power} it is sent, is the one Predbat chose rather than battery_rate_max.
"""

import os

import yaml

from config import INVERTER_DEF
from inverter import Inverter
from tests.test_inverter import dummy_sleep

SCRIPT_TYPE = "SOLAX_SCRIPT_TEST"
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "templates", "solax_sx4.yaml")
RATE_MAX = 6000
RATE_KEYS = ["charge_rate", "discharge_rate", "charge_rate_percent", "discharge_rate_percent"]


def _template_inverter_def():
    """The inverter: block of templates/solax_sx4.yaml, the #3311 setup."""
    with open(TEMPLATE) as handle:
        return yaml.safe_load(handle)["pred_bat"]["inverter"]


def _build_script_inverter(my_predbat, rates=None, rate_max=RATE_MAX):
    """Build inverter 0 as the template's script-driven type, with only the rate entities given in rates."""
    my_predbat.args["inverter_type"] = [SCRIPT_TYPE]
    my_predbat.args["inverter"] = _template_inverter_def()
    my_predbat.args["num_inverters"] = 1
    my_predbat.args["givtcp_rest"] = None
    my_predbat.args["battery_rate_max"] = rate_max
    my_predbat.args["soc_max"] = 10
    my_predbat.args["charge_start_service"] = "charge_start"
    my_predbat.args["charge_stop_service"] = "charge_stop"
    my_predbat.args["discharge_start_service"] = "discharge_start"
    my_predbat.args["discharge_stop_service"] = "discharge_stop"
    my_predbat.args["charge_freeze_service"] = None
    my_predbat.args["discharge_freeze_service"] = None
    my_predbat.args["device_id"] = "DID0"
    for key in RATE_KEYS:
        my_predbat.args.pop(key, None)
    my_predbat.args.update(rates or {})
    inv = Inverter(my_predbat, 0)
    inv.soc_percent = 50
    inv.sleep = dummy_sleep
    return inv


def _service_power(my_predbat, inv, start):
    """Call the immediate charge (or export) path and return the {power} each start service was sent."""
    ha = my_predbat.ha_interface
    my_predbat.last_service_hash.clear()
    ha.service_store_enable = True
    ha.service_store = []
    try:
        if start == "charge":
            inv.adjust_charge_immediate(80)
        else:
            inv.adjust_export_immediate(10)
        return [data.get("power") for service, data in ha.get_service_store() if service == start + "_start"]
    finally:
        # A stored service call is not applied, so leaving this on would stop every later entity write landing
        ha.service_store_enable = False


def _check(test_name, label, got, expect):
    """Report a mismatch; return True when it failed."""
    if got != expect:
        print("ERROR: {} {}: got {} expected {}".format(test_name, label, got, expect))
        return True
    return False


def test_rate_reaches_start_service(test_name, my_predbat):
    """The rate last set is read back and sent as {power}, for charge and discharge (#3311)."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat)
    failed = False
    failed |= _check(test_name, "charge rate before any is set", inv.get_current_charge_rate(), RATE_MAX)
    failed |= _check(test_name, "discharge rate before any is set", inv.get_current_discharge_rate(), RATE_MAX)

    inv.adjust_charge_rate(2500, notify=False)
    inv.adjust_discharge_rate(1500, notify=False)
    failed |= _check(test_name, "charge rate read back", inv.get_current_charge_rate(), 2500)
    failed |= _check(test_name, "discharge rate read back", inv.get_current_discharge_rate(), 1500)
    failed |= _check(test_name, "charge_start_service power", _service_power(my_predbat, inv, "charge"), [2500])
    failed |= _check(test_name, "discharge_start_service power", _service_power(my_predbat, inv, "discharge"), [1500])

    inv.adjust_charge_rate(RATE_MAX, notify=False)
    failed |= _check(test_name, "charge rate reset to max", inv.get_current_charge_rate(), RATE_MAX)
    return failed


def test_rate_held_within_deadband(test_name, my_predbat):
    """A change smaller than the write deadband keeps the rate held, as a register would, so {power} stays steady."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat)
    failed = False
    inv.adjust_charge_rate(2500, notify=False)
    inv.adjust_charge_rate(2540, notify=False)
    failed |= _check(test_name, "charge rate after a change inside the deadband", inv.get_current_charge_rate(), 2500)
    failed |= _check(test_name, "charge_start_service power after a change inside the deadband", _service_power(my_predbat, inv, "charge"), [2500])
    inv.adjust_charge_rate(1500, notify=False)
    failed |= _check(test_name, "charge rate after a change outside the deadband", inv.get_current_charge_rate(), 1500)
    return failed


def test_rate_survives_refresh(test_name, my_predbat):
    """The rate set is held across a config refresh, as the inverter object persists between plan cycles."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat)
    inv.adjust_charge_rate(2500, notify=False)
    inv.refresh_config(quiet=True)
    return _check(test_name, "charge rate after refresh_config", inv.get_current_charge_rate(), 2500)


def test_execute_plan_sends_this_cycles_rate(test_name, my_predbat, rate_max=RATE_MAX):
    """
    Through execute_plan(): the first start call of a window carries the rate planned for that cycle.

    execute_plan() applies rates after its per-inverter loop, so the start services in the loop are
    passed this cycle's rate (#5252) rather than reading back the last one set - which on the first
    cycle of a window is the previous cycle's: the maximum after an idle cycle, or 0 straight after an
    export (charge_discharge_with_rate holds the other direction at 0).

    Run at 10kW too, where the 5% deadband (500W) is wider than the lowest low power rate (400W): a move
    from the 0 an export holds must still count as a change, or the charge is sent 0 and never starts.
    """
    print("**** Running Test: {} ****".format(test_name))
    ha = my_predbat.ha_interface
    inv = _build_script_inverter(my_predbat, rate_max=rate_max)
    my_predbat.inverters = [inv]
    my_predbat.set_read_only = False
    my_predbat.inverter_needs_reset = False
    my_predbat.set_charge_window = True
    my_predbat.set_export_window = True
    my_predbat.set_soc_enable = True
    my_predbat.set_reserve_enable = False
    my_predbat.set_charge_low_power = True
    my_predbat.charge_low_power_margin = 10
    my_predbat.set_export_low_power = False
    my_predbat.balance_inverters_enable = False
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_now = [False]
    my_predbat.num_cars = 1
    my_predbat.soc_max = inv.soc_max = 10
    my_predbat.soc_kw = inv.soc_kw = 5
    inv.soc_percent = 50
    my_predbat.battery_rate_max_charge = my_predbat.battery_rate_max_discharge = my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_charge_dc = rate_max / 60000
    my_predbat.pv_forecast_minute = {}
    now = my_predbat.minutes_now

    def plan(mode):
        """Plan an idle, charging or exporting cycle starting now."""
        my_predbat.charge_window_best, my_predbat.charge_limit_best = [], []
        my_predbat.export_window_best, my_predbat.export_limits_best = [], []
        inv.charge_start_time_minutes = inv.charge_end_time_minutes = my_predbat.forecast_minutes
        if mode == "charge":
            # A long window for 1kWh, so low power charging plans well under the maximum
            my_predbat.charge_window_best, my_predbat.charge_limit_best = [{"start": now, "end": now + 240, "average": 5}], [6.0]
            inv.charge_start_time_minutes, inv.charge_end_time_minutes = now, now + 240
        elif mode == "export":
            my_predbat.export_window_best, my_predbat.export_limits_best = [{"start": now - 10, "end": now + 20, "average": 10}], [10.0]

    def cycle(mode):
        """Run one execute_plan() cycle and return the {power} of each start service it sent."""
        plan(mode)
        ha.service_store_enable = True
        ha.service_store = []
        try:
            my_predbat.execute_plan()
            return [data.get("power") for service, data in ha.get_service_store() if service.endswith("_start")]
        finally:
            ha.service_store_enable = False

    failed = False
    for after in ("idle", "export"):
        # Each case starts from an idle cycle, which sets both rates to the maximum
        cycle("idle")
        if after == "export":
            cycle("export")
        sent = cycle("charge")
        planned = inv.rate_last_set.get("charge")
        if not planned or planned >= rate_max:
            print("ERROR: {} charge after {}: expected a low power rate to be planned, got {}".format(test_name, after, planned))
            failed = True
        failed |= _check(test_name, "first charge_start_service power after {}".format(after), sent, [planned])
        failed |= _check(test_name, "second charge cycle at the same rate after {}".format(after), cycle("charge"), [])
    return failed


def test_rate_entity_still_read(test_name, my_predbat):
    """With a rate entity configured, its state is read, not the rate last set."""
    print("**** Running Test: {} ****".format(test_name))
    ha = my_predbat.ha_interface
    ha.dummy_items["number.test_charge_rate"] = RATE_MAX
    ha.dummy_items["number.test_discharge_rate"] = RATE_MAX
    inv = _build_script_inverter(my_predbat, {"charge_rate": "number.test_charge_rate", "discharge_rate": "number.test_discharge_rate"})
    inv.adjust_charge_rate(2500, notify=False)
    inv.adjust_discharge_rate(1500, notify=False)
    # Something outside Predbat moves the entities after the write
    ha.dummy_items["number.test_charge_rate"] = 3000
    ha.dummy_items["number.test_discharge_rate"] = 3500
    failed = False
    failed |= _check(test_name, "charge rate read from the entity", inv.get_current_charge_rate(), 3000)
    failed |= _check(test_name, "discharge rate read from the entity", inv.get_current_discharge_rate(), 3500)
    return failed


def run_inverter_rate_no_entity_tests(my_predbat):
    """Run the no-rate-entity read-back tests; each rebuilds the inverter and its args."""
    try:
        failed = False
        failed |= test_rate_reaches_start_service("inverter_rate_no_entity_start_service", my_predbat)
        failed |= test_rate_held_within_deadband("inverter_rate_no_entity_deadband", my_predbat)
        failed |= test_rate_survives_refresh("inverter_rate_no_entity_refresh", my_predbat)
        failed |= test_rate_entity_still_read("inverter_rate_no_entity_entity_read", my_predbat)
        return failed
    finally:
        # The custom type is registered in the global INVERTER_DEF, which outlives this test's PredBat
        INVERTER_DEF.pop(SCRIPT_TYPE, None)


def run_inverter_rate_no_entity_execute_tests(my_predbat):
    """execute_plan() sends this cycle's rate to a script-driven inverter, at 6kW."""
    try:
        return test_execute_plan_sends_this_cycles_rate("inverter_rate_no_entity_execute_plan", my_predbat)
    finally:
        INVERTER_DEF.pop(SCRIPT_TYPE, None)


def run_inverter_rate_no_entity_execute_10kw_tests(my_predbat):
    """As run_inverter_rate_no_entity_execute_tests() at 10kW, where the deadband is wider than the lowest low power rate."""
    try:
        return test_execute_plan_sends_this_cycles_rate("inverter_rate_no_entity_execute_plan_10kw", my_predbat, rate_max=10000)
    finally:
        INVERTER_DEF.pop(SCRIPT_TYPE, None)

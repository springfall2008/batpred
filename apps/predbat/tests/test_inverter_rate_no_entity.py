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
from const import POWER_SERVICES
from utils import services_send_power
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


def test_rate_held_like_a_register(test_name, my_predbat):
    """A rate Predbat holds moves as a register would - a small change is held - except that any move to or from 0 goes through."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat, rate_max=10000)  # a 500W deadband
    failed = False
    for rate, expect in ((1300, 1300), (900, 1300), (0, 0), (400, 400), (600, 400), (2000, 2000)):
        inv.adjust_charge_rate(rate, notify=False)
        failed |= _check(test_name, "charge rate read back after setting {}W".format(rate), inv.get_current_charge_rate(), expect)
    failed |= _check(test_name, "charge_start_service power", _service_power(my_predbat, inv, "charge"), [2000])
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

    execute_plan() applies rates after its per-inverter loop, and calls the start services after that
    (#5252), so they send this cycle's rate rather than the previous cycle's: the maximum after an idle
    cycle, or 0 straight after an export (charge_discharge_with_rate holds the other direction at 0).

    Run at 10kW too, where a register's 5% deadband (500W) is wider than the lowest low power rate
    (400W): a rate Predbat holds itself must still move off the 0 an export holds, or the charge is sent
    {power} 0 and never starts.
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


def test_register_small_rate_not_rewritten(test_name, my_predbat):
    """
    The zero rule is not applied to a rate register: one reading 0 after a small rate was asked for is left alone.

    A device that stores a small rate as 0 (GivEnergy's whole-percent step, a *_rate_percent under 1%)
    reads back 0 for, say, 49W on a 10kW battery. Treating that as a change would rewrite it every cycle.
    """
    print("**** Running Test: {} ****".format(test_name))
    ha = my_predbat.ha_interface
    ha.dummy_items["number.test_charge_rate"] = 0
    ha.dummy_items["number.test_discharge_rate"] = 0
    inv = _build_script_inverter(my_predbat, {"charge_rate": "number.test_charge_rate", "discharge_rate": "number.test_discharge_rate"}, rate_max=10000)
    # Each rate "change" logs, notifies and sends MQTT, whether or not write_and_poll_value() then writes
    changes = []
    inv.mqtt_message = lambda topic, payload: changes.append(topic)
    ha.service_store_enable = True
    ha.service_store = []
    try:
        for _ in range(3):
            inv.adjust_charge_rate(49, notify=False)
            inv.adjust_discharge_rate(49, notify=False)
        writes = [data for service, data in ha.get_service_store() if service == "number/set_value"]
    finally:
        ha.service_store_enable = False
    failed = False
    failed |= _check(test_name, "rate writes for a 49W rate on a register reading 0", writes, [])
    failed |= _check(test_name, "rate changes reported for a 49W rate on a register reading 0", changes, [])

    # A percentage register only (GE Cloud's 3-phase units): 49W of 10kW is stored as 0%
    ha.dummy_items["number.test_charge_rate_percent"] = 0
    inv = _build_script_inverter(my_predbat, {"charge_rate_percent": "number.test_charge_rate_percent"}, rate_max=10000)
    changes = []
    inv.mqtt_message = lambda topic, payload: changes.append(topic)
    for _ in range(3):
        inv.adjust_charge_rate(49, notify=False)
    failed |= _check(test_name, "rate changes reported for a 49W rate on a percentage register reading 0%", changes, [])
    return failed


def test_number_in_rate_entry_is_not_an_entity(test_name, my_predbat):
    """A plain number in this inverter's rate entry - another inverter's default, in a mixed fleet - leaves Predbat holding the rate, and is never written."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat, {"charge_rate": [6000.0, "sensor.other_inverter_charge_rate"]})
    logged = []
    base_log = my_predbat.log
    my_predbat.log = lambda message, *args, **kwargs: (logged.append(str(message)), base_log(message, *args, **kwargs))
    try:
        inv.adjust_charge_rate(2500, notify=False)
    finally:
        my_predbat.log = base_log
    failed = False
    failed |= _check(test_name, "charge rate read back", inv.get_current_charge_rate(), 2500)
    # A write would be refused with a "fixed value" warning, every time the rate changed
    failed |= _check(test_name, "write attempts to the number in the rate entry", [message for message in logged if "fixed value" in message], [])
    return failed


def test_rate_not_held_without_power_services(test_name, my_predbat):
    """With no start or freeze service to send {power}, nothing applies a rate Predbat would hold, so it reads back the maximum as before."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat)
    failed = False
    for label, service in (("no start or freeze services", None), ("a start service template without {power}", {"service": "switch.turn_on", "entity_id": "switch.grid_charge"})):
        for name in POWER_SERVICES:
            my_predbat.args[name] = None
        my_predbat.args["charge_start_service"] = service
        inv.refresh_config(quiet=True)
        inv.rate_last_set = {}
        inv.adjust_charge_rate(2500, notify=False)
        failed |= _check(test_name, "charge rate read back with " + label, inv.get_current_charge_rate(), RATE_MAX)
    return failed


def test_services_send_power(test_name, my_predbat):
    """services_send_power(): a service by name alone, or a template referencing {power}, sends the rate; other templates do not."""
    print("**** Running Test: {} ****".format(test_name))
    failed = False
    for label, args, expect in (
        ("no services", {}, False),
        ("start service by name", {"charge_start_service": "script.charge"}, True),
        ("freeze template with {power}", {"discharge_freeze_service": {"service": "script.hold", "power": "{power}"}}, True),
        ("template list, one with {power}", {"charge_start_service": [{"service": "switch.turn_on", "entity_id": "switch.x"}, {"service": "script.rate", "power": "{power}"}]}, True),
        ("template with {power:.0f}", {"charge_start_service": {"service": "script.rate", "power": "{power:.0f}"}}, True),
        ("template without {power}", {"charge_start_service": {"service": "switch.turn_on", "entity_id": "switch.grid_charge"}}, False),
        ("template with {power_max}", {"charge_start_service": {"service": "script.rate", "limit": "{power_max}"}}, False),
        ("stop service only", {"charge_stop_service": "script.stop"}, False),
    ):
        failed |= _check(test_name, label, services_send_power(args), expect)
    # Per direction: a {power} on the charge hooks says nothing about the discharge side, and the reverse
    charge_only = {"charge_start_service": {"service": "script.rate", "power": "{power}"}, "discharge_start_service": {"service": "switch.turn_on", "entity_id": "switch.x"}}
    for label, args, direction, expect in (
        ("charge-only {power}, charge", charge_only, "charge", True),
        ("charge-only {power}, discharge", charge_only, "discharge", False),
        ("discharge freeze {power}, discharge", {"discharge_freeze_service": "script.hold"}, "discharge", True),
        ("discharge freeze {power}, charge", {"discharge_freeze_service": "script.hold"}, "charge", False),
    ):
        failed |= _check(test_name, label, services_send_power(args, direction), expect)
    return failed


def test_queue_immediate(test_name, my_predbat):
    """queue_immediate() makes the call at once when there is no queue, and otherwise queues it."""
    print("**** Running Test: {} ****".format(test_name))
    failed = False
    made, queued = [], []
    my_predbat.queue_immediate(None, lambda: made.append(True))
    failed |= _check(test_name, "calls made at once without a queue", len(made), 1)
    my_predbat.queue_immediate(queued, lambda: made.append(True))
    failed |= _check(test_name, "calls made at once with a queue", len(made), 1)
    failed |= _check(test_name, "calls queued", len(queued), 1)
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
    # An entity with no unit (an input_number helper) holds a decimal string, which must still read as a number
    ha.dummy_items["number.test_charge_rate"] = "2500.0"
    failed |= _check(test_name, "charge rate read from a decimal string with no unit", inv.get_current_charge_rate(), 2500)
    return failed


def test_rate_held_per_direction(test_name, my_predbat):
    """Only the direction whose services send {power} holds a rate itself; the other reads back as before."""
    print("**** Running Test: {} ****".format(test_name))
    inv = _build_script_inverter(my_predbat)
    saved = {key: my_predbat.args.get(key) for key in ("charge_start_service", "discharge_start_service")}
    my_predbat.args["charge_start_service"] = {"service": "script.rate", "power": "{power}"}
    my_predbat.args["discharge_start_service"] = {"service": "switch.turn_on", "entity_id": "switch.x"}
    try:
        failed = False
        failed |= _check(test_name, "charge held (its service sends {power})", inv.rate_without_entity("charge"), True)
        failed |= _check(test_name, "discharge not held (its services do not)", inv.rate_without_entity("discharge"), False)
        # Setting a discharge rate records nothing, so it still reads back as the battery maximum
        inv.rate_last_set["discharge"] = 1234
        failed |= _check(test_name, "discharge read back as before", inv.get_current_discharge_rate(), RATE_MAX)
    finally:
        my_predbat.args.update(saved)
    return failed


def run_inverter_rate_no_entity_tests(my_predbat):
    """Run the no-rate-entity read-back tests; each rebuilds the inverter and its args."""
    try:
        failed = False
        failed |= test_rate_reaches_start_service("inverter_rate_no_entity_start_service", my_predbat)
        failed |= test_rate_held_like_a_register("inverter_rate_no_entity_held", my_predbat)
        failed |= test_rate_survives_refresh("inverter_rate_no_entity_refresh", my_predbat)
        failed |= test_rate_entity_still_read("inverter_rate_no_entity_entity_read", my_predbat)
        failed |= test_register_small_rate_not_rewritten("inverter_rate_no_entity_register_small_rate", my_predbat)
        failed |= test_services_send_power("inverter_rate_no_entity_services_send_power", my_predbat)
        failed |= test_rate_held_per_direction("inverter_rate_no_entity_per_direction", my_predbat)
        failed |= test_queue_immediate("inverter_rate_no_entity_queue_immediate", my_predbat)
        failed |= test_number_in_rate_entry_is_not_an_entity("inverter_rate_no_entity_number_entry", my_predbat)
        failed |= test_rate_not_held_without_power_services("inverter_rate_no_entity_no_power_services", my_predbat)
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
    """As run_inverter_rate_no_entity_execute_tests() at 10kW, where a register's deadband is wider than the lowest low power rate."""
    try:
        return test_execute_plan_sends_this_cycles_rate("inverter_rate_no_entity_execute_plan_10kw", my_predbat, rate_max=10000)
    finally:
        INVERTER_DEF.pop(SCRIPT_TYPE, None)

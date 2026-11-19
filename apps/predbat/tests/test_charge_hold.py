# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests that a charge freeze holds the battery in the model, less any inverter_battery_rate_min leak.

It is easy to read run_prediction and conclude the hold depends on set_reserve_enable, because that is
what gates reserve_expected. It does not. During a freeze the charge limit equals the SoC, which trips
the discharge-during-charge branch and caps the discharge rate at battery_rate_min - so the battery is
held whether or not Predbat owns the reserve register. The hold is exact only when battery_rate_min is 0
(the default); otherwise the battery leaks at that rate, and with reserve control on reserve_expected
sits battery_rate_min * step below the limit so the leak can happen (GH#6269).

That distinction matters for inverters driven by service hooks rather than a reserve entity, where
set_reserve_enable is forced off (in execute.py, when the inverter has no reserve SoC) but the hardware
still holds. These tests pin the behaviour so a future change to either branch cannot quietly un-hold
those setups, or hide the leak again.

Driven directly rather than through simple_scenario, which renders a matplotlib figure on any assertion
failure and blocks.
"""

from tests.test_infra import reset_rates, reset_inverter
from prediction import Prediction

IMPORT_RATE = 10.0
EXPORT_RATE = 5.0


def run_hour(my_predbat, reserve_enable, freeze, battery_rate_min=0):
    """Simulate one hour and return (final SoC, imported kWh).

    No solar and 1kW of house load, so a battery that is not held gives up 1kWh over the hour while a held one
    keeps its charge and the house imports instead. With freeze set, the charge limit is put equal to
    the reserve, which is what marks a slot as a freeze. battery_rate_min is in kW per minute.
    """
    # Gives the battery real charge/discharge rates and unity losses; without it the rates are whatever
    # the previous test left behind and the battery may not discharge at all
    reset_inverter(my_predbat)
    my_predbat.battery_rate_min = battery_rate_min
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.reserve = 1.0
    my_predbat.set_charge_freeze = True
    my_predbat.set_charge_window = True
    my_predbat.set_reserve_enable = reserve_enable
    my_predbat.debug_enable = False
    # Pin the Python engine. With the C++ kernel dispatching, this exercises a prebuilt binary and
    # cannot catch a regression in the source it mirrors; kernel_parity covers the two agreeing.
    my_predbat.prediction_kernel_enable = False

    reset_rates(my_predbat, IMPORT_RATE, EXPORT_RATE)

    step = 5
    pv_step = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, step)}
    load_step = {minute: 1.0 / (60 / step) for minute in range(0, my_predbat.forecast_minutes, step)}
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    if freeze:
        charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": IMPORT_RATE}]
        charge_limit = [1.0]
    else:
        charge_window = []
        charge_limit = []

    result = my_predbat.run_prediction(charge_limit, charge_window, [], [], False, end_record=60)
    return result[5], result[1] + result[2]


def test_freeze_holds_without_reserve_control(my_predbat):
    """A freeze holds the battery with reserve control on or off; no freeze and it discharges."""
    print("  - test_freeze_holds_without_reserve_control")
    failed = False
    saved = (my_predbat.set_reserve_enable, my_predbat.set_charge_freeze, my_predbat.set_charge_window, getattr(my_predbat, "prediction_kernel_enable", False))

    # Control case: no charge window at all, so the battery covers the house and nothing is bought.
    # Without this the assertions below could pass on a model that never discharges.
    idle_soc, idle_import = run_hour(my_predbat, reserve_enable=True, freeze=False)
    if abs(idle_soc - 4.0) > 0.1:
        print("ERROR: with no freeze the battery should fall to 4.0kWh, got {}".format(idle_soc))
        failed = True
    if idle_import > 0.1:
        print("ERROR: with no freeze the battery covers the load, expected no import, got {}kWh".format(idle_import))
        failed = True

    # A freeze holds whether or not Predbat owns the reserve register
    for reserve_enable in (True, False):
        final_soc, import_kwh = run_hour(my_predbat, reserve_enable=reserve_enable, freeze=True)
        if abs(final_soc - 5.0) > 0.1:
            print("ERROR: freeze with set_reserve_enable={} should hold SoC at 5.0kWh, got {}".format(reserve_enable, final_soc))
            failed = True
        if import_kwh < 0.9:
            print("ERROR: freeze with set_reserve_enable={} should import the hour's load, got {}kWh".format(reserve_enable, import_kwh))
            failed = True

    my_predbat.set_reserve_enable, my_predbat.set_charge_freeze, my_predbat.set_charge_window, my_predbat.prediction_kernel_enable = saved
    return failed


def test_freeze_leaks_battery_rate_min(my_predbat):
    """A freeze leaks inverter_battery_rate_min with reserve control on or off (GH#6269).

    Inverters that keep discharging a little when frozen are described by inverter_battery_rate_min. With
    reserve control on, the modelled reserve used to be raised to the held SoC, leaving no headroom for that
    leak, so the plan showed a perfectly flat SoC while the battery drained. 75W over the hour is 0.075kWh,
    smaller than the 0.1kWh tolerance of the hold test above, so this one checks to the watt-hour.
    """
    print("  - test_freeze_leaks_battery_rate_min")
    failed = False
    saved = (my_predbat.set_reserve_enable, my_predbat.set_charge_freeze, my_predbat.set_charge_window, getattr(my_predbat, "prediction_kernel_enable", False), my_predbat.battery_rate_min)
    leak_rate = 0.075 / 60.0

    for reserve_enable in (True, False):
        final_soc, import_kwh = run_hour(my_predbat, reserve_enable=reserve_enable, freeze=True, battery_rate_min=leak_rate)
        if abs(final_soc - 4.925) > 0.001:
            print("ERROR: freeze with set_reserve_enable={} and a 75W minimum rate should leak to 4.925kWh, got {}".format(reserve_enable, final_soc))
            failed = True
        if abs(import_kwh - 0.925) > 0.001:
            print("ERROR: freeze with set_reserve_enable={} and a 75W minimum rate should import the load less the leak (0.925kWh), got {}kWh".format(reserve_enable, import_kwh))
            failed = True

    # With no minimum rate configured (the default) the hold stays exact
    final_soc, _ = run_hour(my_predbat, reserve_enable=True, freeze=True, battery_rate_min=0)
    if abs(final_soc - 5.0) > 0.001:
        print("ERROR: freeze with no minimum rate should hold exactly 5.0kWh, got {}".format(final_soc))
        failed = True

    my_predbat.set_reserve_enable, my_predbat.set_charge_freeze, my_predbat.set_charge_window, my_predbat.prediction_kernel_enable, my_predbat.battery_rate_min = saved
    return failed


def run_charge_hold_tests(my_predbat):
    """Run every charge-hold modelling test."""
    print("**** Running charge hold tests ****\n")
    failed = test_freeze_holds_without_reserve_control(my_predbat)
    failed |= test_freeze_leaks_battery_rate_min(my_predbat)
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests that a charge freeze actually holds the battery in the model.

It is easy to read prediction.py and conclude the hold depends on set_reserve_enable, because that is
what gates reserve_expected at prediction.py:649. It does not. During a freeze the charge limit equals
the SoC, which trips the discharge-rate branch at prediction.py:812 and zeroes the discharge rate
outright - so the battery is held whether or not Predbat owns the reserve register.

That distinction matters for inverters driven by service hooks rather than a reserve entity, where
set_reserve_enable is forced off (execute.py:828) but the hardware still holds. These tests pin the
behaviour so a future change to either branch cannot quietly un-hold those setups.

Driven directly rather than through simple_scenario, which renders a matplotlib figure on any assertion
failure and blocks.
"""

from tests.test_infra import reset_rates, reset_inverter
from prediction import Prediction

IMPORT_RATE = 10.0
EXPORT_RATE = 5.0


def run_hour(my_predbat, reserve_enable, freeze):
    """Simulate one hour and return (final SoC, imported kWh).

    No solar and 1kW of house load, so a battery that is not held gives up 1kWh over the hour while a held one
    keeps its charge and the house imports instead. With freeze set, the charge limit is put equal to
    the reserve, which is what marks a slot as a freeze.
    """
    # Gives the battery real charge/discharge rates and unity losses; without it the rates are whatever
    # the previous test left behind and the battery may not discharge at all
    reset_inverter(my_predbat)
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


def run_charge_hold_tests(my_predbat):
    """Run every charge-hold modelling test."""
    print("**** Running charge hold tests ****\n")
    return test_freeze_holds_without_reserve_control(my_predbat)

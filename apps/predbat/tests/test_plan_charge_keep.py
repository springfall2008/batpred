# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests that calculate_plan() works out the running charge once, for optimise_charge_limit() to keep.

The charge the inverter is running does not change while a plan is worked out, so calculate_plan() reads the
executor's isCharging / isCharging_Target once into charge_keep_target rather than every optimise_charge_limit()
call doing so. optimise_charge_limit()'s use of it is covered by the optimise_windows tests, and the executor
setting isCharging for a Control SOC only charge (#5991) by the execute tests.
"""
from tests.test_infra import reset_inverter
from utils import calc_percent_limit


def run_plan_charge_keep_tests(my_predbat):
    """Run the running charge keep tests. Returns True on failure."""
    print("**** Running plan charge keep tests ****")
    failed = False

    # The cases change the execution state, so work on a throwaway instance rather than leave it for the tests after
    from unit_test import create_predbat

    my_predbat = create_predbat()
    reset_inverter(my_predbat)

    for label, is_charging, expected in [
        ("charging", True, calc_percent_limit(my_predbat.soc_max * 0.8, my_predbat.soc_max)),
        ("not charging", False, None),
    ]:
        my_predbat.isCharging = is_charging
        my_predbat.isCharging_Target = my_predbat.soc_max * 0.8
        my_predbat.charge_keep_target = "stale"
        # The target is read on entry, before any optimisation, so no plan needs computing
        my_predbat.calculate_plan(recompute=False, publish=False)
        if my_predbat.charge_keep_target != expected:
            print("ERROR: {}: calculate_plan should keep target {} but stored {}".format(label, expected, my_predbat.charge_keep_target))
            failed = True

    if not failed:
        print("**** Plan charge keep tests passed ****")
    return failed

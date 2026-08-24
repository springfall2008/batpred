# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from const import EXPORT_LIMIT_FREEZE, EXPORT_LIMIT_IDLE
from tests.test_infra import reset_inverter


def run_manual_overrides_tests(my_predbat):
    """
    Tests for optimise_charge_windows_manual method
    """
    failed = False
    failed |= test_manual_export_forces_active_export(my_predbat)
    failed |= test_manual_export_clamped_when_export_freeze_only(my_predbat)
    failed |= test_manual_freeze_export_unaffected_by_export_freeze_only(my_predbat)
    failed |= test_manual_demand_unaffected_by_export_freeze_only(my_predbat)
    return failed


def setup(my_predbat, set_export_freeze_only):
    """Put the plan into a known state with a single charge and export window covering the same slot"""
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 1.0
    my_predbat.debug_enable = False
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.set_export_freeze_only = set_export_freeze_only

    my_predbat.manual_demand_times = []
    my_predbat.manual_export_times = []
    my_predbat.manual_freeze_export_times = []
    my_predbat.manual_charge_times = []
    my_predbat.manual_freeze_charge_times = []

    my_predbat.charge_window_best = [{"start": 720, "end": 750, "average": 10.0}]
    my_predbat.charge_limit_best = [5.0]
    my_predbat.export_window_best = [{"start": 720, "end": 750, "average": 10.0}]
    my_predbat.export_limits_best = [EXPORT_LIMIT_IDLE]


def check_export_limit(name, my_predbat, expected):
    """Assert the single export window ended up at the expected limit"""
    actual = my_predbat.export_limits_best[0]
    if actual != expected:
        print("ERROR: {} - export limit should be {} got {}".format(name, expected, actual))
        return True
    return False


def test_manual_export_forces_active_export(my_predbat):
    """Baseline: with set_export_freeze_only off, a manual export override forces a real export"""
    print("**** test_manual_export_forces_active_export ****")
    setup(my_predbat, set_export_freeze_only=False)
    my_predbat.manual_export_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_export_limit("test_manual_export_forces_active_export", my_predbat, 0.0)


def test_manual_export_clamped_when_export_freeze_only(my_predbat):
    """
    #4690: set_export_freeze_only means Predbat may only ever freeze export, never force one -
    optimise_export's own search already honours that, but a manual export override wrote an active
    limit straight into the plan regardless. The model then treats any active limit as a freeze when
    the switch is set, while execution saw a non-freeze limit and left charging enabled, so the plan
    assumed a hold that never happened. Clamp the override to freeze instead.
    """
    print("**** test_manual_export_clamped_when_export_freeze_only ****")
    setup(my_predbat, set_export_freeze_only=True)
    my_predbat.manual_export_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_export_limit("test_manual_export_clamped_when_export_freeze_only", my_predbat, EXPORT_LIMIT_FREEZE)


def test_manual_freeze_export_unaffected_by_export_freeze_only(my_predbat):
    """A manual freeze export override is already a freeze, so the switch must not change it"""
    print("**** test_manual_freeze_export_unaffected_by_export_freeze_only ****")
    setup(my_predbat, set_export_freeze_only=True)
    my_predbat.manual_freeze_export_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_export_limit("test_manual_freeze_export_unaffected_by_export_freeze_only", my_predbat, EXPORT_LIMIT_FREEZE)


def test_manual_demand_unaffected_by_export_freeze_only(my_predbat):
    """A manual demand override disables the export window entirely and must stay that way"""
    print("**** test_manual_demand_unaffected_by_export_freeze_only ****")
    setup(my_predbat, set_export_freeze_only=True)
    my_predbat.manual_demand_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_export_limit("test_manual_demand_unaffected_by_export_freeze_only", my_predbat, EXPORT_LIMIT_IDLE)

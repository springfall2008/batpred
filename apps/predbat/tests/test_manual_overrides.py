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
    try:
        failed |= test_manual_export_forces_active_export(my_predbat)
        failed |= test_manual_export_clamped_when_export_freeze_only(my_predbat)
        failed |= test_manual_freeze_export_unaffected_by_export_freeze_only(my_predbat)
        failed |= test_manual_demand_unaffected_by_export_freeze_only(my_predbat)
        failed |= test_manual_charge_forces_full_charge(my_predbat)
        failed |= test_manual_charge_clamped_when_charge_freeze_only(my_predbat)
        failed |= test_manual_freeze_charge_unaffected_by_charge_freeze_only(my_predbat)
        failed |= test_manual_demand_unaffected_by_charge_freeze_only(my_predbat)
    finally:
        # Most tests here deliberately leave a freeze-only switch on, and both are read all over the
        # planner - clip_charge_slots in particular changes behaviour under set_charge_freeze_only.
        # A full run happens to be safe because of registry ordering, which is not something later
        # tests should have to depend on.
        my_predbat.set_export_freeze_only = False
        my_predbat.set_charge_freeze_only = False
    return failed


def setup(my_predbat, set_export_freeze_only=False, set_charge_freeze_only=False):
    """Put the plan into a known state with a single charge and export window covering the same slot"""
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 1.0
    my_predbat.debug_enable = False
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.set_export_freeze_only = set_export_freeze_only
    my_predbat.set_charge_freeze_only = set_charge_freeze_only

    my_predbat.manual_demand_times = []
    my_predbat.manual_export_times = []
    my_predbat.manual_freeze_export_times = []
    my_predbat.manual_charge_times = []
    my_predbat.manual_freeze_charge_times = []

    my_predbat.charge_window_best = [{"start": 720, "end": 750, "average": 10.0}]
    my_predbat.charge_limit_best = [5.0]
    my_predbat.export_window_best = [{"start": 720, "end": 750, "average": 10.0}]
    my_predbat.export_limits_best = [EXPORT_LIMIT_IDLE]


def check_charge_limit(name, my_predbat, expected):
    """Assert the single charge window ended up at the expected limit"""
    actual = my_predbat.charge_limit_best[0]
    if actual != expected:
        print("ERROR: {} - charge limit should be {} got {}".format(name, expected, actual))
        return True
    return False


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


def test_manual_charge_forces_full_charge(my_predbat):
    """Baseline: with set_charge_freeze_only off, a manual charge override charges to full"""
    print("**** test_manual_charge_forces_full_charge ****")
    setup(my_predbat, set_charge_freeze_only=False)
    my_predbat.manual_charge_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_charge_limit("test_manual_charge_forces_full_charge", my_predbat, my_predbat.soc_max)


def test_manual_charge_clamped_when_charge_freeze_only(my_predbat):
    """
    set_charge_freeze_only means Predbat may only ever freeze charge, never grid charge. The
    optimiser's own search honours that, so a manual charge override must not be the one path that
    writes an active charge target into the plan - otherwise the plan assumes an import that
    execution refuses to perform. Clamp the override to freeze charge, the closest available
    approximation of "hold what you have right now" (same treatment as the manual export clamp).
    """
    print("**** test_manual_charge_clamped_when_charge_freeze_only ****")
    setup(my_predbat, set_charge_freeze_only=True)
    my_predbat.manual_charge_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_charge_limit("test_manual_charge_clamped_when_charge_freeze_only", my_predbat, my_predbat.reserve)


def test_manual_freeze_charge_unaffected_by_charge_freeze_only(my_predbat):
    """A manual freeze charge override is already a freeze, so the switch must not change it"""
    print("**** test_manual_freeze_charge_unaffected_by_charge_freeze_only ****")
    setup(my_predbat, set_charge_freeze_only=True)
    my_predbat.manual_freeze_charge_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_charge_limit("test_manual_freeze_charge_unaffected_by_charge_freeze_only", my_predbat, my_predbat.reserve)


def test_manual_demand_unaffected_by_charge_freeze_only(my_predbat):
    """A manual demand override disables the charge window entirely and must stay that way"""
    print("**** test_manual_demand_unaffected_by_charge_freeze_only ****")
    setup(my_predbat, set_charge_freeze_only=True)
    my_predbat.manual_demand_times = [720]

    my_predbat.optimise_charge_windows_manual()

    return check_charge_limit("test_manual_demand_unaffected_by_charge_freeze_only", my_predbat, 0)

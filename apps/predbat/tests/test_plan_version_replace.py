# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests that a plan made by another Predbat version is always replaced on the next recompute.

Plan selection keeps the incumbent unless the new plan beats it by metric_min_improvement_plan, and it scores
both on their pre-clip snapshots. An incumbent restored after an upgrade was clipped and pruned by the old
code, but its snapshot can tie with the new plan's, so without this rule the old code's decisions stay in
force until something else moves the metric.
"""
from tests.test_infra import reset_inverter
from tests.test_random_scenarios import load_scenarios, run_scenario


def plant_incumbent(my_predbat, preclip, made_by_version):
    """Make the plan in force a sentinel with no charge windows whose pre-clip snapshot ties with a fresh plan.

    On a tie the incumbent's executed plan is kept, so the sentinel survives the recompute exactly when the
    incumbent is kept.
    """
    my_predbat.plan_valid = True
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.plan_preclip = (list(preclip[0]), [dict(w) for w in preclip[1]], [dict(w) for w in preclip[2]], list(preclip[3]))
    my_predbat.plan_made_by_version = made_by_version


def run_plan_version_replace_tests(my_predbat):
    """Run the plan version replacement tests. Returns True on failure."""
    print("**** Running plan version replacement tests ****")
    failed = False

    # read_debug_yaml reconfigures the instance wholesale, so work on a throwaway one rather than leaving the
    # shared fixture holding this debug case for every test that runs after
    from unit_test import create_predbat

    my_predbat = create_predbat()

    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml("cases/predbat_debug_agile1.yaml")
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()

    scenario = [s for s in load_scenarios("cases/random_scenarios.yaml") if s["id"] == 10][0]
    run_scenario(my_predbat, scenario)
    preclip = my_predbat.plan_preclip
    if preclip is None or not my_predbat.charge_window_best:
        print("ERROR: the first recompute should leave a pre-clip snapshot and charge windows to compare against")
        return True

    lines = []
    real_log = my_predbat.log
    my_predbat.log = lambda message, *args, **kwargs: (lines.append(str(message)), real_log(message, *args, **kwargs))[1]
    try:
        # Control: an incumbent from this version that ties is kept - so the sentinel survives, which is what
        # gives the version case below its teeth
        plant_incumbent(my_predbat, preclip, my_predbat.running_version)
        my_predbat.calculate_plan(recompute=True)
        if my_predbat.charge_window_best:
            print("ERROR: control - a tying incumbent from this version should be kept, got charge windows {}".format(my_predbat.charge_window_best))
            failed = True
        if not any(line.startswith("Previous plan best metric") for line in lines):
            print("ERROR: control - an incumbent from this version should be compared on its metric")
            failed = True

        # An incumbent from another version is replaced without being compared, even though it ties
        for label, version in (("another version", "v0.0.0 (older build)"), ("an unknown version", None)):
            lines.clear()
            plant_incumbent(my_predbat, preclip, version)
            my_predbat.calculate_plan(recompute=True)
            if not my_predbat.charge_window_best:
                print("ERROR: an incumbent made by {} should be replaced by the new plan, but it was kept".format(label))
                failed = True
            if any(line.startswith("Previous plan best metric") for line in lines):
                print("ERROR: an incumbent made by {} should be replaced unconditionally, not compared".format(label))
                failed = True
            if my_predbat.plan_made_by_version != my_predbat.running_version:
                print("ERROR: the new plan should be marked as made by {}, got {}".format(my_predbat.running_version, my_predbat.plan_made_by_version))
                failed = True
    finally:
        my_predbat.log = real_log

    if not failed:
        print("**** Plan version replacement tests passed ****")
    return failed

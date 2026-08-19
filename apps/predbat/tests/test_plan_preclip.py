# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests that plan selection scores the plan as optimised rather than as clipped.

clip_export_slots and clip_charge_slots set the percentage shown on the plan and sent to the inverter. They
are a no-op in the expected case but move the metric through the PV10 branch, so calculate_plan keeps a
snapshot of the plan taken before clipping and compares on that instead - see plan_scoring_pair().
"""
from tests.test_infra import reset_inverter
from tests.test_random_scenarios import load_scenarios, run_scenario


def run_plan_preclip_tests(my_predbat):
    """Run the pre-clip plan selection tests. Returns True on failure."""
    print("**** Running plan pre-clip selection tests ****")
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

    # Reuse a benchmark scenario rather than generating one: these are known to plan in a couple of seconds
    # Scenario 10 is one where clipping measurably taxes the metric, so the comparison below has teeth: on an
    # untaxed scenario pre-clip and clipped score the same and the assertion would pass either way
    scenario = [s for s in load_scenarios("cases/random_scenarios.yaml") if s["id"] == 10][0]

    # First plan establishes an incumbent; the second recompute is the one that runs the comparison
    run_scenario(my_predbat, scenario)
    if my_predbat.plan_preclip is None:
        print("ERROR: plan_preclip was not captured by the first recompute")
        return True

    run_scenario(my_predbat, scenario)

    if my_predbat.plan_preclip is None:
        print("ERROR: plan_preclip was not carried through the second recompute")
        return True
    if len(my_predbat.plan_preclip) != 4:
        print("ERROR: plan_preclip should be a four part plan, got {}".format(len(my_predbat.plan_preclip)))
        return True

    # Clipping never improves a plan, so the snapshot selection scores must be no worse than the plan that
    # actually executes. Taking the snapshot after clipping instead would make these equal at best and hand
    # the taxed metric to should_replace_plan.
    preclip = my_predbat.plan_preclip
    metric_preclip = my_predbat.run_prediction_metric(preclip[0], preclip[1], preclip[2], preclip[3], end_record=my_predbat.end_record)[0]
    metric_final = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[0]

    if metric_final - metric_preclip < 0.01:
        print("ERROR: clipping did not tax this scenario ({} vs {}), so the comparison proves nothing".format(metric_preclip, metric_final))
        failed = True
    elif metric_preclip > metric_final + 0.0001:
        print("ERROR: pre-clip plan scored {} which is worse than the clipped plan {} - snapshot is not pre-clip".format(metric_preclip, metric_final))
        failed = True
    else:
        print("pre-clip metric {} vs clipped {} (clipping tax {})".format(round(metric_preclip, 4), round(metric_final, 4), round(metric_final - metric_preclip, 4)))

    if not failed:
        print("**** Plan pre-clip selection tests passed ****")
    return failed

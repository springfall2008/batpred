# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for resolve_multi_inverter_status() (#4446) - the multi-inverter headline status
resolver that replaced execute_plan()'s previous "whichever inverter is processed last wins"
overwrite, which silently hid genuine cross-inverter disagreement (e.g. real cross-charging).
"""

from execute import resolve_multi_inverter_status


def test_multi_inverter_status(my_predbat):
    """Verify the aggregation rule: unchanged when no inverter reached a core state, the shared
    state when all agree, the most-active same-side state when they disagree within one side, and
    'Cross-charging' when they disagree across the charge/export divide.
    """
    failed = False
    print("**** Testing resolve_multi_inverter_status ****")

    print("Test: no inverter reached a core charge/export state - falls through to current_status unchanged")
    for current_status in ["Demand", "Demand (Holiday)", "Read-Only", "Read-Only (Axle)", "Calibration", "Hold for car", "Hold for iBoost"]:
        result = resolve_multi_inverter_status({}, current_status)
        if result != current_status:
            print("  ERROR: expected {!r} unchanged with an empty status_per_inverter, got {!r}".format(current_status, result))
            failed = True

    print("Test: a single inverter's own state passes straight through")
    for state in ["Charging", "Freeze charging", "Hold charging", "Exporting", "Freeze exporting", "Hold exporting"]:
        result = resolve_multi_inverter_status({0: state}, "Demand")
        if result != state:
            print("  ERROR: expected single-inverter state {!r}, got {!r}".format(state, result))
            failed = True

    print("Test: all inverters agree on the same state - returns that state regardless of current_status")
    result = resolve_multi_inverter_status({0: "Charging", 1: "Charging", 2: "Charging"}, "Demand")
    if result != "Charging":
        print("  ERROR: expected 'Charging' when all 3 inverters agree, got {!r}".format(result))
        failed = True

    print("Test: charge-side sub-states disagree - most active (Charging) wins over Freeze/Hold charging")
    result = resolve_multi_inverter_status({0: "Hold charging", 1: "Charging", 2: "Freeze charging"}, "Demand")
    if result != "Charging":
        print("  ERROR: expected 'Charging' to win charge-side precedence, got {!r}".format(result))
        failed = True

    print("Test: charge-side sub-states disagree without Charging present - Freeze charging beats Hold charging")
    result = resolve_multi_inverter_status({0: "Hold charging", 1: "Freeze charging"}, "Demand")
    if result != "Freeze charging":
        print("  ERROR: expected 'Freeze charging' to beat 'Hold charging', got {!r}".format(result))
        failed = True

    print("Test: export-side sub-states disagree - most active (Exporting) wins over Freeze/Hold exporting")
    result = resolve_multi_inverter_status({0: "Hold exporting", 1: "Exporting", 2: "Freeze exporting"}, "Demand")
    if result != "Exporting":
        print("  ERROR: expected 'Exporting' to win export-side precedence, got {!r}".format(result))
        failed = True

    print("Test: export-side sub-states disagree without Exporting present - Freeze exporting beats Hold exporting")
    result = resolve_multi_inverter_status({0: "Hold exporting", 1: "Freeze exporting"}, "Demand")
    if result != "Freeze exporting":
        print("  ERROR: expected 'Freeze exporting' to beat 'Hold exporting', got {!r}".format(result))
        failed = True

    print("Test: genuine cross-charging - one inverter charging, another exporting, both at their most active sub-state")
    result = resolve_multi_inverter_status({0: "Charging", 1: "Exporting"}, "Demand")
    if result != "Cross-charging":
        print("  ERROR: expected 'Cross-charging' for Charging+Exporting, got {!r}".format(result))
        failed = True

    print("Test: cross-charging detected even from the mildest sub-states on each side (Hold charging + Hold exporting)")
    result = resolve_multi_inverter_status({0: "Hold charging", 1: "Hold exporting"}, "Demand")
    if result != "Cross-charging":
        print("  ERROR: expected 'Cross-charging' for Hold charging+Hold exporting, got {!r}".format(result))
        failed = True

    print("Test: cross-charging detected across a mix of sub-states on each side, regardless of precedence order")
    result = resolve_multi_inverter_status({0: "Freeze charging", 1: "Hold charging", 2: "Exporting"}, "Demand")
    if result != "Cross-charging":
        print("  ERROR: expected 'Cross-charging' when any charge-side and any export-side state coexist, got {!r}".format(result))
        failed = True

    print("Test: a Read-Only/Calibration-style current_status is preserved even with an empty dict, not clobbered")
    result = resolve_multi_inverter_status({}, "Calibration")
    if result != "Calibration":
        print("  ERROR: expected 'Calibration' preserved with no per-inverter core states recorded, got {!r}".format(result))
        failed = True

    return failed

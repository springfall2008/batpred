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

from execute import build_status_extra, resolve_multi_inverter_status


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

    print("Test: Calibration is preserved even when an earlier-processed inverter left a stale core state behind")
    result = resolve_multi_inverter_status({0: "Charging"}, "Calibration")
    if result != "Calibration":
        print("  ERROR: expected 'Calibration' to override a stale core state left by an earlier inverter, got {!r}".format(result))
        failed = True

    failed |= test_build_status_extra()

    return failed


def test_build_status_extra():
    """Verify the per-inverter status detail text only labels segments when inverters disagree.

    #4466 began labelling every segment with that inverter's own state so a mixed fleet could be
    read. The label is redundant when the whole fleet agrees, because the headline status the detail
    is appended to already says it - a single-inverter export showed
    "Exporting target Exporting 19%-5%" in v8.48.4.
    """
    failed = False
    print("**** Testing build_status_extra ****")

    print("Test: no inverter recorded a detail segment - empty text")
    result = build_status_extra([])
    if result != "":
        print("  ERROR: expected '' with no segments, got {!r}".format(result))
        failed = True

    print("Test: single inverter exporting - no repeated label (regression, GH v8.48.4)")
    result = build_status_extra([(0, "target", "Exporting", "19%-5%")])
    if result != " target 19%-5%":
        print("  ERROR: expected ' target 19%-5%', got {!r}".format(result))
        failed = True

    print("Test: single inverter freeze exporting - uses its own lead word, still unlabelled")
    result = build_status_extra([(0, "current SoC", "Freeze exporting", "19%")])
    if result != " current SoC 19%":
        print("  ERROR: expected ' current SoC 19%', got {!r}".format(result))
        failed = True

    print("Test: multiple inverters all in the same state - joined, still unlabelled")
    result = build_status_extra([(0, "target", "Charging", "50%-80%"), (1, "target", "Charging", "60%-80%")])
    if result != " target 50%-80% / 60%-80%":
        print("  ERROR: expected ' target 50%-80% / 60%-80%', got {!r}".format(result))
        failed = True

    print("Test: inverters in different states - every segment labelled so the fleet can be read")
    result = build_status_extra([(0, "target", "Charging", "90%-100.0%"), (1, "target", "Hold charging", "100%-100.0%")])
    if result != " target Charging 90%-100.0% / Hold charging 100%-100.0%":
        print("  ERROR: expected labelled mixed-fleet text, got {!r}".format(result))
        failed = True

    print("Test: cross-charging (charge and export at once) - labelled, as the headline says neither")
    result = build_status_extra([(0, "target", "Charging", "50%-80%"), (1, "target", "Exporting", "50%-5%")])
    if result != " target Charging 50%-80% / Exporting 50%-5%":
        print("  ERROR: expected labelled cross-charging text, got {!r}".format(result))
        failed = True

    print("Test: only a non-zero inverter recorded a segment - separator used, no lead word")
    result = build_status_extra([(1, "target", "Exporting", "19%-5%")])
    if result != " / 19%-5%":
        print("  ERROR: expected ' / 19%-5%', got {!r}".format(result))
        failed = True

    return failed

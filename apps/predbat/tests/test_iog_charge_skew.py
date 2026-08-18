# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy

from tests.test_infra import reset_rates, reset_inverter, update_rates_import
from prediction import Prediction

# Attributes these tests mutate on the shared my_predbat instance. They are snapshotted on
# entry and restored on exit so this test does not pollute state for later tests in the suite.
_SNAPSHOT_ATTRS = [
    "io_adjusted",
    "minutes_now",
    "calculate_best_charge",
    "calculate_best_export",
    "set_charge_freeze",
    "carbon_enable",
    "inverter_loss",
    "battery_loss",
    "battery_loss_discharge",
    "metric_battery_cycle",
    "metric_self_sufficiency",
    "soc_max",
    "soc_kw",
    "reserve",
    "best_soc_keep",
    "best_soc_keep_weight",
    "inverter_hybrid",
    "battery_rate_max_charge",
    "battery_rate_max_charge_dc",
    "battery_rate_max_discharge",
    "charge_rate_now",
    "discharge_rate_now",
    "inverter_limit",
    "export_limit",
    "prediction",
    "inverters",
    "load_minutes_step",
    "load_minutes_step10",
    "pv_forecast_minute_step",
    "pv_forecast_minute10_step",
    "debug_enable",
    "prediction_kernel_enable",
    "rate_min_forward",
    "calculate_second_pass",
    "rate_import",
    "rate_export",
    "rate_export_min",
    "rate_min",
    "rate_max",
    "rate_min_base",
    "rate_max_base",
    "combine_charge_slots",
    "charge_limit_best",
    "export_limits_best",
    "charge_window_best",
    "export_window_best",
    "num_inverters",
    "current_charge_limit",
    "charge_window",
    "export_window",
    "export_limits",
]
# Objects with back-references to my_predbat must not be deep-copied (would clone the whole instance)
_SNAPSHOT_BY_REFERENCE = {"prediction", "inverters"}


def _snapshot_state(my_predbat):
    """Snapshot the attributes these tests mutate so they can be restored afterwards."""
    saved = {}
    for attr in _SNAPSHOT_ATTRS:
        if not hasattr(my_predbat, attr):
            continue
        value = getattr(my_predbat, attr)
        saved[attr] = value if attr in _SNAPSHOT_BY_REFERENCE else copy.deepcopy(value)
    return saved


def _restore_state(my_predbat, saved):
    """Restore attributes captured by _snapshot_state."""
    for attr, value in saved.items():
        setattr(my_predbat, attr, value)


def _check(failed, name, got, expected, tol=0.001):
    """Compare a numeric result against expectation with a tolerance."""
    if abs(got - expected) > tol:
        print("  ERROR: {}: expected {} but got {}".format(name, expected, got))
        return failed + 1
    print("  OK: {} = {}".format(name, got))
    return failed


def run_io_run_starts_tests(my_predbat):
    """
    Unit test _io_run_starts: maps each io_adjusted window start to the start of its
    contiguous IOG run. Firm windows break a run; windows may arrive unsorted; a time
    gap (missing window) also breaks a run.
    """
    print("\n**** _io_run_starts unit tests ****")
    failed = 0

    # Windows: three contiguous IOG (0-90), one firm (90-120), two contiguous IOG (120-180)
    windows = [
        {"start": 0, "end": 30, "average": 7.0},
        {"start": 30, "end": 60, "average": 7.0},
        {"start": 60, "end": 90, "average": 7.0},
        {"start": 90, "end": 120, "average": 7.0},  # firm
        {"start": 120, "end": 150, "average": 7.0},
        {"start": 150, "end": 180, "average": 7.0},
    ]
    my_predbat.io_adjusted = {0: True, 30: True, 60: True, 120: True, 150: True}

    run_starts = my_predbat._io_run_starts(windows)
    expected = {0: 0, 30: 0, 60: 0, 120: 120, 150: 120}
    if run_starts != expected:
        print("  ERROR: contiguous runs: expected {} but got {}".format(expected, run_starts))
        failed += 1
    else:
        print("  OK: contiguous runs mapped correctly")
    if 90 in run_starts:
        print("  ERROR: firm window (90) should not appear in run map, got {}".format(run_starts))
        failed += 1
    else:
        print("  OK: firm window excluded")

    # Unsorted input must give the same result
    shuffled = [windows[4], windows[0], windows[3], windows[5], windows[1], windows[2]]
    run_starts_shuffled = my_predbat._io_run_starts(shuffled)
    if run_starts_shuffled != expected:
        print("  ERROR: unsorted input: expected {} but got {}".format(expected, run_starts_shuffled))
        failed += 1
    else:
        print("  OK: unsorted input handled")

    # A time gap (missing 30-60 window) breaks the run
    gapped = [
        {"start": 0, "end": 30, "average": 7.0},
        {"start": 60, "end": 90, "average": 7.0},
    ]
    my_predbat.io_adjusted = {0: True, 60: True}
    run_starts_gap = my_predbat._io_run_starts(gapped)
    if run_starts_gap != {0: 0, 60: 60}:
        print("  ERROR: gap should break run: expected {{0: 0, 60: 60}} but got {}".format(run_starts_gap))
        failed += 1
    else:
        print("  OK: time gap breaks run")

    return failed


def run_io_rate_adjustment_tests(my_predbat):
    """
    Unit test _io_rate_adjustment: the signed per-hour gradient across an IOG run.
    Constants: SLOPE=1p/hr, PIVOT=1.5h, MAX_DISCOUNT=3p, MAX_PENALTY=10p, HORIZON=3h.
    Front of run -> discount (negative), back -> penalty (positive), firm level at PIVOT.
    Discount only applies to imminent slots (start within HORIZON of now).
    """
    print("\n**** _io_rate_adjustment unit tests ****")
    failed = 0
    saved_now = my_predbat.minutes_now
    my_predbat.minutes_now = 0

    # (window_start, run_start, expected_adjustment)
    cases = [
        ("front imminent", 0, 0, -1.5),  # hours_in 0 -> (0-1.5)*1 = -1.5
        ("pivot imminent", 90, 0, 0.0),  # hours_in 1.5 -> 0
        ("just past pivot", 120, 0, 0.5),  # hours_in 2 -> +0.5
        ("back within horizon", 150, 0, 1.0),  # hours_in 2.5 -> +1.0 (start 2.5h <= 3h)
        ("deep back capped", 720, 0, 10.0),  # hours_in 12 -> clamp to +10
        ("front but distant -> no discount", 300, 300, 0.0),  # hours_in 0 but 5h ahead -> gated to 0
        ("distant back keeps penalty", 480, 300, 1.5),  # run_start 5h, window 8h: hours_in 3 -> +1.5, ahead 8h keeps penalty
        ("discount capped", 0, 720, -3.0),  # hours_in -12 -> clamp to -3 (imminent)
    ]
    for name, ws, rs, exp in cases:
        got = my_predbat._io_rate_adjustment(ws, rs)
        failed = _check(failed, name, got, exp)

    my_predbat.minutes_now = saved_now
    return failed


def run_iog_skew_scenario(
    name,
    my_predbat,
    charge_window_best,
    io_adjusted_starts,
    battery_size=5.0,
    battery_soc=0.0,
    charge_rate_kw=5.0,
    load_amount=0.1,
    rate_import_base=30.0,
    rate_export=1.0,
):
    """
    Run a single IOG scenario through optimise_all_windows and return charge_limit_best.

    charge_window_best: list of {"start","end","average"} charge windows (the low-rate period).
    io_adjusted_starts: iterable of window["start"] minutes to flag as Octopus Intelligent.
    """
    print("\n  -- scenario: {} --".format(name))
    end_record = my_predbat.forecast_minutes

    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = False
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_keep_weight = 0.5
    my_predbat.reserve = 0.0
    my_predbat.set_charge_freeze = True
    my_predbat.calculate_second_pass = False
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = False
    my_predbat.rate_min_forward = {}

    my_predbat.battery_rate_max_charge = charge_rate_kw / 60.0
    my_predbat.battery_rate_max_charge_dc = charge_rate_kw / 60.0
    my_predbat.charge_rate_now = charge_rate_kw / 60.0
    my_predbat.inverter_limit = charge_rate_kw / 60.0
    my_predbat.export_limit = charge_rate_kw / 60.0

    reset_rates(my_predbat, rate_import_base, rate_export)
    update_rates_import(my_predbat, charge_window_best)

    my_predbat.io_adjusted = {}
    for start in io_adjusted_starts:
        my_predbat.io_adjusted[start] = True

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    export_window_best = []
    charge_limit_best = [0 for _ in range(len(charge_window_best))]
    export_limits_best = []

    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record
    )
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    my_predbat.optimise_all_windows(metric, metric_keep)
    charge_limit_best = my_predbat.charge_limit_best

    charged = [n for n in range(len(charge_limit_best)) if charge_limit_best[n] > 0.5]
    print("     charged slots: {}".format(charged))
    return charge_limit_best, charged


def _charge_averages_by_index(my_predbat, charge_windows):
    """
    Call sort_window_by_price_combined and return a dict of charge-window index -> effective
    (banded) average price, so tests can inspect the gradient the sort applied per window.
    """
    window_sort, window_links, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_windows, [])
    by_index = {}
    for key, link in window_links.items():
        if link["type"] == "c":
            by_index[link["id"]] = link["average"]
    return by_index


def run_iog_sort_wiring_tests(my_predbat):
    """
    Deterministic check that the gradient is wired into sort_window_by_price_combined:
    the earliest IOG slots must be priced BELOW the flat base rate (a discount that the old
    penalty-only code could never produce), and below the firm slots, while a back-of-run
    IOG slot is priced above firm.
    """
    print("\n**** IOG sort-wiring tests ****")
    failed = 0

    saved_now = my_predbat.minutes_now
    my_predbat.minutes_now = 0
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = False
    my_predbat.set_charge_freeze = False
    my_predbat.carbon_enable = False
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.metric_battery_cycle = 0.0
    my_predbat.metric_self_sufficiency = 0.0

    # Minimal prediction object (sort_window_by_price_combined reads pv_forecast_minute_step)
    zero_step = {m: 0.0 for m in range(0, my_predbat.forecast_minutes, 5)}
    my_predbat.prediction = Prediction(my_predbat, zero_step, zero_step, zero_step, zero_step)

    base_rate = 7.0
    # 8 IOG slots (0-7, a 4h imminent run) then 8 firm slots (8-15), all at the flat base rate
    charge_windows = []
    for n in range(16):
        start = 30 * n
        charge_windows.append({"start": start, "end": start + 30, "average": base_rate})
    my_predbat.io_adjusted = {30 * n: True for n in range(8)}

    avg = _charge_averages_by_index(my_predbat, charge_windows)
    print("  effective averages by index: {}".format({k: avg[k] for k in sorted(avg)}))

    firm = avg[8]  # a firm slot, should be unchanged at the base rate
    failed = _check(failed, "firm slot unchanged", firm, base_rate, tol=0.11)

    # Front IOG slot must be discounted below the base/firm rate (impossible under old penalty-only code)
    if avg[0] < base_rate - 0.2:
        print("  OK: front IOG slot discounted below base ({} < {})".format(avg[0], base_rate))
    else:
        print("  ERROR: front IOG slot not discounted: avg[0]={} (base {})".format(avg[0], base_rate))
        failed += 1

    if avg[0] < firm:
        print("  OK: front IOG cheaper than firm ({} < {})".format(avg[0], firm))
    else:
        print("  ERROR: front IOG not cheaper than firm: {} vs {}".format(avg[0], firm))
        failed += 1

    # Back-of-run IOG slot must be penalised above firm
    if avg[7] > firm:
        print("  OK: back IOG penalised above firm ({} > {})".format(avg[7], firm))
    else:
        print("  ERROR: back IOG not penalised above firm: {} vs {}".format(avg[7], firm))
        failed += 1

    my_predbat.minutes_now = saved_now
    return failed


def run_iog_integration_tests(my_predbat):
    """
    Plan-level sanity check that an imminent IOG run at the front of the low-rate period
    keeps charging within the IOG run (does not leak into the later firm slots).
    """
    print("\n**** IOG skew integration tests ****")
    failed = 0
    reset_inverter(my_predbat)
    minutes_now = my_predbat.minutes_now
    low_rate = 7.0

    charge_window_best = []
    for n in range(24):
        start = minutes_now + 30 * n
        charge_window_best.append({"start": start, "end": start + 30, "average": low_rate})

    # First 8 slots (4h) are an imminent IOG run; the rest are firm
    iog_front_starts = [charge_window_best[n]["start"] for n in range(8)]

    _, charged_iog_front = run_iog_skew_scenario("iog_front_run", my_predbat, [dict(w) for w in charge_window_best], io_adjusted_starts=iog_front_starts)

    if not charged_iog_front:
        print("  ERROR: IOG-front scenario charged nothing")
        failed += 1
    elif min(charged_iog_front) > 1:
        print("  ERROR: expected charge to start in the front IOG slots (<=1) but earliest was {}".format(min(charged_iog_front)))
        failed += 1
    else:
        print("  OK: charge starts in the front IOG slots (earliest {})".format(min(charged_iog_front)))

    return failed


def run_iog_charge_skew_tests(my_predbat):
    """
    Tests for the Octopus Intelligent (IOG) earlier-charge skew gradient.
    """
    failed = 0
    saved = _snapshot_state(my_predbat)
    try:
        failed += run_io_run_starts_tests(my_predbat)
        failed += run_io_rate_adjustment_tests(my_predbat)
        failed += run_iog_sort_wiring_tests(my_predbat)
        failed += run_iog_integration_tests(my_predbat)
    finally:
        _restore_state(my_predbat, saved)

    if failed:
        print("\n**** iog_charge_skew tests: FAILED ({} failures) ****".format(failed))
    else:
        print("\n**** iog_charge_skew tests: PASSED ****")
    return failed

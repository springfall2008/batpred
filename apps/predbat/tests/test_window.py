# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on
import copy
import random

import prediction_kernel
from utils import clone_windows, remove_intersecting_windows
from tests.test_infra import reset_rates, reset_inverter
from prediction import Prediction


def run_window_sort_test(name, my_predbat, charge_window_best, export_window_best, expected=[], inverter_loss=1.0, metric_battery_cycle=0.0, battery_loss=1.0, battery_loss_discharge=1.0):
    failed = False
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.inverter_loss = inverter_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.battery_loss = battery_loss
    my_predbat.battery_loss_discharge = battery_loss_discharge
    my_predbat.prediction.pv_forecast_minute_step = {}

    # Pretend we have PV all the time to allow discharge freeze to appear in the sort
    for minute in range(end_record + my_predbat.minutes_now):
        my_predbat.prediction.pv_forecast_minute_step[minute] = 1.0

    print("Starting window sort test {}".format(name))

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, export_window_best), 1)

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window_best[:record_charge_windows], export_window_best[:record_export_windows])

    results = []
    for price_key in price_set:
        links = price_links[price_key]
        for key in links:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            price = window_index[key]["average"]
            results.append((str(typ) + "_" + str(window_n) + "_" + str(price)))

    if len(expected) != len(results):
        print("ERROR: Expected {} results but got {}".format(len(expected), len(results)))
        failed = True
    else:
        for n in range(len(expected)):
            if expected[n] != results[n]:
                print("ERROR: Expected item {} is {} but got {}".format(n, expected[n], results[n]))
                failed = True
    if failed:
        print("Inputs: {} {}".format(charge_window_best, export_window_best))
        print("Results: {}".format(results))

    return failed


def reference_remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best):
    """Deliberately naive reference: clip each charge window against every enabled export window.

    Kept as a plain O(charge x export) scan with no early exits so it can be compared against the
    optimised implementation on randomised inputs - if the two ever disagree, the optimisation
    changed behaviour rather than just skipping work.
    """
    windows = []
    limits = []
    for limit, window in zip(charge_limit_best, charge_window_best):
        segments = [(window["start"], window["end"])]
        clipped = False
        if limit > 0.0:
            for dlimit, dwindow in zip(export_limit_best, export_window_best):
                if dlimit >= 100.0:
                    continue
                dstart, dend = dwindow["start"], dwindow["end"]
                new_segments = []
                for start, end in segments:
                    if dstart >= end or dend < start:
                        new_segments.append((start, end))
                        continue
                    pieces = []
                    if dstart > start:
                        pieces.append((start, min(dstart, end)))
                    if dend < end:
                        pieces.append((max(dend, start), end))
                    # Windows that merely touch at a boundary overlap arithmetically but remove
                    # nothing, and must not count as clipped or the 5 minute rule would discard them
                    if pieces != [(start, end)]:
                        clipped = True
                    new_segments.extend(pieces)
                segments = new_segments
        for start, end in segments:
            # A window that was never clipped passes through whatever its length; the 5 minute
            # minimum only discards the remnants clipping itself created
            if not clipped or (end - start) >= 5:
                windows.append({"start": start, "end": end, "average": window["average"]})
                limits.append(limit)
    return limits, windows


def _intersect_case(name, charge_limit_best, charge_window_best, export_limit_best, export_window_best, expect_windows, expect_limits):
    """Run one remove_intersecting_windows case and compare against expected windows/limits"""
    failed = False
    new_limit_best, new_window_best = remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best)
    got = [(w["start"], w["end"]) for w in new_window_best]
    if got != expect_windows:
        print("ERROR: {} expected windows {} but got {}".format(name, expect_windows, got))
        failed = True
    if list(new_limit_best) != list(expect_limits):
        print("ERROR: {} expected limits {} but got {}".format(name, expect_limits, list(new_limit_best)))
        failed = True
    return failed


def run_intersect_window_tests(my_predbat):
    """Characterisation tests for remove_intersecting_windows.

    This is a hot path - it runs on every simulation - so it needs enough coverage to be optimised
    against. The randomised equivalence check at the end is the real guard: it compares the shipped
    implementation against a naive reference over many random window layouts.
    """
    print("**** Running intersect window tests ****")
    failed = False
    now = my_predbat.minutes_now

    # A fully-covered charge window disappears
    failed |= _intersect_case(
        "fully covered",
        [4],
        [{"start": now, "end": now + 30, "average": 10}],
        [2],
        [{"start": now, "end": now + 30, "average": 10}],
        [],
        [],
    )

    # A disabled charge window (limit 0) is passed through untouched even when an export overlaps it
    failed |= _intersect_case(
        "disabled charge untouched",
        [0],
        [{"start": now, "end": now + 30, "average": 10}],
        [2],
        [{"start": now, "end": now + 30, "average": 10}],
        [(now, now + 30)],
        [0],
    )

    # A disabled export window (limit 100) never clips
    failed |= _intersect_case(
        "disabled export does not clip",
        [4],
        [{"start": now, "end": now + 30, "average": 10}],
        [100.0],
        [{"start": now, "end": now + 30, "average": 10}],
        [(now, now + 30)],
        [4],
    )

    # Export overlapping the start moves the charge window start forward
    failed |= _intersect_case(
        "clip start",
        [4],
        [{"start": now, "end": now + 60, "average": 10}],
        [2],
        [{"start": now, "end": now + 30, "average": 10}],
        [(now + 30, now + 60)],
        [4],
    )

    # Export overlapping the end pulls the charge window end back
    failed |= _intersect_case(
        "clip end",
        [4],
        [{"start": now, "end": now + 60, "average": 10}],
        [2],
        [{"start": now + 30, "end": now + 60, "average": 10}],
        [(now, now + 30)],
        [4],
    )

    # Export in the middle splits the charge window into two
    failed |= _intersect_case(
        "split in two",
        [4],
        [{"start": now, "end": now + 90, "average": 10}],
        [2],
        [{"start": now + 30, "end": now + 60, "average": 10}],
        [(now, now + 30), (now + 60, now + 90)],
        [4, 4],
    )

    # Two exports inside one charge window produce three segments - this is the path that used to
    # need a second pass over the whole window list
    failed |= _intersect_case(
        "two splits in one window",
        [4],
        [{"start": now, "end": now + 120, "average": 10}],
        [2, 2],
        [{"start": now + 20, "end": now + 40, "average": 10}, {"start": now + 60, "end": now + 80, "average": 10}],
        [(now, now + 20), (now + 40, now + 60), (now + 80, now + 120)],
        [4, 4, 4],
    )

    # Overlapping export windows collapse into one clipped region
    failed |= _intersect_case(
        "overlapping exports",
        [4],
        [{"start": now, "end": now + 120, "average": 10}],
        [2, 2],
        [{"start": now + 20, "end": now + 60, "average": 10}, {"start": now + 40, "end": now + 80, "average": 10}],
        [(now, now + 20), (now + 80, now + 120)],
        [4, 4],
    )

    # A head segment shorter than 5 minutes is dropped rather than emitted
    failed |= _intersect_case(
        "short head segment dropped",
        [4],
        [{"start": now, "end": now + 60, "average": 10}],
        [2],
        [{"start": now + 2, "end": now + 30, "average": 10}],
        [(now + 30, now + 60)],
        [4],
    )

    # A clipped remainder shorter than 5 minutes is dropped
    failed |= _intersect_case(
        "short remainder dropped",
        [4],
        [{"start": now, "end": now + 32, "average": 10}],
        [2],
        [{"start": now, "end": now + 30, "average": 10}],
        [],
        [],
    )

    # An unclipped window shorter than 5 minutes is still kept
    failed |= _intersect_case(
        "short unclipped window kept",
        [4],
        [{"start": now, "end": now + 2, "average": 10}],
        [2],
        [{"start": now + 60, "end": now + 90, "average": 10}],
        [(now, now + 2)],
        [4],
    )

    # Windows that merely touch at the boundary do not clip
    failed |= _intersect_case(
        "touching boundaries do not clip",
        [4],
        [{"start": now + 30, "end": now + 60, "average": 10}],
        [2],
        [{"start": now, "end": now + 30, "average": 10}],
        [(now + 30, now + 60)],
        [4],
    )

    # Export windows presented out of order must give the same answer as sorted ones
    failed |= _intersect_case(
        "unsorted export windows",
        [4],
        [{"start": now, "end": now + 120, "average": 10}],
        [2, 2],
        [{"start": now + 60, "end": now + 80, "average": 10}, {"start": now + 20, "end": now + 40, "average": 10}],
        [(now, now + 20), (now + 40, now + 60), (now + 80, now + 120)],
        [4, 4, 4],
    )

    # Several charge windows, only some intersecting - the others must pass through untouched
    failed |= _intersect_case(
        "mixed windows",
        [4, 0, 8],
        [{"start": now, "end": now + 60, "average": 10}, {"start": now + 60, "end": now + 120, "average": 10}, {"start": now + 120, "end": now + 180, "average": 10}],
        [2],
        [{"start": now + 30, "end": now + 90, "average": 10}],
        [(now, now + 30), (now + 60, now + 120), (now + 120, now + 180)],
        [4, 0, 8],
    )

    # Randomised equivalence against the naive reference. Generates short (sub-5-minute) windows,
    # zero-length gaps and overlapping export windows, since those drive the segment-length rules and
    # the clipping order. Export windows are sorted, which is the invariant callers provide and which
    # the single-pass clipping relies on.
    rng = random.Random(1234)
    for case in range(1000):
        n_charge = rng.randint(0, 8)
        n_export = rng.randint(0, 8)
        charge_windows = []
        charge_limits = []
        minute = now
        for _ in range(n_charge):
            length = rng.choice([1, 2, 5, 5, 10, 30, 60, 90, 120])
            charge_windows.append({"start": minute, "end": minute + length, "average": 10})
            charge_limits.append(rng.choice([0, 0, 0.0, 2.0, 4.0, 8.0]))
            minute += length + rng.choice([0, 0, 1, 5, 30])
        export_windows = []
        export_limits = []
        minute = now + rng.choice([0, 5, 10])
        for _ in range(n_export):
            length = rng.choice([1, 2, 5, 10, 30, 60])
            export_windows.append({"start": minute, "end": minute + length, "average": 10})
            export_limits.append(rng.choice([100.0, 100.0, 99.0, 0.0, 50.0, 4.0]))
            minute += length + rng.choice([-5, 0, 0, 5, 30])
        order = sorted(range(len(export_windows)), key=lambda i: export_windows[i]["start"])
        export_windows = [export_windows[i] for i in order]
        export_limits = [export_limits[i] for i in order]

        got_limits, got_windows = remove_intersecting_windows([x for x in charge_limits], [dict(w) for w in charge_windows], export_limits, export_windows)
        exp_limits, exp_windows = reference_remove_intersecting_windows(charge_limits, charge_windows, export_limits, export_windows)
        got_pairs = [(w["start"], w["end"], limit) for w, limit in zip(got_windows, got_limits)]
        exp_pairs = [(w["start"], w["end"], limit) for w, limit in zip(exp_windows, exp_limits)]
        if got_pairs != exp_pairs:
            print("ERROR: randomised case {} disagrees with reference".format(case))
            print("   charge {} limits {}".format(charge_windows, charge_limits))
            print("   export {} limits {}".format(export_windows, export_limits))
            print("   got      {}".format(got_pairs))
            print("   expected {}".format(exp_pairs))
            failed = True
            break

    if not failed:
        print("PASS")
    return failed


def run_window_sort_tests(my_predbat):
    import_rate = 10.0
    export_rate = 5.0
    reset_inverter(my_predbat)
    reset_rates(my_predbat, import_rate, export_rate)
    failed = False

    pv_amount = 0
    load_amount = 0
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    failed |= run_window_sort_test("none", my_predbat, [], [], expected=[])

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": import_rate}]
    failed |= run_window_sort_test("single_charge", my_predbat, charge_window_best, [], expected=["c_0_10.0", "cf_0_10.0"])
    failed |= run_window_sort_test("single_charge_loss", my_predbat, charge_window_best, [], expected=["c_0_20.0", "cf_0_10.0"], inverter_loss=0.5)

    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate}]
    failed |= run_window_sort_test("single_discharge", my_predbat, [], export_window_best, expected=["d_0_5.0", "df_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge", my_predbat, charge_window_best, export_window_best, expected=["c_0_10.0", "cf_0_10.0", "d_0_5.0", "df_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge_loss", my_predbat, charge_window_best, export_window_best, expected=["c_0_20.0", "cf_0_10.0", "df_0_5.0", "d_0_2.4"], inverter_loss=0.5)
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 50.0}]
    failed |= run_window_sort_test("single_charge_discharge_loss2", my_predbat, charge_window_best, export_window_best, expected=["df_0_50.0", "d_0_25.0", "c_0_20.0", "cf_0_10.0"], inverter_loss=0.5)
    failed |= run_window_sort_test("single_charge_discharge_loss3", my_predbat, charge_window_best, export_window_best, expected=["c_0_200.0", "df_0_50.0", "d_0_25.0", "cf_0_10.0"], inverter_loss=0.5, battery_loss=0.1)
    failed |= run_window_sort_test(
        "single_charge_discharge_loss4",
        my_predbat,
        charge_window_best,
        export_window_best,
        expected=["c_0_200.0", "df_0_50.0", "cf_0_10.0", "d_0_2.4"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )

    charge_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": import_rate * 2})
    failed |= run_window_sort_test(
        "single_charge_discharge2",
        my_predbat,
        charge_window_best,
        export_window_best,
        expected=["c_1_400.0", "c_0_200.0", "df_0_50.0", "cf_1_20.0", "cf_0_10.0", "d_0_2.4"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )
    export_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate * 3})
    failed |= run_window_sort_test("single_charge_discharge3", my_predbat, charge_window_best, export_window_best, expected=["d_0_50.0", "df_0_50.0", "c_1_20.0", "cf_1_20.0", "d_1_15.0", "df_1_15.0", "c_0_10.0", "cf_0_10.0"])
    failed |= run_window_sort_test("single_charge_discharge3_c1", my_predbat, charge_window_best, export_window_best, expected=["df_0_50.0", "d_0_49.0", "c_1_21.0", "cf_1_20.0", "df_1_15.0", "d_1_14.0", "c_0_11.0", "cf_0_10.0"], metric_battery_cycle=1.0)

    return failed


def run_clone_windows_tests(my_predbat):
    """Tests for clone_windows, the shallow-copy replacement for copy.deepcopy on window lists.

    clone_windows is only safe because window dicts hold primitives, so these pin both halves of
    that contract: the copy must equal a deepcopy, and mutating either side must not affect the
    other. The start-mutation case mirrors what Prediction.thread_run_prediction_export does to
    the window list it is handed, which is the reason the copy exists at all.
    """
    print("**** Running clone_windows tests ****")
    failed = False

    # Equivalence with deepcopy over the window shapes actually used in the planner
    cases = [
        ("empty", []),
        ("bounds only", [{"start": 0, "end": 30}]),
        ("with average", [{"start": 0, "end": 30, "average": 12.5}, {"start": 30, "end": 60, "average": 7.0}]),
        ("with target and sort keys", [{"start": 60, "end": 120, "average": 3.25, "target": 4.0, "id": 2, "key": "0100.2501"}]),
    ]
    for name, windows in cases:
        cloned = clone_windows(windows)
        if cloned != copy.deepcopy(windows):
            print("ERROR: clone_windows {} did not match deepcopy: {} vs {}".format(name, cloned, copy.deepcopy(windows)))
            failed = True
        if len(cloned) != len(windows):
            print("ERROR: clone_windows {} changed the list length {} -> {}".format(name, len(windows), len(cloned)))
            failed = True

    # The list itself must be a new object, and so must every dict in it
    original = [{"start": 0, "end": 30, "average": 5.0}, {"start": 30, "end": 60, "average": 6.0}]
    cloned = clone_windows(original)
    if cloned is original:
        print("ERROR: clone_windows returned the same list object")
        failed = True
    for index, window in enumerate(cloned):
        if window is original[index]:
            print("ERROR: clone_windows shared dict {} with the original".format(index))
            failed = True

    # Writing a start into the copy must not reach the original - this is exactly what
    # thread_run_prediction_export does to the window list optimise_export hands it
    cloned[0]["start"] = 15
    if original[0]["start"] != 0:
        print("ERROR: clone_windows leaked a start mutation back to the original: {}".format(original[0]["start"]))
        failed = True

    # And the reverse direction, since the planner also mutates the original between passes
    original[1]["target"] = 99.0
    if "target" in cloned[1]:
        print("ERROR: clone_windows leaked a new key from the original into the copy")
        failed = True

    return failed


def run_window_cache_tests(my_predbat):
    """Tests for the window bounds cache in prediction_kernel.

    The cache is only sound while every mutation of a window's start/end invalidates it. The last
    test here is the real guard: it replays a full plan with VALIDATE_WINDOW_CACHE on, which
    re-derives the bounds on every cache hit and raises on any stale entry. If someone later adds
    a bare window["start"] = ... on the planning path, that test fails rather than the plan
    silently simulating against the wrong window geometry.
    """
    print("**** Running window cache tests ****")
    failed = False

    # A window list that grows or shrinks in place keeps its id(), so the entry must also check the
    # length. Otherwise run_prediction_kernel passes n_charge = len(window_list) alongside the older,
    # shorter cached arrays and the C kernel reads past their end - a memory-safety failure rather
    # than merely a wrong plan, which is why this is guarded and not just documented.
    prediction_kernel.invalidate_window_cache()
    growing = [{"start": 0, "end": 30, "average": 5.0}]
    starts_before, ends_before = prediction_kernel.window_bound_arrays(growing)
    if len(starts_before) != 1:
        print("ERROR: expected a single-entry bounds array, got {}".format(len(starts_before)))
        failed = True
    growing.append({"start": 60, "end": 120, "average": 6.0})
    starts_after, ends_after = prediction_kernel.window_bound_arrays(growing)
    if len(starts_after) != len(growing) or len(ends_after) != len(growing):
        print("ERROR: bounds arrays are stale after the window list grew - len {} against {} windows".format(len(starts_after), len(growing)))
        failed = True
    elif list(starts_after) != [0, 60] or list(ends_after) != [30, 120]:
        print("ERROR: bounds arrays wrong after growth: {} / {}".format(list(starts_after), list(ends_after)))
        failed = True
    # The tuple used for the prediction cache key has to follow the same rule
    prediction_kernel.invalidate_window_cache()
    shrinking = [{"start": 0, "end": 30, "average": 5.0}, {"start": 60, "end": 120, "average": 6.0}]
    prediction_kernel.window_bound_tuple(shrinking)
    shrinking.pop()
    if prediction_kernel.window_bound_tuple(shrinking) != ((0, 30),):
        print("ERROR: window_bound_tuple is stale after the window list shrank: {}".format(prediction_kernel.window_bound_tuple(shrinking)))
        failed = True

    # A repeated lookup on an unchanged list returns the identical cached objects
    prediction_kernel.invalidate_window_cache()
    windows = [{"start": 0, "end": 30, "average": 5.0}, {"start": 60, "end": 120, "average": 6.0}]
    starts_a, ends_a = prediction_kernel.window_bound_arrays(windows)
    starts_b, ends_b = prediction_kernel.window_bound_arrays(windows)
    if starts_a is not starts_b or ends_a is not ends_b:
        print("ERROR: window_bound_arrays rebuilt the arrays for an unchanged list")
        failed = True
    if list(starts_a) != [0, 60] or list(ends_a) != [30, 120]:
        print("ERROR: window_bound_arrays gave {} / {}".format(list(starts_a), list(ends_a)))
        failed = True
    if prediction_kernel.window_bound_tuple(windows) != ((0, 30), (60, 120)):
        print("ERROR: window_bound_tuple gave {}".format(prediction_kernel.window_bound_tuple(windows)))
        failed = True

    # set_window_start must invalidate, so the next lookup reflects the new bounds
    prediction_kernel.set_window_start(windows[0], 15)
    if list(prediction_kernel.window_bound_arrays(windows)[0]) != [15, 60]:
        print("ERROR: set_window_start did not invalidate the array cache")
        failed = True
    if prediction_kernel.window_bound_tuple(windows) != ((15, 30), (60, 120)):
        print("ERROR: set_window_start did not invalidate the tuple cache")
        failed = True
    prediction_kernel.set_window_end(windows[1], 90)
    if list(prediction_kernel.window_bound_arrays(windows)[1]) != [30, 90]:
        print("ERROR: set_window_end did not invalidate the array cache")
        failed = True

    # Two distinct lists holding equal values must not alias to one another
    other = [{"start": 999, "end": 1200}]
    if list(prediction_kernel.window_bound_arrays(other)[0]) != [999]:
        print("ERROR: a second window list aliased onto the first cache entry")
        failed = True
    if list(prediction_kernel.window_bound_arrays(windows)[0]) != [15, 60]:
        print("ERROR: caching a second list corrupted the first entry")
        failed = True

    # The cache is bounded, so a caller that never repeats a list (a forked pool worker unpickles
    # fresh lists every call) cannot grow it without limit
    prediction_kernel.invalidate_window_cache()
    for index in range(prediction_kernel.WINDOW_CACHE_MAX * 4):
        prediction_kernel.window_bound_arrays([{"start": index, "end": index + 5}])
    if len(prediction_kernel._WINDOW_BOUNDS_CACHE) > prediction_kernel.WINDOW_CACHE_MAX:
        print("ERROR: window cache grew past its bound: {}".format(len(prediction_kernel._WINDOW_BOUNDS_CACHE)))
        failed = True

    # disable_window_cache (the pool worker initialiser) must turn caching off without changing
    # the values returned, since workers unpickle fresh lists and can never hit it anyway
    prediction_kernel.invalidate_window_cache()
    try:
        prediction_kernel.disable_window_cache()
        disabled = [{"start": 5, "end": 35}, {"start": 100, "end": 160}]
        first_starts, first_ends = prediction_kernel.window_bound_arrays(disabled)
        second_starts, _ = prediction_kernel.window_bound_arrays(disabled)
        if list(first_starts) != [5, 100] or list(first_ends) != [35, 160]:
            print("ERROR: window_bound_arrays gave wrong values with the cache disabled: {} / {}".format(list(first_starts), list(first_ends)))
            failed = True
        if first_starts is second_starts:
            print("ERROR: window_bound_arrays still cached with the cache disabled")
            failed = True
        if prediction_kernel.window_bound_tuple(disabled) != ((5, 35), (100, 160)):
            print("ERROR: window_bound_tuple gave wrong values with the cache disabled")
            failed = True
        if prediction_kernel._WINDOW_BOUNDS_CACHE:
            print("ERROR: the cache stored entries while disabled")
            failed = True
    finally:
        prediction_kernel.WINDOW_CACHE_ENABLED = True
        prediction_kernel.invalidate_window_cache()

    # The validator itself must actually catch a stale entry, otherwise the plan replay below
    # would pass no matter what
    prediction_kernel.invalidate_window_cache()
    sneaky = [{"start": 0, "end": 30}]
    prediction_kernel.window_bound_arrays(sneaky)
    prediction_kernel.window_bound_tuple(sneaky)
    sneaky[0]["start"] = 10  # deliberately bypassing set_window_start
    prediction_kernel.VALIDATE_WINDOW_CACHE = True
    try:
        prediction_kernel.window_bound_arrays(sneaky)
        print("ERROR: VALIDATE_WINDOW_CACHE did not detect a stale entry")
        failed = True
    except AssertionError:
        pass
    finally:
        prediction_kernel.VALIDATE_WINDOW_CACHE = False
        prediction_kernel.invalidate_window_cache()

    # Replay a real plan with validation on - every cache hit re-derives and compares, so any
    # window mutated without set_window_start on the planning path raises here.
    # The instance is loaded from a debug dump rather than used as handed over: this needs complete,
    # deterministic planning state, and the shared fixture carries whatever earlier tests left on it.
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml("cases/predbat_debug_agile1.yaml")
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()
    my_predbat.args["threads"] = 0
    my_predbat.debug_enable = False
    my_predbat.plan_valid = False

    prediction_kernel.VALIDATE_WINDOW_CACHE = True
    try:
        my_predbat.calculate_plan(recompute=True)
    except AssertionError as error:
        print("ERROR: stale window cache during a real plan: {}".format(error))
        failed = True
    finally:
        prediction_kernel.VALIDATE_WINDOW_CACHE = False
        prediction_kernel.invalidate_window_cache()

    return failed

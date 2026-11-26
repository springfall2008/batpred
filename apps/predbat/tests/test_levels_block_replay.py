# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the block replay in optimise_charge_limit_price_threads.

When a price threshold yields the same candidate windows and slot ladders as an earlier one in the
same call, the levels sweep does not rebuild its trials: they are all in tried_list already, so it
folds their recorded metrics into levels_score instead. A wrong replay would usually leave the final
plan unchanged, so the golden and random tests could stay green while levels_score - which the
region passes use to decide which thresholds to skip - silently went wrong. These tests run the
same call with and without replay and require identical results, and require that the replay
path is actually taken.
"""

from prediction import Prediction
from const import EXPORT_MODE_IDLE
from utils import pack_export_limit
from tests.test_infra import reset_inverter, reset_rates, update_rates_import, update_rates_export

SAVED_ATTRIBUTES = (
    "metric_battery_value_export_scaling",
    "metric_battery_value_scaling",
    "pv_metric10_weight",
    "pv_metric90_weight",
    "calculate_best_charge",
    "calculate_best_export",
    "calculate_export_first",
    "calculate_export_oncharge",
    "charge_window_best",
    "charge_limit_best",
    "export_window_best",
    "export_limits_best",
    "pv_forecast_minute_step",
    "pv_forecast_minute10_step",
    "load_minutes_step",
    "load_minutes_step10",
    "prediction",
    "manual_charge_times",
    "manual_export_times",
    "forecast_minutes",
    "end_record",
    "soc_max",
    "soc_kw",
    "best_soc_max",
    "best_soc_keep",
    "best_soc_keep_weight",
    "num_inverters",
    "set_charge_freeze",
    "set_export_freeze",
    "metric_battery_cycle",
)

WINDOW_COUNT = 6
REGION_WINDOWS = 3


def run_levels_block_replay_tests(my_predbat):
    """Run the levels-pass block replay tests"""
    failed = False
    failed |= test_region_replay_matches_rebuild(my_predbat)
    failed |= test_whole_plan_replay_matches_rebuild(my_predbat)
    failed |= test_entry_dedupe_matches_full_loop(my_predbat)
    return failed


def make_window(start, end, average):
    """Build a minimal window dict"""
    return {"start": start, "end": end, "average": average}


def setup_levels_scenario(my_predbat):
    """A 10 kWh battery starting empty, 1 kW of load, and an hourly charge window and export window
    in each of the next WINDOW_COUNT hours, each at its own price. Import is 10p outside the windows,
    so charging pays in the cheap windows and not in the dear ones and exporting pays at the start:
    trials genuinely differ in metric, which the replay comparison needs. Set up the way
    run_optimise_levels does. Returns the saved state to hand back to restore_state()."""
    saved = {name: getattr(my_predbat, name, None) for name in SAVED_ATTRIBUTES}
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes
    my_predbat.best_soc_max = 0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_keep_weight = 0.5
    my_predbat.num_inverters = 1
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 0.0

    my_predbat.metric_battery_value_export_scaling = 1.0
    my_predbat.metric_battery_value_scaling = 1.0
    my_predbat.pv_metric10_weight = 0.0
    my_predbat.pv_metric90_weight = 0.0
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.calculate_export_first = True
    my_predbat.calculate_export_oncharge = False  # exercises the memoised export-on-charge pruning too
    my_predbat.manual_charge_times = []
    my_predbat.manual_export_times = []

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 1.0 / (60 / 5)
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.args["threads"] = 0

    reset_rates(my_predbat, 10.0, 5.0)
    now = my_predbat.minutes_now
    charge_window = [make_window(now + 60 * n, now + 60 * n + 30, 4.0 + 3.0 * n) for n in range(WINDOW_COUNT)]
    export_window = [make_window(now + 60 * n + 30, now + 60 * n + 60, 30.0 - 2.5 * n) for n in range(WINDOW_COUNT)]
    update_rates_import(my_predbat, charge_window)
    update_rates_export(my_predbat, export_window)
    # Prediction takes its copy of the rates when it is built, so it has to come after them
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    my_predbat.charge_window_best = charge_window
    my_predbat.charge_limit_best = [0] * WINDOW_COUNT
    my_predbat.export_window_best = export_window
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_IDLE)] * WINDOW_COUNT
    return saved


def restore_state(my_predbat, saved):
    for name, value in saved.items():
        setattr(my_predbat, name, value)


def run_levels(my_predbat, block_replay, region=None):
    """One optimise_charge_limit_price_threads call on fresh copies of the scenario's lists (the call
    edits its limit lists in place). Returns the result tuple and how many blocks it replayed."""
    charge_window = my_predbat.charge_window_best
    export_window = my_predbat.export_window_best
    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window, export_window)
    region_start, region_end = region if region else (None, None)
    replay_stats = {}
    result = my_predbat.optimise_charge_limit_price_threads(
        price_set,
        price_links,
        window_index,
        len(charge_window),
        len(export_window),
        list(my_predbat.charge_limit_best),
        charge_window,
        export_window,
        list(my_predbat.export_limits_best),
        end_record=my_predbat.end_record,
        region_start=region_start,
        region_end=region_end,
        quiet=True,
        block_replay=block_replay,
        replay_stats=replay_stats,
    )
    return result, replay_stats["replayed_blocks"]


def compare_runs(label, replayed, rebuilt, replay_count, rebuild_count):
    """Both runs must agree on everything the caller sees; only the replay count may differ"""
    failed = False
    names = ("best_limits", "best_export_limits", "best_metric", "best_cost", "best_keep", "best_soc_min", "best_cycle", "best_carbon", "best_import", "best_battery_value")
    for index, name in enumerate(names):
        if replayed[index] != rebuilt[index]:
            print("ERROR: {}: {} differs with replay: {} vs {} rebuilt".format(label, name, replayed[index], rebuilt[index]))
            failed = True
    if len(replayed[10]) != len(rebuilt[10]):
        print("ERROR: {}: tried_list holds {} scenarios with replay but {} rebuilt".format(label, len(replayed[10]), len(rebuilt[10])))
        failed = True
    if replayed[11] != rebuilt[11]:
        print("ERROR: {}: levels_score differs with replay: {} vs {} rebuilt".format(label, replayed[11], rebuilt[11]))
        failed = True
    if replayed[12:14] != rebuilt[12:14]:
        print("ERROR: {}: best slot counts differ with replay: {} vs {} rebuilt".format(label, replayed[12:14], rebuilt[12:14]))
        failed = True
    if replay_count <= 0:
        print("ERROR: {}: expected at least one replayed block, got {} - the test no longer exercises the replay".format(label, replay_count))
        failed = True
    if rebuild_count != 0:
        print("ERROR: {}: block_replay=False still replayed {} blocks".format(label, rebuild_count))
        failed = True
    return failed


def test_region_replay_matches_rebuild(my_predbat):
    """A region pass is where repeats are common: windows outside the region still contribute price
    thresholds but no candidates, so neighbouring thresholds build identical blocks"""
    print("**** test_region_replay_matches_rebuild ****")
    saved = setup_levels_scenario(my_predbat)
    try:
        now = my_predbat.minutes_now
        region = (now, now + 60 * REGION_WINDOWS)
        replayed, replay_count = run_levels(my_predbat, True, region)
        rebuilt, rebuild_count = run_levels(my_predbat, False, region)
        failed = compare_runs("region", replayed, rebuilt, replay_count, rebuild_count)
    finally:
        restore_state(my_predbat, saved)
    if not failed:
        print("PASS ({} blocks replayed)".format(replay_count))
    return failed


def test_whole_plan_replay_matches_rebuild(my_predbat):
    """The same over the whole plan with manually overridden windows: their prices are still
    thresholds, but they are never candidates, so crossing one repeats the previous block"""
    print("**** test_whole_plan_replay_matches_rebuild ****")
    saved = setup_levels_scenario(my_predbat)
    try:
        # manual_all_times is derived from the per-type lists
        my_predbat.manual_charge_times = [my_predbat.charge_window_best[2]["start"]]
        my_predbat.manual_export_times = [my_predbat.export_window_best[4]["start"]]
        replayed, replay_count = run_levels(my_predbat, True)
        rebuilt, rebuild_count = run_levels(my_predbat, False)
        failed = compare_runs("whole plan", replayed, rebuilt, replay_count, rebuild_count)
    finally:
        restore_state(my_predbat, saved)
    if not failed:
        print("PASS ({} blocks replayed)".format(replay_count))
    return failed


def run_levels_dedupe(my_predbat, dedupe_entries, region=None, enable_coarse_fine=True):
    """As run_levels, with block replay on and the repeated-selection skip on or off. Returns the
    result tuple and the call's stats."""
    charge_window = my_predbat.charge_window_best
    export_window = my_predbat.export_window_best
    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window, export_window)
    region_start, region_end = region if region else (None, None)
    stats = {}
    result = my_predbat.optimise_charge_limit_price_threads(
        price_set,
        price_links,
        window_index,
        len(charge_window),
        len(export_window),
        list(my_predbat.charge_limit_best),
        charge_window,
        export_window,
        list(my_predbat.export_limits_best),
        end_record=my_predbat.end_record,
        region_start=region_start,
        region_end=region_end,
        quiet=True,
        replay_stats=stats,
        dedupe_entries=dedupe_entries,
        enable_coarse_fine=enable_coarse_fine,
    )
    return result, stats


def test_entry_dedupe_matches_full_loop(my_predbat):
    """Skipping a charge or export selection that repeats an earlier one in its block changes nothing
    the caller sees.

    With charge and export freeze on, the fine blocks try every slot count with and without freeze,
    and as none of these windows is a freeze-only one the two options select the same windows: the
    case the skip exists for. The skipping and the full loop must agree on the plan, on every
    recorded metric in tried_list, on levels_score and on the best slot counts, and the skip must
    actually have been taken. Run over the whole plan and over a region, and with a battery cycle
    cost, which only a charge entry pays, so each window's freeze entry sorts below its charge entry:
    then the freeze option selects the same windows as the non-freeze one but with freeze flags set,
    which must not be taken for a repeat. Also run without the coarse/fine split, where no coarse
    block has already tried the non-freeze selections, so a skip that ignored the freeze flags would
    drop trials nothing else runs.
    """
    print("**** test_entry_dedupe_matches_full_loop ****")
    failed = False
    saved = setup_levels_scenario(my_predbat)
    try:
        my_predbat.set_charge_freeze = True
        my_predbat.set_export_freeze = True
        my_predbat.prediction = Prediction(my_predbat, my_predbat.pv_forecast_minute_step, my_predbat.pv_forecast_minute10_step, my_predbat.load_minutes_step, my_predbat.load_minutes_step10)
        now = my_predbat.minutes_now
        names = ("best_limits", "best_export_limits", "best_metric", "best_cost", "best_keep", "best_soc_min", "best_cycle", "best_carbon", "best_import", "best_battery_value")
        cases = []
        for battery_cycle in (0.0, 2.0):
            for coarse_fine in (True, False):
                for region_label, region in (("whole plan", None), ("region", (now, now + 60 * REGION_WINDOWS))):
                    cases.append(("{} battery_cycle {} coarse_fine {}".format(region_label, battery_cycle, coarse_fine), battery_cycle, coarse_fine, region))
        for label, battery_cycle, coarse_fine, region in cases:
            my_predbat.metric_battery_cycle = battery_cycle
            deduped, deduped_stats = run_levels_dedupe(my_predbat, True, region, coarse_fine)
            full, full_stats = run_levels_dedupe(my_predbat, False, region, coarse_fine)
            for index, name in enumerate(names):
                if deduped[index] != full[index]:
                    print("ERROR: {}: {} differs with the skip: {} vs {} in full".format(label, name, deduped[index], full[index]))
                    failed = True
            if deduped[10] != full[10]:
                print("ERROR: {}: tried_list differs with the skip ({} vs {} scenarios)".format(label, len(deduped[10]), len(full[10])))
                failed = True
            if deduped[11] != full[11]:
                print("ERROR: {}: levels_score differs with the skip: {} vs {} in full".format(label, deduped[11], full[11]))
                failed = True
            if deduped[12:14] != full[12:14]:
                print("ERROR: {}: best slot counts differ with the skip: {} vs {} in full".format(label, deduped[12:14], full[12:14]))
                failed = True
            if deduped_stats["deduped_entries"] <= 0:
                print("ERROR: {}: expected repeated selections to be skipped, got {} - the test no longer exercises the skip".format(label, deduped_stats["deduped_entries"]))
                failed = True
            if full_stats["deduped_entries"] != 0:
                print("ERROR: {}: dedupe_entries=False still skipped {} selections".format(label, full_stats["deduped_entries"]))
                failed = True
            if not failed:
                print("{}: {} repeated selections skipped, results identical".format(label, deduped_stats["deduped_entries"]))
    finally:
        restore_state(my_predbat, saved)
    if not failed:
        print("PASS")
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""
Tests for set_charge_freeze_only, the charge side counterpart of set_export_freeze_only. When the
switch is On Predbat may freeze charge (hold the current SoC) but must never charge the battery
from the grid, so no charge window may ever be planned or executed at a target above the reserve.
"""
from config import CONFIG_ITEMS
from predbat import PredBat
from prediction import Prediction
from tests.test_infra import reset_rates, reset_inverter, update_rates_import, update_rates_export


def run_charge_freeze_only_tests(my_predbat):
    """Run all set_charge_freeze_only tests"""
    failed = False
    # This module calls load_user_config()/fetch_config_options() and then overwrites a lot of the
    # planner's state to build its scenario. Several later modules (test_optimise_all_windows in
    # particular) do no config reset of their own and inherit whatever the previous test left, so
    # restoring the fixture wholesale on the way out is the only way to keep them independent of
    # where this module sits in the registry.
    saved_state = dict(my_predbat.__dict__)
    try:
        failed |= test_config_item_registered()
        failed |= test_config_option_fetched(my_predbat)
        failed |= test_reset_defaults_to_off()
        failed |= test_plan_grid_charges_when_switch_off(my_predbat)
        failed |= test_plan_never_grid_charges_when_switch_on(my_predbat)
        failed |= test_plan_still_uses_freeze_charge_when_switch_on(my_predbat)
        failed |= test_plan_does_not_charge_at_all_when_freeze_also_disabled(my_predbat)
        failed |= test_prefill_charge_limit_uses_inverter_limit_when_switch_off(my_predbat)
        failed |= test_prefill_charge_limit_clamped_when_switch_on(my_predbat)
        failed |= test_clip_does_not_promote_freeze_to_charge_when_switch_on(my_predbat)
        failed |= test_windows_beyond_record_not_set_to_full_charge_when_switch_on(my_predbat)
        failed |= test_freeze_only_search_drops_an_incoming_grid_charge(my_predbat)
        failed |= test_price_threads_returns_no_grid_charge(my_predbat)
        failed |= test_price_threads_does_not_simulate_forbidden_charges(my_predbat)
        failed |= test_windows_can_still_be_turned_off_with_best_soc_min_set(my_predbat)
        failed |= test_both_permitted_outcomes_considered_with_best_soc_min_set(my_predbat)
    finally:
        my_predbat.__dict__.clear()
        my_predbat.__dict__.update(saved_state)
    return failed


def find_config_item(name):
    """Return the CONFIG_ITEMS entry with this name, or None"""
    for item in CONFIG_ITEMS:
        if item.get("name") == name:
            return item
    return None


def test_config_item_registered():
    """The switch is registered as an expert-mode switch defaulting to Off, like its export twin"""
    print("**** test_config_item_registered ****")
    failed = False
    item = find_config_item("set_charge_freeze_only")
    if item is None:
        print("ERROR: set_charge_freeze_only is not registered in CONFIG_ITEMS")
        return True

    export_twin = find_config_item("set_export_freeze_only")
    for key in ("type", "enable", "default", "reset_inverter"):
        if item.get(key) != export_twin.get(key):
            print("ERROR: set_charge_freeze_only {} is {} but set_export_freeze_only uses {}".format(key, item.get(key), export_twin.get(key)))
            failed = True
    if item.get("friendly_name") != "Set Charge Freeze Only":
        print("ERROR: set_charge_freeze_only friendly_name is {}".format(item.get("friendly_name")))
        failed = True
    return failed


def test_config_option_fetched(my_predbat):
    """fetch_config_options publishes the switch onto the instance so the planner can read it"""
    print("**** test_config_option_fetched ****")
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    value = getattr(my_predbat, "set_charge_freeze_only", "missing")
    if value is not False:
        print("ERROR: set_charge_freeze_only should default to False after fetch_config_options, got {}".format(value))
        return True
    return False


def test_reset_defaults_to_off():
    """reset() must initialise the switch to Off so a run before the config is fetched can't grid charge"""
    print("**** test_reset_defaults_to_off ****")
    bare = PredBat.__new__(PredBat)
    bare.args = {}
    bare.log = lambda *args, **kwargs: None  # reset() logs the config root, which needs a real logfile
    bare.reset()
    value = getattr(bare, "set_charge_freeze_only", "missing")
    if value is not False:
        print("ERROR: reset() should set set_charge_freeze_only to False, got {}".format(value))
        return True
    return False


def setup_scenario(my_predbat, set_charge_freeze_only, set_charge_freeze=True, best_soc_min=0.0):
    """
    Build a day of half-hourly windows whose prices cycle 0..15p with a small constant load and a
    nearly empty battery. Without any restriction the optimiser grid charges in the cheap windows
    and freeze charges in a few mid-priced ones, so the same scenario exercises both charge modes.
    Returns the charge windows it set up.
    """
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)

    charge_window_best = []
    for n in range(0, 48):
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": n % 16})
    export_window_best = []

    my_predbat.forecast_minutes = 24 * 60
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.calculate_second_pass = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 2.0
    my_predbat.reserve = 0.5
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_loss = 0.9
    my_predbat.best_soc_keep = 1.0
    my_predbat.best_soc_keep_weight = 0.5
    my_predbat.set_charge_freeze = set_charge_freeze
    my_predbat.set_charge_freeze_only = set_charge_freeze_only
    my_predbat.best_soc_min = best_soc_min
    my_predbat.debug_enable = False

    reset_rates(my_predbat, 10.0, 5.5)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 0.2 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    my_predbat.charge_limit_best = [0 for n in range(len(charge_window_best))]
    my_predbat.export_limits_best = []
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best
    my_predbat.end_record = end_record
    return charge_window_best


def run_plan(my_predbat, name, set_charge_freeze_only, set_charge_freeze=True, best_soc_min=0.0):
    """Set up the scenario and run a full optimisation over it, returning the resulting limits"""
    print("**** {} ****".format(name))
    charge_window_best = setup_scenario(my_predbat, set_charge_freeze_only, set_charge_freeze, best_soc_min)
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        my_predbat.charge_limit_best, charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=my_predbat.end_record
    )
    my_predbat.optimise_all_windows(metric, metric_keep)
    return my_predbat.charge_limit_best


def test_plan_grid_charges_when_switch_off(my_predbat):
    """
    Baseline for the plan tests below: with the switch Off this scenario really does plan grid
    charges above the reserve, so a plan without them afterwards is the switch working rather than
    a scenario that never wanted to charge in the first place.
    """
    charge_limit_best = run_plan(my_predbat, "test_plan_grid_charges_when_switch_off", set_charge_freeze_only=False)
    if not [limit for limit in charge_limit_best if limit > my_predbat.reserve]:
        print("ERROR: baseline scenario planned no grid charge at all, limits {}".format(charge_limit_best))
        return True
    return False


def test_plan_never_grid_charges_when_switch_on(my_predbat):
    """With the switch On no charge window may be planned above the reserve"""
    charge_limit_best = run_plan(my_predbat, "test_plan_never_grid_charges_when_switch_on", set_charge_freeze_only=True)
    above_reserve = [(n, limit) for n, limit in enumerate(charge_limit_best) if limit > my_predbat.reserve]
    if above_reserve:
        print("ERROR: set_charge_freeze_only planned grid charges at windows {}".format(above_reserve))
        return True
    return False


def test_plan_still_uses_freeze_charge_when_switch_on(my_predbat):
    """
    The switch forbids grid charging, not charge windows - freeze charge must still be reachable,
    otherwise the implementation has just turned charge planning off entirely.
    """
    charge_limit_best = run_plan(my_predbat, "test_plan_still_uses_freeze_charge_when_switch_on", set_charge_freeze_only=True)
    if my_predbat.reserve not in charge_limit_best:
        print("ERROR: set_charge_freeze_only planned no freeze charge at all, limits {}".format(charge_limit_best))
        return True
    return False


def test_plan_does_not_charge_at_all_when_freeze_also_disabled(my_predbat):
    """
    set_charge_freeze Off forbids freeze charge and set_charge_freeze_only forbids grid charge, so
    together they leave no usable charge mode - every window must be left in demand mode.
    """
    charge_limit_best = run_plan(my_predbat, "test_plan_does_not_charge_at_all_when_freeze_also_disabled", set_charge_freeze_only=True, set_charge_freeze=False)
    charging = [(n, limit) for n, limit in enumerate(charge_limit_best) if limit > 0]
    if charging:
        print("ERROR: both switches forbid charging but windows {} are still set".format(charging))
        return True
    return False


def run_prefill(my_predbat, set_charge_freeze_only):
    """
    Drive the pre-fill used when Predbat is not optimising charge itself, with the inverter sat at
    a 100% charge limit. Restores the state it touches so later tests are unaffected.
    """
    saved = (my_predbat.soc_max, my_predbat.reserve, my_predbat.current_charge_limit, my_predbat.charge_window_best, my_predbat.charge_limit_best, my_predbat.set_charge_freeze_only)
    try:
        my_predbat.soc_max = 10.0
        my_predbat.reserve = 0.5
        my_predbat.current_charge_limit = 100.0
        my_predbat.set_charge_freeze_only = set_charge_freeze_only
        my_predbat.charge_window_best = [{"start": 720, "end": 750, "average": 10.0}, {"start": 750, "end": 780, "average": 10.0}]
        my_predbat.prefill_charge_limit_best()
        return my_predbat.charge_limit_best
    finally:
        (my_predbat.soc_max, my_predbat.reserve, my_predbat.current_charge_limit, my_predbat.charge_window_best, my_predbat.charge_limit_best, my_predbat.set_charge_freeze_only) = saved


def test_prefill_charge_limit_uses_inverter_limit_when_switch_off(my_predbat):
    """Baseline: the pre-fill takes the inverter's own charge limit, one entry per window"""
    print("**** test_prefill_charge_limit_uses_inverter_limit_when_switch_off ****")
    charge_limit_best = run_prefill(my_predbat, set_charge_freeze_only=False)
    if charge_limit_best != [10.0, 10.0]:
        print("ERROR: pre-fill should be [10.0, 10.0] got {}".format(charge_limit_best))
        return True
    return False


def test_prefill_charge_limit_clamped_when_switch_on(my_predbat):
    """
    With calculate_best_charge off the charge limits come from the inverter rather than the
    optimiser, so the switch has to clamp them here too - otherwise the plan and the prediction
    assume a grid charge that execute_plan's safeguard then refuses to perform.
    """
    print("**** test_prefill_charge_limit_clamped_when_switch_on ****")
    charge_limit_best = run_prefill(my_predbat, set_charge_freeze_only=True)
    if charge_limit_best != [0.5, 0.5]:
        print("ERROR: pre-fill should be clamped to the reserve [0.5, 0.5] got {}".format(charge_limit_best))
        return True
    return False


def test_clip_does_not_promote_freeze_to_charge_when_switch_on(my_predbat):
    """
    clip_charge_slots turns a freeze charge into a full charge when the battery is already at 100%
    (see test_freeze_charge_to_charge_at_100_soc). Harmless normally - the battery is full so
    nothing is imported - but it writes a grid charge into the plan, which the execute safeguard
    then clamps straight back to a freeze. Leave the freeze alone when the switch is on.
    """
    print("**** test_clip_does_not_promote_freeze_to_charge_when_switch_on ****")
    reset_inverter(my_predbat)
    saved = (my_predbat.soc_max, my_predbat.reserve, my_predbat.set_charge_freeze_only, my_predbat.debug_enable)
    try:
        my_predbat.soc_max = 10.0
        my_predbat.reserve = 0.5
        my_predbat.debug_enable = False
        my_predbat.set_charge_freeze_only = True

        minutes_now = 720
        windows = [{"start": 720, "end": 750, "average": 10.0}]
        limits = [my_predbat.reserve]
        predict_soc = {minute: my_predbat.soc_max for minute in range(0, 65, 5)}

        result_windows, result_limits = my_predbat.clip_charge_slots(minutes_now, predict_soc, windows, limits, 1, 5)

        if result_limits[0] != my_predbat.reserve:
            print("ERROR: freeze charge at 100% should stay a freeze ({}) but got {}".format(my_predbat.reserve, result_limits[0]))
            return True
        return False
    finally:
        (my_predbat.soc_max, my_predbat.reserve, my_predbat.set_charge_freeze_only, my_predbat.debug_enable) = saved


def test_windows_beyond_record_not_set_to_full_charge_when_switch_on(my_predbat):
    """
    optimise_charge_windows_reset parks every window beyond the record window at a full charge, on
    the assumption that Predbat can always charge later. It can't when grid charging is forbidden,
    so those windows would model - and publish - an import that can never happen.
    """
    print("**** test_windows_beyond_record_not_set_to_full_charge_when_switch_on ****")
    saved = (my_predbat.soc_max, my_predbat.reserve, my_predbat.set_charge_freeze_only, my_predbat.charge_window_best, my_predbat.charge_limit_best, my_predbat.export_window_best, my_predbat.end_record, my_predbat.calculate_best_charge)
    try:
        my_predbat.soc_max = 10.0
        my_predbat.reserve = 0.5
        my_predbat.set_charge_freeze_only = True
        my_predbat.calculate_best_charge = True
        my_predbat.end_record = 60
        my_predbat.export_window_best = []
        # One window inside the record window, one well beyond it
        my_predbat.charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 10.0}, {"start": my_predbat.minutes_now + 600, "end": my_predbat.minutes_now + 630, "average": 10.0}]
        my_predbat.charge_limit_best = [0.0, 0.0]

        my_predbat.optimise_charge_windows_reset(reset_all=True)

        above_reserve = [(n, limit) for n, limit in enumerate(my_predbat.charge_limit_best) if limit > my_predbat.reserve]
        if above_reserve:
            print("ERROR: windows {} were parked at a grid charge".format(above_reserve))
            return True
        return False
    finally:
        (my_predbat.soc_max, my_predbat.reserve, my_predbat.set_charge_freeze_only, my_predbat.charge_window_best, my_predbat.charge_limit_best, my_predbat.export_window_best, my_predbat.end_record, my_predbat.calculate_best_charge) = saved


def test_freeze_only_search_drops_an_incoming_grid_charge(my_predbat):
    """
    The freeze_only search keeps the window's existing limit as a candidate, so a plan built before
    the switch was turned on (plans persist across cycles and are held by metric_min_improvement_plan)
    could carry a grid charge straight through the optimiser and back into the new plan.
    """
    print("**** test_freeze_only_search_drops_an_incoming_grid_charge ****")
    charge_window_best = setup_scenario(my_predbat, set_charge_freeze_only=True)
    # Window 0 arrives already set to a full charge, as a plan from before the switch would leave it
    my_predbat.charge_limit_best[0] = my_predbat.soc_max

    best_soc = my_predbat.optimise_charge_limit(
        0,
        len(charge_window_best),
        my_predbat.charge_limit_best,
        charge_window_best,
        my_predbat.export_window_best,
        my_predbat.export_limits_best,
        end_record=my_predbat.end_record,
        freeze_only=True,
    )[0]

    if best_soc > my_predbat.reserve:
        print("ERROR: freeze_only search kept the incoming grid charge, selected {}".format(best_soc))
        return True
    return False


def run_price_threads(my_predbat, set_charge_freeze_only, set_charge_freeze=True):
    """Run just the price-threshold level search over the scenario and return its charge limits"""
    charge_window_best = setup_scenario(my_predbat, set_charge_freeze_only, set_charge_freeze)
    record_charge_windows = max(my_predbat.max_charge_windows(my_predbat.end_record + my_predbat.minutes_now, charge_window_best), 1)
    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window_best[:record_charge_windows], [])
    my_predbat.optimise_charge_windows_reset(reset_all=True)
    return my_predbat.optimise_charge_limit_price_threads(
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        0,
        my_predbat.charge_limit_best,
        charge_window_best,
        my_predbat.export_window_best,
        my_predbat.export_limits_best,
        end_record=my_predbat.end_record,
        fast=True,
        quiet=True,
    )


def test_price_threads_returns_no_grid_charge(my_predbat):
    """
    The price-threshold search selects windows itself and only levels their limits at the end, via
    optimise_charge_limit. That levelling pass is the only thing keeping its output within the
    switch, so pin it here rather than relying on the detailed pass to clean up afterwards - with
    the switch off this same scenario returns a 7.5kWh grid charge in those windows.
    """
    print("**** test_price_threads_returns_no_grid_charge ****")
    charge_limit_best = run_price_threads(my_predbat, set_charge_freeze_only=True)[0]
    above_reserve = [(n, limit) for n, limit in enumerate(charge_limit_best) if limit > my_predbat.reserve]
    if above_reserve:
        print("ERROR: price threshold search scored grid charges at windows {}".format(above_reserve))
        return True
    return False


def test_price_threads_does_not_simulate_forbidden_charges(my_predbat):
    """
    The price-threshold search sweeps price levels and slot counts, simulating a scenario per
    combination. Selecting a window is not the same as freezing it - the candidate list mixes the
    charge and freeze entries a window contributes, so without an explicit restriction the sweep
    simulates full-charge scenarios that the switch forbids, only for the levelling pass at the end
    to flatten them back to a freeze. That work is wasted, and the price level and slot count get
    chosen by comparing plans that can never run.

    tried_list is the search's scenario cache, logged as "total simulations", so its size is a
    direct count of the work done. With the switch on that count has to drop.
    """
    print("**** test_price_threads_does_not_simulate_forbidden_charges ****")
    simulations_off = len(run_price_threads(my_predbat, set_charge_freeze_only=False)[10])
    simulations_on = len(run_price_threads(my_predbat, set_charge_freeze_only=True)[10])
    print("Simulations: switch off {} switch on {}".format(simulations_off, simulations_on))
    if simulations_on >= simulations_off:
        print("ERROR: set_charge_freeze_only simulated {} scenarios vs {} with the switch off - forbidden charges are still being searched".format(simulations_on, simulations_off))
        return True
    return False


def candidates_considered(my_predbat, set_charge_freeze_only, best_soc_min):
    """The SoC candidates optimise_charge_limit actually simulates for the first charge window"""
    charge_window_best = setup_scenario(my_predbat, set_charge_freeze_only, best_soc_min=best_soc_min)
    seen = []
    original = my_predbat.launch_run_prediction_charge

    def spy(loop_soc, *args, **kwargs):
        seen.append(loop_soc)
        return original(loop_soc, *args, **kwargs)

    my_predbat.launch_run_prediction_charge = spy
    try:
        my_predbat.optimise_charge_limit(0, len(charge_window_best), my_predbat.charge_limit_best, charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)
    finally:
        my_predbat.launch_run_prediction_charge = original
    return sorted(set(seen))


def test_windows_can_still_be_turned_off_with_best_soc_min_set(my_predbat):
    """
    optimise_charge_limit's "off" candidate is best_soc_min_setting, which is max(reserve,
    best_soc_min) - so with best_soc_min above the reserve it is a real charge target that would
    import, and the switch has to drop it. Dropping it without putting a genuine off candidate back
    leaves the freeze as the window's only option, forcing a freeze on every window even where
    turning it off is cheaper. Both permitted outcomes must survive.
    """
    print("**** test_windows_can_still_be_turned_off_with_best_soc_min_set ****")
    charge_limit_best = run_plan(my_predbat, "best_soc_min_above_reserve", set_charge_freeze_only=True, best_soc_min=2.0)
    above_reserve = [(n, limit) for n, limit in enumerate(charge_limit_best) if limit > my_predbat.reserve]
    if above_reserve:
        print("ERROR: planned grid charges at windows {}".format(above_reserve))
        return True
    if not [limit for limit in charge_limit_best if limit == 0]:
        print("ERROR: every window was forced to a freeze, none could be turned off - limits {}".format(charge_limit_best))
        return True
    return False


def test_both_permitted_outcomes_considered_with_best_soc_min_set(my_predbat):
    """
    The switch permits exactly two outcomes for a charge window - off, and a freeze at the reserve -
    and the optimiser has to be able to weigh both. The "off" candidate it would otherwise use is
    best_soc_min_setting, which is max(reserve, best_soc_min) and so is a real charge target once
    best_soc_min is above the reserve; dropping that as a forbidden import must not leave the freeze
    as the window's only candidate, or the search becomes a foregone conclusion.

    best_soc_min changes neither permitted outcome, so the candidate set must not depend on it. Note
    the unrestricted optimiser does not offer 0.0 here at all - its low candidate is
    best_soc_min_setting - so this is specific to the switch.
    """
    print("**** test_both_permitted_outcomes_considered_with_best_soc_min_set ****")
    failed = False
    for best_soc_min in (0.0, 2.0):
        candidates = candidates_considered(my_predbat, True, best_soc_min)
        if candidates != [0.0, my_predbat.reserve]:
            print("ERROR: with best_soc_min {} the candidates should be off and freeze [0.0, {}] but got {}".format(best_soc_min, my_predbat.reserve, candidates))
            failed = True
    return failed

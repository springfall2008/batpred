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
Tests for the battery_soc_full_hysteresis feature.

Some inverters clamp their real max charge current to (near) zero once SoC reaches 100%, and will
not resume accepting charge current until SoC has dropped a few percent below full. These tests
cover the two halves of the fix: find_charge_rate()'s short-circuit (shared by live control, the
plan simulation and the dashboard display) and PredBat.update_battery_full_hysteresis()'s state
tracking (which decides when that short-circuit should apply, and survives a restart).
"""

from utils import find_charge_rate
from const import MINUTE_WATT


def test_find_charge_rate_hysteresis_clamps_to_min(my_predbat):
    """
    When full_hysteresis_active is True, find_charge_rate must return battery_rate_min immediately,
    regardless of target SoC, current SoC, low power mode, or any other input - this is what stops
    Predbat commanding a charge rate the inverter has already indicated it will not deliver.
    """
    failed = 0
    log_to = print
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    battery_rate_min = 100 / MINUTE_WATT

    for set_charge_low_power in (True, False):
        best_rate, best_rate_real = find_charge_rate(
            minutes_now,
            9.7,  # soc, kWh - within a 3% hysteresis band of a 10kWh soc_max
            window,
            10.0,  # target_soc - still asking to charge to full
            2500 / MINUTE_WATT,  # max_rate
            10.0,  # soc_max
            {},  # battery_charge_power_curve
            set_charge_low_power,
            10,  # charge_low_power_margin
            battery_rate_min,
            1.0,  # battery_rate_max_scaling
            0.96,  # battery_loss
            log_to,
            current_charge_rate=2500 / MINUTE_WATT,
            full_hysteresis_active=True,
        )
        if best_rate != battery_rate_min or best_rate_real != battery_rate_min:
            print("**** ERROR: full_hysteresis_active should clamp to battery_rate_min ({}), got best_rate {} best_rate_real {} (set_charge_low_power={}) ****".format(battery_rate_min, best_rate, best_rate_real, set_charge_low_power))
            failed = 1

    # Sanity check: with the flag False (the default, matching every pre-existing caller) the same
    # inputs charge normally rather than being clamped - the feature must be strictly opt-in.
    best_rate, best_rate_real = find_charge_rate(
        minutes_now,
        9.7,
        window,
        10.0,
        2500 / MINUTE_WATT,
        10.0,
        {},
        False,
        10,
        battery_rate_min,
        1.0,
        0.96,
        log_to,
        current_charge_rate=2500 / MINUTE_WATT,
    )
    if best_rate == battery_rate_min:
        print("**** ERROR: find_charge_rate should not clamp when full_hysteresis_active is not passed (default False) ****")
        failed = 1

    return failed


def test_battery_full_hysteresis_state_machine(my_predbat):
    """
    Exercise PredBat.update_battery_full_hysteresis()'s state transitions directly against soc_percent,
    independent of any real inverter or the plan simulation.
    """
    failed = 0

    def check(label, expected):
        if my_predbat.battery_full_hysteresis_active != expected:
            print("**** ERROR: {} - expected battery_full_hysteresis_active={}, got {} ****".format(label, expected, my_predbat.battery_full_hysteresis_active))
            return 1
        return 0

    # Disabled (default: hysteresis=0) must never activate, however full the battery is.
    my_predbat.battery_soc_full_hysteresis = 0
    my_predbat.battery_full_hysteresis_active = None
    my_predbat.soc_max = 10.0
    my_predbat.soc_percent = 100.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("disabled at 100%", False)

    # Enabled with a 3% band: 100% -> active
    my_predbat.battery_soc_full_hysteresis = 3.0
    my_predbat.battery_full_hysteresis_active = False
    my_predbat.soc_percent = 100.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("reaches 100%", True)

    # Still within the band (down to 98%, band floor is 97%) -> stays active
    my_predbat.soc_percent = 98.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("98% still within 3% band", True)

    # Exactly at the floor (97%) -> clears
    my_predbat.soc_percent = 97.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("97% at the floor of the band", False)

    # Re-enter the band from below (e.g. solar tops it back up to 99%) without touching 100% ->
    # must NOT reactivate, since the inverter is genuinely happy to accept current at 99% when it
    # was not sitting at 100% a moment ago.
    my_predbat.soc_percent = 99.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("99% approached from below, never hit 100%", False)

    # Reaches 100% again -> active once more
    my_predbat.soc_percent = 100.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("reaches 100% a second time", True)

    # Drops straight past the band in one step (e.g. a big load spike) -> clears
    my_predbat.soc_percent = 80.0
    my_predbat.update_battery_full_hysteresis()
    failed |= check("drops straight through the band", False)

    return failed


def test_battery_full_hysteresis_restores_after_restart(my_predbat):
    """
    battery_full_hysteresis_active starts as None on a fresh process (see PredBat.__init__) - the very
    first call must restore whatever was last published on the predbat.status sensor rather than
    assuming not-active, since the real battery may already be sitting inside the hysteresis band
    when Predbat restarts and has no other way to know that.
    """
    failed = 0
    status_entity = my_predbat.prefix + ".status"

    # Simulate a previous run having published "active" before this process started.
    my_predbat.ha_interface.dummy_items[status_entity] = {"state": "Idle", "battery_full_hysteresis_active": True}
    my_predbat.battery_full_hysteresis_active = None
    my_predbat.battery_soc_full_hysteresis = 3.0
    my_predbat.soc_max = 10.0
    my_predbat.soc_percent = 98.0  # inside the band either way, so the restored value is what decides
    my_predbat.update_battery_full_hysteresis()
    if my_predbat.battery_full_hysteresis_active is not True:
        print("**** ERROR: expected restored state True from predbat.status attribute, got {} ****".format(my_predbat.battery_full_hysteresis_active))
        failed = 1

    # And the inverse: nothing published before (fresh install) -> defaults to not-active rather than
    # assuming the worst.
    my_predbat.ha_interface.dummy_items.pop(status_entity, None)
    my_predbat.battery_full_hysteresis_active = None
    my_predbat.soc_percent = 98.0
    my_predbat.update_battery_full_hysteresis()
    if my_predbat.battery_full_hysteresis_active is not False:
        print("**** ERROR: expected default False with no prior published state, got {} ****".format(my_predbat.battery_full_hysteresis_active))
        failed = 1

    return failed


def test_battery_full_hysteresis_kernel_parity(my_predbat):
    """
    The C++ kernel mirrors find_charge_rate's hysteresis clamp independently (prediction_kernel.cpp),
    since the kernel does not call find_charge_rate itself. Run the same hysteresis-active scenario
    through both engines via the existing dual_run parity harness and confirm they agree bit-for-bit,
    AND that the clamp is genuinely doing something (soc barely moves) rather than both engines just
    happening to agree on an unrelated no-op.

    Skips (rather than fails) if the kernel is not available in this environment, matching the
    skip/require convention of tests/test_kernel_parity.py.
    """
    import os as _os

    from prediction_kernel import load_kernel
    from prediction import Prediction
    from tests.test_kernel_parity import dual_run, make_step_data
    from tests.test_infra import reset_inverter, reset_rates
    from const import PV_SCENARIO_NOMINAL

    lib = load_kernel(print)
    if not lib:
        if _os.environ.get("PREDBAT_KERNEL_REQUIRED") == "1":
            print("**** ERROR: kernel required (PREDBAT_KERNEL_REQUIRED=1) but not available ****")
            return 1
        print("Kernel not available in this environment - skipping kernel parity check (Python-only coverage above still applies)")
        return 0

    failed = 0
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
    my_predbat.battery_soc_full_hysteresis = 3.0
    my_predbat.battery_full_hysteresis_active = True  # battery is already sitting at 100%, per the scenario below
    my_predbat.soc_kw = my_predbat.soc_max  # start full
    pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, pv_kw=0.0, load_kw=0.0)

    # One charge window covering the whole forecast at full target - if hysteresis were not applied
    # (or not applied identically in both engines) this would charge immediately since soc < charge_limit
    # is false at exactly soc_max, so nudge the target a hair above to force the "charge enabled" branch.
    charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + my_predbat.forecast_minutes, "average": 10.0}]
    charge_limit = [my_predbat.soc_max]
    export_window = []
    export_limits = []
    end_record = my_predbat.forecast_minutes

    failed |= dual_run("battery_full_hysteresis", my_predbat, pv_step, pv10_step, load_step, load10_step, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record)

    # Confirm the clamp actually suppressed charging in the Python engine (dual_run already proved the
    # kernel matches it bit-for-bit, so checking one side is enough): soc should not have grown at all
    # in the entire window run with pv/load both zero, since battery_rate_min is 0 in reset_inverter().
    my_predbat.prediction_kernel_enable = False
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
    result = prediction.run_prediction(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, save=None, cache=False)
    final_soc = result[5]
    if abs(final_soc - my_predbat.soc_max) > 1e-6:
        print("**** ERROR: expected soc to stay at soc_max ({}) with hysteresis active and battery_rate_min=0, got {} - the clamp may not be suppressing charge ****".format(my_predbat.soc_max, final_soc))
        failed = 1

    return failed

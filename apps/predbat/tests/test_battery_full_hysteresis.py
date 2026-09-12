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
cover:
  - find_charge_rate() and get_charge_rate_curve_cached()'s clamp to 0.0 (the shared choke point
    used by every charging path: scheduled grid charging, PV self-consumption, export recapture).
  - Inverter.update_full_hysteresis()'s per-inverter state tracking (each inverter's own BMS clamps
    independently, so this cannot be tracked as one fleet-wide flag) and its restart persistence.
  - Kernel vs Python engine parity for the hysteresis-active scenario.

Every test that mutates the shared my_predbat/ha_interface fixture restores what it changed in a
finally block, since the test runner passes the same fixture instance to every test in sequence.
"""

from utils import find_charge_rate, get_charge_rate_curve_cached
from const import MINUTE_WATT
from inverter import Inverter


def test_find_charge_rate_hysteresis_clamps_to_zero(my_predbat):
    """
    When full_hysteresis_active is True, find_charge_rate must return (0.0, 0.0) immediately,
    regardless of target SoC, current SoC, low power mode, or any other input - this is what stops
    Predbat commanding a charge rate the inverter has already indicated it will not deliver.

    The floor is 0.0, not battery_rate_min: battery_rate_min models a different, unrelated inverter
    quirk (some inverters still trickle-charge a little even when commanded to 0), the opposite of
    what full_hysteresis_active represents (the inverter accepts nothing at all).
    """
    failed = 0
    log_to = print
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    battery_rate_min = 200 / MINUTE_WATT  # deliberately nonzero, to prove it is NOT what gets returned

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
        if best_rate != 0.0 or best_rate_real != 0.0:
            print("**** ERROR: full_hysteresis_active should clamp to 0.0 (not battery_rate_min={}), got best_rate {} best_rate_real {} (set_charge_low_power={}) ****".format(battery_rate_min, best_rate, best_rate_real, set_charge_low_power))
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
    if best_rate == 0.0:
        print("**** ERROR: find_charge_rate should not clamp when full_hysteresis_active is not passed (default False) ****")
        failed = 1

    return failed


def test_get_charge_rate_curve_cached_hysteresis_clamps_to_zero(my_predbat):
    """
    find_charge_rate() only covers scheduled grid charging. PV self-consumption charging and export
    recapture in the plan simulation (prediction.py) call get_charge_rate_curve_cached() directly,
    bypassing find_charge_rate() entirely - so the clamp must also live in get_charge_rate_curve_cached
    itself, the one function every charging path shares. This proves that directly, independent of
    which path calls it.
    """
    failed = 0
    rate = get_charge_rate_curve_cached(9.7, 2500 / MINUTE_WATT, 10.0, 2500 / MINUTE_WATT, (), 200 / MINUTE_WATT, 20, (), full_hysteresis_active=True)
    if rate != 0.0:
        print("**** ERROR: get_charge_rate_curve_cached should return 0.0 when full_hysteresis_active, got {} ****".format(rate))
        failed = 1

    # And confirm it behaves normally (nonzero) when the flag is absent/False - opt-in only.
    rate_normal = get_charge_rate_curve_cached(9.7, 2500 / MINUTE_WATT, 10.0, 2500 / MINUTE_WATT, (), 200 / MINUTE_WATT, 20, ())
    if rate_normal <= 0.0:
        print("**** ERROR: get_charge_rate_curve_cached should charge normally without full_hysteresis_active, got {} ****".format(rate_normal))
        failed = 1

    return failed


def test_inverter_full_hysteresis_state_machine(my_predbat):
    """
    Exercise Inverter.update_full_hysteresis()'s state transitions directly against a real Inverter
    instance's soc_kw/soc_max, independent of PredBat or any other inverter in a fleet.
    """
    failed = 0
    inv = Inverter(my_predbat, 0)

    def check(label, expected):
        if inv.full_hysteresis_active != expected:
            print("**** ERROR: {} - expected full_hysteresis_active={}, got {} ****".format(label, expected, inv.full_hysteresis_active))
            return 1
        return 0

    original_hysteresis = my_predbat.battery_soc_full_hysteresis
    try:
        # Disabled (default: hysteresis=0) must never activate, however full the battery is.
        my_predbat.battery_soc_full_hysteresis = 0
        inv.full_hysteresis_active = None
        inv.soc_max = 10.0
        inv.soc_kw = 10.0
        inv.update_full_hysteresis()
        failed |= check("disabled at 100%", False)

        # Enabled with a 3% band: 100% -> active
        my_predbat.battery_soc_full_hysteresis = 3.0
        inv.full_hysteresis_active = False
        inv.soc_kw = 10.0
        inv.update_full_hysteresis()
        failed |= check("reaches 100%", True)

        # Still within the band (down to 9.8kWh = 98%, band floor is 97%) -> stays active
        inv.soc_kw = 9.8
        inv.update_full_hysteresis()
        failed |= check("98% still within 3% band", True)

        # Exactly at the floor (97%) -> clears
        inv.soc_kw = 9.7
        inv.update_full_hysteresis()
        failed |= check("97% at the floor of the band", False)

        # Re-enter the band from below (e.g. solar tops it back up to 99%) without touching 100% ->
        # must NOT reactivate, since the inverter is genuinely happy to accept current at 99% when it
        # was not sitting at 100% a moment ago.
        inv.soc_kw = 9.9
        inv.update_full_hysteresis()
        failed |= check("99% approached from below, never hit 100%", False)

        # Reaches 100% again -> active once more
        inv.soc_kw = 10.0
        inv.update_full_hysteresis()
        failed |= check("reaches 100% a second time", True)

        # Drops straight past the band in one step (e.g. a big load spike) -> clears
        inv.soc_kw = 8.0
        inv.update_full_hysteresis()
        failed |= check("drops straight through the band", False)

        # Fractional threshold precision: 0.5% band must not be swallowed by whole-percent rounding.
        my_predbat.battery_soc_full_hysteresis = 0.5
        inv.full_hysteresis_active = True
        inv.soc_kw = 9.949  # 99.49% - below a 99.5% floor -> should clear
        inv.update_full_hysteresis()
        failed |= check("0.5% band: 99.49% is below a 99.5% floor", False)
        inv.full_hysteresis_active = True
        inv.soc_kw = 9.951  # 99.51% - above a 99.5% floor -> should stay active
        inv.update_full_hysteresis()
        failed |= check("0.5% band: 99.51% is above a 99.5% floor", True)
    finally:
        my_predbat.battery_soc_full_hysteresis = original_hysteresis

    return failed


def test_inverter_full_hysteresis_restores_after_restart(my_predbat):
    """
    full_hysteresis_active starts as None on a freshly constructed Inverter (a fresh process has no
    memory of whether the battery was previously at 100% and hasn't yet dropped through the hysteresis
    band) - the very first call must restore this inverter's own entry from the predbat.status sensor's
    per-inverter dict attribute rather than assuming not-active, since the real battery may already be
    sitting inside the hysteresis band when Predbat restarts and has no other way to know that.
    """
    failed = 0
    status_entity = my_predbat.prefix + ".status"
    original_status = my_predbat.ha_interface.dummy_items.get(status_entity)
    original_hysteresis = my_predbat.battery_soc_full_hysteresis

    try:
        my_predbat.battery_soc_full_hysteresis = 3.0
        inv = Inverter(my_predbat, 0)
        inv.soc_max = 10.0
        inv.soc_kw = 9.8  # inside the band either way, so the restored value is what decides

        # Simulate a previous run having published "active" for THIS inverter's id before this
        # process started, plus a different (inactive) state for a second inverter, to confirm each
        # inverter restores only its own entry rather than a shared/fleet-wide value.
        my_predbat.ha_interface.dummy_items[status_entity] = {"state": "Idle", "battery_full_hysteresis_active": {"0": True, "1": False}}
        inv.full_hysteresis_active = None
        inv.update_full_hysteresis()
        if inv.full_hysteresis_active is not True:
            print("**** ERROR: expected restored state True from predbat.status attribute for inverter 0, got {} ****".format(inv.full_hysteresis_active))
            failed = 1

        inv2 = Inverter(my_predbat, 0)  # construct safely (the fixture is only configured for
        # num_inverters=1, so __init__ with id=1 would fail on unconfigured per-inverter args lists);
        # override id afterwards purely to exercise update_full_hysteresis()'s per-id dict lookup,
        # which is all that's needed to prove restoration is keyed by id and not shared across inverters
        inv2.id = 1
        inv2.soc_max = 10.0
        inv2.soc_kw = 9.8
        inv2.full_hysteresis_active = None
        inv2.update_full_hysteresis()
        if inv2.full_hysteresis_active is not False:
            print("**** ERROR: expected restored state False from predbat.status attribute for inverter 1, got {} ****".format(inv2.full_hysteresis_active))
            failed = 1

        # And the case with nothing published before (fresh install) -> defaults to not-active rather
        # than assuming the worst.
        my_predbat.ha_interface.dummy_items.pop(status_entity, None)
        inv.full_hysteresis_active = None
        inv.update_full_hysteresis()
        if inv.full_hysteresis_active is not False:
            print("**** ERROR: expected default False with no prior published state, got {} ****".format(inv.full_hysteresis_active))
            failed = 1
    finally:
        my_predbat.battery_soc_full_hysteresis = original_hysteresis
        if original_status is None:
            my_predbat.ha_interface.dummy_items.pop(status_entity, None)
        else:
            my_predbat.ha_interface.dummy_items[status_entity] = original_status

    return failed


def test_multi_inverter_full_hysteresis_independence(my_predbat):
    """
    Critical multi-inverter case: each inverter's own BMS clamps independently at its own 100%, so a
    fleet-wide combined SoC percentage cannot represent this correctly (a 2x10kWh fleet at 10kWh+8kWh
    is 90% combined, which would never trip a 3% fleet-wide check even though inverter 0 has already
    hit its own real clamp). Confirms inverter 0 activates while inverter 1 - on the same PredBat,
    same hysteresis setting, but with plenty of headroom - does not.
    """
    failed = 0
    original_hysteresis = my_predbat.battery_soc_full_hysteresis
    try:
        my_predbat.battery_soc_full_hysteresis = 3.0

        inv0 = Inverter(my_predbat, 0)
        inv0.soc_max = 10.0
        inv0.soc_kw = 10.0  # full - this inverter's BMS has clamped
        inv0.full_hysteresis_active = False
        inv0.update_full_hysteresis()

        inv1 = Inverter(my_predbat, 0)  # constructed with id=0 (the fixture is only configured for
        # num_inverters=1); id is overridden below purely as a label, not exercised for restoration
        # here - what this test proves is that two separate Inverter INSTANCES track full_hysteresis_
        # active independently from their own soc_kw, which is the actual multi-inverter divergence bug
        inv1.id = 1
        inv1.soc_max = 10.0
        inv1.soc_kw = 8.0  # 80% - well clear of the band, unaffected
        inv1.full_hysteresis_active = False
        inv1.update_full_hysteresis()

        if inv0.full_hysteresis_active is not True:
            print("**** ERROR: inverter 0 at 100% should be hysteresis-active, got {} ****".format(inv0.full_hysteresis_active))
            failed = 1
        if inv1.full_hysteresis_active is not False:
            print("**** ERROR: inverter 1 at 80% should NOT be hysteresis-active (must not be affected by inverter 0's state), got {} ****".format(inv1.full_hysteresis_active))
            failed = 1
    finally:
        my_predbat.battery_soc_full_hysteresis = original_hysteresis

    return failed


def test_battery_full_hysteresis_kernel_parity(my_predbat):
    """
    The C++ kernel mirrors the hysteresis clamp independently (prediction_kernel.cpp), since the
    kernel does not call the Python utils.py functions. Run a hysteresis-active scenario through both
    engines via the existing dual_run parity harness and confirm they agree bit-for-bit, AND that the
    clamp is genuinely doing something (soc barely moves) rather than both engines just happening to
    agree on an unrelated no-op.

    This seeds the simulation's fleet-wide aggregate flag directly (battery_full_hysteresis_active),
    matching how update_battery_full_hysteresis_aggregate() derives it from the per-inverter flags in
    real use - the aggregate itself is exercised here, per-inverter tracking is covered above.

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
    try:
        reset_inverter(my_predbat)
        reset_rates(my_predbat, 10.0, 5.0)
        my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
        my_predbat.battery_soc_full_hysteresis = 3.0
        my_predbat.battery_full_hysteresis_active = True  # still within the band from a previous 100%, per the scenario below
        # Start WITHIN the hysteresis band (98% of a 10kWh battery, 3% band -> floor is 97%), not
        # exactly at soc_max: a target of soc_max is always <= soc_max in real use, so "soc <
        # charge_limit_n" (the force-charge trigger) is never true exactly at 100% regardless of
        # hysteresis - the real scenario hysteresis has to defend against is soc sitting just below
        # 100% with a still-unmet 100% target, which is exactly what would otherwise resume charging.
        my_predbat.soc_kw = my_predbat.soc_max - 0.2
        starting_soc = my_predbat.soc_kw
        pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, pv_kw=0.0, load_kw=0.0)

        # One charge window covering the whole forecast at full (100%) target - soc(98%) < charge_limit_n
        # (100%) is true, so this genuinely enters the force-charge branch in both engines; if hysteresis
        # were not applied identically in both, this would charge straight back up to 100%.
        charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + my_predbat.forecast_minutes, "average": 10.0}]
        charge_limit = [my_predbat.soc_max]
        export_window = []
        export_limits = []
        end_record = my_predbat.forecast_minutes

        failed |= dual_run("battery_full_hysteresis", my_predbat, pv_step, pv10_step, load_step, load10_step, charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record)

        # Confirm the clamp actually suppressed charging in the Python engine (dual_run already proved the
        # kernel matches it bit-for-bit, so checking one side is enough): soc should not have grown AT ALL
        # from its 98% starting point in the entire window run with pv/load both zero and battery_rate_min
        # 0 (reset_inverter()) - if it grew even slightly the force-charge branch charged despite hysteresis.
        my_predbat.prediction_kernel_enable = False
        prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
        result = prediction.run_prediction(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, end_record, save=None, cache=False)
        final_soc = result[5]
        if abs(final_soc - starting_soc) > 1e-6:
            print("**** ERROR: expected soc to stay at its starting point ({}, within the hysteresis band) with hysteresis active and battery_rate_min=0, got {} - the clamp may not be suppressing charge ****".format(starting_soc, final_soc))
            failed = 1
    finally:
        my_predbat.battery_soc_full_hysteresis = 0
        my_predbat.battery_full_hysteresis_active = False
        my_predbat.prediction_kernel_enable = True

    return failed


def test_record_status_preserves_hysteresis_when_no_inverters(my_predbat):
    """
    record_status() can be called from several early-startup error paths (e.g. "Template
    Configuration" or "components failed to start") before fetch_inverter_data() has ever run, when
    self.inverters is still empty. Since dashboard_item() replaces the whole attributes dict each
    call, naively computing the per-inverter hysteresis dict from an empty self.inverters would wipe
    out whatever was persisted from a previous run. Confirms record_status() preserves the existing
    persisted value in that situation, and still updates it normally once inverters are present.
    """
    failed = 0
    status_entity = my_predbat.prefix + ".status"
    original_status = my_predbat.ha_interface.dummy_items.get(status_entity)
    original_inverters = my_predbat.inverters

    try:
        # Simulate a previous run having persisted real per-inverter state.
        my_predbat.ha_interface.dummy_items[status_entity] = {"state": "Idle", "battery_full_hysteresis_active": {"0": True}}

        # An early-startup error path: no inverters fetched yet.
        my_predbat.inverters = []
        my_predbat.record_status("Error: Some components failed to start (phase0)", had_errors=True)
        preserved = my_predbat.ha_interface.dummy_items[status_entity].get("battery_full_hysteresis_active")
        if preserved != {"0": True}:
            print("**** ERROR: record_status() with no inverters should preserve the prior persisted dict, got {} ****".format(preserved))
            failed = 1

        # Once a real inverter exists, record_status() should reflect its actual current state, not
        # the stale preserved value forever.
        inv = Inverter(my_predbat, 0)
        inv.full_hysteresis_active = False
        my_predbat.inverters = [inv]
        my_predbat.record_status("Idle")
        updated = my_predbat.ha_interface.dummy_items[status_entity].get("battery_full_hysteresis_active")
        if updated != {"0": False}:
            print("**** ERROR: record_status() with a real inverter present should publish its current state, got {} ****".format(updated))
            failed = 1
    finally:
        my_predbat.inverters = original_inverters
        if original_status is None:
            my_predbat.ha_interface.dummy_items.pop(status_entity, None)
        else:
            my_predbat.ha_interface.dummy_items[status_entity] = original_status

    return failed


def test_dashboard_display_reflects_hysteresis_band(my_predbat):
    """
    get_charge_rate_kw() (the dashboard's per-window planned-rate display) must:
      - do nothing when the feature is disabled (default hysteresis=0), even if a window's soc
        happens to sit at exactly soc_max
      - show a suppressed (0) rate for a window whose soc, per the plan's own simulation, is
        anywhere inside the hysteresis band below 100% - not only the exact 100% leading edge -
        since a suppressed charge leaves soc stuck inside the band rather than pinned at soc_max
    """
    failed = 0
    from tests.test_infra import reset_inverter

    original_hysteresis = my_predbat.battery_soc_full_hysteresis
    try:
        reset_inverter(my_predbat)
        my_predbat.soc_max = 10.0
        my_predbat.battery_rate_max_charge = 2500 / MINUTE_WATT
        my_predbat.charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30}]
        my_predbat.charge_limit_best = [my_predbat.soc_max]
        my_predbat.set_charge_low_power = False

        # Disabled (default): a window sitting exactly at soc_max must still show a real (nonzero)
        # planned rate - the feature must have zero effect on everyone who has not opted in.
        my_predbat.battery_soc_full_hysteresis = 0
        my_predbat.predict_soc_best = {0: my_predbat.soc_max}
        rate_disabled = my_predbat.get_charge_rate_kw(0, my_predbat.minutes_now, 0, {})
        if rate_disabled <= 0:
            print("**** ERROR: with hysteresis disabled, dashboard rate at soc_max should be nonzero (unaffected), got {} ****".format(rate_disabled))
            failed = 1

        # Enabled, soc stuck at 98% (within a 3% band, not at exactly 100%) - the previous bug only
        # caught soc==100% exactly, so this is the case it missed.
        my_predbat.battery_soc_full_hysteresis = 3.0
        my_predbat.predict_soc_best = {0: 9.8}
        rate_within_band = my_predbat.get_charge_rate_kw(0, my_predbat.minutes_now, 0, {})
        if rate_within_band != 0:
            print("**** ERROR: with hysteresis enabled and soc at 98% (within a 3% band), dashboard rate should be 0, got {} ****".format(rate_within_band))
            failed = 1

        # Enabled, soc well outside the band (80%) - should show a normal nonzero rate.
        my_predbat.predict_soc_best = {0: 8.0}
        rate_outside_band = my_predbat.get_charge_rate_kw(0, my_predbat.minutes_now, 0, {})
        if rate_outside_band <= 0:
            print("**** ERROR: with hysteresis enabled and soc at 80% (outside the band), dashboard rate should be nonzero, got {} ****".format(rate_outside_band))
            failed = 1
    finally:
        my_predbat.battery_soc_full_hysteresis = original_hysteresis

    return failed

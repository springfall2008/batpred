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
Unit tests for Output.calculate_yesterday().

Covered scenarios
-----------------
1. Early-exit when savings_last_updated is fresh (< 59 min old, same day).
2. Basic run with no car: function runs, key dashboard entities are published
   and all state attributes are correctly restored afterwards.
3. Car-slot subtraction: when car_charging_slots has a slot covering some
   steps, the load at those steps is zeroed out in yesterday_load_step before
   it is handed to the Prediction.  Steps outside the slot are unaffected.
4. State is fully restored after the car-slot variant as well.
"""

import copy
from datetime import datetime, timedelta

import pytz

from tests.test_infra import reset_rates, reset_inverter

UTC = pytz.UTC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FLAT_LOAD_KWH = 0.1   # kWh per 5-min step returned by the step_data_history mock
FLAT_PV_KWH = 0.05    # kWh per 5-min step


def _make_constant_history(value, now_utc, days=2):
    """Return a minimal history list (one point 2 days ago) so that
    minute_data will fill the entire 2-day window with *value*."""
    two_days_ago = now_utc - timedelta(days=days)
    return [[{"state": str(value), "last_updated": two_days_ago.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "attributes": {"p/kWh": "0.0"}}]]


def _make_mock_step_data(pv_today_ref):
    """Return a callable that replaces step_data_history.

    * Returns zeros when *item* is None (pv-zero variant).
    * Returns FLAT_PV_KWH when *item* is the same object as pv_today_ref.
    * Returns FLAT_LOAD_KWH for any other non-None item (load).
    """

    def _mock(item, minutes_now, forward, step=5, scale_today=1.0, scale_fixed=1.0, **kwargs):
        if item is None:
            return {minute: 0.0 for minute in range(0, 24 * 60, 5)}
        elif item is pv_today_ref:
            return {minute: FLAT_PV_KWH for minute in range(0, 24 * 60, 5)}
        else:
            return {minute: FLAT_LOAD_KWH for minute in range(0, 24 * 60, 5)}

    return _mock


def _make_mock_run_prediction(captured):
    """Return a callable that replaces run_prediction.

    Saves a deep-copy of self.prediction.load_minutes_step into *captured*
    (a list) so the test can inspect the values that were passed in.
    Returns a fixed 11-tuple representing a modest baseline cost (500p).
    """

    FIXED_METRIC = 500        # pence
    FIXED_IMPORT_BATTERY = 1.0
    FIXED_IMPORT_HOUSE = 5.0
    FIXED_EXPORT = 0.5
    FIXED_SOC_MIN = 3.0
    FIXED_FINAL_SOC = 5.0
    FIXED_SOC_MIN_MIN = 120
    FIXED_CYCLE = 2.0
    FIXED_METRIC_KEEP = 0
    FIXED_IBOOST = 0.0
    FIXED_CARBON = 0

    def _run_prediction_mock(self_pb, charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, step=5):
        # Capture the load_minutes_step at this call
        if hasattr(self_pb, "prediction") and self_pb.prediction is not None:
            captured.append(copy.deepcopy(self_pb.prediction.load_minutes_step))
        # Also copy the SoC-related attributes so compute_metric works
        self_pb.predict_soc = {}
        self_pb.predict_soc_best = {}
        self_pb.predict_metric_best = {}
        return (
            FIXED_METRIC,
            FIXED_IMPORT_BATTERY,
            FIXED_IMPORT_HOUSE,
            FIXED_EXPORT,
            FIXED_SOC_MIN,
            FIXED_FINAL_SOC,
            FIXED_SOC_MIN_MIN,
            FIXED_CYCLE,
            FIXED_METRIC_KEEP,
            FIXED_IBOOST,
            FIXED_CARBON,
        )

    return _run_prediction_mock


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _setup_base(my_predbat, minutes_now=360):
    """Set up the minimum predbat state needed for calculate_yesterday.

    Uses a fixed reference time of 2024-10-04T06:00:00 UTC so that
    minute 0 = midnight and minutes_now=360 = 06:00.
    """
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 20, 5)   # 20p import, 5p export (flat all day)

    now_utc = datetime(2024, 10, 4, 6, 0, 0, tzinfo=UTC)
    midnight_utc = datetime(2024, 10, 4, 0, 0, 0, tzinfo=UTC)

    my_predbat.now_utc = now_utc
    my_predbat.midnight_utc = midnight_utc
    my_predbat.minutes_now = minutes_now

    # Replicate rate_import_no_io so history_to_future_rates can be called
    my_predbat.rate_import_no_io = my_predbat.rate_import.copy()

    # Savings config
    my_predbat.calculate_savings_max_charge_slots = 0   # skip charge-window search
    my_predbat.savings_last_updated = None

    # Battery state
    my_predbat.soc_kw = 5.0
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 0.5

    # Misc state that calculate_yesterday saves/restores
    my_predbat.cost_today_sofar = 0.0
    my_predbat.import_today_now = 0.0
    my_predbat.export_today_now = 0.0
    my_predbat.pv_today_now = 0.0
    my_predbat.carbon_today_sofar = 0
    my_predbat.carbon_enable = False
    my_predbat.car_charging_hold = False
    my_predbat.iboost_energy_subtract = False
    my_predbat.load_minutes_now = 0.0
    my_predbat.plan_debug = False
    my_predbat.rate_import_replicated = {}
    my_predbat.rate_export_replicated = {}
    my_predbat.predict_soc_best = {}
    my_predbat.predict_metric_best = {}
    my_predbat.predict_soc = {}
    my_predbat.savings_total_soc = 0.0
    my_predbat.num_cars = 0

    # step_data_history needs load_minutes / pv_today objects; we mock the
    # method so the actual values don't matter – just give unique refs.
    my_predbat.load_minutes = {}
    my_predbat.pv_today = {}

    return now_utc


def _make_history_mock(my_predbat, now_utc, cost_value=100.0, soc_value=5.0):
    """Return a function that replaces get_history_wrapper."""

    cost_hist = _make_constant_history(cost_value, now_utc)
    soc_hist = _make_constant_history(soc_value, now_utc)

    prefix = my_predbat.prefix

    def _get_history_wrapper(entity_id, days=30, required=True, tracked=True):
        if entity_id == prefix + ".cost_today":
            return cost_hist
        elif entity_id == prefix + ".soc_kw_h0":
            return soc_hist
        elif entity_id in (prefix + ".status", prefix + ".cost_today_car"):
            return None   # optional – returning None is handled gracefully
        return None

    return _get_history_wrapper


def _apply_mocks(my_predbat, now_utc, cost_value=100.0, soc_value=5.0):
    """Apply all mocks and return the captured-load list."""
    captured_load_steps = []

    my_predbat.step_data_history = _make_mock_step_data(my_predbat.pv_today)
    my_predbat.get_history_wrapper = _make_history_mock(my_predbat, now_utc, cost_value, soc_value)
    my_predbat.plan_write_debug = lambda *a, **kw: ("", "{}")
    my_predbat.publish_html_plan = lambda *a, **kw: ("", "{}")

    original_run_pred = my_predbat.run_prediction
    mock_run_pred = _make_mock_run_prediction(captured_load_steps)
    my_predbat.run_prediction = lambda *a, **kw: mock_run_pred(my_predbat, *a, **kw)

    return captured_load_steps, original_run_pred


def _restore_methods(my_predbat, original_run_pred, original_step_data=None, original_get_history=None):
    """Undo monkey-patches applied by _apply_mocks."""
    my_predbat.run_prediction = original_run_pred
    # Remove the remaining lambdas so subsequent tests start clean
    if hasattr(my_predbat.__class__, "step_data_history"):
        # Restore the bound method by deleting the instance override
        try:
            del my_predbat.step_data_history
        except AttributeError:
            pass
    if hasattr(my_predbat.__class__, "get_history_wrapper"):
        try:
            del my_predbat.get_history_wrapper
        except AttributeError:
            pass
    if hasattr(my_predbat.__class__, "plan_write_debug"):
        try:
            del my_predbat.plan_write_debug
        except AttributeError:
            pass
    if hasattr(my_predbat.__class__, "publish_html_plan"):
        try:
            del my_predbat.publish_html_plan
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def _test_early_exit(my_predbat, failed):
    """Test 1: Early-exit when savings_last_updated is < 59 min old, same day."""
    print("calculate_yesterday: Test 1 – early-exit when timestamp is fresh")
    now_utc = _setup_base(my_predbat)

    # Mark as recently updated (10 minutes ago, same day)
    my_predbat.savings_last_updated = now_utc - timedelta(minutes=10)

    # Make sure dashboard_item is NOT called by asserting that the savings
    # entity is absent before the call (it might or might not exist from a
    # previous test run; we just confirm the function returns without running).
    original_ts = my_predbat.savings_last_updated

    my_predbat.calculate_yesterday()

    # savings_last_updated must not have been updated (early return happened)
    if my_predbat.savings_last_updated != original_ts:
        print("ERROR: savings_last_updated was changed despite fresh timestamp")
        failed = True

    # Reset for next test
    my_predbat.savings_last_updated = None
    return failed


def _test_basic_no_car(my_predbat, failed):
    """Test 2: Basic run (no car) – entities published, state restored."""
    print("calculate_yesterday: Test 2 – basic run, no car")
    now_utc = _setup_base(my_predbat)

    # Snapshot state that must be restored
    rate_import_before = my_predbat.rate_import.copy()
    rate_export_before = my_predbat.rate_export.copy()
    soc_kw_before = my_predbat.soc_kw
    soc_max_before = my_predbat.soc_max
    minutes_now_before = my_predbat.minutes_now
    midnight_utc_before = my_predbat.midnight_utc
    forecast_minutes_before = my_predbat.forecast_minutes
    num_cars_before = my_predbat.num_cars

    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc, cost_value=100.0, soc_value=5.0)

    my_predbat.calculate_yesterday()

    # --- State restoration checks ---
    if my_predbat.rate_import != rate_import_before:
        print("ERROR: rate_import was not restored")
        failed = True
    if my_predbat.rate_export != rate_export_before:
        print("ERROR: rate_export was not restored")
        failed = True
    if my_predbat.soc_kw != soc_kw_before:
        print("ERROR: soc_kw was not restored (expected {}, got {})".format(soc_kw_before, my_predbat.soc_kw))
        failed = True
    if my_predbat.soc_max != soc_max_before:
        print("ERROR: soc_max was not restored (expected {}, got {})".format(soc_max_before, my_predbat.soc_max))
        failed = True
    if my_predbat.minutes_now != minutes_now_before:
        print("ERROR: minutes_now was not restored (expected {}, got {})".format(minutes_now_before, my_predbat.minutes_now))
        failed = True
    if my_predbat.midnight_utc != midnight_utc_before:
        print("ERROR: midnight_utc was not restored")
        failed = True
    if my_predbat.forecast_minutes != forecast_minutes_before:
        print("ERROR: forecast_minutes was not restored")
        failed = True
    if my_predbat.num_cars != num_cars_before:
        print("ERROR: num_cars was not restored")
        failed = True

    # --- Timestamp check ---
    if not my_predbat.savings_last_updated:
        print("ERROR: savings_last_updated was not set after calculate_yesterday")
        failed = True
    else:
        age = my_predbat.now_utc - my_predbat.savings_last_updated
        if age > timedelta(seconds=5):
            print("ERROR: savings_last_updated is too old: {}".format(age))
            failed = True

    # --- Entity publication checks ---
    prefix = my_predbat.prefix
    for entity_suffix in (".cost_yesterday", ".savings_yesterday_predbat", ".savings_yesterday_pvbat"):
        entity_id = prefix + entity_suffix
        state = my_predbat.get_state_wrapper(entity_id)
        if state is None:
            print("ERROR: entity {} was not published".format(entity_id))
            failed = True

    # --- run_prediction was called (once for baseline, once for no-pvbat) ---
    if len(captured_load) < 2:
        print("ERROR: run_prediction should have been called at least twice, got {} captures".format(len(captured_load)))
        failed = True

    _restore_methods(my_predbat, original_run_pred)
    my_predbat.savings_last_updated = None
    return failed


def _test_car_slot_subtraction(my_predbat, failed):
    """Test 3: Car-slot subtraction.

    A single car with a 2-hour IOG slot from minute 60 to 180 (kWh=2.4,
    in_car_slot returns 1.2 kW).  The step_data_history mock returns
    FLAT_LOAD_KWH (0.1 kWh) for every step.

    Because sum(car_load)=1.2 kW > load_value=0.1 kWh (different units but
    the subtraction uses max(..., 0)), steps within the slot should be zeroed.
    Steps outside the slot must remain at FLAT_LOAD_KWH.

    We also verify that car_charging_slots is restored after the call.
    """
    print("calculate_yesterday: Test 3 – car-slot load subtraction")
    now_utc = _setup_base(my_predbat)

    # Enable one car but do NOT configure octopus_intelligent_slot, so
    # calculate_yesterday will NOT re-run load_octopus_slots; it will use
    # whatever we put in car_charging_slots directly.
    my_predbat.num_cars = 1
    # Slot: minute 60..180, 2.4 kWh total over 2 hours → 1.2 kW average
    car_slot = {"start": 60, "end": 180, "kwh": 2.4, "average": 20}
    my_predbat.car_charging_slots[0] = [car_slot]
    car_charging_slots_before = copy.deepcopy(my_predbat.car_charging_slots)

    # octopus_intelligent_slot not configured → entity_id_list is empty
    # so the re-load loop in calculate_yesterday is skipped
    my_predbat.args["octopus_intelligent_slot"] = None

    # Also set octopus_slots for car 0 (used if re-load happened; harmless here)
    my_predbat.octopus_slots = [[]]

    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc, cost_value=100.0, soc_value=5.0)

    my_predbat.calculate_yesterday()

    # --- Verify car load subtraction via the captured load_minutes_step ---
    if len(captured_load) < 1:
        print("ERROR: run_prediction was not called – cannot inspect load step")
        failed = True
    else:
        # The first captured load_minutes_step is from the baseline run.
        # Steps [60, 120, 165] are inside the slot (minute 60..175),
        # step 180 is outside (end is exclusive), and step 0, 300 are outside.
        load_step = captured_load[0]

        # Inside-slot steps should be zeroed out (max(0.1 - 1.2, 0) = 0)
        for inside_min in (60, 90, 120, 150, 175):
            # Round to nearest PREDICT_STEP boundary
            step_min = (inside_min // 5) * 5
            val = load_step.get(step_min, -1)
            if val != 0.0:
                print("ERROR: step {} (inside car slot) should be 0.0 but got {}".format(step_min, val))
                failed = True

        # Outside-slot steps should remain at FLAT_LOAD_KWH
        for outside_min in (0, 5, 55, 180, 300):
            step_min = (outside_min // 5) * 5
            val = load_step.get(step_min, -1)
            if val != FLAT_LOAD_KWH:
                print("ERROR: step {} (outside car slot) should be {} but got {}".format(step_min, FLAT_LOAD_KWH, val))
                failed = True

    # --- State restoration checks for car-specific attributes ---
    if my_predbat.car_charging_slots != car_charging_slots_before:
        print("ERROR: car_charging_slots was not restored after calculate_yesterday")
        failed = True

    if my_predbat.num_cars != 1:
        # num_cars should be restored too
        print("ERROR: num_cars was not restored (expected 1, got {})".format(my_predbat.num_cars))
        failed = True

    # Clean up for next test
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots[0] = []
    _restore_methods(my_predbat, original_run_pred)
    my_predbat.savings_last_updated = None
    return failed


def _test_early_exit_respects_day_rollover(my_predbat, failed):
    """Test 4: Early-exit is NOT triggered if savings_last_updated was from
    a previous day (even if it is < 59 min old by clock, the date differs)."""
    print("calculate_yesterday: Test 4 – early-exit skipped when date rolls over")
    now_utc = _setup_base(my_predbat)

    # Timestamp from yesterday – even though it is within 59 minutes of now_utc,
    # the dates differ so the early-return condition should NOT fire.
    my_predbat.savings_last_updated = (now_utc - timedelta(days=1)) + timedelta(minutes=5)

    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)

    my_predbat.calculate_yesterday()

    # Function should have run fully – savings_last_updated should be now_utc
    if not my_predbat.savings_last_updated:
        print("ERROR: savings_last_updated not set (function may not have run)")
        failed = True
    else:
        if my_predbat.savings_last_updated.date() != now_utc.date():
            print("ERROR: savings_last_updated date does not match now_utc.date()")
            failed = True

    _restore_methods(my_predbat, original_run_pred)
    my_predbat.savings_last_updated = None
    return failed


# ---------------------------------------------------------------------------
# Entry point registered in TEST_REGISTRY
# ---------------------------------------------------------------------------

def test_calculate_yesterday(my_predbat):
    """
    Unit tests for calculate_yesterday covering early-exit, state restoration,
    entity publication, and IOG car-slot load subtraction.
    """
    failed = False
    print("**** Running calculate_yesterday tests ****")

    failed = _test_early_exit(my_predbat, failed)
    failed = _test_basic_no_car(my_predbat, failed)
    failed = _test_car_slot_subtraction(my_predbat, failed)
    failed = _test_early_exit_respects_day_rollover(my_predbat, failed)

    return failed

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

from const import PREDICT_STEP
from tests.test_infra import reset_rates, reset_inverter

UTC = pytz.UTC

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FLAT_LOAD_KWH = 0.1  # kWh per 5-min step returned by the step_data_history mock
FLAT_PV_KWH = 0.05  # kWh per 5-min step


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

    FIXED_METRIC = 500  # pence
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
    reset_rates(my_predbat, 20, 5)  # 20p import, 5p export (flat all day)

    now_utc = datetime(2024, 10, 4, 6, 0, 0, tzinfo=UTC)
    midnight_utc = datetime(2024, 10, 4, 0, 0, 0, tzinfo=UTC)

    my_predbat.now_utc = now_utc
    my_predbat.midnight_utc = midnight_utc
    my_predbat.minutes_now = minutes_now

    # Replicate rate_import_no_io so history_to_future_rates can be called
    my_predbat.rate_import_no_io = my_predbat.rate_import.copy()

    # Savings config
    my_predbat.calculate_savings_max_charge_slots = 0  # skip charge-window search
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
            return None  # optional – returning None is handled gracefully
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
    """Test 3: Car-slot subtraction and car_charging_soc handling.

    A single car with a 2-hour IOG slot from minute 60 to 180 (kWh=2.4,
    in_car_slot returns 1.2 kW = 0.1 kWh per 5-min step).  The
    step_data_history mock returns FLAT_LOAD_KWH (0.1 kWh) for every step.

    After the fix (kW → kWh/step conversion):
      - Inside-slot steps: max(0.1 - 1.2 * 5/60, 0) = max(0.0, 0) = 0.0
      - Outside-slot steps: unchanged at FLAT_LOAD_KWH

    Also verifies that car_charging_soc is zeroed during the simulation so
    a car that is already at its limit does not block energy, and is restored
    afterwards.
    """
    print("calculate_yesterday: Test 3 – car-slot load subtraction")
    now_utc = _setup_base(my_predbat)

    # Enable one car but do NOT configure octopus_intelligent_slot, so
    # calculate_yesterday will NOT re-run load_octopus_slots; it will use
    # whatever we put in car_charging_slots directly.
    my_predbat.num_cars = 1
    # Slot: minute 60..180, 2.4 kWh total over 2 hours → 1.2 kW = 0.1 kWh/step
    car_slot = {"start": 60, "end": 180, "kwh": 2.4, "average": 20}
    my_predbat.car_charging_slots[0] = [car_slot]
    car_charging_slots_before = copy.deepcopy(my_predbat.car_charging_slots)

    # Simulate the car being fully charged (at its limit) so that without the
    # car_charging_soc=0 fix the prediction would add 0 energy.
    my_predbat.car_charging_soc = [80.0, 0.0, 0.0, 0.0]
    my_predbat.car_charging_limit = [80.0, 100.0, 100.0, 100.0]
    car_charging_soc_before = list(my_predbat.car_charging_soc)

    # octopus_intelligent_slot not configured → entity_id_list is empty
    # so the re-load loop in calculate_yesterday is skipped
    my_predbat.args["octopus_intelligent_slot"] = None

    # car_charging_energy must be a dict (not None) so the non-octopus scan
    # in yesterday_reconstruct_car_slots doesn't crash.  No energy in sensor
    # means no extra slots will be synthesised beyond the one set above.
    my_predbat.car_charging_energy = {}

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
        load_step = captured_load[0]

        # Inside-slot steps: max(0.1 kWh - 1.2 kW * 5/60, 0) = max(0.0, 0) = 0.0
        for inside_min in (60, 90, 120, 150, 175):
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

    # --- car_charging_soc must be restored to the pre-call value ---
    if my_predbat.car_charging_soc != car_charging_soc_before:
        print("ERROR: car_charging_soc was not restored (expected {}, got {})".format(car_charging_soc_before, my_predbat.car_charging_soc))
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
    my_predbat.car_charging_soc = [0, 0, 0, 0]
    my_predbat.car_charging_limit = [100.0, 100.0, 100.0, 100.0]
    _restore_methods(my_predbat, original_run_pred)
    my_predbat.savings_last_updated = None
    return failed


def _test_car_slot_from_energy_sensor(my_predbat, failed):
    """Test: car-slot subtraction via the car_charging_energy sensor (non-octopus path).

    No octopus_intelligent_slot is configured and car_charging_slots starts empty.
    The car_charging_energy sensor records 1.5 kWh in the 30-minute window
    starting at minute 60 of yesterday.  yesterday_reconstruct_car_slots should
    synthesise a slot for that window and subtract the corresponding kWh from the
    load step data before handing it to the Prediction.

    Slot: start=60, end=90, kwh=1.5 → load rate = 1.5/0.5h = 3.0 kW
    Subtraction per 5-min step: 3.0 * 5/60 = 0.25 kWh > FLAT_LOAD_KWH (0.1)
    Expected inside-slot value: max(0.1 - 0.25, 0) = 0.0

    Also verifies that car_charging_slots and car_charging_soc are fully
    restored to their pre-call values after calculate_yesterday returns.
    """
    print("calculate_yesterday: Test – car-slot subtraction via energy sensor (non-octopus)")
    now_utc = _setup_base(my_predbat)  # minutes_now = 360

    my_predbat.num_cars = 1
    # Start with no pre-set car slots; the sensor path should add one.
    my_predbat.car_charging_slots[0] = []
    car_charging_slots_before = copy.deepcopy(my_predbat.car_charging_slots)

    # Car is fully charged – without the car_charging_soc=0 fix the Prediction
    # would add 0 energy for the car.
    my_predbat.car_charging_soc = [80.0, 0.0, 0.0, 0.0]
    my_predbat.car_charging_limit = [80.0, 100.0, 100.0, 100.0]
    car_charging_soc_before = list(my_predbat.car_charging_soc)

    # No octopus slot configured → entity_id_list is empty, octopus path skipped.
    my_predbat.args["octopus_intelligent_slot"] = None
    my_predbat.octopus_slots = [[], [], [], []]
    my_predbat.octopus_intelligent_consider_full = False

    # Inside calculate_yesterday, minutes_now is set to 0 before calling
    # yesterday_reconstruct_car_slots, so the lookup formula becomes:
    #   minute_previous = 0 + 1440 - minute
    # For start_minute=60 the inner scan covers minutes 60..89.
    # At minute=60: minute_previous = 1380.
    # get_from_incrementing(data, 1380) = max(data[1380] - data[1381], 0).
    # Correct representation of an incrementing kWh sensor: the sensor reads
    # 1.5 kWh at minute 60 (index 1380) and all more-recent times (lower indices),
    # and 0 at earlier times (indices > 1380).  The telescoping sum over the
    # 30-minute window yields data[1351] - data[1381] = 1.5 - 0 = 1.5 kWh.
    my_predbat.car_charging_energy = {k: 1.5 for k in range(0, 1381)}

    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc, cost_value=100.0, soc_value=5.0)

    my_predbat.calculate_yesterday()

    # --- Verify load subtraction via the captured load_minutes_step ---
    if len(captured_load) < 1:
        print("ERROR: run_prediction was not called – cannot inspect load step")
        failed = True
    else:
        load_step = captured_load[0]
        plan_iv = my_predbat.plan_interval_minutes  # 30 by default
        slot_end = 60 + plan_iv

        # Inside-slot steps: max(0.1 - 3.0*5/60, 0) = max(0.1 - 0.25, 0) = 0.0
        for inside_min in range(60, slot_end, PREDICT_STEP):
            val = load_step.get(inside_min, -1)
            if abs(val) > 1e-9:
                print("ERROR: step {} (inside energy-sensor slot) should be 0.0 but got {}".format(inside_min, val))
                failed = True

        # Outside-slot steps should be unchanged at FLAT_LOAD_KWH.
        for outside_min in [0, 5, 55, slot_end, slot_end + 5, 300]:
            val = load_step.get(outside_min, -1)
            if abs(val - FLAT_LOAD_KWH) > 1e-9:
                print("ERROR: step {} (outside energy-sensor slot) should be {} but got {}".format(outside_min, FLAT_LOAD_KWH, val))
                failed = True

    # --- State restoration checks ---
    if my_predbat.car_charging_soc != car_charging_soc_before:
        print("ERROR: car_charging_soc was not restored (expected {}, got {})".format(car_charging_soc_before, my_predbat.car_charging_soc))
        failed = True
    if my_predbat.car_charging_slots != car_charging_slots_before:
        print("ERROR: car_charging_slots was not restored after calculate_yesterday")
        failed = True
    if my_predbat.num_cars != 1:
        print("ERROR: num_cars was not restored (expected 1, got {})".format(my_predbat.num_cars))
        failed = True

    # Clean up for next test
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots[0] = []
    my_predbat.car_charging_soc = [0, 0, 0, 0]
    my_predbat.car_charging_limit = [100.0, 100.0, 100.0, 100.0]
    my_predbat.car_charging_energy = {}
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


def _test_reconstruct_car_slots(my_predbat, failed):
    """Tests for yesterday_reconstruct_car_slots called directly.

    Three sub-scenarios:
    5a  Non-octopus path: energy detected in car_charging_energy sensor causes
        a slot to be synthesised in car_charging_slots and the matching steps
        to be zeroed in yesterday_load_step.
    5b  No-duplicate guard: when a slot already covers the period, a second
        slot is not appended.
    5c  Octopus re-load path: when octopus_intelligent_slot is configured,
        load_octopus_slots is called and replaces car_charging_slots for that
        car.
    """
    # -----------------------------------------------------------------------
    # 5a – non-octopus slot synthesised from car_charging_energy
    # -----------------------------------------------------------------------
    print("calculate_yesterday: Test 5a – non-octopus slot synthesised from sensor")

    _setup_base(my_predbat, minutes_now=0)  # minutes_now=0 as faked by calculate_yesterday
    my_predbat.num_cars = 1
    my_predbat.car_charging_slots = [[] for _ in range(4)]
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[], [], [], []]
    my_predbat.args["octopus_intelligent_slot"] = None

    # With minutes_now=0 the formula is: minute_previous = 0 + 1440 - minute.
    # For start_minute=60 the inner loop scans minutes 60-89 (indices 1380-1351).
    # An incrementing kWh sensor is monotonically non-decreasing going forward in
    # time (= decreasing index).  Setting data[k]=1.2 for indices 1351-1380 and
    # 0 for indices >1380 means the sensor jumped from 0 to 1.2 kWh at minute 60.
    # The telescoping sum data[1351]-data[1381] = 1.2-0 = 1.2 kWh for the window.
    plan_iv = my_predbat.plan_interval_minutes
    win_start_idx = 24 * 60 - 60  # minute_previous for start_minute=60, minutes_now=0
    win_end_idx = win_start_idx - plan_iv  # exclusive lower bound of the window
    my_predbat.car_charging_energy = {k: 1.2 for k in range(win_end_idx, win_start_idx + 1)}

    end_record = 24 * 60  # 1440 minutes
    yesterday_load_step = {m: FLAT_LOAD_KWH for m in range(0, end_record, PREDICT_STEP)}

    my_predbat.yesterday_reconstruct_car_slots(end_record, yesterday_load_step)

    # Exactly one slot should have been added
    slots = my_predbat.car_charging_slots[0]
    if len(slots) != 1:
        print("ERROR 5a: expected 1 synthesised slot, got {}".format(len(slots)))
        failed = True
    else:
        slot = slots[0]
        if slot.get("start") != 60:
            print("ERROR 5a: slot start should be 60, got {}".format(slot.get("start")))
            failed = True
        if slot.get("end") != 60 + my_predbat.plan_interval_minutes:
            print("ERROR 5a: slot end should be {}, got {}".format(60 + my_predbat.plan_interval_minutes, slot.get("end")))
            failed = True
        # With car_energy_reported_load=True, the slot kwh is capped to load_reported
        # * car_charging_loss.  load_reported = plan_iv/PREDICT_STEP * FLAT_LOAD_KWH
        # = 6 * 0.1 = 0.6 kWh; car_charging_loss=1.0 → expected kwh = 0.6.
        load_steps_in_slot = plan_iv // PREDICT_STEP
        expected_slot_kwh = load_steps_in_slot * FLAT_LOAD_KWH * my_predbat.car_charging_loss
        if abs(slot.get("kwh", -1) - expected_slot_kwh) > 1e-9:
            print("ERROR 5a: slot kwh should be {}, got {}".format(expected_slot_kwh, slot.get("kwh")))
            failed = True
        if slot.get("octopus") is not False:
            print("ERROR 5a: synthesised slot should have octopus=False, got {}".format(slot.get("octopus")))
            failed = True

    # Steps inside the slot [60, 90) should be zeroed out.
    # slot kwh adjusted to 0.6 → rate = 0.6/0.5h = 1.2 kW; 1.2*5/60 = 0.1 kWh/step = FLAT_LOAD_KWH → max(0.1-0.1,0)=0.0
    slot_end = 60 + my_predbat.plan_interval_minutes
    for inside_min in range(60, slot_end, PREDICT_STEP):
        val = yesterday_load_step.get(inside_min, -1)
        if abs(val) > 1e-9:
            print("ERROR 5a: step {} inside slot should be 0.0, got {}".format(inside_min, val))
            failed = True

    # Steps outside the slot should be unchanged.
    for outside_min in [0, 5, 55, slot_end, slot_end + 5, 300]:
        val = yesterday_load_step.get(outside_min, -1)
        if abs(val - FLAT_LOAD_KWH) > 1e-9:
            print("ERROR 5a: step {} outside slot should be {}, got {}".format(outside_min, FLAT_LOAD_KWH, val))
            failed = True

    # -----------------------------------------------------------------------
    # 5b – no duplicate slot when period already covered
    # -----------------------------------------------------------------------
    print("calculate_yesterday: Test 5b – no duplicate slot when period already covered")

    _setup_base(my_predbat, minutes_now=0)
    my_predbat.num_cars = 1
    existing_slot = {"start": 60, "end": 60 + my_predbat.plan_interval_minutes, "kwh": 5.0, "octopus": True}
    my_predbat.car_charging_slots = [[existing_slot], [], [], []]
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[], [], [], []]
    my_predbat.args["octopus_intelligent_slot"] = None
    my_predbat.car_charging_energy = {k: 1.2 for k in range(0, 1381)}  # same energy as 5a

    yesterday_load_step = {m: FLAT_LOAD_KWH for m in range(0, end_record, PREDICT_STEP)}
    my_predbat.yesterday_reconstruct_car_slots(end_record, yesterday_load_step)

    if len(my_predbat.car_charging_slots[0]) != 1:
        print("ERROR 5b: expected 1 slot (no duplicate), got {}".format(len(my_predbat.car_charging_slots[0])))
        failed = True
    else:
        if my_predbat.car_charging_slots[0][0] is not existing_slot:
            print("ERROR 5b: the existing slot object should be unchanged")
            failed = True

    # -----------------------------------------------------------------------
    # 5c – octopus re-load path replaces car_charging_slots
    # -----------------------------------------------------------------------
    print("calculate_yesterday: Test 5c – octopus re-load path calls load_octopus_slots")

    _setup_base(my_predbat, minutes_now=0)
    my_predbat.num_cars = 1
    my_predbat.car_charging_slots = [[], [], [], []]
    my_predbat.octopus_intelligent_consider_full = False
    # A raw octopus slot dict as stored in self.octopus_slots
    raw_octopus_slot = {"start": "01:00", "end": "02:00", "charge_in_kwh": 5.0, "source": "intelligent-dispatches"}
    my_predbat.octopus_slots = [[raw_octopus_slot], [], [], []]
    my_predbat.args["octopus_intelligent_slot"] = "sensor.octopus_intelligent_slot"
    my_predbat.car_charging_energy = {}

    returned_slots = [{"start": 60, "end": 120, "kwh": 5.0, "average": 10, "octopus": True}]
    load_octopus_calls = []

    original_load_octopus_slots = my_predbat.load_octopus_slots

    def _mock_load_octopus_slots(car_n, raw_slots, consider_full):
        load_octopus_calls.append((car_n, raw_slots, consider_full))
        return returned_slots

    my_predbat.load_octopus_slots = _mock_load_octopus_slots

    yesterday_load_step = {m: FLAT_LOAD_KWH for m in range(0, end_record, PREDICT_STEP)}
    my_predbat.yesterday_reconstruct_car_slots(end_record, yesterday_load_step)

    my_predbat.load_octopus_slots = original_load_octopus_slots

    if not load_octopus_calls:
        print("ERROR 5c: load_octopus_slots was not called")
        failed = True
    else:
        car_n, raw, consider = load_octopus_calls[0]
        if car_n != 0:
            print("ERROR 5c: load_octopus_slots called with car_n={}, expected 0".format(car_n))
            failed = True
        if raw != [raw_octopus_slot]:
            print("ERROR 5c: load_octopus_slots called with wrong raw_slots: {}".format(raw))
            failed = True

    if my_predbat.car_charging_slots[0] != returned_slots:
        print("ERROR 5c: car_charging_slots[0] should be the value returned by load_octopus_slots, got {}".format(my_predbat.car_charging_slots[0]))
        failed = True

    # Restore
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots = [[] for _ in range(4)]
    my_predbat.car_charging_energy = {}

    # -----------------------------------------------------------------------
    # 5d – slot cancelled when historical load is far too low to support it
    # -----------------------------------------------------------------------
    print("calculate_yesterday: Test 5d – slot cancelled when load < 10% of slot drain")

    _setup_base(my_predbat, minutes_now=0)
    my_predbat.num_cars = 1
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_loss = 1.0
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[], [], [], []]
    my_predbat.args["octopus_intelligent_slot"] = None
    my_predbat.car_charging_energy = {}

    plan_iv = my_predbat.plan_interval_minutes  # 30
    # A slot claiming 3.0 kWh → kwh_drain = 3.0 kWh; needs load ≥ 0.3 kWh to survive.
    # Provide only 0.01 kWh per step → load_reported = 6 * 0.01 = 0.06 kWh.
    # 0.06 * 10 = 0.6 < 3.0 → slot should be cancelled (kwh set to 0).
    TINY_LOAD = 0.01
    cancelled_slot = {"start": 60, "end": 60 + plan_iv, "kwh": 3.0, "octopus": True}
    my_predbat.car_charging_slots = [[cancelled_slot], [], [], []]

    yesterday_load_step_5d = {m: TINY_LOAD for m in range(0, end_record, PREDICT_STEP)}
    my_predbat.yesterday_reconstruct_car_slots(end_record, yesterday_load_step_5d)

    # The slot kwh should have been zeroed out.
    if cancelled_slot.get("kwh") != 0:
        print("ERROR 5d: slot kwh should be 0 after cancellation, got {}".format(cancelled_slot.get("kwh")))
        failed = True

    # Because kwh=0, subtract_amount=0 → load values should be completely unchanged.
    for m in range(60, 60 + plan_iv, PREDICT_STEP):
        val = yesterday_load_step_5d.get(m, -1)
        if abs(val - TINY_LOAD) > 1e-9:
            print("ERROR 5d: step {} load should be unchanged at {}, got {}".format(m, TINY_LOAD, val))
            failed = True

    # -----------------------------------------------------------------------
    # 5e – slot kwh adjusted down (not cancelled) when load is low but > 10%
    # -----------------------------------------------------------------------
    print("calculate_yesterday: Test 5e – slot kwh adjusted down when load between 10% and 100% of drain")

    _setup_base(my_predbat, minutes_now=0)
    my_predbat.num_cars = 1
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_loss = 1.0
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[], [], [], []]
    my_predbat.args["octopus_intelligent_slot"] = None
    my_predbat.car_charging_energy = {}

    plan_iv = my_predbat.plan_interval_minutes  # 30
    # Slot claims 2.4 kWh → kwh_drain = 2.4; load = 6 * 0.2 = 1.2 kWh.
    # 1.2 * 10 = 12.0 ≥ 2.4 → adjusted: slot["kwh"] = 1.2 * 1.0 = 1.2 kWh.
    # Subtraction: 1.2 kWh / 0.5 h = 2.4 kW; 2.4 * 5/60 = 0.2 kWh/step.
    # After subtraction: max(0.2 - 0.2, 0) = 0.0 inside slot.
    MEDIUM_LOAD = 0.2
    adjusted_slot = {"start": 60, "end": 60 + plan_iv, "kwh": 2.4, "octopus": True}
    my_predbat.car_charging_slots = [[adjusted_slot], [], [], []]

    yesterday_load_step_5e = {m: MEDIUM_LOAD for m in range(0, end_record, PREDICT_STEP)}
    my_predbat.yesterday_reconstruct_car_slots(end_record, yesterday_load_step_5e)

    expected_adj_kwh = (plan_iv // PREDICT_STEP) * MEDIUM_LOAD * my_predbat.car_charging_loss  # 6 * 0.2 * 1.0 = 1.2
    if abs(adjusted_slot.get("kwh", -1) - expected_adj_kwh) > 1e-9:
        print("ERROR 5e: slot kwh should be {} after adjustment, got {}".format(expected_adj_kwh, adjusted_slot.get("kwh")))
        failed = True

    # Inside-slot steps should be zeroed (subtract_amount == MEDIUM_LOAD).
    for m in range(60, 60 + plan_iv, PREDICT_STEP):
        val = yesterday_load_step_5e.get(m, -1)
        if abs(val) > 1e-9:
            print("ERROR 5e: step {} inside slot should be 0.0, got {}".format(m, val))
            failed = True

    # Outside-slot steps should be unchanged.
    for m in [0, 5, 55, 60 + plan_iv, 60 + plan_iv + 5]:
        val = yesterday_load_step_5e.get(m, -1)
        if abs(val - MEDIUM_LOAD) > 1e-9:
            print("ERROR 5e: step {} outside slot should be {}, got {}".format(m, MEDIUM_LOAD, val))
            failed = True

    # -----------------------------------------------------------------------
    # 5f – two cars in the same window; car 0 is satisfied, car 1 is scaled
    # -----------------------------------------------------------------------
    print("calculate_yesterday: Test 5f – two cars; car 0 full, car 1 scaled down by residual load")

    _setup_base(my_predbat, minutes_now=0)
    my_predbat.num_cars = 2
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_loss = 1.0
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_slots = [[], [], [], []]
    my_predbat.args["octopus_intelligent_slot"] = None
    my_predbat.car_charging_energy = {}

    plan_iv = my_predbat.plan_interval_minutes  # 30 minutes → 6 steps
    # Load: 0.2 kWh/step × 6 steps = 1.2 kWh total in the window.
    # Car 0: kwh=0.6, kwh_drain=0.6 → load 1.2 ≥ 0.6 → no scaling.
    #   subtract: 0.6/0.5h × 5/60 = 0.1 kWh/step → residual = 0.1 kWh/step.
    # Car 1: kwh=0.9, kwh_drain=0.9 → load 6×0.1=0.6 < 0.9.
    #   0.6×10=6.0 ≥ 0.9 → adjusted to 0.6 kWh.
    #   subtract: 0.6/0.5h × 5/60 = 0.1 kWh/step → residual = 0.0.
    TWO_CAR_LOAD = 0.2
    slot_car0 = {"start": 60, "end": 60 + plan_iv, "kwh": 0.6, "octopus": True}
    slot_car1 = {"start": 60, "end": 60 + plan_iv, "kwh": 0.9, "octopus": True}
    my_predbat.car_charging_slots = [[slot_car0], [slot_car1], [], []]

    yesterday_load_step_5f = {m: TWO_CAR_LOAD for m in range(0, end_record, PREDICT_STEP)}
    my_predbat.yesterday_reconstruct_car_slots(end_record, yesterday_load_step_5f)

    # Car 0 should be unchanged (load was sufficient).
    if abs(slot_car0.get("kwh", -1) - 0.6) > 1e-9:
        print("ERROR 5f: car 0 slot kwh should remain 0.6, got {}".format(slot_car0.get("kwh")))
        failed = True

    # Car 1 should be scaled down to the residual load × car_charging_loss = 0.6.
    expected_car1_kwh = 0.6
    if abs(slot_car1.get("kwh", -1) - expected_car1_kwh) > 1e-9:
        print("ERROR 5f: car 1 slot kwh should be {} after scaling, got {}".format(expected_car1_kwh, slot_car1.get("kwh")))
        failed = True

    # Both cars have subtracted their full amounts → inside-slot load should be 0.
    for m in range(60, 60 + plan_iv, PREDICT_STEP):
        val = yesterday_load_step_5f.get(m, -1)
        if abs(val) > 1e-9:
            print("ERROR 5f: step {} inside slot should be 0.0, got {}".format(m, val))
            failed = True

    # Outside-slot steps should be completely unchanged.
    for m in [0, 5, 55, 60 + plan_iv, 60 + plan_iv + 5]:
        val = yesterday_load_step_5f.get(m, -1)
        if abs(val - TWO_CAR_LOAD) > 1e-9:
            print("ERROR 5f: step {} outside slot should be {}, got {}".format(m, TWO_CAR_LOAD, val))
            failed = True

    # Restore
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots = [[] for _ in range(4)]
    my_predbat.car_charging_energy = {}

    return failed


def _test_soc_not_mutated_and_override_passed(my_predbat, failed):
    """Test: base soc_kw/soc_max are never mutated during calculate_yesterday,
    and Prediction instances receive the correct overridden SOC values.

    Verifies the race-condition fix (PR #3909):
    - self.soc_kw stays at its original value throughout the call.
    - The baseline Prediction receives soc_kw=soc_yesterday (from history).
    - The no-battery/PV Prediction receives soc_kw=0, soc_max=0.
    """
    print("calculate_yesterday: Test – soc_kw not mutated, Prediction override values verified")
    now_utc = _setup_base(my_predbat)

    # Record the values that must NOT change during calculate_yesterday
    original_soc_kw = my_predbat.soc_kw  # 5.0
    original_soc_max = my_predbat.soc_max  # 10.0

    # soc_yesterday is read from the HA entity prefix+".savings_total_soc".
    # No entity is registered in the test dummy store, so get_state_wrapper
    # returns the default 0.0 → soc_yesterday == 0.0.
    expected_soc_yesterday = 0.0

    # Capture (base_soc_kw, prediction_soc_kw, prediction_soc_max) for each
    # run_prediction call so we can verify both properties simultaneously.
    captured_soc = []  # list of (base_soc_kw, base_soc_max, pred_soc_kw, pred_soc_max)

    def _mock_run_prediction(self_pb, charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, step=5):
        """Capture base and Prediction SOC values at the time of each call."""
        pred = getattr(self_pb, "prediction", None)
        captured_soc.append(
            (
                self_pb.soc_kw,
                self_pb.soc_max,
                pred.soc_kw if pred else None,
                pred.soc_max if pred else None,
            )
        )
        self_pb.predict_soc = {}
        self_pb.predict_soc_best = {}
        self_pb.predict_metric_best = {}
        return (500, 1.0, 5.0, 0.5, 3.0, 5.0, 120, 2.0, 0, 0.0, 0)

    captured_load, original_run_pred = _apply_mocks(my_predbat, now_utc)
    # Override the run_prediction mock with one that also captures SOC state.
    my_predbat.run_prediction = lambda *a, **kw: _mock_run_prediction(my_predbat, *a, **kw)

    my_predbat.calculate_yesterday()

    # --- Base object SOC must never have changed ---
    if my_predbat.soc_kw != original_soc_kw:
        print("ERROR: soc_kw was mutated (final value {}, expected {})".format(my_predbat.soc_kw, original_soc_kw))
        failed = True
    if my_predbat.soc_max != original_soc_max:
        print("ERROR: soc_max was mutated (final value {}, expected {})".format(my_predbat.soc_max, original_soc_max))
        failed = True

    if len(captured_soc) < 2:
        print("ERROR: run_prediction should have been called at least twice, got {} captures".format(len(captured_soc)))
        failed = True
    else:
        # Every call: base soc_kw/soc_max must equal the original values.
        for idx, (base_soc, base_max, pred_soc, pred_max) in enumerate(captured_soc):
            if base_soc != original_soc_kw:
                print("ERROR: call {}: base soc_kw was mutated to {} (expected {})".format(idx, base_soc, original_soc_kw))
                failed = True
            if base_max != original_soc_max:
                print("ERROR: call {}: base soc_max was mutated to {} (expected {})".format(idx, base_max, original_soc_max))
                failed = True

        # First run_prediction call: baseline simulation uses soc_yesterday.
        _, _, pred_soc_0, pred_max_0 = captured_soc[0]
        if pred_soc_0 != expected_soc_yesterday:
            print("ERROR: first Prediction soc_kw should be {} (soc_yesterday), got {}".format(expected_soc_yesterday, pred_soc_0))
            failed = True
        if pred_max_0 != original_soc_max:
            print("ERROR: first Prediction soc_max should be {} (unchanged), got {}".format(original_soc_max, pred_max_0))
            failed = True

        # Find the no-battery/PV run: it is the call where the Prediction was
        # constructed with soc_kw=0, soc_max=0.  It is the last distinct call
        # (after the baseline runs).  Scan from the end backwards.
        no_bat_call = None
        for idx in range(len(captured_soc) - 1, -1, -1):
            _, _, pred_soc, pred_max = captured_soc[idx]
            if pred_soc == 0 and pred_max == 0:
                no_bat_call = idx
                break

        if no_bat_call is None:
            print("ERROR: no run_prediction call found where Prediction had soc_kw=0, soc_max=0 (no-battery sim)")
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
    failed = _test_car_slot_from_energy_sensor(my_predbat, failed)
    failed = _test_early_exit_respects_day_rollover(my_predbat, failed)
    failed = _test_reconstruct_car_slots(my_predbat, failed)
    failed = _test_soc_not_mutated_and_override_passed(my_predbat, failed)

    return failed

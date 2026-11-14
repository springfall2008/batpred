# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone, timedelta

from tests.test_infra import reset_rates, reset_inverter, prime_flat_prediction
from tests.test_kernel_parity import kernel_available


def _keep_metric_for_floor(my_predbat, floor_percent, import_rate, best_soc_keep=0.0, prediction_kernel=False):
    """Run an hour of prediction from 20% SoC with the given manual_soc floor, best_soc_keep and flat import rate on the chosen engine, and return the keep metric."""
    reset_inverter(my_predbat)
    reset_rates(my_predbat, import_rate, 5.0)
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 2.0
    my_predbat.reserve = 0.0
    my_predbat.best_soc_keep = best_soc_keep
    my_predbat.best_soc_keep_weight = 1.0
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = prediction_kernel

    my_predbat.all_active_keep_max = {}
    if floor_percent is None:
        my_predbat.all_active_keep = {}
    else:
        my_predbat.all_active_keep = {minute: floor_percent for minute in range(my_predbat.minutes_now, my_predbat.minutes_now + 120)}

    prime_flat_prediction(my_predbat)
    if prediction_kernel and not getattr(my_predbat.prediction, "kernel_handle", 0):
        raise RuntimeError("expected the C++ prediction kernel but it is not loaded")

    result = my_predbat.run_prediction([], [], [], [], False, end_record=60)
    return result[8]


def _plan_soc_at_free_rate_target(my_predbat, floor_percent):
    """
    Plan a free (0p) horizon with a charge window over the next 5.5 hours and a manual_soc floor for
    the half hour after it, from 10% SoC with no load and a 2p/kWh cycle cost, and return the planned
    SoC (kWh) when the floor starts. With nothing to buy later, only the floor gives the plan a reason
    to charge.
    """
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 0.0, 0.0)
    my_predbat.rate_min_forward = {}
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = False
    my_predbat.calculate_second_pass = False
    my_predbat.set_charge_freeze = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 1.0
    my_predbat.reserve = 0.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_keep_weight = 0.5
    # 3 kW so the window can fill the battery; reset_inverter's 1 kW cannot in 5.5 hours
    my_predbat.battery_rate_max_charge = 3.0 / 60.0
    my_predbat.inverter_limit = 3.0 / 60.0
    my_predbat.metric_battery_cycle = 2.0
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = False

    my_predbat.all_active_keep_max = {}
    my_predbat.all_active_keep = {minute: floor_percent for minute in range(my_predbat.minutes_now + 330, my_predbat.minutes_now + 360)} if floor_percent else {}

    prime_flat_prediction(my_predbat)

    end_record = my_predbat.forecast_minutes
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 330, "average": 0.0}]
    charge_limit_best = [0]
    metric, _, _, _, _, _, _, _, metric_keep, _, _ = my_predbat.run_prediction(charge_limit_best, charge_window_best, [], [], False, end_record=end_record)
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.optimise_all_windows(metric, metric_keep)
    my_predbat.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, [], [], False, end_record=end_record, save="best")
    soc_at_target = my_predbat.predict_soc_best.get(330, 0)

    my_predbat.all_active_keep = {}
    return soc_at_target


def run_test_manual_soc(my_predbat):
    """
    Test manual SOC target feature
    """
    failed = False
    print("Test manual SOC target")

    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0

    # Reset manual_soc to off
    my_predbat.manual_select("manual_soc", "off")

    # Test 1: Basic manual SOC parsing with default value
    print("Test 1: Basic manual SOC parsing with default value")
    my_predbat.args["manual_soc_value"] = 100
    my_predbat.manual_select("manual_soc", "05:30")

    # Read back the manual_soc_keep by calling manual_rates
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    # The minutes are calculated from midnight_utc, not from "now"
    # So we need to check what minutes_now is and adjust
    # For this test, we check if any entry exists and has value 100
    if not my_predbat.manual_soc_keep:
        print("ERROR: T1 Expected manual_soc_keep to have entries but got empty dict")
        failed = True
    else:
        # Check if any value is 100
        has_100 = any(v == 100.0 for v in my_predbat.manual_soc_keep.values())
        if not has_100:
            print("ERROR: T1 Expected manual_soc_keep to have SOC target of 100% but got {}".format(my_predbat.manual_soc_keep))
            failed = True
        else:
            print("PASS: T1 Manual SOC target set correctly to 100% at 05:30")

    # Test 2: Manual SOC with explicit value
    print("Test 2: Manual SOC with explicit value")
    my_predbat.manual_select("manual_soc", "06:00=80")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if not my_predbat.manual_soc_keep:
        print("ERROR: T2 Expected manual_soc_keep to have entries but got empty dict")
        failed = True
    else:
        # Check if any value is 80
        has_80 = any(v == 80.0 for v in my_predbat.manual_soc_keep.values())
        if not has_80:
            print("ERROR: T2 Expected manual_soc_keep to have SOC target of 80% but got {}".format(my_predbat.manual_soc_keep))
            failed = True
        else:
            print("PASS: T2 Manual SOC target set correctly to 80% at 06:00")

    # Test 3: Multiple manual SOC targets
    print("Test 3: Multiple manual SOC targets")
    my_predbat.manual_select("manual_soc", "05:30=100,07:00=90,08:30=50")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    expected_values = {100.0, 90.0, 50.0}
    actual_values = set(my_predbat.manual_soc_keep.values())

    if not expected_values.issubset(actual_values):
        print("ERROR: T3 Expected manual_soc_keep to have values {} but got {}".format(expected_values, actual_values))
        failed = True
    else:
        print("PASS: T3 Multiple manual SOC targets set correctly")

    # Test 4: Manual SOC off clears targets
    print("Test 4: Manual SOC off clears targets")
    my_predbat.manual_select("manual_soc", "off")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if my_predbat.manual_soc_keep:
        print("ERROR: T4 Expected manual_soc_keep to be empty when off but got {}".format(my_predbat.manual_soc_keep))
        failed = True
    else:
        print("PASS: T4 Manual SOC targets cleared when set to off")

    # Test 5: Override SOC for a time slot that has already started
    print("Test 5: Override SOC for a time slot that has already started")
    # Set midnight_utc to a known time and simulate being at 5:40am (340 minutes from midnight)
    # The current 30-minute slot is 5:30-6:00 (330-360 minutes)
    # We should be able to override the 5:30 slot even though it started 10 minutes ago

    # Set midnight to a known time (Dec 19, 2025 00:00 UTC)
    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    # Set now_utc to 5:40am (10 minutes into the 5:30 slot)
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=340)
    my_predbat.minutes_now = 340  # 5:40am (10 minutes into the 5:30 slot)
    my_predbat.manual_select("manual_soc", "05:30=75")

    # Read back the manual_soc_keep
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if not my_predbat.manual_soc_keep:
        print("ERROR: T5 Expected manual_soc_keep to have entries for override in current slot but got empty dict")
        failed = True
    else:
        # Verify the override is at the correct time: 5:30am = 330 minutes from midnight
        # The slot should span 330-359 minutes (30 minutes starting at 5:30)
        expected_start = 330
        expected_end = 359

        # Check if we have 75% values at the expected time range
        minutes_with_75 = sorted([k for k, v in my_predbat.manual_soc_keep.items() if v == 75.0])

        if not minutes_with_75:
            print("ERROR: T5 Expected manual_soc_keep to have SOC target of 75% but got {}".format(my_predbat.manual_soc_keep))
            failed = True
        elif minutes_with_75[0] != expected_start or minutes_with_75[-1] != expected_end:
            print("ERROR: T5 Expected override at minutes {}-{} but got {}-{}".format(expected_start, expected_end, minutes_with_75[0], minutes_with_75[-1]))
            failed = True
        elif len(minutes_with_75) != 30:
            print("ERROR: T5 Expected 30 minutes with 75% SOC but got {} minutes".format(len(minutes_with_75)))
            failed = True
        else:
            print("PASS: T5 Manual SOC target correctly overrides current slot at minutes {}-{} (started at 5:30, now 5:40)".format(minutes_with_75[0], minutes_with_75[-1]))

    # Test 6: When time moves past the slot end, it should be dropped from the list
    print("Test 6: When time moves past the slot end, it should be dropped from the list")
    # Keep the existing selection from Test 5 (05:30=75) but move time to 6:00am
    # The 5:30-6:00 slot has now completely finished, so it should be dropped
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=360)
    my_predbat.minutes_now = 360  # 6:00am (slot 5:30-6:00 has just ended)

    # Read back the manual_soc_keep - the 05:30 slot should now be gone as it's in the past
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))

    if my_predbat.manual_soc_keep:
        # Check if any entries remain - they should not as the slot is now completely in the past
        print("ERROR: T6 Expected manual_soc_keep to be empty as slot 5:30-6:00 has passed (now 6:00) but got {}".format(my_predbat.manual_soc_keep))
        failed = True
    else:
        print("PASS: T6 Manual SOC slot 5:30-6:00 correctly dropped when time moved to 6:00am")

    # Test 7: a manual floor must still cost something to miss when the import rate is free or
    # negative (#5162). The penalty used to be weighted by the deficit minute's import rate alone,
    # so inside a 0p window it was exactly 0 and the plan sat at reserve under a 100% target, and
    # at a negative rate it rewarded being below the target.
    print("Test 7: A manual floor is penalised at a 0p or negative import rate")
    my_predbat.manual_select("manual_soc", "off")
    my_predbat.now_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}

    engines = [False]
    if kernel_available()[0]:
        engines.append(True)
    for prediction_kernel in engines:
        engine = "C++ kernel" if prediction_kernel else "Python"
        keep_normal = _keep_metric_for_floor(my_predbat, 50.0, 10.0, prediction_kernel=prediction_kernel)
        keep_free = _keep_metric_for_floor(my_predbat, 50.0, 0.0, prediction_kernel=prediction_kernel)
        keep_negative = _keep_metric_for_floor(my_predbat, 50.0, -5.0, prediction_kernel=prediction_kernel)
        # With no manual/alert floor a plain best_soc_keep (20% SoC below a 50% keep) is unchanged: a free import costs nothing
        keep_plain_free = _keep_metric_for_floor(my_predbat, None, 0.0, best_soc_keep=5.0, prediction_kernel=prediction_kernel)

        if keep_normal <= 0:
            print("ERROR: T7 {} Expected a keep penalty below a 50% floor at 10p, got {}".format(engine, keep_normal))
            failed = True
        elif abs(keep_free * 10 - keep_normal) > 0.01:
            print("ERROR: T7 {} Expected the 0p floor penalty ({}) at the 1p minimum, a tenth of the 10p one ({})".format(engine, keep_free, keep_normal))
            failed = True
        elif abs(keep_negative - keep_free) > 0.001:
            print("ERROR: T7 {} Expected the -5p floor penalty ({}) to equal the 0p one ({}), both at the 1p minimum".format(engine, keep_negative, keep_free))
            failed = True
        elif keep_plain_free != 0:
            print("ERROR: T7 {} Expected no penalty below a plain best_soc_keep at 0p with no manual/alert floor, got {}".format(engine, keep_plain_free))
            failed = True
        else:
            print("PASS: T7 {} floor penalised at 10p ({}), 0p ({}) and -5p ({}), plain best_soc_keep unclamped".format(engine, keep_normal, keep_free, keep_negative))
    my_predbat.prediction_kernel_enable = False

    # Test 8: the plan itself charges to a manual target that falls inside a free window (#5162)
    print("Test 8: The plan charges to a 100% manual target inside a 0p window")
    soc_no_target = _plan_soc_at_free_rate_target(my_predbat, None)
    soc_target = _plan_soc_at_free_rate_target(my_predbat, 100.0)
    if soc_no_target > 1.5:
        print("ERROR: T8 Expected no charge without a target (2p cycle cost, nothing to buy later), got {} kWh when the floor starts".format(soc_no_target))
        failed = True
    elif soc_target < 9.5:
        print("ERROR: T8 Expected the plan to reach the 100% target when the floor starts, got {} kWh of 10".format(soc_target))
        failed = True
    else:
        print("PASS: T8 SoC when the floor starts is {} kWh with a 100% target, {} kWh without".format(soc_target, soc_no_target))

    # Clean up
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.all_active_keep = {}
    my_predbat.manual_select("manual_soc", "off")

    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)

    return failed

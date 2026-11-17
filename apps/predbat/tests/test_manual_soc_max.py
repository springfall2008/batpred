# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone

from tests.test_infra import reset_rates, reset_inverter, prime_flat_prediction
from tests.test_kernel_parity import kernel_available

IMPORT_RATE = 10.0
EXPORT_RATE = 5.0


def _keep_metric_for_ceiling(my_predbat, ceiling_percent, export_rate=EXPORT_RATE, prediction_kernel=False):
    """Run an hour of prediction with the given manual_soc_max ceiling and flat export rate on the chosen engine, and return the keep metric."""
    reset_inverter(my_predbat)
    reset_rates(my_predbat, IMPORT_RATE, export_rate)
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 8.0
    my_predbat.reserve = 0.0
    # Isolate the ceiling penalty from the floor one, which shares metric_keep
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_keep_weight = 1.0
    my_predbat.debug_enable = False
    # The Python engine unless asked: this exercises the source, not whatever prebuilt binary is present;
    # kernel_parity covers the C++ mirror agreeing with it
    my_predbat.prediction_kernel_enable = prediction_kernel

    my_predbat.all_active_keep = {}
    if ceiling_percent is None:
        my_predbat.all_active_keep_max = {}
    else:
        my_predbat.all_active_keep_max = {minute: ceiling_percent for minute in range(my_predbat.minutes_now, my_predbat.minutes_now + 120)}

    prime_flat_prediction(my_predbat)
    if prediction_kernel and not getattr(my_predbat.prediction, "kernel_handle", 0):
        raise RuntimeError("expected the C++ prediction kernel but it is not loaded")

    result = my_predbat.run_prediction([], [], [], [], False, end_record=60)
    return result[8]


def run_test_manual_soc_max(my_predbat):
    """
    Test manual SOC maximum (ceiling) target feature - the discharge-direction sibling of
    manual_soc, added for issue #1578 (weekly battery calibration ahead of a known cheap slot).
    """
    failed = False
    print("Test manual SOC max target")

    my_predbat.midnight_utc = datetime(2025, 12, 19, 0, 0, 0, tzinfo=timezone.utc)
    my_predbat.midnight = my_predbat.midnight_utc.astimezone(my_predbat.local_tz)
    my_predbat.now_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0

    # Reset manual_soc_max to off
    my_predbat.manual_select("manual_soc_max", "off")

    # Test 1: Basic manual SOC max parsing with an explicit value
    print("Test 1: Basic manual SOC max parsing with an explicit value")
    my_predbat.manual_select("manual_soc_max", "00:00=4")

    my_predbat.manual_soc_max_keep = my_predbat.manual_rates("manual_soc_max", default_rate=my_predbat.get_arg("manual_soc_max_value"))

    if not my_predbat.manual_soc_max_keep:
        print("ERROR: T1 Expected manual_soc_max_keep to have entries but got empty dict")
        failed = True
    else:
        has_4 = any(v == 4.0 for v in my_predbat.manual_soc_max_keep.values())
        if not has_4:
            print("ERROR: T1 Expected manual_soc_max_keep to have SOC ceiling of 4% but got {}".format(my_predbat.manual_soc_max_keep))
            failed = True
        else:
            print("PASS: T1 Manual SOC max target set correctly to 4% at 00:00")

    # Test 2: Manual SOC max with explicit value, independent of manual_soc's own selection
    print("Test 2: Manual SOC max and manual SOC (floor) are independent controls")
    my_predbat.manual_select("manual_soc", "off")
    my_predbat.manual_select("manual_soc_max", "23:30=50")

    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))
    my_predbat.manual_soc_max_keep = my_predbat.manual_rates("manual_soc_max", default_rate=my_predbat.get_arg("manual_soc_max_value"))

    if my_predbat.manual_soc_keep:
        print("ERROR: T2 Expected manual_soc_keep (floor) to be untouched by a manual_soc_max selection, got {}".format(my_predbat.manual_soc_keep))
        failed = True
    elif not any(v == 50.0 for v in my_predbat.manual_soc_max_keep.values()):
        print("ERROR: T2 Expected manual_soc_max_keep to have SOC ceiling of 50% but got {}".format(my_predbat.manual_soc_max_keep))
        failed = True
    else:
        print("PASS: T2 manual_soc_max set independently of manual_soc")

    # Test 3: Manual SOC max off clears targets
    print("Test 3: Manual SOC max off clears targets")
    my_predbat.manual_select("manual_soc_max", "off")

    my_predbat.manual_soc_max_keep = my_predbat.manual_rates("manual_soc_max", default_rate=my_predbat.get_arg("manual_soc_max_value"))

    if my_predbat.manual_soc_max_keep:
        print("ERROR: T3 Expected manual_soc_max_keep to be empty when off but got {}".format(my_predbat.manual_soc_max_keep))
        failed = True
    else:
        print("PASS: T3 Manual SOC max targets cleared when set to off")

    # Test 4: A ceiling below the floor at the same minute is a contradiction - the floor wins and
    # the conflicting ceiling is dropped with a warning, rather than handing the optimiser two
    # penalties pulling opposite ways (see fetch.py's all_active_keep/all_active_keep_max merge).
    print("Test 4: Ceiling below the floor at the same minute is dropped, floor wins")
    my_predbat.manual_select("manual_soc", "01:00=80")
    my_predbat.manual_select("manual_soc_max", "01:00=20")

    log_messages = []
    orig_log = my_predbat.log
    my_predbat.log = lambda msg, *args, **kwargs: log_messages.append(str(msg))
    my_predbat.manual_soc_keep = my_predbat.manual_rates("manual_soc", default_rate=my_predbat.get_arg("manual_soc_value"))
    my_predbat.manual_soc_max_keep = my_predbat.manual_rates("manual_soc_max", default_rate=my_predbat.get_arg("manual_soc_max_value"))

    # Call the production merge/conflict-resolution path itself (fetch.py), not a copy of it
    my_predbat.alert_active_keep = {}
    my_predbat.combine_active_keep()
    my_predbat.log = orig_log

    if my_predbat.all_active_keep_max:
        print("ERROR: T4 Expected the conflicting ceiling to be dropped but got {}".format(my_predbat.all_active_keep_max))
        failed = True
    elif not any("below the manual_soc/alert floor" in msg for msg in log_messages):
        print("ERROR: T4 Expected a warning about the floor/ceiling conflict, got {}".format(log_messages))
        failed = True
    else:
        print("PASS: T4 Conflicting ceiling dropped with a warning, floor preserved")

    # Test 5: a 0% ceiling is a real request (empty the battery for a BMS calibration, the point of
    # issue #1578), not an absent one. Absence has to be encoded separately from the numeric ceiling
    # or the default manual_soc_max_value of 0 silently does nothing.
    print("Test 5: A 0% ceiling penalises held charge, no ceiling does not")
    my_predbat.manual_select("manual_soc", "off")
    my_predbat.manual_select("manual_soc_max", "off")
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}

    no_ceiling_keep = _keep_metric_for_ceiling(my_predbat, None)
    zero_ceiling_keep = _keep_metric_for_ceiling(my_predbat, 0.0)
    half_ceiling_keep = _keep_metric_for_ceiling(my_predbat, 50.0)

    if no_ceiling_keep != 0:
        print("ERROR: T5 Expected no keep penalty with no ceiling set, got {}".format(no_ceiling_keep))
        failed = True
    elif zero_ceiling_keep <= 0:
        print("ERROR: T5 Expected a keep penalty for holding charge above a 0% ceiling, got {}".format(zero_ceiling_keep))
        failed = True
    elif half_ceiling_keep <= 0:
        print("ERROR: T5 Expected a keep penalty for holding charge above a 50% ceiling, got {}".format(half_ceiling_keep))
        failed = True
    elif zero_ceiling_keep <= half_ceiling_keep:
        print("ERROR: T5 Expected the 0% ceiling to penalise more than the 50% one, got {} vs {}".format(zero_ceiling_keep, half_ceiling_keep))
        failed = True
    else:
        print("PASS: T5 0% ceiling penalised ({}) above the 50% ceiling ({}), none without a ceiling".format(zero_ceiling_keep, half_ceiling_keep))

    # Test 6: like the floor, a ceiling still costs something to break when the export rate is free or
    # negative (#5162) - an import-only user has a 0p export rate, so without the minimum the ceiling did nothing
    print("Test 6: A ceiling is penalised at a 0p or negative export rate")
    engines = [False]
    if kernel_available()[0]:
        engines.append(True)
    for prediction_kernel in engines:
        engine = "C++ kernel" if prediction_kernel else "Python"
        half_ceiling_normal = _keep_metric_for_ceiling(my_predbat, 50.0, prediction_kernel=prediction_kernel)
        half_ceiling_free = _keep_metric_for_ceiling(my_predbat, 50.0, export_rate=0.0, prediction_kernel=prediction_kernel)
        half_ceiling_negative = _keep_metric_for_ceiling(my_predbat, 50.0, export_rate=-5.0, prediction_kernel=prediction_kernel)
        if abs(half_ceiling_free * EXPORT_RATE - half_ceiling_normal) > 0.01:
            print("ERROR: T6 {} Expected the 0p ceiling penalty ({}) at the 1p minimum, 1/{} of the {}p one ({})".format(engine, half_ceiling_free, EXPORT_RATE, EXPORT_RATE, half_ceiling_normal))
            failed = True
        elif abs(half_ceiling_negative - half_ceiling_free) > 0.001:
            print("ERROR: T6 {} Expected the -5p ceiling penalty ({}) to equal the 0p one ({}), both at the 1p minimum".format(engine, half_ceiling_negative, half_ceiling_free))
            failed = True
        else:
            print("PASS: T6 {} ceiling penalised at 0p ({}) and -5p ({})".format(engine, half_ceiling_free, half_ceiling_negative))
    my_predbat.prediction_kernel_enable = False

    # Clean up
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.manual_soc_max_keep = {}
    my_predbat.all_active_keep = {}
    my_predbat.all_active_keep_max = {}
    my_predbat.manual_select("manual_soc", "off")
    my_predbat.manual_select("manual_soc_max", "off")

    my_predbat.now_utc = datetime.now(my_predbat.local_tz)
    my_predbat.midnight_utc = my_predbat.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    my_predbat.minutes_now = int((my_predbat.now_utc - my_predbat.midnight_utc).total_seconds() / 60)

    return failed

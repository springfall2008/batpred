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

    # Reproduce the merge fetch_config_options() performs, exercising the actual conflict-resolution code
    my_predbat.alert_active_keep = {}
    my_predbat.all_active_keep = my_predbat.alert_active_keep.copy()
    for minute, soc_value in my_predbat.manual_soc_keep.items():
        my_predbat.all_active_keep[minute] = max(my_predbat.all_active_keep.get(minute, 0), soc_value)
    my_predbat.all_active_keep_max = {}
    for minute, soc_value in my_predbat.manual_soc_max_keep.items():
        my_predbat.all_active_keep_max[minute] = min(my_predbat.all_active_keep_max.get(minute, soc_value), soc_value)
    for minute in list(my_predbat.all_active_keep_max.keys()):
        floor_value = my_predbat.all_active_keep.get(minute, 0)
        if floor_value > my_predbat.all_active_keep_max[minute]:
            my_predbat.log("Warn: manual_soc_max target {}% at minute {} is below the manual_soc/alert floor {}% for the same minute - ignoring the ceiling there".format(my_predbat.all_active_keep_max[minute], minute, floor_value))
            del my_predbat.all_active_keep_max[minute]
    my_predbat.log = orig_log

    if my_predbat.all_active_keep_max:
        print("ERROR: T4 Expected the conflicting ceiling to be dropped but got {}".format(my_predbat.all_active_keep_max))
        failed = True
    elif not any("below the manual_soc/alert floor" in msg for msg in log_messages):
        print("ERROR: T4 Expected a warning about the floor/ceiling conflict, got {}".format(log_messages))
        failed = True
    else:
        print("PASS: T4 Conflicting ceiling dropped with a warning, floor preserved")

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
    my_predbat.midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    return failed

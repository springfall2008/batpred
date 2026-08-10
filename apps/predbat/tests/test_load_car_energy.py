# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for load_car_energy()'s warning when car_charging_energy is configured but resolves to
no data at all - e.g. the entity was removed upstream (#4458) rather than a transient gap. Without
this, car_charging_hold (on by default) silently degrades to the load-threshold heuristic with only
a benign, error-free log line, on every car-charging day, until someone notices by chance.

The loud log line is throttled to once per incident (not every cycle) since the condition can also
be persistent-but-intentional (a recorder-excluded entity, a fresh install with no history yet), not
only a genuine break - see the PR review discussion on #4469. record_status/had_errors still fire on
every cycle the condition persists, so the status sensor stays accurate; only the log spam is
suppressed.
"""


def test_load_car_energy_warns_when_configured_entity_has_no_data(my_predbat):
    """Verify a loud, error-flagged warning fires when car_charging_hold is on and
    car_charging_energy is configured but minute_data_import_export() returns nothing, and that
    the same setup with actual data, or with car_charging_hold off, stays silent.
    """
    failed = False
    print("**** Testing load_car_energy warns on a configured-but-empty car_charging_energy ****")

    # my_predbat is a shared fixture across the whole test run - save everything this test touches
    # so it can be restored afterward regardless of outcome, or later tests get a poisoned instance.
    had_car_charging_energy_arg = "car_charging_energy" in my_predbat.args
    original_arg = my_predbat.args.get("car_charging_energy")
    original_scale = my_predbat.car_charging_energy_scale
    original_max_days = my_predbat.max_days_previous
    original_hold = my_predbat.car_charging_hold
    original_had_errors = my_predbat.had_errors
    original_status = my_predbat.current_status
    original_minute_data_import_export = my_predbat.minute_data_import_export
    original_warned = my_predbat.car_charging_energy_warned
    original_log = my_predbat.log

    try:
        my_predbat.args["car_charging_energy"] = "sensor.ohme_test_energy"
        my_predbat.car_charging_energy_scale = 1.0
        my_predbat.max_days_previous = 7

        print("Test: car_charging_hold on, entity configured but empty - warns and flags an error")
        my_predbat.car_charging_hold = True
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
        result = my_predbat.load_car_energy(my_predbat.now_utc)
        if result != {}:
            print("  ERROR: expected an empty result when the entity has no data, got {}".format(result))
            failed = True
        if not my_predbat.had_errors:
            print("  ERROR: expected had_errors to be set when a configured car_charging_energy entity has no data")
            failed = True
        if "car_charging_energy" not in my_predbat.current_status:
            print("  ERROR: expected the warning to be recorded to current_status, got {!r}".format(my_predbat.current_status))
            failed = True

        print("Test: car_charging_hold on, entity configured and has real data - stays silent")
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        my_predbat.minute_data_import_export = lambda *args, **kwargs: {0: 1.5}
        result = my_predbat.load_car_energy(my_predbat.now_utc)
        if result != {0: 1.5}:
            print("  ERROR: expected the real data to pass through unchanged, got {}".format(result))
            failed = True
        if my_predbat.had_errors:
            print("  ERROR: expected no error when the entity genuinely has data")
            failed = True

        print("Test: car_charging_hold off, entity configured but empty - stays silent (hold isn't in play)")
        my_predbat.car_charging_hold = False
        my_predbat.had_errors = False
        my_predbat.current_status = ""
        my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
        my_predbat.load_car_energy(my_predbat.now_utc)
        if my_predbat.had_errors:
            print("  ERROR: expected no error when car_charging_hold is off, even with an empty entity")
            failed = True

        print("Test: the loud log warning fires once per incident, not every cycle (PR review on #4469)")
        my_predbat.car_charging_hold = True
        my_predbat.car_charging_energy_warned = False
        my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
        captured = []
        my_predbat.log = lambda msg, quiet=True: captured.append(msg)

        my_predbat.had_errors = False
        my_predbat.load_car_energy(my_predbat.now_utc)  # 1st occurrence - should log
        if not my_predbat.had_errors:
            print("  ERROR: expected had_errors on the first occurrence of the empty-entity condition")
            failed = True
        if sum(1 for m in captured if "no data could be loaded" in m) != 1:
            print("  ERROR: expected exactly one loud warning on the first occurrence, captured: {}".format(captured))
            failed = True

        my_predbat.had_errors = False
        my_predbat.load_car_energy(my_predbat.now_utc)  # 2nd occurrence, same incident - should not re-log
        if not my_predbat.had_errors:
            print("  ERROR: expected had_errors to keep being set on every cycle the condition persists")
            failed = True
        if sum(1 for m in captured if "no data could be loaded" in m) != 1:
            print("  ERROR: expected the loud warning to stay suppressed on a repeat of the same incident, captured: {}".format(captured))
            failed = True

        my_predbat.minute_data_import_export = lambda *args, **kwargs: {0: 1.5}
        my_predbat.load_car_energy(my_predbat.now_utc)  # data recovers - clears the incident

        my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
        my_predbat.load_car_energy(my_predbat.now_utc)  # a fresh incident - should log again
        if sum(1 for m in captured if "no data could be loaded" in m) != 2:
            print("  ERROR: expected a fresh occurrence after data recovered to warn again, captured: {}".format(captured))
            failed = True
    finally:
        my_predbat.minute_data_import_export = original_minute_data_import_export
        if had_car_charging_energy_arg:
            my_predbat.args["car_charging_energy"] = original_arg
        else:
            my_predbat.args.pop("car_charging_energy", None)
        my_predbat.car_charging_energy_scale = original_scale
        my_predbat.max_days_previous = original_max_days
        my_predbat.car_charging_hold = original_hold
        my_predbat.had_errors = original_had_errors
        my_predbat.current_status = original_status
        my_predbat.car_charging_energy_warned = original_warned
        my_predbat.log = original_log

    return failed

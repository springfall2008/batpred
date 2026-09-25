# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the export_more_solar / set_export_freeze dependency warning.

export_more_solar works by enabling Freeze Export on otherwise-idle slots that have predicted solar,
so optimise_solar() returns immediately when set_export_freeze is off (plan.py). The switch is then
on but does nothing whatsoever, and nothing anywhere said so - a real system in issue #4865 had run
that combination for months.

The docs already state the dependency ("The feature relies on switch.predbat_set_export_freeze being
enabled"), so this is purely about telling a user who has not read them.
"""


def _override_switches(my_predbat, export_more_solar, set_export_freeze):
    """Force the two switches under test for the next fetch_config_options() call.

    fetch_config_options() re-reads every setting through get_arg(), so setting the attributes
    directly would just be overwritten. Intercept those two keys and let everything else through,
    which keeps the rest of the real config path in play.
    """
    real_get_arg = my_predbat.get_arg
    overrides = {"export_more_solar": export_more_solar, "set_export_freeze": set_export_freeze}

    def get_arg(arg, *args, **kwargs):
        if arg in overrides:
            return overrides[arg]
        return real_get_arg(arg, *args, **kwargs)

    my_predbat.get_arg = get_arg
    return real_get_arg


def test_optimise_solar_is_a_no_op_without_export_freeze(my_predbat):
    """The premise of the warning: optimise_solar() really does nothing when freeze export is off.

    Asserted directly so the warning cannot outlive the behaviour it describes - if optimise_solar()
    ever learns to work without Freeze Export, this fails and the warning must be removed.
    """
    print("**** test_optimise_solar_is_a_no_op_without_export_freeze ****")
    failed = False

    saved = (my_predbat.calculate_best_export, my_predbat.set_export_freeze, my_predbat.export_window_best)
    try:
        my_predbat.calculate_best_export = True
        my_predbat.set_export_freeze = False
        my_predbat.export_window_best = [{"start": 0, "end": 30, "average": 15.0}]

        sentinel = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        result = my_predbat.optimise_solar(*sentinel, 1)

        if result != sentinel:
            print("ERROR: optimise_solar() should return its inputs untouched when set_export_freeze is off, got {}".format(result))
            failed = True
    finally:
        my_predbat.calculate_best_export, my_predbat.set_export_freeze, my_predbat.export_window_best = saved

    if not failed:
        print("PASS")
    return failed


def test_warns_once_when_export_more_solar_has_no_effect(my_predbat):
    """Warn when export_more_solar is on and set_export_freeze is off - but only once per incident.

    fetch_config_options() runs every cycle (~5 minutes), so an un-throttled warning would spam the
    log for a setup the user may have chosen deliberately. Mirrors car_charging_energy_warned.
    """
    print("**** test_warns_once_when_export_more_solar_has_no_effect ****")
    failed = False

    logged = []
    real_log = my_predbat.log
    my_predbat.log = lambda message: logged.append(message)
    saved = (my_predbat.export_more_solar, my_predbat.set_export_freeze, my_predbat.export_more_solar_warned)

    def warnings():
        return [message for message in logged if "export_more_solar" in message and "no effect" in message]

    try:
        my_predbat.export_more_solar_warned = False

        # First cycle with the broken combination warns
        real_get_arg = _override_switches(my_predbat, export_more_solar=True, set_export_freeze=False)
        my_predbat.fetch_config_options()
        if len(warnings()) != 1:
            print("ERROR: expected exactly one warning on the first cycle, got {}".format(len(warnings())))
            failed = True

        # Subsequent cycles stay quiet
        logged.clear()
        my_predbat.fetch_config_options()
        my_predbat.fetch_config_options()
        if warnings():
            print("ERROR: the warning repeated on later cycles - it should be logged once per incident, got {}".format(len(warnings())))
            failed = True

        # Fixing the combination re-arms it, so a recurrence is reported again
        logged.clear()
        _override_switches(my_predbat, export_more_solar=True, set_export_freeze=True)
        my_predbat.fetch_config_options()
        if my_predbat.export_more_solar_warned:
            print("ERROR: the warned flag should reset once the combination is valid again")
            failed = True
        _override_switches(my_predbat, export_more_solar=True, set_export_freeze=False)
        my_predbat.fetch_config_options()
        if len(warnings()) != 1:
            print("ERROR: expected the warning again after the combination was fixed and re-broken, got {}".format(len(warnings())))
            failed = True
    finally:
        my_predbat.log = real_log
        my_predbat.get_arg = real_get_arg
        my_predbat.export_more_solar, my_predbat.set_export_freeze, my_predbat.export_more_solar_warned = saved

    if not failed:
        print("PASS")
    return failed


def test_no_warning_for_valid_combinations(my_predbat):
    """The other three combinations are all legitimate and must stay silent."""
    print("**** test_no_warning_for_valid_combinations ****")
    failed = False

    logged = []
    real_log = my_predbat.log
    real_get_arg = my_predbat.get_arg
    my_predbat.log = lambda message: logged.append(message)
    saved = (my_predbat.export_more_solar, my_predbat.set_export_freeze, my_predbat.export_more_solar_warned)

    try:
        for export_more_solar, set_export_freeze in ((False, False), (False, True), (True, True)):
            logged.clear()
            my_predbat.export_more_solar_warned = False
            _override_switches(my_predbat, export_more_solar, set_export_freeze)
            my_predbat.fetch_config_options()
            warnings = [message for message in logged if "export_more_solar" in message and "no effect" in message]
            if warnings:
                print("ERROR: export_more_solar={} set_export_freeze={} is a valid combination but warned: {}".format(export_more_solar, set_export_freeze, warnings))
                failed = True
    finally:
        my_predbat.log = real_log
        my_predbat.get_arg = real_get_arg
        my_predbat.export_more_solar, my_predbat.set_export_freeze, my_predbat.export_more_solar_warned = saved

    if not failed:
        print("PASS")
    return failed


def run_export_more_solar_warning_tests(my_predbat):
    """Run the export_more_solar dependency warning tests."""
    failed = False
    failed |= test_optimise_solar_is_a_no_op_without_export_freeze(my_predbat)
    failed |= test_warns_once_when_export_more_solar_has_no_effect(my_predbat)
    failed |= test_no_warning_for_valid_combinations(my_predbat)
    return failed

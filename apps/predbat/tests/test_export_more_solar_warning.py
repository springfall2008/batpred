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
so optimise_solar() returns immediately unless export slots are being planned (calculate_best_export)
and set_export_freeze is on (plan.py). The switch is then on but does nothing whatsoever, and nothing
anywhere said so - a real system in issue #4865 had run that combination for months.

set_export_freeze can be off either because the user turned it off or because fetch_inverter_data()
forced it off for an inverter without freeze support, so the check runs after that, reads the
effective value, and asks the inverter which of the two it was.

The docs already state the dependency ("The feature relies on switch.predbat_set_export_freeze being
enabled"), so this is purely about telling a user who has not read them.
"""


SAVED_ATTRS = ("export_more_solar", "calculate_best_export", "set_export_freeze", "export_more_solar_warned_reason", "inverters")


class _StubInverter:
    """Just the capability flag the check reads."""

    def __init__(self, inv_support_discharge_freeze):
        """Record whether this inverter supports export/discharge freeze."""
        self.inv_support_discharge_freeze = inv_support_discharge_freeze


class _Harness:
    """Capture log output and control the inverter's freeze capability, restoring both on exit."""

    def __init__(self, my_predbat):
        """Remember the fixture under test."""
        self.my_predbat = my_predbat
        self.logged = []

    def __enter__(self):
        """Install the log capture and start from a clean warned state."""
        my_predbat = self.my_predbat
        self.real_log = my_predbat.log
        self.saved = tuple(getattr(my_predbat, attr) for attr in SAVED_ATTRS)
        my_predbat.log = lambda message: self.logged.append(message)
        my_predbat.export_more_solar_warned_reason = None
        return self

    def __exit__(self, *exc):
        """Restore everything touched."""
        my_predbat = self.my_predbat
        my_predbat.log = self.real_log
        for attr, value in zip(SAVED_ATTRS, self.saved):
            setattr(my_predbat, attr, value)
        return False

    def run(self, export_more_solar=True, calculate_best_export=True, set_export_freeze=True, inverter_supports_freeze=True):
        """Set the effective state, run one check, and return the warnings it logged."""
        my_predbat = self.my_predbat
        my_predbat.export_more_solar = export_more_solar
        my_predbat.calculate_best_export = calculate_best_export
        my_predbat.set_export_freeze = set_export_freeze
        my_predbat.inverters = [_StubInverter(inverter_supports_freeze)]
        self.logged.clear()
        my_predbat.check_export_more_solar_effective()
        return [message for message in self.logged if "export_more_solar" in message and "no effect" in message]


class _Unreachable:
    """Stands in for self.prediction: any use means optimise_solar() got past its guard."""

    def __getattr__(self, name):
        """Fail loudly on any attribute access."""
        raise AssertionError("optimise_solar() read prediction.{} - it got past its guard".format(name))


def test_optimise_solar_is_a_no_op_when_warned(my_predbat):
    """The premise of the warning: optimise_solar() really does nothing in each case that warns.

    Asserted directly so the warning cannot outlive the behaviour it describes - if optimise_solar()
    ever learns to work in one of these states, this fails and the warning must be changed. prediction
    is swapped for an object that raises on use, so the guard is what is tested rather than whatever an
    earlier test happened to leave in the fixture; the effective case is the positive control.
    """
    print("**** test_optimise_solar_is_a_no_op_when_warned ****")
    failed = False

    # prediction only exists once a plan has been calculated, so it may be absent depending on test order
    had_prediction = hasattr(my_predbat, "prediction")
    saved = (my_predbat.calculate_best_export, my_predbat.set_export_freeze, my_predbat.export_window_best, getattr(my_predbat, "prediction", None))
    try:
        my_predbat.export_window_best = [{"start": 0, "end": 30, "average": 15.0}]
        my_predbat.prediction = _Unreachable()
        sentinel = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
        for calculate_best_export, set_export_freeze in ((True, False), (False, True)):
            my_predbat.calculate_best_export = calculate_best_export
            my_predbat.set_export_freeze = set_export_freeze
            try:
                result = my_predbat.optimise_solar(*sentinel, 1)
            except AssertionError as error:
                print("ERROR: calculate_best_export={} set_export_freeze={}: {}".format(calculate_best_export, set_export_freeze, error))
                failed = True
                continue
            if result != sentinel:
                print("ERROR: optimise_solar() should return its inputs untouched with calculate_best_export={} set_export_freeze={}, got {}".format(calculate_best_export, set_export_freeze, result))
                failed = True

        my_predbat.calculate_best_export = True
        my_predbat.set_export_freeze = True
        try:
            my_predbat.optimise_solar(*sentinel, 1)
            print("ERROR: positive control - optimise_solar() should get past its guard when effective, so this test is not exercising the guard")
            failed = True
        except AssertionError:
            pass
    finally:
        my_predbat.calculate_best_export, my_predbat.set_export_freeze, my_predbat.export_window_best, my_predbat.prediction = saved
        if not had_prediction:
            del my_predbat.prediction

    if not failed:
        print("PASS")
    return failed


def test_warns_with_the_actual_reason(my_predbat):
    """Each ineffective state warns, and the message names the cause so the advice is actionable.

    An inverter without freeze support must not be told to enable set_export_freeze - including when
    the switch is also off, as turning it on would change nothing.
    """
    print("**** test_warns_with_the_actual_reason ****")
    failed = False

    cases = (
        ("mode", dict(calculate_best_export=False), "mode"),
        ("user switch off", dict(set_export_freeze=False), "Enable set_export_freeze"),
        ("inverter unsupported", dict(set_export_freeze=False, inverter_supports_freeze=False), "does not support Freeze Export"),
    )
    with _Harness(my_predbat) as harness:
        for name, state, expected in cases:
            my_predbat.export_more_solar_warned_reason = None
            warnings = harness.run(**state)
            if len(warnings) != 1 or expected not in warnings[0]:
                print("ERROR: {}: expected one warning mentioning '{}', got {}".format(name, expected, warnings))
                failed = True
            elif name == "inverter unsupported" and "Enable set_export_freeze" in warnings[0]:
                print("ERROR: {}: must not advise enabling set_export_freeze, got {}".format(name, warnings[0]))
                failed = True

    if not failed:
        print("PASS")
    return failed


def test_warns_once_per_incident(my_predbat):
    """Warn once, stay quiet on later cycles, and re-arm once the setting becomes effective again.

    The check runs every cycle (~5 minutes), so an un-throttled warning would spam the log for a setup
    the user may have chosen deliberately.
    """
    print("**** test_warns_once_per_incident ****")
    failed = False

    with _Harness(my_predbat) as harness:
        if len(harness.run(set_export_freeze=False)) != 1:
            print("ERROR: expected exactly one warning on the first cycle")
            failed = True
        if harness.run(set_export_freeze=False) or harness.run(set_export_freeze=False):
            print("ERROR: the warning repeated on later cycles - it should be logged once per incident")
            failed = True
        harness.run()
        if my_predbat.export_more_solar_warned_reason is not None:
            print("ERROR: the warned state should reset once export_more_solar is effective again")
            failed = True
        if len(harness.run(calculate_best_export=False)) != 1:
            print("ERROR: expected the warning again after the setting was effective and then broken again")
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_warns_again_when_the_reason_changes(my_predbat):
    """Fixing one cause while another remains must report the remaining one.

    Otherwise a user who follows the first warning's advice sees the log go quiet and reasonably
    concludes export_more_solar now works, when it still does nothing.
    """
    print("**** test_warns_again_when_the_reason_changes ****")
    failed = False

    with _Harness(my_predbat) as harness:
        first = harness.run(calculate_best_export=False, set_export_freeze=False)
        second = harness.run(set_export_freeze=False)
        if len(first) != 1 or "mode" not in first[0]:
            print("ERROR: expected a mode warning first, got {}".format(first))
            failed = True
        if len(second) != 1 or "Enable set_export_freeze" not in second[0]:
            print("ERROR: after fixing the mode, expected a set_export_freeze warning, got {}".format(second))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_warning_when_effective_or_off(my_predbat):
    """export_more_solar off never warns, whatever else is set; fully enabled never warns either."""
    print("**** test_no_warning_when_effective_or_off ****")
    failed = False

    with _Harness(my_predbat) as harness:
        states = [dict(export_more_solar=False, calculate_best_export=c, set_export_freeze=f, inverter_supports_freeze=i) for c in (False, True) for f in (False, True) for i in (False, True)]
        states.append(dict())
        for state in states:
            my_predbat.export_more_solar_warned_reason = None
            warnings = harness.run(**state)
            if warnings:
                print("ERROR: {} should not warn, got {}".format(state, warnings))
                failed = True

    if not failed:
        print("PASS")
    return failed


def run_export_more_solar_warning_tests(my_predbat):
    """Run the export_more_solar dependency warning tests."""
    failed = False
    failed |= test_optimise_solar_is_a_no_op_when_warned(my_predbat)
    failed |= test_warns_with_the_actual_reason(my_predbat)
    failed |= test_warns_once_per_incident(my_predbat)
    failed |= test_warns_again_when_the_reason_changes(my_predbat)
    failed |= test_no_warning_when_effective_or_off(my_predbat)
    return failed

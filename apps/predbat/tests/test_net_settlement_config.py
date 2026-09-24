# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for parsing the metric_net_settlement_window_minutes setting.

The engine and today_cost() netting maths are covered by the net settlement suites in
test_kernel_parity.py; this covers the apps.yaml parsing and logging. The annual per-sample
reset dropping the current-window seed is checked in test_annual_bootstrap.py.
"""

from utils import net_settlement_window_from_arg


def run_net_settlement_window_arg_tests():
    """net_settlement_window_from_arg accepts only multiples of PREDICT_STEP that divide the day, returns True on failure"""
    failed = False
    cases = [
        # Off
        (None, 0),
        (0, 0),
        ("0", 0),
        (0.0, 0),
        (False, 0),
        # Valid windows, in the types apps.yaml or an entity can hand over
        (5, 5),
        (15, 15),
        (30, 30),
        (60, 60),
        (45, 45),
        (1440, 1440),
        ("60", 60),
        (60.0, 60),
        ("15.0", 15),
        # Not a multiple of the 5 minute prediction step
        (7, None),
        (1, None),
        (True, None),
        # Does not divide 1440
        (25, None),
        (35, None),
        (2880, None),
        # Negative, fractional or non-numeric
        (-60, None),
        (-5, None),
        (60.5, None),
        ("abc", None),
        ("", None),
        ([60], None),
        (float("nan"), None),
        (float("inf"), None),
    ]
    for value, expected in cases:
        result = net_settlement_window_from_arg(value)
        if result != expected or type(result) is not type(expected):
            print("ERROR: net_settlement_window_from_arg({!r}) returned {!r} expected {!r}".format(value, result, expected))
            failed = True
    return failed


def run_net_settlement_fetch_config_tests(my_predbat):
    """fetch_net_settlement_config sets the window, disables netting on bad values and logs only on change, returns True on failure"""
    failed = False
    had_arg = "metric_net_settlement_window_minutes" in my_predbat.args
    saved_arg = my_predbat.args.get("metric_net_settlement_window_minutes")
    saved_window = my_predbat.metric_net_settlement_window_minutes
    saved_window_arg = my_predbat.net_settlement_window_arg
    # log is normally the class method; only an instance override needs putting back afterwards
    saved_log = my_predbat.__dict__.get("log")
    logged = []

    def capture_log(message, *args, **kwargs):
        """Collect log lines instead of printing them"""
        logged.append(message)

    my_predbat.log = capture_log
    try:
        # name, value (None = absent from apps.yaml), expected window, expected log (None = nothing logged)
        steps = [
            ("absent", None, 0, None),
            ("valid", 60, 60, "Net settlement of import/export enabled over 60 minute windows"),
            ("valid_again", 60, 60, None),
            ("valid_float_same", 60.0, 60, None),
            ("changed", 15, 15, "Net settlement of import/export enabled over 15 minute windows"),
            ("invalid", 7, 0, "Warn: metric_net_settlement_window_minutes 7 must be a multiple of 5 that divides 1440"),
            ("invalid_again", 7, 0, None),
            ("not_a_number", "hourly", 0, "Warn: metric_net_settlement_window_minutes hourly must be a multiple of 5"),
            # Must not be truncated to a valid-looking 60
            ("fractional", 60.5, 0, "Warn: metric_net_settlement_window_minutes 60.5 must be a multiple of 5"),
            ("string", "30", 30, "Net settlement of import/export enabled over 30 minute windows"),
            ("off", 0, 0, None),
            ("re_enabled", 60, 60, "Net settlement of import/export enabled over 60 minute windows"),
        ]
        for name, value, expected_window, expected_log in steps:
            if value is None:
                my_predbat.args.pop("metric_net_settlement_window_minutes", None)
            else:
                my_predbat.args["metric_net_settlement_window_minutes"] = value
            logged.clear()
            my_predbat.fetch_net_settlement_config()
            if my_predbat.metric_net_settlement_window_minutes != expected_window or not isinstance(my_predbat.metric_net_settlement_window_minutes, int):
                print("ERROR: fetch_net_settlement_config step {}: window {!r} expected {!r}".format(name, my_predbat.metric_net_settlement_window_minutes, expected_window))
                failed = True
            net_logs = [line for line in logged if line.startswith("Warn: metric_net_settlement_window_minutes") or line.startswith("Net settlement")]
            if expected_log is None:
                if net_logs:
                    print("ERROR: fetch_net_settlement_config step {}: expected no log, got {}".format(name, net_logs))
                    failed = True
            elif len(net_logs) != 1 or not net_logs[0].startswith(expected_log):
                print("ERROR: fetch_net_settlement_config step {}: expected one log starting {!r}, got {}".format(name, expected_log, net_logs))
                failed = True
    finally:
        if saved_log is None:
            del my_predbat.log
        else:
            my_predbat.log = saved_log
        if had_arg:
            my_predbat.args["metric_net_settlement_window_minutes"] = saved_arg
        else:
            my_predbat.args.pop("metric_net_settlement_window_minutes", None)
        my_predbat.metric_net_settlement_window_minutes = saved_window
        my_predbat.net_settlement_window_arg = saved_window_arg
    return failed


def run_net_settlement_config_tests(my_predbat):
    """Run the net settlement configuration tests, returns True on failure"""
    print("**** Running net settlement config tests ****")
    failed = False
    failed |= run_net_settlement_window_arg_tests()
    failed |= run_net_settlement_fetch_config_tests(my_predbat)
    if not failed:
        print("PASS")
    return failed

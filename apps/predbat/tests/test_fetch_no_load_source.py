# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""
Regression test for #5092: with no load source, the real fetch_sensor_data() raises LoadSourceMissing,
which plan_once() turns into a skipped cycle, rather than a plain ValueError that reached the main
loop's generic handler and logged a traceback every cycle.

The status message must also name the right cause: nothing configured, or a configured load
forecast that has returned no data yet.
"""

from fetch import LoadSourceMissing


class _PastLoadSection(Exception):
    """Raised by the test's hook to stop fetch_sensor_data() once it is past the load-source check."""

LOAD_ARGS = ("load_today", "load_forecast", "ge_cloud_data", "iboost_energy_today", "load_ml_enable", "load_ml_source")


def _run_fetch(my_predbat, extra_args, forecast=None):
    """
    Run the real fetch_sensor_data() with no load_today and the given extra args, and return
    (raised exception, recorded status message). forecast is what the load forecast fetch returns
    ({} by default, an unavailable sensor). The fetch is stopped with _PastLoadSection at the first
    read after the load section. Restores the instance and its args afterwards.
    """
    saved_state = dict(my_predbat.__dict__)
    saved_args = dict(my_predbat.args)
    statuses = []
    raised = None
    try:
        for key in LOAD_ARGS:
            my_predbat.args.pop(key, None)
        my_predbat.args.update(extra_args)
        my_predbat.args["import_today"] = "sensor.import_today"
        forecast = forecast or {}
        my_predbat.fetch_extra_load_forecast = lambda *args, **kwargs: (dict(forecast), [forecast] if forecast else [])

        def past_load_section(*args, **kwargs):
            """The import_today read straight after the load section: stop the fetch here."""
            raise _PastLoadSection()

        my_predbat.minute_data_import_export = past_load_section
        my_predbat.record_status = lambda message, **kwargs: statuses.append(message)
        try:
            my_predbat.fetch_sensor_data(save=False)
        except Exception as e:
            raised = e
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        for name in [name for name in my_predbat.__dict__ if name not in saved_state]:
            delattr(my_predbat, name)
        my_predbat.__dict__.update(saved_state)
    return raised, statuses[-1] if statuses else None


def test_fetch_no_load_source(my_predbat):
    """
    Check the real fetch_sensor_data() raises LoadSourceMissing (a ValueError subclass, so older
    callers still catch it) with a status message that matches why there is no load data.
    """
    failed = 0
    print("Test fetch_no_load_source: no load source raises LoadSourceMissing with the right status (#5092)")

    cases = [
        ("nothing configured", {}, "load_today not set (in apps.yaml, or by a component's automatic configuration)"),
        # Load ML trains on load_today, so it cannot be the missing source's stand-in
        ("Load ML with no load_today", {"load_ml_enable": True, "load_ml_source": True}, "load_today not set (in apps.yaml, or by a component's automatic configuration)"),
        ("load_forecast with no value", {"load_forecast": []}, "load_today not set (in apps.yaml, or by a component's automatic configuration)"),
        ("load_forecast returned no data",{"load_forecast": "sensor.load_forecast_unavailable"}, "the load forecast returned no data"),
    ]
    for name, extra_args, expect_status in cases:
        raised, status = _run_fetch(my_predbat, extra_args)
        if not isinstance(raised, LoadSourceMissing):
            print("ERROR: {}: expected LoadSourceMissing, got {!r}".format(name, raised))
            failed = 1
        if not status or expect_status not in status:
            print("ERROR: {}: status {!r} does not contain {!r}".format(name, status, expect_status))
            failed = 1

    # A load_forecast that does return data is a load source: the fetch carries on past the check
    raised, _ = _run_fetch(my_predbat, {"load_forecast": "sensor.load_forecast"}, forecast={0: 0.0, 30: 0.5})
    if not isinstance(raised, _PastLoadSection):
        print("ERROR: load_forecast with data: expected the fetch to pass the load section, got {!r}".format(raised))
        failed = 1

    return failed

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
Regression test for #4317: a debug dump taken while fetch_sensor_data() is still running must not
see the PV forecasts as empty.

The web UI's debug dump runs create_debug_yaml() with no check for a fetch in progress. The PV
forecasts used to be reset to {} at the top of fetch_sensor_data() and only refilled near the end,
after the rate fetches, so a dump taken in between captured no PV at all while the live plan had
a full forecast.
"""

PV_NAMES = ("pv_forecast_minute", "pv_forecast_minute10", "pv_forecast_minute90")


class _StopFetch(Exception):
    """Raised by the test's hook to stop fetch_sensor_data() at the moment it is inspected."""


def test_fetch_pv_mid_cycle(my_predbat):
    """
    Run the real fetch_sensor_data() from a populated previous cycle, stop it inside the window
    before the PV forecast is refetched, and check the three PV forecasts still hold the previous
    cycle's values rather than {} - which is what a mid-fetch create_debug_yaml() would have
    written out.
    """
    failed = 0
    print("Test fetch_pv_mid_cycle: PV forecasts are not empty part way through fetch_sensor_data() (#4317)")

    # fetch_sensor_data() is stopped part way, so put back everything it touched before returning
    saved_state = dict(my_predbat.__dict__)

    previous = {
        "pv_forecast_minute": {minute: 0.5 for minute in range(0, 24 * 60, 5)},
        "pv_forecast_minute10": {minute: 0.25 for minute in range(0, 24 * 60, 5)},
        "pv_forecast_minute90": {minute: 0.75 for minute in range(0, 24 * 60, 5)},
    }
    seen = {}

    def hook(*args, **kwargs):
        """Record the PV forecasts as a dump would read them now, then stop the fetch."""
        seen.update({name: getattr(my_predbat, name) for name in PV_NAMES})
        raise _StopFetch()

    try:
        for name in PV_NAMES:
            setattr(my_predbat, name, dict(previous[name]))
        # The first call after the per-cycle resets, unconditional, and well before the PV refill.
        # Running further would need every sensor read stubbed, which the shared fixture can't feed.
        my_predbat.combine_active_keep = hook
        try:
            my_predbat.fetch_sensor_data(save=False)
            print("ERROR: fetch_sensor_data() returned without reaching combine_active_keep()")
            failed = 1
        except _StopFetch:
            for name in PV_NAMES:
                if seen[name] != previous[name]:
                    print("ERROR: {} was {} entries mid-fetch, expected the previous cycle's {} - a debug dump taken now would capture it empty".format(name, len(seen[name]), len(previous[name])))
                    failed = 1
    finally:
        for name in [name for name in my_predbat.__dict__ if name not in saved_state]:
            delattr(my_predbat, name)
        my_predbat.__dict__.update(saved_state)

    return failed

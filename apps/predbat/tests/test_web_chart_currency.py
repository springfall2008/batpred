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
Test that the Rates chart's per-series legend names (WebInterface.get_chart, web.py) use the
configured currency's minor unit rather than a hardcoded "p/kWh" - see issue #4153, where a
user configured for NZ dollars/cents saw "p/kWh" (pence) in the chart legend regardless.
"""

import re

from web import WebInterface


def _make_web(my_predbat):
    """
    Build a minimal WebInterface bound to my_predbat, bypassing ComponentBase.__init__ (which
    would stand up the real aiohttp app). currency_symbols/now_utc/minutes_now/etc are all
    read-only properties on ComponentBase that delegate to self.base, so nothing else needs
    setting here for get_chart() to run.
    """
    w = WebInterface.__new__(WebInterface)
    w.base = my_predbat
    w.log = my_predbat.log
    w.prefix = my_predbat.prefix
    return w


def test_rates_chart_series_names_use_currency_symbol(my_predbat):
    """
    Calls the real WebInterface.get_chart("Rates") and checks the rendered series names,
    rather than re-testing the formatting logic in isolation - a prior version of this test
    only duplicated the format string inline, so it would still pass even if web.py regressed
    back to a hardcoded "p/kWh" (Copilot review on PR #4288).

    get_chart() gates on soc_kw_best being populated (dashboard_values) and reads the Hourly/
    Today series from get_history_wrapper() - both are stubbed directly rather than trying to
    reproduce realistic HA history formatting, since only the currency-dependent series *names*
    are under test here, not the underlying rate data.
    """
    print("**** test_rates_chart_series_names_use_currency_symbol ****")

    original_currency_symbols = my_predbat.currency_symbols
    original_dashboard_values = getattr(my_predbat, "dashboard_values", None)

    try:
        w = _make_web(my_predbat)
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".soc_kw_best": {"attributes": {"results": {"2026-01-01T00:00:00+00:00": 5.0}}},
            my_predbat.prefix + ".rates": {"attributes": {"results": {"2026-01-01T00:00:00+00:00": 10.0}}},
        }
        fake_history = [
            [
                {"state": 10.5, "last_updated": "2026-01-01T00:00:00+00:00"},
                {"state": 12.3, "last_updated": "2026-01-01T00:30:00+00:00"},
            ]
        ]
        w.get_history_wrapper = lambda *a, **kw: fake_history

        for currency_symbols, expected_minor in [("£p", "p"), ("$c", "c"), ("€c", "c")]:
            my_predbat.currency_symbols = currency_symbols

            result = w.get_chart("Rates")
            series_names = re.findall(r"name: '([^']*)'", result)

            expected_hourly = "Hourly {}/kWh".format(expected_minor)
            expected_today = "Today {}/kWh".format(expected_minor)

            assert expected_hourly in series_names, f"Expected series '{expected_hourly}' in {series_names} for currency_symbols={currency_symbols}"
            assert expected_today in series_names, f"Expected series '{expected_today}' in {series_names} for currency_symbols={currency_symbols}"
            if expected_minor != "p":
                assert not any("p/kWh" in name for name in series_names), f"Series names should not be hardcoded to pence for currency_symbols={currency_symbols}, got {series_names}"

        print("✓ Rates chart series names correctly follow currency_symbols[1] (£p, $c, €c all verified against the real get_chart() output)")
        print("✓ Test passed")
        return False
    finally:
        my_predbat.currency_symbols = original_currency_symbols
        if original_dashboard_values is None:
            if hasattr(my_predbat, "dashboard_values"):
                del my_predbat.dashboard_values
        else:
            my_predbat.dashboard_values = original_dashboard_values

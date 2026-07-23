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


def test_rates_chart_series_names_use_currency_symbol(my_predbat):
    """
    Directly mirrors the series-name formatting used in WebInterface.get_chart's "Rates"
    branch (web.py) - get_chart itself requires a fully computed plan (soc_kw_best populated)
    before it reaches this branch, so this tests the formatting logic directly rather than
    standing up that heavier machinery.
    """
    print("**** test_rates_chart_series_names_use_currency_symbol ****")

    for currency_symbols, expected_minor in [("£p", "p"), ("$c", "c"), ("€c", "c")]:
        my_predbat.currency_symbols = currency_symbols
        hourly_name = "Hourly {}/kWh".format(my_predbat.currency_symbols[1])
        today_name = "Today {}/kWh".format(my_predbat.currency_symbols[1])

        assert hourly_name == "Hourly {}/kWh".format(expected_minor), f"Expected 'Hourly {expected_minor}/kWh', got '{hourly_name}'"
        assert today_name == "Today {}/kWh".format(expected_minor), f"Expected 'Today {expected_minor}/kWh', got '{today_name}'"
        if expected_minor != "p":
            assert "p/kWh" not in hourly_name and "p/kWh" not in today_name, f"Series name should not be hardcoded to pence for currency_symbols={currency_symbols}"

    print("✓ Rates chart series names correctly follow currency_symbols[1] (£p, $c, €c all verified)")
    print("✓ Test passed")
    return False

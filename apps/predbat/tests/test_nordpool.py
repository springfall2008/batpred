# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import copy
import json
import os
from datetime import datetime, timedelta

from futurerate import FutureRate

_FIXTURE_FILE = os.path.join(os.path.dirname(__file__), "nordpool_fixture.json")


def _load_fixture():
    """Load saved rate fixture, converting string keys back to ints for rate dicts."""
    if not os.path.exists(_FIXTURE_FILE):
        return None
    with open(_FIXTURE_FILE, "r") as f:
        raw = json.load(f)
    return {
        "agile_import": {int(k): v for k, v in raw["agile_import"].items()},
        "agile_export": {int(k): v for k, v in raw["agile_export"].items()},
        "nordpool_day0": raw["nordpool_day0"],
        "nordpool_day1": raw["nordpool_day1"],
    }


def _save_fixture(agile_import, agile_export, nordpool_day0, nordpool_day1):
    """Persist downloaded data so subsequent runs skip the network calls."""
    payload = {
        "agile_import": agile_import,
        "agile_export": agile_export,
        "nordpool_day0": nordpool_day0,
        "nordpool_day1": nordpool_day1,
    }
    with open(_FIXTURE_FILE, "w") as f:
        json.dump(payload, f, indent=2)
    print("Nordpool fixture saved to {}".format(_FIXTURE_FILE))


def _adjust_nordpool_dates(data, target_date):
    """Shift all deliveryStart/deliveryEnd timestamps to target_date.

    Nordpool entries carry absolute dates; replaying a fixture on a different day
    would push all minute offsets out of the forecast window. Replacing the date
    portion keeps the hour/minute structure intact while making the offsets valid.
    """
    data = copy.deepcopy(data)
    if not isinstance(data, dict) or "multiAreaEntries" not in data:
        return data
    date_str = target_date.strftime("%Y-%m-%d")
    for entry in data["multiAreaEntries"]:
        for key in ("deliveryStart", "deliveryEnd"):
            if key in entry:
                entry[key] = date_str + entry[key][10:]
    return data


def run_nordpool_test(my_predbat):
    """Test the nordpool futurerate analysis against Octopus Agile rates."""

    print("**** Running Nordpool tests ****")
    my_predbat.args["futurerate_url"] = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices?date=DATE&market=N2EX_DayAhead&deliveryArea=UK&currency=GBP"
    my_predbat.args["futurerate_adjust_import"] = True
    my_predbat.args["futurerate_adjust_export"] = True
    my_predbat.args["futurerate_peak_start"] = "16:00:00"
    my_predbat.args["futurerate_peak_end"] = "19:00:00"
    my_predbat.args["futurerate_peak_premium_import"] = 14
    my_predbat.args["futurerate_peak_premium_export"] = 6.5
    failed = False

    fixture = _load_fixture()
    if fixture:
        print("Using saved nordpool fixture (no network calls)")
        rates_agile = fixture["agile_import"]
        rates_agile_export = fixture["agile_export"]
        nordpool_day0 = fixture["nordpool_day0"]
        nordpool_day1 = fixture["nordpool_day1"]
    else:
        # First run: download everything live and save for future runs
        try:
            rates_agile = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/")
            rates_agile_export = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/")
        except ValueError:
            print("WARN: Cannot download Agile rates from Octopus (network or product unavailable), skipping nordpool test")
            return False
        if not rates_agile or not rates_agile_export:
            print("WARN: Empty Agile rates from Octopus, skipping nordpool test")
            return False
        url_template = my_predbat.args["futurerate_url"]
        today = datetime.now()
        tomorrow = today + timedelta(days=1)
        temp_future = FutureRate(my_predbat)
        nordpool_day0 = temp_future.download_futurerate_data_func(url_template.replace("DATE", today.strftime("%Y-%m-%d")))
        nordpool_day1 = temp_future.download_futurerate_data_func(url_template.replace("DATE", tomorrow.strftime("%Y-%m-%d")))
        if nordpool_day0:
            _save_fixture(rates_agile, rates_agile_export, nordpool_day0, nordpool_day1)
        print("Agile rates downloaded...")

    # Wire up the fixture mock before creating FutureRate so the cache is populated correctly
    today = datetime.now()
    tomorrow = today + timedelta(days=1)
    future = FutureRate(my_predbat)
    if fixture:
        nordpool_today_data = _adjust_nordpool_dates(nordpool_day0, today)
        nordpool_tomorrow_data = _adjust_nordpool_dates(nordpool_day1, tomorrow)

        def _mock_nordpool(url):
            """Return date-adjusted fixture data keyed by the date in the URL."""
            if today.strftime("%Y-%m-%d") in url:
                return nordpool_today_data
            if tomorrow.strftime("%Y-%m-%d") in url:
                return nordpool_tomorrow_data
            return {}

        future.download_futurerate_data_func = _mock_nordpool

    rate_import, rate_export = future.futurerate_analysis(rates_agile, rates_agile_export)
    if not rate_import:
        print("ERROR: No rate import data")
        return True
    if not rate_export:
        print("ERROR: No rate export data")
        return True

    future.download_futurerate_data_func = lambda _: ("empty")  # Mock the download function
    rate_import2, rate_export2 = future.futurerate_analysis(rates_agile, rates_agile_export)
    for key in rate_import:
        if rate_import[key] != rate_import2.get(key, None):
            print("ERROR: Rate import data not the same got {} vs {}".format(rate_import[key], rate_import2.get(key, None)))
            failed = True
            break
    for key in rate_export:
        if rate_export[key] != rate_export2.get(key, None):
            print("ERROR: Rate export data not the same got {} vs {}".format(rate_export[key], rate_export2.get(key, None)))
            failed = True
            break

    min_import = min(rate_import.values())
    min_export = min(rate_export.values())
    max_import = max(rate_import.values())
    max_export = max(rate_export.values())

    if min_import == max_import:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_export == max_export:
        print("ERROR: Rate export data is flat")
        failed = True
    if min_import < -15 or max_import > 100:
        print("ERROR: Rate import data out of range got min {} max {}".format(min_import, max_import))
        failed = True
    if min_export < 0 or max_export > 100:
        print("ERROR: Rate export data out of range got min {} max {}".format(min_export, max_export))
        failed = True

    # Compare Agile rates against Nordpool-adjusted rates
    max_diff = 0
    rate_diff = 0
    max_diff_minute = None
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile.get(minute, None)
        rate_nordpool = rate_import.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff = abs(rate_octopus - rate_nordpool)
            if rate_diff > max_diff:
                max_diff = rate_diff
                max_diff_minute = minute
            # print("Import: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, dp2(rate_diff)))
    if max_diff > 20:
        print("ERROR: Rate import data difference too high (max diff {}) minute {}".format(max_diff, my_predbat.time_abs_str(max_diff_minute)))
        failed = True

    rate_diff_export = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile_export.get(minute, None)
        rate_nordpool = rate_export.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff_export = abs(rate_octopus - rate_nordpool)
            max_diff = max(rate_diff_export, rate_diff)
            # print("Export: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if rate_diff_export > 10:
        print("ERROR: Rate export data difference too high (max diff {})".format(rate_diff_export))
        failed = True

    return failed

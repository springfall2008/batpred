# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import re
from datetime import datetime, timedelta, timezone

from web import WebInterface


def make_web(my_predbat):
    """Create a WebInterface instance bound to the given predbat."""
    return WebInterface(my_predbat, web_port=5053)


def parse_timeline_ranges(html):
    """Extract the rendered timeline ranges as a list of (category, label, start_ms, end_ms)."""
    pattern = r"x: '([^']*)',\s*\n\s*y: \[(\d+), (\d+)\],\s*\n\s*fillColor: '[^']*',\s*\n\s*label: '([^']*)'"
    return [(m.group(1), m.group(4), int(m.group(2)), int(m.group(3))) for m in re.finditer(pattern, html)]


def run_web_timeline_fidelity_tests(web):
    """A flapping state must not be erased from the timeline chart, and same-named entities must not share a row."""
    failed = 0

    # -------------------------------------------------------------------------
    # A status sensor that alternates Normal/Lost every minute for six days. The chart used to
    # downsample the raw samples by array index, so whenever the step landed on one phase of the
    # flap the other state vanished entirely and the chart claimed one continuous multi-day run -
    # contradicting the history table, which samples the same data per 5/30-minute bucket.
    print("Test: render_timeline_chart() does not erase a state when downsampling a long flapping history")
    now = datetime(2026, 7, 25, 14, 0, 0, tzinfo=timezone.utc)
    samples = 8640
    flapping = {}
    stamp = now - timedelta(minutes=samples)
    for index in range(samples):
        flapping[stamp.strftime("%Y-%m-%dT%H:%M:%S%z")] = "Lost" if index % 2 else "Normal"
        stamp += timedelta(minutes=1)

    html = web.render_timeline_chart([{"name": "Status", "entity_id": "sensor.inverter_one_status", "data": flapping}], "chart_status", 7)
    ranges = parse_timeline_ranges(html)
    covered = {}
    for _, label, start, end in ranges:
        covered[label] = covered.get(label, 0) + (end - start)
    span = sum(covered.values())
    lost_share = covered.get("Lost", 0) / span if span else 0
    if lost_share < 0.25:
        print(f"  ERROR: 'Lost' holds half the history but the timeline chart gives it {lost_share:.1%} of the span - the other state was aliased over it")
        failed += 1
    longest = max(((end - start) / 60000 for _, label, start, end in ranges if label == "Normal"), default=0)
    if longest > 60:
        print(f"  ERROR: timeline chart reports a continuous {longest:.0f} minute 'Normal' run, but the state never stayed Normal for more than a minute")
        failed += 1

    # -------------------------------------------------------------------------
    # Two inverters both publish a status sensor whose friendly name is "Status", so using the
    # display name as the rangeBar category collapsed both onto a single y-axis row, making it
    # impossible to tell which bar belonged to which inverter.
    print("Test: render_timeline_chart() gives same-named entities distinct rows")
    both = [
        {"name": "Status", "entity_id": "sensor.inverter_one_status", "data": {"2026-07-23T10:00:00+00:00": "Normal"}},
        {"name": "Status", "entity_id": "sensor.inverter_two_status", "data": {"2026-07-23T10:00:00+00:00": "Lost"}},
    ]
    html = web.render_timeline_chart(both, "chart_status", 7)
    categories = {category for category, _, _, _ in parse_timeline_ranges(html)}
    if len(categories) < 2:
        print(f"  ERROR: two entities sharing the friendly name 'Status' collapsed onto one timeline row: {sorted(categories)}")
        failed += 1

    # -------------------------------------------------------------------------
    # get_history_with_now() appends the current state stamped in local time while HA/DB history
    # is UTC, so sorting the raw timestamp strings orders records by their text, not their instant.
    print("Test: render_timeline_chart() orders records by instant, not by raw timestamp string")
    mixed = {
        "2026-07-23T09:00:00+0000": "Normal",
        "2026-07-23T11:30:00+0200": "Lost",  # 09:30 UTC - sorts after "11:00" as text
        "2026-07-23T10:00:00+0000": "Normal",
    }
    html = web.render_timeline_chart([{"name": "Status", "entity_id": "sensor.inverter_one_status", "data": mixed}], "chart_status", 7)
    mixed_ranges = parse_timeline_ranges(html)
    latest_ms = int(datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    covered_to = max((end for _, _, _, end in mixed_ranges), default=0)
    if covered_to != latest_ms:
        print(f"  ERROR: timeline stops at {datetime.fromtimestamp(covered_to / 1000, timezone.utc)} instead of the newest record at 10:00 UTC - the 11:30+0200 record sorted last as text")
        failed += 1
    if [label for _, label, _, _ in mixed_ranges][:2] != ["Normal", "Lost"]:
        print(f"  ERROR: expected the 09:30 UTC 'Lost' record to sort between the 09:00 and 10:00 UTC records, got range labels {[label for _, label, _, _ in mixed_ranges]}")
        failed += 1

    return failed


def run_web_charts_tests(my_predbat):
    """Unit tests for chart rendering - entities with a '%' unit must still be able to chart."""
    failed = 0
    print("**** Running web charts tests ****")

    web = make_web(my_predbat)
    now_str = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    series_data = [{"name": "SoC", "data": {"2026-07-23T10:00:00+00:00": 45.0}, "chart_type": "line"}]

    # -------------------------------------------------------------------------
    print("Test: battery chart JSON and legacy rendering share the same series source")
    original_dashboard_values = my_predbat.dashboard_values
    original_history = my_predbat.soc_kwh_history
    original_soc_kw = my_predbat.soc_kw
    original_history_with_now_attrs = web.get_history_with_now_attrs
    try:
        forecast_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".soc_kw_best": {"attributes": {"results": {forecast_stamp: 6.5}}},
            my_predbat.prefix + ".best_charge_limit_kw": {"attributes": {"results": {forecast_stamp: 8.0}}},
            my_predbat.prefix + ".best_export_limit_kw": {"attributes": {"results": {forecast_stamp: 2.0}}},
        }
        my_predbat.soc_kwh_history = {}
        my_predbat.soc_kw = 6.25
        web.get_history_with_now_attrs = lambda *args, **kwargs: []

        battery_data = web.get_battery_chart_data()
        legacy_html = web.get_chart("Battery")

        if not battery_data["ready"]:
            print("  ERROR: populated optimised forecast should mark battery chart data ready")
            failed += 1
        if battery_data["series"]["optimized"].get(forecast_stamp) != 6.5:
            print(f"  ERROR: optimised series missing from battery chart JSON: {battery_data}")
            failed += 1
        if battery_data["series"]["actual"].get(now_str) != 6.25:
            print(f"  ERROR: current battery value missing from Actual series: {battery_data}")
            failed += 1
        if "name: 'Best'" not in legacy_html or "y: 6.5" not in legacy_html:
            print("  ERROR: legacy chart no longer renders the shared optimised series")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        my_predbat.soc_kwh_history = original_history
        my_predbat.soc_kw = original_soc_kw
        web.get_history_with_now_attrs = original_history_with_now_attrs

    # -------------------------------------------------------------------------
    print("Test: power chart JSON exposes the existing optimised power series")
    original_dashboard_values = my_predbat.dashboard_values
    try:
        forecast_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".battery_power_best": {"attributes": {"results": {forecast_stamp: -2.5}}},
            my_predbat.prefix + ".pv_power_best": {"attributes": {"results": {forecast_stamp: 3.2}}},
            # The prediction engine stores import as positive; the modern chart
            # API normalises it to Predbat's live negative-import convention.
            my_predbat.prefix + ".grid_power_best": {"attributes": {"results": {forecast_stamp: 0.4}}},
            my_predbat.prefix + ".load_power_best": {"attributes": {"results": {forecast_stamp: 1.1}}},
            my_predbat.prefix + ".iboost_best": {"attributes": {"results": {forecast_stamp: 0.25}}},
        }

        power_data = web.get_power_chart_data()

        if not power_data["ready"]:
            print("  ERROR: populated forecast should mark power chart data ready")
            failed += 1
        if power_data["series"]["battery"].get(forecast_stamp) != -2.5:
            print(f"  ERROR: battery power forecast missing from chart JSON: {power_data}")
            failed += 1
        if power_data["series"]["grid"].get(forecast_stamp) != -0.4:
            print(f"  ERROR: grid import was not normalised to negative power: {power_data}")
            failed += 1
        if power_data["series"]["iboost_energy"].get(forecast_stamp) != 0.25:
            print(f"  ERROR: iBoost energy forecast missing from chart JSON: {power_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values

    # -------------------------------------------------------------------------
    print("Test: cost chart JSON exposes actual and optimised costs with currency")
    original_dashboard_values = my_predbat.dashboard_values
    original_currency_symbols = my_predbat.currency_symbols
    try:
        forecast_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".cost_today": {"attributes": {"results": {forecast_stamp: 125.0}}},
            my_predbat.prefix + ".metric": {"attributes": {"results": {forecast_stamp: 240.0}}},
            my_predbat.prefix + ".best_metric": {"attributes": {"results": {forecast_stamp: 180.0}}},
        }

        for currency_symbols, expected_major, expected_minor in (("£p", "£", "p"), ("$c", "$", "c"), (["€", "c"], "€", "c")):
            my_predbat.currency_symbols = currency_symbols
            cost_data = web.get_cost_chart_data()

            if not cost_data["ready"]:
                print("  ERROR: populated optimised forecast should mark cost chart data ready")
                failed += 1
            if cost_data["series"]["actual"].get(forecast_stamp) != 125.0:
                print(f"  ERROR: actual cost missing from chart JSON: {cost_data}")
                failed += 1
            if cost_data["series"]["optimized"].get(forecast_stamp) != 180.0:
                print(f"  ERROR: optimised cost missing from chart JSON: {cost_data}")
                failed += 1
            if cost_data["currency_symbol"] != expected_major or cost_data["currency_unit"] != expected_minor:
                print(f"  ERROR: cost chart currency metadata is incorrect for {currency_symbols}: {cost_data}")
                failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        my_predbat.currency_symbols = original_currency_symbols

    # -------------------------------------------------------------------------
    print("Test: rates chart JSON exposes tariff series and configured currency")
    original_dashboard_values = my_predbat.dashboard_values
    original_currency_symbols = my_predbat.currency_symbols
    original_history_wrapper = web.get_history_wrapper
    try:
        forecast_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.currency_symbols = ["€", "c"]
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".rates": {"attributes": {"results": {forecast_stamp: 21.5}}},
            my_predbat.prefix + ".rates_export": {"attributes": {"results": {forecast_stamp: 12.0}}},
            my_predbat.prefix + ".rates_gas": {"attributes": {"results": {forecast_stamp: 7.25}}},
        }
        web.get_history_wrapper = lambda *args, **kwargs: [[{"state": 18.5, "last_updated": forecast_stamp}]]

        rates_data = web.get_rates_chart_data()

        if not rates_data["ready"]:
            print("  ERROR: populated tariffs should mark rates chart data ready")
            failed += 1
        if rates_data["series"]["import"].get(forecast_stamp) != 21.5:
            print(f"  ERROR: import tariff missing from rates chart JSON: {rates_data}")
            failed += 1
        if rates_data["series"]["export"].get(forecast_stamp) != 12.0:
            print(f"  ERROR: export tariff missing from rates chart JSON: {rates_data}")
            failed += 1
        if rates_data["currency_symbol"] != "€" or rates_data["currency_unit"] != "c":
            print(f"  ERROR: rates chart currency metadata is incorrect: {rates_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        my_predbat.currency_symbols = original_currency_symbols
        web.get_history_wrapper = original_history_wrapper

    # -------------------------------------------------------------------------
    print("Test: in-day chart JSON exposes cumulative load forecasts and adjustment history")
    original_dashboard_values = my_predbat.dashboard_values
    original_history_wrapper = web.get_history_wrapper
    try:
        forecast_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".load_energy_actual": {"attributes": {"results": {forecast_stamp: 4.2}}},
            my_predbat.prefix + ".load_energy_predicted": {"attributes": {"results": {forecast_stamp: 8.5}}},
            my_predbat.prefix + ".load_energy_adjusted": {"attributes": {"results": {forecast_stamp: 9.1}}},
        }
        web.get_history_wrapper = lambda *args, **kwargs: [[{"state": 7.5, "last_updated": forecast_stamp}]]

        inday_data = web.get_inday_chart_data()

        if not inday_data["ready"]:
            print("  ERROR: populated cumulative load data should mark in-day chart data ready")
            failed += 1
        if inday_data["series"]["actual"].get(forecast_stamp) != 4.2:
            print(f"  ERROR: actual cumulative load missing from in-day chart JSON: {inday_data}")
            failed += 1
        if inday_data["series"]["adjusted"].get(forecast_stamp) != 9.1:
            print(f"  ERROR: adjusted load forecast missing from in-day chart JSON: {inday_data}")
            failed += 1
        if 7.5 not in inday_data["series"]["adjustment_factor"].values():
            print(f"  ERROR: in-day adjustment history missing from chart JSON: {inday_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        web.get_history_wrapper = original_history_wrapper

    # -------------------------------------------------------------------------
    print("Test: solar chart JSON combines seven-day history with today and tomorrow forecasts")
    original_dashboard_values = my_predbat.dashboard_values
    original_history_wrapper = web.get_history_wrapper
    try:
        history_stamp = (my_predbat.now_utc - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S%z")
        today_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        tomorrow_stamp = (my_predbat.now_utc + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            "sensor." + my_predbat.prefix + "_pv_today": {"attributes": {"detailedForecast": [{"period_start": today_stamp, "pv_estimate": 2.1, "pv_estimate10": 1.4, "pv_estimate90": 2.8, "pv_estimateCL": 2.3}]}},
            "sensor." + my_predbat.prefix + "_pv_tomorrow": {"attributes": {"detailedForecast": [{"period_start": tomorrow_stamp, "pv_estimate": 3.0, "pv_estimate10": 2.0, "pv_estimate90": 3.8, "pv_estimateCL": 3.2}]}},
        }

        def solar_history(entity, *args, **kwargs):
            if entity == my_predbat.prefix + ".pv_power":
                return [[{"state": 1.9, "last_updated": history_stamp}]]
            if entity == my_predbat.prefix + ".pv_energy_h0":
                return [[{"state": 4.6, "last_updated": history_stamp}]]
            if entity == "sensor." + my_predbat.prefix + "_pv_today":
                return [[{"state": 8.0, "last_updated": history_stamp, "attributes": {"totalCL": 8.0, "remainingCL": 3.1}}]]
            return [[{"state": 1.8, "last_updated": history_stamp, "attributes": {"nowCL": 2.0}}]]

        web.get_history_wrapper = solar_history
        solar_data = web.get_solar_chart_data()

        if not solar_data["ready"]:
            print("  ERROR: populated measured and forecast solar data should mark the chart ready")
            failed += 1
        if 1.9 not in solar_data["series"]["actual"].values():
            print(f"  ERROR: measured solar history missing from chart JSON: {solar_data}")
            failed += 1
        if 2.3 not in solar_data["series"]["forecast_calibrated"].values():
            print(f"  ERROR: today's calibrated solar forecast missing from chart JSON: {solar_data}")
            failed += 1
        if 3.2 not in solar_data["series"]["forecast_calibrated"].values():
            print(f"  ERROR: tomorrow's calibrated solar forecast missing from chart JSON: {solar_data}")
            failed += 1
        if 2.0 not in solar_data["series"]["forecast_history_calibrated"].values():
            print(f"  ERROR: calibrated forecast history missing from chart JSON: {solar_data}")
            failed += 1
        if 4.6 not in solar_data["series"]["energy_actual"].values() or 4.9 not in solar_data["series"]["energy_forecast"].values():
            print(f"  ERROR: cumulative solar accuracy series missing from chart JSON: {solar_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        web.get_history_wrapper = original_history_wrapper

    # -------------------------------------------------------------------------
    print("Test: savings chart JSON exposes daily and cumulative values in major currency units")
    original_currency_symbols = my_predbat.currency_symbols
    original_history_wrapper = web.get_history_wrapper
    total_entities = {
        my_predbat.prefix + ".savings_total_predbat": 301,
        my_predbat.prefix + ".savings_total_pvbat": 450,
    }
    original_total_states = {entity: my_predbat.ha_interface.dummy_items.get(entity) for entity in total_entities}
    try:
        history_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.currency_symbols = ["€", "c"]
        web.get_history_wrapper = lambda entity, *args, **kwargs: [] if ".savings_total_" in entity else [[{"state": 125, "last_updated": history_stamp}]]
        for entity, value in total_entities.items():
            my_predbat.ha_interface.dummy_items[entity] = {"state": value, "attributes": {}}

        savings_data = web.get_savings_chart_data()

        if not savings_data["ready"]:
            print("  ERROR: populated savings history should mark the chart ready")
            failed += 1
        if 1.25 not in savings_data["series"]["daily_predbat"].values():
            print(f"  ERROR: savings chart did not convert minor units to major units: {savings_data}")
            failed += 1
        if 3.01 not in savings_data["series"]["total_predbat"].values() or 4.5 not in savings_data["series"]["total_pv_battery"].values():
            print(f"  ERROR: current cumulative savings are missing when recorder history is empty: {savings_data}")
            failed += 1
        if savings_data["currency_symbol"] != "€":
            print(f"  ERROR: savings chart currency metadata is incorrect: {savings_data}")
            failed += 1
    finally:
        my_predbat.currency_symbols = original_currency_symbols
        web.get_history_wrapper = original_history_wrapper
        for entity, value in original_total_states.items():
            if value is None:
                my_predbat.ha_interface.dummy_items.pop(entity, None)
            else:
                my_predbat.ha_interface.dummy_items[entity] = value

    # -------------------------------------------------------------------------
    print("Test: battery degradation chart JSON includes history and today's sensor values")
    original_dashboard_values = my_predbat.dashboard_values
    original_history_wrapper = web.get_history_wrapper
    original_battery_scaling_auto = my_predbat.battery_scaling_auto
    try:
        sensor_id = "sensor." + my_predbat.prefix + "_soc_max_calculated"
        history_stamp = (my_predbat.now_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            sensor_id: {
                "state": 9.2,
                "attributes": {"nominal_capacity": 10.0, "degradation_percent": 8.0},
            }
        }
        my_predbat.battery_scaling_auto = True
        web.get_history_wrapper = lambda *args, **kwargs: [
            [
                {
                    "state": 9.3,
                    "last_updated": history_stamp,
                    "attributes": {"nominal_capacity": 10.0, "degradation_percent": 7.0},
                }
            ]
        ]

        degradation_data = web.get_battery_degradation_chart_data()
        inverter = degradation_data["inverters"][0]
        today = my_predbat.now_utc.strftime("%Y-%m-%d")

        if not degradation_data["ready"] or inverter["calculated"].get(today) != 9.2:
            print(f"  ERROR: current calculated capacity missing from degradation chart JSON: {degradation_data}")
            failed += 1
        if inverter["nominal"].get(today) != 10.0 or inverter["degradation"].get(today) != 8.0:
            print(f"  ERROR: current battery attributes missing from degradation chart JSON: {degradation_data}")
            failed += 1
        if not degradation_data["automatic_scaling"]:
            print(f"  ERROR: battery scaling state missing from degradation chart JSON: {degradation_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        my_predbat.battery_scaling_auto = original_battery_scaling_auto
        web.get_history_wrapper = original_history_wrapper

    # -------------------------------------------------------------------------
    print("Test: marginal costs chart JSON combines history and forecast")
    original_dashboard_values = my_predbat.dashboard_values
    original_history_with_now_attrs = web.get_history_with_now_attrs
    try:
        sensor_id = "sensor." + my_predbat.prefix + "_marginal_energy_costs"
        time_labels = [(my_predbat.now_utc + timedelta(hours=hours)).strftime("%H:%M") for hours in range(0, 13, 2)]
        my_predbat.dashboard_values = {
            sensor_id: {
                "attributes": {
                    "matrix": {kwh: {stamp: kwh * 10 + index for index, stamp in enumerate(time_labels)} for kwh in (1, 2, 4, 8)},
                    "grid_import": {stamp: 20 + index for index, stamp in enumerate(time_labels)},
                    "grid_export": {stamp: 5 + index for index, stamp in enumerate(time_labels)},
                    "rate_now_low_consumption": 10,
                    "rate_now_med_consumption": 20,
                    "rate_now_high_consumption": 40,
                    "rate_now_ev_consumption": 80,
                }
            },
            "binary_sensor.{}_marginal_rate_now_low_is_cheap".format(my_predbat.prefix): {"state": "on"},
        }
        web.get_history_with_now_attrs = lambda *args, **kwargs: []

        marginal_data = web.get_marginal_costs_chart_data()

        if not marginal_data["ready"] or marginal_data["levels"][0]["current_cost"] != 10:
            print(f"  ERROR: marginal cost summary is incomplete: {marginal_data}")
            failed += 1
        if len(marginal_data["levels"][0]["series"]) != 7 or len(marginal_data["grid_import"]) != 7:
            print(f"  ERROR: marginal forecast series is incomplete: {marginal_data}")
            failed += 1
        if not marginal_data["levels"][0]["cheap"] or marginal_data["currency_unit"] != my_predbat.currency_symbols[1]:
            print(f"  ERROR: marginal cost metadata is incomplete: {marginal_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        web.get_history_with_now_attrs = original_history_with_now_attrs

    # -------------------------------------------------------------------------
    print("Test: carbon chart JSON exposes actual, base, optimised and intensity series")
    original_dashboard_values = my_predbat.dashboard_values
    original_carbon_intensity = my_predbat.carbon_intensity
    try:
        forecast_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            my_predbat.prefix + ".carbon_today": {"attributes": {"results": {forecast_stamp: 8000}}},
            my_predbat.prefix + ".carbon": {"attributes": {"results": {forecast_stamp: 9200}}},
            my_predbat.prefix + ".carbon_best": {"attributes": {"results": {forecast_stamp: 8700}}},
        }
        my_predbat.carbon_intensity = {0: 180, 30: 150}

        carbon_data = web.get_carbon_chart_data()

        if not carbon_data["ready"] or carbon_data["series"]["actual"].get(forecast_stamp) != 8000:
            print(f"  ERROR: actual household carbon is missing from chart JSON: {carbon_data}")
            failed += 1
        if carbon_data["series"]["base"].get(forecast_stamp) != 9200 or carbon_data["series"]["optimized"].get(forecast_stamp) != 8700:
            print(f"  ERROR: carbon forecasts are missing from chart JSON: {carbon_data}")
            failed += 1
        if sorted(carbon_data["series"]["intensity"].values()) != [150, 180]:
            print(f"  ERROR: grid carbon intensity is missing from chart JSON: {carbon_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        my_predbat.carbon_intensity = original_carbon_intensity

    # -------------------------------------------------------------------------
    print("Test: load ML chart JSON exposes energy, converted power and car-adjusted load")
    original_dashboard_values = my_predbat.dashboard_values
    original_history_wrapper = web.get_history_wrapper
    original_history_with_now_attrs = web.get_history_with_now_attrs
    original_car_configured = my_predbat.car_charging_power_configured
    try:
        first_stamp = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        second_stamp = (my_predbat.now_utc + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S%z")
        my_predbat.dashboard_values = {
            "sensor." + my_predbat.prefix + "_load_ml_forecast": {"attributes": {"results": {first_stamp: 1.0, second_stamp: 1.5}}},
            my_predbat.prefix + ".pv_power_best": {"attributes": {"results": {second_stamp: 2.5}}},
            "sensor." + my_predbat.prefix + "_temperature": {"attributes": {"results": {second_stamp: 12.0}}},
        }
        stats = [[{"last_updated": first_stamp, "attributes": {"load_today": 3.0, "load_today_h1": 2.8, "load_today_h8": 2.6, "power_today": 0.8, "power_today_h1": 0.7, "power_today_h8": 0.6}}]]
        web.get_history_with_now_attrs = lambda *args, **kwargs: stats

        def load_ml_history(entity, *args, **kwargs):
            value = 1.2 if entity.endswith(".load_power") else 0.4 if entity.endswith(".car_charging_power") else 2.0
            return [[{"state": value, "last_updated": first_stamp}]]

        web.get_history_wrapper = load_ml_history
        my_predbat.car_charging_power_configured = True
        load_ml_data = web.get_load_ml_chart_data()

        if not load_ml_data["ready"] or 3.0 not in load_ml_data["series"]["energy_actual"].values():
            print(f"  ERROR: learned load energy is missing from chart JSON: {load_ml_data}")
            failed += 1
        if load_ml_data["series"]["power_forecast"].get(second_stamp) != 1.0:
            print(f"  ERROR: cumulative ML energy was not converted to interval power: {load_ml_data}")
            failed += 1
        if 0.8 not in load_ml_data["series"]["power_actual_less_car"].values() or not load_ml_data["car_configured"]:
            print(f"  ERROR: configured car power was not removed from measured load: {load_ml_data}")
            failed += 1
    finally:
        my_predbat.dashboard_values = original_dashboard_values
        web.get_history_wrapper = original_history_wrapper
        web.get_history_with_now_attrs = original_history_with_now_attrs
        my_predbat.car_charging_power_configured = original_car_configured

    # -------------------------------------------------------------------------
    print("Test: render_chart() targets a percent-unit tagname via getElementById, not a CSS id selector")
    html = web.render_chart(series_data, "%", "SoC Chart", now_str, tagname="chart_%")
    if "querySelector('#" in html or 'querySelector("#' in html:
        print("  ERROR: render_chart() still targets the chart element via a '#id' CSS selector, which throws for a tagname like 'chart_%'")
        failed += 1
    if "getElementById('chart_%')" not in html:
        print(f"  ERROR: expected render_chart() to call getElementById('chart_%'), got: {html}")
        failed += 1

    # -------------------------------------------------------------------------
    # daily_chart=False (Savings, BatteryDegradation, Tariff Comparison) computed width as
    # window.innerWidth / 3 * 2 with no lower bound - on a phone-width HA Companion App webview
    # (~400px) that renders a ~270px chart, too narrow to fit a title, dual Y-axis labels and a
    # verbose legend without everything overlapping (#4561). daily_chart=True already has this
    # floor; daily_chart=False needs the same one.
    print("Test: render_chart(daily_chart=False) has the same minimum-width floor as daily_chart=True")
    html_daily_true = web.render_chart(series_data, "%", "SoC Chart", now_str, tagname="chart_a", daily_chart=True)
    html_daily_false = web.render_chart(series_data, "%", "Savings Chart", now_str, tagname="chart_b", daily_chart=False)
    if "if (width < 600)" not in html_daily_true:
        print("  ERROR: test setup assumption wrong - daily_chart=True no longer has the width floor")
        failed += 1
    if "if (width < 600)" not in html_daily_false:
        print(f"  ERROR: render_chart(daily_chart=False) is missing the width < 600 floor that daily_chart=True has - narrow viewports get an unreadably small chart, got: {html_daily_false}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: render_timeline_chart() targets a percent-unit tagname via getElementById, not a CSS id selector")
    timeline_data = [{"name": "Status", "entity_id": "sensor.x", "data": {"2026-07-23T10:00:00+00:00": "on"}}]
    html = web.render_timeline_chart(timeline_data, "chart_%", 7)
    if "querySelector('#" in html or 'querySelector("#' in html:
        print("  ERROR: render_timeline_chart() still targets the chart element via a '#id' CSS selector, which throws for a tagname like 'chart_%'")
        failed += 1
    if "getElementById('chart_%')" not in html:
        print(f"  ERROR: expected render_timeline_chart() to call getElementById('chart_%'), got: {html}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: render_heatmap_chart() targets a percent-unit chart_id via getElementById, not a CSS id selector")
    html = web.render_heatmap_chart([{"name": "SoC", "data": [{"x": "Mon", "y": 45.0}]}], "SoC Heatmap", 0, 100, chart_id="chart_%")
    if "querySelector('#" in html or 'querySelector("#' in html:
        print("  ERROR: render_heatmap_chart() still targets the chart element via a '#id' CSS selector, which throws for a chart_id like 'chart_%'")
        failed += 1
    if "getElementById('chart_%')" not in html:
        print(f"  ERROR: expected render_heatmap_chart() to call getElementById('chart_%'), got: {html}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: render_heatmap_chart() sanitises chart_id before using it as a JS variable name")
    for bad_variable_name in ("height_chart_%", "chart_chart_%"):
        if bad_variable_name in html:
            print(f"  ERROR: render_heatmap_chart() interpolated an unsanitised chart_id into a JS identifier ('{bad_variable_name}'), which is a syntax error")
            failed += 1
    if "height_chart__" not in html:
        print(f"  ERROR: expected render_heatmap_chart() to declare a sanitised 'height_chart__' variable, got: {html}")
        failed += 1
    if "chart_chart__.render()" not in html:
        print(f"  ERROR: expected render_heatmap_chart() to render via a sanitised 'chart_chart__' variable, got: {html}")
        failed += 1

    failed += run_web_timeline_fidelity_tests(web)

    return failed

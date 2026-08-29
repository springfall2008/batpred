# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates, update_rates_import


def run_test_plan_json_rate_adjust(my_predbat):
    """
    Test that import_rate_adjust_type / export_rate_adjust_type fields
    in the JSON plan output match the underlying rate_*_replicated values,
    are omitted when None, and are present when an adjustment exists.
    """
    print("**** Running plan JSON rate adjust type tests ****")
    failed = False

    # --- Test 1: adjust_symbol mapping ---
    print("Test adjust_symbol mapping")
    expected_symbols = {
        "offset": "? &#8518;",
        "future": "? &#x2696;",
        "user": "&#61;",
        "manual": "&#8526;",
        "increment": "&#177;",
        "saving": "&dollar;",
        "unknown_type": "?",
    }
    for adjust_type, expected in expected_symbols.items():
        result = my_predbat.adjust_symbol(adjust_type)
        if result != expected:
            print("ERROR: adjust_symbol('{}') expected '{}' got '{}'".format(adjust_type, expected, result))
            failed = True

    if my_predbat.adjust_symbol(None) != "":
        print("ERROR: adjust_symbol(None) should return empty string")
        failed = True

    if my_predbat.adjust_symbol("") != "":
        print("ERROR: adjust_symbol('') should return empty string")
        failed = True

    # --- Test 2: JSON plan rows via publish_html_plan ---
    print("Test plan JSON output with rate_adjust_type fields")

    # Set up minimal plan state (following test_optimise_all_windows pattern)
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 48 * 60
    my_predbat.debug_enable = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.num_inverters = 1
    my_predbat.reserve = 0.5
    my_predbat.set_charge_freeze = True

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0
        load_step[minute] = 0.5 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]
    export_window_best = []
    reset_rates(my_predbat, 10.0, 5.0)
    update_rates_import(my_predbat, charge_window_best)

    charge_limit_best = [0]
    export_limits_best = []

    # Run prediction with save="best" to populate all plan attributes
    my_predbat.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=my_predbat.end_record, save="best")
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    # Set specific replicated rate types for known minutes
    test_minute = my_predbat.minutes_now
    my_predbat.rate_import_replicated = {test_minute: "future"}
    my_predbat.rate_export_replicated = {test_minute: "manual"}

    html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)

    if not raw_plan or "rows" not in raw_plan:
        print("ERROR: raw_plan has no rows")
        failed = True
    else:
        rows = raw_plan["rows"]
        if len(rows) == 0:
            print("ERROR: raw_plan has zero rows")
            failed = True

        # Find row with our adjusted minute
        adjusted_row = None
        non_adjusted_rows = []
        for row in rows:
            if row.get("slot_minute") == test_minute:
                adjusted_row = row
            elif "import_rate_adjust_type" not in row and "export_rate_adjust_type" not in row:
                non_adjusted_rows.append(row)

        # Verify adjusted row has correct type values
        if adjusted_row is None:
            print("WARNING: Could not find row for minute {} in plan output".format(test_minute))
        else:
            if adjusted_row.get("import_rate_adjust_type") != "future":
                print("ERROR: Expected import_rate_adjust_type='future' got '{}'".format(adjusted_row.get("import_rate_adjust_type")))
                failed = True
            if adjusted_row.get("export_rate_adjust_type") != "manual":
                print("ERROR: Expected export_rate_adjust_type='manual' got '{}'".format(adjusted_row.get("export_rate_adjust_type")))
                failed = True

        # Verify non-adjusted rows omit the keys entirely (attribute bloat prevention)
        if len(non_adjusted_rows) > 0:
            sample = non_adjusted_rows[0]
            if "import_rate_adjust_type" in sample:
                print("ERROR: Non-adjusted row should not contain import_rate_adjust_type key (attribute bloat)")
                failed = True
            if "export_rate_adjust_type" in sample:
                print("ERROR: Non-adjusted row should not contain export_rate_adjust_type key (attribute bloat)")
                failed = True
        else:
            print("WARNING: No non-adjusted rows found to verify key omission")

    # Clean up
    my_predbat.rate_import_replicated = {}
    my_predbat.rate_export_replicated = {}

    # --- Test 3: car rate diverging from the house rate (batpred#4646) ---
    print("Test plan JSON output with a car rate that diverges from the house rate")
    my_predbat.num_cars = 1
    car_minute = my_predbat.minutes_now
    my_predbat.car_charging_slots[0] = [{"start": car_minute, "end": car_minute + 30, "kwh": 3.0, "average": 28.0, "cost": 84.0, "soc": 0.0, "octopus": True}]

    html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
    car_row = next((row for row in raw_plan["rows"] if row.get("slot_minute") == car_minute), None)
    if car_row is None:
        print("WARNING: Could not find row for car minute {} in plan output".format(car_minute))
    else:
        if car_row.get("car_rate") != 28.0:
            print("ERROR: Expected car_rate=28.0 got {}".format(car_row.get("car_rate")))
            failed = True
        if car_row.get("rate_split") is not True:
            print("ERROR: Expected rate_split=True when car rate (28.0) diverges from house rate (10.0), got {}".format(car_row.get("rate_split")))
            failed = True
        if not car_row.get("car_rate_color"):
            print("ERROR: Expected car_rate_color to be set when rate_split is True")
            failed = True
        if "House rate: 10.00" not in html_plan or "Car rate: 28.00" not in html_plan:
            print("ERROR: Expected split-cell HTML with house and car rate tooltips, got:\n{}".format(html_plan))
            failed = True
        if "differs from house rate" not in html_plan:
            print("ERROR: Expected the car tooltip to use neutral 'differs from house rate' wording (not every divergence is an IOG cap), got:\n{}".format(html_plan))
            failed = True

    # currency_symbols is user-configurable free text - a value carrying a double-quote must not
    # break out of the split cell's title="..." attribute in the server-rendered plan (batpred#4647
    # review). Checking for the specific escaped form within the title, not a blanket string search -
    # currency_symbols is also embedded unescaped elsewhere on the page (e.g. the Cost cell text,
    # pre-existing and out of scope for this fix), which would give a false pass/fail either way.
    saved_currency_symbols = my_predbat.currency_symbols
    breakout = '"><script>alert(1)</script>'
    my_predbat.currency_symbols = ["£", "p" + breakout]
    html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
    expected_escaped = "House rate: 10.00p&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;/kWh"
    if expected_escaped not in html_plan:
        print("ERROR: expected the split-cell title to HTML-escape currency_symbols, wanted:\n{}\ngot:\n{}".format(expected_escaped, html_plan))
        failed = True
    my_predbat.currency_symbols = saved_currency_symbols

    # Same car window, but priced the same as the house rate - must not split
    my_predbat.car_charging_slots[0] = [{"start": car_minute, "end": car_minute + 30, "kwh": 3.0, "average": 10.0, "cost": 30.0, "soc": 0.0, "octopus": True}]
    html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
    car_row = next((row for row in raw_plan["rows"] if row.get("slot_minute") == car_minute), None)
    if car_row is not None and car_row.get("rate_split") is not False:
        print("ERROR: Expected rate_split=False when car rate matches house rate, got {}".format(car_row.get("rate_split")))
        failed = True

    # A window with no "average" key at all (non-Octopus historical reconstruction in
    # calculate_yesterday() appends slots like this from the car's own energy sensor) must not be
    # treated as a free/0p charge - that would drag the weighted average down and falsely flag
    # ordinary charging as diverging from the house rate.
    my_predbat.car_charging_slots[0] = [{"start": car_minute, "end": car_minute + 30, "kwh": 3.0, "octopus": False}]
    rate = my_predbat.car_charge_slot_rate(car_minute, car_minute + 30)
    if rate is not None:
        print("ERROR: Expected car_charge_slot_rate to skip a window with no average key, got {}".format(rate))
        failed = True

    # Clean up
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots[0] = []

    if not failed:
        print("All plan JSON rate adjust type tests passed")
    return failed

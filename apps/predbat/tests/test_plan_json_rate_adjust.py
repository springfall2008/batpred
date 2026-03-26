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

    if not failed:
        print("All plan JSON rate adjust type tests passed")
    return failed

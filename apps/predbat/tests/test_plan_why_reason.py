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
from utils import calc_percent_limit


def _setup_baseline(my_predbat):
    """
    Common baseline setup shared by every scenario in this module, following
    the pattern established in test_plan_json_rate_adjust.py: run a single
    real prediction to populate all plan attributes, then let each scenario
    freely override charge/export windows and predict_soc_best afterwards.
    """
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
    my_predbat.reserve_percent = calc_percent_limit(my_predbat.reserve, my_predbat.soc_max)
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

    baseline_charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]
    reset_rates(my_predbat, 10.0, 5.0)
    update_rates_import(my_predbat, baseline_charge_window)

    # Populate predict_soc_best/predict_metric_best/etc with a real (throwaway) prediction run
    my_predbat.run_prediction([0], baseline_charge_window, [], [], False, end_record=my_predbat.end_record, save="best")

    return pv_step, load_step


def _flat_soc(my_predbat, soc_kwh):
    """Build a predict_soc_best dict that is flat at soc_kwh across the whole forecast."""
    return {minute: soc_kwh for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes + 5, 5)}


def _get_row(raw_plan, slot_minute):
    for row in raw_plan["rows"]:
        if row.get("slot_minute") == slot_minute:
            return row
    return None


def run_test_plan_why_reason(my_predbat):
    """
    Test the per-slot "why" reason text in the JSON plan output (json_row["reason"]),
    gated behind the plan_why_explanations switch: verify it's entirely absent when
    the switch is off (zero overhead), and that each plan-slot state category
    (charge / hold charge / freeze charge / export / hold export / freeze export /
    manual overrides / no-window "demand" default) produces the expected plain
    English sentence category.
    """
    print("**** Running plan why-reason tests ****")
    failed = False

    pv_step, load_step = _setup_baseline(my_predbat)
    minutes_now = my_predbat.minutes_now
    window = [{"start": minutes_now, "end": minutes_now + 30, "average": 10.0}]

    def render():
        return my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)

    # --- Test 1: switch off -> no reason key at all (zero overhead) ---
    print("Test switch off omits reason key")
    my_predbat.plan_why_explanations = False
    my_predbat.charge_window_best = window
    my_predbat.charge_limit_best = [8.0]
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.manual_charge_times = []
    my_predbat.manual_freeze_charge_times = []
    my_predbat.manual_export_times = []
    my_predbat.manual_freeze_export_times = []
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 2.0)
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None:
        print("ERROR: could not find row for minutes_now")
        failed = True
    elif "reason" in row:
        print("ERROR: reason key should be absent when plan_why_explanations is off")
        failed = True

    my_predbat.plan_why_explanations = True

    # --- Test 2: Chrg ---
    print("Test Chrg reason")
    my_predbat.charge_window_best = window
    my_predbat.charge_limit_best = [8.0]  # 80% target, not the reserve level
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 2.0)  # 20%, well below the 80% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: Chrg scenario missing reason")
        failed = True
    elif "Charging up to 80" not in row["reason"]:
        print("ERROR: Chrg reason unexpected: {}".format(row.get("reason")))
        failed = True

    # --- Test 3: HoldChrg ---
    print("Test HoldChrg reason")
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)  # 90%, already above the 80% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: HoldChrg scenario missing reason")
        failed = True
    elif "Holding" not in row["reason"]:
        print("ERROR: HoldChrg reason unexpected: {}".format(row.get("reason")))
        failed = True

    # --- Test 4: FrzChrg ---
    print("Test FrzChrg reason")
    my_predbat.charge_limit_best = [my_predbat.reserve]  # target == reserve level
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: FrzChrg scenario missing reason")
        failed = True
    elif "Freeze charging" not in row["reason"]:
        print("ERROR: FrzChrg reason unexpected: {}".format(row.get("reason")))
        failed = True

    # --- Test 5: manual charge override ---
    print("Test manual charge override reason")
    my_predbat.charge_limit_best = [8.0]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 2.0)
    my_predbat.manual_charge_times = [minutes_now]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: manual charge scenario missing reason")
        failed = True
    elif "You manually set this slot to charge" not in row["reason"]:
        print("ERROR: manual charge reason unexpected: {}".format(row.get("reason")))
        failed = True
    my_predbat.manual_charge_times = []

    # --- Test 6: Exp ---
    print("Test Exp reason")
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = window
    my_predbat.export_limits_best = [50.0]  # 50% target
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)  # 90%, well above the 50% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: Exp scenario missing reason")
        failed = True
    elif "Exporting down to" not in row["reason"]:
        print("ERROR: Exp reason unexpected: {}".format(row.get("reason")))
        failed = True

    # --- Test 7: HoldExp ---
    print("Test HoldExp reason")
    my_predbat.export_limits_best = [95.0]  # 95% target, unreachable this window
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)  # 50%, well below the 95% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: HoldExp scenario missing reason")
        failed = True
    elif "not triggered" not in row["reason"]:
        print("ERROR: HoldExp reason unexpected: {}".format(row.get("reason")))
        failed = True

    # --- Test 8: FrzExp ---
    print("Test FrzExp reason")
    my_predbat.export_limits_best = [99]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: FrzExp scenario missing reason")
        failed = True
    elif "Freezing export" not in row["reason"]:
        print("ERROR: FrzExp reason unexpected: {}".format(row.get("reason")))
        failed = True

    # --- Test 9: manual export override ---
    print("Test manual export override reason")
    my_predbat.export_limits_best = [50.0]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)
    my_predbat.manual_export_times = [minutes_now]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: manual export scenario missing reason")
        failed = True
    elif "You manually set this slot to export" not in row["reason"]:
        print("ERROR: manual export reason unexpected: {}".format(row.get("reason")))
        failed = True
    my_predbat.manual_export_times = []

    # --- Test 10: Demand (no charge or export window active) ---
    print("Test Demand default reason")
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)  # perfectly flat -> steady
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "reason" not in row:
        print("ERROR: Demand scenario missing reason")
        failed = True
    elif "steady" not in row["reason"]:
        print("ERROR: Demand reason unexpected: {}".format(row.get("reason")))
        failed = True

    # Clean up
    my_predbat.plan_why_explanations = False

    if not failed:
        print("All plan why-reason tests passed")
    return failed

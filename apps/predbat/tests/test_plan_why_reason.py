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
import warnings
from datetime import timedelta

import web_helper
from const import CAR_CHARGING_LIMIT_UNCAPPED
from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates, update_rates_import
from utils import calc_percent_limit
from web_helper import get_plan_renderer_js


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


def _codes(row):
    return [entry["code"] for entry in row.get("reasons", [])]


def _render(row, templates):
    """
    Mirror of the client-side renderReasonText() in web_helper.py: fill in each reason
    entry's template with its params, join with a space, prefixing "Then" onto the second half
    of a split (demand_before_export_* or hold_for_car_before_export first half). Used here to verify the code/params/template contract
    produces the expected human-readable text end-to-end, not just that the right code was picked.
    """
    reasons = row.get("reasons", [])
    rendered = []
    for entry in reasons:
        template = templates.get(entry["code"])
        if not template:
            rendered.append("")
            continue
        text = re.sub(r"\{(\w+)\}", lambda m: str(entry["params"].get(m.group(1), m.group(0))), template)
        rendered.append(text)
    if len(reasons) >= 2 and rendered[0] and rendered[1] and (reasons[0]["code"].startswith("demand_before_export_") or reasons[0]["code"] == "hold_for_car_before_export"):
        rendered[1] = "Then " + rendered[1][0].lower() + rendered[1][1:]
    return " ".join(part for part in rendered if part)


# State the car hold tests set on the shared fixture - saved and restored around them (#5079 class of
# leak). The prediction tests run real saving predictions, which replace the *_best arrays the rest of
# this module renders from, so those are saved too.
_CAR_HOLD_FIELDS = (
    "num_cars",
    "car_charging_slots",
    "car_charging_from_battery",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_limit",
    "car_charging_limit_model",
    "car_charging_loss",
    "car_charging_battery_size",
    "car_charging_now",
    "car_charging_now_slots",
    "car_charging_rate",
    "set_charge_window",
    "prediction",
    "predict_soc_best",
    "predict_metric_best",
    "predict_iboost_best",
    "predict_carbon_best",
    "predict_clipped_best",
    "predict_car_hold_best",
)


def _set_car(my_predbat, slots, soc=0.0, limit=100.0, from_battery=False, charging_now=False, limit_model=None):
    """Put one car on the fixture with explicit SoC, limit and charging-now state, so no test inherits another's."""
    my_predbat.num_cars = 1
    my_predbat.car_charging_from_battery = from_battery
    my_predbat.car_charging_slots = [slots]
    my_predbat.car_charging_soc = [soc]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_limit = [limit]
    my_predbat.car_charging_limit_model = limit_model
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.car_charging_now = [charging_now]
    my_predbat.car_charging_now_slots = [[]]
    my_predbat.car_charging_rate = [7.0]


def _car_hold(minutes):
    """The predict_car_hold_best a prediction records for a car held at these relative minutes."""
    return {minute: True for minute in minutes}


def _predict_car_hold(my_predbat, pv_step, load_step, export_window=None, export_limits=None, car_slots=None, save="best"):
    """
    Run one prediction from the fixture's car state, as the live plan does, and return the car hold it
    recorded: predict_car_hold_best as copied back onto PredBat for a run that publishes the plan, the
    Prediction's own for any other (which plan.py never copies back).
    """
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step, car_charging_slots=car_slots)
    my_predbat.predict_car_hold_best = None
    my_predbat.run_prediction([], [], export_window or [], export_limits or [], False, end_record=my_predbat.end_record, save=save)
    if save in ("best", "compare", "yesterday"):
        return my_predbat.predict_car_hold_best
    return my_predbat.prediction.predict_car_hold_best


def _run_car_hold_prediction_tests(my_predbat, pv_step, load_step, minutes_now):
    """Tests 10d-10j: a saving prediction records the minutes it held the battery for a charging car."""
    failed = False
    slot = {"start": minutes_now, "end": minutes_now + 30, "kwh": 1.0, "average": 8.0, "octopus": True}
    whole_slot = _car_hold(range(0, 30, 5))

    # --- Test 10d: a car slot with energy holds the battery for every step of the slot, and no other ---
    print("Test the prediction records the car hold for each step of a car slot")
    _set_car(my_predbat, [slot])
    held = _predict_car_hold(my_predbat, pv_step, load_step)
    if held != whole_slot:
        print("ERROR: car hold recorded as {}, expected {}".format(held, whole_slot))
        failed = True

    # --- Test 10e: the car may draw from the battery - nothing is held ---
    print("Test no car hold is recorded when car_charging_from_battery is on")
    _set_car(my_predbat, [slot], from_battery=True)
    held = _predict_car_hold(my_predbat, pv_step, load_step)
    if held != {}:
        print("ERROR: car_charging_from_battery on should record no car hold, got {}".format(held))
        failed = True

    # --- Test 10f: a car already at its limit draws nothing, so nothing is held, even with a planned kWh slot ---
    print("Test no car hold is recorded for a car already at its limit")
    _set_car(my_predbat, [slot], soc=100.0, limit=100.0)
    held = _predict_car_hold(my_predbat, pv_step, load_step)
    if held != {}:
        print("ERROR: a full car should record no car hold, got {}".format(held))
        failed = True

    # --- Test 10g: a car that fills part-way through its slot is held only until it is full - the
    # prediction clamps car energy step by step, which a render-time check against the car's
    # starting SoC could not see. 6kWh over an hour is 0.5kWh a step; a 1kWh gap fills in two ---
    print("Test the car hold stops at the step the car fills")
    _set_car(my_predbat, [{"start": minutes_now, "end": minutes_now + 60, "kwh": 6.0, "average": 8.0, "octopus": True}], soc=0.0, limit=1.0)
    held = _predict_car_hold(my_predbat, pv_step, load_step)
    if held != _car_hold([0, 5]):
        print("ERROR: a car filling after two steps should be held for relative minutes [0, 5], got {}".format(held))
        failed = True

    # --- Test 10h: without charge window control neither execute.py nor the prediction holds for the car ---
    print("Test no car hold is recorded when set_charge_window is off")
    _set_car(my_predbat, [slot])
    my_predbat.set_charge_window = False
    held = _predict_car_hold(my_predbat, pv_step, load_step)
    my_predbat.set_charge_window = True
    if held != {}:
        print("ERROR: set_charge_window off should record no car hold, got {}".format(held))
        failed = True

    # --- Test 10i: a car reporting car_charging_now outside its plan is held (#5245) - the live plan
    # models it through car_charging_slots_model() and uncaps its limit, never the published car plan ---
    print("Test a car charging now outside its plan records the car hold")
    _set_car(my_predbat, [], soc=100.0, limit=100.0, charging_now=True, limit_model=[CAR_CHARGING_LIMIT_UNCAPPED])
    my_predbat.dynamic_load_car_charging_now(minutes_now + 30)
    if not my_predbat.car_charging_now_slots[0]:
        print("ERROR: test setup - dynamic_load_car_charging_now() did not model the charging car")
        failed = True
    else:
        held = _predict_car_hold(my_predbat, pv_step, load_step, car_slots=my_predbat.car_charging_slots_model())
        if not held or 0 not in held:
            print("ERROR: a car charging now outside its plan should be held from now, got {}".format(held))
            failed = True
        elif my_predbat.car_charging_slots != [[]]:
            print("ERROR: the published car plan must stay empty, got {}".format(my_predbat.car_charging_slots))
            failed = True

    # --- Test 10j: only a run that publishes the plan records the hold - not the optimiser's scenario
    # runs, nor the PV10 run, which always takes the Python engine as it saves ---
    for save in (None, "best10"):
        print("Test a prediction with save={} records no car hold".format(save))
        _set_car(my_predbat, [slot])
        held = _predict_car_hold(my_predbat, pv_step, load_step, save=save)
        if held != {}:
            print("ERROR: a prediction with save={} should record no car hold, got {}".format(save, held))
            failed = True

    return failed


def _run_car_hold_render_tests(my_predbat, render, templates, minutes_now):
    """Tests 10k-10q: the plan's "Hold for car" icon and reason follow the car hold the prediction recorded."""
    failed = False
    # No car on the fixture - the row must follow the recorded hold, not rework it from car slots
    _set_car(my_predbat, [])
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)  # flat - held, not falling

    # --- Test 10k: a Demand row held for the whole of it reads as hold_for_car, with the car icon ---
    print("Test Demand row held for a charging car reads as hold_for_car")
    my_predbat.predict_car_hold_best = _car_hold(range(0, 30, 5))
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["hold_for_car"]:
        print("ERROR: car-held demand reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "Hold for car" not in _render(row, templates) or "at least half of this slot" not in _render(row, templates):
        print("ERROR: car-held demand rendered text should say the hold covers at least half the slot: {}".format(_render(row, templates)))
        failed = True
    elif row["state_html"] != "&#128663;":
        print("ERROR: car-held demand state cell should show the car icon in place of the arrow, got: {}".format(row["state_html"]))
        failed = True

    # --- Test 10l: held for exactly half the row still reads as held ---
    print("Test a Demand row held for half of it reads as hold_for_car")
    my_predbat.predict_car_hold_best = _car_hold([0, 5, 10])
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["hold_for_car"]:
        print("ERROR: a row held for half of it should read as hold_for_car: {}".format(row and _codes(row)))
        failed = True

    # --- Test 10m: a short dispatch holds only one step of the row - the battery discharges for the rest,
    # so the row keeps its falling arrow rather than a car icon claiming the grid covers the house ---
    print("Test a Demand row held for one step keeps its trend arrow")
    my_predbat.predict_soc_best = {minute: 5.0 - minute * 0.01 for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes + 5, 5)}
    my_predbat.predict_car_hold_best = _car_hold([0])
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["demand_falling"]:
        print("ERROR: a row held for one step should read as demand_falling: {}".format(row and _codes(row)))
        failed = True
    elif "&#128663;" in row["state_html"]:
        print("ERROR: a row held for one step should show no car icon, got: {}".format(row["state_html"]))
        failed = True
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)

    # --- Test 10n: a split demand-then-export row held before the export reads as
    # "Until HH:MM, ... Then exporting..." - one narrative, like the demand split ---
    print("Test split row held for the car before the export window")
    split_time = (my_predbat.midnight_utc + timedelta(minutes=minutes_now + 15)).strftime("%H:%M")
    my_predbat.export_window_best = [{"start": minutes_now + 15, "end": minutes_now + 60, "average": 15.0}]
    my_predbat.export_limits_best = [50.0]
    my_predbat.predict_car_hold_best = _car_hold([0, 5, 10])
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    rendered = row and _render(row, templates)
    if row is None or _codes(row) != ["hold_for_car_before_export", "export_high_rate"]:
        print("ERROR: split pre-export hold reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "&#128663;" not in row["state_html"]:
        print("ERROR: split pre-export hold state cell should show the car icon, got: {}".format(row["state_html"]))
        failed = True
    elif "Until {}".format(split_time) not in rendered or "Then exporting down to" not in rendered:
        print("ERROR: split pre-export hold should read 'Until {} ... Then exporting', got: {}".format(split_time, rendered))
        failed = True

    # --- Test 10o: a hold only after the export start holds nothing before it - the pre-export
    # segment is checked on its own, not with the whole row ---
    print("Test split row held only after the export start shows no hold")
    my_predbat.predict_car_hold_best = _car_hold([15, 20, 25])
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["demand_before_export_steady", "export_high_rate"]:
        print("ERROR: a hold after the export start should leave the pre-export segment as plain demand: {}".format(row and _codes(row)))
        failed = True
    elif "&#128663;" in row["state_html"]:
        print("ERROR: no car icon expected before the export when the hold starts with it, got: {}".format(row["state_html"]))
        failed = True
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []

    # --- Test 10p: the Yesterday actual-history table shows measured SoC, so its car icon follows the
    # recorded "Hold for car" status, not the model ---
    print("Test the car icon follows recorded hold minutes when given")
    my_predbat.predict_car_hold_best = {}
    _, raw_plan = render(car_hold_minutes=set(range(minutes_now, minutes_now + 20)))
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["hold_for_car"]:
        print("ERROR: recorded Hold for car for most of the row should show hold_for_car with no modelled hold: {}".format(row and _codes(row)))
        failed = True
    my_predbat.predict_car_hold_best = _car_hold(range(0, 30, 5))
    _, raw_plan = render(car_hold_minutes=set())
    row = _get_row(raw_plan, minutes_now)
    if row is None or "hold_for_car" in _codes(row):
        print("ERROR: no recorded hold should show no hold_for_car even with a modelled hold: {}".format(row and _codes(row)))
        failed = True

    # --- Test 10q: the recorded status lags a slot boundary by a minute or two after a hold ends, and
    # that leftover must not put the car icon on the following row ---
    print("Test a recorded hold lagging into a row by two minutes shows no car icon")
    my_predbat.predict_car_hold_best = {}
    _, raw_plan = render(car_hold_minutes={minutes_now, minutes_now + 1})
    row = _get_row(raw_plan, minutes_now)
    if row is None or "hold_for_car" in _codes(row):
        print("ERROR: a two-minute recorded hold at the start of a row should not show hold_for_car: {}".format(row and _codes(row)))
        failed = True

    return failed


def run_test_plan_why_reason(my_predbat):
    """
    Test the per-slot "why" reason data in the JSON plan output: each row's json_row["reasons"]
    is a list of {code, params} entries (no baked text - the text lives once in
    raw_plan["reason_templates"], per maintainer review on PR #4311), covering every plan-slot
    state category (charge / hold charge / freeze charge / export / hold export / freeze export /
    manual overrides / no-window "demand" default). Always on - no opt-in switch, confirmed with
    the maintainer that this doesn't touch the C++ prediction kernel path.
    """
    print("**** Running plan why-reason tests ****")
    failed = False

    pv_step, load_step = _setup_baseline(my_predbat)
    minutes_now = my_predbat.minutes_now
    window = [{"start": minutes_now, "end": minutes_now + 30, "average": 10.0}]

    def render(car_hold_minutes=None):
        """Render the plan JSON for the current fixture state."""
        return my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False, car_hold_minutes=car_hold_minutes)

    my_predbat.charge_window_best = window
    my_predbat.charge_limit_best = [8.0]
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.manual_charge_times = []
    my_predbat.manual_freeze_charge_times = []
    my_predbat.manual_export_times = []
    my_predbat.manual_freeze_export_times = []
    my_predbat.manual_demand_times = []

    # --- Test 1: Chrg ---
    print("Test Chrg reason")
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 2.0)  # 20%, well below the 80% target
    _, raw_plan = render()
    templates = raw_plan["reason_templates"]
    row = _get_row(raw_plan, minutes_now)
    expected_charge_rate_kw = "{:.2f}".format(my_predbat.battery_rate_max_charge * 60)
    if row is None or _codes(row) != ["charge_low_rate"]:
        print("ERROR: Chrg reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"] != {"target_percent": 80, "rate": "{:.2f}".format(row["import_rate"]), "rate_kw": expected_charge_rate_kw}:
        print("ERROR: Chrg params unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "Charging up to 80" not in _render(row, templates) or "{}kW".format(expected_charge_rate_kw) not in _render(row, templates):
        print("ERROR: Chrg rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 2: HoldChrg ---
    print("Test HoldChrg reason")
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)  # 90%, already above the 80% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["hold_charge_at_target"]:
        print("ERROR: HoldChrg reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"] != {"target_percent": 80}:
        print("ERROR: HoldChrg params unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "Holding" not in _render(row, templates):
        print("ERROR: HoldChrg rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 3: FrzChrg ---
    print("Test FrzChrg reason")
    my_predbat.charge_limit_best = [my_predbat.reserve]  # target == reserve level
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["freeze_charge"]:
        print("ERROR: FrzChrg reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif set(row["reasons"][0]["params"]) != {"rate", "threshold"}:
        print("ERROR: FrzChrg params unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "Freeze charging" not in _render(row, templates):
        print("ERROR: FrzChrg rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 4: manual charge override ---
    print("Test manual charge override reason")
    my_predbat.charge_limit_best = [8.0]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 2.0)
    my_predbat.manual_charge_times = [minutes_now]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["charge_low_rate", "manual_override_charge"]:
        print("ERROR: manual charge reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "You manually set this slot to charge" not in _render(row, templates):
        print("ERROR: manual charge rendered text unexpected: {}".format(_render(row, templates)))
        failed = True
    my_predbat.manual_charge_times = []

    # --- Test 4b: Chrg low power (set_charge_low_power throttles the rate below max, computed
    # dynamically via find_charge_rate() rather than a fixed fraction like export's snail encoding) ---
    print("Test Chrg low power reason shows the throttled rate_kw, not the nameplate max")
    low_power_window = [{"start": minutes_now, "end": minutes_now + 60, "average": 10.0}]
    my_predbat.charge_window_best = low_power_window
    my_predbat.charge_limit_best = [8.0]  # small gap above the 7.9 current SoC - easily reached even throttled
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 7.9)
    my_predbat.set_charge_low_power = True
    my_predbat.charge_low_power_margin = 10
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    max_rate_kw = my_predbat.battery_rate_max_charge * 60
    rate_kw = float(row["reasons"][0]["params"]["rate_kw"]) if row is not None and _codes(row) == ["charge_low_rate"] else None
    if row is None or _codes(row) != ["charge_low_rate"]:
        print("ERROR: Chrg low power reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif rate_kw is None or rate_kw >= max_rate_kw:
        print("ERROR: Chrg low power rate_kw not throttled below the {}kW max: got {}".format(max_rate_kw, rate_kw))
        failed = True
    elif "{:.2f}kW".format(rate_kw) not in _render(row, templates):
        print("ERROR: Chrg low power rendered text missing the throttled rate: {}".format(_render(row, templates)))
        failed = True
    my_predbat.set_charge_low_power = False
    my_predbat.charge_window_best = window
    my_predbat.charge_limit_best = [8.0]

    # --- Test 5: Exp ---
    print("Test Exp reason")
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = window
    my_predbat.export_limits_best = [50.0]  # 50% target
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)  # 90%, well above the 50% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    expected_export_rate_kw = "{:.2f}".format(my_predbat.battery_rate_max_export * 60)
    if row is None or _codes(row) != ["export_high_rate"]:
        print("ERROR: Exp reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"] != {"target_percent": 50.0, "rate": "{:.2f}".format(row["export_rate"]), "rate_kw": expected_export_rate_kw}:
        print("ERROR: Exp params unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "Exporting down to" not in _render(row, templates) or "{}kW".format(expected_export_rate_kw) not in _render(row, templates):
        print("ERROR: Exp rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 5b: Exp slow (fractional limit -> reduced rate, the snail-symbol encoding) ---
    print("Test Exp slow reason shows the reduced rate_kw, not the nameplate max")
    my_predbat.export_limits_best = [50.3]  # limit.tens_of_percentage_rate_reduction -> 70% of max rate
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    expected_slow_export_rate_kw = "{:.2f}".format(my_predbat.battery_rate_max_export * 60 * 0.7)
    if row is None or _codes(row) != ["export_high_rate"]:
        print("ERROR: Exp slow reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"].get("rate_kw") != expected_slow_export_rate_kw:
        print("ERROR: Exp slow rate_kw unexpected: expected {}, got {}".format(expected_slow_export_rate_kw, row["reasons"][0]["params"].get("rate_kw")))
        failed = True
    my_predbat.export_limits_best = [50.0]

    # --- Test 6: HoldExp ---
    print("Test HoldExp reason")
    my_predbat.export_limits_best = [95.0]  # 95% target, unreachable this window
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)  # 50%, well below the 95% target
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["hold_export_unreachable"]:
        print("ERROR: HoldExp reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"] != {"target_percent": 95.0}:
        print("ERROR: HoldExp params unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "not triggered" not in _render(row, templates):
        print("ERROR: HoldExp rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 7: FrzExp ---
    print("Test FrzExp reason")
    my_predbat.export_limits_best = [99]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["freeze_export"]:
        print("ERROR: FrzExp reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"] != {}:
        print("ERROR: FrzExp params unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "Freezing export" not in _render(row, templates):
        print("ERROR: FrzExp rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 8: manual export override ---
    print("Test manual export override reason")
    my_predbat.export_limits_best = [50.0]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)
    my_predbat.manual_export_times = [minutes_now]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["export_high_rate", "manual_override_export"]:
        print("ERROR: manual export reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "You manually set this slot to export" not in _render(row, templates):
        print("ERROR: manual export rendered text unexpected: {}".format(_render(row, templates)))
        failed = True
    my_predbat.manual_export_times = []

    # --- Test 8b: merged/rowspan export cell shows a rate range, not just the first slot's rate ---
    print("Test merged export cell reason shows a rate range")
    span_window = [{"start": minutes_now, "end": minutes_now + 90, "average": 20.0}]
    my_predbat.export_window_best = span_window
    my_predbat.export_limits_best = [50.0]
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 9.0)  # 90%, well above the 50% target
    my_predbat.rate_export[minutes_now] = 15.0
    my_predbat.rate_export[minutes_now + 30] = 25.0
    my_predbat.rate_export[minutes_now + 60] = 20.0
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["export_high_rate"]:
        print("ERROR: merged export reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif row["reasons"][0]["params"].get("rate") != "15.00-25.00":
        print("ERROR: merged export rate range unexpected: {}".format(row["reasons"][0]["params"]))
        failed = True
    elif "15.00-25.00" not in _render(row, templates):
        print("ERROR: merged export rendered text missing the rate range: {}".format(_render(row, templates)))
        failed = True

    # A single-slot window (no merge) must still show a plain single value, not a spurious range.
    print("Test single-slot export cell reason still shows a single rate, not a range")
    my_predbat.export_window_best = window
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or "-" in row["reasons"][0]["params"].get("rate", ""):
        print("ERROR: single-slot export rate should not be a range: {}".format(row and row["reasons"][0]["params"]))
        failed = True

    # A minute within the merged span missing from rate_export must fall back to the row's own
    # known rate, not silently default to 0 (regression: rate_range_text originally defaulted a
    # missing minute to 0 rather than the fallback_value it was given, which could widen a range
    # to a spurious "0.00-20.00" - Copilot review finding on PR #4362).
    print("Test merged export cell falls back to the row's own rate for a missing minute, not 0")
    my_predbat.export_window_best = span_window
    my_predbat.rate_export[minutes_now] = 20.0
    my_predbat.rate_export[minutes_now + 60] = 20.0
    del my_predbat.rate_export[minutes_now + 30]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or row["reasons"][0]["params"].get("rate") != "20.00":
        print("ERROR: merged export rate with a missing minute unexpected: {}".format(row and row["reasons"][0]["params"]))
        failed = True
    my_predbat.export_window_best = window
    my_predbat.rate_export[minutes_now] = 5.0
    my_predbat.rate_export[minutes_now + 30] = 5.0
    my_predbat.rate_export[minutes_now + 60] = 5.0

    # --- Test 9: Demand (no charge or export window active) ---
    print("Test Demand default reason")
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)  # perfectly flat -> steady
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["demand_steady"]:
        print("ERROR: Demand reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "steady" not in _render(row, templates):
        print("ERROR: Demand rendered text unexpected: {}".format(_render(row, templates)))
        failed = True

    # --- Test 10: manual demand override ---
    print("Test manual demand override reason")
    my_predbat.manual_demand_times = [minutes_now]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["demand_steady", "manual_override_demand"]:
        print("ERROR: manual demand reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "You manually set this slot to demand mode" not in _render(row, templates):
        print("ERROR: manual demand rendered text unexpected: {}".format(_render(row, templates)))
        failed = True
    my_predbat.manual_demand_times = []

    # --- Test 10b: split slot where the export window only starts partway through ---
    # The state cell splits into "arrow | Exp", so the tooltip must explain both halves - the
    # pre-window arrow used to contribute no reason at all, leaving the split cell with only the
    # export sentence.
    print("Test split slot (demand arrow then export) explains both halves")
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = [{"start": minutes_now + 15, "end": minutes_now + 60, "average": 15.0}]
    my_predbat.export_limits_best = [50.0]
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or not row.get("split"):
        print("ERROR: expected a split state cell for a mid-slot export window start")
        failed = True
    elif len(_codes(row)) != 2 or not _codes(row)[0].startswith("demand_before_export_") or _codes(row)[1] != "export_high_rate":
        # The arrow direction depends on the predicted SoC trend; only the pairing matters here
        print("ERROR: split slot reasons unexpected: {}".format(_codes(row)))
        failed = True
    else:
        rendered = _render(row, templates)
        expected_split_time = (my_predbat.midnight_utc + timedelta(minutes=minutes_now + 15)).strftime("%H:%M")
        if "Until {}".format(expected_split_time) not in rendered:
            print("ERROR: split slot tooltip should state the exact split time, got: {}".format(rendered))
            failed = True
        elif "Then exporting down to" not in rendered:
            print("ERROR: split slot tooltip should join both halves with a lowercase 'Then ', got: {}".format(rendered))
            failed = True
        elif "Then," in rendered:
            print("ERROR: split slot tooltip should not put a comma after 'Then', got: {}".format(rendered))
            failed = True
        # The pre-window wording must not claim nothing is scheduled - the slot does export later
        if "no charging or exporting is scheduled" in rendered:
            print("ERROR: split slot tooltip contradicts itself: {}".format(rendered))
            failed = True

    # --- Test 10c: a near-flat SoC before the export window reads as steady, not rising ---
    # Deliberately a small but non-zero rise (0.02kWh over the pre-window period): an exact-zero
    # trend would also pass under a plain "> 0" test, so only a near-flat one actually exercises
    # the same 0.05kWh tolerance the whole-slot demand arrow uses.
    print("Test near-flat SoC before an export window reads as steady")
    my_predbat.predict_soc_best = {minute: 5.0 + min(minute, 15) * (0.02 / 15.0) for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes + 5, 5)}
    _, raw_plan = render()
    row = _get_row(raw_plan, minutes_now)
    if row is None or _codes(row) != ["demand_before_export_steady", "export_high_rate"]:
        print("ERROR: flat pre-export slot reasons unexpected: {}".format(row and _codes(row)))
        failed = True
    elif "expected to stay steady" not in _render(row, templates):
        print("ERROR: flat pre-export tooltip unexpected: {}".format(_render(row, templates)))
        failed = True

    car_state = {field: getattr(my_predbat, field) for field in _CAR_HOLD_FIELDS}
    try:
        failed |= _run_car_hold_prediction_tests(my_predbat, pv_step, load_step, minutes_now)
        failed |= _run_car_hold_render_tests(my_predbat, render, templates, minutes_now)
    finally:
        for field, value in car_state.items():
            setattr(my_predbat, field, value)
    my_predbat.charge_window_best = window
    my_predbat.charge_limit_best = [8.0]
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []

    # --- Test 11: reason_templates has an entry for every code used across all scenarios ---
    print("Test reason_templates covers every code used")
    all_codes = set()
    for row in raw_plan["rows"]:
        all_codes.update(_codes(row))
    missing = all_codes - set(templates.keys())
    if missing:
        print("ERROR: reason_templates missing entries for codes: {}".format(missing))
        failed = True

    # --- Test 12: client-side renderer wires reasons/reason_templates into a title= tooltip ---
    print("Test renderStateCell wires reasons into a title attribute via renderReasonText")
    renderer_js = get_plan_renderer_js()
    if "function renderReasonText" not in renderer_js:
        print("ERROR: expected a renderReasonText() helper in the plan renderer JS")
        failed = True
    if "reasons[0].code === 'hold_for_car_before_export'" not in renderer_js:
        print("ERROR: renderReasonText() should prefix 'Then' after a hold_for_car_before_export first half, as _render() here does")
        failed = True
    if "row.reasons" not in renderer_js:
        print("ERROR: expected renderStateCell to reference row.reasons")
        failed = True
    if "reason_templates" not in renderer_js:
        print("ERROR: expected renderStateCell to reference the shared reason_templates table")
        failed = True
    if "title=" not in renderer_js:
        print("ERROR: expected renderStateCell to emit a title= attribute")
        failed = True
    # Scoped to renderStateCell's own body (not a whole-file count) since the read-only path
    # (Test 13 below) has its own, separate titleAttr usage with the same variable name.
    state_cell_fn_start = renderer_js.index("function renderStateCell(")
    state_cell_fn_end = renderer_js.index("function renderRateCell(", state_cell_fn_start)
    state_cell_fn_src = renderer_js[state_cell_fn_start:state_cell_fn_end]
    if state_cell_fn_src.count("${titleAttr}") != 2:
        print("ERROR: expected both the first and split (state2) cells to carry the title= tooltip, found {} references".format(state_cell_fn_src.count("${titleAttr}")))
        failed = True

    # --- Test 13a: tap/focus disclosure for touch and keyboard users, alongside title= for mouse
    # hover - a touch interaction can't trigger :hover/title at all, so the two mechanisms never
    # fire together and there's nothing to reconcile between them (see PR discussion). ---
    print("Test renderStateCell also wires a tap/focus disclosure panel, not just hover")
    if "toggleForceDropdown" not in renderer_js:
        print("ERROR: expected renderStateCell to reuse the existing toggleForceDropdown() tap-to-toggle mechanism")
        failed = True
    if "clickable-state-cell" not in renderer_js:
        print("ERROR: expected renderStateCell to mark reason cells with the clickable-state-cell class")
        failed = True
    if "reason-text" not in renderer_js:
        print("ERROR: expected renderStateCell to render the reason panel content with the reason-text class")
        failed = True
    if renderer_js.count("dropdown-content") < 2:  # at least the CSS rule plus renderStateCell's own usage
        print("ERROR: expected renderStateCell to reuse the existing .dropdown-content panel mechanism")
        failed = True
    if 'tabindex="0"' not in renderer_js or "onkeydown" not in renderer_js:
        print("ERROR: expected the reason cell to be keyboard-focusable and keyboard-operable, not just tap/click - a bare <td onclick> isn't reachable by keyboard")
        failed = True
    # Regression: the document-level "click outside closes the dropdown" handler (in get_plan_css(),
    # a separate function from get_plan_renderer_js()) only whitelisted .clickable-time-cell, so
    # opening the new reason panel (class clickable-state-cell) would have its own click event
    # immediately re-close it via that same handler (Copilot review on #4349).
    plan_css = web_helper.get_plan_css()
    outside_click_start = plan_css.index("Close dropdowns when clicking outside")
    outside_click_src = plan_css[outside_click_start : outside_click_start + 400]
    if ".clickable-state-cell" not in outside_click_src:
        print("ERROR: the click-outside handler doesn't whitelist .clickable-state-cell, so tapping a reason cell would open then immediately close its own panel")
        failed = True

    # --- Test 13b: the read-only (History / Yesterday Without Predbat) state cells also get tooltips ---
    print("Test the non-editable state cell path also emits a title= tooltip")
    if "function reasonTitleAttr" not in renderer_js:
        print("ERROR: expected a shared reasonTitleAttr() helper used by both state-cell paths")
        failed = True
    # Scoped to the read-only branch specifically (between its own reasonTitleAttr() call and the
    # next cell section) - the editable path (renderStateCell, Test 12 above) builds its split-cell
    # HTML differently (extra tap/focus attributes sit between the bgcolor and titleAttr), so a
    # single literal substring can no longer match both paths' shapes.
    readonly_branch_start = renderer_js.index("const titleAttr = reasonTitleAttr(row, reasonTemplates);")
    readonly_branch_end = renderer_js.index("// Limit cell", readonly_branch_start)
    readonly_branch_src = renderer_js[readonly_branch_start:readonly_branch_end]
    if "state_color || '#FFFFFF'}${titleAttr}" not in readonly_branch_src:
        print("ERROR: expected the read-only state cell (editable=false) to carry the title= tooltip")
        failed = True
    if "state2_color || '#FFFFFF'}${titleAttr}" not in readonly_branch_src:
        print("ERROR: expected the read-only split (state2) cell to carry the title= tooltip")
        failed = True
    # Templates must come from the dataset being rendered, not the plan view's global - otherwise
    # the History/Yesterday views would render against the wrong (or a missing) template table
    if "window.planData && window.planData.reason_templates" in renderer_js:
        print("ERROR: reason templates should come from the rendered jsonData, not window.planData")
        failed = True
    if "const reasonTemplates = jsonData.reason_templates" not in renderer_js:
        print("ERROR: expected renderPlanTable to take reason_templates from the dataset it renders")
        failed = True

    # --- Test 13c: plan table column headers get a hover tooltip explaining what they mean ---
    print("Test column headers carry a title= explaining what each column means")
    if "COLUMN_HEADER_HELP" not in renderer_js:
        print("ERROR: expected a COLUMN_HEADER_HELP lookup for column header tooltips")
        failed = True
    if "function th(key, innerHtml" not in renderer_js:
        print("ERROR: expected a th() helper wiring COLUMN_HEADER_HELP into <th> title= attributes")
        failed = True
    # Every column referenced by the header-rendering block must have a corresponding help entry -
    # a silently missing key would just render no tooltip rather than fail loudly, so check directly.
    header_start = renderer_js.index("const COLUMN_HEADER_HELP")
    header_block_end = renderer_js.index("function th(key", header_start)
    header_help_block = renderer_js[header_start:header_block_end]
    for key in ["time", "import", "export", "state", "limit", "pv", "load", "clip", "xload", "car", "iboost", "soc", "cost", "total", "co2_rate", "co2_total"]:
        if "{}:".format(key) not in header_help_block:
            print("ERROR: COLUMN_HEADER_HELP is missing an entry for '{}'".format(key))
            failed = True

    # --- Test 14: the renderer JS source has no invalid Python escape sequences ---
    # The JS regexes live inside plain (non-raw) triple-quoted Python strings, so a backslash
    # intended for JS must be doubled. A single "\{" raises SyntaxWarning today and becomes a
    # SyntaxError in a future Python.
    print("Test web_helper.py has no invalid escape sequences")
    with open(web_helper.__file__, "r", encoding="utf-8") as han:
        web_helper_source = han.read()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        compile(web_helper_source, web_helper.__file__, "exec")
        syntax_warnings = [item for item in caught if issubclass(item.category, SyntaxWarning)]
    if syntax_warnings:
        for item in syntax_warnings:
            print("ERROR: SyntaxWarning in web_helper.py line {}: {}".format(item.lineno, item.message))
        failed = True

    # --- Test 15: editable rate cells don't double-mark a manual override (batpred#4474) ---
    # row.{import,export}_rate_adjust_type === 'manual' and renderRateCell()'s own isOverride
    # check both independently render the same turned-F glyph for the same single override -
    # visually stacking two identical markers, and since only isOverride gates the Clear link,
    # the two signals can disagree and leave a marker with no working Clear behind it. The fix
    # skips the adjust-type marker specifically for 'manual' when editable, leaving isOverride as
    # the sole source of truth for both the marker and the Clear control in that case.
    print("Test editable rate cells don't double-render the manual-override marker")
    for label, adjust_var in (("import", "importAdjustType"), ("export", "exportAdjustType")):
        rate_field = "{}_rate_adjust_type".format(label)
        expected = "const {} = (editable && row.{} === 'manual') ? null : row.{};".format(adjust_var, rate_field, rate_field)
        if expected not in renderer_js:
            print("ERROR: expected {} to null out a 'manual' adjust type in editable mode (found: {})".format(adjust_var, expected in renderer_js))
            failed = True
        # The old unconditional form must be gone, not just superseded - if it's still present
        # anywhere the double-render bug is still reachable.
        old_unconditional = "const {} = row.{} ? ` ${{getAdjustSymbol(row.{})}}` : '';".format({"import": "importAdjust", "export": "exportAdjust"}[label], rate_field, rate_field)
        if old_unconditional in renderer_js:
            print("ERROR: old unconditional {} form is still present alongside the fix".format({"import": "importAdjust", "export": "exportAdjust"}[label]))
            failed = True

    # --- Test 16: toggleForceDropdown doesn't throw on a stale/missing dropdown id (#4474 follow-up) ---
    # Lives in get_plan_css() alongside the rest of the dropdown open/close JS, not
    # get_plan_renderer_js() (which only covers the table-building functions).
    print("Test toggleForceDropdown guards against a missing dropdown element")
    plan_css_full = web_helper.get_plan_css()
    toggle_start = plan_css_full.index("function toggleForceDropdown(")
    toggle_end = plan_css_full.index("\n    }", toggle_start)
    toggle_src = plan_css_full[toggle_start:toggle_end]
    if "if (!dropdown)" not in toggle_src:
        print("ERROR: expected toggleForceDropdown to null-guard document.getElementById(id) before using it")
        failed = True

    if not failed:
        print("All plan why-reason tests passed")
    return failed

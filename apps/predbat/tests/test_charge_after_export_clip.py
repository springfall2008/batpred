# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Charge refinement tests with real dispatch, car and saving-session predictions."""

import copy
from datetime import timedelta

from axle import load_axle_slot
from prediction import Prediction
from tests.test_infra import reset_inverter


def run_charge_after_export_clip_tests(my_predbat):
    """Check worthwhile charging, manual protection and unbiased acceptance for both car modes."""
    failed = 0
    for car_from_battery in (False, True):
        for session_type in ("saving", "axle"):
            base = copy.copy(my_predbat)
            base.car_charging_slots = copy.deepcopy(my_predbat.car_charging_slots)
            reset_inverter(base)
            base.minutes_now = 0
            base.forecast_minutes = base.end_record = 180
            base.forecast_hours = 3
            base.soc_max = base.best_soc_max = 10.0
            base.reserve = 0.5
            base.best_soc_min = 0.0
            base.best_soc_step = 0.5
            base.battery_rate_max_charge = base.battery_rate_max_charge_dc = 3.0 / 60.0
            base.battery_rate_max_discharge = base.battery_rate_max_export = 3.0 / 60.0
            base.charge_rate_now = base.discharge_rate_now = base.inverter_limit = 3.0 / 60.0
            base.metric_min_improvement = 0.0
            base.set_charge_freeze_only = False
            base.calculate_best_charge = True
            base.calculate_export_oncharge = True
            base.manual_all_times = []
            base.car_charging_from_battery = car_from_battery
            base.num_cars = 1
            base.car_charging_slots[0] = [{"start": 60, "end": 90, "kwh": 0.5, "average": 6.9, "octopus": True}]
            base.rate_import = {minute: 28.86 for minute in range(180)}
            base.rate_export = {minute: 0.0 for minute in range(180)}
            base.rate_min = 6.9
            base.rate_min_forward = {minute: 6.9 for minute in range(360)}
            base.rate_max_base = 28.86
            base.rate_export_min = base.rate_export_max = 0.0
            base.rate_export_max_forward = {}
            base.io_adjusted = {minute: True for minute in range(60, 90)}
            for minute in base.io_adjusted:
                base.rate_import[minute] = 6.9
            start = (base.midnight_utc + timedelta(minutes=150)).isoformat()
            end = (base.midnight_utc + timedelta(minutes=180)).isoformat()
            if session_type == "saving":
                base.load_saving_slot([{"start": start, "end": end, "rate": 100.0, "state": False}], base.rate_import)
            else:
                load_axle_slot(base, [{"start_time": start, "end_time": end, "import_export": "export", "pence_per_kwh": 100.0}], base.rate_import, export=False)
            base.rate_max = max(base.rate_import.values())
            base.pv_metric10_weight = 0.15
            base.pv_metric90_weight = 0.0
            base.load_scaling_dynamic = {}
            base.all_active_keep = {}
            base.all_active_keep_max = {}
            base.charge_window_best = [{"start": 60, "end": 90, "average": 6.9}]
            base.charge_limit_best = [0.0]
            pv_step = {minute: 0.0 for minute in range(0, 180, 5)}
            load_step = {minute: 1.0 / 12.0 for minute in range(0, 180, 5)}
            base.prediction = Prediction(base, pv_step, pv_step, load_step, load_step)
            before = base.run_prediction_metric(base.charge_limit_best, base.charge_window_best, [], [], end_record=180)[0]
            changed = base.refine_charge_after_export_clip()
            after = base.run_prediction_metric(base.charge_limit_best, base.charge_window_best, [], [], end_record=180)[0]
            if not changed or base.charge_limit_best[0] <= base.reserve or after >= before:
                print("ERROR: charge refinement car_from_battery={} session={} target={} metric {} -> {}".format(car_from_battery, session_type, base.charge_limit_best, before, after))
                failed += 1
            base.charge_limit_best = [0.0]
            base.manual_all_times = [60]
            if base.refine_charge_after_export_clip() or base.charge_limit_best != [0.0]:
                print("ERROR: charge refinement changed a manual window")
                failed += 1
            base.manual_all_times = []
            base.calculate_best_charge = False
            if base.refine_charge_after_export_clip():
                print("ERROR: charge refinement ignored calculate_best_charge")
                failed += 1
            base.calculate_best_charge = True
            # A ranking bonus must never make an otherwise worse plan pass acceptance.
            base.optimise_charge_limit = lambda *args, **kwargs: (10.0, before - 1.0, 0, 0, 0, 0, 0, 0, 0, before + 1.0)
            if base.refine_charge_after_export_clip() or base.charge_limit_best != [0.0]:
                print("ERROR: charge refinement accepted the adjusted metric instead of whole-plan cost")
                failed += 1
            base.io_adjusted = {}
            base.optimise_charge_limit = lambda *args, **kwargs: (10.0, before - 1.0, 0, 0, 0, 0, 0, 0, 0, before - 1.0)
            if base.refine_charge_after_export_clip() or base.charge_limit_best != [0.0]:
                print("ERROR: Intelligent refinement changed an ordinary tariff slot")
                failed += 1
            base.io_adjusted = {0: True}
            if base.refine_charge_after_export_clip() or base.charge_limit_best != [0.0]:
                print("ERROR: Intelligent refinement applied a dispatch from a different window")
                failed += 1
    return failed

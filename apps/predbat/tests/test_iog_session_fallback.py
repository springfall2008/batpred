# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Regression tests for session penalties leaking into uncertain Intelligent slots."""

import copy
from datetime import timedelta

from axle import load_axle_slot
from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10
from prediction import Prediction
from prediction_kernel import create_kernel_context, run_prediction_kernel
from tests.test_infra import reset_inverter
from tests.test_kernel_parity import ensure_kernel_built


def run_iog_session_fallback_tests(my_predbat):
    """Check actual session prices and independent expected costs in both prediction engines."""
    print("**** Intelligent session fallback tests ****")
    if not ensure_kernel_built():
        print("ERROR: session fallback tests require a working prediction kernel")
        return 1
    failed = 0
    for session_type in ("saving", "axle"):
        # Two hours at 1 kW: one hour at 28.86p, half an hour in the dispatch,
        # and half an hour in the session. Costs are calculated independently of prediction.
        for overlap, legacy, nominal_cost, pessimistic_cost in ((False, False, 96.74, 107.72), (True, False, 85.76, 96.74), (False, True, 96.74, 157.72)):
            # Isolate the fixture, including the one nested list reset_inverter mutates.
            base = copy.copy(my_predbat)
            base.car_charging_slots = copy.deepcopy(my_predbat.car_charging_slots)
            reset_inverter(base)
            base.minutes_now = 0
            base.forecast_minutes = 120
            base.forecast_hours = 2
            base.rate_import = {minute: 28.86 for minute in range(120)}
            base.rate_export = {minute: 0.0 for minute in range(120)}
            base.rate_max_base = 0.0 if legacy else 28.86
            base.io_adjusted = {minute: True for minute in range(40, 70)}
            if overlap:
                base.io_adjusted.update({minute: True for minute in range(90, 120)})
            for minute in base.io_adjusted:
                base.rate_import[minute] = 6.9
            base.load_scaling_dynamic = {}
            start = (base.midnight_utc + timedelta(minutes=90)).isoformat()
            end = (base.midnight_utc + timedelta(minutes=120)).isoformat()
            if session_type == "saving":
                base.load_saving_slot([{"start": start, "end": end, "rate": 100.0, "state": False}], base.rate_import)
            else:
                load_axle_slot(base, [{"start_time": start, "end_time": end, "import_export": "export", "pence_per_kwh": 100.0}], base.rate_import, export=False)
            base.rate_max = max(base.rate_import.values())
            base.import_today_now = base.export_today_now = base.cost_today_sofar = 0.0
            base.load_minutes_now = base.pv_today_now = base.carbon_today_sofar = base.iboost_today = 0.0
            base.best_soc_min = 0.0
            base.all_active_keep = {}
            base.all_active_keep_max = {}
            base.prediction_cache_enable = False
            pv_step = {minute: 0.0 for minute in range(0, 120, 5)}
            load_step = {minute: 1.0 / 12.0 for minute in range(0, 120, 5)}
            for scenario in (PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10):
                expected = nominal_cost if scenario == PV_SCENARIO_NOMINAL else pessimistic_cost
                pred = Prediction(base, pv_step, pv_step, load_step, load_step)
                pred.prediction_kernel_enable = False
                python_result = pred.run_prediction([], [], [], [], scenario, 120, cache=False)
                pred.kernel_handle = create_kernel_context(pred)
                kernel_result = run_prediction_kernel(pred, [], [], [], [], scenario, 120, 5, False)
                for engine, result in (("python", python_result), ("kernel", kernel_result)):
                    if result is None or abs(result[0] - expected) > 0.0001:
                        print("ERROR: {} overlap={} legacy={} scenario={} {} expected {}p, got {}".format(session_type, overlap, legacy, scenario, engine, expected, result[0] if result else None))
                        failed += 1
    return failed

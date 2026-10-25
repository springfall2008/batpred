# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""End to end planner test: a bad PV10 forecast must make the overnight charge bigger.

The plan is scored on a blend of the nominal, PV10 and PV90 futures, so when the PV10 forecast says
tomorrow's solar may not turn up, the optimiser buys a buffer in the cheap overnight window that the
nominal forecast alone would not pay for. The buffer has to survive into the executed plan. Once the
cheap window closes, a PV10 shortfall can only be met at the day rate, so a later replan cannot put it
back.

prune_dead_plan_slots() used to strip exactly that buffer: it judged each slot removal on the nominal
metric alone, and dropping a hedge always looks cheaper there. The plan then charged the same whether
PV10 was good or bad.

Runs the real calculate_plan on the committed agile1 template, as the random scenario benchmark does,
with a hand-built scenario: cheap 23:30-06:30 import, an expensive day, nothing paid for export, flat
0.5 kW load and a sunny nominal day. The plan is made twice, once with a PV10 equal to nominal and once
with a PV10 at 10% of it, and the battery's predicted SoC at the end of the cheap window is compared.
"""
import io
import math
import contextlib
from const import CLOUD_FACTOR_PV10
from tests.test_infra import reset_inverter
from tests.test_random_scenarios import RANDOM_TEMPLATE, apply_scenario_to_predbat

MINUTES_NOW = 22 * 60
CHEAP_END = 6 * 60 + 30
BATTERY_KWH = 10.0
# How much more the bad-PV10 plan must hold at the end of the cheap window. With the hedge kept it is
# 3.5 kWh more; with it stripped by the old prune it was 0.3 kWh.
MIN_EXTRA_CHARGE_KWH = 2.0


def _day(fn):
    """Build a 1440-value per-minute daily profile from a function of minute-of-day"""
    return [fn(minute) for minute in range(1440)]


def _is_cheap(minute):
    """True inside the 23:30-06:30 cheap import window"""
    return minute >= 23 * 60 + 30 or minute < CHEAP_END


def _pv_kw(minute):
    """A sunny day: a 3 kW sine peak between 08:00 and 18:00"""
    if 8 * 60 <= minute < 18 * 60:
        return 3.0 * math.sin(math.pi * (minute - 8 * 60) / 600.0)
    return 0.0


def _scenario():
    """The hand-built scenario, in the random benchmark's format"""
    return {
        "id": 0,
        "seed": 0,
        "params": {
            "battery": {"soc_max_kwh": BATTERY_KWH, "initial_soc_percent": 10.0, "rate_max_charge_kw": 3.0, "rate_max_discharge_kw": 3.0},
            "inverter": {"hybrid": True, "inverter_limit_kw": 5.0, "export_limit_kw": 5.0},
            "clock": {"minutes_now": MINUTES_NOW},
            "load_scaling": {"nominal": 1.0, "pv10": 1.1, "pv90": 0.9},
        },
        "data": {
            "rate_import_day": _day(lambda minute: 7.5 if _is_cheap(minute) else 30.0),
            "rate_export_day": _day(lambda minute: 0.0),
            "load_day_kwh": _day(lambda minute: 0.5 / 60.0),
            "pv_day_kw": _day(_pv_kw),
        },
    }


def _soc_at_cheap_end(my_predbat, pv10_fraction):
    """Plan the scenario with PV10 at pv10_fraction of nominal; return the nominal SoC at 06:30 and the charge plan"""
    apply_scenario_to_predbat(my_predbat, _scenario())

    # The benchmark derives PV10 as a fixed fraction of nominal; replace it with the one under test
    my_predbat.pv_forecast_minute10 = {minute: kwh * pv10_fraction for minute, kwh in my_predbat.pv_forecast_minute.items()}
    my_predbat.pv_forecast_minute10_step = my_predbat.step_data_history(
        my_predbat.pv_forecast_minute10,
        my_predbat.minutes_now,
        forward=True,
        cloud_factor=min(my_predbat.metric_cloud_coverage + CLOUD_FACTOR_PV10, 1.0) if my_predbat.metric_cloud_coverage else None,
        flip=True,
    )

    my_predbat.args["threads"] = 0
    my_predbat.plan_valid = False
    my_predbat.debug_enable = False
    my_predbat.calculate_plan(recompute=True)

    my_predbat.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=my_predbat.end_record, save="best")
    soc = my_predbat.predict_soc.get(CHEAP_END + 1440 - MINUTES_NOW)
    plan = [(my_predbat.time_abs_str(window["start"]), my_predbat.time_abs_str(window["end"]), limit) for window, limit in zip(my_predbat.charge_window_best, my_predbat.charge_limit_best)]
    return soc, plan


def run_plan_pv10_hedge_tests(my_predbat):
    """Run the bad-PV10 overnight charge test. Returns True on failure.

    my_predbat is deliberately unused - a fresh instance is planned from the template, for the reason
    run_random_scenario_tests gives: read_debug_yaml only restores what its dump carries, so planning
    on the shared instance both inherits and leaves behind state other tests depend on.
    """
    print("**** Running plan PV10 hedge tests ****")
    # Imported here rather than at module scope: unit_test imports this module, so a top level
    # import would be circular
    from unit_test import create_predbat

    failed = False
    scenario_predbat = create_predbat()
    with contextlib.redirect_stdout(io.StringIO()):
        reset_inverter(scenario_predbat)
        scenario_predbat.read_debug_yaml(RANDOM_TEMPLATE)
        scenario_predbat.config_root = "./"
        scenario_predbat.save_restore_dir = "./"
        scenario_predbat.load_user_config()
        good_soc, good_plan = _soc_at_cheap_end(scenario_predbat, 1.0)
        bad_soc, bad_plan = _soc_at_cheap_end(scenario_predbat, 0.1)

    print("Good PV10: SoC at 06:30 {} kWh, charge plan {}".format(good_soc, good_plan))
    print("Bad PV10: SoC at 06:30 {} kWh, charge plan {}".format(bad_soc, bad_plan))

    if good_soc is None or bad_soc is None:
        print("ERROR: no predicted SoC at the end of the cheap window - the scenario did not plan")
        return True
    if not good_plan:
        print("ERROR: no overnight charge planned with a good PV10 - the scenario no longer exercises the trade-off")
        failed = True
    if bad_soc < good_soc + MIN_EXTRA_CHARGE_KWH:
        print("ERROR: a bad PV10 forecast only raised the 06:30 SoC from {} to {} kWh, expected at least {} kWh more - the PV10 buffer was not kept in the plan".format(good_soc, bad_soc, MIN_EXTRA_CHARGE_KWH))
        failed = True

    if not failed:
        print("PASS")
    return failed

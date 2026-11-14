# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""A Freeze Export in a slot with no solar is Demand, so it must not reach the plan (#4124).

Freeze Export stops the battery charging while it still covers the house. With no PV there is nothing to
stop and nothing to export, so the slot behaves exactly as Demand does - but the plan still labelled it
"Freeze Export". The levels pass seeds each candidate export window as freeze or full export, never idle, so
such a slot can survive the optimiser. prune_dead_plan_slots() is what removes it: it drops any slot whose
removal leaves the nominal metric unchanged and that does not move the SoC in its own window.

The prune tests in test_prune_dead_slots stub the simulator, so they take that equivalence as given. This
runs the real prediction on the committed agile1 template, with a dark evening at 21:00, a 60% battery and
export paying 40p in 22:30-23:00 only, and checks:

1. a Freeze Export in 22:30-23:00 scores the same nominal metric as Demand there;
2. prune_dead_plan_slots() turns it into Demand;
3. a real export to 0% in the same slot earns money and is kept, so the prune is not dropping everything.
"""

import io
import contextlib
from const import EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET
from tests.test_infra import reset_inverter
from tests.test_random_scenarios import RANDOM_TEMPLATE, apply_scenario_to_predbat
from utils import export_mode_of, pack_export_limit

MINUTES_NOW = 21 * 60
DARK_START = 22 * 60 + 30
DARK_END = 23 * 60
BATTERY_KWH = 10.0
# The tolerance prune_dead_plan_slots treats as an unchanged metric
PRUNE_EPS = 0.02


def _day(fn):
    """Build a 1440-value per-minute daily profile from a function of minute-of-day"""
    return [fn(minute) for minute in range(1440)]


def _scenario():
    """The hand-built scenario, in the random benchmark's format: no PV before 08:00 tomorrow"""
    return {
        "id": 0,
        "seed": 0,
        "params": {
            "battery": {"soc_max_kwh": BATTERY_KWH, "initial_soc_percent": 60.0, "rate_max_charge_kw": 3.0, "rate_max_discharge_kw": 3.0},
            "inverter": {"hybrid": True, "inverter_limit_kw": 5.0, "export_limit_kw": 5.0},
            "clock": {"minutes_now": MINUTES_NOW},
            "load_scaling": {"nominal": 1.0, "pv10": 1.0, "pv90": 1.0},
        },
        "data": {
            "rate_import_day": _day(lambda minute: 20.0),
            "rate_export_day": _day(lambda minute: 40.0 if DARK_START <= minute < DARK_END else 5.0),
            "load_day_kwh": _day(lambda minute: 0.5 / 60.0),
            "pv_day_kw": _day(lambda minute: 3.0 if 8 * 60 <= minute < 18 * 60 else 0.0),
        },
    }


def _set_plan(my_predbat, export_limit):
    """Plan no charging and a single export window over the dark slot with the given limit"""
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = [{"start": DARK_START, "end": DARK_END, "average": 40.0}]
    my_predbat.export_limits_best = [export_limit]


def _nominal_metric(my_predbat):
    """The nominal-only metric of the current best plan, as prune_dead_plan_slots scores it"""
    return my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record, nominal_only=True)[0]


def run_plan_dark_freeze_tests(my_predbat):
    """Run the dark Freeze Export tests. Returns True on failure.

    my_predbat is deliberately unused - a fresh instance is loaded from the template, as run_plan_pv10_hedge_tests
    does, so the test neither inherits nor leaves behind state on the shared fixture.
    """
    print("**** Running plan dark freeze export tests ****")
    # Imported here rather than at module scope: unit_test imports this module, so a top level import would be circular
    from unit_test import create_predbat

    failed = False
    scenario_predbat = create_predbat()
    with contextlib.redirect_stdout(io.StringIO()):
        reset_inverter(scenario_predbat)
        scenario_predbat.read_debug_yaml(RANDOM_TEMPLATE)
        scenario_predbat.config_root = "./"
        scenario_predbat.save_restore_dir = "./"
        scenario_predbat.load_user_config()
        apply_scenario_to_predbat(scenario_predbat, _scenario())
        scenario_predbat.args["threads"] = 0
        scenario_predbat.plan_valid = False
        scenario_predbat.debug_enable = False
        # Planning once builds the prediction the trials below run on; its plan is then replaced by hand
        scenario_predbat.calculate_plan(recompute=True)
        scenario_predbat.end_record = scenario_predbat.forecast_minutes

        _set_plan(scenario_predbat, pack_export_limit(EXPORT_MODE_IDLE))
        demand_metric = _nominal_metric(scenario_predbat)
        _set_plan(scenario_predbat, pack_export_limit(EXPORT_MODE_FREEZE))
        freeze_metric = _nominal_metric(scenario_predbat)
        scenario_predbat.prune_dead_plan_slots()
        freeze_after = scenario_predbat.export_limits_best[0]

        _set_plan(scenario_predbat, pack_export_limit(EXPORT_MODE_TARGET, 0))
        export_metric = _nominal_metric(scenario_predbat)
        scenario_predbat.prune_dead_plan_slots()
        export_after = scenario_predbat.export_limits_best[0]

    # pv_forecast_minute is keyed by absolute minute from midnight, not by minutes from now
    dark_pv = sum(scenario_predbat.pv_forecast_minute.get(minute, 0.0) for minute in range(DARK_START, DARK_END))
    print("PV in the dark slot {:.3f} kWh; nominal metric Demand {:.2f}, Freeze Export {:.2f}, export to 0% {:.2f}".format(dark_pv, demand_metric, freeze_metric, export_metric))
    print("After prune: Freeze Export -> {}, export to 0% -> {}".format(freeze_after, export_after))

    if dark_pv > 0:
        print("ERROR: the scenario forecasts {} kWh of PV in the dark slot - it no longer tests a Freeze Export without solar".format(dark_pv))
        failed = True
    if abs(freeze_metric - demand_metric) > PRUNE_EPS:
        print("ERROR: a Freeze Export with no solar scores {:.2f} against {:.2f} for Demand - it should behave exactly as Demand".format(freeze_metric, demand_metric))
        failed = True
    if export_mode_of(freeze_after) != EXPORT_MODE_IDLE:
        print("ERROR: a Freeze Export with no solar was left in the plan as {} - it should have been pruned to Demand".format(freeze_after))
        failed = True
    if export_metric > demand_metric - PRUNE_EPS:
        print("ERROR: exporting at 40p scores {:.2f}, no better than Demand at {:.2f} - the control export no longer earns anything".format(export_metric, demand_metric))
        failed = True
    if export_mode_of(export_after) != EXPORT_MODE_TARGET:
        print("ERROR: an export that earns money was pruned to {} - the prune is dropping slots that do something".format(export_after))
        failed = True

    if not failed:
        print("PASS")
    return failed

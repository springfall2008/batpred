# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Without Freeze Export, an export to a target already stands in for it.

Some inverters cannot do a Freeze Export - the GivEnergy 3-phase units are the usual case, where the charge
rate only limits grid charging and there is no pause register, so solar would still fill the battery (#5082).
For them the plan has only idle and target exports. A target export is enough: execute force-exports only
while the SoC is above the target and drops back to Demand once it is at or below it, re-checking every
cycle, so with a solar surplus the battery sits at the target while the surplus is sold.

Two halves are checked:

1. End to end: the real calculate_plan on the committed agile1 template, with a hand-built day where export
   pays 20p for 10:00-12:00 and 3p after, a 30% battery, a 3 kW solar surplus until 14:20 and an evening
   that needs a full battery. Holding the battery through the premium and filling it afterwards is the best
   plan. Planned with freeze off, the plan must export in the premium window and land within a few pence
   of the freeze-on plan, and well ahead of planning no export at all.
2. Execute: an export to a non-zero target force-exports above it and goes to Demand at and below it.
"""
import io
import contextlib
from const import CLOUD_FACTOR_PV10, EXPORT_MODE_FREEZE, EXPORT_MODE_TARGET
from tests.test_infra import reset_inverter
from tests.test_random_scenarios import RANDOM_TEMPLATE, apply_scenario_to_predbat
from utils import export_mode_of, pack_export_limit

MINUTES_NOW = 10 * 60
PREMIUM_START = 10 * 60
PREMIUM_END = 12 * 60
PV_END = 14 * 60 + 20
BATTERY_KWH = 10.0
BATTERY_KW = 3.0
# Freeze off may cost a little more than a real freeze (the stand-in cycles some solar through the battery),
# measured at 2.7p here; anything near the 61p a plan with no export loses means the stand-in was not found
MAX_GAP_TO_FREEZE = 10.0
MIN_GAIN_OVER_IDLE = 40.0


def _day(fn):
    """Build a 1440-value per-minute daily profile from a function of minute-of-day"""
    return [fn(minute) for minute in range(1440)]


def _scenario():
    """The hand-built scenario, in the random benchmark's format"""
    return {
        "id": 0,
        "seed": 0,
        "params": {
            "battery": {"soc_max_kwh": BATTERY_KWH, "initial_soc_percent": 30.0, "rate_max_charge_kw": BATTERY_KW, "rate_max_discharge_kw": BATTERY_KW},
            "inverter": {"hybrid": True, "inverter_limit_kw": 10.0, "export_limit_kw": 10.0},
            "clock": {"minutes_now": MINUTES_NOW},
            "load_scaling": {"nominal": 1.0, "pv10": 1.0, "pv90": 1.0},
        },
        "data": {
            "rate_import_day": _day(lambda minute: 7.5 if (minute >= 23 * 60 + 30 or minute < 5 * 60 + 30) else 30.0),
            "rate_export_day": _day(lambda minute: 20.0 if PREMIUM_START <= minute < PREMIUM_END else 3.0),
            "load_day_kwh": _day(lambda minute: (1.5 if 17 * 60 <= minute < 23 * 60 + 30 else 1.0) / 60.0),
            "pv_day_kw": _day(lambda minute: 4.0 if PREMIUM_START <= minute < PV_END else 0.0),
        },
    }


def _plan(my_predbat, freeze):
    """Plan the scenario with set_export_freeze as given; return the plan and its metric, plus the metric with no export windows"""
    apply_scenario_to_predbat(my_predbat, _scenario())
    # The loader leaves the template's own DC charge (1 kW) and export (2 kW) rates in place, and a battery that
    # can only take 1 kW of the solar exports most of it whatever the plan says
    my_predbat.battery_rate_max_charge_dc = BATTERY_KW / 60.0
    my_predbat.battery_rate_max_export = BATTERY_KW / 60.0
    # PV10 equal to nominal, so the plan is about the export choice rather than a PV10 hedge
    my_predbat.pv_forecast_minute10 = dict(my_predbat.pv_forecast_minute)
    my_predbat.pv_forecast_minute10_step = my_predbat.step_data_history(
        my_predbat.pv_forecast_minute10,
        my_predbat.minutes_now,
        forward=True,
        cloud_factor=min(my_predbat.metric_cloud_coverage + CLOUD_FACTOR_PV10, 1.0) if my_predbat.metric_cloud_coverage else None,
        flip=True,
    )
    my_predbat.set_export_freeze = freeze
    my_predbat.set_export_freeze_only = False
    my_predbat.calculate_best_export = True
    my_predbat.args["threads"] = 0
    my_predbat.plan_valid = False
    my_predbat.debug_enable = False
    my_predbat.calculate_plan(recompute=True)

    charge_window, charge_limit = list(my_predbat.charge_window_best), list(my_predbat.charge_limit_best)
    export_window, export_limits = list(my_predbat.export_window_best), list(my_predbat.export_limits_best)
    # Score with the prediction calculate_plan built, which took its freeze setting from this run
    metric = my_predbat.run_prediction_metric(charge_limit, charge_window, export_window, export_limits, end_record=my_predbat.end_record)[0]
    metric_idle = my_predbat.run_prediction_metric(charge_limit, charge_window, [], [], end_record=my_predbat.end_record)[0]
    return export_window, export_limits, metric, metric_idle


def _premium_exports(export_window, export_limits, mode):
    """The export windows of the given mode that overlap the premium export period"""
    return [(window, limit) for window, limit in zip(export_window, export_limits) if export_mode_of(limit) == mode and window["start"] < PREMIUM_END and window["end"] > PREMIUM_START]


def _run_plan_test():
    """End to end half: freeze off plans a target export that does nearly as well as a freeze. Returns True on failure."""
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
        freeze_windows, freeze_limits, freeze_metric, _ = _plan(scenario_predbat, True)
        target_windows, target_limits, target_metric, idle_metric = _plan(scenario_predbat, False)

    print("Freeze on: metric {:.2f} exports {}".format(freeze_metric, list(zip(freeze_windows, freeze_limits))))
    print("Freeze off: metric {:.2f} exports {}".format(target_metric, list(zip(target_windows, target_limits))))
    print("No export windows: metric {:.2f}".format(idle_metric))

    if not _premium_exports(freeze_windows, freeze_limits, EXPORT_MODE_FREEZE):
        print("ERROR: no Freeze Export planned in the premium window with freeze on - the scenario no longer exercises a freeze")
        failed = True
    if not _premium_exports(target_windows, target_limits, EXPORT_MODE_TARGET):
        print("ERROR: no target export planned in the premium window with freeze off - the solar surplus is not sold at 20p")
        failed = True
    if target_metric > freeze_metric + MAX_GAP_TO_FREEZE:
        print("ERROR: freeze off scores {:.2f}, more than {} worse than freeze on at {:.2f} - the target export does not stand in for the freeze".format(target_metric, MAX_GAP_TO_FREEZE, freeze_metric))
        failed = True
    if target_metric > idle_metric - MIN_GAIN_OVER_IDLE:
        print("ERROR: freeze off scores {:.2f}, under {} better than no export at {:.2f}".format(target_metric, MIN_GAIN_OVER_IDLE, idle_metric))
        failed = True
    return failed


def _run_execute_test():
    """Execute half: an export to 50% force-exports above the target and returns to Demand at and below it. Returns True on failure."""
    from unit_test import create_predbat
    from tests.test_execute import run_execute_test, ActiveTestInverter

    failed = False
    execute_predbat = create_predbat()
    reset_inverter(execute_predbat)
    execute_predbat.inverters = [ActiveTestInverter(0, 0, 10.0, execute_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, execute_predbat.now_utc)]
    execute_predbat.args["num_inverters"] = 2

    window = [{"start": execute_predbat.minutes_now, "end": execute_predbat.minutes_now + 60, "average": 1}]
    common = dict(export_window_best=window, export_limits_best=[pack_export_limit(EXPORT_MODE_TARGET, 50)], set_charge_window=True, set_export_window=True, set_reserve_enable=False)

    failed |= run_execute_test(
        execute_predbat,
        "target_export_above_target",
        soc_kw=5.5,
        assert_force_export=True,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        assert_discharge_end_time_minutes=execute_predbat.minutes_now + 60 + 1,
        assert_immediate_charge_soc_target_array=[0, 0],
        **common,
    )
    for soc_kw in (5.0, 4.8):
        failed |= run_execute_test(execute_predbat, "target_export_at_or_below_target_{}".format(soc_kw), soc_kw=soc_kw, assert_force_export=False, assert_status="Hold exporting", **common)
    return failed


def run_plan_freeze_export_fallback_tests(my_predbat):
    """Run the no-Freeze-Export fallback tests. Returns True on failure.

    my_predbat is deliberately unused - both halves work on fresh instances, as run_plan_pv10_hedge_tests does,
    so neither inherits nor leaves behind state on the shared fixture.
    """
    print("**** Running plan freeze export fallback tests ****")
    failed = _run_plan_test()
    failed |= _run_execute_test()
    if not failed:
        print("PASS")
    return failed

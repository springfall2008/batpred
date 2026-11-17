# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""An export has to earn its keep on the nominal metric, not only on the blended one.

optimise_export ranks its options on the blended metric (nominal, PV10 and PV90), but only selects an export
that also improves the nominal (central forecast) metric by its share of metric_min_improvement_export. That
gate is the metric - cost, less the value of what is left in the battery, plus carbon, battery cycles and self
sufficiency - in the nominal scenario, leaving out metric_keep: a low-power or freeze export can lower the
best_soc_keep penalty just by holding the battery up while the house imports, without saving anything (#2984).

The gate used to be raw nominal cost, and the detailed and plan passes only compared metric plus threshold share
when writing back, so an export the levels pass seeded could never be removed once it failed the gate: the pass
asked optimise_export, which said off, then reverted to the held export because off scored worse on the blended
metric. In #5969 that kept evening exports at 30.89p that lost money in the central forecast, paid for only by
the PV90 scenario.
"""

from tests.test_infra import reset_rates, update_rates_export, reset_inverter
from tests.test_export_threshold_per_step import SAVED_ATTRS, _save, _restore
from const import EXPORT_MODE_TARGET, EXPORT_MODE_IDLE, PV_SCENARIO_PV90
from utils import pack_export_limit, export_mode_of
from prediction import Prediction

GATE_ATTRS = SAVED_ATTRS + ("carbon_enable", "carbon_metric", "metric_self_sufficiency", "export_window", "pv_forecast_minute90_step", "load_minutes_step90")

WINDOW_OFFSET = 60
WINDOW_MINUTES = 60


class _Sim:
    """A finished simulation handle, as launch_run_prediction_export returns."""

    def __init__(self, cost=0.0, battery_cycle=0.0, metric_keep=0.0, final_carbon_g=0.0):
        self.result = (cost, 0.0, 0.0, 0.0, 0.0, 0.0, 0, battery_cycle, metric_keep, 0.0, final_carbon_g)

    def get(self):
        """Return the simulation result tuple."""
        return self.result


def setup_stubbed_window(my_predbat, export_sim, export_sim90=None):
    """One 30 minute window whose simulations are scripted: off gives _Sim(), any full export gives export_sim.

    export_sim90, when given, is the PV90 result of the export (PV90 is then weighted 0.5); the nominal result
    is used for PV10, whose weight is 0. Returns the window.
    """
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes
    window = {"start": my_predbat.minutes_now + WINDOW_OFFSET, "end": my_predbat.minutes_now + WINDOW_OFFSET + 30, "average": 30.0}
    my_predbat.export_window_best = [dict(window)]
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_IDLE)]
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.metric_min_improvement_export = 0.1
    my_predbat.metric_battery_cycle = 1.0
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_value_scaling = 1.0
    my_predbat.pv_metric10_weight = 0.0
    my_predbat.pv_metric90_weight = 0.5 if export_sim90 is not None else 0.0
    my_predbat.carbon_enable = False
    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False
    my_predbat.plan_interval_minutes = 30
    my_predbat.isExporting = False
    my_predbat.debug_enable = False
    my_predbat.rate_min_forward = {}
    my_predbat.rate_export_max_forward = {}
    my_predbat.rate_export_min_base = None

    def launch(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record):
        """Stub the simulation: off is all zeros, a full-window export gives the scripted result."""
        if export_mode_of(this_export_limit) != EXPORT_MODE_TARGET or start != window["start"]:
            return _Sim()
        if pv_scenario == PV_SCENARIO_PV90 and export_sim90 is not None:
            return export_sim90
        return export_sim

    my_predbat.launch_run_prediction_export = launch
    return window


def chosen(my_predbat):
    """The export mode optimise_export picks for the stubbed window."""
    best_export = my_predbat.optimise_export(0, 1, [], [], my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[0]
    return export_mode_of(best_export)


def test_gate_ignores_metric_keep(my_predbat):
    """An export that only lowers metric_keep is not selected, even though it lowers the metric (#2984)."""
    print("**** test_gate_ignores_metric_keep ****")
    failed = False
    saved = _save(my_predbat, GATE_ATTRS)
    try:
        setup_stubbed_window(my_predbat, _Sim(metric_keep=-5.0))
        if chosen(my_predbat) != EXPORT_MODE_IDLE:
            print("ERROR: an export whose only gain is 5p of metric_keep was selected")
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_gate_charges_battery_cycles(my_predbat):
    """An export that saves money but costs more than that in battery cycles fails the gate.

    Nominal: the export earns 10p but cycles 12 kWh at 1p/kWh, so the nominal metric is 2p worse. PV90 earns 60p
    for the same cycling, so the blended metric is 23p better. Raw nominal cost alone would have passed it.
    """
    print("**** test_gate_charges_battery_cycles ****")
    failed = False
    saved = _save(my_predbat, GATE_ATTRS)
    try:
        setup_stubbed_window(my_predbat, _Sim(cost=-10.0, battery_cycle=12.0), export_sim90=_Sim(cost=-60.0, battery_cycle=12.0))
        if chosen(my_predbat) != EXPORT_MODE_IDLE:
            print("ERROR: an export 2p worse on the nominal metric (10p earned, 12p of battery cycles) was selected on the blended metric")
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_gate_counts_carbon(my_predbat):
    """An export that costs nothing but saves carbon worth more than its threshold share passes the gate."""
    print("**** test_gate_counts_carbon ****")
    failed = False
    saved = _save(my_predbat, GATE_ATTRS)
    try:
        setup_stubbed_window(my_predbat, _Sim(final_carbon_g=-5000.0))
        my_predbat.metric_battery_cycle = 0.0
        my_predbat.carbon_enable = True
        my_predbat.carbon_metric = 1.0
        if chosen(my_predbat) != EXPORT_MODE_TARGET:
            print("ERROR: an export saving 5kg of carbon at 1p/kg (5p of metric, no cost change) was not selected")
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def setup_pv90_only_export(my_predbat, start_offset=WINDOW_OFFSET):
    """A 1 kWh battery, a 60 minute export window at 30p, then 1 kWh of house load at 35p - but none in PV90.

    Nominal: exporting earns 30p and the house then buys the same kWh back at 35p, so the export loses 5p.
    PV90 has no load, so the kWh would otherwise be left in the battery at the 5p it costs to refill, and the
    export gains 25p. Weighted 0.5, the blended metric is 10p better with the export. The export is seeded,
    as the levels pass would. Returns the window.
    """
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes

    window = {"start": my_predbat.minutes_now + start_offset, "end": my_predbat.minutes_now + start_offset + WINDOW_MINUTES, "average": 30.0}
    zero = {}
    load = {}
    evening_start = start_offset + WINDOW_MINUTES
    for minute in range(0, my_predbat.forecast_minutes, 5):
        zero[minute] = 0.0
        load[minute] = 1.0 / 24 if evening_start <= minute < evening_start + 120 else 0.0
    my_predbat.load_minutes_step = load
    my_predbat.load_minutes_step10 = load
    my_predbat.load_minutes_step90 = zero
    my_predbat.pv_forecast_minute_step = zero
    my_predbat.pv_forecast_minute10_step = zero
    my_predbat.pv_forecast_minute90_step = zero

    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = 2.0
    my_predbat.soc_kw = 1.0
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_min = 0.0
    my_predbat.reserve = 0.0
    my_predbat.metric_battery_cycle = 0.0
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_value_scaling = 1.0
    my_predbat.metric_min_improvement_export = 0.1
    my_predbat.carbon_enable = False
    my_predbat.pv_metric10_weight = 0.0
    my_predbat.pv_metric90_weight = 0.5
    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False
    my_predbat.plan_interval_minutes = 30
    my_predbat.isExporting = False
    my_predbat.debug_enable = False

    reset_rates(my_predbat, 35.0, 0.0)
    update_rates_export(my_predbat, [window])
    my_predbat.rate_min_forward = {minute: 5.0 for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 24 * 60)}
    my_predbat.rate_export_max_forward = {}
    my_predbat.rate_export_min_base = None

    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = [dict(window)]
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
    my_predbat.prediction = Prediction(my_predbat, zero, zero, load, load, zero, zero)
    return window


def pv90_only_scenario_builds(my_predbat, window):
    """Check the export is better on the blended metric and worse on the nominal one, by more than its share."""
    held = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
    off = [pack_export_limit(EXPORT_MODE_IDLE)]
    windows = [dict(window)]
    blended_held = my_predbat.run_prediction_metric([], [], windows, held, end_record=my_predbat.end_record)[0]
    blended_off = my_predbat.run_prediction_metric([], [], windows, off, end_record=my_predbat.end_record)[0]
    nominal_held = my_predbat.run_prediction_metric([], [], windows, held, end_record=my_predbat.end_record, nominal_only=True)[0]
    nominal_off = my_predbat.run_prediction_metric([], [], windows, off, end_record=my_predbat.end_record, nominal_only=True)[0]
    share = my_predbat.export_threshold_penalty(held[0], window["start"], window["end"])
    if not (blended_held < blended_off and nominal_held + share > nominal_off):
        print("ERROR: PV90-only scenario did not build - blended {} vs off {}, nominal {} vs off {}, share {}".format(blended_held, blended_off, nominal_held, nominal_off, share))
        return False
    return True


def test_detailed_pass_drops_export_failing_gate(my_predbat):
    """The detailed pass removes a held export that loses on the nominal metric, though it wins on the blended one."""
    print("**** test_detailed_pass_drops_export_failing_gate ****")
    failed = False
    saved = _save(my_predbat, GATE_ATTRS)
    try:
        window = setup_pv90_only_export(my_predbat)
        if not pv90_only_scenario_builds(my_predbat, window):
            return True
        metric, battery_value, cost, keep, cycle, carbon, import_kwh, export_kwh = my_predbat.run_prediction_metric(
            my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record
        )
        my_predbat.optimise_detailed_pass(0.0, window["average"], 0.0, 0.0, metric, cost, keep, 0.0, cycle, carbon, import_kwh, battery_value, 1, 1)
        if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
            print("ERROR: detailed pass kept the export {}, which loses 5p in the central forecast".format(my_predbat.export_limits_best[0]))
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_plan_pass_drops_export_failing_gate(my_predbat):
    """The plan pass removes a held export that loses on the nominal metric, though it wins on the blended one."""
    print("**** test_plan_pass_drops_export_failing_gate ****")
    failed = False
    saved = _save(my_predbat, GATE_ATTRS)
    try:
        window = setup_pv90_only_export(my_predbat)
        if not pv90_only_scenario_builds(my_predbat, window):
            return True
        my_predbat.optimise_plan_pass(my_predbat.end_record)
        if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
            print("ERROR: plan pass kept the export {}, which loses 5p in the central forecast".format(my_predbat.export_limits_best[0]))
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_plan_pass_keeps_running_export(my_predbat):
    """An export the inverter is already running is not cancelled part way through by the gate (#4402)."""
    print("**** test_plan_pass_keeps_running_export ****")
    failed = False
    saved = _save(my_predbat, GATE_ATTRS)
    try:
        window = setup_pv90_only_export(my_predbat, start_offset=-10)
        my_predbat.isExporting = True
        my_predbat.export_window = [dict(window)]
        my_predbat.optimise_plan_pass(my_predbat.end_record)
        if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_TARGET:
            print("ERROR: plan pass cancelled the running export, now {}".format(my_predbat.export_limits_best[0]))
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def run_export_gate_nominal_tests(my_predbat):
    """Run the nominal export gate tests."""
    failed = False
    failed |= test_gate_ignores_metric_keep(my_predbat)
    failed |= test_gate_charges_battery_cycles(my_predbat)
    failed |= test_gate_counts_carbon(my_predbat)
    failed |= test_detailed_pass_drops_export_failing_gate(my_predbat)
    failed |= test_plan_pass_drops_export_failing_gate(my_predbat)
    failed |= test_plan_pass_keeps_running_export(my_predbat)
    return failed

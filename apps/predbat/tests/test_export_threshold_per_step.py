# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""metric_min_improvement_export charged per 5 minutes of export.

Each extra 5 minutes of a forced export has to earn its share of the threshold (metric_min_improvement_export
per plan interval, scaled by export power). optimise_export picks the start - doing nothing included - with
the lowest metric plus that charge, and the second pass keeps a shorter export when it saves its charge,
instead of reverting it as a raw-metric regression. Before this, the threshold was only checked against not
exporting at all, so the cheap first minutes of an export carried a thin tail that earned almost nothing.
"""

from tests.test_infra import reset_rates, update_rates_export, reset_inverter
from const import EXPORT_MODE_TARGET, EXPORT_MODE_IDLE, EXPORT_MODE_FREEZE
from utils import pack_export_limit, export_mode_of
from prediction import Prediction

SAVED_ATTRS = (
    "forecast_minutes",
    "end_record",
    "load_minutes_step",
    "load_minutes_step10",
    "pv_forecast_minute_step",
    "pv_forecast_minute10_step",
    "calculate_best_charge",
    "calculate_best_export",
    "soc_max",
    "soc_kw",
    "inverter_hybrid",
    "inverter_loss",
    "battery_loss",
    "battery_loss_discharge",
    "best_soc_keep",
    "best_soc_min",
    "reserve",
    "metric_battery_cycle",
    "metric_min_improvement_export",
    "metric_min_improvement_export_freeze",
    "plan_interval_minutes",
    "pv_metric10_weight",
    "pv_metric90_weight",
    "set_export_freeze",
    "set_export_freeze_only",
    "set_export_low_power",
    "rate_min_forward",
    "rate_export_max_forward",
    "rate_export_min_base",
    "metric_battery_value_scaling",
    "debug_enable",
    "charge_window_best",
    "charge_limit_best",
    "export_window_best",
    "export_limits_best",
    "prediction",
    "isExporting",
    "launch_run_prediction_export",
)


_MISSING = object()


def _save(my_predbat, names):
    """Snapshot attributes on the instance, remembering which ones it did not have."""
    return {name: my_predbat.__dict__.get(name, _MISSING) for name in names}


def _restore(my_predbat, saved):
    """Put the snapshot back, removing attributes (such as a stubbed method) the instance did not have."""
    for name, value in saved.items():
        if value is _MISSING:
            my_predbat.__dict__.pop(name, None)
        else:
            setattr(my_predbat, name, value)


WINDOW_OFFSET = 60
WINDOW_MINUTES = 60


def setup_thin_tail(my_predbat):
    """A 1 kW export at 30p from a 2 kWh battery, followed by 1.5 kWh of evening load at 29.4p.

    The first 0.5 kWh exported would otherwise be left in the battery at the end, where it is only worth the 5p
    it costs to refill, so each of the first six 5 minute steps earns about 2p. The rest only displaces evening
    load the house then buys back at 29.4p, so each later step earns about 0.05p.

    Returns the export window.
    """
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)

    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes

    window = {"start": my_predbat.minutes_now + WINDOW_OFFSET, "end": my_predbat.minutes_now + WINDOW_OFFSET + WINDOW_MINUTES, "average": 30.0}

    pv_step = {}
    load_step = {}
    evening_start = WINDOW_OFFSET + WINDOW_MINUTES
    evening_end = evening_start + 4 * 60
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 1.5 / (4 * 12) if evening_start <= minute < evening_end else 0.0
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step

    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = 2.0
    my_predbat.soc_kw = 2.0
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_min = 0.0
    my_predbat.reserve = 0.0
    my_predbat.metric_battery_cycle = 0.0
    my_predbat.metric_battery_value_scaling = 1.0
    my_predbat.pv_metric10_weight = 0.0
    my_predbat.pv_metric90_weight = 0.0
    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False
    my_predbat.plan_interval_minutes = 30
    my_predbat.isExporting = False
    my_predbat.debug_enable = False

    reset_rates(my_predbat, 29.4, 0.0)
    update_rates_export(my_predbat, [window])
    # A stored kWh is worth what it costs to refill tomorrow night
    my_predbat.rate_min_forward = {minute: 5.0 for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 24 * 60)}
    my_predbat.rate_export_max_forward = {}
    my_predbat.rate_export_min_base = None

    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = [dict(window)]
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    return window


def metric_by_start(my_predbat, window):
    """The plan metric of exporting from each 5 minute start to the end of the window, plus not exporting (None)."""
    metrics = {}
    for start in list(range(window["end"] - 5, window["start"] - 5, -5)) + [None]:
        export_window = [dict(window, start=start if start is not None else window["start"])]
        limit = pack_export_limit(EXPORT_MODE_TARGET, 0) if start is not None else pack_export_limit(EXPORT_MODE_IDLE)
        metrics[start] = my_predbat.run_prediction_metric([], [], export_window, [limit], end_record=my_predbat.end_record)[0]
    return metrics


def expected_start(metrics, window, threshold, plan_interval):
    """The start with the lowest metric plus threshold per plan interval of export; None means do not export."""
    best = None
    best_score = metrics[None]
    for start in range(window["end"] - 5, window["start"] - 5, -5):
        score = metrics[start] + threshold * (window["end"] - start) / plan_interval
        if score <= best_score:
            best, best_score = start, score
    return best


def thin_tail_threshold(my_predbat, window):
    """Check the scenario has fat early steps and a thin tail, and pick a threshold that charges between them."""
    metrics = metric_by_start(my_predbat, window)
    starts = list(range(window["end"] - 5, window["start"] - 5, -5))
    gains = []
    previous = metrics[None]
    for start in starts:
        gains.append(previous - metrics[start])
        previous = metrics[start]
    fat, thin = gains[:6], gains[6:]
    if min(fat) <= 4 * max(thin) or max(thin) < 0:
        print("ERROR: thin tail scenario did not build - step gains {}".format([round(g, 3) for g in gains]))
        return None, None, metrics
    # Per 5 minute charge between the thin and fat steps, as a threshold per 30 minute plan interval
    per_step = (min(fat) * max(thin)) ** 0.5 if max(thin) > 0 else min(fat) / 10.0
    return per_step * 6, gains, metrics


def test_export_threshold_penalty(my_predbat):
    """The charge a held export carries: per minute of export from now, scaled by power; flat for freeze; none for idle."""
    print("**** test_export_threshold_penalty ****")
    failed = False
    saved = _save(my_predbat, ("metric_min_improvement_export", "metric_min_improvement_export_freeze", "plan_interval_minutes", "isExporting"))
    try:
        my_predbat.metric_min_improvement_export = 0.6
        my_predbat.metric_min_improvement_export_freeze = 0.2
        my_predbat.plan_interval_minutes = 30
        now = my_predbat.minutes_now
        cases = [
            ("idle", pack_export_limit(EXPORT_MODE_IDLE), now + 60, now + 90, 0.0),
            ("freeze", pack_export_limit(EXPORT_MODE_FREEZE), now + 60, now + 90, 0.2),
            ("30 minutes at full power", pack_export_limit(EXPORT_MODE_TARGET, 10), now + 60, now + 90, 0.6),
            ("10 minutes at full power", pack_export_limit(EXPORT_MODE_TARGET, 10), now + 80, now + 90, 0.2),
            ("30 minutes at half power", pack_export_limit(EXPORT_MODE_TARGET, 10, 0.5), now + 60, now + 90, 0.3),
            ("started 20 minutes ago, 10 left", pack_export_limit(EXPORT_MODE_TARGET, 10), now - 20, now + 10, 0.2),
        ]
        my_predbat.isExporting = False
        for label, limit, start, end, expected in cases:
            got = my_predbat.export_threshold_penalty(limit, start, end)
            if abs(got - expected) > 1e-9:
                print("ERROR: export_threshold_penalty for {} is {}, expected {}".format(label, got, expected))
                failed = True

        # An export the inverter is running carries no share, so the second pass cannot cancel it part way on the
        # threshold alone - only a raw-metric gain stops it, as the in-progress commitment (#4402) requires. A later
        # window, or one not yet running, is still charged.
        my_predbat.isExporting = True
        running = [
            ("running now", pack_export_limit(EXPORT_MODE_TARGET, 10), now - 20, now + 10, 0.0),
            ("later window while exporting", pack_export_limit(EXPORT_MODE_TARGET, 10), now + 60, now + 90, 0.6),
        ]
        for label, limit, start, end, expected in running:
            got = my_predbat.export_threshold_penalty(limit, start, end)
            if abs(got - expected) > 1e-9:
                print("ERROR: export_threshold_penalty for {} is {}, expected {}".format(label, got, expected))
                failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_optimise_export_trims_thin_tail(my_predbat):
    """optimise_export stops before the steps that do not earn their share of the threshold."""
    print("**** test_optimise_export_trims_thin_tail ****")
    failed = False
    saved = _save(my_predbat, SAVED_ATTRS)
    try:
        window = setup_thin_tail(my_predbat)
        threshold, gains, metrics = thin_tail_threshold(my_predbat, window)
        if threshold is None:
            return True
        my_predbat.metric_min_improvement_export = threshold
        want = expected_start(metrics, window, threshold, my_predbat.plan_interval_minutes)
        if want is None or want <= window["start"]:
            print("ERROR: scenario should trim the thin tail at threshold {} (gains {}), expected start {}".format(round(threshold, 3), [round(g, 3) for g in gains], want))
            return True

        best_export, best_start = my_predbat.optimise_export(0, 1, [], [], my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[:2]
        if export_mode_of(best_export) != EXPORT_MODE_TARGET or best_start != want:
            print(
                "ERROR: optimise_export chose {} from {} at threshold {}, expected an export from {} - the steps after it earn {} each, less than their share {}".format(
                    best_export, best_start, round(threshold, 3), want, [round(g, 3) for g in gains[6:]], round(threshold / 6, 3)
                )
            )
            failed = True

        # At a negligible threshold every step that earns anything is kept, so the whole window is exported
        my_predbat.metric_min_improvement_export = 0.0
        best_export, best_start = my_predbat.optimise_export(0, 1, [], [], my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[:2]
        if best_start != window["start"]:
            print("ERROR: at a zero threshold optimise_export should export the whole window from {}, chose {}".format(window["start"], best_start))
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_plan_pass_keeps_trimmed_export(my_predbat):
    """The plan pass keeps a shorter export that saves its threshold share, rather than reverting it on the raw metric."""
    print("**** test_plan_pass_keeps_trimmed_export ****")
    failed = False
    saved = _save(my_predbat, SAVED_ATTRS)
    try:
        window = setup_thin_tail(my_predbat)
        threshold, gains, metrics = thin_tail_threshold(my_predbat, window)
        if threshold is None:
            return True
        my_predbat.metric_min_improvement_export = threshold
        want = expected_start(metrics, window, threshold, my_predbat.plan_interval_minutes)

        my_predbat.optimise_plan_pass(my_predbat.end_record)
        got = my_predbat.export_window_best[0]["start"]
        if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_TARGET or got != want:
            print("ERROR: plan pass left the export {} from {}, expected it trimmed to start at {} (threshold {})".format(my_predbat.export_limits_best[0], got, want, round(threshold, 3)))
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def test_detailed_pass_adopts_trimmed_export(my_predbat):
    """The detailed pass adopts a shorter export that saves its threshold share, rather than requiring a raw-metric gain."""
    print("**** test_detailed_pass_adopts_trimmed_export ****")
    failed = False
    saved = _save(my_predbat, SAVED_ATTRS)
    try:
        window = setup_thin_tail(my_predbat)
        threshold, gains, metrics = thin_tail_threshold(my_predbat, window)
        if threshold is None:
            return True
        my_predbat.metric_min_improvement_export = threshold
        want = expected_start(metrics, window, threshold, my_predbat.plan_interval_minutes)

        metric, battery_value, cost, keep, cycle, carbon, import_kwh, export_kwh = my_predbat.run_prediction_metric(
            my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record
        )
        my_predbat.optimise_detailed_pass(0.0, window["average"], 0.0, 0.0, metric, cost, keep, 0.0, cycle, carbon, import_kwh, battery_value, 1, 1)
        got = my_predbat.export_window_best[0]["start"]
        if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_TARGET or got != want:
            print("ERROR: detailed pass left the export {} from {}, expected it trimmed to start at {} (threshold {})".format(my_predbat.export_limits_best[0], got, want, round(threshold, 3)))
            failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


class _Result:
    """A finished simulation handle, as launch_run_prediction_export returns."""

    def __init__(self, cost):
        self.cost = cost

    def get(self):
        """Return the simulation result tuple: cost and an empty battery, nothing else."""
        return (self.cost, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0, 0.0)


def test_uneven_step_gains(my_predbat):
    """A step that does not earn its share is only crossed when the steps beyond it more than pay for it.

    The step gains are stubbed, since a real plan with this shape would need a contrived load. With a charge of 1p
    per 5 minutes, gains of 10, 0, 2, 2, 0, 0 make the 10 minute step a loss that the next two repay, so the
    export runs 20 minutes. Gains of 10, 0, 1.2, 1.2, 0, 0 do not repay it, so the export stops at 5 minutes.
    """
    print("**** test_uneven_step_gains ****")
    failed = False
    saved = _save(my_predbat, SAVED_ATTRS)
    try:
        window = setup_thin_tail(my_predbat)
        window = dict(window, end=window["start"] + 30)
        my_predbat.export_window_best = [dict(window)]
        my_predbat.metric_min_improvement_export = 6.0
        my_predbat.rate_min_forward = {}
        for gains, want_minutes in (([10, 0, 2, 2, 0, 0], 20), ([10, 0, 1.2, 1.2, 0, 0], 5)):
            cost_by_minutes = {0: 0.0}
            total = 0.0
            for step, gain in enumerate(gains):
                total += gain
                cost_by_minutes[(step + 1) * 5] = -total

            def launch(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record, table=cost_by_minutes):
                """Stub the simulation: cost falls by the scripted gain for each 5 minutes exported."""
                if export_mode_of(this_export_limit) != EXPORT_MODE_TARGET:
                    return _Result(table[0])
                return _Result(table[window["end"] - start])

            my_predbat.launch_run_prediction_export = launch
            best_export, best_start = my_predbat.optimise_export(0, 1, [], [], my_predbat.export_window_best, my_predbat.export_limits_best, end_record=my_predbat.end_record)[:2]
            got_minutes = window["end"] - best_start if export_mode_of(best_export) == EXPORT_MODE_TARGET else 0
            if got_minutes != want_minutes:
                print("ERROR: step gains {} with a 1p charge per step should export {} minutes, got {}".format(gains, want_minutes, got_minutes))
                failed = True
    finally:
        _restore(my_predbat, saved)
    return failed


def run_export_threshold_per_step_tests(my_predbat):
    """Run the per-step export threshold tests."""
    failed = False
    failed |= test_export_threshold_penalty(my_predbat)
    failed |= test_optimise_export_trims_thin_tail(my_predbat)
    failed |= test_plan_pass_keeps_trimmed_export(my_predbat)
    failed |= test_detailed_pass_adopts_trimmed_export(my_predbat)
    failed |= test_uneven_step_gains(my_predbat)
    return failed

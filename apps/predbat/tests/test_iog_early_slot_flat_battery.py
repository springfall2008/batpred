# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
from datetime import timedelta

from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10
from prediction import Prediction
from tests.test_infra import reset_rates, reset_inverter, update_rates_import
from tests.test_iog_charge_skew import _snapshot_state, _restore_state

# Car attributes the car cases set, restored afterwards so they do not leak into later tests
_CAR_ATTRS = [
    "num_cars",
    "car_charging_slots",
    "car_charging_soc",
    "car_charging_limit",
    "car_charging_limit_model",
    "car_charging_loss",
    "car_charging_from_battery",
    "car_energy_reported_load",
]

# Shape taken from a real debug dump (28 Sep 2026, 22:30): battery at reserve, an Intelligent Octopus
# Go dispatch already running and running straight on into the fixed 23:30-05:30 off-peak window, the
# rest of the day on the peak rate. Minutes are from local midnight.
DEFAULT_SCENARIO = {
    "minutes_now": 21 * 60 + 30,
    "peak_rate": 30.26,
    "low_rate": 6.9,
    "load_kw": 0.6,
    "battery_kwh": 9.5,
    "charge_kw": 2.6,
    "iog_start": 21 * 60 + 30,
    "iog_end": 23 * 60 + 30,
    "night_start": 23 * 60 + 30,
    "night_end": 24 * 60 + 5 * 60 + 30,
    "inverter_loss": 0.95,
    "battery_loss": 0.96,
    "metric_battery_cycle": 0.0,
    "kernel": False,
    "car_kw": 0.0,
}
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _windows(start, end, rate):
    """30-minute low-rate charge windows covering start..end."""
    return [{"start": minute, "end": minute + 30, "average": rate} for minute in range(start, end, 30)]


def _setup(my_predbat, source, sc):
    """
    Build the flat-battery scenario and return (charge_windows, iog_indices, night_indices).

    source says how the dispatch reaches the rates:
    - "slot": an octopus_intelligent_slot dispatch, running on into the fixed window as a real one
      does, overlaid by rate_add_io_slots() - the path users of Predbat's own Octopus component take;
    - "feed": the rate feed has already lowered the dispatch minutes and marked them io_adjusted
      (is_intelligent_adjusted), as the Octopus Energy integration does.
    """
    reset_inverter(my_predbat)
    my_predbat.minutes_now = sc["minutes_now"]

    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = False
    my_predbat.soc_max = sc["battery_kwh"]
    my_predbat.soc_kw = 0.0
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_loss = sc["inverter_loss"]
    my_predbat.battery_loss = sc["battery_loss"]
    my_predbat.battery_loss_discharge = sc["battery_loss"]
    my_predbat.metric_battery_cycle = sc["metric_battery_cycle"]
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_keep_weight = 0.5
    my_predbat.reserve = 0.0
    my_predbat.set_charge_freeze = True
    my_predbat.calculate_second_pass = False
    my_predbat.debug_enable = False
    my_predbat.prediction_kernel_enable = sc["kernel"]
    my_predbat.carbon_enable = False
    my_predbat.rate_min_forward = {}

    my_predbat.battery_rate_max_charge = sc["charge_kw"] / 60.0
    my_predbat.battery_rate_max_charge_dc = sc["charge_kw"] / 60.0
    my_predbat.battery_rate_max_discharge = sc["charge_kw"] / 60.0
    my_predbat.charge_rate_now = sc["charge_kw"] / 60.0
    my_predbat.discharge_rate_now = sc["charge_kw"] / 60.0
    my_predbat.inverter_limit = sc["charge_kw"] / 60.0
    my_predbat.export_limit = sc["charge_kw"] / 60.0

    iog_windows = _windows(sc["iog_start"], sc["iog_end"], sc["low_rate"])
    night_windows = _windows(sc["night_start"], sc["night_end"], sc["low_rate"]) + _windows(sc["night_start"] + 24 * 60, sc["night_end"] + 24 * 60, sc["low_rate"])
    charge_windows = iog_windows + night_windows

    reset_rates(my_predbat, sc["peak_rate"], 0.0)
    my_predbat.io_adjusted = {}
    if source == "slot":
        update_rates_import(my_predbat, night_windows)
        my_predbat.rate_min_base = sc["low_rate"]
        my_predbat.args["octopus_slot_low_rate"] = True
        my_predbat.args["octopus_slot_max"] = 12
        my_predbat.dynamic_load_car_effective = {}
        dispatch_start = my_predbat.midnight_utc + timedelta(minutes=sc["iog_start"])
        dispatch_end = my_predbat.midnight_utc + timedelta(minutes=sc["night_start"] + 90)
        dispatch = {"start": dispatch_start.strftime(TIME_FORMAT), "end": dispatch_end.strftime(TIME_FORMAT), "charge_in_kwh": 20.0, "source": "smart-charge", "location": "AT_HOME"}
        my_predbat.rate_import = my_predbat.rate_add_io_slots(0, my_predbat.rate_import, [dispatch])
        my_predbat.rate_scan(my_predbat.rate_import, print=False)
    else:
        update_rates_import(my_predbat, charge_windows)
        for minute in range(sc["iog_start"], sc["iog_end"]):
            my_predbat.io_adjusted[minute] = True

    # A car charging through the dispatch, as on the night this came from. With car_charging_from_battery
    # off the battery is held while it charges, so an early top-up is only worth anything in the PV10
    # case if a dispatch that goes away also releases the hold.
    my_predbat.num_cars = 1 if sc["car_kw"] > 0 else 0
    if sc["car_kw"] > 0:
        car_end = sc["night_start"] + 90
        my_predbat.car_charging_slots = [[{"start": sc["iog_start"], "end": car_end, "kwh": sc["car_kw"] * (car_end - sc["iog_start"]) / 60.0, "average": sc["low_rate"], "octopus": True}]]
        my_predbat.car_charging_soc = [0.0]
        my_predbat.car_charging_limit = [100.0]
        my_predbat.car_charging_limit_model = None
        my_predbat.car_charging_loss = 1.0
        my_predbat.car_charging_from_battery = False
        my_predbat.car_energy_reported_load = True

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = sc["load_kw"] / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    iog_indices = list(range(len(iog_windows)))
    night_indices = list(range(len(iog_windows), len(charge_windows)))
    return charge_windows, iog_indices, night_indices


def _forced_costs(my_predbat, charge_windows, charged_indices, battery_kwh):
    """Nominal and PV10 cost of a fixed plan that charges to full in charged_indices only."""
    end_record = my_predbat.forecast_minutes
    limits = [battery_kwh if n in charged_indices else 0 for n in range(len(charge_windows))]
    cost = my_predbat.run_prediction(limits, charge_windows, [], [], PV_SCENARIO_NOMINAL, end_record=end_record)[0]
    cost10 = my_predbat.run_prediction(limits, charge_windows, [], [], PV_SCENARIO_PV10, end_record=end_record)[0]
    return cost, cost10


def _optimise(my_predbat, charge_windows):
    """Run optimise_all_windows from a do-nothing plan and return the chosen charge limits."""
    end_record = my_predbat.forecast_minutes
    charge_limit_best = [0 for _ in charge_windows]
    metric, _, _, _, _, _, _, _, metric_keep, _, _ = my_predbat.run_prediction(charge_limit_best, charge_windows, [], [], PV_SCENARIO_NOMINAL, end_record=end_record)
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_window_best = charge_windows
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.optimise_all_windows(metric, metric_keep)
    return my_predbat.charge_limit_best


def _run_case(my_predbat, name, source, pv_metric10_weight, overrides=None):
    """
    Run one case and print the evidence. Returns (highest charge target in the dispatch-only windows,
    PV10 saving of topping up in the first dispatch window before the night charge, the whole optimised
    plan, the future minutes flagged io_adjusted).

    The first dispatch window is inside the 30 minutes the PV10 case still trusts, so the top-up costs
    the dispatch rate in both scenarios; the saving is what it is worth if the rest of the dispatch
    goes away. The optimiser's choice alone cannot show that - with nothing to gain the top-up is a
    tie, and a tie can fall either way.
    """
    print("\n  -- case: {} (source={}, pv_metric10_weight={}) --".format(name, source, pv_metric10_weight))
    my_predbat.pv_metric10_weight = pv_metric10_weight
    sc = dict(DEFAULT_SCENARIO)
    sc.update(overrides or {})
    charge_windows, iog_indices, night_indices = _setup(my_predbat, source, sc)
    flagged = sorted(minute for minute in my_predbat.io_adjusted if minute >= my_predbat.minutes_now)
    print("     io_adjusted future minutes {}".format("{}-{}".format(flagged[0], flagged[-1]) if flagged else "none"))

    cost_iog, cost10_iog = _forced_costs(my_predbat, charge_windows, iog_indices + night_indices, sc["battery_kwh"])
    cost_night, cost10_night = _forced_costs(my_predbat, charge_windows, night_indices, sc["battery_kwh"])
    cost_top_up, cost10_top_up = _forced_costs(my_predbat, charge_windows, iog_indices[:1] + night_indices, sc["battery_kwh"])
    print("     forced plan charge dispatch+night: cost {:.2f}p  cost10 {:.2f}p".format(cost_iog, cost10_iog))
    print("     forced plan charge night only:     cost {:.2f}p  cost10 {:.2f}p".format(cost_night, cost10_night))
    print("     forced plan top-up first slot+night: cost {:.2f}p  cost10 {:.2f}p".format(cost_top_up, cost10_top_up))

    limits = _optimise(my_predbat, charge_windows)
    iog_limits = [limits[n] for n in iog_indices]
    night_limits = [limits[n] for n in night_indices]
    print("     optimised dispatch limits {} night limits {}".format(iog_limits, night_limits))
    return (max(iog_limits) if iog_limits else 0.0), cost10_night - cost10_top_up, limits, flagged


def run_iog_early_slot_flat_battery_tests(my_predbat):
    """
    Flat battery inside an Intelligent Octopus Go dispatch that runs on into the fixed 23:30 window.

    Octopus dispatches move and vanish - on the night this came from, the planned start slid from
    20:00 to 21:30 in half-hour steps before the car drew anything. The fixed 23:30-05:30 window is
    certain; the dispatch-only minutes before it are not. With the battery at reserve, charging now
    enough to carry the house to 23:30 costs nothing if the dispatch holds (it is the same 6.9p as
    the night) and saves the peak rate on that load if it goes away, because the battery is then
    empty and the house imports at 30p until 23:30.

    The only model of that risk is the PV10 re-pricing in prediction.py ("Assume in worst case that
    slot goes away"), and it keys off io_adjusted. rate_add_io_slots() used to lower the rate of an
    octopus_intelligent_slot dispatch without marking it, so for those users the dispatch minutes
    looked identical to the fixed window, charge-now and charge-later scored the same, and the plan
    charged at the back of the night window, leaving the battery flat through the uncertain part.

    Case 1 runs the dispatch through rate_add_io_slots(), which now marks the minutes it lowered.
    Case 2 is the same dispatch marked by the rate feed, which both paths must now match. Case 3 is
    the bonus-slot-with-a-peak-gap shape, kept as a guard that the re-pricing does not stop an early
    charge there. Cases 4 and 5 add the car charging through the dispatch with car_charging_from_battery
    off, in each engine: the PV10 case keeps the car charging at the dispatch rate, but a dispatch that
    goes away no longer holds the battery, so the top-up carries the house to 23:30.
    """
    print("\n**** IOG dispatch before the night window with a flat battery ****")
    failed = 0
    saved = _snapshot_state(my_predbat)
    saved_weight = my_predbat.pv_metric10_weight
    saved_args = {key: my_predbat.args[key] for key in ("octopus_slot_low_rate", "octopus_slot_max") if key in my_predbat.args}
    saved_effective = my_predbat.dynamic_load_car_effective
    saved_car = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in _CAR_ATTRS if hasattr(my_predbat, attr)}
    try:
        load_to_night = DEFAULT_SCENARIO["load_kw"] * (DEFAULT_SCENARIO["night_start"] - DEFAULT_SCENARIO["minutes_now"]) / 60.0

        slot_path, _, slot_plan, slot_flagged = _run_case(my_predbat, "dispatch from octopus_intelligent_slot", "slot", 0.15)
        feed_path, _, feed_plan, feed_flagged = _run_case(my_predbat, "same dispatch flagged by the rate feed", "feed", 0.15)
        peak_gap, _, _, _ = _run_case(
            my_predbat,
            "bonus dispatch 19:00-21:00 then peak until 23:30, 17:00 now",
            "feed",
            0.15,
            {"minutes_now": 17 * 60, "iog_start": 19 * 60, "iog_end": 21 * 60, "peak_rate": 30.0, "low_rate": 7.0, "load_kw": 1.0, "battery_kwh": 10.0, "charge_kw": 5.0, "inverter_loss": 1.0, "battery_loss": 1.0},
        )

        car_hold = {"car_kw": 7.0}
        _, car_python, _, _ = _run_case(my_predbat, "slot path, 7 kW car in the dispatch, car_charging_from_battery off", "slot", 0.15, car_hold)
        _, car_kernel, _, _ = _run_case(my_predbat, "same with the C++ kernel", "slot", 0.15, dict(car_hold, kernel=True))

        if slot_path < load_to_night:
            print("  ERROR: octopus_intelligent_slot path: battery targets {} kWh in the dispatch-only slots, needs {} kWh to carry the house to 23:30 if the dispatch goes away".format(slot_path, load_to_night))
            failed += 1
        else:
            print("  OK: octopus_intelligent_slot path charges {} kWh in the dispatch before 23:30".format(slot_path))
        if feed_path < load_to_night:
            print("  ERROR: rate feed path: battery targets {} kWh in the dispatch-only slots, needs {} kWh".format(feed_path, load_to_night))
            failed += 1
        else:
            print("  OK: rate feed path charges {} kWh in the dispatch before 23:30".format(feed_path))
        # The two paths must agree, not just each clear the bar: the overlay has to flag exactly the
        # minutes the feed flags, and the same dispatch must then produce the same plan
        if slot_flagged != feed_flagged:
            print("  ERROR: io_adjusted differs between paths: overlay flags {} future minutes, feed flags {}".format(len(slot_flagged), len(feed_flagged)))
            failed += 1
        elif slot_plan != feed_plan:
            print("  ERROR: the same dispatch plans differently by path: overlay {} feed {}".format(slot_plan, feed_plan))
            failed += 1
        else:
            print("  OK: overlay and rate feed paths flag the same minutes and plan the same charge")
        for engine, saving in (("python", car_python), ("kernel", car_kernel)):
            if saving < 1.0:
                print("  ERROR: car holding the battery ({}): a top-up in the first dispatch slot saves {:.2f}p in PV10 - a dispatch that goes away must release the hold so it can carry the house to 23:30".format(engine, saving))
                failed += 1
            else:
                print("  OK: car holding the battery ({}): a top-up in the first dispatch slot saves {:.2f}p in PV10".format(engine, saving))
        if peak_gap <= 0:
            print("  ERROR: bonus dispatch with a peak-rate gap before the night window is not charged")
            failed += 1
        else:
            print("  OK: bonus dispatch with a peak-rate gap is charged")
    finally:
        my_predbat.pv_metric10_weight = saved_weight
        for key in ("octopus_slot_low_rate", "octopus_slot_max"):
            if key in saved_args:
                my_predbat.args[key] = saved_args[key]
            else:
                my_predbat.args.pop(key, None)
        my_predbat.dynamic_load_car_effective = saved_effective
        for attr, value in saved_car.items():
            setattr(my_predbat, attr, value)
        _restore_state(my_predbat, saved)

    if failed:
        print("\n**** iog_early_slot_flat_battery tests: FAILED ({} failures) ****".format(failed))
    else:
        print("\n**** iog_early_slot_flat_battery tests: PASSED ****")
    return failed

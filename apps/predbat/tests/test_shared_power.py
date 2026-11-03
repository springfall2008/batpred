# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Regression tests for EV-first shared EV/home-battery conversion capacity."""

from const import PV_SCENARIO_NOMINAL
from prediction import Prediction
from shared_power import normalise_shared_power_groups
from tests.test_infra import TestInverter, reset_inverter, reset_rates, update_rates_import
from tests.test_kernel_parity import kernel_available


def _run_shared_case(
    my_predbat,
    kernel,
    ev_kw=0.0,
    ev_start=0,
    ev_end=60,
    ev_limit=100.0,
    car_loss=1.0,
    battery_loss=1.0,
    pv_kw=0.0,
    hybrid=False,
    groups=None,
    car_rates=None,
    inverter_rates=None,
    target=100.0,
    car_energy_reported_load=True,
    forecast_minutes=60,
):
    """Run a short forced-charge prediction and return its result plus engine selection."""
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    my_predbat.forecast_minutes = forecast_minutes
    my_predbat.prediction_kernel_enable = kernel
    my_predbat.soc_kw = 0.0
    my_predbat.soc_max = 100.0
    my_predbat.reserve = 0.0
    my_predbat.inverter_hybrid = hybrid
    my_predbat.inverter_limit = 50.0 / 60.0
    my_predbat.export_limit = 50.0 / 60.0
    my_predbat.battery_loss = battery_loss
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.inverter_loss = 1.0
    my_predbat.set_charge_window = True
    my_predbat.set_export_window = False
    my_predbat.car_charging_from_battery = False
    my_predbat.car_energy_reported_load = car_energy_reported_load
    my_predbat.car_charging_loss = car_loss

    inverter_rates = inverter_rates or [8.8]
    my_predbat.num_inverters = len(inverter_rates)
    my_predbat.inverters = []
    for inverter_n, inverter_rate in enumerate(inverter_rates):
        inverter = TestInverter()
        inverter.id = inverter_n
        inverter.battery_rate_max_charge = inverter_rate / 60.0
        my_predbat.inverters.append(inverter)
    total_battery_rate = sum(inverter_rates)
    my_predbat.battery_rate_max_charge = total_battery_rate / 60.0
    my_predbat.battery_rate_max_charge_dc = total_battery_rate / 60.0
    my_predbat.charge_rate_now = total_battery_rate / 60.0

    car_rates = list(car_rates) if car_rates is not None else ([ev_kw] if ev_kw else [])
    my_predbat.num_cars = len(car_rates)
    my_predbat.car_charging_slots = [[] for car_n in range(max(len(car_rates), 1))]
    now = my_predbat.minutes_now
    for car_n, car_rate in enumerate(car_rates):
        duration = ev_end - ev_start
        if car_rate and duration > 0:
            my_predbat.car_charging_slots[car_n] = [{"start": now + ev_start, "end": now + ev_end, "kwh": car_rate * duration / 60.0, "average": 10.0, "octopus": False}]
    my_predbat.car_charging_soc = [0.0] * max(len(car_rates), 1)
    my_predbat.car_charging_limit = [ev_limit] * max(len(car_rates), 1)
    my_predbat.car_charging_limit_model = None
    my_predbat.car_charging_battery_size = [100.0] * max(len(car_rates), 1)
    my_predbat.car_charging_soc_next = [None] * max(len(car_rates), 1)
    my_predbat.shared_power_groups = groups or []

    pv_step = {minute: pv_kw / 12.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
    zero = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
    prediction = Prediction(my_predbat, pv_step, pv_step, zero, zero)
    result = prediction.run_prediction(
        [target],
        [{"start": now, "end": now + 60, "average": 10.0}],
        [],
        [],
        PV_SCENARIO_NOMINAL,
        end_record=forecast_minutes,
    )
    return result, prediction.kernel_handle != 0


def _assert_close(label, actual, expected, tolerance=0.01):
    """Print one compact assertion result and return its failure count."""
    if abs(actual - expected) > tolerance:
        print("  ERROR: {}: got {:.4f}, expected {:.4f} (+/- {:.4f})".format(label, actual, expected, tolerance))
        return 1
    print("  OK: {}: {:.4f}".format(label, actual))
    return 0


def _run_optimizer_case(my_predbat, kernel, groups):
    """Optimise two charge windows where EV contention makes only the later one useful."""
    saved_debug_enable = my_predbat.debug_enable
    saved_threads = my_predbat.args.get("threads")
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 180
    my_predbat.prediction_kernel_enable = kernel
    my_predbat.num_inverters = 1
    my_predbat.inverters[0].battery_rate_max_charge = 1.0 / 60.0
    my_predbat.battery_rate_max_charge = 1.0 / 60.0
    my_predbat.battery_rate_max_charge_dc = 1.0 / 60.0
    my_predbat.battery_rate_max_discharge = 1.0 / 60.0
    my_predbat.battery_rate_max_export = 1.0 / 60.0
    my_predbat.charge_rate_now = 1.0 / 60.0
    my_predbat.discharge_rate_now = 1.0 / 60.0
    my_predbat.soc_max = 1.0
    my_predbat.soc_kw = 0.0
    my_predbat.reserve = 0.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.pv_metric10_weight = 0.0
    my_predbat.pv_metric90_weight = 0.0
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = False
    my_predbat.calculate_second_pass = False
    my_predbat.shared_power_groups = groups
    my_predbat.num_cars = 1
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_limit = [100.0]
    my_predbat.car_charging_limit_model = None
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_from_battery = False
    my_predbat.car_energy_reported_load = True
    now = my_predbat.minutes_now
    my_predbat.car_charging_slots = [[{"start": now, "end": now + 60, "kwh": 1.0, "average": 5.0, "octopus": False}]]

    charge_windows = [
        {"start": now, "end": now + 60, "average": 5.0},
        {"start": now + 60, "end": now + 120, "average": 10.0},
    ]
    reset_rates(my_predbat, 30.0, 5.0)
    update_rates_import(my_predbat, charge_windows)
    zero = {minute: 0.0 for minute in range(0, 180, 5)}
    load = {minute: (1.0 / 12.0 if minute >= 120 else 0.0) for minute in range(0, 180, 5)}
    my_predbat.load_minutes_step = load
    my_predbat.load_minutes_step10 = load
    my_predbat.pv_forecast_minute_step = zero
    my_predbat.pv_forecast_minute10_step = zero
    my_predbat.prediction = Prediction(my_predbat, zero, zero, load, load)
    my_predbat.debug_enable = False
    my_predbat.args["threads"] = 0
    my_predbat.charge_limit_best = [0.0, 0.0]
    my_predbat.charge_window_best = charge_windows
    my_predbat.export_limits_best = []
    my_predbat.export_window_best = []
    metric, _import_battery, _import_house, _export, _soc_min, _soc, _soc_min_minute, _cycle, metric_keep, _iboost, _carbon = my_predbat.run_prediction([0.0, 0.0], charge_windows, [], [], PV_SCENARIO_NOMINAL, end_record=180)
    try:
        my_predbat.optimise_all_windows(metric, metric_keep)
        return list(my_predbat.charge_limit_best)
    finally:
        my_predbat.debug_enable = saved_debug_enable
        if saved_threads is None:
            my_predbat.args.pop("threads", None)
        else:
            my_predbat.args["threads"] = saved_threads


def run_shared_power_tests(my_predbat):
    """Exercise shared capacity, physical calibration, compatibility and Python/C++ parity."""
    print("\n**** Shared EV and home-battery conversion capacity ****")
    failed = 0

    valid_groups = normalise_shared_power_groups([{"id": "grid_to_dc", "limit_kw": 10.5, "priority": "ev_first", "cars": [0], "inverters": [0]}], 1, 1)
    invalid_values = (
        [{"limit_kw": 0, "cars": [0], "inverters": [0]}],
        [{"limit_kw": 10.5, "priority": "battery_first", "cars": [0], "inverters": [0]}],
        [{"limit_kw": 10.5, "cars": [1], "inverters": [0]}],
    )
    for value in invalid_values:
        try:
            normalise_shared_power_groups(value, 1, 1)
            print("  ERROR: invalid configuration was accepted: {}".format(value))
            failed += 1
        except ValueError:
            pass

    engines = [False]
    available, required_failure = kernel_available()
    if required_failure:
        failed += 1
    if available:
        engines.append(True)

    engine_results = {}
    for kernel in engines:
        engine = "kernel" if kernel else "python"

        result, used_kernel = _run_shared_case(my_predbat, kernel, ev_kw=8.0, groups=[])
        failed += _assert_close("{} compatibility: disabled".format(engine), result[5], 8.8)
        if used_kernel != kernel:
            print("  ERROR: {} compatibility case used the wrong engine".format(engine))
            failed += 1

        scenarios = (
            ("no EV connected", {"ev_kw": 0.0, "car_rates": [0.0]}, 8.8),
            ("EV at capacity", {"ev_kw": 10.5}, 0.0),
            ("EV at 8 kW", {"ev_kw": 8.0}, 2.5),
            ("EV at 5 kW", {"ev_kw": 5.0}, 5.5),
            ("multiple EVs", {"car_rates": [3.0, 4.0], "groups": normalise_shared_power_groups([{"id": "two_cars", "limit_kw": 10.5, "cars": [0, 1], "inverters": [0]}], 2, 1)}, 3.5),
            ("partial interval", {"ev_kw": 8.0, "ev_start": 2, "ev_end": 57}, 3.1667),
            ("EV completion", {"ev_kw": 10.0, "ev_limit": 0.5}, 8.4417),
            ("battery target", {"ev_kw": 5.0, "target": 1.0}, 1.0),
            ("direct DC PV", {"ev_kw": 8.0, "pv_kw": 4.0, "hybrid": True}, 6.5),
            ("charging losses", {"ev_kw": 10.0 / 0.95, "car_loss": 0.95, "battery_loss": 0.94}, 0.47),
        )
        for name, arguments, expected_soc in scenarios:
            arguments.setdefault("groups", valid_groups)
            result, used_kernel = _run_shared_case(my_predbat, kernel, **arguments)
            failed += _assert_close("{} {}".format(engine, name), result[5], expected_soc, 0.015)
            if used_kernel != kernel:
                print("  ERROR: {} {} used the wrong engine".format(engine, name))
                failed += 1
            engine_results[(name, kernel)] = result[:11]

        multi_groups = normalise_shared_power_groups([{"id": "limited", "limit_kw": 5.0, "cars": [0], "inverters": [0]}], 1, 2)
        result, _used_kernel = _run_shared_case(my_predbat, kernel, ev_kw=4.0, groups=multi_groups, inverter_rates=[4.0, 4.0])
        failed += _assert_close("{} multiple inverters".format(engine), result[5], 5.0)

        inside, _used_kernel = _run_shared_case(my_predbat, kernel, ev_kw=8.0, groups=valid_groups, car_energy_reported_load=True)
        outside, _used_kernel = _run_shared_case(my_predbat, kernel, ev_kw=8.0, groups=valid_groups, car_energy_reported_load=False)
        failed += _assert_close("{} EV import inside CT counted once".format(engine), inside[1] + inside[2], 10.5)
        failed += _assert_close("{} EV outside CT remains outside house import".format(engine), outside[1] + outside[2], 2.5)

        measured = ((10.04, 0.45), (8.42, 2.09), (7.14, 3.38), (6.08, 4.45), (5.09, 5.35), (4.30, 6.11), (3.64, 6.76), (3.09, 7.31), (2.61, 7.75), (2.24, 8.10))
        for sample_n, (ev_output, battery_output) in enumerate(measured):
            result, _used_kernel = _run_shared_case(my_predbat, kernel, ev_kw=ev_output, ev_end=5, groups=valid_groups, forecast_minutes=5)
            predicted_power = result[5] * 12.0
            failed += _assert_close("{} October sample {}".format(engine, sample_n + 1), predicted_power, battery_output, 0.2)

        optimizer_groups = normalise_shared_power_groups([{"id": "optimizer", "limit_kw": 1.0, "cars": [0], "inverters": [0]}], 1, 1)
        optimizer_limits = _run_optimizer_case(my_predbat, kernel, optimizer_groups)
        if optimizer_limits[1] < 0.9:
            print("  ERROR: {} optimizer did not select the achievable post-EV window: {}".format(engine, optimizer_limits))
            failed += 1
        else:
            print("  OK: {} optimizer selected the achievable post-EV window: {}".format(engine, optimizer_limits))

    if len(engines) == 2:
        for name, _arguments, _expected_soc in scenarios:
            if engine_results[(name, False)] != engine_results[(name, True)]:
                print("  ERROR: Python/kernel mismatch for {}\n    python={}\n    kernel={}".format(name, engine_results[(name, False)], engine_results[(name, True)]))
                failed += 1

    my_predbat.prediction_kernel_enable = False
    reset_inverter(my_predbat)
    if failed:
        print("\n**** shared_power tests: FAILED ({} failures) ****".format(failed))
    else:
        print("\n**** shared_power tests: PASSED ****")
    return failed

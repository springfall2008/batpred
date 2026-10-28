# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from const import PV_SCENARIO_NOMINAL
from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates
from tests.test_kernel_parity import kernel_available

# Each car charges this much over one hour, starting now
CAR_KW = 7.0
IMPORT_RATE = 10.0


def _run_cars(my_predbat, cars, kernel, loss):
    """Charge `cars` cars at CAR_KW for an hour with no PV, no house load and an empty battery; return (grid import kWh, cost, kernel used)."""
    reset_inverter(my_predbat)
    reset_rates(my_predbat, IMPORT_RATE, 5.0)
    my_predbat.prediction_kernel_enable = kernel
    my_predbat.soc_kw = 0.0
    my_predbat.reserve = 0.0
    now = my_predbat.minutes_now
    my_predbat.num_cars = cars
    my_predbat.car_charging_slots = [[{"start": now, "end": now + 60, "kwh": CAR_KW, "average": IMPORT_RATE, "octopus": False}] if car_n < cars else [] for car_n in range(4)]
    my_predbat.car_charging_soc = [0.0] * 4
    my_predbat.car_charging_limit = [100.0] * 4
    my_predbat.car_charging_limit_model = None
    my_predbat.car_charging_loss = loss
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_from_battery = True
    zero = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
    my_predbat.prediction = Prediction(my_predbat, zero, zero, zero, zero)
    result = my_predbat.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, end_record=my_predbat.forecast_minutes)
    return result[1] + result[2], result[0], my_predbat.prediction.kernel_handle != 0


def run_multi_car_load_tests(my_predbat):
    """
    Cars charging in the same step each add their own energy to the house load once (GH#5313).

    The per-car loop used to add the running total of car energy to the load for every car, so the
    first car was counted once per car: two 7 kWh cars modelled 21 kWh, three modelled 42 kWh.
    Checked in both engines, and with a charging loss so it is the grid-side energy that is counted.
    Only cars inside the CT clamp (car_energy_reported_load) are modelled as house load; a car outside
    it goes through car_load_energy_bypass, which already accumulated each car once.
    """
    print("\n**** Multiple cars charging at once ****")
    failed = 0
    engines = [False]
    available, required_failure = kernel_available()
    if required_failure:
        failed += 1
    if available:
        engines.append(True)

    for kernel in engines:
        engine = "kernel" if kernel else "python"
        for loss in (1.0, 0.9):
            for cars in (1, 2, 3):
                expected = CAR_KW * cars
                imported, cost, used_kernel = _run_cars(my_predbat, cars, kernel, loss)
                label = "{} loss={} cars={}".format(engine, loss, cars)
                if used_kernel != kernel:
                    print("  ERROR: {}: expected the {} engine to run".format(label, engine))
                    failed += 1
                elif abs(imported - expected) > 0.01 or abs(cost - expected * IMPORT_RATE) > 0.1:
                    print("  ERROR: {}: modelled {:.2f} kWh import costing {:.2f}p, expected {:.2f} kWh costing {:.2f}p".format(label, imported, cost, expected, expected * IMPORT_RATE))
                    failed += 1
                else:
                    print("  OK: {}: {:.2f} kWh import".format(label, imported))

    my_predbat.prediction_kernel_enable = False
    if failed:
        print("\n**** multi_car_load tests: FAILED ({} failures) ****".format(failed))
    else:
        print("\n**** multi_car_load tests: PASSED ****")
    return failed

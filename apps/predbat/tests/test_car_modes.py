# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from prediction import Prediction
from prediction import Prediction
from tests.test_infra import reset_rates2, reset_inverter
from const import CAR_SOLAR_EXPORT_ALWAYS


def build_prediction(my_predbat, pv_amount=0.0, load_amount=1.0):
    """
    Build a Prediction over a flat PV/load profile, returning (prediction, pv_step, load_step)
    """
    pv_step = {}
    load_step = {}
    pv10_step = {}
    load10_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
        pv10_step[minute] = 0
        load10_step[minute] = 0
    return Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step), pv_step, load_step


def run_car_slot_adds_load_tests(my_predbat):
    """A planned car slot must add its load to the forecast whenever the car is inside the CT clamp.

    Regression guard: a car_charging_in_load_history flag once suppressed this whenever car_charging_hold
    was off, on the grounds that the historical load already carried the car. It does - but smeared across
    the times the car happened to charge before, not at the slot the plan has actually booked, so the plan
    stopped seeing an upcoming charge as demand at all and would sell the battery out from under it.
    """
    failed = False
    print("**** Running Car slot adds load tests ****")

    saved = {
        name: getattr(my_predbat, name) for name in ["num_cars", "car_charging_hold", "car_energy_reported_load", "car_charging_slots", "car_charging_limit", "car_charging_soc", "car_charging_loss", "car_charging_from_battery", "prediction_kernel_enable"]
    }
    reset_inverter(my_predbat)
    reset_rates2(my_predbat, 10.0, 5.0)
    my_predbat.num_cars = 1
    my_predbat.car_charging_limit = [10.0]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_from_battery = True
    my_predbat.car_energy_reported_load = True

    start = my_predbat.minutes_now
    slot = [{"start": start, "end": start + 60, "kwh": 3.0, "average": 10.0, "octopus": False}]

    # Both engines have their own copy of this, so pin both rather than whichever happens to be enabled
    for use_kernel in (True, False):
        my_predbat.prediction_kernel_enable = use_kernel
        failed |= check_slot_adds_load(my_predbat, start, slot, use_kernel)

    for name, value in saved.items():
        setattr(my_predbat, name, value)

    return failed


def check_slot_adds_load(my_predbat, start, slot, use_kernel):
    """Assert a planned slot raises house import on one engine, with car_charging_hold either way round."""
    failed = False
    # The slot's energy must reach the prediction's load either way round - hold only decides whether the
    # car was already taken out of the history, not whether the plan knows about the charge it has booked
    for hold in (True, False):
        my_predbat.car_charging_hold = hold
        my_predbat.car_charging_slots = [[]]
        pred_without, _, _ = build_prediction(my_predbat, pv_amount=0.0, load_amount=0.5)
        without = pred_without.run_prediction([], [], [], [], False, my_predbat.forecast_minutes)[2]

        my_predbat.car_charging_slots = [slot]
        pred_with, _, _ = build_prediction(my_predbat, pv_amount=0.0, load_amount=0.5)
        with_slot = pred_with.run_prediction([], [], [], [], False, my_predbat.forecast_minutes)[2]

        if with_slot <= without:
            print("ERROR: kernel {} car_charging_hold {} - a planned slot added no house import ({} vs {})".format(use_kernel, hold, with_slot, without))
            failed = True

    return failed


def run_car_charger_power_cap_tests(my_predbat):
    """The car cannot take more than the charger's maximum, whatever mix of sun and grid it runs on.

    Regression guard: the grid top-up was only capped by the slot's rate and the room left to
    car_charging_limit, so with a planned slot and a sunny step the model would put the sun's diversion
    and the full grid slot into the car at once - on a live system 2.93kWh into a 30 minute slot through
    a 3.68kW charger.

    This measures the Python engine only: reading the car's SoC needs debug_enable, and kernel_supported
    refuses the kernel whenever that is set. The kernel has its own copy of the cap and is covered by
    test_kernel_parity, which compares the two engines over randomised car solar configurations.
    """
    failed = False
    print("**** Running Car charger power cap tests ****")

    names = [
        "num_cars",
        "car_charging_hold",
        "car_energy_reported_load",
        "car_charging_slots",
        "car_charging_limit",
        "car_charging_soc",
        "car_charging_loss",
        "car_charging_solar",
        "car_charging_plugged",
        "car_charging_solar_limit",
        "car_charging_solar_max_power",
        "car_charging_solar_min_power",
        "car_charging_solar_power_step",
        "car_charging_solar_min_soc",
        "car_charging_solar_export_threshold",
        "car_charging_rate",
        "prediction_kernel_enable",
        "forecast_minutes",
        "end_record",
    ]
    saved = {name: getattr(my_predbat, name) for name in names}

    reset_inverter(my_predbat)
    reset_rates2(my_predbat, 10.0, 5.0)
    my_predbat.num_cars = 1
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_hold = True
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_limit = [50.0]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_rate = [7.0]
    my_predbat.car_charging_solar = [True]
    my_predbat.car_charging_plugged = [True]
    my_predbat.car_charging_solar_limit = [50.0]
    my_predbat.car_charging_solar_min_power = [0.0]
    my_predbat.car_charging_solar_power_step = [0.0]
    my_predbat.car_charging_solar_min_soc = 0.0
    my_predbat.car_charging_solar_export_threshold = [CAR_SOLAR_EXPORT_ALWAYS]

    # A 3.68kW charger with a 7kW grid slot booked and more than 3.68kW of surplus to divert. The horizon
    # is the slot itself, so what lands in the car is what the charger delivered over exactly that hour.
    max_power = 3.68
    my_predbat.car_charging_solar_max_power = [max_power]
    start = my_predbat.minutes_now
    hours = 1.0
    my_predbat.forecast_minutes = int(hours * 60)
    my_predbat.end_record = my_predbat.forecast_minutes
    my_predbat.car_charging_slots = [[{"start": start, "end": start + int(hours * 60), "kwh": 7.0 * hours, "average": 10.0, "octopus": False}]]

    pred, _, _ = build_prediction(my_predbat, pv_amount=10.0, load_amount=0.1)
    # final_car_soc is only filled in when the run is saved or debugging, so ask for the debug fields
    pred.debug_enable = True
    pred.run_prediction([], [], [], [], False, my_predbat.forecast_minutes)
    delivered = pred.final_car_soc[0]
    ceiling = max_power * hours + 0.001
    if delivered > ceiling:
        print("ERROR: car took {}kWh in {}h through a {}kW charger, ceiling {}kWh".format(round(delivered, 3), hours, max_power, round(ceiling, 3)))
        failed = True

    for name, value in saved.items():
        setattr(my_predbat, name, value)

    return failed


def run_one_plugged(my_predbat, sensor_state, now_response):
    """Read car_charging_plugged for one sensor state and car_charging_now_response list."""
    my_predbat.ha_interface.dummy_items["binary_sensor.predbat_evcc_connected"] = sensor_state
    my_predbat.args["car_charging_plugged"] = ["binary_sensor.predbat_evcc_connected"]
    my_predbat.args["car_charging_now_response"] = now_response
    my_predbat.get_car_charging_planned()
    return my_predbat.car_charging_plugged[0]


def run_car_plugged_state_tests(my_predbat):
    """
    Test that a plugged-in sensor is read on the standard on/off states, whatever car_charging_now_response holds

    The evcc component points car_charging_plugged at a binary sensor reporting exactly on/off, while
    car_charging_now_response is written for a charger's own status text - and an unquoted "on" in that
    YAML list is parsed as a boolean, so matching on it alone leaves the solar diversion silently dead.
    """
    failed = False
    print("**** Running Car plugged state tests ****")

    saved = {name: getattr(my_predbat, name) for name in ["num_cars", "car_charging_plugged"]}
    saved_args = {key: my_predbat.args.get(key) for key in ["car_charging_plugged", "car_charging_now_response", "num_cars"]}
    my_predbat.num_cars = 1
    my_predbat.args["num_cars"] = 1

    # (sensor state, car_charging_now_response, expected)
    cases = [
        # The YAML trap: unquoted on/yes/true come back as booleans, so "on" is not in the list at all
        ("on", ["true", "true", "true", "charging"], True),
        ("on", ["yes", "on", "true", "charging"], True),
        ("on", ["charging"], True),
        ("off", ["yes", "on", "true", "charging"], False),
        ("off", ["true", "true", "true", "charging"], False),
        # A charger's own status text still works through car_charging_now_response
        ("latched", ["latched", "locked"], True),
        ("waiting", ["latched", "locked"], False),
        ("unavailable", ["yes", "on"], False),
    ]
    for sensor_state, now_response, expected in cases:
        result = run_one_plugged(my_predbat, sensor_state, now_response)
        if result != expected:
            print("ERROR: car_charging_plugged for state '{}' with car_charging_now_response {} was {}, expected {}".format(sensor_state, now_response, result, expected))
            failed = True

    for name, value in saved.items():
        setattr(my_predbat, name, value)
    for key, value in saved_args.items():
        if value is None:
            my_predbat.args.pop(key, None)
        else:
            my_predbat.args[key] = value

    return failed


def run_car_charging_mode_tests(my_predbat):
    """
    Test the charging decision Predbat publishes for an external charger, with or without evcc

    Solar is the resting state so a sun-following charger keeps charging even if Predbat stops
    publishing; off is only published when it is a decision - the surplus is worth more exported, the
    home battery is below its priority level, or this car does no solar charging at all.
    """
    failed = False
    print("**** Running Car charging mode tests ****")

    saved = {name: getattr(my_predbat, name) for name in ["num_cars", "car_charging_solar", "car_charging_plugged", "car_charging_slots", "car_charging_solar_export_threshold", "rate_export"]}
    my_predbat.num_cars = 1
    my_predbat.car_charging_slots = [[]]
    my_predbat.rate_export = {my_predbat.minutes_now: 10.0}

    # (solar enabled, plugged, export threshold, grid slot now, expected mode, expected reason)
    cases = [
        (True, True, CAR_SOLAR_EXPORT_ALWAYS, True, "now", "grid_slot"),
        (True, True, CAR_SOLAR_EXPORT_ALWAYS, False, "solar", "solar"),
        # Export beats the cheap charge it would displace - the one case worth turning the charger off for
        (True, True, 5.0, False, "off", "export_better"),
        # A grid slot always wins, even when the surplus should be sold
        (True, True, 5.0, True, "now", "grid_slot"),
        # Not plugged in is an absence, not a decision - keep the charger following the sun
        (True, False, CAR_SOLAR_EXPORT_ALWAYS, False, "solar", "idle"),
        # The car does no solar charging, so there is nothing to leave it in solar for
        (False, True, CAR_SOLAR_EXPORT_ALWAYS, False, "off", "solar_disabled"),
    ]
    for solar, plugged, threshold, slot, expect_mode, expect_reason in cases:
        my_predbat.car_charging_solar = [solar]
        my_predbat.car_charging_plugged = [plugged]
        my_predbat.car_charging_solar_export_threshold = [threshold]
        solar_allowed, solar_reason = my_predbat.publish_car_solar_slot(0, "")
        mode, reason = my_predbat.publish_car_charging_mode(0, "", slot, solar_allowed, solar_reason)
        if (mode, reason) != (expect_mode, expect_reason):
            print("ERROR: solar {} plugged {} threshold {} slot {} gave ({}, {}), expected ({}, {})".format(solar, plugged, threshold, slot, mode, reason, expect_mode, expect_reason))
            failed = True

    # The home battery priority the forecast applies has to reach the published decision too, or the
    # charger diverts out of a battery the plan has already assumed it will leave alone
    saved_soc = {name: getattr(my_predbat, name) for name in ["soc_kw", "soc_max", "car_charging_solar_min_soc", "car_charging_solar_min_soc_external", "car_charging_planned", "car_charging_now"]}
    my_predbat.car_charging_solar = [True]
    my_predbat.car_charging_plugged = [True]
    my_predbat.car_charging_solar_export_threshold = [CAR_SOLAR_EXPORT_ALWAYS]
    my_predbat.car_charging_solar_min_soc = 15.0
    my_predbat.car_charging_solar_min_soc_external = False
    my_predbat.soc_max = 100.0
    my_predbat.car_charging_now = [False]

    # (battery kWh, has a grid plan to fall back on, expected mode, expected reason)
    battery_cases = [
        (50.0, True, "solar", "solar"),
        # Below the priority with a plan behind it - stop diverting, the plan still charges the car
        (5.0, True, "off", "home_battery_low"),
        # Below the priority the surplus belongs in the home battery whether or not anything is planned
        (5.0, False, "off", "home_battery_low"),
    ]
    for soc_kw, planned, expect_mode, expect_reason in battery_cases:
        my_predbat.soc_kw = soc_kw
        my_predbat.car_charging_planned = [planned]
        solar_allowed, solar_reason = my_predbat.publish_car_solar_slot(0, "")
        mode, reason = my_predbat.publish_car_charging_mode(0, "", False, solar_allowed, solar_reason)
        if (mode, reason) != (expect_mode, expect_reason):
            print("ERROR: battery {}kWh planned {} gave ({}, {}), expected ({}, {})".format(soc_kw, planned, mode, reason, expect_mode, expect_reason))
            failed = True

    # publish_car_plan runs before the inverter is read, so soc_kw is zero on the first cycle after a
    # restart. That is "not measured yet", not "empty", and must not turn a well charged battery off
    my_predbat.soc_kw = 0.0
    my_predbat.car_charging_planned = [True]
    _, reason = my_predbat.publish_car_solar_slot(0, "")
    if reason == "home_battery_low":
        print("ERROR: an unread battery (soc_kw 0) was treated as below the priority level")
        failed = True

    # A charger that applies the priority itself has already stopped diverting, so Predbat does not repeat it
    my_predbat.soc_kw = 5.0
    my_predbat.car_charging_solar_min_soc_external = True
    mode, reason = my_predbat.publish_car_charging_mode(0, "", False, *my_predbat.publish_car_solar_slot(0, ""))
    if (mode, reason) != ("solar", "solar"):
        print("ERROR: with the priority owned by the charger, expected (solar, solar), got ({}, {})".format(mode, reason))
        failed = True
    my_predbat.car_charging_solar_min_soc_external = False

    # A worse export rate still reads as export_better, so the battery reason only shows when it is the cause
    my_predbat.soc_kw = 5.0
    my_predbat.car_charging_planned = [True]
    my_predbat.car_charging_solar_export_threshold = [5.0]
    solar_allowed, solar_reason = my_predbat.publish_car_solar_slot(0, "")
    mode, reason = my_predbat.publish_car_charging_mode(0, "", False, solar_allowed, solar_reason)
    if (mode, reason) != ("off", "export_better"):
        print("ERROR: low battery and a better export gave ({}, {}), expected (off, export_better)".format(mode, reason))
        failed = True

    for name, value in saved_soc.items():
        setattr(my_predbat, name, value)
    for name, value in saved.items():
        setattr(my_predbat, name, value)

    return failed


def run_car_solar_possible_tests(my_predbat):
    """
    Test that the plan marks a slot where the charger may divert, even when nothing is expected

    A surplus too small to start the charger produces no energy, but the charger is still free to take
    it - the plan has to separate that from "no sun at all", which looks identical otherwise.
    """
    failed = False
    print("**** Running Car solar possible tests ****")

    saved = {
        name: getattr(my_predbat, name)
        for name in [
            "num_cars",
            "car_charging_solar",
            "car_charging_plugged",
            "car_charging_soc",
            "car_charging_limit",
            "car_charging_solar_limit",
            "car_charging_solar_min_power",
            "car_charging_solar_max_power",
            "car_charging_solar_min_soc",
            "car_charging_solar_export_threshold",
            "car_charging_slots",
            "car_charging_loss",
            "soc_kw",
            "soc_max",
            "prediction_kernel_enable",
        ]
    }

    my_predbat.num_cars = 1
    my_predbat.car_charging_solar = [True]
    my_predbat.car_charging_plugged = [True]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_limit = [50.0]
    my_predbat.car_charging_solar_limit = [50.0]
    my_predbat.car_charging_solar_max_power = [7.4]
    my_predbat.car_charging_solar_min_soc = 0.0
    my_predbat.car_charging_solar_export_threshold = [CAR_SOLAR_EXPORT_ALWAYS]
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_loss = 1.0
    my_predbat.soc_kw = 5.0
    my_predbat.soc_max = 10.0
    my_predbat.prediction_kernel_enable = False

    # PV well above the house load, but the charger needs more than the surplus to start
    for min_power, expect_energy in ((0.0, True), (100.0, False)):
        my_predbat.car_charging_solar_min_power = [min_power]
        pred, _, _ = build_prediction(my_predbat, pv_amount=1.0, load_amount=0.1)
        pred.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")
        diverted = max(pred.predict_car_solar_best.values()) if pred.predict_car_solar_best else 0
        possible = any(pred.predict_car_solar_possible_best.values())
        if not possible:
            print("ERROR: min_power {} should still mark the slot as a diversion opportunity".format(min_power))
            failed = True
        if (diverted > 0) != expect_energy:
            print("ERROR: min_power {} diverted {}kWh, expected {}".format(min_power, diverted, "some" if expect_energy else "none"))
            failed = True

    # Not plugged in is not an opportunity
    my_predbat.car_charging_plugged = [False]
    my_predbat.car_charging_solar_min_power = [0.0]
    pred, _, _ = build_prediction(my_predbat, pv_amount=1.0, load_amount=0.1)
    pred.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")
    if any(pred.predict_car_solar_possible_best.values()):
        print("ERROR: an unplugged car must not mark a diversion opportunity")
        failed = True

    for name, value in saved.items():
        setattr(my_predbat, name, value)

    return failed

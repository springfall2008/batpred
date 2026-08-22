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


def run_one(my_predbat, in_load_history, reported_load=True, octopus=False, average=0.0):
    """
    Run a single prediction with one car slot, returning (import_kwh_house, metric, car_soc_next)
    """
    my_predbat.num_cars = 1
    my_predbat.car_energy_reported_load = reported_load
    my_predbat.car_charging_in_load_history = in_load_history
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_soc = [0, 0, 0, 0]
    my_predbat.car_charging_limit = [50, 100, 100, 100]
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 300, "kwh": 10.0, "average": average, "octopus": octopus}]

    prediction, _, _ = build_prediction(my_predbat)
    result = prediction.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save=None)
    metric = result[0]
    import_kwh_house = result[2]
    car_charging_soc_next = result[12]
    return import_kwh_house, metric, car_charging_soc_next[0]


def run_car_charging_in_load_history_tests(my_predbat):
    """
    Test that planned car slots stop adding load when the car energy is already in the load history
    """
    failed = False
    print("**** Running Car charging in load history tests ****")

    reset_inverter(my_predbat)
    reset_rates2(my_predbat, 10.0, 5.0)

    saved = {
        name: getattr(my_predbat, name)
        for name in ["num_cars", "car_energy_reported_load", "car_charging_in_load_history", "car_charging_loss", "car_charging_soc", "car_charging_limit", "car_charging_slots", "car_charging_from_battery", "soc_kw", "soc_max", "prediction_kernel_enable"]
    }
    # Run against the Python engine - the kernel mirror of this guard is covered by test_kernel_parity
    my_predbat.prediction_kernel_enable = False
    my_predbat.car_charging_from_battery = True
    my_predbat.soc_kw = 0
    my_predbat.soc_max = 10.0
    my_predbat.car_charging_slots = [[], [], [], []]

    # Test 1: the planned slot's energy is not added a second time when it is already in the load history.
    # The car draws 10kWh over 5 hours, so the whole difference must be exactly that 10kWh.
    import_off, _, soc_off = run_one(my_predbat, in_load_history=False)
    import_on, _, soc_on = run_one(my_predbat, in_load_history=True)
    difference = import_off - import_on
    if abs(difference - 10.0) >= 0.01:
        print("ERROR: House import should drop by the full car slot (10.0 kWh) when it is already in the load history, got {}".format(difference))
        failed = True

    # Test 2: the car is still modelled - its SoC must advance identically, the guard only stops the load being
    # double counted. A naive fix that skipped the whole car block would break this.
    if abs(soc_off - soc_on) >= 0.001:
        print("ERROR: Car SoC should be unaffected by car_charging_in_load_history, got {} vs {}".format(soc_off, soc_on))
        failed = True

    # Test 3: with the car outside the CT clamp the bypass branch runs instead, so the switch changes nothing.
    # (fetch_config_options forces the switch Off in this configuration, this pins the prediction side.)
    import_bypass_off, metric_bypass_off, _ = run_one(my_predbat, in_load_history=False, reported_load=False)
    import_bypass_on, metric_bypass_on, _ = run_one(my_predbat, in_load_history=True, reported_load=False)
    if abs(import_bypass_off - import_bypass_on) >= 0.001 or abs(metric_bypass_off - metric_bypass_on) >= 0.001:
        print("ERROR: With car_energy_reported_load Off the guard must have no effect, got import {} vs {}, metric {} vs {}".format(import_bypass_off, import_bypass_on, metric_bypass_off, metric_bypass_on))
        failed = True

    # Test 4: the IOG beyond-cap premium is still charged. car_amount_premium must keep accumulating even when
    # the load is not added, because the grid import still contains the car when it comes from history.
    _, metric_premium_on, _ = run_one(my_predbat, in_load_history=True, octopus=True, average=40.0)
    _, metric_no_premium, _ = run_one(my_predbat, in_load_history=True, octopus=True, average=0.0)
    if metric_premium_on <= metric_no_premium:
        print("ERROR: IOG beyond-cap premium should still be charged with the guard on, got {} vs {}".format(metric_premium_on, metric_no_premium))
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
    saved_soc = {name: getattr(my_predbat, name) for name in ["soc_kw", "soc_max", "car_charging_solar_min_soc", "car_charging_planned", "car_charging_now"]}
    my_predbat.car_charging_solar = [True]
    my_predbat.car_charging_plugged = [True]
    my_predbat.car_charging_solar_export_threshold = [CAR_SOLAR_EXPORT_ALWAYS]
    my_predbat.car_charging_solar_min_soc = 15.0
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

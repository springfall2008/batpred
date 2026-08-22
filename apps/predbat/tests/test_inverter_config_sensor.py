# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from const import MINUTE_WATT
from tests.test_infra import reset_inverter, TestInverter


def make_stub_inverter(inverter_id, inverter_limit_watts):
    """Build an inverter stub carrying just the values fetch_inverter_data() aggregates"""
    inverter = TestInverter()
    inverter.id = inverter_id
    inverter.update_status = lambda minutes_now, quiet=False: None
    inverter.charge_window = []
    inverter.export_window = []
    inverter.export_limits = []
    inverter.inv_support_discharge_freeze = True
    inverter.inv_support_charge_freeze = True
    inverter.inv_has_reserve_soc = True
    inverter.current_charge_limit = 100.0
    inverter.soc_max = 5.0
    inverter.soc_kw = 2.0
    inverter.reserve = 0.5
    inverter.reserve_current = 0.5
    inverter.battery_rate_max_charge = 2000 / MINUTE_WATT
    inverter.battery_rate_max_charge_dc = 2000 / MINUTE_WATT
    inverter.battery_rate_max_discharge = 2000 / MINUTE_WATT
    inverter.battery_rate_max_export = 2000 / MINUTE_WATT
    inverter.charge_rate_now = 2000 / MINUTE_WATT
    inverter.discharge_rate_now = 2000 / MINUTE_WATT
    inverter.battery_rate_min = 0
    inverter.inverter_limit = inverter_limit_watts / MINUTE_WATT
    inverter.export_limit = inverter_limit_watts / MINUTE_WATT
    inverter.pv_power = 0
    inverter.load_power = 0
    inverter.battery_power = 0
    inverter.grid_power = 0
    inverter.battery_temperature = 20
    return inverter


def test_inverter_config_sensor(my_predbat):
    """
    The static inputs the prediction runs from - the AC/battery power limits, the system topology
    and the planning scalars - are aggregated across all inverters but were never published, so a
    user could only see them by turning on debug logging. publish_inverter_config() exposes them as
    attributes of a single sensor.
    """
    # Every test in the suite shares one PredBat instance, and to prove each value is published from
    # the attribute it claims to come from this test has to move them all away from the fixture
    # defaults. Snapshot the attribute bindings up front and put them back before returning, so none
    # of that reaches the next test - a leaked 48 hour forecast_minutes ran the C++ prediction kernel
    # off the end of the fixture's 24 hours of step data and crashed model_kernel.
    saved_state = dict(my_predbat.__dict__)
    args_had_num_inverters = "num_inverters" in my_predbat.args
    saved_num_inverters = my_predbat.args.get("num_inverters", None)
    try:
        failed = run_inverter_config_checks(my_predbat)
    finally:
        my_predbat.__dict__.clear()
        my_predbat.__dict__.update(saved_state)
        if args_had_num_inverters:
            my_predbat.args["num_inverters"] = saved_num_inverters
        else:
            my_predbat.args.pop("num_inverters", None)
    return failed


def run_inverter_config_checks(my_predbat):
    """Check sensor.<prefix>_inverter_config against known values, leaving my_predbat dirty"""
    reset_inverter(my_predbat)
    failed = False

    my_predbat.inverter_limit = 6000 / MINUTE_WATT
    my_predbat.export_limit = 5000 / MINUTE_WATT
    my_predbat.pv_ac_limit = 3600 / MINUTE_WATT
    my_predbat.battery_rate_max_charge = 2500 / MINUTE_WATT
    my_predbat.battery_rate_max_charge_dc = 4000 / MINUTE_WATT
    my_predbat.battery_rate_max_discharge = 2600 / MINUTE_WATT
    my_predbat.battery_rate_max_export = 2400 / MINUTE_WATT
    my_predbat.battery_rate_min = 200 / MINUTE_WATT
    my_predbat.soc_max = 9.52
    my_predbat.reserve = 0.95
    my_predbat.num_inverters = 2
    my_predbat.num_cars = 1
    my_predbat.inverter_can_charge_during_export = False
    my_predbat.metric_standing_charge = 42.5
    my_predbat.forecast_minutes = 48 * 60
    my_predbat.plan_interval_minutes = 30

    my_predbat.publish_inverter_config()

    entity_id = "sensor." + my_predbat.prefix + "_inverter_config"
    item = my_predbat.ha_interface.dummy_items.get(entity_id)
    if item is None:
        print("ERROR: {} was not published".format(entity_id))
        return True

    print("Test: state is the AC inverter limit in kW with a power device_class")
    expect_meta = {
        "state": 6.0,
        "friendly_name": "Predbat Inverter Config",
        "state_class": "measurement",
        "unit_of_measurement": "kW",
        "device_class": "power",
    }
    for key, value in expect_meta.items():
        if item.get(key, None) != value:
            print("ERROR: {} {} is {} expected {}".format(entity_id, key, item.get(key, None), value))
            failed = True

    print("Test: power limits are published in kW, converted from the internal kW/minute form")
    expect_power = {
        "inverter_limit": 6.0,
        "export_limit": 5.0,
        "pv_ac_limit": 3.6,
        "battery_rate_max_charge": 2.5,
        "battery_rate_max_charge_dc": 4.0,
        "battery_rate_max_discharge": 2.6,
        "battery_rate_max_export": 2.4,
        "battery_rate_min": 0.2,
    }
    for key, value in expect_power.items():
        if item.get(key, None) != value:
            print("ERROR: {} attribute {} is {} expected {}".format(entity_id, key, item.get(key, None), value))
            failed = True

    print("Test: capacity, topology and planning scalars are published as-is")
    expect_plain = {
        "soc_max": 9.52,
        "reserve": 0.95,
        "num_inverters": 2,
        "num_cars": 1,
        "inverter_can_charge_during_export": False,
        "metric_standing_charge": 42.5,
        "forecast_minutes": 48 * 60,
        "plan_interval_minutes": 30,
    }
    for key, value in expect_plain.items():
        if item.get(key, None) != value:
            print("ERROR: {} attribute {} is {} expected {}".format(entity_id, key, item.get(key, None), value))
            failed = True

    print("Test: a zero limit still publishes rather than being dropped")
    my_predbat.pv_ac_limit = 0
    my_predbat.publish_inverter_config()
    item = my_predbat.ha_interface.dummy_items.get(entity_id)
    if item.get("pv_ac_limit", None) != 0:
        print("ERROR: {} attribute pv_ac_limit is {} expected 0".format(entity_id, item.get("pv_ac_limit", None)))
        failed = True

    print("Test: a normal inverter fetch publishes the sensor with the fleet totals")
    my_predbat.args["num_inverters"] = 2
    my_predbat.inverters = [make_stub_inverter(0, 3000), make_stub_inverter(1, 3000)]
    my_predbat.computed_charge_curve = True
    my_predbat.computed_discharge_curve = True
    my_predbat.battery_charge_power_curve_auto = False
    my_predbat.battery_discharge_power_curve_auto = False
    my_predbat.ha_interface.dummy_items.pop(entity_id, None)

    my_predbat.fetch_inverter_data(create=False)

    item = my_predbat.ha_interface.dummy_items.get(entity_id)
    if item is None:
        print("ERROR: {} was not published by fetch_inverter_data".format(entity_id))
        failed = True
    else:
        for key, value in {"state": 6.0, "inverter_limit": 6.0, "export_limit": 6.0, "soc_max": 10.0, "reserve": 1.0, "num_inverters": 2}.items():
            if item.get(key, None) != value:
                print("ERROR: {} {} is {} expected {} after fetch_inverter_data".format(entity_id, key, item.get(key, None), value))
                failed = True

    return failed

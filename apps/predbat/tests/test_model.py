# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import reset_rates, reset_inverter, simple_scenario, reset_rates2

def run_model_tests(my_predbat):
    print("**** Running Model tests ****")
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    reset_rates(my_predbat, import_rate, export_rate)

    failed = False
    failed |= simple_scenario("zero", my_predbat, 0, 0, 0, 0, with_battery=False)
    failed |= simple_scenario("load_only", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("load_bat_ac", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)
    failed |= simple_scenario("load_bat_ac2", my_predbat, 0.5, 0, assert_final_metric=0, assert_final_soc=100 - (24 * 0.5) / 0.8, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc2", my_predbat, 0.5, 0, assert_final_metric=0, assert_final_soc=100 - (24 * 0.5) / 0.8, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)
    failed |= simple_scenario("load_bat_ac3", my_predbat, 1.0, 0, assert_final_metric=import_rate * 0.2 * 24, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc3", my_predbat, 1.0, 0, assert_final_metric=import_rate * 0.2 * 24, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)

    failed |= simple_scenario("load_empty_bat1", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=4, with_battery=True, battery_soc=4, reserve=4)
    failed |= simple_scenario("load_empty_bat2", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=3, with_battery=True, battery_soc=3, reserve=4)
    failed |= simple_scenario("load_empty_bat_chrg1", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5 + import_rate * 1, assert_final_soc=4, with_battery=True, battery_soc=3, reserve=4, charge=4)
    failed |= simple_scenario("load_empty_bat_chrg2", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5 + import_rate * 2, assert_final_soc=5, with_battery=True, battery_soc=3, reserve=4, charge=5)
    failed |= simple_scenario("load_empty_bat_chrg3", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=5, with_battery=True, battery_soc=5, reserve=4, charge=5)

    failed |= simple_scenario(
        "hold_during discharge",
        my_predbat,
        0.1,
        0,
        assert_final_metric=import_rate,
        assert_final_soc=5 - 23 * 0.1,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge2",
        my_predbat,
        0.1,
        0,
        assert_final_metric=import_rate,
        assert_final_soc=5 - 23 * 0.1 / 0.8,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge3",
        my_predbat,
        0.1,
        0,
        assert_final_metric=import_rate,
        assert_final_soc=5 - 23 * 0.1 / 0.8,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=True,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv1",
        my_predbat,
        0.1,
        0.1,
        assert_final_metric=0,
        assert_final_soc=5,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv2",
        my_predbat,
        0.1,
        0.2,
        assert_final_metric=0,
        assert_final_soc=5 + 0.1 * 24,
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv3",
        my_predbat,
        0.1,
        0.2,
        assert_final_metric=0,
        assert_final_soc=5 + 0.1 * 24,  # For AC Coupled PV arrives as AC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=False,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv4",
        my_predbat,
        0.1,
        0.2,
        assert_final_metric=0,
        assert_final_soc=5 + ((0.2 * 0.8) - 0.1) * 24,  # For DC Coupled PV arrives as DC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=True,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv5",
        my_predbat,
        0.2,
        0.1,
        assert_final_metric=0.1 * import_rate,
        assert_final_soc=5 - (0.1 / 0.8) * 23,  # For AC Coupled PV arrives as AC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=False,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "hold_during discharge_pv6",
        my_predbat,
        0.2,
        0.1,
        assert_final_metric=0.1 * import_rate,
        assert_final_soc=5 - ((0.2 / 0.8) - 0.1) * 23,  # For DC Coupled PV arrives as DC
        with_battery=True,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": import_rate}],
        charge_limit_best=[0.5],
        battery_soc=5.0,
        set_charge_freeze=True,
        reserve=0.5,
        inverter_loss=0.8,
        hybrid=True,
    )
    if failed:
        return failed

    failed |= simple_scenario(
        "load_bat_dc_pv",
        my_predbat,
        4,
        0.5,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=100 - 24 * 0.5,
        with_battery=True,
        battery_soc=100.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario(
        "load_bat_dc_pv2",
        my_predbat,
        4,
        4,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=50 + 24,
        with_battery=True,
        battery_soc=50.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario("load_carbon", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False, carbon=3, assert_final_carbon=3 * 24)
    failed |= simple_scenario(
        "load_carbon_loss_ac",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
    )
    failed |= simple_scenario(
        "load_carbon_loss_dc",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario(
        "pv_carbon_ac",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=-3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
    )
    failed |= simple_scenario(
        "pv_carbon_dc",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24 * 0.8,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=-3 * 24 * 0.8,
        inverter_limit=3.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario("load_car", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 3, assert_final_soc=0, with_battery=False, charge_car=2.0)
    failed |= simple_scenario("load_car_bat_yes", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 2, assert_final_soc=100.0 - 24 * 1, with_battery=True, charge_car=2.0, battery_soc=100.0)
    failed |= simple_scenario(
        "load_car_bat_no",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24 * 3,
        assert_final_soc=100.0,
        with_battery=True,
        charge_car=2.0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no2",
        my_predbat,
        1,
        0,
        assert_final_metric=0,
        assert_final_soc=100.0 - 24,
        with_battery=True,
        charge_car=0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no3",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 3,
        assert_final_soc=100.0 - 24,
        with_battery=True,
        charge_car=60,
        car_soc=97.0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )

    failed |= simple_scenario("load_discharge", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge2", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 24, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario("load_discharge3", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 48, battery_soc=100.0, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("load_discharge4", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=100.0, with_battery=True, battery_loss=0.1)

    # Discharge curve has 0.05 for -9 which is 0.5 max rate
    failed |= simple_scenario("discharge_curve1", my_predbat, 1, 0, assert_final_metric=import_rate * 20 * 0.5 + 4 * import_rate, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_size=10, battery_temperature=-9)
    # Discharge curve has 0.01 for -10 which is 0.1 max rate
    failed |= simple_scenario("discharge_curve2", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 0.90, assert_final_soc=7.6, battery_soc=10.0, with_battery=True, battery_temperature=-10, battery_size=10)

    failed |= simple_scenario(
        "load_discharge_car",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 14 * 4.5 + import_rate * 10 * 3.5,
        assert_final_soc=0,
        battery_soc=10.0,
        with_battery=True,
        charge_car=4.0,
    )
    failed |= simple_scenario(
        "load_discharge_car2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 24 * 1.5,
        assert_final_soc=100 - 24 * 2.5,
        battery_soc=100.0,
        with_battery=True,
        charge_car=4.0,
        discharge=0,
        inverter_limit=3.5,
        battery_rate_max_charge=2.5,
    )
    failed |= simple_scenario("load_discharge_fast", my_predbat, 2, 0, assert_final_metric=import_rate * 38, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge_fast_big", my_predbat, 2, 0, assert_final_metric=import_rate * 24, assert_final_soc=76, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario("load_discharge_reserve", my_predbat, 1, 0, assert_final_metric=import_rate * 15, assert_final_soc=1, battery_soc=10.0, with_battery=True, reserve=1.0)
    failed |= simple_scenario("load_discharge_reserve2", my_predbat, 1, 0, assert_final_metric=import_rate * 20, assert_final_soc=2, battery_soc=10.0, with_battery=True, reserve=2.0, battery_loss=0.5)
    failed |= simple_scenario("load_discharge_loss", my_predbat, 1, 0, assert_final_metric=import_rate * 19, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("load_pv", my_predbat, 1, 1, assert_final_metric=0, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv10_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, pv10=True)
    failed |= simple_scenario("pv_only_loss_ac", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, inverter_loss=0.5)
    failed |= simple_scenario("pv_only_loss_hybrid", my_predbat, 0, 1, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=0, with_battery=False, inverter_loss=0.5, hybrid=True)
    failed |= simple_scenario("pv_only_bat", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_loss", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=12, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("pv_only_bat_100%", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, battery_size=10)
    failed |= simple_scenario("pv_only_bat_ac_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 48, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5)
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit_loss",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 0.1,
        assert_final_soc=12,
        with_battery=True,
        export_limit=0.1,
        inverter_loss=0.5,
    )
    failed |= simple_scenario("pv_only_bat_ac_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5)
    failed |= simple_scenario("pv_only_bat_dc_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario("pv_only_bat_dc_clips2l", my_predbat, 0, 2, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5)
    failed |= simple_scenario("pv_only_bat_dc_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario("pv_only_bat_dc_clips3l", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5)
    failed |= simple_scenario(
        "pv_only_bat_dc_clips3l2",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        hybrid=True,
        inverter_loss=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario("pv_only_bat_dc_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5)
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit_loss",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 0.1,
        assert_final_soc=24,
        with_battery=True,
        hybrid=True,
        export_limit=0.1,
        inverter_loss=0.5,
    )
    failed |= simple_scenario("pv_only_bat_dc_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5)
    failed |= simple_scenario("battery_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)

    failed |= simple_scenario("battery_charge_low_off", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=24.59)
    failed |= simple_scenario("battery_charge_low_on", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=88.89)
    failed |= simple_scenario(
        "battery_charge_low_on_monitor", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=24.59, set_charge_window=False
    )

    failed |= simple_scenario(
        "battery_charge_low_temp1", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=24.59, battery_temperature=20
    )
    failed |= simple_scenario(
        "battery_charge_low_temp2", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=80.00, battery_temperature=1
    )
    failed |= simple_scenario(
        "battery_charge_low_temp3", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=88.89, battery_temperature=1
    )

    if failed:
        return failed
    failed |= simple_scenario("battery_charge_prev_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)

    failed |= simple_scenario(
        "battery_charge_freeze",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 24 * 0.5,
        assert_final_soc=5,
        with_battery=True,
        charge=0.5,
        battery_soc=5,
        battery_size=10,
        reserve=0.5,
        set_charge_freeze=True,
    )
    failed |= simple_scenario(
        "battery_charge_freeze2",
        my_predbat,
        0.5,
        1,
        assert_final_metric=0,
        assert_final_soc=5 + 0.5 * 24,
        with_battery=True,
        charge=0.5,
        battery_soc=5,
        battery_size=100,
        reserve=0.5,
        set_charge_freeze=True,
    )
    failed |= simple_scenario("battery_charge_load", my_predbat, 1, 0, assert_final_metric=import_rate * 34, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_load2", my_predbat, 2, 0, assert_final_metric=import_rate * (34 + 24), assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv2", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv3", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv4_ac", my_predbat, 0, 2, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5, inverter_limit=2)
    failed |= simple_scenario(
        "battery_charge_pv4_dc",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_ac",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_dc",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv6_ac",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 2,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
    )
    failed |= simple_scenario(
        "battery_charge_pv6_dc",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv_term_dc1",
        my_predbat,
        0,
        0.5,
        assert_final_metric=import_rate * 10 * 0.5,
        assert_final_soc=10 + 14 * 0.5,
        with_battery=True,
        charge=10,
        battery_size=100,
        hybrid=True,
        assert_keep=0,
    )
    failed |= simple_scenario(
        "battery_charge_pv_term_dc2",
        my_predbat,
        0,
        0.5,
        assert_final_metric=import_rate * 10 * 0.5,
        assert_final_soc=10 + 14 * 0.5,
        with_battery=True,
        charge=9.95,
        battery_size=100,
        hybrid=True,
        assert_keep=((1 / 60 * 5) - 0.05) * import_rate,
    )
    failed |= simple_scenario(
        "battery_charge_pv_load1",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 10 - export_rate * 14 * 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
    )
    failed |= simple_scenario("battery_charge_pv_load2_ac", my_predbat, 0.5, 1, assert_final_metric=import_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0)
    failed |= simple_scenario(
        "battery_charge_pv_load2_hybrid",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_soc=0,
        hybrid=True,
    )
    failed |= simple_scenario("battery_charge_pv_load3_ac", my_predbat, 0.5, 2, assert_final_metric=-export_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0)
    failed |= simple_scenario(
        "battery_charge_pv_load3_hybrid",
        my_predbat,
        0.5,
        2,
        assert_final_metric=-export_rate * 0.5 * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_soc=0,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_part1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 1,
        assert_final_soc=1,
        with_battery=True,
        charge=10,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": 10}],
    )
    failed |= simple_scenario(
        "battery_charge_part1.5",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 1.5,
        assert_final_soc=1.5,
        with_battery=True,
        charge=10,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 150, "average": 10}],
    )
    failed |= simple_scenario("battery_discharge", my_predbat, 0, 0, assert_final_metric=-export_rate * 10, assert_final_soc=0, with_battery=True, discharge=0, battery_soc=10)
    failed |= simple_scenario(
        "battery_discharge_keep",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.5 + ((1 + (1 / 12)) * import_rate * 0.5 * 0.5),
        keep=1,
        keep_weight=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_keep2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 1,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=1,
        assert_keep=23 * import_rate * 0.5 + ((1 + (1 / 12)) * import_rate * 0.5 * 0.5),
        keep=1,
        keep_weight=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_loss2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.25,
        assert_final_soc=100 - 24 * 0.5,
        battery_soc=100.0,
        with_battery=True,
        inverter_loss=0.5,
        discharge=0,
        inverter_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_load",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate + 1 * import_rate * 0.5,
        keep=1.0,
        keep_weight=1.0,
    )
    failed |= simple_scenario(
        "battery_load_keep_four_hour",
        my_predbat,
        1.0,
        0,
        assert_final_metric=import_rate * 20,
        assert_final_soc=0,
        with_battery=True,
        battery_soc=4,
        assert_keep=20 * import_rate * 4 + 53,
        keep=4.0,
        keep_weight=1.0,
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep_mode_test1",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.8 + 1 * import_rate * 0.8 * 0.5,
        keep=1.0,
        keep_weight=0.8,
        save="test",
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep_mode_test2",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.8 + 1 * import_rate * 0.8 * 0.5,
        keep=1.0,
        keep_weight=0.8,
        save="none",
    )
    failed |= simple_scenario(
        "battery_discharge_pv_ac",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 10 - export_rate * 24 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv_ac_load",
        my_predbat,
        0.1,
        0.5,
        assert_final_metric=-export_rate * 9 - export_rate * 24 * 0.4,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv2_ac",
        my_predbat,
        0,
        1.5,
        assert_final_metric=-export_rate * 10 * 2.5 - export_rate * 14 * 1.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv3_ac",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 10 * 3 - export_rate * 14 * 2,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv4_ac",
        my_predbat,
        0,
        5.0,
        assert_final_metric=-export_rate * 10 * 6 - export_rate * 14 * 5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv5_ac",
        my_predbat,
        1,
        5.0,
        assert_final_metric=-export_rate * 24 * 4.5,
        assert_final_soc=50 - 24 * 1,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        inverter_limit=2,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_pv_hybrid",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 20 - export_rate * 4 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        hybrid=True,
    )
    failed |= simple_scenario("battery_discharge_pv2_hybrid", my_predbat, 0, 1.5, assert_final_metric=-export_rate * 24, assert_final_soc=22, with_battery=True, discharge=0, battery_soc=10, hybrid=True)
    failed |= simple_scenario("battery_discharge_pv3_hybrid", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, discharge=0, battery_soc=0, hybrid=True)
    failed |= simple_scenario(
        "battery_discharge_pv4_hybrid",
        my_predbat,
        1,
        5,
        assert_final_metric=0,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        hybrid=True,
        inverter_limit=2,
        inverter_loss=0.5,
    )
    failed |= simple_scenario("battery_discharge_freeze", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10)
    failed |= simple_scenario("battery_discharge_freeze2", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10, set_export_freeze_only=True)
    failed |= simple_scenario("battery_discharge_freeze_only", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=0, battery_soc=10, set_export_freeze_only=True)

    failed |= simple_scenario("battery_discharge_hold", my_predbat, 0, 0.5, assert_final_metric=-0, assert_final_soc=10 + 24 * 0.5, with_battery=True, discharge=98, battery_soc=10)
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 - 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv2",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.0 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv4",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.0 * 24 * 0.5,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv5",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        battery_rate_max_charge=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv6",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        battery_rate_max_charge=1.0,
        inverter_can_charge_during_export=False,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 - 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.0,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv2",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv3",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 1.0,
        assert_final_soc=50 + 0 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=1.0,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv4",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10 / 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10 / 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario("battery_charge_ac_loss_pv", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24 * 0.5, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5)
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv2",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24 * 0.5,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=0,
        assert_final_soc=24 * 1,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv2",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "iboost_pv",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=50,
        battery_soc=50,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv2",
        my_predbat,
        1,
        1,
        assert_final_metric=0,
        assert_final_soc=50 - 24,
        battery_soc=50,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv3",
        my_predbat,
        1,
        1,
        assert_final_metric=0,
        assert_final_soc=50,
        battery_soc=50,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        iboost_solar_excess=True,
        assert_final_iboost=0,
        assert_iboost_running=False,
        assert_iboost_running_solar=False,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv4",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=100,
        battery_soc=90,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        iboost_solar_excess=True,
        assert_final_iboost=24 - 10,
        assert_iboost_running=False,
        assert_iboost_running_solar=False,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_pv5",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=100,
        battery_soc=100,
        battery_size=100,
        with_battery=True,
        iboost_enable=True,
        iboost_solar=True,
        iboost_solar_excess=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    if failed:
        return failed
    failed |= simple_scenario(
        "iboost_gas1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas=True,
        rate_gas=5.0,
        gas_scale=0.8,
        iboost_charging=False,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_gas2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas=True,
        rate_gas=10.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_gas3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas_export=True,
        rate_gas=4.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_gas4",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas_export=True,
        rate_gas=5.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate * 0.9,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_rate2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold_export=export_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold_export=export_rate - 1,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_charge1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 + 12),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=12,
        charge_period_divide=2,
        export_limit=1,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge4",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_rate_threshold=import_rate - 1,
        iboost_charging=True,
        assert_final_iboost=0,
        end_record=12 * 60,
    )
    failed |= simple_scenario(
        "iboost_discharge1",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10,
        assert_final_soc=0,
        battery_soc=10,
        with_battery=True,
        discharge=0,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_discharge2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        discharge=0,
        battery_size=100,
        iboost_enable=True,
        export_limit=1,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_discharge3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        discharge=0,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_prevent_discharge1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        iboost_prevent_discharge=False,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_prevent_discharge2",
        my_predbat,
        0,
        0,
        assert_final_metric=24 * import_rate,
        assert_final_soc=100,
        battery_soc=100,
        with_battery=True,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        iboost_prevent_discharge=True,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "keep_discharge1",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        battery_soc=10,
        with_battery=True,
        discharge=0,
        battery_size=10,
        keep=1.0,
        keep_weight=1.0,
        assert_final_iboost=0,
        assert_keep=import_rate * 14 + import_rate * 1 * 0.5,
    )

    # Alternating high/low rates
    reset_rates2(my_predbat, import_rate, export_rate)
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=120,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=120,
        iboost_max_energy=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120 * 1.5,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=120,
        iboost_max_energy=60,
        iboost_smart_min_length=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120 * 1.5 - 2 * import_rate * 5 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=110,
        iboost_max_energy=55,
        iboost_smart_min_length=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )

    failed |= simple_scenario(
        "iboost_rate_pv1",
        my_predbat,
        0,
        1.0,
        assert_final_metric=-export_rate * 12 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12,
        export_limit=1,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv2",
        my_predbat,
        0,
        1.0,
        assert_final_metric=-export_rate * 12 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 1,
        export_limit=2,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv3",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 12 * 2 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 2,
        export_limit=2,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )

    if failed:
        print("**** ERROR: Some Model tests failed ****")
    return failed


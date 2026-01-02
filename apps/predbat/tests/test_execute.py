# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import reset_inverter
from utils import calc_percent_limit


class ActiveTestInverter:
    def __init__(self, id, soc_kw, soc_max, now_utc):
        self.soc_target = -1
        self.id = id
        self.isCharging = False
        self.isExporting = False
        self.pause_charge = False
        self.pause_discharge = False
        self.idle_charge_start = -1
        self.idle_charge_end = -1
        self.idle_discharge_start = -1
        self.idle_discharge_end = -1
        self.force_export = False
        self.discharge_start_time_minutes = -1
        self.discharge_end_time_minutes = -1
        self.immediate_charge_soc_target = -1
        self.immediate_discharge_soc_target = -1
        self.immediate_charge_soc_freeze = False
        self.immediate_discharge_soc_freeze = False
        self.charge_start_time_minutes = -1
        self.charge_end_time_minutes = -1
        self.charge_rate = 1000
        self.discharge_rate = 1000
        self.charge_time_enable = False
        self.in_calibration = False
        self.inv_charge_discharge_with_rate = False
        self.inv_can_span_midnight = True
        self.inv_has_target_soc = True
        self.inv_has_charge_enable_time = True
        self.inv_has_timed_pause = True
        self.inv_has_discharge_enable_time = True
        self.soc_kw = soc_kw
        self.soc_max = soc_max
        self.soc_percent = calc_percent_limit(soc_kw, soc_max)
        self.battery_rate_max_charge = 1 / 60.0
        self.battery_rate_max_charge_scaled = 1 / 60.0
        self.battery_rate_max_discharge = 1 / 60.0
        self.battery_rate_max_discharge_scaled = 1 / 60.0
        self.reserve_max = 100.0
        self.now_utc = now_utc
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.count_register_writes = 0
        self.charge_window = []
        self.charge_limits = []
        self.export_window = []
        self.export_limits = []
        self.inv_support_discharge_freeze = True
        self.inv_support_charge_freeze = True
        self.inv_has_reserve_soc = True
        self.current_charge_limit = 0
        self.charge_rate_now = 1000
        self.discharge_rate_now = 1000
        self.battery_rate_min = 0
        self.inverter_limit = 1000
        self.export_limit = 1000
        self.pv_power = 0
        self.load_power = 0
        self.battery_power = 0
        self.grid_power = 0
        self.reserve_percent = 0
        self.reserve = 0
        self.reserve_last = -1
        self.reserve_current = 0
        self.reserve_percent = 0
        self.reserve_percent_current = 0
        self.battery_temperature = 20

    def update_status(self, minutes_now):
        pass

    def find_charge_curve(self, discharge=False):
        return None

    def get_current_charge_rate(self):
        return self.charge_rate

    def disable_charge_window(self):
        self.charge_time_enable = False

    def adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
        self.charge_start_time_minutes = (charge_start_time - self.midnight_utc).total_seconds() / 60
        self.charge_end_time_minutes = (charge_end_time - self.midnight_utc).total_seconds() / 60
        self.charge_time_enable = True
        # print("Charge start_time {} charge_end_time {}".format(self.charge_start_time_minutes, self.charge_end_time_minutes))

    def adjust_charge_immediate(self, target_soc, freeze=False):
        self.immediate_charge_soc_target = target_soc
        self.immediate_charge_soc_freeze = freeze

    def adjust_export_immediate(self, target_soc, freeze=False):
        self.immediate_discharge_soc_target = target_soc
        self.immediate_discharge_soc_freeze = freeze

    def adjust_force_export(self, force_export, new_start_time=None, new_end_time=None):
        self.force_export = force_export
        if new_start_time is not None:
            delta = new_start_time - self.midnight_utc
            self.discharge_start_time_minutes = delta.total_seconds() / 60
        if new_end_time is not None:
            delta = new_end_time - self.midnight_utc
            self.discharge_end_time_minutes = delta.total_seconds() / 60
        # print("Force export {} start_time {} end_time {}".format(self.force_export, self.discharge_start_time_minutes, self.discharge_end_time_minutes))

    def adjust_idle_time(self, charge_start=None, charge_end=None, discharge_start=None, discharge_end=None):
        self.idle_charge_start = charge_start
        self.idle_charge_end = charge_end
        self.idle_discharge_start = discharge_start
        self.idle_discharge_end = discharge_end

    def adjust_inverter_mode(self, force_export, changed_start_end=False):
        self.force_export = force_export
        self.changed_start_end = changed_start_end

    def adjust_reserve(self, reserve):
        self.reserve_last = reserve
        self.reserve_current = max(reserve, self.reserve)
        self.reserve_percent_current = calc_percent_limit(self.reserve_current, self.soc_max)

    def adjust_pause_mode(self, pause_charge=False, pause_discharge=False):
        self.pause_charge = pause_charge
        self.pause_discharge = pause_discharge

    def adjust_battery_target(self, soc, isCharging=False, isExporting=False):
        self.soc_target = soc
        self.current_charge_limit = soc
        self.isCharging = isCharging
        self.isExporting = isExporting

    def adjust_charge_rate(self, charge_rate):
        self.charge_rate = charge_rate
        self.charge_rate_now = charge_rate

    def adjust_discharge_rate(self, discharge_rate):
        self.discharge_rate = discharge_rate
        self.discharge_rate_now = discharge_rate


def run_execute_test(
    my_predbat,
    name,
    charge_window_best=[],
    charge_limit_best=[],
    export_window_best=[],
    export_limits_best=[],
    car_slot=[],
    soc_kw=0,
    soc_max=10,
    car_charging_from_battery=False,
    read_only=False,
    set_read_only_axle=False,
    set_soc_enable=True,
    set_charge_window=False,
    set_export_window=False,
    set_charge_low_power=False,
    set_export_low_power=False,
    charge_low_power_margin=10,
    assert_charge_time_enable=False,
    assert_force_export=False,
    assert_pause_charge=False,
    assert_pause_discharge=False,
    assert_status="Demand",
    assert_charge_start_time_minutes=-1,
    assert_charge_end_time_minutes=-1,
    assert_discharge_start_time_minutes=-1,
    assert_discharge_end_time_minutes=-1,
    inverter_charge_time_minutes_start=-1,
    inverter_charge_time_minutes_end=-1,
    assert_charge_rate=None,
    assert_discharge_rate=None,
    assert_reserve=0,
    assert_soc_target=100,
    assert_soc_target_array=None,
    in_calibration=False,
    set_discharge_during_charge=True,
    assert_immediate_soc_target=None,
    assert_immediate_soc_target_array=None,
    set_reserve_enable=True,
    has_timed_pause=True,
    has_target_soc=True,
    has_charge_enable_time=True,
    inverter_hybrid=False,
    battery_max_rate=1000,
    minutes_now=12 * 60,
    update_plan=False,
    reserve=1,
    soc_kw_array=None,
    reserve_max=100,
    car_soc=0,
    battery_temperature=20,
    assert_button_push=False,
):
    print("Run scenario {}".format(name))
    my_predbat.log("Run scenario {}".format(name))
    failed = False
    my_predbat.set_read_only = read_only
    my_predbat.set_read_only_axle = set_read_only_axle
    my_predbat.car_charging_slots = [car_slot]
    my_predbat.num_cars = 1
    my_predbat.inverter_hybrid = inverter_hybrid
    my_predbat.set_charge_low_power = set_charge_low_power
    my_predbat.set_export_low_power = set_export_low_power
    my_predbat.charge_low_power_margin = charge_low_power_margin
    my_predbat.minutes_now = minutes_now
    my_predbat.battery_temperature_charge_curve = {20: 1.0, 10: 0.5, 9: 0.5, 8: 0.5, 7: 0.5, 6: 0.3, 5: 0.3, 4: 0.3, 3: 0.262, 2: 0.1, 1: 0.1, 0: 0}

    charge_window_best = charge_window_best.copy()
    charge_limit_best = charge_limit_best.copy()
    export_window_best = export_window_best.copy()
    export_limits_best = export_limits_best.copy()

    if assert_immediate_soc_target is None:
        assert_immediate_soc_target = assert_soc_target
    if assert_charge_rate is None:
        assert_charge_rate = battery_max_rate
    if assert_discharge_rate is None:
        assert_discharge_rate = battery_max_rate

    total_inverters = len(my_predbat.inverters)
    my_predbat.battery_rate_max_charge = battery_max_rate / 1000.0 * total_inverters / 60.0
    my_predbat.battery_rate_max_discharge = battery_max_rate / 1000.0 * total_inverters / 60.0
    my_predbat.set_reserve_enable = set_reserve_enable
    for inverter in my_predbat.inverters:
        inverter.charge_start_time_minutes = inverter_charge_time_minutes_start
        inverter.charge_end_time_minutes = inverter_charge_time_minutes_end
        if soc_kw_array:
            inverter.soc_kw = soc_kw_array[inverter.id]
        else:
            inverter.soc_kw = soc_kw / total_inverters
        inverter.soc_max = soc_max / total_inverters
        inverter.soc_percent = calc_percent_limit(inverter.soc_kw, inverter.soc_max)
        inverter.in_calibration = in_calibration
        inverter.battery_rate_max_charge = my_predbat.battery_rate_max_charge / total_inverters
        inverter.battery_rate_max_discharge = my_predbat.battery_rate_max_discharge / total_inverters
        inverter.inv_has_timed_pause = has_timed_pause
        inverter.inv_has_target_soc = has_target_soc
        inverter.inv_has_charge_enable_time = has_charge_enable_time
        reserve_kwh = reserve / total_inverters
        reserve_percent = calc_percent_limit(reserve_kwh, inverter.soc_max)
        inverter.reserve_percent = reserve_percent
        inverter.reserve_current = reserve_percent
        inverter.reserve_percent_current = reserve_percent
        inverter.reserve = reserve_kwh
        inverter.reserve_max = reserve_max
        inverter.battery_temperature = battery_temperature

    my_predbat.fetch_inverter_data(create=False)

    if my_predbat.soc_kw != soc_kw:
        print("ERROR: Predbat level SOC should be {} got {}".format(soc_kw, my_predbat.soc_kw))
        failed = True
    if my_predbat.soc_percent != calc_percent_limit(my_predbat.soc_kw, my_predbat.soc_max):
        print("ERROR: Predbat level SOC percent should be {} got {}".format(calc_percent_limit(my_predbat.soc_kw, my_predbat.soc_max), my_predbat.soc_percent))
        failed = True
    if my_predbat.soc_max != soc_max:
        print("ERROR: Predbat level SOC max should be {} got {}".format(soc_max, my_predbat.soc_max))
        failed = True

    my_predbat.charge_window_best = charge_window_best
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = [calc_percent_limit(x, my_predbat.soc_max) for x in charge_limit_best]
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.set_charge_window = set_charge_window
    my_predbat.set_export_window = set_export_window
    my_predbat.set_soc_enable = set_soc_enable
    my_predbat.set_reserve_enable = set_reserve_enable
    my_predbat.set_reserve_hold = True
    my_predbat.set_export_freeze = True
    my_predbat.set_discharge_during_charge = set_discharge_during_charge
    my_predbat.car_charging_from_battery = car_charging_from_battery
    my_predbat.car_charging_soc[0] = car_soc

    # Shift on plan?
    if update_plan:
        my_predbat.plan_last_updated = my_predbat.now_utc
        my_predbat.args["threads"] = 0
        my_predbat.calculate_plan(recompute=False)

    status, status_extra = my_predbat.execute_plan()

    for inverter in my_predbat.inverters:
        if assert_status != status:
            print("ERROR: Inverter {} status should be {} got {}".format(inverter.id, assert_status, status))
            failed = True
        if assert_charge_time_enable != inverter.charge_time_enable:
            print("ERROR: Inverter {} Charge time enable should be {} got {}".format(inverter.id, assert_charge_time_enable, inverter.charge_time_enable))
            failed = True
        if assert_force_export != inverter.force_export:
            print("ERROR: Inverter {} Force discharge should be {} got {}".format(inverter.id, assert_force_export, inverter.force_export))
            failed = True
        if assert_pause_charge != inverter.pause_charge:
            print("ERROR: Inverter {} Pause charge should be {} got {}".format(inverter.id, assert_pause_charge, inverter.pause_charge))
            failed = True
        if assert_pause_discharge != inverter.pause_discharge:
            print("ERROR: Inverter {} Pause discharge should be {} got {}".format(inverter.id, assert_pause_discharge, inverter.pause_discharge))
            failed = True
        if assert_charge_time_enable and assert_charge_start_time_minutes != inverter.charge_start_time_minutes:
            print("ERROR: Inverter {} Charge start time should be {} got {}".format(inverter.id, assert_charge_start_time_minutes, inverter.charge_start_time_minutes))
            failed = True
        if assert_charge_time_enable and assert_charge_end_time_minutes != inverter.charge_end_time_minutes:
            print("ERROR: Inverter {} Charge end time should be {} got {}".format(inverter.id, assert_charge_end_time_minutes, inverter.charge_end_time_minutes))
            failed = True
        if assert_force_export and assert_discharge_start_time_minutes != inverter.discharge_start_time_minutes:
            print("ERROR: Inverter {} Discharge start time should be {} got {}".format(inverter.id, assert_discharge_start_time_minutes, inverter.discharge_start_time_minutes))
            failed = True
        if assert_force_export and assert_discharge_end_time_minutes != inverter.discharge_end_time_minutes:
            print("ERROR: Inverter {} Discharge end time should be {} got {}".format(inverter.id, assert_discharge_end_time_minutes, inverter.discharge_end_time_minutes))
            failed = True
        if assert_charge_rate != inverter.charge_rate:
            print("ERROR: Inverter {} Charge rate should be {} got {}".format(inverter.id, assert_charge_rate, inverter.charge_rate))
            failed = True
        if assert_discharge_rate != inverter.discharge_rate:
            print("ERROR: Inverter {} Discharge rate should be {} got {}".format(inverter.id, assert_discharge_rate, inverter.discharge_rate))
            failed = True
        if assert_reserve != inverter.reserve_last:
            print("ERROR: Inverter {} Reserve should be {} got {}".format(inverter.id, assert_reserve, inverter.reserve_last))
            failed = True
        if assert_soc_target_array:
            assert_soc_target = assert_soc_target_array[inverter.id]
        if assert_soc_target != inverter.soc_target:
            print("ERROR: Inverter {} SOC target should be {} got {}".format(inverter.id, assert_soc_target, inverter.soc_target))
            failed = True

        if assert_immediate_soc_target_array:
            assert_immediate_soc_target = assert_immediate_soc_target_array[inverter.id]

        assert_soc_target_force = (
            assert_immediate_soc_target
            if assert_status in ["Charging", "Charging, Hold for car", "Hold charging", "Freeze charging", "Hold charging, Hold for iBoost", "Hold charging, Hold for car", "Freeze charging, Hold for iBoost", "Hold for car", "Hold for iBoost"]
            else 0
        )
        if not set_charge_window:
            assert_soc_target_force = -1
        if inverter.immediate_charge_soc_target != assert_soc_target_force:
            print("ERROR: Inverter {} Immediate charge SOC target should be {} got {}".format(inverter.id, assert_soc_target_force, inverter.immediate_charge_soc_target))
            failed = True
        if assert_status in ["Freeze charging"] and inverter.immediate_charge_soc_freeze != True:
            print("ERROR: Inverter {} Immediate charge SOC freeze should be True got {}".format(inverter.id, inverter.immediate_charge_soc_freeze))
            failed = True
        assert_soc_target_force_dis = assert_immediate_soc_target if assert_status in ["Exporting", "Freeze exporting"] else 100
        if not set_export_window:
            assert_soc_target_force_dis = -1
        if inverter.immediate_discharge_soc_target != assert_soc_target_force_dis:
            print("ERROR: Inverter {} Immediate export SOC target should be {} got {}".format(inverter.id, assert_soc_target_force_dis, inverter.immediate_discharge_soc_target))
            failed = True
        if assert_status in ["Freeze exporting"] and inverter.immediate_discharge_soc_freeze != True:
            print("ERROR: Inverter {} Immediate export SOC freeze should be True got {}".format(inverter.id, inverter.immediate_discharge_soc_freeze))
            failed = True

    my_predbat.minutes_now = 12 * 60
    return failed


def run_execute_tests(my_predbat):
    print("**** Running execute tests ****\n")
    reset_inverter(my_predbat)

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best_slot = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "kwh": 7.5}]
    charge_window_best_no_slot = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "kwh": 0}]
    charge_window_best_soon = [{"start": my_predbat.minutes_now + 5, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best2 = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best3 = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now, "average": 1}, {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best4 = [{"start": my_predbat.minutes_now + 24 * 60, "end": my_predbat.minutes_now + 60 + 24 * 60, "average": 1}]
    charge_window_best5 = [{"start": my_predbat.minutes_now - 24 * 60, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best6 = [{"start": my_predbat.minutes_now + 8 * 60, "end": my_predbat.minutes_now + 60 + 8 * 60, "average": 1}]
    charge_window_best7 = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    charge_window_best7b = [{"start": 24 * 60 - 5, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    charge_window_best7c = [{"start": 0, "end": 11 * 60, "average": 1}]
    charge_window_best7d = [{"start": 22 * 60, "end": 23 * 60, "average": 1}]
    charge_window_best8 = [{"start": 0, "end": my_predbat.minutes_now + 12 * 60, "average": 1}]
    charge_window_best9 = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 1}]
    charge_window_best_short = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 15, "average": 1}]
    charge_limit_best0 = [10]
    charge_limit_best = [10, 10]
    charge_limit_best2 = [5]
    charge_limit_best_frz = [1]
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    export_window_best2 = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best3 = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best4 = [{"start": my_predbat.minutes_now + 15, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best5 = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    export_window_best6 = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best7 = [{"start": 0, "end": my_predbat.minutes_now + 12 * 60, "average": 1}]
    export_limits_best = [0]
    export_limits_best2 = [50]
    export_limits_best3 = [50.5]
    export_limits_best_frz = [99]

    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters
    my_predbat.args["num_inverters"] = 2

    failed = False
    failed |= run_execute_test(my_predbat, "off", assert_reserve=-1)
    my_predbat.holiday_days_left = 2
    failed |= run_execute_test(my_predbat, "off_holiday", assert_status="Demand (Holiday)", assert_reserve=-1)
    my_predbat.holiday_days_left = 0

    failed |= run_execute_test(my_predbat, "no_charge", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best, assert_reserve=-1)
    failed |= run_execute_test(my_predbat, "no_charge2", set_charge_window=True, set_export_window=True, set_discharge_during_charge=False)
    failed |= run_execute_test(my_predbat, "no_charge3", set_charge_window=True, set_export_window=True, set_discharge_during_charge=False, has_timed_pause=False)
    failed |= run_execute_test(my_predbat, "no_charge_future", set_charge_window=True, set_export_window=True, charge_window_best=charge_window_best4, charge_limit_best=charge_limit_best)
    failed |= run_execute_test(my_predbat, "no_charge_future_hybrid", set_charge_window=True, set_export_window=True, charge_window_best=charge_window_best4, charge_limit_best=charge_limit_best, inverter_hybrid=True)
    failed |= run_execute_test(
        my_predbat,
        "no_charge_future_no_soc",
        set_charge_window=True,
        set_export_window=True,
        charge_window_best=charge_window_best4,
        charge_limit_best=charge_limit_best,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_charge_future_no_enable_time",
        set_charge_window=True,
        set_export_window=True,
        charge_window_best=charge_window_best4,
        charge_limit_best=charge_limit_best,
        has_target_soc=True,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_charge_future_no_enable_time_hybrid",
        set_charge_window=True,
        set_export_window=True,
        charge_window_best=charge_window_best4,
        charge_limit_best=charge_limit_best,
        has_target_soc=True,
        has_charge_enable_time=False,
        inverter_hybrid=True,
        assert_soc_target=0,
    )
    if failed:
        return failed

    # Iboost hold tests
    my_predbat.iboost_enable = True
    my_predbat.iboost_prevent_discharge = True
    my_predbat.iboost_running_full = True
    failed |= run_execute_test(my_predbat, "no_charge_iboost", set_charge_window=True, set_export_window=True, assert_pause_discharge=True, assert_status="Hold for iBoost", soc_kw=1, assert_immediate_soc_target=10)

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_iboost",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=True,
        assert_status="Freeze charging, Hold for iBoost",
        assert_discharge_rate=1000,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_iboost2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Freeze charging, Hold for iBoost",
        assert_discharge_rate=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        assert_reserve=100,
        has_timed_pause=False,
    )
    if failed:
        return failed

    my_predbat.iboost_prevent_discharge = False
    failed |= run_execute_test(my_predbat, "no_charge_iboost2", set_charge_window=True, set_export_window=True)
    my_predbat.iboost_running_full = False
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=9.5,
        soc_kw_array=[5, 4.5],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=9.5,
        soc_kw_array=[4.5, 5],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=5,
        soc_kw_array=[2.0, 3.0],
        assert_soc_target_array=[40, 60],
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance4",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=6,
        soc_kw_array=[3.0, 3.0],
        assert_soc_target_array=[50, 50],
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance5",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=7,
        soc_kw_array=[3.0, 4.0],
        assert_soc_target_array=[40, 60],
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 10 minute margin = 50 minutes to add 0.5kWh to each battery (x2 inverters)
    # (60 / 50) * 500 = 600
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=600,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 10 minute margin = 50 minutes to add 0.4kWh to each battery (x2 inverters)
    # (60 / 50) * 400 = 480
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.2,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=600,  # Within 10%
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 30 minute margin = 30 minutes to add 0.4kWh to each battery (x2 inverters)
    # (60 / 20) * 400 = 1200
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2c",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.2,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        charge_low_power_margin=40,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=1200,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 10 minute margin = 50 minutes to add 0.45kWh to each battery (x2 inverters)
    # (60 / 50) * 450 = 540
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2d",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.1,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=600,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    my_predbat.battery_charge_power_curve = {
        100: 0.50,
        99: 0.50,
        98: 0.50,
        97: 0.50,
        96: 0.50,
        95: 0.50,
        94: 1.00,
        93: 1.00,
        92: 1.00,
        91: 1.00,
        90: 1.00,
        89: 1.00,
        88: 1.00,
        87: 1.00,
        86: 1.00,
        85: 1.00,
    }

    # 60 minutes - 10 minute margin = 50 minutes to add 0.75kWh to each battery (x2 inverters)
    # (60 / 50) * 750 = 900
    # But with the low power curve it will go at half rate from 95%

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power3a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=8.0,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=1300,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_short",
        charge_window_best=charge_window_best_short,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.835,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 15,
        assert_charge_rate=1300,  # Keep current rate as it is over the max rate we will achieve anyhow
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # No impact at 10 degrees
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_temp1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=8.0,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=1300,
        battery_max_rate=2000,
        battery_temperature=10,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_temp2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=8.0,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=1300,
        battery_max_rate=2000,
        battery_temperature=3,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_temp3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=8.0,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=2000,
        battery_max_rate=2000,
        battery_temperature=1,
    )
    if failed:
        return failed

    # Reset curve
    my_predbat.battery_charge_power_curve = {}

    failed |= run_execute_test(
        my_predbat,
        "charge_long",
        charge_window_best=charge_window_best8,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 12 * 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_no_soc",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_no_enable_time",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_no_enable_time_no_soc",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        set_discharge_during_charge=False,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_soc_target=50,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge2b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_reserve=0,
        assert_immediate_soc_target=50,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge2c",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
        assert_pause_discharge=False,
        assert_reserve=51,
        assert_immediate_soc_target=50,
        has_timed_pause=False,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge2d",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_soc_target=50,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge2e",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        soc_kw=0,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
        assert_pause_discharge=False,
        assert_reserve=0,
        assert_immediate_soc_target=50,
        assert_charge_time_enable=True,
        assert_soc_target=50,
        has_timed_pause=False,
        set_discharge_during_charge=False,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        soc_kw=4,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        set_discharge_during_charge=False,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_soc_target=50,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge4_no_reserve",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_immediate_soc_target=50,
        set_reserve_enable=False,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge4_no_reserve_no_pause",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
        assert_pause_discharge=False,
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        set_reserve_enable=False,
        has_timed_pause=False,
        assert_charge_time_enable=True,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target=50,
        reserve_max=50,
        has_timed_pause=False,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target_array=[60, 40],
        assert_immediate_soc_target=50,
        reserve_max=90,
        has_timed_pause=False,
        soc_kw_array=[5, 4],
    )

    # Charge/discharge with rate
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = True
    failed |= run_execute_test(
        my_predbat,
        "charge_with_rate",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = False
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_midnight1",
        charge_window_best=charge_window_best7,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 23 * 60,
    )
    # Can span midnight false test
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = False
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_midnight2a",
        charge_window_best=charge_window_best7,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=24 * 60 - 1,
        inverter_charge_time_minutes_start=30,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_midnight2b",
        charge_window_best=charge_window_best7b,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=24 * 60 - 30,
        assert_charge_end_time_minutes=24 * 60 - 1,
        inverter_charge_time_minutes_start=30,
        minutes_now=24 * 60 - 5,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_midnight2c",
        charge_window_best=charge_window_best7c,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=0,
        assert_charge_end_time_minutes=11 * 60,
        inverter_charge_time_minutes_start=12 * 60,
        minutes_now=0,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_midnight2d",
        charge_window_best=charge_window_best7d,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=30 + 24 * 60,
        assert_charge_end_time_minutes=-1,
        inverter_charge_time_minutes_start=30 + 24 * 60,
    )

    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = True
    if failed:
        return failed

    my_predbat.debug_enable = True
    failed |= run_execute_test(
        my_predbat,
        "charge_shift",
        charge_window_best=charge_window_best3,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_shift2",
        charge_window_best=charge_window_best5,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_shift3",
        charge_window_best=charge_window_best5,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        inverter_charge_time_minutes_start=-24 * 60,
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    my_predbat.debug_enable = False

    # Reset inverters
    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters

    failed |= run_execute_test(my_predbat, "calibration", in_calibration=True, assert_status="Calibration", assert_charge_time_enable=False, assert_reserve=0, assert_soc_target=100)
    failed |= run_execute_test(my_predbat, "no_charge3", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(my_predbat, "charge_read_only", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best, set_charge_window=True, set_export_window=True, read_only=True, assert_status="Read-Only", reserve=0)
    failed |= run_execute_test(
        my_predbat, "charge_axle_read_only", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best, set_charge_window=True, set_export_window=True, read_only=True, set_read_only_axle=True, assert_status="Read-Only (Axle)", reserve=0
    )

    failed |= run_execute_test(
        my_predbat,
        "charge3",
        inverter_charge_time_minutes_start=1,
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge4",
        inverter_charge_time_minutes_start=24 * 60 - 1,
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge5",
        inverter_charge_time_minutes_start=24 * 60 - 1,
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        set_reserve_enable=False,
        has_timed_pause=False,
        reserve=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_hold",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Hold charging",
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_hold2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=True,
        assert_status="Hold charging",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_hold2b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_discharge_rate=0,
        assert_reserve=51,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
        has_timed_pause=False,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Freeze charging",
        assert_discharge_rate=0,
        assert_reserve=100,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1c",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=1,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=10,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1d_too_low",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=0,
        assert_pause_discharge=False,
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target=10,
        assert_immediate_soc_target=10,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=2,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_reserve=0,
        assert_soc_target_array=[10, 40],
        assert_immediate_soc_target_array=[10, 40],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[0, 2],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=2,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_reserve=0,
        assert_soc_target_array=[40, 10],
        assert_immediate_soc_target_array=[40, 10],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[2, 0],
    )
    if failed:
        return failed

    # Target SOC can not be lower than reserve (which is 1) so it will charge to 1 not freeze
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=0.75,
        assert_pause_discharge=False,
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target_array=[10, 10],
        assert_immediate_soc_target=10,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[0.5, 0.25],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb4",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=1,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_reserve=0,
        assert_soc_target_array=[10, 15],
        assert_immediate_soc_target_array=[10, 15],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[0.25, 0.75],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb5",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=False,
        soc_kw=1,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_reserve=0,
        assert_soc_target_array=[100, 100],
        assert_immediate_soc_target=10,
        soc_kw_array=[0.5, 0.5],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_soon",
        charge_window_best=charge_window_best_soon,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Demand",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_soon2",
        charge_window_best=charge_window_best_soon,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        has_target_soc=False,
        assert_pause_discharge=False,
        assert_status="Demand",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=0,
        assert_immediate_soc_target=100,
    )

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_no_pause",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Freeze charging",
        assert_discharge_rate=0,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        set_reserve_enable=False,
        has_timed_pause=False,
        assert_charge_time_enable=True,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze2_no_pause",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        reserve=1,
        assert_pause_discharge=False,
        assert_status="Freeze charging",
        assert_discharge_rate=0,
        assert_reserve=0,
        assert_soc_target=50,
        assert_immediate_soc_target=50,
        set_reserve_enable=False,
        has_timed_pause=False,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(my_predbat, "charge_freeze3", charge_window_best=charge_window_best2, charge_limit_best=charge_limit_best_frz, assert_charge_time_enable=False, set_charge_window=True, set_export_window=True, soc_kw=5)

    failed |= run_execute_test(my_predbat, "no_charge4", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(
        my_predbat,
        "charge_later",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_hybrid",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        inverter_hybrid=True,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_soc",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "freeze_later_no_soc",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_enable_time",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_enable_time_hybrid",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
        inverter_hybrid=True,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_enable_time_no_soc",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(my_predbat, "charge_later2", charge_window_best=charge_window_best6, charge_limit_best=charge_limit_best, assert_charge_time_enable=False, set_charge_window=True, set_export_window=True, assert_status="Demand")
    failed |= run_execute_test(my_predbat, "no_charge5", set_charge_window=True, set_export_window=True, assert_immediate_soc_target=0)
    # Reset inverters
    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters

    failed |= run_execute_test(my_predbat, "no_discharge", export_window_best=export_window_best, export_limits_best=export_limits_best, assert_reserve=-1)
    failed |= run_execute_test(my_predbat, "no_discharge2", export_window_best=export_window_best, export_limits_best=export_limits_best, set_charge_window=True, set_export_window=True, soc_kw=0, assert_status="Hold exporting")
    failed |= run_execute_test(
        my_predbat,
        "discharge_upcoming1",
        export_window_best=export_window_best3,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 90 + 1,
    )
    failed |= run_execute_test(my_predbat, "no_discharge3b", export_window_best=export_window_best6, export_limits_best=export_limits_best, set_charge_window=True, set_export_window=True, soc_kw=0)
    failed |= run_execute_test(
        my_predbat,
        "discharge_upcoming2",
        export_window_best=export_window_best4,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now + 15,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 90 + 1,
    )
    # Freeze should not set the timer as it doesn't actually export
    failed |= run_execute_test(
        my_predbat,
        "discharge_upcoming3",
        export_window_best=export_window_best3,
        export_limits_best=export_limits_best_frz,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_force_export=False,
        assert_discharge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 90 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_status="Hold exporting, Hold for car",
        car_slot=charge_window_best_slot,
        assert_pause_discharge=True,
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_status="Hold exporting, Hold for car",
        car_slot=charge_window_best_slot,
        assert_pause_discharge=False,
        assert_discharge_rate=0,
        assert_reserve=1,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_car_full_bat",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Exporting",
        car_slot=charge_window_best,
        car_charging_from_battery=True,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car_demand1",
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Hold for car",
        assert_pause_discharge=True,
        car_slot=charge_window_best_slot,
        assert_immediate_soc_target=100,
        car_charging_from_battery=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car_demand2",
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Demand",
        assert_pause_discharge=False,
        car_slot=charge_window_best_slot,
        car_charging_from_battery=False,
        car_soc=100,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car_demand1",
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Demand",
        assert_pause_discharge=False,
        car_slot=charge_window_best_no_slot,
        assert_immediate_soc_target=100,
        car_charging_from_battery=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_long",
        export_window_best=export_window_best7,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 12 * 60 + 1,
        soc_kw=10,
        assert_status="Exporting",
        assert_force_export=True,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_charge1",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        charge_limit_best=charge_limit_best0,
        charge_window_best=charge_window_best9,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        minutes_now=775,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_charge2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        charge_limit_best=charge_limit_best0,
        charge_window_best=charge_window_best9,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Charging",
        assert_immediate_soc_target=100,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 90,
        assert_charge_time_enable=True,
        minutes_now=780,
        update_plan=True,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge2_no_reserve",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best2,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        set_reserve_enable=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge2_no_reserve_no_pause",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best2,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        set_reserve_enable=False,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best2,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge3",
        export_window_best=export_window_best2,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 90 + 1,
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge4",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best3,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge5",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best3,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_discharge_rate=500,
        set_export_low_power=True,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_midnight1",
        export_window_best=export_window_best5,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 23 * 60 + 1,
    )
    if failed:
        return failed

    # Can span midnight false test
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = False

    failed |= run_execute_test(
        my_predbat,
        "discharge_midnight2",
        export_window_best=export_window_best5,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=24 * 60 - 1,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = True

    # Charge/discharge with rate
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = True
    failed |= run_execute_test(
        my_predbat,
        "discharge_with_rate",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_charge_rate=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_freeze",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best_frz,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Freeze exporting",
        assert_pause_charge=True,
        assert_charge_rate=0,
        assert_immediate_soc_target=90,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = False
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "discharge_freeze2",
        export_window_best=export_window_best2,
        export_limits_best=export_limits_best_frz,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Freeze exporting",
        assert_pause_charge=True,
        assert_charge_rate=1000,
        assert_immediate_soc_target=90,
    )
    failed |= run_execute_test(
        my_predbat,
        "discharge_freeze2b",
        export_window_best=export_window_best2,
        export_limits_best=export_limits_best_frz,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Freeze exporting",
        assert_pause_charge=False,
        assert_charge_rate=0,
        assert_immediate_soc_target=90,
        has_timed_pause=False,
    )
    failed |= run_execute_test(my_predbat, "no_charge5", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(
        my_predbat, "car", car_slot=charge_window_best_slot, set_charge_window=True, set_export_window=True, assert_status="Hold for car", assert_pause_discharge=True, assert_discharge_rate=1000, soc_kw=1, assert_immediate_soc_target=10
    )
    failed |= run_execute_test(
        my_predbat,
        "car2",
        car_slot=charge_window_best_slot,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold for car",
        assert_pause_discharge=False,
        assert_discharge_rate=0,
        has_timed_pause=False,
        soc_kw=1,
        assert_immediate_soc_target=10,
        assert_reserve=11,
    )
    failed |= run_execute_test(
        my_predbat,
        "car_charge",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        soc_kw=0,
        car_slot=charge_window_best_slot,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging, Hold for car",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_immediate_soc_target=100,
        assert_pause_discharge=True,
    )
    failed |= run_execute_test(
        my_predbat,
        "car_charge2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        soc_kw=10,
        car_slot=charge_window_best_slot,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging, Hold for car",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        assert_pause_discharge=True,
    )
    failed |= run_execute_test(
        my_predbat,
        "car_discharge",
        car_slot=charge_window_best_slot,
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )

    # Reset test
    my_predbat.reset_inverter()
    failed |= run_execute_test(
        my_predbat,
        "demand_after_reset",
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_reserve=0,
        assert_immediate_soc_target=0,
        assert_soc_target=100,
    )

    # Test for bug fix: charge_limit_best[0] comparison and unit conversion
    # Bug 1: self.charge_limit_best != self.reserve was comparing list to scalar (always True)
    # Bug 2: self.soc_kw was used directly instead of calc_percent_limit(self.soc_kw, self.soc_max)
    # This test verifies freeze charge works correctly with proper percent conversion
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_target_soc_percent_conversion",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,  # [1] = reserve, triggers freeze charge
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,  # 5 kWh current level
        soc_max=10,  # 10 kWh max -> 50% SoC
        assert_status="Freeze charging",
        assert_pause_discharge=True,
        assert_soc_target=100,  # Freeze charge sets target to 100%
        assert_immediate_soc_target=50,  # Current SoC% should be 50%, not 5%
    )
    if failed:
        return failed

    # Test for GitHub issue #3107: Floating point rounding causes freeze charge mismatch
    # When charge_limit_best is rounded to 0.5 kWh but reserve is 0.51 kWh (5% of 10.149 kWh)
    # they should both equal 5% and trigger freeze charge, not hold charge
    charge_limit_best_rounded = [0.5]  # 0.5 kWh is the charge limit rounded down by dp2() from ~0.507 (5% of 10.149 kWh)
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_rounding_issue_3107",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_rounded,  # 0.5 kWh rounds to 5%
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10.048,  # Current SoC at 99%
        soc_max=10.149,  # Real-world battery size
        reserve=0.51,  # 5% of 10.149 = 0.50745, rounds to 0.51 with dp3()
        assert_status="Freeze charging",  # Should be freeze, not "Hold charging"
        assert_pause_discharge=True,
        assert_soc_target=100,
        assert_immediate_soc_target=99,  # Current SoC% = 10.048/10.149 = ~99%
    )
    if failed:
        return failed

    return failed

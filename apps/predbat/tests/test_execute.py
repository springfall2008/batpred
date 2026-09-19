# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import reset_inverter
from const import EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE, MINUTE_WATT
from utils import pack_export_limit
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
        self.inv_has_ge_eco_toggle = False
        self.inv_has_ge_inverter_mode = False
        self.soc_kw = soc_kw
        self.soc_max = soc_max
        self.soc_percent = calc_percent_limit(soc_kw, soc_max)
        self.battery_rate_max_charge = 1 / 60.0
        self.battery_rate_max_charge_dc = 1 / 60.0
        self.battery_rate_max_discharge = 1 / 60.0
        self.battery_rate_max_export = 1 / 60.0
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
        self.inv_support_feedin_first = False
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

    def find_battery_size(self):
        return self.soc_max * 0.90

    def update_status(self, minutes_now, quiet=False):
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

    def adjust_charge_rate(self, charge_rate, notify=True):
        self.charge_rate = charge_rate
        self.charge_rate_now = charge_rate
        # Mirrors the real signature so tests can assert that balancing writes stay silent
        self.charge_rate_notify = notify

    def adjust_discharge_rate(self, discharge_rate, notify=True):
        self.discharge_rate = discharge_rate
        self.discharge_rate_now = discharge_rate
        self.discharge_rate_notify = notify


def run_execute_test(
    my_predbat,
    name,
    charge_window_best=None,
    charge_limit_best=None,
    export_window_best=None,
    export_limits_best=None,
    car_slot=None,
    soc_kw=0,
    soc_max=10,
    soc_max_array=None,
    car_charging_from_battery=False,
    car_energy_reported_load=True,
    read_only=False,
    set_read_only_axle=False,
    set_soc_enable=True,
    set_charge_window=False,
    set_export_window=False,
    set_charge_low_power=False,
    set_export_low_power=False,
    charge_low_power_margin=10,
    assert_charge_time_enable=False,
    assert_charge_time_enable_array=None,
    assert_force_export=False,
    assert_pause_charge=False,
    assert_pause_charge_array=None,
    assert_pause_discharge=False,
    assert_pause_discharge_array=None,
    assert_status="Demand",
    assert_status_extra=None,
    assert_charge_start_time_minutes=-1,
    assert_charge_end_time_minutes=-1,
    assert_charge_start_time_minutes_array=None,
    assert_charge_end_time_minutes_array=None,
    assert_discharge_start_time_minutes=-1,
    assert_discharge_end_time_minutes=-1,
    inverter_charge_time_minutes_start=-1,
    inverter_charge_time_minutes_end=-1,
    assert_charge_rate=None,
    assert_discharge_rate=None,
    assert_charge_rate_array=None,
    assert_discharge_rate_array=None,
    assert_reserve=0,
    assert_soc_target=100,
    assert_soc_target_array=None,
    assert_immediate_charge_soc_target_array=None,
    assert_immediate_discharge_soc_target_array=None,
    assert_is_charging=None,
    in_calibration=False,
    in_calibration_array=None,
    set_discharge_during_charge=True,
    assert_immediate_soc_target=None,
    assert_immediate_soc_target_array=None,
    set_reserve_enable=True,
    has_timed_pause=True,
    has_timed_pause_array=None,
    has_target_soc=True,
    has_charge_enable_time=True,
    has_ge_eco_toggle=False,
    inverter_hybrid=False,
    battery_max_rate=1000,
    battery_max_export_rate=None,
    battery_max_rate_array=None,
    battery_max_export_rate_array=None,
    minutes_now=12 * 60,
    update_plan=False,
    reserve=1,
    soc_kw_array=None,
    reserve_max=100,
    reserve_max_array=None,
    assert_reserve_array=None,
    car_soc=0,
    battery_temperature=20,
    assert_immediate_charge_soc_freeze_array=None,
    pv_forecast=0.0,
    set_charge_freeze_only=False,
):
    if assert_immediate_charge_soc_freeze_array is None:
        assert_immediate_charge_soc_freeze_array = []
    if car_slot is None:
        car_slot = []
    if export_limits_best is None:
        export_limits_best = []
    if export_window_best is None:
        export_window_best = []
    if charge_limit_best is None:
        charge_limit_best = []
    if charge_window_best is None:
        charge_window_best = []
    print("> Run scenario {}".format(name))
    my_predbat.log("> Run scenario {}".format(name))
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
    # Flat PV forecast of pv_forecast kW, reset every scenario so a solar run does not leak into the next one
    my_predbat.pv_forecast_minute = {minute: pv_forecast / 60.0 for minute in range(minutes_now, minutes_now + my_predbat.forecast_minutes)}

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
    if battery_max_export_rate is None:
        battery_max_export_rate = battery_max_rate
    # Mirror the scalar fallback for the arrays: a per-inverter rate ceiling with no explicit
    # export array means the export ceiling follows it, exactly as battery_max_export_rate
    # follows battery_max_rate above.
    if battery_max_export_rate_array is None and battery_max_rate_array is not None:
        battery_max_export_rate_array = list(battery_max_rate_array)

    total_inverters = len(my_predbat.inverters)
    # Fleet totals are summed from the per-inverter values rather than assumed uniform, so a
    # heterogeneous fleet still satisfies the Predbat-level sanity checks below.
    if battery_max_rate_array:
        fleet_rate_w = sum(battery_max_rate_array)
    else:
        fleet_rate_w = battery_max_rate * total_inverters
    if battery_max_export_rate_array:
        fleet_export_w = sum(battery_max_export_rate_array)
    else:
        fleet_export_w = battery_max_export_rate * total_inverters
    my_predbat.battery_rate_max_charge = fleet_rate_w / 1000.0 / 60.0
    my_predbat.battery_rate_max_charge_dc = fleet_rate_w / 1000.0 / 60.0
    my_predbat.battery_rate_max_discharge = fleet_rate_w / 1000.0 / 60.0
    my_predbat.battery_rate_max_export = fleet_export_w / 1000.0 / 60.0
    my_predbat.set_reserve_enable = set_reserve_enable
    for inverter in my_predbat.inverters:
        inverter.charge_start_time_minutes = inverter_charge_time_minutes_start
        inverter.charge_end_time_minutes = inverter_charge_time_minutes_end
        # Reset the immediate-control sentinels so a scenario that doesn't call
        # adjust_charge_immediate()/adjust_export_immediate() this cycle reads as "untouched" (-1)
        # rather than inheriting whatever the previous scenario in this run happened to leave behind.
        inverter.immediate_charge_soc_target = -1
        inverter.immediate_discharge_soc_target = -1
        if soc_kw_array:
            inverter.soc_kw = soc_kw_array[inverter.id]
        else:
            inverter.soc_kw = soc_kw / total_inverters
        inverter.soc_max = soc_max_array[inverter.id] if soc_max_array else soc_max / total_inverters
        inverter.soc_percent = calc_percent_limit(inverter.soc_kw, inverter.soc_max)
        inverter.in_calibration = in_calibration_array[inverter.id] if in_calibration_array else in_calibration
        inv_rate_w = battery_max_rate_array[inverter.id] if battery_max_rate_array else battery_max_rate
        inv_export_w = battery_max_export_rate_array[inverter.id] if battery_max_export_rate_array else battery_max_export_rate
        inverter.battery_rate_max_charge = inv_rate_w / 1000.0 / 60.0
        inverter.battery_rate_max_charge_dc = inv_rate_w / 1000.0 / 60.0
        inverter.battery_rate_max_discharge = inv_rate_w / 1000.0 / 60.0
        inverter.battery_rate_max_export = inv_export_w / 1000.0 / 60.0
        inverter.inv_has_timed_pause = has_timed_pause_array[inverter.id] if has_timed_pause_array else has_timed_pause
        inverter.inv_has_target_soc = has_target_soc
        inverter.inv_has_charge_enable_time = has_charge_enable_time
        inverter.inv_has_ge_eco_toggle = has_ge_eco_toggle
        reserve_kwh = reserve / total_inverters
        reserve_percent = calc_percent_limit(reserve_kwh, inverter.soc_max)
        inverter.reserve_percent = reserve_percent
        inverter.reserve_current = reserve_percent
        inverter.reserve_percent_current = reserve_percent
        inverter.reserve = reserve_kwh
        inverter.reserve_max = reserve_max_array[inverter.id] if reserve_max_array else reserve_max
        inverter.battery_temperature = battery_temperature

    # fetch_inverter_data() only ever narrows the freeze capability flags (it sets them False for an
    # inverter that doesn't support freeze, and never widens them back), matching production where
    # fetch_config_options() re-derives them from raw config every cycle before fetch_inverter_data()
    # runs. This helper calls fetch_inverter_data() directly and is reused across many scenarios in
    # one process, so re-derive them here too - otherwise a narrowing from an earlier scenario (e.g.
    # one using an inverter with no freeze support) leaks forward into every later scenario, making
    # results depend on test ordering.
    my_predbat.set_charge_freeze = my_predbat.get_arg("set_charge_freeze")
    my_predbat.set_export_freeze = my_predbat.get_arg("set_export_freeze")
    my_predbat.set_export_freeze_only = my_predbat.get_arg("set_export_freeze_only")
    my_predbat.set_charge_freeze_only = set_charge_freeze_only

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
    my_predbat.car_energy_reported_load = car_energy_reported_load
    my_predbat.car_charging_soc[0] = car_soc

    # Shift on plan?
    if update_plan:
        my_predbat.plan_last_updated = my_predbat.now_utc
        my_predbat.args["threads"] = 0
        my_predbat.calculate_plan(recompute=False)

    status, status_extra = my_predbat.execute_plan()

    if assert_status_extra is not None and assert_status_extra != status_extra:
        print("ERROR: status_extra should be {!r} got {!r}".format(assert_status_extra, status_extra))
        failed = True

    for inverter in my_predbat.inverters:
        if assert_status != status:
            print("ERROR: Inverter {} status should be {} got {}".format(inverter.id, assert_status, status))
            failed = True
        inv_assert_charge_time_enable = assert_charge_time_enable_array[inverter.id] if assert_charge_time_enable_array else assert_charge_time_enable
        if inv_assert_charge_time_enable != inverter.charge_time_enable:
            print("ERROR: Inverter {} Charge time enable should be {} got {}".format(inverter.id, inv_assert_charge_time_enable, inverter.charge_time_enable))
            failed = True
        if assert_force_export != inverter.force_export:
            print("ERROR: Inverter {} Force discharge should be {} got {}".format(inverter.id, assert_force_export, inverter.force_export))
            failed = True
        inv_assert_pause_charge = assert_pause_charge_array[inverter.id] if assert_pause_charge_array else assert_pause_charge
        if inv_assert_pause_charge != inverter.pause_charge:
            print("ERROR: Inverter {} Pause charge should be {} got {}".format(inverter.id, inv_assert_pause_charge, inverter.pause_charge))
            failed = True
        inv_assert_pause_discharge = assert_pause_discharge_array[inverter.id] if assert_pause_discharge_array else assert_pause_discharge
        if inv_assert_pause_discharge != inverter.pause_discharge:
            print("ERROR: Inverter {} Pause discharge should be {} got {}".format(inverter.id, inv_assert_pause_discharge, inverter.pause_discharge))
            failed = True
        if assert_charge_start_time_minutes_array:
            if assert_charge_start_time_minutes_array[inverter.id] != inverter.charge_start_time_minutes:
                print("ERROR: Inverter {} Charge start time should be {} got {}".format(inverter.id, assert_charge_start_time_minutes_array[inverter.id], inverter.charge_start_time_minutes))
                failed = True
        elif inv_assert_charge_time_enable and assert_charge_start_time_minutes != inverter.charge_start_time_minutes:
            print("ERROR: Inverter {} Charge start time should be {} got {}".format(inverter.id, assert_charge_start_time_minutes, inverter.charge_start_time_minutes))
            failed = True
        if assert_charge_end_time_minutes_array:
            if assert_charge_end_time_minutes_array[inverter.id] != inverter.charge_end_time_minutes:
                print("ERROR: Inverter {} Charge end time should be {} got {}".format(inverter.id, assert_charge_end_time_minutes_array[inverter.id], inverter.charge_end_time_minutes))
                failed = True
        elif inv_assert_charge_time_enable and assert_charge_end_time_minutes != inverter.charge_end_time_minutes:
            print("ERROR: Inverter {} Charge end time should be {} got {}".format(inverter.id, assert_charge_end_time_minutes, inverter.charge_end_time_minutes))
            failed = True
        if assert_force_export and assert_discharge_start_time_minutes != inverter.discharge_start_time_minutes:
            print("ERROR: Inverter {} Discharge start time should be {} got {}".format(inverter.id, assert_discharge_start_time_minutes, inverter.discharge_start_time_minutes))
            failed = True
        if assert_force_export and assert_discharge_end_time_minutes != inverter.discharge_end_time_minutes:
            print("ERROR: Inverter {} Discharge end time should be {} got {}".format(inverter.id, assert_discharge_end_time_minutes, inverter.discharge_end_time_minutes))
            failed = True
        expect_charge_rate = assert_charge_rate_array[inverter.id] if assert_charge_rate_array else assert_charge_rate
        expect_discharge_rate = assert_discharge_rate_array[inverter.id] if assert_discharge_rate_array else assert_discharge_rate
        if expect_charge_rate != inverter.charge_rate:
            print("ERROR: Inverter {} Charge rate should be {} got {}".format(inverter.id, expect_charge_rate, inverter.charge_rate))
            failed = True
        if expect_discharge_rate != inverter.discharge_rate:
            print("ERROR: Inverter {} Discharge rate should be {} got {}".format(inverter.id, expect_discharge_rate, inverter.discharge_rate))
            failed = True
        inv_assert_reserve = assert_reserve_array[inverter.id] if assert_reserve_array else assert_reserve
        if inv_assert_reserve != inverter.reserve_last:
            print("ERROR: Inverter {} Reserve should be {} got {}".format(inverter.id, inv_assert_reserve, inverter.reserve_last))
            failed = True
        if assert_soc_target_array:
            assert_soc_target = assert_soc_target_array[inverter.id]
        if assert_soc_target != inverter.soc_target:
            print("ERROR: Inverter {} SOC target should be {} got {}".format(inverter.id, assert_soc_target, inverter.soc_target))
            failed = True

        if assert_immediate_soc_target_array:
            assert_immediate_soc_target = assert_immediate_soc_target_array[inverter.id]

        if assert_immediate_charge_soc_target_array:
            assert_soc_target_force = assert_immediate_charge_soc_target_array[inverter.id]
        else:
            charging_immediate_statuses = ["Charging", "Charging, Hold for car", "Hold charging", "Freeze charging", "Hold charging, Hold for iBoost", "Hold charging, Hold for car", "Freeze charging, Hold for iBoost", "Hold for car", "Hold for iBoost"]
            exporting_statuses = ["Exporting", "Freeze exporting"]
            status_base = assert_status.split(" [")[0]
            if assert_status in charging_immediate_statuses:
                assert_soc_target_force = assert_immediate_soc_target
            elif any(s in status_base for s in exporting_statuses):
                # Untouched (-1), not the charge-stop's target: while exporting the charge-off block
                # is skipped, since adjust_export_immediate() already stopped charging as part of
                # starting the export (GH#4165, GH#4641).
                assert_soc_target_force = -1
            else:
                assert_soc_target_force = 0
            if not set_charge_window:
                assert_soc_target_force = -1
        if inverter.immediate_charge_soc_target != assert_soc_target_force:
            print("ERROR: Inverter {} Immediate charge SOC target should be {} got {}".format(inverter.id, assert_soc_target_force, inverter.immediate_charge_soc_target))
            failed = True
        if assert_immediate_charge_soc_freeze_array:
            if inverter.immediate_charge_soc_freeze != assert_immediate_charge_soc_freeze_array[inverter.id]:
                print("ERROR: Inverter {} Immediate charge SOC freeze should be {} got {}".format(inverter.id, assert_immediate_charge_soc_freeze_array[inverter.id], inverter.immediate_charge_soc_freeze))
                failed = True
        elif assert_status in ["Freeze charging"] and inverter.immediate_charge_soc_freeze is not True:
            print("ERROR: Inverter {} Immediate charge SOC freeze should be True got {}".format(inverter.id, inverter.immediate_charge_soc_freeze))
            failed = True
        if assert_immediate_discharge_soc_target_array:
            assert_soc_target_force_dis = assert_immediate_discharge_soc_target_array[inverter.id]
        else:
            assert_soc_target_force_dis = assert_immediate_soc_target if assert_status in ["Exporting", "Freeze exporting"] else 100
            if not set_export_window:
                assert_soc_target_force_dis = -1
        if inverter.immediate_discharge_soc_target != assert_soc_target_force_dis:
            print("ERROR: Inverter {} Immediate export SOC target should be {} got {}".format(inverter.id, assert_soc_target_force_dis, inverter.immediate_discharge_soc_target))
            failed = True
        if assert_status in ["Freeze exporting"] and inverter.immediate_discharge_soc_freeze is not True:
            print("ERROR: Inverter {} Immediate export SOC freeze should be True got {}".format(inverter.id, inverter.immediate_discharge_soc_freeze))
            failed = True

    # Validate isCharging binary sensor state: must be True for any charging status (Freeze charging, Hold charging, Charging variants)
    charging_statuses = ["Charging", "Freeze charging", "Hold charging"]
    expected_is_charging = assert_is_charging if assert_is_charging is not None else any(s in assert_status for s in charging_statuses)
    if my_predbat.isCharging != expected_is_charging:
        print("ERROR: isCharging should be {} for status '{}' got {}".format(expected_is_charging, assert_status, my_predbat.isCharging))
        failed = True

    # Validate isExporting binary sensor state: must be True for any exporting status (Exporting, Freeze exporting variants)
    # Use only the base status (before any " [...]" suffix) to avoid "Freeze exporting" matching "Demand [Freeze exporting]"
    exporting_statuses = ["Exporting", "Freeze exporting"]
    status_base = assert_status.split(" [")[0]
    expected_is_exporting = any(s in status_base for s in exporting_statuses)
    if my_predbat.isExporting != expected_is_exporting:
        print("ERROR: isExporting should be {} for status '{}' got {}".format(expected_is_exporting, assert_status, my_predbat.isExporting))
        failed = True

    my_predbat.minutes_now = 12 * 60
    return failed


def _run_fetch_inverter_data_pv_test(my_predbat, pv_sensors, pv_power=0):
    """
    Helper: configure one inverter with inverter_pv_power W of PV, set pv_sensors as the pv_power
    config list, optionally place extra_sensor_value under 'sensor.extra_pv', call
    fetch_inverter_data, and return the resulting my_predbat.pv_power.
    Restores the original config_index["pv_power"]["value"] on exit.
    """
    inverter = ActiveTestInverter(0, soc_kw=5, soc_max=10, now_utc=my_predbat.now_utc)
    inverter.pv_power = pv_power
    my_predbat.inverters = [inverter]
    my_predbat.args["num_inverters"] = 1
    my_predbat.args["pv_power"] = pv_sensors
    # Set pv_power via config_index so get_ha_config returns it (args is bypassed for known config items)

    my_predbat.fetch_inverter_data(create=False)
    result = my_predbat.pv_power

    return result


def test_fetch_inverter_data_extra_pv_sensors(my_predbat):
    """
    Test that fetch_inverter_data adds extra pv_power sensors beyond num_inverters to the total.
    This is the Fox thirdPartyGen case: one battery inverter whose pv_power list has an extra
    sensor (meterpower2) pointing to the AC-coupled Solis inverter measured via CT2.
    """
    print("  - test_fetch_inverter_data_extra_pv_sensors")

    result = _run_fetch_inverter_data_pv_test(my_predbat, pv_sensors=[994.0, 500.0], pv_power=994.0)
    if result != 1494:
        print("ERROR: pv_power should be 1494 (500 inverter + 994 extra CT2), got {}".format(result))
        return True
    return False


def test_fetch_inverter_data_extra_pv_sensors_invalid_value(my_predbat):
    """
    Test that fetch_inverter_data handles a non-numeric extra PV sensor value gracefully,
    leaving pv_power unchanged from the inverter total.
    """
    print("  - test_fetch_inverter_data_extra_pv_sensors_invalid_value")

    result = _run_fetch_inverter_data_pv_test(
        my_predbat,
        pv_sensors=["500", "undefined"],
        pv_power=500,
    )
    if result != 500:
        print("ERROR: pv_power should be 500 (invalid extra sensor skipped), got {}".format(result))
        return True
    return False


def test_fetch_inverter_data_no_extra_pv_when_sensors_match_inverters(my_predbat):
    """
    Test that fetch_inverter_data does not process extra PV sensors when len(pv_power) == num_inverters.
    The extra-sensor loop must only fire when the list is longer than the inverter count.
    """
    print("  - test_fetch_inverter_data_no_extra_pv_when_sensors_match_inverters")

    result = _run_fetch_inverter_data_pv_test(
        my_predbat,
        pv_sensors=["500"],
        pv_power=500,
    )
    if result != 500:
        print("ERROR: pv_power should be 500 (sensor count matches inverter count, no extra lookup), got {}".format(result))
        return True
    return False


def test_export_target_soc_percent(my_predbat):
    """
    The export target must carry the reserve floor when Predbat owns no reserve register

    A planned export limit of 0 means "empty it as far as you are allowed". Handing that raw to an
    inverter whose service maps it onto a device reserve tells it to drain the battery flat, which is
    what a Powerwall driven by service hooks actually did.
    """
    print("  - test_export_target_soc_percent")
    failed = False
    saved = (my_predbat.export_limits_best, my_predbat.set_reserve_enable, my_predbat.reserve, my_predbat.best_soc_min, my_predbat.soc_max)

    my_predbat.soc_max = 10.0
    my_predbat.reserve = 1.0  # 10%
    my_predbat.best_soc_min = 0.0

    cases = [
        # (export limit, reserve control, expected target)
        (0, False, 10),  # the reported bug: 0 would drain the battery flat
        (5, False, 10),  # below the reserve, still raised
        (20, False, 20),  # above the reserve, left alone
        (0, True, 0),  # Predbat owns the reserve, so the target is not its job
        (20, True, 20),
        # The same as tuples: export_target_soc_percent must read the target field, not int() the
        # instruction, which raised a TypeError once a limit was a tuple rather than a bare number.
        (pack_export_limit(EXPORT_MODE_TARGET, 0), False, 10),
        (pack_export_limit(EXPORT_MODE_TARGET, 20), True, 20),
        # A freeze carries no target - `export_target_of(...) or 0` falls back to 0, which the
        # reserve floor then raises when Predbat owns no register
        (pack_export_limit(EXPORT_MODE_FREEZE), False, 10),
    ]
    for limit, reserve_enable, expect in cases:
        my_predbat.export_limits_best = [limit]
        my_predbat.set_reserve_enable = reserve_enable
        got = my_predbat.export_target_soc_percent()
        if got != expect:
            print("ERROR: export target for limit {} with set_reserve_enable={} should be {} got {}".format(limit, reserve_enable, expect, got))
            failed = True

    # best_soc_min above the reserve wins, matching how discharge_soc resolves the floor
    my_predbat.best_soc_min = 3.0  # 30%
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
    my_predbat.set_reserve_enable = False
    if my_predbat.export_target_soc_percent() != 30:
        print("ERROR: export target should follow best_soc_min of 30%, got {}".format(my_predbat.export_target_soc_percent()))
        failed = True

    my_predbat.export_limits_best, my_predbat.set_reserve_enable, my_predbat.reserve, my_predbat.best_soc_min, my_predbat.soc_max = saved
    return failed


def test_quick_poll_rebalance_guards(my_predbat):
    """
    The inverter poll is the second caller of the single write path, so it must never invent a
    rate the executor did not ask for.

    Three guards: it does nothing before execute_plan has ever run, nothing when balancing is
    disabled, and it works on a COPY of the stored intent so successive polls cannot compound
    their own skew on top of each other.
    """
    failed = False
    saved = save_balance_state(my_predbat)
    saved_read_only = my_predbat.set_read_only
    try:
        # No intent yet - a poll that beats the first plan run must write nothing
        my_predbat.inverter_rate_intent = {}
        my_predbat.balance_inverters_enable = True
        my_predbat.set_read_only = False
        my_predbat.rebalance_inverter_rates()

        # Balancing disabled - the poll must leave the intent alone
        my_predbat.inverter_rate_intent = {0: {"charge_rate": 1234, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "charge"}}
        my_predbat.balance_inverters_enable = False
        my_predbat.rebalance_inverter_rates()
        if my_predbat.inverter_rate_intent[0]["charge_rate"] != 1234:
            print("ERROR: a poll with balancing disabled modified the stored intent")
            failed = True

        # Enabled, and forced into a state where balancing genuinely acts: inverter 0 well below
        # inverter 1 while both discharge hard. Balance must set discharge_rate 0 on inverter 0 in
        # its working copy, and the STORED intent must still come back untouched - otherwise
        # successive polls would compound their own skew on top of each other.
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_discharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_crosscharge = False
        my_predbat.balance_inverters_threshold_discharge = 5
        my_predbat.balance_inverters_threshold_charge = 5
        saved_readings = [(inverter.soc_percent, inverter.battery_power, inverter.discharge_rate_now, inverter.reserve_current, inverter.battery_rate_max_discharge) for inverter in my_predbat.inverters]
        try:
            for inverter in my_predbat.inverters:
                inverter.soc_percent = 20 if inverter.id == 0 else 80
                inverter.battery_power = 1000
                inverter.discharge_rate_now = 2600 / MINUTE_WATT
                # Pin the ceiling too. The capacity guard reasons about the rates that will be
                # applied, which for an unclaimed intent is the maximum - so leaving this to
                # whatever the previous scenario happened to set makes the fixture order-dependent.
                inverter.battery_rate_max_discharge = 2600 / MINUTE_WATT
                inverter.reserve_current = 4
            my_predbat.inverter_rate_intent = {inverter.id: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"} for inverter in my_predbat.inverters}

            # Prove balancing really does act on these readings, or the copy assertion below is vacuous
            probe = {inverter_id: dict(value) for inverter_id, value in my_predbat.inverter_rate_intent.items()}
            my_predbat.balance_inverter_rates(probe)
            if probe[0]["discharge_rate"] != 0:
                print("ERROR: the rebalance fixture does not actually trigger balancing, so the copy test proves nothing")
                failed = True

            my_predbat.rebalance_inverter_rates()
            for inverter_id, value in my_predbat.inverter_rate_intent.items():
                if value["discharge_rate"] is not None:
                    print("ERROR: the poll mutated the stored intent for inverter {} - successive polls would compound".format(inverter_id))
                    failed = True
        finally:
            for inverter, (soc, power, rate, reserve, max_discharge) in zip(my_predbat.inverters, saved_readings):
                inverter.soc_percent = soc
                inverter.battery_power = power
                inverter.discharge_rate_now = rate
                inverter.reserve_current = reserve
                inverter.battery_rate_max_discharge = max_discharge
    finally:
        # Every balance switch and threshold this test assigns has to go back, not just the three
        # it used to restore - the whole registry entry shares one PredBat, so anything left
        # behind turns later scenarios order-dependent (GH#5079).
        restore_balance_state(my_predbat, saved)
        my_predbat.set_read_only = saved_read_only
    return failed


def balance_fixture(my_predbat, name):
    """
    Establish a valid two-inverter demand plan state for the balancing tests to build on.

    Returns:
    - bool: True if the setup scenario itself failed
    """
    return run_execute_test(
        my_predbat,
        name,
        soc_kw=7.35,
        soc_kw_array=[4.75, 2.6],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_rate_array=[2600, 2600],
        assert_discharge_rate_array=[2600, 2600],
    )


def save_balance_state(my_predbat):
    """
    Capture everything the balancing tests mutate.

    The suite shares one PredBat, so anything forced here has to be put back or it leaks into
    later tests as an ordering-dependent failure (GH#5079).

    Returns:
    - dict: the saved state, for restore_balance_state()
    """
    return {
        "readings": [(inverter.battery_power, inverter.grid_power, inverter.soc_percent) for inverter in my_predbat.inverters],
        "enable": my_predbat.balance_inverters_enable,
        "charge": my_predbat.balance_inverters_charge,
        "discharge": my_predbat.balance_inverters_discharge,
        "crosscharge": my_predbat.balance_inverters_crosscharge,
        "threshold_charge": my_predbat.balance_inverters_threshold_charge,
        "threshold_discharge": my_predbat.balance_inverters_threshold_discharge,
        "intent": my_predbat.inverter_rate_intent,
    }


def restore_balance_state(my_predbat, saved):
    """
    Put back everything save_balance_state() captured.
    """
    my_predbat.balance_inverters_enable = saved["enable"]
    my_predbat.balance_inverters_charge = saved["charge"]
    my_predbat.balance_inverters_discharge = saved["discharge"]
    my_predbat.balance_inverters_crosscharge = saved["crosscharge"]
    my_predbat.balance_inverters_threshold_charge = saved["threshold_charge"]
    my_predbat.balance_inverters_threshold_discharge = saved["threshold_discharge"]
    my_predbat.inverter_rate_intent = saved["intent"]
    for inverter, (power, grid, soc) in zip(my_predbat.inverters, saved["readings"]):
        inverter.battery_power = power
        inverter.grid_power = grid
        inverter.soc_percent = soc


def force_fleet(my_predbat, powers, grid=0.0, socs=None):
    """
    Force each inverter's measured battery power, the fleet's grid power, and optionally each
    inverter's SoC percentage so a test can create a deliberate imbalance.

    Grid follows Predbat's convention: positive exporting, negative importing. It is put on
    inverter 0 only, since balance_inverters sums it across the fleet.
    """
    for inverter in my_predbat.inverters:
        inverter.battery_power = powers[inverter.id]
        inverter.grid_power = grid if inverter.id == 0 else 0.0
        if socs:
            inverter.soc_percent = socs[inverter.id]


def clean_intent(my_predbat):
    """
    An executor baseline where no branch has claimed any rate.

    Returns:
    - dict: inverter id -> rate intent
    """
    return {inverter.id: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"} for inverter in my_predbat.inverters}


def test_crosscharge_is_prevented_through_execute_plan(my_predbat):
    """
    Cross-charge prevention is the behaviour this feature exists for, so it gets its own test
    rather than riding along as a precondition inside a test about something else.

    Inverter 1 charges while inverter 0 discharges and the fleet imports - no PV surplus behind
    it, so that charge is coming off the grid or out of the other battery.
    """
    failed = balance_fixture(my_predbat, "crosscharge_execute_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False
        force_fleet(my_predbat, {0: 2000.0, 1: -500.0}, grid=0.0)
        my_predbat.execute_plan()

        if my_predbat.inverters[1].charge_rate != 0:
            print("ERROR: the cross-charging inverter was not held, got charge rate {}".format(my_predbat.inverters[1].charge_rate))
            failed = True
        if my_predbat.inverters[0].discharge_rate != 2600:
            print("ERROR: the discharging inverter must be left carrying the house, got discharge rate {}".format(my_predbat.inverters[0].discharge_rate))
            failed = True
        if my_predbat.inverters[0].charge_rate != 2600:
            print("ERROR: the innocent inverter's charge rate must be untouched, got {}".format(my_predbat.inverters[0].charge_rate))
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_crosscharge_is_prevented_through_the_inverter_poll(my_predbat):
    """
    The 60s inverter poll is the path that actually does the correcting between plan runs, so it
    has to be shown reaching the hardware - not just execute_plan.
    """
    failed = balance_fixture(my_predbat, "crosscharge_poll_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False
        force_fleet(my_predbat, {0: 2000.0, 1: -500.0}, grid=0.0)
        my_predbat.inverter_rate_intent = clean_intent(my_predbat)
        my_predbat.rebalance_inverter_rates()

        if my_predbat.inverters[1].charge_rate != 0:
            print("ERROR: the poll did not hold the cross-charger, got charge rate {}".format(my_predbat.inverters[1].charge_rate))
            failed = True
        if my_predbat.inverter_rate_intent[1]["charge_rate"] is not None:
            print("ERROR: the poll mutated its stored baseline, so successive polls would compound")
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_pv_surplus_holds_the_discharger_not_the_chargers(my_predbat):
    """
    With spare PV the fleet should be soaking it up, so an inverter DISCHARGING into the surplus
    is the anomaly - not the one charging.

    This is also the only test that proves grid_power reaches the balance snapshot. Drop that one
    line from build_inverter_snapshot and spare PV reads as -1000W instead of +4000W, the direction
    flips, and the charging inverter gets held instead.
    """
    failed = balance_fixture(my_predbat, "pv_surplus_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False
        # Exporting 5000W while the batteries net +1000W discharging: spare PV = 5000 - 1000 = 4000W
        force_fleet(my_predbat, {0: -1000.0, 1: 2000.0}, grid=5000.0)
        my_predbat.execute_plan()

        if my_predbat.inverters[0].charge_rate != 2600:
            print("ERROR: the inverter absorbing surplus PV was held, got charge rate {}".format(my_predbat.inverters[0].charge_rate))
            failed = True
        if my_predbat.inverters[1].discharge_rate != 0:
            print("ERROR: the inverter discharging into a PV surplus was not held, got discharge rate {}".format(my_predbat.inverters[1].discharge_rate))
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_executor_rate_survives_a_balance_hold(my_predbat):
    """
    Holding one inverter must not reset another to the register ceiling.

    This is the F5 invariant at execute level: convergence returns to the EXECUTOR's rate, not to
    max. Inverter 0 is given a deliberately reduced rate, inverter 1 is held for working against
    the fleet, and inverter 0's reduced rate has to survive the pass.
    """
    failed = balance_fixture(my_predbat, "executor_rate_survives_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False
        # Fleet net charging, with inverter 1 discharging against it
        force_fleet(my_predbat, {0: -2000.0, 1: 500.0}, grid=-1500.0)
        intent = clean_intent(my_predbat)
        intent[0]["charge_rate"] = 900
        intent[0]["owner"] = "charge"
        my_predbat.inverter_rate_intent = intent
        my_predbat.rebalance_inverter_rates()

        if my_predbat.inverters[1].discharge_rate != 0:
            print("ERROR: the inverter working against the fleet was not held, got discharge rate {}".format(my_predbat.inverters[1].discharge_rate))
            failed = True
        if my_predbat.inverters[0].charge_rate != 900:
            print("ERROR: the executor's reduced charge rate was not preserved, got {} (2600 means it was reset to max)".format(my_predbat.inverters[0].charge_rate))
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_discharge_balancing_through_execute_plan(my_predbat):
    """
    SoC balancing on discharge, on a heterogeneous fleet, through the real execute path.

    Inverter 0 is well behind inverter 1 while both discharge, so it stops discharging and the
    fuller one carries the house until they converge.
    """
    failed = balance_fixture(my_predbat, "discharge_balance_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_discharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_crosscharge = False
        force_fleet(my_predbat, {0: 800.0, 1: 800.0}, grid=-1600.0, socs={0: 20.0, 1: 80.0})
        my_predbat.execute_plan()

        if my_predbat.inverters[0].discharge_rate != 0:
            print("ERROR: the inverter behind on SoC was not held, got discharge rate {}".format(my_predbat.inverters[0].discharge_rate))
            failed = True
        if my_predbat.inverters[1].discharge_rate != 2600:
            print("ERROR: the fuller inverter must be left carrying the house, got discharge rate {}".format(my_predbat.inverters[1].discharge_rate))
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_charge_balancing_through_execute_plan(my_predbat):
    """
    SoC balancing on charge, on a heterogeneous fleet, through the real execute path.

    Until this existed the charge branch had no execute-level coverage at all, and no pure test
    either - the whole branch could be deleted and only one legacy scenario with two identical
    inverters would notice.
    """
    failed = balance_fixture(my_predbat, "charge_balance_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_charge = True
        my_predbat.balance_inverters_discharge = False
        my_predbat.balance_inverters_crosscharge = False
        force_fleet(my_predbat, {0: -800.0, 1: -800.0}, grid=-1600.0, socs={0: 80.0, 1: 20.0})
        my_predbat.execute_plan()

        if my_predbat.inverters[0].charge_rate != 0:
            print("ERROR: the inverter ahead on SoC was not held, got charge rate {}".format(my_predbat.inverters[0].charge_rate))
            failed = True
        if my_predbat.inverters[1].charge_rate != 2600:
            print("ERROR: the inverter behind must be left to catch up, got charge rate {}".format(my_predbat.inverters[1].charge_rate))
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_calibration_discards_intent_collected_so_far(my_predbat):
    """
    When any inverter is calibrating the whole fleet is put to full rates and the loop breaks.

    Inverters processed BEFORE the calibrating one have already recorded their planned intent,
    though. Applying that afterwards writes those planned rates straight back over the
    calibration-safe settings - and the stored baseline keeps them, so the 60s poll re-applies
    them for as long as calibration lasts. Before the intent refactor this could not happen:
    earlier inverters wrote inline and the calibration reset came after them.
    """
    failed = balance_fixture(my_predbat, "calibration_setup")
    if failed:
        return failed
    saved_calibration = [inverter.in_calibration for inverter in my_predbat.inverters]
    try:
        # Give inverter 0 a deliberately non-max rate, then make the LATER inverter calibrate
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(500)
        my_predbat.inverters[0].in_calibration = False
        my_predbat.inverters[1].in_calibration = True
        my_predbat.execute_plan()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate != 2600:
                print("ERROR: inverter {} was left at {}W during calibration, the fleet must be at full rate".format(inverter.id, inverter.charge_rate))
                failed = True
        if my_predbat.inverter_rate_intent:
            print("ERROR: intent collected before the calibrating inverter was kept as the poll baseline: {}".format(my_predbat.inverter_rate_intent))
            failed = True
    finally:
        for inverter, calibration in zip(my_predbat.inverters, saved_calibration):
            inverter.in_calibration = calibration
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
    return failed


def test_read_only_mode_writes_no_rates(my_predbat):
    """
    In read-only mode Predbat must not write a rate at all.

    The read-only branch continues before recording any intent, so the apply pass skips those
    inverters on "id in intent". Drop that guard and every inverter gets written - and because an
    absent intent resolves to max, it writes the maximum rate. Existing read-only scenarios cannot
    see this: they leave the rates at max anyway, so writing max looks identical to not writing.
    A deliberately non-max rate is set first so the two are distinguishable.
    """
    failed = balance_fixture(my_predbat, "read_only_setup")
    if failed:
        return failed
    saved_read_only = my_predbat.set_read_only
    try:
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(500)
            inverter.adjust_discharge_rate(500)
        my_predbat.set_read_only = True
        my_predbat.execute_plan()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate != 500:
                print("ERROR: read-only mode wrote inverter {} charge rate, got {} (2600 means it was reset to max)".format(inverter.id, inverter.charge_rate))
                failed = True
            if inverter.discharge_rate != 500:
                print("ERROR: read-only mode wrote inverter {} discharge rate, got {}".format(inverter.id, inverter.discharge_rate))
                failed = True
    finally:
        my_predbat.set_read_only = saved_read_only
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
    return failed


def test_balance_holds_do_not_raise_notifications(my_predbat):
    """
    A balancing hold must not notify the user.

    The old timer-based balancer passed notify=False on every rate it wrote, precisely because it
    runs on a minute cadence and would otherwise spam anyone with set_inverter_notify on. Routing
    those writes through the shared apply pass lost that, so a hold and its release each raised a
    notification every time the fleet drifted in and out of balance.

    The executor's own rate changes must still notify - only the balancer's mutations are silent.
    """
    failed = balance_fixture(my_predbat, "balance_notify_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False
        force_fleet(my_predbat, {0: 2000.0, 1: -500.0}, grid=0.0)
        my_predbat.execute_plan()

        if my_predbat.inverters[1].charge_rate != 0:
            print("ERROR: balancing did not hold the cross-charger, so this test proves nothing")
            failed = True
        if my_predbat.inverters[1].charge_rate_notify:
            print("ERROR: the balancing hold on inverter 1 was written with notify on")
            failed = True
        # The executor's own rates are unchanged by balancing here, so they keep notifying
        if not my_predbat.inverters[0].charge_rate_notify:
            print("ERROR: inverter 0 was not touched by balancing and must still notify")
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_poll_clamps_stored_rates_to_refreshed_ceilings(my_predbat):
    """
    The poll's stored intent carries explicit watt targets from the last plan run, but the poll
    has just re-read each inverter's limits. Some configurations source inverter_limit_charge and
    _discharge from live BMS sensors, so a ceiling can drop between plan runs - and the poll would
    then keep re-applying a rate above it, with the balance capacity guard overestimating what the
    fleet can deliver on top.
    """
    failed = balance_fixture(my_predbat, "ceiling_clamp_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    saved_ceilings = [(inverter.battery_rate_max_charge, inverter.battery_rate_max_discharge) for inverter in my_predbat.inverters]
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = False
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False
        force_fleet(my_predbat, {0: 0.0, 1: 0.0}, grid=0.0)
        # The last plan asked for 2400W; the BMS has since dropped the ceiling to 1000W
        my_predbat.inverter_rate_intent = {inverter.id: {"charge_rate": 2400, "discharge_rate": 2400, "pause_charge": False, "pause_discharge": False, "owner": "charge"} for inverter in my_predbat.inverters}
        for inverter in my_predbat.inverters:
            inverter.battery_rate_max_charge = 1000 / MINUTE_WATT
            inverter.battery_rate_max_discharge = 1000 / MINUTE_WATT
        my_predbat.rebalance_inverter_rates()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate > 1000:
                print("ERROR: inverter {} was written {}W of charge above its {}W ceiling".format(inverter.id, inverter.charge_rate, 1000))
                failed = True
            if inverter.discharge_rate > 1000:
                print("ERROR: inverter {} was written {}W of discharge above its {}W ceiling".format(inverter.id, inverter.discharge_rate, 1000))
                failed = True
    finally:
        for inverter, (max_charge, max_discharge) in zip(my_predbat.inverters, saved_ceilings):
            inverter.battery_rate_max_charge = max_charge
            inverter.battery_rate_max_discharge = max_discharge
        restore_balance_state(my_predbat, saved)
    return failed


def test_monitor_mode_writes_no_rates(my_predbat):
    """
    Monitor and Control-SoC-only modes must not touch the rate registers.

    Both leave set_charge_window and set_export_window false, and Monitor is documented as not
    controlling charging or discharging. The reset flags this refactor replaced started FALSE in
    that case, so no rate call was made at all. Resolving an unclaimed intent to the inverter
    maximum and writing it unconditionally would have Predbat setting full rates in the one mode
    where it is meant to be watching.
    """
    failed = balance_fixture(my_predbat, "monitor_mode_setup")
    if failed:
        return failed
    saved_charge_window = my_predbat.set_charge_window
    saved_export_window = my_predbat.set_export_window
    try:
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(500)
            inverter.adjust_discharge_rate(500)
        my_predbat.set_charge_window = False
        my_predbat.set_export_window = False
        my_predbat.execute_plan()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate != 500:
                print("ERROR: monitor mode wrote inverter {} charge rate, got {} (2600 means it was reset to max)".format(inverter.id, inverter.charge_rate))
                failed = True
            if inverter.discharge_rate != 500:
                print("ERROR: monitor mode wrote inverter {} discharge rate, got {}".format(inverter.id, inverter.discharge_rate))
                failed = True
    finally:
        my_predbat.set_charge_window = saved_charge_window
        my_predbat.set_export_window = saved_export_window
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
    return failed


def test_poll_does_not_apply_stale_intent_during_calibration(my_predbat):
    """
    A poll landing just after an inverter starts calibrating still holds the previous plan's
    intent, because execute_plan has not run since to clear it.

    balance_inverters() returns untouched when it sees calibration, but the poll would apply that
    stale intent anyway - writing planned rates over the full-rate calibration settings every 60s
    until the next plan run. The calibrating inverter's own firmware is driving it at that point.
    """
    failed = balance_fixture(my_predbat, "poll_calibration_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    saved_calibration = [inverter.in_calibration for inverter in my_predbat.inverters]
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
        # The previous plan wanted 900W; an inverter has since entered calibration
        my_predbat.inverter_rate_intent = {inverter.id: {"charge_rate": 900, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "charge", "reset_rates": True} for inverter in my_predbat.inverters}
        my_predbat.inverters[1].in_calibration = True
        my_predbat.rebalance_inverter_rates()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate == 900:
                print("ERROR: the poll applied the previous plan's {}W to inverter {} during calibration".format(inverter.charge_rate, inverter.id))
                failed = True
        if my_predbat.inverter_rate_intent:
            print("ERROR: stale intent was kept during calibration, so the next poll would apply it again")
            failed = True
    finally:
        for inverter, calibration in zip(my_predbat.inverters, saved_calibration):
            inverter.in_calibration = calibration
        restore_balance_state(my_predbat, saved)
    return failed


def test_a_claimed_rate_is_written_even_when_rates_are_not_reset(my_predbat):
    """
    Both halves of the write rule, in one scenario.

    set_freeze_export_during_demand is not gated on the charge/export windows, so it can claim a
    rate while reset_rates is false. The original wrote that claim regardless of the reset flags,
    and so must this: a rate something claimed is always written, while a rate nobody claimed is
    left alone when the executor is not driving the windows.
    """
    failed = balance_fixture(my_predbat, "claimed_rate_setup")
    if failed:
        return failed
    saved_charge_window = my_predbat.set_charge_window
    saved_export_window = my_predbat.set_export_window
    saved_freeze_demand = my_predbat.set_freeze_export_during_demand
    try:
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(500)
            inverter.adjust_discharge_rate(500)
            inverter.inv_charge_discharge_with_rate = False
            inverter.inv_has_timed_pause = False
        my_predbat.set_charge_window = False
        my_predbat.set_export_window = False
        my_predbat.set_freeze_export_during_demand = True
        my_predbat.execute_plan()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate != 0:
                print("ERROR: inverter {} claimed charge rate 0 was not written, got {}".format(inverter.id, inverter.charge_rate))
                failed = True
            if inverter.discharge_rate != 500:
                print("ERROR: inverter {} discharge was unclaimed and must be left alone, got {}".format(inverter.id, inverter.discharge_rate))
                failed = True
    finally:
        my_predbat.set_charge_window = saved_charge_window
        my_predbat.set_export_window = saved_export_window
        my_predbat.set_freeze_export_during_demand = saved_freeze_demand
        for inverter in my_predbat.inverters:
            inverter.inv_has_timed_pause = True
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
    return failed


def test_control_soc_only_sets_targets_without_writing_rates(my_predbat):
    """
    Control SoC only is the other mode where both windows are off, and it differs from Monitor in
    that set_soc_enable stays true: it still sets the battery target, it just never touches the
    rate registers. Monitor leaves both off.
    """
    failed = balance_fixture(my_predbat, "control_soc_only_setup")
    if failed:
        return failed
    saved_charge_window = my_predbat.set_charge_window
    saved_export_window = my_predbat.set_export_window
    saved_soc_enable = my_predbat.set_soc_enable
    try:
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(500)
            inverter.adjust_discharge_rate(500)
            inverter.soc_target = -1
        my_predbat.set_charge_window = False
        my_predbat.set_export_window = False
        my_predbat.set_soc_enable = True
        my_predbat.execute_plan()

        for inverter in my_predbat.inverters:
            if inverter.charge_rate != 500 or inverter.discharge_rate != 500:
                print("ERROR: Control SoC only wrote rates on inverter {}: charge {} discharge {}".format(inverter.id, inverter.charge_rate, inverter.discharge_rate))
                failed = True
            if inverter.soc_target == -1:
                print("ERROR: Control SoC only must still set the battery target on inverter {}".format(inverter.id))
                failed = True
    finally:
        my_predbat.set_charge_window = saved_charge_window
        my_predbat.set_export_window = saved_export_window
        my_predbat.set_soc_enable = saved_soc_enable
        for inverter in my_predbat.inverters:
            inverter.adjust_charge_rate(inverter.battery_rate_max_charge * MINUTE_WATT)
            inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * MINUTE_WATT)
    return failed


def test_balance_release_is_also_silent(my_predbat):
    """
    Both edges of a balancing override must be silent, not just the one that applies it.

    On release the fresh intent matches the executor baseline again - None == None - so comparing
    the two says "nothing changed" and the write that takes the rate back off zero notifies. The
    old balancer passed notify=False on its restore loop as well as on the hold.
    """
    failed = balance_fixture(my_predbat, "balance_release_setup")
    if failed:
        return failed
    saved = save_balance_state(my_predbat)
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        my_predbat.balance_inverters_charge = False
        my_predbat.balance_inverters_discharge = False

        # Cycle one: cross-charging, so inverter 1 is held
        force_fleet(my_predbat, {0: 2000.0, 1: -500.0}, grid=0.0)
        my_predbat.execute_plan()
        if my_predbat.inverters[1].charge_rate != 0:
            print("ERROR: the hold was not applied, so the release cannot be tested")
            failed = True

        # Cycle two: the fleet is no longer fighting itself, so the hold is released
        force_fleet(my_predbat, {0: 1000.0, 1: 1000.0}, grid=0.0)
        my_predbat.execute_plan()
        if my_predbat.inverters[1].charge_rate == 0:
            print("ERROR: the hold was not released, so the release cannot be tested")
            failed = True
        if my_predbat.inverters[1].charge_rate_notify:
            print("ERROR: releasing the balancing hold on inverter 1 notified the user")
            failed = True
    finally:
        restore_balance_state(my_predbat, saved)
    return failed


def test_stored_intent_is_the_executor_baseline(my_predbat):
    """
    The baseline the inverter poll re-applies must be the EXECUTOR's intent, not the balanced one.

    execute_plan mutates intent in place when it balances, so storing it afterwards saves the
    holds too. The poll then re-applies those holds every cycle, and once the fleet comes back
    into balance the balancer returns without touching them - so a temporary hold sticks until the
    next full plan run, up to five minutes later. That is the F5 failure this design is supposed
    to make unrepresentable, reintroduced through the back door.
    """
    failed = run_execute_test(
        my_predbat,
        "baseline_setup",
        soc_kw=7.35,
        soc_kw_array=[4.75, 2.6],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_rate_array=[2600, 2600],
        assert_discharge_rate_array=[2600, 2600],
    )
    if failed:
        return failed

    saved = [(inverter.battery_power, inverter.grid_power, inverter.soc_percent) for inverter in my_predbat.inverters]
    saved_enable = my_predbat.balance_inverters_enable
    saved_cross = my_predbat.balance_inverters_crosscharge
    try:
        my_predbat.balance_inverters_enable = True
        my_predbat.balance_inverters_crosscharge = True
        # Inverter 1 charging while the fleet is net discharging, with no PV surplus behind it
        for inverter in my_predbat.inverters:
            inverter.grid_power = 0
            inverter.battery_power = 2000 if inverter.id == 0 else -500
        my_predbat.execute_plan()

        if my_predbat.inverters[1].charge_rate != 0:
            print("ERROR: balancing did not hold the cross-charger, so this test proves nothing (got {})".format(my_predbat.inverters[1].charge_rate))
            failed = True
        if my_predbat.inverter_rate_intent.get(1, {}).get("charge_rate") is not None:
            print("ERROR: the stored baseline contains the balance hold - the poll would re-apply it until the next plan run")
            failed = True
    finally:
        my_predbat.balance_inverters_enable = saved_enable
        my_predbat.balance_inverters_crosscharge = saved_cross
        for inverter, (power, grid, soc) in zip(my_predbat.inverters, saved):
            inverter.battery_power = power
            inverter.grid_power = grid
            inverter.soc_percent = soc
    return failed


def run_execute_tests(my_predbat):
    print("**** Running execute tests ****\n")

    failed = test_export_target_soc_percent(my_predbat)
    failed |= test_fetch_inverter_data_extra_pv_sensors(my_predbat)
    failed |= test_fetch_inverter_data_extra_pv_sensors_invalid_value(my_predbat)
    failed |= test_fetch_inverter_data_no_extra_pv_when_sensors_match_inverters(my_predbat)
    if failed:
        return failed

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
    charge_limit_best3 = [8]
    charge_limit_best_frz = [1]
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    export_window_best2 = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best3 = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best4 = [{"start": my_predbat.minutes_now + 15, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best5 = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    export_window_best6 = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best7 = [{"start": 0, "end": my_predbat.minutes_now + 12 * 60, "average": 1}]
    export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
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

    # Inverter is actively Charging, not merely holding - the discharge-hold action is correctly
    # skipped (status is in ["Exporting", "Charging"]), so no pause/reserve change happens. The
    # "Hold for iBoost" annotation must not be appended either, since nothing was actually held for
    # iBoost this cycle - it should stay coupled to whether the hold action fired, not fire regardless.
    failed |= run_execute_test(
        my_predbat,
        "charge_iboost_no_hold_text",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_pause_discharge=False,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
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

    # Here inverter 0 is below target (still charging) and inverter 1 is already above target
    # (holding). The headline status shows "Charging" - the most active state present across the
    # fleet (#4446) - rather than whichever inverter happened to be processed last, since the fleet
    # overall genuinely is still charging until inverter 0 catches up.
    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_status_extra=" target Charging 90%-100.0% / Hold charging 100%-100.0%",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=9.5,
        soc_kw_array=[4.5, 5],
    )
    if failed:
        return failed

    # Here the target is 5 and we are at 5 so we will hold charge with a pause
    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        soc_kw=5,
        soc_kw_array=[2.0, 3.0],
        assert_soc_target_array=[100, 100],
        assert_immediate_soc_target_array=[40, 60],
        assert_pause_discharge=True,
    )
    if failed:
        return failed

    # We the battery level is 6kWh and the target is 5kWh, we will hold charge and let it discharge.
    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance4",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        soc_kw=6,
        soc_kw_array=[3.0, 3.0],
        assert_soc_target_array=[100, 100],
        assert_immediate_soc_target=50,
        assert_pause_discharge=False,
        assert_reserve=51,
    )
    if failed:
        return failed

    # Here the battery level is 7kWh and we target 5kWh, we will hold charge and let it discharge.
    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance5",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        soc_kw=7,
        soc_kw_array=[3.0, 4.0],
        assert_soc_target_array=[100, 100],
        assert_immediate_soc_target_array=[40, 60],
        assert_pause_discharge=False,
        assert_reserve_array=[41, 61],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance6",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_soc_target_array=[80, 20],
        assert_immediate_soc_target_array=[80, 20],
        soc_kw=4,
        soc_kw_array=[3.5, 0.5],
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance7",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best3,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_soc_target_array=[100, 35],
        assert_immediate_soc_target_array=[100, 35],
        soc_kw=5.5,
        soc_kw_array=[5, 0.5],
    )
    if failed:
        return failed

    # Here the battery level is 4kWh and we target 5kWh, we will charge.
    # We add 1 kWh which is 0.5kWh per inverter, hence going from 20%->30% and 60%->70% SOC, which is the target.
    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance8",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=780,
        soc_kw=4,
        soc_kw_array=[1.0, 3.0],
        assert_soc_target_array=[30, 70],
        assert_immediate_soc_target_array=[30, 70],
        assert_pause_discharge=False,
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

    # Same window, but 1kW of PV forecast across the 60 minutes (1kWh, over the 0.1kWh threshold).
    # Throttling would cap how much of that PV reaches the battery, so charge at the max rate instead
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_pv",
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
        assert_charge_rate=2000,
        battery_max_rate=2000,
        pv_forecast=1.0,
    )
    if failed:
        return failed

    # A trace of PV (0.05kWh over the window) is under the threshold, low power charging still applies
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_pv_trace",
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
        pv_forecast=0.05,
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
        assert_charge_rate=500,  # Low power picks 500W; the old execute-side 10% (200W) check suppressed the change and left 600, the single 5% (100W) deadband does not
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

    # Here the current SOC is 9kWh and the target is 5kWh
    # but set_charge_during_charge is false so we should pause the discharge.
    failed |= run_execute_test(
        my_predbat,
        "charge2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        set_discharge_during_charge=False,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
        assert_reserve=51,
    )
    if failed:
        return failed
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
    if failed:
        return failed
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
    # Here the target is 5kWh and we are at 9kWh, so we will hold charge and let it discharge.
    failed |= run_execute_test(
        my_predbat,
        "charge2d",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
        assert_reserve=51,
    )
    if failed:
        return failed
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
    if failed:
        return failed
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

    # Here the target is 5kWh and we are at 9kWh so we will let it discharge.
    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
        reserve_max=50,
        has_timed_pause=False,
        assert_reserve_array=[50, 50],
    )
    if failed:
        return failed

    # Here the reserve can't hold so we keep charging on with target 50%
    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max1b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=780,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target=50,
        reserve_max=10,
        has_timed_pause=False,
        assert_reserve_array=[0, 0],
    )
    if failed:
        return failed
    # Here the target is 5kWh and we are at 9kWh so we will let it discharge.
    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target_array=[100, 100],
        assert_immediate_soc_target_array=[60, 40],
        assert_reserve_array=[61, 41],
        reserve_max=90,
        has_timed_pause=False,
        soc_kw_array=[5, 4],
    )
    if failed:
        return failed

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

    # Regression for PR #4466: inverter 0 genuinely charges and reaches "Charging" (populating
    # status_per_inverter[0]) before inverter 1 - processed second - enters calibration mode and
    # breaks out of the loop. The headline status must stay "Calibration", not resolve back to the
    # stale "Charging" state inverter 0 left behind in status_per_inverter.
    # The per-inverter asserts below capture the real half-processed state the break leaves behind:
    # inverter 0 keeps the charge window/immediate targets it was already given, inverter 1 never
    # reached those calls, and isCharging stays True from inverter 0 (pre-existing behaviour, since
    # the calibration branch breaks without resetting it).
    failed |= run_execute_test(
        my_predbat,
        "calibration_after_charging_inverter",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        set_charge_window=True,
        set_export_window=True,
        in_calibration_array=[False, True],
        assert_charge_time_enable_array=[True, False],
        assert_status="Calibration",
        assert_reserve=0,
        assert_soc_target=100,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_immediate_charge_soc_target_array=[100, -1],
        assert_immediate_discharge_soc_target_array=[100, -1],
        assert_is_charging=True,
    )
    if failed:
        return failed
    failed |= run_execute_test(my_predbat, "no_charge3", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(
        my_predbat,
        "charge_read_only",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        set_charge_window=True,
        set_export_window=True,
        read_only=True,
        assert_status="Read-Only",
        reserve=0,
        assert_immediate_charge_soc_target_array=[-1, -1],
        assert_immediate_discharge_soc_target_array=[-1, -1],
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_axle_read_only",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        set_charge_window=True,
        set_export_window=True,
        read_only=True,
        set_read_only_axle=True,
        assert_status="Read-Only (Axle)",
        reserve=0,
        assert_immediate_charge_soc_target_array=[-1, -1],
        assert_immediate_discharge_soc_target_array=[-1, -1],
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

    # Charge freeze, one inverter is empty and the other at 40%
    # Inverter 0 will charge to 10% while inverter 1 will freeze at current level of 40% (as it is above the reserve of 1kWh)
    # Headline status shows "Charging" - the most active state present (#4446) - since inverter 0
    # genuinely is still charging, not just whichever inverter happened to be processed last.
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable_array=[True, False],
        soc_kw=2,
        assert_pause_discharge_array=[False, True],
        assert_status="Charging",
        assert_status_extra=" target Charging 0%-10% / Freeze charging 40%",
        assert_reserve=0,
        assert_soc_target_array=[10, 100],
        assert_immediate_soc_target_array=[10, 40],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes_array=[my_predbat.minutes_now + 60, -1],
        soc_kw_array=[0, 2],
        assert_reserve_array=[0, 0],
        assert_immediate_charge_soc_freeze_array=[False, True],
    )
    if failed:
        return failed

    # Charge freeze, one inverter is empty and the other at 40%
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable_array=[False, True],
        soc_kw=2,
        assert_pause_discharge_array=[True, False],
        assert_status="Charging",
        assert_soc_target_array=[100, 10],
        assert_immediate_soc_target_array=[40, 10],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_reserve_array=[0, 0],
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
        assert_charge_time_enable_array=[False, True],
        soc_kw=0.75,
        assert_pause_discharge_array=[True, False],
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target_array=[100, 10],
        assert_immediate_soc_target_array=[10, 10],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes_array=[-1, my_predbat.minutes_now + 60],
        soc_kw_array=[0.5, 0.25],
    )
    if failed:
        return failed

    # One inverter below the reserve, the other above it, re-balance at 10%
    # Headline status shows "Charging" - the most active state present (#4446) - since inverter 0
    # genuinely is still charging toward the target, not just whichever inverter was processed last.
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb4",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable_array=[True, False],
        soc_kw=1,
        assert_pause_discharge_array=[False, True],
        assert_status="Charging",
        assert_status_extra=" target Charging 5%-10% / Freeze charging 15%",
        assert_reserve_array=[0, 0],
        assert_soc_target_array=[10, 100],
        assert_immediate_soc_target_array=[10, 15],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes_array=[my_predbat.minutes_now + 60, -1],
        soc_kw_array=[0.25, 0.75],
        assert_immediate_charge_soc_freeze_array=[False, True],
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
    # GEC (GivEnergy Cloud) inverters use an immediate ECO toggle switch - ECO mode must NOT be turned off
    # before the export window actually starts, otherwise the battery stops supplying demand load early.
    # With inv_has_ge_eco_toggle=True the pre-window adjust_force_export call should pass force_export=False.
    failed |= run_execute_test(
        my_predbat,
        "discharge_upcoming_ge_eco_prewindow",
        export_window_best=export_window_best3,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        has_ge_eco_toggle=True,
        assert_force_export=False,
        assert_status="Demand",
        assert_discharge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 90 + 1,
    )
    # Confirm the existing behaviour for standard inverters (inv_has_ge_eco_toggle=False) is unchanged:
    # force_export=True is still passed to pre-program the discharge window.
    failed |= run_execute_test(
        my_predbat,
        "discharge_upcoming_std_prewindow",
        export_window_best=export_window_best3,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_force_export=True,
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
        # A fleet acting in unison must not repeat the headline status on every segment (v8.48.4 regression)
        assert_status_extra=" target 100%-0% / 100%-0%",
        # set_charge_window and set_export_window both true, actively exporting: this is the
        # combination that used to also fire an extra adjust_charge_immediate(0) call after the
        # export was started, clobbering a shared mode-select entity back off on service-template
        # inverters such as Tesla (GH#4165, GH#4641). No assert_immediate_charge_soc_target_array
        # here means it defaults to "untouched" (-1) for an "Exporting" status - i.e.
        # adjust_charge_immediate() must not have been called at all this cycle.
        car_slot=charge_window_best_slot,
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
        "discharge_car_full_bat2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Exporting",
        car_slot=charge_window_best_slot,
        car_charging_from_battery=False,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    # Single-inverter status text (v8.48.4 regression): every other case in this module runs two
    # inverters, so the single-inverter rendering had no end-to-end coverage at all and shipped
    # showing the headline status twice - "Exporting target Exporting 19%-5%".
    two_inverters = my_predbat.inverters
    my_predbat.inverters = [two_inverters[0]]
    my_predbat.args["num_inverters"] = 1
    failed |= run_execute_test(
        my_predbat,
        "single_inverter_export_status",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Exporting",
        assert_status_extra=" target 100%-0%",
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_immediate_soc_target=0,
    )
    my_predbat.inverters = two_inverters
    my_predbat.args["num_inverters"] = 2
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

    # Discharge-hold applies regardless of car_energy_reported_load; that switch controls whether EV
    # energy is included in the CT-clamp house-load model, not whether we enforce the discharge hold.
    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car_demand1b",
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Hold for car",
        assert_pause_discharge=True,
        car_slot=charge_window_best_slot,
        assert_immediate_soc_target=100,
        car_charging_from_battery=False,
        car_energy_reported_load=False,
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

    # Test inverter_limit_export: discharge rate during forced export should be capped to battery_max_export_rate (500) not battery_max_rate (1000)
    failed |= run_execute_test(
        my_predbat,
        "discharge_limit_export",
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
        battery_max_rate=1000,
        battery_max_export_rate=500,
        assert_discharge_rate=500,
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
    # #3899: holding the car off the battery must not ratchet reserve while the battery is actually
    # charging - it is filling from the grid, so it cannot be feeding the car, and tracking a rising
    # SoC costs a register write per 1%. Reserve resets for the duration instead; the "car2" scenario
    # above covers it being latched at SoC+1 once charging is no longer running.
    failed |= run_execute_test(
        my_predbat,
        "car_charge_no_reserve_ratchet",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        soc_kw=5,
        car_slot=charge_window_best_slot,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging, Hold for car",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_immediate_soc_target=100,
        has_timed_pause=False,
        assert_pause_discharge=False,
        assert_discharge_rate=0,
        assert_reserve=0,
    )
    failed |= run_execute_test(
        my_predbat,
        "car_charge2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        soc_kw=10,
        car_slot=charge_window_best_slot,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging, Hold for car",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=-1,
        assert_immediate_soc_target=50,
        assert_soc_target=100,
        assert_reserve=51,
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

    # iBoost hold without timed pause (lines 476-478): discharge rate set to 0 instead of pause
    my_predbat.iboost_enable = True
    my_predbat.iboost_prevent_discharge = True
    my_predbat.iboost_running_full = True
    failed |= run_execute_test(
        my_predbat,
        "no_charge_iboost_no_pause",
        set_charge_window=True,
        set_export_window=True,
        has_timed_pause=False,
        assert_status="Hold for iBoost",
        soc_kw=1,
        assert_immediate_soc_target=10,
        assert_pause_discharge=False,
        assert_discharge_rate=0,
        assert_reserve=11,  # soc_percent=10, min(10+1, 100)=11
    )
    my_predbat.iboost_enable = False
    my_predbat.iboost_prevent_discharge = False
    my_predbat.iboost_running_full = False
    if failed:
        return failed

    # set_freeze_export_during_demand tests (lines 419-433): demand mode prevents charging
    my_predbat.set_freeze_export_during_demand = True

    # Sub-test: inv_charge_discharge_with_rate=True + inv_has_timed_pause=True (lines 423-428)
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = True
    failed |= run_execute_test(
        my_predbat,
        "freeze_export_demand_with_rate",
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand [Freeze exporting]",
        assert_charge_rate=0,
        assert_pause_charge=True,
        assert_discharge_rate=1000,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = False
    if failed:
        return failed

    # Sub-test: inv_charge_discharge_with_rate=False + inv_has_timed_pause=True (lines 426-428)
    failed |= run_execute_test(
        my_predbat,
        "freeze_export_demand_timed_pause",
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand [Freeze exporting]",
        assert_charge_rate=1000,
        assert_pause_charge=True,
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    # Sub-test: inv_charge_discharge_with_rate=False + inv_has_timed_pause=False (lines 429-431)
    failed |= run_execute_test(
        my_predbat,
        "freeze_export_demand_no_pause",
        set_charge_window=True,
        set_export_window=True,
        has_timed_pause=False,
        assert_status="Demand [Freeze exporting]",
        assert_charge_rate=0,
        assert_pause_charge=False,
        assert_discharge_rate=1000,
    )

    my_predbat.set_freeze_export_during_demand = False
    if failed:
        return failed

    # Export with inv_has_discharge_enable_time=False (lines 505-506): soc_target set to 0
    for inverter in my_predbat.inverters:
        inverter.inv_has_discharge_enable_time = False
    failed |= run_execute_test(
        my_predbat,
        "export_no_discharge_enable_time",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_status="Exporting",
        assert_soc_target=0,
        assert_immediate_soc_target=0,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_has_discharge_enable_time = True
    if failed:
        return failed

    # Export with inverter_hybrid=True (lines 511-513): soc_target set to 0 (no AC-coupled reset)
    failed |= run_execute_test(
        my_predbat,
        "export_hybrid",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        inverter_hybrid=True,
        assert_status="Exporting",
        assert_soc_target=0,
        assert_immediate_soc_target=0,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    # Charge freeze approaching, not yet charging, inv_has_target_soc=False (line 534-536)
    # Pre-set charge_end_time_minutes so the approaching-window SOC path is entered even though
    # disable_charge_window() (not adjust_charge_window()) is called at the approaching-window step
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_approaching_no_target_soc",
        charge_window_best=charge_window_best_soon,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        has_target_soc=False,
        inverter_charge_time_minutes_end=my_predbat.minutes_now + 60,
        assert_status="Demand",
        assert_charge_time_enable=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    # Charge freeze approaching, not yet charging, inv_has_target_soc=True (lines 537-540): hold at 100%
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_approaching_with_target_soc",
        charge_window_best=charge_window_best_soon,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        has_target_soc=True,
        inverter_charge_time_minutes_end=my_predbat.minutes_now + 60,
        assert_status="Demand",
        assert_charge_time_enable=False,
        assert_soc_target=100,
    )
    if failed:
        return failed

    # Alert status append (lines 629-630)
    my_predbat.alert_active_keep = {my_predbat.minutes_now: 1}
    failed |= run_execute_test(
        my_predbat,
        "status_alert",
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand [Alert]",
    )
    my_predbat.alert_active_keep = {}
    if failed:
        return failed

    # Manual SoC status append (lines 631-632)
    my_predbat.manual_soc_keep = {my_predbat.minutes_now: 1}
    failed |= run_execute_test(
        my_predbat,
        "status_manual_soc",
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand [Manual SoC]",
    )
    my_predbat.manual_soc_keep = {}
    if failed:
        return failed

    # Export window overlaps charge window (line 317): discharge_start_time_minutes is in the past,
    # within the charge window range, so the export start is clamped to max(window start, minutes_now)
    for inverter in my_predbat.inverters:
        inverter.discharge_start_time_minutes = 100
    failed |= run_execute_test(
        my_predbat,
        "export_overlap_charge_window",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        inverter_charge_time_minutes_end=my_predbat.minutes_now + 60,
        assert_status="Exporting",
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    # Export window would wrap over 24 hours (lines 330-331): start time gets realigned to the
    # nearest plan_interval_minutes boundary rather than left far in the past
    export_window_best_long = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 1500, "average": 1}]
    for inverter in my_predbat.inverters:
        inverter.discharge_start_time_minutes = -1
    failed |= run_execute_test(
        my_predbat,
        "export_window_wrap_avoid",
        export_window_best=export_window_best_long,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 1500 + 1,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    # Export window end coincides with the charge window start (line 342): the 1 minute margin
    # normally added to the export end time is dropped to avoid overlapping the charge window
    for inverter in my_predbat.inverters:
        inverter.discharge_start_time_minutes = my_predbat.minutes_now + 1000
    failed |= run_execute_test(
        my_predbat,
        "export_overlap_charge_start",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        inverter_charge_time_minutes_start=my_predbat.minutes_now + 60,
        assert_status="Exporting",
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_immediate_soc_target=0,
    )
    for inverter in my_predbat.inverters:
        inverter.discharge_start_time_minutes = -1
    if failed:
        return failed

    # Register write counter feeds the Prometheus metric (line 613)
    my_predbat.inverters[0].count_register_writes = 1
    failed |= run_execute_test(
        my_predbat,
        "register_writes_metric",
        set_charge_window=True,
        set_export_window=True,
    )
    if failed:
        return failed

    # quick_inverter_data_update (lines 890-895): no-inverters guard and the happy path
    saved_inverters = my_predbat.inverters
    my_predbat.inverters = None
    if my_predbat.quick_inverter_data_update() is not False:
        print("ERROR: quick_inverter_data_update should return False when inverters is None")
        failed = True
    my_predbat.inverters = saved_inverters
    if failed:
        return failed

    if my_predbat.quick_inverter_data_update() is not True:
        print("ERROR: quick_inverter_data_update should return True")
        failed = True
    if failed:
        return failed

    # Template mode (#4965): update_pred() early-returns before fetch_config_options(), so the
    # attributes update_status() reads were never created - the quick update must skip rather
    # than AttributeError every 120 seconds. Deleting the attribute reproduces that state.
    saved_template = my_predbat.args.get("template")
    had_skew = "inverter_clock_skew_discharge_start" in my_predbat.__dict__
    saved_skew = my_predbat.__dict__.pop("inverter_clock_skew_discharge_start", None)
    my_predbat.args["template"] = True
    try:
        result = my_predbat.quick_inverter_data_update()
    finally:
        if had_skew:
            my_predbat.inverter_clock_skew_discharge_start = saved_skew
        if saved_template is None:
            my_predbat.args.pop("template", None)
        else:
            my_predbat.args["template"] = saved_template
    if result is not False:
        print("ERROR: quick_inverter_data_update should return False in template mode")
        failed = True
    if failed:
        return failed

    failed |= test_freeze_flags_do_not_leak_between_scenarios(my_predbat)
    if failed:
        return failed

    failed |= test_quick_poll_rebalance_guards(my_predbat)
    if failed:
        return failed

    failed |= test_stored_intent_is_the_executor_baseline(my_predbat)
    if failed:
        return failed

    failed |= test_balance_holds_do_not_raise_notifications(my_predbat)
    if failed:
        return failed

    failed |= test_balance_release_is_also_silent(my_predbat)
    if failed:
        return failed

    failed |= test_poll_clamps_stored_rates_to_refreshed_ceilings(my_predbat)
    if failed:
        return failed

    failed |= test_read_only_mode_writes_no_rates(my_predbat)
    if failed:
        return failed

    failed |= test_monitor_mode_writes_no_rates(my_predbat)
    if failed:
        return failed

    failed |= test_a_claimed_rate_is_written_even_when_rates_are_not_reset(my_predbat)
    if failed:
        return failed

    failed |= test_control_soc_only_sets_targets_without_writing_rates(my_predbat)
    if failed:
        return failed

    failed |= test_poll_does_not_apply_stale_intent_during_calibration(my_predbat)
    if failed:
        return failed

    failed |= test_calibration_discards_intent_collected_so_far(my_predbat)
    if failed:
        return failed

    failed |= test_crosscharge_is_prevented_through_execute_plan(my_predbat)
    if failed:
        return failed

    failed |= test_crosscharge_is_prevented_through_the_inverter_poll(my_predbat)
    if failed:
        return failed

    failed |= test_pv_surplus_holds_the_discharger_not_the_chargers(my_predbat)
    if failed:
        return failed

    failed |= test_executor_rate_survives_a_balance_hold(my_predbat)
    if failed:
        return failed

    failed |= test_discharge_balancing_through_execute_plan(my_predbat)
    if failed:
        return failed

    failed |= test_charge_balancing_through_execute_plan(my_predbat)
    if failed:
        return failed

    # Mixed fleet exporting to empty at 70% of fleet max. With a target of 0 the per-inverter
    # needs are the raw SoCs (9:1), which is NOT proportional to the rate ceilings (2600:1000) -
    # so the allocation genuinely differs from uniform scaling. The fuller inverter does more of
    # the work, and the fleet still delivers exactly the power the planner costed.
    export_window_best_alloc = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 20.0}]
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_export_allocation",
        export_window_best=export_window_best_alloc,
        export_limits_best=[pack_export_limit(EXPORT_MODE_TARGET, 0, 0.7)],
        assert_force_export=True,
        soc_kw=10.0,
        soc_kw_array=[9.0, 1.0],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        battery_max_rate_array=[2600, 1000],
        set_charge_window=True,
        set_export_window=True,
        set_export_low_power=True,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        # Uniform scaling would give [1820, 700]. Allocating by need instead sends the fuller
        # inverter to 2268W and the emptier one to 252W - and 2268 + 252 = 2520 = 3600W * 0.7,
        # exactly the fleet power the planner costed.
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_discharge_rate_array=[2268, 252],
        assert_charge_rate_array=[2600, 1000],
    )
    if failed:
        return failed

    # Same heterogeneous fleet exporting at FULL power. The planned fleet power then equals the sum
    # of the ceilings, so every inverter clamps at its own maximum and the allocation collapses to
    # today's uniform scaling exactly. That provable no-op outside low power mode is what lets the
    # allocator ship without a switch, so it is pinned here rather than left as an argument.
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_export_full_rate_is_a_no_op",
        export_window_best=export_window_best_alloc,
        export_limits_best=[pack_export_limit(EXPORT_MODE_TARGET, 0, 1.0)],
        assert_force_export=True,
        soc_kw=10.0,
        soc_kw_array=[9.0, 1.0],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        battery_max_rate_array=[2600, 1000],
        set_charge_window=True,
        set_export_window=True,
        set_export_low_power=True,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_discharge_rate_array=[2600, 1000],
        assert_charge_rate_array=[2600, 1000],
    )
    if failed:
        return failed

    # An inverter with nothing left to shed takes none of the budget and its share spills to the
    # one that can still deliver, so the fleet keeps putting out the power the planner costed
    # instead of sagging as inverters reach target one by one.
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_export_at_target_spills",
        export_window_best=export_window_best_alloc,
        export_limits_best=[pack_export_limit(EXPORT_MODE_TARGET, 0, 0.7)],
        assert_force_export=True,
        soc_kw=9.5,
        soc_kw_array=[9.5, 0.0],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        battery_max_rate_array=[2600, 1000],
        set_charge_window=True,
        set_export_window=True,
        set_export_low_power=True,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        # Inverter 1 is at its target and takes none of the budget; its share spills to inverter 0,
        # so the fleet still delivers 2520W = 3600W * 0.7, the power the planner costed. Uniform
        # scaling would have given [1820, 700] and the fleet would have sagged to 1820W once
        # inverter 1 hit target.
        assert_discharge_rate_array=[2520, 0],
        assert_charge_rate_array=[2600, 1000],
    )
    if failed:
        return failed

    # Mixed fleet: 9.5kWh/2600W alongside 5.2kWh/1500W, one with timed pause and one without.
    # Nothing planned, so each inverter resets to its OWN maximum rather than a shared one -
    # which is what the per-inverter arrays exist to express. Asserts the plumbing, not new behaviour.
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_idle",
        soc_kw=7.35,
        soc_kw_array=[4.75, 2.6],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        battery_max_rate_array=[2600, 1500],
        has_timed_pause_array=[True, False],
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_rate_array=[2600, 1500],
        assert_discharge_rate_array=[2600, 1500],
    )
    if failed:
        return failed

    # A freeze-charge hold on a mixed fleet: the inverter with timed pause holds via pause mode,
    # the one without expresses the same hold as discharge rate 0. That split is the F5 / #829
    # shape - with two writers the balancer's reset loop raised the zero back to max. This
    # characterises today's behaviour so the intent refactor has a regression guard.
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 5.0}]
    charge_limit_best = [my_predbat.reserve]
    failed |= run_execute_test(
        my_predbat,
        "mixed_fleet_freeze_hold",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        soc_kw=7.35,
        soc_kw_array=[4.75, 2.6],
        soc_max=14.7,
        soc_max_array=[9.5, 5.2],
        battery_max_rate=2600,
        has_timed_pause_array=[True, False],
        set_charge_window=True,
        set_export_window=True,
        set_discharge_during_charge=False,
        assert_status="Freeze charging",
        assert_pause_discharge_array=[True, False],
        assert_discharge_rate_array=[2600, 0],
        assert_charge_rate_array=[2600, 2600],
        assert_immediate_soc_target=50,
        # Inverter 0 holds via pause mode so its reserve is untouched; inverter 1 has no timed
        # pause, so the hold is carried by reserve at soc_percent + 1 instead.
        assert_reserve_array=[0, 51],
    )
    if failed:
        return failed

    return failed


def test_freeze_flags_do_not_leak_between_scenarios(my_predbat):
    """
    fetch_inverter_data() narrows the freeze capability flags for an inverter that doesn't support
    freeze and never widens them back. In production fetch_config_options() re-derives them from raw
    config every cycle, so the narrowing lasts one cycle; run_execute_test() calls
    fetch_inverter_data() directly, so without re-deriving them the narrowing would persist for the
    rest of the process and silently disable freeze in every later scenario.
    """
    print("**** Running test_freeze_flags_do_not_leak_between_scenarios ****")
    failed = False
    # These run here rather than at the end of the module: the last scenario leaves my_predbat.isCharging
    # set, and tests further down the registry (test_optimise_all_windows' charging-skew check) read it.
    # set_charge_freeze_only is a safeguard as well as a planning restriction: a charge limit above
    # the reserve can still reach execution from a plan built before the switch was turned on, and
    # the battery must not be charged from the grid when it does.
    charge_window_freeze_only = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_only_off_grid_charges",
        charge_window_best=charge_window_freeze_only,
        charge_limit_best=[10],
        set_charge_window=True,
        set_export_window=True,
        soc_kw=1,
        assert_charge_time_enable=True,
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    # Same plan, switch on - must behave exactly like the charge_freeze1c freeze scenario above
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_only_on_clamps_to_freeze",
        charge_window_best=charge_window_freeze_only,
        charge_limit_best=[10],
        set_charge_window=True,
        set_export_window=True,
        soc_kw=1,
        set_charge_freeze_only=True,
        assert_charge_time_enable=False,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=10,
    )
    if failed:
        return failed

    # Turning the switch back off restores grid charging - the clamp must not be sticky
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_only_off_again_grid_charges",
        charge_window_best=charge_window_freeze_only,
        charge_limit_best=[10],
        set_charge_window=True,
        set_export_window=True,
        soc_kw=1,
        assert_charge_time_enable=True,
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    saved = [(inverter.inv_support_charge_freeze, inverter.inv_support_discharge_freeze) for inverter in my_predbat.inverters]

    for inverter in my_predbat.inverters:
        inverter.inv_support_charge_freeze = False
        inverter.inv_support_discharge_freeze = False
    run_execute_test(my_predbat, "freeze_unsupported_inverter", set_charge_window=True, set_export_window=True)

    for inverter, (support_charge, support_discharge) in zip(my_predbat.inverters, saved):
        inverter.inv_support_charge_freeze = support_charge
        inverter.inv_support_discharge_freeze = support_discharge
    run_execute_test(my_predbat, "freeze_supported_inverter_after", set_charge_window=True, set_export_window=True)

    for name in ("set_charge_freeze", "set_export_freeze", "set_export_freeze_only", "set_charge_freeze_only"):
        expected = my_predbat.get_arg(name)
        actual = getattr(my_predbat, name)
        if actual != expected:
            print("ERROR: {} leaked from the previous scenario - is {} but config says {}".format(name, actual, expected))
            failed = True

    return failed

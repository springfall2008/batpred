# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""Tests for the shared rules on which car slots an export may cancel."""

import contextlib
import copy
import io
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from car_cancel import CAR_CANCEL_MAX_SLOTS, CancelSlot, build_car_cancel_table, cancellable_slots, cancelled_mask, cancelled_windows, step_reverted
from const import EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10
from prediction import Prediction
from utils import export_mode_of, pack_export_limit, parse_car_plan_windows
from tests.test_infra import run_async
from tests.test_car_charger_control import FakeCharger, FakeComponent
from tests.test_kernel_parity import RESULT_NAMES, make_step_data


def _slot(start, end, kwh=1.0):
    """A car charging slot."""
    return {"start": start, "end": end, "kwh": kwh, "octopus": False}


def test_cancellable_slots_rules():
    """Only Predbat-led and holdable cars qualify, and not empty, past or charging-now slots."""
    model = [
        [_slot(0, 30), _slot(60, 90), _slot(120, 150, 0), _slot(150, 180)],  # car 0: index 0 is a charging-now slot
        [_slot(60, 90)],  # car 1 is neither
        [_slot(60, 90), _slot(30, 60)],  # car 2 is holdable
    ]
    slots = cancellable_slots(model, [1, 0, 0], {0, 2}, {}, 60)
    got = [(s.car_n, s.index, s.start, s.end) for s in slots]
    # car 0 index 0 (now slot), index 2 (0 kWh); car 2 index 1 ends at minutes_now
    assert got == [(0, 1, 60, 90), (2, 0, 60, 90), (0, 3, 150, 180)], got
    assert not any(s.forced for s in slots)


def test_cancellable_slots_cap():
    """The list is capped, keeping the earliest slots."""
    model = [[_slot(i * 30, i * 30 + 30) for i in range(70)]]
    slots = cancellable_slots(model, [0], {0}, {}, 0)
    assert len(slots) == CAR_CANCEL_MAX_SLOTS
    assert slots[-1].start == 63 * 30
    assert all(s.start != 64 * 30 for s in slots)


def test_cancellable_slots_forced():
    """Slots starting before the hold ends are forced, and a held car need not be cancellable."""
    model = [[_slot(30, 60), _slot(60, 90), _slot(90, 120)]]
    slots = cancellable_slots(model, [0], {0}, {0: 90}, 0)
    assert [s.forced for s in slots] == [True, True, False], slots
    slots = cancellable_slots(model, [0], set(), {0: 61}, 0)
    assert [s.forced for s in slots] == [True, True, False], slots
    assert len(slots) == 3
    assert cancellable_slots(model, [0], set(), {}, 0) == []


def test_cancelled_mask():
    """Target windows cancel what they overlap; freeze, idle and non-overlapping windows do not."""
    slots = [CancelSlot(0, 0, 60, 90, False), CancelSlot(0, 1, 120, 150, True), CancelSlot(0, 2, 180, 210, False)]
    target = pack_export_limit(EXPORT_MODE_TARGET, 10)
    # One minute of overlap with slot 0
    assert cancelled_mask(slots, [{"start": 30, "end": 61}], [target]) == 0b010 | 0b001
    # Ends exactly at slot start: nothing but the forced slot
    assert cancelled_mask(slots, [{"start": 30, "end": 60}], [target]) == 0b010
    # Starts exactly at slot end
    assert cancelled_mask(slots, [{"start": 90, "end": 120}], [target]) == 0b010
    for mode in (EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE):
        limit = pack_export_limit(mode)
        assert cancelled_mask(slots, [{"start": 60, "end": 90}, {"start": 180, "end": 210}], [limit, limit]) == 0b010
    assert cancelled_mask(slots, [], []) == 0b010


def test_cancelled_windows():
    """A mask groups the chosen slots by car, time-ordered."""
    slots = [CancelSlot(1, 0, 60, 90, False), CancelSlot(0, 0, 90, 120, False), CancelSlot(1, 1, 120, 150, False)]
    assert cancelled_windows(slots, 0b101) == {1: [(60, 90), (120, 150)]}
    assert cancelled_windows(slots, 0b110) == {0: [(90, 120)], 1: [(120, 150)]}
    assert cancelled_windows(slots, 0) == {}


def _set_switch(my_predbat, value):
    """Set the cancel switch the way the UI does."""
    my_predbat.expose_config("car_charging_cancel_for_export", value, force=True)


def test_car_cancellable_cars(my_predbat):
    """Only with the switch on, and then Predbat-led plus holdable cars."""
    names = ("num_cars", "car_planned_by_predbat", "car_force_holdable")
    saved = {name: getattr(my_predbat, name) for name in names}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        my_predbat.num_cars = 2
        my_predbat.car_planned_by_predbat = {0}
        my_predbat.car_force_holdable = {1: "Fake"}
        _set_switch(my_predbat, False)
        assert my_predbat.car_cancellable_cars() == set()
        assert my_predbat.car_cancel_slots() == []
        _set_switch(my_predbat, True)
        assert my_predbat.car_cancellable_cars() == {0, 1}
        my_predbat.num_cars = 0
        assert not my_predbat.car_cancel_enabled()
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_car_cancel_slots(my_predbat):
    """The PredBat wrapper puts now slots ahead, and converts the hold end to a minute."""
    names = ("num_cars", "car_charging_slots", "car_charging_now_slots", "car_planned_by_predbat", "car_force_holdable", "car_force_hold_until", "minutes_now")
    saved = {name: getattr(my_predbat, name) for name in names}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        _set_switch(my_predbat, True)
        my_predbat.num_cars = 1
        my_predbat.minutes_now = 60
        my_predbat.car_charging_now_slots = [[_slot(60, 90)]]
        my_predbat.car_charging_slots = [[_slot(60, 90), _slot(120, 150), _slot(180, 210)]]
        my_predbat.car_planned_by_predbat = {0}
        my_predbat.car_force_holdable = {}
        # Held until 02:30:30 after midnight: ceil gives minute 151 so the slots at 60 and 120 are forced
        my_predbat.car_force_hold_until = {0: my_predbat.midnight_utc + timedelta(minutes=150, seconds=30)}
        slots = my_predbat.car_cancel_slots()
        assert [(s.index, s.start, s.forced) for s in slots] == [(1, 60, True), (2, 120, True), (3, 180, False)], slots
        # A hold that has ended is dropped
        my_predbat.car_force_hold_until = {0: my_predbat.midnight_utc + timedelta(minutes=60)}
        assert not any(s.forced for s in my_predbat.car_cancel_slots())
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


GATE_NAMES = ("num_cars", "car_charging_slots", "car_charging_now_slots", "car_planned_by_predbat", "car_force_holdable", "car_force_hold_until", "minutes_now", "car_cancel_keys", "car_charging_from_battery", "export_window_best", "calculate_best_export", "calculate_export_oncharge", "manual_all_times", "iboost_enable")


def _gate_setup(my_predbat, slots, planned, switch=True):
    """Set up cars, car slots and one export window over minutes 60 to 90 and fill in the cancel keys as calculate_plan does."""
    my_predbat.num_cars = 1
    my_predbat.minutes_now = 0
    my_predbat.car_charging_now_slots = [[]]
    my_predbat.car_charging_slots = [slots]
    my_predbat.car_planned_by_predbat = {0} if planned else set()
    my_predbat.car_force_holdable = {}
    my_predbat.car_force_hold_until = {}
    my_predbat.car_charging_from_battery = False
    my_predbat.export_window_best = [{"start": 60, "end": 90, "average": 10.0}]
    my_predbat.calculate_best_export = True
    my_predbat.calculate_export_oncharge = True
    my_predbat.manual_all_times = []
    my_predbat.iboost_enable = False
    _set_switch(my_predbat, switch)
    my_predbat.car_cancel_keys = frozenset((s.car_n, s.index) for s in my_predbat.car_cancel_slots())


def _gate_run(my_predbat, slots, planned, switch=True):
    """Return allow_this_export_window(0) for the setup, restoring the fixture afterwards."""
    saved = {name: getattr(my_predbat, name) for name in GATE_NAMES}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        _gate_setup(my_predbat, slots, planned, switch)
        return my_predbat.allow_this_export_window(0)
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_gate_relaxed_for_cancellable(my_predbat):
    """A Predbat-led car's slot no longer blocks an export window."""
    assert _gate_run(my_predbat, [_slot(60, 90)], True)


def test_gate_blocked_for_uncancellable(my_predbat):
    """A car that is neither Predbat-led nor holdable still blocks the export window."""
    assert not _gate_run(my_predbat, [_slot(60, 90)], False)


def test_gate_switch_off_unchanged(my_predbat):
    """With the switch off the car slot blocks the window, as before."""
    assert not _gate_run(my_predbat, [_slot(60, 90)], True, switch=False)


def test_gate_mixed_slots(my_predbat):
    """A window over a cancellable and an uncancellable slot stays blocked."""
    saved = {name: getattr(my_predbat, name) for name in GATE_NAMES}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        _gate_setup(my_predbat, [_slot(60, 75), _slot(75, 90)], True)
        # The second slot is not in the cancel set (as if it had no energy left)
        my_predbat.car_cancel_keys = frozenset(k for k in my_predbat.car_cancel_keys if k[1] == 0)
        assert len(my_predbat.car_cancel_keys) == 1
        assert not my_predbat.allow_this_export_window(0)
        assert my_predbat.hit_uncancellable_car_window(60, 90) is True
        assert my_predbat.hit_uncancellable_car_window(60, 75) is False
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_gate_cap(my_predbat):
    """A window over the 65th cancellable slot is blocked, as only 64 are cancellable."""
    slots = [_slot(i * 30, i * 30 + 30) for i in range(CAR_CANCEL_MAX_SLOTS + 1)]
    saved = {name: getattr(my_predbat, name) for name in GATE_NAMES}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        _gate_setup(my_predbat, slots, True)
        assert len(my_predbat.car_cancel_keys) == CAR_CANCEL_MAX_SLOTS
        my_predbat.export_window_best = [{"start": 63 * 30, "end": 64 * 30, "average": 10.0}]
        assert my_predbat.allow_this_export_window(0)
        my_predbat.export_window_best = [{"start": 64 * 30, "end": 65 * 30, "average": 10.0}]
        assert not my_predbat.allow_this_export_window(0)
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_levels_and_freeze_unchanged(my_predbat):
    """hit_car_window, used by the levels pass and freeze export, still sees a cancellable slot."""
    saved = {name: getattr(my_predbat, name) for name in GATE_NAMES}
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        _gate_setup(my_predbat, [_slot(60, 90)], True)
        assert my_predbat.hit_car_window(60, 90)
        assert not my_predbat.hit_uncancellable_car_window(60, 90)
        my_predbat.car_cancel_keys = frozenset()
        assert my_predbat.hit_uncancellable_car_window(60, 90) == my_predbat.hit_car_window(60, 90)
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_force_hold_tick_publishes_holdable():
    """The component publishes the cars it can hold, before its early return, and removes only its own."""
    component = FakeComponent([FakeCharger("a")])
    name = component.charger_control_log_name
    base = component.base
    base.car_force_holdable = {}
    now = None
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {0: name}, base.car_force_holdable
    component.args["car_charging_cancel_for_export"] = False
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {}, base.car_force_holdable
    # Another component's entry is left alone
    base.car_force_holdable = {0: "Other"}
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {0: "Other"}
    component.args["car_charging_cancel_for_export"] = True
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {0: name}, "the switch on takes the car over"
    base.car_force_holdable = {0: name}
    component.chargers[0].can_force = False
    run_async(component.charger_control_force_hold_tick(now))
    assert base.car_force_holdable == {}


# ---------------------------------------------------------------------------
# The cancel table and the Python engine
# ---------------------------------------------------------------------------

PEAK = 30.0  # rate_max_base, the dearest base import rate
OFF_PEAK = 7.0  # A dispatch's rate, and the car slot's own
MINUTES_NOW = 12 * 60
HORIZON = 24 * 60
LOAD_KW = 0.5
CAR_KWH = 10.0
TARGET_EMPTY = pack_export_limit(EXPORT_MODE_TARGET, 0)


def _car(start, end, kwh=CAR_KWH, octopus=True):
    """A car slot at the off-peak rate; an Octopus dispatch unless octopus is False."""
    return {"start": start, "end": end, "kwh": kwh, "average": OFF_PEAK, "cost": OFF_PEAK * kwh, "octopus": octopus}


def _scenario_values(num_cars, soc_kw):
    """Every attribute the Python engine and the cancel table read, pinned so no earlier test's residue moves the results."""
    return {
        "minutes_now": MINUTES_NOW,
        "forecast_minutes": HORIZON,
        "end_record": HORIZON,
        "soc_max": 10.0,
        "soc_kw": soc_kw,
        "reserve": 0.0,
        "best_soc_min": 0.0,
        "best_soc_keep": 0.0,
        "best_soc_keep_weight": 0.5,
        "battery_rate_max_charge": 3 / 60.0,
        "battery_rate_max_charge_dc": 3 / 60.0,
        "battery_rate_max_discharge": 3 / 60.0,
        "battery_rate_max_export": 3 / 60.0,
        "battery_rate_min": 0.0,
        "charge_rate_now": 3 / 60.0,
        "discharge_rate_now": 3 / 60.0,
        "battery_rate_max_scaling": 1.0,
        "battery_rate_max_scaling_discharge": 1.0,
        "charge_scaling10": 1.0,
        "battery_loss": 1.0,
        "battery_loss_discharge": 1.0,
        "inverter_hybrid": False,
        "inverter_loss": 1.0,
        "inverter_freeze_export_discharge_rate": 0.0,
        "inverter_limit": 5 / 60.0,
        "export_limit": 5 / 60.0,
        "pv_ac_limit": 0.0,
        "inverter_can_charge_during_export": True,
        "inverter_support_feedin_first": False,
        "set_charge_freeze": False,
        "set_reserve_enable": False,
        "set_export_freeze": False,
        "set_export_freeze_only": False,
        "set_charge_window": True,
        "set_export_window": True,
        "set_discharge_during_charge": True,
        "set_export_low_power": False,
        "set_charge_low_power": False,
        "set_read_only": False,
        "calculate_export_on_pv": False,
        "battery_charge_power_curve": {},
        "battery_discharge_power_curve": {},
        "battery_temperature": 20,
        "battery_temperature_prediction": {},
        "battery_temperature_charge_curve": {},
        "battery_temperature_discharge_curve": {},
        "rate_import": {minute: PEAK for minute in range(MINUTES_NOW + HORIZON)},
        "rate_export": {minute: 0.0 for minute in range(MINUTES_NOW + HORIZON)},
        "rate_max": PEAK,
        "rate_max_base": PEAK,
        "rate_import_tariff_max": PEAK,
        "rate_import_saving_minutes": set(),
        "io_adjusted": {},
        "io_confirmed_credit": {},
        "all_active_keep": {},
        "all_active_keep_max": {},
        "carbon_enable": False,
        "carbon_intensity": {},
        "carbon_today_sofar": 0,
        "cost_today_sofar": 0,
        "metric_standing_charge": 0,
        "export_today_now": 0,
        "import_today_now": 0,
        "load_minutes_now": 0,
        "pv_today_now": 0,
        "num_cars": num_cars,
        "car_charging_loss": 1.0,
        "car_energy_reported_load": True,
        "car_charging_from_battery": False,
        "car_charging_soc": [0.0] * num_cars,
        "car_charging_soc_next": [None] * num_cars,
        "car_charging_limit": [100.0] * num_cars,
        "car_charging_limit_model": None,
        "car_charging_battery_size": [100.0] * num_cars,
        "car_charging_slots": [[] for _ in range(num_cars)],
        "car_charging_now_slots": [[] for _ in range(num_cars)],
        "car_planned_by_predbat": {0},
        "car_force_holdable": {},
        "car_force_hold_until": {},
        "iboost_enable": False,
        "iboost_next": 0,
        "iboost_today": 0,
        "iboost_plan": [],
        "rate_gas": {},
        "debug_enable": False,
        "prediction_kernel_enable": False,
        "prediction_cache_enable": False,
    }


@contextlib.contextmanager
def _engine_scenario(my_predbat, num_cars=1, soc_kw=0.0, switch=True, billing="octopus"):
    """
    A flat 0.5 kW load, no PV and peak import everywhere, with the switch and the billing rule as given and the
    Python engine forced (the parity test compares the kernel against it). Restores the fixture afterwards.
    """
    values = _scenario_values(num_cars, soc_kw)
    saved = {name: copy.deepcopy(getattr(my_predbat, name)) for name in values if hasattr(my_predbat, name)}
    missing = [name for name in values if not hasattr(my_predbat, name)]
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    had_billing = "intelligent_dispatch_billing" in my_predbat.args
    saved_billing = my_predbat.args.get("intelligent_dispatch_billing")
    try:
        for name, value in values.items():
            setattr(my_predbat, name, value)
        my_predbat.args["intelligent_dispatch_billing"] = billing
        _set_switch(my_predbat, switch)
        yield
    finally:
        _set_switch(my_predbat, saved_switch)
        if had_billing:
            my_predbat.args["intelligent_dispatch_billing"] = saved_billing
        else:
            my_predbat.args.pop("intelligent_dispatch_billing", None)
        for name, value in saved.items():
            setattr(my_predbat, name, value)
        for name in missing:
            delattr(my_predbat, name)


def _dispatch_rates(my_predbat, start, end):
    """Make start to end (rounded out to half hours) a dispatch: off-peak and io_adjusted, as the rate feed delivers it."""
    first = (start // 30) * 30
    last = ((end + 29) // 30) * 30
    for minute in range(first, last):
        my_predbat.rate_import[minute] = OFF_PEAK
        my_predbat.io_adjusted[minute] = True


def _predict(my_predbat, export_window, export_limits=None, pv_scenario=PV_SCENARIO_NOMINAL, with_table=True):
    """Run the Python engine on the fixture's car slots, with the cancel table the live plan would build."""
    model = my_predbat.car_charging_slots_model()
    table = build_car_cancel_table(my_predbat, model, my_predbat.car_cancel_slots()) if with_table else None
    pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, pv_kw=0.0, load_kw=LOAD_KW)
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step, car_charging_slots=model, car_cancel=table)
    assert prediction.kernel_handle == 0, "the Python engine must run"
    if export_limits is None:
        export_limits = [TARGET_EMPTY] * len(export_window)
    result = prediction.run_prediction([], [], export_window, export_limits, pv_scenario, HORIZON)
    return result, table


def _hand_cost(peak_hours, off_peak_hours, car_kwh_off_peak=0.0):
    """The cost of the flat house load over the horizon, split between peak and off-peak hours, plus car energy at off-peak."""
    assert abs(peak_hours + off_peak_hours - HORIZON / 60) < 1e-9
    return LOAD_KW * peak_hours * PEAK + LOAD_KW * off_peak_hours * OFF_PEAK + car_kwh_off_peak * OFF_PEAK


def _assert_close(name, got, want, tolerance=0.01):
    """Fail with both values when they differ."""
    assert abs(got - want) < tolerance, "{}: got {} want {}".format(name, got, want)


def test_revert_outside_offpeak(my_predbat):
    """A cancelled dispatch at 18:00 outside the fixed window pays rate_max_base."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1080, 1110)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        result, table = _predict(my_predbat, [{"start": 1080, "end": 1110}])
        k = (1080 - MINUTES_NOW) // 5
        assert step_reverted(table, k, 1)
        assert not step_reverted(table, k, 0), "not cancelled, not reverted"
        # Every half hour at peak, and the car buys nothing
        _assert_close("cost", result[0], _hand_cost(24, 0))


def test_revert_inside_offpeak(my_predbat):
    """A cancelled dispatch at 00:00, inside the fixed 23:30-05:30 window, keeps its 7p."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1440, 1470)]]
        _dispatch_rates(my_predbat, 1440, 1470)
        result, table = _predict(my_predbat, [{"start": 1440, "end": 1470}])
        assert table.revert_rate[(1440 - MINUTES_NOW) // 5] == -1.0
        _assert_close("cost", result[0], _hand_cost(23.5, 0.5))


def test_revert_other_car_trusted(my_predbat):
    """Another car's uncancellable dispatch over the same half hour keeps the 7p for everyone."""
    with _engine_scenario(my_predbat, num_cars=2):
        my_predbat.car_charging_slots = [[_car(1080, 1110)], [_car(1080, 1110, kwh=2.0)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        result, table = _predict(my_predbat, [{"start": 1080, "end": 1110}])
        assert table.trusted[(1080 - MINUTES_NOW) // 5] == 1
        assert [(slot.car_n, slot.index) for slot in table.slots] == [(0, 0)]
        # Car 1 still charges its 2 kWh at 7p
        _assert_close("cost", result[0], _hand_cost(23.5, 0.5, car_kwh_off_peak=2.0))


def test_revert_split_half_hour(my_predbat):
    """Two dispatches in one half hour: cancelling one leaves 7p, cancelling both reverts it."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1140, 1145, kwh=1.0), _car(1145, 1170, kwh=5.0)]]
        _dispatch_rates(my_predbat, 1140, 1170)
        result, table = _predict(my_predbat, [{"start": 1140, "end": 1145}])
        assert table.need_mask[(1140 - MINUTES_NOW) // 5] == 0b11
        # The second dispatch still charges its 5 kWh
        _assert_close("cost one cancelled", result[0], _hand_cost(23.5, 0.5, car_kwh_off_peak=5.0))
        result, _ = _predict(my_predbat, [{"start": 1140, "end": 1170}])
        _assert_close("cost both cancelled", result[0], _hand_cost(24, 0))


def test_revert_saving_session(my_predbat):
    """A saving session minute keeps its own rate."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1080, 1110)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        my_predbat.rate_import_saving_minutes = set(range(1080, 1110))
        result, _ = _predict(my_predbat, [{"start": 1080, "end": 1110}])
        _assert_close("cost", result[0], _hand_cost(23.5, 0.5))


def test_revert_window_billing(my_predbat):
    """Under window billing nothing reverts, but the car load and the penalty still go."""
    with _engine_scenario(my_predbat, billing="window"):
        my_predbat.car_charging_slots = [[_car(1080, 1110)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        result, table = _predict(my_predbat, [{"start": 1080, "end": 1110}])
        assert all(rate == -1.0 for rate in table.revert_rate)
        _assert_close("cost", result[0], _hand_cost(23.5, 0.5))
        _assert_close("metric_keep", result[8], CAR_KWH * PEAK)


def test_reverted_step_not_dispatch_gone(my_predbat):
    """In PV10 a reverted step pays rate_max_base, not the dispatch-gone worst case, and the car buys nothing."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1080, 1110)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        # The dispatch-gone worst case reads the tariff maximum; make it distinguishable from rate_max_base
        my_predbat.rate_import_tariff_max = 50.0
        result, _ = _predict(my_predbat, [{"start": 1080, "end": 1110}], pv_scenario=PV_SCENARIO_PV10)
        _assert_close("cost", result[0], _hand_cost(24, 0))
        _assert_close("imports", result[1] + result[2], LOAD_KW * 24)
        _assert_close("metric_keep", result[8], CAR_KWH * PEAK)


def test_reverted_step_no_io_credit(my_predbat):
    """A reverted step banks no confirmed-dispatch credit for what a charge window stores in it."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1080, 1110)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        my_predbat.io_confirmed_credit = {minute: 10.0 for minute in range(1080, 1110)}
        model = my_predbat.car_charging_slots_model()
        table = build_car_cancel_table(my_predbat, model, my_predbat.car_cancel_slots())
        pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, pv_kw=0.0, load_kw=LOAD_KW)
        prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step, car_charging_slots=model, car_cancel=table)
        # A short export cancels the whole slot; the charge window fills the rest of the half hour
        charge_window = [{"start": 1085, "end": 1110, "average": OFF_PEAK}]
        result = prediction.run_prediction([10.0], charge_window, [{"start": 1080, "end": 1085}], [TARGET_EMPTY], PV_SCENARIO_NOMINAL, HORIZON)
        assert result[1] > 1.0, "the charge window should store energy: {}".format(result)
        # Only the buy-back penalty, no credit
        _assert_close("metric_keep", result[8], CAR_KWH * PEAK)


def test_penalty_respects_car_full(my_predbat):
    """A car 1 kWh from its limit is charged the penalty on the 1 kWh it would have bought, not the slot's 10."""
    with _engine_scenario(my_predbat):
        my_predbat.car_charging_slots = [[_car(1080, 1110, octopus=False)]]
        my_predbat.car_charging_soc = [49.0]
        my_predbat.car_charging_limit = [50.0]
        my_predbat.car_charging_loss = 0.9
        result, _ = _predict(my_predbat, [{"start": 1080, "end": 1110}])
        _assert_close("metric_keep", result[8], 1.0 / 0.9 * PEAK)


def test_valuation_ready_time_not_free(my_predbat):
    """
    The slot ends at the car's ready time and every rate after it is 7p, but the penalty is the horizon's 30p peak,
    so cancelling is never free just because nothing dear is left before the car leaves.
    """
    with _engine_scenario(my_predbat):
        my_predbat.rate_import = {minute: (PEAK if 13 * 60 <= minute < 16 * 60 else OFF_PEAK) for minute in range(MINUTES_NOW + HORIZON)}
        my_predbat.car_charging_slots = [[_car(1080, 1110, octopus=False)]]
        window = [{"start": 1080, "end": 1110}]
        cancelled, table = _predict(my_predbat, window)
        assert table.penalty_rate == PEAK
        my_predbat.car_charging_slots = [[]]
        no_car, _ = _predict(my_predbat, window, with_table=False)
        _assert_close("metric_keep rise", cancelled[8] - no_car[8], CAR_KWH * PEAK)
        _assert_close("cost", cancelled[0], no_car[0])


# The switch-off results, captured on the Task 2 commit (6f27acf1) before the cancel table existed
SWITCH_OFF_NOMINAL = (158.75, 0, 12.0, 0.0, 0.0, 0.0, 1770, 10.0, 0.0, 0, 0)
SWITCH_OFF_PV10 = (158.75, 0, 12.0, 0.0, 0.0, 0.0, 1770, 10.0, 0.0, 0, 0)


def test_switch_off_identical(my_predbat):
    """With the switch off there is no table, and the engine gives exactly what it gave before the table existed."""
    with _engine_scenario(my_predbat, soc_kw=10.0, switch=False):
        my_predbat.car_charging_slots = [[_car(1080, 1110)]]
        _dispatch_rates(my_predbat, 1080, 1110)
        assert my_predbat.car_cancel_slots() == []
        window = [{"start": 1080, "end": 1110}]
        nominal, table = _predict(my_predbat, window)
        assert table is None
        pv10, _ = _predict(my_predbat, window, pv_scenario=PV_SCENARIO_PV10)
        got_nominal = tuple(nominal[:11])
        got_pv10 = tuple(pv10[:11])
        for name, got, want in (("nominal", got_nominal, SWITCH_OFF_NOMINAL), ("pv10", got_pv10, SWITCH_OFF_PV10)):
            assert got == want, "{} changed: {}".format(name, ", ".join("{} {} -> {}".format(field, old, new) for field, old, new in zip(RESULT_NAMES, want or [None] * 11, got) if old != new))


# ---------------------------------------------------------------------------
# End to end: does calculate_plan() cancel for a saving session and not for a normal export?
# ---------------------------------------------------------------------------

PLAN_SLOT = (18 * 60, 18 * 60 + 30)


def _plan_scenario(export_rate):
    """A one-car day in the random benchmark's format: the car's 10 kWh slot at 7p is under an export at export_rate."""
    slot_start, slot_end = PLAN_SLOT

    def import_rate(minute):
        """7p at night and in the car slot, 30p otherwise."""
        if minute >= 23 * 60 + 30 or minute < 5 * 60 + 30 or slot_start <= minute < slot_end:
            return OFF_PEAK
        return PEAK

    return {
        "id": 0,
        "seed": 0,
        "params": {
            "battery": {"soc_max_kwh": 20.0, "initial_soc_percent": 100.0, "rate_max_charge_kw": 10.0, "rate_max_discharge_kw": 10.0},
            "inverter": {"hybrid": False, "inverter_limit_kw": 10.0, "export_limit_kw": 10.0},
            "clock": {"minutes_now": MINUTES_NOW},
            "load_scaling": {"nominal": 1.0, "pv10": 1.0, "pv90": 1.0},
            "cars": {"num_cars": 1, "battery_kwh": [100.0], "slots": [[_car(slot_start, slot_end, octopus=False)]]},
        },
        "data": {
            "rate_import_day": [import_rate(minute) for minute in range(1440)],
            "rate_export_day": [export_rate if slot_start <= minute < slot_end else 0.0 for minute in range(1440)],
            "load_day_kwh": [LOAD_KW / 60.0] * 1440,
            "pv_day_kw": [0.0] * 1440,
        },
    }


def _plan(export_rate, switch=True, publish_calls=None):
    """Plan the scenario on a fresh PredBat with the switch on; return it and the cancellable slots."""
    # Imported here: unit_test imports this module, so a top level import would be circular
    from unit_test import create_predbat
    from tests.test_infra import reset_inverter
    from tests.test_random_scenarios import RANDOM_TEMPLATE, apply_scenario_to_predbat

    plan_predbat = create_predbat()
    with contextlib.redirect_stdout(io.StringIO()):
        reset_inverter(plan_predbat)
        plan_predbat.read_debug_yaml(RANDOM_TEMPLATE)
        plan_predbat.config_root = "./"
        plan_predbat.save_restore_dir = "./"
        plan_predbat.load_user_config()
        apply_scenario_to_predbat(plan_predbat, _plan_scenario(export_rate))
        plan_predbat.battery_rate_max_export = 10.0 / 60.0
        plan_predbat.rate_max_base = PEAK
        plan_predbat.car_charging_now_slots = [[]]
        plan_predbat.car_planned_by_predbat = {0}
        plan_predbat.car_force_holdable = {}
        plan_predbat.car_force_hold_until = {}
        plan_predbat.car_charging_from_battery = False
        plan_predbat.car_energy_reported_load = True
        plan_predbat.car_charging_loss = 1.0
        plan_predbat.prediction_kernel_enable = False
        plan_predbat.calculate_best_export = True
        plan_predbat.args["threads"] = 0
        plan_predbat.plan_valid = False
        plan_predbat.debug_enable = False
        _set_switch(plan_predbat, switch)
        if publish_calls is not None:
            real_publish = plan_predbat.publish_car_plan
            plan_predbat.publish_car_plan = lambda: (publish_calls.append(1), real_publish())[1]
        plan_predbat.calculate_plan(recompute=True)
    return plan_predbat, plan_predbat.car_cancel_slots()


def _exports_over_slot(plan_predbat):
    """The target exports the chosen plan has over the car slot."""
    slot_start, slot_end = PLAN_SLOT
    return [window for window, limit in zip(plan_predbat.export_window_best, plan_predbat.export_limits_best) if export_mode_of(limit) == EXPORT_MODE_TARGET and window["start"] < slot_end and window["end"] > slot_start]


def test_valuation_saving_session_wins():
    """A 200p export over a 7p car slot beats buying the 10 kWh back at 30p, and the plan charges exactly that."""
    plan_predbat, slots = _plan(200.0)
    assert plan_predbat.prediction.kernel_handle == 0
    assert _exports_over_slot(plan_predbat), "no target export over the slot: {}".format(list(zip(plan_predbat.export_window_best, plan_predbat.export_limits_best)))
    plan = (plan_predbat.charge_limit_best, plan_predbat.charge_window_best, plan_predbat.export_window_best, plan_predbat.export_limits_best)
    assert cancelled_mask(slots, plan[2], plan[3]) == 1
    cancelled = plan_predbat.prediction.run_prediction(*plan, PV_SCENARIO_NOMINAL, plan_predbat.end_record)
    # The same plan with the switch off and the car slot removed
    _set_switch(plan_predbat, False)
    no_car_model = [[]]
    no_car = Prediction(
        plan_predbat,
        plan_predbat.pv_forecast_minute_step,
        plan_predbat.pv_forecast_minute10_step,
        plan_predbat.load_minutes_step,
        plan_predbat.load_minutes_step10,
        plan_predbat.pv_forecast_minute90_step,
        plan_predbat.load_minutes_step90,
        car_charging_slots=no_car_model,
        car_cancel=build_car_cancel_table(plan_predbat, no_car_model, plan_predbat.car_cancel_slots()),
    ).run_prediction(*plan, PV_SCENARIO_NOMINAL, plan_predbat.end_record)
    _assert_close("metric_keep rise", cancelled[8] - no_car[8], CAR_KWH * PEAK)
    _assert_close("cost", cancelled[0], no_car[0])


def test_valuation_normal_export_loses():
    """A 15p export does not pay for buying the car's energy back at 30p: the window stays idle and the car keeps its slot."""
    plan_predbat, slots = _plan(15.0)
    assert slots, "the slot should be cancellable"
    assert not _exports_over_slot(plan_predbat), "a target export over the slot: {}".format(list(zip(plan_predbat.export_window_best, plan_predbat.export_limits_best)))
    assert cancelled_mask(slots, plan_predbat.export_window_best, plan_predbat.export_limits_best) == 0


# ---------------------------------------------------------------------------
# Publishing, the plan display and the execute hold
# ---------------------------------------------------------------------------

PUBLISH_NAMES = (
    "num_cars",
    "car_charging_slots",
    "car_charging_now_slots",
    "car_planned_by_predbat",
    "car_force_holdable",
    "car_force_hold_until",
    "minutes_now",
    "export_window_best",
    "export_limits_best",
    "car_cancelled_for_export",
)


@contextlib.contextmanager
def _publish_scenario(my_predbat, export_over=None, switch=True, minutes_now=None):
    """
    One Predbat-led car with slot A (30 to 60 minutes from now, 4 kWh) and slot B (90 to 120, 6 kWh) and, if
    export_over is a (start, end) offset pair from now, a target export over it. Restores the fixture afterwards.
    """
    saved = {name: copy.deepcopy(getattr(my_predbat, name)) for name in PUBLISH_NAMES}
    saved_dashboard = copy.deepcopy(my_predbat.dashboard_values)
    saved_switch = my_predbat.get_arg("car_charging_cancel_for_export", False)
    try:
        if minutes_now is not None:
            my_predbat.minutes_now = minutes_now
        base = my_predbat.minutes_now
        my_predbat.num_cars = 1
        my_predbat.car_charging_now_slots = [[]]
        my_predbat.car_planned_by_predbat = {0}
        my_predbat.car_force_holdable = {}
        my_predbat.car_force_hold_until = {}
        my_predbat.car_cancelled_for_export = {}
        my_predbat.car_charging_slots = [
            [
                {"start": base + 30, "end": base + 60, "kwh": 4.0, "average": 10.0, "cost": 40.0, "octopus": False},
                {"start": base + 90, "end": base + 120, "kwh": 6.0, "average": 10.0, "cost": 60.0, "octopus": False},
            ]
        ]
        if export_over:
            my_predbat.export_window_best = [{"start": base + export_over[0], "end": base + export_over[1], "average": 20.0}]
            my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
        else:
            my_predbat.export_window_best = []
            my_predbat.export_limits_best = []
        _set_switch(my_predbat, switch)
        yield base
    finally:
        _set_switch(my_predbat, saved_switch)
        for name, value in saved.items():
            setattr(my_predbat, name, value)
        my_predbat.dashboard_values.clear()
        my_predbat.dashboard_values.update(saved_dashboard)


def _sensor(my_predbat):
    """The published car 0 slot sensor: (state, attributes)."""
    entity = my_predbat.dashboard_values["binary_sensor." + my_predbat.prefix + "_car_charging_slot"]
    return entity["state"], entity["attributes"]


def test_publish_cancelled_for_export(my_predbat):
    """A slot under a target export leaves the plan and is listed in cancelled_for_export, which the charger reader parses."""
    with _publish_scenario(my_predbat, export_over=(30, 60)) as base:
        my_predbat.publish_car_plan()
        state, attributes = _sensor(my_predbat)
        a_start, a_end = my_predbat.time_abs_str(base + 30), my_predbat.time_abs_str(base + 60)
        assert [window["start"] for window in attributes["planned"]] == [my_predbat.time_abs_str(base + 90)], attributes
        assert attributes["cancelled_for_export"] == [{"start": a_start, "end": a_end}], attributes
        assert attributes["kwh"] == 6.0 and attributes["cost"] == 60.0, attributes
        assert my_predbat.car_cancelled_for_export == {0: [(base + 30, base + 60)]}
        now = (my_predbat.midnight_utc + timedelta(minutes=base)).astimezone(my_predbat.local_tz)
        parsed = parse_car_plan_windows(attributes["cancelled_for_export"], now, my_predbat.local_tz)
        assert len(parsed) == 1 and parsed[0][0] == (my_predbat.midnight_utc + timedelta(minutes=base + 30)).astimezone(my_predbat.local_tz), parsed


def test_publish_partial_overlap_whole_slot(my_predbat):
    """An export over only the last 5 minutes of a slot still cancels the whole slot."""
    with _publish_scenario(my_predbat, export_over=(55, 75)) as base:
        my_predbat.publish_car_plan()
        _, attributes = _sensor(my_predbat)
        assert attributes["cancelled_for_export"] == [{"start": my_predbat.time_abs_str(base + 30), "end": my_predbat.time_abs_str(base + 60)}], attributes
        assert len(attributes["planned"]) == 1, attributes


def test_publish_state_off_when_current_cancelled(my_predbat):
    """With now inside a cancelled slot the sensor is off, and on once inside a slot that is kept."""
    with _publish_scenario(my_predbat, export_over=(-10, 10)) as base:
        my_predbat.car_charging_slots[0][0].update({"start": base - 10, "end": base + 20})
        my_predbat.publish_car_plan()
        state, attributes = _sensor(my_predbat)
        assert state == "off", (state, attributes)
        assert len(attributes["cancelled_for_export"]) == 1 and len(attributes["planned"]) == 1, attributes
    with _publish_scenario(my_predbat) as base:
        my_predbat.car_charging_slots[0][0].update({"start": base - 10, "end": base + 20})
        my_predbat.publish_car_plan()
        assert _sensor(my_predbat)[0] == "on"


def test_publish_fetch_time_uses_last_plan(my_predbat):
    """Called before calculate_plan(), as fetch does, the previous cycle's export windows already leave the slot out."""
    with _publish_scenario(my_predbat, export_over=(30, 60)):
        my_predbat.car_cancelled_for_export = {}
        my_predbat.publish_car_plan()
        _, attributes = _sensor(my_predbat)
        assert len(attributes["planned"]) == 1 and len(attributes["cancelled_for_export"]) == 1, attributes


def test_publish_switch_off_unchanged(my_predbat):
    """With the switch off nothing is cancelled, even under a target export."""
    with _publish_scenario(my_predbat, export_over=(30, 60), switch=False) as base:
        my_predbat.publish_car_plan()
        _, attributes = _sensor(my_predbat)
        assert attributes["cancelled_for_export"] == [], attributes
        assert [window["start"] for window in attributes["planned"]] == [my_predbat.time_abs_str(base + 30), my_predbat.time_abs_str(base + 90)], attributes
        assert attributes["kwh"] == 10.0 and attributes["cost"] == 100.0, attributes
        assert my_predbat.car_cancelled_for_export == {}


def test_publish_held_car(my_predbat):
    """A car a charger is holding off lists the slot starting before the hold ends, with no export at all."""
    with _publish_scenario(my_predbat) as base:
        my_predbat.car_planned_by_predbat = set()
        my_predbat.car_force_holdable = {0: "Fake"}
        my_predbat.car_force_hold_until = {0: my_predbat.midnight_utc + timedelta(minutes=base + 40)}
        my_predbat.publish_car_plan()
        _, attributes = _sensor(my_predbat)
        assert attributes["cancelled_for_export"] == [{"start": my_predbat.time_abs_str(base + 30), "end": my_predbat.time_abs_str(base + 60)}], attributes
        assert len(attributes["planned"]) == 1, attributes


def test_publish_all_cancelled_and_no_slots(my_predbat):
    """A car with every slot cancelled publishes like one with none, and still carries the list."""
    with _publish_scenario(my_predbat, export_over=(30, 120)) as base:
        my_predbat.publish_car_plan()
        state, attributes = _sensor(my_predbat)
        assert state == "off" and attributes["planned"] == [] and len(attributes["cancelled_for_export"]) == 2, attributes
        my_predbat.car_charging_slots = [[]]
        my_predbat.publish_car_plan()
        assert _sensor(my_predbat)[1]["cancelled_for_export"] == []


def test_display_marks_cancelled(my_predbat):
    """The plan display counts nothing for a cancelled slot and shows its kWh as cancelled, so it gets the "?"."""
    with _publish_scenario(my_predbat) as base:
        my_predbat.car_cancelled_for_export = {}
        assert my_predbat.car_charge_slot_kwh(base + 30, base + 60) == 4.0
        assert my_predbat.car_charge_slot_kwh_cancelled(base + 30, base + 60) == 0
        my_predbat.car_cancelled_for_export = {0: [(base + 30, base + 60)]}
        assert my_predbat.car_charge_slot_kwh(base + 30, base + 60) == 0
        assert my_predbat.car_charge_slot_kwh_cancelled(base + 30, base + 60) == 4.0
        assert my_predbat.car_charge_slot_kwh(base + 90, base + 120) == 6.0
        assert my_predbat.car_charge_slot_kwh_cancelled(base + 90, base + 120) == 0


def test_plan_republishes_car_plan_only_when_enabled():
    """calculate_plan() republishes the car plan after planning only with the switch on; switch off keeps today's single publish."""
    off_calls = []
    _plan(200.0, switch=False, publish_calls=off_calls)
    assert off_calls == [], "switch off published the car plan {} times".format(len(off_calls))
    on_calls = []
    _plan(200.0, switch=True, publish_calls=on_calls)
    assert on_calls, "switch on did not publish the car plan after planning"


def run_car_cancel_tests(my_predbat):
    """Run the car cancel tests. Returns True on failure."""
    print("**** Running car cancel tests ****")
    test_cancellable_slots_rules()
    test_cancellable_slots_cap()
    test_cancellable_slots_forced()
    test_cancelled_mask()
    test_cancelled_windows()
    test_car_cancellable_cars(my_predbat)
    test_car_cancel_slots(my_predbat)
    test_gate_relaxed_for_cancellable(my_predbat)
    test_gate_blocked_for_uncancellable(my_predbat)
    test_gate_switch_off_unchanged(my_predbat)
    test_gate_mixed_slots(my_predbat)
    test_gate_cap(my_predbat)
    test_levels_and_freeze_unchanged(my_predbat)
    test_force_hold_tick_publishes_holdable()
    test_revert_outside_offpeak(my_predbat)
    test_revert_inside_offpeak(my_predbat)
    test_revert_other_car_trusted(my_predbat)
    test_revert_split_half_hour(my_predbat)
    test_revert_saving_session(my_predbat)
    test_revert_window_billing(my_predbat)
    test_reverted_step_not_dispatch_gone(my_predbat)
    test_reverted_step_no_io_credit(my_predbat)
    test_penalty_respects_car_full(my_predbat)
    test_valuation_ready_time_not_free(my_predbat)
    test_switch_off_identical(my_predbat)
    test_valuation_saving_session_wins()
    test_valuation_normal_export_loses()
    test_plan_republishes_car_plan_only_when_enabled()
    test_publish_cancelled_for_export(my_predbat)
    test_publish_partial_overlap_whole_slot(my_predbat)
    test_publish_state_off_when_current_cancelled(my_predbat)
    test_publish_fetch_time_uses_last_plan(my_predbat)
    test_publish_switch_off_unchanged(my_predbat)
    test_publish_held_car(my_predbat)
    test_publish_all_cancelled_and_no_slots(my_predbat)
    test_display_marks_cancelled(my_predbat)
    print("**** car cancel tests passed ****")
    return False

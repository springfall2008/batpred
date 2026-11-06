"""Broad first-principles power combinations for EV and domestic-battery plans."""

from dataclasses import replace
from itertools import product

from car_charging_solver import (
    BatteryConfig,
    EVConfig,
    PlannerConfig,
    PlannerInputError,
    SiteConfig,
    Slot,
    _segment,
    plan,
)


_TOLERANCE = 1e-8


def _assert_close(actual, expected, label):
    """Compare independently derived physical quantities with a useful failure label."""
    assert abs(actual - expected) <= _TOLERANCE, "{}: expected {!r}, got {!r}".format(label, expected, actual)


def _assert_segment_physics(segments, duration_min, house_kw, ev_kw, pv_kw, battery, site):
    """Check AC balance, stored-energy change, mutually exclusive flows and every cap."""
    assert segments
    _assert_close(sum(segment.duration_min for segment in segments), duration_min, "segment duration")
    for segment in segments:
        hours = segment.duration_min / 60
        _assert_close(segment.load_kw, house_kw, "house power")
        _assert_close(segment.ev_kw, ev_kw, "EV power")
        _assert_close(segment.pv_kw, pv_kw, "PV power")
        _assert_close(
            segment.grid_import_kw + segment.pv_kw + segment.battery_discharge_kw,
            segment.load_kw + segment.ev_kw + segment.battery_charge_kw + segment.grid_export_kw + segment.curtailed_kw,
            "AC bus balance",
        )
        _assert_close(
            segment.soc_end_kwh - segment.soc_start_kwh,
            hours * (segment.battery_charge_kw * battery.charge_efficiency - segment.battery_discharge_kw / battery.discharge_efficiency),
            "stored-energy change",
        )
        assert segment.battery_charge_kw * segment.battery_discharge_kw <= _TOLERANCE
        assert segment.grid_import_kw * segment.grid_export_kw <= _TOLERANCE
        assert segment.grid_import_kw <= site.import_cap_kw + _TOLERANCE
        if site.export_cap_kw is not None:
            assert segment.grid_export_kw <= site.export_cap_kw + _TOLERANCE
        if site.load_port_cap_kw is not None:
            assert segment.load_kw + segment.ev_kw <= site.load_port_cap_kw + _TOLERANCE
        assert 0 <= segment.battery_charge_kw <= battery.max_charge_kw + _TOLERANCE
        assert 0 <= segment.battery_discharge_kw <= battery.max_discharge_kw + _TOLERANCE
        assert battery.reserve_kwh - _TOLERANCE <= segment.soc_start_kwh <= battery.capacity_kwh + _TOLERANCE
        assert battery.reserve_kwh - _TOLERANCE <= segment.soc_end_kwh <= battery.capacity_kwh + _TOLERANCE


def test_segment_power_matrix():
    """Compare charge, demand and hold against hand-calculated source/load combinations."""
    battery = BatteryConfig(10, 5, 1, 2, 3, 0.8, 0.5, 1)
    ev = EVConfig(40, 0, 20, 4, 0.9, pause_battery_percent=0, restart_battery_percent=0)
    cases = (
        # label, mode, house, EV, PV, SOC, site-import cap, export cap, charge cap, expected flows
        ("demand-battery-support", "demand", 3, 2, 1, 5, 6, 2, None, (1, 0, 0, 0, 3, 4.5)),
        ("hold-keeps-grid-import", "hold", 3, 2, 1, 5, 6, 2, None, (4, 0, 0, 0, 0, 5)),
        ("demand-solar-charge", "demand", 2, 0, 5, 5, 6, 2, None, (0, 1, 0, 2, 0, 5 + 2 * 0.8 / 12)),
        ("hold-solar-charge", "hold", 2, 0, 5, 5, 6, 2, None, (0, 1, 0, 2, 0, 5 + 2 * 0.8 / 12)),
        ("charge-without-support", "charge", 1, 2, 1, 5, 6, 2, None, (4, 0, 0, 2, 0, 5 + 2 * 0.8 / 12)),
        ("charge-with-support-headroom", "charge", 2, 0, 1, 5, 6, 2, 4, (3, 0, 0, 2, 0, 5 + 2 * 0.8 / 12)),
        ("charge-with-support-discharge", "charge", 4, 2, 0, 5, 6, 2, 4, (4, 0, 0, 0, 2, 5 - 2 / 12 / 0.5)),
        ("demand-pv-and-battery", "demand", 2, 0, 1, 5, 6, 2, None, (0, 0, 0, 0, 1, 5 - 1 / 12 / 0.5)),
        ("balanced-zero-import", "hold", 1, 0, 1, 5, 0, 0, None, (0, 0, 0, 0, 0, 5)),
        ("export-and-curtail", "hold", 0, 0, 5, 5, 6, 2, None, (0, 2, 1, 2, 0, 5 + 2 * 0.8 / 12)),
        ("zero-export-cap", "hold", 0, 0, 5, 5, 6, 0, None, (0, 0, 3, 2, 0, 5 + 2 * 0.8 / 12)),
        ("reserve-prevents-discharge", "demand", 5, 0, 0, 1, 6, 2, None, (5, 0, 0, 0, 0, 1)),
        ("full-pack-exports-solar", "demand", 2, 0, 5, 10, 6, 2, None, (0, 2, 1, 0, 0, 10)),
        ("full-pack-does-not-charge", "charge", 2, 2, 0, 10, 6, 2, None, (4, 0, 0, 0, 0, 10)),
        ("charge-overload-is-rejected", "charge", 4, 3, 0, 5, 6, 2, None, None),
        ("support-shortfall-over-import-cap", "charge", 8, 0, 0, 5, 4, 2, 4, None),
    )
    rejected = 0
    for label, mode, house_kw, ev_kw, pv_kw, soc, import_cap, export_cap, charge_cap, expected in cases:
        site = SiteConfig(import_cap, export_cap, 12, charge_cap)
        actual = _segment(5, house_kw, ev_kw, pv_kw, soc, mode, battery, ev, site)
        if expected is None:
            assert actual is None, "{} should exceed its instantaneous cap".format(label)
            rejected += 1
            continue
        assert actual is not None, "{} unexpectedly rejected".format(label)
        segments, end_soc = actual
        assert len(segments) == 1, "{} should remain one constant-power segment".format(label)
        _assert_segment_physics(segments, 5, house_kw, ev_kw, pv_kw, battery, site)
        segment = segments[0]
        for name, actual_value, expected_value in zip(
            ("grid import", "grid export", "curtailment", "battery charge", "battery discharge", "end SOC"),
            (segment.grid_import_kw, segment.grid_export_kw, segment.curtailed_kw, segment.battery_charge_kw, segment.battery_discharge_kw, end_soc),
            expected,
        ):
            _assert_close(actual_value, expected_value, "{} {}".format(label, name))
        _assert_close(end_soc, segment.soc_end_kwh, "{} returned SOC".format(label))
    assert rejected == 2

    limited_discharge = replace(battery, max_discharge_kw=1.5)
    discharge_result = _segment(5, 4, 0, 0, 5, "demand", limited_discharge, ev, SiteConfig(6, 2, 12))
    assert discharge_result is not None
    discharge_segments, discharge_end = discharge_result
    _assert_segment_physics(discharge_segments, 5, 4, 0, 0, limited_discharge, SiteConfig(6, 2, 12))
    _assert_close(discharge_segments[0].battery_discharge_kw, 1.5, "limited battery discharge rate")
    _assert_close(discharge_segments[0].grid_import_kw, 2.5, "limited-discharge grid remainder")
    _assert_close(discharge_end, 5 - 1.5 / 12 / 0.5, "limited-discharge stored delta")

    limited_charge = replace(battery, max_charge_kw=0.5)
    charge_result = _segment(5, 2, 0, 5, 5, "hold", limited_charge, ev, SiteConfig(6, 2, 12))
    assert charge_result is not None
    charge_segments, charge_end = charge_result
    _assert_segment_physics(charge_segments, 5, 2, 0, 5, limited_charge, SiteConfig(6, 2, 12))
    _assert_close(charge_segments[0].battery_charge_kw, 0.5, "limited battery charge rate")
    _assert_close(charge_segments[0].grid_export_kw, 2, "limited-charge solar export")
    _assert_close(charge_segments[0].curtailed_kw, 0.5, "limited-charge solar curtailment")
    _assert_close(charge_end, 5 + 0.5 * 0.8 / 12, "limited-charge stored delta")

    port_rejected = _segment(5, 3, 3, 6, 5, "hold", battery, ev, SiteConfig(10, 2, 5))
    assert port_rejected is None, "house plus fixed-rate EV must respect the load-port limit"
    return len(cases) + 3, rejected + 1


def test_segment_boundary_clipping():
    """Exercise intra-step reserve/full transitions and PV export after saturation."""
    battery = BatteryConfig(10, 5, 1, 2, 4, 0.8, 0.5, 1)
    ev = EVConfig(40, 0, 20, 4, 0.9, pause_battery_percent=0, restart_battery_percent=0)

    reserve_site = SiteConfig(3, 0, 10)
    reserve_result = _segment(5, 3, 0, 0, 1.1, "demand", battery, ev, reserve_site)
    assert reserve_result is not None
    reserve_segments, reserve_end = reserve_result
    assert len(reserve_segments) == 2
    _assert_segment_physics(reserve_segments, 5, 3, 0, 0, battery, reserve_site)
    _assert_close(reserve_segments[0].duration_min, 1, "minutes to reserve")
    _assert_close(reserve_segments[0].battery_discharge_kw, 3, "pre-reserve discharge")
    _assert_close(reserve_segments[0].grid_import_kw, 0, "pre-reserve import")
    _assert_close(reserve_segments[1].duration_min, 4, "minutes after reserve")
    _assert_close(reserve_segments[1].battery_discharge_kw, 0, "post-reserve discharge")
    _assert_close(reserve_segments[1].grid_import_kw, 3, "post-reserve import")
    _assert_close(reserve_end, 1, "reserve-clipped SOC")

    charge_site = SiteConfig(10, 1, 10)
    charge_result = _segment(10, 1, 0, 0, 9.95, "charge", battery, ev, charge_site)
    assert charge_result is not None
    charge_segments, charge_end = charge_result
    assert len(charge_segments) == 2
    _assert_segment_physics(charge_segments, 10, 1, 0, 0, battery, charge_site)
    _assert_close(charge_segments[0].duration_min, 1.875, "minutes to full")
    _assert_close(charge_segments[0].grid_import_kw, 3, "pre-full import")
    _assert_close(charge_segments[0].battery_charge_kw, 2, "pre-full charge")
    _assert_close(charge_segments[1].duration_min, 8.125, "minutes after full")
    _assert_close(charge_segments[1].grid_import_kw, 1, "post-full import")
    _assert_close(charge_segments[1].battery_charge_kw, 0, "post-full charge")
    _assert_close(charge_end, 10, "full-clipped SOC")

    solar_result = _segment(10, 2, 0, 6, 9.95, "hold", battery, ev, charge_site)
    assert solar_result is not None
    solar_segments, solar_end = solar_result
    assert len(solar_segments) == 2
    _assert_segment_physics(solar_segments, 10, 2, 0, 6, battery, charge_site)
    _assert_close(solar_segments[0].duration_min, 1.875, "solar minutes to full")
    _assert_close(solar_segments[0].grid_export_kw, 1, "pre-full solar export")
    _assert_close(solar_segments[0].curtailed_kw, 1, "pre-full solar curtailment")
    _assert_close(solar_segments[1].duration_min, 8.125, "solar minutes after full")
    _assert_close(solar_segments[1].grid_export_kw, 1, "post-full solar export")
    _assert_close(solar_segments[1].curtailed_kw, 3, "post-full solar curtailment")
    _assert_close(solar_end, 10, "solar-clipped SOC")
    return 3


def _assert_plan_physics(result, slots, ev, battery, site):
    """Replay returned slot segments using only conservation, fixed power and state bounds."""
    assert result.status == "feasible", result.diagnostics
    assert len(result.actions) == len(slots)
    previous_battery = battery.initial_kwh
    previous_ev = ev.start_soc_kwh
    for action, slot in zip(result.actions, slots):
        _assert_close(action.battery_soc_kwh_start, previous_battery, "slot battery start")
        _assert_close(action.ev_soc_kwh_start, previous_ev, "slot EV start")
        _assert_close(action.load_kwh, slot.ac_load_kwh, "slot house energy")
        _assert_close(action.pv_kwh, slot.pv_kwh, "slot PV energy")
        assert action.segments
        _assert_close(sum(part.duration_min for part in action.segments), slot.duration_min, "slot segment duration")
        imported = exported = charged = discharged = delivered = 0.0
        on_minutes = 0.0
        for part in action.segments:
            hours = part.duration_min / 60
            _assert_close(part.load_kw * hours, slot.ac_load_kwh * hours * 60 / slot.duration_min, "segment house energy")
            _assert_close(part.pv_kw * hours, slot.pv_kwh * hours * 60 / slot.duration_min, "segment PV energy")
            _assert_close(
                part.grid_import_kw + part.pv_kw + part.battery_discharge_kw,
                part.load_kw + part.ev_kw + part.battery_charge_kw + part.grid_export_kw + part.curtailed_kw,
                "planned AC bus balance",
            )
            _assert_close(
                part.soc_end_kwh - part.soc_start_kwh,
                hours * (part.battery_charge_kw * battery.charge_efficiency - part.battery_discharge_kw / battery.discharge_efficiency),
                "planned stored-energy change",
            )
            assert part.battery_charge_kw * part.battery_discharge_kw <= _TOLERANCE
            assert part.grid_import_kw * part.grid_export_kw <= _TOLERANCE
            assert part.grid_import_kw <= site.import_cap_kw + _TOLERANCE
            if site.export_cap_kw is not None:
                assert part.grid_export_kw <= site.export_cap_kw + _TOLERANCE
            if site.load_port_cap_kw is not None:
                assert part.load_kw + part.ev_kw <= site.load_port_cap_kw + _TOLERANCE
            assert part.ev_kw in (0, ev.rate_kw)
            assert part.battery_charge_kw <= battery.max_charge_kw + _TOLERANCE
            assert part.battery_discharge_kw <= battery.max_discharge_kw + _TOLERANCE
            assert battery.reserve_kwh - _TOLERANCE <= part.soc_start_kwh <= battery.capacity_kwh + _TOLERANCE
            assert battery.reserve_kwh - _TOLERANCE <= part.soc_end_kwh <= battery.capacity_kwh + _TOLERANCE
            assert min(part.soc_start_kwh, part.soc_end_kwh) >= slot.battery_min_kwh - _TOLERANCE
            imported += part.grid_import_kw * hours
            exported += part.grid_export_kw * hours
            charged += part.battery_charge_kw * hours
            discharged += part.battery_discharge_kw * hours
            delivered += part.ev_kw * hours
            if part.ev_kw:
                on_minutes += part.duration_min
        _assert_close(action.grid_import_kwh, imported, "slot imported energy")
        _assert_close(action.grid_export_kwh, exported, "slot exported energy")
        _assert_close(action.battery_charge_kwh, charged, "slot battery charge energy")
        _assert_close(action.battery_discharge_kwh, discharged, "slot battery discharge energy")
        _assert_close(action.ev_delivered_kwh, delivered, "slot delivered EV energy")
        _assert_close(action.ev_on_minutes, on_minutes, "slot EV run time")
        _assert_close(action.battery_soc_kwh_end - previous_battery, charged * battery.charge_efficiency - discharged / battery.discharge_efficiency, "slot battery SOC delta")
        _assert_close(action.ev_soc_kwh_end - previous_ev, delivered * ev.efficiency, "slot EV SOC delta")
        assert action.ev_soc_kwh_end <= max(ev.stop_soc_kwh, ev.start_soc_kwh) + _TOLERANCE
        previous_battery = action.battery_soc_kwh_end
        previous_ev = action.ev_soc_kwh_end
    assert previous_battery >= battery.end_min_kwh - _TOLERANCE
    assert previous_ev >= ev.stop_soc_kwh - _TOLERANCE
    assert result.diagnostics.ev_stop_target_met


def test_plan_cartesian_power_matrix():
    """Run a bounded Cartesian sweep across modes, support, loads, PV, SoC and losses."""
    modes = ("demand", "hold", "charge")
    support_caps = (None, 5)
    house_loads = (0, 2, 4)
    pv_levels = (0, 3)
    battery_levels = (1.1, 5, 9.9)
    efficiency_pairs = ((0.8, 0.75), (0.96, 0.9))
    count = feasible = bounded_misses = 0
    for mode, support_cap, house_kw, pv_kw, initial_soc, efficiencies in product(modes, support_caps, house_loads, pv_levels, battery_levels, efficiency_pairs):
        charge_efficiency, discharge_efficiency = efficiencies
        ev_efficiency = 0.85 if charge_efficiency < 0.9 else 0.92
        ev = EVConfig(
            30,
            0,
            0.1,
            3,
            ev_efficiency,
            deadline_min=5,
            availability=(0,),
            pause_battery_percent=0,
            restart_battery_percent=0,
            min_off_minutes=0,
            initially_on=True,
        )
        battery = BatteryConfig(10, initial_soc, 1, 2, 4, charge_efficiency, discharge_efficiency, 1)
        site = SiteConfig(8, 2, 10, support_cap)
        slot = Slot(0, 0, 5, house_kw * 5 / 60, pv_kw * 5 / 60, 10, 0)
        planner = PlannerConfig(beam_width=None, allowed_battery_modes=(mode,))
        result = plan((slot,), ev, battery, site, planner)
        count += 1
        expected_miss = mode == "charge" and support_cap is None and house_kw == 4 and pv_kw == 0
        if expected_miss:
            assert result.status == "bounded_no_plan", (mode, support_cap, house_kw, pv_kw, initial_soc, efficiencies, result.diagnostics)
            assert not result.actions
            bounded_misses += 1
            continue
        _assert_plan_physics(result, (slot,), ev, battery, site)
        assert result.actions[0].battery_mode == mode
        assert result.actions[0].ev_delivered_kwh >= ev.stop_soc_kwh - _TOLERANCE
        assert result.actions[0].ev_on_minutes <= ev.deadline_min + _TOLERANCE
        feasible += 1
    assert count == 216
    assert feasible + bounded_misses == count
    assert bounded_misses == 6
    return count, feasible, bounded_misses


def test_plan_target_and_departure_clipping():
    """Check fixed-rate partial charging at target completion and an intra-slot departure."""
    battery = BatteryConfig(10, 5, 1, 2, 4, 0.9, 0.8, 1)
    site = SiteConfig(10, 0, 10)
    planner = PlannerConfig(beam_width=None, allowed_battery_modes=("hold",))
    target_cases = (
        ("target-inside-slot", 0.18, 5, 2),
        ("departure-inside-slot", 0.27, 3, 3),
    )
    for label, target, deadline, expected_minutes in target_cases:
        ev = EVConfig(
            40,
            0,
            target,
            6,
            0.9,
            deadline_min=deadline,
            availability=(0,),
            pause_battery_percent=0,
            restart_battery_percent=0,
            min_off_minutes=0,
            initially_on=True,
        )
        slot = Slot(0, 0, 5, 1 / 12, 0, 10, 0)
        result = plan((slot,), ev, battery, site, planner)
        _assert_plan_physics(result, (slot,), ev, battery, site)
        _assert_close(result.actions[0].ev_on_minutes, expected_minutes, label + " active minutes")
        assert any(part.ev_kw == 0 for part in result.actions[0].segments), label + " must stop inside the slot"

    impossible_ev = EVConfig(
        40,
        0,
        0.5,
        6,
        0.9,
        deadline_min=2,
        availability=(0,),
        pause_battery_percent=0,
        restart_battery_percent=0,
        min_off_minutes=0,
        initially_on=True,
    )
    impossible = plan((Slot(0, 0, 5, 1 / 12, 0, 10, 0),), impossible_ev, battery, site, planner)
    assert impossible.status == "bounded_no_plan"
    assert not impossible.actions
    return len(target_cases) + 1


def test_valid_zero_caps_and_overload():
    """Distinguish valid zero caps from a real, unserviceable site-power overload."""
    battery = BatteryConfig(10, 5, 1, 2, 4, 0.9, 0.8, 1)
    ev = EVConfig(30, 0, 0, 3, 0.9, deadline_min=5)
    planner = PlannerConfig(beam_width=None, allowed_battery_modes=("hold",))
    balanced = Slot(0, 0, 5, 1 / 12, 1 / 12, 10, 0)
    zero_cap_result = plan((balanced,), ev, battery, SiteConfig(0, 0, 10), planner)
    assert zero_cap_result.status == "feasible"
    _assert_plan_physics(zero_cap_result, (balanced,), ev, battery, SiteConfig(0, 0, 10))

    overloaded = Slot(0, 0, 5, 2 / 12, 0, 10, 0)
    no_power = replace(battery, max_discharge_kw=0, max_charge_kw=0)
    overload_result = plan((overloaded,), ev, no_power, SiteConfig(1, 0, 10), planner)
    assert overload_result.status == "bounded_no_plan"
    assert not overload_result.actions
    return 2


def test_slot_battery_floors():
    """Enforce a per-slot domestic SoC floor independently from reserve and terminal floors."""
    battery = BatteryConfig(10, 5, 1, 2, 4, 0.9, 0.8, 1)
    ev = EVConfig(30, 0, 0, 3, 0.9, deadline_min=5)
    site = SiteConfig(6, 0, 10)
    planner = PlannerConfig(beam_width=None, allowed_battery_modes=("demand",))
    results = []
    for floor, expected_status in ((4.6, "feasible"), (4.8, "bounded_no_plan")):
        slot = Slot(0, 0, 5, 3 * 5 / 60, 0, 10, 0, floor)
        result = plan((slot,), ev, battery, site, planner)
        assert result.status == expected_status, (floor, result.diagnostics)
        if expected_status == "feasible":
            _assert_plan_physics(result, (slot,), ev, battery, site)
            _assert_close(result.actions[0].battery_soc_kwh_end, 5 - 3 * 5 / 60 / 0.8, "floor-feasible end SOC")
        else:
            assert not result.actions
        results.append(result)
    assert results[0].actions[0].battery_soc_kwh_end < 4.8
    return len(results)


def test_planner_rejects_invalid_power_inputs():
    """Reject negative, boolean and non-finite physical inputs at the validated planner API."""
    slots = (Slot(0, 0, 5, 0, 0, 10, 0),)
    ev = EVConfig(30, 0, 0, 3, 0.9, deadline_min=5)
    battery = BatteryConfig(10, 5, 1, 2, 4, 0.9, 0.8, 1)
    site = SiteConfig(6, 1, 10)
    invalid_cases = (
        ((Slot(0, 0, 5, -0.1, 0, 10, 0),), ev, battery, site),
        ((Slot(0, 0, 5, 0, -0.1, 10, 0),), ev, battery, site),
        ((Slot(0, 0, 5, True, 0, 10, 0),), ev, battery, site),
        ((Slot(0, 0, 5, 0, float("nan"), 10, 0),), ev, battery, site),
        ((Slot(0, 0, 5, float("inf"), 0, 10, 0),), ev, battery, site),
        ((Slot(0, 0, 5, 0, 0, float("nan"), 0),), ev, battery, site),
        ((Slot(0, 0, 5, 0, 0, 0, float("inf")),), ev, battery, site),
        (slots, ev, battery, SiteConfig(-1, 1, 10)),
        (slots, ev, battery, SiteConfig(float("nan"), 1, 10)),
        (slots, ev, battery, SiteConfig(True, 1, 10)),
        (slots, ev, battery, SiteConfig(6, -1, 10)),
        (slots, ev, battery, SiteConfig(6, 1, float("inf"))),
        (slots, ev, battery, SiteConfig(6, 1, 10, 0)),
        (slots, ev, battery, SiteConfig(6, 1, 10, 7)),
        (slots, ev, replace(battery, initial_kwh=float("nan")), site),
        (slots, ev, replace(battery, charge_efficiency=float("inf")), site),
        (slots, ev, replace(battery, max_charge_kw=-1), site),
        (slots, EVConfig(30, 0, 0, -1, 0.9), battery, site),
    )
    for bad_slots, bad_ev, bad_battery, bad_site in invalid_cases:
        try:
            plan(bad_slots, bad_ev, bad_battery, bad_site, PlannerConfig())
        except PlannerInputError:
            continue
        raise AssertionError("Invalid planner power input was accepted")
    return len(invalid_cases)


def run_car_planning_power_matrix_tests(unused_base):
    """Run the deterministic power matrix and return the harness's failure boolean."""
    direct_count, direct_rejected = test_segment_power_matrix()
    clipping_count = test_segment_boundary_clipping()
    planner_count, planner_feasible, planner_misses = test_plan_cartesian_power_matrix()
    target_count = test_plan_target_and_departure_clipping()
    edge_count = test_valid_zero_caps_and_overload()
    floor_count = test_slot_battery_floors()
    invalid_count = test_planner_rejects_invalid_power_inputs()
    total = direct_count + clipping_count + planner_count + target_count + edge_count + floor_count + invalid_count
    print(
        "EV power matrix: {} cases ({} direct flows, {} direct rejections, {} boundary clips, "
        "{} planner combinations: {} feasible/{} bounded misses, {} target/departure, {} zero/overload, "
        "{} battery-floor, {} invalid-input)".format(
            total,
            direct_count - direct_rejected,
            direct_rejected,
            clipping_count,
            planner_count,
            planner_feasible,
            planner_misses,
            target_count,
            edge_count,
            floor_count,
            invalid_count,
        )
    )
    return False

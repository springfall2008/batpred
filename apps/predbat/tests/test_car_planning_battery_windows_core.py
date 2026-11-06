"""Pure planner API: selectable minimum battery-mode windows (5/15/30/60 minutes).

These tests pin the contract for the new ``battery_min_window_minutes``,
``initial_window_mode`` and ``initial_window_remaining_minutes`` planner fields.
They exercise the pure API; native replay and dispatch have separate tests.

Conventions:
    * EV is never frozen inside a battery-mode window; the new constraint only
      restricts which battery mode applies to the next slot expansion.
    * Physical constraints (grid cap, EV deadline, reserve, off/cooldown) are
      preserved exactly; if a windowed mode would block a feasible action the
      planner must return ``bounded_no_plan`` instead of silently relaxing the
      window.
    * ``battery_min_window_minutes == 5`` and no initial commitment must be
      indistinguishable from the previous solver: same actions, same frontier
      partitioning, same dedup key shape (no extra components that change
      pruning).
    * The first node carries ``initial_window_mode`` and
      ``initial_window_remaining_minutes``; a positive remaining time is only
      legal when an initial mode is also declared.
     * The final horizon run may be truncated without weakening an interior run.
"""

from dataclasses import replace
import math
import unittest

from car_charging_solver import (
    BATTERY_DEMAND,
    BATTERY_HOLD,
    BATTERY_CHARGE,
    BATTERY_LIMITED_DEMAND,
    BatteryConfig,
    EVConfig,
    PlannerConfig,
    PlannerInputError,
    SiteConfig,
    Slot,
    STATUS_FEASIBLE,
    STATUS_BOUNDED_NO_PLAN,
    plan,
)


def _battery(**kwargs):
    """Synthetic 32 kWh domestic pack with explicit AC limits and cell-side energy."""
    values = dict(
        capacity_kwh=32,
        initial_kwh=20,
        reserve_kwh=3.2,
        max_charge_kw=3,
        max_discharge_kw=8.192,
        charge_efficiency=0.96,
        discharge_efficiency=0.96,
        end_min_kwh=3.2,
        terminal_value_per_kwh=8,
    )
    values.update(kwargs)
    return BatteryConfig(**values)


def _vehicle(**kwargs):
    """A fixed 6.77 kW synthetic charger, not a solar-surplus modulating charger."""
    values = dict(
        battery_kwh=25,
        start_soc_kwh=0,
        stop_soc_kwh=3,
        rate_kw=6.77,
        efficiency=0.92,
        deadline_min=60,
    )
    values.update(kwargs)
    return EVConfig(**values)


def _series(minutes=60, load_kw=1, pv_kw=0, price=10, step=5, export_price=0):
    """Build complete constant-power forecasts in kWh per interval."""
    return tuple(Slot(i, m, step, load_kw * step / 60, pv_kw * step / 60, price, export_price) for i, m in enumerate(range(0, minutes, step)))


def _modes_of(actions):
    """Return the battery-mode sequence of a feasible plan."""
    return tuple(a.battery_mode for a in actions)


def _run_lengths_of(actions):
    """Consecutive run lengths of identical battery modes."""
    if not actions:
        return ()
    runs = []
    prev = actions[0].battery_mode
    n = 1
    for action in actions[1:]:
        if action.battery_mode == prev:
            n += 1
        else:
            runs.append((prev, n))
            prev = action.battery_mode
            n = 1
    runs.append((prev, n))
    return tuple(runs)


def _slot_minutes_of(actions):
    """Per-slot minute lengths of battery modes."""
    return tuple(a.segments[0].duration_min if a.segments else 0.0 for a in actions)


def _assert_first_balance(result, slots, ev, bat, site):
    """Helper: standard conservation + interval checks (mirrors core tests)."""
    assert result.status == STATUS_FEASIBLE, result.diagnostics
    assert len(result.actions) == len(slots)
    assert result.diagnostics.unmet_load_kwh == 0
    previous_battery, previous_ev = bat.initial_kwh, ev.start_soc_kwh
    for action, slot in zip(result.actions, slots):
        assert action.slot_index == slot.index
        assert math.isclose(action.load_kwh, slot.ac_load_kwh, abs_tol=1e-8)
        assert math.isclose(action.pv_kwh, slot.pv_kwh, abs_tol=1e-8)
        assert math.isclose(action.battery_soc_kwh_start, previous_battery, abs_tol=1e-8)
        assert math.isclose(action.ev_soc_kwh_start, previous_ev, abs_tol=1e-8)
        assert math.isclose(
            action.grid_import_kwh + action.pv_kwh + action.battery_discharge_kwh,
            action.load_kwh + action.ev_delivered_kwh + action.battery_charge_kwh + action.grid_export_kwh + action.pv_curtailed_kwh,
            abs_tol=1e-8,
        )
        assert math.isclose(
            action.battery_soc_kwh_end - previous_battery,
            action.battery_charge_kwh * bat.charge_efficiency - action.battery_discharge_kwh / bat.discharge_efficiency,
            abs_tol=1e-8,
        )
        assert math.isclose(action.ev_soc_kwh_end - previous_ev, action.ev_delivered_kwh * ev.efficiency, abs_tol=1e-8)
        assert action.battery_soc_kwh_end >= bat.reserve_kwh - 1e-8
        assert action.battery_soc_kwh_end <= bat.capacity_kwh + 1e-8
        assert math.isclose(action.ev_delivered_kwh, ev.rate_kw * action.ev_on_minutes / 60, abs_tol=1e-8)
        assert math.isclose(sum(s.duration_min for s in action.segments), slot.duration_min, abs_tol=1e-8)
        for segment in action.segments:
            assert segment.grid_import_kw <= site.import_cap_kw + 1e-8
            if site.load_port_cap_kw is not None:
                assert segment.load_kw + segment.ev_kw <= site.load_port_cap_kw + 1e-8
            assert segment.ev_kw in (0, ev.rate_kw)
            assert segment.battery_charge_kw <= bat.max_charge_kw + 1e-8
            assert segment.battery_discharge_kw <= bat.max_discharge_kw + 1e-8
            assert not (segment.battery_charge_kw and segment.battery_discharge_kw)
        previous_battery, previous_ev = action.battery_soc_kwh_end, action.ev_soc_kwh_end
    assert previous_ev >= ev.stop_soc_kwh - 1e-8
    assert previous_battery >= bat.end_min_kwh - 1e-8


class BatteryWindowCoreTests(unittest.TestCase):
    """Selectable minimum-mode windows: parity, persistence, validation and safety."""

    # ------------------------------------------------------------------
    # Parity: default 5 with no commitment must match the previous solver.
    # ------------------------------------------------------------------

    def test_default_5_no_initial_matches_legacy_exact(self):
        """min=5 + no initial mode reproduces the exact previous result and dedup key."""
        # Use exact (beam=None) so dedup keys collapse on physical state only.
        slots = _series(minutes=30, load_kw=1, pv_kw=2, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0), _battery(), SiteConfig(6, 0, 10)
        legacy = plan(slots, ev, bat, site, PlannerConfig(beam_width=None))
        windowed = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=None,
                battery_min_window_minutes=5,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(legacy.status, windowed.status)
        self.assertEqual(legacy.status, STATUS_FEASIBLE)
        self.assertEqual(_modes_of(legacy.actions), _modes_of(windowed.actions))
        self.assertEqual(legacy.battery_soc_trace, windowed.battery_soc_trace)
        self.assertEqual(legacy.total_cost, windowed.total_cost)
        # Identical frontier size means identical pruning/partition.
        self.assertEqual(
            legacy.diagnostics.max_frontier_size,
            windowed.diagnostics.max_frontier_size,
        )

    def test_default_5_no_initial_matches_legacy_beam(self):
        """Beam-pruned search must also be identical with min=5 and no initial mode."""
        slots = _series(minutes=60, load_kw=0.5, pv_kw=1.5, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=60), _battery(), SiteConfig(6, 0, 10)
        legacy = plan(slots, ev, bat, site, PlannerConfig(beam_width=32))
        windowed = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=32,
                battery_min_window_minutes=5,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(legacy.status, windowed.status)
        self.assertEqual(_modes_of(legacy.actions), _modes_of(windowed.actions))
        self.assertEqual(legacy.battery_soc_trace, windowed.battery_soc_trace)
        self.assertEqual(legacy.diagnostics.max_frontier_size, windowed.diagnostics.max_frontier_size)

    # ------------------------------------------------------------------
    # 15/30/60 windows with alternating price incentives.
    # ------------------------------------------------------------------

    def test_min_15_alternating_prices(self):
        """With 5-min slots and min=15, every new mode must persist for >=3 slots."""
        # 6 slots of 5 min; cheap import for the first 3, expensive for the last 3.
        slots = tuple(replace(s, import_price=8.0 if s.start_min < 15 else 30.0) for s in _series(minutes=30, load_kw=0.5))
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=30), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        runs = _run_lengths_of(result.actions)
        # Each committed run must be at least 15 minutes (3 slots of 5 min each),
        # except possibly the final truncated run (none here: 30/5=6).
        for mode, count in runs[:-1]:
            self.assertGreaterEqual(
                count * 5,
                15,
                "Mode {} ran only {} slots (<3) before switch".format(mode, count),
            )

    def test_min_30_alternating_prices(self):
        """With 15-min slots and min=30, every new mode must persist for >=2 slots."""
        slots = tuple(
            replace(
                s,
                import_price=8.0 if s.start_min < 30 else 30.0,
                pv_kwh=0.0,
                ac_load_kwh=0.25,
            )
            for s in _series(minutes=60, load_kw=1, step=15)
        )
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=60), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                time_step_minutes=15,
                battery_min_window_minutes=30,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        # Two slots of 15 min = 30 min; 4 slots total, 60 min horizon. Final
        # run may be truncated only if the last slot is shorter, which it is
        # not here, so every run must be at least 30 minutes.
        for mode, count in _run_lengths_of(result.actions):
            self.assertGreaterEqual(
                count * 15,
                30,
                "Mode {} ran only {} slots (<2) under min=30".format(mode, count),
            )

    def test_min_60_alternating_prices(self):
        """min=60 forces the entire 60-minute horizon to share one mode."""
        slots = tuple(replace(s, import_price=8.0 if s.start_min < 30 else 30.0) for s in _series(minutes=60, load_kw=0.4, pv_kw=1.0))
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=60), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=60,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        runs = _run_lengths_of(result.actions)
        # Exactly one run, since the entire 60-min horizon is a single window.
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][1] * 5, 60)
        self.assertEqual(runs[0][0], result.actions[0].battery_mode)

    # ------------------------------------------------------------------
    # Carried initial remaining time and mode.
    # ------------------------------------------------------------------

    def test_initial_remaining_carries_into_first_run(self):
        """initial_remaining=10 with min=15 locks the initial mode for two 5-min slots."""
        slots = _series(minutes=30, load_kw=0.5, pv_kw=0.0, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=30), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_HOLD,
                initial_window_remaining_minutes=10,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        # 10 minutes remaining = 2 locked slots (slots 0 and 1).
        first_two = [a.battery_mode for a in result.actions[:2]]
        self.assertEqual(first_two, [BATTERY_HOLD, BATTERY_HOLD])
        # After the 10 minutes elapse, the mode is free to change. If the
        # planner commits a new mode at slot 2, that new mode must persist
        # for 15 minutes (3 slots). Whichever mode is chosen at slot 2 must
        # also appear at slots 3 and 4.
        committed = result.actions[2].battery_mode
        self.assertEqual(result.actions[3].battery_mode, committed)
        self.assertEqual(result.actions[4].battery_mode, committed)

    def test_initial_remaining_zero_does_not_force_first_slot(self):
        """initial_remaining=0 with initial_mode is equivalent to "no commitment"."""
        slots = _series(minutes=15, load_kw=0.5, pv_kw=0.0, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=15), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_DEMAND,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        # If the very first slot chose a different mode, it must persist for
        # the whole 15-minute window.
        first_mode = result.actions[0].battery_mode
        if first_mode != BATTERY_DEMAND:
            self.assertEqual(
                _run_lengths_of(result.actions)[0],
                (first_mode, 3),
                "New mode at slot 0 must persist for the full window",
            )

    def test_initial_remaining_matches_min_acts_as_fresh_window(self):
        """initial_remaining == min_window means the mode is committed for the full window."""
        slots = _series(minutes=15, load_kw=0.5, pv_kw=0.0, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=15), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_HOLD,
                initial_window_remaining_minutes=15,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        # The first mode must hold for the entire 3-slot horizon.
        self.assertTrue(all(a.battery_mode == BATTERY_HOLD for a in result.actions))

    # ------------------------------------------------------------------
    # dedup correctness: tiny exhaustive oracle comparing exact vs beam.
    # ------------------------------------------------------------------

    def test_dedup_correctness_tiny_exhaustive(self):
        """Exact search and beam search must agree on a tiny horizon with min>5.

        Independent reference: enumerate every mode x on/off schedule that
        respects the 15-minute window and the same physical accounting, then
        verify the planner matches the cheapest one. The dedup key now
        partitions by (mode, mode_remaining) when min>5, so two schedules
        that share a stored energy and EV state but differ in their mode
        commitment are kept separately; this test confirms the planner
        still reaches the global optimum on a tiny instance.
        """
        slots = tuple(Slot(i, i * 5, 5, 0.25, pv, rate, 0.5) for i, (pv, rate) in enumerate(zip((0.0, 0.5, 0.0, 0.25), (2.0, 3.0, 1.0, 4.0))))
        ev = _vehicle(
            rate_kw=2,
            efficiency=1,
            stop_soc_kwh=0.4,
            deadline_min=20,
            availability=tuple(range(4)),
        )
        bat = _battery(
            capacity_kwh=100,
            initial_kwh=50,
            reserve_kwh=10,
            end_min_kwh=10,
            max_charge_kw=3,
            max_discharge_kw=8,
            charge_efficiency=1,
            discharge_efficiency=1,
            cycle_cost_per_kwh=0.2,
            terminal_value_per_kwh=0.0,
        )
        site = SiteConfig(3, 0, 12)
        min_window = 15
        all_modes = (BATTERY_DEMAND, BATTERY_HOLD, BATTERY_CHARGE)
        best = math.inf
        # Enumerate every (mode_seq, on_seq) schedule that respects the
        # window: each committed mode must persist for at least 3 slots
        # (15 min) except the final run, which is allowed to truncate.
        for m0 in all_modes:
            for m1 in all_modes:
                for m2 in all_modes:
                    for m3 in all_modes:
                        mode_seq = (m0, m1, m2, m3)
                        runs = []
                        prev = mode_seq[0]
                        n = 1
                        for m in mode_seq[1:]:
                            if m == prev:
                                n += 1
                            else:
                                runs.append((prev, n))
                                prev = m
                                n = 1
                        runs.append((prev, n))
                        for idx, (_, count) in enumerate(runs):
                            is_last = idx == len(runs) - 1
                            if not is_last and count * 5 < min_window:
                                break
                        else:
                            # Enumerate every on/off schedule: 2^4 = 16.
                            for o0 in (False, True):
                                for o1 in (False, True):
                                    for o2 in (False, True):
                                        for o3 in (False, True):
                                            on_seq = (o0, o1, o2, o3)
                                            # Window applies only to modes;
                                            # EV on/off is unconstrained here.
                                            stored, car, cost = 50.0, 0.0, 0.0
                                            ok = True
                                            for slot, mode, on in zip(slots, mode_seq, on_seq):
                                                # Decide the actual on-minutes, matching the
                                                # planner's min(slot_duration, deadline_left,
                                                # (target - car) / rate / eff * 60).
                                                deadline_left = 20 - slot.start_min
                                                remaining_target_kwh = max(0.4 - car, 0)
                                                if on and slot.index in ev.availability and slot.start_min < 20 and remaining_target_kwh > 0:
                                                    on_minutes = min(5, deadline_left, remaining_target_kwh / 2 * 60)
                                                else:
                                                    on_minutes = 0
                                                ev_load = 2 * on_minutes / 60 if on_minutes > 0 else 0.0
                                                demand = 0.25 + ev_load
                                                deficit = demand - slot.pv_kwh
                                                charge_cap_kwh = 3 * 5 / 60
                                                discharge_cap_kwh = 8 * 5 / 60
                                                charge = charge_cap_kwh if mode == BATTERY_CHARGE else min(max(-deficit, 0), charge_cap_kwh)
                                                discharge_kwh = min(max(deficit, 0), discharge_cap_kwh) if mode == BATTERY_DEMAND else 0
                                                net = deficit + charge - discharge_kwh
                                                if net > 3 * 0.25 + 1e-9:
                                                    ok = False
                                                    break
                                                stored += charge - discharge_kwh
                                                car += 2 * on_minutes / 60
                                                cost += max(net, 0) * slot.import_price - max(-net, 0) * slot.export_price + 0.2 * discharge_kwh
                                            if ok and car >= 0.4 - 1e-9:
                                                best = min(best, cost)
        self.assertLess(best, math.inf, "Reference oracle found no feasible schedule")
        # Exact planner must match the reference exactly.
        exact = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=None,
                battery_min_window_minutes=15,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(exact.status, STATUS_FEASIBLE)
        self.assertAlmostEqual(exact.total_cost, best, places=8)
        # Beam-pruned search must reach the same cost on this tiny case.
        beam = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=4,
                battery_min_window_minutes=15,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(beam.status, STATUS_FEASIBLE)
        self.assertAlmostEqual(beam.total_cost, exact.total_cost, places=8)

    # ------------------------------------------------------------------
    # Safety: blocked scheduling returns bounded_no_plan, no silent relax.
    # ------------------------------------------------------------------

    def test_reserve_exhaustion_under_window_returns_bounded_no_plan(self):
        """A windowed hold cannot be relaxed to demand even when the reserve is low."""
        # Reserve-bound battery: cannot discharge, only charge.
        # Window forces hold for 15 minutes; but solar is also low so charging
        # is limited. The only feasible plan needs demand, but the window
        # forbids the switch within 15 minutes, so the planner must report
        # bounded_no_plan.
        bat = _battery(
            capacity_kwh=10,
            initial_kwh=4,
            reserve_kwh=3.5,
            end_min_kwh=3.5,
            max_discharge_kw=8,
        )
        ev = _vehicle(stop_soc_kwh=2, deadline_min=20, availability=tuple(range(4)))
        # Strong solar only at slot 0..2 (cheap charge); slot 3 has no PV.
        slots = tuple(replace(s, pv_kwh=0.4 if s.index < 3 else 0.0) for s in _series(minutes=20, load_kw=0.2, price=10))
        site = SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_HOLD,
                initial_window_remaining_minutes=15,
            ),
        )
        # The full 20-min horizon is locked to hold by initial_remaining=15
        # followed by a 5-min slot that completes the window at minute 15.
        # If demand is required before then, the planner must not silently
        # switch.
        if result.status == STATUS_FEASIBLE:
            # If somehow feasible, the entire horizon must be hold (no
            # silent relax).
            self.assertTrue(all(a.battery_mode == BATTERY_HOLD for a in result.actions))
        else:
            self.assertEqual(result.status, STATUS_BOUNDED_NO_PLAN)

    def test_grid_cap_with_windowed_demand_returns_bounded_no_plan(self):
        """A 15-minute demand window that exceeds the import cap must be reported, not bypassed."""
        # 5 kW base load + 6.77 kW EV demand; site cap 6 kW. The only feasible
        # physical answer is bounded_no_plan (battery cannot help here).
        # Window forces demand for the full first 15 minutes, so no hold
        # reprieve is allowed.
        bat = _battery(capacity_kwh=10, initial_kwh=3.5, reserve_kwh=3.5, end_min_kwh=3.5, max_discharge_kw=0)
        ev = _vehicle(stop_soc_kwh=2, deadline_min=15, availability=tuple(range(3)))
        slots = _series(minutes=15, load_kw=5, pv_kw=0, price=10)
        site = SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_DEMAND,
                initial_window_remaining_minutes=15,
            ),
        )
        self.assertEqual(result.status, STATUS_BOUNDED_NO_PLAN)

    def test_ev_deadline_with_windowed_hold_returns_bounded_no_plan(self):
        """A windowed hold that prevents the EV from meeting its deadline must not be relaxed."""
        # 5-min deadline: EV must be on at slot 0, but windowed hold for the
        # first slot (initial_remaining=5 with min=15) forbids demand/charge,
        # and a single 5-min hold slot cannot deliver the requested energy.
        bat = _battery(capacity_kwh=10, initial_kwh=3.5, reserve_kwh=3.5, end_min_kwh=3.5)
        ev = _vehicle(stop_soc_kwh=0.5, deadline_min=5, availability=(0,))
        slots = _series(minutes=5, load_kw=0.1, pv_kw=0, price=10)
        site = SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_HOLD,
                initial_window_remaining_minutes=5,
            ),
        )
        self.assertEqual(result.status, STATUS_BOUNDED_NO_PLAN)

    # ------------------------------------------------------------------
    # Hold / limited subsets.
    # ------------------------------------------------------------------

    def test_hold_only_subset_with_window(self):
        """allowed_battery_modes=(hold,) forces every slot to hold under min>5."""
        slots = _series(minutes=15, load_kw=0.2, pv_kw=0, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=15), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                allowed_battery_modes=(BATTERY_HOLD,),
                battery_min_window_minutes=15,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertTrue(all(a.battery_mode == BATTERY_HOLD for a in result.actions))

    def test_limited_demand_with_window(self):
        """allow_limited_discharge + initial limited_demand window is honoured."""
        # 15-min slots, min=30, limited_demand forced for the first window.
        slots = _series(minutes=60, load_kw=1, pv_kw=0, price=10, step=15)
        ev = _vehicle(stop_soc_kwh=0, deadline_min=60, availability=tuple(range(4)))
        bat = _battery(initial_kwh=10)
        site = SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                time_step_minutes=15,
                allow_limited_discharge=True,
                battery_min_window_minutes=30,
                initial_window_mode=BATTERY_LIMITED_DEMAND,
                initial_window_remaining_minutes=30,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        # First two slots (30 min) must be limited_demand; remaining slots
        # may use demand (hold/charge are unavailable in the legacy path here).
        self.assertEqual(result.actions[0].battery_mode, BATTERY_LIMITED_DEMAND)
        self.assertEqual(result.actions[1].battery_mode, BATTERY_LIMITED_DEMAND)
        self.assertTrue(result.actions[0].discharge_limit_kw is not None)
        self.assertTrue(result.actions[1].discharge_limit_kw is not None)

    # ------------------------------------------------------------------
    # Validation.
    # ------------------------------------------------------------------

    def test_validation_min_window_rejects_invalid_values(self):
        """Only 5, 15, 30, 60 are accepted; the rest are rejected with PlannerInputError."""
        slots = _series(minutes=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=10), _battery(), SiteConfig(6, 0, 10)
        for bad in (0, 1, 7, 20, 45, 90, "15", True, 15.0, None):
            with self.assertRaises(PlannerInputError):
                plan(
                    slots,
                    ev,
                    bat,
                    site,
                    PlannerConfig(
                        beam_width=16,
                        battery_min_window_minutes=bad,
                    ),
                )

    def test_validation_initial_mode_must_be_string(self):
        """initial_window_mode must be a string when set."""
        slots = _series(minutes=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=10), _battery(), SiteConfig(6, 0, 10)
        for bad in (1, 0, True, 6.77, ("demand",), b"demand"):
            with self.assertRaises(PlannerInputError):
                plan(
                    slots,
                    ev,
                    bat,
                    site,
                    PlannerConfig(
                        beam_width=16,
                        battery_min_window_minutes=15,
                        initial_window_mode=bad,
                        initial_window_remaining_minutes=0,
                    ),
                )

    def test_validation_initial_mode_must_be_in_allowed_modes(self):
        """initial_window_mode must be a member of allowed_battery_modes (+ limited if supported)."""
        slots = _series(minutes=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=10), _battery(), SiteConfig(6, 0, 10)
        with self.assertRaises(PlannerInputError):
            # charge is not in the explicit (demand, hold) subset.
            plan(
                slots,
                ev,
                bat,
                site,
                PlannerConfig(
                    beam_width=16,
                    allowed_battery_modes=(BATTERY_DEMAND, BATTERY_HOLD),
                    battery_min_window_minutes=15,
                    initial_window_mode=BATTERY_CHARGE,
                    initial_window_remaining_minutes=10,
                ),
            )
        with self.assertRaises(PlannerInputError):
            # limited_demand is not allowed by default.
            plan(
                slots,
                ev,
                bat,
                site,
                PlannerConfig(
                    beam_width=16,
                    battery_min_window_minutes=15,
                    initial_window_mode=BATTERY_LIMITED_DEMAND,
                    initial_window_remaining_minutes=10,
                ),
            )
        # But limited_demand is accepted when allow_limited_discharge is True.
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=16,
                allow_limited_discharge=True,
                battery_min_window_minutes=15,
                initial_window_mode=BATTERY_LIMITED_DEMAND,
                initial_window_remaining_minutes=10,
            ),
        )
        self.assertIn(result.status, (STATUS_FEASIBLE, STATUS_BOUNDED_NO_PLAN))

    def test_validation_initial_remaining_must_be_int_finite(self):
        """initial_window_remaining_minutes must be a finite int in 0..60."""
        slots = _series(minutes=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=10), _battery(), SiteConfig(6, 0, 10)
        for bad in ("5", 5.0, True, None, float("nan")):
            with self.assertRaises(PlannerInputError):
                plan(
                    slots,
                    ev,
                    bat,
                    site,
                    PlannerConfig(
                        beam_width=16,
                        battery_min_window_minutes=15,
                        initial_window_mode=BATTERY_HOLD,
                        initial_window_remaining_minutes=bad,
                    ),
                )
        for bad in (-1, 61, 1000):
            with self.assertRaises(PlannerInputError):
                plan(
                    slots,
                    ev,
                    bat,
                    site,
                    PlannerConfig(
                        beam_width=16,
                        battery_min_window_minutes=15,
                        initial_window_mode=BATTERY_HOLD,
                        initial_window_remaining_minutes=bad,
                    ),
                )

    def test_validation_positive_remaining_requires_mode(self):
        """initial_window_remaining_minutes > 0 must come with initial_window_mode set."""
        slots = _series(minutes=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=10), _battery(), SiteConfig(6, 0, 10)
        with self.assertRaises(PlannerInputError):
            plan(
                slots,
                ev,
                bat,
                site,
                PlannerConfig(
                    beam_width=16,
                    battery_min_window_minutes=15,
                    initial_window_mode=None,
                    initial_window_remaining_minutes=10,
                ),
            )

    def test_validation_remaining_capped_by_min_window(self):
        """initial_window_remaining_minutes > min_window is rejected."""
        slots = _series(minutes=10)
        ev, bat, site = _vehicle(stop_soc_kwh=0, deadline_min=10), _battery(), SiteConfig(6, 0, 10)
        with self.assertRaises(PlannerInputError):
            plan(
                slots,
                ev,
                bat,
                site,
                PlannerConfig(
                    beam_width=16,
                    battery_min_window_minutes=15,
                    initial_window_mode=BATTERY_HOLD,
                    initial_window_remaining_minutes=30,
                ),
            )

    # ------------------------------------------------------------------
    # End-to-end conservation with min>5 (a second invariant sanity check).
    # ------------------------------------------------------------------

    def test_min_15_run_conservation(self):
        """The full conservation laws still hold when min>5 changes mode runs."""
        slots = _series(minutes=30, load_kw=0.6, pv_kw=1.0, price=10)
        ev, bat, site = _vehicle(stop_soc_kwh=2, deadline_min=30, availability=tuple(range(6))), _battery(), SiteConfig(6, 0, 10)
        result = plan(
            slots,
            ev,
            bat,
            site,
            PlannerConfig(
                beam_width=64,
                battery_min_window_minutes=15,
                initial_window_mode=None,
                initial_window_remaining_minutes=0,
            ),
        )
        self.assertEqual(result.status, STATUS_FEASIBLE)
        _assert_first_balance(result, slots, ev, bat, site)


class BatteryWindowContinuationTests(unittest.TestCase):
    """A completed minimum is not a recurring lock, and initial constraints compose."""

    def test_mature_mode_can_switch_after_one_more_step(self):
        """Continuing an already mature Hold for five minutes must not start another fifteen."""
        slots = tuple(replace(slot, import_price=100 if slot.index == 0 else -10) for slot in _series(30, load_kw=0))
        config = PlannerConfig(beam_width=None, allowed_battery_modes=(BATTERY_HOLD, BATTERY_CHARGE), battery_min_window_minutes=15, initial_window_mode=BATTERY_HOLD)
        result = plan(slots, _vehicle(stop_soc_kwh=0), _battery(terminal_value_per_kwh=0), SiteConfig(6), config)
        self.assertEqual(_modes_of(result.actions), (BATTERY_HOLD,) + (BATTERY_CHARGE,) * 5)

    def test_expired_initial_window_does_not_restart_on_same_mode(self):
        """After ten remaining minutes, five further Hold minutes do not renew the timer."""
        slots = tuple(replace(slot, import_price=100 if slot.index < 3 else -10) for slot in _series(30, load_kw=0))
        config = PlannerConfig(beam_width=None, allowed_battery_modes=(BATTERY_HOLD, BATTERY_CHARGE), battery_min_window_minutes=15, initial_window_mode=BATTERY_HOLD, initial_window_remaining_minutes=10)
        result = plan(slots, _vehicle(stop_soc_kwh=0), _battery(terminal_value_per_kwh=0), SiteConfig(6), config)
        self.assertEqual(_modes_of(result.actions), (BATTERY_HOLD,) * 3 + (BATTERY_CHARGE,) * 3)

    def test_conflicting_first_step_and_carried_mode_cannot_be_ignored(self):
        """The self-consumption trial must fail rather than silently override its first-step contract."""
        config = PlannerConfig(battery_min_window_minutes=15, initial_window_mode=BATTERY_CHARGE, initial_window_remaining_minutes=10, initial_battery_mode=BATTERY_DEMAND)
        result = plan(_series(30), _vehicle(stop_soc_kwh=0), _battery(), SiteConfig(6), config)
        self.assertEqual(result.status, STATUS_BOUNDED_NO_PLAN)

    def test_explicit_five_minute_initial_commitment_is_respected(self):
        """Compatibility without a commitment does not justify dropping an explicit initial one."""
        config = PlannerConfig(initial_window_mode=BATTERY_HOLD, initial_window_remaining_minutes=5)
        result = plan(_series(10, price=-10), _vehicle(stop_soc_kwh=0), _battery(terminal_value_per_kwh=0), SiteConfig(6), config)
        self.assertEqual(result.actions[0].battery_mode, BATTERY_HOLD)


if __name__ == "__main__":
    unittest.main(verbosity=2)

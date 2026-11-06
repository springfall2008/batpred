"""First-principles acceptance tests: no reference calls the solver's physics helpers."""

from dataclasses import replace
from itertools import product
import math
import random
import time
import unittest

from car_charging_solver import BatteryConfig, EVConfig, PlannerConfig, SiteConfig, Slot, STATUS_FEASIBLE, STATUS_BOUNDED_NO_PLAN, PlannerInputError, plan


def battery(**kwargs):
    """A synthetic 32 kWh domestic pack with explicit AC limits and cell-side energy."""
    values = dict(capacity_kwh=32, initial_kwh=20, reserve_kwh=3.2, max_charge_kw=3, max_discharge_kw=8.192, charge_efficiency=0.96, discharge_efficiency=0.96, end_min_kwh=3.2, terminal_value_per_kwh=8)
    values.update(kwargs)
    return BatteryConfig(**values)


def vehicle(**kwargs):
    """A fixed 6.77 kW synthetic charger, not a solar-surplus modulating charger."""
    values = dict(battery_kwh=25, start_soc_kwh=0, stop_soc_kwh=3, rate_kw=6.77, efficiency=0.92, deadline_min=60)
    values.update(kwargs)
    return EVConfig(**values)


def series(minutes=60, load_kw=1, pv_kw=0, price=10, step=5):
    """Build complete constant-power forecasts in kWh per interval."""
    return tuple(Slot(i, m, step, load_kw * step / 60, pv_kw * step / 60, price, 0) for i, m in enumerate(range(0, minutes, step)))


class CarPlanningCoreTests(unittest.TestCase):
    """Physical invariants, required use cases and an independent tiny exhaustive oracle."""

    def assert_balance(self, result, slots, ev, bat, site):
        """Check output against conservation laws, not against implementation helpers."""
        self.assertEqual(result.status, STATUS_FEASIBLE, result.diagnostics)
        self.assertEqual(len(result.actions), len(slots))
        self.assertEqual(result.diagnostics.unmet_load_kwh, 0)
        previous_battery, previous_ev = bat.initial_kwh, ev.start_soc_kwh
        for action, slot in zip(result.actions, slots):
            self.assertEqual(action.slot_index, slot.index)
            self.assertAlmostEqual(action.load_kwh, slot.ac_load_kwh, places=8)
            self.assertAlmostEqual(action.pv_kwh, slot.pv_kwh, places=8)
            self.assertAlmostEqual(action.battery_soc_kwh_start, previous_battery, places=8)
            self.assertAlmostEqual(action.ev_soc_kwh_start, previous_ev, places=8)
            self.assertAlmostEqual(action.grid_import_kwh + action.pv_kwh + action.battery_discharge_kwh, action.load_kwh + action.ev_delivered_kwh + action.battery_charge_kwh + action.grid_export_kwh + action.pv_curtailed_kwh, places=8)
            self.assertAlmostEqual(action.battery_soc_kwh_end - previous_battery, action.battery_charge_kwh * bat.charge_efficiency - action.battery_discharge_kwh / bat.discharge_efficiency, places=8)
            self.assertAlmostEqual(action.ev_soc_kwh_end - previous_ev, action.ev_delivered_kwh * ev.efficiency, places=8)
            self.assertGreaterEqual(action.battery_soc_kwh_end, bat.reserve_kwh - 1e-8)
            self.assertLessEqual(action.battery_soc_kwh_end, bat.capacity_kwh + 1e-8)
            self.assertLessEqual(action.ev_soc_kwh_end, max(ev.stop_soc_kwh, ev.start_soc_kwh) + 1e-8)
            self.assertAlmostEqual(action.ev_delivered_kwh, ev.rate_kw * action.ev_on_minutes / 60, places=8)
            self.assertAlmostEqual(sum(s.duration_min for s in action.segments), slot.duration_min, places=8)
            for segment in action.segments:
                self.assertLessEqual(segment.grid_import_kw, site.import_cap_kw + 1e-8)
                if site.load_port_cap_kw is not None:
                    self.assertLessEqual(segment.load_kw + segment.ev_kw, site.load_port_cap_kw + 1e-8)
                self.assertIn(segment.ev_kw, (0, ev.rate_kw))
                self.assertLessEqual(segment.battery_charge_kw, bat.max_charge_kw + 1e-8)
                self.assertLessEqual(segment.battery_discharge_kw, bat.max_discharge_kw + 1e-8)
                self.assertFalse(segment.battery_charge_kw and segment.battery_discharge_kw)
                self.assertAlmostEqual(segment.grid_import_kw + segment.pv_kw + segment.battery_discharge_kw, segment.load_kw + segment.ev_kw + segment.battery_charge_kw + segment.grid_export_kw + segment.curtailed_kw, places=8)
            previous_battery, previous_ev = action.battery_soc_kwh_end, action.ev_soc_kwh_end
        self.assertGreaterEqual(previous_ev, ev.stop_soc_kwh - 1e-8)
        self.assertGreaterEqual(previous_battery, bat.end_min_kwh - 1e-8)

    def test_solar_support_and_later_recovery(self):
        """EV uses actual battery support now, followed by later solar replenishment."""
        slots = series(120, pv_kw=8)
        slots = tuple(replace(s, pv_kwh=5 / 12) if s.start_min < 60 else s for s in slots)
        ev, bat, site = vehicle(availability=tuple(range(12))), battery(), SiteConfig(6, 0, 10)
        result = plan(slots, ev, bat, site, PlannerConfig(beam_width=128))
        self.assert_balance(result, slots, ev, bat, site)
        self.assertTrue(any(a.ev_action == "on" and a.battery_discharge_kwh > 0 for a in result.actions))
        self.assertGreater(result.battery_soc_trace[-1], min(result.battery_soc_trace))
        self.assertTrue(all(a.ev_action == "off" for a in result.actions[12:]))

    def test_cheap_night_preparation(self):
        """Only an earlier grid-charge preparation makes the later fixed EV physically possible."""
        slots = tuple(replace(s, import_price=8.1 if s.start_min < 120 else 19.1) for s in series(180))
        ev = vehicle(deadline_min=180, availability=tuple(range(24, 36)))
        bat, site = battery(initial_kwh=3.2), SiteConfig(6, 0, 10)
        result = plan(slots, ev, bat, site, PlannerConfig(beam_width=256))
        self.assert_balance(result, slots, ev, bat, site)
        self.assertTrue(any(a.battery_mode == "charge" for a in result.actions[:24]))
        self.assertTrue(any(a.ev_action == "on" and a.battery_discharge_kwh > 0 for a in result.actions[24:]))

    def test_next_more_expensive_band_is_used(self):
        """Thirty cheap minutes cannot meet the target; eligible expensive minutes must be used."""
        slots = tuple(replace(s, import_price=8.1 if s.start_min < 30 else 19.1) for s in series())
        ev, bat, site = vehicle(stop_soc_kwh=3.7), battery(), SiteConfig(6, 0, 10)
        result = plan(slots, ev, bat, site)
        self.assert_balance(result, slots, ev, bat, site)
        self.assertTrue(any(a.ev_action == "on" for a in result.actions[6:]))

    def test_house_is_never_shed(self):
        """No EV or battery target justifies silently dropping non-flexible heating demand."""
        slots = series(5, load_kw=12)
        ev, bat, site = vehicle(stop_soc_kwh=0), battery(max_discharge_kw=0), SiteConfig(6)
        self.assertEqual(plan(slots, ev, bat, site).status, STATUS_BOUNDED_NO_PLAN)

    def test_absent_and_late_ev(self):
        """A demand forecast is not licence to schedule energy before expected availability."""
        for availability in ((), (11,)):
            self.assertEqual(plan(series(), vehicle(availability=availability), battery(), SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)

    def test_future_solar_cannot_meet_earlier_deadline(self):
        """Energy after departure cannot satisfy the earlier car target."""
        slots = tuple(replace(s, pv_kwh=12 / 12 if s.start_min >= 60 else 0) for s in series(120))
        bat = battery(initial_kwh=3.2, max_charge_kw=0)
        self.assertEqual(plan(slots, vehicle(), bat, SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)

    def test_fixed_power_partial_target_at_reserve(self):
        """A tiny EV energy target still draws 6.77 kW; averaging it must not bypass the cap."""
        ev = vehicle(stop_soc_kwh=0.01, pause_battery_percent=0, restart_battery_percent=0)
        bat = battery(initial_kwh=3.2, max_charge_kw=0)
        self.assertEqual(plan(series(5, load_kw=1.2), ev, bat, SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)

    def test_partial_target_and_losses(self):
        """Exactly 3 stored kWh, with real full-power active time and correct battery losses."""
        slots, ev, bat, site = series(pv_kw=5), vehicle(), battery(), SiteConfig(6, 0, 10)
        result = plan(slots, ev, bat, site, PlannerConfig(beam_width=128))
        self.assert_balance(result, slots, ev, bat, site)
        self.assertAlmostEqual(result.ev_soc_trace[-1], 3, places=8)
        self.assertAlmostEqual(sum(a.ev_on_minutes for a in result.actions), 3 / 0.92 / 6.77 * 60, places=8)

    def test_domestic_hysteresis_and_initial_state(self):
        """20% cannot restart, but a charger already on may continue above its 15% stop band."""
        bat = battery(capacity_kwh=10, initial_kwh=2, reserve_kwh=1, end_min_kwh=1, max_charge_kw=0)
        ev = vehicle(stop_soc_kwh=0.1, deadline_min=5)
        self.assertEqual(plan(series(5), ev, bat, SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)
        result = plan(series(5), replace(ev, initially_on=True), bat, SiteConfig(6))
        self.assertEqual(result.status, STATUS_FEASIBLE)
        stopped = plan(series(5), replace(ev, initially_on=True), replace(bat, initial_kwh=1.5), SiteConfig(6))
        self.assertEqual(stopped.status, STATUS_BOUNDED_NO_PLAN)

    def test_cooldown(self):
        """The initial off time is explicit; a ten-minute cooldown cannot be shortened to five."""
        ev = vehicle(stop_soc_kwh=0.3, deadline_min=15, initial_off_minutes=0)
        result = plan(series(15), ev, battery(), SiteConfig(6))
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertEqual([a.ev_action for a in result.actions], ["off", "off", "on"])
        self.assertEqual(plan(series(10), replace(ev, deadline_min=10), battery(), SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)

    def test_negative_prices_do_not_overfill_ev(self):
        """Negative prices may fill the house battery, never charge an EV beyond its request."""
        slots, ev, bat, site = series(price=-5), vehicle(stop_soc_kwh=0), battery(initial_kwh=3.2), SiteConfig(6)
        result = plan(slots, ev, bat, site)
        self.assert_balance(result, slots, ev, bat, site)
        self.assertTrue(all(a.ev_action == "off" for a in result.actions))
        self.assertLess(result.total_cost, 0)

    def test_saturation_export_and_curtailment(self):
        """Storage saturation splits the flow; PV export and curtailment never disappear."""
        slots = series(15, load_kw=2, pv_kw=12, step=15)
        ev = vehicle(stop_soc_kwh=0)
        bat = battery(capacity_kwh=10, initial_kwh=9.9, reserve_kwh=1, end_min_kwh=1, charge_efficiency=0.8, max_charge_kw=1)
        result = plan(slots, ev, bat, SiteConfig(6, 1))
        self.assert_balance(result, slots, ev, bat, SiteConfig(6, 1))
        self.assertAlmostEqual(result.battery_soc_trace[-1], 10, places=8)
        self.assertAlmostEqual(result.actions[0].grid_export_kwh, 0.25, places=8)
        self.assertAlmostEqual(result.actions[0].pv_curtailed_kwh, 2.125, places=8)

    def test_short_battery_support_does_not_hide_grid_peak(self):
        """A partly available battery cannot spread its energy to impersonate battery-first dispatch."""
        bat = battery(capacity_kwh=10, initial_kwh=1.1, reserve_kwh=1, end_min_kwh=1, max_charge_kw=0, discharge_efficiency=0.5)
        self.assertEqual(plan(series(15, load_kw=6.1, step=15), vehicle(stop_soc_kwh=0), bat, SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)

    def test_charge_full_rate_not_hidden_by_near_full_pack(self):
        """A short full-rate charge peak is still a peak, even if its whole-step average is small."""
        bat = battery(capacity_kwh=10, initial_kwh=9.99, reserve_kwh=1, end_min_kwh=10)
        self.assertEqual(plan(series(5, load_kw=5), vehicle(stop_soc_kwh=0), bat, SiteConfig(6)).status, STATUS_BOUNDED_NO_PLAN)

    def test_deadline_inside_step_keeps_slot_identity(self):
        """Sub-step departure cuts EV active time, never renumbers availability indices."""
        slots, ev, bat, site = series(10), vehicle(stop_soc_kwh=0.1, deadline_min=7, availability=(1,)), battery(), SiteConfig(6)
        result = plan(slots, ev, bat, site)
        self.assert_balance(result, slots, ev, bat, site)
        self.assertEqual([a.slot_index for a in result.actions], [0, 1])
        self.assertEqual(result.actions[0].ev_action, "off")
        self.assertLessEqual(result.actions[1].ev_on_minutes, 2)

    def test_validation(self):
        """Reject bool coercion, non-finite numbers, malformed state and chronology."""
        for value in (True, float("nan"), float("inf"), "1"):
            with self.assertRaises(PlannerInputError):
                plan((replace(series(5)[0], ac_load_kwh=value),), vehicle(), battery(), SiteConfig(6))
        for config in (PlannerConfig(beam_width=0), PlannerConfig(beam_width=True), PlannerConfig(soc_bucket_kwh=float("nan"))):
            with self.assertRaises(PlannerInputError):
                plan(series(), vehicle(), battery(), SiteConfig(6), config)
        with self.assertRaises(PlannerInputError):
            plan((series(10)[0], replace(series(10)[1], start_min=6)), vehicle(), battery(), SiteConfig(6))
        # Unequal but contiguous durations are valid.
        slots = (Slot(0, 0, 5, 0, 0, 1, 0), Slot(1, 5, 10, 0, 0, 1, 0))
        self.assertEqual(plan(slots, vehicle(stop_soc_kwh=0), battery(), SiteConfig(6)).status, STATUS_FEASIBLE)

    def test_tiny_exhaustive_independent_reference(self):
        """Enumerate all 6^4 lossless schedules with separate arithmetic and no solver helpers."""
        slots = tuple(Slot(i, i * 15, 15, 0.25, pv, rate, 0.5) for i, (pv, rate) in enumerate(zip((0, 0.5, 0, 0.25), (2, 3, 1, 4))))
        ev = vehicle(rate_kw=2, efficiency=1, stop_soc_kwh=1, deadline_min=60)
        for terminal in (0, 1, 4):
            bat = battery(capacity_kwh=100, initial_kwh=50, reserve_kwh=10, end_min_kwh=10, max_charge_kw=3, max_discharge_kw=8, charge_efficiency=1, discharge_efficiency=1, terminal_value_per_kwh=terminal, cycle_cost_per_kwh=0.2)
            best = math.inf
            for choices in product(product(("demand", "hold", "charge"), (False, True)), repeat=4):
                stored, car, cost = 50.0, 0.0, 0.0
                for slot, (mode, on) in zip(slots, choices):
                    if on and car >= 1:
                        break
                    demand = 0.25 + (0.5 if on else 0)
                    deficit = demand - slot.pv_kwh
                    charge = 0.75 if mode == "charge" else min(max(-deficit, 0), 0.75)
                    discharge = min(max(deficit, 0), 2) if mode == "demand" else 0
                    net = deficit + charge - discharge
                    if net > 3 * 0.25 + 1e-9:
                        break
                    stored += charge - discharge
                    car += 0.5 if on else 0
                    cost += max(net, 0) * slot.import_price - max(-net, 0) * slot.export_price + 0.2 * discharge
                else:
                    if car >= 1:
                        best = min(best, cost - terminal * stored)
            result = plan(slots, ev, bat, SiteConfig(3), PlannerConfig(beam_width=None))
            self.assert_balance(result, slots, ev, bat, SiteConfig(3))
            self.assertAlmostEqual(result.total_cost, best, places=8)

    def test_random_conservation(self):
        """Deterministic varied losses, caps and forecasts exercise the returned physical traces."""
        rng = random.Random(5490)
        count = 0
        for _ in range(40):
            slots = series(15, load_kw=rng.uniform(0, 8), pv_kw=rng.uniform(0, 12), price=rng.uniform(-5, 30))
            ev = vehicle(stop_soc_kwh=rng.uniform(0.01, 1), deadline_min=15)
            bat = battery(initial_kwh=rng.uniform(12, 30), charge_efficiency=rng.uniform(0.7, 1), discharge_efficiency=rng.uniform(0.7, 1))
            site = SiteConfig(6, rng.uniform(0, 3), 12)
            result = plan(slots, ev, bat, site)
            if result.status == STATUS_FEASIBLE:
                self.assert_balance(result, slots, ev, bat, site)
                count += 1
        self.assertGreater(count, 15)

    def test_48h_bound_and_early_deadline(self):
        """Assert a deterministic search-work bound; wall time is evidence, not a flaky CI gate."""
        slots = tuple(replace(s, pv_kwh=max(0, 6 * math.sin(math.pi * ((s.start_min % 1440) - 360) / 720)) / 12) for s in series(2880, load_kw=0.6))
        ev, bat, site = vehicle(stop_soc_kwh=3, deadline_min=450), battery(), SiteConfig(6)
        start = time.monotonic()
        result = plan(slots, ev, bat, site, PlannerConfig(beam_width=128))
        print("48h combined search: {:.3f}s, {} transitions".format(time.monotonic() - start, result.diagnostics.transitions_evaluated))
        self.assert_balance(result, slots, ev, bat, site)
        self.assertLessEqual(result.diagnostics.max_frontier_size, 128)
        self.assertLessEqual(result.diagnostics.transitions_evaluated, 576 * 128 * 6)
        self.assertTrue(all(a.ev_action == "off" for a in result.actions[90:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

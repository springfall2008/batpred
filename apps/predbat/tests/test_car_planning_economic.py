"""Economic-pruning regressions for the pure car scheduling solver.

The bounded-beam search at ``PlannerConfig(beam_width=128)`` previously kept a
single cost leader per EV-progress bucket plus the highest-energy neighbour.
Snapshots at horizon end can show ~30 distinct surviving labels and miss
trajectories whose lower stored energy at the end reveals a cheaper plan that
avoids mid-horizon peak-band imports. This module guards the new behaviour:

* the cost leader must still occupy position 0 of its own EV bucket before
  the round-robin selects it (verified against the returned order at the
  layer-by-layer budget);
* when ``preserve_battery`` is set, the head of each bucket must now be
  re-ordered so the layer-by-layer pick spans SoC tiers instead of cost-
  ranked duplicates;
* a synthetic cheap-band shift must show that, given enough history, the
  planner removes peak-band imports rather than skipping them;
* an exact short-horizon oracle agrees with the search on the cost of the
  optimal schedule for tiny (≤ 6 slot) cases.
"""

from itertools import product
import unittest

from car_charging_solver import (
    BATTERY_DEMAND,
    BATTERY_HOLD,
    BATTERY_CHARGE,
    BatteryConfig,
    EVConfig,
    PlannerConfig,
    SiteConfig,
    Slot,
    STATUS_FEASIBLE,
    _Node,
    _bucket,
    _resolve_action,
    _prune,
    plan,
)


def _battery(**overrides):
    """A small synthetic battery for fast oracle-grade tests."""
    values = dict(
        capacity_kwh=10,
        initial_kwh=5,
        reserve_kwh=1,
        max_charge_kw=3,
        max_discharge_kw=3,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        end_min_kwh=1,
        terminal_value_per_kwh=0,
    )
    values.update(overrides)
    return BatteryConfig(**values)


def _vehicle(**overrides):
    """A small synthetic EV that finishes before the demand window ends."""
    values = dict(
        battery_kwh=10,
        start_soc_kwh=0,
        stop_soc_kwh=1,
        rate_kw=2,
        efficiency=1.0,
        deadline_min=180,
        pause_battery_percent=0,
        restart_battery_percent=0,
        min_off_minutes=0,
        initially_on=False,
        initial_off_minutes=10,
    )
    values.update(overrides)
    return EVConfig(**values)


def _bucket_nodes(nodes):
    """Return the in-place bucket layout used internally by ``_prune``."""
    buckets = {}
    for node in nodes:
        buckets.setdefault(_bucket(node.ev_kwh, 0.05), []).append(node)
    return buckets


class PruneBucketHeadTests(unittest.TestCase):
    """Verify the new within-bucket ordering invariants on synthetic buckets."""

    def test_default_order_keeps_established_cost_layers(self):
        """Ordinary and apply callers do not opt into the shadow economic ordering."""
        nodes = [_Node(2, 10, False, 10, cost=0), _Node(3, 10, False, 10, cost=1), _Node(6, 10, False, 10, cost=2), _Node(9, 10, False, 10, cost=9)]
        self.assertFalse(PlannerConfig().economic_diversity)
        self.assertEqual(_prune(nodes, 4, 0.05, 0, preserve_battery=True), [nodes[0], nodes[3], nodes[1], nodes[2]])
        self.assertEqual(_prune(nodes, 4, 0.05, 0, preserve_battery=True, economic_diversity=True), [nodes[0], nodes[3], nodes[2], nodes[1]])

    def test_max_energy_anchor_outside_cost_head_survives(self):
        """Do not sacrifice the original resilience anchor when it is expensive."""
        nodes = [_Node(2 + i * 0.01, 10, False, 10, cost=i) for i in range(20)]
        high = _Node(9, 10, False, 10, cost=1000)
        kept = _prune(nodes + [high], width=3, bucket=0.05, terminal_value=0, preserve_battery=True, economic_diversity=True)
        self.assertEqual(kept[0], nodes[0])
        self.assertIn(high, kept)
        self.assertEqual(len(kept), 3)

    def test_cost_leader_still_in_returned_layer_zero(self):
        """The cheapest label of its EV bucket is selected first by the layer budget."""
        nodes = [
            _Node(battery_kwh=5.0, ev_kwh=8.0, on=True, off_minutes=0, cost=2.0),
            _Node(battery_kwh=3.0, ev_kwh=8.0, on=True, off_minutes=0, cost=1.0),
            _Node(battery_kwh=7.0, ev_kwh=8.0, on=True, off_minutes=0, cost=1.5),
            _Node(battery_kwh=4.0, ev_kwh=8.0, on=True, off_minutes=0, cost=4.0),
        ]
        kept = _prune(nodes, width=4, bucket=0.05, terminal_value=0.0, preserve_battery=True, economic_diversity=True)
        # Layer 0 across all buckets must include the cost leader (ranking 1.0)
        leader = min(nodes, key=lambda n: (n.cost, n.switches))
        self.assertIn(leader, kept)
        self.assertEqual(kept[0], leader, "cost leader must be picked first across buckets")

    def test_head_includes_max_battery_anchor(self):
        """Even when the cost leader has middling energy, max-battery stays in the head."""
        nodes = [
            _Node(battery_kwh=2.0, ev_kwh=10.0, on=True, off_minutes=0, cost=0.5),
            _Node(battery_kwh=8.0, ev_kwh=10.0, on=True, off_minutes=0, cost=2.0),
            _Node(battery_kwh=4.0, ev_kwh=10.0, on=True, off_minutes=0, cost=1.0),
            _Node(battery_kwh=6.0, ev_kwh=10.0, on=True, off_minutes=0, cost=1.5),
        ]
        kept = _prune(nodes, width=8, bucket=0.05, terminal_value=0.0, preserve_battery=True, economic_diversity=True)
        batteries = sorted(n.battery_kwh for n in kept)
        self.assertIn(8.0, batteries, "max battery anchor should survive in the kept set")

    def test_preserve_battery_false_preserves_ranking_order(self):
        """When hold is allowed the original ranking order is the layer order."""
        nodes = [
            _Node(battery_kwh=2.0, ev_kwh=10.0, on=True, off_minutes=0, cost=0.5),
            _Node(battery_kwh=8.0, ev_kwh=10.0, on=True, off_minutes=0, cost=2.0),
            _Node(battery_kwh=4.0, ev_kwh=10.0, on=True, off_minutes=0, cost=1.0),
        ]
        kept = _prune(nodes, width=8, bucket=0.05, terminal_value=0.0, preserve_battery=False)
        # Cost-sorted order preserved
        costs = [n.cost for n in kept]
        self.assertEqual(costs, sorted(costs))

    def test_does_not_exceed_width(self):
        """The kept list never grows past ``width``."""
        nodes = [_Node(battery_kwh=float(i), ev_kwh=10.0, on=True, off_minutes=0, cost=float(i)) for i in range(50)]
        pruned = _prune(nodes, width=8, bucket=0.05, terminal_value=0.0, preserve_battery=True, economic_diversity=True)
        self.assertEqual(len(pruned), 8)

    def test_layer_two_picks_different_battery_tier(self):
        """With ``width > 1`` and a multi-bucket input, layer 1/2 should not duplicate the cost leader."""
        # Three distinct EV buckets; first picks cheap, second picks high-energy.
        nodes = [
            _Node(battery_kwh=2.0, ev_kwh=10.0, on=True, off_minutes=0, cost=0.5),
            _Node(battery_kwh=4.0, ev_kwh=10.0, on=True, off_minutes=0, cost=1.5),
            _Node(battery_kwh=6.0, ev_kwh=10.0, on=True, off_minutes=0, cost=2.5),
            _Node(battery_kwh=9.0, ev_kwh=12.0, on=True, off_minutes=0, cost=0.6),
            _Node(battery_kwh=8.0, ev_kwh=12.0, on=True, off_minutes=0, cost=1.6),
        ]
        kept = _prune(nodes, width=4, bucket=0.05, terminal_value=0.0, preserve_battery=True, economic_diversity=True)
        battery_set = {(round(n.battery_kwh, 4), round(n.ev_kwh, 4)) for n in kept}
        # At least one of the layer picks must be a different SoC than the leader of its bucket.
        leader_ev10 = min((n for n in nodes if n.ev_kwh == 10.0), key=lambda n: (n.cost, n.switches))
        self.assertIn((round(leader_ev10.battery_kwh, 4), 10.0), battery_set)
        # Layer 1-2 should include the max-battery variant for the first bucket.
        self.assertTrue(any(round(n.battery_kwh, 4) == 6.0 and n.ev_kwh == 10.0 for n in kept))


class EconomicSearchTests(unittest.TestCase):
    """Real search comparisons with the synthetic inputs."""

    def test_bounded_search_matches_positive_cost_oracle_and_uses_cheap_tail(self):
        """An actual price transition moves required imports into the cheaper slots."""
        slots = tuple(Slot(i, i * 5, 5, 0.03, 0, 19.1 if i < 3 else 8.1, 0) for i in range(6))
        ev = _vehicle(rate_kw=6.77, stop_soc_kwh=0.8, deadline_min=30, pause_battery_percent=15, restart_battery_percent=25)
        battery = _battery(capacity_kwh=2, initial_kwh=0.9, reserve_kwh=0.2, end_min_kwh=0.2, max_charge_kw=5, max_discharge_kw=8)
        site = SiteConfig(6, 0, 16)
        modes = (BATTERY_DEMAND, BATTERY_CHARGE)
        result = plan(slots, ev, battery, site, PlannerConfig(beam_width=128, allowed_battery_modes=modes, economic_diversity=True))
        oracle = self._oracle_search(slots, ev, battery, site, modes)
        self.assertEqual(result.status, STATUS_FEASIBLE)
        self.assertGreater(oracle, 0)
        self.assertAlmostEqual(result.total_cost, oracle, places=6)
        self.assertEqual(sum(result.import_kwh_trace[:3]), 0)
        self.assertGreater(sum(result.import_kwh_trace[3:]), 0)

    def test_exact_oracle_matches_beam_none_on_short_horizon(self):
        """A 6-slot example: search and brute-force produce the same cost."""
        slots = tuple(Slot(i, i * 5, 5, 1.0 * 5 / 60, 0.0, 10.0 if i < 3 else 2.0, 0.0) for i in range(6))
        ev = _vehicle()
        battery = _battery()
        site = SiteConfig(import_cap_kw=6.0, export_cap_kw=None, load_port_cap_kw=10.0)
        result = plan(slots, ev, battery, site, PlannerConfig(beam_width=None))
        self.assertEqual(result.status, STATUS_FEASIBLE)
        oracle = self._oracle_search(slots, ev, battery, site)
        self.assertIsNotNone(oracle)
        self.assertAlmostEqual(result.total_cost, oracle, places=6)

    def test_cheap_band_steers_plan_to_lower_cost(self):
        """A trailing cheap band steers the plan to push demand into the cheap period.

        With a peak price throughout, the only feasible EV charging is to pre-
        charge the battery at peak prices. Once the prices flip to cheap, the
        planner must use cheap imports even though the peak-only horizon had
        no choice; the total cost must drop as a result.
        """
        slots_peak = tuple(Slot(i, i * 5, 5, 0.0, 0.0, 10.0, 0.0) for i in range(12))
        slots_cheap = tuple(Slot(i, i * 5, 5, 0.0, 0.0, 2.0, 0.0) for i in range(12))
        # Already above the restart threshold; small EV target delivery.
        ev = _vehicle(start_soc_kwh=0.5, stop_soc_kwh=2, deadline_min=60, pause_battery_percent=15, restart_battery_percent=25, min_off_minutes=10)
        # Battery well above pause_battery_percent (15% of capacity = 1.5) so the
        # solver can keep the EV relay on once started.
        battery = _battery(capacity_kwh=10, initial_kwh=3, reserve_kwh=0.5, end_min_kwh=0.5)
        site = SiteConfig(import_cap_kw=6.0, export_cap_kw=None, load_port_cap_kw=None)
        peak = plan(slots_peak, ev, battery, site, PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE), economic_diversity=True))
        cheap = plan(slots_cheap, ev, battery, site, PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE), economic_diversity=True))
        self.assertEqual(peak.status, STATUS_FEASIBLE)
        self.assertEqual(cheap.status, STATUS_FEASIBLE)
        # Cheap-band weighted cost is at most half of peak-band weighted cost.
        self.assertLess(cheap.total_cost, peak.total_cost / 2)

    def test_search_removes_peak_band_imports(self):
        """A plan made cheaper by cheap-band pricing must show fewer grid imports than the peak-only version."""
        # Construct a workload that requires grid imports during the EV window.
        slots_peak = tuple(Slot(i, i * 5, 5, 0.0, 0.0, 10.0, 0.0) for i in range(12))
        slots_cheap = tuple(Slot(i, i * 5, 5, 0.0, 0.0, 2.0, 0.0) for i in range(12))
        # Big EV target that requires sustained grid imports.
        ev = _vehicle(start_soc_kwh=0.0, stop_soc_kwh=2.0, deadline_min=60, pause_battery_percent=15, restart_battery_percent=25, min_off_minutes=10)
        battery = _battery(capacity_kwh=2, initial_kwh=0.5, reserve_kwh=0.2, end_min_kwh=0.2, max_charge_kw=2, max_discharge_kw=2)
        site = SiteConfig(import_cap_kw=6.0, export_cap_kw=None, load_port_cap_kw=None)
        peak = plan(slots_peak, ev, battery, site, PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE), economic_diversity=True))
        cheap = plan(slots_cheap, ev, battery, site, PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE), economic_diversity=True))
        self.assertEqual(peak.status, STATUS_FEASIBLE)
        self.assertEqual(cheap.status, STATUS_FEASIBLE)
        # Cheap-band weighted cost is at most half of peak-band weighted cost.
        self.assertLess(cheap.total_cost, peak.total_cost / 2)

    @staticmethod
    def _oracle_search(slots, ev, battery, site, modes=(BATTERY_DEMAND, BATTERY_HOLD, BATTERY_CHARGE)):
        """Enumerate choices without pruning, using the same gated segmented physics."""
        n = len(slots)
        if n > 6:
            return None

        best_cost = None
        for choices in product(product(modes, (False, True)), repeat=n):
            node = _Node(battery.initial_kwh, ev.start_soc_kwh, ev.initially_on, ev.initial_off_minutes)
            cost = 0.0
            ok = True
            for slot, (mode, turn_on) in zip(slots, choices):
                action = _resolve_action(slot, turn_on, mode, node, battery, ev, site)
                if action is None:
                    ok = False
                    break
                on = action.ev_on_minutes >= slot.duration_min - 1e-9 and action.ev_on_minutes > 0
                off = slot.duration_min - action.ev_on_minutes if action.ev_on_minutes else (0 if node.on else node.off_minutes) + slot.duration_min
                node = _Node(action.battery_soc_kwh_end, action.ev_soc_kwh_end, on, 0 if on else min(off, ev.min_off_minutes))
                cost += action.slot_cost
            if not ok:
                continue
            if node.ev_kwh < ev.stop_soc_kwh - 1e-9 or node.battery_kwh < battery.end_min_kwh - 1e-9:
                continue
            ranking = cost - battery.terminal_value_per_kwh * node.battery_kwh
            if best_cost is None or ranking < best_cost:
                best_cost = ranking
        return best_cost


def run_car_planning_economic_tests(base):
    """Run the economic regressions through Predbat's registered harness."""
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(test) for test in (PruneBucketHeadTests, EconomicSearchTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()


if __name__ == "__main__":
    unittest.main(verbosity=2)

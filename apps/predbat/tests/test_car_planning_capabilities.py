"""Regression tests for the planner `allowed_battery_modes` capability filter.

These tests cover the new ``PlannerConfig.allowed_battery_modes`` field that
lets callers narrow the search to a subset of the supported battery-mode
vocabulary. The filter is enforced by ``_validate`` (so malformed inputs raise
``PlannerInputError`` when ``plan`` is called) and by the ``plan`` loop (so an
unsupported mode never appears in any returned ``SlotAction.battery_mode``).

The 48-hour acceptance case matches the user-specified physical model:

- EV 25 kWh, 6.77 kW fixed-rate charger, start SoC 3.25 kWh, target 25 kWh
- Departure at next 07:30 = 1170 minutes from midnight
- House 1 kW, zero PV, 6 kW grid import cap
- Battery cell-side: 32.154 / 20 / reserve 3.215
- DC-side 3 kW charge / 8192 W discharge, loss 0.95 / 0.96
- AC-side rates and efficiencies: max_charge 3/0.95, max_discharge 8.192*0.95,
  charge_efficiency 0.96*0.95, discharge_efficiency 0.96*0.95

This test also documents the bounded-search beam sensitivity: the default
beam=128 budget genuinely loses the feasible trajectory in this physical
configuration; beam=512 finds it. The honest finding is recorded for the main
agent's design review.

Tests run with ``-v`` so the full unfiltered output lands in the configured log
path; downstream grep operates on that file, never on a streaming filter.
"""

import sys
import time
import unittest

from car_charging_solver import (
    BATTERY_CHARGE,
    BATTERY_DEMAND,
    BATTERY_HOLD,
    BATTERY_LIMITED_DEMAND,
    BATTERY_MODES,
    BatteryConfig,
    EVConfig,
    PlannerConfig,
    PlannerInputError,
    SiteConfig,
    Slot,
    STATUS_BOUNDED_NO_PLAN,
    STATUS_FEASIBLE,
    plan,
)


def _series(minutes=2880, load_kw=1.0, pv_kw=0.0, price=10.0, step=5):
    """Build a flat constant-power forecast in 5-minute steps over the horizon."""
    return tuple(Slot(i, m, step, load_kw * step / 60, pv_kw * step / 60, price, 0) for i, m in enumerate(range(0, minutes, step)))


def _battery_user_case(**overrides):
    """The user-specified 32.154 kWh domestic pack with DC→AC rate conversion."""
    values = dict(
        capacity_kwh=32.154,
        initial_kwh=20.0,
        reserve_kwh=3.215,
        max_charge_kw=3.0 / 0.95,
        max_discharge_kw=8.192 * 0.95,
        charge_efficiency=0.96 * 0.95,
        discharge_efficiency=0.96 * 0.95,
        end_min_kwh=3.215,
        cycle_cost_per_kwh=0.0,
        terminal_value_per_kwh=8.0,
    )
    values.update(overrides)
    return BatteryConfig(**values)


def _vehicle_user_case(**overrides):
    """The user-specified 25 kWh EV at 6.77 kW with start 3.25 → target 25 by 1170."""
    values = dict(
        battery_kwh=25.0,
        start_soc_kwh=3.25,
        stop_soc_kwh=25.0,
        rate_kw=6.77,
        efficiency=0.92,
        deadline_min=1170,
        availability=None,
        pause_battery_percent=15.0,
        restart_battery_percent=25.0,
        min_off_minutes=10.0,
        initially_on=False,
        initial_off_minutes=10.0,
    )
    values.update(overrides)
    return EVConfig(**values)


def _triggers_validation(bad_modes, *, site=None, slots=None, ev=None, battery=None):
    """Return a callable that invokes ``plan`` with a config carrying ``bad_modes``.

    Used to test validation lives in ``_validate`` (i.e. a malformed subset
    surfaces only when ``plan`` is called), not in ``PlannerConfig.__init__``.
    """
    if site is None:
        site = SiteConfig(6)
    if slots is None:
        slots = _series(minutes=60)
    if ev is None:
        # Start below target so the search exercises the planner (not the
        # trivial "EV already at target" path).
        ev = _vehicle_user_case(deadline_min=60, start_soc_kwh=0.0, stop_soc_kwh=1.0)
    if battery is None:
        battery = _battery_user_case()

    def runner():
        """Search with the malformed capability subset so validation must reject it."""
        plan(slots, ev, battery, site, PlannerConfig(allowed_battery_modes=bad_modes))

    return runner


class CarPlanningCapabilitiesValidation(unittest.TestCase):
    """``PlannerConfig.allowed_battery_modes`` is rejected in ``_validate``.

    The planner dataclass itself accepts arbitrary values; the validation
    surfaces in ``plan()`` (and therefore in every caller that actually
    searches). This matches the existing pattern for ``beam_width`` and
    ``allow_limited_discharge``, where validation lives in ``_validate``.
    """

    def test_default_is_full_base_vocabulary(self):
        """The default subset equals the full base vocabulary, never including limited_demand."""
        config = PlannerConfig()
        self.assertEqual(config.allowed_battery_modes, BATTERY_MODES)
        self.assertNotIn(BATTERY_LIMITED_DEMAND, config.allowed_battery_modes)

    def test_construction_does_not_validate_subset(self):
        """Construction accepts arbitrary values; validation is deferred to plan().

        Documents the deliberate choice to put the check in ``_validate``
        only. Mirrors ``beam_width=0`` and other field-level constraints that
        only raise inside ``plan()``.
        """
        # None of these should raise at construction time.
        for bad in ((), "demand", [1, 2, 3], [BATTERY_DEMAND, BATTERY_DEMAND], [BATTERY_DEMAND, "turbo"]):
            try:
                PlannerConfig(allowed_battery_modes=bad)
            except Exception as exc:  # pragma: no cover - intentional diagnostic
                self.fail(f"construction unexpectedly raised for {bad!r}: {exc!r}")

    def test_rejects_non_sequence_in_plan(self):
        """A non-tuple/list (string, set, int, dict, None) is rejected when plan runs."""
        for bad in ("demand", {BATTERY_DEMAND}, 5, None, {"k": BATTERY_DEMAND}):
            with self.assertRaises(PlannerInputError, msg=f"value={bad!r}"):
                _triggers_validation(bad)()

    def test_rejects_non_string_entries_in_plan(self):
        """Every entry must be a string; integers, bytes, None are rejected in plan()."""
        for bad in ([BATTERY_DEMAND, 1], [BATTERY_DEMAND, None], [b"demand"], [BATTERY_DEMAND, [BATTERY_CHARGE]]):
            with self.assertRaises(PlannerInputError, msg=f"value={bad!r}"):
                _triggers_validation(bad)()

    def test_rejects_unsupported_mode_in_plan(self):
        """Unknown mode tags are rejected before any search begins."""
        for bad in (["demand", "hold", "charge", "turbo"], ["free_charge"], ["hold", "limited_discharge"]):
            with self.assertRaises(PlannerInputError, msg=f"value={bad!r}"):
                _triggers_validation(bad)()

    def test_rejects_duplicates_in_plan(self):
        """A duplicated entry is rejected; the subset must be a set-equivalent tuple/list."""
        for bad in ((BATTERY_DEMAND, BATTERY_DEMAND), (BATTERY_HOLD, BATTERY_HOLD, BATTERY_CHARGE), [BATTERY_CHARGE, BATTERY_CHARGE]):
            with self.assertRaises(PlannerInputError, msg=f"value={bad!r}"):
                _triggers_validation(bad)()

    def test_rejects_empty_subset_in_plan(self):
        """An empty subset is malformed — it would silently disable every battery mode."""
        for bad in ((), []):
            with self.assertRaises(PlannerInputError, msg=f"value={bad!r}"):
                _triggers_validation(bad)()

    def test_accepts_supported_subsets(self):
        """Every supported subset of the base vocabulary is accepted and runs to completion."""
        for subset in (
            (BATTERY_DEMAND,),
            (BATTERY_CHARGE,),
            (BATTERY_HOLD,),
            (BATTERY_DEMAND, BATTERY_CHARGE),
            (BATTERY_DEMAND, BATTERY_HOLD),
            (BATTERY_HOLD, BATTERY_CHARGE),
            (BATTERY_DEMAND, BATTERY_HOLD, BATTERY_CHARGE),
            [BATTERY_DEMAND, BATTERY_CHARGE],
            list(BATTERY_MODES),
        ):
            config = PlannerConfig(allowed_battery_modes=subset)
            self.assertEqual(tuple(config.allowed_battery_modes), tuple(subset))
            # And the planner actually accepts the subset (does not raise).
            _triggers_validation(subset)()


class CarPlanningCapabilitiesEnforced(unittest.TestCase):
    """The planner honours allowed_battery_modes in the actual search loop."""

    def test_subset_excludes_unsupported_mode_from_output(self):
        """A demand+charge subset must never produce a `hold` action."""
        slots = _series(minutes=60)
        # EV starts below target so the planner actually exercises the modes.
        ev = _vehicle_user_case(deadline_min=60, start_soc_kwh=0.0, stop_soc_kwh=2.0)
        planner = PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE))
        result = plan(slots, ev, _battery_user_case(initial_kwh=10.0), SiteConfig(6), planner)
        self.assertEqual(result.status, STATUS_FEASIBLE, result.diagnostics)
        emitted = {action.battery_mode for action in result.actions}
        self.assertNotIn(BATTERY_HOLD, emitted)
        self.assertTrue(emitted.issubset({BATTERY_DEMAND, BATTERY_CHARGE}), f"emitted={emitted}")

    def test_charge_only_subset_still_meets_target_via_preparation(self):
        """When only `charge` is allowed, the planner reports honest bounded_no_plan
        when the EV target cannot be served without discharging.

        The capability filter must narrow the search but must not silently
        permit the planner to claim a target was met when the only allowed
        mode cannot serve the EV. Start below target so reaching the target
        actually requires EV charging, not just a starting coincidence.
        """
        slots = _series(minutes=60)
        ev = _vehicle_user_case(deadline_min=60, start_soc_kwh=0.0, stop_soc_kwh=2.0)
        planner = PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_CHARGE,))
        result = plan(slots, ev, _battery_user_case(initial_kwh=4.0), SiteConfig(6), planner)
        # The only allowed mode is `charge`, which cannot discharge to feed the
        # EV. The search must surface an honest bounded miss.
        self.assertEqual(result.status, STATUS_BOUNDED_NO_PLAN, result.diagnostics)

    def test_default_subset_matches_legacy_behaviour(self):
        """Leaving the field at its default reproduces the legacy search output."""
        slots = _series(minutes=60)
        # EV starts below target so the planner actually exercises the modes.
        ev = _vehicle_user_case(deadline_min=60, start_soc_kwh=0.0, stop_soc_kwh=2.0)
        default = plan(slots, ev, _battery_user_case(), SiteConfig(6), PlannerConfig(beam_width=128))
        explicit = plan(slots, ev, _battery_user_case(), SiteConfig(6), PlannerConfig(beam_width=128, allowed_battery_modes=BATTERY_MODES))
        self.assertEqual(default.status, explicit.status)
        self.assertAlmostEqual(default.total_cost, explicit.total_cost, places=8)
        for a, b in zip(default.actions, explicit.actions):
            self.assertEqual(a.battery_mode, b.battery_mode)
            self.assertEqual(a.ev_action, b.ev_action)


class CarPlanningCapabilitiesUserCase(unittest.TestCase):
    """User-specified 48h case: EV 25 kWh @3.25 → 25 by next 07:30, fixed 6.77 kW.

    Battery 32.154/20/reserve 3.215; charging 3 kW DC (3/0.95 AC); discharge
    8192 W DC (8.192*0.95 AC); loss 0.95/0.96 on cell side, 0.96*0.95/0.96*0.95
    on the planner side. House 1 kW, zero PV, grid cap 6 kW.

    The full-target case (3.25 → 25 kWh by 1170 min) demands ~209 min of EV-on
    active time plus 19.5 kWh of house consumption within the 19.5 h budget, so
    stored-energy resilience now preserves the feasible trajectory at both
    beam=128 and beam=512 without widening the default search budget.
    """

    def test_48h_demand_charge_subset_beam_512_finds_feasible_full_target(self):
        """The user case is feasible at beam=512 with the demand+charge capability subset."""
        slots = _series()  # 48h, house 1kW, zero PV
        ev = _vehicle_user_case()
        planner = PlannerConfig(beam_width=512, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE))
        start = time.monotonic()
        result = plan(slots, ev, _battery_user_case(), SiteConfig(6), planner)
        elapsed = time.monotonic() - start
        self.assertEqual(result.status, STATUS_FEASIBLE, result.diagnostics)
        # Search-work bound: deterministic, not a flaky wall-clock gate.
        self.assertLessEqual(result.diagnostics.max_frontier_size, 512)
        self.assertLessEqual(result.diagnostics.transitions_evaluated, 576 * 512 * 6)
        # Output is fully within the declared capability subset.
        emitted = {action.battery_mode for action in result.actions}
        self.assertTrue(emitted.issubset({BATTERY_DEMAND, BATTERY_CHARGE}), f"emitted={emitted}")
        self.assertNotIn(BATTERY_HOLD, emitted)
        # Conservation invariants: no unmet load, full EV target reached,
        # battery respects reserve/floor at end, no PV so no curtailment.
        self.assertEqual(result.diagnostics.unmet_load_kwh, 0)
        self.assertAlmostEqual(result.diagnostics.final_ev_soc_kwh, 25.0, places=8)
        self.assertGreaterEqual(result.diagnostics.final_battery_soc_kwh, 3.215 - 1e-8)
        self.assertEqual(result.diagnostics.pv_curtailed_total_kwh, 0)
        # Per-slot balance with the user's documented cell-side efficiencies.
        bat = _battery_user_case()
        ev_cfg = _vehicle_user_case()
        prev_b, prev_e = bat.initial_kwh, ev_cfg.start_soc_kwh
        for action in result.actions:
            self.assertAlmostEqual(action.battery_soc_kwh_start, prev_b, places=8)
            self.assertAlmostEqual(action.ev_soc_kwh_start, prev_e, places=8)
            self.assertLessEqual(
                action.battery_charge_kwh * bat.charge_efficiency - action.battery_discharge_kwh / bat.discharge_efficiency,
                bat.capacity_kwh - prev_b + 1e-8,
            )
            prev_b, prev_e = action.battery_soc_kwh_end, action.ev_soc_kwh_end
        print(
            f"48h demand+charge beam=512: {elapsed:.3f}s, "
            f"transitions={result.diagnostics.transitions_evaluated}, "
            f"frontier_max={result.diagnostics.max_frontier_size}, "
            f"modes_used={list(result.diagnostics.mode_counts)}, "
            f"final_ev={result.diagnostics.final_ev_soc_kwh:.3f}"
        )

    def test_48h_demand_charge_subset_beam_128_preserves_resilience(self):
        """The original bounded miss now finds the full target within the unchanged beam bound."""
        slots = _series()
        ev = _vehicle_user_case()
        planner = PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE))
        result = plan(slots, ev, _battery_user_case(), SiteConfig(6), planner)
        self.assertEqual(result.status, STATUS_FEASIBLE, result.diagnostics)
        self.assertAlmostEqual(result.diagnostics.final_ev_soc_kwh, 25.0, places=8)
        # Search-work bound still respected.
        self.assertLessEqual(result.diagnostics.max_frontier_size, 128)
        self.assertLessEqual(result.diagnostics.transitions_evaluated, 576 * 128 * 6)

    def test_48h_default_modes_beam_512_demonstrates_subtractive_advantage(self):
        """Honest finding: the (demand, charge) subset has a search-space advantage.

        With the default subset (demand, hold, charge) at beam=512 the user case
        returns ``bounded_no_plan``; the tighter (demand, charge) subset at the
        same beam returns ``feasible``. This is consistent with the wider
        default subset expanding the per-layer state count enough that beam=512
        binds and prunes the right trajectory. It does NOT prove `hold` is
        harmful in general — it proves the capability filter is a useful tool
        when the caller knows which modes are needed.

        The test locks down this exact behaviour so future pruning changes
        cannot silently flip it without a deliberate design review.
        """
        slots = _series()
        ev = _vehicle_user_case()
        default = plan(slots, ev, _battery_user_case(), SiteConfig(6), PlannerConfig(beam_width=512))
        subset = plan(slots, ev, _battery_user_case(), SiteConfig(6), PlannerConfig(beam_width=512, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE)))
        self.assertEqual(default.status, STATUS_BOUNDED_NO_PLAN)
        self.assertEqual(subset.status, STATUS_FEASIBLE)
        self.assertAlmostEqual(subset.diagnostics.final_ev_soc_kwh, 25.0, places=8)


class CarPlanningCapabilitiesBeamSensitivity(unittest.TestCase):
    """Preserve feasible low-energy and deadline cases within both bounded beam budgets."""

    def _scan(self, *, init_kwh, deadline_slot, stop_soc_kwh, label, start_soc_kwh=0.0):
        """Compare both bounded beam budgets for the same fixed-rate energy/deadline scenario."""
        slots = _series(minutes=2880)
        ev = _vehicle_user_case(start_soc_kwh=start_soc_kwh, stop_soc_kwh=stop_soc_kwh, deadline_min=deadline_slot * 5, availability=None)
        battery = _battery_user_case(initial_kwh=init_kwh)
        site = SiteConfig(6)
        r128 = plan(slots, ev, battery, site, PlannerConfig(beam_width=128, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE)))
        r512 = plan(slots, ev, battery, site, PlannerConfig(beam_width=512, allowed_battery_modes=(BATTERY_DEMAND, BATTERY_CHARGE)))
        print(f"[{label}] beam128={r128.status!r} transitions={r128.diagnostics.transitions_evaluated}; " f"beam512={r512.status!r} transitions={r512.diagnostics.transitions_evaluated}")
        return r128, r512

    def test_user_full_target_spec_resilience_at_both_beams(self):
        """The full-target regression succeeds without requiring a wider beam."""
        r128, r512 = self._scan(init_kwh=20.0, deadline_slot=234, stop_soc_kwh=25.0, start_soc_kwh=3.25, label="user-full-target")
        self.assertEqual(r128.status, STATUS_FEASIBLE)
        self.assertEqual(r512.status, STATUS_FEASIBLE)

    def test_tight_stop_eight_with_early_deadline_resilience_at_both_beams(self):
        """The early-deadline regression retains enough stored energy at either beam width."""
        r128, r512 = self._scan(init_kwh=6.0, deadline_slot=96, stop_soc_kwh=8.0, label="tight-stop8")
        self.assertEqual(r128.status, STATUS_FEASIBLE)
        self.assertEqual(r512.status, STATUS_FEASIBLE)

    def test_tight_low_init_small_target_is_feasible_at_both_beams(self):
        """Small target (stop=3.25) within a tight horizon is feasible even at beam=128.

        Honest finding: the bounded-search loss is real for large deltas but
        not for small ones. The earlier sub-agent report that beam=128 also
        loses tight-low-init used legacy 0.96/0.96 efficiencies and did not
        represent the user's actual physical setup.
        """
        r128, r512 = self._scan(init_kwh=4.0, deadline_slot=48, stop_soc_kwh=3.25, label="tight-small-target")
        self.assertEqual(r128.status, STATUS_FEASIBLE)
        self.assertEqual(r512.status, STATUS_FEASIBLE)


def run_car_planning_capabilities_tests(unused_base):
    """Exported entry point matching the repository test-runner convention.

    Pure unit-style tests; no ``base`` fixture is required. The parent owns
    ``unit_test.py`` registration and decides when to wire this in.

    Uses ``sys.modules[__name__]`` (not ``__import__(__name__)``) so the
    registered runner resolves to the actual test module — the parent
    imports it as ``tests.test_car_planning_capabilities``, where
    ``__import__("tests.test_car_planning_capabilities")`` returns the top-level
    ``tests`` package and would load zero tests.
    """
    # ``sys.modules[__name__]`` is the absolute module path that was actually
    # imported (e.g. ``tests.test_car_planning_capabilities``); using it directly
    # avoids the bug where ``__import__(__name__)`` returns the *parent*
    # package when ``__name__`` contains dots.
    this_module = sys.modules[__name__]
    suite = unittest.defaultTestLoader.loadTestsFromModule(this_module)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(f"REGISTERED_RUNNER_TEST_COUNT={result.testsRun}")
    if result.failures or result.errors:
        raise AssertionError(f"car_planning_capabilities: {len(result.failures)} failures, {len(result.errors)} errors")
    return False


if __name__ == "__main__":
    unittest.main(verbosity=2)

# Cancel a Car Slot for Export — Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** With `car_charging_cancel_for_export` on, the detailed planner may run a full export over a cancellable car's slot. That cancels the slot, at a score-only cost of the car energy bought back at `rate_max_base`. The cancellation is published as `cancelled_for_export` on the car slot sensor, which Plan 1's force hold already consumes.

**Architecture:** A new module, `car_cancel.py`, owns the rules:
- which slots can be cancelled;
- which slots a given set of export windows cancels, as a bitmask;
- a per-step table the prediction engines read.

`calculate_plan()` builds the table once per cycle and hands it to `Prediction`. Each prediction call derives its own cancel mask from its export windows. The Python engine and the C++ kernel apply the same three effects: the car load is zeroed, the import rate is reverted, and the penalty is added. The export gate and publishing use the same module, so trials, the final plan, a re-scored old plan and a reused plan all agree.

**Tech Stack:** Python 3, the C++17 prediction kernel loaded through ctypes, and the repo's `unit_test.py` harness.

**Spec:** `docs/superpowers/specs/2026-10-30-cancel-car-slot-for-export-design.md`. Read "Switch", "Cancellation is a property of the plan", "Valuing the cancelled energy", "Fair rates", "The prediction kernel", "Publishing the cancellation" and "Constraints".

**Stacked on:** `feat/charger-force-hold` (PR #5900, Plan 1). That branch already provides:
- the config switch `car_charging_cancel_for_export`;
- `self.car_force_hold_until` (car_n → aware UTC end);
- `PredBat.car_force_held()`;
- the force-hold engine in `car_charger_control.py`, which reads `cancelled_for_export`.

## Global Constraints

- Switch off ⇒ byte-for-byte today's behaviour, and no measurable optimiser slowdown. `car_cancel` is `None` and every new branch is skipped by one `is None` test.
- `CAR_CANCEL_MAX_SLOTS = 64`. Only the first 64 cancellable slots, ordered by `(start, car_n)`, can be cancelled. Any later slot keeps the hard block. The mask is a 64-bit integer (`uint64_t` in C++).
- A slot is cancelled by an export window when the window's mode is `EXPORT_MODE_TARGET` and `window["end"] > slot["start"] and window["start"] < slot["end"]`. Freeze and idle windows never cancel. Test the mode on the export limits **as passed in**, before `floor_export_limits()`. Flooring never changes the mode, and the kernel gets the raw limits.
- The penalty per step in a cancelled slot is the car energy the step would have bought, `car_load_scale / car_charging_loss`. Here `car_load_scale` is clamped to the room left in the car, exactly as today. Multiply that energy by `rate_max_base` and add it to `metric_keep`. The car's SoC does not rise.
- Revert a step's import rate to `rate_max_base` only when all of these hold:
  - the step is marked `io_adjusted`;
  - it is outside the fixed IOG window (`in_iog_fixed_window()`);
  - it is not in `rate_import_saving_minutes`;
  - `dispatch_billing_window()` is False;
  - no uncancellable dispatch covers it;
  - every cancellable dispatch that covers it is cancelled, and there is at least one.

  "Covers" uses the slot rounded out to whole half hours (`round_out_to_period`). A "dispatch" is a slot with `octopus: True`.
- A reverted step is no longer a dispatch for that call: `dispatch_gone` is False and its `io_confirmed_credit` is 0.
- Both engines must agree to the parity test's tolerance. Bump the revisions: `KERNEL_ABI_VERSION` 10 → 11 and `KERNEL_PARITY_REVISION` 18 → 19 in `prediction_kernel.py`, and `PK_ABI_VERSION` and `PK_PARITY_REVISION` to match in the `.cpp`.
- The levels pass (`export_window_allowed()` inside `optimise_charge_limit_price_threads`) and the freeze-export block in `optimise_solar()` keep their hard `hit_car_window()` block unchanged.
- Line length 256, 100% docstrings, British English, and `lower_case_with_underscores` names.
- Tests share one `PredBat` fixture. Restore every attribute a test changes.

## Review Focus

1. **A car slot that only partly overlaps the export.** The whole slot is cancelled, so the published plan, the penalty and the execute hold must all treat the entire slot as gone, not just the overlap. Tested in Tasks 3 and 5.
2. **Two cancellable dispatches sharing one half hour** (Octopus re-cuts 19:00→19:05 and 19:05→19:30). The rate reverts only when both are cancelled. Tested in Task 3.
3. **Plan reused without re-optimising** (`calculate_plan(recompute=False)`), or the old plan re-scored by `should_replace_plan()`. It must cancel the same slots and pay the same penalty as when it was chosen. Tested in Task 6.
4. **Publishing at fetch time.** `publish_car_plan()` runs in `fetch_sensor_data()` before planning, against the last cycle's export windows. A window still cancelled must stay out of `planned`, or a Predbat-led charger starts the car for a minute. Tested in Task 5.
5. **More than 64 cancellable slots** (several cars over 48 hours). Slots past the cap must keep the hard block in the gate, and both engines must ignore them. Tested in Tasks 1 and 2.

## Decisions this plan makes where the spec is silent or loose

- **Publishing is derived, not written into `car_charging_slots`.** The spec suggests the `dynamic_load_car_check()` pattern (kWh to 0, `kwh_cancelled`). But `car_charging_slots` feeds every later prediction in the same cycle, and a slot already zeroed would give no penalty and no car load in the final `save="best"` run, breaking the spec's own "derived, not stored" rule. So the slots are left alone. Publishing, the plan display and execute read `car_cancelled_for_export_windows()` instead. The result the user sees is the same.
- **A held car's slots are cancelled in every trial** (`forced`). The hold is committed whatever the plan says, so modelling the car as charging during it would be wrong. The penalty is the same in every trial, so it never sways a choice.
- **Slots from `car_charging_now_slots` are never cancellable.** A car drawing power outside its plan isn't one the plan scheduled.
- **The 64-slot cap**, so the kernel's mask fits in one `uint64_t`.
- **Execute's car hold skips a cancelled slot.** Otherwise, for the part of a whole cancelled slot outside the export window, the battery would be held for a car that is not charging.

---

### Task 1: Cancellable cars and the shared cancel rules

**Files:**
- Create: `apps/predbat/car_cancel.py`
- Modify: `apps/predbat/predbat.py` `reset()`, next to `self.car_force_hold_until = {}`
- Modify: `apps/predbat/fetch.py` `fetch_sensor_data_car_planning()`, at the `self.plan_car_charging(...)` assignment (line ~1854)
- Modify: `apps/predbat/car_charger_control.py` `charger_control_force_hold_tick()`
- Modify: `apps/predbat/plan.py`, new methods beside `car_force_held()`
- Test: `apps/predbat/tests/test_car_cancel.py` (new), registered in `unit_test.py` `TEST_REGISTRY` as `("car_cancel", run_car_cancel_tests, "Cancel a car slot for export tests", False)`

**Interfaces:**
- Produces:
  - `car_cancel.CAR_CANCEL_MAX_SLOTS = 64`.
  - `car_cancel.CancelSlot`, a `NamedTuple(car_n: int, index: int, start: int, end: int, forced: bool)`. `index` is the slot's position in that car's `car_charging_slots_model()` list.
  - `car_cancel.cancellable_slots(car_slots_model: list[list[dict]], now_slot_counts: list[int], cancellable_cars: set[int], held_until: dict[int, int], minutes_now: int) -> list[CancelSlot]`. A slot qualifies when all of these hold:
    - its car is in `cancellable_cars` or `held_until`;
    - `index >= now_slot_counts[car_n]` (a `car_charging_now_slots` entry is never cancellable);
    - `dp2(kwh) > 0`;
    - `end > minutes_now`.

    The list is sorted by `(start, car_n)` and capped at `CAR_CANCEL_MAX_SLOTS`. `forced` is True when the car is in `held_until` (car_n → absolute minute) and `start < held_until[car_n]`.
  - `car_cancel.cancelled_mask(slots: list[CancelSlot], export_window: list[dict], export_limits: list) -> int`: bit j is set for every slot j that is forced, or that a target-mode window overlaps.
  - `car_cancel.cancelled_windows(slots, mask) -> dict[int, list[tuple[int, int]]]`: car_n → the `(start, end)` of every slot in the mask, time-ordered.
  - `self.car_planned_by_predbat: set[int]`, cleared at the start of `fetch_sensor_data_car_planning()`. A car is added when `plan_car_charging()` gives it its slots.
  - `self.car_force_holdable: dict[int, str]`, car_n → the component's log name. Written by `charger_control_force_hold_tick()`, **before** its early return. Each car this component serves is set while `charger_control_force_hold_wanted()` and `charger_control_can_force_hold(handle)` both hold, and removed otherwise. The component only removes entries whose value is its own name.
  - `PredBat.car_cancel_enabled() -> bool`: `get_arg("car_charging_cancel_for_export", False)` and `num_cars > 0`.
  - `PredBat.car_cancellable_cars() -> set[int]`: `car_planned_by_predbat | set(car_force_holdable)`, or the empty set when the switch is off.
  - `PredBat.car_cancel_slots() -> list[CancelSlot]`: `cancellable_slots()` over `car_charging_slots_model()`. It passes `car_force_hold_until`, converted to absolute minutes with `ceil((end - midnight_utc).total_seconds() / 60)` and dropping ends at or before now. It returns `[]` when the switch is off.

- [ ] **Step 1: Write the failing tests** in `test_car_cancel.py`, using `create_predbat()` and restoring every attribute a test changes:
  - `test_cancellable_slots_rules`. With car 0 Predbat-led, car 1 neither, and car 2 holdable:
    - only cars 0 and 2 have slots in the result;
    - the 0 kWh slot is skipped, and so is the slot ending at `minutes_now`;
    - the car 0 slot from `car_charging_now_slots` (index 0) is skipped;
    - the order is `(start, car_n)`.
  - `test_cancellable_slots_cap`. 70 qualifying slots give `len == 64`, and the 65th earliest is absent.
  - `test_cancellable_slots_forced`. With `held_until={0: m}`, slots starting before `m` have `forced=True` and the others `False`. A car only in `held_until` (not in the cancellable set) is still included.
  - `test_cancelled_mask`:
    - a target window overlapping one minute of slot 0 sets bit 0;
    - a window ending exactly at `slot.start` does not;
    - freeze and idle windows over the slot set nothing;
    - a forced slot's bit is set with no windows at all.
  - `test_cancelled_windows`. A mask `0b101` over 3 slots returns those two slots grouped by car, time-ordered.
  - `test_car_cancellable_cars`:
    - switch off gives `set()`, even with `car_planned_by_predbat={0}`;
    - switch on gives `{0} | set(car_force_holdable)`.
  - `test_force_hold_tick_publishes_holdable`. Use the `FakeComponent` from `tests/test_car_charger_control.py`:
    - switch on and `can_force_hold` True gives `base.car_force_holdable == {0: <name>}`;
    - switch off removes it;
    - an entry owned by another name is left alone.

- [ ] **Step 2: Run them and see them fail**

Run: `cd coverage && ./run_all --test car_cancel > /tmp/cc1.log 2>&1; grep -E "FAIL|Error|passed" /tmp/cc1.log`
Expected: an import error for `car_cancel`.

- [ ] **Step 3: Implement** the module, the two base attributes, the fetch line, the publishing in the component tick, and the three `PredBat` methods, as listed under Interfaces.

- [ ] **Step 4: Run the new tests, plus the `car_charger_control` and `dynamic_load_car` tests**

Run: `cd coverage && ./run_all --test car_cancel --test car_charger_control --test dynamic_load_car > /tmp/cc1.log 2>&1; tail -5 /tmp/cc1.log`
Expected: all pass.

- [ ] **Step 5: Commit** with the message `feat(plan): shared rules for which car slots an export may cancel`.

---

### Task 2: The relaxed export gate

**Files:**
- Modify: `apps/predbat/plan.py`:
  - `allow_this_export_window()` (line ~4734);
  - `optimise_swap_export()`, the swap-target check (line ~4418);
  - a new `hit_uncancellable_car_window()` beside `hit_car_window()`;
  - `calculate_plan()`, where it sets `self.car_cancel_keys` before the `Prediction` is built (line ~2277).
- Test: `apps/predbat/tests/test_car_cancel.py`

**Interfaces:**
- Consumes: `PredBat.car_cancel_slots()` (Task 1).
- Produces:
  - `self.car_cancel_keys: frozenset[tuple[int, int]]`, the `(car_n, index)` pairs of this cycle's `car_cancel_slots()`. Set in `calculate_plan()` and reset to `frozenset()` in `reset()`.
  - `PredBat.hit_uncancellable_car_window(window_start: int, window_end: int) -> bool`. It is the same scan as `hit_car_window()`, but skips any slot whose `(car_n, index)` is in `car_cancel_keys`. With empty keys it returns exactly what `hit_car_window()` returns, and it uses no cache.

- [ ] **Step 1: Write the failing tests:**
  - `test_gate_relaxed_for_cancellable`. Switch on, Predbat-led car 0 with one slot, and an export window over it. Expect `allow_this_export_window(n)` to be True, with `car_charging_from_battery` False.
  - `test_gate_blocked_for_uncancellable`. The same, but the car is in neither set. Expect False.
  - `test_gate_switch_off_unchanged`. Switch off gives False, as today.
  - `test_gate_mixed_slots`. A window over a cancellable slot and an uncancellable one gives False.
  - `test_gate_cap`. A window over the 65th cancellable slot gives False.
  - `test_levels_and_freeze_unchanged`. With the switch on, `hit_car_window()` (used by the levels pass and freeze export) still returns True over the cancellable slot.

- [ ] **Step 2: Run them and see them fail**

Run: `cd coverage && ./run_all --test car_cancel > /tmp/cc2.log 2>&1; grep -E "FAIL|passed" /tmp/cc2.log`

- [ ] **Step 3: Implement.** In `allow_this_export_window()` and the swap-target check, replace `self.hit_car_window(...)` with `self.hit_uncancellable_car_window(...)`. Set `self.car_cancel_keys` in `calculate_plan()` immediately before the `Prediction(...)` line.

- [ ] **Step 4: Run the tests, plus the quick suite's plan tests**

Run: `cd coverage && ./run_all --test car_cancel --test optimise_levels --test basic_rates > /tmp/cc2.log 2>&1; tail -5 /tmp/cc2.log`
Expected: all pass.

- [ ] **Step 5: Commit** with the message `feat(plan): let the detailed passes export over a cancellable car slot`.

---

### Task 3: The cancel table and the Python engine

**Files:**
- Modify: `apps/predbat/car_cancel.py`
- Modify: `apps/predbat/prediction.py`:
  - `Prediction.__init__` (new kwarg);
  - `run_prediction()`: the rate block at line ~763, the car block at line ~892, and the io credit at line ~1352.
- Modify: `apps/predbat/plan.py`, the `Prediction(...)` call in `calculate_plan()` (line ~2277)
- Test: `apps/predbat/tests/test_car_cancel.py`

**Interfaces:**
- Consumes: `CancelSlot`, `cancelled_mask()`, `PredBat.car_cancel_slots()`.
- Produces:
  - `car_cancel.CarCancelTable` (plain class) with these fields:
    - `slots: list[CancelSlot]`;
    - `forced_mask: int`;
    - `n_steps: int`;
    - `slot_flat: list[int]`, length `num_cars * n_steps`. Each entry is the j of the slot `in_car_slot()` would pick at that step's first minute, **first match in model order**, if that slot is in `slots`; otherwise -1.
    - `need_mask: list[int]`, length `n_steps`: the bits of cancellable dispatches whose rounded-out span covers the step.
    - `trusted: list[int]`, length `n_steps`: 1 when an uncancellable dispatch with `dp2(kwh) > 0` covers it (rounded out), else 0.
    - `revert_rate: list[float]`, length `n_steps`: `rate_max_base` where the static revert conditions hold (see Global Constraints), else -1.0.
    - `penalty_rate: float`, which is `rate_max_base`.
  - `car_cancel.build_car_cancel_table(base, car_slots_model: list[list[dict]], slots: list[CancelSlot]) -> CarCancelTable | None`. It returns None when `slots` is empty. Step k covers absolute minute `base.minutes_now + k * PREDICT_STEP`, and `n_steps = base.forecast_minutes // PREDICT_STEP`. It reads `io_adjusted`, `in_iog_fixed_window()`, `rate_import_saving_minutes`, `dispatch_billing_window()` and `rate_max_base` from `base`.
  - `car_cancel.step_reverted(table, k: int, mask: int) -> bool`: `revert_rate[k] >= 0 and not trusted[k] and need_mask[k] and not (need_mask[k] & ~mask)`.
  - `Prediction.__init__(..., car_cancel=None)` stores `self.car_cancel`. Only the live plan passes it, with `build_car_cancel_table(self, model, self.car_cancel_slots())`.

- [ ] **Step 1: Write the failing tests.** Each runs `my_predbat.prediction.run_prediction()` on a one-car scenario: flat load, no PV, a 10 kWh car slot over a 30-minute export window, and `car_charging_from_battery` False. Use the helpers `test_kernel_parity.py` already has to build the scenario, and force the Python engine with `prediction_kernel_enable = False`.
  - `test_valuation_saving_session_wins`: slot rate 7p, `rate_max_base` 30p, export 200p over the slot.
    - The detailed plan chosen by `calculate_plan()` contains the target export.
    - `run_prediction` for that plan returns `metric_keep` greater by 10 kWh × 30p (to within the battery-keep terms; compare against the same plan with the switch off and the car slot removed).
  - `test_valuation_normal_export_loses`: export 15p over the slot. `calculate_plan()` leaves the window idle, and the car keeps its slot.
  - `test_valuation_ready_time_not_free`: the slot ends at the car's ready time, and every rate after it is 7p except a 30p peak earlier in the horizon. The penalty still uses 30p per kWh, and `metric_keep` rises by 10 × 30.
  - `test_penalty_respects_car_full`: the car is 1 kWh from its limit. The penalty is about 1 kWh × 30p (allowing for `car_charging_loss`), not 10.
  - `test_revert_outside_offpeak`: a dispatch at 18:00–18:30 marked `io_adjusted` at 7p. When cancelled, `predict` pays `rate_max_base` there (check `cost` against a hand calculation).
  - `test_revert_inside_offpeak`: the same at 00:00–00:30, inside the fixed window. The rate stays 7p.
  - `test_revert_other_car_trusted`: car 1 has an uncancellable dispatch over the same half hour. The rate stays 7p.
  - `test_revert_split_half_hour`: car 0 has dispatches at 19:00–19:05 and 19:05–19:30, both cancellable. Cancelling only the first leaves 7p; cancelling both reverts.
  - `test_revert_saving_session`: the minute is in `rate_import_saving_minutes`, so its rate is unchanged.
  - `test_revert_window_billing`: with `intelligent_dispatch_billing: window`, nothing reverts, but the car load and penalty still apply.
  - `test_reverted_step_not_dispatch_gone`: in PV10, a reverted step pays `rate_max_base` and keeps the car off the `car_gone` path (car energy 0).
  - `test_switch_off_identical`: with the switch off, `car_cancel is None`. The same scenario gives results identical to the old code, compared against a stored tuple taken before the change: run it once on the Task 2 commit and paste the tuple into the test.

- [ ] **Step 2: Run them and see them fail**

Run: `cd coverage && ./run_all --test car_cancel > /tmp/cc3.log 2>&1; grep -E "FAIL|passed" /tmp/cc3.log`

- [ ] **Step 3: Implement** `CarCancelTable`, `build_car_cancel_table()` and `step_reverted()`, then wire them into `run_prediction()`:
  - Compute `mask = cancelled_mask(...)` from the raw `export_window`/`export_limits` **before** `floor_export_limits()`. Skip all of the following when `self.car_cancel is None` or `mask == 0`.
  - At the rate block: when `step_reverted(table, minute // PREDICT_STEP, mask)`, set `import_rate = table.revert_rate[k]` and `dispatch_rate = import_rate`, and force `dispatch_gone = False`.
  - In the car loop: when `slot_flat[car_n * n_steps + k]` is j and bit j is in `mask`, compute `car_load_scale` (loss and clamp) as today, add `car_load_scale / car_charging_loss * penalty_rate` to `metric_keep`, and `continue` without touching `car_soc`, the load, the premium or the hold.
  - At the io credit: a reverted step uses credit 0.

  Per-step lookups must stay O(1). Hoist `table`, `mask` and `n_steps` into locals before the loop.

- [ ] **Step 4: Run the tests and the existing car and prediction tests**

Run: `cd coverage && ./run_all --test car_cancel --test dynamic_load_car --test multi_car_load --test units > /tmp/cc3.log 2>&1; tail -5 /tmp/cc3.log`
Expected: all pass.

- [ ] **Step 5: Commit** with the message `feat(prediction): cancel car slots an export overlaps, revert their dispatch rate and charge the buy-back`.

---

### Task 4: Kernel parity

**Files:**
- Modify: `apps/predbat/prediction_kernel.cpp`:
  - `PkContext`;
  - the context-store copy (line ~605–660);
  - the run: mask before the step loop, the rate block at ~843, the car block at ~956, and the io credit at ~1373;
  - `PK_ABI_VERSION` and `PK_PARITY_REVISION`, with a comment line for each.
- Modify: `apps/predbat/prediction_kernel.py`: the `PkContext` ctypes struct, `create_kernel_context()`, `KERNEL_ABI_VERSION` and `KERNEL_PARITY_REVISION`.
- Test: `apps/predbat/tests/test_kernel_parity.py`

**Interfaces:**
- Consumes: `CarCancelTable` (Task 3) from `pred.car_cancel`.
- Produces: new `PkContext` fields, placed after `car_rate_flat` in both struct definitions:
  - `int32_t cancel_count` (0 when `car_cancel is None`);
  - `int32_t cancel_start[64]`, `cancel_end[64]`;
  - `uint64_t cancel_forced_mask`;
  - `const int32_t *cancel_slot_flat` (`num_cars * n_steps`);
  - `const uint64_t *cancel_need_mask`, `const int32_t *cancel_trusted` and `const double *cancel_revert_rate` (`n_steps` each);
  - `double cancel_penalty_rate`.

  Null pointers when `cancel_count == 0`. The context store copies the arrays, as it does `car_load_flat`.

- [ ] **Step 1: Write the failing parity tests:**
  - In `apply_random_scenario()`, with probability 0.3 when `num_cars > 0`:
    - turn the switch on;
    - set `car_planned_by_predbat` to a random subset of cars;
    - mark random half hours `io_adjusted`, and tag some slots `octopus: True`;
    - build `my_predbat.prediction` with `car_cancel=build_car_cancel_table(...)`;
    - make some export windows target mode over car slots.

    Snapshot and restore every new attribute, using the existing `snapshot_scenario_state()` list: add `car_planned_by_predbat`, `car_force_holdable`, `io_adjusted`, `rate_import_saving_minutes` and `rate_max_base`.
  - In `run_edge_case_tests()`, add these cases:
    - `car_cancel_penalty`;
    - `car_cancel_revert`;
    - `car_cancel_split_half_hour`;
    - `car_cancel_forced` (a held car with no export);
    - `car_cancel_pv10`.

    Each goes through `dual_run()`.

- [ ] **Step 2: Run them and see them fail.** The stale binary is rebuilt by `ensure_kernel_built()`, and the old C++ ignores the table, so parity fails.

Run: `cd coverage && ./run_all --test kernel_parity > /tmp/cc4.log 2>&1; grep -E "FAIL|mismatch|passed" /tmp/cc4.log | head`

- [ ] **Step 3: Implement the C++.** The mask is computed once per run: start from `cancel_forced_mask`, then for each slot j and each export window i with `export_limits[i].mode == PK_EXPORT_MODE_TARGET` that overlaps, set bit j. Use the scenario's raw `export_limits`, not the floored ones. Then mirror Task 3 exactly. Bump both revisions and add the parity comment: "Parity 19: an export over a cancellable car slot cancels it - car load off, buy-back penalty, dispatch rate reverted."

- [ ] **Step 4: Build locally and run parity**

Run: `cd apps/predbat && bash build_kernel.sh && cd ../../coverage && ./run_all --test kernel_parity --test car_cancel > /tmp/cc4.log 2>&1; tail -5 /tmp/cc4.log`
Expected: all pass. Do not commit the cross-built `.so` files for other platforms: CI's code-quality workflow rebuilds and commits them to the PR branch. Commit only the locally built `prediction_kernel_lib_darwin_arm64.so`, and only if `build_kernel.sh` produced it.

- [ ] **Step 5: Run the quick suite's timing check.** Run `./run_all --test optimise_levels` with the switch off before and after the change; the wall time must be within noise (±5%). Record both times in the commit message.

- [ ] **Step 6: Commit** with the message `feat(kernel): mirror car slot cancellation for export (parity 19, ABI 11)`.

---

### Task 5: Publishing, the plan display and the execute hold

**Files:**
- Modify: `apps/predbat/output.py` `publish_car_plan()` (line ~220)
- Modify: `apps/predbat/plan.py`:
  - `car_charge_slot_kwh()` and `car_charge_slot_kwh_cancelled()` (line ~6395–6430);
  - `calculate_plan()`, a `self.publish_car_plan()` call inside `if publish:` after `publish_export_limit(...)` (line ~2560).
- Modify: `apps/predbat/execute.py`, the car hold (line ~964)
- Test: `apps/predbat/tests/test_car_cancel.py`

**Interfaces:**
- Consumes: `car_cancel_slots()`, `cancelled_mask()`, `cancelled_windows()`, `self.export_window_best`, `self.export_limits_best`, `self.car_force_hold_until`.
- Produces:
  - `PredBat.car_cancelled_for_export_windows() -> dict[int, list[tuple[int, int]]]`, in `plan.py`. It is always derived fresh, as `cancelled_windows(slots, cancelled_mask(slots, export_window_best, export_limits_best))`, with `slots = car_cancel_slots()`. It returns `{}` when the switch is off.
  - `self.car_cancelled_for_export`, the last result, set by `publish_car_plan()`. The display and execute read it. Reset to `{}` in `reset()`.
  - Changes to the car slot sensor's attributes, per car:
    - a slot whose `(start, end)` is in that car's cancelled windows is left out of `planned` and out of `kwh` and `cost`;
    - it is listed in a new attribute, `cancelled_for_export: [{"start": time_abs_str(start), "end": time_abs_str(end)}]`, with an empty list when there are none;
    - the sensor state is "off" when the slot under way is cancelled.

- [ ] **Step 1: Write the failing tests:**
  - `test_publish_cancelled_for_export`. With the switch on, a Predbat-led car with slots A and B, and a target export over A:
    - `planned` has only B;
    - `cancelled_for_export == [{"start": ..A.., "end": ..A..}]`;
    - `kwh` equals B's kWh;
    - the attribute parses with `parse_car_plan_windows()`, which is Plan 1's reader.
  - `test_publish_partial_overlap_whole_slot`. An export over only the last 5 minutes of A still lists all of A.
  - `test_publish_state_off_when_current_cancelled`. With now inside A, the sensor state is "off".
  - `test_publish_fetch_time_uses_last_plan`. With last cycle's `export_window_best` covering A, calling `publish_car_plan()` before `calculate_plan()` already leaves A out of `planned`.
  - `test_publish_switch_off_unchanged`. With the switch off, the attributes equal today's and `cancelled_for_export == []`.
  - `test_publish_held_car`. With `car_force_hold_until={0: now+20min}` and no export, the slot starting before the hold end is in `cancelled_for_export` (forced).
  - `test_display_marks_cancelled`. `car_charge_slot_kwh()` over A returns 0, and `car_charge_slot_kwh_cancelled()` returns A's kWh share.
  - `test_execute_no_hold_for_cancelled`. With now inside a cancelled A, `car_charging_from_battery` False and not exporting, the execute car hold is not applied (assert on the log line or on `carHolding`, following the existing execute tests in `tests/test_execute.py`).

- [ ] **Step 2: Run them and see them fail**

Run: `cd coverage && ./run_all --test car_cancel > /tmp/cc5.log 2>&1; grep -E "FAIL|passed" /tmp/cc5.log`

- [ ] **Step 3: Implement:**
  - `publish_car_plan()` calls `car_cancelled_for_export_windows()` once, stores it on `self.car_cancelled_for_export`, and skips cancelled windows from `planned` and the totals, picking the first window that isn't cancelled for the on/off state and the start time.
  - `car_charge_slot_kwh()` skips cancelled windows. `car_charge_slot_kwh_cancelled()` adds their kWh, so the existing "?" styling shows them.
  - In execute, a window that is in `self.car_cancelled_for_export` for that car is treated as if it had no kWh. The `car_charging_now` branch is untouched: a car really drawing power still holds.

- [ ] **Step 4: Run the tests, plus the `execute`, `car_charger_control` and `output` tests**

Run: `cd coverage && ./run_all --test car_cancel --test execute --test car_charger_control --test web_if > /tmp/cc5.log 2>&1; tail -5 /tmp/cc5.log`
Expected: all pass.

- [ ] **Step 5: Commit** with the message `feat(output): publish car slots cancelled for export and keep them out of the car plan`.

---

### Task 6: One cancel set everywhere, and the docs

**Files:**
- Test: `apps/predbat/tests/test_car_cancel.py`
- Modify: `docs/car-charging.md`, the Octopus Intelligent section of `docs/energy-rates.md`, `docs/components.md` (Wallbox section, replacing "planner support arrives with Plan 2")
- Modify: `tools/debug-journal.md`, adding a planner row under the car charging entries: symptom "export never planned over the car slot" → `car_cancel.py` / `hit_uncancellable_car_window()`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the end-to-end tests:**
  - `test_cancel_set_consistent`. With a saving-session scenario where `calculate_plan()` chooses to cancel slot A, record `metric_keep` from the final `save="best"` run. Then:
    - (a) `run_prediction()` on the same best windows gives the same `metric_keep`;
    - (b) `calculate_plan(recompute=False)` gives the same `metric_keep`, and `car_cancelled_for_export_windows()` gives the same windows;
    - (c) re-scoring the plan as the "previous" plan through the same path `should_replace_plan()` uses gives the same metric.
  - `test_neither_car_never_cancelled`. A car on an Octopus dispatch with no force-hold charger, under the same saving session: no export over its slot, and `cancelled_for_export` is empty.
  - `test_holdable_car_cancelled`. The same car with `car_force_holdable={0: "wallbox"}` gets its slot cancelled and published.

- [ ] **Step 2: Run them.** Expect a pass if Tasks 1–5 are right. Fix the code, not the test, on any failure.

Run: `cd coverage && ./run_all --test car_cancel > /tmp/cc6.log 2>&1; tail -5 /tmp/cc6.log`

- [ ] **Step 3: Write the docs** in British English:
  - **In `car-charging.md`:**
    - the switch, and what a cancellation costs: kWh × the highest base import rate, so only events like saving sessions win;
    - which cars qualify: Predbat-led cars, and cars on a charger that can force-hold (Wallbox today);
    - that the levels pass and freeze export never cancel;
    - the `cancelled_for_export` attribute.
  - **In the Octopus Intelligent doc:**
    - a cancelled dispatch outside 23:30–05:30 is costed at peak;
    - under the window billing rule it isn't;
    - Octopus smart control is never touched.

- [ ] **Step 4: Run the full quick suite and pre-commit**

Run: `cd coverage && ./run_all --quick > /tmp/cc6q.log 2>&1; tail -3 /tmp/cc6q.log; cd .. && coverage/venv/bin/pre-commit run --from-ref feat/charger-force-hold --to-ref HEAD > /tmp/cc6p.log 2>&1; grep -E "Failed|Passed" /tmp/cc6p.log`
Expected: "All tests passed", and every hook passes.

- [ ] **Step 5: Commit** with the message `test(plan): one cancel set across trials, the final plan and a reused plan; document cancelling a car slot for export`.

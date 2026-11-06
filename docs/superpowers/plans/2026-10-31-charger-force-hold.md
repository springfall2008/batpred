# Charger Force Hold Implementation Plan (Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a charger component hold a car off for a cancelled-for-export slot and hand it back afterwards. Wallbox is the first implementation (switch OCPP off, then pause; switch OCPP back on to resume).

**Architecture:** `CarChargerControl` gains an optional force-hold capability: four hooks a component implements, plus a shared engine. The engine reads the cancelled windows from each car's slot sensor, starts a hold 5 minutes before a window, keeps it until the window ends (extending into a touching window), and persists it. It also tells `plan.py` which cars are held, so the "in a dispatch but not charging" check leaves them alone. Wallbox implements the hooks with the OCPP configuration endpoint and the existing pause/resume. Plan 2 (planner, kernel, publishing) produces the `cancelled_for_export` attribute this plan consumes; Plan 3 adds Zappi, Ohme and Hypervolt.

**Tech Stack:** Python 3, asyncio, aiohttp (Wallbox transport), Predbat's custom test runner (`coverage/run_all`).

**Spec:** `docs/superpowers/specs/2026-10-30-cancel-car-slot-for-export-design.md`, sections "The force hold on the charger", "Wallbox force hold" and "Live test".

## Global Constraints

- Line length: 256 (Black), 250 (Flake8).
- Every function and class has a docstring (`interrogate`, 100%).
- British English spelling (CSpell, `en-gb`). Add new valid words to `.cspell/custom-dictionary-workspace.txt`.
- Variable names: `lower_case_with_underscores`.
- Tests run from `coverage/`. Always save the output to a file, then grep it: `./run_all --test <name> > /tmp/t.txt 2>&1; grep -E "FAIL|ERROR|PASS|✓" /tmp/t.txt`.
- `./run_pre_commit` passes before every commit.
- Run `gitnexus impact` on every existing symbol before editing it, and report the blast radius. Run `detect_changes` before every commit.
- Tests share one `PredBat` fixture: restore every field a test changes.
- With the switch `car_charging_cancel_for_export` off, behaviour is unchanged. No hold ever starts, and no extra API calls are made.
- The OCPP password is never logged, never put in an exception message, and never saved to storage.
- Time constants (spec): start a hold **5 minutes** before a cancelled window, as `FORCE_HOLD_LEAD_MINUTES = 5`. Warn when a hand-back is still owed **15 minutes** after the hold's end, as `FORCE_HOLD_SAFETY_MINUTES = 15` (builder's call, recorded here). Wallbox OCPP connected is `connectionStatus` **4** (live test).

## The cross-plan contract (Plan 2 produces it, this plan consumes it)

`binary_sensor.<prefix>_car_charging_slot[_N]` gains the attribute `cancelled_for_export`. It's a list of `{"start": "%m-%d %H:%M:%S", "end": "%m-%d %H:%M:%S"}` windows, the same shape as `planned` (`CAR_PLAN_TIME_FORMAT`), parsed by `parse_car_plan_windows()`. A window in `cancelled_for_export` is not listed in `planned`. Tests in this plan set the attribute directly.

## Review Focus

1. **Stale Wallbox status.** The cloud kept reporting "Charging" for minutes after a pause landed, and a second pause on an already paused charger may get a 409 (`WallboxStateError`). A refused pause while stopping must count as "not stopped yet", log once, and be retried, never crash the cycle. Test in Task 4.
2. **Restart mid-hold with OCPP off.** After a restart, a saved hold whose end has passed must hand the charger back at once (OCPP on), even if the plan no longer mentions the window. Tests in Tasks 2 and 4.
3. **The user turns OCPP back on mid-hold**, from the new switch. That ends the hold at the user's request: nothing is re-applied, and the hold is dropped with an Info log. Test in Task 4.
4. **Car unplugged mid-hold.** OCPP must still be restored when the hold ends, or the charger is left off Octopus. Test in Task 4.
5. **Two chargers, one held.** The other charger's Predbat-led control and status are untouched. Test in Task 2.

---

### Task 1: Switch, base hold record, and pausing the dispatch check

**Files:**
- Modify: `apps/predbat/config.py`, a new item directly after `car_charging_from_battery`.
- Modify: `apps/predbat/predbat.py`, in `reset()` next to `self.car_slot_owner = None` (line ~584).
- Modify: `apps/predbat/plan.py`: `dynamic_load_car_target()` (line ~670), plus a new helper beside it.
- Test: `apps/predbat/tests/test_dynamic_load_car.py`.

**Interfaces:**
- Produces:
  - Config switch `car_charging_cancel_for_export`: type `switch`, default `False`, friendly name "Allow export to cancel a car charging slot", `enable: num_cars`, `enable_condition: num_cars > 0`.
  - `self.car_force_hold_until: dict[int, datetime]`, car_n → the hold's end (UTC, aware). It's written by charger components (Task 2) and empty when no car is held.
  - `PredBat.car_force_held(car_n: int, now: datetime) -> bool`: True while `car_n` is in `car_force_hold_until` with an end after `now`.

- [ ] **Step 1: Write the failing test.** Add `_run_force_hold(my_predbat)` to `test_dynamic_load_car.py`, call it from `test_dynamic_load_car_not_charging()` beside `_run()`, and add `"car_force_hold_until"` to `STATE_FIELDS`:

```python
def _run_force_hold(my_predbat):
    """A car a charger is force-holding is never cancelled for not charging, and the grace clock restarts after."""
    failed = False
    _reset(my_predbat)
    _sensor(my_predbat, "off")
    my_predbat.car_force_hold_until = {0: _at(my_predbat, 840) + timedelta(minutes=30)}
    for minute, seconds in ((840, 10), (842, 10), (845, 0)):
        changed = _cycle(my_predbat, minute, seconds)
        failed |= _check("force hold {}".format(minute), not changed and _kwh(my_predbat) == [3.5, 3.5], "changed {} kwh {}".format(changed, _kwh(my_predbat)))
    failed |= _check("force hold clock", 0 not in my_predbat.dynamic_load_car_since, str(my_predbat.dynamic_load_car_since))
    my_predbat.car_force_hold_until = {}
    changed = _cycle(my_predbat, 846, 0)
    failed |= _check("after hold first sighting", not changed and _kwh(my_predbat) == [3.5, 3.5])
    changed = _cycle(my_predbat, 848, 10)
    failed |= _check("after hold grace passed", changed and _kwh(my_predbat) == [0, 0])
    return failed
```

- [ ] **Step 2: Run it and confirm it fails.** Run `./run_all --test dynamic_load_car_not_charging > /tmp/t1.txt 2>&1; grep -E "ERROR|FAILED|PASSED" /tmp/t1.txt`. Expected: `ERROR: force hold 842` (the slots are cancelled after the grace period).
- [ ] **Step 3: Implement.** Add the config item and the `reset()` field. Add `car_force_held()`. In `dynamic_load_car_target()`, straight after `cancelled` is computed: if `self.car_force_held(car_n, now)`, pop `car_n` from `dynamic_load_car_since` (when `record` is set) and return `cancelled` unchanged.
- [ ] **Step 4: Run it and confirm it passes.** Same command. Expected: `PASSED`. Also run `./run_all --test dynamic_load_car_slot_cancellation` and expect `PASSED`.
- [ ] **Step 5: Commit.** `feat: add car_charging_cancel_for_export switch and pause slot confirmation for force-held cars`

---

### Task 2: The force-hold engine in CarChargerControl

**Files:**
- Modify: `apps/predbat/car_charger_control.py`: `charger_control_setup()`, `charger_control_save()`, `charger_control_load_enabled()`, `charger_control_tick()`, `charger_control_release()`, `charger_control_release_inactive()`, `charger_control_drive_one()`, plus the new methods below.
- Test: `apps/predbat/tests/test_car_charger_control.py`.

**Interfaces:**
- Consumes: `car_force_hold_until` from Task 1. The `cancelled_for_export` attribute from the contract above.
- Produces, the hooks a component overrides. Each default is the no-op shown:
  - `charger_control_can_force_hold(handle) -> bool`, default `False`.
  - `async charger_control_force_pause(handle, saved: Optional[dict]) -> dict`. It takes one step towards stopping the charger. `saved` is `None` on the first call; the call returns the pre-hold state to keep (JSON-serialisable, no secrets). It raises on a refusal.
  - `charger_control_force_stopped(handle, saved: dict) -> bool`. Is the charger confirmed stopped?
  - `async charger_control_force_resume(handle, saved: dict) -> None`. It takes one step towards handing back exactly what `force_pause` changed, and raises on a refusal.
  - `async charger_control_force_handed_back(handle, saved: dict) -> bool`. Is the hand-back confirmed?
- Produces, the engine:
  - Constants `FORCE_HOLD_LEAD_MINUTES = 5` and `FORCE_HOLD_SAFETY_MINUTES = 15`.
  - `self.charger_control_force_holds: dict[str, dict]`, charger key → `{"car": int, "end": str (ISO 8601, aware), "saved": dict | None, "stopped": bool, "warned": bool}`. It's saved under `"force_holds"` in the same storage document as `"held"`, and restored by `charger_control_load_enabled()`.
  - `charger_control_cancelled_windows(car_n: int, now: datetime) -> list[tuple[datetime, datetime]]`. It parses `cancelled_for_export` and merges windows whose start ≤ the previous end.
  - `charger_control_force_hold_wanted() -> bool`, meaning `get_arg("car_charging_cancel_for_export", False)` and not `charger_control_read_only_now()`.
  - `async charger_control_force_hold_tick(now: datetime) -> None`.
  - `charger_control_force_hold_busy(now: datetime) -> bool`. True while any hold exists, or while a cancelled window starts within `FORCE_HOLD_LEAD_MINUTES + 5` minutes. Wallbox uses it to poll every cycle.
  - `charger_control_end_force_hold(key: str, why: str) -> None` (async). It drops a hold without a resume, logs `Info`, saves, and clears `base.car_force_hold_until`. Used by Task 4's switch.

Engine rules for `charger_control_force_hold_tick()`. It runs per `(car_n, key, handle)` over `charger_control_chargers()[:car_count]`, each charger through `charger_control_each()`:
1. **No hold yet.** A hold starts when all of these are true:
   - `charger_control_force_hold_wanted()` is true;
   - the charger can force-hold;
   - `charger_control_unplugged(handle)` is false;
   - a cancelled window has `start − FORCE_HOLD_LEAD_MINUTES ≤ now < end`.

   The hold record is saved **before** the first `force_pause` call, then the tick continues with rule 2.
2. **Holding.** The hold stays while `now < end` and the switch is still wanted:
   - It extends `end` to the end of any cancelled window with `start ≤ end < window_end`.
   - Until `stopped`: call `force_pause(handle, saved)`, store the returned `saved`, set `stopped = force_stopped(handle, saved)`, and save on any change.
3. **Handing back.** This happens when `now ≥ end`, or the switch is no longer wanted (switch off or read only releases at once, my call): call `force_resume(handle, saved)`. If `await force_handed_back(handle, saved)`, drop the hold, log `Info`, and save. If `now ≥ end + FORCE_HOLD_SAFETY_MINUTES` and the hand-back isn't confirmed, log one `Warn` per hold naming the charger.
4. Keep `self.base.car_force_hold_until[car_n] = end` while held, and delete it when the hold ends.

`charger_control_tick()` calls `charger_control_force_hold_tick(now)` right after the load and save-retry block, **before** the `charger_control_active` check, so holds run even when Predbat-led control is off. `charger_control_drive_one()`, `charger_control_release()` and `charger_control_release_inactive()` skip any key in `charger_control_force_holds`.

- [ ] **Step 1: Write the failing tests.** Extend the fakes first:
  - `FakeBase` gains `car_force_hold_until = {}`.
  - `FakeComponent` gains `self.cancelled = {}` (entity → windows), returned by `get_state_wrapper` for `attribute="cancelled_for_export"`, and `self.args["car_charging_cancel_for_export"] = True`.
  - The force hooks record `(key, "force_pause")` and `(key, "force_resume")` into `commands`.
  - `force_pause` adds 1 to `handle.pauses` and returns `{"was": "on"}`.
  - `force_stopped` returns `handle.pauses >= handle.stop_after`.
  - `force_handed_back` returns `handle.handed_back`.
  - `FakeCharger` gains `can_force = True`, `pauses = 0`, `stop_after = 1` and `handed_back = True`.
  - Add `_cancel(component, car_n, windows)`, mirroring `_plan`.

  Then add the tests below, all with `_now()` = 01:30 and a cancelled window 02:00–02:30 unless stated:

```python
def test_force_hold_waits_for_the_lead_time():          # at 01:54 no command; at 01:55 one force_pause, hold saved with saved == {"was": "on"}
def test_force_hold_needs_the_switch_and_no_read_only(): # switch False -> no command; set_read_only True -> no command
def test_force_hold_skips_unplugged_and_incapable():     # unplugged -> none; can_force False -> none
def test_force_hold_repeats_pause_until_stopped():       # stop_after=3 -> force_pause at 3 ticks, none at the 4th
def test_force_hold_is_committed():                      # cancelled list emptied at 02:10 -> no force_resume until 02:30
def test_force_hold_extends_into_a_touching_window():    # windows 02:00-02:30 and 02:30-03:00 -> hold end 03:00, no resume at 02:30
def test_force_hold_resume_retried_until_handed_back():  # handed_back False at 02:30 -> force_resume each tick; True -> hold gone, base.car_force_hold_until == {}
def test_force_hold_refused_resume_is_retried():         # force_resume raises once -> hold kept; next tick resumes
def test_force_hold_survives_a_restart():                # saved storage with hold end 02:30; new component at 02:40 -> force_resume on first tick with no plan published
def test_force_hold_switch_off_hands_back_at_once():     # switch False at 02:10 -> force_resume at 02:10
def test_force_hold_warns_once_after_the_safety_margin():# handed_back False; at 02:45 one "Warn:" log, not repeated at 02:46
def test_force_held_charger_is_not_driven_by_the_plan(): # planned window 02:00-03:00 + hold -> no "on" command for the held key, second charger still driven
def test_force_hold_sets_and_clears_base_hold_until():   # during hold base.car_force_hold_until == {0: end}; after hand-back {}
```

Register each in `run_car_charger_control_tests()`.
- [ ] **Step 2: Run them and confirm they fail.** Run `./run_all --test car_charger_control > /tmp/t2.txt 2>&1; grep -E "Error|FAIL|✓" /tmp/t2.txt`. Expected: `AttributeError` on `charger_control_force_holds` / `_cancel`.
- [ ] **Step 3: Implement** the hooks, the engine, and the persistence and skip changes above.
- [ ] **Step 4: Run them and confirm they pass.** Same command, plus `./run_all --test wallbox --test myenergi --test ohme --test hypervolt --test gecloud > /tmp/t2b.txt 2>&1`. Expected: all PASS, since the existing components inherit the no-op defaults.
- [ ] **Step 5: Commit.** `feat: add the force hold engine to CarChargerControl`

---

### Task 3: Wallbox OCPP configuration and the OCPP switch

**Files:**
- Modify: `apps/predbat/wallbox.py`: `WallboxTransport`, `wallbox_attribute_table`, `publish_data()`, `switch_event_handler()`.
- Test: `apps/predbat/tests/test_wallbox.py`.

**Interfaces:**
- Produces:
  - `WALLBOX_OCPP_CONNECTED = 4`.
  - `WallboxTransport.get_ocpp_configuration(charger_id) -> dict`, which is `GET v3/chargers/<id>/ocpp-configuration`.
  - `WallboxTransport.set_ocpp_mode(charger_id, ocpp: bool) -> dict`. It reads the configuration, then `POST`s the same path with body `{"address", "chargePointIdentity", "password", "type"}`, taken from the read, with `type` set to `"ocpp"` or `"wallbox"`. It is a write (`write=True`).
  - The switch `switch.<prefix>_wallbox_<id>_ocpp`, with table key `"ocpp"`, friendly name "Wallbox OCPP Connection" and icon `mdi:lan-connect`. Its state is `on` when `charger.ocpp`. `turn_off` calls `set_ocpp_mode(id, False)` and `turn_on` calls `set_ocpp_mode(id, True)`, both queued like the other switches.
  - `_StubTransport` gains `get_ocpp_configuration` (it returns `self.ocpp[charger_id]`, default `{"type": "ocpp", "address": "wss://ocpp.example", "chargePointIdentity": "id-1", "password": "pw", "connectionStatus": 4}`) and `set_ocpp_mode`, which records the call and updates the stub's `type`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_transport_ocpp_requests():
    # two calls: GET .../ocpp-configuration, then POST same URL with
    # {"address": "wss://ocpp.octopus.energy", "chargePointIdentity": "id-1", "password": "pw", "type": "wallbox"}
    # and for set_ocpp_mode(101, True) the POST body has "type": "ocpp"
def test_transport_ocpp_error_hides_the_password():
    # POST answers 502 -> WallboxApiError raised; "pw" not in str(exc) and not in any logged line
def test_ocpp_switch_published_and_controls():
    # mode "ocpp" -> switch.predbat_wallbox_101_ocpp == "on"; turn_off queued then run -> ("set_ocpp_mode", 101, False)
```

Add `test_transport_ocpp_requests` and the other two to `test_wallbox()`.
- [ ] **Step 2: Run them and confirm they fail.** Run `./run_all --test wallbox > /tmp/t3.txt 2>&1; grep -E "Error|✓" /tmp/t3.txt`. Expected: `AttributeError: ... get_ocpp_configuration`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and confirm they pass.** Same command. Expected: every `✓`, no error.
- [ ] **Step 5: Commit.** `feat(wallbox): read and switch the OCPP connection`

---

### Task 4: Wallbox force hold

**Files:**
- Modify: `apps/predbat/wallbox.py`: `WallboxAPI` (the four hooks, `run()` cadence, and `switch_event_handler()` for the OCPP switch during a hold).
- Test: `apps/predbat/tests/test_wallbox.py`.

**Interfaces:**
- Consumes: the Task 2 hooks and `charger_control_force_hold_busy()` / `charger_control_end_force_hold()`; the Task 3 transport calls.
- Produces the Wallbox hook behaviour:
  - `charger_control_can_force_hold(charger)` → `True`.
  - `charger_control_force_pause(charger, saved)`:
    - On the first call, `saved = {"ocpp": charger.ocpp}`.
    - If `saved["ocpp"]` and `charger.ocpp`, call `set_ocpp_mode(id, False)` and stop there. The pause waits for a later cycle that reads the charger out of OCPP mode, because a pause sent before the mode change lands is ignored (live test).
    - Otherwise, if `charger.charging`, call `transport.pause(id)`. A `WallboxStateError` here is logged once per hold at `Info` and swallowed, because a stale "Charging" status on a charger that is already paused answers 409.
    - Return `saved`.
  - `charger_control_force_stopped(charger, saved)`: the charger isn't stale, isn't charging, and, when `saved["ocpp"]`, isn't in OCPP mode.
  - `charger_control_force_resume(charger, saved)`: if `saved["ocpp"]` and the charger isn't in OCPP mode, call `set_ocpp_mode(id, True)`. Errors, including HTTP 502, raise and are retried next cycle. No resume command is sent; Octopus restarts the charge. If not `saved["ocpp"]` and `charger.paused`, call `transport.resume(id)`.
  - `charger_control_force_handed_back(charger, saved)`: with `saved["ocpp"]`, it reads `get_ocpp_configuration()` and returns `type == "ocpp" and connectionStatus == WALLBOX_OCPP_CONNECTED`. Otherwise it returns `not charger.paused`.
  - `run()`: when `charger_control_force_hold_busy(now)` is true, poll every cycle instead of every `poll_seconds`.
  - `switch_event_handler()`: if the OCPP switch is turned **on** while that charger is force-held, call `charger_control_end_force_hold(key, "the OCPP switch was turned on")` after sending the write.

- [ ] **Step 1: Write the failing tests.** These use `_control_component({}, ...)` with `wallbox_control=False`, `component.base.args["car_charging_cancel_for_export"] = True`, and `cancelled_for_export` set on the slot sensor through `component.base.set_state_wrapper(...)`. Drive each step with `_load_chargers()` and then `control_tick(now)`:

```python
def test_wallbox_force_hold_turns_ocpp_off_then_pauses():
    # ocpp charging at lead time -> calls == [("set_ocpp_mode", 101, False)] only (get_ocpp_configuration allowed)
    # next tick, status mode "wallbox" charging -> [("pause", 101)]
    # next tick, status 182 power 0 -> no calls; hold stopped
def test_wallbox_force_hold_refused_pause_is_retried():
    # pause raises WallboxStateError -> no exception out of control_tick, one Info log; next tick pauses again
def test_wallbox_force_hold_hands_back_by_ocpp_only():
    # at end: set_ocpp_mode True raises WallboxApiError("HTTP 502") once -> hold kept; next tick sets it;
    # connectionStatus 2 -> hold kept; connectionStatus 4 -> hold gone; ("resume", 101) never called
def test_wallbox_force_hold_leaves_a_user_ocpp_off_alone():
    # charger already mode "wallbox" at start -> pause/resume only, set_ocpp_mode never called
def test_wallbox_force_hold_restores_ocpp_after_unplug():
    # status 161 (no car) mid-hold -> at end set_ocpp_mode(101, True) still called
def test_wallbox_force_hold_restart_with_ocpp_off():
    # storage holds a hold for "101" with saved {"ocpp": True}, end passed; status mode "wallbox" -> first tick set_ocpp_mode(101, True)
def test_wallbox_ocpp_switch_on_ends_the_hold():
    # during hold, turn_on of switch.predbat_wallbox_101_ocpp -> set_ocpp_mode True sent, hold dropped, no further pause
def test_wallbox_force_hold_polls_every_cycle():
    # with a hold in place, run(60, False) polls (get_status called) although 60 % poll_seconds != 0
def test_wallbox_force_hold_off_by_default():
    # switch not set -> a cancelled window produces no calls at all
```

- [ ] **Step 2: Run them and confirm they fail.** Run `./run_all --test wallbox > /tmp/t4.txt 2>&1; grep -E "Error|✓" /tmp/t4.txt`. Expected: the first new test fails because no `set_ocpp_mode` was called.
- [ ] **Step 3: Implement** the hooks, the `run()` cadence and the switch override.
- [ ] **Step 4: Run them and confirm they pass.** Same command, then `./run_all --quick > /tmp/t4q.txt 2>&1; grep -E "FAILED|passed|failed" /tmp/t4q.txt`. Expected: all pass.
- [ ] **Step 5: Commit.** `feat(wallbox): force hold by switching OCPP off and pausing`

---

### Task 5: Manual control from the command line, docs and journal

**Files:**
- Modify: `apps/predbat/wallbox.py`: `main()` / `run_wallbox_cli()` (`# pragma: no cover`, like the existing command-line code).
- Modify: `docs/components.md` (the Wallbox section) and `docs/car-charging.md`.
- Modify: `tools/debug-journal.md`.

**Interfaces:**
- Produces two new command-line arguments, in the existing mutually exclusive group:
  - `--ocpp {on,off}` calls `set_ocpp_mode` and reads the charger back.
  - `--force-hold MINUTES`:
    - Set `car_charging_cancel_for_export` on the mock base, and publish `cancelled_for_export` with one window `[now, now + MINUTES]` for the chosen charger's car.
    - Every 30 seconds, call `component.poll()` and print the charger table plus the hold record (with no `saved` secrets), until `charger_control_force_holds` is empty or 30 minutes after the end.
    - Ctrl-C triggers a hand-back loop (set the window end to now and keep polling) rather than leaving OCPP off.

- [ ] **Step 1: Implement the two arguments.**
- [ ] **Step 2: Live-test on the user's Pulsar. The user runs this, outside a dispatch first.** From `apps/predbat`, run `python3 wallbox.py --username <email> --force-hold 5`. Expected:
  - OCPP turns off;
  - a pause follows on a later loop;
  - `status_id` 182;
  - at the end, OCPP comes back on and the loop exits once `connectionStatus` is 4.
- [ ] **Step 3: Write the docs.**
  - `docs/components.md` Wallbox section: the OCPP connection switch, what force hold does, and that the first version needs `car_charging_cancel_for_export` (planner support arrives with Plan 2).
  - `docs/car-charging.md`: one paragraph naming the switch and which chargers can hold.
- [ ] **Step 4: Add to the journal.** Add a Wallbox row to `tools/debug-journal.md`:
  - Octopus `SUSPEND` doesn't stop a Pulsar (it charges at full power).
  - A pause under OCPP gets a 409.
  - A pause sent before the OCPP-off lands is ignored.
  - The OCPP-on write can get a 502 while the paused charger is quiet on the cloud; a retry works.
  - The cloud status lags by minutes after a pause; trust `last_sync`.
- [ ] **Step 5: Run the checks and commit.** Run `./run_pre_commit`, then commit `docs(wallbox): OCPP switch, force hold and live-test findings`.

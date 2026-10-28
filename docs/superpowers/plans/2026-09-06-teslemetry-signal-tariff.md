# Teslemetry Signal Tariff (TBC control mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Predbat drive a Tesla Powerwall at its full charge rate by pushing a synthetic "signal" tariff that tells Tesla's own optimiser when to charge and export, gated behind a trial setting that defaults off.

**Architecture:** The pushed time-of-use tariff stops describing real prices and becomes a control signal: 0p over the charge window, 100p over the export window, 50p buy / 0p sell everywhere else, mirrored identically across all seven days so the tariff is a pure function of the two committed windows. Device state moves to `autonomous` mode throughout so Tesla's optimiser acts on it, with backup reserve carrying only its designed meaning (a floor) plus a reserve-100 hold. Both behaviours sit behind one component arg, so the existing real-rate path is untouched when the arg is off.

**Tech Stack:** Python 3, `apps/predbat/teslemetry.py`, the Predbat component arg system in `apps/predbat/components.py`, and the repo's own unit-test harness (`apps/predbat/tests/test_teslemetry.py`, run through `tools/triage_test.sh`).

**Spec:** `docs/superpowers/specs/2026-09-06-teslemetry-signal-tariff.md`

## Global Constraints

- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every function, method and class needs one, including test functions.
- **Spell checking:** British English (`en-gb`) via CSpell. Unknown words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit, so re-stage after running pre-commit.
- **Variable naming:** `lower_case_with_underscores`.
- **All checks run via `./run_pre_commit`** from the repo root and must pass before the final commit.
- **Unit tests must be added for all new code.**
- **Run one test module, never the full suite:** `tools/triage_test.sh teslemetry /tmp/tesla-test.log` from the repo root. It prints the exit status and the last 30 lines; grep the log for anything more. Never pipe test output straight to grep — a wrong search means re-running.
- **The trial arg defaults to `False`.** No existing user's behaviour may change from this work.
- **Do not modify** `build_tariff`, `_quantise_side`, `_rate_side`, `_side_rates`, `current_rates`, `_boost_segments`, `_apply_boost`, `_side_layout`, `_local_today_weekday`, `_tesla_dow` or `_boost_price`. They remain the legacy path and their existing tests must keep passing untouched.

---

### Task 1: Signal tariff builder

Builds the synthetic tariff from the two committed windows. Pure functions with no clock, no day-of-week and no live SoC — that independence is the property that stops the tariff re-pushing at every midnight rollover, and it is asserted directly in Step 9.

**Files:**
- Modify: `apps/predbat/teslemetry.py` (constants after line 71; new methods beside `_discharge_window` at line 1108)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Consumes: existing `_carve_interval(intervals, start_min, end_min, tier)`, `_render_side(layout, tier_prices)`, `_assemble_tariff(code, buy_charges, buy_periods, sell_charges, sell_periods)`, `time_to_minutes(value)`, and the module constant `BOOST_TIER`.
- Produces:
  - `SIGNAL_CHEAP_TIER` / `SIGNAL_BASE_TIER` / `SIGNAL_PEAK_TIER` — `str` tier names.
  - `SIGNAL_BUY_PRICES` / `SIGNAL_SELL_PRICES` — `dict[str, float]`, GBP per kWh.
  - `SIGNAL_HOLD_DEADBAND_PERCENT: int` and `SIGNAL_HOLD_RESERVE: int` (used by Task 2, defined here so all signal-mode constants live together).
  - `TeslemetryAPI._window_intervals(window) -> list[tuple[int, int]]` (staticmethod).
  - `TeslemetryAPI._signal_layout(charge_window, export_window) -> dict[int, list[tuple[int, int, str]]]` (staticmethod).
  - `TeslemetryAPI.build_signal_tariff(charge_window=None, export_window=None) -> dict`.
  - `TeslemetryAPI._charge_window() -> tuple[int, int] | None`.

- [ ] **Step 1: Add the module constants**

In `apps/predbat/teslemetry.py`, directly after the existing `SLOT_MINUTES = 30` line (line 71 area), add:

```python
# Signal-tariff control mode (GH#4892). In this mode the pushed tariff stops describing real prices
# and becomes a control signal saying when Predbat wants energy moved, so Tesla's own optimiser runs
# the charge and reaches the full rate that reserve-driven charging cannot. Three fixed bands,
# written identically to every day of the week, which is what makes the tariff a pure function of the
# two committed windows - no clock, no day-of-week arithmetic, and no re-push at midnight.
SIGNAL_CHEAP_TIER = "SUPER_OFF_PEAK"
SIGNAL_BASE_TIER = "PARTIAL_PEAK"
SIGNAL_PEAK_TIER = BOOST_TIER
# GBP/kWh. Buy and sell match inside each window so the optimiser can never profit by charging to
# re-export within the same band (the same invariant the real-rate path keeps by mirroring the boost
# onto the buy side). The 0p sell floor outside both windows means deferring an export past its
# window end earns nothing at all, rather than merely less.
SIGNAL_BUY_PRICES = {SIGNAL_CHEAP_TIER: 0.0, SIGNAL_BASE_TIER: 0.5, SIGNAL_PEAK_TIER: 1.0}
SIGNAL_SELL_PRICES = {SIGNAL_CHEAP_TIER: 0.0, SIGNAL_BASE_TIER: 0.0, SIGNAL_PEAK_TIER: 1.0}
# Percent below the charge target that still counts as being at it. A freeze charge arrives as a
# charge window whose target is the SOC at the moment execute.py wrote it, so house load can drop SOC
# a fraction below that before the next cycle - without a deadband the state would flip out of hold
# and import against the 0p band.
SIGNAL_HOLD_DEADBAND_PERCENT = 1
# The only reserve above 80 that Tesla still accepts: since firmware 25.18.4, 81-99 snap to 80.
SIGNAL_HOLD_RESERVE = 100
# Highest reserve below the snap band. A request above this is rounded UP to SIGNAL_HOLD_RESERVE
# rather than left to be snapped down to 80 by the device: Predbat only asks for a reserve up here
# when it wants a hold (execute.py writes soc+1 under set_reserve_hold), and 80 would not hold it.
# Either way what Predbat models and what the battery honours must agree, which is the divergence
# GH#4953/#4956 fixed for the reserve floor generally.
SIGNAL_MAX_SETTABLE_RESERVE = 80
```

- [ ] **Step 2: Write the failing tests for the tariff builder**

Add to `apps/predbat/tests/test_teslemetry.py`, after the existing `test_teslemetry_build_tariff_*` block (which ends around line 760). First the shared helper, then five tests:

```python
def _signal_tier_at(tariff, day, minute, sell=False):
    """Return the tier name covering a minute on a day-of-week in a rendered signal tariff."""
    seasons = tariff["sell_tariff"]["seasons"] if sell else tariff["seasons"]
    for tier, block in seasons["AllYear"]["tou_periods"].items():
        for period in block["periods"]:
            if not (period["fromDayOfWeek"] <= day <= period["toDayOfWeek"]):
                continue
            start = period["fromHour"] * 60 + period["fromMinute"]
            end = period["toHour"] * 60 + period["toMinute"]
            if end == 0:
                end = 1440
            if start <= minute < end:
                return tier
    return None


def test_teslemetry_signal_tariff_mirrors_every_day():
    """The signal tariff writes one shape to all seven days, so no day-of-week logic is needed."""
    api = MockTeslemetryAPI()
    tariff = api.build_signal_tariff((120, 300), (1020, 1140))  # charge 02:00-05:00, export 17:00-19:00
    for day in range(7):
        assert _signal_tier_at(tariff, day, 180) == "SUPER_OFF_PEAK"  # 03:00, inside the charge window
        assert _signal_tier_at(tariff, day, 1080) == "ON_PEAK"  # 18:00, inside the export window
        assert _signal_tier_at(tariff, day, 600) == "PARTIAL_PEAK"  # 10:00, outside both


def test_teslemetry_signal_tariff_fixed_band_prices():
    """Bands carry the fixed signal prices, with buy and sell equal inside both windows."""
    api = MockTeslemetryAPI()
    tariff = api.build_signal_tariff((120, 300), (1020, 1140))
    assert tariff["energy_charges"]["AllYear"]["rates"] == {"SUPER_OFF_PEAK": 0.0, "PARTIAL_PEAK": 0.5, "ON_PEAK": 1.0}
    assert tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"] == {"SUPER_OFF_PEAK": 0.0, "PARTIAL_PEAK": 0.0, "ON_PEAK": 1.0}


def test_teslemetry_signal_tariff_midnight_wrap_is_two_intervals():
    """A window crossing midnight becomes two ranges on every day and still partitions the day."""
    api = MockTeslemetryAPI()
    tariff = api.build_signal_tariff((1380, 300), None)  # charge 23:00 -> 05:00
    for day in range(7):
        assert _signal_tier_at(tariff, day, 1410) == "SUPER_OFF_PEAK"  # 23:30, before midnight
        assert _signal_tier_at(tariff, day, 60) == "SUPER_OFF_PEAK"  # 01:00, after midnight
        assert _signal_tier_at(tariff, day, 600) == "PARTIAL_PEAK"  # 10:00, outside
        day_periods = {tier: {"periods": [p for p in block["periods"] if p["fromDayOfWeek"] <= day <= p["toDayOfWeek"]]} for tier, block in tariff["seasons"]["AllYear"]["tou_periods"].items()}
        _assert_tou_periods_partition_day(day_periods)


def test_teslemetry_signal_tariff_is_independent_of_the_clock_and_rates():
    """Same windows -> byte-identical tariff whatever the day, time or real rates.

    This is the property that stops a re-push firing at every midnight rollover: the real-rate path
    re-serialises differently once the day index moves, and the signal path must not.
    """
    import json
    from datetime import datetime

    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    first = json.dumps(api.build_signal_tariff((120, 300), (1020, 1140)), sort_keys=True)
    api.base.now = datetime(2026, 7, 23, 3, 30)  # different weekday and time of day
    api.base.rate_import = {minute: 9.0 for minute in range(0, 2880)}  # and different real rates
    second = json.dumps(api.build_signal_tariff((120, 300), (1020, 1140)), sort_keys=True)
    assert first == second


def test_teslemetry_signal_tariff_without_windows_is_flat_base():
    """With neither window committed there is nothing cheap and nothing at peak - only the base band."""
    api = MockTeslemetryAPI()
    tariff = api.build_signal_tariff(None, None)
    assert set(tariff["seasons"]["AllYear"]["tou_periods"]) == {"PARTIAL_PEAK"}
    assert tariff["energy_charges"]["AllYear"]["rates"] == {"PARTIAL_PEAK": 0.5}
    assert tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"] == {"PARTIAL_PEAK": 0.0}


def test_teslemetry_charge_window_accessor():
    """_charge_window mirrors _discharge_window: None unless enabled with a non-empty span."""
    api = MockTeslemetryAPI()
    api.schedule = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}, "discharge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "enable": 0}}
    assert api._charge_window() == (120, 300)
    api.schedule["charge"]["enable"] = 0
    assert api._charge_window() is None
    api.schedule["charge"].update({"enable": 1, "end_time": "02:00:00"})
    assert api._charge_window() is None
```

- [ ] **Step 3: Register the new tests**

The module's tests are collected by an explicit list. Open `apps/predbat/tests/test_teslemetry.py`, find the `test_teslemetry()` entry point near the end of the file, and add the six new function names to the list of tests it calls, following the existing style exactly.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `tools/triage_test.sh teslemetry /tmp/tesla-test.log`
Expected: FAIL with `AttributeError: 'MockTeslemetryAPI' object has no attribute 'build_signal_tariff'`.

- [ ] **Step 5: Implement the builder**

In `apps/predbat/teslemetry.py`, add these four methods immediately after `_discharge_window` (line 1108-1115):

```python
    def _charge_window(self):
        """Return (start_min, end_min) for the committed charge window when enabled, else None."""
        charge = self.schedule.get("charge", {})
        if not charge.get("enable"):
            return None
        start = self.time_to_minutes(charge.get("start_time", "00:00:00"))
        end = self.time_to_minutes(charge.get("end_time", "00:00:00"))
        return None if start == end else (start, end)

    @staticmethod
    def _window_intervals(window):
        """Split a (start, end) minute window into non-wrapping [from, to) ranges inside one day.

        A window whose start is after its end wraps midnight and becomes two ranges. Because the same
        shape is written to every day of the week, that is all a midnight crossing needs here - there
        is no "which day does this land on" question of the kind _boost_segments has to answer on the
        real-rate path.
        """
        if not window:
            return []
        start, end = window
        if start == end:
            return []
        if start < end:
            return [(start, end)]
        return [(start, 1440), (0, end)]

    @staticmethod
    def _signal_layout(charge_window, export_window):
        """Return the per-day interval layout for the signal tariff, identical on all seven days.

        Starts from a base band covering the whole day and carves the export then charge windows into
        it. Predbat's optimiser guarantees the two windows never overlap, so the carve order decides
        no minute's band; it is fixed only so the rendered output is deterministic.
        """
        intervals = [(0, 1440, SIGNAL_BASE_TIER)]
        for window, tier in ((export_window, SIGNAL_PEAK_TIER), (charge_window, SIGNAL_CHEAP_TIER)):
            for start, end in TeslemetryAPI._window_intervals(window):
                intervals = TeslemetryAPI._carve_interval(intervals, start, end, tier)
        return {day: list(intervals) for day in range(7)}

    def build_signal_tariff(self, charge_window=None, export_window=None):
        """Build the signal tariff: fixed 0p/50p/100p bands over the committed windows (GH#4892).

        Deliberately takes no clock, no day-of-week and no live SOC, so the serialised body changes
        only when a window changes - which is what makes set_tariff's write-on-change dedupe mean
        something rather than firing once a day on the calendar alone. One layout serves both sides
        and only the price map differs, so the buy and sell periods cannot drift apart.
        """
        layout = self._signal_layout(charge_window, export_window)
        buy_charges, buy_periods = self._render_side(layout, SIGNAL_BUY_PRICES)
        sell_charges, sell_periods = self._render_side(layout, SIGNAL_SELL_PRICES)
        return self._assemble_tariff("PREDBAT", buy_charges, buy_periods, sell_charges, sell_periods)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `tools/triage_test.sh teslemetry /tmp/tesla-test.log`
Expected: PASS, with every pre-existing teslemetry test still passing. If any `test_teslemetry_build_tariff_*` test fails, the legacy path has been disturbed — revert and re-check that only new code was added.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): build a signal tariff from the committed windows (#4892)"
```

---
### Task 2: Device-state mapping under signal-tariff control

Maps the committed schedule to the device tuple in the new mode. Separate from Task 1 because a reviewer could accept the tariff shape and still reject the reserve/mode policy — this is where the reserve stops being an overloaded charge signal and goes back to carrying only the reserve Predbat actually asked for.

**Files:**
- Modify: `apps/predbat/teslemetry.py` (new methods beside `evaluate_schedule` at line 524)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Consumes: `SIGNAL_HOLD_DEADBAND_PERCENT`, `SIGNAL_HOLD_RESERVE`, `SIGNAL_MAX_SETTABLE_RESERVE` from Task 1; existing `in_window(minutes_now, window)` and `self.schedule`.
- Produces:
  - `TeslemetryAPI._settable_reserve(percent) -> int` (staticmethod).
  - `TeslemetryAPI.evaluate_schedule_tbc(minutes_now, soc) -> dict` with the same four keys as `evaluate_schedule`: `export_rule`, `grid_charging`, `reserve`, `mode`.

**Design note the implementer needs.** Reserve is whatever Predbat wrote into `schedule["reserve"]` via the `reserve` arg, which `automatic_config` binds to the `schedule_reserve` number entity — the real reserve, in every state. It is deliberately **not** the export target: under this mode Predbat's charge and export targets are advisory (Tesla decides how much energy to move) and window length is the lever for them, so overloading reserve as an export floor would be the one place the component second-guessed that decision.

The 81–99 band still has to be handled, because Predbat itself reaches it: `execute.py:306`, `:593` and `:620` write `adjust_reserve(min(soc_percent + 1, 100))` under `set_reserve_hold`, so a battery at 85% produces a reserve request of 86. That request means "hold above 80", and 80 would not hold it — the battery would be free to discharge back down to 80. So such a request rounds **up** to 100, which does hold, with grid charging disabled alongside so the device cannot import to reach it.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`, after the Task 1 tests:

```python
def _tbc_api(charge=None, discharge=None, reserve=15):
    """Build a mock API with a committed schedule for the signal-tariff state tests."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": reserve,
        "charge": charge or {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": 0},
        "discharge": discharge or {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "enable": 0},
    }
    return api


def test_teslemetry_tbc_charging_below_target_enables_grid_at_the_real_reserve():
    """Below the charge target the tariff does the work: grid charging on, reserve left where Predbat set it."""
    api = _tbc_api(charge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1})
    assert api.evaluate_schedule_tbc(3 * 60, 40) == {"export_rule": "pv_only", "grid_charging": True, "reserve": 15, "mode": "autonomous"}


def test_teslemetry_tbc_at_target_holds_with_reserve_100_and_no_grid():
    """At target it holds: reserve 100 stops discharge, grid charging off stops it importing to reach it."""
    api = _tbc_api(charge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1})
    assert api.evaluate_schedule_tbc(3 * 60, 90) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 100, "mode": "autonomous"}


def test_teslemetry_tbc_hold_deadband_survives_a_one_percent_sag():
    """A freeze charge whose SOC has sagged 1% below the written target still holds, rather than importing."""
    api = _tbc_api(charge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 85, "enable": 1})
    assert api.evaluate_schedule_tbc(3 * 60, 84)["grid_charging"] is False
    assert api.evaluate_schedule_tbc(3 * 60, 84)["reserve"] == 100
    # Two points below the target is a real shortfall, not sensor sag, so charging resumes.
    assert api.evaluate_schedule_tbc(3 * 60, 83)["grid_charging"] is True


def test_teslemetry_tbc_export_uses_the_real_reserve_not_the_export_target():
    """Exporting leaves reserve at the real reserve; the export target is advisory under this mode.

    Reserve is not overloaded as an export floor here: Tesla decides how much to move, and window
    length is the lever for the target, so writing the target as a floor would second-guess that.
    """
    api = _tbc_api(discharge={"start_time": "17:00:00", "end_time": "19:00:00", "soc": 20, "enable": 1}, reserve=5)
    state = api.evaluate_schedule_tbc(18 * 60, 60)
    assert state == {"export_rule": "battery_ok", "grid_charging": False, "reserve": 5, "mode": "autonomous"}


def test_teslemetry_tbc_export_stops_at_the_target():
    """Once down to the target the export rule drops back to pv_only, reserve still the real reserve."""
    api = _tbc_api(discharge={"start_time": "17:00:00", "end_time": "19:00:00", "soc": 20, "enable": 1}, reserve=5)
    assert api.evaluate_schedule_tbc(18 * 60, 20) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 5, "mode": "autonomous"}


def test_teslemetry_tbc_demand_is_autonomous_with_no_grid_charging():
    """Outside both windows: autonomous on the base band, real reserve, and no route to import."""
    api = _tbc_api()
    assert api.evaluate_schedule_tbc(12 * 60, 50) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 15, "mode": "autonomous"}


def test_teslemetry_tbc_charge_wins_over_an_overlapping_export():
    """Charge is tested first, matching execute.py and the real-rate path's own precedence."""
    api = _tbc_api(
        charge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1},
        discharge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 20, "enable": 1},
    )
    assert api.evaluate_schedule_tbc(3 * 60, 40)["grid_charging"] is True


def test_teslemetry_tbc_reserve_hold_above_80_becomes_100_with_grid_off():
    """Predbat's own reserve hold (soc+1) lands in the band Tesla rejects and must round UP, not down.

    execute.py writes adjust_reserve(soc+1) under set_reserve_hold, so a battery at 85% asks for 86.
    Snapping that to 80 would let it discharge 6% during a hold; 100 actually holds, and grid
    charging off is what stops it importing to reach 100.
    """
    api = _tbc_api(reserve=86)
    assert api.evaluate_schedule_tbc(12 * 60, 85) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 100, "mode": "autonomous"}


def test_teslemetry_tbc_reserve_hold_suppresses_grid_charging_inside_a_charge_window():
    """A hold reserve inside a charge window must not import to fill to 100 against the 0p band."""
    api = _tbc_api(charge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}, reserve=86)
    state = api.evaluate_schedule_tbc(3 * 60, 40)
    assert state["reserve"] == 100
    assert state["grid_charging"] is False


def test_teslemetry_tbc_never_writes_a_reserve_in_the_invalid_band():
    """Tesla snaps 81-99 to 80, so no state may ask for a reserve in that band, whatever is committed.

    Sweeps every reserve Predbat could write, in each of the three states, since the reserve is now
    the single value that reaches the device from all of them.
    """
    for reserve in range(0, 101):
        states = [
            _tbc_api(charge={"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}, reserve=reserve).evaluate_schedule_tbc(3 * 60, 40),
            _tbc_api(discharge={"start_time": "17:00:00", "end_time": "19:00:00", "soc": 20, "enable": 1}, reserve=reserve).evaluate_schedule_tbc(18 * 60, 60),
            _tbc_api(reserve=reserve).evaluate_schedule_tbc(12 * 60, 50),
        ]
        for state in states:
            assert state["reserve"] <= 80 or state["reserve"] == 100
```

- [ ] **Step 2: Register the new tests**

Add the ten new function names to the list in `test_teslemetry()`, as in Task 1 Step 3.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `tools/triage_test.sh teslemetry /tmp/tesla-test.log`
Expected: FAIL with `AttributeError: 'MockTeslemetryAPI' object has no attribute 'evaluate_schedule_tbc'`.

- [ ] **Step 4: Implement the mapping**

In `apps/predbat/teslemetry.py`, add both methods immediately after `evaluate_schedule` (which ends at line 545):

```python
    @staticmethod
    def _settable_reserve(percent):
        """Map a requested reserve onto one the Powerwall will actually hold.

        Since firmware 25.18.4 only 0-80 and exactly 100 are honoured; 81-99 are silently snapped
        down to 80. A request in that band is Predbat asking to hold above 80 - execute.py writes
        adjust_reserve(soc+1) under set_reserve_hold - and 80 would not hold it, leaving the battery
        free to discharge back down to 80 during a window meant to keep SOC flat. So it rounds UP to
        the one value above 80 that is honoured. Callers disable grid charging whenever this returns
        SIGNAL_HOLD_RESERVE, which is what stops the device importing to reach it.
        """
        percent = int(percent)
        if percent <= SIGNAL_MAX_SETTABLE_RESERVE:
            return percent
        return SIGNAL_HOLD_RESERVE

    def evaluate_schedule_tbc(self, minutes_now, soc):
        """Map the committed schedule to the device tuple under signal-tariff control (GH#4892).

        Mode is autonomous in every state, because Tesla's optimiser only acts on the pushed tariff
        under Time-Based Control - the tariff, not this tuple, is what asks for the charge or the
        export. Reserve therefore stops being an overloaded charge signal and is simply the reserve
        Predbat asked for, in every state; the charge and export target percentages are advisory
        under this mode, since Tesla decides how much energy actually moves, and window length rather
        than a reserve floor is the lever for them.

        Grid charging is enabled only while a charge is actually wanted and no hold is in force, so
        no other state can import unexpectedly whatever the optimiser decides.
        """
        charge = self.schedule.get("charge", {})
        discharge = self.schedule.get("discharge", {})
        reserve = self._settable_reserve(self.schedule.get("reserve", 20))
        if self.in_window(minutes_now, charge):
            target = int(charge.get("soc", 100))
            if soc >= target - SIGNAL_HOLD_DEADBAND_PERCENT:
                # At (or effectively at) target: hold. Reserve 100 stops the discharge and grid
                # charging off stops it importing to reach that reserve; solar may still charge,
                # which is what a freeze charge wants.
                return {"export_rule": "pv_only", "grid_charging": False, "reserve": SIGNAL_HOLD_RESERVE, "mode": "autonomous"}
            # Charging. A reserve that came back as SIGNAL_HOLD_RESERVE is a hold request Predbat
            # made in its own right, and must not be turned into an import up to 100% against the 0p
            # band, so grid charging is suppressed in that case.
            return {"export_rule": "pv_only", "grid_charging": reserve < SIGNAL_HOLD_RESERVE, "reserve": reserve, "mode": "autonomous"}
        if self.in_window(minutes_now, discharge):
            target = int(discharge.get("soc", 10))
            return {"export_rule": "battery_ok" if soc > target else "pv_only", "grid_charging": False, "reserve": reserve, "mode": "autonomous"}
        return {"export_rule": "pv_only", "grid_charging": False, "reserve": reserve, "mode": "autonomous"}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `tools/triage_test.sh teslemetry /tmp/tesla-test.log`
Expected: PASS, all pre-existing tests included.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): map device state for signal-tariff control (#4892)"
```

---

### Task 3: Gate both behaviours behind the trial setting

Wires the two new behaviours to a component arg that defaults off, so nothing changes for existing users, and documents it. Folded into one task because the arg, the two dispatch points, the docs row and the dictionary entry are one reviewable decision: "this feature is now reachable".

**Files:**
- Modify: `apps/predbat/components.py` (the `"teslemetry"` args block, lines 626-634)
- Modify: `apps/predbat/teslemetry.py` (`evaluate_schedule` line 524, `sync_tariff` line 1206)
- Modify: `docs/inverter-setup.md` (the Teslemetry configuration section)
- Modify: `.cspell/custom-dictionary-workspace.txt`
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Consumes: `build_signal_tariff` and `_charge_window` from Task 1, `evaluate_schedule_tbc` from Task 2.
- Produces: component arg `tbc_control` (apps.yaml key `teslemetry_tbc_control`), read as `self.tbc_control`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
def test_teslemetry_tbc_control_defaults_off_and_uses_the_real_rate_tariff():
    """With the trial setting off nothing changes: the real-rate builder is still what gets pushed."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.schedule = {"reserve": 15, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}, "discharge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "enable": 0}}
    assert api.tbc_control is False
    pushed = {}
    api.set_tariff = lambda tariff, force=False: _record_tariff(pushed, tariff)
    run_async(api.sync_tariff())
    # The real-rate path prices from rate_import, so a 28p flat import cannot render as the 0p/50p
    # signal bands - asserting the absence of the signal shape rather than an exact legacy body.
    assert pushed["tariff"]["energy_charges"]["AllYear"]["rates"] != {"SUPER_OFF_PEAK": 0.0, "PARTIAL_PEAK": 0.5, "ON_PEAK": 1.0}
    assert api.evaluate_schedule(3 * 60, 40)["mode"] == "backup"


def test_teslemetry_tbc_control_on_pushes_the_signal_tariff():
    """With the trial setting on, the committed windows drive the signal bands and autonomous mode."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.tbc_control = True
    api.schedule = {"reserve": 15, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}, "discharge": {"start_time": "17:00:00", "end_time": "19:00:00", "soc": 20, "enable": 1}}
    pushed = {}
    api.set_tariff = lambda tariff, force=False: _record_tariff(pushed, tariff)
    run_async(api.sync_tariff())
    assert pushed["tariff"]["energy_charges"]["AllYear"]["rates"] == {"SUPER_OFF_PEAK": 0.0, "PARTIAL_PEAK": 0.5, "ON_PEAK": 1.0}
    assert _signal_tier_at(pushed["tariff"], 0, 180) == "SUPER_OFF_PEAK"
    assert _signal_tier_at(pushed["tariff"], 0, 1080) == "ON_PEAK"
    assert api.evaluate_schedule(3 * 60, 40)["mode"] == "autonomous"
```

Add this helper next to `_signal_tier_at`:

```python
async def _record_tariff(pushed, tariff):
    """Stand in for set_tariff, capturing the tariff that would have been sent."""
    pushed["tariff"] = tariff
    return True
```

- [ ] **Step 2: Give the mock the new attribute**

In `MockTeslemetryAPI.__init__` in `apps/predbat/tests/test_teslemetry.py`, beside the existing `self.automatic = False` line, add:

```python
        self.tbc_control = False
```

- [ ] **Step 3: Register the new tests**

Add the two new function names to the list in `test_teslemetry()`.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `tools/triage_test.sh teslemetry /tmp/tesla-test.log`
Expected: FAIL — `test_teslemetry_tbc_control_on_pushes_the_signal_tariff` asserts the signal bands but `sync_tariff` still calls the real-rate builder, so the rates dict will not match.

- [ ] **Step 5: Declare the component arg**

In `apps/predbat/components.py`, inside the `"teslemetry"` entry's `"args"` dict (after the `"automatic"` line at 630), add:

```python
            "tbc_control": {"required": False, "default": False, "config": "teslemetry_tbc_control"},
```

- [ ] **Step 6: Dispatch on the arg**

In `apps/predbat/teslemetry.py`, add this as the first statement in the body of `evaluate_schedule`, immediately after its docstring (line 532):

```python
        if getattr(self, "tbc_control", False):
            return self.evaluate_schedule_tbc(minutes_now, soc)
```

Then in `sync_tariff` (line 1206), replace the single `tariff = ...` line so the body reads:

```python
        if self._is_read_only():
            return True
        if getattr(self, "tbc_control", False):
            tariff = self.build_signal_tariff(self._charge_window(), self._discharge_window())
        else:
            tariff = self.build_tariff(self._discharge_window())
        return await self.set_tariff(tariff)
```

`getattr` rather than plain attribute access at both sites: the standalone CLI harness at the bottom of this module constructs the class without going through the component arg system, so the attribute is not guaranteed to exist there.

- [ ] **Step 7: Add a sentence to each dispatching docstring**

Append to `evaluate_schedule`'s docstring, as its own paragraph:

```
        With teslemetry_tbc_control set this delegates to evaluate_schedule_tbc, which drives Tesla's
        own optimiser through the tariff instead of asserting a charge directly - see GH#4892.
```

Append to `sync_tariff`'s docstring, as its own paragraph:

```
        With teslemetry_tbc_control set the signal tariff is pushed instead of the real-rate one; it
        depends only on the committed windows, so it re-pushes strictly less often.
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `tools/triage_test.sh teslemetry /tmp/tesla-test.log`
Expected: PASS, including every pre-existing test — the default-off path must be unchanged.

- [ ] **Step 9: Document the setting**

In `docs/inverter-setup.md`, find the Teslemetry configuration section that lists `teslemetry_automatic` and add an entry for `teslemetry_tbc_control` in the same format, saying: it is off by default; when on, Predbat pushes a control-signal tariff (0p over the charge window, 100p over the export window, 50p import elsewhere) and switches the Powerwall to Time-Based Control so Tesla's Opticaster runs the charge at full rate rather than the slower reserve-driven charge; it is a trial setting, and while it is on Predbat's charge and export target percentages are advisory because Tesla decides how much to move.

- [ ] **Step 10: Add Opticaster to the spell dictionary**

Add `Opticaster` to `.cspell/custom-dictionary-workspace.txt`. The file is auto-sorted on commit, so run pre-commit and re-stage it.

- [ ] **Step 11: Run the full pre-commit suite**

Run: `./run_pre_commit`
Expected: all hooks pass. Re-stage any file the hooks rewrite, then run it again to confirm a clean pass.

- [ ] **Step 12: Commit**

```bash
git add apps/predbat/components.py apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py docs/inverter-setup.md .cspell/custom-dictionary-workspace.txt
git commit -m "feat(teslemetry): add the teslemetry_tbc_control trial setting (#4892)"
```

---
### Task 4: Guard manual freeze export by the inverter's capability

Independent of Tasks 1–3 and worth doing either way, but required before the trial: a manual freeze export currently reaches the component on an inverter that declares freeze export unsupported, and under the signal tariff it would carve the 100p band over a window meant to hold.

**Files:**
- Modify: `apps/predbat/plan.py:4405-4406`
- Test: `apps/predbat/tests/test_manual_overrides.py`

**Interfaces:**
- Consumes: `self.set_export_freeze` (already forced off from `INVERTER_DEF[...]["support_discharge_freeze"]` by `execute.py:962-966`), and `EXPORT_LIMIT_IDLE`, already imported in the test module at `test_manual_overrides.py:10` and in `plan.py`.
- Produces: no new symbols.

**Conventions in this test module** (read `test_manual_overrides.py:14-80` before writing): every test takes `my_predbat`, returns a truthy value on failure, and is aggregated in `run_manual_overrides_tests` as `failed |= test_name(my_predbat)`. `setup(my_predbat, ...)` puts one charge and one export window at minute 720 into a known state; `check_export_limit(name, my_predbat, expected)` takes the test name first and returns the failure bool.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_manual_overrides.py`, after `test_manual_freeze_export_unaffected_by_export_freeze_only`:

```python
def test_manual_freeze_export_dropped_when_inverter_cannot_freeze(my_predbat):
    """An inverter that cannot freeze export must not have a manual freeze export planned for it.

    execute.py forces set_export_freeze off from the inverter's support_discharge_freeze capability
    (TESLA declares it False), but this override wrote EXPORT_LIMIT_FREEZE regardless - so the plan
    assumed a hold the hardware would never perform. Falls back to demand rather than a forced
    export: the user asked to hold the battery, and exporting it would be the opposite request.
    """
    setup(my_predbat)
    my_predbat.set_export_freeze = False
    my_predbat.manual_freeze_export_times = [720]
    my_predbat.optimise_charge_windows_manual()
    return check_export_limit("test_manual_freeze_export_dropped_when_inverter_cannot_freeze", my_predbat, EXPORT_LIMIT_IDLE)
```

- [ ] **Step 2: Register the test and reset the switch it leaves behind**

`set_export_freeze` is read all over the planner and this module shares one `PredBat` instance with every other test module, so leaving it False would silently change later tests. The existing `finally` block exists for exactly this reason — extend it rather than adding a new one.

In `run_manual_overrides_tests`, add the call alongside the other export tests:

```python
        failed |= test_manual_freeze_export_dropped_when_inverter_cannot_freeze(my_predbat)
```

and add to the existing `finally` block, beside the two switches already reset there:

```python
        my_predbat.set_export_freeze = True
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `tools/triage_test.sh manual_overrides /tmp/manual-test.log`
Expected: FAIL, printing `ERROR: test_manual_freeze_export_dropped_when_inverter_cannot_freeze - export limit should be 100.0 got 99.0`.

- [ ] **Step 4: Implement the guard**

In `apps/predbat/plan.py`, replace lines 4405-4406:

```python
                elif self.export_window_best[window_n]["start"] in self.manual_freeze_export_times:
                    self.export_limits_best[window_n] = EXPORT_LIMIT_FREEZE
```

with:

```python
                elif self.export_window_best[window_n]["start"] in self.manual_freeze_export_times:
                    if not self.set_export_freeze:
                        # execute.py forces set_export_freeze off for an inverter whose INVERTER_DEF
                        # says support_discharge_freeze is False, but this override wrote a freeze
                        # anyway - so the plan assumed a hold the hardware cannot perform. Drop to
                        # demand rather than a forced export: the user asked to hold the battery, and
                        # exporting it is the opposite of that request (GH#4892).
                        self.log("Warn: Manual freeze export time {} dropped to demand as this inverter does not support freeze export".format(self.time_abs_str(self.export_window_best[window_n]["start"])))
                        self.export_limits_best[window_n] = EXPORT_LIMIT_IDLE
                    else:
                        self.export_limits_best[window_n] = EXPORT_LIMIT_FREEZE
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `tools/triage_test.sh manual_overrides /tmp/manual-test.log`
Expected: PASS, with all eight pre-existing tests in the module still passing.

- [ ] **Step 6: Confirm no ordering pollution**

Run the two modules that share planner state in one go and confirm both still pass:

Run: `tools/triage_test.sh manual_overrides /tmp/manual-test.log` then `tools/triage_test.sh plan_tiebreak /tmp/plan-test.log`
Expected: PASS for both. A test that passes alone but fails in a full run is the shared-instance pollution this module's `finally` block guards against.

- [ ] **Step 7: Run the pre-commit suite**

Run: `./run_pre_commit`
Expected: all hooks pass.

- [ ] **Step 8: Commit**

```bash
git add apps/predbat/plan.py apps/predbat/tests/test_manual_overrides.py
git commit -m "fix(plan): don't plan a manual freeze export on an inverter that cannot do it (#4892)"
```

---

## Verification before opening the PR

- [ ] Run both touched test modules and confirm they pass:
  `tools/triage_test.sh teslemetry /tmp/tesla-test.log` then `tools/triage_test.sh manual_overrides /tmp/manual-test.log`
- [ ] Run `./run_pre_commit` from the repo root and confirm a clean pass.
- [ ] Run `detect_changes({scope: "compare", base_ref: "main"})` via the GitNexus MCP tool and confirm the affected symbols are only those this plan names — `build_signal_tariff`, `_signal_layout`, `_window_intervals`, `_charge_window`, `evaluate_schedule_tbc`, `evaluate_schedule`, `sync_tariff` and `optimise_charge_windows_manual`.
- [ ] Confirm by inspection that with `teslemetry_tbc_control` unset, `sync_tariff` and `evaluate_schedule` take exactly the paths they took before this work.
- [ ] State plainly in the PR description that the hardware behaviour is unverified, and list the five questions from the spec's "Hardware questions the trial must answer" section as what the trial needs to establish — in particular whether reserve 100 with grid charging disabled genuinely avoids importing, because the hold depends on it.

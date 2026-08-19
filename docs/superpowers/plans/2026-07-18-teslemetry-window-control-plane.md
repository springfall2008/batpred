# Teslemetry Window Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the service-hook control path of the TeslemetryAPI component (PR #4177) with a fox.py-style window control plane: a `TESLA` inverter type, virtual charge/discharge-window schedule entities driven directly by `inverter.py`, a component-side scheduler emulator that translates the committed windows into Powerwall REST commands, and `teslemetry_automatic` auto-configuration so a user needs only three config keys.

**Architecture:** Predbat's `Inverter` class programs charge/discharge windows by writing virtual `predbat_teslemetry_schedule_*` entities (time selects, SoC numbers, enable switches) and pressing a write button — exactly how it drives Fox. The writes route back into the component via the existing `predbat_teslemetry_` event filter, accumulate in a *pending* schedule, and the write button commits them atomically. Because the Powerwall has no native scheduler (unlike Fox Cloud), the component's `run()` loop acts as the scheduler: each 60s cycle it evaluates the committed schedule against the wall clock and live SOC, computes a desired device tuple `(tariff_mode, export_rule, grid_charging, reserve, mode)`, and asserts it through the existing deduped command layer (`_apply_command` write-on-change is kept unchanged, so command-credit spend stays flat and failed writes self-retry).

**Tech Stack:** Python 3 (async), aiohttp, existing Predbat component framework (`ComponentBase`), Storage component for persistence, existing `tests/test_teslemetry.py` mock harness.

## Global Constraints

- Line length: 256 chars (Black), 250 chars (Flake8) — run `./run_pre_commit` from repo root before finishing
- Docstrings: 100% coverage required (`interrogate`) — **every** new function, method, class, and test function needs a docstring
- Spell checking: British English (`en-gb`) via CSpell; add unknown valid words to `.cspell/custom-dictionary-workspace.txt`
- Variable naming: `lower_case_with_underscores`
- Tests run from `coverage/` dir; **always save test output to a file, then grep the file** (never pipe directly to grep)
- New test functions MUST be added to the `test_teslemetry()` runner at the bottom of `apps/predbat/tests/test_teslemetry.py`
- Use the Storage component for persistence, never direct file access
- Work on the existing branch `feat/tesla-powerwall-teslemetry`
- Git commits end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

## Design decisions (agreed in review — do not re-litigate)

1. **No service hooks.** The five `*_service` hooks from `templates/tesla_powerwall.yaml` are replaced entirely by window entities + emulator.
2. **`support_discharge_freeze: False`** — the Powerwall cannot pin SOC while exporting PV (`backup` absorbs solar into the battery; `self_consumption`/`autonomous` discharge to load). `execute.py:794` automatically forces `set_export_freeze` off for such inverters. `support_charge_freeze: True` (`backup` + grid-charging-off holds during a charge window).
3. **`output_charge_control: "none"`** — no fake charge/discharge rate entities; the Powerwall has no rate registers. `battery_rate_max` is a modelling sensor only.
4. **Write-button atomicity** (`time_button_press: True`): entity writes are staged in `pending_schedule`; only the write button copies pending → committed. The emulator only ever reads the committed schedule, so a half-written window is never acted on.
5. **Device-state ownership:** when the component is enabled and Predbat is not read-only, the emulator asserts the full device tuple every cycle (deduped). This intentionally replaces the customer's device tariff with Predbat's built tariff. `set_read_only` gates ALL emulator device writes.
6. **Idle export rule is `pv_only`** (divergence from the template's `never`): with the battery full and excess solar, `never` would curtail PV export. Flagged in the PR description.
7. **The five existing control entities** (`operation_mode`, `backup_reserve`, `allow_charging_from_grid`, `allow_export`, `tariff_mode`) remain as diagnostic mirrors + manual overrides. Their event handlers stay; the emulator updates their states after successful asserts.
8. **`reserve` maps to Powerwall `backup_reserve`**; `has_reserve_soc: True`, `inverter_reserve_max: 80` (Predbat treats 80–100 as 100).

## File Structure

- Modify: `apps/predbat/config.py` — add `INVERTER_DEF["TESLA"]`, add `teslemetry_automatic` to `APPS_SCHEMA`
- Modify: `apps/predbat/components.py` — add `automatic` arg + `can_restart` to the `teslemetry` entry
- Modify: `apps/predbat/teslemetry.py` — schedule model, window math, schedule entities, event routing, persistence, scheduler emulator, `automatic_config()`, extra site_info sensors
- Modify: `apps/predbat/tests/test_teslemetry.py` — all new tests + mock harness extensions
- Rewrite: `templates/tesla_powerwall.yaml` — component-based config
- Modify: `docs/components.md`, `docs/inverter-setup.md`

---

### Task 1: `TESLA` inverter type in `INVERTER_DEF`

**Files:**

- Modify: `apps/predbat/config.py` (insert after the `"FoxCloud"` entry which ends at line 1919)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Produces: `INVERTER_DEF["TESLA"]` dict consumed by `inverter.py` (read via `INVERTER_DEF[self.inverter_type][...]` at `inverter.py:207-243`). Every key present in the `"FoxCloud"` entry must be present here because `inverter.py` reads most of them unconditionally.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_teslemetry.py` (after `test_teslemetry_reconcile_forces_write_even_if_cache_preseeded`, before the `test_teslemetry()` runner):

```python
def test_teslemetry_inverter_def_tesla():
    """TESLA inverter type is registered with window control, no rate control and no export freeze."""
    from config import INVERTER_DEF

    tesla = INVERTER_DEF.get("TESLA")
    assert tesla is not None
    assert tesla["name"] == "Tesla Powerwall"
    assert tesla["has_charge_enable_time"] is True
    assert tesla["has_discharge_enable_time"] is True
    assert tesla["has_target_soc"] is True
    assert tesla["has_reserve_soc"] is True
    assert tesla["charge_time_entity_is_option"] is True
    assert tesla["charge_time_format"] == "HH:MM:SS"
    assert tesla["time_button_press"] is True
    assert tesla["output_charge_control"] == "none"
    assert tesla["support_charge_freeze"] is True
    assert tesla["support_discharge_freeze"] is False
    assert tesla["can_span_midnight"] is False
    assert tesla["target_soc_used_for_discharge"] is True
    # inverter.py reads the FoxCloud key set unconditionally - TESLA must not miss any of them
    for key in INVERTER_DEF["FoxCloud"]:
        assert key in tesla, "TESLA INVERTER_DEF missing key {}".format(key)
```

Register it in the `test_teslemetry()` runner (add the call just before the `print` line):

```python
    test_teslemetry_inverter_def_tesla()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t1.log 2>&1; grep -E "AssertionError|Error|passed" /tmp/teslemetry_t1.log
```

Expected: FAIL (`tesla is not None` assertion fails — no TESLA entry yet).

- [ ] **Step 3: Add the INVERTER_DEF entry**

In `apps/predbat/config.py`, insert immediately after the closing `},` of the `"FoxCloud"` entry (line 1919), before `"SolaxCloud"`:

```python
    "TESLA": {
        "name": "Tesla Powerwall",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "none",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "has_timed_pause": False,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "has_ge_eco_toggle": False,
        "has_fox_inverter_mode": False,
        "time_button_press": True,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_charge_freeze": True,
        "support_discharge_freeze": False,
        "has_idle_time": False,
        "can_span_midnight": False,
        "charge_discharge_with_rate": False,
        "target_soc_used_for_discharge": True,
    },
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t1.log 2>&1; grep -E "AssertionError|Error|tests passed" /tmp/teslemetry_t1.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/config.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): add TESLA inverter type to INVERTER_DEF

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Config plumbing — `teslemetry_automatic`

**Files:**

- Modify: `apps/predbat/components.py:370-379` (teslemetry entry)
- Modify: `apps/predbat/config.py` (`APPS_SCHEMA`, next to the existing `teslemetry_base_url` key)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Produces: component arg `automatic` (bool, default False) passed into `TeslemetryAPI.initialize(**kwargs)` — absorbed by the existing `**kwargs` until Task 4 adds the named parameter.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
def test_teslemetry_component_registry_config():
    """Component registry exposes the automatic arg, can_restart, and the schema accepts teslemetry_automatic."""
    from components import COMPONENT_LIST
    from config import APPS_SCHEMA

    entry = COMPONENT_LIST["teslemetry"]
    assert entry["args"]["automatic"]["config"] == "teslemetry_automatic"
    assert entry["args"]["automatic"]["default"] is False
    assert entry["args"]["automatic"]["required"] is False
    assert entry.get("can_restart") is True
    assert APPS_SCHEMA["teslemetry_automatic"] == {"type": "boolean"}
```

Register in the `test_teslemetry()` runner:

```python
    test_teslemetry_component_registry_config()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t2.log 2>&1; grep -E "KeyError|AssertionError|tests passed" /tmp/teslemetry_t2.log
```

Expected: FAIL with `KeyError: 'automatic'`.

- [ ] **Step 3: Implement**

In `apps/predbat/components.py`, change the teslemetry entry to:

```python
    "teslemetry": {
        "class": TeslemetryAPI,
        "name": "Tesla Powerwall (Teslemetry)",
        "event_filter": "predbat_teslemetry_",
        "args": {
            "key": {"required": True, "config": "teslemetry_key"},
            "site_id": {"required": True, "config": "teslemetry_site_id"},
            "base_url": {"required": False, "config": "teslemetry_base_url", "default": "https://api.teslemetry.com"},
            "automatic": {"required": False, "default": False, "config": "teslemetry_automatic"},
        },
        "phase": 1,
        "can_restart": True,
    },
```

In `apps/predbat/config.py`, find the existing `"teslemetry_base_url"` line in `APPS_SCHEMA` and add below it:

```python
    "teslemetry_automatic": {"type": "boolean"},
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t2.log 2>&1; grep -E "AssertionError|tests passed" /tmp/teslemetry_t2.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/components.py apps/predbat/config.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): wire teslemetry_automatic config and can_restart

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Extra site_info sensors + live SOC tracking

**Files:**

- Modify: `apps/predbat/teslemetry.py` (`fetch_site_info`, `fetch_live_status`)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Produces: sensors `sensor.<prefix>_teslemetry_battery_rate_max` (W) and `sensor.<prefix>_teslemetry_inverter_limit` (W) — referenced by `automatic_config()` in Task 8. Attribute `self.last_soc` (float or None) — consumed by the emulator in Task 7.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
SITE_INFO_FULL = {
    "response": {
        "nameplate_energy": 13500,
        "nameplate_power": 11500,
        "max_site_meter_power_ac": 11500,
        "default_real_mode": "self_consumption",
        "backup_reserve_percent": 20,
    }
}


def test_teslemetry_site_info_publishes_rate_and_limit():
    """site_info nameplate power and site AC limit are published as W sensors for automatic config."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_battery_rate_max"]["state"] == 11500
    assert api.dashboard_items["sensor.predbat_teslemetry_inverter_limit"]["state"] == 11500


def test_teslemetry_site_info_limit_kw_normalised():
    """A max_site_meter_power_ac reported in kW (small magnitude) is normalised to W."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = {"response": {"nameplate_energy": 13500, "nameplate_power": 11500, "max_site_meter_power_ac": 11.5}}
    run_async(api.fetch_site_info())
    assert api.dashboard_items["sensor.predbat_teslemetry_inverter_limit"]["state"] == 11500


def test_teslemetry_live_status_tracks_last_soc():
    """fetch_live_status records the live SOC for the scheduler emulator."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    run_async(api.fetch_live_status())
    assert api.last_soc == 55.5
```

Register all three in the `test_teslemetry()` runner. Also add `self.last_soc = None` to `MockTeslemetryAPI.__init__` so pre-existing tests are unaffected.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t3.log 2>&1; grep -E "KeyError|AssertionError|tests passed" /tmp/teslemetry_t3.log
```

Expected: FAIL with `KeyError: 'sensor.predbat_teslemetry_battery_rate_max'`.

- [ ] **Step 3: Implement**

In `apps/predbat/teslemetry.py` `fetch_site_info`, after the `nameplate_wh` block (after `soc_max_published = True`), add:

```python
        nameplate_power = response.get("nameplate_power", 0)
        if nameplate_power:
            self.publish_sensor("battery_rate_max", nameplate_power, unit="W", state_class=None, friendly="Powerwall Max Rate")
        site_limit = response.get("max_site_meter_power_ac", 0) or nameplate_power
        if site_limit and site_limit < 100:
            # Some sites report this field in kW; normalise to W
            site_limit = site_limit * 1000
        if site_limit:
            self.publish_sensor("inverter_limit", int(site_limit), unit="W", state_class=None, friendly="Powerwall Site Limit")
```

In `fetch_live_status`, after `response = data.get("response", {})`, add:

```python
        self.last_soc = response.get("percentage_charged", self.last_soc)
```

In `initialize()`, after `self.reconcile_done = False`, add:

```python
        self.last_soc = None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t3.log 2>&1; grep -E "AssertionError|tests passed" /tmp/teslemetry_t3.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): publish battery_rate_max/inverter_limit sensors, track live SOC

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Schedule model + window math + state evaluation (pure logic)

**Files:**

- Modify: `apps/predbat/teslemetry.py`
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Produces:
    - `DEFAULT_SCHEDULE` (module constant), `OPTIONS_TIME_FULL` (module constant, per-minute `"HH:MM:00"` strings)
    - `TeslemetryAPI.time_to_minutes(value: str) -> int` (staticmethod)
    - `TeslemetryAPI.in_window(minutes_now: int, window: dict) -> bool` (staticmethod; window dict keys: `start_time`, `end_time`, `enable`)
    - `TeslemetryAPI.evaluate_schedule(minutes_now: int, soc: float) -> dict` returning exactly `{"tariff_mode": str, "export_rule": str, "grid_charging": bool, "reserve": int, "mode": str}`
    - Instance attrs `self.schedule`, `self.pending_schedule` (dicts shaped like `DEFAULT_SCHEDULE`), `self.schedule_loaded` (bool), `self.automatic` (bool), `self.automatic_done` (bool)

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py` (also change the import line at the top of the file to `from teslemetry import TeslemetryAPI, OPERATION_MODES, OPTIONS_TIME_FULL, DEFAULT_SCHEDULE` and add `import copy` to the imports):

```python
def test_teslemetry_time_to_minutes():
    """HH:MM:SS strings convert to minutes since midnight; garbage converts to 0."""
    assert TeslemetryAPI.time_to_minutes("00:00:00") == 0
    assert TeslemetryAPI.time_to_minutes("05:30:00") == 330
    assert TeslemetryAPI.time_to_minutes("23:59:00") == 1439
    assert TeslemetryAPI.time_to_minutes("garbage") == 0


def test_teslemetry_in_window():
    """Window membership: inclusive start, exclusive end, disabled and midnight-wrap cases."""
    window = {"enable": 1, "start_time": "01:00:00", "end_time": "05:00:00"}
    assert TeslemetryAPI.in_window(60, window) is True
    assert TeslemetryAPI.in_window(299, window) is True
    assert TeslemetryAPI.in_window(300, window) is False
    assert TeslemetryAPI.in_window(0, window) is False
    assert TeslemetryAPI.in_window(60, {**window, "enable": 0}) is False
    assert TeslemetryAPI.in_window(60, {**window, "end_time": "01:00:00"}) is False
    wrap = {"enable": 1, "start_time": "23:00:00", "end_time": "01:00:00"}
    assert TeslemetryAPI.in_window(23 * 60 + 30, wrap) is True
    assert TeslemetryAPI.in_window(30, wrap) is True
    assert TeslemetryAPI.in_window(12 * 60, wrap) is False


def test_teslemetry_evaluate_schedule_states():
    """The five reachable device states: charging, hold-at-target, exporting, discharge floor, idle."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1},
        "discharge": {"start_time": "17:00:00", "end_time": "19:00:00", "soc": 30, "enable": 1},
    }
    assert api.evaluate_schedule(2 * 60, 50) == {"tariff_mode": "normal", "export_rule": "never", "grid_charging": True, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(2 * 60, 90) == {"tariff_mode": "normal", "export_rule": "never", "grid_charging": False, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(18 * 60, 80) == {"tariff_mode": "export_now", "export_rule": "battery_ok", "grid_charging": False, "reserve": 30, "mode": "autonomous"}
    assert api.evaluate_schedule(18 * 60, 30) == {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": False, "reserve": 30, "mode": "self_consumption"}
    assert api.evaluate_schedule(12 * 60, 60) == {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}


def test_teslemetry_evaluate_schedule_charge_precedence():
    """When charge and discharge windows overlap, charge wins (matches execute.py ordering)."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 100, "enable": 1},
        "discharge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 10, "enable": 1},
    }
    assert api.evaluate_schedule(2 * 60, 50)["mode"] == "backup"
```

Register all four in the `test_teslemetry()` runner. Extend `MockTeslemetryAPI.__init__` with the new schedule attributes (add `import copy` at the top of the test file if not present):

```python
        self.schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.pending_schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.schedule_loaded = False
        self.automatic = False
        self.automatic_done = False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t4.log 2>&1; grep -E "ImportError|AttributeError|AssertionError|tests passed" /tmp/teslemetry_t4.log
```

Expected: FAIL with `ImportError: cannot import name 'OPTIONS_TIME_FULL'`.

- [ ] **Step 3: Implement**

In `apps/predbat/teslemetry.py`, add `import copy` to the imports. After the `TARIFF_MODES` constant, add:

```python
OPTIONS_TIME_FULL = ["{:02d}:{:02d}:00".format(hour, minute) for hour in range(24) for minute in range(60)]

DEFAULT_SCHEDULE = {
    "reserve": 20,
    "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": 0},
    "discharge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "enable": 0},
}
```

In `initialize()`, change the signature to accept the automatic flag and add the schedule state (after `self.last_soc = None`):

```python
    def initialize(self, key="", site_id="", base_url=TESLEMETRY_DEFAULT_URL, automatic=False, **kwargs):
```

(Update the docstring's Args accordingly: `automatic: Automatically configure Predbat's inverter args to use this component (fox-style).`)

```python
        self.automatic = automatic
        self.automatic_done = False
        self.schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.pending_schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.schedule_loaded = False
```

Add the three pure methods to `TeslemetryAPI` (after `register_control_entities`):

```python
    @staticmethod
    def time_to_minutes(value):
        """Convert an HH:MM[:SS] time string to minutes since midnight, or 0 on garbage."""
        try:
            parts = str(value).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def in_window(minutes_now, window):
        """Return True if minutes_now falls inside an enabled schedule window (inclusive start, exclusive end, midnight wrap supported; start == end means disabled)."""
        if not window.get("enable"):
            return False
        start = TeslemetryAPI.time_to_minutes(window.get("start_time", "00:00:00"))
        end = TeslemetryAPI.time_to_minutes(window.get("end_time", "00:00:00"))
        if start == end:
            return False
        if end > start:
            return start <= minutes_now < end
        return minutes_now >= start or minutes_now < end

    def evaluate_schedule(self, minutes_now, soc):
        """Map the committed schedule + wall clock + live SOC to the desired device tuple.

        Returns a dict with keys tariff_mode, export_rule, grid_charging, reserve and mode.
        Charge window wins over an overlapping discharge window (matches execute.py ordering).
        Charging uses backup mode (proven template semantics); the hold state (SOC at target,
        which is also how Predbat expresses charge freeze) is backup + grid charging off.
        Export uses the tariff-trick + autonomous with the device reserve as the discharge floor.
        Idle allows PV-only export so excess solar is never curtailed.
        """
        charge = self.schedule.get("charge", {})
        discharge = self.schedule.get("discharge", {})
        reserve = self.schedule.get("reserve", 20)
        if self.in_window(minutes_now, charge):
            target = int(charge.get("soc", 100))
            if soc >= target:
                return {"tariff_mode": "normal", "export_rule": "never", "grid_charging": False, "reserve": target, "mode": "backup"}
            return {"tariff_mode": "normal", "export_rule": "never", "grid_charging": True, "reserve": target, "mode": "backup"}
        if self.in_window(minutes_now, discharge):
            target = int(discharge.get("soc", 10))
            if soc > target:
                return {"tariff_mode": "export_now", "export_rule": "battery_ok", "grid_charging": False, "reserve": target, "mode": "autonomous"}
            return {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": False, "reserve": target, "mode": "self_consumption"}
        return {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": True, "reserve": int(reserve), "mode": "self_consumption"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t4.log 2>&1; grep -E "AssertionError|tests passed" /tmp/teslemetry_t4.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): schedule model, window math and device-state evaluation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Schedule entities + event routing + write-button staging

**Files:**

- Modify: `apps/predbat/teslemetry.py`
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Consumes: `OPTIONS_TIME_FULL`, `DEFAULT_SCHEDULE`, `self.pending_schedule`, `self.schedule` (Task 4)
- Produces:
    - Entities: `number.<prefix>_teslemetry_schedule_reserve`, `select.<prefix>_teslemetry_schedule_{charge,discharge}_{start_time,end_time}`, `number.<prefix>_teslemetry_schedule_{charge,discharge}_soc`, `switch.<prefix>_teslemetry_schedule_{charge,discharge}_enable`, `switch.<prefix>_teslemetry_schedule_write`
    - `publish_schedule_entities()` — renders `pending_schedule` into the entities
    - `schedule_event(entity_id, value)` — async; stages edits, commits on write button
    - `apply_schedule()` — async; commits pending → committed, persists (Task 6), asserts device (Task 7). In THIS task it is a stub that copies + republishes only:

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
def test_teslemetry_schedule_entities_published():
    """Schedule entities are published with option lists, ranges and safe defaults."""
    api = MockTeslemetryAPI()
    api.publish_schedule_entities()
    assert api.dashboard_items["select.predbat_teslemetry_schedule_charge_start_time"]["attributes"]["options"] == OPTIONS_TIME_FULL
    assert api.dashboard_items["select.predbat_teslemetry_schedule_discharge_end_time"]["state"] == "00:00:00"
    assert api.dashboard_items["number.predbat_teslemetry_schedule_reserve"]["state"] == 20
    assert api.dashboard_items["number.predbat_teslemetry_schedule_charge_soc"]["state"] == 100
    assert api.dashboard_items["number.predbat_teslemetry_schedule_discharge_soc"]["state"] == 10
    assert api.dashboard_items["switch.predbat_teslemetry_schedule_charge_enable"]["state"] == "off"
    assert api.dashboard_items["switch.predbat_teslemetry_schedule_write"]["state"] == "off"


def test_teslemetry_schedule_edits_stage_without_device_writes():
    """Entity writes accumulate in pending_schedule, mirror into entity state, and send nothing to the device."""
    api = MockTeslemetryAPI()
    run_async(api.select_event("select.predbat_teslemetry_schedule_charge_start_time", "01:30:00"))
    run_async(api.select_event("select.predbat_teslemetry_schedule_charge_end_time", "05:00:00"))
    run_async(api.number_event("number.predbat_teslemetry_schedule_charge_soc", 90))
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_charge_enable", "turn_on"))
    assert api.pending_schedule["charge"] == {"start_time": "01:30:00", "end_time": "05:00:00", "soc": 90, "enable": 1}
    assert api.schedule["charge"]["enable"] == 0
    assert api.requests_made == []
    assert api.entity_states["select.predbat_teslemetry_schedule_charge_start_time"] == "01:30:00"
    assert api.entity_states["switch.predbat_teslemetry_schedule_charge_enable"] == "on"


def test_teslemetry_schedule_write_button_commits():
    """The write button copies pending to committed and leaves the button off."""
    api = MockTeslemetryAPI()
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_discharge_enable", "turn_on"))
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_write", "turn_on"))
    assert api.schedule["discharge"]["enable"] == 1
    assert api.entity_states["switch.predbat_teslemetry_schedule_write"] == "off"


def test_teslemetry_schedule_invalid_values_rejected():
    """Garbage times and non-numeric SOC values are rejected or clamped without corrupting the schedule."""
    api = MockTeslemetryAPI()
    run_async(api.select_event("select.predbat_teslemetry_schedule_charge_start_time", "25:99:00"))
    assert api.pending_schedule["charge"]["start_time"] == "00:00:00"
    run_async(api.number_event("number.predbat_teslemetry_schedule_charge_soc", "banana"))
    assert api.pending_schedule["charge"]["soc"] == 100
    run_async(api.number_event("number.predbat_teslemetry_schedule_reserve", 150))
    assert api.pending_schedule["reserve"] == 100


def test_teslemetry_schedule_reserve_applies_immediately():
    """Reserve edits commit without the write button (fox parity) and persist into both schedules."""
    api = MockTeslemetryAPI()
    run_async(api.number_event("number.predbat_teslemetry_schedule_reserve", 35))
    assert api.pending_schedule["reserve"] == 35
    assert api.schedule["reserve"] == 35
```

Register all five in the `test_teslemetry()` runner.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t5.log 2>&1; grep -E "AttributeError|AssertionError|tests passed" /tmp/teslemetry_t5.log
```

Expected: FAIL with `AttributeError: ... 'publish_schedule_entities'`.

- [ ] **Step 3: Implement**

In `apps/predbat/teslemetry.py`, add after `evaluate_schedule`:

```python
    def publish_schedule_entities(self):
        """Publish the schedule entities from the pending schedule (pending == committed after boot/apply).

        Entity states must track pending edits immediately so inverter.py's write-and-poll
        verification sees its own writes reflected back.
        """
        sched = self.pending_schedule
        self.dashboard_item(
            self.entity("schedule_reserve", domain="number"),
            sched.get("reserve", 20),
            {"friendly_name": "Powerwall Schedule Reserve", "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "icon": "mdi:gauge"},
            app="teslemetry",
        )
        for direction in ["charge", "discharge"]:
            window = sched.get(direction, {})
            for attribute in ["start_time", "end_time"]:
                self.dashboard_item(
                    self.entity("schedule_{}_{}".format(direction, attribute), domain="select"),
                    window.get(attribute, "00:00:00"),
                    {"options": OPTIONS_TIME_FULL, "friendly_name": "Powerwall Schedule {} {}".format(direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:clock-outline"},
                    app="teslemetry",
                )
            self.dashboard_item(
                self.entity("schedule_{}_soc".format(direction), domain="number"),
                window.get("soc", 100 if direction == "charge" else 10),
                {"friendly_name": "Powerwall Schedule {} Soc".format(direction.capitalize()), "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "icon": "mdi:gauge"},
                app="teslemetry",
            )
            self.dashboard_item(
                self.entity("schedule_{}_enable".format(direction), domain="switch"),
                "on" if window.get("enable") else "off",
                {"friendly_name": "Powerwall Schedule {} Enable".format(direction.capitalize()), "icon": "mdi:check-circle-outline"},
                app="teslemetry",
            )
        self.dashboard_item(self.entity("schedule_write", domain="switch"), "off", {"friendly_name": "Powerwall Schedule Write", "icon": "mdi:content-save-outline"}, app="teslemetry")

    async def apply_schedule(self):
        """Commit the pending schedule atomically (extended with persistence and an immediate device assert in later tasks)."""
        self.schedule = copy.deepcopy(self.pending_schedule)
        self.publish_schedule_entities()

    async def schedule_event(self, entity_id, value):
        """Stage a schedule entity write into pending_schedule; the write switch commits it.

        Reserve applies immediately (fox parity) since it is not part of window atomicity.
        Invalid times fall back to 00:00:00 and non-numeric SOC/reserve values are rejected.
        """
        if entity_id.endswith("_schedule_write"):
            if value in ("turn_on", "toggle"):
                await self.apply_schedule()
            self.publish_schedule_entities()
            return
        if entity_id.endswith("_schedule_reserve"):
            try:
                reserve = max(0, min(100, int(float(value))))
            except (ValueError, TypeError):
                self.log("Warn: Teslemetry invalid schedule reserve value {}".format(value))
                return
            self.pending_schedule["reserve"] = reserve
            self.schedule["reserve"] = reserve
            self.publish_schedule_entities()
            return
        direction = None
        if "_schedule_charge_" in entity_id:
            direction = "charge"
        elif "_schedule_discharge_" in entity_id:
            direction = "discharge"
        if not direction:
            self.log("Warn: Teslemetry unhandled schedule event {} = {}".format(entity_id, value))
            return
        window = self.pending_schedule.setdefault(direction, {})
        if entity_id.endswith("_start_time") or entity_id.endswith("_end_time"):
            attribute = "start_time" if entity_id.endswith("_start_time") else "end_time"
            window[attribute] = value if value in OPTIONS_TIME_FULL else "00:00:00"
        elif entity_id.endswith("_soc"):
            try:
                window["soc"] = max(0, min(100, int(float(value))))
            except (ValueError, TypeError):
                self.log("Warn: Teslemetry invalid schedule soc value {}".format(value))
                return
        elif entity_id.endswith("_enable"):
            if value == "turn_on":
                window["enable"] = 1
            elif value == "turn_off":
                window["enable"] = 0
            elif value == "toggle":
                window["enable"] = 0 if window.get("enable") else 1
        else:
            self.log("Warn: Teslemetry unhandled schedule event {} = {}".format(entity_id, value))
            return
        self.publish_schedule_entities()
```

Route schedule entities in the three existing event handlers by inserting at the TOP of each:

In `select_event`:

```python
        if "_schedule_" in entity_id:
            await self.schedule_event(entity_id, value)
            return
```

In `number_event`:

```python
        if "_schedule_" in entity_id:
            await self.schedule_event(entity_id, value)
            return
```

In `switch_event` (note: the parameter is named `service`):

```python
        if "_schedule_" in entity_id:
            await self.schedule_event(entity_id, service)
            return
```

Finally call `self.publish_schedule_entities()` at the end of `register_control_entities()` so the entities exist from boot.

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t5.log 2>&1; grep -E "AssertionError|tests passed" /tmp/teslemetry_t5.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): schedule entities with staged edits and write-button commit

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Schedule persistence via the Storage component

**Files:**

- Modify: `apps/predbat/teslemetry.py`
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Consumes: `ComponentBase.storage` property (returns the storage component or None); `storage.save(module, filename, data, format=...)` / `storage.load(module, filename)` (`storage.py:69/85`)
- Produces: `save_schedule()` / `load_schedule()` — async; storage key `("teslemetry", "schedule")`, JSON format. `apply_schedule()` now persists.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self):
        """Create the empty in-memory store."""
        self.saved = {}

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record saved data keyed on (module, filename)."""
        self.saved[(module, filename)] = data
        return True

    async def load(self, module, filename):
        """Return previously saved data or None."""
        return self.saved.get((module, filename))


def test_teslemetry_schedule_persistence_roundtrip():
    """apply_schedule persists the committed schedule; load_schedule restores it and resets pending."""
    api = MockTeslemetryAPI()
    api.mock_storage = FakeStorage()
    api.pending_schedule["charge"]["enable"] = 1
    api.pending_schedule["charge"]["start_time"] = "02:00:00"
    run_async(api.apply_schedule())
    assert api.mock_storage.saved[("teslemetry", "schedule")]["charge"]["enable"] == 1
    api2 = MockTeslemetryAPI()
    api2.mock_storage = api.mock_storage
    run_async(api2.load_schedule())
    assert api2.schedule["charge"]["start_time"] == "02:00:00"
    assert api2.pending_schedule == api2.schedule


def test_teslemetry_schedule_load_without_storage_is_safe():
    """With no storage component available, load_schedule keeps the safe defaults."""
    api = MockTeslemetryAPI()
    run_async(api.load_schedule())
    assert api.schedule == DEFAULT_SCHEDULE
```

Register both in the `test_teslemetry()` runner. Add the storage hook to `MockTeslemetryAPI` (inside the class):

```python
    mock_storage = None

    @property
    def storage(self):
        """Return the fake storage component for tests (None by default)."""
        return self.mock_storage
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t6.log 2>&1; grep -E "AttributeError|AssertionError|tests passed" /tmp/teslemetry_t6.log
```

Expected: FAIL with `AttributeError: ... 'load_schedule'`.

- [ ] **Step 3: Implement**

In `apps/predbat/teslemetry.py`, add after `apply_schedule`:

```python
    async def save_schedule(self):
        """Persist the committed schedule via the Storage component (no-op when storage is unavailable)."""
        storage = self.storage
        if storage:
            await storage.save("teslemetry", "schedule", self.schedule, format="json")

    async def load_schedule(self):
        """Restore the committed schedule from storage at boot; keep safe defaults when absent.

        The Powerwall has no native scheduler to read the plan back from (unlike Fox Cloud),
        so persistence is what makes a schedule survive a restart mid-plan.
        """
        storage = self.storage
        data = await storage.load("teslemetry", "schedule") if storage else None
        if isinstance(data, dict) and "charge" in data and "discharge" in data:
            self.schedule = data
        self.pending_schedule = copy.deepcopy(self.schedule)
```

Update `apply_schedule` to persist (full replacement):

```python
    async def apply_schedule(self):
        """Commit the pending schedule atomically and persist it (immediate device assert added by the emulator task)."""
        self.schedule = copy.deepcopy(self.pending_schedule)
        await self.save_schedule()
        self.publish_schedule_entities()
```

Also update the reserve branch of `schedule_event` to persist the immediate commit — after `self.schedule["reserve"] = reserve` add:

```python
            await self.save_schedule()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t6.log 2>&1; grep -E "AssertionError|tests passed" /tmp/teslemetry_t6.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): persist committed schedule via Storage component

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Scheduler emulator — `assert_device_state` + `run()` integration

**Files:**

- Modify: `apps/predbat/teslemetry.py` (`assert_device_state`, `get_minutes_now`, `run()`, `apply_schedule`, module docstring)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Consumes: `evaluate_schedule` (Task 4), setters `set_tariff`/`set_export_rule`/`set_grid_charging`/`set_backup_reserve`/`set_operation_mode` (existing, deduped), `self.last_soc` (Task 3), `_is_read_only()` (existing)
- Produces: `assert_device_state(desired: dict) -> bool` (async), `get_minutes_now() -> int`. `run()` gains latches: `load_schedule` once, emulator assert each cycle when `schedule_loaded and reconcile_done and last_soc is not None and not read_only`.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
def command_ok_responses(api):
    """Register success responses for all four Powerwall command endpoints."""
    for path in ["operation", "backup", "grid_import_export", "time_of_use_settings"]:
        api.mock_responses["/api/1/energy_sites/123456/{}".format(path)] = {"response": {}}


def test_teslemetry_assert_device_state_posts_commands():
    """assert_device_state issues all four command groups and mirrors success into the diagnostic entities."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    desired = {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}
    assert run_async(api.assert_device_state(desired)) is True
    paths = [req[1] for req in api.requests_made if req[0] == "POST"]
    assert "/api/1/energy_sites/123456/time_of_use_settings" in paths
    assert "/api/1/energy_sites/123456/grid_import_export" in paths
    assert "/api/1/energy_sites/123456/backup" in paths
    assert "/api/1/energy_sites/123456/operation" in paths
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "self_consumption"
    assert api.entity_states["number.predbat_teslemetry_backup_reserve"] == 20
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "pv_only"


def test_teslemetry_assert_device_state_dedupes_repeat():
    """Asserting an unchanged desired state issues no further REST commands (write-on-change)."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    desired = {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}
    run_async(api.assert_device_state(desired))
    first_count = len(api.requests_made)
    run_async(api.assert_device_state(desired))
    assert len(api.requests_made) == first_count


def test_teslemetry_apply_schedule_asserts_immediately():
    """Committing a schedule mid-window asserts the device state without waiting for the next run cycle."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.last_soc = 50
    api.get_minutes_now = lambda: 2 * 60
    api.pending_schedule["charge"] = {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1}
    run_async(api.switch_event("switch.predbat_teslemetry_schedule_write", "turn_on"))
    posts = [(req[1], req[2]) for req in api.requests_made if req[0] == "POST"]
    assert ("/api/1/energy_sites/123456/operation", {"default_real_mode": "backup"}) in posts
    assert ("/api/1/energy_sites/123456/backup", {"backup_reserve_percent": 90}) in posts


def test_teslemetry_run_asserts_schedule_each_cycle():
    """A healthy run cycle evaluates the committed schedule and asserts the device state."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    api.get_minutes_now = lambda: 12 * 60
    assert run_async(api.run(seconds=0, first=True)) is True
    posts = [req[1] for req in api.requests_made if req[0] == "POST"]
    assert "/api/1/energy_sites/123456/operation" in posts


def test_teslemetry_run_skips_assert_when_read_only():
    """Read-only mode gates all emulator device writes."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    api._is_read_only = lambda: True
    run_async(api.run(seconds=0, first=True))
    assert [req for req in api.requests_made if req[0] == "POST"] == []


def test_teslemetry_run_skips_assert_without_soc():
    """The emulator never asserts before a live SOC reading exists (no blind mode changes)."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    # live_status has no mock response -> fetch fails -> last_soc stays None
    run_async(api.run(seconds=0, first=True))
    assert [req for req in api.requests_made if req[0] == "POST"] == []
```

Register all six in the `test_teslemetry()` runner.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t7.log 2>&1; grep -E "AttributeError|AssertionError|tests passed" /tmp/teslemetry_t7.log
```

Expected: FAIL with `AttributeError: ... 'assert_device_state'`.

- [ ] **Step 3: Implement**

In `apps/predbat/teslemetry.py`, add after `load_schedule`:

```python
    def get_minutes_now(self):
        """Return minutes since local midnight, preferring the base's clock (which Predbat schedules against)."""
        base = getattr(self, "base", None)
        minutes = getattr(base, "minutes_now", None) if base is not None else None
        if minutes is not None:
            return minutes
        now = datetime.now(timezone.utc).astimezone(getattr(self, "local_tz", None) or timezone.utc)
        return now.hour * 60 + now.minute

    async def assert_device_state(self, desired):
        """Assert the desired device tuple, tariff first and mode last (the template-proven ordering).

        Each setter dedupes on write-on-change, so an unchanged assert costs zero command credits.
        Successful writes are mirrored into the diagnostic control entities; failures leave both
        the dedupe cache and the entity state untouched so the next cycle retries.
        """
        results = {}
        results["tariff_mode"] = await self.set_tariff(desired["tariff_mode"])
        results["export_rule"] = await self.set_export_rule(desired["export_rule"])
        results["grid_charging"] = await self.set_grid_charging(desired["grid_charging"])
        results["reserve"] = await self.set_backup_reserve(desired["reserve"])
        results["mode"] = await self.set_operation_mode(desired["mode"])
        if results["tariff_mode"]:
            self.publish_control(self.entity("tariff_mode", domain="select"), desired["tariff_mode"])
        if results["export_rule"]:
            self.publish_control(self.entity("allow_export", domain="select"), desired["export_rule"])
        if results["grid_charging"]:
            self.publish_control(self.entity("allow_charging_from_grid", domain="switch"), "on" if desired["grid_charging"] else "off")
        if results["reserve"]:
            self.publish_control(self.entity("backup_reserve", domain="number"), int(desired["reserve"]))
        if results["mode"]:
            self.publish_control(self.entity("operation_mode", domain="select"), desired["mode"])
        if not all(results.values()):
            self.log("Warn: Teslemetry device-state assert incomplete: {}".format({key: value for key, value in results.items() if not value}))
        return all(results.values())
```

Replace the body of `run()` after the auth-failed block (keep everything from the docstring down to and including the `if self.api_auth_failed:` block unchanged) with:

```python
        success = True
        if not self.site_info_done:
            self.site_info_done = await self.fetch_site_info()
        if not self.reconcile_done:
            self.reconcile_done = await self.reconcile_on_start()
        if not self.schedule_loaded:
            await self.load_schedule()
            self.schedule_loaded = True
            self.publish_schedule_entities()
        if first or (seconds - self.last_live_poll >= LIVE_POLL_SECONDS):
            self.last_live_poll = seconds
            success = await self.fetch_live_status()
        if first or (seconds - self.last_energy_poll >= ENERGY_POLL_SECONDS):
            self.last_energy_poll = seconds
            await self.fetch_energy_today()
        if self.schedule_loaded and self.reconcile_done and self.last_soc is not None and not self._is_read_only():
            # Scheduler emulator: the Powerwall has no native scheduler, so translate the committed
            # windows into device commands each cycle. Failures are logged and self-retry via the
            # dedupe cache; they do not fail the run() data path.
            await self.assert_device_state(self.evaluate_schedule(self.get_minutes_now(), self.last_soc))
        return success
```

Update `apply_schedule` (full replacement) to assert immediately:

```python
    async def apply_schedule(self):
        """Commit the pending schedule atomically, persist it, and assert the device state immediately rather than waiting up to 60s for the next run cycle."""
        self.schedule = copy.deepcopy(self.pending_schedule)
        await self.save_schedule()
        self.publish_schedule_entities()
        if self.last_soc is not None and not self._is_read_only():
            await self.assert_device_state(self.evaluate_schedule(self.get_minutes_now(), self.last_soc))
```

Update the module docstring's "Control path" paragraph (replace the two `Control path:`/`Command dedupe:` paragraphs) with:

```python
Control path: exposes fox-style virtual schedule entities (charge/discharge window
time selects, SoC numbers, enable switches, a reserve number and an atomic write
button) that inverter.py programs directly via the TESLA inverter type. Because the
Powerwall has no native scheduler, run() acts as one: each cycle it evaluates the
committed windows against the wall clock and live SOC and asserts the resulting
device tuple (tariff / export rule / grid charging / backup reserve / operation
mode) through deduped write-on-change commands, so unchanged cycles cost no
Teslemetry command credits and failed writes self-retry. All emulator writes are
gated on Predbat's set_read_only configuration.
```

- [ ] **Step 4: Run tests and fix pre-existing run() test fallout**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t7.log 2>&1; grep -E "AttributeError|AssertionError|tests passed" /tmp/teslemetry_t7.log
```

Pre-existing `run()` tests (`test_teslemetry_run_first_success_returns_true`, the reconcile-latch tests) may now see extra POSTs from the emulator or need the new mock attributes. Fix them by either registering `command_ok_responses(api)` or leaving `last_soc` as None depending on what each test verifies — do NOT weaken their original assertions. Re-run until: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): scheduler emulator asserts window-derived device state each cycle

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: `automatic_config()`

**Files:**

- Modify: `apps/predbat/teslemetry.py`
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**

- Consumes: `self.set_arg` (ComponentBase → `base.set_arg`), entity helpers, sensors from Task 3, schedule entities from Task 5, `self.automatic` / `self.automatic_done` (Task 4)
- Produces: Predbat args wired to component entities; called from `run()` once `site_info_done` is True.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_teslemetry.py`:

```python
def test_teslemetry_automatic_config_sets_args():
    """automatic_config wires every inverter arg to this component's published entities."""
    api = MockTeslemetryAPI()
    run_async(api.automatic_config())
    assert api.args_set["inverter_type"] == ["TESLA"]
    assert api.args_set["num_inverters"] == 1
    assert api.args_set["inverter_reserve_max"] == 80
    assert api.args_set["soc_percent"] == ["sensor.predbat_teslemetry_soc"]
    assert api.args_set["soc_max"] == ["sensor.predbat_teslemetry_soc_max"]
    assert api.args_set["battery_power"] == ["sensor.predbat_teslemetry_battery_power"]
    assert api.args_set["battery_power_invert"] == [False]
    assert api.args_set["grid_power"] == ["sensor.predbat_teslemetry_grid_power"]
    assert api.args_set["grid_power_invert"] == [True]
    assert api.args_set["load_power"] == ["sensor.predbat_teslemetry_load_power"]
    assert api.args_set["pv_power"] == ["sensor.predbat_teslemetry_solar_power"]
    assert api.args_set["load_today"] == ["sensor.predbat_teslemetry_load_today"]
    assert api.args_set["import_today"] == ["sensor.predbat_teslemetry_import_today"]
    assert api.args_set["export_today"] == ["sensor.predbat_teslemetry_export_today"]
    assert api.args_set["pv_today"] == ["sensor.predbat_teslemetry_solar_today"]
    assert api.args_set["battery_rate_max"] == ["sensor.predbat_teslemetry_battery_rate_max"]
    assert api.args_set["inverter_limit"] == ["sensor.predbat_teslemetry_inverter_limit"]
    assert api.args_set["reserve"] == ["number.predbat_teslemetry_schedule_reserve"]
    assert api.args_set["charge_start_time"] == ["select.predbat_teslemetry_schedule_charge_start_time"]
    assert api.args_set["charge_end_time"] == ["select.predbat_teslemetry_schedule_charge_end_time"]
    assert api.args_set["charge_limit"] == ["number.predbat_teslemetry_schedule_charge_soc"]
    assert api.args_set["scheduled_charge_enable"] == ["switch.predbat_teslemetry_schedule_charge_enable"]
    assert api.args_set["discharge_start_time"] == ["select.predbat_teslemetry_schedule_discharge_start_time"]
    assert api.args_set["discharge_end_time"] == ["select.predbat_teslemetry_schedule_discharge_end_time"]
    assert api.args_set["discharge_target_soc"] == ["number.predbat_teslemetry_schedule_discharge_soc"]
    assert api.args_set["scheduled_discharge_enable"] == ["switch.predbat_teslemetry_schedule_discharge_enable"]
    assert api.args_set["schedule_write_button"] == ["switch.predbat_teslemetry_schedule_write"]


def test_teslemetry_automatic_config_references_published_entities():
    """Every entity automatic_config references is actually published by the component."""
    api = MockTeslemetryAPI()
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.register_control_entities()
    run_async(api.fetch_live_status())
    run_async(api.fetch_site_info())
    run_async(api.fetch_energy_today())
    run_async(api.automatic_config())
    published = set(api.dashboard_items.keys())
    for arg, value in api.args_set.items():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and "." in item:
                    assert item in published, "automatic_config references unpublished entity {} (arg {})".format(item, arg)


def test_teslemetry_run_triggers_automatic_config_once_after_site_info():
    """run() calls automatic_config exactly once, only when automatic is enabled and site_info succeeded."""
    api = MockTeslemetryAPI()
    api.register_control_entities()
    command_ok_responses(api)
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    api.automatic = True
    run_async(api.run(seconds=0, first=True))
    assert api.args_set.get("inverter_type") == ["TESLA"]
    api.args_set.clear()
    run_async(api.run(seconds=120, first=False))
    assert "inverter_type" not in api.args_set

    api_off = MockTeslemetryAPI()
    api_off.register_control_entities()
    command_ok_responses(api_off)
    api_off.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api_off.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO_FULL
    api_off.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api_off.mock_responses["/api/1/energy_sites/123456/tariff_rate"] = TARIFF_RATE_NORMAL
    run_async(api_off.run(seconds=0, first=True))
    assert api_off.args_set == {}
```

Register all three in the `test_teslemetry()` runner. Add to `MockTeslemetryAPI.__init__`:

```python
        self.args_set = {}
```

And add the capture method to `MockTeslemetryAPI`:

```python
    def set_arg(self, arg, value):
        """Capture set_arg calls for automatic_config assertions."""
        self.args_set[arg] = value
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t8.log 2>&1; grep -E "AttributeError|AssertionError|tests passed" /tmp/teslemetry_t8.log
```

Expected: FAIL with `AttributeError: ... 'automatic_config'`.

- [ ] **Step 3: Implement**

In `apps/predbat/teslemetry.py`, add after `assert_device_state`:

```python
    async def automatic_config(self):
        """Automatically wire Predbat's inverter args to this component's virtual entities (fox parity).

        With teslemetry_automatic enabled the user needs no manual inverter configuration in
        apps.yaml: the TESLA inverter type plus these args make inverter.py program the
        schedule entities directly and the emulator drive the device.
        """
        self.log("Info: Teslemetry automatic configuration - wiring Predbat to the TESLA inverter type")
        self.set_arg("inverter_type", ["TESLA"])
        self.set_arg("num_inverters", 1)
        self.set_arg("inverter_reserve_max", 80)
        self.set_arg("soc_percent", [self.entity("soc")])
        self.set_arg("soc_max", [self.entity("soc_max")])
        self.set_arg("battery_power", [self.entity("battery_power")])
        self.set_arg("battery_power_invert", [False])
        self.set_arg("grid_power", [self.entity("grid_power")])
        self.set_arg("grid_power_invert", [True])
        self.set_arg("load_power", [self.entity("load_power")])
        self.set_arg("pv_power", [self.entity("solar_power")])
        self.set_arg("load_today", [self.entity("load_today")])
        self.set_arg("import_today", [self.entity("import_today")])
        self.set_arg("export_today", [self.entity("export_today")])
        self.set_arg("pv_today", [self.entity("solar_today")])
        self.set_arg("battery_rate_max", [self.entity("battery_rate_max")])
        self.set_arg("inverter_limit", [self.entity("inverter_limit")])
        self.set_arg("reserve", [self.entity("schedule_reserve", domain="number")])
        self.set_arg("charge_start_time", [self.entity("schedule_charge_start_time", domain="select")])
        self.set_arg("charge_end_time", [self.entity("schedule_charge_end_time", domain="select")])
        self.set_arg("charge_limit", [self.entity("schedule_charge_soc", domain="number")])
        self.set_arg("scheduled_charge_enable", [self.entity("schedule_charge_enable", domain="switch")])
        self.set_arg("discharge_start_time", [self.entity("schedule_discharge_start_time", domain="select")])
        self.set_arg("discharge_end_time", [self.entity("schedule_discharge_end_time", domain="select")])
        self.set_arg("discharge_target_soc", [self.entity("schedule_discharge_soc", domain="number")])
        self.set_arg("scheduled_discharge_enable", [self.entity("schedule_discharge_enable", domain="switch")])
        self.set_arg("schedule_write_button", [self.entity("schedule_write", domain="switch")])
```

In `run()`, insert after the `schedule_loaded` latch block (before the live poll):

```python
        if self.automatic and not self.automatic_done and self.site_info_done:
            await self.automatic_config()
            self.automatic_done = True
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd coverage && ./run_all --test teslemetry > /tmp/teslemetry_t8.log 2>&1; grep -E "AssertionError|tests passed" /tmp/teslemetry_t8.log
```

Expected: `**** Teslemetry tests passed ****`

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): automatic_config wires Predbat to the TESLA inverter type

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Template rewrite + documentation

**Files:**

- Modify: `templates/tesla_powerwall.yaml`
- Modify: `docs/components.md` (add section after the Fox section which ends ~line 525)
- Modify: `docs/inverter-setup.md` (Tesla Powerwall section at line 2484)

**Interfaces:**

- Consumes: config keys `teslemetry_key`, `teslemetry_site_id`, `teslemetry_base_url`, `teslemetry_automatic` (Tasks 2/8)

- [ ] **Step 1: Rewrite the template control section**

In `templates/tesla_powerwall.yaml`, DELETE lines 27–170 (everything from `# Misc` / `charge_control_immediate: False` through `inverter_limit_discharge:` inclusive — the inline `inverter:` dict, all `*_power`/`*_today`/`soc_*` entity mappings, all five `*_service` hook blocks, and the static limits). REPLACE with:

```yaml
  # --- Tesla Powerwall via the Teslemetry component ---
  # Get your API token from https://teslemetry.com and your energy site id
  # from the Teslemetry console. With teslemetry_automatic enabled Predbat
  # configures the inverter automatically - no further inverter settings needed.
  # For a direct Tesla Fleet API connection set teslemetry_base_url accordingly.
  teslemetry_key: 'xxxx'
  teslemetry_site_id: '123456'
  teslemetry_automatic: True
  #teslemetry_base_url: 'https://api.teslemetry.com'
```

Keep everything from `# Inverter clock skew in minutes` (line 172) onward unchanged.

- [ ] **Step 2: Add the components.md section**

In `docs/components.md`, add to the contents list (after the Fox entry at line 19):

```markdown
    - [Tesla Powerwall Teslemetry API (teslemetry)](#tesla-powerwall-teslemetry-api-teslemetry)
```

Add after the Fox section's closing `---` (~line 525):

```markdown
### Tesla Powerwall Teslemetry API (teslemetry)

**Can be restarted:** Yes

#### What it does (teslemetry)

Integrates a Tesla Powerwall via the [Teslemetry](https://teslemetry.com) REST API (which mirrors Tesla Fleet API paths, so a direct Fleet API connection works by changing the base URL). Publishes live power flows, SOC and daily energy sensors, and exposes fox-style charge/discharge window entities that Predbat programs directly. Because the Powerwall has no native scheduler, the component translates the programmed windows into operation mode, backup reserve, grid-charging and export-rule commands each cycle, including the export tariff-trick needed to force the Powerwall to export.

#### When to enable (teslemetry)

- You have a Tesla Powerwall (developed against Powerwall 3)
- You want Predbat to control charging and export directly via the Tesla cloud
- You have a Teslemetry subscription and API token (or Tesla Fleet API access)

#### Important notes (teslemetry)

- Export freeze is not supported by the Powerwall hardware and is disabled automatically
- The Powerwall has no charge/discharge rate control; rates are modelled from the nameplate power
- When enabled (and Predbat is not read-only) the component owns the device tariff, replacing the customer's configured tariff with one built from Predbat's rate data
- Commands are deduped write-on-change to conserve Teslemetry command credits

#### Configuration Options (teslemetry)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `key` | String | Yes | - | `teslemetry_key` | Your Teslemetry (or Fleet API) bearer token |
| `site_id` | String | Yes | - | `teslemetry_site_id` | Tesla energy site id to poll and control |
| `base_url` | String | No | `https://api.teslemetry.com` | `teslemetry_base_url` | REST base URL; set to the Fleet API endpoint for a direct connection |
| `automatic` | Boolean | No | false | `teslemetry_automatic` | Set to `true` to automatically configure Predbat to use the Powerwall (no manual apps.yaml inverter settings required) |

---
```

- [ ] **Step 3: Update inverter-setup.md**

In `docs/inverter-setup.md`, at the start of the `## Tesla Powerwall` section (line 2484), insert BEFORE the existing content:

```markdown
### Recommended: Teslemetry component

The recommended integration is Predbat's built-in Teslemetry component, which needs only three keys in `apps.yaml` and no Home Assistant Tesla integration:

```yaml
  teslemetry_key: 'your-teslemetry-token'
  teslemetry_site_id: 'your-energy-site-id'
  teslemetry_automatic: True
```

See [Tesla Powerwall Teslemetry API](components.md#tesla-powerwall-teslemetry-api-teslemetry) for details. The manual configuration below remains available if you prefer to drive the Powerwall through the Home Assistant Tesla Fleet/Teslemetry integrations.

### Manual configuration via Home Assistant integrations

```

(The existing prose then continues under the "Manual configuration" heading; demote nothing else.)

- [ ] **Step 4: Verify docs and spelling**

```bash
./run_pre_commit > /tmp/precommit_t9.log 2>&1; grep -E "Failed|error|cspell" /tmp/precommit_t9.log
```

If CSpell flags new words (e.g. `teslemetry` variants), add them to `.cspell/custom-dictionary-workspace.txt` and re-stage (the file auto-sorts on commit).

- [ ] **Step 5: Commit**

```bash
git add templates/tesla_powerwall.yaml docs/components.md docs/inverter-setup.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(teslemetry): component-based template and documentation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Full verification + PR update

**Files:**

- No new code; verification only

- [ ] **Step 1: Full quick test suite**

```bash
cd coverage && ./run_all --quick > /tmp/run_all_final.log 2>&1; grep -E "FAIL|Error|passed|failed" /tmp/run_all_final.log | tail -30
```

Expected: all groups pass except the known pre-existing `multi_car_iog_load_slots_regression` failure (verify it is the same failure as on main via `git stash` baseline if unsure).

- [ ] **Step 2: Pre-commit**

```bash
./run_pre_commit > /tmp/precommit_final.log 2>&1; tail -20 /tmp/precommit_final.log
```

Expected: all hooks pass (interrogate 100%, black, flake8, cspell).

- [ ] **Step 3: Push and update the PR description**

```bash
git push origin feat/tesla-powerwall-teslemetry
git pull   # pre-commit.ci may auto-fix and push
```

Update the PR #4177 body (`gh pr edit 4177 --body-file <file>`) to describe the window control plane: TESLA inverter type, schedule entities + write button, scheduler emulator, `teslemetry_automatic`, no service hooks, `support_discharge_freeze: False` rationale, and the idle `pv_only` divergence from the old template. Keep the existing "Known follow-ups" (tariff_rate shape validation on live PW3) and add: emulator behaviour validated against live hardware during the beta pilot.

---

## Self-Review (completed)

1. **Spec coverage:** TESLA INVERTER_DEF (T1), config plumbing (T2), sensors for auto-config (T3), window math + evaluation incl. no-export-freeze states (T4), fox-style entities with atomic write button (T5), persistence via Storage (T6), scheduler emulator with read-only gating + dedupe (T7), automatic_config (T8), template/docs (T9), verification (T10). Export-freeze decision is encoded in T1 (flag) and T4 (no export-freeze state exists in `evaluate_schedule`).
2. **Placeholder scan:** none — every step has concrete code/commands. T7 Step 4 names the specific pre-existing tests that may need mock updates and forbids weakening their assertions.
3. **Type consistency:** `evaluate_schedule` returns the exact 5-key dict consumed by `assert_device_state` and asserted in tests; entity suffixes (`schedule_charge_start_time` etc.) match between `publish_schedule_entities`, `schedule_event` routing, `automatic_config`, and all tests; storage key `("teslemetry", "schedule")` consistent between save/load/tests.

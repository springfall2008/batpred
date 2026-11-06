# GE Cloud API Budget Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A test harness that runs one simulated day (00:00–23:55) of a GE Cloud inverter through the real `execute.py` → `gecloud.py` path against a fake GE Cloud, and reports every API call with when and why it was made.

**Architecture:** A fake GE Cloud replaces `aiohttp.ClientSession` and is the only thing gecloud can reach. It records each request as a `CallRecord`. A harness builds a real `PredBat` and a real `GECloudDirect` (a subclass whose clock is simulated). It cold-starts gecloud against the fake and lets `ge_cloud_automatic` wire the inverter. It applies a random scenario at midnight and plans once. Then it steps 5 minutes at a time: gecloud's `run()` once per minute, `fetch_inverter_data()`, `calculate_plan(recompute=False)` and `execute_plan()`. Writes from execute reach gecloud's `select_event` / `number_event` / `switch_event` through a harness HA interface.

**Tech Stack:** Python 3, the repo's own test infrastructure (`create_predbat()`, `TestHAInterface`, `run_async`), `unittest.mock.patch`, `contextvars`.

**Spec:** `docs/superpowers/specs/2026-11-06-gecloud-api-budget-harness-design.md`

## Global Constraints

- Every function and class has a docstring (interrogate 100%).
- British spelling (cspell `en-gb`). Add unknown valid words to `.cspell/custom-dictionary-workspace.txt`.
- Black line length 256, Flake8 250. Names are `lower_case_with_underscores`.
- File header: copy the 5-line copyright block and the `# pylint: disable=...` lines from `apps/predbat/tests/fleet_rig.py`.
- **No production code changes.** `gecloud.py`, `execute.py`, `inverter.py` and `plan.py` stay as they are. Everything lives under `apps/predbat/tests/`.
- Count at the HTTP boundary (the faked `aiohttp.ClientSession`), never at `async_get_inverter_data`.
- An unmatched request raises `AssertionError`. Nothing may reach `api.givenergy.cloud`.
- No real wall-clock waiting. Patch `asyncio.sleep` (gecloud's retry back-offs and its 0.2 s sweep pacing) and `Inverter.sleep` (`write_and_poll_sleep`, 10 s for GEC) for the whole simulation.
- **`coverage/ge_cloud.log` contains a live API key and a customer's personal details.** Copy only register names and types out of it. Never copy serials, the key, or account data. Use the anonymised identities in Task 1.
- Do not run `git stash` in this worktree (the stash stack is shared). Use WIP commits.
- Tests are run from `coverage/` with output saved to a file and then grepped, e.g. `./run_all --test gecloud_budget_harness > /tmp/claude-501/gb.txt 2>&1; grep -n "ERROR\|FAIL\|passed\|failed" /tmp/claude-501/gb.txt`.

## Review Focus

1. **A write that never reaches gecloud.** If `call_service` routing misses one of execute's service shapes (for example `select/select_option` with `option=`), execute's write-and-poll sees no change and quietly retries. That gives a plausible but wrong count. Pinned in Task 2 by `test_execute_write_reaches_fake`.
2. **The day wrap firing anyway.** If `minutes_now` or `plan_last_updated_minutes` drift, `calculate_plan` logs "Force recompute due to start of day" and re-optimises mid-run. Pinned in Task 3 by `test_short_day_never_recomputes`.
3. **Template inverter args leaking.** `cases/predbat_debug_agile1.yaml` is a GivTCP (`GE`) dump. Any inverter entity arg that automatic config doesn't overwrite would point execute at a `dummy_items` entity, and its writes would never reach the fake. Pinned in Task 2 by `test_inverter_args_point_at_gecloud`.
4. **Calls from the wrong place.** `fetch_inverter_data()` must cost no API calls: it reads the entities gecloud publishes. If it does make calls, the harness is miscounting or mis-wiring. Pinned in Task 3 by `test_fetch_makes_no_calls`.
5. **Device skipped as stale.** `async_get_devices` skips a device whose `last_updated` is more than 5 days before the *real* `datetime.now()`, not the simulated one, so the fake must stamp real "now". Pinned in Task 2 by `test_startup_finds_device`.

---

## File Structure

| File | Responsibility |
| ---- | -------------- |
| `apps/predbat/tests/gecloud_fake_cloud.py` | Captured register fixture, anonymised identities, `CallRecord`, `FakeGECloud` (routing, state, recording, categorising) and its fake `aiohttp` session |
| `apps/predbat/tests/gecloud_budget_harness.py` | `HarnessGECloud`, `HarnessHAInterface`, `GECloudBudgetHarness` (start-up, midnight plan, 5-minute step, day loop), `DayResult`, `summarise()`, `write_call_log()` |
| `apps/predbat/tests/test_gecloud_budget.py` | Unit tests for both modules (quick) and the full-day baseline run (slow) |
| `apps/predbat/unit_test.py` | Two `TEST_REGISTRY` entries |
| `.gitignore` | Ignore `coverage/gecloud_api_budget_log.jsonl` |
| `docs/superpowers/specs/2026-11-06-gecloud-api-budget-harness-design.md` | "Baseline results" section appended in Task 4 |

---

### Task 1: Fake GE Cloud and call record

**Files:**
- Create: `apps/predbat/tests/gecloud_fake_cloud.py`
- Create: `apps/predbat/tests/test_gecloud_budget.py`
- Modify: `apps/predbat/unit_test.py` (import + `TEST_REGISTRY` entry)

**Interfaces:**
- Produces:
  - Constants: `HARNESS_SERIAL = "SA0000H001"` (as the cloud returns it; gecloud lowercases it), `HARNESS_DONGLE_SERIAL = "WA0000H001"`, `HARNESS_SITE_ID = 10001`, `HARNESS_ACCOUNT_ID = 1`.
  - `CAPTURED_REGISTERS: list[tuple[str, str]]`. These are `(friendly_name, kind)` for the 81 registers in `coverage/ge_cloud.log`, with names exactly as the log's `friendly_name` (e.g. `"AC Charge 1 Start Time"`). `kind` is one of `"time"`, `"number"`, `"switch"`, `"option"`, `"writeonly"`. Restart Hardware is `"writeonly"`. Export Power Priority and Pause Battery are `"option"`.
  - `build_register_list(registers: list[tuple[str, str]], rate_w: int) -> list[dict]`. Each entry is `{"id": int, "name": str, "validation_rules": list[str], "validation": str}`, with ids 1..N in order. Rules by kind: time → `["date_format:H:i"]`; number → `["between:0,100"]`, except Battery Charge Power / Battery Discharge Power → `["between:0,<rate_w>"]`; switch → `["boolean"]`; option → `["in:0,1,2,3"]`; writeonly → `["writeonly"]`.
  - `CALL_CONTEXT: contextvars.ContextVar[str]` (default `"unlabelled"`). The harness sets it to say why calls are being made. Because it is a ContextVar, tasks gecloud creates with `loop.create_task` inherit it.
  - `@dataclass CallRecord`: `sim_time: datetime`, `scenario_id: int`, `method: str`, `endpoint: str` (the `GE_API_*` template), `path: str`, `serial: str`, `setting_id: str`, `setting_name: str`, `category: str`, `context: str`, `caller: str`, `status: int`, `retry: bool`. Plus `to_dict() -> dict` with `sim_time` as ISO text.
  - `class FakeGECloud`:
    - `__init__(self, battery_kwh: float, rate_w: int, clock: Callable[[], datetime], registers=CAPTURED_REGISTERS, model: str = "GIV-HY5.0")`
    - attributes: `soc_percent: float`, `meter_today: dict`, `meter_total: dict`, `register_values: dict[int, object]`, `calls: list[CallRecord]`, `scenario_id: int`
    - `handle(self, method: str, url: str, body: dict | None) -> tuple[int, dict]`: route, record, respond
    - `session_factory(self) -> Callable[..., object]`: a drop-in for `aiohttp.ClientSession(timeout=...)` (async context manager with `.get(url, headers=)` and `.post(url, headers=, json=)` returning an async context manager response with `.status` and `async .json()`)
    - `register_count(self) -> int`: readable (non-writeonly) registers

- [ ] **Step 1: Write the failing tests** in `apps/predbat/tests/test_gecloud_budget.py`, under `run_gecloud_budget_harness_tests(my_predbat) -> bool` (returns True on failure, the repo's convention). Each sub-test is a plain function called from it. Drive requests through `fake.session_factory()` with `run_async` from `tests.test_infra`, exactly as gecloud would:
  - `test_register_fixture`: `len(CAPTURED_REGISTERS) == 81`; `regname_to_ha(name)` for every name gives the snake-case suffixes in the log (spot-check `ac_charge_1_start_time`, `battery_reserve_percent_limit`, `enable_eco_mode`); `register_count() == 80`.
  - `test_routes_status_and_meter`: GET `.../inverter/SA0000H001/system-data/latest` → 200, `["data"]["battery"]["percent"] == fake.soc_percent`; GET `.../meter-data/latest` → 200 with `today` and `total` dicts; records have categories `"status"` and `"meter"`.
  - `test_read_returns_written_value`: POST `.../settings/5/write` with `{"value": "55", "context": "predbat"}`, then POST `.../settings/5/read` → `["data"]["value"] == "55"`; categories `"register_write"` then `"register_read"`; `setting_name` is register 5's name.
  - `test_device_list_shape`: GET `.../communication-device` returns one device whose `inverter.serial == HARNESS_SERIAL`, `site_id == HARNESS_SITE_ID`, one battery in `connections.batteries`, and `last_updated` within 1 minute of real UTC now.
  - `test_unmatched_request_raises`: GET `.../nonsense` raises `AssertionError`; GET to any host other than `api.givenergy.cloud` raises `AssertionError`.
  - `test_context_and_caller_recorded`: with `CALL_CONTEXT.set("poll seconds=120")`, a request's record has `context == "poll seconds=120"`. A request made from inside a coroutine named `async_get_device_info` in a module whose file is `gecloud.py` is not needed here; `caller` only has to be a string (Task 2 asserts real callers).
  - `test_device_info_vs_device_list_category`: the same path `communication-device` is categorised `"device_info"` when `caller` contains `async_get_device_info`, and `"device_list"` otherwise.

- [ ] **Step 2: Register the quick test and run it to see it fail**

Add to `TEST_REGISTRY` in `apps/predbat/unit_test.py`, next to the `random` entries:
`("gecloud_budget_harness", run_gecloud_budget_harness_tests, "GE Cloud API budget harness unit tests", False),`
and the import `from tests.test_gecloud_budget import run_gecloud_budget_harness_tests`.

Run: `cd coverage && ./run_all --test gecloud_budget_harness > /tmp/claude-501/gb.txt 2>&1; tail -30 /tmp/claude-501/gb.txt`
Expected: FAIL, with an import error for `tests.gecloud_fake_cloud`.

- [ ] **Step 3: Implement `apps/predbat/tests/gecloud_fake_cloud.py`**

Routing: strip `GE_API_URL` (import it from `gecloud`). Match the remainder against regexes built from the `GE_API_*` templates in `gecloud.py` (`{name}` → `[^/?]+`, query string ignored). Response bodies follow the shapes in the docstrings of `publish_status`, `publish_meter`, `publish_info` and `async_get_devices` in `gecloud.py`, wrapped as `{"data": ...}`. Device info: `nominal_voltage` 51.2, `nominal_capacity = battery_kwh * 1000 / 51.2`, `depth_of_discharge` 1, `max_charge_rate`/`max_discharge_rate = rate_w`, `model`, and battery `capacity.full == capacity.design`. Account: an `id`, `standard_timezone: "Europe/London"`, placeholder name fields. Site: `{"id": HARNESS_SITE_ID}` with no `limits`. EVC and smart-device lists: empty `[]`. Writes store `body["value"]` and answer `{"data": {"value": <stored>, "success": True}}`.

`caller`: walk `inspect.stack()`, keep frames whose filename ends in `gecloud.py`, drop `async_get_inverter_data` and `async_get_inverter_data_retry`, and join the function names outermost first with `" > "`.

`retry`: True when the previous record with the same `method` + `path` and the same `context` got a non-2xx status or a negative `value`. (The ideal fake never fails, so this stays False, but the column must be real.)

Categories: `account`, `site`, `device_list`, `device_info`, `status`, `meter`, `register_list` (GET settings), `register_read`, `register_write`, `evc`, `smart_device`.

- [ ] **Step 4: Run the tests and see them pass**

Run: `cd coverage && ./run_all --test gecloud_budget_harness > /tmp/claude-501/gb.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/claude-501/gb.txt`
Expected: no `ERROR`/`Traceback`, test reported passed.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/tests/gecloud_fake_cloud.py apps/predbat/tests/test_gecloud_budget.py apps/predbat/unit_test.py
git commit -m "test(gecloud): fake GE Cloud with per-call recording for the API budget harness"
```

---

### Task 2: Harness start-up and the execute → gecloud write route

**Files:**
- Create: `apps/predbat/tests/gecloud_budget_harness.py`
- Modify: `apps/predbat/tests/test_gecloud_budget.py`

**Interfaces:**
- Consumes: everything Task 1 produces; `create_predbat` (import it inside functions from `unit_test`, as `run_random_scenario_tests` does, to avoid a circular import); `load_scenarios`, `apply_scenario_to_predbat`, `RANDOM_SCENARIOS`, `RANDOM_TEMPLATE`, `reset_inverter` from `tests.test_random_scenarios` / `tests.test_infra`.
- Produces:
  - `class HarnessGECloud(GECloudDirect)`: attribute `sim_now: datetime`; property `now_utc_exact` returns `self.sim_now`.
  - `class HarnessHAInterface(TestHAInterface)`: attribute `gecloud: HarnessGECloud | None`. `call_service(self, service, **kwargs)`: when `entity_id` starts with `"<domain>.predbat_gecloud_"`, route `switch/turn_on|turn_off|toggle` → `gecloud.switch_event(entity_id, "turn_on"|...)`, `number/set_value` → `gecloud.number_event(entity_id, value)`, `select/select_option` → `gecloud.select_event(entity_id, option)`. Each runs through `run_async`, with `CALL_CONTEXT` set to `"execute <entity_id> <old>-><new>"` (old = current `dummy_items` state), and returns True. Any other call goes to `super().call_service`.
  - `class GECloudBudgetHarness`:
    - `__init__(self, scenario: dict, template_path: str = RANDOM_TEMPLATE)`
    - attributes: `my_predbat`, `gecloud: HarnessGECloud`, `fake: FakeGECloud`, `scenario`, `midnight: datetime`, `startup_calls: list[CallRecord]`
    - `startup(self) -> None`: build and cold-start, then plan at midnight (steps below)
    - `sim_time(self, minute: int) -> datetime`: `self.midnight + timedelta(minutes=minute)`
  - `reset_config_to_defaults(my_predbat) -> None`: for every `CONFIG_ITEMS` entry with a `"default"`, `my_predbat.expose_config(name, default)`, then `my_predbat.fetch_config_options()`.

`startup()` does this, in this order:
1. `my_predbat = create_predbat()`. Replace its `ha_interface` with a `HarnessHAInterface` that keeps the original's `dummy_items`/`history`, and set `ha_interface.base = my_predbat`. Then `reset_inverter(my_predbat)`, `my_predbat.read_debug_yaml(template_path)`, and `my_predbat.config_root = my_predbat.save_restore_dir = "./"`.
2. `reset_config_to_defaults(my_predbat)`, so the simulated user has default settings, not the template user's (spec: "the defaults a GE Cloud user gets").
3. Delete from `my_predbat.args` every inverter-entity arg that `async_automatic_config` in `gecloud.py` sets with `set_arg`, plus `pause_mode`, `inverter_time`, `scheduled_charge_enable`, `scheduled_discharge_enable` and `inverter_mode`. This stops the GivTCP template's entities surviving. Keep the list as a module constant `TEMPLATE_INVERTER_ARGS`.
4. `self.midnight = my_predbat.midnight_utc`. Create `FakeGECloud(battery_kwh=scenario.params.battery.soc_max_kwh, rate_w=int(rate_max_charge_kw * 1000), clock=lambda: self.gecloud.sim_now)`.
5. `HarnessGECloud(my_predbat, ge_cloud_direct=True, api_key="harness-key", automatic=True)`, `sim_now = self.midnight`. Under `patch("aiohttp.ClientSession", fake.session_factory())` and `CALL_CONTEXT = "startup"`, run `run_async(gecloud.run(0, True))`. Startup calls are the records made so far; copy them into `startup_calls` and clear `fake.calls`.
6. `my_predbat.inverters = []`, `my_predbat.fetch_inverter_data()` (builds real `Inverter`s from the args automatic config set).
7. Deep-copy the scenario, set `params["clock"] = {"minutes_now": 0}`, and `apply_scenario_to_predbat(my_predbat, scenario_copy)`. Set `my_predbat.args["threads"] = 0`, then `calculate_plan(recompute=True)` and `execute_plan()` under `CALL_CONTEXT = "execute 00:00"`. Records from this first execute belong to the day (minute 0), not to start-up.

`patch("asyncio.sleep", <async no-op>)` and `patch.object(Inverter, "sleep", lambda self, seconds: None)` are active throughout `startup()` and Task 3's day loop. Expose a context manager `GECloudBudgetHarness.patched(self)` that enters all three patches.

- [ ] **Step 1: Write the failing tests** (add to `run_gecloud_budget_harness_tests`), using scenario id 0 from `load_scenarios(RANDOM_SCENARIOS)`:
  - `test_startup_finds_device`: after `startup()`, `harness.gecloud.device_list == ["sa0000h001"]`, `harness.gecloud.polling_mode is True`, and `startup_calls` contains at least one record of each category `account`, `device_list`, `register_list`, `register_read`.
  - `test_inverter_args_point_at_gecloud`: for every arg in `TEMPLATE_INVERTER_ARGS` that is present in `my_predbat.args`, every entity id in it contains `"predbat_gecloud_sa0000h001"`; `my_predbat.inverters[0].inverter_type == "GEC"`.
  - `test_inverter_reads_fake_soc`: set `fake.soc_percent = 37`, run `gecloud.run(120, False)` under the patches, then `my_predbat.fetch_inverter_data()`; `round(my_predbat.soc_kw / my_predbat.soc_max * 100) == 37`.
  - `test_execute_write_reaches_fake`: clear `fake.calls`. Call `my_predbat.inverters[0].adjust_charge_window(<start>, <end>, my_predbat.minutes_now)` with a start/end different from the current registers. There is ≥1 `register_write` record whose `setting_name` is `"AC Charge 1 Start Time"` and whose `context` starts with `"execute select.predbat_gecloud_sa0000h001_ac_charge_1_start_time"`, and `caller` contains `select_event`. There are no `register_read` records (read-back costs no calls).
  - `test_config_defaults`: `my_predbat.get_arg("set_charge_low_power")` equals its `CONFIG_ITEMS` default (False).

- [ ] **Step 2: Run them and see them fail**

Run: `cd coverage && ./run_all --test gecloud_budget_harness > /tmp/claude-501/gb.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/claude-501/gb.txt`
Expected: FAIL, with an import error for `tests.gecloud_budget_harness`.

- [ ] **Step 3: Implement `apps/predbat/tests/gecloud_budget_harness.py`** with the start-up sequence above.

- [ ] **Step 4: Run them and see them pass** (same command; no `ERROR`/`Traceback`). If `test_execute_write_reaches_fake` shows writes going to `dummy_items` instead of the fake, the service shape execute uses is not routed. Fix the routing rather than the test.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/tests/gecloud_budget_harness.py apps/predbat/tests/test_gecloud_budget.py
git commit -m "test(gecloud): API budget harness start-up and execute-to-gecloud write route"
```

---

### Task 3: The 5-minute step, the day loop, the summary and the log

**Files:**
- Modify: `apps/predbat/tests/gecloud_budget_harness.py`
- Modify: `apps/predbat/tests/test_gecloud_budget.py`

**Interfaces:**
- Consumes: Task 2's `GECloudBudgetHarness`.
- Produces:
  - `GECloudBudgetHarness.step(self, minute: int) -> None`, for `minute` in 5..1435 (multiples of 5).
  - `GECloudBudgetHarness.simulate_day(self, last_minute: int = 1435) -> DayResult`: `startup()`, then `step(m)` for `m` in `range(5, last_minute + 1, 5)`.
  - `@dataclass DayResult`: `scenario_id: int`, `startup_calls: list[CallRecord]`, `day_calls: list[CallRecord]`, `register_count: int`, `steps: int` (executes run, including minute 0), `recomputes: int`.
  - `summarise(results: list[DayResult]) -> str`
  - `write_call_log(results: list[DayResult], path: str) -> None`: JSON lines, one `CallRecord.to_dict()` per line, with startup records' `context` kept as `"startup"`.
  - Constants: `MONTH_DAYS = 30`, `BUDGETS = (10000, 20000)`.

`step(minute)`:
1. **Ideal SOC:** `fake.soc_percent` = the previous plan's `my_predbat.predict_soc_best[5] / my_predbat.soc_max * 100` (kWh → %). Read it at the end of the previous step, store it on the harness, and apply it here. Keep this in one method, `next_soc_percent(self) -> float`, so the follow-on SOC-drift mode is a one-method override.
2. **Meter:** add the scenario's daily load kWh / 288 to `meter_today`/`meter_total` consumption and import, and daily PV kWh / 288 to solar. They only need to move.
3. For each `m` in `minute-4 .. minute`: `gecloud.sim_now = sim_time(m)`, `CALL_CONTEXT = "poll seconds=<60*m>"`, `run_async(gecloud.run(60 * m, False))`.
4. `my_predbat.update_time(now=sim_time(minute))`, then `CALL_CONTEXT = "fetch"` and `my_predbat.fetch_inverter_data()`.
5. `CALL_CONTEXT = "plan advance"`, then `my_predbat.calculate_plan(recompute=False)`. This drops expired charge/export windows, as a real `update_pred` cycle does when it does not re-plan.
6. `CALL_CONTEXT = "execute <HH:MM>"`, then `my_predbat.execute_plan()`. `HarnessHAInterface` replaces the context with the entity-level reason for each routed write. Writes not routed through it (for example gecloud's own AC charge gate inside `run()`) keep the poll context and their `caller`.

`recomputes`: 1 + the number of times `my_predbat.plan_last_updated_minutes` changes after start-up. It must stay 1.

`summarise()` output, per scenario then overall:
- Start-up calls (a cost paid on each restart) and the register count the run used.
- Steady-state day total, and the count per category.
- Month projection = day total × `MONTH_DAYS`, shown against each of `BUDGETS` as a percentage.
- Writes per day, broken down by `setting_name`.
- Worst-case scenario.
- The fixed lines `"SOC follows the plan exactly (ideal mode): write counts are a floor."` and `"GECloudData history download: not simulated (not used)."`

- [ ] **Step 1: Write the failing tests:**
  - `test_summary_maths`: build two `DayResult`s by hand with known records (e.g. 100 status + 20 register_write, and 300 status). `summarise` contains `"3600"` (120 × 30) for the first, the worst case names the second scenario, and `"36.0%"` (3600 / 10000) appears.
  - `test_short_day_cadence` (scenario 0, `simulate_day(last_minute=60)`): `steps == 13`; `day_calls` has 30 `status` records (gecloud polls when `seconds % 120 == 0`, so even minutes 2..60) and the same for `meter`; `register_read` count == `register_count * 6` (sweeps at minutes 10..60, from `polling_mode`).
  - `test_short_day_never_recomputes`: the same run has `recomputes == 1`, and no log line containing `"Force recompute due to start of day"` (capture `my_predbat.log`).
  - `test_fetch_makes_no_calls`: in the same run, no `day_calls` record has `context == "fetch"` or `context == "plan advance"`.
  - `test_call_log_round_trip`: `write_call_log` into a temp file. Line count == startup + day records, and every line parses as JSON with the `CallRecord` keys.

- [ ] **Step 2: Run them and see them fail**

Run: `cd coverage && ./run_all --test gecloud_budget_harness > /tmp/claude-501/gb.txt 2>&1; grep -n "ERROR\|Traceback\|passed\|failed" /tmp/claude-501/gb.txt`
Expected: FAIL (`step` / `summarise` not defined).

- [ ] **Step 3: Implement `step`, `simulate_day`, `DayResult`, `summarise` and `write_call_log`.**

- [ ] **Step 4: Run them and see them pass** (same command). If the cadence counts differ, check them against `GECloudDirect.run` before changing the test. The test pins gecloud's real cadence on main.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/tests/gecloud_budget_harness.py apps/predbat/tests/test_gecloud_budget.py
git commit -m "test(gecloud): simulate a day of 5-minute cycles and summarise API calls"
```

---

### Task 4: Full-day baseline run and results

**Files:**
- Modify: `apps/predbat/tests/test_gecloud_budget.py`
- Modify: `apps/predbat/unit_test.py`
- Modify: `.gitignore`
- Modify: `docs/superpowers/specs/2026-11-06-gecloud-api-budget-harness-design.md`

**Interfaces:**
- Produces: `run_gecloud_api_budget(my_predbat) -> bool`, `DEFAULT_SCENARIO_IDS: tuple[int, ...]`, `BUDGET_LOG_PATH = "gecloud_api_budget_log.jsonl"` (relative to `coverage/`).

- [ ] **Step 1: Choose `DEFAULT_SCENARIO_IDS`**: five ids from `coverage/cases/random_scenarios.yaml`. Include at least one with cars (`params.cars.num_cars > 0`), one with an IOG run, and one whose plan exports. Record next to the constant which id covers which case.

- [ ] **Step 2: Implement `run_gecloud_api_budget(my_predbat)`**: `simulate_day()` for each id (a fresh harness each time), `print(summarise(results))`, `write_call_log(results, BUDGET_LOG_PATH)`. It returns True only if a simulation raised. There is deliberately no budget assertion (spec: current main would fail any sensible one). Register it:
`("gecloud_api_budget", run_gecloud_api_budget, "GE Cloud API calls over a simulated day (baseline measurement)", True),`
Add `coverage/gecloud_api_budget_log.jsonl` to `.gitignore`.

- [ ] **Step 3: Run the baseline**

Run: `cd coverage && ./run_all --test gecloud_api_budget > /tmp/claude-501/gab.txt 2>&1; grep -n "Traceback\|ERROR\|Month projection\|Start-up\|Worst" /tmp/claude-501/gab.txt`
Expected: no Traceback, five scenario summaries, and `coverage/gecloud_api_budget_log.jsonl` written.

- [ ] **Step 4: Check the quick suite is not slowed or broken**

Run: `cd coverage && ./run_all --quick > /tmp/claude-501/quick.txt 2>&1; tail -20 /tmp/claude-501/quick.txt`
Expected: all pass, and `gecloud_api_budget` skipped as slow.

- [ ] **Step 5: Append a "Baseline results (main @ <short sha>)" section to the spec.** It holds the summary table (start-up, steady-state per day by category, month projection vs 10k/20k, writes per day by setting) and the register count used.

- [ ] **Step 6: Pre-commit, then commit**

Run: `cd <worktree root> && git add -A apps/predbat/tests .gitignore docs apps/predbat/unit_test.py && ./run_pre_commit > /tmp/claude-501/pc.txt 2>&1; grep -n "Failed\|Passed\|Skipped" /tmp/claude-501/pc.txt`
Expected: no `Failed`. Re-stage `.cspell/custom-dictionary-workspace.txt` if it was re-sorted.

```bash
git commit -m "test(gecloud): full-day GE Cloud API budget baseline"
```

# Open-Meteo Primary Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single boolean, `forecast_solar_open_meteo_first`, that makes Predbat fetch solar forecasts from Open-Meteo first and fall back to Forecast.solar, reusing the existing `forecast_solar` configuration unchanged.

**Architecture:** A new branch is added ahead of the existing `self.forecast_solar` branch in `SolarAPI.fetch_pv_forecast()`. It mirrors the existing Open-Meteo backup path in reverse: Open-Meteo is called first, and Forecast.solar is called only when Open-Meteo returns no data. The `forecast_solar` config entries are passed straight to `download_open_meteo_data()` because both download paths read the same keys and apply `azimuth_zero_south` identically.

**Tech Stack:** Python 3, asyncio, aiohttp. Tests use the project's own `TestSolarAPI` harness in `apps/predbat/tests/test_solcast.py` with mocked HTTP; no pytest.

## Global Constraints

- Line length: 256 chars (Black), 250 chars (Flake8).
- Docstrings required on every function and class (`interrogate`, 100% coverage). The new helper method must have one.
- Spell checking is British English via CSpell. `docs/superpowers/` is excluded, but `docs/apps-yaml.md` and `docs/install.md` are **not** — new words there must be added to `.cspell/custom-dictionary-workspace.txt`.
- Variable naming: `lower_case_with_underscores`.
- Tests are run from the `coverage/` directory. Always redirect test output to a file and grep the file afterwards; never pipe directly to grep.
- The existing `forecast_solar_open_meteo_backup` setting must not change in type or behaviour.
- Do not modify the caps in `pv_calibration()` or the `kwp * efficiency` value sent to Forecast.solar. Both are explicitly out of scope in the spec.

## Reference: how to run the tests

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/solcast_out.txt 2>&1
grep -E "ERROR|FAIL|Passed|failed" /tmp/solcast_out.txt
```

A passing run prints no `ERROR:` lines from the solcast tests. Each test function returns a `failed` boolean; `run_solcast_tests` ORs them together.

---

### Task 1: Add the setting and reverse the source order

**Files:**
- Modify: `apps/predbat/config.py:2367` (add schema entry)
- Modify: `apps/predbat/components.py:109` (add component arg)
- Modify: `apps/predbat/solcast.py:47-76` (add `initialize()` parameter and assignment)
- Modify: `apps/predbat/solcast.py:1218-1226` (reverse the source order)
- Test: `apps/predbat/tests/test_solcast.py` (harness update at line 171, plus four new tests)

**Interfaces:**
- Consumes: `SolarAPI.download_open_meteo_data(configs=...)` returning `(sorted_data, max_kwh)`; `SolarAPI.download_forecast_solar_data()` returning `(sorted_data, max_kwh)`.
- Produces: `self.forecast_solar_open_meteo_first` (bool, default `False`) on the `SolarAPI` instance. Task 2 reads nothing from this task beyond the branch structure it edits.

- [ ] **Step 1: Write the four failing tests**

Add these four functions to `apps/predbat/tests/test_solcast.py`, immediately after `test_fetch_pv_forecast_forecast_solar_open_meteo_backup_not_used_on_success` (which ends around line 1822, just before `def test_fetch_pv_forecast_ha_sensors`).

```python
def test_fetch_pv_forecast_open_meteo_first_used_when_set(my_predbat):
    """
    When forecast_solar_open_meteo_first is True, Open-Meteo is used as the primary
    source and forecast.solar is not called at all.
    """
    print("  - test_fetch_pv_forecast_open_meteo_first_used_when_set")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.forecast_solar_open_meteo_first = True
        test_api.solar.open_meteo_forecast_max_age = 1.0
        # Both sources would succeed - Open-Meteo must win and forecast.solar must not be called
        test_api.set_mock_response(
            "api.open-meteo.com",
            {
                "hourly": {
                    "time": ["2025-06-15T12:00", "2025-06-15T13:00", "2025-06-15T14:00"],
                    "global_tilted_irradiance": [500.0, 600.0, 550.0],
                    "temperature_2m": [25.0, 25.0, 25.0],
                    "wind_speed_10m": [1.0, 1.0, 1.0],
                }
            },
        )
        test_api.set_mock_response(
            "ensemble-api.open-meteo.com",
            {
                "hourly": {
                    "time": ["2025-06-15T12:00", "2025-06-15T13:00", "2025-06-15T14:00"],
                    "global_tilted_irradiance_member01": [400.0, 480.0, 440.0],
                }
            },
        )
        test_api.set_mock_response(
            "forecast.solar",
            {
                "result": {"watts": {"2025-06-15T12:00:00+0000": 500, "2025-06-15T12:30:00+0000": 600}},
                "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
            },
            200,
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        open_meteo_calls = [r for r in test_api.request_log if "open-meteo.com" in r["url"]]
        if len(open_meteo_calls) == 0:
            print("ERROR: Expected Open-Meteo API call when open_meteo_first is set, got none")
            failed = True

        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) != 0:
            print(f"ERROR: Expected no forecast.solar calls when Open-Meteo succeeds, got {len(forecast_calls)}")
            failed = True

        if f"sensor.{test_api.mock_base.prefix}_pv_today" not in test_api.dashboard_items:
            print("ERROR: Expected pv_today sensor to be published from Open-Meteo primary")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_open_meteo_first_falls_back_on_failure(my_predbat):
    """
    When forecast_solar_open_meteo_first is True and Open-Meteo returns no data,
    fetch_pv_forecast falls back to forecast.solar.
    """
    print("  - test_fetch_pv_forecast_open_meteo_first_falls_back_on_failure")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.forecast_solar_open_meteo_first = True
        test_api.solar.open_meteo_forecast_max_age = 1.0
        # Open-Meteo fails
        test_api.set_mock_response("api.open-meteo.com", {"error": "server error"}, 500)
        test_api.set_mock_response("ensemble-api.open-meteo.com", {"error": "server error"}, 500)
        # forecast.solar succeeds
        test_api.set_mock_response(
            "forecast.solar",
            {
                "result": {"watts": {"2025-06-15T12:00:00+0000": 500, "2025-06-15T12:30:00+0000": 600}},
                "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
            },
            200,
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) == 0:
            print("ERROR: Expected forecast.solar fallback call when Open-Meteo fails, got none")
            failed = True

        if f"sensor.{test_api.mock_base.prefix}_pv_today" not in test_api.dashboard_items:
            print("ERROR: Expected pv_today sensor to be published after forecast.solar fallback")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_open_meteo_first_ignored_when_unset(my_predbat):
    """
    When forecast_solar_open_meteo_first is False the existing ordering is unchanged:
    forecast.solar is primary and Open-Meteo is not called.
    """
    print("  - test_fetch_pv_forecast_open_meteo_first_ignored_when_unset")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.forecast_solar_open_meteo_first = False
        test_api.set_mock_response(
            "forecast.solar",
            {
                "result": {"watts": {"2025-06-15T12:00:00+0000": 500, "2025-06-15T12:30:00+0000": 600}},
                "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
            },
            200,
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        if len(forecast_calls) == 0:
            print("ERROR: Expected forecast.solar to remain primary when open_meteo_first is False")
            failed = True

        open_meteo_calls = [r for r in test_api.request_log if "open-meteo.com" in r["url"]]
        if len(open_meteo_calls) != 0:
            print(f"ERROR: Expected no Open-Meteo calls when open_meteo_first is False, got {len(open_meteo_calls)}")
            failed = True

    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_open_meteo_first_preserves_azimuth_zero_south(my_predbat):
    """
    A forecast_solar entry with azimuth_zero_south True must reach the Open-Meteo request
    with the azimuth unconverted. A regression here would silently mis-orient every array.
    """
    print("  - test_fetch_pv_forecast_open_meteo_first_preserves_azimuth_zero_south")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 54.81306, "longitude": -1.38647, "declination": 32, "azimuth": 85, "azimuth_zero_south": True, "kwp": 6.44}]
        test_api.solar.forecast_solar_open_meteo_first = True
        test_api.solar.open_meteo_forecast_max_age = 1.0
        test_api.set_mock_response(
            "api.open-meteo.com",
            {
                "hourly": {
                    "time": ["2025-06-15T12:00", "2025-06-15T13:00"],
                    "global_tilted_irradiance": [500.0, 600.0],
                    "temperature_2m": [25.0, 25.0],
                    "wind_speed_10m": [1.0, 1.0],
                }
            },
        )
        test_api.set_mock_response(
            "ensemble-api.open-meteo.com",
            {"hourly": {"time": ["2025-06-15T12:00", "2025-06-15T13:00"], "global_tilted_irradiance_member01": [400.0, 480.0]}},
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        forecast_urls = [r["url"] for r in test_api.request_log if "api.open-meteo.com" in r["url"]]
        if not forecast_urls:
            print("ERROR: Expected an Open-Meteo forecast request, got none")
            failed = True
        for url in forecast_urls:
            if "azimuth=85" not in url:
                print(f"ERROR: Expected azimuth=85 (unconverted) in Open-Meteo URL, got {url}")
                failed = True

    finally:
        test_api.cleanup()

    return failed
```

Register all four in `run_solcast_tests`. Find the block near line 3709 and add them immediately after the existing `..._backup_not_used_on_success` line:

```python
    failed |= test_fetch_pv_forecast_forecast_solar_open_meteo_backup_not_used_on_success(my_predbat)
    failed |= test_fetch_pv_forecast_open_meteo_first_used_when_set(my_predbat)
    failed |= test_fetch_pv_forecast_open_meteo_first_falls_back_on_failure(my_predbat)
    failed |= test_fetch_pv_forecast_open_meteo_first_ignored_when_unset(my_predbat)
    failed |= test_fetch_pv_forecast_open_meteo_first_preserves_azimuth_zero_south(my_predbat)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/solcast_step2.txt 2>&1
grep -E "ERROR|Traceback|AttributeError" /tmp/solcast_step2.txt
```

Expected: failures. `test_api.solar.forecast_solar_open_meteo_first = True` sets an attribute that `fetch_pv_forecast` never reads, so Open-Meteo is not called and `test_..._used_when_set` reports `ERROR: Expected Open-Meteo API call when open_meteo_first is set, got none`.

- [ ] **Step 3: Add the schema entry**

In `apps/predbat/config.py`, find line 2367 and insert the new key directly after it:

```python
    "forecast_solar_open_meteo_backup": {"type": "boolean"},
    "forecast_solar_open_meteo_first": {"type": "boolean"},
    "open_meteo_forecast": {"type": "dict_list"},
```

- [ ] **Step 4: Add the component arg**

In `apps/predbat/components.py`, insert directly after line 109:

```python
            "forecast_solar_open_meteo_backup": {"required": False, "config": "forecast_solar_open_meteo_backup", "default": False},
            "forecast_solar_open_meteo_first": {"required": False, "config": "forecast_solar_open_meteo_first", "default": False},
```

- [ ] **Step 5: Add the initialize() parameter**

In `apps/predbat/solcast.py`, add the parameter to `SolarAPI.initialize()` directly after `forecast_solar_open_meteo_backup,` (line 56):

```python
        forecast_solar_open_meteo_backup,
        forecast_solar_open_meteo_first,
```

And the assignment directly after line 73:

```python
        self.forecast_solar_open_meteo_backup = forecast_solar_open_meteo_backup
        self.forecast_solar_open_meteo_first = forecast_solar_open_meteo_first
```

- [ ] **Step 6: Update the test harness**

`TestSolarAPI.__init__` calls `initialize()` with keyword arguments, so it must pass the new one or every solcast test raises `TypeError`. In `apps/predbat/tests/test_solcast.py`, add directly after line 171:

```python
            forecast_solar_open_meteo_backup=False,
            forecast_solar_open_meteo_first=False,
```

- [ ] **Step 7: Reverse the source order**

In `apps/predbat/solcast.py`, replace lines 1218-1226 (the `if self.forecast_solar:` block through the existing backup fallback) with:

```python
        if self.forecast_solar and self.forecast_solar_open_meteo_first:
            self.log("SolarAPI: Obtaining solar forecast from Open-Meteo API (primary, Forecast Solar fallback)")
            primary_configs = self.open_meteo_forecast if self.open_meteo_forecast else self.forecast_solar
            pv_forecast_data, max_kwh = await self.download_open_meteo_data(configs=primary_configs)
            divide_by = 30.0
            create_pv10 = True
            if not pv_forecast_data:
                self.log("Warn: SolarAPI: Open-Meteo returned no data, falling back to Forecast Solar")
                pv_forecast_data, max_kwh = await self.download_forecast_solar_data()
        elif self.forecast_solar:
            self.log("SolarAPI: Obtaining solar forecast from Forecast Solar API")
            pv_forecast_data, max_kwh = await self.download_forecast_solar_data()
            divide_by = 30.0
            create_pv10 = True
            if not pv_forecast_data and self.forecast_solar_open_meteo_backup:
                self.log("SolarAPI: Forecast Solar returned no data, falling back to Open-Meteo backup")
                backup_configs = self.open_meteo_forecast if self.open_meteo_forecast else self.forecast_solar
                pv_forecast_data, max_kwh = await self.download_open_meteo_data(configs=backup_configs)
```

Leave the `elif self.open_meteo_forecast:` branch and everything below it untouched.

- [ ] **Step 8: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/solcast_step8.txt 2>&1
grep -E "ERROR|Traceback|FAIL" /tmp/solcast_step8.txt
```

Expected: no output from grep. If `test_..._ignored_when_unset` fails, the `elif` ordering is wrong — the `open_meteo_first` branch must be checked before the plain `forecast_solar` branch.

- [ ] **Step 9: Run pre-commit**

```bash
cd /Users/treforsouthwell/source/batpred
source coverage/venv/bin/activate
pre-commit run --files apps/predbat/solcast.py apps/predbat/config.py apps/predbat/components.py apps/predbat/tests/test_solcast.py
```

Expected: all hooks Passed. Black may reformat; if it does, re-stage the files.

- [ ] **Step 10: Commit**

```bash
git add apps/predbat/solcast.py apps/predbat/config.py apps/predbat/components.py apps/predbat/tests/test_solcast.py
git commit -m "feat(solar): add forecast_solar_open_meteo_first to use Open-Meteo as primary source"
```

---

### Task 2: Warn when the active forecast source changes

**Files:**
- Modify: `apps/predbat/solcast.py` (add `log_source_change()` helper; set `active_source` in each branch of `fetch_pv_forecast`; call the helper before the `if pv_forecast_data:` block at line 1267)
- Test: `apps/predbat/tests/test_solcast.py` (one new test)

**Interfaces:**
- Consumes: `self.storage` (may be `None`), `self.storage.load(module, filename)` and `self.storage.save(module, filename, data, format=, expiry=)`, both async.
- Produces: `async SolarAPI.log_source_change(source)` returning `None`. Persists `{"source": <str>}` under module `"solcast"`, filename `"active_forecast_source"`.

Source names used: `"open_meteo"`, `"forecast_solar"`, `"solcast"`, `"ha_sensors"`.

- [ ] **Step 1: Write the failing test**

Add to `apps/predbat/tests/test_solcast.py`, after `test_fetch_pv_forecast_open_meteo_first_preserves_azimuth_zero_south`:

```python
def test_fetch_pv_forecast_open_meteo_first_logs_source_change(my_predbat):
    """
    Switching the active forecast source emits a warning so the 7-day PV calibration
    settling period is visible in the log rather than silently skewing the scaling factor.
    """
    print("  - test_fetch_pv_forecast_open_meteo_first_logs_source_change")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0
        test_api.set_mock_response(
            "api.open-meteo.com",
            {
                "hourly": {
                    "time": ["2025-06-15T12:00", "2025-06-15T13:00"],
                    "global_tilted_irradiance": [500.0, 600.0],
                    "temperature_2m": [25.0, 25.0],
                    "wind_speed_10m": [1.0, 1.0],
                }
            },
        )
        test_api.set_mock_response(
            "ensemble-api.open-meteo.com",
            {"hourly": {"time": ["2025-06-15T12:00", "2025-06-15T13:00"], "global_tilted_irradiance_member01": [400.0, 480.0]}},
        )
        test_api.set_mock_response(
            "forecast.solar",
            {
                "result": {"watts": {"2025-06-15T12:00:00+0000": 500, "2025-06-15T12:30:00+0000": 600}},
                "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
            },
            200,
        )

        def create_mock_session(*args, **kwargs):
            """Create a mock aiohttp session."""
            return test_api.mock_aiohttp_session()

        # MockBase.log only prints, so capture messages by replacing the copied log reference.
        # ComponentBase copies base.log onto the component, so this override is local to the test.
        captured = []

        def capture_log(message, quiet=True):
            """Capture a log message emitted by SolarAPI."""
            captured.append(message)

        test_api.solar.log = capture_log

        # First run on forecast.solar establishes the stored source, no warning expected
        test_api.solar.forecast_solar_open_meteo_first = False
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        changed_first_run = [m for m in captured if "forecast source changed" in m]
        if changed_first_run:
            print(f"ERROR: Did not expect a source change warning on the first run, got {changed_first_run}")
            failed = True

        # Second run flips to Open-Meteo - a warning is expected
        captured.clear()
        test_api.solar.forecast_solar_open_meteo_first = True
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        changed_second_run = [m for m in captured if "forecast source changed" in m]
        if not changed_second_run:
            print("ERROR: Expected a source change warning after switching to Open-Meteo, got none")
            failed = True

    finally:
        test_api.cleanup()

    return failed
```

Register it in `run_solcast_tests` after the four added in Task 1:

```python
    failed |= test_fetch_pv_forecast_open_meteo_first_logs_source_change(my_predbat)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/solcast_t2_step3.txt 2>&1
grep -E "ERROR|Traceback" /tmp/solcast_t2_step3.txt
```

Expected: `ERROR: Expected a source change warning after switching to Open-Meteo, got none`.

- [ ] **Step 3: Add the helper method**

In `apps/predbat/solcast.py`, add this method directly above `async def fetch_pv_forecast(self):` (line 1204):

```python
    async def log_source_change(self, source):
        """Warn when the active solar forecast source changes so the PV calibration settling period is visible."""
        if not self.storage:
            return
        stored = await self.storage.load("solcast", "active_forecast_source")
        previous = stored.get("source") if isinstance(stored, dict) else None
        if previous == source:
            return
        if previous:
            self.log("Warn: SolarAPI: Solar forecast source changed from {} to {}, PV calibration will settle over the next 7 days".format(previous, source))
        await self.storage.save("solcast", "active_forecast_source", {"source": source}, format="json", expiry=None)
```

- [ ] **Step 4: Record the active source in each branch**

In `fetch_pv_forecast()`, add `active_source = "..."` assignments. In the `open_meteo_first` branch added in Task 1:

```python
        if self.forecast_solar and self.forecast_solar_open_meteo_first:
            self.log("SolarAPI: Obtaining solar forecast from Open-Meteo API (primary, Forecast Solar fallback)")
            primary_configs = self.open_meteo_forecast if self.open_meteo_forecast else self.forecast_solar
            pv_forecast_data, max_kwh = await self.download_open_meteo_data(configs=primary_configs)
            divide_by = 30.0
            create_pv10 = True
            active_source = "open_meteo"
            if not pv_forecast_data:
                self.log("Warn: SolarAPI: Open-Meteo returned no data, falling back to Forecast Solar")
                pv_forecast_data, max_kwh = await self.download_forecast_solar_data()
                active_source = "forecast_solar"
```

In the existing `elif self.forecast_solar:` branch:

```python
            active_source = "forecast_solar"
            if not pv_forecast_data and self.forecast_solar_open_meteo_backup:
                self.log("SolarAPI: Forecast Solar returned no data, falling back to Open-Meteo backup")
                backup_configs = self.open_meteo_forecast if self.open_meteo_forecast else self.forecast_solar
                pv_forecast_data, max_kwh = await self.download_open_meteo_data(configs=backup_configs)
                active_source = "open_meteo"
```

In the `elif self.open_meteo_forecast:` branch add `active_source = "open_meteo"`; in the `elif self.solcast_host and self.solcast_api_key:` branch add `active_source = "solcast"`; in the final `else:` (HA sensors) branch add `active_source = "ha_sensors"` immediately after `using_ha_data = True`.

- [ ] **Step 5: Call the helper**

Insert immediately before the `if pv_forecast_data:` line (line 1267 before this task's edits), after the whole if/elif chain has completed:

```python
        await self.log_source_change(active_source)

        if pv_forecast_data:
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --test solcast > /tmp/solcast_t2_step6.txt 2>&1
grep -E "ERROR|Traceback|FAIL" /tmp/solcast_t2_step6.txt
```

Expected: no output from grep.

- [ ] **Step 7: Run the full quick suite**

`fetch_pv_forecast` is on the main path, so check nothing else regressed:

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all --quick > /tmp/quick_out.txt 2>&1
grep -E "ERROR|FAIL|Traceback" /tmp/quick_out.txt
tail -5 /tmp/quick_out.txt
```

Expected: no errors. Note: if the suite hangs, a locally running Predbat may be holding port 5052 — stop it and re-run.

- [ ] **Step 8: Run pre-commit and commit**

```bash
cd /Users/treforsouthwell/source/batpred
source coverage/venv/bin/activate
pre-commit run --files apps/predbat/solcast.py apps/predbat/tests/test_solcast.py
git add apps/predbat/solcast.py apps/predbat/tests/test_solcast.py
git commit -m "feat(solar): warn when the active solar forecast source changes"
```

---

### Task 3: Document the setting

**Files:**
- Modify: `docs/apps-yaml.md:1584-1598` (the "Open-Meteo backup for Forecast.solar" section)
- Modify: `docs/install.md` (the matching backup section, around line 155)

**Interfaces:**
- Consumes: the setting name `forecast_solar_open_meteo_first` from Task 1.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Extend the apps-yaml.md section**

In `docs/apps-yaml.md`, append to the "Open-Meteo backup for Forecast.solar" section, after the existing example block:

````markdown
### Using Open-Meteo as the primary source

Setting `forecast_solar_open_meteo_first: true` reverses the order: Predbat fetches from Open-Meteo
first and only calls Forecast.solar if Open-Meteo returns no data. Your existing `forecast_solar`
entries are reused as-is, so no other configuration changes are needed. If you also have an
`open_meteo_forecast` section, that is used instead, which lets you apply Open-Meteo-specific
options such as `shading_factors`.

```yaml
  forecast_solar:
    - postcode: SW1A 2AB
      kwp: 3
      azimuth: 45
      declination: 45
  forecast_solar_open_meteo_first: true
```

While Open-Meteo is succeeding, Forecast.solar is not called at all, so no Forecast.solar API quota
is consumed.

Note that PV calibration compares the last seven days of recorded forecasts against actual
generation. After changing the source, that history still holds values from the previous source, so
the calibration scaling factor takes up to seven days to settle. Predbat logs a warning when the
source changes. Do not judge the accuracy of the new source until the settling period has passed.
````

- [ ] **Step 2: Add the matching note to install.md**

In `docs/install.md`, after the existing `azimuth_zero_south` paragraph around line 155, add:

```markdown
Setting `forecast_solar_open_meteo_first: true` makes Predbat use Open-Meteo as the primary forecast
source and fall back to Forecast.solar only if Open-Meteo returns no data. Your existing
`forecast_solar` settings are reused unchanged. See the Open-Meteo section in
[apps-yaml.md](apps-yaml.md) for details.
```

- [ ] **Step 3: Run pre-commit**

Both files are spell-checked and markdown-linted (unlike `docs/superpowers/`).

```bash
cd /Users/treforsouthwell/source/batpred
source coverage/venv/bin/activate
pre-commit run --files docs/apps-yaml.md docs/install.md
```

Expected: all Passed. If CSpell rejects a word, add it to `.cspell/custom-dictionary-workspace.txt`, then re-run pre-commit and re-stage — the dictionary file is auto-sorted on commit.

- [ ] **Step 4: Commit**

```bash
git add docs/apps-yaml.md docs/install.md
git commit -m "docs(solar): document forecast_solar_open_meteo_first"
```

---

## Verification

After all three tasks:

```bash
cd /Users/treforsouthwell/source/batpred/coverage
source venv/bin/activate
./run_all > /tmp/full_out.txt 2>&1
grep -E "ERROR|FAIL|Traceback" /tmp/full_out.txt
tail -20 /tmp/full_out.txt
```

Expected: the full suite passes.

Manual check on the target site: set `forecast_solar_open_meteo_first: true`, restart, and confirm the
log shows `SolarAPI: Obtaining solar forecast from Open-Meteo API (primary, Forecast Solar fallback)`,
a source change warning, and no `https://api.forecast.solar/` request.

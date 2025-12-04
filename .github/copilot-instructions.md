# Predbat AI Coding Agent Instructions

## Project Overview

Predbat is a Home Assistant battery prediction and automatic charging system. It runs in Home Assistant as an Addon or standalone native Linux/MAC or with a Docker,
predicting optimal battery charge/discharge schedules based on solar forecasts, energy rates, and consumption patterns.

**Core Architecture**: Multiple inheritance pattern with `PredBat` class inheriting from `hass.Hass`, `Octopus`, `Energidataservice`, `Fetch`, `Plan`, `Execute`, `Output`, and `UserInterface`.
The codebase uses a component-based architecture where major features extend `ComponentBase` for lifecycle management.

## Submitting changes

All changes MUST be submitted via Pull Requests to the `main` branch. Follow these guidelines:

- Create a branch for the feature/fix
- Write clear commit messages
- **Run pre-commit checks** before committing (see below)
- Create a pull request
- Ensure the PR tests are passing
- Wait for review
- Merge after approval

### Pre-commit Checks

Before committing code, ALWAYS run pre-commit hooks to ensure code quality:

```bash
cd coverage
source venv/bin/activate
pre-commit run --all-files
```

Common pre-commit checks:

- **Ruff** - Linter for unused imports (F401), etc.
- **Black** - Code formatter (256 char line length)
- **Trailing whitespace** - Removes trailing spaces
- **Markdown linting** - Checks markdown formatting
- **cspell** - Spell checking

If pre-commit fails, fix the issues and run again until all checks pass.

### Git Commands

Use GitKraken MCP tools or local git commands:

**GitKraken MCP tools** (preferred for most operations):

- `mcp_gitkraken_git_add_or_commit` - Stage and commit changes
- `mcp_gitkraken_git_push` - Push to remote
- `mcp_gitkraken_git_status` - Check git status
- `mcp_gitkraken_git_branch` - List/create branches
- `mcp_gitkraken_git_log_or_diff` - View log or diff

**Local git commands** (use when MCP tools fail or for complex operations):

```bash
git add .                    # Stage all changes
git commit -m "message"      # Commit with message
git push                     # Push to remote
git pull --rebase            # Pull with rebase
git status                   # Check status
```

**Important**: If `git push` fails with "rejected" error, run `git pull --rebase` first to sync with remote changes (often from pre-commit.ci), then push again.

## Code Structure & Patterns

### Core PredBat Class

The `PredBat` class in `apps/predbat/predbat.py` is the main orchestrator using multiple inheritance:

```python
class PredBat(hass.Hass, Octopus, Energidataservice, Fetch, Plan, Execute, Output, UserInterface):
```

**Main control loop** runs via `update_pred()` method called every 5 minutes (`RUN_EVERY = 5`):

1. `fetch_config_options()` - Load configuration
2. `fetch_sensor_data()` - Read HA sensor data
3. `fetch_inverter_data()` - Get inverter status
4. `calculate_plan()` - Compute optimal charge/discharge schedule (in `plan.py`)
5. `execute_plan()` - Send commands to inverter (in `execute.py`)

The core prediction/planning happens in `Plan` mixin (`apps/predbat/plan.py`) which orchestrates:

- `optimise_all_windows()` - Main optimization entry point
- `optimise_levels_pass()` - Initial rate-based optimization
- `optimise_detailed_pass()` - Fine-tune charge/export windows
- `run_prediction()` - Execute battery simulation via `Prediction` class

### Thread Pool System

**Multiprocessing for predictions** (in `apps/predbat/plan.py`):

```python
from multiprocessing import Pool, cpu_count

# Pool created on demand
if not self.pool:
    threads = self.get_arg("threads", "auto")
    if threads == "auto":
        self.pool = Pool(processes=cpu_count())
    else:
        self.pool = Pool(processes=int(threads))
```

**How it works**:

- `self.pool` is a `multiprocessing.Pool` instance (NOT thread pool - uses processes)
- Pool runs prediction scenarios in parallel using `pool.apply_async()`
- Wrapped functions in `prediction.py`: `wrapped_run_prediction_single()`, `wrapped_run_prediction_charge()`, etc.
- Global state shared via `PRED_GLOBAL` dict (copied to child processes)
- Pool configured via `threads` config option: `"auto"` for CPU count or specific number
- Returns `DummyThread` objects when pool disabled (for single-threaded testing)

**Key pattern**:

```python
# Async call to run prediction in pool
if self.pool and self.pool._state == "RUN":
    han = self.pool.apply_async(wrapped_run_prediction_charge, (args...))
    return han
else:
    # Fallback to synchronous execution
    result = wrapped_run_prediction_charge(args...)
    return DummyThread(result)
```

### Component System (Critical Pattern)

All major features inherit from `ComponentBase` (see `apps/predbat/component_base.py`):

```python
class MyComponent(ComponentBase):
    def initialize(self, **kwargs):
        # Component-specific initialization

    async def start(self):
        # Main async loop - MUST set self.api_started = True
        self.api_started = True
        while not self.api_stop:
            # Component work
            await asyncio.sleep(interval)
```

**Key components**: `WebInterface`, `PredbatMCPServer`, `SolarAPI`, `AlertFeed`, `GECloudDirect`, `CarbonAPI`, `FoxAPI`, `DatabaseManager`, `OctopusAPI`, `OhmeAPI`, `HAHistory`, `HAInterface`

Components are managed via `Components` class (`apps/predbat/components.py`) and initialized via plugin system (`apps/predbat/plugin_system.py`).

### Configuration System

- Config lives in `apps.yaml` (AppDaemon format) or `coverage/apps.yaml` for testing
- All config options defined in `apps/predbat/config.py` in `CONFIG_ITEMS` list
- Use `self.get_arg("config_name", default_value)` to access config values
- Entity naming follows pattern: `{prefix}.{entity_name}` (default prefix is `predbat`)

### Multi-Inverter Support

The codebase supports multiple inverters (battery systems) via the `Inverter` class (`apps/predbat/inverter.py`). Key patterns:

- `num_inverters` config determines count
- Inverter control via REST API (`givtcp_rest` config) or HA entities
- Use `self.inverter_control()` wrapper for inverter commands
- Supports GivEnergy, Solis, Huawei, SolarEdge, Sofar inverters

## Testing Workflow

### Running Unit Tests

```bash
cd coverage/
./run_all
```

Use --quick argument to skip long tests:

**Test structure**: 9994-line `unit_test.py` contains ALL tests. Uses custom `TestHAInterface` mock, not pytest/unittest.

**Key test patterns**:

- Tests are functions prefixed `test_*` (e.g., `test_basic_rates()`, `test_inverter_self_test()`)
- Mock HA via `TestHAInterface` class which stores dummy state in `dummy_items` dict
- Tests must be run from the coverage directory
- Run all tests: `./run_all`
- Run named unit tests `./run_all --test test_name`
- Run specific debug scenarios: `python3 ../apps/predbat/unit_test.py --debug predbat_debug_file.yaml`
- Performance tests: `./run_all --perf-only`

**Coverage analysis**:

```bash
cd coverage/
./run_cov  --quick # Generates htmlcov/index.html
```

### Test Data

- Test cases in `coverage/cases/*.yaml` - YAML format with expected JSON outputs
- Debug scenarios: `coverage/predbat_debug_*.yaml` files
- Plan outputs: `coverage/plan_*.html` visualization files

## Code Quality & Formatting

**Pre-commit hooks** (`.pre-commit-config.yaml`):

- Black formatting: 256 char line length (see `pyproject.toml`)
- Ruff linter (F401 - unused imports only)
- Markdown linting
- JSON5 validation
- cspell dictionary

**Formatting rules** (critical):

- Line length: 256 chars (Black config)
- `# fmt: off` at top of most Python files to disable autoformat
- `# pylint: disable=line-too-long` used extensively
- Import sorting via isort (currently disabled in pre-commit)

**Running pre-commit checks**:

Pre-commit hooks automatically check code quality before commits. They are also run automatically by pre-commit.ci on pull requests.

```bash
# Run all pre-commit checks locally
python3 -m pre_commit run --all-files

# Install pre-commit hooks (one-time setup)
python3 -m pre_commit install
```

**Common pre-commit failures and fixes**:

1. **Ruff F401 (unused imports)**: Remove the unused import

   ```python
   # Bad
   from datetime import datetime, timedelta  # datetime unused

   # Good
   from datetime import timedelta
   ```

2. **Trailing whitespace**: Automatically fixed by pre-commit
3. **Black formatting**: Automatically reformatted by pre-commit

Always run pre-commit checks before pushing to catch issues early.

## Development Patterns

### Logging

Always use `self.log()` not `print()`:

```python
self.log("Info: Starting component")
self.log("Warn: Something unusual: {}".format(value))
self.log("Error: Failed with: {}".format(e))
```

### Time Handling

- Always use timezone-aware datetime: `self.now_utc`, `self.midnight_utc`, `self.minutes_now`
- Time formats in `config.py`: `TIME_FORMAT`, `TIME_FORMAT_HA`, `TIME_FORMAT_OCTOPUS`
- Prediction step: 5 minutes (`PREDICT_STEP = 5`)
- Use `utils.py` helpers: `dp1()`, `dp2()`, `dp3()` for decimal formatting

### Prediction Engine

Core prediction happens in `apps/predbat/prediction.py`:

- `Prediction` class runs battery simulation in 5-min steps
- Multiprocessing via `wrapped_run_prediction_*` functions
- Global state in `PRED_GLOBAL` dict for process sharing
- Optimization in `apps/predbat/plan.py` via `optimise_all_windows()`

### State Management

**DO NOT** directly access HA state. Always use wrappers:

```python
# Read state
value = self.get_state_wrapper(entity_id, default=0, attribute="attr_name")

# Write state  
self.set_state_wrapper(entity_id, state, attributes={...})

# History data
history = self.get_history_wrapper(entity_id, days=30)
```

### REST API & Web Interface

Web interface in `apps/predbat/web.py` runs on port 5052 (configurable via `web_port`):

- `/api/state` - GET/POST entity states
- `/api/service` - POST to call HA services
- `/api/plan` - GET battery plan visualization
- MCP server integration via `apps/predbat/web_mcp.py`

## File Organization

**Main entry**: `apps/predbat/predbat.py` - defines `PredBat` class and version (`THIS_VERSION`)

**Key modules**:

- `config.py` - All configuration constants and schema
- `prediction.py` - Battery simulation engine
- `plan.py` - Optimization algorithms
- `execute.py` - Inverter control execution
- `inverter.py` - Multi-inverter abstraction layer
- `ha.py` - Home Assistant interface (`HAInterface`, `HAHistory`)
- `utils.py` - Helper functions (time, math, data manipulation)
- `fetch.py` - Data fetching (solar, rates, history)
- `output.py` - Entity/sensor publishing
- `userinterface.py` - HA UI components (selects, switches, numbers)
- `unit_tests.py` - Unit test runner, imports tests from tests/*

**External integrations**:

- `octopus.py` - Octopus Energy API (rates, saving sessions)
- `solcast.py` - Solcast solar forecasting
- `gecloud.py` - GivEnergy cloud API
- `fox.py` - Fox ESS cloud API  
- `ohme.py` - Ohme EV charger integration
- `carbon.py` - Carbon intensity API

## Common Gotchas

1. **Never modify globals directly** - Use `reset_prediction_globals()` pattern in `prediction.py`
2. **Entity IDs use prefix** - Always prepend `{prefix}.` (usually `predbat.`)
3. **Time is always minutes since midnight** - Not timestamps in core logic
4. **Multi-inverter indexing** - Many configs are lists: `inverter_limit[0]`, `inverter_limit[1]`
5. **REST API fallback** - Code must work with both HA entities AND REST API for inverter control
6. **File list is hardcoded** - `PREDBAT_FILES` in `predbat.py` must be updated when adding files
7. **Test in coverage dir** - Unit tests MUST run from `coverage/` directory, not project root

## Documentation

Built with mkdocs (see `mkdocs.yml`):

```bash
mkdocs serve  # Live preview on port 8000
mkdocs build  # Generate static site
```

Docs in `docs/` folder, published to GitHub Pages via `.github/workflows/publish-docs.yml`.

## Dependencies

See `requirements.txt`:

- `aiohttp` - Async HTTP
- `pytz` - Timezone handling  
- `requests` - Sync HTTP
- `ruamel.yaml` - YAML parsing
- `pyjwt` - JWT tokens (for APIs)
- `matplotlib` - Chart generation (testing only)

## License & Copyright

Copyright © Trefor Southwell 2025 - Personal use only. See `License.md` for terms.

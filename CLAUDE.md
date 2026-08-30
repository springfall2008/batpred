# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Predbat is a Home Assistant addon (app) that predicts and optimizes home battery charging/discharging based on electricity rates, solar forecasts, and historical load data. It supports inverters from GivEnergy, Solis, Huawei, SolarEdge, and Sofar, and integrates with energy providers like Octopus Energy, Kraken (EDF/E.ON), and Axle Energy VPP.

It also supports Predbat.com which is a cloud based product that does not use Home Assistant and can run in a Docker environment.

## Running Tests

Tests take time to run, _always_ save the test output to a file and then grep the file afterwards.
Never just pipe the output to grep as if you search for the wrong thing you will have to re-run it again.

Tests live in `apps/predbat/tests/` and are run from the `coverage/` directory:

```bash
# First-time setup (creates venv and installs deps):
cd coverage
source setup.csh

# Run all tests:
./run_all

# Skip slow tests (used by CI):
./run_all --quick

# Run a specific test by name:
./run_all --test basic_rates

# Run multiple specific tests:
./run_all --test basic_rates --test units

# Run tests matching a keyword:
./run_all -k octopus_

# List all available test names:
./run_all --list

# Coverage analysis:
./run_cov --quick
# Then open htmlcov/index.html
```

The `run_all` script is a thin wrapper; you can run `unit_test.py` directly from the `coverage/` directory (it needs to be the working directory so relative paths resolve).

### Replaying a debug dump

`predbat_debug_*.yaml` is a full state dump, written to `debug/` when `switch.predbat_debug_enable` is on and normally what users attach to bug reports. Replay one against the current tree with:

```bash
cd coverage
./run_all --debug_file <path-to-predbat_debug.yaml>
```

It lists every config item that differs from its default, recalculates the plan, and writes `plan_orig.html` / `plan_final.json` into `coverage/`. Add `--redo` to recompute rates, load model and Octopus slots instead of reusing the ones in the dump. Committed examples live in `coverage/cases/*.yaml` and run as golden regressions under `./run_all --test debug_cases`.

### Debugging notes

`.claude/skills/issue-triage/references/debug-journal.md` records what past investigations found: per-integration API quirks, symptom-to-module pointers, and traps such as stale kernel binaries and test-order pollution. Read it before debugging an integration or a "the plan is wrong" report, and add to it when you learn something a future session would want.

## Code Quality

All checks are enforced via pre-commit and must pass before merging:

```bash
./run_pre_commit
```

Key constraints:

- **Line length**: 256 chars (Black), 250 chars (Flake8)
- **Docstrings**: 100% coverage required (`interrogate`) for all functions and classes
- **Spell checking**: British English (`en-gb`) via CSpell; add valid unknown words to `.cspell/custom-dictionary-workspace.txt` (file is auto-sorted alphabetically on commit, so re-stage after running pre-commit)
- **Variable naming**: `lower_case_with_underscores`
- pre-commit.ci will auto-commit fixable issues (trailing whitespace, etc.) back to your PR branch — run `git pull` after pushing to avoid divergence

## Architecture

### Orchestrator Pattern

`PredBat` in `predbat.py` is the main class and uses **multiple inheritance** to compose its behaviour:

```python
class PredBat(hass.Hass, Octopus, Energidataservice, Fetch, Plan, Execute, Output, UserInterface):
```

The main loop (`update_pred()`) runs every 5 minutes: fetch data → run optimization → execute plan → publish results.

### Core Modules

| Module | Role |
|--------|------|
| `plan.py` | Optimization engine — multi-threaded search across thousands of charge/discharge window scenarios |
| `predict.py` / `prediction.py` | Battery SOC prediction models, PV generation, load forecasting |
| `fetch.py` | Pulls PV forecasts, historical load, rate data, and inverter state |
| `execute.py` | Sends charge/discharge/reserve commands to inverters |
| `output.py` | Creates and updates Home Assistant sensors, switches, selects |
| `inverter.py` | Multi-inverter abstraction layer (GivEnergy, Solis, Huawei, SolarEdge, Sofar) |
| `config.py` | Defines `CONFIG_ITEMS` (all user settings) and `APPS_SCHEMA` (YAML validation) |
| `ha.py` | WebSocket + REST communication with Home Assistant |
| `userinterface.py` | Manages HA input entities (switches, selects, input_numbers) |
| `components.py` | Plugin registry and component lifecycle management |
| `component_base.py` | Abstract base class for all pluggable components |

### Component/Plugin System

`components.py` defines a registry of 18 pluggable components (DB, HA, Web, MCP, GECloud, Octopus, Fox, Solax, Solis, Axle, Ohme, Kraken, etc.). Each component:

- Inherits from `ComponentBase`
- Has `api_start()` / `api_stop()` lifecycle methods
- Can be independently enabled/disabled
- Has health monitoring with exponential backoff
- Routes HA events via entity prefix filtering

### Key Data Flow

1. `Fetch` retrieves rates (Octopus/Kraken API), solar forecasts (Solcast), historical load (HA history), and live inverter state
2. `Plan` runs a search algorithm to find the optimal set of charge/discharge windows over a 48-hour horizon
3. `Execute` sends the resulting commands to the inverter
4. `Output` publishes the plan and metrics as HA sensor states

### Storage

The Storage component provides an abstraction of saving/loading from a cache and must be used instead of direct file access.

### Testing Infrastructure

`unit_test.py` uses `TestHAInterface` (from `tests/test_infra.py`) to mock the Home Assistant connection. Tests call `create_predbat()` which builds a full `PredBat` instance against the mock. Individual test modules in `tests/` follow the naming convention `test_<feature>.py` with an exported `run_<feature>_tests()` or `test_<feature>()` function registered in `TEST_REGISTRY` in `unit_test.py`.

**IMPORTANT** Unit tests must be added for all new code.

## Documentation

Documentation source lives in `docs/` and is built with MkDocs:

```bash
mkdocs serve   # Live preview at http://localhost:8000
```

When adding a new doc page, add it to `mkdocs.yml`. The published site at <https://springfall2008.github.io/batpred/> is built automatically from `main` via GitHub Actions.

<!-- gitnexus:start -->
## GitNexus — Code Intelligence

This project is indexed by GitNexus as **batpred** (13494 symbols, 38059 relationships, 279 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/batpred/context` | Codebase overview, check index freshness |
| `gitnexus://repo/batpred/clusters` | All functional areas |
| `gitnexus://repo/batpred/processes` | All execution flows |
| `gitnexus://repo/batpred/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

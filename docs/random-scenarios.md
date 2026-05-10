# Random Scenario Benchmarking

The random scenario framework lets you generate reproducible synthetic test cases, run them through the optimisation engine, compare two result sets, and profile performance — all from the `coverage/` directory.

---

## Overview

A *scenario* is a fully self-contained synthetic energy environment: battery size, charge/discharge limits, load profile, PV profile, import/export rates, and initial SOC. Each scenario is deterministically generated from an integer seed, so the same seed always produces the same scenario regardless of when you run it.

The workflow is:

1. **Generate** a set of scenarios and save them to YAML.
2. **Run** the scenarios against a template debug YAML, saving metric/cost/runtime results to JSON.
3. Make a code change.
4. **Run** the same scenarios again to a second JSON file.
5. **Compare** the two JSON files to see which scenarios improved or regressed.
6. Optionally **profile** a single scenario to find performance hotspots.

---

## Prerequisites

All commands are run from the `coverage/` directory with the project virtual environment active:

```bash
cd coverage
source venv/bin/activate   # or: source setup.csh
```

A **template debug YAML** is required for the run and profile steps. This is any `predbat_debug_*.yaml` file that provides the HA entity state and configuration baseline. The random scenario data (rates, load, PV, battery) is overlaid on top of it.

Example template files already present:

- `cases/predbat_debug_agile1.yaml`
- `predbat_debug_plan.yaml`

---

## Step 1 — Generate Scenarios

```bash
python3 ../apps/predbat/unit_test.py \
  --random-generate \
  --random-count 50 \
  --random-seed 0 \
  --random-output random_scenarios.yaml
```

| Option | Default | Description |
|---|---|---|
| `--random-count N` | 100 | Number of scenarios to generate |
| `--random-seed N` | 0 | Seed for the first scenario; scenario `i` uses seed `start + i` |
| `--random-output PATH` | `random_scenarios.yaml` | Output YAML file |

The YAML file contains the full generated time-series data (rates, load, PV) embedded in each scenario entry so the file is entirely self-contained — regenerating from the same seed always produces identical data.

---

## Step 2 — Run Scenarios

```bash
python3 ../apps/predbat/unit_test.py \
  --random-run \
  --random-template cases/predbat_debug_agile1.yaml \
  --random-scenarios random_scenarios.yaml \
  --random-results random_results_before.json
```

| Option | Default | Description |
|---|---|---|
| `--random-template PATH` | *(required)* | Template debug YAML to load as the baseline configuration |
| `--random-scenarios PATH` | `random_scenarios.yaml` | Scenarios YAML file to read |
| `--random-results PATH` | `random_results.json` | Output JSON file for results |
| `--random-scenario N` | *(all)* | Run only the scenario with this id number |
| `--full-debug` | off | Save a `plan_scenario{id}.html` plan visualisation for each scenario |

Each scenario prints a progress line:

```text
Scenario 3/50 seed=2 metric=541.6 cost=726.1 runtime=0.462s [ok]
```

The `metric` is the full optimisation metric (cost + battery value adjustment + PV10 weighting + carbon). The `cost` is the raw import/export money with no adjustments. `runtime_s` measures the wall-clock time of `calculate_plan()` alone.

### Results JSON format

```json
{
  "run_info": {
    "template_yaml": "cases/predbat_debug_agile1.yaml",
    "scenarios_file": "random_scenarios.yaml",
    "timestamp": "2026-05-04T10:00:00+00:00"
  },
  "results": [
    {
      "id": 0,
      "seed": 0,
      "metric": 541.6327,
      "cost": 726.0931,
      "import_kwh_battery": 1.23,
      "import_kwh_house": 5.67,
      "export_kwh": 0.45,
      "soc_min": 0.5,
      "soc_final": 2.4,
      "battery_cycles": 0.8,
      "carbon_g": 1234.5,
      "runtime_s": 0.462,
      "failed": false,
      "error": null
    }
  ]
}
```

---

## Step 3 — Compare Two Result Sets

After making a code change, run the scenarios again to a different output file, then compare:

```bash
# Run with the new code
python3 ../apps/predbat/unit_test.py \
  --random-run \
  --random-template cases/predbat_debug_agile1.yaml \
  --random-results random_results_after.json

# Compare
python3 ../apps/predbat/unit_test.py \
  --random-compare random_results_before.json random_results_after.json
```

The compare output is a per-scenario table followed by summary statistics:

```text
Comparing results:
  A: random_results_before.json (2026-05-04T09:00:00+00:00)
  B: random_results_after.json  (2026-05-04T10:00:00+00:00)

  ID      metric_A      metric_B    met_diff        cost_A        cost_B   cost_diff    time_A    time_B    status
------------------------------------------------------------------------------------------------------------------
   0      541.6327      541.6327     +0.0000      726.0931      726.0931     +0.0000    0.462s    0.389s
   1      481.6034      482.7461     +1.1427      564.8907      564.7083     -0.1824    0.451s    0.401s
...
------------------------------------------------------------------------------------------------------------------

Metric summary (50 scenarios compared):
  Average diff : +0.2341  (+ = B worse, - = B better)
  Min diff     : -8.9576
  Max diff     : +20.5438
  B worse      : 5
  B better     : 4
  Unchanged    : 41

Cost summary (raw import/export, no battery value adjustment):
  Average diff : +0.1234
  ...

Runtime summary (optimisation wall-clock time):
  Average A    : 0.461s
  Average B    : 0.395s
  Average diff : -0.066s  (+ = B slower, - = B faster)
  Min diff     : -0.120s
  Max diff     : +0.015s
```

**Interpreting metric diff**: a positive value means B (the new code) produced a *worse* plan — the optimiser is spending more money or leaving less battery value. A negative value is an improvement.

---

## Step 4 — Profile a Single Scenario

Profile scenario 0 and print the top 30 hotspot functions ordered by cumulative time:

```bash
python3 ../apps/predbat/unit_test.py \
  --random-profile \
  --random-template cases/predbat_debug_agile1.yaml \
  --random-scenario 0
```

| Option | Default | Description |
|---|---|---|
| `--random-profile-lines N` | 30 | Number of top functions to display |
| `--random-profile-sort KEY` | `cumulative` | Sort order: `cumulative`, `tottime`, `calls` |
| `--random-profile-output PATH` | *(none)* | Also write a `.prof` binary file for use with external tools |

### Sorting options

| Key | Shows |
|---|---|
| `cumulative` | Total time spent in function + all callees — best for finding the slow *path* |
| `tottime` | Time spent only inside the function body — best for finding the slow *work* |
| `calls` | Number of calls — best for finding hot loops |

### Using an external profiler

Save the raw `.prof` file and open it with [snakeviz](https://jiffyclub.github.io/snakeviz/) for an interactive flame graph:

```bash
python3 ../apps/predbat/unit_test.py \
  --random-profile \
  --random-template cases/predbat_debug_agile1.yaml \
  --random-scenario 0 \
  --random-profile-output profile.prof

pip install snakeviz
snakeviz profile.prof
```

---

## Quick Reference

```bash
# Generate 20 scenarios from seed 42
python3 ../apps/predbat/unit_test.py --random-generate --random-count 20 --random-seed 42 --random-output my_scenarios.yaml

# Run all scenarios, save baseline
python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml \
  --random-scenarios my_scenarios.yaml --random-results baseline.json

# Run a single scenario for quick iteration
python3 ../apps/predbat/unit_test.py --random-run --random-template cases/predbat_debug_agile1.yaml \
  --random-scenarios my_scenarios.yaml --random-scenario 3 --random-results new.json

# Compare baseline vs new
python3 ../apps/predbat/unit_test.py --random-compare baseline.json new.json

# Profile scenario 3, sort by self-time
python3 ../apps/predbat/unit_test.py --random-profile --random-template cases/predbat_debug_agile1.yaml \
  --random-scenario 3 --random-profile-sort tottime --random-profile-lines 40
```

# GE Cloud API budget harness — design

Date: 2026-11-06
Status: approved; harness built, baseline recorded below

## Why

GivEnergy is introducing a monthly limit of 10,000 API calls per account; going over it costs money. 10,000 a month is
about 330 calls a day — roughly one per 5-minute Predbat cycle. Reading `apps/predbat/gecloud.py` suggests a single
ordinary (non-EMS) inverter currently makes somewhere around 12,000–16,000 calls **a day**, dominated by the full
register sweep, which reads every setting individually and runs every 10 minutes because `polling_mode` is true for
every non-EMS site.

Before changing anything we want a **measured baseline**: a harness that runs a realistic simulated day through the
real `execute.py` → `gecloud.py` path against a fake GE Cloud server, counts every API call, and logs when and why each
one was made. The harness is then the yardstick for the optimisation work that follows.

The user's view of a realistic target: 10,000 a month may not be achievable once writes are counted; 20,000 a month
(about 660 a day) is more feasible. The open question the harness must answer is **what execution costs** — how many
writes a day a real plan generates.

## Scope of this piece of work

**In scope:** the harness, and running it to record the baseline for current `main`.

**Not in scope (follow-on work, measured with this harness afterwards):** changing gecloud's polling. The user has
already decided the direction for that follow-on:

- status and meter polled every 5 minutes (currently every 2 minutes);
- device info moved to a daily poll (currently every 2 minutes, and called once per inverter although the endpoint
  returns every device on the account);
- the full register sweep dropped, replaced by reading only the registers Predbat actually uses, every 12 hours;
- the `GECloudData` history download is not used and can go.

The harness must be able to show the before/after count for each of those changes, but making them is not part of
this work.

## Goals

1. Count every HTTP request `GECloudDirect` (and `GECloudData`, if enabled) makes during a simulated 24 hours.
2. Produce a per-call log that says **when** (simulated time), **what** (method, endpoint, and the setting name for a
   register read or write) and **why** (which polling branch made it, or which execute action triggered the write —
   e.g. "charge window start 23:30 → 00:00").
3. Summarise: total per day, breakdown by category (status, meter, device info, register list, register reads,
   register writes, account, site, device list, EVC, history, retries), and projection to a 30-day month compared
   against 10,000 and 20,000.
4. Writes must be genuine: they travel the real route — `execute.py` → inverter control via HA entity →
   gecloud's `select_event` / `number_event` / `switch_event` → HTTP — not be synthesised by the harness.
5. Run several random scenarios, because writes vary a lot by plan shape (export days, cars, IOG dispatches), and
   report each scenario plus the worst case.

## Non-goals

- Simulating GivEnergy's real behaviour beyond what is needed to keep Predbat on its normal code path (no rate-limit
  emulation, no device timeouts unless the builder wants a retry-path test).
- Modelling physics accurately. SOC only needs to be believable enough that execute makes realistic decisions.
- EMS / Gateway / multi-inverter / EV charger topologies in the first version (see "Left to the builder").
- Changing any production polling behaviour.

## Anti-goals (would make it a failure even if it runs)

- Calls that bypass the fake server and go to the real `api.givenergy.cloud`. The fake must be the only reachable
  endpoint; an unmatched request should fail loudly, not silently succeed.
- Counting at a layer that misses calls — e.g. counting `async_get_inverter_data` invocations but not retries, or
  counting in gecloud but not in `GECloudData`. Count at the HTTP boundary.
- A harness that takes real wall-clock time. Retry back-off sleeps (`asyncio.sleep` in gecloud),
  `write_and_poll_sleep` (10 s for GE Cloud inverters) and the 60-second component loop must all be skipped; a
  simulated day should take seconds to a few minutes.
- A "why" column that is just the endpoint restated. The reason has to point at the code branch or the plan change
  responsible, or the log is useless for tuning.

## How it works (decided)

### Fake GE Cloud server

A local HTTP stand-in for `GE_API_URL` (`https://api.givenergy.cloud/v1/`). It serves:

- `account`, `site/{uuid}`, `communication-device`, `inverter/{serial}/system-data/latest` (status),
  `inverter/{serial}/meter-data/latest` (meter), `inverter/{serial}/settings` (register list),
  `inverter/{serial}/settings/{id}/read`, `inverter/{serial}/settings/{id}/write`, and the inverter
  `data-points/{date}` history endpoint if `GECloudData` is enabled;
- a register list taken from a real capture: `coverage/ge_cloud.log` (git-ignored; produced by `gecloud.py`'s
  `main()` test mode against a real account). It holds 81 control registers — 28 number, 45 select, 8 switch — from a
  GivEnergy **Gateway** site (two All-In-Ones behind it): AC charge 1–10 start/end/upper SOC limit, DC discharge 1–10
  start/end/lower SOC limit, battery charge/discharge power, charge/discharge power percentage, reserve, cutoff,
  AC charge upper limit + enable, eco mode, DC discharge enable, pause battery + start/end, export power priority, EPS,
  calculated load, force off grid, max output active power, restart hardware. The log gives each register's friendly
  name and entity type (and so its HA name) but **not** its setting id or validation rules; ids are assigned by the
  harness and validation rules reconstructed from type (time selects → `date_format:H:i`, numbers → `between:`,
  switches → boolean, option selects → `in:` / options text; restart hardware → `writeonly`).
  **The log contains a live API key and a customer's personal details: copy only register names and types out of it,
  never serials, the key, or account data.** Use anonymised serials in the fixture. The user can capture a fresh log
  if needed;
- writes update the stored register value, so subsequent reads return what was written.

Status responses carry SOC; meter responses carry today/total kWh. Both evolve over the simulated day (see below).

Every request is recorded with the simulated time, method, path and — where the harness can attach one — a reason tag.

### The simulated day

- Take a scenario from the random-scenario generator in `apps/predbat/tests/test_random_scenarios.py`
  (`generate_random_scenario` / `apply_scenario_to_predbat`) to set rates, load, PV and battery.
- Build a `PredBat` configured for a single GE Cloud inverter (`ge_cloud_direct`, `ge_cloud_automatic`, inverter
  type `GEC`) with the `GECloudDirect` component pointed at the fake server. **Settings: the defaults a GE Cloud user
  gets** — `CONFIG_ITEMS` defaults plus whatever `ge_cloud_automatic` sets up — not a particular user's debug dump.
  Low power charge (`set_charge_low_power` on) is a follow-on variant, expected to be the heaviest writer; leave room
  for a settings variant without building it now.
- Start cold: the first gecloud `run()` uses `first=True` with no storage cache, as a real start-up does. **Start-up
  calls (account, device list, site, register list, first sweep, automatic config, start-up default-option writes) are
  reported on their own line**, separate from the steady-state day. The month projection is made from the
  steady-state 24 hours only, with start-up shown as a per-restart cost. *(Builder's call, made by the designer.)*
- Step the clock 288 times in 5-minute increments, from 00:00 to 23:55. Each step:
  1. run gecloud's `run(seconds, first)` once for each simulated minute in the step, with `seconds` advancing as the
     real component loop would (so the 2-minute / 10-minute / hourly branches fire at their real cadence);
  2. **advance the existing plan — do not re-plan.** The plan is optimised once at the start (`calculate_plan(recompute=True)`).
     Each later step moves the clock forward and calls `calculate_plan(recompute=False)`, which is exactly what a real
     `update_pred()` cycle does when it does not re-plan: it drops charge and export windows whose end is in the past, so
     the first window is always the current or next one (plan.py, the "Shift onto next charge/export window" loops at the
     top of `calculate_plan`), and refreshes the prediction without optimising again. Use that function rather than
     re-implementing the trim. *(Decided by the user.)* Rationale: with SOC following the plan and fixed forecasts a
     re-plan would mostly reproduce the same plan at many times the run time, and advancing keeps every write
     attributable to execute reacting to time and SOC.
  3. run `execute_plan()`, whose inverter writes reach gecloud through the HA event route.
- **The day runs midnight to 23:55.** The plan is generated at midnight (`minutes_now = 0`, overriding the random
  scenario's own start time), and the last step is at 23:55. That gives 288 steps that never cross midnight, so
  `calculate_plan`'s start-of-day forced recompute ("Force recompute due to start of day") never fires and no clock
  handling is needed. *(Decided by the user.)*
- **Re-planning every cycle is a follow-on**, paired with the SOC-perturbation mode below: once SOC drifts from the
  prediction, plan correction becomes a real source of writes and is worth measuring.

### SOC and meter model

**Decided: start with the ideal case.** The fake server's SOC follows the plan's own predicted SOC for the current
simulated minute (from the plan's prediction), and forecasts do not change during the day. Meter counters
accumulate from the scenario's load and PV for the elapsed time. Because reality never departs from the plan, the
write count this gives is a **floor**, and the summary must say so.

**Follow-on, once the ideal run works:** a perturbation mode that nudges SOC away from the prediction at each 5-minute
step (e.g. a few percent, seeded so runs are repeatable), combined with re-planning each cycle, to see how much extra writing plan correction causes. Design
the SOC source so this is a small addition (one hook where the next step's SOC is decided), not a rework.

### Output

- A summary printed at the end: per-scenario totals, per-category breakdown, worst case, month projection vs 10k / 20k.
- A per-call log written to a file (CSV or JSON lines) with columns: simulated time, scenario id, method, endpoint
  template, serial, setting id + name, reason, HTTP status, whether it was a retry.

### Where it lives and how it is run

A slow test registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`, run with
`./run_all --test gecloud_api_budget` from `coverage/`, excluded from `--quick`. It writes the log into `coverage/`
(or a path given on the command line). The first version records the baseline; it should not assert a budget yet
(current main would fail any sensible one). A budget assertion can be added once the optimisation lands.

The harness itself needs unit tests for its own moving parts (fake server routing, the call log, the reason tagging,
the summary maths), per the repo rule that all new code is tested.

## Constraints

- Repo conventions: docstrings on every function/class (interrogate 100%), British spelling (cspell), Black line
  length 256, `lower_case_with_underscores`.
- `gecloud.py` must not grow harness-only branches in production code paths. If attaching a "why" needs a hook, prefer
  something cheap and general (e.g. a context/reason that the call layer can read) over special-casing the test.
- Use the repo's existing test infrastructure (`create_predbat()`, `TestHAInterface`) rather than a parallel one.

## Known wiring points (for the builder)

- Inverter writes for GE Cloud go through `call_service_wrapper` (`number/set_value`, `select/select_option`,
  `switch/turn_on` / `turn_off`) in `inverter.py` (`write_and_poll_value`, `write_and_poll_option`,
  `write_and_poll_switch`). In a real install HA turns those into events that reach `userinterface.py`'s
  `select_event` / `number_event` / `switch_event`, then `components.py` routes them to `GECloudDirect` by entity
  prefix. `TestHAInterface.call_service` currently just updates `dummy_items`; the harness has to complete that loop.
- After a write gecloud republishes the register entity from the write's response, so execute's read-back costs no
  API call. The harness should confirm this rather than assume it.
- `async_get_inverter_data` opens a fresh `aiohttp.ClientSession` per call against `GE_API_URL`; redirecting to the
  fake can be done by pointing the URL at a local server or by intercepting the session — builder's choice, as long as
  counting happens at the HTTP boundary.
- `component_base.py` drives `run()` every 60 s; the harness calls `run()` directly with simulated `seconds` instead.

## Left to the builder

- How the fake server is implemented (aiohttp test server, a patched client session, etc.).
- How reasons are attached to calls (call-site tags, a context variable, stack inspection), provided the result names
  the polling branch or the execute action.
- Setting ids and reconstructed validation rules for the captured register list.
- How many scenarios run by default and which seeds (enough to cover export, car and IOG variants), and the
  command-line options for choosing scenarios, fixed-plan mode and the log path.
- Whether `GECloudData` (history download) is included in the default run — it is configurable today; the summary
  should make clear whether it was on.
- Whether to include an optional multi-inverter or EMS variant later.

## Topology note

The captured register list comes from a Gateway site, but the first version simulates a **single** inverter with no
EMS and no Gateway. That is the commonest install and the one where `polling_mode` makes the register sweep run every
10 minutes. Note that a Gateway site also sweeps every 10 minutes (the gateway device is always swept), so the baseline
applies to it too. A single hybrid's real register list may be a little shorter than the Gateway's 81; the summary
should state the register count it ran with so the sweep cost can be rescaled.

## Baseline results (main @ e5f424be)

Measured with `./run_all --test gecloud_api_budget`: one GE Cloud inverter, no EMS, default settings,
80 readable registers (81 captured, one write-only). Execute runs every 5 minutes from 00:00 to 23:55,
and gecloud's own loop carries on to 24:00, so a whole 24 hours of polling is counted. SOC follows the
plan exactly, so the write counts are a **floor**. The per-call log is written to
`coverage/gecloud_api_budget_log.jsonl`.

| Scenario | Plan shape | Calls/day | Writes/day | Month (×30) | vs 10k | vs 20k |
| -------- | ---------- | --------: | ---------: | ----------: | -----: | -----: |
| 0 | IOG, negative half-hourly import | 13,765 | 34 | 412,950 | 4,130% | 2,065% |
| 5 | two cars, dual rate, 13.5 kWh | 13,771 | 40 | 413,130 | 4,131% | 2,066% |
| 9 | IOG, heavy export | 13,872 | 141 | 416,160 | 4,162% | 2,081% |
| 17 | car + IOG, Agile-like, heavy export | 13,801 | 70 | 414,030 | 4,140% | 2,070% |
| 19 | negative half-hourly, 20 kWh, heavy charge | 13,815 | 84 | 414,450 | 4,145% | 2,072% |

Polling is the same in every scenario, 13,731 calls a day:

| Category | Calls/day | Where it comes from |
| -------- | --------: | ------------------- |
| register_read | 11,520 | the full register sweep, 80 registers × 144 sweeps. It runs every 10 min because `polling_mode` is on for a non-EMS site (the log's sweep reason says so) |
| status | 720 | every 2 min |
| meter | 720 | every 2 min |
| device_info | 720 | every 2 min (`communication-device`, which returns every device on the account) |
| device_list | 24 | hourly device refresh |
| evc | 24 | hourly device refresh (EV charger list, even with no charger) |
| site | 2 | 12-hourly |
| account | 1 | daily |
| register_write | 34–141 | execute |

The sweep is 84% of the day.

**Start-up** costs 88 calls on a cold start: one full sweep (80) plus account, device list, EV charger
list, site, register list, status, meter and device info. A warm restart costs less. Settings under 10
minutes old, the account (under 24 h) and the site (under 12 h) are restored from storage.

**Writes** are 0.2–1.0% of the day today, but they matter once polling is cut. Two patterns stand out:

- **The export start time follows the clock.** In scenario 9, from 20:55 to 23:45 during an active export
  window, `DC Discharge 1 Start Time` is rewritten to the current time every cycle: 36 writes in under
  3 hours.
- **Eco mode and DC discharge toggle together.** Each change to whether Predbat is exporting writes both
  `Enable Eco Mode` and `Enable DC Discharge`. That is 12–64 writes a day across the scenarios (64 in
  scenario 9). Some of it is export starting and stopping as solar lifts the SOC back over the export
  target.

**A hazard found while building the harness.** When a write is clamped by the register's range (for
example a `Battery Discharge Power` range below the charge-rate sensor Predbat takes its discharge rate
from), the read-back never matches. Execute's write-and-poll then retries about 10 times every cycle,
which is about 2,880 extra calls a day. Real GivEnergy hardware reports matching charge and discharge
rates, so the baseline models that. But a mismatch between what Predbat writes and what a register
accepts is expensive under a call limit. The harness test `test_no_write_retry_loop` guards against it.

**Against the direction already chosen** (estimated from this baseline, not yet measured):

| Item | Calls/day |
| ---- | --------: |
| status and meter every 5 min | 576 |
| a twice-daily read of the registers Predbat uses (assuming about 25 registers) | ~50 |
| device info daily, plus site, account and the hourly device and EV charger list refresh | ~52 |
| **Before writes** | **~680** |
| **With this baseline's 34–141 writes** | **710–820** |

That is 21,400–24,600 a month: around the 20k target and well over 10k. After the sweep, the next
levers are:

- the status/meter interval;
- the hourly device and EV charger refresh (48/day for an account with no charger);
- the two write patterns above.

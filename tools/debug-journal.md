# Debug journal

Notes distilled from maintainer debugging sessions and triage-bot runs on this repo (June–November 2026), written for whoever is triaging an issue next.

Every entry is an observation from a past investigation, not a statement about current main. The code moves. Use these as "look here first", confirm against the working tree before you put anything in a comment, and say what you actually confirmed rather than what this file says.

The file was compacted on 2026-11-05, from 730KB of per-issue detail down to the durable facts and pointers. If you need the full story behind an entry, the issue or PR it cites has it, and so does the last uncompacted version: `git show ea6ba38d:tools/debug-journal.md`.

## Replay the reporter's debug file

`predbat_debug.yaml` is a full state dump the harness replays against the current tree:

```bash
cd coverage
./run_all --debug_file <scratch>/predbat_debug.yaml > <scratch>/replay.log 2>&1
./run_all --debug_file <scratch>/predbat_debug.yaml --redo > <scratch>/replay-redo.log 2>&1
```

- Grep the log first for the non-default config block (`- <item> = <value> (default <value>)`). Output: `coverage/plan_orig.html`, `plan_final.json`.
- Without `--redo` you replay the dump's own rates, load model and Octopus slots. `--redo` recomputes them, and is the retry when an older dump crashes with `KeyError: 0` near `pv_forecast_minute` (GH#4766).
- A replay cannot reproduce live ML-forecast choices, so a mismatch in marginal ~10p slots is inconclusive (GH#5213); grep the reporter's log for `Best export window` / `Best charge window`.
- The triage bot may only run `tools/triage_test.sh <test>`: replay via a temporary `TEST_REGISTRY` entry calling `run_single_debug(..., redo=True)`, which also re-derives `set_rate_thresholds()`/`rate_scan_window()` (a dump stores end state, GH#5221).
- `Replay input:` log lines (PR #5427, 2026-10-12) rebuild a cycle's inputs from the log alone (formats pinned in `tests/test_log_replay_inputs.py`). The external parser (PR #5363, open) makes `log_replay_*` a cross-repo contract; unknown lines are skipped silently. On main a repeated identical line means the value changed (open PR #5591 would change that).
- Reading a dump directly (GH#5509): rates and windows use absolute minutes of the dump day, `predict_*_best` minutes from now. `export_window_best[].target` is not the `export_limits_best[]` level; decode with `export_mode_of()`/`export_target_of()`.

Loader failures before Predbat code runs:

- `could not determine a constructor for the tag '!secret'`: `DebugYamlLoader` has no `!secret` constructor (GH#5748); sed captured args to dummies in a copy.
- numpy dtype `SystemError: bad argument to internal function` (v9.1.0+ dumps, GH#5205): fixed in PR #5892 (2026-11-01).
- `found unhashable key` from `io_slot_log_gate` (v9.3.8, GH#5569): fixed in PR #5575 (2026-10-17).
- Without `apps/predbat` on `sys.path` a load dies at the first `!!python/object` tag it cannot import. A newer dump on an older tree fails with `No module named 'control_monitor'` (GH#5719); null `control_monitor:`/`control_ledger:` in a copy.

Normal lines, not the bug: `Prediction kernel stale binary ... - using Python engine` (plan still correct) and `Config item ... is below the minimum ... - clamping to ...`.

## Debug yaml can contain real secrets

Read dumps locally and never quote a raw block publicly unchecked.

- Pre-v8.55.0 dumps can carry `args_from_apps_yaml` with resolved secrets (GH#4765); masked at source since PR #4910, so no reason to tell a current reporter to rotate.
- `mask_secret_args()`/`is_secret_key()` match `_key`/`password`/`secret`/`token` in top-level keys. `annual.py`'s `scrub_secrets()` is recursive and masks differently (GH#4770).
- `debug_history_enable` defaults True and snapshots ~3-hourly into `predbat_debug_history.tgz`, independent of `switch.predbat_debug_enable`.
- Logs are redacted at write time (`LogRedaction`, `log_secrets.py`).
- `!secret` resolves only via the private `SecretYamlLoader`. Bare `yaml.load(...)` hits in the tree are ruamel, not PyYAML.
- Issue line numbers may resolve against a PR branch, not main. If `gh api .../pulls/<n>/comments` is denied, use `gh pr view --json`.

## Check configuration before code

Pull the non-default list first: `combine_export_slots`, `metric_min_improvement_export`/`_plan`, `best_soc_keep`, `metric_battery_value_scaling`, `metric_battery_value_export_scaling`, `metric_battery_cycle`, `metric_cloud_enable`, `load_scaling*`. "Configuration" is a legitimate outcome.

- Replay one setting change at a time and compare decisions, not the metric (an emptier final battery always looks cheaper). `best_soc_keep: 0.0` and `metric_battery_value_export_scaling: 0.8` are defaults.
- Read `optimise_all_windows()` end to end before calling a pass dead. The commented-out swap call is not the live one, which runs last (GH#4478).
- Near-parity export tariffs (GH#4840): leftover SoC is credited at `battery_value_rate()` (replacement cost), a ~1% margin, so `metric_battery_value_scaling` below ~0.987 flips exports. `metric_cloud_enable: False` predicts zero import.
- Charge-to-export "loss" (GH#5069): naive arithmetic omits the end SoC credit and displaced peak house load.
- Non-GBP tariffs (GH#3683, open 2026-11-05): `metric_min_improvement*` gates compare unconverted against the minor unit, with pence defaults, so plans churn. Scale them by the unit ratio.
- Money-losing plans: grep `Previous plan best metric is ... and new plan best metric is ...`. Positive deltas under `metric_min_improvement_plan` mean retention (GH#5616); negative deltas every cycle mean lock-in on stale windows, which lowering the gate cannot help (`should_replace_plan()`, GH#5686).
- Levels-pass exports that paid only in the PV10/PV90 blend, which `metric_min_improvement_export` could not remove and the write-backs restored (GH#3376/#5633, GH#5969): fixed in PR #5977 (2026-11-05, `export_gate_metric()`). Per-5-minute charging came in PR #5874 (2026-10-31).
- `best_soc_keep` is only a soft `metric_keep` penalty, never a window or reserve (GH#4961). `manual_soc_keep` and alert keeps (default 100 per alert) force `keep_minute_scaling >= 10` weighted by import rate, so are inert in 0p windows (GH#5162). Only `set_reserve_enable` plus `reserve` is a hard floor.
- `HoldChrg` (within 1% of the limit, `set_discharge_during_charge` default True) is not `FrzChrg` (`is_freeze_charge()`).
- No charge-start hysteresis (GH#2513): the hold band needs `inv_target_soc_percent < 100.0`, so a 100% target is re-commanded every cycle.

`get_arg()`/validation traps that look like a feature silently off:

- `key:` (null) defeats the default for direct `get_arg()` callers (component args fixed since PR #5188, 2026-09-24). A quoted `'1'`/`'y'` coerces to False.
- Float-default truncation, e.g. `solcast_poll_hours: 4.8` read as 4 (GH#4925), was fixed in PR #4938 (2026-10-08).
- An enable-gated CONFIG_ITEM still returns its declared default while gated off (`get_ha_config()`).
- Unknown apps.yaml keys are ignored silently (GH#4999, open). `allowed:` is enforced only for string and integer types.
- Sensor-typed items reject numeric scalars (`not of type 'sensor'`); use list form (GH#2172).

## Test-order pollution (GH#5079)

- Since PR #5102 each registry test gets its own `create_predbat()`. Module- and class-level state can still leak (`PLOT_ENABLED`, `LogRedaction`'s lock).
- From `coverage/`: `./run_shuffle --hunt --quick` bisects to a (culprit, victim) repro. Also `--bisect <seed>`, `--campaign N`, and `./run_all --shuffle [--shuffle-seed N]`.
- Victims cluster in `model`/`kernel_parity` (tight assertions). Before fixing, check the culprit really touches the victim's state.
- `run_pre_commit` runs the quick suite only after the hooks pass, so a log ending at the hooks is not a green suite.

## The shared clock is rewound for ~0.8s every hour

`calculate_yesterday()` sets `midnight_utc` back a day and `minutes_now = 0` on the shared `PredBat` (`# Fake to yesterday state` / `# Save state`), unlocked, while other threads read it live. Present on main 2026-11-05.

- Off-thread readers are hit: manual overrides (GH#4900, fixed with `manual_time_origin()`) and component PV buckets (GH#4804). `ComponentBase` derives its clock from `now_utc` since 2026-09-13/14; `PredBat` mixins such as `Octopus` still read the fake.
- Code inside the fake must pass the real `minutes_now` to history indexing (GH#5004).
- Suspected, unverified (code-read 2026-10): `decode_octopus_slot()`, `dispatch_billed_off_peak()` and zero-kWh slot synthesis misread it (GH#4946). These affect History only.
- On a "shifted by one day" report, check this first. New off-thread code should derive time from `now_utc`.

## Per-integration notes

Grep for the named symbol rather than trusting a line number. Each area lists durable facts and "look here first" pointers, one per bullet.

### Fox (`fox.py`)

- Cloud errno 42015/44096 = setting unsupported by the device (`FOX_SETTINGS_UNSUPPORTED_ERRNO`); it is marked unavailable and never polled or written again. WorkMode is a select, ExportLimit a number.
- `soc_max` 4x too big when `batteryList` holds four `bmu` entries with one serial (AIO ESS, EVO 10-5-H): fixed in PR #5629 (2026-10-24). Unverified: whether `batteryDesignCapacity` totals a multi-pack stack.
- `fox_automatic: true` re-`set_arg`s `soc_max` every cycle and it is not in `CONFIG_API_OVERRIDE`, so apps.yaml cannot override it; escape hatch `fox_automatic: false`.
- With the Mode Scheduler deleted, `compute_schedule()` shows the charge window from the legacy `forceChargeTime` read, which Predbat cannot clear (`set_battery_charging_time()` has no production callers; GH#4939, re-checked 2026-11-05).
- A Fox Cloud `didn't complete got X` is a genuine failure, but the cloud verify read is slow: old value at 3s, new one after ~18s.
- Reserve never reads back from the device (GH#5821, open), so "Current reserve is X%, already at target" is Predbat echoing its own write. Fox accepts `minSocOnGrid: 100` yet still discharges: declared ranges are acceptance, not enforcement.
- Device MaxSoc is published (`number.<prefix>_fox_<sn>_setting_maxsoc`, writable) but never bound to the planner (GH#5395, open); `automatic_config()` binds only `soc_max`/`reserve`/`battery_min_soc`/`export_limit`.
- ForceCharge overshooting its target (`fdSoc: 100` on ForceCharge groups): fixed in PR #5623 (2026-10-24).
- FoxESS HA/modbus path (`FoxESS`, not `FoxCloud`): the stock template binds `reserve:` to `number.foxess_min_soc_on_grid`; at 100%, `min_soc_on_grid` stops grid-connected discharge. `FoxESS` is `has_idle_time: False`, so `idle time is ...` programs nothing (GH#4961).
- Hold drain-block (GH#5704, open, verified 2026-10-23): `execute.py`'s hold-above-target branch calls `adjust_charge_immediate(..., freeze=True)` even for non-planner holds, so `charge_freeze_service` selects `"Back-up"`, which supplies no house load. Workaround: remove `charge_freeze_service`.
- `ForceDischarge` in a `Fetch scheduler V1 returned` list does not prove a discharge window was written.
- Tests: `./run_all --test fox_api --test fox_oauth`

### Solis (`solis.py`)

- CID 636 (`SOLIS_CID_STORAGE_MODE`, Modbus 43110) is a bit mask. On "4B and above" firmware (`is_tou_v2_mode()`: CID 6798 reads 43605) per-slot registers (43707) alone drive timed charging and TOU bit 1 is refused, so CID 636 verify warnings are that refused bit, not a failed write (GH#4707).
- Do not infer V1/V2 from one read-back (GH#4774); keep the post-write `read_cid()` cache overwrite, which lets change detection retry. V2 writes the mode with slot 1, V1 at the window boundary (`claim_window_mode_assertion()`, once per window).
- A CID 636 report with no `setting storage mode to` lines came from the HA select handler `set_storage_mode_value()`, not `write_time_windows_if_changed()`.
- PV-only detection: `_reports_no_battery()` and `SOLIS_GRID_TIED_MACHINE_PATTERN` (`^S\d+-GR\dP`); Cloud alternates `batteryType` `'0'`/`'0.0'`, so match by value (`_reads_zero()`).
- Discovery beats apps.yaml (`overwrite=True`, grep `auto-discovery wins for this setting`) for `charge_limit`, `export_limit`, `battery_rate_max`, `battery_scaling`; only opt-out `solis_automatic: false`. Discharge capped at the charge limit: fixed in PR #5308 (2026-09-29).
- Slot current cap (`slot_current_limits()`): CID 7224/7226 as read, else `SOLIS_SLOT_CURRENT_DEFAULT_AMPS` (60), lowered only by `--probe-ceiling` (GH#5068). The planner does not see the clamp; set `inverter_limit_discharge`.
- Verify echoes: the slot-current entity republishes the clamped cache at live voltage (`didn't complete got 3094.0` every cycle) and CID 5965's slot-SOC readback tracks live SOC (GH#4184); grep `CID 5967 ... is set to` for what reached the inverter.
- Still live (2026-11-05): `write_time_windows()` defaults a never-read slot current to the cap, so no write is attempted; seed `cached_values` in fixtures.
- Parallel pair (GH#5556, open): the master's V2 slot current is the group's, but 7224/7226 are per-inverter, so the pair under-delivers; PR #5909 (`solis_parallel_slaves`) unmerged.
- API: `B0115` (datalogger offline) and `R0000` (200/day allowance gone) are never retried (`SOLIS_API_CODES_NO_RETRY`); `automatic_config()` retries every cycle; `_with_retry()` catches only `SolisAPIError`.
- Still live (GH#5093, open): `SolisAPI.run()` folds all inverters into one `poll_success`, so one unreachable unit holds startup in backoff (to 128 min); list working SNs in `solis_inverter_sn`.
- Still live (GH#5190, open): `int()`/`float()` accept `_`, so `"1234567890_00"` passes `parse_cid_int()` and `get_arg()` coercion; start here on "absurdly huge value, fallback never fires".
- Holds (PR #5265): `reserve` is CID 157, a floor only while CID 636 bit 4 (`SOLIS_BIT_BACKUP_MODE`) is set, so the bit is always set. Non-slot events wait for the next `run()`, beyond the 2s verify.
- Older-log signatures: `export limit 0.48kW` (CID 499 100x small, PR #5625, 2026-10-24); `poll failed for <SN>: '<SN>'` (`KeyError` repr, PR #5733, 2026-10-24); `battery_scaling read as 0.0` every cycle, plan unaffected (warns once since PR #5841, 2026-10-29).
- `H M` types (`GS_fb00`, most cloud subtypes) rewrite time entities every cycle by design (#1529): first suspect for "hammers my Solis with writes".
- Test trap: `number_event_handler()` takes the SN from `parts[0]` of the entity id; use underscore-free mock SNs (`SN0EXPORT1`).
- Tests: `./run_all --test solis`

### Solis Modbus (`GS`, `GS_fb00` in `inverter.py`)

- The Energy Storage Control Switch (Modbus 43110, `energy_control_switch`) is written by `alt_charge_discharge_enable()`, gated on `has_solis_energy_control`; `GS` reaches it via `mimic_target_soc()` (35/33).
- `GS_fb00` (PR #5258): `write_solis_energy_control()` runs from `adjust_charge_immediate()` (49 charge/idle, 17 freeze/hold) or `adjust_export_immediate()` (64 freeze export, 49 export); a third writer would flip the switch twice a cycle. Before PR #5256 (2026-09-26) a switch left at 1 held charge slots at 0 W.
- Bit 4 (Battery Reserve) makes `backup_mode_soc` (43024) a discharge floor. A SolisCloud session leaves the switch at 1 (`ENUM_SELF_USE_NO_GRID_CHARGING`) outside a slot. FB00 plugin names differ (`SOLAX_SOLIS_MODES_FB00`: Self-Use is 33).
- `set_current_from_power()` re-asserts `timed_*_current` on every rate call (PR #4437); `inv_charge_discharge_with_rate` zeroes only the power register. A 2600 W plan rate is `battery_rate_max_raw`'s fallback (GH#4198 family). Open: #3914, #3868 (HV battery amp conversion).
- Modbus-path types (`GS`, `GS_fb00`, `SX4`, Sofar) have no component: control args come only from the template, and `create_missing_arg()` only dummies, so a missed `charge_limit` logs `No entity_id for charge_limit` on every target change (GH#5129).
- solax_modbus mode tables (`SOLAX_SOLIS_MODES`/`_NEW`) only compute 33/35, which mean different things in each; write the table-derived name, never a number. Feed-in priority is cloud-component only.
- The hold lever is window-scoped (GH#5152, open): only `set_current_from_power()`'s `timed_*_current`; `battery_discharge_current` is never driven.
- `GS` with the plain Solis plugin on V2/FB00 firmware writes 35, reads 33 every cycle and never exports; use the FB00 plugin and `GS_fb00` (`templates/ginlong_solis_fb00.yaml`). Cloud credentials without `solis_automatic: True` make `solis.py` fight the modbus path.
- Tests: `./run_all --test inverter` (`solis_fb00_*`, `solis_gs_*`)

### SolaX (`solax.py`)

- Code `10402` (token/auth) is retried in-request by `request_wrapper()`; keep that retry. Every cycle while data flows is server-side token invalidation (suspected: a second client on one `solax_client_id`), not data loss; count `error 10402` against `Fetched ... successfully`.
- SolaX clamps minimum SOC at 10% (`SOLAX_MIN_RESERVE_PERCENT`), so `battery_min_soc` is auto-configured.
- `No solar forecast data was returned from HA sensors` with the SolaX Cloud template means no PV forecast source: `fetch_pv_forecast()` falls through to HA sensors, whose regexes need the Solcast HA integration.
- Plant `totalYield` is PV plus every inverter's AC output, so pre-PR #5357 (2026-10-04) logs on mixed plants double-count discharge; `pv_today` is now `get_plant_pv_yield()` (GH#5356). Device `dailyYield` reads 0 once the inverter sleeps.
- Plant lifetime counters can dip and recover within a day; `hold_counter_dip()` holds them (`SOLAX_COUNTER_DIP_HOLD_HOURS`; GH#5388, open). Third-party PV is invisible to SolaX (no `M2` read), so `total_load` under-reads; an X1-AC-only plant warns `has no inverter with PV inputs`.
- `plant_inverters` is keyed by the raw plant ID; entity names use the lowercased form.
- With no battery `ratedPower`, `get_max_power_battery()` uses the inverter rating for `battery_rate_max` (PV-only model codes 4, 16, 18, 22, 26, 27, 28, 29, 44 excluded); unknown models can overestimate mixed plants. Never infer a battery from model name or power.
- Tests: `./run_all --test solax`

### Sigenergy (`sigenergy.py`)

- Lifetime/history totals reset around EU midnight; `fetch_history_totals` clamps monotonically. Start here on "totals went backwards overnight".
- Stock `SIG` is deliberately `has_reserve_soc: False`, so the planned reserve is model-only (GH#5358); an apps.yaml override bypasses the reserve guards and `adjust_reserve()` writes `number.sigen_plant_ess_discharge_cut_off_state_of_charge` (GH#4728).
- `Applying mode=eco ... duration=720min` comes only from the no-window fall-through. Still live (GH#4894, open): with `sigenergy_automatic: False` writes land on dummy entities, and `fetch_controls()` runs only under `if first:`, so one missed event leaves state out of step for good.
- Before PR #5377 (2026-10-04, v9.3.5) the six optional power/priority fields were sent outside `commands[0]` and ignored, so no power lever reached SIGCLOUD hardware (GH#5376); a test locking in current output made suite-green meaningless.
- Docs freeze automations zeroed `grid_import_limitation`, blocking Sigenergy EV chargers: fixed in PR #5464 (2026-10-12), with the rated-power sensor names (plant `..._charging_power`, inverter `..._charge_power`; GH#4911/#5459).
- Export limit (PR #5681): `antiReflux` off → 99999, on → `gridMaxBackfeedPowerW`; cached; `overwrite=False`. Each MQTT connect re-subscribes `openapi/change` (PRs #5813/#5815); MQTT 3.1.1 hides a refused subscription.
- Two SoC floors (PR #5723): `backupCutOffSOC%` → `battery_min_soc` (plan floor), `dischargeCutOffSOC%` → reserve (execution only). The `set_reserve_min` self-heal follows the plant up, never down.
- Operating-mode writes (MQTT `openapi/instruction/mode`, REST settings PUT) have no observed effect while onboarded (author-reported, PRs #5692/#5724); only offboarding releases. Stuck in VPP is not a Predbat defect.
- MQTT `period` data can lag ~2 h while arriving every 5 min; freshness is `statisticsTime` (`mqtt_data_lag`; PRs #5757/#5790). Messages carry only changed fields, so merge. Never set `last_mqtt_update` from `statisticsTime`: via `inverter_time`, `check_clock_skew()` would restart the app.
- Still live (GH#5784, open): `_get_battery_max_power_kw()`/`_get_inverter_max_power_kw()` read only the REST nameplate (MQTT `inverterMax*ActivePowerW` is unread), so plans sit under real capability.
- Onboarding: invalid system ID (code 13001) reports `invalid_system_id` since PR #5844 (2026-11-01); mistyped and unapproved IDs look alike until the onboard POST, so read `Response from POST /openapi/board/onboard`.
- `code=1201 Access restriction for /openapi/system`: that endpoint lists every system on the app key and Sigenergy throttles it globally (once per five minutes, per its docs), so instances sharing a key collide. With `system_id` set, `discover_systems()` confirms the system from its own device list and never reads the system list. A `1201` or any other error retries, and must never fall through to onboarding.
- An app key not authorised for a system gets HTTP 403 on that system's device list, not a Sigenergy error code; the system is then treated as not onboarded and `onboard_systems()` runs, whose per-item `codeList` says why (verified 2026-10-20).
- Device-list `ratedEnergy` is kWh (9.04 per module), but an older response gave Ah, so `_get_battery_capacity_kwh()` treats only values above 100 as Ah. A capacity that reads tiny (45.2kWh as 1.3kWh) is that conversion (verified 2026-10-20).
- Tests: `./run_all --test sigenergy`

### AlphaESS (`alphaess.py`)

- The Open API has no forced-export, working-mode or dispatch endpoint, so Force Export exports only solar surplus and Freeze Export equals Demand. Recommended mode: `Control charge`; forced export needs local Modbus.
- Once any discharge window is configured (`ctrDis=1`) firmware allows discharge only inside it; `execute.py` has no AlphaESS case, so an early-programmed window bars house load until it opens (GH#4723, open).
- `batUseCap` below 10 was rejected with undocumented errno `10001`; floored at 10% in PR #5838 (2026-10-29, GH#4748).
- Hold is an enabled 10% target at 100 W on a stable daily window (`ALPHAESS_HOLD_SOC`/`ALPHAESS_HOLD_POWER`), not zero rate; below 10% SoC it is a standing 100 W charge request (untested on hardware).
- `snap_time_grid()` snaps times to a select's step.
- Tests: `./run_all --test alphaess_control`

### Ohme (`ohme.py`)

- A vendored, version-pinned copy of `dan-r/ohmepy` (`VERSION` inline in `ohme.py`; upstream in `ohme/const.py`, so diff both). A `File "/config/ohme.py"` traceback is the vendored client, not the HA integration.
- 404s on `/v1/chargeSessions/...`: Spring `No static resource` = withdrawn route (old versions, GH#4719); `Entity of type: 'ActiveChargeSessionDB' ... not found` = no active session, so every pause 404s (GH#5444, open). Main controls via the `car_charger_control.py` mixin (PR #5448).
- Still live (GH#4952, open): `slot_list()` energy is `watts * hours` with `watts` a per-slot cap (2.55x overstatement measured); `estimatedSoc` is unread. Slots publish as `AT_HOME` dispatches without `source`, so `rate_add_io_slots()` prices them low. Existing tests pin this.
- Since PRs #5401/#5405 (2026-10-05), with `ohme_control` off Ohme's own schedule becomes the car plan (`reprice_charger_schedule_slots()`), and slot ownership checks Octopus's live Intelligent devices (`is_ohme_charger()`), which tariff-only cases in `test_ohme.py` do not test.
- `CONNECTED_STATUSES` excludes FINISHED, so with `ohme_control: true` an attached idle car reads unplanned and stays paused (GH#5444; plugged-in-idle mode unverified). Workaround: `ohme_control: false`.
- Tests: `./run_all --test ohme`

### Solcast (`solcast.py`)

- `max_kwh` is 9999 on the Solcast direct and HA-sensor paths (set only for Forecast.Solar/Open-Meteo), so "Raw forecast exceeds the array ceiling" never fires for Solcast (GH#4730, open, re-checked 2026-11-05).
- `fetch_pv_forecast()` order: Forecast.Solar with `forecast_solar_open_meteo_first` → Forecast.Solar (backup on empty data only) → `open_meteo_forecast` → Solcast direct → HA sensors. So `open_meteo_forecast` means Solcast is never used, and `solcast_api_key` beats `pv_forecast_*` sensors.
- Solcast direct has no fallback (GH#5545, open): a failed call returns the stale cache, so data is never empty and the plan ages; a backup must key off `solcast_last_success_timestamp`.
- `sensor.<prefix>_pv_forecast_h0` state is calibrated; raw is the `now` attribute. Since PR #5121 calibration reads `..._pv_forecast_h0_uncalibrated`. `history_attribute(attributes=True)` drops points lacking the key unless `fallback_to_state=True`.
- P10/P90 with calibration off: fixed in PR #5347 (2026-10-02, v9.3.4). Open-Meteo applies ensemble P10/P90 as ratios to the deterministic P50, so a re-issued deterministic forecast can push `pv_estimate90` implausibly high, bounded only by `1.2 x kWp`.
- Dull-day calibration inflated by per-minute `dp2()` rounding: fixed in PR #5802 (2026-10-28, GH#5690). On "small values misbehaving", grep `dp2(`/`dp4(` in accumulation loops first.
- Test property: each future day's calibrated total is raw × `days_use_scaling` to ~1% (`calibrate()` in `tests/test_pv_calibration_clipping.py`).
- A 0-PV day is a calibration down-day; ≤2 valid days switches calibration off, told by `Not enough valid (non-down) days for calibration` (AC-coupled netting, GH#5552).
- Open-Meteo has no `horizon` parameter; Predbat has only monthly `shading_factors` (`gti_hourly_to_period_kwh()`).
- Test traps: the solar mock matches the first key that is a URL substring (`api.open-meteo.com` is inside `ensemble-api...`); patch `solcast.history_attribute_to_minute_data`; `now_utc_exact` is wall-clock while `minutes_now`/`midnight_utc` are frozen (`patch_now_utc_exact()`).
- Tests: `./run_all --test solcast`

### HA write/verify (`ha.py`, `inverter.py`)

- `get_state(refresh=True)` is a no-op in a normal add-on install (re-reads only when `not self.ha_key`), so verification reads the websocket cache.
- `write_and_poll_value()` treats `sensor.*` as Predbat-owned and POSTs `/api/states/<entity>`, so a control bound to a sensor "succeeds" with no integration write. Compare the dump's live `args` with `args_from_apps_yaml` (GH#4738).
- `call_service_template()` checks the result and logs `was not accepted, it will be retried next cycle` (PR #4878); most other `call_service_wrapper()` sites discard it. Six of the eight `adjust_*` writers notify/MQTT regardless of the result (GH#4845, open).
- `write_and_poll_switch()` builds `domain + "/turn_..."` from the entity's domain, so a `select.*` gets `select/turn_on` and retries every cycle; `switch_state()` reads only `on`/`enable`/`true` as on. Hits `inverter_mode` (GH#4909; PR #5647) and `scheduled_*_enable` (GH#5637), all open. Workaround: a template switch.
- Times are not snapped to a select's options, so 15-minute selects (SA/Deye) retry an impossible value (GH#4947, open). `write_and_poll_option()` strips seconds only after a healthy `HH:MM` state, so `unknown` gives `didn't complete got unknown` (GH#5009, GH#5214).
- Changing `charge_time_format` from `"HH:MM:SS"` swaps entities for `sensor.predbat_*` dummies, severing window control (`Creating dummy entity sensor.predbat_{TYPE}_{id}_charge_start_time`); time entities prefer a wired entity since PR #4745. A capability flag set false does the same for other args.
- False alarms: `didn't complete got X` with X the previous target is HA's cache; a value between old and target is a settling bounce (GH#5130, ~44s on solax_modbus). Verify does up to 10 doubling reads (~40s GS, ~100s GE). Trace the value from the `adjust_*` caller, not the arg name.
- Without `discharge_stop_service`, `adjust_charge_immediate()` falls back to `charge_stop_service` with `domain="discharge"`; on Tesla hooks that is discharge to load, then hold ~2s later (GH#5094, open). Define `discharge_stop_service`.
- `execute.py`'s reserve reset (`adjust_reserve(0)` on `resetReserve`) has no isCharging guard, so a hook using the reserve as its charge signal loses the race (GH#5300, open). A reserve read of 0 with `already at target` means a dummy arg.
- Service-controlled charging that overshoots its target tries `charge_freeze_service` before `charge_stop_service` since PR #5541 (v9.4.1).
- `self.control_monitor` (PR #5563, v9.4.1) publishes commands the battery did not follow as `predbat.control_failures` (24h count, `control_failures_total`, newest 20 events). Read it first on any "didn't charge/hold"; small 24h against a large total means chronic (GH#5748).
- Tests: `./run_all --test inverter`

### GE Cloud (`gecloud.py`)

- Cloud fields come back null; `merge_non_null()` keeps the last good value, so a failed poll can feed stale SoC/power to the plan.
- `number_event` clamps out-of-range writes silently, but `write_and_poll_value()` compares with the pre-clamp target: false `had_errors`, endless retries (GH#4826). Only `adjust_reserve()` clamps (GH#4831, open).
- Integer `3HY` ratings (`GIV-3HY-11`) fell back to `max_charge_rate` until PR #5293 (2026-09-30), so read the model string first. `inverter_limit` comes from `_max_inverter_rate` and clips PV. Still live (2026-11-05): `battery_rate_max` binds only `_max_charge_rate`; `max_discharge_rate` is unread.
- Mixed percent/watts fleets gated rate reads/writes on fleet-wide key existence (`No entity_id for charge_rate to write`, GH#5359): fixed by `Inverter.rate_controls()` (d70125a2, 2026-10-13).
- `site/{uuid}` limits are `{'enabled', 'power': {'watts', 'amps'}}`: only `enabled` is a restriction, a disabled limit is the declared capacity, none is null. `GET /v1/site` returns every site's limits and inverters in one call.
- Every `/settings` entry becomes an entity, so a register missing from the dump was never listed by the API (GH#5017).
- `python3 apps/predbat/gecloud.py --api-key <key> --write-entity number.probe_nothing 0` lists every register and writes nothing, though startup still runs `enable_default_options()` on the real inverter.
- Clobber trap: a missing-feature `set_arg(key, None)` deletes apps.yaml values; still on main 2026-11-05 for `pause_mode`, `pause_start_time`, `pause_end_time`, `discharge_target_soc`, `givtcp_rest`. `set_arg_auto(..., overwrite=False)` lets apps.yaml win.
- Test trap (GH#5056): mock `set_arg`s store `None` rather than deleting (`test_ge_cloud.py`, `test_ohme.py`, `test_alphaess_api.py`); populate `args_from_apps_yaml` and assert the manual value survives.
- GIV-3HY has no eco/mode register, yet `GEC` sets `has_ge_eco_toggle: True`, so `No entity_id for ECO Toggle` every cycle (GH#4909, open).
- On GIV-3HY `charge_power_rate` limits only grid charging. Since d70125a2 (2026-10-13, GH#5082) a `grid_charge_only` attribute on the rate entity disables freeze export fleet-wide; check it first on "freeze planned but battery charged".
- 3-phase "Charging but imports nothing": an external writer clearing `enable_ac_charge`; fixed in PR #5270 (2026-09-27, `ensure_ac_charge_gate`).
- EMS sites need slot 1 at 00:00–23:59 (#3781). Settings are re-read hourly and `check_ems_inverter_slots()` warns (PR #5109); a settings cache under 10 minutes old skips the startup read.
- An option label containing `(` crash-looped the component (`too many values to unpack`): fixed in PR #5218 (2026-09-26).
- `refresh_discovery()` only rebuilds the catalogue; `refresh_devices()` calls the API hourly, skipping devices silent over 5 days.
- `enable_default_options()` writes unused slots 2–10 as `00:00`, but on GIV-3HY `00:00-00:00` is live for a minute, so a forced midnight charge fires them; it also overwrites user-set spare slots (GH#5371, open).
- Two retry layers (GH#5777): a 200 carrying a device code (-1/-2/-5/-6) is data to `async_get_inverter_data_retry()`, which retries only `None`; the settings functions retry codes, then log `Device <serial> Failed to read inverter setting id ...`.
- `-1` is a server-side device timeout (rate limiting is HTTP 429); EMS settings are read every block, so `code -1` bursts are normal. -3/-4/-7 never succeed on retry.
- Literal `{setting_id}` braces in logs are Predbat output from before PR #5800 (2026-10-28).
- Tests: `./run_all --test ge_cloud`

### Teslemetry / Powerwall (`teslemetry.py`)

- Since PR #5276 (2026-09-28), all `overwrite=False`: `inverter_limit` from `nameplate_power`; `export_limit` from `min_site_meter_power_ac` (±1e9 = none, 0 = a real 0 W); charge 5 kW per unit, capped at nameplate; `inverter_hybrid` on for PW3 only (`teslemetry_hybrid`).
- The whole TOU schedule is pushed every cycle, so a Tesla-app edit reverts next cycle (GH#4600, GH#4610). Every `set_tariff()` sends `optimization_strategy: "economics"`.
- `teslemetry_automatic` gates only `automatic_config()`; the scheduler emulator writes for every non-read-only user, and only `set_read_only` stops it.
- TBC is the default since PR #5188 (2026-09-24): a signal tariff from `build_signal_tariff()` (0/50/100p bands), mode `autonomous`, reserve as a real reserve, so "app shows 0p/50p/100p" is normal. `MockTeslemetryAPI` defaults to `tbc_control = False`.
- Non-TBC: `build_tariff()` carves one flat-priced ON_PEAK interval, so Tesla defers the export to the window's end (GH#4887, open). A slow reserve-driven charge is firmware, not a write bug (GH#4892).
- `_settable_reserve()` passes 0–80 and 100 and rounds 81–99 up to 100; its docstring and `set_backup_reserve()`'s disagree on which way firmware snaps that band. Device value: `hold_reserve_percent()` (execute.py), then the component's `schedule_reserve` remap.
- `adjust_reserve()` floors a hold reserve at `reserve_percent` (the user's floor). `can_freeze_charge` is False when `soc_kw < reserve`, sending TESLA into hold-charging.
- Hold reserve ratcheting up 1% per cycle at day rate (62→63→64…): fixed in PR #5823 (2026-10-28, v9.6.2) by `reserve_above_soc_charges_from_grid`; EnphaseCloud sets the same flag, so Enphase "imports during a hold" is that behaviour.
- A Powerwall can stall silently while `site_info` reads correctly (GH#5157); `run()` re-asserts the device tuple every `FORCED_ASSERT_SECONDS` (2h), bounding "plateaued until restart" at ~2h.
- Freeze export (GH#5225, open) is disabled only by `support_discharge_freeze: False`; enabling it also needs a device hold at the current SoC via `_settable_reserve()`. Other cloud emulators probably need both (suspected).
- `update_battery_export_permitted()` (b37f25af, PR #5710, 2026-10-27): any `components.net_meter_mode` other than `battery_ok` means installer Solar Only and turns export planning off fleet-wide; `teslemetry_battery_export` overrides.
- TESLA and EnphaseCloud have `can_span_midnight: False`: a 23:30→05:30 band commits as 23:30–23:59 and re-commits after midnight, so the night's first signal band is partial (GH#5748); `plan.py` does no midnight splitting.
- Log decoder: `Replay input: inverter changed {...}` = held window or reserve changed; a `..._time_of_use_settings` POST plus `schedule_write` press = an `apply_schedule()` commit. The tariff is never read back, so a cloud overwrite is invisible.
- Under TBC one PW3 charged at ~1.67 kW against a 5 kW limit, so a charge window can end below its starting SoC (see `predbat.control_failures` under *HA write/verify*). `HoldChrg` on the plan is the predicted fill-then-hold.
- Service-hook (`rest_command`) installs: a wrong-domain `charge_freeze_service` turns a hold into a grid charge, and write checks only echo Predbat's dummy entities (GH#5775, closed 2026-11-01); check the wiring first.
- Tests: `./run_all --test teslemetry`

### Sunsynk / DEYE (`sunsynk.py`, `deye.py`)

- Freeze export is gated by the per-slot power register (`sellTime{n}Pac`), hardware-confirmed; the energy mode alone does nothing.
- DEYE telemetry keys have one hardcoded spelling each (`DEYE_TELEMETRY_KEYS`): a SUN-8K-SG05LP1-EU-AM2-P sends `TotalDCInputPower` + `DCPowerPV1-3`, not `TotalSolarPower`, so PV reads 0 with `missing expected keys [...]` every cycle (unit-equivalence unverified).
- Sunsynk exposes the TOU block twice (live, 2026-09-19): `volt1-4` = `sellTime3-6`/50, `volt5-10` = `sellTime1-6Pac`/10, `volt11-12`, `current1-4` = `sellTime1-6Volt`×10. Read them to prove a write landed; never write them (`SUNSYNK_SYSTEM_MODE_FIELDS`).
- Since PR #5360 (2026-10-05, v9.3.6) the payload cache lasts 15 minutes but ownership (`control_active`) 8h. To v9.3.5, `control cache is X minutes old (limit 15), forcing a rewrite` meant nothing rewrote it for hours (GH#5349); newer `(limit 480), requiring a fresh write` means ownership expired.
- `has not applied Predbat's settings after N settings polls` = a write the cloud accepted but the dongle never collected; it triggers a re-apply.
- The write-button comment "presses this on every cycle" is wrong: both press sites in `inverter.py` are change-gated, so nothing is pressed after a restart with a matching plan.
- A hold is the TOU slot cap `SoC+1`; a `discharge_rate 0` entity write is symbolic. Windows cannot cross midnight, so they are written to 23:59 and re-rolled.
- On M3.3.8.9, `totalPower` (`load_power`) read 0–100 W while the battery was active, while `dailyUsed` kept counting (GH#5193, open, cause suspected).
- Every planned export writes `sysWorkMode=selling_first` (`derive_control_state()`); whether the mode or the slot sell flag governs battery export is unsettled. Load args use `overwrite=True`, so only `sunsynk_automatic: False` opts out.
- `battery_rate_max()` derives watts from BMS current (280 A → 14.3 kW on an 8 kW inverter) and `inverter_limit_charge`/`_discharge` are never set, so the BMS figure is written (deye.py too); set them in apps.yaml (GH#5238, open).
- "Planned export didn't happen, battery charged" (GH#5460, dup of #4865): replans moved the window later and the idle gaps filled the battery with PV first.
- Log conventions: displayed grid = −raw `pac`; battery + = discharge. A repeating SoC is the 15-minute cloud poll. Trust SoC and the `etoday` counters, not spot power balances.
- Parallel master/slave groups not modelled (half the metered import/export, doubled `soc_max`): fixed in PR #5596 (2026-10-18).
- Tests: `./run_all --test sunsynk_control --test deye_control`

### Grid sign / arrow direction

- `grid_power_invert` is set by some integrations (Teslemetry: True) and not others; with two systems one integration's True bleeds into the other's entities and inverts the arrows. Set an explicit False in both automatic configs.
- Tests: `./run_all --test sunsynk_config --test teslemetry`

### Enphase (`enphase.py`)

- The Enlighten endpoints are unofficial. MFA accounts cannot log in; export control needs discharge-to-grid schedules; writes need a double-submit CSRF token or return 403; using the Enphase app concurrently can trip session limits.
- Tests: `./run_all --test enphase_api`

### myenergi (`myenergi.py`)

- `myenergi_automatic_zappi` (PR #4997) gates the Zappi half of automatic config; off, Zappis are monitor-only and `charger_control_active` is never set.
- A Zappi held in Stopped stayed Stopped after a restart with a control prerequisite off: fixed in PR #5793 (2026-10-28) via `charger_control_release_inactive()`. Not re-verified: whether the control switch stays unpublished until the gate latches.
- Published entities are never removed (upsert only), so a switch a gate stops publishing stays frozen until HA restarts.
- `set_arg`/`set_arg_auto` values survive a component-only restart (`Components.restart()` keeps `base.args`), so auto-wired `car_charging_*` args persist until the process restarts.
- Tests: `./run_all --test myenergi`

### Wallbox (`wallbox.py`)

- Sign-in works with `User-Agent: Predbat`. Capture: `wallbox.py --username <email> --raw`.
- An OCPP-run charger (e.g. Octopus IOG) reports `operation_mode: "ocpp"` and sits in 209 Locked; unlock does nothing, and pause/resume 403/409 even for a super-admin, so a 403 does not prove missing rights. `held_by_lock()`/`held_by_ocpp()` keep control off it.
- `GET v3/chargers/<id>/ocpp-configuration` returns the charge-point password: never paste `--get` output unredacted. A `POST` to the same path, writing the whole stored configuration back with only `type` changed (`set_ocpp_mode()`), switches OCPP off (`"wallbox"`) or on.
- `locked` is 1/0 and `state_of_charge` always null. Charger N is car N through the append-only `car_order`.
- Status ids (OCPP Pulsar Plus, Oct 2026): 209 no car; 210 just plugged in; 164/181 plugged in, including full; 161 brief no-car blips. 0 Disconnected means off Wallbox's cloud (even mid-charge), not unplugged.
- On the shared `CarChargerControl` loop since PR #5798 (2026-10-28); records drop only on `EMPTY_STATUS_IDS` (161/162/165/209), no longer on 0. "Stayed paused after control was turned off": read `CarChargerControl`'s release path.
- Unverified on hardware (Oct 2026): pause/resume on an unlocked non-OCPP charger, the charging/paused ids, 429s at a 120s poll.
- Force hold (Pulsar Max on Intelligent Octopus over OCPP, live 31 Oct 2026): Octopus `updateDeviceSmartControl` SUSPEND does not stop the charger, so the hold is OCPP off then pause. A pause sent before a read shows OCPP really off is accepted but ignored, so `charger_control_force_pause()` sends only the switch until then; paused reads 182 at 0 W. PR #5900.
- The cloud status lags the charger by minutes (Wallbox and HA both kept showing `Charging`): trust `last_sync`, and expect a repeated pause to answer 409.
- Hand-back for an OCPP charger is the switch back on, then waiting for `connectionStatus` 4 (about 2.5 min); Octopus restarts the charge itself, so never send a resume. The switch answered 502 once while the paused charger had not synced; the retry worked.
- By hand: `wallbox.py --ocpp on|off` and `--force-hold <minutes>` (Ctrl-C hands back). The CLI saves nothing, so recover a killed `--force-hold` with `--ocpp on`.

### Hypervolt (`hypervolt.py`)

- V3 `get.session` pushes carry no `session` key, and `watt_hours` drops to 0 when charging stops (Home 3 Pro, fw 4009); `apply_session` resets `session_energy_kwh` when `watt_hours` falls, without which `car_manual_soc_track` missed charges.
- Tests: `./run_all --test hypervolt`

### Charger control (`car_charger_control.py`, shared mixin — Hypervolt, Wallbox and Ohme-class components)

- With `<component>_control: true`, `charger_control_dispatch_active()` follows the raw `octopus_intelligent_slot` while planning reads `octopus_intelligent_planned_slot`, so a raw dispatch beats a filtered plan (GH#5807, open; PR #5816 open).
- The raw follow is deliberate for `octopus_intelligent_charger_follows_car`; a fix must keep it.
- `is_charger` is published only by Predbat's Octopus/Kraken components; for other dispatch sensors `charger_control_dispatch_is_charger()` returns None and `_control: true` means "drive anyway", with no stop-only veto. First question: which sensor `octopus_intelligent_slot` points at.
- The planned-slot fallback rule (arg unset, list too short, non-string, unresolved `re:`, equal to the slot sensor, or `octopus_intelligent_slot_action_config`) lives in `car_planned_slot_entity()` and, under #5816, the mixin; change both together.
- Tests: `./run_all --test car_charger_control`

### Hanchu Cloud (`hanchu.py`, `hanchu_const.py`)

- Added in PR #5305 (2026-10-20), largely unverified on hardware; #5664/#5665/#5667/#5668 are from one live-hardware reporter, so search them first.
- Power limits (PR #5684, 2026-10-22), highest priority first: user override (`hanchu_battery_rate_max`/`hanchu_inverter_limit`), the `HANCHU_MODELS` rating for the `devModel` code (codes are not in size order), the `power_ceiling` ratchet, then the menu range.
- `CHG_PWR_LMT` limits grid charging only, so freeze export is never requested. A hold caps `DTU_AC_CHG_SOC_LMT` at the current SoC (`_hold_grid_target()`).
- Floors and caps: `CHG_BAT_SOC_LMT` ≥ 50 (read, never written), `DTU_AC_CHG_SOC_LMT` ≥ 20, `DSCHG_BAT_SOC_LMT` ≤ 45, so a hold below 50% or a discharge target above 45% cannot be set. A hold is a 0 W charge slot.
- One batched `iotSet` per change; a partial accept is never cached; overlapping writes fail with error 300004. The one-hour rolling hold slot is deliberate.
- HC-HY-SL-*-G2 is missing from the `devModel` table, so it always takes the fallback.

### Growatt Cloud OpenAPI (`growatt.py`, `growatt_const.py`)

- PR #5666 still open at 2026-11-05, no component on main; from code review only: `etoUserToday` is read as grid import into `load_today`; daily UTC `HH:MM` slots emit a degenerate `00:00–00:00` at UTC midnight; SoC limits are a software gate (`_gate_reason()`); stale telemetry is silent.

### Gateway MQTT (`gateway.py`)

- Acks (PRs #5220/#5231, 2026-09-26): `_subscribe_acks()` subscribes to `predbat/devices/<id>/ack/+`; with acks seen, an identical command goes once per 30s plus one re-send. Ids are `PBAT<n>`; PR #5172 (uuid ids) is open, so its symbols are not on main.
- Empty-serial slots with battery telemetry are still bound, giving `select.predbat_gateway__...` entities and `dongle_serial required` rejects (GH#5227, open).
- A status with no inverters still returns before `_inject_entities()` unless `gateway_inverter_serial` is set (checked 2026-11-05). Symptom: "EV sensors froze / component unhealthy".
- Probe trap: the `battery.ByteSize() > 0` filter hides data-less slots from `automatic_config()`'s topology; build the exact visible set (`TestGatewayUnitControlBinding`).
- Unreadable-at-discovery inverters stayed unconfigured until PR #5475 (2026-10-11); GH#5478's fabricated 6000 W rates were fixed too (`_last_rating` keeps the last real value; rate writes are held).
- Slot windows (PR #5559, 2026-10-16) stage in `_staged` (600s) and commit via the `schedule_write` switch; `not written to the Hub` comes from `_commit_failed()`. A disabled EMS slot keeps publishing its window, so matching selects do not prove it is armed.
- PR #5620's freshness gate (`cannot plan or control the battery`) is still open, so not on main.
- EV planning needs `_connected` plus Preparing/Charging/SuspendedEV/SuspendedEVSE/Finishing, so a hub reporting `Available` after a stop leaves later windows unplanned (`Car N charging is not planned as it is not plugged in`). PR #5670 closed unmerged; #5699 open but superseded.
- EV control (PR #5739, 2026-10-27) is a stateless reconcile: starts in-window, stops any out-of-window session (manual ones too) by design; 3 attempts 120s apart; car 1's charger only.
- `online` (PR #5713, 2026-10-24): `online`/`offline` payloads are accepted. Only a status with a readable slot stamps health, so an all-slots-unread hub can restart a SaaS pod about every 8h.
- PV-only (PR #5805, 2026-11-01): `_auto_pv_only()` counts `pv_power` but not `pv_today`. A PV sensor stuck at 0 → check `connected`.
- Tests: `./run_all --test gateway`

### Octopus (`octopus.py`, `fetch.py`)

- **Intelligent Go / dispatches:** for "a cheap slot Octopus never offered", compare `rate_import_base` with `rate_import_no_io` at that minute in the dump: cheap in both = the integration's rate sensor; cheap only after `no_io` = `rate_add_io_slots()`. `io_adjusted` cannot tell them apart (GH#5012, GH#5663).
- With `octopus_intelligent_dynamic` on (default), `dynamic_load_car_check()` cancels the slots of a car in a dispatch but not seen charging, and re-prices them outside 23:30–05:30 (`in a dispatch but not charging`); check `car_charging_now` history, as `eco`/`boost` never equals `charging` (GH#5335, GH#5390).
- Confirmation counts any car charging (PR #5725) from 2 minutes into the half hour; sub-5-minute dispatches need a confirmed half hour (PR #5657). Without `car_charging_now`, a charge below 0.85 × `car_charging_threshold` (kWh/minute: `0.1` is 6 kW) cancels real dispatches (GH#5706, open).
- Completed dispatches stay off-peak only while the feed lists them, and applied rates are not persisted (GH#2785, open), so `today_cost()` and History re-price the past (GH#5413). `octopus_held_dispatches()` (PR #5533) bridges the gap (some accounts never populate `completedDispatches`, GH#5544). Planned dispatches are never promoted, by design. Keep the abs() in `decode_octopus_slot()`.
- `trim_started_dispatch()` moves a running dispatch's start to the poll time, so the tail re-prices as a short dispatch (GH#5702, open) and `dynamic_load()` under-subtracts car load (`Dynamic load adjust is setting load minimum`, GH#5762, open). A withdrawn dispatch keeps the feed discount (GH#5663, open).
- Cap: `has_six_hour_cap()` matches `IOG-SMB` only; since PR #5639 (2026-10-24) `octopus_account_slots()` keeps one budget per account, not per car (GH#5215). `octopus_intelligent_charging` off still stamps (GH#5065).
- A 200 carrying `errors` comes back as data (`null` = that part failed, `[]` = none); guarded for dispatches, devices and `savingSessions.account` (PRs #5493/#5750), not for null `events`/`joinedEvents` in `get_saving_session_data()`.
- Leaving an Intelligent tariff freezes the devices (`async_update_intelligent_devices()` still returns early; GH#5412 closed). `octopus_automatic: true` re-wires over a user's `octopus_intelligent_slot` every restart. Add-X% vs SoC targets: `intelligent_device_is_charger()` (PR #5822).
- **Saving and free sessions:** the args read event entities (`events` free, `joined_events`/`available_events` saving); Both ship disabled (calendar twins lack attributes), so "the sensor does not exist" usually means enable it (GH#5370, GH#5707). Log: `Octopus: free events code`.
- Saving-session value is credited to every minute with no baseline (GH#2090), but Octopus pays only above the baseline, so an already-exporting plan earns nothing (GH#5458).
- `eventType` alone splits Power Down from Happy Hour; the flexibility path drops it, so read `event.get("eventType") or event_type.get(eventId)` (PR #5727). The Happy Hour skip must follow the type-map build (`c0fb4e9c`; three check sites, GH#5615). A moved booking stays free (GH#5778, open).
- Free electricity comes only from `customerFlexibilityCampaignEvents` (`last: 50`, no pagination, no fallback); `Found N ... via flexibility API` shows what it holds (GH#5614). Saving sessions' `falling back to legacy query` = campaign endpoint empty.
- **Tariff and rate fetching:** with no `standard_unit_rates` link, `async_get_day_night_rates()` votes the off-peak window from TOU labels (`from measurement TOU labels`), on the meter's clock since PR #5794 (2026-11-01). Never swap pytz `localize()` for `replace(tzinfo=...)`.
- In `fetch_octopus_rates()` the last write wins, so after a switch a stale next-day event shadows today until the integration refreshes, silently (GH#5503, GH#5018). Dumps lack rate events: use the `min/max/average` log lines. A fixed product can renew under the same code.
- `filter_payment_method()` picks DIRECT_DEBIT from overlapping rows and must keep null-method rows (GH#5144). `self.mpan` is the import MPAN only; meters use `self.tariffs[direction]["mpan"]`. Compare reads only `standard-unit-rates` (GH#4921, open).
- **API and auth:** polls every 30 min (account), 10 min (events), 2 min (dispatch); a changed GraphQL body is logged in full. Migration to `api.oegb-kraken.energy`: the old domain is live without redirect; probe both with curl (GH#5786; PR #5799 open).
- Tests: `./run_all -k octopus_`, `./run_all -k saving_session`

### Kraken / EDF (`kraken.py`)

- EDF answers a day/night tariff on `standard-unit-rates/` with HTTP 400 (Octopus four-rate: 200, empty). 400/404/410 (`KRAKEN_REST_RATES_UNAVAILABLE_STATUSES`) trigger the GraphQL `applicableRates` fallback; standing charges only on 404/410 (GH#5166). Probe the URL from the log.
- `_fetch_rates_rest()` returns `(None, None)` on a network error, so `err is None` is not success; success is `(results, None)`. `async_graphql_query()` counts its own failures, so do not count twice (PR #5641).
- Standing charges: payment-method variant fixed in PR #5568 (2026-10-17, GH#5502), covering-row selection in PR #5737 (2026-10-24, `_standing_charge_covering()`). Validity helpers must compare with `datetime.now(timezone.utc)`; `str2time()` is tz-aware.
- REST rate rows arrive newest-first; the 3-page cap drops only the oldest history. The published `rates` attribute is window-trimmed (PR #5571). Provider entities (`sensor.predbat_<component>_...`) are outside the FAQ's recorder section (GH#5534).
- SmartFlex dispatches publish as `octopus_intelligent_slot`, so every Octopus dispatch rule applies; start E.ON/EDF "EV smart-charge cost wrong" there (GH#5521). `intelligent_dispatch_billing` `auto` on eon makes every in-force half hour off-peak, never cancelled (PR #5581).
- Auth: `oauth_failed` latches on credential rejection until re-init (repeating `Auth not available for <context>` with no mint attempt); a CDN/WAF block backs off. E.ON began rejecting email/password mints with HTTP 400 around 16 Oct 2026: use `kraken_auth_method: api_key`.
- A 200 response that is not a JSON object is treated as a failed fetch (PR #5641).
- EDF Flextras free hours (PR #5730, 2026-10-27): the `ui/hours` endpoint (token, no Bearer prefix), read every 30 min; UK wall-clock times despite the UTC label; only `SELECTED` slots count. Replay: `python3 kraken.py --flextras`. Kraken saving sessions are GH#5291.
- VAT: GraphQL `applicableRates.value` is assumed exc-VAT (unchecked against a bill) and scaled by `standingCharge / preVatStandingCharge`; GB domestic electricity is zero-rated 1 Oct 2026 – 31 Mar 2027 Whether the standing-charge fallback includes VAT is unverified.
- Tests: `./run_all --test kraken`

### Axle (`axle.py`)

- An export event boosts import as well as export, by design (PR #4520, like Octopus saving sessions; `docs/energy-rates.md`); `axle_pence_per_kwh` scales both and nothing disables only the import side. #5060 and #5175 are by-design answers.
- `load_axle_slot()` bounds only the event end (no `start_minutes >= 0` guard as in `load_free_slot()`), so a closed event writes dead keys at negative minutes (GH#5036, open); they are inert, as `rate_minmax()` and the window scans start at `minutes_now`.
- Event boosts run before user overrides (`apply_rate_overrides()`), so a full-horizon `rates_export_override` rewrites event minutes as `user` (GH#4277 family). Workaround: put the fixed schedule in `rates_export` as the base tariff.
- Forensics: `rate_*_replicated` marked `user` through the event window = boost ran, override clobbered it; no `saving` marks and no boost = it never ran. `rate_*_base` holds the tariff's own price.
- Tests: `./run_all --test axle`

### History fetch / memory (`ha.py`)

- History is fetched in `HISTORY_CHUNK_DAYS` chunks with boundary dedup; the chunking also multiplies REST calls enough to trip the fatal API-error counter at startup (see "Too many API errors" under *Startup, config and apps.yaml*).
- The largest memory peak is ML load-predictor training, not the plan.
- Tests: `./run_all --test history_chunking`

### Minute data (`utils.py`)

- `minute_data()`'s gap-fill writes `newest_state` beyond the data, which for `backwards=False` is the earliest sample's value, so deltas after the fill show a phantom step when the series ends lower than it started. Convert deltas per sensor before summing (PR #4756 review).
- Tests: `./run_all --test minute_data`

### Battery capacity estimation (`inverter.py` `find_battery_size()`)

- The estimator applies `battery_loss × inverter_loss` to every sample, right only for an AC-side `battery_power`; DC-side it under-reads ~4% at defaults (GH#5831, open, code-read 2026-10-29). Suspected, unverified: GivTCP's `battery_power` is DC-side.
- The 80–100%-of-nominal clamp (#3974, #3940) means `battery_scaling_auto` tracks only degradation below `battery_scaling`; a measured capacity above nominal cannot take effect.
- Tests: `./run_all --test find_battery_size`

### SoC sources (`inverter.py` `soc_percent`/`soc_kw` args)

- `soc_percent` wins everywhere; `soc_kw` is a fallback only when it is absent (live SoC, `find_battery_size()`, `find_charge_curve()` all branch on key presence), so with both set the kWh sensor is inert.
- Neither configured gives a silent SoC of 0.0 with no validation (`get_arg("soc_kw", default=0.0) * battery_scaling`).
- `soc_kw` has an `APPS_SCHEMA` entry and `soc_percent` none; neither is a CONFIG_ITEM, so a compare `config:` block cannot override either.
- SIGCLOUD `automatic_config()` rebinds `soc_kw` with plain `set_arg`, overwriting a hand-set apps.yaml `soc_kw` when the component runs.

### Charge/discharge curve (`inverter.py`)

- The curve is evaluated per target minute; tapering near ~93% SoC is expected.
- Still live (GH#5219, open): `find_charge_curve()` divides by `max_power` unguarded, so with `inverter_limit_charge`/`_discharge` bound to live BMS rate sensors, Predbat's own 0 W freeze-export write crashes every cycle with `ZeroDivisionError`. `adjust_battery_target_multi()` divides by the fleet rate too.
- One bad SoC sample stamping a low value across the learned curve: fixed in PR #5592 (2026-10-18).
- The learner runs for inverter 0 only (`if id == 0 and ...` in `execute.py`): `Inverter 1 Looking for ... curve` present means the gate is not the problem; absent while inverter 0's is present means it is.
- The learned curve is one fleet-wide value at combined fleet SoC (GH#5442); mixed-fleet deliverability is the same single-battery model (`battery_to_min` on fleet SoC, plain `+=` sums in `execute.py`) (dup-close to #4856, GH#5508, GH#5628).
- In auto mode, `Cannot find battery discharge curve (no full rate discharge cycle ...)` every 5 minutes is normal, not a fault loop.
- `battery_temperature_charge_curve`/`_discharge_curve` are apps.yaml-only keys checked by `validate_curve()` in `fetch.py`, not CONFIG_ITEMS (grepping `config.py` misleads); `battery_charge_power_curve`/`battery_discharge_power_curve` are CONFIG_ITEMS (GH#5630).
- `Sensor history data lengths: 0, 0, 0, 0` printed zeros whatever was missing until PR #5646 (2026-10-24).
- Tests: `./run_all --test find_charge_curve --test battery_curve_keys`

### Inverter time string (`inverter.py`)

- `Warn: Inverter N unable to read inverter time string X` is a format gap in `Inverter.__init__`'s parse chain; read the actual string first. `unknown` is the unavailability path (GH#3599), handled without restart on main.
- A failed parse leaves `inverter_time` None (no skew check), repeats the warn each cycle and fires `auto_restart("Unable to read inverter time")`.
- Test trap: a stale-dated time string parses, then trips `check_clock_skew` → `auto_restart`, which raises in the harness; probe with a near-`now_utc` string.
- On cloud/REST paths the inverter-time sensor advances only with the poll, so `check_clock_skew()` can measure data age, not skew (GH#5562, open).
- Tests: `./run_all --test inverter`

### LuxPower (no code — `templates/luxpower.yaml` only)

- No LuxPower code, `INVERTER_DEF` entry or component: support is template mapping only. The stock template targets LuxPython (`lux_*`); `lxp_*` entities come from the cloud integration. Start with what the user mapped and which integration publishes it (GH#5661).
- `adjust_battery_target()` floors every target-SoC write at the reserve, so `execute.py`'s 0% resets arrive at `set_reserve_min`, which LuxPower's per-register minimums can reject (umbrella GH#4831, open). Workaround: `set_reserve_min` at or above the device minimum.
- Tests: `./run_all --test inverter`

### Anker SOLIX X1 (`templates/anker_x1.yaml` template family, no code)

- PR #5677 (template, package and guide) still open as of 2026-11-05; the facts below come from it.
- Custom type `AX1` over GE defaults with charge/discharge freeze unsupported; one HA script (`script.predbat_anker_x1_control`) writes a signed setpoint in VPP mode, so for freeze/hold reports read its hold→auto conversion first.
- Integration afewyards/anker-x1-ha: power control needs ≥0.7.0; `grid_bought`/`grid_fed_in` are lifetime on firmware 1.0.19.16 but reset at midnight on 1.0.16.1.
- The X1 reports no house-load energy; `load_today` is a template balance of PV, battery and meter, so on "load history wrong today" ask whether HA restarted mid-day.

### Standalone / Docker (non-HA)

- Anything working under HA but not standalone: check the `ha.py` websocket and `userinterface.py` callback paths (GH#4601: a callback returning `None` instead of `True`).
- Tests: `./run_all --test trigger_callback_success_signal`

### Holiday mode (`fetch.py`)

- Under `days_previous_auto` (default) holiday mode lives entirely in `compute_load_forecast_history()`; the `days_previous = [1]` branch needs `days_previous_auto: False`. A slot without matching history falls back to `holiday_load_scaling` (0.7) (GH#4732).
- Load ML bypasses it: a non-empty ML forecast sets `load_forecast_only`, skipping the weighted-bucket forecast, and `load_ml_component.py` has no holiday handling (GH#5523, open). Any "Load ML ignores X" where X acts in `compute_load_forecast_history()` starts at `load_forecast_only`.
- Tests: `./run_all --test holiday_mode`

### Predheat (`predheat.py`)

- No control path: the only outbound call is a read-only `weather/get_forecasts`, `smart_thermostat` only pre-empts a setpoint rise in the simulation, and it is its own object, so `plan.py` never sees a heat decision (GH#4848).
- `load_forecast: - predheat.heat_energy$external` feeds modelled heat to the plan as fixed load, on top of history and Load ML (`fetch_extra_load_forecast()` sums all sources); feature asks are usually wiring (GH#5483).
- `get_weather_data()` passes the forecast `temperature` raw, so a °F weather entity makes the internal forecast run away while energy stays 0; check `temperature_unit` (GH#5153, open).
- The thermostat bands are swapped against `docs/predheat.md`: `run_simulation()` engages on a `hysteresis_off` (0.1°) dip and stops at `hysteresis_on` (0.5°) above. Open PR #5767 fixes it.
- Two routes to the 20.0 °C fallback target, both fixed: an unset `heating_energy` meter pinned `minute_data_age` to 0 (PR #5867, 2026-11-01); `EntityHistory.prune_history()` dropped the state-in-force record, so an `unavailable` blip floored age to 0 for 24 h (PR #5837, 2026-10-29).
- Traps: `run_simulation(save="best")` publishes HA entities, so a parameter sweep overwrites the sensors; Predheat reads `datetime.now(local_tz)`, so build history mocks against wall-clock time.

### Load ML (`load_ml_component.py`, `load_predictor.py`)

- The 2-hourly retrain is a curriculum of passes (`ml_curriculum_step_days`/`ml_curriculum_max_passes`, hardcoded), a 16–40 minute CPU spike every 2 h (GH#3896, GH#5213). `threads` reaches only the C++ kernel and nothing caps BLAS threads, so NumPy may use every core.
- On "CPU regressed after upgrade", reconstruct from the log when fine-tunes ran under each version; a version window under 2 h proves nothing (GH#5213).
- `load_ml_retrain_interval_hours` (PR #5433, 2026-10-12; default 2, max 48) is age-based, not clock-aware. Past `ml_max_model_age_hours = 48`, `is_valid()` returns `"stale"` and ML predictions fall back to empty.
- `load_ml_enable`, `load_ml_source` and the retrain interval are component/apps.yaml args, not CONFIG_ITEMS.
- `is_valid()` can say "active" for a never-trained model (`model_initialized` is set before the first epoch), and an aborted curriculum leaves fresh stamps that suppress the staleness retrain (GH#5075; fix in open PR #5112).
- `PREDICT_HORIZON` is 48 h (576 steps) and the plan's load beyond it is exactly 0, as `load_forecast_only` skips the historical forecast (GH#5336, open); "over 48h" in `Generated N predictions` is hardcoded text.
- Each fine-tune can move the published 48 h total by double digits (GH#5561, open): bracket `Training successful` with the adjacent `Generated N predictions (total X kWh ...)` totals. The ML forecast replaces the load forecast with no blend; a quiet log is not evidence of health.
- Load exclusion knobs (`car_charging_energy`, `iboost_energy_subtract`, future `heat_pump_energy` #5445) reach ML-source users only if `load_ml_component.py`'s data prep subtracts the same: car already did, iBoost since PR #5829 (2026-10-29).
- Saved ML rows are prepped at fetch time and restored verbatim, so a data-prep change reaches only fresh minutes until old rows age out; `Database iBoost subtraction mismatch ... discarding` after an upgrade or switch flip is #5829's gate.
- Tests: `./run_all --test ml_training_perf`

### Savings & metrics (`output.py`, `predbat.py`)

- `savings_total_predbat` accumulates the unadjusted `saving`, while `savings_yesterday_predbat` publishes `saving_adjusted`; they can disagree in sign (GH#3894).
- The battery-value adjustment bills the day-end SoC level against the baseline, not the day's change, so a day exporting through midnight can be charged the full SoC gap.
- The cumulative totals have no plausibility guard or reset: `update_pred()` restores each via `load_previous_value_from_ha()` and adds the daily value once per date at ~01:05 (GH#5450, open). A manual reset must set `start_date` (the first day a #2844 build ran) in the same edit.

### Metrics dashboard (`predbat_metrics.py`, `web_metrics_dashboard.py`)

- "Card always shows 0": grep `apps/predbat/*.py` for the gauge name; hits only on the `_gauge()` definition and its `to_dict()` entry mean a dead metric (`set_to_current_time()` counts as a setter).
- The `solcast_api_limit`/`_used`/`_remaining` gauges have no set-site and never had one (GH#5638, open).

### GivTCP REST (`givtcp.py`, `givtcp_rest.py`)

- With `givtcp_rest` configured the component auto-configures the power keys over apps.yaml lists (deliberate `0` placeholders too); `givtcp_rest_power_ignore` opts out with an Info line (GH#4883).
- The component starts whenever `givtcp_rest` is set, and the stock `config/apps.yaml` ships it uncommented with example URLs, so non-REST template installs get a failing "GivTCP REST" component (GH#4993); comment out the block and any `givtcp_automatic:` line.
- `initialize()` builds one `InverterRestState` per entry unchecked, so a mapping indented into the list raises `TypeError: unsupported operand type(s) for +: 'dict' and 'str'` in `read_data()` with no index (GH#5101); tell: no `discovered N inverter(s)` line.
- `calibration_reported()` treats only `on`/`On`/`true`/`True` as calibrating, while GivTCP's detection is `Battery_Calibration != "Off"`.
- The `discharge_target_soc` write is always `int(reserve_percent)`, a hardware backstop; `discharge_target_unsupported()` guards only auto-config, so manual entity-list users hit the add-on directly. First workaround: drop `discharge_target_soc` from apps.yaml (GH#5132).
- A component withholding an entity has two gates, publication and claim, which can disagree; the Gen 2 guard needed both (PR #5716, PR #5747, 2026-10-24/25).
- REST publishes its full entity surface (~44 per inverter) whatever the HA integration provides; "Predbat created N duplicate GivTCP entities" is a feature request (GH#5178, open).
- Rule from PR #5216 (2026-09-26): a gate over `published_discovery` must not be judged in the cycle where `rediscover()` appended an index.
- `inverter_details()` returns `{}` on the Gateway/EMS shape (empty `Invertor_Details`, null `raw.invertor.serial_number`), so the detail sensors are skipped, freezing `inverter_time` and faking a clock-skew alarm; tell: serial sensor unknown (GH#5334). Open PR #5346 reads the top-level block.
- REST read-back tolerance is `write_tolerance_watts()/12` (charge) and `/25` (discharge) of GivTCP's reported max rate (2600 W fallback); a larger miss never converges and re-warns every retry (family #5324, #2882, #5386).
- Tests: `./run_all --test inverter`

### Compare (`compare.py`)

- Hardware overrides must include `battery_rate_max_export` (used by the export prediction); a "bigger inverter" scenario also needs `override_inverter_limit_kw` or it is inert (GH#4895).
- A negative "True cost" with zero import is the end battery credited at the replacement import rate (`metric_end - metric_start`), not phantom export. Export under `rates_export: 0` is forced PV clipping (GH#4881). Import/Export columns are whole-horizon totals (GH#2033).
- Compare passes `include_manual_api=False`, and a tariff's own `rates_import`/`rates_export` replaces live rates wholesale; per-tariff `rates_*_override` is the workaround.
- Entity ids are concatenated, so a `compare_list` id with `/` gives paired `POST /api/states/...` 404s (POST plus read-back GET). `comparisons.yaml` is never pruned against `compare_list`, so ask for that file (GH#5133, open; PR #5143 open).
- Stored tariffs are republished once per start, so "sensors for a deleted tariff come back" happens on restart, not every cycle.
- The compared cost excludes the standing charge (`enable_standing_charge` omits `"compare"`), though the page's "Actual" line includes it. A `config:` block overrides only CONFIG_ITEMS; other args drop with `Warn: Compare, config item X not found` (GH#5452, open).
- `rate_import`/`rate_export` are event-stamped (IOG, saving sessions, free, Axle, overrides); `rate_import_no_io`, `rate_export_base` and `rate_import_pre_saving` are different snapshots, so diffing live tables against a plain rate card differs at every event minute (PR #5645).
- An id-less `compare_list` entry crash-looped `run_all()` every 5 minutes until PR #5488 (2026-10-12); older tell: `Scenario complete for tariff:` every cycle.
- The Compare chart reads HA recorder rows of `predbat.compare_tariff_<id>`, one per local day; missing days are recorder or nightly-run problems, not chart aggregation (GH#5485).
- Recorder trap: any `include.entity_globs` or `include.domains` makes the filter default-deny, even with literal ids; `include.entities` plus excludes stays default-allow. `docs/faq.md` and `docs/compare.md` still recommend `entity_globs` (GH#5507, GH#5683).
- Tests: `./run_all --test compare`

### Car charging (`plan.py`, `fetch.py`, `execute.py`)

- `car_charge_slot_kwh()` returns raw slot kWh with no limit awareness and feeds the HTML, JSON plan, status sentence and slot attributes, while the prediction clamps: "car kWh shown but cost never moves" is display versus model. Only `octopus_intelligent_consider_full` trims volume (GH#4888).
- Since PR #5678 (2026-10-23) a car with a `car_charging_energy` counter, a plug sensor and no `car_charging_soc` auto-tracks manual SoC as `sensor.predbat_car_manual_soc_tracked[_N]`. Without a counter, `update_car_manual_soc()`'s write-back never resets (GH#4889).
- `update_car_charging_power()` sums every entity under `car_charging_power`; two per-car template sensors on one charger double count (GH#4879).
- A `car_charging_rate` list raised `float(list)` every cycle (car fell back to 7.4 kW) until PR #5383 (2026-10-04, `car_list_arg()`).
- `car_charging_rate` has several writers (`get_car_charging_planned()`, gateway `expose_config`, Octopus's rise-only `max(rate, current)`), so clamp where it is consumed (PR #5669 review).
- Car hold: the plan holds on `(car_load_scale > 0) and (not car_charging_from_battery) and set_charge_window`, with no live check, while execution needs an active slot with `kwh > 0` (GH#5146). Since PR #5640 (2026-10-24) the live hold is skipped only while that inverter really exports (GH#5612).
- "Hold for car" (carHolding, `car_charging_from_battery` off) only stops the battery feeding the car; it never blocks grid charging. No car parameter gates charging on EV SoC or price, so "charge only when X" is an enhancement (GH#4572).
- `plan_car_charging()` ranks slots by import price only (`window["average"]`), ignoring the export average (GH#5384, open).
- A `re:` arg matches the first matching entity only (`resolve_arg_re()` breaks on first hit); an alternation does not combine sensors (GH#5390).
- `car_charging_plan_time` caps the non-IOG horizon at the next ready time (`end = min(window["end"], ready_minutes)`), so ≤ ~24 h, and no select value disables it; IOG cars bypass it. Link GH#1172 and GH#3890.
- With no `car_charging_soc`, `car_soc` stays 0 and the plan re-books the full limit every cycle (defaults 100 kWh, 100%, 7.4 kW): with an EV configured, "grid-charges the house battery despite big PV" is this until proven otherwise (GH#3762, open).
- `short_textual_plan()`'s "Your car is currently charging." comes from `car_charge_slot_kwh()`, never `car_charging_now` (GH#3017, open). The shared per-car `planned` list was fixed in PR #5448 (2026-10-12, GH#5407).
- `octopus_charge_limit` as "% to add" was read as absolute SoC; fixed in PR #5822 (2026-10-29, GH#3034).
- With `octopus_intelligent_consider_full` on, unneeded future dispatch minutes lose the cheap rate (`octopus_surplus_minutes()`, PR #5403, 2026-10-05): "cheap rate missing at the back of a dispatch".
- Multi-charger index: myenergi sorts Zappis by serial, the Octopus component sorts Intelligent devices by device_id, never joined, so per-car `car_charging_planned`/`_now`/`_energy`/`_plugged` can read the wrong charger (GH#5726, open).
- Tests: `./run_all -k octopus_`, `./run_all -k car_`

### Excluded load (`fetch.py`, `web.py`)

- `car_charging_energy` is a generic excluded-load list (any incrementing kWh sensor, heat pumps too), subtracted from load history and gated on `car_charging_hold`; `car_energy_reported_load` is the CT-clamp flag. "Double-counted load" needs these, not a new mechanism (GH#5445).

### Web, MCP and Chat (`web.py`, `web_mcp.py`, `chat.py`)

- `html_plan_override` always returns success and `Hass.run_in_executor()` never reads its future, so an override-persist exception is swallowed silently (code-read, GH#3078).
- MCP OAuth (GH#4799, open): `client_secret_basic` is advertised but `oauth_token` reads only the POST body; `issuer` is the bare base, not `{base}/mcp`, with hard-coded `http://`. Changing `resource` needs a matching audience check (`http://{request.host}`).
- An unregistered method hits the `add_route("*")` catch-all, logging `Default route for <path> - 404` without the method, so a blocked OPTIONS preflight looks like a stray GET. MCP routes: `PredbatMCPServer.add_routes()`; PR #5622 (2026-10-24, GH#3807) added the missing preflight routes.
- CORS headers appear only on preflight and `/.well-known` handlers (no middleware), so a browser MCP client passes the preflight and then has the real response blocked.
- Chat: `type: openai` against Gemini breaks tool calls (fragments keyed on `index`; `thought_signature` not echoed; GH#4904, open). `ChatRequestError.friendly()` words every non-401 failure "OpenRouter returned HTTP N" whatever the provider (GH#5074).
- Header SoC/status icons read published entities (`binary_sensor.<prefix>_charging`/`_exporting` from `set_charge_export_status()`), not live state (GH#5125). Page CSS is inline in `get_header_html()`; override with `body.dark-mode .foo`, not inline `style=`.
- CSS traps (PR #5585): global `p { white-space: nowrap }`; `body.dark-mode button` paints every button red with `!important`. WebKit clips fixed popups inside `.menu-bar`, so make them siblings of the bar; only real WebKit reproduces "menu does nothing on iPhone".
- `resolve_arg()` catches only `KeyError` from `value.format()`, so a stray `{` or `{0}` in a value is suspected (not reproduced) to 500 `/apps`. The editor's dotted paths break on keys containing `.` or `[`.
- "MCP/get_status reports X as null/0" is usually the wrong resolver: `get_ha_config()` knows only `CONFIG_ITEMS` and silently returns the default for an apps.yaml arg (`num_cars` fixed in PR #5742, 2026-10-25); nothing pins these fields.
- Tests: `./run_all -k web_`, `./run_all --test web_mcp`, `./run_all --test web_chat`

### Plugins (`plugin_system.py`)

- "Plugin loaded but does nothing": check `Initialising plugin class (name-based): <X>`. `load_plugin()` takes the first name-sorted class ending `Plugin`, so the imported no-op base `PredBatPlugin` beats a plugin class sorting after it. Fix PR #4319 open.
- Plugin endpoints mount after `on_web_start`, but the nav in `get_header_html()` is hardcoded, so plugin pages are reachable only by URL (GH#5367, open).

### Self-update (`github.py`, `download.py`)

- The GitHub API calls are unauthenticated (60/hour/IP): behind CGNAT or a VPN they 403 and retry every cycle, and the listing error reaches only addon stdout (GH#4886). A phone-hotspot A/B is the quick differential.
- Picking `main` or a branch and having the release reinstalled is `auto_update` comparing the baked-in `THIS_VERSION`, not a bug. Warnings inside `download_predbat_releases()` repeat every cycle.

### Cloud / divergence modelling (`fetch.py`, `plan.py`)

- Before v8.55.0 neither `metric_cloud_enable` (PV10-only) nor `metric_load_divergence` (inert) can explain a plan (GH#4870). Review suspicion, unverified: an empty `pv_forecast_minute10` pins `metric_cloud_coverage` at 0.5.

### Load forecast & scenario hedging (`fetch.py`, `plan.py`)

- Under `days_previous_auto` the forecast is `compute_load_forecast_history()`, a weighted mean with built-in weights; `days_previous_weight` drives only the legacy comparison and divergence path (GH#5198).
- The downside hedge is PV10's `load_scaling10`; `compute_metric()` blends scenarios with `pv_metric10_weight`/`pv_metric90_weight` (0.15 each), so raising `load_scaling` alone adds no hedge. Load ML gives a point forecast with no band.
- Fresh-install gap (base load for unrecorded times of day) fixed in PR #5553 (2026-10-15).

### iBoost (`plan.py`)

- The modes are not exclusive, despite the docs: `iboost_smart` builds a plan whatever `iboost_solar` says, with no PV input, booking cheap slots up to `iboost_max_energy`, so solar diverts little (GH#5454, open). `iboost_rate_threshold` goes down to -10.
- Test trap: the smart tests use a flat rate and cannot expose averaging or priority bugs. As of Aug 2026 the duplicate-slot guard never fires (unverified since).
- Tests: `./run_all --test iboost_smart`

### Component auto-config gate (`components.py`)

- `Skipping <X> interface, missing required configuration` fires when any non-`shared_config` arg appears in apps.yaml; `solar` still warns on battery-only installs because the `pv_forecast_*` `re:` defaults ship uncommented (re-checked 2026-11-05).

### Component health (`components.py`, `component_base.py`)

- `count_errors` is a lifetime counter. `api_started` means `run()` returned truthy once (Gateway does so while unconfigured). `is_alive()` is the real health check (60-minute staleness).
- `record_status()` writes raw text as the HA state, and HA rejects states over 255 characters. A test of `update_status()` sees only the last `record_status()` message.
- Each component has its own thread and loop, so blocking compute stalls only that component and `run_timeout` never fires. Slow shutdown (all log Stopped and "Predbat terminated", the process lingers): find the `run()` that cannot return, load_ml first (GH#5075; PR #5112 open).
- On timeout, `start()` cancels the task without setting `api_stop`, and the next tick may start a second run.
- `Components.start()` waits up to 10 minutes per component, serially: restart delay ÷ 10 minutes = dead-component count (GH#5580; GH#5134, open).
- A `run()` exception skips the rest of that cycle and retries on backoff (first cycle: `api_started` stays unset), so report what was skipped, not a "crash" (GH#5584). `json.JSONDecodeError` slips past `aiohttp.ClientError` handlers.
- Stamping liveness on cache-only cycles hides a dead component, so override `is_alive()` (enphase, Kraken; GH#5582); an empty signed-in account should return True without stamping, as False costs 10 minutes per restart.
- Tests: `./run_all --test components`

### Run loop (`predbat.py`)

- The HA-interface guard is duplicated in `update_time_loop()`/`run_time_loop()`. Production triggers it via a websocket drop with `db_primary` off, but `test_time_loop_guard.py` tests only `ha_interface is None` (re-checked 2026-11-05).

### HA websocket socketLoop (`ha.py`, `hass.py`)

- Event handlers run on the socketLoop, so a handler awaiting a queued WS call deadlocks for 2 minutes. `run_in_executor()` returns inside a `with` block and blocks its loop.
- Never-sent signature: an all-None `Service call ... failed or timed out` after exactly 2:00, with no `Service call timeout for request id` line (GH#3460, open, code-read).

### Carbon intensity (`carbon.py`, `fetch.py`)

- The API publishes up to ~48h ahead, often far less, and returns `{}` with HTTP 200 past the horizon; `prediction.py` scores uncovered minutes as zero carbon, so `best_carbon` swings (still on main, 2026-11-05). Matters only when `metric_carbon` is non-zero.
- Tests: `./run_all --test carbon`

### Storage cache expiry (`storage.py`)

- Expiry is checked against the real clock and expired files are deleted, so an expiry built from Predbat's `now_utc` is born expired under replay or tests (GH#5079). Run a fuzzer victim alone, at more than one time of day.
- Tests: `./run_all --test plan_persistence`

### DB manager / DB engine (`db_manager.py`, `db_engine.py`)

- Opt-in paths (`db_enable`/`db_primary` default off). `set_state_db()`'s `utcoffset().seconds // 3600` is wrong for negative or half-hour offsets (GH#5693; PR #5698 open as of 2026-11-05).
- `db_engine` lowercases ids on read only. The read-back key is `last_updated` with a trailing `Z`.
- Tests: `./run_all --test db_manager`

### Manual override selects (`userinterface.py`)

- Option lists are built in three places (`manual_times()`, `manual_rates()`, `api_select_update()`); change all three.
- Elapsed slots are pruned every cycle, so past cheap rates re-price at base (GH#5517, open). A dump never shows past selections; ask the reporter.
- `get_override_time_from_string()` rolls forward only bare `HH:MM`, so a same-weekday earlier `"Thu 07:00"` decodes to the past and drops (GH#5832, open; coordinate with PR #5729). PRs #5078 and #5425 are open.
- Tests: `./run_all --test manual_api`

### Marginal rate sensors (`marginal.py`)

- Plan-relative predictions (+1/2/4/8 kWh injected), refreshed only on plan publish. Bands use `rate_min_base`/`rate_max_base` since PR #5827 (2026-11-01).
- With a base minimum ≤ 0, cheap never turns on (GH#5826, open). `marginal_plan_cost()` duplicates `compute_metric`'s battery_value term (PR #5756), so edit both.

### Manual rates (`fetch.py` `basic_rates()`/`rate_replicate()`)

- A rule ending `:59:59` leaves the window's last minute at 0p (`Import rates: min 0`); end rules at the next rule's start. Unmatched minutes take `metric_future_rate_offset_*`.
- `day_of_week` beyond tomorrow was fixed in PR #5173 (2026-09-24); tests must look past tomorrow.
- Tests: `./run_all --test basic_rates`

### Manual load adjust (`fetch.py` `step_data_history()`)

- `manual_load_adjust` is additive, floored at zero, and marked `±` in the plan HTML (GH#5176). Free-event load gets baked into history (GH#4942, open).
- Tests: `./run_all --test load_today_comparison`

### Status health (`output.py`, `predbat.py`)

- A false "(unhealthy)" on Python ≤3.10: `last_updated` is written with `%z` (no colon), which `is_running()`'s `fromisoformat()` rejects before 3.11 (GH#5181, open, unchanged 2026-11-05). Fix the writer to `.isoformat()`; a `str2time()` swap loses naive-string tolerance.

### Futurerate / AgilePredict (`futurerate.py`, `fetch.py`)

- `rate_replicate(use_future=True)` fills live import/export, so the `*_base` tables, compare recognition (`is_live_published()`) and the IOG strip value include forecast fills (PR #5738).
- AgilePredict: `GET https://prices.fly.dev/api/{REGION}/?days=N&high_low=false` (REGION in `ABCDEFGHJKLMNP`, N ≤ 14; `&export=true` for export) returns one region object of half-hour `{date_time, agile_pred}` entries from the current half hour; mixed timezones across DST; cached ~180 min.
- `futurerate_calibrate()` makes published ≈ real within the fitted window whatever the VAT/discount constants, which only affect projection beyond it, and no offline test guards them; a flat formula change outside `all_range` skews the future (PR #5731, 2026-11-02).
- Tests: `./run_all --test futurerate`

## Symptom → first place to look

What a report or log shows, and where to start. Where a symptom belongs to one integration, the bullet points at that area's notes above.

### Startup, config and apps.yaml

- **`Error: You have not set load_today or load_forecast in apps.yaml` then `Error: Exception raised ValueError` every cycle** — a bare `raise ValueError` in `fetch_sensor_data()` aborts the cycle. Cloud components set `load_today` at runtime, so during their startup delay check the Components panel. Open 2026-11-05. (GH#5092)
- **`Error: timer_tick caught exception: <X>`, status `Error: Exception raised <X>`, "stopped planning"** — an exception escaped `update_pred()`; find the raiser from the payload. `ReadTimeout` is not a `ConnectionError`, a hole in any `except ConnectionError` around a bounded request (PR #5801). (GH#5743)
- **`Failed to decode response <Response [404]>`, `Error: Too many API errors, stopping`, `Web Socket failed to reconnect`; "stopped but running", `/api/ping` 500** — `HAInterface.api_call()` latches `fatal_error` after 10 consecutive REST failures, reachable at boot by history chunks. Open: #5134 (10-minute `wait_api_started()` hangs), #5354 (the latch), #5437 (startup 502s). (GH#5354, GH#5744)
- **`Cannot initialise Home Assistant Interface` (empty message), repeating `HAHistory: No HAInterface available`, or silence after a restart** — `Components.initialize()` swallows the constructor's bare `raise ValueError`: one attempt per process, then 600 s phase-0 waits A 502 flood on `supervisor/core/api/websocket` is HA core restarting. (GH#5437, GH#5806)
- **predbat.log shows only `Loading secrets from ...` / `Loading apps.yaml` pairs every ~20 s, never `Predbat: Startup`** — apps.yaml failed to parse, usually an unknown tag (only `!secret` is registered); the error is only on the add-on console log. (GH#5530)
- **`Invalid value None in element predheat.heat_energy$external` / `Unable to load the load forecast ... predheat.heat_energy$external`, no `Predheat:` lines** — `predheat:` must sit inside `pred_bat:`; bundled sub-apps read via `self.args.get(domain, {})` ignore a top-level section. (GH#5002)
- **I set a config item above its maximum and it stays capped** — a `CONFIG_ITEMS`/`APPS_SCHEMA` `max` clamps every source, apps.yaml included (`Warn: Config item ... clamping to ...`). (GH#5070)
- **"Predbat reports a GivEnergy error but I use Solis / Fox / GE Cloud"** — `inverter_type` is `GE` until component auto-config sets it (tell: `Warn: ... inverter_type is not set in apps.yaml`). Self-heals. (GH#4990)
- **`Validation ... is not a list, but requires N entries based on num_inverters` plus `Return bad int value [<list>]` every cycle** — the `entries: num_inverters` check disagrees with a `get_arg()` reader lacking `index=` (PR #5651 open). Unindexed list reads default with `had_errors`. (GH#5369)
- **`Error: Inverter N unable to read charge window time - no source is configured`** — deliberate with no `charge_start_time` arg and `inverter_source_active()` False. On Tesla, configure the Teslemetry component or `templates/tesla_powerwall.yaml`. (GH#5804)
- **`apps.yaml has N errors` naming a `*_today` accumulator "is not a valid type float", a few times a day** — `validate_config()` reads live states, so an `unavailable` accumulator fails; `transient_ok` covers only car charging items (checked 2026-11-05; GH#5421 closed). (GH#5421)
- **`apps.yaml has N errors` naming Predbat's own constant-valued `sensor.<custom-prefix>_<type>_<id>_*` placeholder** — fixed in PR #5845 (2026-11-01). (GH#5242)
- **An inverter drops out of the plan after an upgrade on a mixed fleet** — start at the vendor `automatic_config()`'s `set_arg_auto(..., overwrite=True)` writes (grep `auto-discovery wins`), not `execute.py`. GivTCP auto-config now only raises `num_inverters`; Workaround: `givtcp_automatic: False`. (GH#5029, GH#5209)

### Plan: charging and holding

- **Battery charges or freezes when it shouldn't** — rule the plan out first. A planned freeze is a charge window whose limit equals the reserve %. On an all-loss tariff, holding full is correct. (GH#4890)
- **Battery pinned at 100% while the house imports at peak** — `Completed run status Demand` every cycle, zero `Inverter N count register writes` and a flat SoC mean something outside Predbat refused the discharge; check the integration's reserve/min-SoC binding. (GH#4961)
- **`Hold charging target X%-Y%` while importing at peak, or a peak-price charge window** — the high-price gate (`best_price_charge_level`) guards only windows whose limit is still 0. Without `set_reserve_hold`+`set_reserve_enable` (or `inv_has_timed_pause`) "Hold charging" is an armed-window fallback; `not yet in charge freeze, holding target SoC at 100%` is a deliberate pre-window write. The suffix is SoC–target, not start–end. (GH#2716)
- **Plan shows `HoldChrg -> N%` / "charging to N%" while the slot charges higher** — the plan text classifies from `charge_window_best[n]["target"]`, the predicted end SoC when a charge cannot complete. Open 2026-11-05. (GH#4445)
- **Weather alert active and the battery sits at its current SoC while importing ("my keep is ignored")** — the alert keep (default 100) is a floor the optimiser defends, not a hold (see "Check configuration before code"). (GH#3200)
- **"Why does Predbat discharge to the house during cheap hours?"** — a model gap: every minute outside a charge or export window is ECO in `predict()`; only EV slots (`car_charging_from_battery: false`) and iBoost (`iboost_prevent_discharge`) hold the battery there. (GH#5122)
- **Reserve raised to keep the battery full (`set_reserve_min` 100) and it never charges — `Filtered charge windows []`** — the charge scan needs levels strictly above the reserve, so a reserve ≥ `soc_max` (or `best_soc_max` below it) leaves only "off"; a fix must also cap `reserve_percent`, as a limit at the reserve is a kernel freeze. (GH#4766)
- **Plan shows a flat hold at the reserve but the battery drains past it (Sigenergy to 0%)** — `has_reserve_soc: False` keeps the floor model-only (`Note: Inverter does not support reserve - disabling reserve functions`), so only the inverter's own cut-off or the export-hold poll stops it. See the *Sigenergy* notes. (GH#5358)
- **First cheap window charges to 0% / the charge target lands later than the cheap band** — a metric tie: the SoC scan adopts on a non-strict `<=`, and with `metric_battery_cycle` at 0 a charged kWh scores as its stored credit; neither `metric_min_improvement` nor `metric_battery_cycle` fixes it. Open 2026-11-05. (GH#5069)
- **Overnight charge stops at "just enough", or starts late and stops short** — pruned pv10/pv90 hedge slots; fixed in PRs #5721/#5735 (2026-10-24/25). `debug_enable` logs `Prune dead` / `Prune kept hedge` / `Prune trade-off` per slot. With 0p export, stored kWh earn `metric_battery_value_export_scaling` (0.8), so 100% is not the optimum.
- **Overnight charge stops short when a free electricity session sits later in the horizon** — by design (it bridges to the 0p session). Replay traps: the 0p is baked into the dump's `rate_import` (deleting `octopus_free_slots` does nothing), and the config-diff table shows the apps.yaml value while `calculate_plan()` uses the dump's scalar. (GH#5719)
- **The plan seems stuck and won't adopt a clearly better replan** — `should_replace_plan()` compares pre-clip snapshots and a retained incumbent keeps a stale one; gates and units are under "Check configuration before code". (GH#4736, GH#5261)
- **Charge and hold writes alternate every 5 minutes while the target is steady** — SoC flips 1% across the target: the `execute.py` hold gate `soc_percent >= inv_target_soc_percent` has no tolerance, and the `- 1.0` hysteresis nested inside it is dead (2026-11-05). (GH#5083, open)
- **Low power charging does nothing / still charges at full rate** — usually by design: `find_charge_rate()` uses max rate when the window's average remaining PV forecast is at least `low_power_pv_threshold_w` (150 W) (`Low power mode: PV forecast in window ... default to max rate`); the same threshold drives the dawn split. Treat requests as enhancements. (GH#4577, GH#4975, GH#3311)
- **A dawn-split window's dark half shows "rate zero"** — frozen at the reserve (`FrzChrg`), so no rate is computed. (GH#5241)
- **The low-power rate changes several times within one window** — by design: `find_charge_rate()` re-runs every cycle, filtered only by `rate_tolerances()`' 5%. (GH#5536, GH#2628)
- **Two charge windows meet at 00:00 inside one continuous cheap period** — `calc_dawn()`'s light latch resets at midnight; since PR #5709 (2026-10-23) `find_charge_window()` splits only at dawn. (GH#5205)
- **Turning `debug_enable` on changes the plan** — deliberate: `kernel_supported()` refuses under `debug_enable`, and Python honours the coarse `enable_fast_mode_levels` step while the kernel runs at 5 minutes, so near-ties can flip. (GH#5408, GH#4453)
- **Predbat exceeded the site's import ceiling** — the planner has no import clamp: `import_limit` is only a discovery rating (sunsynk/fox/deye). Draft PR #5486 (diagnostics) open. (GH#5490)

### Plan: exporting

- **Exports happen at the wrong time** — `plan.py`'s export pass order, `combine_export_slots`, `metric_min_improvement_export`.
- **"Random" exports that lose money on a flat export tariff** — see GH#4840 under "Check configuration before code".
- **Exports at the export peak then imports all evening ("never sells above my buy price")** — sell is weighed against the refill price, not the concurrent import price; `best_soc_keep` handles the tail and `best_soc_min` binds only in export windows. Exports run at full power, so window length is the stop line; a row's limit % is the SoC reached. (GH#5509, GH#5566, GH#5579)
- **`Best export window @ 0p` on a non-zero export tariff** — a stale discharge window read back from the inverter (`update_status()` pre-fills `average = 0`), used verbatim under `calculate_best_export: false`. A `discharge_rate` + reserve write after a charge window is the routine restore, not an export. (GH#5036)
- **"Charging and exporting in the same slot" in the plan html** — two windows in one 30-minute row; `output.py` splits the State cell deliberately. (GH#4987)
- **Loss-making forced export kept plan after plan; `Prune kept hedge export slot` / `Reverted change to d window N`** — fixed in PR #5977 (2026-11-05); see "Check configuration before code". Prune pairs read (without slot) vs (with slot), lower is better; `export_threshold_penalty` is per 30-minute interval. (GH#5969)
- **An evening export drains to the same level whatever the load risk** — pv10 scored target exports at `best_soc_min`; since PR #5736 (2026-10-25) it floors at the nominal run's end SoC.
- **An export window is in the plan but moves nothing** — limits in the sentinel band (99.3/99.5/99.7) mean "Hold exporting" in both engines, and 99.0 is the freeze sentinel (GH#4914; whether a 99% floor still emits it is unverified). A freeze-export window zeroes an overlapping charge candidate (`remove_intersecting_windows()`). Change both engines together (`test_kernel_parity.py`).
- **Every ordinary export window vanishes when a saving session or Axle event is in the horizon** — the threshold latched at 99; fixed in PR #5163 (2026-10-09, v9.3.7). A dump's `rate_export_cost_threshold` is rewritten from the plan's own level, so it is not the operating threshold. (GH#5221)
- **Predbat plans exports the site may not make (DNO or installer restriction)** — on Teslemetry see `update_battery_export_permitted()`. Elsewhere `export_limit: 0` (zeroes solar export) and `battery_rate_max_discharge: 0` (stops self-consumption) both mis-model it. (GH#5710)
- **The export icon or `binary_sensor.predbat_exporting` says off while the battery exports** — `isExporting` is not set by the demand-freeze `[Freeze exporting]` block; read the status text. PR #5131 open. (GH#5125)

### Car charging and Intelligent dispatch

- **IOG: battery flat inside an evening dispatch running past 23:30, plan waits until 01:00** — fixed in PR #5304 (2026-09-30). Replays, even `--redo`, reuse the dump's `rate_import`/`io_adjusted`; overlay from `rate_import_no_io`.
- **IOG: the battery charges at the back of a long, equally cheap dispatch, or the limit flaps tick to tick** — placement is metric-neutral, so search convergence and `metric_min_improvement_plan` decide it. Since PR #5635 (2026-10-19) `io_confirmed_credit_update()` adds a small score-only early credit in a confirmed run. Open. (GH#5330, GH#5344)
- **IOG: the battery charges cheaply for the first minutes of a dispatch that started the moment it was published** — `octopus_surplus_from()` withholds only from the next half hour; in the current one only dynamic-load evidence vetoes. Umbrella #4516 open. (GH#5424)
- **Plan won't use Intelligent dispatch windows, and `pv_metric10_weight` alone toggles it** — PV10's `dispatch_gone` rule prices `io_adjusted` minutes after the first 30 at `max(rate_max, import)`; any feed with an `adjust_key` sets the flags. Gate the kernel's `io_flag` too or `test_kernel_parity` fails. (GH#5540)
- **Predbat charged the car at peak rate** — before PR #5251 (2026-09-26) `car_charging_plan_smart` defaulted false. A saving-session reward on the import rate can drop that hour from `low_rates`, gapping the car slots. (GH#2716)
- **Repeating `Warn: Out of range index N within item car_charging_planned` (or `_plugged`, `_energy`)** — fewer charger entries than `num_cars`, so extra cars read unplugged; keep a fix car-arg-scoped, as per-inverter args rely on the fallback. PR #4928 open. (GH#5783)
- **Battery idle ("Hold for car") while the car charges from grid** — expected; see the *Car charging* notes. Since PR #5470 (2026-10-13) a charging inverter is left alone (`Inverter N is charging, so the hold for car N leaves the discharge rate and reserve alone`). (GH#4572, GH#5419)
- **Too many inverter writes or replans while the car charges, though the plan looks right** — hour-bucket `grep "write_and_poll_value: Wrote"`. Causes: `Dynamic load adjustment changed, will recompute the plan`, `watch_event()` refires, flat-band limit swings, low power rewrites, `disabled charge window while waiting for schedule`. (GH#5434, open)
- **A car hold during a charge window blocks grid charging on GivEnergy** — fixed in PR #5470 (2026-10-13). Since PR #5543, `has_charge_enable_time` inverters get charge enable written off between windows, deliberately. (GH#5419, GH#5371)
- **Freeze exporting plus a car hold on a timed-pause inverter drains the battery into the car** — fixed in PR #5640 (2026-10-24); after it, `timed pause is already pausing charge only` means discharge is not paused. `carHolding` also turns the charge-off write into a freeze. (GH#5612)

### Inverter control and writes

- **Inverter setting not applied** — follow `execute.py` into the component's reconcile loop, and check `read_only`.
- **Battery charges to 100% at start, on a mode change or on `set_read_only` (Victron `SK`, any `has_charge_enable_time: False` profile)** — `reset_inverter()` writes `adjust_battery_target(100.0, False)`, a real order where that register is a floor, on any `reset_inverter: True` config change including startup, even in Read-Only.
- **`Note: Inverter does not support charge/discharge freeze - disabled` despite the flag; `has_service_api`/`has_rest_api` changes nothing** — capability flags are `INVERTER_DEF` values in the `inverter:` block (a custom type copies `GE`); `has_service_api` does not exist and `has_rest_api` is never read. Services come from the six `*_service` keys. (GH#1406, GH#5066, GH#5203)
- **`write_and_poll_switch: No entity_id for scheduled_charge_enable ... to write` / `write_and_poll_option: No entity_id for inverter_mode`, only a red `with Errors reported`** — presence flags with no bound entity; custom types inherit GE's `has_charge_enable_time: True` / `has_ge_inverter_mode`, so check the `inverter:` block first. (GH#5539)
- **Reserve ratchets up and never returns (`Increasing set_reserve_min from X% to battery_min_soc of Y%`)** — a one-way raise in `Inverter.__init__`; bound to a live entity (Solar Assistant Growatt SPH template) it pins the highest value read. (GH#4013, GH#5232)
- **"Predbat always sets the reserve to X%" where X = charge target + 1** — the hold-on-reserve path (`Hold charging as SoC ... is above target SoC` before `Current Reserve is ... new target is X%`). Changing `set_reserve_min`/`set_reserve_enable` drops the reserve to the device floor for a cycle. Blame drift only with ledger evidence. (GH#5660)
- **Hundreds of `Trying to write N to X didn't complete got M` overnight** — check the plan had a window: idle cycles rewrite rates to max and `write_and_poll_value()` has no failure dedup, so a refused register (SolarEdge preserve-charge) repeats every cycle. False alarms: see *HA write/verify*. SolarEdge `Connection to inverter ID N failed` is the HA integration. (GH#5073, GH#5324)
- **Every charge/export window runs ~N minutes late** — the inverter's clock; compensate manually (`inverter_clock_skew_*`). `check_clock_skew()` warns hourly at ≥5 minutes from v9.0.2. (GH#4927)
- **GivEnergy-mode discharge times appear in the inverter portal only once the window starts** — as coded: `adjust_force_export()` nulls start/end when `inv_has_ge_inverter_mode and not force_export`. The 30 s `Sleeping (workaround)` settle can delay full rate ~10 minutes. (GH#2649, GH#4180)
- **Charge or discharge rate pinned far below the hardware's** — check the component's auto-config rate binding first (GE Cloud: the `charge_rate` entity's `max`; Fox: a duplicated `batteryList`; Solis: its notes); compare `battery_rate_max_raw`/`soc_max` in the dump with the nameplate. (GH#4908, GH#4919, GH#4940)
- **Low power computes the right rate but a service-driven inverter still charges at full power** — with `output_charge_control: "power"` there is no dummy `charge_rate`, so `{power}` is `battery_rate_max_raw` (GH#3311, GH#5252 open); with a helper entity it lags a cycle. (GH#5182, GH#5252)
- **Inverter service calls stopped, or are re-sent every cycle** — `Calling service` with no effect is an HA rejection (`was not accepted`); repeated `Skipped service ... as it was previously called` is payload dedup, keyed by domain alone (`hash_index = domain`), so two inverters on one domain re-call every cycle (alternating `Calling service`). (GH#5021, GH#5627, open)
- **Solis: no charge or export since a pod restart, `Failed to verify CID 636 storage mode ... read back` (2 lower), errors naming `cid 103`** — a timed-out CID 6798 read left a TOU V2 inverter treated as V1; fixed in PR #5864 (2026-10-30). See the *Solis* notes.
- **Sigenergy: MQTT `published` but commands do nothing, or `code=1201 Access restriction for /openapi/system`** — the publish line proves nothing; run `sigenergy.py --command-test --command-mode idle`, and see the *Sigenergy* notes.

### Rates, costs and savings

- **`cost_today` steps up after each half hour of an Intelligent car charge and falls back 15–60 minutes later** — `today_cost()` re-prices a running dispatch before `completedDispatches` lists it; `octopus_held_update()` bridges it (`OCTOPUS_DISPATCH_HOLD_MINUTES`). See the *Octopus* notes; test `octopus_dispatch_hold`. (GH#5520)
- **Rates read zero, or `Warn: Octopus: No Octopus data in sensor ... attribute 'rates' / 'raw_today' / 'prices'`** — `fetch_octopus_rates()` reads only `rates`/`raw_today`/`prices` (plus `raw_tomorrow` for non-Octopus entities); else use a template sensor. Keys: `rate`, `value_inc_vat`, `start`/`end` (×100, so EUR shows as cents), `value`, `price`/`from`/`till`. (GH#5676)
- **Plan-card summary cost one digit short ("£3.2")** — fixed in PR #5796 (2026-10-28).
- **The day rate is classed as low (low-rate sensors stuck on) while a saving session or Axle event is in the forecast** — fixed in PR #5163 (2026-10-09, v9.3.7) by `rate_minmax_excluding_saving()`; `rate_max`/`rate_average` stay event-boosted. (GH#5050, GH#5237)
- **Import cost far above the bill on a 3-phase site with per-phase sensors** — `minute_data_import_export()`/`minute_data_load()` sum phases instead of netting, double counting cross-phase offsets in today-so-far cost (plan unaffected). (GH#5746, GH#5835, GH#4856)
- **`cost_yesterday` / the History view uses today's rates for yesterday on a dynamic tariff** — `rate_replicate()` back-fills negative minutes with today's same-time rate, which `calculate_yesterday()` shifts back 24 h; the live plan is unaffected. (GH#5194, GH#3267, open)
- **`cost_yesterday` / `savings_yesterday_*` show the wrong day after a clock change** — `calculate_yesterday()` reads `minutes_back = minutes_now + 1`, but `minute_data()` keys by elapsed minutes, so lookups are ±60 off on a change day Fix the lookup sites, not `minutes_now`. (GH#5760, open)
- **"I can only override one plan slot at a time"** — by design (one `POST /rate_override` and a reload each). Batch: `rates_import_override`/`rates_export_override` in apps.yaml, or the manual API. (GH#5289)

### Sensors, history and load data

- **Plan tab blank / no plan shown in HA** — `predbat.cost_today` is missing from HA recorder history (recorder configuration). See test `faq_recorder_config` and `docs/faq.md`. (GH#3936)
- **Energy totals jump, or a day counts a huge figure** — look for a counter dip then recovery: `minute_data()` drops only upward steps above `MAX_INCREMENT` (1.2 kWh/min), so a gentle recovery after a held dip counts in full. Applies to any `*_today` binding. (GH#5388, GH#5356)
- **The plan is too optimistic about solar on an AC-coupled site whose load reads ~0 in daylight** — derived load is already net of PV and `get_diff()` subtracts PV again; no generic netting option. Calibration side: *Solcast*. (GH#5552, open)
- **SoC reads too high, never falls, or is frozen** — check the sensor behind `soc_kw` (a "charge today" sensor ratchets); `battery_soc:` is not a config key. `Inverter N SoC:` fixed while power moves is a frozen entity (often mixed GivTCP prefixes). `soc_max` bound to "kWh remaining" shows as `nominal_capacity` on `predbat_soc_max_calculated` moving with charge. See *SoC sources*. (GH#4884, GH#5522, GH#5650)
- **`sensor.<prefix>_soc_max_calculated` disappears after a restart** — fixed in PR #5283 (2026-09-30). Check whether a missing sensor's only publish sits in a recovered-history branch. (GH#5222)
- **`Warn: Found N periods of zero load with power data, filling using power integration` repeats every cycle** — benign: `fill_load_from_power()` never marks a stretch handled; `load_power_fill_enable: false` turns it off. (GH#3623, GH#5204)
- **load_today climbs by kWh per hour on v9.4.1, or `Filled N gaps in load_today ... from predbat.load_energy_h0 history`** — feedback on Predbat's own `load_energy_h0`, fixed in PR #5656 (2026-10-20). Read any sensor Predbat also publishes with `interpolate=False`: `minute_data` extrapolates past a fresh newest record. (GH#5652)
- **A live export or charge window stops mid-slot after `Dynamic load last period 9.60kW, status high`** — `load_last_period` came from a coarse counter (Solis' whole-kWh `homeLoadTotalEnergy`); since PR #5851 (2026-10-29) "high" must be confirmed by `load_power`. (GH#5851)

### Platform, web and updates

- **Works in HA, broken in Docker/standalone** — see the *Standalone / Docker* notes.
- **Predbat can't self-update, "unable to fetch", or a 403** — the shared unauthenticated GitHub quota, not a broken release; see *Self-update*. (GH#4886)
- **`update.predbat_version` sits in an "unknown" group, or its skip button is blocked** — a state-only entity with no device or registry entry; skip is blocked by the hard-coded `auto_update: True` (#727) and `update_event()`'s stub skip branch. (GH#5374, GH#5489, open)
- **A downloaded debug yaml has empty step arrays** — expected with `debug_enable` off; replaying it crashes with `KeyError: 0`, so grep `pv_forecast_minute_step:` for `{}`. (GH#4853, GH#5650)
- **Memory high or RSS creeping** — the Linux idle floor is ~75 MB of imports plus ~25 MB of data; expect ~126 MB idle. Ask whether Load ML is on (~30 MB plus 16 OpenBLAS threads).
- **The plan tooltip's charge kW is far above the plotted `battery_power_best`** — display only; fixed in PR #5643 (2026-10-28), though it still under-reports where `battery_rate_max_charge_dc` exceeds the AC rate. (GH#5394)
- **WhatIf (annual) shows 0 kWp for a panels-mode array** — fixed in PR #5842 (2026-10-29). Still live (2026-11-05): `web_annual.py`'s `_arg()` passes no `required_unit`, so per-inverter kW sensor lists are summed raw. (GH#4395)
- **Plan-table times or the override dropdown are shifted an hour on a clock-change day** — minute keys are instants, but labels use `midnight_utc + timedelta(...)` at a fixed offset (PR #5729 open). Decode `rate_import` minute keys (first key −1440) to settle it. (GH#1566, GH#5708, GH#5749, open)
- **Predbat or Predheat logs nothing for the whole repeated hour at the autumn change while components keep logging** — `timer_tick()` compared naive local times; fixed in PR #5772 (2026-10-27). Other DST issues: #3688/#3694 (`Low Import rate found rates in range 99p to -99p`). (GH#5770)

## Traps when investigating

### Versions, logs and attachments

- **Version drift.** Compare the log's version with `git tag --contains <sha>` (fetch tags first); many reports are fixed on main, and `v9.0.1` is `v8.55.1`. Treat "worked on 9.x, broke on 9.y" as a version question: `git show vX:<file>` before accepting a regression (GH#5390).
- **A `predbat.log` spans versions and restarts** (appended across upgrades and downgrades, GH#5178). `version X currently running` prints every cycle, so count restarts by `Loading apps.yaml` → `Predbat: Startup predbat`. A restart without `Predbat terminating` was an outside kill, whose trigger is only in the add-on log (GH#3969).
- **Expect one failed cycle after a downgrade.** ≤v9.0.x over v9.1.0+ state raises `TypeError: must be real number, not list` in `double_array` once (export-limit tuples, PR #5047); it is not the reported bug (GH#5213).
- **`Best charge window` is the prefilled incumbent, not the optimised plan**, which is in the later `Raw/Filtered charge windows` lines. In a repeated pair in one tick the later line is the recompute (GH#5434, GH#5344).
- **Prove an attached debug yaml is the reporter's own** (compare `soc_kw` and serials/account ids at its capture minute with their log); once proven, it beats the issue prose (GH#5335).
- **Attachments disagree in time.** Numbered logs (`predbat.1.log`, `.2.log`) are overlapping snapshots, not rotation order (check head and tail timestamps); debug-history snapshots are ~3-hourly; `predbat_debug.yaml` is download-time state (GH#5344). A new attachment URL can 404 for 10+ minutes (GH#5503).
- **Debug file names.** Ring snapshots are `debug/predbat_debug_<id>.yaml.txt` (GitHub rejects bare `.yaml` and `.tar`); the per-cycle `create_debug_yaml()` dump still needs renaming. The HA Companion app's webview ignores `Content-Disposition: attachment` (GH#4720).
- **Warnings.** Routine `Warn:` lines are the cause only if their timing matches the report. HA core's `Referenced entities ... are missing or not currently available` is only in the HA core log (GH#5420).
- **Hardware behaviour needs the hardware.** A unit test shows what the code does, not what the inverter did; say the maintainer must confirm.
- **A flat SoC is not necessarily a stall.** On a large pack one percent can be ~10 minutes at 2 kW (GH#4784).
- **Check that the report's premise exists.** Reports have cited AI-invented settings (GH#4918), features only on unmerged PRs (GH#4917), history `git log -S` disproves (GH#5022) and false "expert gated" claims (GH#5237). Grep `config.py`/`APPS_SCHEMA`, read each item's `enable:`, and check a proposal's symbols are on main (GH#5063).

### Replays and debug dumps

- **Copy `manifest.yaml` into a new worktree before running anything.** It is gitignored; without it `check_install()` downloads the release over tracked files and exits (`Predbat files are not installed correctly, trying to download them`). Recover: `git checkout -- .`, `git clean -fd apps/predbat`.
- **A/B across versions by checking out the old tag in place** and running `./run_all --debug_file <dump> --redo` on both (`git worktree add` is denied under triage permissions). First confirm the tag diff is inert for that config and the replay matches the live log (GH#5719).
- **Replay-probe pitfalls.** The dump's window list holds both days in one minute-of-day frame, so filter with `1140 <= start < 1440`. `Prediction.__init__` copies SoC, rates and ~200 other attributes from `base`, so set them first or rebuild it (GH#5616, PR #5756).
- **`self.predict_soc` after a trial run holds the nominal scenario** (nominal runs last in `run_prediction_metric()`); its keys stop at `forecast_minutes − 5` (PR #5735).
- **Ad-hoc replay under the triage permission mode.** Env-var prefixes break the allowlist: add a temporary probe to `run_debug_cases()`, gated on a marker file, and revert it. Plan pass lines go to `coverage/predbat.log`, not the test log (GH#5633).
- **Golden plans move with load scaling.** Regenerate `coverage/cases/*.expected.json` from `*.actual.json` and `random_results.json` via `./run_random`; `debug_cases` stops at the first failing case. First prove the shift by restoring the old behaviour.

### Tests and the C++ kernel

- **Do not rebuild the kernel binaries while triaging**: the `.so` files are committed and CI-built. A `scan_soc_range` count of 0 over a full plan means the kernel served it (GH#5408). After a `.cpp` edit delete the local `prediction_kernel_lib.so`; a loadable build is not rebuilt.
- **New kernel context arrays need `ContextStore` copies.** `pk_context_create` copies per-step arrays because the ctypes buffers are freed; a missing `ContextStore` vector/`assign`/`.data()` dangles silently, giving wrong values, not a crash (2026-10-18).
- **Kernel and Python can choose equal-scoring but different plans.** The kernel ignores the levels pass's `step=30` fast mode; `enable_fast_mode_levels: False` does not disable it, as `optimise_levels_pass` sets `fast_mode` per region (2026-10-20).
- **`--test <name>` resolves through `TEST_REGISTRY`, not file names** (`dynamic_load_car` is in `test_dynamic_load.py`); a wrong name gives a green run with zero new assertions (GH#5407). The fourth tuple field is the slow flag.
- **The suite aborts at the first failure.** A raising test aborts the run (the registry loop `test_failed = func(my_predbat)` has no try/except), and some module runners (e.g. `run_kraken_tests()`) return at their first failing `test_*`, so one FAIL proves nothing about later tests; rename them to `xtest_` one at a time (PR #5641).
- **Skips can count as passes.** `run_nordpool_test()` returns False (pass) on a download failure; `tests/nordpool_fixture.json` is committed, so the suite never hits the live API (PR #5799).
- **Gated tests skip their assertions in CI.** CI and `coverage/setup.csh` install only `requirements.txt`, so `if have_<pkg>:` gates run only their skip path (GH#5715).
- **A passing test proves nothing when the fixture encodes the bug.** Seen: flat-rate fixtures, identical inverters, zero-iteration loops, `_mk_get_ctx`'s `json_body or {}`. `TestHAInterface.get_state` returns `""`, not `default`, for a missing attribute (PR #5131); window asserts must check `start < end` (PR #4943).
- **Vacuous negative assertions.** A log-grep "not called" check is vacuous if the method is a silent stub; use a calls counter (GH#5718). For address-reuse guards assert the object is pinned (`gc.get_referrers()`), as `tests/test_secrets.py` does (GH#5171).
- **MagicMock bases wake up host hooks.** A MagicMock `self.base` passes `getattr(base, hook, None) is not None`, so a new hook needs `base.<hook> = None` in those doubles (PR #5819).
- **Stub and constructor shapes.** `Execute.__new__` stubs lack base methods (GH#4965). `ComponentBase.__init__` aliases the live shared `base.args`; prefer `Cls.__new__` plus explicit `base`/`log`/`prefix`/`args` (unaudited for actual mis-renders, PR #5211).
- **`self.base.set_read_only` is `True` until the first `update_pred()`**; read `get_arg("set_read_only")`. The charger mixin and wallbox were fixed in PRs #5793/#5798; `gecloud.py` and `gateway.py` still read the attribute first (checked 2026-11-05).

### Clocks and time

- **`midnight_utc` and `now_utc` are local time**, in `ComponentBase` and `now_utc_exact` too. Use `.astimezone(timezone.utc)` before a `Z` suffix or an API window, and test with a BST date (GH#5572, GH#5693). `time_abs_str()` prints local time (GH#5036).
- **Build test dates from Predbat's clock**: anchor to `my_predbat.midnight_utc` and pin both clocks (PR #4877, PR #4998). Storage-cache expiry is the exception (real clock).
- **The quick suite fails 00:00–01:00 local in summer on main** (2026-11-05): the teslemetry weekday test raises and aborts the run, and `test_solax.py`'s month test fails with an empty detail (grep `**** ERROR:`). Check the clock first.
- **`minutes_now = 0` hides time-of-day bugs.** History-index code needs a test at non-zero `minutes_now` (GH#4732). A pin-guard test is vacuous when the pin equals the residue (`FIXTURE_MINUTES_NOW`).
- **`timedelta.seconds` wraps into [0, 86399].** Use `abs((a - b).total_seconds())`; the button-press site was fixed in PR #5697 (GH#5694). Sites whose cache cleanup caps age below 24 h are fine.
- **Naive-tz sweeps after `strptime_aware()`.** It raises on missing tzinfo, so a `%z` site's naive handling is dead, and a bare-statement `.replace(tzinfo=...)` is a no-op. Watch for orphaned F401 imports (GH#5695).
- **CI's `python-version: 3.x` resolves to different CPythons per runner**; check `Successfully set up CPython` in the job log (PR #5653). The AST guard `offset_strptime_calls()` misses attribute and `BinOp` formats (2026-11-05).
- **`HAHistory` lags up to 2 minutes, and smoothing back-fills the gap**, so moving a marker to now loses the tail; count only the uncounted delta (`car_manual_soc_track()`, PR #5752).

### Reading the code

- **`self.base.args` holds Predbat's own dummy entity ids.** `create_entity()` ids pass `is_entity_id()`; use `user_configured_entity()` (PR #4745). Since PR #5126 Inverter objects persist and `refresh_config()` re-reads config every cycle (GH#5099).
- **`inverter_source_active()` is fleet-wide**, and `rest_data` has gone (since #4864). `create_missing_arg()` pads with `None`.
- **`re:` arg values are full-matched `^...$`** (`resolve_arg_re()`, GH#5677).
- **Sign conventions.** `grid_power` is negative on import (`grid_power_invert` exists for meters that disagree); `battery_power` is positive while discharging. Find out which convention a sign report assumes (GH#4797).
- **Throwaway event loops in component events.** `select_event`/`switch_event` presses run under `asyncio.run` on a fresh loop, which breaks a persistent `aiohttp.ClientSession` or `asyncio.Lock`; hand the work over with `run_coroutine_threadsafe` (fixed for Hanchu in PR #5684).
- **Midnight tariff compare.** Before PR #5465 a `using new plan` inside the midnight tariff compare could become the live plan (GH#4900).
- **Two loops over one slot.** A `TypeError` against `None` deep in the plan often means two loops over one slot drifted apart (PR #4943).

### Docs

- **Docs yaml fences are unvalidated.** Extract each fence and `yaml.safe_load` it; mis-indented `variables:` keys parse silently. `git blame` the fence and suspect wide re-indent commits (GH#5479).
- **`docs/apps-yaml.md` documents each setting twice**: a key bullet and a `### <Display Name>` section that links target, so grep both. On main (2026-11-05), `### Inverter reserve maximum` still says "the default is 98" (PR #5644).
- **Some docs entities live in HA core.** `binary_sensor.predbat_running` is the Supervisor's per-add-on Running sensor, disabled by default (GH#5755).
- **Inverter templates.**
    - An unknown `inverter_type` needs no code.
    - `battery_min_soc` reaches the plan only through the reserve chain.
    - With no freeze service, `freeze=True` falls back to `discharge_stop_service` (PR #5648).
    - A `template:` sensor's entity id comes from `name:` (GH#5683).
    - Check `mdi:` names against the SVG repo, because unknown names render blank.

### Reviewing PRs

- **A clean auto-merge can still be a semantic conflict.** Run the full `run_pre_commit` (PR #4645).
- **Check base staleness first.** The fix may already be on main, and resolving a test conflict can delete main's test (PR #4756).
- **Stacked-PR squashes.** A `(#M)` squash suffix on PR N's head commit means its diff spans both PRs (PR #4186).
- **Fork PRs can carry pasted truncation banners.** Grep the head tree for `truncated output` and `Total output lines`, confirming with `grep`, not Read, which adds its own banners (PR #5766).
- **CI runs the quick suite in the `pre-commit` job**, so "tests didn't run locally" does not mean untested; check the touched modules' slow flags (PR #5346).
- **Docstring coverage (interrogate) and flake8 are enforced nowhere** (re-checked 2026-11-05, PR #5810).
- **GitNexus is indexed from main.** On a PR branch read `detect_changes()` as "which files", not "which symbols" (PR #5143).
- **Re-running `/pr-review` on an unchanged head duplicates the review.** Post a follow-up with `comments: []` instead (PR #5645).

### GitHub and tooling

- **`gh issue list --search` without `--repo` searches all of GitHub.** Always pass `--repo springfall2008/batpred`, and distrust an empty result: fetch the exact title, typos included (GH#4705, GH#4973), and search bare `#N in:body` and the reporter's own issues (GH#5420).
- **`gh api` permission rules match literal prefixes.** Put flags after the endpoint. Events come back oldest-first. The `comments: 0` in a review-creation response is unreliable (PR #5759, PR #5143).
- **jq on `--json comments`.** Use `--jq '.comments | length'`; plain `length` counts the wrapper's keys (GH#5349).
- **Triage permission-mode routes.** Read a branch doc with `git fetch` plus `git show <branch>:<path>` (GH#5291). For another repo use WebFetch, after checking its default branch (GH#5417).
- **Kraken (EDF/E.ON) issues have no component label.** Do not guess `Octopus`.

## What belongs here, and what does not

The journal is for what a future triage run would otherwise have to rediscover. Test every entry with one question: would it save time on a **different** issue?

- **Keep: techniques and traps.** How to replay a dump (`--redo`), test-order pollution, stale kernel binaries, the macOS local-network block. These apply to every investigation.
- **Keep, tightly: durable facts about a device or API.** One line, no story. "GivEnergy stores rates as a whole percent of capacity, rounded down" is about the hardware, not about the issue that found it.
- **Keep, short and dated: findings that explain a class of report.** "Car not charging because X" is useful when the same symptom comes back. Give the issue or PR number and the date, say how it was verified, and word it as "look here first", so the reader confirms it against the code.
- **Drop: per-issue narrative.** What was tried, what the fix was, why the alternatives were rejected. That belongs in the PR and the issue, where it stays tied to the code it describes. In the journal it goes stale as the code moves.
- **Drop: file paths and line numbers.** They drift with every merge, and a citation pointing at unrelated code misleads more than no citation. The area heading already names the module, so point at a function, a config item or a log line instead: those survive a refactor, and a reader can grep for them. Naming a file is fine when the file is the subject rather than a location: `apps.yaml`, a template, a test to run, or "`execute.py` has no AlphaESS case".

Write each entry in the same shape: the **symptom** (the log line verbatim, or what the reporter sees), the **fact or mechanism** behind it, and **where to start looking** (a function or config item). Then add the issue or PR number, and the date if it is a bug.

Entries go stale, so retire them:

- When the code an entry describes has changed, correct or delete the entry. Do not leave it marked as stale.
- Once a fix for a bug entry has shipped, cut it down to what a reader holding an older log needs ("fixed in PR #N") and drop the story. After about six weeks, delete it unless the mechanism is still a trap.
- Prefer sharpening an existing entry over adding a second one.

Keep it small. Every triage, review and journal-update run reads this file end to end, and at 730KB it cost them most of their turns. One fact per bullet, a few hundred characters at most. If an area's notes grow past about 3KB, consolidate them rather than adding more.

## Adding to this file

When an investigation turns up something that passes the test above, add a bullet under the right heading: the area under "Per-integration notes", or the topic under "Symptom → first place to look". Keep it short, name a function or config item rather than a file or line, and cite the issue number so the next reader can check the original.

If you are running under the triage bot you cannot edit this file directly — the clone is `reset --hard` before every flow, so the edit would not survive. Write the finding as a single markdown file in the queue directory named in your system prompt instead; the `/journal-update` flow folds the queue in once a day, re-checking each candidate against current `main` first, and opens a PR for a maintainer to merge.

Whichever route you take, the bar is the same and it is not "interesting": record what you **verified**, say how you verified it, and keep anything you merely suspect explicitly marked as suspected. A wrong entry here is worse than a missing one — every later run trusts this file, so a confident mistake ends up in a comment posted to a real reporter. Correcting or deleting a stale entry counts as much as adding one.

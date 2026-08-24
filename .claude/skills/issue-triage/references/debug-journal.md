# Debug journal

Notes distilled from maintainer debugging sessions on this repo (roughly June–August 2026), written for whoever is triaging an issue next.

Every entry is an observation from a past investigation, not a statement about current main. The code moves. Use these as "look here first", confirm against the working tree before you put anything in a comment, and say what you actually confirmed rather than what this file says.

## Replay the reporter's debug file

The most useful attachment on a bug report is `predbat_debug.yaml` — a full state dump the test harness can replay against current main:

```bash
cd coverage
./run_all --debug_file <scratch>/predbat_debug.yaml > <scratch>/replay.log 2>&1
```

What you get out of it:

- **Every config item the reporter has changed from default**, printed as `- <item> = <value> (default <value>)`. Grep the replay log for that block before reading their yaml — it is where most "the plan is wrong" reports get settled.
- A recalculated plan with a metric before and after, written to `plan_orig.html` and `plan_final.json` in `coverage/`. Both are gitignored, and `git clean -fd` clears them.
- `--redo` recomputes rates, load model and Octopus slots instead of reusing the ones in the dump. Without it you are replaying the exact state they had, which is normally what you want.

Committed examples of the same format live in `coverage/cases/*.yaml`; they run as golden regressions under `./run_all --test debug_cases`.

Two lines in that output are normal and are not the reporter's bug:

- `Prediction kernel stale binary ... - using Python engine` — the local C++ kernel is older than the Python side expects, so the pure-Python engine runs instead. The plan is still correct, just slower.
- `Config item ... is below the minimum ... - clamping to ...` — routine clamping of an out-of-range setting.

## Check configuration before code

"The plan is wrong" has repeatedly turned out to be settings rather than a defect:

- **GH#4222** (odd freeze-export windows) — traced to the reporter's own `combine_export_slots: True` plus a high `metric_min_improvement_export`, not a code regression. A stale local test file also produced a false kernel regression partway through that investigation.
- **GH#4478** (exports scheduled late) — there *was* a real ordering bug (`optimise_swap_export` ran before the later plan passes, fixed in PR #4513), but the user-visible difference was dominated by `best_soc_keep: 4.0` in their config.

So pull the non-default config list out of the replay first and look at `combine_export_slots`, `metric_min_improvement_export`, `best_soc_keep`, `metric_battery_value_export_scaling` and the `load_scaling*` family before concluding "code bug". Saying "this is configuration" is a legitimate triage outcome — that is what the `configuration` label is for.

## Per-integration notes

Grep for the named symbol rather than trusting a line number.

| Area | What past debugging found | Targeted test |
|------|---------------------------|---------------|
| Fox (`fox.py`) | The cloud API returns errno 42015/44096 for settings a given device does not support (`FOX_SETTINGS_UNSUPPORTED_ERRNO`); those are marked unavailable and never polled or written again. Entity type matters — WorkMode is a select, ExportLimit a number. | `fox_api`, `fox_oauth` |
| Solis (`solis.py`) | `SOLIS_CID_STORAGE_MODE = 636`. The inverter only retains the TOU bit on CID 636 while a charge or discharge window is configured; with slots disabled the bit silently drops and the verification warning repeats. A log full of CID 636 verification warnings is usually this, not a failed write. | `solis` |
| SolaX (`solax.py`) | Code `10402` is a token/auth failure, retried in-request rather than waiting for the next cycle. SolaX clamps battery minimum SOC at 10% (`SOLAX_MIN_RESERVE_PERCENT`), so `battery_min_soc` is auto-configured to stop Predbat writing limits the inverter will reject. | `solax` |
| Sigenergy (`sigenergy.py`) | Lifetime/history totals are cumulative server-side and reset around EU midnight, which showed up as an overnight dip; `fetch_history_totals` applies a monotonic clamp. "Energy totals went backwards overnight" starts here. | `sigenergy` |
| GE Cloud (`gecloud.py`) | Gateway fields can come back null. `merge_non_null` stops nulls overwriting good values, and the publish path guards null containers — GH#4656 was a null crash in that area. | `ge_cloud` |
| Teslemetry / Powerwall (`teslemetry.py`) | Battery model is inferred from site `nameplate_power / battery_count`. A nameplate fallback combined with `inverter_hybrid: True` once produced a false 5 kW inverter limit and spurious morning export. Tariff writes push a whole TOU schedule every cycle, so a setting changed by hand in the Tesla app is reverted on the next cycle (GH#4600, GH#4610). | `teslemetry` |
| Sunsynk / DEYE (`sunsynk.py`, `deye.py`) | Freeze export is gated by the per-slot power register (`sellTime{n}Pac` on Sunsynk), confirmed on live hardware — setting the energy mode alone had no effect, and with slot power at zero the battery still charged. `read_only` was ignored by the reconcile loops until `_is_read_only()` gated `_reconcile_control()` (GH#4436). | `sunsynk_control`, `deye_control` |
| Grid sign / arrow direction | `grid_power_invert` is owned by some integrations and not others. With two systems configured, one integration setting it `True` bleeds into the other's entities and inverts the arrows. The fix is an explicit `False` in the automatic config of both. | `sunsynk_config`, `teslemetry` |
| Enphase (`enphase.py`) | Unofficial Enlighten endpoints. Accounts with MFA cannot log in at all. Discharge-to-grid schedules are required for export control. Writes need a double-submit CSRF token or return 403. Using the Enphase app at the same time can trip session limits. | `enphase_api` |
| Octopus (`octopus.py`, `fetch.py`) | Intelligent Go tariffs are detected via `is_intelligent_go_tariff()`, and IOG-prefixed tariffs must be skipped when updating intelligent devices. Saving-session auto-join rebinding regressed when `joined_events` was empty (GH#4573). `octopus_slots_signature()` deliberately omits the time-drifting fields of active dispatch slots so a replan is not forced every cycle. | `octopus_*`, `saving_session*` |
| Axle (`axle.py`) | Export sessions have to boost the import rate as well as the export rate. State is published unconditionally from `run()` so a fetch failure does not freeze the sensor at a stale value. | `axle` |
| History fetch / memory (`ha.py`) | History is fetched in `HISTORY_CHUNK_DAYS`-sized chunks with boundary dedup — records landing exactly on a chunk start inside a data gap corrupted smoothing before that was fixed. The largest memory peak in a run is ML load-predictor training (`load_predictor.py`), not the plan. | `history_chunking` |
| Charge/discharge curve (`inverter.py`) | The curve is evaluated per target minute; tapering near ~93% SOC is expected behaviour, not a fault. | `find_charge_curve`, `battery_curve_keys` |
| Standalone / Docker (non-HA) | GH#4601: a callback returning `None` instead of `True` broke the Octopus saving-session fallback in standalone mode. Anything that works under HA but not standalone is worth checking along the `ha.py` websocket and `userinterface.py` callback paths. | `trigger_callback_success_signal` |
| Predheat (`predheat.py`) | GH#4670: with `predheat_enable` set, Predheat still did not activate after startup because of lazy flag initialisation. There is no registered Predheat test module, so there is nothing to run here — investigate by reading. | none |

## Symptom → first place to look

| Report | Start here |
|--------|-----------|
| Plan tab blank / no plan shown in HA | `predbat.cost_today` missing from HA recorder history — a recorder configuration problem (GH#3936). Test: `faq_recorder_config`, and see `docs/faq.md`. |
| Exports happen at the wrong time | `plan.py` export optimisation order, plus `combine_export_slots` and `metric_min_improvement_export` in their config. |
| Battery charges or freezes when it shouldn't | Freeze paths in `prediction.py` and the inverter component's control write; check `best_soc_keep` and the freeze-related config in the replay. |
| Rates wrong, missing or not updating | `fetch.py` rate scanning, then the provider component (`octopus.py`, `kraken.py`, `energydataservice.py`). |
| Inverter setting not applied | `execute.py` into the component's reconcile loop; check whether `read_only` is set. |
| Energy totals jump or reset | Provider-side cumulative counters and any monotonic clamping. |
| Works in HA, broken in Docker/standalone | `ha.py` websocket and `userinterface.py` callback return values. |

## Traps when investigating

- **Stale kernel binary.** The `prediction_kernel_lib_*.so` binaries are committed and CI has a job for them. The warning above means the checkout falls back to the Python engine, which is fine for triage. Never rebuild or commit binaries while triaging.
- **Test ordering.** Some tests share a `PredBat` instance, which is exactly why `run_debug_cases` builds a fresh one per case. A test that fails in a full run but passes alone is usually pollution, not the reporter's bug — another reason to run only the targeted test.
- **Version drift.** Compare `git describe --tags` against the version in the first few lines of their log. A fair number of reports are already fixed on main, and that is a useful triage answer on its own.
- **Log noise.** `predbat.log` carries routine `Warn:` lines (config clamps, kernel status, unsupported settings). Don't quote a warning as the root cause unless it lines up with the time the reporter describes.
- **Hardware questions.** A unit test settles what the code does, not what an inverter did. Several findings here were only confirmed on live hardware. If the question is hardware behaviour, say the maintainer needs to confirm it rather than running a test to look thorough.

## Adding to this file

When an investigation turns up something a future triage run would have wanted to know — a config item that explains a class of report, an API quirk, a symptom that maps to a module — add a row. Keep it short, name the symbol rather than the line number, and cite the issue number so the next reader can check the original.

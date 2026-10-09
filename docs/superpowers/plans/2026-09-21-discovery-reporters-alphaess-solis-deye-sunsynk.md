# Discovery Reporter Rollout — Plan 2: AlphaESS, Solis, Deye and Sunsynk

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give AlphaESS, Solis Cloud, Deye Cloud and Sunsynk Cloud each an observe-only `build_discovery()` reporter, so the discovery catalogue covers the large majority of Predbat's cloud inverter base rather than GivEnergy and Fox alone.

**Architecture:** Each component gets one `build_discovery()` method that reads only state it already holds and builds records with the shared `inverter_record()`, plus one `self.refresh_discovery()` call in `run()` placed right after the cycle's publish and before any later early return. The four are independent: Deye and Sunsynk are hardware siblings but their data structures diverge (station grouping, export limit, telemetry shape, capacity derivation), so each reads its own attributes — no shared helper.

**Tech Stack:** Python 3.14, no new dependencies. Tests via `coverage/run_all`, quality via `coverage/run_pre_commit`.

**Spec:** `docs/superpowers/specs/2026-09-10-discovery-catalogue-design.md`, the reporter contract in `docs/discovery-catalogue.md` ("Writing a reporter"), and the rules learnt from Plan 1 in `docs/superpowers/plans/2026-09-20-discovery-reporter-rollout.md` (Global Constraints and Amendments).

**Depends on:** PR #5184 (the Fox reporter and the amended rollout plan), merged 2026-09-24. This plan was executed on a branch stacked on it, as PR #5206.

## Amendments

_Recorded 2026-09-24, after all five tasks were executed, the branch was reviewed as a whole, and PR #5206 had its first review. The working notes these rulings were made in are not committed, so they are copied here. The committed code is authoritative. Task 2's Solis code and test blocks below have been corrected to match it, since their superseded figures were the most likely to mislead. The other tasks' blocks still show the code as planned, and this list gives how they differ._

Made during execution:

- **Task 2's fixture binds the real `SolisAPI.is_tou_v2_mode`.** `MockSolisAPI.is_tou_v2_mode` returns a `_test_v2_mode` attribute and ignores the cached 43605 register the fixture sets, so the `tou_v2` assertion could never pass. The fixture binds the production method onto its instance with `__get__`, as the file already does for `automatic_config`.
- **Task 4's docstring states that Sunsynk excludes no device.** Neither discovery nor `automatic_config()` filters Sunsynk devices, which Task 4's Background required saying and its code block omitted.
- **Task 5's paragraph sits after the "What each section describes" table.** The anchor Step 2 named was the `inverters` row of that table, and a paragraph cannot go inside a table row.

From the whole-branch review (commit 82fd66b2):

- **Solis `battery_capacity_ah` is the bank total**, register 172 × `parallel_battery_count`, as `publish_entities()` computes it. As planned it was the per-pack figure, while Deye and Sunsynk report the whole bank under the same key.
- **Derived `battery_kwh` is rounded to 2 dp** in the Solis and Sunsynk reporters. Deye already rounded.
- **A Solis inverter whose detail has not been read reports no `functions`.** As planned it showed `["solar"]`, which reads as PV-only when `automatic_config()` treats the same state as "not read yet".
- **Two more Solis wiring tests.** One checks that `run()` files a report with the auto-configure gate closed. The other checks that a changed fact is re-filed on a later cycle (`first=False`).
- **Docstring and docs wording.** Sunsynk's `export_limit` is the per-device half of `automatic_config()`'s fleet-wide test. The Deye and Sunsynk / PV-only docs paragraph says records follow configuration, not hardware evidence, and how a misconfigured PV-only unit shows.

From the first review of PR #5206:

- **Sunsynk keeps its last battery ratings through a partial poll.** `fetch_device_data()` rebuilds `device_values` every poll and leaves out a field the battery endpoint did not return. `publish_data()` then leaves the `battery_capacity` sensor, and so `soc_max`, at its last value. The reporter now does the same (`_discovery_battery_ratings`) instead of filing a thinner report. The whole-branch review had accepted the drop-out, but it made the catalogue less stable than what Predbat uses.
- **`nominal_pack_voltage()` warns once per `chargeVolt` it cannot place.** Task 4's Background said it does not log, which was wrong: it logged a `Warn` on every call for a charge voltage that fits no LiFePO4 stack. That happened twice a cycle through `publish_data()`, and three times with the reporter.

## Global Constraints

Copied verbatim from Plan 1's Global Constraints — they bind every task here:

- **Observe-only.** Nothing in this plan may change any component's behaviour. No task writes `self.args`, calls `set_arg_auto()`, publishes an entity, or alters an existing control path. A reporter reads state that already exists.
- **A reporter never degrades its component.** Never call `non_fatal_error_occurred()` from discovery code: it sets `base.had_errors`, which makes `update_pred()` skip `record_status()` and suppress the run notification. `ComponentBase.refresh_discovery()` already catches and logs; do not add a second try/except around it.
- **A reporter is `build_discovery()` plus one unconditional `self.refresh_discovery()` call in `run()`.** Do not add a marker attribute, a state-key snapshot, or a try/except — `ComponentBase.refresh_discovery()` owns all of that. Return `None` from `build_discovery()` when there is nothing to describe yet.
- **Report only entities that exist.** Build descriptors and pass them through `self.discovery_entities(descriptors)`, which keeps only those Home Assistant has actually seen.
- **Never invent a record to satisfy a cross-link.** A dangling `measures_meter` is correct; a fabricated `meters` record is not.
- **Typed containers.** `ratings` accepts numbers and booleans ONLY. `info` accepts bounded vendor strings. `hardware_ids` accepts bounded strings. `account_ids` is pseudonymised on output. `functions`/`capabilities`/`flags`/`effects` are lists of lowercase tokens matching `^[a-z0-9_]{1,32}$`. A value that does not fit its container is silently dropped by `validate_report()` — so putting a model name in `ratings` means losing it.
- **Call `refresh_discovery()` as soon as the discovery data is in hand** - before the component's `automatic_config()` and before any later early return. `automatic_config()` can raise, and a report filed after it is never filed on the installs whose dump most needs one. Every wiring test includes a cycle where auto-config fails.
- **Set `inverter_type` using exactly the predicate the vendor's own `automatic_config()` uses to count a device as an inverter**, duplicated with a comment naming `automatic_config()` as the source of truth, and test a device that predicate excludes.
- **Build fixtures from the module's real API samples and existing fixtures, units included; never invent them.** Docstring samples, captured test fixtures, and real responses recorded in issues are the sources.
- **Any number reported as evidence must be able to distinguish the cases it claims to.** Where the vendor figure is known to be wrong, name the rating after what the API returned, not after the true quantity it fails to measure.
- **Every reporter gets a test that pushes its report through `validate_report()`** and requires every record back unchanged.
- **Station/plant/site IDs go in `account_ids`; site NAMES never go in `info`.** A site name is user-authored free text that can hold a street address, and `info`'s guards (a length cap, no `@`) would let one through.
- **Use the spec's capability tokens where one exists** (`schedule`, `pause_mode`, `pause_slots`, `target_soc`, `discharge_target`, `charge_rate_power`, `charge_rate_percent`, `soh`). A fact about topology rather than something the device can do is a flag, not a capability.
- **Firmware goes in `info["firmware"]`**, flattened to one string when the vendor reports a version per board.
- **Comments cite symbols, not line numbers.** A line number is wrong after the next edit above it.
- **Line length:** 256 (Black), 250 (Flake8).
- **Docstrings:** every function and class needs one (`interrogate`, 100%).
- **Spelling:** British English via cspell. Add genuinely new words to `.cspell/custom-dictionary-workspace.txt` (auto-sorted on commit, so re-stage after).
- **`git add` new files BEFORE running pre-commit.** `--all-files` means git-tracked only, so an untracked new test file passes unchecked.
- **Tests run from `coverage/`:** `./run_all --test <name>`. Save output to a file and grep it; do not pipe to grep directly.
- **Scope `git add` to the files you changed.** A GitNexus re-index can regenerate banners into `CLAUDE.md`/`AGENTS.md`/`.claude/skills/**`; never commit those incidentally.

Added for this plan:

- **No entity maps.** Decided 2026-09-21: reporters ship without `entities`. Each integration's `automatic_config()` already maps Predbat's controls to its entities, but extracting that table into a shared constant touches a control path, which this observe-only rollout may not do. Entity maps are a separate later project.
- **No new API calls.** A reporter reads only what the component already holds. Where a component does not hold a fact (a model, a firmware version, a station ID), the record omits it. Never add a fetch, and never read a response that the component fetches and then discards.
- **Report only stable facts.** `refresh_discovery()` compares each whole report with the last one filed, so a value that moves from cycle to cycle — state of charge, live power, a status string — would re-file the report every cycle. Report ratings, identity, capabilities and topology; never telemetry or status.
- **Call only side-effect-free helpers from `build_discovery()`.** A helper that logs or writes state (Deye's `derive_battery_capacity()` does both) would log once a minute. Each task names the pure accessors to use.
- **None of these four `automatic_config()` methods raises.** The placement rule still holds, but for a different reason than Fox: AlphaESS, Deye and Sunsynk each return early with `if first and not live_ok: return False` after publishing and before `automatic_config()`, and Solis gates `automatic_config()` on its own state. Each placement comment states the component's real reason — never "because automatic_config() raises". The wiring test's "cycle where auto-config fails" is therefore each component's own early exit: the first cycle whose live telemetry fails, or `automatic_config()` configuring nothing.

---

## Research basis

Every fact below was established by reading the source, with the sample quoted where one exists. Where the repository holds no real sample, the task says so and the fixture is marked as derived, with its derivation.

| | AlphaESS | Solis | Deye | Sunsynk |
|---|---|---|---|---|
| `inverter_type` written by `automatic_config()` | `"AlphaESSCloud"` | `"SolisCloud"` | `"DeyeCloud"` | `"SunsynkCloud"` |
| Predicate for "an inverter it configures" | every serial in `device_list` (`has_battery()` — `cobat > 0` — already filtered at discovery) | not `_reports_no_battery(detail)` AND `batteryHealthSoh` parses as a number | every serial in `device_list` (`deviceType == "INVERTER"` at discovery) | every serial in `device_list` (no filter) |
| `automatic_config()` raises? | no | no | no | no |
| `device_list` / fleet shape | list of serial strings | `inverter_sn`: list of serial strings | list of serial strings | list of serial strings |
| Model held | `minv`, `mbat` | `productModel` | none | none |
| Firmware held | none | none | none | none |
| Station/site ID held | none | none | `station_ids` | none |
| Early return after publish | `if first and not live_ok` | none (gated `automatic_config()`) | `if first and not live_ok` | `if first and not live_ok` |

---

## File Structure

| File | Change |
|------|--------|
| `apps/predbat/alphaess.py` | add `build_discovery()`; add `refresh_discovery()` call in `run()` |
| `apps/predbat/solis.py` | add `build_discovery()`; add `refresh_discovery()` call in `run()` |
| `apps/predbat/deye.py` | add `build_discovery()`; add `refresh_discovery()` call in `run()` |
| `apps/predbat/sunsynk.py` | add `build_discovery()`; add `refresh_discovery()` call in `run()` |
| `apps/predbat/tests/test_alphaess_api.py` | reporter tests, registered in `run_alphaess_api_tests()` |
| `apps/predbat/tests/test_solis.py` | reporter tests, registered in `run_solis_tests()` |
| `apps/predbat/tests/test_deye_api.py` | reporter tests, registered in `run_deye_api_tests()` |
| `apps/predbat/tests/test_sunsynk_config.py` | reporter tests, registered in `run_sunsynk_config_tests()` |
| `docs/discovery-catalogue.md` | the four new reporters |

Each reporter's tests go in the file that already holds that component's real-sample fixtures and its `run()`-driving harness.

---

## Task 1: The AlphaESS reporter

**Files:**
- Modify: `apps/predbat/alphaess.py` (`AlphaESSAPI`: add `build_discovery()`; one call in `run()`)
- Test: `apps/predbat/tests/test_alphaess_api.py`

**Interfaces:**
- Consumes: `inverter_record(device_id, *, ...)` from `coordinator.py`; `ComponentBase.refresh_discovery()`.
- Produces: `AlphaESSAPI.build_discovery() -> dict | None`.

**Background (verified):**
- `self.device_list` is a list of serial **strings** (`sysSn`) — not dicts like Fox's. Iterate `for sn in self.device_list:` and read `self.device_detail.get(sn, {})`, the raw `getEssList` entry.
- Only systems that passed `has_battery()` (`cobat > 0`) reach `device_list` and `device_detail`; a battery-less system (the VT1000 family) is dropped in `get_device_list()`. `automatic_config()` then registers **every** serial as `"AlphaESSCloud"` with no further test — so every reported system gets that `inverter_type`.
- The real `getEssList` sample is `ESS_LIST_SAMPLE` in `test_alphaess_api.py`: `popv` (PV rating, kW), `poinv` (inverter rating, kW), `cobat` (battery, kWh), `minv` (inverter model), `mbat` (battery model), plus `surplusCobat`, `usCapacity` and `emsStatus`.
- Pure accessors: `inverter_limit(sn)` returns `poinv * 1000` W; `battery_capacity(sn)` returns `cobat` kWh; `_as_float(value, default)` is a static helper. All read `device_detail` only.
- `self._ev_present` is `{sn: True|False}`, set only from live telemetry (`_apply_live_payload()`), never from the history fallback. Absent means not yet seen.
- Capabilities are static for the type (`INVERTER_DEF["AlphaESSCloud"]`): timed charge/discharge windows (`schedule`), target SoC (`target_soc`), reserve/discharge floor (`discharge_target`), rate as a power (`charge_rate_power`). There is no pause endpoint and no SoH field.
- `run()` publishes with `await self.publish_data()`, then returns early with `if first and not live_ok: return False` (deferring startup when the first telemetry poll fails), then `if first and self.automatic: await self.automatic_config()`.
- Not held, so not reported: firmware (the API exposes none); any station/site ID (every endpoint is keyed by `sysSn`); an AC-coupled verdict (`detect_ac_coupled()` infers it live, and `ALPHAESS_AC_COUPLED_MODELS` ships empty on purpose).
- Deliberately not reported although held: `emsStatus` (a status that changes — would re-file the report); `usCapacity`/`surplusCobat` (they fit "current SoC" and "configured usable depth" equally well, which is why `publish_data()` never maps them to SoC).

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_alphaess_api.py`, after `test_alphaess_run_defers_startup_without_telemetry()`. Add `from coordinator import validate_report` to the imports at the top of the file.

```python
def _alphaess_discovered(payload):
    """A MockAlphaESS whose get_device_list() has run against the given getEssList payload, as refresh_static() does."""
    client = MockAlphaESS()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, payload)))
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client.get_device_list())
    return client


def test_alphaess_catalogue_describes_each_system():
    """Each battery system becomes one AlphaESSCloud record carrying getEssList's own ratings and models.

    Built from the real ESS_LIST_SAMPLE plus the two battery-less VT1000 entries the discovery tests
    already use: get_device_list() drops those at discovery, so they must never be reported.
    """
    failed = False
    payload = list(ESS_LIST_SAMPLE) + [
        {"sysSn": "VT100000000001", "minv": "VT1000", "poinv": 0.8, "popv": 0.8, "cobat": 0},
        {"sysSn": "VT100000000002", "minv": "VT1000", "poinv": 0.8, "popv": 0.8},
    ]
    client = _alphaess_discovered(payload)
    report = client.build_discovery()
    by_id = {record["device_id"]: record for record in report["inverters"]}
    if set(by_id) != {"alphaess:AL70110230306xx", "alphaess:AL70110230302xx"}:
        print("ERROR: expected exactly the two battery systems, got {}".format(sorted(by_id)))
        return True
    first = by_id["alphaess:AL70110230306xx"]
    checks = [
        (first["inverter_type"], "AlphaESSCloud"),
        (first["composition"], "direct"),
        (first["functions"], ["solar", "battery"]),
        (first["hardware_ids"], {"serial": "AL70110230306xx"}),
        (first["info"], {"model": "SMILE5-INV", "battery_model": "SMILE-BAT-13.3P"}),
        (first["ratings"], {"inverter_w": 5000.0, "pv_w": 9000.0, "battery_kwh": 13.34}),
        (sorted(first["capabilities"]), ["charge_rate_power", "discharge_target", "schedule", "target_soc"]),
        (by_id["alphaess:AL70110230302xx"]["ratings"]["battery_kwh"], 10.1),
    ]
    for actual, expected in checks:
        if actual != expected:
            print("ERROR: expected {!r}, got {!r}".format(expected, actual))
            failed = True
    for record in report["inverters"]:
        if "account_ids" in record or "firmware" in record.get("info", {}):
            print("ERROR: AlphaESS holds no station ID or firmware, so none may be reported: {}".format(record))
            failed = True
        flat = str(record)
        for held_but_unreported in ("emsStatus", "Normal", "usCapacity", "surplusCobat"):
            if held_but_unreported in flat:
                print("ERROR: {} is a changing status or an ambiguous SoC figure and must not be reported: {}".format(held_but_unreported, record))
                failed = True
    return failed


def test_alphaess_catalogue_ev_charger_only_when_seen():
    """An EV charger is a flag only once live telemetry has seen one; an unseen charger reports nothing either way."""
    failed = False
    client = _alphaess_discovered(ESS_LIST_SAMPLE)
    sn = "AL70110230306xx"
    for present, expect_flag in ((True, True), (False, False), (None, False)):
        client._ev_present = {} if present is None else {sn: present}
        record = {r["device_id"]: r for r in client.build_discovery()["inverters"]}["alphaess:" + sn]
        has_flag = "ev_charger" in record.get("flags", [])
        if has_flag != expect_flag:
            print("ERROR: _ev_present {!r} should give ev_charger flag {}, got {}".format(present, expect_flag, has_flag))
            failed = True
    return failed


def test_alphaess_catalogue_none_before_discovery():
    """With nothing discovered there is nothing to describe, so nothing is reported."""
    client = MockAlphaESS()
    if client.build_discovery() is not None:
        print("ERROR: an empty device_list must report None, meaning ask again next cycle")
        return True
    return False


def test_alphaess_catalogue_round_trips_through_validate_report():
    """validate_report() hands every AlphaESS record back unchanged, so nothing is silently dropped."""
    failed = False
    client = _alphaess_discovered(ESS_LIST_SAMPLE)
    client._ev_present = {"AL70110230306xx": True}
    report = client.build_discovery()
    warnings = []
    cleaned = validate_report(report, "alphaess", warnings.append)
    if warnings:
        print("ERROR: validation warned: {}".format(warnings))
        failed = True
    if cleaned.get("inverters") != report["inverters"]:
        print("ERROR: validation changed the records:\n{}\n{}".format(report["inverters"], cleaned.get("inverters")))
        failed = True
    return failed


def test_alphaess_catalogue_filed_when_first_cycle_defers():
    """run() files the report even on a first cycle that defers startup for want of telemetry.

    Same setup as test_alphaess_run_defers_startup_without_telemetry: real getEssList, then every
    telemetry call answers "system offline", so run() publishes and then returns False from
    `if first and not live_ok:` before automatic_config(). That early exit is exactly the install
    whose dump most needs to say what hardware was found, so the report must already be filed.
    """
    failed = False
    client = MockAlphaESS(automatic=True)
    reports = []
    client.report_discovery = reports.append
    responses = [create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))] + [create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")) for _ in range(20)]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.run(seconds=0, first=True))
    if ok:
        print("ERROR: run() should defer startup (return False) with no telemetry")
        failed = True
    if len(reports) != 1 or len(reports[0]["inverters"]) != 2:
        print("ERROR: expected one report describing both systems despite the deferred startup, got {}".format(reports))
        failed = True
    return failed
```

Register all five in `run_alphaess_api_tests()`'s `(name, fn)` list:

```python
        ("catalogue_describes_each_system", test_alphaess_catalogue_describes_each_system),
        ("catalogue_ev_charger_only_when_seen", test_alphaess_catalogue_ev_charger_only_when_seen),
        ("catalogue_none_before_discovery", test_alphaess_catalogue_none_before_discovery),
        ("catalogue_round_trips", test_alphaess_catalogue_round_trips_through_validate_report),
        ("catalogue_filed_when_first_cycle_defers", test_alphaess_catalogue_filed_when_first_cycle_defers),
```

(The existing `discovery_ratings` entry tests device discovery, not the catalogue — leave it alone.)

- [ ] **Step 2: Run them and confirm they fail**

```bash
cd coverage
./run_all --test alphaess_api > /tmp/p2t1.log 2>&1; grep -E "FAILED|EXCEPTION|ERROR" /tmp/p2t1.log | head
```

Expected: every new test fails with `'MockAlphaESS' object has no attribute 'build_discovery'` (reported as EXCEPTION).

- [ ] **Step 3: Write `build_discovery()`**

In `apps/predbat/alphaess.py`, add `from coordinator import inverter_record` beside the other project imports, then add this method to `AlphaESSAPI`, immediately after `automatic_config()`:

```python
    def build_discovery(self):
        """
        Describe the discovered AlphaESS systems for the discovery catalogue.

        Reads only what get_device_list() already holds - self.device_list and self.device_detail -
        plus the EV-charger verdict _apply_live_payload() records, so this adds no API calls and
        cannot change what AlphaESS does. Reporting is independent of self.automatic: the catalogue
        records the hardware, and the report's own "automatic" flag says whether Predbat wired
        apps.yaml to it.

        self.device_list holds serial strings (sysSn), and only systems that passed has_battery()
        at discovery: a battery-less system (the VT1000 family, cobat 0 or missing) is dropped in
        get_device_list() and never reaches device_detail. automatic_config() applies no further
        test - it registers every serial in device_list as "AlphaESSCloud" - so inverter_type is set
        on every record, mirroring automatic_config() as the source of truth.

        Ratings are getEssList's own figures: poinv (kW) as inverter_w via inverter_limit(), popv
        (kW) as pv_w, cobat (kWh) as battery_kwh via battery_capacity(). cobat is one scalar per
        system, so there is no list of entries to mis-sum. A system reports "solar" only when popv
        says PV is attached.

        Deliberately not reported:
        - emsStatus: a status that changes. refresh_discovery() compares whole reports, so a
          changing value would re-file the report every time it moved.
        - usCapacity and surplusCobat: they fit "current SoC" and "configured usable depth" equally
          well, which is why publish_data() never maps them to SoC; reporting either as a rating
          would assert a meaning nobody knows.
        - firmware: the AlphaESS Open API exposes none.
        - account_ids: the API has no station, plant or site identifier; every endpoint is keyed by
          sysSn alone.
        - an AC-coupled verdict: detect_ac_coupled() infers it from live telemetry and
          ALPHAESS_AC_COUPLED_MODELS ships empty on purpose, so there is no held fact to report.

        An EV charger is reported as a flag only when _ev_present says True. That verdict comes from
        live telemetry alone, so a charger not yet seen reports nothing either way rather than a
        guess. No chargers record is invented for it: AlphaESS reports a charger's power, not its
        identity.

        Returns None when no system has been discovered yet, which refresh_discovery() treats as
        "nothing to report, ask again next cycle".
        """
        if not self.device_list:
            return None

        inverters = []
        for sn in self.device_list:
            detail = self.device_detail.get(sn, {}) or {}
            pv_kw = self._as_float(detail.get("popv"), 0.0)

            functions = ["solar", "battery"] if pv_kw > 0 else ["battery"]

            info = {}
            if detail.get("minv"):
                info["model"] = str(detail["minv"])
            if detail.get("mbat"):
                info["battery_model"] = str(detail["mbat"])

            ratings = {}
            inverter_w = self.inverter_limit(sn)
            if inverter_w > 0:
                ratings["inverter_w"] = inverter_w
            if pv_kw > 0:
                ratings["pv_w"] = pv_kw * 1000.0
            battery_kwh = self.battery_capacity(sn)
            if battery_kwh > 0:
                ratings["battery_kwh"] = battery_kwh

            flags = ["ev_charger"] if self._ev_present.get(sn) is True else []

            inverters.append(
                inverter_record(
                    "alphaess:{}".format(sn),
                    inverter_type="AlphaESSCloud",
                    composition="direct",
                    functions=functions,
                    capabilities=["schedule", "target_soc", "discharge_target", "charge_rate_power"],
                    flags=flags,
                    hardware_ids={"serial": sn},
                    info=info,
                    ratings=ratings,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 4: Wire it into `run()`**

In `AlphaESSAPI.run()`, find:

```python
        await self.publish_data()
```

followed (after a blank line) by `if first and not live_ok:`. Insert between them, at the method body's indentation:

```python
        # Filed right after this cycle's publish and BEFORE `if first and not live_ok:` below:
        # that branch returns False to defer startup when the first telemetry poll fails, and
        # automatic_config() follows it, so a report filed any later would never be filed on
        # exactly the installs whose dump most needs to say what hardware was found. (AlphaESS's
        # automatic_config() does not raise - the early return is the reason.) refresh_discovery()
        # owns the compare/retry/guard loop and never raises.
        self.refresh_discovery()
```

- [ ] **Step 5: Run the tests, then the full suite**

```bash
cd coverage
./run_all --test alphaess_api > /tmp/p2t1.log 2>&1; grep -E "FAILED|EXCEPTION|passed" /tmp/p2t1.log | tail -3
./run_all > /tmp/p2t1-full.log 2>&1; echo "exit=$?"; grep -E "All tests passed|Some tests failed" /tmp/p2t1-full.log | tail -1
```

Expected: `alphaess_api` passes; the full suite ends `All tests passed` in roughly two minutes.

- [ ] **Step 6: Prove the tests are load-bearing**

1. Move the `self.refresh_discovery()` call (and its comment) below the `if first and not live_ok:` block; confirm `catalogue_filed_when_first_cycle_defers` fails. Restore it.
2. Change `if self._ev_present.get(sn) is True` to `if self._ev_present.get(sn) is not False`; confirm `catalogue_ev_charger_only_when_seen` fails. Restore it.

Re-run `alphaess_api` green after each restore.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py
git commit -m "feat(discovery): report AlphaESS systems to the discovery catalogue

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: The Solis reporter

**Files:**
- Modify: `apps/predbat/solis.py` (`SolisAPI`: add `build_discovery()`; one call in `run()`)
- Test: `apps/predbat/tests/test_solis.py`

**Interfaces:**
- Consumes: `inverter_record(device_id, *, ...)`; `ComponentBase.refresh_discovery()`.
- Produces: `SolisAPI.build_discovery() -> dict | None`.

**Background (verified):**
- The fleet is `self.inverter_sn` (list of serial strings); `self.inverter_details` maps serial to the raw `inverterDetail` response. `self.cached_values` maps serial to `{cid: value}` register reads, values as strings.
- `automatic_config()` counts an inverter as a battery inverter it configures only when `not self._reports_no_battery(detail)` AND `detail["batteryHealthSoh"]` parses as a number (`0` is a valid reading). It then writes `"SolisCloud"` for those. `_reports_no_battery()` is a static, side-effect-free method.
- `automatic_config()` sets `pv_devices = [sn.lower() for sn in self.inverter_sn]` — **every** Solis inverter contributes PV, battery or not. So every record gets `"solar"`.
- Battery capacity is not an `inverterDetail` field. `publish_entities()` computes it as register 172 (`SOLIS_CID_BATTERY_CAPACITY`, Ah per battery) × `self.parallel_battery_count[sn]` × a voltage from `get_capacity_voltage(sn)` (the configured `solis_nominal_voltage`, or `None`) falling back to `get_nominal_voltage(sn)`, an **inference** (GH#5090: for an HV pack still a live, moving reading). So the derived kWh is only a stated fact when `get_capacity_voltage(sn)` is not `None`. Register 172 is held as a string, e.g. `"100"` in the existing fixtures.
- `get_capacity_voltage(sn)` is a pure read (`return self.nominal_pack_voltage`). `is_tou_v2_mode(sn)` is a pure read of `cached_values` (TOU V2 register layout when the mode register reads 43605).
- Inverter rating: `detail["power"]` with its unit in `detail["powerStr"]`, which `publish_entities()` defaults to `"kW"`. No correction helper.
- Model: `detail["productModel"]`. `detail["inverterName"]` is user-set and must not be reported.
- Capabilities for a battery inverter Predbat drives (`INVERTER_DEF["SolisCloud"]`): `schedule`, `target_soc`, `discharge_target`, `charge_rate_power`, and `soh` (it reports `batteryHealthSoh`). No pause.
- `run()` publishes with `if first or (seconds % 60 == 0): await self.publish_entities()`, then gates `automatic_config()` on `self.automatic and self.inverter_sn and not self.automatic_config_done`. `run()`'s only early return is inside `if first:`, before any publish, when discovery found nothing.
- Not held: firmware; any station/plant/account ID.

**Real fixtures:** `_DETAIL_WITH_BATTERY` and `_DETAIL_NO_BATTERY` in `test_solis.py` were captured from a live two-inverter account. Neither carries `productModel`, `power` or `powerStr`; the only sample with `productModel` is `test_publish_entities`'s `"Solis-5G-Hybrid"`, and the repository holds **no captured `power`/`powerStr` pair at all**. The test therefore adds `"productModel": "Solis-5G-Hybrid"` from that sample and `"power": 5.0, "powerStr": "kW"` **derived** from it — the model name's "5G" rating and the API's own `<field>Str: "kW"` convention seen on `pacStr`/`eTotalStr` in the same sample. The test docstring must say so.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_solis.py`, after `_run_automatic_config()`. Add `from coordinator import validate_report` to the imports and make sure `SOLIS_CID_BATTERY_CAPACITY` and `SOLIS_CID_TOU_V2_MODE` are imported from `solis`.

```python
def _solis_fleet():
    """A MockSolisAPI holding one captured battery inverter and one captured PV-only inverter.

    Both details are the live-captured _DETAIL_WITH_BATTERY / _DETAIL_NO_BATTERY. The battery
    inverter additionally carries productModel from test_publish_entities' sample and a DERIVED
    power/powerStr pair: the repository holds no captured power field, so 5.0 kW follows the
    sample's "Solis-5G-Hybrid" rating and the API's <field>Str "kW" convention.
    """
    api = MockSolisAPI()
    api.inverter_sn = ["BAT001", "PV001"]
    api.inverter_details = {
        "BAT001": dict(_DETAIL_WITH_BATTERY, productModel="Solis-5G-Hybrid", power=5.0, powerStr="kW"),
        "PV001": dict(_DETAIL_NO_BATTERY),
    }
    api.cached_values = {"BAT001": {SOLIS_CID_BATTERY_CAPACITY: "100", SOLIS_CID_TOU_V2_MODE: "43605"}}
    api.parallel_battery_count = {"BAT001": 2}
    api.nominal_pack_voltage = None
    return api


def test_solis_catalogue_describes_battery_and_pv_only():
    """A battery inverter is a SolisCloud inverter; a PV-only one reports solar with no inverter_type."""
    report = _solis_fleet().build_discovery()
    by_id = {record["device_id"]: record for record in report["inverters"]}
    assert set(by_id) == {"solis:BAT001", "solis:PV001"}, sorted(by_id)
    battery = by_id["solis:BAT001"]
    assert battery["inverter_type"] == "SolisCloud"
    assert battery["composition"] == "direct"
    assert battery["functions"] == ["solar", "battery"]
    assert battery["hardware_ids"] == {"serial": "BAT001"}
    assert battery["info"] == {"model": "Solis-5G-Hybrid"}
    assert sorted(battery["capabilities"]) == ["charge_rate_power", "discharge_target", "schedule", "soh", "target_soc"]
    assert battery["flags"] == ["tou_v2"]
    pv = by_id["solis:PV001"]
    assert "inverter_type" not in pv, "a PV-only inverter is not one automatic_config() configures"
    assert pv["functions"] == ["solar"], "every Solis inverter feeds the PV totals (automatic_config's pv_devices)"
    assert "capabilities" not in pv
    return False


def test_solis_catalogue_battery_ratings_carry_only_stated_facts():
    """Register 172's Ah and the pack count are always reported; kWh only when the voltage is configured.

    GH#5090: without solis_nominal_voltage the voltage is inferred, and for an HV pack still a live
    reading that moves dump to dump, so a derived kWh would present an estimate as a rating.
    """
    api = _solis_fleet()
    ratings = {r["device_id"]: r for r in api.build_discovery()["inverters"]}["solis:BAT001"]["ratings"]
    # 100 Ah per pack (register 172) x 2 packs: battery_capacity_ah is the bank total (Amendments)
    assert ratings == {"inverter_w": 5000.0, "battery_capacity_ah": 200.0, "battery_pack_count": 2}, ratings
    api.nominal_pack_voltage = 51.2
    ratings = {r["device_id"]: r for r in api.build_discovery()["inverters"]}["solis:BAT001"]["ratings"]
    assert ratings["battery_kwh"] == round(100.0 * 2 * 51.2 / 1000.0, 2), ratings
    return False


async def test_solis_catalogue_inverter_type_tracks_automatic_config():
    """inverter_type is set exactly where the real automatic_config() would configure the inverter.

    Uses the existing _run_automatic_config() helper, which runs the real automatic_config() over the
    captured details and returns the set_arg_auto args it recorded. "battery but no SoH field" is the
    case the predicate excludes despite a battery: automatic_config() needs batteryHealthSoh to parse.
    """
    cases = {
        "with battery": _DETAIL_WITH_BATTERY,
        "no battery": _DETAIL_NO_BATTERY,
        "no battery, alt firmware": _DETAIL_NO_BATTERY_ALT_FIRMWARE,
        "with battery, alt firmware": _DETAIL_WITH_BATTERY_ALT_FIRMWARE,
        "battery but no SoH field": {key: value for key, value in _DETAIL_WITH_BATTERY.items() if key != "batteryHealthSoh"},
    }
    for label, detail in cases.items():
        recorded, api = await _run_automatic_config({"INV001": dict(detail)})
        record = api.build_discovery()["inverters"][0]
        assert ("inverter_type" in record) == ("inverter_type" in recorded), "{}: catalogue says {!r}, automatic_config configured {!r}".format(label, record.get("inverter_type"), recorded.get("inverter_type"))
    return False


def test_solis_catalogue_never_reports_the_inverter_name():
    """inverterName is user-set free text and must never reach the report."""
    api = _solis_fleet()
    api.inverter_details["BAT001"]["inverterName"] = "12 Acacia Avenue"
    assert "Acacia" not in str(api.build_discovery()), "inverterName leaked into the report"
    return False


def test_solis_catalogue_none_before_discovery():
    """With no inverters there is nothing to describe."""
    assert MockSolisAPI().build_discovery() is None
    return False


def test_solis_catalogue_round_trips_through_validate_report():
    """validate_report() hands every Solis record back unchanged."""
    api = _solis_fleet()
    api.nominal_pack_voltage = 51.2
    report = api.build_discovery()
    warnings = []
    cleaned = validate_report(report, "solis", warnings.append)
    assert not warnings, warnings
    assert cleaned["inverters"] == report["inverters"], (report["inverters"], cleaned["inverters"])
    return False


async def test_solis_catalogue_filed_even_when_automatic_config_configures_nothing():
    """run() files the report on a cycle where the real automatic_config() finds no battery inverter.

    Solis's automatic_config() returns False rather than raising, and is gated on
    automatic_config_done; the report must not depend on either. Built on _make_run_api() and
    test_run_first_success(): the discovered inverter has the captured no-battery detail.
    """
    sn = "INV001"
    api = _make_run_api(configured_sns=[sn], automatic=True)
    # _make_run_api() stubs automatic_config(); put the real one back so it genuinely runs.
    api.automatic_config = SolisAPI.automatic_config.__get__(api)
    configured = {}
    api.set_arg_auto = lambda key, value: configured.__setitem__(key, value)

    async def mock_get_inverter_list():
        """Discover the one inverter."""
        return [{"sn": sn}]

    api.get_inverter_list = mock_get_inverter_list
    # _make_run_api()'s fetch_inverter_details stub only records the call, so this detail survives.
    api.inverter_details = {sn: dict(_DETAIL_NO_BATTERY)}
    reports = []
    api.report_discovery = reports.append
    with patch.object(solis_module.aiohttp, "ClientSession", return_value=_FakeAiohttpSession()), patch.object(solis_module.aiohttp, "ClientTimeout", return_value=None):
        await api.run(0, True)
    # automatic_config() found no battery inverter. It returns details_read (True here), so
    # automatic_config_done DOES become True - "done, nothing to configure" - which is why this
    # asserts on what it configured, not on that flag.
    assert "inverter_type" not in configured, "automatic_config() found no battery inverter, so it must configure nothing: {}".format(configured)
    assert len(reports) == 1 and reports[0]["inverters"][0]["device_id"] == "solis:" + sn, reports
    return False
```

Register them in `run_solis_tests()` beside the other `failed |=` lines:

```python
        failed |= test_solis_catalogue_describes_battery_and_pv_only()
        failed |= test_solis_catalogue_battery_ratings_carry_only_stated_facts()
        failed |= asyncio.run(test_solis_catalogue_inverter_type_tracks_automatic_config())
        failed |= test_solis_catalogue_never_reports_the_inverter_name()
        failed |= test_solis_catalogue_none_before_discovery()
        failed |= test_solis_catalogue_round_trips_through_validate_report()
        failed |= asyncio.run(test_solis_catalogue_filed_even_when_automatic_config_configures_nothing())
```

- [ ] **Step 2: Run them and confirm they fail**

```bash
cd coverage
./run_all --test solis > /tmp/p2t2.log 2>&1; grep -E "AttributeError|Error|FAIL" /tmp/p2t2.log | head
```

Expected: `'MockSolisAPI' object has no attribute 'build_discovery'`.

- [ ] **Step 3: Write `build_discovery()`**

In `apps/predbat/solis.py`, add `from coordinator import inverter_record` beside the other project imports, then add to `SolisAPI`, immediately after `automatic_config()`:

```python
    def build_discovery(self):
        """
        Describe the discovered Solis inverters for the discovery catalogue.

        Reads only what the component already holds - self.inverter_sn, self.inverter_details,
        self.cached_values and self.parallel_battery_count - so this adds no API calls and cannot
        change what Solis does. Reporting is independent of self.automatic.

        inverter_type "SolisCloud" is set with automatic_config()'s own test for a battery inverter
        it configures, duplicated here rather than shared so the control path is untouched:
        _reports_no_battery() must be false AND batteryHealthSoh must parse as a number (0 is a
        valid reading). "battery" in functions is the hardware fact alone - details read and
        Solis Cloud not saying "no battery" - so a battery inverter automatic_config() declines is
        still described. Every inverter reports "solar": automatic_config() puts every inverter in
        pv_devices, battery or not.

        Battery ratings carry only stated facts. Register 172 (SOLIS_CID_BATTERY_CAPACITY) is the
        per-battery Ah; battery_capacity_ah reports the bank total - register 172 x
        parallel_battery_count, the same product publish_entities() uses - so it means the same
        thing here as on every other reporter. battery_pack_count carries the pack count alongside
        it, and both are always reported for a battery inverter. A kWh figure is reported only when
        get_capacity_voltage() returns the configured solis_nominal_voltage: otherwise
        publish_entities() falls back to get_nominal_voltage(), an inference that for an HV pack is
        still a live reading moving dump to dump (GH#5090), and a derived kWh would present that
        estimate as a rating.

        The inverter rating is inverterDetail's power in powerStr's unit - defaulted to "kW"
        exactly as publish_entities() does - and is reported only for a unit this code knows how
        to convert, never guessed.

        Deliberately not reported: inverterName (user-set free text that can hold an address);
        firmware (inverterDetail carries none); any station or account ID (none is held).
        The TOU V2 register layout is reported as the flag "tou_v2" - how the inverter is driven,
        not something it can do.

        Returns None when no inverter has been discovered yet.
        """
        if not self.inverter_sn:
            return None

        inverters = []
        for sn in self.inverter_sn:
            detail = self.inverter_details.get(sn, {}) or {}
            has_battery = bool(detail) and not self._reports_no_battery(detail)
            try:
                float(detail.get("batteryHealthSoh"))
                reports_soh = True
            except (TypeError, ValueError):
                reports_soh = False
            # automatic_config()'s own predicate for an inverter it configures - the source of truth.
            drives_it = not self._reports_no_battery(detail) and reports_soh

            info = {}
            if detail.get("productModel"):
                info["model"] = str(detail["productModel"])

            ratings = {}
            try:
                power = float(detail.get("power"))
            except (TypeError, ValueError):
                power = 0.0
            power_unit = str(detail.get("powerStr", "kW")).strip()
            if power > 0 and power_unit == "kW":
                ratings["inverter_w"] = power * 1000.0
            elif power > 0 and power_unit == "W":
                ratings["inverter_w"] = power
            if has_battery:
                try:
                    capacity_ah = float(self.cached_values.get(sn, {}).get(SOLIS_CID_BATTERY_CAPACITY))
                except (TypeError, ValueError):
                    capacity_ah = 0.0
                if capacity_ah > 0:
                    pack_count = self.parallel_battery_count.get(sn, 1)
                    ratings["battery_capacity_ah"] = capacity_ah * pack_count
                    ratings["battery_pack_count"] = pack_count
                    configured_volts = self.get_capacity_voltage(sn)
                    if configured_volts:
                        ratings["battery_kwh"] = round(capacity_ah * pack_count * configured_volts / 1000.0, 2)

            inverters.append(
                inverter_record(
                    "solis:{}".format(sn),
                    inverter_type="SolisCloud" if drives_it else None,
                    composition="direct",
                    functions=["solar", "battery"] if has_battery else ["solar"],
                    capabilities=["schedule", "target_soc", "discharge_target", "charge_rate_power", "soh"] if drives_it else None,
                    flags=["tou_v2"] if self.is_tou_v2_mode(sn) else None,
                    hardware_ids={"serial": sn},
                    info=info,
                    ratings=ratings,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 4: Wire it into `run()`**

In `SolisAPI.run()`, find:

```python
        if first or (seconds % 60 == 0):
            await self.publish_entities()
```

followed by the comment beginning `# Auto-configure Predbat if enabled.` Insert between them:

```python
        # Filed every cycle, right after this cycle's publish and before the automatic_config()
        # gate below. Solis's automatic_config() never raises, but it runs only when
        # self.automatic is set and automatic_config_done is not, so a report filed after it
        # would depend on unrelated auto-config state. refresh_discovery() owns the
        # compare/retry/guard loop and never raises.
        self.refresh_discovery()
```

- [ ] **Step 5: Run the tests, then the full suite**

```bash
cd coverage
./run_all --test solis > /tmp/p2t2.log 2>&1; grep -E "Error|FAIL|passed" /tmp/p2t2.log | tail -3
./run_all > /tmp/p2t2-full.log 2>&1; echo "exit=$?"; grep -E "All tests passed|Some tests failed" /tmp/p2t2-full.log | tail -1
```

- [ ] **Step 6: Prove the tests are load-bearing**

1. Report `battery_kwh` unconditionally (drop the `if configured_volts:` guard, using `self.get_nominal_voltage(sn)` when not configured); confirm `test_solis_catalogue_battery_ratings_carry_only_stated_facts` fails. Restore.
2. Drop `and reports_soh` from `drives_it`; confirm `test_solis_catalogue_inverter_type_tracks_automatic_config` fails. Restore.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/solis.py apps/predbat/tests/test_solis.py
git commit -m "feat(discovery): report Solis inverters to the discovery catalogue

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: The Deye reporter

**Files:**
- Modify: `apps/predbat/deye.py` (`DeyeAPI`: add `build_discovery()`; one call in `run()`)
- Test: `apps/predbat/tests/test_deye_api.py`

**Interfaces:**
- Consumes: `inverter_record(device_id, *, ...)`; `ComponentBase.refresh_discovery()`.
- Produces: `DeyeAPI.build_discovery() -> dict | None`.

**Background (verified):**
- `self.device_list` is a list of serial strings (`deviceSn`), already filtered to `deviceType == "INVERTER"` in `get_device_list()`. `automatic_config()` registers **every** serial as `"DeyeCloud"` with no further test. Neither discovery nor `automatic_config()` can tell a PV-only unit from a hybrid: `automatic_config()` binds both PV and battery entities for every device. The reporter mirrors that — `functions: ["solar", "battery"]` on every record — which is exactly what Predbat believes about the device, and is the fact a maintainer needs when a PV-only unit has been configured as a battery inverter.
- `self.station_ids` is a flat list of station IDs; `get_device_list()` queries every station at once and flattens the result, so which device belongs to which station is not held. A station ID can be attributed to every device only when the account has exactly one station.
- Pure accessors: `battery_capacity(sn)` (kWh — `config/battery`'s `battCapacity` Ah scaled by `device_pack_voltage`, else `device_capacity`); `_battery_config_value(sn, "capacity")` (the raw `battCapacity` Ah); `_as_float(value, default)`. `self.device_rated_power[sn]` is `device/latest`'s `RatedPower` in W, written only when present.
- **Never call `derive_battery_capacity()`** from the reporter: it logs and writes `device_pack_voltage`/`device_capacity`. It is the producer `fetch_device_data()` calls; `battery_capacity()` is the accessor.
- Capabilities (`INVERTER_DEF["DeyeCloud"]`): `schedule` (6-slot TOU), `target_soc`, `discharge_target`, `charge_rate_power`. No pause, no SoH, no export limit.
- `run()` publishes (`await self.publish_data()` then a `for sn in self.device_list: await self.publish_schedule_settings_ha(sn)` loop), drains pending control orders, reconciles, returns early with `if first and not live_ok: return False`, then `if first and self.automatic: await self.automatic_config()`.
- Not held: model, firmware (`device/measurePoints` and `station/latest` are fetched in `refresh_static()` for debug logging and discarded — do not read them).
- The rule "test a device the predicate excludes": for Deye the exclusion happens at discovery, not in `automatic_config()` — `get_device_list()` drops anything whose `deviceType` is not `"INVERTER"`, and the existing `test_get_device_list_filters_inverters` already proves a `METER` never reaches `device_list`. There is nothing further to exclude, so the reporter tests do not repeat it.

**Real fixtures:** `LIVE_DATA_LIST` in `test_deye_api.py` is trimmed verbatim from a live `device/latest` response (`RatedPower 8000 W`, `BatteryRatedCapacity 1200 Ah`, `BMSChargeVoltage 57.60 V`). The live `config/battery` response is quoted in `deye_const.py`: `{"maxChargeCurrent": 185, "maxDischargeCurrent": 185, "battLowCapacity": 14, "battShutDownCapacity": 9, "battCapacity": 1200}`. The tests derive every figure through the component's own code from these, never by hand.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_deye_api.py`, after `test_run_first_cycle_publishes_and_configures()`. Add `from coordinator import validate_report` to the imports.

```python
DEYE_LIVE_BATTERY_CONFIG = {"maxChargeCurrent": 185, "maxDischargeCurrent": 185, "battLowCapacity": 14, "battShutDownCapacity": 9, "battCapacity": 1200}


def _deye_fleet(station_ids=(10,)):
    """A MockDeye holding one inverter populated from the live device/latest and config/battery samples.

    The pack voltage and derived capacity come from the component's own derive_battery_capacity()
    run on LIVE_DATA_LIST - test setup is allowed its side effects; the reporter never calls it.
    """
    d = MockDeye()
    sn = "INV1"
    d.device_list = [sn]
    d.station_ids = list(station_ids)
    flat = d._datalist_to_dict(LIVE_DATA_LIST)
    d.device_rated_power[sn] = d._as_float(flat.get("RatedPower"), 0.0)
    d.device_battery_config[sn] = dict(DEYE_LIVE_BATTERY_CONFIG)
    d.derive_battery_capacity(sn, flat)
    return d


def test_deye_catalogue_describes_each_inverter():
    """Each inverter is a DeyeCloud record with the live sample's own ratings."""
    failed = False
    d = _deye_fleet()
    record = d.build_discovery()["inverters"][0]
    checks = [
        (record["device_id"], "deye:INV1"),
        (record["inverter_type"], "DeyeCloud"),
        (record["composition"], "direct"),
        (record["functions"], ["solar", "battery"]),
        (record["hardware_ids"], {"serial": "INV1"}),
        (record["account_ids"], {"station_id": 10}),
        (sorted(record["capabilities"]), ["charge_rate_power", "discharge_target", "schedule", "target_soc"]),
        (record["ratings"]["inverter_w"], 8000.0),
        (record["ratings"]["battery_capacity_ah"], 1200.0),
        (record["ratings"]["battery_kwh"], d.battery_capacity("INV1")),
    ]
    for actual, expected in checks:
        if actual != expected:
            print("ERROR: expected {!r}, got {!r}".format(expected, actual))
            failed = True
    if "info" in record:
        print("ERROR: Deye holds no model or firmware, so info must be absent: {}".format(record["info"]))
        failed = True
    return failed


def test_deye_catalogue_station_id_only_when_unambiguous():
    """A station ID is attributed only when the account has exactly one station: devices are not held per station."""
    failed = False
    for stations, expect in (((10,), {"station_id": 10}), ((10, 11), None), ((), None)):
        record = _deye_fleet(station_ids=stations).build_discovery()["inverters"][0]
        if record.get("account_ids") != expect:
            print("ERROR: stations {} should give account_ids {!r}, got {!r}".format(stations, expect, record.get("account_ids")))
            failed = True
    return failed


def test_deye_catalogue_does_not_derive_capacity():
    """The reporter reads battery_capacity() and never calls derive_battery_capacity(), which logs and writes state."""
    d = _deye_fleet()
    d.derive_battery_capacity = MagicMock(side_effect=AssertionError("the reporter must not call derive_battery_capacity()"))
    d.log_messages.clear()
    d.build_discovery()
    if d.log_messages:
        print("ERROR: build_discovery() logged: {}".format(d.log_messages))
        return True
    return False


def test_deye_catalogue_none_before_discovery():
    """With no inverters there is nothing to describe."""
    if MockDeye().build_discovery() is not None:
        print("ERROR: an empty device_list must report None")
        return True
    return False


def test_deye_catalogue_round_trips_through_validate_report():
    """validate_report() hands every Deye record back unchanged."""
    report = _deye_fleet().build_discovery()
    warnings = []
    cleaned = validate_report(report, "deye", warnings.append)
    if warnings or cleaned.get("inverters") != report["inverters"]:
        print("ERROR: validation changed or warned on the report: {} {}".format(warnings, cleaned.get("inverters")))
        return True
    return False
```

Then add the wiring test. It is `test_run_first_cycle_publishes_and_configures()` with the live poll made to fail: `refresh_live()` sets `live_ok` only when `fetch_device_data(sn)` returns something truthy, so a fake returning `{}` sends `run()` into `if first and not live_ok: return False`.

```python
def test_deye_catalogue_filed_when_first_cycle_defers():
    """run() files the report even on a first cycle that defers startup because the live poll failed.

    `if first and not live_ok: return False` - and automatic_config() after it - would otherwise
    swallow the report on exactly the installs whose dump most needs to say what hardware was found.
    Built on test_run_first_cycle_publishes_and_configures(); only fetch_device_data() differs.
    """
    from unittest.mock import patch

    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "tok"
    d.automatic = True
    seq = {"configured": 0}
    reports = []
    d.report_discovery = reports.append

    async def fake_dev_list():
        """Discover one inverter."""
        d.device_list = ["INV1"]
        return ["INV1"]

    async def fake_data(sn):
        """Fail the live poll: refresh_live() treats a falsy result as no data."""
        return {}

    async def fake_batt(sn):
        """Return no battery config."""
        return {}

    async def fake_publish():
        """Publish nothing."""

    async def fake_pub_sched(sn):
        """Publish no schedule."""

    async def fake_get_sched(sn):
        """Read no schedule."""
        return {}

    async def fake_auto():
        """Record that automatic_config() ran."""
        seq["configured"] += 1

    with patch.multiple(
        d,
        get_device_list=fake_dev_list,
        fetch_device_data=fake_data,
        fetch_battery_config=fake_batt,
        publish_data=fake_publish,
        publish_schedule_settings_ha=fake_pub_sched,
        get_schedule_settings_ha=fake_get_sched,
        automatic_config=fake_auto,
    ):
        ok = run_async_local(d.run(0, True))
    if ok:
        print("ERROR: run() should defer startup (return False) when the first live poll fails")
        failed = True
    if seq["configured"]:
        print("ERROR: automatic_config() must not run on a deferred first cycle")
        failed = True
    if len(reports) != 1 or reports[0]["inverters"][0]["device_id"] != "deye:INV1":
        print("ERROR: the report must be filed before the deferring return, got {}".format(reports))
        failed = True
    return failed
```

Register all six in `run_deye_api_tests()`'s `(name, fn)` list:

```python
        ("catalogue_describes_each_inverter", test_deye_catalogue_describes_each_inverter),
        ("catalogue_station_id_only_when_unambiguous", test_deye_catalogue_station_id_only_when_unambiguous),
        ("catalogue_does_not_derive_capacity", test_deye_catalogue_does_not_derive_capacity),
        ("catalogue_none_before_discovery", test_deye_catalogue_none_before_discovery),
        ("catalogue_round_trips", test_deye_catalogue_round_trips_through_validate_report),
        ("catalogue_filed_when_first_cycle_defers", test_deye_catalogue_filed_when_first_cycle_defers),
```

`MagicMock` and `patch` are already imported at the top of `test_deye_api.py`.

- [ ] **Step 2: Run them and confirm they fail**

```bash
cd coverage
./run_all --test deye_api > /tmp/p2t3.log 2>&1; grep -E "FAILED|EXCEPTION|ERROR" /tmp/p2t3.log | head
```

Expected: `'MockDeye' object has no attribute 'build_discovery'`.

- [ ] **Step 3: Write `build_discovery()`**

In `apps/predbat/deye.py`, add `from coordinator import inverter_record` beside the other project imports, then add to `DeyeAPI`, immediately after `automatic_config()`:

```python
    def build_discovery(self):
        """
        Describe the discovered Deye inverters for the discovery catalogue.

        Reads only state the component already holds - device_list, station_ids,
        device_rated_power and the battery accessors - so this adds no API calls and cannot
        change what Deye does. Reporting is independent of self.automatic.

        automatic_config() registers every serial in device_list (already filtered to deviceType
        "INVERTER") as "DeyeCloud" with no further test, and binds both PV and battery entities for
        each. Deye's API offers no way to tell a PV-only unit from a hybrid, so neither can this:
        every record carries inverter_type "DeyeCloud" and functions solar and battery, mirroring
        automatic_config() as the source of truth. That is exactly what Predbat believes about the
        device - and the fact a maintainer needs when a PV-only unit has been configured as a
        battery inverter.

        Ratings: RatedPower (W) as inverter_w; battery_capacity() (kWh) as battery_kwh; and
        config/battery's battCapacity as battery_capacity_ah, the raw Ah the API returned, so a
        reader can check the kWh against its inputs. derive_battery_capacity() is never called
        here: it logs and writes device_pack_voltage/device_capacity, whereas battery_capacity()
        only reads them.

        station_ids goes in account_ids only when the account has exactly one station:
        get_device_list() queries every station at once and flattens the result, so which device
        belongs to which station is not held, and attributing one of several would be a guess.

        Deliberately not reported: model and firmware (not held - device/measurePoints and
        station/latest are fetched for debug logging and discarded, and reading them would be a
        new API dependency).

        Returns None when no inverter has been discovered yet.
        """
        if not self.device_list:
            return None

        account_ids = {"station_id": self.station_ids[0]} if len(self.station_ids) == 1 else None

        inverters = []
        for sn in self.device_list:
            ratings = {}
            rated_w = self._as_float(self.device_rated_power.get(sn), 0.0)
            if rated_w > 0:
                ratings["inverter_w"] = rated_w
            battery_kwh = self.battery_capacity(sn)
            if battery_kwh > 0:
                ratings["battery_kwh"] = battery_kwh
            configured_ah = self._battery_config_value(sn, "capacity")
            if configured_ah > 0:
                ratings["battery_capacity_ah"] = configured_ah

            inverters.append(
                inverter_record(
                    "deye:{}".format(sn),
                    inverter_type="DeyeCloud",
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=["schedule", "target_soc", "discharge_target", "charge_rate_power"],
                    hardware_ids={"serial": sn},
                    account_ids=account_ids,
                    ratings=ratings,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

- [ ] **Step 4: Wire it into `run()`**

In `DeyeAPI.run()`, find the loop:

```python
        for sn in self.device_list:
            await self.publish_schedule_settings_ha(sn)
```

that follows `await self.publish_data()`. Insert immediately after that loop, before the comment that begins `# Drain any control orders`:

```python
        # Filed right after this cycle's publish and BEFORE `if first and not live_ok:` below:
        # that branch returns False to defer startup when the first live poll fails, and
        # automatic_config() follows it, so a report filed any later would never be filed on
        # exactly the installs whose dump most needs to say what hardware was found. (Deye's
        # automatic_config() does not raise - the early return is the reason.) refresh_discovery()
        # owns the compare/retry/guard loop and never raises.
        self.refresh_discovery()
```

- [ ] **Step 5: Run the tests, then the full suite**

```bash
cd coverage
./run_all --test deye_api > /tmp/p2t3.log 2>&1; grep -E "FAILED|EXCEPTION|passed" /tmp/p2t3.log | tail -3
./run_all > /tmp/p2t3-full.log 2>&1; echo "exit=$?"; grep -E "All tests passed|Some tests failed" /tmp/p2t3-full.log | tail -1
```

- [ ] **Step 6: Prove the tests are load-bearing**

1. Move the call below the `if first and not live_ok:` block; confirm `catalogue_filed_when_first_cycle_defers` fails. Restore.
2. Make `account_ids` use `self.station_ids[0]` whenever the list is non-empty; confirm `catalogue_station_id_only_when_unambiguous` fails. Restore.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_api.py
git commit -m "feat(discovery): report Deye inverters to the discovery catalogue

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: The Sunsynk reporter

**Files:**
- Modify: `apps/predbat/sunsynk.py` (`SunsynkAPI`: add `build_discovery()`; one call in `run()`)
- Test: `apps/predbat/tests/test_sunsynk_config.py`

**Interfaces:**
- Consumes: `inverter_record(device_id, *, ...)`; `ComponentBase.refresh_discovery()`.
- Produces: `SunsynkAPI.build_discovery() -> dict | None`.

**Background (verified):**
- `self.device_list` is a list of serial strings (`sn`). Discovery applies **no** device-type filter, and `automatic_config()` registers every serial as `"SunsynkCloud"` with no further test, binding PV and battery entities for each. As with Deye, the reporter mirrors that: `inverter_type` and `functions: ["solar", "battery"]` on every record.
- There is no station or plant grouping at all, so there is no `account_ids`.
- Pure accessors: `inverter_limit(sn)` (`device_rated_power`, from `inverter_detail`'s `ratePower`, W); `battery_capacity(sn)` (kWh — `device_values[sn]["capacity"]` Ah × `nominal_pack_voltage(chargeVolt)`, 0 when underivable); `export_limit(sn)` (`min` of the rating and `settings["pvMaxLimit"]` over whichever are positive); `_as_float(value)`. `nominal_pack_voltage()` does not log.
- `automatic_config()` binds `export_limit` only when `self.export_limit(sn) > 0`, so the capability `export_limit` (the Fox precedent; the spec has no token for it) is reported per device on exactly that evidence. Deye has no such concept.
- Capabilities otherwise: `schedule`, `target_soc`, `discharge_target`, `charge_rate_power`.
- `run()` publishes with `await self.publish_data()`, then returns early with `if first and not live_ok: return False`, then `if first and self.automatic: await self.automatic_config()`.
- Not held: model, firmware, any station ID.
- The rule "test a device the predicate excludes" has nothing to bite on: Sunsynk excludes no device at discovery or in `automatic_config()`. Say so in `build_discovery()`'s docstring rather than invent a case.

**Real fixtures, and their limits:** `sunsynk_const.py` states plainly that no one on the project has a live Sunsynk account and many field names are inferred. The values used here are only those it records as **confirmed live**: `ratePower 8000` with settings `pvMaxLimit 7000`; `chargeVolt 58.4` → 16 cells, with `200` Ah → 10.24 kWh; and serial `2405116013`, the inverter the live telemetry sample was captured from. The test docstring must say the fixture is assembled from those confirmed values rather than from one captured response.

- [ ] **Step 1: Write the failing tests**

Add to `apps/predbat/tests/test_sunsynk_config.py`, after `test_run_first_cycle_polls_and_publishes()`. Add `from coordinator import validate_report` to the imports.

```python
SUNSYNK_LIVE_SERIAL = "2405116013"


def _sunsynk_fleet():
    """A MockSunsynk holding one inverter built from sunsynk_const.py's CONFIRMED-live values only.

    No captured inverter_detail or settings response exists in the repository (sunsynk_const.py
    records that nobody on the project has a live account), so this is assembled from the values it
    does mark confirmed live: ratePower 8000 with pvMaxLimit 7000; chargeVolt 58.4 with a 200 Ah pack
    (10.24 kWh); and the serial the live telemetry sample was taken from.
    """
    s = MockSunsynk()
    sn = SUNSYNK_LIVE_SERIAL
    s.device_list = [sn]
    s.device_rated_power = {sn: 8000.0}
    s.device_values = {sn: {"capacity": 200, "chargeVolt": 58.4}}
    s.device_settings = {sn: {"pvMaxLimit": 7000}}
    return s


def test_sunsynk_catalogue_describes_each_inverter():
    """Each inverter is a SunsynkCloud record with the confirmed-live ratings."""
    failed = False
    s = _sunsynk_fleet()
    record = s.build_discovery()["inverters"][0]
    checks = [
        (record["device_id"], "sunsynk:" + SUNSYNK_LIVE_SERIAL),
        (record["inverter_type"], "SunsynkCloud"),
        (record["composition"], "direct"),
        (record["functions"], ["solar", "battery"]),
        (record["hardware_ids"], {"serial": SUNSYNK_LIVE_SERIAL}),
        (sorted(record["capabilities"]), ["charge_rate_power", "discharge_target", "export_limit", "schedule", "target_soc"]),
        (record["ratings"]["inverter_w"], 8000.0),
        (record["ratings"]["battery_capacity_ah"], 200.0),
        (record["ratings"]["battery_kwh"], s.battery_capacity(SUNSYNK_LIVE_SERIAL)),
    ]
    for actual, expected in checks:
        if actual != expected:
            print("ERROR: expected {!r}, got {!r}".format(expected, actual))
            failed = True
    if abs(record["ratings"]["battery_kwh"] - 10.24) > 0.01:
        print("ERROR: the confirmed-live pack is 10.24 kWh, got {}".format(record["ratings"]["battery_kwh"]))
        failed = True
    for absent in ("info", "account_ids"):
        if absent in record:
            print("ERROR: Sunsynk holds no model, firmware or station ID, so {} must be absent: {}".format(absent, record[absent]))
            failed = True
    return failed


def test_sunsynk_catalogue_export_limit_only_on_evidence():
    """export_limit is a capability exactly where automatic_config() would bind it: export_limit() > 0."""
    s = _sunsynk_fleet()
    s.device_rated_power = {}
    s.device_settings = {}
    record = s.build_discovery()["inverters"][0]
    if "export_limit" in record.get("capabilities", []):
        print("ERROR: with no rating and no pvMaxLimit, export_limit() is 0 and the capability must be absent")
        return True
    return False


def test_sunsynk_catalogue_none_before_discovery():
    """With no inverters there is nothing to describe."""
    if MockSunsynk().build_discovery() is not None:
        print("ERROR: an empty device_list must report None")
        return True
    return False


def test_sunsynk_catalogue_round_trips_through_validate_report():
    """validate_report() hands every Sunsynk record back unchanged."""
    report = _sunsynk_fleet().build_discovery()
    warnings = []
    cleaned = validate_report(report, "sunsynk", warnings.append)
    if warnings or cleaned.get("inverters") != report["inverters"]:
        print("ERROR: validation changed or warned on the report: {} {}".format(warnings, cleaned.get("inverters")))
        return True
    return False
```

Then add the wiring test. It is `test_run_first_cycle_polls_and_publishes()` with the live poll made to fail and `automatic_config()` observed: `refresh_live()` sets `live_ok` only when `fetch_device_data(sn)` returns something truthy, so a fake returning `{}` sends `run()` into `if first and not live_ok: return False`.

```python
def test_sunsynk_catalogue_filed_when_first_cycle_defers():
    """run() files the report even on a first cycle that defers startup because the live poll failed.

    `if first and not live_ok: return False` - and automatic_config() after it - would otherwise
    swallow the report on exactly the installs whose dump most needs to say what hardware was found.
    Built on test_run_first_cycle_polls_and_publishes(); fetch_device_data() fails and
    automatic_config() is observed.
    """
    failed = False
    s = ConfigSunsynk()
    s.automatic = True
    reports = []
    s.report_discovery = reports.append
    configured = []

    async def fake_restore():
        """Restore nothing."""

    async def fake_token():
        """Log in successfully."""
        return True

    async def fake_device_list():
        """Discover one inverter."""
        s.device_list = ["INV1"]
        return ["INV1"]

    async def fake_detail(sn):
        """Return the confirmed-live rating."""
        return {"ratePower": 8000}

    async def fake_device_data(sn):
        """Fail the live poll: refresh_live() treats a falsy result as no data."""
        return {}

    async def fake_settings(sn):
        """Return a minimal settings read."""
        return {"batteryLowCap": "10"}

    async def fake_publish():
        """Publish nothing."""

    async def fake_auto():
        """Record that automatic_config() ran."""
        configured.append(True)

    with (
        patch.object(s, "restore_state", side_effect=fake_restore),
        patch.object(s, "fetch_token", side_effect=fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list),
        patch.object(s, "fetch_device_detail", side_effect=fake_detail),
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "publish_data", side_effect=fake_publish),
        patch.object(s, "automatic_config", side_effect=fake_auto),
    ):
        result = run_async_local(s.run(0, True))
    if result is not False:
        print("ERROR: run() should defer startup (return False) when the first live poll fails, got {!r}".format(result))
        failed = True
    if configured:
        print("ERROR: automatic_config() must not run on a deferred first cycle")
        failed = True
    if len(reports) != 1 or reports[0]["inverters"][0]["device_id"] != "sunsynk:INV1":
        print("ERROR: the report must be filed before the deferring return, got {}".format(reports))
        failed = True
    return failed
```

Register all five in `run_sunsynk_config_tests()`'s `(name, fn)` list:

```python
        ("catalogue_describes_each_inverter", test_sunsynk_catalogue_describes_each_inverter),
        ("catalogue_export_limit_only_on_evidence", test_sunsynk_catalogue_export_limit_only_on_evidence),
        ("catalogue_none_before_discovery", test_sunsynk_catalogue_none_before_discovery),
        ("catalogue_round_trips", test_sunsynk_catalogue_round_trips_through_validate_report),
        ("catalogue_filed_when_first_cycle_defers", test_sunsynk_catalogue_filed_when_first_cycle_defers),
```

- [ ] **Step 2: Run them and confirm they fail**

```bash
cd coverage
./run_all --test sunsynk_config > /tmp/p2t4.log 2>&1; grep -E "FAILED|EXCEPTION|ERROR" /tmp/p2t4.log | head
```

Expected: `'MockSunsynk' object has no attribute 'build_discovery'`.

- [ ] **Step 3: Write `build_discovery()`**

In `apps/predbat/sunsynk.py`, add `from coordinator import inverter_record` beside the other project imports, then add to `SunsynkAPI`, immediately after `automatic_config()`:

```python
    def build_discovery(self):
        """
        Describe the discovered Sunsynk inverters for the discovery catalogue.

        Reads only state the component already holds - device_list, the rating, capacity and
        export-limit accessors - so this adds no API calls and cannot change what Sunsynk does.
        Reporting is independent of self.automatic.

        Discovery applies no device-type filter and automatic_config() registers every serial as
        "SunsynkCloud" with no further test, binding PV and battery entities for each. Nothing
        Sunsynk returns tells a PV-only unit from a hybrid, so neither can this: every record
        carries inverter_type "SunsynkCloud" and functions solar and battery, mirroring
        automatic_config() as the source of truth.

        Ratings: inverter_limit() (ratePower, W) as inverter_w; battery_capacity() (kWh) as
        battery_kwh; and the battery endpoint's capacity field as battery_capacity_ah, the raw Ah
        the API returned. export_limit is reported as a capability exactly where
        automatic_config() would bind it - export_limit() > 0.

        Deliberately not reported: model, firmware and any station ID - none is held, and Sunsynk
        has no station grouping at all.

        Returns None when no inverter has been discovered yet.
        """
        if not self.device_list:
            return None

        inverters = []
        for sn in self.device_list:
            ratings = {}
            rated_w = self.inverter_limit(sn)
            if rated_w > 0:
                ratings["inverter_w"] = rated_w
            battery_kwh = self.battery_capacity(sn)
            if battery_kwh > 0:
                ratings["battery_kwh"] = battery_kwh
            capacity_ah = self._as_float(self.device_values.get(sn, {}).get(SUNSYNK_CAPACITY_AH_FIELD))
            if capacity_ah > 0:
                ratings["battery_capacity_ah"] = capacity_ah

            capabilities = ["schedule", "target_soc", "discharge_target", "charge_rate_power"]
            if self.export_limit(sn) > 0:
                capabilities.append("export_limit")

            inverters.append(
                inverter_record(
                    "sunsynk:{}".format(sn),
                    inverter_type="SunsynkCloud",
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=capabilities,
                    hardware_ids={"serial": sn},
                    ratings=ratings,
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}
```

`SUNSYNK_CAPACITY_AH_FIELD` is already imported in `sunsynk.py` (`battery_capacity()` uses it).

- [ ] **Step 4: Wire it into `run()`**

In `SunsynkAPI.run()`, find:

```python
        await self.publish_data()
```

followed by `if first and not live_ok:`. Insert between them:

```python
        # Filed right after this cycle's publish and BEFORE `if first and not live_ok:` below:
        # that branch returns False to defer startup when the first live poll fails, and
        # automatic_config() follows it, so a report filed any later would never be filed on
        # exactly the installs whose dump most needs to say what hardware was found. (Sunsynk's
        # automatic_config() does not raise - the early return is the reason.) refresh_discovery()
        # owns the compare/retry/guard loop and never raises.
        self.refresh_discovery()
```

- [ ] **Step 5: Run the tests, then the full suite**

```bash
cd coverage
./run_all --test sunsynk_config > /tmp/p2t4.log 2>&1; grep -E "FAILED|EXCEPTION|passed" /tmp/p2t4.log | tail -3
./run_all > /tmp/p2t4-full.log 2>&1; echo "exit=$?"; grep -E "All tests passed|Some tests failed" /tmp/p2t4-full.log | tail -1
```

- [ ] **Step 6: Prove the tests are load-bearing**

1. Move the call below the `if first and not live_ok:` block; confirm `catalogue_filed_when_first_cycle_defers` fails. Restore.
2. Append `export_limit` unconditionally; confirm `catalogue_export_limit_only_on_evidence` fails. Restore.

- [ ] **Step 7: Commit**

```bash
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_config.py
git commit -m "feat(discovery): report Sunsynk inverters to the discovery catalogue

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Documentation

**Files:**
- Modify: `docs/discovery-catalogue.md`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: nothing code-facing.

- [ ] **Step 1: Add the four reporters to the reporter list**

Find the sentence listing the reporters that populate no `programmes` — it currently names `(GivTCP, GE Cloud, Octopus, Ohme, Solcast, Fox)`. Change the list to `(GivTCP, GE Cloud, Octopus, Ohme, Solcast, Fox, AlphaESS, Solis, Deye, Sunsynk)`. Change nothing else on that line.

- [ ] **Step 2: Say what the cloud reporters cannot tell apart**

In the section that describes the `inverters` section, after the sentence about entities, add:

```markdown
Deye and Sunsynk cannot tell a PV-only inverter from a hybrid: their APIs give no signal, and
their automatic configuration treats every discovered inverter as a battery inverter. Their
records say the same - `solar` and `battery` on every inverter - because the catalogue reports
what Predbat believes about the hardware. If a PV-only unit has been configured as a battery
inverter, that record is where the mistake shows.
```

- [ ] **Step 3: Run the quality gate**

```bash
git add docs/discovery-catalogue.md
cd coverage
./run_pre_commit > /tmp/p2t5-pc.log 2>&1; echo "exit=$?"; grep -E "Failed$" -A5 /tmp/p2t5-pc.log || echo "all hooks passed"
```

If cspell flags a genuinely correct new word, add it to `.cspell/custom-dictionary-workspace.txt` and re-stage it.

- [ ] **Step 4: Commit**

```bash
git add docs/discovery-catalogue.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(discovery): AlphaESS, Solis, Deye and Sunsynk as reporters

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Not in this plan

- **Entity maps** for any cloud reporter — a separate project (decided 2026-09-21).
- **The remaining inverter reporters** — Teslemetry, Sigenergy, SolaX, Enphase and Gateway — and **myenergi, Kraken, Axle and Carbon**, per the rollout plan's Subsequent plans.
- **A capability-vocabulary pass.** This plan reuses the spec's tokens plus `export_limit` (Fox's precedent). `ev_charger` (AlphaESS) and `tou_v2` (Solis) are new flags. When five or more reporters have contributed tokens, it is worth reconciling them.

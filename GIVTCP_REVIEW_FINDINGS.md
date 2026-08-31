# GivTCP component refactor — outstanding review findings

Review of **PR #4864** (`pr4739-main-merge`, supersedes #4649 + #4739) against `main`.
Run at `xhigh` effort, scoped to *correctness vs main*: this is meant to be a behaviour-preserving
refactor, so anything that changes runtime behaviour is a finding.

**Verification status:** 15 findings were raised. The two that are closed were verified against the
code in detail before being fixed. **The 13 below have *not* been independently verified** — they are
review claims with specific line references, produced at an effort level that explicitly admits
uncertain findings. Confirm each before acting on it. The full test suite is green on this branch, so
every finding here is also a test-coverage gap.

## Status

| # | Finding | Location | Status |
|---|---------|----------|--------|
| 1 | `num_inverters` from configured list length | `givtcp.py` | ✅ Fixed — `bd15cf0c`, `15fe7ece` |
| 2 | Stale `rest_data` republished as healthy | `givtcp.py` | ✅ Fixed — `28c2c61f` |
| 3 | `enable_charge_target()` never called | `givtcp_rest.py:342` | ✅ Fixed — see below |
| 4 | Rate write tolerance ~23× too wide | `givtcp.py:173` | ✅ Fixed — see below |
| 5 | `max: 20000` becomes `battery_rate_max_raw` | `givtcp.py:73` | ✅ Fixed — see below |
| 6 | `inverter_limit` overrides the user's AC limit | `givtcp.py:349` | ⛔ Won't fix here — see below |
| 7 | `soc_max` apps.yaml fallback destroyed | `givtcp.py:339` | ✅ Fixed — see below |
| 8 | REST-failure fallback path eliminated | `givtcp.py:349` | 📌 Accepted limitation |
| 9 | Window written as two non-atomic writes | `inverter.py:2954` | 📌 Accepted limitation |
| 10 | Window other-end defaults to `00:00:00` | `givtcp.py:367` | ✅ Fixed — see below |
| 11 | `rest_v3` gate dropped on discharge target | `givtcp.py:247` | ⬜ Open |
| 12 | GH#4826 clamp reads a hard-coded `min: 4` | `givtcp.py:76` | ⬜ Open |
| 13 | `battery_voltage` read-back loop | `givtcp_rest.py:128` | ⬜ Open |
| 14 | Unguarded `rest_data` indexing aborts publish | `givtcp.py:234` | ⬜ Open |
| 15 | Dead REST read every cycle in `update_status` | `inverter.py:1313` | ⬜ Open |
| 16 | Design capacity discarded, neutering SoH | `givtcp.py:381` | ✅ Fixed — see below |

Three themes account for most of them:

1. **`automatic_config()` claims apps.yaml keys unconditionally** (6, 7, 8) — including the very keys
   the shipped template tells users to keep as the REST-failure fallback.
2. **The entity layer publishes hard-coded attributes that `Inverter` reads back as device truth**
   (4, 5, 12).
3. **Guards were dropped that `main` had for stated reasons** (3, 9, 11).

---

## 3. `enable_charge_target()` is never called — the inverter ignores the target SOC  ✅ FIXED

> **Verified real, and a regression of #4141.** `cf92d8ca` added the enable specifically because
> "when reg 20 is off (the GivTCP default), the inverter ignores the SOC limit and charges to 100%,
> which is the root cause of Hold Charge not holding on AIO inverters". Fixed by publishing the
> register as a `charge_limit_enable` switch and auto-configuring that key, so
> `adjust_battery_target`'s existing entity path performs the enable. Withheld when GivTCP does not
> report the register.

**Location:** `apps/predbat/givtcp_rest.py:342`

`main`'s `adjust_battery_target` REST branch called `rest_enableChargeTarget(True)` immediately before
`rest_setChargeTarget(soc)`, with the comment *"Enable charge target, without it the inverter ignores
the target SOC."*

`GIVTCP_CONTROLS` maps `charge_limit` → `set_charge_target` only. The component publishes no
`charge_limit_enable` switch and does not auto-configure that key. `grep` shows zero production
callers of `enable_charge_target` — it was carried into the new client but orphaned.

**Failure:** a REST-only user (Docker / Predbat.com, or anyone without the GivTCP HA integration
entities in apps.yaml — which the PR says is supported since "existing users need no apps.yaml
changes") writes the target SOC to a register the inverter ignores, and the battery charges past the
planned limit.

**Fix direction:** either publish a `charge_limit_enable` switch and auto-config it, or have the
component's `set_charge_target` write path call `enable_charge_target(True)` inline before the target.

---

## 4. Rate write-verification tolerance is ~23× too wide  ✅ FIXED

> **Real, but less severe than first reported.** `_handle_write` discards the return value and
> `Inverter.write_and_poll_value` re-verifies against the republished entity with `fuzzy = rate/20`,
> so an unapplied write *is* caught — the review's "never recorded" claim was wrong. The real costs
> were skipped REST-level retries, a false success log, and an inflated register-write count. Fixed
> by deleting the redundant `battery_rate_max_charge`/`_discharge` fields entirely and sizing the
> tolerance from `max_battery_rate()`, which the object already had.

**Location:** `apps/predbat/givtcp.py:173`

`InverterRestState` is constructed with placeholder `battery_rate_max_charge` / `battery_rate_max_discharge`
of `1.0`. `GivTCPRest.set_charge_rate()` accepts a write when:

```
abs(new - rate) < inverter.battery_rate_max_charge * MINUTE_WATT / 12
```

With the real `Inverter` value (kW/min — e.g. `2600/60000 = 0.0433`) that threshold is **217 W**. With
the hard-coded `1.0` it is **5000 W** (and 2400 W for discharge, via `/25`).

**Failure:** Predbat writes `charge_rate` 200 W to hold the battery; the inverter ignores it and stays
at 3000 W. `|3000-200| = 2800 < 5000`, so `set_charge_rate` logs *"successful on retry 0"*, increments
`count_register_writes`, and returns `True`. The real failure is never recorded.

**Fix direction:** feed the discovered per-inverter max rate into `InverterRestState` once known
(`max_battery_rate()` already exists), rather than leaving the placeholder in place.

---

## 5. A missing max rate publishes a 20 kW battery  ✅ FIXED

> **Confirmed by test**: with no reported rate the published attribute really was `20000`.
> `main` had a REST-specific chain (`inverter.py:389-395`) trying `Invertor_Max_Bat_Rate`, then
> `Invertor_Max_Rate`, and only then falling back to `get_arg("charge_rate", attribute="max",
> default=2600.0)`. The branch keeps only that last line, and `charge_rate` now resolves to the
> component's own entity. Fixed by publishing **no** `max` attribute when the rate is unknown:
> `ha.py:804-808` returns the caller's default for a missing attribute, so `Inverter` gets its own
> 2600 W — exactly main's fallback. Both real captures do report `Invertor_Max_Bat_Rate` (2600 v2,
> 3600 v3), so this fires only when `inverter_details()` resolves empty — which on v3 also means
> capacity, inverter limit and time are lost at the same time (see finding 7).

**Location:** `apps/predbat/givtcp.py:73`

`publish_data()` overrides `charge_rate_attributes['max']` only when `rest.max_battery_rate()` is
truthy; otherwise the generic `GIVTCP_CONTROLS` ceiling of `20000` is published. `inverter.py:310`
then does `get_arg("charge_rate", attribute="max", default=2600.0)` and gets 20000.

On `main` there was no such entity to hit, so the same call fell back to 2600 W or the user's real
GivTCP entity max.

**Failure:** a GivTCP install whose `inverter_details()` lacks `Invertor_Max_Bat_Rate` /
`Invertor_Max_Rate` (v3 where the serial-named block is missing, or v2 firmware that omits it) has
Predbat planning a 20 kW charge/discharge battery. `battery_rate_max_charge`, `discharge` and `export`
are all sized off it.

**Fix direction:** withhold the `max` attribute entirely when the real rate is unknown, so `Inverter`
falls back to its own default instead of trusting a placeholder ceiling.

---

## 6. `inverter_limit` silently overrides the user's hand-set AC limit  ⛔ WON'T FIX HERE

> **Real, but not a defect of this PR, and two claims in the review are wrong.** It is not
> *silent* — the branch uses `set_arg_auto`, which logs a note naming the displaced value. And it is
> not aberrant: **ten** components already auto-set `inverter_limit` (AlphaESS, Deye, Fox, GE Cloud,
> Solax, Solis, Sunsynk, Sigenergy, Teslemetry, gateway), eight of them with bare `set_arg` which
> logs nothing. GivTCP was the outlier; this brings it into line. Where the user set nothing,
> behaviour is identical to main.
>
> The concern underneath is legitimate — an AC limit often encodes a *site* constraint (G98/G99, DNO)
> that the inverter cannot know, unlike `soc_max` or `battery_temperature` which are device facts. But
> that applies to all ten components today. It is a repo-wide policy question for `set_arg_auto`
> (whose docstring states "auto-discovery always wins currently"), and belongs in its own change
> applied uniformly. Note there is no escape hatch: `inverter_limit_override` caps
> `inverter_limit_charge`/`discharge` but never `self.inverter_limit`.

**Location:** `apps/predbat/givtcp.py:349` (`GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS`)

`main` assigned `self.inverter_limit` from REST `Invertor_Max_Inv_Rate` **first**, then at
`inverter.py:419` let `if "inverter_limit" in self.base.args` override it with the apps.yaml value —
the user always won. The template documents it as a user value: *"Inverter max AC limit (one per
inverter). E.g for a 3.6kw inverter set to 3600"* (`apps.yaml:219-221`).

`automatic_config` now replaces that arg with `sensor.<prefix>_givtcp_N_inverter_limit`.

**Failure:** a user with a 3.6 kW export limit whose GivTCP reports `Invertor_Max_Inv_Rate` 6000 gets
planned at 6 kW AC, and clips or trips on export.

**Fix direction:** do not auto-claim `inverter_limit` when the user has set it explicitly, or drop it
from the discovery keys entirely and let `Inverter` keep its existing precedence.

---

## 7. `soc_max` fallback is destroyed, and can leave `soc_max = 0`  ✅ FIXED

> **Confirmed by test** — a user's `soc_max: 12.0` really was replaced by an entity that was never
> published. Not the same issue as finding 6: that displaces a key whose entity *works*, this claims
> a key with *nothing behind it*. Fixed by tracking which discovery sensors each inverter actually
> published and claiming each key only where every managed inverter reported it. `battery_calibration`
> is deliberately exempt: `in_calibration()` returns a definite `False` when unreported, so it is
> always published and "not calibrating" is the correct default.

**Location:** `apps/predbat/givtcp.py:339`

`automatic_config()` claims all of `GIVTCP_AUTO_CONFIG_DISCOVERY_KEYS` (`soc_max`,
`battery_temperature`, `inverter_time`, `inverter_limit`, `battery_calibration`) unconditionally —
including sensors `publish_data()` may never publish.

`publish_data()` only emits `sensor.<prefix>_givtcp_N_soc_max` when `rest.battery_capacity_kwh()` is
truthy, and its comment claims a missing one *"falls back to the user's own apps.yaml value"*. It
cannot: `automatic_config` has already replaced `args['soc_max']` with the entity id.

**Failure:** GivTCP omits `Battery_Capacity_kWh` (or `inverter_details()` returns `{}`) → `resolve_arg`
resolves a non-existent entity → `get_arg` returns default `0.0` → `Inverter.soc_max = 0`
(`inverter.py:306-307`). `main` had an explicit rescue for exactly this (*"Warn: REST data does not
report Battery Capacity kWh, attempting to use soc_max apps.yaml instead…"*) which this PR deletes.

**Fix direction:** claim a discovery key only when its sensor was actually published — the same
discovery-gated principle already applied to `num_inverters`.

---

## 8. The documented REST-failure fallback path no longer exists  📌 ACCEPTED LIMITATION

> **Acknowledged as a known limitation of this refactor** rather than something to fix here. It is
> inherent to moving REST behind entities: once Predbat's keys point at the component, the
> GivTCP-HA-integration entities are no longer consulted.
>
> Partially mitigated by finding 7's fix — a key whose sensor was never published now leaves the
> user's own apps.yaml entity in place. Findings 2's warning and health timeout mean a REST outage is
> at least surfaced rather than silent. Keys that *are* published still displace the fallback, so the
> outage path itself remains. Worth stating plainly in the PR description.

**Location:** `apps/predbat/givtcp.py:349`

`apps/predbat/config/apps.yaml:102-103` says verbatim:

> If not using REST then instead set the Control here (one for each inverter)
> You should keep this section even when using REST as a fallback if it fails and for charge curve calculations

`main` honoured that: every REST branch was `if self.rest_data: … else: <entity>`, so a REST outage
fell through to `sensor.givtcp_<serial>_*`, which the GivTCP HA integration still populates over MQTT.

After this PR those args are replaced by the component's own entities — which are exactly the ones
that go stale when REST fails (see finding 2). A GivTCP REST outage now has no recovery path.

**Fix direction:** architectural, and worth a decision rather than a patch. Either leave the
GivTCP-HA-integration keys unclaimed, or have the component mark its entities `unavailable` when stale
so `Inverter` fails visibly rather than acting on frozen values.

---

## 9. Windows are written as two non-atomic writes, tripling register writes  📌 ACCEPTED LIMITATION

> **Verified real**, for both the charge and export windows: `main` wrote the whole window in one
> `rest_setChargeSlot1(new_start, new_end)` and explicitly skipped both the per-end entity writes
> (*"REST will be written as start/end together"*) and the disable step (*"for REST no need as we
> change start and end together anyhow"*). Where both ends move, a REST user now does 4 register
> writes (disable, 2x setChargeSlot1, re-enable) against main's 1.
>
> **Accepted**: this is simply how the normal entity control path works, and REST users are now on
> it like every other inverter. Three things temper it: the disable/re-enable is *correct* now
> rather than a bug (it exists to avoid a blip during non-atomic writes, so it cannot be removed
> without restoring atomicity); when only one end changes the caller writes only that end, giving
> parity with main; and `count_register_writes` is observability only (logged plus the
> `inverter_register_writes_total` metric), with no threshold or enforcement.
>
> A proper fix would be an atomic "write both ends" capability on the Inverter/component interface,
> benefiting GE Cloud and the others too. That is a design addition, not a patch, and belongs in its
> own change.

**Location:** `apps/predbat/inverter.py:2954`

`main` wrote the whole window with one `rest_setChargeSlot1(new_start, new_end)` and explicitly
skipped the disable step: *"Disable charging if required, for REST no need as we change start and end
together anyhow."*

Now `write_and_poll_option` fires `charge_start_time` and `charge_end_time` separately. Each
`select_event` drives `_set_charge_slot`, which reissues `/setChargeSlot1` with the other end taken
from `rest_data` — so the inverter is briefly programmed with `(new_start, old_end)`, which can be an
inverted or overlapping window if the new start is later than the old end.

Separately, the `not self.rest_data` guard at line 2954 was dropped, so every window change for a REST
user now also issues `enableChargeSchedule(False)` + `adjust_idle_time` + `enableChargeSchedule(True)`.

**Failure:** transient invalid window on real hardware, plus ~3× the register writes per change.
GivEnergy register writes are flash-backed, and Predbat counts them for a reason.

**Fix direction:** give the component a combined "set window" write that takes both ends, and restore
the disable-step skip for the REST path.

---

## 10. Window other-end defaults to `00:00:00` before the first poll  ✅ FIXED

> **Not the same as finding 9** — 9 is inherent to using the normal control path, this is the
> component fabricating a value it does not have. Confirmed reachable by test: with no snapshot the
> handler really did call `set_charge_slot1("09:00:00", "00:00:00")`.
>
> The reachable path is narrower than the review suggested. Finding 1's fix removed the
> "second inverter of a fleet" route (undiscovered inverters are never auto-configured, and a
> discovered one that later fails keeps its stale snapshot rather than reverting to `None`). What
> remains is a **component restart from the web UI** (`web.py:5088` -> `restart()` -> `initialize()`),
> which resets `rest_data` to `None` while Predbat's args still point at these entities.
>
> Fixed by refusing the write and logging, for all three slot types. There is no safe default for
> the half of a window you do not know, and refusing leaves Inverter's write-and-poll to report the
> failure through its usual path.

**Location:** `apps/predbat/givtcp.py:367`

```python
timeslots = rest.inverter.rest_data.get("Timeslots", {}) if rest.inverter.rest_data else {}
... .get("Charge_end_time_slot_1", "00:00:00")
```

**Failure:** a write event arriving before the first successful poll — or after a component restart —
writes only the end the caller asked for and sets the other to `00:00:00`: a zero-length or
midnight-terminated charge window on real hardware. `_set_discharge_slot` and `_set_pause_slot` do the
same.

**Fix direction:** refuse the write and log, rather than defaulting. There is no safe default for the
half of a window you do not know.

---

## 11. `rest_v3` gate dropped: v2 gets a discharge-target retry loop

**Location:** `apps/predbat/givtcp.py:247`

`main` gated the whole export-target block on `if self.rest_data and self.rest_v3`. Here
`discharge_target_soc` is in `GIVTCP_AUTO_CONFIG_KEYS` unconditionally, and `publish_data` emits the
entity whenever `read_discharge_target()` finds `raw.invertor.discharge_target_soc_1` (present in v2
raw dumps) and the model is not in `DISCHARGE_TARGET_UNSUPPORTED_MODELS`.

**Failure:** on a v2 install, every force-export cycle POSTs to a non-existent `/setDischargeTarget`,
burns `INVERTER_MAX_RETRY_REST × (1s + 2s)` of blocking sleep plus a `runAll` each, then
`record_status(had_errors=True)` — reviving the permanent every-cycle rewrite loop that #4517 was
meant to end.

**Fix direction:** gate the `discharge_target_soc` publish and auto-config on `rest_v3`, as `main` did.

---

## 12. The GH#4826 clamp reads a hard-coded `min: 4`, not a device bound

**Location:** `apps/predbat/givtcp.py:76` (`GIVTCP_CONTROLS['reserve']`), consumed at `inverter.py:1709-1725`

> ⚠️ **This one is a consequence of the merge resolution made while rebasing this PR onto `main`, not
> of the original contributor's work.** The `if not self.rest_data:` guard was deliberately dropped so
> the clamp would run for GivTCP REST users — correct in itself, but the entity's `min` was not checked.

The clamp's whole purpose is to respect the inverter's *real* register bounds. The `min` it now reads
is the literal `4` in `GIVTCP_CONTROLS['reserve']`, and the `max` the literal `100`.

**Failure:** a user with `battery_min_soc: 0` and `set_reserve_min: 0` (both allow 0, `config.py:613`)
who could previously reach a 0–3% reserve over REST is silently floored at 4%. Any GE model whose
reserve register really does go below 4 can no longer be driven there. The clamp's comment still
claims it is respecting *"the inverter's own register bounds"*.

**Fix direction:** publish the inverter's real register bounds on the reserve entity, or publish no
`min`/`max` at all so the clamp is a no-op rather than a fabricated constraint.

---

## 13. `battery_voltage` reads back its own published entity

**Location:** `apps/predbat/givtcp_rest.py:128`

For non-v3 (`rest_v3` False), `power_readings()` returns `get_arg("battery_voltage", default=52.0)`.
`GIVTCP_AUTO_CONFIG_POWER_KEYS` includes `battery_voltage`, so after `automatic_config` that key
resolves to `sensor.<prefix>_givtcp_N_battery_voltage` — the entity `publish_data` writes
`power['battery_voltage']` into.

**Failure:** from the second poll onwards the component reads back its own last publication. The value
freezes at whatever the first cycle produced (52.0 if the user's own entity had already been claimed),
and the user's real voltage sensor named in apps.yaml is never read again.

**Fix direction:** read the user's original apps.yaml value, captured before `automatic_config`
rebinds the key — or exclude `battery_voltage` from auto-config on v2.

---

## 14. Unguarded `rest_data` indexing aborts the whole publish

**Location:** `apps/predbat/givtcp.py:234`

`rest.target_soc` does `float(rest_data["Control"]["Target_SOC"])`; `rest.charge_enable_time` and
`discharge_enable_time` do `rest_data["Control"]["Enable_Charge_Schedule"]` — all unguarded.
`read_data()` only checks that a top-level `"Control"` key exists, not its contents.

**Failure:** a GivTCP version or partial `/readData` response that omits `Target_SOC` raises out of
`publish_data`, up through `run()`, into `ComponentBase.start()`'s catch-all. Everything published
after line 234 for that inverter — the reserve, all four window selects, `soc_kw`, `soc_max`,
`battery_calibration`, the power block — **and every entity of inverters n+1..N** is left at its
previous or never-published value, while the exception is logged as a generic component error rather
than naming the missing field.

**Fix direction:** `.get()` with explicit handling, and wrap each inverter's publish so one bad
snapshot cannot starve the rest of the fleet.

---

## 15. `update_status()` still does a full REST read nothing consumes

**Location:** `apps/predbat/inverter.py:1313`

After the diff, `self.rest_data` is only consumed at `inverter.py:400` (`reserve_percent_current`, in
`__init__`, before `update_status` runs). `grep` shows no other reader in the tree.

**Failure:** an HTTP GET per inverter per 5-minute cycle on top of the component's own 60s poll. When
GivTCP is slow it burns up to `20+40+40+40`s of `Inverter.sleep()` **inside the main planning loop**
and fires `record_status(..., had_errors=True)` for data that is then discarded. `self.rest_v3`
(lines 186/284) is likewise now write-only in `inverter.py`.

The block comment at lines 264-270 is also stale — it claims REST is *"kept alive for … pause_mode /
inverter_mode … and the #4517 discharge-target unsupported-model check"*, all of which the component
now publishes as entities.

**Fix direction:** drop the read (keeping whatever `__init__` genuinely needs) and correct the
comment. Lowest risk of the remaining set, and removes blocking I/O from the planning loop.

---

## 16. Battery state of health was never used  ✅ FIXED (as a feature, not a regression)

**Not from the original review — found while discussing finding 4.**

> **My first write-up of this was wrong.** I assumed `Invertor_Details.Battery_Capacity_kWh` was the
> battery's *current* capacity and `raw.invertor.battery_nominal_capacity` its *design* capacity, and
> reported that the refactor had collapsed the two and neutered `battery_scaling_auto`. The real
> captures in `coverage/cases/rest_v{2,3}.json` disprove that: they are **the same design figure in
> different units** — 186.0 Ah / 19.53125 == 9.5232 kWh, and 19.53125 is 1000/51.2, the Ah→kWh
> conversion at these packs' 51.2 V nominal. Their ratio is always exactly 1.0. `nominal_capacity`
> was the design capacity on both `main` and the branch, so nothing was broken.

The actual gap: **GivTCP reports real state of health and Predbat has never read it.** Each battery
module carries `Battery_Capacity` and `Battery_Design_Capacity` (Ah) under `Battery_Details` — flat
on v2, nested under `Battery_Stack_N` on v3. The v2 capture shows 184.82 of a 186.0 design (SoH
0.9937); GE Cloud's own test uses that same battery and ratio. Predbat left `battery_scaling` at the
user's manual value and relied on `battery_scaling_auto` to infer degradation from history instead.

**Fixed** by adopting GE Cloud's model (`gecloud.py:623-650`, `:1157`, `:1169`):

- `soc_max` carries the **design** capacity (`Battery_Capacity_kWh`, which already is that).
- `battery_soh` = `min(Σ Battery_Capacity / Σ Battery_Design_Capacity, 1.0)`, walked across modules
  and both version layouts. Asserted against both real captures.
- `battery_dod` = depth of discharge — GivTCP does not report it, so it defaults to `1.0` and can be
  set per inverter with the new `givtcp_battery_dod` apps.yaml key.
- `battery_dod_soh` = `soh × dod`, and `battery_scaling` is auto-configured (via `set_arg_auto`, so a
  manual value logs a displacement note) to point at it.

`nominal_capacity` stays the nameplate figure, so `battery_scaling_auto` and `degradation` are
unaffected; the BMS's own SoH now feeds `battery_scaling` instead of that being left at 1.0. Where no
per-module capacities are reported, no scaling is claimed rather than displacing the user's own value
with a fabricated 1.0 — which is exactly what deriving SoH from the two design figures would have done.
The expert switch keeps its meaning: with it on the health derate is suppressed.

**This is a new feature, not refactor parity** — planned battery size will drop by the measured SoH
(~0.6% on the captured battery) for GivTCP users who have not set `battery_scaling` themselves. Worth
stating in the PR, since the rest of the branch argues behaviour preservation.

A side benefit: `nominal_capacity()`'s docstring now answers `main`'s `XXX: Where does 19.53125 come
from?` — it is 1000/51.2, assuming a 51.2 V pack, which the v2 capture confirms.

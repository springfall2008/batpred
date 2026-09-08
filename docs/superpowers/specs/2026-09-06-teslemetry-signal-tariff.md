# Teslemetry Signal Tariff (TBC control mode) — Spec

**Issue:** <https://github.com/springfall2008/batpred/issues/4892>

## Problem

Predbat charges a Powerwall by asserting operation mode `backup` plus a raised
`backup_reserve_percent` plus grid charging (`teslemetry.py:539`). Tesla firmware
25.18.4 (mid-2025) throttled reserve-driven grid charging, so this path reaches
roughly 3.3 kW per Powerwall against a 5 kW nameplate. The full rate is only
reached when Tesla's own optimiser (Opticaster) decides to charge, under
Time-Based Control in `autonomous` mode. The Fleet API exposes no direct
"charge at N kW" command — the only levers are operation mode, backup reserve,
the grid import/export flags, and the pushed time-of-use tariff.

The same firmware change made backup reserve values of 81–99% invalid: they snap
to 80%. Predbat currently forwards the charge target SoC as the reserve, so a
target in that band cannot be set.

## Approach

Stop using the pushed tariff to describe real prices. Use it purely as a control
signal that tells Opticaster when Predbat wants energy moved, and let Opticaster
run the charge at full rate.

### Tariff shape

Three price levels, mirrored identically across all seven days of the week:

| Band | Buy (import) | Sell (export) | Applies to |
|---|---|---|---|
| Cheap | 0p/kWh | 0p/kWh | the committed charge window |
| Peak | 100p/kWh | 100p/kWh | the committed export window |
| Base | 50p/kWh | 0p/kWh | everything else |

Rationale for each choice:

- **Equal buy and sell inside each window.** 0p/0p over the charge window stops
  charge-then-immediately-export; 100p/100p over the export window stops
  import-to-re-export. This preserves an invariant the existing code maintains
  deliberately ("buy mirrors sell so it never grid-charges to re-export",
  `teslemetry.py:1129`).
- **0p sell outside the windows.** Deferring an export past the window end earns
  nothing, which is a hard cliff rather than the gentle slope that let TBC defer
  most of a discharge to the last half hour (#4887).
- **50p buy outside the windows.** The most expensive non-export band, so there
  is nothing to arbitrage from except the cheap window, and avoiding import is
  worth more than anything else the battery could do.
- **Mirrored on all seven days.** Matches the inverter contract Predbat already
  has everywhere else (a daily repeating start/end time with no date), makes a
  midnight-crossing window two ordinary intervals per day instead of
  day-offset arithmetic, removes the day-of-week mapping that GH#4610 lived in,
  and makes the tariff a pure function of the two windows — so it no longer
  re-pushes at every midnight rollover purely because the day index changed.

Only one charge window and one export window are ever mapped. The Predbat
optimiser guarantees they never overlap, so no overlap resolution is needed.

### Device state

Operation mode becomes `autonomous` in every state, because Opticaster only acts
on the tariff under Time-Based Control.

Reserve is the real reserve Predbat wrote, in every state — `automatic_config`
binds the `reserve` arg to the `schedule_reserve` entity, so it is exactly what
`adjust_reserve()` asked for. It is deliberately never the charge or export
target: under this mode those targets are advisory, because Tesla decides how
much energy actually moves, and window length is the lever for them.

| Predbat state | Reserve | Grid charging | Export rule |
|---|---|---|---|
| Charge window, `soc < target` | real reserve | on (off if the reserve is itself a hold request) | `pv_only` |
| Charge window, `soc >= target - 1` (hold) | 100 | off | `pv_only` |
| Export window, `soc > target` | real reserve | off | `battery_ok` |
| Export window, `soc <= target` | real reserve | off | `pv_only` |
| Demand (no window) | real reserve | off | `pv_only` |

- **The hold uses reserve 100 with grid charging disabled.** Reserve 100 is the
  only value above 80 Tesla still accepts, and it genuinely stops discharge;
  disabling grid charging is what stops it importing to reach 100. Clamping to
  80 instead would not hold — a battery at 85% would be free to discharge to
  80%, up to 19% of the pack, during a window whose purpose is to hold SoC flat.
  Freeze charge mostly fires when the battery is already well filled, so that is
  the common case rather than an edge case.
- **A 1% deadband on the hold test.** A freeze charge arrives as a charge window
  whose target equals the SoC at the moment `execute.py` wrote it; house load
  can drop SoC a fraction below that before the next cycle, which would
  otherwise flip the state from hold back to charge and import against the 0p
  band.
- **A reserve request in the 81–99 band rounds up to 100, not down to 80.**
  Predbat reaches that band itself: `execute.py:306`, `:593` and `:620` write
  `adjust_reserve(min(soc_percent + 1, 100))` under `set_reserve_hold`, so a
  battery at 85% asks for 86. That request means "hold above 80", and 80 would
  not hold it — the battery would be free to discharge back down to 80. Rounding
  up to 100 does hold, and the caller disables grid charging alongside so the
  device cannot import to reach it. The hold and the band mapping are therefore
  the same mechanism rather than two competing ones, and what Predbat models
  matches what the battery honours — the divergence #4953/#4956 fixed for the
  reserve floor generally.
- **Grid charging is off in every state except an active charge.** The tariff
  already discourages import elsewhere; turning the flag off makes it certain.
- **The tariff never depends on live SoC**, only on the committed windows. If it
  did, it would re-push as SoC moved and defeat the write-on-change dedupe.

### Freeze export

Already handled and needs no new code: `INVERTER_DEF["TESLA"]` declares
`"support_discharge_freeze": False` (`config.py:2209`), and
`fetch_inverter_data()` forces `set_export_freeze` and `set_export_freeze_only`
off from that flag every cycle before the planner reads them
(`execute.py:962-966`).

One gap remains: `manual_freeze_export` sets `EXPORT_LIMIT_FREEZE`
unconditionally (`plan.py:4405-4406`) with no capability check, unlike the
manual-export branch above it which does consult `set_export_freeze_only`. Under
the signal tariff that would carve the 100p band over a window meant to hold.

## Scope boundaries

**In scope:** the synthetic tariff builder, the TBC device-state mapping, the
trial setting that gates both, and the `manual_freeze_export` capability guard.

**Out of scope, deliberately:**

- **Enforcing the charge or export target.** With 0p import and a 100p export
  window later, the rational move for Opticaster is to fill completely; a tariff
  cannot express a ceiling, and reserve is left carrying the real reserve rather
  than being overloaded as a floor at the export target. Both targets are
  therefore advisory in this mode. The agreed lever is to shorten the window to
  deliver the target kWh, deferred until the trial shows whether it matters. That work depends on `battery_rate_max` carrying the
  achieved rate rather than the 5 kW nameplate, which `automatic_config`
  currently overwrites every cycle whenever `teslemetry_automatic` is on.
- **Closing #4887.** Within-window export deferral is still possible, since the
  export window is still a single flat band.
- **Supervision fallback** (detecting no SoC progress mid-window and reverting
  to `backup` mode). Worth having, but it should be designed against what the
  trial actually observes.

## Hardware questions the trial must answer

None of these can be settled by a unit test:

1. Does Opticaster act on the 0p band, and how quickly? It re-plans every few
   minutes.
2. Is the resulting charge actually at or near 5 kW?
3. Does reserve 100 with `disallow_charge_from_grid_with_solar_installed` set
   genuinely avoid grid import? If it imports at the throttled rate anyway, the
   hold is worse than leaking and the fallback is to clamp to 80 **and model
   80**, so the plan does not assume a flat SoC the device will not deliver
   (the divergence #4953/#4956 just fixed for the reserve floor).
4. Does a 100p buy band suppress import during the export window?
5. Does 0p export outside the windows cause solar curtailment rather than
   export?

# DEYE storage persistence and tiered refresh — design

Date: 2026-07-28
Status: approved, ready for implementation planning

## Problem

The DEYE component holds all of its state in memory and re-polls everything on
every 60-second tick. Two consequences:

1. **Every restart is a cold start.** `run()` gates discovery on
   `if first or not self.device_list`, so a process restart re-runs
   `station/list` and `station/device`, and rebuilds every derived value from
   scratch. Predbat restarts routinely, so this is the common case, not an edge
   case.
2. **Steady-state polling is wasteful.** `config/battery` is called once per
   device per minute — 1440 calls/device/day — to read installer settings that
   change perhaps once a year.

Worse, some state cannot be re-derived at all. `applied_payload` is a pure
in-memory change-detection cache with no API read-back (`config/tou` is defined
in `DEYE_ENDPOINTS` but never called), and `pending_orders` tracks in-flight
control orders. Both are lost on restart.

## Goals

- Survive a restart without a full poll.
- Refresh each class of state on a cadence matched to how fast it actually
  changes.
- Never let persistence cause the inverter to be controlled incorrectly.

## Non-goals

- Multi-instance coordination. `StorageBase` exposes `_acquire_refresh_lock()`
  for this; a single DEYE component per Predbat instance does not need it.
- Persisting `local_schedule`. It is rebuilt for free from HA entity states on
  every tick, and the existing `"unknown"`/`"unavailable"` defaulting in
  `get_schedule_settings_ha()` already covers the startup window before HA has
  republished.

## Refresh tiers

`ComponentBase` ticks `run()` every 60 seconds after startup
(`seconds % 60 == 0`), so a one-minute live tier is the natural rate and needs
no special handling.

| Tier | TTL | API calls | State refreshed |
|---|---|---|---|
| `static` | 8h | `station/list`, `station/device`, `station/latest`, `device/measurePoints` | `station_ids`, `device_list` |
| `config` | 15 min | `config/battery` per device | `device_battery_config` |
| `ratings` | — (written by the `live` refresh, saved only on change) | none of its own | `device_capacity`, `device_pack_voltage`, `device_rated_power` |
| `live` | 1 min | `device/latest` per device | `device_values`, `device_energy` — **not cached** |
| `control` | on change | none | `applied_payload`, `pending_orders`, `order_poll_count` |

`device/latest` returns telemetry, the energy counters and the ratings
(`RatedPower`, `BatteryRatedCapacity`, `BMSChargeVoltage`) in a single response,
so the `ratings` tier has no API cost of its own — it is a separate *file*
purely because it has a different restore rule (see below), not a separate poll.

Steady-state API cost falls from 2 calls/device/minute to ~1.07, and
`config/battery` from 1440 to 96 calls/device/day.

## Storage layout

Four files under module `deye`: `static`, `config`, `ratings`, `control`.

Telemetry and the energy counters are deliberately **not** cached. The live tier
polls every minute, so persisting it would mean 1440 writes a day to save at most
one tick's gap at startup — a poor trade against that much file or network IO.
Home Assistant already retains the last published value of every entity, and
`publish_data()` only writes a sensor when it has a value, so a failed poll
leaves the previous reading in place rather than overwriting it. The `live` tier
therefore has a TTL (governing poll cadence) but no file: its clock starts unset
after a restart and the first tick polls immediately.

Ratings come from the same `device/latest` response but *are* cached, because
they are static and are what lets `automatic_config()` map `soc_max`,
`battery_rate_max` and `inverter_limit` before the first poll returns. They are
written only when the value actually changes, compared by serialised signature,
so the once-a-minute poll does not rewrite an identical file.

Per-tier files rather than a single blob because `storage.age()` is per-file, so
each tier gets an independent clock for free. A single blob would need
hand-rolled per-section timestamps, and rewriting it every minute for live data
would reset the static section's age to zero, defeating the 8h TTL.

`save()` stamps `created` on every write, so `age()` is time-since-last-write.
This only holds as a TTL if a tier is saved **only when it actually refreshes**.

## The refresh clock

Each tier keeps an in-memory last-refresh timestamp. At startup it is seeded
from `storage.age()`; a tier with no stored file leaves its timestamp unset.

A tier refreshes when:

- its timestamp is unset, **or**
- its timestamp is older than the tier's TTL, **or**
- its product is empty — specifically `not self.device_list` forces a `static`
  refresh regardless of TTL.

That last condition matters: without it, a discovery attempt that ran during an
API outage and cached an empty `device_list` would pin the component dead for a
full 8 hours.

This deliberately does **not** use the `seconds % N == 0` idiom seen in
`gecloud.py` and `ha.py`. That counter starts from process start, so every
restart re-polls every tier — exactly the behaviour being removed. Seeding from
`storage.age()` is what makes the cadence survive a restart.

The design works unchanged when storage is absent: `self.storage` returns `None`,
no timestamps are seeded, every tier refreshes on the first tick, and the
in-memory clock governs from then on.

`first` reduces to "process start": restore the five files, seed the clocks, then
let the TTLs decide. The existing `if first or not self.device_list` gate is
removed.

## Restore rules

| File | Restore rule | Rationale |
|---|---|---|
| `static` | unconditional | Station and device IDs do not go stale. |
| `config` | unconditional | Installer settings. Also covers a transient `config/battery` failure — this endpoint has already returned `2106001 config point not supported` on one cycle and succeeded on the next. |
| `ratings` | unconditional | Static per install. Restoring these is the main win: `automatic_config()` can map `soc_max`, `battery_rate_max` and `inverter_limit` at startup without waiting for a poll. |
| `control` — `pending_orders`, `order_poll_count` | unconditional | An unpolled order is orphaned. `DEYE_ORDER_MAX_POLLS` still bounds it. |
| `control` — `applied_payload` | only if age < 15 min | See below. |

### Why `applied_payload` is bounded

This is the one place persistence can cause *incorrect control*, not merely
staleness.

`apply_dynamic_control()` skips the write when the desired payload equals the
applied one. Restoring `applied_payload` asserts *"the inverter still holds what
we last wrote"*. If the inverter was changed externally while Predbat was down —
installer, the Deye app, a manual TOU edit — that assertion is false, the write
is skipped, and the battery silently diverges from the plan.

Today a restart clears the cache and forces one corrective write. That is
accidentally safe, and the safety must not be lost.

A redundant write is cheap. A skipped write means the battery does the wrong
thing. The 15-minute bound keeps the benefit for the quick restarts Predbat
actually performs, and forces a corrective rewrite after any longer outage.

## Error handling

- `self.storage` is `None` → every save and load no-ops (the `teslemetry.py`
  idiom).
- A corrupt or missing file → that tier's clock stays unset → it refreshes on the
  next tick. No other tier is affected.
- Restored data is shape-validated (`isinstance` dict/list) before use, as
  `gecloud.py` does, so a truncated file cannot poison in-memory state.
- A first cycle whose live poll returns nothing makes `run()` return **False**.
  `automatic_config()` runs on the first cycle alone, so completing startup
  without telemetry would permanently skip the energy args until the next process
  restart. Returning False leaves `first` set and `ComponentBase` retries the
  whole startup path on its backoff (60s doubling to 128 minutes). Deliberately
  gated on the live poll only, not on `config/battery`: that endpoint can be
  permanently unsupported on a model (`2106001`), and gating on it would back off
  to 128 minutes and never complete startup, whereas `automatic_config()` already
  degrades gracefully when battery config is missing.
- A failed refresh must **not** save — saving would reset the TTL and skip the
  retry — and must not clobber good in-memory state. This is the same principle
  as the `RatedPower` clobber fix: absence of data is not zero.

## Testing

`MockDeye` gains an injected `storage` property returning `self._mock_storage`,
following the pattern already in `tests/test_ge_cloud.py`. `StorageLocalFiles`
is used for a round-trip test against a temporary directory.

Cases:

- Cold start, no stored state: every tier refreshes.
- Warm restart inside every TTL: **zero** API calls beyond the `live` tier.
- Telemetry is never restored and the live clock always starts expired.
- Unchanged ratings are not rewritten by repeated polls; a changed rating is.
- Restored ratings prime the signature so the first poll writes nothing.
- Each tier expiring independently: an expired `config` does not trigger a
  `static` refresh, and vice versa.
- Empty `device_list` forces a `static` refresh despite a fresh clock.
- Corrupt file for one tier: that tier refreshes, the others restore.
- `storage is None` throughout: component behaves exactly as it does today.
- A failed refresh does not save and does not clobber cached state.
- `applied_payload` restored inside 15 minutes suppresses a redundant write;
  outside 15 minutes it is discarded and the next apply writes.
- A discovery that returns nothing does not clobber an already-known device list.
- A first cycle without telemetry returns False and does not run
  `automatic_config()`; the retry with telemetry returns True and does.
- A pending order restored from storage resumes polling to completion.

## Files touched

- `apps/predbat/deye.py` — tier clocks, restore/save, `run()` restructuring.
- `apps/predbat/deye_const.py` — TTL constants.
- `apps/predbat/tests/test_deye_storage.py` — new.
- `apps/predbat/tests/test_deye_api.py` — `MockDeye` storage property.
- `apps/predbat/unit_test.py` — register the new test module.

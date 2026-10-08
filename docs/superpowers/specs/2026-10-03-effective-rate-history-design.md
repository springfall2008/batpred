# Effective rate history design

Status: implemented on `feat/effective-rate-history`. The quick test suite and changed-file pre-commit checks pass. The code has not been deployed to Home Assistant.

## Why we're doing this

Predbat rebuilds its rates every fetch. If a provider drops an old event, or a user clears an override, the next cost calculation can charge yesterday's or today's energy at a different price. We need to keep the effective prices that applied, while still recalculating costs when energy readings arrive late.

The agreed behaviour is in [the plan](../plans/2026-10-03-effective-rate-history.md). The old `rate-store` branch is useful reference, but we won't merge it into current main.

## Structure

Add `rate_history.py` with a `RateHistory` class owned by `PredBat`. It holds open periods, confirmed price adjustments and closed periods in memory. It saves them through the existing Storage component.

Keep the existing rate dictionaries for live planning. Add a separate historical lookup for cost calculations and history views. Don't load historical prices into the tables that `rate_replicate`, tariff comparison and future planning rebuild.

The first version stores effective Predbat prices. Axle's import opportunity-cost uplift remains part of those prices, as it is today. This work doesn't reconcile supplier bills or Axle payments.

## A period isn't necessarily a price change

Keep two kinds of timing metadata:

- Billing periods determine when a price record closes.
- Price and event bounds determine where an adjustment applies inside that period.

Two adjacent half-hours can have the same price and still be separate billing periods. A fixed tariff can publish one validity range covering months without making that range one history period. An Axle event can cover only part of a billing period.

Each source reader retains its interval metadata before expanding prices into minute dictionaries. Don't infer billing boundaries from runs of equal prices. Explicit source intervals take priority. For long validity ranges or durations inferred from missing source rows, use the existing, correctly configured `plan_interval_minutes` and label the boundary configured rather than provider-supplied.

Import and export can use different periods. The record identity is the direction plus its UTC start and end. Tariff metadata also identifies the rate source. A mid-period tariff change splits the price profile at its actual validity boundary.

Use supplied slot boundaries wherever they represent actual tariff periods. Keep IOG's explicit half-hour billing rule. Sources that supply only price validity or inferred cadence use the user's existing configured interval. There is no new fallback setting or hard-coded universal slot duration.

The own-Octopus component currently publishes rate rows at the UI's plan interval after flattening its source data. Add separate `rate_periods` metadata there. The URL cache must retain original periods beside its cached minute prices. Generated null-end replacements and forecast padding do not count as provider periods.

## What the store holds

Use one record per billing period and direction. Store a list of price segments inside the period rather than force every source into one price.

An open record holds:

- Its UTC start and end, direction, source identity and boundary origin.
- Automatic subsegments with their latest observations. Retain earlier observed subsegments when their event ends before the parent billing period closes.
- The latest applicable user override operations and their known validity bounds.
- Any confirmed IOG adjustment or active Axle offer needed to retain that price if the feed drops it.
- The observation time and quality of the evidence.
- The observed import no-IO profile where the existing yesterday baseline needs it.

A closed record holds the resolved price segments and their provenance. It no longer accepts normal schedule or override updates.

Full source offers live separately from parent records. They contain observed operations and their announced bounds, not future price records. Open parents reference offer IDs and resolve those operations against their latest underlying tariff. This keeps an active Axle offer across later parents and restarts without copying an old compound price onto a changed tariff. Explicit managed-curve null values withdraw open offers; closed prices stay fixed.

Observe automatic evidence even while a manual replacement masks its price. If the user removes that replacement, restore only offers observed or confirmed while applicable. Don't restore a future event just because it appeared in an earlier forecast. An automatic subsegment can close before its parent period; keep its saved price until the parent resolves its closing manual choice.

Don't save all future rate tables as history. Keep future schedules in the live fetch path. Only active observations and source-confirmed billing periods enter this store.

Example closed import record, with prices in the same minor currency units per kWh that Predbat uses today:

```json
{
  "direction": "import",
  "start": "2026-10-03T09:00:00Z",
  "end": "2026-10-03T09:30:00Z",
  "source_id": "octopus:import:configured-source",
  "boundary_origin": "provider",
  "state": "closed",
  "last_observed_at": "2026-10-03T09:25:00Z",
  "closed_at": "2026-10-03T09:30:00Z",
  "quality": "observed",
  "segments": [
    {
      "start": "2026-10-03T09:00:00Z",
      "end": "2026-10-03T09:30:00Z",
      "rate": 6.0,
      "sources": ["tariff", "iog_confirmed"]
    }
  ]
}
```

The file envelope contains a schema version, timezone, currency unit, generation and all retained records. Open records use the same identity and include their pending profiles and operations. Validate finite prices, ordered bounds, segment coverage and supported versions on load. Negative prices are valid. Zero is a price, not a marker for missing data.

## Price precedence

Keep today's precedence:

1. Underlying tariff and any integration-supplied adjustment.
2. Predbat's IOG pricing, saving sessions, free sessions and Axle adjustments, in their current order.
3. Configured rate overrides and manual API overrides.
4. UI manual rates.

Retained source evidence belongs at its existing automatic stage. It doesn't override a deliberate user price.

Capture the automatic profile just before user overrides. Add an optional trace collector to the existing override functions so the store knows whether each operation replaces a price or increments it. Reuse the existing parser and ordering. Don't build a second manual override parser in the store.

### Manual closing rule

When a billing period closes, use the last observed user override state that applies at the final instant inside that period.

- A replacement rate applies to the whole period.
- Removing that replacement restores the automatic profile for the period.
- An increment applies to each automatic segment. It doesn't copy an Axle-adjusted closing price onto minutes outside the event.
- Known start and end times still apply. If an override is known to expire before the billing period ends, don't extend it because a poll was missed.

For example, changing a whole-period replacement from 10p to 15p before 10:30 closes 10:00 to 10:30 at 15p. Removing it before 10:30 restores the underlying price profile. If Axle only applies from 10:10 to 10:20, that underlying profile can have more than one price.

This keeps existing override arithmetic where automatic events have narrower bounds. The parser records only currently applicable operations, with their actual start and end. A future override covering the closing minute is not an observed choice. An active override that expires before the close affects the provisional view, then returns to the underlying profile at finalisation.

UI selection duration and available selections both use `plan_interval_minutes`. Assume the user has configured that interval to match their tariff slots. This feature won't redesign configuration or resolve a deliberately mismatched setup. Preserve declared API/configured override bounds and current precedence. Close the saved period before boundary-time UI pruning so natural expiry doesn't erase its final price.

### IOG confirmation

Reuse the current evidence readers, but separate confirmed charging from a merely trusted or planned slot. A cheap forecast rate alone isn't confirmation. The existing `dynamic_load_car_confirmed` field can also come from aggregate house load, so copying it into durable confirmed history would give unrelated loads the same authority as car telemetry.

A source adapter must identify fresh measured car charging during an eligible smart-dispatch overlap. Keep the existing boundary guard unless a measurement timestamp supplies stronger evidence. Reject static configured booleans and stale cached power as confirmation of new periods. A direct Octopus completed entry can supply evidence only where its metered delivery and interval granularity support the relevant half-hour. A positive total for a multi-hour row doesn't prove delivery in every half-hour.

Record the eligible half-hour's cheap automatic price as soon as qualifying evidence is available. Retain it through unplugging and API withdrawal. Don't retain charging instructions. Apply the existing cap and tariff eligibility rules before accepting the price adjustment.

Current code keeps `dynamic_load_car_confirmed` only for the current half-hour. The new store keeps accepted price evidence after that field expires.

Ohme labels elapsed scheduled rows as `completed_dispatches`, but derives their energy from scheduled watts and duration. Those rows aren't metered completion evidence. Its session power sensor and successful-fetch timestamp can provide measured charging evidence. Compatible field names alone must not select the direct-Octopus confirmation policy. Kraken SmartFlex also needs its own billing rule; don't assume IOG terms apply to it.

A confirmation can cover remaining minutes of its own billing half-hour. It cannot confirm a separate future half-hour.

User overrides remain above this automatic price. Clearing a user override during the open period reveals the confirmed IOG price again.

Reapply only the still-applicable confirmed automatic price to the live working table after replication and before later automatic/user adjustments. This preserves the cheap remainder of the confirmed half-hour after its dispatch disappears. Closed historical prices still never enter the live table.

Retain confirmed cap allocations. A disappeared dispatch must not free its allocation or let a future planned slot displace it. Deduplicate reservations and current feed entries by billing-period identity. Preserve the existing per-car cap, noon bucket and allocation order in this feature; changing to an account-wide cap requires separate agreement.

The current confirmation code can attribute a just-finished load sample to the previous half-hour. Accept evidence that meets the source policy before closing that period in the same fetch. Don't reopen older closed records when a provider revises completed dispatches later. Trustworthy late metered evidence may fill a missing record or improve provenance on a matching price, but cannot change a different final price without a separately agreed correction rule.

### Axle bounds

Retain an Axle offer only after observing it as active. Its adjustment applies inside its announced bounds, not across an entire supplier billing period.

A successful empty response before the event starts removes the provisional offer. Once active, an empty response keeps the last-known offer until its announced end. A failed fetch isn't cancellation. Explicit future cancellation evidence would supersede this rule, but the currently consumed API fields have no cancellation status.

Managed curves remain provisional until their supplied period closes. Null withdraws a provisional value. Do not mistake a managed future entry in `event_history` for an observed period.

Keep overlapping event handling and signed values consistent with the current loader. The store records the winning effective profile once; it doesn't replay event rewards onto an already adjusted stored price.

## Fetch ordering

Capture one observation time for a live fetch. Use that time throughout the history update rather than calling `datetime.now` in individual methods.

The update sequence is:

1. Read provider data and charging evidence into the normal live fetch calculation. Keep the raw source bounds and whether a response succeeded.
2. Accept source evidence that explicitly covers the previous or current period, including a just-finished IOG charging sample.
3. Close expired automatic subsegments and parent records from their saved observations. Keep elapsed automatic subsegments inside an open parent until its manual choice resolves. Do this before a post-boundary rate rebuild can replace their prices.
4. Build the current automatic profiles and user override traces with retained source evidence at the right precedence.
5. Update parent manual state and automatic subsegment observations only where they are applicable at the captured observation time. Keep previously observed automatic subsegments that have ended. Never create elapsed history by copying a future forecast whose period passed during downtime.
6. Persist changed records. Prepare historical lookups for cost calculation and history rendering.

This ordering must live before the current `today_cost` call inside `fetch_sensor_data`, not as a hook at the end of `update_pred`.

A removal observed at exactly 10:30 affects 10:30 onward. It doesn't change the record ending at 10:30. All ranges are start-inclusive and end-exclusive.

No extra half-hour timer is needed. The next live fetch closes the previous period from its saved observation. A source's known expiry still limits its adjustment when the next fetch is late.

## Downtime and missing prices

Open observations survive restart. A restored open record can close from its last known profile after its end. Mark it `last_known` if the expected closing observation was missed.

A restored future projection cannot do that. The store never treats passing time as proof that a forecast applied.

If no active observation exists, return a gap. The caller can use a currently available underlying historical tariff as an estimated fallback. It must not replay a withdrawn IOG or Axle forecast into that gap. Keep the quality separate from observed or confirmed history.

This fallback remains an estimate. Predbat cannot recover a short-lived override it never observed.

## Storage and restart

Construct the store after Storage and configuration are available. Load retained local dates before the first live calculation. Use the existing `run_async` bridge from the synchronous orchestration code. Don't schedule untracked background writes.

`StorageLocalFiles.save` currently truncates its target before writing. A crash can leave an unreadable file. Simply switching the old branch to Storage would not make the history reliable.

Use two alternating full snapshots of the bounded history:

- Namespace `rate_history`, keys `snapshot_a` and `snapshot_b`. Each contains open records, retained closed records and active-source evidence.
- Write the next generation to the older or invalid key. Leave the newest valid key untouched. Choose the spare from the actual valid winner, not generation parity.
- Include a checksum over deterministic JSON of the payload and generation. Don't maintain a separate current-file pointer.
- Load both and choose the highest valid generation. Reject corrupt or incomplete snapshots. If a newer unsupported schema exists, disable writes rather than replace it with an empty older schema.
- Only mark a generation saved if `Storage.save` returns `True`. Keep dirty memory and retry after `False`, `None` or an exception.

This stays within Storage and avoids changing every other cache writer. It protects the previous completed snapshot from a failed rewrite. It isn't a claim that the backend guarantees survival of power loss without filesystem durability support.

Writes are serial. Save changed active observations each live fetch, even if the numerical price is unchanged, because observation time affects the last-known decision. Save confirmed source evidence during the same fetch. Don't create a new history writer for every component.

Save the full retained state in one generation. Midnight rollover doesn't require a transaction across separate day files. Keep all still-open periods and active evidence even if their start date falls outside normal retention.

Retain today and yesterday by default. `rate_retention_days` counts local calendar dates and has a minimum of two. Prune closed records by whether they overlap the retained local dates. Save snapshots with `expiry=None`; two fixed keys bound file count. Longer retention keeps price records only; it doesn't make older energy history available. Increasing retention cannot recover records already pruned.

## Time handling

Use UTC instants in records and convert to local dates only for display, boundaries and retention.

`PredBat.now_utc` isn't actually UTC. It is local time rounded to `PREDICT_STEP`, with configured clock skew. `midnight_utc` also carries that timezone. Don't copy those names into a new timestamp contract.

Define an observation timestamp from the configured Predbat clock without the five-minute rounding, then convert it to UTC. Keep the existing rounded plan time for planning. Tests inject both clocks, including configured skew.

Adapters convert between absolute history timestamps and each existing minute table's actual origin. UTC identities and local-calendar retention preserve short and long days. The legacy yesterday renderer still assumes a 1,440-minute axis. New yesterday cost recomputation checks the actual UTC day length and origin; on DST days or a shifted origin it uses the existing recorded-cost fallback rather than silently calculate a partial day. Fixing that renderer is outside this work.

## Accounting and history views

### Today and the last hour

Allow `today_cost` to read historical import/export lookups without replacing the planner's live dictionaries. Closed or source-confirmed records take priority. For an open period, use its latest provisional profile so today's estimate still responds to changes.

Keep standing charge, carbon calculation and the existing IOG car premium separate. Those aren't supplier import/export prices. Don't freeze cumulative costs or energy readings.

The current beyond-cap car premium depends on live `car_charging_slots`, so house prices alone won't stabilise that add-on after those slots disappear. Its formula multiplies the price premium by slot kWh, which can be scheduled or modelled energy. Multiplying a retained premium rate by measured car energy would change that formula.

Leave the beyond-cap premium formula unchanged. It is outside this work, with no planned follow-up unless someone raises a separate issue. The new history stabilises the house-price portion of car costs but doesn't promise persistence of the separate capped-car premium. Don't change its energy weighting or claim complete car-cost coverage.

Retain the applied daily standing charge as accounting context. Otherwise a configuration change after midnight can change yesterday's total even when its energy prices are stable. Don't change battery-value or carbon policies in this feature.

### Yesterday

Replacing the displayed rate alone isn't enough. `calculate_yesterday` currently reads HA `cost_today` history for its totals and cumulative metric curve. Those recorded costs can be stale after a manual closing decision or late energy readings.

Add a pure cost-series helper that multiplies available minute energy by retained prices. Use it for yesterday's actual import/export costs and cumulative history curve. It must include the existing standing-charge and car-cost rules rather than silently change reported totals.

Build yesterday's price lookup from stored periods. Use the stored no-IO baseline only where the existing counterfactual savings calculation needs that distinct profile. Do not substitute effective IOG or reward prices into a baseline that deliberately excludes them.

If required energy history or recorded prices are missing, retain the existing HA fallback and mark/log the estimate. Don't write back into HA Recorder history. Present and future calculations can use corrected totals without rewriting old entity samples.

Remove the current early return on missing HA `cost_today` history only when the new inputs cover the required energy and prices. Missing stored prices, daily standing charge or car-premium evidence prevent a claim of complete coverage.

Use an endpoint cumulative curve. The first endpoint is zero, each minute adds its cost to the next endpoint, and the terminal endpoint contains the full day. This avoids losing the last minute in the History renderer's start/end differences. Check absolute day boundaries before using the existing backwards arrays. Recompute only when the legacy axis covers the complete actual day; use the documented fallback otherwise.

Lifetime totals currently add yesterday's contribution once per local date after 01:00. Later energy corrections can change yesterday's displayed cost without changing that accumulated contribution.

Preserve that once-per-day behaviour. Feed it the recalculated yesterday values available at that time, but don't retroactively change lifetime totals afterward. Lifetime correction is outside this work. Document the existing limit; don't add per-date contribution tracking or delta updates.

### Replay and comparison

`save=False` is necessary but isn't a complete isolation boundary. Tariff comparison and replay can use hypothetical prices and fake time axes.

Add an explicit history context to fetch/cost callers. Normal live execution uses read/write history. Comparison, annual prediction and tests use disabled or isolated in-memory history. Debug replay defaults to the snapshot's in-memory history, if supplied, and never writes the live store. Include serialisable history in new debug snapshots so a replay can reproduce historical costs.

## Core APIs

`rate_history_adapter.py` connects these methods to the existing rate readers and override parser. Storage methods are asynchronous.

```python
await history.load(storage)
history.confirm_iog(start, end, rate, observed_at, **source_metadata)
history.close_elapsed(observed_at)
history.retain_offer(offer, observed_at)
history.withdraw_offer(identity, observed_at)
history.offers_for(direction, start, end)
history.observe(direction, periods, automatic_segments, manual_operations, observed_at, **profiles)
history.lookup(direction, start, end, include_open=True)
await history.save_dirty(storage)
```

A fetch snapshot contains source periods, automatic profiles, user override operations, baseline profiles and response quality. It carries UTC bounds plus adapters to the existing rate-table origin. It doesn't require every provider to write to Storage.

Keep price resolution pure so period closing and precedence tests can run without HA, live APIs or files. Keep Storage read/write and Predbat integration outside that resolver.

## Scope limit

Assume users have configured Predbat's slot duration correctly. This work records existing effective prices and their declared bounds. It doesn't fix unrelated integration bugs, redesign manual selections, change tariff cap policy or make every possible setup valid. Any issue that prevents recording correct history must be identified separately rather than folded into this feature without agreement.

## Expected changes

- New `rate_history.py` and its tests.
- Rate readers retain interval metadata alongside existing minute tables. Existing public dictionary results stay compatible where possible.
- `basic_rates` and `apply_manual_rates` expose an optional override trace.
- `fetch_sensor_data` builds the snapshot, closes and observes periods before calculating costs.
- `PredBat` owns the store, loads it during startup and includes its data in debug snapshots.
- `today_cost` and `calculate_yesterday` use historical lookups. A pure helper supplies recalculated cost series.
- Config defines retention. The existing configured plan interval handles sources without explicit period bounds.
- User documentation explains provisional prices, last-known gaps and Axle estimates.

Before editing runtime symbols, run the repository's required impact analysis. GitNexus isn't available in this session's tools or local checkout yet. Resolve that before implementation; the source inspection here isn't a substitute for the required check.

## Tests that decide whether this works

Test the resolver first, then Storage and the real consumers.

- Manual replacement changes and removal close at the agreed final value. A boundary-time change belongs to the next period.
- Manual increments and removal preserve automatic event bounds.
- A confirmed IOG half-hour survives stop, unplugging, API disappearance and restart. An unconfirmed future half-hour doesn't become cheap history.
- Confirmation just after a boundary only affects the half-hour its evidence covers. Cheap trusted forecasts don't count as confirmed charging.
- Axle rewards apply only inside exact event bounds on 15, 30 and 60-minute tariffs. Active disappearance retains known bounds; a future cancellation doesn't.
- Managed null and changed future curves don't become closed history from old projections.
- Equal adjacent prices retain separate period identities. Fixed validity ranges don't become month-long history periods.
- Missed polls and restarts close observed periods from the last known state. Never-observed periods remain gaps.
- Import and export can have different boundaries. Midnight and both DST changes preserve UTC identities and local retention.
- Failed writes, truncated snapshots, missing metadata and checksum failures recover the previous generation. Save failures leave dirty memory for retry.
- Late energy readings change costs without changing stored prices. Yesterday's price, total and cumulative curve agree.
- Recalculating yesterday doesn't add its lifetime contribution twice. Tests preserve the existing limit on post-rollup energy corrections.
- Comparison, annual prediction and replay cannot write or mutate live history.

Run tests from `coverage/`, save their output to a file, then inspect the result. No tests have run for this design-only change.

## Agreed scope and remaining code checks

Use supplied slot bounds and assume correctly configured slot durations. Sources with long validity or inferred cadence use the existing configured interval, not a new default. Don't expand this work into fixing unrelated setup problems.

Preserve existing override operations and precedence, including manual precedence above confirmed IOG prices. Verify increment handling against the current parser rather than redesigning it.

Verify that each supported IOG path supplies enough evidence to distinguish qualifying charging from a planned dispatch. Where it doesn't, mark the record observed or last-known rather than claim confirmation.

The beyond-cap car premium is outside scope, with no planned follow-up unless a separate issue is raised. Lifetime corrections are also outside scope. Keep both existing calculations unchanged. This work fixes retained prices, the house-price portion of car costs and today/yesterday cost series.

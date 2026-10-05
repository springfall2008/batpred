# Effective rate history

Status: implemented and tested locally; [technical design and scope limits](../specs/2026-10-03-effective-rate-history-design.md). Not deployed.
Branch: `feat/effective-rate-history`, based on main `990faba1`.

## Goal

Persist provider-independent effective import/export prices so historical cost estimates and history views do not change when dispatches, events or overrides disappear. Keep energy readings recalculable: this stores prices, not frozen cost totals.

References:

- [Canonical issue #2785](https://github.com/springfall2008/batpred/issues/2785)
- [Previous draft PR #3340](https://github.com/springfall2008/batpred/pull/3340)
- Existing fork branches `origin/rate-store` and `origin/rate-store-refactor` are reference only. Reimplement against current architecture; do not merge them wholesale.

## Agreed rules

### General

- Future scheduled prices are provisional, not historical evidence. Changing or removing them before they apply must not create permanent history.
- Track effective prices while a period is open. Finalise the last known observation when its boundary passes, unless a source-specific confirmation establishes a price earlier.
- A post-boundary fetch must not replace the last observation made during the period with a newly rebuilt tariff price.
- Persist open-period observations as well as final records so restart does not lose the last known price.
- Finalised records are not changed by ordinary live override/configuration changes. No historical correction interface in this scope.
- Preserve the existing precedence of tariff, automatic adjustments, configured overrides and manual overrides. Persistence must not reapply rewards or double-add adjustments.
- Store effective Predbat prices, with provenance/quality sufficient to distinguish confirmed, observed and last-known values. These are not reconciled supplier bills or confirmed reward settlements.

### Intelligent Octopus Go (including Ohme)

- Qualifying charging confirms cheap pricing for the entire applicable billing half-hour, including elapsed and remaining minutes.
- Stopping charging, unplugging, or subsequent API withdrawal does not remove that confirmed price.
- Confirmation of one half-hour does not confirm subsequent scheduled half-hours.
- Price confirmation is not a charging instruction: do not retain obsolete future car-charging commands or dispatches.
- Confirmation must respect current eligibility/cap rules. Determine available confirmation evidence for each integration before implementing; a planned dispatch alone is insufficient.

### Manual overrides

- The tariff period stays open until its end. Its last observed effective price becomes the historical price for the whole period.
- Changing an override updates its provisional value; intermediate choices do not create separate historical prices within the same tariff period.
- Removing an override before the period ends restores the underlying effective price, including applicable automatic adjustments.
- Changes after finalisation do not rewrite history.
- Live planning still responds to intermediate changes; only historical finalisation uses the closing value.

### Axle

- Apply the event adjustment to its exact announced half-open interval `[start, end)`, retaining tariff intersections/subsegments. Do not round an event out to cover a whole tariff period.
- Future event disappearance removes the provisional offer.
- Once an event has been observed active, retain its last-known announced offer within its original bounds if it disappears before the end, unless explicit cancellation evidence is available. Never extend it beyond the end.
- A fetch failure is not cancellation.
- Stopping export does not erase the offered adjustment for energy previously exported. Actual energy determines estimated earnings; storing an offer does not guarantee payment.
- Managed price-curve values remain provisional until their own supplied periods close. Explicit null means no market participation; do not retain a withdrawn future premium as final history. Missing entries outside a returned horizon are not automatically cancellation.
- Preserve existing Predbat effective-rate semantics, including the export-event import uplift. That uplift is an optimiser opportunity cost, not a supplier import charge. Managed curves are indicative flexibility values, not settled revenue.
- Do not treat Axle `event_history` as an immutable ledger: it contains mutable entries and managed future periods.

### Boundaries and time

- Align to actual tariff periods, not a universal 30-minute interval and not UI plan intervals.
- Preserve subdivisions where automatic events cover only part of a tariff period.
- Keep import/export period definitions independent where their tariffs differ.
- Do not infer period boundaries solely from changes in price: adjacent periods may have equal prices.
- Store absolute timezone-aware instants (UTC for identity/order), with local dates used for history display and retention. Handle midnight and DST without assuming every local day is 1,440 elapsed minutes.
- IOG's billing half-hour rule is source-specific even where the underlying rate feed publishes longer validity ranges.

### Gaps and retention

- If a closing update is missed, use the last known observation for that period; persist that choice as final after closure.
- Do not promote an unobserved future projection merely because the clock passed its end.
- For periods never observed while applicable, fallback/unknown handling remains an implementation decision. Distinguish it from confirmed history and do not invent a cheap override.
- Retain at least today and yesterday for history views, with configurable longer retention. Define retention by local calendar dates so yesterday survives both short and long DST days.

## Repository findings

- `apps/predbat/fetch.py:fetch_sensor_data` builds minute tables from source rates, then applies IOG, saving/free sessions, Axle, configured overrides and manual overrides before publishing rates and calculating costs.
- `apps/predbat/output.py:today_cost` recomputes day/hour costs from energy and the live effective tables.
- `apps/predbat/output.py:calculate_yesterday` uses `history_to_future_rates` on live tables; it also reads cost entity history. Both paths must be examined to keep price/cost display consistent. Rate persistence alone does not remove all HA history dependencies.
- `apps/predbat/storage.py` provides the existing Storage abstraction. Use it, not the old branch's direct-file `PersistentStore`.
- Rate readers currently flatten source intervals into per-minute dictionaries. Octopus/Nordpool-compatible feeds and Stromligning provide explicit bounds; Energi Data Service infers 15/30/60-minute intervals. Capture interval metadata before flattening.
- `apps/predbat/userinterface.py:manual_rates` uses `plan_interval_minutes`; distinguish UI selection width from tariff/billing period width.
- Fixed/basic tariffs may publish long or absent validity bounds, not settlement granularity. Define an explicit fallback/configuration rather than infer it from identical prices.
- Axle adds export reward to both import and export; import events subtract from import. Later manual overrides win. Preserve the final winning value without replaying event adjustments onto stored history.

## Primary Axle evidence

- [Self-Dispatch guidance](https://help.axle.energy/en/articles/10449602): payment covers energy exported during the scheduled window; stopping early retains earlier credit; event end does not change mid-event.
- [Public Home Assistant API](https://vpp.axle.energy/landing/home-assistant): start/end/direction/update fields, no explicit cancellation or settlement status consumed by Predbat.
- [Price-curve overview](https://docs.axle.energy/workflows/price-curves/overview.md): indicative, non-binding gross marginal flexibility values relative to baseline.
- [Price-curve API](https://docs.axle.energy/api-reference/entities/site/price-curve.md): half-hourly curve; null means no market participation.
- [Flex-event API](https://docs.axle.energy/api-reference/entities/site/flex-events.md): estimated/final revenue separate from the curve; settlement can arrive much later than history retention.

## Implementation work to resolve before coding

1. Trace all rate readers/consumers and define how real period metadata reaches the store, including static tariffs and differing import/export schedules.
2. Specify open/final record schema, source confirmation evidence, and precedence when IOG confirmation overlaps manual overrides.
3. Specify boundary ordering: finalise prior observations before ingesting a post-boundary rebuild; handle first-ever observations, empty/failed fetches and replay/test `save=False` isolation.
4. Preserve exact Axle event subsegments while applying the agreed manual whole-period closing rule. Define precedence without smearing an event beyond its bounds.
5. Choose integration points for live planning versus historical accounting/display so immutable historical prices do not distort future replication, no-IO baselines or compared tariffs.
6. Define storage lifecycle, versioning, write failure behaviour, atomicity, retention and bounded growth. Reuse Storage; document any backend durability limitations.
7. Run impact analysis before editing runtime symbols; report high-risk changes. GitNexus is not currently available in this checkout/session, so resolve tooling before code changes.

## Regression matrix

- IOG: charge 10 minutes then stop/unplug; preserve the whole confirmed half-hour and remove unconfirmed future half-hours.
- IOG: API withdrawal, location relabel, restart during/after confirmed interval; no scheduled-dispatch-only confirmation; daily cap remains honoured.
- Manual: change rate twice within an open period, retain closing value across whole period.
- Manual: remove before close, restore underlying effective tariff; removal/change after close leaves history unchanged.
- Manual: override ended at the boundary; do not overwrite the prior period with the new period's tariff.
- Axle: irregular event bounds intersect 15/30/60-minute tariffs; adjustment only inside event bounds.
- Axle: future cancellation; active disappearance; failure versus successful empty response; restart; no reward leakage after known end.
- Axle managed: priced-to-null and revised future curves; negative values; changes after closure; missing horizon entries distinct from withdrawal.
- Overlaps: manual suppresses automatic adjustment; removal restores it; no double rewards from duplicate current/history entries.
- Gaps: missed boundary finalises last observation; downtime crossing several periods never finalises unobserved cheap future projections.
- Time: midnight rollover, spring/autumn DST, tariff change, differing import/export periods, adjacent equal-priced periods.
- Retention: today/yesterday always present; longer configured retention; corrupt/missing storage; write/load failures do not silently erase valid history.
- Consumers: cost today/hour/car and yesterday price/history views use retained effective prices; late energy readings can still change costs correctly.
- Isolation: comparison/annual simulations and debug replay do not overwrite live history or inherit inappropriate effective adjustments.

## Out of scope

- Reconciled electricity billing or final Axle payment accounting.
- Changes to the separate beyond-cap IOG car premium. No planned follow-up unless someone raises an issue.
- Retrospective corrections to lifetime totals. Preserve the existing once-per-day rollup.
- Configuration redesign or correction of mismatched slot durations. Assume a correctly configured setup and preserve supplied slot bounds.
- Historical editing UI/API.
- Dispatch collection/polling redesign (short-lived unobserved changes remain a limitation).
- Reconstructing lost historical overrides before this feature was installed.
- Broad fixes to Axle component caching/restart behaviour unless necessary to avoid recording incorrect history; track separately where possible.

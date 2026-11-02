# Real billing alongside the Plan page's History view — design

Date: 2026-11-01. Status: draft for review.

## Intent

Users want to answer one question: **"did I get billed what Predbat said I would be billed?"**

Predbat already records what each half-hour cost, from inverter readings priced at Predbat's rates
(`predbat.cost_today`, rebuilt into the Plan page's **History** view by `calculate_yesterday()` in
`apps/predbat/output.py`). Octopus and the other Kraken suppliers (EDF, E.ON) publish what they
actually billed per half-hour. This feature reads that billing, stores it in a sensor, and shows it
next to Predbat's own cost on the Plan page's History view, so a mismatch is visible slot by slot
and as a day total.

Note the comparison: it is billed vs **Predbat's recorded cost** (inverter data × Predbat's rates),
not vs the forecast the plan made beforehand.

## Goals

1. Read half-hourly billing from the Kraken GraphQL `measurements` query for every Octopus and
   Kraken (EDF/E.ON) user, with no opt-in switch.
2. Cover the import MPAN(s) and, where configured, the export MPAN. Whether export returns
   measurement data is **not yet known** and must be checked against a real account.
3. Publish a sensor holding the last **7 days** of 30-minute billing, refreshed every few hours.
4. On the Plan page (`/plan`), extend the **History** button's view with:
   - a **day picker** for the last 7 days (as well as the current yesterday+today view),
   - two new columns after *Cost*: **Billed** (pence; buckets on hover) and **Diff**
     (billed − Predbat cost; red when billed is higher, green when lower),
   - a **summary strip** above the table for the chosen day: Predbat cost, billed cost, difference,
     import kWh and export kWh (Predbat vs meter), standing charge.
   The chosen layout is option C from the mockups (compact columns + summary strip).
5. The sensor's data is shaped so predbat.com can consume it later. Only the Predbat web UI changes now.

## Non-goals

- predbat.com UI changes (the sensor feeds it later; not in this work).
- The separate "History" pages elsewhere in the web UI — this is only the Plan page's History view.
- Comparing against the plan's forecast cost.
- Reading statements / final bills. Kraken `measurements` cost is Kraken's *estimate*; that is the
  data source.
- Changing how Predbat prices or plans anything. This is read-only reporting.

## Anti-goal (the user's words): "Bad data is worse than no data"

A wrong billed figure, or a Diff that shows a gap which isn't real, is a failure even if everything
else works. Users will act on a red Diff (raise tickets, change tariffs). So:

- If a figure cannot be trusted, **hide it** rather than show a best guess.
- A half-published day must not look like a mismatch.
- Diff must compare like with like on both sides (same legs, same VAT basis, same treatment of the
  standing charge). A systematic offset (e.g. VAT) showing up as a Diff on every slot is a failure.

## Constraints and known facts about the data

From earlier support investigations (agent-memory; file memory
`octopus-iog-billing-buckets-vs-dispatch-location` in the predbat-saas-infra project). Treat as
pointers to confirm, not as settled API documentation.

- Query: `account(accountNumber) → properties → measurements(utilityFilters: [{electricityFilters:
  {readingFrequencyType: THIRTY_MIN_INTERVAL, marketSupplyPointId: "<mpan>", readingDirection: ...}}],
  startAt, endAt, ...)`. `utilityFilters` is a list; the enum is `THIRTY_MIN_INTERVAL` (no
  `HALF_HOURLY`); `startAt`/`endAt` need `... on IntervalMeasurementType`.
- Each interval has `value` (kWh) and `metaData.statistics`: a list of buckets, each with a label,
  kWh value and an `EstimatedMoneyType` cost (`estimatedAmount`; there is also a `costInclTax`
  variant seen on E.ON). `pricePerUnit` is the **standard tariff rate, constant on every interval —
  not the applied rate; do not use it.**
- **Do not depend on bucket names.** Labels have changed on live accounts (Sep 2026
  `ECO7_DAY_P` / `ECO7_NIGHT_P` / `EV_DEVICE_OFF_PEAK_P` → Oct 2026 `Home Standard Rate` /
  `Home Low Rate` / `EV Standard Rate` / `EV Low Rate`), and the old suffixes varied per account.
  Store and display whatever labels arrive; sum them generically. The car/house split (car peak,
  car off-peak, house peak, house off-peak) falls out of this naturally on Octopus IOG.
- The **standing charge** arrives as a bucket (e.g. `STANDING_CHARGE_STANDARD_*`) with kWh 0 and a
  non-zero cost. Separate it by shape (cost but no kWh), not by name, and keep it out of per-slot
  p/kWh and energy cost.
- E.ON puts all consumption in one bucket; the applied rate is cost ÷ kWh. EDF untested.
- Billing lags roughly **1–2 days** (Octopus Kraken measurements are fresher than the REST
  `/consumption/` endpoint). Some accounts publish **no** half-hourly measurements at all (query
  succeeds, zero edges).
- Accounts can have **several import MPANs**; some may return `KT-CT-4718 not authorized`.
- **VAT basis is unresolved.** On Octopus IOG-SMB, Predbat's rates were seen ex-VAT
  (3.33p / 27.3p) while billing was inc 5% VAT (3.49p / 28.67p). Which `measurements` cost field is
  inc/ex VAT, and what basis Predbat's own rates are on per supplier, must be established before
  Diff is trusted.
- Predbat's `cost_today` adds the full day's standing charge at midnight; `cost_today_import`,
  `cost_today_export` and `cost_today_car` also exist and can provide per-leg comparisons.
- Existing infrastructure: `OctopusAPI` (`octopus.py`, has `async_graphql_query` with rate-limit
  backoff and a Kraken token) and `KrakenAPI` (`kraken.py`, discovers import/export MPANs and
  holds the token for EDF/E.ON). Components read config via `get_arg`, never `self.base.<attr>`.
  File access goes through the Storage component.
- `octopus.py` already has a `MEASUREMENTS_QUERY` and a parser (`_tou_windows_from_measurements`,
  used by `async_get_tou_night_windows`) reading these same `THIRTY_MIN_INTERVAL` measurements —
  reuse rather than write a second query.
- Plan slots are `plan_interval_minutes` long, which need not be 30; billing is always 30-minute.
- The Plan page renders from JSON returned by `/api/plan_data` (`plan`, `yesterday`, `baseline`).
- Home Assistant won't record state attributes over ~16 KB. **This is acceptable** (user's
  decision): only the current value matters; history of the attribute payload is not needed.
- Unit tests are required for all new code; 100% docstrings; British English; pre-commit must pass.

## Decided (by the user)

- Count buckets generically; no dependency on bucket names.
- Read the export MPAN too, if one is configured; check first whether it returns data.
- Sensor holds the last 7 days of 30-minute billing, updated every few hours.
- The Plan page's History view reads the sensor and shows billing alongside.
- The History view gets a way to switch between the last 7 days, not just yesterday.
- `calculate_yesterday()` needs to provide the last 7 days, but the older days need not be
  recomputed every time — they change at most once a day.
- Layout: option C (Billed + Diff columns after Cost, buckets on hover, summary strip above).
- It is an addition to the existing History view; existing columns and behaviour stay.
- Enabled automatically for all Octopus and Kraken users, no switch.
- A large sensor attribute (>16 KB, not recorded by HA) is fine.
- Web UI only for now; the same sensor data can feed predbat.com later.
- Slots where Predbat's rate included an event the bill won't reflect (saving session,
  free-electricity session, Axle VPP event, manual rate override): Diff is still shown, and the
  slot is **highlighted with the event's name when Predbat knows it** (e.g. "Saving session").
  Where Predbat has no record of an event for that slot, nothing is highlighted. Saving-session
  rewards are paid as OctoPoints and Axle pays separately, so these slots are expected to differ.

## Claude's calls (not discussed with the user — check these)

1. **Where it lives:** inside the existing `OctopusAPI` and `KrakenAPI` components (they already
   own the token, MPAN discovery and rate-limit handling). One shared parser module turns a
   `measurements` response into the common slot shape, so both suppliers produce the same sensor.
2. **Fetch cadence:** every 3 hours. After the first fill, only re-request days that are not yet
   complete (fewer than 48 half-hours, or 46/50 on clock-change days), so Octopus isn't asked for a
   week each time. Persist the cache through the Storage component so a restart doesn't refetch.
3. **Sensor:** `predbat.billing` (entity name to be settled against existing naming).
   State = billed total in pence for the most recent *complete* day. Attributes: per-slot data for
   the last 7 days (slot start in UTC, import buckets `{label: {kwh, cost}}`, export buckets,
   import/export totals), per-day summaries (energy cost, standing charge, kWh per bucket),
   `complete_until` timestamp, which legs (import/export) are available, and the supplier.
4. **Merged-blocks view:** a block spanning several slots shows the sum of their billing.
5. **Unpublished slots** show "—" and count for nothing. The summary strip states
   "billing complete to HH:MM" and its Predbat-side figures cover **only the slots that have
   billing**, so a half-published day never shows a false difference.
6. **Export:** netted into Billed and Diff the same way Predbat's Cost column nets it. If export
   billing is unavailable for a user who has an export tariff, Billed and Diff become
   **import-only** against `cost_today_import`, and are labelled as import-only — never a net
   Predbat cost compared with an import-only bill.
7. **Standing charge** is kept out of Billed/Diff on both sides (Predbat's midnight standing-charge
   step stripped out) and shown separately in the strip, Predbat's figure next to the billed one.
8. **Trust rules (hide, don't guess):** hide billing for a day if any import MPAN for that period
   failed to read (not authorised, error), if the account publishes no measurements, or if the VAT
   basis for that supplier is not established. Hide Diff for a slot where Predbat has no recorded
   cost. Log why at Info level once per fetch, not per slot.
9. **Day picker:** the default view stays exactly as today (yesterday + today so far). The picker
   adds the six days before yesterday, each shown as the **full** History table plus billing.
   The History view is a replay, not a simulation (the simulation in `calculate_yesterday()` is
   only for "Yesterday Without Predbat"): it renders the plan table from recorded SoC
   (`predict_soc_best`), recorded cost (`predict_metric_best = cost_yesterday_array`) and
   charge/export windows rebuilt from `predbat.status` history. So older days need longer history
   reads, not more simulation runs: `cost_today` (+ import/export/car variants), SoC,
   `predbat.status`, load and PV, for 8 days. The one gap is the Import/Export rate columns —
   `rate_import`/`rate_export` only reach back to yesterday — so older days take their rates from
   the recorded `predbat.rates` / `predbat.rates_export` history in HA, and show "—" where that
   history is missing. Older days are rebuilt at most once a day (on the midnight re-plan or first
   run); yesterday and today keep their existing per-slot rebuild. A day HA history doesn't reach
   is left out of the picker. "Yesterday Without Predbat" stays yesterday-only.
10. **No Octopus/Kraken, or no billing yet:** the columns and strip are hidden; the History view
    looks exactly as it does today.
11. **Which MPANs:** read only the import MPAN Predbat models (the one its rates come from —
    `OctopusAPI.mpan`, `KrakenAPI.import_mpan` / `configured_mpan`) and the export MPAN Predbat
    already discovered. Other MPANs on the account (another property, a separate meter) are ignored:
    adding them would make every slot's Diff false, and an unreadable unrelated MPAN must not hide
    billing. The call-8 trust rule therefore applies to these MPANs only.
12. **Who is in scope:** users with Octopus API credentials in Predbat (`octopus_api_key` +
    `octopus_api_account`, i.e. the `OctopusAPI` component running) and Kraken component users.
    Users who get Octopus rates only through the Home Assistant Octopus Energy integration have no
    Kraken token in Predbat and are out of scope; the docs say so.

## Left to the builder

- The exact GraphQL query text and field names, verified against a live Octopus account and one
  Kraken (E.ON or EDF) account, including the export reading direction and whether it returns data.
- Which cost field to use and its VAT basis, and how to bring both sides to the same basis.
- How the older days' History JSON is delivered to the page (extend `/api/plan_data`, or fetch per
  day on demand) and how it is cached.
- Timezone and clock-change handling (slots keyed in UTC; displayed in local time).
- Test design, including recorded `measurements` fixtures for Octopus IOG (four consumption
  buckets + standing charge) and E.ON (single bucket).
- Documentation for the new sensor and the History view additions in `docs/`.

## Verified data shape (probe, 2 Nov 2026)

Read from one live Octopus account (import `E-1R-IOG-SMB-VAR-24-10-29-L`, 6.6p / 36.28p; export
`E-1R-AGILE-OUTGOING-19-05-13-L`) for 31 Oct 2026 and the clock-change day 25 Oct 2026, using
`octopus.MEASUREMENTS_QUERY`. Anonymised payloads are in `apps/predbat/tests/billing_fixture.json`.

- **Units:** `estimatedAmount` is in **pence**, although `costCurrency` says `GBP`
  (1.485 kWh × 6.6p = `9.801`). Export amounts are rounded to 2 decimal places.
- **Statistic types:** import half-hours carry `CONSUMPTION_COST` rows (one per bucket:
  `Home Standard Rate`, `Home Low Rate`, `EV Low Rate`, `EV Standard Rate`, zero rows included) plus
  one `STANDING_CHARGE_COST` row. Export half-hours carry one `GENERATION_VALUE` row plus a
  zero `STANDING_CHARGE_COST` row.
- **No total row:** across 98 import half-hours the `CONSUMPTION_COST` kWh always summed to the
  node's `value`.
- **Standing charge:** apportioned to **every** half-hour (1.15674p each, 55.52p/day), with
  `value: null`. It is not landed on a single slot.
- **Export kWh:** the `GENERATION_VALUE` row has `value: null`; the kWh is the node's own `value`.
  Its cost is positive (earnings), e.g. 1.417 kWh → 44.86p at 18:00Z.
- **Export needs a reading direction:** the export MPAN returns **zero edges** with the plain
  query. Adding `readingDirection: GENERATION` to `electricityFilters` returns all 48 half-hours.
- **Clock change:** 25 Oct 2026 returned 50 half-hours.
- **VAT:** `costInclTax` equals `costExclTax` (GB electricity is zero-rated until 31 Mar 2027), so the
  basis cannot be told apart from data today. The billed rates (6.6p / 36.28p) match the tariff.
  `costInclTax` is used, which is the same basis as Predbat's `value_inc_vat` Octopus rates;
  **re-check after 1 Apr 2027**. The public products unit-rates endpoint returned no rows for
  this IOG-SMB tariff, so there was no independent REST cross-check.
- E.ON / EDF: not probed (no account available); Kraken tests use a synthetic single-bucket day.

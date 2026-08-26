# Teslemetry real-rate tariff & mode-driven export control

- **Date:** 2026-07-18
- **Component:** `apps/predbat/teslemetry.py` (Tesla Powerwall via Teslemetry/Fleet API)
- **Status:** Design approved, pending implementation plan

## Problem

The Powerwall has no native scheduler, so `teslemetry.py` emulates one by pushing a
synthetic `tariff_content_v2` and toggling operation mode. Today it publishes a **flat,
single-tier** tariff (`PREDBAT-NORMAL`) in every non-export state, and a single-hour
boosted-export tariff (`PREDBAT-EXPORT-NOW`) during export. Two problems:

1. **The customer's real tariff is clobbered.** Their Tesla app and the Powerwall's own
   cost/savings reporting show a fake flat rate, not their real time-of-use tariff.
2. **Export granularity is coarse.** The export trick advances a 30-minute-aligned,
   60-minute `ON_PEAK` window and re-pushes the tariff every 30 minutes, so export windows
   can't be shorter than the slot and the (possibly charged) `time_of_use_settings` command
   fires repeatedly.

## Goal

Publish the customer's **real rates** into the Powerwall tariff (for reporting accuracy in
their app), while keeping Predbat in full control of battery behaviour. Decouple export
start/stop from tariff pushes so it becomes per-cycle precise and cheap.

**Primary goal is reporting accuracy (cosmetic).** Predbat still drives ALL battery
behaviour via operation modes + the export boost; the Powerwall's own optimiser is not
relied upon for planning. This sets a *low fidelity bar* — the tariff must look like the
customer's rates, not serve as a control-grade signal.

## Tesla `tariff_content_v2` rules (constraints)

From the Tesla API documentation, the tariff must satisfy:

- At least one season present; seasons have arbitrary names and are date ranges
  (`fromMonth`/`fromDay` → `toMonth`/`toDay`). ToU periods live inside seasons and are
  day-of-week + hour/minute based.
- Rates are **per-season, per-tier**: `energy_charges[season].rates[tier] = price`. A tier
  therefore has **one price per season**.
- ToU labels may be any string, but the mobile app only *displays*
  `ON_PEAK`, `OFF_PEAK`, `PARTIAL_PEAK`, `SUPER_OFF_PEAK` — so we must stay inside those 4.
- Validation: no overlaps/gaps in time periods; no overlapping/gapped seasons; every tier
  with a price has periods and vice-versa (**matched tier sets**); **no negative prices**
  (rounded to 0 — use tax-inclusive); **buy price ≥ sell price** at any instant, else buy is
  clamped up to sell.
- `energy_charges.ALL` applies to all periods — recommended for genuinely flat tariffs
  instead of fabricating periods.
- Valid currencies include `GBP`.

## Data source

Predbat stores `rate_import` / `rate_export` as per-minute dicts over a ~48h horizon
(`fetch.py:basic_rates`, keyed by minutes from local midnight, replicated forward). With
Agile these differ every half-hour and every calendar day. The Tesla schema cannot express
48 distinct prices/day, so real rates must be **quantised into ≤3 tiers**.

## Design

### 1. Tariff = pure function of `(real rates, committed discharge window + enable)`

The published tariff depends on exactly two inputs and nothing else. This is what makes it
stable and cheap to change-detect (§2).

**Real-rate bands (cosmetic, always present).**

- Quantise over **fixed local calendar days** — today (minutes 0–1439) and tomorrow
  (1440–2879), NOT a rolling `now→now+48h` window. A stable input within a day means the
  quantised bands are identical cycle-to-cycle, so the §2 dedupe suppresses re-pushes; the
  bands only change at **midnight rollover** or when **new rate data lands** (Agile ~16:00).
- Quantise the combined today+tomorrow series into **≤3 tiers** (`SUPER_OFF_PEAK` / `OFF_PEAK`
  / `PARTIAL_PEAK`, cheapest→dearest), because one season means one price per tier:
  - ≤3 distinct rates (flat, Economy-7) → use them directly, one tier each (exact).
  - otherwise split `[min,max]` into 3 equal-width bands; each tier price = **mean of the
    real rates** that fell in it (representative, not a band edge).
  - clamp every tier price to `max(0, price)` (Agile export/plunge can go negative; Tesla
    would zero it anyway — we do it deterministically), and **round to whole pence**. The
    rounding both matches app display and stabilises the push trigger (§2) against sub-pence
    per-cycle rate nudges.
- **Placed on the real day-of-week:** today's shape on today's DOW, tomorrow's on tomorrow's,
  and **tomorrow's replicated onto the other 5 weekdays** so the week is fully tiled (Tesla's
  no-gaps rule). Publishing today + tomorrow is the whole point — hence DOW matters. Tesla uses
  **`0=Sunday`** (Python `weekday()` is Mon=0, so `tesla_dow = (weekday + 1) % 7`), isolated in
  `_tesla_dow`. Coalesce consecutive same-tier half-hours into ToU periods per day; assert each
  day partitions (reuse `_assert_tou_periods_partition_day`).
- **Buy and sell are quantised independently** — buy from `rate_import`, sell from
  `rate_export`, each with its own tiers/periods under its own `energy_charges` /
  `sell_tariff` block.
- **`ON_PEAK` is reserved exclusively for the synthetic export boost** — never used for a
  real band. Cosmetic trade-off (accepted): the customer's dearest real periods label as
  `PARTIAL_PEAK`, never `ON_PEAK`.
- **Buy ≥ sell:** normally import > export so this holds. In rare plunge half-hours where the
  real export rate exceeds import, Tesla clamps the displayed buy up to sell — accepted as a
  cosmetic blip; it never occurs inside a boosted window (buy = sell = boost) and never
  affects control (those states use `backup`/`self_consumption`, which ignore the tariff).

**Export boost (present only for the committed discharge window).**

- If `schedule["discharge"]["enable"]` is on, overlay an `ON_PEAK` period covering **exactly
  the committed discharge window `[start_time, end_time]`** — minute-precise (so sub-30-min
  windows work, no 30-min alignment forced), placed on the window's **real day(s) only**, chosen
  by the window's current-or-next occurrence **relative to now**:
  - A same-day window (`start < end`) that still **ends now or later today** → **today's DOW**; one
    that has **already ended today** (`end ≤ now`) → **tomorrow's DOW** (its next occurrence).
  - A window that **wraps midnight** (`start ≥ end`) → today's DOW for `[start, 24:00)` and
    tomorrow's DOW for `[00:00, end)`; but if we are already inside its post-midnight tail
    (`now < end`), only today's `[00:00, end)` head is boosted.
  - **Never all 7 days** — a boost band repeated every day would show a fake daily `ON_PEAK` peak
    in the app. (The midnight `sync_tariff` refresh keeps the chosen day correct as the date rolls.)
- Boost price = `max(EXPORT_SELL_RATE £0.50, 2 × horizon_max)` where `horizon_max` is the max
  across the published import **and** export tier prices. Being the strict maximum anywhere in
  the tariff, the discharge window is unambiguously the best sell moment and the device's
  look-ahead can't find a better future peak to wait for.
- Applied to **both** sell (drives export) and buy (mirror — blocks a grid-charge-to-export
  round-trip and satisfies buy ≥ sell inside the window, both = boost).
- **Carve upkeep:** on the affected day(s), remove the window from whichever real band(s)
  covered it (may split a period → re-coalesce; may empty a band → drop that tier from both
  rates and periods to keep the sets matched); each affected day still partitions.
- No boost when discharge is disabled — the idle tariff is clean real rates.

**Fallback** (no `base`, or empty rate dicts — unit tests, pre-first-fetch): build a
genuinely flat tariff via the **`ALL` field** with the default/current flat rate and no
fabricated periods. A committed discharge window still overlays its boost so export works
without real rates.

**One tariff, one code.** There is no longer a normal-vs-export tariff pair — it's a single
`PREDBAT` tariff (stable `code`/`name`) whose `ON_PEAK` band merely appears during a committed
discharge window and disappears afterward. The customer's app shows one consistently-named
tariff throughout. With `reconcile_on_start` removed (§4) nothing needs the old
`PREDBAT-EXPORT-NOW`/`PREDBAT-NORMAL` marker split, and the §2 JSON dedupe decides pushes, so
the `code` never has to change to trigger one. Drop the `mode` parameter from
`build_tariff` — the presence of the boost is decided solely by the committed discharge window.

### 2. When the tariff is pushed

`time_of_use_settings` costs no command credits, **but Teslemetry meters total API calls per
month**, so a tariff POST must fire only when strictly necessary — never per cycle. The tariff
is (re)built and pushed on exactly three triggers:

1. **Committed discharge window changes** — start/end/enable differ from the window baked into
   the currently-published tariff (detected in `apply_schedule()` on the write-button commit).
2. **Rate data changes** — the quantised band layout differs from what's published.
3. **Local-day rollover** — at local midnight the quantised day advances to the new day's
   rates, changing the bands (a special case of "rates changed").

**Enforcement — the built tariff *is* the signature, made stable by rounding.** Rather than a
bespoke gate, `sync_tariff()` rebuilds the tariff and pushes it through `set_tariff`'s existing
write-on-change dedupe (`_apply_command` keyed on `json.dumps(tariff, sort_keys=True)`). The key
is that the built tariff is **jitter-proof**: band prices are **rounded to whole pence** and both
the bands and `horizon_max` (the boost basis) are computed over the **fixed calendar-day** rate
set, so the small per-cycle nudges Predbat makes to `rate_import`/`rate_export` (IO dispatch
slots, manual overrides, intelligent adjustments) don't change a single byte of the tariff JSON
unless they genuinely shift a band or the discharge window. Byte-equality of a stabilised tariff
is therefore a sound change-detector — no separate signature structure needed.

`sync_tariff()` may be *called* each `run()` cycle and from `apply_schedule()` — the call is a
cheap local build + JSON compare; it only reaches the API on a real change. Expected cadence:
~1–2 pushes/day for rates + one per genuine discharge-window change. This is on top of, and far
smaller than, the existing `live_status`/energy GET polls, which dominate the monthly call
budget.

### 3. Per-cycle control = operation mode + export rule only

Both are free commands (`operation`, `grid_import_export`), evaluated every cycle, giving
**per-cycle / sub-30-min** start/stop precision:

- `evaluate_schedule` returns `{mode, export_rule, grid_charging, reserve}` — **`tariff_mode`
  is removed** from the per-cycle tuple.
- Charge (SOC < target): `backup` + grid charging on, `reserve = target`, `export_rule = pv_only`.
- Charge hold (SOC ≥ target): `backup` + grid charging off, `export_rule = pv_only`.
- Export (in discharge window, SOC > target): `autonomous` + `battery_ok` + `reserve = target`.
  The boost over that window (already in the tariff) makes autonomous export favourable.
- Export done / floor (SOC ≤ target): `self_consumption` + `pv_only` — export stops
  immediately regardless of the tariff still showing the boost.
- Idle: `self_consumption` + `pv_only` + grid charging on, `reserve = schedule reserve`.

`assert_device_state` pushes only these free per-cycle commands. The tariff is no longer part
of the per-cycle assert.

### 4. Consequent changes

- Remove the `tariff_mode` select entity and the `TARIFF_MODES` normal/export_now toggle
  (`register_control_entities`, `select_event`, `set_tariff` per-cycle usage).
- `evaluate_schedule` and `assert_device_state` drop `tariff_mode`.
- **Remove `reconcile_on_start` entirely** — along with `get_current_tariff_code`,
  `_find_tariff_code`, the `reconcile_done` latch and `RECONCILE_MAX_ATTEMPTS`, and their
  tests. It is unnecessary in this design (see "Boot behaviour" below).

## Boot behaviour (no reconcile needed)

On restart Predbat simply continues where it left off — there is no stale device state to
detect or repair, for three independent reasons:

1. **Cold boot re-asserts everything.** `_last_sent` (the dedupe cache) starts empty, so the
   first `assert_device_state` and first `sync_tariff` actually send — nothing is skipped.
   The device is fully re-driven from the persisted plan on the first live cycle.
2. **The plan is persisted.** `load_schedule` restores the committed charge/discharge windows,
   and `evaluate_schedule` re-derives the correct mode/export-rule from the live clock + SOC.
   If we crashed mid-export and are still inside the window, export resumes; if the window has
   passed or SOC has reached target, the first per-cycle assert stops it.
3. **The boost self-expires even if Predbat never restarts.** The `ON_PEAK` boost is pinned to
   the planned discharge window, so once the wall clock passes that window's end the export
   incentive is gone and the device falls back to real-rate behaviour. The reserve floor
   bounds the worst-case drain before then. (This is strictly safer than the old rolling-window
   trick, which relied on Predbat staying alive to keep advancing the window.)

## Control-behaviour dependency (validate on pilot hardware)

The design assumes — as the current shipping code already does — that:

- `self_consumption` reliably suppresses battery→grid export (stop is safe regardless of
  tariff), and
- `autonomous` + `battery_ok` + a boosted-now tariff reliably drives export.

Stop is inherently safe. The only residual risk is a coarse/committed boost failing to
trigger start, mitigated by boost = horizon max so "now" is unambiguously the best sell
moment. Confirm on live hardware during the pilot.

## Module structure

Keep `build_tariff` a thin composer; extract testable helpers:

- `_tesla_dow(python_weekday)` → `(python_weekday + 1) % 7` (Python Mon=0 → Tesla Sun=0).
- `_quantise_side(rate_dict, default_pence)` → `(tier_prices, today_tiers, tomorrow_tiers)` —
  today's and tomorrow's 48 slots quantised into ≤3 shared tiers, clamped ≥0, rounded to pence.
- `_side_layout(today_tiers, tomorrow_tiers, today_dow)` → `{tesla_dow: [(from,to,tier), …]}`
  for all 7 days: today's coalesced shape on `today_dow`, tomorrow's on `(today_dow+1)%7`,
  tomorrow's replicated on the rest; each day partitions `[0,1440)`.
- `_render_side(layout, tier_prices)` → one side's `energy_charges` + per-day `tou_periods`,
  matched tier sets; flat input → `ALL`.
- `_boost_segments(window, now_min)` → `[(day_offset, from, to), …]` choosing the boost's
  current-or-next occurrence day(s) (0=today/1=tomorrow) per the now-relative rule above.
- `_apply_boost(buy_layout, sell_layout, segments, today_dow)` → carve `ON_PEAK` onto
  `(today_dow + offset) % 7` for each segment, both sides.
- `build_tariff(discharge_window_or_none)` → compose buy + sell + optional boost + single
  `PREDBAT` code (no `mode` param).
- `sync_tariff()` → build from current rates + committed discharge window and push via
  `set_tariff`, whose JSON dedupe (jitter-proofed by the whole-pence rounding) reaches the API
  only on a genuine change. Called by `apply_schedule()` and once per `run()` cycle.

## Testing (all new helpers; repo requires unit tests for new code)

- `_quantise_side`: flat → 1 tier; 2 distinct (Economy-7) → 2 exact tiers; Agile-like → 3
  mean-priced bands; negative input clamped to 0; today and tomorrow both returned.
- `_side_layout`/DOW placement: today's shape on `_tesla_dow(today)`, tomorrow's on the next
  day, other 5 replicate tomorrow; every day partitions `[0,1440)`.
- `_boost_segments` day selection: same-day window ending later today → today; already ended
  today → tomorrow; wrap → today+tomorrow, or today's head only when inside the post-midnight tail.
- Boost overlay: `ON_PEAK` = strictly the max price; carved onto the chosen day(s) only; no
  gaps/overlaps; emptied band pruned from both rates and periods; buy mirror = sell in-window;
  minute-precise sub-30-min window; not on the other days; no boost when discharge disabled.
- `sync_tariff` dedupe: unchanged inputs → **zero API calls** across many cycles; sub-pence rate
  nudges that don't cross a band edge → no push; a changed discharge window (via
  `apply_schedule`), a genuine band shift, or a day rollover → exactly one push each.
- `evaluate_schedule`: updated states without `tariff_mode`; export uses `autonomous` +
  `battery_ok`; stop uses `self_consumption` + `pv_only`.
- Boot continuation: with a persisted schedule and an empty dedupe cache, the first cycle
  re-asserts mode + tariff from the committed plan (no reconcile); mid-window restart resumes
  export, post-window restart stops it.
- Fallback tariff is schema-valid (flat via `ALL`); boost still overlays on the flat base.
- Update/replace existing `build_tariff`, `_assert_tou_periods_partition_day`, and
  export-window tests; **delete** the `tariff_mode` and `reconcile_on_start` tests.

## Out of scope

- Relying on the Powerwall's own optimiser for planning (functional mode) — explicitly not a
  goal; Predbat remains the planner.
- Multi-season / per-calendar-day tier pricing (rejected: no-gaps-between-seasons rule would
  force full-year tiling for marginal cosmetic gain).
- Per-day distinct band *prices* (accepted loss: a band's displayed price is a today/tomorrow
  mean; the per-day *shape* is preserved).

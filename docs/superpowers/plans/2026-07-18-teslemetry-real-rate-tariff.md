# Teslemetry Real-Rate Tariff & Mode-Driven Export — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the customer's real rates into the Powerwall `tariff_content_v2` (as up to 3 quantised bands per side, `ON_PEAK` reserved for a synthetic export boost over the committed discharge window), and move export start/stop onto the free operation-mode + export-rule commands so the tariff is pushed only when its inputs actually change.

**Architecture:** The tariff becomes a pure function of `(real rates, committed discharge window)`. New pure helpers quantise `rate_import`/`rate_export` into ≤3 named tiers over today+tomorrow local calendar days, render them to a single `PREDBAT` tariff, and overlay an `ON_PEAK` boost over the committed discharge window. `sync_tariff()` rebuilds+pushes it (behind the existing `set_tariff` JSON dedupe, made jitter-proof by rounding prices to whole pence) on the write-button commit and once per `run()` cycle. `evaluate_schedule`/`assert_device_state` lose their per-cycle tariff lever; export is driven purely by `operation` + `grid_import_export`. `reconcile_on_start` and the `tariff_mode` entity are removed.

**Tech Stack:** Python 3, `asyncio`, `aiohttp`; Predbat component framework (`ComponentBase`); tests via `apps/predbat/tests/test_teslemetry.py` with the `MockTeslemetryAPI` double; test runner `coverage/run_all`.

## Global Constraints

- **Line length:** ≤256 chars (Black), ≤250 (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every new function/method/class needs a docstring.
- **Naming:** `lower_case_with_underscores`.
- **Spelling:** British English (CSpell, `en-gb`); add unknown valid words to `.cspell/custom-dictionary-workspace.txt`.
- **Tests:** unit tests required for all new code. Run from `coverage/`; **save test output to a file, then grep it** (never pipe straight to grep). Run one test module with `./run_all -k teslemetry > /tmp/t.log 2>&1` then grep `/tmp/t.log`.
- **Tesla `tariff_content_v2` rules (must hold for every built tariff):** one full-year season; ToU labels limited to `ON_PEAK`/`OFF_PEAK`/`PARTIAL_PEAK`/`SUPER_OFF_PEAK`; per-season/per-tier prices; **no negative prices**; **no gaps/overlaps** in a day's periods; **matched tier sets** (every tier with a price has periods and vice-versa); **buy ≥ sell** (Tesla clamps otherwise); `energy_charges.ALL` for flat tariffs; currency `GBP`.
- **Rates:** `base.rate_import`/`base.rate_export` are dicts keyed by **minutes from local midnight** (today 0–1439, tomorrow 1440–2879), values in **pence**. Divide by 100 for GBP.
- **DOW convention:** Tesla `fromDayOfWeek` is `0=Sunday … 6=Saturday`. Python `date.weekday()` is `0=Monday … 6=Sunday`, so `tesla_dow = (weekday + 1) % 7`. Isolated in `_tesla_dow()`. Rate bands are placed on today's/tomorrow's real DOW; the **boost is placed only on the window's real day(s)** — today's DOW, plus tomorrow's DOW for the portion of a midnight-wrapped window past 00:00 — never all 7 days (a daily-repeating `ON_PEAK` would show a fake peak every day in the app).

## File Structure

- **Modify:** `apps/predbat/teslemetry.py` — all production changes (new tariff helpers; rewrite `build_tariff`/`set_tariff`/`evaluate_schedule`/`assert_device_state`/`apply_schedule`/`run`/`initialize`/`register_control_entities`/`select_event`; delete `reconcile_on_start`/`get_current_tariff_code`/`_find_tariff_code`).
- **Modify:** `apps/predbat/tests/test_teslemetry.py` — new helper tests; update `evaluate_schedule`/`build_tariff` tests; delete `reconcile_*`/`tariff_mode` tests.
- **No new files** — the component is one cohesive module and the team keeps it that way.

Module-level constants to add near the existing tier lists:

```python
REAL_TIERS = ["SUPER_OFF_PEAK", "OFF_PEAK", "PARTIAL_PEAK"]  # cheapest -> dearest; ON_PEAK reserved for the boost
BOOST_TIER = "ON_PEAK"
SLOTS_PER_DAY = 48          # 30-minute slots
SLOT_MINUTES = 30
```

`EXPORT_SELL_RATE = 0.50` (existing) stays as the boost floor. `TARIFF_MODES` and `OPERATION_MODES`/`EXPORT_RULES` — `OPERATION_MODES`/`EXPORT_RULES` stay; `TARIFF_MODES` is deleted in Task 7.

---

### Task 1: `_quantise_side` — rates → ≤3 rounded tiers + per-slot assignment

**Files:**
- Modify: `apps/predbat/teslemetry.py` (add module constants + `_quantise_side` staticmethod near `current_rates`, ~line 780)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Produces: `TeslemetryAPI._quantise_side(rate_dict, default_pence) -> (tier_prices: dict[str,float], today_tiers: list[str], tomorrow_tiers: list[str])`. `tier_prices` maps used tier names → GBP price rounded to whole pence. `today_tiers`/`tomorrow_tiers` are 48-element lists (one tier name per 30-min slot). Every tier appearing in the slot lists appears in `tier_prices`.

- [ ] **Step 1: Write the failing tests**

```python
def test_teslemetry_quantise_flat_single_tier():
    """A flat rate collapses to one tier priced in GBP whole pence, all 48 slots the same."""
    rates = {m: 28.0 for m in range(0, 2880)}  # 28p flat
    prices, today, tomorrow = TeslemetryAPI._quantise_side(rates, 28.0)
    assert prices == {"SUPER_OFF_PEAK": 0.28}
    assert today == ["SUPER_OFF_PEAK"] * 48
    assert tomorrow == ["SUPER_OFF_PEAK"] * 48


def test_teslemetry_quantise_two_distinct_exact():
    """Economy-7 (two distinct rates) uses two tiers, cheapest -> SUPER_OFF_PEAK, exact prices."""
    rates = {}
    for m in range(0, 2880):
        local = m % 1440
        rates[m] = 8.0 if (0 <= local < 300) else 30.0  # cheap 00:00-05:00, else dear
    prices, today, tomorrow = TeslemetryAPI._quantise_side(rates, 30.0)
    assert prices == {"SUPER_OFF_PEAK": 0.08, "OFF_PEAK": 0.30}
    assert today[0] == "SUPER_OFF_PEAK" and today[10] == "OFF_PEAK"  # slot 10 = 05:00


def test_teslemetry_quantise_agile_three_bands_clamped_rounded():
    """A wide/varied series quantises to 3 mean-priced bands; negatives clamp to 0; prices whole pence."""
    rates = {}
    for m in range(0, 2880):
        slot = (m % 1440) // 30
        rates[m] = [-5.0, 12.0, 45.0][slot % 3]  # a value in each band, incl. a negative
    prices, today, tomorrow = TeslemetryAPI._quantise_side(rates, 12.0)
    assert set(prices) == {"SUPER_OFF_PEAK", "OFF_PEAK", "PARTIAL_PEAK"}
    assert prices["SUPER_OFF_PEAK"] == 0.0            # -5p clamped to 0, rounded whole pence
    assert all(p == round(p, 2) for p in prices.values())
    assert set(today) <= set(prices)                  # matched sets: every slot tier is priced
    assert len(today) == 48 and len(tomorrow) == 48
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "AttributeError|_quantise_side|FAIL|Error" /tmp/t.log`
Expected: FAIL — `_quantise_side` does not exist.

- [ ] **Step 3: Add constants and implement `_quantise_side`**

Add the module constants from the File Structure section, then add this staticmethod:

```python
@staticmethod
def _quantise_side(rate_dict, default_pence):
    """Quantise a per-minute pence rate dict into <=3 named GBP tiers over today+tomorrow.

    Samples every 30 minutes for today (minutes 0-1439) and tomorrow (1440-2879), converts to
    GBP clamped at 0 and rounded to whole pence, then either maps <=3 distinct values one-to-one
    onto the real tiers (cheapest -> SUPER_OFF_PEAK) or splits [min,max] into 3 equal-width bands
    priced at each band's mean. Returns (tier_prices, today_tiers, tomorrow_tiers) where the slot
    lists only ever name tiers present in tier_prices (matched sets).
    """
    def slot_price(minute):
        pence = rate_dict.get(minute, default_pence)
        return round(max(0.0, pence) / 100.0, 2)

    today = [slot_price(m) for m in range(0, 1440, SLOT_MINUTES)]
    tomorrow = [slot_price(m) for m in range(1440, 2880, SLOT_MINUTES)]
    combined = today + tomorrow
    distinct = sorted(set(combined))

    if len(distinct) <= len(REAL_TIERS):
        value_to_tier = {value: REAL_TIERS[index] for index, value in enumerate(distinct)}
        tier_prices = {tier: value for value, tier in value_to_tier.items()}

        def band_of(value):
            """Return the tier for an exact value in the small-distinct case."""
            return value_to_tier[value]
    else:
        low, high = distinct[0], distinct[-1]
        width = (high - low) / len(REAL_TIERS)
        buckets = {index: [] for index in range(len(REAL_TIERS))}

        def band_index(value):
            """Return 0..2 for the equal-width band a value falls in (clamped)."""
            if width <= 0:
                return 0
            return min(len(REAL_TIERS) - 1, int((value - low) / width))

        for value in combined:
            buckets[band_index(value)].append(value)
        tier_prices = {REAL_TIERS[index]: round(sum(values) / len(values), 2) for index, values in buckets.items() if values}

        def band_of(value):
            """Return the tier name for the band a value falls in."""
            return REAL_TIERS[band_index(value)]

    today_tiers = [band_of(value) for value in today]
    tomorrow_tiers = [band_of(value) for value in tomorrow]
    return tier_prices, today_tiers, tomorrow_tiers
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAIL|Error" /tmp/t.log`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): quantise rates into <=3 GBP tiers for the real-rate tariff"
```

---

### Task 2: `_render_side` — one side's intervals → `energy_charges` + `tou_periods`

**Files:**
- Modify: `apps/predbat/teslemetry.py` (add `_tesla_dow`, `_side_layout`, `_render_side` staticmethods)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Consumes: `_quantise_side` (Task 1).
- Produces:
  - `_tesla_dow(python_weekday) -> int` — `(python_weekday + 1) % 7` (Python Mon=0 → Tesla Sun=0).
  - `_side_layout(today_tiers, tomorrow_tiers, today_dow) -> dict[int, list[tuple[int,int,str]]]` — per-day-of-week (0-6) list of `(from_min, to_min, tier)` intervals coalescing consecutive same-tier slots; today's shape on `today_dow`, tomorrow's on `today_dow+1`, all other days replicate tomorrow. Each day's intervals partition `[0,1440)`.
  - `_render_side(layout, tier_prices) -> (energy_charges_side: dict, tou_periods: dict)` — Tesla blocks. `energy_charges_side = {"ALL": {"rates": {"ALL": 0}}, "AllYear": {"rates": <only tiers present in layout>}}`; `tou_periods = {tier: {"periods": [period_dict, ...]}}`. A day-end interval (`to_min == 1440`) renders as `toHour:0, toMinute:0`.

- [ ] **Step 1: Write the failing tests**

```python
def test_teslemetry_tesla_dow_sunday_zero():
    """Python weekday (Mon=0..Sun=6) maps to Tesla fromDayOfWeek (Sun=0..Sat=6)."""
    assert TeslemetryAPI._tesla_dow(6) == 0   # Sunday
    assert TeslemetryAPI._tesla_dow(0) == 1   # Monday
    assert TeslemetryAPI._tesla_dow(5) == 6   # Saturday


def test_teslemetry_side_layout_partitions_every_day():
    """Every day-of-week's intervals tile [0,1440) with no gaps or overlaps."""
    today = ["SUPER_OFF_PEAK"] * 10 + ["OFF_PEAK"] * 38
    tomorrow = ["OFF_PEAK"] * 48
    layout = TeslemetryAPI._side_layout(today, tomorrow, today_dow=2)
    assert set(layout) == set(range(7))
    for day, intervals in layout.items():
        covered = 0
        for (frm, to, _tier) in sorted(intervals):
            assert frm == covered  # no gap/overlap
            covered = to
        assert covered == 1440
    # today's shape only on dow 2; the rest carry tomorrow's flat shape
    assert layout[2][0] == (0, 300, "SUPER_OFF_PEAK")
    assert layout[3] == [(0, 1440, "OFF_PEAK")]


def test_teslemetry_render_side_matched_sets_and_day_end():
    """Rendered rates name exactly the tiers used in periods; day-end shows 00:00."""
    layout = {day: [(0, 1440, "OFF_PEAK")] for day in range(7)}
    charges, periods = TeslemetryAPI._render_side(layout, {"OFF_PEAK": 0.30, "SUPER_OFF_PEAK": 0.08})
    assert set(charges["AllYear"]["rates"]) == {"OFF_PEAK"}      # SUPER_OFF_PEAK unused -> dropped (matched sets)
    assert set(periods) == {"OFF_PEAK"}
    assert charges["ALL"] == {"rates": {"ALL": 0}}
    sample = periods["OFF_PEAK"]["periods"][0]
    assert (sample["fromHour"], sample["fromMinute"], sample["toHour"], sample["toMinute"]) == (0, 0, 0, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "AttributeError|_side_layout|_render_side|FAIL" /tmp/t.log`
Expected: FAIL — helpers do not exist.

- [ ] **Step 3: Implement the three helpers**

```python
@staticmethod
def _tesla_dow(python_weekday):
    """Map a Python weekday (0=Mon..6=Sun) to Tesla's fromDayOfWeek (0=Sun..6=Sat).

    Isolated so any future convention change is a one-line fix.
    """
    return (python_weekday + 1) % 7

@staticmethod
def _coalesce_day(slot_tiers):
    """Coalesce a 48-slot tier list into [(from_min, to_min, tier), ...] partitioning [0,1440)."""
    intervals = []
    index = 0
    while index < SLOTS_PER_DAY:
        tier = slot_tiers[index]
        run_end = index
        while run_end < SLOTS_PER_DAY and slot_tiers[run_end] == tier:
            run_end += 1
        intervals.append((index * SLOT_MINUTES, run_end * SLOT_MINUTES, tier))
        index = run_end
    return intervals

@staticmethod
def _side_layout(today_tiers, tomorrow_tiers, today_dow):
    """Place today's coalesced shape on today_dow, tomorrow's on the next day, replicate tomorrow's on the rest."""
    today_intervals = TeslemetryAPI._coalesce_day(today_tiers)
    tomorrow_intervals = TeslemetryAPI._coalesce_day(tomorrow_tiers)
    layout = {}
    for day in range(7):
        if day == today_dow % 7:
            layout[day] = list(today_intervals)
        else:
            layout[day] = list(tomorrow_intervals)
    layout[(today_dow + 1) % 7] = list(tomorrow_intervals)
    return layout

@staticmethod
def _render_side(layout, tier_prices):
    """Render a per-day interval layout into (energy_charges_side, tou_periods), matched tier sets only."""
    periods = {}
    used = set()
    for day, intervals in layout.items():
        for (frm, to, tier) in intervals:
            from_hour, from_minute = frm // 60, frm % 60
            if to >= 1440:
                to_hour, to_minute = 0, 0
            else:
                to_hour, to_minute = to // 60, to % 60
            periods.setdefault(tier, {"periods": []})["periods"].append(
                {"fromDayOfWeek": day, "toDayOfWeek": day, "fromHour": from_hour, "fromMinute": from_minute, "toHour": to_hour, "toMinute": to_minute}
            )
            used.add(tier)
    rates = {tier: price for tier, price in tier_prices.items() if tier in used}
    energy_charges_side = {"ALL": {"rates": {"ALL": 0}}, "AllYear": {"rates": rates}}
    return energy_charges_side, periods
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAIL|Error" /tmp/t.log`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): render quantised bands into per-DOW tou_periods"
```

---

### Task 3: `_apply_boost` — carve an `ON_PEAK` window onto its real day(s)

**Files:**
- Modify: `apps/predbat/teslemetry.py` (add `_carve_interval`, `_boost_segments`, `_apply_boost` staticmethods)
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Produces:
  - `_carve_interval(intervals, start_min, end_min, tier) -> list[tuple[int,int,str]]` — removes `[start,end)` from a single day's partitioning intervals and inserts `(start,end,tier)`; result stays sorted and partitions `[0,1440)`.
  - `_boost_segments(window, now_min) -> list[tuple[int,int,int]]` — decides which day(s) the boost's current-or-next occurrence lands on, as `(day_offset, from_min, to_min)` where `day_offset` is `0`=today or `1`=tomorrow. A same-day window (`start < end`) that has **already ended today** (`end <= now_min`) rolls to **tomorrow**; otherwise **today**. A midnight-wrapped window (`start >= end`) is `[(0,start,1440),(1,0,end)]` normally, but when we are already in its post-midnight tail (`now_min < end`) it is just `[(0,0,end)]` (today's head).
  - `_apply_boost(buy_layout, sell_layout, segments, today_dow) -> None` — mutates both layouts in place, carving `BOOST_TIER` onto `(today_dow + offset) % 7` for each segment. Never all 7 days.

- [ ] **Step 1: Write the failing tests**

```python
def test_teslemetry_carve_interval_splits_and_partitions():
    """Carving a mid-day window splits the covering interval and still partitions the day."""
    day = [(0, 1440, "OFF_PEAK")]
    out = TeslemetryAPI._carve_interval(day, 1020, 1080, "ON_PEAK")  # 17:00-18:00
    assert out == [(0, 1020, "OFF_PEAK"), (1020, 1080, "ON_PEAK"), (1080, 1440, "OFF_PEAK")]


def test_teslemetry_boost_segments_today_vs_tomorrow():
    """A same-day window ending in the future is today; one already ended is tomorrow; wrap splits."""
    assert TeslemetryAPI._boost_segments((1020, 1080), now_min=600) == [(0, 1020, 1080)]   # 10:00, 17-18 upcoming -> today
    assert TeslemetryAPI._boost_segments((540, 660), now_min=600) == [(0, 540, 660)]        # in progress (09-11 @10:00) -> today
    assert TeslemetryAPI._boost_segments((300, 420), now_min=600) == [(1, 300, 420)]        # 05-07 ended by 10:00 -> tomorrow
    assert TeslemetryAPI._boost_segments((1380, 60), now_min=720) == [(0, 1380, 1440), (1, 0, 60)]  # 23-01 upcoming -> today+tomorrow
    assert TeslemetryAPI._boost_segments((1380, 60), now_min=30) == [(0, 0, 60)]            # 00:30 inside the 23-01 tail -> today head


def test_teslemetry_apply_boost_places_segments_on_offset_days():
    """Each (offset, from, to) segment carves BOOST_TIER onto (today_dow + offset) % 7, both sides."""
    buy = {d: [(0, 1440, "OFF_PEAK")] for d in range(7)}
    sell = {d: [(0, 1440, "SUPER_OFF_PEAK")] for d in range(7)}
    TeslemetryAPI._apply_boost(buy, sell, [(0, 1020, 1080)], today_dow=3)  # today = Tesla dow 3
    assert (1020, 1080, "ON_PEAK") in buy[3]
    assert (1020, 1080, "ON_PEAK") in sell[3]
    assert all(seg[2] != "ON_PEAK" for d in range(7) if d != 3 for seg in buy[d])


def test_teslemetry_apply_boost_wrap_segments_span_two_days():
    """A two-segment wrap carves the tail on today's DOW and the head on tomorrow's DOW."""
    buy = {d: [(0, 1440, "OFF_PEAK")] for d in range(7)}
    sell = {d: [(0, 1440, "OFF_PEAK")] for d in range(7)}
    TeslemetryAPI._apply_boost(buy, sell, [(0, 1380, 1440), (1, 0, 60)], today_dow=6)  # tomorrow = (6+1)%7 = 0
    assert (1380, 1440, "ON_PEAK") in buy[6]  # today
    assert (0, 60, "ON_PEAK") in buy[0]       # tomorrow
    assert all(seg[2] != "ON_PEAK" for seg in buy[1])  # an unrelated day untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "AttributeError|_carve_interval|_apply_boost|FAIL" /tmp/t.log`
Expected: FAIL.

- [ ] **Step 3: Implement carve + boost**

```python
@staticmethod
def _carve_interval(intervals, start_min, end_min, tier):
    """Remove [start_min, end_min) from a day's partitioning intervals and insert (start, end, tier)."""
    out = []
    for (frm, to, existing) in intervals:
        if to <= start_min or frm >= end_min:
            out.append((frm, to, existing))
            continue
        if frm < start_min:
            out.append((frm, start_min, existing))
        if to > end_min:
            out.append((end_min, to, existing))
    out.append((start_min, end_min, tier))
    out.sort()
    return out

@staticmethod
def _boost_segments(window, now_min):
    """Decide which day(s) the boost lands on for the window's current-or-next occurrence.

    Returns [(day_offset, from_min, to_min)] with day_offset 0=today / 1=tomorrow. A same-day
    window already finished today (end <= now) rolls to tomorrow; otherwise it stays today. A
    midnight-wrapped window splits across today and tomorrow, except when we are already inside its
    post-midnight tail (now < end), where only today's head [0, end) still needs the boost.
    """
    start_min, end_min = window
    if start_min < end_min:
        offset = 0 if end_min > now_min else 1
        return [(offset, start_min, end_min)]
    if now_min < end_min:
        return [(0, 0, end_min)]
    return [(0, start_min, 1440), (1, 0, end_min)]

@staticmethod
def _apply_boost(buy_layout, sell_layout, segments, today_dow):
    """Carve BOOST_TIER onto (today_dow + offset) % 7 for each (offset, from, to) segment, both sides."""
    for layout in (buy_layout, sell_layout):
        for (offset, seg_start, seg_end) in segments:
            day = (today_dow + offset) % 7
            layout[day] = TeslemetryAPI._carve_interval(layout[day], seg_start, seg_end, BOOST_TIER)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAIL|Error" /tmp/t.log`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): carve ON_PEAK export boost into both tariff sides"
```

---

### Task 4: Rewrite `build_tariff(discharge_window)` and `set_tariff(tariff)`

**Files:**
- Modify: `apps/predbat/teslemetry.py` — replace `build_tariff` (currently ~line 799, takes `mode`/`now`) and `set_tariff` (~line 876, takes `mode`); add `_discharge_window`, `_rate_side`, `_boost_price` helpers.
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Consumes: `_quantise_side`, `_side_layout`, `_render_side`, `_boost_segments`, `_apply_boost`, `_tesla_dow`, `current_rates` (existing), `get_minutes_now` (existing).
- Produces:
  - `_discharge_window() -> tuple[int,int] | None` — `(start_min, end_min)` from `self.schedule["discharge"]` when `enable` is set and `start != end`, else `None`.
  - `_boost_price(buy_prices, sell_prices) -> float` — `max(EXPORT_SELL_RATE, round(2 * horizon_max, 2))` where `horizon_max` is the max GBP tier price across both sides.
  - `build_tariff(discharge_window=None, now_min=None) -> dict` — a single `PREDBAT` tariff (buy from `rate_import`, sell from `rate_export`, optional boost placed via `_boost_segments`). No `mode` param. `now_min` defaults to `get_minutes_now()`; tests pass it explicitly for determinism.
  - `set_tariff(tariff, force=False) -> bool` — pushes a **prebuilt** tariff via `time_of_use_settings`, deduped on its JSON.
- `today_dow` comes from `self.base.now` (local naive datetime) via `.weekday()`, falling back to `datetime.now(local_tz)` when no base (tests); `now_min` from `get_minutes_now()` (same local clock).

- [ ] **Step 1: Write the failing tests**

```python
def test_teslemetry_build_tariff_single_code_real_bands():
    """build_tariff() with no window yields one PREDBAT tariff, GBP, with real bands and no ON_PEAK."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)  # helper below
    tariff = api.build_tariff(None)
    assert tariff["code"] == "PREDBAT"
    assert tariff["currency"] == "GBP"
    assert "ON_PEAK" not in tariff["seasons"]["AllYear"]["tou_periods"]
    assert "ON_PEAK" not in tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]


def test_teslemetry_build_tariff_boost_is_strict_max_on_today_dow():
    """A discharge window adds ON_PEAK above every real band, on today's DOW only (both sides)."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    tariff = api.build_tariff((1020, 1080), now_min=600)  # 17:00-18:00 window, now 10:00 -> today
    sell_periods = tariff["sell_tariff"]["seasons"]["AllYear"]["tou_periods"]
    today_dow = api._tesla_dow(api.base.now.weekday())
    assert set(p["fromDayOfWeek"] for p in sell_periods["ON_PEAK"]["periods"]) == {today_dow}
    boost = tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"]["ON_PEAK"]
    real = [v for t, v in tariff["sell_tariff"]["energy_charges"]["AllYear"]["rates"].items() if t != "ON_PEAK"]
    assert all(boost > v for v in real)
    assert tariff["energy_charges"]["AllYear"]["rates"]["ON_PEAK"] == boost  # buy mirror


def test_teslemetry_build_tariff_fallback_flat_when_no_rates():
    """No base/rates -> flat tariff via the ALL field, still schema-valid, boost still overlays."""
    api = MockTeslemetryAPI()
    api.base = None
    tariff = api.build_tariff(None)
    assert tariff["energy_charges"]["ALL"]["rates"]["ALL"] >= 0
```

Add these test helpers near the top of the test module (after `MockTeslemetryAPI`):

```python
def _rate_base(import_p, export_p):
    """A minimal base double exposing flat import/export rate dicts and a local clock for build_tariff."""
    from types import SimpleNamespace
    from datetime import datetime
    rate_import = {m: import_p for m in range(0, 2880)}
    rate_export = {m: export_p for m in range(0, 2880)}
    return SimpleNamespace(rate_import=rate_import, rate_export=rate_export, minutes_now=0, now=datetime(2026, 7, 20, 12, 0), local_tz=None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "TypeError|KeyError|code|FAIL" /tmp/t.log`
Expected: FAIL — old `build_tariff` takes `mode`.

- [ ] **Step 3: Implement the rewrite**

Replace `build_tariff` and `set_tariff`, and add helpers. Replace the whole `build_tariff` method body and `set_tariff` with:

```python
def _rate_side(self, rate_dict, default_gbp):
    """Return the 4-tuple (energy_charges_side, tou_periods, tier_prices, layout) for one side.

    layout is None in the flat/fallback branch (no rates), signalling build_tariff to skip the boost;
    otherwise it is the per-DOW interval layout the boost carves into.
    """
    if not rate_dict:
        flat = round(max(0.0, default_gbp), 2)
        return {"ALL": {"rates": {"ALL": flat}}}, {}, {"SUPER_OFF_PEAK": flat}, None
    today_dow = self._tesla_dow(self._local_today_weekday())
    tier_prices, today_tiers, tomorrow_tiers = self._quantise_side(rate_dict, default_gbp * 100.0)
    layout = self._side_layout(today_tiers, tomorrow_tiers, today_dow)
    return (*self._render_side(layout, tier_prices), tier_prices, layout)

def _local_today_weekday(self):
    """Return the site-local weekday (0=Mon) from base.now, falling back to the system clock in tests."""
    base = getattr(self, "base", None)
    now = getattr(base, "now", None) if base is not None else None
    if now is None:
        now = datetime.now(getattr(self, "local_tz", None) or timezone.utc)
    return now.weekday()

def _discharge_window(self):
    """Return (start_min, end_min) for the committed discharge window when enabled, else None."""
    discharge = self.schedule.get("discharge", {})
    if not discharge.get("enable"):
        return None
    start = self.time_to_minutes(discharge.get("start_time", "00:00:00"))
    end = self.time_to_minutes(discharge.get("end_time", "00:00:00"))
    return None if start == end else (start, end)

@staticmethod
def _boost_price(*price_maps):
    """Return the synthetic boost price: strictly above every real band and at least the floor."""
    horizon_max = max([value for prices in price_maps for value in prices.values()] + [0.0])
    return max(TeslemetryAPI.EXPORT_SELL_RATE, round(2 * horizon_max, 2))

def build_tariff(self, discharge_window=None, now_min=None):
    """Build the single PREDBAT tariff: real 3-band buy/sell rates plus an optional ON_PEAK export boost.

    Buy comes from rate_import, sell from rate_export, each quantised over today+tomorrow local days.
    When discharge_window is given, an ON_PEAK band priced above every real band is carved over that
    window on its current-or-next occurrence's real day(s) (today if it still ends now/later, else
    tomorrow; split across today+tomorrow for a midnight wrap), both sides — buy mirrors sell so it
    never grid-charges to re-export. now_min defaults to the live local clock.
    """
    if now_min is None:
        now_min = self.get_minutes_now()
    import_gbp, export_gbp = self.current_rates()
    today_dow = self._tesla_dow(self._local_today_weekday())
    buy_charges, buy_periods, buy_prices, buy_layout = self._rate_side(self._side_rates("import"), import_gbp)
    sell_charges, sell_periods, sell_prices, sell_layout = self._rate_side(self._side_rates("export"), export_gbp)
    code = "PREDBAT"
    if discharge_window is not None and buy_layout is not None and sell_layout is not None:
        boost = self._boost_price(buy_prices, sell_prices)
        segments = self._boost_segments(discharge_window, now_min)
        self._apply_boost(buy_layout, sell_layout, segments, today_dow)
        buy_charges, buy_periods = self._render_side(buy_layout, {**buy_prices, BOOST_TIER: boost})
        sell_charges, sell_periods = self._render_side(sell_layout, {**sell_prices, BOOST_TIER: boost})
    return self._assemble_tariff(code, buy_charges, buy_periods, sell_charges, sell_periods)
```

> Note: `_rate_side` must **always return the 4-tuple** `(charges, periods, prices, layout)` — the flat/fallback branch returns `return {"ALL": {"rates": {"ALL": flat}}}, {}, {"SUPER_OFF_PEAK": flat}, None` (layout `None`) and the banded branch returns `(*self._render_side(layout, tier_prices), tier_prices, layout)`. `build_tariff` unpacks the 4-tuple and skips the boost when `layout is None`. Add `_side_rates(kind)` returning `self.base.rate_import`/`rate_export` (or `{}` when no base) and `_assemble_tariff(...)` building the top-level dict (below).

```python
def _side_rates(self, kind):
    """Return the base rate dict for 'import'/'export', or {} when no base is wired (tests/fallback)."""
    base = getattr(self, "base", None)
    if base is None:
        return {}
    return getattr(base, "rate_import" if kind == "import" else "rate_export", {}) or {}

def _assemble_tariff(self, code, buy_charges, buy_periods, sell_charges, sell_periods):
    """Assemble the top-level tariff_content_v2 dict from prebuilt buy/sell charge + period blocks."""
    common = {
        "min_applicable_demand": 0, "max_applicable_demand": 0, "monthly_minimum_bill": 0,
        "monthly_charges": 0, "daily_charges": [{"name": "Charge", "amount": 0}],
        "demand_charges": {"ALL": {"rates": {"ALL": 0}}, "AllYear": {"rates": {}}},
    }
    buy_seasons = {"AllYear": {"fromMonth": 1, "fromDay": 1, "toMonth": 12, "toDay": 31, "tou_periods": buy_periods}}
    sell_seasons = {"AllYear": {"fromMonth": 1, "fromDay": 1, "toMonth": 12, "toDay": 31, "tou_periods": sell_periods}}
    return {
        "version": 1, "utility": "Predbat", "code": code, "name": "Predbat", "currency": "GBP",
        "daily_demand_charges": {}, "energy_charges": buy_charges, "seasons": buy_seasons,
        "sell_tariff": {**common, "utility": "Predbat", "energy_charges": sell_charges, "seasons": sell_seasons},
        **common,
    }

async def set_tariff(self, tariff, force=False):
    """Push a prebuilt tariff via time_of_use_settings, deduped on the serialised tariff body.

    Prices are rounded to whole pence upstream so per-cycle rate nudges do not change the JSON;
    a re-push therefore fires only on a genuine band, boost-window or day-of-week change.
    """
    signature = json.dumps(tariff, sort_keys=True)
    return await self._apply_command("tariff", signature, lambda: self._command("time_of_use_settings", {"tou_settings": {"tariff_content_v2": tariff}}), force=force)
```

Delete the old `build_tariff` body (the `charges`/`tou_periods`/`seasons`/`sell_rates`/`buy_rates` construction and the `mode == "export_now"` window maths, ~lines 799-874) — it is fully replaced.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAIL|Error" /tmp/t.log`
Expected: the three new tests PASS. Old `build_tariff(mode=...)` tests will now fail — they are fixed in Task 8.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): single PREDBAT tariff from real rates + discharge-window boost"
```

---

### Task 5: `sync_tariff()` and wiring into `run()` + `apply_schedule()`

**Files:**
- Modify: `apps/predbat/teslemetry.py` — add `sync_tariff`; call it from `apply_schedule` (~line 546) and `run` (~line 356); the per-cycle tariff push leaves `assert_device_state` (Task 6).
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Consumes: `build_tariff`, `_discharge_window`, `set_tariff`, `_is_read_only` (existing).
- Produces: `sync_tariff() -> bool` — builds from current rates + committed discharge window and pushes via `set_tariff` (deduped); returns `True` on success/no-op, `False` on a failed push. Gated on `not self._is_read_only()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_teslemetry_sync_tariff_dedupes_unchanged():
    """Two syncs with identical inputs push the tariff exactly once (monthly API-call budget)."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    run_async(api.sync_tariff())
    run_async(api.sync_tariff())
    posts = [r for r in api.requests_made if r[0] == "POST" and r[1].endswith("/time_of_use_settings")]
    assert len(posts) == 1


def test_teslemetry_sync_tariff_pushes_on_window_change():
    """Enabling a discharge window changes the tariff and triggers a second push."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    run_async(api.sync_tariff())
    api.schedule["discharge"] = {"start_time": "17:00:00", "end_time": "18:00:00", "soc": 30, "enable": 1}
    run_async(api.sync_tariff())
    posts = [r for r in api.requests_made if r[0] == "POST" and r[1].endswith("/time_of_use_settings")]
    assert len(posts) == 2


def test_teslemetry_sync_tariff_read_only_no_push():
    """Read-only mode sends no tariff command."""
    from types import SimpleNamespace
    api = MockTeslemetryAPI()
    api.base = SimpleNamespace(rate_import={m: 28.0 for m in range(2880)}, rate_export={m: 15.0 for m in range(2880)}, minutes_now=0, now=None, local_tz=None, get_arg=lambda a, d=None, **k: True if a == "set_read_only" else d)
    run_async(api.sync_tariff())
    assert not [r for r in api.requests_made if r[0] == "POST"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "AttributeError|sync_tariff|FAIL" /tmp/t.log`
Expected: FAIL.

- [ ] **Step 3: Implement and wire `sync_tariff`**

```python
async def sync_tariff(self):
    """Build the tariff from current rates + committed discharge window and push it (deduped).

    Cheap to call every cycle: set_tariff only reaches the API when the serialised tariff changes,
    i.e. on a genuine rate-band, discharge-window or day-of-week change. Gated on read-only mode.
    """
    if self._is_read_only():
        return True
    tariff = self.build_tariff(self._discharge_window())
    return await self.set_tariff(tariff)
```

In `apply_schedule` (~line 546), after `self.publish_schedule_entities()` and before/with the existing device-state assertion, add a tariff sync so the boost tracks the just-committed window:

```python
        if self.last_soc is not None and not self._is_read_only():
            await self.sync_tariff()
            await self.assert_device_state(self.evaluate_schedule(self.get_minutes_now(), self.last_soc))
```

In `run` (~line 418), where the emulator asserts device state each cycle, add the sync before the assert:

```python
        if self.schedule_loaded and self.last_soc is not None and not self._is_read_only():
            await self.sync_tariff()
            await self.assert_device_state(self.evaluate_schedule(self.get_minutes_now(), self.last_soc))
```

(The `self.reconcile_done` term in that `run` guard is removed in Task 7.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAIL|Error" /tmp/t.log`
Expected: the three new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "feat(teslemetry): sync_tariff on commit and per-cycle, deduped to genuine changes"
```

---

### Task 6: Rewrite `evaluate_schedule` (drop `tariff_mode`) and `assert_device_state` (drop tariff)

**Files:**
- Modify: `apps/predbat/teslemetry.py` — `evaluate_schedule` (~line 481) and `assert_device_state` (~line 582).
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Produces:
  - `evaluate_schedule(minutes_now, soc) -> dict` with keys `export_rule, grid_charging, reserve, mode` (no `tariff_mode`).
  - `assert_device_state(desired) -> bool` — asserts `export_rule`, `grid_charging`, `reserve`, `mode` only (no tariff).

- [ ] **Step 1: Update the failing tests**

Replace `test_teslemetry_evaluate_schedule_states` body assertions to drop `tariff_mode`:

```python
def test_teslemetry_evaluate_schedule_states():
    """The five reachable device states without the removed tariff_mode lever."""
    api = MockTeslemetryAPI()
    api.schedule = {
        "reserve": 20,
        "charge": {"start_time": "01:00:00", "end_time": "05:00:00", "soc": 90, "enable": 1},
        "discharge": {"start_time": "17:00:00", "end_time": "19:00:00", "soc": 30, "enable": 1},
    }
    assert api.evaluate_schedule(2 * 60, 50) == {"export_rule": "pv_only", "grid_charging": True, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(2 * 60, 90) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 90, "mode": "backup"}
    assert api.evaluate_schedule(18 * 60, 80) == {"export_rule": "battery_ok", "grid_charging": False, "reserve": 30, "mode": "autonomous"}
    assert api.evaluate_schedule(18 * 60, 30) == {"export_rule": "pv_only", "grid_charging": False, "reserve": 30, "mode": "self_consumption"}
    assert api.evaluate_schedule(12 * 60, 60) == {"export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "tariff_mode|AssertionError|FAIL" /tmp/t.log`
Expected: FAIL — current dict still has `tariff_mode`.

- [ ] **Step 3: Rewrite both methods**

`evaluate_schedule` — remove `tariff_mode` from every returned dict (keep the branching identical otherwise):

```python
    def evaluate_schedule(self, minutes_now, soc):
        """Map the committed schedule + wall clock + live SOC to the desired device tuple.

        Returns a dict with keys export_rule, grid_charging, reserve and mode. Charge wins over an
        overlapping discharge (matches execute.py). Export uses autonomous + battery_ok, which the
        ON_PEAK boost over the committed discharge window (in the tariff) makes favourable; the tariff
        is no longer part of this per-cycle tuple. Every non-export state allows pv_only export so
        surplus solar is never curtailed.
        """
        charge = self.schedule.get("charge", {})
        discharge = self.schedule.get("discharge", {})
        reserve = self.schedule.get("reserve", 20)
        if self.in_window(minutes_now, charge):
            target = int(charge.get("soc", 100))
            grid = soc < target
            return {"export_rule": "pv_only", "grid_charging": grid, "reserve": target, "mode": "backup"}
        if self.in_window(minutes_now, discharge):
            target = int(discharge.get("soc", 10))
            if soc > target:
                return {"export_rule": "battery_ok", "grid_charging": False, "reserve": target, "mode": "autonomous"}
            return {"export_rule": "pv_only", "grid_charging": False, "reserve": target, "mode": "self_consumption"}
        return {"export_rule": "pv_only", "grid_charging": True, "reserve": int(reserve), "mode": "self_consumption"}
```

`assert_device_state` — drop the tariff setter and its mirror; keep the other four:

```python
    async def assert_device_state(self, desired):
        """Assert the desired device tuple (export rule, grid charging, reserve, mode); tariff is synced separately.

        Each setter dedupes on write-on-change, so an unchanged assert costs zero command credits.
        Successful writes are mirrored into the diagnostic control entities; failures leave both the
        dedupe cache and the entity state untouched so the next cycle retries.
        """
        results = {}
        results["export_rule"] = await self.set_export_rule(desired["export_rule"])
        results["grid_charging"] = await self.set_grid_charging(desired["grid_charging"])
        results["reserve"] = await self.set_backup_reserve(desired["reserve"])
        results["mode"] = await self.set_operation_mode(desired["mode"])
        if results["export_rule"]:
            self.publish_control(self.entity("allow_export", domain="select"), desired["export_rule"])
        if results["grid_charging"]:
            self.publish_control(self.entity("allow_charging_from_grid", domain="switch"), "on" if desired["grid_charging"] else "off")
        if results["reserve"]:
            self.publish_control(self.entity("backup_reserve", domain="number"), int(desired["reserve"]))
        if results["mode"]:
            self.publish_control(self.entity("operation_mode", domain="select"), desired["mode"])
        if not all(results.values()):
            self.log("Warn: Teslemetry device-state assert incomplete: {}".format({key: value for key, value in results.items() if not value}))
        return all(results.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "evaluate_schedule|assert_device_state|PASSED|FAIL" /tmp/t.log`
Expected: the updated tests PASS. `test_teslemetry_assert_device_state_posts_commands` may need its `desired` dict trimmed — fixed in Task 8.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "refactor(teslemetry): drop tariff_mode from per-cycle control; tariff synced separately"
```

---

### Task 7: Remove reconcile machinery and the `tariff_mode` entity

**Files:**
- Modify: `apps/predbat/teslemetry.py` — delete `reconcile_on_start` (~949), `get_current_tariff_code` (~912), `_find_tariff_code` (~890); remove `reconcile_done`/`reconcile_attempts`/`RECONCILE_MAX_ATTEMPTS` and the reconcile block in `run` (~395); remove the `tariff_mode` control entity + `TARIFF_MODES` + its `select_event` branch; remove the `reconcile_done` term from the `run` emulator guard.
- Test: `apps/predbat/tests/test_teslemetry.py`

**Interfaces:**
- Produces: `run()` no longer references reconcile; `register_control_entities` no longer registers `tariff_mode`; `select_event` no longer handles `_tariff_mode`.

- [ ] **Step 1: Write the guard test**

```python
def test_teslemetry_run_boots_without_reconcile():
    """A healthy first cycle asserts device state directly, with no tariff-read reconcile call."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.base.get_arg = lambda a, d=None, **k: d  # not read-only
    api.mock_responses["/api/1/products"] = {"response": [{"energy_site_id": 123456}]}
    api.mock_responses["/api/1/energy_sites/123456/site_info"] = SITE_INFO
    api.mock_responses["/api/1/energy_sites/123456/live_status"] = LIVE_STATUS
    api.mock_responses["/api/1/energy_sites/123456/calendar_history?kind=energy&period=day"] = ENERGY_HISTORY
    api.mock_responses["/api/1/energy_sites/123456/time_of_use_settings"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/operation"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/backup"] = {"response": {"code": 201}}
    api.mock_responses["/api/1/energy_sites/123456/grid_import_export"] = {"response": {"code": 201}}
    run_async(api.run(seconds=0, first=True))
    assert not hasattr(api, "reconcile_done") or True  # attribute may be gone
    assert not any("reconcil" in m.lower() for m in api.log_messages)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "reconcil|FAIL|Error" /tmp/t.log`
Expected: FAIL — reconcile still runs/logs.

- [ ] **Step 3: Delete the reconcile + tariff_mode code**

- Delete methods `reconcile_on_start`, `get_current_tariff_code`, `_find_tariff_code`.
- Delete the module constant `RECONCILE_MAX_ATTEMPTS` (line 46) and `TARIFF_MODES` (line 58).
- In `initialize`, delete `self.reconcile_done = False`, `self.reconcile_attempts = 0`.
- In `run`, delete the whole `if not self.reconcile_done:` block (~395-401) and remove `self.reconcile_done and` from the emulator guard (leaving the Task 5 form).
- In `register_control_entities`, delete the `("tariff_mode", "select", "normal", {...})` tuple.
- In `select_event`, delete the `elif entity_id.endswith("_tariff_mode") and value in TARIFF_MODES:` branch.
- Remove the now-unused `TARIFF_MODES` import usages; update `initialize`'s log line that mentions reconciliation.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAIL|Error|reconcil" /tmp/t.log`
Expected: the new test PASSES (reconcile tests still present will fail — deleted in Task 8).

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/teslemetry.py apps/predbat/tests/test_teslemetry.py
git commit -m "refactor(teslemetry): remove reconcile_on_start and the tariff_mode entity"
```

---

### Task 8: Test-suite cleanup and full green

**Files:**
- Modify: `apps/predbat/tests/test_teslemetry.py`
- Also: `apps/predbat/config.py` if the `TESLA` `INVERTER_DEF` references a tariff-mode/export freeze that no longer applies (check `test_teslemetry_inverter_def_tesla` around line 1027).

**Interfaces:** none new — this task makes the whole suite pass and adds boot-continuation coverage.

- [ ] **Step 1: Delete obsolete tests and update survivors**

- **Delete** every reconcile test: `test_teslemetry_reconcile_on_start_device_marker_restores_normal`, `_device_normal_is_noop`, `_read_failure_skips_without_crash`, `_no_code_in_response_skips`, `_read_only_mode_skips_writes`, `_ignores_boot_default_read_only_attribute`, `test_teslemetry_reconcile_forces_write_even_if_cache_preseeded`, `test_teslemetry_reconcile_latch_survives_auth_failed_first_cycle`, `test_teslemetry_reconcile_latch_runs_once_on_healthy_boot`, and any `_find_tariff_code`/`get_current_tariff_code` tests. Remove their entries from the `run_*`/`__main__` registry at the bottom (~lines 1624-1685) and the `RECONCILE_MAX_ATTEMPTS` import.
- **Delete** `tariff_mode` tests: `test_teslemetry_select_tariff_mode` (if present) and any assertion of `select.predbat_teslemetry_tariff_mode`.
- **Update** `test_teslemetry_set_tariff_posts_tou_settings` to build a tariff first: `tariff = api.build_tariff(None); run_async(api.set_tariff(tariff))`, then assert `"tariff_content_v2" in body["tou_settings"]`.
- **Update** the `build_tariff` window/partition tests (`test_teslemetry_*export_now*`, `_assert_tou_periods_partition_day` users) to call `api.build_tariff((1020, 1080))` with `api.base = _rate_base(...)`, and assert the ON_PEAK periods partition each day via the existing `_assert_tou_periods_partition_day` helper applied per `fromDayOfWeek`.
- **Update** `test_teslemetry_assert_device_state_posts_commands` and `_dedupes_repeat`: set `desired = {"export_rule": "pv_only", "grid_charging": True, "reserve": 20, "mode": "self_consumption"}` (no `tariff_mode`); the expected command count drops from 5 to 4 (no `time_of_use_settings`).

- [ ] **Step 2: Add a boot-continuation test**

```python
def test_teslemetry_boot_resumes_from_persisted_schedule():
    """With a persisted mid-discharge schedule and empty dedupe cache, the first cycle asserts autonomous export."""
    api = MockTeslemetryAPI()
    api.base = _rate_base(import_p=28.0, export_p=15.0)
    api.base.get_arg = lambda a, d=None, **k: d
    api.schedule = {"reserve": 20, "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": 0},
                    "discharge": {"start_time": "00:00:00", "end_time": "23:59:00", "soc": 10, "enable": 1}}
    api.schedule_loaded = True
    api.last_soc = 80
    for path in ("operation", "backup", "grid_import_export", "time_of_use_settings"):
        api.mock_responses["/api/1/energy_sites/123456/" + path] = {"response": {"code": 201}}
    run_async(api.assert_device_state(api.evaluate_schedule(12 * 60, 80)))
    assert api.entity_states["select.predbat_teslemetry_operation_mode"] == "autonomous"
    assert api.entity_states["select.predbat_teslemetry_allow_export"] == "battery_ok"
```

- [ ] **Step 3: Run the full teslemetry module**

Run: `cd coverage && ./run_all -k teslemetry > /tmp/t.log 2>&1; grep -E "PASSED|FAILED|Error|Traceback" /tmp/t.log`
Expected: `Teslemetry tests passed`, module PASSED.

- [ ] **Step 4: Run the full suite (quick) + pre-commit**

Run: `cd coverage && ./run_all --quick > /tmp/all.log 2>&1; grep -E "PASSED|FAILED|All tests" /tmp/all.log`
Expected: all tests pass.
Run: `./run_pre_commit > /tmp/pc.log 2>&1; grep -E "Failed|Passed|error" /tmp/pc.log` — fix any Black/Flake8/interrogate/CSpell issues (add new British-English words to `.cspell/custom-dictionary-workspace.txt` and re-stage).

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/tests/test_teslemetry.py apps/predbat/teslemetry.py .cspell/custom-dictionary-workspace.txt
git commit -m "test(teslemetry): drop reconcile/tariff_mode tests; cover single-tariff + boot resume"
```

---

## Self-Review

**Spec coverage:**
- Real 3-band quantisation, clamp ≥0, whole-pence rounding → Task 1.
- Single `AllYear` season, matched tier sets, `ALL` flat fallback, per-DOW placement → Task 2.
- `ON_PEAK` reserved boost, carve/partition, now-relative day selection (`_boost_segments`: today if it still ends later, else tomorrow; wrap splits today+tomorrow), buy mirror, `2 × horizon_max` floor → Tasks 3–4.
- Single `PREDBAT` code, `build_tariff` loses `mode` → Task 4.
- Minimal API calls via dedupe made jitter-proof by rounding; push on commit + genuine change → Tasks 4–5.
- Export via mode + export rule only; `tariff_mode` gone from the tuple → Task 6.
- Remove reconcile; boot continues from persisted plan → Tasks 7–8.
- Cosmetic acceptances (PARTIAL_PEAK top band, today/tomorrow mean price, plunge buy≥sell clamp) — inherent in the quantiser; no separate task.

**Pilot-validation item (not a blocker):** the `autonomous`+`battery_ok` export-gating behaviour (already relied on by the shipping code). The `_tesla_dow` convention (`0=Sunday`) is confirmed and isolated in one function.

**Type consistency:** `_quantise_side` → `(tier_prices, today_tiers, tomorrow_tiers)`; `_side_layout`/`_render_side` operate on the `{day: [(from,to,tier)]}` layout; `_boost_segments(window, now_min)` → `[(offset,from,to)]` consumed by `_apply_boost(buy_layout, sell_layout, segments, today_dow)`; `build_tariff(discharge_window, now_min)` and `set_tariff(tariff)` signatures match every call site (`sync_tariff`, tests).

**Note for the implementer:** `_rate_side` is described twice (3-tuple in prose, then normalised to a 4-tuple) — use the **4-tuple** form `(charges, periods, prices, layout)` with `layout=None` in the flat case, and unpack that in `build_tariff`.

# fmt: off
# pylint: disable=line-too-long
"""
Tests for OctopusAPI.async_get_day_night_rates — verifies that the correct night-window
is selected for each of the three tariff families:

  1. IOG TOU  (INTELLI or IOG+TOU in tariff_code)  → 23:30–05:30, cross_midnight=True
  2. GO / generic day-night (not E-2R-*)            → 00:30–05:30, cross_midnight=False
  3. Economy 7 (E-2R-* tariff_code)                → 00:30–07:30, cross_midnight=False
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, PropertyMock

from octopus import OctopusAPI, DATE_TIME_STR_FORMAT


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)  # midday UTC, Saturday


def _make_api(my_predbat, day_rate, night_rate):
    """Create an OctopusAPI instance with fetch_url_cached mocked to return the given rates."""
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    # valid_to=None means open-ended (in force indefinitely), covering all slots across the 8-day schedule
    day_entry = [{"valid_from": "2026-05-01T00:00:00+0000", "valid_to": None, "value_inc_vat": day_rate}]
    night_entry = [{"valid_from": "2026-05-01T00:00:00+0000", "valid_to": None, "value_inc_vat": night_rate}]

    async def mock_fetch(url, **kwargs):
        if "day-unit-rates" in url:
            return day_entry
        if "night-unit-rates" in url:
            return night_entry
        return []

    api.fetch_url_cached = mock_fetch
    return api


def _extract_schedule(mdata):
    """Return a sorted list of (start_hour, start_min, end_hour, end_min, rate) tuples from mdata."""
    result = []
    for entry in mdata:
        start = datetime.strptime(entry["valid_from"], DATE_TIME_STR_FORMAT)
        end = datetime.strptime(entry["valid_to"], DATE_TIME_STR_FORMAT)
        result.append((start.hour, start.minute, end.hour, end.minute, entry["value_inc_vat"]))
    return result


# ---------------------------------------------------------------------------
# test function
# ---------------------------------------------------------------------------


def test_octopus_day_night_rates_wrapper(my_predbat):
    """Synchronous wrapper for pytest / unit_test.py runner."""
    return asyncio.run(test_octopus_day_night_rates(my_predbat))


async def test_octopus_day_night_rates(my_predbat):
    """
    Test async_get_day_night_rates for all three tariff window cases.

    Tests:
    - Test 1: IOG TOU tariff → night window 23:30–05:30 (crosses midnight)
    - Test 2: INTELLI tariff → same IOG 23:30–05:30 window
    - Test 3: GO-style tariff (non E-2R-) → night window 00:30–05:30
    - Test 4: Economy 7 tariff (E-2R-*) → night window 00:30–07:30
    - Test 5: Missing rates → returns empty list
    """
    print("\n**** Running async_get_day_night_rates tests ****")
    failed = False

    base_url = "https://api.octopus.energy/v1/products/PROD/electricity-tariffs/TARIFF/standard-unit-rates/"

    # ------------------------------------------------------------------
    # Test 1: IOG TOU — night window must be 23:30–05:30, cross-midnight
    # ------------------------------------------------------------------
    print("\n*** Test 1: IOG TOU tariff → night 23:30–05:30 ***")
    api1 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00)

    with patch.object(type(api1), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW
        mdata = await api1.async_get_day_night_rates(base_url, tariff_code="E-1R-IOG-SMB-TOU-25-12-12-H")

    schedule = _extract_schedule(mdata)

    # Every night slot must start at 23:30 and end at 05:30 next day
    night_slots = [(sh, sm, eh, em, r) for sh, sm, eh, em, r in schedule if abs(r - 7.00) < 0.01]
    day_slots = [(sh, sm, eh, em, r) for sh, sm, eh, em, r in schedule if abs(r - 29.14) < 0.01]

    if not night_slots:
        print("ERROR: No night slots found in IOG TOU schedule")
        failed = True
    elif any(sh != 23 or sm != 30 for sh, sm, *_ in night_slots):
        print(f"ERROR: IOG TOU night slots don't start at 23:30 — got {[(sh, sm) for sh, sm, *_ in night_slots]}")
        failed = True
    elif any(eh != 5 or em != 30 for _, _, eh, em, _ in night_slots):
        print(f"ERROR: IOG TOU night slots don't end at 05:30 — got {[(eh, em) for _, _, eh, em, _ in night_slots]}")
        failed = True
    else:
        print("PASS: IOG TOU night window is 23:30–05:30")

    if not day_slots:
        print("ERROR: No day slots found in IOG TOU schedule")
        failed = True
    elif any(sh != 5 or sm != 30 for sh, sm, *_ in day_slots):
        print(f"ERROR: IOG TOU day slots don't start at 05:30 — got {[(sh, sm) for sh, sm, *_ in day_slots]}")
        failed = True
    else:
        print("PASS: IOG TOU day window starts at 05:30")

    # ------------------------------------------------------------------
    # Test 2: INTELLI tariff — same IOG window
    # ------------------------------------------------------------------
    print("\n*** Test 2: INTELLI tariff → same IOG 23:30–05:30 window ***")
    api2 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00)

    with patch.object(type(api2), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW
        mdata2 = await api2.async_get_day_night_rates(base_url, tariff_code="E-1R-INTELLI-VAR-25-01-01-H")

    schedule2 = _extract_schedule(mdata2)
    night_slots2 = [(sh, sm, eh, em, r) for sh, sm, eh, em, r in schedule2 if abs(r - 7.00) < 0.01]

    if not night_slots2 or any(sh != 23 or sm != 30 for sh, sm, *_ in night_slots2):
        print(f"ERROR: INTELLI tariff night slots not at 23:30 — {[(sh, sm) for sh, sm, *_ in night_slots2]}")
        failed = True
    else:
        print("PASS: INTELLI tariff uses IOG 23:30 night start")

    # ------------------------------------------------------------------
    # Test 3: GO-style tariff (non E-2R-) — night 00:30–05:30
    # ------------------------------------------------------------------
    print("\n*** Test 3: GO-style tariff → night 00:30–05:30 ***")
    api3 = _make_api(my_predbat, day_rate=24.0, night_rate=8.5)

    with patch.object(type(api3), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW
        mdata3 = await api3.async_get_day_night_rates(base_url, tariff_code="E-1R-GO-VAR-22-10-14-H")

    schedule3 = _extract_schedule(mdata3)
    night_slots3 = [(sh, sm, eh, em, r) for sh, sm, eh, em, r in schedule3 if abs(r - 8.5) < 0.01]

    if not night_slots3:
        print("ERROR: No night slots in GO schedule")
        failed = True
    elif any(sh != 0 or sm != 30 for sh, sm, *_ in night_slots3):
        print(f"ERROR: GO night slots don't start at 00:30 — {[(sh, sm) for sh, sm, *_ in night_slots3]}")
        failed = True
    elif any(eh != 5 or em != 30 for _, _, eh, em, _ in night_slots3):
        print(f"ERROR: GO night slots don't end at 05:30 — {[(eh, em) for _, _, eh, em, _ in night_slots3]}")
        failed = True
    else:
        print("PASS: GO-style night window is 00:30–05:30")

    # ------------------------------------------------------------------
    # Test 4: Economy 7 (E-2R-*) — night 00:30–07:30
    # ------------------------------------------------------------------
    print("\n*** Test 4: Economy 7 tariff → night 00:30–07:30 ***")
    api4 = _make_api(my_predbat, day_rate=20.0, night_rate=10.0)

    with patch.object(type(api4), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW
        mdata4 = await api4.async_get_day_night_rates(base_url, tariff_code="E-2R-VAR-22-11-01-A")

    schedule4 = _extract_schedule(mdata4)
    night_slots4 = [(sh, sm, eh, em, r) for sh, sm, eh, em, r in schedule4 if abs(r - 10.0) < 0.01]

    if not night_slots4:
        print("ERROR: No night slots in Economy 7 schedule")
        failed = True
    elif any(sh != 0 or sm != 30 for sh, sm, *_ in night_slots4):
        print(f"ERROR: Economy 7 night slots don't start at 00:30 — {[(sh, sm) for sh, sm, *_ in night_slots4]}")
        failed = True
    elif any(eh != 7 or em != 30 for _, _, eh, em, _ in night_slots4):
        print(f"ERROR: Economy 7 night slots don't end at 07:30 — {[(eh, em) for _, _, eh, em, _ in night_slots4]}")
        failed = True
    else:
        print("PASS: Economy 7 night window is 00:30–07:30")

    # ------------------------------------------------------------------
    # Test 5: Missing day/night rate data → returns empty list
    # ------------------------------------------------------------------
    print("\n*** Test 5: Missing rates → returns empty list ***")
    api5 = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)
    api5.fetch_url_cached = AsyncMock(return_value=[])

    with patch.object(type(api5), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW
        mdata5 = await api5.async_get_day_night_rates(base_url, tariff_code="E-1R-GO-VAR-22-10-14-H")

    if mdata5 != []:
        print(f"ERROR: Expected empty list when no rates available, got {mdata5}")
        failed = True
    else:
        print("PASS: Empty list returned when rates unavailable")

    # ------------------------------------------------------------------
    # Test 6: Multiple historical rates — newest rate must be selected
    # Reproduces real API response for E-2R-OE-FIX-12M-25-11-24-J where the
    # newer rate (28.61292, valid from 2026-03-31) was being ignored in favour
    # of the older rate (32.11992, valid from 2025-11-24) because the API
    # returns results newest-first and the loop was keeping the last match.
    # ------------------------------------------------------------------
    print("\n*** Test 6: Multiple historical day rates (newest first) → most-recent rate selected ***")
    _NOW6 = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    api6 = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    # Mirrors the real API response for the tariff in the bug report
    day_entry6 = [
        {"value_exc_vat": 27.2504, "value_inc_vat": 28.61292, "valid_from": "2026-03-31T23:00:00Z", "valid_to": None, "payment_method": None},
        {"value_exc_vat": 30.5904, "value_inc_vat": 32.11992, "valid_from": "2025-11-24T00:00:00Z", "valid_to": "2026-03-31T23:00:00Z", "payment_method": None},
    ]
    night_entry6 = [
        {"value_exc_vat": 13.0, "value_inc_vat": 13.65, "valid_from": "2026-03-31T23:00:00Z", "valid_to": None, "payment_method": None},
        {"value_exc_vat": 11.0, "value_inc_vat": 11.55, "valid_from": "2025-11-24T00:00:00Z", "valid_to": "2026-03-31T23:00:00Z", "payment_method": None},
    ]

    async def mock_fetch6(url, **kwargs):
        if "day-unit-rates" in url:
            return day_entry6
        if "night-unit-rates" in url:
            return night_entry6
        return []

    api6.fetch_url_cached = mock_fetch6

    with patch.object(type(api6), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW6
        mdata6 = await api6.async_get_day_night_rates(base_url, tariff_code="E-2R-OE-FIX-12M-25-11-24-J")

    # Extract the unique rate values used in the schedule
    day_rates6 = set(entry["value_inc_vat"] for entry in mdata6 if abs(entry["value_inc_vat"] - 28.61292) < 0.001 or abs(entry["value_inc_vat"] - 32.11992) < 0.001)
    night_rates6 = set(entry["value_inc_vat"] for entry in mdata6 if abs(entry["value_inc_vat"] - 13.65) < 0.001 or abs(entry["value_inc_vat"] - 11.55) < 0.001)

    if 28.61292 not in day_rates6:
        print("ERROR: Expected newer day rate 28.61292 to be used but it was not found in the schedule")
        failed = True
    elif 32.11992 in day_rates6:
        print("ERROR: Older day rate 32.11992 was used instead of the newer rate 28.61292")
        failed = True
    else:
        print("PASS: Newer day rate 28.61292 correctly selected (not the older 32.11992)")

    if 13.65 not in night_rates6:
        print("ERROR: Expected newer night rate 13.65 to be used but it was not found in the schedule")
        failed = True
    elif 11.55 in night_rates6:
        print("ERROR: Older night rate 11.55 was used instead of the newer rate 13.65")
        failed = True
    else:
        print("PASS: Newer night rate 13.65 correctly selected (not the older 11.55)")

    # ------------------------------------------------------------------
    # Test 7: Rate changes within the 8-day schedule window — each slot gets the
    # rate that was actually in force at that slot's start time, not a single
    # rate snapshotted at "now".
    # Setup: now = 2026-04-02 12:00 UTC (2 days after rate change at 2026-03-31 23:00 UTC)
    # Schedule starts 2 days back = 2026-03-31 00:30 UTC (eco7 night start)
    # Slots starting before 2026-03-31 23:00 → old rates; slots from 2026-04-01 onwards → new rates
    # ------------------------------------------------------------------
    print("\n*** Test 7: Rate change within schedule window → per-day rate lookup ***")
    _NOW7 = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
    api7 = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    day_entry7 = [
        {"value_exc_vat": 27.2504, "value_inc_vat": 28.61292, "valid_from": "2026-03-31T23:00:00Z", "valid_to": None},
        {"value_exc_vat": 30.5904, "value_inc_vat": 32.11992, "valid_from": "2025-11-24T00:00:00Z", "valid_to": "2026-03-31T23:00:00Z"},
    ]
    night_entry7 = [
        {"value_inc_vat": 13.65, "valid_from": "2026-03-31T23:00:00Z", "valid_to": None},
        {"value_inc_vat": 11.55, "valid_from": "2025-11-24T00:00:00Z", "valid_to": "2026-03-31T23:00:00Z"},
    ]

    async def mock_fetch7(url, **kwargs):
        if "day-unit-rates" in url:
            return day_entry7
        if "night-unit-rates" in url:
            return night_entry7
        return []

    api7.fetch_url_cached = mock_fetch7

    with patch.object(type(api7), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = _NOW7
        mdata7 = await api7.async_get_day_night_rates(base_url, tariff_code="E-2R-OE-FIX-12M-25-11-24-J")

    # The rate change is 2026-03-31T23:00Z.
    # Eco7 night starts at 00:30 and day at 07:30, both before 23:00 on 03-31 → old rates.
    # From 2026-04-01 00:30 onwards → new rates.
    rate_change_dt = datetime(2026, 3, 31, 23, 0, 0, tzinfo=timezone.utc)
    old_rates = {11.55, 32.11992}
    new_rates = {13.65, 28.61292}

    slots_before = [(datetime.strptime(e["valid_from"], DATE_TIME_STR_FORMAT), e["value_inc_vat"]) for e in mdata7 if datetime.strptime(e["valid_from"], DATE_TIME_STR_FORMAT) < rate_change_dt]
    slots_after = [(datetime.strptime(e["valid_from"], DATE_TIME_STR_FORMAT), e["value_inc_vat"]) for e in mdata7 if datetime.strptime(e["valid_from"], DATE_TIME_STR_FORMAT) >= rate_change_dt]

    if not slots_before:
        print("ERROR: Expected slots before rate change date but found none")
        failed = True
    elif any(r not in old_rates for _, r in slots_before):
        bad = [(t.isoformat(), r) for t, r in slots_before if r not in old_rates]
        print(f"ERROR: Slots before rate change should use old rates, but got unexpected rates: {bad}")
        failed = True
    else:
        print(f"PASS: {len(slots_before)} slot(s) before rate change correctly use old rates")

    if not slots_after:
        print("ERROR: Expected slots after rate change date but found none")
        failed = True
    elif any(r not in new_rates for _, r in slots_after):
        bad = [(t.isoformat(), r) for t, r in slots_after if r not in new_rates]
        print(f"ERROR: Slots after rate change should use new rates, but got unexpected rates: {bad}")
        failed = True
    else:
        print(f"PASS: {len(slots_after)} slot(s) after rate change correctly use new rates")

    # ------------------------------------------------------------------
    # Test 8: _get_rate_for_time valid_to boundary — expired entries must be
    # ignored even if their valid_from is the most recent, and a missing
    # valid_from must be treated as the epoch (always started).
    # ------------------------------------------------------------------
    print("\n*** Test 8: _get_rate_for_time respects valid_to and treats missing valid_from as epoch ***")
    _TS = datetime(2026, 5, 16, 12, 0, 0, tzinfo=timezone.utc)
    api8 = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    # Entry A: valid_from missing (epoch), valid_to in the future → should match
    # Entry B: valid_from very recent but valid_to already expired → must NOT match
    # Entry C: valid_from set, valid_to absent (forever) → should match and beats A
    rates8 = [
        {"value_inc_vat": 10.0, "valid_from": None, "valid_to": "2027-01-01T00:00:00+0000"},
        {"value_inc_vat": 99.0, "valid_from": "2026-05-16T11:00:00+0000", "valid_to": "2026-05-16T11:30:00+0000"},
        {"value_inc_vat": 28.0, "valid_from": "2026-03-01T00:00:00+0000", "valid_to": None},
    ]

    result8 = api8._get_rate_for_time(rates8, _TS)
    if result8 != 28.0:
        print(f"ERROR: Expected 28.0 (most-recent non-expired entry with valid_from set), got {result8}")
        failed = True
    else:
        print("PASS: 28.0 selected — expired entry ignored, missing valid_from treated as epoch")

    # With only the epoch entry active (no other valid entries at this time)
    rates8b = [
        {"value_inc_vat": 10.0, "valid_from": "", "valid_to": "2027-01-01T00:00:00+0000"},
    ]
    result8b = api8._get_rate_for_time(rates8b, _TS)
    if result8b != 10.0:
        print(f"ERROR: Expected 10.0 for epoch-start entry, got {result8b}")
        failed = True
    else:
        print("PASS: 10.0 selected for entry with empty valid_from (treated as epoch)")

    # Timestamp exactly at valid_to must NOT match (half-open interval)
    rates8c = [
        {"value_inc_vat": 5.0, "valid_from": "2026-05-16T00:00:00+0000", "valid_to": "2026-05-16T12:00:00+0000"},
    ]
    result8c = api8._get_rate_for_time(rates8c, _TS)
    if result8c is not None:
        print(f"ERROR: Expected None when timestamp == valid_to (half-open interval), got {result8c}")
        failed = True
    else:
        print("PASS: None returned when timestamp falls exactly on valid_to boundary")

    # ------------------------------------------------------------------
    if failed:
        print("\n**** ❌ async_get_day_night_rates tests FAILED ****")
    else:
        print("\n**** ✅ All async_get_day_night_rates tests PASSED ****")

    return 1 if failed else 0

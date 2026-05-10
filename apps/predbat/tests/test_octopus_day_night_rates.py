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

    day_entry = [{"valid_from": "2026-05-10T00:00:00+0000", "valid_to": "2026-05-10T23:30:00+0000", "value_inc_vat": day_rate}]
    night_entry = [{"valid_from": "2026-05-10T00:00:00+0000", "valid_to": "2026-05-10T05:30:00+0000", "value_inc_vat": night_rate}]

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
    if failed:
        print("\n**** ❌ async_get_day_night_rates tests FAILED ****")
    else:
        print("\n**** ✅ All async_get_day_night_rates tests PASSED ****")

    return 1 if failed else 0

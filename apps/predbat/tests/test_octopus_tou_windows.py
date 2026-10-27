# fmt: off
# pylint: disable=line-too-long
"""
Tests for off-peak window determination in OctopusAPI.

Two behaviours are covered:

  1. The hard-wired fallback windows in OCTOPUS_NIGHT_RATE_WINDOWS are UTC ("Z") times, so they
     must be anchored to UTC midnight rather than local midnight. Octopus documents the Economy 7
     smart-meter window as a fixed 00:30-07:30 UTC period, which lands at 01:30-08:30 during BST.

  2. The meter's real windows are read from the measurements API's TOU_BUCKET_COST labels when
     they are available, and the hard-wired times are used only when that lookup fails.
"""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch, PropertyMock

import pytz

from octopus import OctopusAPI, DATE_TIME_STR_FORMAT, _night_time_to_minutes, _windows_from_night_times, _localise_wall_clock


LONDON = pytz.timezone("Europe/London")

# Midday during British Summer Time. now_utc_exact is local-timezone aware in production, so this
# is what the production code actually receives - and it is what makes a local-anchored window wrong.
_NOW_BST = LONDON.localize(datetime(2026, 8, 20, 12, 0, 0))
# Midday during GMT, when local and UTC coincide and either anchor gives the same answer.
_NOW_GMT = LONDON.localize(datetime(2026, 1, 20, 12, 0, 0))


def _make_api(my_predbat, day_rate, night_rate, now):
    """Create an OctopusAPI whose rate fetches are stubbed and whose clock is pinned to `now`."""
    api = OctopusAPI(my_predbat, key="test-key", account_id="test-account", automatic=False)

    day_entry = [{"valid_from": "2026-01-01T00:00:00+0000", "valid_to": None, "value_inc_vat": day_rate}]
    night_entry = [{"valid_from": "2026-01-01T00:00:00+0000", "valid_to": None, "value_inc_vat": night_rate}]

    async def mock_fetch(url, **kwargs):
        """Return the stubbed day or night unit rates for the requested endpoint."""
        if "day-unit-rates" in url:
            return day_entry
        if "night-unit-rates" in url:
            return night_entry
        return []

    api.fetch_url_cached = mock_fetch
    api.mpan = "1100014811702"
    return api, now


async def _run(api_and_now, base_url, tariff_code):
    """Call async_get_day_night_rates with now_utc_exact pinned, since it is a read-only property."""
    api, now = api_and_now
    with patch.object(type(api), "now_utc_exact", new_callable=PropertyMock) as mock_now:
        mock_now.return_value = now
        return await api.async_get_day_night_rates(base_url, tariff_code=tariff_code)


def _night_slots_utc(mdata, night_rate):
    """Return (start, end) datetimes in UTC for every night-rate slot in mdata."""
    slots = []
    for entry in mdata:
        if abs(entry["value_inc_vat"] - night_rate) > 0.001:
            continue
        start = datetime.strptime(entry["valid_from"], DATE_TIME_STR_FORMAT).astimezone(timezone.utc)
        end = datetime.strptime(entry["valid_to"], DATE_TIME_STR_FORMAT).astimezone(timezone.utc)
        slots.append((start, end))
    return slots


def _night_slots_local(mdata, night_rate):
    """Return (start, end) datetimes in Europe/London for every night-rate slot in mdata."""
    slots = []
    for entry in mdata:
        if abs(entry["value_inc_vat"] - night_rate) > 0.001:
            continue
        start = datetime.strptime(entry["valid_from"], DATE_TIME_STR_FORMAT).astimezone(LONDON)
        end = datetime.strptime(entry["valid_to"], DATE_TIME_STR_FORMAT).astimezone(LONDON)
        slots.append((start, end))
    return slots


def _stub_tou(api_and_now, labelled_windows_utc):
    """
    Stub the measurements GraphQL query so the given UTC windows come back labelled NIGHT_RATE.

    labelled_windows_utc is a list of (start_minute, end_minute) from UTC midnight.
    """
    api = api_and_now[0]

    def label_for(minute_of_day):
        """Return the TOU bucket label for a given minute offset from UTC midnight."""
        for start, end in labelled_windows_utc:
            if start <= minute_of_day < end:
                return "NIGHT_RATE"
        return "DAY_RATE"

    async def mock_query(query, context, **kwargs):
        """Return a stubbed measurements response carrying one TOU label per half-hour slot."""
        if "measurements" not in query:
            return None
        edges = []
        base = datetime(2026, 8, 18, 0, 0, 0, tzinfo=timezone.utc)
        for slot in range(48):
            start = base.replace(hour=slot // 2, minute=(slot % 2) * 30)
            edges.append(
                {
                    "node": {
                        "value": "0.100000",
                        "unit": "kwh",
                        "startAt": start.strftime(DATE_TIME_STR_FORMAT),
                        "metaData": {"statistics": [{"type": "TOU_BUCKET_COST", "label": label_for(slot * 30), "costInclTax": {"estimatedAmount": "1.0"}}]},
                    }
                }
            )
        return {"account": {"properties": [{"measurements": {"edges": edges}}]}}

    api.async_graphql_query = mock_query


@contextmanager
def _night_times(my_predbat, value):
    """Set octopus_night_times on the shared fixture for the block (None clears it), restoring the prior state after."""
    had_key = "octopus_night_times" in my_predbat.args
    saved = my_predbat.args.get("octopus_night_times")
    if value is None:
        my_predbat.args.pop("octopus_night_times", None)
    else:
        my_predbat.args["octopus_night_times"] = value
    try:
        yield
    finally:
        if had_key:
            my_predbat.args["octopus_night_times"] = saved
        else:
            my_predbat.args.pop("octopus_night_times", None)


def _stub_tou_unavailable(api_and_now):
    """Stub the measurements query so it returns nothing, as it would for a meter with no history."""
    api = api_and_now[0]

    async def mock_query(query, context, **kwargs):
        """Return nothing, as the measurements query would for a meter with no billed history."""
        return None

    api.async_graphql_query = mock_query


def test_octopus_tou_windows_wrapper(my_predbat):
    """Synchronous wrapper for the unit_test.py runner."""
    return asyncio.run(test_octopus_tou_windows(my_predbat))


async def test_octopus_tou_windows(my_predbat):
    """
    Test UTC anchoring of the fallback windows and TOU-label based window detection.

    Tests:
    - Test 1: Economy 7 during BST uses 00:30-07:30 UTC (01:30-08:30 local), not 00:30 local
    - Test 2: Economy 7 during GMT still uses 00:30-07:30 UTC
    - Test 3: A non-standard TOU window from the measurements API overrides the hard-wired one
    - Test 4: A multi-block TOU schedule produces multiple night windows per day
    - Test 5: GO falls back to a hard-wired window anchored to local time, not UTC
    - Test 6: IOG likewise keeps its local wall-clock window through BST
    - Test 7: Economy 7 is UTC-anchored and GO is local-anchored at the same instant
    - Test 8: octopus_night_times configures the windows manually, in local time
    - Test 9: a manually configured window may be given in UTC with utc: true
    - Test 10: manual configuration beats the meter's own TOU labels
    - Test 11: an hour of 24 is normalised to 0, matching how basic_rates parses times
    - Test 12: an IOG meter's TOU labels never become a recurring schedule (issue: phantom slot)
    - Test 13: a TOU-derived window on a local wall-clock tariff holds across a DST change
    - Test 14: the hard-wired GO window keeps 8 contiguous nights across both clock changes
    - Test 15: IOG and manual windows in the changeover hour stay contiguous and never invert
    - Test 16: _localise_wall_clock directly, and a window wholly inside the skipped hour
    """
    print("\n**** Running Octopus TOU window tests ****")
    failed = False

    base_url = "https://api.oegb-kraken.energy/v1/products/PROD/electricity-tariffs/TARIFF/standard-unit-rates/"
    eco7 = "E-2R-OE-FIX-12M-26-06-11-B"

    # ------------------------------------------------------------------
    # Test 1: Economy 7 in BST must anchor the 00:30-07:30 window to UTC
    # ------------------------------------------------------------------
    print("\n*** Test 1: Economy 7 during BST -> 00:30-07:30 UTC ***")
    api1 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou_unavailable(api1)
    mdata1 = await _run(api1, base_url, eco7)
    slots1 = _night_slots_utc(mdata1, 13.04)

    if not slots1:
        print("ERROR: no night slots produced for Economy 7 in BST")
        failed = True
    else:
        bad = [(s, e) for s, e in slots1 if (s.hour, s.minute) != (0, 30) or (e.hour, e.minute) != (7, 30)]
        if bad:
            print("ERROR: BST night slots are not 00:30-07:30 UTC - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in bad[:3]]))
            failed = True
        else:
            local_start = slots1[0][0].astimezone(LONDON)
            print("PASS: BST night window is 00:30-07:30 UTC ({} local)".format(local_start.strftime("%H:%M")))

    # ------------------------------------------------------------------
    # Test 2: Economy 7 in GMT must give the same UTC window
    # ------------------------------------------------------------------
    print("\n*** Test 2: Economy 7 during GMT -> 00:30-07:30 UTC ***")
    api2 = _make_api(my_predbat, day_rate=30.16, night_rate=13.44, now=_NOW_GMT)
    _stub_tou_unavailable(api2)
    mdata2 = await _run(api2, base_url, eco7)
    slots2 = _night_slots_utc(mdata2, 13.44)

    if not slots2 or any((s.hour, s.minute) != (0, 30) or (e.hour, e.minute) != (7, 30) for s, e in slots2):
        print("ERROR: GMT night slots are not 00:30-07:30 UTC - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots2[:3]]))
        failed = True
    else:
        print("PASS: GMT night window is 00:30-07:30 UTC")

    # ------------------------------------------------------------------
    # Test 3: a real TOU schedule from the API overrides the hard-wired window
    # ------------------------------------------------------------------
    print("\n*** Test 3: TOU labels override the hard-wired window ***")
    api3 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou(api3, [(120, 360)])  # 02:00-06:00 UTC, deliberately not the Economy 7 default
    mdata3 = await _run(api3, base_url, eco7)
    slots3 = _night_slots_utc(mdata3, 13.04)

    if not slots3:
        print("ERROR: no night slots produced when TOU labels were available")
        failed = True
    elif any((s.hour, s.minute) != (2, 0) or (e.hour, e.minute) != (6, 0) for s, e in slots3):
        print("ERROR: TOU window not honoured - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots3[:3]]))
        failed = True
    else:
        print("PASS: TOU-detected 02:00-06:00 UTC window overrides the Economy 7 default")

    # ------------------------------------------------------------------
    # Test 4: a multi-block TOU schedule yields several night windows per day
    # ------------------------------------------------------------------
    print("\n*** Test 4: multi-block TOU schedule ***")
    api4 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou(api4, [(60, 300), (780, 960), (1200, 1320)])  # 01:00-05:00, 13:00-16:00, 20:00-22:00 UTC
    mdata4 = await _run(api4, base_url, eco7)
    slots4 = _night_slots_utc(mdata4, 13.04)

    starts = sorted({(s.hour, s.minute) for s, _ in slots4})
    expected_starts = [(1, 0), (13, 0), (20, 0)]
    if starts != expected_starts:
        print("ERROR: multi-block TOU windows not reproduced - expected {} got {}".format(expected_starts, starts))
        failed = True
    else:
        print("PASS: all three TOU blocks reproduced")

    # ------------------------------------------------------------------
    # Test 5: GO falls back to a hard-wired window anchored to LOCAL time
    # ------------------------------------------------------------------
    print("\n*** Test 5: GO fallback window is local, not UTC ***")
    api5 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00, now=_NOW_BST)
    _stub_tou_unavailable(api5)
    mdata5 = await _run(api5, base_url, "E-1R-GO-VAR-22-10-14-M")
    slots5 = _night_slots_local(mdata5, 7.00)

    if not slots5:
        print("ERROR: no night slots produced for the GO fallback")
        failed = True
    elif any((s.hour, s.minute) != (0, 30) or (e.hour, e.minute) != (5, 30) for s, e in slots5):
        print("ERROR: GO window is not 00:30-05:30 local - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots5[:3]]))
        failed = True
    else:
        utc_start = slots5[0][0].astimezone(timezone.utc)
        print("PASS: GO window is 00:30-05:30 local during BST ({} UTC)".format(utc_start.strftime("%H:%M")))

    # ------------------------------------------------------------------
    # Test 6: IOG keeps its local wall-clock window through BST too
    # ------------------------------------------------------------------
    print("\n*** Test 6: IOG fallback window is local, not UTC ***")
    api6 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00, now=_NOW_BST)
    _stub_tou_unavailable(api6)
    mdata6 = await _run(api6, base_url, "E-1R-IOG-SMB-TOU-25-12-12-H")
    slots6 = _night_slots_local(mdata6, 7.00)

    if not slots6 or any((s.hour, s.minute) != (23, 30) or (e.hour, e.minute) != (5, 30) for s, e in slots6):
        print("ERROR: IOG window is not 23:30-05:30 local - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots6[:3]]))
        failed = True
    else:
        print("PASS: IOG window is 23:30-05:30 local during BST")

    # ------------------------------------------------------------------
    # Test 7: Economy 7 stays UTC-anchored while GO stays local, same clock
    # ------------------------------------------------------------------
    print("\n*** Test 7: E7 is UTC and GO is local at the same moment ***")
    api7 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou_unavailable(api7)
    e7_local = _night_slots_local(await _run(api7, base_url, eco7), 13.04)
    api8 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou_unavailable(api8)
    go_local = _night_slots_local(await _run(api8, base_url, "E-1R-GO-VAR-22-10-14-M"), 13.04)

    if not e7_local or not go_local:
        print("ERROR: missing slots when comparing E7 and GO anchoring")
        failed = True
    elif (e7_local[0][0].hour, e7_local[0][0].minute) != (1, 30):
        print("ERROR: E7 should start 01:30 local in BST - got {}".format(e7_local[0][0].strftime("%H:%M")))
        failed = True
    elif (go_local[0][0].hour, go_local[0][0].minute) != (0, 30):
        print("ERROR: GO should start 00:30 local in BST - got {}".format(go_local[0][0].strftime("%H:%M")))
        failed = True
    else:
        print("PASS: E7 shifts to 01:30 local while GO stays at 00:30 local")

    # ------------------------------------------------------------------
    # Test 8: octopus_night_times sets the windows by hand, in local time
    # ------------------------------------------------------------------
    print("\n*** Test 8: octopus_night_times configures windows manually ***")
    api9 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou_unavailable(api9)
    my_predbat.args["octopus_night_times"] = [
        {"start": "01:00:00", "end": "05:00:00"},
        {"start": "13:00:00", "end": "16:00:00"},
        {"start": "20:00:00", "end": "22:00:00"},
    ]
    try:
        slots9 = _night_slots_local(await _run(api9, base_url, eco7), 13.04)
    finally:
        del my_predbat.args["octopus_night_times"]

    starts9 = sorted({(s.hour, s.minute) for s, _ in slots9})
    if starts9 != [(1, 0), (13, 0), (20, 0)]:
        print("ERROR: manual windows not applied - expected 01:00/13:00/20:00 local, got {}".format(starts9))
        failed = True
    elif sorted({(e.hour, e.minute) for _, e in slots9}) != [(5, 0), (16, 0), (22, 0)]:
        print("ERROR: manual window ends wrong - got {}".format(sorted({(e.hour, e.minute) for _, e in slots9})))
        failed = True
    else:
        print("PASS: three manually configured off-peak bands applied in local time")

    # ------------------------------------------------------------------
    # Test 9: a manually configured window given in UTC
    # ------------------------------------------------------------------
    print("\n*** Test 9: octopus_night_times with utc: true ***")
    api10 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou_unavailable(api10)
    my_predbat.args["octopus_night_times"] = [{"start": "02:30:00", "end": "06:30:00", "utc": True}]
    try:
        slots10 = _night_slots_utc(await _run(api10, base_url, eco7), 13.04)
    finally:
        del my_predbat.args["octopus_night_times"]

    if not slots10 or any((s.hour, s.minute) != (2, 30) or (e.hour, e.minute) != (6, 30) for s, e in slots10):
        print("ERROR: utc-flagged manual window is not 02:30-06:30 UTC - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots10[:3]]))
        failed = True
    else:
        print("PASS: utc-flagged manual window is 02:30-06:30 UTC (03:30 local in BST)")

    # ------------------------------------------------------------------
    # Test 10: manual configuration wins over the meter's TOU labels
    # ------------------------------------------------------------------
    print("\n*** Test 10: manual configuration overrides TOU labels ***")
    api11 = _make_api(my_predbat, day_rate=29.26, night_rate=13.04, now=_NOW_BST)
    _stub_tou(api11, [(120, 360)])  # the meter reports 02:00-06:00 UTC
    my_predbat.args["octopus_night_times"] = [{"start": "01:00:00", "end": "05:00:00"}]
    try:
        slots11 = _night_slots_local(await _run(api11, base_url, eco7), 13.04)
    finally:
        del my_predbat.args["octopus_night_times"]

    if not slots11 or any((s.hour, s.minute) != (1, 0) for s, _ in slots11):
        print("ERROR: manual window did not override the TOU labels - got {}".format([(s.strftime("%H:%M")) for s, _ in slots11[:3]]))
        failed = True
    else:
        print("PASS: manual configuration takes precedence over the meter's TOU labels")

    # ------------------------------------------------------------------
    # Test 11: hour 24 normalises to hour 0, as time_string_to_stamp does
    # ------------------------------------------------------------------
    print("\n*** Test 11: 24:xx is normalised to 00:xx ***")
    if _night_time_to_minutes("24:00:00") != 0 or _night_time_to_minutes("24:30:00") != 30:
        print("ERROR: 24:00 should parse as 0 and 24:30 as 30, got {} and {}".format(_night_time_to_minutes("24:00:00"), _night_time_to_minutes("24:30:00")))
        failed = True
    elif _windows_from_night_times([{"start": "24:30:00", "end": "05:00:00"}]) != [(30, 300, False)]:
        print("ERROR: a 24:30 start should behave as 00:30, got {}".format(_windows_from_night_times([{"start": "24:30:00", "end": "05:00:00"}])))
        failed = True
    elif _windows_from_night_times([{"start": "20:00:00", "end": "24:00:00"}]) != [(1200, 1440, False)]:
        print("ERROR: a 24:00 end should still close the day at 1440, got {}".format(_windows_from_night_times([{"start": "20:00:00", "end": "24:00:00"}])))
        failed = True
    else:
        print("PASS: hour 24 normalised to hour 0, and a 24:00 end still closes the day")

    # ------------------------------------------------------------------
    # Test 12: an IOG meter's TOU labels must not become a fixed schedule
    #
    # On Intelligent Octopus Go the off-peak buckets cover the guaranteed overnight window plus
    # whichever ad-hoc dispatch slots Octopus granted that night. Those dispatch slots are decided
    # night by night, so projecting them forward as a recurring window invents a cheap slot the
    # customer has not been given. These are one real customer's labels: 21:00-21:30 UTC is a bonus
    # dispatch that landed on most of the sampled nights, 22:30-04:30 UTC is the genuine window.
    # ------------------------------------------------------------------
    print("\n*** Test 12: IOG dispatch labels do not become a recurring window ***")
    api12 = _make_api(my_predbat, day_rate=30.3, night_rate=6.9, now=_NOW_BST)
    _stub_tou(api12, [(1260, 1290), (1350, 1440), (0, 270)])  # 22:00-22:30 and 23:30-05:30 BST
    slots12 = _night_slots_local(await _run(api12, base_url, "E-1R-IOG-SMB-VAR-24-10-29-A"), 6.9)

    phantom = [(s, e) for s, e in slots12 if (s.hour, s.minute) == (22, 0)]
    if not slots12:
        print("ERROR: no night slots produced for the IOG meter")
        failed = True
    elif phantom:
        print("ERROR: a 22:00 dispatch slot became a recurring off-peak window - {} occurrences".format(len(phantom)))
        failed = True
    elif any((s.hour, s.minute) != (23, 30) or (e.hour, e.minute) != (5, 30) for s, e in slots12):
        print("ERROR: IOG window is not 23:30-05:30 local - got {}".format([(s.strftime("%H:%M"), e.strftime("%H:%M")) for s, e in slots12[:3]]))
        failed = True
    else:
        print("PASS: IOG keeps its guaranteed 23:30-05:30 local window and ignores the dispatch label")

    # ------------------------------------------------------------------
    # Test 13: a TOU-derived window on a local wall-clock tariff survives DST
    #
    # The measurement labels are UTC instants, so a GO meter sampled during BST reports its
    # 00:30-05:30 local window as 23:30-04:30 UTC. Holding that UTC offset fixed runs the window an
    # hour early once the clocks go back, which the 9-day schedule spans when the change is near.
    # ------------------------------------------------------------------
    print("\n*** Test 13: TOU window on a local-anchored tariff holds across DST ***")
    api13 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00, now=LONDON.localize(datetime(2026, 10, 22, 12, 0, 0)))
    _stub_tou(api13, [(1410, 1440), (0, 270)])  # 23:30-04:30 UTC, which is 00:30-05:30 BST
    slots13 = _night_slots_local(await _run(api13, base_url, "E-1R-GO-VAR-22-10-14-M"), 7.00)

    # The changeover day is included: the window opens at 00:30 BST and closes at 05:30 GMT.
    nights13 = [(s.month, s.day) for s, _ in slots13]
    drifted = [(s, e) for s, e in slots13 if (s.hour, s.minute) != (0, 30) or (e.hour, e.minute) != (5, 30)]
    if not slots13:
        print("ERROR: no night slots produced for the GO meter across the DST change")
        failed = True
    elif len(nights13) != len(set(nights13)) or (10, 25) not in nights13 or (10, 26) not in nights13:
        print("ERROR: expected one night per date including 25 and 26 Oct, got {}".format(nights13))
        failed = True
    elif drifted:
        print("ERROR: TOU window drifted off local time across the DST change - {} of {} slots wrong, e.g. {}".format(len(drifted), len(slots13), [(s.strftime("%d %H:%M"), e.strftime("%H:%M")) for s, e in drifted[:3]]))
        failed = True
    else:
        print("PASS: TOU-derived window stays 00:30-05:30 local on both sides of the DST change")

    # ------------------------------------------------------------------
    # Test 14: the hard-wired GO window across both clock changes
    #
    # Adding days to a localised midnight keeps the starting UTC offset, so across the autumn change
    # the last day of the schedule landed on the day before and the 8-day schedule lost an entry.
    # ------------------------------------------------------------------
    for label14, now14, changeover14 in (
        ("autumn", LONDON.localize(datetime(2026, 10, 20, 9, 0, 0)), (10, 25)),
        ("spring", LONDON.localize(datetime(2026, 3, 25, 9, 0, 0)), (3, 29)),
    ):
        print("\n*** Test 14: hard-wired GO window across the {} clock change ***".format(label14))
        api14 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00, now=now14)
        _stub_tou_unavailable(api14)
        mdata14 = await _run(api14, base_url, "E-1R-GO-VAR-22-10-14-M")
        slots14 = _night_slots_local(mdata14, 7.00)
        nights14 = [(s.month, s.day) for s, _ in slots14]
        drifted14 = [(s, e) for s, e in slots14 if (s.hour, s.minute) != (0, 30) or (e.hour, e.minute) != (5, 30)]
        gaps14 = [index for index in range(len(mdata14) - 1) if datetime.strptime(mdata14[index]["valid_to"], DATE_TIME_STR_FORMAT) != datetime.strptime(mdata14[index + 1]["valid_from"], DATE_TIME_STR_FORMAT)]
        if len(mdata14) != 16:
            print("ERROR: {}: expected 16 entries (8 days), got {} - nights {}".format(label14, len(mdata14), nights14))
            failed = True
        elif len(nights14) != len(set(nights14)) or changeover14 not in nights14:
            print("ERROR: {}: expected one night per date including the changeover, got {}".format(label14, nights14))
            failed = True
        elif drifted14:
            print("ERROR: {}: night window drifted off 00:30-05:30 local, e.g. {}".format(label14, [(s.strftime("%d %H:%M"), e.strftime("%H:%M")) for s, e in drifted14[:3]]))
            failed = True
        elif gaps14:
            print("ERROR: {}: schedule is not contiguous at entries {}".format(label14, gaps14))
            failed = True
        else:
            print("PASS: {}: 8 nights at 00:30-05:30 local, contiguous, changeover night included".format(label14))

    # ------------------------------------------------------------------
    # Test 15: windows that cross midnight or sit in the changeover hour itself
    #
    # IOG's 23:30-05:30 spans the change. A manual window starting in the repeated autumn hour takes
    # its first occurrence, and one starting in the skipped spring hour opens when the clocks jump -
    # in neither case may an interval end before it starts or the schedule leave a gap.
    # ------------------------------------------------------------------
    cases15 = [
        ("IOG autumn", LONDON.localize(datetime(2026, 10, 20, 9, 0, 0)), "E-1R-IOG-SMB-TOU-25-12-12-H", None, datetime(2026, 10, 24, 22, 30, tzinfo=timezone.utc), datetime(2026, 10, 25, 5, 30, tzinfo=timezone.utc)),
        ("IOG spring", LONDON.localize(datetime(2026, 3, 25, 9, 0, 0)), "E-1R-IOG-SMB-TOU-25-12-12-H", None, datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc), datetime(2026, 3, 29, 4, 30, tzinfo=timezone.utc)),
        ("manual 01:30 autumn", LONDON.localize(datetime(2026, 10, 20, 9, 0, 0)), eco7, [{"start": "01:30:00", "end": "05:30:00"}], datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc), datetime(2026, 10, 25, 5, 30, tzinfo=timezone.utc)),
        ("manual 01:30 spring", LONDON.localize(datetime(2026, 3, 25, 9, 0, 0)), eco7, [{"start": "01:30:00", "end": "02:15:00"}], datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc), datetime(2026, 3, 29, 1, 15, tzinfo=timezone.utc)),
    ]
    for label15, now15, tariff15, manual15, want_start15, want_end15 in cases15:
        print("\n*** Test 15: {} ***".format(label15))
        api15 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00, now=now15)
        _stub_tou_unavailable(api15)
        with _night_times(my_predbat, manual15):
            mdata15 = await _run(api15, base_url, tariff15)
        entries15 = [(datetime.strptime(entry["valid_from"], DATE_TIME_STR_FORMAT), datetime.strptime(entry["valid_to"], DATE_TIME_STR_FORMAT)) for entry in mdata15]
        inverted15 = [(s, e) for s, e in entries15 if e <= s]
        gaps15 = [index for index in range(len(entries15) - 1) if entries15[index][1] != entries15[index + 1][0]]
        if len(entries15) != 16 or inverted15 or gaps15:
            print("ERROR: {}: expected 16 contiguous forward entries, got {} with inverted {} and gaps at {}".format(label15, len(entries15), inverted15[:2], gaps15))
            failed = True
        elif (want_start15, want_end15) not in _night_slots_utc(mdata15, 7.00):
            print("ERROR: {}: changeover night {} - {} UTC missing, got {}".format(label15, want_start15, want_end15, [(s.strftime("%d %H:%M"), e.strftime("%d %H:%M")) for s, e in _night_slots_utc(mdata15, 7.00)]))
            failed = True
        else:
            print("PASS: {}: changeover night runs {} - {} UTC, schedule contiguous".format(label15, want_start15.strftime("%d %H:%M"), want_end15.strftime("%d %H:%M")))

    # ------------------------------------------------------------------
    # Test 16: _localise_wall_clock on its own, at both changes and for a non-pytz zone
    # ------------------------------------------------------------------
    print("\n*** Test 16: _localise_wall_clock resolves repeated and skipped wall-clock times ***")
    cases16 = [
        ("before the autumn change", LONDON, datetime(2026, 10, 25, 0, 59), datetime(2026, 10, 24, 23, 59, tzinfo=timezone.utc)),
        ("repeated autumn hour takes the first occurrence", LONDON, datetime(2026, 10, 25, 1, 30), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)),
        ("after the autumn change", LONDON, datetime(2026, 10, 25, 2, 0), datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)),
        ("before the spring change", LONDON, datetime(2026, 3, 29, 0, 59), datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)),
        ("skipped spring hour moves to the jump", LONDON, datetime(2026, 3, 29, 1, 0), datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)),
        ("late in the skipped spring hour moves to the jump", LONDON, datetime(2026, 3, 29, 1, 59, 30), datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)),
        ("after the spring change", LONDON, datetime(2026, 3, 29, 2, 15), datetime(2026, 3, 29, 1, 15, tzinfo=timezone.utc)),
        ("non-pytz zone is attached as it stands", timezone.utc, datetime(2026, 10, 25, 1, 30), datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)),
    ]
    for label16, tz16, wall16, want16 in cases16:
        got16 = _localise_wall_clock(tz16, wall16)
        if got16 != want16:
            print("ERROR: {}: {} localised to {}, expected {}".format(label16, wall16, got16, want16))
            failed = True
        else:
            print("PASS: {}: {} -> {} UTC".format(label16, wall16.strftime("%d %b %H:%M:%S"), got16.astimezone(timezone.utc).strftime("%H:%M")))

    # A manual window lying wholly in the skipped hour is dropped that night rather than emitted
    # with zero length, and the day rate runs on across it.
    api16 = _make_api(my_predbat, day_rate=29.14, night_rate=7.00, now=LONDON.localize(datetime(2026, 3, 25, 9, 0, 0)))
    _stub_tou_unavailable(api16)
    with _night_times(my_predbat, [{"start": "01:00:00", "end": "01:30:00"}]):
        mdata16 = await _run(api16, base_url, eco7)
    entries16 = [(datetime.strptime(entry["valid_from"], DATE_TIME_STR_FORMAT), datetime.strptime(entry["valid_to"], DATE_TIME_STR_FORMAT)) for entry in mdata16]
    nights16 = [s.astimezone(LONDON).day for s, _ in _night_slots_utc(mdata16, 7.00)]
    if any(e <= s for s, e in entries16) or any(entries16[index][1] != entries16[index + 1][0] for index in range(len(entries16) - 1)):
        print("ERROR: skipped-hour window left an empty or broken schedule: {}".format([(s.strftime("%d %H:%M%z"), e.strftime("%d %H:%M%z")) for s, e in entries16]))
        failed = True
    elif 29 in nights16 or 28 not in nights16 or 30 not in nights16:
        print("ERROR: skipped-hour window should be dropped on 29 Mar only, got nights on {}".format(nights16))
        failed = True
    else:
        print("PASS: a window wholly inside the skipped hour is dropped on 29 Mar, schedule contiguous")

    return failed

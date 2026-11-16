# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for SolarAPI.pv_calibration_update_evidence: the evidence store, the scan schedule and the save guards."""

from datetime import timedelta

from pv_calibration import pv_day
from tests.test_infra import run_async
from tests.test_solcast import MockComponents, create_test_solar_api


class FakeStorage:
    """An async storage backend that records saves and serves a scripted sequence of load results."""

    def __init__(self, loads=None):
        """loads: values returned by successive load calls (the last repeats); an Exception instance is raised."""
        self.loads = list(loads) if loads else [None]
        self.saves = []
        self.load_calls = 0

    async def load(self, namespace, key):
        """Return the next scripted load result."""
        self.load_calls += 1
        value = self.loads[min(self.load_calls - 1, len(self.loads) - 1)]
        if isinstance(value, Exception):
            raise value
        return value

    async def save(self, namespace, key, data, format="json", expiry=None):
        """Record the save and report success."""
        self.saves.append({"namespace": namespace, "key": key, "data": data, "expiry": expiry})
        return True


def day(test_api, days_ago):
    """The evidence key for the day days_ago before the test's now."""
    return pv_day(test_api.mock_base.now_utc, days_ago * 24 * 60, test_api.mock_base.local_tz)


def record(ratio=0.9):
    """A one-slot evidence day."""
    return {"slots": {720: [1.0, ratio]}, "peak_kw": 2.0, "clipped_minutes": 0}


def setup(storage, scans):
    """A solar API with the fake storage and a mocked scan that returns successive entries of scans (the last repeats) and records its calls."""
    test_api = create_test_solar_api()
    if storage is not None:
        test_api.mock_base.components = MockComponents(storage)
    else:
        test_api.mock_base.components = None
    calls = []

    def mock_scan(days, tracked=True):
        """Record the call and return the next scripted scan."""
        calls.append({"days": days, "tracked": tracked})
        value = scans[min(len(calls) - 1, len(scans) - 1)]
        return value(test_api) if callable(value) else value

    test_api.solar.pv_calibration_scan = mock_scan
    return test_api, calls


def advance(test_api, minutes):
    """Move the test clock forward."""
    base = test_api.mock_base
    base.now_utc = base.now_utc + timedelta(minutes=minutes)
    base.now_utc_exact = base.now_utc_exact + timedelta(minutes=minutes)


def update(test_api):
    """Run one evidence update."""
    with test_api.patch_now_utc_exact():
        run_async(test_api.solar.pv_calibration_update_evidence())


def check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def test_update_first_scan_reads_history_days():
    """An empty store: the first scan reads the history days without tracking, and the result is saved with a 60-day expiry."""
    storage = FakeStorage([None])
    test_api, calls = setup(storage, [lambda t: {day(t, 1): record()}])
    failed = False
    try:
        for _ in range(3):  # an empty store is only trusted after 3 failed loads spanning 10 minutes
            update(test_api)
            advance(test_api, 6)
        failed |= check("first scan", calls and calls[0] == {"days": 28, "tracked": False}, calls)
        failed |= check("saved", len(storage.saves) >= 1, storage.saves)
        if storage.saves:
            saved = storage.saves[-1]
            failed |= check("namespace/key", saved["namespace"] == "solcast" and saved["key"] == "pv_calibration_evidence", saved)
            failed |= check("slot minutes", saved["data"]["slot_minutes"] == test_api.solar.plan_interval_minutes, saved["data"])
            failed |= check("expiry", abs((saved["expiry"] - test_api.mock_base.now_utc) - timedelta(days=60)) < timedelta(hours=1), saved["expiry"])
            failed |= check("days saved", list(saved["data"]["days"]) == [day(test_api, 1)], saved["data"]["days"].keys())
    finally:
        test_api.cleanup()
    return failed


def test_update_daily_scan_reads_two():
    """After a successful scan the next is a day later and reads 2 days; an hour later there is none."""
    test_api, calls = setup(FakeStorage([{"slot_minutes": 5, "days": {}}]), [lambda t: {day(t, 1): record()}])
    failed = False
    try:
        update(test_api)
        advance(test_api, 60)
        update(test_api)
        failed |= check("no scan within the day", len(calls) == 1, calls)
        advance(test_api, 23 * 60)
        update(test_api)
        failed |= check("daily scan", len(calls) == 2 and calls[1]["days"] == 2, calls)
    finally:
        test_api.cleanup()
    return failed


def test_update_failed_scan_retries_after_an_hour():
    """A failed history read leaves the evidence alone and is retried after 60 minutes, not before."""
    stored = {"slot_minutes": 5, "days": {}}
    test_api, calls = setup(FakeStorage([stored]), [None, None, lambda t: {day(t, 1): record()}])
    failed = False
    try:
        update(test_api)
        failed |= check("status", "retry" in test_api.solar.pv_calibration_scan_status, test_api.solar.pv_calibration_scan_status)
        failed |= check("next scan", test_api.solar.pv_calibration_next_scan == test_api.mock_base.now_utc + timedelta(minutes=60), test_api.solar.pv_calibration_next_scan)
        advance(test_api, 59)
        update(test_api)
        failed |= check("no retry at 59", len(calls) == 1, calls)
        advance(test_api, 1)
        update(test_api)
        failed |= check("retry at 60", len(calls) == 2, calls)
        failed |= check("evidence unchanged", test_api.solar.pv_calibration_evidence == {}, test_api.solar.pv_calibration_evidence)
    finally:
        test_api.cleanup()
    return failed


def test_update_save_waits_for_load():
    """A store that reads back nothing is not overwritten until 3 failed loads spanning 10 minutes."""
    storage = FakeStorage([None])
    test_api, calls = setup(storage, [lambda t: {day(t, 1): record()}])
    failed = False
    try:
        update(test_api)
        advance(test_api, 5)
        update(test_api)
        failed |= check("no save after 2 failures", storage.saves == [], storage.saves)
        advance(test_api, 5)
        update(test_api)
        failed |= check("save after 3 failures over 10 minutes", len(storage.saves) == 1, storage.saves)
    finally:
        test_api.cleanup()
    return failed


def test_update_merges_with_stored():
    """Stored days and newly scanned ones are merged."""
    base_test = create_test_solar_api()
    stored_days = {day(base_test, k): record() for k in range(3, 11)}
    base_test.cleanup()
    test_api, calls = setup(FakeStorage([{"slot_minutes": 5, "days": stored_days}]), [lambda t: {day(t, 1): record(), day(t, 2): record()}])
    try:
        update(test_api)
        evidence = test_api.solar.pv_calibration_evidence
        failed = check("merged", sorted(evidence) == sorted([day(test_api, k) for k in range(1, 11)]), sorted(evidence or {}))
    finally:
        test_api.cleanup()
    return failed


def test_update_discards_other_slot_minutes():
    """A store saved at another slot length is ignored, and the next save writes the current one."""
    base_test = create_test_solar_api()
    stored_days = {day(base_test, 3): record()}
    base_test.cleanup()
    storage = FakeStorage([{"slot_minutes": 15, "days": stored_days}])
    test_api, calls = setup(storage, [lambda t: {day(t, 1): record()}])
    try:
        update(test_api)
        failed = check("discarded", sorted(test_api.solar.pv_calibration_evidence) == [day(test_api, 1)], test_api.solar.pv_calibration_evidence)
        failed |= check("saved current slot", storage.saves and storage.saves[-1]["data"]["slot_minutes"] == test_api.solar.plan_interval_minutes, storage.saves)
    finally:
        test_api.cleanup()
    return failed


def test_update_resaves_daily():
    """Unchanged evidence is saved again a day after the last save, so its expiry keeps moving."""
    base_test = create_test_solar_api()
    stored_days = {day(base_test, 1): record()}
    base_test.cleanup()
    storage = FakeStorage([{"slot_minutes": 5, "days": stored_days}])
    test_api, calls = setup(storage, [{}])
    try:
        update(test_api)
        saves = len(storage.saves)
        advance(test_api, 60)
        update(test_api)
        failed = check("no resave within the day", len(storage.saves) == saves, storage.saves)
        advance(test_api, 24 * 60)
        update(test_api)
        failed |= check("resave after a day", len(storage.saves) == saves + 1, storage.saves)
    finally:
        test_api.cleanup()
    return failed


def test_update_without_storage():
    """With no storage the evidence is kept in memory and the scan schedule still applies."""
    test_api, calls = setup(None, [lambda t: {day(t, 1): record()}])
    try:
        update(test_api)
        failed = check("in memory", sorted(test_api.solar.pv_calibration_evidence or {}) == [day(test_api, 1)], test_api.solar.pv_calibration_evidence)
        advance(test_api, 60)
        update(test_api)
        failed |= check("schedule", len(calls) == 1, calls)
    finally:
        test_api.cleanup()
    return failed


def test_update_scan_exception_is_a_failed_scan():
    """A scan that raises is logged and retried like a failed read, and never breaks the forecast fetch."""
    test_api, calls = setup(FakeStorage([{"slot_minutes": 5, "days": {}}]), [None])

    def broken_scan(days, tracked=True):
        """Raise as a bad history read might."""
        calls.append({"days": days})
        raise ValueError("bad history")

    test_api.solar.pv_calibration_scan = broken_scan
    try:
        update(test_api)
        failed = check("retry scheduled", test_api.solar.pv_calibration_next_scan == test_api.mock_base.now_utc + timedelta(minutes=60), test_api.solar.pv_calibration_next_scan)
    finally:
        test_api.cleanup()
    return failed


def test_update_scans_after_midnight_for_yesterday():
    """A scan at 21:00 is followed by one just after local midnight, so yesterday is in the store that morning; no more until the next day."""
    from datetime import datetime, timezone

    test_api, calls = setup(FakeStorage([{"slot_minutes": 5, "days": {}}]), [lambda t: {day(t, 1): record()}])
    try:
        test_api.mock_base.now_utc = datetime(2025, 6, 15, 20, 0, tzinfo=timezone.utc)  # 21:00 BST
        test_api.mock_base.now_utc_exact = test_api.mock_base.now_utc
        update(test_api)
        advance(test_api, 4 * 60)  # 01:00 BST the next day
        update(test_api)
        failed = check("scan after midnight", len(calls) == 2, calls)
        failed |= check("yesterday stored", day(test_api, 1) in (test_api.solar.pv_calibration_evidence or {}), sorted(test_api.solar.pv_calibration_evidence or {}))
        advance(test_api, 60)
        update(test_api)
        failed |= check("no further scan", len(calls) == 2, calls)
    finally:
        test_api.cleanup()
    return failed


def test_update_restart_reads_two_days():
    """After a restart the stored last success is used, so the first scan reads the days since then, not the whole history."""
    storage = FakeStorage([None])
    test_api, calls = setup(storage, [lambda t: {day(t, 1): record()}])
    storage.loads = [{"slot_minutes": 5, "days": {}, "last_success": (test_api.mock_base.now_utc - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S%z")}]
    try:
        update(test_api)
        failed = check("restart scan length", calls and calls[0]["days"] == 2, calls)
    finally:
        test_api.cleanup()
    return failed


def test_update_rescans_when_clock_moves_back():
    """A next scan more than a day ahead (the host clock moved back - NTP, a VM restore; not daylight saving) is not waited for."""
    test_api, calls = setup(FakeStorage([{"slot_minutes": 5, "days": {}}]), [lambda t: {day(t, 1): record()}])
    try:
        update(test_api)
        test_api.solar.pv_calibration_next_scan = test_api.mock_base.now_utc + timedelta(days=3)
        advance(test_api, 60)
        update(test_api)
        failed = check("rescan after clock moved back", len(calls) == 2, calls)
    finally:
        test_api.cleanup()
    return failed


def test_update_discarded_store_rebuilds_full_history():
    """A store saved at another slot length is discarded with its last success too, so the rebuild reads the whole history."""
    storage = FakeStorage([None])
    test_api, calls = setup(storage, [lambda t: {day(t, 1): record()}])
    storage.loads = [{"slot_minutes": 15, "days": {}, "last_success": (test_api.mock_base.now_utc - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S%z")}]
    try:
        update(test_api)
        failed = check("rebuild reads the whole history", calls and calls[0]["days"] == 28, calls)
    finally:
        test_api.cleanup()
    return failed


def run_pv_calibration_update_tests(my_predbat=None):
    """Run the PV calibration evidence store tests."""
    failed = False
    for test in (
        test_update_first_scan_reads_history_days,
        test_update_daily_scan_reads_two,
        test_update_failed_scan_retries_after_an_hour,
        test_update_save_waits_for_load,
        test_update_merges_with_stored,
        test_update_discards_other_slot_minutes,
        test_update_resaves_daily,
        test_update_without_storage,
        test_update_scan_exception_is_a_failed_scan,
        test_update_scans_after_midnight_for_yesterday,
        test_update_restart_reads_two_days,
        test_update_rescans_when_clock_moves_back,
        test_update_discarded_store_rebuilds_full_history,
    ):
        print("**** {} ****".format(test.__name__))
        failed = test() or failed
    return failed

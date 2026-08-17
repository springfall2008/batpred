# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk storage persistence
# -----------------------------------------------------------------------------

"""Tests for Sunsynk cache save, restore and per-tier age tracking."""

from sunsynk_const import SUNSYNK_CACHE_STATIC, SUNSYNK_CACHE_CONFIG, SUNSYNK_CACHE_RATINGS, SUNSYNK_CACHE_CONTROL, SUNSYNK_RESTORE_MAX_CONTROL, SUNSYNK_TTL_STATIC, SUNSYNK_TTL_LIVE
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


class FakeStorage:
    """In-memory stand-in for the Storage component, tracking each file's age."""

    def __init__(self, ages=None):
        """Start empty, with optional pre-set ages in minutes per cache name."""
        self.files = {}
        self.ages = ages or {}
        self.saves = []

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a save, mirroring the real Storage.save(module, filename, data, format, expiry) signature."""
        self.files[filename] = data
        self.saves.append(filename)
        return True

    async def load(self, module, filename):
        """Return previously saved data, or None when absent, mirroring Storage.load(module, filename).

        Deliberately has no ``default`` parameter: the real Storage.load() does not accept
        one either, and an earlier round of this task passed ``default=None`` at the
        sunsynk.py call site, which this fake's original, more permissive signature happily
        accepted while the real component would have raised a TypeError. Keeping this
        signature exact stops the fake from masking that class of bug again.
        """
        return self.files.get(filename)

    async def age(self, module, filename):
        """Return the configured age in minutes, or None when never written."""
        return self.ages.get(filename)


class RaisingStorage:
    """Storage double whose every method raises, simulating a failing backend."""

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Raise to simulate a save failure."""
        raise RuntimeError("simulated storage failure")

    async def load(self, module, filename):
        """Raise to simulate a load failure."""
        raise RuntimeError("simulated storage failure")

    async def age(self, module, filename):
        """Raise to simulate an age-lookup failure."""
        raise RuntimeError("simulated storage failure")


class StoredSunsynk(MockSunsynk):
    """MockSunsynk with a fake Storage attached."""

    def __init__(self, ages=None, **kwargs):
        """Attach the fake storage."""
        super().__init__(**kwargs)
        self._storage = FakeStorage(ages=ages)

    @property
    def storage(self):
        """Return the fake storage."""
        return self._storage


def test_each_tier_saves_to_its_own_file():
    """One file per tier, so storage.age() gives each an independent clock."""
    failed = False
    s = StoredSunsynk()
    s.device_list = ["INV1"]
    s.device_detail = {"INV1": {"ratePower": 8000}}
    s.device_settings = {"INV1": {"batteryLowCap": "10"}}
    s.device_rated_power = {"INV1": 8000.0}
    s.applied_payload = {"INV1": {"sysWorkMode": "1"}}
    run_async_local(s.save_static())
    run_async_local(s.save_config())
    run_async_local(s.save_ratings())
    run_async_local(s.save_control())
    for name in (SUNSYNK_CACHE_STATIC, SUNSYNK_CACHE_CONFIG, SUNSYNK_CACHE_RATINGS, SUNSYNK_CACHE_CONTROL):
        if name not in s.storage.files:
            print(f"ERROR: tier {name} was not saved")
            failed = True
    if s.storage.files.get(SUNSYNK_CACHE_STATIC, {}).get("device_list") != ["INV1"]:
        print(f"ERROR: static cache contents {s.storage.files.get(SUNSYNK_CACHE_STATIC)}")
        failed = True
    assert not failed, "test_each_tier_saves_to_its_own_file"


def test_restore_reinstates_static_and_config():
    """A restart restores discovery and settings without re-polling."""
    failed = False
    s = StoredSunsynk(ages={SUNSYNK_CACHE_STATIC: 5.0, SUNSYNK_CACHE_CONFIG: 3.0})
    s.storage.files[SUNSYNK_CACHE_STATIC] = {"device_list": ["INV1"], "device_detail": {"INV1": {"ratePower": 8000}}}
    s.storage.files[SUNSYNK_CACHE_CONFIG] = {"device_settings": {"INV1": {"batteryLowCap": "12"}}}
    run_async_local(s.restore_state())
    if s.device_list != ["INV1"]:
        print(f"ERROR: device_list not restored, got {s.device_list}")
        failed = True
    if s.battery_reserve_min("INV1") != 12:
        print(f"ERROR: settings not restored, floor is {s.battery_reserve_min('INV1')}")
        failed = True
    assert not failed, "test_restore_reinstates_static_and_config"


def test_control_cache_restore_is_time_bounded():
    """A stale applied-payload cache is discarded so the next write is forced.

    It is a change-detection cache with no read-back, so restoring it asserts the inverter
    still holds what Predbat last wrote. After a long outage that assertion is false, the
    next write would be wrongly skipped and the battery would silently diverge.
    """
    failed = False
    fresh = StoredSunsynk(ages={SUNSYNK_CACHE_CONTROL: SUNSYNK_RESTORE_MAX_CONTROL - 1})
    fresh.storage.files[SUNSYNK_CACHE_CONTROL] = {"applied_payload": {"INV1": {"sysWorkMode": "1"}}}
    run_async_local(fresh.restore_state())
    if fresh.applied_payload.get("INV1") != {"sysWorkMode": "1"}:
        print("ERROR: a fresh control cache should be restored")
        failed = True

    stale = StoredSunsynk(ages={SUNSYNK_CACHE_CONTROL: SUNSYNK_RESTORE_MAX_CONTROL + 1})
    stale.storage.files[SUNSYNK_CACHE_CONTROL] = {"applied_payload": {"INV1": {"sysWorkMode": "1"}}}
    run_async_local(stale.restore_state())
    if stale.applied_payload:
        print(f"ERROR: a stale control cache was restored: {stale.applied_payload}")
        failed = True
    assert not failed, "test_control_cache_restore_is_time_bounded"


def test_tier_expiry_uses_the_seeded_clock():
    """Tier clocks are seeded from storage age, so cadence survives a restart."""
    failed = False
    s = StoredSunsynk(ages={SUNSYNK_CACHE_STATIC: 10.0})
    s.storage.files[SUNSYNK_CACHE_STATIC] = {"device_list": ["INV1"]}
    run_async_local(s.restore_state())
    # 10 minutes old against an 8-hour TTL: not expired, so no re-poll on startup.
    if s.tier_expired("static", SUNSYNK_TTL_STATIC):
        print("ERROR: a 10-minute-old static tier should not be expired")
        failed = True
    # The live tier was never seeded, so it must be treated as expired.
    if not s.tier_expired("live", SUNSYNK_TTL_LIVE):
        print("ERROR: an unseeded tier must be expired so the first poll happens")
        failed = True
    s.mark_refreshed("live")
    if s.tier_expired("live", SUNSYNK_TTL_LIVE):
        print("ERROR: a just-refreshed tier should not be expired")
        failed = True
    assert not failed, "test_tier_expiry_uses_the_seeded_clock"


def test_telemetry_is_not_cached():
    """Live telemetry is never written to storage."""
    failed = False
    s = StoredSunsynk()
    s.device_list = ["INV1"]
    s.device_values = {"INV1": {"soc": 62}}
    s.device_energy = {"INV1": {"pv_today": 9.8}}
    run_async_local(s.save_static())
    run_async_local(s.save_ratings())
    blob = str(s.storage.files)
    if '"soc"' in blob or "'soc'" in blob:
        print("ERROR: telemetry leaked into a cache file")
        failed = True
    assert not failed, "test_telemetry_is_not_cached"


def test_restore_state_survives_a_raising_storage_and_retries_once_recovered():
    """A storage backend that raises on every call must not crash restore_state.

    Regression test: restore_state() used to call self.storage.age(...) directly, with no
    try/except, so a raising storage backend propagated an unhandled exception out of the
    method. Worse, _cache_restored was set True before that call, so component_base.py's
    outer loop survived the crash but restore_state() permanently did nothing on every
    later call - a single transient storage hiccup on first boot silently disabled cache
    restoration for the life of the process. This asserts both halves of the fix: no crash,
    and a later call (once storage recovers) still actually restores.
    """
    failed = False
    s = StoredSunsynk()
    s._storage = RaisingStorage()
    try:
        run_async_local(s.restore_state())
    except Exception as error:
        print(f"ERROR: restore_state raised with a failing storage backend: {error}")
        failed = True
    if s._cache_restored:
        print("ERROR: a failed restore attempt must not be marked complete, or a transient storage failure locks restoration out forever")
        failed = True
    if s.device_list:
        print(f"ERROR: nothing should have been restored from a raising storage, got {s.device_list}")
        failed = True
    # A REAL storage failure must still be visible in the log - only "no storage
    # configured at all" (self.storage is None) is meant to go quiet.
    if not any("Warn:" in message for message in s.log_messages):
        print(f"ERROR: a raising storage backend produced no warnings at all: {s.log_messages}")
        failed = True

    # Storage recovers (e.g. the next tick) - a later call on the SAME instance must still
    # be able to complete a real restore, not stay locked out by the earlier failure.
    s._storage = FakeStorage(ages={SUNSYNK_CACHE_STATIC: 1.0})
    s.storage.files[SUNSYNK_CACHE_STATIC] = {"device_list": ["INV1"]}
    run_async_local(s.restore_state())
    if s.device_list != ["INV1"]:
        print(f"ERROR: restore did not succeed once storage recovered, got {s.device_list}")
        failed = True
    if not s._cache_restored:
        print("ERROR: a successful restore should mark the guard so it does not run again")
        failed = True
    assert not failed, "test_restore_state_survives_a_raising_storage_and_retries_once_recovered"


def test_restore_state_completes_silently_and_permanently_when_storage_is_absent():
    """No storage component at all - the normal standalone-CLI case - must not crash restore_state,
    must not warn, and must complete once rather than being retried on every later call.

    mock_base.py documents components as always None for a standalone run, so
    ComponentBase.storage resolves to None in that mode; this is not a hypothetical edge
    case, and it is a permanent, by-design condition rather than a transient storage
    outage. Unlike a raising backend (see the raising-storage test above), it must not
    flag _restore_had_error - doing so would leave _cache_restored perpetually False and
    repeat the (now silent) no-op load/age calls on every retried first cycle forever,
    which is exactly the "redo its work every tick" failure mode Fix 1 exists to avoid.
    """
    failed = False
    s = StoredSunsynk()
    s._storage = None
    try:
        run_async_local(s.restore_state())
    except Exception as error:
        print(f"ERROR: restore_state raised with storage=None: {error}")
        failed = True
    if any("Warn:" in message for message in s.log_messages):
        print(f"ERROR: absent storage produced warnings: {s.log_messages}")
        failed = True
    if not s._cache_restored:
        print("ERROR: a completed restore attempt with no storage available should still be marked complete, not retried forever")
        failed = True
    if s.device_list:
        print(f"ERROR: nothing should have been restored with no storage, got {s.device_list}")
        failed = True

    # A second call must be a genuine no-op guarded by _cache_restored, not a repeat of the
    # same (silent) work - confirmed by the log staying exactly as it was.
    messages_before = list(s.log_messages)
    run_async_local(s.restore_state())
    if s.log_messages != messages_before:
        print(f"ERROR: a second call re-did work instead of being guarded, new messages: {s.log_messages[len(messages_before):]}")
        failed = True
    assert not failed, "test_restore_state_completes_silently_and_permanently_when_storage_is_absent"


def test_cache_helpers_are_silent_when_storage_is_none():
    """load_cache/save_cache/age_cache must not warn when there is simply no Storage component.

    Driven directly (rather than via restore_state) so each helper's return value is
    checked individually: {} from load_cache, None from age_cache, no exception from
    save_cache, and critically - no "Warn:" log line, since absent storage is the normal,
    by-design standalone-CLI condition, not a fault.
    """
    failed = False
    s = StoredSunsynk()
    s._storage = None
    loaded = run_async_local(s.load_cache(SUNSYNK_CACHE_CONFIG))
    try:
        run_async_local(s.save_cache(SUNSYNK_CACHE_CONFIG, {"a": 1}))
    except Exception as error:
        print(f"ERROR: save_cache raised with storage=None: {error}")
        failed = True
    age = run_async_local(s.age_cache(SUNSYNK_CACHE_CONFIG))
    if loaded != {}:
        print(f"ERROR: load_cache with no storage should return {{}}, got {loaded!r}")
        failed = True
    if age is not None:
        print(f"ERROR: age_cache with no storage should return None, got {age!r}")
        failed = True
    if any("Warn:" in message for message in s.log_messages):
        print(f"ERROR: absent storage produced warnings: {s.log_messages}")
        failed = True
    assert not failed, "test_cache_helpers_are_silent_when_storage_is_none"


def test_cache_helpers_still_warn_on_a_real_storage_failure():
    """A storage backend that actually raises must still warn - only 'no storage at all' goes quiet.

    Regression guard for Fix 1: the broad except Exception in load_cache/save_cache/
    age_cache is load-bearing for a transient real storage outage and must not be
    silenced along with the "storage is None" case.
    """
    failed = False
    s = StoredSunsynk()
    s._storage = RaisingStorage()
    run_async_local(s.load_cache(SUNSYNK_CACHE_CONFIG))
    run_async_local(s.save_cache(SUNSYNK_CACHE_CONFIG, {"a": 1}))
    run_async_local(s.age_cache(SUNSYNK_CACHE_CONFIG))
    warn_count = sum(1 for message in s.log_messages if "Warn:" in message)
    if warn_count < 3:
        print(f"ERROR: expected a warning from each of load/save/age against a raising storage, got {warn_count}: {s.log_messages}")
        failed = True
    assert not failed, "test_cache_helpers_still_warn_on_a_real_storage_failure"


def run_sunsynk_storage_tests(my_predbat):
    """Run all Sunsynk storage tests."""
    failed = False
    for name, fn in [
        ("tier_files", test_each_tier_saves_to_its_own_file),
        ("restore_static_config", test_restore_reinstates_static_and_config),
        ("control_restore_bounded", test_control_cache_restore_is_time_bounded),
        ("tier_expiry", test_tier_expiry_uses_the_seeded_clock),
        ("telemetry_not_cached", test_telemetry_is_not_cached),
        ("restore_survives_raising_storage", test_restore_state_survives_a_raising_storage_and_retries_once_recovered),
        ("restore_storage_none_completes_silently", test_restore_state_completes_silently_and_permanently_when_storage_is_absent),
        ("cache_helpers_silent_storage_none", test_cache_helpers_are_silent_when_storage_is_none),
        ("cache_helpers_warn_on_real_failure", test_cache_helpers_still_warn_on_a_real_storage_failure),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_storage.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_storage.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

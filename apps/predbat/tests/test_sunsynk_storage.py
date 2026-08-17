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

    async def save(self, module, name, data):
        """Record a save."""
        self.files[name] = data
        self.saves.append(name)
        return True

    async def load(self, module, name, default=None):
        """Return previously saved data, or the default."""
        return self.files.get(name, default)

    async def age(self, module, name):
        """Return the configured age in minutes, or None when never written."""
        return self.ages.get(name)


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
    s.device_capacity = {"INV1": 14.3}
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


def run_sunsynk_storage_tests(my_predbat):
    """Run all Sunsynk storage tests."""
    failed = False
    for name, fn in [
        ("tier_files", test_each_tier_saves_to_its_own_file),
        ("restore_static_config", test_restore_reinstates_static_and_config),
        ("control_restore_bounded", test_control_cache_restore_is_time_bounded),
        ("tier_expiry", test_tier_expiry_uses_the_seeded_clock),
        ("telemetry_not_cached", test_telemetry_is_not_cached),
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

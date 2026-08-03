# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE storage persistence and tiered refresh
# -----------------------------------------------------------------------------

"""Tests for DEYE cache persistence and the TTL-driven refresh tiers (``deye.py``)."""

import time
from unittest.mock import patch
from tests.test_infra import run_async
from tests.test_deye_api import MockDeye, LIVE_DATA_LIST
from deye_const import DEYE_TTL_STATIC, DEYE_TTL_CONFIG, DEYE_TTL_LIVE, DEYE_RESTORE_MAX_CONTROL, DEYE_CACHE_STATIC, DEYE_CACHE_CONFIG, DEYE_CACHE_RATINGS, DEYE_CACHE_CONTROL


class FakeStorage:
    """In-memory stand-in for the Storage component, with controllable ages."""

    def __init__(self, data=None, ages=None):
        """Seed the fake with pre-existing cache entries and their ages in minutes."""
        self.data = dict(data or {})
        self.ages = dict(ages or {})
        self.saves = []
        self.fail_on = set()

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a save and store the value, resetting that entry's age to zero."""
        if filename in self.fail_on:
            raise IOError(f"simulated write failure for {filename}")
        self.saves.append(filename)
        self.data[filename] = data
        self.ages[filename] = 0.0
        return True

    async def load(self, module, filename):
        """Return a stored value, or None when absent."""
        if filename in self.fail_on:
            raise IOError(f"simulated read failure for {filename}")
        return self.data.get(filename)

    async def age(self, module, filename):
        """Return the stored age in minutes, or None when absent."""
        if filename not in self.data:
            return None
        return self.ages.get(filename, 0.0)


class StorageDeye(MockDeye):
    """MockDeye with an injectable storage component, mirroring tests/test_ge_cloud.py."""

    @property
    def storage(self):
        """Return the injected storage double, or None when not set."""
        return getattr(self, "_mock_storage", None)


def _warm_cache(**ages):
    """Build a FakeStorage holding a complete set of caches at the given ages."""
    data = {
        DEYE_CACHE_STATIC: {"station_ids": [61016286], "device_list": ["INV1"]},
        DEYE_CACHE_CONFIG: {"INV1": {"battCapacity": 1200, "battLowCapacity": 14, "maxChargeCurrent": 185}},
        DEYE_CACHE_RATINGS: {"capacity": {"INV1": 61.44}, "pack_voltage": {"INV1": 51.2}, "rated_power": {"INV1": 8000.0}},
        DEYE_CACHE_CONTROL: {"applied_payload": {"INV1": {"deviceSn": "INV1", "touAction": "on"}}, "pending_orders": {}, "order_poll_count": {}},
    }
    default_ages = {name: ages.get(name, 0.5) for name in data}
    return FakeStorage(data=data, ages=default_ages)


def test_tier_expiry_clock():
    """A tier is expired when never refreshed or older than its TTL, and fresh in between."""
    failed = False
    d = StorageDeye()
    if not d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: an unrefreshed tier must be expired")
        failed = True
    d.mark_refreshed("static")
    if d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: a just-refreshed tier must not be expired")
        failed = True
    # Backdate past the TTL
    d.mark_refreshed("static", age_minutes=DEYE_TTL_STATIC + 1)
    if not d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: a tier older than its TTL must be expired")
        failed = True
    # Backdating preserves the remaining life rather than granting a full TTL
    d.mark_refreshed("config", age_minutes=DEYE_TTL_CONFIG - 1)
    if d.tier_expired("config", DEYE_TTL_CONFIG):
        print("ERROR: a tier with life left must not be expired")
        failed = True
    d.mark_refreshed("config", age_minutes=DEYE_TTL_CONFIG - 1)
    with patch("deye.time.time", return_value=time.time() + 120):
        if not d.tier_expired("config", DEYE_TTL_CONFIG):
            print("ERROR: the remaining life must expire, not reset to a full TTL")
            failed = True
    assert not failed, "test_tier_expiry_clock"


def test_restore_seeds_clocks_and_state():
    """A warm cache restores every tier and seeds its clock, so nothing needs re-polling."""
    failed = False
    d = StorageDeye()
    d._mock_storage = _warm_cache()

    run_async(d.restore_state())

    if d.device_list != ["INV1"] or d.station_ids != [61016286]:
        print(f"ERROR: discovery not restored: {d.device_list} {d.station_ids}")
        failed = True
    if d.device_battery_config.get("INV1", {}).get("battCapacity") != 1200:
        print(f"ERROR: config not restored: {d.device_battery_config}")
        failed = True
    if d.device_capacity.get("INV1") != 61.44 or d.device_rated_power.get("INV1") != 8000.0:
        print(f"ERROR: ratings not restored: {d.device_capacity} {d.device_rated_power}")
        failed = True
    if d.applied_payload.get("INV1", {}).get("touAction") != "on":
        print(f"ERROR: applied_payload not restored: {d.applied_payload}")
        failed = True
    for tier, ttl in (("static", DEYE_TTL_STATIC), ("config", DEYE_TTL_CONFIG)):
        if d.tier_expired(tier, ttl):
            print(f"ERROR: {tier} clock should have been seeded fresh")
            failed = True
    # Live is deliberately not cached, so its clock stays unset and the first tick polls
    if not d.tier_expired("live", DEYE_TTL_LIVE):
        print("ERROR: the live tier must start expired so telemetry is polled fresh")
        failed = True
    assert not failed, "test_restore_seeds_clocks_and_state"


def test_warm_restart_makes_no_api_calls_beyond_live():
    """The point of the whole feature: a restart inside every TTL only polls device/latest."""
    failed = False
    d = StorageDeye(auth_method="oauth")
    d._mock_storage = _warm_cache()
    # Live is 1 minute, so age it past that while leaving the other tiers fresh
    calls = []

    async def fake_post(endpoint_key, body):
        """Record every endpoint the run touches."""
        calls.append(endpoint_key)
        if endpoint_key == "device_latest":
            return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=_noop):
                            run_async(d.run(seconds=0, first=True))

    if "station_list" in calls or "station_device" in calls:
        print(f"ERROR: discovery re-polled on a warm restart: {calls}")
        failed = True
    if "config_battery" in calls:
        print(f"ERROR: config/battery re-polled while still inside its TTL: {calls}")
        failed = True
    if "device_latest" not in calls:
        print(f"ERROR: the live tier should still poll: {calls}")
        failed = True
    assert not failed, "test_warm_restart_makes_no_api_calls_beyond_live"


async def _true(*args, **kwargs):
    """Async stand-in returning True."""
    return True


async def _noop(*args, **kwargs):
    """Async no-op."""
    return None


async def _noop_arg(*args, **kwargs):
    """Async no-op accepting positional arguments."""
    return None


def test_expired_config_refreshes_without_touching_discovery():
    """Tiers expire independently — a stale config must not drag discovery along with it."""
    failed = False
    d = StorageDeye(auth_method="oauth")
    d._mock_storage = _warm_cache()
    d._mock_storage.ages[DEYE_CACHE_CONFIG] = DEYE_TTL_CONFIG + 5
    calls = []

    async def fake_post(endpoint_key, body):
        """Record every endpoint the run touches."""
        calls.append(endpoint_key)
        if endpoint_key == "device_latest":
            return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}
        return {"success": True, "battCapacity": 1200}

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=_noop):
                            run_async(d.run(seconds=0, first=True))

    if "config_battery" not in calls:
        print(f"ERROR: an expired config tier should re-poll: {calls}")
        failed = True
    if "station_list" in calls:
        print(f"ERROR: discovery must not refresh just because config expired: {calls}")
        failed = True
    assert not failed, "test_expired_config_refreshes_without_touching_discovery"


def test_empty_device_list_forces_discovery_despite_fresh_clock():
    """No devices means rediscover now — never wait out the 8h TTL with nothing to work on."""
    failed = False
    d = StorageDeye(auth_method="oauth")
    # A cache written during an outage: the file exists and is fresh, but holds no devices
    d._mock_storage = FakeStorage(data={DEYE_CACHE_STATIC: {"station_ids": [], "device_list": []}}, ages={DEYE_CACHE_STATIC: 0.1})

    run_async(d.restore_state())
    if not d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: an empty device_list must not seed the static clock")
        failed = True

    # Even with a deliberately fresh clock, an empty device list must still trigger refresh
    d.mark_refreshed("static")
    calls = []

    async def fake_post(endpoint_key, body):
        """Record calls and return a station/device discovery result."""
        calls.append(endpoint_key)
        if endpoint_key == "station_list":
            return {"success": True, "stationList": [{"id": 7}]}
        if endpoint_key == "station_device":
            return {"success": True, "total": 1, "deviceListItems": [{"deviceType": "INVERTER", "deviceSn": "INV9"}]}
        if endpoint_key == "device_latest":
            return {"success": True, "deviceDataList": [{"deviceSn": "INV9", "dataList": LIVE_DATA_LIST}]}
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=_noop):
                            run_async(d.run(seconds=60, first=False))

    if "station_list" not in calls:
        print(f"ERROR: an empty device_list must force rediscovery: {calls}")
        failed = True
    if d.device_list != ["INV9"]:
        print(f"ERROR: rediscovery did not populate the device list: {d.device_list}")
        failed = True
    assert not failed, "test_empty_device_list_forces_discovery_despite_fresh_clock"


def test_failed_discovery_is_not_cached():
    """A discovery that finds nothing must not be saved, or it would stick for 8 hours."""
    failed = False
    d = StorageDeye()
    d._mock_storage = FakeStorage()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: the account returns no stations."""
        return {"success": True, "stationList": []}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async(d.refresh_static())

    if DEYE_CACHE_STATIC in d._mock_storage.saves:
        print("ERROR: an empty discovery must not be cached")
        failed = True
    if not d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: a failed discovery must leave the tier expired so it retries")
        failed = True
    assert not failed, "test_failed_discovery_is_not_cached"


def test_telemetry_is_not_cached_at_all():
    """Telemetry is never persisted: the live tier re-polls, and HA holds the last value.

    Caching it would mean 1440 writes a day to save at most one tick's gap at startup.
    Ratings come from the same poll but ARE cached, because they are static and are what
    lets automatic_config() map soc_max/inverter_limit before the first poll returns.
    """
    failed = False
    d = StorageDeye()
    d._mock_storage = _warm_cache()

    run_async(d.restore_state())

    if d.device_values or d.device_energy:
        print(f"ERROR: telemetry must not be restored: {d.device_values} {d.device_energy}")
        failed = True
    if not d.tier_expired("live", DEYE_TTL_LIVE):
        print("ERROR: with no telemetry cache the live tier must start expired so it polls")
        failed = True
    if d.device_rated_power.get("INV1") != 8000.0:
        print(f"ERROR: ratings must still restore: {d.device_rated_power}")
        failed = True
    assert not failed, "test_telemetry_is_not_cached_at_all"


def test_ratings_are_saved_only_when_they_change():
    """The once-a-minute poll must not rewrite an identical ratings file every time."""
    failed = False
    d = StorageDeye()
    d._mock_storage = FakeStorage()
    d.device_list = ["INV1"]

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST returning the same device/latest payload each call."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async(d.refresh_live())
        first_saves = list(d._mock_storage.saves)
        for _ in range(5):
            run_async(d.refresh_live())

    if first_saves.count(DEYE_CACHE_RATINGS) != 1:
        print(f"ERROR: the first poll should write ratings once: {first_saves}")
        failed = True
    if d._mock_storage.saves.count(DEYE_CACHE_RATINGS) != 1:
        print(f"ERROR: unchanged ratings must not be rewritten: {d._mock_storage.saves}")
        failed = True

    # A genuine change is written
    d.device_rated_power["INV1"] = 5000.0
    run_async(d.save_ratings())
    if d._mock_storage.saves.count(DEYE_CACHE_RATINGS) != 2:
        print(f"ERROR: changed ratings should be saved: {d._mock_storage.saves}")
        failed = True
    assert not failed, "test_ratings_are_saved_only_when_they_change"


def test_restore_primes_the_ratings_signature():
    """Restored ratings are recorded as already-on-disk so the first poll writes nothing."""
    failed = False
    d = StorageDeye()
    d._mock_storage = _warm_cache()
    run_async(d.restore_state())
    d.device_list = ["INV1"]

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST returning the same ratings the cache already holds."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async(d.refresh_live())

    if DEYE_CACHE_RATINGS in d._mock_storage.saves:
        print(f"ERROR: identical restored ratings must not be rewritten: {d._mock_storage.saves}")
        failed = True
    assert not failed, "test_restore_primes_the_ratings_signature"


def test_stale_applied_payload_is_discarded_but_orders_are_not():
    """A stale applied_payload is dropped so the next apply re-writes; orders always resume."""
    failed = False
    d = StorageDeye()
    d._mock_storage = _warm_cache()
    d._mock_storage.data[DEYE_CACHE_CONTROL]["pending_orders"] = {"INV1": "order-1"}
    d._mock_storage.data[DEYE_CACHE_CONTROL]["order_poll_count"] = {"INV1": 2}
    d._mock_storage.ages[DEYE_CACHE_CONTROL] = DEYE_RESTORE_MAX_CONTROL + 1

    run_async(d.restore_state())

    if d.applied_payload:
        print(f"ERROR: a stale applied_payload must be discarded: {d.applied_payload}")
        failed = True
    if not any("applied-payload cache is stale" in m for m in d.log_messages):
        print(f"ERROR: expected a stale applied-payload log line: {d.log_messages}")
        failed = True
    if d.pending_orders != {"INV1": "order-1"}:
        print(f"ERROR: pending orders must resume regardless of age: {d.pending_orders}")
        failed = True
    if d.order_poll_count != {"INV1": 2}:
        print(f"ERROR: the poll count must carry over so DEYE_ORDER_MAX_POLLS still bounds it: {d.order_poll_count}")
        failed = True
    assert not failed, "test_stale_applied_payload_is_discarded_but_orders_are_not"


def test_fresh_applied_payload_suppresses_a_redundant_write():
    """A recent applied_payload survives a restart and stops the same payload being rewritten."""
    failed = False
    d = StorageDeye()
    d._mock_storage = _warm_cache()
    d.device_list = ["INV1"]
    run_async(d.restore_state())

    desired = d.applied_payload.get("INV1")
    if not desired:
        print("ERROR: applied_payload should have restored")
        return True

    posted = []

    async def fake_post(endpoint_key, body):
        """Record any write attempt."""
        posted.append(endpoint_key)
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "build_dynamic_payload", return_value=dict(desired)):
            wrote = run_async(d.apply_dynamic_control("INV1", {}, 50))

    if wrote:
        print("ERROR: an unchanged payload should not be written after a restart")
        failed = True
    if posted:
        print(f"ERROR: no API write should have been made: {posted}")
        failed = True
    assert not failed, "test_fresh_applied_payload_suppresses_a_redundant_write"


def test_first_run_fails_when_telemetry_is_unavailable():
    """A first cycle with no telemetry returns False so ComponentBase retries startup.

    automatic_config() runs on the first cycle alone, so completing startup without
    telemetry would permanently skip the energy args (import_today and friends) until the
    next process restart. Failing the run leaves first set and the driver backs off and
    retries instead.
    """
    failed = False
    d = StorageDeye(auth_method="oauth")
    d._mock_storage = _warm_cache()
    configured = []

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: device/latest reports a failure body."""
        if endpoint_key == "device_latest":
            return {"success": False, "msg": "device offline"}
        return {"success": True}

    async def record_config():
        """Record that automatic_config was reached."""
        configured.append(True)

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=record_config):
                            d.automatic = True
                            result = run_async(d.run(seconds=0, first=True))

    if result is not False:
        print(f"ERROR: a first run without telemetry must return False, got {result!r}")
        failed = True
    if configured:
        print("ERROR: automatic_config must not run on an incomplete first cycle")
        failed = True
    if not any("deferring startup" in m for m in d.log_messages):
        print(f"ERROR: expected a deferred-startup warning: {d.log_messages}")
        failed = True
    assert not failed, "test_first_run_fails_when_telemetry_is_unavailable"


def test_first_run_succeeds_once_telemetry_arrives():
    """The retry completes startup: telemetry polls, automatic_config runs, run returns True."""
    failed = False
    d = StorageDeye(auth_method="oauth")
    d._mock_storage = _warm_cache()
    configured = []

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: device/latest now succeeds."""
        if endpoint_key == "device_latest":
            return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}
        return {"success": True}

    async def record_config():
        """Record that automatic_config was reached."""
        configured.append(True)

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=record_config):
                            d.automatic = True
                            result = run_async(d.run(seconds=0, first=True))

    if result is not True:
        print(f"ERROR: a first run with telemetry must return True, got {result!r}")
        failed = True
    if not configured:
        print("ERROR: automatic_config should run once telemetry is available")
        failed = True
    if not d.device_energy.get("INV1"):
        print(f"ERROR: energy counters should be populated for automatic_config: {d.device_energy}")
        failed = True
    assert not failed, "test_first_run_succeeds_once_telemetry_arrives"


def test_successful_run_reports_a_success_timestamp():
    """A completed cycle must report success, or the component is judged dead.

    components.is_alive() requires a success within the last 60 minutes; with no timestamp
    ever recorded it returns False for the entire session, so every Predbat run ended
    "Complete run status Demand with component errors: DEYE Cloud" with nothing wrong.
    """
    failed = False
    d = StorageDeye(auth_method="oauth")
    d._mock_storage = _warm_cache()
    reported = []

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: device/latest succeeds."""
        if endpoint_key == "device_latest":
            return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=_noop):
                            with patch.object(d, "update_success_timestamp", side_effect=lambda: reported.append(True)):
                                result = run_async(d.run(seconds=0, first=True))

    if result is not True:
        print(f"ERROR: expected a successful run, got {result!r}")
        failed = True
    if not reported:
        print("ERROR: a successful cycle must call update_success_timestamp()")
        failed = True
    assert not failed, "test_successful_run_reports_a_success_timestamp"


def test_failed_run_does_not_report_success():
    """A cycle that bails must not report success, or a dead component would look alive."""
    failed = False
    d = StorageDeye(auth_method="oauth")
    d._mock_storage = _warm_cache()
    reported = []

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: device/latest fails, so the first cycle defers."""
        if endpoint_key == "device_latest":
            return {"success": False, "msg": "device offline"}
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "check_and_refresh_oauth_token", side_effect=_true):
            with patch.object(d, "publish_data", side_effect=_noop):
                with patch.object(d, "publish_schedule_settings_ha", side_effect=_noop_arg):
                    with patch.object(d, "_reconcile_control", side_effect=_noop):
                        with patch.object(d, "automatic_config", side_effect=_noop):
                            with patch.object(d, "update_success_timestamp", side_effect=lambda: reported.append(True)):
                                result = run_async(d.run(seconds=0, first=True))

    if result is not False:
        print(f"ERROR: expected the cycle to fail, got {result!r}")
        failed = True
    if reported:
        print("ERROR: a failed cycle must not report success")
        failed = True
    assert not failed, "test_failed_run_does_not_report_success"


def test_storage_absent_behaves_as_before():
    """With no storage component every load/save no-ops and each tier simply refreshes."""
    failed = False
    d = StorageDeye()  # no _mock_storage set, so the property returns None

    run_async(d.restore_state())

    if d.device_list or d.device_values:
        print("ERROR: nothing should be restored without storage")
        failed = True
    for tier, ttl in (("static", DEYE_TTL_STATIC), ("config", DEYE_TTL_CONFIG), ("live", DEYE_TTL_LIVE)):
        if not d.tier_expired(tier, ttl):
            print(f"ERROR: {tier} must be expired so it refreshes without storage")
            failed = True
    if run_async(d.save_static()) is not False:
        print("ERROR: save must no-op and report False without storage")
        failed = True
    assert not failed, "test_storage_absent_behaves_as_before"


def test_corrupt_cache_only_affects_its_own_tier():
    """An unreadable file refreshes that tier alone and never breaks the others."""
    failed = False
    d = StorageDeye()
    d._mock_storage = _warm_cache()
    d._mock_storage.fail_on.add(DEYE_CACHE_CONFIG)

    run_async(d.restore_state())

    if not d.tier_expired("config", DEYE_TTL_CONFIG):
        print("ERROR: an unreadable config cache must leave that tier expired")
        failed = True
    if d.device_list != ["INV1"]:
        print(f"ERROR: the other tiers must still restore: {d.device_list}")
        failed = True
    if d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: a corrupt config file must not disturb the static clock")
        failed = True
    if not any("could not read the config cache" in m for m in d.log_messages):
        print(f"ERROR: expected a cache-read warning: {d.log_messages}")
        failed = True
    assert not failed, "test_corrupt_cache_only_affects_its_own_tier"


def test_shape_validation_rejects_garbage():
    """A file holding the wrong shape is ignored rather than poisoning in-memory state."""
    failed = False
    d = StorageDeye()
    d._mock_storage = FakeStorage(
        data={
            DEYE_CACHE_STATIC: ["not", "a", "dict"],
            DEYE_CACHE_CONFIG: "garbage",
            DEYE_CACHE_RATINGS: 42,
            DEYE_CACHE_CONTROL: {"applied_payload": "nope", "pending_orders": []},
        },
        ages={name: 0.1 for name in (DEYE_CACHE_STATIC, DEYE_CACHE_CONFIG, DEYE_CACHE_RATINGS, DEYE_CACHE_CONTROL)},
    )

    run_async(d.restore_state())

    if d.device_list or d.device_battery_config or d.applied_payload:
        print(f"ERROR: garbage should not be restored: {d.device_list} {d.device_battery_config} {d.applied_payload}")
        failed = True
    if not isinstance(d.pending_orders, dict):
        print(f"ERROR: pending_orders must stay a dict: {d.pending_orders}")
        failed = True
    if not d.tier_expired("static", DEYE_TTL_STATIC):
        print("ERROR: a rejected cache must leave the tier expired")
        failed = True
    assert not failed, "test_shape_validation_rejects_garbage"


def test_refresh_saves_each_tier():
    """A successful refresh writes its own cache file; the live poll writes ratings."""
    failed = False
    d = StorageDeye()
    d._mock_storage = FakeStorage()
    d.device_list = ["INV1"]

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST covering config/battery and device/latest."""
        if endpoint_key == "device_latest":
            return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}
        return {"success": True, "battCapacity": 1200}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async(d.refresh_config())
        run_async(d.refresh_live())

    for name in (DEYE_CACHE_CONFIG, DEYE_CACHE_RATINGS):
        if name not in d._mock_storage.saves:
            print(f"ERROR: {name} was not saved after a successful refresh: {d._mock_storage.saves}")
            failed = True
    assert not failed, "test_refresh_saves_each_tier"


def test_failed_live_refresh_does_not_save_or_clobber():
    """A failed poll leaves the cached values alone and writes nothing."""
    failed = False
    d = StorageDeye()
    d._mock_storage = FakeStorage()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 80.0}}

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: device/latest returns a failure body."""
        return {"success": False, "msg": "device offline"}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async(d.refresh_live())

    if d._mock_storage.saves:
        print(f"ERROR: a failed refresh must not save: {d._mock_storage.saves}")
        failed = True
    if d.device_values.get("INV1", {}).get("soc") != 80.0:
        print(f"ERROR: a failed refresh must not clobber cached values: {d.device_values}")
        failed = True
    if not d.tier_expired("live", DEYE_TTL_LIVE):
        print("ERROR: a failed refresh must leave the tier expired so it retries")
        failed = True
    assert not failed, "test_failed_live_refresh_does_not_save_or_clobber"


def test_save_failure_is_survivable():
    """A storage write error is logged, not raised — a cache must never break a run."""
    failed = False
    d = StorageDeye()
    d._mock_storage = FakeStorage()
    d._mock_storage.fail_on.add(DEYE_CACHE_STATIC)
    d.station_ids = [1]
    d.device_list = ["INV1"]

    try:
        result = run_async(d.save_static())
    except Exception as e:
        print(f"ERROR: save must not raise, got {type(e).__name__}: {e}")
        return True

    if result is not False:
        print(f"ERROR: a failed save should report False, got {result!r}")
        failed = True
    if not any("could not write the static cache" in m for m in d.log_messages):
        print(f"ERROR: expected a cache-write warning: {d.log_messages}")
        failed = True
    assert not failed, "test_save_failure_is_survivable"


def run_deye_storage_tests(my_predbat):
    """Run all DEYE storage persistence tests."""
    failed = False
    for name, fn in [
        ("tier_expiry_clock", test_tier_expiry_clock),
        ("restore_seeds_clocks", test_restore_seeds_clocks_and_state),
        ("warm_restart_no_api", test_warm_restart_makes_no_api_calls_beyond_live),
        ("tiers_expire_independently", test_expired_config_refreshes_without_touching_discovery),
        ("empty_devices_force_discovery", test_empty_device_list_forces_discovery_despite_fresh_clock),
        ("failed_discovery_not_cached", test_failed_discovery_is_not_cached),
        ("telemetry_not_cached", test_telemetry_is_not_cached_at_all),
        ("ratings_saved_on_change", test_ratings_are_saved_only_when_they_change),
        ("restore_primes_ratings", test_restore_primes_the_ratings_signature),
        ("stale_applied_payload", test_stale_applied_payload_is_discarded_but_orders_are_not),
        ("fresh_applied_payload_suppresses", test_fresh_applied_payload_suppresses_a_redundant_write),
        ("first_fails_without_telemetry", test_first_run_fails_when_telemetry_is_unavailable),
        ("first_succeeds_with_telemetry", test_first_run_succeeds_once_telemetry_arrives),
        ("success_timestamp_reported", test_successful_run_reports_a_success_timestamp),
        ("no_success_on_failure", test_failed_run_does_not_report_success),
        ("storage_absent", test_storage_absent_behaves_as_before),
        ("corrupt_cache_isolated", test_corrupt_cache_only_affects_its_own_tier),
        ("shape_validation", test_shape_validation_rejects_garbage),
        ("refresh_saves_tiers", test_refresh_saves_each_tier),
        ("failed_refresh_safe", test_failed_live_refresh_does_not_save_or_clobber),
        ("save_failure_survivable", test_save_failure_is_survivable),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_storage.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_storage.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

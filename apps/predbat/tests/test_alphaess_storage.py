# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS cache persistence
# -----------------------------------------------------------------------------

"""Tests for AlphaESS storage-backed cache persistence across restarts."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from unittest.mock import patch
from tests.test_alphaess_api import MockAlphaESS, ESS_LIST_SAMPLE, CHARGE_CONFIG_SAMPLE, DISCHARGE_CONFIG_SAMPLE, _envelope
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self, fail=False):
        """Set up an empty store, optionally one that raises on every call."""
        self.data = {}
        self.fail = fail

    async def load(self, module, name):
        """Return a previously saved entry, or None when absent.

        This matches the real Storage component contract - the absence of a cached value
        is represented as None, not {}, so that load_cache can distinguish between
        "nothing was ever saved" and "something was saved but it was an empty dict".
        """
        if self.fail:
            raise IOError("storage unavailable")
        return self.data.get((module, name))

    async def save(self, module, name, payload):
        """Record an entry."""
        if self.fail:
            raise IOError("storage unavailable")
        self.data[(module, name)] = payload


class StoredAlphaESS(MockAlphaESS):
    """MockAlphaESS with a working Storage component attached."""

    def __init__(self, store=None, **kwargs):
        """Attach a FakeStorage so the cache paths are actually exercised."""
        super().__init__(**kwargs)
        self._store = store if store is not None else FakeStorage()

    @property
    def storage(self):
        """Return the fake store."""
        return self._store


def test_alphaess_cache_round_trip():
    """Every verdict the component learns survives a restart."""
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    client.device_config = {"AL70": {"charge": {"gridCharge": 1}}}
    client._periodic_ok = {"AL70": False}
    client._live_ok = {"AL70": False}
    client.control_active = {"AL70"}
    client._unbind_done = {"AL99"}
    client.applied_payload = {"AL70": {"charge": {"sysSn": "AL70", "gridCharge": 1}}}
    run_async_local(client.save_static())
    run_async_local(client.save_config())
    run_async_local(client.save_ratings())
    run_async_local(client.save_control())

    restored = StoredAlphaESS(store=store)
    run_async_local(restored.restore_state())
    # Type assertions: these attributes are used as specific types elsewhere and must
    # round-trip with correct types, not just correct values.
    if not isinstance(restored.device_list, list):
        print(f"ERROR: device_list type {type(restored.device_list)}, expected list")
        failed = True
    if not isinstance(restored.device_detail, dict):
        print(f"ERROR: device_detail type {type(restored.device_detail)}, expected dict")
        failed = True
    if not isinstance(restored.device_config, dict):
        print(f"ERROR: device_config type {type(restored.device_config)}, expected dict")
        failed = True
    if not isinstance(restored._periodic_ok, dict):
        print(f"ERROR: _periodic_ok type {type(restored._periodic_ok)}, expected dict")
        failed = True
    if not isinstance(restored._live_ok, dict):
        print(f"ERROR: _live_ok type {type(restored._live_ok)}, expected dict")
        failed = True
    if not isinstance(restored.control_active, set):
        print(f"ERROR: control_active type {type(restored.control_active)}, expected set")
        failed = True
    if not isinstance(restored._unbind_done, set):
        print(f"ERROR: _unbind_done type {type(restored._unbind_done)}, expected set")
        failed = True
    if not isinstance(restored.local_schedule, dict):
        print(f"ERROR: local_schedule type {type(restored.local_schedule)}, expected dict")
        failed = True
    if not isinstance(restored.applied_payload, dict):
        print(f"ERROR: applied_payload type {type(restored.applied_payload)}, expected dict")
        failed = True
    # Value assertions
    if restored.device_list != ["AL70"]:
        print(f"ERROR: device_list {restored.device_list}")
        failed = True
    if restored._periodic_ok.get("AL70") is not False:
        print(f"ERROR: periodic verdict not restored: {restored._periodic_ok}")
        failed = True
    if restored._live_ok.get("AL70") is not False:
        print(f"ERROR: live verdict not restored: {restored._live_ok}")
        failed = True
    if "AL70" not in restored.control_active:
        print(f"ERROR: control_active not restored: {restored.control_active}")
        failed = True
    if "AL99" not in restored._unbind_done:
        print(f"ERROR: unbind latch not restored: {restored._unbind_done}")
        failed = True
    if restored.applied_payload.get("AL70", {}).get("charge", {}).get("gridCharge") != 1:
        print(f"ERROR: applied payload not restored: {restored.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_cache_round_trip"


def test_alphaess_no_storage_component_is_silent():
    """self.storage being None is the normal state for a standalone CLI run.

    It is a permanent, by-design condition rather than a transient fault, so it must not
    warn and must not flag the restore as incomplete.
    """
    failed = False
    client = MockAlphaESS()  # storage property returns None
    run_async_local(client.save_control())
    data = run_async_local(client.load_cache("control"))
    if data != {}:
        print(f"ERROR: load_cache returned {data}")
        failed = True
    if any("Warn" in message for message in client.log_messages):
        print(f"ERROR: warned about an absent Storage component: {client.log_messages}")
        failed = True
    if client._restore_had_error:
        print("ERROR: an absent Storage component flagged a restore error")
        failed = True
    assert not failed, "test_alphaess_no_storage_component_is_silent"


def test_alphaess_real_storage_failure_is_flagged_for_retry():
    """A genuine storage fault must warn AND leave the restore marked incomplete, so a
    transient outage is retried rather than silently marked done with nothing restored."""
    failed = False
    client = StoredAlphaESS(store=FakeStorage(fail=True))
    run_async_local(client.restore_state())
    if not any("Warn" in message for message in client.log_messages):
        print(f"ERROR: no warning on a real storage failure: {client.log_messages}")
        failed = True
    if client._cache_restored:
        print("ERROR: a failed restore was marked complete")
        failed = True
    assert not failed, "test_alphaess_real_storage_failure_is_flagged_for_retry"


def test_alphaess_empty_discovery_is_not_persisted():
    """Writing {'device_list': []} and stamping the tier fresh would make a restart restore
    nothing and skip re-discovery for a full 8-hour TTL."""
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    run_async_local(client.save_static())
    saved = store.data.get(("alphaess", "static"), {})
    if saved.get("device_list") != ["AL70"]:
        print(f"ERROR: static cache {saved}")
        failed = True
    # An empty in-memory list must not overwrite a good cache.
    client.device_list = []
    run_async_local(client.save_static())
    saved = store.data.get(("alphaess", "static"), {})
    if saved.get("device_list") != ["AL70"]:
        print(f"ERROR: empty discovery overwrote the cache: {saved}")
        failed = True
    assert not failed, "test_alphaess_empty_discovery_is_not_persisted"


def test_alphaess_corrupted_cache_data_coerced_safely():
    """Non-dict values in the cache are safely coerced to {}, not propagated.

    If the store is corrupted or returns a string, list, or other non-dict value,
    load_cache must coerce it to {} rather than passing it through, so restore_state
    uses the default initialization rather than crashing on attribute access.
    """
    failed = False

    class CorruptedStorage:
        """Storage that returns non-dict values instead of dicts."""

        async def load(self, module, name):
            """Return various non-dict types to test coercion."""
            if name == "static":
                return "corrupted_string"
            elif name == "config":
                return ["corrupted", "list"]
            elif name == "ratings":
                return 42
            elif name == "control":
                return ("corrupted", "tuple")
            return None

    client = StoredAlphaESS(store=CorruptedStorage())
    run_async_local(client.restore_state())
    # All caches were corrupted but coerced safely to {}, so defaults apply.
    if client.device_list != []:
        print(f"ERROR: device_list should be empty default: {client.device_list}")
        failed = True
    if client.device_detail != {}:
        print(f"ERROR: device_detail should be empty default: {client.device_detail}")
        failed = True
    if client.device_config != {}:
        print(f"ERROR: device_config should be empty default: {client.device_config}")
        failed = True
    if client._periodic_ok != {}:
        print(f"ERROR: _periodic_ok should be empty default: {client._periodic_ok}")
        failed = True
    if client._live_ok != {}:
        print(f"ERROR: _live_ok should be empty default: {client._live_ok}")
        failed = True
    if client.control_active != set():
        print(f"ERROR: control_active should be empty default: {client.control_active}")
        failed = True
    if client._unbind_done != set():
        print(f"ERROR: _unbind_done should be empty default: {client._unbind_done}")
        failed = True
    if client.local_schedule != {}:
        print(f"ERROR: local_schedule should be empty default: {client.local_schedule}")
        failed = True
    if client.applied_payload != {}:
        print(f"ERROR: applied_payload should be empty default: {client.applied_payload}")
        failed = True
    # No storage failure should have been logged because coercion is silent.
    if any("Warn" in message for message in client.log_messages):
        print(f"ERROR: corrupted cache data should not warn: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_corrupted_cache_data_coerced_safely"


def test_alphaess_refresh_static_saves_inline_on_success():
    """A container kill or crash - the ordinary Home Assistant add-on restart - between
    refresh cycles must not lose discovery. Before the fix, save_static/save_config/
    save_ratings/save_control were called only from final(), which runs only on a clean
    loop exit, so the whole point of persisting at all was defeated by the most common
    restart path. This checks the cache reaches storage the moment refresh_static
    succeeds, without final() ever being called."""
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.api_delay = 0
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        ok = run_async_local(client.refresh_static())
    if not ok:
        print("ERROR: refresh_static reported failure")
        failed = True
    saved = store.data.get(("alphaess", "static"), {})
    if saved.get("device_list") != client.device_list:
        print(f"ERROR: static cache not saved inline by refresh_static: {saved}")
        failed = True
    assert not failed, "test_alphaess_refresh_static_saves_inline_on_success"


def test_alphaess_refresh_config_saves_inline_when_a_serial_fully_succeeds():
    """Same as refresh_static: the config baseline and the periodic entitlement verdict
    must reach storage the moment refresh_config succeeds, not only at shutdown."""
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.api_delay = 0
    client.device_list = ["AL70"]
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, CHARGE_CONFIG_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, DISCHARGE_CONFIG_SAMPLE)),
        # probe_periodic, since AL70 is not yet in _periodic_ok.
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        got_any = run_async_local(client.refresh_config())
    if not got_any:
        print("ERROR: refresh_config reported failure")
        failed = True
    saved = store.data.get(("alphaess", "config"), {})
    if saved.get("device_config", {}).get("AL70", {}).get("charge") != CHARGE_CONFIG_SAMPLE:
        print(f"ERROR: config cache not saved inline by refresh_config: {saved}")
        failed = True
    if saved.get("periodic_ok", {}).get("AL70") is not False:
        print(f"ERROR: the periodic verdict learned this cycle was not part of the inline save: {saved}")
        failed = True
    assert not failed, "test_alphaess_refresh_config_saves_inline_when_a_serial_fully_succeeds"


def test_alphaess_write_persists_the_control_cache_inline():
    """A container kill right after a write must not lose the applied-payload cache or the
    write-pacing timestamp (last_write_time) - both must reach storage the moment the write
    completes, success OR rejection, not only at shutdown via final().

    Also covers last_write_time's JSON encoding end to end: it is keyed (sn, direction),
    which JSON cannot carry as a dict key, so it is flattened to "sn|direction" strings -
    this checks that flattened key actually lands in the saved cache.
    """
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.api_delay = 0
    client.control_active.add("AL70")
    schedule = {
        "reserve": 10,
        "charge": {"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"},
        "export": {"enable": False, "soc": 0, "power": 3000, "start": "00:00:00", "end": "00:00:00"},
    }
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.time.time", return_value=5000.0):
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
            run_async_local(client.apply_settings("AL70", schedule))
    saved = store.data.get(("alphaess", "control"), {})
    if saved.get("applied_payload", {}).get("AL70", {}).get("charge", {}).get("batHighCap") != 90:
        print(f"ERROR: applied_payload not saved inline on a successful write: {saved}")
        failed = True
    if saved.get("last_write_time", {}).get("AL70|charge") != 5000.0:
        print(f"ERROR: last_write_time not saved inline (flattened as 'AL70|charge'): {saved}")
        failed = True

    # A rejected write must ALSO persist its attempt timestamp inline, so the write budget
    # survives a restart even though nothing was actually written this time.
    changed_schedule = dict(schedule, charge=dict(schedule["charge"], soc=70))
    rejected = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    with patch("alphaess.time.time", return_value=6000.0):
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(rejected)):
            run_async_local(client.apply_settings("AL70", changed_schedule))
    saved = store.data.get(("alphaess", "control"), {})
    if saved.get("last_write_time", {}).get("AL70|charge") != 6000.0:
        print(f"ERROR: a rejected write's timestamp was not saved inline: {saved}")
        failed = True
    if saved.get("applied_payload", {}).get("AL70", {}).get("charge", {}).get("batHighCap") != 90:
        print(f"ERROR: a rejected write's payload was wrongly cached as applied: {saved}")
        failed = True
    assert not failed, "test_alphaess_write_persists_the_control_cache_inline"


def test_alphaess_last_write_time_round_trips_across_a_restart():
    """The write-pacing timestamps must survive a restart, or the documented 24-hour write
    budget resets every time the add-on restarts, and a restart loop bypasses
    alphaess_min_write_interval entirely. Keys are (sn, direction) tuples, which JSON
    cannot carry, so they must round-trip through the "sn|direction" string encoding
    without loss - including keeping the (sn, direction) tuple SHAPE, not just the value.
    """
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.last_write_time = {("AL70", "charge"): 12345.5, ("AL70", "discharge"): 999.0, ("AL71", "periodic"): 42.0}
    run_async_local(client.save_control())

    restored = StoredAlphaESS(store=store)
    run_async_local(restored.restore_state())
    if restored.last_write_time.get(("AL70", "charge")) != 12345.5:
        print(f"ERROR: AL70/charge timestamp not restored: {restored.last_write_time}")
        failed = True
    if restored.last_write_time.get(("AL70", "discharge")) != 999.0:
        print(f"ERROR: AL70/discharge timestamp not restored: {restored.last_write_time}")
        failed = True
    if restored.last_write_time.get(("AL71", "periodic")) != 42.0:
        print(f"ERROR: AL71/periodic timestamp not restored: {restored.last_write_time}")
        failed = True
    if len(restored.last_write_time) != 3:
        print(f"ERROR: unexpected extra or missing entries: {restored.last_write_time}")
        failed = True
    assert not failed, "test_alphaess_last_write_time_round_trips_across_a_restart"


def run_alphaess_storage_tests(my_predbat):
    """Run all AlphaESS storage tests."""
    failed = False
    for name, fn in [
        ("cache_round_trip", test_alphaess_cache_round_trip),
        ("no_storage_silent", test_alphaess_no_storage_component_is_silent),
        ("real_failure_flagged", test_alphaess_real_storage_failure_is_flagged_for_retry),
        ("empty_discovery_not_persisted", test_alphaess_empty_discovery_is_not_persisted),
        ("corrupted_data_coerced_safely", test_alphaess_corrupted_cache_data_coerced_safely),
        ("refresh_static_saves_inline", test_alphaess_refresh_static_saves_inline_on_success),
        ("refresh_config_saves_inline", test_alphaess_refresh_config_saves_inline_when_a_serial_fully_succeeds),
        ("write_persists_control_inline", test_alphaess_write_persists_the_control_cache_inline),
        ("last_write_time_round_trip", test_alphaess_last_write_time_round_trips_across_a_restart),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_storage.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_storage.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

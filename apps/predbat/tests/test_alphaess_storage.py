# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS cache persistence
# -----------------------------------------------------------------------------

"""Tests for AlphaESS storage-backed cache persistence across restarts."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from tests.test_alphaess_api import MockAlphaESS
from tests.test_infra import run_async as run_async_local


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self, fail=False):
        """Set up an empty store, optionally one that raises on every call."""
        self.data = {}
        self.fail = fail

    async def load(self, module, name):
        """Return a previously saved entry, or {} when absent."""
        if self.fail:
            raise IOError("storage unavailable")
        return self.data.get((module, name), {})

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


def run_alphaess_storage_tests(my_predbat):
    """Run all AlphaESS storage tests."""
    failed = False
    for name, fn in [
        ("cache_round_trip", test_alphaess_cache_round_trip),
        ("no_storage_silent", test_alphaess_no_storage_component_is_silent),
        ("real_failure_flagged", test_alphaess_real_storage_failure_is_flagged_for_retry),
        ("empty_discovery_not_persisted", test_alphaess_empty_discovery_is_not_persisted),
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

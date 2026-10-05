"""Managed Axle explicit-null scope and restart metadata regressions."""

import asyncio
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from storage import StorageLocalFiles
from axle import AxleAPI


class ManagedReader(AxleAPI):
    """Minimal source host without the shared Predbat/config fixture."""

    def __init__(self):
        """Supply source sensor and clock dependencies only."""
        self.prefix = "predbat"
        self._state_store = {}
        self.dashboard_items = {}
        self._now_utc = datetime(2026, 1, 1, 10, 10, tzinfo=timezone.utc)

    @property
    def now_utc(self):
        """Return the isolated source observation clock."""
        return self._now_utc

    def log(self, message):
        """Discard source diagnostics."""
        pass

    def get_state_wrapper(self, entity_id, attribute=None):
        """Read isolated sensor attributes."""
        return self._state_store.get(entity_id, {}).get("attributes", {}).get(attribute)

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Capture published metadata and make it available for restart."""
        item = {"state": state, "attributes": attributes}
        self.dashboard_items[entity_id] = item
        self._state_store[entity_id] = item


def managed_reader():
    """Construct an isolated managed component using its real initialisation."""
    reader = ManagedReader()
    reader.initialize(api_key=None, pence_per_kwh=100, automatic=False, managed_mode=True, site_id="site", partner_username="test", partner_password="test")
    reader._now_utc = datetime(2026, 1, 1, 10, 10, tzinfo=timezone.utc)
    return reader


def curve(*rows):
    """Build the provider's raw half-hour price response."""
    return {"half_hourly_traded_prices": [{"start_timestamp": start, "price_gbp_per_mwh": price} for start, price in rows]}


def run_axle_managed_periods_tests(my_predbat=None):
    """Run scoped source metadata regressions without shared fixture mutation."""
    failed = 0
    for check in (test_future_and_active_null, test_past_and_absent_horizon, test_managed_storage_and_sensor_roundtrip):
        try:
            check()
            print("PASS:", check.__name__)
        except AssertionError as error:
            print("ERROR:", check.__name__, error)
            failed += 1
    return failed


def test_future_and_active_null():
    """Null removes only the matching provisional row and retains nullable metadata."""
    reader = managed_reader()
    reader._process_price_curve(curve(("2026-01-01T11:00:00Z", None)))
    assert reader.event_history == []
    assert reader.managed_price_periods == [{"start_time": "2026-01-01T11:00:00+00:00", "end_time": "2026-01-01T11:30:00+00:00", "pence_per_kwh": None}]
    reader._process_price_curve(curve(("2026-01-01T11:00:00Z", 50)))
    assert len(reader.event_history) == 1
    reader._process_price_curve(curve(("2026-01-01T11:00:00Z", None)))
    assert reader.event_history == []
    reader._process_price_curve(curve(("2026-01-01T10:00:00Z", -20)))
    assert reader.current_event["pence_per_kwh"] == -2.0
    # Equivalent absolute bounds with a different offset still match.
    reader._process_price_curve(curve(("2026-01-01T11:00:00+01:00", None)))
    assert reader.current_event["start_time"] is None and reader.event_history == []
    reader.publish_axle_event()
    attributes = reader.dashboard_items["binary_sensor.predbat_axle_event"]["attributes"]
    assert attributes["managed_mode"] is True and attributes["managed_price_periods"] == reader.managed_price_periods
    assert attributes["event_current"] == []


def test_past_and_absent_horizon():
    """Late null preserves closed source rows; outside returned scope is unknown."""
    reader = managed_reader()
    reader._process_price_curve(curve(("2026-01-01T09:00:00Z", 50), ("2026-01-01T11:00:00Z", 60), ("2026-01-01T12:00:00Z", 70)))
    reader._process_price_curve(curve(("2026-01-01T09:00:00Z", None), ("2026-01-01T11:00:00Z", None)))
    assert len(reader.event_history) == 2
    assert sorted(event["pence_per_kwh"] for event in reader.event_history) == [5.0, 7.0]
    assert len(reader.managed_price_periods) == 2 and all(period["pence_per_kwh"] is None for period in reader.managed_price_periods)
    # A missing field is not explicit null and must not cancel an old projection.
    reader._process_price_curve({"half_hourly_traded_prices": [{"start_timestamp": "2026-01-01T12:00:00Z"}]})
    assert len(reader.event_history) == 2 and reader.managed_price_periods == []
    reader._process_price_curve(curve())
    assert len(reader.event_history) == 2  # Empty horizon does not cancel absent rows.


def test_managed_storage_and_sensor_roundtrip():
    """Real Storage preserves nullable scope through sensor-first startup paths."""
    with TemporaryDirectory() as directory:
        storage = StorageLocalFiles(directory, lambda message: None)
        reader = managed_reader()
        reader.base = SimpleNamespace(components=SimpleNamespace(get_component=lambda name: storage))
        reader._process_price_curve(curve(("2026-01-01T09:00:00Z", 50), ("2026-01-01T11:00:00Z", None)))
        asyncio.run(reader.save_event_history())
        payload = asyncio.run(storage.load("axle", "event_history"))
        assert payload["managed_price_periods"] == reader.managed_price_periods
        restored = managed_reader()
        restored.base = reader.base
        asyncio.run(restored.load_event_history())
        assert restored.managed_price_periods == reader.managed_price_periods
        reader.publish_axle_event()
        # Sensor-priority path must retain metadata even when it skips storage.
        sensor_restored = managed_reader()
        sensor_restored._state_store = reader._state_store
        asyncio.run(sensor_restored.load_event_history())
        assert sensor_restored.managed_price_periods == reader.managed_price_periods
        # Legacy sensor with valid history but no metadata obtains metadata only
        # from Storage, without discarding the chosen sensor history.
        legacy_restored = managed_reader()
        sensor = reader._state_store["binary_sensor.predbat_axle_event"]
        attributes = dict(sensor["attributes"])
        del attributes["managed_price_periods"]
        legacy_restored._state_store = {"binary_sensor.predbat_axle_event": {"state": sensor["state"], "attributes": attributes}}
        legacy_restored.base = reader.base
        asyncio.run(legacy_restored.load_event_history())
        assert legacy_restored.managed_price_periods == reader.managed_price_periods
        assert len(legacy_restored.event_history) == 1


if __name__ == "__main__":
    raise SystemExit(run_axle_managed_periods_tests())

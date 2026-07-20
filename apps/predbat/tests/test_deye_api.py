# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the DEYE Cloud API component (``deye.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from deye import DeyeAPI
from deye_const import DEYE_BASE_URLS, DEYE_TELEMETRY_KEYS
from tests.test_infra import run_async as run_async_local


class MockDeye(DeyeAPI):
    """Test double: build a DeyeAPI without the full component lifecycle."""

    def __init__(self, auth_method="app_credentials", data_center="eu", inverter_sn=None):
        """Set up a minimal DeyeAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.automatic = False
        self.automatic_ignore_pv = False
        self.data_center = data_center
        self.company_id = ""
        self.app_id = ""
        self.app_secret = "test-secret"
        self.username = ""
        self.password = ""
        self.token_hash = ""
        self.inverter_sn_filter = inverter_sn or []
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.order_poll_count = {}
        self.applied_payload = {}
        self.cached_values = {}
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-deye-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self._init_oauth(auth_method, "test-token", None, "deye")

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass


def test_deye_base_url():
    """base_url resolves from the data centre."""
    failed = False
    d = MockDeye(data_center="eu")
    if d.base_url != DEYE_BASE_URLS["eu"]:
        print(f"ERROR: base_url {d.base_url}")
        failed = True
    assert not failed, "test_deye_base_url"


def test_get_device_list_filters_inverters():
    """Only INVERTER devices are kept; sn filter is honoured case-insensitively."""
    failed = False
    # Filter is lower-case while the device serial is upper-case: proves case-insensitivity.
    d = MockDeye(inverter_sn=["inv1"])

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return a station list, then a paginated device list."""
        if endpoint_key == "station_list":
            return {"success": True, "stationList": [{"id": 10}]}
        if endpoint_key == "station_device":
            return {
                "success": True,
                "total": 3,
                "deviceListItems": [
                    {"deviceType": "INVERTER", "deviceSn": "INV1"},
                    {"deviceType": "INVERTER", "deviceSn": "INV2"},
                    {"deviceType": "METER", "deviceSn": "MET9"},
                ],
            }
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        devices = run_async_local(d.get_device_list())
    # INV2 is a real inverter but not in the filter, so it must be excluded (proves the sn filter runs);
    # INV1 must survive despite the case mismatch between filter ("inv1") and serial ("INV1").
    if devices != ["INV1"]:
        print(f"ERROR: devices {devices}")
        failed = True
    assert not failed, "test_get_device_list_filters_inverters"


def test_fetch_device_data_maps_keys():
    """dataList key/value pairs map to normalised telemetry via the key table."""
    failed = False
    d = MockDeye()
    data_list = [
        {"key": DEYE_TELEMETRY_KEYS["soc"], "value": "57", "unit": "%"},
        {"key": DEYE_TELEMETRY_KEYS["grid_power"], "value": "-1200", "unit": "W"},
    ]

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return a single device/latest dataList payload."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": data_list}]}

    with patch.object(d, "_post", side_effect=fake_post):
        out = run_async_local(d.fetch_device_data("INV1"))
    if out.get("soc") != 57.0:
        print(f"ERROR: soc {out.get('soc')}")
        failed = True
    if out.get("grid_power") != -1200.0:
        print(f"ERROR: grid_power {out.get('grid_power')}")
        failed = True
    if d.device_values.get("INV1", {}).get("soc") != 57.0:
        print("ERROR: not cached")
        failed = True
    assert not failed, "test_fetch_device_data_maps_keys"


def test_fetch_battery_config_caches_on_success():
    """A successful config/battery call caches the payload per serial and returns it."""
    failed = False
    d = MockDeye()
    payload = {"success": True, "battCapacity": 100, "battLowCapacity": 10, "maxChargeCurrent": 25}

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return a successful battery config payload."""
        return payload

    with patch.object(d, "_post", side_effect=fake_post):
        out = run_async_local(d.fetch_battery_config("INV1"))
    if out != payload:
        print(f"ERROR: fetch_battery_config return {out}")
        failed = True
    if d.device_battery_config.get("INV1") != payload:
        print(f"ERROR: not cached: {d.device_battery_config}")
        failed = True
    assert not failed, "test_fetch_battery_config_caches_on_success"


def test_fetch_battery_config_failure_returns_empty():
    """A failed config/battery call returns an empty dict and does not cache."""
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: report a failed battery config lookup."""
        return {"success": False, "msg": "device offline"}

    with patch.object(d, "_post", side_effect=fake_post):
        out = run_async_local(d.fetch_battery_config("INV1"))
    if out != {}:
        print(f"ERROR: expected empty dict on failure, got {out}")
        failed = True
    if "INV1" in d.device_battery_config:
        print(f"ERROR: should not cache on failure: {d.device_battery_config}")
        failed = True
    assert not failed, "test_fetch_battery_config_failure_returns_empty"


def test_run_first_cycle_publishes_and_configures():
    """First run discovers, publishes and (when automatic) configures."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "tok"
    d.automatic = True
    seq = {"published": 0, "configured": 0}

    async def fake_dev_list():
        """Fake device discovery returning a single inverter serial."""
        d.device_list = ["INV1"]
        return ["INV1"]

    async def fake_data(sn):
        """Fake telemetry fetch caching a single SoC reading."""
        d.device_values[sn] = {"soc": 55.0}
        return d.device_values[sn]

    async def fake_batt(sn):
        """Fake battery config fetch returning an empty payload."""
        return {}

    async def fake_publish():
        """Fake publish that records the call."""
        seq["published"] += 1

    async def fake_pub_sched(sn):
        """Fake schedule publish that is a no-op."""
        pass

    async def fake_get_sched(sn):
        """Fake schedule read returning an empty payload."""
        return {}

    async def fake_auto():
        """Fake automatic_config that records the call."""
        seq["configured"] += 1

    from unittest.mock import patch

    with patch.multiple(
        d,
        get_device_list=fake_dev_list,
        fetch_device_data=fake_data,
        fetch_battery_config=fake_batt,
        publish_data=fake_publish,
        publish_schedule_settings_ha=fake_pub_sched,
        get_schedule_settings_ha=fake_get_sched,
        automatic_config=fake_auto,
    ):
        ok = run_async_local(d.run(0, True))
    if not ok:
        print("ERROR: run returned falsy")
        failed = True
    if seq["published"] == 0 or seq["configured"] == 0:
        print(f"ERROR: run did not publish/configure {seq}")
        failed = True
    assert not failed, "test_run_first_cycle_publishes_and_configures"


def run_deye_api_tests(my_predbat):
    """Run all DEYE API tests."""
    failed = False
    for name, fn in [
        ("base_url", test_deye_base_url),
        ("device_list_filter", test_get_device_list_filters_inverters),
        ("fetch_device_data", test_fetch_device_data_maps_keys),
        ("fetch_battery_config_success", test_fetch_battery_config_caches_on_success),
        ("fetch_battery_config_failure", test_fetch_battery_config_failure_returns_empty),
        ("run_first_cycle", test_run_first_cycle_publishes_and_configures),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

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
from deye_const import DEYE_BASE_URLS, DEYE_TELEMETRY_KEYS, CONFIG_BATTERY_KEYS
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
        self.station_ids = []
        self.device_values = {}
        self.device_battery_config = {}
        self.device_capacity = {}
        self.device_pack_voltage = {}
        self.device_energy = {}
        self.device_rated_power = {}
        self.battery_nominal_voltage = 0.0
        self.local_schedule = {}
        self.pending_orders = {}
        self.order_poll_count = {}
        self.applied_payload = {}
        self.control_active = set()
        self.cached_values = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        self._soc_floor_warned = set()
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-deye-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.minutes_now = 0  # local minutes-since-midnight; tests set this for time-aware control
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
    # grid_power is negated into Predbat's convention on the way through
    if out.get("grid_power") != 1200.0:
        print(f"ERROR: grid_power {out.get('grid_power')}")
        failed = True
    if d.device_values.get("INV1", {}).get("soc") != 57.0:
        print("ERROR: not cached")
        failed = True
    assert not failed, "test_fetch_device_data_maps_keys"


# Trimmed verbatim from a live device/latest response (3-phase hybrid, productId
# 0_5411_1). Built from the real keys, NOT from DEYE_TELEMETRY_KEYS, so it fails if the
# key table drifts back to guessed spellings — the earlier map missed every one of these
# and published 0.0 across the board.
LIVE_DATA_LIST = [
    {"key": "RatedPower", "value": "8000.00", "unit": "W"},
    {"key": "TotalSolarPower", "value": "1584.00", "unit": "W"},
    {"key": "TotalGridPower", "value": "25", "unit": "W"},
    {"key": "TotalConsumptionPower", "value": "820", "unit": "W"},
    {"key": "TotalEnergyBuy", "value": "13579.10", "unit": "kWh"},
    {"key": "TotalEnergySell", "value": "11.70", "unit": "kWh"},
    {"key": "TotalActiveProduction", "value": "2198.10", "unit": "kWh"},
    {"key": "TotalConsumption", "value": "14326.40", "unit": "kWh"},
    {"key": "DailyActiveProduction", "value": "4.50", "unit": "kWh"},
    {"key": "DailyEnergyPurchased", "value": "7.00", "unit": "kWh"},
    {"key": "DailyGridFeedIn", "value": "0.00", "unit": "kWh"},
    {"key": "DailyConsumption", "value": "12.80", "unit": "kWh"},
    {"key": "DailyChargingEnergy", "value": "2.80", "unit": "kWh"},
    {"key": "DailyDischargingEnergy", "value": "4.10", "unit": "kWh"},
    {"key": "BatteryVoltage", "value": "53.83", "unit": "V"},
    {"key": "BatteryPower", "value": "-603", "unit": "W"},
    {"key": "SOC", "value": "94", "unit": "%"},
    {"key": "BatteryRatedCapacity", "value": "1200", "unit": "Ah"},
    {"key": "BMSChargeVoltage", "value": "57.60", "unit": "V"},
    {"key": "Temperature- Battery", "value": "27.90", "unit": "℃"},
    {"key": "AC Temperature", "value": "51.60", "unit": "℃"},
]


def test_fetch_device_data_against_live_payload():
    """Every telemetry metric resolves against a real DEYE response, in Predbat's sign convention."""
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return the captured live device/latest payload."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        out = run_async_local(d.fetch_device_data("INV1"))

    expected = {
        "soc": 94.0,
        "battery_power": -603.0,  # DEYE negative while charging == Predbat negative for charge
        "grid_power": -25.0,  # DEYE +25 importing negated to Predbat's negative-for-import
        "pv_power": 1584.0,
        "load_power": 820.0,
        "temperature": 27.9,
    }
    for name, want in expected.items():
        if abs(out.get(name, 0.0) - want) > 0.001:
            print(f"ERROR: {name} expected {want}, got {out.get(name)}")
            failed = True
    if any("missing expected keys" in m for m in d.log_messages):
        print(f"ERROR: unexpected missing-key warning: {d.log_messages}")
        failed = True
    assert not failed, "test_fetch_device_data_against_live_payload"


def test_derive_battery_capacity_from_live_payload():
    """Battery capacity is derived from the Ah rating and BMS charge voltage."""
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return the captured live device/latest payload."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async_local(d.fetch_device_data("INV1"))

    # 1200 Ah, BMS charge 57.6 V -> 16 cells -> 51.2 V nominal -> 61.44 kWh
    if abs(d.device_capacity.get("INV1", 0) - 61.44) > 0.01:
        print(f"ERROR: expected 61.44 kWh, got {d.device_capacity.get('INV1')}")
        failed = True
    if abs(d.battery_capacity("INV1") - 61.44) > 0.01:
        print(f"ERROR: battery_capacity returned {d.battery_capacity('INV1')}")
        failed = True
    assert not failed, "test_derive_battery_capacity_from_live_payload"


def test_nominal_pack_voltage_variants():
    """Cell count comes from the BMS charge target; there is no guessed default."""
    failed = False
    d = MockDeye()
    cases = [
        (57.6, 51.2, "16S from BMS charge voltage"),
        (28.8, 25.6, "8S from BMS charge voltage"),
        (403.2, 358.4, "112S HV pack scales with no configuration"),
        (0.0, 0.0, "no BMS charge voltage means no safe inference"),
    ]
    for bms, want, label in cases:
        got = d.nominal_pack_voltage(bms)
        if abs(got - want) > 0.01:
            print(f"ERROR: {label}: expected {want}, got {got}")
            failed = True
    # An explicit override wins over everything, including a reported charge voltage
    d.battery_nominal_voltage = 102.4
    if d.nominal_pack_voltage(57.6) != 102.4:
        print(f"ERROR: configured nominal should win, got {d.nominal_pack_voltage(57.6)}")
        failed = True
    if d.nominal_pack_voltage(0.0) != 102.4:
        print(f"ERROR: configured nominal should apply without a charge voltage, got {d.nominal_pack_voltage(0.0)}")
        failed = True
    assert not failed, "test_nominal_pack_voltage_variants"


def test_no_capacity_guessed_without_pack_voltage():
    """Without a BMS charge voltage no capacity is invented, and the warning names the fix.

    A guessed soc_max is worse than none: it becomes nominal_capacity, which sets the
    plausibility band that filters Predbat's own find_battery_size() estimator, so a wrong
    value also rejects every correct sample. Leaving it unset keeps that band disabled.
    """
    failed = False
    d = MockDeye()
    flat = {"BatteryRatedCapacity": "1200", "BatteryVoltage": "58.0"}
    if d.derive_battery_capacity("INV1", flat) != 0.0:
        print("ERROR: capacity must not be derived from the resting pack voltage")
        failed = True
    if "INV1" in d.device_capacity:
        print("ERROR: no capacity should be cached")
        failed = True
    warning = [m for m in d.log_messages if "cannot be derived" in m]
    if not warning:
        print(f"ERROR: expected a warning, got {d.log_messages}")
        failed = True
    elif "deye_battery_nominal_voltage" not in warning[0]:
        print(f"ERROR: the warning should name the setting that fixes it: {warning[0]}")
        failed = True
    # With the override supplied the same payload does resolve
    d.battery_nominal_voltage = 51.2
    if abs(d.derive_battery_capacity("INV1", flat) - 61.44) > 0.01:
        print(f"ERROR: expected 61.44 kWh with the override, got {d.derive_battery_capacity('INV1', flat)}")
        failed = True
    assert not failed, "test_no_capacity_guessed_without_pack_voltage"


def test_battery_capacity_scales_config_battery_amp_hours():
    """config/battery reports battCapacity in Ah and must be scaled, not published as kWh.

    Regression: the live response is {"battCapacity": 1200} for the same pack device/latest
    reports as BatteryRatedCapacity 1200 Ah. Returning it raw told Predbat the battery was
    1200 kWh.
    """
    failed = False
    d = MockDeye()
    d.device_capacity["INV1"] = 61.44
    d.device_pack_voltage["INV1"] = 51.2
    d.device_battery_config["INV1"] = {CONFIG_BATTERY_KEYS["capacity"]: 1200}
    got = d.battery_capacity("INV1")
    if abs(got - 61.44) > 0.01:
        print(f"ERROR: expected 1200Ah scaled to 61.44 kWh, got {got}")
        failed = True
    # A config response that carries no usable capacity falls back to the derived value
    d.device_battery_config["INV1"] = {}
    if abs(d.battery_capacity("INV1") - 61.44) > 0.01:
        print(f"ERROR: expected the derived 61.44, got {d.battery_capacity('INV1')}")
        failed = True
    # Without a known pack voltage there is nothing to scale by, so use the derived value
    d.device_battery_config["INV1"] = {CONFIG_BATTERY_KEYS["capacity"]: 1200}
    d.device_pack_voltage["INV1"] = 0.0
    if abs(d.battery_capacity("INV1") - 61.44) > 0.01:
        print(f"ERROR: expected the derived value without a pack voltage, got {d.battery_capacity('INV1')}")
        failed = True
    assert not failed, "test_battery_capacity_scales_config_battery_amp_hours"


def test_battery_rate_max_from_charge_current():
    """battery_rate_max is maxChargeCurrent scaled to watts by the nominal pack voltage."""
    failed = False
    d = MockDeye()
    d.device_pack_voltage["INV1"] = 51.2
    d.device_battery_config["INV1"] = {CONFIG_BATTERY_KEYS["max_charge_current"]: 185}
    # 185 A at 51.2 V nominal = 9472 W. Higher than the 8000 W AC rating on purpose: this
    # is the DC battery-side limit, and inverter_limit constrains the AC side separately.
    if d.battery_rate_max("INV1") != 9472:
        print(f"ERROR: expected 9472 W, got {d.battery_rate_max('INV1')}")
        failed = True
    # No pack voltage means nothing to scale by
    d.device_pack_voltage["INV1"] = 0.0
    if d.battery_rate_max("INV1") != 0.0:
        print(f"ERROR: expected 0.0 without a pack voltage, got {d.battery_rate_max('INV1')}")
        failed = True
    # No config/battery response at all
    d.device_pack_voltage["INV1"] = 51.2
    d.device_battery_config.pop("INV1")
    if d.battery_rate_max("INV1") != 0.0:
        print(f"ERROR: expected 0.0 without config/battery, got {d.battery_rate_max('INV1')}")
        failed = True
    assert not failed, "test_battery_rate_max_from_charge_current"


def test_rated_power_captured_for_inverter_limit():
    """RatedPower is captured from device/latest for Predbat's inverter_limit, in watts."""
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return the captured live device/latest payload."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async_local(d.fetch_device_data("INV1"))

    if d.device_rated_power.get("INV1") != 8000.0:
        print(f"ERROR: expected 8000.0 W, got {d.device_rated_power.get('INV1')}")
        failed = True
    assert not failed, "test_rated_power_captured_for_inverter_limit"


def test_rated_power_survives_a_payload_that_omits_it():
    """A later payload without RatedPower must not wipe a rating captured earlier.

    Coercing an absent key to 0.0 would stop publish_data() emitting the inverter_limit
    sensor and stop automatic_config() mapping it, even though the device had reported the
    rating on a previous cycle. DEYE responses are demonstrably not always complete —
    config/battery returned "config point not supported" transiently on this same site.
    """
    failed = False
    d = MockDeye()
    payloads = [LIVE_DATA_LIST, [{"key": "SOC", "value": "50"}]]

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: full payload first, then one missing RatedPower."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": payloads.pop(0)}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async_local(d.fetch_device_data("INV1"))
        if d.device_rated_power.get("INV1") != 8000.0:
            print(f"ERROR: first poll should capture 8000.0, got {d.device_rated_power.get('INV1')}")
            failed = True
        run_async_local(d.fetch_device_data("INV1"))

    if d.device_rated_power.get("INV1") != 8000.0:
        print(f"ERROR: rating was clobbered by the incomplete payload, got {d.device_rated_power.get('INV1')}")
        failed = True
    # A device that has never reported it still records nothing, so no bogus sensor
    d2 = MockDeye()

    async def fake_post_never(endpoint_key, body):
        """Fake DEYE POST: a payload that never carries RatedPower."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV2", "dataList": [{"key": "SOC", "value": "50"}]}]}

    with patch.object(d2, "_post", side_effect=fake_post_never):
        run_async_local(d2.fetch_device_data("INV2"))
    if "INV2" in d2.device_rated_power:
        print(f"ERROR: expected no rating recorded, got {d2.device_rated_power.get('INV2')}")
        failed = True
    assert not failed, "test_rated_power_survives_a_payload_that_omits_it"


def test_energy_counters_use_the_daily_registers():
    """The energy counters read the Daily registers, not the drifting lifetime accumulators.

    The lifetime "Total*" accumulators are firmware-derived and drift. On a live capture
    TotalConsumption read 14332.40 kWh where the other lifetime counters implied 15475.00
    (buy 13579.20 + PV 2208.30 - sell 11.80 + discharge 5255.90 - charge 5556.60), a 7.4%
    shortfall that made Predbat's learned load too low and under-sized every charge window.
    The Daily registers balanced exactly on that same payload - see
    test_daily_energy_registers_balance_on_the_live_payload.
    """
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return the captured live device/latest payload."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": LIVE_DATA_LIST}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async_local(d.fetch_device_data("INV1"))

    energy = d.device_energy.get("INV1", {})
    # The Daily* values from LIVE_DATA_LIST, not the Total* ones (14326.40, 13579.10, ...).
    expected = {"import_today": 7.0, "export_today": 0.0, "pv_today": 4.5, "load_today": 12.8}
    for name, want in expected.items():
        got = energy.get(name)
        if got is None or abs(got - want) > 0.01:
            print(f"ERROR: {name} expected {want} (daily register), got {got}")
            failed = True
    assert not failed, "test_energy_counters_use_the_daily_registers"


def test_daily_energy_registers_balance_on_the_live_payload():
    """The Daily registers are self-consistent, which is why load_today trusts them.

    Guards the key map as a set: if any of these five names drifts to a wrong spelling the
    balance breaks, which is the same failure that hid the original TotalConsumption bug.
    """
    failed = False
    flat = {item["key"]: float(item["value"]) for item in LIVE_DATA_LIST}

    derived = flat["DailyActiveProduction"] + flat["DailyEnergyPurchased"] - flat["DailyGridFeedIn"] + flat["DailyDischargingEnergy"] - flat["DailyChargingEnergy"]
    if abs(derived - flat["DailyConsumption"]) > 0.01:
        print(f"ERROR: daily energy balance {derived} != DailyConsumption {flat['DailyConsumption']}")
        failed = True
    assert not failed, "test_daily_energy_registers_balance_on_the_live_payload"


def test_energy_counters_absent_are_not_invented():
    """A device that reports no energy counters records none, rather than zeros."""
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: a payload with telemetry but no energy counters."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": [{"key": "SOC", "value": "50"}]}]}

    with patch.object(d, "_post", side_effect=fake_post):
        run_async_local(d.fetch_device_data("INV1"))

    if d.device_energy.get("INV1"):
        print(f"ERROR: expected no energy counters, got {d.device_energy.get('INV1')}")
        failed = True
    assert not failed, "test_energy_counters_absent_are_not_invented"


def test_derive_battery_capacity_without_rating():
    """No Ah rating means no derived capacity, and no bogus zero-capacity sensor."""
    failed = False
    d = MockDeye()
    if d.derive_battery_capacity("INV1", {"BatteryVoltage": "53.83"}) != 0.0:
        print("ERROR: expected 0.0 without a rated capacity")
        failed = True
    if "INV1" in d.device_capacity:
        print("ERROR: should not cache a capacity that could not be derived")
        failed = True
    if d.battery_capacity("INV1") != 0.0:
        print(f"ERROR: expected 0.0, got {d.battery_capacity('INV1')}")
        failed = True
    assert not failed, "test_derive_battery_capacity_without_rating"


def test_fetch_device_data_warns_on_missing_keys():
    """A response without the expected keys warns instead of silently publishing zeros."""
    failed = False
    d = MockDeye()

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return a payload using the old, wrong key spellings."""
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": [{"key": "batterySOC", "value": "57"}]}]}

    with patch.object(d, "_post", side_effect=fake_post):
        out = run_async_local(d.fetch_device_data("INV1"))
    if out.get("soc") != 0.0:
        print(f"ERROR: expected 0.0 for an unmatched key, got {out.get('soc')}")
        failed = True
    warning = [m for m in d.log_messages if "missing expected keys" in m]
    if not warning:
        print(f"ERROR: expected a missing-key warning, got {d.log_messages}")
        failed = True
    elif "batterySOC" not in warning[0]:
        print(f"ERROR: warning should name the keys the device did return: {warning[0]}")
        failed = True
    assert not failed, "test_fetch_device_data_warns_on_missing_keys"


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
        ("fetch_device_data_live_payload", test_fetch_device_data_against_live_payload),
        ("fetch_device_data_missing_keys", test_fetch_device_data_warns_on_missing_keys),
        ("derive_battery_capacity", test_derive_battery_capacity_from_live_payload),
        ("nominal_pack_voltage", test_nominal_pack_voltage_variants),
        ("no_capacity_guess", test_no_capacity_guessed_without_pack_voltage),
        ("battery_capacity_amp_hours", test_battery_capacity_scales_config_battery_amp_hours),
        ("battery_rate_max", test_battery_rate_max_from_charge_current),
        ("rated_power_captured", test_rated_power_captured_for_inverter_limit),
        ("rated_power_not_clobbered", test_rated_power_survives_a_payload_that_omits_it),
        ("energy_counters_daily_registers", test_energy_counters_use_the_daily_registers),
        ("daily_energy_balance", test_daily_energy_registers_balance_on_the_live_payload),
        ("energy_counters_absent", test_energy_counters_absent_are_not_invented),
        ("derive_capacity_no_rating", test_derive_battery_capacity_without_rating),
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

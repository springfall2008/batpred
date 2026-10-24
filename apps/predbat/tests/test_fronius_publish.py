# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fronius publishing, automatic configuration, discovery and lifecycle
# -----------------------------------------------------------------------------

"""Tests for the Fronius component's published entities, automatic_config, discovery record and run()."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from coordinator import CAPABILITY_KEYS
from fronius import FroniusCloud, FRONIUS_CAPABILITIES
from fronius_const import FRONIUS_CACHE_STATIC, FRONIUS_STORAGE_MODULE
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_infra import run_async
from tests.test_fronius_api import ENERGY, DEVICES, FLOW, KEY_ID, KEY_VALUE, PV_ID, SHORT_ID, FakeResponse, FakeStorage, MockFronius, http, logged, telemetry_client


def sensor(leaf):
    """Return a Fronius sensor entity id for the test system."""
    return "sensor.predbat_fronius_{}_{}".format(SHORT_ID, leaf)


def test_fronius_publish_sensors():
    """Telemetry, ratings and daily energy are published in Predbat's units and signs."""
    client = telemetry_client()
    run_async(client.publish_data())
    expected = {
        "soc": 55,
        "battery_power": -1200,
        "grid_power": 1500,
        "load_power": 800,
        "pv_power": 3500,
        "battery_capacity": 13.824,
        "battery_rate_max": 5632,
        "inverter_limit": 10000,
        "load_today": 9.0,
        "import_today": 5.0,
        "export_today": 6.5,
        "pv_today": 15.0,
    }
    for leaf, value in expected.items():
        published = client.published.get(sensor(leaf))
        assert published is not None, "{} not published".format(leaf)
        assert published["state"] == value, (leaf, published["state"])
    assert client.published[sensor("import_today")]["attributes"]["unit_of_measurement"] == "kWh"
    assert client.published[sensor("grid_power")]["attributes"]["unit_of_measurement"] == "W"


def test_fronius_publish_omits_unreported_values():
    """A value the system never reported is not published as a fabricated zero."""
    client = MockFronius()
    run_async(client.publish_data())
    for leaf in ("soc", "battery_power", "pv_power", "battery_capacity", "load_today"):
        assert sensor(leaf) not in client.published, leaf
    assert client.published[sensor("status")]["state"] == "unknown"


def test_fronius_status_sensor_carries_no_secrets():
    """The status sensor explains the API state and the schedule, and never the keys."""
    client = telemetry_client()
    run_async(client.publish_data())
    status = client.published[sensor("status")]
    assert status["state"] == "online"
    attributes = status["attributes"]
    assert attributes["pv_system_id"] == PV_ID and attributes["data_points_today"] == 8
    rendered = repr(attributes)
    assert KEY_ID not in rendered and KEY_VALUE not in rendered


def test_fronius_control_entities_published_with_safe_defaults():
    """Control entities exist from the first tick, and the default power is not a zero (a hold)."""
    client = telemetry_client()
    run_async(client.publish_schedule_settings_ha())
    power = client.published["number.predbat_fronius_{}_battery_schedule_export_power".format(SHORT_ID)]
    assert power["state"] == 5632, power
    assert client.published["select.predbat_fronius_{}_battery_schedule_charge_start_time".format(SHORT_ID)]["state"] == "00:00:00"
    assert client.published["switch.predbat_fronius_{}_battery_schedule_charge_write".format(SHORT_ID)]["state"] == "off"
    # Unknown battery limit: still not zero.
    bare = MockFronius()
    run_async(bare.publish_schedule_settings_ha())
    assert bare.published["number.predbat_fronius_{}_battery_schedule_charge_power".format(SHORT_ID)]["state"] > 0


def test_fronius_automatic_config_binds_sensors_and_controls():
    """Every arg Predbat needs is bound; there is no reserve, and the sign flags are owned."""
    client = telemetry_client(automatic=True)
    run_async(client.automatic_config())
    args = client.base.args
    assert args.get("inverter_type") == ["FroniusCloud"] and args.get("num_inverters") == 1
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "battery_rate_max", "inverter_limit", "load_today", "import_today", "export_today", "pv_today"):
        assert args.get(arg), "{} was not bound".format(arg)
    for arg in (
        "charge_start_time",
        "charge_end_time",
        "charge_limit",
        "charge_rate",
        "scheduled_charge_enable",
        "discharge_start_time",
        "discharge_end_time",
        "discharge_target_soc",
        "discharge_rate",
        "scheduled_discharge_enable",
        "schedule_write_button",
    ):
        assert args.get(arg), "control arg {} was not bound".format(arg)
    assert "reserve" not in args, "the API cannot set a reserve, so none may be bound"
    assert "export_limit" not in args
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        assert args.get(flag) == [False], (flag, args.get(flag))


def test_fronius_automatic_config_respects_ignore_pv():
    """automatic_ignore_pv leaves pv_power and pv_today unbound."""
    client = telemetry_client(automatic=True, automatic_ignore_pv=True)
    run_async(client.automatic_config())
    assert "pv_power" not in client.base.args and "pv_today" not in client.base.args


def test_fronius_discovery_record_rebuilds_the_row_and_agrees_with_automatic_config():
    """The record validates, rebuilds INVERTER_DEF, and binds exactly what automatic_config() binds."""
    client = telemetry_client(automatic=True)
    report = client.build_discovery()
    assert report is not None
    records = validated_inverters(report)
    record = records[0]
    assert record["inverter_type"] == "FroniusCloud"
    for key in CAPABILITY_KEYS:
        assert record["capabilities"].get(key) == FRONIUS_CAPABILITIES[key], key
    assert record["ratings"]["soc_max"] == 13.824 and "solar" in record["functions"]
    assert_definition_complete(record, FroniusCloud.WRITE_AND_POLL_SLEEP)
    captured = capture_automatic_config(client)
    assert_record_agrees(record, captured)
    assert_record_binds_nothing_extra(record, captured)


def test_fronius_discovery_record_is_none_before_data():
    """Nothing read yet means nothing to report."""
    assert MockFronius().build_discovery() is None


def test_fronius_run_requires_the_contract_keys():
    """Missing keys or system id are named and the component stays in startup backoff."""
    client = MockFronius(access_key_value="", pv_system_id="")
    transport, patcher = http()
    with patcher:
        assert run_async(client.run(0, True)) is False
    assert transport.calls == []
    assert logged(client, "fronius_access_key_value") and logged(client, "fronius_pv_system_id")


def test_fronius_run_defers_startup_without_telemetry():
    """automatic_config() runs on the first cycle alone, so a cold start without telemetry defers."""
    client = MockFronius(automatic=True)
    transport, patcher = http(FakeResponse(200, DEVICES), FakeResponse(500, {"responseError": 1001}), FakeResponse(200, ENERGY))
    with patcher:
        assert run_async(client.run(0, True)) is False
    assert "soc_max" not in client.base.args and client.success_updates == 0


def test_fronius_run_first_cycle_configures_and_reads_once():
    """A good first cycle reads each tier once, binds the args and stamps success; the next tick reads nothing."""
    client = MockFronius(automatic=True)
    transport, patcher = http(FakeResponse(200, DEVICES), FakeResponse(200, FLOW), FakeResponse(200, ENERGY))
    with patcher:
        assert run_async(client.run(0, True)) is True
        client.advance(minutes=1)
        assert run_async(client.run(60, False)) is True
    assert len(transport.calls) == 3, [call["url"] for call in transport.calls]
    assert client.base.args.get("inverter_type") == ["FroniusCloud"]
    assert client.success_updates == 2


def test_fronius_run_reads_power_once_per_cycle():
    """Power flow is read at most once per five-minute Predbat cycle; energy every energy_interval."""
    client = MockFronius()
    transport, patcher = http(FakeResponse(200, DEVICES), FakeResponse(200, FLOW), FakeResponse(200, ENERGY))
    with patcher:
        run_async(client.run(0, True))
        for minute in range(1, 31):
            client.advance(minutes=1)
            if minute % 5 == 0:
                transport.queue(FakeResponse(200, FLOW))
            if minute == 30:
                transport.queue(FakeResponse(200, ENERGY))
            run_async(client.run(minute * 60, False))
    paths = [call["url"].rsplit("/", 1)[-1] for call in transport.calls]
    assert paths.count("flowdata") == 7, paths
    assert paths.count("aggrdata") == 2, paths


def test_fronius_stale_telemetry_is_unhealthy():
    """Success is only stamped while telemetry is fresh, so a dead data feed surfaces as unhealthy."""
    client = telemetry_client()
    client._tier_refreshed = {"static": client._epoch(), "energy": client._epoch()}
    client.advance(minutes=20)
    client.mark_refreshed("power")
    client.mark_refreshed("energy")
    transport, patcher = http()
    with patcher:
        assert run_async(client.run(0, False)) is True
    assert client.success_updates == 0


def test_fronius_health_message():
    """The run status names a rejected key or a missing control permission."""
    client = MockFronius()
    assert client.health_message() is None
    client.auth_failed["control"] = True
    assert "contract" in client.health_message()
    client.auth_failed["query"] = True
    assert "rejected" in client.health_message()


def test_fronius_static_cache_round_trip():
    """Device metadata survives a restart; a cache for another PV system is ignored."""
    storage = FakeStorage()
    client = telemetry_client(storage=storage)
    run_async(client.save_static())
    restored = MockFronius(storage=storage)
    run_async(restored.restore_state())
    assert restored.battery_capacity() == 13.824 and not restored.tier_expired("static", 24 * 60)
    other = MockFronius(storage=storage, pv_system_id="ffffffff-0000-0000-0000-000000000000")
    run_async(other.restore_state())
    assert other.battery_info == {}
    assert (FRONIUS_STORAGE_MODULE, FRONIUS_CACHE_STATIC) in storage.data


def run_fronius_publish_tests(my_predbat):
    """Run all Fronius publish/config/lifecycle tests."""
    failed = False
    for name, fn in [
        ("publish_sensors", test_fronius_publish_sensors),
        ("publish_omits_missing", test_fronius_publish_omits_unreported_values),
        ("status_no_secrets", test_fronius_status_sensor_carries_no_secrets),
        ("control_defaults", test_fronius_control_entities_published_with_safe_defaults),
        ("automatic_config", test_fronius_automatic_config_binds_sensors_and_controls),
        ("automatic_ignore_pv", test_fronius_automatic_config_respects_ignore_pv),
        ("discovery_record", test_fronius_discovery_record_rebuilds_the_row_and_agrees_with_automatic_config),
        ("discovery_none", test_fronius_discovery_record_is_none_before_data),
        ("run_requires_keys", test_fronius_run_requires_the_contract_keys),
        ("run_defers", test_fronius_run_defers_startup_without_telemetry),
        ("run_first_cycle", test_fronius_run_first_cycle_configures_and_reads_once),
        ("run_read_cadence", test_fronius_run_reads_power_once_per_cycle),
        ("stale_unhealthy", test_fronius_stale_telemetry_is_unhealthy),
        ("health_message", test_fronius_health_message),
        ("static_cache", test_fronius_static_cache_round_trip),
    ]:
        try:
            if fn():
                print(f"  FAILED: fronius_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in fronius_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

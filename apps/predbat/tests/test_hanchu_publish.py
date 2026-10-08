# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Hanchu ESS cloud publishing, automatic_config and discovery
# -----------------------------------------------------------------------------

"""Tests for what the Hanchu component publishes, binds and reports to the catalogue."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF
from coordinator import CAPABILITY_KEYS
from hanchu import HANCHU_CAPABILITIES
from hanchu_const import HANCHU_KEY_CHARGE_END, HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_CHARGE_START
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_hanchu_api import MockHanchu, STATISTICS_SAMPLE, STATUS_SAMPLE
from tests.test_infra import run_async as run_async_local

SN = "HC240100001"


def populated(automatic=True, automatic_ignore_pv=False):
    """Return a MockHanchu holding one fully polled device, as run() would after a good cycle."""
    client = MockHanchu(automatic=automatic, automatic_ignore_pv=automatic_ignore_pv)
    client.device_list = [SN]
    client._apply_status_payload(SN, STATUS_SAMPLE)
    client.device_energy[SN] = {
        "load_today": STATISTICS_SAMPLE["load"],
        "import_today": STATISTICS_SAMPLE["gridImport"],
        "export_today": STATISTICS_SAMPLE["gridExport"],
        "pv_today": STATISTICS_SAMPLE["pv"],
        "battery_charge_today": STATISTICS_SAMPLE["batCharge"],
        "battery_discharge_today": STATISTICS_SAMPLE["batDisCharge"],
    }
    client.device_settings[SN] = {"WORK_MODE_CMB": "3", HANCHU_KEY_CHARGE_START.format(1): 5400, HANCHU_KEY_CHARGE_END.format(1): 16200}
    return client


def test_hanchu_publish_data_emits_the_sensors_automatic_config_binds():
    """Every sensor the arg bindings point at must actually be published."""
    failed = False
    client = populated()
    run_async_local(client.publish_data())
    for leaf, state in (("soc", 62.0), ("battery_power", -2400.0), ("grid_power", -1500.0), ("pv_power", 3100.0), ("load_power", 900.0)):
        entity = client._sensor_name(SN, leaf)
        if entity not in client.published:
            print(f"ERROR: {entity} was not published")
            failed = True
        elif client.published[entity]["state"] != state:
            print(f"ERROR: {entity} = {client.published[entity]['state']} != {state}")
            failed = True
    for leaf in ("load_today", "import_today", "export_today", "pv_today"):
        entity = client._sensor_name(SN, leaf)
        if entity not in client.published:
            print(f"ERROR: {entity} was not published")
            failed = True
        elif client.published[entity]["attributes"].get("unit_of_measurement") != "kWh":
            print(f"ERROR: {entity} is not published in kWh")
            failed = True
    # Ratings.
    if client.published.get(client._sensor_name(SN, "battery_capacity"), {}).get("state") != 10.24:
        print("ERROR: the battery capacity rating was not published")
        failed = True
    for leaf in ("battery_rate_max", "inverter_limit"):
        if client._sensor_name(SN, leaf) not in client.published:
            print(f"ERROR: {leaf} was not published")
            failed = True
    # The work mode is the correctness of the whole schedule, so a tester has to be able to see it,
    # decoded and with the raw value alongside.
    work_mode = client.published.get(client._sensor_name(SN, "work_mode"))
    if not work_mode or work_mode["state"] != "User-defined" or work_mode["attributes"].get("raw") != "3":
        print(f"ERROR: the work mode sensor is {work_mode}")
        failed = True
    assert not failed, "test_hanchu_publish_data_emits_the_sensors_automatic_config_binds"


def test_hanchu_ratings_are_published_only_when_derivable():
    """An arg pointing at a sensor that never appears is worse than an absent arg."""
    failed = False
    client = MockHanchu()
    client.device_list = [SN]
    # A status payload with no capacity field at all.
    client._apply_status_payload(SN, {"batSoc": 0.5, "loadPwr": 500})
    run_async_local(client.publish_data())
    if client._sensor_name(SN, "battery_capacity") in client.published:
        print("ERROR: a capacity sensor was published although the device reported none")
        failed = True
    if client._sensor_name(SN, "station_id") in client.published:
        print("ERROR: a station sensor was published although the device reported none")
        failed = True
    # And the sensors that ARE derivable still appear, so this is not passing for the wrong reason.
    if client._sensor_name(SN, "soc") not in client.published:
        print("ERROR: the SoC sensor should still be published")
        failed = True
    assert not failed, "test_hanchu_ratings_are_published_only_when_derivable"


def test_hanchu_device_slots_are_published_as_readable_times():
    """The slot table as the DEVICE holds it, in HH:MM:SS with the raw seconds alongside.

    This is the diagnostic that separates "the window was never written" from "the window was
    written and the inverter ignored it" - the exact failure the inferred work mode would cause.
    """
    failed = False
    client = populated()
    run_async_local(client.publish_data())
    start = client.published.get(client._sensor_name(SN, "device_charge_slot_1_start"))
    if not start or start["state"] != "01:30:00" or start["attributes"].get("raw") != 5400:
        print(f"ERROR: the device charge slot 1 start sensor is {start}")
        failed = True
    end = client.published.get(client._sensor_name(SN, "device_charge_slot_1_end"))
    if not end or end["state"] != "04:30:00":
        print(f"ERROR: the device charge slot 1 end sensor is {end}")
        failed = True
    # A slot the device did not report is skipped, not published as zero - publishing zero would
    # claim it is disabled when the truth is that it was not read.
    if client._sensor_name(SN, "device_charge_slot_2_start") in client.published:
        print("ERROR: an unreported slot was published as though it were disabled")
        failed = True
    assert not failed, "test_hanchu_device_slots_are_published_as_readable_times"


def test_hanchu_schedule_controls_advertise_the_narrow_soc_ranges():
    """The min/max on the published numbers tell the user what this hardware will actually take."""
    failed = False
    client = populated()
    run_async_local(client.publish_schedule_settings_ha(SN))
    charge_soc = client.published.get(client._control_name("number", SN, "battery_schedule_charge_soc"))
    if not charge_soc or (charge_soc["attributes"].get("min"), charge_soc["attributes"].get("max")) != (50, 100):
        print(f"ERROR: the charge SoC control advertises {charge_soc['attributes'] if charge_soc else None}")
        failed = True
    for leaf in ("battery_schedule_export_soc", "battery_schedule_reserve"):
        control = client.published.get(client._control_name("number", SN, leaf))
        if not control or (control["attributes"].get("min"), control["attributes"].get("max")) != (5, 45):
            print(f"ERROR: {leaf} advertises {control['attributes'] if control else None}, expected 5-45")
            failed = True
    # The time entities must be HH:MM:SS, or inverter.py replaces them with its own dummies and the
    # window never reaches the component.
    for leaf in ("battery_schedule_charge_start_time", "battery_schedule_export_end_time"):
        control = client.published.get(client._control_name("select", SN, leaf))
        if not control or len(str(control["state"]).split(":")) != 3:
            print(f"ERROR: {leaf} published {control['state'] if control else None}, not HH:MM:SS")
            failed = True
    # A menu-supplied range wins over the default here too.
    client.device_ranges[SN] = {HANCHU_KEY_CHARGE_POWER: (0, 7000)}
    run_async_local(client.publish_schedule_settings_ha(SN))
    power = client.published.get(client._control_name("number", SN, "battery_schedule_charge_power"))
    if not power or power["attributes"].get("max") != 7000:
        print(f"ERROR: the charge power control did not follow the menu range: {power['attributes'] if power else None}")
        failed = True
    assert not failed, "test_hanchu_schedule_controls_advertise_the_narrow_soc_ranges"


def test_hanchu_automatic_config_binds_the_expected_args():
    """automatic_config registers the inverter type and points every arg at a published entity."""
    failed = False
    client = populated()
    run_async_local(client.publish_data())
    run_async_local(client.publish_schedule_settings_ha(SN))
    run_async_local(client.automatic_config())
    args = client.base.args
    if args.get("inverter_type") != ["HanchuCloud"] or args.get("num_inverters") != 1:
        print(f"ERROR: inverter registration {args.get('inverter_type')} {args.get('num_inverters')}")
        failed = True
    # Every bound entity must be one that has actually been published, or Predbat points at nothing.
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "load_today", "import_today", "export_today", "pv_today", "soc_max", "battery_rate_max", "inverter_limit"):
        value = args.get(arg)
        if not value:
            print(f"ERROR: arg {arg} was not bound")
            failed = True
            continue
        if value[0] not in client.published:
            print(f"ERROR: arg {arg} points at {value[0]}, which was never published")
            failed = True
    for arg in (
        "reserve",
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
        value = args.get(arg)
        if not value or value[0] not in client.published:
            print(f"ERROR: control arg {arg} = {value}, which was never published")
            failed = True
    # The sign flags must be claimed, not inherited: base.args is shared and not namespaced per
    # inverter type, so a component that inverts its own grid sensor would otherwise leave that key
    # set for this one too and an export would read as an import.
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        if args.get(flag) != [False]:
            print(f"ERROR: {flag} = {args.get(flag)}, should be claimed as [False]")
            failed = True
    # export_limit is deliberately never bound - the API reports no grid connection cap.
    if "export_limit" in args:
        print("ERROR: export_limit must not be auto-bound; the API reports no export limit")
        failed = True
    assert not failed, "test_hanchu_automatic_config_binds_the_expected_args"


def test_hanchu_automatic_ignore_pv_skips_the_pv_args():
    """The opt-out is the user's choice, not a fact about the device."""
    failed = False
    client = populated(automatic_ignore_pv=True)
    run_async_local(client.publish_data())
    run_async_local(client.publish_schedule_settings_ha(SN))
    run_async_local(client.automatic_config())
    for arg in ("pv_power", "pv_today"):
        if arg in client.base.args:
            print(f"ERROR: {arg} was bound despite hanchu_automatic_ignore_pv")
            failed = True
    if "soc_percent" not in client.base.args:
        print("ERROR: the PV opt-out must not stop the other args being bound")
        failed = True
    assert not failed, "test_hanchu_automatic_ignore_pv_skips_the_pv_args"


def test_hanchu_energy_args_need_every_device_to_report():
    """Binding an arg for only some inverters would silently under-report the whole site."""
    failed = False
    client = populated()
    second = "HC240100002"
    client.device_list.append(second)
    client._apply_status_payload(second, STATUS_SAMPLE)
    # The second device reports no energy counters at all.
    run_async_local(client.publish_data())
    run_async_local(client.automatic_config())
    for arg in ("load_today", "import_today", "export_today", "pv_today"):
        if arg in client.base.args:
            print(f"ERROR: {arg} was bound although only one of two devices reports it")
            failed = True
    if not any("not every device reports" in message for message in client.log_messages):
        print("ERROR: the unbound energy args were not explained")
        failed = True
    assert not failed, "test_hanchu_energy_args_need_every_device_to_report"


def test_hanchu_capabilities_match_the_inverter_def_row():
    """The discovery record rebuilds the INVERTER_DEF row, so the two must agree exactly."""
    failed = False
    row = INVERTER_DEF["HanchuCloud"]
    for key in CAPABILITY_KEYS:
        # support_feedin_first is absent from the row on purpose, and inverter.py defaults it to
        # False, which is what the capability set must therefore claim.
        expected = row.get(key, False)
        if HANCHU_CAPABILITIES.get(key) != expected:
            print(f"ERROR: capability {key} = {HANCHU_CAPABILITIES.get(key)} != INVERTER_DEF's {expected}")
            failed = True
    extra = set(HANCHU_CAPABILITIES) - set(CAPABILITY_KEYS)
    if extra:
        print(f"ERROR: HANCHU_CAPABILITIES carries keys the catalogue does not know: {sorted(extra)}")
        failed = True
    assert not failed, "test_hanchu_capabilities_match_the_inverter_def_row"


def test_hanchu_discovery_record_is_complete_and_agrees_with_automatic_config():
    """The record must rebuild its INVERTER_DEF row and describe exactly what automatic_config binds."""
    failed = False
    client = populated()
    run_async_local(client.publish_data())
    run_async_local(client.publish_schedule_settings_ha(SN))
    captured = capture_automatic_config(client)
    report = client.build_discovery()
    if report is None:
        print("ERROR: build_discovery returned None for a fully polled device")
        assert False, "test_hanchu_discovery_record_is_complete_and_agrees_with_automatic_config"
    if report.get("automatic") is not True:
        print(f"ERROR: the report's automatic flag is {report.get('automatic')}")
        failed = True
    records = validated_inverters(report)
    if len(records) != 1:
        print(f"ERROR: expected one inverter record, got {len(records)}")
        assert False, "test_hanchu_discovery_record_is_complete_and_agrees_with_automatic_config"
    record = records[0]
    if record.get("device_id") != "hanchu:{}".format(SN):
        print(f"ERROR: device_id {record.get('device_id')}")
        failed = True
    if record.get("hardware_ids", {}).get("serial") != SN:
        print(f"ERROR: hardware_ids {record.get('hardware_ids')}")
        failed = True
    if record.get("account_ids", {}).get("station") != "ST-9001":
        print(f"ERROR: account_ids {record.get('account_ids')}")
        failed = True
    if sorted(record.get("functions") or []) != ["battery", "solar"]:
        print(f"ERROR: functions {record.get('functions')}")
        failed = True
    ratings = record.get("ratings") or {}
    if ratings.get("soc_max") != 10.24:
        print(f"ERROR: soc_max rating {ratings.get('soc_max')}")
        failed = True
    # The record alone has to rebuild the INVERTER_DEF row, or the completeness test proves nothing.
    assert_definition_complete(record, INVERTER_DEF["HanchuCloud"]["write_and_poll_sleep"])
    assert_record_agrees(record, captured)
    assert_record_binds_nothing_extra(record, captured)
    assert not failed, "test_hanchu_discovery_record_is_complete_and_agrees_with_automatic_config"


def test_hanchu_discovery_returns_none_before_discovery():
    """refresh_discovery treats None as 'nothing to report, ask again next cycle'."""
    failed = False
    client = MockHanchu()
    if client.build_discovery() is not None:
        print("ERROR: build_discovery should be None with no devices discovered")
        failed = True
    # A device with no telemetry yet claims battery only, never solar - "solar" is claimed on
    # observed PV, not on a nameplate this API never gives.
    client.device_list = [SN]
    record = (client.build_discovery() or {}).get("inverters", [{}])[0]
    if record.get("functions") != ["battery"]:
        print(f"ERROR: functions before any PV was seen = {record.get('functions')}")
        failed = True
    assert not failed, "test_hanchu_discovery_returns_none_before_discovery"


def run_hanchu_publish_tests(my_predbat):
    """Run all Hanchu publish, automatic_config and discovery tests."""
    failed = False
    for name, fn in [
        ("publish_data", test_hanchu_publish_data_emits_the_sensors_automatic_config_binds),
        ("ratings_conditional", test_hanchu_ratings_are_published_only_when_derivable),
        ("device_slots", test_hanchu_device_slots_are_published_as_readable_times),
        ("control_ranges", test_hanchu_schedule_controls_advertise_the_narrow_soc_ranges),
        ("automatic_config", test_hanchu_automatic_config_binds_the_expected_args),
        ("ignore_pv", test_hanchu_automatic_ignore_pv_skips_the_pv_args),
        ("energy_all_or_nothing", test_hanchu_energy_args_need_every_device_to_report),
        ("capabilities", test_hanchu_capabilities_match_the_inverter_def_row),
        ("discovery_record", test_hanchu_discovery_record_is_complete_and_agrees_with_automatic_config),
        ("discovery_none", test_hanchu_discovery_returns_none_before_discovery),
    ]:
        try:
            if fn():
                print(f"  FAILED: hanchu_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in hanchu_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

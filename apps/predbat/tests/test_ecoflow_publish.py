# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test EcoFlow publish and auto-configuration
# -----------------------------------------------------------------------------

"""Tests for the EcoFlow component's published sensors, automatic_config and discovery record."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from coordinator import CAPABILITY_KEYS, validate_report
from ecoflow_const import ECOFLOW_TELEMETRY_UNITS
from tests.test_ecoflow_api import MockEcoFlow
from tests.test_infra import run_async as run_async_local


def _loaded(client, serials=("SN1",), values=None, detail=None):
    """Put a client into the state a successful poll would have left it in."""
    client.device_list = list(serials)
    client.device_detail = {sn: dict(detail or {"sn": sn, "productName": "PowerOcean", "deviceName": "Home", "online": 1}) for sn in serials}
    resolved = values if values is not None else {"soc": 57.0, "battery_power": -1200.0, "grid_power": 800.0, "pv_power": 2400.0, "load_power": 1900.0}
    client.device_values = {sn: dict(resolved) for sn in serials}
    client.device_quota_keys = {sn: ["bpPwr", "bpSoc", "mpptPwr", "sysGridPwr", "sysLoadPwr"] for sn in serials}
    return client


def test_ecoflow_publishes_every_resolved_leaf_with_units():
    """Each resolved telemetry leaf publishes one sensor, carrying its unit."""
    failed = False
    client = _loaded(MockEcoFlow())
    run_async_local(client.publish_data())
    for leaf, unit in ECOFLOW_TELEMETRY_UNITS.items():
        entity = "sensor.predbat_ecoflow_sn1_{}".format(leaf)
        if entity not in client.published:
            print(f"ERROR: {entity} not published")
            failed = True
            continue
        if client.published[entity]["attributes"].get("unit_of_measurement") != unit:
            print(f"ERROR: {entity} unit {client.published[entity]['attributes'].get('unit_of_measurement')} != {unit}")
            failed = True
    assert not failed, "test_ecoflow_publishes_every_resolved_leaf_with_units"


def test_ecoflow_publishes_nothing_for_an_unresolved_leaf():
    """An unresolved leaf publishes NO sensor, rather than a zero.

    The whole contract with the unverified key table. A permanent 0 W reads exactly like a real
    measurement and would be wrong in every plan; an absent sensor is visibly absent.
    """
    failed = False
    client = _loaded(MockEcoFlow(), values={"soc": 44.0})
    run_async_local(client.publish_data())
    if "sensor.predbat_ecoflow_sn1_soc" not in client.published:
        print("ERROR: the resolved leaf should still publish")
        failed = True
    for leaf in ("battery_power", "grid_power", "pv_power", "load_power"):
        entity = "sensor.predbat_ecoflow_sn1_{}".format(leaf)
        if entity in client.published:
            print(f"ERROR: {entity} was published from nothing: {client.published[entity]}")
            failed = True
    assert not failed, "test_ecoflow_publishes_nothing_for_an_unresolved_leaf"


def test_ecoflow_publishes_the_quota_key_diagnostic():
    """The quota_keys sensor carries the device's real field names in an attribute.

    This is the diagnostic that makes the unverified key table fixable by a user rather than
    only by whoever can capture an API trace, so it is a tested contract, not a nicety. The
    names go in an attribute because the state field is length-limited and a Power Ocean quota
    runs to well over a hundred keys.
    """
    failed = False
    client = _loaded(MockEcoFlow())
    run_async_local(client.publish_data())
    entity = "sensor.predbat_ecoflow_sn1_quota_keys"
    if entity not in client.published:
        print("ERROR: quota_keys sensor not published")
        assert False, "test_ecoflow_publishes_the_quota_key_diagnostic"
    published = client.published[entity]
    if published["state"] != 5:
        print(f"ERROR: state {published['state']} should be the key count")
        failed = True
    if "bpSoc" not in published["attributes"].get("keys", []):
        print(f"ERROR: keys attribute {published['attributes'].get('keys')}")
        failed = True
    if sorted(published["attributes"].get("resolved", [])) != ["battery_power", "grid_power", "load_power", "pv_power", "soc"]:
        print(f"ERROR: resolved attribute {published['attributes'].get('resolved')}")
        failed = True
    assert not failed, "test_ecoflow_publishes_the_quota_key_diagnostic"


def test_ecoflow_ratings_publish_only_when_the_user_supplied_them():
    """battery_capacity and inverter_limit are published only from the user's own figures.

    No documented quota field carries either, so an unset arg must publish nothing at all - a
    sensor pointing at a guessed capacity is worse than an absent one the user can fill in.
    """
    failed = False
    client = _loaded(MockEcoFlow())
    run_async_local(client.publish_data())
    for leaf in ("battery_capacity", "inverter_limit"):
        if "sensor.predbat_ecoflow_sn1_{}".format(leaf) in client.published:
            print(f"ERROR: {leaf} published with no user figure")
            failed = True
    client = _loaded(MockEcoFlow(battery_capacity=10, inverter_limit=5000))
    run_async_local(client.publish_data())
    if client.published.get("sensor.predbat_ecoflow_sn1_battery_capacity", {}).get("state") != 10.0:
        print(f"ERROR: capacity {client.published.get('sensor.predbat_ecoflow_sn1_battery_capacity')}")
        failed = True
    if client.published.get("sensor.predbat_ecoflow_sn1_inverter_limit", {}).get("state") != 5000:
        print(f"ERROR: limit {client.published.get('sensor.predbat_ecoflow_sn1_inverter_limit')}")
        failed = True
    assert not failed, "test_ecoflow_ratings_publish_only_when_the_user_supplied_them"


def test_ecoflow_automatic_config_binds_monitoring_only():
    """automatic_config binds the monitoring args and NO control arg.

    Binding a control entity that no write can reach would leave Predbat believing it is driving
    the battery. inverter.py substitutes a visibly inert dummy for an unbound control, which is
    the safer failure by a wide margin - see the note on ECOFLOW_SCHEDULE_COMMANDS.
    """
    failed = False
    client = _loaded(MockEcoFlow(automatic=True, battery_capacity=10, inverter_limit=5000))
    run_async_local(client.automatic_config())
    args = client.base.args
    for arg, expected in [
        ("inverter_type", ["EcoFlowCloud"]),
        ("num_inverters", 1),
        ("soc_percent", ["sensor.predbat_ecoflow_sn1_soc"]),
        ("battery_power", ["sensor.predbat_ecoflow_sn1_battery_power"]),
        ("grid_power", ["sensor.predbat_ecoflow_sn1_grid_power"]),
        ("load_power", ["sensor.predbat_ecoflow_sn1_load_power"]),
        ("pv_power", ["sensor.predbat_ecoflow_sn1_pv_power"]),
        ("soc_max", ["sensor.predbat_ecoflow_sn1_battery_capacity"]),
        ("inverter_limit", ["sensor.predbat_ecoflow_sn1_inverter_limit"]),
    ]:
        if args.get(arg) != expected:
            print(f"ERROR: arg {arg} = {args.get(arg)} != {expected}")
            failed = True
    # Every sign flag is owned explicitly. base.args is shared and NOT namespaced per inverter
    # type, so a component that legitimately inverts its own grid sensor leaves that key set for
    # every inverter index and an EcoFlow inverter that never claims it inherits the flip.
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        if args.get(flag) != [False]:
            print(f"ERROR: {flag} = {args.get(flag)}")
            failed = True
    for control in ("charge_start_time", "charge_end_time", "charge_limit", "charge_rate", "scheduled_charge_enable", "discharge_start_time", "scheduled_discharge_enable", "schedule_write_button", "reserve"):
        if control in args:
            print(f"ERROR: control arg {control} must not be bound while there is no write command")
            failed = True
    if not any("cannot charge or discharge" in line for line in client.log_messages):
        print("ERROR: automatic_config must say out loud that it bound no control")
        failed = True
    assert not failed, "test_ecoflow_automatic_config_binds_monitoring_only"


def test_ecoflow_automatic_config_declines_a_partly_reported_arg():
    """An arg is bound only when EVERY device reports it, and the miss is explained."""
    failed = False
    client = MockEcoFlow(automatic=True)
    _loaded(client, serials=("SN1", "SN2"))
    # SN2 never resolved a grid reading, so grid_power must not be bound for either device.
    client.device_values["SN2"].pop("grid_power")
    run_async_local(client.automatic_config())
    if "grid_power" in client.base.args:
        print(f"ERROR: grid_power bound as {client.base.args['grid_power']} although SN2 does not report it")
        failed = True
    if "soc_percent" not in client.base.args:
        print("ERROR: a fully reported arg should still bind")
        failed = True
    if not any("not every device reports grid_power" in line for line in client.log_messages):
        print("ERROR: the miss was not explained")
        failed = True
    assert not failed, "test_ecoflow_automatic_config_declines_a_partly_reported_arg"


def test_ecoflow_automatic_config_warns_about_the_unmapped_ratings():
    """The three figures Predbat cannot discover are named explicitly, not left silent.

    inverter.py's fallbacks - a 2600W battery rate in particular - are roughly half a Power
    Ocean's real rate and never report themselves, so silence here is a silently wrong plan.
    """
    failed = False
    client = _loaded(MockEcoFlow(automatic=True))
    run_async_local(client.automatic_config())
    warnings = " ".join(line for line in client.log_messages if line.startswith("Warn:"))
    for needle in ("soc_max", "inverter_limit", "battery_rate_max", "export_limit", "ecoflow_battery_capacity"):
        if needle not in warnings:
            print(f"ERROR: nothing warned about {needle}")
            failed = True
    assert not failed, "test_ecoflow_automatic_config_warns_about_the_unmapped_ratings"


def test_ecoflow_automatic_ignore_pv_skips_only_pv():
    """automatic_ignore_pv leaves pv_power unbound and everything else alone."""
    failed = False
    client = _loaded(MockEcoFlow(automatic=True))
    client.automatic_ignore_pv = True
    run_async_local(client.automatic_config())
    if "pv_power" in client.base.args:
        print("ERROR: pv_power should not be bound under automatic_ignore_pv")
        failed = True
    if "soc_percent" not in client.base.args:
        print("ERROR: the other args should still bind")
        failed = True
    assert not failed, "test_ecoflow_automatic_ignore_pv_skips_only_pv"


def test_ecoflow_discovery_record_is_monitor_only_and_valid():
    """The discovery record survives validate_report, and says control=False.

    control=False is a FACT about this integration today rather than an absence, which is why
    inverter_record() keeps it. It flips with ECOFLOW_SCHEDULE_COMMANDS, via control_bindable().
    """
    failed = False
    client = _loaded(MockEcoFlow(battery_capacity=10, inverter_limit=5000))
    report = client.build_discovery()
    if report is None:
        print("ERROR: no report built")
        assert False, "test_ecoflow_discovery_record_is_monitor_only_and_valid"
    cleaned = validate_report(report, "ecoflow", client.log)
    records = cleaned.get("inverters") or []
    if len(records) != 1:
        print(f"ERROR: validate_report dropped the record: {cleaned}")
        assert False, "test_ecoflow_discovery_record_is_monitor_only_and_valid"
    record = records[0]
    if record.get("device_id") != "ecoflow:SN1":
        print(f"ERROR: device_id {record.get('device_id')}")
        failed = True
    if record.get("control") is not False:
        print(f"ERROR: control {record.get('control')} should be False while no write command exists")
        failed = True
    if record.get("inverter_type") != "EcoFlowCloud":
        print(f"ERROR: inverter_type {record.get('inverter_type')}")
        failed = True
    if sorted(record.get("functions") or []) != ["battery", "solar"]:
        print(f"ERROR: functions {record.get('functions')}")
        failed = True
    if record.get("ratings", {}).get("soc_max") != 10.0:
        print(f"ERROR: ratings {record.get('ratings')}")
        failed = True
    # Every capability key is declared, and all are False - none of them can be delivered
    # without a write.
    capabilities = record.get("capabilities") or {}
    for key in CAPABILITY_KEYS:
        if key not in capabilities:
            print(f"ERROR: capability {key} missing from the record")
            failed = True
        elif capabilities[key] is not False:
            print(f"ERROR: capability {key} is {capabilities[key]}, but nothing can be written")
            failed = True
    # No rw entity: a record that bound one would rebuild a definition claiming a control
    # surface this component does not have.
    writable = [name for name, descriptor in (record.get("entities") or {}).items() if descriptor.get("access") != "r"]
    if writable:
        print(f"ERROR: the record binds writable entities {writable}")
        failed = True
    assert not failed, "test_ecoflow_discovery_record_is_monitor_only_and_valid"


def test_ecoflow_discovery_record_omits_unknown_ratings():
    """With no user figures, the record carries no ratings and no solar function."""
    failed = False
    client = _loaded(MockEcoFlow(), values={"soc": 40.0})
    record = client.build_discovery()["inverters"][0]
    if "ratings" in record:
        print(f"ERROR: ratings should be omitted entirely, got {record['ratings']}")
        failed = True
    if record.get("functions") != ["battery"]:
        print(f"ERROR: functions {record.get('functions')} - solar is claimed only when PV resolved")
        failed = True
    if sorted(record.get("entities") or {}) != ["soc_percent"]:
        print(f"ERROR: entities {sorted(record.get('entities') or {})}")
        failed = True
    assert not failed, "test_ecoflow_discovery_record_omits_unknown_ratings"


def test_ecoflow_discovery_returns_none_before_discovery():
    """No devices means no report, which refresh_discovery() reads as 'ask again next cycle'."""
    failed = False
    if MockEcoFlow().build_discovery() is not None:
        print("ERROR: build_discovery should return None with no devices")
        failed = True
    assert not failed, "test_ecoflow_discovery_returns_none_before_discovery"


def run_ecoflow_publish_tests(my_predbat):
    """Run all EcoFlow publish/auto-config tests."""
    failed = False
    for name, fn in [
        ("publish_leaves", test_ecoflow_publishes_every_resolved_leaf_with_units),
        ("publish_absent_leaf", test_ecoflow_publishes_nothing_for_an_unresolved_leaf),
        ("quota_key_diagnostic", test_ecoflow_publishes_the_quota_key_diagnostic),
        ("ratings_from_user", test_ecoflow_ratings_publish_only_when_the_user_supplied_them),
        ("automatic_config", test_ecoflow_automatic_config_binds_monitoring_only),
        ("partial_arg", test_ecoflow_automatic_config_declines_a_partly_reported_arg),
        ("rating_warnings", test_ecoflow_automatic_config_warns_about_the_unmapped_ratings),
        ("ignore_pv", test_ecoflow_automatic_ignore_pv_skips_only_pv),
        ("discovery_record", test_ecoflow_discovery_record_is_monitor_only_and_valid),
        ("discovery_omits", test_ecoflow_discovery_record_omits_unknown_ratings),
        ("discovery_none", test_ecoflow_discovery_returns_none_before_discovery),
    ]:
        try:
            if fn():
                print(f"  FAILED: ecoflow_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in ecoflow_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

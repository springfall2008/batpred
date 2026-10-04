# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk automatic configuration and registration
# -----------------------------------------------------------------------------

"""Tests for Sunsynk automatic_config, INVERTER_DEF and component registration."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from unittest.mock import patch
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from coordinator import validate_report
from sunsynk_const import SUNSYNK_TTL_STATIC
from sunsynk import SunsynkAPI, load_apps_yaml_credentials, _tou_test_window
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_sunsynk_api import MockSunsynk
from tests.test_sunsynk_publish import PublishingSunsynk
from tests.test_infra import run_async as run_async_local


class ConfigSunsynk(MockSunsynk):
    """MockSunsynk that records set_arg calls."""

    def __init__(self, **kwargs):
        """Set up the recorder."""
        super().__init__(**kwargs)
        self.args_set = {}

    def set_arg(self, key, value):
        """Record an arg assignment."""
        self.args_set[key] = value


class RunSunsynk(PublishingSunsynk):
    """PublishingSunsynk that also records set_arg, for driving run() end to end.

    PublishingSunsynk supplies the Home Assistant entity surface - dashboard_item plus a
    get_state_wrapper that reads back whatever was published - which is what makes it
    possible to drive run() with control enabled and ONLY the transport patched. That is
    the altitude the schedule-clobbering bugs lived at: every earlier run() test patched
    apply_schedule itself, so no test ever exercised run() with a real apply_settings
    underneath and the unplanned first-cycle write went unnoticed through nine reviews.
    """

    def __init__(self, **kwargs):
        """Set up the recorder alongside the publishing double."""
        super().__init__(**kwargs)
        self.args_set = {}

    def set_arg(self, key, value):
        """Record an arg assignment."""
        self.args_set[key] = value


def _live_plan_entities(s, sn="INV1"):
    """Seed the control entities with an in-flight 02:00-05:00 charge to 90%, as HA would hold it.

    Home Assistant retains Predbat's control entities across a Predbat restart, so this is
    exactly the state a 03:00 restart finds mid-charge.
    """
    s.published[s._control_name("number", sn, "battery_schedule_reserve")] = {"state": 10, "attributes": {}}
    s.published[s._control_name("switch", sn, "battery_schedule_charge_enable")] = {"state": "on", "attributes": {}}
    s.published[s._control_name("select", sn, "battery_schedule_charge_start_time")] = {"state": "02:00:00", "attributes": {}}
    s.published[s._control_name("select", sn, "battery_schedule_charge_end_time")] = {"state": "05:00:00", "attributes": {}}
    s.published[s._control_name("number", sn, "battery_schedule_charge_soc")] = {"state": 90, "attributes": {}}
    s.published[s._control_name("number", sn, "battery_schedule_charge_power")] = {"state": 3000, "attributes": {}}


def _transport(s, posts, settings=None):
    """Return (fake_get, fake_post) covering every endpoint run() touches.

    Patching only these two leaves discovery, the tier refreshes, the control-entity
    round trip and the whole apply_settings read-modify-write as the real code.
    """
    baseline = settings if settings is not None else {"sn": "INV1", "sysWorkMode": "2", "peakAndVallery": "0", "batteryLowCap": "20", "sellTime1": "01:00", "cap1": "80", "time1on": True, "mondayOn": False}

    async def fake_get(endpoint_key, sn=None, params=None):
        """Serve discovery, static detail, telemetry and the settings baseline."""
        if endpoint_key == "inverter_list":
            return {"infos": [{"sn": "INV1"}], "total": 1}
        if endpoint_key == "inverter_detail":
            return {"ratePower": 8000}
        if endpoint_key == "battery":
            return {"soc": 50, "power": -100, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100, "etodayChg": 1.0, "etodayDischg": 1.0}
        if endpoint_key == "grid":
            return {"pac": 100, "etodayFrom": 2.0, "etodayTo": 1.0}
        if endpoint_key == "load":
            return {"totalPower": 400, "dailyUsed": 5.0}
        if endpoint_key == "input":
            return {"pac": 1200, "etoday": 8.0}
        if endpoint_key == "settings_read":
            return dict(baseline)
        return {}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record every write the component attempts, reporting a successful empty payload."""
        posts.append((endpoint_key, sn, body))
        return {}

    return fake_get, fake_post


async def _fake_token():
    """Stand in for a successful login; the auth path has its own tests."""
    return True


def test_inverter_def_registered():
    """SunsynkCloud exists and declares the capabilities the control model relies on."""
    failed = False
    definition = INVERTER_DEF.get("SunsynkCloud")
    if not definition:
        print("ERROR: INVERTER_DEF['SunsynkCloud'] is missing")
        assert False, "test_inverter_def_registered"
    expected = {
        "output_charge_control": "power",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "%",
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "can_span_midnight": False,
        "target_soc_used_for_discharge": True,
    }
    for key, value in expected.items():
        if definition.get(key) != value:
            print(f"ERROR: SunsynkCloud {key} = {definition.get(key)!r}, expected {value!r}")
            failed = True
    assert not failed, "test_inverter_def_registered"


def test_component_registered():
    """The sunsynk component is registered with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("sunsynk")
    if not entry:
        print("ERROR: COMPONENT_LIST['sunsynk'] is missing")
        assert False, "test_component_registered"
    if entry.get("event_filter") != "predbat_sunsynk_":
        print(f"ERROR: event_filter {entry.get('event_filter')!r}")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')!r}, expected 1")
        failed = True
    # Activation must be gated on having at least one usable auth path; every individual
    # arg is optional so either auth mode can be configured alone.
    if sorted(entry.get("required_or", [])) != ["key", "username"]:
        print(f"ERROR: required_or {entry.get('required_or')!r}")
        failed = True
    for arg in ("username", "password", "key", "region", "auth_method", "inverter_sn", "automatic", "control_enable"):
        if arg not in entry.get("args", {}):
            print(f"ERROR: component arg {arg} not registered")
            failed = True
    # Pin the registered defaults. control_enable in particular decides whether Predbat
    # writes to a real inverter at all, so a silent flip either way is worth catching:
    # False would leave a configured component inert, True is the intended behaviour and
    # matches solis_control_enable.
    defaults = {"control_enable": True, "region": "sunsynk", "auth_method": "password", "automatic": False, "automatic_ignore_pv": False}
    for arg, expect in defaults.items():
        got = entry.get("args", {}).get(arg, {}).get("default")
        if got != expect:
            print(f"ERROR: component arg {arg} default {got!r}, expected {expect!r}")
            failed = True
    assert not failed, "test_component_registered"


def test_apps_schema_keys():
    """Every sunsynk_* config key is declared for apps.yaml validation."""
    failed = False
    expected = {
        "sunsynk_username": "string",
        "sunsynk_password": "string",
        "sunsynk_key": "string",
        "sunsynk_region": "string",
        "sunsynk_auth_method": "string",
        "sunsynk_token_expires_at": "string",
        "sunsynk_token_hash": "string",
        "sunsynk_inverter_sn": "string|string_list",
        "sunsynk_automatic": "boolean",
        "sunsynk_automatic_ignore_pv": "boolean",
        "sunsynk_control_enable": "boolean",
        "sunsynk_battery_nominal_voltage": "float",
    }
    for key, kind in expected.items():
        entry = APPS_SCHEMA.get(key)
        if not entry:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
        elif entry.get("type") != kind:
            print(f"ERROR: {key} type {entry.get('type')!r}, expected {kind!r}")
            failed = True
    assert not failed, "test_apps_schema_keys"


def test_automatic_config_maps_control_entities():
    """Every inverter is registered as SunsynkCloud with its sensors and controls."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    for sn in s.device_list:
        s.device_values[sn] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
        s.device_energy[sn] = {"pv_today": 1.0, "import_today": 1.0, "export_today": 1.0, "load_today": 1.0, "battery_charge_today": 1.0, "battery_discharge_today": 1.0}
        s.device_rated_power[sn] = 8000.0
        s.device_settings[sn] = {"batteryLowCap": "10"}
    run_async_local(s.automatic_config())
    if s.args_set.get("inverter_type") != ["SunsynkCloud", "SunsynkCloud"]:
        print(f"ERROR: inverter_type {s.args_set.get('inverter_type')}")
        failed = True
    if s.args_set.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {s.args_set.get('num_inverters')}")
        failed = True
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "battery_rate_max", "inverter_limit", "battery_min_soc"):
        if arg not in s.args_set:
            print(f"ERROR: sensor arg {arg} not mapped")
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
        if arg not in s.args_set:
            print(f"ERROR: control arg {arg} not mapped")
            failed = True
    # Control args must point at the control entities, not sensors.
    if not str(s.args_set.get("charge_start_time", [""])[0]).startswith("select."):
        print(f"ERROR: charge_start_time should be a select entity, got {s.args_set.get('charge_start_time')}")
        failed = True
    assert not failed, "test_automatic_config_maps_control_entities"


def test_automatic_config_skips_partial_capabilities():
    """An arg is only mapped when every inverter reports the underlying value."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    # INV2 has no chargeVolt, so its capacity and rate cannot be derived.
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_values["INV2"] = {"soc": 50, "capacity": 280, "maxChargeCurrentLimit": 100}
    s.device_energy = {"INV1": {"pv_today": 1.0}, "INV2": {}}
    s.device_rated_power = {"INV1": 8000.0}
    run_async_local(s.automatic_config())
    for arg in ("soc_max", "battery_rate_max", "inverter_limit", "pv_today"):
        if arg in s.args_set:
            print(f"ERROR: {arg} was mapped although not every inverter reports it")
            failed = True
    if not any("manually" in str(m) for m in s.log_messages):
        print("ERROR: skipping an arg should warn the user to set it in apps.yaml")
        failed = True
    assert not failed, "test_automatic_config_skips_partial_capabilities"


def test_automatic_config_respects_ignore_pv():
    """automatic_ignore_pv leaves the PV args for another component to own."""
    failed = False
    s = ConfigSunsynk()
    s.automatic_ignore_pv = True
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_energy["INV1"] = {"pv_today": 1.0}
    s.device_rated_power["INV1"] = 8000.0
    run_async_local(s.automatic_config())
    for arg in ("pv_power", "pv_today"):
        if arg in s.args_set:
            print(f"ERROR: {arg} mapped despite automatic_ignore_pv")
            failed = True
    if "soc_percent" not in s.args_set:
        print("ERROR: non-PV args should still be mapped")
        failed = True
    assert not failed, "test_automatic_config_respects_ignore_pv"


def test_run_first_cycle_polls_and_publishes():
    """The first run restores caches, discovers, polls and publishes."""
    failed = False
    s = ConfigSunsynk()
    calls = []

    async def fake_restore():
        """Record the cache restore."""
        calls.append("restore")

    async def fake_token():
        """Record the login."""
        calls.append("token")
        return True

    async def fake_device_list():
        """Record discovery and return one inverter."""
        calls.append("discover")
        s.device_list = ["INV1"]
        return ["INV1"]

    async def fake_detail(sn):
        """Record the detail fetch."""
        calls.append("detail")
        return {"ratePower": 8000}

    async def fake_device_data(sn):
        """Record the telemetry poll."""
        calls.append("telemetry")
        s.device_values[sn] = {"soc": 50}
        return {"soc": 50}

    async def fake_settings(sn):
        """Record the settings read."""
        calls.append("settings")
        return {"batteryLowCap": "10"}

    async def fake_publish():
        """Record publishing."""
        calls.append("publish")

    with (
        patch.object(s, "restore_state", side_effect=fake_restore),
        patch.object(s, "fetch_token", side_effect=fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list),
        patch.object(s, "fetch_device_detail", side_effect=fake_detail),
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "publish_data", side_effect=fake_publish),
    ):
        result = run_async_local(s.run(0, True))
    for step in ("restore", "discover", "telemetry", "publish"):
        if step not in calls:
            print(f"ERROR: first cycle never did {step}; calls were {calls}")
            failed = True
    # ComponentBase.start() only clears its startup "first" flag and reaches the normal
    # 60-second cadence when run() returns something truthy; a regression back to an
    # implicit `None` here would strand the component in an ever-growing backoff without
    # this assertion ever noticing, since every step above still runs either way.
    if result is not True:
        print(f"ERROR: run() should return True on a completed first cycle, got {result!r}")
        failed = True
    assert not failed, "test_run_first_cycle_polls_and_publishes"


SUNSYNK_LIVE_SERIAL = "2405116013"

# The seven behaviour keys as the SunsynkCloud INVERTER_DEF row states them (config.py). Spelled out, not
# read from the row, so a change to either side shows up here.
SUNSYNK_ROW_CAPABILITIES = {
    "support_charge_freeze": True,
    "support_discharge_freeze": True,
    "support_feedin_first": True,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}


def _sunsynk_fleet():
    """A MockSunsynk holding one inverter built from sunsynk_const.py's CONFIRMED-live values only.

    No captured inverter_detail or settings response exists in the repository (sunsynk_const.py
    records that nobody on the project has a live account), so this is assembled from the values it
    does mark confirmed live: ratePower 8000 with pvMaxLimit 7000; chargeVolt 58.4 with a 200 Ah pack
    (10.24 kWh); and the serial the live telemetry sample was taken from.
    """
    s = MockSunsynk()
    sn = SUNSYNK_LIVE_SERIAL
    s.device_list = [sn]
    s.device_rated_power = {sn: 8000.0}
    s.device_values = {sn: {"capacity": 200, "chargeVolt": 58.4}}
    s.device_settings = {sn: {"pvMaxLimit": 7000}}
    return s


def test_sunsynk_catalogue_describes_each_inverter():
    """Each inverter is a SunsynkCloud record with the confirmed-live ratings under Predbat's setting names."""
    failed = False
    s = _sunsynk_fleet()
    record = s.build_discovery()["inverters"][0]
    checks = [
        (record["device_id"], "sunsynk:" + SUNSYNK_LIVE_SERIAL),
        (record["inverter_type"], "SunsynkCloud"),
        (record["composition"], "direct"),
        (record["functions"], ["solar", "battery"]),
        (record["hardware_ids"], {"serial": SUNSYNK_LIVE_SERIAL}),
        (record["capabilities"], SUNSYNK_ROW_CAPABILITIES),
        (record["ratings"]["inverter_limit"], 8000.0),
        (record["ratings"]["export_limit"], 7000.0),
        (record["ratings"]["battery_capacity_ah"], 200.0),
        (record["entities"]["soc_max"]["entity_id"], s._sensor_name(SUNSYNK_LIVE_SERIAL, "battery_capacity")),
    ]
    for actual, expected in checks:
        if actual != expected:
            print("ERROR: expected {!r}, got {!r}".format(expected, actual))
            failed = True
    # Spec D14: the kWh is Predbat's derivation (Ah x an inferred or user-set pack voltage), so it is
    # bound as the battery_capacity sensor above but is not a rating
    for old in ("inverter_w", "battery_kwh", "max_charge_w", "soc_max"):
        if old in record["ratings"]:
            print("ERROR: rating {} must be absent (renamed, or a Predbat derivation): {}".format(old, record["ratings"]))
            failed = True
    for absent in ("info", "account_ids"):
        if absent in record:
            print("ERROR: Sunsynk holds no model, firmware or station ID, so {} must be absent: {}".format(absent, record[absent]))
            failed = True
    return failed


def test_sunsynk_catalogue_export_limit_only_on_evidence():
    """The export_limit rating is the raw pvMaxLimit only; the entity follows automatic_config()'s export_limit() test.

    export_limit() falls back to the inverter rating when pvMaxLimit is absent, so automatic_config()
    still binds the export_limit sensor, and so does the record - but the rating is the configured
    cap (spec D1), which this install has not stated, so there is no rating.
    """
    failed = False
    s = _sunsynk_fleet()
    s.device_settings = {}
    record = s.build_discovery()["inverters"][0]
    if "export_limit" in record.get("ratings", {}):
        print("ERROR: with no pvMaxLimit there is no configured export cap to report: {}".format(record["ratings"]))
        failed = True
    if record.get("entities", {}).get("export_limit", {}).get("entity_id") != s._sensor_name(SUNSYNK_LIVE_SERIAL, "export_limit"):
        print("ERROR: export_limit() falls back to the rating, so the export_limit sensor is bound: {}".format(record.get("entities", {}).get("export_limit")))
        failed = True
    nothing = _sunsynk_fleet()
    nothing.device_rated_power = {}
    nothing.device_settings = {}
    record = nothing.build_discovery()["inverters"][0]
    if "export_limit" in record.get("ratings", {}) or "export_limit" in record.get("entities", {}):
        print("ERROR: with no rating and no pvMaxLimit, export_limit() is 0 - no rating and no entity: {}".format(record))
        failed = True
    return failed


def test_sunsynk_catalogue_keeps_battery_ratings_through_a_partial_poll():
    """A poll that omits the battery fields keeps the last battery Ah rating, as the published sensors keep their state.

    fetch_device_data() rebuilds device_values from every poll and leaves out a field the battery
    endpoint did not return. publish_data() then skips the battery sensors, so Home Assistant - and
    Predbat's soc_max - keep the last value. The catalogue keeps its last Ah too, rather than filing a
    thinner report and then the full one again when the fields return. The kWh is never a rating
    (spec D14), so there is no Ah/kWh pair left to keep consistent.
    """
    failed = False
    s = _sunsynk_fleet()
    sn = SUNSYNK_LIVE_SERIAL
    full = s.build_discovery()
    for name, partial in (("no capacity", {"chargeVolt": 58.4}), ("no battery fields", {})):
        s.device_values = {sn: dict(partial)}
        if s.build_discovery() != full:
            print("ERROR: {}: the report changed on a partial poll: {}".format(name, s.build_discovery()))
            failed = True

    # A poll that carries a new Ah replaces the kept one, with or without a chargeVolt
    s.device_values = {sn: {"capacity": 280}}
    ratings = s.build_discovery()["inverters"][0]["ratings"]
    if ratings.get("battery_capacity_ah") != 280.0 or "soc_max" in ratings:
        print("ERROR: a fresh Ah must replace the kept one, and soc_max is never a rating: {}".format(ratings))
        failed = True

    # Nothing is invented for an inverter whose battery fields have never been seen
    s.device_list = [sn, "UNSEEN1"]
    unseen = {record["device_id"]: record for record in s.build_discovery()["inverters"]}["sunsynk:UNSEEN1"]
    if {"soc_max", "battery_capacity_ah"} & set(unseen.get("ratings", {})) or "soc_max" in unseen.get("entities", {}):
        print("ERROR: an inverter never polled must carry no battery rating or capacity binding: {}".format(unseen))
        failed = True
    return failed


def test_sunsynk_catalogue_none_before_discovery():
    """With no inverters there is nothing to describe."""
    if MockSunsynk().build_discovery() is not None:
        print("ERROR: an empty device_list must report None")
        return True
    return False


def test_sunsynk_catalogue_round_trips_through_validate_report():
    """validate_report() hands every Sunsynk record back unchanged."""
    report = _sunsynk_fleet().build_discovery()
    warnings = []
    cleaned = validate_report(report, "sunsynk", warnings.append)
    if warnings or cleaned.get("inverters") != report["inverters"]:
        print("ERROR: validation changed or warned on the report: {} {}".format(warnings, cleaned.get("inverters")))
        return True
    return False


def _sunsynk_driven_fleet(serials=(SUNSYNK_LIVE_SERIAL,)):
    """A MockSunsynk whose inverters report everything automatic_config() binds.

    The confirmed-live figures of _sunsynk_fleet() (ratePower 8000, pvMaxLimit 7000, 200 Ah at
    chargeVolt 58.4) plus the confirmed-live importPower 10350, a batteryLowCap floor, a charge
    current limit and every daily energy counter, so every conditional binding in
    automatic_config() is taken.
    """
    s = MockSunsynk()
    s.device_list = list(serials)
    for sn in serials:
        s.device_rated_power[sn] = 8000.0
        s.device_values[sn] = {"soc": 50, "capacity": 200, "chargeVolt": 58.4, "chargeCurrentLimit": 100}
        s.device_energy[sn] = {"pv_today": 1.0, "import_today": 1.0, "export_today": 1.0, "load_today": 1.0, "battery_charge_today": 1.0, "battery_discharge_today": 1.0}
        s.device_settings[sn] = {"batteryLowCap": "20", "pvMaxLimit": "7000", "importPower": "10350"}
    return s


def test_sunsynk_record_rebuilds_its_inverter_def_row():
    """Completeness: the record alone, with no INVERTER_DEF row as a base, rebuilds the SunsynkCloud row."""
    s = _sunsynk_driven_fleet()
    for record in validated_inverters(s.build_discovery()):
        assert_definition_complete(record, SunsynkAPI.WRITE_AND_POLL_SLEEP)
    return False


def test_sunsynk_record_agrees_with_automatic_config():
    """Agreement both ways: every setting automatic_config() binds is in the record, and the record binds nothing more."""
    s = _sunsynk_driven_fleet()
    records = validated_inverters(s.build_discovery())
    captured = capture_automatic_config(s)
    assert_record_agrees(records[0], captured, index=0)
    assert_record_binds_nothing_extra(records[0], captured, index=0)
    return False


def test_sunsynk_two_inverters_give_two_records_with_their_own_entities():
    """Review Focus 1: two inverters give two records, each binding its own serial's entities and agreeing at its own index."""
    failed = False
    s = _sunsynk_driven_fleet(("2405116013", "2211093089"))
    records = validated_inverters(s.build_discovery())
    if [record["device_id"] for record in records] != ["sunsynk:2405116013", "sunsynk:2211093089"]:
        print("ERROR: expected one record per serial, got {}".format([record["device_id"] for record in records]))
        return True
    captured = capture_automatic_config(s)
    for index, record in enumerate(records):
        assert_record_agrees(record, captured, index=index)
        assert_record_binds_nothing_extra(record, captured, index=index)
    first, second = (record["entities"] for record in records)
    if set(first) != set(second):
        print("ERROR: both inverters report the same data, so they bind the same settings: {} vs {}".format(sorted(first), sorted(second)))
        failed = True
    for setting in sorted(set(first) & set(second)):
        if first[setting]["entity_id"] == second[setting]["entity_id"]:
            print("ERROR: {} is bound to the same entity {} for both inverters".format(setting, first[setting]["entity_id"]))
            failed = True
    return failed


def test_sunsynk_record_ratings_are_the_configured_figures():
    """Spec D1: export_limit is the raw pvMaxLimit and import_limit the raw importPower, never capped by the rating.

    A cap set above the inverter's rating is reported as set. export_limit() - the state of the
    export_limit sensor automatic_config() binds - is the lower of the two, 8000; the rating is
    the configured 9000.
    """
    failed = False
    s = _sunsynk_driven_fleet()
    s.device_settings[SUNSYNK_LIVE_SERIAL]["pvMaxLimit"] = "9000"
    record = s.build_discovery()["inverters"][0]
    expected = {"inverter_limit": 8000.0, "export_limit": 9000.0, "import_limit": 10350.0, "battery_min_soc": 20, "battery_capacity_ah": 200.0}
    if record["ratings"] != expected:
        print("ERROR: ratings {}, expected {}".format(record["ratings"], expected))
        failed = True
    if s.export_limit(SUNSYNK_LIVE_SERIAL) != 8000.0:
        print("ERROR: export_limit() should still be the lower of cap and rating, got {}".format(s.export_limit(SUNSYNK_LIVE_SERIAL)))
        failed = True
    if record["entities"]["export_limit"] != {"entity_id": s._sensor_name(SUNSYNK_LIVE_SERIAL, "export_limit"), "access": "r", "unit": "W"}:
        print("ERROR: the export_limit binding must be automatic_config()'s sensor: {}".format(record["entities"]["export_limit"]))
        failed = True
    return failed


def test_sunsynk_record_keeps_pv_despite_automatic_ignore_pv():
    """Spec D11: automatic_ignore_pv stops automatic_config() binding PV, but the record still describes the device's PV."""
    failed = False
    s = _sunsynk_driven_fleet()
    s.automatic_ignore_pv = True
    record = validated_inverters(s.build_discovery())[0]
    for setting, leaf in (("pv_power", "pv_power"), ("pv_today", "pv_today")):
        if record["entities"].get(setting, {}).get("entity_id") != s._sensor_name(SUNSYNK_LIVE_SERIAL, leaf):
            print("ERROR: {} must stay in the record despite automatic_ignore_pv: {}".format(setting, record["entities"].get(setting)))
            failed = True
    captured = capture_automatic_config(s)
    for setting in ("pv_power", "pv_today"):
        if setting in captured:
            print("ERROR: automatic_config() should still skip {} under automatic_ignore_pv".format(setting))
            failed = True
    assert_record_agrees(record, captured)
    assert_record_binds_nothing_extra(record, captured, allowed_extra=("pv_power", "pv_today"))
    return failed


def test_sunsynk_mixed_fleet_records_describe_each_device():
    """Spec D10: each record lists what its own device has, not what automatic_config()'s all-inverters gate allows.

    The second inverter reports no chargeVolt, charge current or PV counter, so automatic_config()
    binds soc_max, battery_rate_max and pv_today for neither. The first inverter's record still
    carries all three; the second's carries none of them.
    """
    failed = False
    per_device = ("soc_max", "battery_rate_max", "pv_today")
    s = _sunsynk_driven_fleet(("2405116013", "2211093089"))
    s.device_values["2211093089"] = {"soc": 50, "capacity": 200}
    del s.device_energy["2211093089"]["pv_today"]
    first, second = validated_inverters(s.build_discovery())
    captured = capture_automatic_config(s)
    for setting in per_device:
        if setting in captured:
            print("ERROR: the fleet gate should have withheld {} from automatic_config(): {}".format(setting, captured[setting]))
            failed = True
        if setting not in first["entities"]:
            print("ERROR: the first inverter reports {}, so its record must bind it".format(setting))
            failed = True
        if setting in second["entities"]:
            print("ERROR: the second inverter does not report {}, so its record must not bind it: {}".format(setting, second["entities"][setting]))
            failed = True
    assert_record_agrees(first, captured, index=0)
    assert_record_agrees(second, captured, index=1)
    assert_record_binds_nothing_extra(first, captured, index=0, allowed_extra=per_device)
    assert_record_binds_nothing_extra(second, captured, index=1)
    return failed


def test_sunsynk_record_keeps_sensor_bindings_through_a_partial_poll():
    """A poll that omits a rating's inputs or an energy counter keeps the binding, as Home Assistant keeps the sensor.

    publish_data() skips a sensor whose value this poll did not bring, so the entity keeps its last
    state and automatic_config()'s binding stays good. The record keeps the binding too, rather than
    filing a thinner report and then the full one again.
    """
    failed = False
    s = _sunsynk_driven_fleet()
    full = s.build_discovery()
    s.device_values = {SUNSYNK_LIVE_SERIAL: {"soc": 50}}
    s.device_energy = {SUNSYNK_LIVE_SERIAL: {}}
    if s.build_discovery() != full:
        print("ERROR: a partial poll changed the report: {}".format(s.build_discovery()))
        failed = True
    fresh = _sunsynk_driven_fleet()
    fresh.device_values = {SUNSYNK_LIVE_SERIAL: {"soc": 50}}
    fresh.device_energy = {SUNSYNK_LIVE_SERIAL: {"load_today": 1.0}}
    entities = fresh.build_discovery()["inverters"][0]["entities"]
    for setting in ("soc_max", "battery_rate_max", "pv_today", "import_today"):
        if setting in entities:
            print("ERROR: {} was never reported, so no sensor exists to bind: {}".format(setting, entities[setting]))
            failed = True
    if "load_today" not in entities:
        print("ERROR: load_today is reported, so its sensor must be bound")
        failed = True
    return failed


def test_sunsynk_catalogue_filed_when_first_cycle_defers():
    """run() files the report even on a first cycle that defers startup because the live poll failed.

    `if first and not live_ok: return False` - and automatic_config() after it - would otherwise
    swallow the report on exactly the installs whose dump most needs to say what hardware was found.
    Built on test_run_first_cycle_polls_and_publishes(); fetch_device_data() fails and
    automatic_config() is observed.
    """
    failed = False
    s = ConfigSunsynk()
    s.automatic = True
    reports = []
    s.report_discovery = reports.append
    configured = []

    async def fake_restore():
        """Restore nothing."""

    async def fake_token():
        """Log in successfully."""
        return True

    async def fake_device_list():
        """Discover one inverter."""
        s.device_list = ["INV1"]
        return ["INV1"]

    async def fake_detail(sn):
        """Return the confirmed-live rating."""
        return {"ratePower": 8000}

    async def fake_device_data(sn):
        """Fail the live poll: refresh_live() treats a falsy result as no data."""
        return {}

    async def fake_settings(sn):
        """Return a minimal settings read."""
        return {"batteryLowCap": "10"}

    async def fake_publish():
        """Publish nothing."""

    async def fake_auto():
        """Record that automatic_config() ran."""
        configured.append(True)

    with (
        patch.object(s, "restore_state", side_effect=fake_restore),
        patch.object(s, "fetch_token", side_effect=fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list),
        patch.object(s, "fetch_device_detail", side_effect=fake_detail),
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "publish_data", side_effect=fake_publish),
        patch.object(s, "automatic_config", side_effect=fake_auto),
    ):
        result = run_async_local(s.run(0, True))
    if result is not False:
        print("ERROR: run() should defer startup (return False) when the first live poll fails, got {!r}".format(result))
        failed = True
    if configured:
        print("ERROR: automatic_config() must not run on a deferred first cycle")
        failed = True
    if len(reports) != 1 or reports[0]["inverters"][0]["device_id"] != "sunsynk:INV1":
        print("ERROR: the report must be filed before the deferring return, got {}".format(reports))
        failed = True
    return failed


def test_run_returns_false_on_login_failure():
    """A failed login must not be reported as a completed cycle, or ComponentBase would clear 'first' anyway."""
    failed = False
    s = ConfigSunsynk()

    async def fake_restore():
        """No-op restore."""
        return None

    async def fake_token_fail():
        """Simulate a login failure."""
        return False

    with patch.object(s, "restore_state", side_effect=fake_restore), patch.object(s, "fetch_token", side_effect=fake_token_fail):
        result = run_async_local(s.run(0, True))
    if result is not False:
        print(f"ERROR: run() should return False after a login failure, got {result!r}")
        failed = True
    assert not failed, "test_run_returns_false_on_login_failure"


def test_run_returns_false_with_no_inverters():
    """An account with nothing discovered must not be reported as a completed cycle."""
    failed = False
    s = ConfigSunsynk()

    async def fake_restore():
        """No-op restore."""
        return None

    async def fake_token():
        """Successful login."""
        return True

    async def fake_device_list_empty():
        """Discovery finds nothing."""
        s.device_list = []
        return []

    with (
        patch.object(s, "restore_state", side_effect=fake_restore),
        patch.object(s, "fetch_token", side_effect=fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list_empty),
    ):
        result = run_async_local(s.run(0, True))
    if result is not False:
        print(f"ERROR: run() should return False with no inverters discovered, got {result!r}")
        failed = True
    assert not failed, "test_run_returns_false_with_no_inverters"


def test_run_publishes_schedule_every_tick_not_only_first():
    """The control-entity readback must keep tracking local_schedule every tick, not only at startup."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1"]
    s.local_schedule["INV1"] = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    # Pre-mark every tier fresh so this non-first tick does not attempt a real network
    # discovery/config/telemetry refresh - only the per-inverter publish loop is under test.
    s.mark_refreshed("static")
    s.mark_refreshed("config")
    s.mark_refreshed("live")
    published = []

    async def fake_get_schedule(sn):
        """Return the schedule unchanged."""
        return s.local_schedule[sn]

    async def fake_apply_schedule(sn):
        """No plan change, nothing to write."""
        return False

    async def fake_publish_schedule(sn):
        """Record each publish."""
        published.append(sn)

    async def fake_publish_data():
        """No-op."""
        return None

    with (
        patch.object(s, "get_schedule_settings_ha", side_effect=fake_get_schedule),
        patch.object(s, "apply_schedule", side_effect=fake_apply_schedule),
        patch.object(s, "publish_schedule_settings_ha", side_effect=fake_publish_schedule),
        patch.object(s, "publish_data", side_effect=fake_publish_data),
    ):
        run_async_local(s.run(0, False))
    if published != ["INV1"]:
        print(f"ERROR: publish_schedule_settings_ha was not called on a non-first tick; published={published}")
        failed = True
    assert not failed, "test_run_publishes_schedule_every_tick_not_only_first"


def test_refresh_config_logs_external_changes():
    """refresh_config must observe a setting changed outside Predbat before it overwrites the baseline used to spot it.

    apply_settings only re-reads settings on a genuine plan change, so the config tier is
    where nearly every externally made change is first observed. If refresh_config does not
    itself compare old vs new before fetch_settings overwrites device_settings in place,
    the change is silently absorbed and the design spec's mitigation for the read-modify-
    write race with the phone app never fires.
    """
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1"]
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_settings(sn):
        """Simulate the phone app having raised the floor since the last read."""
        return {"sn": "INV1", "batteryLowCap": "40"}

    with patch.object(s, "fetch_settings", side_effect=fake_settings):
        run_async_local(s.refresh_config())
    if not any("batteryLowCap" in str(m) and "outside Predbat" in str(m) for m in s.log_messages):
        print(f"ERROR: external change to batteryLowCap was not logged; log_messages={s.log_messages}")
        failed = True
    assert not failed, "test_refresh_config_logs_external_changes"


def test_refresh_config_skips_save_when_every_read_fails():
    """A refresh where every inverter's settings read fails must not re-stamp the on-disk cache as fresh.

    save_config()'s file mtime is what age_cache()/restore_state() use to seed the config
    tier's clock after a restart. Saving unconditionally would re-stamp days-stale content
    as fresh on every failed poll, and after a restart the tier would then be skipped for a
    full TTL while running on stale settings.
    """
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    s.device_settings["INV2"] = {"sn": "INV2", "batteryLowCap": "10"}
    saved = []

    async def fake_settings_fail(sn):
        """Simulate every settings read failing."""
        return {}

    async def fake_save_config():
        """Record whether the cache was (re)written."""
        saved.append(True)

    with patch.object(s, "fetch_settings", side_effect=fake_settings_fail), patch.object(s, "save_config", side_effect=fake_save_config):
        run_async_local(s.refresh_config())
    if saved:
        print("ERROR: save_config was called even though every settings read failed")
        failed = True
    assert not failed, "test_refresh_config_skips_save_when_every_read_fails"


def test_refresh_config_saves_when_any_read_succeeds():
    """A partial success (one inverter offline, one fine) still persists what was actually learned."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    saved = []

    async def fake_settings_partial(sn):
        """INV1 fails, INV2 succeeds."""
        return {} if sn == "INV1" else {"sn": "INV2", "batteryLowCap": "10"}

    async def fake_save_config():
        """Record whether the cache was (re)written."""
        saved.append(True)

    with patch.object(s, "fetch_settings", side_effect=fake_settings_partial), patch.object(s, "save_config", side_effect=fake_save_config):
        run_async_local(s.refresh_config())
    if not saved:
        print("ERROR: save_config was not called even though one inverter's read succeeded")
        failed = True
    assert not failed, "test_refresh_config_saves_when_any_read_succeeds"


def test_run_writes_nothing_before_the_write_button_is_pressed():
    """Two ticks from cold with control enabled and no plan must never reach the inverter.

    Only the transport is patched, so this drives run() -> apply_schedule -> apply_settings
    for real. Before the control_active gate, tick 2 posted a complete settings object built
    from entities Predbat had never written: sellTime1..6 overwritten with the filler times,
    cap1..6 forced to 20, peakAndVallery flipped on, every weekend day-flag turned on and
    sysWorkMode rewritten - wiping the user's own time-of-use programme on first startup,
    before Predbat had any plan at all. deye.py gates exactly this on control_active;
    Sunsynk kept the attribute but dropped the gate.
    """
    failed = False
    s = RunSunsynk(control_enable=True)
    posts = []
    fake_get, fake_post = _transport(s, posts)
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        run_async_local(s.run(0, True))
        run_async_local(s.run(0, False))
    if posts:
        print(f"ERROR: run() wrote to the inverter with no plan and no write-button press: {posts}")
        failed = True
    if s.control_active:
        print(f"ERROR: control_active should stay empty until the write button is pressed, got {s.control_active}")
        failed = True
    assert not failed, "test_run_writes_nothing_before_the_write_button_is_pressed"


def test_run_first_cycle_preserves_a_live_plan_in_the_control_entities():
    """A restart mid-charge must republish the live plan, not an empty schedule.

    dashboard_item reaches set_state_wrapper, so seeding local_schedule from
    _empty_schedule() on the first cycle and publishing it back OVERWRITES Predbat's live
    plan in Home Assistant. Traced at 03:00 inside a 02:00-05:00 charge to 90%: afterwards
    charge_enable was off, the window was 00:00:00 -> 00:00:00 and the reserve 0, so the
    charge stopped until Predbat replanned. The first cycle must READ the entities instead.
    """
    failed = False
    s = RunSunsynk(control_enable=True)
    _live_plan_entities(s)
    posts = []
    fake_get, fake_post = _transport(s, posts)
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        run_async_local(s.run(0, True))
    expected = {
        s._control_name("switch", "INV1", "battery_schedule_charge_enable"): "on",
        s._control_name("select", "INV1", "battery_schedule_charge_start_time"): "02:00:00",
        s._control_name("select", "INV1", "battery_schedule_charge_end_time"): "05:00:00",
        s._control_name("number", "INV1", "battery_schedule_charge_soc"): 90,
        s._control_name("number", "INV1", "battery_schedule_reserve"): 10,
    }
    for entity, want in expected.items():
        got = s.published.get(entity, {}).get("state")
        if got != want:
            print(f"ERROR: the first cycle overwrote {entity}: {got!r}, expected {want!r}")
            failed = True
    if s.local_schedule.get("INV1", {}).get("charge", {}).get("soc") != 90:
        print(f"ERROR: local_schedule did not pick up the live plan: {s.local_schedule.get('INV1')}")
        failed = True
    assert not failed, "test_run_first_cycle_preserves_a_live_plan_in_the_control_entities"


def test_run_writes_once_the_write_button_has_been_pressed():
    """The control_active gate must not make the component inert once Predbat is driving it.

    The write button is Predbat's normal "apply the schedule" action, so pressing it both
    writes and marks the inverter as one Predbat now controls; a later tick whose plan has
    genuinely changed must then write again without another press.
    """
    failed = False
    s = RunSunsynk(control_enable=True)
    _live_plan_entities(s)
    posts = []
    fake_get, fake_post = _transport(s, posts)
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        run_async_local(s.run(0, True))
        run_async_local(s.switch_event(s._control_name("switch", "INV1", "battery_schedule_charge_write"), "turn_on"))
        if len(posts) != 1:
            print(f"ERROR: the write button should have produced exactly one write, got {posts}")
            failed = True
        # Predbat replans and moves the charge window; the next tick must carry that through.
        s.published[s._control_name("select", "INV1", "battery_schedule_charge_start_time")] = {"state": "01:00:00", "attributes": {}}
        run_async_local(s.run(0, False))
    if len(posts) != 2:
        print(f"ERROR: a changed plan after the write button should write again, got {len(posts)} write(s): {posts}")
        failed = True
    elif "01:00" not in str(posts[-1][2]):
        print(f"ERROR: the second write did not carry the new 01:00 window: {posts[-1][2]}")
        failed = True
    assert not failed, "test_run_writes_once_the_write_button_has_been_pressed"


def test_run_defers_startup_when_the_first_telemetry_poll_fails():
    """A first cycle with the telemetry endpoints down must not be reported as a success.

    automatic_config() runs on the first cycle ONLY, so a first tick that returns True with
    no telemetry permanently loses soc_max, battery_rate_max and every energy arg for the
    whole session, behind a single log warning. Returning False leaves ComponentBase's
    `first` flag set so the whole startup path is retried on its backoff.
    """
    failed = False
    s = RunSunsynk(control_enable=False)
    s.automatic = True
    configured = []

    async def fake_device_list():
        """Discovery works; only telemetry is down."""
        s.device_list = ["INV1"]
        return ["INV1"]

    async def fake_device_data(sn):
        """Every telemetry endpoint is briefly unavailable."""
        return {}

    async def fake_detail(sn):
        """Static detail is fine, so only telemetry is missing."""
        return {"ratePower": 8000}

    async def fake_settings(sn):
        """The settings read is fine, so only telemetry is missing."""
        return {"sn": sn, "batteryLowCap": "10"}

    async def fake_automatic_config():
        """Record an automatic_config that must not run without telemetry."""
        configured.append(True)

    with (
        patch.object(s, "fetch_token", side_effect=_fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list),
        patch.object(s, "fetch_device_detail", side_effect=fake_detail),
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "automatic_config", side_effect=fake_automatic_config),
    ):
        result = run_async_local(s.run(0, True))
    if result is not False:
        print(f"ERROR: a first cycle with no telemetry should return False, got {result!r}")
        failed = True
    if configured:
        print("ERROR: automatic_config ran on a first cycle that had no telemetry at all")
        failed = True
    assert not failed, "test_run_defers_startup_when_the_first_telemetry_poll_fails"


def test_refresh_static_keeps_known_inverters_on_a_failed_discovery():
    """A transient discovery failure must not empty device_list or poison the cache.

    This tier re-runs every 8 hours in a long-lived process. Assigning the empty result
    unconditionally, then marking the tier fresh and saving, wrote {'device_list': []} to
    disk and stamped it fresh - taking a working component down until the next success and
    across the next restart. Absence of a result is not a result (deye.py refuses the same).
    """
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    saved = []

    async def fake_discovery_fails():
        """Simulate the discovery endpoint returning nothing this cycle."""
        s.device_list = []
        return []

    async def fake_save_cache(name, data):
        """Record any cache write."""
        saved.append(name)

    with patch.object(s, "get_device_list", side_effect=fake_discovery_fails), patch.object(s, "save_cache", side_effect=fake_save_cache):
        run_async_local(s.refresh_static())
    if s.device_list != ["INV1", "INV2"]:
        print(f"ERROR: a failed discovery wiped device_list, got {s.device_list}")
        failed = True
    if saved:
        print(f"ERROR: a failed discovery wrote the cache: {saved}")
        failed = True
    if not s.tier_expired("static", SUNSYNK_TTL_STATIC):
        print("ERROR: a failed discovery marked the static tier fresh, so it will not retry for a full TTL")
        failed = True
    assert not failed, "test_refresh_static_keeps_known_inverters_on_a_failed_discovery"


def test_run_isolates_one_inverters_failure_from_the_rest():
    """One inverter raising must not cost the others their poll, nor skip the publish step.

    Without per-serial isolation a single exception on INV1 aborts the whole tick: INV2 is
    never polled, publish_data() never runs and update_success_timestamp() is never called,
    so components.is_alive() reports the whole component down over one bad inverter.
    """
    failed = False
    s = RunSunsynk(control_enable=True)
    s.device_list = ["INV1", "INV2"]
    s.mark_refreshed("static")
    polled = []
    published = []

    async def fake_device_data(sn):
        """INV1's telemetry endpoint raises; INV2's is fine."""
        if sn == "INV1":
            raise ValueError("simulated telemetry failure")
        polled.append(sn)
        s.device_values[sn] = {"soc": 50}
        return {"soc": 50}

    async def fake_settings(sn):
        """INV1's settings read raises; INV2's is fine."""
        if sn == "INV1":
            raise ValueError("simulated settings failure")
        return {"sn": sn, "batteryLowCap": "10"}

    async def fake_publish_data():
        """Record that the publish step still ran."""
        published.append(True)

    with (
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "publish_data", side_effect=fake_publish_data),
    ):
        result = run_async_local(s.run(0, False))
    if polled != ["INV2"]:
        print(f"ERROR: INV2 was not polled after INV1 failed, polled={polled}")
        failed = True
    if not published:
        print("ERROR: publish_data was skipped because one inverter failed")
        failed = True
    if result is not True:
        print(f"ERROR: a tick where one of two inverters failed should still complete, got {result!r}")
        failed = True
    assert not failed, "test_run_isolates_one_inverters_failure_from_the_rest"


def test_export_limit_is_auto_mapped_from_the_export_cap():
    """export_limit must be mapped, or Predbat plans exports the inverter clips.

    inverter.py defaults export_limit to 99999W. pvMaxLimit is the export cap (Sunsynk
    documents the app's "Inverter Power Limiter" as limiting export), so it is mapped, while
    inverter_limit stays on the hardware rating.
    """
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {"soc": 50, "capacity": 200, "chargeVolt": 58.4, "chargeCurrentLimit": 216}
    s.device_energy["INV1"] = {leaf: 1.0 for leaf in ("pv_today", "import_today", "export_today", "load_today", "battery_charge_today", "battery_discharge_today")}
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"batteryLowCap": "20", "pvMaxLimit": "7000"}
    run_async_local(s.automatic_config())
    for arg in ("inverter_limit", "export_limit"):
        if arg not in s.args_set:
            print(f"ERROR: {arg} was not mapped")
            failed = True
    if s.inverter_limit("INV1") != 8000.0:
        print(f"ERROR: inverter_limit should be the rating 8000, got {s.inverter_limit('INV1')}")
        failed = True
    if s.export_limit("INV1") != 7000.0:
        print(f"ERROR: export_limit should be the cap 7000, got {s.export_limit('INV1')}")
        failed = True
    assert not failed, "test_export_limit_is_auto_mapped_from_the_export_cap"


def test_sign_flags_are_claimed_not_inherited():
    """Sunsynk owns the invert flags, so another component cannot flip its sign.

    base.args is shared and NOT namespaced per inverter type. teslemetry and fox both set
    grid_power_invert True for their own hardware, quite correctly - but that leaves the key
    set for every inverter index, and a Sunsynk inverter that never claimed it inherited the
    flip. inverter.py then negated an already-correct sensor, so an export read as an import
    and the power-flow arrow pointed the wrong way on any install running both.

    All three are False because publish_data already emits Predbat's conventions: grid
    negative on import, battery positive on discharge, load positive.
    """
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    s.device_rated_power = {"INV1": 8000.0, "INV2": 8000.0}
    with patch.object(s, "battery_capacity", return_value=10.0), patch.object(s, "battery_rate_max", return_value=3000.0), patch.object(s, "export_limit", return_value=7000.0):
        run_async_local(s.automatic_config())
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        if flag not in s.args_set:
            print(f"ERROR: {flag} was never set, so it is inherited from whatever else configured the install")
            failed = True
        elif s.args_set[flag] != [False, False]:
            print(f"ERROR: {flag} = {s.args_set[flag]}, expected [False, False] - one entry per inverter")
            failed = True
    assert not failed, "test_sign_flags_are_claimed_not_inherited"


def test_apps_yaml_credentials_loader():
    """The standalone CLI's --apps-yaml loader builds a usable account or refuses precisely.

    This is what lets the CLI drive a real oauth account for the live TOU round trip, where
    there is a bearer token and no password at all. A bug here either half-builds a client
    that fails at login with a confusing message, or silently drops the serial.
    """
    failed = False
    import os
    import tempfile

    import yaml as yaml_module

    def write(folder, name, payload):
        """Write a YAML fixture and return its path."""
        path = os.path.join(folder, name)
        with open(path, "w") as handle:
            yaml_module.safe_dump(payload, handle)
        return path

    with tempfile.TemporaryDirectory() as folder:
        oauth = write(folder, "oauth.yaml", {"pred_bat": {"sunsynk_auth_method": "oauth", "sunsynk_key": "token-abc", "sunsynk_token_hash": "hash-abc", "sunsynk_inverter_sn": ["2211093089"], "sunsynk_token_expires_at": "2026-09-19T22:11:54.287+00:00"}})
        credentials = load_apps_yaml_credentials(oauth)
        expected = {"auth_method": "oauth", "key": "token-abc", "token_hash": "hash-abc", "inverter_sn": ["2211093089"], "token_expires_at": "2026-09-19T22:11:54.287+00:00"}
        if credentials != expected:
            print(f"ERROR: oauth config loaded as {credentials}, expected {expected}")
            failed = True

        # A YAML-parsed timestamp must come back as the string _parse_expiry wants, not a
        # datetime - safe_load turns an unquoted ISO date into one.
        from datetime import datetime as real_datetime

        typed = write(folder, "typed.yaml", {"pred_bat": {"sunsynk_auth_method": "oauth", "sunsynk_key": "token-abc", "sunsynk_inverter_sn": ["2211093089"], "sunsynk_token_expires_at": real_datetime(2026, 9, 19, 22, 11, 54)}})
        if not isinstance(load_apps_yaml_credentials(typed).get("token_expires_at"), str):
            print("ERROR: a YAML-typed expiry must be handed on as a string")
            failed = True

        # A bare mapping (a hand-trimmed file with no pred_bat wrapper) is accepted too.
        bare = write(folder, "bare.yaml", {"sunsynk_auth_method": "oauth", "sunsynk_key": "token-abc", "sunsynk_inverter_sn": "2211093089"})
        if load_apps_yaml_credentials(bare).get("inverter_sn") != ["2211093089"]:
            print("ERROR: a bare mapping with a scalar serial should load as a one-element list")
            failed = True

        # Each way of being unusable must refuse with its own reason, never half-build.
        refusals = {
            "no_serial.yaml": {"pred_bat": {"sunsynk_auth_method": "oauth", "sunsynk_key": "token-abc"}},
            "oauth_no_token.yaml": {"pred_bat": {"sunsynk_auth_method": "oauth", "sunsynk_inverter_sn": ["2211093089"]}},
            "password_incomplete.yaml": {"pred_bat": {"sunsynk_username": "someone@example.com", "sunsynk_inverter_sn": ["2211093089"]}},
        }
        for name, payload in refusals.items():
            try:
                load_apps_yaml_credentials(write(folder, name, payload))
                print(f"ERROR: {name} should have been refused")
                failed = True
            except ValueError:
                pass
        for name, path in (("missing", os.path.join(folder, "missing.yaml")), ("not a mapping", write(folder, "list.yaml", ["a", "b"]))):
            try:
                load_apps_yaml_credentials(path)
                print(f"ERROR: {name} should have been refused")
                failed = True
            except ValueError:
                pass

        # A password account is complete without any token.
        password = write(folder, "password.yaml", {"pred_bat": {"sunsynk_username": "someone@example.com", "sunsynk_password": "hunter2", "sunsynk_inverter_sn": ["2211093089"]}})
        if load_apps_yaml_credentials(password).get("username") != "someone@example.com":
            print("ERROR: a username/password account should load without a token")
            failed = True
    assert not failed, "test_apps_yaml_credentials_loader"


def test_tou_test_window_avoids_now():
    """The CLI's live TOU window never contains the current time.

    The programme --tou-test writes is meant to be stored, read back and removed without
    the inverter ever acting on it. A window covering the current minute would put the
    battery into a real grid-charge for the seconds before the restore.
    """
    failed = False
    for now_minutes in range(0, 24 * 60, 7):
        start, end = _tou_test_window(now_minutes)
        start_minutes = int(start[:2]) * 60 + int(start[3:])
        end_minutes = int(end[:2]) * 60 + int(end[3:])
        if start_minutes < end_minutes:
            inside = start_minutes <= now_minutes < end_minutes
        else:
            inside = now_minutes >= start_minutes or now_minutes < end_minutes
        if inside:
            print(f"ERROR: window {start}-{end} contains the current time {now_minutes // 60:02d}:{now_minutes % 60:02d}")
            failed = True
        if start == end:
            print(f"ERROR: window {start}-{end} is zero length and would be dropped")
            failed = True
    assert not failed, "test_tou_test_window_avoids_now"


def run_sunsynk_config_tests(my_predbat):
    """Run all Sunsynk configuration tests."""
    failed = False
    for name, fn in [
        ("inverter_def", test_inverter_def_registered),
        ("apps_yaml_credentials", test_apps_yaml_credentials_loader),
        ("tou_test_window", test_tou_test_window_avoids_now),
        ("component_registered", test_component_registered),
        ("apps_schema", test_apps_schema_keys),
        ("automatic_config", test_automatic_config_maps_control_entities),
        ("sign_flags_claimed", test_sign_flags_are_claimed_not_inherited),
        ("export_limit_mapped", test_export_limit_is_auto_mapped_from_the_export_cap),
        ("partial_capabilities", test_automatic_config_skips_partial_capabilities),
        ("ignore_pv", test_automatic_config_respects_ignore_pv),
        ("run_first_cycle", test_run_first_cycle_polls_and_publishes),
        ("catalogue_describes_each_inverter", test_sunsynk_catalogue_describes_each_inverter),
        ("catalogue_export_limit_only_on_evidence", test_sunsynk_catalogue_export_limit_only_on_evidence),
        ("catalogue_keeps_battery_ratings_through_a_partial_poll", test_sunsynk_catalogue_keeps_battery_ratings_through_a_partial_poll),
        ("catalogue_none_before_discovery", test_sunsynk_catalogue_none_before_discovery),
        ("catalogue_round_trips", test_sunsynk_catalogue_round_trips_through_validate_report),
        ("catalogue_filed_when_first_cycle_defers", test_sunsynk_catalogue_filed_when_first_cycle_defers),
        ("record_rebuilds_inverter_def_row", test_sunsynk_record_rebuilds_its_inverter_def_row),
        ("record_agrees_with_automatic_config", test_sunsynk_record_agrees_with_automatic_config),
        ("record_two_inverters", test_sunsynk_two_inverters_give_two_records_with_their_own_entities),
        ("record_ratings_are_configured_figures", test_sunsynk_record_ratings_are_the_configured_figures),
        ("record_keeps_pv_despite_ignore_pv", test_sunsynk_record_keeps_pv_despite_automatic_ignore_pv),
        ("record_mixed_fleet", test_sunsynk_mixed_fleet_records_describe_each_device),
        ("record_keeps_bindings_through_partial_poll", test_sunsynk_record_keeps_sensor_bindings_through_a_partial_poll),
        ("run_login_failure", test_run_returns_false_on_login_failure),
        ("run_no_inverters", test_run_returns_false_with_no_inverters),
        ("run_publishes_every_tick", test_run_publishes_schedule_every_tick_not_only_first),
        ("run_no_write_before_button", test_run_writes_nothing_before_the_write_button_is_pressed),
        ("run_first_cycle_preserves_plan", test_run_first_cycle_preserves_a_live_plan_in_the_control_entities),
        ("run_writes_after_button", test_run_writes_once_the_write_button_has_been_pressed),
        ("run_defers_without_telemetry", test_run_defers_startup_when_the_first_telemetry_poll_fails),
        ("refresh_static_keeps_devices", test_refresh_static_keeps_known_inverters_on_a_failed_discovery),
        ("run_isolates_inverter_failure", test_run_isolates_one_inverters_failure_from_the_rest),
        ("refresh_config_logs_external_changes", test_refresh_config_logs_external_changes),
        ("refresh_config_skips_save_on_total_failure", test_refresh_config_skips_save_when_every_read_fails),
        ("refresh_config_saves_on_partial_success", test_refresh_config_saves_when_any_read_succeeds),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for the shared MockBase used by the standalone command-line harnesses.
"""

from datetime import datetime, timezone

from component_base import ComponentBase
from mock_base import MockBase


class _ContractProbeComponent(ComponentBase):
    """Minimal concrete ComponentBase subclass used only to exercise the base contract."""

    def initialize(self, **kwargs):
        """Do nothing; this probe only needs the base ComponentBase wiring, not extra state."""
        pass


def test_mock_base_attribute_superset(my_predbat):
    """Every attribute ComponentBase dereferences off self.base is present after construction."""
    base = MockBase()
    for name in (
        "local_tz",
        "now_utc",
        "now_utc_exact",
        "midnight_utc",
        "minutes_now",
        "prefix",
        "args",
        "entities",
        "config_root",
        "plan_interval_minutes",
        "fatal_error",
        "had_errors",
        "components",
        "num_cars",
        "currency_symbols",
        "arg_errors",
    ):
        assert hasattr(base, name), f"MockBase is missing attribute {name}"
    assert base.prefix == "predbat", "prefix should default to predbat"
    assert base.components is None, "components must be None so ComponentBase.storage resolves to None"
    assert base.fatal_error is False, "fatal_error should start False"
    assert base.had_errors is False, "had_errors should start False"
    assert base.config_root == "./temp_predbat", "config_root should use the documented default"
    print("PASS: MockBase exposes the full base attribute superset")
    return False


def test_mock_base_covers_component_base_contract(my_predbat):
    """MockBase must satisfy every property and delegated method ComponentBase exposes.

    test_mock_base_attribute_superset only checks that MockBase's attribute list mirrors this
    file's own hardcoded copy of the ComponentBase surface - it is a change-detector, not a
    regression test, because both lists drift together. This test instead binds a real
    ComponentBase subclass to a MockBase and exercises the actual properties and delegate
    methods ComponentBase defines, so it fails with an AttributeError if someone adds a new
    self.base.<something> dereference to component_base.py that the mock does not cover.
    """
    component = _ContractProbeComponent(MockBase())

    # Properties defined on ComponentBase that read through to self.base.
    assert component.currency_symbols == "£p", "currency_symbols property failed"
    assert component.arg_errors == {}, "arg_errors property failed"
    assert component.now_utc is not None, "now_utc property failed"
    assert component.midnight_utc is not None, "midnight_utc property failed"
    assert component.now_utc_exact is not None, "now_utc_exact property failed"
    assert isinstance(component.minutes_now, int), "minutes_now property failed"
    assert component.plan_interval_minutes == 30, "plan_interval_minutes property failed"
    assert component.num_cars == 0, "num_cars property failed"
    assert component.config_root == "./temp_predbat", "config_root property failed"
    assert component.storage is None, "storage property failed (components is None on MockBase)"
    assert component.fatal_error is False, "fatal_error property failed"

    # Methods ComponentBase delegates straight through to self.base.
    assert component.get_arg("missing_key", "fallback") == "fallback", "get_arg delegate failed"
    component.set_arg("probe_key", "probe_value")
    assert component.get_arg("probe_key") == "probe_value", "set_arg delegate failed"
    component.dashboard_item("sensor.predbat_probe", "on", {"friendly_name": "Probe"})
    assert component.get_ha_config("anything", "fallback") == "fallback", "get_ha_config delegate failed"
    assert component.get_state_wrapper("sensor.predbat_probe") == "on", "get_state_wrapper delegate failed"
    component.set_state_wrapper("sensor.predbat_probe2", "off")
    assert component.get_state_wrapper("sensor.predbat_probe2") == "off", "set_state_wrapper delegate failed"
    assert component.get_history_wrapper("sensor.predbat_probe") is None, "get_history_wrapper delegate failed"
    component.call_notify("probe notification")
    component.log("probe log message")

    print("PASS: MockBase covers the full ComponentBase property/delegate contract")
    return False


def test_mock_base_config_root_and_local_tz_overrides(my_predbat):
    """config_root and local_tz are constructor-overridable, as the axle/gecloud/octopus/solax subclasses need."""
    base = MockBase(config_root="./temp_example")
    assert base.config_root == "./temp_example", "config_root override was ignored"
    utc_base = MockBase(local_tz=timezone.utc)
    assert utc_base.local_tz == timezone.utc, "local_tz override was ignored"
    assert utc_base.now_utc.tzinfo == timezone.utc, "now_utc should use the supplied timezone"
    print("PASS: MockBase honours config_root and local_tz overrides")
    return False


def test_mock_base_midnight_utc_is_aware(my_predbat):
    """midnight_utc is timezone-aware so now_utc - midnight_utc does not raise (fox/kraken/solis had naive values)."""
    base = MockBase()
    assert base.midnight_utc.tzinfo is not None, "midnight_utc must be timezone-aware"
    delta = base.now_utc - base.midnight_utc
    assert delta.total_seconds() >= 0, "now_utc should not precede midnight"
    assert base.midnight_utc.hour == 0, "midnight_utc should be at hour zero"
    assert base.midnight_utc.minute == 0, "midnight_utc should be at minute zero"
    print("PASS: MockBase midnight_utc is timezone-aware")
    return False


def test_mock_base_kwargs_populate_args(my_predbat):
    """Surplus kwargs land in self.args, covering the KrakenMockBase(user_id=...) call site."""
    base = MockBase(user_id="user-123")
    assert base.args.get("user_id") == "user-123", "kwargs should be stored into args"
    print("PASS: MockBase stores surplus kwargs into args")
    return False


def test_mock_base_none_kwargs_are_skipped(my_predbat):
    """A None-valued kwarg is not stored, matching kraken's 'if user_id:' guard and oauth_mixin's args.get(key, '')."""
    base = MockBase(user_id=None)
    assert "user_id" not in base.args, "None-valued kwargs must not be stored"
    assert base.args.get("user_id", "") == "", "absent user_id must fall back to the empty-string default"
    falsy = MockBase(control_enable=False)
    assert falsy.args.get("control_enable") is False, "a legitimate False value must still be stored"
    print("PASS: MockBase skips None kwargs but keeps False")
    return False


def test_mock_base_arg_round_trip(my_predbat):
    """set_arg persists into args and get_arg reads it back; unset keys return the caller's default."""
    base = MockBase()
    assert base.get_arg("missing_key", "fallback") == "fallback", "unset keys should return the default"
    assert base.get_arg("set_read_only", False) is False, "unset boolean should return the supplied default"
    base.set_arg("set_read_only", True)
    assert base.get_arg("set_read_only", False) is True, "set_arg value should be readable via get_arg"
    print("PASS: MockBase get_arg/set_arg round-trip")
    return False


def test_mock_base_set_arg_none_deletes_key(my_predbat):
    """set_arg(key, None) must delete the key, matching userinterface.py's Fetch.set_arg.

    gecloud.py makes several set_arg(key, None) calls expecting the key to disappear so a
    later get_arg(key, default) falls back to the caller's default rather than returning None.
    """
    base = MockBase()
    base.set_arg("probe_key", "probe_value")
    assert base.get_arg("probe_key", "fallback") == "probe_value", "set_arg should have stored the value"
    base.set_arg("probe_key", None)
    assert base.get_arg("probe_key", "fallback") == "fallback", "set_arg(key, None) should delete the key, not store None"
    assert "probe_key" not in base.args, "the deleted key must not remain in args"
    print("PASS: MockBase set_arg(key, None) deletes the key")
    return False


def test_mock_base_reexport_identity(my_predbat):
    """The five plain re-export modules must expose the identical shared MockBase object.

    deye, enphase, fox and solis are not otherwise exercised anywhere (teslemetry is covered
    incidentally by test_teslemetry.py), so nothing else would catch a botched edit to one of
    those `from mock_base import MockBase` lines - e.g. accidentally defining a local class
    that shadows the shared one.
    """
    from deye import MockBase as DeyeMockBase
    from enphase import MockBase as EnphaseMockBase
    from fox import MockBase as FoxMockBase
    from solis import MockBase as SolisMockBase
    from teslemetry import MockBase as TeslemetryMockBase

    for name, reexported in (
        ("deye", DeyeMockBase),
        ("enphase", EnphaseMockBase),
        ("fox", FoxMockBase),
        ("solis", SolisMockBase),
        ("teslemetry", TeslemetryMockBase),
    ):
        assert reexported is MockBase, f"{name}.MockBase should be the identical shared mock_base.MockBase object"

    from axle import MockBase as AxleMockBase
    from gecloud import MockBase as GECloudMockBase
    from octopus import MockBase as OctopusMockBase
    from sigenergy import MockBase as SigenergyMockBase
    from solax import MockBase as SolaxMockBase

    for name, subclass in (
        ("axle", AxleMockBase),
        ("gecloud", GECloudMockBase),
        ("octopus", OctopusMockBase),
        ("sigenergy", SigenergyMockBase),
        ("solax", SolaxMockBase),
    ):
        assert issubclass(subclass, MockBase), f"{name}.MockBase should be a subclass of the shared mock_base.MockBase"
        assert subclass is not MockBase, f"{name}.MockBase should be its own subclass, not a bare re-export"

    print("PASS: the eleven module MockBase names resolve to the shared class or a true subclass of it")
    return False


def test_mock_base_dashboard_item_does_not_mutate_attributes(my_predbat):
    """dashboard_item must not corrupt the caller's attributes dict when eliding the options list."""
    base = MockBase()
    attributes = {"options": ["a", "b", "c"], "friendly_name": "Test"}
    base.dashboard_item("select.predbat_test", state="a", attributes=attributes)
    assert attributes["options"] == ["a", "b", "c"], "dashboard_item mutated the caller's options list"
    stored = base.get_state_wrapper("select.predbat_test", raw=True)
    assert stored["attributes"]["options"] == ["a", "b", "c"], "the stored attributes were corrupted"
    assert stored["state"] == "a", "the stored state is wrong"
    print("PASS: MockBase dashboard_item leaves caller attributes intact")
    return False


def test_mock_base_dashboard_item_serialises_datetime(my_predbat):
    """dashboard_item serialises non-JSON-native attribute values instead of raising TypeError."""
    base = MockBase()
    base.dashboard_item("sensor.predbat_test", state="ok", attributes={"last_updated": datetime.now()})
    assert base.get_state_wrapper("sensor.predbat_test") == "ok", "state should still be stored"
    print("PASS: MockBase dashboard_item serialises datetime attributes")
    return False


def test_mock_base_state_wrapper_paths(my_predbat):
    """get_state_wrapper covers the raw, attribute and default-fallback paths."""
    base = MockBase()
    base.set_state_wrapper("sensor.predbat_test", "42", attributes={"unit_of_measurement": "kWh"})
    assert base.get_state_wrapper("sensor.predbat_test") == "42", "plain state lookup failed"
    assert base.get_state_wrapper("sensor.predbat_test", attribute="unit_of_measurement") == "kWh", "attribute lookup failed"
    assert base.get_state_wrapper("sensor.predbat_test", raw=True)["state"] == "42", "raw lookup failed"
    assert base.get_state_wrapper("sensor.predbat_missing", default="none") == "none", "missing entity should return the default"
    assert base.get_state_wrapper("sensor.predbat_test", attribute="absent", default="fallback") == "fallback", "missing attribute should return the default"
    print("PASS: MockBase get_state_wrapper handles raw/attribute/default paths")
    return False


def test_mock_base_set_state_wrapper_accepts_both_kwargs(my_predbat):
    """set_state_wrapper accepts both app= and required_unit=, since the modules disagree on which they pass."""
    base = MockBase()
    base.set_state_wrapper("sensor.predbat_one", "1", attributes={}, app="predbat")
    base.set_state_wrapper("sensor.predbat_two", "2", attributes={}, required_unit="kWh")
    assert base.get_state_wrapper("sensor.predbat_one") == "1", "app= form failed"
    assert base.get_state_wrapper("sensor.predbat_two") == "2", "required_unit= form failed"
    print("PASS: MockBase set_state_wrapper accepts app and required_unit")
    return False


def test_mock_base_record_status_tracks_errors(my_predbat):
    """record_status sets had_errors when told to, so gecloud's guarded call reflects failures."""
    base = MockBase()
    base.record_status("All good")
    assert base.had_errors is False, "a clean status must not set had_errors"
    base.record_status("Something broke", debug="url", had_errors=True)
    assert base.had_errors is True, "had_errors should be set when reported"
    print("PASS: MockBase record_status tracks the error flag")
    return False


def test_mock_base_no_ha_helpers(my_predbat):
    """get_ha_config returns the caller's default and get_history_wrapper returns None, matching a no-HA run."""
    base = MockBase()
    assert base.get_ha_config("anything", "fallback") == "fallback", "get_ha_config should return the default"
    assert base.get_history_wrapper("sensor.predbat_test") is None, "get_history_wrapper should return None with no HA interface"
    print("PASS: MockBase HA helpers degrade cleanly")
    return False


def test_mock_base_module_subclasses(my_predbat):
    """The five subclassing modules keep their distinguishing state on top of the shared base."""
    from axle import MockBase as AxleMockBase
    from gecloud import MockBase as GECloudMockBase
    from octopus import MockBase as OctopusMockBase
    from sigenergy import MockBase as SigenergyMockBase
    from solax import MockBase as SolaxMockBase

    assert AxleMockBase().config_root == "./temp_axle", "axle config_root is wrong"
    assert OctopusMockBase().config_root == "./temp_octopus", "octopus config_root is wrong"

    gecloud_base = GECloudMockBase()
    assert gecloud_base.config_root == "./temp_gecloud", "gecloud config_root is wrong"
    assert gecloud_base.ha_interface is not None, "gecloud must supply a mock ha_interface"
    assert hasattr(gecloud_base.ha_interface, "set_state_external"), "gecloud ha_interface needs set_state_external"

    assert SolaxMockBase().local_tz == timezone.utc, "solax must keep using UTC"

    read_only = SigenergyMockBase(readonly=True)
    assert read_only.get_state_wrapper("switch.predbat_set_read_only") == "on", "sigenergy readonly=True should seed the switch on"
    writable = SigenergyMockBase(readonly=False)
    assert writable.get_state_wrapper("switch.predbat_set_read_only") == "off", "sigenergy readonly=False should seed the switch off"

    print("PASS: module MockBase subclasses keep their distinguishing state")
    return False


def test_mock_base_kraken_alias(my_predbat):
    """KrakenMockBase is the shared base and still accepts user_id, skipping it when None."""
    from kraken import KrakenMockBase

    assert KrakenMockBase(user_id="user-123").args.get("user_id") == "user-123", "user_id should be stored"
    assert "user_id" not in KrakenMockBase(user_id=None).args, "a None user_id must not be stored"
    print("PASS: KrakenMockBase preserves its user_id contract")
    return False


def test_mock_base_all(my_predbat):
    """Run all mock_base tests."""
    tests = [
        ("attribute_superset", test_mock_base_attribute_superset, "Full base attribute superset is present"),
        ("component_base_contract", test_mock_base_covers_component_base_contract, "MockBase satisfies the real ComponentBase property/delegate contract"),
        ("constructor_overrides", test_mock_base_config_root_and_local_tz_overrides, "config_root and local_tz are overridable"),
        ("midnight_aware", test_mock_base_midnight_utc_is_aware, "midnight_utc is timezone-aware"),
        ("kwargs_args", test_mock_base_kwargs_populate_args, "Surplus kwargs populate args"),
        ("none_kwargs", test_mock_base_none_kwargs_are_skipped, "None kwargs are skipped, False is kept"),
        ("arg_round_trip", test_mock_base_arg_round_trip, "get_arg/set_arg round-trip"),
        ("set_arg_none_deletes", test_mock_base_set_arg_none_deletes_key, "set_arg(key, None) deletes the key"),
        ("reexport_identity", test_mock_base_reexport_identity, "Re-export and subclass modules resolve to the shared MockBase"),
        ("dashboard_no_mutate", test_mock_base_dashboard_item_does_not_mutate_attributes, "dashboard_item does not mutate caller attributes"),
        ("dashboard_datetime", test_mock_base_dashboard_item_serialises_datetime, "dashboard_item serialises datetime attributes"),
        ("state_wrapper", test_mock_base_state_wrapper_paths, "get_state_wrapper raw/attribute/default paths"),
        ("state_wrapper_kwargs", test_mock_base_set_state_wrapper_accepts_both_kwargs, "set_state_wrapper accepts app and required_unit"),
        ("record_status", test_mock_base_record_status_tracks_errors, "record_status tracks had_errors"),
        ("ha_helpers", test_mock_base_no_ha_helpers, "HA helpers degrade cleanly"),
        ("module_subclasses", test_mock_base_module_subclasses, "Module subclasses keep their distinguishing state"),
        ("kraken_alias", test_mock_base_kraken_alias, "KrakenMockBase alias preserves the user_id contract"),
    ]

    failed = []
    for name, test_func, description in tests:
        print(f"\n*** Running: {name} - {description} ***")
        try:
            result = test_func(my_predbat)
            if result:
                failed.append(name)
                print(f"FAILED: {name}")
        except Exception as e:
            failed.append(name)
            print(f"ERROR in {name}: {e}")

    if failed:
        print(f"\n*** {len(failed)} test(s) failed: {', '.join(failed)} ***")
        return True
    else:
        print(f"\n*** All {len(tests)} mock_base tests passed ***")
        return False

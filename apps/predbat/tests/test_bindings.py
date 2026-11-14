# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery bindings (bindings.py) - manual slot detection and the serial scan."""

from bindings import BINDING_SETTINGS, manual_slots, slot_serials


def test_manual_slot_type_and_control():
    """A slot with an inverter_type and a control entity is manual."""
    assert manual_slots({"num_inverters": 1, "inverter_type": ["GE"], "charge_rate": ["number.a_charge_rate"]}) == [{"slot": 0, "origin": "manual", "inverter_type": "GE"}]
    print("PASS: manual slot with type and control")
    return 0


def test_manual_slot_type_only():
    """A typed slot with no control method is free."""
    assert manual_slots({"num_inverters": 1, "inverter_type": ["GE"]})[0]["origin"] == "free"
    print("PASS: typed slot without control is free")
    return 0


def test_manual_slot_control_only():
    """A slot with control entities but no type is a dead slot, reported as free with inverter_type None."""
    assert manual_slots({"num_inverters": 1, "charge_rate": ["number.a_charge_rate"]}) == [{"slot": 0, "origin": "free", "inverter_type": None}]
    print("PASS: control without type is free")
    return 0


def test_manual_slot_scalar_type():
    """A scalar inverter_type is inverter 0's only."""
    slots = manual_slots({"num_inverters": 2, "inverter_type": "GE", "charge_rate": ["number.a", "number.b"]})
    assert [s["origin"] for s in slots] == ["manual", "free"], slots
    print("PASS: scalar type is slot 0's only")
    return 0


def test_manual_slot_givtcp_rest():
    """A givtcp_rest entry is a control method."""
    assert manual_slots({"inverter_type": ["GE"], "givtcp_rest": ["http://h:6345"]})[0]["origin"] == "manual"
    print("PASS: givtcp_rest is a control method")
    return 0


def test_manual_slot_service_api():
    """A control service is not per inverter: it counts for every typed slot."""
    slots = manual_slots({"num_inverters": 2, "inverter_type": ["MINE", "MINE"], "charge_start_service": {"service": "x.y"}})
    assert [s["origin"] for s in slots] == ["manual", "manual"], slots
    print("PASS: control service counts for every typed slot")
    return 0


def test_manual_slot_regex_binding():
    """A re: pattern on a modify setting is a control method (Review Focus 5)."""
    assert manual_slots({"inverter_type": ["GE"], "charge_rate": ["re:(number.givtcp_.*_charge_rate)"]})[0]["origin"] == "manual"
    print("PASS: re: binding is a control method")
    return 0


def test_slot_serials():
    """Serials are found case-insensitively in a slot's bound strings, and in ge_cloud_serial for slot 0 only."""
    apps = {"ge_cloud_serial": "CE1234G567"}
    args = {"charge_rate": ["number.ge_inverter_ce1234g567_battery_charge_power", "number.other"]}
    assert slot_serials(0, {}, args, ["ce1234g567"]) == ["ce1234g567"]  # case-insensitive entity match
    assert slot_serials(1, {}, args, ["ce1234g567"]) == []
    assert slot_serials(0, apps, {}, ["ce1234g567"]) == ["ce1234g567"]  # ge_cloud_serial from the snapshot, slot 0 only
    assert slot_serials(1, apps, {}, ["ce1234g567"]) == []
    assert slot_serials(0, {}, {"charge_rate": ["number.x_ab12_rate"]}, ["ab12"]) == []  # shorter than MIN_SERIAL_LENGTH
    print("PASS: slot serial scan")
    return 0


def test_slot_serials_skip_summed_settings():
    """A serial found only in a summed list is not matched: those lists are not indexed by slot."""
    args = {"charge_rate": ["number.a_charge_rate", "number.b_charge_rate"], "load_today": ["sensor.a_load_today", "sensor.ge_ce1234g567_load_today"]}
    assert slot_serials(1, {}, args, ["ce1234g567"]) == []  # compact list: index 1 may be another slot's entry or a user's extra
    assert slot_serials(0, {}, {"pv_power": ["sensor.ge_ce1234g567_pv_power"]}, ["ce1234g567"]) == []  # pv_power alone
    print("PASS: slot serial scan skips summed settings")
    return 0


def test_binding_settings_leave_tuning_alone():
    """No user tuning setting is owned by the bindings, so the coordinator never reads, writes or clears one."""
    tuning = {"inverter_battery_rate_min", "inverter_limit_charge", "inverter_limit_discharge", "inverter_limit_export", "inverter_limit_override", "battery_export_permitted", "soc_max_fallback", "inverter_reserve_max"}
    assert not tuning & BINDING_SETTINGS, sorted(tuning & BINDING_SETTINGS)
    assert "givtcp_rest" in BINDING_SETTINGS and "inverter_mode" in BINDING_SETTINGS
    print("PASS: binding settings leave tuning alone")
    return 0


def test_bindings_all(my_predbat=None):
    """Run every bindings test, returning the number of failures."""
    failures = 0
    failures += test_manual_slot_type_and_control()
    failures += test_manual_slot_type_only()
    failures += test_manual_slot_control_only()
    failures += test_manual_slot_scalar_type()
    failures += test_manual_slot_givtcp_rest()
    failures += test_manual_slot_service_api()
    failures += test_manual_slot_regex_binding()
    failures += test_slot_serials()
    failures += test_slot_serials_skip_summed_settings()
    failures += test_binding_settings_leave_tuning_alone()
    return failures

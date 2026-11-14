# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery bindings (bindings.py) - manual slot detection, the serial scan and building the bindings."""

import copy

from bindings import BINDING_SETTINGS, MAX_INVERTERS, build_bindings, manual_slots, slot_map, slot_serials


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


def _rec(serial, entities=None, inverter_type="GEC", serials=None):
    """A validated-shape GE Cloud inverter record for serial; inverter_type None makes it PV-only."""
    record = {"device_id": "gecloud:" + serial, "hardware_ids": {"serial": serial}}
    if inverter_type:
        record["inverter_type"] = inverter_type
    if serials:
        record["serials"] = list(serials)
    if entities:
        record["entities"] = entities
    return record


def _src(records, **options):
    """A source named gecloud carrying records, with the opt-in options given (defaults otherwise)."""
    return {
        "name": "gecloud",
        "records": records,
        "skip_settings": frozenset(options.get("skip_settings", ())),
        "keep_user_settings": frozenset(options.get("keep_user_settings", ())),
        "include_pv_only": options.get("include_pv_only", False),
    }


def _rw(entity_id):
    """A writable entity descriptor."""
    return {"entity_id": entity_id, "access": "rw"}


def _layout(bindings):
    """Each bindings entry as (origin, device_id, slot), in list order."""
    return [(entry["origin"], entry["device_id"], entry["slot"]) for entry in bindings["inverters"]]


def _manual_apps(*charge_rates, types=None):
    """An apps.yaml snapshot with one GE slot per charge_rate entity (types overrides the inverter_type list)."""
    return {"num_inverters": len(charge_rates), "inverter_type": list(types) if types is not None else ["GE"] * len(charge_rates), "charge_rate": list(charge_rates)}


def test_build_empty_install():
    """No apps.yaml at all: the record takes slot 0 with its entities and the source's options."""
    b, w = build_bindings({}, {}, [_src([_rec("ce1111g111", {"charge_rate": _rw("number.p_gecloud_ce1111g111_battery_charge_power")})], skip_settings={"load_today"}, keep_user_settings={"inverter_mode", "export_limit"})], {})
    assert [s["origin"] for s in b["inverters"]] == ["discovered"] and b["inverters"][0]["inverter_type"] == "GEC" and w == []
    assert _layout(b) == [("discovered", "gecloud:ce1111g111", 0)], _layout(b)
    assert b["inverters"][0] == {
        "slot": 0,
        "origin": "discovered",
        "source": "gecloud",
        "device_id": "gecloud:ce1111g111",
        "inverter_type": "GEC",
        "serials": ["ce1111g111"],
        "settings": {"charge_rate": _rw("number.p_gecloud_ce1111g111_battery_charge_power")},
        "skip_settings": ["load_today"],
        "keep_user_settings": ["export_limit", "inverter_mode"],
    }, b["inverters"][0]
    assert b["site"] == {"settings": {}, "extras": {}}, b["site"]
    print("PASS: build on an empty install")
    return 0


def test_build_nothing():
    """No apps.yaml, no sources and no previous bindings: an empty structure and no warnings."""
    b, w = build_bindings({}, {}, [], None)
    assert b == {"inverters": [], "site": {"settings": {}, "extras": {}}} and w == [], (b, w)
    assert slot_map(b) == {} and slot_map(None) == {} and slot_map({}) == {}
    print("PASS: build with nothing")
    return 0


def test_build_manual_plus_discovered():
    """A manual slot naming another inverter is left alone; the record takes slot 1."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    args = copy.deepcopy(apps)
    sources = [_src([_rec("ce1111g111", {"charge_rate": _rw("number.ce1111g111_rate")})])]
    inputs = copy.deepcopy((apps, args, sources))
    b, w = build_bindings(apps, args, sources, None)
    assert (apps, args, sources) == inputs  # writes nothing
    b["inverters"][1]["settings"]["charge_rate"]["entity_id"] = "number.changed"
    assert sources[0]["records"][0]["entities"]["charge_rate"]["entity_id"] == "number.ce1111g111_rate"  # descriptors are copies
    b, w = build_bindings(apps, args, sources, None)
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:ce1111g111", 1)], _layout(b)
    assert b["inverters"][0] == {"slot": 0, "origin": "manual", "source": None, "device_id": None, "inverter_type": "GE", "serials": [], "settings": {"charge_rate": {"entity_id": "number.givtcp_sa9999s999_charge_rate"}}}, b["inverters"][0]
    assert w == [], w
    print("PASS: manual slot kept, record in slot 1")
    return 0


def test_build_takeover():
    """A manual slot naming the record's serial is taken over, with one warning."""
    apps = _manual_apps("number.givtcp_CE1111G111_charge_rate")
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("CE1111G111")])], None)
    assert _layout(b) == [("discovered", "gecloud:CE1111G111", 0)], _layout(b)
    assert b["inverters"][0]["serials"] == ["ce1111g111"] and b["inverters"][0]["inverter_type"] == "GEC"
    assert w == ["Inverter slot 0: gecloud inverter ce1111g111 is also configured manually in apps.yaml - discovery is taking over this slot"], w
    # The next pass keeps the slot and, the apps.yaml slot still being there, warns the same way
    again, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("CE1111G111")])], b)
    assert again == b and w == ["Inverter slot 0: gecloud inverter ce1111g111 is also configured manually in apps.yaml - discovery is taking over this slot"], (again, w)
    print("PASS: takeover of a matching manual slot")
    return 0


def test_build_gateway_two_manual_slots():
    """A gateway record claiming the batteries in manual slots 0 and 1 takes slot 0 and clears slot 1."""
    apps = _manual_apps("number.givtcp_ce1111g111_charge_rate", "number.givtcp_ce2222g222_charge_rate")
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("gw1111g111", serials=["ce1111g111", "ce2222g222"])])], None)
    assert _layout(b) == [("discovered", "gecloud:gw1111g111", 0)], _layout(b)
    assert b["inverters"][0]["serials"] == ["ce1111g111", "ce2222g222", "gw1111g111"]
    assert w == [
        "Inverter slot 0: gecloud inverter ce1111g111 is also configured manually in apps.yaml - discovery is taking over this slot",
        "Inverter slot 1: also names ce2222g222, already configured in slot 0 - clearing this slot",
    ], w
    print("PASS: gateway collapses two manual slots")
    return 0


def test_build_dead_slot_taken():
    """A control-only slot 0 (no inverter_type) is free: the record takes it without a warning."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate", types=[])
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("ce1111g111")])], None)
    assert _layout(b) == [("discovered", "gecloud:ce1111g111", 0)], _layout(b)
    assert w == [], w
    print("PASS: dead slot taken")
    return 0


def test_build_fifth_inverter():
    """With all four slots manual, a record that matches none of them is not configured."""
    apps = _manual_apps("number.a_charge_rate", "number.b_charge_rate", "number.c_charge_rate", "number.d_charge_rate")
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("ce1111g111")])], None)
    assert _layout(b) == [("manual", None, 0), ("manual", None, 1), ("manual", None, 2), ("manual", None, 3)], _layout(b)
    assert w == ["gecloud inverter gecloud:ce1111g111 cannot be configured - all {} inverter slots are in use".format(MAX_INVERTERS)], w
    print("PASS: fifth inverter not configured")
    return 0


def test_build_sticky():
    """A record already holding a slot keeps it whatever the report order; a new record takes the lowest free slot."""
    first, _ = build_bindings({}, {}, [_src([_rec("x11111"), _rec("b11111")])], None)
    assert slot_map(first) == {"gecloud:x11111": 0, "gecloud:b11111": 1}, slot_map(first)
    b, w = build_bindings({}, {}, [_src([_rec("b11111"), _rec("a11111")])], first)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(b)
    assert w == [], w
    print("PASS: sticky slots")
    return 0


def test_build_release():
    """A report that still drives an inverter but no longer has a record releases that record's slot."""
    first, _ = build_bindings({}, {}, [_src([_rec("a11111"), _rec("b11111")])], None)
    b, w = build_bindings({}, {}, [_src([_rec("a11111")])], first)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0)], _layout(b)
    assert w == [], w
    print("PASS: slot released")
    return 0


def test_build_no_release_on_empty_report():
    """A report with no driven inverter keeps the source's slots exactly as they were, with a warning."""
    first, _ = build_bindings({}, {}, [_src([_rec("a11111", {"charge_rate": _rw("number.a_rate")})])], None)
    for records in ([], [_rec("pv1111", {"pv_power": {"entity_id": "sensor.pv_power", "access": "r"}}, inverter_type=None)]):
        b, w = build_bindings({}, {"charge_rate": ["number.something_else"]}, [_src(records)], first)
        assert _layout(b) == [("discovered", "gecloud:a11111", 0)], _layout(b)
        assert b["inverters"][0] == first["inverters"][0], b["inverters"][0]
        assert w == ["gecloud reported no inverter to configure - keeping its existing slots"], w
    print("PASS: no release on an empty report")
    return 0


def test_build_retained_absent_source():
    """A source absent from sources keeps its slots copied verbatim from previous, summed settings included, with no warning."""
    temp = {"entity_id": "sensor.a_temp_history", "access": "r"}
    entities = {"charge_rate": _rw("number.a_rate"), "pv_power": {"entity_id": "sensor.a_pv_power", "access": "r"}, "load_today": {"entity_id": "sensor.a_load_today", "access": "r"}, "battery_temperature_history": temp}
    first, _ = build_bindings({}, {}, [_src([_rec("a11111", entities), _rec("b11111")], skip_settings={"load_today"})], None)
    b, w = build_bindings({}, {"pv_power": ["sensor.other", "sensor.more"], "load_today": ["sensor.other_load"]}, [], first)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(b)
    assert b["inverters"] == first["inverters"], b["inverters"]
    assert b["site"]["settings"] == {"battery_temperature_history": temp}, b["site"]  # no fresh record to take it from: kept from previous
    assert b["inverters"][0]["settings"]["pv_power"] == {"entity_id": "sensor.a_pv_power", "access": "r"}
    assert b["inverters"][0] is not first["inverters"][0] and b["inverters"][0]["settings"] is not first["inverters"][0]["settings"]
    assert w == [], w
    print("PASS: absent source keeps its slots verbatim")
    return 0


def test_build_hole_warns():
    """A free slot below a configured one, with nothing to fill it, is warned about."""
    apps = _manual_apps("number.a_charge_rate", "number.b_charge_rate", "number.c_charge_rate", types=["GE", "", "GE"])
    b, w = build_bindings(apps, copy.deepcopy(apps), [], None)
    assert _layout(b) == [("manual", None, 0), ("free", None, 1), ("manual", None, 2)], _layout(b)
    assert b["inverters"][1] == {"slot": 1, "origin": "free", "source": None, "device_id": None, "inverter_type": None, "serials": [], "settings": {}}, b["inverters"][1]
    assert w == ["Inverter slot 1 is empty below a configured inverter - it has no inverter_type and Predbat will stop on it"], w
    print("PASS: hole warned about")
    return 0


def test_build_site_setting():
    """battery_temperature_history comes from the lowest-numbered discovered slot and leaves the slots' settings."""
    temp_a = {"entity_id": "sensor.a_temp_history", "access": "r"}
    temp_b = {"entity_id": "sensor.b_temp_history", "access": "r"}
    rec_a = _rec("a11111", {"charge_rate": _rw("number.a_rate"), "battery_temperature_history": temp_a})
    rec_b = _rec("b11111", {"charge_rate": _rw("number.b_rate"), "battery_temperature_history": temp_b})
    b, w = build_bindings({}, {}, [_src([rec_a, rec_b])], None)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(b)
    assert b["site"]["settings"] == {"battery_temperature_history": temp_a}, b["site"]
    assert all("battery_temperature_history" not in entry["settings"] for entry in b["inverters"])
    assert w == [], w
    # The lowest slot wins, not the first record reported
    first, _ = build_bindings({}, {}, [_src([rec_b, rec_a])], None)
    b, w = build_bindings({}, {}, [_src([rec_a, rec_b])], first)
    assert _layout(b) == [("discovered", "gecloud:b11111", 0), ("discovered", "gecloud:a11111", 1)], _layout(b)
    assert b["site"]["settings"] == {"battery_temperature_history": temp_b}, b["site"]
    assert w == [], w
    print("PASS: site setting from the lowest slot")
    return 0


def test_build_pv_only():
    """A record with no inverter_type takes no slot; with include_pv_only its pv_power and pv_today go to site extras."""
    pv = _rec("pv1111", {"pv_power": {"entity_id": "sensor.pv1111_pv_power", "access": "r"}, "pv_today": {"entity_id": "sensor.pv1111_pv_today", "access": "r"}}, inverter_type=None)
    b, w = build_bindings({}, {}, [_src([_rec("a11111"), pv])], None)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0)], _layout(b)
    assert b["site"]["extras"] == {}, b["site"]
    assert w == [], w
    b, w = build_bindings({}, {}, [_src([_rec("a11111"), pv], include_pv_only=True)], None)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0)], _layout(b)
    assert b["site"]["extras"] == {"pv_power": ["sensor.pv1111_pv_power"], "pv_today": ["sensor.pv1111_pv_today"]}, b["site"]
    assert w == [], w
    print("PASS: PV-only records")
    return 0


def test_build_user_extras():
    """apps.yaml summed entries at or above num_inverters are the user's extras, ahead of PV-only entries."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["pv_power"] = ["sensor.manual_pv_power", "sensor.extra_pv_power"]
    pv = _rec("pv1111", {"pv_power": {"entity_id": "sensor.pv1111_pv_power", "access": "r"}}, inverter_type=None)
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("a11111"), pv], include_pv_only=True)], None)
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:a11111", 1)], _layout(b)
    assert b["site"]["extras"] == {"pv_power": ["sensor.extra_pv_power", "sensor.pv1111_pv_power"]}, b["site"]
    assert b["inverters"][0]["settings"]["pv_power"] == {"entity_id": "sensor.manual_pv_power"}
    assert w == [], w
    print("PASS: user extras ahead of PV-only")
    return 0


def test_build_summed_from_apps_yaml():
    """A manual slot's summed entry and the user's extras come from apps.yaml, not from a live list replay has rewritten."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["load_today"] = ["sensor.manual_load_today", "sensor.extra_load_today"]
    args = copy.deepcopy(apps)
    args["load_today"] = ["sensor.gecloud_load_today", "sensor.manual_load_today", "sensor.extra_load_today", "sensor.stray"]
    b, w = build_bindings(apps, args, [_src([_rec("a11111", {"load_today": {"entity_id": "sensor.gecloud_load_today", "access": "r"}})])], None)
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:a11111", 1)], _layout(b)
    assert b["inverters"][0]["settings"]["load_today"] == {"entity_id": "sensor.manual_load_today"}, b["inverters"][0]
    assert b["site"]["extras"] == {"load_today": ["sensor.extra_load_today"]}, b["site"]
    assert w == [], w
    # A taken-over slot contributes nothing from apps.yaml for its index; its extras still survive
    apps = _manual_apps("number.givtcp_a11111_charge_rate")
    apps["load_today"] = ["sensor.manual_load_today", "sensor.extra_load_today"]
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("a11111")])], None)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0)], _layout(b)
    assert b["inverters"][0]["settings"] == {} and b["site"]["extras"] == {"load_today": ["sensor.extra_load_today"]}, b
    assert w == ["Inverter slot 0: gecloud inverter a11111 is also configured manually in apps.yaml - discovery is taking over this slot"], w
    print("PASS: summed settings read from apps.yaml")
    return 0


def test_build_manual_settings_shape():
    """A manual slot's settings: entity ids as entity_id, literals as value, no access, only binding settings."""
    apps = {"num_inverters": 1, "inverter_type": ["GE"], "charge_rate": ["number.a"], "reserve": [0], "givtcp_rest": ["http://h:6345"], "inverter_limit_charge": [3000]}
    b, w = build_bindings(apps, copy.deepcopy(apps), [], None)
    assert _layout(b) == [("manual", None, 0)], _layout(b)
    assert b["inverters"][0]["settings"] == {"charge_rate": {"entity_id": "number.a"}, "reserve": {"value": 0}, "givtcp_rest": {"value": "http://h:6345"}}, b["inverters"][0]["settings"]
    assert w == [], w
    print("PASS: manual settings shape")
    return 0


def test_build_record_without_serial_or_entities():
    """A record with no hardware serial and no entities is still allocated, with empty serials and settings."""
    b, w = build_bindings({}, {}, [_src([{"device_id": "gecloud:bare", "inverter_type": "GEC"}])], None)
    assert _layout(b) == [("discovered", "gecloud:bare", 0)], _layout(b)
    assert b["inverters"][0]["serials"] == [] and b["inverters"][0]["settings"] == {}
    assert w == [], w
    print("PASS: record without serial or entities")
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
    failures += test_build_empty_install()
    failures += test_build_nothing()
    failures += test_build_manual_plus_discovered()
    failures += test_build_takeover()
    failures += test_build_gateway_two_manual_slots()
    failures += test_build_dead_slot_taken()
    failures += test_build_fifth_inverter()
    failures += test_build_sticky()
    failures += test_build_release()
    failures += test_build_no_release_on_empty_report()
    failures += test_build_retained_absent_source()
    failures += test_build_hole_warns()
    failures += test_build_site_setting()
    failures += test_build_pv_only()
    failures += test_build_user_extras()
    failures += test_build_summed_from_apps_yaml()
    failures += test_build_manual_settings_shape()
    failures += test_build_record_without_serial_or_entities()
    return failures

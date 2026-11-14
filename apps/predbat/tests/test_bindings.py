# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery bindings (bindings.py) - manual slot detection, the serial scan and building the bindings."""

import copy

from bindings import BINDING_SETTINGS, MAX_INVERTERS, build_bindings, desired_args, manual_slots, replay_bindings, slot_map, slot_serials


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


def test_manual_slot_needs_a_modify_binding():
    """Only a bound entity on a setting Predbat writes is a control method: a read-only binding or a literal on a modify setting leaves a typed slot free."""
    assert manual_slots({"inverter_type": ["GE"], "soc_percent": ["sensor.givtcp_soc"]})[0]["origin"] == "free"  # read-only setting
    assert manual_slots({"inverter_type": ["GE"], "charge_rate": [3000]})[0]["origin"] == "free"  # a literal, not an entity
    assert manual_slots({"inverter_type": ["GE"], "charge_rate": ["number.givtcp_charge_rate"]})[0]["origin"] == "manual"  # the same setting bound to an entity
    print("PASS: a control method needs an entity on a modify setting")
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
    """No user tuning setting is owned by the bindings, so the coordinator never clears one.

    Whether it writes one a record binds (tests/discovery_contract.py REPORTED_TUNING) is decided when the component binding it is converted.
    """
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
    """A gateway record claiming the batteries in manual slots 0 and 1 takes slot 0 and clears slot 1, and stays that way once replayed."""
    apps = _manual_apps("number.givtcp_ce1111g111_charge_rate", "number.givtcp_ce2222g222_charge_rate")
    warnings = [
        "Inverter slot 0: gecloud inverter ce1111g111 is also configured manually in apps.yaml - discovery is taking over this slot",
        "Inverter slot 1: also names ce2222g222, already configured in slot 0 - clearing this slot",
    ]
    # The gateway's own entities name only the gateway, so once replayed the live args at slots 0 and 1 hold no battery serial
    for gateway in ("gw1111g111", "gw001"):
        record = _rec(gateway, {"charge_rate": _rw("number.gecloud_{}_rate".format(gateway))}, serials=["ce1111g111", "ce2222g222"])
        results, args = _passes(apps, [_src([record])])
        b, w, written = results[0]
        assert _layout(b) == [("discovered", "gecloud:" + gateway, 0)], (gateway, _layout(b))
        assert b["inverters"][0]["serials"] == sorted(["ce1111g111", "ce2222g222", gateway]), b["inverters"][0]["serials"]
        assert w == warnings, (gateway, w)
        assert written and args["num_inverters"] == 1 and args["inverter_type"] == ["GEC"], (gateway, args)
        # Stable on the next passes: slot 0 still matches through the first pass's args, slot 1 is cleared again, nothing is written
        for again, w, written in results[1:]:
            assert again == b and w == warnings and written == [], (gateway, _layout(again), w, written)
        assert args["num_inverters"] == 1 and args["inverter_type"] == ["GEC"] and args["charge_rate"] == ["number.gecloud_{}_rate".format(gateway)], (gateway, args)
    print("PASS: gateway collapses two manual slots")
    return 0


def test_build_takeover_stable_after_replay():
    """A taken-over slot whose discovered entities name no serial stays taken over once replayed: the same bindings, the same warning and no writes."""
    apps = _manual_apps("number.givtcp_CE1111G111_charge_rate")
    apps["soc_percent"] = ["sensor.givtcp_ce1111g111_soc"]
    record = _rec("CE1111G111", {"charge_rate": _rw("number.gecloud_main_rate"), "soc_percent": _ro("sensor.gecloud_main_soc")})
    results, args = _passes(apps, [_src([record])])
    b, w, written = results[0]
    warning = ["Inverter slot 0: gecloud inverter ce1111g111 is also configured manually in apps.yaml - discovery is taking over this slot"]
    assert _layout(b) == [("discovered", "gecloud:CE1111G111", 0)] and w == warning and written, (_layout(b), w, written)
    for again, w, written in results[1:]:
        assert again == b and w == warning and written == [], (_layout(again), w, written)
    assert args["num_inverters"] == 1 and args["inverter_type"] == ["GEC"] and args["charge_rate"] == ["number.gecloud_main_rate"], args
    print("PASS: takeover stable after replay")
    return 0


def test_build_scans_the_given_baseline():
    """The serial scan reads scan_args when given and args otherwise; ge_cloud_serial still comes from the snapshot."""
    apps = _manual_apps("number.givtcp_ce1111g111_charge_rate")
    rewritten = dict(copy.deepcopy(apps), charge_rate=["number.gecloud_rate"])
    sources = [_src([_rec("ce1111g111")])]
    assert _layout(build_bindings(apps, rewritten, sources, None)[0])[0] == ("manual", None, 0)
    assert _layout(build_bindings(apps, rewritten, sources, None, scan_args=copy.deepcopy(apps))[0]) == [("discovered", "gecloud:ce1111g111", 0)]
    assert _layout(build_bindings(dict(apps, ge_cloud_serial="CE1111G111"), rewritten, sources, None, scan_args={})[0]) == [("discovered", "gecloud:ce1111g111", 0)]
    print("PASS: build scans the given baseline")
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


def test_build_sticky_never_overrides_unrelated_manual():
    """A previous slot now held by a manual inverter sharing no serial is left alone: the record moves, with no takeover warning."""
    first, _ = build_bindings({}, {}, [_src([_rec("a11111", {"charge_rate": _rw("number.a_rate")})])], None)
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    manual = {"slot": 0, "origin": "manual", "source": None, "device_id": None, "inverter_type": "GE", "serials": [], "settings": {"charge_rate": {"entity_id": "number.givtcp_sa9999s999_charge_rate"}}}
    # (a) the source reports the record again
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("a11111", {"charge_rate": _rw("number.a_rate")})])], first)
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:a11111", 1)], _layout(b)
    assert b["inverters"][0] == manual, b["inverters"][0]
    assert w == [], w
    # (b) the source is absent: the retained entry moves to the lowest free slot, otherwise unchanged
    b, w = build_bindings(apps, copy.deepcopy(apps), [], first)
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:a11111", 1)], _layout(b)
    assert b["inverters"][0] == manual and b["inverters"][1] == dict(first["inverters"][0], slot=1), b["inverters"]
    assert w == [], w
    # A retained entry with no slot left is dropped with the no-room warning
    apps = _manual_apps("number.a_charge_rate", "number.b_charge_rate", "number.c_charge_rate", "number.d_charge_rate")
    b, w = build_bindings(apps, copy.deepcopy(apps), [], first)
    assert _layout(b) == [("manual", None, 0), ("manual", None, 1), ("manual", None, 2), ("manual", None, 3)], _layout(b)
    assert w == ["gecloud inverter gecloud:a11111 cannot be configured - all {} inverter slots are in use".format(MAX_INVERTERS)], w
    print("PASS: sticky slot never overrides an unrelated manual inverter")
    return 0


def test_build_displaced_sticky_waits_for_kept_sticky():
    """Every previous placement that can keep its slot is placed before a displaced one looks for a free slot."""
    first, _ = build_bindings({}, {}, [_src([_rec("a11111"), _rec("b11111")])], None)
    assert slot_map(first) == {"gecloud:a11111": 0, "gecloud:b11111": 1}, slot_map(first)
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    expected = [("manual", None, 0), ("discovered", "gecloud:b11111", 1), ("discovered", "gecloud:a11111", 2)]
    # Fresh path: the source reports both again
    b, w = build_bindings(apps, copy.deepcopy(apps), [_src([_rec("a11111"), _rec("b11111")])], first)
    assert _layout(b) == expected and w == [], (_layout(b), w)
    # Retained path: the source is absent
    b, w = build_bindings(apps, copy.deepcopy(apps), [], first)
    assert _layout(b) == expected and w == [], (_layout(b), w)
    assert b["inverters"][1] == first["inverters"][1] and b["inverters"][2] == dict(first["inverters"][0], slot=2), b["inverters"]
    print("PASS: displaced sticky entry waits for the kept ones")
    return 0


def test_build_duplicate_device_id():
    """Within one pass the first record with a device_id is placed and a later one with the same device_id is ignored."""
    b, w = build_bindings({}, {}, [_src([_rec("a11111", {"charge_rate": _rw("number.first")}), _rec("b11111"), _rec("a11111", {"charge_rate": _rw("number.second")})])], None)
    assert _layout(b) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(b)
    assert b["inverters"][0]["settings"] == {"charge_rate": _rw("number.first")}, b["inverters"][0]
    assert w == [], w
    print("PASS: duplicate device_id ignored")
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


class _ReadTracker(dict):
    """A dict that records every key read through [], get or in, to prove replay never looks at a key."""

    def __init__(self, *args, **kwargs):
        """Build the dict with nothing read yet."""
        super().__init__(*args, **kwargs)
        self.read = set()

    def __getitem__(self, key):
        """Record the key, then read it."""
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        """Record the key, then read it."""
        self.read.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        """Record the key, then test for it."""
        self.read.add(key)
        return super().__contains__(key)


def _apply(bindings, apps_yaml, args):
    """Replay bindings into args through a set_arg with PredBat.set_arg's semantics; return the (setting, index) pairs replay reported, checked against the calls made."""
    calls = []

    def set_arg(arg, value, index=None):
        """Record the call, refuse a per-entry None (PredBat.set_arg would delete the whole key), then apply it as PredBat.set_arg does."""
        assert not (value is None and index is not None), "set_arg({}, None, index={}) deletes the whole key".format(arg, index)
        calls.append((arg, index))
        if value is None:
            if arg in args:
                del args[arg]
        elif index is not None:
            if arg not in args:
                args[arg] = []
            while len(args[arg]) <= index:
                args[arg].append(None)
            args[arg][index] = value
        else:
            args[arg] = value

    written = replay_bindings(bindings, apps_yaml, args, set_arg)
    assert written == calls, (written, calls)
    return written


def _bind(apps, records, args=None, previous=None, **options):
    """Bindings for the snapshot apps and one gecloud source of records, with the live args (a copy of apps unless given)."""
    args = copy.deepcopy(apps) if args is None else args
    bindings, _ = build_bindings(apps, args, [_src(records, **options)], previous)
    return bindings, args


def _ro(entity_id):
    """A read-only entity descriptor."""
    return {"entity_id": entity_id, "access": "r"}


def _passes(apps, sources, count=3, args=None):
    """
    count coordinator-style passes: build, then replay into args, each pass building on the last pass's bindings.

    The serial scan reads a copy of args taken before the first pass, as the coordinator's baseline does. Returns
    ([(bindings, warnings, written)] one per pass, args as the last pass left them).
    """
    args = copy.deepcopy(apps) if args is None else args
    baseline = copy.deepcopy(args)
    previous = None
    results = []
    for _ in range(count):
        bindings, warnings = build_bindings(apps, args, sources, previous, scan_args=baseline)
        written = _apply(bindings, apps, args)
        results.append((copy.deepcopy(bindings), warnings, written))
        previous = bindings
    return results, args


def _phase_passes(apps, phases, args=None, extra=2):
    """
    One coordinator-style pass per entry of phases (a list of sources), then `extra` more with the last sources.

    Each pass builds on the last pass's bindings and replays into args; the serial scan reads a copy of args
    taken before the first pass, as the coordinator's baseline does. Returns ([(bindings, warnings, written)], args).
    """
    args = copy.deepcopy(apps) if args is None else args
    baseline = copy.deepcopy(args)
    previous = None
    results = []
    for sources in list(phases) + [phases[-1]] * extra:
        bindings, warnings = build_bindings(apps, args, sources, previous, scan_args=baseline)
        written = _apply(bindings, apps, args)
        results.append((copy.deepcopy(bindings), warnings, written))
        previous = bindings
    return results, args


def _moved(old, new, serial):
    """The warning a compacting move gives."""
    return "Inverter slot {} moved to slot {}: gecloud inverter gecloud:{} closes the gap left by a device that is no longer reported".format(old, new, serial)


def _full(serial):
    """A record binding a control, a per-slot sensor and a compact summed total for serial."""
    return _rec(serial, {"charge_rate": _rw("number.{}_rate".format(serial)), "soc_percent": _ro("sensor.{}_soc".format(serial)), "load_today": _ro("sensor.{}_load_today".format(serial))})


def _assert_settled(results, start, warnings_each=()):
    """Every pass from index start on wrote nothing and warned about nothing but warnings_each (a takeover warns on every pass)."""
    for bindings, warnings, written in results[start:]:
        assert written == [] and warnings == list(warnings_each), (warnings, written)


def test_compact_two_first_released():
    """Two discovered, slot 0 released: the device in slot 1 moves to slot 0, its whole configuration is rewritten and every list shortened."""
    results, args = _phase_passes({}, [[_src([_full("a11111"), _full("b11111")])], [_src([_full("b11111")])]])
    assert _layout(results[0][0]) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(results[0][0])
    bindings, warnings, written = results[1]
    assert _layout(bindings) == [("discovered", "gecloud:b11111", 0)], _layout(bindings)
    assert warnings == [_moved(1, 0, "b11111")], warnings
    assert all(index is None for _, index in written), written  # every list is shortened, so every write is a whole value
    assert args["num_inverters"] == 1 and args["inverter_type"] == ["GEC"], args
    assert args["charge_rate"] == ["number.b11111_rate"] and args["soc_percent"] == ["sensor.b11111_soc"], args
    assert args["load_today"] == ["sensor.b11111_load_today"] and args["pv_power"] == [0], args
    moved = results[1][0]["inverters"][0]
    assert moved == dict(results[0][0]["inverters"][1], slot=0), moved  # everything but slot is the entry it was
    _assert_settled(results, 2)
    print("PASS: compact two, first released")
    return 0


def test_compact_three_middle_released():
    """Three discovered, the middle one released: the top one moves down to slot 1."""
    results, args = _phase_passes({}, [[_src([_full("a11111"), _full("b11111"), _full("c11111")])], [_src([_full("a11111"), _full("c11111")])]])
    bindings, warnings, _ = results[1]
    assert _layout(bindings) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:c11111", 1)], _layout(bindings)
    assert warnings == [_moved(2, 1, "c11111")], warnings
    assert args["num_inverters"] == 2 and args["charge_rate"] == ["number.a11111_rate", "number.c11111_rate"], args
    assert args["load_today"] == ["sensor.a11111_load_today", "sensor.c11111_load_today"], args
    _assert_settled(results, 2)
    print("PASS: compact three, middle released")
    return 0


def test_compact_around_manual():
    """[discovered A, manual M, discovered B], A released: B moves to slot 0 and M stays at slot 1."""
    apps = _manual_apps("number.dead_rate", "number.m_rate", types=["", "GE"])
    results, args = _phase_passes(apps, [[_src([_full("a11111"), _full("b11111")])], [_src([_full("b11111")])]])
    assert _layout(results[0][0]) == [("discovered", "gecloud:a11111", 0), ("manual", None, 1), ("discovered", "gecloud:b11111", 2)], _layout(results[0][0])
    bindings, warnings, _ = results[1]
    assert _layout(bindings) == [("discovered", "gecloud:b11111", 0), ("manual", None, 1)], _layout(bindings)
    assert warnings == [_moved(2, 0, "b11111")], warnings
    assert args["num_inverters"] == 2 and args["inverter_type"] == ["GEC", "GE"] and args["charge_rate"] == ["number.b11111_rate", "number.m_rate"], args
    _assert_settled(results, 2)
    print("PASS: compact around a manual slot")
    return 0


def test_compact_takeover_pinned():
    """A takeover slot below a released device is not moved; a released device below a takeover slot leaves a hole, with nothing to move."""
    # Takeover at 0, released A at 1, B above it: B moves to 1, the takeover stays at 0
    apps = _manual_apps("number.givtcp_t11111_rate")
    results, args = _phase_passes(apps, [[_src([_full("t11111"), _full("a11111"), _full("b11111")])], [_src([_full("t11111"), _full("b11111")])]])
    assert _layout(results[0][0]) == [("discovered", "gecloud:t11111", 0), ("discovered", "gecloud:a11111", 1), ("discovered", "gecloud:b11111", 2)], _layout(results[0][0])
    takeover = "Inverter slot 0: gecloud inverter t11111 is also configured manually in apps.yaml - discovery is taking over this slot"
    bindings, warnings, _ = results[1]
    assert _layout(bindings) == [("discovered", "gecloud:t11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(bindings)
    assert warnings == [takeover, _moved(2, 1, "b11111")], warnings
    _assert_settled(results, 2, [takeover])
    # Released A at 0, below a takeover of the manual slot 1: the takeover is pinned, so nothing moves and the hole is warned about
    apps = _manual_apps("number.dead_rate", "number.givtcp_t11111_rate", types=["", "GE"])
    results, args = _phase_passes(apps, [[_src([_full("a11111"), _full("t11111")])], [_src([_full("t11111")])]])
    assert _layout(results[0][0]) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:t11111", 1)], _layout(results[0][0])
    hole = "Inverter slot 0 is empty below a configured inverter - it has no inverter_type and Predbat will stop on it"
    takeover = "Inverter slot 1: gecloud inverter t11111 is also configured manually in apps.yaml - discovery is taking over this slot"
    for bindings, warnings, _ in results[1:]:
        assert _layout(bindings) == [("free", None, 0), ("discovered", "gecloud:t11111", 1)], _layout(bindings)
        assert warnings == [takeover, hole], warnings
    assert results[2][2] == [] and results[3][2] == [], results
    print("PASS: takeover slots are pinned")
    return 0


def test_compact_not_on_empty_or_absent_report():
    """A report with no driven inverter, or a source absent, keeps every slot it had: nothing is released, so nothing moves."""
    first, _ = build_bindings({}, {}, [_src([_full("a11111"), _full("b11111")])], None)
    for sources in ([_src([])], []):
        b, w = build_bindings({}, {}, sources, first)
        assert _layout(b) == [("discovered", "gecloud:a11111", 0), ("discovered", "gecloud:b11111", 1)], _layout(b)
        assert not [warning for warning in w if "moved to slot" in warning], w
    print("PASS: no compaction on an empty or absent report")
    return 0


def test_compact_residual_hole():
    """The user's dead apps.yaml slot below manual slots, taken by a device that is then released, stays free with the hole warning and no move."""
    apps = _manual_apps("number.dead_rate", "number.m_rate", "number.givtcp_t11111_rate", types=["", "GE", "GE"])
    results, args = _phase_passes(apps, [[_src([_full("a11111"), _full("t11111")])], [_src([_full("t11111")])]])
    assert _layout(results[0][0]) == [("discovered", "gecloud:a11111", 0), ("manual", None, 1), ("discovered", "gecloud:t11111", 2)], _layout(results[0][0])
    hole = "Inverter slot 0 is empty below a configured inverter - it has no inverter_type and Predbat will stop on it"
    takeover = "Inverter slot 2: gecloud inverter t11111 is also configured manually in apps.yaml - discovery is taking over this slot"
    for bindings, warnings, _ in results[1:]:
        assert _layout(bindings) == [("free", None, 0), ("manual", None, 1), ("discovered", "gecloud:t11111", 2)], _layout(bindings)
        assert warnings == [takeover, hole], warnings
    assert args["inverter_type"] == [None, "GE", "GEC"] and args["num_inverters"] == 3, args
    assert results[2][2] == [] and results[3][2] == [], results
    print("PASS: residual hole stays, warned")
    return 0


def test_replay_discovered_only():
    """Manual slot 0's entries are never written; discovered slot 1 gets inverter_type and each bound setting at index 1."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["soc_percent"] = ["sensor.givtcp_sa9999s999_soc"]
    b, args = _bind(apps, [_rec("a11111", {"charge_rate": _rw("number.a_rate"), "soc_percent": _ro("sensor.a_soc")})])
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:a11111", 1)], _layout(b)
    written = _apply(b, apps, args)
    assert written == [("charge_rate", 1), ("inverter_type", 1), ("num_inverters", None), ("pv_power", 1), ("soc_percent", 1)], written
    assert args == {
        "num_inverters": 2,
        "inverter_type": ["GE", "GEC"],
        "charge_rate": ["number.givtcp_sa9999s999_charge_rate", "number.a_rate"],
        "soc_percent": ["sensor.givtcp_sa9999s999_soc", "sensor.a_soc"],
        "pv_power": [None, 0],
    }, args
    print("PASS: replay writes the discovered slot only")
    return 0


def test_replay_clears_unbound():
    """A binding setting the record does not bind, set in args at the discovered slot, is cleared; givtcp_rest too."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    args = copy.deepcopy(apps)
    args["battery_voltage"] = [None, "sensor.stale_voltage"]
    args["givtcp_rest"] = [None, "http://old:6345"]
    b, args = _bind(apps, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})], args=args)
    written = _apply(b, apps, args)
    # Every entry left is None, so each key goes in one whole-value delete
    assert written == [("battery_voltage", None), ("charge_rate", 1), ("givtcp_rest", None), ("inverter_type", 1), ("num_inverters", None), ("pv_power", 1)], written
    assert "battery_voltage" not in args and "givtcp_rest" not in args, args
    assert args["charge_rate"] == ["number.givtcp_sa9999s999_charge_rate", "number.a_rate"], args
    print("PASS: replay clears what the record does not bind")
    return 0


def test_replay_clear_entry_whole_value():
    """Clearing one entry of a list that keeps a value elsewhere is one whole-value write of the full list, never set_arg(key, None, index)."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["battery_power"] = ["sensor.m_battery_power"]
    apps["givtcp_rest"] = ["http://m:6345"]
    args = copy.deepcopy(apps)
    args["battery_power"] = ["sensor.m_battery_power", "sensor.stale_battery_power"]
    args["givtcp_rest"] = ["http://m:6345", "http://old:6345"]
    b, args = _bind(apps, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})], args=args)
    written = _apply(b, apps, args)
    assert written == [("battery_power", None), ("charge_rate", 1), ("givtcp_rest", None), ("inverter_type", 1), ("num_inverters", None), ("pv_power", 1)], written
    assert args["battery_power"] == ["sensor.m_battery_power", None] and args["givtcp_rest"] == ["http://m:6345", None], args
    print("PASS: one entry cleared by a whole-value write")
    return 0


def test_replay_shortens_long_list():
    """A live list longer than the slot count is shortened by one whole-value write."""
    args = {"charge_rate": ["number.old0", "number.old1", "number.old2"], "inverter_type": ["X", "Y"], "num_inverters": 2}
    b, args = _bind({}, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})], args=args)
    written = _apply(b, {}, args)
    assert written == [("charge_rate", None), ("inverter_type", None), ("num_inverters", None), ("pv_power", 0)], written
    assert args == {"charge_rate": ["number.a_rate"], "inverter_type": ["GEC"], "num_inverters": 1, "pv_power": [0]}, args
    print("PASS: a long live list is shortened")
    return 0


def test_replay_tuning_untouched():
    """inverter_battery_rate_min at the discovered slot is neither read nor written."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["inverter_battery_rate_min"] = [100]
    args = _ReadTracker(copy.deepcopy(apps))
    args["inverter_battery_rate_min"] = [100, 200]
    b, args = _bind(apps, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})], args=args)
    args.read = set()
    written = _apply(b, apps, args)
    assert "inverter_battery_rate_min" not in args.read, sorted(args.read)
    assert "inverter_battery_rate_min" not in [setting for setting, _ in written] and args["inverter_battery_rate_min"] == [100, 200], (written, args)
    print("PASS: tuning untouched")
    return 0


def test_replay_skip_setting():
    """A skipped setting is left alone when live args match apps.yaml, slot-indexed or compact."""
    apps = {"load_today": ["sensor.user_load_today", "sensor.user_extra_load_today"], "soc_percent": ["sensor.user_soc"]}
    record = _rec("a11111", {"charge_rate": _rw("number.a_rate"), "load_today": _ro("sensor.a_load_today"), "soc_percent": _ro("sensor.a_soc")})
    b, args = _bind(apps, [record], skip_settings={"load_today", "soc_percent"})
    assert _layout(b) == [("discovered", "gecloud:a11111", 0)], _layout(b)
    written = _apply(b, apps, args)
    assert written == [("charge_rate", 0), ("inverter_type", 0), ("num_inverters", None), ("pv_power", 0)], written
    assert args["load_today"] == apps["load_today"] and args["soc_percent"] == apps["soc_percent"], args
    print("PASS: skipped setting left as apps.yaml had it")
    return 0


def test_replay_keep_user_setting():
    """A kept setting with an apps.yaml value at the slot is not written; without one the binding is."""
    record = _rec("a11111", {"inverter_mode": _rw("select.a_mode")})
    apps = {"inverter_mode": ["select.user_mode"]}
    b, args = _bind(apps, [record], keep_user_settings={"inverter_mode"})
    written = _apply(b, apps, args)
    assert "inverter_mode" not in [setting for setting, _ in written] and args["inverter_mode"] == ["select.user_mode"], (written, args)
    b, args = _bind({}, [record], keep_user_settings={"inverter_mode"})
    written = _apply(b, {}, args)
    assert ("inverter_mode", 0) in written and args["inverter_mode"] == ["select.a_mode"], (written, args)
    print("PASS: kept user setting")
    return 0


def test_replay_keep_single_value_whole_key():
    """
    A kept setting apps.yaml gives as one value (3000, 0) is the user's for the whole key: on two discovered slots nothing is
    written to it on any pass and live args keep it as they are, as set_arg_auto(overwrite=False) kept the whole value. None in
    apps.yaml does not count, and a list keeps the per-slot rule.
    """
    records = [_rec(serial, {"export_limit": _ro("sensor.{}_export_limit".format(serial)), "charge_rate": _rw("number.{}_charge".format(serial))}) for serial in ("a11111", "b22222")]
    for value in (3000, 0):
        apps = {"export_limit": value}
        results, args = _passes(apps, [_src(records, keep_user_settings={"export_limit"})])
        for _, warnings, written in results:
            assert "export_limit" not in [setting for setting, _ in written], (value, written)
        assert args["export_limit"] == value and type(args["export_limit"]) is type(value), (value, args)
        assert results[1][2] == [] and results[2][2] == [], (value, results)  # idempotent
        assert args["charge_rate"] == ["number.a11111_charge", "number.b22222_charge"], args
        assert "export_limit" not in desired_args(results[-1][0], apps, args), value
    # None in apps.yaml is no value of the user's: the slots are bound from the records
    apps = {"export_limit": None}
    _, args = _passes(apps, [_src(records, keep_user_settings={"export_limit"})], args={})
    assert args["export_limit"] == ["sensor.a11111_export_limit", "sensor.b22222_export_limit"], args
    # A list is per slot: kept where it has an entry, bound where it has none
    apps = {"export_limit": [2000]}
    results, args = _passes(apps, [_src(records, keep_user_settings={"export_limit"})])
    assert args["export_limit"] == [2000, "sensor.b22222_export_limit"], args
    assert results[1][2] == [] and results[2][2] == [], results
    # Not kept at all, a single value is replaced as any other binding
    apps = {"export_limit": 3000}
    _, args = _passes(apps, [_src(records)])
    assert args["export_limit"] == ["sensor.a11111_export_limit", "sensor.b22222_export_limit"], args
    print("PASS: a single kept value owns the whole key")
    return 0


def test_replay_skip_keep_keep_live():
    """A skipped or kept non-summed setting keeps whatever live args hold, even when that differs from apps.yaml; a summed one still takes the apps.yaml entry."""
    apps = {"soc_percent": ["re:sensor.givtcp_(.+)_soc"], "inverter_mode": ["select.user_mode"], "load_today": ["sensor.user_load_today"]}
    args = {"soc_percent": ["sensor.givtcp_x_soc"], "inverter_mode": ["select.written_elsewhere"], "load_today": ["sensor.rewritten_load_today"]}
    record = _rec("a11111", {"soc_percent": _ro("sensor.a_soc"), "inverter_mode": _rw("select.a_mode"), "load_today": _ro("sensor.a_load_today"), "reserve": _rw("number.a_reserve")})
    b, args = _bind(apps, [record], args=args, skip_settings={"soc_percent", "load_today"}, keep_user_settings={"inverter_mode", "reserve"})
    written = _apply(b, apps, args)
    assert written == [("inverter_type", 0), ("load_today", 0), ("num_inverters", None), ("pv_power", 0), ("reserve", 0)], written
    assert args["soc_percent"] == ["sensor.givtcp_x_soc"] and args["inverter_mode"] == ["select.written_elsewhere"], args  # left exactly as live had them
    assert args["reserve"] == ["number.a_reserve"], args  # kept, but apps.yaml has no value: written from the record
    assert args["load_today"] == ["sensor.user_load_today"], args  # summed: the apps.yaml entry
    assert desired_args(b, apps, args)["soc_percent"] == ["sensor.givtcp_x_soc"]
    print("PASS: skipped and kept settings keep live args")
    return 0


def test_replay_scalar_becomes_list():
    """A scalar per-inverter arg becomes a list with the scalar at index 0 (Review Focus 1)."""
    apps = {"num_inverters": 1, "inverter_type": "GE", "charge_rate": "number.a"}
    b, args = _bind(apps, [_rec("b11111", {"charge_rate": _rw("number.b")})])
    assert _layout(b) == [("manual", None, 0), ("discovered", "gecloud:b11111", 1)], _layout(b)
    written = _apply(b, apps, args)
    assert written == [("charge_rate", None), ("inverter_type", None), ("num_inverters", None), ("pv_power", 1)], written
    assert args["charge_rate"] == ["number.a", "number.b"] and args["inverter_type"] == ["GE", "GEC"], args
    print("PASS: scalar becomes a list")
    return 0


def test_replay_value_binding():
    """A value binding writes its literal."""
    b, args = _bind({}, [_rec("a11111", {"grid_power": {"value": 0, "access": "r"}})])
    written = _apply(b, {}, args)
    assert ("grid_power", 0) in written and args["grid_power"] == [0], (written, args)
    print("PASS: value binding")
    return 0


def test_replay_compact_settings():
    """A shared CT bound by one slot only gives a one-entry list with no None, whichever slot binds it."""
    b, args = _bind({}, [_rec("a11111", {"import_today": _ro("sensor.a_import_today")}), _rec("b11111")])
    written = _apply(b, {}, args)
    assert ("import_today", 0) in written and args["import_today"] == ["sensor.a_import_today"], (written, args)
    b, args = _bind({}, [_rec("a11111"), _rec("b11111", {"import_today": _ro("sensor.b_import_today")})])
    _apply(b, {}, args)
    assert args["import_today"] == ["sensor.b_import_today"], args
    print("PASS: compact settings")
    return 0


def test_replay_pv_power_indexed():
    """pv_power is indexed by slot, a discovered slot binding nothing gets 0, and the extras follow the last slot."""
    pv = _rec("pv1111", {"pv_power": _ro("sensor.pv1111_pv_power")}, inverter_type=None)
    b, args = _bind({}, [_rec("a11111"), _rec("b11111", {"pv_power": _ro("sensor.b_pv_power")}), pv], include_pv_only=True)
    written = _apply(b, {}, args)
    assert ("pv_power", 0) in written and args["pv_power"] == [0, "sensor.b_pv_power", "sensor.pv1111_pv_power"], (written, args)
    print("PASS: pv_power indexed by slot")
    return 0


def test_replay_manual_summed_entries():
    """A manual slot's summed entries are its own apps.yaml ones, never a fabricated 0; the discovered slot after it and the extras follow."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["load_today"] = ["sensor.m_load_today", "sensor.extra_load_today"]
    apps["pv_power"] = ["sensor.m_pv_power", "sensor.extra_pv_power"]
    record = _rec("a11111", {"load_today": _ro("sensor.a_load_today")})
    b, args = _bind(apps, [record])
    written = _apply(b, apps, args)
    assert [item for item in written if item[0] in ("load_today", "pv_power")] == [("load_today", 1), ("load_today", 2), ("pv_power", 1), ("pv_power", 2)], written
    assert args["load_today"] == ["sensor.m_load_today", "sensor.a_load_today", "sensor.extra_load_today"], args
    assert args["pv_power"] == ["sensor.m_pv_power", 0, "sensor.extra_pv_power"], args
    # A second pass: the live lists have been rewritten, the manual entries still come from apps.yaml
    again, _ = build_bindings(apps, args, [_src([record])], b)
    assert _apply(again, apps, args) == [], args
    # A manual slot with no pv_power of its own keeps None at its position; one with no load_today contributes nothing
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["pv_power"] = [None, "sensor.extra_pv_power"]
    b, args = _bind(apps, [record])
    _apply(b, apps, args)
    assert args["pv_power"] == [None, 0, "sensor.extra_pv_power"] and args["load_today"] == ["sensor.a_load_today"], args
    # A manual slot 1 behind a discovered slot 0: its compact entry moves to index 0, and the next pass still finds it
    apps = _manual_apps("number.dead_charge_rate", "number.givtcp_sa9999s999_charge_rate", types=["", "GE"])
    apps["load_today"] = ["sensor.dead_load_today", "sensor.m_load_today"]
    apps["pv_power"] = ["sensor.dead_pv_power", "sensor.m_pv_power"]
    b, args = _bind(apps, [_rec("a11111")])
    assert _layout(b) == [("discovered", "gecloud:a11111", 0), ("manual", None, 1)], _layout(b)
    written = _apply(b, apps, args)
    assert ("load_today", None) in written and ("pv_power", 0) in written, written
    assert args["load_today"] == ["sensor.m_load_today"] and args["pv_power"] == [0, "sensor.m_pv_power"], args
    again, _ = build_bindings(apps, args, [_src([_rec("a11111")])], b)
    assert _apply(again, apps, args) == [] and args["load_today"] == ["sensor.m_load_today"], args
    print("PASS: manual slot summed entries")
    return 0


def test_replay_hole_cleared():
    """A slot a gateway record cleared, below a manual one, has every binding setting and its inverter_type cleared."""
    apps = _manual_apps("number.givtcp_ce1111g111_charge_rate", "number.givtcp_ce2222g222_charge_rate", "number.givtcp_sa9999s999_charge_rate")
    b, args = _bind(apps, [_rec("gw1111g111", {"charge_rate": _rw("number.gw_rate")}, serials=["ce1111g111", "ce2222g222"])])
    assert _layout(b) == [("discovered", "gecloud:gw1111g111", 0), ("free", None, 1), ("manual", None, 2)], _layout(b)
    written = _apply(b, apps, args)
    assert written == [("charge_rate", None), ("inverter_type", None), ("pv_power", 0)], written
    assert args["charge_rate"] == ["number.gw_rate", None, "number.givtcp_sa9999s999_charge_rate"] and args["inverter_type"] == ["GEC", None, "GE"], args
    assert args["pv_power"] == [0] and args["num_inverters"] == 3, args
    print("PASS: hole cleared")
    return 0


def test_replay_site_and_count():
    """battery_temperature_history is written as a scalar and num_inverters is the slot count; a change to the site setting alone writes only it."""
    rec_a = _rec("a11111", {"battery_temperature_history": _ro("sensor.a_temp_history")})
    b, args = _bind({}, [rec_a, _rec("b11111")])
    written = _apply(b, {}, args)
    assert ("battery_temperature_history", None) in written and ("num_inverters", None) in written, written
    assert args["battery_temperature_history"] == "sensor.a_temp_history" and args["num_inverters"] == 2, args
    rec_a = _rec("a11111", {"battery_temperature_history": _ro("sensor.a_temp_history_new")})
    again, _ = build_bindings({}, args, [_src([rec_a, _rec("b11111")])], b)
    assert _apply(again, {}, args) == [("battery_temperature_history", None)] and args["battery_temperature_history"] == "sensor.a_temp_history_new", args
    print("PASS: site setting and count")
    return 0


def test_replay_all_none_key_deleted():
    """A key whose every entry would be None is deleted from args."""
    b, args = _bind({}, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})], args={"battery_voltage": ["sensor.stale_voltage"]})
    written = _apply(b, {}, args)
    assert ("battery_voltage", None) in written and "battery_voltage" not in args, (written, args)
    print("PASS: all-None key deleted")
    return 0


def test_replay_idempotent():
    """A second replay of the same inputs writes nothing."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["load_today"] = ["sensor.m_load_today", "sensor.extra_load_today"]
    apps["pv_power"] = ["sensor.m_pv_power"]
    entities = {"charge_rate": _rw("number.a_rate"), "load_today": _ro("sensor.a_load_today"), "grid_power": {"value": 0, "access": "r"}, "battery_temperature_history": _ro("sensor.a_temp")}
    sources = [_src([_rec("a11111", entities), _rec("pv1111", {"pv_power": _ro("sensor.pv1111_pv_power")}, inverter_type=None)], include_pv_only=True)]
    args = copy.deepcopy(apps)
    args["charge_rate"] = "number.givtcp_sa9999s999_charge_rate"
    b, _ = build_bindings(apps, args, sources, None)
    assert _apply(b, apps, args) != []
    snapshot = copy.deepcopy(args)
    assert _apply(b, apps, args) == [] and args == snapshot, args
    again, _ = build_bindings(apps, args, sources, b)
    assert _apply(again, apps, args) == [] and args == snapshot, args
    print("PASS: replay idempotent")
    return 0


def test_replay_single_entry():
    """Changing one bound entity writes exactly that entry."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    b, args = _bind(apps, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})])
    _apply(b, apps, args)
    again, _ = build_bindings(apps, args, [_src([_rec("a11111", {"charge_rate": _rw("number.a_rate_new")})])], b)
    assert _apply(again, apps, args) == [("charge_rate", 1)], args
    assert args["charge_rate"] == ["number.givtcp_sa9999s999_charge_rate", "number.a_rate_new"], args
    print("PASS: single entry written")
    return 0


def test_replay_leaves_manual_change_alone():
    """A manual slot's non-summed entry changed from outside between passes is left as it now is, not repaired."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    apps["soc_percent"] = ["sensor.givtcp_sa9999s999_soc"]
    record = _rec("a11111", {"charge_rate": _rw("number.a_rate"), "soc_percent": _ro("sensor.a_soc")})
    b, args = _bind(apps, [record])
    baseline = copy.deepcopy(args)
    _apply(b, apps, args)
    args["charge_rate"][0] = "number.changed_elsewhere"
    args["soc_percent"][0] = "sensor.changed_elsewhere"
    again, _ = build_bindings(apps, args, [_src([record])], b, scan_args=baseline)
    assert _layout(again) == [("manual", None, 0), ("discovered", "gecloud:a11111", 1)], _layout(again)
    assert _apply(again, apps, args) == [], args
    assert args["charge_rate"] == ["number.changed_elsewhere", "number.a_rate"] and args["soc_percent"] == ["sensor.changed_elsewhere", "sensor.a_soc"], args
    print("PASS: a manual slot's changed entry is left alone")
    return 0


def test_replay_repairs_overwrite():
    """An entry something else overwrote, or a key it deleted, is written back by the next replay."""
    b, args = _bind({}, [_rec("a11111", {"soc_percent": _ro("sensor.a_soc")})])
    _apply(b, {}, args)
    args["soc_percent"] = ["sensor.other_soc"]
    assert _apply(b, {}, args) == [("soc_percent", 0)] and args["soc_percent"] == ["sensor.a_soc"], args
    del args["soc_percent"]
    assert _apply(b, {}, args) == [("soc_percent", 0)] and args["soc_percent"] == ["sensor.a_soc"], args
    print("PASS: overwrite repaired")
    return 0


def test_replay_never_zero_inverters():
    """Bindings with no non-free slot write nothing at all."""
    b, _ = build_bindings({}, {}, [], None)
    args = {"num_inverters": 1, "charge_rate": ["number.x"]}
    assert _apply(b, {}, args) == [] and args == {"num_inverters": 1, "charge_rate": ["number.x"]}, args
    print("PASS: never zero inverters")
    return 0


def test_replay_nothing_discovered():
    """Bindings with no discovered slot - manual slots only, or none - decide nothing and write nothing."""
    apps = _manual_apps("number.a_charge_rate", "number.b_charge_rate")
    args = copy.deepcopy(apps)
    args["givtcp_rest"] = ["http://stale:6345"]
    b, _ = build_bindings(apps, args, [], None)
    assert _layout(b) == [("manual", None, 0), ("manual", None, 1)], _layout(b)
    assert desired_args(b, apps, args) == {} and desired_args(None, apps, args) == {}
    snapshot = copy.deepcopy(args)
    assert _apply(b, apps, args) == [] and args == snapshot, args
    print("PASS: nothing discovered, nothing written")
    return 0


def test_desired_args_shape():
    """desired_args gives the full value of every arg the bindings decide, None for a key that should be absent."""
    apps = _manual_apps("number.givtcp_sa9999s999_charge_rate")
    b, args = _bind(apps, [_rec("a11111", {"charge_rate": _rw("number.a_rate")})])
    desired = desired_args(b, apps, args)
    assert set(desired) == BINDING_SETTINGS | {"inverter_type", "num_inverters"}, sorted(set(desired) ^ (BINDING_SETTINGS | {"inverter_type", "num_inverters"}))
    assert desired["charge_rate"] == ["number.givtcp_sa9999s999_charge_rate", "number.a_rate"] and desired["num_inverters"] == 2 and desired["pv_power"] == [None, 0], desired
    assert desired["soc_percent"] is None and desired["load_today"] is None, desired
    assert args == apps, args  # pure
    print("PASS: desired_args shape")
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
    failures += test_manual_slot_needs_a_modify_binding()
    failures += test_manual_slot_regex_binding()
    failures += test_slot_serials()
    failures += test_slot_serials_skip_summed_settings()
    failures += test_binding_settings_leave_tuning_alone()
    failures += test_build_empty_install()
    failures += test_build_nothing()
    failures += test_build_manual_plus_discovered()
    failures += test_build_takeover()
    failures += test_build_gateway_two_manual_slots()
    failures += test_build_takeover_stable_after_replay()
    failures += test_build_scans_the_given_baseline()
    failures += test_build_dead_slot_taken()
    failures += test_build_fifth_inverter()
    failures += test_build_sticky()
    failures += test_build_release()
    failures += test_build_no_release_on_empty_report()
    failures += test_build_retained_absent_source()
    failures += test_build_sticky_never_overrides_unrelated_manual()
    failures += test_build_displaced_sticky_waits_for_kept_sticky()
    failures += test_build_duplicate_device_id()
    failures += test_build_hole_warns()
    failures += test_build_site_setting()
    failures += test_build_pv_only()
    failures += test_build_user_extras()
    failures += test_build_summed_from_apps_yaml()
    failures += test_build_manual_settings_shape()
    failures += test_build_record_without_serial_or_entities()
    failures += test_compact_two_first_released()
    failures += test_compact_three_middle_released()
    failures += test_compact_around_manual()
    failures += test_compact_takeover_pinned()
    failures += test_compact_not_on_empty_or_absent_report()
    failures += test_compact_residual_hole()
    failures += test_replay_discovered_only()
    failures += test_replay_clears_unbound()
    failures += test_replay_clear_entry_whole_value()
    failures += test_replay_shortens_long_list()
    failures += test_replay_tuning_untouched()
    failures += test_replay_skip_setting()
    failures += test_replay_keep_user_setting()
    failures += test_replay_keep_single_value_whole_key()
    failures += test_replay_skip_keep_keep_live()
    failures += test_replay_scalar_becomes_list()
    failures += test_replay_value_binding()
    failures += test_replay_compact_settings()
    failures += test_replay_pv_power_indexed()
    failures += test_replay_manual_summed_entries()
    failures += test_replay_hole_cleared()
    failures += test_replay_site_and_count()
    failures += test_replay_all_none_key_deleted()
    failures += test_replay_idempotent()
    failures += test_replay_single_entry()
    failures += test_replay_leaves_manual_change_alone()
    failures += test_replay_repairs_overwrite()
    failures += test_replay_never_zero_inverters()
    failures += test_replay_nothing_discovered()
    failures += test_desired_args_shape()
    return failures

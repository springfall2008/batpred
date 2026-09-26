# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the shared discovery contract checks (tests/discovery_contract.py)."""

from coordinator import CAPABILITY_KEYS
from config import INVERTER_DEF
from tests.discovery_contract import assert_definition_complete, assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, dummied_settings, validated_inverters


def _sunsynk_like_record():
    """A record that exactly rebuilds the SunsynkCloud row."""
    row = INVERTER_DEF["SunsynkCloud"]
    return {
        "device_id": "test:SN1",
        "inverter_type": "SunsynkCloud",
        "capabilities": {key: row.get(key, key == "target_soc_used_for_discharge") for key in CAPABILITY_KEYS},
        "entities": {
            "soc_percent": {"entity_id": "sensor.soc", "access": "r", "unit": "%"},
            "charge_rate": {"entity_id": "number.rate", "access": "rw", "unit": "W"},
            "charge_start_time": {"entity_id": "select.start", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "charge_limit": {"entity_id": "number.limit", "access": "rw"},
            "reserve": {"entity_id": "number.reserve", "access": "rw"},
            "scheduled_charge_enable": {"entity_id": "switch.c", "access": "rw"},
            "scheduled_discharge_enable": {"entity_id": "switch.d", "access": "rw"},
            "schedule_write_button": {"entity_id": "switch.w", "access": "rw"},
            "grid_power": {"entity_id": "sensor.grid", "access": "r", "invert": True},
        },
        "ratings": {"inverter_limit": 8000},
    }


class _FakeComponent:
    """Stands in for a component: automatic_config() binds a few settings the way real ones do."""

    def __init__(self, bindings):
        """Remember the (setting, value) pairs automatic_config() will bind."""
        self.bindings = bindings

    def set_arg(self, arg, value):
        """Must never be reached - the capture replaces it."""
        raise AssertionError("set_arg was not captured")

    def set_arg_auto(self, arg, value, overwrite=True):
        """Must never be reached - the capture replaces it."""
        raise AssertionError("set_arg_auto was not captured")

    async def automatic_config(self):
        """Bind every remembered setting, alternating the two setters as real components do."""
        for n, (arg, value) in enumerate(self.bindings):
            (self.set_arg if n % 2 else self.set_arg_auto)(arg, value)


def test_complete_record_passes():
    """A record that rebuilds its row passes the completeness check."""
    assert_definition_complete(_sunsynk_like_record(), 2)
    print("PASS: complete record passes")
    return 0


def test_incomplete_record_fails_with_the_field_named():
    """Dropping a capability, or binding a setting the row dummies, fails and names the field."""
    record = _sunsynk_like_record()
    del record["capabilities"]["support_feedin_first"]
    try:
        assert_definition_complete(record, 2)
    except AssertionError as error:
        assert "support_feedin_first" in str(error), error
    else:
        raise AssertionError("a missing capability must fail completeness")
    record = _sunsynk_like_record()
    record["entities"]["pause_mode"] = {"entity_id": "select.pause", "access": "rw"}
    try:
        assert_definition_complete(record, 2)
    except AssertionError as error:
        assert "has_timed_pause" in str(error), error
    else:
        raise AssertionError("a setting the row dummies must fail completeness")
    print("PASS: incomplete record fails with the field named")
    return 0


def test_capture_records_both_setters_and_restores_them():
    """capture_automatic_config() sees set_arg and set_arg_auto calls and puts the real methods back."""
    component = _FakeComponent([("soc_percent", ["sensor.soc"]), ("num_inverters", 1)])
    captured = capture_automatic_config(component)
    assert captured == {"soc_percent": ["sensor.soc"], "num_inverters": 1}, captured
    assert "set_arg" not in vars(component) and "set_arg_auto" not in vars(component)
    print("PASS: capture sees both setters")
    return 0


def test_agreement_accepts_matching_record_and_skips_exclusions():
    """Entity ids, invert flags, ratings and the out-of-record settings all line up."""
    captured = {
        "inverter_type": ["SunsynkCloud"],
        "num_inverters": 1,
        "soc_percent": ["sensor.soc"],
        "grid_power_invert": [True],
        "inverter_limit": [8000],
        "pause_mode": ["select.never_used"],
        "givtcp_rest": None,
    }
    assert_record_agrees(_sunsynk_like_record(), captured)
    print("PASS: agreement accepts a matching record")
    return 0


def test_agreement_names_each_disagreement():
    """A wrong entity id, a missing invert and a missing rating are each reported."""
    captured = {"soc_percent": ["sensor.other"], "battery_power_invert": ["True"], "export_limit": [3680]}
    try:
        assert_record_agrees(_sunsynk_like_record(), captured)
    except AssertionError as error:
        text = str(error)
        assert "soc_percent" in text and "battery_power_invert" in text and "export_limit" in text, text
    else:
        raise AssertionError("disagreements must fail")
    print("PASS: agreement names each disagreement")
    return 0


def test_dummied_settings_follow_the_row():
    """SolisCloud has no reserve, and no cloud type has a GE mode entity."""
    assert "reserve" in dummied_settings("SolisCloud")
    assert "reserve" not in dummied_settings("SunsynkCloud")
    assert "inverter_mode" in dummied_settings("SunsynkCloud")
    assert "inverter_mode" not in dummied_settings("GE")
    print("PASS: dummied settings follow the row")
    return 0


def test_validated_inverters_refuses_a_dropped_record():
    """A report whose record the validator drops fails loudly instead of testing nothing."""
    assert len(validated_inverters({"inverters": [_sunsynk_like_record()]})) == 1
    try:
        validated_inverters({"inverters": [{"inverter_type": "SunsynkCloud"}]})
    except AssertionError:
        pass
    else:
        raise AssertionError("a dropped record must fail")
    print("PASS: validated_inverters refuses drops")
    return 0


def test_named_exception_is_skipped_and_must_still_differ():
    """except_fields skips a deliberately different field, and fails once that field matches the row again."""
    record = _sunsynk_like_record()
    record["entities"]["inverter_time"] = {"entity_id": "sensor.t", "access": "r", "format": "%Y-%m-%dT%H:%M:%S%z"}
    assert_definition_complete(record, 2, except_fields=("clock_time_format",))
    record["entities"]["inverter_time"]["format"] = INVERTER_DEF["SunsynkCloud"]["clock_time_format"]
    try:
        assert_definition_complete(record, 2, except_fields=("clock_time_format",))
    except AssertionError as error:
        assert "no longer needed" in str(error), error
    else:
        raise AssertionError("a stale exception must fail")
    print("PASS: named exception skipped and kept honest")
    return 0


def test_site_setting_expected_only_on_the_first_record():
    """battery_temperature_history is checked on index 0 and ignored for later devices."""
    record = _sunsynk_like_record()
    captured = {"battery_temperature_history": "sensor.temp_history"}
    try:
        assert_record_agrees(record, captured, index=0)
    except AssertionError as error:
        assert "battery_temperature_history" in str(error), error
    else:
        raise AssertionError("the first record must carry the site setting")
    assert_record_agrees(record, captured, index=1)
    print("PASS: site setting on the first record only")
    return 0


def test_upper_case_serial_entity_id_is_an_entity_id():
    """An entity id built from an upper-case serial is still compared as an entity id, not a literal."""
    record = _sunsynk_like_record()
    record["entities"]["soc_percent"]["entity_id"] = "sensor.predbat_gecloud_SA2243G277_soc"
    assert_record_agrees(record, {"soc_percent": ["sensor.predbat_gecloud_SA2243G277_soc"]})
    print("PASS: upper-case serial entity id")
    return 0


def test_reverse_check_names_extra_settings_and_honours_allowed_extra():
    """A record entity automatic_config() did not bind fails, unless it is named in allowed_extra."""
    record = _sunsynk_like_record()
    captured = {name: ["x.y"] for name in record["entities"] if name != "grid_power"}
    try:
        assert_record_binds_nothing_extra(record, captured)
    except AssertionError as error:
        assert "grid_power" in str(error), error
    else:
        raise AssertionError("an unbound record entity must fail")
    assert_record_binds_nothing_extra(record, captured, allowed_extra=("grid_power",))
    print("PASS: reverse check")
    return 0


def run_discovery_contract_tests(my_predbat=None):
    """Run every discovery contract test, returning the number of failures."""
    failures = 0
    failures += test_complete_record_passes()
    failures += test_incomplete_record_fails_with_the_field_named()
    failures += test_capture_records_both_setters_and_restores_them()
    failures += test_agreement_accepts_matching_record_and_skips_exclusions()
    failures += test_agreement_names_each_disagreement()
    failures += test_dummied_settings_follow_the_row()
    failures += test_validated_inverters_refuses_a_dropped_record()
    failures += test_named_exception_is_skipped_and_must_still_differ()
    failures += test_site_setting_expected_only_on_the_first_record()
    failures += test_upper_case_serial_entity_id_is_an_entity_id()
    failures += test_reverse_check_names_extra_settings_and_honours_allowed_extra()
    return failures

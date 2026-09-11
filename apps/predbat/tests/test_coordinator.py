# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery catalogue coordinator (coordinator.py) - container validation and report collection."""

from mock_base import MockBase
from coordinator import Coordinator, SCHEMA_VERSION


def _coordinator():
    """A Coordinator on a bare MockBase."""
    base = MockBase()
    return base, Coordinator(base)


def test_report_keeps_valid_containers():
    """A well-formed inverter record survives validation intact."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{
        "device_id": "givtcp:SN1",
        "inverter_type": "GE",
        "functions": ["solar", "battery"],
        "hardware_ids": {"serial": "SN1"},
        "info": {"firmware": "D0.451"},
        "ratings": {"battery_kwh": 9.5, "max_charge_w": 3600},
        "entities": {"soc_kw": {"entity_id": "sensor.predbat_givtcp_0_soc_kw", "domain": "sensor", "access": "r", "unit": "kWh"}},
    }]})
    record = coordinator.reports["givtcp"]["inverters"][0]
    assert record["hardware_ids"] == {"serial": "SN1"}
    assert record["ratings"]["battery_kwh"] == 9.5
    assert record["functions"] == ["solar", "battery"]
    assert record["entities"]["soc_kw"]["entity_id"] == "sensor.predbat_givtcp_0_soc_kw"
    print("PASS: valid containers preserved")
    return 0


def test_ratings_rejects_non_numeric():
    """A numbers-only container drops a string value rather than emitting it - the structural safety guarantee."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "ratings": {"battery_kwh": 9.5, "owner": "Bob Smith, 12 Acacia Ave"}}]})
    ratings = coordinator.reports["givtcp"]["inverters"][0]["ratings"]
    assert ratings == {"battery_kwh": 9.5}, ratings
    print("PASS: non-numeric dropped from ratings")
    return 0


def test_vocabulary_rejects_free_text():
    """Vocabulary lists take short lowercase tokens only, so free text cannot ride in on one."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "functions": ["battery", "My Home Battery!", "SOLAR"]}]})
    assert coordinator.reports["givtcp"]["inverters"][0]["functions"] == ["battery"]
    print("PASS: vocabulary constraint enforced")
    return 0


def test_info_rejects_email_shaped_and_long_strings():
    """info takes vendor descriptors, not addresses - an @ or an over-long string is dropped."""
    base, coordinator = _coordinator()
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1", "info": {"vendor": "Ohme", "login": "bob@example.com", "notes": "x" * 200}}]})
    assert coordinator.reports["ohme"]["chargers"][0]["info"] == {"vendor": "Ohme"}
    print("PASS: info rejects email-shaped and over-long values")
    return 0


def test_credential_named_field_rejected():
    """A field whose NAME trips is_secret_key is refused in any container, however it is nested."""
    base, coordinator = _coordinator()
    coordinator.report("fox", {"inverters": [{"device_id": "fox:SN1", "info": {"model": "H3", "api_key": "abcd1234", "auth_token": "zzz"}}]})
    assert coordinator.reports["fox"]["inverters"][0]["info"] == {"model": "H3"}
    print("PASS: credential-named fields refused")
    return 0


def test_entities_credential_named_key_rejected():
    """An entities descriptor KEYED by a credential-shaped name is refused too - the guard covers every container, not just info."""
    base, coordinator = _coordinator()
    coordinator.report("fox", {"inverters": [{
        "device_id": "fox:SN1",
        "entities": {
            "soc_kw": {"entity_id": "sensor.predbat_fox_0_soc_kw", "domain": "sensor", "access": "r"},
            "api_key": {"entity_id": "sensor.predbat_fox_0_api_key", "domain": "sensor", "access": "r"},
        },
    }]})
    entities = coordinator.reports["fox"]["inverters"][0]["entities"]
    assert "soc_kw" in entities
    assert "api_key" not in entities
    print("PASS: entities key named for a credential refused")
    return 0


def test_entities_free_text_unit_dropped_legitimate_descriptor_intact():
    """A free-text value in a typed descriptor field is dropped, while a fully legitimate descriptor survives with every field intact."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{
        "device_id": "givtcp:SN1",
        "entities": {
            "charge_rate": {
                "entity_id": "number.predbat_givtcp_0_charge_rate",
                "domain": "number", "access": "rw", "unit": "W",
                "device_class": "power", "min": 0, "max": 3600, "step": 100, "precision": 0,
            },
            "soc_kw": {
                "entity_id": "sensor.predbat_givtcp_0_soc_kw",
                "domain": "sensor", "access": "r", "unit": "not a real unit " * 6,
            },
        },
    }]})
    entities = coordinator.reports["givtcp"]["inverters"][0]["entities"]
    assert entities["charge_rate"] == {
        "entity_id": "number.predbat_givtcp_0_charge_rate",
        "domain": "number", "access": "rw", "unit": "W",
        "device_class": "power", "min": 0, "max": 3600, "step": 100, "precision": 0,
    }, entities["charge_rate"]
    assert entities["soc_kw"]["entity_id"] == "sensor.predbat_givtcp_0_soc_kw"
    assert "unit" not in entities["soc_kw"]
    print("PASS: free-text descriptor field dropped, legitimate descriptor intact")
    return 0


def test_entities_options_preserves_realistic_values():
    """options takes bounded strings, not vocabulary tokens - realistic select values like time strings and mixed-case labels survive."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{
        "device_id": "givtcp:SN1",
        "entities": {
            "charge_start_time": {"entity_id": "select.predbat_givtcp_0_charge_start_time", "domain": "select", "access": "rw", "options": ["00:00", "00:30"]},
            "pause_mode": {"entity_id": "select.predbat_givtcp_0_pause_mode", "domain": "select", "access": "rw", "options": ["Disabled", "PauseCharge"]},
        },
    }]})
    entities = coordinator.reports["givtcp"]["inverters"][0]["entities"]
    assert entities["charge_start_time"]["options"] == ["00:00", "00:30"], entities["charge_start_time"]
    assert entities["pause_mode"]["options"] == ["Disabled", "PauseCharge"], entities["pause_mode"]
    print("PASS: options preserves realistic time and label values")
    return 0


def test_coverage_accepts_numbers_booleans_and_vocabulary_lists():
    """coverage accepts numbers, booleans and lists of vocabulary tokens - Solcast's pv10/pv50/pv90 variants must survive whole."""
    base, coordinator = _coordinator()
    coordinator.report("solcast", {"forecasts": [{"device_id": "solcast:site1", "kind": "solar", "coverage": {"horizon_hours": 168, "variants": ["pv10", "pv50"]}}]})
    coverage = coordinator.reports["solcast"]["forecasts"][0]["coverage"]
    assert coverage == {"horizon_hours": 168, "variants": ["pv10", "pv50"]}, coverage
    print("PASS: coverage accepts numbers and vocabulary-token lists")
    return 0


def test_validate_report_never_raises_on_non_dict():
    """A non-dict report (None, a list, ...) never raises - it is treated as empty, per the 'never raises' contract."""
    base, coordinator = _coordinator()
    coordinator.report("broken", None)
    assert coordinator.reports["broken"] == {"schema_version": SCHEMA_VERSION, "automatic": True}
    coordinator.report("broken2", ["not", "a", "dict"])
    assert coordinator.reports["broken2"] == {"schema_version": SCHEMA_VERSION, "automatic": True}
    print("PASS: non-dict report never raises")
    return 0


def test_unknown_container_dropped():
    """A container this schema does not know is dropped - there is no untyped path into the catalogue."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "scratchpad": {"anything": "at all"}}]})
    assert "scratchpad" not in coordinator.reports["givtcp"]["inverters"][0]
    print("PASS: unknown container dropped")
    return 0


def test_record_without_device_id_dropped():
    """device_id is the one genuinely required field; a record without it cannot be referred to, so it is dropped."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"hardware_ids": {"serial": "SN1"}}, {"device_id": "givtcp:SN2"}]})
    records = coordinator.reports["givtcp"]["inverters"]
    assert len(records) == 1 and records[0]["device_id"] == "givtcp:SN2"
    print("PASS: record without device_id dropped")
    return 0


def test_report_is_idempotent_and_versioned():
    """Re-reporting replaces the prior report, and the schema version is stamped."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}, {"device_id": "givtcp:B"}]})
    assert len(coordinator.reports["givtcp"]["inverters"]) == 2
    assert coordinator.reports["givtcp"]["schema_version"] == SCHEMA_VERSION
    print("PASS: report replaces and is versioned")
    return 0


def test_meter_sub_record_validated():
    """A meter's nested tariff record is validated with the same container rules."""
    base, coordinator = _coordinator()
    coordinator.report("octopus", {"meters": [{
        "device_id": "octopus:m1", "direction": "import",
        "account_ids": {"mpan": "1234567890123"},
        "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A"}, "flags": ["agile"], "ratings": {"standing_charge_p": 47.5}, "secret_token": "nope"},
    }]})
    tariff = coordinator.reports["octopus"]["meters"][0]["tariff"]
    assert tariff["info"]["tariff_code"] == "E-1R-AGILE-24-10-01-A"
    assert tariff["flags"] == ["agile"]
    assert "secret_token" not in tariff
    print("PASS: nested tariff record validated")
    return 0


def test_credential_guard_fires_inside_sub_record_container():
    """The credential guard is shared code, so it fires identically for a key nested inside a sub-record's container (tariff.info), not just at record level."""
    base, coordinator = _coordinator()
    coordinator.report("octopus", {"meters": [{
        "device_id": "octopus:m1", "direction": "import",
        "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A", "api_key": "abcd1234"}},
    }]})
    tariff_info = coordinator.reports["octopus"]["meters"][0]["tariff"]["info"]
    assert tariff_info == {"tariff_code": "E-1R-AGILE-24-10-01-A"}, tariff_info
    print("PASS: credential guard fires inside a sub-record container")
    return 0


class _StubRegistry:
    """Stands in for Components so assemble() can derive a status for every registry entry."""

    def __init__(self, active=(), alive=(), errors=None, all_names=()):
        """Record which component names are active, alive, failed to load, and known at all."""
        self._active = set(active)
        self._alive = set(alive)
        self._errors = errors or {}
        self._all = list(all_names)

    def get_all(self):
        """Every component name the registry knows."""
        return list(self._all)

    def is_active(self, name):
        """Whether the component was constructed."""
        return name in self._active

    def is_alive(self, name):
        """Whether the component is running and fresh."""
        return name in self._alive

    def load_error(self, name):
        """Why the component failed to construct, or None."""
        return self._errors.get(name)


def test_assemble_merges_sections_and_tags_source():
    """Records from several components merge into one list per section, each tagged with its source."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A", "inverter_type": "GE"}]})
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:b", "inverter_type": "GEC"}]})
    catalogue = coordinator.assemble()
    sources = sorted(record["source"] for record in catalogue["inverters"])
    assert sources == ["gecloud", "givtcp"], sources
    assert catalogue["schema_version"] == SCHEMA_VERSION
    assert catalogue["generated"]
    print("PASS: sections merged and tagged with source")
    return 0


def test_assemble_component_status():
    """Every registry entry gets a status, distinguishing silent from timed out from failed from absent."""
    base, coordinator = _coordinator()
    base.components = _StubRegistry(active=["givtcp", "octopus", "gecloud"], alive=["givtcp", "octopus"],
                                    errors={"fox": "No module named 'protobuf'"},
                                    all_names=["givtcp", "octopus", "gecloud", "fox", "solis"])
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    components = coordinator.assemble()["components"]
    assert components["givtcp"]["status"] == "ok"
    assert components["octopus"]["status"] == "no_report"      # alive, simply does not report yet
    assert components["gecloud"]["status"] == "not_started"    # active but not alive
    assert components["fox"]["status"] == "load_error"
    assert components["solis"]["status"] == "not_configured"
    print("PASS: component statuses derived")
    return 0


def test_observations_duplicate_serial():
    """The same serial claimed by two components is recorded, not resolved."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SN1", "hardware_ids": {"serial": "SA2242"}}]})
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:sn1", "hardware_ids": {"serial": "sa2242"}}]})
    conflicts = coordinator.assemble()["observations"]["conflicts"]
    duplicate = [entry for entry in conflicts if entry["kind"] == "duplicate_serial"]
    assert duplicate and sorted(duplicate[0]["claimed_by"]) == ["gecloud", "givtcp"]
    assert any(entry["kind"] == "multiple_inverter_sources" for entry in conflicts)
    print("PASS: duplicate serial and multiple sources observed")
    return 0


def test_observations_contested_cars_and_meters():
    """A charger component and an intelligent-device component both claiming cars is recorded, as are two import meters."""
    base, coordinator = _coordinator()
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1"}], "cars": [{"device_id": "ohme:v1"}]})
    coordinator.report("octopus", {"cars": [{"device_id": "octopus:d1"}], "meters": [{"device_id": "octopus:m1", "direction": "import"}]})
    coordinator.report("kraken", {"meters": [{"device_id": "kraken:m1", "direction": "import"}]})
    conflicts = coordinator.assemble()["observations"]["conflicts"]
    kinds = {entry["kind"] for entry in conflicts}
    assert "contested_car_slots" in kinds and "multiple_import_meters" in kinds, kinds
    print("PASS: contested cars and multiple import meters observed")
    return 0


def test_observations_resulting_config():
    """The config discovery did NOT set is recorded alongside it, for comparison."""
    base, coordinator = _coordinator()
    base.args.update({"num_inverters": 2, "num_cars": 1, "inverter_type": ["GE", "GEC"]})
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    resulting = coordinator.assemble()["observations"]["resulting_config"]
    assert resulting["num_inverters"] == 2 and resulting["inverter_type"] == ["GE", "GEC"]
    print("PASS: resulting config recorded")
    return 0


def _redacting_coordinator():
    """A coordinator with a fixed salt, so pseudonym tokens are reproducible inside one test."""
    base, coordinator = _coordinator()
    coordinator.salt = "test-salt-0001"
    return base, coordinator


def test_account_ids_pseudonymised_and_stable():
    """account_ids values never appear in the clear, and the same value maps to the same token throughout."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [{"device_id": "octopus:1234567890123", "direction": "import", "account_ids": {"mpan": "1234567890123", "account": "A-1234ABCD"}}],
            "programmes": [{"device_id": "axle:site1", "kind": "vpp", "meter": "octopus:1234567890123"}],
        },
    )
    catalogue = coordinator.catalogue()
    text = str(catalogue)
    assert "1234567890123" not in text, "raw MPAN must not survive redaction"
    assert "A-1234ABCD" not in text
    meter = catalogue["meters"][0]
    assert meter["account_ids"]["mpan"].startswith("#")
    # the cross-link still resolves to the same meter
    assert catalogue["programmes"][0]["meter"] == meter["device_id"]
    print("PASS: account ids pseudonymised, cross-link preserved")
    return 0


def test_cross_link_resolves_without_coincidental_substring():
    """The device_id/meter cross-link resolves even when device_id does not literally embed the account
    identifier as a substring - proving the link is via noting the owning record's device_id, not a
    coincidence of the previous test's device_id happening to spell out the raw MPAN."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [{"device_id": "octopus:m1", "direction": "import", "account_ids": {"mpan": "1234567890123"}}],
            "programmes": [{"device_id": "axle:site1", "kind": "vpp", "meter": "octopus:m1"}],
        },
    )
    catalogue = coordinator.catalogue()
    meter = catalogue["meters"][0]
    assert meter["device_id"] != "octopus:m1", "device_id of a record with account_ids must itself be pseudonymised"
    assert catalogue["programmes"][0]["meter"] == meter["device_id"]
    print("PASS: cross-link resolves without a coincidental substring match")
    return 0


def test_measures_meter_cross_link_resolves():
    """The measures_meter cross-link (inverter -> meter) resolves the same way as meter (programme -> meter)."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "givtcp",
        {
            "meters": [{"device_id": "givtcp:m1", "direction": "import", "account_ids": {"mpan": "1234567890123"}}],
            "inverters": [{"device_id": "givtcp:inv1", "measures_meter": "givtcp:m1"}],
        },
    )
    catalogue = coordinator.catalogue()
    meter = catalogue["meters"][0]
    assert catalogue["inverters"][0]["measures_meter"] == meter["device_id"]
    print("PASS: measures_meter cross-link resolves")
    return 0


def test_pseudonym_differs_across_salts():
    """The same value under a different installation salt produces a different token."""
    base_a, coordinator_a = _redacting_coordinator()
    base_b, coordinator_b = _coordinator()
    coordinator_b.salt = "a-different-salt"
    report = {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"mpan": "1234567890123"}}]}
    coordinator_a.report("octopus", report)
    coordinator_b.report("octopus", report)
    token_a = coordinator_a.catalogue()["meters"][0]["account_ids"]["mpan"]
    token_b = coordinator_b.catalogue()["meters"][0]["account_ids"]["mpan"]
    assert token_a != token_b
    print("PASS: tokens differ across installations")
    return 0


def test_pseudonym_substituted_inside_entity_ids():
    """A vendor that embeds an identifier in an entity name does not republish what the field just hid."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "solar",
        {
            "forecasts": [
                {
                    "device_id": "solcast:abcdef123456",
                    "kind": "solar",
                    "account_ids": {"site_id": "abcdef123456"},
                    "entities": {"pv_forecast_today": {"entity_id": "sensor.predbat_solcast_abcdef123456_today", "domain": "sensor", "access": "r"}},
                }
            ]
        },
    )
    catalogue = coordinator.catalogue()
    assert "abcdef123456" not in str(catalogue), catalogue["forecasts"][0]["entities"]
    print("PASS: pseudonymised value substituted inside entity ids")
    return 0


def test_serials_and_tariff_codes_stay_clear():
    """Hardware identity and product codes stay readable - they are what makes a bug report diagnosable."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SA2242G123", "hardware_ids": {"serial": "SA2242G123"}, "info": {"firmware": "D0.451"}}]})
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A"}}}]})
    text = str(coordinator.catalogue())
    assert "SA2242G123" in text and "D0.451" in text and "E-1R-AGILE-24-10-01-A" in text
    print("PASS: serials, firmware and tariff codes kept clear")
    return 0


def test_misfiled_identifier_caught_by_shape_guard():
    """An MPAN is a number, so ratings accepts it - the shape guard pseudonymises it anyway and logs."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "ratings": {"standing_charge_p": 47.5, "supply_number": 1234567890123}}]})
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    assert catalogue["meters"][0]["ratings"]["standing_charge_p"] == 47.5, "a real measurement is untouched"
    print("PASS: misfiled identifier caught by the shape guard")
    return 0


def test_misfiled_identifier_inside_entity_descriptor_caught():
    """An MPAN misfiled into a nested entity descriptor field (e.g. its "max") is still caught: entities
    values are descriptor dicts, not scalars, so the shape guard has to recurse into them rather than
    stringify the whole descriptor - the exact gap Correction 2 exists to close."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [
                {
                    "device_id": "octopus:m",
                    "direction": "import",
                    "entities": {"supply_number": {"entity_id": "sensor.predbat_octopus_m_supply_number", "domain": "sensor", "access": "r", "max": 1234567890123}},
                }
            ]
        },
    )
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    entity = catalogue["meters"][0]["entities"]["supply_number"]
    assert entity["entity_id"] == "sensor.predbat_octopus_m_supply_number", "the descriptor itself survives, only the misfiled value is pseudonymised"
    print("PASS: identifier misfiled inside a nested entity descriptor field caught")
    return 0


def test_misfiled_email_inside_entity_id_caught():
    """entity_id is kept verbatim by validation (unlike every other descriptor field, which is cleaned by
    type), so it is the one realistic path an email-shaped value can reach a clear container - info and
    every other string field already refuse "@" at validation. The shape guard is what stands between a
    buggy component's entity_id and a public dump."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1", "entities": {"login": {"entity_id": "sensor.bob@example.com", "domain": "sensor", "access": "r"}}}]})
    assert "bob@example.com" in str(coordinator.catalogue_raw()), "sanity check: the email really does reach the raw catalogue unguarded"
    catalogue = coordinator.catalogue()
    assert "bob@example.com" not in str(catalogue)
    print("PASS: email-shaped entity_id caught by the shape guard")
    return 0


def test_misfiled_identifier_inside_vocabulary_list_caught():
    """A vocabulary token pattern allows digits, so an MPAN-shaped value misfiled into a flags/functions
    list is still caught, even though vocabulary lists are validated and walked differently from the
    dict-shaped clear containers."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "tariff": {"flags": ["agile", "1234567890123"]}}]})
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    assert "agile" in catalogue["meters"][0]["tariff"]["flags"]
    print("PASS: misfiled identifier inside a vocabulary list caught")
    return 0


def test_catalogue_raw_is_unredacted():
    """catalogue_raw is the in-process view and keeps originals, so the redacted path is demonstrably doing work."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"mpan": "1234567890123"}}]})
    assert "1234567890123" in str(coordinator.catalogue_raw())
    print("PASS: raw catalogue retains originals")
    return 0


def test_coordinator_all(my_predbat=None):
    """Run every coordinator test, returning the number of failures."""
    failures = 0
    failures += test_report_keeps_valid_containers()
    failures += test_ratings_rejects_non_numeric()
    failures += test_vocabulary_rejects_free_text()
    failures += test_info_rejects_email_shaped_and_long_strings()
    failures += test_credential_named_field_rejected()
    failures += test_entities_credential_named_key_rejected()
    failures += test_entities_free_text_unit_dropped_legitimate_descriptor_intact()
    failures += test_entities_options_preserves_realistic_values()
    failures += test_coverage_accepts_numbers_booleans_and_vocabulary_lists()
    failures += test_validate_report_never_raises_on_non_dict()
    failures += test_unknown_container_dropped()
    failures += test_record_without_device_id_dropped()
    failures += test_report_is_idempotent_and_versioned()
    failures += test_meter_sub_record_validated()
    failures += test_credential_guard_fires_inside_sub_record_container()
    failures += test_assemble_merges_sections_and_tags_source()
    failures += test_assemble_component_status()
    failures += test_observations_duplicate_serial()
    failures += test_observations_contested_cars_and_meters()
    failures += test_observations_resulting_config()
    failures += test_account_ids_pseudonymised_and_stable()
    failures += test_cross_link_resolves_without_coincidental_substring()
    failures += test_measures_meter_cross_link_resolves()
    failures += test_pseudonym_differs_across_salts()
    failures += test_pseudonym_substituted_inside_entity_ids()
    failures += test_serials_and_tariff_codes_stay_clear()
    failures += test_misfiled_identifier_caught_by_shape_guard()
    failures += test_misfiled_identifier_inside_entity_descriptor_caught()
    failures += test_misfiled_email_inside_entity_id_caught()
    failures += test_misfiled_identifier_inside_vocabulary_list_caught()
    failures += test_catalogue_raw_is_unredacted()
    return failures

# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery catalogue coordinator (coordinator.py) - container validation and report collection."""

from mock_base import MockBase
from coordinator import Coordinator, Redactor, SCHEMA_VERSION


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
    """Stands in for Components so assemble() can derive a status for every registry entry, and load_salt() can find a stub Storage."""

    def __init__(self, active=(), alive=(), errors=None, all_names=(), components=None):
        """Record which component names are active, alive, failed to load, known at all, and any stub components (e.g. storage) registered by name."""
        self._active = set(active)
        self._alive = set(alive)
        self._errors = errors or {}
        self._all = list(all_names)
        self._components = components or {}

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

    def get_component(self, name):
        """The stub component registered under this name, or None."""
        return self._components.get(name)


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
    assert meter["device_id"] != "givtcp:m1", "this test is only load-bearing if device_id actually changed - it must not stay equal by both sides going unredacted"
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
    entity = catalogue["forecasts"][0]["entities"]["pv_forecast_today"]
    assert entity["domain"] == "sensor" and entity["access"] == "r", "legitimate neighbouring descriptor fields are untouched"
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
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1", "info": {"vendor": "Ohme"}, "entities": {"login": {"entity_id": "sensor.bob@example.com", "domain": "sensor", "access": "r"}}}]})
    assert "bob@example.com" in str(coordinator.catalogue_raw()), "sanity check: the email really does reach the raw catalogue unguarded"
    catalogue = coordinator.catalogue()
    assert "bob@example.com" not in str(catalogue)
    assert catalogue["chargers"][0]["info"]["vendor"] == "Ohme", "a legitimate neighbouring field is untouched"
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


# --- Review round 2: root cause A - the shape guard's digit detection was too narrow ---


def test_misfiled_float_identifier_caught():
    """str(1234567890123.0) is '1234567890123.0', which an anchored ^\\d{10,}$ pattern rejects even
    though _clean_number accepts floats happily - any component that passes an API number through
    float() must not be able to launder an identifier that way."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "ratings": {"standing_charge_p": 47.5, "supply_number": 1234567890123.0}}]})
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    assert catalogue["meters"][0]["ratings"]["standing_charge_p"] == 47.5
    print("PASS: a misfiled identifier reported as a float is still caught")
    return 0


def test_misfiled_identifier_embedded_or_separated_caught():
    """An identifier does not have to be a bare digit string to leak: embedded in prose ("MPAN
    1234567890123"), broken up by formatting separators ("1234-5678-90123"), or written as a phone
    number ("+447700900123") - only whitespace was stripped before, so all three previously
    survived."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [
                {"device_id": "octopus:m1", "direction": "import", "info": {"supply": "MPAN 1234567890123"}},
                {"device_id": "octopus:m2", "direction": "import", "info": {"ref": "1234-5678-90123"}},
                {"device_id": "octopus:m3", "direction": "import", "info": {"contact": "+447700900123"}},
            ]
        },
    )
    catalogue = coordinator.catalogue()
    text = str(catalogue)
    assert "1234567890123" not in text
    assert "1234-5678-90123" not in text and "5678" not in text
    assert "447700900123" not in text
    assert catalogue["meters"][1]["device_id"] == "octopus:m2", "an ordinary device_id is a completely different shape and is untouched"
    print("PASS: an embedded, separator-formatted or phone-shaped identifier is caught")
    return 0


def test_misfiled_identifier_grouped_by_underscore_or_comma_caught():
    """Adversarial pass: an identifier grouped with "_" (a Python-style numeric literal) or ","
    (thousands separators) reads identically to a human as the same identifier, but was found to
    survive redaction untouched when only whitespace/hyphen/slash/dot were stripped before the
    digit-run search - fixed by widening the stripped separator set rather than narrowing this
    test to only the separators already handled."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [
                {"device_id": "octopus:m1", "direction": "import", "info": {"ref": "1_234_567_890_123"}, "ratings": {"standing_charge_p": 47.5}},
                {"device_id": "octopus:m2", "direction": "import", "info": {"ref": "1,234,567,890,123"}},
            ]
        },
    )
    catalogue = coordinator.catalogue()
    text = str(catalogue)
    assert "1_234_567_890_123" not in text and "1,234,567,890,123" not in text
    assert "1234567890123" not in text
    assert catalogue["meters"][0]["ratings"]["standing_charge_p"] == 47.5, "a real measurement is untouched"
    print("PASS: an underscore- or comma-grouped identifier is caught")
    return 0


def test_location_shaped_key_pseudonymised_regardless_of_value_shape():
    """latitude/longitude cannot be recognised as a pair from one value, so the guard checks the
    KEY NAME instead - this sidesteps the pair problem entirely and cannot false-positive on an
    ordinary rating like battery_kwh: 9.5."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("solar", {"forecasts": [{"device_id": "solcast:site1", "kind": "solar", "ratings": {"latitude": 51.5074, "longitude": -0.1278, "horizon_hours": 168}}]})
    catalogue = coordinator.catalogue()
    ratings = catalogue["forecasts"][0]["ratings"]
    assert str(ratings["latitude"]).startswith("#") and ratings["latitude"] != 51.5074
    assert str(ratings["longitude"]).startswith("#") and ratings["longitude"] != -0.1278
    assert ratings["horizon_hours"] == 168, "an ordinary rating under an unrelated key name is untouched"
    print("PASS: a location-named key is pseudonymised regardless of its value's shape")
    return 0


# --- Review round 3: NEW-1 - the shape guard, widened for round 2, over-corrected on ordinary floats ---


def test_legitimate_long_float_survives_unchanged_and_still_numeric():
    """NEW-1 (Critical): stripping "." and searching the whole repr for a 10+ digit run means any
    float whose repr carries that many digits after the decimal point gets pseudonymised - and a
    numbers-only container silently starts handing consumers a str. An efficiency, a percentage or
    a unit conversion routinely produces exactly such a float. The guard must judge a NUMERIC
    value on its INTEGER PART's digit count, not its full stringified repr."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "givtcp",
        {"inverters": [{"device_id": "givtcp:inv1", "ratings": {"third": 1 / 3, "long_division": 700 / 11, "precise": 17 / 6, "float_sum": 0.1 + 0.2, "big_but_legit": 123456789.0}}]},
    )
    catalogue = coordinator.catalogue()
    ratings = catalogue["inverters"][0]["ratings"]
    for key, expected in (("third", 1 / 3), ("long_division", 700 / 11), ("precise", 17 / 6), ("float_sum", 0.1 + 0.2), ("big_but_legit", 123456789.0)):
        assert ratings[key] == expected, (key, ratings[key])
        assert isinstance(ratings[key], float), "a numbers-only container must still hand back a number, not a str"
    print("PASS: a legitimate long float survives unchanged and stays numeric")
    return 0


# --- Review round 3: NEW-4 - hardware_ids over-pseudonymised real vendor serials ---


def test_hardware_ids_only_flags_all_digit_values_not_prefixed_serials():
    """NEW-4 (Important): a letter-prefixed serial with a long digit tail is the industry-standard
    shape ("HV2160123456", "1102B1234567890", "SVT1234567890") and hardware_ids exists precisely
    so a serial survives in the clear - over-hiding it is a real design regression, not a safe
    default. Inside hardware_ids only, the digit-run guard must fire solely when the value is
    nothing BUT digits; a bare all-digit string there is still genuinely suspicious."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "givtcp",
        {
            "inverters": [
                {
                    "device_id": "givtcp:inv1",
                    "hardware_ids": {"serial": "HV2160123456", "battery_serial": "1102B1234567890", "wifi_serial": "SVT1234567890", "mpan_misfiled": "1234567890123"},
                }
            ]
        },
    )
    catalogue = coordinator.catalogue()
    hardware_ids = catalogue["inverters"][0]["hardware_ids"]
    assert hardware_ids["serial"] == "HV2160123456"
    assert hardware_ids["battery_serial"] == "1102B1234567890"
    assert hardware_ids["wifi_serial"] == "SVT1234567890"
    assert hardware_ids["mpan_misfiled"] != "1234567890123" and str(hardware_ids["mpan_misfiled"]).startswith("#"), "a BARE all-digit value in hardware_ids is still caught"
    print("PASS: prefixed vendor serials stay clear in hardware_ids; a bare all-digit value is still caught")
    return 0


# --- Review round 2: root cause B - the substitution pass was too narrow and too broad ---


def test_pseudonymised_value_substituted_inside_entity_id_value():
    """A site id embedded in an entity_id VALUE is still hidden wherever it appears - substring
    replacement is still full-strength for values, only dict KEYS are exact-match-only (see
    NEW-3/test_account_ids_value_does_not_corrupt_structural_or_descriptor_keys below, which is
    what a version of this test that also embedded the id in the descriptor's KEY would now have
    to accept as a residual, deliberate trade-off)."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "solar",
        {
            "forecasts": [
                {
                    "device_id": "solcast:site1",
                    "kind": "solar",
                    "account_ids": {"site_id": "abcdef123456"},
                    "entities": {"pv_today": {"entity_id": "sensor.pv_abcdef123456_today", "domain": "sensor", "access": "r"}},
                }
            ]
        },
    )
    catalogue = coordinator.catalogue()
    entity = catalogue["forecasts"][0]["entities"]["pv_today"]
    assert "abcdef123456" not in entity["entity_id"], entity
    assert entity["domain"] == "sensor" and entity["access"] == "r", "a legitimate neighbouring descriptor field is untouched"
    print("PASS: pseudonymised value substituted inside an entity_id value")
    return 0


def test_account_ids_value_does_not_corrupt_structural_or_descriptor_keys():
    """NEW-3: an account_ids value can be as ordinary as a short English word - nothing forces a
    component to report something identifier-shaped there - and substring-rewriting dict keys
    turned exactly that coincidence into an entire published section vanishing: with
    account_ids: {"acct": "charge"}, the top-level "chargers" key was corrupted and the entities
    key "charge_rate" lost half its name. Keys are now rewritten by exact match only, never
    substring, so a section name and a component-chosen entity name both survive intact."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "ohme",
        {
            "chargers": [
                {
                    "device_id": "ohme:CH1",
                    "account_ids": {"acct": "charge"},
                    "entities": {"charge_rate": {"entity_id": "number.predbat_ohme_0_charge_rate", "domain": "number", "access": "rw"}},
                }
            ]
        },
    )
    catalogue = coordinator.catalogue()
    assert "chargers" in catalogue, list(catalogue.keys())
    assert "charge_rate" in catalogue["chargers"][0]["entities"], list(catalogue["chargers"][0]["entities"].keys())
    assert catalogue["chargers"][0]["account_ids"]["acct"] != "charge", "the value itself is still pseudonymised"
    print("PASS: an account_ids value does not corrupt structural or descriptor keys via substring")
    return 0


def test_int_identifier_echoed_outside_guarded_container_is_substituted():
    """A non-string scalar (an int) that exactly repeats an account identifier elsewhere in the
    catalogue is rewritten too - substitution previously only ever looked at strings, so an
    identifier echoed as a bare int survived untouched."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"customer": "123456"}, "ratings": {"customer_ref": 123456}}]},
    )
    catalogue = coordinator.catalogue()
    meter = catalogue["meters"][0]
    assert meter["ratings"]["customer_ref"] == meter["account_ids"]["customer"]
    assert meter["ratings"]["customer_ref"] != 123456
    print("PASS: an int identifier echoed outside its guarded container is substituted")
    return 0


def test_numeric_identifier_echo_substituted_regardless_of_int_float_or_string_form():
    """NEW-2 (Important): the int-echo fix above compares str(node), so it missed the FLOAT echo -
    account_ids: {"customer": "123456"} alongside ratings: {"customer_ref": 123456.0} published
    123456.0 in the clear right next to the tokenised original, for any identifier of <=8 digits
    (9+ get caught by the shape guard directly). The same gap works in reverse: a float noted in
    account_ids must be caught when echoed as a plain string elsewhere too."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [
                {"device_id": "octopus:m1", "direction": "import", "account_ids": {"customer": "123456"}, "ratings": {"customer_ref": 123456.0}},
                {"device_id": "octopus:m2", "direction": "import", "account_ids": {"customer": 654321.0}, "info": {"note": "654321"}},
            ]
        },
    )
    catalogue = coordinator.catalogue()
    m1, m2 = catalogue["meters"]
    assert m1["ratings"]["customer_ref"] == m1["account_ids"]["customer"] and m1["ratings"]["customer_ref"] != 123456.0
    assert m2["info"]["note"] == m2["account_ids"]["customer"] and m2["info"]["note"] != "654321"
    print("PASS: a numeric identifier is substituted the same way whether echoed as a string, int or float")
    return 0


def test_structural_scalar_shape_guard_catches_bare_identifier_device_id():
    """A meter reported with a raw MPAN AS its device_id, and no account_ids container at all,
    still gets caught - structural fields sit outside CONTAINER_SPEC, but that must not exempt
    them from the same shape guard clear-container values get."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "1234567890123", "direction": "import"}]})
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    print("PASS: a bare identifier reported directly as a structural field is still caught")
    return 0


def test_device_id_without_identity_data_never_corrupts_unrelated_text():
    """A device_id with no account_ids alongside it carries no identity-linking data at all, so it
    is never noted for cross-linking at all (not whole-string, not substring) - an ordinary word
    used as one (a charger literally named "charger") cannot corrupt unrelated text that happens
    to contain the same word, however common it is. (See NEW-5 below for the complementary case:
    a device_id that IS identity-derived, because its own record carries account_ids, is deliberately
    substring-eligible, since its identity comes from a real identifier and under-redacting it
    would make its token trivially correlatable.)"""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "ohme",
        {
            "chargers": [{"device_id": "charger", "info": {"model": "charger v2"}}],
            "cars": [{"device_id": "ohme:v1", "entities": {"status": {"entity_id": "sensor.ohme_charger_status", "domain": "sensor", "access": "r"}}}],
        },
    )
    catalogue = coordinator.catalogue()
    charger = catalogue["chargers"][0]
    assert charger["device_id"] == "charger", "no account_ids on this record, so nothing marks device_id as identity-derived"
    assert charger["info"]["model"] == "charger v2", "an unrelated string containing the word must not be corrupted"
    assert catalogue["cars"][0]["entities"]["status"]["entity_id"] == "sensor.ohme_charger_status", "an unrelated entity_id containing the word must not be corrupted either"
    print("PASS: a device_id with no account_ids alongside it never corrupts unrelated text")
    return 0


def test_identity_derived_device_id_substituted_wherever_it_is_echoed():
    """NEW-5 (Important): a device_id whose record carries account_ids is identity-derived, but the
    shape guard does not independently catch every such id ("A-AAAA1111" is Octopus-account-shaped
    but has no 10+ digit run) - it was tokenised in place but left in the clear wherever it was
    merely ECHOED elsewhere in the same record (an entity_id, a free-text note), making its token
    trivially correlatable back to the raw text sitting right next to it. An identity-derived
    device_id must be substring-matched too, not just whole-string."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {
            "meters": [
                {
                    "device_id": "A-AAAA1111",
                    "direction": "import",
                    "account_ids": {"mpan": "1234567890123"},
                    "entities": {"pv": {"entity_id": "sensor.octopus_A-AAAA1111_import", "domain": "sensor", "access": "r"}},
                    "info": {"note": "linked to A-AAAA1111"},
                }
            ]
        },
    )
    catalogue = coordinator.catalogue()
    text = str(catalogue)
    assert "A-AAAA1111" not in text, text
    meter = catalogue["meters"][0]
    assert meter["entities"]["pv"]["entity_id"].startswith("sensor.octopus_#") and meter["entities"]["pv"]["entity_id"].endswith("_import")
    print("PASS: an identity-derived device_id is substituted wherever it is echoed, not just where it was noted")
    return 0


def test_substitution_does_not_touch_the_catalogue_timestamp():
    """generated is the catalogue's own timestamp, stamped by assemble() itself rather than any
    component, so a short account identifier that coincidentally matches digits inside it must not
    corrupt it. Constructed directly against Redactor so the collision is deterministic rather than
    depending on the real clock. Also asserts the account id WAS actually redacted elsewhere - a
    no-op redact() would pass the timestamp assertion for the wrong reason."""
    redactor = Redactor("test-salt-0001")
    catalogue = {
        "schema_version": SCHEMA_VERSION,
        "generated": "2026-09-11T12:34:56.123456+00:00",
        "meters": [{"source": "octopus", "device_id": "octopus:m", "direction": "import", "account_ids": {"customer": "123456"}}],
    }
    redacted = redactor.redact(catalogue)
    assert redacted["generated"] == "2026-09-11T12:34:56.123456+00:00"
    assert redacted["meters"][0]["account_ids"]["customer"] != "123456" and redacted["meters"][0]["account_ids"]["customer"].startswith("#"), "redact() must still be doing real work, not a no-op"
    print("PASS: a coincidental digit collision does not corrupt the catalogue's own timestamp, while real redaction still happens")
    return 0


def test_shorter_original_does_not_fragment_a_longer_one():
    """When one noted original is a substring of a longer one, and NEITHER is independently caught
    by the shape guard (both are plain alphabetic strings here, not digit runs), the longer
    original must still be substituted whole rather than leaving fragments of it exposed around a
    shorter token spliced into the middle. The previous version of this test used two identifiers
    that both tripped the shape guard directly (both had an embedded 13-digit run), so it passed
    even with the sort removed and insertion order used instead - this repro is the one that
    actually exercises the substitution ordering fix and nothing else."""
    base, coordinator = _redacting_coordinator()
    coordinator.report(
        "octopus",
        {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"short": "abcdef", "long": "xxabcdefyy"}, "info": {"note": "ref xxabcdefyy end"}}]},
    )
    catalogue = coordinator.catalogue()
    note = catalogue["meters"][0]["info"]["note"]
    assert "abcdef" not in note, note
    assert note.startswith("ref #") and note.endswith(" end"), note
    print("PASS: the longer original is substituted before a shorter one that is its substring")
    return 0


# --- Review round 2: adversarial pass - a raw identifier used as a dict key, not a value ---


def test_misfiled_identifier_used_as_a_container_key_caught():
    """Adversarial: a component keys hardware_ids by the serial itself instead of naming the field.
    Before this fix only VALUES were shape-guarded and only NOTED originals were substituted into
    keys, so a raw identifier that a component used AS a dict key - never a value anywhere -
    reached the published catalogue untouched by either pass."""
    base, coordinator = _redacting_coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:inv1", "hardware_ids": {"1234567890123": "primary", "model": "H3"}}]})
    catalogue = coordinator.catalogue()
    assert "1234567890123" not in str(catalogue)
    assert catalogue["inverters"][0]["hardware_ids"]["model"] == "H3", "a legitimate neighbouring key is untouched"
    print("PASS: an identifier used as a clear-container key is caught")
    return 0


# --- Review round 2: test gaps ---


class _StubStorage:
    """A minimal async Storage stand-in, recording save() calls and replaying them from load()."""

    def __init__(self):
        """Start with nothing stored."""
        self.saved = {}

    async def load(self, module, filename):
        """Return the previously saved payload for this module/filename, or None."""
        return self.saved.get((module, filename))

    async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
        """Record a payload under its module/filename key."""
        self.saved[(module, filename)] = data


def test_load_salt_fallback_without_storage():
    """Without a Storage component (MockBase.components is None), load_salt() falls back to a
    fresh per-process salt rather than running unsalted or reusing a fixed value - previously
    every test set coordinator.salt directly, so this branch had zero coverage."""
    base1, coordinator1 = _coordinator()
    salt1 = coordinator1.load_salt()
    assert len(salt1) == 32 and all(c in "0123456789abcdef" for c in salt1)
    base2, coordinator2 = _coordinator()
    salt2 = coordinator2.load_salt()
    assert salt2 != salt1, "two installations must not somehow end up with the same fallback salt"
    print("PASS: load_salt falls back to a fresh random salt without Storage")
    return 0


def test_load_salt_round_trips_through_storage():
    """With a Storage component available, load_salt() persists the salt and a second coordinator
    sharing that Storage loads the SAME salt rather than minting a new one - the other half of
    load_salt()'s contract that setting coordinator.salt directly in every other test never
    exercised."""
    base1, coordinator1 = _coordinator()
    storage = _StubStorage()
    base1.components = _StubRegistry(components={"storage": storage})
    salt1 = coordinator1.load_salt()

    base2, coordinator2 = _coordinator()
    base2.components = _StubRegistry(components={"storage": storage})
    salt2 = coordinator2.load_salt()

    assert salt1 == salt2, "a second coordinator must load the persisted salt, not mint a new one"
    assert ("coordinator", "salt") in storage.saved
    print("PASS: load_salt persists through Storage and a fresh coordinator reuses it")
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
    failures += test_misfiled_float_identifier_caught()
    failures += test_misfiled_identifier_embedded_or_separated_caught()
    failures += test_misfiled_identifier_grouped_by_underscore_or_comma_caught()
    failures += test_location_shaped_key_pseudonymised_regardless_of_value_shape()
    failures += test_legitimate_long_float_survives_unchanged_and_still_numeric()
    failures += test_hardware_ids_only_flags_all_digit_values_not_prefixed_serials()
    failures += test_pseudonymised_value_substituted_inside_entity_id_value()
    failures += test_account_ids_value_does_not_corrupt_structural_or_descriptor_keys()
    failures += test_int_identifier_echoed_outside_guarded_container_is_substituted()
    failures += test_numeric_identifier_echo_substituted_regardless_of_int_float_or_string_form()
    failures += test_structural_scalar_shape_guard_catches_bare_identifier_device_id()
    failures += test_device_id_without_identity_data_never_corrupts_unrelated_text()
    failures += test_identity_derived_device_id_substituted_wherever_it_is_echoed()
    failures += test_substitution_does_not_touch_the_catalogue_timestamp()
    failures += test_shorter_original_does_not_fragment_a_longer_one()
    failures += test_misfiled_identifier_used_as_a_container_key_caught()
    failures += test_load_salt_fallback_without_storage()
    failures += test_load_salt_round_trips_through_storage()
    return failures

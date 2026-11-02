# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the discovery catalogue coordinator (coordinator.py) - container validation and report collection."""

import inspect
from datetime import datetime

from mock_base import MockBase
from coordinator import CAPABILITY_KEYS, CONTAINER_SPEC, Coordinator, NOT_APPLICABLE_DEFAULTS, SCHEMA_VERSION, SECTION_SPEC, VOCAB_CONTAINERS, car_record, charger_record, charger_with_stub_car, inverter_definition, inverter_record, validate_report
from config import INVERTER_DEF


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
        "ratings": {"soc_max": 9.5, "battery_rate_max": 3600},
        "entities": {"soc_kw": {"entity_id": "sensor.predbat_givtcp_0_soc_kw", "domain": "sensor", "access": "r", "unit": "kWh"}},
    }]})
    record = coordinator.reports["givtcp"]["inverters"][0]
    assert record["hardware_ids"] == {"serial": "SN1"}
    assert record["ratings"]["soc_max"] == 9.5
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
    """A non-dict report (None, a list, ...) never raises - it is treated as empty, per the 'never
    raises' contract. "automatic" is absent, not defaulted to True: an empty/broken report carries
    no evidence the component has any such concept, matching validate_report()'s "only carry
    'automatic' through when the raw report actually provided one" rule."""
    base, coordinator = _coordinator()
    coordinator.report("broken", None)
    assert coordinator.reports["broken"] == {"schema_version": SCHEMA_VERSION}
    coordinator.report("broken2", ["not", "a", "dict"])
    assert coordinator.reports["broken2"] == {"schema_version": SCHEMA_VERSION}
    print("PASS: non-dict report never raises")
    return 0


def test_validate_report_never_raises_on_a_non_list_section():
    """Review round: a section that is not a list of records is dropped whole, never iterated.

    The "never raises" contract has to hold for a malformed SECTION exactly as it already does for
    a malformed report: a scalar ({"inverters": 1}) is not iterable at all, so it raised TypeError
    straight out of report(), and a bare string would have iterated as its characters - one log
    line per character, for a report that was simply the wrong shape.
    """
    base, coordinator = _coordinator()
    coordinator.report("broken", {"inverters": 1, "meters": "octopus:1", "cars": {"device_id": "x"}})
    assert coordinator.reports["broken"] == {"schema_version": SCHEMA_VERSION}, coordinator.reports["broken"]

    # A valid section alongside a malformed one still survives - the drop is per section, not per report
    coordinator.report("mixed", {"inverters": 1, "meters": [{"device_id": "octopus:m1", "direction": "import"}]})
    assert "inverters" not in coordinator.reports["mixed"]
    assert coordinator.reports["mixed"]["meters"][0]["device_id"] == "octopus:m1"
    print("PASS: a non-list section is dropped rather than iterated")
    return 0


def test_automatic_field_absent_when_component_omits_it():
    """A component with no automatic-config concept (Solcast: solar-forecast sourcing is a plain
    apps.yaml choice, never something this catalogue auto-wires) omits "automatic" from its report
    entirely, and neither the cleaned report nor the assembled catalogue's component status may
    default it to True on its behalf - that would claim an auto-config relationship that does not
    exist. A component that DOES report it (every other v1 reporter) keeps whatever value it sent,
    explicit False included."""
    base, coordinator = _coordinator()
    coordinator.report("solar", {"forecasts": [{"device_id": "solcast:site1", "kind": "solar"}]})
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1"}], "automatic": False})
    assert "automatic" not in coordinator.reports["solar"], coordinator.reports["solar"]
    assert coordinator.reports["ohme"]["automatic"] is False
    components = coordinator.assemble()["components"]
    assert "automatic" not in components["solar"], components["solar"]
    assert components["ohme"]["automatic"] is False
    print("PASS: automatic is absent when a component's report never provided it, and preserved otherwise")
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


def test_inverter_record_omits_empty_containers():
    """An empty or unset container is left out of the record rather than written as {} or [].

    Every reporter previously wrote its own `if info: record["info"] = info` ladder. Centralising
    it means a reporter can pass whatever it gathered and let the builder decide, and the shape of
    an inverters record lives in exactly one place.
    """
    record = inverter_record("fox:ABC123", inverter_type="FoxCloud", composition="direct", functions=["solar", "battery"], info={}, ratings={}, entities={}, serials=[])
    assert record == {"device_id": "fox:ABC123", "inverter_type": "FoxCloud", "composition": "direct", "functions": ["solar", "battery"]}, record
    print("PASS: inverter_record omits empty containers")
    return 0


def test_inverter_record_keeps_everything_populated():
    """Every populated field survives, including a falsy-but-real rating like 0 and control=False.

    control is the one top-level field where a falsy value is itself the fact: False means a
    monitor-only inverter. A drop rule "simplified" to `if not value: continue` would silently
    turn every monitor-only inverter into one whose control is unknown, so it is pinned here.
    """
    record = inverter_record(
        "fox:ABC123",
        inverter_type="FoxCloud",
        control=False,
        composition="gateway",
        serials=["S1", "S2"],
        measures_meter="fox:meter:M1",
        functions=["solar"],
        capabilities={"support_charge_freeze": True},
        hardware_ids={"serial": "ABC123"},
        info={"model": "H3-10.0"},
        ratings={"battery_kwh": 10.4, "max_charge_w": 0},
        entities={"charge_rate": {"entity_id": "number.fox_abc123_charge_rate", "domain": "number", "access": "rw"}},
    )
    assert record["serials"] == ["S1", "S2"]
    assert record["measures_meter"] == "fox:meter:M1"
    assert record["capabilities"] == {"support_charge_freeze": True}
    assert record["hardware_ids"] == {"serial": "ABC123"}
    assert record["ratings"]["max_charge_w"] == 0, "a real zero rating is data, not an empty container"
    assert record["entities"]["charge_rate"]["domain"] == "number"
    assert record["control"] is False, "control=False is a fact (monitor-only), not an empty value: {}".format(record)
    print("PASS: inverter_record keeps every populated field")
    return 0


def test_inverter_record_round_trips_through_validation():
    """A record the builder produced survives validate_report() unchanged.

    The builder's whole job is producing something the coordinator will accept. If a field name
    here ever drifts from SECTION_SPEC's structural list, validation silently drops it - so pin
    that the two agree rather than trusting they do.
    """
    record = inverter_record("fox:ABC123", inverter_type="FoxCloud", composition="direct", serials=["S1"], measures_meter="fox:meter:M1", functions=["solar"], hardware_ids={"serial": "ABC123"}, info={"model": "H3"}, ratings={"battery_kwh": 10.4})
    cleaned = validate_report({"inverters": [record]}, "fox", print)["inverters"][0]
    for field in ("device_id", "inverter_type", "composition", "serials", "measures_meter", "functions", "hardware_ids", "info", "ratings"):
        assert field in cleaned, "{} was dropped by validate_report - builder and SECTION_SPEC disagree: {}".format(field, cleaned)
    assert cleaned == record, "validate_report() changed a built record:\n  built:   {}\n  cleaned: {}".format(record, cleaned)
    print("PASS: a built record survives validation with every field intact")
    return 0


def test_inverter_record_parameters_match_the_inverters_schema():
    """inverter_record()'s parameters are exactly the inverters section's fields - no more, no fewer.

    The builder spells the schema out as named parameters so a mistyped field is a TypeError at
    the call site. That only holds while its parameter list and the coordinator's schema agree:
    rename a container in CONTAINER_SPEC, or add a structural field to SECTION_SPEC, and the builder
    would go on emitting the old name, which validate_report() then silently drops from every
    reporter's records at once. This passes today; its job is to fail the day the two drift.
    """
    parameters = set(inspect.signature(inverter_record).parameters)
    assert "device_id" in parameters, "device_id is the record's identity and must stay a parameter: {}".format(sorted(parameters))
    schema = (set(SECTION_SPEC["inverters"]["structural"]) - {"device_id"}) | set(CONTAINER_SPEC) | set(VOCAB_CONTAINERS)
    fields = parameters - {"device_id"}
    assert fields == schema, "inverter_record() and the inverters schema disagree - missing from the builder: {}, not in the schema: {}".format(sorted(schema - fields), sorted(fields - schema))
    print("PASS: inverter_record's parameters are exactly the inverters schema")
    return 0


def test_inverter_record_fields_are_keyword_only():
    """Only device_id may be passed positionally; every other field must be named.

    Fifteen optional parameters in a row are an easy place to slip by one: accepted positionally,
    inverter_record("x:1", "direct", True) would quietly build inverter_type="direct",
    control=True - a wrong record with nothing to say so.
    """
    try:
        inverter_record("gecloud:x", "direct", True)
    except TypeError:
        pass
    else:
        raise AssertionError("a positional second argument must be a TypeError, not silently taken as inverter_type")
    parameters = inspect.signature(inverter_record).parameters
    positional = [name for name, parameter in parameters.items() if parameter.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert positional == ["device_id"], "only device_id may be positional, got {}".format(positional)
    print("PASS: inverter_record's schema fields are keyword-only")
    return 0


def test_inverter_record_copies_the_callers_containers():
    """The record holds its own copies of the containers, never the caller's live objects.

    refresh_discovery() stores the report it filed and compares the next build against it with ==.
    If the record held the caller's own list - a reporter passing serials=self.serials - then
    growing that list would grow the STORED report too, the rebuilt report would compare equal to
    it, and the change would never be filed: a report frozen at its first state, the exact bug
    refresh_discovery() exists to prevent.
    """
    live_serials = ["S1"]
    live_info = {"model": "GIV-GATEWAY"}
    first = inverter_record("gecloud:gateway001", composition="gateway", serials=live_serials, info=live_info)

    live_serials.append("S2")
    live_info["firmware"] = "ARM 1"
    second = inverter_record("gecloud:gateway001", composition="gateway", serials=live_serials, info=live_info)

    assert first != second, "a rebuilt record must differ once the caller's live state has moved on: {}".format(first)
    assert first["serials"] == ["S1"], "the first record's serials changed under it: {}".format(first["serials"])
    assert first["info"] == {"model": "GIV-GATEWAY"}, "the first record's info changed under it: {}".format(first["info"])
    print("PASS: inverter_record copies the caller's containers, so live state cannot freeze a report")
    return 0


def test_inverter_record_normalises_sets_and_tuples():
    """A set or frozenset becomes a sorted list, a tuple becomes a list, and an empty frozenset is omitted.

    validate_report() keeps only a list for a structural or vocabulary field, so anything else
    would be silently dropped from the catalogue. A set is sorted rather than listed as-is: string
    hashing is randomised per process, so list(some_set) comes out in a different order after a
    restart and two dumps of the same hardware would diff for no reason.
    """
    record = inverter_record(
        "gecloud:gateway001",
        serials={"S5", "S3", "S1", "S4", "S2"},
        functions=frozenset({"solar", "battery"}),
        capabilities=frozenset(),
        flags=("monitor_only",),
    )
    assert record["serials"] == ["S1", "S2", "S3", "S4", "S5"], "a set should become a sorted list: {}".format(record.get("serials"))
    assert record["functions"] == ["battery", "solar"], "a frozenset should become a sorted list: {}".format(record.get("functions"))
    assert "capabilities" not in record, "an empty frozenset is empty and must be omitted: {}".format(record)
    assert record["flags"] == ["monitor_only"], "a tuple should become a list: {}".format(record.get("flags"))
    assert "capabilities" not in inverter_record("gecloud:gateway001", capabilities={}), "an empty capabilities dict must be omitted too"
    print("PASS: inverter_record sorts sets, lists tuples and omits an empty frozenset")
    return 0


def test_inverter_record_sorts_a_mixed_type_set_without_raising():
    """A set mixing types that cannot be compared is sorted by string form rather than raising.

    A bare sorted() raises TypeError on {1, "a"}. build_discovery() is called from
    refresh_discovery(), which catches it - so the component survives, but its whole report is
    withheld, on every cycle, because nothing changes the offending data. Sorting by str() makes
    every element comparable, and leaves the order of an all-string set exactly as before.
    """
    record = inverter_record("x:1", serials={1, "a"})
    assert record["serials"] == [1, "a"], "a mixed-type set should sort by string form: {}".format(record.get("serials"))
    print("PASS: inverter_record sorts a mixed-type set by string form instead of raising")
    return 0


def test_inverter_record_drops_an_empty_string():
    """An empty string is unset, like None - but control=False is a real value and is kept."""
    record = inverter_record("fox:ABC123", inverter_type="", measures_meter="", composition="direct", control=False)
    assert "inverter_type" not in record, "an empty inverter_type is unset and must be omitted: {}".format(record)
    assert "measures_meter" not in record, "an empty measures_meter is unset and must be omitted: {}".format(record)
    assert record["composition"] == "direct"
    assert record["control"] is False, "control=False is a bool, not an empty string, and must be kept: {}".format(record)
    print("PASS: inverter_record drops an empty string but keeps control=False")
    return 0


def test_charger_record_omits_empty_containers():
    """A charger record leaves out unset and empty fields exactly as an inverter record does."""
    record = charger_record("hypervolt:ABC123", serves_cars=[], hardware_ids={"serial": "ABC123"}, account_ids={}, info={}, ratings={}, entities={})
    assert record == {"device_id": "hypervolt:ABC123", "hardware_ids": {"serial": "ABC123"}}, record
    print("PASS: charger_record omits empty containers")
    return 0


def test_charger_and_car_record_parameters_match_their_sections():
    """Each builder accepts exactly its section's structural fields plus the containers it can carry.

    The parameter list is the schema, so a structural field added to SECTION_SPEC without a matching
    parameter would have no way into a record built here.
    """
    import inspect

    for builder, section in ((charger_record, "chargers"), (car_record, "cars")):
        params = set(inspect.signature(builder).parameters) - {"device_id"}
        structural = set(SECTION_SPEC[section]["structural"]) - {"device_id"}
        assert structural <= params, "{} is missing structural fields {}".format(builder.__name__, structural - params)
        assert params - structural <= set(CONTAINER_SPEC) | set(VOCAB_CONTAINERS), "{} has unknown parameters {}".format(builder.__name__, params - structural - set(CONTAINER_SPEC) - set(VOCAB_CONTAINERS))
    print("PASS: charger_record and car_record parameters match their sections")
    return 0


def test_charger_with_stub_car_cross_links():
    """A charger that knows nothing of its vehicle gets a stub car, linked both ways."""
    charger, car = charger_with_stub_car("wallbox:900001", hardware_ids={"serial": "900001"}, info={"vendor": "Wallbox"}, entities={})
    assert charger == {"device_id": "wallbox:900001", "serves_cars": ["wallbox:900001:car"], "hardware_ids": {"serial": "900001"}, "info": {"vendor": "Wallbox"}}, charger
    assert car == {"device_id": "wallbox:900001:car", "charged_by": "wallbox:900001", "flags": ["stub"]}, car
    print("PASS: charger_with_stub_car cross-links the charger and its stub car")
    return 0


def test_stub_car_survives_validation():
    """The stub marker reaches the catalogue (GH#5849).

    It used to ride in info as {"stub": True}; info only holds strings, so validation dropped it and
    logged a warning, and a stub car was published indistinguishable from a real vehicle record.
    """
    _, car = charger_with_stub_car("x:1")
    logs = []
    cleaned = validate_report({"cars": [car]}, "x", logs.append)
    assert cleaned["cars"][0].get("flags") == ["stub"], cleaned
    assert not logs, logs
    print("PASS: a stub car keeps its stub flag through validation")
    return 0


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


def test_catalogue_reassembles_so_a_post_assembly_report_reaches_it():
    """Final review, Ruling R42: catalogue() used to return `self.assembled or self.assemble()`,
    and assemble() was only ever called once, at the startup barrier - so a report filed AFTER
    that point (a retry, a rediscovered device, a changed tariff or vehicle, a later-discovered
    forecast site) could never reach a dump, no matter how many times catalogue() was called
    afterwards. catalogue() must re-assemble on every call so later work is always reflected."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A", "inverter_type": "GE"}]})
    coordinator.assemble()
    first = coordinator.catalogue()
    assert len(first["inverters"]) == 1, first["inverters"]

    # A second inverter is reported AFTER assemble() has already run once - simulating GivTCP's own
    # rediscovery, GE Cloud's retry, or any report arriving after the startup barrier.
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A", "inverter_type": "GE"}, {"device_id": "givtcp:B", "inverter_type": "GE"}]})
    second = coordinator.catalogue()
    assert len(second["inverters"]) == 2, "a report filed after assemble() already ran must still reach a later catalogue() call: {}".format(second["inverters"])

    # catalogue_raw() must behave identically - see its own docstring.
    assert len(coordinator.catalogue_raw()["inverters"]) == 2
    print("PASS: catalogue() re-assembles on every call, so a post-assembly report reaches it")
    return 0


def test_assemble_component_status():
    """A configured component gets a status distinguishing silent from timed out from failed.

    A component the user never configured is absent entirely rather than carrying a status: the
    registry holds 30 entries and a handful can report, so listing them all restated a static list
    from components.py on every dump. Absence from the map is its status.
    """
    base, coordinator = _coordinator()
    base.components = _StubRegistry(active=["givtcp", "octopus", "gecloud"], alive=["givtcp", "octopus"],
                                    errors={"fox": "No module named 'protobuf'"},
                                    all_names=["givtcp", "octopus", "gecloud", "fox", "solis"])
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    components = coordinator.assemble()["components"]
    assert components["givtcp"]["status"] == "ok"
    assert components["octopus"]["status"] == "no_report"      # alive, capable of reporting, silent
    assert components["gecloud"]["status"] == "not_started"    # active but not alive
    assert components["fox"]["status"] == "load_error"
    assert "solis" not in components, "a component that was never configured must not appear at all"
    print("PASS: component statuses derived, unconfigured components omitted")
    return 0


class _SilentComponent:
    """A component with no build_discovery() - working as intended, never going to report."""


class _ReportingComponent:
    """A component that carries a reporter, so silence from it is an anomaly worth showing."""

    def build_discovery(self):
        """Report nothing - this stub exists only to be recognised as reporter-carrying."""
        return None


def test_component_status_omits_components_that_cannot_report():
    """An active component with no build_discovery() is left out; one that has it and stayed silent is kept.

    This is the distinction that makes the map worth reading. Before it, "no_report" covered both
    storage/web/db/ha - which are working perfectly and will never report - and a component whose
    reporter genuinely failed to fire. The first group is the overwhelming majority, so it buried
    the second. Dropping it is what turns a silent reporter back into a visible anomaly.
    """
    base, coordinator = _coordinator()
    base.components = _StubRegistry(
        active=["storage", "givtcp"],
        alive=["storage", "givtcp"],
        all_names=["storage", "givtcp"],
        components={"storage": _SilentComponent(), "givtcp": _ReportingComponent()},
    )
    components = coordinator.assemble()["components"]
    assert "storage" not in components, "a component with no reporter must not be filed as no_report"
    assert components["givtcp"]["status"] == "no_report", "a component that CAN report and did not is the anomaly worth keeping"
    print("PASS: only components capable of reporting are held to account for silence")
    return 0


def test_component_status_reported_at_set_only_for_ok():
    """reported_at carries a UTC timestamp for a component that has actually reported, and stays
    None for every other status - the field was previously never populated anywhere, so a
    published catalogue showed reported_at: null forever even for a component reporting normally."""
    base, coordinator = _coordinator()
    base.components = _StubRegistry(active=["givtcp", "octopus"], alive=["octopus"], all_names=["givtcp", "octopus", "solis"])
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A"}]})
    components = coordinator.assemble()["components"]
    assert components["givtcp"]["status"] == "ok"
    reported_at = components["givtcp"]["reported_at"]
    assert reported_at, "an 'ok' component must carry a reported_at timestamp"
    datetime.fromisoformat(reported_at)  # must parse as a real ISO-8601 timestamp, not just be truthy
    assert components["octopus"]["reported_at"] is None, "a component that has not reported must not have a timestamp"
    assert "solis" not in components, "an unconfigured component is absent, so it has no reported_at at all"
    print("PASS: reported_at is set only for a component with status ok")
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


def test_observations_duplicate_serial_sees_a_gateways_fronted_batteries():
    """Review round: a gateway record claims the batteries it fronts, so GE Cloud + GivTCP on the same battery collides.

    GE Cloud in gateway composition reports the GATEWAY's serial in hardware_ids and the fronted
    battery serials in the structural `serials` list, while GivTCP reports each battery's own
    serial in hardware_ids. Grouping only on hardware_ids.serial therefore missed the single most
    likely real-world double-claim - two components controlling the same physical battery - which
    is the observation this catalogue release exists to count.
    """
    base, coordinator = _coordinator()
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:GW1", "hardware_ids": {"serial": "GW1"}, "composition": "gateway", "serials": ["BAT001", "BAT002"]}]})
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:BAT001", "hardware_ids": {"serial": "bat001"}}]})
    conflicts = coordinator.assemble()["observations"]["conflicts"]
    duplicate = [entry for entry in conflicts if entry["kind"] == "duplicate_serial"]
    assert duplicate, "a battery fronted by a gateway and claimed directly by GivTCP is a duplicate serial: {}".format(conflicts)
    assert duplicate[0]["serial"] == "bat001", duplicate
    assert sorted(duplicate[0]["claimed_by"]) == ["gecloud", "givtcp"], duplicate

    # The gateway's own serials must not collide with themselves - one source cannot conflict with itself
    base, coordinator = _coordinator()
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:GW1", "hardware_ids": {"serial": "GW1"}, "composition": "gateway", "serials": ["BAT001", "BAT002"]}]})
    assert not [entry for entry in coordinator.assemble()["observations"]["conflicts"] if entry["kind"] == "duplicate_serial"], "a gateway alone claims nothing twice"
    print("PASS: a gateway's fronted serials take part in duplicate_serial detection")
    return 0


def test_component_load_error_carries_no_free_text():
    """Review round: a load_error status records THAT a component failed, never the exception text.

    components.load_error() is the raw str() of whatever construction raised - unbounded free text
    that could carry a URL, an account value or a credential from a component's own error message.
    Every other value in this document reaches it through a typed container that cannot hold one;
    a dump gets attached to a public issue, so the one field outside that boundary is removed:
    free text can carry a credential under no name the validation's key check would catch. The message stays in the log and on the
    component-status entity, which is where a maintainer already reads it.
    """
    base, coordinator = _coordinator()
    base.components = _StubRegistry(all_names=["fox"], errors={"fox": "auth failed for https://api.example.com/?key=SECRET123"})
    entry = coordinator.assemble()["components"]["fox"]
    assert entry["status"] == "load_error", entry
    assert "error" not in entry, "the raw exception text must not be published into the catalogue: {}".format(entry)
    assert "SECRET123" not in str(coordinator.catalogue()), "no part of an exception message may reach the published catalogue"
    print("PASS: a load_error status carries no free-text exception message")
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


def test_serials_and_tariff_codes_stay_clear():
    """Hardware identity and product codes stay readable - they are what makes a bug report diagnosable."""
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:SA2242G123", "hardware_ids": {"serial": "SA2242G123"}, "info": {"firmware": "D0.451"}}]})
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A"}}}]})
    text = str(coordinator.catalogue())
    assert "SA2242G123" in text and "D0.451" in text and "E-1R-AGILE-24-10-01-A" in text
    print("PASS: serials, firmware and tariff codes kept clear")
    return 0


def test_email_inside_entity_id_scrubbed():
    """entity_id is kept verbatim by validation (unlike every other descriptor field, which is cleaned by
    type), so it is the one realistic path an email address can take into a container - info and every
    other string field already refuse "@" at validation. The Redactor's email scrub is what stands
    between a buggy component's entity_id and a public dump."""
    base, coordinator = _coordinator()
    coordinator.report("ohme", {"chargers": [{"device_id": "ohme:CH1", "info": {"vendor": "Ohme"}, "entities": {"login": {"entity_id": "sensor.bob@example.com", "domain": "sensor", "access": "r"}}}]})
    assert "bob@example.com" in str(coordinator.catalogue_raw()), "sanity check: the email really does reach the raw catalogue"
    catalogue = coordinator.catalogue()
    assert "bob@example.com" not in str(catalogue)
    assert catalogue["chargers"][0]["info"]["vendor"] == "Ohme", "a legitimate neighbouring field is untouched"
    print("PASS: an email address inside an entity_id is scrubbed")
    return 0


def test_catalogue_raw_is_unredacted():
    """catalogue_raw is the in-process view and keeps an email address the published catalogue scrubs; identifiers are the same in both."""
    base, coordinator = _coordinator()
    coordinator.report("octopus", {"meters": [{"device_id": "octopus:m", "direction": "import", "account_ids": {"mpan": "1234567890123", "login": "jane@example.com"}}]})
    assert "jane@example.com" in str(coordinator.catalogue_raw())
    assert "jane@example.com" not in str(coordinator.catalogue())
    assert "1234567890123" in str(coordinator.catalogue_raw()) and "1234567890123" in str(coordinator.catalogue())
    print("PASS: raw catalogue retains the email the published one scrubs")
    return 0


# --- Review round 2: test gaps ---


def test_report_discovery_helper():
    """A component reports through the base-class helper, and does nothing harmful when there is no coordinator."""
    from component_base import ComponentBase

    class _FakeComponent(ComponentBase):
        """Minimal component used to exercise report_discovery."""

        def initialize(self, **kwargs):
            """Nothing to set up."""
            pass

        async def run(self, seconds, first):
            """Never called here."""
            return True

    base, coordinator = _coordinator()
    component = _FakeComponent(base)
    component.component_name = "givtcp"
    component.report_discovery({"inverters": [{"device_id": "givtcp:A"}]})  # no coordinator yet - must not raise
    assert coordinator.reports == {}

    class _Registry:
        """Registry stub exposing the coordinator, as Components does."""

        def __init__(self):
            """Hold the coordinator."""
            self.coordinator = coordinator

        def get_all(self):
            """One known component."""
            return ["givtcp"]

        def is_active(self, name):
            """Active."""
            return True

        def is_alive(self, name):
            """Alive."""
            return True

        def load_error(self, name):
            """No error."""
            return None

    base.components = _Registry()
    component.report_discovery({"inverters": [{"device_id": "givtcp:A"}]})
    assert coordinator.reports["givtcp"]["inverters"][0]["device_id"] == "givtcp:A"
    print("PASS: report_discovery routes to the coordinator and no-ops without one")
    return 0


def test_publish_writes_sensor():
    """publish() writes a SUMMARY to the entity, not the full catalogue.

    Two inverters sharing a serial gives the test a real observations.conflicts entry to check
    threads through, and a hardware_ids container on each record gives it real per-section
    payload that must NOT leak into the entity's attributes - a test that only checked presence
    would not notice a regression back to the full attributes.update(catalogue) dump.
    """
    base, coordinator = _coordinator()
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:A", "inverter_type": "GE", "hardware_ids": {"serial": "SA2242G123"}}]})
    coordinator.report("gecloud", {"inverters": [{"device_id": "gecloud:b", "inverter_type": "GEC", "hardware_ids": {"serial": "SA2242G123"}}]})
    coordinator.assemble()
    coordinator.publish()
    entity = base.entities.get("sensor.predbat_discovery")
    assert entity, base.entities
    attributes = entity["attributes"]

    # Summary half: present and correct
    assert attributes["friendly_name"] == "Predbat discovery"
    assert attributes["icon"] == "mdi:sitemap"
    assert attributes["schema_version"] == SCHEMA_VERSION
    assert attributes["generated"]
    assert attributes["counts"]["inverters"] == 2, attributes["counts"]
    assert attributes["components"]["givtcp"]["status"] == "ok", attributes["components"]
    conflicts = attributes["observations"]["conflicts"]
    assert any(entry["kind"] == "duplicate_serial" for entry in conflicts), conflicts
    assert entity["state"] == sum(attributes["counts"].values())

    # NOT-present half: no section record list anywhere in the attributes
    for section in SECTION_SPEC:
        assert section not in attributes, "{} record list must not reach the entity - it bloats the HA recorder".format(section)

    print("PASS: discovery sensor carries a summary (counts, components, conflicts), never the full catalogue")
    return 0


def _one_inverter(**fields):
    """Validate a single inverter record carrying the given fields and return what survives."""
    record = {"device_id": "test:SN1", "inverter_type": "SunsynkCloud"}
    record.update(fields)
    logs = []
    cleaned = validate_report({"inverters": [record]}, "test", logs.append)
    return (cleaned.get("inverters") or [{}])[0], logs


def test_schema_version_is_two():
    """The document shape changed (dict capabilities, renamed ratings, required access), so the version moved."""
    assert SCHEMA_VERSION == 2
    print("PASS: schema version 2")
    return 0


def test_capabilities_dict_keeps_known_bool_keys():
    """A capabilities dict of the seven behaviour keys survives validation intact."""
    capabilities = {key: True for key in CAPABILITY_KEYS}
    capabilities["can_span_midnight"] = False
    record, _ = _one_inverter(capabilities=capabilities)
    assert record["capabilities"] == capabilities, record
    print("PASS: capabilities dict preserved")
    return 0


def test_capabilities_drops_unknown_key_and_non_bool():
    """An unknown key, or a value that is not a bool, is dropped and logged; the rest survives."""
    record, logs = _one_inverter(capabilities={"support_charge_freeze": True, "has_reserve_soc": True, "can_span_midnight": 1})
    assert record["capabilities"] == {"support_charge_freeze": True}, record
    assert any("has_reserve_soc" in line for line in logs), logs
    assert any("can_span_midnight" in line for line in logs), logs
    print("PASS: capabilities rejects unknown keys and non-bools")
    return 0


def test_capabilities_list_is_dropped():
    """Schema 2: capabilities is a dict of the seven behaviour keys, so a token list no longer validates."""
    record, _ = _one_inverter(capabilities=["schedule", "target_soc"])
    assert "capabilities" not in record, record
    base, coordinator = _coordinator()
    coordinator.report("test", {"inverters": [{"device_id": "test:SN1", "capabilities": ["schedule"]}]})
    assert "capabilities" not in coordinator.catalogue()["inverters"][0]
    print("PASS: capability token list dropped")
    return 0


def test_descriptor_requires_access():
    """An entities descriptor without access rw or r is dropped."""
    record, _ = _one_inverter(entities={
        "soc_percent": {"entity_id": "sensor.a", "access": "r"},
        "charge_limit": {"entity_id": "number.b"},
        "reserve": {"entity_id": "number.c", "access": "write"},
    })
    assert set(record["entities"]) == {"soc_percent"}, record
    print("PASS: descriptor needs access")
    return 0


def test_descriptor_needs_exactly_one_of_entity_id_and_value():
    """entity_id and value are alternatives: both, or neither, drops the descriptor."""
    record, _ = _one_inverter(entities={
        "pv_power": {"value": 0, "access": "r"},
        "grid_power": {"entity_id": "sensor.g", "value": 0, "access": "r"},
        "load_power": {"access": "r"},
    })
    assert record["entities"] == {"pv_power": {"value": 0, "access": "r"}}, record
    print("PASS: exactly one of entity_id/value")
    return 0


def test_descriptor_value_rejects_free_text():
    """A string value must pass the info-string guard: no "@", no over-long text."""
    record, _ = _one_inverter(entities={
        "battery_power_invert_note": {"value": "True", "access": "r"},
        "email": {"value": "someone@example.com", "access": "r"},
        "blob": {"value": "x" * 200, "access": "r"},
    })
    assert set(record["entities"]) == {"battery_power_invert_note"}, record
    print("PASS: descriptor value rejects free text")
    return 0


def test_descriptor_invert_must_be_bool():
    """invert is kept when it is a bool and dropped (descriptor kept) when it is not."""
    record, _ = _one_inverter(entities={
        "grid_power": {"entity_id": "sensor.g", "access": "r", "invert": True},
        "battery_power": {"entity_id": "sensor.b", "access": "r", "invert": "yes"},
    })
    assert record["entities"]["grid_power"]["invert"] is True, record
    assert "invert" not in record["entities"]["battery_power"], record
    print("PASS: invert must be bool")
    return 0


def test_descriptor_step_percent_of_capacity_must_be_a_percentage():
    """step_percent_of_capacity is kept when it is a percentage above 0 and up to 100, dropped otherwise (#5324)."""
    record, _ = _one_inverter(entities={
        "charge_rate": {"entity_id": "number.c", "access": "rw", "step_percent_of_capacity": 1},
        "discharge_rate": {"entity_id": "number.d", "access": "rw", "step_percent_of_capacity": True},
        "charge_limit": {"entity_id": "number.l", "access": "rw", "step_percent_of_capacity": 0},
        "reserve": {"entity_id": "number.r", "access": "rw", "step_percent_of_capacity": 150},
    })
    assert record["entities"]["charge_rate"]["step_percent_of_capacity"] == 1, record
    for setting in ("discharge_rate", "charge_limit", "reserve"):
        assert "step_percent_of_capacity" not in record["entities"][setting], record
    print("PASS: step_percent_of_capacity must be a percentage")
    return 0


def test_new_containers_survive_redaction_unchanged():
    """Bool capabilities, a strftime format and a string value stand-in pass the redactor untouched."""
    base, coordinator = _coordinator()
    coordinator.report("test", {"inverters": [{
        "device_id": "test:SN1",
        "capabilities": {"support_charge_freeze": True},
        "entities": {
            "inverter_time": {"entity_id": "sensor.t", "access": "r", "format": "%Y-%m-%d %H:%M:%S"},
            "pv_power": {"value": "0", "access": "r"},
        },
    }]})
    record = coordinator.catalogue()["inverters"][0]
    assert record["capabilities"] == {"support_charge_freeze": True}, record
    assert record["entities"]["inverter_time"]["format"] == "%Y-%m-%d %H:%M:%S", record
    assert record["entities"]["pv_power"]["value"] == "0", record
    print("PASS: new containers survive redaction")
    return 0


def _full_record():
    """A record carrying every source inverter_definition() reads, shaped like a cloud inverter."""
    return {
        "device_id": "test:SN1",
        "inverter_type": "SunsynkCloud",
        "capabilities": {key: key != "can_span_midnight" for key in CAPABILITY_KEYS},
        "entities": {
            "soc_percent": {"entity_id": "sensor.soc", "access": "r", "unit": "%"},
            "charge_rate": {"entity_id": "number.rate", "access": "rw", "unit": "W"},
            "charge_start_time": {"entity_id": "select.start", "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "charge_limit": {"entity_id": "number.limit", "access": "rw"},
            "reserve": {"entity_id": "number.reserve", "access": "rw"},
            "scheduled_charge_enable": {"entity_id": "switch.c", "access": "rw"},
            "scheduled_discharge_enable": {"entity_id": "switch.d", "access": "rw"},
            "schedule_write_button": {"entity_id": "switch.w", "access": "rw"},
        },
    }


def test_inverter_definition_builds_every_field_without_a_base():
    """A full record yields every derived field and no gaps."""
    definition, gaps, not_applicable = inverter_definition(_full_record(), 2)
    assert gaps == [], gaps
    assert not_applicable == ["clock_time_format", "current_dp"], not_applicable
    assert definition["support_charge_freeze"] is True and definition["can_span_midnight"] is False
    assert definition["has_charge_enable_time"] and definition["has_discharge_enable_time"]
    assert definition["has_target_soc"] and definition["has_reserve_soc"]
    assert not definition["has_idle_time"] and not definition["has_timed_pause"]
    assert not definition["has_ge_inverter_mode"] and not definition["has_ge_eco_toggle"] and not definition["has_mqtt_api"]
    assert definition["charge_time_entity_is_option"] is True and definition["charge_time_format"] == "HH:MM:SS"
    assert definition["clock_time_format"] == NOT_APPLICABLE_DEFAULTS["clock_time_format"]
    assert definition["soc_units"] == "%" and definition["output_charge_control"] == "power"
    assert definition["time_button_press"] is True and definition["num_load_entities"] == 1
    assert definition["write_and_poll_sleep"] == 2 and definition["name"] == "SunsynkCloud"
    print("PASS: definition built without a base")
    return 0


def test_inverter_definition_presence_needs_a_real_rw_entity():
    """An r entry, or a value stand-in, does not make a presence flag True."""
    record = _full_record()
    record["entities"]["reserve"] = {"entity_id": "number.reserve", "access": "r"}
    record["entities"]["charge_limit"] = {"value": 100, "access": "rw"}
    record["entities"]["pause_mode"] = {"entity_id": "select.pause", "access": "rw"}
    definition, _, _ = inverter_definition(record, 2)
    assert definition["has_reserve_soc"] is False and definition["has_target_soc"] is False
    assert definition["has_timed_pause"] is True
    print("PASS: presence needs rw entity")
    return 0


def test_inverter_definition_rate_step_from_charge_rate():
    """rate_step_percent_of_capacity comes from charge_rate's step_percent_of_capacity; absent, base's value stands, else 0 (#5324)."""
    record = _full_record()
    definition, gaps, _ = inverter_definition(record, 2)
    assert definition["rate_step_percent_of_capacity"] == 0 and "rate_step_percent_of_capacity" not in gaps, definition
    definition, _, _ = inverter_definition(record, 2, base={"rate_step_percent_of_capacity": 1})
    assert definition["rate_step_percent_of_capacity"] == 1, definition
    record["entities"]["charge_rate"]["step_percent_of_capacity"] = 1
    definition, _, _ = inverter_definition(record, 2)
    assert definition["rate_step_percent_of_capacity"] == 1, definition
    del record["entities"]["charge_rate"]
    definition, _, _ = inverter_definition(record, 2)
    assert definition["rate_step_percent_of_capacity"] == 0, definition
    print("PASS: rate step from charge_rate")
    return 0


def test_inverter_definition_ge_mode_flags_follow_inverter_mode_domain():
    """inverter_mode as a select means a GE inverter-mode select; as a switch, GE Cloud's eco toggle."""
    record = _full_record()
    record["entities"]["inverter_mode"] = {"entity_id": "select.mode", "access": "rw", "domain": "select"}
    definition, _, _ = inverter_definition(record, 2)
    assert definition["has_ge_inverter_mode"] is True and definition["has_ge_eco_toggle"] is False
    record["entities"]["inverter_mode"] = {"entity_id": "switch.eco", "access": "rw", "domain": "switch"}
    definition, _, _ = inverter_definition(record, 2)
    assert definition["has_ge_inverter_mode"] is False and definition["has_ge_eco_toggle"] is True
    print("PASS: GE mode flags from inverter_mode domain")
    return 0


def test_inverter_definition_charge_rate_units():
    """Amps mean current control with decimal places from the step; no charge_rate means none, unless charge_rate_percent is bound."""
    record = _full_record()
    record["entities"]["charge_rate"] = {"entity_id": "number.amps", "access": "rw", "unit": "A", "step": 0.1}
    definition, gaps, not_applicable = inverter_definition(record, 2)
    assert definition["output_charge_control"] == "current" and definition["current_dp"] == 1, definition
    assert "current_dp" not in not_applicable and gaps == []
    record["entities"]["charge_rate"]["step"] = 1
    assert inverter_definition(record, 2)[0]["current_dp"] == 0
    del record["entities"]["charge_rate"]
    definition, _, _ = inverter_definition(record, 2)
    assert definition["output_charge_control"] == "none"
    record["entities"]["charge_rate_percent"] = {"entity_id": "number.pct", "access": "rw", "unit": "%"}
    definition, gaps, _ = inverter_definition(record, 2)
    assert definition["output_charge_control"] == "power" and "output_charge_control" not in gaps, "a percentage-rate model is still power control"
    del record["entities"]["charge_rate_percent"]
    record["entities"]["charge_rate"] = {"entity_id": "number.rate", "access": "rw"}
    _, gaps, _ = inverter_definition(record, 2)
    assert "output_charge_control" in gaps, gaps
    print("PASS: charge_rate unit handling")
    return 0


def test_inverter_definition_gaps_and_not_applicable():
    """A bound entity missing its format is a gap; an unbound source is not applicable; a missing capability is a gap."""
    record = _full_record()
    del record["entities"]["charge_start_time"]["format"]
    del record["capabilities"]["support_feedin_first"]
    record["entities"]["inverter_time"] = {"entity_id": "sensor.t", "access": "r", "format": "%H:%M:%S"}
    definition, gaps, not_applicable = inverter_definition(record, 2)
    assert "charge_time_format" in gaps and "support_feedin_first" in gaps, gaps
    assert "charge_time_format" not in definition and "support_feedin_first" not in definition
    assert definition["clock_time_format"] == "%H:%M:%S" and "clock_time_format" not in not_applicable
    del record["entities"]["charge_start_time"]
    _, gaps, not_applicable = inverter_definition(record, 2)
    assert "charge_time_format" in not_applicable and "charge_time_format" not in gaps
    print("PASS: gaps vs not applicable")
    return 0


def test_inverter_definition_counts_load_entities():
    """num_load_entities is 1 plus the consecutive load_power_N entities bound."""
    record = _full_record()
    record["entities"]["load_power"] = {"entity_id": "sensor.l0", "access": "r"}
    record["entities"]["load_power_1"] = {"entity_id": "sensor.l1", "access": "r"}
    record["entities"]["load_power_3"] = {"entity_id": "sensor.l3", "access": "r"}
    assert inverter_definition(record, 2)[0]["num_load_entities"] == 2
    print("PASS: load entity count")
    return 0


def test_inverter_definition_base_fills_gaps_and_is_never_mutated():
    """With the live INVERTER_DEF row as base, a gap keeps the base value and the row is untouched."""
    row = INVERTER_DEF["SunsynkCloud"]
    before = dict(row)
    record = _full_record()
    record["capabilities"] = {}
    definition, gaps, _ = inverter_definition(record, 2, base=row)
    assert set(CAPABILITY_KEYS) <= set(gaps)
    assert definition["support_charge_freeze"] == row["support_charge_freeze"]
    definition["support_charge_freeze"] = "changed"
    assert INVERTER_DEF["SunsynkCloud"] == before, "inverter_definition() must never mutate INVERTER_DEF"
    print("PASS: base fills gaps, never mutated")
    return 0


def _published(report, component="test"):
    """The published catalogue for one report, plus every Warn the coordinator logged while building it."""
    base, coordinator = _coordinator()
    logs = []
    coordinator.log = logs.append
    coordinator.report(component, report)
    return coordinator.catalogue(), [line for line in logs if line.startswith("Warn")]


def test_published_catalogue_keeps_account_ids_readable():
    """Identifiers are not hidden: an MPAN, an account number and the entity ids and device_id that embed them stay as reported."""
    catalogue, warnings = _published(
        {
            "meters": [
                {
                    "device_id": "octopus:1234567890123",
                    "direction": "import",
                    "account_ids": {"mpan": "1234567890123", "account": "A-1B2C3D4E"},
                    "entities": {"import_today": {"entity_id": "sensor.octopus_1234567890123_import", "access": "r"}},
                }
            ]
        }
    )
    meter = catalogue["meters"][0]
    assert meter["device_id"] == "octopus:1234567890123", meter
    assert meter["account_ids"] == {"mpan": "1234567890123", "account": "A-1B2C3D4E"}, meter
    assert meter["entities"]["import_today"]["entity_id"] == "sensor.octopus_1234567890123_import", meter
    assert not warnings, warnings
    print("PASS: account ids stay readable in the published catalogue")
    return 0


def test_published_catalogue_keeps_long_digit_runs_readable():
    """A long digit run is not taken for a misfiled identifier: Sigenergy's documented system id shape survives everywhere."""
    catalogue, warnings = _published(
        {
            "inverters": [
                {
                    "device_id": "sigenergy:system:TESTA1700000000",
                    "hardware_ids": {"system_id": "TESTA1700000000", "station": "1234567890123"},
                    "info": {"firmware": "26.18.3 184289b9"},
                    "entities": {"soc_kw": {"entity_id": "sensor.predbat_sigenergy_ta1700000000_battery_soc", "access": "r"}},
                }
            ]
        }
    )
    record = catalogue["inverters"][0]
    assert record["device_id"] == "sigenergy:system:TESTA1700000000", record
    assert record["hardware_ids"] == {"system_id": "TESTA1700000000", "station": "1234567890123"}, record
    assert record["info"] == {"firmware": "26.18.3 184289b9"}, record
    assert record["entities"]["soc_kw"]["entity_id"] == "sensor.predbat_sigenergy_ta1700000000_battery_soc", record
    assert not warnings, warnings
    print("PASS: long digit runs stay readable in the published catalogue")
    return 0


def test_published_catalogue_keeps_location_readable():
    """Location is not hidden either: a latitude/longitude reported as a rating is published as reported."""
    catalogue, warnings = _published({"forecasts": [{"device_id": "solcast:site1", "kind": "solar", "ratings": {"latitude": 51.5, "longitude": -0.12}}]})
    assert catalogue["forecasts"][0]["ratings"] == {"latitude": 51.5, "longitude": -0.12}, catalogue["forecasts"][0]
    assert not warnings, warnings
    print("PASS: location stays readable in the published catalogue")
    return 0


def test_published_catalogue_scrubs_email_addresses():
    """An email address never reaches the published catalogue, wherever a component put it."""
    catalogue, _ = _published({"chargers": [{"device_id": "ohme:jane.smith@example.com", "account_ids": {"login": "jane.smith@example.com"}, "serves_cars": ["ohme:jane.smith@example.com:car"]}]})
    text = str(catalogue)
    assert "jane.smith@example.com" not in text and "@example.com" not in text, text
    assert catalogue["chargers"], "the record itself survives - only the address is removed"
    print("PASS: email addresses are scrubbed from the published catalogue")
    return 0


def test_account_ids_drop_an_overlong_string():
    """account_ids holds strings to MAX_STRING like every other container, so a pasted blob under an innocent key is dropped.

    With identifiers published as reported, the name-based credential check is the only guard on this
    container; the length cap is its value-side backstop. Every real identifier fits - a 13-digit MPAN,
    an account number, a 36-character UUID.
    """
    logs = []
    uuid = "0a1b2c3d-4e5f-6071-8293-a4b5c6d7e8f9"
    cleaned = validate_report({"cars": [{"device_id": "x:1", "account_ids": {"mpan": "1234567890123", "device": uuid, "note": "eyJ" + "a" * 70}}]}, "x", logs.append)
    assert cleaned["cars"][0]["account_ids"] == {"mpan": "1234567890123", "device": uuid}, cleaned
    assert any("account_ids.note" in line for line in logs), logs
    print("PASS: account_ids drops an over-long string and keeps real identifiers")
    return 0

def test_published_catalogue_refuses_credentials():
    """Passwords, tokens and keys never reach the catalogue: a credential-named field is refused at validation."""
    catalogue, _ = _published({"inverters": [{"device_id": "x:1", "account_ids": {"password": "hunter2", "api_key": "sk_live_abc", "access_token": "eyJhbGciOi"}}]})
    text = str(catalogue)
    for secret in ("hunter2", "sk_live_abc", "eyJhbGciOi"):
        assert secret not in text, text
    print("PASS: credentials are refused")
    return 0

def test_coordinator_all(my_predbat=None):
    """Run every coordinator test, returning the number of failures."""
    failures = 0
    failures += test_report_keeps_valid_containers()
    failures += test_published_catalogue_keeps_account_ids_readable()
    failures += test_published_catalogue_keeps_long_digit_runs_readable()
    failures += test_published_catalogue_keeps_location_readable()
    failures += test_published_catalogue_scrubs_email_addresses()
    failures += test_published_catalogue_refuses_credentials()
    failures += test_account_ids_drop_an_overlong_string()
    failures += test_ratings_rejects_non_numeric()
    failures += test_vocabulary_rejects_free_text()
    failures += test_info_rejects_email_shaped_and_long_strings()
    failures += test_credential_named_field_rejected()
    failures += test_entities_credential_named_key_rejected()
    failures += test_entities_free_text_unit_dropped_legitimate_descriptor_intact()
    failures += test_entities_options_preserves_realistic_values()
    failures += test_coverage_accepts_numbers_booleans_and_vocabulary_lists()
    failures += test_validate_report_never_raises_on_non_dict()
    failures += test_validate_report_never_raises_on_a_non_list_section()
    failures += test_automatic_field_absent_when_component_omits_it()
    failures += test_unknown_container_dropped()
    failures += test_record_without_device_id_dropped()
    failures += test_report_is_idempotent_and_versioned()
    failures += test_meter_sub_record_validated()
    failures += test_credential_guard_fires_inside_sub_record_container()
    failures += test_assemble_merges_sections_and_tags_source()
    failures += test_catalogue_reassembles_so_a_post_assembly_report_reaches_it()
    failures += test_assemble_component_status()
    failures += test_inverter_record_omits_empty_containers()
    failures += test_inverter_record_keeps_everything_populated()
    failures += test_inverter_record_round_trips_through_validation()
    failures += test_inverter_record_parameters_match_the_inverters_schema()
    failures += test_inverter_record_fields_are_keyword_only()
    failures += test_inverter_record_copies_the_callers_containers()
    failures += test_inverter_record_normalises_sets_and_tuples()
    failures += test_inverter_record_sorts_a_mixed_type_set_without_raising()
    failures += test_inverter_record_drops_an_empty_string()
    failures += test_charger_record_omits_empty_containers()
    failures += test_charger_and_car_record_parameters_match_their_sections()
    failures += test_charger_with_stub_car_cross_links()
    failures += test_stub_car_survives_validation()
    failures += test_component_status_omits_components_that_cannot_report()
    failures += test_component_status_reported_at_set_only_for_ok()
    failures += test_observations_duplicate_serial()
    failures += test_observations_duplicate_serial_sees_a_gateways_fronted_batteries()
    failures += test_component_load_error_carries_no_free_text()
    failures += test_observations_contested_cars_and_meters()
    failures += test_observations_resulting_config()
    failures += test_serials_and_tariff_codes_stay_clear()
    failures += test_email_inside_entity_id_scrubbed()
    failures += test_catalogue_raw_is_unredacted()
    failures += test_report_discovery_helper()
    failures += test_publish_writes_sensor()
    failures += test_schema_version_is_two()
    failures += test_capabilities_dict_keeps_known_bool_keys()
    failures += test_capabilities_drops_unknown_key_and_non_bool()
    failures += test_capabilities_list_is_dropped()
    failures += test_descriptor_requires_access()
    failures += test_descriptor_needs_exactly_one_of_entity_id_and_value()
    failures += test_descriptor_value_rejects_free_text()
    failures += test_descriptor_invert_must_be_bool()
    failures += test_descriptor_step_percent_of_capacity_must_be_a_percentage()
    failures += test_new_containers_survive_redaction_unchanged()
    failures += test_inverter_definition_builds_every_field_without_a_base()
    failures += test_inverter_definition_presence_needs_a_real_rw_entity()
    failures += test_inverter_definition_ge_mode_flags_follow_inverter_mode_domain()
    failures += test_inverter_definition_rate_step_from_charge_rate()
    failures += test_inverter_definition_charge_rate_units()
    failures += test_inverter_definition_gaps_and_not_applicable()
    failures += test_inverter_definition_counts_load_entities()
    failures += test_inverter_definition_base_fills_gaps_and_is_never_mutated()
    return failures

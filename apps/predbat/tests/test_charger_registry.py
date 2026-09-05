# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the charger registry."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from mock_base import MockBase
from charger_registry import ChargerEntry, ChargerRegistry, preregister_legacy
from userinterface import UserInterface
from utils import is_debug_excluded_key


def _registry(base=None):
    """A registry over the given MockBase, or a fresh one when the test does not need the base."""
    return ChargerRegistry(base if base is not None else MockBase())


class AutoConfigBase(MockBase):
    """A MockBase that runs Predbat's own auto_config() over its args before the registry sees them.

    Borrowed from UserInterface rather than reimplemented: what makes preregister_legacy's input
    awkward is precisely what the real resolve_arg_re() does to an unmatched list element, so a
    hand-written stand-in for it would be testing the fixture rather than the code. The full
    PredBat object cannot be used here - PredBat.__init__ reads an apps.yaml from the working
    directory, which exists under coverage/ but not where pytest runs this file from - so the two
    real methods are bound onto the mock instead.
    """

    auto_config = UserInterface.auto_config
    resolve_arg_re = UserInterface.resolve_arg_re

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Return the whole entity map when asked for it, as PredBat's own wrapper does.

        auto_config() calls this with no entity_id to get the state keys its regexes match
        against; MockBase's version looks up the key None and returns {}, which would make every
        pattern fail to match for the wrong reason.
        """
        if entity_id is None:
            return self.entities
        return super().get_state_wrapper(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh, required_unit=required_unit, raw=raw)


def test_two_sources_compose_into_two_slots():
    """The bug this registry exists to fix: two components, two chargers, two slots."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", energy="sensor.predbat_ohme_energy_today")])
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.predbat_gecloud_ce1234_evc_car_connected", energy="sensor.predbat_gecloud_ce1234_evc_energy")])

    assert base.args["num_cars"] == 2
    # Deterministic order: sorted by (source, device_id), so gecloud precedes ohme.
    assert base.args["car_charging_planned"] == ["binary_sensor.predbat_gecloud_ce1234_evc_car_connected", "binary_sensor.predbat_ohme_connected"]
    # Site aggregates: concatenated, not slot-aligned.
    assert base.args["car_charging_energy"] == ["sensor.predbat_gecloud_ce1234_evc_energy", "sensor.predbat_ohme_energy_today"]


def test_slot_for_returns_the_allocated_slot():
    """Components must be able to find their own car index - this is what keeps control safe."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.a")])
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.b")])

    assert registry.slot_for("gecloud", "CE1234") == 0
    assert registry.slot_for("ohme", "ohme0") == 1
    assert registry.slot_for("ohme", "nosuch") is None


def test_never_writes_none_into_a_list():
    """apps.yaml validation rejects non-string list elements (predbat.py:1644)."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.b")])
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.a", soc="sensor.predbat_ohme_battery_percent")])

    for key in ("car_charging_planned", "car_charging_now", "car_charging_soc", "car_charging_energy", "car_charging_power"):
        assert None not in base.args.get(key, [])
    # Only one of two slots can report SoC, so the key is left exactly as it stands rather than
    # padded - here that means untouched, since nothing had ever written it. See
    # test_hand_written_soc_and_now_survive_a_discovered_charger for the case where it holds
    # the user's own config; car_charging_manual_soc covers the slots with no sensor.
    assert "car_charging_soc" not in base.args


def test_slot_aligned_list_written_when_every_slot_has_one():
    """The list is written whole when every slot has a value - the normal case."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("gateway", [
        ChargerEntry("gateway", "CP-AAAAAA", planned="binary_sensor.a", soc="sensor.a_soc"),
        ChargerEntry("gateway", "CP-BBBBBB", planned="binary_sensor.b", soc="sensor.b_soc"),
    ])
    assert base.args["car_charging_soc"] == ["sensor.a_soc", "sensor.b_soc"]


def test_replace_source_is_idempotent_and_dedupes():
    """Re-running discovery must not duplicate chargers, even if the source repeats one."""
    base = MockBase()
    registry = _registry(base)
    entry = ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")
    registry.replace_source("ohme", [entry, entry])
    registry.replace_source("ohme", [entry])

    assert base.args["num_cars"] == 1
    assert base.args["car_charging_planned"] == ["binary_sensor.predbat_ohme_connected"]


def test_empty_replace_clears_the_source_and_rewrites_args():
    """A vanished charger must lower num_cars AND clear its stale entities."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("myenergi", [
        ChargerEntry("myenergi", "Z1", planned="sensor.z1_plug", energy="sensor.z1_energy"),
        ChargerEntry("myenergi", "Z2", planned="sensor.z2_plug", energy="sensor.z2_energy"),
    ])
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme")])

    registry.replace_source("myenergi", [])

    assert base.args["num_cars"] == 1
    assert base.args["car_charging_planned"] == ["binary_sensor.ohme"]
    assert "sensor.z1_energy" not in base.args.get("car_charging_energy", [])


def test_clamped_to_max_cars():
    """PREDBAT_MAX_CARS is a kernel array bound, so the excess is dropped, not written."""
    from const import PREDBAT_MAX_CARS

    base = MockBase()
    registry = _registry(base)
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "Z{:02d}".format(n), planned="sensor.z{}".format(n)) for n in range(PREDBAT_MAX_CARS + 3)])

    assert base.args["num_cars"] == PREDBAT_MAX_CARS
    assert len(base.args["car_charging_planned"]) == PREDBAT_MAX_CARS


def test_max_rate_is_exposed_per_slot():
    """car_charging_rate is a UI config item, indexed _1.._7 after slot 0 (config.py)."""
    base = MockBase()
    exposed = {}
    base.expose_config = lambda name, value: exposed.__setitem__(name, value)
    registry = _registry(base)
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.a", max_rate_kw=7.4)])
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.b", max_rate_kw=22.0)])

    assert exposed["car_charging_rate"] == 7.4
    assert exposed["car_charging_rate_1"] == 22.0


def test_clearing_the_last_source_resets_args():
    """When the registry becomes empty after being populated, stale args must be cleared."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "Z1", planned="sensor.z1_plug")])

    assert base.args["num_cars"] == 1
    assert base.args["car_charging_planned"] == ["sensor.z1_plug"]

    registry.replace_source("myenergi", [])

    assert base.args["num_cars"] == 0
    assert "car_charging_planned" not in base.args or base.args["car_charging_planned"] is None


def test_never_populated_registry_is_a_noop():
    """A registry that has never held anything must not write num_cars or car_charging_* args."""
    base = MockBase()
    registry = _registry(base)

    assert "num_cars" not in base.args
    assert "car_charging_planned" not in base.args


def test_legacy_only_config_is_a_noop():
    """Existing apps.yaml users must see identical args. This is the compat guarantee."""
    import copy

    base = MockBase(
        num_cars=2,
        car_charging_planned=["binary_sensor.my_car_a", "binary_sensor.my_car_b"],
        car_charging_soc=["sensor.my_car_a_soc", "sensor.my_car_b_soc"],
        car_charging_energy=["sensor.my_charger_energy"],
    )
    before = copy.deepcopy(base.args)
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args == before


def test_legacy_scalar_is_broadcast_not_padded():
    """A scalar feeds every index today (resolve_arg), so it must keep doing so.

    The arg itself is left exactly as the user wrote it - a key holding only legacy slots is
    never rewritten - so the broadcast shows up where it matters: in the composed list once a
    charger is discovered, where car 1 is still the user's scalar and not an empty slot.
    """
    base = MockBase(num_cars=2, car_charging_planned="binary_sensor.my_car_a")
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args["num_cars"] == 2
    assert base.args["car_charging_planned"] == "binary_sensor.my_car_a", "the user's own scalar is not rewritten"

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_a", "binary_sensor.my_car_a", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 2


def test_legacy_slot_count_comes_from_num_cars_only():
    """A planned list longer than num_cars is ignored today; do not activate the extras.

    Ignored, not deleted: fetch.py simply reads no index past num_cars, so the surplus entry
    stays in the user's config untouched - and the slot it never got goes to the discovered
    charger, which is what proves it was not activated.
    """
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.a", "binary_sensor.b"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args["num_cars"] == 1
    assert base.args["car_charging_planned"] == ["binary_sensor.a", "binary_sensor.b"], "the surplus entry is ignored, not rewritten away"

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["car_charging_planned"] == ["binary_sensor.a", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 1


def test_legacy_aggregates_survive_a_component_registering():
    """A hand-configured energy sensor must not be replaced by a discovered one."""
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.my_car_a"], car_charging_energy=["sensor.my_charger_energy"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.ce1234", energy="sensor.ce1234_energy")])

    assert base.args["car_charging_energy"] == ["sensor.my_charger_energy", "sensor.ce1234_energy"]


def test_autodiscovery_appends_after_legacy_slots():
    """A discovered charger must not displace a hand-configured one from slot 0."""
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.my_car_a"])
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_a", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 1


def test_legacy_preregistration_with_no_config_does_nothing():
    """No car config at all means the registry stays empty and writes nothing."""
    base = MockBase()
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert "num_cars" not in base.args or base.args["num_cars"] == 0


def test_legacy_scalar_config_without_num_cars_defaults_to_one_slot():
    """car_charging_planned without num_cars key defaults to 1 slot, matching fetch.py:2834."""
    base = MockBase(car_charging_planned="binary_sensor.my_car")
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args["num_cars"] == 1
    # One slot, and the arg is left as the user wrote it - see the append test below for the
    # slot itself, which is what the count is actually for.
    assert base.args["car_charging_planned"] == "binary_sensor.my_car"


def test_legacy_energy_without_cars_survives_component_registration():
    """Hand-configured car_charging_energy without cars is protected and prepended to discovered chargers."""
    base = MockBase(car_charging_energy="sensor.my_charger_energy")
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", energy="sensor.predbat_ohme_energy_today")])

    assert base.args["car_charging_energy"] == ["sensor.my_charger_energy", "sensor.predbat_ohme_energy_today"]


def test_legacy_scalar_appends_discovered_chargers():
    """Discovered chargers append after implicit single legacy slot when num_cars is absent."""
    base = MockBase(car_charging_planned="binary_sensor.my_car_a")
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_a", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 1


def test_mock_base_exposes_a_registry():
    """Every component reaches the registry through the base, so MockBase must carry one."""
    base = MockBase()
    assert isinstance(base.charger_registry, ChargerRegistry)


def test_component_base_helpers_use_the_registry():
    """Components register and look up their slot through the base; never touch args."""
    from component_base import ComponentBase

    class TestComponent(ComponentBase):
        """Test implementation of ComponentBase."""

        def initialize(self, **kwargs):
            """No-op initialize for testing."""
            pass

    base = MockBase()
    component = TestComponent(base)

    component.register_chargers("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["num_cars"] == 1
    assert component.charger_slot("ohme", "ohme0") == 0
    assert component.charger_slot("ohme", "nosuch") is None


def test_ohme_control_follows_its_own_slot():
    """Ohme at slot 1 must read car 1's plan entity, not the unindexed car-0 one.

    Reading the wrong entity starts or stops a physical charger from another car's plan.
    """
    from charger_registry import slot_entity_suffix

    assert slot_entity_suffix(0) == ""
    assert slot_entity_suffix(1) == "_1"
    assert slot_entity_suffix(7) == "_7"


def test_myenergi_zappis_get_distinct_slots_after_another_source():
    """Zappi N is no longer car N once another component holds an earlier slot."""
    base = MockBase()
    registry = base.charger_registry
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.ce1234")])
    registry.replace_source("myenergi", [
        ChargerEntry("myenergi", "10000001", planned="sensor.z1_plug"),
        ChargerEntry("myenergi", "10000002", planned="sensor.z2_plug"),
    ])

    assert registry.slot_for("myenergi", "10000001") == 1
    assert registry.slot_for("myenergi", "10000002") == 2
    assert base.args["num_cars"] == 3


def test_gecloud_and_ohme_do_not_collapse_into_one_slot():
    """The exact bug: gecloud's raise-only num_cars took a max, so both claimed slot 0."""
    base = MockBase()
    registry = base.charger_registry
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.predbat_gecloud_ce1234_evc_car_connected")])

    assert base.args["num_cars"] == 2
    assert len(set(base.args["car_charging_planned"])) == 2
    assert registry.slot_for("gecloud", "CE1234") != registry.slot_for("ohme", "ohme0")


def test_gateway_registers_every_charge_point_with_its_own_slot():
    """The gateway used to register only the first charger and force num_cars=1."""
    base = MockBase()
    registry = base.charger_registry
    registry.replace_source(
        "gateway",
        [
            ChargerEntry("gateway", "CP-AAAAAA", planned="binary_sensor.predbat_gateway_ev_aaaaaa_connected", soc="sensor.predbat_gateway_ev_aaaaaa_soc", max_rate_kw=7.4),
            ChargerEntry("gateway", "CP-BBBBBB", planned="binary_sensor.predbat_gateway_ev_bbbbbb_connected", soc="sensor.predbat_gateway_ev_bbbbbb_soc", max_rate_kw=22.0),
        ],
    )

    assert base.args["num_cars"] == 2
    assert registry.slot_for("gateway", "CP-AAAAAA") == 0
    assert registry.slot_for("gateway", "CP-BBBBBB") == 1
    assert base.args["car_charging_soc"] == ["sensor.predbat_gateway_ev_aaaaaa_soc", "sensor.predbat_gateway_ev_bbbbbb_soc"]


def test_all_four_sources_compose_with_distinct_slots():
    """Four components, four chargers, four distinct slots, no entity lost.

    Before the registry, whichever component ran last owned car_charging_planned outright
    and num_cars was the largest single component's count - so this site planned for one
    car and ignored three chargers.
    """
    base = MockBase()
    registry = base.charger_registry
    registry.replace_source("gateway", [ChargerEntry("gateway", "CP-AAAAAA", planned="binary_sensor.gw_a", energy="sensor.gw_a_energy")])
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.ce1234", energy="sensor.ce1234_energy")])
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme", energy="sensor.ohme_energy")])
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned="sensor.z1_plug", energy="sensor.z1_energy")])

    assert base.args["num_cars"] == 4
    assert len(set(base.args["car_charging_planned"])) == 4
    assert len(base.args["car_charging_energy"]) == 4
    assert len({registry.slot_for("gateway", "CP-AAAAAA"), registry.slot_for("gecloud", "CE1234"), registry.slot_for("ohme", "ohme0"), registry.slot_for("myenergi", "10000001")}) == 4


def test_iog_arrays_are_untouched_by_the_registry():
    """IOG is not part of the collision and must stay that way.

    Octopus writes its own octopus_* arrays and consumes car_charging_planned rather than
    supplying it, so the registry must never write an octopus_* key.
    """
    base = MockBase(octopus_intelligent_slot=["binary_sensor.octopus_slot"], octopus_ready_time="07:00:00", octopus_charge_limit=80)
    base.charger_registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["octopus_intelligent_slot"] == ["binary_sensor.octopus_slot"]
    assert base.args["octopus_ready_time"] == "07:00:00"
    assert base.args["octopus_charge_limit"] == 80


def test_legacy_car_charging_rate_is_left_alone():
    """An existing car_charging_rate is an already-effective cap, never re-derived."""
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.my_car_a"])
    exposed = {}
    base.expose_config = lambda name, value: exposed.__setitem__(name, value)
    preregister_legacy(base.charger_registry, base)

    assert exposed == {}


def test_adding_an_earlier_sorting_charger_shifts_later_slots():
    """Documented, deliberate consequence of deterministic ordering.

    A charger discovered later whose (source, device_id) sorts earlier takes a lower slot
    and shifts the rest. Per-car settings (limit, battery size, manual SoC, exclusive) are
    addressed by slot, so they follow the position, not the car. Components are safe because
    they re-read slot_for() every cycle; a user's per-car *settings* are not. This is why
    slot_for() is authoritative and nothing may cache a slot across cycles.
    """
    base = MockBase()
    registry = base.charger_registry
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme")])
    assert registry.slot_for("ohme", "ohme0") == 0

    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.ce1234")])

    assert registry.slot_for("ohme", "ohme0") == 1
    assert registry.slot_for("gecloud", "CE1234") == 0


def test_charger_registry_excluded_from_debug_dumps():
    """Charger registry holds a back-reference to base, forming a cycle.

    Walking this cycle during YAML serialization (userinterface.py:768 create_debug_yaml)
    encounters coroutine objects that cannot be pickled, causing a 500 error on /debug_yaml.
    The registry must be in DEBUG_EXCLUDE_LIST to prevent this.
    """
    assert is_debug_excluded_key("charger_registry") is True


def test_stock_apps_yaml_template_does_not_invent_a_car():
    """The shipped apps.yaml declares num_cars: 1 with only unmatched template regexes.

    Reading that as a hand-configured car gave virtually every installation a phantom slot 0:
    an auto-discovered charger landed at slot 1, and because the phantom slot supplies no SoC
    the omit-on-gap rule dropped car_charging_soc altogether - so Predbat planned a full
    charge into a car whose level it could not see.
    """
    base = MockBase(
        num_cars=1,
        car_charging_planned=["re:(sensor.wallbox_portal_status_description|sensor.myenergi_zappi_[0-9a-z]+_plug_status)"],
        car_charging_energy="re:(sensor.myenergi_zappi_[0-9a-z]+_charge_added_session|sensor.wallbox_portal_added_energy)",
    )
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", soc="sensor.predbat_ohme_battery_percent", energy="sensor.predbat_ohme_energy_today")])

    assert base.args["num_cars"] == 1
    assert registry.slot_for("ohme", "ohme0") == 0
    assert base.args["car_charging_planned"] == ["binary_sensor.predbat_ohme_connected"]
    assert base.args["car_charging_soc"] == ["sensor.predbat_ohme_battery_percent"]
    # The unmatched template regex is not a sensor either, so it is not prepended to the aggregate.
    assert base.args["car_charging_energy"] == ["sensor.predbat_ohme_energy_today"]


def test_hand_configured_car_keeps_its_slot_alongside_a_discovered_charger():
    """A real entity id is real config: it keeps slot 0 and the discovered charger appends."""
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.my_car_a"], car_charging_soc=["sensor.my_car_a_soc"])
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", soc="sensor.predbat_ohme_battery_percent")])

    assert base.args["num_cars"] == 2
    assert registry.slot_for("ohme", "ohme0") == 1
    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_a", "binary_sensor.predbat_ohme_connected"]
    assert base.args["car_charging_soc"] == ["sensor.my_car_a_soc", "sensor.predbat_ohme_battery_percent"]


def test_declared_num_cars_survives_as_a_floor_when_its_slots_are_empty():
    """num_cars the user declared is honoured even when nothing was configured behind it.

    Dropping the empty slots must not quietly reduce the number of cars Predbat plans for -
    the count is kept as a floor, the phantom entities are what go away.
    """
    base = MockBase(num_cars=2, car_charging_planned=["re:(sensor.nothing_matched)"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    # Nothing registered at all: apps.yaml is left untouched, so fetch.py still reads 2.
    assert base.args["num_cars"] == 2

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["num_cars"] == 2
    assert registry.slot_for("ohme", "ohme0") == 0


def test_registry_does_not_lower_num_cars_claimed_by_octopus():
    """octopus.py and kraken.py own IOG cars the registry has no charger for.

    Those cars are wired through octopus_intelligent_slot, which fetch.py only reads for car
    indices below num_cars - so overwriting num_cars with the registered charger count dropped
    them, deterministically, for every site whose chargers register after the raise.
    """
    base = MockBase()
    registry = _registry(base)
    # Exactly what OctopusAPI.automatic_config() now does for two intelligent devices.
    registry.set_external_num_cars("octopus", 2)

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["num_cars"] == 2
    assert registry.slot_for("ohme", "ohme0") == 0

    # And the claim has to survive: components re-register on every rediscovery, so honouring
    # it for one cycle and dropping the IOG car on the next fixes nothing.
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["num_cars"] == 2


def test_external_claim_is_held_when_chargers_go_away():
    """The whole point of the claim: four IOG cars survive the chargers dropping to one.

    The old inference read "num_cars differs from what we last wrote" as somebody else's raise,
    which was blind whenever the external raise landed on the same number we had just written -
    exactly the four-and-four case here.
    """
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "Z{}".format(n), planned="sensor.z{}_plug".format(n)) for n in range(4)])
    assert base.args["num_cars"] == 4

    registry.set_external_num_cars("octopus", 4)
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "Z0", planned="sensor.z0_plug")])

    assert base.args["num_cars"] == 4


def test_external_claim_can_be_lowered_and_dropped():
    """A raise-only writer can never take its own floor back; an explicit claim can."""
    base = MockBase()
    registry = _registry(base)
    registry.set_external_num_cars("octopus", 4)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])
    assert base.args["num_cars"] == 4

    # One of the intelligent devices goes away: the claim drops to 2, and so does num_cars,
    # because the registry's own charger only accounts for one car.
    registry.set_external_num_cars("octopus", 2)
    assert base.args["num_cars"] == 2

    # All of them go: num_cars follows the registered chargers again.
    registry.set_external_num_cars("octopus", 0)
    assert base.args["num_cars"] == 1


def test_external_only_claim_writes_and_releases_num_cars_with_no_chargers():
    """Kraken/IOG on a site with no registered charger at all: the claim IS num_cars.

    The registry only wrote num_cars once it had held a charger, so on a site whose only cars are
    provider-enrolled ones the claim went in through the component's own raise and the registry
    never touched the key - which meant the release had nowhere to land and the phantom car stayed
    for the rest of the run. The claim has to be able to write num_cars with zero entries behind
    it, and to lower it again.
    """
    base = MockBase()
    registry = _registry(base)

    registry.set_external_num_cars("kraken", 3)

    assert base.args["num_cars"] == 3

    registry.set_external_num_cars("kraken", 0)

    assert base.args["num_cars"] == 0
    # Releasing a claim is not a charger going away: nothing slot-aligned was ever composed for
    # those cars, so those keys are not the registry's to clear.
    for field in ("car_charging_planned", "car_charging_now", "car_charging_soc"):
        assert field not in base.args


def test_external_only_claim_release_respects_the_declared_floor():
    """A release drops the claim, not the user's own apps.yaml num_cars."""
    base = MockBase(num_cars=2, car_charging_planned=["binary_sensor.my_car_a", "binary_sensor.my_car_b"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.set_external_num_cars("kraken", 4)
    assert base.args["num_cars"] == 4

    registry.set_external_num_cars("kraken", 0)

    # Back to what the user declared, not to zero.
    assert base.args["num_cars"] == 2
    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_a", "binary_sensor.my_car_b"]


def test_a_claim_does_not_lower_a_num_cars_nobody_declared():
    """With nothing registered, a num_cars the registry did not write is not its to lower.

    The registry has composed no chargers here, so it has no count of its own to be authoritative
    with - the number in the args was put there by something else, and a claim for fewer cars than
    that is not evidence the rest have gone.
    """
    base = MockBase()
    registry = _registry(base)
    base.set_arg("num_cars", 4)

    registry.set_external_num_cars("octopus", 3)

    assert base.args["num_cars"] == 4


def test_a_claim_can_lower_the_raise_its_own_component_made():
    """The claimants raise num_cars themselves the instant before declaring, so that IS the claim.

    octopus.py:1352 and kraken.py:1287 both set num_cars directly and then declare the same count.
    On a site with no registered charger that raise is the only thing behind num_cars, so if the
    registry treated it as somebody else's the claim could never be released.
    """
    base = MockBase()
    registry = _registry(base)

    # Exactly the order kraken uses: raise, then declare.
    base.set_arg("num_cars", 2)
    registry.set_external_num_cars("kraken", 2)
    assert base.args["num_cars"] == 2

    registry.set_external_num_cars("kraken", 0)

    assert base.args["num_cars"] == 0


def test_a_released_claim_leaves_no_ownership_residue():
    """A count the registry once wrote must not vouch for the same number set later by someone else.

    Ownership has to be bounded in time. Tracking every count ever claimed meant a claim of 3,
    long since released, still marked a num_cars of 3 as the registry's - so when a different
    owner set 3 much later, the next claim overwrote it with its own smaller count. Only the
    single most recent write and the claims standing right now may confer ownership.
    """
    base = MockBase()
    registry = _registry(base)

    registry.set_external_num_cars("kraken", 3)
    assert base.args["num_cars"] == 3
    registry.set_external_num_cars("kraken", 0)
    assert base.args["num_cars"] == 0

    # An unrelated owner now puts 3 there - the same number the released claim used to hold.
    base.set_arg("num_cars", 3)

    registry.set_external_num_cars("octopus", 1)

    assert base.args["num_cars"] == 3, "the historical claim of 3 must not make this value ours"

    registry.set_external_num_cars("octopus", 0)

    assert base.args["num_cars"] == 3, "and releasing that claim must not lower it either"


def test_a_claim_that_was_never_held_writes_nothing():
    """Dropping a claim that never existed must not create num_cars out of nothing.

    Both octopus and kraken call set_external_num_cars(source, 0) on their release path whether or
    not they ever claimed anything, so this runs on every site that has either component and no
    enrolled car. It must stay as much of a no-op as an untouched registry.
    """
    base = MockBase()
    registry = _registry(base)

    registry.set_external_num_cars("kraken", 0)

    assert "num_cars" not in base.args
    assert base.args == {}


def test_external_claims_from_two_sources_do_not_erase_each_other():
    """Octopus and Kraken can both be configured; the floor is the larger claim, not the last."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    registry.set_external_num_cars("octopus", 3)
    registry.set_external_num_cars("kraken", 2)

    assert base.args["num_cars"] == 3

    registry.set_external_num_cars("octopus", 0)

    assert base.args["num_cars"] == 2


def test_registry_still_lowers_the_num_cars_it_wrote_itself():
    """The floor is other people's raises only - our own previous count is still ours to lower."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("myenergi", [
        ChargerEntry("myenergi", "Z1", planned="sensor.z1_plug"),
        ChargerEntry("myenergi", "Z2", planned="sensor.z2_plug"),
    ])
    assert base.args["num_cars"] == 2

    registry.replace_source("myenergi", [ChargerEntry("myenergi", "Z1", planned="sensor.z1_plug")])

    assert base.args["num_cars"] == 1


def test_legacy_interior_hole_keeps_its_position_and_raw_value():
    """An unconfigured slot in the middle of a legacy list must not compact away or vanish.

    'nothing matched for car 0, here is car 1' is a real config. Dropping slot 0 moved the
    second car down into it, so it inherited car 0's battery size, charge limit, exclusive
    flag and manual SoC, and handed slot 1 to the next charger discovered - the two cars'
    settings simply swapped. Only trailing empty slots are droppable.

    The hole reaching the registry is a None, not the raw 're:' string: auto_config() runs
    first and turns a list element whose regex did not match into one (userinterface.py:1098).
    Reading that as a gap and applying the all-or-omit rule deleted the whole key, losing car
    1's perfectly valid sensor; pre-branch Predbat kept [None, valid] and car 1 worked.

    This is the unit-level version, seeded with the post-auto_config state directly.
    test_legacy_interior_hole_through_the_real_auto_config covers the same case with Predbat's
    own auto_config() producing that None, so this fixture cannot drift away from what the real
    sequence hands the registry.
    """
    # Post-auto_config state: slot 0's pattern did not match and has been nulled in place.
    base = MockBase(num_cars=2, car_charging_planned=[None, "binary_sensor.second_car"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args["car_charging_planned"] == [None, "binary_sensor.second_car"]

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    # Slot 0 keeps its hole verbatim, the hand-configured car keeps slot 1, and the discovered
    # charger appends at slot 2.
    assert base.args["car_charging_planned"] == [None, "binary_sensor.second_car", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 2
    assert base.args["num_cars"] == 3


def test_legacy_interior_hole_through_the_real_auto_config():
    """The interior-hole case again, with Predbat's own auto_config() producing the hole.

    The unit-level twin above seeds [None, 'binary_sensor.second_car'] by hand, which only
    protects the registry if that really is the state auto_config() leaves behind. Here the args
    start as the user wrote them - an unmatched 're:' pattern in slot 0 - and the None is made by
    resolve_arg_re() itself, so the whole sequence apps.yaml -> auto_config -> preregister_legacy
    -> component registration is exercised end to end.
    """
    base = AutoConfigBase(num_cars=2, car_charging_planned=["re:(binary_sensor\\.zappi_.*_plug_status)", "binary_sensor.second_car"])
    # A populated state map, so the pattern fails for the reason it fails in the field - no Zappi
    # on this site - rather than because there is nothing at all to match against.
    base.set_state_wrapper("binary_sensor.second_car", "off")
    base.set_state_wrapper("sensor.house_load", "1200")

    base.auto_config()

    assert base.args["car_charging_planned"] == [None, "binary_sensor.second_car"], "auto_config nulls the unmatched element in place"

    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args["car_charging_planned"] == [None, "binary_sensor.second_car"], "a legacy-only key is not rewritten"

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    # Identical to the hand-seeded test's expectations: the hole keeps slot 0 verbatim, the
    # hand-configured car keeps slot 1, and the discovered charger appends at slot 2.
    assert base.args["car_charging_planned"] == [None, "binary_sensor.second_car", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 2
    assert base.args["num_cars"] == 3


def test_legacy_only_slot_aligned_args_are_never_rewritten():
    """A key holding nothing but legacy slots is left byte-identical, not round-tripped.

    The args already hold exactly what the user's apps.yaml (plus auto_config) produced, so
    there is nothing for the registry to add - and writing anyway would put the all-or-omit
    rule in charge of config the registry did not compose.
    """
    import copy

    base = MockBase(num_cars=2, car_charging_planned=[None, "binary_sensor.second_car"], car_charging_soc=["sensor.a_soc", "sensor.b_soc"])
    before = copy.deepcopy(base.args)
    registry = _registry(base)
    preregister_legacy(registry, base)

    assert base.args["car_charging_planned"] == before["car_charging_planned"]
    assert base.args["car_charging_soc"] == before["car_charging_soc"]
    assert "car_charging_now" not in base.args


def test_trailing_unconfigured_legacy_slots_are_still_dropped():
    """The stock-template rule is unchanged: an empty tail is a count, not a charger."""
    base = MockBase(num_cars=3, car_charging_planned=["binary_sensor.my_car_a", "re:(unmatched)"])
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_a", "binary_sensor.predbat_ohme_connected"]
    assert registry.slot_for("ohme", "ohme0") == 1
    # The declared count is still honoured as a floor, so nothing planned for stops being planned for.
    assert base.args["num_cars"] == 3


def test_legacy_slot_duplicating_a_discovered_charger_is_dropped():
    """One Zappi must be one car even when the stock regex resolves onto it.

    The shipped car_charging_planned pattern matches the third-party ha-myenergi integration's
    entity names. On a site running that integration and Predbat's own myenergi component, the
    same physical charger arrived twice - once as a legacy slot, once as a registered charger -
    so its session energy was subtracted from house load for two cars.

    The two entity ids are never identical in the field: ha-myenergi publishes
    sensor.myenergi_zappi_<serial>_plug_status and our own component registers it under
    sensor.predbat_myenergi_zappi_<serial>_plug_status. The serial is what identifies the
    hardware, so that is what the match is made on.
    """
    ha_myenergi_zappi = "sensor.myenergi_zappi_10000001_plug_status"
    predbat_zappi = "sensor.predbat_myenergi_zappi_10000001_plug_status"
    base = MockBase(num_cars=1, car_charging_planned=[ha_myenergi_zappi])
    registry = _registry(base)
    preregister_legacy(registry, base)
    assert base.args["car_charging_planned"] == [ha_myenergi_zappi]

    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned=predbat_zappi, energy="sensor.predbat_myenergi_zappi_10000001_session_energy")])

    # One car, and it is the component's - the legacy slot has no identity for control to use.
    assert base.args["num_cars"] == 1
    assert base.args["car_charging_planned"] == [predbat_zappi]
    assert registry.slot_for("myenergi", "10000001") == 0
    assert registry.slot_for("legacy", 0) is None
    assert base.args["car_charging_energy"] == ["sensor.predbat_myenergi_zappi_10000001_session_energy"]


def test_a_different_legacy_entity_is_not_treated_as_a_duplicate():
    """The identity has to actually appear in the legacy entity; a second real car keeps its slot."""
    base = MockBase(num_cars=1, car_charging_planned=["sensor.some_other_car"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000002", planned="sensor.predbat_myenergi_zappi_10000002_plug_status")])

    assert base.args["num_cars"] == 2
    assert registry.slot_for("myenergi", "10000002") == 1


def test_a_serial_that_is_a_prefix_of_another_serial_is_not_a_duplicate():
    """Serial 10000001 must not claim Zappi 100000010's legacy slot - that is a different charger.

    myenergi serials are consecutive, so a site with two Zappis can easily hold one whose serial
    is a prefix of the other's. An unbounded substring test read the shorter one as naming the
    longer one's entity and silently deleted a real car; the match is on a word boundary, and
    entity ids separate their parts with "_", so the digit either side of it is what tells them
    apart.
    """
    other_zappi = "sensor.myenergi_zappi_100000010_plug_status"
    base = MockBase(num_cars=1, car_charging_planned=[other_zappi])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned="sensor.predbat_myenergi_zappi_10000001_plug_status")])

    assert base.args["num_cars"] == 2
    assert base.args["car_charging_planned"] == [other_zappi, "sensor.predbat_myenergi_zappi_10000001_plug_status"]
    assert registry.slot_for("myenergi", "10000001") == 1


def test_a_gateway_charge_point_id_is_not_matched_inside_a_legacy_entity():
    """An OCPP charge point id is a name its installer typed, not a high-entropy serial.

    myenergi and GivEnergy Cloud device ids are manufacturer serials, so finding one inside an
    entity id is real evidence of the same hardware. A gateway charge point id can be anything -
    'garage', 'CP1', the customer's surname - so the same test would delete any car whose entity
    happened to contain that word. Gateway chargers are still deduped against a legacy slot
    naming the exact same entity; only the looser match is withheld.
    """
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.garage_car_connected"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("gateway", [ChargerEntry("gateway", "garage", planned="binary_sensor.predbat_gateway_ev_garage_connected")])

    assert base.args["num_cars"] == 2
    assert base.args["car_charging_planned"] == ["binary_sensor.garage_car_connected", "binary_sensor.predbat_gateway_ev_garage_connected"]
    assert registry.slot_for("gateway", "garage") == 1


def test_a_gateway_charger_still_dedupes_a_legacy_slot_naming_its_own_entity():
    """Withholding the serial match must not cost the exact-entity match, which is unambiguous."""
    gateway_entity = "binary_sensor.predbat_gateway_ev_garage_connected"
    base = MockBase(num_cars=1, car_charging_planned=[gateway_entity])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("gateway", [ChargerEntry("gateway", "garage", planned=gateway_entity)])

    assert base.args["num_cars"] == 1
    assert base.args["car_charging_planned"] == [gateway_entity]
    assert registry.slot_for("gateway", "garage") == 0


def test_a_short_device_id_is_not_matched_inside_a_legacy_entity():
    """A two- or three-character id would collide by chance, so it is not identity evidence.

    'sensor.my_car_a' contains '_a'; a device_id that short naming the same hardware is not
    something the substring match can tell apart from a coincidence, so the legacy slot stays.
    """
    base = MockBase(num_cars=1, car_charging_planned=["sensor.my_car_a"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    registry.replace_source("gecloud", [ChargerEntry("gecloud", "_a", planned="binary_sensor.predbat_gecloud_evc_car_connected")])

    assert base.args["num_cars"] == 2
    assert base.args["car_charging_planned"] == ["sensor.my_car_a", "binary_sensor.predbat_gecloud_evc_car_connected"]


def test_aggregate_set_by_another_component_is_not_clobbered():
    """A key no registered charger supplies is not the registry's to write.

    alphaess sets car_charging_energy from its own discovery at runtime, and ohme deliberately
    backs off when somebody else owns it - replaying the legacy snapshot on every materialise
    undid both.
    """
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.my_car_a"])
    registry = _registry(base)
    preregister_legacy(registry, base)

    # A third party writes the aggregate after the snapshot was taken.
    base.set_arg("car_charging_energy", "sensor.alphaess_car_charging_energy")

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", energy=None)])

    assert base.args["car_charging_energy"] == "sensor.alphaess_car_charging_energy"


def test_the_registry_still_clears_an_aggregate_it_wrote_itself():
    """Ownership is what gates the write, so removal still restores the legacy value."""
    base = MockBase(car_charging_energy="sensor.my_charger_energy")
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme", energy="sensor.predbat_ohme_energy_today")])
    assert base.args["car_charging_energy"] == ["sensor.my_charger_energy", "sensor.predbat_ohme_energy_today"]

    registry.replace_source("ohme", [])

    assert base.args["car_charging_energy"] == ["sensor.my_charger_energy"]


def test_aggregate_overwritten_after_we_wrote_it_is_left_alone():
    """Having written a key once is not ownership of it forever.

    alphaess.py:805 sets car_charging_energy from its own EV-charger discovery at any point.
    Tracking only that the registry had *ever* written the key meant the last charger leaving
    restored over the top of alphaess's value, silently disconnecting its charger.
    """
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme", energy="sensor.predbat_ohme_energy_today")])
    assert base.args["car_charging_energy"] == ["sensor.predbat_ohme_energy_today"]

    # alphaess wires its own charger afterwards.
    base.set_arg("car_charging_energy", ["sensor.alphaess_ev_energy_today"])

    registry.replace_source("ohme", [])

    assert base.args["car_charging_energy"] == ["sensor.alphaess_ev_energy_today"]


def test_slot_map_is_logged_only_when_it_changes():
    """Support logs need the map, but this runs on every rediscovery - so log the changes."""
    base = MockBase()
    messages = []
    base.log = lambda message, quiet=True: messages.append(message)
    registry = _registry(base)

    def slot_logs():
        """Just the slot-map lines out of everything the registry logged."""
        return [message for message in messages if "ChargerRegistry: slots" in message]

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme")])
    assert len(slot_logs()) == 1, slot_logs()
    assert "ohme/ohme0" in slot_logs()[0]

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.ohme")])
    assert len(slot_logs()) == 1, "An unchanged map must not be re-logged"

    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.ce1234")])
    assert len(slot_logs()) == 2, "A reallocation must be logged"


def test_charger_registry(my_predbat=None):
    """Aggregate runner for unit_test.py, which is the only test gate upstream CI runs.

    pytest is not run in CI, so a test file that is not reachable from unit_test.py's
    TEST_REGISTRY is not actually protecting anything. The sub-tests are discovered from the
    module rather than hand-listed, so a test added here cannot silently drop out of CI the
    way this whole file did.

    Returns a truthy value when anything failed, which is what unit_test.py's runner reads.
    """
    print("\n" + "=" * 70)
    print("CHARGER REGISTRY TEST SUITE")
    print("=" * 70)

    sub_tests = [(name, func) for name, func in sorted(globals().items()) if name.startswith("test_") and name != "test_charger_registry" and callable(func)]

    passed = 0
    failed = 0
    for name, test_func in sub_tests:
        print(f"\n[{name}] {test_func.__doc__.splitlines()[0] if test_func.__doc__ else ''}")
        print("-" * 70)
        try:
            test_func()
            print(f"✓ PASSED: {name}")
            passed += 1
        except Exception as e:
            print(f"✗ FAILED: {name}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed > 0


# Not a test itself: pytest would otherwise collect this runner alongside the sub-tests it
# calls, running the whole file twice per session and reporting a pass/fail count that does
# not match the tests. unit_test.py calls it directly by name, which this does not affect.
test_charger_registry.__test__ = False


def test_ohme_energy_survives_both_discovery_orders_and_rediscovery():
    """Registry-owned energy lists must not make Ohme drop its own contribution."""
    import asyncio
    from tests.test_ohme import MockOhmeAPI, ENERGY_TODAY_ENTITY, POWER_WATTS_ENTITY

    for ohme_first in (True, False):
        api = MockOhmeAPI()
        registry = api.base.charger_registry
        if ohme_first:
            asyncio.run(api.automatic_config())
        registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.ce1234", energy="sensor.ce1234_energy", power="sensor.ce1234_power")])
        for _ in range(2):
            asyncio.run(api.automatic_config())
            assert api.args["car_charging_energy"] == ["sensor.ce1234_energy", ENERGY_TODAY_ENTITY]
            assert api.args["car_charging_power"] == ["sensor.ce1234_power", POWER_WATTS_ENTITY]


def test_legacy_and_discovered_energy_are_counted_once():
    """Two integrations measuring the same serial must not double the site total."""
    for legacy in ("sensor.myenergi_zappi_10000001_charge_added_session", "sensor.predbat_myenergi_zappi_10000001_session_energy"):
        base = MockBase(car_charging_energy=[legacy, "sensor.myenergi_zappi_100000010_charge_added_session"])
        registry = _registry(base)
        preregister_legacy(registry, base)
        registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned="sensor.zappi_connected", energy="sensor.predbat_myenergi_zappi_10000001_session_energy")])
        assert base.args["car_charging_energy"] == ["sensor.myenergi_zappi_100000010_charge_added_session", "sensor.predbat_myenergi_zappi_10000001_session_energy"]
        registry.replace_source("myenergi", [])
        assert base.args["car_charging_energy"] == [legacy, "sensor.myenergi_zappi_100000010_charge_added_session"]


def test_plan_publication_is_per_car_and_confirms_only_its_allocation():
    """Publishing isolates car windows and cannot approve a mapping changed mid-plan."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from output import Output

    registry = _registry()
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0")])
    generation = registry.snapshot_generation()
    captured = {}
    base = SimpleNamespace(
        num_cars=3, prefix="predbat", minutes_now=0, forecast_minutes=1440,
        midnight_utc=datetime(2026, 9, 4, tzinfo=timezone.utc),
        car_charging_slots=[
            [{"start": 60, "end": 120, "kwh": 1, "average": 10, "cost": 10}],
            [{"start": 180, "end": 240, "kwh": 2, "average": 20, "cost": 40}],
            [],
        ],
        charger_registry=registry, car_charger_generation=generation,
        time_abs_str=str,
        dashboard_item=lambda entity, state, attributes: captured.update({entity: attributes}),
    )
    # The plan is in flight when another source takes slot 0.
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234")])
    Output.publish_car_plan(base)
    assert [window["start"] for window in captured["binary_sensor.predbat_car_charging_slot"]["planned"]] == ["60"]
    assert [window["start"] for window in captured["binary_sensor.predbat_car_charging_slot_1"]["planned"]] == ["180"]
    assert captured["binary_sensor.predbat_car_charging_slot_2"]["planned"] == []
    assert not registry.plan_is_current()
    base.car_charger_generation = registry.snapshot_generation()
    Output.publish_car_plan(base)
    assert registry.plan_is_current()


def test_all_charger_controls_wait_after_slot_reallocation():
    """Existing slot sensors cannot authorise control after their car identities change."""
    import asyncio
    from unittest.mock import AsyncMock
    from tests.test_ohme import _ohme_control_api
    from tests.test_gateway import TestEvControl
    from tests.test_myenergi import _controlling_component, IN_WINDOW, NIGHT_WINDOW
    from tests.test_ge_cloud import _evc_control_component, _register_gecloud_chargers, EVC_PLAN_SENSOR

    ohme = _ohme_control_api(windows=[])
    ohme.base.charger_registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234")])
    ohme.states[("binary_sensor.predbat_car_charging_slot_1", "planned")] = []
    asyncio.run(ohme.control_charge())
    assert not ohme.client.request_log
    ohme.base.charger_registry.confirm_plan(ohme.base.charger_registry.generation)
    asyncio.run(ohme.control_charge())
    assert ohme.client.request_log

    gateway = TestEvControl()._make_gateway(configured_chargers=["CP-A", "CP-B"])
    gateway._ev_charging_active = {"CP-B": True}
    gateway._ev_windows = {0: [], 1: []}
    gateway._send_ev_command = AsyncMock()
    gateway.base.charger_registry.replace_source("gateway", [ChargerEntry("gateway", "CP-B")])
    asyncio.run(gateway._apply_ev_charging_state())
    gateway._send_ev_command.assert_not_awaited()
    gateway.base.charger_registry.confirm_plan(gateway.base.charger_registry.generation)
    asyncio.run(gateway._apply_ev_charging_state())
    gateway._send_ev_command.assert_awaited_once()

    myenergi = _controlling_component(plans={0: [NIGHT_WINDOW], 1: []})
    myenergi.base.charger_registry.confirm_plan(myenergi.base.charger_registry.generation)
    myenergi.base.charger_registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234")])
    asyncio.run(myenergi.control_charge(IN_WINDOW))
    myenergi.transport.set_mode.assert_not_awaited()
    myenergi.base.charger_registry.confirm_plan(myenergi.base.charger_registry.generation)
    asyncio.run(myenergi.control_charge(IN_WINDOW))
    myenergi.transport.set_mode.assert_awaited_once()

    commands = []
    gecloud = _evc_control_component(commands)
    gecloud.evc_device_list = ["charger"]
    gecloud.evc_device = {"charger": {"serial_number": "EVC200", "status": "charging"}}
    gecloud.entity_attributes = {EVC_PLAN_SENSOR: {"planned": []}}
    _register_gecloud_chargers(gecloud, ["EVC100", "EVC200"])
    gecloud.base.charger_registry.replace_source("gecloud", [ChargerEntry("gecloud", "EVC200")])
    asyncio.run(gecloud.evc_control_charge(IN_WINDOW))
    assert commands == []
    gecloud.base.charger_registry.confirm_plan(gecloud.base.charger_registry.generation)
    asyncio.run(gecloud.evc_control_charge(IN_WINDOW))
    assert commands == [("charger", "stop-charge")]


def test_hand_written_soc_and_now_survive_a_discovered_charger():
    """Discovery must never delete config it cannot compose - the blocker this fixes.

    myenergi supplies neither soc nor now, so registering a Zappi alongside a hand-configured
    car leaves both keys with a gap. Writing None into them, which is what an earlier version
    did, deletes the key outright: fetch.py:1383 then read car_charging_soc as 0.0 and planned
    a full charge into a possibly-full car, and fetch.py:2400 fell back to "no" for
    car_charging_now. Before the registry existed the myenergi component never wrote either
    key, so the user's own value simply survived - and that is the outcome restored here.
    """
    base = MockBase(
        num_cars=1,
        car_charging_planned=["binary_sensor.my_car_plugged"],
        car_charging_soc=["sensor.my_car_soc"],
        car_charging_now=["binary_sensor.my_car_charging"],
    )
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned="sensor.predbat_myenergi_zappi_10000001_plug_status", energy="sensor.predbat_myenergi_zappi_10000001_charge_added_session")])

    assert base.args["car_charging_soc"] == ["sensor.my_car_soc"]
    assert base.args["car_charging_now"] == ["binary_sensor.my_car_charging"]
    # The key that can be composed still is, and the Zappi still gets its own car.
    assert base.args["car_charging_planned"] == ["binary_sensor.my_car_plugged", "sensor.predbat_myenergi_zappi_10000001_plug_status"]
    assert base.args["num_cars"] == 2
    assert registry.slot_for("myenergi", "10000001") == 1


def test_a_second_source_does_not_delete_the_now_the_first_supplied():
    """A charger arriving without `now` must not take away the `now` another one has.

    The gateway is the only source that populates car_charging_now, so on a gateway install
    that adds an Ohme - ohme supplies no now - nulling the key on a gap dropped the gateway
    car's own charging-now sensor. That is essentially every mixed install.
    """
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("gateway", [ChargerEntry("gateway", "CP-A", planned="binary_sensor.gw_plugged", now="binary_sensor.gw_session_active")])
    assert base.args["car_charging_now"] == ["binary_sensor.gw_session_active"]

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])

    # Short for the new car count, which is exactly the pre-registry outcome: fetch.py warns
    # the extra car is out of range rather than silently reading a deleted key as "no".
    assert base.args["car_charging_now"] == ["binary_sensor.gw_session_active"]
    assert base.args["car_charging_planned"] == ["binary_sensor.gw_plugged", "binary_sensor.predbat_ohme_connected"]
    assert base.args["num_cars"] == 2


def test_the_key_is_written_again_once_every_slot_has_a_value():
    """Leaving a key alone on a gap must not stop the registry writing it when the gap closes."""
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("gateway", [ChargerEntry("gateway", "CP-A", planned="binary_sensor.a", soc="sensor.a_soc")])
    assert base.args["car_charging_soc"] == ["sensor.a_soc"]

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.b")])
    assert base.args["car_charging_soc"] == ["sensor.a_soc"], "a gap leaves the key as it stands"

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.b", soc="sensor.predbat_ohme_battery_percent")])
    assert base.args["car_charging_soc"] == ["sensor.a_soc", "sensor.predbat_ohme_battery_percent"]


def test_a_charger_that_takes_slot_zero_does_not_inherit_the_previous_cars_soc():
    """A list the registry composed itself must not survive the slots moving under it.

    Ohme registers alone, so the registry writes car_charging_soc = [ohme_soc] with the ohme in
    slot 0. A myenergi Zappi then registers and sorts ahead of it ("myenergi" < "ohme"), taking
    slot 0 and pushing the ohme to slot 1 - and myenergi supplies no SoC. Keeping the old list
    on that gap left slot 0 holding the *ohme's* SoC while slot 0 is now the Zappi's car, so
    fetch.py:1383 read one car's battery level for another and planned its charge from it.
    """
    base = MockBase()
    registry = _registry(base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", soc="sensor.predbat_ohme_battery_percent")])
    assert base.args["car_charging_soc"] == ["sensor.predbat_ohme_battery_percent"]

    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned="sensor.predbat_myenergi_zappi_10000001_plug_status")])

    assert registry.slot_for("myenergi", "10000001") == 0
    assert registry.slot_for("ohme", "ohme0") == 1
    assert base.args.get("car_charging_soc") != ["sensor.predbat_ohme_battery_percent"], "the ohme's SoC must not be read as the Zappi car's"
    assert "car_charging_soc" not in base.args, "nothing of the user's was in there, so the key goes back to unset"


def test_a_stale_composed_list_keeps_the_users_own_slots():
    """Trimming a composed list drops only what the registry invented, never the user's config.

    Legacy slots sort first, so they are always inside the kept head: here the hand-written SoC
    keeps slot 0 while the discovered charger's, which no longer describes the car standing in
    its slot, is dropped.
    """
    base = MockBase(num_cars=1, car_charging_planned=["binary_sensor.my_car_plugged"], car_charging_soc=["sensor.my_car_soc"])
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected", soc="sensor.predbat_ohme_battery_percent")])
    assert base.args["car_charging_soc"] == ["sensor.my_car_soc", "sensor.predbat_ohme_battery_percent"]

    # The ohme goes away and a Zappi arrives, which supplies no SoC at all.
    registry.replace_source("ohme", [])
    registry.replace_source("myenergi", [ChargerEntry("myenergi", "10000001", planned="sensor.predbat_myenergi_zappi_10000001_plug_status")])

    assert base.args["car_charging_soc"] == ["sensor.my_car_soc"]


def test_a_slot_gap_is_logged_once_and_again_when_it_changes():
    """The gap is worth one line per gap, not one per rediscovery."""
    base = MockBase()
    logs = []
    base.log = lambda message, quiet=True: logs.append(message)
    registry = _registry(base)

    def gap_logs():
        """Every line the gap rule has written so far."""
        return [message for message in logs if "leaving car_charging_soc" in message]

    ohme = ChargerEntry("ohme", "ohme0", planned="binary_sensor.b")
    registry.replace_source("gateway", [ChargerEntry("gateway", "CP-A", planned="binary_sensor.a", soc="sensor.a_soc")])
    registry.replace_source("ohme", [ohme])
    assert len(gap_logs()) == 1
    assert "('ohme', 'ohme0')" in gap_logs()[0]

    registry.replace_source("ohme", [ohme])
    assert len(gap_logs()) == 1, "an unchanged gap must not be re-logged on every rediscovery"

    # A second charger with no SoC changes what the gap is, which is worth saying.
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "EVC999999", planned="binary_sensor.c")])
    assert len(gap_logs()) == 2


def test_an_uppercase_gecloud_serial_dedupes_a_legacy_entity():
    """GivEnergy Cloud serials are upper case, HA entity ids are not - the match must ignore case.

    async_automatic_config_evc lower-cases the serial when it builds the entity name, so a
    case-sensitive search for the raw device_id could never match a gecloud serial and that
    half of the legacy dedup silently did nothing. myenergi serials are numeric, which is why
    it went unnoticed.
    """
    base = MockBase(num_cars=1, car_charging_planned=["sensor.givenergy_evc123456_status"])
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "EVC123456", planned="binary_sensor.predbat_gecloud_evc123456_evc_car_connected")])

    assert base.args["car_charging_planned"] == ["binary_sensor.predbat_gecloud_evc123456_evc_car_connected"]
    assert base.args["num_cars"] == 1, "the same physical charger must not be counted twice"


def test_an_uppercase_serial_still_respects_the_word_boundary():
    """Ignoring case must not weaken the boundary rule that keeps a longer serial its own car."""
    base = MockBase(num_cars=1, car_charging_planned=["sensor.givenergy_we1913g0055_status"])
    registry = _registry(base)
    preregister_legacy(registry, base)
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "WE1913G005", planned="binary_sensor.predbat_gecloud_we1913g005_evc_car_connected")])

    assert base.args["num_cars"] == 2, "WE1913G0055 is a different charger, not a duplicate"
    assert base.args["car_charging_planned"] == ["sensor.givenergy_we1913g0055_status", "binary_sensor.predbat_gecloud_we1913g005_evc_car_connected"]


def test_a_control_loop_logs_once_while_it_waits_for_a_plan():
    """A blocked control loop must say so - once - rather than returning silently.

    charger_plan_ready() now gates all four control loops and every wait on it is a bare
    return, so "my charger stopped responding" left nothing at all in the log to explain it.
    """
    from component_base import ComponentBase

    class TestComponent(ComponentBase):
        """Test implementation of ComponentBase."""

        def initialize(self, **kwargs):
            """No-op initialize for testing."""
            pass

    base = MockBase()
    logs = []
    base.log = lambda message, quiet=True: logs.append(message)
    component = TestComponent(base)
    registry = base.charger_registry

    def waits():
        """Every wait line logged so far."""
        return [message for message in logs if "waiting for a car charge plan" in message]

    registry.replace_source("ohme", [ChargerEntry("ohme", "ohme0", planned="binary_sensor.predbat_ohme_connected")])
    generation = registry.snapshot_generation()

    assert not component.charger_plan_ready(generation, "Ohme API")
    assert not component.charger_plan_ready(generation, "Ohme API")
    assert len(waits()) == 1, "the wait must be logged once, not once per cycle"
    assert "Ohme API" in waits()[0]

    registry.confirm_plan(generation)
    assert component.charger_plan_ready(generation, "Ohme API")
    assert len(waits()) == 1

    # The latch cleared when the plan arrived, so a fresh wait is reported afresh.
    registry.replace_source("gecloud", [ChargerEntry("gecloud", "CE1234", planned="binary_sensor.a")])
    assert not component.charger_plan_ready(registry.snapshot_generation(), "Ohme API")
    assert len(waits()) == 2


def test_published_car_plans_do_not_leak_windows_between_cars():
    """Each car's published `planned` attribute must hold only that car's own windows.

    A regression test for output.py on its own - no registry involved. The window list was
    built once outside the per-car loop, so car 1's attribute carried car 0's windows as well
    as its own and car 2's carried both. Multi-car sites have had this since long before this
    branch; it matters more now because the new control loops read exactly that attribute to
    decide whether to charge, so a leaked window starts a charger outside its own plan.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from output import Output

    captured = {}
    base = SimpleNamespace(
        num_cars=3,
        prefix="predbat",
        minutes_now=0,
        forecast_minutes=1440,
        midnight_utc=datetime(2026, 9, 4, tzinfo=timezone.utc),
        car_charging_slots=[
            [{"start": 60, "end": 120, "kwh": 1, "average": 10, "cost": 10}],
            [{"start": 180, "end": 240, "kwh": 2, "average": 20, "cost": 40}],
            [{"start": 300, "end": 360, "kwh": 3, "average": 30, "cost": 90}],
        ],
        charger_registry=SimpleNamespace(confirm_plan=lambda generation: None),
        car_charger_generation=None,
        time_abs_str=str,
        dashboard_item=lambda entity, state, attributes: captured.update({entity: attributes}),
    )
    Output.publish_car_plan(base)

    published = [captured["binary_sensor.predbat_car_charging_slot" + suffix]["planned"] for suffix in ("", "_1", "_2")]
    assert [[window["start"] for window in plan] for plan in published] == [["60"], ["180"], ["300"]]
    # Distinct list objects too: one shared list appended to three times would fail the check
    # above, but a list published by reference and cleared in place would not.
    assert len({id(plan) for plan in published}) == 3

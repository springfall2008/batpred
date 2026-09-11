# fmt: off
# pylint: disable=line-too-long
"""End-to-end tests for the discovery catalogue: coordinator.py plus the five reporters from Tasks 5-9.

Task 1-9 built the coordinator, its typed containers, the redactor and five reporters (GivTCP, GE
Cloud, Octopus, Ohme, Solcast) in isolation, each with its own unit tests. This module is the one
place that wires several of them together against a SHARED base object and proves three things no
single-reporter test can: the assembled catalogue tags records from different components by
source and surfaces the conflicts a mixed fleet actually creates; a seeded corpus of sensitive
values never survives redaction, in original OR transformed form; and - the release's central
promise - that running the whole discovery path changes not one byte of apps.yaml configuration.

Kraken and Axle also exist in the codebase but report nothing in v1 (see coordinator.py's
SECTION_SPEC docstring), so they are not part of this fleet - a component with no build_discovery()
would only prove Coordinator._component_status()'s "no_report" path, which test_coordinator.py's
test_assemble_component_status already covers directly and more cheaply than constructing one here
would.
"""

import copy
import functools
import json

import yaml

from coordinator import Coordinator
from gecloud import GECloudDirect
from givtcp import GivTCPComponent
from mock_base import MockBase
from octopus import OctopusAPI
from ohme import CAR_DISCOVERY_ENTITY_SPEC, CHARGER_DISCOVERY_ENTITY_SPEC, OhmeAPI
from solcast import SolarAPI
from tests.test_givtcp_component import _rest_data_blob
from tests.test_infra import run_async

# A registry name deliberately never constructed by _build_reporting_fleet(), so the assembly test
# can assert that a component which never reports still gets a status entry rather than being
# silently absent from the catalogue's components map.
UNCONFIGURED_REGISTRY_NAME = "solis"

# The five registry keys the fleet's reporters are filed under - the same strings
# components.py's COMPONENT_LIST registers each class against, so a report lands in the catalogue
# under exactly the source name a real install would show.
REPORTER_NAMES = ("givtcp", "gecloud", "octopus", "ohme", "solar")


class _StubComponents:
    """Stand-in for components.Components: the real Coordinator plus a per-name status lookup.

    Coordinator.assemble() reads get_all()/is_active()/is_alive()/load_error() to build a status
    entry for every component the registry knows about, and report_discovery() reaches the
    coordinator via base.components.coordinator - this stub supplies both surfaces without
    pulling in the real component lifecycle/health-monitoring machinery, which build_discovery()
    never touches.
    """

    def __init__(self, coordinator, active, all_names):
        """Hold `coordinator`; every name in `active` is both active and alive, `all_names` is the full registry (a superset, so an extra name that was never constructed still gets a status)."""
        self.coordinator = coordinator
        self._active = set(active)
        self._all = list(all_names)

    def get_all(self):
        """Every component name this registry knows about."""
        return list(self._all)

    def is_active(self, name):
        """Whether the named component was constructed."""
        return name in self._active

    def is_alive(self, name):
        """Whether the named component is running and fresh - every active name, in this stub."""
        return name in self._active

    def load_error(self, name):
        """Why the named component failed to construct - never, in this stub."""
        return None

    def get_component(self, name):
        """The stub component registered under this name - none, in this stub (no Storage)."""
        return None


def _build_reporting_fleet():
    """One shared MockBase, wired to a real Coordinator and five real reporters via a registry stub.

    Returns (base, components): `base` is the shared MockBase, seeded with a handful of apps.yaml
    keys representative of a real install so the observe-only invariant test has something
    non-trivial to snapshot; `components` is the list of five constructed reporter instances, each
    already carrying enough fixture state for build_discovery() to produce a non-empty report.

    Every component is the REAL reporter class from its own module (GivTCPComponent, GECloudDirect,
    OctopusAPI, OhmeAPI, SolarAPI), constructed directly against the shared base exactly as
    Components.initialize() would - only the network/websocket layer underneath each one is
    skipped, since build_discovery() never touches it. component_name is set explicitly to the
    registry key each is filed under in components.py's COMPONENT_LIST, matching what a real
    install does, so report_discovery() files every report under that name.

    Nothing here calls run() or automatic_config() on any component: constructing a component and
    calling build_discovery() directly is the exact discovery-only path production takes (see
    each reporter's own run(), which calls build_discovery()/report_discovery() independently of
    its automatic_config() call), so there is nothing here that would legitimately write to
    self.args in the first place - not just an automatic_config() call this fixture happens not to
    make.
    """
    base = MockBase()
    base.args.update(
        {
            "givtcp_rest": ["http://givtcp:6345"],
            "num_inverters": 2,
            "inverter_type": ["GE", "GEC"],
            "octopus_api_key": "should-never-be-read-or-written-by-discovery",
            "octopus_api_account": "A-1234ABCD",
            "ohme_login": "driver@example.com",
            "num_cars": 1,
        }
    )
    coordinator = Coordinator(base)
    base.components = _StubComponents(coordinator, active=REPORTER_NAMES, all_names=REPORTER_NAMES + (UNCONFIGURED_REGISTRY_NAME,))

    givtcp = GivTCPComponent(base, rest_urls=["http://givtcp:6345"], automatic=True)
    givtcp.component_name = "givtcp"
    givtcp.rest[0].inverter.rest_data = _rest_data_blob()
    givtcp.discovered = [0]
    givtcp.discovery_done = True
    run_async(givtcp.publish_data())

    gecloud = GECloudDirect(base, ge_cloud_direct=True, api_key="test-ge-cloud-key", automatic=True)
    gecloud.component_name = "gecloud"
    gecloud.info = {"pv001": {"info": {"model": "GIV-PV"}}}
    gecloud_devices = {"ems": None, "gateway": None, "battery": ["battery001"], "pv": ["pv001"], "battery_meters": {}}
    # GE Cloud's real build_discovery() takes `devices` as an explicit argument - the one named
    # exception among the five reporters (see the design's own Interfaces block for it) - so it is
    # bound here to a fixed devices snapshot via functools.partial. This is what lets every
    # reporter in the fleet be driven the same no-argument way: component.build_discovery().
    gecloud.build_discovery = functools.partial(GECloudDirect.build_discovery, gecloud, gecloud_devices)

    octopus = OctopusAPI(base, key="test-octopus-key", account_id="A-1234ABCD", automatic=True)
    octopus.component_name = "octopus"
    octopus.mpan = "1200023305967"
    octopus.tariffs = {"import": {"tariffCode": "E-1R-AGILE-24-10-01-A", "productCode": "AGILE-24-10-01", "deviceID": "meter-1"}}
    octopus.intelligent_devices = {"smart-charge-9001": {"suspended": False}}

    ohme = OhmeAPI(base, email="driver@example.com", password="hunter2", ohme_automatic=True)
    ohme.component_name = "ohme"
    ohme.client.serial = "OHME-SERIAL-1"
    for entity_id, _domain, _access in list(CHARGER_DISCOVERY_ENTITY_SPEC.values()) + list(CAR_DISCOVERY_ENTITY_SPEC.values()):
        base.set_state_wrapper(entity_id, "on")

    solar = SolarAPI(
        base,
        solcast_host=None,
        solcast_api_key=None,
        solcast_sites=None,
        solcast_poll_hours=None,
        forecast_solar=False,
        forecast_solar_max_age=None,
        forecast_solar_open_meteo_backup=False,
        forecast_solar_open_meteo_first=False,
        pv_forecast_today=None,
        pv_forecast_tomorrow=None,
        pv_forecast_d3=None,
        pv_forecast_d4=None,
        pv_scaling=None,
        open_meteo_forecast=False,
        open_meteo_forecast_max_age=None,
    )
    solar.component_name = "solar"
    solar.discovered_sites = ["site-one"]

    return base, [givtcp, gecloud, octopus, ohme, solar]


def test_discovery_writes_no_config(my_predbat=None):
    """Discovery is observe-only: nothing it does may change a single apps.yaml key.

    This is the release's central promise. A prior task's test let a real automatic_config() run
    against a shared base and corrupt an unrelated later test - the fix there was to stub whatever
    would legitimately write args. Here nothing needs stubbing: build_discovery()/report_discovery()
    is the actual discovery-only code path every reporter's run() takes independently of its own
    automatic_config() call (see _build_reporting_fleet()'s own docstring), so calling exactly that
    - and never run() or automatic_config() itself - proves discovery writes nothing without
    dodging the thing that would.
    """
    base, components = _build_reporting_fleet()
    before = copy.deepcopy(base.args)
    for component in components:
        component.report_discovery(component.build_discovery())
    base.components.coordinator.assemble()
    base.components.coordinator.catalogue()
    base.components.coordinator.publish()
    assert base.args == before, "discovery must not write configuration in v1"
    print("PASS: discovery changed no configuration")
    return 0


def test_discovery_assembles_across_sections_and_sources(my_predbat=None):
    """Several reporters into one coordinator: records land in the right section tagged by
    source, the conflicts a mixed fleet actually creates are recorded, every registry entry gets
    a status, and the assembled catalogue survives both JSON and YAML serialisation."""
    base, components = _build_reporting_fleet()
    for component in components:
        component.report_discovery(component.build_discovery())

    catalogue = base.components.coordinator.assemble()

    inverter_sources = {record["source"] for record in catalogue["inverters"]}
    assert inverter_sources == {"givtcp", "gecloud"}, inverter_sources

    meter_sources = {record["source"] for record in catalogue["meters"]}
    assert meter_sources == {"octopus"}, meter_sources

    charger_sources = {record["source"] for record in catalogue["chargers"]}
    assert charger_sources == {"ohme"}, charger_sources

    car_sources = {record["source"] for record in catalogue["cars"]}
    assert car_sources == {"octopus", "ohme"}, car_sources

    forecast_sources = {record["source"] for record in catalogue["forecasts"]}
    assert forecast_sources == {"solar"}, forecast_sources

    pv_record = next(record for record in catalogue["inverters"] if record["device_id"] == "gecloud:pv001")
    assert pv_record["source"] == "gecloud"
    assert pv_record["functions"] == ["solar"], pv_record
    assert "inverter_type" not in pv_record, "a PV-only device has no inverter_type"

    conflicts = {entry["kind"]: entry for entry in catalogue["observations"]["conflicts"]}
    assert "multiple_inverter_sources" in conflicts, conflicts
    assert sorted(conflicts["multiple_inverter_sources"]["claimed_by"]) == ["gecloud", "givtcp"]
    assert "contested_car_slots" in conflicts, conflicts

    for name in REPORTER_NAMES + (UNCONFIGURED_REGISTRY_NAME,):
        assert name in catalogue["components"], catalogue["components"]
    for name in REPORTER_NAMES:
        assert catalogue["components"][name]["status"] == "ok", catalogue["components"][name]
    assert catalogue["components"][UNCONFIGURED_REGISTRY_NAME]["status"] == "not_configured"

    # The document every consumer actually gets (the debug dump, the sensor) is the redacted one -
    # that is what has to round-trip through both serialisers a debug dump and its YAML export use.
    redacted = base.components.coordinator.catalogue()
    json.dumps(redacted)
    yaml.safe_dump(redacted)

    print("PASS: records from five reporters assemble into tagged sections, conflicts are recorded, every registry entry gets a status, and the catalogue serialises cleanly")
    return 0


def _corpus_coordinator():
    """A fresh Coordinator with a fixed salt, so this test's redaction is reproducible."""
    base = MockBase()
    coordinator = Coordinator(base)
    coordinator.salt = "corpus-salt-0001"
    return coordinator


def test_redaction_corpus_hides_identifiers_and_keeps_public_data(my_predbat=None):
    """A report seeded with a credential-shaped field, a 13-digit MPAN, an email, a postcode and a
    coordinate pair - spread across hardware_ids/info/ratings/entities/account_ids/tariff - never
    surfaces any of those originals, in byte-exact OR transformed form, while a serial, a firmware
    string and a tariff code all stay readable.

    The transformed-form check matters on its own: an earlier leak survived a passing test that
    only checked the byte-exact original, because the value actually appeared case-folded with its
    separators swapped inside an entity_id a get_entity_name()-style helper built from it.
    """
    coordinator = _corpus_coordinator()
    coordinator.report(
        "givtcp",
        {
            "inverters": [
                {
                    "device_id": "givtcp:SN-CORPUS-1",
                    "hardware_ids": {"serial": "SA2242G123"},
                    "info": {"firmware": "D0.451", "api_key": "sk-should-never-reach-the-catalogue", "postcode": "SW1A 1AA"},
                    "ratings": {"latitude": 51.5074, "longitude": -0.1278},
                    "entities": {"login": {"entity_id": "sensor.givtcp_owner_user@example.com", "domain": "sensor", "access": "r"}},
                }
            ]
        },
    )
    coordinator.report(
        "octopus",
        {
            "meters": [
                {
                    "device_id": "octopus:m1",
                    "direction": "import",
                    "account_ids": {"mpan": "1234567890123"},
                    "tariff": {"info": {"tariff_code": "E-1R-AGILE-24-10-01-A"}},
                }
            ],
            "cars": [
                {
                    "device_id": "octopus:dev1",
                    "account_ids": {"account": "A-1234ABCD"},
                    "entities": {"slot": {"entity_id": "binary_sensor.predbat_octopus_a_1234abcd_intelligent_dispatch_1", "domain": "binary_sensor", "access": "r"}},
                }
            ],
        },
    )

    catalogue = coordinator.catalogue()
    text = str(catalogue)

    for original in (
        "sk-should-never-reach-the-catalogue",
        "1234567890123",
        "user@example.com",
        "SW1A 1AA",
        "51.5074",
        "0.1278",
        "A-1234ABCD",
    ):
        assert original not in text, "{} must not survive redaction".format(original)

    # Transformed form: the account id echoed lower-cased with "-" swapped for "_", exactly as a
    # get_entity_name()-style helper folds one into an entity id - checked separately from the
    # byte-exact originals above, since a mutant that only broke exact-match substitution would
    # otherwise pass this test for the wrong reason.
    assert "a_1234abcd" not in text, "a case-folded, separator-swapped echo of the account id must not survive either"

    assert "SA2242G123" in text, "a genuine hardware serial must stay readable"
    assert "D0.451" in text, "a genuine firmware string must stay readable"
    assert "E-1R-AGILE-24-10-01-A" in text, "a genuine tariff code must stay readable"

    print("PASS: the redaction corpus hides every seeded identifier, in original and transformed form, while serials/firmware/tariff codes stay readable")
    return 0


def test_discovery_catalogue_all(my_predbat=None):
    """Run every discovery catalogue end-to-end test, returning the number of failures."""
    failures = 0
    failures += test_discovery_writes_no_config()
    failures += test_discovery_assembles_across_sections_and_sources()
    failures += test_redaction_corpus_hides_identifiers_and_keeps_public_data()
    return failures

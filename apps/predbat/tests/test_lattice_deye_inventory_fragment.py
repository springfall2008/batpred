"""Tests for the pure Deye complete-inventory normalizer."""

# cspell:ignore autoconfig

import dataclasses
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    ProviderHealth,
    compile_auto_config,
)
from lattice_complete_inventory_fragment import (  # noqa: E402
    MAX_COMPLETE_INVENTORY_DEVICES,
    MAX_COMPLETE_INVENTORY_SITES,
)
from lattice_deye_inventory_fragment import (  # noqa: E402
    DeyeInventoryFragmentPublisher,
    DeyeInverterObservation,
    DeyeStationObservation,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    InMemoryFragmentAdapterStateStore,
)
from lattice_ge_cloud_fragment import (  # noqa: E402
    GECloudDeviceSnapshot,
    GECloudFragmentPublisher,
)


def assert_value_free_validation_error(test_case, action, raw_value):
    """Assert one public validation error retains no provider source text."""
    try:
        action()
    except ValueError as error:
        test_case.assertIs(type(error), ValueError)
        test_case.assertNotIn(raw_value, str(error))
        test_case.assertNotIn(raw_value, repr(error))
        test_case.assertNotIn(raw_value, repr(error.args))
        test_case.assertNotIn(raw_value, error.args)
        test_case.assertFalse(hasattr(error, "object"))
        test_case.assertIsNone(error.__cause__)
        test_case.assertIsNone(error.__context__)
    else:
        test_case.fail("expected a value-free ValueError")


class HostileText(str):
    """String subclass whose scalar hooks expose a fake provider secret."""

    secret = "provider-secret-value"

    def _fail(self, *_args, **_kwargs):
        """Fail if validation invokes any subclass-controlled hook."""
        raise RuntimeError(self.secret)

    strip = _fail
    lower = _fail
    upper = _fail
    __format__ = _fail
    __iter__ = _fail
    __len__ = _fail
    __hash__ = _fail
    __eq__ = _fail
    __lt__ = _fail


class HostileInt(int):
    """Integer subclass whose scalar hooks expose a fake provider secret."""

    secret = HostileText.secret

    def _fail(self, *_args, **_kwargs):
        """Fail if validation invokes any subclass-controlled hook."""
        raise RuntimeError(self.secret)

    __str__ = _fail
    __format__ = _fail
    __hash__ = _fail
    __eq__ = _fail
    __lt__ = _fail


class DeyeStationObservationSubclass(DeyeStationObservation):
    """Observation subclass that must not cross the public boundary."""


class DeyeInverterObservationSubclass(DeyeInverterObservation):
    """Observation subclass that must not cross the public boundary."""


def station(station_id="STATION-A", online=True):
    """Build one explicit Deye station observation."""
    return DeyeStationObservation(
        station_id=station_id,
        online=online,
    )


def inverter(
    device_id="DEYE-DEVICE-A",
    station_id="STATION-A",
    api_verified_serial="DEYE-SERIAL-A",
    model="SUN-6K",
    online=True,
):
    """Build one explicit Deye inverter observation."""
    return DeyeInverterObservation(
        device_id=device_id,
        station_id=station_id,
        api_verified_serial=api_verified_serial,
        model=model,
        online=online,
    )


def publisher(enabled=True, state_store=None):
    """Build one Deye inventory publisher and durable store."""
    state_store = state_store or InMemoryFragmentAdapterStateStore()
    return (
        DeyeInventoryFragmentPublisher(
            "deye-cloud",
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestDeyeInventoryFragmentPublisher(unittest.TestCase):
    """Deye discovery remains pure, complete, and authority-free."""

    def test_default_off_does_not_import_live_deye_or_write(self):
        """Construction cannot touch live auth, discovery, or control code."""
        module_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), ".."),
        )
        isolated = subprocess.run(
            (
                sys.executable,
                "-c",
                "import sys; import lattice_deye_inventory_fragment; " "assert 'deye' not in sys.modules",
            ),
            cwd=module_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(isolated.returncode, 0, isolated.stderr)

        adapter, state_store = publisher(enabled=False)
        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(
            adapter.ingest_snapshot(
                -1,
                (object(),),
                (object(),),
            )
        )
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_scalar_and_observation_subclasses_fail_value_free(self):
        """Deye discovery cannot invoke provider-controlled scalar hooks."""
        constructor_cases = (
            lambda: station(HostileInt(1)),
            lambda: inverter(device_id=HostileText("DEVICE")),
            lambda: inverter(station_id=HostileText("STATION")),
            lambda: inverter(
                api_verified_serial=HostileText("SERIAL"),
            ),
            lambda: inverter(model=HostileText("Model")),
        )
        for action in constructor_cases:
            with self.subTest(action=action):
                assert_value_free_validation_error(
                    self,
                    action,
                    HostileText.secret,
                )

        ingestion_cases = (
            lambda adapter: adapter.ingest_snapshot(
                HostileInt(1),
                (),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (
                    DeyeStationObservationSubclass(
                        "STATION-A",
                    ),
                ),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (station(),),
                (
                    DeyeInverterObservationSubclass(
                        "DEVICE-A",
                        "STATION-A",
                    ),
                ),
            ),
        )
        for action in ingestion_cases:
            with self.subTest(action=action):
                adapter, state_store = publisher()
                assert_value_free_validation_error(
                    self,
                    lambda: action(adapter),
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, 0)

    def test_mutated_observations_are_revalidated_before_publication(self):
        """Post-construction Deye mutation cannot execute source hooks."""
        mutated_station = station()
        object.__setattr__(
            mutated_station,
            "station_id",
            HostileText("STATION-A"),
        )
        mutated_inverter = inverter()
        object.__setattr__(
            mutated_inverter,
            "model",
            HostileText("Model"),
        )
        cases = (
            (
                (mutated_station,),
                (),
            ),
            (
                (station(),),
                (mutated_inverter,),
            ),
        )
        for stations, inverters in cases:
            with self.subTest(
                stations=stations,
                inverters=inverters,
            ):
                adapter, state_store = publisher()
                assert_value_free_validation_error(
                    self,
                    lambda: adapter.ingest_snapshot(
                        1,
                        stations,
                        inverters,
                    ),
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, 0)

    def test_post_init_normalization_cannot_heal_deye_observations(self):
        """Whitespace and case mutations fail before durable publication."""

        def station_id():
            candidate = station()
            object.__setattr__(
                candidate,
                "station_id",
                " STATION-A ",
            )
            return (candidate,), (), " STATION-A "

        def inverter_device_id():
            candidate = inverter()
            object.__setattr__(
                candidate,
                "device_id",
                " DEYE-DEVICE-A ",
            )
            return (station(),), (candidate,), " DEYE-DEVICE-A "

        def inverter_station_id():
            candidate = inverter()
            object.__setattr__(
                candidate,
                "station_id",
                " STATION-A ",
            )
            return (station(),), (candidate,), " STATION-A "

        def inverter_serial():
            candidate = inverter()
            object.__setattr__(
                candidate,
                "api_verified_serial",
                "deye-serial-a",
            )
            return (station(),), (candidate,), "deye-serial-a"

        def inverter_model():
            candidate = inverter()
            object.__setattr__(
                candidate,
                "model",
                " SUN-6K ",
            )
            return (station(),), (candidate,), " SUN-6K "

        for build_case in (
            station_id,
            inverter_device_id,
            inverter_station_id,
            inverter_serial,
            inverter_model,
        ):
            with self.subTest(build_case=build_case):
                stations, inverters, raw_value = build_case()
                adapter, state_store = publisher()
                assert_value_free_validation_error(
                    self,
                    lambda: adapter.ingest_snapshot(
                        1,
                        stations,
                        inverters,
                    ),
                    raw_value,
                )
                self.assertEqual(state_store.writes, 0)

    def test_multi_station_inventory_is_normalized_and_complete(self):
        """Deye ordering is deterministic and a newer view removes absence."""
        adapter, state_store = publisher()
        stations = (
            station("STATION-B", online=None),
            station("STATION-A"),
        )
        inverters = (
            inverter(
                "DEVICE-B",
                "STATION-B",
                "SERIAL-B",
                online=None,
            ),
            inverter(
                "DEVICE-A",
                "STATION-A",
                "SERIAL-A",
            ),
        )

        self.assertTrue(
            adapter.ingest_snapshot(
                1,
                reversed(stations),
                reversed(inverters),
                health=True,
            )
        )
        first = adapter.read_snapshot()
        self.assertEqual(
            tuple(node["id"] for node in first.topology_fragment["nodes"]),
            (
                "inventory:deye-cloud:site:STATION-A",
                "inventory:deye-cloud:site:STATION-B",
                "inventory:deye-cloud:device:DEVICE-A",
                "inventory:deye-cloud:device:DEVICE-B",
            ),
        )

        self.assertTrue(
            adapter.ingest_snapshot(
                2,
                (station("STATION-A"),),
                (
                    inverter(
                        "DEVICE-A",
                        "STATION-A",
                        "SERIAL-A",
                    ),
                ),
            )
        )
        second = adapter.read_snapshot()
        serialized = repr(second)
        self.assertNotIn("STATION-B", serialized)
        self.assertNotIn("DEVICE-B", serialized)
        self.assertNotIn("SERIAL-B", serialized)
        self.assertEqual(adapter.generation, 2)
        self.assertEqual(adapter.discovery_generation, 2)
        self.assertEqual(state_store.writes, 2)

    def test_only_explicit_api_verified_serial_is_strong_identity(self):
        """Provider device/station IDs and model never correlate globally."""
        adapter, _state_store = publisher()
        adapter.ingest_snapshot(
            1,
            (station(),),
            (
                inverter(
                    device_id="LOOKS-LIKE-A-SERIAL",
                    api_verified_serial=None,
                    model="MODEL-IDENTIFIER",
                ),
                inverter(
                    device_id="DEVICE-VERIFIED",
                    api_verified_serial="verified-serial",
                ),
            ),
            health=True,
        )
        snapshot = adapter.read_snapshot()

        self.assertEqual(
            tuple((alias.kind, alias.value, alias.node_id) for alias in snapshot.identity_aliases),
            (
                (
                    "serial",
                    "VERIFIED-SERIAL",
                    "inventory:deye-cloud:device:DEVICE-VERIFIED",
                ),
            ),
        )
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertTrue(all(node["capabilities"] == () for node in snapshot.topology_fragment["nodes"]))
        self.assertEqual(
            snapshot.topology_fragment["producer"]["authority"],
            0,
        )

    def test_verified_serial_composes_without_materialization(self):
        """A verified serial may join another provider's hardware observation."""
        deye, _deye_store = publisher()
        deye.ingest_snapshot(
            1,
            (station(),),
            (
                inverter(
                    api_verified_serial="shared-serial",
                ),
            ),
            health=True,
        )
        ge = GECloudFragmentPublisher(
            "ge-cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        ge.ingest_discovery(
            1,
            (
                GECloudDeviceSnapshot(
                    serial="SHARED-SERIAL",
                    kind="battery-inverter",
                    online=True,
                ),
            ),
            health=True,
        )

        plan = compile_auto_config(
            (deye.read_snapshot(), ge.read_snapshot()),
        )
        inverter_nodes = tuple(node for node in plan.topology["nodes"] if node["kind"] == "inverter")

        self.assertEqual(len(inverter_nodes), 1)
        self.assertEqual(
            inverter_nodes[0]["id"],
            "identity:serial:SHARED-SERIAL",
        )
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertFalse(plan.materialization_readiness.ready)

    def test_input_shape_has_no_secret_endpoint_feature_or_control_fields(self):
        """The adapter cannot accept auth, endpoint, TOU, or feature claims."""
        station_fields = {field.name for field in dataclasses.fields(DeyeStationObservation)}
        inverter_fields = {field.name for field in dataclasses.fields(DeyeInverterObservation)}
        forbidden = {
            "access_token",
            "app_secret",
            "auth",
            "capabilities",
            "endpoint",
            "features",
            "password",
            "schedule",
            "tou",
        }

        self.assertTrue(station_fields.isdisjoint(forbidden))
        self.assertTrue(inverter_fields.isdisjoint(forbidden))
        self.assertEqual(
            inverter_fields,
            {
                "api_verified_serial",
                "device_id",
                "model",
                "online",
                "station_id",
            },
        )

    def test_membership_conflict_fails_before_publication(self):
        """An inverter cannot be attached to an undiscovered station."""
        adapter, state_store = publisher()

        with self.assertRaisesRegex(
            ValueError,
            "^complete inventory device references an unknown site_id$",
        ):
            adapter.ingest_snapshot(
                1,
                (station(),),
                (inverter(station_id="OTHER"),),
            )
        with self.assertRaises(FragmentAdapterConflict):
            adapter.ingest_snapshot(
                1,
                (station(),),
                (
                    inverter(
                        "DEVICE-A",
                        api_verified_serial="SHARED",
                    ),
                    inverter(
                        "DEVICE-B",
                        api_verified_serial="shared",
                    ),
                ),
            )
        self.assertEqual(state_store.writes, 0)

    def test_maximum_station_and_inverter_cardinality_is_accepted(self):
        """Deye permits the same bounded complete inventory limits."""
        station_adapter, station_store = publisher()
        stations = (station("STATION-{}".format(index)) for index in range(MAX_COMPLETE_INVENTORY_SITES))
        self.assertTrue(station_adapter.ingest_snapshot(1, stations, ()))
        self.assertEqual(station_store.writes, 1)

        inverter_adapter, inverter_store = publisher()
        inverters = (
            inverter(
                "DEVICE-{}".format(index),
                api_verified_serial=None,
            )
            for index in range(MAX_COMPLETE_INVENTORY_DEVICES)
        )
        self.assertTrue(
            inverter_adapter.ingest_snapshot(
                1,
                (station(),),
                inverters,
            )
        )
        self.assertEqual(inverter_store.writes, 1)

    def test_maximum_plus_one_is_rejected_after_bounded_consumption(self):
        """Finite oversized Deye inputs consume only max+1 and never write."""
        cases = (
            (
                "stations",
                MAX_COMPLETE_INVENTORY_SITES,
                lambda counter: (
                    (
                        counter.append(None) or station("STATION-{}".format(index))
                        for index in range(
                            MAX_COMPLETE_INVENTORY_SITES + 5,
                        )
                    ),
                    (),
                ),
            ),
            (
                "inverters",
                MAX_COMPLETE_INVENTORY_DEVICES,
                lambda counter: (
                    (station(),),
                    (
                        counter.append(None)
                        or inverter(
                            "DEVICE-{}".format(index),
                            api_verified_serial=None,
                        )
                        for index in range(
                            MAX_COMPLETE_INVENTORY_DEVICES + 5,
                        )
                    ),
                ),
            ),
        )
        for label, maximum, inventory in cases:
            with self.subTest(label=label):
                adapter, state_store = publisher()
                counter = []
                stations, inverters = inventory(counter)
                with self.assertRaisesRegex(
                    ValueError,
                    "^{} must contain at most {} observations$".format(
                        label,
                        maximum,
                    ),
                ):
                    adapter.ingest_snapshot(
                        1,
                        stations,
                        inverters,
                    )
                self.assertEqual(len(counter), maximum + 1)
                self.assertEqual(state_store.writes, 0)

    def test_infinite_inputs_terminate_after_maximum_plus_one(self):
        """Infinite Deye discovery streams are bounded before publication."""

        def infinite_observations(observation, counter):
            while True:
                counter.append(None)
                yield observation

        cases = (
            (
                MAX_COMPLETE_INVENTORY_SITES,
                lambda counter: (
                    infinite_observations(station(), counter),
                    (),
                ),
            ),
            (
                MAX_COMPLETE_INVENTORY_DEVICES,
                lambda counter: (
                    (station(),),
                    infinite_observations(
                        inverter(api_verified_serial=None),
                        counter,
                    ),
                ),
            ),
        )
        for maximum, inventory in cases:
            with self.subTest(maximum=maximum):
                adapter, state_store = publisher()
                counter = []
                stations, inverters = inventory(counter)
                with self.assertRaises(ValueError):
                    adapter.ingest_snapshot(
                        1,
                        stations,
                        inverters,
                    )
                self.assertEqual(len(counter), maximum + 1)
                self.assertEqual(state_store.writes, 0)

    def test_iterator_type_errors_do_not_retain_provider_values(self):
        """Deye source faults cannot survive in public error objects."""
        raw_value = "provider-secret-value"

        class FaultingIterable:
            def __iter__(self):
                raise TypeError(raw_value)

        cases = (
            lambda adapter: adapter.ingest_snapshot(
                1,
                FaultingIterable(),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (station(),),
                FaultingIterable(),
            ),
        )
        for action in cases:
            with self.subTest(action=action):
                adapter, state_store = publisher()
                assert_value_free_validation_error(
                    self,
                    lambda: action(adapter),
                    raw_value,
                )
                self.assertEqual(state_store.writes, 0)

    def test_non_type_iterator_errors_do_not_retain_provider_values(self):
        """Deye replaces acquisition and mid-stream source faults."""
        raw_value = "provider-secret-value"

        class FaultOnIter:
            def __iter__(self):
                raise RuntimeError(raw_value)

        class FaultOnNext:
            def __init__(self, first):
                self._first = first

            def __iter__(self):
                return self

            def __next__(self):
                if self._first is not None:
                    first = self._first
                    self._first = None
                    return first
                raise ValueError(raw_value)

        cases = (
            lambda adapter: adapter.ingest_snapshot(
                1,
                FaultOnIter(),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (station(),),
                FaultOnIter(),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                FaultOnNext(station()),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (station(),),
                FaultOnNext(inverter()),
            ),
        )
        for action in cases:
            with self.subTest(action=action):
                adapter, state_store = publisher()
                assert_value_free_validation_error(
                    self,
                    lambda: action(adapter),
                    raw_value,
                )
                self.assertEqual(state_store.writes, 0)

    def test_liveness_and_whole_provider_tombstone_delegate_safely(self):
        """Deye wrapper preserves generic liveness/removal guarantees."""
        adapter, state_store = publisher()
        adapter.ingest_snapshot(
            4,
            (station(),),
            (inverter(),),
            health=True,
        )

        self.assertTrue(adapter.set_liveness(False))
        self.assertEqual(
            adapter.read_snapshot().health,
            ProviderHealth.OFFLINE,
        )
        self.assertEqual(adapter.discovery_generation, 4)
        self.assertTrue(adapter.remove())
        self.assertTrue(adapter.read_state().removed)
        self.assertEqual(state_store.writes, 3)


if __name__ == "__main__":
    unittest.main()

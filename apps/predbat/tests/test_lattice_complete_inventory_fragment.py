"""Tests for the generic complete-inventory Lattice publisher."""

# cspell:ignore autoconfig

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileStatus,
    ProviderHealth,
    compile_auto_config,
)
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_complete_inventory_fragment import (  # noqa: E402
    CompleteInventoryFragmentPublisher,
    InventoryDeviceObservation,
    InventorySiteObservation,
    MAX_COMPLETE_INVENTORY_DEVICES,
    MAX_COMPLETE_INVENTORY_SITES,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
)


def site(site_id="SITE-A", online=True):
    """Build one provider-local site observation."""
    return InventorySiteObservation(site_id=site_id, online=online)


def device(
    device_id="DEVICE-A",
    site_id="SITE-A",
    kind="inverter",
    model="Model A",
    online=True,
    verified_hardware_serial=None,
):
    """Build one provider-local device observation."""
    return InventoryDeviceObservation(
        device_id=device_id,
        site_id=site_id,
        kind=kind,
        model=model,
        online=online,
        verified_hardware_serial=verified_hardware_serial,
    )


def publisher(enabled=True, state_store=None):
    """Build one generic publisher and durable store."""
    state_store = state_store or InMemoryFragmentAdapterStateStore()
    return (
        CompleteInventoryFragmentPublisher(
            "inventory-provider",
            "Inventory Provider",
            state_store,
            enabled=enabled,
        ),
        state_store,
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


class TestInventoryObservations(unittest.TestCase):
    """Inventory inputs are closed, bounded, and provider-local."""

    def test_site_and_device_identifiers_are_canonical(self):
        """Integer site IDs normalize without becoming strong identities."""
        observed_site = site(1234, online=None)
        observed_device = device(
            device_id="dev_1",
            site_id=1234,
            kind="energy_management_system",
            model=None,
            online=None,
        )

        self.assertEqual(observed_site.site_id, "1234")
        self.assertEqual(observed_device.site_id, "1234")
        self.assertEqual(
            observed_device.kind,
            "energy-management-system",
        )
        self.assertIsNone(observed_device.verified_hardware_serial)

    def test_metadata_rejects_controls_and_unbounded_values(self):
        """Control characters and unbounded metadata cannot be serialized."""
        with self.assertRaisesRegex(ValueError, "provider identifier"):
            site("site with spaces")
        with self.assertRaisesRegex(ValueError, "control characters"):
            device(model="model\0secret")
        with self.assertRaisesRegex(ValueError, "at most 128"):
            device(model="m" * 129)
        with self.assertRaisesRegex(ValueError, "kind must be"):
            device(kind="tou-schedule-controller")
        with self.assertRaisesRegex(ValueError, "online"):
            device(online=1)

    def test_model_rejects_unicode_controls_and_surrogates(self):
        """Invisible controls and malformed Unicode cannot enter metadata."""
        invalid_models = (
            ("bidi override", "Model\u202eA"),
            ("zero-width", "Model\u200bA"),
            ("unpaired surrogate", "Model\ud800A"),
        )
        for label, invalid_model in invalid_models:
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    ValueError,
                    "control characters or malformed Unicode",
                ):
                    device(model=invalid_model)

    def test_producer_name_rejects_unicode_controls_and_surrogates(self):
        """Publisher provenance cannot contain invisible or malformed text."""
        invalid_names = (
            ("bidi override", "Inventory\u202eProvider"),
            ("zero-width", "Inventory\u200bProvider"),
            ("unpaired surrogate", "Inventory\ud800Provider"),
        )
        for label, invalid_name in invalid_names:
            with self.subTest(label=label):
                with self.assertRaisesRegex(
                    ValueError,
                    "control characters or malformed Unicode",
                ):
                    CompleteInventoryFragmentPublisher(
                        "inventory-provider",
                        invalid_name,
                        InMemoryFragmentAdapterStateStore(),
                    )

    def test_valid_unicode_metadata_is_preserved_exactly(self):
        """Valid provider evidence is not silently Unicode-normalized."""
        decomposed_model = "Cafe\u0301 Model"

        self.assertEqual(device(model=decomposed_model).model, decomposed_model)

    def test_provider_id_is_bounded_and_canonical(self):
        """Provider qualification cannot contain paths or controls."""
        store = InMemoryFragmentAdapterStateStore()
        with self.assertRaisesRegex(ValueError, "lowercase"):
            CompleteInventoryFragmentPublisher(
                "Inventory Provider",
                "Inventory",
                store,
            )
        with self.assertRaisesRegex(ValueError, "control"):
            CompleteInventoryFragmentPublisher(
                "inventory\0provider",
                "Inventory",
                store,
            )


class TestCompleteInventoryFragmentPublisher(unittest.TestCase):
    """Complete inventories replace one durable provider-local view."""

    def test_default_off_is_unseeded_unregistered_and_write_free(self):
        """Disabled input is not validated, persisted, or discoverable."""
        adapter, state_store = publisher(enabled=False)

        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(
            adapter.ingest_inventory(
                -1,
                (object(),),
                (object(),),
            )
        )
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_multi_site_inventory_is_deterministic_reference_only(self):
        """Order cannot affect topology or manufacture control authority."""
        adapter, state_store = publisher()
        sites = [site("SITE-B", online=None), site("SITE-A")]
        devices = [
            device(
                "METER-B",
                "SITE-B",
                kind="meter",
                model=None,
                online=None,
            ),
            device(
                "INV-A",
                "SITE-A",
                verified_hardware_serial="serial-a",
            ),
        ]

        self.assertTrue(
            adapter.ingest_inventory(
                7,
                reversed(sites),
                reversed(devices),
                health=True,
            )
        )
        sites.append(site("MUTATED"))
        devices.append(device("MUTATED"))
        snapshot = adapter.read_snapshot()
        topology = snapshot.topology_fragment

        self.assertIs(adapter.lattice_fragment_adapter(), adapter)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(adapter.discovery_generation, 7)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(
            tuple(node["id"] for node in topology["nodes"]),
            (
                "inventory:inventory-provider:site:SITE-A",
                "inventory:inventory-provider:site:SITE-B",
                "inventory:inventory-provider:device:INV-A",
                "inventory:inventory-provider:device:METER-B",
            ),
        )
        self.assertEqual(
            tuple(
                (
                    relationship["from"],
                    relationship["to"],
                    relationship["type"],
                )
                for relationship in topology["relationships"]
            ),
            (
                (
                    "inventory:inventory-provider:site:SITE-A",
                    "inventory:inventory-provider:device:INV-A",
                    "contains",
                ),
                (
                    "inventory:inventory-provider:site:SITE-B",
                    "inventory:inventory-provider:device:METER-B",
                    "contains",
                ),
            ),
        )
        self.assertEqual(topology["producer"]["authority"], 0)
        self.assertTrue(all(node["capabilities"] == () for node in topology["nodes"]))
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertEqual(
            tuple((alias.kind, alias.value) for alias in snapshot.identity_aliases),
            (("serial", "SERIAL-A"),),
        )

        plan = compile_auto_config((snapshot,))
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertFalse(plan.materialization_readiness.ready)

    def test_newer_complete_inventory_removes_omitted_observations(self):
        """A disappearing device is absent, not retained from an older view."""
        adapter, _state_store = publisher()
        adapter.ingest_inventory(
            1,
            (site("SITE-A"), site("SITE-B")),
            (
                device("INV-A", "SITE-A", verified_hardware_serial="SER-A"),
                device("INV-B", "SITE-B", verified_hardware_serial="SER-B"),
            ),
            health=True,
        )
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(
            registry.discover((adapter,)),
            ("inventory-provider",),
        )
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )
        self.assertEqual(compiler.drain().status, CompileStatus.FRESH)

        self.assertTrue(
            adapter.ingest_inventory(
                2,
                (site("SITE-A"),),
                (
                    device(
                        "INV-A",
                        "SITE-A",
                        verified_hardware_serial="SER-A",
                    ),
                ),
            )
        )
        snapshot = adapter.read_snapshot()
        serialized = repr(snapshot)

        self.assertNotIn("INV-B", serialized)
        self.assertNotIn("SER-B", serialized)
        self.assertNotIn("SITE-B", serialized)
        self.assertEqual(adapter.generation, 2)
        self.assertEqual(adapter.discovery_generation, 2)
        compiled = compiler.drain()
        self.assertEqual(compiled.status, CompileStatus.FRESH)
        self.assertEqual(
            compiled.publication.provider_generations,
            (("inventory-provider", 2),),
        )

    def test_empty_complete_inventory_removes_every_observation(self):
        """An explicit empty view is distinct from provider removal."""
        adapter, _state_store = publisher()
        adapter.ingest_inventory(
            1,
            (site(),),
            (device(),),
            health=True,
        )

        self.assertTrue(adapter.ingest_inventory(2, (), ()))
        snapshot = adapter.read_snapshot()
        self.assertEqual(snapshot.topology_fragment["nodes"], ())
        self.assertNotIn(
            "relationships",
            snapshot.topology_fragment,
        )
        self.assertEqual(snapshot.aliases, ())
        self.assertEqual(snapshot.identity_aliases, ())
        self.assertFalse(adapter.read_state().removed)

    def test_generation_replay_regression_and_conflict(self):
        """Replay is inert; stale input is ignored; mutation fails closed."""
        adapter, state_store = publisher()
        original_sites = (site(),)
        original_devices = (device(),)
        adapter.ingest_inventory(
            5,
            original_sites,
            original_devices,
            health=True,
        )
        before = adapter.read_state()

        self.assertFalse(
            adapter.ingest_inventory(
                5,
                reversed(original_sites),
                reversed(original_devices),
            )
        )
        self.assertFalse(
            adapter.ingest_inventory(
                4,
                (site(online=False),),
                (device(model="Stale"),),
                health=False,
            )
        )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "reused complete-inventory discovery generation 5",
        ):
            adapter.ingest_inventory(
                5,
                original_sites,
                (device(model="Different"),),
            )
        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 1)

    def test_collisions_and_unknown_membership_fail_before_write(self):
        """Ambiguous local and strong identities cannot become durable."""
        adapter, state_store = publisher()

        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "^complete inventory contains a duplicate site_id$",
        ):
            adapter.ingest_inventory(
                1,
                (site(), site()),
                (),
            )
        with self.assertRaisesRegex(
            ValueError,
            "^complete inventory device references an unknown site_id$",
        ):
            adapter.ingest_inventory(
                1,
                (site(),),
                (device(site_id="OTHER"),),
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "^complete inventory contains a duplicate device_id$",
        ):
            adapter.ingest_inventory(
                1,
                (site("SITE-A"), site("SITE-B")),
                (
                    device("DEVICE", "SITE-A"),
                    device("DEVICE", "SITE-B"),
                ),
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "^complete inventory contains duplicate " "verified_hardware_serial ownership$",
        ):
            adapter.ingest_inventory(
                1,
                (site(),),
                (
                    device(
                        "DEVICE-A",
                        verified_hardware_serial="shared",
                    ),
                    device(
                        "DEVICE-B",
                        verified_hardware_serial="SHARED",
                    ),
                ),
            )
        self.assertEqual(state_store.writes, 0)

    def test_maximum_inventory_cardinality_is_accepted(self):
        """The documented limits still permit unusually large inventories."""
        adapter, state_store = publisher()
        sites = tuple(site("SITE-{}".format(index)) for index in range(MAX_COMPLETE_INVENTORY_SITES))
        devices = tuple(
            device(
                "DEVICE-{}".format(index),
                "SITE-0",
            )
            for index in range(MAX_COMPLETE_INVENTORY_DEVICES)
        )

        self.assertTrue(adapter.ingest_inventory(1, sites, devices))
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(
            len(adapter.read_snapshot().topology_fragment["nodes"]),
            MAX_COMPLETE_INVENTORY_SITES + MAX_COMPLETE_INVENTORY_DEVICES,
        )

    def test_maximum_plus_one_is_rejected_before_publication(self):
        """A finite oversized inventory cannot publish or persist."""
        cases = (
            (
                "sites",
                (site("SITE-{}".format(index)) for index in range(MAX_COMPLETE_INVENTORY_SITES + 1)),
                (),
                MAX_COMPLETE_INVENTORY_SITES,
            ),
            (
                "devices",
                (site(),),
                (device("DEVICE-{}".format(index)) for index in range(MAX_COMPLETE_INVENTORY_DEVICES + 1)),
                MAX_COMPLETE_INVENTORY_DEVICES,
            ),
        )
        for label, sites, devices, maximum in cases:
            with self.subTest(label=label):
                adapter, state_store = publisher()
                with self.assertRaisesRegex(
                    ValueError,
                    "^{} must contain at most {} observations$".format(
                        label,
                        maximum,
                    ),
                ):
                    adapter.ingest_inventory(1, sites, devices)
                self.assertEqual(state_store.writes, 0)
                self.assertIsNone(adapter.lattice_fragment_adapter())

    def test_infinite_inventory_is_bounded_before_validation(self):
        """Only max+1 observations are consumed from an infinite source."""

        def infinite_observations(observation, counter):
            while True:
                counter.append(None)
                yield observation

        cases = (
            (
                "sites",
                MAX_COMPLETE_INVENTORY_SITES,
                lambda counter: (
                    infinite_observations(site(), counter),
                    (),
                ),
            ),
            (
                "devices",
                MAX_COMPLETE_INVENTORY_DEVICES,
                lambda counter: (
                    (site(),),
                    infinite_observations(device(), counter),
                ),
            ),
        )
        for label, maximum, inventory in cases:
            with self.subTest(label=label):
                adapter, state_store = publisher()
                counter = []
                sites, devices = inventory(counter)
                with self.assertRaisesRegex(
                    ValueError,
                    "^{} must contain at most {} observations$".format(
                        label,
                        maximum,
                    ),
                ):
                    adapter.ingest_inventory(1, sites, devices)
                self.assertEqual(len(counter), maximum + 1)
                self.assertEqual(state_store.writes, 0)
                self.assertIsNone(adapter.lattice_fragment_adapter())

    def test_string_and_mapping_inventories_are_rejected_without_values(self):
        """Ambiguous iterable containers fail with stable value-free errors."""
        cases = (
            ("sites", "SITE-SECRET", (), "sites"),
            ("sites", {"SITE-SECRET": site()}, (), "sites"),
            ("devices", (site(),), "DEVICE-SECRET", "devices"),
            (
                "devices",
                (site(),),
                {"DEVICE-SECRET": device()},
                "devices",
            ),
        )
        for label, sites, devices, expected_name in cases:
            with self.subTest(label=label, container=type(sites).__name__):
                adapter, state_store = publisher()
                with self.assertRaisesRegex(
                    ValueError,
                    "^{} must be an iterable of Inventory".format(
                        expected_name,
                    ),
                ) as raised:
                    adapter.ingest_inventory(1, sites, devices)
                self.assertNotIn("SECRET", str(raised.exception))
                self.assertEqual(state_store.writes, 0)

    def test_iterator_type_errors_do_not_retain_provider_values(self):
        """Malformed iterable faults cannot survive in public error objects."""
        raw_value = "provider-secret-value"

        class FaultingIterable:
            def __iter__(self):
                raise TypeError(raw_value)

        cases = (
            lambda adapter: adapter.ingest_inventory(
                1,
                FaultingIterable(),
                (),
            ),
            lambda adapter: adapter.ingest_inventory(
                1,
                (site(),),
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
        """Acquisition and mid-stream faults are replaced outside handlers."""
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
            lambda adapter: adapter.ingest_inventory(
                1,
                FaultOnIter(),
                (),
            ),
            lambda adapter: adapter.ingest_inventory(
                1,
                (site(),),
                FaultOnIter(),
            ),
            lambda adapter: adapter.ingest_inventory(
                1,
                FaultOnNext(site()),
                (),
            ),
            lambda adapter: adapter.ingest_inventory(
                1,
                (site(),),
                FaultOnNext(device()),
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

    def test_liveness_and_feedback_invalidate_with_fresh_reads(self):
        """Health advances while generated feedback persists nothing."""
        adapter, state_store = publisher()
        adapter.ingest_inventory(
            3,
            (site(),),
            (device(),),
            health=True,
        )
        registry = FragmentAdapterRegistry(enabled=True)
        registry.discover((adapter,))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )
        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)

        self.assertTrue(adapter.set_liveness(None))
        degraded = compiler.drain()
        self.assertEqual(degraded.status, CompileStatus.DEGRADED)
        self.assertEqual(adapter.discovery_generation, 3)
        self.assertEqual(adapter.generation, 2)

        before = adapter.read_state()
        writes = state_store.writes
        self.assertFalse(
            adapter.ingest_inventory(
                4,
                (site(),),
                (device(model="Feedback"),),
                feedback_token=degraded.publication.feedback_token,
            )
        )
        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, writes)
        self.assertFalse(compiler.drain().pending)

    def test_removal_is_durable_invalidating_and_irreversible(self):
        """Whole-provider removal survives restart and blocks resurrection."""
        adapter, state_store = publisher()
        adapter.ingest_inventory(
            2,
            (site(),),
            (device(),),
            health=True,
        )
        invalidations = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: (
                invalidations.append(
                    (provider_id, generation, reason),
                )
            )
        )

        self.assertTrue(adapter.remove())
        self.assertFalse(adapter.remove())
        removed = adapter.read_state()
        self.assertTrue(removed.removed)
        self.assertEqual(
            removed.snapshot.health,
            ProviderHealth.OFFLINE,
        )
        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()
        with self.assertRaisesRegex(
            FragmentAdapterRemoved,
            "cannot re-enrol",
        ):
            adapter.ingest_inventory(99, (), ())
        self.assertEqual(
            invalidations[-1],
            (
                "inventory-provider",
                2,
                "complete inventory integration removed",
            ),
        )

        restarted = CompleteInventoryFragmentPublisher(
            "inventory-provider",
            "Inventory Provider",
            state_store,
            enabled=True,
        )
        self.assertTrue(restarted.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.ingest_inventory(100, (), ())


if __name__ == "__main__":
    unittest.main()

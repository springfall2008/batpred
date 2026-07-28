"""Tests for the pure Enphase complete-inventory normalizer."""

# cspell:ignore autoconfig egateway

import dataclasses
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileStatus,
)
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_complete_inventory_fragment import (  # noqa: E402
    MAX_COMPLETE_INVENTORY_DEVICES,
    MAX_COMPLETE_INVENTORY_SITES,
)
from lattice_enphase_inventory_fragment import (  # noqa: E402
    EnphaseDeviceObservation,
    EnphaseInventoryFragmentPublisher,
    EnphaseSiteObservation,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
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


def site(site_id="SITE-A", online=True):
    """Build one explicit Enphase site observation."""
    return EnphaseSiteObservation(
        site_id=site_id,
        online=online,
    )


def device(
    device_id="BATTERY-A",
    site_id="SITE-A",
    kind="battery",
    api_verified_serial="SERIAL-A",
    model="IQ Battery",
    online=True,
):
    """Build one explicit Enphase device observation."""
    return EnphaseDeviceObservation(
        device_id=device_id,
        site_id=site_id,
        kind=kind,
        api_verified_serial=api_verified_serial,
        model=model,
        online=online,
    )


def publisher(enabled=True, state_store=None):
    """Build one Enphase publisher and durable store."""
    state_store = state_store or InMemoryFragmentAdapterStateStore()
    return (
        EnphaseInventoryFragmentPublisher(
            "enphase-cloud",
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestEnphaseInventoryFragmentPublisher(unittest.TestCase):
    """Enphase inventory remains complete, pure, and authority-free."""

    def test_default_off_does_not_import_live_enphase_or_write(self):
        """Construction cannot touch live auth, discovery, or control code."""
        module_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), ".."),
        )
        isolated = subprocess.run(
            (
                sys.executable,
                "-c",
                "import sys; import lattice_enphase_inventory_fragment; " "assert 'enphase' not in sys.modules",
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
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_complete_inventory_replaces_omissions_and_empty_is_explicit(self):
        """Newer complete views remove absent devices and may become empty."""
        adapter, state_store = publisher()
        adapter.ingest_snapshot(
            1,
            (site("SITE-A"), site("SITE-B")),
            (
                device("GW-A", "SITE-A", "gateway", "GW-SERIAL"),
                device("INV-A", "SITE-A", "inverter", "INV-SERIAL"),
                device("BAT-A", "SITE-A", "battery", "BAT-SERIAL"),
                device("METER-B", "SITE-B", "meter", None),
            ),
            health=True,
        )

        self.assertTrue(
            adapter.ingest_snapshot(
                2,
                (site("SITE-A"),),
                (
                    device(
                        "BAT-A",
                        "SITE-A",
                        "battery",
                        "BAT-SERIAL",
                    ),
                ),
            )
        )
        second = repr(adapter.read_snapshot())
        self.assertNotIn("SITE-B", second)
        self.assertNotIn("METER-B", second)
        self.assertNotIn("GW-A", second)
        self.assertNotIn("INV-A", second)

        self.assertTrue(adapter.ingest_snapshot(3, (), ()))
        empty = adapter.read_snapshot()
        self.assertEqual(empty.topology_fragment["nodes"], ())
        self.assertNotIn("relationships", empty.topology_fragment)
        self.assertEqual(empty.aliases, ())
        self.assertEqual(empty.identity_aliases, ())
        self.assertFalse(adapter.read_state().removed)
        self.assertEqual(state_store.writes, 3)

    def test_only_api_verified_serial_is_strong_identity(self):
        """Provider IDs, models, and serial-like IDs cannot correlate nodes."""
        adapter, _state_store = publisher()
        adapter.ingest_snapshot(
            1,
            (site(),),
            (
                device(
                    device_id="LOOKS-LIKE-A-SERIAL",
                    api_verified_serial=None,
                    model="SERIAL-SHAPED-MODEL",
                ),
                device(
                    device_id="VERIFIED-DEVICE",
                    kind="inverter",
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
                    "inventory:enphase-cloud:device:VERIFIED-DEVICE",
                ),
            ),
        )
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertEqual(
            snapshot.topology_fragment["producer"]["authority"],
            0,
        )
        self.assertTrue(all(node["capabilities"] == () for node in snapshot.topology_fragment["nodes"]))

    def test_input_shape_excludes_live_cloud_and_control_fields(self):
        """The isolated boundary cannot accept secrets, endpoints, or writes."""
        site_fields = {field.name for field in dataclasses.fields(EnphaseSiteObservation)}
        device_fields = {field.name for field in dataclasses.fields(EnphaseDeviceObservation)}
        forbidden = {
            "access_token",
            "auth",
            "capabilities",
            "cookie",
            "endpoint",
            "password",
            "profile",
            "reserve",
            "schedule",
            "token",
            "username",
            "xsrf_token",
        }

        self.assertTrue(site_fields.isdisjoint(forbidden))
        self.assertTrue(device_fields.isdisjoint(forbidden))
        self.assertEqual(
            device_fields,
            {
                "api_verified_serial",
                "device_id",
                "kind",
                "model",
                "online",
                "site_id",
            },
        )

    def test_device_kind_is_closed_to_verified_inventory_classes(self):
        """No schedule/controller kind can manufacture authority."""
        accepted = ("gateway", "inverter", "battery", "meter")
        for kind in accepted:
            with self.subTest(kind=kind):
                self.assertEqual(device(kind=kind).kind, kind)
        with self.assertRaisesRegex(
            ValueError,
            "^kind must be one of battery, gateway, inverter, meter$",
        ):
            device(kind="schedule-controller")

    def test_collisions_and_unknown_membership_fail_before_write(self):
        """Ambiguous site, device, and serial ownership fail closed."""
        cases = (
            lambda adapter: adapter.ingest_snapshot(
                1,
                (site(), site()),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (site(),),
                (device(site_id="OTHER"),),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (site(),),
                (
                    device("A", api_verified_serial=None),
                    device("A", api_verified_serial=None),
                ),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                (site(),),
                (
                    device("A", api_verified_serial="SERIAL"),
                    device("B", api_verified_serial="serial"),
                ),
            ),
        )
        for action in cases:
            with self.subTest(action=action):
                adapter, state_store = publisher()
                with self.assertRaises((ValueError, FragmentAdapterConflict)):
                    action(adapter)
                self.assertEqual(state_store.writes, 0)

    def test_unicode_and_secret_values_are_value_free_at_boundary(self):
        """Malformed metadata and secret-like identifiers never escape."""
        values_and_actions = (
            (
                "secret\ud800model",
                lambda value: device(model=value),
            ),
            (
                "secret\u202egateway",
                lambda value: device(model=value),
            ),
            (
                "secret token with spaces",
                lambda value: device(device_id=value),
            ),
            (
                "secret\0serial",
                lambda value: device(api_verified_serial=value),
            ),
        )
        for raw_value, action in values_and_actions:
            with self.subTest(raw_value=repr(raw_value)):
                assert_value_free_validation_error(
                    self,
                    lambda: action(raw_value),
                    raw_value,
                )

    def test_maximum_plus_one_and_infinite_streams_are_bounded(self):
        """Infinite inputs terminate after consuming their limit plus one."""

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
                    infinite_observations(
                        device(api_verified_serial=None),
                        counter,
                    ),
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
                    adapter.ingest_snapshot(1, sites, devices)
                self.assertEqual(len(counter), maximum + 1)
                self.assertEqual(state_store.writes, 0)

    def test_faulting_streams_are_replaced_with_value_free_errors(self):
        """Acquisition and mid-stream source faults cannot retain secrets."""
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
                (site(),),
                FaultOnIter(),
            ),
            lambda adapter: adapter.ingest_snapshot(
                1,
                FaultOnNext(site()),
                (),
            ),
            lambda adapter: adapter.ingest_snapshot(
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

    def test_post_init_normalization_cannot_heal_enphase_observations(self):
        """Every outer field must already have its exact canonical value."""

        def site_id_whitespace():
            candidate = site()
            object.__setattr__(
                candidate,
                "site_id",
                " SITE-A ",
            )
            return (candidate,), (), " SITE-A "

        def site_id_type():
            candidate = site("1")
            object.__setattr__(
                candidate,
                "site_id",
                1,
            )
            return (candidate,), (), "1"

        def site_online_type():
            candidate = site()
            object.__setattr__(
                candidate,
                "online",
                1,
            )
            return (candidate,), (), "1"

        def device_id_whitespace():
            candidate = device()
            object.__setattr__(
                candidate,
                "device_id",
                " BATTERY-A ",
            )
            return (site(),), (candidate,), " BATTERY-A "

        def device_site_id_whitespace():
            candidate = device()
            object.__setattr__(
                candidate,
                "site_id",
                " SITE-A ",
            )
            return (site(),), (candidate,), " SITE-A "

        def device_kind_case():
            candidate = device()
            object.__setattr__(
                candidate,
                "kind",
                "BATTERY",
            )
            return (site(),), (candidate,), "BATTERY"

        def device_serial_case():
            candidate = device()
            object.__setattr__(
                candidate,
                "api_verified_serial",
                "serial-a",
            )
            return (site(),), (candidate,), "serial-a"

        def device_model_whitespace():
            candidate = device()
            object.__setattr__(
                candidate,
                "model",
                " IQ Battery ",
            )
            return (site(),), (candidate,), " IQ Battery "

        def device_online_type():
            candidate = device()
            object.__setattr__(
                candidate,
                "online",
                1,
            )
            return (site(),), (candidate,), "1"

        for build_case in (
            site_id_whitespace,
            site_id_type,
            site_online_type,
            device_id_whitespace,
            device_site_id_whitespace,
            device_kind_case,
            device_serial_case,
            device_model_whitespace,
            device_online_type,
        ):
            with self.subTest(build_case=build_case):
                sites, devices, raw_value = build_case()
                adapter, state_store = publisher()
                invalidations = []
                adapter.subscribe_invalidation(
                    lambda *event: invalidations.append(event),
                )
                assert_value_free_validation_error(
                    self,
                    lambda: adapter.ingest_snapshot(
                        1,
                        sites,
                        devices,
                    ),
                    raw_value,
                )
                self.assertEqual(state_store.writes, 0)
                self.assertEqual(invalidations, [])

    def test_instance_shadowed_converters_are_never_called(self):
        """Conversion uses class methods only after exact reconstruction."""
        enphase_site = site()
        enphase_device = device(
            api_verified_serial=None,
        )
        callbacks = []

        def shadowed_site():
            callbacks.append("site")
            raise AssertionError("instance site callback executed")

        def shadowed_device():
            callbacks.append("device")
            raise AssertionError("instance device callback executed")

        object.__setattr__(
            enphase_site,
            "inventory_site",
            shadowed_site,
        )
        object.__setattr__(
            enphase_device,
            "inventory_device",
            shadowed_device,
        )

        adapter, state_store = publisher()
        invalidations = []
        adapter.subscribe_invalidation(
            lambda *event: invalidations.append(event),
        )
        self.assertTrue(
            adapter.ingest_snapshot(
                1,
                (enphase_site,),
                (enphase_device,),
            )
        )
        snapshot = adapter.read_snapshot()

        self.assertEqual(callbacks, [])
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(len(invalidations), 1)
        self.assertEqual(snapshot.identity_aliases, ())
        self.assertEqual(
            tuple(node["id"] for node in snapshot.topology_fragment["nodes"]),
            (
                "inventory:enphase-cloud:site:SITE-A",
                "inventory:enphase-cloud:device:BATTERY-A",
            ),
        )

    def test_liveness_feedback_restart_and_tombstone_are_inherited(self):
        """The wrapper preserves invalidation, replay, and removal semantics."""
        adapter, state_store = publisher()
        adapter.ingest_snapshot(
            4,
            (site(),),
            (device(),),
            health=True,
        )
        registry = FragmentAdapterRegistry(enabled=True)
        registry.discover((adapter,))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )
        self.assertEqual(compiler.drain().status, CompileStatus.FRESH)

        self.assertTrue(adapter.set_liveness(None))
        degraded = compiler.drain()
        self.assertEqual(degraded.status, CompileStatus.DEGRADED)
        self.assertEqual(adapter.discovery_generation, 4)

        before_feedback = adapter.read_state()
        writes = state_store.writes
        self.assertFalse(
            adapter.ingest_snapshot(
                5,
                (site(),),
                (device(model="Feedback"),),
                feedback_token=degraded.publication.feedback_token,
            )
        )
        self.assertEqual(adapter.read_state(), before_feedback)
        self.assertEqual(state_store.writes, writes)

        restarted = EnphaseInventoryFragmentPublisher(
            "enphase-cloud",
            state_store,
            enabled=True,
        )
        self.assertEqual(restarted.read_state(), before_feedback)
        self.assertTrue(restarted.remove())
        self.assertTrue(restarted.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()

        removed_restart = EnphaseInventoryFragmentPublisher(
            "enphase-cloud",
            state_store,
            enabled=True,
        )
        with self.assertRaisesRegex(
            FragmentAdapterRemoved,
            "cannot re-enrol",
        ):
            removed_restart.ingest_snapshot(99, (), ())


if __name__ == "__main__":
    unittest.main()

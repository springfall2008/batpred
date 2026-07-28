"""Tests for generic durable Lattice fragment adapter discovery."""

# cspell:ignore autoconfig noncanonical

import os
import sys
import unittest
from itertools import repeat
from types import MappingProxyType

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileStatus,
    ProjectionCardinality,
    ProjectionRouting,
    ProjectionValueKind,
    ProviderAlias,
    ProviderConfigProjection,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderProjectionValue,
    ProviderRoleAssignment,
    ProviderSnapshot,
)
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_fragment_adapters import (  # noqa: E402
    DurableFragmentAdapter,
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    FragmentAdapterState,
    InMemoryFragmentAdapterStateStore,
    _semantic_fingerprint,
)
from tests.test_lattice_autoconfig import snapshot  # noqa: E402


def assert_value_free_error(
    test_case,
    expected_type,
    action,
    raw_value,
):
    """Assert one boundary error retains no source-controlled value."""
    try:
        action()
    except expected_type as error:
        test_case.assertIs(type(error), expected_type)
        test_case.assertNotIn(raw_value, str(error))
        test_case.assertNotIn(raw_value, repr(error))
        test_case.assertNotIn(raw_value, repr(error.args))
        test_case.assertNotIn(raw_value, error.args)
        test_case.assertIsNone(error.__cause__)
        test_case.assertIsNone(error.__context__)
    else:
        test_case.fail(
            "expected {}".format(expected_type.__name__),
        )


class HostileText(str):
    """String subclass whose scalar hooks expose a fake provider secret."""

    secret = "provider-secret-value"

    def _fail(self, *_args, **_kwargs):
        """Fail if validation invokes any subclass-controlled hook."""
        raise RuntimeError(self.secret)

    strip = _fail
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


class HostileMapping(dict):
    """Mapping whose traversal exposes a fake provider secret."""

    def items(self):
        """Fail if fingerprint validation traverses corrupt state."""
        raise RuntimeError(HostileText.secret)


class InfiniteItemsMapping(dict):
    """Mapping whose item iterator never terminates."""

    def items(self):
        """Return an infinite exact key/value stream."""
        return repeat(("item", 1))


class HostileProviderSnapshot(ProviderSnapshot):
    """Snapshot subclass whose field access exposes a fake provider secret."""

    def __getattribute__(self, _name):
        """Fail if publication reads a subclass-controlled field."""
        raise RuntimeError(HostileText.secret)


class FragmentAdapterStateSubclass(FragmentAdapterState):
    """Durable state subclass that must not dispatch validation."""


class HostileFragmentAdapterState(FragmentAdapterState):
    """State subclass whose field access exposes a fake provider secret."""

    def __getattribute__(self, _name):
        """Fail if registry validation reads a subclass-controlled field."""
        raise RuntimeError(HostileText.secret)


class HostileRegistryAdapter:
    """Structural adapter whose attribute access exposes a fake secret."""

    def __getattribute__(self, _name):
        """Fail if registry validation leaks an adapter hook."""
        raise RuntimeError(HostileText.secret)


class FakeHealth:
    """Enum-shaped offline health that must never become usable."""

    value = "offline"


class FakeAlias:
    """Alias-shaped value that is not an exact provider alias."""

    name = "fake"
    node_id = "GW-INV"
    roles = frozenset((AliasRole.REFERENCE,))


class FakeIdentityAlias:
    """Identity-shaped value that is not an exact assertion."""

    kind = "serial"
    value = "SERIAL-A"
    node_id = "GW-INV"


class FakeRoleAssignment:
    """Role-shaped value that is not an exact assignment."""

    role = AliasRole.PRIMARY
    group = "battery"
    index = 0
    node_id = "GW-INV"


class FakeProjectionValue:
    """Projection-value-shaped object that is not an exact value."""

    node_id = "GW-INV"
    kind = ProjectionValueKind.NONE
    value = None
    capability = None
    identity_kind = None
    identity_value = None
    access_path_id = None


class FakeConfigProjection:
    """Projection-shaped object that is not an exact projection."""

    argument = "fake_arg"
    role = AliasRole.PRIMARY
    group = "battery"
    routing = ProjectionRouting.LEAF
    cardinality = ProjectionCardinality.SCALAR
    values = (FakeProjectionValue(),)
    required = False
    transforms = ()


class MutableFragmentPublisher:
    """Valid structural adapter whose state reader can fail after registration."""

    def __init__(self, adapter):
        """Delegate every surface while allowing a test reader replacement."""
        self.provider_id = adapter.provider_id
        self._adapter = adapter
        self.state_reader = adapter.read_state

    def read_state(self):
        """Return the current test-selected state."""
        return self.state_reader()

    def read_snapshot(self):
        """Delegate immutable snapshot reads."""
        return self._adapter.read_snapshot()

    def subscribe_invalidation(self, listener):
        """Delegate compiler invalidation subscriptions."""
        return self._adapter.subscribe_invalidation(listener)


class FragmentComponent:
    """Brand-neutral component exposing only the common discovery method."""

    def __init__(self, adapter):
        """Store the adapter returned during discovery."""
        self.adapter = adapter
        self.calls = 0

    def lattice_fragment_adapter(self):
        """Return the component-owned fragment publisher."""
        self.calls += 1
        return self.adapter


class UnrelatedComponent:
    """Component without any Lattice fragment publisher surface."""


class RaisingLoadStore(InMemoryFragmentAdapterStateStore):
    """State store whose durable read is unavailable."""

    def load(self):
        """Raise one representative durable-store fault."""
        raise OSError("disk unavailable")


class RejectingStore(InMemoryFragmentAdapterStateStore):
    """State store rejecting every atomic fragment write."""

    def compare_and_store(self, expected, replacement):
        """Reject the candidate without changing durable state."""
        return False


class ToggleLoadStore(InMemoryFragmentAdapterStateStore):
    """State store that can fail after an initial successful compilation."""

    def __init__(self):
        """Create an initially available durable store."""
        super().__init__()
        self.fail_reads = False

    def load(self):
        """Return state until the test makes durable reads unavailable."""
        if self.fail_reads:
            raise OSError("durable fragment unavailable")
        return super().load()


def publisher(provider_id, generation=1, health=ProviderHealth.HEALTHY):
    """Build one seeded durable generic publisher."""
    state_store = InMemoryFragmentAdapterStateStore()
    adapter = DurableFragmentAdapter(provider_id, state_store)
    initial = snapshot(
        provider_id,
        generation=generation,
        node_id="{}-INV".format(provider_id.upper()),
        health=health,
    )
    adapter.publish(initial, "initial discovery")
    return adapter, state_store, initial


def advance(
    current,
    generation,
    health=None,
    node_id=None,
):
    """Build the next immutable test snapshot without copying frozen mappings."""
    if health is None:
        health = current.health
    if node_id is None:
        node_id = current.topology_fragment["nodes"][0]["id"]
    return snapshot(
        current.provider_id,
        generation=generation,
        node_id=node_id,
        health=health,
    )


def mutated_snapshot(field, value):
    """Return one exact snapshot with a post-init field mutation."""
    candidate = snapshot(
        "gateway",
        generation=1,
        node_id="GW-INV",
    )
    object.__setattr__(candidate, field, value)
    return candidate


def matched_corrupt_state(candidate, provider_id="gateway"):
    """Bind a corrupt snapshot to its matching semantic fingerprint."""
    state = object.__new__(FragmentAdapterState)
    object.__setattr__(
        state,
        "provider_id",
        provider_id,
    )
    object.__setattr__(state, "generation", 1)
    object.__setattr__(
        state,
        "semantic_fingerprint",
        _semantic_fingerprint(candidate),
    )
    object.__setattr__(state, "snapshot", candidate)
    object.__setattr__(state, "removed", False)
    return state


def compiled_registry(*adapters):
    """Discover generic components and create a durable compiler."""
    registry = FragmentAdapterRegistry(enabled=True)
    components = [
        UnrelatedComponent(),
    ] + [FragmentComponent(adapter) for adapter in adapters]
    discovered = registry.discover(components)
    compiled_store = InMemoryCompiledLatticeStateStore()
    compiler = registry.create_compiler(compiled_store)
    return registry, compiler, compiled_store, discovered


class TestDurableFragmentAdapter(unittest.TestCase):
    """Each integration owns a monotonic durable immutable fragment cursor."""

    def test_seed_and_fresh_reads_are_immutable_and_durable(self):
        """A published snapshot is detached and restored from its store."""
        adapter, state_store, initial = publisher("gateway")

        state = adapter.read_state()

        self.assertIsInstance(state, FragmentAdapterState)
        self.assertEqual(state.generation, 1)
        self.assertEqual(len(state.semantic_fingerprint), 64)
        self.assertEqual(state.snapshot, initial)
        self.assertIsNot(state.snapshot, initial)
        self.assertEqual(adapter.read_snapshot(), initial)
        self.assertIsNot(adapter.read_snapshot(), initial)
        self.assertEqual(state_store.writes, 1)

        restarted = DurableFragmentAdapter("gateway", state_store)
        self.assertEqual(restarted.read_state(), state)
        self.assertEqual(restarted.read_snapshot(), initial)
        self.assertIsNot(restarted.read_snapshot(), initial)

    def test_restart_rejects_regression_and_generation_reuse(self):
        """Durable cursor restoration rejects regressions and mutations."""
        _adapter, state_store, initial = publisher("cloud", generation=7)
        restarted = DurableFragmentAdapter("cloud", state_store)

        with self.assertRaisesRegex(ValueError, "regressed"):
            restarted.publish(
                advance(initial, 6),
                "stale cache",
            )
        with self.assertRaisesRegex(ValueError, "reused"):
            restarted.publish(
                advance(initial, 7, node_id="MUTATED"),
                "same generation mutation",
            )
        self.assertFalse(
            restarted.publish(initial, "exact replay"),
        )
        self.assertEqual(state_store.writes, 1)

    def test_reader_store_failure_is_wrapped_and_fails_closed(self):
        """No synthetic or empty fragment is returned on durable read failure."""
        store = RaisingLoadStore()

        assert_value_free_error(
            self,
            FragmentAdapterReadError,
            lambda: DurableFragmentAdapter("gateway", store),
            "disk unavailable",
        )

    def test_scalar_subclasses_fail_before_write_or_invalidation(self):
        """Adapter scalar validation invokes no subclass-controlled hook."""
        assert_value_free_error(
            self,
            ValueError,
            lambda: DurableFragmentAdapter(
                HostileText("gateway"),
                InMemoryFragmentAdapterStateStore(),
            ),
            HostileText.secret,
        )

        adapter, state_store, initial = publisher("gateway")
        invalidations = []
        adapter.subscribe_invalidation(
            lambda *event: invalidations.append(event),
        )
        writes = state_store.writes
        cases = (
            lambda: adapter.publish(
                initial,
                HostileText("reason"),
            ),
            lambda: adapter.remove(
                HostileInt(2),
                "integration removed",
            ),
        )
        for action in cases:
            with self.subTest(action=action):
                assert_value_free_error(
                    self,
                    ValueError,
                    action,
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, writes)
                self.assertEqual(invalidations, [])

    def test_snapshot_subclasses_and_mutations_fail_before_side_effects(self):
        """Publication validates exact snapshots without retaining source data."""

        def mutated_snapshot(field, value):
            candidate = snapshot(
                "gateway",
                generation=1,
                node_id="GW-INV",
            )
            object.__setattr__(candidate, field, value)
            return candidate

        hostile_subclass = object.__new__(
            HostileProviderSnapshot,
        )
        cases = (
            (
                hostile_subclass,
                False,
            ),
            (
                mutated_snapshot(
                    "provider_id",
                    HostileText("gateway"),
                ),
                False,
            ),
            (
                mutated_snapshot(
                    "generation",
                    HostileInt(1),
                ),
                False,
            ),
            (
                mutated_snapshot(
                    "topology_fragment",
                    HostileMapping(),
                ),
                False,
            ),
            (
                snapshot(
                    "gateway",
                    generation=1,
                    node_id="GW-INV",
                ),
                HostileText("false"),
            ),
        )
        for candidate, removed in cases:
            with self.subTest(
                candidate_type=type(candidate),
                removed_type=type(removed),
            ):
                state_store = InMemoryFragmentAdapterStateStore()
                adapter = DurableFragmentAdapter(
                    "gateway",
                    state_store,
                )
                invalidations = []
                adapter.subscribe_invalidation(
                    lambda *event: invalidations.append(event),
                )
                assert_value_free_error(
                    self,
                    ValueError,
                    lambda: adapter.publish(
                        candidate,
                        "hostile candidate",
                        removed=removed,
                    ),
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, 0)
                self.assertEqual(invalidations, [])

    def test_full_snapshot_reconstruction_rejects_invalid_types_and_bounds(self):
        """Fingerprint-shaped mutations never become durable snapshots."""

        def mutated_alias():
            alias = ProviderAlias(
                "gateway",
                "GW-INV",
            )
            object.__setattr__(
                alias,
                "name",
                HostileText("gateway"),
            )
            return mutated_snapshot("aliases", (alias,))

        def mutated_identity():
            alias = ProviderIdentityAlias(
                "serial",
                "SERIAL-A",
                "GW-INV",
            )
            object.__setattr__(
                alias,
                "value",
                HostileText("SERIAL-A"),
            )
            return mutated_snapshot(
                "identity_aliases",
                (alias,),
            )

        def mutated_role():
            assignment = ProviderRoleAssignment(
                AliasRole.PRIMARY,
                "battery",
                0,
                "GW-INV",
            )
            object.__setattr__(
                assignment,
                "index",
                HostileInt(0),
            )
            return mutated_snapshot(
                "role_assignments",
                (assignment,),
            )

        def projection(value=None):
            if value is None:
                value = ProviderProjectionValue(
                    node_id="GW-INV",
                    kind=ProjectionValueKind.NONE,
                )
            return ProviderConfigProjection(
                argument="fake_arg",
                role=AliasRole.PRIMARY,
                group="battery",
                routing=ProjectionRouting.LEAF,
                cardinality=ProjectionCardinality.SCALAR,
                values=(value,),
                required=False,
            )

        def mutated_projection():
            candidate = projection()
            object.__setattr__(
                candidate,
                "argument",
                HostileText("fake_arg"),
            )
            return mutated_snapshot(
                "config_projections",
                (candidate,),
            )

        def fake_projection_value():
            candidate = projection()
            object.__setattr__(
                candidate,
                "values",
                (FakeProjectionValue(),),
            )
            return mutated_snapshot(
                "config_projections",
                (candidate,),
            )

        def mutated_projection_value():
            value = ProviderProjectionValue(
                node_id="GW-INV",
                kind=ProjectionValueKind.NONE,
            )
            object.__setattr__(
                value,
                "node_id",
                HostileText("GW-INV"),
            )
            return mutated_snapshot(
                "config_projections",
                (projection(value),),
            )

        def excessive_depth():
            value = None
            for _depth in range(34):
                value = (value,)
            return mutated_snapshot(
                "topology_fragment",
                MappingProxyType(
                    {
                        "deep": value,
                    }
                ),
            )

        def excessive_total_items():
            wide = (0,) * 65536
            return mutated_snapshot(
                "topology_fragment",
                MappingProxyType({"wide-{}".format(index): wide for index in range(16)}),
            )

        cases = (
            lambda: mutated_snapshot(
                "health",
                FakeHealth(),
            ),
            lambda: mutated_snapshot(
                "topology_fragment",
                [],
            ),
            lambda: mutated_snapshot(
                "topology_fragment",
                MappingProxyType(
                    {
                        "nested": MappingProxyType(
                            HostileMapping(),
                        ),
                    }
                ),
            ),
            lambda: mutated_snapshot(
                "topology_fragment",
                MappingProxyType(
                    InfiniteItemsMapping(),
                ),
            ),
            lambda: mutated_snapshot(
                "topology_fragment",
                MappingProxyType(
                    {
                        "huge": "x" * (16384 + 1),
                    }
                ),
            ),
            lambda: mutated_snapshot(
                "topology_fragment",
                MappingProxyType(
                    {
                        "huge": 1 << 63,
                    }
                ),
            ),
            lambda: mutated_snapshot(
                "topology_fragment",
                MappingProxyType(
                    {
                        "notFinite": float("inf"),
                    }
                ),
            ),
            excessive_depth,
            excessive_total_items,
            lambda: mutated_snapshot(
                "aliases",
                (FakeAlias(),),
            ),
            mutated_alias,
            lambda: mutated_snapshot(
                "identity_aliases",
                (FakeIdentityAlias(),),
            ),
            mutated_identity,
            lambda: mutated_snapshot(
                "role_assignments",
                (FakeRoleAssignment(),),
            ),
            mutated_role,
            lambda: mutated_snapshot(
                "config_projections",
                (FakeConfigProjection(),),
            ),
            mutated_projection,
            fake_projection_value,
            mutated_projection_value,
        )
        for build_candidate in cases:
            with self.subTest(
                build_candidate=build_candidate,
            ):
                candidate = build_candidate()
                state_store = InMemoryFragmentAdapterStateStore()
                adapter = DurableFragmentAdapter(
                    "gateway",
                    state_store,
                )
                invalidations = []
                adapter.subscribe_invalidation(
                    lambda *event: invalidations.append(event),
                )
                assert_value_free_error(
                    self,
                    ValueError,
                    lambda: adapter.publish(
                        candidate,
                        "invalid snapshot",
                    ),
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, 0)
                self.assertEqual(invalidations, [])

    def test_matched_invalid_snapshot_fails_restart_registry_and_compiler(self):
        """A matching fingerprint cannot legitimize an invalid snapshot."""
        corrupt_snapshot = mutated_snapshot(
            "health",
            FakeHealth(),
        )
        corrupt_state = matched_corrupt_state(
            corrupt_snapshot,
        )

        restart_store = InMemoryFragmentAdapterStateStore(
            corrupt_state,
        )
        assert_value_free_error(
            self,
            FragmentAdapterReadError,
            lambda: DurableFragmentAdapter(
                "gateway",
                restart_store,
            ),
            HostileText.secret,
        )
        self.assertEqual(restart_store.writes, 0)

        valid_adapter, valid_store, _initial = publisher(
            "gateway",
        )
        structural = MutableFragmentPublisher(
            valid_adapter,
        )
        structural.state_reader = lambda: corrupt_state
        registry = FragmentAdapterRegistry(enabled=True)
        assert_value_free_error(
            self,
            ValueError,
            lambda: registry.register(structural),
            HostileText.secret,
        )
        self.assertEqual(registry.provider_ids, ())
        self.assertEqual(valid_store.writes, 1)

        live_registry = FragmentAdapterRegistry(enabled=True)
        self.assertTrue(
            live_registry.register(valid_adapter),
        )
        compiled_store = InMemoryCompiledLatticeStateStore()
        compiler = live_registry.create_compiler(
            compiled_store,
        )
        valid_store._state = corrupt_state
        self.assertTrue(
            compiler.invalidate(
                "gateway",
                1,
                "matched invalid state",
            )
        )
        run = compiler.drain()
        self.assertIsNot(run.status, CompileStatus.FRESH)
        self.assertIsNone(run.plan)
        self.assertTrue(any(issue.code == "provider_read_failed" for issue in run.issues))
        self.assertEqual(valid_store.writes, 1)

    def test_noncanonical_snapshot_fields_fail_every_boundary(self):
        """Normalizing reconstruction cannot heal mutated durable input."""

        def projection(value=None, transforms=()):
            if value is None:
                value = ProviderProjectionValue(
                    node_id="GW-INV",
                    kind=ProjectionValueKind.NONE,
                )
            return ProviderConfigProjection(
                argument="fake_arg",
                role=AliasRole.PRIMARY,
                group="battery",
                routing=ProjectionRouting.LEAF,
                cardinality=ProjectionCardinality.SCALAR,
                values=(value,),
                required=False,
                transforms=transforms,
            )

        def snapshot_provider_id():
            return mutated_snapshot(
                "provider_id",
                " gateway ",
            )

        def alias_name():
            alias = ProviderAlias(
                "gateway",
                "GW-INV",
            )
            object.__setattr__(alias, "name", " gateway ")
            return mutated_snapshot("aliases", (alias,))

        def identity_kind():
            alias = ProviderIdentityAlias(
                "serial",
                "SERIAL-A",
                "GW-INV",
            )
            object.__setattr__(alias, "kind", "SERIAL")
            return mutated_snapshot(
                "identity_aliases",
                (alias,),
            )

        def role_group():
            assignment = ProviderRoleAssignment(
                AliasRole.PRIMARY,
                "battery",
                0,
                "GW-INV",
            )
            object.__setattr__(
                assignment,
                "group",
                " battery ",
            )
            return mutated_snapshot(
                "role_assignments",
                (assignment,),
            )

        def projection_argument():
            candidate = projection()
            object.__setattr__(
                candidate,
                "argument",
                " fake_arg ",
            )
            return mutated_snapshot(
                "config_projections",
                (candidate,),
            )

        def projection_value_identity_kind():
            value = ProviderProjectionValue(
                node_id="GW-INV",
                kind=ProjectionValueKind.NONE,
                identity_kind="serial",
                identity_value="SERIAL-A",
            )
            object.__setattr__(
                value,
                "identity_kind",
                "SERIAL",
            )
            return mutated_snapshot(
                "config_projections",
                (projection(value),),
            )

        def projection_transform():
            candidate = projection(transforms=("scale",))
            object.__setattr__(
                candidate,
                "transforms",
                (" scale ",),
            )
            return mutated_snapshot(
                "config_projections",
                (candidate,),
            )

        cases = (
            (snapshot_provider_id, " gateway "),
            (alias_name, " gateway "),
            (identity_kind, "SERIAL"),
            (role_group, " battery "),
            (projection_argument, " fake_arg "),
            (projection_value_identity_kind, "SERIAL"),
            (projection_transform, " scale "),
        )
        for build_candidate, raw_value in cases:
            with self.subTest(
                build_candidate=build_candidate,
            ):
                candidate = build_candidate()
                empty_store = InMemoryFragmentAdapterStateStore()
                adapter = DurableFragmentAdapter(
                    "gateway",
                    empty_store,
                )
                invalidations = []
                adapter.subscribe_invalidation(
                    lambda *event: invalidations.append(event),
                )
                assert_value_free_error(
                    self,
                    ValueError,
                    lambda: adapter.publish(
                        candidate,
                        "noncanonical snapshot",
                    ),
                    raw_value,
                )
                self.assertEqual(empty_store.writes, 0)
                self.assertEqual(invalidations, [])

                corrupt_state = matched_corrupt_state(candidate)
                restart_store = InMemoryFragmentAdapterStateStore(
                    corrupt_state,
                )
                assert_value_free_error(
                    self,
                    FragmentAdapterReadError,
                    lambda: DurableFragmentAdapter(
                        "gateway",
                        restart_store,
                    ),
                    raw_value,
                )
                self.assertEqual(restart_store.writes, 0)

                valid_adapter, valid_store, _initial = publisher(
                    "gateway",
                )
                structural = MutableFragmentPublisher(
                    valid_adapter,
                )
                structural.state_reader = lambda: corrupt_state
                registry = FragmentAdapterRegistry(enabled=True)
                assert_value_free_error(
                    self,
                    ValueError,
                    lambda: registry.register(structural),
                    raw_value,
                )
                self.assertEqual(registry.provider_ids, ())
                self.assertEqual(valid_store.writes, 1)

                live_registry = FragmentAdapterRegistry(
                    enabled=True,
                )
                self.assertTrue(
                    live_registry.register(valid_adapter),
                )
                compiler = live_registry.create_compiler(
                    InMemoryCompiledLatticeStateStore(),
                )
                valid_store._state = corrupt_state
                self.assertTrue(
                    compiler.invalidate(
                        "gateway",
                        1,
                        "noncanonical durable state",
                    )
                )
                run = compiler.drain()
                self.assertIsNot(
                    run.status,
                    CompileStatus.FRESH,
                )
                self.assertIsNone(run.plan)
                self.assertTrue(any(issue.code == "provider_read_failed" for issue in run.issues))
                for issue in run.issues:
                    self.assertNotIn(
                        raw_value,
                        issue.detail,
                    )
                self.assertEqual(valid_store.writes, 1)

    def test_noncanonical_durable_provider_id_fails_every_read_boundary(self):
        """The durable cursor provider ID must already be canonical."""
        candidate = mutated_snapshot(
            "provider_id",
            "gateway",
        )
        raw_value = " gateway "
        corrupt_state = matched_corrupt_state(
            candidate,
            provider_id=raw_value,
        )

        restart_store = InMemoryFragmentAdapterStateStore(
            corrupt_state,
        )
        assert_value_free_error(
            self,
            FragmentAdapterReadError,
            lambda: DurableFragmentAdapter(
                "gateway",
                restart_store,
            ),
            raw_value,
        )
        self.assertEqual(restart_store.writes, 0)

        valid_adapter, valid_store, _initial = publisher(
            "gateway",
        )
        structural = MutableFragmentPublisher(valid_adapter)
        structural.state_reader = lambda: corrupt_state
        registry = FragmentAdapterRegistry(enabled=True)
        assert_value_free_error(
            self,
            ValueError,
            lambda: registry.register(structural),
            raw_value,
        )
        self.assertEqual(registry.provider_ids, ())
        self.assertEqual(valid_store.writes, 1)

        live_registry = FragmentAdapterRegistry(enabled=True)
        self.assertTrue(live_registry.register(valid_adapter))
        compiler = live_registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )
        valid_store._state = corrupt_state
        self.assertTrue(
            compiler.invalidate(
                "gateway",
                1,
                "noncanonical durable provider ID",
            )
        )
        run = compiler.drain()
        self.assertIsNot(run.status, CompileStatus.FRESH)
        self.assertIsNone(run.plan)
        self.assertTrue(any(issue.code == "provider_read_failed" for issue in run.issues))
        for issue in run.issues:
            self.assertNotIn(raw_value, issue.detail)
        self.assertEqual(valid_store.writes, 1)

    def test_hostile_durable_state_fails_value_free_after_restart(self):
        """Every corrupt scalar and snapshot hook is sanitized on read."""

        def mutate_state(field, value):
            return lambda state: object.__setattr__(
                state,
                field,
                value,
            )

        def mutate_snapshot(field, value):
            return lambda state: object.__setattr__(
                state.snapshot,
                field,
                value,
            )

        def replace_with_subclass(state):
            replacement = object.__new__(
                FragmentAdapterStateSubclass,
            )
            for field in (
                "provider_id",
                "generation",
                "semantic_fingerprint",
                "snapshot",
                "removed",
            ):
                object.__setattr__(
                    replacement,
                    field,
                    getattr(state, field),
                )
            return replacement

        cases = (
            mutate_state(
                "provider_id",
                HostileText("gateway"),
            ),
            mutate_state(
                "generation",
                HostileInt(1),
            ),
            mutate_state(
                "semantic_fingerprint",
                HostileText("fingerprint"),
            ),
            mutate_state(
                "removed",
                HostileText("false"),
            ),
            mutate_snapshot(
                "provider_id",
                HostileText("gateway"),
            ),
            mutate_snapshot(
                "generation",
                HostileInt(1),
            ),
            mutate_snapshot(
                "topology_fragment",
                HostileMapping(),
            ),
            replace_with_subclass,
        )
        for corrupt in cases:
            with self.subTest(corrupt=corrupt):
                adapter, state_store, _initial = publisher(
                    "gateway",
                )
                state = state_store.load()
                replacement = corrupt(state)
                if replacement is not None:
                    state_store._state = replacement
                writes = state_store.writes
                assert_value_free_error(
                    self,
                    FragmentAdapterReadError,
                    lambda: DurableFragmentAdapter(
                        "gateway",
                        state_store,
                    ),
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, writes)

    def test_conflicting_atomic_write_leaves_requested_generation_pending(self):
        """A rejected CAS never presents an uncommitted fragment as current."""
        adapter, seeded_store, initial = publisher("gateway")
        rejecting = RejectingStore(adapter.read_state())
        adapter = DurableFragmentAdapter("gateway", rejecting)
        _registry, compiler, _compiled_store, _discovered = compiled_registry(adapter)
        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)

        updated = advance(initial, 2)
        with self.assertRaises(FragmentAdapterConflict):
            adapter.publish(updated, "new telemetry")

        failed = compiler.drain()
        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertTrue(failed.pending)
        self.assertEqual(adapter.read_state(), seeded_store.load())
        self.assertEqual(
            first.publication,
            compiler.publication,
        )

    def test_removal_is_durable_and_reader_fails_closed_after_restart(self):
        """Runtime removal is an auditable tombstone, never silent absence."""
        adapter, state_store, _initial = publisher("gateway")

        self.assertTrue(adapter.remove(2, "integration removed"))
        removed = adapter.read_state()

        self.assertTrue(removed.removed)
        self.assertEqual(removed.generation, 2)
        self.assertEqual(removed.snapshot.health, ProviderHealth.OFFLINE)
        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()

        restarted = DurableFragmentAdapter("gateway", state_store)
        self.assertEqual(restarted.read_state(), removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()


class TestFragmentAdapterRegistry(unittest.TestCase):
    """Any common-surface component can drive the compiled coordinator."""

    def test_registry_is_default_off_and_does_not_touch_components(self):
        """Disabled discovery performs no integration calls or registration."""
        adapter, _store, _initial = publisher("gateway")
        component = FragmentComponent(adapter)
        registry = FragmentAdapterRegistry()

        self.assertEqual(registry.discover([component]), ())
        self.assertEqual(component.calls, 0)
        self.assertEqual(registry.provider_ids, ())
        self.assertFalse(registry.register(adapter))
        self.assertFalse(registry.unregister("gateway"))
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            registry.create_compiler(InMemoryCompiledLatticeStateStore())

    def test_discovery_has_no_brand_allow_list(self):
        """Arbitrary provider IDs are discovered through the common surface."""
        alpha, _alpha_store, _alpha = publisher("future-cloud-alpha")
        beta, _beta_store, _beta = publisher("local-modbus-beta")

        registry, compiler, _store, discovered = compiled_registry(
            alpha,
            beta,
        )
        run = compiler.drain()

        self.assertEqual(
            discovered,
            ("future-cloud-alpha", "local-modbus-beta"),
        )
        self.assertEqual(
            registry.provider_ids,
            ("future-cloud-alpha", "local-modbus-beta"),
        )
        self.assertEqual(
            dict(run.publication.provider_generations),
            {
                "future-cloud-alpha": 1,
                "local-modbus-beta": 1,
            },
        )

    def test_invalid_discovery_batch_is_transactional(self):
        """Duplicate discovery does not partially mutate registry membership."""
        alpha, _alpha_store, _initial = publisher("same-provider")
        duplicate = DurableFragmentAdapter(
            "same-provider",
            _alpha_store,
        )
        registry = FragmentAdapterRegistry(enabled=True)

        with self.assertRaisesRegex(ValueError, "more than once"):
            registry.discover(
                [
                    FragmentComponent(alpha),
                    FragmentComponent(duplicate),
                ]
            )

        self.assertEqual(registry.provider_ids, ())

    def test_hostile_adapter_and_state_hooks_fail_value_free(self):
        """Every registry entry path sanitizes adapter and state faults."""
        adapter, state_store, _initial = publisher("gateway")
        state = adapter.read_state()
        hostile_state = object.__new__(
            HostileFragmentAdapterState,
        )
        for field in (
            "provider_id",
            "generation",
            "semantic_fingerprint",
            "snapshot",
            "removed",
        ):
            object.__setattr__(
                hostile_state,
                field,
                object.__getattribute__(state, field),
            )

        register_registry = FragmentAdapterRegistry(enabled=True)
        assert_value_free_error(
            self,
            ValueError,
            lambda: register_registry.register(
                HostileRegistryAdapter(),
            ),
            HostileText.secret,
        )
        self.assertEqual(register_registry.provider_ids, ())

        discovered = MutableFragmentPublisher(adapter)
        discovered.state_reader = lambda: hostile_state
        discover_registry = FragmentAdapterRegistry(enabled=True)
        assert_value_free_error(
            self,
            ValueError,
            lambda: discover_registry.discover(
                (FragmentComponent(discovered),),
            ),
            HostileText.secret,
        )
        self.assertEqual(discover_registry.provider_ids, ())

        compiling = MutableFragmentPublisher(adapter)
        compiler_registry = FragmentAdapterRegistry(enabled=True)
        self.assertTrue(
            compiler_registry.register(compiling),
        )

        def fail_state_read():
            raise RuntimeError(HostileText.secret)

        compiling.state_reader = fail_state_read
        compiled_store = InMemoryCompiledLatticeStateStore()
        assert_value_free_error(
            self,
            ValueError,
            lambda: compiler_registry.create_compiler(
                compiled_store,
            ),
            HostileText.secret,
        )
        self.assertEqual(compiled_store.writes, 0)
        self.assertEqual(state_store.writes, 1)

    def test_register_unregister_only_before_compiler_is_sealed(self):
        """Runtime membership changes cannot silently alter compiler inputs."""
        adapter, _store, _initial = publisher("gateway")
        registry = FragmentAdapterRegistry(enabled=True)

        self.assertTrue(registry.register(adapter))
        self.assertTrue(registry.unregister("gateway"))
        self.assertTrue(registry.register(adapter))
        compiler = registry.create_compiler(InMemoryCompiledLatticeStateStore())

        with self.assertRaisesRegex(RuntimeError, "tombstone"):
            registry.unregister("gateway")
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            registry.register(adapter)
        self.assertEqual(compiler.drain().status, CompileStatus.FRESH)

    def test_any_registered_integration_invalidates_and_republishes(self):
        """Every discovered publisher can replace its prior fragment."""
        alpha, _alpha_store, alpha_one = publisher("alpha")
        beta, _beta_store, beta_one = publisher("beta")
        _registry, compiler, compiled_store, _ids = compiled_registry(
            alpha,
            beta,
        )
        first = compiler.drain()

        alpha_two = advance(alpha_one, 2)
        beta_two = advance(beta_one, 2)
        self.assertTrue(alpha.publish(alpha_two, "alpha refresh"))
        self.assertTrue(beta.publish(beta_two, "beta refresh"))
        second = compiler.drain()

        self.assertEqual(second.attempts, 1)
        self.assertTrue(second.published)
        self.assertEqual(second.publication.lattice_version, 2)
        self.assertEqual(
            dict(second.publication.provider_generations),
            {"alpha": 2, "beta": 2},
        )
        self.assertEqual(compiled_store.writes, 2)
        self.assertEqual(
            {(cause.source_id, cause.generation) for cause in second.publication.invalidation_causes},
            {("alpha", 2), ("beta", 2)},
        )
        self.assertEqual(first.publication.lattice_version, 1)

    def test_degraded_fragment_invalidates_and_publishes_degraded_cursor(self):
        """Health-only changes are durable inputs and trigger recompilation."""
        adapter, _store, initial = publisher("gateway")
        _registry, compiler, _compiled_store, _ids = compiled_registry(adapter)
        compiler.drain()

        degraded = advance(
            initial,
            2,
            health=ProviderHealth.DEGRADED,
        )
        self.assertTrue(adapter.publish(degraded, "provider health degraded"))
        run = compiler.drain()

        self.assertTrue(run.published)
        self.assertEqual(run.status, CompileStatus.DEGRADED)
        self.assertEqual(
            dict(run.publication.provider_generations),
            {"gateway": 2},
        )
        self.assertIn(
            "provider_degraded",
            {issue.code for issue in run.issues},
        )

    def test_offline_fragment_invalidates_but_preserves_last_known_good(self):
        """An active provider going offline fails closed after one attempt."""
        adapter, _store, initial = publisher("gateway")
        _registry, compiler, _compiled_store, _ids = compiled_registry(adapter)
        first = compiler.drain()

        offline = advance(
            initial,
            2,
            health=ProviderHealth.OFFLINE,
        )
        self.assertTrue(adapter.publish(offline, "provider disconnected"))
        run = compiler.drain()

        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.status, CompileStatus.STALE)
        self.assertTrue(run.pending)
        self.assertIs(run.publication, first.publication)
        self.assertIs(run.plan, first.publication.plan)
        self.assertIn(
            "active_provider_unavailable",
            {issue.code for issue in run.issues},
        )

    def test_other_provider_invalidation_exposes_reader_failure_fail_closed(self):
        """A fresh-read failure blocks publication instead of dropping input."""
        gateway_store = ToggleLoadStore()
        gateway = DurableFragmentAdapter("gateway", gateway_store)
        gateway_one = snapshot("gateway", generation=1, node_id="GW-INV")
        gateway.publish(gateway_one, "initial gateway discovery")
        cloud, _cloud_store, cloud_one = publisher("cloud")
        _registry, compiler, _compiled_store, _ids = compiled_registry(
            gateway,
            cloud,
        )
        first = compiler.drain()

        gateway_store.fail_reads = True
        self.assertTrue(
            cloud.publish(
                advance(cloud_one, 2),
                "cloud refresh requires fresh read of every provider",
            )
        )
        run = compiler.drain()

        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.status, CompileStatus.STALE)
        self.assertTrue(run.pending)
        self.assertIs(run.publication, first.publication)
        self.assertIn(
            "provider_read_failed",
            {issue.code for issue in run.issues},
        )

    def test_removal_invalidates_and_clears_prior_provider_contribution(self):
        """A durable removal compiles as an acknowledged empty fragment."""
        adapter, _store, _initial = publisher("gateway")
        _registry, compiler, _compiled_store, _ids = compiled_registry(adapter)
        first = compiler.drain()

        self.assertTrue(adapter.remove(2, "integration disabled by user"))
        run = compiler.drain()

        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.status, CompileStatus.FRESH)
        self.assertFalse(run.pending)
        self.assertTrue(run.published)
        self.assertEqual(run.publication.lattice_version, 2)
        self.assertEqual(
            dict(run.publication.provider_generations),
            {"gateway": 2},
        )
        self.assertEqual(run.plan.topology["nodes"], ())
        self.assertNotEqual(run.publication, first.publication)

    def test_restart_restores_adapter_and_compiler_cursor_protection(self):
        """Both durable layers reject reuse after a complete process restart."""
        adapter, adapter_store, initial = publisher("gateway")
        registry, compiler, compiled_store, _ids = compiled_registry(adapter)
        compiler.drain()
        second_snapshot = advance(initial, 2)
        self.assertTrue(adapter.publish(second_snapshot, "new discovery"))
        second = compiler.drain()
        self.assertEqual(second.publication.lattice_version, 2)

        restarted_adapter = DurableFragmentAdapter(
            "gateway",
            adapter_store,
        )
        restarted_registry, restarted_compiler, _same_store, _ids = compiled_registry_with_store(
            compiled_store,
            restarted_adapter,
        )
        exact = restarted_compiler.drain()

        self.assertEqual(
            restarted_registry.provider_ids,
            registry.provider_ids,
        )
        self.assertFalse(exact.published)
        self.assertEqual(exact.publication, second.publication)
        with self.assertRaisesRegex(ValueError, "reused"):
            restarted_adapter.publish(
                advance(second_snapshot, 2, node_id="REUSED"),
                "same cursor mutation after restart",
            )
        self.assertEqual(
            restarted_compiler.publication,
            second.publication,
        )

    def test_publication_feedback_token_does_not_persist_or_recompile(self):
        """Compiler-origin feedback is suppressed before adapter persistence."""
        adapter, state_store, initial = publisher("gateway")
        _registry, compiler, compiled_store, _ids = compiled_registry(adapter)
        first = compiler.drain()

        feedback_snapshot = advance(initial, 2)
        self.assertFalse(
            adapter.publish(
                feedback_snapshot,
                "published config observed",
                feedback_token=first.publication.feedback_token,
            )
        )
        idle = compiler.drain()

        self.assertEqual(adapter.read_state().generation, 1)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(compiled_store.writes, 1)
        self.assertEqual(idle.attempts, 0)
        self.assertIs(idle.publication, first.publication)

    def test_invalidation_during_read_gets_one_bounded_follow_up(self):
        """Concurrent adapter invalidation triggers exactly one fresh follow-up."""
        adapter, _store, initial = publisher("gateway")
        registry = FragmentAdapterRegistry(enabled=True)
        registry.register(adapter)
        compiler = registry.create_compiler(InMemoryCompiledLatticeStateStore())
        original_reader = adapter.read_state
        fired = [False]

        def invalidating_reader():
            """Publish one newer generation during the first compile read."""
            value = original_reader()
            if not fired[0]:
                fired[0] = True
                adapter.publish(
                    advance(initial, 2),
                    "concurrent rediscovery",
                )
            return value

        adapter.read_state = invalidating_reader
        run = compiler.drain()

        self.assertEqual(run.attempts, 2)
        self.assertTrue(run.published)
        self.assertEqual(
            dict(run.publication.provider_generations),
            {"gateway": 2},
        )


def compiled_registry_with_store(compiled_store, *adapters):
    """Create a restarted registry against an existing compiled store."""
    registry = FragmentAdapterRegistry(enabled=True)
    discovered = registry.discover([FragmentComponent(adapter) for adapter in adapters])
    compiler = registry.create_compiler(compiled_store)
    return registry, compiler, compiled_store, discovered


if __name__ == "__main__":
    unittest.main()

"""Tests for pure Lattice fragment auto-configuration compilation."""

# cspell:ignore autoconfig

import os
import sys
import threading
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (
    AliasRole,
    AutoConfigCompileError,
    CompileStatus,
    LatticeAutoConfigCompiler,
    MaterializationReadiness,
    ProjectionCardinality,
    ProjectionRouting,
    ProjectionValueKind,
    ProviderConfigProjection,
    ProviderAlias,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderProjectionValue,
    ProviderRoleAssignment,
    ProviderSnapshot,
    UserConfigOverride,
    compile_auto_config,
)


def fragment(provider, generation, node_id="INV1", kind="inverter"):
    """Build a compact provider-owned topology fragment."""
    access_path = "{}-path".format(provider)
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": generation,
        "producer": {"name": provider, "provider": provider, "authority": 10},
        "nodes": [
            {
                "id": node_id,
                "kind": kind,
                "deviceType": "hybrid",
                "accessPaths": [{"id": access_path, "provider": provider, "preference": 10}],
                "capabilities": [
                    {
                        "capability": "battery.target_soc",
                        "accessPath": access_path,
                        "ref": 1,
                        "shape": "setpoint",
                        "control": {"protocol": "mqtt"},
                    }
                ],
            }
        ],
    }


def snapshot(
    provider,
    generation=1,
    node_id="INV1",
    kind="inverter",
    health=ProviderHealth.HEALTHY,
    aliases=(),
    identity_aliases=(),
    role_assignments=(),
):
    """Build one typed provider snapshot."""
    return ProviderSnapshot(
        provider,
        generation,
        health,
        fragment(provider, generation, node_id=node_id, kind=kind),
        aliases,
        identity_aliases,
        role_assignments,
    )


def multi_fragment(provider, node_ids):
    """Build a provider fragment containing ordered independent inverter nodes."""
    nodes = []
    for index, node_id in enumerate(node_ids):
        access_path = "{}-{}-path".format(provider, node_id)
        nodes.append(
            {
                "id": node_id,
                "kind": "inverter",
                "deviceType": "hybrid",
                "accessPaths": [
                    {
                        "id": access_path,
                        "provider": provider,
                        "preference": 10,
                    }
                ],
                "capabilities": [
                    {
                        "capability": "battery.target_soc",
                        "accessPath": access_path,
                        "ref": index + 1,
                        "shape": "setpoint",
                        "control": {"protocol": "mqtt"},
                    }
                ],
            }
        )
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": 1,
        "producer": {
            "name": provider,
            "provider": provider,
            "authority": 10,
        },
        "nodes": nodes,
    }


def projection_snapshot(
    provider,
    node_ids,
    role_assignments,
    config_projections,
    identity_aliases=(),
    generation=1,
):
    """Build a provider snapshot carrying indexed roles and config projections."""
    return ProviderSnapshot(
        provider_id=provider,
        generation=generation,
        health=ProviderHealth.HEALTHY,
        topology_fragment=multi_fragment(provider, node_ids),
        identity_aliases=identity_aliases,
        role_assignments=role_assignments,
        config_projections=config_projections,
    )


def projection_value(
    node_id,
    kind,
    value=None,
    capability=None,
    identity=None,
    access_path_id=None,
):
    """Build one compact provider projection value for tests."""
    identity_kind, identity_value = identity or (None, None)
    if kind is ProjectionValueKind.ENTITY and capability is None:
        capability = "battery.target_soc"
    return ProviderProjectionValue(
        node_id=node_id,
        kind=kind,
        value=value,
        capability=capability,
        identity_kind=identity_kind,
        identity_value=identity_value,
        access_path_id=access_path_id,
    )


def partial_projection_snapshot(
    provider,
    node_id,
    index,
    argument,
    value,
    required=True,
    identity_aliases=(),
):
    """Build one provider-owned aggregate slot and local projection value."""
    roles = tuple(
        ProviderRoleAssignment(
            role,
            "inverters",
            index,
            node_id,
        )
        for role in (AliasRole.PRIMARY, AliasRole.CONTROL)
    )
    return projection_snapshot(
        provider,
        (node_id,),
        roles,
        (
            config_projection(
                argument,
                (value,),
                required=required,
            ),
        ),
        identity_aliases=identity_aliases,
    )


def config_projection(
    argument,
    values,
    role=AliasRole.PRIMARY,
    group="inverters",
    routing=ProjectionRouting.LEAF,
    cardinality=ProjectionCardinality.PER_INDEX,
    required=True,
    transforms=(),
):
    """Build one generic provider projection declaration for tests."""
    return ProviderConfigProjection(
        argument=argument,
        role=role,
        group=group,
        routing=routing,
        cardinality=cardinality,
        values=values,
        required=required,
        transforms=transforms,
    )


def indexed_roles(node_ids):
    """Select each node as both an indexed primary and control target."""
    return tuple(ProviderRoleAssignment(role, "inverters", index, node_id) for index, node_id in enumerate(node_ids) for role in (AliasRole.PRIMARY, AliasRole.CONTROL))


def ready_plan():
    """Copy a compiled shadow plan into a future-materializer test harness."""
    plan = compile_auto_config((snapshot("gateway"),))
    return replace(
        plan,
        materialization_readiness=MaterializationReadiness(True, ()),
    )


class MutableReader:
    """Thread-safe-enough mutable snapshot reader for deterministic tests."""

    def __init__(self, value):
        """Store the first snapshot and initialise the call count."""
        self.value = value
        self.calls = 0

    def __call__(self):
        """Return the current snapshot and count the fresh read."""
        self.calls += 1
        return self.value


class TestPlanCompilation(unittest.TestCase):
    """Compiler output is safe, immutable, deterministic, and attributable."""

    def test_materialization_readiness_rejects_inconsistent_state(self):
        """Readiness is derived exactly from a normalized blocker tuple."""
        readiness = MaterializationReadiness(
            False,
            [" config_projection_bindings_missing "],
        )

        self.assertEqual(
            readiness.blockers,
            ("config_projection_bindings_missing",),
        )
        with self.assertRaisesRegex(ValueError, "absence of blockers"):
            MaterializationReadiness(True, ("projection_missing",))
        with self.assertRaisesRegex(ValueError, "absence of blockers"):
            MaterializationReadiness(False, ())
        with self.assertRaisesRegex(ValueError, "unique"):
            MaterializationReadiness(False, ("projection_missing", "projection_missing"))
        with self.assertRaisesRegex(ValueError, "non-empty"):
            MaterializationReadiness(False, (" ",))
        with self.assertRaisesRegex(ValueError, "iterable of strings"):
            MaterializationReadiness(False, "projection_missing")

    def test_order_independent_digest_and_provider_qualified_aliases(self):
        """Input order and shared local alias names cannot alter a plan."""
        gateway_alias = ProviderAlias("battery", "INV1", frozenset((AliasRole.REFERENCE, AliasRole.PRIMARY, AliasRole.CONTROL)))
        cloud_alias = ProviderAlias("battery", "INV1")
        gateway = snapshot("gateway", aliases=(gateway_alias,), identity_aliases=(ProviderIdentityAlias("serial", "SER123", "INV1"),))
        cloud = snapshot("cloud", aliases=(cloud_alias,), identity_aliases=(ProviderIdentityAlias("serial", "SER123", "INV1"),))

        left = compile_auto_config((gateway, cloud))
        right = compile_auto_config((cloud, gateway))

        self.assertEqual(left.digest, right.digest)
        self.assertEqual([binding.qualified_name for binding in left.aliases], ["cloud:battery", "gateway:battery"])
        self.assertEqual(dict(left.provider_generations), {"cloud": 1, "gateway": 1})
        self.assertEqual({field.name for field in left.fields}, {"alias.cloud:battery", "alias.gateway:battery", "control_target", "primary_target"})
        self.assertEqual(left.primary_target, "identity:serial:SER123")
        self.assertEqual(left.control_target, "identity:serial:SER123")
        self.assertEqual(left.primary_targets, ())
        self.assertEqual(left.control_targets, ())
        self.assertTrue(all(field.provenance for field in left.fields))
        with self.assertRaises(TypeError):
            left.topology["scope"] = "fragment"

    def test_generation_bookkeeping_does_not_change_semantic_digest(self):
        """An identical newer fragment generation avoids materialization churn."""
        alias = ProviderAlias("battery", "INV1", frozenset((AliasRole.PRIMARY,)))
        first = compile_auto_config((snapshot("gateway", generation=1, aliases=(alias,)),))
        second = compile_auto_config((snapshot("gateway", generation=2, aliases=(alias,)),))

        self.assertEqual(first.digest, second.digest)
        self.assertNotEqual(first.provider_generations, second.provider_generations)

    def test_shared_stable_identity_correlates_different_local_nodes(self):
        """Gateway and cloud assertions for one serial compile to one node."""
        gateway_alias = ProviderIdentityAlias("serial", "SER123", "gw-local-1")
        cloud_alias = ProviderIdentityAlias("serial", "SER123", "cloud-local-9")
        primary = ProviderAlias("battery", "gw-local-1", frozenset((AliasRole.PRIMARY, AliasRole.CONTROL)))
        gateway = snapshot("gateway", node_id="gw-local-1", aliases=(primary,), identity_aliases=(gateway_alias,))
        cloud = snapshot("cloud", node_id="cloud-local-9", identity_aliases=(cloud_alias,))

        plan = compile_auto_config((gateway, cloud))

        self.assertEqual(len(plan.topology["nodes"]), 1)
        self.assertEqual(plan.topology["nodes"][0]["id"], "identity:serial:SER123")
        self.assertEqual({path["id"] for path in plan.topology["nodes"][0]["accessPaths"]}, {"gateway-path", "cloud-path"})
        self.assertEqual(plan.aliases[0].node_id, "identity:serial:SER123")
        self.assertEqual({binding.provider_id for binding in plan.identity_aliases}, {"gateway", "cloud"})
        capability_sources = {item.provider_id: item.source_path for item in plan.provenance if "/capabilities/" in item.field_path}
        self.assertIn("gw-local-1", capability_sources["gateway"])
        self.assertIn("cloud-local-9", capability_sources["cloud"])

    def test_equal_provider_local_ids_do_not_implicitly_correlate(self):
        """Matching local labels remain separate without a shared stable identity."""
        plan = compile_auto_config((snapshot("gateway", node_id="INV1"), snapshot("cloud", node_id="INV1")))

        self.assertEqual(
            {node["id"] for node in plan.topology["nodes"]},
            {"provider:gateway:INV1", "provider:cloud:INV1"},
        )

    def test_duplicate_provider_snapshots_fail_closed(self):
        """The pure compile entry point cannot accept two generations of one provider."""
        with self.assertRaisesRegex(AutoConfigCompileError, "duplicate provider"):
            compile_auto_config((snapshot("gateway", generation=1), snapshot("gateway", generation=2)))

    def test_provider_cannot_reuse_stable_identity_for_two_nodes(self):
        """One integration cannot correlate two local nodes to one identity."""
        document = fragment("gateway", 1, node_id="INV1")
        second = dict(document["nodes"][0])
        second["id"] = "INV2"
        document["nodes"].append(second)
        aliases = (ProviderIdentityAlias("serial", "SER123", "INV1"), ProviderIdentityAlias("serial", "SER123", "INV2"))
        provider = ProviderSnapshot("gateway", 1, ProviderHealth.HEALTHY, document, (), aliases)

        with self.assertRaisesRegex(AutoConfigCompileError, "identity alias collision"):
            compile_auto_config((provider,))

    def test_correlated_strong_identity_values_cannot_conflict(self):
        """A cross-kind correlation cannot conceal conflicting serial values."""
        gateway_aliases = (
            ProviderIdentityAlias("serial", "SER-A", "gw"),
            ProviderIdentityAlias("mac", "AA:BB", "gw"),
        )
        cloud_aliases = (
            ProviderIdentityAlias("serial", "SER-B", "cloud"),
            ProviderIdentityAlias("mac", "AA:BB", "cloud"),
        )
        gateway = snapshot("gateway", node_id="gw", identity_aliases=gateway_aliases)
        cloud = snapshot("cloud", node_id="cloud", identity_aliases=cloud_aliases)

        with self.assertRaisesRegex(AutoConfigCompileError, "strong identity aliases conflict"):
            compile_auto_config((gateway, cloud))

    def test_duplicate_qualified_alias_fails_closed(self):
        """One provider cannot publish the same qualified alias twice."""
        aliases = (ProviderAlias("battery", "INV1"), ProviderAlias("battery", "INV1"))
        with self.assertRaisesRegex(AutoConfigCompileError, "alias collision"):
            compile_auto_config((snapshot("gateway", aliases=aliases),))

    def test_identity_collision_fails_closed(self):
        """Conflicting identity fields for one node cannot be authority-merged."""
        gateway_alias = ProviderIdentityAlias("serial", "SER123", "INV1")
        cloud_alias = ProviderIdentityAlias("serial", "SER123", "INV1")
        with self.assertRaisesRegex(AutoConfigCompileError, "identity collision"):
            compile_auto_config(
                (
                    snapshot("gateway", kind="inverter", identity_aliases=(gateway_alias,)),
                    snapshot("cloud", kind="battery", identity_aliases=(cloud_alias,)),
                )
            )

    def test_ambiguous_primary_and_control_targets_fail_closed(self):
        """Several distinct target nodes cannot silently pick a winner."""
        primary_a = ProviderAlias("battery", "INV1", frozenset((AliasRole.PRIMARY,)))
        primary_b = ProviderAlias("battery", "INV2", frozenset((AliasRole.PRIMARY,)))
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous legacy primary"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(primary_a,)), snapshot("cloud", node_id="INV2", aliases=(primary_b,))))

        control_a = ProviderAlias("control", "INV1", frozenset((AliasRole.CONTROL,)))
        control_b = ProviderAlias("control", "INV2", frozenset((AliasRole.CONTROL,)))
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous legacy control"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(control_a,)), snapshot("cloud", node_id="INV2", aliases=(control_b,))))

    def test_alias_must_target_provider_local_identity(self):
        """An alias cannot smuggle a target owned only by another provider."""
        bad = ProviderAlias("battery", "INV2")
        with self.assertRaisesRegex(AutoConfigCompileError, "unknown provider-local node"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(bad,)), snapshot("cloud", node_id="INV2")))

    def test_indexed_roles_are_order_independent_and_contiguous(self):
        """Indexed targets sort by group, role, and index regardless of input order."""
        first = snapshot(
            "a",
            node_id="A",
            role_assignments=(
                ProviderRoleAssignment(AliasRole.CONTROL, "battery", 1, "A"),
                ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "A"),
            ),
        )
        second = snapshot(
            "z",
            node_id="Z",
            role_assignments=(
                ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 1, "Z"),
                ProviderRoleAssignment(AliasRole.CONTROL, "battery", 0, "Z"),
            ),
        )

        left = compile_auto_config((second, first))
        right = compile_auto_config((first, second))

        self.assertEqual(left.digest, right.digest)
        self.assertEqual(
            [(target.group, target.index, target.node_id) for target in left.primary_targets],
            [
                ("battery", 0, "provider:a:A"),
                ("battery", 1, "provider:z:Z"),
            ],
        )
        self.assertEqual(
            [(target.group, target.index, target.node_id) for target in left.control_targets],
            [
                ("battery", 0, "provider:z:Z"),
                ("battery", 1, "provider:a:A"),
            ],
        )
        self.assertEqual(
            [(item.group, item.role, item.index) for item in left.role_assignments],
            sorted((item.group, item.role, item.index) for item in left.role_assignments),
        )
        self.assertEqual(
            {field.name for field in left.fields if field.name.startswith(("primary_targets", "control_targets"))},
            {
                "primary_targets.battery.0",
                "primary_targets.battery.1",
                "control_targets.battery.0",
                "control_targets.battery.1",
            },
        )
        indexed_fields = [field for field in left.fields if field.name.startswith(("primary_targets", "control_targets"))]
        self.assertTrue(all(field.provenance for field in indexed_fields))
        self.assertTrue(all(item in left.provenance for field in indexed_fields for item in field.provenance))
        self.assertFalse(left.materialization_readiness.ready)
        self.assertEqual(left.materialization_readiness.blockers, ("config_projection_bindings_missing",))
        with self.assertRaises(AttributeError):
            left.primary_targets[0].node_id = "changed"

    def test_provider_role_assignment_rejects_reference_and_negative_index(self):
        """Only indexed primary/control assignments with non-negative indices exist."""
        with self.assertRaisesRegex(ValueError, "PRIMARY or CONTROL"):
            ProviderRoleAssignment(AliasRole.REFERENCE, "battery", 0, "INV1")
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ProviderRoleAssignment(AliasRole.PRIMARY, "battery", -1, "INV1")

    def test_indexed_role_indices_must_be_contiguous_per_group_and_role(self):
        """A gap in one role sequence fails without affecting another sequence."""
        assignments = (
            ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "INV1"),
            ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 2, "INV1"),
        )
        with self.assertRaisesRegex(AutoConfigCompileError, "indices must be contiguous"):
            compile_auto_config((snapshot("gateway", role_assignments=assignments),))

    def test_correlated_providers_may_share_one_index(self):
        """Explicit strong identity correlation permits duplicate provider assertions."""
        gateway_identity = ProviderIdentityAlias("serial", "SER123", "gw")
        cloud_identity = ProviderIdentityAlias("serial", "SER123", "cloud")
        gateway_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "gw")
        cloud_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "cloud")
        plan = compile_auto_config(
            (
                snapshot("gateway", node_id="gw", identity_aliases=(gateway_identity,), role_assignments=(gateway_role,)),
                snapshot("cloud", node_id="cloud", identity_aliases=(cloud_identity,), role_assignments=(cloud_role,)),
            )
        )

        self.assertEqual(len(plan.primary_targets), 1)
        self.assertEqual(plan.primary_targets[0].node_id, "identity:serial:SER123")
        self.assertEqual({item.provider_id for item in plan.primary_targets[0].provenance}, {"gateway", "cloud"})

    def test_uncorrelated_providers_conflicting_at_one_index_fail_closed(self):
        """Equal role slots cannot select unrelated provider-local nodes."""
        gateway_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "gw")
        cloud_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "cloud")
        with self.assertRaisesRegex(AutoConfigCompileError, "conflicting primary target"):
            compile_auto_config(
                (
                    snapshot("gateway", node_id="gw", role_assignments=(gateway_role,)),
                    snapshot("cloud", node_id="cloud", role_assignments=(cloud_role,)),
                )
            )

    def test_same_node_may_fill_multiple_indices(self):
        """An EMS aggregate can fan one canonical node out over several indices."""
        assignments = (
            ProviderRoleAssignment(AliasRole.PRIMARY, "ems", 0, "EMS"),
            ProviderRoleAssignment(AliasRole.PRIMARY, "ems", 1, "EMS"),
        )
        plan = compile_auto_config((snapshot("ge-cloud", node_id="EMS", role_assignments=assignments),))

        self.assertEqual([target.node_id for target in plan.primary_targets], ["provider:ge-cloud:EMS", "provider:ge-cloud:EMS"])

    def test_legacy_and_indexed_role_assignments_cannot_mix(self):
        """A plan must use exactly one target-addressing model."""
        legacy = ProviderAlias("battery", "INV1", frozenset((AliasRole.PRIMARY,)))
        indexed = ProviderRoleAssignment(AliasRole.CONTROL, "battery", 0, "INV1")
        with self.assertRaisesRegex(AutoConfigCompileError, "cannot be mixed"):
            compile_auto_config((snapshot("gateway", aliases=(legacy,), role_assignments=(indexed,)),))

    def test_multiple_legacy_assignments_are_ambiguous_even_when_correlated(self):
        """The singular compatibility field represents exactly one assertion."""
        gateway_alias = ProviderAlias("battery", "gw", frozenset((AliasRole.PRIMARY,)))
        cloud_alias = ProviderAlias("battery", "cloud", frozenset((AliasRole.PRIMARY,)))
        gateway_identity = ProviderIdentityAlias("serial", "SER123", "gw")
        cloud_identity = ProviderIdentityAlias("serial", "SER123", "cloud")
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous legacy primary"):
            compile_auto_config(
                (
                    snapshot("gateway", node_id="gw", aliases=(gateway_alias,), identity_aliases=(gateway_identity,)),
                    snapshot("cloud", node_id="cloud", aliases=(cloud_alias,), identity_aliases=(cloud_identity,)),
                )
            )

    def test_reference_only_plan_is_explicitly_shadow_only(self):
        """Reference discovery compiles but exposes every write blocker."""
        reference = ProviderAlias("battery", "INV1")
        plan = compile_auto_config((snapshot("gateway", aliases=(reference,)),))

        self.assertFalse(plan.materialization_readiness.ready)
        self.assertEqual(
            plan.materialization_readiness.blockers,
            (
                "indexed_primary_targets_missing",
                "indexed_control_targets_missing",
                "config_projection_bindings_missing",
            ),
        )
        self.assertIsNone(plan.primary_target)
        self.assertIsNone(plan.control_target)


class TestConfigProjectionCompilation(unittest.TestCase):
    """Provider contracts project selected indexed capabilities into config."""

    def test_disjoint_providers_compose_partial_indexed_slots(self):
        """Each provider can fill its own slot without publishing aggregate width."""
        gateway = partial_projection_snapshot(
            "gateway",
            "GW1",
            0,
            "battery_power",
            projection_value(
                "GW1",
                ProjectionValueKind.ENTITY,
                "sensor.gateway_battery_power",
            ),
        )
        cloud = partial_projection_snapshot(
            "cloud",
            "CLOUD1",
            1,
            "battery_power",
            projection_value(
                "CLOUD1",
                ProjectionValueKind.ENTITY,
                "sensor.cloud_battery_power",
            ),
        )

        left = compile_auto_config((gateway, cloud))
        right = compile_auto_config((cloud, gateway))
        argument = left.config_arguments[0]

        self.assertEqual(left.digest, right.digest)
        self.assertEqual(
            left.projected_config["battery_power"],
            (
                "sensor.gateway_battery_power",
                "sensor.cloud_battery_power",
            ),
        )
        self.assertEqual(
            {
                candidate.provider_id: (
                    candidate.slot_indexes,
                    candidate.target_count,
                    candidate.values,
                )
                for candidate in argument.candidates
            },
            {
                "gateway": (
                    (0,),
                    2,
                    ("sensor.gateway_battery_power",),
                ),
                "cloud": (
                    (1,),
                    2,
                    ("sensor.cloud_battery_power",),
                ),
            },
        )
        self.assertEqual(
            [
                (
                    source.field_path,
                    source.provider_id,
                )
                for source in argument.provenance
            ],
            [
                ("/projected_config/battery_power/0", "gateway"),
                ("/projected_config/battery_power/1", "cloud"),
            ],
        )
        self.assertFalse(left.materialization_readiness.ready)
        self.assertEqual(
            left.materialization_readiness.blockers,
            ("atomic_materializer_missing",),
        )

    def test_atomic_materializer_capability_makes_projected_plan_ready(self):
        """Only an explicitly installed atomic materializer clears its blocker."""
        provider = projection_snapshot(
            "gateway",
            ("GW1",),
            indexed_roles(("GW1",)),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "GW1",
                            ProjectionValueKind.ENTITY,
                            "sensor.gateway_battery_power",
                        ),
                    ),
                ),
            ),
        )

        plan = compile_auto_config(
            (provider,),
            atomic_materializer=True,
        )

        self.assertTrue(plan.materialization_readiness.ready)
        self.assertEqual(plan.materialization_readiness.blockers, ())
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            compile_auto_config(
                (provider,),
                atomic_materializer="yes",
            )

    def test_provider_owned_slot_disambiguates_repeated_canonical_node(self):
        """Explicit roles place correlated providers in their owned slots."""
        identity = "SER-SHARED"
        gateway = partial_projection_snapshot(
            "gateway",
            "GW1",
            0,
            "battery_power",
            projection_value(
                "GW1",
                ProjectionValueKind.ENTITY,
                "sensor.gateway_battery_power",
            ),
            identity_aliases=(
                ProviderIdentityAlias(
                    "serial",
                    identity,
                    "GW1",
                ),
            ),
        )
        cloud = partial_projection_snapshot(
            "cloud",
            "CLOUD1",
            1,
            "battery_power",
            projection_value(
                "CLOUD1",
                ProjectionValueKind.ENTITY,
                "sensor.cloud_battery_power",
            ),
            identity_aliases=(
                ProviderIdentityAlias(
                    "serial",
                    identity,
                    "CLOUD1",
                ),
            ),
        )

        plan = compile_auto_config((gateway, cloud))

        self.assertEqual(
            plan.projected_config["battery_power"],
            (
                "sensor.gateway_battery_power",
                "sensor.cloud_battery_power",
            ),
        )
        self.assertEqual(
            {candidate.provider_id: candidate.slot_indexes for candidate in plan.config_arguments[0].candidates},
            {
                "gateway": (0,),
                "cloud": (1,),
            },
        )

    def test_repeated_node_projection_ignores_role_declaration_order(self):
        """Role tuple order cannot swap values between repeated node slots."""
        roles = tuple(
            ProviderRoleAssignment(
                role,
                "inverters",
                index,
                "GW1",
            )
            for index in (0, 1)
            for role in (AliasRole.PRIMARY, AliasRole.CONTROL)
        )
        projection = config_projection(
            "battery_power",
            (
                projection_value(
                    "GW1",
                    ProjectionValueKind.ENTITY,
                    "sensor.gateway_slot_0",
                ),
                projection_value(
                    "GW1",
                    ProjectionValueKind.ENTITY,
                    "sensor.gateway_slot_1",
                ),
            ),
        )
        left = projection_snapshot(
            "gateway",
            ("GW1",),
            roles,
            (projection,),
        )
        right = projection_snapshot(
            "gateway",
            ("GW1",),
            tuple(reversed(roles)),
            (projection,),
        )

        left_plan = compile_auto_config((left,))
        right_plan = compile_auto_config((right,))

        self.assertEqual(left_plan.digest, right_plan.digest)
        self.assertEqual(
            left_plan.projected_config["battery_power"],
            (
                "sensor.gateway_slot_0",
                "sensor.gateway_slot_1",
            ),
        )

    def test_unowned_value_cannot_guess_between_repeated_canonical_slots(self):
        """A correlated observer must not guess which repeated slot it fills."""
        identity = "SER-AMBIGUOUS"

        def target_provider(provider, node_id, index):
            """Build one indexed target without a config projection."""
            roles = tuple(
                ProviderRoleAssignment(
                    role,
                    "inverters",
                    index,
                    node_id,
                )
                for role in (AliasRole.PRIMARY, AliasRole.CONTROL)
            )
            return projection_snapshot(
                provider,
                (node_id,),
                roles,
                (),
                identity_aliases=(
                    ProviderIdentityAlias(
                        "serial",
                        identity,
                        node_id,
                    ),
                ),
            )

        observer = projection_snapshot(
            "observer",
            ("OBS1",),
            (),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "OBS1",
                            ProjectionValueKind.ENTITY,
                            "sensor.observer_battery_power",
                        ),
                    ),
                ),
            ),
            identity_aliases=(
                ProviderIdentityAlias(
                    "serial",
                    identity,
                    "OBS1",
                ),
            ),
        )

        with self.assertRaisesRegex(
            AutoConfigCompileError,
            "ambiguous across aggregate slots",
        ):
            compile_auto_config(
                (
                    target_provider("gateway", "GW1", 0),
                    target_provider("cloud", "CLOUD1", 1),
                    observer,
                )
            )

    def test_partial_indexed_user_override_covers_aggregate_shape(self):
        """A user override resolves the complete array and retains candidates."""
        gateway = partial_projection_snapshot(
            "gateway",
            "GW1",
            0,
            "battery_power",
            projection_value(
                "GW1",
                ProjectionValueKind.ENTITY,
                "sensor.gateway_candidate",
            ),
        )
        cloud = partial_projection_snapshot(
            "cloud",
            "CLOUD1",
            1,
            "battery_power",
            projection_value(
                "CLOUD1",
                ProjectionValueKind.ENTITY,
                "sensor.cloud_candidate",
            ),
        )
        override = UserConfigOverride(
            "battery_power",
            [
                "sensor.user_slot_0",
                "sensor.user_slot_1",
            ],
            "/apps.yaml/battery_power",
        )

        plan = compile_auto_config(
            (gateway, cloud),
            user_overrides=(override,),
        )
        argument = plan.config_arguments[0]

        self.assertEqual(
            plan.projected_config["battery_power"],
            (
                "sensor.user_slot_0",
                "sensor.user_slot_1",
            ),
        )
        self.assertEqual(
            {source.provider_id for source in argument.candidate_provenance},
            {"gateway", "cloud"},
        )
        self.assertEqual(
            [source.provider_id for source in argument.provenance],
            ["user_override"],
        )

    def test_optional_partial_projection_preserves_aggregate_hole(self):
        """An absent optional slot remains explicit None in the final array."""
        cloud_roles = tuple(
            ProviderRoleAssignment(
                role,
                "inverters",
                1,
                "CLOUD1",
            )
            for role in (AliasRole.PRIMARY, AliasRole.CONTROL)
        )
        gateway = partial_projection_snapshot(
            "gateway",
            "GW1",
            0,
            "pause_mode",
            projection_value(
                "GW1",
                ProjectionValueKind.NONE,
            ),
            required=False,
        )
        cloud = projection_snapshot(
            "cloud",
            ("CLOUD1",),
            cloud_roles,
            (),
        )

        plan = compile_auto_config((gateway, cloud))

        self.assertEqual(
            plan.projected_config["pause_mode"],
            (None, None),
        )
        self.assertEqual(
            {
                (
                    source.field_path,
                    source.provider_id,
                )
                for source in plan.config_arguments[0].candidate_provenance
            },
            {
                ("/projected_config/pause_mode/0", "gateway"),
            },
        )

    def test_gateway_multi_aio_projects_ordered_leaf_arrays(self):
        """Two Gateway-like AIO nodes produce deterministic per-index arrays."""
        nodes = ("AIO1", "AIO2")
        gateway = projection_snapshot(
            "gateway",
            nodes,
            indexed_roles(nodes),
            (
                config_projection(
                    "battery_power",
                    tuple(
                        projection_value(
                            node,
                            ProjectionValueKind.ENTITY,
                            "sensor.gateway_{}_battery_power".format(node.lower()),
                        )
                        for node in nodes
                    ),
                ),
                config_projection(
                    "inverter_type",
                    tuple(
                        projection_value(
                            node,
                            ProjectionValueKind.CONSTANT,
                            "GEC",
                        )
                        for node in nodes
                    ),
                ),
                config_projection(
                    "num_inverters",
                    (
                        projection_value(
                            nodes[0],
                            ProjectionValueKind.CONSTANT,
                            2,
                        ),
                    ),
                    cardinality=ProjectionCardinality.SCALAR,
                ),
            ),
        )

        plan = compile_auto_config((gateway,))

        self.assertEqual(
            dict(plan.projected_config),
            {
                "battery_power": (
                    "sensor.gateway_aio1_battery_power",
                    "sensor.gateway_aio2_battery_power",
                ),
                "inverter_type": ("GEC", "GEC"),
                "num_inverters": 2,
            },
        )
        self.assertEqual(
            [argument.name for argument in plan.config_arguments],
            ["battery_power", "inverter_type", "num_inverters"],
        )
        self.assertEqual(
            {field.name for field in plan.fields if field.name.startswith("config.")},
            {
                "config.battery_power",
                "config.inverter_type",
                "config.num_inverters",
            },
        )
        self.assertEqual(
            plan.materialization_readiness.blockers,
            ("atomic_materializer_missing",),
        )
        self.assertFalse(plan.materialization_readiness.ready)
        requests = []
        run = LatticeAutoConfigCompiler(
            {"gateway": MutableReader(gateway)},
            requests.append,
        ).drain()
        self.assertEqual(run.materializations, 0)
        self.assertEqual(requests, [])

    def test_ge_ems_coordinator_fans_out_entities_and_zero_constants(self):
        """One selected EMS coordinator can publish an ordered fan-out array."""
        role_assignments = (
            ProviderRoleAssignment(
                AliasRole.PRIMARY,
                "inverters",
                0,
                "BAT1",
            ),
            ProviderRoleAssignment(
                AliasRole.PRIMARY,
                "inverters",
                1,
                "BAT2",
            ),
            ProviderRoleAssignment(
                AliasRole.CONTROL,
                "inverters",
                0,
                "EMS",
            ),
            ProviderRoleAssignment(
                AliasRole.CONTROL,
                "inverters",
                1,
                "EMS",
            ),
        )
        ems = projection_snapshot(
            "gecloud",
            ("EMS", "BAT1", "BAT2"),
            role_assignments,
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "EMS",
                            ProjectionValueKind.ENTITY,
                            "sensor.gecloud_ems_battery_power",
                        ),
                        projection_value(
                            "EMS",
                            ProjectionValueKind.CONSTANT,
                            0,
                        ),
                    ),
                    role=AliasRole.CONTROL,
                    routing=ProjectionRouting.COORDINATOR,
                ),
                config_projection(
                    "charge_start_time",
                    (
                        projection_value(
                            "EMS",
                            ProjectionValueKind.ENTITY,
                            "select.gecloud_ems_charge_start",
                        ),
                        projection_value(
                            "EMS",
                            ProjectionValueKind.ENTITY,
                            "select.gecloud_ems_charge_start",
                        ),
                    ),
                    role=AliasRole.CONTROL,
                    routing=ProjectionRouting.COORDINATOR,
                ),
            ),
        )

        plan = compile_auto_config((ems,))

        self.assertEqual(
            plan.projected_config["battery_power"],
            ("sensor.gecloud_ems_battery_power", 0),
        )
        self.assertEqual(
            plan.projected_config["charge_start_time"],
            (
                "select.gecloud_ems_charge_start",
                "select.gecloud_ems_charge_start",
            ),
        )
        battery_argument = next(argument for argument in plan.config_arguments if argument.name == "battery_power")
        self.assertEqual(
            battery_argument.candidates[0].routing,
            ProjectionRouting.COORDINATOR.value,
        )

    def test_fox_projection_carries_transform_and_constant_flags(self):
        """Fox-like power entities retain transform metadata and invert flags."""
        fox = projection_snapshot(
            "fox",
            ("FOX1",),
            indexed_roles(("FOX1",)),
            (
                config_projection(
                    "grid_power",
                    (
                        projection_value(
                            "FOX1",
                            ProjectionValueKind.ENTITY,
                            "sensor.fox_fox1_grid_power",
                        ),
                    ),
                    transforms=("invert", "watts"),
                ),
                config_projection(
                    "grid_power_invert",
                    (
                        projection_value(
                            "FOX1",
                            ProjectionValueKind.CONSTANT,
                            True,
                        ),
                    ),
                ),
                config_projection(
                    "inverter_type",
                    (
                        projection_value(
                            "FOX1",
                            ProjectionValueKind.CONSTANT,
                            "FoxCloud",
                        ),
                    ),
                ),
            ),
        )

        plan = compile_auto_config((fox,))
        grid_argument = next(argument for argument in plan.config_arguments if argument.name == "grid_power")

        self.assertEqual(grid_argument.transforms, ("invert", "watts"))
        self.assertEqual(plan.projected_config["grid_power_invert"], (True,))
        self.assertEqual(plan.projected_config["inverter_type"], ("FoxCloud",))

    def test_solis_optional_projection_preserves_none_and_absence(self):
        """Solis-like optional args distinguish explicit None from no binding."""
        solis = projection_snapshot(
            "solis",
            ("SOLIS1",),
            indexed_roles(("SOLIS1",)),
            (
                config_projection(
                    "givtcp_rest",
                    (
                        projection_value(
                            "SOLIS1",
                            ProjectionValueKind.NONE,
                        ),
                    ),
                    cardinality=ProjectionCardinality.SCALAR,
                    required=False,
                ),
                config_projection(
                    "pause_mode",
                    (
                        projection_value(
                            "SOLIS1",
                            ProjectionValueKind.NONE,
                        ),
                    ),
                    required=False,
                ),
                config_projection(
                    "inverter_type",
                    (
                        projection_value(
                            "SOLIS1",
                            ProjectionValueKind.CONSTANT,
                            "SolisCloud",
                        ),
                    ),
                ),
            ),
        )

        plan = compile_auto_config((solis,))

        self.assertIsNone(plan.projected_config["givtcp_rest"])
        self.assertEqual(plan.projected_config["pause_mode"], (None,))
        self.assertNotIn("idle_start_time", plan.projected_config)

    def test_cross_provider_identity_and_access_path_select_one_value(self):
        """An explicit correlated access path beats a generic provider value."""
        gateway = projection_snapshot(
            "gateway",
            ("GW1",),
            indexed_roles(("GW1",)),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "GW1",
                            ProjectionValueKind.ENTITY,
                            "sensor.gateway_battery_power",
                        ),
                    ),
                ),
            ),
            identity_aliases=(ProviderIdentityAlias("serial", "SER123", "GW1"),),
        )
        cloud = projection_snapshot(
            "cloud",
            ("CLOUD1",),
            (),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "CLOUD1",
                            ProjectionValueKind.ENTITY,
                            "sensor.cloud_battery_power",
                            identity=("serial", "SER123"),
                            access_path_id="cloud-CLOUD1-path",
                        ),
                    ),
                ),
            ),
            identity_aliases=(
                ProviderIdentityAlias(
                    "serial",
                    "SER123",
                    "CLOUD1",
                ),
            ),
        )

        plan = compile_auto_config((gateway, cloud))
        argument = plan.config_arguments[0]

        self.assertEqual(
            plan.projected_config["battery_power"],
            ("sensor.cloud_battery_power",),
        )
        self.assertEqual(
            {candidate.provider_id for candidate in argument.candidates},
            {"gateway", "cloud"},
        )
        cloud_candidate = next(candidate for candidate in argument.candidates if candidate.provider_id == "cloud")
        self.assertEqual(
            cloud_candidate.capabilities,
            ("battery.target_soc",),
        )
        self.assertEqual(
            cloud_candidate.identity_selectors,
            (("serial", "SER123"),),
        )
        self.assertEqual(
            cloud_candidate.access_path_ids,
            ("cloud-CLOUD1-path",),
        )
        self.assertEqual(
            {source.provider_id for source in argument.candidate_provenance},
            {"gateway", "cloud"},
        )

    def test_user_override_wins_and_retains_provider_candidates(self):
        """An explicit override resolves values without erasing candidates."""
        identity = "SER-OVERRIDE"
        gateway = projection_snapshot(
            "gateway",
            ("GW1",),
            indexed_roles(("GW1",)),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "GW1",
                            ProjectionValueKind.ENTITY,
                            "sensor.gateway_candidate",
                        ),
                    ),
                ),
            ),
            identity_aliases=(ProviderIdentityAlias("serial", identity, "GW1"),),
        )
        cloud = projection_snapshot(
            "cloud",
            ("CLOUD1",),
            (),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "CLOUD1",
                            ProjectionValueKind.ENTITY,
                            "sensor.cloud_candidate",
                        ),
                    ),
                ),
            ),
            identity_aliases=(ProviderIdentityAlias("serial", identity, "CLOUD1"),),
        )
        override = UserConfigOverride(
            "battery_power",
            ["sensor.user_selected"],
            "/apps.yaml/battery_power",
        )

        plan = compile_auto_config(
            (gateway, cloud),
            user_overrides=(override,),
        )
        argument = plan.config_arguments[0]

        self.assertEqual(
            plan.projected_config["battery_power"],
            ("sensor.user_selected",),
        )
        self.assertEqual(argument.override_source, "/apps.yaml/battery_power")
        self.assertEqual(
            {source.provider_id for source in argument.candidate_provenance},
            {"gateway", "cloud"},
        )
        self.assertEqual(
            [source.provider_id for source in argument.provenance],
            ["user_override"],
        )

    def test_projection_order_and_outputs_are_deterministic_and_immutable(self):
        """Provider and declaration order cannot alter or mutate the plan."""
        projections = (
            config_projection(
                "inverter_type",
                (
                    projection_value(
                        "INV1",
                        ProjectionValueKind.CONSTANT,
                        "GEC",
                    ),
                ),
            ),
            config_projection(
                "battery_power",
                (
                    projection_value(
                        "INV1",
                        ProjectionValueKind.ENTITY,
                        "sensor.inv1_battery_power",
                    ),
                ),
            ),
        )
        left_snapshot = projection_snapshot(
            "gateway",
            ("INV1",),
            indexed_roles(("INV1",)),
            projections,
        )
        right_snapshot = projection_snapshot(
            "gateway",
            ("INV1",),
            indexed_roles(("INV1",)),
            tuple(reversed(projections)),
        )

        left = compile_auto_config((left_snapshot,))
        right = compile_auto_config((right_snapshot,))

        self.assertEqual(left.digest, right.digest)
        self.assertEqual(
            [argument.name for argument in left.config_arguments],
            ["battery_power", "inverter_type"],
        )
        with self.assertRaises(TypeError):
            left.projected_config["battery_power"] = ("changed",)
        with self.assertRaises(TypeError):
            left.projected_config["battery_power"][0] = "changed"

    def test_projection_invalidation_recompiles_a_new_provider_generation(self):
        """Any provider can invalidate and replace its projection generation."""

        def projected(generation, entity_id):
            """Build one generation of the provider's projected entity."""
            return projection_snapshot(
                "gateway",
                ("INV1",),
                indexed_roles(("INV1",)),
                (
                    config_projection(
                        "battery_power",
                        (
                            projection_value(
                                "INV1",
                                ProjectionValueKind.ENTITY,
                                entity_id,
                            ),
                        ),
                    ),
                ),
                generation=generation,
            )

        reader = MutableReader(projected(1, "sensor.gateway_battery_power_v1"))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        first = compiler.drain()

        reader.value = projected(2, "sensor.gateway_battery_power_v2")
        self.assertTrue(
            compiler.invalidate(
                "gateway",
                2,
                "projection binding changed",
            )
        )
        second = compiler.drain()

        self.assertNotEqual(first.plan.digest, second.plan.digest)
        self.assertEqual(
            second.plan.projected_config["battery_power"],
            ("sensor.gateway_battery_power_v2",),
        )
        self.assertEqual(
            dict(second.plan.provider_generations),
            {"gateway": 2},
        )

    def test_projection_gaps_types_conflicts_and_unrelated_nodes_fail_closed(self):
        """Unsafe required gaps, shapes, sources, and assertions are rejected."""
        roles = indexed_roles(("INV1", "INV2"))
        required_gap = projection_snapshot(
            "gateway",
            ("INV1", "INV2"),
            roles,
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "INV1",
                            ProjectionValueKind.ENTITY,
                            "sensor.inv1",
                        ),
                        projection_value(
                            "INV2",
                            ProjectionValueKind.NONE,
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(
            AutoConfigCompileError,
            "required slot",
        ):
            compile_auto_config((required_gap,))

        wrong_cardinality = projection_snapshot(
            "gateway",
            ("INV1", "INV2"),
            roles,
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "INV1",
                            ProjectionValueKind.ENTITY,
                            "sensor.inv1",
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(
            AutoConfigCompileError,
            "cardinality mismatch",
        ):
            compile_auto_config((wrong_cardinality,))

        unrelated = projection_snapshot(
            "gateway",
            ("INV1", "OTHER"),
            indexed_roles(("INV1",)),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "OTHER",
                            ProjectionValueKind.ENTITY,
                            "sensor.other",
                        ),
                    ),
                ),
            ),
        )
        with self.assertRaisesRegex(
            AutoConfigCompileError,
            "unrelated",
        ):
            compile_auto_config((unrelated,))

        identity = "SER-CONFLICT"
        gateway = projection_snapshot(
            "gateway",
            ("GW1",),
            indexed_roles(("GW1",)),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "GW1",
                            ProjectionValueKind.ENTITY,
                            "sensor.gateway",
                        ),
                    ),
                ),
            ),
            identity_aliases=(ProviderIdentityAlias("serial", identity, "GW1"),),
        )
        cloud = projection_snapshot(
            "cloud",
            ("CLOUD1",),
            (),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "CLOUD1",
                            ProjectionValueKind.ENTITY,
                            "sensor.cloud",
                        ),
                    ),
                ),
            ),
            identity_aliases=(ProviderIdentityAlias("serial", identity, "CLOUD1"),),
        )
        with self.assertRaisesRegex(
            AutoConfigCompileError,
            "ambiguous multi-provider",
        ):
            compile_auto_config((gateway, cloud))

        cloud_constant = projection_snapshot(
            "cloud",
            ("CLOUD1",),
            (),
            (
                config_projection(
                    "battery_power",
                    (
                        projection_value(
                            "CLOUD1",
                            ProjectionValueKind.CONSTANT,
                            0,
                        ),
                    ),
                ),
            ),
            identity_aliases=(ProviderIdentityAlias("serial", identity, "CLOUD1"),),
        )
        with self.assertRaisesRegex(
            AutoConfigCompileError,
            "type mismatch",
        ):
            compile_auto_config((gateway, cloud_constant))


class TestInvalidationStateMachine(unittest.TestCase):
    """Invalidations coalesce without losing freshness or last-known-good state."""

    def test_replayed_and_stale_generations_are_rejected(self):
        """A generation is accepted once and never rolls backwards."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})

        self.assertTrue(compiler.invalidate("gateway", 1, "first telemetry"))
        self.assertFalse(compiler.invalidate("gateway", 1, "duplicate"))
        self.assertFalse(compiler.invalidate("gateway", 0, "stale"))
        run = compiler.drain()
        self.assertEqual(run.attempts, 1)
        self.assertFalse(compiler.invalidate("gateway", 1, "observed replay"))

    def test_burst_of_ten_simultaneous_invalidations_coalesces(self):
        """Ten integrations invalidating together need only one all-provider read."""
        readers = {name: MutableReader(snapshot(name, generation=1, node_id="INV1")) for name in ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")}
        compiler = LatticeAutoConfigCompiler(readers)
        barrier = threading.Barrier(len(readers))
        accepted = []

        def invalidate(name):
            """Release the burst together and record acceptance."""
            barrier.wait()
            accepted.append(compiler.invalidate(name, 1, "simultaneous discovery"))

        threads = [threading.Thread(target=invalidate, args=(name,)) for name in readers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        run = compiler.drain()
        self.assertTrue(all(accepted))
        self.assertEqual(run.attempts, 1)
        self.assertLessEqual(run.attempts, 2)
        self.assertTrue(all(reader.calls == 1 for reader in readers.values()))

    def test_invalidation_during_compile_guarantees_one_fresh_follow_up(self):
        """A mid-compile generation change is included by one bounded follow-up."""
        entered = threading.Event()
        release = threading.Event()
        state = {"value": snapshot("gateway", generation=1), "calls": 0}

        def reader():
            """Block only the first read after capturing its generation."""
            state["calls"] += 1
            value = state["value"]
            if state["calls"] == 1:
                entered.set()
                release.wait(5)
            return value

        requests = []
        compiler = LatticeAutoConfigCompiler({"gateway": reader}, requests.append)
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("run", compiler.drain()))
        worker.start()
        self.assertTrue(entered.wait(5))
        state["value"] = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "new device"))
        release.set()
        worker.join(5)

        run = result["run"]
        self.assertEqual(run.attempts, 2)
        self.assertEqual(state["calls"], 2)
        self.assertEqual(dict(run.plan.provider_generations), {"gateway": 2})
        self.assertEqual(requests, [])
        self.assertEqual(run.materializations, 0)
        self.assertFalse(run.plan.materialization_readiness.ready)
        self.assertFalse(run.pending)

    def test_all_accepted_invalidation_causes_survive_coalescing(self):
        """Execution coalesces while distinct provider causes remain auditable."""
        reader = MutableReader(snapshot("gateway", generation=2))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})

        self.assertTrue(compiler.invalidate("gateway", 1, "health changed"))
        self.assertTrue(compiler.invalidate("gateway", 2, "topology changed"))
        run = compiler.drain()

        self.assertEqual(
            {(item.provider_id, item.generation, item.reason) for item in run.invalidations},
            {
                ("gateway", 1, "health changed"),
                ("gateway", 2, "topology changed"),
            },
        )
        self.assertEqual(run.attempts, 1)

    def test_single_flight_rejects_a_concurrent_drain(self):
        """Only one caller can own the compile flight."""
        entered = threading.Event()
        release = threading.Event()

        def reader():
            """Hold the active flight until the competing drain returns."""
            entered.set()
            release.wait(5)
            return snapshot("gateway")

        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("run", compiler.drain()))
        worker.start()
        self.assertTrue(entered.wait(5))
        concurrent = compiler.drain()
        release.set()
        worker.join(5)

        self.assertEqual(concurrent.attempts, 0)
        self.assertEqual(result["run"].attempts, 1)

    def test_provider_failures_and_offline_health_are_isolated(self):
        """Bad and offline producers do not erase a healthy provider's plan."""
        good = MutableReader(snapshot("gateway"))
        offline = MutableReader(snapshot("cloud", health=ProviderHealth.OFFLINE))

        def broken():
            """Simulate an integration-local reader failure."""
            raise RuntimeError("cloud API failed")

        compiler = LatticeAutoConfigCompiler({"gateway": good, "cloud": offline, "broken": broken})
        run = compiler.drain()

        self.assertEqual(run.status, CompileStatus.DEGRADED)
        self.assertEqual(dict(run.plan.provider_generations), {"gateway": 1})
        self.assertEqual({issue.code for issue in run.issues}, {"provider_offline", "provider_read_failed"})
        self.assertEqual((good.calls, offline.calls), (1, 1))

    def test_malformed_provider_is_isolated(self):
        """Malformed topology from one integration does not poison healthy input."""
        malformed = ProviderSnapshot("cloud", 1, ProviderHealth.HEALTHY, {"not": "topology"})
        compiler = LatticeAutoConfigCompiler({"gateway": MutableReader(snapshot("gateway")), "cloud": MutableReader(malformed)})
        run = compiler.drain()

        self.assertEqual(run.status, CompileStatus.DEGRADED)
        self.assertEqual(dict(run.plan.provider_generations), {"gateway": 1})
        self.assertIn("provider_invalid", {issue.code for issue in run.issues})

    def test_compile_failure_preserves_last_known_good_as_stale(self):
        """A later global collision leaves the prior immutable plan active."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        first = compiler.drain()
        last_known_good = first.plan

        bad_fragment = fragment("gateway", 2)
        bad_fragment["nodes"].append(dict(bad_fragment["nodes"][0]))
        reader.value = ProviderSnapshot("gateway", 2, ProviderHealth.HEALTHY, bad_fragment)
        self.assertTrue(compiler.invalidate("gateway", 2, "conflicting rediscovery"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.plan, last_known_good)
        self.assertIn("compile_failed", {issue.code for issue in failed.issues})

    def test_all_offline_preserves_last_known_good_as_stale(self):
        """Temporary total provider loss cannot replace a working plan."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        last_known_good = compiler.drain().plan

        reader.value = snapshot("gateway", generation=2, health=ProviderHealth.OFFLINE)
        self.assertTrue(compiler.invalidate("gateway", 2, "connection lost"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.plan, last_known_good)
        self.assertEqual(
            {issue.code for issue in failed.issues},
            {"provider_offline", "active_provider_unavailable", "compile_failed"},
        )
        self.assertTrue(failed.pending)

    def test_unavailable_active_provider_cannot_materialize_destructive_removal(self):
        """A transient provider outage keeps the complete last-known-good plan."""
        gateway = MutableReader(snapshot("gateway", generation=1, node_id="GW1"))
        cloud = MutableReader(snapshot("cloud", generation=1, node_id="CLOUD1"))
        requests = []
        compiler = LatticeAutoConfigCompiler(
            {"gateway": gateway, "cloud": cloud},
            requests.append,
        )
        first = compiler.drain()
        last_known_good = first.plan

        cloud.value = snapshot(
            "cloud",
            generation=2,
            node_id="CLOUD1",
            health=ProviderHealth.OFFLINE,
        )
        self.assertTrue(compiler.invalidate("cloud", 2, "cloud unavailable"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.plan, last_known_good)
        self.assertEqual(len(requests), 0)
        self.assertEqual(
            set(dict(failed.plan.provider_generations)),
            {"gateway", "cloud"},
        )
        self.assertIn(
            "active_provider_unavailable",
            {issue.code for issue in failed.issues},
        )
        self.assertTrue(failed.pending)

    def test_shadow_only_plan_never_invokes_materializer(self):
        """A not-ready plan remains observable without reaching a write callback."""
        reader = MutableReader(snapshot("gateway", generation=1))
        requests = []

        def materialize(request):
            """Record any unsafe hand-off to make the test fail."""
            requests.append(request)

        compiler = LatticeAutoConfigCompiler({"gateway": reader}, materialize)
        run = compiler.drain()

        self.assertEqual(run.status, CompileStatus.FRESH)
        self.assertIsNotNone(run.plan)
        self.assertFalse(run.plan.materialization_readiness.ready)
        self.assertEqual(run.materializations, 0)
        self.assertFalse(run.pending)
        self.assertEqual(requests, [])

    def test_unchanged_digest_skips_materialization(self):
        """A newer generation with identical semantics updates provenance only."""
        reader = MutableReader(snapshot("gateway", generation=1))
        requests = []
        compiler = LatticeAutoConfigCompiler({"gateway": reader}, requests.append)
        first = compiler.drain()

        reader.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "heartbeat refresh"))
        second = compiler.drain()

        self.assertEqual(first.materializations, 0)
        self.assertEqual(second.materializations, 0)
        self.assertEqual(len(requests), 0)
        self.assertEqual(first.plan.digest, second.plan.digest)
        self.assertEqual(dict(second.plan.provider_generations), {"gateway": 2})

    def test_shadow_plan_cannot_create_materializer_feedback(self):
        """A blocked hand-off cannot cause an integration feedback loop."""
        reader = MutableReader(snapshot("gateway", generation=1))
        feedback_results = []
        holder = {}

        def materialize(request):
            """Echo the materializer token through the provider invalidation API."""
            feedback_results.append(holder["compiler"].invalidate("gateway", 2, "materialized config observed", request.feedback_token))

        compiler = LatticeAutoConfigCompiler({"gateway": reader}, materialize)
        holder["compiler"] = compiler
        run = compiler.drain()

        self.assertEqual(feedback_results, [])
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.materializations, 0)
        self.assertFalse(run.pending)

    def test_ready_plan_retries_after_materializer_failure(self):
        """A failed hand-off is not treated as a successful materialization."""
        requests = []

        def materialize(request):
            """Fail the first hand-off and accept the retry."""
            requests.append(request)
            if len(requests) == 1:
                raise RuntimeError("temporary write failure")

        compiler = LatticeAutoConfigCompiler(materializer=materialize)
        plan = ready_plan()

        count, issues = compiler._materialize_if_changed(plan)
        self.assertEqual(count, 0)
        self.assertEqual([issue.code for issue in issues], ["materialization_failed"])

        count, issues = compiler._materialize_if_changed(plan)
        self.assertEqual(count, 1)
        self.assertEqual(issues, ())
        self.assertEqual(len(requests), 2)

    def test_ready_plan_unchanged_digest_skips_materialization(self):
        """Bookkeeping-only generation changes do not repeat a ready hand-off."""
        requests = []
        compiler = LatticeAutoConfigCompiler(materializer=requests.append)
        plan = ready_plan()

        count, issues = compiler._materialize_if_changed(plan)
        self.assertEqual((count, issues), (1, ()))
        compiler._active_plan = plan
        newer_generation = replace(
            plan,
            provider_generations=(("gateway", 2),),
        )

        count, issues = compiler._materialize_if_changed(newer_generation)
        self.assertEqual((count, issues), (0, ()))
        self.assertEqual(len(requests), 1)

    def test_ready_plan_suppresses_materializer_feedback_token(self):
        """A ready hand-off cannot invalidate itself through its feedback token."""
        feedback_results = []
        holder = {}

        def materialize(request):
            """Echo the hand-off token through the provider invalidation API."""
            feedback_results.append(
                holder["compiler"].invalidate(
                    "gateway",
                    2,
                    "materialized config observed",
                    request.feedback_token,
                )
            )

        compiler = LatticeAutoConfigCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            materialize,
        )
        holder["compiler"] = compiler

        count, issues = compiler._materialize_if_changed(ready_plan())

        self.assertEqual((count, issues), (1, ()))
        self.assertEqual(feedback_results, [False])
        self.assertTrue(
            compiler.invalidate(
                "gateway",
                2,
                "independent provider change",
            )
        )

    def test_every_attempt_fresh_reads_all_providers(self):
        """Independent invalidations still re-read the complete provider set."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        cloud = MutableReader(snapshot("cloud", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": gateway, "cloud": cloud})
        compiler.drain()

        gateway.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "new gateway topology"))
        compiler.drain()

        self.assertEqual((gateway.calls, cloud.calls), (2, 2))


if __name__ == "__main__":
    unittest.main()

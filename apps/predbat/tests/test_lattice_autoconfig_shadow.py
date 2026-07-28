"""Tests for pure, default-off Lattice auto-config shadow comparison."""

# cspell:ignore autoconfig materializable

import os
import sys
import unittest
from dataclasses import FrozenInstanceError, replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileIssue,
    CompileStatus,
    MaterializationReadiness,
    ProjectionValueKind,
)
from lattice_autoconfig_shadow import (  # noqa: E402
    DEFAULT_COMPILER_VERSION,
    LegacyAutoConfigObservation,
    ShadowCanaryGates,
    ShadowComparisonLimits,
    ShadowDivergenceReason,
    compare_autoconfig_shadow,
    compiled_autoconfig_shadow_document,
)
from lattice_compiled_publication import (  # noqa: E402
    CompiledLatticeCompiler,
    CompiledLatticeDiagnostics,
    InMemoryCompiledLatticeStateStore,
)
from tests.test_lattice_autoconfig import (  # noqa: E402
    MutableReader,
    config_projection,
    indexed_roles,
    projection_snapshot,
    projection_value,
    snapshot,
)


def projected_snapshot(generation=1, required=True, value=None):
    """Build one provider with an effective entity projection."""
    if value is None:
        value = projection_value(
            "GW1",
            ProjectionValueKind.ENTITY,
            "sensor.gateway_soc",
            capability="battery.target_soc",
        )
    return projection_snapshot(
        "gateway",
        ("GW1",),
        indexed_roles(("GW1",)),
        (
            config_projection(
                "soc_kw",
                (value,),
                role=AliasRole.PRIMARY,
                required=required,
            ),
        ),
        generation=generation,
    )


def compile_publication(provider_snapshot=None):
    """Publish one immutable Lattice plan for comparison tests."""
    reader = MutableReader(provider_snapshot or projected_snapshot())
    compiler = CompiledLatticeCompiler(
        {"gateway": reader},
        state_store=InMemoryCompiledLatticeStateStore(),
    )
    run = compiler.drain()
    return compiler, reader, run


def matching_observation(publication, hub_id="hub-1", capabilities=None):
    """Mirror a publication into an independently frozen legacy observation."""
    document = compiled_autoconfig_shadow_document(publication)
    targets = document["targets"]
    return LegacyAutoConfigObservation(
        hub_id=hub_id,
        projected_config=document["projected_config"],
        primary_target=targets["primary_target"],
        control_target=targets["control_target"],
        primary_targets=targets["primary_targets"],
        control_targets=targets["control_targets"],
        routing=document["routing"],
        provenance=document["provenance"],
        capabilities=({"battery.target_soc"} if capabilities is None else capabilities),
    )


def fully_open_gates():
    """Allow only the focused test hub, provider, and capability."""
    return ShadowCanaryGates(
        enabled=True,
        hub_ids={"hub-1"},
        provider_ids={"gateway"},
        capability_ids={"battery.target_soc"},
    )


def materializable(publication):
    """Model the future materializer tranche without changing this module."""
    readiness = MaterializationReadiness(True, ())
    return replace(
        publication,
        plan=replace(
            publication.plan,
            materialization_readiness=readiness,
        ),
        diagnostics=CompiledLatticeDiagnostics(
            CompileStatus.FRESH,
            False,
            False,
            (),
            readiness,
        ),
    )


class TestShadowComparison(unittest.TestCase):
    """Shadow comparison is semantic, deterministic, bounded, and read-only."""

    def test_matching_paths_emit_metadata_but_default_gates_stay_closed(self):
        """An exact match cannot bypass the default-off canary gate."""
        _compiler, _reader, run = compile_publication()
        report = compare_autoconfig_shadow(
            run.publication,
            matching_observation(run.publication),
            run=run,
        )

        self.assertTrue(report.matches)
        self.assertEqual(report.divergences, ())
        self.assertEqual(report.compiler_version, DEFAULT_COMPILER_VERSION)
        self.assertEqual(report.lattice_version, 1)
        self.assertEqual(
            tuple((item.provider_id, item.generation) for item in report.provider_inputs),
            (("gateway", 1),),
        )
        self.assertEqual(report.required_capabilities, ("battery.target_soc",))
        self.assertFalse(report.canary.ready)
        self.assertIn("shadow_disabled", report.canary.blockers)
        self.assertIn(
            "materialization:atomic_materializer_missing",
            report.canary.blockers,
        )

    def test_list_tuple_and_integer_float_forms_are_semantically_equal(self):
        """Container representation and JSON-number spelling do not diverge."""
        _compiler, _reader, run = compile_publication(
            projected_snapshot(
                value=projection_value(
                    "GW1",
                    ProjectionValueKind.CONSTANT,
                    5,
                )
            )
        )
        document = compiled_autoconfig_shadow_document(run.publication)
        targets = document["targets"]
        observation = LegacyAutoConfigObservation(
            "hub-1",
            {"soc_kw": [5.0]},
            primary_target=targets["primary_target"],
            control_target=targets["control_target"],
            primary_targets=list(targets["primary_targets"]),
            control_targets=list(targets["control_targets"]),
            routing={key: list(value) for key, value in document["routing"].items()},
            provenance={key: list(value) for key, value in document["provenance"].items()},
        )

        report = compare_autoconfig_shadow(run.publication, observation)

        self.assertTrue(report.matches)

    def test_optional_all_none_may_be_absent_but_required_may_not(self):
        """Only compiler-declared optional None projections collapse to missing."""
        none_value = projection_value("GW1", ProjectionValueKind.NONE)
        _compiler, _reader, optional_run = compile_publication(projected_snapshot(required=False, value=none_value))
        optional_document = compiled_autoconfig_shadow_document(optional_run.publication)
        optional_targets = optional_document["targets"]
        optional_observation = LegacyAutoConfigObservation(
            "hub-1",
            {},
            primary_target=optional_targets["primary_target"],
            control_target=optional_targets["control_target"],
            primary_targets=optional_targets["primary_targets"],
            control_targets=optional_targets["control_targets"],
            routing=optional_document["routing"],
            provenance=optional_document["provenance"],
        )

        optional_report = compare_autoconfig_shadow(
            optional_run.publication,
            optional_observation,
        )

        self.assertTrue(optional_report.matches)

        required_plan = replace(
            optional_run.publication.plan,
            config_arguments=tuple(replace(argument, required=True) for argument in optional_run.publication.plan.config_arguments),
        )
        required_publication = replace(
            optional_run.publication,
            plan=required_plan,
        )
        required_document = compiled_autoconfig_shadow_document(required_publication)
        required_targets = required_document["targets"]
        required_observation = LegacyAutoConfigObservation(
            "hub-1",
            {},
            primary_target=required_targets["primary_target"],
            control_target=required_targets["control_target"],
            primary_targets=required_targets["primary_targets"],
            control_targets=required_targets["control_targets"],
            routing=required_document["routing"],
            provenance=required_document["provenance"],
        )

        required_report = compare_autoconfig_shadow(
            required_publication,
            required_observation,
        )

        self.assertFalse(required_report.matches)
        self.assertEqual(
            required_report.divergences[0].path,
            "/projected_config/soc_kw",
        )
        self.assertEqual(
            required_report.divergences[0].reason,
            ShadowDivergenceReason.MISSING_LEGACY,
        )

    def test_absent_empty_targets_and_empty_sections_are_equivalent(self):
        """Legacy omission is safe only for semantically empty target sections."""
        _compiler, _reader, run = compile_publication(snapshot("gateway"))
        document = compiled_autoconfig_shadow_document(run.publication)
        observation = LegacyAutoConfigObservation(
            "hub-1",
            {},
            provenance=document["provenance"],
        )

        report = compare_autoconfig_shadow(run.publication, observation)

        self.assertTrue(report.matches)

    def test_value_type_and_extra_field_reasons_are_ordered(self):
        """Reason paths are stable regardless of legacy mapping insertion order."""
        _compiler, _reader, run = compile_publication()
        document = compiled_autoconfig_shadow_document(run.publication)
        targets = document["targets"]
        observation = LegacyAutoConfigObservation(
            "hub-1",
            {"z_extra": 1, "soc_kw": {"wrong": True}},
            primary_target="wrong-node",
            control_target=targets["control_target"],
            primary_targets=targets["primary_targets"],
            control_targets=targets["control_targets"],
            routing=document["routing"],
            provenance=document["provenance"],
        )

        report = compare_autoconfig_shadow(run.publication, observation)

        self.assertEqual(
            tuple((item.path, item.reason.value) for item in report.divergences),
            (
                ("/projected_config/soc_kw", "type_mismatch"),
                ("/projected_config/z_extra", "missing_lattice"),
                ("/targets/primary_target", "type_mismatch"),
            ),
        )

    def test_routing_and_provenance_are_compared_independently(self):
        """Routing and attribution drift retain their exact paths."""
        _compiler, _reader, run = compile_publication()
        document = compiled_autoconfig_shadow_document(run.publication)
        targets = document["targets"]
        routing = {key: list(value) for key, value in document["routing"].items()}
        routing["soc_kw"][0] = dict(routing["soc_kw"][0])
        routing["soc_kw"][0]["routing"] = "coordinator"
        provenance = {key: list(value) for key, value in document["provenance"].items()}
        field_path = sorted(provenance)[0]
        provenance[field_path][0] = dict(provenance[field_path][0])
        provenance[field_path][0]["generation"] = 99
        observation = LegacyAutoConfigObservation(
            "hub-1",
            document["projected_config"],
            primary_target=targets["primary_target"],
            control_target=targets["control_target"],
            primary_targets=targets["primary_targets"],
            control_targets=targets["control_targets"],
            routing=routing,
            provenance=provenance,
        )

        report = compare_autoconfig_shadow(run.publication, observation)

        paths = tuple(item.path for item in report.divergences)
        self.assertIn("/routing/soc_kw/0/routing", paths)
        self.assertTrue(any(path.startswith("/provenance/") for path in paths))

    def test_inputs_are_detached_and_neither_path_is_mutated(self):
        """Caller mutation after observation cannot alter comparison state."""
        _compiler, _reader, run = compile_publication()
        document = compiled_autoconfig_shadow_document(run.publication)
        targets = document["targets"]
        projected = {"soc_kw": ["sensor.gateway_soc"]}
        target_list = [dict(targets["primary_targets"][0])]
        observation = LegacyAutoConfigObservation(
            "hub-1",
            projected,
            primary_target=targets["primary_target"],
            control_target=targets["control_target"],
            primary_targets=target_list,
            control_targets=targets["control_targets"],
            routing=document["routing"],
            provenance=document["provenance"],
            capabilities={"battery.target_soc"},
        )
        projected["soc_kw"][0] = "sensor.mutated"
        target_list[0]["node_id"] = "mutated"

        report = compare_autoconfig_shadow(run.publication, observation)

        self.assertTrue(report.matches)
        self.assertEqual(
            observation.projected_config["soc_kw"][0],
            "sensor.gateway_soc",
        )
        with self.assertRaises(FrozenInstanceError):
            report.matches = False

    def test_divergence_count_summary_and_paths_are_bounded(self):
        """Large disagreement emits a deterministic retained prefix only."""
        _compiler, _reader, run = compile_publication()
        document = compiled_autoconfig_shadow_document(run.publication)
        targets = document["targets"]
        projected = {"extra-{}-{}".format(index, "x" * 120): "y" * 200 for index in range(20)}
        observation = LegacyAutoConfigObservation(
            "hub-1",
            projected,
            primary_target=targets["primary_target"],
            control_target=targets["control_target"],
            primary_targets=targets["primary_targets"],
            control_targets=targets["control_targets"],
            routing=document["routing"],
            provenance=document["provenance"],
        )
        limits = ShadowComparisonLimits(
            max_divergences=3,
            max_nodes=1000,
            max_depth=32,
            max_path_length=40,
            max_summary_length=24,
        )

        report = compare_autoconfig_shadow(
            run.publication,
            observation,
            limits=limits,
        )

        self.assertGreater(report.divergence_count, 3)
        self.assertEqual(len(report.divergences), 3)
        self.assertTrue(report.truncated)
        self.assertTrue(all(len(item.path) <= 40 for item in report.divergences))
        self.assertTrue(all(len(item.legacy_summary) <= 24 and len(item.lattice_summary) <= 24 for item in report.divergences))

    def test_comparison_budget_is_a_fail_closed_divergence(self):
        """Depth or node exhaustion can never produce a false match."""
        _compiler, _reader, run = compile_publication()
        observation = matching_observation(run.publication)

        report = compare_autoconfig_shadow(
            run.publication,
            observation,
            limits=ShadowComparisonLimits(max_nodes=1),
        )

        self.assertFalse(report.matches)
        self.assertTrue(report.truncated)
        self.assertEqual(
            report.divergences[0].reason,
            ShadowDivergenceReason.COMPARISON_LIMIT,
        )


class TestCanaryReadiness(unittest.TestCase):
    """Canary readiness is explicit, default-off, and fail closed."""

    def test_all_gates_must_pass_for_a_materializable_match(self):
        """A fully allowlisted, healthy, ready match is the sole ready state."""
        _compiler, _reader, run = compile_publication()
        publication = materializable(run.publication)

        report = compare_autoconfig_shadow(
            publication,
            matching_observation(publication),
            gates=fully_open_gates(),
        )

        self.assertTrue(report.canary.ready)
        self.assertEqual(report.canary.blockers, ())
        self.assertTrue(all(item.passed for item in report.canary.decisions))

    def test_hub_provider_and_capability_gates_report_distinct_blockers(self):
        """Each explicit rollout dimension is independently observable."""
        _compiler, _reader, run = compile_publication()
        publication = materializable(run.publication)
        gates = ShadowCanaryGates(
            enabled=True,
            hub_ids={"other-hub"},
            provider_ids={"other-provider"},
            capability_ids={"other.capability"},
        )
        observation = matching_observation(publication, capabilities=set())

        report = compare_autoconfig_shadow(
            publication,
            observation,
            gates=gates,
        )

        self.assertEqual(
            report.canary.blockers,
            (
                "hub_not_allowed:hub-1",
                "provider_not_allowed:gateway",
                "capability_not_allowed:battery.target_soc",
                "capability_unavailable:battery.target_soc",
            ),
        )

    def test_stale_and_degraded_live_diagnostics_are_emitted_and_block(self):
        """Live compiler health may block a still-immutable LKG publication."""
        _compiler, _reader, run = compile_publication()
        publication = materializable(run.publication)
        stale = CompiledLatticeDiagnostics(
            CompileStatus.STALE,
            True,
            False,
            (CompileIssue("provider_invalid", "reader failed", "gateway"),),
            publication.plan.materialization_readiness,
        )

        stale_report = compare_autoconfig_shadow(
            publication,
            matching_observation(publication),
            gates=fully_open_gates(),
            diagnostics=stale,
        )

        self.assertTrue(stale_report.stale)
        self.assertFalse(stale_report.degraded)
        self.assertIn("compiler_stale", stale_report.canary.blockers)

        degraded = CompiledLatticeDiagnostics(
            CompileStatus.DEGRADED,
            False,
            True,
            (CompileIssue("provider_offline", "cloud offline", "cloud"),),
            publication.plan.materialization_readiness,
        )
        degraded_report = compare_autoconfig_shadow(
            publication,
            matching_observation(publication),
            gates=fully_open_gates(),
            diagnostics=degraded,
        )

        self.assertFalse(degraded_report.stale)
        self.assertTrue(degraded_report.degraded)
        self.assertIn("compiler_degraded", degraded_report.canary.blockers)

    def test_shadow_divergence_only_changes_report_not_inputs(self):
        """Disagreement closes readiness without any mutation callback surface."""
        _compiler, _reader, run = compile_publication()
        publication = materializable(run.publication)
        observation = matching_observation(publication)
        mismatched = replace(
            observation,
            projected_config={"soc_kw": ["sensor.wrong"]},
        )

        report = compare_autoconfig_shadow(
            publication,
            mismatched,
            gates=fully_open_gates(),
        )

        self.assertFalse(report.canary.ready)
        self.assertIn("shadow_divergence", report.canary.blockers)
        self.assertEqual(
            publication.plan.projected_config["soc_kw"][0],
            "sensor.gateway_soc",
        )
        self.assertEqual(
            mismatched.projected_config["soc_kw"][0],
            "sensor.wrong",
        )


class TestInvalidationTelemetry(unittest.TestCase):
    """Invalidation-triggered compiles are distinct from semantic comparison."""

    def test_causes_coalescing_and_provider_cursor_changes_are_reported(self):
        """Two integration invalidations retain identity, generation, and digest."""
        gateway = MutableReader(snapshot("gateway", generation=1, node_id="GW1"))
        cloud = MutableReader(snapshot("cloud", generation=1, node_id="CLOUD1"))
        compiler = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud},
            state_store=InMemoryCompiledLatticeStateStore(),
        )
        first = compiler.drain()
        gateway.value = snapshot("gateway", generation=2, node_id="GW1")
        cloud.value = snapshot("cloud", generation=2, node_id="CLOUD1")
        self.assertTrue(compiler.invalidate("gateway", 2, "local refresh"))
        self.assertTrue(compiler.invalidate("cloud", 2, "cloud refresh"))
        second = compiler.drain()

        report = compare_autoconfig_shadow(
            second.publication,
            matching_observation(
                second.publication,
                capabilities=set(),
            ),
            run=second,
            previous_publication=first.publication,
        )

        self.assertTrue(report.recompile.triggered)
        self.assertTrue(report.recompile.coalesced)
        self.assertEqual(report.recompile.attempts, 1)
        self.assertFalse(report.recompile.bounded_follow_up_used)
        self.assertEqual(
            tuple((cause.source_id, cause.generation, cause.reason) for cause in report.recompile.causes),
            (
                ("cloud", 2, "cloud refresh"),
                ("gateway", 2, "local refresh"),
            ),
        )
        self.assertEqual(
            tuple(
                (
                    item.provider_id,
                    item.previous_generation,
                    item.generation,
                    item.generation_changed,
                    item.fingerprint_changed,
                )
                for item in report.provider_inputs
            ),
            (
                ("cloud", 1, 2, True, True),
                ("gateway", 1, 2, True, True),
            ),
        )

    def test_bounded_follow_up_and_remaining_pending_work_are_visible(self):
        """Two-attempt drain exhaustion is never hidden by an exact match."""
        compiler, reader, first = compile_publication()
        reader.value = projected_snapshot(generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "provider refresh"))
        second = compiler.drain()
        bounded = replace(second, attempts=2, pending=True)

        report = compare_autoconfig_shadow(
            bounded.publication,
            matching_observation(bounded.publication),
            run=bounded,
            previous_publication=first.publication,
        )

        self.assertTrue(report.recompile.bounded_follow_up_used)
        self.assertTrue(report.recompile.follow_up_pending)
        self.assertIn(
            "compiler_follow_up_pending",
            report.canary.blockers,
        )

    def test_unobserved_integration_generation_is_reported_and_blocks(self):
        """A newly invalidated integration cannot hide outside the observed cursor."""
        _compiler, _reader, run = compile_publication()
        publication = materializable(
            replace(
                run.publication,
                provider_requested_generations=(
                    ("cloud", 2),
                    ("gateway", 1),
                ),
            )
        )

        report = compare_autoconfig_shadow(
            publication,
            matching_observation(publication),
            gates=fully_open_gates(),
        )

        cloud = report.provider_inputs[0]
        self.assertEqual(cloud.provider_id, "cloud")
        self.assertIsNone(cloud.generation)
        self.assertIsNone(cloud.fingerprint)
        self.assertEqual(cloud.requested_generation, 2)
        self.assertTrue(cloud.observation_pending)
        self.assertIn(
            "provider_generation_unobserved:cloud:2",
            report.canary.blockers,
        )

    def test_live_run_diagnostics_are_used_without_duplicate_argument(self):
        """A stale live run cannot inherit the immutable publication's fresh status."""
        _compiler, _reader, run = compile_publication()
        stale = CompiledLatticeDiagnostics(
            CompileStatus.STALE,
            True,
            False,
            (CompileIssue("provider_invalid", "reader failed", "gateway"),),
            run.publication.plan.materialization_readiness,
        )
        stale_run = replace(run, diagnostics=stale)

        report = compare_autoconfig_shadow(
            stale_run.publication,
            matching_observation(stale_run.publication),
            run=stale_run,
        )

        self.assertTrue(report.stale)
        self.assertEqual(report.status, CompileStatus.STALE)
        self.assertIn("compiler_stale", report.canary.blockers)

    def test_run_must_belong_to_the_compared_publication(self):
        """Telemetry cannot accidentally pair causes with another version."""
        _compiler, _reader, first = compile_publication()
        _other_compiler, _other_reader, other = compile_publication()
        other_publication = replace(other.publication, lattice_version=2)

        with self.assertRaisesRegex(ValueError, "run publication"):
            compare_autoconfig_shadow(
                other_publication,
                matching_observation(other_publication),
                run=first,
            )


if __name__ == "__main__":
    unittest.main()

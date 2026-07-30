"""Tests for detached, default-off Lattice shadow runtime composition."""

# cspell:ignore autoconfig fffe fspath multibyte noncanonical noncharacters ufdd

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_atomic_materializer import MaterializationStatus  # noqa: E402
from lattice_autoconfig_shadow import (  # noqa: E402
    LegacyAutoConfigObservation,
    compiled_autoconfig_shadow_document,
)
from lattice_fragment_adapters import DurableFragmentAdapter  # noqa: E402
from lattice_runtime_composition import (  # noqa: E402
    LatticeRuntimeConfig,
    LatticeRuntimePhase,
    MAX_OBSERVATION_CAUSES,
    MAX_OBSERVATION_DETAIL_BYTES,
    MAX_OBSERVATION_TOTAL_TEXT_BYTES,
    RuntimeFragmentSource,
    build_lattice_shadow_runtime,
)
from tests.test_lattice_autoconfig import (  # noqa: E402
    config_projection,
    indexed_roles,
    projection_snapshot,
    projection_value,
    snapshot,
)
from lattice_autoconfig import ProjectionValueKind  # noqa: E402


class FragmentComponent:
    """Test component exposing one existing generic fragment adapter."""

    def __init__(self, adapter):
        """Retain one adapter for structural registry discovery."""
        self.adapter = adapter

    def lattice_fragment_adapter(self):
        """Expose the adapter through the production discovery method."""
        return self.adapter


class RaisingIterable:
    """Disabled-path probe that must never be enumerated."""

    def __iter__(self):
        """Fail if a disabled runtime inspects fragment sources."""
        raise AssertionError("disabled runtime enumerated sources")


class RaisingPath:
    """Disabled-path probe that must never be converted to a path."""

    def __fspath__(self):
        """Fail if a disabled runtime touches configured paths."""
        raise AssertionError("disabled runtime touched a path")


def source_for(provider_id, initial_snapshot, holder, order=None):
    """Build one generic source whose state is durable across restarts."""

    def factory(state_store):
        """Hydrate one existing source before registry membership is sealed."""
        if order is not None:
            order.append(provider_id)
        adapter = DurableFragmentAdapter(provider_id, state_store)
        if state_store.load() is None:
            adapter.publish(initial_snapshot, "initial-hydration")
        holder[provider_id] = adapter
        return FragmentComponent(adapter)

    return RuntimeFragmentSource(provider_id, factory)


def projected_gateway(generation=1, value="sensor.gateway_soc"):
    """Build one projection-complete Gateway-like generic fragment."""
    return projection_snapshot(
        "gateway",
        ("GW1",),
        indexed_roles(("GW1",)),
        (
            config_projection(
                "soc_kw",
                (
                    projection_value(
                        "GW1",
                        ProjectionValueKind.ENTITY,
                        value,
                        capability="battery.target_soc",
                    ),
                ),
            ),
        ),
        generation=generation,
    )


def matching_observation(publication):
    """Mirror compiler output into a detached legacy observation."""
    document = compiled_autoconfig_shadow_document(publication)
    targets = document["targets"]
    return LegacyAutoConfigObservation(
        hub_id="hub-shadow",
        projected_config=document["projected_config"],
        primary_target=targets["primary_target"],
        control_target=targets["control_target"],
        primary_targets=targets["primary_targets"],
        control_targets=targets["control_targets"],
        routing=document["routing"],
        provenance=document["provenance"],
        capabilities={"battery.target_soc"},
    )


def canonical_directory(directory):
    """Resolve the platform temporary-directory alias for strict path tests."""
    return os.path.realpath(directory)


class TestLatticeRuntimeComposition(unittest.TestCase):
    """Exercise gates, lifecycle, invalidation, restart and observability."""

    def test_disabled_gate_is_zero_effect_and_never_registers_component(self):
        """Default construction touches no source, path, file or registry."""
        components_module = sys.modules.get("components")
        config = LatticeRuntimeConfig(
            state_directory=RaisingPath(),
            overlay_path=RaisingPath(),
        )
        runtime = build_lattice_shadow_runtime(
            config,
            RaisingIterable(),
            override_reader=object(),
        )

        self.assertFalse(runtime.enabled)
        self.assertEqual(runtime.phase, LatticeRuntimePhase.DISABLED)
        self.assertFalse(runtime.materializer_enabled)
        observation = runtime.reconcile(object())
        self.assertEqual(
            observation.materialization_status,
            MaterializationStatus.DISABLED,
        )
        self.assertEqual(observation.provider_ids, ())
        self.assertEqual(observation.attempts, 0)
        self.assertFalse(hasattr(observation, "run"))
        self.assertFalse(hasattr(observation, "shadow_report"))
        self.assertEqual(observation.telemetry_text_bytes, 0)
        self.assertIs(sys.modules.get("components"), components_module)

    def test_enabled_paths_reject_relative_and_noncanonical_inputs(self):
        """CWD-dependent and aliased durable identities fail before factories."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            calls = []
            source = source_for(
                "gateway",
                snapshot("gateway", node_id="GW1"),
                {},
                calls,
            )
            configurations = (
                LatticeRuntimeConfig(
                    enabled=True,
                    state_directory="relative-state",
                    overlay_path=os.path.join(directory, "generated.json"),
                ),
                LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=os.path.join(
                        directory,
                        "unused",
                        "..",
                        "state",
                    ),
                    overlay_path=os.path.join(directory, "generated.json"),
                ),
            )

            for config in configurations:
                with self.subTest(config=config):
                    with self.assertRaisesRegex(
                        ValueError,
                        "canonical and absolute",
                    ):
                        build_lattice_shadow_runtime(config, (source,))

            self.assertEqual(calls, [])
            self.assertFalse(os.path.exists(os.path.join(directory, "state")))

    def test_provider_ids_reject_multibyte_overflow_and_surrogates(self):
        """Provider identity is strict bounded UTF-8 before any side effect."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            calls = []
            invalid_provider_ids = (
                "é" * 128,
                "provider-\ud800",
            )

            for provider_id in invalid_provider_ids:
                with self.subTest(provider_id=repr(provider_id)):
                    with self.assertRaisesRegex(
                        ValueError,
                        "provider_id is invalid",
                    ):
                        source_for(
                            provider_id,
                            snapshot("gateway", node_id="GW1"),
                            {},
                            calls,
                        )

            self.assertEqual(calls, [])
            self.assertEqual(os.listdir(directory), [])

    def test_enabled_paths_reject_both_ancestry_directions_before_effects(self):
        """Overlay/state ancestry is rejected before source or filesystem work."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            calls = []
            source = source_for(
                "gateway",
                snapshot("gateway", node_id="GW1"),
                {},
                calls,
            )
            state = os.path.join(directory, "state")
            overlay_in_state = os.path.join(state, "compiled.json")
            overlay = os.path.join(directory, "generated.json")
            state_in_overlay_namespace = os.path.join(
                overlay,
                "state",
            )
            configurations = (
                (state, overlay_in_state),
                (state_in_overlay_namespace, overlay),
            )

            for state_directory, overlay_path in configurations:
                config = LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=state_directory,
                    overlay_path=overlay_path,
                )
                with self.subTest(config=config):
                    with self.assertRaisesRegex(ValueError, "overlap"):
                        build_lattice_shadow_runtime(config, (source,))

            self.assertEqual(calls, [])
            self.assertFalse(os.path.exists(state))
            self.assertFalse(os.path.exists(overlay))

    def test_enabled_paths_reject_lock_and_temp_namespace_collisions(self):
        """Sibling lock/temp collisions fail before source or file effects."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            calls = []
            source = source_for(
                "gateway",
                snapshot("gateway", node_id="GW1"),
                {},
                calls,
            )
            overlay = os.path.join(directory, "generated.json")
            configurations = (
                os.path.join(directory, "generated.json.lock"),
                os.path.join(
                    directory,
                    ".generated.json.collision.tmp",
                    "state",
                ),
            )

            for state_directory in configurations:
                config = LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=state_directory,
                    overlay_path=overlay,
                )
                with self.subTest(config=config):
                    with self.assertRaisesRegex(
                        ValueError,
                        "namespace",
                    ):
                        build_lattice_shadow_runtime(config, (source,))

            self.assertEqual(calls, [])
            self.assertFalse(os.path.exists(overlay))
            self.assertTrue(all(not os.path.exists(path) for path in configurations))

    def test_shadow_build_hydrates_sources_in_deterministic_order(self):
        """Factories receive private stores before sorted registry sealing."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            order = []
            holders = {}
            config = LatticeRuntimeConfig(
                enabled=True,
                state_directory=os.path.join(directory, "state"),
                overlay_path=os.path.join(directory, "generated.json"),
                allowed_keys=("soc_kw",),
            )
            sources = (
                source_for(
                    "zeta",
                    snapshot("zeta", node_id="Z1"),
                    holders,
                    order,
                ),
                source_for(
                    "alpha",
                    snapshot("alpha", node_id="A1"),
                    holders,
                    order,
                ),
            )

            runtime = build_lattice_shadow_runtime(config, sources)
            observation = runtime.drain()

            self.assertTrue(runtime.enabled)
            self.assertEqual(
                runtime.phase,
                LatticeRuntimePhase.SHADOW_READY,
            )
            self.assertEqual(order, ["alpha", "zeta"])
            self.assertEqual(runtime.provider_ids, ("alpha", "zeta"))
            self.assertFalse(runtime.materializer_enabled)
            self.assertEqual(observation.attempts, 1)
            self.assertTrue(observation.published)
            self.assertEqual(observation.publication_version, 1)
            self.assertEqual(
                observation.materialization_status,
                MaterializationStatus.DISABLED,
            )
            self.assertFalse(os.path.exists(config.overlay_path))
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        config.state_directory,
                        "materializer.json",
                    )
                )
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        config.state_directory,
                        "materializer.json.fence",
                    )
                )
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        config.state_directory,
                        "target-journal.json",
                    )
                )
            )

    def test_invalidations_coalesce_and_remain_observable_without_write(self):
        """Multiple provider changes produce one shadow publication and causes."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            holders = {}
            overlay = os.path.join(directory, "generated.json")
            config = LatticeRuntimeConfig(
                enabled=True,
                state_directory=os.path.join(directory, "state"),
                overlay_path=overlay,
                allowed_keys=("soc_kw",),
            )
            runtime = build_lattice_shadow_runtime(
                config,
                (
                    source_for(
                        "gateway",
                        snapshot("gateway", node_id="GW1"),
                        holders,
                    ),
                    source_for(
                        "cloud",
                        snapshot("cloud", node_id="CL1"),
                        holders,
                    ),
                ),
            )
            first = runtime.drain()
            self.assertEqual(first.publication_version, 1)

            holders["gateway"].publish(
                snapshot("gateway", generation=2, node_id="GW1"),
                "topology-refresh",
            )
            holders["cloud"].publish(
                snapshot("cloud", generation=2, node_id="CL1"),
                "discovery-refresh",
            )
            observation = runtime.reconcile()

            self.assertEqual(observation.attempts, 1)
            self.assertEqual(observation.publication_version, 2)
            self.assertTrue(observation.published)
            self.assertEqual(
                tuple(
                    (
                        cause.source_id,
                        cause.generation,
                        cause.reason,
                    )
                    for cause in observation.invalidation_causes
                ),
                (
                    ("cloud", 2, "discovery-refresh"),
                    ("gateway", 2, "topology-refresh"),
                ),
            )
            self.assertEqual(observation.compiler_materializations, 0)
            self.assertFalse(os.path.exists(overlay))

    def test_restart_restores_publication_without_duplicate_version(self):
        """A fresh composition reads the same durable source and publication."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            config = LatticeRuntimeConfig(
                enabled=True,
                state_directory=os.path.join(directory, "state"),
                overlay_path=os.path.join(directory, "generated.json"),
                allowed_keys=("soc_kw",),
            )
            first_holders = {}
            first = build_lattice_shadow_runtime(
                config,
                (
                    source_for(
                        "gateway",
                        projected_gateway(),
                        first_holders,
                    ),
                ),
            ).drain()

            second_holders = {}
            restarted_runtime = build_lattice_shadow_runtime(
                config,
                (
                    source_for(
                        "gateway",
                        projected_gateway(),
                        second_holders,
                    ),
                ),
            )
            restarted = restarted_runtime.drain()

            self.assertEqual(first.publication_version, 1)
            self.assertEqual(restarted.publication_version, 1)
            self.assertEqual(
                restarted.publication_digest,
                first.publication_digest,
            )
            self.assertFalse(restarted.published)
            self.assertEqual(restarted.compiler_materializations, 0)
            self.assertFalse(os.path.exists(config.overlay_path))

    def test_reconcile_emits_pure_shadow_report_and_preserves_config_files(self):
        """Comparison is read-only and all activation gates stay closed."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            apps_path = os.path.join(directory, "apps.yaml")
            secrets_path = os.path.join(directory, "secrets.yaml")
            with open(apps_path, "w", encoding="utf-8") as stream:
                stream.write("apps-sentinel\n")
            with open(secrets_path, "w", encoding="utf-8") as stream:
                stream.write("secrets-sentinel\n")

            holders = {}
            overlay = os.path.join(directory, "generated.json")
            config = LatticeRuntimeConfig(
                enabled=True,
                state_directory=os.path.join(directory, "state"),
                overlay_path=overlay,
                allowed_keys=("soc_kw",),
            )
            runtime = build_lattice_shadow_runtime(
                config,
                (
                    source_for(
                        "gateway",
                        projected_gateway(),
                        holders,
                    ),
                ),
            )
            first = runtime.drain()
            self.assertEqual(first.publication_version, 1)
            legacy = matching_observation(runtime._compiler.publication)
            observation = runtime.reconcile(legacy)

            self.assertIsNotNone(observation.shadow)
            self.assertTrue(observation.shadow.matches)
            self.assertFalse(observation.shadow.canary_ready)
            self.assertIn(
                "shadow_disabled",
                observation.shadow.canary_blockers,
            )
            self.assertFalse(hasattr(observation, "run"))
            self.assertFalse(hasattr(observation, "shadow_report"))
            self.assertEqual(observation.compiler_materializations, 0)
            self.assertFalse(os.path.exists(overlay))
            with open(apps_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "apps-sentinel\n")
            with open(secrets_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "secrets-sentinel\n")

    def test_exception_telemetry_is_sanitized_to_fixed_byte_limits(self):
        """A huge exception cannot escape through the public observation."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            secret_suffix = "SECRET-TAIL-MUST-NOT-LEAK"

            def failing_override_reader():
                """Raise an intentionally excessive diagnostic payload."""
                raise RuntimeError(("x" * 200000) + secret_suffix)

            runtime = build_lattice_shadow_runtime(
                LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=os.path.join(directory, "state"),
                    overlay_path=os.path.join(directory, "generated.json"),
                    allowed_keys=("soc_kw",),
                ),
                (
                    source_for(
                        "gateway",
                        snapshot("gateway", node_id="GW1"),
                        {},
                    ),
                ),
                override_reader=failing_override_reader,
            )
            observation = runtime.drain()

            issue = next(item for item in observation.issues if item.code == "user_overrides_read_failed")
            self.assertLessEqual(
                len(issue.detail.encode("utf-8")),
                MAX_OBSERVATION_DETAIL_BYTES,
            )
            self.assertTrue(issue.text_truncated)
            self.assertFalse(issue.text_sanitized)
            self.assertNotIn(secret_suffix, issue.detail)
            self.assertFalse(observation.telemetry_text_sanitized)
            self.assertTrue(observation.telemetry_text_truncated)
            self.assertLessEqual(
                observation.telemetry_text_bytes,
                MAX_OBSERVATION_TOTAL_TEXT_BYTES,
            )

    def test_lone_surrogate_exception_has_safe_deterministic_telemetry(self):
        """A non-encodable exception is escaped before hashing and capping."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            raw_payload = "\ud800" * 200000

            def failing_override_reader():
                """Raise an exception whose text cannot encode as UTF-8."""
                raise RuntimeError(raw_payload)

            runtime = build_lattice_shadow_runtime(
                LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=os.path.join(directory, "state"),
                    overlay_path=os.path.join(directory, "generated.json"),
                    allowed_keys=("soc_kw",),
                ),
                (
                    source_for(
                        "gateway",
                        snapshot("gateway", node_id="GW1"),
                        {},
                    ),
                ),
                override_reader=failing_override_reader,
            )
            observation = runtime.reconcile()
            issue = next(item for item in observation.issues if item.code == "user_overrides_read_failed")
            canonical_detail = "RuntimeError: " + ("\\ud800" * 200000)
            digest = hashlib.sha256(
                canonical_detail.encode("utf-8"),
            ).hexdigest()[:16]

            self.assertTrue(
                issue.detail.endswith(
                    "...[sha256:{}]".format(digest),
                )
            )
            self.assertNotIn("\ud800", issue.detail)
            self.assertTrue(issue.text_sanitized)
            self.assertTrue(issue.text_truncated)
            self.assertTrue(observation.telemetry_text_sanitized)
            self.assertTrue(observation.telemetry_text_truncated)
            self.assertLessEqual(
                len(issue.detail.encode("utf-8")),
                MAX_OBSERVATION_DETAIL_BYTES,
            )
            self.assertLessEqual(
                observation.telemetry_text_bytes,
                MAX_OBSERVATION_TOTAL_TEXT_BYTES,
            )

    def test_unicode_format_separators_and_noncharacters_are_escaped(self):
        """Unsafe scalars expand canonically while printable Unicode survives."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            unsafe = "\u2028\u2029\u202e\u200b\ufdd0\U0001fffe"
            escaped = "\\u2028\\u2029\\u202e\\u200b\\ufdd0" "\\U0001fffe"
            raw_payload = "é雪🙂" + (unsafe * 6)

            def failing_override_reader():
                """Raise valid UTF-8 text containing unsafe log scalars."""
                raise RuntimeError(raw_payload)

            runtime = build_lattice_shadow_runtime(
                LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=os.path.join(directory, "state"),
                    overlay_path=os.path.join(directory, "generated.json"),
                    allowed_keys=("soc_kw",),
                ),
                (
                    source_for(
                        "gateway",
                        snapshot("gateway", node_id="GW1"),
                        {},
                    ),
                ),
                override_reader=failing_override_reader,
            )
            observation = runtime.reconcile()
            issue = next(item for item in observation.issues if item.code == "user_overrides_read_failed")
            raw_detail = "RuntimeError: " + raw_payload
            canonical_detail = "RuntimeError: é雪🙂" + (escaped * 6)
            digest = hashlib.sha256(
                canonical_detail.encode("utf-8"),
            ).hexdigest()[:16]

            self.assertLess(
                len(raw_detail.encode("utf-8")),
                MAX_OBSERVATION_DETAIL_BYTES,
            )
            self.assertGreater(
                len(canonical_detail.encode("utf-8")),
                MAX_OBSERVATION_DETAIL_BYTES,
            )
            self.assertTrue(issue.detail.startswith("RuntimeError: é雪🙂"))
            self.assertTrue(
                issue.detail.endswith(
                    "...[sha256:{}]".format(digest),
                )
            )
            self.assertTrue(all(character not in issue.detail for character in unsafe))
            self.assertTrue(issue.text_sanitized)
            self.assertTrue(issue.text_truncated)
            self.assertTrue(observation.telemetry_text_sanitized)
            self.assertTrue(observation.telemetry_text_truncated)
            self.assertLessEqual(
                len(issue.detail.encode("utf-8")),
                MAX_OBSERVATION_DETAIL_BYTES,
            )
            self.assertLessEqual(
                observation.telemetry_text_bytes,
                MAX_OBSERVATION_TOTAL_TEXT_BYTES,
            )

    def test_many_long_invalidations_have_truthful_bounded_cardinality(self):
        """Many long reasons retain totals, deterministic order and truncation."""
        with tempfile.TemporaryDirectory() as directory:
            directory = canonical_directory(directory)
            holders = {}
            sources = tuple(
                source_for(
                    "provider-{:03d}".format(index),
                    snapshot(
                        "provider-{:03d}".format(index),
                        node_id="N{:03d}".format(index),
                    ),
                    holders,
                )
                for index in range(MAX_OBSERVATION_CAUSES + 8)
            )
            runtime = build_lattice_shadow_runtime(
                LatticeRuntimeConfig(
                    enabled=True,
                    state_directory=os.path.join(directory, "state"),
                    overlay_path=os.path.join(directory, "generated.json"),
                    allowed_keys=("soc_kw",),
                ),
                sources,
            )
            runtime.drain()
            for index, provider_id in enumerate(sorted(holders)):
                holders[provider_id].publish(
                    snapshot(
                        provider_id,
                        generation=2,
                        node_id="N{:03d}".format(index),
                    ),
                    "reason-{:03d}-{}".format(index, "r" * 10000),
                )

            observation = runtime.reconcile()

            self.assertEqual(
                observation.invalidation_cause_total,
                MAX_OBSERVATION_CAUSES + 8,
            )
            self.assertEqual(
                len(observation.invalidation_causes),
                MAX_OBSERVATION_CAUSES,
            )
            self.assertTrue(observation.invalidation_causes_truncated)
            self.assertEqual(
                tuple(cause.source_id for cause in observation.invalidation_causes),
                tuple("provider-{:03d}".format(index) for index in range(MAX_OBSERVATION_CAUSES)),
            )
            self.assertTrue(all(cause.text_truncated and len(cause.reason.encode("utf-8")) <= MAX_OBSERVATION_DETAIL_BYTES for cause in observation.invalidation_causes))
            self.assertTrue(observation.telemetry_text_truncated)
            self.assertLessEqual(
                observation.telemetry_text_bytes,
                MAX_OBSERVATION_TOTAL_TEXT_BYTES,
            )


if __name__ == "__main__":
    unittest.main()

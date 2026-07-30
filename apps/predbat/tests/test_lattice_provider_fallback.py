"""Tests for the detached Lattice provider-path fallback boundary."""

# cspell:ignore codepoint codepoints cooldown fffe noncanonical noncharacter overclaim

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_provider_fallback import (
    LatticeProviderFallbackRouter,
    ProviderExecutionResult,
    ProviderExecutionState,
    ProviderFallbackResult,
    ProviderFallbackValidationError,
    ProviderPath,
    ProviderPathHealthLedger,
    ProviderPathHealthPhase,
    ProviderPathHealthPolicy,
    ProviderPathIdentity,
    ProviderPathKind,
    QualifiedTargetAlias,
)


class ExplodingValue:
    """Object that proves the disabled path performs no accidental inspection."""

    def __getattribute__(self, name):
        """Raise on every attempted attribute read."""
        raise AssertionError("disabled request was inspected: {}".format(name))

    def __str__(self):
        """Raise on accidental text coercion."""
        raise AssertionError("disabled request was coerced")


class Clock:
    """Controllable monotonic clock that records reads."""

    def __init__(self, value=100.0):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class Executor:
    """Scripted sync or async executor with exact call evidence."""

    def __init__(self, results, asynchronous=False):
        self.results = list(results)
        self.asynchronous = asynchronous
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if not self.asynchronous:
            return result

        async def respond():
            return result

        return respond()


def path(
    provider_id,
    path_id,
    kind,
    executor,
    target_id="battery-main",
    aliases=(),
    ttl=30.0,
    cooldown=60.0,
):
    """Build one concise valid provider path."""
    return ProviderPath(
        target_id=target_id,
        provider_id=provider_id,
        path_id=path_id,
        kind=kind,
        executor=executor,
        aliases=aliases,
        health_policy=ProviderPathHealthPolicy(ttl, cooldown),
    )


class TestStrictModels(unittest.TestCase):
    """Exhaustively reject outcome combinations that could overclaim safety."""

    def test_valid_execution_result_truth_table(self):
        cases = (
            (
                ProviderExecutionResult.applied(2750),
                ProviderExecutionState.APPLIED,
                False,
                True,
            ),
            (
                ProviderExecutionResult.not_applied(False),
                ProviderExecutionState.NOT_APPLIED,
                False,
                False,
            ),
            (
                ProviderExecutionResult.not_applied(True),
                ProviderExecutionState.NOT_APPLIED,
                True,
                False,
            ),
            (
                ProviderExecutionResult.unknown(),
                ProviderExecutionState.UNKNOWN,
                False,
                False,
            ),
        )
        for result, state, fallback_safe, verified in cases:
            with self.subTest(state=state, fallback_safe=fallback_safe):
                self.assertIs(result.state, state)
                self.assertIs(result.fallback_safe, fallback_safe)
                self.assertIs(result.verified, verified)

    def test_invalid_execution_result_truth_table(self):
        for state in ProviderExecutionState:
            for fallback_safe in (False, True):
                for verified in (False, True):
                    for has_value in (False, True):
                        valid = (
                            (state is ProviderExecutionState.APPLIED and not fallback_safe and verified)
                            or (state is ProviderExecutionState.NOT_APPLIED and not verified and not has_value)
                            or (state is ProviderExecutionState.UNKNOWN and not fallback_safe and not verified and not has_value)
                        )
                        if valid:
                            continue
                        with self.subTest(
                            state=state,
                            fallback_safe=fallback_safe,
                            verified=verified,
                            has_value=has_value,
                        ):
                            with self.assertRaises(
                                ProviderFallbackValidationError,
                            ):
                                ProviderExecutionResult(
                                    state,
                                    fallback_safe=fallback_safe,
                                    verified=verified,
                                    applied_value=1 if has_value else None,
                                )

    def test_non_enum_and_non_boolean_fields_are_rejected(self):
        invalid = (
            lambda: ProviderExecutionResult("APPLIED"),
            lambda: ProviderExecutionResult(
                ProviderExecutionState.UNKNOWN,
                fallback_safe=1,
            ),
            lambda: ProviderExecutionResult(
                ProviderExecutionState.UNKNOWN,
                verified=1,
            ),
        )
        for factory in invalid:
            with self.subTest(factory=factory):
                with self.assertRaises(ProviderFallbackValidationError):
                    factory()

    def test_detail_rejects_every_ascii_control(self):
        for codepoint in tuple(range(0x20)) + (0x7F,):
            with self.subTest(codepoint=codepoint):
                with self.assertRaises(ProviderFallbackValidationError):
                    ProviderExecutionResult.unknown(chr(codepoint))

    def test_detail_rejects_every_unicode_noncharacter(self):
        codepoints = list(range(0xFDD0, 0xFDF0))
        codepoints.extend(codepoint for plane in range(17) for codepoint in (plane * 0x10000 + 0xFFFE, plane * 0x10000 + 0xFFFF))
        for codepoint in codepoints:
            with self.subTest(codepoint=hex(codepoint)):
                with self.assertRaises(ProviderFallbackValidationError):
                    ProviderExecutionResult.unknown(chr(codepoint))

    def test_detail_is_bounded_by_utf8_bytes(self):
        self.assertEqual(
            ProviderExecutionResult.unknown("£" * 128).detail,
            "£" * 128,
        )
        with self.assertRaises(ProviderFallbackValidationError):
            ProviderExecutionResult.unknown("£" * 129)

    def test_aggregate_result_cannot_overclaim(self):
        invalid = (
            lambda: ProviderFallbackResult(
                ProviderExecutionState.APPLIED,
                True,
            ),
            lambda: ProviderFallbackResult(
                ProviderExecutionState.UNKNOWN,
                True,
            ),
            lambda: ProviderFallbackResult(
                ProviderExecutionState.NOT_APPLIED,
                False,
                disabled=True,
            ),
        )
        for factory in invalid:
            with self.subTest(factory=factory):
                with self.assertRaises(ProviderFallbackValidationError):
                    factory()


class TestIdentityAndDeclarations(unittest.TestCase):
    """Prove provider qualification, canonical identity, and collision handling."""

    def test_provider_qualified_aliases_can_repeat_across_providers(self):
        gateway = path(
            "predbat-gateway",
            "mqtt",
            ProviderPathKind.GATEWAY_LOCAL,
            Executor([]),
            aliases=("SERIAL-1",),
        )
        cloud = path(
            "givenergy-cloud",
            "rest",
            ProviderPathKind.VENDOR_CLOUD,
            Executor([]),
            aliases=("SERIAL-1",),
        )
        router = LatticeProviderFallbackRouter((gateway, cloud))
        self.assertEqual(
            router.ranked_paths(
                QualifiedTargetAlias("predbat-gateway", "SERIAL-1"),
            ),
            (gateway, cloud),
        )
        self.assertEqual(
            router.ranked_paths(
                QualifiedTargetAlias("givenergy-cloud", "SERIAL-1"),
            ),
            (gateway, cloud),
        )

    def test_alias_collision_within_provider_fails_closed(self):
        first = path(
            "vendor-cloud",
            "one",
            ProviderPathKind.VENDOR_CLOUD,
            Executor([]),
            target_id="battery-one",
            aliases=("SERIAL-1",),
        )
        second = path(
            "vendor-cloud",
            "two",
            ProviderPathKind.VENDOR_CLOUD,
            Executor([]),
            target_id="battery-two",
            aliases=("SERIAL-1",),
        )
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "alias collision",
        ):
            LatticeProviderFallbackRouter((first, second))

    def test_implicit_target_alias_collision_fails_closed(self):
        first = path(
            "vendor-cloud",
            "one",
            ProviderPathKind.VENDOR_CLOUD,
            Executor([]),
            target_id="battery-one",
            aliases=("battery-two",),
        )
        second = path(
            "vendor-cloud",
            "two",
            ProviderPathKind.VENDOR_CLOUD,
            Executor([]),
            target_id="battery-two",
        )
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "alias collision",
        ):
            LatticeProviderFallbackRouter((first, second))

    def test_duplicate_path_identity_fails_even_if_declarations_match(self):
        executor = Executor([])
        declaration = path(
            "vendor-cloud",
            "rest",
            ProviderPathKind.VENDOR_CLOUD,
            executor,
        )
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "path identity collision",
        ):
            LatticeProviderFallbackRouter((declaration, declaration))

    def test_duplicate_alias_inside_one_declaration_fails(self):
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "duplicate alias",
        ):
            path(
                "vendor-cloud",
                "rest",
                ProviderPathKind.VENDOR_CLOUD,
                Executor([]),
                aliases=("SERIAL-1", "SERIAL-1"),
            )

    def test_unqualified_lookup_is_rejected(self):
        router = LatticeProviderFallbackRouter(())
        for value in ("battery-main", ("vendor", "battery-main"), None):
            with self.subTest(value=value):
                with self.assertRaises(ProviderFallbackValidationError):
                    router.ranked_paths(value)

    def test_identity_rejects_noncanonical_values(self):
        values = (
            "",
            " leading",
            "trailing ",
            "-leading",
            "trailing-",
            "contains space",
            "unicode-£",
            "a" * 129,
            "line\nbreak",
        )
        for value in values:
            with self.subTest(value=repr(value)):
                with self.assertRaises(ProviderFallbackValidationError):
                    QualifiedTargetAlias("provider", value)
        for value in ("UPPER", "Provider", "provider-£"):
            with self.subTest(provider=value):
                with self.assertRaises(ProviderFallbackValidationError):
                    QualifiedTargetAlias(value, "target")

    def test_identifier_boundary_is_exact(self):
        valid = "a" * 128
        self.assertEqual(
            QualifiedTargetAlias("provider", valid).alias,
            valid,
        )
        with self.assertRaises(ProviderFallbackValidationError):
            QualifiedTargetAlias("provider", "a" * 129)

    def test_invalid_path_shapes_fail_at_construction(self):
        valid_executor = Executor([])
        invalid = (
            lambda: ProviderPath(
                "target",
                "provider",
                "path",
                "gw-local",
                valid_executor,
            ),
            lambda: ProviderPath(
                "target",
                "provider",
                "path",
                ProviderPathKind.GATEWAY_LOCAL,
                None,
            ),
            lambda: ProviderPath(
                "target",
                "provider",
                "path",
                ProviderPathKind.GATEWAY_LOCAL,
                valid_executor,
                aliases=[],
            ),
            lambda: LatticeProviderFallbackRouter([], enabled=False),
            lambda: LatticeProviderFallbackRouter((), enabled=1),
        )
        for factory in invalid:
            with self.subTest(factory=factory):
                with self.assertRaises(ProviderFallbackValidationError):
                    factory()


class TestHealthLedger(unittest.TestCase):
    """Exercise TTL, cooldown, identity isolation, and monotonicity."""

    def setUp(self):
        self.clock = Clock()
        self.ledger = ProviderPathHealthLedger(self.clock)
        self.executor = Executor([])
        self.path = path(
            "predbat-gateway",
            "mqtt",
            ProviderPathKind.GATEWAY_LOCAL,
            self.executor,
            ttl=10.0,
            cooldown=20.0,
        )

    def test_unobserved_then_applied_ttl_then_probe(self):
        self.assertIs(
            self.ledger.phase(self.path),
            ProviderPathHealthPhase.UNOBSERVED,
        )
        self.ledger.observe(
            self.path.identity,
            ProviderExecutionResult.applied(),
        )
        self.clock.value = 110.0
        self.assertIs(
            self.ledger.phase(self.path),
            ProviderPathHealthPhase.HEALTHY,
        )
        self.clock.value = 110.0001
        self.assertIs(
            self.ledger.phase(self.path),
            ProviderPathHealthPhase.PROBE,
        )

    def test_safe_not_applied_cools_then_probes(self):
        self.ledger.observe(
            self.path.identity,
            ProviderExecutionResult.not_applied(fallback_safe=True),
        )
        self.clock.value = 119.999
        self.assertIs(
            self.ledger.phase(self.path),
            ProviderPathHealthPhase.COOLING_FALLBACK_SAFE,
        )
        self.clock.value = 120.0
        self.assertIs(
            self.ledger.phase(self.path),
            ProviderPathHealthPhase.PROBE,
        )

    def test_unknown_and_unsafe_not_applied_cool_blocked(self):
        for result in (
            ProviderExecutionResult.unknown(),
            ProviderExecutionResult.not_applied(fallback_safe=False),
        ):
            with self.subTest(result=result):
                ledger = ProviderPathHealthLedger(Clock())
                ledger.observe(self.path.identity, result)
                self.assertIs(
                    ledger.phase(self.path, now=119.999),
                    ProviderPathHealthPhase.COOLING_BLOCKED,
                )
                self.assertIs(
                    ledger.phase(self.path, now=120.0),
                    ProviderPathHealthPhase.PROBE,
                )

    def test_health_is_isolated_by_target_provider_and_path(self):
        self.ledger.observe(
            self.path.identity,
            ProviderExecutionResult.unknown(),
        )
        variants = (
            path(
                "predbat-gateway",
                "other",
                ProviderPathKind.GATEWAY_LOCAL,
                self.executor,
            ),
            path(
                "other-provider",
                "mqtt",
                ProviderPathKind.GATEWAY_LOCAL,
                self.executor,
            ),
            path(
                "predbat-gateway",
                "mqtt",
                ProviderPathKind.GATEWAY_LOCAL,
                self.executor,
                target_id="other-target",
            ),
        )
        for variant in variants:
            with self.subTest(identity=variant.identity):
                self.assertIs(
                    self.ledger.phase(variant),
                    ProviderPathHealthPhase.UNOBSERVED,
                )

    def test_time_regression_and_same_time_conflict_fail_closed(self):
        self.ledger.observe(
            self.path.identity,
            ProviderExecutionResult.applied(),
            observed_at=100.0,
        )
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "time regressed",
        ):
            self.ledger.observe(
                self.path.identity,
                ProviderExecutionResult.unknown(),
                observed_at=99.0,
            )
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "reused timestamp",
        ):
            self.ledger.observe(
                self.path.identity,
                ProviderExecutionResult.unknown(),
                observed_at=100.0,
            )
        repeated = self.ledger.observe(
            self.path.identity,
            ProviderExecutionResult.applied(),
            observed_at=100.0,
        )
        self.assertEqual(repeated, self.ledger.record(self.path.identity))

    def test_decision_before_observation_fails_closed(self):
        self.ledger.observe(
            self.path.identity,
            ProviderExecutionResult.applied(),
            observed_at=100.0,
        )
        with self.assertRaisesRegex(
            ProviderFallbackValidationError,
            "precedes observation",
        ):
            self.ledger.phase(self.path, now=99.0)

    def test_invalid_time_and_policy_values_are_rejected(self):
        values = (
            True,
            False,
            -1,
            0,
            float("inf"),
            float("-inf"),
            float("nan"),
            "1",
            None,
        )
        for value in values:
            with self.subTest(policy=value):
                with self.assertRaises(ProviderFallbackValidationError):
                    ProviderPathHealthPolicy(value, 1)
                with self.assertRaises(ProviderFallbackValidationError):
                    ProviderPathHealthPolicy(1, value)
        for value in (
            True,
            -1,
            float("inf"),
            float("-inf"),
            float("nan"),
            "1",
            None,
        ):
            with self.subTest(timestamp=value):
                with self.assertRaises(ProviderFallbackValidationError):
                    self.ledger.observe(
                        self.path.identity,
                        ProviderExecutionResult.unknown(),
                        observed_at=value,
                    )


class TestRouting(unittest.TestCase):
    """Prove deterministic rank, strict fallthrough, and zero-effect disable."""

    def test_disabled_returns_before_all_caller_objects_and_clock(self):
        clock = Clock()
        ledger = ProviderPathHealthLedger(clock)
        executor = Executor(
            [ProviderExecutionResult.applied()],
        )
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "predbat-gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    executor,
                ),
            ),
            health_ledger=ledger,
        )
        result = asyncio.run(
            router.dispatch(ExplodingValue(), ExplodingValue()),
        )
        self.assertTrue(result.disabled)
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)
        self.assertEqual(result.attempts, ())
        self.assertEqual(clock.calls, 0)
        self.assertEqual(executor.calls, [])

    def test_gateway_local_always_ranks_before_vendor_cloud(self):
        calls = []

        def executor(name, result):
            def execute(_request):
                calls.append(name)
                return result

            return execute

        declarations = (
            path(
                "z-vendor",
                "z-rest",
                ProviderPathKind.VENDOR_CLOUD,
                executor(
                    "z-cloud",
                    ProviderExecutionResult.applied(),
                ),
                aliases=("SERIAL-1",),
            ),
            path(
                "z-gateway",
                "z-mqtt",
                ProviderPathKind.GATEWAY_LOCAL,
                executor(
                    "z-local",
                    ProviderExecutionResult.not_applied(True),
                ),
            ),
            path(
                "a-vendor",
                "a-rest",
                ProviderPathKind.VENDOR_CLOUD,
                executor(
                    "a-cloud",
                    ProviderExecutionResult.applied(),
                ),
            ),
            path(
                "a-gateway",
                "a-mqtt",
                ProviderPathKind.GATEWAY_LOCAL,
                executor(
                    "a-local",
                    ProviderExecutionResult.not_applied(True),
                ),
            ),
        )
        router = LatticeProviderFallbackRouter(
            declarations,
            enabled=True,
        )
        ranked = router.ranked_paths(
            QualifiedTargetAlias("z-vendor", "SERIAL-1"),
        )
        self.assertEqual(
            tuple((item.kind, item.provider_id, item.path_id) for item in ranked),
            (
                (ProviderPathKind.GATEWAY_LOCAL, "a-gateway", "a-mqtt"),
                (ProviderPathKind.GATEWAY_LOCAL, "z-gateway", "z-mqtt"),
                (ProviderPathKind.VENDOR_CLOUD, "a-vendor", "a-rest"),
                (ProviderPathKind.VENDOR_CLOUD, "z-vendor", "z-rest"),
            ),
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("z-vendor", "SERIAL-1"),
                {"watts": 2500},
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(calls, ["a-local", "z-local", "a-cloud"])

    def test_each_terminal_state_controls_fallback_exactly(self):
        first_results = (
            ProviderExecutionResult.applied(),
            ProviderExecutionResult.not_applied(False),
            ProviderExecutionResult.not_applied(True),
            ProviderExecutionResult.unknown(),
        )
        for first_result in first_results:
            with self.subTest(first_result=first_result):
                local = Executor([first_result])
                cloud = Executor([ProviderExecutionResult.applied()])
                router = LatticeProviderFallbackRouter(
                    (
                        path(
                            "gateway",
                            "mqtt",
                            ProviderPathKind.GATEWAY_LOCAL,
                            local,
                            aliases=("target-alias",),
                        ),
                        path(
                            "cloud",
                            "rest",
                            ProviderPathKind.VENDOR_CLOUD,
                            cloud,
                        ),
                    ),
                    enabled=True,
                    health_ledger=ProviderPathHealthLedger(
                        Clock(),
                    ),
                )
                result = asyncio.run(
                    router.dispatch(
                        QualifiedTargetAlias(
                            "gateway",
                            "target-alias",
                        ),
                        object(),
                    ),
                )
                should_fallback = first_result.state is ProviderExecutionState.NOT_APPLIED and first_result.fallback_safe
                self.assertEqual(len(cloud.calls), int(should_fallback))
                self.assertEqual(
                    result.state,
                    (ProviderExecutionState.APPLIED if should_fallback else first_result.state),
                )

    def test_unknown_never_falls_through_to_cloud(self):
        local = Executor([ProviderExecutionResult.unknown()])
        cloud = Executor([ProviderExecutionResult.applied()])
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    local,
                    aliases=("battery",),
                ),
                path(
                    "cloud",
                    "rest",
                    ProviderPathKind.VENDOR_CLOUD,
                    cloud,
                ),
            ),
            enabled=True,
            health_ledger=ProviderPathHealthLedger(Clock()),
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(cloud.calls, [])

    def test_exception_and_invalid_return_become_unknown_without_fallback(self):
        invalid_returns = (
            True,
            False,
            "NOT_APPLIED",
            None,
            {"state": "NOT_APPLIED", "fallback_safe": True},
            RuntimeError("secret message must not escape"),
        )
        for invalid_return in invalid_returns:
            with self.subTest(invalid_return=repr(invalid_return)):
                local = Executor([invalid_return], asynchronous=True)
                cloud = Executor([ProviderExecutionResult.applied()])
                router = LatticeProviderFallbackRouter(
                    (
                        path(
                            "gateway",
                            "mqtt",
                            ProviderPathKind.GATEWAY_LOCAL,
                            local,
                            aliases=("battery",),
                        ),
                        path(
                            "cloud",
                            "rest",
                            ProviderPathKind.VENDOR_CLOUD,
                            cloud,
                        ),
                    ),
                    enabled=True,
                    health_ledger=ProviderPathHealthLedger(
                        Clock(),
                    ),
                )
                result = asyncio.run(
                    router.dispatch(
                        QualifiedTargetAlias("gateway", "battery"),
                        object(),
                    ),
                )
                self.assertIs(
                    result.state,
                    ProviderExecutionState.UNKNOWN,
                )
                self.assertFalse(result.fallback_safe)
                self.assertEqual(cloud.calls, [])
                self.assertNotIn("secret", result.detail)

    def test_safe_cooling_path_skips_to_cloud(self):
        clock = Clock()
        ledger = ProviderPathHealthLedger(clock)
        local = path(
            "gateway",
            "mqtt",
            ProviderPathKind.GATEWAY_LOCAL,
            Executor([]),
            aliases=("battery",),
            cooldown=60,
        )
        cloud_executor = Executor([ProviderExecutionResult.applied()])
        cloud = path(
            "cloud",
            "rest",
            ProviderPathKind.VENDOR_CLOUD,
            cloud_executor,
        )
        ledger.observe(
            local.identity,
            ProviderExecutionResult.not_applied(True),
        )
        router = LatticeProviderFallbackRouter(
            (local, cloud),
            enabled=True,
            health_ledger=ledger,
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(cloud_executor.calls.__len__(), 1)
        self.assertEqual(len(result.attempts), 1)

    def test_unknown_cooling_path_blocks_cloud(self):
        clock = Clock()
        ledger = ProviderPathHealthLedger(clock)
        local = path(
            "gateway",
            "mqtt",
            ProviderPathKind.GATEWAY_LOCAL,
            Executor([]),
            aliases=("battery",),
            cooldown=60,
        )
        cloud_executor = Executor([ProviderExecutionResult.applied()])
        cloud = path(
            "cloud",
            "rest",
            ProviderPathKind.VENDOR_CLOUD,
            cloud_executor,
        )
        ledger.observe(local.identity, ProviderExecutionResult.unknown())
        router = LatticeProviderFallbackRouter(
            (local, cloud),
            enabled=True,
            health_ledger=ledger,
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertEqual(result.selected_path, local.identity)
        self.assertEqual(result.attempts, ())
        self.assertEqual(cloud_executor.calls, [])

    def test_cooldown_expiry_retries_higher_ranked_path(self):
        clock = Clock()
        ledger = ProviderPathHealthLedger(clock)
        local_executor = Executor([ProviderExecutionResult.applied()])
        local = path(
            "gateway",
            "mqtt",
            ProviderPathKind.GATEWAY_LOCAL,
            local_executor,
            aliases=("battery",),
            cooldown=60,
        )
        cloud_executor = Executor([ProviderExecutionResult.applied()])
        cloud = path(
            "cloud",
            "rest",
            ProviderPathKind.VENDOR_CLOUD,
            cloud_executor,
        )
        ledger.observe(local.identity, ProviderExecutionResult.unknown())
        clock.value = 160.0
        router = LatticeProviderFallbackRouter(
            (cloud, local),
            enabled=True,
            health_ledger=ledger,
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(len(local_executor.calls), 1)
        self.assertEqual(cloud_executor.calls, [])

    def test_all_definite_safe_declines_are_fallback_safe(self):
        executors = (
            Executor([ProviderExecutionResult.not_applied(True)]),
            Executor([ProviderExecutionResult.not_applied(True)]),
        )
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    executors[0],
                    aliases=("battery",),
                ),
                path(
                    "cloud",
                    "rest",
                    ProviderPathKind.VENDOR_CLOUD,
                    executors[1],
                ),
            ),
            enabled=True,
            health_ledger=ProviderPathHealthLedger(Clock()),
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(
            result.state,
            ProviderExecutionState.NOT_APPLIED,
        )
        self.assertTrue(result.fallback_safe)
        self.assertEqual(len(result.attempts), 2)

    def test_unknown_alias_is_definite_no_path_without_executor_effects(self):
        executor = Executor([ProviderExecutionResult.applied()])
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    executor,
                ),
            ),
            enabled=True,
            health_ledger=ProviderPathHealthLedger(Clock()),
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "not-declared"),
                object(),
            ),
        )
        self.assertIs(
            result.state,
            ProviderExecutionState.NOT_APPLIED,
        )
        self.assertTrue(result.fallback_safe)
        self.assertEqual(executor.calls, [])

    def test_health_observation_is_recorded_for_each_attempt(self):
        clock = Clock()
        ledger = ProviderPathHealthLedger(clock)
        executor = Executor([ProviderExecutionResult.applied(1234)])
        declaration = path(
            "gateway",
            "mqtt",
            ProviderPathKind.GATEWAY_LOCAL,
            executor,
            aliases=("battery",),
        )
        router = LatticeProviderFallbackRouter(
            (declaration,),
            enabled=True,
            health_ledger=ledger,
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertEqual(
            ledger.record(declaration.identity).result,
            result.attempts[0].result,
        )

    def test_health_clock_failure_is_unknown_before_executor(self):
        def broken_clock():
            raise RuntimeError("clock secret")

        executor = Executor([ProviderExecutionResult.applied()])
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    executor,
                    aliases=("battery",),
                ),
            ),
            enabled=True,
            health_ledger=ProviderPathHealthLedger(broken_clock),
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertEqual(executor.calls, [])
        self.assertNotIn("secret", result.detail)

    def test_health_write_failure_is_unknown_and_stops_fallback(self):
        class FailingLedger(ProviderPathHealthLedger):
            def observe(self, identity, result, observed_at=None):
                raise RuntimeError("storage secret")

        local = Executor([ProviderExecutionResult.not_applied(True)])
        cloud = Executor([ProviderExecutionResult.applied()])
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    local,
                    aliases=("battery",),
                ),
                path(
                    "cloud",
                    "rest",
                    ProviderPathKind.VENDOR_CLOUD,
                    cloud,
                ),
            ),
            enabled=True,
            health_ledger=FailingLedger(Clock()),
        )
        result = asyncio.run(
            router.dispatch(
                QualifiedTargetAlias("gateway", "battery"),
                object(),
            ),
        )
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertEqual(cloud.calls, [])
        self.assertNotIn("secret", result.detail)

    def test_keyboard_interrupt_and_system_exit_are_not_swallowed(self):
        for exception in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(exception=type(exception).__name__):
                executor = Executor([exception])
                router = LatticeProviderFallbackRouter(
                    (
                        path(
                            "gateway",
                            "mqtt",
                            ProviderPathKind.GATEWAY_LOCAL,
                            executor,
                            aliases=("battery",),
                        ),
                    ),
                    enabled=True,
                    health_ledger=ProviderPathHealthLedger(
                        Clock(),
                    ),
                )
                with self.assertRaises(type(exception)):
                    asyncio.run(
                        router.dispatch(
                            QualifiedTargetAlias("gateway", "battery"),
                            object(),
                        ),
                    )

    def test_cancellation_is_not_swallowed_before_or_during_await(self):
        def cancelled_synchronously(_request):
            raise asyncio.CancelledError()

        async def cancelled_while_awaiting(_request):
            await asyncio.sleep(0)
            raise asyncio.CancelledError()

        for executor in (
            cancelled_synchronously,
            cancelled_while_awaiting,
        ):
            with self.subTest(executor=executor.__name__):
                ledger = ProviderPathHealthLedger(Clock())
                router = LatticeProviderFallbackRouter(
                    (
                        path(
                            "gateway",
                            "mqtt",
                            ProviderPathKind.GATEWAY_LOCAL,
                            executor,
                            aliases=("battery",),
                        ),
                    ),
                    enabled=True,
                    health_ledger=ledger,
                )
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(
                        router.dispatch(
                            QualifiedTargetAlias("gateway", "battery"),
                            object(),
                        ),
                    )
                self.assertIsNone(
                    ledger.record(
                        ProviderPathIdentity(
                            "battery-main",
                            "gateway",
                            "mqtt",
                        ),
                    ),
                )

    def test_router_can_be_reused_across_sequential_event_loops(self):
        executor = Executor(
            [
                ProviderExecutionResult.applied(),
                ProviderExecutionResult.applied(),
            ],
        )
        router = LatticeProviderFallbackRouter(
            (
                path(
                    "gateway",
                    "mqtt",
                    ProviderPathKind.GATEWAY_LOCAL,
                    executor,
                    aliases=("battery",),
                ),
            ),
            enabled=True,
            health_ledger=ProviderPathHealthLedger(Clock()),
        )
        alias = QualifiedTargetAlias("gateway", "battery")

        first = asyncio.run(router.dispatch(alias, object()))
        second = asyncio.run(router.dispatch(alias, object()))

        self.assertIs(first.state, ProviderExecutionState.APPLIED)
        self.assertIs(second.state, ProviderExecutionState.APPLIED)
        self.assertEqual(len(executor.calls), 2)


if __name__ == "__main__":
    unittest.main()

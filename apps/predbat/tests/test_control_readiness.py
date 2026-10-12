"""Test protected loaded projection and actual sealed startup without real endpoints."""

import copy
import asyncio
import json
import os
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from control_handoff import ControlAttachFailed, ControlWriteBlocked, start_control_handoff
from control_readiness import battery_manifest_digest, canonical_battery_value, loaded_battery_manifest
from control_handoff import ControlWriteGate
from gateway import GatewayMQTT


class ControlReadinessTests(unittest.TestCase):
    """Observe fresh physical identity while no component or battery writer can start."""

    def setUp(self):
        """Build a synthetic service-issued envelope over real loaded arguments."""
        self.args = {"user_id": "10000000-0000-4000-8000-000000000001", "ge_cloud_direct": True, "ge_cloud_key": "synthetic-key", "num_inverters": 1}
        ref = {"kind": "instance_secret", "instance_id": self.args["user_id"], "integration": "givenergy", "path": "api_key", "config_version": 7}
        routes = [{"manufacturer": "givenergy", "serial": "FD123", "gateway_ref": ref}]
        manifest = loaded_battery_manifest(self.args, {"schema": 1, "provider": "givenergy", "routes": routes, "credential_ref": ref, "battery_args": {}})
        self.envelope = {
            "state": "challenge",
            "activation_available": False,
            "challenge_id": "20000000-0000-4000-8000-000000000001",
            "transition_id": "30000000-0000-4000-8000-000000000001",
            "target_epoch": "40000000-0000-4000-8000-000000000001",
            "nonce": "ab" * 32,
            "config_version": 7,
            "provider": "givenergy",
            "routes": routes,
            "manifest": manifest,
            "manifest_digest": battery_manifest_digest(manifest),
            "boot_id": None,
            "expires_at": datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat(),
        }
        self.args.update(control_readiness=self.envelope, control_handoff={"enabled": True, "readiness": True}, saas_config_version=7)
        self.base = SimpleNamespace(args=self.args)
        self.env = {"SUPABASE_URL": "https://synthetic.invalid", "SUPABASE_KEY": "synthetic-service-key"}

    def response(self, value, status=200):
        """Return a real requests-shaped response with synthetic contents."""
        return SimpleNamespace(status_code=status, json=lambda: value)

    def post(self, _url, **kwargs):
        """Echo only immutable service response fields and inspect local admission."""
        self.assertTrue(self.base.control_write_gate.snapshot()["closed"])
        with self.assertRaises(ControlWriteBlocked):
            self.base.control_write_gate.admit("givenergy")
        body = kwargs["json"]
        return self.response(
            {"state": "attached" if body["action"] == "attach" else "observed", "challenge_id": body["challenge_id"], "target_epoch": body["binding"]["target_epoch"], "boot_id": body["boot_id"], "sealed": True, "activation_available": False}
        )

    def get(self, url, **kwargs):
        """Serve fresh account and telemetry reads using the actual loaded key."""
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer synthetic-key")
        self.assertFalse(kwargs["allow_redirects"])
        if url.endswith("communication-device"):
            return self.response({"data": [{"inverter": {"serial": "fd123", "connections": {"batteries": [{}]}}}], "meta": {"last_page": 1}})
        return self.response({"data": {"time": datetime.now(timezone.utc).isoformat(), "battery": {"percent": 45}}})

    def test_cross_language_canonical_bytes(self):
        """Match the exact TypeScript fixture including Unicode and IEEE-754 values."""
        self.assertEqual(canonical_battery_value({"z": -0.0, "a": [None, True, False, "é😀", 1.5]}), "o2:s1:aa5:ntfs6:é😀d3ff8000000000000s1:zd0000000000000000")
        self.assertEqual(canonical_battery_value(9007199254740991), "d433fffffffffffff")
        for value in (float("nan"), float("inf"), 9007199254740992, "\ud800", {"é": 1}, [None] * 257):
            with self.subTest(value=repr(value)), self.assertRaises((ValueError, UnicodeError)):
                canonical_battery_value(value)

    def test_actual_startup_records_observation_and_never_constructs_components(self):
        """Run PredBat.initialize, not merely the isolated helper function."""
        # Import the actual orchestrator without its unrelated release updater.
        with patch("download.check_install", return_value=(True, False)):
            import predbat as module

        self.base.log = lambda *_: None
        self.base.update_time = lambda **_kwargs: None
        self.base.now_utc_real = datetime.now(timezone.utc)
        self.base.reset = lambda: None
        self.base.record_status = lambda *_args, **_kwargs: None
        with patch.dict(os.environ, self.env), patch("control_readiness.requests.post", side_effect=self.post) as post, patch("control_readiness.requests.get", side_effect=self.get) as get, patch.object(module, "Components") as components:
            with self.assertRaisesRegex(ControlAttachFailed, "observation recorded"):
                module.PredBat.initialize(self.base)
        components.assert_not_called()
        self.assertEqual([call.kwargs["json"]["action"] for call in post.call_args_list], ["attach", "observe"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(post.call_args_list[1].kwargs["json"]["observation"]["serials"], ["FD123"])

    def test_changed_loaded_arguments_or_revision_never_claim_boot(self):
        """A corrected boot is possible because stale local values never attach."""
        for key, value in (("num_inverters", 2), ("saas_config_version", "7"), ("saas_config_version", True), ("auto_restart", True)):
            with self.subTest(key=key, value=value):
                args = copy.deepcopy(self.args)
                args[key] = value
                base = SimpleNamespace(args=args)
                with patch.dict(os.environ, self.env), patch("control_readiness.requests.post") as post, patch("control_readiness.requests.get") as get, self.assertRaises(ControlAttachFailed):
                    start_control_handoff(base)
                post.assert_not_called()
                get.assert_not_called()
                self.assertTrue(base.control_write_gate.snapshot()["closed"])

    def test_stale_or_wrong_physical_identity_never_reports_observation(self):
        """Successful account access cannot replace current exact inverter evidence."""
        for telemetry in ({"time": "2000-01-01T00:00:00Z", "battery": {"percent": 45}}, {"time": datetime.now(timezone.utc).isoformat(), "serial": "OTHER", "battery": {"percent": 45}}):
            self.base = SimpleNamespace(args=copy.deepcopy(self.args))

            def get(url, **kwargs):
                """Substitute only the fresh endpoint, keeping real discovery parsing."""
                return self.get(url, **kwargs) if url.endswith("communication-device") else self.response({"data": telemetry})

            with patch.dict(os.environ, self.env), patch("control_readiness.requests.post", side_effect=self.post) as post, patch("control_readiness.requests.get", side_effect=get), self.assertRaises(ControlAttachFailed):
                start_control_handoff(self.base)
            self.assertEqual(post.call_count, 1)
            self.assertTrue(self.base.control_write_gate.snapshot()["closed"])


class HubReadinessTransportTests(unittest.TestCase):
    """Race actual GatewayMQTT one-shot publishing and receipt validation."""

    def setUp(self):
        """Prepare one sealed process and a bounded signed-probe-shaped fixture."""
        self.base = SimpleNamespace(args={"gateway_device_id": "pbgw_test", "gateway_mqtt_host": "synthetic.invalid", "gateway_mqtt_port": 8883, "gateway_mqtt_token": "synthetic-token"})
        self.base.control_write_gate = ControlWriteGate("target", "epoch", 7, time.monotonic() + 60)
        self.base.control_write_gate.seal()
        self.command_id = "20000000-0000-4000-8000-000000000001"
        self.control = {
            "protocol": 1,
            "domain": "battery",
            "transition_id": "transition",
            "controller_epoch": "epoch",
            "boot_id": "current-boot",
            "generation": 4,
            "config_version": 7,
            "inverter_serials": ["FD123", "FD456"],
            "for_s": 120,
            "expires_at": int(time.time()) + 60,
        }
        self.wire = {"device_id": "pbgw_test", "command_id": self.command_id, "command": "probe_battery_control", "expires_at": self.control["expires_at"], "control_grant": {"payload": "synthetic", "signature": "synthetic"}}
        self.receipt = {**self.control, "phase": "probe_ready", "persisted": True, "pending": 0, "uncertain": 0}
        self.ack = {"command_id": self.command_id, "command": "probe_battery_control", "ok": True, "applied": {"control": self.receipt}}
        self.calls = []

    def client(self, messages, **kwargs):
        """Fake the socket boundary while running the actual GatewayMQTT method."""
        self.assertEqual(kwargs["password"], "synthetic-token")
        self.assertTrue(kwargs["clean_session"])
        calls = self.calls

        class Client:
            """Bounded broker adapter with ordered subscription and publish."""

            async def __aenter__(self):
                """Open the synthetic fresh connection."""
                return self

            async def __aexit__(self, *_args):
                """Close without retaining or resending any wire payload."""

            async def subscribe(self, topic, **options):
                """Record the exact command-specific ACK subscription."""
                calls.append(("subscribe", topic, options))
                return [0]

            async def publish(self, topic, payload, **options):
                """Record the sole permitted signed probe publication."""
                calls.append(("publish", topic, json.loads(payload), options))

            @property
            def messages(self):
                """Provide only packets delivered on this synthetic connection."""

                async def stream():
                    """Yield without creating cache or retained discovery authority."""
                    for message in messages:
                        yield message

                return stream()

        return Client()

    def observe(self, ack=None, retained=False, wire=None):
        """Execute the actual one-shot transport with a current command reply."""
        message = SimpleNamespace(topic="predbat/devices/pbgw_test/ack/" + self.command_id, payload=json.dumps(ack or self.ack).encode(), retain=retained)
        with patch("gateway.aiomqtt.Client", side_effect=lambda **kwargs: self.client([message], **kwargs)):
            return asyncio.run(GatewayMQTT.observe_readonly_transport(self.base, wire or self.wire, self.control, 1))

    def test_exact_probe_receipt_is_derived_from_actual_received_ack(self):
        """Subscribe before publishing, with no retained plans or control writes."""
        result = self.observe()
        self.assertEqual(result["serials"], ["FD123", "FD456"])
        self.assertEqual(result["receipt"], self.receipt)
        self.assertEqual(self.calls[0][0], "subscribe")
        self.assertEqual(self.calls[1], ("publish", "predbat/devices/pbgw_test/command", self.wire, {"qos": 1, "retain": False}))
        self.assertTrue(self.base.control_write_gate.snapshot()["closed"])

    def test_retained_replayed_wrong_boot_or_pending_receipts_never_pass(self):
        """Neither cached MQTT nor physical uncertainty can become readiness."""
        with self.assertRaises(ValueError):
            self.observe(retained=True)
        for change in ({"boot_id": "old-boot"}, {"pending": 1}, {"uncertain": 1}, {"inverter_serials": ["FD123"]}, {"expires_at": self.control["expires_at"] + 1}):
            ack = copy.deepcopy(self.ack)
            ack["applied"]["control"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.observe(ack=ack)

    def test_mutation_command_or_open_gate_cannot_use_readonly_path(self):
        """The dedicated probe route cannot bypass ordinary mutation admission."""
        with self.assertRaises(ValueError):
            self.observe(wire={**self.wire, "command": "set_charge_rate"})
        self.base.control_write_gate = ControlWriteGate("target", "epoch", 7, time.monotonic() + 60)
        with self.assertRaises(ValueError):
            self.observe()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

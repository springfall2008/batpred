"""Synthetic races at the actual GivEnergy and Gateway mutation sinks."""

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from control_handoff import ControlWriteBlocked, ControlWriteGate, ControlAttachFailed, ControlHandoffService, start_control_handoff, validate_control_component
from gecloud import GECloudDirect, GE_API_INVERTER_READ_SETTING, GE_API_INVERTER_WRITE_SETTING
from gateway import GatewayMQTT


def make_gate(provider="givenergy", clock=time.monotonic):
    """Make one synthetic controller with no external backend or credentials."""
    return ControlWriteGate(provider, "epoch-1", 7, clock() + 60, clock=clock)


def close_gate(gate):
    """Close exactly the synthetic source epoch."""
    gate.close("epoch-1", 7, "transition-1")


def drained(gate, timeout=0):
    """Read local transport-drain evidence for the synthetic binding."""
    return gate.wait_drained("epoch-1", 7, "transition-1", timeout)


def make_cloud(gate=None):
    """Construct the actual transport class without starting component threads."""
    cloud = GECloudDirect.__new__(GECloudDirect)
    cloud.base = SimpleNamespace()
    if gate is not None:
        cloud.base.control_write_gate = gate
    cloud.api_key = "synthetic-key"
    cloud.requests_total = 0
    cloud.failures_total = 0
    cloud.api_auth_failed = False
    cloud.pending_writes = {"A": []}
    cloud.log = lambda *_args, **_kwargs: None
    cloud.update_success_timestamp = lambda: None
    return cloud


class FakeResponse:
    """A controlled provider response, reached through the real aiohttp call site."""

    status = 200

    def __init__(self, outcome):
        """Save an awaitable body supplier."""
        self.outcome = outcome

    async def __aenter__(self):
        """Enter the synthetic response context."""
        return self

    async def __aexit__(self, *_args):
        """Leave the response context without consuming exceptions."""
        return False

    async def json(self):
        """Return or fail the body only when the test permits it."""
        return await self.outcome()


class FakeSession:
    """Intercept real transport requests without contacting a customer or provider."""

    def __init__(self, outcome):
        """Collect every submitted URL and body."""
        self.outcome = outcome
        self.requests = []

    async def __aenter__(self):
        """Enter a synthetic aiohttp session."""
        return self

    async def __aexit__(self, *_args):
        """Leave the session."""
        return False

    def post(self, url, **kwargs):
        """Record an actual POST admission."""
        self.requests.append((url, kwargs))
        return FakeResponse(self.outcome)


def make_gateway(gate, client, loop=None):
    """Create the actual MQTT component without opening a broker connection."""
    gateway = GatewayMQTT.__new__(GatewayMQTT)
    gateway.base = SimpleNamespace(control_write_gate=gate) if gate is not None else SimpleNamespace()
    gateway.gateway_display_only = False
    gateway._topic_base = "predbat/devices/pbgw_synthetic"
    gateway.topic_command = gateway._topic_base + "/command"
    gateway.topic_schedule = gateway._topic_base + "/schedule"
    gateway._mqtt_client = client
    gateway._mqtt_connected = True
    gateway._loop = loop
    gateway.log = lambda *_args, **_kwargs: None
    return gateway


class TestControlWriteGate(unittest.TestCase):
    """Thread-safe epoch/revision binding, timeouts and uncertainty semantics."""

    def test_binding_timeout_expiry_and_permanent_close(self):
        """No stale binding, wait timeout or expired lease can authorise drain."""
        clock = [100.0]
        gate = make_gate(clock=lambda: clock[0])
        token = gate.admit("givenergy")
        with self.assertRaises(ControlWriteBlocked):
            gate.admit("fox_ess")
        with self.assertRaises(ValueError):
            gate.close("old-epoch", 7, "transition-1")
        close_gate(gate)
        close_gate(gate)
        with self.assertRaises(ValueError):
            gate.close("epoch-1", 7, "another-transition")
        self.assertFalse(drained(gate))
        gate.complete(token, True)
        self.assertTrue(drained(gate))
        with self.assertRaises(ControlWriteBlocked):
            gate.admit("givenergy")
        clock[0] += 61
        self.assertFalse(drained(gate))
        with self.assertRaises(ValueError):
            gate.complete(token, True)

    def test_concurrent_admission_and_closure(self):
        """Race eight real threads against permanent source fencing."""
        gate = make_gate()
        barrier = threading.Barrier(9)
        outcomes = []

        def worker():
            """Attempt exactly one mutation across the close boundary."""
            barrier.wait()
            try:
                token = gate.admit("givenergy")
            except ControlWriteBlocked:
                outcomes.append("blocked")
            else:
                gate.complete(token, True)
                outcomes.append("finished")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        close_gate(gate)
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 8)
        self.assertTrue(drained(gate))
        with self.assertRaises(ControlWriteBlocked):
            gate.admit("givenergy")


class TestWiredCloudGate(unittest.IsolatedAsyncioTestCase):
    """Exercise the decorated production transport and its production retry caller."""

    async def test_pending_write_closes_new_admission_but_allows_post_reads(self):
        """Documented device success completes a local drain; reads still work."""
        gate = make_gate()
        cloud = make_cloud(gate)
        started, release = asyncio.Event(), asyncio.Event()

        async def outcome():
            """Hold one provider response across gate closure."""
            started.set()
            await release.wait()
            return {"data": {"value": 1, "success": True}}

        session = FakeSession(outcome)
        with patch("gecloud.aiohttp.ClientSession", return_value=session):
            write = asyncio.create_task(cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, post=True, datain={"value": "1"}))
            await started.wait()
            close_gate(gate)
            self.assertEqual(gate.snapshot()["pending"], 1)
            self.assertFalse(drained(gate))
            with self.assertRaises(ControlWriteBlocked):
                await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, post=True, datain={"value": "2"})
            self.assertEqual(len(session.requests), 1)
            release.set()
            self.assertEqual(await write, {"value": 1, "success": True})
            self.assertTrue(drained(gate))
            self.assertEqual(gate.snapshot()["uncertain"], 0)
            await cloud.async_get_inverter_data(GE_API_INVERTER_READ_SETTING, "A", 1, post=True)
            self.assertEqual(len(session.requests), 2)

    async def test_strict_vendor_success_and_truthy_post(self):
        """Malformed, missing and ambiguous results never become completion evidence."""
        cases = [
            ({"value": True, "success": True}, True),
            ({"value": 12.5, "success": True}, True),
            ({"value": 1}, False),
            ({"value": 1, "success": 1}, False),
            ({"value": "1", "success": True}, False),
            ({"success": True}, False),
            ({"value": float("nan"), "success": True}, False),
            ({"value": float("inf"), "success": True}, False),
            ({"value": -1, "success": False}, False),
            ({"value": -5, "success": False}, False),
            ({"value": -6, "success": False}, False),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                gate = make_gate()
                cloud = make_cloud(gate)

                async def outcome():
                    """Supply one unwrapped vendor response shape."""
                    return {"data": body}

                session = FakeSession(outcome)
                with patch("gecloud.aiohttp.ClientSession", return_value=session):
                    await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, 1)
                    close_gate(gate)
                    self.assertEqual(drained(gate), expected)
                    with self.assertRaises(ControlWriteBlocked):
                        await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, 1)
                self.assertEqual(len(session.requests), 1)

    async def test_retry_never_submits_after_closure(self):
        """Vendor timeout uncertainty remains even though subsequent retries are fenced."""
        gate = make_gate()
        cloud = make_cloud(gate)

        async def outcome():
            """Represent a potentially successful vendor timeout code."""
            return {"data": {"value": -1}}

        async def stop_retry(_delay):
            """Fence while the actual retry loop is backing off."""
            close_gate(gate)

        session = FakeSession(outcome)
        with patch("gecloud.aiohttp.ClientSession", return_value=session), patch("gecloud.asyncio.sleep", side_effect=stop_retry):
            with self.assertRaises(ControlWriteBlocked):
                await cloud.async_write_inverter_setting("A", 1, 1)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(gate.snapshot()["uncertain"], 1)
        self.assertFalse(drained(gate))

    async def test_timeout_late_application_never_claims_drained(self):
        """A server operation outlives its timed-out client; local idle is insufficient."""
        gate = make_gate()
        cloud = make_cloud(gate)
        apply_later = asyncio.Event()
        applied = []

        async def server():
            """Model remote work that cannot be cancelled by the client timeout."""
            await apply_later.wait()
            applied.append(1)

        async def outcome():
            """The transport times out before the provider operation finishes."""
            raise asyncio.TimeoutError()

        server_task = asyncio.create_task(server())
        with patch("gecloud.aiohttp.ClientSession", return_value=FakeSession(outcome)):
            self.assertIsNone(await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, post=True, datain={"value": "1"}))
        close_gate(gate)
        self.assertEqual(gate.snapshot()["pending"], 0)
        self.assertEqual(gate.snapshot()["uncertain"], 1)
        self.assertFalse(drained(gate))
        apply_later.set()
        await server_task
        self.assertEqual(applied, [1])
        self.assertFalse(drained(gate))

    async def test_cancelled_transport_is_ambiguous(self):
        """Cancellation never becomes a positive source-drained receipt."""
        gate = make_gate()
        cloud = make_cloud(gate)
        started = asyncio.Event()

        async def outcome():
            """Hold the actual write response until its client is cancelled."""
            started.set()
            await asyncio.Event().wait()

        with patch("gecloud.aiohttp.ClientSession", return_value=FakeSession(outcome)):
            task = asyncio.create_task(cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, post=True, datain={"value": "1"}))
            await started.wait()
            close_gate(gate)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(drained(gate))
        self.assertEqual(gate.snapshot()["uncertain"], 1)

    async def test_no_gate_preserves_normal_transport(self):
        """Normal installations require no transition configuration."""
        cloud = make_cloud()

        async def outcome():
            """Return the normal successful write response."""
            return {"data": {"value": 1}}

        session = FakeSession(outcome)
        with patch("gecloud.aiohttp.ClientSession", return_value=session):
            self.assertEqual(await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, post=True, datain={"value": "1"}), {"value": 1})
        self.assertEqual(len(session.requests), 1)


class TestWiredGatewayGate(unittest.IsolatedAsyncioTestCase):
    """MQTT owner-loop futures, retained schedules and optional display traffic."""

    async def test_owner_loop_future_survives_waiter_cancellation_and_blocks_drain(self):
        """Keep a submitted retained schedule counted until the owning loop finishes."""
        gate = make_gate("predbat_gateway")
        started, release = threading.Event(), threading.Event()
        calls = []
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever)
        thread.start()

        async def publish(topic, payload, **kwargs):
            """Model a pending broker publish on its actual owning thread."""
            calls.append((topic, payload, kwargs))
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.001)

        gateway = make_gateway(gate, SimpleNamespace(publish=publish), loop)
        try:
            task = asyncio.create_task(gateway._publish_raw(gateway.topic_schedule, b"schedule", retain=True))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            close_gate(gate)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(gate.snapshot()["pending"], 1)
            self.assertFalse(drained(gate))
            with self.assertRaises(ControlWriteBlocked):
                await gateway._publish_raw(gateway.topic_command, b"command")
            release.set()
            for _ in range(1000):
                if gate.snapshot()["pending"] == 0:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(gate.snapshot()["pending"], 0)
            self.assertEqual(gate.snapshot()["uncertain"], 1)
            self.assertFalse(drained(gate))
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][2]["retain"])
            await gateway._publish_raw(gateway._topic_base + "/predbat_data", b"display")
            self.assertEqual(len(calls), 2)
            await gateway._publish_raw(gateway._topic_base + "/query", b"read-only-query")
            self.assertEqual(len(calls), 3)
            await gateway._publish_raw(gateway._topic_base + "/ev/command", b"independent-charger")
            self.assertEqual(len(calls), 4)
        finally:
            release.set()
            loop.call_soon_threadsafe(loop.stop)
            await asyncio.to_thread(thread.join, 2)
            loop.close()

    async def test_queued_retained_plan_cannot_escape_closed_epoch(self):
        """The actual reconnect and periodic schedule paths reach the fenced sink."""
        gate = make_gate("predbat_gateway")
        calls = []

        async def publish(*args, **kwargs):
            """Collect only publishes that reached the transport."""
            calls.append((args, kwargs))

        gateway = make_gateway(gate, SimpleNamespace(publish=publish))
        gateway._mqtt_connected = False
        gateway._last_published_plan = None
        gateway._pending_plan = None
        await gateway.publish_plan([], "UTC")
        self.assertEqual(gateway._pending_plan, ([], "UTC"))
        close_gate(gate)
        gateway._mqtt_connected = True
        gateway._plan_version = 1
        gateway.build_execution_plan = lambda *_args, **_kwargs: b"schedule"
        gateway._debug_dump = lambda *_args, **_kwargs: None
        with self.assertRaises(ControlWriteBlocked):
            await gateway.publish_plan(*gateway._pending_plan)
        gateway._last_plan_publish_time = 0
        with self.assertRaises(ControlWriteBlocked):
            await gateway._republish_plan_if_stale()
        self.assertEqual(calls, [])


class ServiceResponse:
    """A local synchronous HTTP response for the real service client boundary."""

    def __init__(self, data=None, status=200):
        """Keep the response body and status without any network activity."""
        self.data = data
        self.status_code = status
        self.content = b"{}"

    def json(self):
        """Return JSON or simulate a malformed successful response."""
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class ControlServiceTests(unittest.TestCase):
    """Exercise startup, real HTTP call sites and permanently fenced renewals."""

    instance = "11111111-1111-4111-8111-111111111111"
    boot = "22222222-2222-4222-8222-222222222222"
    epoch = "33333333-3333-4333-8333-333333333333"
    transition = "44444444-4444-4444-8444-444444444444"

    def setUp(self):
        """Use a deterministic monotonic clock and synthetic service credentials."""
        self.now = 100.0
        self.base = SimpleNamespace(args={"control_handoff": {"enabled": True}, "user_id": self.instance, "ge_cloud_direct": True, "ge_cloud_key": "synthetic-inverter-key"}, log=lambda *_: None)
        self.service = ControlHandoffService(self.base, self.instance, self.boot, "https://service.invalid", "synthetic-service-key", clock=lambda: self.now)

    def active(self, **changes):
        """Return an authoritative same-boot active response."""
        data = {"state": "active", "provider": "givenergy", "epoch": self.epoch, "config_version": 7, "boot_id": self.boot, "lease_for_s": 60}
        data.update(changes)
        return data

    def attach(self):
        """Attach through the actual requests.post boundary."""
        self.service._stop.clear()
        with patch("control_handoff.requests.post", return_value=ServiceResponse(self.active())):
            self.base.control_write_gate = self.service.attach()

    def closing(self):
        """Return an exact service-authorized closing transition."""
        data = self.active(state="closing", transition_id=self.transition)
        data.pop("lease_for_s")
        return data

    def test_attach_uses_service_credentials_and_exact_boot(self):
        """Bind only the service-issued epoch and never follow credential redirects."""
        with patch("control_handoff.requests.post", return_value=ServiceResponse(self.active())) as post:
            gate = self.service.attach()
        self.assertEqual(gate.epoch, self.epoch)
        call = post.call_args
        self.assertEqual(call.args[0], "https://service.invalid/functions/v1/engine-battery-control")
        self.assertEqual(call.kwargs["json"], {"action": "attach", "instance_id": self.instance, "boot_id": self.boot})
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer synthetic-service-key")

    def test_attach_rejects_malformed_or_delayed_lease(self):
        """Strict types, identities and request-start deadlines prevent stale release."""
        for changes in ({"lease_for_s": True}, {"lease_for_s": 91}, {"lease_for_s": 0}, {"config_version": True}, {"config_version": 0}, {"boot_id": self.epoch}, {"epoch": "bad"}, {"provider": "GivEnergy"}):
            with self.subTest(changes=changes), patch("control_handoff.requests.post", return_value=ServiceResponse(self.active(**changes))):
                with self.assertRaises(ControlAttachFailed):
                    self.service.attach()

        def delayed(*_args, **_kwargs):
            """Consume the entire granted lease before HTTP completes."""
            self.now += 60
            return ServiceResponse(self.active())

        with patch("control_handoff.requests.post", side_effect=delayed), self.assertRaises(ControlAttachFailed):
            self.service.attach()

    def test_renewal_is_bound_to_request_start_and_never_reopens_expired_gate(self):
        """A delayed renewal cannot extend from response arrival or revive expiry."""
        self.attach()
        self.now = 110

        def delayed(*_args, **_kwargs):
            """Return a 30 second grant after five seconds of transport delay."""
            self.now = 115
            return ServiceResponse(self.active(lease_for_s=30))

        with patch("control_handoff.requests.post", side_effect=delayed):
            self.service.poll_once()
        self.assertEqual(self.service.gate.expires_at, 140)
        self.now = 140
        with patch("control_handoff.requests.post", return_value=ServiceResponse(self.active())):
            self.service.poll_once()
        with self.assertRaises(ControlWriteBlocked):
            self.service.gate.admit("givenergy")

    def test_transient_failure_retains_only_live_lease_and_rejection_seals(self):
        """Outages keep a bounded existing lease, service refusal closes immediately."""
        self.attach()
        with patch("control_handoff.requests.post", return_value=ServiceResponse(status=500)):
            self.service.poll_once()
        token = self.service.gate.admit("givenergy")
        self.service.gate.complete(token, True)
        for status in (409, 503, 302):
            self.attach()
            with patch("control_handoff.requests.post", return_value=ServiceResponse(status=status)):
                self.service.poll_once()
            self.assertTrue(self.service.gate.snapshot()["closed"])
        self.attach()
        self.now = 161
        with patch("control_handoff.requests.post", return_value=ServiceResponse(status=500)):
            self.service.poll_once()
        self.assertTrue(self.service.gate.snapshot()["closed"])

    def test_wrong_binding_and_malformed_json_seal_without_report(self):
        """Neither another epoch nor broken JSON can keep write authority alive."""
        for response in (ServiceResponse(self.active(epoch=self.transition)), ServiceResponse(self.active(boot_id=self.epoch)), ServiceResponse(ValueError("synthetic invalid JSON"))):
            self.service._stop.clear()
            self.attach()
            with patch("control_handoff.requests.post", return_value=response) as post:
                self.service.poll_once()
            self.assertTrue(self.service.gate.snapshot()["closed"])
            self.assertEqual(post.call_count, 1)

    def test_closing_waits_actual_cloud_transport_then_reports_exact_evidence(self):
        """A held real mutation prevents a success report until its outcome settles."""
        self.attach()
        cloud = make_cloud(self.service.gate)

        async def race():
            """Hold the actual decorated mutation across the service closing response."""
            entered = asyncio.Event()
            release = asyncio.Event()

            async def outcome():
                """Return a definitive provider response only after closure."""
                entered.set()
                await release.wait()
                return {"data": {"success": True, "value": 50}}

            session = FakeSession(outcome)
            with patch("gecloud.aiohttp.ClientSession", return_value=session):
                task = asyncio.create_task(cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, "A", 1, post=True, datain={"value": 50}))
                await entered.wait()
                with patch("control_handoff.requests.post", return_value=ServiceResponse(self.closing())) as post:
                    self.service.poll_once()
                    self.assertEqual(post.call_count, 1)
                self.assertEqual(self.service.gate.snapshot()["pending"], 1)
                release.set()
                await task
            with patch("control_handoff.requests.post", return_value=ServiceResponse({"state": "drained"})) as post:
                self.service.poll_once()
            evidence = post.call_args.kwargs["json"]["evidence"]
            self.assertEqual(evidence, {"provider": "givenergy", "epoch": self.epoch, "config_version": 7, "transition_id": self.transition, "closed": True, "pending": 0, "uncertain": 0, "boot_id": self.boot})

        asyncio.run(race())

    def test_ambiguity_is_reported_not_converted_to_drained(self):
        """A lost physical outcome stays visible in the service receipt."""
        self.attach()
        token = self.service.gate.admit("givenergy")
        self.service.gate.complete(token, False)
        with patch("control_handoff.requests.post", side_effect=[ServiceResponse(self.closing()), ServiceResponse({"state": "ambiguous"})]) as post:
            self.service.poll_once()
        self.assertEqual(post.call_args.kwargs["json"]["evidence"]["uncertain"], 1)
        self.assertTrue(self.service.gate.snapshot()["closed"])

    def test_shutdown_never_publishes_a_receipt_or_accepts_late_renewal(self):
        """Shutdown fences first and ignores a response already in flight."""
        self.attach()

        def shutdown_during_request(*_args, **_kwargs):
            """Simulate service stop while HTTP is pending."""
            self.service.stop()
            return ServiceResponse(self.active())

        with patch("control_handoff.requests.post", side_effect=shutdown_during_request) as post:
            self.service.poll_once()
        self.assertEqual(post.call_count, 1)
        self.assertTrue(self.service.gate.snapshot()["closed"])

    def test_actual_startup_stops_before_components_on_attach_failure(self):
        """Invoke PredBat.initialize itself and prove failed enrollment has no fallback."""
        import predbat as predbat_module
        from datetime import datetime, timezone

        base = self.base
        base.update_time = lambda **_kwargs: None
        base.now_utc_real = datetime.now(timezone.utc)
        base.reset = lambda: None
        base.record_status = lambda *_args, **_kwargs: None
        import requests

        for failure in (ServiceResponse(status=503), ServiceResponse(ValueError("malformed")), requests.Timeout("synthetic-service-key")):
            base.__dict__.pop("control_write_gate", None)  # A fresh app object for each simulated boot.

            def response(*_args, **_kwargs):
                """Simulate refusal, invalid JSON or an actual transport exception."""
                if isinstance(failure, Exception):
                    raise failure
                return failure

            messages = []
            base.log = messages.append
            with patch.dict("os.environ", {"SUPABASE_URL": "https://service.invalid", "SUPABASE_KEY": "synthetic-service-key"}), patch("control_handoff.requests.post", side_effect=response), patch.object(predbat_module, "Components") as components:
                with self.assertRaises(ControlAttachFailed):
                    predbat_module.PredBat.initialize(base)
            components.assert_not_called()
            with self.assertRaises(ControlWriteBlocked):
                base.control_write_gate.admit("givenergy")
            self.assertNotIn("synthetic-service-key", " ".join(messages))

    def test_actual_startup_has_gate_before_component_construction(self):
        """The real initialize call exposes a live gate only after verified attach."""
        import predbat as predbat_module
        from datetime import datetime, timezone

        base = self.base
        base.update_time = lambda **_kwargs: None
        base.now_utc_real = datetime.now(timezone.utc)
        base.reset = lambda: None
        base.record_status = lambda *_args, **_kwargs: None

        def response(*_args, **kwargs):
            """Echo the actual boot generated before the startup request."""
            return ServiceResponse(self.active(boot_id=kwargs["json"]["boot_id"]))

        def components(constructed_base):
            """Check the startup boundary then halt before peripheral construction."""
            token = constructed_base.control_write_gate.admit("givenergy")
            constructed_base.control_write_gate.complete(token, True)
            raise RuntimeError("synthetic construction boundary")

        with patch.dict("os.environ", {"SUPABASE_URL": "https://service.invalid", "SUPABASE_KEY": "synthetic-service-key"}), patch("control_handoff.requests.post", side_effect=response), patch("control_handoff.ControlHandoffService.start"), patch.object(
            predbat_module, "Components", side_effect=components
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic construction boundary"):
                predbat_module.PredBat.initialize(base)
        self.assertTrue(base.control_write_gate.snapshot()["closed"])

    def test_actual_worker_thread_closes_and_reports_without_event_loop_blocking(self):
        """Run the real worker through closing and idempotent service publication."""
        self.attach()
        self.service._poll_delay = 0.001
        with patch("control_handoff.requests.post", side_effect=[ServiceResponse(self.closing()), ServiceResponse({"state": "drained"})]) as post:
            self.service.start()
            self.assertTrue(self.service._stop.wait(2))
            self.service._thread.join(2)
        self.assertFalse(self.service._thread.is_alive())
        self.assertEqual([call.kwargs["json"]["action"] for call in post.call_args_list], ["poll", "drained"])

    def test_actual_terminate_fences_before_component_shutdown(self):
        """Invoke PredBat.terminate and inspect the actual ordering boundary."""
        import predbat as predbat_module

        self.attach()
        self.base.control_handoff_service = self.service

        async def stop_components():
            """Check components cannot perform a shutdown mutation."""
            with self.assertRaises(ControlWriteBlocked):
                self.service.gate.admit("givenergy")

        self.base.components = SimpleNamespace(stop=stop_components)
        with patch("control_handoff.requests.post") as post:
            asyncio.run(predbat_module.PredBat.terminate(self.base))
        post.assert_not_called()

    def test_actual_ordinary_startup_reaches_components_without_gate(self):
        """The actual startup path stays unchanged for unenrolled installations."""
        import predbat as predbat_module
        from datetime import datetime, timezone

        base = SimpleNamespace(args={}, log=lambda *_: None, update_time=lambda **_kwargs: None, now_utc_real=datetime.now(timezone.utc), reset=lambda: None, record_status=lambda *_args, **_kwargs: None)

        def components(constructed_base):
            """Stop at the first component after confirming legacy admission."""
            self.assertNotIn("control_write_gate", constructed_base.__dict__)
            raise RuntimeError("synthetic ordinary startup boundary")

        with patch.object(predbat_module, "Components", side_effect=components), patch("control_handoff.requests.post") as post:
            with self.assertRaisesRegex(RuntimeError, "synthetic ordinary startup boundary"):
                predbat_module.PredBat.initialize(base)
        post.assert_not_called()

    def test_closing_expiry_stops_without_posting_a_late_receipt(self):
        """An expired local grant cannot certify a source transition."""
        self.attach()
        token = self.service.gate.admit("givenergy")
        with patch("control_handoff.requests.post", return_value=ServiceResponse(self.closing())):
            self.service.poll_once()
        self.service.gate.complete(token, True)
        self.now = 161
        with patch("control_handoff.requests.post") as post:
            self.service.poll_once()
        post.assert_not_called()
        self.assertTrue(self.service.gate.snapshot()["closed"])

    def test_real_component_registry_refuses_unsupported_inverter_and_external_ha(self):
        """Use actual eligibility and constructors, including restart initialization."""
        from components import Components, COMPONENT_LIST

        self.attach()
        self.base.args_from_apps_yaml = self.base.args
        self.base.get_arg = lambda key, default=None, **_kwargs: self.base.args.get(key, default)
        self.base.args["fox_key"] = "synthetic-fox-key"
        components = Components(self.base)
        with patch("components.load_component_class") as load:
            with self.assertRaises(ControlAttachFailed):
                components.initialize(only="fox", phase=COMPONENT_LIST["fox"]["phase"])
        load.assert_not_called()
        self.assertTrue(self.service.gate.snapshot()["closed"])
        self.attach()
        self.base.args["givtcp_rest"] = "https://synthetic-rest.invalid"
        with patch("components.load_component_class") as load:
            with self.assertRaises(ControlAttachFailed):
                components.initialize(only="givtcp", phase=1)
        load.assert_not_called()
        self.assertTrue(self.service.gate.snapshot()["closed"])
        self.attach()
        self.base.args["ha_key"] = "synthetic-ha-key"
        with patch("components.load_component_class") as load:
            with self.assertRaises(ControlAttachFailed):
                components.initialize(only="ha", phase=0)
        load.assert_not_called()
        self.assertTrue(self.service.gate.snapshot()["closed"])

    def test_component_guard_preserves_independent_charger_and_display_paths(self):
        """Only uncertified battery and external HA transports are rejected."""
        from components import COMPONENT_LIST

        self.attach()
        for name in ("gecloud", "gateway", "ha", "ohme", "wallbox"):
            validate_control_component(self.base, COMPONENT_LIST[name], {"ha_key": None})
        self.assertFalse(self.service.gate.snapshot()["closed"])

    def test_selected_control_provider_is_required_before_components(self):
        """A direct-provider session cannot certify a display-only or absent writer."""
        self.base.args["ge_cloud_direct"] = False

        def response(*_args, **kwargs):
            """Echo startup boot while returning the enrolled provider."""
            return ServiceResponse(self.active(boot_id=kwargs["json"]["boot_id"]))

        with patch.dict("os.environ", {"SUPABASE_URL": "https://service.invalid", "SUPABASE_KEY": "synthetic-service-key"}), patch("control_handoff.requests.post", side_effect=response), patch("control_handoff.ControlHandoffService.start") as start:
            with self.assertRaises(ControlAttachFailed):
                start_control_handoff(self.base)
        start.assert_not_called()
        self.assertTrue(self.base.control_write_gate.snapshot()["closed"])

    def test_reinitialization_cannot_replace_gate_or_lose_pending_work(self):
        """Even a changed opt-out cannot detach a previously enrolled process."""
        self.attach()
        self.base.control_handoff_service = self.service
        gate = self.service.gate
        token = gate.admit("givenergy")
        self.base.args["control_handoff"] = {"enabled": False}
        with patch("control_handoff.requests.post") as post, self.assertRaises(ControlAttachFailed):
            start_control_handoff(self.base)
        post.assert_not_called()
        self.assertIs(self.base.control_write_gate, gate)
        self.assertTrue(gate.snapshot()["closed"])
        self.assertEqual(gate.snapshot()["pending"], 1)
        gate.complete(token, True)

    def test_opt_out_and_invalid_opt_in_are_strict(self):
        """Default installs stay unchanged; truthy strings never bypass a gate."""
        for config in (None, {}, {"enabled": False}):
            self.base.args["control_handoff"] = config
            self.assertIsNone(start_control_handoff(self.base))
            self.assertFalse(hasattr(self.base, "control_write_gate"))
        for config in (True, "true", {"enabled": "true"}, {"enabled": 1}, {"enabled": True, "epoch": self.epoch}):
            self.base.args["control_handoff"] = config
            with self.assertRaises(ControlAttachFailed):
                start_control_handoff(self.base)
            self.assertTrue(self.base.control_write_gate.snapshot()["closed"])


class TestControlDebugExport(unittest.TestCase):
    """Live controller objects must never enter shared debug/state serialization."""

    def test_actual_debug_export_excludes_live_control_objects(self):
        """Keep debug downloads usable without traversing locks or credentials."""
        from datetime import datetime, timezone
        from tempfile import TemporaryDirectory

        import yaml
        from userinterface import UserInterface
        from utils import is_debug_excluded_key

        with TemporaryDirectory() as root:
            base = SimpleNamespace(
                now_utc=datetime.now(timezone.utc),
                config_root=root,
                config_root_p=root,
                inverters=[],
                CONFIG_ITEMS=[],
                components=None,
                args={},
                minutes_now=42,
                log=lambda *_args: None,
            )
            gate = make_gate()
            service = ControlHandoffService(base, "synthetic-instance", "synthetic-boot", "https://example.invalid", "synthetic-private-service-credential")
            service.gate = gate
            base.control_write_gate = gate
            base.control_handoff_service = service
            text = UserInterface.create_debug_yaml(base, write_file=False)
            debug = yaml.safe_load(text)
            self.assertEqual(debug["minutes_now"], 42)
            for name in ("control_write_gate", "control_handoff_service"):
                self.assertTrue(is_debug_excluded_key(name))
                self.assertNotIn(name, debug)
            self.assertNotIn("synthetic-private-service-credential", text)
            self.assertNotIn("python/object", text)


def run_control_handoff_tests(_predbat=None):
    """Run through Predbat's normal unit-test registry as well as unittest."""
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["_"]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()


if __name__ == "__main__":
    unittest.main()

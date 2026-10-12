"""Synthetic races at the actual GivEnergy and Gateway mutation sinks."""

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from control_handoff import ControlWriteBlocked, ControlWriteGate
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


def run_control_handoff_tests(_predbat=None):
    """Run through Predbat's normal unit-test registry as well as unittest."""
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__, fromlist=["_"]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()


if __name__ == "__main__":
    unittest.main()

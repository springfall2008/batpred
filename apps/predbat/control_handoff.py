"""Optional process-local write fencing, not a backend handoff acknowledgement.

Ordinary installations have no gate. Explicit service enrollment attaches a
fail-closed gate before components start and reports only local transport evidence.
"""

import asyncio
import functools
import inspect
import math
import threading
import time
import os
import uuid
from urllib.parse import urlsplit

import requests


class ControlWriteBlocked(RuntimeError):
    """A closed, expired or wrong-provider epoch cannot admit another mutation."""


class ControlWriteGate:
    """Fence transport attempts across component threads and independent event loops.

    Closing admission is permanent. Uncertain remote outcomes remain latched,
    even after all local tasks finish. A transport drain is never proof that a
    Hub is physically fenced or that a backend transition was committed.
    """

    def __init__(self, provider, epoch, config_version, expires_at, clock=time.monotonic):
        """Bind one gate to a controller epoch and a monotonic lease deadline."""
        if not provider or not epoch or isinstance(config_version, bool) or not isinstance(config_version, int):
            raise ValueError("Invalid controller binding")
        if not math.isfinite(expires_at) or expires_at <= clock():
            raise ValueError("Invalid controller lease")
        self.provider = provider
        self.epoch = epoch
        self.config_version = config_version
        self.expires_at = expires_at
        self._clock = clock
        self._condition = threading.Condition()
        self._closed = False
        self._transition_id = None
        self._next_token = 0
        self._pending = set()
        self._uncertain = set()

    def admit(self, provider):
        """Atomically admit one transport attempt or reject before transmission."""
        with self._condition:
            if self._clock() >= self.expires_at:
                self._closed = True
            if self._closed or provider != self.provider:
                raise ControlWriteBlocked("Controller epoch cannot admit a mutation")
            self._next_token += 1
            self._pending.add(self._next_token)
            return self._next_token

    def seal(self):
        """Permanently refuse admission without inventing a transition identity."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def renew(self, provider, epoch, config_version, expires_at):
        """Accept only a server renewal of this still-live, unchanged binding."""
        with self._condition:
            now = self._clock()
            if now >= self.expires_at:
                self._closed = True
            if self._closed or provider != self.provider or epoch != self.epoch or config_version != self.config_version:
                raise ControlWriteBlocked("Controller renewal cannot reopen admission")
            if not math.isfinite(expires_at) or expires_at <= now:
                self._closed = True
                raise ControlWriteBlocked("Controller renewal already expired")
            self.expires_at = expires_at

    def complete(self, token, definitive):
        """Finish local work, retaining every remotely ambiguous outcome."""
        with self._condition:
            if token not in self._pending:
                raise ValueError("Unknown or already completed mutation")
            self._pending.remove(token)
            if definitive is not True:
                self._uncertain.add(token)
            self._condition.notify_all()

    def close(self, epoch, config_version, transition_id):
        """Permanently close this exact epoch for one transition, idempotently."""
        with self._condition:
            if epoch != self.epoch or config_version != self.config_version or not transition_id:
                raise ValueError("Wrong controller binding")
            if self._transition_id is not None and transition_id != self._transition_id:
                raise ValueError("Controller already belongs to another transition")
            self._closed = True
            self._transition_id = transition_id
            self._condition.notify_all()

    def snapshot(self):
        """Return local evidence without claiming a durable or physical handoff."""
        with self._condition:
            return {
                "provider": self.provider,
                "epoch": self.epoch,
                "config_version": self.config_version,
                "transition_id": self._transition_id,
                "closed": self._closed,
                "expired": self._clock() >= self.expires_at,
                "pending": len(self._pending),
                "uncertain": len(self._uncertain),
            }

    def wait_drained(self, epoch, config_version, transition_id, timeout):
        """Wait for local drain only; timeout, expiry and ambiguity return false.

        This synchronous method belongs on a worker thread, never on a component
        event loop needed to finish admitted work. Cancelling an async caller
        must not cause it to emit an acknowledgement from a finally block.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Invalid drain timeout")
        deadline = time.monotonic() + timeout
        with self._condition:
            if not self._closed or self._transition_id != transition_id or epoch != self.epoch or config_version != self.config_version:
                raise ValueError("Drain requires the exact closed transition")
            while self._pending:
                remaining = min(deadline - time.monotonic(), self.expires_at - self._clock())
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return not self._uncertain and self._clock() < self.expires_at


def control_gate_for(base):
    """Use only an explicitly attached gate; ordinary installations are unchanged."""
    gate = getattr(base, "__dict__", {}).get("control_write_gate")
    if gate is not None and not isinstance(gate, ControlWriteGate):
        raise ControlWriteBlocked("Invalid controller gate")
    return gate


def control_mutation(provider, endpoint, definitive_result, definitive_error):
    """Fence each actual Cloud mutation, including each retry's transport call."""

    def decorate(function):
        """Wrap a transport without changing its public calling convention."""
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def guarded(self, *args, **kwargs):
            """Track one submitted write until its complete classified outcome."""
            gate = control_gate_for(getattr(self, "base", None))
            if gate is None:
                return await function(self, *args, **kwargs)
            arguments = signature.bind(self, *args, **kwargs)
            arguments.apply_defaults()
            if arguments.arguments.get("endpoint") != endpoint or not arguments.arguments.get("post"):
                return await function(self, *args, **kwargs)
            token = gate.admit(provider)
            definitive = False
            try:
                result = await function(self, *args, **kwargs)
                definitive = definitive_result(result) is True
                return result
            except Exception as error:
                definitive = definitive_error(error) is True
                raise
            finally:
                # BaseException (including cancellation) deliberately stays uncertain.
                gate.complete(token, definitive)

        return guarded

    return decorate


async def gated_mqtt_publish(gate, provider, publish, owner_loop):
    """Keep owner-loop work counted even if its awaiting caller is cancelled.

    Broker acceptance is not physical completion. Every submitted MQTT mutation
    stays uncertain until a future protocol can prove the physical fence.
    """
    token = gate.admit(provider)

    def finished(future):
        """Consume completion and retain an unresolved physical outcome."""
        if not future.cancelled():
            future.exception()
        gate.complete(token, False)

    coroutine = None
    try:
        coroutine = publish()
        if owner_loop is not None and owner_loop is not asyncio.get_running_loop():
            future = asyncio.run_coroutine_threadsafe(coroutine, owner_loop)
            future.add_done_callback(finished)
            waiter = asyncio.wrap_future(future)
        else:
            future = asyncio.ensure_future(coroutine)
            future.add_done_callback(finished)
            waiter = future
    except BaseException:
        if coroutine is not None:
            coroutine.close()
        gate.complete(token, False)
        raise
    await asyncio.shield(waiter)


class ControlAttachFailed(RuntimeError):
    """Explicit enrollment failed, so startup must stop before components exist."""


def _uuid(value):
    """Accept only a canonical lower-case UUID string, never a generated fallback."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class ControlHandoffService:
    """Poll a service-only controller lease on a dedicated bounded worker thread.

    This reports process transport counts, never a Hub physical fence. The
    service independently validates enrollment, reservation and receipt expiry.
    """

    def __init__(self, base, instance_id, boot_id, url, key, clock=time.monotonic):
        """Keep service credentials private and bind this process boot once."""
        self.base = base
        self.instance_id = instance_id
        self.boot_id = boot_id
        self._url = url.rstrip("/") + "/functions/v1/engine-battery-control"
        self._key = key
        self._clock = clock
        self._stop = threading.Event()
        self._thread = None
        self.gate = None
        self._poll_delay = 1.0
        self._closing = False

    def _request(self, action, evidence=None):
        """Call the fixed service endpoint with redirects and exception logging off."""
        payload = {"action": action, "instance_id": self.instance_id, "boot_id": self.boot_id}
        if action == "attach":
            payload["loaded_config_version"] = self.base.args.get("saas_config_version")
        if self.gate is not None:
            payload["epoch"] = self.gate.epoch
        if evidence is not None:
            payload["evidence"] = evidence
        started = self._clock()
        try:
            response = requests.post(
                self._url,
                headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
                json=payload,
                timeout=(3, 5),
                allow_redirects=False,
            )
            if response.status_code != 200:
                return started, None, response.status_code
            if len(response.content) > 8192:
                return started, None, 400
            return started, response.json(), 200
        except requests.RequestException:
            return started, None, None
        except (ValueError, TypeError):
            return started, None, 400

    def _binding(self, data):
        """Validate a complete service response before accepting any authority."""
        return isinstance(data, dict) and data.get("provider") in ("givenergy", "predbat_gateway") and _uuid(data.get("epoch")) and type(data.get("config_version")) is int and data["config_version"] > 0 and data.get("boot_id") == self.boot_id

    def attach(self):
        """Claim the pre-enrolled epoch before any component can be constructed."""
        loaded_version = self.base.args.get("saas_config_version")
        if type(loaded_version) is not int or not 1 <= loaded_version <= 2147483647:
            raise ControlAttachFailed("Loaded controller configuration revision unavailable")
        started, data, status = self._request("attach")
        if status != 200 or not self._binding(data) or data.get("state") != "active":
            raise ControlAttachFailed("Controller service attach unavailable")
        if data["config_version"] != loaded_version:
            raise ControlAttachFailed("Loaded controller configuration revision changed")
        lease = data.get("lease_for_s")
        if type(lease) is not int or not 1 <= lease <= 90 or started + lease <= self._clock():
            raise ControlAttachFailed("Controller service attach lease invalid")
        self.gate = ControlWriteGate(data["provider"], data["epoch"], data["config_version"], started + lease, clock=self._clock)
        self._poll_delay = min(15.0, lease / 3.0)
        return self.gate

    def start(self):
        """Start only after startup has installed the successfully attached gate."""
        if self.gate is None or self._thread is not None:
            raise ControlAttachFailed("Controller service worker invalid")
        self._thread = threading.Thread(target=self._run, name="battery-control-service", daemon=True)
        self._thread.start()

    def stop(self):
        """Seal first, then stop the worker without publishing from cleanup."""
        if self.gate is not None:
            self.gate.seal()
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=9)

    def _same_binding(self, data):
        """Require the pinned provider, epoch, configuration and process boot."""
        return self._binding(data) and data["provider"] == self.gate.provider and data["epoch"] == self.gate.epoch and data["config_version"] == self.gate.config_version

    def _publish_drained(self):
        """Report only an exact closed snapshot after all local work finishes."""
        if self._stop.is_set():
            return
        if self._clock() >= self.gate.expires_at:
            self.stop()
            return
        evidence = self.gate.snapshot()
        if not evidence["closed"] or evidence["pending"] or not _uuid(evidence["transition_id"]):
            return
        evidence.pop("expired")
        evidence["boot_id"] = self.boot_id
        _, data, status = self._request("drained", evidence)
        if status == 200 or status is not None and status < 500 or status == 503:
            self._stop.set()

    def poll_once(self):
        """Apply one authoritative response, retaining only a live lease on outages."""
        if self._stop.is_set():
            return
        if self._clock() >= self.gate.expires_at:
            self.stop()
            return
        if self._closing:
            self._publish_drained()
            return
        started, data, status = self._request("poll")
        if self._stop.is_set():
            return
        if status is None or status >= 500 and status != 503:
            if self._clock() >= self.gate.expires_at:
                self.stop()
            return
        if status != 200 or not self._same_binding(data):
            self.stop()
            return
        state = data.get("state")
        if state == "active":
            lease = data.get("lease_for_s")
            if type(lease) is not int or not 1 <= lease <= 90:
                self.stop()
                return
            try:
                self.gate.renew(data["provider"], data["epoch"], data["config_version"], started + lease)
            except ControlWriteBlocked:
                self.stop()
                return
            self._poll_delay = min(15.0, lease / 3.0)
        elif state == "closing" and _uuid(data.get("transition_id")):
            try:
                self.gate.close(data["epoch"], data["config_version"], data["transition_id"])
            except ValueError:
                self.stop()
                return
            self._closing = True
            self._poll_delay = 0.25
            self._publish_drained()
        else:
            self.stop()

    def _run(self):
        """Poll off component event loops, sealing on any unexpected worker failure."""
        try:
            while not self._stop.wait(self._poll_delay):
                self.poll_once()
        except Exception:
            self.gate.seal()
            self._stop.set()
            self.base.log("Error: Controller service worker stopped; battery writes fenced")


def start_control_handoff(base):
    """Attach an explicitly enabled engine before component startup or fail closed."""
    existing = control_gate_for(base)
    if existing is not None:
        existing.seal()
        service = base.__dict__.get("control_handoff_service")
        if service is not None:
            service.stop()
        raise ControlAttachFailed("Controller startup cannot replace an attached gate")
    config = base.args.get("control_handoff")
    if config is None or isinstance(config, dict) and config.get("enabled", False) is False:
        return None
    boot_id = str(uuid.uuid4())
    blocked = ControlWriteGate("unattached", boot_id, 1, time.monotonic() + 1)
    blocked.seal()
    base.control_write_gate = blocked
    if not isinstance(config, dict) or config.get("enabled") is not True or set(config) != {"enabled"}:
        raise ControlAttachFailed("Controller service configuration invalid")
    instance_id = base.args.get("user_id")
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    parsed = urlsplit(url)
    if not _uuid(instance_id) or not key or parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ControlAttachFailed("Controller service credentials unavailable")
    service = ControlHandoffService(base, instance_id, boot_id, url, key)
    gate = service.attach()
    args = base.args
    supported = (
        gate.provider == "givenergy"
        and args.get("ge_cloud_direct") is True
        and bool(args.get("ge_cloud_key"))
        or gate.provider == "predbat_gateway"
        and all(args.get(key) for key in ("gateway_device_id", "gateway_mqtt_host", "gateway_mqtt_token"))
        and args.get("gateway_display_only", False) is False
    )
    if not supported or args.get("auto_restart"):
        service.stop()
        raise ControlAttachFailed("Controller provider configuration unsupported")
    base.control_write_gate = gate
    base.control_handoff_service = service
    try:
        service.start()
    except Exception:
        service.stop()
        raise ControlAttachFailed("Controller service worker unavailable") from None
    return service


def validate_control_component(base, component_info, args):
    """Refuse uncovered battery transports and external HA before construction.

    Standalone HA loopback still routes chargers and display/status traffic. A
    real HA credential would also enable battery service paths outside this gate.
    """
    gate = control_gate_for(base)
    if gate is None:
        return
    unsupported = component_info.get("inverter", False) and component_info["class"] not in ("gecloud.GECloudDirect", "gateway.GatewayMQTT")
    external_ha = component_info["class"] == "ha.HAInterface" and args.get("ha_key")
    if unsupported or external_ha:
        gate.seal()
        raise ControlAttachFailed("Controller component transport unsupported")

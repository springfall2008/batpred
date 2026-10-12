"""Optional process-local write fencing, not a backend handoff acknowledgement.

Only an explicitly attached base.control_write_gate enables this foundation.
No configuration flag, receipt publisher or controller release is provided yet.
"""

import asyncio
import functools
import inspect
import math
import threading
import time


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
            if self._closed or provider != self.provider or self._clock() >= self.expires_at:
                raise ControlWriteBlocked("Controller epoch cannot admit a mutation")
            self._next_token += 1
            self._pending.add(self._next_token)
            return self._next_token

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

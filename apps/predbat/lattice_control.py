"""Safe scalar Lattice dispatch with durable command-result reconciliation."""

import asyncio
import base64
import contextlib
import inspect
import json
import uuid
from dataclasses import dataclass

import lattice_control_v0_3_pb2 as lattice_pb


CAPABILITY_CHARGE_POWER_LIMIT = "battery.charge_power_limit"
RESULT_APPLIED = "APPLIED"
RESULT_NOT_APPLIED = "NOT_APPLIED"
RESULT_UNKNOWN = "UNKNOWN"
_STORAGE_MODULE = "lattice_control"
_STORAGE_KEY = "charge_power_limit"


@dataclass(frozen=True)
class ScalarDispatchResult:
    """Outcome known by BatPred after dispatch and any reconciliation."""

    state: str
    command_id: str = ""
    applied_value: int = None
    verified: bool = False
    fallback_used: bool = False
    detail: str = ""


class LatticeScalarDispatcher:
    """Serialize one topology-bound scalar write and reconcile its durable ACK."""

    def __init__(self, publish, storage, log, control_topic, result_get_prefix, ack_timeout=3.0, reconcile_timeout=3.0, command_id_factory=None):
        """Create a dispatcher around injected MQTT, storage, and logging seams."""
        self._publish = publish
        self._storage = storage
        self._log = log
        self._control_topic = control_topic
        self._result_get_prefix = result_get_prefix
        self._ack_timeout = ack_timeout
        self._reconcile_timeout = reconcile_timeout
        self._command_id_factory = command_id_factory or (lambda: str(uuid.uuid4()))
        self._lock = asyncio.Lock()
        self._waiting_command_id = None
        self._ack_future = None
        self._accepted_payload = None

    def _telemetry(self, event, **fields):
        """Emit one structured, searchable control event."""
        payload = {"event": event, "capability": CAPABILITY_CHARGE_POWER_LIMIT}
        payload.update(fields)
        self._log("Info: LatticeControl {}".format(json.dumps(payload, sort_keys=True, separators=(",", ":"))))

    async def _save(self, record):
        """Persist dispatcher state before any action that must not repeat."""
        if not self._storage:
            return False
        return bool(await self._storage.save(_STORAGE_MODULE, _STORAGE_KEY, record, format="json", expiry=None))

    async def _load(self):
        """Load the last durable dispatcher state."""
        if not self._storage:
            return None
        record = await self._storage.load(_STORAGE_MODULE, _STORAGE_KEY)
        return record if isinstance(record, dict) else None

    @staticmethod
    def _valid_record(record):
        """Reject corrupt durable state before it can trigger either write path."""
        if not isinstance(record.get("command_id"), str) or not record["command_id"]:
            return False
        if record.get("state") not in ("PENDING", RESULT_APPLIED, RESULT_NOT_APPLIED, RESULT_UNKNOWN):
            return False
        value = record.get("value")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
        if record.get("fallback_state") not in (None, "started", "complete"):
            return False
        if record["state"] not in ("PENDING", RESULT_UNKNOWN):
            return True
        doc_version = record.get("doc_version")
        cap_ref = record.get("cap_ref")
        return (
            record.get("provider") == "predbat-gateway"
            and isinstance(record.get("node_id"), str)
            and bool(record["node_id"])
            and isinstance(doc_version, int)
            and not isinstance(doc_version, bool)
            and doc_version > 0
            and isinstance(cap_ref, int)
            and not isinstance(cap_ref, bool)
            and cap_ref > 0
        )

    @staticmethod
    def _ack_result(ack):
        """Validate an ACK and return its terminal state and applied value."""
        if ack.result == lattice_pb.CONTROL_RESULT_APPLIED:
            if not ack.ok or ack.error or not ack.HasField("applied_scalar") or ack.applied_scalar.WhichOneof("value") != "i":
                return None
            return RESULT_APPLIED, int(ack.applied_scalar.i)
        if ack.result == lattice_pb.CONTROL_RESULT_NOT_APPLIED:
            if ack.ok or not ack.error or ack.HasField("applied_scalar") or ack.verified:
                return None
            return RESULT_NOT_APPLIED, None
        if ack.result == lattice_pb.CONTROL_RESULT_UNKNOWN:
            if ack.ok or not ack.error or ack.HasField("applied_scalar") or ack.verified:
                return None
            return RESULT_UNKNOWN, None
        return None

    def accept_ack(self, topic_command_id, payload):
        """Correlate a protobuf ACK from ``ack/<id>`` with the active command."""
        try:
            ack = lattice_pb.ControlAck.FromString(payload)
        except Exception:
            self._telemetry("result_rejected", reason="decode")
            return False
        if not topic_command_id or ack.command_id != topic_command_id:
            self._telemetry("result_rejected", reason="topic_command_mismatch", topic_command_id=topic_command_id, command_id=ack.command_id)
            return False
        parsed = self._ack_result(ack)
        if parsed is None:
            self._telemetry("result_rejected", reason="invalid_terminal_ack", command_id=ack.command_id)
            return False

        if ack.command_id != self._waiting_command_id or not self._ack_future:
            self._telemetry("result_rejected", reason="not_active", command_id=ack.command_id)
            return False
        if self._accepted_payload is not None and self._accepted_payload != payload:
            self._telemetry("result_rejected", reason="conflicting_duplicate", command_id=ack.command_id)
            return False
        self._accepted_payload = bytes(payload)
        if self._ack_future.done():
            self._telemetry("result_duplicate", command_id=ack.command_id, state=parsed[0])
            return True
        self._ack_future.set_result((ack, parsed))
        return True

    async def _wait_for_ack(self, command_id, timeout):
        """Wait only for a validated, correlated ACK."""
        self._waiting_command_id = command_id
        self._accepted_payload = None
        self._ack_future = asyncio.get_running_loop().create_future()
        try:
            return await asyncio.wait_for(self._ack_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiting_command_id = None
            self._ack_future = None
            self._accepted_payload = None

    async def _publish_and_wait(self, topic, payload, command_id, timeout):
        """Arm ACK correlation before publishing so a fast ACK cannot be lost."""
        waiter = asyncio.create_task(self._wait_for_ack(command_id, timeout))
        await asyncio.sleep(0)
        try:
            await self._publish(topic, payload)
        except Exception:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
            raise
        return await waiter

    async def _reconcile(self, record):
        """Query the gateway ledger by command ID without repeating the control."""
        command_id = record["command_id"]
        request = lattice_pb.ControlResultRequest(command_id=command_id)
        self._telemetry("result_query", command_id=command_id)
        try:
            return await self._publish_and_wait(
                "{}{}".format(self._result_get_prefix, command_id),
                request.SerializeToString(),
                command_id,
                self._reconcile_timeout,
            )
        except Exception as exc:
            self._telemetry("result_query_failed", command_id=command_id, error=str(exc))
            return None

    async def _legacy_once(self, record, legacy):
        """Invoke legacy only after durably marking this command's fallback consumed."""
        if record.get("fallback_state") in ("started", "complete"):
            return ScalarDispatchResult(RESULT_NOT_APPLIED, record["command_id"], fallback_used=True, detail="legacy fallback already consumed")
        record["fallback_state"] = "started"
        if not await self._save(record):
            return ScalarDispatchResult(RESULT_UNKNOWN, record["command_id"], detail="could not durably reserve legacy fallback")
        self._telemetry("fallback", command_id=record["command_id"], value=record["value"])
        result = legacy()
        if inspect.isawaitable(result):
            await result
        record["fallback_state"] = "complete"
        if not await self._save(record):
            return ScalarDispatchResult(RESULT_UNKNOWN, record["command_id"], detail="legacy fallback completion persistence failed")
        return ScalarDispatchResult(RESULT_NOT_APPLIED, record["command_id"], fallback_used=True)

    async def _finish(self, record, ack_result, legacy):
        """Persist a terminal ACK, reconcile UNKNOWN, or perform safe fallback."""
        ack, (state, applied_value) = ack_result
        record.update(
            {
                "state": state,
                "ack": base64.b64encode(ack.SerializeToString()).decode("ascii"),
                "verified": bool(ack.verified),
                "applied_value": applied_value,
            }
        )
        if not await self._save(record):
            return ScalarDispatchResult(RESULT_UNKNOWN, record["command_id"], detail="terminal result persistence failed")
        self._telemetry("result", command_id=record["command_id"], state=state, applied_value=applied_value, verified=bool(ack.verified), error=ack.error)
        if state == RESULT_APPLIED:
            return ScalarDispatchResult(state, record["command_id"], applied_value, bool(ack.verified))
        if state == RESULT_NOT_APPLIED:
            return await self._legacy_once(record, legacy)

        replay = await self._reconcile(record)
        if replay is None:
            return ScalarDispatchResult(RESULT_UNKNOWN, record["command_id"], detail="durable result unavailable")
        replay_state = replay[1][0]
        if replay_state == RESULT_UNKNOWN:
            return ScalarDispatchResult(RESULT_UNKNOWN, record["command_id"], detail="gateway result remains unknown")
        return await self._finish(record, replay, legacy)

    async def dispatch_charge_power(self, source, value, legacy):
        """Dispatch one charge-power intent, never racing it with legacy control."""
        async with self._lock:
            existing = await self._load()
            if existing and not self._valid_record(existing):
                self._telemetry("state_rejected", reason="corrupt_persisted_record")
                return ScalarDispatchResult(RESULT_UNKNOWN, detail="corrupt persisted dispatcher state")
            if existing and existing.get("state") == RESULT_NOT_APPLIED and existing.get("fallback_state") in (None, "started"):
                self._telemetry("state_blocked", command_id=existing["command_id"], reason="legacy_fallback_may_be_in_flight")
                return ScalarDispatchResult(RESULT_UNKNOWN, existing["command_id"], detail="legacy fallback completion is ambiguous")
            if existing and existing.get("state") in ("PENDING", RESULT_UNKNOWN):
                self._telemetry("selected_path", command_id=existing.get("command_id"), provider=existing.get("provider"), node_id=existing.get("node_id"), doc_version=existing.get("doc_version"), cap_ref=existing.get("cap_ref"), resumed=True)
                replay = await self._reconcile(existing)
                if replay is None:
                    return ScalarDispatchResult(RESULT_UNKNOWN, existing["command_id"], detail="unresolved prior command")
                return await self._finish(existing, replay, legacy)

            if source is None:
                record = {"command_id": self._command_id_factory(), "value": int(value), "state": RESULT_NOT_APPLIED, "fallback_state": None}
                return await self._legacy_once(record, legacy)

            source_valid = (
                source.provider == "predbat-gateway"
                and str(source.topology_version).split(".")[:2] in (["0", "2"], ["0", "3"])
                and source.access_path == "gw-local"
                and source.capability == CAPABILITY_CHARGE_POWER_LIMIT
                and isinstance(source.doc_version, int)
                and not isinstance(source.doc_version, bool)
                and source.doc_version > 0
                and isinstance(source.cap_ref, int)
                and not isinstance(source.cap_ref, bool)
                and source.cap_ref > 0
                and bool(source.node_id)
            )
            if not source_valid:
                record = {"command_id": self._command_id_factory(), "value": int(value), "state": RESULT_NOT_APPLIED, "fallback_state": None}
                self._telemetry("selected_path_rejected", reason="unsafe_source_provenance")
                return await self._legacy_once(record, legacy)

            command_id = self._command_id_factory()
            control = lattice_pb.Control(command_id=command_id, doc_version=source.doc_version, cap_ref=source.cap_ref)
            control.scalar.i = int(value)
            record = {
                "command_id": command_id,
                "state": "PENDING",
                "value": int(value),
                "provider": source.provider,
                "node_id": source.node_id,
                "access_path": source.access_path,
                "doc_version": source.doc_version,
                "cap_ref": source.cap_ref,
                "control": base64.b64encode(control.SerializeToString()).decode("ascii"),
                "fallback_state": None,
            }
            self._telemetry("selected_path", command_id=command_id, provider=source.provider, node_id=source.node_id, access_path=source.access_path, doc_version=source.doc_version, cap_ref=source.cap_ref, value=int(value))
            if not await self._save(record):
                record["state"] = RESULT_NOT_APPLIED
                return await self._legacy_once(record, legacy)

            self._telemetry("command", command_id=command_id, doc_version=source.doc_version, cap_ref=source.cap_ref, value=int(value))
            try:
                ack_result = await self._publish_and_wait(self._control_topic, control.SerializeToString(), command_id, self._ack_timeout)
            except Exception as exc:
                self._telemetry("command_publish_failed", command_id=command_id, error=str(exc))
                ack_result = None
            if ack_result is None:
                record["state"] = RESULT_UNKNOWN
                await self._save(record)
                replay = await self._reconcile(record)
                if replay is None:
                    return ScalarDispatchResult(RESULT_UNKNOWN, command_id, detail="ACK and durable result unavailable")
                ack_result = replay
            return await self._finish(record, ack_result, legacy)

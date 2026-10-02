# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test the Sungrow external EMS heartbeat
# -----------------------------------------------------------------------------

"""Tests for the Sungrow external EMS heartbeat (parameter 10017).

The heartbeat is a dead-man's switch: taking control means keeping it alive, and letting it
lapse returns the inverter to self-consumption. That revert is the SAFETY PROPERTY - if
Predbat dies, a customer's battery goes back to self-consumption rather than sitting stopped
or holding a stale forced setpoint. These tests exist to stop anyone accidentally defeating it.
"""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import asyncio
from unittest.mock import patch
from sungrow_const import SUNGROW_PARAM_HEARTBEAT, SUNGROW_CMD_CHARGE, heartbeat_send_interval
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session
from tests.test_sungrow_api import MockSungrow, _envelope, _mock_post, PLANT_LIST, DEVICE_LIST, PLANT_REALTIME, DEVICE_REALTIME

UUID = "987654"

DISPATCH_OK = {"check_result": "1", "dev_result_list": [{"code": "1", "task_id": "task-1"}]}
TASK_DONE = {"command_status": 8, "param_list": [{"param_code": "10017", "set_value": "300", "point_name": "External EMS heartbeat"}]}
CHECK_OK = {"check_result": "1", "dev_result_list": [{"check_result": "1"}]}


def _no_sleep():
    """Return an async no-op to stand in for asyncio.sleep, so loops do not burn wall clock."""

    async def sleeper(_seconds):
        """Return immediately instead of sleeping."""
        return None

    return sleeper


def _controlled_client(**kwargs):
    """Return a client that has discovered one inverter and polled telemetry once."""
    client = MockSungrow(**kwargs)
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    with _mock_post([_envelope(PLANT_REALTIME), _envelope(DEVICE_REALTIME)]):
        run_async_local(client.refresh_power())
    return client


def test_send_heartbeat_writes_parameter_10017():
    """The beat writes 10017 and nothing else - it is not a change of plan."""
    failed = False
    client = _controlled_client(heartbeat_interval=180)
    captured = {}
    session = create_aiohttp_mock_session([create_aiohttp_mock_response(json_data=_envelope(DISPATCH_OK)), create_aiohttp_mock_response(json_data=_envelope(TASK_DONE))])
    original_post = session.post

    def capture_post(url, headers=None, json=None):
        """Record the outgoing body so the beat's parameters can be asserted."""
        captured.setdefault("bodies", []).append(json)
        return original_post(url, headers=headers, json=json)

    session.post = capture_post
    with patch("asyncio.sleep", new=_no_sleep()):
        with patch("aiohttp.ClientSession", return_value=session):
            ok = run_async_local(client.send_heartbeat(UUID))
    if not ok:
        print("ERROR: the heartbeat was not confirmed")
        failed = True
    params = captured["bodies"][0]["param_list"]
    if len(params) != 1 or params[0]["param_code"] != SUNGROW_PARAM_HEARTBEAT:
        print(f"ERROR: the beat wrote {params}, expected only 10017")
        failed = True
    if params[0]["set_value"] != "180":
        print(f"ERROR: the beat declared {params[0]['set_value']}s, expected the configured 180")
        failed = True
    assert not failed, "test_send_heartbeat_writes_parameter_10017"


def test_send_heartbeat_reports_a_failed_beat_without_escalating():
    """A failed beat is logged and retried; there is nothing to escalate to.

    The consequence of the beats stopping altogether is a revert to self-consumption, which is
    the safe state - so a failed beat must not, for example, take the whole component down.
    """
    failed = False
    client = _controlled_client()
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(None, code="E00000", message="device offline")]):
            ok = run_async_local(client.send_heartbeat(UUID))
    if ok:
        print("ERROR: a failed beat reported success")
        failed = True
    if not any("revert to self-consumption" in message for message in client.log_messages):
        print("ERROR: the failed-beat log does not say what happens next")
        failed = True
    assert not failed, "test_send_heartbeat_reports_a_failed_beat_without_escalating"


def test_heartbeat_bypasses_the_write_pacing_gate():
    """A beat is not a change of plan, so holding it back would drop control of the battery.

    Every beat is byte-identical to the last one by design, so routing it through _write_params
    would also have the change-detection cache swallow it entirely.
    """
    failed = False
    # A pacing interval far longer than the test, and a write already stamped, so any
    # pacing-gated call would be refused.
    client = _controlled_client(min_write_interval=3600)
    client.last_write_time[UUID] = 9e18  # far in the future: pacing would never allow a write
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            ok = run_async_local(client.send_heartbeat(UUID))
    if not ok:
        print("ERROR: the heartbeat was blocked by the write pacing gate")
        failed = True
    assert not failed, "test_heartbeat_bypasses_the_write_pacing_gate"


def test_taking_control_starts_the_heartbeat_loop():
    """A confirmed control write marks the device held and starts exactly one loop."""
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    client.local_schedule[UUID] = {"reserve": 0, "charge": {}, "export": {}}
    started = {"count": 0}
    real_ensure = client._ensure_heartbeat

    def counting_ensure():
        """Count the calls, then defer to the real implementation."""
        started["count"] += 1
        return real_ensure()

    client._ensure_heartbeat = counting_ensure
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            ok = run_async_local(client._write_params(UUID, {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"}))
    if not ok:
        print("ERROR: the control write did not succeed")
        failed = True
    if UUID not in client.control_held:
        print("ERROR: the device was not marked as held after a confirmed write")
        failed = True
    if started["count"] != 1:
        print(f"ERROR: the heartbeat was ensured {started['count']} times")
        failed = True
    run_async_local(client._stop_heartbeat())
    assert not failed, "test_taking_control_starts_the_heartbeat_loop"


def test_ensure_heartbeat_is_idempotent():
    """Called from every path that takes control, so it must never start a second loop."""
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)

    async def drive():
        """Ensure the loop twice inside a running event loop and report how many tasks exist."""
        client._ensure_heartbeat()
        first = client._heartbeat_task
        client._ensure_heartbeat()
        second = client._heartbeat_task
        await client._stop_heartbeat()
        return first, second

    first, second = run_async_local(drive())
    if first is None:
        print("ERROR: no heartbeat task was started")
        failed = True
    if first is not second:
        print("ERROR: a second heartbeat loop was started")
        failed = True
    assert not failed, "test_ensure_heartbeat_is_idempotent"


def test_ensure_heartbeat_does_nothing_when_nothing_is_held():
    """No held device means no beats - the inverter is already running self-consumption."""
    failed = False
    client = _controlled_client()
    client._ensure_heartbeat()
    if client._heartbeat_task is not None:
        print("ERROR: a heartbeat loop was started with nothing held")
        failed = True
    assert not failed, "test_ensure_heartbeat_does_nothing_when_nothing_is_held"


def test_heartbeat_loop_beats_for_every_held_device_then_exits():
    """The loop beats once per held device and exits when nothing is held any more."""
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    beats = []

    async def record_beat(uuid):
        """Record the beat, then release the device so the loop's exit condition is exercised."""
        beats.append(uuid)
        client.control_held.discard(uuid)
        return True

    client.send_heartbeat = record_beat
    with patch("asyncio.sleep", new=_no_sleep()):
        run_async_local(client._heartbeat_loop())
    if beats != [UUID]:
        print(f"ERROR: beats {beats}")
        failed = True
    if not any("heartbeat loop stopped" in message for message in client.log_messages):
        print("ERROR: the loop did not log that it stopped")
        failed = True
    assert not failed, "test_heartbeat_loop_beats_for_every_held_device_then_exits"


def test_heartbeat_loop_stops_in_read_only_mode():
    """Read-only must stop the BEATS, not just the plan writes.

    Continuing to beat would hold the inverter in external dispatch with whatever setpoint it
    last had - precisely the "Predbat is not driving but the battery is still forced" state
    read-only exists to avoid. Letting the beats lapse returns it to self-consumption.
    """
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    client.state["switch.predbat_set_read_only"] = "on"
    beats = []

    async def record_beat(uuid):
        """Record any beat that escapes the read-only gate."""
        beats.append(uuid)
        # Stop the loop after the first escaped beat. Without this a regression that removes
        # the gate would hang the suite forever instead of failing it, which is much harder to
        # diagnose than a plain assertion.
        client._heartbeat_stop = True
        return True

    client.send_heartbeat = record_beat
    with patch("asyncio.sleep", new=_no_sleep()):
        run_async_local(client._heartbeat_loop())
    if beats:
        print(f"ERROR: {len(beats)} beats went out in read-only mode")
        failed = True
    if not any("heartbeat paused" in message for message in client.log_messages):
        print("ERROR: the pause was not logged")
        failed = True
    assert not failed, "test_heartbeat_loop_stops_in_read_only_mode"


def test_heartbeat_loop_stops_when_control_is_disabled():
    """sungrow_control_enable false means the component holds nothing open."""
    failed = False
    client = _controlled_client(control_enable=False)
    client.control_held.add(UUID)
    beats = []

    async def record_beat(uuid):
        """Record any beat that escapes the control-disabled gate."""
        beats.append(uuid)
        # Stop the loop after the first escaped beat. Without this a regression that removes
        # the gate would hang the suite forever instead of failing it, which is much harder to
        # diagnose than a plain assertion.
        client._heartbeat_stop = True
        return True

    client.send_heartbeat = record_beat
    with patch("asyncio.sleep", new=_no_sleep()):
        run_async_local(client._heartbeat_loop())
    if beats:
        print("ERROR: beats went out with control disabled")
        failed = True
    assert not failed, "test_heartbeat_loop_stops_when_control_is_disabled"


def test_heartbeat_loop_stops_when_the_component_stops():
    """api_stop ends the loop, so a shutting-down component does not keep holding the battery."""
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    client.api_stop = True
    beats = []

    async def record_beat(uuid):
        """Record any beat that escapes the api_stop gate."""
        beats.append(uuid)
        # Stop the loop after the first escaped beat. Without this a regression that removes
        # the gate would hang the suite forever instead of failing it, which is much harder to
        # diagnose than a plain assertion.
        client._heartbeat_stop = True
        return True

    client.send_heartbeat = record_beat
    with patch("asyncio.sleep", new=_no_sleep()):
        run_async_local(client._heartbeat_loop())
    if beats:
        print("ERROR: beats went out after api_stop")
        failed = True
    assert not failed, "test_heartbeat_loop_stops_when_the_component_stops"


def test_heartbeat_beats_well_inside_the_declared_window():
    """The loop's cadence is a fraction of what was promised to 10017, not equal to it.

    Beating exactly at the promised interval is a coin flip: one lost request, one slow dispatch
    or one skipped tick and the inverter has already reverted.
    """
    failed = False
    client = _controlled_client(heartbeat_interval=300)
    cadence = heartbeat_send_interval(client.heartbeat_interval)
    if cadence >= client.heartbeat_interval:
        print(f"ERROR: cadence {cadence} is not inside the declared {client.heartbeat_interval}s window")
        failed = True
    assert not failed, "test_heartbeat_beats_well_inside_the_declared_window"


def test_final_stops_the_heartbeat_before_releasing():
    """Stopping first stops a beat racing the release and putting the inverter straight back.

    Releasing explicitly is a courtesy, not a requirement: if final() never runs - a container
    kill, a power cut - the beats simply stop and the inverter reverts by itself within one
    interval. That is the whole point of the dead-man's switch.
    """
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    order = []

    async def record_stop():
        """Record that the heartbeat was stopped."""
        order.append("stop")

    async def record_release(uuid):
        """Record that the release ran, and for which device."""
        order.append("release")
        client.control_held.discard(uuid)
        return True

    client._stop_heartbeat = record_stop
    client.release_control = record_release
    run_async_local(client.final())
    if order[:2] != ["stop", "release"]:
        print(f"ERROR: shutdown order {order}, expected the heartbeat to stop first")
        failed = True
    assert not failed, "test_final_stops_the_heartbeat_before_releasing"


def test_control_held_is_not_persisted_across_a_restart():
    """After a restart the inverter has already reverted, so restoring 'we hold control' would lie.

    The component would start beating for a mode the inverter is not in, and would never re-send
    the command that puts it there.
    """
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    saved = {}

    async def capture_save(name, data):
        """Capture what save_control would have written."""
        saved[name] = data

    client.save_cache = capture_save
    run_async_local(client.save_control())
    blob = saved.get("control", {})
    if "control_held" in blob:
        print("ERROR: control_held was persisted, which would make a restart beat for a mode the inverter is not in")
        failed = True
    # The pacing clock IS persisted, or a restart loop escapes the write budget entirely.
    if "last_write_time" not in blob:
        print("ERROR: the write pacing clock was not persisted")
        failed = True
    assert not failed, "test_control_held_is_not_persisted_across_a_restart"


def test_heartbeat_loop_survives_being_cancelled():
    """_stop_heartbeat cancels the task and waits for it, so shutdown cannot leave one running."""
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)

    async def drive():
        """Start the loop, then stop it, and report whether the task finished."""
        client._ensure_heartbeat()
        task = client._heartbeat_task
        await asyncio.sleep(0)
        await client._stop_heartbeat()
        return task

    task = run_async_local(drive())
    if task is None:
        print("ERROR: no heartbeat task was started")
        failed = True
    elif not task.done():
        print("ERROR: the heartbeat task was still running after _stop_heartbeat")
        failed = True
    if client._heartbeat_task is not None:
        print("ERROR: the heartbeat task handle was not cleared")
        failed = True
    assert not failed, "test_heartbeat_loop_survives_being_cancelled"


def run_sungrow_heartbeat_tests(my_predbat):
    """Run all Sungrow EMS heartbeat tests."""
    failed = False
    for name, fn in [
        ("send_writes_10017", test_send_heartbeat_writes_parameter_10017),
        ("failed_beat_not_escalated", test_send_heartbeat_reports_a_failed_beat_without_escalating),
        ("bypasses_pacing", test_heartbeat_bypasses_the_write_pacing_gate),
        ("taking_control_starts_loop", test_taking_control_starts_the_heartbeat_loop),
        ("ensure_idempotent", test_ensure_heartbeat_is_idempotent),
        ("ensure_noop_when_nothing_held", test_ensure_heartbeat_does_nothing_when_nothing_is_held),
        ("loop_beats_then_exits", test_heartbeat_loop_beats_for_every_held_device_then_exits),
        ("loop_stops_read_only", test_heartbeat_loop_stops_in_read_only_mode),
        ("loop_stops_control_disabled", test_heartbeat_loop_stops_when_control_is_disabled),
        ("loop_stops_on_api_stop", test_heartbeat_loop_stops_when_the_component_stops),
        ("cadence_inside_window", test_heartbeat_beats_well_inside_the_declared_window),
        ("final_stops_first", test_final_stops_the_heartbeat_before_releasing),
        ("held_not_persisted", test_control_held_is_not_persisted_across_a_restart),
        ("loop_cancellable", test_heartbeat_loop_survives_being_cancelled),
    ]:
        try:
            if fn():
                print(f"  FAILED: sungrow_heartbeat.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sungrow_heartbeat.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

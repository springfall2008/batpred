# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Hanchu ESS cloud control logic
# -----------------------------------------------------------------------------

"""Tests for the Hanchu component's control path: clamping, slot building and batched writes."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import asyncio
import time
from hanchu_const import (
    HANCHU_KEY_CHARGE_END,
    HANCHU_KEY_CHARGE_POWER,
    HANCHU_KEY_CHARGE_SOC,
    HANCHU_KEY_CHARGE_START,
    HANCHU_KEY_DISCHARGE_END,
    HANCHU_KEY_DISCHARGE_POWER,
    HANCHU_KEY_DISCHARGE_SOC,
    HANCHU_KEY_DISCHARGE_START,
    HANCHU_KEY_WORK_MODE,
    HANCHU_SECONDS_PER_DAY,
    HANCHU_SLOT_DISABLED,
    HANCHU_WORK_MODE_FOR_SCHEDULE,
)
from tests.test_hanchu_api import MockHanchu, envelope, logged, patched_session
from tests.test_infra import run_async as run_async_local

SN = "HC240100001"


def ready(client=None, **kwargs):
    """Return a MockHanchu already past discovery, with a live token, ready to write."""
    client = client or MockHanchu(**kwargs)
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = [SN]
    return client


def schedule(charge=None, export=None, reserve=10):
    """Build a schedule dict in the shape get_schedule_settings_ha() produces."""
    base = {
        "reserve": reserve,
        "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
    }
    if charge:
        base["charge"].update(charge)
    if export:
        base["export"].update(export)
    return base


def test_hanchu_charge_window_fills_slot_one():
    """A plain charge window lands in slot 1 with the target, the rate and the work mode."""
    failed = False
    client = ready()
    payload = client.build_write_payload(SN, schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:30:00", "end": "04:30:00"}))
    expected = {
        HANCHU_KEY_CHARGE_START.format(1): 5400,
        HANCHU_KEY_CHARGE_END.format(1): 16200,
        HANCHU_KEY_CHARGE_SOC: 90,
        HANCHU_KEY_CHARGE_POWER: 3000,
        HANCHU_KEY_WORK_MODE: HANCHU_WORK_MODE_FOR_SCHEDULE,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            print(f"ERROR: payload[{key}] = {payload.get(key)} != {value}")
            failed = True
    # The discharge floor carries the reserve, because no export window is running.
    if payload.get(HANCHU_KEY_DISCHARGE_SOC) != 10:
        print(f"ERROR: discharge floor {payload.get(HANCHU_KEY_DISCHARGE_SOC)} should be the reserve, 10")
        failed = True
    # Slots 2 and 3 are written as disabled: User-defined mode runs all three, so a slot left over
    # from the Hanchu app must not keep running against the plan.
    for index in (2, 3):
        for key in (HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END, HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END):
            if payload.get(key.format(index)) != HANCHU_SLOT_DISABLED:
                print(f"ERROR: {key.format(index)} = {payload.get(key.format(index))}, expected disabled")
                failed = True
    assert not failed, "test_hanchu_charge_window_fills_slot_one"


def test_hanchu_midnight_spanning_window_uses_slot_two():
    """Predbat spells 'until 02:00 tomorrow' as an end of 26:00; slot 2 carries the remainder."""
    failed = False
    client = ready()
    payload = client.build_write_payload(SN, schedule(charge={"enable": True, "soc": 80, "power": 2000, "start": "23:00:00", "end": "26:00:00"}))
    if payload.get(HANCHU_KEY_CHARGE_START.format(1)) != 82800 or payload.get(HANCHU_KEY_CHARGE_END.format(1)) != HANCHU_SECONDS_PER_DAY - 60:
        print(f"ERROR: slot 1 = {payload.get(HANCHU_KEY_CHARGE_START.format(1))}-{payload.get(HANCHU_KEY_CHARGE_END.format(1))}")
        failed = True
    if payload.get(HANCHU_KEY_CHARGE_START.format(2)) != HANCHU_SLOT_DISABLED or payload.get(HANCHU_KEY_CHARGE_END.format(2)) != 7200:
        print(f"ERROR: slot 2 = {payload.get(HANCHU_KEY_CHARGE_START.format(2))}-{payload.get(HANCHU_KEY_CHARGE_END.format(2))}")
        failed = True
    assert not failed, "test_hanchu_midnight_spanning_window_uses_slot_two"


def test_hanchu_soc_limits_are_clamped_and_explained_once():
    """The narrow SoC ranges are a real limitation, so the clamp is logged - but only once."""
    failed = False
    client = ready()
    # 30% charge target is below this hardware's floor of 50, and a 70% export target is above its
    # ceiling of 45. Both are perfectly ordinary Predbat plans that this inverter cannot express.
    client.set_mock_clock(13 * 60)
    plan = schedule(charge={"enable": True, "soc": 30, "power": 2000, "start": "01:00:00", "end": "04:00:00"}, export={"enable": True, "soc": 70, "power": 3000, "start": "12:00:00", "end": "16:00:00"})
    payload = client.build_write_payload(SN, plan)
    if payload.get(HANCHU_KEY_CHARGE_SOC) != 50:
        print(f"ERROR: charge target {payload.get(HANCHU_KEY_CHARGE_SOC)} should be clamped up to 50")
        failed = True
    if payload.get(HANCHU_KEY_DISCHARGE_SOC) != 45:
        print(f"ERROR: export target {payload.get(HANCHU_KEY_DISCHARGE_SOC)} should be clamped down to 45")
        failed = True
    if not logged(client, "only accepts 50-100"):
        print("ERROR: the charge target clamp was not explained")
        failed = True
    if not logged(client, "only accepts 5-45"):
        print("ERROR: the export target clamp was not explained")
        failed = True
    # Rebuilt on the next cycle with the same out-of-range plan: the clamp still applies, but the
    # explanation must not repeat, or hours of ordinary planning would bury every other log line.
    before = len(client.log_messages)
    payload = client.build_write_payload(SN, plan)
    new_clamp_lines = [message for message in client.log_messages[before:] if "only accepts" in message]
    if new_clamp_lines:
        print(f"ERROR: the clamp was re-explained on a later cycle: {new_clamp_lines}")
        failed = True
    if payload.get(HANCHU_KEY_CHARGE_SOC) != 50:
        print("ERROR: the clamp itself must keep applying even once it has been explained")
        failed = True
    assert not failed, "test_hanchu_soc_limits_are_clamped_and_explained_once"


def test_hanchu_freeze_sends_a_zero_rate_unclamped():
    """Zero is how Predbat expresses freeze, and it must survive the clamp as zero."""
    failed = False
    client = ready()
    payload = client.build_write_payload(SN, schedule(charge={"enable": True, "soc": 90, "power": 0, "start": "01:00:00", "end": "04:00:00"}))
    if payload.get(HANCHU_KEY_CHARGE_POWER) != 0:
        print(f"ERROR: freeze rate became {payload.get(HANCHU_KEY_CHARGE_POWER)}, not 0")
        failed = True
    if logged(client, "only accepts"):
        print("ERROR: a zero rate must not be reported as clamped")
        failed = True
    assert not failed, "test_hanchu_freeze_sends_a_zero_rate_unclamped"


def test_hanchu_discharge_floor_tracks_the_active_export_window():
    """The floor is device-wide, so it carries the export target only while that window is running."""
    failed = False
    client = ready()
    plan = schedule(export={"enable": True, "soc": 20, "power": 3000, "start": "12:00:00", "end": "16:00:00"}, reserve=8)

    # Inside the window: the floor is the export target.
    client.set_mock_clock(13 * 60)
    payload = client.build_write_payload(SN, plan)
    if payload.get(HANCHU_KEY_DISCHARGE_SOC) != 20:
        print(f"ERROR: inside the window the floor is {payload.get(HANCHU_KEY_DISCHARGE_SOC)}, should be the export target 20")
        failed = True

    # Outside it: the floor is the reserve, so the battery is not drained after the window ends.
    client.set_mock_clock(18 * 60)
    payload = client.build_write_payload(SN, plan)
    if payload.get(HANCHU_KEY_DISCHARGE_SOC) != 8:
        print(f"ERROR: outside the window the floor is {payload.get(HANCHU_KEY_DISCHARGE_SOC)}, should be the reserve 8")
        failed = True

    # A DISABLED export window never claims the floor, whatever the clock says.
    client.set_mock_clock(13 * 60)
    payload = client.build_write_payload(SN, schedule(export={"enable": False, "soc": 20, "start": "12:00:00", "end": "16:00:00"}, reserve=8))
    if payload.get(HANCHU_KEY_DISCHARGE_SOC) != 8:
        print(f"ERROR: a disabled window claimed the floor: {payload.get(HANCHU_KEY_DISCHARGE_SOC)}")
        failed = True
    assert not failed, "test_hanchu_discharge_floor_tracks_the_active_export_window"


def test_hanchu_every_unused_slot_is_disabled():
    """Predbat owns all six slots: a superseded window is retracted and unused slots are disabled.

    User-defined mode runs all three charge and all three discharge slots, and 00:00-00:00 is how a
    slot is switched off (confirmed on hardware, batpred#5305). A slot left over from the Hanchu app
    would otherwise keep running against the plan.
    """
    failed = False
    client = ready()
    first = client.build_write_payload(SN, schedule(charge={"enable": True, "soc": 90, "power": 2000, "start": "01:00:00", "end": "04:00:00"}))
    client.applied_payload[SN] = dict(first)

    # Predbat no longer wants a window: every slot, including the one we filled, is disabled.
    second = client.build_write_payload(SN, schedule())
    for index in (1, 2, 3):
        for key in (HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END, HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END):
            if second.get(key.format(index)) != HANCHU_SLOT_DISABLED:
                print(f"ERROR: {key.format(index)} = {second.get(key.format(index))}, expected disabled")
                failed = True
    # A cold start with no plan still disables every slot, so an app slot cannot run.
    cold = MockHanchu()
    empty = cold.build_write_payload(SN, schedule())
    slot_keys = sorted(key for key in empty if key.startswith(("TCT_", "TDT_")))
    if len(slot_keys) != 12 or any(empty[key] != HANCHU_SLOT_DISABLED for key in slot_keys):
        print(f"ERROR: a cold start should disable all twelve slot keys, got {slot_keys}")
        failed = True
    assert not failed, "test_hanchu_every_unused_slot_is_disabled"


def test_hanchu_concurrent_writes_are_serialised():
    """Two writes started together never overlap on the wire: the cloud refuses concurrent control
    calls with error 300004 (batpred#5305)."""
    failed = False
    client = ready()
    client.min_write_interval = 0
    in_flight = {"now": 0, "max": 0, "calls": 0}

    async def slow_post(endpoint_key, body=None, anonymous=False):
        in_flight["now"] += 1
        in_flight["calls"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.05)
        in_flight["now"] -= 1
        return True, {}

    client._post = slow_post
    one = client.build_write_payload(SN, schedule(charge={"enable": True, "soc": 90, "power": 2000, "start": "01:00:00", "end": "04:00:00"}))
    two = client.build_write_payload(SN, schedule(export={"enable": True, "soc": 20, "power": 2000, "start": "17:00:00", "end": "19:00:00"}))

    async def both():
        return await asyncio.gather(client._write_payload(SN, one), client._write_payload(SN, two))

    results = run_async_local(both())
    if in_flight["max"] != 1:
        print(f"ERROR: {in_flight['max']} writes were in flight at once")
        failed = True
    if in_flight["calls"] != 2 or results != [True, True]:
        print(f"ERROR: expected two sequential accepted writes, got calls={in_flight['calls']} results={results}")
        failed = True
    assert not failed, "test_hanchu_concurrent_writes_are_serialised"


def test_hanchu_work_mode_is_asserted_only_with_a_window_and_only_when_enabled():
    """The work mode is the least well evidenced write, so it is never sent speculatively."""
    failed = False
    client = ready()
    # No window at all: the mode is the user's business.
    if HANCHU_KEY_WORK_MODE in client.build_write_payload(SN, schedule()):
        print("ERROR: the work mode was written with no window enabled")
        failed = True
    # An export-only window still needs the timed mode.
    payload = client.build_write_payload(SN, schedule(export={"enable": True, "soc": 20, "power": 3000, "start": "12:00:00", "end": "16:00:00"}))
    if payload.get(HANCHU_KEY_WORK_MODE) != HANCHU_WORK_MODE_FOR_SCHEDULE:
        print(f"ERROR: an export window did not assert the work mode: {payload.get(HANCHU_KEY_WORK_MODE)}")
        failed = True
    # The escape hatch: with work_mode_control off, the windows still go out and the mode never does.
    opted_out = ready(client=MockHanchu(work_mode_control=False))
    payload = opted_out.build_write_payload(SN, schedule(charge={"enable": True, "soc": 90, "power": 2000, "start": "01:00:00", "end": "04:00:00"}))
    if HANCHU_KEY_WORK_MODE in payload:
        print("ERROR: hanchu_work_mode_control: False still wrote the work mode")
        failed = True
    if payload.get(HANCHU_KEY_CHARGE_START.format(1)) != 3600:
        print("ERROR: opting out of work mode control must not stop the window being written")
        failed = True
    assert not failed, "test_hanchu_work_mode_is_asserted_only_with_a_window_and_only_when_enabled"


def test_hanchu_write_is_one_call_carrying_every_setting():
    """The whole control state goes out in ONE iotSet - the batching the API needs."""
    failed = False
    client = ready()
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    with patched_session([(200, envelope(data={}))]) as session_cls:
        ok = run_async_local(client.apply_schedule(SN))
    if not ok:
        print("ERROR: the write reported failure")
        failed = True
    if session_cls.return_value.post.call_count != 1:
        print(f"ERROR: the write took {session_cls.return_value.post.call_count} calls, must be exactly 1")
        failed = True
    body = session_cls.return_value.post.call_args.kwargs["json"]
    if body.get("sn") != SN or body.get("devType") != "2":
        print(f"ERROR: iotSet body {body}")
        failed = True
    value = body.get("value") or {}
    for key in (HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER, HANCHU_KEY_CHARGE_SOC, HANCHU_KEY_DISCHARGE_SOC, HANCHU_KEY_CHARGE_START.format(1), HANCHU_KEY_WORK_MODE):
        if key not in value:
            print(f"ERROR: the batched value dict is missing {key}")
            failed = True
    if client.applied_payload.get(SN) != value:
        print("ERROR: an accepted payload was not cached as applied")
        failed = True
    assert not failed, "test_hanchu_write_is_one_call_carrying_every_setting"


def test_hanchu_unchanged_payload_sends_nothing():
    """Predbat presses the write button every cycle, so change detection is what stops a write storm."""
    failed = False
    client = ready()
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    with patched_session([(200, envelope(data={}))]) as session_cls:
        run_async_local(client.apply_schedule(SN))
        first_calls = session_cls.return_value.post.call_count
    with patched_session([(200, envelope(data={}))]) as session_cls:
        ok = run_async_local(client.apply_schedule(SN))
        second_calls = session_cls.return_value.post.call_count
    if first_calls != 1:
        print(f"ERROR: the first write took {first_calls} calls")
        failed = True
    if second_calls != 0:
        print(f"ERROR: an unchanged payload sent {second_calls} call(s)")
        failed = True
    if not ok:
        print("ERROR: 'already matches' must report True - the device does match the plan")
        failed = True
    if not logged(client, "unchanged"):
        print("ERROR: the skipped write was not explained")
        failed = True
    assert not failed, "test_hanchu_unchanged_payload_sends_nothing"


def test_hanchu_write_button_does_not_force():
    """The button is Predbat's ordinary per-cycle apply, so forcing it would write every cycle."""
    failed = False
    client = ready()
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    entity = client._control_name("switch", SN, "battery_schedule_charge_write")
    with patched_session([(200, envelope(data={}))]) as session_cls:
        run_async_local(client.switch_event(entity, "turn_on"))
        first_calls = session_cls.return_value.post.call_count
    with patched_session([(200, envelope(data={}))]) as session_cls:
        run_async_local(client.switch_event(entity, "turn_on"))
        second_calls = session_cls.return_value.post.call_count
    if first_calls != 1 or second_calls != 0:
        print(f"ERROR: repeated button presses produced {first_calls} then {second_calls} writes; the second must be 0")
        failed = True
    # The press is what marks the device as Predbat-driven, so the reconcile loop may act from here.
    if SN not in client.control_active:
        print("ERROR: the button press did not mark the device as actively controlled")
        failed = True
    assert not failed, "test_hanchu_write_button_does_not_force"


def test_hanchu_read_only_blocks_the_reconcile_write():
    """The payload is time-aware, so a window transition would write in read-only mode without this."""
    failed = False
    client = ready()
    client.control_active.add(SN)
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    client.state["switch.predbat_set_read_only"] = "on"
    with patched_session([(200, envelope(data={}))]) as session_cls:
        run_async_local(client._reconcile_control(SN))
    if session_cls.return_value.post.call_count:
        print(f"ERROR: {session_cls.return_value.post.call_count} write(s) went out in read-only mode")
        failed = True
    # And with read-only off it does write, so the test above is not passing for the wrong reason.
    client.state["switch.predbat_set_read_only"] = "off"
    with patched_session([(200, envelope(data={}))]) as session_cls:
        run_async_local(client._reconcile_control(SN))
    if session_cls.return_value.post.call_count != 1:
        print(f"ERROR: with read-only off the reconcile should write once, got {session_cls.return_value.post.call_count}")
        failed = True
    assert not failed, "test_hanchu_read_only_blocks_the_reconcile_write"


def test_hanchu_reconcile_ignores_a_device_predbat_does_not_drive():
    """A startup cycle must never clobber an inverter's slots before there is a plan to apply."""
    failed = False
    client = ready()
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    with patched_session([(200, envelope(data={}))]) as session_cls:
        run_async_local(client._reconcile_control(SN))
    if session_cls.return_value.post.call_count:
        print(f"ERROR: {session_cls.return_value.post.call_count} write(s) went to a device whose write button has never been pressed")
        failed = True
    assert not failed, "test_hanchu_reconcile_ignores_a_device_predbat_does_not_drive"


def test_hanchu_control_disabled_writes_nothing():
    """hanchu_control_enable: False is monitoring only."""
    failed = False
    client = ready(client=MockHanchu(control_enable=False))
    client.control_active.add(SN)
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    with patched_session([(200, envelope(data={}))]) as session_cls:
        ok = run_async_local(client.apply_schedule(SN))
    if ok or session_cls.return_value.post.call_count:
        print(f"ERROR: control disabled still wrote: ok={ok} calls={session_cls.return_value.post.call_count}")
        failed = True
    assert not failed, "test_hanchu_control_disabled_writes_nothing"


def test_hanchu_pacing_holds_a_change_rather_than_dropping_it():
    """A held change is retried on the next eligible cycle, so the applied cache must not advance."""
    failed = False
    client = ready(client=MockHanchu(min_write_interval=300))
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    with patched_session([(200, envelope(data={}))]):
        run_async_local(client.apply_schedule(SN))
    applied_after_first = dict(client.applied_payload.get(SN) or {})

    # A new plan moments later is inside the pacing window.
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 80, "power": 2000, "start": "02:00:00", "end": "05:00:00"})
    with patched_session([(200, envelope(data={}))]) as session_cls:
        ok = run_async_local(client.apply_schedule(SN))
    if ok:
        print("ERROR: a held change must not report the device as matching the plan")
        failed = True
    if session_cls.return_value.post.call_count:
        print(f"ERROR: {session_cls.return_value.post.call_count} write(s) escaped the pacing interval")
        failed = True
    if client.applied_payload.get(SN) != applied_after_first:
        print("ERROR: the applied cache advanced for a change that was never sent, so it would never be retried")
        failed = True
    if not logged(client, "hanchu_min_write_interval"):
        print("ERROR: the held change was not explained, and the config key was not named")
        failed = True

    # Once the interval has elapsed the held change goes out.
    client.last_write_time[SN] = time.time() - 301
    with patched_session([(200, envelope(data={}))]) as session_cls:
        ok = run_async_local(client.apply_schedule(SN))
    if not ok or session_cls.return_value.post.call_count != 1:
        print(f"ERROR: the held change was not retried: ok={ok} calls={session_cls.return_value.post.call_count}")
        failed = True
    assert not failed, "test_hanchu_pacing_holds_a_change_rather_than_dropping_it"


def test_hanchu_rejected_write_is_retried_once_then_paced():
    """A rejected batch strands every setting in it, so one retry - then pacing, not hammering."""
    failed = False
    client = ready()
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    responses = [(200, envelope(success=False, code=100, msg="device busy")), (200, envelope(success=False, code=100, msg="device busy"))]
    with patched_session(responses) as session_cls:
        ok = run_async_local(client.apply_schedule(SN))
    if ok:
        print("ERROR: a rejected write must not report success")
        failed = True
    if session_cls.return_value.post.call_count != 2:
        print(f"ERROR: expected the original write plus one retry, got {session_cls.return_value.post.call_count}")
        failed = True
    if client.applied_payload.get(SN):
        print("ERROR: a rejected payload must not be cached as applied, or it would never be resent")
        failed = True
    # The attempt is still stamped, so the reconcile loop cannot re-post it on every tick forever.
    if SN not in client.last_write_time:
        print("ERROR: a failed write did not stamp the pacing timestamp, so a retry storm is possible")
        failed = True
    assert not failed, "test_hanchu_rejected_write_is_retried_once_then_paced"


def test_hanchu_partial_accept_is_not_treated_as_applied():
    """A per-key rejection map means the write only half landed, so it must be rebuilt and resent."""
    failed = False
    client = ready()
    client.local_schedule[SN] = schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "04:00:00"})
    ack = {"responseSemMap": {HANCHU_KEY_CHARGE_POWER: "1", HANCHU_KEY_CHARGE_SOC: "0"}}
    with patched_session([(200, envelope(data=ack))]):
        ok = run_async_local(client.apply_schedule(SN))
    if ok:
        print("ERROR: a partial accept must not report success")
        failed = True
    if client.applied_payload.get(SN):
        print("ERROR: a partially accepted payload must not be cached as applied")
        failed = True
    if not logged(client, HANCHU_KEY_CHARGE_SOC):
        print("ERROR: the refused key was not named in the log")
        failed = True
    # The map is single-source evidence, so its ABSENCE must never be read as a failure.
    client.applied_payload.pop(SN, None)
    with patched_session([(200, envelope(data={}))]):
        ok = run_async_local(client.apply_schedule(SN))
    if not ok:
        print("ERROR: a response with no acknowledgement map must count as accepted")
        failed = True
    assert not failed, "test_hanchu_partial_accept_is_not_treated_as_applied"


def test_hanchu_rejected_keys_reads_only_a_present_map():
    """rejected_keys is tolerant of every shape a response could take."""
    failed = False
    client = MockHanchu()
    cases = [
        (None, []),
        ({}, []),
        ({"responseSemMap": None}, []),
        ({"responseSemMap": {}}, []),
        ({"responseSemMap": {"A": "1", "B": "1"}}, []),
        ({"responseSemMap": {"A": "1", "B": "0"}}, ["B"]),
        ({"responseSemMap": {"B": 2, "A": "no"}}, ["A", "B"]),
    ]
    for data, expect in cases:
        got = client.rejected_keys(data)
        if got != expect:
            print(f"ERROR: rejected_keys({data!r}) = {got} != {expect}")
            failed = True
    assert not failed, "test_hanchu_rejected_keys_reads_only_a_present_map"


def test_hanchu_entity_routing_does_not_confuse_prefixed_serials():
    """An entity for HC701 must never route to HC70 - that would write to the wrong inverter."""
    failed = False
    client = MockHanchu()
    client.device_list = ["HC70", "HC701"]
    for serial in ("HC70", "HC701"):
        entity = client._control_name("number", serial, "battery_schedule_reserve")
        got = client._sn_from_entity(entity)
        if got != serial:
            print(f"ERROR: {entity} routed to {got}, not {serial}")
            failed = True
    if client._sn_from_entity("number.predbat_hanchu_unknown_battery_schedule_reserve") is not None:
        print("ERROR: an unknown serial should not resolve")
        failed = True
    assert not failed, "test_hanchu_entity_routing_does_not_confuse_prefixed_serials"


def test_hanchu_control_events_update_the_local_schedule():
    """Each control entity change lands on the right schedule field, with junk tolerated."""
    failed = False
    client = ready()
    updates = [
        ("number", "battery_schedule_reserve", 12, lambda s: s["reserve"] == 12),
        ("select", "battery_schedule_charge_start_time", "01:30:00", lambda s: s["charge"]["start"] == "01:30:00"),
        ("select", "battery_schedule_charge_end_time", "04:30:00", lambda s: s["charge"]["end"] == "04:30:00"),
        ("number", "battery_schedule_charge_soc", 95, lambda s: s["charge"]["soc"] == 95),
        ("number", "battery_schedule_charge_power", 2600, lambda s: s["charge"]["power"] == 2600),
        ("switch", "battery_schedule_charge_enable", "turn_on", lambda s: s["charge"]["enable"] is True),
        ("switch", "battery_schedule_export_enable", "turn_on", lambda s: s["export"]["enable"] is True),
        ("number", "battery_schedule_export_soc", 20, lambda s: s["export"]["soc"] == 20),
    ]
    for domain, leaf, value, check in updates:
        entity = client._control_name(domain, SN, leaf)
        if domain == "switch":
            run_async_local(client.switch_event(entity, value))
        elif domain == "select":
            run_async_local(client.select_event(entity, value))
        else:
            run_async_local(client.number_event(entity, value))
        if not check(client.local_schedule[SN]):
            print(f"ERROR: {leaf} = {value} did not reach the schedule: {client.local_schedule[SN]}")
            failed = True
    # A toggle flips whatever is held, and junk leaves the value alone rather than raising.
    entity = client._control_name("switch", SN, "battery_schedule_charge_enable")
    run_async_local(client.switch_event(entity, "toggle"))
    if client.local_schedule[SN]["charge"]["enable"] is not False:
        print("ERROR: toggle did not flip the enable switch")
        failed = True
    entity = client._control_name("number", SN, "battery_schedule_charge_soc")
    run_async_local(client.number_event(entity, "unavailable"))
    if client.local_schedule[SN]["charge"]["soc"] != 0:
        print(f"ERROR: an unavailable number should fall back to 0, got {client.local_schedule[SN]['charge']['soc']}")
        failed = True
    assert not failed, "test_hanchu_control_events_update_the_local_schedule"


def test_hanchu_schedule_read_back_survives_unavailable_entities():
    """Right after a Home Assistant restart the entities can read 'unknown' - that must not raise."""
    failed = False
    client = ready()
    for leaf in ("battery_schedule_charge_soc", "battery_schedule_charge_power", "battery_schedule_reserve"):
        client.state[client._control_name("number", SN, leaf)] = "unavailable"
    got = run_async_local(client.get_schedule_settings_ha(SN))
    if got["reserve"] != 0 or got["charge"]["soc"] != 0 or got["charge"]["power"] != 0:
        print(f"ERROR: unavailable entities did not fall back to 0: {got}")
        failed = True
    # A genuinely cold start lands on exactly the empty-schedule shape, so nothing is overwritten.
    cold = ready()
    if run_async_local(cold.get_schedule_settings_ha(SN)) != cold._empty_schedule():
        print("ERROR: a cold read should equal _empty_schedule()")
        failed = True
    assert not failed, "test_hanchu_schedule_read_back_survives_unavailable_entities"


def test_hanchu_collapsed_window_is_disabled_not_wrapped():
    """An inverted or zero-length window is written as disabled, and the decision is logged."""
    failed = False
    client = ready()
    payload = client.build_write_payload(SN, schedule(charge={"enable": True, "soc": 90, "power": 2000, "start": "05:00:00", "end": "04:00:00"}))
    # Slot 1 is absent (never written before, nothing to put in it) rather than wrapped around.
    if payload.get(HANCHU_KEY_CHARGE_START.format(1), HANCHU_SLOT_DISABLED) != HANCHU_SLOT_DISABLED:
        print(f"ERROR: an inverted window was written as {payload.get(HANCHU_KEY_CHARGE_START.format(1))}")
        failed = True
    if not logged(client, "no usable time"):
        print("ERROR: the collapsed window was not explained")
        failed = True
    assert not failed, "test_hanchu_collapsed_window_is_disabled_not_wrapped"


def test_hanchu_restore_state_rebuilds_the_range_tuples():
    """Ranges round-trip through JSON as lists, so restore must hand clamp_range tuples again."""
    failed = False
    client = MockHanchu()
    cached = {
        "static": {"device_list": [SN], "device_detail": {SN: {"capacity_kwh": 10.24}}},
        "config": {"device_ranges": {SN: [[HANCHU_KEY_CHARGE_POWER, [0, 7000]]]}, "device_settings": {SN: {"WORK_MODE_CMB": "3"}}},
        "control": {"local_schedule": {SN: schedule()}, "applied_payload": {SN: {HANCHU_KEY_CHARGE_POWER: 2000}}, "control_active": [SN], "last_write_time": {SN: 1234.0}},
    }

    async def load_cache(name):
        """Stand in for the Storage component."""
        return cached.get(name, {})

    client.load_cache = load_cache
    run_async_local(client.restore_state())
    if client.device_list != [SN]:
        print(f"ERROR: device list not restored: {client.device_list}")
        failed = True
    if client.device_ranges.get(SN, {}).get(HANCHU_KEY_CHARGE_POWER) != (0, 7000):
        print(f"ERROR: range not rebuilt as a tuple: {client.device_ranges}")
        failed = True
    if client.battery_rate_max(SN) != 7000.0:
        print(f"ERROR: the restored range did not reach battery_rate_max: {client.battery_rate_max(SN)}")
        failed = True
    if client.control_active != {SN} or client.last_write_time.get(SN) != 1234.0:
        print(f"ERROR: control state not restored: {client.control_active} {client.last_write_time}")
        failed = True
    if not client._cache_restored:
        print("ERROR: a clean restore should set the guard so it is not repeated")
        failed = True
    # The restored applied payload is what makes the "only zero our own slots" rule survive a
    # restart, and what stops a restart re-sending a payload the device already holds.
    if client.applied_payload.get(SN) != {HANCHU_KEY_CHARGE_POWER: 2000}:
        print(f"ERROR: applied payload not restored: {client.applied_payload}")
        failed = True
    assert not failed, "test_hanchu_restore_state_rebuilds_the_range_tuples"


def test_hanchu_empty_payload_sends_nothing():
    """An empty batch is a no-op, not a request carrying an empty value dict."""
    failed = False
    client = ready()
    with patched_session([(200, envelope(data={}))]) as session_cls:
        ok = run_async_local(client._write_payload(SN, {}))
    if not ok or session_cls.return_value.post.call_count:
        print(f"ERROR: an empty payload produced ok={ok} calls={session_cls.return_value.post.call_count}")
        failed = True
    assert not failed, "test_hanchu_empty_payload_sends_nothing"


def run_hanchu_control_tests(my_predbat):
    """Run all Hanchu control-logic tests."""
    failed = False
    for name, fn in [
        ("charge_window", test_hanchu_charge_window_fills_slot_one),
        ("midnight_split", test_hanchu_midnight_spanning_window_uses_slot_two),
        ("soc_clamping", test_hanchu_soc_limits_are_clamped_and_explained_once),
        ("freeze_rate", test_hanchu_freeze_sends_a_zero_rate_unclamped),
        ("discharge_floor", test_hanchu_discharge_floor_tracks_the_active_export_window),
        ("slot_ownership", test_hanchu_every_unused_slot_is_disabled),
        ("write_serialised", test_hanchu_concurrent_writes_are_serialised),
        ("work_mode", test_hanchu_work_mode_is_asserted_only_with_a_window_and_only_when_enabled),
        ("batched_write", test_hanchu_write_is_one_call_carrying_every_setting),
        ("change_detection", test_hanchu_unchanged_payload_sends_nothing),
        ("button_not_forced", test_hanchu_write_button_does_not_force),
        ("read_only", test_hanchu_read_only_blocks_the_reconcile_write),
        ("reconcile_gate", test_hanchu_reconcile_ignores_a_device_predbat_does_not_drive),
        ("control_disabled", test_hanchu_control_disabled_writes_nothing),
        ("pacing", test_hanchu_pacing_holds_a_change_rather_than_dropping_it),
        ("rejected_write", test_hanchu_rejected_write_is_retried_once_then_paced),
        ("partial_accept", test_hanchu_partial_accept_is_not_treated_as_applied),
        ("rejected_keys", test_hanchu_rejected_keys_reads_only_a_present_map),
        ("entity_routing", test_hanchu_entity_routing_does_not_confuse_prefixed_serials),
        ("control_events", test_hanchu_control_events_update_the_local_schedule),
        ("schedule_read_back", test_hanchu_schedule_read_back_survives_unavailable_entities),
        ("collapsed_window", test_hanchu_collapsed_window_is_disabled_not_wrapped),
        ("restore_state", test_hanchu_restore_state_rebuilds_the_range_tuples),
        ("empty_payload", test_hanchu_empty_payload_sends_nothing),
    ]:
        try:
            if fn():
                print(f"  FAILED: hanchu_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in hanchu_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

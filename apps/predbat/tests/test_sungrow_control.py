# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sungrow control logic and the asynchronous write path
# -----------------------------------------------------------------------------

"""Tests for the Sungrow control decision and the dispatch-then-poll write path."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from unittest.mock import patch
from sungrow_const import (
    SUNGROW_CMD_CHARGE,
    SUNGROW_CMD_DISCHARGE,
    SUNGROW_CMD_STOP,
    SUNGROW_PARAM_EMS_MODE,
    SUNGROW_PARAM_COMMAND,
    SUNGROW_PARAM_POWER,
    SUNGROW_PARAM_HEARTBEAT,
    SUNGROW_PARAM_SOC_UPPER,
    SUNGROW_PARAM_SOC_LOWER,
    SUNGROW_PARAM_FORCED_CHARGING,
    SUNGROW_PARAM_FORCED_START_HOUR_1,
    SUNGROW_PARAM_FORCED_TARGET_SOC_1,
    SUNGROW_EMS_EXTERNAL_DISPATCH,
    SUNGROW_EMS_SELF_CONSUMPTION,
    SUNGROW_ENABLE,
    SUNGROW_DISABLE,
)
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session
from tests.test_sungrow_api import MockSungrow, _envelope, _mock_post, PLANT_LIST, DEVICE_LIST, PLANT_REALTIME, DEVICE_REALTIME

UUID = "987654"


def _controlled_client(**kwargs):
    """Return a client that has discovered one inverter and polled telemetry once."""
    client = MockSungrow(**kwargs)
    with _mock_post([_envelope(PLANT_LIST), _envelope(DEVICE_LIST)]):
        run_async_local(client.refresh_static())
    with _mock_post([_envelope(PLANT_REALTIME), _envelope(DEVICE_REALTIME)]):
        run_async_local(client.refresh_power())
    return client


def _schedule(charge=None, export=None, reserve=0):
    """Build a schedule in the shape the control entities produce."""
    base = {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": dict(base, **(charge or {})), "export": dict(base, **(export or {}))}


def _params_as_map(param_list):
    """Flatten a param_list into {param_code: set_value} for easy assertions."""
    return {entry["param_code"]: entry["set_value"] for entry in param_list}


# ---------------------------------------------------------------------------
# decide_command
# ---------------------------------------------------------------------------


def test_decide_charge_inside_an_active_window():
    """An enabled charge window with real power and a target above SoC is a forced charge."""
    failed = False
    client = _controlled_client()
    client.set_mock_clock(3 * 60)  # 03:00, inside 02:00-05:00
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    decision = client.decide_command(UUID, schedule)
    if decision is None:
        print("ERROR: no decision inside an active charge window")
        assert False, "test_decide_charge_inside_an_active_window"
    if decision["command"] != SUNGROW_CMD_CHARGE:
        print(f"ERROR: command {decision['command']}, expected charge")
        failed = True
    if decision["power"] != 3000:
        print(f"ERROR: power {decision['power']}")
        failed = True
    # The charge target is the UPPER limit (10001), which is what stops the charge.
    if decision.get("soc_upper") != 90:
        print(f"ERROR: soc_upper {decision.get('soc_upper')}")
        failed = True
    assert not failed, "test_decide_charge_inside_an_active_window"


def test_decide_releases_control_outside_every_window():
    """Nothing to hold means release, which also lets the heartbeat stop."""
    failed = False
    client = _controlled_client()
    client.set_mock_clock(12 * 60)  # midday, outside 02:00-05:00
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    if client.decide_command(UUID, schedule) is not None:
        print("ERROR: control was taken outside the planned window")
        failed = True
    assert not failed, "test_decide_releases_control_outside_every_window"


def test_decide_export_beats_charge():
    """An active export window with power takes precedence, with its target as the LOWER limit."""
    failed = False
    client = _controlled_client()
    client.set_mock_clock(17 * 60)
    schedule = _schedule(
        charge={"enable": True, "soc": 90, "power": 3000, "start": "16:00:00", "end": "19:00:00"},
        export={"enable": True, "soc": 20, "power": 4000, "start": "16:00:00", "end": "19:00:00"},
        reserve=10,
    )
    decision = client.decide_command(UUID, schedule)
    if decision["command"] != SUNGROW_CMD_DISCHARGE:
        print(f"ERROR: command {decision['command']}, expected discharge")
        failed = True
    # The lower limit is what stops the discharge at the target rather than at the pack floor.
    if decision.get("soc_lower") != 20:
        print(f"ERROR: soc_lower {decision.get('soc_lower')}")
        failed = True
    if "soc_upper" in decision:
        print("ERROR: an export decision should not carry an upper limit")
        failed = True
    assert not failed, "test_decide_export_beats_charge"


def test_decide_export_lower_limit_never_dips_below_reserve():
    """Predbat's reserve is a floor the discharge target cannot undercut."""
    failed = False
    client = _controlled_client()
    client.set_mock_clock(17 * 60)
    schedule = _schedule(export={"enable": True, "soc": 5, "power": 4000, "start": "16:00:00", "end": "19:00:00"}, reserve=15)
    decision = client.decide_command(UUID, schedule)
    if decision.get("soc_lower") != 15:
        print(f"ERROR: soc_lower {decision.get('soc_lower')}, expected the reserve of 15")
        failed = True
    assert not failed, "test_decide_export_lower_limit_never_dips_below_reserve"


def test_decide_freeze_export_becomes_the_stop_command():
    """Predbat expresses every no-discharge hold as an export window at zero power.

    Sungrow has a real stop command (10004 = 204), so unlike AlphaESS this needs no synthetic
    low-target charge profile to express a hold.
    """
    failed = False
    client = _controlled_client()
    client.set_mock_clock(17 * 60)
    schedule = _schedule(export={"enable": True, "soc": 20, "power": 0, "start": "16:00:00", "end": "19:00:00"}, reserve=10)
    decision = client.decide_command(UUID, schedule)
    if decision is None:
        print("ERROR: a freeze produced no decision, so the battery would be left free to discharge")
        assert False, "test_decide_freeze_export_becomes_the_stop_command"
    if decision["command"] != SUNGROW_CMD_STOP:
        print(f"ERROR: command {decision['command']}, expected stop")
        failed = True
    if decision["power"] != 0:
        print(f"ERROR: power {decision['power']}")
        failed = True
    assert not failed, "test_decide_freeze_export_becomes_the_stop_command"


def test_decide_holds_once_the_charge_target_is_reached():
    """A reached target inside its own window is a hold, not a discharge back down to it.

    execute_plan clears the enable switch once the target is met but deliberately leaves the
    window times intact, which is the signal this branch reads.
    """
    failed = False
    client = _controlled_client()
    client.set_mock_clock(3 * 60)
    # Telemetry says SoC is 64%; a target of 60% has already been passed.
    schedule = _schedule(charge={"enable": False, "soc": 60, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    decision = client.decide_command(UUID, schedule)
    if decision is None or decision["command"] != SUNGROW_CMD_STOP:
        print(f"ERROR: a reached charge target gave {decision}, expected a stop")
        failed = True
    # Outside the window the same stale entities must NOT extend the hold.
    client.set_mock_clock(12 * 60)
    if client.decide_command(UUID, schedule) is not None:
        print("ERROR: stale target/time entities extended the hold beyond its planned window")
        failed = True
    assert not failed, "test_decide_holds_once_the_charge_target_is_reached"


def test_decide_ignores_a_charge_window_with_no_power():
    """A charge window at zero power is Predbat freezing, not asking for a charge."""
    failed = False
    client = _controlled_client()
    client.set_mock_clock(3 * 60)
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 0, "start": "02:00:00", "end": "05:00:00"})
    decision = client.decide_command(UUID, schedule)
    if decision is not None and decision["command"] == SUNGROW_CMD_CHARGE:
        print("ERROR: a zero-power charge window was sent as a charge")
        failed = True
    assert not failed, "test_decide_ignores_a_charge_window_with_no_power"


def test_window_active_now_handles_a_window_crossing_midnight():
    """Predbat represents a window crossing midnight as 23:00-25:00."""
    failed = False
    client = _controlled_client()
    window = {"start": "23:00:00", "end": "25:00:00"}
    client.set_mock_clock(23 * 60 + 30)
    if not client._window_active_now(window):
        print("ERROR: 23:30 was not inside 23:00-25:00")
        failed = True
    client.set_mock_clock(30)
    if not client._window_active_now(window):
        print("ERROR: 00:30 was not inside 23:00-25:00")
        failed = True
    client.set_mock_clock(12 * 60)
    if client._window_active_now(window):
        print("ERROR: midday read as inside 23:00-25:00")
        failed = True
    # A zero-length window is disabled.
    if client._window_active_now({"start": "02:00:00", "end": "02:00:00"}):
        print("ERROR: a zero-length window read as active")
        failed = True
    assert not failed, "test_window_active_now_handles_a_window_crossing_midnight"


# ---------------------------------------------------------------------------
# build_param_list
# ---------------------------------------------------------------------------


def test_build_param_list_writes_the_mode_and_heartbeat_with_the_command():
    """10003 and 10017 travel WITH the command, because 10004 alone is silently ignored.

    Writing the command while the inverter is still in self-consumption is accepted and does
    nothing, which is the worst failure mode available: no error, no effect.
    """
    failed = False
    client = _controlled_client(heartbeat_interval=120)
    params = client.build_param_list({"command": SUNGROW_CMD_CHARGE, "power": 3000, "soc_upper": 90, "reason": "test"})
    values = _params_as_map(params)
    if values.get(SUNGROW_PARAM_EMS_MODE) != str(SUNGROW_EMS_EXTERNAL_DISPATCH):
        print(f"ERROR: EMS mode {values.get(SUNGROW_PARAM_EMS_MODE)}, expected external dispatch")
        failed = True
    if values.get(SUNGROW_PARAM_HEARTBEAT) != "120":
        print(f"ERROR: heartbeat {values.get(SUNGROW_PARAM_HEARTBEAT)}")
        failed = True
    if values.get(SUNGROW_PARAM_COMMAND) != str(SUNGROW_CMD_CHARGE):
        print(f"ERROR: command {values.get(SUNGROW_PARAM_COMMAND)}")
        failed = True
    if values.get(SUNGROW_PARAM_POWER) != "3000":
        print(f"ERROR: power {values.get(SUNGROW_PARAM_POWER)}")
        failed = True
    # 90% goes out as 900 - the documented range is 700-1000 for 70-100%.
    if values.get(SUNGROW_PARAM_SOC_UPPER) != "900":
        print(f"ERROR: soc upper {values.get(SUNGROW_PARAM_SOC_UPPER)}, expected 900")
        failed = True
    if SUNGROW_PARAM_SOC_LOWER in values:
        print("ERROR: a charge decision should not carry a lower limit")
        failed = True
    # The mode must be first in the list, so it is applied before the command it enables.
    if params[0]["param_code"] != SUNGROW_PARAM_EMS_MODE:
        print(f"ERROR: the first parameter is {params[0]['param_code']}, expected the EMS mode")
        failed = True
    assert not failed, "test_build_param_list_writes_the_mode_and_heartbeat_with_the_command"


def test_build_forced_charge_params_disables_an_empty_window():
    """No usable window means the forced-charging schedule is switched off, not left stale."""
    failed = False
    client = _controlled_client(forced_charge_schedule=True)
    params = client.build_forced_charge_params(_schedule(charge={"enable": False}))
    values = _params_as_map(params)
    if values.get(SUNGROW_PARAM_FORCED_CHARGING) != str(SUNGROW_DISABLE):
        print(f"ERROR: forced charging {values.get(SUNGROW_PARAM_FORCED_CHARGING)}, expected disable")
        failed = True
    if len(params) != 1:
        print("ERROR: a disabled schedule should write only the off switch")
        failed = True
    assert not failed, "test_build_forced_charge_params_disables_an_empty_window"


def test_build_forced_charge_params_splits_the_window_into_hour_and_minute():
    """The window parameters take hour and minute as separate codes, not a time string."""
    failed = False
    client = _controlled_client(forced_charge_schedule=True)
    params = client.build_forced_charge_params(_schedule(charge={"enable": True, "soc": 85, "power": 3000, "start": "02:30:00", "end": "05:15:00"}))
    values = _params_as_map(params)
    if values.get(SUNGROW_PARAM_FORCED_CHARGING) != str(SUNGROW_ENABLE):
        print("ERROR: forced charging was not enabled")
        failed = True
    if values.get(SUNGROW_PARAM_FORCED_START_HOUR_1) != "2":
        print(f"ERROR: start hour {values.get(SUNGROW_PARAM_FORCED_START_HOUR_1)}")
        failed = True
    # Target SoC here is scale 1, unlike the 10001 limit which is scale 10.
    if values.get(SUNGROW_PARAM_FORCED_TARGET_SOC_1) != "85":
        print(f"ERROR: target soc {values.get(SUNGROW_PARAM_FORCED_TARGET_SOC_1)}, expected an unscaled 85")
        failed = True
    assert not failed, "test_build_forced_charge_params_splits_the_window_into_hour_and_minute"


def test_forced_charge_schedule_is_not_written_by_default():
    """It is stored in the inverter and survives Predbat stopping, so it defeats the safe revert."""
    failed = False
    client = _controlled_client()
    if client.forced_charge_schedule:
        print("ERROR: the forced-charge schedule defaulted on")
        failed = True
    params = client.build_param_list({"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"})
    if any(entry["param_code"] == SUNGROW_PARAM_FORCED_CHARGING for entry in params):
        print("ERROR: the forced-charging parameters were included in an ordinary control write")
        failed = True
    assert not failed, "test_forced_charge_schedule_is_not_written_by_default"


# ---------------------------------------------------------------------------
# The asynchronous write path
# ---------------------------------------------------------------------------

CHECK_OK = {"check_result": "1", "dev_result_list": [{"check_result": "1"}]}
DISPATCH_OK = {"check_result": "1", "dev_result_list": [{"code": "1", "task_id": "task-1"}]}
TASK_RUNNING = {"command_status": 2}
TASK_DONE = {"command_status": 8, "param_list": [{"param_code": "10004", "set_value": "170", "point_name": "Charge/Discharge command"}]}


def test_dispatch_polls_until_the_task_completes():
    """paramSetting only QUEUES a task; the value is not applied until getParamSettingTask says so.

    Treating the dispatch envelope as "the inverter has the value" is the single easiest mistake
    to make against this API, so no caller ever sees the task_id.
    """
    failed = False
    client = _controlled_client()
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_RUNNING), _envelope(TASK_DONE)]):
            result = run_async_local(client.dispatch_params(UUID, [{"param_code": "10004", "set_value": "170"}]))
    if result is None:
        print("ERROR: a task that completed after one running poll returned None")
        failed = True
    elif result[0]["param_code"] != "10004":
        print(f"ERROR: param_list {result}")
        failed = True
    assert not failed, "test_dispatch_polls_until_the_task_completes"


def _no_sleep():
    """Return an async no-op to stand in for asyncio.sleep, so poll loops do not burn wall clock."""

    async def sleeper(_seconds):
        """Return immediately instead of sleeping."""
        return None

    return sleeper


def test_dispatch_returns_none_when_the_task_fails():
    """A terminal status other than done is a failure, not a silent success."""
    failed = False
    client = _controlled_client()
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope({"command_status": 4})]):
            result = run_async_local(client.dispatch_params(UUID, [{"param_code": "10004", "set_value": "170"}]))
    if result is not None:
        print(f"ERROR: a failed task returned {result}")
        failed = True
    assert not failed, "test_dispatch_returns_none_when_the_task_fails"


def test_dispatch_rejects_a_response_with_no_task_id():
    """An accepted dispatch with no task_id cannot be polled, so it is not a success."""
    failed = False
    client = _controlled_client()
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope({"check_result": "1", "dev_result_list": [{"code": "1"}]})]):
            result = run_async_local(client.dispatch_params(UUID, [{"param_code": "10004", "set_value": "170"}]))
    if result is not None:
        print("ERROR: a dispatch with no task_id was treated as a success")
        failed = True
    assert not failed, "test_dispatch_rejects_a_response_with_no_task_id"


def test_dispatch_fails_when_a_parameter_inside_a_completed_task_failed():
    """A COMPLETED TASK IS NOT A SUCCESSFUL WRITE.

    The status is two-level: the task reports command_status 8 once it has finished running,
    while each parameter inside it carries its own status and can have failed or timed out.
    Reading only the task level reports a setpoint as landed when the inverter never took it -
    and _write_params would then cache the decision as applied and never retry it.
    """
    failed = False
    client = _controlled_client()
    partial = {
        "command_status": 8,
        "param_list": [
            {"param_code": "10003", "set_value": "3", "command_status": 4},
            {"param_code": "10004", "set_value": "170", "command_status": 5},
        ],
    }
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(partial)]):
            result = run_async_local(client.dispatch_params(UUID, [{"param_code": "10004", "set_value": "170"}]))
    if result is not None:
        print(f"ERROR: a task whose command parameter FAILED returned {result}")
        failed = True
    if not any("did not take" in message for message in client.log_messages):
        print("ERROR: the per-parameter failure was not named in the log")
        failed = True
    assert not failed, "test_dispatch_fails_when_a_parameter_inside_a_completed_task_failed"


def test_dispatch_tolerates_a_task_that_omits_per_parameter_status():
    """An omitted per-parameter status is not a failure.

    Not every response carries command_status inside param_list, and treating its absence as a
    failure would reject every successful write on such an account.
    """
    failed = False
    client = _controlled_client()
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            result = run_async_local(client.dispatch_params(UUID, [{"param_code": "10004", "set_value": "170"}]))
    if result is None:
        print("ERROR: a completed task with no per-parameter status was rejected")
        failed = True
    assert not failed, "test_dispatch_tolerates_a_task_that_omits_per_parameter_status"


def test_a_partially_failed_task_is_not_cached_as_applied():
    """The whole point of checking per-parameter status: the decision must stay retryable."""
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    partial = {"command_status": 8, "param_list": [{"param_code": "10005", "set_value": "3000", "command_status": 6}]}
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(partial)]):
            ok = run_async_local(client._write_params(UUID, {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"}))
    if ok:
        print("ERROR: a partially failed write reported the inverter as matching the plan")
        failed = True
    if client.applied_command.get(UUID) is not None:
        print("ERROR: a partially failed write was cached as applied, so it would never be retried")
        failed = True
    if UUID in client.control_held:
        print("ERROR: control was marked held although a parameter did not take")
        failed = True
    assert not failed, "test_a_partially_failed_task_is_not_cached_as_applied"


def test_write_params_checks_support_once_and_caches_the_verdict():
    """paramSettingCheck is a hardware/account fact, so re-probing it every cycle wastes a call."""
    failed = False
    client = _controlled_client()
    client.local_schedule[UUID] = _schedule()
    decision = {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"}
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(CHECK_OK), _envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            ok = run_async_local(client._write_params(UUID, decision))
    if not ok:
        print("ERROR: the first write did not succeed")
        failed = True
    if client._write_supported.get(UUID) is not True:
        print("ERROR: the support verdict was not cached")
        failed = True
    # The second write must not re-issue paramSettingCheck - only dispatch and poll.
    decision2 = {"command": SUNGROW_CMD_DISCHARGE, "power": 2000, "reason": "test2"}
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            ok = run_async_local(client._write_params(UUID, decision2))
    if not ok:
        print("ERROR: the second write did not succeed, so the support check was probably repeated")
        failed = True
    assert not failed, "test_write_params_checks_support_once_and_caches_the_verdict"


def test_write_params_refuses_a_device_that_cannot_be_configured():
    """An unsupported device is monitored, not repeatedly written to."""
    failed = False
    client = _controlled_client()
    unsupported = {"check_result": "1", "dev_result_list": [{"check_result": "0"}]}
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(unsupported)]):
            ok = run_async_local(client._write_params(UUID, {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"}))
    if ok:
        print("ERROR: a write to an unsupported device reported success")
        failed = True
    if client._write_supported.get(UUID) is not False:
        print("ERROR: the unsupported verdict was not cached, so it will be re-probed forever")
        failed = True
    if UUID in client.control_held:
        print("ERROR: control was marked held for a device that cannot be configured")
        failed = True
    assert not failed, "test_write_params_refuses_a_device_that_cannot_be_configured"


def test_write_params_does_not_cache_a_failed_write():
    """A rejected write must keep retrying - just paced, not hammering."""
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    decision = {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"}
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(None, code="E00000", message="device offline")]):
            ok = run_async_local(client._write_params(UUID, decision))
    if ok:
        print("ERROR: a rejected write reported success")
        failed = True
    if client.applied_command.get(UUID) is not None:
        print("ERROR: a failed write was cached as applied, so it would never be retried")
        failed = True
    if UUID in client.control_held:
        print("ERROR: control was marked held although the task never landed")
        failed = True
    # The attempt is still stamped, or the reconcile loop re-dispatches on every tick forever.
    if UUID not in client.last_write_time:
        print("ERROR: a failed attempt was not paced")
        failed = True
    assert not failed, "test_write_params_does_not_cache_a_failed_write"


def test_write_params_skips_an_unchanged_decision():
    """An identical decision needs nothing sent, which is what keeps the write button cheap.

    Predbat presses the write button on every cycle as its normal action, not only when the plan
    changed, so without this gate every cycle would dispatch a byte-identical task.
    """
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    decision = {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "test"}
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            run_async_local(client._write_params(UUID, dict(decision)))
    # No mocked responses at all: if anything were sent, the call would raise or fail.
    with patch("aiohttp.ClientSession", side_effect=AssertionError("a write went out for an unchanged decision")):
        ok = run_async_local(client._write_params(UUID, dict(decision)))
    if not ok:
        print("ERROR: an unchanged decision did not report the inverter as matching")
        failed = True
    assert not failed, "test_write_params_skips_an_unchanged_decision"


def test_write_params_holds_a_change_inside_the_pacing_window():
    """A change arriving inside min_write_interval is HELD, not dropped."""
    failed = False
    client = _controlled_client(min_write_interval=300)
    client._write_supported[UUID] = True
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(DISPATCH_OK), _envelope(TASK_DONE)]):
            run_async_local(client._write_params(UUID, {"command": SUNGROW_CMD_CHARGE, "power": 3000, "reason": "a"}))
    with patch("aiohttp.ClientSession", side_effect=AssertionError("a paced write went out anyway")):
        ok = run_async_local(client._write_params(UUID, {"command": SUNGROW_CMD_DISCHARGE, "power": 2000, "reason": "b"}))
    if ok:
        print("ERROR: a held change reported the inverter as matching the plan")
        failed = True
    # The applied cache is untouched, so the next eligible tick rebuilds and sends it.
    if client.applied_command[UUID]["command"] != SUNGROW_CMD_CHARGE:
        print("ERROR: the held change overwrote the applied cache")
        failed = True
    assert not failed, "test_write_params_holds_a_change_inside_the_pacing_window"


def test_release_control_writes_self_consumption():
    """Releasing hands the inverter back explicitly rather than waiting for the heartbeat to lapse."""
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    client.applied_command[UUID] = {"command": SUNGROW_CMD_CHARGE}
    captured = {}
    response = create_aiohttp_mock_response(json_data=_envelope(DISPATCH_OK))
    done = create_aiohttp_mock_response(json_data=_envelope(TASK_DONE))
    session = create_aiohttp_mock_session([response, done])
    original_post = session.post

    def capture_post(url, headers=None, json=None):
        """Record each outgoing body so the released mode can be asserted."""
        captured.setdefault("bodies", []).append(json)
        return original_post(url, headers=headers, json=json)

    session.post = capture_post
    with patch("asyncio.sleep", new=_no_sleep()):
        with patch("aiohttp.ClientSession", return_value=session):
            run_async_local(client.release_control(UUID))
    first = captured["bodies"][0]
    values = _params_as_map(first["param_list"])
    if values.get(SUNGROW_PARAM_EMS_MODE) != str(SUNGROW_EMS_SELF_CONSUMPTION):
        print(f"ERROR: released with EMS mode {values.get(SUNGROW_PARAM_EMS_MODE)}, expected self-consumption")
        failed = True
    if UUID in client.control_held:
        print("ERROR: the device is still marked as held after a release")
        failed = True
    assert not failed, "test_release_control_writes_self_consumption"


def test_release_control_gives_up_holding_even_when_the_write_fails():
    """A failed release still stops the beats, so the inverter reverts on its own.

    Continuing to beat would hold a mode Predbat no longer wants, which is worse than losing the
    explicit release - the dead-man's switch gets there anyway within one interval.
    """
    failed = False
    client = _controlled_client()
    client.control_held.add(UUID)
    with patch("asyncio.sleep", new=_no_sleep()):
        with _mock_post([_envelope(None, code="E00000", message="offline")]):
            run_async_local(client.release_control(UUID))
    if UUID in client.control_held:
        print("ERROR: a failed release left the device marked as held, so the beats would continue")
        failed = True
    assert not failed, "test_release_control_gives_up_holding_even_when_the_write_fails"


def test_reconcile_control_is_gated_on_read_only():
    """The decision is time-aware, so a window boundary changes it with no plan change at all.

    Predbat's own read-only handling covers writes that originate from a plan, but not one the
    component initiates itself - and the reconcile loop is exactly that (GH#4436).
    """
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    client.control_active.add(UUID)
    client.set_mock_clock(3 * 60)
    client.local_schedule[UUID] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    client.state["switch.predbat_set_read_only"] = "on"
    with patch("aiohttp.ClientSession", side_effect=AssertionError("a write went out in read-only mode")):
        run_async_local(client._reconcile_control(UUID))
    if client.applied_command.get(UUID) is not None:
        print("ERROR: read-only mode did not stop the reconcile write")
        failed = True
    assert not failed, "test_reconcile_control_is_gated_on_read_only"


def test_reconcile_control_only_runs_for_inverters_predbat_drives():
    """A startup cycle must never take control before the write button has ever been pressed."""
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    client.set_mock_clock(3 * 60)
    client.local_schedule[UUID] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch("aiohttp.ClientSession", side_effect=AssertionError("a write went out before the write button")):
        run_async_local(client._reconcile_control(UUID))
    if UUID in client.control_held:
        print("ERROR: control was taken without the write button having been pressed")
        failed = True
    assert not failed, "test_reconcile_control_only_runs_for_inverters_predbat_drives"


def test_control_enable_false_writes_nothing():
    """Monitoring-only means monitoring only, including the component's own reconcile."""
    failed = False
    client = _controlled_client(control_enable=False)
    client.control_active.add(UUID)
    client.local_schedule[UUID] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    client.set_mock_clock(3 * 60)
    with patch("aiohttp.ClientSession", side_effect=AssertionError("a write went out with control disabled")):
        result = run_async_local(client.apply_schedule(UUID))
    if result:
        print("ERROR: apply_schedule reported success with control disabled")
        failed = True
    assert not failed, "test_control_enable_false_writes_nothing"


def test_write_button_press_marks_the_inverter_as_driven():
    """Marked on the press itself, not on a successful write: a failed write still means ownership."""
    failed = False
    client = _controlled_client()
    client._write_supported[UUID] = True
    client.local_schedule[UUID] = _schedule()
    client.set_mock_clock(12 * 60)
    entity = client._control_name("switch", UUID, "battery_schedule_charge_write")
    run_async_local(client.switch_event(entity, "turn_on"))
    if UUID not in client.control_active:
        print("ERROR: the write button press did not mark the inverter as driven")
        failed = True
    assert not failed, "test_write_button_press_marks_the_inverter_as_driven"


def test_control_entities_round_trip_through_home_assistant():
    """What is published is what is read back, so Predbat's write-and-poll confirm succeeds."""
    failed = False
    client = _controlled_client()
    client.local_schedule[UUID] = _schedule(charge={"enable": True, "soc": 85, "power": 3200, "start": "02:00:00", "end": "05:00:00"}, reserve=12)
    run_async_local(client.publish_schedule_settings_ha(UUID))
    read_back = run_async_local(client.get_schedule_settings_ha(UUID))
    if read_back["reserve"] != 12:
        print(f"ERROR: reserve round-tripped as {read_back['reserve']}")
        failed = True
    if read_back["charge"] != {"enable": True, "soc": 85, "power": 3200, "start": "02:00:00", "end": "05:00:00"}:
        print(f"ERROR: charge window round-tripped as {read_back['charge']}")
        failed = True
    assert not failed, "test_control_entities_round_trip_through_home_assistant"


def test_entity_ids_resolve_back_to_the_right_inverter():
    """An entity for A2211 must never route to A221, which would write to the wrong inverter."""
    failed = False
    client = _controlled_client()
    entity = client._control_name("number", UUID, "battery_schedule_charge_soc")
    if client._uuid_from_entity(entity) != UUID:
        print(f"ERROR: {entity} resolved to {client._uuid_from_entity(entity)}")
        failed = True
    if client._uuid_from_entity("number.predbat_sungrow_a2211test0_battery_schedule_charge_soc") is not None:
        print("ERROR: a prefix-colliding serial resolved to the wrong inverter")
        failed = True
    assert not failed, "test_entity_ids_resolve_back_to_the_right_inverter"


def run_sungrow_control_tests(my_predbat):
    """Run all Sungrow control-logic tests."""
    failed = False
    for name, fn in [
        ("decide_charge", test_decide_charge_inside_an_active_window),
        ("decide_release", test_decide_releases_control_outside_every_window),
        ("decide_export_precedence", test_decide_export_beats_charge),
        ("decide_reserve_floor", test_decide_export_lower_limit_never_dips_below_reserve),
        ("decide_freeze_stop", test_decide_freeze_export_becomes_the_stop_command),
        ("decide_target_reached", test_decide_holds_once_the_charge_target_is_reached),
        ("decide_zero_power_charge", test_decide_ignores_a_charge_window_with_no_power),
        ("window_crosses_midnight", test_window_active_now_handles_a_window_crossing_midnight),
        ("param_list_mode_and_heartbeat", test_build_param_list_writes_the_mode_and_heartbeat_with_the_command),
        ("forced_disable", test_build_forced_charge_params_disables_an_empty_window),
        ("forced_hour_minute", test_build_forced_charge_params_splits_the_window_into_hour_and_minute),
        ("forced_off_by_default", test_forced_charge_schedule_is_not_written_by_default),
        ("dispatch_polls", test_dispatch_polls_until_the_task_completes),
        ("dispatch_failure", test_dispatch_returns_none_when_the_task_fails),
        ("dispatch_no_task_id", test_dispatch_rejects_a_response_with_no_task_id),
        ("param_status_failure", test_dispatch_fails_when_a_parameter_inside_a_completed_task_failed),
        ("param_status_absent_ok", test_dispatch_tolerates_a_task_that_omits_per_parameter_status),
        ("partial_failure_not_cached", test_a_partially_failed_task_is_not_cached_as_applied),
        ("support_cached", test_write_params_checks_support_once_and_caches_the_verdict),
        ("unsupported_device", test_write_params_refuses_a_device_that_cannot_be_configured),
        ("failed_write_not_cached", test_write_params_does_not_cache_a_failed_write),
        ("unchanged_decision", test_write_params_skips_an_unchanged_decision),
        ("pacing_holds", test_write_params_holds_a_change_inside_the_pacing_window),
        ("release_writes_self_consumption", test_release_control_writes_self_consumption),
        ("release_on_failure", test_release_control_gives_up_holding_even_when_the_write_fails),
        ("reconcile_read_only", test_reconcile_control_is_gated_on_read_only),
        ("reconcile_only_when_driven", test_reconcile_control_only_runs_for_inverters_predbat_drives),
        ("control_disabled", test_control_enable_false_writes_nothing),
        ("write_button", test_write_button_press_marks_the_inverter_as_driven),
        ("entities_round_trip", test_control_entities_round_trip_through_home_assistant),
        ("entity_resolution", test_entity_ids_resolve_back_to_the_right_inverter),
    ]:
        try:
            if fn():
                print(f"  FAILED: sungrow_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sungrow_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

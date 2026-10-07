# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fronius control logic and the schedule write path
# -----------------------------------------------------------------------------

"""Tests for how the Fronius component turns Predbat's control entities into a dated schedule."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from datetime import datetime, timezone
from fronius_const import ACTION_CHARGE, ACTION_HOLD, FRONIUS_CACHE_CONTROL, FRONIUS_STORAGE_MODULE
from tests.test_infra import run_async
from tests.test_fronius_api import ENERGY, DEVICES, FLOW, PV_ID, SHORT_ID, FakeResponse, FakeStorage, MockFronius, http, logged

ACCEPTED = {"dispatchId": "52dd9d36-ec43-406a-9038-8bc317ac9366"}


def entity(domain, leaf):
    """Return a Fronius control entity id for the test system."""
    return "{}.predbat_fronius_{}_{}".format(domain, SHORT_ID, leaf)


def set_window(client, direction, start="00:00:00", end="00:00:00", soc=0, power=3000, enable=False):
    """Write one window's control entities the way Predbat would."""
    client.state[entity("select", "battery_schedule_{}_start_time".format(direction))] = start
    client.state[entity("select", "battery_schedule_{}_end_time".format(direction))] = end
    client.state[entity("number", "battery_schedule_{}_soc".format(direction))] = soc
    client.state[entity("number", "battery_schedule_{}_power".format(direction))] = power
    client.state[entity("switch", "battery_schedule_{}_enable".format(direction))] = "on" if enable else "off"


def controlled_client(now=None, tz="Europe/London", soc=55, **kwargs):
    """Return a client with metadata and telemetry read, Predbat driving it, and idle windows."""
    client = MockFronius(tz=tz, **({"now": now} if now else {}), **kwargs)
    transport, patcher = http(FakeResponse(200, DEVICES), FakeResponse(200, FLOW), FakeResponse(200, ENERGY))
    with patcher:
        run_async(client.refresh_static())
        run_async(client.refresh_power())
        run_async(client.refresh_energy())
    client.flow["BattSOC"] = soc
    client.control_active = True
    set_window(client, "charge")
    set_window(client, "export")
    return client


def apply(client, *responses):
    """Read the control entities and apply the schedule against canned responses; return the calls."""
    transport, patcher = http(*responses)
    client.get_schedule_settings_ha()
    with patcher:
        result = run_async(client.apply_schedule())
    return result, transport.calls


def body(call):
    """Return the command list a PUT sent."""
    assert call["method"] == "PUT", call
    assert isinstance(call["json"], list), "the single-system schedule body is a bare command array"
    return call["json"]


def test_fronius_charge_window_becomes_charge_battery():
    """An active charge window is an exact-rate ChargeBattery from now, clipped to the horizon."""
    client = controlled_client()
    # 13:00-14:30 BST = 12:00Z-13:30Z; now is 12:00Z, horizon 13:00Z.
    set_window(client, "charge", "13:00:00", "14:30:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert ok and len(calls) == 1, calls
    assert calls[0]["url"].endswith("/pvsystems/{}/schedules".format(PV_ID))
    commands = body(calls[0])
    assert commands == [
        {
            "dispatchType": "ChargeBattery",
            "dispatchDateTime": "2026-06-15T12:00:00Z",
            "dispatchDuration": 3600,
            "dispatchParameters": [{"name": "MaxW", "value": 3000}, {"name": "MinW", "value": 3000}],
            "dispatchPayload": commands[0]["dispatchPayload"],
        }
    ], commands
    assert commands[0]["dispatchPayload"].startswith("predbat-")
    assert client.dispatch_id == ACCEPTED["dispatchId"]


def test_fronius_unchanged_schedule_is_not_resent():
    """The same plan is not re-sent each tick; it is renewed only when the sent one runs short."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "15:30:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert ok and len(calls) == 1
    ok, calls = apply(client)
    assert ok and calls == [], "an identical schedule was re-sent"
    client.advance(minutes=5)
    ok, calls = apply(client)
    assert ok and calls == [], "a moving clock alone must not re-send"
    client.advance(minutes=26)  # 12:31Z: the sent schedule (ends 13:00Z) has under 30 minutes left
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert ok and len(calls) == 1, "the schedule was not renewed before running out"
    commands = body(calls[0])
    assert commands[0]["dispatchDateTime"] == "2026-06-15T12:31:00Z" and commands[0]["dispatchDuration"] == (14 * 60 - 12 * 60 - 31) * 60, commands


def test_fronius_target_reached_switches_to_hold():
    """ChargeBattery has no target, so reaching it is turned into MaxW=0 for the rest of the window."""
    client = controlled_client(soc=80)
    set_window(client, "charge", "13:00:00", "14:30:00", soc=90, power=3000, enable=True)
    apply(client, FakeResponse(200, ACCEPTED))
    client.flow["BattSOC"] = 90
    client.advance(minutes=1)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert ok and len(calls) == 1, calls
    commands = body(calls[0])
    assert commands[0]["dispatchType"] == "ChargeBattery" and commands[0]["dispatchParameters"] == [{"name": "MaxW", "value": 0}, {"name": "MinW", "value": 0}], commands
    assert client.sent_segments[0]["action"] == ACTION_HOLD
    # Holding is now the plan, so it is not re-sent either.
    client.advance(minutes=1)
    ok, calls = apply(client)
    assert ok and calls == []


def test_fronius_export_window_becomes_discharge_battery():
    """An active export window is an exact-rate DischargeBattery."""
    client = controlled_client(soc=80)
    set_window(client, "export", "13:00:00", "13:30:00", soc=20, power=2500, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    commands = body(calls[0])
    assert commands == [
        {
            "dispatchType": "DischargeBattery",
            "dispatchDateTime": "2026-06-15T12:00:00Z",
            "dispatchDuration": 1800,
            "dispatchParameters": [{"name": "MaxW", "value": 2500}, {"name": "MinW", "value": 2500}],
            "dispatchPayload": commands[0]["dispatchPayload"],
        }
    ], commands


def test_fronius_export_stops_at_its_target():
    """At or below the export target nothing is forced out; with nothing sent, nothing is called."""
    client = controlled_client(soc=20)
    set_window(client, "export", "13:00:00", "13:30:00", soc=20, power=2500, enable=True)
    ok, calls = apply(client)
    assert ok and calls == [], calls


def test_fronius_freeze_charge_holds_the_battery():
    """A zero discharge rate outside any window is Predbat's hold: ChargeBattery MaxW=0 until the horizon."""
    client = controlled_client()
    set_window(client, "export", power=0)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    commands = body(calls[0])
    assert len(commands) == 1 and commands[0]["dispatchType"] == "ChargeBattery", commands
    assert commands[0]["dispatchParameters"] == [{"name": "MaxW", "value": 0}, {"name": "MinW", "value": 0}]
    assert commands[0]["dispatchDuration"] == 3600


def test_fronius_freeze_export_blocks_charging_only():
    """A zero charge rate is freeze export: DischargeBattery MinW=0 so load is covered but nothing is forced out."""
    client = controlled_client()
    set_window(client, "charge", power=0)
    set_window(client, "export", power=4000)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    commands = body(calls[0])
    assert commands[0]["dispatchType"] == "DischargeBattery", commands
    assert commands[0]["dispatchParameters"] == [{"name": "MaxW", "value": 4000}, {"name": "MinW", "value": 0}], commands


def test_fronius_upcoming_window_is_sent_ahead():
    """A window starting inside the look-ahead goes out now, dated, so delivery latency is covered."""
    client = controlled_client()
    set_window(client, "charge", "13:20:00", "14:00:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    commands = body(calls[0])
    assert commands[0]["dispatchDateTime"] == "2026-06-15T12:20:00Z" and commands[0]["dispatchDuration"] == 2400, commands


def test_fronius_idle_cancels_the_previous_schedule():
    """When the plan goes idle the last schedule is cancelled by its dispatch id."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    apply(client, FakeResponse(200, ACCEPTED))
    set_window(client, "charge")
    client.advance(minutes=2)
    ok, calls = apply(client, FakeResponse(204))
    assert ok and len(calls) == 1 and calls[0]["method"] == "DELETE", calls
    assert calls[0]["url"].endswith("/schedules/{}".format(ACCEPTED["dispatchId"]))
    assert client.sent_segments == [] and client.dispatch_id is None
    # A cancel that finds every command already finished has still done its job.
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    client.advance(minutes=2)
    apply(client, FakeResponse(200, ACCEPTED))
    set_window(client, "charge")
    client.advance(minutes=2)
    ok, calls = apply(client, FakeResponse(400, {"responseError": 10306, "responseMessage": "Unable to cancel schedule, all commands are already in an end-state."}))
    assert ok and client.sent_segments == [], calls


def test_fronius_idle_with_nothing_sent_makes_no_call():
    """Self-consumption is the absence of a command, so an idle plan with nothing outstanding is free."""
    client = controlled_client()
    ok, calls = apply(client)
    assert ok and calls == []


def test_fronius_read_only_and_disabled_never_write():
    """Read-only mode and fronius_control_enable false both stop every write."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    client.state["switch.predbat_set_read_only"] = "on"
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert not ok and calls == []
    disabled = controlled_client(control_enable=False)
    set_window(disabled, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    ok, calls = apply(disabled, FakeResponse(200, ACCEPTED))
    assert not ok and calls == []


def test_fronius_reconcile_waits_for_predbat_to_drive():
    """The per-tick reconcile does nothing until Predbat has written a control entity or pressed write."""
    client = controlled_client()
    client.control_active = False
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    client.get_schedule_settings_ha()
    transport, patcher = http(FakeResponse(200, ACCEPTED), FakeResponse(200, ACCEPTED))
    with patcher:
        run_async(client._reconcile_control())
        assert transport.calls == []
        run_async(client.switch_event(entity("switch", "battery_schedule_charge_write"), "turn_on"))
    assert client.control_active and len(transport.calls) == 1
    # A rate write alone (Predbat's freeze/hold signal, which comes without a button press) also arms it.
    held = controlled_client()
    held.control_active = False
    run_async(held.number_event(entity("number", "battery_schedule_export_power"), "0"))
    assert held.control_active
    transport, patcher = http(FakeResponse(200, ACCEPTED))
    with patcher:
        run_async(held._reconcile_control())
    assert len(transport.calls) == 1 and transport.calls[0]["json"][0]["dispatchParameters"] == [{"name": "MaxW", "value": 0}, {"name": "MinW", "value": 0}]


def test_fronius_writes_are_paced():
    """Two different schedules within a minute: the second is held, then sent on a later tick."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    apply(client, FakeResponse(200, ACCEPTED))
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=2000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert not ok and calls == [] and logged(client, "held")
    client.advance(seconds=61)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert ok and len(calls) == 1 and body(calls[0])[0]["dispatchParameters"][0]["value"] == 2000


def test_fronius_parameter_shape_falls_back_once():
    """A malformed-request rejection retries once with object-keyed parameters, which then sticks."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(400, {"responseError": 10303, "responseMessage": "Unexpected parameters for dispatch type."}), FakeResponse(200, ACCEPTED))
    assert ok and len(calls) == 2, calls
    assert isinstance(calls[0]["json"][0]["dispatchParameters"], list)
    assert calls[1]["json"][0]["dispatchParameters"] == {"MaxW": 3000, "MinW": 3000}
    assert client.params_shape == "object" and client.params_shape_confirmed
    # Once a shape has been accepted there is no second guess on a later failure.
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=2000, enable=True)
    client.advance(minutes=2)
    ok, calls = apply(client, FakeResponse(400, {"responseError": 10303}))
    assert not ok and len(calls) == 1


def test_fronius_unsupported_system_stops_control():
    """A system the API cannot battery-control is reported once and not written to again."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(422, {"responseError": 10406, "responseMessage": "PV System has no battery."}))
    assert not ok and len(calls) == 1
    assert client.control_unsupported and logged(client, "keep monitoring")
    client.advance(minutes=5)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert not ok and calls == []
    assert client.api_status() == "control_unsupported"


def test_fronius_power_is_clamped_to_the_battery_limit():
    """A rate above the battery's nominal maximum is clamped rather than rejected by the API."""
    client = controlled_client()
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=9000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert body(calls[0])[0]["dispatchParameters"] == [{"name": "MaxW", "value": 5632}, {"name": "MinW", "value": 5632}]


def test_fronius_grid_export_limit_fills_idle_periods():
    """The opt-in export limit is applied to idle periods only, after the battery commands."""
    client = controlled_client(grid_export_limit=3680)
    set_window(client, "charge", "13:00:00", "13:30:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    commands = body(calls[0])
    assert [command["dispatchType"] for command in commands] == ["ChargeBattery", "SetGridExportLimit"], commands
    assert commands[1]["dispatchDateTime"] == "2026-06-15T12:30:00Z"
    assert commands[1]["dispatchParameters"] == [{"name": "ExportLimitW", "value": 3680}]


def test_fronius_vienna_window_is_sent_in_utc():
    """A Vienna summer window is converted from CEST to Zulu."""
    client = controlled_client(tz="Europe/Vienna")
    set_window(client, "charge", "14:00:00", "15:00:00", soc=90, power=3000, enable=True)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert body(calls[0])[0]["dispatchDateTime"] == "2026-06-15T12:00:00Z"


def test_fronius_dst_day_window_has_its_real_length():
    """On the London spring-forward night 00:30-04:30 local is three real hours, sent from 00:30Z."""
    client = controlled_client(now=datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc))
    set_window(client, "charge", "00:30:00", "04:30:00", soc=90, power=3000, enable=True)
    client.get_schedule_settings_ha()
    segments, start, end = client.build_segments(client.utc_now())
    charge = [segment for segment in segments if segment["action"] == ACTION_CHARGE]
    assert charge and charge[0]["start"] == datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc), segments
    # Thirty minutes ahead is outside the look-ahead; a minute later it is inside and goes out.
    ok, calls = apply(client)
    assert ok and calls == [], calls
    client.advance(minutes=1)
    ok, calls = apply(client, FakeResponse(200, ACCEPTED))
    assert body(calls[0])[0]["dispatchDateTime"] == "2026-03-29T00:30:00Z"


def test_fronius_restart_does_not_resend_the_same_schedule():
    """The sent schedule is persisted, so a restart with the same plan makes no call."""
    storage = FakeStorage()
    client = controlled_client(storage=storage)
    set_window(client, "charge", "13:00:00", "14:00:00", soc=90, power=3000, enable=True)
    apply(client, FakeResponse(200, ACCEPTED))
    assert (FRONIUS_STORAGE_MODULE, FRONIUS_CACHE_CONTROL) in storage.data
    restarted = controlled_client(storage=storage)
    restarted.state = dict(client.state)
    run_async(restarted.restore_state())
    restarted.advance(minutes=3)
    ok, calls = apply(restarted)
    assert ok and calls == [], calls
    assert restarted.dispatch_id == ACCEPTED["dispatchId"]


def test_fronius_control_events_update_the_local_schedule():
    """Entity events for this system update the schedule; another system's entities are ignored."""
    client = controlled_client()
    run_async(client.number_event(entity("number", "battery_schedule_charge_power"), "2500"))
    run_async(client.select_event(entity("select", "battery_schedule_charge_start_time"), "02:00:00"))
    run_async(client.switch_event(entity("switch", "battery_schedule_charge_enable"), "turn_on"))
    charge = client.local_schedule["charge"]
    assert charge["power"] == 2500 and charge["start"] == "02:00:00" and charge["enable"] is True, charge
    run_async(client.number_event("number.predbat_fronius_ffffffff_battery_schedule_charge_power", "100"))
    assert client.local_schedule["charge"]["power"] == 2500


def test_fronius_missing_rate_entity_is_not_a_hold():
    """Before Predbat has written a rate, an absent power entity must not read as a zero (a hold)."""
    client = controlled_client()
    for direction in ("charge", "export"):
        client.state.pop(entity("number", "battery_schedule_{}_power".format(direction)))
    ok, calls = apply(client)
    assert ok and calls == [], calls


def run_fronius_control_tests(my_predbat):
    """Run all Fronius control-logic tests."""
    failed = False
    for name, fn in [
        ("charge_window", test_fronius_charge_window_becomes_charge_battery),
        ("unchanged_not_resent", test_fronius_unchanged_schedule_is_not_resent),
        ("target_reached_hold", test_fronius_target_reached_switches_to_hold),
        ("export_window", test_fronius_export_window_becomes_discharge_battery),
        ("export_target", test_fronius_export_stops_at_its_target),
        ("freeze_charge", test_fronius_freeze_charge_holds_the_battery),
        ("freeze_export", test_fronius_freeze_export_blocks_charging_only),
        ("upcoming_window", test_fronius_upcoming_window_is_sent_ahead),
        ("idle_cancels", test_fronius_idle_cancels_the_previous_schedule),
        ("idle_no_call", test_fronius_idle_with_nothing_sent_makes_no_call),
        ("read_only_disabled", test_fronius_read_only_and_disabled_never_write),
        ("reconcile_gating", test_fronius_reconcile_waits_for_predbat_to_drive),
        ("write_pacing", test_fronius_writes_are_paced),
        ("params_shape_fallback", test_fronius_parameter_shape_falls_back_once),
        ("unsupported_system", test_fronius_unsupported_system_stops_control),
        ("power_clamp", test_fronius_power_is_clamped_to_the_battery_limit),
        ("grid_export_limit", test_fronius_grid_export_limit_fills_idle_periods),
        ("vienna_utc", test_fronius_vienna_window_is_sent_in_utc),
        ("dst_window", test_fronius_dst_day_window_has_its_real_length),
        ("restart_no_resend", test_fronius_restart_does_not_resend_the_same_schedule),
        ("control_events", test_fronius_control_events_update_the_local_schedule),
        ("missing_rate_not_hold", test_fronius_missing_rate_entity_is_not_a_hold),
    ]:
        try:
            if fn():
                print(f"  FAILED: fronius_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in fronius_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

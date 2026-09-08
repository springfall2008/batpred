# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS control derivation and write path
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS control entities, payload derivation and write gating."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from unittest.mock import patch
from tests.test_alphaess_api import MockAlphaESS, _envelope
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session
from alphaess_const import ALPHAESS_SETTLE_POLLS, ALPHAESS_WRITE_SETTLE_SECONDS, ALPHAESS_WRITE_BURST_MAX


def _schedule(reserve=10, charge=None, export=None, charge_power=3000, export_power=3000):
    """Build a schedule dict in the shape the control entities produce.

    A DISABLED window still carries a power, because the real control entities do:
    adjust_charge_rate/adjust_discharge_rate write Predbat's rates every cycle whether or
    not a window is enabled. That matters because a ZERO rate is precisely how Predbat
    signals freeze on this inverter (there is no pause endpoint), so a fixture leaving
    both at zero would silently be testing a freeze instead of the demand state it reads as.
    """
    idle_charge = {"enable": False, "soc": 0, "power": charge_power, "start": "00:00:00", "end": "00:00:00"}
    idle_export = {"enable": False, "soc": 0, "power": export_power, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": charge or idle_charge, "export": export or idle_export}


def _client(sn="AL70"):
    """Build a client with one discovered inverter ready for control tests."""
    client = MockAlphaESS()
    # 0 rather than the real default (2s): api_delay sleeps between the charge and discharge
    # writes on every apply_settings call, and the control-write tests do not exercise that
    # pacing - it just added ~2s per apply across the whole suite for nothing.
    client.api_delay = 0
    client.device_list = [sn]
    client.device_detail = {sn: {"sysSn": sn, "cobat": 13.34, "poinv": 5.0, "popv": 9.0, "minv": "SMILE5-INV"}}
    client.device_values = {sn: {"soc": 50.0}}
    client.device_config[sn] = {
        "charge": {"gridCharge": 0, "timeChaf1": "00:00", "timeChae1": "00:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 100},
        "discharge": {"ctrDis": 0, "timeDisf1": "00:00", "timeDise1": "00:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 10},
    }
    return client


def test_alphaess_control_entities_round_trip():
    """Published control entities read back into exactly the schedule shape they came from."""
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule(
        reserve=12,
        charge={"enable": True, "soc": 90, "power": 2500, "start": "01:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 1800, "start": "14:30:00", "end": "16:45:00"},
    )
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes Predbat
    # replace these entities with its own dummies and the window never arrives.
    charge_start = client.published.get("select.predbat_alphaess_al70_battery_schedule_charge_start_time", {}).get("state")
    if charge_start != "01:00:00":
        print(f"ERROR: published charge start {charge_start}")
        failed = True
    export_end = client.published.get("select.predbat_alphaess_al70_battery_schedule_export_end_time", {}).get("state")
    if export_end != "16:45:00":
        print(f"ERROR: published export end {export_end}")
        failed = True
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["charge"]["enable"] is not True or read_back["charge"]["soc"] != 90 or read_back["reserve"] != 12:
        print(f"ERROR: charge read back {read_back}")
        failed = True
    if read_back["charge"]["power"] != 2500 or read_back["charge"]["end"] != "05:00:00":
        print(f"ERROR: charge power/end read back {read_back['charge']}")
        failed = True
    if read_back["export"]["enable"] is not True or read_back["export"]["soc"] != 20 or read_back["export"]["power"] != 1800:
        print(f"ERROR: export read back {read_back['export']}")
        failed = True
    if read_back["export"]["start"] != "14:30:00" or read_back["export"]["end"] != "16:45:00":
        print(f"ERROR: export times read back {read_back['export']}")
        failed = True
    assert not failed, "test_alphaess_control_entities_round_trip"


def test_alphaess_reserve_entity_is_published_unclamped():
    """Predbat writes then reads back to confirm (write_and_poll_value).

    Publishing anything other than what was written guarantees a mismatch and a retry
    storm; the floor is enforced at the API boundary in the payload builder instead.
    """
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule(reserve=3)
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    state = client.published.get("number.predbat_alphaess_al70_battery_schedule_reserve", {}).get("state")
    if state != 3:
        print(f"ERROR: reserve published as {state}, should echo the written 3 unclamped")
        failed = True
    assert not failed, "test_alphaess_reserve_entity_is_published_unclamped"


def test_alphaess_entity_routing_does_not_confuse_prefixed_serials():
    """An entity for AL701 must never route to AL70 - that would write to the wrong inverter."""
    failed = False
    client = _client()
    client.device_list = ["AL70", "AL701"]
    if client._sn_from_entity("number.predbat_alphaess_al701_battery_schedule_reserve") != "AL701":
        print("ERROR: AL701 entity routed to the wrong serial")
        failed = True
    if client._sn_from_entity("number.predbat_alphaess_al70_battery_schedule_reserve") != "AL70":
        print("ERROR: AL70 entity misrouted")
        failed = True
    if client._sn_from_entity("number.predbat_alphaess_zz99_battery_schedule_reserve") is not None:
        print("ERROR: unknown serial should not resolve")
        failed = True
    assert not failed, "test_alphaess_entity_routing_does_not_confuse_prefixed_serials"


def test_alphaess_unbind_fallback_routing_still_respects_prefix_collision():
    """_sn_from_entity falls back to _unbind_done once a serial has left device_list, so
    its own unbind switch keeps resolving after a successful unbind. That fallback must
    not regress the AL701/AL70 collision guard.

    Deliberately uses a single-element _unbind_done ({"AL70"} only) rather than both
    serials together: with both present, a missing anchor only misroutes when AL70 happens
    to be visited before AL701 in set iteration, which is hash-randomised per process and
    so would make this test pass or fail at random rather than reliably catching the bug.
    With only AL70 present there is no ordering to depend on - AL701's entity resolving to
    anything at all can only happen via the (missing) anchor.
    """
    failed = False
    client = _client()
    client.device_list = []
    client._unbind_done = {"AL70"}
    # AL701 was never unbound - only AL70 was. AL701's own entity must not resolve to AL70
    # just because "AL70" is a string prefix of "AL701".
    if client._sn_from_entity("switch.predbat_alphaess_al701_unbind", include_unbound=True) is not None:
        print("ERROR: AL701's entity incorrectly resolved via the AL70 unbind fallback")
        failed = True
    if client._sn_from_entity("switch.predbat_alphaess_al70_unbind", include_unbound=True) != "AL70":
        print("ERROR: AL70's own unbind entity failed to resolve via the fallback")
        failed = True
    assert not failed, "test_alphaess_unbind_fallback_routing_still_respects_prefix_collision"


def test_alphaess_unbound_serial_does_not_resolve_for_non_unbind_entities():
    """The _unbind_done fallback in _sn_from_entity must be scoped to the unbind switch
    only.

    _sn_from_entity is consulted BEFORE the _unbind suffix check inside
    _handle_control_event, so an unscoped fallback would resolve ANY entity type for an
    already-unbound serial - not just its own unbind switch. Reproduced live by a
    reviewer: with device_list=[] and _unbind_done={"AL70"}, toggling the write button
    resolved sn="AL70", added it to control_active, and called apply_schedule("AL70") -
    a live control write (repeated every cycle by _reconcile_control) to an account the
    code's own docstring says Predbat "can no longer read or control".

    Drives switch_event, the real routing entry point, for a non-unbind entity while the
    serial is already unbound and absent from device_list - pinning exactly that
    scenario - then confirms the unbind switch itself still works for the same serial.
    """
    failed = False
    client = _client()
    client.device_list = []
    client._unbind_done = {"AL70"}
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession") as mock_session_cls:
        run_async_local(client.switch_event("switch.predbat_alphaess_al70_battery_schedule_charge_write", "turn_on"))
    if mock_session_cls.called:
        print("ERROR: an unbound serial's write button issued an API call")
        failed = True
    if "AL70" in client.control_active:
        print(f"ERROR: an unbound serial was marked control_active: {client.control_active}")
        failed = True
    # The unbind switch itself, for the same serial, must still resolve and still work.
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client.switch_event("switch.predbat_alphaess_al70_unbind", "turn_off"))
    if "AL70" in client._unbind_done:
        print(f"ERROR: the unbind switch itself failed to resolve/clear the latch: {client._unbind_done}")
        failed = True
    assert not failed, "test_alphaess_unbound_serial_does_not_resolve_for_non_unbind_entities"


def test_alphaess_update_local_schedule_applies_each_field():
    """Each control entity change lands on the right field of the held schedule."""
    failed = False
    client = _client()
    for entity, value, _path in [
        ("number.predbat_alphaess_al70_battery_schedule_reserve", 15, ("reserve",)),
        ("select.predbat_alphaess_al70_battery_schedule_charge_start_time", "02:30:00", ("charge", "start")),
        ("number.predbat_alphaess_al70_battery_schedule_charge_soc", 85, ("charge", "soc")),
        ("number.predbat_alphaess_al70_battery_schedule_export_power", 4000, ("export", "power")),
        ("switch.predbat_alphaess_al70_battery_schedule_export_enable", "turn_on", ("export", "enable")),
    ]:
        client.update_local_schedule("AL70", entity, value)
    schedule = client.local_schedule["AL70"]
    if schedule["reserve"] != 15 or schedule["charge"]["start"] != "02:30:00" or schedule["charge"]["soc"] != 85:
        print(f"ERROR: schedule {schedule}")
        failed = True
    if schedule["export"]["power"] != 4000 or schedule["export"]["enable"] is not True:
        print(f"ERROR: export {schedule['export']}")
        failed = True
    assert not failed, "test_alphaess_update_local_schedule_applies_each_field"


def test_alphaess_fallback_on_unknown_or_unavailable_values():
    """Control entities reporting unknown/unavailable fall back to safe defaults.

    Home Assistant returns "unknown" or "unavailable" right after a restart, before Predbat
    republishes - get_schedule_settings_ha must route numeric casts through _as_float to fall
    back to 0 rather than raising and killing the control loop. Time selects have no _as_float
    wrapper but fall back to their default when the entity doesn't exist.
    """
    failed = False
    client = _client()
    # Set up initial schedule with known values
    client.local_schedule["AL70"] = _schedule(reserve=10)
    run_async_local(client.publish_schedule_settings_ha("AL70"))

    # Now simulate unknown/unavailable states for various numeric entities.
    # Numeric casts route through _as_float, which converts non-numeric values to the default.
    # reserve set to "unknown" → _as_float converts it to 0
    client.state["number.predbat_alphaess_al70_battery_schedule_reserve"] = "unknown"
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["reserve"] != 0:
        print(f"ERROR: reserve 'unknown' did not fall back to 0, got {read_back['reserve']}")
        failed = True

    # soc entity set to "unavailable" → _as_float converts it to 0
    client.state["number.predbat_alphaess_al70_battery_schedule_charge_soc"] = "unavailable"
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["charge"]["soc"] != 0:
        print(f"ERROR: charge soc 'unavailable' did not fall back to 0, got {read_back['charge']['soc']}")
        failed = True

    # power entity set to "unknown" → _as_float converts it to 0
    client.state["number.predbat_alphaess_al70_battery_schedule_export_power"] = "unknown"
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["export"]["power"] != 0:
        print(f"ERROR: export power 'unknown' did not fall back to 0, got {read_back['export']['power']}")
        failed = True

    # time selects have no _as_float wrapper, so they fall back to default only when the
    # entity doesn't exist at all (not when it's "unknown"). Verify that missing entities
    # fall back to "00:00:00".
    # (Don't set the entity at all - it falls back to the default)
    if "select.predbat_alphaess_al70_battery_schedule_charge_end_time" in client.state:
        del client.state["select.predbat_alphaess_al70_battery_schedule_charge_end_time"]
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["charge"]["end"] != "00:00:00":
        print(f"ERROR: charge end (missing entity) did not fall back to '00:00:00', got {read_back['charge']['end']}")
        failed = True

    assert not failed, "test_alphaess_fallback_on_unknown_or_unavailable_values"


def test_alphaess_controls_pass_straight_through():
    """Predbat's controls map onto the schedule fields verbatim; the inverter does timing.

    execute.py:514 already gates how far ahead a window is programmed
    ((minutes_start - minutes_now) <= set_window_minutes), so Predbat never hands the
    component a window hours in advance. That is why the naive pass-through is safe and no
    window-blanking state machine is needed.
    """
    failed = False
    client = _client()
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "17:00:00", "end": "19:00:00"},
    )
    charge = client.build_charge_payload("AL70", schedule)
    discharge = client.build_discharge_payload("AL70", schedule)
    for key, expect in (("sysSn", "AL70"), ("gridCharge", 1), ("timeChaf1", "01:00"), ("timeChae1", "05:00"), ("batHighCap", 90)):
        if charge.get(key) != expect:
            print(f"ERROR: charge[{key}] = {charge.get(key)} != {expect}")
            failed = True
    for key, expect in (("sysSn", "AL70"), ("ctrDis", 1), ("timeDisf1", "17:00"), ("timeDise1", "19:00")):
        if discharge.get(key) != expect:
            print(f"ERROR: discharge[{key}] = {discharge.get(key)} != {expect}")
            failed = True
    # batUseCap is the EXPORT TARGET while an export window is programmed.
    if discharge.get("batUseCap") != 20:
        print(f"ERROR: batUseCap {discharge.get('batUseCap')} should be the export target 20")
        failed = True
    # Period 2 is the midnight split, not a state - disabled when no split occurred.
    for key in ("timeChaf2", "timeChae2"):
        if charge.get(key) != "00:00":
            print(f"ERROR: {key} = {charge.get(key)} should be disabled")
            failed = True
    assert not failed, "test_alphaess_controls_pass_straight_through"


def test_alphaess_batusecap_is_the_reserve_outside_an_export_window():
    """batUseCap serves two Predbat concepts because the API has one field for the floor."""
    failed = False
    client = _client()
    schedule = _schedule(reserve=25, charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    discharge = client.build_discharge_payload("AL70", schedule)
    if discharge.get("batUseCap") != 25:
        print(f"ERROR: batUseCap {discharge.get('batUseCap')} should be the reserve 25")
        failed = True
    # With no export window and a non-zero discharge rate, discharge time control is OFF -
    # that is demand mode, the battery covers the house normally.
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should be 0 in demand mode")
        failed = True
    assert not failed, "test_alphaess_batusecap_is_the_reserve_outside_an_export_window"


def test_alphaess_rate_zero_is_freeze():
    """Zero charge power disables charging; zero discharge power requests a hold.

    Live AlphaESS hardware ignores discharge scheduling enabled with no periods, so the
    adapter must translate the generic zero-discharge signal into a fixed 10% charge
    profile instead.
    """
    failed = False
    client = _client()

    # charge_rate == 0 -> no grid charging, overriding the charge window (freeze charge,
    # and no cross-charging during an export).
    frozen_charge = _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 0, "start": "01:00:00", "end": "05:00:00"})
    charge = client.build_charge_payload("AL70", frozen_charge)
    if charge.get("gridCharge") != 0:
        print(f"ERROR: gridCharge {charge.get('gridCharge')} should be 0 when charge_rate is 0")
        failed = True

    # discharge_rate == 0 -> a valid daily charge profile at a fixed 10%, with
    # ordinary demand-mode discharge settings. The charge profile is the hardware hold.
    frozen_export = _schedule(reserve=10, export_power=0)
    hold_charge = client.build_charge_payload("AL70", frozen_export)
    discharge = client.build_discharge_payload("AL70", frozen_export)
    if hold_charge.get("gridCharge") != 1 or hold_charge.get("batHighCap") != 10:
        print(f"ERROR: zero-discharge hold did not create a 10% charge profile: {hold_charge}")
        failed = True
    if hold_charge.get("timeChaf1") != "00:00" or hold_charge.get("timeChae1") != "23:45":
        print(f"ERROR: hold profile should cover the stable daily period, got {hold_charge}")
        failed = True
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: discharge time control {discharge.get('ctrDis')} should be off while the charge profile holds")
        failed = True
    assert not failed, "test_alphaess_rate_zero_is_freeze"


def test_alphaess_both_rates_zero_still_holds_the_battery():
    """Both rates at zero is an ordinary, reachable hold - not evidence of "no plan".

    Predbat reaches this combination through set_freeze_export_during_demand zeroing the
    charge rate (execute.py:532/538 - AlphaESSCloud has has_timed_pause False, so the "else"
    branch fires) together with a car-charging-from-battery-disable (execute.py:564) or
    iboost_prevent_discharge (execute.py:591) hold zeroing the discharge rate in the same
    pass. The hold is realised as a fixed 10% charge profile, not the empty discharge
    schedule AlphaESS ignores. Stranding a genuinely unconfigured system is prevented
    elsewhere by the control_active gate in _reconcile_control, which only re-applies once
    a serial's write button has been pressed.
    """
    failed = False
    client = _client()
    schedule = _schedule(reserve=15, charge_power=0, export_power=0)
    charge = client.build_charge_payload("AL70", schedule)
    discharge = client.build_discharge_payload("AL70", schedule)
    if charge.get("gridCharge") != 1 or charge.get("batHighCap") != 10:
        print(f"ERROR: both-zero hold did not create a 10% charge profile: {charge}")
        failed = True
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: both-zero hold left discharge time control enabled: {discharge}")
        failed = True
    assert not failed, "test_alphaess_both_rates_zero_still_holds_the_battery"


def test_alphaess_disabled_planned_hold_recovers_the_charge_window():
    """Generic Hold charging disables the switch but leaves target and times intact.

    execute.py restores normal/max charge power after disabling the window, so zero power
    is not a production-realistic hold signature. Recover the active target-reached window
    with its normal power still present instead of returning AlphaESS to demand mode.
    """
    failed = False
    client = _client()
    client.base.minutes_now = 2 * 60
    schedule = _schedule(reserve=41, charge={"enable": False, "soc": 40, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    charge = client.build_charge_payload("AL70", schedule)
    discharge = client.build_discharge_payload("AL70", schedule)
    if charge.get("gridCharge") != 1 or charge.get("batHighCap") != 10:
        print(f"ERROR: planned hold was not converted to the fixed 10% profile: {charge}")
        failed = True
    if charge.get("timeChaf1") != "01:00" or charge.get("timeChae1") != "05:00":
        print(f"ERROR: recovered hold did not retain its planned times: {charge}")
        failed = True
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: recovered hold should leave discharge time control off: {discharge}")
        failed = True
    assert not failed, "test_alphaess_disabled_planned_hold_recovers_the_charge_window"


def test_alphaess_planned_hold_releases_when_its_window_ends():
    """Normal restored power must not release the hold early, but window end must.

    AlphaESS does not discharge down towards a reached grid-charge target while that period
    remains enabled. The retained target/time entities therefore recover the hold until the
    planned window ends; the active-time check prevents stale fields latching it afterwards.
    """
    failed = False
    client = _client()
    client.base.minutes_now = 2 * 60
    schedule = _schedule(reserve=41, charge={"enable": False, "soc": 40, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    first = client.build_charge_payload("AL70", schedule)
    client.base.minutes_now = 6 * 60
    second = client.build_charge_payload("AL70", schedule)
    if first.get("gridCharge") != 1:
        print(f"ERROR: test setup did not enter the recovered hold: {first}")
        failed = True
    if second.get("gridCharge") != 0:
        print(f"ERROR: ended planned window did not release stale hold fields: {second}")
        failed = True
    assert not failed, "test_alphaess_planned_hold_releases_when_its_window_ends"


def test_alphaess_hold_profile_is_constant_while_soc_changes():
    """The fixed hold target must not follow live SOC or inverter rating."""
    failed = False
    client = _client()
    schedule = _schedule(reserve=15, export_power=0)
    first = client.build_charge_payload("AL70", schedule)
    client.device_values["AL70"]["soc"] = 55.0
    second = client.build_charge_payload("AL70", schedule)
    if first.get("batHighCap") != 10 or second.get("batHighCap") != 10:
        print(f"ERROR: hold target changed with SOC: first={first} second={second}")
        failed = True
    client.build_charge_payload("AL70", _schedule(reserve=15))
    third = client.build_charge_payload("AL70", schedule)
    if third.get("batHighCap") != 10:
        print(f"ERROR: a new hold did not use the fixed target: {third}")
        failed = True
    assert not failed, "test_alphaess_hold_profile_is_constant_while_soc_changes"


def test_alphaess_real_charge_survives_a_simultaneous_discharge_hold():
    """An EV/iBoost hold during real grid charging must not lower the charge target."""
    failed = False
    client = _client()
    client.base.minutes_now = 2 * 60
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"}, export_power=0)
    charge = client.build_charge_payload("AL70", schedule)
    discharge = client.build_discharge_payload("AL70", schedule)
    if charge.get("gridCharge") != 1 or charge.get("batHighCap") != 90:
        print(f"ERROR: real charge was replaced by a hold profile: {charge}")
        failed = True
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: real charge plus hold should leave discharge time control off: {discharge}")
        failed = True
    assert not failed, "test_alphaess_real_charge_survives_a_simultaneous_discharge_hold"


def test_alphaess_future_charge_does_not_satisfy_a_current_discharge_hold():
    """A configured charge window prevents discharge only while it is active.

    Predbat can publish tonight's grid-charge schedule before it starts. Returning that
    future profile for an EV/iBoost hold now would set ctrDis to ordinary demand mode while
    no active charge schedule stops the battery feeding the load.
    """
    failed = False
    client = _client()
    client.base.minutes_now = 22 * 60
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 3000, "start": "23:00:00", "end": "23:45:00"}, export_power=0)
    charge = client.build_charge_payload("AL70", schedule)
    discharge = client.build_discharge_payload("AL70", schedule)
    if charge.get("batHighCap") != 10 or charge.get("timeChaf1") != "00:00" or charge.get("timeChae1") != "23:45":
        print(f"ERROR: future real charge replaced the current full-day hold: {charge}")
        failed = True
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: current hold should leave discharge time control off: {discharge}")
        failed = True
    assert not failed, "test_alphaess_future_charge_does_not_satisfy_a_current_discharge_hold"


def test_alphaess_times_snap_inward_to_the_15_minute_grid():
    """Off-grid values are accepted by the API and silently ignored by the device."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:07:00", "end": "05:53:00"})
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("timeChaf1") != "01:15" or charge.get("timeChae1") != "05:45":
        print(f"ERROR: snapped window {charge.get('timeChaf1')}-{charge.get('timeChae1')} should be 01:15-05:45")
        failed = True
    assert not failed, "test_alphaess_times_snap_inward_to_the_15_minute_grid"


def test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped():
    """An inverted window must NOT be written as a wrap-around - wrap behaviour is
    undocumented, so it is disabled instead and the decision is logged."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:05:00", "end": "01:10:00"})
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("gridCharge") != 0 or charge.get("timeChaf1") != "00:00" or charge.get("timeChae1") != "00:00":
        print(f"ERROR: collapsed window not disabled: {charge}")
        failed = True
    if not any("collaps" in message.lower() or "too short" in message.lower() for message in client.log_messages):
        print(f"ERROR: no log for the collapsed window, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped"


def test_alphaess_midnight_end_snaps_to_the_maximum():
    """23:45 is the documented maximum, so Predbat's midnight end lands there."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "22:00:00", "end": "24:00:00"})
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("timeChae1") != "23:45":
        print(f"ERROR: 24:00 end snapped to {charge.get('timeChae1')} != 23:45")
        failed = True
    assert not failed, "test_alphaess_midnight_end_snaps_to_the_maximum"


def test_alphaess_period_two_carries_the_midnight_split():
    """can_span_midnight is False, so Predbat splits and period 2 takes the remainder."""
    failed = False
    client = _client()
    (start1, end1), (start2, end2) = client.split_window("23:00:00", "26:00:00")
    if (start1, end1) != ("23:00", "23:45"):
        print(f"ERROR: period 1 {start1}-{end1}")
        failed = True
    if (start2, end2) != ("00:00", "02:00"):
        print(f"ERROR: period 2 {start2}-{end2}")
        failed = True
    assert not failed, "test_alphaess_period_two_carries_the_midnight_split"


def test_alphaess_period_one_collapse_does_not_disable_period_two():
    """A collapsed period 1 must not throw away a valid period 2.

    A genuine midnight-crossing window (23:50 today -> 02:00 tomorrow) snaps period 1 - whose
    raw start (23:50) is already past the API's 23:45 maximum - down to an empty
    23:45-23:45, while period 2 (00:00-02:00) survives untouched. Disabling the whole
    payload because period 1 alone collapsed would silently stop a real overnight
    charge/export window (a missed night charge, or a missed peak-rate export); only both
    periods collapsing together should disable it.
    """
    failed = False
    client = _client()
    schedule = _schedule(
        charge={"enable": True, "soc": 90, "power": 3000, "start": "23:50:00", "end": "26:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "23:50:00", "end": "26:00:00"},
    )
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("gridCharge") != 1:
        print(f"ERROR: gridCharge {charge.get('gridCharge')} should still be 1 - period 2 is a valid window")
        failed = True
    if charge.get("timeChaf1") != "00:00" or charge.get("timeChae1") != "00:00":
        print(f"ERROR: collapsed period 1 {charge.get('timeChaf1')}-{charge.get('timeChae1')} should be disabled")
        failed = True
    if charge.get("timeChaf2") != "00:00" or charge.get("timeChae2") != "02:00":
        print(f"ERROR: period 2 {charge.get('timeChaf2')}-{charge.get('timeChae2')} should carry the remainder 00:00-02:00")
        failed = True

    discharge = client.build_discharge_payload("AL70", schedule)
    if discharge.get("ctrDis") != 1:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should still be 1 - period 2 is a valid window")
        failed = True
    if discharge.get("timeDisf1") != "00:00" or discharge.get("timeDise1") != "00:00":
        print(f"ERROR: collapsed period 1 {discharge.get('timeDisf1')}-{discharge.get('timeDise1')} should be disabled")
        failed = True
    if discharge.get("timeDisf2") != "00:00" or discharge.get("timeDise2") != "02:00":
        print(f"ERROR: period 2 {discharge.get('timeDisf2')}-{discharge.get('timeDise2')} should carry the remainder 00:00-02:00")
        failed = True
    assert not failed, "test_alphaess_period_one_collapse_does_not_disable_period_two"


def test_alphaess_reserve_is_clamped_at_the_api_boundary():
    """The entity is published unclamped; the payload is where the API's limits apply.

    Asserts the EXACT clamped value against genuinely out-of-range input on both sides - a
    loose `0 <= x <= 100` range check passes for almost any implementation, including one
    whose floor is wrong (for example clamping negative input to 5 instead of 0).
    """
    failed = False
    client = _client()
    low = client.build_discharge_payload("AL70", _schedule(reserve=-20))
    if low.get("batUseCap") != 0:
        print(f"ERROR: batUseCap {low.get('batUseCap')} should clamp -20 down to exactly 0")
        failed = True
    high = client.build_charge_payload("AL70", _schedule(charge={"enable": True, "soc": 250, "power": 3000, "start": "01:00:00", "end": "05:00:00"}))
    if high.get("batHighCap") != 100:
        print(f"ERROR: batHighCap {high.get('batHighCap')} should clamp 250 down to exactly 100")
        failed = True
    assert not failed, "test_alphaess_reserve_is_clamped_at_the_api_boundary"


def test_alphaess_payload_is_a_full_replacement():
    """update*ConfigInfo replaces the whole object - all seven fields must be present or
    the omitted ones are silently reset."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    charge = client.build_charge_payload("AL70", schedule)
    for key in ("sysSn", "batHighCap", "gridCharge", "timeChaf1", "timeChae1", "timeChaf2", "timeChae2"):
        if key not in charge:
            print(f"ERROR: charge payload missing {key}")
            failed = True
    discharge = client.build_discharge_payload("AL70", schedule)
    for key in ("sysSn", "batUseCap", "ctrDis", "timeDisf1", "timeDise1", "timeDisf2", "timeDise2"):
        if key not in discharge:
            print(f"ERROR: discharge payload missing {key}")
            failed = True
    assert not failed, "test_alphaess_payload_is_a_full_replacement"


def test_alphaess_payloads_equal_is_type_strict_and_order_independent():
    """payloads_equal drives Task 10's reconciliation: only rewrite when the live payload differs.

    Key order must not matter - dict construction order is not guaranteed to match the API's
    read-back order. But the comparison is type-strict: 90 and 90.0 compare UNEQUAL. Task 10
    depends on knowing that, since a numeric type mismatch alone is otherwise indistinguishable
    from a real value difference.
    """
    failed = False
    client = _client()
    a = {"sysSn": "AL70", "gridCharge": 1, "batHighCap": 90, "timeChaf1": "01:00"}
    b = {"timeChaf1": "01:00", "batHighCap": 90, "sysSn": "AL70", "gridCharge": 1}
    if not client.payloads_equal(a, b):
        print("ERROR: identical payloads with keys in a different order compared unequal")
        failed = True
    c = dict(a, gridCharge=0)
    if client.payloads_equal(a, c):
        print("ERROR: payloads with one differing value compared equal")
        failed = True
    d = dict(a, batHighCap=90.0)
    if client.payloads_equal(a, d):
        print("ERROR: payloads_equal is meant to be type-strict, but 90 and 90.0 compared equal")
        failed = True
    assert not failed, "test_alphaess_payloads_equal_is_type_strict_and_order_independent"


def _writable(sn="AL70"):
    """A client whose serial Predbat has already been asked to drive."""
    client = _client(sn)
    client.control_active.add(sn)
    return client


def test_alphaess_settle_window_suppresses_a_stale_read_back():
    """Settings reach the inverter on its NEXT cloud poll, typically one to five minutes
    after Predbat writes them.

    So a read-back immediately after a write shows the old values. That is not
    interference and must not be reported as such, or every single write would produce a
    false alarm.
    """
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    client.settle_count[("AL70", "charge")] = 0
    stale = {"gridCharge": 0, "timeChaf1": "00:00", "timeChae1": "00:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 100}
    client.note_external_change("AL70", "charge", stale)
    if any("overwritten" in message.lower() or "interference" in message.lower() for message in client.log_messages):
        print(f"ERROR: a stale read-back inside the settle window was reported as interference: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_settle_window_suppresses_a_stale_read_back"


def test_alphaess_external_change_reported_after_the_settle_window():
    """Past the settle window, a Predbat-owned field that does not match what Predbat
    wrote means something else changed it - the phone app, or another Predbat instance.

    The write endpoints are whole-object replacements, so the last writer wins.
    """
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    changed = {"gridCharge": 1, "timeChaf1": "02:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "charge", changed)
    if not any("timeChaf1" in message for message in client.log_messages):
        print(f"ERROR: the changed field was not named: {client.log_messages}")
        failed = True
    if not any("AlphaESS app" in message or "another" in message.lower() for message in client.log_messages):
        print(f"ERROR: no interference explanation: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_external_change_reported_after_the_settle_window"


def test_alphaess_only_predbat_owned_fields_count_as_interference():
    """A field Predbat never writes changing is not interference with Predbat's plan."""
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    observed = {"gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90, "someOtherField": "changed"}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "charge", observed)
    if any("overwritten" in message.lower() for message in client.log_messages):
        print(f"ERROR: an unowned field was reported as interference: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_only_predbat_owned_fields_count_as_interference"


def test_alphaess_no_interference_check_before_predbat_has_written():
    """With nothing in applied_payload there is no Predbat intent to compare against, so
    whatever the inverter reports is simply the user's own configuration."""
    failed = False
    client = _writable()
    observed = {"gridCharge": 1, "timeChaf1": "03:00", "timeChae1": "04:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 55}
    for _ in range(ALPHAESS_SETTLE_POLLS + 2):
        client.note_external_change("AL70", "charge", observed)
    if any("overwritten" in message.lower() for message in client.log_messages):
        print(f"ERROR: reported interference with no prior Predbat write: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_no_interference_check_before_predbat_has_written"


def test_alphaess_detected_change_clears_applied_payload_so_next_cycle_reapplies():
    """When interference is detected, the recorded intent is cleared so the next cycle
    re-applies rather than deciding the payload is unchanged and leaving the inverter
    on someone else's settings.

    This is the most consequential line: without it, Predbat would cache the old intent,
    see the inverter now has someone else's settings, and decide "nothing to do".
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    # Seed with what Predbat wrote
    original_payload = {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
    client.applied_payload["AL70"] = {"charge": original_payload}
    # Observe that an owned field changed (someone else wrote)
    changed = {"gridCharge": 1, "timeChaf1": "02:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "charge", changed)
    # Verify detection was logged
    if not any("AlphaESS app" in message or "another" in message.lower() for message in client.log_messages):
        print(f"ERROR: interference not detected: {client.log_messages}")
        failed = True
    # CRITICAL: applied_payload must be cleared so next cycle re-applies
    if "charge" in client.applied_payload.get("AL70", {}):
        print(f"ERROR: applied_payload['AL70']['charge'] not cleared after detection: {client.applied_payload['AL70']}")
        failed = True
    # Now apply the SAME schedule and verify it sends a write (because cache was cleared)
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        calls_before = session.return_value.post.call_count
        run_async_local(client.apply_settings("AL70", schedule))
        calls_after = session.return_value.post.call_count
    # Both charge and discharge writes should go out (the schedule differs from a blank slate)
    if calls_after - calls_before < 1:
        print(f"ERROR: apply_settings sent {calls_after - calls_before} POST(s) after intent clear, should send at least 1")
        failed = True
    assert not failed, "test_alphaess_detected_change_clears_applied_payload_so_next_cycle_reapplies"


def test_alphaess_settle_window_boundary_at_exactly_alphaess_settle_polls():
    """At exactly ALPHAESS_SETTLE_POLLS reads, interference is suppressed. On the very
    next read (ALPHAESS_SETTLE_POLLS + 1), it is reported.

    This pins the boundary condition: the check must be <= not <.
    """
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    changed = {"gridCharge": 0, "timeChaf1": "02:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
    # Call exactly ALPHAESS_SETTLE_POLLS times - should suppress
    for _ in range(ALPHAESS_SETTLE_POLLS):
        client.note_external_change("AL70", "charge", changed)
    if any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: interference reported at exactly ALPHAESS_SETTLE_POLLS reads: {client.log_messages}")
        failed = True
    # One more call - should now report
    client.note_external_change("AL70", "charge", changed)
    if not any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: interference not reported after ALPHAESS_SETTLE_POLLS+1 reads: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_settle_window_boundary_at_exactly_alphaess_settle_polls"


def test_alphaess_interference_detection_for_discharge_direction():
    """Discharge direction must also detect interference on owned fields and ignore unowned ones."""
    failed = False
    client = _writable()
    # Set up discharge payload
    client.applied_payload["AL70"] = {"discharge": {"sysSn": "AL70", "ctrDis": 1, "timeDisf1": "17:00", "timeDise1": "19:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 20}}
    # Owned field changed: timeDisf1
    changed_owned = {"ctrDis": 1, "timeDisf1": "18:00", "timeDise1": "19:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 20}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "discharge", changed_owned)
    if not any("timeDisf1" in message for message in client.log_messages):
        print(f"ERROR: discharge owned field change not detected: {client.log_messages}")
        failed = True
    # Clear logs and test unowned field filter for discharge
    client.log_messages = []
    client.applied_payload["AL70"] = {"discharge": {"sysSn": "AL70", "ctrDis": 1, "timeDisf1": "17:00", "timeDise1": "19:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 20}}
    # Unowned field changed
    changed_unowned = {"ctrDis": 1, "timeDisf1": "17:00", "timeDise1": "19:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 20, "someUnownedField": "changed"}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "discharge", changed_unowned)
    if any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: discharge unowned field reported as interference: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_interference_detection_for_discharge_direction"


def test_alphaess_absent_owned_field_in_observed_does_not_count_as_differing():
    """When an owned field is absent from observed, it is skipped (not treated as differing).

    The API read might be missing fields in edge cases; absence is not interference,
    only presence-with-different-value is.
    """
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    # Observed is missing timeChaf1 (owned field)
    observed_missing_field = {"gridCharge": 1, "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "charge", observed_missing_field)
    if any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: absent owned field reported as interference: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_absent_owned_field_in_observed_does_not_count_as_differing"


def test_alphaess_identical_payload_is_not_rewritten():
    """The write button is pressed EVERY cycle as Predbat's normal 'apply' action, not only
    when the plan changed.

    DEYE hit this first: PR #4371 (commit 3e1de759) measured 40 button presses producing 36
    byte-identical control orders over two hours on a live site once the button forced the
    write. The applied-payload cache is the single source of truth for whether a write is
    needed.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        result_first = run_async_local(client.apply_settings("AL70", schedule))
        first_calls = session.return_value.post.call_count if hasattr(session.return_value, "post") else None
        result_second = run_async_local(client.apply_settings("AL70", schedule))
        second_calls = session.return_value.post.call_count if hasattr(session.return_value, "post") else None
    # Second identical apply must send nothing: the POST count must not move at all, not
    # merely end up holding the same cached value - a value can be identical either because
    # nothing was sent, or because something identical was sent again and just landed on the
    # same bytes, and only the call count tells those two apart.
    if second_calls != first_calls:
        print(f"ERROR: second identical apply sent {second_calls} - {first_calls} POST(s), should send zero")
        failed = True
    # Both calls actually caught the inverter up (sent, then already-matching) so both must
    # report True - only a HELD or rejected write should ever read as not-caught-up.
    if not result_first or not result_second:
        print(f"ERROR: apply_settings returned {result_first!r} then {result_second!r}, both should be truthy")
        failed = True
    if client.applied_payload.get("AL70", {}).get("charge") is None:
        print("ERROR: applied payload not cached")
        failed = True
    if not any("unchanged" in message.lower() or "no change" in message.lower() for message in client.log_messages):
        print(f"ERROR: no 'unchanged' log on the second apply, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_identical_payload_is_not_rewritten"


def test_alphaess_charge_and_discharge_gated_independently():
    """A charge-only change must not consume a discharge write.

    Both endpoints are documented as writable once per 24 hours, so a shared gate would
    burn half the budget for nothing.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    base = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        run_async_local(client.apply_settings("AL70", base))
        discharge_before = dict(client.applied_payload["AL70"]["discharge"])
        calls_after_base = session.return_value.post.call_count
        changed = _schedule(charge={"enable": True, "soc": 80, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
        run_async_local(client.apply_settings("AL70", changed))
        calls_after_changed = session.return_value.post.call_count
    if client.applied_payload["AL70"]["charge"]["batHighCap"] != 80:
        print(f"ERROR: charge payload not updated: {client.applied_payload['AL70']['charge']}")
        failed = True
    if client.applied_payload["AL70"]["discharge"] != discharge_before:
        print("ERROR: discharge payload rewritten by a charge-only change")
        failed = True
    # The value check above passes even if discharge was needlessly re-sent with identical
    # bytes, so the call count is what actually proves the discharge write was skipped -
    # exactly one POST (charge only) must have gone out for the charge-only change.
    if calls_after_changed - calls_after_base != 1:
        print(f"ERROR: a charge-only change sent {calls_after_changed - calls_after_base} POST(s), should send exactly 1 (charge only, discharge held back)")
        failed = True
    assert not failed, "test_alphaess_charge_and_discharge_gated_independently"


def test_alphaess_reconcile_is_gated_on_predbat_read_only():
    """execute.py:145 covers every write that ORIGINATES FROM A PLAN, but not one the
    component initiates itself - and _reconcile_control is exactly that.

    The payload is time-dependent because batUseCap switches between the export target and
    the reserve, so a window transition changes it with no plan change at all. Without this
    gate that transition would write during read-only. GH#4436, fixed for DEYE and Sunsynk
    after the fact (deye.py:1661, sunsynk.py:1346).
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    client.state["switch.predbat_set_read_only"] = "on"
    client.local_schedule["AL70"] = _schedule(export={"enable": True, "soc": 20, "power": 3000, "start": "17:00:00", "end": "19:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client._reconcile_control("AL70"))
    if client.applied_payload.get("AL70"):
        print(f"ERROR: wrote while read-only: {client.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_reconcile_is_gated_on_predbat_read_only"


def test_alphaess_reconcile_is_gated_on_control_enable():
    """control_enable false means monitoring only.

    apply_settings ALSO checks control_enable as defence-in-depth, so asserting only on
    applied_payload would pass even with _reconcile_control's own clause deleted - the
    deeper gate would still catch it. This pins _reconcile_control's OWN guard specifically,
    by replacing apply_schedule with a spy: if the clause were removed, _reconcile_control
    would call it despite control_enable being False, regardless of what apply_settings does
    once inside it.
    """
    failed = False
    client = _writable()
    client.control_enable = False
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    called = {"apply_schedule": False}

    async def spy_apply_schedule(sn, force=False):
        """Record whether _reconcile_control attempted to apply at all."""
        called["apply_schedule"] = True
        return True

    client.apply_schedule = spy_apply_schedule
    run_async_local(client._reconcile_control("AL70"))
    if called["apply_schedule"]:
        print("ERROR: _reconcile_control called apply_schedule despite control_enable being False")
        failed = True
    assert not failed, "test_alphaess_reconcile_is_gated_on_control_enable"


def test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive():
    """A startup cycle must never clobber an inverter before there is a plan to apply."""
    failed = False
    client = _client()  # NOT in control_active
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client._reconcile_control("AL70"))
    if client.applied_payload.get("AL70"):
        print(f"ERROR: wrote for an undriven serial: {client.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive"


def test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it():
    """The 24h documented write limit is treated as a real budget, but a held change must
    be applied on the next eligible tick - not lost.

    The second write is 100 seconds later, past ALPHAESS_WRITE_SETTLE_SECONDS: a genuinely
    NEW schedule update, not the tail of the one just committed. Corrections inside the
    settle window are exempt on purpose and are covered by
    test_alphaess_same_cycle_correction_is_not_held_by_the_write_pacer.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 300
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    first = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    second = _schedule(charge={"enable": True, "soc": 70, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", first))
        with patch("alphaess.time.time", return_value=1100.0):
            run_async_local(client.apply_settings("AL70", second))
        held = client.applied_payload["AL70"]["charge"]["batHighCap"]
        if held != 90:
            print(f"ERROR: write not held inside the interval, batHighCap {held}")
            failed = True
        # Past the interval, the held change goes out.
        with patch("alphaess.time.time", return_value=1400.0):
            run_async_local(client.apply_settings("AL70", second))
    if client.applied_payload["AL70"]["charge"]["batHighCap"] != 70:
        print(f"ERROR: held change never applied: {client.applied_payload['AL70']['charge']}")
        failed = True
    assert not failed, "test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it"


def test_alphaess_6053_backs_off_rather_than_counting_as_a_failure():
    """Too-fast is a pacing signal, not a broken component - it must not be logged as a
    fault (Warn) either, or the pacing intent this test name asserts is contradicted."""
    failed = False
    client = _writable()
    client.min_write_interval = 0
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    busy = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="The request was too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(busy)):
        run_async_local(client.apply_settings("AL70", schedule))
    # A rejected write must NOT be cached as applied, or the retry never happens.
    if client.applied_payload.get("AL70", {}).get("charge") is not None:
        print("ERROR: a 6053-rejected write was cached as applied")
        failed = True
    if any(message.startswith("Warn:") and "6053" in message for message in client.log_messages):
        print(f"ERROR: a 6053 was logged at Warn, reading as a component fault rather than pacing: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_6053_backs_off_rather_than_counting_as_a_failure"


def test_alphaess_6053_backs_off_the_retry_via_min_write_interval():
    """A 6053 must stamp an attempt time so the retry itself is paced too.

    last_write_time was previously stamped only on success, so the very next cycle hammered
    the same endpoint again with no extra spacing at all - the opposite of pacing, against
    endpoints documented as writable once per 24 hours.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 300
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    busy = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="The request was too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(busy)) as session:
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", schedule))
            calls_after_first = session.return_value.post.call_count
        # 10 seconds later, well inside the 300s minimum interval: the retry must be held,
        # not attempted again.
        with patch("alphaess.time.time", return_value=1010.0):
            run_async_local(client.apply_settings("AL70", schedule))
            calls_after_second = session.return_value.post.call_count
    if calls_after_second != calls_after_first:
        print(f"ERROR: a 6053 was retried inside min_write_interval instead of being paced, {calls_after_first} -> {calls_after_second} POST(s)")
        failed = True
    assert not failed, "test_alphaess_6053_backs_off_the_retry_via_min_write_interval"


def test_alphaess_persistently_rejected_write_is_paced_not_retried_every_tick():
    """A persistently rejected write (6008, 6042, 6001, ...) must be paced by
    alphaess_min_write_interval exactly like a 6053, not re-POSTed every tick forever.

    Before the fix, only 6053 stamped last_write_time on failure - every other rejection
    left _write_allowed open, so _reconcile_control re-POSTed on every single tick (roughly
    5,800 log lines a day at a 60-second cadence) while burning the documented 24-hour
    write budget doing it. Uses 6008 "Set failed" specifically, since that is the code the
    API documents for a rejected write, distinct from the already-covered 6053 pacing
    signal.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 300
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    rejected = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(rejected)) as session:
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", schedule))
            calls_after_first = session.return_value.post.call_count
        # 10 seconds later, well inside the 300s minimum interval: the retry must be held,
        # not attempted again - exactly the 6053 behaviour, for a different rejection code.
        with patch("alphaess.time.time", return_value=1010.0):
            run_async_local(client.apply_settings("AL70", schedule))
            calls_after_second = session.return_value.post.call_count
    if calls_after_second != calls_after_first:
        print(f"ERROR: a persistent 6008 rejection was retried inside min_write_interval, {calls_after_first} -> {calls_after_second} POST(s)")
        failed = True
    # A rejected write must still not be cached as applied, so it keeps retrying once the
    # interval has passed - only the PACE changed, not the retry-forever behaviour itself.
    if client.applied_payload.get("AL70", {}).get("charge") is not None:
        print("ERROR: a rejected write was cached as applied")
        failed = True
    assert not failed, "test_alphaess_persistently_rejected_write_is_paced_not_retried_every_tick"


def test_alphaess_rejected_hold_charge_defers_legacy_discharge_update():
    """A rejected hold profile must not be followed by ctrDis=0 demand mode.

    Legacy AlphaESS uses separate charge/discharge endpoints. The enabled charge profile is
    the only working hold primitive, so sending the companion demand-mode payload after its
    rejection would actively remove the old attempted hold while installing no replacement.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    schedule = _schedule(reserve=10, export_power=0)
    rejected = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(rejected)) as session:
        result = run_async_local(client.apply_settings("AL70", schedule))
    if result:
        print(f"ERROR: rejected hold transition returned {result!r}, should be falsy")
        failed = True
    if session.return_value.post.call_count != 1:
        print(f"ERROR: rejected hold charge was followed by {session.return_value.post.call_count - 1} additional POST(s)")
        failed = True
    if client.applied_payload.get("AL70", {}).get("discharge") is not None:
        print(f"ERROR: discharge payload was applied after hold charge rejection: {client.applied_payload}")
        failed = True
    if not any("discharge update deferred" in message.lower() for message in client.log_messages):
        print(f"ERROR: deferred legacy discharge transition was not logged: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_rejected_hold_charge_defers_legacy_discharge_update"


def test_alphaess_held_write_is_not_reported_as_applied():
    """apply_settings must not report success for a payload that was HELD, not sent.

    A later consumer (Task 11's periodic reconciliation) could otherwise read a bare True
    as "the inverter matches the plan" when a real change is still pending behind the
    minimum write interval.

    100 seconds apart, so this is a new schedule update rather than a same-cycle correction
    inside ALPHAESS_WRITE_SETTLE_SECONDS, which is deliberately allowed through.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 300
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    first = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    second = _schedule(charge={"enable": True, "soc": 70, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        with patch("alphaess.time.time", return_value=1000.0):
            first_result = run_async_local(client.apply_settings("AL70", first))
        with patch("alphaess.time.time", return_value=1100.0):
            held_result = run_async_local(client.apply_settings("AL70", second))
    if not first_result:
        print(f"ERROR: a genuinely sent write returned {first_result!r}, should be truthy")
        failed = True
    if held_result:
        print(f"ERROR: apply_settings returned {held_result!r} for a change HELD by min_write_interval, should be falsy")
        failed = True
    assert not failed, "test_alphaess_held_write_is_not_reported_as_applied"


def test_alphaess_same_cycle_correction_is_not_held_by_the_write_pacer():
    """GH#4769: a stale target SoC committed with the charge window must be correctable now.

    Predbat commits a schedule in stages - window, then enable, then target SoC - pressing
    the schedule write button after each one. The first commit of a cycle therefore carries
    whatever target SoC the control entity still holds from the previous cycle, which for a
    manual charge on the live slot was 0, clamped up to the API's chargeLimit floor of 10.
    The corrected 100 arrived three seconds later and was held for the full 300s, so the
    inverter ran a "charge to 10%" schedule on an already-active window and the house sat on
    the grid at 69% SoC.

    Reproduced on the periodic path, since that is what the reporter's system uses, and
    asserted on the POST BODY rather than the call count: the point is that the value which
    actually reached AlphaESS is the corrected one.
    """
    failed = False
    client = _writable()
    client._periodic_ok["AL70"] = True
    client.min_write_interval = 300
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    stale = _schedule(charge={"enable": True, "soc": 0, "power": 5000, "start": "00:30:00", "end": "01:00:00"})
    corrected = _schedule(charge={"enable": True, "soc": 100, "power": 5000, "start": "00:30:00", "end": "01:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", stale))
        # Three seconds later, exactly as adjust_battery_target follows adjust_charge_window.
        with patch("alphaess.time.time", return_value=1003.0):
            corrected_result = run_async_local(client.apply_settings("AL70", corrected))
    limits = [call.kwargs.get("json", {}).get("chargeTimeList", [{}])[0].get("chargeLimit") for call in session.return_value.post.call_args_list]
    if limits != [10, 100]:
        print(f"ERROR: the corrected target SoC did not reach AlphaESS in the same cycle, chargeLimit sent: {limits}")
        failed = True
    if not corrected_result:
        print(f"ERROR: apply_settings returned {corrected_result!r} for a correction that was actually sent")
        failed = True
    assert not failed, "test_alphaess_same_cycle_correction_is_not_held_by_the_write_pacer"


def test_alphaess_correction_burst_is_capped_so_pacing_still_bounds_the_write_budget():
    """The settle exemption is a correction path, not an open door.

    Once ALPHAESS_WRITE_BURST_MAX writes have gone out inside one settle window, the next
    differing payload is held again - so the worst case against the documented 24-hour write
    budget stays a small constant per pacing interval rather than one write per tick.
    """
    failed = False
    client = _writable()
    # Periodic, so one apply_settings is exactly one POST and the count below is unambiguous -
    # the legacy pair sends charge and discharge separately and is gated per direction.
    client._periodic_ok["AL70"] = True
    client.min_write_interval = 300
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    # One payload per burst slot, plus one more that must be refused.
    schedules = [_schedule(charge={"enable": True, "soc": soc, "power": 3000, "start": "01:00:00", "end": "05:00:00"}) for soc in range(90, 90 - (ALPHAESS_WRITE_BURST_MAX + 1), -1)]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        for offset, schedule in enumerate(schedules):
            with patch("alphaess.time.time", return_value=1000.0 + offset):
                run_async_local(client.apply_settings("AL70", schedule))
        sent = session.return_value.post.call_count
        if sent != ALPHAESS_WRITE_BURST_MAX:
            print(f"ERROR: burst sent {sent} write(s), expected the cap of {ALPHAESS_WRITE_BURST_MAX}")
            failed = True
        # Past the settle window but still inside the pacing interval: still held.
        with patch("alphaess.time.time", return_value=1000.0 + ALPHAESS_WRITE_SETTLE_SECONDS + 10):
            run_async_local(client.apply_settings("AL70", schedules[-1]))
        if session.return_value.post.call_count != sent:
            print("ERROR: a write went out past the settle window but inside alphaess_min_write_interval")
            failed = True
        # Past the pacing interval: the held change goes out, and starts a fresh burst.
        with patch("alphaess.time.time", return_value=1400.0):
            run_async_local(client.apply_settings("AL70", schedules[-1]))
    if session.return_value.post.call_count != sent + 1:
        print(f"ERROR: the held change never went out past alphaess_min_write_interval, {sent} -> {session.return_value.post.call_count} POST(s)")
        failed = True
    if client.write_burst_writes.get(("AL70", "periodic")) != 1:
        print(f"ERROR: a write outside the settle window did not start a fresh burst: {client.write_burst_writes}")
        failed = True
    assert not failed, "test_alphaess_correction_burst_is_capped_so_pacing_still_bounds_the_write_budget"


def test_alphaess_rejected_write_does_not_open_a_correction_burst():
    """Only a SUCCESSFUL write opens the settle window.

    A rejected write applied nothing, so there is no half-applied schedule to correct, and
    exempting it would reopen exactly the retry storm alphaess_min_write_interval exists to
    stop - the 6053/6008 pacing this component already fixed once.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 300
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    changed = _schedule(charge={"enable": True, "soc": 80, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    rejected = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(rejected)) as session:
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", schedule))
            calls_after_first = session.return_value.post.call_count
        # Three seconds later with a genuinely different payload: still paced, because the
        # first write never landed.
        with patch("alphaess.time.time", return_value=1003.0):
            run_async_local(client.apply_settings("AL70", changed))
    if session.return_value.post.call_count != calls_after_first:
        print(f"ERROR: a rejected write opened a correction burst, {calls_after_first} -> {session.return_value.post.call_count} POST(s)")
        failed = True
    if client.write_burst_start:
        print(f"ERROR: a rejected write recorded a settle burst: {client.write_burst_start}")
        failed = True
    assert not failed, "test_alphaess_rejected_write_does_not_open_a_correction_burst"


def test_alphaess_settle_window_never_outlasts_a_shortened_write_interval():
    """A user who shortens alphaess_min_write_interval must not get a grace longer than it.

    A fixed 60s window would otherwise swallow a 30s pacer whole, leaving that user with no
    pacing at all rather than the tighter pacing they asked for.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 30
    if client._write_settle_seconds() != 30:
        print(f"ERROR: settle window {client._write_settle_seconds()}s exceeds a 30s min_write_interval")
        failed = True
    client.min_write_interval = 300
    if client._write_settle_seconds() != ALPHAESS_WRITE_SETTLE_SECONDS:
        print(f"ERROR: settle window {client._write_settle_seconds()}s should be ALPHAESS_WRITE_SETTLE_SECONDS at the default interval")
        failed = True
    assert not failed, "test_alphaess_settle_window_never_outlasts_a_shortened_write_interval"


def test_alphaess_write_button_is_not_forced():
    """Predbat presses this every cycle as its normal apply action (time_button_press), so
    force=True here would bypass the change-detection gate on every single cycle."""
    failed = False
    client = _client()
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    captured = {}

    async def fake_apply(sn, force=False):
        """Record how apply_schedule was called."""
        captured["force"] = force
        return True

    client.apply_schedule = fake_apply
    run_async_local(client._handle_control_event("switch.predbat_alphaess_al70_battery_schedule_charge_write", "turn_on"))
    if captured.get("force") is not False:
        print(f"ERROR: the write button forced the write: {captured}")
        failed = True
    # Pressing it marks the serial as driven, on the press itself: a write that failed
    # still means Predbat owns this inverter and the next tick should retry.
    if "AL70" not in client.control_active:
        print("ERROR: the write button did not mark the serial as driven")
        failed = True
    assert not failed, "test_alphaess_write_button_is_not_forced"


def test_alphaess_periodic_6017_is_cached_and_never_retried():
    """The API docs are explicit that 6017 is an ENTITLEMENT verdict, not a transient
    error. Retrying it every config tier would burn calls forever on a system that will
    never answer differently."""
    failed = False
    client = _client()
    refused = create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(refused)):
        verdict = run_async_local(client.probe_periodic("AL70"))
    if verdict is not False:
        print(f"ERROR: 6017 verdict {verdict} should be False")
        failed = True
    if client._periodic_ok.get("AL70") is not False:
        print(f"ERROR: verdict not cached: {client._periodic_ok}")
        failed = True
    # A second probe must not call the API at all.
    with patch("alphaess.aiohttp.ClientSession", side_effect=AssertionError("probe_periodic must not re-call after 6017")):
        again = run_async_local(client.probe_periodic("AL70"))
    if again is not False:
        print(f"ERROR: cached verdict not reused, got {again}")
        failed = True
    assert not failed, "test_alphaess_periodic_6017_is_cached_and_never_retried"


def test_alphaess_periodic_other_failures_leave_the_verdict_unknown():
    """Only 6017 is an entitlement verdict; a transient failure must be re-probed."""
    failed = False
    client = _client()
    busy = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(busy)):
        verdict = run_async_local(client.probe_periodic("AL70"))
    if verdict is not None:
        print(f"ERROR: transient failure verdict {verdict} should be None")
        failed = True
    if "AL70" in client._periodic_ok:
        print(f"ERROR: transient failure cached a verdict: {client._periodic_ok}")
        failed = True
    assert not failed, "test_alphaess_periodic_other_failures_leave_the_verdict_unknown"


def test_alphaess_periodic_payload_shape():
    """Six windows and a per-window chargePower, with the constraints the API enforces."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 4000, "start": "17:00:00", "end": "19:00:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    if payload.get("executeCycleType") != 0:
        print(f"ERROR: executeCycleType {payload.get('executeCycleType')} should be 0 (daily)")
        failed = True
    charge_list = payload.get("chargeTimeList") or []
    discharge_list = payload.get("dischargeTimeList") or []
    # BOTH lists need at least one element: [] is rejected with 6001 "time list is null",
    # and omitting the key gets 10001.
    if not charge_list or not discharge_list:
        print(f"ERROR: empty list in payload: {payload}")
        failed = True
    first = charge_list[0]
    if first.get("beginTime") != "01:00" or first.get("endTime") != "05:00":
        print(f"ERROR: charge period {first}")
        failed = True
    # chargeLimit range is [10,100] - 6001 otherwise.
    if not 10 <= first.get("chargeLimit", 0) <= 100:
        print(f"ERROR: chargeLimit {first.get('chargeLimit')} out of the [10,100] range")
        failed = True
    if first.get("chargePower") != 3000:
        print(f"ERROR: chargePower {first.get('chargePower')} should carry Predbat's rate")
        failed = True
    if discharge_list[0].get("chargePower") != 4000:
        print(f"ERROR: discharge chargePower {discharge_list[0].get('chargePower')}")
        failed = True
    if payload.get("gridChargeCycle") != 1 or payload.get("ctrDisCycle") != 1:
        print(f"ERROR: cycle flags {payload.get('gridChargeCycle')}/{payload.get('ctrDisCycle')}")
        failed = True
    assert not failed, "test_alphaess_periodic_payload_shape"


def test_alphaess_periodic_disabled_direction_uses_the_cycle_flag():
    """Both lists must be non-empty, so a disabled direction is expressed by its FLAG and
    a filler period rather than by an empty list the API rejects."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    payload = client.build_periodic_payload("AL70", schedule)
    if not payload.get("dischargeTimeList"):
        print("ERROR: dischargeTimeList must not be empty - [] gets 6001 'time list is null'")
        failed = True
    if payload.get("ctrDisCycle") != 0:
        print(f"ERROR: ctrDisCycle {payload.get('ctrDisCycle')} should be 0 when no export is planned")
        failed = True
    if payload.get("gridChargeCycle") != 1:
        print(f"ERROR: gridChargeCycle {payload.get('gridChargeCycle')}")
        failed = True
    assert not failed, "test_alphaess_periodic_disabled_direction_uses_the_cycle_flag"


def test_alphaess_periodic_windows_do_not_overlap():
    """Charge and discharge periods must not overlap - 6008 otherwise."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "06:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "05:00:00", "end": "08:00:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    charge_end = payload["chargeTimeList"][0]["endTime"]
    discharge_start = payload["dischargeTimeList"][0]["beginTime"]
    if charge_end > discharge_start:
        print(f"ERROR: overlapping periods {charge_end} > {discharge_start}")
        failed = True
    if not any("overlap" in message.lower() for message in client.log_messages):
        print(f"ERROR: no overlap-trim log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_periodic_windows_do_not_overlap"


def test_alphaess_periodic_charge_limit_floor_is_clamped():
    """chargeLimit is documented as [10,100] and Predbat's own idle soc is 0 - passing that
    straight through gets 6001. Added beyond the brief: mutation-testing
    test_alphaess_periodic_payload_shape showed soc=90/20 stays in-range even with the
    low-10 floor removed from _periodic_entry, so that test alone does not prove the floor
    exists. This test exercises soc=0 on both directions, which only passes if the floor is
    actually applied."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        charge={"enable": True, "soc": 0, "power": 3000, "start": "01:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 0, "power": 4000, "start": "17:00:00", "end": "19:00:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    charge_limit = payload["chargeTimeList"][0].get("chargeLimit")
    discharge_limit = payload["dischargeTimeList"][0].get("chargeLimit")
    if charge_limit != 10:
        print(f"ERROR: charge chargeLimit {charge_limit} should be floored to 10, not passed through as 0")
        failed = True
    if discharge_limit != 10:
        print(f"ERROR: discharge chargeLimit {discharge_limit} should be floored to 10, not passed through as 0")
        failed = True
    assert not failed, "test_alphaess_periodic_charge_limit_floor_is_clamped"


def test_alphaess_periodic_rate_zero_is_freeze_not_demand_mode():
    """Periodic zero discharge must use the same fixed 10% charge profile."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(reserve=15, export_power=0)
    payload = client.build_periodic_payload("AL70", schedule)
    if payload.get("gridChargeCycle") != 1 or payload.get("ctrDisCycle") != 0:
        print(f"ERROR: hold cycle flags should be charge=1/discharge=0, got {payload}")
        failed = True
    entry = (payload.get("chargeTimeList") or [{}])[0]
    if entry.get("beginTime") != "00:00" or entry.get("endTime") != "23:45" or entry.get("chargeLimit") != 10:
        print(f"ERROR: periodic hold charge profile is wrong: {entry}")
        failed = True
    if entry.get("chargePower") != 100:
        print(f"ERROR: periodic hold should use the fixed 100W power, got {entry}")
        failed = True
    assert not failed, "test_alphaess_periodic_rate_zero_is_freeze_not_demand_mode"


def test_alphaess_periodic_both_rates_zero_still_holds_the_battery():
    """Both-zero periodic intent still creates the working charge-profile hold."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(reserve=10, charge_power=0, export_power=0)
    payload = client.build_periodic_payload("AL70", schedule)
    if payload.get("ctrDisCycle") != 0:
        print(f"ERROR: ctrDisCycle {payload.get('ctrDisCycle')} should be 0 while charge profile holds")
        failed = True
    if payload.get("gridChargeCycle") != 1 or payload["chargeTimeList"][0].get("chargeLimit") != 10 or payload["chargeTimeList"][0].get("chargePower") != 100:
        print(f"ERROR: both-zero periodic intent did not create the hold profile: {payload}")
        failed = True
    assert not failed, "test_alphaess_periodic_both_rates_zero_still_holds_the_battery"


def test_alphaess_periodic_reserve_reaches_the_idle_discharge_entry():
    """With no export window planned (demand mode, NOT a hold), the reserve must still
    reach the inverter.

    Before the fix, the idle discharge filler hard-coded chargeLimit to 10 regardless of
    schedule["reserve"], so an entitled system's reserve never left Predbat's memory:
    INVERTER_DEF sets has_reserve_soc True and automatic_config maps reserve, so Predbat
    believed it set a floor the inverter never received. chargeLimit on the idle entry is
    the only field this path has to carry a standing floor - there is no separate batUseCap
    on the periodic pair.
    """
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    # export not enabled (demand mode): the idle filler must carry the ACTUAL reserve, not
    # an arbitrary constant. 37 is picked specifically because it differs from the old
    # hard-coded 10, so a regression back to the constant is caught.
    schedule = _schedule(reserve=37)
    payload = client.build_periodic_payload("AL70", schedule)
    entry = (payload.get("dischargeTimeList") or [{}])[0]
    if entry.get("chargeLimit") != 37:
        print(f"ERROR: idle discharge chargeLimit {entry.get('chargeLimit')} should carry the reserve 37")
        failed = True
    if payload.get("ctrDisCycle") != 0:
        print(f"ERROR: ctrDisCycle {payload.get('ctrDisCycle')} should be 0 - this is demand mode, not a hold")
        failed = True
    # The [10,100] range constraint still applies to the reserve itself.
    low = client.build_periodic_payload("AL70", _schedule(reserve=3))
    if low["dischargeTimeList"][0].get("chargeLimit") != 10:
        print(f"ERROR: reserve 3 should floor to 10 in the idle entry, got {low['dischargeTimeList'][0].get('chargeLimit')}")
        failed = True
    assert not failed, "test_alphaess_periodic_reserve_reaches_the_idle_discharge_entry"


def test_alphaess_periodic_non_overlapping_windows_survive_intact():
    """Windows that do not actually overlap must be written verbatim, not trimmed.

    Before the fix, the overlap check (hm_to_minutes(export_start) < hm_to_minutes(charge_end))
    ignored charge_start entirely, so an export window entirely BEFORE a later charge
    window (export 10:00-11:00, charge 13:00-14:00 - no overlap at all) was falsely trimmed:
    the check saw export_start (10:00) < charge_end (14:00) and moved export_start to
    14:00, collapsing a genuine window and discarding a whole peak-rate export.
    """
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        charge={"enable": True, "soc": 90, "power": 3000, "start": "13:00:00", "end": "14:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "10:00:00", "end": "11:00:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    charge_entry = payload["chargeTimeList"][0]
    discharge_entry = payload["dischargeTimeList"][0]
    if (charge_entry.get("beginTime"), charge_entry.get("endTime")) != ("13:00", "14:00"):
        print(f"ERROR: charge window trimmed despite no overlap: {charge_entry}")
        failed = True
    if (discharge_entry.get("beginTime"), discharge_entry.get("endTime")) != ("10:00", "11:00"):
        print(f"ERROR: export window trimmed despite no overlap: {discharge_entry}")
        failed = True
    if payload.get("ctrDisCycle") != 1 or payload.get("gridChargeCycle") != 1:
        print(f"ERROR: a falsely-collapsed window disabled a cycle flag: gridChargeCycle={payload.get('gridChargeCycle')} ctrDisCycle={payload.get('ctrDisCycle')}")
        failed = True
    if any("overlap" in message.lower() for message in client.log_messages):
        print(f"ERROR: an overlap was logged when the windows do not overlap: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_periodic_non_overlapping_windows_survive_intact"


def test_alphaess_periodic_export_before_charge_overlap_trims_the_export_end():
    """A genuine before-overlap must be trimmed at the correct (nearer) edge, not destroyed.

    Before the fix, ANY detected overlap trimmed export_start to charge_end regardless of
    which window came first. For export 12:00-13:30 overlapping charge 13:00-14:00 (export
    starts BEFORE the charge window), that produced export_start=14:00 with export_end
    still 13:30 - an inverted, collapsed window - destroying the whole export rather than
    trimming it to the 12:00-13:00 portion that does not overlap.
    """
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        charge={"enable": True, "soc": 90, "power": 3000, "start": "13:00:00", "end": "14:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "12:00:00", "end": "13:30:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    discharge_entry = payload["dischargeTimeList"][0]
    if (discharge_entry.get("beginTime"), discharge_entry.get("endTime")) != ("12:00", "13:00"):
        print(f"ERROR: export window {discharge_entry.get('beginTime')}-{discharge_entry.get('endTime')} should be trimmed to 12:00-13:00, not destroyed")
        failed = True
    if payload.get("ctrDisCycle") != 1:
        print(f"ERROR: ctrDisCycle {payload.get('ctrDisCycle')} should still be 1 - the trimmed window is still real")
        failed = True
    if not any("overlap" in message.lower() for message in client.log_messages):
        print(f"ERROR: no overlap-trim log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_periodic_export_before_charge_overlap_trims_the_export_end"


def test_alphaess_periodic_direction_detects_interference():
    """Entitled systems write via setTimeChargeBySn, so applied_payload[sn] only ever
    carries a "periodic" key for them - note_external_change("charge"/"discharge", ...)
    can never find a match against it, and interference on an entitled system's REAL
    write path went undetected forever. The "periodic" direction must detect it too."""
    failed = False
    client = _writable()
    client._periodic_ok["AL70"] = True
    original = {
        "sysSn": "AL70",
        "gridChargeCycle": 1,
        "ctrDisCycle": 0,
        "chargeTimeList": [{"beginTime": "01:00", "endTime": "05:00", "chargeLimit": 90, "chargePower": 3000}],
        "dischargeTimeList": [{"beginTime": "00:00", "endTime": "00:00", "chargeLimit": 10}],
    }
    client.applied_payload["AL70"] = {"periodic": original}
    changed = dict(original, ctrDisCycle=1, dischargeTimeList=[{"beginTime": "17:00", "endTime": "19:00", "chargeLimit": 20, "chargePower": 3000}])
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "periodic", changed)
    if not any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: periodic interference not detected: {client.log_messages}")
        failed = True
    if "periodic" in client.applied_payload.get("AL70", {}):
        print(f"ERROR: periodic intent not cleared after detection: {client.applied_payload['AL70']}")
        failed = True
    assert not failed, "test_alphaess_periodic_direction_detects_interference"


def _semantically_identical_periodic_echo():
    """Build (applied, echoed) periodic payloads that mean the same thing but differ in
    ways a naive str()-of-the-list comparison would wrongly flag: key order inside each
    entry, chargeLimit returned as a float, and a server-added chargePower: 0 on the idle
    discharge entry Predbat never sent (it only ever writes chargePower when rate > 0)."""
    applied = {
        "sysSn": "AL70",
        "gridChargeCycle": 1,
        "ctrDisCycle": 0,
        "chargeTimeList": [{"beginTime": "01:00", "endTime": "05:00", "chargeLimit": 90, "chargePower": 3000}],
        "dischargeTimeList": [{"beginTime": "00:00", "endTime": "00:00", "chargeLimit": 10}],
    }
    echoed = {
        "sysSn": "AL70",
        "gridChargeCycle": 1,
        "ctrDisCycle": 0,
        # Same charge entry, keys reordered and chargeLimit as a float.
        "chargeTimeList": [{"chargePower": 3000, "endTime": "05:00", "chargeLimit": 90.0, "beginTime": "01:00"}],
        # Same idle discharge entry, plus a server-added chargePower: 0 Predbat never sent.
        "dischargeTimeList": [{"chargeLimit": 10.0, "chargePower": 0, "endTime": "00:00", "beginTime": "00:00"}],
    }
    return applied, echoed


def test_alphaess_periodic_interference_ignores_key_order_float_limit_and_echoed_zero_power():
    """A semantically-identical server echo must NOT be read as interference, or the next
    _reconcile_control pops the recorded intent and sends a redundant setTimeChargeBySn -
    to an endpoint documented as writable once per 24 hours. This is exactly the write-
    budget burn the whole component exists to prevent, just triggered by the interference
    detector itself rather than by a real external change."""
    failed = False
    client = _writable()
    client._periodic_ok["AL70"] = True
    applied, echoed = _semantically_identical_periodic_echo()
    client.applied_payload["AL70"] = {"periodic": applied}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "periodic", echoed)
    if any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: a semantically-identical echo was reported as interference: {client.log_messages}")
        failed = True
    if "periodic" not in client.applied_payload.get("AL70", {}):
        print(f"ERROR: recorded intent was popped for a semantically-identical echo: {client.applied_payload.get('AL70')}")
        failed = True
    assert not failed, "test_alphaess_periodic_interference_ignores_key_order_float_limit_and_echoed_zero_power"


def test_alphaess_periodic_interference_still_detects_a_genuine_window_change():
    """The structural comparison must not be a blunt "never detect anything" - a genuinely
    different window has to be caught. Deliberately leaves gridChargeCycle/ctrDisCycle
    untouched and changes ONLY chargeTimeList's begin/end times, so this cannot pass via
    the (already-tested, str()-compared) scalar fields riding along - it isolates the list
    comparison itself.
    """
    failed = False
    client = _writable()
    client._periodic_ok["AL70"] = True
    applied, echoed = _semantically_identical_periodic_echo()
    client.applied_payload["AL70"] = {"periodic": applied}
    genuinely_changed = dict(echoed, chargeTimeList=[{"beginTime": "02:00", "endTime": "06:00", "chargeLimit": 90, "chargePower": 3000}])
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "periodic", genuinely_changed)
    if not any("no longer match" in message.lower() for message in client.log_messages):
        print(f"ERROR: a genuine window change was not detected: {client.log_messages}")
        failed = True
    if "periodic" in client.applied_payload.get("AL70", {}):
        print(f"ERROR: periodic intent not cleared after a genuine change: {client.applied_payload['AL70']}")
        failed = True
    assert not failed, "test_alphaess_periodic_interference_still_detects_a_genuine_window_change"


def test_alphaess_apply_settings_routes_entitled_systems_through_the_periodic_endpoint():
    """Added beyond the brief: none of the five prescribed tests call apply_settings with
    _periodic_ok True, so the routing glue between build_periodic_payload and
    _write_payload - the actual reason apply_settings changed in this task - was otherwise
    exercised by nothing. Checks the periodic endpoint is used, and the legacy pair is not,
    for an entitled system."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    client.min_write_interval = 0
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        run_async_local(client.apply_settings("AL70", schedule))
    calls = session.return_value.post.call_args_list
    urls = [call.args[0] if call.args else call.kwargs.get("url", "") for call in calls]
    if not any("setTimeChargeBySn" in url for url in urls):
        print(f"ERROR: periodic endpoint not called for an entitled system, got {urls}")
        failed = True
    if any("updateChargeConfigInfo" in url or "updateDisChargeConfigInfo" in url for url in urls):
        print(f"ERROR: legacy endpoint called for an entitled system, got {urls}")
        failed = True
    if len(calls) != 1:
        print(f"ERROR: expected exactly 1 POST for the periodic path, got {len(calls)}")
        failed = True
    assert not failed, "test_alphaess_apply_settings_routes_entitled_systems_through_the_periodic_endpoint"


def test_alphaess_unbind_switch_is_published_for_every_serial():
    """One toggle per discovered serial, default off, following the Sigenergy offboard
    pattern."""
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule()
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    entity = "switch.predbat_alphaess_al70_unbind"
    published = client.published.get(entity)
    if not published:
        print(f"ERROR: {entity} not published")
        failed = True
    elif published.get("state") != "off":
        print(f"ERROR: unbind default {published.get('state')} should be off")
        failed = True
    # The switch is one-way from Home Assistant: undoing it needs a code emailed to the
    # system owner, so the friendly name has to say so.
    name = (published or {}).get("attributes", {}).get("friendly_name", "")
    if "one-way" not in name.lower() and "cannot be undone" not in name.lower():
        print(f"ERROR: unbind friendly_name does not warn it is one-way: {name!r}")
        failed = True
    assert not failed, "test_alphaess_unbind_switch_is_published_for_every_serial"


def test_alphaess_unbind_switch_reflects_an_already_latched_serial():
    """After a restart, restore_state repopulates _unbind_done from the cache before the
    first publish - the switch must come back on, not silently reset to off and invite the
    user to press it again."""
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule()
    client._unbind_done.add("AL70")
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    entity = "switch.predbat_alphaess_al70_unbind"
    published = client.published.get(entity)
    if not published or published.get("state") != "on":
        print(f"ERROR: latched unbind switch published as {published.get('state') if published else None}, expected on")
        failed = True
    assert not failed, "test_alphaess_unbind_switch_reflects_an_already_latched_serial"


def test_alphaess_unbind_latches_and_removes_the_serial():
    """A successful unbind must not re-fire on the next tick or after a restart, and the
    serial has to leave device_list - the API will refuse every call for it now."""
    failed = False
    client = _client()
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if "AL70" not in client._unbind_done:
        print(f"ERROR: unbind not latched: {client._unbind_done}")
        failed = True
    if "AL70" in client.device_list:
        print(f"ERROR: unbound serial still in device_list: {client.device_list}")
        failed = True
    if not any("num_inverters" in message or "apps.yaml" in message for message in client.log_messages):
        print(f"ERROR: no warning that auto-config args now point at a dead system: {client.log_messages}")
        failed = True
    # A second turn-on must not call the API again. AssertionError as a side_effect would
    # be swallowed by _request's own transport-failure retry loop (it catches Exception),
    # so this checks the mock's call count directly rather than relying on an exception
    # escaping the client.
    with patch("alphaess.aiohttp.ClientSession") as mock_session_cls:
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if mock_session_cls.called:
        print("ERROR: unbind must not repeat once latched, but ClientSession was called again")
        failed = True
    assert not failed, "test_alphaess_unbind_latches_and_removes_the_serial"


def test_alphaess_failed_unbind_leaves_the_latch_clear_for_retry():
    """Matches _offboard_system_if_needed: a failure must be retried on the next tick."""
    failed = False
    client = _client()
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if "AL70" in client._unbind_done:
        print("ERROR: a failed unbind was latched")
        failed = True
    if "AL70" not in client.device_list:
        print("ERROR: a failed unbind removed the serial anyway")
        failed = True
    assert not failed, "test_alphaess_failed_unbind_leaves_the_latch_clear_for_retry"


def test_alphaess_unbind_toggle_off_clears_the_latch():
    """Turning it back off lets discovery pick the system up again if it was re-bound via
    the CLI or the AlphaESS portal. It does NOT re-bind - that needs the emailed code.

    Drives the real switch_event -> _handle_control_event -> _sn_from_entity routing path
    rather than calling _handle_unbind_event directly. That matters here specifically: a
    successful unbind removes the serial from device_list, and _sn_from_entity used to
    resolve serials from device_list alone, so a genuine Home Assistant turn_off on this
    switch could never resolve to a serial once the system was actually unbound - the
    documented "turning it back off clears the latch" behaviour was unreachable in
    production even though a test calling the handler directly could not see it.
    """
    failed = False
    client = _client()
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client.switch_event("switch.predbat_alphaess_al70_unbind", "turn_on"))
    if "AL70" not in client._unbind_done or "AL70" in client.device_list:
        print(f"ERROR: setup unbind did not complete: unbind_done={client._unbind_done} device_list={client.device_list}")
        failed = True
    run_async_local(client.switch_event("switch.predbat_alphaess_al70_unbind", "turn_off"))
    if "AL70" in client._unbind_done:
        print(f"ERROR: latch not cleared: {client._unbind_done}")
        failed = True
    assert not failed, "test_alphaess_unbind_toggle_off_clears_the_latch"


def test_alphaess_unbind_toggle_off_skips_save_when_never_latched():
    """Turning off the switch for a serial that was never unbound is a no-op: discard on
    an absent member is harmless, but there is nothing changed to persist, so
    save_control must not be called."""
    failed = False
    client = _client()
    calls = []

    async def fake_save_control():
        """Record that save_control was invoked."""
        calls.append(True)

    client.save_control = fake_save_control
    run_async_local(client.switch_event("switch.predbat_alphaess_al70_unbind", "turn_off"))
    if calls:
        print(f"ERROR: save_control called for a no-op toggle-off: {calls}")
        failed = True
    assert not failed, "test_alphaess_unbind_toggle_off_skips_save_when_never_latched"


def test_alphaess_unbind_is_not_gated_by_read_only():
    """Read-only guards writes to the INVERTER; unbinding is account management.

    Asserted so nobody 'fixes' this later by folding it into the control gate.
    """
    failed = False
    client = _client()
    client.state["switch.predbat_set_read_only"] = "on"
    client.control_enable = False
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if "AL70" not in client._unbind_done:
        print("ERROR: unbind was blocked by read-only or control_enable")
        failed = True
    assert not failed, "test_alphaess_unbind_is_not_gated_by_read_only"


def run_alphaess_control_tests(my_predbat):
    """Run all AlphaESS control-logic tests."""
    failed = False
    for name, fn in [
        ("entities_round_trip", test_alphaess_control_entities_round_trip),
        ("reserve_unclamped", test_alphaess_reserve_entity_is_published_unclamped),
        ("entity_routing", test_alphaess_entity_routing_does_not_confuse_prefixed_serials),
        ("unbind_fallback_routing_collision", test_alphaess_unbind_fallback_routing_still_respects_prefix_collision),
        ("unbound_serial_scoped_to_unbind_only", test_alphaess_unbound_serial_does_not_resolve_for_non_unbind_entities),
        ("update_local_schedule", test_alphaess_update_local_schedule_applies_each_field),
        ("fallback_on_unknown_or_unavailable", test_alphaess_fallback_on_unknown_or_unavailable_values),
        ("controls_pass_through", test_alphaess_controls_pass_straight_through),
        ("batusecap_is_reserve", test_alphaess_batusecap_is_the_reserve_outside_an_export_window),
        ("rate_zero_is_freeze", test_alphaess_rate_zero_is_freeze),
        ("both_rates_zero_still_holds", test_alphaess_both_rates_zero_still_holds_the_battery),
        ("disabled_planned_hold_recovers_window", test_alphaess_disabled_planned_hold_recovers_the_charge_window),
        ("planned_hold_releases_at_window_end", test_alphaess_planned_hold_releases_when_its_window_ends),
        ("hold_profile_constant", test_alphaess_hold_profile_is_constant_while_soc_changes),
        ("real_charge_survives_hold", test_alphaess_real_charge_survives_a_simultaneous_discharge_hold),
        ("future_charge_does_not_satisfy_hold", test_alphaess_future_charge_does_not_satisfy_a_current_discharge_hold),
        ("snap_inward", test_alphaess_times_snap_inward_to_the_15_minute_grid),
        ("collapsed_disabled", test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped),
        ("midnight_end_snaps", test_alphaess_midnight_end_snaps_to_the_maximum),
        ("midnight_split", test_alphaess_period_two_carries_the_midnight_split),
        ("period_one_collapse_keeps_period_two", test_alphaess_period_one_collapse_does_not_disable_period_two),
        ("clamped_at_boundary", test_alphaess_reserve_is_clamped_at_the_api_boundary),
        ("full_replacement", test_alphaess_payload_is_a_full_replacement),
        ("payloads_equal_type_strict", test_alphaess_payloads_equal_is_type_strict_and_order_independent),
        ("settle_suppresses_stale", test_alphaess_settle_window_suppresses_a_stale_read_back),
        ("external_change_reported", test_alphaess_external_change_reported_after_the_settle_window),
        ("only_owned_fields", test_alphaess_only_predbat_owned_fields_count_as_interference),
        ("no_check_before_writing", test_alphaess_no_interference_check_before_predbat_has_written),
        ("intent_clearing_enables_reapply", test_alphaess_detected_change_clears_applied_payload_so_next_cycle_reapplies),
        ("settle_boundary_pins_equality", test_alphaess_settle_window_boundary_at_exactly_alphaess_settle_polls),
        ("discharge_direction_tested", test_alphaess_interference_detection_for_discharge_direction),
        ("absent_field_not_differing", test_alphaess_absent_owned_field_in_observed_does_not_count_as_differing),
        ("identical_not_rewritten", test_alphaess_identical_payload_is_not_rewritten),
        ("independent_gating", test_alphaess_charge_and_discharge_gated_independently),
        ("read_only_gate", test_alphaess_reconcile_is_gated_on_predbat_read_only),
        ("control_enable_gate", test_alphaess_reconcile_is_gated_on_control_enable),
        ("undriven_serial_skipped", test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive),
        ("min_write_interval", test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it),
        ("6053_backoff", test_alphaess_6053_backs_off_rather_than_counting_as_a_failure),
        ("6053_paces_the_retry", test_alphaess_6053_backs_off_the_retry_via_min_write_interval),
        ("persistent_rejection_paced", test_alphaess_persistently_rejected_write_is_paced_not_retried_every_tick),
        ("rejected_hold_charge_defers_discharge", test_alphaess_rejected_hold_charge_defers_legacy_discharge_update),
        ("held_write_not_applied", test_alphaess_held_write_is_not_reported_as_applied),
        ("same_cycle_correction_not_held", test_alphaess_same_cycle_correction_is_not_held_by_the_write_pacer),
        ("correction_burst_capped", test_alphaess_correction_burst_is_capped_so_pacing_still_bounds_the_write_budget),
        ("rejected_write_opens_no_burst", test_alphaess_rejected_write_does_not_open_a_correction_burst),
        ("settle_bounded_by_write_interval", test_alphaess_settle_window_never_outlasts_a_shortened_write_interval),
        ("write_button_not_forced", test_alphaess_write_button_is_not_forced),
        ("periodic_6017_cached", test_alphaess_periodic_6017_is_cached_and_never_retried),
        ("periodic_transient_unknown", test_alphaess_periodic_other_failures_leave_the_verdict_unknown),
        ("periodic_payload", test_alphaess_periodic_payload_shape),
        ("periodic_disabled_flag", test_alphaess_periodic_disabled_direction_uses_the_cycle_flag),
        ("periodic_no_overlap", test_alphaess_periodic_windows_do_not_overlap),
        ("periodic_charge_limit_floor", test_alphaess_periodic_charge_limit_floor_is_clamped),
        ("periodic_rate_zero_is_freeze", test_alphaess_periodic_rate_zero_is_freeze_not_demand_mode),
        ("periodic_both_rates_zero_holds", test_alphaess_periodic_both_rates_zero_still_holds_the_battery),
        ("periodic_reserve_reaches_idle_entry", test_alphaess_periodic_reserve_reaches_the_idle_discharge_entry),
        ("periodic_no_overlap_survives", test_alphaess_periodic_non_overlapping_windows_survive_intact),
        ("periodic_before_overlap_trims_end", test_alphaess_periodic_export_before_charge_overlap_trims_the_export_end),
        ("periodic_direction_interference", test_alphaess_periodic_direction_detects_interference),
        ("periodic_interference_ignores_cosmetic_diffs", test_alphaess_periodic_interference_ignores_key_order_float_limit_and_echoed_zero_power),
        ("periodic_interference_detects_genuine_change", test_alphaess_periodic_interference_still_detects_a_genuine_window_change),
        ("apply_settings_routes_periodic", test_alphaess_apply_settings_routes_entitled_systems_through_the_periodic_endpoint),
        ("unbind_switch_published", test_alphaess_unbind_switch_is_published_for_every_serial),
        ("unbind_switch_reflects_latch", test_alphaess_unbind_switch_reflects_an_already_latched_serial),
        ("unbind_latches", test_alphaess_unbind_latches_and_removes_the_serial),
        ("unbind_failure_retries", test_alphaess_failed_unbind_leaves_the_latch_clear_for_retry),
        ("unbind_toggle_off", test_alphaess_unbind_toggle_off_clears_the_latch),
        ("unbind_toggle_off_skips_save_when_unlatched", test_alphaess_unbind_toggle_off_skips_save_when_never_latched),
        ("unbind_not_read_only_gated", test_alphaess_unbind_is_not_gated_by_read_only),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

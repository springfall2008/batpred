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


def test_alphaess_update_local_schedule_applies_each_field():
    """Each control entity change lands on the right field of the held schedule."""
    failed = False
    client = _client()
    for entity, value, path in [
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
    """AlphaESS has no pause endpoint, so Predbat expresses freeze by zeroing the rates
    (execute.py:491-495). Zero is a distinct instruction, not just 'slow'."""
    failed = False
    client = _client()

    # charge_rate == 0 -> no grid charging, overriding the charge window (freeze charge,
    # and no cross-charging during an export).
    frozen_charge = _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 0, "start": "01:00:00", "end": "05:00:00"})
    charge = client.build_charge_payload("AL70", frozen_charge)
    if charge.get("gridCharge") != 0:
        print(f"ERROR: gridCharge {charge.get('gridCharge')} should be 0 when charge_rate is 0")
        failed = True

    # discharge_rate == 0 -> ctrDis 1 with BOTH periods disabled, so the battery holds SOC.
    frozen_export = _schedule(reserve=10, export_power=0)
    discharge = client.build_discharge_payload("AL70", frozen_export)
    if discharge.get("ctrDis") != 1:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should be 1 to hold SOC")
        failed = True
    for key in ("timeDisf1", "timeDise1", "timeDisf2", "timeDise2"):
        if discharge.get(key) != "00:00":
            print(f"ERROR: {key} = {discharge.get(key)} should be disabled to hold SOC")
            failed = True
    assert not failed, "test_alphaess_rate_zero_is_freeze"


def test_alphaess_both_rates_zero_still_holds_the_battery():
    """Both rates at zero is an ordinary, reachable hold - not evidence of "no plan".

    Predbat reaches this combination through set_freeze_export_during_demand zeroing the
    charge rate (execute.py:532/538 - AlphaESSCloud has has_timed_pause False, so the "else"
    branch fires) together with a car-charging-from-battery-disable (execute.py:564) or
    iboost_prevent_discharge (execute.py:591) hold zeroing the discharge rate in the same
    pass. Treating that as "no plan" would let the battery discharge into the EV or iBoost
    load, silently defeating the explicit hold Predbat asked for. Stranding a genuinely
    unconfigured system is prevented elsewhere, by the control_active gate in
    _reconcile_control (Task 10), which only re-applies once a serial's write button has
    been pressed.
    """
    failed = False
    client = _client()
    schedule = _schedule(reserve=15, charge_power=0, export_power=0)
    discharge = client.build_discharge_payload("AL70", schedule)
    if discharge.get("ctrDis") != 1:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should be 1 - both rates zero still means hold")
        failed = True
    for key in ("timeDisf1", "timeDise1", "timeDisf2", "timeDise2"):
        if discharge.get(key) != "00:00":
            print(f"ERROR: {key} = {discharge.get(key)} should be disabled to hold SOC")
            failed = True
    assert not failed, "test_alphaess_both_rates_zero_still_holds_the_battery"


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
        run_async_local(client.apply_settings("AL70", schedule))
        first_calls = session.return_value.post.call_count if hasattr(session.return_value, "post") else None
        run_async_local(client.apply_settings("AL70", schedule))
        second_calls = session.return_value.post.call_count if hasattr(session.return_value, "post") else None
    # Second identical apply must send nothing: the POST count must not move at all, not
    # merely end up holding the same cached value - a value can be identical either because
    # nothing was sent, or because something identical was sent again and just landed on the
    # same bytes, and only the call count tells those two apart.
    if second_calls != first_calls:
        print(f"ERROR: second identical apply sent {second_calls} - {first_calls} POST(s), should send zero")
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
    """control_enable false means monitoring only."""
    failed = False
    client = _writable()
    client.control_enable = False
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client._reconcile_control("AL70"))
    if client.applied_payload.get("AL70"):
        print(f"ERROR: wrote with control disabled: {client.applied_payload}")
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
    be applied on the next eligible tick - not lost."""
    failed = False
    client = _writable()
    client.min_write_interval = 300
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    first = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    second = _schedule(charge={"enable": True, "soc": 70, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", first))
        with patch("alphaess.time.time", return_value=1010.0):
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
    """Too-fast is a pacing signal, not a broken component."""
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
    assert not failed, "test_alphaess_6053_backs_off_rather_than_counting_as_a_failure"


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


def run_alphaess_control_tests(my_predbat):
    """Run all AlphaESS control-logic tests."""
    failed = False
    for name, fn in [
        ("entities_round_trip", test_alphaess_control_entities_round_trip),
        ("reserve_unclamped", test_alphaess_reserve_entity_is_published_unclamped),
        ("entity_routing", test_alphaess_entity_routing_does_not_confuse_prefixed_serials),
        ("update_local_schedule", test_alphaess_update_local_schedule_applies_each_field),
        ("fallback_on_unknown_or_unavailable", test_alphaess_fallback_on_unknown_or_unavailable_values),
        ("controls_pass_through", test_alphaess_controls_pass_straight_through),
        ("batusecap_is_reserve", test_alphaess_batusecap_is_the_reserve_outside_an_export_window),
        ("rate_zero_is_freeze", test_alphaess_rate_zero_is_freeze),
        ("both_rates_zero_still_holds", test_alphaess_both_rates_zero_still_holds_the_battery),
        ("snap_inward", test_alphaess_times_snap_inward_to_the_15_minute_grid),
        ("collapsed_disabled", test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped),
        ("midnight_end_snaps", test_alphaess_midnight_end_snaps_to_the_maximum),
        ("midnight_split", test_alphaess_period_two_carries_the_midnight_split),
        ("period_one_collapse_keeps_period_two", test_alphaess_period_one_collapse_does_not_disable_period_two),
        ("clamped_at_boundary", test_alphaess_reserve_is_clamped_at_the_api_boundary),
        ("full_replacement", test_alphaess_payload_is_a_full_replacement),
        ("payloads_equal_type_strict", test_alphaess_payloads_equal_is_type_strict_and_order_independent),
        ("identical_not_rewritten", test_alphaess_identical_payload_is_not_rewritten),
        ("independent_gating", test_alphaess_charge_and_discharge_gated_independently),
        ("read_only_gate", test_alphaess_reconcile_is_gated_on_predbat_read_only),
        ("control_enable_gate", test_alphaess_reconcile_is_gated_on_control_enable),
        ("undriven_serial_skipped", test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive),
        ("min_write_interval", test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it),
        ("6053_backoff", test_alphaess_6053_backs_off_rather_than_counting_as_a_failure),
        ("write_button_not_forced", test_alphaess_write_button_is_not_forced),
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

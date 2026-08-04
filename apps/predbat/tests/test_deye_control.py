# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE control-state derivation
# -----------------------------------------------------------------------------

"""Tests for the DEYE behaviour to work-mode derivation (``derive_control_state``)."""

from unittest.mock import patch
from deye_const import DEYE_WORKMODE, FREEZE_EXPORT_SOC, TOU_FIELD, TOU_SLOT_COUNT, TOU_FILLER_TIMES, DEYE_ORDER_MAX_POLLS
from tests.test_deye_api import MockDeye
from tests.test_infra import run_async as run_async_local


def _state(reserve=10, charge=None, export=None):
    """Build a schedule dict in the shape ``derive_control_state`` expects."""
    return {"reserve": reserve, "charge": charge or {"enable": False, "soc": 0, "power": 0}, "export": export or {"enable": False, "soc": 0, "power": 0}}


def test_derive_control_state_table():
    """Each Predbat intent maps to the correct DEYE control state (spec table)."""
    failed = False
    d = MockDeye()
    cases = [
        # name, schedule, current_soc, expect(behaviour, work_mode, grid_charge, solar_sell, slot_soc)
        ("charge", _state(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50, ("charge", DEYE_WORKMODE["zero_export_load"], True, False, 90)),
        ("freeze_charge", _state(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, ("freeze_charge", DEYE_WORKMODE["zero_export_load"], True, False, 50)),
        ("hold_charge", _state(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, ("hold_charge", DEYE_WORKMODE["zero_export_load"], False, False, 50)),
        ("export", _state(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, ("export", DEYE_WORKMODE["selling_first"], False, True, 20)),
        ("freeze_export", _state(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, ("freeze_export", DEYE_WORKMODE["selling_first"], False, True, FREEZE_EXPORT_SOC)),
        ("idle", _state(reserve=15), 60, ("idle", DEYE_WORKMODE["zero_export_load"], False, False, 15)),
    ]
    for name, sched, soc, exp in cases:
        r = d.derive_control_state(sched, soc)
        got = (r["behaviour"], r["work_mode"], r["grid_charge"], r["solar_sell"], r["slot_soc"])
        if got != exp:
            print(f"ERROR: {name} expected {exp} got {got}")
            failed = True
    assert not failed, "test_derive_control_state_table"


def test_build_tou_slots_charge_window():
    """A charge window produces exactly 6 ordered slots with a grid-charge segment."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    slots = d.build_tou_slots(sched, current_soc=40)
    if len(slots) != TOU_SLOT_COUNT:
        print(f"ERROR: expected {TOU_SLOT_COUNT} slots got {len(slots)}")
        failed = True
    else:
        times = [s[TOU_FIELD["time"]] for s in slots]
        if times != sorted(times):
            print(f"ERROR: slots not ordered {times}")
            failed = True
        charge_slots = [s for s in slots if s[TOU_FIELD["grid_charge"]] and s[TOU_FIELD["soc"]] == 95]
        if not charge_slots:
            print("ERROR: no grid-charge slot at soc 95")
            failed = True
        if slots[0][TOU_FIELD["time"]] != "00:00":
            print(f"ERROR: first slot must start 00:00 got {slots[0][TOU_FIELD['time']]}")
            failed = True
    assert not failed, "test_build_tou_slots_charge_window"


def test_build_tou_slots_times_are_distinct():
    """All 6 TOU slot start times must be unique and ascending (DEYE rejects duplicates)."""
    failed = False
    d = MockDeye()
    # A single short charge window leaves several padding slots — the old code
    # repeated the last slot's time, producing duplicates. Assert they're distinct.
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    for current_soc in (40, 95):
        slots = d.build_tou_slots(sched, current_soc=current_soc)
        times = [s[TOU_FIELD["time"]] for s in slots]
        if len(times) != TOU_SLOT_COUNT:
            print(f"ERROR: expected {TOU_SLOT_COUNT} slots got {len(times)}")
            failed = True
        if len(set(times)) != len(times):
            print(f"ERROR: duplicate slot start times: {times}")
            failed = True
        if times != sorted(times):
            print(f"ERROR: slot times not ascending: {times}")
            failed = True
    # An idle schedule (no windows) must also yield 6 distinct times.
    idle = {"reserve": 15, "charge": {"enable": False, "soc": 0, "power": 0}, "export": {"enable": False, "soc": 0, "power": 0}}
    idle_times = [s[TOU_FIELD["time"]] for s in d.build_tou_slots(idle, current_soc=50)]
    if len(set(idle_times)) != TOU_SLOT_COUNT:
        print(f"ERROR: idle schedule produced non-distinct times: {idle_times}")
        failed = True
    assert not failed, "test_build_tou_slots_times_are_distinct"


def test_build_dynamic_payload_and_equality():
    """Payload carries work mode + on/off actions + 6 slots; equality ignores deviceSn."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    p1 = d.build_dynamic_payload("INV1", sched, current_soc=40)
    p2 = d.build_dynamic_payload("INV2", sched, current_soc=40)
    if p1.get("deviceSn") != "INV1":
        print("ERROR: deviceSn not set")
        failed = True
    if len(p1.get("timeUseSettingItems", [])) != 6:
        print("ERROR: payload must carry 6 slots")
        failed = True
    if p1.get("gridChargeAction") not in ("on", "off"):
        print(f"ERROR: gridChargeAction {p1.get('gridChargeAction')}")
        failed = True
    if not d.payloads_equal(p1, p2):
        print("ERROR: payloads differing only by deviceSn should be equal")
        failed = True
    assert not failed, "test_build_dynamic_payload_and_equality"


def test_apply_dynamic_control_suppresses_when_unchanged():
    """No write when the desired payload equals the last-applied cached payload."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    # Seed the cache with what we would compute now, so a re-apply is a no-op.
    d.applied_payload["INV1"] = d.build_dynamic_payload("INV1", sched, 40)
    posts = []

    async def fake_post(endpoint_key, body):
        """Record the endpoint key posted to and return a success stub."""
        posts.append(endpoint_key)
        return {"success": True, "orderId": 1}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", sched, 40))
    if wrote:
        print("ERROR: should not write when unchanged")
        failed = True
    if "dynamic_control" in posts:
        print("ERROR: dynamic_control was posted despite no change")
        failed = True
    assert not failed, "test_apply_dynamic_control_suppresses_when_unchanged"


def test_apply_dynamic_control_writes_and_caches_on_change():
    """A changed payload is written, orderId recorded and payload cached."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}

    async def fake_post(endpoint_key, body):
        """Return a success stub with an orderId, ignoring the actual body."""
        return {"success": True, "orderId": 7}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", sched, 40))
    if not wrote:
        print("ERROR: expected a write on first apply")
        failed = True
    if d.pending_orders.get("INV1") != 7:
        print(f"ERROR: orderId not recorded: {d.pending_orders}")
        failed = True
    if "INV1" not in d.applied_payload:
        print("ERROR: applied payload not cached")
        failed = True
    assert not failed, "test_apply_dynamic_control_writes_and_caches_on_change"


def test_poll_order_success():
    """poll_order clears a successful order via GET /order/{orderId}."""
    failed = False
    d = MockDeye()
    d.pending_orders["INV1"] = 42
    seen = []

    async def fake_get(path):
        """Record the polled path and return a success stub."""
        seen.append(path)
        return {"success": True, "connectionStatus": 1}

    with patch.object(d, "_get", side_effect=fake_get):
        status = run_async_local(d.poll_order("INV1"))
    if status != "success":
        print(f"ERROR: status {status}")
        failed = True
    if not seen or not seen[0].endswith("/order/42"):
        print(f"ERROR: wrong poll path {seen}")
        failed = True
    if "INV1" in d.pending_orders:
        print("ERROR: successful order should be cleared")
        failed = True
    assert not failed, "test_poll_order_success"


def test_active_workmode_follows_time():
    """Top-level workMode follows the window active NOW, so a charge period isn't defeated by an export window."""
    failed = False
    d = MockDeye()
    # A day with BOTH a grid-charge window (02:00-05:00) and an export window (18:00-20:00) enabled.
    sched = {
        "reserve": 10,
        "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"},
        "export": {"enable": True, "soc": 20, "power": 3000, "start": "18:00", "end": "20:00"},
    }
    from deye_const import DEYE_WORKMODE

    cases = [
        # now_minutes, expected work_mode, gridChargeAction, solarSellAction
        (3 * 60, DEYE_WORKMODE["zero_export_load"], "on", "off"),  # 03:00 -> in charge window
        (19 * 60, DEYE_WORKMODE["selling_first"], "off", "on"),  # 19:00 -> in export window
        (12 * 60, DEYE_WORKMODE["zero_export_load"], "off", "off"),  # 12:00 -> idle (neither window)
    ]
    for now_minutes, exp_mode, exp_grid, exp_sell in cases:
        payload = d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=now_minutes)
        got = (payload["workMode"], payload["gridChargeAction"], payload["solarSellAction"])
        if got != (exp_mode, exp_grid, exp_sell):
            print(f"ERROR: now={now_minutes} expected {(exp_mode, exp_grid, exp_sell)} got {got}")
            failed = True
    assert not failed, "test_active_workmode_follows_time"


def test_reconcile_only_controlled_inverters():
    """_reconcile_control re-applies only inverters Predbat already controls, with change-detection (force=False)."""
    failed = False
    d = MockDeye()
    d.device_list = ["INV1", "INV2"]
    d.control_active = {"INV1"}  # INV2 not yet driven by Predbat
    d.local_schedule = {"INV1": {"reserve": 10, "charge": {"enable": False}, "export": {"enable": False}}, "INV2": {"reserve": 10, "charge": {"enable": False}, "export": {"enable": False}}}
    d.device_values = {"INV1": {"soc": 50}, "INV2": {"soc": 50}}
    calls = []

    async def fake_apply(sn, schedule, current_soc, force=False):
        """Record reconcile applies."""
        calls.append((sn, force))
        return False

    with patch.object(d, "apply_dynamic_control", side_effect=fake_apply):
        run_async_local(d._reconcile_control())
    if calls != [("INV1", False)]:
        print(f"ERROR: reconcile should apply only the controlled inverter with force=False: {calls}")
        failed = True
    assert not failed, "test_reconcile_only_controlled_inverters"


def test_poll_order_empty_response_stays_pending():
    """An empty/error response (network/auth) must NOT falsely confirm the order."""
    failed = False
    d = MockDeye()
    d.pending_orders["INV1"] = 42

    async def fake_get(path):
        """Simulate _get returning {} on a network/auth error."""
        return {}

    with patch.object(d, "_get", side_effect=fake_get):
        status = run_async_local(d.poll_order("INV1"))
    if status != "pending":
        print(f"ERROR: empty response should be pending, got {status}")
        failed = True
    if d.pending_orders.get("INV1") != 42:
        print("ERROR: order must NOT be cleared on an empty/error response")
        failed = True
    assert not failed, "test_poll_order_empty_response_stays_pending"


async def _fake_run_step_sn(sn):
    """No-op stand-in for a per-inverter run() step (fetch_battery_config, fetch_device_data, ...)."""
    return {}


async def _fake_run_step():
    """No-op stand-in for a run() step taking no arguments (publish_data)."""
    return None


def _patched_run(d, poll_status):
    """Run one DEYE run() cycle with per-inverter I/O stubbed and poll_order fixed to poll_status."""
    # These tests are about draining control orders on a component that has already
    # discovered its inverters, so the static tier starts fresh; without this run() would
    # re-run discovery over the network before ever reaching the drain loop.
    d.mark_refreshed("static")

    async def fake_poll_order(sn):
        """Return the fixed status for every call, mirroring the real poll_order's pop-on-success side effect."""
        if poll_status == "success":
            d.pending_orders.pop(sn, None)
        return poll_status

    with patch.multiple(
        d,
        fetch_battery_config=_fake_run_step_sn,
        fetch_device_data=_fake_run_step_sn,
        get_schedule_settings_ha=_fake_run_step_sn,
        publish_data=_fake_run_step,
        publish_schedule_settings_ha=_fake_run_step_sn,
        poll_order=fake_poll_order,
    ):
        return run_async_local(d.run(0, False))


def test_run_forces_rewrite_after_max_unconfirmed_polls():
    """A pending order that never reports success is dropped after DEYE_ORDER_MAX_POLLS run() cycles, invalidating the applied-payload cache so the next apply re-writes."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "tok"
    d.device_list = ["INV1"]
    d.pending_orders["INV1"] = 42
    d.applied_payload["INV1"] = {"deviceSn": "INV1", "workMode": "ZERO_EXPORT_TO_LOAD"}

    for i in range(DEYE_ORDER_MAX_POLLS - 1):
        _patched_run(d, "pending")
        if d.order_poll_count.get("INV1") != i + 1:
            print(f"ERROR: after poll {i + 1} expected count {i + 1}, got {d.order_poll_count.get('INV1')}")
            failed = True
        if "INV1" not in d.pending_orders or "INV1" not in d.applied_payload:
            print(f"ERROR: order/cache dropped too early after poll {i + 1}")
            failed = True

    _patched_run(d, "pending")
    if "INV1" in d.pending_orders:
        print(f"ERROR: pending order should be dropped after {DEYE_ORDER_MAX_POLLS} polls: {d.pending_orders}")
        failed = True
    if "INV1" in d.order_poll_count:
        print(f"ERROR: poll count should be reset after drop: {d.order_poll_count}")
        failed = True
    if "INV1" in d.applied_payload:
        print(f"ERROR: applied_payload cache should be invalidated after drop: {d.applied_payload}")
        failed = True
    assert not failed, "test_run_forces_rewrite_after_max_unconfirmed_polls"


def test_run_clears_pending_order_and_count_on_success():
    """A run() cycle whose poll_order reports success clears both the pending order and the poll count, and leaves the applied-payload cache untouched."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "tok"
    d.device_list = ["INV1"]
    d.pending_orders["INV1"] = 7
    d.order_poll_count["INV1"] = 2
    d.applied_payload["INV1"] = {"deviceSn": "INV1", "workMode": "ZERO_EXPORT_TO_LOAD"}

    _patched_run(d, "success")

    if "INV1" in d.pending_orders:
        print(f"ERROR: successful order should be cleared: {d.pending_orders}")
        failed = True
    if "INV1" in d.order_poll_count:
        print(f"ERROR: poll count should be reset on success: {d.order_poll_count}")
        failed = True
    if "INV1" not in d.applied_payload:
        print("ERROR: applied_payload cache should not be touched on success")
        failed = True
    assert not failed, "test_run_clears_pending_order_and_count_on_success"


def test_window_times_reach_the_slots_in_deye_format():
    """A HH:MM:SS window becomes HH:MM slot times and actually produces a window.

    Live regression: every control payload for two hours carried only the filler times
    (00:00/04:00/08:00/12:00/16:00/20:00) with grid charge off everywhere — the signature of
    build_tou_slots seeing no window at all — because Predbat was writing the window times
    to dummy entities this component never reads.
    """
    failed = False
    d = MockDeye()
    sched = {
        "reserve": 14,
        "charge": {"enable": True, "soc": 95, "power": 3000, "start": "05:00:00", "end": "05:30:00"},
        "export": {"enable": False, "soc": 0, "power": 0},
    }
    slots = d.build_tou_slots(sched, current_soc=40)
    times = [s[TOU_FIELD["time"]] for s in slots]

    if any(len(t) != 5 or t.count(":") != 1 for t in times):
        print(f"ERROR: DEYE slot times must be HH:MM, got {times}")
        failed = True
    if "05:00" not in times:
        print(f"ERROR: the charge window start never reached the slots: {times}")
        failed = True
    if not any(s[TOU_FIELD["grid_charge"]] and s[TOU_FIELD["soc"]] == 95 for s in slots):
        print(f"ERROR: no grid-charge slot was produced for the window: {slots}")
        failed = True
    # The all-filler payload is exactly what the bug produced, so assert we are not back there
    if times == TOU_FILLER_TIMES[:TOU_SLOT_COUNT]:
        print(f"ERROR: slots are pure filler, the window was lost: {times}")
        failed = True

    # A HH:MM window still works, so a stale entity value cannot break the payload
    sched["charge"]["start"] = "05:00"
    sched["charge"]["end"] = "05:30"
    if "05:00" not in [s[TOU_FIELD["time"]] for s in d.build_tou_slots(sched, current_soc=40)]:
        print("ERROR: a HH:MM window should still resolve")
        failed = True
    assert not failed, "test_window_times_reach_the_slots_in_deye_format"


def test_to_slot_time_normalisation():
    """Schedule times are reduced to the HH:MM DEYE requires, tolerating bad input."""
    failed = False
    d = MockDeye()
    cases = [("05:00:00", "05:00"), ("05:30", "05:30"), ("5:3", "05:03"), ("23:59:59", "23:59"), ("", "00:00"), (None, "00:00"), ("garbage", "00:00"), ("12", "00:00")]
    for value, want in cases:
        got = d._to_slot_time(value)
        if got != want:
            print(f"ERROR: _to_slot_time({value!r}) expected {want!r}, got {got!r}")
            failed = True
    assert not failed, "test_to_slot_time_normalisation"


def test_repeated_write_button_presses_do_not_resend_an_unchanged_payload():
    """Predbat presses the write button every cycle, so it must not force a write.

    Live regression: 40 button presses produced 36 byte-identical control orders across two
    hours with nothing changing, because apply_schedule defaulted to force=True and bypassed
    the applied-payload cache. Over the same period _reconcile_control, which is unforced,
    correctly suppressed 119 writes — so the cache was working; only the button ignored it.
    """
    failed = False
    d = MockDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 99.0}}
    d.local_schedule["INV1"] = {"reserve": 14, "charge": {"enable": False, "soc": 0, "power": 0}, "export": {"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000, "start": "16:00", "end": "23:30"}}
    posts = []

    async def fake_post(endpoint_key, body):
        """Record every control POST that reaches the API."""
        posts.append(endpoint_key)
        return {"success": True, "orderId": len(posts)}

    async def fake_read(sn):
        """Return the unchanging schedule, as the HA entities would."""
        return d.local_schedule["INV1"]

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "get_schedule_settings_ha", side_effect=fake_read):
            for _ in range(5):
                # Clear the order the previous press raised, as poll_order does on success,
                # so the in-flight guard is not what suppresses the repeats.
                d.pending_orders.pop("INV1", None)
                run_async_local(d.switch_event("switch.predbat_deye_inv1_battery_schedule_charge_write", "turn_on"))

    if len(posts) != 1:
        print(f"ERROR: an unchanged schedule should be written once, got {len(posts)} writes")
        failed = True
    if not any("control unchanged" in m for m in d.log_messages):
        print(f"ERROR: expected the repeats to be suppressed as unchanged: {d.log_messages}")
        failed = True
    assert not failed, "test_repeated_write_button_presses_do_not_resend_an_unchanged_payload"


def test_write_button_still_writes_when_the_schedule_changes():
    """Suppression must not swallow a genuine change."""
    failed = False
    d = MockDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    d.local_schedule["INV1"] = {"reserve": 14, "charge": {"enable": False, "soc": 0, "power": 0}, "export": {"enable": False, "soc": 0, "power": 0}}
    posts = []

    async def fake_post(endpoint_key, body):
        """Record every control POST that reaches the API."""
        posts.append(body)
        return {"success": True, "orderId": len(posts)}

    async def fake_read(sn):
        """Return the current schedule, as the HA entities would."""
        return d.local_schedule["INV1"]

    with patch.object(d, "_post", side_effect=fake_post):
        with patch.object(d, "get_schedule_settings_ha", side_effect=fake_read):
            run_async_local(d.switch_event("switch.predbat_deye_inv1_battery_schedule_charge_write", "turn_on"))
            d.pending_orders.pop("INV1", None)
            # A real change: a charge window opens
            d.local_schedule["INV1"]["charge"] = {"enable": True, "soc": 90, "power": 3000, "start": "01:00", "end": "05:00"}
            run_async_local(d.switch_event("switch.predbat_deye_inv1_battery_schedule_charge_write", "turn_on"))

    if len(posts) != 2:
        print(f"ERROR: a changed schedule must be written, got {len(posts)} writes")
        failed = True
    assert not failed, "test_write_button_still_writes_when_the_schedule_changes"


def test_slot_soc_never_goes_below_the_inverter_floor():
    """Slot SOC is clamped to config/battery battLowCapacity, whatever the schedule says.

    Live regression: the first control write of a cycle went out with slot SOC 0 on a pack
    whose installer floor is 14%, because Predbat's reserve entity reads 0 until it has
    written the real value. Mirrors fox's max(value, fdsoc_min) clamp.
    """
    failed = False
    d = MockDeye()
    d.device_battery_config["INV1"] = {"battLowCapacity": 14}
    idle = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0}, "export": {"enable": False, "soc": 0, "power": 0}}
    payload = d.build_dynamic_payload("INV1", idle, current_soc=98)
    socs = [s[TOU_FIELD["soc"]] for s in payload["timeUseSettingItems"]]
    if any(s < 14 for s in socs):
        print(f"ERROR: slot SOC below the 14% floor: {socs}")
        failed = True

    # An explicit target above the floor is untouched
    export = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0}, "export": {"enable": True, "soc": 24, "power": 3000, "start": "18:00", "end": "23:30"}}
    socs = [s[TOU_FIELD["soc"]] for s in d.build_dynamic_payload("INV1", export, current_soc=98)["timeUseSettingItems"]]
    if 24 not in socs:
        print(f"ERROR: an above-floor target should survive: {socs}")
        failed = True
    if any(s < 14 for s in socs):
        print(f"ERROR: slot SOC below the floor in the export case: {socs}")
        failed = True

    # With no config/battery there is no known floor, so nothing is clamped
    d2 = MockDeye()
    socs = [s[TOU_FIELD["soc"]] for s in d2.build_dynamic_payload("INV1", idle, current_soc=98)["timeUseSettingItems"]]
    if any(s != 0 for s in socs):
        print(f"ERROR: without a known floor the schedule should pass through: {socs}")
        failed = True
    assert not failed, "test_slot_soc_never_goes_below_the_inverter_floor"


def test_battery_reserve_min_reads_the_configured_floor():
    """The floor comes from battLowCapacity, defaulting to 0 when unknown or nonsensical."""
    failed = False
    d = MockDeye()
    if d.battery_reserve_min("INV1") != 0:
        print("ERROR: with no config/battery the floor must be 0")
        failed = True
    d.device_battery_config["INV1"] = {"battLowCapacity": 14}
    if d.battery_reserve_min("INV1") != 14:
        print(f"ERROR: expected 14, got {d.battery_reserve_min('INV1')}")
        failed = True
    for bad in (0, -5, 150):
        d.device_battery_config["INV1"] = {"battLowCapacity": bad}
        if d.battery_reserve_min("INV1") != 0:
            print(f"ERROR: {bad} is not a usable floor, expected 0")
            failed = True
    assert not failed, "test_battery_reserve_min_reads_the_configured_floor"


def _schedule():
    """Return a minimal schedule shape for a control write."""
    return {"reserve": 10, "charge": {"enable": True, "soc": 100, "power": 3000, "start": "01:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}


def test_control_write_deferred_while_an_order_is_in_flight():
    """DEYE runs one order per device, so a write is deferred rather than sent and rejected.

    Live log: apply_schedule submitted an order at 20:09:39 and apply_reserve_live posted
    another 5ms later, which DEYE refused with 2104004 "command concurrent running". Five
    of twelve commands in that run were rejected this way.
    """
    failed = False
    d = MockDeye()
    d.device_list = ["INV1"]
    d.pending_orders["INV1"] = 832017929888709
    posted = []

    async def fake_post(endpoint_key, body):
        """Record any control POST that gets through."""
        posted.append(endpoint_key)
        return {"success": True, "orderId": 1}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", _schedule(), 50, force=True))

    if wrote:
        print("ERROR: a write must not be attempted while an order is in flight")
        failed = True
    if posted:
        print(f"ERROR: nothing should have been POSTed, got {posted}")
        failed = True
    if not any("still in flight" in m for m in d.log_messages):
        print(f"ERROR: expected a deferral log line: {d.log_messages}")
        failed = True
    # applied_payload must be left alone so the next cycle still sees the difference
    if "INV1" in d.applied_payload:
        print("ERROR: a deferred write must not record an applied payload")
        failed = True
    assert not failed, "test_control_write_deferred_while_an_order_is_in_flight"


def test_control_write_proceeds_once_the_order_clears():
    """With no order outstanding the same write goes through."""
    failed = False
    d = MockDeye()
    d.device_list = ["INV1"]
    posted = []

    async def fake_post(endpoint_key, body):
        """Accept the control POST."""
        posted.append(endpoint_key)
        return {"success": True, "orderId": 99}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", _schedule(), 50, force=True))

    if not wrote or posted != ["dynamic_control"]:
        print(f"ERROR: expected the write to proceed, wrote={wrote} posted={posted}")
        failed = True
    if d.pending_orders.get("INV1") != 99:
        print(f"ERROR: the new order should be tracked: {d.pending_orders}")
        failed = True
    assert not failed, "test_control_write_proceeds_once_the_order_clears"


def test_busy_response_detection():
    """A busy rejection is recognised by code or message, and nothing else is."""
    failed = False
    d = MockDeye()
    busy = [
        {"success": False, "code": "2104004", "msg": "command concurrent running"},
        {"success": False, "code": "", "msg": "Command Concurrent Running"},
        {"success": False, "code": "2104004", "msg": ""},
    ]
    for body in busy:
        if not d.is_busy_response(body):
            print(f"ERROR: expected busy for {body}")
            failed = True
    other = [
        {"success": True, "code": "2104004"},
        {"success": False, "msg": "device offline"},
        {"success": False, "code": "2106001", "msg": "config point not supported"},
        {},
        None,
    ]
    for body in other:
        if d.is_busy_response(body):
            print(f"ERROR: did not expect busy for {body}")
            failed = True
    assert not failed, "test_busy_response_detection"


def test_busy_rejection_is_not_logged_as_a_failure():
    """A busy rejection is back-pressure, so it must not be reported as a control failure."""
    failed = False
    d = MockDeye()
    d.device_list = ["INV1"]

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: the device is already running an order."""
        return {"success": False, "code": "2104004", "msg": "command concurrent running"}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", _schedule(), 50, force=True))

    if wrote:
        print("ERROR: a busy rejection is not a successful write")
        failed = True
    if any("dynamic control failed" in m for m in d.log_messages):
        print(f"ERROR: busy must not be logged as a failure: {d.log_messages}")
        failed = True
    if not any("busy running a control order" in m for m in d.log_messages):
        print(f"ERROR: expected a busy log line: {d.log_messages}")
        failed = True
    if "INV1" in d.applied_payload:
        print("ERROR: a rejected write must not record an applied payload")
        failed = True
    assert not failed, "test_busy_rejection_is_not_logged_as_a_failure"


def run_deye_control_tests(my_predbat):
    """Run all DEYE control-logic tests."""
    failed = False
    for name, fn in [
        ("derive_table", test_derive_control_state_table),
        ("tou_slots", test_build_tou_slots_charge_window),
        ("tou_slots_distinct", test_build_tou_slots_times_are_distinct),
        ("payload", test_build_dynamic_payload_and_equality),
        ("apply_suppress", test_apply_dynamic_control_suppresses_when_unchanged),
        ("apply_write", test_apply_dynamic_control_writes_and_caches_on_change),
        ("active_workmode_time", test_active_workmode_follows_time),
        ("reconcile_controlled", test_reconcile_only_controlled_inverters),
        ("poll_order", test_poll_order_success),
        ("poll_order_empty_pending", test_poll_order_empty_response_stays_pending),
        ("run_forces_rewrite_after_max_polls", test_run_forces_rewrite_after_max_unconfirmed_polls),
        ("run_clears_on_success", test_run_clears_pending_order_and_count_on_success),
        ("window_times_reach_slots", test_window_times_reach_the_slots_in_deye_format),
        ("to_slot_time", test_to_slot_time_normalisation),
        ("write_button_no_resend", test_repeated_write_button_presses_do_not_resend_an_unchanged_payload),
        ("write_button_writes_on_change", test_write_button_still_writes_when_the_schedule_changes),
        ("slot_soc_floor", test_slot_soc_never_goes_below_the_inverter_floor),
        ("battery_reserve_min", test_battery_reserve_min_reads_the_configured_floor),
        ("control_deferred_in_flight", test_control_write_deferred_while_an_order_is_in_flight),
        ("control_proceeds_when_clear", test_control_write_proceeds_once_the_order_clears),
        ("busy_response_detection", test_busy_response_detection),
        ("busy_not_a_failure", test_busy_rejection_is_not_logged_as_a_failure),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

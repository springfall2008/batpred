# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE control-state derivation
# -----------------------------------------------------------------------------

"""Tests for the DEYE behaviour to work-mode derivation (``derive_control_state``)."""

from unittest.mock import patch
from deye_const import DEYE_WORKMODE, FREEZE_EXPORT_SOC, TOU_FIELD, TOU_SLOT_COUNT, DEYE_ORDER_MAX_POLLS
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

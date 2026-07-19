# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE control-state derivation
# -----------------------------------------------------------------------------

"""Tests for the DEYE behaviour to work-mode derivation (``derive_control_state``)."""

from unittest.mock import patch
from deye_const import DEYE_WORKMODE, FREEZE_EXPORT_SOC, TOU_FIELD, TOU_SLOT_COUNT
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

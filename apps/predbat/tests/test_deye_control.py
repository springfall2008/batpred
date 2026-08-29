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
from deye_const import CONFIG_BATTERY_KEYS, DEYE_TOU_DAYS
from tests.test_deye_api import MockDeye, MOCK_RATED_POWER
from tests.test_infra import run_async as run_async_local


def _state(reserve=10, charge=None, export=None, charge_power=3000, export_power=3000):
    """Build a schedule dict in the shape ``derive_control_state`` expects.

    A DISABLED window still carries a power, because the real control entities do:
    adjust_charge_rate/adjust_discharge_rate write Predbat's rates every cycle whether or
    not a window is enabled. That matters because a zero charge rate against a non-zero
    export rate is precisely how Predbat signals Freeze Export (see derive_control_state),
    so a fixture leaving both at zero would silently be testing a freeze rather than the
    demand state it reads as.
    """
    return {
        "reserve": reserve,
        "charge": charge or {"enable": False, "soc": 0, "power": charge_power},
        "export": export or {"enable": False, "soc": 0, "power": export_power},
    }


def test_derive_control_state_table():
    """Each Predbat intent maps to the correct DEYE control state (spec table)."""
    failed = False
    d = MockDeye()
    cases = [
        # name, schedule, current_soc, expect(behaviour, work_mode, grid_charge, solar_sell, slot_soc)
        ("charge", _state(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50, ("charge", DEYE_WORKMODE["zero_export_ct"], True, False, 90)),
        ("freeze_charge", _state(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, ("freeze_charge", DEYE_WORKMODE["zero_export_ct"], True, False, 50)),
        ("hold_charge", _state(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, ("hold_charge", DEYE_WORKMODE["zero_export_ct"], False, False, 50)),
        ("export", _state(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, ("export", DEYE_WORKMODE["selling_first"], False, True, 20)),
        # The cap is the RESERVE, never the 99 sentinel: 99 marks the window as a freeze,
        # it is not a battery level, and writing it through told a Selling First inverter to
        # drive the battery to 99%. The zero sell rate is what makes a cap below the SoC
        # safe - see _freeze_export_state for the live Sunsynk runs that settled this.
        ("freeze_export", _state(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, ("freeze_export", DEYE_WORKMODE["selling_first"], False, True, 10)),
        # The signal Predbat actually sends: both windows off, charge rate zeroed, export
        # rate left alone. Without this the freeze never reached the inverter at all.
        ("freeze_export_by_rate", _state(reserve=10, charge_power=0), 80, ("freeze_export", DEYE_WORKMODE["selling_first"], False, True, 10)),
        # Both rates zero is an absence of a plan, not a freeze.
        ("no_rates_is_not_a_freeze", _state(reserve=15, charge_power=0, export_power=0), 60, ("idle", DEYE_WORKMODE["zero_export_ct"], False, False, 15)),
        ("idle", _state(reserve=15), 60, ("idle", DEYE_WORKMODE["zero_export_ct"], False, False, 15)),
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
    slots = d.build_tou_slots(sched, current_soc=40, self_use_power=MOCK_RATED_POWER)
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
        slots = d.build_tou_slots(sched, current_soc=current_soc, self_use_power=MOCK_RATED_POWER)
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
    idle = {"reserve": 15, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    idle_times = [s[TOU_FIELD["time"]] for s in d.build_tou_slots(idle, current_soc=50, self_use_power=MOCK_RATED_POWER)]
    if len(set(idle_times)) != TOU_SLOT_COUNT:
        print(f"ERROR: idle schedule produced non-distinct times: {idle_times}")
        failed = True
    assert not failed, "test_build_tou_slots_times_are_distinct"


def test_build_dynamic_payload_and_equality():
    """Payload carries work mode + on/off actions + 6 slots; equality ignores deviceSn."""
    failed = False
    d = MockDeye().with_rating("INV1", "INV2")
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
    d = MockDeye().with_rating("INV1")
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
    d = MockDeye().with_rating("INV1")
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
    d = MockDeye().with_rating("INV1")
    # A day with BOTH a grid-charge window (02:00-05:00) and an export window (18:00-20:00) enabled.
    sched = {
        "reserve": 10,
        "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"},
        "export": {"enable": True, "soc": 20, "power": 3000, "start": "18:00", "end": "20:00"},
    }
    from deye_const import DEYE_WORKMODE

    cases = [
        # now_minutes, expected work_mode, gridChargeAction, solarSellAction. solarSellAction
        # is always on: it governs surplus PV, not the battery (see test_solar_sell_is_always_on).
        (3 * 60, DEYE_WORKMODE["zero_export_ct"], "on", "on"),  # 03:00 -> in charge window
        (19 * 60, DEYE_WORKMODE["selling_first"], "off", "on"),  # 19:00 -> in export window
        (12 * 60, DEYE_WORKMODE["zero_export_ct"], "off", "on"),  # 12:00 -> idle (neither window)
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


def test_reconcile_control_skips_while_read_only():
    """_reconcile_control must not write to the inverter while switch.predbat_set_read_only is on."""
    failed = False
    d = MockDeye()
    d.device_list = ["INV1"]
    d.control_active = {"INV1"}
    d.local_schedule = {"INV1": {"reserve": 10, "charge": {"enable": False}, "export": {"enable": False}}}
    d.device_values = {"INV1": {"soc": 50}}
    d.base.get_state_wrapper.return_value = "on"
    calls = []

    async def fake_apply(sn, schedule, current_soc, force=False):
        """Record reconcile applies."""
        calls.append((sn, force))
        return False

    with patch.object(d, "apply_dynamic_control", side_effect=fake_apply):
        run_async_local(d._reconcile_control())
    if calls:
        print(f"ERROR: reconcile must not write while read-only is on: {calls}")
        failed = True
    assert not failed, "test_reconcile_control_skips_while_read_only"


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
    slots = d.build_tou_slots(sched, current_soc=40, self_use_power=MOCK_RATED_POWER)
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
    if "05:00" not in [s[TOU_FIELD["time"]] for s in d.build_tou_slots(sched, current_soc=40, self_use_power=MOCK_RATED_POWER)]:
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
    d = MockDeye().with_rating("INV1")
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 99.0}}
    d.local_schedule["INV1"] = {"reserve": 14, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000, "start": "16:00", "end": "23:30"}}
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
    d = MockDeye().with_rating("INV1")
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    d.local_schedule["INV1"] = {"reserve": 14, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
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
    d = MockDeye().with_rating("INV1")
    d.device_battery_config["INV1"] = {"battLowCapacity": 14}
    idle = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    payload = d.build_dynamic_payload("INV1", idle, current_soc=98)
    socs = [s[TOU_FIELD["soc"]] for s in payload["timeUseSettingItems"]]
    if any(s < 14 for s in socs):
        print(f"ERROR: slot SOC below the 14% floor: {socs}")
        failed = True

    # An explicit target above the floor is untouched
    export = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": True, "soc": 24, "power": 3000, "start": "18:00", "end": "23:30"}}
    socs = [s[TOU_FIELD["soc"]] for s in d.build_dynamic_payload("INV1", export, current_soc=98)["timeUseSettingItems"]]
    if 24 not in socs:
        print(f"ERROR: an above-floor target should survive: {socs}")
        failed = True
    if any(s < 14 for s in socs):
        print(f"ERROR: slot SOC below the floor in the export case: {socs}")
        failed = True

    # With no config/battery there is no known floor, so nothing is clamped
    d2 = MockDeye().with_rating("INV1")
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
    d = MockDeye().with_rating("INV1")
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
    d = MockDeye().with_rating("INV1")
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
    d = MockDeye().with_rating("INV1")
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


def test_zero_length_window_produces_no_action_slot():
    """An enabled window whose start equals its end must not emit an unterminated action slot.

    Live hazard (#4560): the control entities are written one at a time and the schedule
    default is start = end = "00:00:00", so an enable event arriving before the times do
    leaves exactly this state. The action segment used to be added with no matching
    return-to-self-use segment — setdefault() cannot add a key the action has just taken —
    so the slot ran on until the next boundary, up to four hours of full-power grid charge,
    while _window_active() correctly reported the window inactive.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    idle = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    baseline = d.build_tou_slots(idle, current_soc=20, self_use_power=MOCK_RATED_POWER)
    for direction, window in (
        ("charge", {"enable": True, "soc": 95, "power": 3000, "start": "00:00:00", "end": "00:00:00"}),
        ("export", {"enable": True, "soc": 20, "power": 3000, "start": "06:00", "end": "06:00"}),
    ):
        sched = dict(idle)
        sched[direction] = window
        slots = d.build_tou_slots(sched, current_soc=20, self_use_power=MOCK_RATED_POWER)
        if slots != baseline:
            print(f"ERROR: a zero-length {direction} window must leave the day in self-use, got {slots}")
            failed = True
    # The payload the inverter would actually receive must agree with _window_active, which
    # has always reported a zero-length window inactive.
    sched = dict(idle)
    sched["charge"] = {"enable": True, "soc": 95, "power": 3000, "start": "00:00:00", "end": "00:00:00"}
    payload = d.build_dynamic_payload("INV1", sched, current_soc=20, now_minutes=30)
    if any(slot[TOU_FIELD["grid_charge"]] for slot in payload["timeUseSettingItems"]):
        print(f"ERROR: a zero-length charge window still grid-charges: {payload['timeUseSettingItems']}")
        failed = True
    if payload["gridChargeAction"] != "off":
        print(f"ERROR: gridChargeAction should be off for an inactive window, got {payload['gridChargeAction']}")
        failed = True
    assert not failed, "test_zero_length_window_produces_no_action_slot"


def test_solar_sell_is_always_on():
    """solarSellAction is written on in every state — it governs surplus PV, not the battery.

    Regression (#4580): derived from the active window, it was off except inside an export
    window, so a user with an ordinary overnight charge window had surplus solar curtailed
    all through the following day — invisible to Predbat, whose model has no notion of PV
    curtailment, so the lost export revenue never shows up anywhere.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    idle = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    charge = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    export = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": True, "soc": 20, "power": 3000, "start": "16:00", "end": "19:00"}}
    for name, sched, now_minutes in (("idle midday", idle, 12 * 60), ("charging 03:00", charge, 3 * 60), ("charge set, midday idle", charge, 12 * 60), ("exporting 17:00", export, 17 * 60)):
        payload = d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=now_minutes)
        if payload["solarSellAction"] != "on":
            print(f"ERROR: {name} wrote solarSellAction={payload['solarSellAction']!r}, so surplus PV would be curtailed")
            failed = True
    assert not failed, "test_solar_sell_is_always_on"


def test_non_export_states_measure_at_the_grid_ct():
    """Non-export states use ZERO_EXPORT_TO_CT so the battery can serve the whole house.

    ZERO_EXPORT_TO_LOAD measures at the inverter's own output, so on a CT-clamp install it
    stops the battery serving anything not wired to the inverter and the shortfall is drawn
    from the grid instead. Confirmed on Sunsynk hardware, which sits behind the same
    registers (#4580).
    """
    failed = False
    d = MockDeye()
    cases = [
        ("charge", _state(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50),
        ("freeze_charge", _state(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50),
        ("hold_charge", _state(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50),
        ("idle", _state(reserve=15), 60),
    ]
    for name, sched, soc in cases:
        mode = d.derive_control_state(sched, soc)["work_mode"]
        if mode != DEYE_WORKMODE["zero_export_ct"]:
            print(f"ERROR: {name} work mode {mode}, expected {DEYE_WORKMODE['zero_export_ct']}")
            failed = True
    if d.derive_control_state(_state(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80)["work_mode"] != DEYE_WORKMODE["selling_first"]:
        print("ERROR: an export window must still sell first")
        failed = True
    assert not failed, "test_non_export_states_measure_at_the_grid_ct"


def test_self_use_slots_carry_the_inverter_rating():
    """Self-use slots are written at the inverter's rating, never at zero power.

    Regression (#4581): zero power IS the freeze — the battery neither charges nor
    discharges — and self-use slots cover every interval Predbat is not actively charging
    or exporting, so writing zero there froze the battery as its default state and put the
    house on the grid.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    sched = {"reserve": 20, "charge": {"enable": True, "soc": 90, "power": 3000, "start": "03:00", "end": "04:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    slots = d.build_dynamic_payload("INV1", sched, current_soc=38, now_minutes=22 * 60)["timeUseSettingItems"]
    frozen = [slot for slot in slots if not slot[TOU_FIELD["power"]]]
    if frozen:
        print(f"ERROR: {len(frozen)} of {len(slots)} slots were written with zero power, which freezes the battery: {slots}")
        failed = True
    self_use = [slot for slot in slots if not slot[TOU_FIELD["grid_charge"]]]
    if any(slot[TOU_FIELD["power"]] != int(MOCK_RATED_POWER) for slot in self_use):
        print(f"ERROR: self-use slots must carry the inverter rating {int(MOCK_RATED_POWER)}: {self_use}")
        failed = True
    charging = [slot for slot in slots if slot[TOU_FIELD["grid_charge"]]]
    if [slot[TOU_FIELD["power"]] for slot in charging] != [3000]:
        print(f"ERROR: the charge window must keep Predbat's own charge rate: {charging}")
        failed = True
    assert not failed, "test_self_use_slots_carry_the_inverter_rating"


def test_freeze_states_hold_with_zero_power():
    """freeze_charge and freeze_export write zero slot power, which is what tells the inverter to hold.

    Regression (#4581): both carried the requested charge/export power, the opposite of a
    freeze. They still classify as action slots rather than self-use, because freeze_charge
    keeps grid_charge=True and freeze_export keeps solar_sell=True.
    """
    failed = False
    d = MockDeye()
    freeze_charge = d.derive_control_state(_state(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50)
    freeze_export = d.derive_control_state(_state(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80)
    for name, state in (("freeze_charge", freeze_charge), ("freeze_export", freeze_export)):
        if state["power"] != 0:
            print(f"ERROR: {name} power {state['power']}, a freeze is expressed as zero power")
            failed = True
    if not freeze_charge["grid_charge"] or not freeze_export["solar_sell"]:
        print("ERROR: a zero-power freeze must still classify as an action slot")
        failed = True
    # hold_charge is NOT a freeze: the battery is already at or above target, so the slot
    # keeps Predbat's chosen charge rate and only the grid-charge flag differs.
    hold = d.derive_control_state(_state(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50)
    if hold["power"] != 3000:
        print(f"ERROR: hold_charge should keep Predbat's charge rate, got {hold['power']}")
        failed = True
    assert not failed, "test_freeze_states_hold_with_zero_power"


def test_control_write_fails_closed_without_a_self_use_power():
    """With no inverter rating and no battery config there is no honest self-use power, so nothing is written.

    Writing zero instead would freeze the battery for most of the day (#4581), which is
    worse than writing nothing at all.
    """
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    if d.build_dynamic_payload("INV1", sched, current_soc=40) != {}:
        print("ERROR: with no usable power the payload must be empty, not a zero-power freeze")
        failed = True
    if not any("no inverter rating" in message for message in d.log_messages):
        print(f"ERROR: expected a warning naming the missing rating: {d.log_messages}")
        failed = True
    # _reconcile_control rebuilds this payload every cycle, so the warning is throttled to
    # once per serial rather than repeated for as long as the rating is missing.
    d.build_dynamic_payload("INV1", sched, current_soc=40)
    warnings = [message for message in d.log_messages if "no inverter rating" in message]
    if len(warnings) != 1:
        print(f"ERROR: the missing-rating warning should be logged once per serial, got {len(warnings)}")
        failed = True
    # ...but a serial that recovers and then loses its rating again is reported afresh.
    d.with_rating("INV1")
    d.build_dynamic_payload("INV1", sched, current_soc=40)
    d.device_rated_power.pop("INV1")
    d.build_dynamic_payload("INV1", sched, current_soc=40)
    if len([message for message in d.log_messages if "no inverter rating" in message]) != 2:
        print(f"ERROR: a recurrence after a good read should warn again: {d.log_messages}")
        failed = True
    posts = []

    async def fake_post(endpoint_key, body):
        """Record any post, so the test can assert nothing reached the inverter."""
        posts.append(endpoint_key)
        return {"success": True, "orderId": 1}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", sched, 40, force=True))
    if wrote or posts:
        print(f"ERROR: an empty payload must not be written: wrote={wrote} posts={posts}")
        failed = True
    if "INV1" in d.applied_payload:
        print("ERROR: nothing was written, so nothing should be cached as applied")
        failed = True

    # The battery's own maximum charge rate backs the AC rating up, so control still works
    # on a model whose device/latest does not report RatedPower.
    d2 = MockDeye()
    d2.device_pack_voltage["INV1"] = 51.2
    d2.device_battery_config["INV1"] = {CONFIG_BATTERY_KEYS["max_charge_current"]: 100}
    slots = d2.build_dynamic_payload("INV1", sched, current_soc=40).get("timeUseSettingItems", [])
    if not slots or any(not slot[TOU_FIELD["power"]] for slot in slots):
        print(f"ERROR: the battery rate should back-stop a missing inverter rating: {slots}")
        failed = True
    assert not failed, "test_control_write_fails_closed_without_a_self_use_power"


def test_payload_names_every_day_the_schedule_runs_on():
    """The control payload carries touDays for all seven days.

    DEYE's TOU programme only runs on the days named in touDays, and Predbat's plan is a
    24h programme it re-derives every cycle — it has no notion of a day the schedule should
    be dormant. Every one of the four official strategy samples
    (clientcode/strategy/dynamic_control_*.py) sends the full seven-day list; Predbat sent
    none, leaving the active days at whatever the inverter happened to hold. If that is
    empty, or missing the day the plan is for, the whole schedule silently never runs — the
    slots are stored and simply not applied. Sunsynk, on the same registers, has the same
    field as its seven mondayOn..sundayOn flags and Predbat sets all of them there.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    payload = d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=3 * 60)
    days = payload.get("touDays")
    if sorted(days or []) != sorted(DEYE_TOU_DAYS):
        print(f"ERROR: touDays must name all seven days, got {days!r}")
        failed = True
    if len(DEYE_TOU_DAYS) != 7 or any(day != day.upper() for day in DEYE_TOU_DAYS):
        print(f"ERROR: DEYE names its days as seven upper-case strings, got {DEYE_TOU_DAYS!r}")
        failed = True
    # Every payload, not just an active one: the slots are a 24h programme whatever state
    # the top level is in.
    idle = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    if d.build_dynamic_payload("INV1", idle, current_soc=40, now_minutes=12 * 60).get("touDays") != DEYE_TOU_DAYS:
        print("ERROR: an idle payload must still name the days its slots apply on")
        failed = True
    assert not failed, "test_payload_names_every_day_the_schedule_runs_on"


def test_every_slot_carries_the_complete_field_set():
    """Every TOU item carries all five documented fields, none of them omitted.

    Sunsynk, the same hardware behind a different cloud, validates the per-slot field set as
    a whole and silently discards the flags when it is incomplete: with one flag left out,
    the grid-charge flag vanished on six consecutive writes across every encoding tried
    while the rest of each write persisted, and the API reported success throughout. DEYE
    cannot hit that while every item carries the full set its own samples post, so this
    pins the set rather than trusting each call site to remember it.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    # Compared against TOU_FIELD, which is what the slot builders use, so this asks "does
    # every slot carry the whole set" rather than "are the names right". The names
    # themselves are pinned against DEYE's published model in test_deye_const.py, which is
    # the check an edit to TOU_FIELD has to get past.
    expected = set(TOU_FIELD.values())
    schedules = [
        {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}},
        {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": True, "soc": 20, "power": 3000, "start": "16:00", "end": "19:00"}},
        {"reserve": 20, "charge": {"enable": True, "soc": 20, "power": 3000, "start": "01:00", "end": "02:00"}, "export": {"enable": False, "soc": 0, "power": 0}},
        {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}},
    ]
    for index, sched in enumerate(schedules):
        for slot in d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=3 * 60)["timeUseSettingItems"]:
            if set(slot) != expected:
                print(f"ERROR: schedule {index} produced a slot with fields {sorted(slot)}, expected {sorted(expected)}")
                failed = True
    assert not failed, "test_every_slot_carries_the_complete_field_set"


def test_freeze_export_reaches_the_inverter_from_the_charge_rate():
    """A Freeze Export written the way execute.py writes it must reach the inverter.

    Predbat expresses Freeze Export by turning the forced-export window OFF and calling
    adjust_charge_rate(0), because DeyeCloud declares has_timed_pause False. It never writes
    99 to the export SoC entity, so the export-SoC route into the freeze state could not
    fire, and charge_rate maps to a per-WINDOW field that a disabled window never reads. The
    payload came out identical to plain Demand and the inverter carried on charging the
    battery from the very solar the freeze existed to export.

    Diagnosed and confirmed live on Sunsynk, which drives the same Deye registers through a
    different cloud - see _freeze_export_state for the runs, and for the VERIFY@SPIKE noting
    this leg is inherited on DEYE rather than measured.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    # Both windows off, charge rate zeroed, export rate untouched.
    sched = _state(reserve=20, charge_power=0, export_power=1229)
    payload = d.build_dynamic_payload("INV1", sched, current_soc=95, now_minutes=12 * 60)

    if payload.get("workMode") != DEYE_WORKMODE["selling_first"]:
        print(f"ERROR: freeze export left the work mode at {payload.get('workMode')!r}, expected selling_first - zero-export-to-CT charges the battery from the surplus")
        failed = True
    for slot in payload["timeUseSettingItems"]:
        # Every slot, not just the one covering now: Predbat never says when a freeze ends,
        # so a self-use filler left in the programme would defeat it once the clock reached
        # it. The zero rate is the safety property - a non-zero one exports the battery down
        # to the cap.
        if slot[TOU_FIELD["power"]] != 0:
            print(f"ERROR: freeze export slot {slot[TOU_FIELD['time']]} carries {slot[TOU_FIELD['power']]}W, expected 0")
            failed = True
        if slot[TOU_FIELD["soc"]] != 20:
            print(f"ERROR: freeze export slot {slot[TOU_FIELD['time']]} caps at {slot[TOU_FIELD['soc']]}%, expected the 20% reserve")
            failed = True
        if not slot[TOU_FIELD["sell"]]:
            print(f"ERROR: freeze export slot {slot[TOU_FIELD['time']]} did not arm the sell flag")
            failed = True
        if slot[TOU_FIELD["grid_charge"]]:
            print(f"ERROR: freeze export slot {slot[TOU_FIELD['time']]} enables grid charge, a freeze must not charge")
            failed = True

    # And the sentinel is never a battery level, whatever the SoC or reserve.
    for current_soc in (30, 80, 99):
        for reserve in (5, 20):
            state = d.derive_control_state(_state(reserve=reserve, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), current_soc)
            if state["slot_soc"] != reserve:
                print(f"ERROR: freeze export with reserve {reserve} at {current_soc}% capped at {state['slot_soc']}, expected {reserve}")
                failed = True
    assert not failed, "test_freeze_export_reaches_the_inverter_from_the_charge_rate"


def test_export_slots_arm_the_sell_flag():
    """Only export slots carry enableSell, and every slot carries the field.

    enableSell is TimeUseSettingItem's per-slot forced-export enable — the same register bit
    Sunsynk exposes as sellTime{n}En, where a live write test proved a forced export slot
    does not arm without it. Predbat never sent it, so DEYE export windows had the work mode
    and the SOC target but not the slot flag that makes the slot an export slot.

    It is written on every slot, not just export ones: on Sunsynk an absent per-slot flag
    made the API silently discard the OTHER flags in the same item, grid charge included, on
    six consecutive writes that each reported success.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    sell = TOU_FIELD["sell"]
    export = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": True, "soc": 20, "power": 3000, "start": "16:00", "end": "19:00"}}
    slots = d.build_dynamic_payload("INV1", export, current_soc=80, now_minutes=17 * 60)["timeUseSettingItems"]
    armed = [slot for slot in slots if slot[sell]]
    if [slot[TOU_FIELD["time"]] for slot in armed] != ["16:00"]:
        print(f"ERROR: only the export window should arm the sell flag, got {[s[TOU_FIELD['time']] for s in armed]}")
        failed = True
    if not armed or armed[0][TOU_FIELD["soc"]] != 20:
        print(f"ERROR: the armed slot should be the export target: {armed}")
        failed = True

    # A freeze-export holds the battery but still sells, so it arms too.
    freeze = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000, "start": "16:00", "end": "19:00"}}
    frozen = [slot for slot in d.build_dynamic_payload("INV1", freeze, current_soc=80, now_minutes=17 * 60)["timeUseSettingItems"] if slot[sell]]
    if len(frozen) != 1 or frozen[0][TOU_FIELD["power"]] != 0:
        print(f"ERROR: freeze-export should arm one zero-power sell slot: {frozen}")
        failed = True

    # A charge window never sells, and neither does an idle day.
    charge = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    idle = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    for name, sched in (("charge", charge), ("idle", idle)):
        slots = d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=3 * 60)["timeUseSettingItems"]
        if any(slot[sell] for slot in slots):
            print(f"ERROR: a {name} schedule must not arm any sell slot: {slots}")
            failed = True
        if any(sell not in slot for slot in slots):
            print(f"ERROR: every slot must carry the sell field even when off: {slots}")
            failed = True
    assert not failed, "test_export_slots_arm_the_sell_flag"


def test_generator_charging_is_never_switched_on_by_predbat():
    """enableGeneration is off in every slot Predbat writes when the inverter's own value is unknown.

    It authorises charging the battery from an EXTERNAL GENERATOR. Predbat has no model of a
    generator — no fuel cost, no run hours, nothing it can plan against — so it must never
    be the thing that switches one on. Predbat previously wrote True on every slot, which
    authorised generator charging across the whole day on any system with one wired in.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    generate = TOU_FIELD["generate"]
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": True, "soc": 20, "power": 3000, "start": "16:00", "end": "19:00"}}
    for now_minutes in (3 * 60, 12 * 60, 17 * 60):
        slots = d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=now_minutes)["timeUseSettingItems"]
        if any(slot[generate] for slot in slots):
            print(f"ERROR: at {now_minutes} Predbat authorised generator charging: {slots}")
            failed = True
        if any(generate not in slot for slot in slots):
            print(f"ERROR: the field must still be present on every slot: {slots}")
            failed = True
    assert not failed, "test_generator_charging_is_never_switched_on_by_predbat"


def test_generator_charging_the_owner_configured_is_carried_through():
    """The inverter's own enableGeneration survives, slot by slot, rather than being cleared.

    Predbat owns the TOU programme but not this flag, so an owner who has generator charging
    configured keeps it. Read from config/tou and carried across by slot position — the same
    thing the Sunsynk component does with genTime{n}on through its read-modify-write.
    """
    failed = False
    d = MockDeye().with_rating("INV1")
    generate = TOU_FIELD["generate"]
    # The owner runs the generator on the 2nd and 5th slots of the day.
    d.device_tou_config["INV1"] = [{"time": f"{n * 4:02d}:00", generate: n in (1, 4), "enableGridCharge": False, "power": 5000, "soc": 20} for n in range(6)]
    sched = {"reserve": 10, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 0}}
    slots = d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=12 * 60)["timeUseSettingItems"]
    if [slot[generate] for slot in slots] != [False, True, False, False, True, False]:
        print(f"ERROR: the owner's generator slots were not carried through: {[s[generate] for s in slots]}")
        failed = True

    # A short read is padded rather than trusted, and never invents an enable.
    d.device_tou_config["INV1"] = [{generate: True}]
    got = [slot[generate] for slot in d.build_dynamic_payload("INV1", sched, current_soc=40, now_minutes=12 * 60)["timeUseSettingItems"]]
    if got != [True, False, False, False, False, False]:
        print(f"ERROR: a short read should pad with off, got {got}")
        failed = True
    assert not failed, "test_generator_charging_the_owner_configured_is_carried_through"


def test_fetch_tou_config_caches_and_survives_an_unsupported_model():
    """config/tou is read into the cache, and a model that rejects it leaves generator charging off."""
    failed = False
    d = MockDeye()
    items = [{"time": "00:00", "enableGeneration": True, "enableGridCharge": False, "power": 5000, "soc": 20}]

    async def fake_post(endpoint_key, body):
        """Return a TOU read for the config point, mirroring DeviceTimeOfUseResponse."""
        if endpoint_key != "config_tou":
            return {"success": True}
        return {"success": True, "timeUseSettingItems": items, "touAction": "on"}

    with patch.object(d, "_post", side_effect=fake_post):
        got = run_async_local(d.fetch_tou_config("INV1"))
    if got != items or d.device_tou_config.get("INV1") != items:
        print(f"ERROR: the TOU read should be cached: {d.device_tou_config}")
        failed = True

    # Some models answer "config point not supported" — that must not clear what is known,
    # nor raise, and the flags fall back to off for a serial that was never read.
    async def fake_fail(endpoint_key, body):
        """Reject the config point the way a model without it does."""
        return {"success": False, "code": "2106001", "msg": "config point not supported"}

    with patch.object(d, "_post", side_effect=fake_fail):
        got = run_async_local(d.fetch_tou_config("INV2"))
    if got != []:
        print(f"ERROR: a rejected read should report nothing, got {got}")
        failed = True
    if d.device_tou_config.get("INV1") != items:
        print("ERROR: a failure for one serial must not disturb another's cached read")
        failed = True
    if any(d._generation_flags("INV2")):
        print(f"ERROR: an unread serial must default to no generator charging: {d._generation_flags('INV2')}")
        failed = True
    assert not failed, "test_fetch_tou_config_caches_and_survives_an_unsupported_model"


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
        ("reconcile_read_only", test_reconcile_control_skips_while_read_only),
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
        ("zero_length_window", test_zero_length_window_produces_no_action_slot),
        ("solar_sell_always_on", test_solar_sell_is_always_on),
        ("non_export_uses_ct", test_non_export_states_measure_at_the_grid_ct),
        ("self_use_slot_power", test_self_use_slots_carry_the_inverter_rating),
        ("freeze_zero_power", test_freeze_states_hold_with_zero_power),
        ("freeze_export_reaches_inverter", test_freeze_export_reaches_the_inverter_from_the_charge_rate),
        ("no_self_use_power_fails_closed", test_control_write_fails_closed_without_a_self_use_power),
        ("tou_days", test_payload_names_every_day_the_schedule_runs_on),
        ("slot_field_set", test_every_slot_carries_the_complete_field_set),
        ("sell_flag", test_export_slots_arm_the_sell_flag),
        ("generation_never_on", test_generator_charging_is_never_switched_on_by_predbat),
        ("generation_carried", test_generator_charging_the_owner_configured_is_carried_through),
        ("tou_config_read", test_fetch_tou_config_caches_and_survives_an_unsupported_model),
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

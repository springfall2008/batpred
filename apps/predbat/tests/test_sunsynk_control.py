# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk control-state derivation
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk intent to settings-object derivation."""

from unittest.mock import patch
from sunsynk_const import (
    SUNSYNK_SYSTEM_MODE_FIELDS,
    SUNSYNK_WORKMODE,
    SUNSYNK_WORKMODE_FIELD,
    SUNSYNK_SOLAR_SELL_FIELD,
    SUNSYNK_TOU_ENABLE_FIELD,
    SUNSYNK_SERIAL_FIELD,
    SUNSYNK_DAY_FIELDS,
    FREEZE_EXPORT_SOC,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    SUNSYNK_SETTLE_POLLS,
)
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


def _schedule(reserve=10, charge=None, export=None, charge_power=3000, export_power=3000):
    """Build a schedule dict in the shape the control entities produce.

    A DISABLED window still carries a power, because the real control entities do:
    adjust_charge_rate/adjust_discharge_rate write Predbat's rates every cycle whether or
    not a window is enabled. That matters because a zero charge rate against a non-zero
    export rate is precisely how Predbat signals Freeze Export (see derive_control_state),
    so a fixture that left both at zero would silently be testing a freeze instead of the
    demand state it reads as.
    """
    idle_charge = {"enable": False, "soc": 0, "power": charge_power, "start": "00:00:00", "end": "00:00:00"}
    idle_export = {"enable": False, "soc": 0, "power": export_power, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": charge or idle_charge, "export": export or idle_export}


def test_derive_control_state_table():
    """Each Predbat intent maps to the work mode and flags the spec's table requires."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    cases = [
        ("charge", _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50, ("charge", SUNSYNK_WORKMODE["zero_export_ct"], True, False, 90)),
        ("freeze_charge", _schedule(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, ("freeze_charge", SUNSYNK_WORKMODE["zero_export_ct"], True, False, 50)),
        ("hold_charge", _schedule(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, ("hold_charge", SUNSYNK_WORKMODE["zero_export_ct"], False, False, 50)),
        ("export", _schedule(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, ("export", SUNSYNK_WORKMODE["selling_first"], False, True, 20)),
        # A freeze export caps at the RESERVE, not the 99 sentinel: the battery needs that
        # room to cover the house, and the zero sell rate is what keeps it from being pushed
        # to the grid instead. Confirmed live - see _freeze_export_state.
        ("freeze_export", _schedule(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, ("freeze_export", SUNSYNK_WORKMODE["selling_first"], False, True, 10)),
        # The signal Predbat actually sends: both windows off, charge rate zeroed, export
        # rate left alone. Without this the freeze never reached the inverter at all.
        ("freeze_export_by_rate", _schedule(reserve=10, charge_power=0), 80, ("freeze_export", SUNSYNK_WORKMODE["selling_first"], False, True, 10)),
        # Both rates zero is an absence of a plan, not a freeze - do not strand an
        # unconfigured system holding its battery all day.
        ("no_rates_is_not_a_freeze", _schedule(reserve=15, charge_power=0, export_power=0), 60, ("idle", SUNSYNK_WORKMODE["zero_export_ct"], False, False, 15)),
        ("idle", _schedule(reserve=15), 60, ("idle", SUNSYNK_WORKMODE["zero_export_ct"], False, False, 15)),
    ]
    for name, schedule, soc, expect in cases:
        result = s.derive_control_state(schedule, soc)
        got = (result["behaviour"], result["work_mode"], result["grid_charge"], result["solar_sell"], result["slot_soc"])
        if got != expect:
            print(f"ERROR: {name} expected {expect} got {got}")
            failed = True
    assert not failed, "test_derive_control_state_table"


def test_build_tou_slots_shape():
    """A charge window yields exactly 6 slots with distinct ascending times from 00:00."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    slots = s.build_tou_slots(schedule, current_soc=40, self_use_power=8000)
    if len(slots) != TOU_SLOT_COUNT:
        print(f"ERROR: expected {TOU_SLOT_COUNT} slots, got {len(slots)}")
        failed = True
    times = [slot["time"] for slot in slots]
    if len(set(times)) != len(times):
        print(f"ERROR: duplicate slot start times: {times}")
        failed = True
    if times != sorted(times):
        print(f"ERROR: slot times not ascending: {times}")
        failed = True
    if times and times[0] != "00:00":
        print(f"ERROR: first slot must start at 00:00, got {times[0]}")
        failed = True
    charging = [slot for slot in slots if slot["grid_charge"] and slot["soc"] == 95]
    if not charging:
        print(f"ERROR: no grid-charge slot at soc 95 in {slots}")
        failed = True
    assert not failed, "test_build_tou_slots_shape"


def test_build_tou_slots_seconds_are_dropped():
    """Entity times carry seconds (HH:MM:SS); slots must be HH:MM."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:30:00", "end": "05:45:00"})
    times = [slot["time"] for slot in s.build_tou_slots(schedule, current_soc=40, self_use_power=8000)]
    for time_text in times:
        if len(time_text) != 5 or time_text.count(":") != 1:
            print(f"ERROR: slot time {time_text!r} is not HH:MM")
            failed = True
    if "02:30" not in times or "05:45" not in times:
        print(f"ERROR: window boundaries missing from {times}")
        failed = True
    assert not failed, "test_build_tou_slots_seconds_are_dropped"


def test_build_tou_slots_idle_is_still_six_distinct():
    """An empty schedule still yields six distinct self-use slots."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    slots = s.build_tou_slots(_schedule(reserve=15), current_soc=50, self_use_power=8000)
    times = [slot["time"] for slot in slots]
    if len(set(times)) != TOU_SLOT_COUNT:
        print(f"ERROR: idle schedule produced {len(set(times))} distinct times: {times}")
        failed = True
    if any(slot["grid_charge"] for slot in slots):
        print("ERROR: an idle schedule must not enable grid charge")
        failed = True
    if any(slot["soc"] != 15 for slot in slots):
        print(f"ERROR: idle slots should all hold at the reserve: {slots}")
        failed = True
    assert not failed, "test_build_tou_slots_idle_is_still_six_distinct"


def test_build_tou_slots_zero_length_window_has_no_effect():
    """An enabled window whose start equals end must not become an unterminated action slot.

    _window_active already refuses to treat a zero-length window as active. build_tou_slots
    must apply the same guard to its own segment boundaries: without it, an enable event
    that arrives before the time fields (both still the "00:00:00" default) adds an action
    segment at 00:00 with no matching return-to-self-use segment, producing a multi-hour
    full-power grid-charge/export slot even though _active_state correctly reports the
    window inactive. Two shapes are checked: literally equal strings, and strings that are
    only equal once seconds are normalised away ("02:00:00" vs "02:00").
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    cases = [
        _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "00:00:00", "end": "00:00:00"}),
        _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "02:00"}),
    ]
    for schedule in cases:
        slots = s.build_tou_slots(schedule, current_soc=40, self_use_power=8000)
        if any(slot["grid_charge"] for slot in slots):
            print(f"ERROR: a zero-length window produced a grid-charge slot: {slots}")
            failed = True
        if any(slot["soc"] == 95 for slot in slots):
            print(f"ERROR: a zero-length window's target SOC leaked into a slot: {slots}")
            failed = True
        if len(slots) != TOU_SLOT_COUNT or len({slot["time"] for slot in slots}) != TOU_SLOT_COUNT:
            print(f"ERROR: a zero-length window broke the six-distinct-slots invariant: {slots}")
            failed = True
    assert not failed, "test_build_tou_slots_zero_length_window_has_no_effect"


def test_active_window_drives_the_global_mode():
    """The top-level mode follows the window active NOW, not a static precedence.

    Sunsynk has one global work mode. If an export window enabled elsewhere in the day
    pinned the mode to selling-first, it would block the charge window's grid charging.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "16:00:00", "end": "19:00:00"},
    )
    # 03:00 -> inside the charge window.
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["zero_export_ct"]:
        print(f"ERROR: at 03:00 expected zero_export_load, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    # 17:00 -> inside the export window.
    payload = s.build_settings_payload("INV1", schedule, current_soc=80, now_minutes=17 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["selling_first"]:
        print(f"ERROR: at 17:00 expected selling_first, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    # 12:00 -> neither window, so self-use.
    payload = s.build_settings_payload("INV1", schedule, current_soc=60, now_minutes=12 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["zero_export_ct"]:
        print(f"ERROR: at 12:00 expected zero_export_load, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    assert not failed, "test_active_window_drives_the_global_mode"


def test_window_active_handles_midnight_wrap():
    """A window running past midnight is active on both sides of it."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    window = {"enable": True, "start": "23:00", "end": "02:00"}
    for minutes, expect in ((23 * 60 + 30, True), (60, True), (12 * 60, False), (22 * 60, False)):
        got = s._window_active(window, minutes)
        if got != expect:
            print(f"ERROR: minute {minutes} active={got}, expected {expect}")
            failed = True
    # A zero-length window is never active.
    if s._window_active({"enable": True, "start": "05:00", "end": "05:00"}, 5 * 60):
        print("ERROR: a zero-length window must never be active")
        failed = True
    assert not failed, "test_window_active_handles_midnight_wrap"


def test_payload_renders_indexed_fields_and_types():
    """Slots become sellTimeN/capN/timeNon with the right wire types."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    for n in range(1, TOU_SLOT_COUNT + 1):
        for concept in ("time", "power", "soc", "grid_charge"):
            name = TOU_FIELD[concept].format(n=n)
            if name not in payload:
                print(f"ERROR: payload missing {name}")
                failed = True
                continue
            value = payload[name]
            if concept == "grid_charge":
                if value not in ("true", "false"):
                    print(f"ERROR: {name} = {value!r} should be the string 'true' or 'false' - a bare boolean is discarded by the API")
                    failed = True
            elif not isinstance(value, str):
                print(f"ERROR: {name} = {value!r} should be a string")
                failed = True
    for day in SUNSYNK_DAY_FIELDS:
        if payload.get(day) != "true":
            print(f"ERROR: {day} should be the string 'true', got {payload.get(day)!r}")
            failed = True
    if payload.get(SUNSYNK_TOU_ENABLE_FIELD) != "1":
        print(f"ERROR: TOU master enable should be '1', got {payload.get(SUNSYNK_TOU_ENABLE_FIELD)!r}")
        failed = True
    if payload.get(SUNSYNK_SERIAL_FIELD) != "INV1":
        print(f"ERROR: serial should be echoed back, got {payload.get(SUNSYNK_SERIAL_FIELD)!r}")
        failed = True
    if payload.get(SUNSYNK_SOLAR_SELL_FIELD) != "1":
        print(f"ERROR: solarSell should always be '1', got {payload.get(SUNSYNK_SOLAR_SELL_FIELD)!r}")
        failed = True
    assert not failed, "test_payload_renders_indexed_fields_and_types"


def test_payload_preserves_unowned_settings():
    """Fields Predbat does not own survive - in-group verbatim, out-of-group by never being sent.

    Read-modify-write only covers the System Mode group, because the endpoint discards a
    larger object outright. So an installer setting is protected two different ways
    depending on which side of that boundary it sits, and both are worth pinning: a
    zeroExportPower must come back unchanged, while a batteryShutdownCap must be absent
    from the payload entirely rather than echoed.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {
        "sn": "INV1",
        # inside the System Mode group - must be carried through untouched
        "safetyType": "3",
        "battMode": "1",
        "energyMode": "1",
        "zeroExportPower": "20",
        "solarMaxSellPower": "8000",
        "pvMaxLimit": "7000",
        "sellTime1Volt": "49.0",
        "genTime1on": "false",
        # outside it - must never be transmitted by a schedule write
        "batteryShutdownCap": "5",
        "batteryLowCap": "10",
        "batteryMaxCurrentCharge": "100",
    }
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    for key, expect in (("safetyType", "3"), ("battMode", "1"), ("energyMode", "1"), ("zeroExportPower", "20"), ("solarMaxSellPower", "8000"), ("pvMaxLimit", "7000"), ("sellTime1Volt", "49.0"), ("genTime1on", "false")):
        if str(payload.get(key)) != expect:
            print(f"ERROR: in-group unowned field {key} became {payload.get(key)!r}, expected {expect!r}")
            failed = True
    for key in ("batteryShutdownCap", "batteryLowCap", "batteryMaxCurrentCharge"):
        if key in payload:
            print(f"ERROR: {key} is outside the System Mode group and must not be sent, got {payload[key]!r}")
            failed = True
    assert not failed, "test_payload_preserves_unowned_settings"


def test_payload_clamps_to_the_inverter_soc_floor():
    """No slot may ask for less than the installer-set floor the inverter reports.

    current_soc=0 (not e.g. 40) is deliberate: with soc=5 and reserve=0, a current_soc
    above 5 makes derive_control_state fall through to hold_charge (target 5 is not above
    current_soc, so it isn't actually "charging" yet) and the slot holds at the reserve
    (0), not at the requested 5 - so the clamp would be exercised on the reserve, not on
    the genuine sub-floor charge target the test name promises. current_soc=0 makes 5 a
    real charge target (5 > current_soc and 5 > reserve), so the clamp raising 5 -> 20 is
    the one actually under test.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"batteryLowCap": "20"}
    # Predbat's control entities start at 0 and only reach real values once written.
    schedule = _schedule(reserve=0, charge={"enable": True, "soc": 5, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=0, now_minutes=3 * 60)
    for n in range(1, TOU_SLOT_COUNT + 1):
        value = int(payload[TOU_FIELD["soc"].format(n=n)])
        if value < 20:
            print(f"ERROR: slot {n} soc {value} is below the inverter's 20% floor")
            failed = True
    if not any("floor" in str(m).lower() for m in s.log_messages):
        print("ERROR: clamping to the floor should be logged once")
        failed = True
    assert not failed, "test_payload_clamps_to_the_inverter_soc_floor"


def test_build_settings_payload_returns_empty_without_a_baseline():
    """No settings have ever been read for this serial, so there is nothing to modify.

    build_settings_payload is a public producer, not just an apply_settings helper, so the
    guard against posting an owned-keys-only payload (which would drop every installer
    setting Predbat does not own) belongs here rather than only in apply_settings's caller
    logic - the same reasoning as the SOC clamp living at the API boundary.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    if payload != {}:
        print(f"ERROR: expected {{}} with no baseline, got {len(payload)} key(s): {sorted(payload)}")
        failed = True
    assert not failed, "test_build_settings_payload_returns_empty_without_a_baseline"


def test_payloads_equal_ignores_nothing_material():
    """Change detection sees a real difference and ignores an identical rewrite."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    first = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    same = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    if not s.payloads_equal(first, same):
        print("ERROR: two identical payloads compared unequal")
        failed = True
    other = _schedule(reserve=10, charge={"enable": True, "soc": 80, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    changed = s.build_settings_payload("INV1", other, current_soc=40, now_minutes=3 * 60)
    if s.payloads_equal(first, changed):
        print("ERROR: a changed target SOC compared equal")
        failed = True
    assert not failed, "test_payloads_equal_ignores_nothing_material"


def test_apply_settings_skips_an_unchanged_payload():
    """An unchanged plan does not re-post, so the dongle is not churned every cycle."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object for the read half of read-modify-write."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write."""
        posts.append((endpoint_key, sn))
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if len(posts) != 1:
        print(f"ERROR: expected exactly 1 write for an unchanged plan, got {len(posts)}")
        failed = True
    # force must override the diff gate.
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40, force=True))
    if len(posts) != 2:
        print(f"ERROR: force=True should have written again, total writes {len(posts)}")
        failed = True
    assert not failed, "test_apply_settings_skips_an_unchanged_payload"


def test_apply_settings_noop_ticks_perform_no_io():
    """Once applied, repeating the same plan costs zero GETs and zero POSTs.

    The owned-field diff is computed BEFORE any network call, from only the schedule,
    current_soc, now_minutes and the cached battery_reserve_min(sn) - so a no-op tick
    never touches the network, not even a settings read. Only the first (baseline-setting)
    write may cost a GET and a POST.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    gets = []
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Record each read and return a minimal settings object."""
        gets.append(endpoint_key)
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
        gets_after_first, posts_after_first = len(gets), len(posts)
        for _ in range(5):
            run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if gets_after_first != 1 or posts_after_first != 1:
        print(f"ERROR: the first write should cost exactly one GET and one POST, got {gets_after_first} GET(s), {posts_after_first} POST(s)")
        failed = True
    if len(gets) != gets_after_first:
        print(f"ERROR: 5 no-op ticks made {len(gets) - gets_after_first} extra GET(s)")
        failed = True
    if len(posts) != posts_after_first:
        print(f"ERROR: 5 no-op ticks made {len(posts) - posts_after_first} extra POST(s)")
        failed = True
    assert not failed, "test_apply_settings_noop_ticks_perform_no_io"


def test_apply_settings_changed_plan_performs_one_get_and_one_post():
    """A genuine change in the plan costs exactly one read and one write, not more."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    gets = []
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Record each read and return a minimal settings object."""
        gets.append(endpoint_key)
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    changed = _schedule(reserve=10, charge={"enable": True, "soc": 80, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
        run_async_local(s.apply_settings("INV1", changed, current_soc=40))
    if len(gets) != 2:
        print(f"ERROR: expected exactly 2 GETs total (one per genuine change), got {len(gets)}")
        failed = True
    if len(posts) != 2:
        print(f"ERROR: expected exactly 2 POSTs total (one per genuine change), got {len(posts)}")
        failed = True
    assert not failed, "test_apply_settings_changed_plan_performs_one_get_and_one_post"


def test_apply_settings_ignores_a_volatile_unowned_field():
    """A field that changes on every read (e.g. a server timestamp) must not force a write.

    The owned-field diff decides whether to read and write at all; it never sees baseline
    fields at all, so a value the cloud rewrites every poll cannot make a whole-payload
    comparison perpetually disagree with itself the way it would if the diff gate compared
    the full read-modify-write result including unowned fields.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    posts = []
    tick = [0]

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a settings object whose 'lastUpdate' field changes on every read."""
        tick[0] += 1
        return {"sn": "INV1", "batteryLowCap": "10", "lastUpdate": str(tick[0])}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        for _ in range(5):
            run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if len(posts) != 1:
        print(f"ERROR: a volatile unowned field triggered {len(posts)} writes for an unchanged plan, expected 1")
        failed = True
    assert not failed, "test_apply_settings_ignores_a_volatile_unowned_field"


def test_apply_settings_fails_closed_without_a_read():
    """If the settings read fails, nothing is written — the write baseline is unknown."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    posts = []

    async def fake_get_empty(endpoint_key, sn=None, params=None):
        """Simulate a failed settings read."""
        return {}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write, which must not happen here."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get_empty), patch.object(s, "_post", side_effect=fake_post):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if applied:
        print("ERROR: apply_settings reported success without a settings read")
        failed = True
    if posts:
        print(f"ERROR: wrote {posts} despite having no baseline to modify")
        failed = True
    assert not failed, "test_apply_settings_fails_closed_without_a_read"


def test_apply_settings_respects_control_enable():
    """With control disabled, the component derives but never writes."""
    failed = False
    s = MockSunsynk(control_enable=False)
    s.device_rated_power["INV1"] = 8000.0
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write, which must not happen here."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if posts:
        print(f"ERROR: control is disabled but {posts} was written")
        failed = True
    assert not failed, "test_apply_settings_respects_control_enable"


def test_reconcile_control_applies_a_controlled_inverter():
    """_reconcile_control re-applies the schedule of an inverter Predbat already controls."""
    failed = False
    s = MockSunsynk()
    s.control_active = {"INV1"}
    calls = []

    async def fake_apply(sn):
        """Record reconcile applies."""
        calls.append(sn)
        return False

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s._reconcile_control("INV1"))
    if calls != ["INV1"]:
        print(f"ERROR: reconcile should apply the controlled inverter: {calls}")
        failed = True
    assert not failed, "test_reconcile_control_applies_a_controlled_inverter"


def test_reconcile_control_skips_an_uncontrolled_inverter():
    """_reconcile_control must not write to an inverter Predbat has not yet driven via the write button."""
    failed = False
    s = MockSunsynk()
    s.control_active = set()  # INV1 not yet driven by Predbat
    calls = []

    async def fake_apply(sn):
        """Record reconcile applies, which must not happen here."""
        calls.append(sn)
        return False

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s._reconcile_control("INV1"))
    if calls:
        print(f"ERROR: reconcile should not apply an uncontrolled inverter: {calls}")
        failed = True
    assert not failed, "test_reconcile_control_skips_an_uncontrolled_inverter"


def test_reconcile_control_skips_while_read_only():
    """_reconcile_control must not write to the inverter while switch.predbat_set_read_only is on."""
    failed = False
    s = MockSunsynk()
    s.control_active = {"INV1"}
    s.base.get_state_wrapper.return_value = "on"
    calls = []

    async def fake_apply(sn):
        """Record reconcile applies, which must not happen here."""
        calls.append(sn)
        return False

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s._reconcile_control("INV1"))
    if calls:
        print(f"ERROR: reconcile must not write while read-only is on: {calls}")
        failed = True
    assert not failed, "test_reconcile_control_skips_while_read_only"


def test_apply_settings_skips_when_the_payload_is_empty():
    """apply_settings must not post a payload that build_settings_payload refused to build.

    build_settings_payload returns {} when there is no baseline to modify. This is
    contrived here via a direct patch (the normal fail-closed read check makes it
    otherwise unreachable through apply_settings's own flow), specifically to prove the
    caller-side guard exists too and does not depend on that other check alone.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object, as if the read genuinely succeeded."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write, which must not happen here."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "build_settings_payload", return_value={}):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if applied:
        print("ERROR: apply_settings reported success while posting nothing")
        failed = True
    if posts:
        print(f"ERROR: wrote {posts} despite build_settings_payload refusing to build one")
        failed = True
    assert not failed, "test_apply_settings_skips_when_the_payload_is_empty"


def test_apply_settings_reports_a_failed_write():
    """A failed write is detected and does not update the applied-payload cache.

    _post reports failure as None; a successful settings write carries no data payload,
    so {} means "written, nothing returned". Confusing the two would let a failed write
    be cached as applied, and the diff gate would then never retry it.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post_fails(endpoint_key, sn=None, body=None):
        """Simulate a failed write."""
        return None

    async def fake_post_empty(endpoint_key, sn=None, body=None):
        """Simulate a successful write that returns no data payload."""
        return {}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post_fails):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if applied:
        print("ERROR: a failed write reported success")
        failed = True
    if "INV1" in s.applied_payload:
        print("ERROR: a failed write was cached as applied, so it would never be retried")
        failed = True

    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post_empty):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if not applied:
        print("ERROR: a successful write returning no data was read as a failure")
        failed = True
    if "INV1" not in s.applied_payload:
        print("ERROR: a successful write was not cached")
        failed = True
    assert not failed, "test_apply_settings_reports_a_failed_write"


def test_note_settle_normalises_wire_types_before_comparing():
    """A read-back that renders bools as strings must not be treated as a mismatch.

    Sunsynk hands per-slot grid-charge flags back as strings ("true"/"false", "1"/"0") as
    plausibly as bare booleans - SUNSYNK_FALSE_STRINGS exists precisely because of this.
    note_settle must decode both sides through encode_setting before comparing, or a
    healthy inverter whose read-back happens to render as a string warns forever and
    settle_count never resets.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    applied = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    s.applied_payload["INV1"] = applied

    # Same state, but every bool rendered as a string the way a real read-back might.
    string_settings = {key: (("true" if value else "false") if isinstance(value, bool) else value) for key, value in applied.items()}
    s.note_settle("INV1", string_settings)
    if s.settle_count.get("INV1", 0) != 0:
        print(f"ERROR: a string-rendered but otherwise identical read-back was treated as a mismatch, settle_count={s.settle_count.get('INV1')}")
        failed = True

    # A genuine mismatch must still be detected.
    real_mismatch = dict(applied)
    real_mismatch[SUNSYNK_WORKMODE_FIELD] = "9"
    s.note_settle("INV1", real_mismatch)
    if s.settle_count.get("INV1", 0) != 1:
        print(f"ERROR: a genuine mismatch was not counted, settle_count={s.settle_count.get('INV1')}")
        failed = True
    assert not failed, "test_note_settle_normalises_wire_types_before_comparing"


def _settled_baseline():
    """Build an applied payload and matching MockSunsynk baseline for note_settle tests.

    Returns (s, applied): a full owned+unowned payload from build_settings_payload, already
    stashed as the last-applied payload, so callers only need to mutate a read-back copy of
    it to exercise one field at a time.
    """
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    applied = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    s.applied_payload["INV1"] = applied
    return s, applied


def _drive_to_warn(s, sn, settings):
    """Call note_settle enough times to cross SUNSYNK_SETTLE_POLLS and return whether it warned."""
    for _ in range(SUNSYNK_SETTLE_POLLS + 1):
        s.note_settle(sn, settings)
    return any("has not applied" in str(m) for m in s.log_messages)


def test_note_settle_catches_a_divergent_power_field():
    """A read-back that agrees on every slot time/soc/grid_charge but not sellTimeNPac must not settle.

    This is the gap the code review found: note_settle used to hand-roll its own field list
    (work mode plus slot time/soc/grid_charge only), so a partial apply that dropped the
    per-slot power field would compare equal on every field it checked and settle_count
    would wrongly reset to zero, never warning about a real divergence.
    """
    failed = False
    s, applied = _settled_baseline()
    divergent = dict(applied)
    divergent[TOU_FIELD["power"].format(n=2)] = "9999"
    warned = _drive_to_warn(s, "INV1", divergent)
    if s.settle_count.get("INV1", 0) != SUNSYNK_SETTLE_POLLS + 1:
        print(f"ERROR: a divergent sellTime2Pac was not counted as a mismatch, settle_count={s.settle_count.get('INV1')}")
        failed = True
    if not warned:
        print("ERROR: sustained sellTimeNPac divergence should eventually warn")
        failed = True
    assert not failed, "test_note_settle_catches_a_divergent_power_field"


def test_note_settle_catches_a_divergent_solar_sell():
    """A read-back that diverges only on solarSell must not settle."""
    failed = False
    s, applied = _settled_baseline()
    divergent = dict(applied)
    divergent[SUNSYNK_SOLAR_SELL_FIELD] = "9"
    warned = _drive_to_warn(s, "INV1", divergent)
    if s.settle_count.get("INV1", 0) != SUNSYNK_SETTLE_POLLS + 1:
        print(f"ERROR: a divergent solarSell was not counted as a mismatch, settle_count={s.settle_count.get('INV1')}")
        failed = True
    if not warned:
        print("ERROR: sustained solarSell divergence should eventually warn")
        failed = True
    assert not failed, "test_note_settle_catches_a_divergent_solar_sell"


def test_note_settle_catches_a_divergent_tou_enable():
    """A read-back that diverges only on peakAndVallery (the TOU master enable) must not settle."""
    failed = False
    s, applied = _settled_baseline()
    divergent = dict(applied)
    divergent[SUNSYNK_TOU_ENABLE_FIELD] = "0"
    warned = _drive_to_warn(s, "INV1", divergent)
    if s.settle_count.get("INV1", 0) != SUNSYNK_SETTLE_POLLS + 1:
        print(f"ERROR: a divergent peakAndVallery was not counted as a mismatch, settle_count={s.settle_count.get('INV1')}")
        failed = True
    if not warned:
        print("ERROR: sustained peakAndVallery divergence should eventually warn")
        failed = True
    assert not failed, "test_note_settle_catches_a_divergent_tou_enable"


def test_note_settle_catches_a_divergent_day_flag():
    """A read-back that diverges only on one of the seven day-enable flags must not settle."""
    failed = False
    s, applied = _settled_baseline()
    divergent = dict(applied)
    divergent[SUNSYNK_DAY_FIELDS[0]] = False
    warned = _drive_to_warn(s, "INV1", divergent)
    if s.settle_count.get("INV1", 0) != SUNSYNK_SETTLE_POLLS + 1:
        print(f"ERROR: a divergent {SUNSYNK_DAY_FIELDS[0]} was not counted as a mismatch, settle_count={s.settle_count.get('INV1')}")
        failed = True
    if not warned:
        print(f"ERROR: sustained {SUNSYNK_DAY_FIELDS[0]} divergence should eventually warn")
        failed = True
    assert not failed, "test_note_settle_catches_a_divergent_day_flag"


def test_note_settle_ignores_a_readback_missing_some_owned_keys():
    """A read-back missing some owned fields, but agreeing on every one it does contain, must settle.

    Sunsynk's read-back is not confirmed to echo every field Predbat writes - nobody on the
    project has a real account to check against. A key absent from the read-back means "the
    API told us nothing about this field", not "the inverter diverged", so it must not be
    treated as a mismatch. Getting this wrong reintroduces the cry-wolf bug this component
    already had once: warning every poll, forever, against a perfectly healthy inverter.
    """
    failed = False
    s, applied = _settled_baseline()
    # Drop every per-slot power field and every day flag from the read-back, as if the API
    # simply never echoes them, while keeping everything else identical to what was applied.
    partial = {key: value for key, value in applied.items() if key not in ({TOU_FIELD["power"].format(n=n) for n in range(1, TOU_SLOT_COUNT + 1)} | set(SUNSYNK_DAY_FIELDS))}
    for _ in range(SUNSYNK_SETTLE_POLLS + 2):
        s.note_settle("INV1", partial)
    if s.settle_count.get("INV1", 0) != 0:
        print(f"ERROR: a read-back missing unconfirmed fields should still settle to zero, settle_count={s.settle_count.get('INV1')}")
        failed = True
    if any("has not applied" in str(m) for m in s.log_messages):
        print("ERROR: a read-back that agrees on everything it reports should never warn")
        failed = True
    assert not failed, "test_note_settle_ignores_a_readback_missing_some_owned_keys"


def test_note_settle_leaves_the_counter_unchanged_when_readback_has_no_owned_keys():
    """A read-back that reports none of Predbat's owned fields must not reset the counter to zero.

    Zero owned keys present means Predbat learned nothing about whether its settings applied
    - not that they applied. Resetting settle_count here would let a completely uninformative
    read-back mask a genuine, ongoing divergence.
    """
    failed = False
    s, applied = _settled_baseline()
    # First establish a nonzero count with a genuine, detected mismatch.
    mismatched = dict(applied)
    mismatched[SUNSYNK_WORKMODE_FIELD] = "9"
    s.note_settle("INV1", mismatched)
    before = s.settle_count.get("INV1", 0)
    if before != 1:
        print(f"ERROR: setup failed to establish a nonzero settle_count, got {before}")
        failed = True

    # A read-back that only echoes the request's serial number - none of the fields Predbat
    # actually owns and watches for.
    empty_of_owned = {SUNSYNK_SERIAL_FIELD: "INV1"}
    s.note_settle("INV1", empty_of_owned)
    after = s.settle_count.get("INV1", 0)
    if after != before:
        print(f"ERROR: a read-back with no owned keys changed settle_count from {before} to {after}")
        failed = True
    assert not failed, "test_note_settle_leaves_the_counter_unchanged_when_readback_has_no_owned_keys"


def test_external_changes_are_logged():
    """A setting changed outside Predbat is reported, not silently overwritten."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    before = {"sn": "INV1", "batteryLowCap": "10", "zeroExportPower": "20", "cap1": "50"}
    after = {"sn": "INV1", "batteryLowCap": "10", "zeroExportPower": "80", "cap1": "95"}
    s.note_external_change("INV1", before, after)
    joined = " ".join(str(m) for m in s.log_messages)
    if "zeroExportPower" not in joined:
        print(f"ERROR: an unowned field change was not reported: {joined}")
        failed = True
    # cap1 is Predbat's own field, so its change is expected and must not be reported.
    if "cap1" in joined:
        print("ERROR: a field Predbat owns was reported as an external change")
        failed = True
    # No change at all must stay quiet.
    quiet = MockSunsynk()
    quiet.note_external_change("INV1", before, dict(before))
    if quiet.log_messages:
        print(f"ERROR: an unchanged read logged anyway: {quiet.log_messages}")
        failed = True
    assert not failed, "test_external_changes_are_logged"


def test_solar_export_is_never_disabled():
    """solarSell must stay on in every state, including idle and charging.

    It governs whether surplus PV reaches the grid at all, not what the battery does.
    Deriving it from the active window - on only while exporting - would curtail spare
    solar for most daylight hours once the battery is full, silently costing export
    revenue, and Predbat has no notion of PV curtailment to notice it. An export window
    still exports via the selling-first work mode and the slot SoC targets.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    charge = {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"}
    export = {"enable": True, "soc": 20, "power": 3000, "start": "16:00:00", "end": "19:00:00"}
    cases = [
        ("idle midday", _schedule(reserve=10), 12 * 60),
        ("inside the charge window", _schedule(reserve=10, charge=charge), 3 * 60),
        ("inside the export window", _schedule(reserve=10, export=export), 17 * 60),
        ("both windows set, neither active", _schedule(reserve=10, charge=charge, export=export), 12 * 60),
    ]
    for name, schedule, minutes in cases:
        payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=minutes)
        if payload.get(SUNSYNK_SOLAR_SELL_FIELD) != "1":
            print(f"ERROR: {name} produced solarSell {payload.get(SUNSYNK_SOLAR_SELL_FIELD)!r}, expected '1'")
            failed = True
    assert not failed, "test_solar_export_is_never_disabled"


def test_non_export_states_use_limited_to_home():
    """Every non-export state must use zero_export_ct, and only exporting uses selling_first.

    The work mode gates whether the BATTERY exports; solarSell independently gates PV. A
    live system exported 11.1 kWh in a day while in "Limited to Home" with solarSell on, so
    the mode does not block solar. zero_export_load ("Limit To Load Only") measures at the
    inverter's own output rather than the grid CT, so on a CT-clamp install it would stop
    the battery serving loads not wired to the inverter and push them onto the grid.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    charge = {"enable": True, "soc": 95, "power": 3000}
    cases = [
        ("charge", _schedule(reserve=10, charge=charge), 50, "zero_export_ct"),
        ("freeze_charge", _schedule(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, "zero_export_ct"),
        ("hold_charge", _schedule(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, "zero_export_ct"),
        ("idle", _schedule(reserve=15), 60, "zero_export_ct"),
        ("export", _schedule(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, "selling_first"),
        ("freeze_export", _schedule(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, "selling_first"),
    ]
    for name, schedule, soc, expect in cases:
        got = s.derive_control_state(schedule, soc)["work_mode"]
        if got != SUNSYNK_WORKMODE[expect]:
            print(f"ERROR: {name} derived work mode {got!r}, expected {expect} ({SUNSYNK_WORKMODE[expect]!r})")
            failed = True
    # zero_export_load is never written: it is a real mode, just not one Predbat should pick.
    if SUNSYNK_WORKMODE["zero_export_load"] in {s.derive_control_state(sched, soc)["work_mode"] for _, sched, soc, _ in cases}:
        print("ERROR: zero_export_load was derived for some state")
        failed = True
    assert not failed, "test_non_export_states_use_limited_to_home"


def test_self_use_slots_are_never_zero_power():
    """A zero-power self-use slot would freeze the battery for most of the day.

    Zero power is how Sunsynk expresses a freeze - the battery neither charges nor
    discharges. Self-use slots cover every interval Predbat is not actively charging or
    exporting, so writing zero there would stop the battery serving the house and push the
    load onto the grid as the DEFAULT state.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "20", "pvMaxLimit": "7000"}
    sched = _schedule(reserve=20, charge={"enable": True, "soc": 90, "power": 3000, "start": "03:00:00", "end": "04:00:00"})
    payload = s.build_settings_payload("INV1", sched, current_soc=38, now_minutes=22 * 60)
    for n in range(1, TOU_SLOT_COUNT + 1):
        power = int(payload[TOU_FIELD["power"].format(n=n)])
        # The encoded flag is a STRING: "false" is truthy in Python, so compare it.
        charging = payload[TOU_FIELD["grid_charge"].format(n=n)] == "true"
        if not charging and power <= 0:
            print(f"ERROR: self-use slot {n} at {payload[TOU_FIELD['time'].format(n=n)]} has power {power} - that freezes the battery")
            failed = True
    # The charge slot carries the requested charge power, not the self-use power.
    charge_slots = [n for n in range(1, TOU_SLOT_COUNT + 1) if payload[TOU_FIELD["grid_charge"].format(n=n)] == "true"]
    if not charge_slots or int(payload[TOU_FIELD["power"].format(n=charge_slots[0])]) != 3000:
        print(f"ERROR: charge slot should carry 3000W, got {[payload[TOU_FIELD['power'].format(n=n)] for n in charge_slots]}")
        failed = True
    assert not failed, "test_self_use_slots_are_never_zero_power"


def test_freeze_states_are_expressed_as_zero_power():
    """Freeze charge and freeze export are expressed by zero slot power."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    freeze_charge = s.derive_control_state(_schedule(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50)
    if freeze_charge["behaviour"] != "freeze_charge" or freeze_charge["power"] != 0:
        print(f"ERROR: freeze_charge should have power 0, got {freeze_charge}")
        failed = True
    freeze_export = s.derive_control_state(_schedule(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80)
    if freeze_export["behaviour"] != "freeze_export" or freeze_export["power"] != 0:
        print(f"ERROR: freeze_export should have power 0, got {freeze_export}")
        failed = True
    # A real export still carries its requested power.
    real_export = s.derive_control_state(_schedule(reserve=10, export={"enable": True, "soc": 20, "power": 2500}), 80)
    if real_export["power"] != 2500:
        print(f"ERROR: a real export should keep its 2500W, got {real_export['power']}")
        failed = True
    assert not failed, "test_freeze_states_are_expressed_as_zero_power"


def test_freeze_export_reaches_the_inverter_from_the_charge_rate():
    """A Freeze Export written the way execute.py writes it must reach the inverter.

    This is the regression that mattered. Predbat expresses Freeze Export by turning the
    forced-export window OFF and calling adjust_charge_rate(0), because SunsynkCloud
    declares has_timed_pause False. It never writes 99 to the export SoC entity, so the
    export-SoC route into the freeze state could not fire, and charge_rate maps to a
    per-WINDOW field that a disabled window never reads. The result was a settings object
    byte-identical to plain Demand, leaving the inverter in self-use charging the battery
    from the very solar the freeze existed to export - confirmed live 2026-08-20, a 99%
    battery still taking 540 W of PV.

    So the fixture is the entity state after a freeze export, not a synthetic one, and the
    expected payload is the one confirmed live: m0 p0 c<reserve> s1.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "20"}
    # Both windows off, charge rate zeroed, export rate untouched.
    sched = _schedule(reserve=20, charge_power=0, export_power=1229)
    payload = s.build_settings_payload("INV1", sched, current_soc=95, now_minutes=12 * 60)

    if payload.get(SUNSYNK_WORKMODE_FIELD) != SUNSYNK_WORKMODE["selling_first"]:
        print(f"ERROR: freeze export left the work mode at {payload.get(SUNSYNK_WORKMODE_FIELD)!r}, expected selling_first - Limited to Home charges the battery from the surplus")
        failed = True
    for n in range(1, TOU_SLOT_COUNT + 1):
        power = int(payload[TOU_FIELD["power"].format(n=n)])
        soc = int(payload[TOU_FIELD["soc"].format(n=n)])
        sell = payload[TOU_FIELD["sell"].format(n=n)]
        grid_charge = payload[TOU_FIELD["grid_charge"].format(n=n)]
        # Every slot, not just the one covering now: Predbat never says when a freeze ends,
        # so a self-use filler left in the programme would defeat it the moment the clock
        # reached it.
        #
        # The rate is the safety property. At 8000 W with a cap below the SoC the battery
        # drained to the grid at 4.7 kW; at 0 W it held. Never relax this to the rating.
        if power != 0:
            print(f"ERROR: freeze export slot {n} carries {power}W, expected 0 - a non-zero sell rate exports the battery")
            failed = True
        # The cap is the reserve, which is the room the battery needs to cover the house.
        if soc != 20:
            print(f"ERROR: freeze export slot {n} caps at {soc}%, expected the 20% reserve")
            failed = True
        if sell != "1":
            print(f"ERROR: freeze export slot {n} sell flag {sell!r}, expected '1'")
            failed = True
        if grid_charge != "false":
            print(f"ERROR: freeze export slot {n} has grid charge {grid_charge!r}, a freeze must not charge")
            failed = True
    assert not failed, "test_freeze_export_reaches_the_inverter_from_the_charge_rate"


def test_freeze_export_caps_at_the_reserve_never_the_sentinel():
    """The freeze cap is the reserve, and never FREEZE_EXPORT_SOC whatever the SoC.

    99 is Predbat's MARKER for "this export window is a freeze" (export_limits_best of 99),
    not a battery level. Writing it through to cap{n} told a Selling First inverter to drive
    the battery to 99% - charging it from the solar the freeze exists to export, or selling
    down to 99% from above. That single field is the whole of the original bug; the rest of
    the mapping was right.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    for current_soc in (30, 55, 80, 95, 99, 100):
        for reserve in (5, 10, 20):
            for sched in (_schedule(reserve=reserve, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), _schedule(reserve=reserve, charge_power=0)):
                state = s.derive_control_state(sched, current_soc)
                if state["behaviour"] != "freeze_export":
                    print(f"ERROR: expected a freeze export at {current_soc}%, got {state['behaviour']}")
                    failed = True
                    continue
                if state["slot_soc"] != reserve:
                    print(f"ERROR: freeze export with reserve {reserve} capped at {state['slot_soc']}%, expected {reserve}")
                    failed = True
                if state["slot_soc"] == FREEZE_EXPORT_SOC and reserve != FREEZE_EXPORT_SOC:
                    print(f"ERROR: freeze export wrote the {FREEZE_EXPORT_SOC} sentinel as a battery level")
                    failed = True
                # The zero rate is what makes a cap below the SoC safe.
                if state["power"] != 0:
                    print(f"ERROR: freeze export carries rate {state['power']}, expected 0")
                    failed = True
    assert not failed, "test_freeze_export_caps_at_the_reserve_never_the_sentinel"


def test_hold_charge_keeps_predbat_rate_without_charging():
    """A charge target at or below current SoC must not trigger a charge, but keeps the rate.

    Grid charge stays off - there is nothing to charge - while the slot still carries
    Predbat's chosen charge rate for that window. Zero would mean freeze, a different
    state; the inverter's full rating would discard the rate Predbat asked for.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "20"}
    # Target 40% with the battery already at 60% -> hold_charge.
    state = s.derive_control_state(_schedule(reserve=20, charge={"enable": True, "soc": 40, "power": 3000}), 60)
    if state["behaviour"] != "hold_charge":
        print(f"ERROR: expected hold_charge, got {state['behaviour']}")
        failed = True
    if state["grid_charge"]:
        print("ERROR: hold_charge must not enable grid charge - there is nothing to charge")
        failed = True
    if state["power"] != 3000:
        print(f"ERROR: hold_charge should keep Predbat's 3000W rate, got {state['power']}")
        failed = True
    sched = _schedule(reserve=20, charge={"enable": True, "soc": 40, "power": 3000, "start": "03:00:00", "end": "04:00:00"})
    payload = s.build_settings_payload("INV1", sched, current_soc=60, now_minutes=3 * 60 + 30)
    hold = [n for n in range(1, TOU_SLOT_COUNT + 1) if payload[TOU_FIELD["time"].format(n=n)] == "03:00"]
    if not hold:
        print("ERROR: no slot at the window start")
        failed = True
    else:
        n = hold[0]
        if payload[TOU_FIELD["grid_charge"].format(n=n)] == "true":
            print("ERROR: the hold-charge slot enabled grid charge")
            failed = True
        if int(payload[TOU_FIELD["power"].format(n=n)]) != 3000:
            print(f"ERROR: hold-charge slot power {payload[TOU_FIELD['power'].format(n=n)]}, expected Predbat's 3000W")
            failed = True
    assert not failed, "test_hold_charge_keeps_predbat_rate_without_charging"


def test_payload_is_limited_to_the_system_mode_group():
    """Only the System Mode group may be posted; a larger object is silently discarded.

    Confirmed live: posting the full 350-key settings object returned success and changed
    nothing, twice, while the same change sent as the 53-key group persisted immediately.
    Fields inside the group that Predbat does not own must still be carried through, and
    fields outside it must never be sent at all.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    # A realistic blob: System Mode fields Predbat does not own, plus unrelated groups.
    s.device_settings["INV1"] = {
        "sn": "INV1",
        "batteryLowCap": "20",
        "sysWorkMode": "2",
        "solarSell": "1",
        "peakAndVallery": "1",
        "safetyType": "3",
        "battMode": "1",
        "energyMode": "1",
        "zeroExportPower": "0",
        "solarMaxSellPower": "9200",
        "pvMaxLimit": "7000",
        "sellTime1Volt": "49.0",
        "genTime1on": "false",
        # outside the group - battery and grid settings that a schedule write must not touch
        "batteryShutdownCap": "5",
        "batteryMaxCurrentCharge": "100",
        "importPower": "10350",
        "acOutputPowerLimit": "14464",
        "someUnknownKnob": "42",
    }
    sched = _schedule(reserve=20, charge={"enable": True, "soc": 90, "power": 3000, "start": "03:00:00", "end": "04:00:00"})
    payload = s.build_settings_payload("INV1", sched, current_soc=40, now_minutes=3 * 60 + 30)
    outside = [k for k in payload if k not in SUNSYNK_SYSTEM_MODE_FIELDS]
    if outside:
        print(f"ERROR: payload carries fields outside the System Mode group: {sorted(outside)}")
        failed = True
    for k in ("batteryShutdownCap", "batteryMaxCurrentCharge", "importPower", "acOutputPowerLimit", "someUnknownKnob"):
        if k in payload:
            print(f"ERROR: {k} must never be sent by a schedule write")
            failed = True
    # Unowned fields INSIDE the group must survive verbatim.
    for k, expect in (("safetyType", "3"), ("battMode", "1"), ("energyMode", "1"), ("zeroExportPower", "0"), ("solarMaxSellPower", "9200"), ("pvMaxLimit", "7000"), ("sellTime1Volt", "49.0"), ("genTime1on", "false")):
        if str(payload.get(k)) != expect:
            print(f"ERROR: in-group unowned field {k} became {payload.get(k)!r}, expected {expect!r}")
            failed = True
    assert not failed, "test_payload_is_limited_to_the_system_mode_group"


def test_export_slots_set_the_sell_flag():
    """A forced export slot must set sellTime{n}En, and it must be present on every slot.

    CONFIRMED live: with sellTime{n}En absent from the payload the API silently discarded
    time{n}on on six consecutive writes across every encoding tried, while the rest of the
    same write persisted. It validates the per-slot field set as a whole. The flags are
    independent once all are present.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "20"}
    sched = _schedule(reserve=20, export={"enable": True, "soc": 20, "power": 2500, "start": "16:00:00", "end": "19:00:00"})
    payload = s.build_settings_payload("INV1", sched, current_soc=80, now_minutes=17 * 60)
    # Present on every slot, or the API drops the flags.
    for n in range(1, TOU_SLOT_COUNT + 1):
        name = TOU_FIELD["sell"].format(n=n)
        if name not in payload:
            print(f"ERROR: {name} missing - the API needs the full per-slot set or it drops the flags")
            failed = True
        elif payload[name] not in ("0", "1"):
            print(f"ERROR: {name} = {payload[name]!r}, expected the numeric string '0' or '1'")
            failed = True
    # The export slot sells; the self-use slots do not.
    selling = [n for n in range(1, TOU_SLOT_COUNT + 1) if payload.get(TOU_FIELD["sell"].format(n=n)) == "1"]
    if not selling:
        print("ERROR: an export window produced no slot with the Sell flag set")
        failed = True
    for n in selling:
        if payload[TOU_FIELD["time"].format(n=n)] != "16:00":
            print(f"ERROR: Sell set on slot {n} at {payload[TOU_FIELD['time'].format(n=n)]}, expected the 16:00 export slot")
            failed = True
    # A charge-only schedule must not set Sell anywhere.
    charge_sched = _schedule(reserve=20, charge={"enable": True, "soc": 90, "power": 3000, "start": "03:00:00", "end": "04:00:00"})
    charge_payload = s.build_settings_payload("INV1", charge_sched, current_soc=40, now_minutes=3 * 60 + 30)
    if any(charge_payload.get(TOU_FIELD["sell"].format(n=n)) == "1" for n in range(1, TOU_SLOT_COUNT + 1)):
        print("ERROR: a charge-only schedule set the Sell flag")
        failed = True
    assert not failed, "test_export_slots_set_the_sell_flag"


def test_server_derived_slot_fields_are_never_written():
    """time{n}On (capital O) is server-derived and must never be transmitted.

    It changed from '0' to '65' on a write that did not mention it, and writing '1' to it
    also produced '65'. It is not the boolean it resembles.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "20", **{f"time{n}On": "0" for n in range(1, TOU_SLOT_COUNT + 1)}}
    payload = s.build_settings_payload("INV1", _schedule(reserve=20), current_soc=50, now_minutes=12 * 60)
    leaked = [f"time{n}On" for n in range(1, TOU_SLOT_COUNT + 1) if f"time{n}On" in payload]
    if leaked:
        print(f"ERROR: server-derived fields transmitted: {leaked}")
        failed = True
    assert not failed, "test_server_derived_slot_fields_are_never_written"


def run_sunsynk_control_tests(my_predbat):
    """Run all Sunsynk control-logic tests."""
    failed = False
    for name, fn in [
        ("derive_state_table", test_derive_control_state_table),
        ("non_export_mode", test_non_export_states_use_limited_to_home),
        ("tou_slots_shape", test_build_tou_slots_shape),
        ("self_use_never_zero", test_self_use_slots_are_never_zero_power),
        ("freeze_zero_power", test_freeze_states_are_expressed_as_zero_power),
        ("freeze_export_reaches_inverter", test_freeze_export_reaches_the_inverter_from_the_charge_rate),
        ("freeze_export_cap", test_freeze_export_caps_at_the_reserve_never_the_sentinel),
        ("hold_charge_rate", test_hold_charge_keeps_predbat_rate_without_charging),
        ("tou_slots_seconds", test_build_tou_slots_seconds_are_dropped),
        ("tou_slots_idle", test_build_tou_slots_idle_is_still_six_distinct),
        ("tou_slots_zero_length_window", test_build_tou_slots_zero_length_window_has_no_effect),
        ("active_window_mode", test_active_window_drives_the_global_mode),
        ("midnight_wrap", test_window_active_handles_midnight_wrap),
        ("payload_field_types", test_payload_renders_indexed_fields_and_types),
        ("solar_export_never_off", test_solar_export_is_never_disabled),
        ("payload_preserves", test_payload_preserves_unowned_settings),
        ("payload_system_mode_only", test_payload_is_limited_to_the_system_mode_group),
        ("export_sell_flag", test_export_slots_set_the_sell_flag),
        ("no_derived_fields", test_server_derived_slot_fields_are_never_written),
        ("payload_soc_floor", test_payload_clamps_to_the_inverter_soc_floor),
        ("payload_no_baseline_is_empty", test_build_settings_payload_returns_empty_without_a_baseline),
        ("payloads_equal", test_payloads_equal_ignores_nothing_material),
        ("apply_skips_unchanged", test_apply_settings_skips_an_unchanged_payload),
        ("apply_noop_ticks_no_io", test_apply_settings_noop_ticks_perform_no_io),
        ("apply_changed_one_get_one_post", test_apply_settings_changed_plan_performs_one_get_and_one_post),
        ("apply_ignores_volatile_field", test_apply_settings_ignores_a_volatile_unowned_field),
        ("apply_fails_closed", test_apply_settings_fails_closed_without_a_read),
        ("apply_control_enable", test_apply_settings_respects_control_enable),
        ("reconcile_applies_controlled", test_reconcile_control_applies_a_controlled_inverter),
        ("reconcile_skips_uncontrolled", test_reconcile_control_skips_an_uncontrolled_inverter),
        ("reconcile_skips_read_only", test_reconcile_control_skips_while_read_only),
        ("apply_skips_empty_payload", test_apply_settings_skips_when_the_payload_is_empty),
        ("apply_failed_write", test_apply_settings_reports_a_failed_write),
        ("note_settle_normalises_types", test_note_settle_normalises_wire_types_before_comparing),
        ("note_settle_catches_power_divergence", test_note_settle_catches_a_divergent_power_field),
        ("note_settle_catches_solar_sell_divergence", test_note_settle_catches_a_divergent_solar_sell),
        ("note_settle_catches_tou_enable_divergence", test_note_settle_catches_a_divergent_tou_enable),
        ("note_settle_catches_day_flag_divergence", test_note_settle_catches_a_divergent_day_flag),
        ("note_settle_ignores_missing_owned_keys", test_note_settle_ignores_a_readback_missing_some_owned_keys),
        ("note_settle_keeps_count_when_readback_empty", test_note_settle_leaves_the_counter_unchanged_when_readback_has_no_owned_keys),
        ("external_changes", test_external_changes_are_logged),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

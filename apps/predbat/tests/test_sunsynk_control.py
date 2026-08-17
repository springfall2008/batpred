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
    SUNSYNK_WORKMODE,
    SUNSYNK_WORKMODE_FIELD,
    SUNSYNK_SOLAR_SELL_FIELD,
    SUNSYNK_TOU_ENABLE_FIELD,
    SUNSYNK_SERIAL_FIELD,
    SUNSYNK_DAY_FIELDS,
    FREEZE_EXPORT_SOC,
    TOU_FIELD,
    TOU_SLOT_COUNT,
)
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


def _schedule(reserve=10, charge=None, export=None):
    """Build a schedule dict in the shape the control entities produce."""
    idle = {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": charge or dict(idle), "export": export or dict(idle)}


def test_derive_control_state_table():
    """Each Predbat intent maps to the work mode and flags the spec's table requires."""
    failed = False
    s = MockSunsynk()
    cases = [
        ("charge", _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50, ("charge", SUNSYNK_WORKMODE["zero_export_load"], True, False, 90)),
        ("freeze_charge", _schedule(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, ("freeze_charge", SUNSYNK_WORKMODE["zero_export_load"], True, False, 50)),
        ("hold_charge", _schedule(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, ("hold_charge", SUNSYNK_WORKMODE["zero_export_load"], False, False, 50)),
        ("export", _schedule(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, ("export", SUNSYNK_WORKMODE["selling_first"], False, True, 20)),
        ("freeze_export", _schedule(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, ("freeze_export", SUNSYNK_WORKMODE["selling_first"], False, True, FREEZE_EXPORT_SOC)),
        ("idle", _schedule(reserve=15), 60, ("idle", SUNSYNK_WORKMODE["zero_export_load"], False, False, 15)),
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
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    slots = s.build_tou_slots(schedule, current_soc=40)
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
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:30:00", "end": "05:45:00"})
    times = [slot["time"] for slot in s.build_tou_slots(schedule, current_soc=40)]
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
    slots = s.build_tou_slots(_schedule(reserve=15), current_soc=50)
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
    cases = [
        _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "00:00:00", "end": "00:00:00"}),
        _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "02:00"}),
    ]
    for schedule in cases:
        slots = s.build_tou_slots(schedule, current_soc=40)
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
    s.device_settings["INV1"] = {"sn": "INV1", "batteryLowCap": "10"}
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "16:00:00", "end": "19:00:00"},
    )
    # 03:00 -> inside the charge window.
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["zero_export_load"]:
        print(f"ERROR: at 03:00 expected zero_export_load, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    # 17:00 -> inside the export window.
    payload = s.build_settings_payload("INV1", schedule, current_soc=80, now_minutes=17 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["selling_first"]:
        print(f"ERROR: at 17:00 expected selling_first, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    # 12:00 -> neither window, so self-use.
    payload = s.build_settings_payload("INV1", schedule, current_soc=60, now_minutes=12 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["zero_export_load"]:
        print(f"ERROR: at 12:00 expected zero_export_load, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    assert not failed, "test_active_window_drives_the_global_mode"


def test_window_active_handles_midnight_wrap():
    """A window running past midnight is active on both sides of it."""
    failed = False
    s = MockSunsynk()
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
                if not isinstance(value, bool):
                    print(f"ERROR: {name} = {value!r} should be a bare JSON boolean")
                    failed = True
            elif not isinstance(value, str):
                print(f"ERROR: {name} = {value!r} should be a string")
                failed = True
    for day in SUNSYNK_DAY_FIELDS:
        if payload.get(day) is not True:
            print(f"ERROR: {day} should be True, got {payload.get(day)!r}")
            failed = True
    if payload.get(SUNSYNK_TOU_ENABLE_FIELD) != "1":
        print(f"ERROR: TOU master enable should be '1', got {payload.get(SUNSYNK_TOU_ENABLE_FIELD)!r}")
        failed = True
    if payload.get(SUNSYNK_SERIAL_FIELD) != "INV1":
        print(f"ERROR: serial should be echoed back, got {payload.get(SUNSYNK_SERIAL_FIELD)!r}")
        failed = True
    if payload.get(SUNSYNK_SOLAR_SELL_FIELD) not in ("0", "1"):
        print(f"ERROR: solarSell should be '0' or '1', got {payload.get(SUNSYNK_SOLAR_SELL_FIELD)!r}")
        failed = True
    assert not failed, "test_payload_renders_indexed_fields_and_types"


def test_payload_preserves_unowned_settings():
    """Read-modify-write leaves every field Predbat does not own exactly as it was."""
    failed = False
    s = MockSunsynk()
    s.device_settings["INV1"] = {
        "sn": "INV1",
        "batteryShutdownCap": "5",
        "batteryLowCap": "10",
        "safetyType": "3",
        "zeroExportPower": "20",
        "solarMaxSellPower": "8000",
        "genTime1on": False,
        "sellTime1Volt": "49.0",
        "batteryMaxCurrentCharge": "100",
    }
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    for key, expect in (("batteryShutdownCap", "5"), ("safetyType", "3"), ("zeroExportPower", "20"), ("solarMaxSellPower", "8000"), ("batteryMaxCurrentCharge", "100"), ("genTime1on", False), ("sellTime1Volt", "49.0")):
        if payload.get(key) != expect:
            print(f"ERROR: unowned field {key} became {payload.get(key)!r}, expected {expect!r}")
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


def test_apply_settings_skips_when_the_payload_is_empty():
    """apply_settings must not post a payload that build_settings_payload refused to build.

    build_settings_payload returns {} when there is no baseline to modify. This is
    contrived here via a direct patch (the normal fail-closed read check makes it
    otherwise unreachable through apply_settings's own flow), specifically to prove the
    caller-side guard exists too and does not depend on that other check alone.
    """
    failed = False
    s = MockSunsynk()
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


def test_external_changes_are_logged():
    """A setting changed outside Predbat is reported, not silently overwritten."""
    failed = False
    s = MockSunsynk()
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


def run_sunsynk_control_tests(my_predbat):
    """Run all Sunsynk control-logic tests."""
    failed = False
    for name, fn in [
        ("derive_state_table", test_derive_control_state_table),
        ("tou_slots_shape", test_build_tou_slots_shape),
        ("tou_slots_seconds", test_build_tou_slots_seconds_are_dropped),
        ("tou_slots_idle", test_build_tou_slots_idle_is_still_six_distinct),
        ("tou_slots_zero_length_window", test_build_tou_slots_zero_length_window_has_no_effect),
        ("active_window_mode", test_active_window_drives_the_global_mode),
        ("midnight_wrap", test_window_active_handles_midnight_wrap),
        ("payload_field_types", test_payload_renders_indexed_fields_and_types),
        ("payload_preserves", test_payload_preserves_unowned_settings),
        ("payload_soc_floor", test_payload_clamps_to_the_inverter_soc_floor),
        ("payload_no_baseline_is_empty", test_build_settings_payload_returns_empty_without_a_baseline),
        ("payloads_equal", test_payloads_equal_ignores_nothing_material),
        ("apply_skips_unchanged", test_apply_settings_skips_an_unchanged_payload),
        ("apply_noop_ticks_no_io", test_apply_settings_noop_ticks_perform_no_io),
        ("apply_changed_one_get_one_post", test_apply_settings_changed_plan_performs_one_get_and_one_post),
        ("apply_ignores_volatile_field", test_apply_settings_ignores_a_volatile_unowned_field),
        ("apply_fails_closed", test_apply_settings_fails_closed_without_a_read),
        ("apply_control_enable", test_apply_settings_respects_control_enable),
        ("apply_skips_empty_payload", test_apply_settings_skips_when_the_payload_is_empty),
        ("apply_failed_write", test_apply_settings_reports_a_failed_write),
        ("note_settle_normalises_types", test_note_settle_normalises_wire_types_before_comparing),
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

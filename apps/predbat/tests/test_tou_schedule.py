# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test the shared Time-of-Use slot programme builder
# -----------------------------------------------------------------------------

"""Tests for TouScheduleMixin, the TOU programme builder shared by DEYE and Sunsynk.

These exercise the mixin directly against a minimal fake component, so they hold whatever
either vendor does with the slots afterwards. The vendor-level equivalents live in
test_deye_control.py and test_sunsynk_control.py.
"""

from tou_schedule import TouScheduleMixin, TOU_PADDING_STEP, MINUTES_PER_DAY

SLOT_COUNT = 6


class FakeTouComponent(TouScheduleMixin):
    """Smallest component the mixin can drive: states are plain labels, slots plain dicts."""

    TOU_SLOTS = SLOT_COUNT

    def derive_control_state(self, intent, current_soc):
        """Label the intent as charge, export or idle, carrying the target SoC."""
        if intent.get("charge", {}).get("enable"):
            return {"action": "charge", "slot_soc": intent["charge"].get("soc", 0), "grid_charge": True, "power": 3000}
        if intent.get("export", {}).get("enable"):
            return {"action": "export", "slot_soc": intent["export"].get("soc", 0), "solar_sell": True, "power": 3000}
        return {"action": "idle", "slot_soc": intent.get("reserve", 0), "grid_charge": False, "power": 0}

    def _slot_for(self, start_time, state, reserve, self_use_power):
        """Encode a slot as the time plus the state's label and SoC."""
        return {"time": start_time, "action": state["action"], "soc": state["slot_soc"]}


def _schedule(charge=None, export=None, reserve=10):
    """Build a schedule dict in the shape build_tou_slots expects."""
    sched = {"reserve": reserve, "charge": {"enable": False, "soc": 0, "power": 3000}, "export": {"enable": False, "soc": 0, "power": 3000}}
    if charge:
        sched["charge"] = {"enable": True, "soc": 95, "power": 3000, "start": charge[0], "end": charge[1]}
    if export:
        sched["export"] = {"enable": True, "soc": 20, "power": 3000, "start": export[0], "end": export[1]}
    return sched


def _build(charge=None, export=None, reserve=10):
    """Run the builder over a schedule and return its slots."""
    return FakeTouComponent().build_tou_slots(_schedule(charge, export, reserve), current_soc=50, self_use_power=5000)


def _hm(text):
    """Convert an HH:MM slot time to minutes past midnight."""
    hours, minutes = str(text).split(":")
    return int(hours) * 60 + int(minutes)


def _action_at(slots, minute):
    """Return the action in force at a minute past midnight.

    A slot runs from its own start until the next slot starts, and the programme loops at
    midnight, so before the first slot the last one is still running.
    """
    action = slots[-1]["action"]
    for slot in slots:
        if _hm(slot["time"]) <= minute:
            action = slot["action"]
        else:
            break
    return action


def _covered(slots, start, end, action):
    """Return the minutes between start and end NOT showing the expected action."""
    first, last = _hm(start), _hm(end)
    minutes = range(first, last) if first < last else list(range(first, MINUTES_PER_DAY)) + list(range(0, last))
    return [minute for minute in minutes if _action_at(slots, minute) != action]


def test_programme_shape():
    """Every programme is exactly TOU_SLOTS slots, distinct, ascending, starting at 00:00."""
    failed = False
    cases = {
        "idle": _build(),
        "one charge window": _build(charge=("02:00", "05:00")),
        "charge and export": _build(charge=("02:00", "05:00"), export=("16:00", "19:00")),
        "charge wrapping midnight": _build(charge=("22:00", "02:00")),
        "wrapping charge and export": _build(charge=("23:30", "05:30"), export=("16:00", "19:00")),
        "window ending at midnight": _build(charge=("22:00", "00:00")),
        "window starting at midnight": _build(charge=("00:00", "06:00")),
    }
    for label, slots in cases.items():
        times = [slot["time"] for slot in slots]
        if len(slots) != SLOT_COUNT:
            print(f"ERROR: {label} produced {len(slots)} slots, expected {SLOT_COUNT}: {times}")
            failed = True
        if len(set(times)) != len(times):
            print(f"ERROR: {label} has duplicate slot times: {times}")
            failed = True
        if times != sorted(times):
            print(f"ERROR: {label} slot times are not ascending: {times}")
            failed = True
        if times and times[0] != "00:00":
            print(f"ERROR: {label} must start at 00:00, got {times[0]}")
            failed = True
    assert not failed, "test_programme_shape"


def test_padding_never_interrupts_a_window():
    """A window runs unbroken from its start to its end, whatever padding is added.

    GH#5156: padding used to be placed at fixed clock hours, so any window crossing one of
    them was silently ended there while Predbat's plan still showed it running.
    """
    failed = False
    cases = [
        ("reported day 2", ("09:45", "15:30"), None, "charge"),
        ("reported day 1", ("11:15", "15:30"), None, "charge"),
        ("overnight crossing 04:00", ("00:30", "07:30"), None, "charge"),
        ("export crossing 12:00", None, ("11:00", "13:00"), "export"),
        ("charge crossing 08:00 with an export", ("06:00", "10:00"), ("16:00", "19:00"), "charge"),
    ]
    for label, charge, export, action in cases:
        window = charge or export
        slots = _build(charge=charge, export=export)
        missed = _covered(slots, window[0], window[1], action)
        if missed:
            first = missed[0]
            print(f"ERROR: {label} {window[0]}-{window[1]} loses {len(missed)} minutes from {first // 60:02d}:{first % 60:02d}: {[s['time'] for s in slots]}")
            failed = True
    assert not failed, "test_padding_never_interrupts_a_window"


def test_window_spanning_midnight_is_split():
    """A wrapping window becomes a pre-midnight and a post-midnight slot, charging throughout.

    No slot may span midnight, so a 22:00-02:00 charge has to be written as 22:00-00:00 plus
    00:00-02:00. Without the split the post-midnight half was silently dropped, because the
    00:00 slot was always seeded idle.
    """
    failed = False
    for label, charge in (("22:00-02:00", ("22:00", "02:00")), ("23:30-05:30", ("23:30", "05:30"))):
        slots = _build(charge=charge)
        missed = _covered(slots, charge[0], charge[1], "charge")
        if missed:
            first = missed[0]
            print(f"ERROR: wrapping charge {label} loses {len(missed)} minutes from {first // 60:02d}:{first % 60:02d}: {[(s['time'], s['action']) for s in slots]}")
            failed = True
        if slots[0]["action"] != "charge":
            print(f"ERROR: wrapping charge {label} must leave slot 1 at 00:00 charging, got {slots[0]}")
            failed = True
        # And it must stop at the window's end rather than running all day.
        if _action_at(slots, _hm(charge[1]) + 1) != "idle":
            print(f"ERROR: wrapping charge {label} did not return to idle after {charge[1]}")
            failed = True
    assert not failed, "test_window_spanning_midnight_is_split"


def test_split_at_midnight_halves():
    """_split_at_midnight returns halves that never span midnight."""
    failed = False
    expected = {
        ("02:00", "05:00"): [("02:00", "05:00")],
        ("22:00", "02:00"): [("00:00", "02:00"), ("22:00", "00:00")],
        ("22:00", "00:00"): [("22:00", "00:00")],
        ("00:00", "06:00"): [("00:00", "06:00")],
    }
    for (start, end), want in expected.items():
        got = TouScheduleMixin._split_at_midnight(start, end)
        if got != want:
            print(f"ERROR: split {start}-{end} expected {want} got {got}")
            failed = True
        for half_start, half_end in got:
            if half_end != "00:00" and half_start >= half_end:
                print(f"ERROR: split {start}-{end} produced a non-forward half {half_start}-{half_end}")
                failed = True
    assert not failed, "test_split_at_midnight_halves"


def test_padding_sits_after_the_last_slot():
    """Padding follows the last real slot, carrying its state, at TOU_PADDING_STEP intervals."""
    failed = False
    slots = _build(charge=("02:00", "05:00"))
    times = [slot["time"] for slot in slots]
    # 00:00 idle, 02:00 charge, 05:00 idle, then three padding slots after 05:00.
    if times[:3] != ["00:00", "02:00", "05:00"]:
        print(f"ERROR: expected the schedule's own boundaries first, got {times}")
        failed = True
    expected_padding = [f"05:{TOU_PADDING_STEP * step:02d}" for step in range(1, 4)]
    if times[3:] != expected_padding:
        print(f"ERROR: expected padding at {expected_padding}, got {times[3:]}")
        failed = True
    if any(slot["action"] != "idle" for slot in slots[3:]):
        print(f"ERROR: padding must carry the state it follows (idle), got {slots[3:]}")
        failed = True
    assert not failed, "test_padding_sits_after_the_last_slot"


def test_padding_falls_back_when_the_day_runs_out():
    """Padding that will not fit before midnight moves into the widest gap instead.

    A window ending at 23:50 leaves only 10 minutes before the programme loops, which is not
    enough for the slots still to be placed. They go into the largest gap instead, where they
    carry that gap's own state and so still change nothing.
    """
    failed = False
    slots = _build(charge=("23:40", "23:50"))
    times = [slot["time"] for slot in slots]
    if len(times) != SLOT_COUNT or len(set(times)) != SLOT_COUNT:
        print(f"ERROR: expected {SLOT_COUNT} distinct times, got {times}")
        failed = True
    if times != sorted(times):
        print(f"ERROR: padding fallback broke the ascending order: {times}")
        failed = True
    if any(_hm(time) >= MINUTES_PER_DAY for time in times):
        print(f"ERROR: a padding slot ran past midnight: {times}")
        failed = True
    # The window itself must survive the fallback.
    missed = _covered(slots, "23:40", "23:50", "charge")
    if missed:
        print(f"ERROR: the fallback padding truncated the window: {[(s['time'], s['action']) for s in slots]}")
        failed = True
    assert not failed, "test_padding_falls_back_when_the_day_runs_out"


def test_idle_schedule_holds_one_state_all_day():
    """With no windows every slot is idle, so the padding cannot introduce a change.

    The degenerate case: one state for 24 hours still has to fill TOU_SLOTS slots, and they
    all say the same thing rather than needing the day split artificially.
    """
    failed = False
    slots = _build()
    if any(slot["action"] != "idle" for slot in slots):
        print(f"ERROR: an idle schedule must be idle in every slot, got {slots}")
        failed = True
    if _covered(slots, "00:00", "23:59", "idle"):
        print(f"ERROR: an idle schedule must read idle at every minute, got {slots}")
        failed = True
    assert not failed, "test_idle_schedule_holds_one_state_all_day"


def test_zero_length_window_is_ignored():
    """A window whose start equals its end has no interval and must not reach the slots."""
    failed = False
    slots = _build(charge=("03:00", "03:00"))
    if any(slot["action"] != "idle" for slot in slots):
        print(f"ERROR: a zero-length window must produce no action slot, got {slots}")
        failed = True
    # The same via the HH:MM:SS form the control entities actually carry.
    sched = _schedule()
    sched["charge"] = {"enable": True, "soc": 95, "power": 3000, "start": "03:00:00", "end": "03:00"}
    if any(slot["action"] != "idle" for slot in FakeTouComponent().build_tou_slots(sched, current_soc=50, self_use_power=5000)):
        print("ERROR: a zero-length window must be caught on the normalised times too")
        failed = True
    assert not failed, "test_zero_length_window_is_ignored"


def run_tou_schedule_tests(my_predbat):
    """Run all shared TOU programme builder tests."""
    failed = False
    for name, fn in [
        ("programme_shape", test_programme_shape),
        ("padding_no_interruption", test_padding_never_interrupts_a_window),
        ("midnight_split", test_window_spanning_midnight_is_split),
        ("split_halves", test_split_at_midnight_halves),
        ("padding_after_last", test_padding_sits_after_the_last_slot),
        ("padding_fallback", test_padding_falls_back_when_the_day_runs_out),
        ("idle_all_day", test_idle_schedule_holds_one_state_all_day),
        ("zero_length_window", test_zero_length_window_is_ignored),
    ]:
        try:
            if fn():
                print(f"  FAILED: tou_schedule.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in tou_schedule.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

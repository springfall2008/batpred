# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for Octopus.build_dispatch_timeline() and dispatch_timeline_should_log() - the #4516
Stage 1 diagnostic dispatch-timeline render and its heartbeat/change-triggered logging decision.
Purely observational (not used for any rate/plan decision yet), so these tests only check the
rendered status string and the logging decision, not any side effect on rates or the plan.
"""

from datetime import datetime, timedelta

import pytz

UTC = pytz.UTC
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _slot(start, end):
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "source": "smart-charge", "location": "AT_HOME"}


def _bare_slot(start, end):
    """A slot with only start/end - the real shape of a started_dispatches entry from the HA
    Octopus Energy integration (no kWh, source or location)."""
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT)}


def _render(blocks, now_block):
    """Join an expected block list the way build_dispatch_timeline() does, inserting the '|'
    now-marker between columns so test expectations stay written in plain block terms."""
    return "".join(blocks[:now_block]) + "|" + "".join(blocks[now_block:])


def run_dispatch_timeline_tests(my_predbat):
    """
    Test for build_dispatch_timeline() - the #4516 Stage 1 diagnostic dispatch-timeline render.
    """
    failed = False
    print("**** Running dispatch_timeline tests ****")

    # Save state that will be mutated
    saved_now_utc = my_predbat.now_utc
    saved_midnight_utc = my_predbat.midnight_utc
    saved_minutes_now = my_predbat.minutes_now
    saved_dispatch_timeline_last = my_predbat.dispatch_timeline_last
    saved_charge_window_best = my_predbat.charge_window_best
    saved_charge_limit_best = my_predbat.charge_limit_best
    saved_dispatch_timeline_pending = my_predbat.dispatch_timeline_pending
    saved_dispatch_unconfirmed_last = my_predbat.dispatch_unconfirmed_last
    saved_rate_import = my_predbat.rate_import
    saved_rate_import_cost_threshold = getattr(my_predbat, "rate_import_cost_threshold", None)

    try:
        # Fixed, mutually-consistent midnight/now so this test doesn't depend on (or leak into)
        # whatever the ambient fixture state happens to be elsewhere in a full suite run.
        midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        my_predbat.midnight_utc = midnight_utc
        my_predbat.now_utc = midnight_utc + timedelta(hours=10)
        my_predbat.minutes_now = 10 * 60

        # Default window: -4h..+24h at 30-min step = 56 blocks. "now" (offset 0) sits at block 8 (4h * 2).
        NUM_BLOCKS = 56
        NOW_BLOCK = 8

        # No plan by default: the existing cases below assert uppercase letters, which only hold
        # when Predbat is not charging in those blocks
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []

        # A known, all-expensive rate set so the background is a flat '.' and these cases test
        # dispatch placement only. Setting it explicitly also pins it against a full-suite run,
        # which leaves ambient rate_import behind from earlier tests.
        _origin = (my_predbat.minutes_now // 30) * 30 - 4 * 60
        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        my_predbat.rate_import_cost_threshold = 10.0

        print("Test 1: nothing scheduled - all dots")
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if timeline != _render(["."] * NUM_BLOCKS, NOW_BLOCK):
            print("  ERROR: expected all dots, got {!r}".format(timeline))
            failed = True

        print("Test 2: a single future planned slot renders as P at the right offset")
        slot_start = midnight_utc + timedelta(hours=12)  # 2h from now (now=10:00) -> offset block NOW_BLOCK+4
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "p"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 3: a currently-active started slot renders as S")
        slot_start = midnight_utc + timedelta(hours=10)  # now
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [_slot(slot_start, slot_end)], [])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK] = "s"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 4: a past completed slot renders as C, placed before the 'now' column")
        slot_start = midnight_utc + timedelta(hours=9)  # 1h before now -> offset block NOW_BLOCK-2
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [_slot(slot_start, slot_end)], [], [])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK - 2] = "c"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 5: when completed, started and planned all cover the same block, completed wins")
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(minutes=30)
        slot = _slot(slot_start, slot_end)
        timeline = my_predbat.build_dispatch_timeline(0, [slot], [slot], [slot])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "c"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected completed to win over started/planned, got {!r}".format(timeline))
            failed = True

        print("Test 6: started wins over planned when completed is absent")
        timeline = my_predbat.build_dispatch_timeline(0, [], [slot], [slot])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "s"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected started to win over planned, got {!r}".format(timeline))
            failed = True

        print("Test 7: a slot entirely outside the window has no effect and does not crash")
        slot_start = midnight_utc + timedelta(days=3)  # far beyond +24h
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        if timeline != _render(["."] * NUM_BLOCKS, NOW_BLOCK):
            print("  ERROR: expected all dots for an out-of-window slot, got {!r}".format(timeline))
            failed = True

        print("Test 8: a slot spanning multiple 30-min blocks marks every block it touches")
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(hours=1, minutes=30)  # 3 blocks
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "p"
        expected[NOW_BLOCK + 5] = "p"
        expected[NOW_BLOCK + 6] = "p"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected 3 consecutive P blocks, got {!r}".format(timeline))
            failed = True
        print("Test 9: a planned slot Predbat charges in renders uppercase")
        slot_start = midnight_utc + timedelta(hours=12)  # 2h from now -> block NOW_BLOCK+4
        slot_end = slot_start + timedelta(minutes=30)
        my_predbat.charge_window_best = [{"start": 12 * 60, "end": 12 * 60 + 30}]
        my_predbat.charge_limit_best = [5.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "P"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a slot Predbat plans to charge in should be 'P', expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 10: a charge window with a zero limit does not uppercase the slot")
        my_predbat.charge_limit_best = [0.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "p"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a zero-limit window is not a charge, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 11: a bare started slot (start/end only, no kWh/source/location) still renders as S")
        # Copilot review on #4948: real started_dispatches entries from the HA Octopus Energy
        # integration carry only start/end. decode_octopus_slot()'s "remove empty slots" check
        # (no kWh, source or location) would otherwise decode a real one as empty, so 'S' could
        # never render for real data - every other test here uses _slot(), which adds source and
        # location and masked this.
        slot_start = midnight_utc + timedelta(hours=10)  # now
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [_bare_slot(slot_start, slot_end)], [])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK] = "s"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a bare start/end-only started slot should still render as S, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 12: a charge window where there is no slot marks the import, not a dispatch")
        # This used to assert the background was left untouched. Losing a committed import the
        # moment its dispatch was withdrawn was the blind spot; it now renders 'X' (expensive) or
        # 'I' (cheap). See tests 25-28.
        my_predbat.charge_window_best = [{"start": 14 * 60, "end": 14 * 60 + 30}]
        my_predbat.charge_limit_best = [5.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 8] = "X"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a planned import with no dispatch should read 'X', expected {!r}, got {!r}".format(expected, timeline))
            failed = True
        if "p" in timeline or "P" in timeline:
            print("  ERROR: no dispatch exists, so no dispatch letter should appear, got {!r}".format(timeline))
            failed = True

        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []

        # ------------------------------------------------------------------
        # Cheap-rate background, now-marker placement and 30-min grid alignment.
        print("Test 18: cheap slots render as '-' background, expensive stay '.'")
        my_predbat.rate_import_cost_threshold = 10.0
        # Everything expensive except one 30-min block 2h from now (block NOW_BLOCK+4).
        origin = (my_predbat.minutes_now // 30) * 30 - 4 * 60
        my_predbat.rate_import = {origin + block * 30: 30.0 for block in range(NUM_BLOCKS)}
        cheap_minute = origin + (NOW_BLOCK + 4) * 30
        my_predbat.rate_import[cheap_minute] = 5.0
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "-"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a cheap block should render '-', expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 19: a dispatch letter wins over the cheap background")
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["."] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "p"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a dispatch must overwrite the cheap background, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        my_predbat.rate_import_cost_threshold = 10.0

        print("Test 20: the now-marker sits between past and future, one per line")
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if timeline.count("|") != 1:
            print("  ERROR: expected exactly one now-marker, got {!r}".format(timeline))
            failed = True
        if timeline.index("|") != NOW_BLOCK:
            print("  ERROR: now-marker should sit at block {}, got index {} in {!r}".format(NOW_BLOCK, timeline.index("|"), timeline))
            failed = True
        if len(timeline) != NUM_BLOCKS + 1:
            print("  ERROR: the marker must not consume a block, expected width {}, got {}".format(NUM_BLOCKS + 1, len(timeline)))
            failed = True

        print("Test 21: an off-boundary cycle renders a 30-min slot in one column, not two")
        # The aliasing bug: anchoring on minutes_now split a single 30-min dispatch across two
        # columns ("pp") on any cycle not landing on :00/:30, which also read as a change and
        # got marked '*'. The grid is anchored to the boundary at or before now instead.
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(minutes=30)
        renders = {}
        for offset in (0, 5, 10, 25):
            my_predbat.minutes_now = 10 * 60 + offset
            renders[offset] = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        my_predbat.minutes_now = 10 * 60
        for offset, rendered in renders.items():
            if rendered.count("p") != 1:
                print("  ERROR: a 30-min slot must occupy one column at +{}min, got {!r}".format(offset, rendered))
                failed = True
        if len(set(renders.values())) != 1:
            print("  ERROR: renders within one 30-min block must be identical (no spurious change), got {!r}".format(renders))
            failed = True

        print("Test 22: the timeline is rendered after rates exist, not at capture time")
        # The cheap-rate background needs rate_import and rate_import_cost_threshold, which are
        # set after fetch_sensor_data_cars() runs. Capturing the render inputs and emitting via
        # log_dispatch_timelines() is what makes the '-' band appear on the first cycle after a
        # restart rather than one cycle late.
        my_predbat.dispatch_timeline_last = {}
        my_predbat.dispatch_timeline_pending = []
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            # Capture with no rates known yet, exactly as the real call order does.
            my_predbat.rate_import = {}
            my_predbat.rate_import_cost_threshold = None
            my_predbat.dispatch_timeline_pending = [{"car_n": 0, "completed": [], "started": [], "planned": [], "plugged": True, "charging_now": True}]

            # Rates arrive, then the render runs.
            origin = (my_predbat.minutes_now // 30) * 30 - 4 * 60
            my_predbat.rate_import = {origin + block * 30: 30.0 for block in range(NUM_BLOCKS)}
            my_predbat.rate_import[origin + (NOW_BLOCK + 4) * 30] = 5.0
            my_predbat.rate_import_cost_threshold = 10.0
            my_predbat.log_dispatch_timelines()
        finally:
            my_predbat.log = saved_log

        if len(logged) != 1:
            print("  ERROR: expected exactly one timeline line, got {!r}".format(logged))
            failed = True
        elif "-" not in logged[0]:
            print("  ERROR: the cheap background must render from rates set after capture, got {!r}".format(logged[0]))
            failed = True
        elif "plugged" not in logged[0] or "unplugged" in logged[0]:
            print("  ERROR: the captured plugged-in state should survive to the render, got {!r}".format(logged[0]))
            failed = True
        if my_predbat.dispatch_timeline_pending:
            print("  ERROR: pending timelines must be cleared after rendering, got {!r}".format(my_predbat.dispatch_timeline_pending))
            failed = True

        print("Test 22b: a pending entry with no charging_now key is treated as unknown, not charging")
        # Copilot review on #5077: log_dispatch_unconfirmed() must never be handed a bare True
        # default for a pending entry that never captured charging_now - that would silently
        # suppress GH#5080's reconciliation note for every real dispatch.
        started_slot = [_slot(midnight_utc + timedelta(hours=10), midnight_utc + timedelta(hours=10, minutes=30))]
        my_predbat.dispatch_timeline_last = {}
        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.dispatch_timeline_pending = [{"car_n": 0, "completed": [], "started": started_slot, "planned": [], "plugged": True}]
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.log_dispatch_timelines()
        finally:
            my_predbat.log = saved_log
        if any("not charging" in message for message in logged):
            print("  ERROR: a missing charging_now key must read as unknown (no flag), got {!r}".format(logged))
            failed = True

        my_predbat.dispatch_timeline_pending = []
        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        my_predbat.rate_import_cost_threshold = 10.0
        my_predbat.dispatch_timeline_last = {}

        print("Test 23: the provisional automatic-mode threshold does not paint the day cheap")
        # In automatic mode (rate_low_threshold 0) set_rate_thresholds() sets a provisional
        # "rate_max - 0.5" threshold, under which nearly every block reads cheap. The low-rate
        # scan then replaces it with the real band. Rendering must use the corrected value, so a
        # day of 8p overnight against a 32p peak shows a narrow band, not a full-width one.
        my_predbat.dispatch_timeline_last = {}
        origin = (my_predbat.minutes_now // 30) * 30 - 4 * 60
        # A realistic day: a 24p daytime rate, a 32.72p peak, and a 6-block 8p overnight band.
        # The daytime rate has to sit below the provisional threshold for this to prove anything -
        # that is exactly the case that painted most of the real day cheap.
        my_predbat.rate_import = {origin + block * 30: 24.0 for block in range(NUM_BLOCKS)}
        my_predbat.rate_import[origin + (NOW_BLOCK + 2) * 30] = 32.72
        for block in range(NOW_BLOCK + 10, NOW_BLOCK + 16):
            my_predbat.rate_import[origin + block * 30] = 8.0

        # The provisional value would mark everything below 32.22p as cheap.
        my_predbat.rate_import_cost_threshold = 32.72 - 0.5
        provisional = my_predbat.build_dispatch_timeline(0, [], [], [])

        # The corrected value only marks the real 8p band.
        my_predbat.rate_import_cost_threshold = 8.0
        corrected = my_predbat.build_dispatch_timeline(0, [], [], [])

        if corrected.count("-") != 6:
            print("  ERROR: expected exactly the 6 cheap blocks to render '-', got {!r}".format(corrected))
            failed = True
        if provisional.count("-") <= corrected.count("-"):
            print("  ERROR: the provisional threshold should over-mark, so this test is not proving anything: {!r} vs {!r}".format(provisional, corrected))
            failed = True

        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        my_predbat.rate_import_cost_threshold = 10.0
        my_predbat.dispatch_timeline_last = {}

        print("Test 24: a block with no rate data renders '?', not a fake expensive '.'")
        # rate_import is rebuilt each cycle and is briefly empty while apps.yaml is re-read.
        # Drawing that as '.' made a transient gap look like a genuinely expensive slot, so the
        # overnight band appeared to vanish for one cycle.
        my_predbat.rate_import = {}
        my_predbat.rate_import_cost_threshold = 10.0
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if timeline != _render(["?"] * NUM_BLOCKS, NOW_BLOCK):
            print("  ERROR: missing rate data should render '?', got {!r}".format(timeline))
            failed = True

        # A dispatch still renders over an unknown background.
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["?"] * NUM_BLOCKS
        expected[NOW_BLOCK + 4] = "p"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a dispatch must still render over unknown rates, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        my_predbat.rate_import_cost_threshold = 10.0
        my_predbat.dispatch_timeline_last = {}

        print("Test 25: a planned import with no dispatch renders 'I' when cheap, 'X' when not")
        # The case the diagnostic exists to catch: a dispatch Predbat planned around is withdrawn,
        # so the block loses its letter. Before this the committed import vanished with the slot.
        my_predbat.dispatch_timeline_last = {}
        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        for block in range(NOW_BLOCK + 10, NOW_BLOCK + 12):
            my_predbat.rate_import[_origin + block * 30] = 5.0
        my_predbat.rate_import_cost_threshold = 10.0

        # Predbat charges over one expensive block and one cheap one, with no dispatch anywhere.
        my_predbat.charge_window_best = [
            {"start": 12 * 60, "end": 12 * 60 + 30},
            {"start": 15 * 60, "end": 15 * 60 + 30},
        ]
        my_predbat.charge_limit_best = [5.0, 5.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        expected = ["."] * NUM_BLOCKS
        for block in range(NOW_BLOCK + 10, NOW_BLOCK + 12):
            expected[block] = "-"
        expected[NOW_BLOCK + 4] = "X"
        expected[NOW_BLOCK + 10] = "I"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: expected 'X' on the expensive block and 'I' on the cheap one, expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 26: a dispatch still wins over 'I'/'X' in the same block")
        slot_start = midnight_utc + timedelta(hours=12)
        slot_end = slot_start + timedelta(minutes=30)
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [_slot(slot_start, slot_end)])
        expected = ["."] * NUM_BLOCKS
        for block in range(NOW_BLOCK + 10, NOW_BLOCK + 12):
            expected[block] = "-"
        expected[NOW_BLOCK + 4] = "P"
        expected[NOW_BLOCK + 10] = "I"
        expected = _render(expected, NOW_BLOCK)
        if timeline != expected:
            print("  ERROR: a dispatch block should read 'P', not 'X', expected {!r}, got {!r}".format(expected, timeline))
            failed = True

        print("Test 27: a zero-limit window is not an import, so no 'I'/'X'")
        my_predbat.charge_limit_best = [0.0, 0.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if "X" in timeline or "I" in timeline:
            print("  ERROR: a zero-limit window must not mark an import, got {!r}".format(timeline))
            failed = True

        print("Test 28: an import where the rate is unknown stays '?'")
        my_predbat.rate_import = {}
        my_predbat.rate_import_cost_threshold = 10.0
        my_predbat.charge_limit_best = [5.0, 5.0]
        timeline = my_predbat.build_dispatch_timeline(0, [], [], [])
        if timeline != _render(["?"] * NUM_BLOCKS, NOW_BLOCK):
            print("  ERROR: an unknown rate cannot be classified cheap or expensive, got {!r}".format(timeline))
            failed = True

        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.rate_import = {_origin + block * 30: 30.0 for block in range(56)}
        my_predbat.rate_import_cost_threshold = 10.0
        my_predbat.dispatch_timeline_last = {}

        print("Test 29: a dispatch running with no car charging logs a reconciliation note")
        # GH#5080 loss mode 2: Predbat prices a live dispatch cheap and imports against it. If the
        # car never draws power, Octopus may bill that import at the full rate. This flags the
        # slot for later reconciliation against the settled bill.
        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.minutes_now = 10 * 60
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            slot = [_slot(midnight_utc + timedelta(hours=10), midnight_utc + timedelta(hours=10, minutes=30))]
            my_predbat.log_dispatch_unconfirmed(0, slot, False)
        finally:
            my_predbat.log = saved_log
        if len(logged) != 1 or "not charging" not in logged[0]:
            print("  ERROR: expected one reconciliation note, got {!r}".format(logged))
            failed = True

        print("Test 30: the note is not repeated within the same half-hour slot")
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.minutes_now = 10 * 60 + 5
            my_predbat.log_dispatch_unconfirmed(0, slot, False)
            my_predbat.minutes_now = 10 * 60 + 25
            my_predbat.log_dispatch_unconfirmed(0, slot, False)
        finally:
            my_predbat.log = saved_log
        if logged:
            print("  ERROR: a dispatch spanning a slot must log once, not every cycle, got {!r}".format(logged))
            failed = True

        print("Test 31: the next half-hour slot logs again")
        # A dispatch running continuously across the boundary - Copilot review on #5077 made
        # log_dispatch_unconfirmed() check that `started` actually covers minutes_now (below),
        # so a slot ending exactly at the old minutes_now would no longer look "active" here.
        slot_two_blocks = [_slot(midnight_utc + timedelta(hours=10), midnight_utc + timedelta(hours=11))]
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.minutes_now = 10 * 60 + 30
            my_predbat.log_dispatch_unconfirmed(0, slot_two_blocks, False)
        finally:
            my_predbat.log = saved_log
        if len(logged) != 1:
            print("  ERROR: a new slot should log again, got {!r}".format(logged))
            failed = True

        print("Test 31b: started_dispatches history that has already ended does not log (Copilot review on #5077)")
        # started_dispatches is retained by the HA integration as recent history, not trimmed to
        # the current interval - a non-empty list alone must not be read as "a dispatch is active
        # right now".
        my_predbat.dispatch_unconfirmed_last = {}
        ended_slot = [_slot(midnight_utc + timedelta(hours=9), midnight_utc + timedelta(hours=9, minutes=30))]
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.minutes_now = 10 * 60
            my_predbat.log_dispatch_unconfirmed(0, ended_slot, False)
        finally:
            my_predbat.log = saved_log
        if logged:
            print("  ERROR: a dispatch that already ended must not log, got {!r}".format(logged))
            failed = True

        print("Test 31c: the same slot on the next day logs again, not suppressed by yesterday's note (Copilot review on #5077)")
        # minutes_now resets to 0 at each local midnight, so a car flagged at (for example) 00:00
        # must not have that dedup key collide with the same car's genuinely new 00:00 slot the
        # following day.
        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.midnight_utc = midnight_utc
        my_predbat.minutes_now = 0
        midnight_dispatch_day1 = [_slot(midnight_utc, midnight_utc + timedelta(minutes=30))]
        midnight_dispatch_day2 = [_slot(midnight_utc + timedelta(days=1), midnight_utc + timedelta(days=1, minutes=30))]
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.log_dispatch_unconfirmed(0, midnight_dispatch_day1, False)  # day 1
            my_predbat.midnight_utc = midnight_utc + timedelta(days=1)  # day 2, same minutes_now
            my_predbat.log_dispatch_unconfirmed(0, midnight_dispatch_day2, False)
        finally:
            my_predbat.log = saved_log
            my_predbat.midnight_utc = midnight_utc
        if len(logged) != 2:
            print("  ERROR: the same slot on a new day should log again, got {!r}".format(logged))
            failed = True

        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.minutes_now = 10 * 60

        print("Test 32: a charging car, or no dispatch, logs nothing")
        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.minutes_now = 11 * 60
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.log_dispatch_unconfirmed(0, slot, True)  # car is charging - the normal case
            my_predbat.log_dispatch_unconfirmed(0, [], False)  # no dispatch running
            my_predbat.log_dispatch_unconfirmed(0, None, False)
        finally:
            my_predbat.log = saved_log
        if logged:
            print("  ERROR: nothing to reconcile should log nothing, got {!r}".format(logged))
            failed = True

        print("Test 32b: an unknown charging state (car_charging_now not configured) logs nothing")
        # charging_now is None when the optional car_charging_now sensor isn't set up (Copilot
        # review on #5077). Without a real signal we can neither confirm nor deny the car is
        # drawing power, so this must not be treated as "not charging".
        my_predbat.dispatch_unconfirmed_last = {}
        my_predbat.minutes_now = 11 * 60
        logged = []
        saved_log = my_predbat.log
        my_predbat.log = lambda message: logged.append(message)
        try:
            my_predbat.log_dispatch_unconfirmed(0, slot, None)
        finally:
            my_predbat.log = saved_log
        if logged:
            print("  ERROR: an unknown charging state should log nothing, got {!r}".format(logged))
            failed = True

        my_predbat.minutes_now = 10 * 60
        my_predbat.dispatch_unconfirmed_last = {}

        # ------------------------------------------------------------------
        # get_car_charging_now_tristate() - Copilot review on #5077: the pending capture must not
        # mistake "no signal" (sensor not configured, or HA reporting unknown/unavailable right
        # after a restart) for a genuine "not charging", since that would misfire GH#5080's note.
        saved_args = my_predbat.args
        saved_car_charging_now = my_predbat.car_charging_now

        print("Test 33: car_charging_now not configured at all reads as unknown")
        my_predbat.args = {}
        my_predbat.car_charging_now = [False]
        result = my_predbat.get_car_charging_now_tristate(0)
        if result is not None:
            print("  ERROR: expected None (unknown) when the sensor isn't configured, got {!r}".format(result))
            failed = True

        print("Test 34: HA reporting 'unknown' or 'unavailable' reads as unknown, not False")
        for ha_state in ("unknown", "unavailable", "Unknown", "UNAVAILABLE"):
            my_predbat.args = {"car_charging_now": ha_state}
            my_predbat.car_charging_now = [False]  # what get_car_charging_planned() collapsed it to
            result = my_predbat.get_car_charging_now_tristate(0)
            if result is not None:
                print("  ERROR: HA state {!r} should read as unknown, got {!r}".format(ha_state, result))
                failed = True

        print("Test 35: a configured sensor with a real reading passes the normalised boolean through")
        my_predbat.args = {"car_charging_now": "Charging"}
        my_predbat.car_charging_now = [True]
        if my_predbat.get_car_charging_now_tristate(0) is not True:
            print("  ERROR: expected the normalised True to pass through")
            failed = True
        my_predbat.args = {"car_charging_now": "Idle"}
        my_predbat.car_charging_now = [False]
        if my_predbat.get_car_charging_now_tristate(0) is not False:
            print("  ERROR: expected the normalised False to pass through for a genuine non-matching state")
            failed = True

        print("Test 36: an out-of-range car index reads as unknown, not False")
        my_predbat.args = {"car_charging_now": ["Charging"]}
        my_predbat.car_charging_now = [True]
        result = my_predbat.get_car_charging_now_tristate(5)
        if result is not None:
            print("  ERROR: an out-of-range index should read as unknown, got {!r}".format(result))
            failed = True

        my_predbat.args = saved_args
        my_predbat.car_charging_now = saved_car_charging_now

        # ------------------------------------------------------------------
        # dispatch_timeline_should_log() - #4948 review: a heartbeat every 30 minutes alone would
        # miss a provisional slot that appears and disappears entirely within one half-hour window.
        my_predbat.dispatch_timeline_last = {}

        print("Test 13: minute 0 (a 30-min boundary) always logs, unmarked, as a heartbeat")
        my_predbat.minutes_now = 10 * 60  # 10:00, a boundary
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "....")
        if not (should_log and marker == ""):
            print("  ERROR: expected a heartbeat to log with no marker, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 14: off-boundary with no change since the last log does not log")
        my_predbat.minutes_now = 10 * 60 + 5  # not a boundary
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "....")
        if should_log:
            print("  ERROR: an unchanged timeline off-boundary should not log, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 15: off-boundary with a change logs immediately, marked")
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "...P")
        if not (should_log and marker == " *"):
            print("  ERROR: a changed timeline off-boundary should log marked ' *', got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 16: the same changed timeline does not log again next cycle")
        my_predbat.minutes_now += 5
        should_log, marker = my_predbat.dispatch_timeline_should_log(0, "...P")
        if should_log:
            print("  ERROR: a timeline unchanged since the change-triggered log should not log again, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        print("Test 17: cars are tracked independently")
        my_predbat.minutes_now = 11 * 60 + 5  # off-boundary, car 0 already logged "...P"
        should_log, marker = my_predbat.dispatch_timeline_should_log(1, "...P")
        if not (should_log and marker == " *"):
            print("  ERROR: a second car's first-seen timeline should log independently of car 0's, got should_log={!r} marker={!r}".format(should_log, marker))
            failed = True

        my_predbat.dispatch_timeline_last = {}

    finally:
        my_predbat.dispatch_timeline_pending = saved_dispatch_timeline_pending
        my_predbat.dispatch_unconfirmed_last = saved_dispatch_unconfirmed_last
        my_predbat.rate_import = saved_rate_import
        my_predbat.rate_import_cost_threshold = saved_rate_import_cost_threshold
        my_predbat.now_utc = saved_now_utc
        my_predbat.midnight_utc = saved_midnight_utc
        my_predbat.minutes_now = saved_minutes_now
        my_predbat.dispatch_timeline_last = saved_dispatch_timeline_last
        my_predbat.charge_window_best = saved_charge_window_best
        my_predbat.charge_limit_best = saved_charge_limit_best

    if failed:
        print("\n**** dispatch_timeline tests: FAILED ****")
    else:
        print("\n**** dispatch_timeline tests: PASSED ****")

    return failed

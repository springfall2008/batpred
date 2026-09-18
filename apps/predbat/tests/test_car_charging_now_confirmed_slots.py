# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
import pytz
from const import CAR_CHARGING_NOW_STREAK_MAX_ROLLOVER_SLOTS, PREDBAT_MAX_CARS

UTC = pytz.UTC


def test_car_charging_now_confirmed_slots(my_predbat):
    """
    Test get_car_charging_planned()'s car_charging_now "trusted streak" confirmation mechanism (#4516).

    trust_future_dynamic_iog_slots's "started" level trusts a 30-min IOG settlement slot once
    car_charging_now has been seen True at some point during it (with enough of the slot left to
    rule out ambiguous trailing/residual draw - see CAR_CHARGING_NOW_CONFIRM_GUARD_MINUTES). That
    positive reading starts a streak that then rolls forward automatically through every following
    slot - without needing its own fresh True reading in each one - right up until an explicit False
    reading is seen (a genuine negative edge) or the streak's rollover cap
    (CAR_CHARGING_NOW_STREAK_MAX_ROLLOVER_SLOTS) is reached with no fresh positive read since. This
    is deliberately more forgiving than requiring a fresh read every slot: a real charger/readiness
    sensor can briefly toggle right at a settlement-period boundary (observed live as Hypervolt's
    readiness sensor flipping Charging->Ready->Charging within a couple of minutes - almost
    certainly negotiation noise, not the car actually stopping), and a multi-slot dispatch (e.g. a
    2-hour BOOST spanning four 30-min slots) should not lose confirmation on that kind of momentary
    gap. The rollover cap exists as the mechanism's defence against the opposite failure mode: if a
    genuine negative edge is ever itself missed (the same re-sampling gap that motivates the streak
    in the first place, just in the other direction), an uncapped streak would otherwise roll on
    indefinitely and quietly reproduce "planned"'s unconditional trust.

    These tests exercise the confirmation-set/streak-building logic in get_car_charging_planned()
    (fetch.py) directly, in isolation from rate_add_io_slots() (covered separately in
    test_rate_add_io_slots.py).
    """
    print("**** Running car_charging_now_confirmed_slots tests ****")
    failed = False

    # No state to save/restore here: each test gets a fresh PredBat (#5102).

    def reset_streak_state():
        """
        Clear car_charging_now_confirmed_slots/streak_last_read so each test starts clean.

        Reset in place rather than deleted: PredBat.__init__ pre-sizes both for PREDBAT_MAX_CARS and
        get_car_charging_planned() relies on that, rather than lazily creating them on first use.
        """
        my_predbat.car_charging_now_confirmed_slots = [set() for _ in range(PREDBAT_MAX_CARS)]
        my_predbat.car_charging_now_streak_last_read = [None for _ in range(PREDBAT_MAX_CARS)]

    my_predbat.num_cars = 1
    my_predbat.midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    reset_streak_state()

    # Test 1: a True reading well inside a slot (10:00, 30 minutes to go) confirms that slot.
    print("*** Test 1: car_charging_now True with plenty of the slot left confirms the slot")
    my_predbat.minutes_now = 10 * 60  # 10:00 - the very start of the 10:00-10:30 slot
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()

    if 600 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 600 (10:00-10:30) should be confirmed after a True reading at its very start")
        failed = True
    else:
        print("Test 1 passed")

    # Test 2: confirmation persists for the rest of the slot even if a later read in the same slot
    # samples False again - the read is a genuine negative edge (ends the streak going forward,
    # tested separately below) but does not retroactively un-confirm a slot already confirmed.
    print("*** Test 2: an already-confirmed slot is not retroactively un-confirmed by a later False read")
    my_predbat.minutes_now = 10 * 60 + 20  # 10:20, still within the 10:00-10:30 slot
    my_predbat.args["car_charging_now"] = "no"
    my_predbat.get_car_charging_planned()

    if 600 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 600 should stay confirmed even though this read was False")
        failed = True
    else:
        print("Test 2 passed")

    # Test 3 (rollover - the core requested behaviour): a single True reading in the FIRST slot of a
    # multi-slot dispatch propagates the streak forward through several following slots automatically,
    # without needing its own fresh True reading in each one - covering both a call landing in an
    # intermediate slot (slot 2) and a call skipping straight to a later slot (slot 4), simulating a
    # replan that didn't happen to trigger in between.
    print("*** Test 3: a streak rolls forward through consecutive slots without a fresh read in each ***")
    reset_streak_state()
    my_predbat.minutes_now = 14 * 60  # 14:00 - start of slot 840
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()  # confirms slot 840, starts the streak

    # Advance into slot 2 (14:30-15:00) - call get_car_charging_planned() again but leave
    # car_charging_now unset (defaults to "no"/False)... except that would be a genuine negative
    # edge, which is a different scenario (test 5). To exercise pure rollover (no reading at all in
    # the intervening slots), jump straight to slot 4 (15:30-16:00) without calling
    # get_car_charging_planned() again in between - the backfill on this one call must retroactively
    # confirm slots 2 and 3 too, since they're within the rollover cap of the original slot 1 read.
    my_predbat.minutes_now = 15 * 60 + 30  # 15:30 - start of slot 930 (the 4th consecutive slot: 840, 870, 900, 930)
    my_predbat.get_car_charging_planned()  # car_charging_now still "yes" - a fresh read, but this also proves backfill works

    for slot in (840, 870, 900, 930):
        if slot not in my_predbat.car_charging_now_confirmed_slots[0]:
            print("ERROR: slot {} should be confirmed as part of the rolled-forward streak".format(slot))
            failed = True
    if not failed:
        print("Test 3 passed - streak rolled forward and backfilled intermediate slots")

    # Test 4: pure backfill with NO fresh True reading in the intermediate slots at all - the streak
    # started by test 3's slot-840 reading (still active, last fresh read recorded at 930 from the
    # re-read above) must still cover an intermediate slot purely from the streak/rollover-cap logic,
    # confirmed here by checking a slot that never had its own explicit call.
    print("*** Test 4: an intermediate slot with no explicit reading of its own is still confirmed ***")
    if 870 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 870 (10:30 after the initial read, no call of its own) should have been backfilled by the streak")
        failed = True
    else:
        print("Test 4 passed")

    # Test 5: an explicit False reading ends the streak going forward - the slot it occurs in is not
    # itself confirmed (car_charging_now is False, not True, in that slot), and the streak stops
    # rolling into slots after it.
    print("*** Test 5: an explicit False reading ends the streak - later slots are not auto-confirmed ***")
    my_predbat.minutes_now = 16 * 60  # 16:00 - the next consecutive slot (960) after the streak above
    my_predbat.args["car_charging_now"] = "no"
    my_predbat.get_car_charging_planned()  # genuine negative edge

    if 960 in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 960 should NOT be confirmed - the reading in it was False")
        failed = True

    my_predbat.minutes_now = 16 * 60 + 30  # 16:30 - one slot further on, still no positive reading since the False edge
    my_predbat.get_car_charging_planned()  # car_charging_now still "no"

    if 990 in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 990 should NOT be confirmed - the streak ended at the False edge and never resumed")
        failed = True
    if not failed:
        print("Test 5 passed - negative edge correctly stopped the streak from rolling further")

    # Test 6: a fresh True reading after a negative edge starts a brand NEW streak (does not resume
    # the old one), which itself then rolls forward normally.
    print("*** Test 6: a fresh True reading after a negative edge starts a new streak ***")
    my_predbat.minutes_now = 17 * 60  # 17:00 - slot 1020, a new positive reading
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()

    if 1020 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 1020 should be confirmed - a fresh True reading after the earlier negative edge")
        failed = True

    my_predbat.minutes_now = 17 * 60 + 30  # 17:30 - slot 1050, rolled forward from the NEW streak, no reading of its own
    my_predbat.get_car_charging_planned()  # car_charging_now still "yes" from above, but exercise the rollover path too

    if 1050 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 1050 should be confirmed - rolled forward from the new streak started at slot 1020")
        failed = True
    if not failed:
        print("Test 6 passed - a new streak starts cleanly after a negative edge and rolls forward itself")

    # Test 7 (rollover cap): a streak that rolls forward more than
    # CAR_CHARGING_NOW_STREAK_MAX_ROLLOVER_SLOTS slots past its last fresh positive read, with no
    # fresh True reading of its own anywhere in between, lapses - it must NOT confirm a slot beyond
    # the cap purely from stale streak state, since a missed negative edge would otherwise let the
    # streak roll on indefinitely. Each check below reads car_charging_now as True but only in the
    # last couple of minutes of its own slot (failing the start guard) - this isolates the pure
    # rollover/cap arithmetic: such a reading cannot itself start a fresh streak (Test 10 covers
    # that) and is not a negative edge either, so any confirmation it produces can only have come
    # from the streak/cap logic rolling forward from the original slot-480 read.
    print("*** Test 7: a streak lapses once it rolls further than the cap without a fresh read ***")
    reset_streak_state()
    my_predbat.minutes_now = 8 * 60  # 08:00 - slot 480, the one and only genuine positive reading
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()
    last_confirmed_streak_read = my_predbat.car_charging_now_streak_last_read[0]

    cap_boundary_slot = last_confirmed_streak_read + CAR_CHARGING_NOW_STREAK_MAX_ROLLOVER_SLOTS * 30
    beyond_cap_slot = cap_boundary_slot + 30

    my_predbat.minutes_now = cap_boundary_slot + 29  # 1 minute left in this slot - fails the start guard, but does not clear the streak either (see elif charging_now: pass)
    my_predbat.get_car_charging_planned()

    if cap_boundary_slot not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot {} (exactly at the rollover cap) should still be confirmed by rollover".format(cap_boundary_slot))
        failed = True

    my_predbat.minutes_now = beyond_cap_slot + 29  # again, guard-failing True so it can't itself start a fresh streak
    my_predbat.get_car_charging_planned()

    if beyond_cap_slot in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot {} (one slot beyond the rollover cap, no fresh confirming read since slot 480) should NOT be confirmed - the streak should have lapsed".format(beyond_cap_slot))
        failed = True
    if not failed:
        print("Test 7 passed - streak correctly lapses beyond the rollover cap with no fresh read")

    # Test 7b: a fresh confirming read must actually stick even when the *previous* cycle's anchor
    # (still on record when this cycle starts) is already more than the rollover cap behind the new
    # slot - it must not be wiped in the same cycle by the rollover-cap check comparing the fresh
    # anchor against that now-superseded old one (Copilot review on #5110). Distinct from Test 7:
    # there the lapse and the eventual read are two separate cycles; here both the stale-old-anchor
    # comparison and the fresh confirming read happen in the SAME get_car_charging_planned() call.
    print("*** Test 7b: a fresh read whose own old anchor is already past the cap still re-anchors ***")
    reset_streak_state()
    my_predbat.minutes_now = 8 * 60  # 08:00 - slot 480, a genuine positive read to establish an anchor
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()
    old_anchor_7b = my_predbat.car_charging_now_streak_last_read[0]
    if old_anchor_7b != 480:
        print("ERROR: setup assumption broken - expected an anchor at slot 480, got {}".format(old_anchor_7b))
        failed = True

    # Jump straight to a slot comfortably beyond the rollover cap from slot 480, in one single call -
    # the old anchor (480) is now stale enough that, uncorrected, the cap check would clear it, but
    # this same cycle's own reading independently confirms and re-anchors the current slot.
    fresh_slot = old_anchor_7b + (CAR_CHARGING_NOW_STREAK_MAX_ROLLOVER_SLOTS + 5) * 30
    my_predbat.minutes_now = fresh_slot  # start of the slot - plenty of time left, passes the confirm guard
    my_predbat.get_car_charging_planned()  # car_charging_now is still "yes"

    if fresh_slot not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot {} should be confirmed by this cycle's own fresh positive read".format(fresh_slot))
        failed = True
    if my_predbat.car_charging_now_streak_last_read[0] != fresh_slot:
        print("ERROR: the fresh read should have re-anchored the streak at slot {}, got {}".format(fresh_slot, my_predbat.car_charging_now_streak_last_read[0]))
        failed = True
    if not failed:
        print("Test 7b passed - a fresh read survives even when its own old anchor was already past the cap")

    # Test 8: old slots (more than a day behind the current slot) are pruned so the set cannot grow
    # unbounded on a long-running install.
    print("*** Test 8: stale confirmed slots are pruned ***")
    reset_streak_state()
    my_predbat.minutes_now = 10 * 60
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()  # confirms slot 600 on day 1

    # Jump forward 2 days and confirm a new slot (a fresh positive read, a new streak) - the old one
    # should be pruned away.
    my_predbat.minutes_now = 10 * 60 + 2 * 24 * 60
    my_predbat.get_car_charging_planned()

    if 600 in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 600 should have been pruned after jumping 2 days forward")
        failed = True
    if (10 * 60 + 2 * 24 * 60) not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: the new slot 2 days later should be confirmed")
        failed = True
    if not failed:
        print("Test 8 passed - stale slots pruned, new slot confirmed")

    # Test 9: multi-car - each car's confirmed slots AND streak state are tracked independently, so
    # one car's streak/rollover never leaks into another's.
    print("*** Test 9: multi-car streak state is independent per car ***")
    reset_streak_state()
    my_predbat.num_cars = 2
    my_predbat.minutes_now = 12 * 60  # 12:00
    my_predbat.args["car_charging_now"] = ["yes", "no"]
    my_predbat.get_car_charging_planned()

    if 720 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: car 0 (charging) should have slot 720 confirmed")
        failed = True
    if 720 in my_predbat.car_charging_now_confirmed_slots[1]:
        print("ERROR: car 1 (not charging) should not have slot 720 confirmed")
        failed = True

    # Advance one slot with no reading for either car - car 0's streak should roll forward, car 1
    # (which never had a streak) should stay unconfirmed.
    my_predbat.minutes_now = 12 * 60 + 30  # 12:30, slot 750
    my_predbat.get_car_charging_planned()

    if 750 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: car 0's streak should have rolled forward to slot 750")
        failed = True
    if 750 in my_predbat.car_charging_now_confirmed_slots[1]:
        print("ERROR: car 1 should still have nothing confirmed - it never had a streak to roll forward")
        failed = True
    if not failed:
        print("Test 9 passed - per-car streak state is independent")

    # Test 10 (start guard, unchanged from before this rework): a True reading only in a slot's
    # closing minutes must not start a streak that then confirms the NEXT slot either - the start
    # guard still applies to the FIRST reading of a new streak, exactly as before.
    print("*** Test 10: a guard-failing True reading does not start a streak that rolls into the next slot ***")
    reset_streak_state()
    my_predbat.num_cars = 1
    my_predbat.minutes_now = 10 * 60 + 28  # 10:28 - only 2 minutes left in the 10:00-10:30 slot, inside the 3-minute guard
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()

    if 600 in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 600 should NOT be confirmed - the only reading was within the start guard")
        failed = True
    if my_predbat.car_charging_now_streak_last_read[0] is not None:
        print("ERROR: a guard-failing reading should not have started a streak (streak_last_read should still be None)")
        failed = True

    # The next check must not itself be a fresh, guard-passing True reading in slot 630 (which would
    # legitimately start ITS OWN new streak and confirm 630 on its own merits, masking the thing
    # being tested here). Land it within slot 630's own closing minutes too, so this call cannot
    # itself confirm anything either - isolating whether the earlier guard-failing slot-600 reading
    # incorrectly left behind any streak state that rolls forward.
    my_predbat.minutes_now = 10 * 60 + 58  # 10:58 - within slot 630 (10:30-11:00), 2 minutes left, inside the guard again
    my_predbat.get_car_charging_planned()

    if 630 in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 630 should NOT be confirmed - the earlier guard-failing reading in slot 600 must not have started a streak that rolls into slot 630")
        failed = True
    if not failed:
        print("Test 10 passed - a guard-failing reading does not start a streak at all")

    # Test 11: a True reading with exactly guard+1 minutes remaining DOES start a streak (the guard
    # is a "more than" comparison, not "at least") - unchanged boundary semantics from before.
    print("*** Test 11: a True reading with exactly guard+1 minutes remaining starts a streak ***")
    reset_streak_state()
    my_predbat.minutes_now = 10 * 60 + 26  # 10:26 - 4 minutes left, one more than the 3-minute guard
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()

    if 600 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 600 should be confirmed with 4 minutes left (more than the 3-minute guard)")
        failed = True
    else:
        print("Test 11 passed")

    # Test 12: a transient HA "unknown"/"unavailable" reading is not evidence of a stop - it must
    # fall through the same as a guard-failing True read, leaving an active streak intact, rather
    # than being treated as a negative edge (Copilot review on #5110). A real sensor genuinely goes
    # "unknown" for a few seconds around an HA restart while the car may still be charging throughout.
    print("*** Test 12: an unknown/unavailable reading does not end an active streak ***")
    reset_streak_state()
    my_predbat.minutes_now = 9 * 60  # 09:00 - slot 540, a genuine positive read starts the streak
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()
    if my_predbat.car_charging_now_streak_last_read[0] != 540:
        print("ERROR: setup assumption broken - expected an anchor at slot 540, got {}".format(my_predbat.car_charging_now_streak_last_read[0]))
        failed = True

    my_predbat.minutes_now = 9 * 60 + 30  # 09:30 - next slot (570), sensor now reporting "unknown"
    my_predbat.args["car_charging_now"] = "unknown"
    my_predbat.get_car_charging_planned()

    if my_predbat.car_charging_now_streak_last_read[0] != 540:
        print("ERROR: an unknown/unavailable reading should not have moved or cleared the streak anchor, got {}".format(my_predbat.car_charging_now_streak_last_read[0]))
        failed = True
    if 570 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 570 should still have been backfilled by the still-active streak despite the unknown reading")
        failed = True

    # A genuine negative edge afterwards must still end the streak normally - "unknown" only
    # suspends judgement, it does not disable the mechanism.
    my_predbat.minutes_now = 10 * 60  # 10:00 - slot 600
    my_predbat.args["car_charging_now"] = "no"
    my_predbat.get_car_charging_planned()
    if my_predbat.car_charging_now_streak_last_read[0] is not None:
        print("ERROR: a genuine False reading after an unknown one should still end the streak, got anchor {}".format(my_predbat.car_charging_now_streak_last_read[0]))
        failed = True
    if not failed:
        print("Test 12 passed - unknown/unavailable readings are ignored, real negative edges still end the streak")

    if not failed:
        print("**** All car_charging_now_confirmed_slots tests PASSED ****")
    else:
        print("**** Some car_charging_now_confirmed_slots tests FAILED ****")

    return failed


def test_car_charging_now_confirmed_slots_midnight_rollover(my_predbat):
    """
    A slot confirmed yesterday must not be mistaken for the same slot number today (Copilot review).

    car_charging_now_confirmed_slots/streak_last_read store minutes-since-midnight_utc - a slot
    number only means anything alongside the midnight_utc it was recorded against. midnight_utc
    itself is recomputed fresh from real time on every update_time() call, so a long-running install
    crosses real midnight while these structures still hold yesterday's numbers. Confirming slot 840
    (14:00) yesterday and then, after midnight_utc rolls over, reading minutes_now near 840 again
    today must not find that stale entry - get_car_charging_planned() has to rebase (or otherwise
    invalidate) both structures onto the new midnight_utc, not merely rely on the 24h prune, which
    does not catch this: the prune bound is itself computed from the same (now day-changed)
    minutes_now, so a small positive slot number like 840 always satisfies it regardless of which
    calendar day it was actually recorded on.
    """
    print("**** Running car_charging_now_confirmed_slots_midnight_rollover tests ****")
    failed = False

    my_predbat.num_cars = 1
    my_predbat.midnight_utc = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    my_predbat.car_charging_now_confirmed_slots = [set() for _ in range(PREDBAT_MAX_CARS)]
    my_predbat.car_charging_now_streak_last_read = [None for _ in range(PREDBAT_MAX_CARS)]

    # Day 1: confirm slot 840 (14:00) and end the streak there with an explicit False read, so
    # nothing is left active to roll forward across the rollover below - isolating this test to the
    # stale-slot-number risk rather than streak persistence, which is covered elsewhere.
    print("*** Test 1: slot 840 confirmed on day 1, streak explicitly ended ***")
    my_predbat.minutes_now = 14 * 60  # 14:00
    my_predbat.args["car_charging_now"] = "yes"
    my_predbat.get_car_charging_planned()
    my_predbat.minutes_now = 14 * 60 + 25  # still within slot 840, but ends the streak
    my_predbat.args["car_charging_now"] = "no"
    my_predbat.get_car_charging_planned()

    if 840 not in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 840 should be confirmed from the day-1 reading")
        failed = True

    # Day 2: midnight_utc rolls forward exactly one day, as update_time() does every cycle against
    # the real clock. minutes_now lands back at 840 (14:00) with NO car_charging_now reading at all
    # today - car_charging_now defaults to "no" via get_arg's own default, so if get_car_charging_planned()
    # incorrectly treated yesterday's slot 840 as still valid for today, this call's fresh negative
    # reading would need to explicitly clear it; the actual regression is that a value with no
    # reading either way (a replan landing exactly on 840 before any today reading has happened)
    # would see yesterday's leftover entry and treat it as already confirmed.
    print("*** Test 2: after midnight_utc rolls over, day-1's slot 840 must not confirm day-2's slot 840 ***")
    my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=1)
    my_predbat.minutes_now = 14 * 60  # 14:00 again, but this is a new calendar day
    del my_predbat.args["car_charging_now"]  # no reading yet today - get_arg() falls back to its "no" default
    my_predbat.get_car_charging_planned()

    if 840 in my_predbat.car_charging_now_confirmed_slots[0]:
        print("ERROR: slot 840 is confirmed on day 2 with no positive reading - yesterday's entry leaked across the midnight rollover")
        failed = True
    else:
        print("Test 2 passed - the stale day-1 entry did not survive the rollover")

    if not failed:
        print("**** All car_charging_now_confirmed_slots_midnight_rollover tests PASSED ****")
    else:
        print("**** Some car_charging_now_confirmed_slots_midnight_rollover tests FAILED ****")

    return failed

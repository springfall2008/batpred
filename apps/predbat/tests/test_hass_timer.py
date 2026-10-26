# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests for hass.py's standalone-mode scheduler (run_every/timer_tick) across clock changes.

Covers #5770: due times were compared with the naive local wall clock, so when the clocks went back
the 5-minute plan loop and the 15-second loop both stalled for up to an hour.
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from hass import Hass
from utils import next_aligned_run


def scheduled_runs(register, start, end):
    """Tick a scheduler every 30 seconds from start to end (UTC), as hass.py's main loop does, with a 5-minute task
    registered by register(hass, callback); return the moments it ran, with start and end either side."""
    hass = object.__new__(Hass)
    hass.run_list = []
    hass.log = lambda *args, **kwargs: None
    runs = []
    clock = {"now": None}

    def callback(_args):
        """Record when the task ran."""
        runs.append(clock["now"])

    register(hass, callback)
    now = start
    while now <= end:
        clock["now"] = now
        asyncio.get_event_loop().run_until_complete(hass.timer_tick(now=now))
        now += timedelta(seconds=30)
    return [start] + runs + [end]


def check_every_five_minutes(moments, label):
    """Return 1 and print the gaps when the task did not run at least every 5 minutes from start to end."""
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(moments, moments[1:])]
    if any(gap > 5 * 60 + 30 for gap in gaps):
        print("ERROR: {}: a 5-minute task should keep running every 5 minutes, ran at {} with gaps {}".format(label, [moment.strftime("%H:%M:%S") for moment in moments[1:-1]], gaps))
        return 1
    return 0


def test_hass_timer(my_predbat):
    """A 5-minute task keeps its pace when the clocks go back and forward, and its first run is worked out in aware time."""
    print("**** test_hass_timer ****")
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    failed = 0
    try:
        # Autumn: started at 01:55 BST, the first run is due at 02:00 BST, which is 01:00 UTC - not 02:00 GMT
        first = next_aligned_run(300, now=datetime(2026, 10, 25, 0, 55, 10, tzinfo=timezone.utc).astimezone())
        if first != datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc):
            print("ERROR: the run after 01:55:10 BST should be due at 01:00 UTC, not {}".format(first.astimezone(timezone.utc)))
            failed += 1

        # Autumn: started at 01:31 BST, running on through the clocks going back at 01:00 UTC
        first = next_aligned_run(300, now=datetime(2026, 10, 25, 0, 31, tzinfo=timezone.utc).astimezone())
        runs = scheduled_runs(lambda hass, callback: hass.run_every(callback, first, 300), datetime(2026, 10, 25, 0, 31, tzinfo=timezone.utc), datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc))
        failed += check_every_five_minutes(runs, "clocks going back")

        # A naive due time, as the 15-second loop and older callers pass, is read as local time; here in the
        # repeated hour, where the earlier reading only makes it due sooner
        runs = scheduled_runs(lambda hass, callback: hass.run_every(callback, datetime(2026, 10, 25, 1, 35), 300), datetime(2026, 10, 25, 0, 31, tzinfo=timezone.utc), datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc))
        failed += check_every_five_minutes(runs, "naive due time in the repeated hour")

        # Spring: the clocks go forward at 01:00 UTC, from 01:00 GMT to 02:00 BST
        runs = scheduled_runs(lambda hass, callback: hass.run_every(callback, datetime(2026, 3, 29, 0, 55), 300), datetime(2026, 3, 29, 0, 50, tzinfo=timezone.utc), datetime(2026, 3, 29, 2, 0, tzinfo=timezone.utc))
        failed += check_every_five_minutes(runs, "clocks going forward")
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Control failure monitor - detect Predbat's own commands not taking effect
# -----------------------------------------------------------------------------

"""Detect when the battery is not doing what Predbat commanded.

The control ledger (control_ledger.py) catches settings changed OUTSIDE Predbat. This catches the
other half: Predbat asked for Charging, Exporting, Freeze charging or Freeze exporting, and the
battery's measured power says it is not happening.

The expensive failure here is a false alarm, so judgement is deliberately conservative: a grace
period after entering a mode, a shortfall measured against the rate the battery can actually
deliver (the rate written, capped by the charge/discharge curve and battery temperature, and when
exporting by the AC and export headroom left after solar - so low-power, tapering, cold and
solar-limited batteries are not failures; the caller works that rate out and passes it in), no
judgement once the slot has ended, and several consecutive failing readings before anything is
reported. Each failed slot is reported, and counted, once.

This module has no engine dependencies so it can be tested standalone.
"""

from datetime import datetime


MODE_CHARGING = "Charging"
MODE_EXPORTING = "Exporting"
MODE_FREEZE_CHARGING = "Freeze charging"
MODE_FREEZE_EXPORTING = "Freeze exporting"
JUDGED_MODES = (MODE_CHARGING, MODE_EXPORTING, MODE_FREEZE_CHARGING, MODE_FREEZE_EXPORTING)

# Seconds after entering a mode in a slot before any reading is judged - the inverter needs time to
# act on the command, and cloud integrations can take minutes to report it.
GRACE_S = 300
# Consecutive failing readings after grace before a slot is declared failed.
CONFIRM_READINGS = 3
# Charging/Exporting fail when the measured power is below this fraction of the rate written.
FAIL_FRACTION = 0.25
# Below this requested rate there is too little power to judge; also the wrong-way flow a Freeze
# mode tolerates before it counts as failing.
FLOOR_W = 200
# Cap on the failed time credited between two readings, so a long gap in polling is not counted.
MAX_GAP_S = 120
# A reading closer than this to the previous one is ignored - a plan-cycle fetch and the 60s poll
# can land seconds apart and return the same sensor value.
MIN_GAP_S = 30
# The rolling window the 24h sensor reports over.
DEFAULT_WINDOW_S = 86400
# While the restore is unresolved, every this-many attempts (and the first) also searches history;
# the attempts in between read the entities' current state only. At one attempt per plan cycle this
# is roughly hourly, so a fresh install that never fails does not query 30 days of history forever.
RESTORE_HISTORY_EVERY = 12


def event_key(event):
    """Return the identity of a failure event: (inverter, mode, slot_start)."""
    return (event.get("inverter"), event.get("mode"), event.get("slot_start"))


def _is_number(value):
    """True for an int or float that is not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_valid_event(event):
    """True when a restored event has every field the monitor relies on, with sane types."""
    if not isinstance(event, dict):
        return False
    inverter_id = event.get("inverter")
    if not isinstance(inverter_id, int) or isinstance(inverter_id, bool):
        return False
    if event.get("mode") not in JUDGED_MODES:
        return False
    return all(_is_number(event.get(field)) for field in ("slot_start", "slot_end", "at", "failed_minutes"))


def judge_reading(mode, requested_w, target_percent, battery_power_w, soc_percent):
    """
    Judge one reading against the mode Predbat commanded.

    Battery power follows Predbat's convention: negative is charging, positive is discharging.

    Returns:
        True if the reading is failing, False if it is passing, None if it cannot be judged (the
        target is already reached, or too little power was requested to tell).
    """
    if mode == MODE_CHARGING:
        if requested_w is None or requested_w < FLOOR_W or target_percent is None or soc_percent >= target_percent:
            return None
        return -battery_power_w < FAIL_FRACTION * requested_w
    if mode == MODE_EXPORTING:
        if requested_w is None or requested_w < FLOOR_W or target_percent is None or soc_percent <= target_percent:
            return None
        return battery_power_w < FAIL_FRACTION * requested_w
    if mode == MODE_FREEZE_CHARGING:
        return battery_power_w > FLOOR_W
    if mode == MODE_FREEZE_EXPORTING:
        return -battery_power_w > FLOOR_W
    return None


def format_failure(event, tz=None):
    """Return the one-line Warn: log message for a newly confirmed failure."""

    def clock(timestamp):
        """Format an epoch timestamp as HH:MM in tz."""
        return datetime.fromtimestamp(timestamp, tz).strftime("%H:%M")

    measured = int(event.get("measured_w") or 0)
    if measured < 0:
        power_text = "battery charging at {}W".format(-measured)
    elif measured > 0:
        power_text = "battery discharging at {}W".format(measured)
    else:
        power_text = "battery idle"
    mode = event["mode"]
    if mode == MODE_CHARGING:
        detail = "{} vs {}W charge requested".format(power_text, int(event["requested_w"]))
    elif mode == MODE_EXPORTING:
        detail = "{} vs {}W discharge requested".format(power_text, int(event["requested_w"]))
    elif mode == MODE_FREEZE_CHARGING:
        detail = "{}, {} allows at most {}W discharge".format(power_text, mode, FLOOR_W)
    else:
        detail = "{}, {} allows at most {}W charge".format(power_text, mode, FLOOR_W)
    return "Warn: Control failure: inverter {} {} {}-{} - {}".format(event["inverter"], mode, clock(event["slot_start"]), clock(event["slot_end"]), detail)


class ControlMonitor:
    """Track, per inverter, whether the battery is following the mode Predbat commanded."""

    def __init__(self):
        """Start with nothing tracked, no events and a total of zero."""
        self.tracking = {}  # inverter id -> per-slot tracking state
        self.events = []  # oldest first; also the durable store published in the sensor attributes
        self.total = 0  # lifetime count of failed slots
        self.restored = False  # True once the total has been read back from its entity, or first written
        self.restore_attempts = 0  # restore reads made while unresolved, for the history cadence

    def _find_event(self, inverter_id, mode, slot_start):
        """Return the newest event for this inverter and mode whose slot contains slot_start, or None."""
        for event in reversed(self.events):
            if event.get("inverter") == inverter_id and event.get("mode") == mode and event["slot_start"] <= slot_start <= event["slot_end"]:
                return event
        return None

    def _attach_event(self, inverter_id, track):
        """
        Attach unattached tracking to an existing event for its slot, if there is one.

        Tracking can exist before the event that owns its slot does - after a restart execute_plan()
        sets the expectation before the publish cycle restores the events - so this is retried
        wherever such an event may have appeared. The two then share the merged slot.
        """
        if track["event"] is not None:
            return
        event = self._find_event(inverter_id, track["mode"], track["slot_start"])
        if event is None:
            return
        slot_end = max(event["slot_end"], track["slot_end"])
        track["event"] = event
        track["slot_start"] = event["slot_start"]
        track["slot_end"] = slot_end
        event["slot_end"] = slot_end

    def set_expectation(self, inverter_id, mode, slot_start, slot_end, target_percent, now):
        """
        Record what this inverter should be doing until the next plan run.

        The same mode with a start inside the current slot is the same slot - the planner splits a
        long charge into back-to-back windows - so it is extended in place and keeps its grace and
        counters. Anything else starts fresh tracking, attached to any existing event for that slot
        so that a re-entry, or a restart, cannot count it twice.

        Two ends are kept. slot_end is the span the slot has covered, only ever widened, which is what
        events are matched and de-duplicated on. deadline is the end most recently commanded, which
        is what judgement stops at: a replan can shorten the active window, and the inverter then
        rightly leaves the mode at the new end even if the next plan run is skipped.
        """
        current = self.tracking.get(inverter_id)
        if current and current["mode"] == mode and current["slot_start"] <= slot_start <= current["slot_end"]:
            current["slot_end"] = max(current["slot_end"], slot_end)
            current["deadline"] = slot_end
            current["target_percent"] = target_percent
            if current["event"] is None:
                self._attach_event(inverter_id, current)
            else:
                current["event"]["slot_end"] = max(current["event"]["slot_end"], current["slot_end"])
            return
        deadline = slot_end
        event = self._find_event(inverter_id, mode, slot_start)
        if event is not None:
            slot_start = event["slot_start"]
            slot_end = max(slot_end, event["slot_end"])
            event["slot_end"] = slot_end
        self.tracking[inverter_id] = {
            "mode": mode,
            "slot_start": slot_start,
            "slot_end": slot_end,
            "deadline": deadline,
            "target_percent": target_percent,
            "entered_at": now,
            "consecutive": 0,
            "pending_s": 0.0,
            "last_observed_at": None,
            "event": event,
        }

    def clear_expectation(self, inverter_id=None):
        """Stop judging one inverter, or every inverter when inverter_id is None. Recorded events are kept."""
        if inverter_id is None:
            self.tracking.clear()
        else:
            self.tracking.pop(inverter_id, None)

    def observe(self, inverter_id, battery_power_w, soc_percent, requested_charge_w, requested_discharge_w, now):
        """
        Judge one reading for one inverter.

        The requested rates are passed per reading rather than fixed when the expectation is set,
        because the 60s balancer can change them between plan runs. Charging is judged against the
        charge rate, Exporting against the discharge rate; the Freeze modes ignore both.

        Nothing is judged once the commanded end (the deadline) has passed: the 60s poll keeps
        observing when a plan run is skipped, and the battery has rightly left the mode by then.

        Returns:
            The new event dict when this reading confirms the slot as failed, otherwise None. A slot
            returns an event at most once.
        """
        track = self.tracking.get(inverter_id)
        if track is None or now >= track["deadline"]:
            return None
        last = track["last_observed_at"]
        if last is not None and now - last < MIN_GAP_S:
            return None
        track["last_observed_at"] = now
        grace_end = track["entered_at"] + GRACE_S
        if now < grace_end:
            return None

        mode = track["mode"]
        if mode == MODE_CHARGING:
            requested_w = requested_charge_w
        elif mode == MODE_EXPORTING:
            requested_w = requested_discharge_w
        else:
            requested_w = None
        if not judge_reading(mode, requested_w, track["target_percent"], battery_power_w, soc_percent):
            track["consecutive"] = 0
            track["pending_s"] = 0.0
            return None

        credited = 0.0
        if last is not None:
            credited = max(0.0, min(now - max(last, grace_end), MAX_GAP_S))

        event = track["event"]
        if event is not None:
            event["failed_minutes"] += credited / 60.0
            return None

        track["consecutive"] += 1
        track["pending_s"] += credited
        if track["consecutive"] < CONFIRM_READINGS:
            return None

        event = {
            "inverter": inverter_id,
            "mode": mode,
            "slot_start": track["slot_start"],
            "slot_end": track["slot_end"],
            "at": now,
            "requested_w": requested_w,
            "measured_w": battery_power_w,
            "failed_minutes": track["pending_s"] / 60.0,
        }
        self.events.append(event)
        track["event"] = event
        self.total += 1
        return event

    def recent_events(self, now, window_s=DEFAULT_WINDOW_S):
        """Return the events confirmed within the last window_s seconds, oldest first."""
        return [event for event in self.events if event["at"] >= now - window_s]

    def newest_events(self, limit):
        """Return the newest limit events, oldest first - the list published as the durable store."""
        return self.events[-limit:] if limit > 0 else []

    def prune(self, now, window_s=DEFAULT_WINDOW_S):
        """Drop events older than the window, except one still attached to a live slot."""
        live = [track.get("event") for track in self.tracking.values() if track.get("event") is not None]
        self.events = [event for event in self.events if event["at"] >= now - window_s or any(event is owned for owned in live)]

    @staticmethod
    def by_mode(events):
        """Count events per mode."""
        counts = {}
        for event in events:
            counts[event["mode"]] = counts.get(event["mode"], 0) + 1
        return counts

    def restore(self, events, total):
        """
        Merge the events and lifetime total read back from the sensors after a restart.

        Events are merged on every call, de-duplicated by event_key and keeping the larger
        failed_minutes, and any tracking not yet attached to an event is attached to a restored
        event for its slot - so a slot that failed before the restart is not counted again. The
        total is merged once, the first time it reads as a number, as restored + total: until then
        the in-memory total holds only failures counted by this process, and taking the max would
        drop them.

        Returns:
            True once the total has been restored, now or on an earlier call.
        """
        if isinstance(events, list):
            for item in events:
                if not is_valid_event(item):
                    continue
                existing = next((event for event in self.events if event_key(event) == event_key(item)), None)
                if existing is None:
                    self.events.append(dict(item))
                else:
                    existing["failed_minutes"] = max(existing["failed_minutes"], item["failed_minutes"])
            self.events.sort(key=lambda event: event["at"])
        for inverter_id, track in self.tracking.items():
            self._attach_event(inverter_id, track)
        if self.restored:
            return True
        try:
            restored_total = int(float(total))
        except (TypeError, ValueError, OverflowError):
            return False
        if restored_total < 0:
            return False
        self.total += restored_total
        self.restored = True
        return True

    def history_read_due(self):
        """
        Count one restore attempt and say whether it should also search history.

        True on the first attempt and every RESTORE_HISTORY_EVERY attempts after it; the attempts
        in between read current state only.
        """
        due = self.restore_attempts % RESTORE_HISTORY_EVERY == 0
        self.restore_attempts += 1
        return due

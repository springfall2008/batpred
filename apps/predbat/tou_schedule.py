# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Time-of-Use slot programme builder, shared by the DEYE-family components
# -----------------------------------------------------------------------------

"""Build a fixed-length Time-of-Use slot programme from Predbat's schedule.

DEYE and Sunsynk are the same inverter firmware behind two different clouds, so both
expose the same TOU model: a fixed number of slots (TOU_SLOTS, 6 on both) that
between them tile the whole 24 hours. Only the wire encoding differs - DEYE sends a
JSON list of slot objects, Sunsynk sends positional numbered fields (sellTime1..6) -
so the ENCODING stays in each component while the programme logic lives here.

The rules this builder implements, which are properties of the firmware rather than
Predbat preferences. The first two are CONFIRMED by Sunsynk's own documentation
("Avoiding conflicts in the System Mode timer"), which describes the same registers:

* A slot carries only a START time and runs until the next slot starts, so the slots
  must be distinct and chronological - "Timers MUST be set chronologically from Timer
  1 to Timer 6" - and slot 1 must start at 00:00. There is no "off" slot: whatever a
  slot says is in force for its whole interval, whatever its grid-charge flag says.
  The documented factory default runs all six timers as ranges with Grid Charge ticked
  on only two of them.
* No slot may span midnight: Timer 6 is the only one permitted to roll over, and it
  does so by running until Timer 1 restarts. A window that wraps therefore has to be
  SPLIT into a pre-midnight slot and a post-midnight one - see _split_at_midnight.
* Predbat describes at most one charge window and one export window, so the schedule
  needs at most 5 slots (the 00:00 seed, plus two boundaries per window; a wrapping
  window still costs 2 because its post-midnight half reuses the 00:00 seed). There
  is therefore always at least one slot left over to pad with.

Note an inverter can nonetheless be found sitting with all six slots at 00:00 (an
unconfigured default) and the API stores that happily. It is simply not a chronological
programme, so no conclusion about what a VALID schedule may look like follows from it.

A component using this mixin must provide:

* ``TOU_SLOTS`` - how many slots the programme holds.
* ``derive_control_state(intent, current_soc)`` - turn an intent into a control state.
* ``_slot_for(start_time, state, reserve, self_use_power)`` - encode one slot.
"""

# Minutes between consecutive padding slots. Padding slots are REAL intervals rather
# than zero-length ones: two slots sharing a start time is not a chronological
# programme, and the firmware's response to it is not consistent enough to rely on
# (an inverter can be found sitting with all six slots at 00:00, but that is an
# unconfigured default, not a schedule it was asked to run). Five minutes is below
# Predbat's own planning resolution, so a padding slot can never split a window it
# was not already inside.
TOU_PADDING_STEP = 5

# Minutes in a day. Slot times are wall-clock within a single day - the programme
# loops at midnight rather than running past it.
MINUTES_PER_DAY = 24 * 60


class TouScheduleMixin:
    """Shared Time-of-Use programme construction for the DEYE-family components."""

    @staticmethod
    def _to_slot_time(value):
        """Normalise a schedule time to the HH:MM the TOU slots require.

        The control entities carry HH:MM:SS because that is what Predbat writes (see
        INVERTER_DEF charge_time_format), so the seconds are dropped here - at the one
        point a schedule time becomes a slot time.
        """
        parts = str(value or "00:00").split(":")
        if len(parts) < 2:
            return "00:00"
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except ValueError:
            return "00:00"

    @staticmethod
    def _hm_to_minutes(hm):
        """Convert an HH:MM string to minutes since midnight (0 on bad input)."""
        try:
            parts = str(hm).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def _minutes_to_hm(minutes):
        """Convert minutes since midnight to an HH:MM slot time."""
        minutes = int(minutes) % MINUTES_PER_DAY
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def _window_active(self, window, now_minutes):
        """Return True if an enabled window covers now_minutes, handling a midnight wrap."""
        if not window.get("enable") or not window.get("start") or not window.get("end"):
            return False
        start = self._hm_to_minutes(self._to_slot_time(window["start"]))
        end = self._hm_to_minutes(self._to_slot_time(window["end"]))
        if start == end:
            return False
        if start < end:
            return start <= now_minutes < end
        return now_minutes >= start or now_minutes < end  # window wraps past midnight

    def _active_state(self, schedule, current_soc, now_minutes):
        """Derive the control state for the window active at now_minutes, else idle.

        These inverters have a single global work mode per schedule, so the top-level
        mode must follow the window active RIGHT NOW rather than a static export-first
        precedence: otherwise an export window enabled elsewhere in the day would pin
        the mode to selling-first and block the charge window's grid charging.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})
        # The rates are carried even when no window is active: with both windows shut a zero
        # charge rate is Predbat's Freeze Export, and the mode has to follow it rather than
        # sit in demand (see derive_control_state).
        intent = {"reserve": reserve, "charge": {"enable": False, "power": int(charge.get("power", 0))}, "export": {"enable": False, "power": int(export.get("power", 0))}}
        if self._window_active(export, now_minutes):
            intent["export"] = {"enable": True, "soc": export.get("soc", 0), "power": export.get("power", 0)}
        elif self._window_active(charge, now_minutes):
            intent["charge"] = {"enable": True, "soc": charge.get("soc", 0), "power": charge.get("power", 0)}
        return self.derive_control_state(intent, current_soc)

    @staticmethod
    def _split_at_midnight(start_time, end_time):
        """Split a window into (start, end) halves that never span midnight.

        A slot runs from its start until the next slot begins, and the programme loops at
        midnight rather than running through it, so a window from 22:00 to 02:00 cannot be
        one slot. It becomes two: 00:00-02:00 and 22:00-00:00. Both carry the window's
        action state, and because slot 1 at 00:00 is one of them the charge runs unbroken
        across midnight.

        A window ending exactly at midnight has no post-midnight half - it already stops
        where the programme loops.
        """
        if end_time == "00:00":
            return [(start_time, "00:00")]
        if start_time < end_time:
            return [(start_time, end_time)]
        return [("00:00", end_time), (start_time, "00:00")]

    def _padding_segments(self, ordered, needed, baseline):
        """Place `needed` extra slots where they cannot disturb the programme.

        Every one of the inverter's slots is always in force, so a slot Predbat has no use
        for still has to say something. It says the SAME THING as the slot it follows, and
        sits just after it, which makes it a real but inert interval: the state does not
        change at its boundary, so it cannot end a window early (GH#5156, where padding at
        fixed clock hours truncated any window crossing 04:00/08:00/12:00).

        They go after the LAST slot by preference, keeping them out of the way at the end
        of the day. When the last slot is too close to midnight to fit them - a window
        ending at 23:50, say - they go into the largest gap instead, which is always wide
        enough: with at most 5 slots in the programme the widest gap is at least
        1440/5 minutes, against the 25 minutes five padding slots need.
        """
        if needed <= 0:
            return []
        if not ordered:
            return [(self._minutes_to_hm(index * TOU_PADDING_STEP), dict(baseline)) for index in range(needed)]
        span = TOU_PADDING_STEP * needed
        starts = [self._hm_to_minutes(start_time) for start_time, _ in ordered]
        # Candidate anchors are every slot, each paired with the room before the next one
        # (the last slot's room running to midnight). Prefer the last, then the widest.
        room = [(starts[index + 1] if index + 1 < len(starts) else MINUTES_PER_DAY) - starts[index] for index in range(len(starts))]
        anchor = len(starts) - 1
        if room[anchor] <= span:
            anchor = room.index(max(room))
        state = ordered[anchor][1]
        return [(self._minutes_to_hm(starts[anchor] + step * TOU_PADDING_STEP), dict(state)) for step in range(1, needed + 1)]

    def build_tou_slots(self, schedule, current_soc, self_use_power):
        """Build exactly TOU_SLOTS ordered slots covering 24h from the schedule windows."""
        reserve = int(schedule.get("reserve", 0))
        # Collect (start_time, state) segment boundaries, from a DERIVED baseline at 00:00.
        # "No window is active" is not always demand - a freeze export is exactly that state
        # plus a zero charge rate (see derive_control_state) - so every slot the schedule
        # does not otherwise claim carries the baseline, and a freeze covers the whole
        # programme. Predbat never says when a freeze ends, so there is no boundary to write.
        baseline = self.derive_control_state({"reserve": reserve, "charge": {"enable": False, "power": int(schedule.get("charge", {}).get("power", 0))}, "export": {"enable": False, "power": int(schedule.get("export", {}).get("power", 0))}}, current_soc)
        segments = {"00:00": dict(baseline)}
        for direction in ("charge", "export"):
            window = schedule.get(direction, {})
            if not (window.get("enable") and window.get("start") and window.get("end")):
                continue
            # Normalised to HH:MM here: these strings become slot times, and the entities
            # they came from carry seconds.
            start_time = self._to_slot_time(window["start"])
            end_time = self._to_slot_time(window["end"])
            if start_time == end_time:
                # Mirrors the guard in _window_active: a zero-length window has no interval
                # to act over. Compared on the NORMALISED times, so "02:00:00" against
                # "02:00" is caught too. Without this, an enable event arriving before the
                # time fields (both still the "00:00:00" default) would add an action
                # segment whose matching return-to-baseline segment cannot be added at the
                # same key - an unterminated, multi-hour full-power grid-charge/export
                # slot, even though _active_state correctly reports the window inactive.
                continue
            intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
            intent[direction] = {"enable": True, "soc": window.get("soc", 0), "power": window.get("power", 0)}
            action = self.derive_control_state(intent, current_soc)
            for segment_start, segment_end in self._split_at_midnight(start_time, end_time):
                segments[segment_start] = dict(action)
                # After the window, return to the baseline. setdefault so a window ending
                # where another starts does not overwrite that window's action, and so the
                # midnight end of a wrapping window leaves its own post-midnight half alone.
                segments.setdefault(segment_end, dict(baseline))
        ordered = sorted(segments.items(), key=lambda kv: kv[0])
        ordered = ordered + self._padding_segments(ordered, self.TOU_SLOTS - len(ordered), baseline)
        ordered = sorted(ordered, key=lambda kv: kv[0])[: self.TOU_SLOTS]
        return [self._slot_for(start_time, state, reserve, self_use_power) for start_time, state in ordered]

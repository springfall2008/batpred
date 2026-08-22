# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Control ownership ledger - detect settings changed outside Predbat
# -----------------------------------------------------------------------------

"""Remember which control values Predbat has PROVED it set, so a later change can be
attributed.

The distinction that makes this safe is ownership. Predbat writing a value proves
nothing - the write may never have reached the inverter. Predbat writing a value and
reading it back proves the inverter accepted it, and only then does a later difference
mean somebody else changed it. Without that step there is no way to separate "a third
party changed this" from "our write silently failed", which is exactly the ambiguity
that makes naive settings-diffing untrustworthy.

The owned value stored here is the value READ BACK at confirmation, never the value
passed to the write. Both sides of every later comparison are then reads of the same
entity, so units, rounding and time formatting match by construction rather than by
re-implementing the write path's comparison rules.

This module has no engine dependencies so it can be tested standalone.
"""

import re
from datetime import datetime

# Verdicts returned by observe().
OWNED = "owned"  # reads back as we set it - the normal case
UNOWNED = "unowned"  # we never proved we set this, so nothing can have been taken
STALE = "stale"  # the read is not newer than our confirmation
IMPLAUSIBLE = "implausible"  # the inverter returned something that cannot be a real value
SETTLING = "settling"  # exception-path guard: a write was noted that never resolved (see observe())
EXTERNAL = "external"  # everything else ruled out - somebody else changed it
UNAVAILABLE = "unavailable"  # the entity is not reporting - a non-reading cannot contradict anything

# The rolling window the customer-facing entity reports over.
DEFAULT_WINDOW_S = 86400

# How far into the future an event's timestamp may sit and still be judged. Clocks jitter
# between the write and the publish, so a few seconds forward is ordinary; anything beyond
# this is a clock that has not synchronised or an attribute that has been corrupted.
CLOCK_JITTER_S = 60

# Controls whose value must parse as a single wall-clock time. An inverter returning "00:80"
# is a corrupt read; treating it as evidence would manufacture accusations.
TIME_CONTROLS = frozenset(
    {
        "charge_start_time",
        "charge_end_time",
        "discharge_start_time",
        "discharge_end_time",
        "idle_start_time",
        "idle_end_time",
        "pause_start_time",
        "pause_end_time",
    }
)

# Controls that hold a time RANGE rather than a single time. Inverters using
# inv_charge_time_format "H:M-H:M" (Solis, Solax) write one entity holding both ends -
# adjust_charge_window() builds "01:30-05:00", and the disable path writes "00:00-00:00".
# Validating those against a single-time regex made them permanently implausible, which
# silently blinded the whole H:M-H:M family. Each side is validated separately and a range
# is the ONLY accepted shape here, so a truncated read of half a range stays implausible
# rather than being reported as somebody shortening the window.
#
# Deliberately DISJOINT from TIME_CONTROLS: is_plausible() tests this set first and returns,
# so listing a name in both made the TIME_CONTROLS membership unreachable and implied a
# single-time shape was also accepted, which is exactly what this set exists to refuse.
TIME_RANGE_CONTROLS = frozenset(
    {
        "charge_time",
        "discharge_time",
    }
)

# Controls holding the hour or minute component of a charge/export window. adjust_charge_window()
# writes these alongside the *_time entity whenever inv_charge_time_format is "H M", so they carry
# exactly the semantics TIME_CONTROLS validates, split in two. Left unvalidated, a Modbus or GivTCP
# glitch returning 255 or -1 walked every suppression rung and became a customer-facing accusation.
HOUR_CONTROLS = frozenset(
    {
        "charge_start_hour",
        "charge_end_hour",
        "discharge_start_hour",
        "discharge_end_hour",
    }
)
MINUTE_CONTROLS = frozenset(
    {
        "charge_start_minute",
        "charge_end_minute",
        "discharge_start_minute",
        "discharge_end_minute",
    }
)

# Controls expressed as a percentage of capacity or rate.
PERCENT_CONTROLS = frozenset(
    {
        "charge_limit",
        "reserve",
        "discharge_target_soc",
        "charge_rate_percent",
        "discharge_rate_percent",
    }
)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")


def _is_wall_clock(value):
    """Return True when the value parses as a single wall-clock time."""
    match = _TIME_RE.match(str(value).strip())
    if not match:
        return False
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3)) if match.group(3) else 0
    return hour <= 23 and minute <= 59 and second <= 59


def _is_clock_component(value, maximum, owned=None):
    """Return True when the value is a valid hour or minute component for this control.

    Two shapes are legitimate for the same control NAME, decided by the entity rather than the
    name: adjust_charge_window() writes the integer component to a `number.`/`select.` hour or
    minute entity, but writes the WHOLE time string to both the hour and the minute entity when
    they are `time.` ones (GivEnergy FB00 firmware, inverter.py:3071-3080).

    Which shape is legitimate is therefore decided by the shape Predbat OWNS. An entity we
    confirmed holding 30 has no business reading "01:30" - that is a corrupt read, not a change.
    An entity we confirmed holding "23:30:00" legitimately reads wall-clock. With no ownership
    context (a direct call) both are accepted, because permissive here means fewer accusations.

    255 and -1 are refused in every case, which is the point.
    """
    if owned is not None and _is_wall_clock(owned):
        # A `time.` entity. It holds whole times, so a bare component is a truncated read.
        return _is_wall_clock(value)
    if _is_wall_clock(value):
        # No ownership context: both shapes stay acceptable, because permissive here means fewer
        # accusations. An integer entity (owned is a number) falls through to the range check.
        return owned is None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return 0 <= number <= maximum


def values_match(a, b, fuzzy=0):
    """Return True when a read is not meaningfully different from the owned value.

    Numeric comparison honours a tolerance because the write path itself does: a charge
    rate written as 3000 W and reading back 2950 W is accepted there, so reporting it
    here would contradict the engine's own definition of a successful write.
    """
    try:
        return abs(float(a) - float(b)) <= float(fuzzy)
    except (TypeError, ValueError):
        return str(a) == str(b)


def is_plausible(control, value, owned=None):
    """Return False when the value cannot be a real setting for this control.

    `owned` is the value Predbat confirmed for this entity, where there is one. Only the
    hour/minute family uses it, to tell an integer entity from a `time.` one - see
    _is_clock_component(). Defaults to True for controls with no rule: an unvalidated control
    must not be silently undetectable, only unvalidated.
    """
    if control in TIME_RANGE_CONTROLS:
        parts = str(value).split("-")
        if len(parts) != 2:
            return False
        return _is_wall_clock(parts[0]) and _is_wall_clock(parts[1])
    if control in TIME_CONTROLS:
        return _is_wall_clock(value)
    if control in HOUR_CONTROLS:
        return _is_clock_component(value, 23, owned)
    if control in MINUTE_CONTROLS:
        return _is_clock_component(value, 59, owned)
    if control in PERCENT_CONTROLS:
        try:
            return 0 <= float(value) <= 100
        except (TypeError, ValueError):
            return False
    return True


# Values Home Assistant uses for an entity that is not currently reporting. None of these
# are readings, so none of them can confirm or contradict an owned value.
NON_READINGS = frozenset({"", "none", "unavailable", "unknown", "nan", "null"})


def is_reading(value):
    """Return False when the value means "this entity is not reporting right now".

    An entity that has dropped out fails an equality check against every owned value, and
    for any control without a plausibility rule that would otherwise fall straight through
    to EXTERNAL - reporting a dropout as somebody else changing the inverter.
    """
    if value is None:
        return False
    return str(value).strip().lower() not in NON_READINGS


def _event_time(event):
    """Return an event's "at" as a float, or None when it cannot be read as one.

    THE single definition of "unusable timestamp". A restored event can carry anything
    after a round trip through Home Assistant - restore() deliberately does not filter a bad
    "at", so a non-dict or a nonsense string reaches here - and both the sort key and the
    window filter have to survive that rather than taking out the whole plan run. Duplicating
    the parse with narrower exception handling is how a non-dict event previously raised
    AttributeError out of recent_events() while sorting perfectly happily.
    """
    try:
        return float(event.get("at", 0))
    except (TypeError, ValueError, AttributeError):
        return None


def _event_sort_key(event):
    """Order events by time, sorting an unusable "at" to the front.

    Harmless because recent_events() drops those from every window anyway - they just need a
    deterministic place to sit so sorted() has a total order to work with.
    """
    at = _event_time(event)
    return 0.0 if at is None else at


def generation_from_state(raw):
    """Extract an observation generation (a float timestamp) from a raw HA state record.

    "last_changed" is read first because that is the key the engine actually produces:
    ha.py's update_state_item() is the only writer of the state table a raw read returns,
    and it stores the timestamp as "last_changed". Reading only "last_updated" made this
    return None on every production call, so the freshness gate below never fired and the
    cycle counter was the sole defence against a vendor cloud serving a cached read of the
    pre-write value. "last_changed" is also the better semantic - Home Assistant moves it
    exactly when the value changes, which is the event this ordering is about.

    "last_updated" is still honoured as a fallback: the raw records straight off the HA
    API carry that key instead, and ha.py itself falls back to it when populating
    last_changed.

    Returns None when the record is missing, malformed, or carries no usable timing
    metadata - the ledger then falls back to its cycle counter, which is weaker but
    never wrong in the unsafe direction.
    """
    if not isinstance(raw, dict):
        return None
    stamp = raw.get("last_changed")
    if stamp is None:
        stamp = raw.get("last_updated")
    if stamp is None:
        return None
    try:
        return float(stamp)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class ControlLedger:
    """Ownership state for every control entity Predbat writes.

    Keyed on the resolved ENTITY ID, never on (inverter index, control name): a GivEnergy
    EMS assigns the same entity to every inverter index, so index-keyed state would let
    inverter 1's write be reported as tampering against inverter 0's record.

    Deliberately not persisted. After a restart nothing is owned, so nothing is reported
    until Predbat has set and confirmed a value again - which is correct, because after a
    restart we genuinely do not know who set what.
    """

    def __init__(self):
        self.records = {}
        self.events = []
        # The subset of self.events this process detected, as opposed to restored from the
        # published attribute. Held by identity so newest_events() can protect them from the
        # publish cap - see there.
        self.session_events = []
        self.cycle = 0

    def begin_cycle(self):
        """Advance the cycle counter. Called once per plan run."""
        self.cycle += 1

    def owned_value(self, entity_id):
        """Return the value Predbat has proved it set for this entity, or None if it owns none.

        Exists so a caller can log what the ledger believed WITHOUT reaching into records -
        observe() pops the record when it reports, so a caller that read it afterwards would
        always find nothing.
        """
        record = self.records.get(entity_id)
        return record["owned_value"] if record else None

    def record_write(self, entity_id, control, read_back, fuzzy=0, now=0.0, generation=None):
        """Record a VERIFIED read-back, conferring ownership.

        Only ever called with a read-back the write helper has already checked against the
        value it asked for. A write that failed, or one that returned success without ever
        polling (ignore_fail), proves nothing - those call clear(entity_id) instead, which
        says what is meant rather than passing a value this would then discard.

        A read-back that is itself a non-reading (the inverter answered "unavailable" to the
        poll) is worthless in the same way - "we confirmed it holds unavailable" cannot mean
        anything, so it drops ownership rather than being stored as an owned value.
        """
        if not is_reading(read_back):
            self.records.pop(entity_id, None)
            return
        self.records[entity_id] = {
            "control": control,
            "owned_value": read_back,
            "fuzzy": fuzzy,
            "confirmed_cycle": self.cycle,
            "confirmed_at": now,
            "confirmed_generation": generation,
            "wrote_cycle": self.cycle,
        }

    def note_write_attempt(self, entity_id):
        """Mark that Predbat wrote this control, so divergence is treated as settling."""
        record = self.records.get(entity_id)
        if record:
            record["wrote_cycle"] = self.cycle

    def clear(self, entity_id=None):
        """Drop ownership. Called when Predbat stops controlling (read-only, calibration)."""
        if entity_id is None:
            self.records.clear()
        else:
            self.records.pop(entity_id, None)

    def observe(self, entity_id, control, value, now=0.0, generation=None):
        """Classify a freshly read control value. Returns one of the verdict constants.

        The ladder is biased toward silence: every innocent explanation is eliminated
        before EXTERNAL is returned.
        """
        record = self.records.get(entity_id)
        if not record:
            return UNOWNED

        if not is_reading(value):
            return UNAVAILABLE

        if values_match(value, record["owned_value"], record["fuzzy"]):
            return OWNED

        # The read must be newer than the confirmation, by cycle and - where the entity
        # exposes it - by generation. GivEnergy serves bulk settings reads from cache, so
        # a read taken before our write propagated shows the old value and looks exactly
        # like a reversion.
        if self.cycle <= record["confirmed_cycle"]:
            return STALE
        if generation is not None and record["confirmed_generation"] is not None and generation <= record["confirmed_generation"]:
            return STALE

        if not is_plausible(control, value, record["owned_value"]):
            return IMPLAUSIBLE

        # An exception-path guard, NOT the ordinary in-flight case. Every note_write_attempt()
        # is followed, in the same helper call, by a record_write() that re-confirms (making
        # wrote_cycle == confirmed_cycle) or a clear() that pops the record entirely - so on
        # any path that returns normally this rung cannot be reached, and the cycle gate above
        # is what actually suppresses our own change propagating. It stays because a helper
        # that raises between the two leaves wrote_cycle ahead, and next cycle that state must
        # not be read as somebody else's change.
        if record["wrote_cycle"] > record["confirmed_cycle"]:
            return SETTLING

        event = {
            "at": now,
            "control": control,
            "entity_id": entity_id,
            "we_set": record["owned_value"],
            "now_reads": value,
            "confirmed_at": record["confirmed_at"],
        }
        self.events.append(event)
        self.session_events.append(event)
        # Ownership is cleared so one externally-set value counts once. Predbat rewrites
        # and re-confirms next cycle, which re-arms detection naturally.
        self.records.pop(entity_id, None)
        return EXTERNAL

    def _in_window(self, now, window_s, drop_future):
        """The one place the window rules live, oldest first.

        Two callers want almost the same filter, and the difference is the whole point:

        - recent_events() (READ) wants `drop_future=True`. Testing only `(now - at) <= window_s`
          is satisfied by any negative age, so an event stamped in the future counted for ever.
          CLOCK_JITTER_S of ordinary forward drift still counts; anything further ahead cannot be
          judged against a window that runs backwards from now.
        - prune() (WRITE) wants `drop_future=False`, because it maintains the durable store and a
          future "at" may simply be a clock that has not caught up yet.

        An "at" that cannot be read as a number is dropped for both: no clock correction makes it
        parseable.
        """
        kept = []
        for event in self.events:
            at = _event_time(event)
            if at is None:
                continue
            age = now - at
            if age > window_s:
                continue
            if drop_future and age < -CLOCK_JITTER_S:
                continue
            kept.append(event)
        return kept

    def recent_events(self, now, window_s=DEFAULT_WINDOW_S):
        """READ path: events that can be judged right now, oldest first.

        Excludes future-dated events, which prune() deliberately keeps - see both.
        """
        return self._in_window(now, window_s, drop_future=True)

    def newest_events(self, limit):
        """Return at most `limit` events to publish, oldest first, never losing one we just detected.

        The published attribute IS the durable store, so whatever this drops is gone for good.
        Sorting by time is NOT sufficient, and neither is the tail of the list: restore() already
        leaves the store sorted, so the two are the same thing. On a pod whose clock is behind,
        this run's real event is stamped with a small "at" and is therefore genuinely the OLDEST
        by timestamp - so any by-time cap discards precisely the event just detected, which is the
        one thing here that cannot be recovered from anywhere else.

        Events detected by THIS process are therefore kept ahead of restored history, whatever
        their timestamp says, and the remaining room goes to the newest history by time.
        """
        if len(self.events) <= limit:
            return sorted(self.events, key=_event_sort_key)
        detected = {id(event) for event in self.session_events}
        mine = [event for event in self.events if id(event) in detected]
        if len(mine) >= limit:
            return self.session_events[-limit:]
        history = sorted((event for event in self.events if id(event) not in detected), key=_event_sort_key)
        return sorted(history[len(history) - (limit - len(mine)) :] + mine, key=_event_sort_key)

    def sustained_controls(self, events, threshold=3):
        """Return control names hit repeatedly, from an ALREADY-WINDOWED list of events.

        A single event has too many exotic explanations to put in front of a customer.
        Repeated events on the same control mean Predbat sets it, something changes it
        back, repeatedly - which is the state actually worth reporting.

        Takes the windowed list rather than a `now`/`window_s` pair so there is exactly one
        place the window is applied - a signature accepting both silently ignored one of them.
        """
        counts = {}
        for event in events:
            # restore() does not require "control" to be present, so a restored event can carry
            # control=None or a non-name. The result is written straight into a customer-facing
            # attribute and interpolated into a log line, so an event with no usable control
            # name is dropped here rather than counted - it can only ever name nothing.
            control = event.get("control") if isinstance(event, dict) else None
            if not isinstance(control, str) or not control.strip():
                continue
            counts[control] = counts.get(control, 0) + 1
        return sorted(control for control, count in counts.items() if count >= threshold)

    def prune(self, now, window_s=DEFAULT_WINDOW_S):
        """WRITE path: drop only what has genuinely aged out, so the list cannot grow unbounded.

        Deliberately keeps future-dated events, which recent_events() excludes. This list is the
        durable store - the publisher writes it back to the entity attribute restore() reads - so
        anything dropped here is gone for good, and a pod that starts before NTP sync with a slow
        clock makes every restored event look future-dated. Pruning on that basis would delete a
        real 24 hours of history irrecoverably on the very next publish. The clock corrects; the
        events have to still be there when it does.
        """
        self.events = self._in_window(now, window_s, drop_future=False)
        # Keep the protected set in step, or it grows for the life of the process and can protect
        # events that have already aged out of the store.
        surviving = {id(event) for event in self.events}
        self.session_events = [event for event in self.session_events if id(event) in surviving]

    def restore(self, events):
        """Rehydrate the event list from the entity's own last published attributes.

        The rolling window needs timestamps, so seeding a bare count is not enough. Reading
        back what we published is the cheapest durable store available and needs no new
        table. Anything malformed is dropped rather than trusted - except a bad "at" is
        deliberately NOT filtered out here: it is recent_events()/prune() that treat it as
        unusable, so a restore-time bug can't silently masquerade as "no events".

        The attribute round-trips through Home Assistant, so it is not guaranteed to still
        be a list - it may be missing, None, or (on a corrupt read) a non-iterable value.
        Anything that is not a list is treated the same as no history, not as a crash.

        The whole list is re-sorted afterwards because restore() runs AFTER execute_plan()
        has already appended this cycle's events, so appending history to the end leaves the
        newest event FIRST. Sorting makes the list genuinely oldest-first, as recent_events()
        already documents it to be, so anything reading the store in order sees history in order.
        The publisher does not rely on that ordering - see newest_events() for why.
        """
        if not isinstance(events, list):
            return
        for event in events:
            if isinstance(event, dict) and "at" in event:
                self.events.append(event)
        self.events.sort(key=_event_sort_key)

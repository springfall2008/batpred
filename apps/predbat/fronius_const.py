# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Fronius Solar.web constants
# -----------------------------------------------------------------------------

"""Constants and pure helpers for the Fronius Solar.web component.

Kept separate from fronius.py so the wire format, the channel maps, the time conversion and the
schedule arithmetic can be tested without constructing a component.

Fronius expose two APIs that share one key pair and one PV system id:

- the Query API for telemetry - power flow, aggregated energy and device metadata;
- the Flexibility API for control - dated, duration-bounded battery commands.

Both are authenticated by the same two headers on every call. Control additionally needs a
contract with Fronius and the system owner's consent; without them every control call is
rejected with HTTP 403, which this component reports as such rather than as a fault.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

# -----------------------------------------------------------------------------
# Hosts and endpoints
# -----------------------------------------------------------------------------

FRONIUS_QUERY_URL = "https://swqapi.solarweb.com"
FRONIUS_CONTROL_URL = "https://swcapi.solarweb.com"

# Paths are formatted with the PV system id. The query host serves the first three, the control
# host the last two.
FRONIUS_ENDPOINTS = {
    "flowdata": "/pvsystems/{pv_system_id}/flowdata",
    "aggrdata": "/pvsystems/{pv_system_id}/aggrdata",
    "devices": "/pvsystems/{pv_system_id}/devices",
    "schedules": "/pvsystems/{pv_system_id}/schedules",
    "schedule": "/pvsystems/{pv_system_id}/schedules/{dispatch_id}",
}

FRONIUS_HEADER_KEY_ID = "AccessKeyId"
FRONIUS_HEADER_KEY_VALUE = "AccessKeyValue"

# Per-call total timeout in seconds. Every call is bounded so a hung request can never stall the
# component's tick past ComponentBase's run_timeout.
FRONIUS_TIMEOUT = 20

# Two backoff scopes, one per host: a 403 from the control host (no contract, no owner consent)
# must not stop telemetry, and a rate limit on one host says nothing about the other.
FRONIUS_SCOPE_QUERY = "query"
FRONIUS_SCOPE_CONTROL = "control"

# -----------------------------------------------------------------------------
# Refresh tiers (minutes)
# -----------------------------------------------------------------------------

# Query calls are billed by data point (one power-flow read is one data point; an aggregated
# energy read is one per channel per day returned), so every tier is as slow as Predbat allows.
# Power flow once per Predbat cycle is the floor for following a charge target.
FRONIUS_TTL_POWER = 5
FRONIUS_TTL_ENERGY_DEFAULT = 30
FRONIUS_TTL_STATIC = 24 * 60
# A failed tier is retried after this long rather than on the next one-minute tick, so an
# outage costs a call every couple of minutes rather than every minute.
FRONIUS_TTL_RETRY = 2

# -----------------------------------------------------------------------------
# Backoff (seconds)
# -----------------------------------------------------------------------------

# Bad keys or a missing consent do not fix themselves within a minute, and retrying them every
# tick only fills the log, so the scope is parked for half an hour.
FRONIUS_AUTH_BACKOFF_SECONDS = 30 * 60
# Used when a 429 carries no usable x-rate-limit-reset header, doubling up to the cap.
FRONIUS_RATE_LIMIT_BACKOFF_SECONDS = 60
FRONIUS_RATE_LIMIT_BACKOFF_MAX_SECONDS = 60 * 60
# Server maintenance (detailed code 1012) - the message carries an end time, but a fixed pause
# is enough at this cadence.
FRONIUS_MAINTENANCE_BACKOFF_SECONDS = 15 * 60

FRONIUS_HEADER_RATE_LIMIT = "x-rate-limit-limit"
FRONIUS_HEADER_RATE_REMAINING = "x-rate-limit-remaining"
FRONIUS_HEADER_RATE_RESET = "x-rate-limit-reset"

# -----------------------------------------------------------------------------
# Channels
# -----------------------------------------------------------------------------

# Power flow channels read from /flowdata. Fronius's documented sign rule for this endpoint is
# "positive towards the inverter, negative away from it", so:
#   PowerFeedIn     positive = importing from the grid, negative = exporting
#   PowerBattCharge positive = discharging,             negative = charging
#   PowerLoad       negative = consumption
#   PowerPV         positive = generation
# Predbat wants grid_power positive on export and battery_power positive on discharge, so only
# the grid channel is negated (see predbat_grid_power). The documentation's own example response
# does not quite balance, so this reading is marked UNVERIFIED in the component docstring.
FRONIUS_FLOW_SOC = "BattSOC"
FRONIUS_FLOW_GRID = "PowerFeedIn"
FRONIUS_FLOW_BATTERY = "PowerBattCharge"
FRONIUS_FLOW_LOAD = "PowerLoad"
FRONIUS_FLOW_PV = "PowerPV"

# Aggregated daily energy channels, all in Wh. Asked for by name because each channel costs a
# data point. The two *Grid battery channels are needed because EnergyPurchased and EnergyFeedIn
# are documented as grid-to-consumer and generator-to-grid only - grid charging and battery export
# are in neither, and Predbat's import/export counters must include both.
FRONIUS_ENERGY_PURCHASED = "EnergyPurchased"
FRONIUS_ENERGY_BATT_CHARGE_GRID = "EnergyBattChargeGrid"
FRONIUS_ENERGY_FEED_IN = "EnergyFeedIn"
FRONIUS_ENERGY_BATT_DISCHARGE_GRID = "EnergyBattDischargeGrid"
FRONIUS_ENERGY_CONSUMPTION = "EnergyConsumptionTotal"
FRONIUS_ENERGY_PRODUCTION = "EnergyProductionTotal"
FRONIUS_ENERGY_CHANNELS = (
    FRONIUS_ENERGY_PURCHASED,
    FRONIUS_ENERGY_BATT_CHARGE_GRID,
    FRONIUS_ENERGY_FEED_IN,
    FRONIUS_ENERGY_BATT_DISCHARGE_GRID,
    FRONIUS_ENERGY_CONSUMPTION,
    FRONIUS_ENERGY_PRODUCTION,
)
FRONIUS_WH_TO_KWH = 0.001

# Device metadata, one data point per request. Both types in one call.
FRONIUS_DEVICE_TYPES = "battery,inverter"

# -----------------------------------------------------------------------------
# Dispatch commands
# -----------------------------------------------------------------------------

FRONIUS_CHARGE = "ChargeBattery"
FRONIUS_DISCHARGE = "DischargeBattery"
FRONIUS_EXPORT_LIMIT = "SetGridExportLimit"
# DisconnectGrid exists in the API and is deliberately never sent: it is a galvanic disconnect
# that Fronius say shortens inverter life when used often.

FRONIUS_PARAM_MIN_W = "MinW"
FRONIUS_PARAM_MAX_W = "MaxW"
FRONIUS_PARAM_EXPORT_LIMIT_W = "ExportLimitW"

# The documentation describes dispatchParameters in two incompatible ways: the object tables say
# "array of objects with name and value", while every JSON example (request and query response
# alike) shows a plain object keyed by parameter name. The examples are what the wire actually
# carries, so the object form is sent first. If the API rejects a schedule as malformed the
# component retries ONCE with the array form, and the first accepted schedule is read back to
# confirm its parameters really stuck, because a silently ignored shape would otherwise look like
# success (see fronius.py put_schedule / verify_schedule).
FRONIUS_PARAMS_LIST = "list"
FRONIUS_PARAMS_OBJECT = "object"
FRONIUS_PARAMS_DEFAULT = FRONIUS_PARAMS_OBJECT

# Commands Predbat builds, in planner terms. IDLE is "send nothing for this period", which leaves
# the inverter in its own normal self-consumption operation.
ACTION_IDLE = "idle"
ACTION_CHARGE = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_HOLD = "hold"
ACTION_NO_CHARGE = "no_charge"
ACTION_EXPORT_LIMIT = "export_limit"

# A schedule covers at least FRONIUS_HORIZON_MINUTES ahead of the moment it is built, rounded up
# to the next FRONIUS_RENEW_MINUTES boundary. That end is also the dead-man's switch: if Predbat
# stops, the last schedule runs out at most HORIZON + RENEW minutes after it was sent and the
# inverter returns to its own normal operation. Long enough to ride out a missed cycle or two,
# short enough that a stale forced charge cannot run on for hours.
FRONIUS_HORIZON_MINUTES = 60
FRONIUS_RENEW_MINUTES = 30
# Change detection compares the sent and wanted schedules over this many minutes from now. A
# difference further ahead waits until it comes this close, which is still far more than the two
# minutes delivery can take, and it means an unchanged plan is re-sent only once the sent schedule
# has less than this much left - every 30 to 60 minutes, not every tick.
FRONIUS_LOOKAHEAD_MINUTES = FRONIUS_HORIZON_MINUTES - FRONIUS_RENEW_MINUTES
# Schedules take seconds to accept and up to two minutes to reach the inverter, so a segment
# shorter than this is not worth sending.
FRONIUS_MIN_SEGMENT_SECONDS = 60
# Minimum gap between two schedule writes, so a value flapping across a boundary cannot turn
# into a write per tick.
FRONIUS_MIN_WRITE_SECONDS = 60

FRONIUS_STORAGE_MODULE = "fronius"
FRONIUS_CACHE_STATIC = "static"
FRONIUS_CACHE_CONTROL = "control"

# -----------------------------------------------------------------------------
# Error codes
# -----------------------------------------------------------------------------

# A short gloss for the detailed codes this component can usefully react to or explain, in our
# own words. Codes not listed are still logged with the server's own message.
FRONIUS_ERROR_CODES = {
    1004: "input invalid",
    1005: "invalid date/time format",
    1008: "invalid channel name",
    1011: "API call quota exceeded",
    1012: "API under maintenance",
    1013: "key not authorised for this PV system",
    1015: "PV system not found",
    1101: "access key headers missing",
    1102: "access key not found",
    1103: "access key not active",
    1104: "access key expired",
    1105: "account blocked",
    1106: "authentication failed",
    1125: "access key malformed",
    10002: "schedule or PV system not found",
    10003: "request invalid",
    10005: "PV system not found",
    10010: "request could not be parsed",
    10101: "access key headers missing",
    10102: "access key not found",
    10103: "access key not active",
    10104: "access key expired",
    10105: "account blocked",
    10106: "not authorised for this schedule",
    10107: "access key malformed",
    10301: "values could not be processed",
    10303: "parameters do not match the dispatch type",
    10306: "schedule already finished",
    10307: "every command ends in the past",
    10308: "parameter value out of range",
    10309: "a command ends in the past",
    10310: "no active components on the PV system",
    10401: "empty or invalid command",
    10404: "PV system has no smart meter",
    10405: "battery control needs a GEN24 or newer inverter",
    10406: "PV system has no battery",
    10407: "inverter not registered or offline",
    10408: "battery not connected to the primary inverter",
    10409: "command not supported on multi-inverter systems",
    10410: "command not supported on LocalNet systems",
    10411: "percentage or wattage values not accepted",
    10412: "missing or unexpected parameters",
    10603: "device offline, schedule not assigned",
    10606: "schedule could not be forwarded",
}

# Credential or permission problems: park the scope rather than retry.
FRONIUS_AUTH_ERROR_CODES = {1013, 1101, 1102, 1103, 1104, 1105, 1106, 1125, 10101, 10102, 10103, 10104, 10105, 10106, 10107}
FRONIUS_RATE_LIMIT_CODES = {1011}
# The configured PV system id does not exist (or is not visible to this key): a config error, parked
# like a credential problem.
FRONIUS_SYSTEM_NOT_FOUND_CODES = {1015, 10005}
FRONIUS_MAINTENANCE_CODES = {1012}
# The PV system cannot be battery-controlled at all through this API. Logged once as a hardware
# limitation; the component keeps monitoring.
FRONIUS_UNSUPPORTED_CONTROL_CODES = {10405, 10406, 10408, 10409, 10410}
# Malformed-request codes that justify retrying a schedule with the other parameter shape.
FRONIUS_SHAPE_ERROR_CODES = {10003, 10010, 10301, 10303, 10401, 10411, 10412}
# A cancel that finds nothing to cancel has achieved its purpose.
FRONIUS_CANCEL_DONE_CODES = {10002, 10306}


def describe_error(code, message=""):
    """Render a detailed Fronius error as "code (gloss): server message" for the log."""
    gloss = FRONIUS_ERROR_CODES.get(code)
    text = "{}".format(code) if code is not None else "(no code)"
    if gloss:
        text += " ({})".format(gloss)
    if message:
        text += ": {}".format(message)
    return text


def parse_error_body(body):
    """Return (code, message) from a Fronius error body, or (None, "") when it carries none.

    Errors arrive as {"responseError": int, "responseMessage": str}. The code is coerced to int
    because a string code would silently miss every set membership test above.
    """
    if not isinstance(body, dict):
        return None, ""
    code = body.get("responseError")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    return code, str(body.get("responseMessage") or "")


# -----------------------------------------------------------------------------
# Value helpers
# -----------------------------------------------------------------------------


def as_float(value, default=None):
    """Coerce a value to float, returning default for None, "", NULL-ish text or garbage."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def predbat_grid_power(feed_in):
    """Convert Fronius PowerFeedIn (positive = import) to Predbat's grid_power (positive = export)."""
    value = as_float(feed_in)
    return None if value is None else -value


def predbat_battery_power(batt_charge):
    """Convert Fronius PowerBattCharge (negative while charging) to Predbat's battery_power.

    Both conventions are positive on discharge, so the value passes through unchanged. This is a
    function rather than an identity at the call site so the convention is stated, and tested,
    in exactly one place.
    """
    return as_float(batt_charge)


def predbat_load_power(load):
    """Convert Fronius PowerLoad (negative = consumption) to Predbat's positive load_power."""
    value = as_float(load)
    return None if value is None else abs(value)


def null_channels(channels):
    """Return the names of the channels a Fronius channels array reported as NULL (or unparseable).

    A NULL means Solar.web could not fetch that value this time. The previous value is then stale
    and must be dropped rather than kept, or a frozen SoC would go on driving target checks.
    """
    names = set()
    for entry in channels or []:
        if isinstance(entry, dict) and entry.get("channelName") and as_float(entry.get("value")) is None:
            names.add(entry["channelName"])
    return names


def normalise_params(params):
    """Return dispatchParameters as {name: int} whichever documented shape it arrived in."""
    result = {}
    if isinstance(params, dict):
        items = params.items()
    elif isinstance(params, list):
        items = [(entry.get("name"), entry.get("value")) for entry in params if isinstance(entry, dict)]
    else:
        items = []
    for name, value in items:
        number = as_float(value)
        if name and number is not None:
            result[str(name)] = int(number)
    return result


def channel_values(channels):
    """Turn a Fronius channels array into {channelName: float}, omitting NULL values.

    Solar.web sometimes answers with NULL for a channel it timed out fetching from the device. A
    NULL is left out rather than published as 0, because a fabricated zero SoC or zero load is
    indistinguishable from a real one.
    """
    values = {}
    for entry in channels or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("channelName")
        value = as_float(entry.get("value"))
        if name and value is not None:
            values[name] = value
    return values


def daily_energy_kwh(values):
    """Derive Predbat's four daily counters in kWh from the aggregated channel values in Wh.

    Returns {leaf: kWh} with a leaf only when its primary channel was reported - the add-on
    battery-to/from-grid channels default to zero because a system with no grid charging simply
    reports none, but a missing primary channel means the figure is unknown, not zero.
    """
    result = {}
    purchased = values.get(FRONIUS_ENERGY_PURCHASED)
    if purchased is not None:
        result["import_today"] = round((purchased + values.get(FRONIUS_ENERGY_BATT_CHARGE_GRID, 0.0)) * FRONIUS_WH_TO_KWH, 3)
    feed_in = values.get(FRONIUS_ENERGY_FEED_IN)
    if feed_in is not None:
        result["export_today"] = round((feed_in + values.get(FRONIUS_ENERGY_BATT_DISCHARGE_GRID, 0.0)) * FRONIUS_WH_TO_KWH, 3)
    consumption = values.get(FRONIUS_ENERGY_CONSUMPTION)
    if consumption is not None:
        result["load_today"] = round(consumption * FRONIUS_WH_TO_KWH, 3)
    production = values.get(FRONIUS_ENERGY_PRODUCTION)
    if production is not None:
        result["pv_today"] = round(production * FRONIUS_WH_TO_KWH, 3)
    return result


def short_system_id(pv_system_id):
    """Return the short, stable id used in entity names: the first 8 hex digits of the GUID.

    A PV system id is a 36-character GUID, which would make every entity id unreadable. One
    component drives exactly one system, so eight hex digits cannot collide within an install.
    """
    text = "".join(ch for ch in str(pv_system_id or "").lower() if ch.isalnum())
    return text[:8] or "system"


# -----------------------------------------------------------------------------
# Time helpers
# -----------------------------------------------------------------------------


def zulu(moment):
    """Format an aware datetime as the UTC Zulu string Fronius recommend for dispatchDateTime."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_zulu(text):
    """Parse a Fronius UTC timestamp into an aware datetime, or None.

    Accepts "Z" or an explicit offset and any number of fractional-second digits - the rate-limit
    reset header uses seven, which datetime.fromisoformat rejects on older Pythons.
    """
    if not text:
        return None
    value = str(text).strip()
    offset = timezone.utc
    if value.endswith("Z") or value.endswith("z"):
        value = value[:-1]
    elif len(value) > 6 and value[-6] in "+-" and value[-3] == ":":
        sign = 1 if value[-6] == "+" else -1
        try:
            offset = timezone(sign * timedelta(hours=int(value[-5:-3]), minutes=int(value[-2:])))
        except ValueError:
            return None
        value = value[:-6]
    if "." in value:
        value = value.split(".", 1)[0]
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=offset)
    except ValueError:
        return None


def hms_to_minutes(value):
    """Convert Predbat's "HH:MM[:SS]" control-entity value to minutes past midnight, or None."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return None
    if hours < 0 or minutes < 0 or minutes > 59:
        return None
    return hours * 60 + minutes


def local_wall_to_utc(day, minutes, tz, second=False):
    """Convert a local wall-clock time (a date plus minutes past its midnight) to aware UTC.

    The minutes may be negative or exceed a day; the arithmetic is done on the naive wall clock
    first so a window "01:00-04:00" stays 01:00-04:00 local on a DST change day rather than
    shifting by the offset change, which is what aware-datetime + timedelta does under pytz.

    Two DST edge cases are resolved deliberately, the same way for pytz and zoneinfo zones:
    - a time that does not exist (inside the spring-forward gap) is read with the pre-transition
      offset, so it lands just after the gap;
    - a time that occurs twice (the autumn fall-back hour) is read as its first occurrence, or as
      its second when second=True (window_to_utc picks whichever is current or upcoming).
    """
    naive = datetime(day.year, day.month, day.day) + timedelta(minutes=minutes)
    if hasattr(tz, "localize"):
        try:
            aware = tz.localize(naive, is_dst=None)
        except Exception as error:  # pytz raises AmbiguousTimeError / NonExistentTimeError
            ambiguous = type(error).__name__ == "AmbiguousTimeError"
            aware = tz.localize(naive, is_dst=(ambiguous and not second))
    else:
        first = naive.replace(tzinfo=tz, fold=0)
        later = naive.replace(tzinfo=tz, fold=1)
        # zoneinfo: a wall time is ambiguous when both folds round-trip to it; in a gap they do not.
        ambiguous = first.utcoffset() != later.utcoffset() and all(item.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive for item in (first, later))
        aware = later if (ambiguous and second) else first
    return aware.astimezone(timezone.utc)


def window_to_utc(start_hms, end_hms, now_utc, tz):
    """Map Predbat's HH:MM:SS window onto the next real (start, end) in aware UTC, or None.

    Mirrors utils.compute_window_minutes, which is how inverter.py itself reads these entities:
    a window that spans midnight is anchored around now, and a window that has already ended
    today is taken to be tomorrow's. An empty window (start == end) is None.

    In the repeated autumn hour an edge can mean two instants. The first occurrence is used unless
    the window would then already have ended while its second-occurrence reading is still current,
    so a window such as 01:00-01:30 is not skipped when the clock is in the second 01:xx.
    """
    start = hms_to_minutes(start_hms)
    end = hms_to_minutes(end_hms)
    if start is None or end is None or start == end:
        return None
    now_local = now_utc.astimezone(tz)
    now_minutes = now_local.hour * 60 + now_local.minute
    if end < start:
        if end > now_minutes:
            start -= 24 * 60
        else:
            end += 24 * 60
    if end <= now_minutes:
        start += 24 * 60
        end += 24 * 60
    day = now_local.date()
    start_utc, end_utc = local_wall_to_utc(day, start, tz), local_wall_to_utc(day, end, tz)
    if end_utc <= now_utc:
        late_end = local_wall_to_utc(day, end, tz, second=True)
        if late_end > now_utc:
            late_start = local_wall_to_utc(day, start, tz, second=True)
            start_utc = late_start if late_start < late_end else start_utc
            end_utc = late_end
    return start_utc, end_utc


def ceil_minute(moment):
    """Round an aware datetime up to the next whole minute (unchanged if already whole)."""
    floored = moment.replace(second=0, microsecond=0)
    return floored if floored == moment else floored + timedelta(minutes=1)


def horizon_end(start):
    """Return the schedule horizon: start + FRONIUS_HORIZON_MINUTES, rounded up to a renewal boundary.

    Rounding to a fixed grid (rather than "now + 60 minutes") is what keeps the schedule stable
    between cycles: the horizon only moves when the clock crosses a boundary, so an unchanged
    plan produces a byte-identical schedule for FRONIUS_RENEW_MINUTES at a time.
    """
    target = start + timedelta(minutes=FRONIUS_HORIZON_MINUTES)
    epoch = int(target.timestamp())
    step = FRONIUS_RENEW_MINUTES * 60
    rounded = ((epoch + step - 1) // step) * step
    return datetime.fromtimestamp(rounded, tz=timezone.utc)


# -----------------------------------------------------------------------------
# Segments and commands
# -----------------------------------------------------------------------------


def make_segment(start, end, action, params=None):
    """Build one schedule segment: an action with integer parameters over [start, end) in UTC."""
    return {"start": start, "end": end, "action": action, "params": dict(params or {})}


def merge_segments(segments):
    """Sort segments, drop empty ones and merge neighbours that carry the same action and parameters."""
    merged = []
    for segment in sorted(segments, key=lambda item: item["start"]):
        if segment["end"] <= segment["start"]:
            continue
        if merged and merged[-1]["end"] == segment["start"] and merged[-1]["action"] == segment["action"] and merged[-1]["params"] == segment["params"]:
            merged[-1] = make_segment(merged[-1]["start"], segment["end"], segment["action"], segment["params"])
            continue
        merged.append(make_segment(segment["start"], segment["end"], segment["action"], segment["params"]))
    return merged


def clip_segments(segments, start, end):
    """Return the non-idle parts of segments that fall inside [start, end), merged."""
    clipped = []
    for segment in segments or []:
        if segment["action"] == ACTION_IDLE:
            continue
        lo = max(segment["start"], start)
        hi = min(segment["end"], end)
        if hi > lo:
            clipped.append(make_segment(lo, hi, segment["action"], segment["params"]))
    return merge_segments(clipped)


def segments_equivalent(old, new, start, end):
    """Return True when two schedules make the inverter do the same thing throughout [start, end).

    This is the "has the schedule actually changed" test. Comparing whole schedules would see a
    difference every cycle (the current segment's start time moves with the clock); comparing what
    each one does from now on sees only real changes.
    """
    return clip_segments(old, start, end) == clip_segments(new, start, end)


def segments_have_commands(segments, start):
    """Return True when any non-idle segment is still running or due at or after start."""
    return any(segment["action"] != ACTION_IDLE and segment["end"] > start for segment in segments or [])


def segment_dispatch_type(action):
    """Return the Fronius dispatchType for a planner action, or None for idle."""
    return {
        ACTION_CHARGE: FRONIUS_CHARGE,
        ACTION_HOLD: FRONIUS_CHARGE,
        ACTION_DISCHARGE: FRONIUS_DISCHARGE,
        ACTION_NO_CHARGE: FRONIUS_DISCHARGE,
        ACTION_EXPORT_LIMIT: FRONIUS_EXPORT_LIMIT,
    }.get(action)


def render_params(params, shape=FRONIUS_PARAMS_DEFAULT):
    """Render integer parameters in one of the two documented dispatchParameters shapes."""
    ordered = sorted((params or {}).items())
    if shape == FRONIUS_PARAMS_OBJECT:
        return {name: int(value) for name, value in ordered}
    return [{"name": name, "value": int(value)} for name, value in ordered]


def segments_to_commands(segments, payload_prefix, shape=FRONIUS_PARAMS_DEFAULT):
    """Turn segments into the command array PUT /schedules takes.

    Idle segments become gaps (no command), and segments shorter than FRONIUS_MIN_SEGMENT_SECONDS
    are dropped. dispatchDuration is required and integer seconds; dispatchPayload is a
    correlation id unique to this schedule so a status query can be matched back to it.
    """
    commands = []
    for segment in segments or []:
        dispatch_type = segment_dispatch_type(segment["action"])
        if dispatch_type is None:
            continue
        duration = int((segment["end"] - segment["start"]).total_seconds())
        if duration < FRONIUS_MIN_SEGMENT_SECONDS:
            continue
        commands.append(
            {
                "dispatchType": dispatch_type,
                "dispatchDateTime": zulu(segment["start"]),
                "dispatchDuration": duration,
                "dispatchParameters": render_params(segment["params"], shape),
                "dispatchPayload": "{}-{}".format(payload_prefix, len(commands) + 1),
            }
        )
    return commands


def segments_to_json(segments):
    """Serialise segments for storage and attributes (ISO UTC strings, plain dicts)."""
    return [{"start": zulu(segment["start"]), "end": zulu(segment["end"]), "action": segment["action"], "params": dict(segment["params"])} for segment in segments or []]


def segments_from_json(data):
    """Rebuild segments from segments_to_json output, skipping anything malformed."""
    segments = []
    for entry in data or []:
        if not isinstance(entry, dict):
            continue
        start = parse_zulu(entry.get("start"))
        end = parse_zulu(entry.get("end"))
        if start is None or end is None:
            continue
        params = {}
        for name, value in (entry.get("params") or {}).items():
            number = as_float(value)
            if number is not None:
                params[str(name)] = int(number)
        segments.append(make_segment(start, end, str(entry.get("action") or ACTION_IDLE), params))
    return segments


def schedule_hash(segments):
    """Return a short, stable hash of a schedule's non-idle content, for logs and the status entity."""
    content = [segment for segment in segments_to_json(segments) if segment["action"] != ACTION_IDLE]
    digest = hashlib.sha1(json.dumps(content, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:12]

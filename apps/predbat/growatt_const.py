# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Constants and pure helpers for Growatt's public OAuth OpenAPI."""

import json
from datetime import datetime, timedelta, timezone

# The API lives under /prod-api: endpoint paths such as /oauth2/getDeviceInfo are appended to this
# whole base, so a host alone (https://opencloud.growatt.com) misses every endpoint.
GROWATT_BASE_URL = "https://opencloud.growatt.com/prod-api"

GROWATT_ENDPOINTS = {
    "devices": "/oauth2/getDeviceListAuthed",
    "device_info": "/oauth2/getDeviceInfo",
    "device_data": "/oauth2/getDeviceData",
    "dispatch": "/oauth2/deviceDispatch",
    "read_dispatch": "/oauth2/readDeviceDispatch",
}

GROWATT_RETURN_CODES = {
    0: "Successful operation",
    2: "Token is invalid",
    5: "Device offline",
    6: "Setting failed",
    7: "Wrong device type",
    12: "Device serial does not have permission",
    15: "Device did not respond",
    16: "Device request timed out",
    18: "Read device parameter failed",
    105: "Too many requests",
}

GROWATT_CODE_OK = 0
GROWATT_CODE_TOKEN_INVALID = 2
GROWATT_CODE_TOO_MANY_REQUESTS = 105
# A 105 answer says how long to wait ("retry after 3551ms"). The request is sent once more after
# that wait, capped here in seconds, without counting as a failed attempt; a second 105 is returned.
GROWATT_RATE_LIMIT_WAIT_MAX = 10

GROWATT_TIMEOUT = 30
# Transport attempts for a read. A dispatch write is sent once: a timed-out write may still have
# been applied, so it is confirmed by reading it back (as Growatt's docs advise) rather than
# re-sent blind.
GROWATT_RETRIES = 3
# Attempts at a verified write (write, then read back) and at a dispatch read, and the back-off
# between them in seconds. A live SPH answered PARAMETER_SETTING_FAILED to a table it did apply,
# so one retry is worth having; but it also went on answering READ_DEVICE_PARAM_FAIL for over
# ten minutes, and three nested levels of retries then held one write for about four minutes.
# Two of each bounds a write at about a minute and a half; the next run tries again.
GROWATT_WRITE_ATTEMPTS = 2
GROWATT_READ_ATTEMPTS = 2
GROWATT_RETRY_BACKOFF = 5
# Outcomes of a verified write. On a live SPH about half of all writes answered
# PARAMETER_SETTING_FAILED (6) or timed out yet read back as applied, and reads answered
# READ_DEVICE_PARAM_FAIL (18) intermittently, so a write whose read-back failed is "unknown",
# not failed: the next attempt reads first and writes only if the device does not hold it.
GROWATT_WRITE_CONFIRMED = "confirmed"
GROWATT_WRITE_MISMATCH = "mismatch"
GROWATT_WRITE_UNKNOWN = "unknown"
# Minutes before retrying a slot table that could not be confirmed, doubling with each
# consecutive failure up to the cap: 1, 2, 4, 8, 10, 10... A datalogger that is offline would
# otherwise get four to six requests a minute for as long as it is down. A changed table skips
# the wait, so a new plan is never held back by an old failure.
GROWATT_WRITE_BACKOFF_MAX = 10

# Public documentation is inconsistent for getDeviceData: its endpoint page says one
# request per minute per device, while Appendix E says one per ten seconds. A live SPH
# polled every minute was rate limited (105) whenever a loop ran a little early, or anything
# else on the same client polled the device, so poll at twice the stricter published limit:
# the datalogger only uploads every five minutes, so nothing fresher is lost.
GROWATT_TTL_STATIC = 8 * 60
GROWATT_TTL_LIVE = 2
# Minutes without fresh telemetry before the software SoC gates stop trusting the last
# reading. Polled every two minutes, but a live SPH's datalogger uploads a new sample only every
# five minutes (utcTime 21:06:37, then 21:11:37), so this allows two missed uploads; five
# would trip whenever an upload was a few seconds late.
GROWATT_TELEMETRY_STALE = 12
# Percentage points SoC may fall below the charge target while the charge slot holds at 0%
# power, before the full charge rate resumes
GROWATT_HOLD_HYSTERESIS = 1
# Seconds between dispatch requests (writes and reads) to one device. Growatt documents
# deviceDispatch and readDeviceDispatch as 1 request per 5 seconds per device (code 105,
# TOO_MANY_REQUEST, beyond it); 6 leaves a second of margin for clock and network jitter.
GROWATT_DISPATCH_INTERVAL = 6
# Minutes between reads of the slot table actually held by the device. The read shares the
# dispatch pacing, so it is kept infrequent; it catches a table changed in the
# Growatt app, or one a write reported as accepted but the device never applied.
GROWATT_TTL_READBACK = 10
# Minutes before a release (clearing the slot table) that could not be confirmed is tried again.
# A device whose datalogger is offline answers READ_DEVICE_PARAM_FAIL for hours, and retrying
# every run would send four to six requests a minute that cannot succeed.
GROWATT_RELEASE_RETRY = 10
# Seconds a control event from another event loop waits for its write on the component's loop
# (ComponentBase.on_component_loop).
GROWATT_LOOP_HANDOVER_TIMEOUT = 120

GROWATT_SET_TIME_SLOTS = "time_slot_charge_discharge"
GROWATT_SET_ENABLE_CONTROL = "enable_control"
# The OpenAPI export limit: a live SPH answered {"percentage": 0, "exportLimitEnabled": 1} and its
# export slots then only ever covered the house load, while the inverter's own schedule exported.
# Growatt documents percentage from -100 to 100, positive for export limiting and negative for
# "forward-flow control". Predbat writes GROWATT_EXPORT_LIMIT_OPEN before an export when the
# limit does not already read as open, and leaves it there.
GROWATT_SET_EXPORT_LIMIT = "export_limit"
GROWATT_EXPORT_LIMIT_OPEN = {"exportLimitEnabled": 1, "percentage": 100}
# Minutes before retrying an export limit change that could not be confirmed
GROWATT_EXPORT_OPEN_RETRY = 10
# Refresh clocks saved across a restart (by tier prefix), so a restart neither re-reads the export
# limit early nor cuts short a release or export-limit retry. The telemetry, static and read-back
# tiers are not: a fresh process should read those straight away.
GROWATT_PERSISTED_TIERS = ("export_limit", "export_open", "release")
# Minutes between reads of the export limit once one has been read, and while none has yet
GROWATT_TTL_EXPORT_LIMIT = 8 * 60
GROWATT_TTL_EXPORT_LIMIT_RETRY = 10
# enable_control values: 1 hands the battery schedule to the OpenAPI, 0 hands it back to the
# inverter's own settings. A live SPH 3600TL acted on a slot only with 1: a charge slot charged
# the battery from the grid at the slot's power, and stopped when the slot ended. With 0 it
# refused slot writes and reads for minutes, then read back 1 again within about ten minutes
# without anything writing it - so 0 is not a state Predbat can rely on, and a release clears
# the slot table instead of sending it.
GROWATT_CONTROL_ON = 1
GROWATT_CONTROL_OFF = 0

# Growatt documents up to 16 slots; a live SPH read its table back as 16 on one day and 20 on
# another, an unused slot always as 00:00-00:00 at 0%. Writing an empty list is refused
# (WRONG_PARAMETER_VALUE, code 14, on a live SPH), so an empty table is written as 16 of these,
# which the device accepted and which normalize_slots() compares equal to any number of them.
GROWATT_SLOT_COUNT = 16
GROWATT_UNUSED_SLOT = {"startTime": "00:00", "endTime": "00:00", "percentage": 0}

# Storage component module and file for the control state that must survive a restart
GROWATT_STORAGE_MODULE = "growatt"
GROWATT_CACHE_CONTROL = "control"

GROWATT_CAPABILITIES = {
    "support_charge_freeze": False,
    "support_discharge_freeze": False,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": True,
}


def clamp_percent(value):
    """Return an integer percentage inside Growatt's documented [-100, 100] range."""
    try:
        return max(-100, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def _parse_hhmm(value):
    """Parse HH:MM[:SS], returning (hour, minute), or None for an invalid value."""
    try:
        parts = str(value).split(":")
        hour = int(parts[0])
        minute = int(parts[1])
    except (IndexError, TypeError, ValueError):
        return None
    if hour == 24 and minute == 0:
        return 24, 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour, minute


def _localize(local_tz, naive):
    """Attach local_tz correctly for both pytz and zoneinfo time zones."""
    localize = getattr(local_tz, "localize", None)
    if localize:
        try:
            return localize(naive, is_dst=None)
        except Exception:
            return localize(naive)
    return naive.replace(tzinfo=local_tz)


def local_window_to_utc_slots(start, end, percentage, now_local):
    """Convert one local repeating window into one or two non-wrapping UTC slots.

    Growatt documents dispatch times as UTC, while Predbat's control entities use the
    installation's local time. A window crossing UTC midnight is split because the API
    does not document wrap-around semantics. A zero-length local window is disabled.
    """
    start_hm = _parse_hhmm(start)
    end_hm = _parse_hhmm(end)
    if start_hm is None or end_hm is None or start_hm == end_hm:
        return []

    local_tz = now_local.tzinfo or timezone.utc
    local_date = now_local.date()

    start_day = local_date + (timedelta(days=1) if start_hm[0] == 24 else timedelta())
    end_day = local_date + (timedelta(days=1) if end_hm[0] == 24 else timedelta())
    start_hour = 0 if start_hm[0] == 24 else start_hm[0]
    end_hour = 0 if end_hm[0] == 24 else end_hm[0]
    start_dt = _localize(local_tz, datetime.combine(start_day, datetime.min.time()).replace(hour=start_hour, minute=start_hm[1]))
    end_dt = _localize(local_tz, datetime.combine(end_day, datetime.min.time()).replace(hour=end_hour, minute=end_hm[1]))
    if end_dt <= start_dt:
        next_day = end_day + timedelta(days=1)
        end_dt = _localize(local_tz, datetime.combine(next_day, datetime.min.time()).replace(hour=end_hour, minute=end_hm[1]))

    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    percent = clamp_percent(percentage)

    if start_utc.date() == end_utc.date():
        return [{"startTime": start_utc.strftime("%H:%M"), "endTime": end_utc.strftime("%H:%M"), "percentage": percent}]

    # A normal daily Predbat window can cross at most one UTC midnight. 23:59 is
    # the documented end-of-day representation used by Growatt's own example.
    slots = [{"startTime": start_utc.strftime("%H:%M"), "endTime": "23:59", "percentage": percent}]
    # A window ending exactly at UTC midnight has no after-midnight portion; a
    # 00:00-00:00 slot has no documented meaning on the device, so omit it.
    if end_utc.strftime("%H:%M") != "00:00":
        slots.append({"startTime": "00:00", "endTime": end_utc.strftime("%H:%M"), "percentage": percent})
    return slots


def normalize_slots(value):
    """Canonicalise a read/write slot list for equality checks.

    A zero-length slot is dropped: readDeviceDispatch always answers with all 16 slots and
    shows an unused one as 00:00-00:00 at 0% (seen on a live SPH), and Predbat never writes a
    zero-length slot, so the device's empty table must compare equal to Predbat's [].
    """
    if not isinstance(value, list):
        return []
    slots = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start = str(item.get("startTime", ""))[:5]
        end = str(item.get("endTime", ""))[:5]
        if not start or not end or start == end:
            continue
        slots.append({"startTime": start, "endTime": end, "percentage": clamp_percent(item.get("percentage", 0))})
    return sorted(slots, key=lambda slot: (slot["startTime"], slot["endTime"], slot["percentage"]))


def dispatch_slot_payload(slots):
    """Return the value to send for a slot table: the slots themselves, or 16 unused slots for an empty table.

    Growatt refuses an empty list, so "no slots" is sent the way the device reports it.
    """
    table = normalize_slots(slots)
    if not table:
        return [dict(GROWATT_UNUSED_SLOT) for _ in range(GROWATT_SLOT_COUNT)]
    return table


def _is_slot_list(value):
    """Return True for a list whose every entry looks like a Growatt time slot."""
    return isinstance(value, list) and all(isinstance(item, dict) and "startTime" in item and "endTime" in item for item in value)


def parse_dispatch_slots(data, _depth=0):
    """Extract the slot table from a readDeviceDispatch answer, or None when it holds none.

    A live SPH answers with the bare list of all 16 slots as the response data, unused ones as
    00:00-00:00 at 0%. Growatt does not document the envelope, so this also accepts the list
    encoded as a JSON string, or a dict carrying it under "value" or any other key. Anything
    else is None - unknown - which callers must not mistake for an empty table.
    """
    if _depth > 3:
        return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if _is_slot_list(data):
        return normalize_slots(data)
    if isinstance(data, dict):
        keys = ["value"] + [key for key in data if key != "value"]
        for key in keys:
            if key in data:
                found = parse_dispatch_slots(data[key], _depth + 1)
                # Only "value" may report an empty table: an empty list under some other
                # key is far more likely to be an unrelated field than the slot table.
                if found is not None and (key == "value" or found):
                    return found
    return None


def parse_export_limit(data, _depth=0):
    """Read readDeviceDispatch's export_limit answer as (enabled, percentage), or None when it is not understood.

    A live SPH answered with the bare {"percentage": 0, "exportLimitEnabled": 1}. As for the slot
    table (see parse_dispatch_slots), Growatt does not document the envelope, so the answer is
    also accepted encoded as a JSON string or nested in a dict under "value" or any other key.
    """
    if _depth > 3:
        return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    if "exportLimitEnabled" not in data or "percentage" not in data:
        keys = ["value"] + [key for key in data if key != "value"]
        for key in keys:
            if key in data:
                found = parse_export_limit(data[key], _depth + 1)
                if found is not None:
                    return found
        return None
    try:
        enabled = int(data["exportLimitEnabled"]) == 1
        percent = float(data["percentage"])
    except (TypeError, ValueError):
        return None
    return enabled, percent


def export_limit_watts(enabled, percent, nominal_w):
    """Return the export limit in W an export_limit setting means, or None for no usable limit.

    Only an enabled limit of 0..100% of a known nominal power is a limit; a disabled one, a negative
    percentage (forward-flow control, whose meaning Growatt does not document) or an unknown rating
    leaves Predbat's own export_limit in place.
    """
    if not enabled or percent < 0 or percent > 100 or not nominal_w or nominal_w <= 0:
        return None
    return int(round(nominal_w * percent / 100.0))

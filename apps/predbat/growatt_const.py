# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Constants and pure helpers for Growatt's public OAuth OpenAPI."""

from datetime import datetime, timedelta, timezone


GROWATT_BASE_URL = "https://opencloud.growatt.com"

GROWATT_ENDPOINTS = {
    "devices": "/oauth2/getDeviceListAuthed",
    "device_info": "/oauth2/getDeviceInfo",
    "device_data": "/oauth2/getDeviceData",
    "dispatch": "/oauth2/deviceDispatch",
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

GROWATT_TIMEOUT = 30
GROWATT_RETRIES = 3

# Public documentation is inconsistent for getDeviceData: its endpoint page says one
# request per minute per device, while Appendix E says one per ten seconds. Use the
# stricter published limit until Growatt resolves the discrepancy.
GROWATT_TTL_STATIC = 8 * 60
GROWATT_TTL_LIVE = 1
# Minutes without fresh telemetry before the software SoC gates stop trusting the last
# reading - several missed one-minute polls, so a single slow response does not trip it.
GROWATT_TELEMETRY_STALE = 5
GROWATT_DISPATCH_INTERVAL = 5

GROWATT_SET_TIME_SLOTS = "time_slot_charge_discharge"
GROWATT_SET_ENABLE_CONTROL = "enable_control"

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
    """Canonicalise a read/write slot list for equality checks."""
    if not isinstance(value, list):
        return []
    slots = []
    for item in value:
        if not isinstance(item, dict):
            continue
        start = str(item.get("startTime", ""))[:5]
        end = str(item.get("endTime", ""))[:5]
        if not start or not end:
            continue
        slots.append({"startTime": start, "endTime": end, "percentage": clamp_percent(item.get("percentage", 0))})
    return sorted(slots, key=lambda slot: (slot["startTime"], slot["endTime"], slot["percentage"]))

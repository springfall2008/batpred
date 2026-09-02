# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Utility functions for data processing, time manipulation, and calculations.

Provides helpers for parsing Home Assistant history data into per-minute
dictionaries, time string parsing, data filtering/pruning, rounding,
and historical data extraction from incrementing energy counters.
"""

import re
import array
import os
from datetime import datetime, timedelta, timezone, time
from io import StringIO
from functools import lru_cache
from const import LOW_POWER_PV_THRESHOLD, MINUTE_WATT, PREDICT_STEP, TIME_FORMAT, TIME_FORMAT_SECONDS, TIME_FORMAT_OCTOPUS, MAX_INCREMENT, TIME_FORMAT_DAILY
import copy
import json

DAY_OF_WEEK_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# The live log and the one rotated out from under it - both read whole when serving logs.
PREDBAT_LOG_FILE = "predbat.log"
PREDBAT_LOG_FILE_PREV = "predbat.1.log"

# Key-name substrings that mark an apps.yaml value as a credential, for mask_secret_args().
# "_key" and "password" were the original pair; "secret" and "token" were added for #4768,
# which promotes apps.yaml over MCP as the config-review route and so hands it to a cloud AI -
# sigenergy_app_secret, solis_api_secret, solis_access_token, gateway_mqtt_token and
# mcp_secret were all being served in the clear.
SECRET_KEY_SUBSTRINGS = ("_key", "password", "secret", "token")

# Key suffixes that match a credential substring but hold no secret - timing metadata about a
# token rather than the token itself. An expiry time is exactly what you want to see when
# debugging "my cloud integration stopped working", so keep it readable.
SECRET_KEY_EXEMPT_SUFFIXES = ("_expires_at", "_expires", "_expiry", "_expiration", "_birth")

# What a redacted credential is replaced with. Named because find_redacted_secret_overwrite()
# has to recognise it coming back in on a write, so the writer and the redactor must agree.
SECRET_MASK = "xxx"

# Use datetime.fromisoformat in str2time rather than strptime, set False to revert to strptime
STR2TIME_USE_FROMISOFORMAT = True


class MinuteArray:
    """Dense array-backed replacement for dict[int, float] keyed by contiguous minute indices.

    Stores values in a stdlib array.array('d') to reduce memory ~29x versus a Python dict
    (8 bytes per entry instead of ~232 bytes). Exposes the same get/[] interface used by
    get_from_incrementing and get_now_from_cumulative so no callers need updating.

    Only suitable when the key range is contiguous from 0 to size-1 (i.e. after smoothing).
    """

    def __init__(self, data, size):
        """Initialise from an existing dense dict, pre-allocated to size entries."""
        self._data = array.array("d", (data.get(i, 0.0) for i in range(size)))

    def get(self, key, default=0.0):
        """Return the value at key, or default if key is out of range."""
        if 0 <= key < len(self._data):
            return self._data[key]
        return default

    def __getitem__(self, key):
        """Return the value at key (no bounds check — callers are responsible)."""
        return self._data[key]

    def __len__(self):
        """Return the number of entries in the array."""
        return len(self._data)

    def __contains__(self, key):
        """True when key is a valid in-bounds index."""
        return isinstance(key, int) and 0 <= key < len(self._data)

    def __bool__(self):
        """True when the array is non-empty."""
        return len(self._data) > 0

    def __setitem__(self, key, value):
        """Set the value at key."""
        self._data[key] = float(value)

    def __iter__(self):
        """Iterate over all valid indices, mirroring dict iteration over keys."""
        return iter(range(len(self._data)))

    def keys(self):
        """Return a range covering all valid indices, mirroring dict.keys() for dense data."""
        return range(len(self._data))

    def copy(self):
        """Return a shallow copy of this MinuteArray."""
        new = MinuteArray.__new__(MinuteArray)
        new._data = array.array("d", self._data)
        return new


# Predbat member variables never included in a debug dump or served over MCP - live object
# graphs, the HA interface, loaded secrets and the URL caches. Shared with is_debug_excluded_key().
DEBUG_EXCLUDE_LIST = [
    "ha_interface",
    "components",
    "prediction",
    "logfile",
    "predheat",
    "inverters",
    "run_list",
    "threads",
    "EVENT_LISTEN_LIST",
    "local_tz",
    "CONFIG_ITEMS",
    "config_index",
    "comparison",
    "plugin_system",
    "ge_url_cache",
    "github_url_cache",
    "octopus_url_cache",
    "secrets",
]


def is_debug_excluded_key(key):
    """
    Return True when a Predbat member variable must be kept out of a debug dump or state query.

    The "db" prefix drops the database internals and "_key" drops credentials; both predate
    is_secret_key(), which is applied on top so secrets and tokens are caught here too (#4768).
    """
    if key.startswith("__") or key.startswith("db"):
        return True
    if key in DEBUG_EXCLUDE_LIST:
        return True
    return is_secret_key(key)


_REGISTRY_SECRET_NAMES = None


def registry_secret_key_names():
    """
    Return the apps.yaml config names components.py explicitly flags with "secret": True.

    utils is imported by every component module, so components cannot be imported at module
    scope here - it is imported on first use instead. An empty or failed result is not cached,
    so a redaction that runs while components is still importing (a partially initialised
    module) resolves properly on the next call rather than silently losing these names for the
    life of the process. Standalone tools that never import components keep working on the
    substring heuristic alone.
    """
    global _REGISTRY_SECRET_NAMES
    if _REGISTRY_SECRET_NAMES is None:
        try:
            import components

            names = components.secret_config_names()
        except Exception:
            names = None
        if not names:
            return frozenset()
        _REGISTRY_SECRET_NAMES = frozenset(names)
    return _REGISTRY_SECRET_NAMES


def is_secret_key(key, registry=True):
    """
    Return True when an apps.yaml key name holds a credential and must not be served in the clear.

    An explicit "secret": True flag in the component registry wins over both the substring
    heuristic and the exempt-suffix list - the registry names a credential the key name alone
    cannot reveal, such as an account number or a login identifier.

    registry=False drops back to the key-name substrings alone, for callers asking the narrower
    question "does this grant access?" rather than "must this be redacted?". Only
    find_unmasked_secret_paths() does: an account number identifies rather than authenticates, so
    telling every user with an inline octopus_api_account to move it into secrets.yaml would be
    noise. Redaction is the strict default so a new caller fails safe rather than leaking.
    """
    key_lower = str(key).lower()
    if registry and key_lower in registry_secret_key_names():
        return True
    if key_lower.endswith(SECRET_KEY_EXEMPT_SUFFIXES):
        return False
    return any(substring in key_lower for substring in SECRET_KEY_SUBSTRINGS)


def _mask_secrets_in_place(value):
    """
    Redact credential-like keys anywhere inside an already-copied structure, in place.
    """
    if isinstance(value, dict):
        for key in value:
            if is_secret_key(key):
                value[key] = SECRET_MASK
            else:
                _mask_secrets_in_place(value[key])
    elif isinstance(value, list):
        for entry in value:
            _mask_secrets_in_place(entry)


def mask_secret_args(args):
    """
    Return a deep copy of an apps.yaml-style args dict with credential-like keys redacted.

    Recurses through nested dicts and lists rather than checking only top-level names. apps.yaml
    routinely nests credentials one level down - the shipped template documents
    forecast_solar as a list of dicts each carrying its own api_key - and 'forecast_solar'
    matches none of SECRET_KEY_SUBSTRINGS, so a top-level-only pass hands that key over intact.
    That matters because everything this redacts is on its way to a third-party model.
    """
    masked = copy.deepcopy(args)
    _mask_secrets_in_place(masked)
    return masked


def find_unmasked_secret_paths(node, path=""):
    """
    Recursively walk a ruamel round-trip-loaded apps.yaml section and yield the dotted path
    of every credential-like key (per is_secret_key()) whose value is a plain scalar rather
    than a '!secret' reference into secrets.yaml (loaded as a ruamel TaggedScalar).

    Deliberately asks is_secret_key(registry=False): this drives the "stored in plain text,
    consider !secret" advice, which is about values that grant access. The registry additionally
    flags account numbers, meter point numbers and login identifiers so they are redacted out of
    anything shared, but an inline octopus_api_account is the documented normal setup and
    warning every user about it would be noise rather than advice.

    Only usable against a document loaded with ruamel's round-trip loader - a plain
    yaml.safe_load() has already resolved '!secret' tags to their real value and lost the
    distinction this depends on.
    """
    from ruamel.yaml.comments import TaggedScalar

    if isinstance(node, dict):
        for key, value in node.items():
            key_path = "{}.{}".format(path, key) if path else str(key)
            if is_secret_key(key, registry=False):
                if value not in (None, "") and not isinstance(value, TaggedScalar):
                    yield key_path
            else:
                yield from find_unmasked_secret_paths(value, key_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from find_unmasked_secret_paths(item, "{}[{}]".format(path, index))


def _mask_secrets_in_yaml_node(node):
    """
    Redact credential values in a ruamel round-trip node, in place, leaving layout alone.

    A '!secret name' reference (a TaggedScalar) is left exactly as written: it holds no
    credential, only the name of one in secrets.yaml, and which secret a key resolves to is
    what makes a misconfigured integration diagnosable.
    """
    from ruamel.yaml.comments import TaggedScalar

    if isinstance(node, dict):
        for key in node:
            value = node[key]
            if is_secret_key(key):
                if value not in (None, "") and not isinstance(value, TaggedScalar):
                    node[key] = SECRET_MASK
            else:
                _mask_secrets_in_yaml_node(value)
    elif isinstance(node, list):
        for item in node:
            _mask_secrets_in_yaml_node(item)


def mask_secret_yaml_text(text):
    """
    Return apps.yaml text with credential values redacted, preserving comments and layout.

    mask_secret_args() redacts the parsed args Predbat is running on; this redacts the file as
    the user wrote it, so a download still reads like their own apps.yaml - comments, ordering,
    quoting and '!secret' references intact - with only the credential values replaced.

    Raises rather than returning anything on a file that will not parse: the caller asked for
    a redacted document, and serving unredacted text because the parse failed is exactly the
    leak this exists to prevent.
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = YAML_DUMP_WIDTH
    data = yaml.load(text)
    _mask_secrets_in_yaml_node(data)
    buf = StringIO()
    yaml.dump(data, buf)
    return buf.getvalue()


def read_predbat_log(logfile=PREDBAT_LOG_FILE, logfile_prev=PREDBAT_LOG_FILE_PREV):
    """
    Return the contents of predbat.log, prefixed with the rotated previous log when one exists.
    """
    # Decoded explicitly rather than with the platform default: a single non-UTF-8 byte anywhere
    # in the log - an inverter API error message carrying one, say - would otherwise raise
    # UnicodeDecodeError and take out both /api/log and the get_log MCP tool.
    logdata = ""
    if os.path.exists(logfile):
        with open(logfile, "r", encoding="utf-8", errors="replace") as f:
            logdata = f.read()
    if os.path.exists(logfile_prev):
        with open(logfile_prev, "r", encoding="utf-8", errors="replace") as f:
            logdata = f.read() + "\n" + logdata
    return logdata


def classify_log_line(line):
    """
    Return the severity bucket ("error", "warning", "info" or "log") for one predbat.log line.
    """
    line_lower = line.lower()
    if "error" in line_lower:
        return "error"
    if "warn" in line_lower:
        return "warning"
    if "info" in line_lower:
        return "info"
    return "log"


def log_line_included(line_type, filter_type):
    """
    Return True when a log line of the given severity belongs in the requested view.

    Errors appear on every view; warnings on "all" and "warnings"; info on "all" and
    "info"; everything else only on "all".
    """
    if line_type == "error":
        return True
    if line_type == "warning":
        return filter_type in ("all", "warnings")
    if line_type == "info":
        return filter_type in ("all", "info")
    return filter_type == "all"


def parse_log_timestamp(line):
    """
    Return the datetime a predbat.log line was written, or None when it carries no timestamp.

    Lines are written as "{datetime.now()}: {message}", so the stamp is the leading 26
    characters - or 19 when the microseconds happened to be zero and str() dropped them.
    """
    for length, time_format in ((26, "%Y-%m-%d %H:%M:%S.%f"), (19, "%Y-%m-%d %H:%M:%S")):
        if len(line) >= length:
            try:
                return datetime.strptime(line[0:length], time_format)
            except ValueError:
                continue
    return None


# Helper to make dict hashable for caching
def charge_curve_to_tuple(d):
    """Convert dict to tuple for use as cache key"""
    if not d:
        return ()
    return tuple(sorted(d.items()))


def get_now_from_cumulative(data, minutes_now, backwards):
    """
    Get current value from cumulative data
    """
    if backwards:
        # Work out the lowest value in a 5 minute period between minutes_now and minutes_now - 5
        lowest = 9999999999
        for minute in range(0, 5):
            lowest = min(data.get(minutes_now - minute, lowest), lowest)
        value = data.get(0, 0) - lowest
    else:
        lowest = 9999999999
        for minute in range(0, 5):
            lowest = min(data.get(minute, lowest), lowest)
        value = data.get(minutes_now, 0) - lowest
    return max(value, 0)


def prune_today(data, now_utc, midnight_utc, prune=True, group=15, prune_future=False, prune_future_days=0, prune_past_days=0, intermediate=False, offset_minutes=0):
    """
    Remove data from before today
    """
    results = {}
    last_time = None
    prev_value = None
    for key in data:
        # Convert key in format '2024-09-07T15:40:09.799567+00:00' into a datetime
        timekey = str2time(key)
        if last_time and (timekey - last_time).total_seconds() < group * 60:
            continue
        if intermediate and last_time and ((timekey - last_time).total_seconds() > group * 60):
            # Large gap, introduce intermediate data point
            seconds_gap = int((timekey - last_time).total_seconds())
            for i in range(1, seconds_gap // int(group * 60)):
                new_time = last_time + timedelta(seconds=i * group * 60) + timedelta(minutes=offset_minutes)
                results[new_time.isoformat()] = prev_value
        if not prune or (timekey > (midnight_utc - timedelta(days=prune_past_days))):
            if prune_future and (timekey > (now_utc + timedelta(days=prune_future_days))):
                continue
            new_time = timekey + timedelta(minutes=offset_minutes)
            results[new_time.isoformat()] = data[key]
            last_time = timekey
            prev_value = data[key]
    return results


def is_data_numerical(history, attribute=None):
    """
    Check if history data is numerical (supports both state and attribute checking)
    Returns True if at least 10% of values are numeric or boolean
    """
    count_nums = 0
    count_total = 0

    if history and len(history) >= 1:
        for item in history[0]:
            if attribute:
                # Check attribute value
                attr_value = item.get("attributes", {}).get(attribute, None)
                if attr_value is None:
                    continue
                value = str(attr_value)
            else:
                # Check state value
                value = item.get("state", None)
                if value is None:
                    continue
                value = str(value)

            if value.lower() in ["on", "off", "true", "false"]:
                count_nums += 1
            else:
                try:
                    float(value)
                    count_nums += 1
                except (ValueError, TypeError):
                    pass
            count_total += 1

    if count_total > 0 and (count_nums / count_total) >= 0.1:
        return True
    elif count_total == 0:
        return True
    return False


# The top-level key apps.yaml wraps its whole Predbat configuration section in. Shared between
# web.py's apps.yaml editor and the AI tool layer (agent_tools.py/chat_tools.py) so both read and
# write the same section under one name - moved here, alongside update_nested_yaml_value() below,
# for the same reason is_data_numerical() was: the tool layer must not import from web.py (#4768).
ROOT_YAML_KEY = "pred_bat"

# Line width for any dump of apps.yaml. ruamel defaults to 80, which folds a long plain scalar onto
# a following, more-indented line - so rewriting the file to change one setting silently re-wraps
# every long value in it, API keys included. That still parses back to the same string, but it
# turns a one-line edit into a diff across the whole file and leaves credentials looking mangled.
# Set high enough that nothing Predbat writes ever wraps.
YAML_DUMP_WIDTH = 4096


def parse_yaml_path(path):
    """
    Split a dot-notation apps.yaml path into its segments, with "[n]" indexes as their own entry.

    "forecast_solar[0].azimuth" becomes ["forecast_solar", "[0]", "azimuth"]. Shared by
    update_nested_yaml_value(), resolve_nested_yaml_value() and set_apps_config()'s guards so all
    three agree on what a path means - a second copy of this parsing would eventually disagree
    with the writer about which segment is the leaf, which is the segment the credential checks
    depend on.
    """
    keys = []
    for component in path.split("."):
        # Split every bracket group into its own key, so a directly nested index - "foo[0][1]" -
        # becomes "foo", "[0]", "[1]". The earlier version split on the first "[" and unpacked
        # into two, which raised ValueError on any path with more than one index rather than
        # returning anything: reachable from set_apps_config, where it surfaced as a failed tool
        # call against the user's real configuration. Matches WebInterface._split_yaml_path, which
        # arrived at the same algorithm independently for the apps.yaml editor.
        for token in re.split(r"(\[[^\[\]]*\])", component):
            if token:
                keys.append(token)
    return keys


def resolve_nested_yaml_value(data, path):
    """
    Return the value a dot-notation path points at, raising KeyError if any segment is missing.

    The read-only twin of update_nested_yaml_value(), so a caller can confirm a path exists and
    read its current value *before* taking a backup and writing - update_nested_yaml_value raises
    part-way through otherwise, after the caller has already committed to the write.
    """
    keys = parse_yaml_path(path)
    current = data
    for key in keys:
        if key.startswith("[") and key.endswith("]"):
            index = int(key[1:-1])
            if not isinstance(current, list) or index >= len(current):
                raise KeyError("Index '{}' out of range in path '{}'".format(index, path))
            current = current[index]
        else:
            try:
                contains = key in current
            except TypeError:
                contains = False
            if not contains:
                raise KeyError("Key '{}' not found in path '{}'".format(key, path))
            current = current[key]
    return current


def find_redacted_secret_overwrite(previous_value, new_value):
    """
    Return the name of a credential a write would replace with the redaction placeholder.

    get_apps_config redacts credentials to "xxx", so a model that reads a container, edits one
    field and writes the whole thing back would store the literal "xxx" over a live key - the
    read-modify-write round trip silently destroys the credential it was careful not to read.
    Returns None when nothing is at risk, so the caller can refuse and point at the nested path
    instead of the container.
    """
    if isinstance(new_value, dict) and isinstance(previous_value, dict):
        for key, item in new_value.items():
            if is_secret_key(key) and item == SECRET_MASK and previous_value.get(key) not in (None, SECRET_MASK):
                return key
            found = find_redacted_secret_overwrite(previous_value.get(key), item)
            if found:
                return found
    elif isinstance(new_value, list) and isinstance(previous_value, list):
        for index, item in enumerate(new_value):
            if index < len(previous_value):
                found = find_redacted_secret_overwrite(previous_value[index], item)
                if found:
                    return found
    return None


def update_nested_yaml_value(data, path, value):
    """
    Update a nested value in YAML data using a dot-notation path, e.g. "battery_charge_low.normal"
    or a plain top-level key such as "num_inverters" (a path with no dots).

    Shared by web.py's apps.yaml batch editor (WebInterface.html_apps_post) and the chat agent's
    set_apps_config tool (chat_tools.py) - moved here so the tool layer can reuse it without
    importing from web.py (#4768). Raises KeyError when a key in the path - including the final
    one - is not already present, which is what gives both callers their "a key must already exist
    to be changed" rule for free, rather than each having to check it separately.
    """
    keys = parse_yaml_path(path)

    current = data

    # Navigate to the parent of the target value
    for key in keys[:-1]:
        if key.startswith("[") and key.endswith("]"):
            # Handle numerical index in square brackets
            index = int(key[1:-1])
            if not isinstance(current, list) or index >= len(current):
                raise KeyError(f"Index '{index}' out of range in path '{path}'")
            current = current[index]
        elif key in current:
            current = current[key]
        else:
            raise KeyError(f"Key '{key}' not found in path '{path}'")

    # Set the final value
    key = keys[-1]
    if key.startswith("[") and key.endswith("]"):
        # Handle numerical index in square brackets
        index = int(key[1:-1])
        if not isinstance(current, list) or index >= len(current):
            raise KeyError(f"Index '{index}' out of range in path '{path}'")
        current[index] = value
    elif key in current:
        current[key] = value
    else:
        # If final key is numerical try it as an integer
        if key.isdigit():
            key = int(key)
            if key not in current:
                raise KeyError(f"Final key '{key}' not found in path '{path}'")
            else:
                current[key] = value
        else:
            raise KeyError(f"Final key '{key}' not found in path '{path}'")


def history_attribute(history, state_key="state", last_updated_key="last_updated", scale=1.0, attributes=False, daily=False, offset_days=0, first=True, pounds=False, is_numerical=True):
    """
    Get historical data for an attribute
    """
    results = {}
    last_updated_time = None
    last_day_stamp = None

    if not isinstance(history, list):
        return results

    if history and len(history) >= 1:
        history = history[0]

    if not isinstance(history, list):
        return results

    # Process history
    for item in history:
        if last_updated_key not in item:
            continue

        if attributes:
            if state_key not in item.get("attributes", {}):
                continue
            state = item["attributes"][state_key]
        else:
            # Ignore data without correct keys
            if state_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue

            state = item[state_key]

        # Get the numerical key and the timestamp and ignore if in error
        if is_numerical:
            try:
                state = float(state) * scale
                if pounds:
                    state = dp2(state / 100)
                else:
                    state = dp4(state)

            except (ValueError, TypeError):
                if isinstance(state, str):
                    if state.lower() in ["on", "true", "yes"]:
                        state = 1
                    elif state.lower() in ["off", "false", "no"]:
                        state = 0
                    else:
                        continue
                else:
                    continue

        try:
            last_updated_time = item[last_updated_key]
            last_updated_stamp = str2time(last_updated_time)
        except (ValueError, TypeError):
            continue

        day_stamp = last_updated_stamp.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        if offset_days:
            day_stamp += timedelta(days=offset_days)

        if first and daily and day_stamp == last_day_stamp:
            continue
        last_day_stamp = day_stamp

        # Add the state to the result
        if daily:
            # Convert day stamp from UTC into localtime
            results[day_stamp.strftime(TIME_FORMAT_DAILY)] = state
        else:
            results[last_updated_time] = state

    return results


def get_override_time_from_string(now_utc, time_str, plan_interval_minutes):
    """
    Convert a time string like "Sun 13:00" into a datetime object
    """
    # Parse the time string into a datetime object
    # Format is Sun 13:00
    try:
        override_time = datetime.strptime(time_str, "%a %H:%M")
        day_of_week_text = time_str.split()[0].lower()
        day_of_week = DAY_OF_WEEK_MAP.get(day_of_week_text, 0)
        has_day = True
    except ValueError:
        try:
            override_time = datetime.strptime(time_str, "%H:%M")
            day_of_week = now_utc.weekday()
            has_day = False
        except ValueError:
            return None

    # Convert day of week text to a number (0=Monday, 6=Sunday)
    day_of_week_today = now_utc.weekday()

    override_time = now_utc.replace(hour=override_time.hour, minute=override_time.minute, second=0, microsecond=0)

    # Ensure minutes are rounded down to the nearest plan_interval_minutes (e.g., 15 or 10)
    minute = (override_time.minute // plan_interval_minutes) * plan_interval_minutes
    override_time = override_time.replace(minute=minute)

    # Calculate days to add
    add_days = day_of_week - day_of_week_today

    # If the day has passed this week then use next week
    if add_days < 0:
        add_days += 7
    elif not has_day and override_time <= now_utc:
        # Check if override_time is within the current active time slot
        # A slot is active if it started within plan_interval_minutes ago
        minutes_since_override = (now_utc - override_time).total_seconds() / 60
        is_outside_current_slot = minutes_since_override >= plan_interval_minutes
        if is_outside_current_slot:
            # Not in current slot, use tomorrow
            add_days += 1
        # else: override_time is within current active slot, use today (add_days stays 0)

    override_time += timedelta(days=add_days)

    return override_time


def minute_data_state(history, days, now, state_key, last_updated_key, prev_last_updated_time=None):
    """
    Get historical data for state (e.g. predbat status)
    """
    mdata = {}
    last_state = "unknown"
    newest_state = "unknown"
    newest_age = 999999

    if not history:
        return mdata

    # Process history
    for item in history:
        # Ignore data without correct keys
        if state_key not in item:
            continue
        if last_updated_key not in item:
            continue

        # Unavailable or bad values
        if item[state_key] == "unavailable" or item[state_key] == "unknown":
            continue

        state = item[state_key]
        last_updated_time = str2time(item[last_updated_key])

        # Update prev to the first if not set
        if not prev_last_updated_time:
            prev_last_updated_time = last_updated_time
            last_state = state

        timed = now - last_updated_time
        timed_to = now - prev_last_updated_time

        minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60 * 24)
        minutes = int(timed.seconds / 60) + int(timed.days * 60 * 24)

        minute = minutes
        while minute <= minutes_to:
            mdata[minute] = last_state
            minute += 1
        mdata[minutes] = state

        # Store previous state
        prev_last_updated_time = last_updated_time
        last_state = state

        if minutes <= newest_age:
            newest_age = minutes
            newest_state = state

    state = newest_state
    for minute in range(0, 60 * 24 * days):
        rindex = 60 * 24 * days - minute - 1
        state = mdata.get(rindex, state)
        mdata[rindex] = state

    return mdata


def history_attribute_to_minute_data(now_utc, data, backwards=True):
    """
    Get historical data for an attribute with history attribute filtering first
    """
    history = []
    oldest_date = now_utc
    for key in data:
        try:
            timestamp_key = str2time(key)
            oldest_date = min(oldest_date, timestamp_key)
        except (ValueError, TypeError):
            continue

        value = data[key]
        history.append({"last_updated": key, "state": value})
    max_age = now_utc - oldest_date
    max_days = max(max_age.days, 1)
    mdata, _ = minute_data(history, max_days, now_utc, "state", "last_updated", backwards=backwards, smoothing=False, scale=1.0, clean_increment=False, required_unit=None)
    return [mdata, max_days]


def minute_data(
    history,
    days,
    now,
    state_key,
    last_updated_key,
    backwards=False,
    to_key=None,
    smoothing=False,
    clean_increment=False,
    divide_by=0,
    scale=1.0,
    accumulate=[],
    adjust_key=None,
    spreading=None,
    required_unit=None,
    prev_last_updated_time=None,
    last_state=0,
    attributes=False,
    max_increment=MAX_INCREMENT,
    interpolate=False,
    debug=False,
    can_modify_history=False,
):
    """
    Turns data from HA into a hash of data indexed by minute with the data being the value
    Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
    """
    mdata = {}
    adata = {}
    io_adjusted = {}
    newest_state = 0
    newest_age = 999999

    # Bounds on the data we store
    minute_min = -days * 24 * 60
    minute_max = days * 24 * 60

    # Check history is valid, if not empty return
    if not history:
        return mdata, io_adjusted

    # The glitch filter below is the only code here that writes to history, and it only runs for
    # backwards incrementing data, so that is the only case worth copying for. Copying regardless
    # cost ~150k deepcopy calls on a plan cycle for the two calculate_yesterday calls alone, neither
    # of which asks for the filter. can_modify_history stays the caller's explicit opt-out on top.
    if clean_increment and backwards and not can_modify_history:
        history = copy.deepcopy(history)  # Copy to avoid modifying original history

    # Glitch filter, cleans glitches in the data and removes bad values, only for incrementing data
    if clean_increment and backwards:
        if len(history) > 2:
            prev_prev_item = history[0]
            prev_item = history[1]

            if state_key in prev_item and state_key in prev_prev_item:
                try:
                    prev_value = float(prev_item[state_key])
                except (ValueError, TypeError):
                    prev_value = 0

                try:
                    prev_prev_value = float(prev_prev_item[state_key])
                except (ValueError, TypeError):
                    prev_prev_value = 0

                for item in history[2:]:
                    try:
                        value = float(item[state_key])
                    except (ValueError, TypeError):
                        value = prev_value
                        item[state_key] = value

                    # Filter simple glitch
                    if (prev_value > value) and (prev_value > prev_prev_value) and abs(prev_value - value) >= 0.1 and (value >= prev_prev_value):
                        prev_item[state_key] = value
                        prev_value = value

                    prev_prev_item = prev_item
                    prev_prev_value = prev_value
                    prev_value = value
                    prev_item = item

    # Process history
    for item in history:
        if last_updated_key not in item:
            continue

        if attributes:
            if state_key not in item["attributes"]:
                continue
            if item["attributes"][state_key] == "unavailable" or item["attributes"][state_key] == "unknown":
                continue
            state = item["attributes"][state_key]
        else:
            # Ignore data without correct keys
            if state_key not in item:
                continue
            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue
            state = item[state_key]

        # Get the numerical key and the timestamp and ignore if in error
        try:
            state = float(state) * scale
            last_updated_time = str2time(item[last_updated_key])
            # Truncate sub-minute precision: a timestamp of 23:30:04 should land on the 23:30 minute boundary,
            # not be floored to 23:31 due to int() truncation of the elapsed seconds.
            last_updated_time = last_updated_time.replace(second=0, microsecond=0)
        except (ValueError, TypeError):
            continue

        # Find and converter units
        integrate = False
        if required_unit and ("attributes" in item):
            if "unit_of_measurement" in item["attributes"]:
                unit = item["attributes"]["unit_of_measurement"]
                if unit != required_unit:
                    if required_unit in ["kWh"] and unit in ["W"]:
                        state = state / 1000.0
                        integrate = True
                    elif required_unit in ["kWh"] and unit in ["kW"]:
                        integrate = True
                    elif required_unit in ["kW", "kWh", "kg", "kg/kWh"] and unit in ["W", "Wh", "g", "g/kWh"]:
                        state = state / 1000.0
                    elif required_unit in ["W", "Wh", "g", "g/kWh"] and unit in ["kW", "kWh", "kg", "kg/kWh"]:
                        state = state * 1000.0
                    elif required_unit in ["MW", "MWh"] and unit in ["kW", "kWh"]:
                        state = state / 1000.0
                    elif required_unit in ["kW", "kWh"] and unit in ["MW", "MWh"]:
                        state = state * 1000.0
                    else:
                        # Ignore data in wrong units if we can't converter
                        continue

        # Divide down the state if required
        if divide_by:
            state /= divide_by

        # Update prev to the first if not set
        if not prev_last_updated_time:
            prev_last_updated_time = last_updated_time
            last_state = state

        # Intelligent adjusted?
        if adjust_key:
            adjusted = item.get(adjust_key, False)
        else:
            adjusted = False

        # Work out end of time period
        # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
        if to_key:
            to_value = item[to_key]
            if not to_value:
                to_time = now + timedelta(minutes=24 * 60 * days)
            else:
                to_time = str2time(item[to_key])
        else:
            if backwards:
                to_time = prev_last_updated_time
            else:
                if smoothing:
                    to_time = last_updated_time
                    last_updated_time = prev_last_updated_time
                else:
                    to_time = None

        if backwards:
            timed = now - last_updated_time
            if to_time:
                timed_to = now - to_time
        else:
            timed = last_updated_time - now
            if to_time:
                timed_to = to_time - now

        minutes = int(timed.total_seconds() / 60)
        if to_time:
            minutes_to = int(timed_to.total_seconds() / 60)
            minutes_delta = (timed_to.total_seconds() - timed.total_seconds()) / 60.0

        if minutes < newest_age:
            newest_age = minutes
            newest_state = state

        # Power to Energy
        if integrate and to_time:
            total_minutes = abs(minutes_delta)
            state = last_state + state * total_minutes / 60.0

        if to_time:
            minute = minutes
            if minute == minutes_to:
                if minute >= minute_min and minute <= minute_max:
                    mdata[minute] = state
            else:
                if smoothing:
                    near_midnight = (last_updated_time.time() < time(0, 6)) or (last_updated_time.time() > time(23, 58))
                    if clean_increment and state < last_state and (near_midnight or (last_state - state >= 1)):
                        # If there is a large drop in the data or we are near midnight where the sensor resets to zero,
                        # then smooth out the drop.
                        if debug:
                            print(f"Found drop at minute {minute}, where {state} < {last_state} (near midnight = {near_midnight}). Padding to {minutes_to}")
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = state
                            minute += 1
                    else:
                        # Otherwise linearly interpolate between the two points, ignoring small dips in the data
                        if clean_increment and state < last_state:
                            state = last_state
                        diff = (state - last_state) / minutes_delta

                        if debug:
                            print(f"Smoothing from minute {minute} to {minutes_to}, where diff = {diff} = ({state} - {last_state}) / {minutes_delta}")

                        # If the spike is too big don't smooth it, it will removed in the clean function later
                        if clean_increment and max_increment > 0 and diff > max_increment:
                            if debug:
                                print(f"    Increment larger than max {max_increment}, setting diff to 0.")
                            diff = 0

                        index = 0
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                if backwards:
                                    mdata[minute] = state - diff * index
                                else:
                                    mdata[minute] = last_state + diff * index
                            minute += 1
                            index += 1
                else:
                    if backwards:
                        # In backwards (oldest-first) mode, this item's `state` became active AT `minutes`.
                        # Write the current state at the transition minute, then fill the older period
                        # (minutes+1 to minutes_to inclusive) with `last_state` (the previous value).
                        if minutes >= minute_min and minutes <= minute_max:
                            mdata[minutes] = state
                        minute = minutes + 1
                        while minute <= minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = last_state
                                if adjusted:
                                    adata[minute] = True
                            minute += 1
                    else:
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = state
                                if adjusted:
                                    adata[minute] = True
                            minute += 1
        else:
            if spreading:
                for minute in range(minutes, minutes + spreading):
                    if minute >= minute_min and minute <= minute_max:
                        mdata[minute] = state
            else:
                if minutes >= minute_min and minutes <= minute_max:
                    mdata[minutes] = state

        # Store previous time & state
        if to_time and not backwards:
            prev_last_updated_time = to_time
        else:
            prev_last_updated_time = last_updated_time
        last_state = state

    # If we only have a start time then fill the gaps with the last values
    if not to_key:
        # Fill from last sample until now with interpolation if enabled
        if interpolate and clean_increment and backwards:
            last_sample_minute = 0
            for minute in range(60):
                if minute in mdata:
                    last_sample_minute = minute
                    break
            last_but_one_sample_minute = last_sample_minute
            for minute in range(last_sample_minute + 5, 60):
                if minute in mdata and (mdata[minute] != mdata[last_sample_minute]):
                    last_but_one_sample_minute = minute
                    break
            sample_gap = last_but_one_sample_minute - last_sample_minute
            if last_sample_minute > 0 and sample_gap > 0 and last_sample_minute < 15:
                last_sample_value = mdata[last_sample_minute]
                last_but_one_minute_sample = mdata[last_but_one_sample_minute]
                step = (last_sample_value - last_but_one_minute_sample) / sample_gap
                if step > 0:
                    for minute in range(last_sample_minute):
                        if minute >= minute_min and minute <= minute_max:
                            mdata[minute] = dp4(last_sample_value + step * (last_sample_minute - minute))

        # Fill from last sample until now
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = minute
            else:
                rindex = 60 * 24 * days - minute - 1

            if rindex not in mdata:
                if rindex >= minute_min and rindex <= minute_max:
                    mdata[rindex] = newest_state
            else:
                break

        # Find the first value
        state = 0
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = 60 * 24 * days - minute - 1
            else:
                rindex = minute
            if rindex in mdata:
                state = mdata[rindex]
                break

        # Fill gaps in the middle
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = 60 * 24 * days - minute - 1
            else:
                rindex = minute
            state = mdata.get(rindex, state)
            if rindex >= minute_min and rindex <= minute_max:
                mdata[rindex] = state

    # Reverse data with smoothing
    if clean_increment:
        mdata = clean_incrementing_reverse(mdata, max_increment)

    # Accumulate to previous data?
    if accumulate:
        for minute in range(60 * 24 * days):
            if minute in mdata:
                mdata[minute] += accumulate.get(minute, 0)
            else:
                if minute >= minute_min and minute <= minute_max:
                    mdata[minute] = accumulate.get(minute, 0)

    if adjust_key:
        io_adjusted = adata

    # Rounding
    for minute in mdata.keys():
        mdata[minute] = dp4(mdata[minute])

    return mdata, io_adjusted


def clean_incrementing_reverse(data, max_increment=0):
    """
    Cleanup an incrementing sensor data that runs backwards in time to remove the
    resets (where it goes back to 0) and make it always increment
    """
    new_data = {}
    if not data:
        return new_data
    length = max(data) + 1

    increment = 0
    last = data[length - 1]

    for index in range(length):
        rindex = length - index - 1
        nxt = data.get(rindex, last)
        if nxt >= last:
            if (max_increment > 0) and ((nxt - last) > max_increment):
                # Smooth out big spikes
                pass
            else:
                increment += nxt - last
            last = nxt
        elif nxt < last:
            if nxt <= 0 or ((last - nxt) >= (1.0)):
                last = nxt
        new_data[rindex] = increment

    return new_data


def format_time_ago(last_updated):
    """
    Format a timestamp to show how many minutes ago it was updated
    """
    if not last_updated:
        return "Never updated"

    try:
        now = datetime.now(timezone.utc)
        if not last_updated:
            return "Never updated"

        # Calculate time difference
        time_diff = now - last_updated
        total_minutes = int(time_diff.total_seconds() / 60)

        if total_minutes < 0:
            return "Just now"
        elif total_minutes == 0:
            return "Just now"
        elif total_minutes == 1:
            return "1 minute ago"
        elif total_minutes < 60:
            return f"{total_minutes} minutes ago"
        elif total_minutes < 120:
            return "1 hour ago"
        elif total_minutes < 1440:  # Less than 24 hours
            hours = total_minutes // 60
            return f"{hours} hours ago"
        else:  # More than 24 hours
            days = total_minutes // 1440
            if days == 1:
                return "1 day ago"
            else:
                return f"{days} days ago"

    except Exception as e:
        print(f"Error formatting time ago: {e}")
        return "Unknown ({})".format(last_updated)


# The format Predbat publishes car charging plan windows in. No year, because a plan never
# reaches more than 48 hours ahead - parse_car_plan_windows() puts one back.
CAR_PLAN_TIME_FORMAT = "%m-%d %H:%M:%S"

# How far from now a parsed window has to land before the year stamped on it is treated as
# the wrong one. Comfortably beyond the 48 hours a plan covers, so a genuinely distant
# window is never dragged into a different year, and far short of the ~12 months a
# mis-stamped year produces.
CAR_PLAN_YEAR_MARGIN = timedelta(days=180)


def parse_car_plan_windows(planned, now, local_tz):
    """Turn one car's published charging plan into a list of localised (start, end) pairs.

    Shared by the components that drive a charger from the plan (myenergi, GivEnergy EVC)
    so the awkward parts stay in one place: the plan carries no year, so each window is
    rebuilt around now - without that, a plan read either side of New Year lands eleven
    months out - and a malformed entry is skipped rather than costing the rest of the plan.

    The rebuild is symmetric. A window read at 23:30 on 31 December whose end is stamped
    01-01 parses as January of the year just ending, and needs shifting forward; the same
    window read at 00:30 on 1 January has its 12-31 start parsed as December of the year
    just started, and needs shifting back. Only the second case ever hides an active
    window, which is why it is the one that stops a car mid-charge if it is missed.

    Args:
        planned: The 'planned' attribute of a car charging slot sensor, a list of dicts
            with 'start' and 'end' keys.
        now: The instant every window is judged against, localised.
        local_tz: The timezone the plan's wall clock times are expressed in.
    """
    parsed = []
    for window in planned or []:
        try:
            start = local_tz.localize(datetime.strptime(window["start"], CAR_PLAN_TIME_FORMAT).replace(year=now.year))
            end = local_tz.localize(datetime.strptime(window["end"], CAR_PLAN_TIME_FORMAT).replace(year=now.year))
        except (KeyError, TypeError, ValueError):
            continue
        # Shift both ends together so their spacing survives, then close a window whose
        # end is in January while its start is still in December
        if start > now + CAR_PLAN_YEAR_MARGIN:
            start = start.replace(year=start.year - 1)
            end = end.replace(year=end.year - 1)
        elif start < now - CAR_PLAN_YEAR_MARGIN:
            start = start.replace(year=start.year + 1)
            end = end.replace(year=end.year + 1)
        if end < start:
            end = end.replace(year=end.year + 1)
        parsed.append((start, end))
    return parsed


def in_car_plan_window(windows, now):
    """Is now inside one of the (start, end) pairs returned by parse_car_plan_windows."""
    return any(start <= now < end for start, end in windows)


def in_iboost_slot(minute, iboost_plan):
    """
    Is the given minute inside a car slot
    """
    load_amount = 0

    if iboost_plan:
        for slot in iboost_plan:
            start_minutes = slot["start"]
            end_minutes = slot["end"]
            kwh = slot["kwh"]
            slot_minutes = end_minutes - start_minutes
            slot_hours = slot_minutes / 60.0

            # Return the load in that slot
            if minute >= start_minutes and minute < end_minutes:
                load_amount = abs(kwh / slot_hours)
                break
    return load_amount


def in_car_slot(minute, num_cars, car_charging_slots, slot_cap=None, slot_cap_period=None):
    """
    Is the given minute inside a car slot
    """
    load_amount = [0 for car_n in range(num_cars)]
    rate_amount = [0 for car_n in range(num_cars)]

    for car_n in range(num_cars):
        if car_charging_slots[car_n]:
            for slot in car_charging_slots[car_n]:
                start_minutes = slot["start"]
                end_minutes = slot["end"]
                kwh = slot["kwh"]
                octopus = slot.get("octopus", False)
                slot_minutes = end_minutes - start_minutes
                slot_hours = slot_minutes / 60.0

                # Return the load in that slot
                if minute >= start_minutes and minute < end_minutes:
                    load_amount[car_n] = abs(kwh / slot_hours)
                    # Only return rate for octopus as its used for premium calculations
                    rate_amount[car_n] = slot.get("average", 0) if octopus else 0
                    break
    return load_amount, rate_amount


def time_string_to_stamp(time_string):
    """
    Convert a time string to a timestamp
    """
    if time_string is None:
        return None
    if time_string == "unknown":
        return None

    if isinstance(time_string, str) and len(time_string) == 5:
        time_string += ":00"

    # Some inverters (e.g. GivEnergy) use "24:00:00" for midnight end-of-day
    if isinstance(time_string, str) and time_string.startswith("24:"):
        time_string = "00:" + time_string[3:]

    try:
        return datetime.strptime(time_string, "%H:%M:%S")
    except (ValueError, TypeError):
        print("WARN: time_string_to_stamp: invalid time string '{}', returning None".format(time_string))
        return None


def compute_window_minutes(start_time, end_time, minutes_now):
    """
    Convert start/end times to minutes and adjust for midnight-spanning windows and past windows.

    Args:
        start_time: Start time (datetime, time, or object with hour/minute attributes)
        end_time: End time (datetime, time, or object with hour/minute attributes)
        minutes_now: Current time in minutes from midnight

    Returns:
        Tuple of (start_minute, end_minute) adjusted for midnight spanning and current time
    """
    if start_time is None or end_time is None:
        # Invalid time, return 0,0
        return 0, 0

    start_minute = start_time.hour * 60 + start_time.minute
    end_minute = end_time.hour * 60 + end_time.minute

    if end_minute < start_minute:
        # Window spans midnight - adjust based on current time
        if end_minute > minutes_now:
            # We're past midnight but before end - move start back
            start_minute -= 24 * 60
        else:
            # End has passed - move end forward
            end_minute += 24 * 60

    # Window already passed, move it forward until the next one
    if end_minute <= minutes_now:
        start_minute += 24 * 60
        end_minute += 24 * 60

    return start_minute, end_minute


def window2minutes(start, end, minutes_now):
    """
    Convert time start/end window string into minutes
    """
    start = time_string_to_stamp(start)
    end = time_string_to_stamp(end)
    return compute_window_minutes(start, end, minutes_now)


def minutes_since_yesterday(now):
    """
    Calculate the number of minutes since 23:59 yesterday
    """
    yesterday = now - timedelta(days=1)
    yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
    difference = now - yesterday_at_2359
    difference_minutes = int((difference.seconds + 59) / 60)
    return difference_minutes


def dp0(value):
    """
    Round to 0 decimal places
    """
    return round(value)


def dp1(value):
    """
    Round to 1 decimal place
    """
    return round(value, 1)


def dp2(value):
    """
    Round to 2 decimal places
    """
    return round(value, 2)


def dp3(value):
    """
    Round to 3 decimal places
    """
    return round(value, 3)


def dp4(value):
    """
    Round to 4 decimal places
    """
    return round(value, 4)


def minutes_to_time(updated, now):
    """
    Compute the number of minutes between a time (now) and the updated time
    """
    timeday = updated - now
    minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
    return minutes


def str2time_strptime(time_str):
    """
    Parse a timezone-aware time string into a datetime using strptime (legacy implementation)
    """
    if "." in time_str:
        tdata = datetime.strptime(time_str, TIME_FORMAT_SECONDS)
    elif "T" in time_str:
        tdata = datetime.strptime(time_str, TIME_FORMAT)
    else:
        tdata = datetime.strptime(time_str, TIME_FORMAT_OCTOPUS)
    return tdata


def str2time(time_str):
    """
    Parse a timezone-aware time string into a datetime

    Uses datetime.fromisoformat as a fast path (C-implemented, far less allocation churn than
    strptime) when STR2TIME_USE_FROMISOFORMAT is set, falling back to strptime for any string
    fromisoformat cannot handle or parses without a UTC offset (the strptime formats all
    require an offset, so the fallback preserves the ValueError contract for naive strings).
    """
    if STR2TIME_USE_FROMISOFORMAT:
        try:
            tdata = datetime.fromisoformat(time_str)
        except (ValueError, TypeError):
            return str2time_strptime(time_str)
        if tdata.tzinfo is None:
            return str2time_strptime(time_str)
        return tdata
    return str2time_strptime(time_str)


def calc_percent_limit(charge_limit, soc_max):
    """
    Calculate a charge limit in percent
    """
    if isinstance(charge_limit, list):
        if soc_max <= 0:
            return [0 for i in range(len(charge_limit))]
        else:
            return [min(int((float(charge_limit[i]) / soc_max * 100.0) + 0.5), 100) for i in range(len(charge_limit))]
    else:
        if soc_max <= 0:
            return 0
        else:
            return min(int((float(charge_limit) / soc_max * 100.0) + 0.5), 100)


def clone_windows(windows):
    """Shallow-copy a list of window dicts (start/end/average/... primitive fields only).

    Window dicts never hold nested mutable values, so copying each dict is equivalent to
    copy.deepcopy(windows) here but far cheaper - deepcopy's generic recursive walk measured
    ~275us per call on a typical export_window, this is a few us.
    """
    return [w.copy() for w in windows]


def remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best):
    """
    Filters and removes intersecting charge windows

    This runs on every simulation (see Prediction.run_prediction and run_prediction_kernel), so it
    sits in front of the C++ kernel on the hot path and only does the work that can change something:

    - only export windows that are enabled (limit < 100) can clip anything, so they are collected
      once and the function returns immediately when there are none
    - only charge windows that are enabled (limit > 0) can be clipped, so a disabled one
      short-circuits instead of being scanned against every export window

    Clipping is a single pass. Export windows are processed in start order, so when a charge window
    is split the head segment it emits ends at the current export window's start and no later export
    window can reach back into it - which is what the previous "clip again" pass over the whole
    window list existed to catch. The sort is kept even though callers already provide sorted
    windows, so correctness does not depend on that.

    See run_intersect_window_tests, which pins this behaviour with hand-written cases and compares
    the result against a naive reference implementation over randomised window layouts.
    """
    # Enabled export windows only - the sole candidates for clipping anything
    export_active = sorted((export_window_best[n]["start"], export_window_best[n]["end"]) for n in range(len(export_limit_best)) if export_limit_best[n] < 100.0)
    if not export_active:
        # Rebuild the windows rather than passing the caller's dicts back, so the returned windows
        # carry exactly the same keys (and are as freshly owned) as on the clipping path below
        return list(charge_limit_best), [{"start": w["start"], "end": w["end"], "average": w["average"]} for w in charge_window_best]

    new_limit_best = []
    new_window_best = []

    # For each charge window
    for window_n in range(len(charge_limit_best)):
        window = charge_window_best[window_n]
        start = window["start"]
        end = window["end"]
        average = window["average"]
        limit = charge_limit_best[window_n]
        clipped = False

        if limit <= 0.0:
            # A disabled charge window can never be clipped; rebuild it exactly as the clipping
            # path below would have done, so the returned dicts are equivalent either way
            new_window_best.append({"start": start, "end": end, "average": average})
            new_limit_best.append(limit)
            continue

        # For each enabled discharge window, in start order
        for dstart, dend in export_active:
            # Overlapping window?
            if (dstart < end) and (dend >= start):
                if dstart <= start:
                    if start != dend:
                        start = dend
                        clipped = True
                elif dend >= end:
                    if end != dstart:
                        end = dstart
                        clipped = True
                else:
                    # Two segments - emit the head now, carry on clipping the tail
                    if (dstart - start) >= 5:
                        new_window_best.append({"start": start, "end": dstart, "average": average})
                        new_limit_best.append(limit)
                    start = dend
                    clipped = True

        if not clipped or ((end - start) >= 5):
            new_window_best.append({"start": start, "end": end, "average": average})
            new_limit_best.append(limit)

    return new_limit_best, new_window_best


@lru_cache(maxsize=8192)
def get_charge_rate_curve_cached(soc, charge_rate_setting, soc_max, battery_rate_max_charge, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple):
    """
    Cached computation of true charging rate from SoC and charge rate setting
    """
    battery_charge_power_curve = dict(battery_charge_power_curve_tuple) if battery_charge_power_curve_tuple else {}
    battery_temperature_curve = dict(battery_temperature_curve_tuple) if battery_temperature_curve_tuple else {}

    soc_percent = calc_percent_limit(soc, soc_max)
    max_charge_rate = battery_rate_max_charge * get_curve_value(battery_charge_power_curve, soc_percent, 1.0)

    # Temperature cap
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_charge)
    max_charge_rate = min(max_charge_rate, max_rate_cap)

    return max(min(charge_rate_setting, max_charge_rate), battery_rate_min)


def get_charge_rate_curve(soc, charge_rate_setting, soc_max, battery_rate_max_charge, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve):
    """
    Compute true charging rate from SoC and charge rate setting
    """
    return get_charge_rate_curve_cached(round(soc, 1), charge_rate_setting, soc_max, battery_rate_max_charge, charge_curve_to_tuple(battery_charge_power_curve), battery_rate_min, battery_temperature, charge_curve_to_tuple(battery_temperature_curve))


@lru_cache(maxsize=8192)
def get_discharge_rate_curve_cached(soc, discharge_rate_setting, soc_max, battery_rate_max_discharge, battery_discharge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple):
    """
    Cached computation of true discharging rate from SoC and charge rate setting
    """
    battery_discharge_power_curve = dict(battery_discharge_power_curve_tuple) if battery_discharge_power_curve_tuple else {}
    battery_temperature_curve = dict(battery_temperature_curve_tuple) if battery_temperature_curve_tuple else {}

    soc_percent = calc_percent_limit(soc, soc_max)
    max_discharge_rate = battery_rate_max_discharge * get_curve_value(battery_discharge_power_curve, soc_percent, 1.0)
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_discharge)
    max_discharge_rate = min(max_discharge_rate, max_rate_cap)

    return max(min(discharge_rate_setting, max_discharge_rate), battery_rate_min)


def get_discharge_rate_curve(soc, discharge_rate_setting, soc_max, battery_rate_max_discharge, battery_discharge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve):
    """
    Compute true discharging rate from SoC and charge rate setting
    """
    return get_discharge_rate_curve_cached(
        round(soc, 1), discharge_rate_setting, soc_max, battery_rate_max_discharge, charge_curve_to_tuple(battery_discharge_power_curve), battery_rate_min, battery_temperature, charge_curve_to_tuple(battery_temperature_curve)
    )


"""
Get value from curve with integer or string index
"""


def get_curve_value(curve, index, default=1.0):
    return curve.get(index, default)


def find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, max_rate):
    """
    Find the battery temperature cap
    """
    battery_temperature_idx = min(battery_temperature, 20)
    battery_temperature_idx = max(battery_temperature_idx, -20)
    battery_temperature_idx = int(battery_temperature_idx)  # Convert to int for proper key matching
    # Try to get the temperature adjustment from the curve (handles both int and string keys)
    battery_temperature_adjust = get_curve_value(battery_temperature_curve, battery_temperature_idx, None)
    if battery_temperature_adjust is None:
        # If not found, try fallback values
        if battery_temperature_idx > 0:
            battery_temperature_adjust = get_curve_value(battery_temperature_curve, 20, 1.0)
        else:
            battery_temperature_adjust = get_curve_value(battery_temperature_curve, 0, 1.0)
    battery_temperature_rate_cap = soc_max * battery_temperature_adjust / 60.0

    return min(battery_temperature_rate_cap, max_rate)


def find_charge_rate(
    minutes_now,
    soc,
    window,
    target_soc,
    max_rate,
    soc_max,
    battery_charge_power_curve,
    set_charge_low_power,
    charge_low_power_margin,
    battery_rate_min,
    battery_rate_max_scaling,
    battery_loss,
    log_to,
    battery_temperature=20,
    battery_temperature_curve={},
    current_charge_rate=None,
    pv_window_kwh=0.0,
):
    """
    Find the lowest charge rate that fits the charge slow

    pv_window_kwh is the PV forecast in kWh over the remainder of the charge window, when the window
    overlaps PV production low power charging is abandoned as the throttled rate applies for the whole
    window and would push the PV out of the battery, raising the cost above the planned full rate charge
    """
    margin = charge_low_power_margin
    target_soc = round(target_soc, 2)

    # Current charge rate
    if current_charge_rate is None:
        current_charge_rate = max_rate

    battery_temperature_curve_tuple = charge_curve_to_tuple(battery_temperature_curve)
    battery_charge_power_curve_tuple = charge_curve_to_tuple(battery_charge_power_curve)

    # Real achieved max rate
    max_rate_real = get_charge_rate_curve_cached(round(soc, 1), max_rate, soc_max, max_rate, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple) * battery_rate_max_scaling

    min_battery_rate = max(400, int(round(battery_rate_min * MINUTE_WATT)))
    if set_charge_low_power:
        # If the charge window overlaps with PV production then charge at max rate, a throttled rate would
        # cap the PV going into the battery, exporting the surplus and importing to make the target up later
        if pv_window_kwh > LOW_POWER_PV_THRESHOLD:
            if log_to:
                log_to("Low power mode: PV forecast in window {}kWh > {}kWh, default to max rate".format(dp2(pv_window_kwh), LOW_POWER_PV_THRESHOLD))
            return max_rate, max_rate_real

        minutes_left = window["end"] - minutes_now - margin
        abs_minutes_left = window["end"] - minutes_now

        # If we don't have enough minutes left go to max
        if abs_minutes_left < 0:
            if log_to:
                log_to("Low power mode: abs_minutes_left {} < 0, default to max rate".format(dp2(abs_minutes_left)))
            return max_rate, max_rate_real

        # If we already have reached target go back to max
        if round(soc, 2) >= target_soc:
            if log_to:
                log_to("Low power mode: SoC {}kW >= target_SoC {}kW, default to max rate".format(soc, target_soc))
            return max_rate, max_rate_real

        # Work out the charge left in kw
        charge_left = round(target_soc - soc, 2)

        # If we can never hit the target then go to max
        if round(max_rate_real * abs_minutes_left, 2) <= charge_left:
            if log_to:
                log_to(
                    "Low power mode: Can't hit target: max_rate * abs_minutes_left = {}kW <= charge_left {}kW, minutes_left {}, window_end {}, minutes_now {}, default to max rate".format(
                        dp2(max_rate_real * abs_minutes_left), charge_left, abs_minutes_left, window["end"], minutes_now
                    )
                )
            return max_rate, max_rate_real

        # What's the lowest we could go?
        min_rate = charge_left / abs_minutes_left
        min_rate_w = int(min_rate * MINUTE_WATT)

        # Apply the curve at each rate to pick one that works
        rate_w = max_rate * MINUTE_WATT
        best_rate = max_rate
        best_rate_real = max_rate_real
        highest_achievable_rate = 0

        if log_to:
            log_to(
                "Find charge rate for low power mode: SoC: {}kW, target_SoC: {}kW, charge_left: {}kW, minutes_left: {}, abs_minutes_left: {}, max_rate: {}W, min_rate: {}W, min_rate_w: {}W".format(
                    soc, target_soc, charge_left, minutes_left, abs_minutes_left, dp0(max_rate * MINUTE_WATT), dp0(min_rate * MINUTE_WATT), dp0(min_rate_w)
                )
            )

        while rate_w >= min_battery_rate:
            rate = rate_w / MINUTE_WATT
            if rate_w >= min_rate_w:
                charge_now = soc
                minute = 0
                rate_scale_max = 0
                # Compute over the time period, include the completion time
                for minute in range(0, minutes_left, PREDICT_STEP):
                    rate_scale = get_charge_rate_curve_cached(round(charge_now, 1), rate, soc_max, max_rate, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple)
                    highest_achievable_rate = max(highest_achievable_rate, rate_scale)
                    rate_scale *= battery_rate_max_scaling
                    rate_scale_max = max(rate_scale_max, rate_scale)
                    charge_amount = rate_scale * PREDICT_STEP * battery_loss
                    charge_now += charge_amount
                    if (round(charge_now, 2) >= target_soc) and (rate_scale_max < best_rate_real):
                        best_rate = rate
                        best_rate_real = rate_scale_max
                        break
                # if log_to:
                #   log_to("Low Power mode: rate: {} minutes: {} SOC: {} Target SoC: {} Charge left: {} Charge now: {} Rate scale: {} Charge amount: {} Charge now: {} best rate: {} highest achievable_rate {}".format(
                #        rate * MINUTE_WATT, minute, soc, target_soc, charge_left, charge_now, rate_scale * MINUTE_WATT, charge_amount, round(charge_now, 2), best_rate*MINUTE_WATT, highest_achievable_rate*MINUTE_WATT))
            else:
                break
            rate_w -= 100.0

        # Stick with current rate if it doesn't matter
        if best_rate >= highest_achievable_rate and current_charge_rate >= highest_achievable_rate:
            best_rate = current_charge_rate
            if log_to:
                log_to(
                    "Low Power mode: best rate {}W is greater than highest achievable rate {}W and current rate {}W, so sticking with current rate".format(
                        dp0(best_rate * MINUTE_WATT), dp0(highest_achievable_rate * MINUTE_WATT), dp0(current_charge_rate * MINUTE_WATT)
                    )
                )

        best_rate_real = get_charge_rate_curve_cached(round(soc, 1), best_rate, soc_max, max_rate, battery_charge_power_curve_tuple, battery_rate_min, battery_temperature, battery_temperature_curve_tuple) * battery_rate_max_scaling
        if log_to:
            log_to(
                "Low Power mode: minutes left: {}, absolute: {}, SoC: {}kW, Target SoC: {}kW, Charge left: {}kW, Max rate: {}W, Min rate: {}W, Best rate: {}W, Best rate real: {}W, Battery temp {}°C".format(
                    minutes_left, abs_minutes_left, soc, target_soc, charge_left, dp0(max_rate * MINUTE_WATT), dp0(min_rate * MINUTE_WATT), dp0(best_rate * MINUTE_WATT), dp0(best_rate_real * MINUTE_WATT), battery_temperature
                )
            )
        return best_rate, best_rate_real
    else:
        return max_rate, max_rate_real


CDN_BLOCK_MARKERS = ("cloudfront", "request blocked", "the request could not be satisfied")
HTML_DOCUMENT_PREFIXES = ("<!doctype", "<html")
# Every Kraken-based provider mints its JWT through the same CDN-fronted endpoint, so an
# edge block can catch the mint as well as the queries. Unlike a query the mint has no cached
# result to fall back on: once the JWT expires every authenticated call needs a new one, so
# without a backoff a component re-mints on every poll and keeps hammering an endpoint that
# is already refusing it. Back off exponentially instead, capped so a block that lifts is
# still picked up within the hour.
TOKEN_MINT_BACKOFF_BASE_SECONDS = 300
TOKEN_MINT_BACKOFF_MAX_SECONDS = 3600
# Bound the exponent so a long block cannot grow 2 ** block_count without limit; the delay
# is capped well before this, so the clamp only stops the arithmetic running away.
TOKEN_MINT_BACKOFF_MAX_DOUBLINGS = 16
# While suppressed the mint makes no request and so logs nothing, which leaves a reader of a
# short log window unable to tell a deliberate cooldown from a bad API key. Repeat the reason
# at most this often.
TOKEN_MINT_BACKOFF_LOG_INTERVAL_SECONDS = 600


def token_mint_backoff_seconds(block_count):
    """Backoff delay in seconds after this many consecutive CDN blocks on a token mint.

    Doubles per consecutive block from TOKEN_MINT_BACKOFF_BASE_SECONDS, capped at
    TOKEN_MINT_BACKOFF_MAX_SECONDS so a block that lifts is still picked up within the hour.

    Args:
        block_count: Number of consecutive blocks so far, 1 for the first.

    Returns:
        int: Delay in seconds.
    """
    exponent = min(max(block_count - 1, 0), TOKEN_MINT_BACKOFF_MAX_DOUBLINGS)
    return min(TOKEN_MINT_BACKOFF_BASE_SECONDS * (2**exponent), TOKEN_MINT_BACKOFF_MAX_SECONDS)


def is_edge_block_body(text):
    """Return True if a 403 body is positively identifiable as a CDN/WAF error page.

    Kraken reports authentication problems as a JSON GraphQL error body (normally with
    HTTP 200) or as a 401. A 403 carrying an HTML error page - e.g. CloudFront's
    "Request blocked" - is edge rate limiting, not a credential problem, so the cached
    token must be kept rather than discarded and immediately re-minted.

    Two conditions must both hold: the body must not parse as JSON (anything the API
    itself produces is JSON), and it must look like an HTML document or name a known CDN.
    Matching on wording alone would misclassify a genuine JSON error that happens to say
    something like "access denied", which would keep an invalid token forever - the same
    permanent lockout this check exists to prevent, arrived at from the other direction.

    Detection is deliberately conservative: a 403 we cannot identify as a CDN page keeps
    the existing "refresh the token and retry" behaviour, which recovers genuinely revoked
    tokens without needing a restart.

    Args:
        text: The raw response body.

    Returns:
        bool: True if the body carries a known CDN/WAF block signature.
    """
    if not isinstance(text, str) or not text:
        return False
    try:
        json.loads(text)
    except (ValueError, TypeError):
        pass
    else:
        # A parseable JSON body came from the API, not from an edge appliance
        return False
    stripped = text.lstrip().lower()
    return stripped.startswith(HTML_DOCUMENT_PREFIXES) or any(marker in stripped for marker in CDN_BLOCK_MARKERS)

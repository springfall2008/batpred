# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Shared tool layer for Predbat's AI surfaces.

Holds one implementation of each Predbat tool, plus the schema each surface needs: MCP publishes
``inputSchema``, the chat agent publishes OpenAI function-calling ``parameters``. Keeping both
projections over one list is what stops the two surfaces drifting apart as tools are added.
"""

import asyncio
import functools
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from utils import (
    SECRET_MASK,
    parse_yaml_path,
    resolve_nested_yaml_value,
    calc_percent_limit,
    get_override_time_from_string,
    mask_secret_args,
    is_secret_key,
    read_predbat_log,
    classify_log_line,
    log_line_included,
    parse_log_timestamp,
    is_debug_excluded_key,
    is_data_numerical,
    str2time,
)


# Log filter levels accepted by the get_log tool, matching the web log view's tabs
LOG_FILTER_TYPES = ("all", "info", "warnings", "errors")

# get_log line budget - the default keeps a response small enough for an AI context window,
# the cap stops a "max_lines": 999999 request pulling a 10MB log through the protocol (#4768)
MCP_LOG_DEFAULT_LINES = 500
MCP_LOG_MAX_LINES = 5000


# get_state size guards. A real debug dump is ~5MB, but 272 of its 313 top-level keys are under
# 1KB and total under 10KB between them - the whole scalar state of Predbat. The per-key budget
# returns those freely while the handful of per-minute series (load_minutes, rate_import, ...)
# are described rather than serialised (#4768).
MCP_STATE_DEFAULT_MAX_BYTES = 2048
MCP_STATE_MAX_BYTES_LIMIT = 262144
MCP_STATE_TOTAL_BYTES_LIMIT = 262144

# Collections longer than this are summarised without serialising them first - measuring a 2880
# entry per-minute dict by encoding it would cost more than the tool call is worth.
MCP_STATE_LARGE_COLLECTION = 200

# How many entries of a large collection to show in its summary
MCP_STATE_SAMPLE_ENTRIES = 3

# search_entities size guards - a large install can carry several thousand entities, so a search
# with no limit (or a badly-chosen pattern matching almost everything) is capped the same way
# get_log's max_lines is: a small default for a normal answer, a hard ceiling so a client asking
# for "everything" cannot flood the response.
MCP_ENTITY_SEARCH_DEFAULT_LIMIT = 50
MCP_ENTITY_SEARCH_MAX_LIMIT = 200

# get_entity_history guards (#4768 follow-up: HA state access). The lookback cap bounds the
# *fetch* - how far back get_history_wrapper is asked to go - which matters even before any
# bucketing happens, since a careless request could otherwise ask the recorder for years of a
# per-minute sensor. 30 days matches get_history_wrapper's own default and HAHistory's default
# retention window, so a request within it never asks Predbat for more than Predbat already
# tracks about itself. The bucket cap is a separate guard on the *response*: it protects the
# reply from a small bucket_minutes over a long window (e.g. 1-minute buckets over 30 days would
# be 43200 buckets) by shrinking the window rather than the bucket width, since a caller that
# asked for fine-grained buckets presumably wants them fine, not merged.
MCP_HISTORY_MAX_LOOKBACK_DAYS = 30
MCP_HISTORY_DEFAULT_BUCKET_MINUTES = 30
MCP_HISTORY_MAX_BUCKETS = 500


def json_safe_value(value, depth=0):
    """
    Convert a Predbat state value into something json.dumps can encode.

    Tool results are serialised with a plain json.dumps, so a stray datetime or inverter object
    would fail the whole call - anything unrecognised becomes its str() rather than an error.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if depth >= 6:
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe_value(item, depth + 1) for item in value]
    return str(value)


def summarise_state_value(value):
    """
    Describe a state value that is too large to return in full, so the caller can still see its
    shape and decide whether to ask for it another way.
    """
    summary = {"type": type(value).__name__}
    try:
        summary["length"] = len(value)
    except TypeError:
        pass

    if isinstance(value, dict):
        keys = list(value.keys())[:MCP_STATE_SAMPLE_ENTRIES]
        summary["sample_keys"] = [str(key) for key in keys]
        numbers = [item for item in value.values() if isinstance(item, (int, float)) and not isinstance(item, bool)]
    elif isinstance(value, (list, tuple, set)):
        numbers = [item for item in value if isinstance(item, (int, float)) and not isinstance(item, bool)]
        summary["sample_entries"] = [json_safe_value(item, depth=5) for item in list(value)[:MCP_STATE_SAMPLE_ENTRIES]]
    elif isinstance(value, str):
        summary["preview"] = value[:200]
        numbers = []
    else:
        numbers = []

    if numbers:
        summary["min"] = min(numbers)
        summary["max"] = max(numbers)
        summary["mean"] = sum(numbers) / len(numbers)

    return summary


def measure_state_value(value, max_bytes):
    """
    Return (fits, safe_value, size_bytes) for a state value.

    fits is a separate flag rather than a None safe_value because plenty of Predbat state is
    legitimately None, and a None sentinel would report every one of those as omitted.

    Long collections are rejected on their entry count alone so the per-minute series are never
    serialised just to discover they don't fit.
    """
    if isinstance(value, (dict, list, tuple, set)) and len(value) > MCP_STATE_LARGE_COLLECTION:
        return False, None, None
    safe = json_safe_value(value)
    try:
        size = len(json.dumps(safe))
    except (TypeError, ValueError):
        safe = str(value)
        size = len(safe) + 2
    if size > max_bytes:
        return False, None, None
    return True, safe, size


class MCPArgumentError(ValueError):
    """Raised when a tool argument is the wrong type or shape, so it can be reported clearly."""


def parse_number_argument(value, name, default, minimum=None, maximum=None, as_float=False):
    """
    Coerce a numeric tool argument, clamping it into range.

    Out-of-range values are clamped rather than rejected - a client asking for more than the cap
    wants as much as it can have - but a value that is not a number at all is a mistake, and the
    caller is an AI assistant that can only correct itself if told which argument was wrong.
    """
    if value is None:
        value = default
    if value is None:
        return None
    if isinstance(value, bool):
        raise MCPArgumentError("'{}' must be a number, not a boolean".format(name))
    try:
        value = float(value) if as_float else int(value)
    except (TypeError, ValueError):
        raise MCPArgumentError("'{}' must be a {}, got {!r}".format(name, "number" if as_float else "whole number", value))
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


# The longest regex a tool argument may carry, and a detector for the nested-quantifier shape
# that causes exponential backtracking. Matches SOURCE_PATTERN_MAX in chat_tools.py, which
# guards search_source's pattern for the same reason.
FILTER_PATTERN_MAX = 200

# A quantified group that itself contains a quantifier: (a+)+, (.*)*, (x+){2,}. This is the
# classic catastrophic-backtracking shape - the outer quantifier re-partitions what the inner
# one already matched, so the engine explores exponentially many splits before failing. Plain
# groups like (sensor|switch) and unquantified inner quantifiers like (\d+) are untouched: the
# outer quantifier is what turns the inner one into a blowup.
NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[*+][^()]*\)\s*[*+{]")


def reject_pathological_pattern(value, name="filter"):
    """
    Raise MCPArgumentError if a model-supplied regex is too long or backtracks catastrophically.

    A heuristic, not a proof: it catches the nested-quantifier shape that causes exponential
    blowup, which is what a pattern arriving through an injected instruction would use. It does
    not make every accepted pattern fast, so callers doing unbounded work should still bound it.
    Rejecting is safe for legitimate use - entity and config searches do not need a quantified
    group wrapped around another quantifier.
    """
    if len(value) > FILTER_PATTERN_MAX:
        raise MCPArgumentError("'{}' is longer than {} characters".format(name, FILTER_PATTERN_MAX))
    if NESTED_QUANTIFIER_RE.search(value):
        raise MCPArgumentError("'{}' contains a quantifier applied to a group that already contains one (such as '(a+)+'), which can take effectively forever to match; rewrite it without the nested quantifier".format(name))


def compile_filter_argument(value, name="filter"):
    """
    Compile a regex tool argument, or return None when no filter was given.

    Compiling up front turns an invalid pattern into a named argument error instead of a bare
    Python traceback, and avoids re-compiling it for every entry the caller filters over.

    The pattern comes from the model, and Python's re backtracks with no timeout, so a
    catastrophic pattern would run to the heat death of the universe on whichever thread called
    it - for search_entities that is the component's only event loop, which would hang every
    other chat and MCP request until Predbat restarted. reject_pathological_pattern() bounds
    what can be compiled here; every regex tool argument goes through this one function, so the
    same guard covers search_entities and the 'filter' arguments on get_state, get_apps and
    get_config alike.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MCPArgumentError("'{}' must be a string regular expression, got {!r}".format(name, value))
    reject_pathological_pattern(value, name)
    try:
        return re.compile(value)
    except re.error as error:
        raise MCPArgumentError("'{}' is not a valid regular expression: {}".format(name, error))


def parse_iso_argument(value, name):
    """
    Parse an ISO-8601 tool argument into a timezone-aware datetime.

    A timestamp with no offset is assumed to be UTC rather than rejected: the caller is an AI
    model with no way to know Predbat's local timezone, and every history record it will be
    compared against is timezone-aware (str2time never returns a naive datetime), so accepting
    only offset-qualified strings would make the common case - a model asking about "yesterday
    16:00" - fail every time instead of just some of the time.
    """
    if not value or not isinstance(value, str):
        raise MCPArgumentError("'{}' must be an ISO-8601 timestamp, got {!r}".format(name, value))
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MCPArgumentError("'{}' is not a valid ISO-8601 timestamp: {!r}".format(name, value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_bool_argument(value, default=False):
    """
    Coerce an MCP tool argument to a bool, tolerating the strings some clients send.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("false", "0", "no", "off", "")


class PredbatTools:
    """The Predbat tool implementations, shared by the MCP server and the chat agent.

    Holds no protocol state: a caller supplies the base Predbat instance and gets coroutines that
    return plain result dicts, which each surface then wraps in its own envelope.
    """

    def __init__(self, base, log_func=None):
        """Bind the tools to a running Predbat instance."""
        self.base = base
        self.prefix = base.prefix
        self.log = log_func or print
        self.plan_interval_minutes = base.plan_interval_minutes

    async def execute(self, name, arguments):
        """Run one tool by name and return its result dict.

        An unknown name is a result rather than an exception because the caller is a model that
        can only correct itself if the failure comes back through the same channel as a success.
        A genuine handler crash is not caught here - it propagates so the protocol layer (MCP's
        isError, or the chat agent's own try/except) can tell a real fault from a normal failed
        result. Every handler already wraps its own body, so this should never actually fire, but
        that is an unreachability argument, not a contract one, and a swallowed error here would
        make a future handler bug look like a clean tool failure instead of crashing loudly.
        """
        handler = getattr(self, "_execute_{}".format(name), None)
        if handler is None or name not in {entry["name"] for entry in TOOL_DEFS}:
            return {"success": False, "error": "Unknown tool: {}".format(name)}
        try:
            return await handler(arguments or {})
        except MCPArgumentError as error:
            return {"success": False, "error": str(error), "data": None}

    async def _execute_get_plan(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_plan tool"""
        try:
            raw_plan = self.base.get_state_wrapper(self.base.prefix + ".plan_html", attribute="raw", default=None)
            # Check if we have plan data available
            if not raw_plan:
                return {"success": False, "error": "No plan data available", "data": None}

            # Return the complete plan data
            return {"success": True, "error": None, "data": raw_plan, "timestamp": datetime.now().isoformat(), "description": "Current Predbat battery plan including forecasts, costs, and operational states"}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving plan data: {str(e)}", "data": None}

    async def _execute_get_entities(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get current Predbat entities
        """
        try:
            filter = compile_filter_argument(arguments.get("filter", None))
            entities = self.base.dashboard_values
            returned_entities = []
            for entity in entities:
                if isinstance(entity, str):
                    entity_id = entity
                else:
                    entity_id = entity.get("entity_id", "")
                if filter:
                    if not filter.search(entity_id):
                        continue
                if isinstance(entity, str):
                    value = {"entity_id": entity_id}
                else:
                    value = {
                        "entity_id": entity.get("entity_id"),
                        "state": entity.get("state"),
                        "friendly_name": entity.get("friendly_name"),
                    }
                    if "unit_of_measurement" in entity:
                        value["unit_of_measurement"] = entity.get("unit_of_measurement")
                    if "device_class" in entity:
                        value["device_class"] = entity.get("device_class")
                    if "state_class" in entity:
                        value["state_class"] = entity.get("state_class")
                    if "icon" in entity:
                        value["icon"] = entity.get("icon")
                returned_entities.append(value)
            return {"success": True, "error": None, "data": returned_entities, "timestamp": datetime.now().isoformat(), "description": "The current Predbat entities and their states"}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving entities data: {str(e)}", "data": None}

    async def _execute_set_plan_override(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a plan override request
        """
        try:
            action = arguments.get("action", None)
            time_str = arguments.get("time", None)

            if not action or not time_str:
                return {"success": False, "error": "Missing required parameters", "data": None}

            action = action.lower()
            action = action.replace(" ", "_")

            now_utc = self.base.now_utc
            override_time = get_override_time_from_string(now_utc, time_str, self.plan_interval_minutes)
            if not override_time:
                return {"success": False, "error": "Invalid time format. Use 'Day HH:MM' format e.g. Sat 14:30", "data": None}

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 17 * 60:
                return {"success": False, "error": "Override time must be within 17 hours from now.", "data": None}

            selection_option = "{}".format(override_time.strftime("%H:%M:%S"))
            clear_option = "[{}]".format(override_time.strftime("%H:%M:%S"))
            if action == "clear":
                await self.base.async_manual_select("manual_demand", selection_option)
                await self.base.async_manual_select("manual_demand", clear_option)
            else:
                if action == "demand":
                    await self.base.async_manual_select("manual_demand", selection_option)
                elif action == "charge":
                    await self.base.async_manual_select("manual_charge", selection_option)
                elif action == "export":
                    await self.base.async_manual_select("manual_export", selection_option)
                elif action == "freeze_charge":
                    await self.base.async_manual_select("manual_freeze_charge", selection_option)
                elif action == "freeze_export":
                    await self.base.async_manual_select("manual_freeze_export", selection_option)
                else:
                    return {"success": False, "error": "Unknown action {}".format(action), "data": None}

            # Refresh plan
            self.base.update_pending = True
            self.base.plan_valid = False
            return {"success": True, "error": None, "data": {"action": action, "time": override_time.isoformat()}, "timestamp": datetime.now().isoformat(), "description": f"Plan override applied: {action} at {override_time.isoformat()}"}
        except Exception as e:
            return {"success": False, "error": f"Error applying plan override: {str(e)}", "data": None}

    async def _execute_get_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get full HA configuration for Predbat
        """
        try:
            entity_id_filter = compile_filter_argument(arguments.get("filter", None))
            config_return = []
            for item in self.base.CONFIG_ITEMS:
                if entity_id_filter:
                    entity_id = item.get("entity", None)
                    if entity_id and entity_id_filter.search(entity_id):
                        config_return.append(item)
                else:
                    config_return.append(item)
            return {"success": True, "error": None, "data": config_return, "timestamp": datetime.now().isoformat(), "description": "The contents of the Predbat configuration settings"}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving apps.yaml data: {str(e)}", "data": None}

    async def _execute_get_apps(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_apps tool"""
        try:
            # Credentials are redacted unless the caller explicitly opts out - apps.yaml carries
            # API keys, secrets and tokens, and this tool exists to hand the configuration to an
            # AI assistant for review (#4768). Matches the web UI's own apps.yaml download.
            masked = parse_bool_argument(arguments.get("masked", True), default=True)
            configuration = mask_secret_args(self.base.args) if masked else self.base.args
            return_configuration = {}
            config_id_filter = compile_filter_argument(arguments.get("filter", None))
            for key, value in configuration.items():
                if config_id_filter:
                    if config_id_filter.search(key):
                        return_configuration[key] = value
                else:
                    return_configuration[key] = value

            description = "The contents of the Predbat apps.yaml configuration"
            if masked:
                description += " (credential-like values redacted as 'xxx')"
            return {"success": True, "error": None, "data": return_configuration, "masked": masked, "timestamp": datetime.now().isoformat(), "description": description}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving Predbat apps.yaml data: {str(e)}", "data": None}

    @staticmethod
    def _example_child_path(key, value):
        """Build a concrete child path for a container, to show in a description.

        A model that has just read a list of dicts needs to be told the syntax for reaching one
        field inside it, using its own key names rather than an abstract example it has to adapt.
        """
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict) and first:
                return "{}[0].{}".format(key, next(iter(first)))
            return "{}[0]".format(key)
        if isinstance(value, dict) and value:
            return "{}.{}".format(key, next(iter(value)))
        return key

    @staticmethod
    def _join_yaml_path(segments):
        """Rebuild a readable path string from parsed segments, e.g. ['a', '[0]', 'b'] -> a[0].b."""
        out = ""
        for segment in segments:
            if segment.startswith("[") and segment.endswith("]"):
                out += segment
            elif out:
                out += "." + segment
            else:
                out = segment
        return out

    def _describe_available_keys(self, key):
        """Describe what does exist near a path that did not resolve, or return an empty string.

        A bare "not found" leaves a model guessing at both spelling and the path syntax itself.
        Naming what exists at the deepest point that did resolve turns a failed call into a usable
        answer, which matters more here than usual: every wasted call costs a round trip and
        counts against the turn's tool-round budget. Credential names are listed - a name is not
        a value, get_apps already returns the full masked key list, and hiding them would only
        make the model retry a key it cannot see.
        """
        current = self.base.args
        resolved = []
        for segment in parse_yaml_path(key):
            try:
                if segment.startswith("[") and segment.endswith("]"):
                    index = int(segment[1:-1])
                    if not isinstance(current, list) or index >= len(current):
                        break
                    current = current[index]
                else:
                    if not isinstance(current, dict) or segment not in current:
                        break
                    current = current[segment]
                resolved.append(segment)
            except (TypeError, ValueError):
                break

        where = self._join_yaml_path(resolved) or "the top level"
        if isinstance(current, dict) and current:
            names = [str(name) for name in list(current.keys())[:20]]
            return " Available keys at {}: {}.".format(where, ", ".join(names))
        if isinstance(current, list) and current:
            return " '{}' is a list of {} entries - index it, e.g. '{}[0]'.".format(where, len(current), where)
        return ""

    async def _execute_get_apps_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_apps_config tool.

        Reads one apps.yaml key at a time, so a model can read-modify-write it with the chat
        agent's set_apps_config tool (chat_tools.py; not offered here - see that tool's comment
        for why). Unlike get_apps there is no 'masked' bypass at all: get_apps' escape hatch is
        stripped from the chat schema but still reachable (see test_dispatch_strips_chat_omit_
        properties), which is an acceptable trade-off for a full-configuration review tool with an
        explicit opt-out. A single-key read has no equivalent legitimate use for seeing a raw
        credential, so it is simpler and safer to never offer the choice (#4768).
        """
        try:
            key = arguments.get("key")
            if not key or not isinstance(key, str):
                raise MCPArgumentError("'key' must be a non-empty string, got {!r}".format(key))
            # Accepts the same dotted/indexed paths set_apps_config writes, so a model can read
            # back exactly what it just wrote. Without this the two halves disagree: the write
            # succeeds on "forecast_solar[0].azimuth" and the read of the same string fails.
            try:
                value = resolve_nested_yaml_value(self.base.args, key)
            except (KeyError, ValueError):
                return {"success": False, "error": "'{}' was not found in apps.yaml.{}".format(key, self._describe_available_keys(key)), "data": None}

            # Any credential-named segment masks the whole result: for "forecast_solar[0].api_key"
            # the leaf is the credential, and for "ha_key.anything" the parent is.
            masked = any(is_secret_key(segment) for segment in parse_yaml_path(key) if not segment.startswith("["))
            if masked:
                value = SECRET_MASK
            else:
                # No segment is credential-like, but the value may still nest one - forecast_solar
                # is a list of dicts each holding an api_key. mask_secret_args walks the whole
                # structure, so wrap and unwrap to reuse it here ("value" is not a secret name).
                value = mask_secret_args({"value": value})["value"]

            description = "The current value of apps.yaml key '{}'".format(key)
            if masked:
                description += " (credential-like value redacted as '{}')".format(SECRET_MASK)
            elif isinstance(value, (dict, list)):
                description += ". To change one setting inside it, pass a path such as '{}' to set_apps_config rather than writing the whole structure back".format(self._example_child_path(key, value))
            return {"success": True, "error": None, "data": {"key": key, "value": value}, "masked": masked, "timestamp": datetime.now().isoformat(), "description": description}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving apps.yaml key: {str(e)}", "data": None}

    async def _execute_get_state(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_state tool"""
        try:
            requested = arguments.get("keys", None)
            if isinstance(requested, str):
                requested = [requested]
            if requested is not None and not isinstance(requested, list):
                raise MCPArgumentError("'keys' must be a list of state variable names, got {!r}".format(requested))

            key_filter = compile_filter_argument(arguments.get("filter", None))
            max_bytes = parse_number_argument(arguments.get("max_bytes", None), "max_bytes", MCP_STATE_DEFAULT_MAX_BYTES, minimum=1, maximum=MCP_STATE_MAX_BYTES_LIMIT)

            state = {}
            omitted = {}
            total_bytes = 0
            budget_exhausted = False
            unknown_keys = []

            # Snapshot the key list up front - the plan thread can add attributes while we walk it
            available = list(self.base.__dict__.keys())
            if requested is not None:
                unknown_keys = [key for key in requested if key not in available]
                candidates = [key for key in requested if key in available]
            else:
                candidates = available

            for key in candidates:
                # Same filter the debug yaml uses, so this can never return what a debug dump won't
                if is_debug_excluded_key(key):
                    continue
                if key_filter and not key_filter.search(key):
                    continue
                try:
                    value = self.base.__dict__[key]
                except KeyError:
                    continue
                if callable(value):
                    continue
                if key == "args":
                    value = mask_secret_args(value)

                fits, safe, size = measure_state_value(value, max_bytes)
                if not fits:
                    # Too large to return, but say what it is so the caller isn't guessing
                    omitted[key] = summarise_state_value(value)
                    continue
                if total_bytes + size > MCP_STATE_TOTAL_BYTES_LIMIT:
                    budget_exhausted = True
                    omitted[key] = summarise_state_value(value)
                    continue
                state[key] = safe
                total_bytes += size

            data = {
                "state": state,
                "omitted": omitted,
                "returned_keys": len(state),
                "omitted_keys": len(omitted),
                "approx_bytes": total_bytes,
                "max_bytes": max_bytes,
            }
            if unknown_keys:
                data["unknown_keys"] = unknown_keys

            description = "Predbat internal state - {} variables returned".format(len(state))
            if omitted:
                description += ", {} described in 'omitted' instead of being returned in full (ask for one by name, or raise max_bytes)".format(len(omitted))
            if budget_exhausted:
                description += ". The overall response budget was reached, so narrow the request with 'keys' or 'filter'"
            return {"success": True, "error": None, "data": data, "timestamp": datetime.now().isoformat(), "description": description}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving state data: {str(e)}", "data": None}

    async def _execute_get_log(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_log tool"""
        try:
            filter_type = str(arguments.get("filter", "warnings")).lower()
            if filter_type not in LOG_FILTER_TYPES:
                return {"success": False, "error": "Unknown filter '{}', expected one of {}".format(filter_type, ", ".join(LOG_FILTER_TYPES)), "data": None}

            search_term = str(arguments.get("search", "") or "").lower().strip()
            max_lines = parse_number_argument(arguments.get("max_lines", None), "max_lines", MCP_LOG_DEFAULT_LINES, minimum=1, maximum=MCP_LOG_MAX_LINES)
            hours = parse_number_argument(arguments.get("hours", None), "hours", None, minimum=0, as_float=True)

            # Offloaded to an executor: read_predbat_log() does a synchronous open().read() on a
            # file that reaches 10MB before rotation, plus the rotated previous log. The MCP
            # server can afford to block its own thread on that, but the chat agent runs its
            # tools on the web loop, where a multi-second synchronous read would freeze the web
            # server and stop the SSE stream mid-token.
            loop = asyncio.get_running_loop()
            logdata = await loop.run_in_executor(None, read_predbat_log)
            loglines = logdata.split("\n")
            total_lines = len(loglines)

            # Cut-off for the hours filter. Lines with no parseable stamp (tracebacks and other
            # continuation lines) inherit the timestamp of the newer line above them, so a
            # multi-line entry is kept or dropped as a whole.
            cutoff = (datetime.now() - timedelta(hours=hours)) if hours else None
            last_timestamp = None

            result_lines = []
            truncated = False
            matched_lines = 0

            # Walk newest-first so max_lines keeps the most recent entries, as the web log view does
            for lineno in range(total_lines - 1, -1, -1):
                line = loglines[lineno]
                if not line.strip():
                    continue

                timestamp = parse_log_timestamp(line)
                if timestamp:
                    last_timestamp = timestamp
                if cutoff:
                    effective_time = timestamp or last_timestamp
                    if effective_time and effective_time < cutoff:
                        # Everything below this point is older still
                        break

                line_type = classify_log_line(line)
                if not log_line_included(line_type, filter_type):
                    continue
                if search_term and search_term not in line.lower():
                    continue

                matched_lines += 1
                if len(result_lines) >= max_lines:
                    truncated = True
                    continue

                result_lines.append({"line_number": lineno, "type": line_type, "line": line})

            # Return oldest-first, which reads the way a log does
            result_lines.reverse()

            data = {
                "lines": result_lines,
                "total_lines": total_lines,
                "returned_lines": len(result_lines),
                "matched_lines": matched_lines,
                "truncated": truncated,
                "filter": filter_type,
                "search": search_term,
                "hours": hours,
            }
            description = "The Predbat log filtered to '{}' level".format(filter_type)
            if truncated:
                description += ", truncated to the most recent {} of {} matching lines".format(max_lines, matched_lines)
            return {"success": True, "error": None, "data": data, "timestamp": datetime.now().isoformat(), "description": description}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving log data: {str(e)}", "data": None}

    async def _execute_get_status(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_status tool"""

        try:
            debug_enable, _ = self.base.get_ha_config("debug_enable", None)
            read_only, _ = self.base.get_ha_config("set_read_only", None)
            predbat_mode, _ = self.base.get_ha_config("mode", None)
            num_cars, _ = self.base.get_ha_config("num_cars", None)
            status_entity = self.prefix + ".status"
            last_updated = self.base.get_state_wrapper(status_entity, attribute="last_updated", default=None)
            soc_percent = calc_percent_limit(self.base.soc_kw, self.base.soc_max)
            grid_power = self.base.grid_power
            battery_power = self.base.battery_power
            pv_power = self.base.pv_power
            load_power = self.base.load_power
            status_data = {
                "is_running": self.base.is_running(),
                "status": self.base.get_state_wrapper(status_entity),
                "current_soc": self.base.soc_kw,
                "soc_max": self.base.soc_max,
                "soc_percent": soc_percent,
                "reserve": self.base.reserve,
                "mode": predbat_mode,
                "num_cars": num_cars,
                "carbon_enable": self.base.carbon_enable,
                "iboost_enable": self.base.iboost_enable,
                "forecast_minutes": self.base.forecast_minutes,
                "debug_enable": debug_enable,
                "read_only": read_only,
                "last_updated": last_updated,
                "grid_power": grid_power,
                "battery_power": battery_power,
                "pv_power": pv_power,
                "load_power": load_power,
            }

            return {"success": True, "error": None, "data": status_data, "timestamp": datetime.now().isoformat()}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving status: {str(e)}", "data": None}

    async def _execute_set_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the set_config tool"""
        try:
            entity_id = arguments.get("entity_id")
            value = arguments.get("value")

            if not entity_id or value is None:
                return {"success": False, "error": "Both 'entity_id' and 'value' must be provided", "data": None}

            # Update the configuration setting
            await self.base.ha_interface.set_state_external(entity_id, value)

            return {"success": True, "error": None, "data": {"entity_id": entity_id, "new_value": value}, "timestamp": datetime.now().isoformat(), "description": f"Configuration setting '{entity_id}' updated successfully"}

        except Exception as e:
            return {"success": False, "error": f"Error setting configuration: {str(e)}", "data": None}

    def _ha_state_access_enabled(self):
        """
        Return whether switch.predbat_ai_ha_state_enable currently allows the HA-state tools to run.

        Read at the moment each tool is called, not cached, so toggling the switch takes effect on
        the very next call - the same reasoning ChatAgent.confirm_writes_enabled() gives for
        chat_confirm_writes. Unlike that switch, this one is prefixed 'ai_', not 'chat_': it gates
        every AI surface (MCP included), not just the Chat tab, because reading arbitrary Home
        Assistant state - not just Predbat's own entities - is a materially larger disclosure to
        whichever third-party model is asking, and MCP clients are no less a third party than chat.
        """
        value, _ = self.base.get_ha_config("ai_ha_state_enable", False)
        return bool(value)

    def _ha_state_access_denied(self):
        """
        Return the standard denial result for an HA-state tool when its switch is off.

        Names the switch rather than just saying "disabled": a model that can tell the user which
        switch to turn on is more useful than one whose tool has silently vanished from its list -
        which is also why these tools stay in TOOL_DEFS unconditionally rather than being filtered
        out when the switch is off.
        """
        return {"success": False, "error": "Home Assistant state access is disabled. Enable switch.predbat_ai_ha_state_enable to allow it.", "data": None}

    async def _execute_search_entities(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the search_entities tool"""
        try:
            if not self._ha_state_access_enabled():
                return self._ha_state_access_denied()

            pattern = compile_filter_argument(arguments.get("pattern", None), name="pattern")
            if pattern is None:
                raise MCPArgumentError("'pattern' is required")
            limit = parse_number_argument(arguments.get("limit", None), "limit", MCP_ENTITY_SEARCH_DEFAULT_LIMIT, minimum=1, maximum=MCP_ENTITY_SEARCH_MAX_LIMIT)

            # No attributes in the result - a real install can carry thousands of entities, and
            # their attribute dicts are the bulky part. get_entity_state exists precisely so a
            # caller that has found the one entity it cares about can ask for those separately.
            all_state = self.base.ha_interface.get_state() or {}
            matches = []
            total_matches = 0
            for entity_id, item in all_state.items():
                if not pattern.search(entity_id):
                    continue
                total_matches += 1
                if len(matches) < limit:
                    matches.append({"entity_id": entity_id, "state": item.get("state"), "last_changed": item.get("last_changed")})

            data = {"entities": matches, "total_matches": total_matches, "returned": len(matches), "limit": limit}
            description = "{} of {} matching Home Assistant entities returned".format(len(matches), total_matches)
            if total_matches > len(matches):
                description += " - truncated to 'limit'; raise it or narrow 'pattern' to see the rest"
            return {"success": True, "error": None, "data": data, "timestamp": datetime.now().isoformat(), "description": description}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error searching entities: {str(e)}", "data": None}

    async def _execute_get_entity_state(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_entity_state tool"""
        try:
            if not self._ha_state_access_enabled():
                return self._ha_state_access_denied()

            entity_id = arguments.get("entity_id", None)
            if not entity_id or not isinstance(entity_id, str):
                raise MCPArgumentError("'entity_id' must be a non-empty string, got {!r}".format(entity_id))
            include_attributes = parse_bool_argument(arguments.get("attributes", False), default=False)

            all_state = self.base.ha_interface.get_state() or {}
            item = all_state.get(entity_id)
            if item is None:
                # A clean result, not an exception - an unknown entity id is a normal wrong guess
                # for a model exploring an install it cannot see the entity registry of directly.
                return {"success": False, "error": "Unknown entity: {}".format(entity_id), "data": None}

            data = {"entity_id": entity_id, "state": item.get("state"), "last_changed": item.get("last_changed")}
            if include_attributes:
                data["attributes"] = item.get("attributes", {})
            return {"success": True, "error": None, "data": data, "timestamp": datetime.now().isoformat(), "description": "Current state of {}".format(entity_id)}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving entity state: {str(e)}", "data": None}

    def _bucket_entity_history(self, windowed_records, start_dt, bucket_minutes, bucket_count, attribute, numeric):
        """
        Aggregate (timestamp, record) pairs already sorted and filtered to the request window into
        fixed-width time buckets.

        The mode (numeric vs text) is decided once by the caller, over the whole window, and passed
        in rather than re-derived per record or per bucket - so every bucket in the response has the
        same shape and a consumer never has to detect a format change partway down the list.

        Numeric buckets report min/max/mean/count plus how many samples were unavailable, so "the
        mean looks fine but the sensor was dead for half the window" stays visible instead of being
        silently averaged away. Text buckets deliberately do not report a most-common value: a
        sensor that sat on 'off' for 28 of 30 minutes then flipped to 'on' would report as 'off' and
        lose the one thing that mattered - so they report first/last/changes instead, which is what
        it was, what it became, and whether it flapped in between.
        """
        buckets = [{"start": (start_dt + timedelta(minutes=index * bucket_minutes)).isoformat()} for index in range(bucket_count)]
        if numeric:
            for bucket in buckets:
                bucket.update({"min": None, "max": None, "mean": None, "count": 0, "unavailable": 0})
            sums = [0.0] * bucket_count
        else:
            for bucket in buckets:
                bucket.update({"first": None, "last": None, "changes": 0})

        bucket_seconds = bucket_minutes * 60
        for record_time, record in windowed_records:
            index = int((record_time - start_dt).total_seconds() // bucket_seconds)
            if index < 0 or index >= bucket_count:
                continue
            bucket = buckets[index]

            raw_value = record.get("attributes", {}).get(attribute) if attribute else record.get("state")
            if raw_value is None:
                if numeric:
                    bucket["unavailable"] += 1
                continue
            text_value = str(raw_value)
            low_value = text_value.strip().lower()
            if low_value in ("unavailable", "unknown"):
                if numeric:
                    bucket["unavailable"] += 1
                continue

            if numeric:
                if low_value in ("on", "true"):
                    numeric_value = 1.0
                elif low_value in ("off", "false"):
                    numeric_value = 0.0
                else:
                    try:
                        numeric_value = float(raw_value)
                    except (TypeError, ValueError):
                        bucket["unavailable"] += 1
                        continue
                sums[index] += numeric_value
                bucket["count"] += 1
                bucket["min"] = numeric_value if bucket["min"] is None else min(bucket["min"], numeric_value)
                bucket["max"] = numeric_value if bucket["max"] is None else max(bucket["max"], numeric_value)
            else:
                if bucket["first"] is None:
                    bucket["first"] = text_value
                elif text_value != bucket["last"]:
                    bucket["changes"] += 1
                bucket["last"] = text_value

        if numeric:
            for index, bucket in enumerate(buckets):
                if bucket["count"]:
                    bucket["mean"] = sums[index] / bucket["count"]

        return buckets

    async def _execute_get_entity_history(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_entity_history tool"""
        try:
            if not self._ha_state_access_enabled():
                return self._ha_state_access_denied()

            entity_id = arguments.get("entity_id", None)
            if not entity_id or not isinstance(entity_id, str):
                raise MCPArgumentError("'entity_id' must be a non-empty string, got {!r}".format(entity_id))

            attribute = arguments.get("attribute", None)
            if attribute is not None and not isinstance(attribute, str):
                raise MCPArgumentError("'attribute' must be a string, got {!r}".format(attribute))

            start_dt = parse_iso_argument(arguments.get("start", None), "start")
            end_dt = parse_iso_argument(arguments.get("end", None), "end")
            if end_dt <= start_dt:
                raise MCPArgumentError("'end' must be after 'start'")

            bucket_minutes = parse_number_argument(arguments.get("bucket_minutes", None), "bucket_minutes", MCP_HISTORY_DEFAULT_BUCKET_MINUTES, minimum=1)

            # Cap the lookback before the fetch - this bounds what get_history_wrapper is asked
            # for, distinct from the bucket cap below, which bounds what this tool hands back.
            lookback_clamped = False
            earliest_allowed = end_dt - timedelta(days=MCP_HISTORY_MAX_LOOKBACK_DAYS)
            if start_dt < earliest_allowed:
                start_dt = earliest_allowed
                lookback_clamped = True

            # Cap the bucket count by shrinking the window rather than widening the buckets - a
            # caller that asked for fine-grained buckets over too long a window presumably wants
            # fewer, fine buckets, not the same count merged into coarser ones.
            range_truncated = False
            total_minutes = (end_dt - start_dt).total_seconds() / 60
            bucket_count = max(1, math.ceil(total_minutes / bucket_minutes))
            if bucket_count > MCP_HISTORY_MAX_BUCKETS:
                end_dt = start_dt + timedelta(minutes=MCP_HISTORY_MAX_BUCKETS * bucket_minutes)
                bucket_count = MCP_HISTORY_MAX_BUCKETS
                range_truncated = True

            now_reference = getattr(self.base, "now_utc", None) or datetime.now(timezone.utc)
            days_needed = max(1, min((now_reference - start_dt).days + 2, MCP_HISTORY_MAX_LOOKBACK_DAYS + 2))

            # tracked=False: an ad hoc lookup of whatever entity the caller names, not one of
            # Predbat's own tracked series. tracked=True would register it in HAHistory's
            # history_entities and have Predbat re-fetch and cache it forever after a single
            # question - the web /entity page's own arbitrary-entity history fetch
            # (get_history_with_now in web.py) makes the same tracked=False choice for the same
            # reason: a one-off browse of any HA entity must not become a permanent subscription.
            # Offloaded for the same reason get_log is: get_history_wrapper reaches
            # HAInterface.get_history, which chunks the window at HISTORY_CHUNK_DAYS and issues
            # one synchronous requests.get per chunk, each with TIMEOUT (300s) to spare. A 30-day
            # window is up to 11 sequential blocking fetches of potentially tens of megabytes,
            # and tracked=False means none of it is cached, so every call refetches. Run on the
            # event loop that would freeze the component - and with it every chat and MCP request
            # awaiting run_on_agent_loop - for the whole fetch.
            loop = asyncio.get_running_loop()
            history = await loop.run_in_executor(None, functools.partial(self.base.get_history_wrapper, entity_id, days=days_needed, required=False, tracked=False))
            records = history[0] if history and history[0] else []

            windowed_records = []
            for record in records:
                stamp = record.get("last_updated")
                if not stamp:
                    continue
                try:
                    record_time = str2time(stamp) if isinstance(stamp, str) else stamp
                except (ValueError, TypeError):
                    continue
                if record_time < start_dt or record_time > end_dt:
                    continue
                windowed_records.append((record_time, record))
            windowed_records.sort(key=lambda pair: pair[0])

            # Classified once, over the whole filtered window, so a consumer never sees the
            # bucket shape change partway down the response.
            numeric = is_data_numerical([[record for _, record in windowed_records]], attribute=attribute)
            mode = "numeric" if numeric else "text"

            buckets = self._bucket_entity_history(windowed_records, start_dt, bucket_minutes, bucket_count, attribute, numeric)

            data = {
                "entity_id": entity_id,
                "attribute": attribute,
                "mode": mode,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "bucket_minutes": bucket_minutes,
                "bucket_count": len(buckets),
                "record_count": len(windowed_records),
                "buckets": buckets,
                "lookback_clamped": lookback_clamped,
                "range_truncated": range_truncated,
            }
            description = "{} history for {} bucketed into {} x {}-minute {} buckets".format(attribute or "state", entity_id, len(buckets), bucket_minutes, mode)
            if lookback_clamped:
                description += "; 'start' was clamped to the {}-day maximum lookback".format(MCP_HISTORY_MAX_LOOKBACK_DAYS)
            if range_truncated:
                description += "; 'end' was pulled in to keep the bucket count at or under {}".format(MCP_HISTORY_MAX_BUCKETS)
            return {"success": True, "error": None, "data": data, "timestamp": datetime.now().isoformat(), "description": description}

        except MCPArgumentError as e:
            return {"success": False, "error": str(e), "data": None}
        except Exception as e:
            return {"success": False, "error": f"Error retrieving entity history: {str(e)}", "data": None}


TOOL_DEFS = [
    {"name": "get_plan", "description": "Get the current Predbat battery plan data including forecast, costs, and state information", "parameters": {"type": "object", "properties": {}, "required": []}, "writes": False, "chat_omit_properties": []},
    {"name": "get_status", "description": "Get the current Predbat system status and configuration", "parameters": {"type": "object", "properties": {}, "required": []}, "writes": False, "chat_omit_properties": []},
    # get_apps: 'masked' is stripped from the chat projection so a model cannot ask for
    # unmasked credentials and send them to a third-party provider. See spec section 14.1.
    {
        "name": "get_apps",
        "description": "Get predbat apps.yaml static configuration data, with credentials redacted by default",
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "The configuration item name to filter on, as a Python regex (optional)"},
                "masked": {"type": "boolean", "description": "Redact credential-like values such as API keys, secrets and passwords (default true)"},
            },
            "required": [],
        },
        "writes": False,
        "chat_omit_properties": ["masked"],
    },
    {
        "name": "get_apps_config",
        "description": "Get the current value of one apps.yaml key, with a credential-like value redacted. Use before set_apps_config to read the value you are about to change. Accepts the same dotted paths set_apps_config writes, so you can read back exactly what you wrote.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The apps.yaml key to read. Use a dotted path to reach inside a nested structure, e.g. 'forecast_solar[0].azimuth'. Call get_apps first, or read the parent key, to see what the structure contains. If a path does not exist the error lists the keys that do.",
                }
            },
            "required": ["key"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "get_state",
        "description": "Get Predbat's internal state variables - the same data a debug yaml carries, one key at a time. Called with no arguments it returns every small variable and describes the large ones (per-minute series) without returning them.",
        "parameters": {
            "type": "object",
            "properties": {
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Specific state variable names to return (optional - omit for every small variable)"},
                "filter": {"type": "string", "description": "Only return variables whose name matches this Python regex (optional)"},
                "max_bytes": {"type": "integer", "description": "Per-variable size budget before it is described rather than returned (default {}, maximum {})".format(MCP_STATE_DEFAULT_MAX_BYTES, MCP_STATE_MAX_BYTES_LIMIT)},
            },
            "required": [],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "get_log",
        "description": "Get the Predbat log (predbat.log), filtered by level, search term and age - use this to diagnose warnings and errors. Lines are returned oldest-first.",
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {"type": "string", "description": "Log level to return: all, info, warnings or errors (default warnings)", "enum": list(LOG_FILTER_TYPES)},
                "search": {"type": "string", "description": "Only return lines containing this text, case-insensitive (optional)"},
                "hours": {"type": "number", "description": "Only return lines written in the last N hours (optional)"},
                "max_lines": {
                    "type": "integer",
                    "description": "Maximum number of lines to return. The most recent matching lines are the ones kept, but they are returned oldest-first (default {}, maximum {})".format(MCP_LOG_DEFAULT_LINES, MCP_LOG_MAX_LINES),
                },
            },
            "required": [],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "get_config",
        "description": "Get the current Predbat live configuration settings",
        "parameters": {"type": "object", "properties": {"filter": {"type": "string", "description": "The entity ID name to filter on, as a Python regex (optional)"}}, "required": []},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "get_entities",
        "description": "Get the current Predbat entities",
        "parameters": {"type": "object", "properties": {"filter": {"type": "string", "description": "The configuration item name to filter on, as a Python regex (optional)"}}, "required": []},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "set_config",
        "description": "Set Predbat configuration setting",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string", "description": "The entity ID of the configuration setting to update"}, "value": {"type": "string", "description": "The new value for the configuration setting"}},
            "required": ["entity_id", "value"],
        },
        "writes": True,
        "chat_omit_properties": [],
    },
    {
        "name": "set_plan_override",
        "description": "Override the current Predbat plan for a specific 30 minute period with a manual action",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "The action to perform: demand, charge, export, freeze_charge, freeze_export, clear"},
                "time": {"type": "string", "description": 'The time at which to perform the action, in "Day HH:MM" format (24-hour), covers one 30-minute period'},
            },
            "required": ["action", "time"],
        },
        "writes": True,
        "chat_omit_properties": [],
    },
    # search_entities/get_entity_state/get_entity_history: unlike every other tool above, these
    # read arbitrary Home Assistant state - not just Predbat's own entities - so they are gated
    # behind switch.predbat_ai_ha_state_enable (off by default) rather than always available. The
    # gate is enforced inside each handler, not by omitting these from TOOL_DEFS, so a model that
    # calls one while the switch is off is told which switch to enable rather than finding the
    # tool silently missing.
    {
        "name": "search_entities",
        "description": "Search every Home Assistant entity id with a regular expression - not just Predbat's own. Requires switch.predbat_ai_ha_state_enable.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression matched against entity ids"},
                "limit": {"type": "integer", "description": "Maximum number of matches to return (default {}, maximum {})".format(MCP_ENTITY_SEARCH_DEFAULT_LIMIT, MCP_ENTITY_SEARCH_MAX_LIMIT)},
            },
            "required": ["pattern"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "get_entity_state",
        "description": "Get one Home Assistant entity's current state - not just Predbat's own. Requires switch.predbat_ai_ha_state_enable.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "The entity id to look up, e.g. binary_sensor.front_door"},
                "attributes": {"type": "boolean", "description": "Include the entity's attribute dict as well as its state (default false) - kept separate from search_entities because attribute dicts are bulky"},
            },
            "required": ["entity_id"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "get_entity_history",
        "description": "Get one Home Assistant entity's history over a time window, bucketed into fixed-width time slots - not just Predbat's own. Requires switch.predbat_ai_ha_state_enable.",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "The entity id to fetch history for"},
                "start": {"type": "string", "description": "Start of the window, an ISO-8601 timestamp (assumed UTC if it carries no offset). Clamped to at most {} days before 'end'".format(MCP_HISTORY_MAX_LOOKBACK_DAYS)},
                "end": {"type": "string", "description": "End of the window, an ISO-8601 timestamp (assumed UTC if it carries no offset)"},
                "bucket_minutes": {
                    "type": "integer",
                    "description": "Width of each bucket in minutes (default {}). The window is pulled in, not the bucket width, if this would exceed {} buckets".format(MCP_HISTORY_DEFAULT_BUCKET_MINUTES, MCP_HISTORY_MAX_BUCKETS),
                },
                "attribute": {"type": "string", "description": "Bucket this attribute's value instead of the entity's state (optional - defaults to the entity's state)"},
            },
            "required": ["entity_id", "start", "end"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
]


def mcp_tool_list():
    """Project TOOL_DEFS into the MCP tools/list shape."""
    return [{"name": entry["name"], "description": entry["description"], "inputSchema": entry["parameters"]} for entry in TOOL_DEFS]


def openai_tool_list(defs=None):
    """Project a tool definition list into OpenAI function-calling shape.

    Properties named in ``chat_omit_properties`` are dropped, so a schema can offer an argument
    over MCP that the chat agent must not be able to express.
    """
    projected = []
    for entry in defs if defs is not None else TOOL_DEFS:
        parameters = json.loads(json.dumps(entry["parameters"]))
        omit = entry.get("chat_omit_properties") or []
        if omit:
            properties = parameters.get("properties", {})
            for name in omit:
                properties.pop(name, None)
            parameters["required"] = [name for name in parameters.get("required", []) if name not in omit]
        projected.append({"type": "function", "function": {"name": entry["name"], "description": entry["description"], "parameters": parameters}})
    return projected

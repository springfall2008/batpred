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
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict
from utils import calc_percent_limit, get_override_time_from_string, mask_secret_args, read_predbat_log, classify_log_line, log_line_included, parse_log_timestamp, is_debug_excluded_key


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


def compile_filter_argument(value, name="filter"):
    """
    Compile a regex tool argument, or return None when no filter was given.

    Compiling up front turns an invalid pattern into a named argument error instead of a bare
    Python traceback, and avoids re-compiling it for every entry the caller filters over.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MCPArgumentError("'{}' must be a string regular expression, got {!r}".format(name, value))
    try:
        return re.compile(value)
    except re.error as error:
        raise MCPArgumentError("'{}' is not a valid regular expression: {}".format(name, error))


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

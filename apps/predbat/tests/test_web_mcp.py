# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the MCP server's log and apps.yaml tools (#4768).

Covers the new get_log tool, get_apps' credential redaction, the widened
mask_secret_args() key match, and the log-filter helpers now shared between the
MCP tool and the web log view.
"""

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta

import web
import web_mcp
from web import WebInterface
from web_mcp import (
    MCPServerWrapper,
    LOG_FILTER_TYPES,
    MCP_LOG_DEFAULT_LINES,
    MCP_LOG_MAX_LINES,
    parse_bool_argument,
    json_safe_value,
    summarise_state_value,
    measure_state_value,
    MCP_STATE_DEFAULT_MAX_BYTES,
    MCP_STATE_LARGE_COLLECTION,
    MCP_STATE_MAX_BYTES_LIMIT,
)
from utils import mask_secret_args, is_secret_key, is_debug_excluded_key, read_predbat_log, classify_log_line, log_line_included, parse_log_timestamp


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing only the query string a handler reads."""

    def __init__(self, query=None):
        """Store the query string a handler will read."""
        self.query = query or {}


def _stamp(minutes_ago):
    """Return a predbat.log-style timestamp prefix for a line written N minutes ago."""
    return "{}: ".format(datetime.now() - timedelta(minutes=minutes_ago))


def _sample_log():
    """Return a small synthetic predbat.log, newest line last, spanning three days."""
    lines = [
        _stamp(60 * 48) + "Info: Predbat starting up two days ago",
        _stamp(60 * 48) + "Warn: An old warning from before the window",
        _stamp(60 * 3) + "Info: Fetched Octopus rates",
        _stamp(60 * 2) + "Warn: battery_rate_max_charge clamped to 3.0",
        _stamp(60) + "Error: Failed to write charge window to inverter",
        "Traceback (most recent call last):",  # a continuation line, written without its own stamp
        _stamp(30) + "Fetched 48 slots of solar forecast",
        _stamp(10) + "Warn: iboost target not reached",
    ]
    return "\n".join(lines) + "\n"


def _make_mcp(my_predbat):
    """Build an MCPServerWrapper bound to my_predbat without starting any server."""
    return MCPServerWrapper(my_predbat, log_func=my_predbat.log)


def _make_web(my_predbat):
    """Build a minimal WebInterface bound to my_predbat, bypassing ComponentBase.__init__
    (which would stand up the real aiohttp app) - same pattern as test_web_debug_history_routes.py.
    """
    w = WebInterface.__new__(WebInterface)
    w.base = my_predbat
    w.log = my_predbat.log
    w.prefix = my_predbat.prefix
    return w


def _call_tool(mcp, name, arguments=None):
    """Run one tool through the MCP tools/call path and return the decoded JSON result."""
    result = asyncio.run(mcp._handle_tools_call({"name": name, "arguments": arguments or {}}))
    return json.loads(result["content"][0]["text"]), result.get("isError", False)


def test_mask_secret_args(my_predbat):
    """Credential-like apps.yaml keys are redacted, including the *_secret and *_token names
    the original "_key"/"password" match missed (#4768), while timestamps stay readable.
    """
    failed = False
    print("**** Testing mask_secret_args key matching ****")

    args = {
        "ha_key": "ha-key-value",
        "octopus_api_key": "octopus-key-value",
        "deye_password": "deye-password-value",
        "sigenergy_app_secret": "sigenergy-secret-value",
        "solis_api_secret": "solis-secret-value",
        "solis_access_token": "solis-token-value",
        "gateway_mqtt_token": "mqtt-token-value",
        "mcp_secret": "mcp-secret-value",
        "solis_token_expires_at": "2026-08-27T09:00:00",
        "fox_token_expires_at": "2026-08-27T09:00:00",
        "battery_rate_max_charge": 3.0,
        "inverter_type": "GE",
        "keyword_notes": "not a credential",
    }
    masked = mask_secret_args(args)

    for key in ["ha_key", "octopus_api_key", "deye_password", "sigenergy_app_secret", "solis_api_secret", "solis_access_token", "gateway_mqtt_token", "mcp_secret"]:
        if masked.get(key) != "xxx":
            print("  ERROR: expected {} to be masked, got {!r}".format(key, masked.get(key)))
            failed = True

    for key in ["solis_token_expires_at", "fox_token_expires_at", "battery_rate_max_charge", "inverter_type", "keyword_notes"]:
        if masked.get(key) != args[key]:
            print("  ERROR: expected {} to be left alone, got {!r}".format(key, masked.get(key)))
            failed = True

    # The original dict must not be touched - it is the live self.args
    if args["ha_key"] != "ha-key-value":
        print("  ERROR: mask_secret_args mutated the caller's dict")
        failed = True

    if is_secret_key("SOLIS_API_SECRET") is not True:
        print("  ERROR: is_secret_key should be case-insensitive")
        failed = True
    if is_secret_key("forecast_hours") is not False:
        print("  ERROR: is_secret_key matched a non-credential key")
        failed = True

    return failed


def test_read_predbat_log(my_predbat):
    """read_predbat_log() concatenates the rotated log ahead of the live one, and copes with
    either or both files being absent.
    """
    failed = False
    print("**** Testing read_predbat_log ****")

    tmpdir = tempfile.mkdtemp(prefix="predbat_test_mcp_log_")
    live = os.path.join(tmpdir, "predbat.log")
    prev = os.path.join(tmpdir, "predbat.1.log")

    if read_predbat_log(logfile=live, logfile_prev=prev) != "":
        print("  ERROR: expected an empty string when neither log file exists")
        failed = True

    with open(live, "w") as f:
        f.write("newer\n")
    if read_predbat_log(logfile=live, logfile_prev=prev) != "newer\n":
        print("  ERROR: expected only the live log when no rotated log exists")
        failed = True

    with open(prev, "w") as f:
        f.write("older\n")
    data = read_predbat_log(logfile=live, logfile_prev=prev)
    if data != "older\n\nnewer\n":
        print("  ERROR: expected the rotated log first, got {!r}".format(data))
        failed = True

    return failed


def test_log_filter_helpers(my_predbat):
    """The severity classifier and per-view include rules keep the semantics the web log view
    had inline before they were shared with the MCP get_log tool.
    """
    failed = False
    print("**** Testing log filter helpers ****")

    cases = [
        ("2026-08-27 09:00:00.000000: Error: something broke", "error"),
        ("2026-08-27 09:00:00.000000: Warn: something odd", "warning"),
        ("2026-08-27 09:00:00.000000: Info: something happened", "info"),
        ("2026-08-27 09:00:00.000000: just a line", "log"),
    ]
    for line, expected in cases:
        line_type = classify_log_line(line)
        if line_type != expected:
            print("  ERROR: expected {!r} to classify as {}, got {}".format(line, expected, line_type))
            failed = True

    # Errors show everywhere; warnings on all/warnings; info on all/info; plain lines on all only
    expected_matrix = {
        ("error", "all"): True,
        ("error", "info"): True,
        ("error", "warnings"): True,
        ("error", "errors"): True,
        ("warning", "all"): True,
        ("warning", "warnings"): True,
        ("warning", "info"): False,
        ("warning", "errors"): False,
        ("info", "all"): True,
        ("info", "info"): True,
        ("info", "warnings"): False,
        ("info", "errors"): False,
        ("log", "all"): True,
        ("log", "info"): False,
        ("log", "warnings"): False,
        ("log", "errors"): False,
    }
    for (line_type, filter_type), expected in expected_matrix.items():
        got = log_line_included(line_type, filter_type)
        if got != expected:
            print("  ERROR: log_line_included({}, {}) expected {}, got {}".format(line_type, filter_type, expected, got))
            failed = True

    stamped = parse_log_timestamp("2026-08-27 09:15:30.123456: Info: hello")
    if stamped != datetime(2026, 8, 27, 9, 15, 30, 123456):
        print("  ERROR: failed to parse a microsecond timestamp, got {!r}".format(stamped))
        failed = True

    # str(datetime) drops ".000000", so whole-second lines are 19 characters not 26
    stamped = parse_log_timestamp("2026-08-27 09:15:30: Info: hello")
    if stamped != datetime(2026, 8, 27, 9, 15, 30):
        print("  ERROR: failed to parse a whole-second timestamp, got {!r}".format(stamped))
        failed = True

    if parse_log_timestamp("Traceback (most recent call last):") is not None:
        print("  ERROR: expected None for a line with no timestamp")
        failed = True
    if parse_log_timestamp("") is not None:
        print("  ERROR: expected None for an empty line")
        failed = True

    return failed


def test_mcp_get_apps(my_predbat):
    """get_apps redacts credentials by default, honours an explicit masked=false opt-out, still
    filters by regex, and never mutates the live args (#4768).
    """
    failed = False
    print("**** Testing MCP get_apps redaction ****")

    mcp = _make_mcp(my_predbat)
    saved_args = my_predbat.args
    try:
        my_predbat.args = {"ha_key": "supersecret", "mcp_secret": "mcp-secret-value", "battery_rate_max_charge": 3.0, "inverter_type": "GE"}

        result, _ = _call_tool(mcp, "get_apps")
        if not result.get("success"):
            print("  ERROR: get_apps failed: {}".format(result.get("error")))
            failed = True
        data = result.get("data") or {}
        if data.get("ha_key") != "xxx" or data.get("mcp_secret") != "xxx":
            print("  ERROR: expected credentials to be masked by default, got {!r}".format(data))
            failed = True
        if data.get("battery_rate_max_charge") != 3.0:
            print("  ERROR: masking should leave ordinary settings alone, got {!r}".format(data.get("battery_rate_max_charge")))
            failed = True
        if result.get("masked") is not True:
            print("  ERROR: expected the response to report masked=True")
            failed = True
        if my_predbat.args["ha_key"] != "supersecret":
            print("  ERROR: get_apps mutated the live args")
            failed = True

        print("Test: masked=false returns the raw values")
        result, _ = _call_tool(mcp, "get_apps", {"masked": False})
        if (result.get("data") or {}).get("ha_key") != "supersecret":
            print("  ERROR: expected the unmasked key with masked=false, got {!r}".format(result.get("data")))
            failed = True
        if result.get("masked") is not False:
            print("  ERROR: expected the response to report masked=False")
            failed = True

        print("Test: a string 'false' from a client that can't send booleans is honoured")
        result, _ = _call_tool(mcp, "get_apps", {"masked": "false"})
        if (result.get("data") or {}).get("ha_key") != "supersecret":
            print("  ERROR: expected string 'false' to disable masking")
            failed = True

        print("Test: the filter regex still applies, and its results are masked too")
        result, _ = _call_tool(mcp, "get_apps", {"filter": "^ha_key$"})
        data = result.get("data") or {}
        if list(data.keys()) != ["ha_key"] or data.get("ha_key") != "xxx":
            print("  ERROR: expected only a masked ha_key, got {!r}".format(data))
            failed = True
    finally:
        my_predbat.args = saved_args

    if parse_bool_argument(None, default=True) is not True:
        print("  ERROR: parse_bool_argument should fall back to its default for None")
        failed = True
    for value in [False, "false", "False", "0", "no", "off", ""]:
        if parse_bool_argument(value, default=True) is not False:
            print("  ERROR: parse_bool_argument({!r}) should be False".format(value))
            failed = True
    for value in [True, "true", "1", 1, "yes"]:
        if parse_bool_argument(value, default=False) is not True:
            print("  ERROR: parse_bool_argument({!r}) should be True".format(value))
            failed = True

    return failed


def test_mcp_get_log(my_predbat):
    """get_log filters by level, search term and age, caps the number of lines returned, and
    reports the log oldest-first (#4768).
    """
    failed = False
    print("**** Testing MCP get_log ****")

    mcp = _make_mcp(my_predbat)
    saved_reader = web_mcp.read_predbat_log
    try:
        web_mcp.read_predbat_log = lambda: _sample_log()

        print("Test: the default warnings view returns warnings and errors only")
        result, _ = _call_tool(mcp, "get_log")
        if not result.get("success"):
            print("  ERROR: get_log failed: {}".format(result.get("error")))
            return True
        data = result["data"]
        types = [line["type"] for line in data["lines"]]
        if sorted(set(types)) != ["error", "warning"]:
            print("  ERROR: expected only warnings and errors by default, got {}".format(sorted(set(types))))
            failed = True
        if data["filter"] != "warnings":
            print("  ERROR: expected the default filter to be warnings, got {}".format(data["filter"]))
            failed = True

        print("Test: lines come back oldest-first")
        stamps = [parse_log_timestamp(line["line"]) for line in data["lines"]]
        if stamps != sorted(stamps):
            print("  ERROR: expected oldest-first ordering, got {}".format(stamps))
            failed = True

        print("Test: the errors view drops warnings")
        result, _ = _call_tool(mcp, "get_log", {"filter": "errors"})
        types = [line["type"] for line in result["data"]["lines"]]
        if types != ["error"]:
            print("  ERROR: expected a single error line, got {}".format(types))
            failed = True

        print("Test: the all view includes untyped lines such as tracebacks")
        result, _ = _call_tool(mcp, "get_log", {"filter": "all"})
        if not any(line["type"] == "log" for line in result["data"]["lines"]):
            print("  ERROR: expected plain log lines in the all view")
            failed = True
        if result["data"]["returned_lines"] != 8:
            print("  ERROR: expected all 8 sample lines, got {}".format(result["data"]["returned_lines"]))
            failed = True

        print("Test: search narrows to matching lines, case-insensitively")
        result, _ = _call_tool(mcp, "get_log", {"filter": "all", "search": "IBOOST"})
        lines = result["data"]["lines"]
        if len(lines) != 1 or "iboost" not in lines[0]["line"]:
            print("  ERROR: expected one iboost line, got {}".format(lines))
            failed = True

        print("Test: hours drops entries older than the window")
        result, _ = _call_tool(mcp, "get_log", {"filter": "all", "hours": 4})
        lines = result["data"]["lines"]
        if any("two days ago" in line["line"] or "before the window" in line["line"] for line in lines):
            print("  ERROR: expected the two-day-old lines to be excluded, got {}".format([line["line"] for line in lines]))
            failed = True
        if len(lines) != 6:
            print("  ERROR: expected 6 lines within 4 hours, got {}".format(len(lines)))
            failed = True

        print("Test: a traceback line with no timestamp of its own is kept with the entry above it")
        if not any("Traceback" in line["line"] for line in lines):
            print("  ERROR: expected the traceback continuation line to be retained")
            failed = True

        print("Test: max_lines keeps the most recent lines and flags the truncation")
        result, _ = _call_tool(mcp, "get_log", {"filter": "all", "max_lines": 2})
        data = result["data"]
        if data["returned_lines"] != 2 or not data["truncated"]:
            print("  ERROR: expected 2 truncated lines, got {}".format(data))
            failed = True
        if data["matched_lines"] != 8:
            print("  ERROR: expected matched_lines to count all 8 matches, got {}".format(data["matched_lines"]))
            failed = True
        if "iboost" not in data["lines"][-1]["line"]:
            print("  ERROR: expected the newest line to survive truncation, got {}".format(data["lines"][-1]["line"]))
            failed = True

        print("Test: max_lines is clamped to the protocol cap and to at least one line")
        result, _ = _call_tool(mcp, "get_log", {"filter": "all", "max_lines": MCP_LOG_MAX_LINES * 100})
        if result["data"]["truncated"]:
            print("  ERROR: an oversized max_lines should still return the whole sample log")
            failed = True
        result, _ = _call_tool(mcp, "get_log", {"filter": "all", "max_lines": 0})
        if result["data"]["returned_lines"] != 1:
            print("  ERROR: expected max_lines=0 to be clamped up to 1, got {}".format(result["data"]["returned_lines"]))
            failed = True

        print("Test: an unknown filter is rejected rather than silently returning everything")
        result, _ = _call_tool(mcp, "get_log", {"filter": "everything"})
        if result.get("success") or "everything" not in (result.get("error") or ""):
            print("  ERROR: expected an error naming the bad filter, got {}".format(result))
            failed = True

        print("Test: an empty log is reported as success with no lines")
        web_mcp.read_predbat_log = lambda: ""
        result, _ = _call_tool(mcp, "get_log", {"filter": "all"})
        if not result.get("success") or result["data"]["returned_lines"] != 0:
            print("  ERROR: expected an empty but successful result, got {}".format(result))
            failed = True

        print("Test: a reader failure is reported rather than raised")

        def _boom():
            """Stand-in reader that fails the way an unreadable log file would."""
            raise IOError("log file unreadable")

        web_mcp.read_predbat_log = _boom
        result, _ = _call_tool(mcp, "get_log")
        if result.get("success") or "unreadable" not in (result.get("error") or ""):
            print("  ERROR: expected a failure result naming the error, got {}".format(result))
            failed = True
    finally:
        web_mcp.read_predbat_log = saved_reader

    return failed


class FakeBase:
    """A stand-in for the PredBat instance, carrying only the attributes get_state walks."""

    def __init__(self, log_func):
        """Populate a mix of small, large, excluded and awkward state variables."""
        self.log = log_func
        self.prefix = "predbat"
        self.plan_interval_minutes = 30

        # Small scalars - the bulk of a real dump, and what get_state exists to return
        self.current_status = "Charging"
        self.soc_max = 9.52
        self.num_cars = 1
        self.carbon_enable = False
        self.charge_limit_best = [9.52, 4.0]

        # A per-minute series - too long to serialise, must be described instead
        self.load_minutes = {minute: minute * 0.01 for minute in range(2880)}

        # Short but bulky - rejected on byte size rather than entry count
        self.html_plan = "x" * (MCP_STATE_DEFAULT_MAX_BYTES + 500)

        # Values a plain json.dumps would choke on
        self.midnight_utc = datetime(2026, 8, 27, 0, 0, 0)
        self.inverter_object = object()

        # Legitimately-None state - a great deal of Predbat's is, so None must not double as
        # the "too large to return" sentinel
        self.plan_last_updated = None
        self.previous_status = None
        self.empty_dict = {}

        # Must never be returned: credentials and live object graphs
        self.ha_key = "ha-key-value"
        self.mcp_secret = "mcp-secret-value"
        self.solis_access_token = "solis-token-value"
        self.ha_interface = object()
        self.components = object()
        self.octopus_url_cache = {"https://example.invalid": "cached"}
        self.db_connection = object()
        self.args = {"ha_key": "ha-key-value", "battery_rate_max_charge": 3.0}

    def is_running(self):
        """Callables are skipped by get_state - present so that path is exercised."""
        return True


def test_state_value_helpers(my_predbat):
    """json_safe_value coerces anything json.dumps would reject, and summarise_state_value
    describes a value's shape well enough to decide whether to ask for it.
    """
    failed = False
    print("**** Testing get_state value helpers ****")

    safe = json_safe_value({"when": datetime(2026, 8, 27, 9, 0, 0), "nested": [1, {"deep": object()}], "ok": 2.5, "flag": True})
    try:
        json.dumps(safe)
    except TypeError as error:
        print("  ERROR: json_safe_value left a value json.dumps cannot encode: {}".format(error))
        failed = True
    if safe["when"] != "2026-08-27T09:00:00":
        print("  ERROR: expected a datetime to become an ISO string, got {!r}".format(safe["when"]))
        failed = True
    if safe["ok"] != 2.5 or safe["flag"] is not True:
        print("  ERROR: plain values should pass through untouched, got {!r}".format(safe))
        failed = True

    print("Test: deeply nested values stop recursing rather than blowing the stack")
    deep = {}
    node = deep
    for _ in range(40):
        node["next"] = {}
        node = node["next"]
    try:
        json.dumps(json_safe_value(deep))
    except (TypeError, ValueError, RecursionError) as error:
        print("  ERROR: deep nesting was not handled: {}".format(error))
        failed = True

    print("Test: a numeric series is summarised with its range")
    summary = summarise_state_value({minute: float(minute) for minute in range(1000)})
    if summary.get("type") != "dict" or summary.get("length") != 1000:
        print("  ERROR: expected a dict of 1000 entries, got {}".format(summary))
        failed = True
    if summary.get("min") != 0.0 or summary.get("max") != 999.0:
        print("  ERROR: expected min/max over the values, got {}".format(summary))
        failed = True
    if len(summary.get("sample_keys", [])) != 3:
        print("  ERROR: expected three sample keys, got {}".format(summary.get("sample_keys")))
        failed = True

    print("Test: measure_state_value reports fit separately from the value itself")
    fits, safe, size = measure_state_value(None, MCP_STATE_DEFAULT_MAX_BYTES)
    if not fits or safe is not None or size != len("null"):
        print("  ERROR: None should fit and come back as None, got fits={} safe={!r} size={}".format(fits, safe, size))
        failed = True
    fits, safe, _ = measure_state_value({minute: minute for minute in range(MCP_STATE_LARGE_COLLECTION + 1)}, MCP_STATE_DEFAULT_MAX_BYTES)
    if fits or safe is not None:
        print("  ERROR: a long collection should not fit, got fits={}".format(fits))
        failed = True
    fits, _, _ = measure_state_value("z" * (MCP_STATE_DEFAULT_MAX_BYTES + 1), MCP_STATE_DEFAULT_MAX_BYTES)
    if fits:
        print("  ERROR: an oversized string should not fit")
        failed = True

    print("Test: a long string is summarised with a preview, not its full contents")
    summary = summarise_state_value("y" * 5000)
    if summary.get("length") != 5000 or len(summary.get("preview", "")) != 200:
        print("  ERROR: expected a 200 character preview of a 5000 character string, got {}".format({k: v for k, v in summary.items() if k != "preview"}))
        failed = True

    return failed


def test_debug_excluded_keys(my_predbat):
    """The state query and the debug yaml share one exclusion filter, so a variable that never
    reaches a debug dump can't be read over MCP either.
    """
    failed = False
    print("**** Testing is_debug_excluded_key ****")

    for key in ["ha_interface", "components", "secrets", "octopus_url_cache", "github_url_cache", "inverters", "CONFIG_ITEMS", "logfile"]:
        if not is_debug_excluded_key(key):
            print("  ERROR: expected {} to be excluded".format(key))
            failed = True

    print("Test: credentials are excluded via the shared secret matcher, including secrets and tokens")
    for key in ["ha_key", "octopus_api_key", "mcp_secret", "solis_access_token", "deye_password"]:
        if not is_debug_excluded_key(key):
            print("  ERROR: expected the credential {} to be excluded".format(key))
            failed = True

    print("Test: database internals and double-underscore names are excluded by prefix")
    for key in ["db_connection", "db_manage", "__class__"]:
        if not is_debug_excluded_key(key):
            print("  ERROR: expected {} to be excluded by prefix".format(key))
            failed = True

    print("Test: ordinary diagnostic state is not excluded")
    for key in ["soc_max", "charge_limit_best", "load_minutes", "current_status", "dashboard_values", "solis_token_expires_at"]:
        if is_debug_excluded_key(key):
            print("  ERROR: expected {} to be readable".format(key))
            failed = True

    return failed


def test_mcp_get_state(my_predbat):
    """get_state returns the small variables, describes the large ones instead of returning them,
    honours keys/filter/max_bytes, and never leaks a credential or a live object graph (#4768).
    """
    failed = False
    print("**** Testing MCP get_state ****")

    mcp = _make_mcp(my_predbat)
    mcp.base = FakeBase(my_predbat.log)

    print("Test: with no arguments the small variables come back and the big ones are described")
    result, _ = _call_tool(mcp, "get_state")
    if not result.get("success"):
        print("  ERROR: get_state failed: {}".format(result.get("error")))
        return True
    data = result["data"]
    state, omitted = data["state"], data["omitted"]

    for key in ["current_status", "soc_max", "num_cars", "carbon_enable", "charge_limit_best"]:
        if key not in state:
            print("  ERROR: expected the small variable {} to be returned".format(key))
            failed = True
    if state.get("soc_max") != 9.52 or state.get("carbon_enable") is not False:
        print("  ERROR: small values came back altered: {!r}".format({k: state.get(k) for k in ["soc_max", "carbon_enable"]}))
        failed = True

    print("Test: None-valued state is returned as null, not reported as omitted")
    for key in ["plan_last_updated", "previous_status"]:
        if key in omitted:
            print("  ERROR: {} is None, not too large - it must not be listed as omitted".format(key))
            failed = True
        if key not in state or state[key] is not None:
            print("  ERROR: expected {} to come back as null, got {!r}".format(key, state.get(key)))
            failed = True
    if "empty_dict" not in state or state["empty_dict"] != {}:
        print("  ERROR: expected an empty collection to be returned, got {!r}".format(state.get("empty_dict")))
        failed = True

    print("Test: a per-minute series is described, not returned")
    if "load_minutes" in state:
        print("  ERROR: the 2880 entry series should not have been returned in full")
        failed = True
    if omitted.get("load_minutes", {}).get("length") != 2880:
        print("  ERROR: expected load_minutes to be described with its length, got {}".format(omitted.get("load_minutes")))
        failed = True
    if "max" not in omitted.get("load_minutes", {}):
        print("  ERROR: expected a numeric summary for load_minutes, got {}".format(omitted.get("load_minutes")))
        failed = True

    print("Test: a bulky string is rejected on size even though it is a single value")
    if "html_plan" in state or "html_plan" not in omitted:
        print("  ERROR: expected html_plan to be described rather than returned")
        failed = True

    print("Test: credentials and live object graphs never appear, in state or in omitted")
    for key in ["ha_key", "mcp_secret", "solis_access_token", "ha_interface", "components", "octopus_url_cache", "db_connection"]:
        if key in state or key in omitted:
            print("  ERROR: excluded key {} was exposed".format(key))
            failed = True
    blob = json.dumps(data)
    for secret in ["ha-key-value", "mcp-secret-value", "solis-token-value"]:
        if secret in blob:
            print("  ERROR: the secret {!r} leaked into the response".format(secret))
            failed = True

    print("Test: args is masked if it is returned at all")
    if "args" in state and state["args"].get("ha_key") != "xxx":
        print("  ERROR: expected args to be masked, got {!r}".format(state.get("args")))
        failed = True

    print("Test: values json.dumps could not encode are coerced rather than failing the call")
    if state.get("midnight_utc") != "2026-08-27T00:00:00":
        print("  ERROR: expected the datetime to be returned as an ISO string, got {!r}".format(state.get("midnight_utc")))
        failed = True
    if not isinstance(state.get("inverter_object"), str):
        print("  ERROR: expected an opaque object to become a string, got {!r}".format(state.get("inverter_object")))
        failed = True

    print("Test: methods are skipped")
    if "is_running" in state or "is_running" in omitted:
        print("  ERROR: callables should not be reported as state")
        failed = True

    print("Test: keys= returns just those variables, and reports the ones that don't exist")
    result, _ = _call_tool(mcp, "get_state", {"keys": ["soc_max", "num_cars", "not_a_real_key"]})
    data = result["data"]
    if sorted(data["state"].keys()) != ["num_cars", "soc_max"]:
        print("  ERROR: expected only the two named keys, got {}".format(sorted(data["state"].keys())))
        failed = True
    if data.get("unknown_keys") != ["not_a_real_key"]:
        print("  ERROR: expected the unknown key to be reported back, got {}".format(data.get("unknown_keys")))
        failed = True

    print("Test: a single key may be given as a bare string")
    result, _ = _call_tool(mcp, "get_state", {"keys": "soc_max"})
    if list(result["data"]["state"].keys()) != ["soc_max"]:
        print("  ERROR: expected a bare string key to work, got {}".format(result["data"]["state"]))
        failed = True

    print("Test: asking for an excluded key by name still refuses it")
    result, _ = _call_tool(mcp, "get_state", {"keys": ["ha_key", "mcp_secret", "ha_interface"]})
    data = result["data"]
    if data["state"] or data["omitted"]:
        print("  ERROR: naming an excluded key explicitly must not return it, got {}".format(data))
        failed = True

    print("Test: filter narrows by variable name")
    result, _ = _call_tool(mcp, "get_state", {"filter": "^charge_"})
    if list(result["data"]["state"].keys()) != ["charge_limit_best"]:
        print("  ERROR: expected only charge_ variables, got {}".format(list(result["data"]["state"].keys())))
        failed = True

    print("Test: raising max_bytes lets a previously omitted value through")
    result, _ = _call_tool(mcp, "get_state", {"keys": ["html_plan"], "max_bytes": MCP_STATE_DEFAULT_MAX_BYTES + 2000})
    if "html_plan" not in result["data"]["state"]:
        print("  ERROR: expected html_plan to fit once max_bytes was raised")
        failed = True

    print("Test: max_bytes cannot be raised past the protocol cap, and a long series still won't fit")
    result, _ = _call_tool(mcp, "get_state", {"keys": ["load_minutes"], "max_bytes": MCP_STATE_MAX_BYTES_LIMIT * 100})
    if result["data"]["max_bytes"] != MCP_STATE_MAX_BYTES_LIMIT:
        print("  ERROR: expected max_bytes to be clamped to the cap, got {}".format(result["data"]["max_bytes"]))
        failed = True
    if "load_minutes" in result["data"]["state"]:
        print("  ERROR: a {} entry collection should be refused on entry count regardless of max_bytes".format(MCP_STATE_LARGE_COLLECTION))
        failed = True

    print("Test: a non-list keys argument is rejected with a clear error")
    result, _ = _call_tool(mcp, "get_state", {"keys": {"soc_max": True}})
    if result.get("success") or "keys" not in (result.get("error") or ""):
        print("  ERROR: expected an error naming the bad argument, got {}".format(result))
        failed = True

    return failed


def test_mcp_tools_list(my_predbat):
    """get_log is advertised by tools/list with a usable schema, and tools/call routes to it."""
    failed = False
    print("**** Testing MCP tools/list registration ****")

    mcp = _make_mcp(my_predbat)
    tools = asyncio.run(mcp._handle_tools_list({}))["tools"]
    by_name = {tool["name"]: tool for tool in tools}

    if "get_log" not in by_name:
        print("  ERROR: get_log is missing from tools/list")
        return True
    if "get_state" not in by_name:
        print("  ERROR: get_state is missing from tools/list")
        return True

    state_schema = by_name["get_state"]["inputSchema"]
    for prop in ["keys", "filter", "max_bytes"]:
        if prop not in state_schema["properties"]:
            print("  ERROR: get_state schema is missing the {} property".format(prop))
            failed = True
    if state_schema["properties"]["keys"].get("type") != "array":
        print("  ERROR: get_state keys should be declared as an array")
        failed = True
    if state_schema.get("required"):
        print("  ERROR: get_state should have no required arguments")
        failed = True

    schema = by_name["get_log"]["inputSchema"]
    for prop in ["filter", "search", "hours", "max_lines"]:
        if prop not in schema["properties"]:
            print("  ERROR: get_log schema is missing the {} property".format(prop))
            failed = True
    if schema.get("required"):
        print("  ERROR: get_log should have no required arguments")
        failed = True
    if schema["properties"]["filter"].get("enum") != list(LOG_FILTER_TYPES):
        print("  ERROR: get_log filter enum should list every supported view, got {}".format(schema["properties"]["filter"].get("enum")))
        failed = True
    if str(MCP_LOG_DEFAULT_LINES) not in schema["properties"]["max_lines"]["description"]:
        print("  ERROR: the max_lines description should name the default")
        failed = True

    if "masked" not in by_name["get_apps"]["inputSchema"]["properties"]:
        print("  ERROR: get_apps schema is missing the masked property")
        failed = True

    print("Test: an unknown tool is still rejected")
    result = asyncio.run(mcp._handle_tools_call({"name": "get_logs", "arguments": {}}))
    if not result.get("isError"):
        print("  ERROR: expected an error for an unknown tool name")
        failed = True

    return failed


def test_web_api_log_unchanged(my_predbat):
    """The web log API still filters exactly as it did before it moved onto the shared helpers,
    including its HTML escaping and search highlighting.
    """
    failed = False
    print("**** Testing /api/log after the shared-helper refactor ****")

    w = _make_web(my_predbat)
    saved_reader = web.read_predbat_log
    try:
        web.read_predbat_log = lambda: _sample_log() + _stamp(5) + "Info: <b>escape me</b>\n"

        response = asyncio.run(w.html_api_get_log(FakeRequest({"filter": "errors"})))
        data = json.loads(response.text)
        if data["status"] != "success":
            print("  ERROR: expected a successful response, got {}".format(data))
            return True
        if [line["type"] for line in data["lines"]] != ["error"]:
            print("  ERROR: expected only the error line on the errors tab, got {}".format([line["type"] for line in data["lines"]]))
            failed = True

        response = asyncio.run(w.html_api_get_log(FakeRequest({"filter": "warnings"})))
        types = sorted(set(line["type"] for line in json.loads(response.text)["lines"]))
        if types != ["error", "warning"]:
            print("  ERROR: expected warnings and errors on the warnings tab, got {}".format(types))
            failed = True

        response = asyncio.run(w.html_api_get_log(FakeRequest({"filter": "all"})))
        data = json.loads(response.text)
        if not any(line["type"] == "log" for line in data["lines"]):
            print("  ERROR: expected untyped lines on the all tab")
            failed = True
        if not any("&lt;b&gt;escape me&lt;/b&gt;" in line["full_line"] for line in data["lines"]):
            print("  ERROR: expected HTML in log lines to still be escaped")
            failed = True

        response = asyncio.run(w.html_api_get_log(FakeRequest({"filter": "all", "search": "iboost"})))
        data = json.loads(response.text)
        if data.get("search_matches") != 1:
            print("  ERROR: expected a single search match, got {}".format(data.get("search_matches")))
            failed = True
        if not any("search-highlight" in line["full_line"] for line in data["lines"]):
            print("  ERROR: expected the search term to be highlighted")
            failed = True
    finally:
        web.read_predbat_log = saved_reader

    return failed


def run_web_mcp_tests(my_predbat):
    """Run every MCP log/apps.yaml test, returning True if any of them failed."""
    failed = False
    failed |= test_mask_secret_args(my_predbat)
    failed |= test_read_predbat_log(my_predbat)
    failed |= test_log_filter_helpers(my_predbat)
    failed |= test_mcp_get_apps(my_predbat)
    failed |= test_mcp_get_log(my_predbat)
    failed |= test_state_value_helpers(my_predbat)
    failed |= test_debug_excluded_keys(my_predbat)
    failed |= test_mcp_get_state(my_predbat)
    failed |= test_mcp_tools_list(my_predbat)
    failed |= test_web_api_log_unchanged(my_predbat)
    return failed

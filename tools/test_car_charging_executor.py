"""Offline acceptance matrix for an operator-supplied Home Assistant EV executor.

Run inside the installed Home Assistant Python environment, passing a private JSON
fixture on stdin. The fixture contains ``automation``, an ``entities`` role map and
explicit electrical ``limits``. No credentials, network access or device service
calls are used. Actual supplied templates run against isolated in-memory states;
the physical expectation is calculated independently of those templates.

This intentionally is not a controller or a hardware commissioning certificate.
The ordinary unit suite tests scenario construction without requiring HA installed.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import product
import json
import math
import sys
from unittest.mock import patch


@dataclass
class ExecutorCase:
    """One independent observation, expected decision and optional evidence faults."""

    name: str
    load_w: float = 1000
    grid_w: float = 0
    pv_w: float = 0
    battery_percent: float = 60
    powered: bool = False
    off_seconds: float = 700
    permission: str = "on"
    overrides: dict = field(default_factory=dict)
    ages: dict = field(default_factory=dict)
    lease: dict = field(default_factory=dict)
    expected: bool | None = None


def validate_limits(limits):
    """Reject ambiguous/non-finite contracts rather than invent installation defaults."""
    required = {"grid_cap_w", "load_cap_w", "battery_discharge_w", "ev_rate_w", "stop_percent", "restart_percent", "cooldown_seconds", "measurement_seconds"}
    if set(limits) != required:
        raise ValueError("An explicit complete electrical test contract is required")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in limits.values()):
        raise ValueError("Limits must be finite positive numeric values")
    if not limits["stop_percent"] < limits["restart_percent"] <= 100:
        raise ValueError("Battery hysteresis must be ordered within 100 percent")
    if limits["grid_cap_w"] > limits["load_cap_w"]:
        raise ValueError("Grid cap cannot exceed the load-port contract")


def expected_power_decision(case, limits):
    """Independent start/continuation oracle; grid is signed and PV offsets load."""
    if case.expected is not None:
        return case.expected
    if case.permission != "on":
        return False
    if case.battery_percent <= limits["stop_percent"] or case.battery_percent > 100:
        return False
    if case.grid_w > limits["load_cap_w"] or case.load_w > limits["load_cap_w"]:
        return False
    if case.powered:
        return True
    if case.battery_percent < limits["restart_percent"] or case.off_seconds < limits["cooldown_seconds"]:
        return False
    available_w = min(limits["load_cap_w"], limits["grid_cap_w"] + min(limits["load_cap_w"], limits["battery_discharge_w"] + max(0, case.pv_w)))
    return case.load_w + limits["ev_rate_w"] <= available_w


def validate_active_variable_order(variables, active_order):
    """Dictionary equality ignores execution order; compare the active key sequence too."""
    if active_order is not None and active_order != list(variables):
        raise ValueError("Active automation variable order differs from saved configuration")


def executor_cases(limits):
    """Sweep physical boundaries and explicit evidence faults, not random happy paths."""
    validate_limits(limits)
    cap = limits["load_cap_w"]
    start_load = min(cap, limits["grid_cap_w"] + limits["battery_discharge_w"]) - limits["ev_rate_w"]
    loads = sorted({0, 1000, max(0, start_load - 1), max(0, start_load), max(0, start_load + 1), cap - 1, cap, cap + 1})
    grids = sorted({-limits["grid_cap_w"], 0, limits["grid_cap_w"] - 1, limits["grid_cap_w"], cap - 1, cap, cap + 1})
    pvs = sorted({0, 1000, limits["battery_discharge_w"], cap, cap * 2})
    levels = sorted({limits["stop_percent"], limits["stop_percent"] + 0.01, limits["restart_percent"] - 0.01, limits["restart_percent"], 100})
    cases = []
    for index, (load, grid, pv, level, powered) in enumerate(product(loads, grids, pvs, levels, (False, True))):
        cases.append(ExecutorCase("power_{}".format(index), load_w=load, grid_w=grid, pv_w=pv, battery_percent=level, powered=powered))
    for value in (0, limits["cooldown_seconds"] - 0.01, limits["cooldown_seconds"], limits["cooldown_seconds"] + 0.01):
        for powered in (False, True):
            cases.append(ExecutorCase("cooldown_{}_{}".format(value, powered), off_seconds=value, powered=powered))
    for permission in ("off", "unknown", "unavailable", "on"):
        cases.append(ExecutorCase("permission_" + permission, permission=permission))
    for role in ("grid", "load", "battery"):
        for value in ("unknown", "unavailable", "nan", "inf", "not-a-number"):
            cases.append(ExecutorCase("bad_{}_{}".format(role, value), overrides={role: value}, expected=False))
        for age in (limits["measurement_seconds"] - 0.01, limits["measurement_seconds"], limits["measurement_seconds"] + 0.01, -5, -5.01, -6):
            cases.append(ExecutorCase("age_{}_{}".format(role, age), ages={role: age}, expected=-5 <= age < limits["measurement_seconds"]))
    for role, value in (("read_only", "on"), ("mode", "Monitor"), ("peak_enabled", "off"), ("peak_limit", "0"), ("peak_limit", "unavailable"), ("slot", "off")):
        cases.append(ExecutorCase("guard_{}_{}".format(role, value), overrides={role: value}, expected=False))
    for role, value in (("pv", "unavailable"), ("pv", "nan"), ("pv", "-1000")):
        cases.append(ExecutorCase("conservative_{}_{}".format(role, value), overrides={role: value}, expected=True))
    lease_cases = {
        "expired": {"start_delta": -301, "end_delta": -1},
        "future": {"start_delta": 1, "end_delta": 299},
        "overlong": {"start_delta": -1, "end_delta": 300},
        "zero_duration": {"start_delta": 0, "end_delta": 0},
        "reverse": {"start_delta": 1, "end_delta": -1},
        "bad_version": {"lease_version": 2},
        "boolean_version": {"lease_version": True},
        "string_version": {"lease_version": "1"},
        "missing_start": {"valid_from": None},
        "missing_end": {"valid_until": None},
        "bad_timestamp": {"valid_until": "not-a-date"},
        "naive_timestamp": {"valid_until": "2030-01-01T00:00:00"},
    }
    for name, lease in lease_cases.items():
        cases.append(ExecutorCase("lease_" + name, lease=lease, expected=False))
    cases.extend(
        [
            ExecutorCase("lease_exact_maximum", lease={"start_delta": -1, "end_delta": 299}, expected=True),
            ExecutorCase("lease_start_inclusive", lease={"start_delta": 0, "end_delta": 300}, expected=True),
            ExecutorCase("lease_end_exclusive", lease={"start_delta": -300, "end_delta": 0}, expected=False),
            ExecutorCase("health_error", overrides={"health_error": True}, expected=False),
            ExecutorCase("health_stale", ages={"status": 901}, expected=False),
            ExecutorCase("health_age_limit", ages={"status": 900}, expected=False),
            ExecutorCase("health_before_age_limit", ages={"status": 899.99}, expected=True),
            ExecutorCase("health_future", ages={"status": -6}, expected=False),
            ExecutorCase("health_clock_tolerance", ages={"status": -5}, expected=True),
            ExecutorCase("battery_above_range", battery_percent=100.01, expected=False),
            ExecutorCase("battery_below_range", battery_percent=-0.01, expected=False),
            ExecutorCase("pv_stale_no_invented_headroom", load_w=max(0, start_load + 1), pv_w=cap, ages={"pv": limits["measurement_seconds"]}, expected=False),
            ExecutorCase("pv_fresh_supplies_headroom", load_w=max(0, start_load + 1), pv_w=cap, expected=True),
            ExecutorCase("reserve_latched_below_restart", battery_percent=limits["restart_percent"] - 0.01, powered=True, overrides={"reserve_paused": "on"}, expected=False),
            ExecutorCase("reserve_latched_at_restart", battery_percent=limits["restart_percent"], overrides={"reserve_paused": "on"}, expected=True),
        ]
    )
    return cases


async def run_native(fixture):
    """Render supplied real templates inside isolated HA; never execute their actions."""
    try:
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import template
        from homeassistant.helpers.script_variables import ScriptVariables
        from homeassistant.util import dt as dt_util
    except ImportError as error:
        raise RuntimeError("Run this acceptance matrix inside the installed Home Assistant Python environment") from error
    limits = fixture["limits"]
    cases = executor_cases(limits)
    entities = fixture["entities"]
    required_roles = {"slot", "relay", "grid", "load", "pv", "battery", "status", "mode", "read_only", "reserve_paused", "peak_enabled", "peak_limit", "automatic"}
    if set(entities) != required_roles or len(set(entities.values())) != len(entities):
        raise ValueError("A unique complete entity role map is required")
    automation = fixture["automation"]
    variables = automation["variables"]
    active_order = fixture.get("active_variable_order")
    validate_active_variable_order(variables, active_order)
    if "predbat_lease_ok" not in variables or "want_on" not in variables:
        raise ValueError("Executor lease and request variables are required")
    # Permission is a native top-level condition, not part of want_on. Refuse a
    # changed condition shape rather than quietly testing a different controller.
    if automation["conditions"] != [{"condition": "state", "entity_id": entities["automatic"], "state": "on"}]:
        raise ValueError("Unsupported master permission condition; update the test contract")
    clock = datetime.now(timezone.utc)
    hass = HomeAssistant("/tmp")
    rendered_variables = ScriptVariables({key: template.Template(value, hass) for key, value in variables.items()})
    failures = []
    accepted = rejected = 0
    cases.append(ExecutorCase("dependency_order_eligible_baseline", expected=True))
    with patch.object(dt_util, "utcnow", return_value=clock), patch.object(dt_util, "now", return_value=clock):
        for case in cases:
            values = {
                "slot": "on",
                "relay": "on" if case.powered else "off",
                "grid": str(case.grid_w),
                "load": str(case.load_w),
                "pv": str(case.pv_w),
                "battery": str(case.battery_percent),
                "status": "Charging",
                "mode": "Control charge",
                "read_only": "off",
                "reserve_paused": "off",
                "peak_enabled": "on",
                "peak_limit": str(limits["grid_cap_w"]),
                "automatic": case.permission,
            }
            values.update({key: value for key, value in case.overrides.items() if key in values})
            lease = {"source": "predbat", "lease_version": 1, "valid_from": (clock + timedelta(seconds=case.lease.get("start_delta", -1))).isoformat(), "valid_until": (clock + timedelta(seconds=case.lease.get("end_delta", 299))).isoformat()}
            lease.update({key: value for key, value in case.lease.items() if key not in ("start_delta", "end_delta")})
            for role, entity in entities.items():
                attributes = {}
                if role == "slot":
                    attributes = lease
                elif role == "status":
                    attributes = {"error": case.overrides.get("health_error", False), "last_updated": (clock - timedelta(seconds=case.ages.get("status", 0))).isoformat()}
                hass.states.async_set(entity, values[role], attributes)
                state = hass.states.get(entity)
                state.last_reported = clock - timedelta(seconds=case.ages.get(role, 0))
                if role == "relay":
                    state.last_changed = clock - timedelta(seconds=case.off_seconds)
            expected = expected_power_decision(case, limits)
            try:
                rendered = rendered_variables.async_render(hass, {})
                decision = rendered.get("want_on")
                if type(decision) is not bool:
                    raise ValueError("Decision did not render as a boolean")
                actual = decision and values["automatic"] == "on"
                if actual != expected:
                    failures.append({"case": case.name, "expected": expected, "actual": actual})
            except Exception as error:
                failures.append({"case": case.name, "error": type(error).__name__})
            accepted += int(expected)
            rejected += int(not expected)
        defective = {key: value for key, value in variables.items() if key != "predbat_lease_ok"}
        defective["predbat_lease_ok"] = variables["predbat_lease_ok"]
        bad_variables = ScriptVariables({key: template.Template(value, hass) for key, value in defective.items()})
        try:
            rejected_bad_order = bad_variables.async_render(hass, {}).get("want_on") is not True
        except Exception:
            rejected_bad_order = True
        if not rejected_bad_order:
            failures.append({"case": "dependency_reorder_regression_not_detected"})
    # An accidental variable reorder must fail a genuinely eligible request;
    # current OFF telemetry must not mask the overnight defect through short-circuiting.
    return {
        "cases": len(cases),
        "expected_on": accepted,
        "expected_off": rejected,
        "failures": failures,
        "dependency_reorder_regression_detected": rejected_bad_order,
        "active_config_checked": active_order is not None,
        "services_called": 0,
        "isolated": True,
    }


def main():
    """Read a private fixture from stdin and return a machine-checkable test verdict."""
    fixture = json.load(sys.stdin)
    result = asyncio.run(run_native(deepcopy(fixture)))
    print(json.dumps(result))
    return int(bool(result["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())

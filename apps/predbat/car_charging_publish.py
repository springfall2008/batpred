"""Attach planning diagnostics to existing native car entities, never authorise execution."""

from copy import deepcopy


def car_plan_attributes(report):
    """Keep forecast diagnostics distinct from the native current-interval lease fields."""
    if not isinstance(report, dict):
        return {}
    return {
        "source": "predbat",
        "plan_status": report.get("status"),
        "plan_reason": report.get("execution_reason"),
        "plan_generated_at": report.get("generated_at"),
        "plan": deepcopy({key: value for key, value in report.items() if key != "preview"}),
    }


def publish_car_charging_report(base, report):
    """Update diagnostic attributes without changing ON/OFF, planned authority or lease expiry."""
    entity = "binary_sensor." + base.prefix + "_car_charging_slot"
    previous = getattr(base, "dashboard_values", {}).get(entity, {})
    if isinstance(report, dict) and report.get("status") == "disabled" and not previous:
        # Merely importing the optional planner must not create a car entity in
        # an ordinary household installation with no car publication.
        return
    state = previous.get("state", "off")
    attributes = {
        "planned": [],
        "source": "predbat",
        "lease_version": 1,
        "valid_from": None,
        "valid_until": None,
        **previous.get("attributes", {}),
        **car_plan_attributes(report),
    }
    base.dashboard_item(entity, state=state if state in ("on", "off") else "off", attributes=attributes)

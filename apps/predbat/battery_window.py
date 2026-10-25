"""Battery-mode stability preferences; never actuator or EV execution authority.

Only an observed successful combined dispatch starts a commitment. It survives normal
replanning, not process restarts, changed configuration or observed control drift.
All reserve, manual, connection, replay and lease guards remain independent.
"""

from car_charging_config import read_car_charging_config

from datetime import timedelta
import math

WINDOW_MINUTES = (5, 15, 30, 60)


def selected_window_minutes(base):
    """Resolve the native persisted select or an explicit literal/cached dropdown override."""
    settings = read_car_charging_config(base)
    if isinstance(settings, dict) and "battery_min_window_minutes" in settings:
        value = settings["battery_min_window_minutes"]
    else:
        value = base.get_arg("battery_min_window_minutes", "30", indirect=False)
    if isinstance(value, str) and value.startswith(("input_select.", "select.")):
        from car_charging_plan import _read_entity_state

        observed = _read_entity_state(base, value)
        value = observed["state"] if observed else None
    if isinstance(value, str) and value in tuple(str(minutes) for minutes in WINDOW_MINUTES):
        return int(value)
    if type(value) is int and value in WINDOW_MINUTES:
        return value
    raise ValueError("Battery minimum window must be 5, 15, 30 or 60 minutes")


def command_mode(command):
    """Classify control intent, not instantaneous power direction or the independently switched EV."""
    if command.get("freeze"):
        return "hold"
    if command["target_soc"] > 0:
        return "charge"
    return "limited_demand" if command.get("power_w") is not None else "demand"


def observed_window(base, now):
    """Read a prior observed commitment without reviving an expired EV receipt or polling hardware."""
    from car_charging_execute import _control_fingerprint, execution_fingerprint, _stamp

    previous = getattr(base, "battery_window", None)
    if not isinstance(previous, dict):
        return None
    try:
        if previous["fingerprint"] != execution_fingerprint(base):
            return None
        if previous["control_fingerprint"] != _control_fingerprint(base.inverters[0]):
            return None
        if previous["mode"] not in ("charge", "demand", "hold", "limited_demand") or _stamp(previous["started_at"]) > now:
            return None
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
    return dict(previous)


def planning_window(base, reference, observed_now, minimum):
    """Carry remaining real dwell time onto the five-minute forecast axis without restarting its clock."""
    from car_charging_execute import _stamp

    previous = observed_window(base, observed_now) if minimum > 5 else None
    if previous is None:
        return None, 0
    remaining = math.ceil((_stamp(previous["started_at"]) + timedelta(minutes=minimum) - reference).total_seconds() / 60)
    return previous["mode"], min(minimum, max(0, remaining))


def record_observed_window(base, command, now, previous):
    """Remember only accepted observed mode starts; target/EV changes cannot restart the same mode."""
    from car_charging_execute import _control_fingerprint, execution_fingerprint

    mode = command_mode(command)
    base.battery_window = {
        "mode": mode,
        "started_at": previous["started_at"] if previous and previous["mode"] == mode else now.isoformat(),
        "fingerprint": execution_fingerprint(base),
        "control_fingerprint": _control_fingerprint(base.inverters[0]),
    }


def commands_respect_windows(commands, minimum, initial_mode=None, initial_remaining=0):
    """Independently check complete mode runs; a horizon-ending run may be truncated, never an interior run."""
    mode, remaining = initial_mode, initial_remaining
    for command in commands:
        next_mode = command_mode(command)
        if next_mode != mode:
            if remaining > 0:
                return False
            mode, remaining = next_mode, minimum
        remaining = max(0, remaining - (command["end"] - command["start"]))
    return True

"""Test-only mutable view of internal inputs backed by real native flat arguments.

Production uses read_car_charging_config, which returns an ordinary input snapshot.
This view keeps the large regression matrix concise while every mutation changes
the actual native argument that the planner reads, not a parallel fixture store.
"""

from copy import deepcopy

from car_charging_config import CAR_SETTING_KEYS, native_car_arguments, read_car_charging_config


class CarSettings(dict):
    """Translate fixture mutations into the same flat arguments used in production."""

    def __init__(self, base):
        """Initialise a detached key index with shared configured values."""
        self.base = base
        super().__init__(read_car_charging_config(base) or {})

    def __setitem__(self, key, value):
        """Write the public native key before updating the convenient fixture view."""
        self.base.args[CAR_SETTING_KEYS[key]] = value
        super().__setitem__(key, value)

    def __delitem__(self, key):
        """Remove the native argument, not merely a test snapshot."""
        self.base.args.pop(CAR_SETTING_KEYS[key], None)
        super().__delitem__(key)

    def update(self, *args, **kwargs):
        """Respect the setter for all fixture bulk updates."""
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def pop(self, key, *default):
        """Keep optional fixture-field removals visible to the production reader."""
        if key in self:
            value = self[key]
            del self[key]
            return value
        if default:
            return default[0]
        raise KeyError(key)

    def __deepcopy__(self, memo):
        """Copy only the input values, never the Predbat instance or credentials."""
        return deepcopy(dict(self), memo)


def car_settings(base):
    """Return a mutable fixture view; a missing plan remains explicitly disabled."""
    return CarSettings(base)


def clear_car_settings(base):
    """Clear only the mapped configuration before an isolated synthetic scenario."""
    previous = read_car_charging_config(base)
    for key in CAR_SETTING_KEYS.values():
        base.args.pop(key, None)
    return previous


def configure_car_settings(base, settings):
    """Initialise a regression using flat native arguments, including deliberate bad modes."""
    clear_car_settings(base)
    if settings is None or settings == {}:
        return
    if not isinstance(settings, dict):
        base.args["car_charging_plan_mode"] = settings
        return
    base.args.update(native_car_arguments(settings))
    # Existing dated regression inputs use kWh; the public native car SOC default
    # remains percentage-based and must not be inferred from those fixtures.
    base.args.setdefault("car_charging_soc_unit", "kWh")
    if "import_limit_w" not in settings:
        base.args["import_limit"] = base.import_limit

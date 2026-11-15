# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for apps.yaml currency_scale (#6155, #3683): the min/max of every money setting is multiplied
by it when Predbat starts, so a currency with a far smaller minor unit than the penny can set a
threshold that means something, while defaults and non-money settings stay as they were.
"""

from tests.test_infra import TestHAInterface


def _restart(my_predbat, currency_scale=None, overrides=None):
    """Rebuild the config the way startup does (reset() then load_user_config()) with the given apps.yaml values."""
    my_predbat.args.pop("currency_scale", None)
    if currency_scale is not None:
        my_predbat.args["currency_scale"] = currency_scale
    for name, value in (overrides or {}).items():
        my_predbat.args[name] = value
    my_predbat.reset()
    my_predbat.update_time(print=False)
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.base = my_predbat
    my_predbat.ha_interface.history_enable = False
    my_predbat.load_user_config()


def _range(my_predbat, name):
    """Return the (min, max) a config item publishes after startup."""
    item = my_predbat.config_index[name]
    return item["min"], item["max"]


def _check(label, got, want):
    """Print and return True on a mismatch."""
    if got != want:
        print("  ERROR: {}: expected {}, got {}".format(label, want, got))
        return True
    return False


def run_currency_scale_tests(my_predbat):
    """Check currency_scale widens only the money settings, and that a bad value is ignored."""
    failed = False
    saved_args = dict(my_predbat.args)
    try:
        print("  [currency_scale] unset leaves the declared ranges alone")
        _restart(my_predbat)
        failed |= _check("unset metric_min_improvement_export range", _range(my_predbat, "metric_min_improvement_export"), (-50, 50.0))
        failed |= _check("unset currency_scale attribute", getattr(my_predbat, "currency_scale", None), 1.0)

        print("  [currency_scale] 29 widens the money settings and nothing else")
        _restart(my_predbat, currency_scale=29, overrides={"expert_mode": True, "metric_min_improvement_export": 145})
        failed |= _check("metric_min_improvement_export range", _range(my_predbat, "metric_min_improvement_export"), (-1450, 1450))
        failed |= _check("metric_min_improvement_plan range", _range(my_predbat, "metric_min_improvement_plan"), (-1450, 7250))
        failed |= _check("metric_battery_cycle range (p/kWh)", _range(my_predbat, "metric_battery_cycle"), (-1450, 1450))
        failed |= _check("carbon_metric range (p/kg)", _range(my_predbat, "carbon_metric"), (0, 14500))
        failed |= _check("car_charging_plan_max_price range", _range(my_predbat, "car_charging_plan_max_price"), (-29000, 29000))
        # Not money: a kWh level, and a unit that merely contains the letter p
        failed |= _check("best_soc_keep range (kWh)", _range(my_predbat, "best_soc_keep"), (0, 100.0))
        failed |= _check("debug_history_count range (snapshots)", _range(my_predbat, "debug_history_count"), (1, 500))
        # Defaults stay in pence-equivalent terms the user chose: only the range moves
        failed |= _check("metric_min_improvement_swap default", my_predbat.config_index["metric_min_improvement_swap"]["default"], -0.25)
        # An apps.yaml value above the old 50 cap is no longer clamped back to it
        failed |= _check("metric_min_improvement_export value", my_predbat.config_index["metric_min_improvement_export"]["value"], 145)
        entity = "input_number.{}_metric_min_improvement_export".format(my_predbat.prefix)
        failed |= _check("HA entity max attribute", my_predbat.get_state_wrapper(entity_id=entity, attribute="max"), 1450)

        print("  [currency_scale] a fractional scale keeps fractional bounds")
        _restart(my_predbat, currency_scale=8.5)
        failed |= _check("metric_min_improvement range at 8.5", _range(my_predbat, "metric_min_improvement"), (-425, 425))
        failed |= _check("metric_min_improvement_plan range at 8.5", _range(my_predbat, "metric_min_improvement_plan"), (-425, 2125))
        failed |= _check("iboost_rate_threshold range at 8.5", _range(my_predbat, "iboost_rate_threshold"), (-85, 8500))

        for bad in ["twenty", 0.5, -3, "nan", "inf"]:
            print("  [currency_scale] {!r} is ignored".format(bad))
            _restart(my_predbat, currency_scale=bad)
            failed |= _check("metric_min_improvement_export range with {!r}".format(bad), _range(my_predbat, "metric_min_improvement_export"), (-50, 50.0))
            failed |= _check("currency_scale attribute with {!r}".format(bad), getattr(my_predbat, "currency_scale", None), 1.0)
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import ast
import os

from prediction import Prediction
from tests.test_infra import reset_inverter

# Attribute keys Home Assistant reads by exact name; a misspelling is stored as an unknown attribute and silently ignored
HA_ATTRIBUTE_KEYS = ["unit_of_measurement", "state_class", "device_class", "friendly_name", "icon"]


def _run_best_prediction(my_predbat):
    """Run a 24 hour flat prediction with save="best" so plan.py publishes its *_best dashboard sensors."""
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes
    horizon = my_predbat.forecast_minutes + my_predbat.minutes_now + 10
    my_predbat.pv_forecast_minute = {m: 0.0 for m in range(0, horizon)}
    my_predbat.pv_forecast_minute10 = dict(my_predbat.pv_forecast_minute)
    my_predbat.load_minutes_step = {m: 0.0 for m in range(0, horizon)}
    my_predbat.load_minutes_step10 = dict(my_predbat.load_minutes_step)
    pv_step = {m: 0.0 for m in range(0, horizon, 5)}
    load_step = {m: 0.0 for m in range(0, horizon, 5)}
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")


def _misspelt_attribute_key(key):
    """Return the HA attribute key that key is a misspelling of (differs only by whitespace or case), or None."""
    if not isinstance(key, str) or key in HA_ATTRIBUTE_KEYS:
        return None
    normalised = "".join(key.split()).lower()
    for canonical in HA_ATTRIBUTE_KEYS:
        if normalised == canonical:
            return canonical
    return None


def _misspelt_source_keys(tree):
    """Yield (lineno, key, canonical) for each misspelt HA attribute key in a parsed module: dict literal keys and subscript-assignment keys (attributes["key"] = ...)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            candidates = node.keys
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            candidates = [node.slice]
        else:
            continue
        for key in candidates:
            if isinstance(key, ast.Constant):
                canonical = _misspelt_attribute_key(key.value)
                if canonical:
                    yield key.lineno, key.value, canonical


def test_dashboard_device_class(my_predbat):
    """
    #3352 - a batch of dashboard sensors published with the right unit but no device_class, so
    Home Assistant can't offer them for energy-dashboard style graphing/statistics. Only
    predbat.temperature (temperature.py) had device_class set; predbat.battery_temperature and the
    eight power sensors below did not. Regression-tests all nine in one pass.
    """
    reset_inverter(my_predbat)
    failed = False

    print("Test: current power sensors (execute.py) get device_class 'power'")
    my_predbat.pv_power = 1000
    my_predbat.grid_power = 500
    my_predbat.load_power = 800
    my_predbat.battery_power = 300
    my_predbat.publish_inverter_data()
    for entity in ["pv_power", "grid_power", "load_power", "battery_power"]:
        attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + "." + entity)
        if not attrs or attrs.get("device_class") != "power":
            print("  ERROR: {} missing device_class=power, got {}".format(entity, attrs.get("device_class") if attrs else None))
            failed = True

    print("Test: battery_temperature (fetch.py) gets device_class 'temperature'")
    my_predbat.predict_battery_temperature({0: 20}, 30)
    attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + ".battery_temperature")
    if not attrs or attrs.get("device_class") != "temperature":
        print("  ERROR: battery_temperature missing device_class=temperature, got {}".format(attrs.get("device_class") if attrs else None))
        failed = True

    print("Test: predicted 'best' power sensors (plan.py) get device_class 'power'")
    _run_best_prediction(my_predbat)

    for entity in ["pv_power_best", "grid_power_best", "load_power_best", "battery_power_best"]:
        attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + "." + entity)
        if not attrs or attrs.get("device_class") != "power":
            print("  ERROR: {} missing device_class=power, got {}".format(entity, attrs.get("device_class") if attrs else None))
            failed = True

    return failed


def test_dashboard_attribute_keys(my_predbat):
    """
    #6388 - predbat.soc_kw_best_h12 was published with the attribute key "unit _of_measurement" (stray
    space), so Home Assistant never saw a unit for it while its h1/h8 siblings were fine. Checks the
    published soc_kw_best_h* sensors carry kWh, that nothing the plan publishes has a misspelt HA
    attribute key, and that no dict literal or subscript assignment (attributes["key"] = ...) anywhere
    in the source spells one of those keys wrongly.
    """
    reset_inverter(my_predbat)
    failed = False

    print("Test: soc_kw_best_h1/h8/h12 (plan.py) report unit_of_measurement kWh")
    my_predbat.ha_interface.dummy_items = {}
    _run_best_prediction(my_predbat)
    for entity in ["soc_kw_best_h1", "soc_kw_best_h8", "soc_kw_best_h12"]:
        attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + "." + entity)
        if not attrs or attrs.get("unit_of_measurement") != "kWh":
            print("  ERROR: {} missing unit_of_measurement=kWh, got {}".format(entity, attrs.get("unit_of_measurement") if attrs else None))
            failed = True

    print("Test: no sensor published by the plan has a misspelt HA attribute key")
    published = 0
    for entity_id, item in my_predbat.ha_interface.dummy_items.items():
        if not isinstance(item, dict):
            continue
        published += 1
        for key in item:
            canonical = _misspelt_attribute_key(key)
            if canonical:
                print("  ERROR: {} has attribute key {!r}, which Home Assistant ignores - should be {!r}".format(entity_id, key, canonical))
                failed = True
    if published < 10:
        print("  ERROR: expected the best prediction to publish its dashboard sensors, only found {}".format(published))
        failed = True

    print("Test: the source scan flags misspelt keys in dict literals and subscript assignments, but not reads")
    sample = (
        'a = {"unit _of_measurement": "kWh"}\nattributes["Device_Class"] = "energy"\nattributes["State_ class"]: str = "total"\nattributes["Icon "] += ""\nx = attributes["Friendly_Name"]\nok = {"unit_of_measurement": "kWh"}\nattributes["icon"] = "mdi:x"\n'
    )
    found = sorted((lineno, canonical) for lineno, _key, canonical in _misspelt_source_keys(ast.parse(sample)))
    expected = [(1, "unit_of_measurement"), (2, "device_class"), (3, "state_class"), (4, "icon")]
    if found != expected:
        print("  ERROR: source scan found {} in the sample, expected {}".format(found, expected))
        failed = True

    print("Test: no dict literal or subscript assignment in the Predbat source misspells an HA attribute key")
    source_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scanned = 0
    for filename in sorted(os.listdir(source_dir)):
        if not filename.endswith(".py"):
            continue
        path = os.path.join(source_dir, filename)
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        scanned += 1
        for lineno, key, canonical in _misspelt_source_keys(tree):
            print("  ERROR: {}:{} has attribute key {!r} - should be {!r}".format(filename, lineno, key, canonical))
            failed = True
    if scanned < 20:
        print("  ERROR: expected to scan the Predbat source in {}, only found {} files".format(source_dir, scanned))
        failed = True

    return failed

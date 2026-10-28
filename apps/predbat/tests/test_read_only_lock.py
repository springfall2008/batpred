# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio

from components import Components
from tests.test_infra import MockConfigProvider

LOCK_LOG = "Info: set_read_only is locked on, ignoring request to turn it off"
_MISSING = object()


def _check(label, got, expected):
    if got != expected:
        print("ERROR: {} - expected {!r}, got {!r}".format(label, expected, got))
        return True
    return False


def _save(my_predbat):
    item = my_predbat.config_index["set_read_only"]
    saved = {name: my_predbat.__dict__.get(name, _MISSING) for name in ("read_only_lock", "inverter_needs_reset", "inverter_needs_reset_force", "log", "components", "update_pending", "plan_valid")}
    return item, item.get("value", None), saved


def _restore(my_predbat, item, value, saved):
    item["value"] = value
    for name, old in saved.items():
        if old is _MISSING:
            my_predbat.__dict__.pop(name, None)
        else:
            setattr(my_predbat, name, old)


def _capture_log(my_predbat):
    messages = []
    my_predbat.log = lambda message, *args, **kwargs: messages.append(message)
    return messages


def _switch_off(my_predbat):
    """Turn the read-only switch off through switch_event, as HA or the web UI would."""
    my_predbat.components = Components(my_predbat)
    my_predbat.update_pending = False
    my_predbat.plan_valid = True
    entity = my_predbat.config_index["set_read_only"]["entity"]
    asyncio.run(my_predbat.switch_event("call_service", {"service": "turn_off", "service_data": {"entity_id": entity}}, {}))


def _test_default_unchanged(my_predbat):
    """No lock: the switch reads back whatever it is set to and turning it off is a real change."""
    failed = False
    item, value, saved = _save(my_predbat)
    try:
        failed |= _check("lock defaults off", my_predbat.read_only_lock, False)
        item["value"] = False
        failed |= _check("default get_arg off", my_predbat.get_arg("set_read_only"), False)
        item["value"] = True
        failed |= _check("default get_arg on", my_predbat.get_arg("set_read_only"), True)

        my_predbat.inverter_needs_reset = False
        my_predbat.inverter_needs_reset_force = ""
        my_predbat.expose_config("set_read_only", False, event=True)
        failed |= _check("default switch off", item["value"], False)
        failed |= _check("default switch off resets inverter", my_predbat.inverter_needs_reset, True)
        failed |= _check("default switch off forces reset", my_predbat.inverter_needs_reset_force, "set_read_only")

        # Turning the switch off through switch_event is a real change and asks for a replan
        item["value"] = True
        messages = _capture_log(my_predbat)
        _switch_off(my_predbat)
        failed |= _check("default switch_event off", item["value"], False)
        failed |= _check("default switch_event replan", (my_predbat.update_pending, my_predbat.plan_valid), (True, False))
        failed |= _check("default no lock log", LOCK_LOG in messages, False)
    finally:
        _restore(my_predbat, item, value, saved)
    return failed


def _test_lock_get_arg(my_predbat):
    """Lock on: the arg reads True whatever the switch holds, other items are untouched."""
    failed = False
    item, value, saved = _save(my_predbat)
    try:
        my_predbat.read_only_lock = True
        item["value"] = False
        failed |= _check("locked get_arg", my_predbat.get_arg("set_read_only"), True)
        failed |= _check("locked get_ha_config", my_predbat.get_ha_config("set_read_only", None)[0], True)
        item["value"] = None
        failed |= _check("locked get_arg unset", my_predbat.get_arg("set_read_only"), True)
        failed |= _check("other item unaffected", my_predbat.get_arg("debug_enable"), my_predbat.get_ha_config("debug_enable", None)[0])
    finally:
        _restore(my_predbat, item, value, saved)
    return failed


def _test_lock_expose_config(my_predbat):
    """Lock on: a UI/API request to turn the switch off is a no-op and does not reset the inverter."""
    failed = False
    item, value, saved = _save(my_predbat)
    try:
        my_predbat.read_only_lock = True
        item["value"] = True
        my_predbat.inverter_needs_reset = False
        my_predbat.inverter_needs_reset_force = ""
        messages = _capture_log(my_predbat)
        my_predbat.expose_config("set_read_only", False, event=True)
        failed |= _check("locked refusal logged once", messages.count(LOCK_LOG), 1)
        failed |= _check("locked switch stays on", item["value"], True)
        failed |= _check("locked switch off no reset", my_predbat.inverter_needs_reset, False)
        failed |= _check("locked switch off no forced reset", my_predbat.inverter_needs_reset_force, "")

        # Unset value (first start) is published as on
        item["value"] = None
        my_predbat.expose_config("set_read_only", False)
        failed |= _check("locked first publish on", item["value"], True)

        # Turning it on is not a refusal, so nothing is logged
        messages = _capture_log(my_predbat)
        my_predbat.expose_config("set_read_only", True)
        failed |= _check("locked turn on not logged", messages.count(LOCK_LOG), 0)

        # switch_event off is refused without a replan
        item["value"] = True
        my_predbat.inverter_needs_reset = False
        my_predbat.inverter_needs_reset_force = ""
        messages = _capture_log(my_predbat)
        _switch_off(my_predbat)
        failed |= _check("locked switch_event stays on", item["value"], True)
        failed |= _check("locked switch_event no replan", (my_predbat.update_pending, my_predbat.plan_valid), (False, True))
        failed |= _check("locked switch_event no reset", (my_predbat.inverter_needs_reset, my_predbat.inverter_needs_reset_force), (False, ""))
        failed |= _check("locked switch_event refusal logged", messages.count(LOCK_LOG), 1)
    finally:
        _restore(my_predbat, item, value, saved)
    return failed


def _test_lock_fetch_config_options(my_predbat):
    """Lock on: fetch_config_options keeps set_read_only True and the Axle branch is not taken."""
    failed = False
    item, value, saved = _save(my_predbat)
    names = ("get_arg", "manual_times", "manual_rates", "api_select_update", "get_state_wrapper", "update_save_restore_list", "expose_config", "args")
    originals = {name: getattr(my_predbat, name) for name in names}
    in_dict = {name: name in my_predbat.__dict__ for name in names}

    mock_config = MockConfigProvider()
    mock_config.config["axle_session"] = "binary_sensor.predbat_axle_event"
    mock_config.config["axle_control"] = True
    real_get_arg = originals["get_arg"]

    def get_arg(key, default=None, index=None, indirect=True, required_unit=None):
        # set_read_only goes through the real get_arg / get_ha_config path
        if key == "set_read_only":
            return real_get_arg(key, default)
        return mock_config.get_arg(key, default, index=index, indirect=indirect, required_unit=required_unit)

    def get_state_wrapper(entity_id, default=None, attribute=None):
        if entity_id == "predbat.status":
            return "Demand"
        if entity_id == "binary_sensor.predbat_axle_event":
            return "on"
        return default

    try:
        my_predbat.get_arg = get_arg
        my_predbat.manual_times = lambda key: []
        my_predbat.manual_rates = lambda key, default_rate=None: {}
        my_predbat.api_select_update = lambda key: []
        my_predbat.get_state_wrapper = get_state_wrapper
        my_predbat.update_save_restore_list = lambda: None
        my_predbat.expose_config = lambda key, value: None
        my_predbat.args = {
            "battery_charge_power_curve": {},
            "battery_charge_power_curve_default": {},
            "battery_discharge_power_curve": {},
            "battery_discharge_power_curve_default": {},
            "battery_temperature_charge_curve": {},
            "battery_temperature_discharge_curve": {},
        }

        # Without the lock the switch is off, so the active Axle event turns read-only on
        item["value"] = False
        my_predbat.fetch_config_options()
        failed |= _check("unlocked axle read only", my_predbat.set_read_only, True)
        failed |= _check("unlocked axle flag", my_predbat.set_read_only_axle, True)

        # With the lock the switch value is ignored and Axle does not claim read-only
        my_predbat.read_only_lock = True
        my_predbat.fetch_config_options()
        failed |= _check("locked fetch read only", my_predbat.set_read_only, True)
        failed |= _check("locked fetch axle flag", my_predbat.set_read_only_axle, False)
    finally:
        for name in names:
            if in_dict[name]:
                setattr(my_predbat, name, originals[name])
            else:
                my_predbat.__dict__.pop(name, None)
        _restore(my_predbat, item, value, saved)
    return failed


def test_read_only_lock(my_predbat):
    """
    Test the optional read_only_lock that holds set_read_only on
    """
    print("**** Testing read_only_lock ****")
    failed = False
    failed |= _test_default_unchanged(my_predbat)
    failed |= _test_lock_get_arg(my_predbat)
    failed |= _test_lock_expose_config(my_predbat)
    failed |= _test_lock_fetch_config_options(my_predbat)
    failed |= _check("lock cleared after tests", my_predbat.read_only_lock, False)
    return failed

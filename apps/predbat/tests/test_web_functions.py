# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from web import WebInterface


def make_web(my_predbat):
    """Create a WebInterface instance bound to the given predbat."""
    return WebInterface(my_predbat, web_port=5052)


def set_entity(my_predbat, entity_id, state=None, **attributes):
    """Set entity state and attributes in the HA mock."""
    entry = dict(attributes)
    if state is not None:
        entry["state"] = state
    my_predbat.ha_interface.dummy_items[entity_id] = entry


def run_web_functions_tests(my_predbat):
    """Unit tests for web.py helper functions."""
    failed = 0
    print("**** Running web functions tests ****")

    web = make_web(my_predbat)
    prefix = my_predbat.prefix

    charging_entity = "binary_sensor." + prefix + "_charging"
    exporting_entity = "binary_sensor." + prefix + "_exporting"
    soc_entity = prefix + ".soc_kw"

    def set_soc(soc_now, soc_max):
        set_entity(my_predbat, soc_entity, state=str(soc_now), soc_now=soc_now, soc_max=soc_max)

    def set_charging(on):
        set_entity(my_predbat, charging_entity, state="on" if on else "off")

    def set_exporting(on):
        set_entity(my_predbat, exporting_entity, state="on" if on else "off")

    original_dashboard_index = my_predbat.dashboard_index

    # -------------------------------------------------------------------------
    print("Test: no dashboard_index returns sync icon")
    my_predbat.dashboard_index = []
    result = web.get_battery_status_icon()
    if "battery-sync" not in result:
        print(f"  ERROR: expected battery-sync icon, got: {result}")
        failed += 1

    # Activate dashboard for remaining tests
    my_predbat.dashboard_index = [prefix + ".status"]
    set_charging(False)
    set_exporting(False)

    # -------------------------------------------------------------------------
    print("Test: 50% SOC idle shows battery-50")
    set_soc(5.0, 10.0)
    result = web.get_battery_status_icon()
    if "mdi-battery-50" not in result:
        print(f"  ERROR: expected battery-50, got: {result}")
        failed += 1
    if "transmission-tower-export" in result:
        print(f"  ERROR: unexpected export icon, got: {result}")
        failed += 1
    if "50%" not in result:
        print(f"  ERROR: expected '50%' in result, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 0% SOC idle shows battery-outline")
    set_soc(0.0, 10.0)
    result = web.get_battery_status_icon()
    if "mdi-battery-outline" not in result:
        print(f"  ERROR: expected battery-outline, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 100% SOC idle shows plain battery")
    set_soc(10.0, 10.0)
    result = web.get_battery_status_icon()
    if 'mdi-battery"' not in result:
        print(f"  ERROR: expected plain mdi-battery, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 50% SOC charging shows battery-charging-50")
    set_soc(5.0, 10.0)
    set_charging(True)
    result = web.get_battery_status_icon()
    if "mdi-battery-charging-50" not in result:
        print(f"  ERROR: expected battery-charging-50, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 0% SOC charging shows battery-charging-outline")
    set_soc(0.0, 10.0)
    set_charging(True)
    result = web.get_battery_status_icon()
    if "mdi-battery-charging-outline" not in result:
        print(f"  ERROR: expected battery-charging-outline, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: exporting appends export icon")
    set_soc(5.0, 10.0)
    set_charging(False)
    set_exporting(True)
    result = web.get_battery_status_icon()
    if "transmission-tower-export" not in result:
        print(f"  ERROR: expected transmission-tower-export icon, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: not exporting omits export icon")
    set_exporting(False)
    result = web.get_battery_status_icon()
    if "transmission-tower-export" in result:
        print(f"  ERROR: unexpected export icon when not exporting, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 30% SOC rounds to nearest 10 (battery-30)")
    set_soc(3.0, 10.0)
    set_charging(False)
    result = web.get_battery_status_icon()
    if "mdi-battery-30" not in result:
        print(f"  ERROR: expected battery-30 for 30% SOC, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 34% SOC rounds down to battery-30")
    set_soc(3.4, 10.0)
    set_charging(False)
    result = web.get_battery_status_icon()
    if "mdi-battery-30" not in result:
        print(f"  ERROR: expected battery-30 for 34%, got: {result}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 36% SOC rounds up to battery-40")
    set_soc(3.6, 10.0)
    set_charging(False)
    result = web.get_battery_status_icon()
    if "mdi-battery-40" not in result:
        print(f"  ERROR: expected battery-40 for 36%, got: {result}")
        failed += 1

    my_predbat.dashboard_index = original_dashboard_index

    print("**** Web functions tests completed ****")
    return failed

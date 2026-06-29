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

    # -------------------------------------------------------------------------
    # Currency unit display in the web config pages (issue #4071)
    # The web UI must show the user's configured currency symbol, not the raw "p".
    failed += run_currency_unit_tests(my_predbat, web)

    print("**** Web functions tests completed ****")
    return failed


def run_currency_unit_tests(my_predbat, web):
    """Verify config item units are converted to the user's currency symbols in the web UI."""
    failed = 0
    print("Test: web config pages convert currency units (issue #4071)")

    original_symbols = my_predbat.currency_symbols
    original_num_cars = my_predbat.num_cars

    try:
        my_predbat.currency_symbols = ["€", "c"]
        my_predbat.num_cars = 1

        # convert_currency_unit helper
        if my_predbat.convert_currency_unit("p") != "c":
            print(f"  ERROR: 'p' should convert to 'c', got: {my_predbat.convert_currency_unit('p')}")
            failed += 1
        if my_predbat.convert_currency_unit("p/kWh") != "c/kWh":
            print(f"  ERROR: 'p/kWh' should convert to 'c/kWh', got: {my_predbat.convert_currency_unit('p/kWh')}")
            failed += 1
        if my_predbat.convert_currency_unit("£") != "€":
            print(f"  ERROR: '£' should convert to '€', got: {my_predbat.convert_currency_unit('£')}")
            failed += 1
        if my_predbat.convert_currency_unit("kWh") != "kWh":
            print(f"  ERROR: 'kWh' should be unchanged, got: {my_predbat.convert_currency_unit('kWh')}")
            failed += 1
        if my_predbat.convert_currency_unit("") != "":
            print(f"  ERROR: empty unit should stay empty, got: {my_predbat.convert_currency_unit('')}")
            failed += 1

        # Enable and locate the car charging max price config item
        entity = None
        original_item_value = None
        original_item_ref = None
        for item in my_predbat.CONFIG_ITEMS:
            if item.get("name") == "car_charging_plan_max_price":
                original_item_ref = item
                original_item_value = item.get("value", None)
                item["value"] = 14
                entity = item.get("entity")
                break

        if entity is None:
            print("  ERROR: car_charging_plan_max_price config item not found")
            return failed + 1

        # html_config_item_text (shown on the /entity page) must use the converted unit
        item_html = web.html_config_item_text(entity)
        if item_html is None:
            print("  ERROR: html_config_item_text returned None for car_charging_plan_max_price")
            failed += 1
        else:
            if "14 c" not in item_html:
                print(f"  ERROR: expected '14 c' in config item HTML, got: {item_html}")
                failed += 1
            if "14 p" in item_html:
                print(f"  ERROR: unexpected raw 'p' unit in config item HTML: {item_html}")
                failed += 1
    finally:
        if original_item_ref is not None:
            original_item_ref["value"] = original_item_value
        my_predbat.currency_symbols = original_symbols
        my_predbat.num_cars = original_num_cars

    return failed

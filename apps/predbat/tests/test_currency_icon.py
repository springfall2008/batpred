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
Tests that the icon on currency-valued config items follows the primary
currency symbol (currency_symbols[0] in apps.yaml).
"""

import copy

from config import (
    CONFIG_ITEMS,
    CURRENCY_ICON_DEFAULT,
    CURRENCY_ICON_PLACEHOLDER,
    CURRENCY_ICONS,
    CURRENCY_ITEM_UNITS,
    apply_currency_icon,
    currency_icon,
)


def test_currency_icon_lookup(my_predbat):
    """
    Known symbols map to their own icon, unknown symbols fall back to the generic icon
    """
    print("**** test_currency_icon_lookup ****")

    assert currency_icon("£") == "mdi:currency-gbp", "GBP icon wrong: {}".format(currency_icon("£"))
    assert currency_icon("€") == "mdi:currency-eur", "EUR icon wrong: {}".format(currency_icon("€"))
    assert currency_icon("$") == "mdi:currency-usd", "USD icon wrong: {}".format(currency_icon("$"))
    assert currency_icon("R$") == "mdi:currency-brl", "BRL icon wrong: {}".format(currency_icon("R$"))
    print("✓ Known symbols map to their own icon")

    assert currency_icon(" £ ") == "mdi:currency-gbp", "Surrounding whitespace should be ignored"
    print("✓ Surrounding whitespace is ignored")

    for symbol in ["kr", "zł", "", None, "??"]:
        assert currency_icon(symbol) == CURRENCY_ICON_DEFAULT, "Symbol {!r} should fall back to {}, got {}".format(symbol, CURRENCY_ICON_DEFAULT, currency_icon(symbol))
    print("✓ Unknown symbols fall back to the default icon")

    for symbol, icon in CURRENCY_ICONS.items():
        assert icon.startswith("mdi:"), "Icon for {} is not an mdi icon: {}".format(symbol, icon)
    print("✓ Every mapped icon is an mdi: icon")

    print("✓ Test passed: currency icon lookup works correctly")
    return False


def test_apply_currency_icon(my_predbat):
    """
    Only items that carry the placeholder icon AND are priced in the minor currency unit have the icon updated
    """
    print("**** test_apply_currency_icon ****")

    items = [
        {"name": "minor", "unit": "p", "icon": CURRENCY_ICON_PLACEHOLDER},
        {"name": "minor_per_kwh", "unit": "p/kWh", "icon": CURRENCY_ICON_PLACEHOLDER},
        {"name": "energy_kwh", "unit": "kWh", "icon": CURRENCY_ICON_PLACEHOLDER},
        {"name": "duration", "unit": "minutes", "icon": CURRENCY_ICON_PLACEHOLDER},
        {"name": "other_icon", "unit": "p", "icon": "mdi:flash"},
        {"name": "no_unit", "icon": CURRENCY_ICON_PLACEHOLDER},
        {"name": "no_icon", "unit": "p"},
    ]
    apply_currency_icon(items, "€")
    by_name = {item["name"]: item for item in items}

    assert by_name["minor"]["icon"] == "mdi:currency-eur", "p item should have icon updated, got {}".format(by_name["minor"]["icon"])
    assert by_name["minor_per_kwh"]["icon"] == "mdi:currency-eur", "p/kWh item should have icon updated, got {}".format(by_name["minor_per_kwh"]["icon"])
    print("✓ p and p/kWh items have had icons updated")

    assert by_name["energy_kwh"]["icon"] == CURRENCY_ICON_PLACEHOLDER, "kWh item must keep its icon, got {}".format(by_name["energy_kwh"]["icon"])
    assert by_name["duration"]["icon"] == CURRENCY_ICON_PLACEHOLDER, "minutes item must keep its icon, got {}".format(by_name["duration"]["icon"])
    assert by_name["no_unit"]["icon"] == CURRENCY_ICON_PLACEHOLDER, "item with no unit must keep its icon, got {}".format(by_name["no_unit"]["icon"])
    print("✓ Non-currency units are left alone (safeguard)")

    assert by_name["other_icon"]["icon"] == "mdi:flash", "item without the placeholder icon must be untouched, got {}".format(by_name["other_icon"]["icon"])
    assert "icon" not in by_name["no_icon"], "item with no icon must not gain one"
    print("✓ Items without the placeholder icon are untouched")

    # An unknown symbol should use the generic icon
    items = [{"name": "minor", "unit": "p", "icon": CURRENCY_ICON_PLACEHOLDER}]
    apply_currency_icon(items, "kr")
    assert items[0]["icon"] == CURRENCY_ICON_DEFAULT, "Unknown symbol should use default icon, got {}".format(items[0]["icon"])
    print("✓ Unknown symbol uses the generic icon")

    print("✓ Test passed: apply_currency_icon only touches currency items")
    return False


def test_apply_currency_icon_real_config(my_predbat):
    """
    Check against the real CONFIG_ITEMS list: every eligible item follows the symbol, nothing else changes,
    and the module-level list is not modified
    """
    print("**** test_apply_currency_icon_real_config ****")

    original_icons = [item.get("icon") for item in CONFIG_ITEMS]
    items = copy.deepcopy(CONFIG_ITEMS)
    apply_currency_icon(items, "€")

    changed = 0
    for before_icon, after in zip(original_icons, items):
        eligible = before_icon == CURRENCY_ICON_PLACEHOLDER and after.get("unit") in CURRENCY_ITEM_UNITS
        if eligible:
            assert after["icon"] == "mdi:currency-eur", "{} should have icon updated, got {}".format(after["name"], after["icon"])
            changed += 1
        else:
            assert after.get("icon") == before_icon, "{} icon should be unchanged, got {}".format(after["name"], after.get("icon"))
    assert changed > 0, "No currency items found in CONFIG_ITEMS - placeholder or unit guard out of date?"
    print("✓ {} currency items icons updated, all other item icons unchanged".format(changed))

    assert [item.get("icon") for item in CONFIG_ITEMS] == original_icons, "Module-level CONFIG_ITEMS must not be modified"
    print("✓ Module-level CONFIG_ITEMS is not modified")

    print("✓ Test passed: real CONFIG_ITEMS handled correctly")
    return False


def test_currency_icon_reaches_ha(my_predbat):
    """
    Check the updated icon value is what expose_config writes to the HA entity attributes
    """
    print("**** test_currency_icon_reaches_ha ****")

    # Use an item with no enable condition: expert_mode items are not published when expert mode is off
    name = "manual_import_value"
    item = my_predbat.config_index.get(name)
    assert item is not None, "{} config item not found".format(name)
    assert item.get("unit") in CURRENCY_ITEM_UNITS, "{} is no longer a currency item (unit {})".format(name, item.get("unit"))
    assert my_predbat.user_config_item_enabled(item), "{} must be enabled for this test".format(name)
    entity = item.get("entity") or "input_number.predbat_" + name

    saved_icon = item.get("icon")
    saved_value = item.get("value", item.get("default"))
    try:
        # Put the item back to the placeholder, then apply a different currency to the live item
        item["icon"] = CURRENCY_ICON_PLACEHOLDER
        apply_currency_icon([item], "€")
        assert item["icon"] == "mdi:currency-eur", "Live item should have the EUR icon, got {}".format(item["icon"])

        my_predbat.expose_config(name, item.get("default"), force_ha=True)
        state = my_predbat.ha_interface.dummy_items.get(entity)
        assert isinstance(state, dict), "Expected a dict for {} in dummy_items, got {!r}".format(entity, state)
        icon = state.get("icon")
        assert icon == "mdi:currency-eur", "HA entity icon should be mdi:currency-eur, got {!r}; stored state was {!r}".format(icon, state)
        print("✓ expose_config writes the updated icon to the HA entity")
    finally:
        item["icon"] = saved_icon
        my_predbat.expose_config(name, saved_value, force_ha=True)

    print("✓ Test passed: currency icon reaches the HA entity")
    return False


def test_predbat_currency_icon_attribute(my_predbat):
    """
    Check the instance-level icon used by the output/plan dashboard writes follows the primary currency symbol
    """
    print("**** test_predbat_currency_icon_attribute ****")

    assert hasattr(my_predbat, "currency_icon"), "my_predbat.currency_icon not set - is it assigned in reset()?"
    expected = currency_icon(my_predbat.currency_symbols[0])
    assert my_predbat.currency_icon == expected, "currency_icon should be {} for symbol {!r}, got {}".format(expected, my_predbat.currency_symbols[0], my_predbat.currency_icon)
    print("✓ my_predbat.currency_icon matches the primary currency symbol ({})".format(expected))

    print("✓ Test passed: currency_icon attribute is set from currency_symbols")
    return False

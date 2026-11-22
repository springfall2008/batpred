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
from datetime import timedelta

from config import (
    CONFIG_ITEMS,
    CURRENCY_ICON_DEFAULT,
    CURRENCY_ICON_PLACEHOLDER,
    CURRENCY_ICONS,
    CURRENCY_UNITS,
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
        {"name": "minor_per_kg", "unit": "p/kg", "icon": CURRENCY_ICON_PLACEHOLDER},
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
    assert by_name["minor_per_kg"]["icon"] == "mdi:currency-eur", "p/kg item should have icon updated, got {}".format(by_name["minor_per_kg"]["icon"])
    print("✓ p, p/kWh and p/kg items have had icons updated")

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
        eligible = before_icon == CURRENCY_ICON_PLACEHOLDER and after.get("unit") in CURRENCY_UNITS
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
    assert item.get("unit") in CURRENCY_UNITS, "{} is no longer a currency item (unit {})".format(name, item.get("unit"))
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


def _make_predbat_with_currency(symbols):
    """Build a PredBat the way create_predbat() does, but with currency_symbols set in apps.yaml before reset()."""
    from predbat import PredBat
    from tests.test_infra import TestHAInterface

    predbat = PredBat()
    predbat.states = {}
    predbat.args["currency_symbols"] = symbols
    predbat.reset()
    predbat.update_time()
    # Pin the clock as create_predbat() does, so the cost loops below always cover part of a day
    predbat.minutes_now = 12 * 60
    predbat.now_utc = predbat.midnight_utc + timedelta(minutes=predbat.minutes_now)
    predbat.ha_interface = TestHAInterface()
    predbat.ha_interface.base = predbat
    predbat.ha_interface.history_enable = False
    predbat.auto_config()
    predbat.load_user_config()
    predbat.fetch_config_options()
    predbat.ha_interface.history_enable = True
    return predbat


def test_currency_icon_end_to_end(my_predbat):
    """
    With currency_symbols set to euro in apps.yaml, the icon reaches HA through Predbat's own start-up and
    output code, with nothing in the test applying it: the money input_numbers get mdi:currency-eur when
    reset() and load_user_config() publish them, and an output money sensor (predbat.cost_today),
    Predheat's cost_today and its predicted cost sensors use the same icon. Reverting the wiring in reset(), or any of those output
    icons back to mdi:currency-usd, fails this test.
    """
    print("**** test_currency_icon_end_to_end ****")

    predbat = _make_predbat_with_currency(["€", "c"])
    assert predbat.currency_icon == "mdi:currency-eur", "currency_icon should be mdi:currency-eur, got {}".format(predbat.currency_icon)

    # A money input_number, as published by load_user_config() at start-up
    name = "manual_import_value"
    item = predbat.config_index.get(name)
    assert item is not None, "{} config item not found".format(name)
    assert item.get("icon") == "mdi:currency-eur", "{} config item icon should be mdi:currency-eur, got {}".format(name, item.get("icon"))
    entity = item.get("entity") or "input_number.predbat_" + name
    state = predbat.ha_interface.dummy_items.get(entity)
    assert isinstance(state, dict), "Expected a dict for {} in dummy_items, got {!r}".format(entity, state)
    assert state.get("icon") == "mdi:currency-eur", "{} HA icon should be mdi:currency-eur, got {!r}".format(entity, state.get("icon"))
    print("✓ Money input_number published at start-up with the euro icon")

    # A non-money input_number keeps its own icon
    assert predbat.config_index.get("manual_load_value", {}).get("icon") != "mdi:currency-eur", "manual_load_value (kWh) must not get the currency icon"
    print("✓ Non-money input_number left alone")

    # An output money sensor
    energy = {minute: 0.01 * (predbat.minutes_now - minute) for minute in range(0, predbat.minutes_now + 1)}
    predbat.today_cost(energy, {}, None, energy, save=True)
    cost_today = predbat.dashboard_values.get(predbat.prefix + ".cost_today", {})
    icon = cost_today.get("attributes", {}).get("icon")
    assert icon == "mdi:currency-eur", "predbat.cost_today icon should be mdi:currency-eur, got {!r}".format(icon)
    assert cost_today["attributes"].get("unit_of_measurement") == "c", "predbat.cost_today unit should be c, got {!r}".format(cost_today["attributes"].get("unit_of_measurement"))
    print("✓ predbat.cost_today published with the euro icon and minor unit")

    # Predheat's cost today
    from predheat import PredHeat

    published = {}
    predheat = PredHeat(predbat)
    predheat.prefix = predbat.prefix
    predheat.midnight_utc = predbat.midnight_utc
    predheat.minutes_now = predbat.minutes_now
    predheat.set_state = lambda entity_id, state=None, attributes=None: published.update({entity_id: attributes or {}})
    predheat.rate_import = {minute: 10.0 for minute in range(-24 * 60, 2 * 24 * 60)}
    predheat.today_cost(energy)
    attributes = published.get(predbat.prefix + ".cost_today", {})
    assert attributes.get("icon") == "mdi:currency-eur", "Predheat cost_today icon should be mdi:currency-eur, got {!r}".format(attributes.get("icon"))
    assert attributes.get("unit_of_measurement") == "c", "Predheat cost_today unit should be c, got {!r}".format(attributes.get("unit_of_measurement"))
    print("✓ Predheat cost_today published with the euro icon and minor unit")

    # Predheat's predicted cost sensors from run_simulation()
    from tests.test_predheat import BASE_TEMP, _make_simulation

    simulation = _make_simulation(predbat, [])
    simulation.run_simulation(BASE_TEMP, True)
    for suffix in ["cost", "cost_h1", "cost_h2", "cost_h8"]:
        attributes = simulation.published.get(simulation.prefix + "." + suffix, {})
        assert attributes.get("icon") == "mdi:currency-eur", "Predheat {} icon should be mdi:currency-eur, got {!r}".format(suffix, attributes.get("icon"))
        assert attributes.get("unit_of_measurement") == "c", "Predheat {} unit should be c, got {!r}".format(suffix, attributes.get("unit_of_measurement"))
    print("✓ Predheat cost, cost_h1, cost_h2 and cost_h8 published with the euro icon and minor unit")

    print("✓ Test passed: currency icon wired end to end")
    return False

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the performance_tweaks toggle.

performance_tweaks works like expert_mode: it is a switch that other config items name in their
"enable" key, and while it is Off those items are hidden. The difference is what hiding them means
here. Everything behind this toggle defaults to On, so a user who never touches it gets the best
plan Predbat can produce; the toggle exists so someone on slow hardware can reveal the switches and
turn features off to buy back CPU time.

That relies on a specific property of the config layer: get_ha_config substitutes an item's default
when the item is disabled, rather than returning None, so get_arg resolves a hidden item to its
default and never reaches its self.args lookup. A hidden option is therefore pinned On for everyone
and cannot be held Off from apps.yaml - which is the behaviour these tests lock down, because it is
what makes the toggle a reliable on-switch and it is also the one sharp edge for users.
"""

# Every config item that performance_tweaks reveals. Each must default to On, so that leaving the
# toggle alone runs the feature.
GATED_ITEMS = ["calculate_second_pass", "calculate_pv90_plan"]


def test_performance_tweaks_item(my_predbat):
    """performance_tweaks must exist as a switch defaulting to Off, with no enable gate of its own."""
    failed = False
    by_name = {item["name"]: item for item in my_predbat.CONFIG_ITEMS}
    item = by_name.get("performance_tweaks")
    if not item:
        print("ERROR: config item performance_tweaks is missing from CONFIG_ITEMS")
        return True
    if item.get("type") != "switch":
        print("ERROR: config item performance_tweaks type is {}, expected switch".format(item.get("type")))
        failed = True
    if item.get("enable") is not None:
        print("ERROR: config item performance_tweaks enable is {}, expected no gating - it is the gate".format(item.get("enable")))
        failed = True
    if item.get("default") is not False:
        print("ERROR: config item performance_tweaks default is {}, expected False so the switches stay hidden by default".format(item.get("default")))
        failed = True
    return failed


def test_performance_tweaks_gated_items_default_on(my_predbat):
    """Everything behind the toggle must be gated on it and default to On."""
    failed = False
    by_name = {item["name"]: item for item in my_predbat.CONFIG_ITEMS}
    for name in GATED_ITEMS:
        item = by_name.get(name)
        if not item:
            print("ERROR: config item {} is missing from CONFIG_ITEMS".format(name))
            failed = True
            continue
        if item.get("enable") != "performance_tweaks":
            print("ERROR: config item {} enable is {}, expected performance_tweaks".format(name, item.get("enable")))
            failed = True
        if item.get("default") is not True:
            print("ERROR: config item {} default is {}, expected True so the feature runs while the toggle is Off".format(name, item.get("default")))
            failed = True
    return failed


def test_performance_tweaks_hides_and_reveals(my_predbat):
    """With the toggle Off the gated items are disabled and resolve to their default; On, they are live.

    The Off case asserts through get_arg as well as user_config_item_enabled, because resolving a hidden
    item to its default is the whole mechanism - if get_ha_config ever returned None for a disabled item
    instead, get_arg would fall through to apps.yaml and the features would silently stop defaulting On.
    """
    failed = False
    saved_perf = my_predbat.config_index["performance_tweaks"].get("value")
    saved_values = {name: my_predbat.config_index[name].get("value") for name in GATED_ITEMS}
    try:
        # Toggle Off: hidden, and pinned to the default regardless of anything else
        my_predbat.config_index["performance_tweaks"]["value"] = False
        for name in GATED_ITEMS:
            my_predbat.config_index[name]["value"] = False
            if my_predbat.user_config_item_enabled(my_predbat.config_index[name]):
                print("ERROR: {} is enabled with performance_tweaks Off, expected it to be hidden".format(name))
                failed = True
            if my_predbat.get_arg(name) is not True:
                print("ERROR: hidden {} resolved to {}, expected True - a hidden item must run at its default".format(name, my_predbat.get_arg(name)))
                failed = True

        # Toggle On: revealed, and the user's own value is honoured
        my_predbat.config_index["performance_tweaks"]["value"] = True
        for name in GATED_ITEMS:
            if not my_predbat.user_config_item_enabled(my_predbat.config_index[name]):
                print("ERROR: {} is disabled with performance_tweaks On, expected it to be revealed".format(name))
                failed = True
            if my_predbat.get_arg(name) is not False:
                print("ERROR: revealed {} resolved to {}, expected the configured False to win".format(name, my_predbat.get_arg(name)))
                failed = True
            my_predbat.config_index[name]["value"] = True
            if my_predbat.get_arg(name) is not True:
                print("ERROR: revealed {} resolved to {} after being switched back On, expected True".format(name, my_predbat.get_arg(name)))
                failed = True
    finally:
        my_predbat.config_index["performance_tweaks"]["value"] = saved_perf
        for name, value in saved_values.items():
            my_predbat.config_index[name]["value"] = value
    return failed


def test_performance_tweaks_drives_second_pass(my_predbat):
    """fetch_config_options must read calculate_second_pass through the gate, not from a stale attribute.

    Hidden it must come out On (the default); revealed and switched Off it must come out Off. This is the
    read that decides which planning path optimise_all_windows takes.
    """
    failed = False
    saved_state = my_predbat.__dict__.copy()
    saved_perf = my_predbat.config_index["performance_tweaks"].get("value")
    saved_switch = my_predbat.config_index["calculate_second_pass"].get("value")
    try:
        my_predbat.config_index["performance_tweaks"]["value"] = False
        my_predbat.config_index["calculate_second_pass"]["value"] = False
        my_predbat.fetch_config_options()
        if my_predbat.calculate_second_pass is not True:
            print("ERROR: calculate_second_pass is {} with performance_tweaks Off, expected the default True".format(my_predbat.calculate_second_pass))
            failed = True

        my_predbat.config_index["performance_tweaks"]["value"] = True
        my_predbat.fetch_config_options()
        if my_predbat.calculate_second_pass is not False:
            print("ERROR: calculate_second_pass is {} after being revealed and switched Off, expected False".format(my_predbat.calculate_second_pass))
            failed = True
    finally:
        my_predbat.config_index["performance_tweaks"]["value"] = saved_perf
        my_predbat.config_index["calculate_second_pass"]["value"] = saved_switch
        my_predbat.__dict__.clear()
        my_predbat.__dict__.update(saved_state)
    return failed


def run_performance_tweaks_tests(my_predbat):
    """Run the performance_tweaks toggle tests and return True on failure."""
    print("**** Running performance tweaks tests ****")
    failed = False
    failed |= test_performance_tweaks_item(my_predbat)
    failed |= test_performance_tweaks_gated_items_default_on(my_predbat)
    failed |= test_performance_tweaks_hides_and_reveals(my_predbat)
    failed |= test_performance_tweaks_drives_second_pass(my_predbat)
    if failed:
        print("**** Performance tweaks tests FAILED ****")
    else:
        print("**** Performance tweaks tests passed ****")
    return failed

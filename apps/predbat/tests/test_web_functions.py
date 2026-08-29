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
import os

from web import WebInterface
from web_helper import get_plan_renderer_js


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
    # Last Started/Last Updated date format consistency (issue #4223)
    print("Test: Last Started date is displayed in the same format as Last Updated")
    status_entity = prefix + ".status"
    last_started_entity = prefix + ".last_started"
    set_entity(my_predbat, status_entity, state="Idle", last_updated="2026-07-10 14:40:10.512590")
    set_entity(my_predbat, last_started_entity, state="2026-07-10T01:10:39+0100")
    my_predbat.dashboard_index = [status_entity]
    status_html = web.get_status_html("1.0")
    my_predbat.dashboard_index = original_dashboard_index
    if "2026-07-10 01:10:39" not in status_html:
        print(f"  ERROR: expected 'Last Started' to be reformatted without 'T'/timezone offset, got: {status_html}")
        failed += 1
    if "2026-07-10T01:10:39+0100" in status_html:
        print(f"  ERROR: 'Last Started' should not show the raw stored ISO format, got: {status_html}")
        failed += 1

    # -------------------------------------------------------------------------
    # is_running() must handle both the legacy naive last_updated format (pre-existing
    # installs, before record_status() started writing a timezone-aware value) and the
    # current timezone-aware format, without raising on the naive/aware datetime subtraction
    print("Test: is_running() handles both naive and timezone-aware last_updated")
    original_stop_thread = my_predbat.stop_thread
    original_fatal_error = my_predbat.fatal_error
    my_predbat.stop_thread = False
    my_predbat.fatal_error = False
    my_predbat.dashboard_index = [status_entity]

    set_entity(my_predbat, status_entity, state="Idle", error=False, last_updated="2026-07-10 14:40:10.512590")
    try:
        result = my_predbat.is_running()
    except TypeError as e:
        print(f"  ERROR: is_running() raised on a legacy naive last_updated: {e}")
        failed += 1
    else:
        if result is not False:
            print(f"  ERROR: expected is_running() False for a stale (2026-07-10) naive last_updated, got: {result}")
            failed += 1

    set_entity(my_predbat, status_entity, state="Idle", error=False, last_updated="2026-07-10T14:40:10+0100")
    try:
        result = my_predbat.is_running()
    except TypeError as e:
        print(f"  ERROR: is_running() raised on a timezone-aware last_updated: {e}")
        failed += 1
    else:
        if result is not False:
            print(f"  ERROR: expected is_running() False for a stale (2026-07-10) aware last_updated, got: {result}")
            failed += 1

    my_predbat.dashboard_index = original_dashboard_index
    my_predbat.stop_thread = original_stop_thread
    my_predbat.fatal_error = original_fatal_error

    # -------------------------------------------------------------------------
    # Currency unit display in the web config pages (issue #4071)
    # The web UI must show the user's configured currency symbol, not the raw "p".
    failed += run_currency_unit_tests(my_predbat, web)

    # -------------------------------------------------------------------------
    # Compare page empty state (issue #4334) - an empty compare_list must not show the
    # "Loading chart (please wait)..." messages forever, since nothing will ever load.
    failed += run_compare_empty_state_tests(my_predbat, web)

    failed += run_plan_empty_state_tests(my_predbat, web)

    print("**** Web functions tests completed ****")
    return failed


def run_compare_empty_state_tests(my_predbat, web):
    """Unit tests for the Compare page's empty-state messaging (issue #4334)."""
    failed = 0
    print("**** Running compare empty state tests ****")

    original_args = my_predbat.args.copy()

    # -------------------------------------------------------------------------
    print("Test: no compare_list configured shows a clear 'not configured' message, not a stuck loading message")
    my_predbat.args.pop("compare_list", None)
    response = asyncio.run(web.html_compare(None))
    text = response.text
    if "No tariffs configured yet" not in text:
        print(f"  ERROR: expected a 'no tariffs configured' message when compare_list is empty")
        failed += 1
    if "Loading chart (please wait)" in text:
        print(f"  ERROR: should not show the stuck 'Loading chart' message when nothing is configured")
        failed += 1
    if "7 day rolling average chart loading (please wait)" in text:
        print(f"  ERROR: should not show the stuck '7 day rolling average' message when nothing is configured")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a configured but not-yet-computed compare_list keeps the genuine loading message")
    my_predbat.args["compare_list"] = [{"id": "test1", "name": "Test tariff"}]
    response = asyncio.run(web.html_compare(None))
    text = response.text
    if "No tariffs configured yet" in text:
        print(f"  ERROR: should not show the 'no tariffs configured' message when compare_list is set")
        failed += 1
    if "Loading chart (please wait)" not in text:
        print(f"  ERROR: expected the genuine loading message when compare_list is set but not yet computed")
        failed += 1

    my_predbat.args = original_args

    print("**** Compare empty state tests completed ****")
    return failed


def run_plan_empty_state_tests(my_predbat, web):
    """Unit tests for the Plan page's empty-state messaging (issue #4583).

    The History and Yesterday Without Predbat views are only populated once calculate_yesterday()
    has run, which it can't do when Home Assistant has no history for predbat.cost_today. Showing
    'Plan data is loading, please wait...' forever tells the user nothing about why.
    """
    failed = 0
    print("**** Running plan empty state tests ****")

    renderer_js = get_plan_renderer_js()

    # Scope the checks to refreshPlan(), which is what decides what an empty view shows
    start = renderer_js.index("function refreshPlan(")
    end = renderer_js.index("function checkStaleness(", start)
    refresh_plan_src = renderer_js[start:end]

    empty_branch_start = refresh_plan_src.find("if (!data)")
    if empty_branch_start < 0:
        print("  ERROR: expected refreshPlan() to handle a view with no data")
        failed += 1
    else:
        # Slice to the branch's own return so the assertions can't be satisfied by later code
        empty_branch = refresh_plan_src[empty_branch_start : refresh_plan_src.index("return;", empty_branch_start)]

        print("Test: the History / Yesterday views explain themselves instead of loading forever")
        if "currentView" not in empty_branch:
            print("  ERROR: the empty-data branch should distinguish the plan view from the yesterday/baseline views")
            failed += 1
        if "cost_today" not in empty_branch:
            print("  ERROR: the empty-data message should name the predbat.cost_today history it needs")
            failed += 1
        if "recorder" not in empty_branch:
            print("  ERROR: the empty-data message should point at the Home Assistant recorder as the thing to check")
            failed += 1

        print("Test: the plan view itself keeps its genuine loading message")
        if "Plan data is loading" not in empty_branch:
            print("  ERROR: the plan view should still show a loading message while it waits for its first plan")
            failed += 1

    print("Test: the rendered plan page carries the explanation")
    response = asyncio.run(web.html_plan(None))
    if "cost_today" not in response.text:
        print("  ERROR: the plan page should carry the empty-state explanation for the yesterday views")
        failed += 1

    print("**** Plan empty state tests completed ****")
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


class FakeImageRequest:
    """A minimal aiohttp-request stand-in exposing only ``match_info``."""

    def __init__(self, filename=None):
        """Store the route parameter a handler will read."""
        self.match_info = {} if filename is None else {"filename": filename}


def run_web_logo_image_tests(my_predbat):
    """Unit tests for the local logo image route (issue #4562).

    The Predbat logo used to be fetched from raw.githubusercontent.com on every page
    load, so a GitHub outage or rate limit left the dashboard hanging for ~15s. The
    images now ship alongside the app and are served locally, with no network
    dependency at all.
    """
    failed = 0
    print("**** Running web logo image tests ****")

    web = make_web(my_predbat)

    print("Test: each bundled logo file is served with the right content type")
    expected_content_types = {
        "bat_logo.svg": "image/svg+xml",
        "bat_logo_light.png": "image/png",
        "bat_logo_dark.png": "image/png",
    }
    for filename, content_type in expected_content_types.items():
        response = asyncio.run(web.html_logo_image(FakeImageRequest(filename)))
        if response.status != 200:
            print(f"  ERROR: {filename} should serve with status 200, got {response.status}")
            failed += 1
        if response.content_type != content_type:
            print(f"  ERROR: {filename} should serve as {content_type}, got {response.content_type}")
            failed += 1
        if not response.body:
            print(f"  ERROR: {filename} should have a non-empty body")
            failed += 1

    print("Test: an unknown or missing filename 404s rather than leaking the app directory")
    for request in [FakeImageRequest("predbat.py"), FakeImageRequest("../predbat.py"), FakeImageRequest()]:
        response = asyncio.run(web.html_logo_image(request))
        if response.status != 404:
            print(f"  ERROR: request for {request.match_info} should 404, got {response.status}")
            failed += 1

    print("Test: a whitelisted filename whose file is genuinely absent on disk 404s cleanly")
    # Regression for issue #4568: when an install has updated predbat.py before its self-updater has
    # fetched these newly-added logo files (an old download.py running against a new release), the
    # image genuinely won't exist yet. This should degrade to a missing image, not raise.
    filename = "bat_logo_light.png"
    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", filename)
    moved_path = file_path + ".test_moved"
    os.rename(file_path, moved_path)
    try:
        response = asyncio.run(web.html_logo_image(FakeImageRequest(filename)))
        if response.status != 404:
            print(f"  ERROR: {filename} missing from disk should 404, got {response.status}")
            failed += 1
    finally:
        os.rename(moved_path, file_path)

    print("Test: the page header no longer depends on GitHub for the logo")
    from web_helper import get_header_html

    header = get_header_html("Test", False, "./dash", [], "v1.0", "")
    if "githubusercontent" in header:
        print("  ERROR: the page header should not reference githubusercontent.com any more")
        failed += 1
    if "./images/bat_logo" not in header:
        print("  ERROR: the page header should reference the local logo route")
        failed += 1

    print("Test: the dark mode background colour is set before any render-blocking external resource (issue #2256)")
    dark_mode_class_pos = header.find("classList.add('dark-mode')")
    external_resource_pos = header.find("cdn.jsdelivr.net")
    background_style_pos = header.find("background-color: #121212")
    if dark_mode_class_pos < 0 or external_resource_pos < 0 or background_style_pos < 0:
        print("  ERROR: expected to find the dark-mode class script, an external CDN resource, and a dark background-color rule in the header")
        failed += 1
    elif not (dark_mode_class_pos < background_style_pos < external_resource_pos):
        print(
            "  ERROR: the dark-mode background colour must be set before the external CDN font/chart resources "
            "(which block rendering while they load) - otherwise a slow fetch flashes the default white "
            f"background first. Positions: dark-mode class {dark_mode_class_pos}, background colour rule "
            f"{background_style_pos}, external CDN resource {external_resource_pos}"
        )
        failed += 1

    print("**** Web logo image tests completed ****")
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from web import resolve_group_unit_and_name


def run_web_entity_unit_resolution_tests(my_predbat):
    """Unit tests for resolve_group_unit_and_name() - the /entity chart's unit/name grouping."""
    failed = 0
    print("**** Running web entity unit resolution tests ****")

    # -------------------------------------------------------------------------
    print("Test: an entity tracked in dashboard_values groups and labels from its cached attributes")
    dashboard_values = {"number.percent": {"attributes": {"unit_of_measurement": "%", "friendly_name": "AC Charge Upper % Limit"}}}
    unit, friendly_name = resolve_group_unit_and_name("number.percent", dashboard_values)
    if unit != "%":
        print(f"  ERROR: expected unit '%', got '{unit}'")
        failed += 1
    if friendly_name != "AC Charge Upper % Limit":
        print(f"  ERROR: expected friendly name from dashboard_values, got '{friendly_name}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an entity tracked in dashboard_values with no unit falls back to '(no unit)'")
    dashboard_values = {"predbat.status": {"attributes": {"friendly_name": "Status"}}}
    unit, friendly_name = resolve_group_unit_and_name("predbat.status", dashboard_values)
    if unit != "(no unit)":
        print(f"  ERROR: expected '(no unit)', got '{unit}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an entity NOT tracked in dashboard_values uses the caller-supplied live HA lookup")
    unit, friendly_name = resolve_group_unit_and_name("number.gecloud_ac_charge_upper_percent_limit", {}, live_unit="%", live_friendly_name="AC Charge Upper % Limit")
    if unit != "%":
        print(f"  ERROR: expected the live HA unit '%' to be used for an entity Predbat doesn't track, got '{unit}'")
        failed += 1
    if friendly_name != "AC Charge Upper % Limit":
        print(f"  ERROR: expected the live HA friendly name to be used, got '{friendly_name}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an entity NOT tracked in dashboard_values with no live lookup result falls back to '(no unit)' and its id")
    unit, friendly_name = resolve_group_unit_and_name("number.unknown", {}, live_unit=None, live_friendly_name=None)
    if unit != "(no unit)":
        print(f"  ERROR: expected '(no unit)', got '{unit}'")
        failed += 1
    if friendly_name != "number.unknown":
        print(f"  ERROR: expected the entity_id itself as a last-resort name, got '{friendly_name}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: dashboard_values takes priority - the live lookup arguments are ignored when the entity is tracked")
    dashboard_values = {"number.percent": {"attributes": {"unit_of_measurement": "%"}}}
    unit, friendly_name = resolve_group_unit_and_name("number.percent", dashboard_values, live_unit="kWh", live_friendly_name="Wrong Name")
    if unit != "%":
        print(f"  ERROR: expected dashboard_values' unit '%' to win over the live lookup, got '{unit}'")
        failed += 1

    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests that get_arg(..., combine=True) converts each list entry to required_unit before summing.

A per-inverter arg such as inverter_limit is a list, and each entry may be a number or a sensor. combine=True
resolves every entry and sums them, but it resolved the entries without passing required_unit on, so a sensor
reported in kW was added to the total as if it were already in W. Reading one entry by index was unaffected:
the entry is picked out before the unit conversion runs. PV calibration reads the summed inverter limit in W
as its clipping ceiling, where a kW sensor would have made it a thousand times too small.
"""


def test_get_arg_combine_units(my_predbat):
    """Run the get_arg combine unit conversion tests. Returns True on failure."""
    print("**** Testing get_arg combine=True unit conversion ****")
    failed = False
    ha = my_predbat.ha_interface

    # Shared fixture: restore everything this touches, whatever the outcome
    had_arg = "inverter_limit" in my_predbat.args
    original_arg = my_predbat.args.get("inverter_limit")
    sensors = ["sensor.test_combine_limit_kw", "sensor.test_combine_limit_w"]
    original_sensors = {entity_id: ha.dummy_items.get(entity_id) for entity_id in sensors}

    try:
        ha.dummy_items["sensor.test_combine_limit_kw"] = {"state": 6, "unit_of_measurement": "kW"}
        ha.dummy_items["sensor.test_combine_limit_w"] = {"state": 6000, "unit_of_measurement": "W"}

        print("Test: combine with required_unit converts a kW sensor entry before summing")
        my_predbat.args["inverter_limit"] = ["sensor.test_combine_limit_kw", 5500]
        value = my_predbat.get_arg("inverter_limit", 0.0, combine=True, required_unit="W")
        if value != 11500:
            print("  ERROR: expected 6 kW + 5500 W = 11500 W, got {!r}".format(value))
            failed = True

        print("Test: combine with required_unit leaves a sensor already in that unit alone")
        my_predbat.args["inverter_limit"] = ["sensor.test_combine_limit_w", 5500]
        value = my_predbat.get_arg("inverter_limit", 0.0, combine=True, required_unit="W")
        if value != 11500:
            print("  ERROR: expected 6000 W + 5500 W = 11500 W, got {!r}".format(value))
            failed = True

        print("Test: combine without required_unit sums the raw values, as before")
        my_predbat.args["inverter_limit"] = ["sensor.test_combine_limit_kw", 5500]
        value = my_predbat.get_arg("inverter_limit", 0.0, combine=True)
        if value != 5506:
            print("  ERROR: expected the raw 6 + 5500 = 5506 when no unit is asked for, got {!r}".format(value))
            failed = True

        print("Test: reading one entry by index still converts it")
        value = my_predbat.get_arg("inverter_limit", 0.0, index=0, required_unit="W")
        if value != 6000:
            print("  ERROR: expected the kW sensor at index 0 as 6000 W, got {!r}".format(value))
            failed = True
    finally:
        if had_arg:
            my_predbat.args["inverter_limit"] = original_arg
        else:
            my_predbat.args.pop("inverter_limit", None)
        for entity_id, item in original_sensors.items():
            if item is None:
                ha.dummy_items.pop(entity_id, None)
            else:
                ha.dummy_items[entity_id] = item

    if not failed:
        print("**** get_arg combine=True unit conversion tests passed ****")
    return failed

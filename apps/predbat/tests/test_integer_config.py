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
Test that input_number entities with integer steps remain integers
and don't get converted to floats during config refresh.
"""


def test_integer_config_entities(my_predbat):
    """
    Test that entities with step=1 remain integers when loaded/refreshed
    """
    print("**** test_integer_config_entities ****")

    # Test 1: Direct test of the conversion logic for integer step
    # Simulate what load_user_config does

    # Get the config item for holiday_days_left
    item = None
    for config_item in my_predbat.CONFIG_ITEMS:
        if config_item.get("name") == "holiday_days_left":
            item = config_item.copy()
            break

    assert item is not None, "holiday_days_left config item not found"
    assert item.get("step") == 1, f"holiday_days_left step should be 1, got {item.get('step')}"

    # Test conversion logic: integer value with integer step
    ha_value = 2.0  # Simulate float from HA
    step = item.get("step", 1)

    # Apply the conversion logic from load_user_config
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        # Step is an integer, so keep value as integer if it has no decimal part
        if ha_value == int(ha_value):
            ha_value = int(ha_value)

    assert isinstance(ha_value, int), f"Value 2.0 with step=1 should convert to int, got {type(ha_value)}"
    assert ha_value == 2, f"Value should be 2, got {ha_value}"
    print(f"PASS: Float 2.0 with step=1 converts to integer 2")

    # Test 2: String integer like "3" should convert to int
    ha_value = "3"
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)

    assert isinstance(ha_value, int), f"Value '3' with step=1 should convert to int, got {type(ha_value)}"
    assert ha_value == 3, f"Value should be 3, got {ha_value}"
    print(f"PASS: String '3' with step=1 converts to integer 3")

    # Test 3: String float like "4.0" should convert to int for integer step
    ha_value = "4.0"
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)

    assert isinstance(ha_value, int), f"Value '4.0' with step=1 should convert to int, got {type(ha_value)}"
    assert ha_value == 4, f"Value should be 4, got {ha_value}"
    print(f"PASS: String '4.0' with step=1 converts to integer 4")

    # Test 4: Float with decimal part should stay as float even for integer step
    ha_value = 4.5
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)

    assert isinstance(ha_value, float), f"Value 4.5 should remain float, got {type(ha_value)}"
    assert ha_value == 4.5, f"Value should be 4.5, got {ha_value}"
    print(f"PASS: Float 4.5 stays as float 4.5 (has fractional part)")

    # Test 5: Test with decimal step - should always stay as float
    # Find an entity with decimal step
    decimal_item = None
    for config_item in my_predbat.CONFIG_ITEMS:
        if config_item.get("type") == "input_number":
            step_val = config_item.get("step", 1)
            if isinstance(step_val, float) and step_val != int(step_val):
                decimal_item = config_item.copy()
                break

    if decimal_item:
        decimal_step = decimal_item.get("step")
        ha_value = 5.0  # Even whole number should stay as float
        ha_value = float(ha_value)
        if isinstance(decimal_step, int) or (isinstance(decimal_step, float) and decimal_step == int(decimal_step)):
            if ha_value == int(ha_value):
                ha_value = int(ha_value)

        # With decimal step, conversion shouldn't happen
        assert isinstance(ha_value, float), f"Value 5.0 with step={decimal_step} should stay float, got {type(ha_value)}"
        print(f"PASS: Float 5.0 with step={decimal_step} stays as float")
    else:
        print("! No decimal step entity found to test")

    # Test 6: Test set_reserve_min (another integer step entity)
    item = None
    for config_item in my_predbat.CONFIG_ITEMS:
        if config_item.get("name") == "set_reserve_min":
            item = config_item.copy()
            break

    assert item is not None, "set_reserve_min config item not found"
    assert item.get("step") == 1, f"set_reserve_min step should be 1, got {item.get('step')}"

    ha_value = 27.0
    step = item.get("step", 1)
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)

    assert isinstance(ha_value, int), f"set_reserve_min 27.0 should convert to int, got {type(ha_value)}"
    assert ha_value == 27, f"Value should be 27, got {ha_value}"
    print(f"PASS: set_reserve_min: Float 27.0 with step=1 converts to integer 27")

    print("PASS: Test passed: Integer conversion logic works correctly")
    return False


def test_expose_config_preserves_integer(my_predbat):
    """
    Test that expose_config writes integer values correctly
    """
    print("**** test_expose_config_preserves_integer ****")

    # Expose an integer value
    my_predbat.expose_config("holiday_days_left", 5, force_ha=True)

    # Check what was written to HA
    state = my_predbat.ha_interface.dummy_items.get("input_number.predbat_holiday_days_left")
    if isinstance(state, dict):
        state_value = state.get("state")
    else:
        state_value = state

    # State should be integer 5, not float 5.0
    assert isinstance(state_value, int), f"Exposed state should be int, got {type(state_value)}: {state_value}"
    assert state_value == 5, f"Exposed state should be 5, got {state_value}"
    print(f"PASS: expose_config correctly writes integer 5")

    # Test with set_reserve_min
    my_predbat.expose_config("set_reserve_min", 30, force_ha=True)

    state = my_predbat.ha_interface.dummy_items.get("input_number.predbat_set_reserve_min")
    if isinstance(state, dict):
        state_value = state.get("state")
    else:
        state_value = state

    assert isinstance(state_value, int), f"Exposed set_reserve_min should be int, got {type(state_value)}: {state_value}"
    assert state_value == 30, f"Exposed set_reserve_min should be 30, got {state_value}"
    print(f"PASS: expose_config correctly writes integer 30 for set_reserve_min")

    # Test with a float value to ensure floats still work
    # Find an entity with decimal step
    decimal_name = None
    for item in my_predbat.CONFIG_ITEMS:
        if item.get("type") == "input_number":
            step = item.get("step", 1)
            if isinstance(step, float) and step != int(step):
                decimal_name = item["name"]
                break

    if decimal_name:
        my_predbat.expose_config(decimal_name, 12.75, force_ha=True)

        entity = "input_number.predbat_" + decimal_name
        state = my_predbat.ha_interface.dummy_items.get(entity)
        if isinstance(state, dict):
            state_value = state.get("state")
        else:
            state_value = state

        assert isinstance(state_value, float), f"Exposed {decimal_name} should be float, got {type(state_value)}: {state_value}"
        assert state_value == 12.75, f"Exposed {decimal_name} should be 12.75, got {state_value}"
        print(f"PASS: expose_config correctly writes float 12.75 for {decimal_name}")
    else:
        print("! No decimal step entity found to test")

    # Put config back to original state
    for item in my_predbat.CONFIG_ITEMS:
        my_predbat.expose_config(item.get("name"), item.get("default"), force_ha=True)

    print("PASS: Test passed: expose_config preserves type correctly")
    return False


def test_config_item_range_clamp(my_predbat):
    """
    Test that load_user_config clamps input_number config items to their declared min/max
    (e.g. an out-of-range apps.yaml override), rather than passing the raw value straight
    through to the optimiser, and flags the clamp via had_errors.
    """
    print("**** test_config_item_range_clamp ****")

    original_args = my_predbat.args.copy()
    original_had_errors = my_predbat.had_errors

    item = None
    for config_item in my_predbat.CONFIG_ITEMS:
        if config_item.get("name") == "pv_metric10_weight":
            item = config_item
            break

    assert item is not None, "pv_metric10_weight config item not found"
    assert item.get("min") == 0, f"pv_metric10_weight min should be 0, got {item.get('min')}"
    assert item.get("max") == 1.0, f"pv_metric10_weight max should be 1.0, got {item.get('max')}"

    # load_user_config() prefers a value already stored on the HA entity over the apps.yaml
    # default (that's only used on very first load), so simulate a stale/raw out-of-range
    # value already sitting in HA state - exactly how this reaches Predbat in practice (an
    # apps.yaml override gets pushed straight to the entity with no min/max enforcement) -
    # then confirm a subsequent config refresh catches and clamps it.

    # Test 1: a stored value above the declared max should clamp to the max and flag an error
    my_predbat.expose_config("pv_metric10_weight", 30, force_ha=True)
    my_predbat.had_errors = False
    my_predbat.load_user_config()
    assert item["value"] == 1.0, f"Expected clamp to max 1.0, got {item['value']}"
    assert my_predbat.had_errors is True, "Out-of-range config value should flag had_errors"
    print("✓ Above-max value (30) clamped to max (1.0) and flagged")

    # Test 2: a stored value below the declared min should clamp to the min and flag an error
    my_predbat.expose_config("pv_metric10_weight", -5, force_ha=True)
    my_predbat.had_errors = False
    my_predbat.load_user_config()
    assert item["value"] == 0, f"Expected clamp to min 0, got {item['value']}"
    assert my_predbat.had_errors is True, "Out-of-range config value should flag had_errors"
    print("✓ Below-min value (-5) clamped to min (0) and flagged")

    # Test 3: an in-range value should pass through unmodified and not flag an error
    my_predbat.expose_config("pv_metric10_weight", 0.3, force_ha=True)
    my_predbat.had_errors = False
    my_predbat.load_user_config()
    assert item["value"] == 0.3, f"Expected in-range value to pass through as 0.3, got {item['value']}"
    assert my_predbat.had_errors is False, "In-range config value should not flag had_errors"
    print("✓ In-range value (0.3) passes through unclamped")

    # Test 4: an integer-step item with a float-typed max (metric_min_improvement_plan: step=1,
    # max=250.0) must still come out as an int after clamping, not the raw float max - clamping
    # runs before the integer-preservation check, so it needs to re-apply after the clamp rather
    # than just handing back the schema's float boundary value untouched.
    int_item = None
    for config_item in my_predbat.CONFIG_ITEMS:
        if config_item.get("name") == "metric_min_improvement_plan":
            int_item = config_item
            break

    assert int_item is not None, "metric_min_improvement_plan config item not found"
    assert int_item.get("step") == 1, f"metric_min_improvement_plan step should be 1, got {int_item.get('step')}"
    assert int_item.get("max") == 250.0, f"metric_min_improvement_plan max should be 250.0, got {int_item.get('max')}"
    assert int_item.get("enable") == "expert_mode", "metric_min_improvement_plan is expected to require expert_mode"

    # This item is gated on expert_mode - enable it so load_user_config doesn't just null the value out
    original_expert_mode = my_predbat.config_index["expert_mode"].get("value")
    my_predbat.expose_config("expert_mode", True, force_ha=True)

    my_predbat.expose_config("metric_min_improvement_plan", 300, force_ha=True)
    my_predbat.had_errors = False
    my_predbat.load_user_config()
    assert int_item["value"] == 250, f"Expected clamp to max 250, got {int_item['value']}"
    assert isinstance(int_item["value"], int), f"Clamped value for an integer-step item should stay an int, got {type(int_item['value'])}"
    assert my_predbat.had_errors is True, "Out-of-range config value should flag had_errors"
    print("✓ Above-max value (300) on an integer-step item clamps to an int (250), not a float")

    my_predbat.expose_config("metric_min_improvement_plan", int_item.get("default"), force_ha=True)
    my_predbat.expose_config("expert_mode", original_expert_mode, force_ha=True)

    # Restore original state
    my_predbat.args = original_args
    my_predbat.had_errors = original_had_errors
    my_predbat.expose_config("pv_metric10_weight", item.get("default"), force_ha=True)

    print("✓ Test passed: config item range clamp works correctly")
    return False


def test_config_item_step_min_max_types_consistent(my_predbat):
    """
    Schema self-check: for any input_number CONFIG_ITEM with an integer-valued step, min/max
    must also be integer-valued (e.g. 250 or 250.0 - the numeric type doesn't matter, just that
    there's no fractional part). A float step is compatible with any min/max, integer or not, so
    only the integer-step direction is checked.

    This exists because load_user_config's integer-preservation logic only makes sense for a
    schema declared this way in the first place - a mismatch here (integer step, fractional
    min/max) would mean the "preserve as int" intent and the declared range disagree with each
    other, and it's cheap to catch that at test time rather than only in generated values.
    """
    print("**** test_config_item_step_min_max_types_consistent ****")

    def is_integer_valued(value):
        return isinstance(value, int) or (isinstance(value, float) and value == int(value))

    mismatches = []
    for item in my_predbat.CONFIG_ITEMS:
        if item.get("type") != "input_number":
            continue

        step = item.get("step", 1)
        if not is_integer_valued(step):
            # A float step (e.g. 0.01, 0.25) is compatible with any min/max - nothing to check.
            continue

        for bound_name in ("min", "max"):
            bound = item.get(bound_name)
            if bound is None:
                continue
            if not is_integer_valued(bound):
                mismatches.append("{}: step={} but {}={}".format(item.get("name"), step, bound_name, bound))

    assert not mismatches, "input_number items with an integer step must have integer-valued min/max: {}".format(mismatches)

    print("✓ Test passed: all integer-step input_number items have integer-valued min/max")
    return False

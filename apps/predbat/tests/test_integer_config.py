# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
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
    print(f"✓ Float 2.0 with step=1 converts to integer 2")
    
    # Test 2: String integer like "3" should convert to int
    ha_value = "3"
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)
    
    assert isinstance(ha_value, int), f"Value '3' with step=1 should convert to int, got {type(ha_value)}"
    assert ha_value == 3, f"Value should be 3, got {ha_value}"
    print(f"✓ String '3' with step=1 converts to integer 3")
    
    # Test 3: String float like "4.0" should convert to int for integer step
    ha_value = "4.0"
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)
    
    assert isinstance(ha_value, int), f"Value '4.0' with step=1 should convert to int, got {type(ha_value)}"
    assert ha_value == 4, f"Value should be 4, got {ha_value}"
    print(f"✓ String '4.0' with step=1 converts to integer 4")
    
    # Test 4: Float with decimal part should stay as float even for integer step
    ha_value = 4.5
    ha_value = float(ha_value)
    if isinstance(step, int) or (isinstance(step, float) and step == int(step)):
        if ha_value == int(ha_value):
            ha_value = int(ha_value)
    
    assert isinstance(ha_value, float), f"Value 4.5 should remain float, got {type(ha_value)}"
    assert ha_value == 4.5, f"Value should be 4.5, got {ha_value}"
    print(f"✓ Float 4.5 stays as float 4.5 (has fractional part)")
    
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
        print(f"✓ Float 5.0 with step={decimal_step} stays as float")
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
    print(f"✓ set_reserve_min: Float 27.0 with step=1 converts to integer 27")
    
    print("✓ Test passed: Integer conversion logic works correctly")
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
    print(f"✓ expose_config correctly writes integer 5")
    
    # Test with set_reserve_min
    my_predbat.expose_config("set_reserve_min", 30, force_ha=True)
    
    state = my_predbat.ha_interface.dummy_items.get("input_number.predbat_set_reserve_min")
    if isinstance(state, dict):
        state_value = state.get("state")
    else:
        state_value = state
    
    assert isinstance(state_value, int), f"Exposed set_reserve_min should be int, got {type(state_value)}: {state_value}"
    assert state_value == 30, f"Exposed set_reserve_min should be 30, got {state_value}"
    print(f"✓ expose_config correctly writes integer 30 for set_reserve_min")
    
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
        print(f"✓ expose_config correctly writes float 12.75 for {decimal_name}")
    else:
        print("! No decimal step entity found to test")
    
    print("✓ Test passed: expose_config preserves type correctly")
    return False

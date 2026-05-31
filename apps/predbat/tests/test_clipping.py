from tests.test_infra import simple_scenario, create_mock_predbat

def test_clipping_buffer_plan_inverter_injection():
    """
    Test that the calculate_plan method correctly injects an export window
    to force the inverter to route surplus solar into the battery buffer.
    """
    my_predbat = create_mock_predbat()
    my_predbat.minutes_now = 12 * 60
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 10.0  # Battery is full!
    
    # Fake PV forecast: 7kW flat during noon, but inverter is 5kW
    my_predbat.pv_forecast_minute = {m: 0.0 for m in range(24 * 60)}
    my_predbat.pv_forecast_minuteCS = {m: 0.0 for m in range(24 * 60)}
    for m in range(11 * 60, 15 * 60):
        my_predbat.pv_forecast_minute[m] = 7.0 / 60.0
        my_predbat.pv_forecast_minuteCS[m] = 7.0 / 60.0

    my_predbat.inverters = []
    my_predbat.inverter_limit = 5.0
    my_predbat.pv_ac_limit = 0.0
    my_predbat.inverter_hybrid = True
    my_predbat.export_limits = []
    
    # Enable clipping buffer (auto-calculated)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast = "pv_estimate"
    my_predbat.clipping_buffer_can_discharge = "Cost Optimal"
    
    # Calculate clipping limits
    rem, c_start, c_end = my_predbat.calculate_clipping_buffer()
    
    # Verify that the calculation correctly identified a need for a buffer
    assert rem > 0, "Clipping buffer should be > 0"
    assert c_start is not None and c_end is not None, "Clipping window should be identified"
    
    # Now run calculate_plan (skip recompute for speed)
    # Actually, we need to populate rate data for the plan to run without crashing
    for m in range(24 * 60):
        my_predbat.rate_import[m] = 10.0
        my_predbat.rate_export[m] = 5.0
        my_predbat.load_minutes[m] = 0.5 / 60.0
        
    my_predbat.calculate_plan(recompute=False, publish=False)
    
    # Check that an export window was injected to force the battery to target SOC
    injected = False
    for i, e_win in enumerate(my_predbat.export_window_best):
        if e_win["start"] <= c_start and e_win["end"] >= c_end:
            # Check that the target is set to the floor of the buffer
            target_kw = max(0, my_predbat.soc_max - rem)
            target_percent = (target_kw / my_predbat.soc_max) * 100.0
            
            # Allow some floating point variance
            assert abs(e_win["target"] - target_percent) < 1.0, f"Export target {e_win['target']} should match buffer floor {target_percent}"
            injected = True
            
    assert injected, "An export window should have been injected for the clipping buffer"

def test_clipping_buffer_plan_manual_injection():
    """
    Test that the calculate_plan method correctly injects an export window
    when the user specifies a manual fixed buffer size.
    """
    my_predbat = create_mock_predbat()
    my_predbat.minutes_now = 12 * 60
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 10.0  # Battery is full!
    
    my_predbat.pv_forecast_minute = {m: 0.0 for m in range(24 * 60)}
    my_predbat.pv_forecast_minuteCS = {m: 0.0 for m in range(24 * 60)}

    my_predbat.inverters = []
    my_predbat.inverter_limit = 5.0
    my_predbat.pv_ac_limit = 0.0
    my_predbat.inverter_hybrid = True
    my_predbat.export_limits = []
    
    # Manual clipping buffer overrides
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_can_discharge = "Always"
    my_predbat.clipping_buffer_min_kwh = 2.5
    my_predbat.clipping_buffer_max_kwh = 2.5
    my_predbat.clipping_buffer_start_time = "12:00:00"
    my_predbat.clipping_buffer_end_time = "14:00:00"
    
    # Calculate clipping limits
    rem, c_start, c_end = my_predbat.calculate_clipping_buffer()
    
    assert rem == 2.5, "Clipping buffer should be exactly 2.5kWh based on manual overrides"
    assert c_start == 12 * 60, "Clipping start should be 12:00"
    assert c_end == 14 * 60, "Clipping end should be 14:00"
    
    for m in range(24 * 60):
        my_predbat.rate_import[m] = 10.0
        my_predbat.rate_export[m] = 5.0
        my_predbat.load_minutes[m] = 0.5 / 60.0
        
    my_predbat.calculate_plan(recompute=False, publish=False)
    
    injected = False
    for i, e_win in enumerate(my_predbat.export_window_best):
        if e_win["start"] <= c_start and e_win["end"] >= c_end:
            target_kw = max(0, my_predbat.soc_max - rem)
            target_percent = (target_kw / my_predbat.soc_max) * 100.0
            
            assert abs(e_win["target"] - target_percent) < 1.0, f"Export target {e_win['target']} should match buffer floor {target_percent}"
            injected = True
            
    assert injected, "An export window should have been injected for the manual clipping buffer"

if __name__ == "__main__":
    test_clipping_buffer_plan_inverter_injection()
    test_clipping_buffer_plan_manual_injection()
    print("Clipping plan tests passed!")
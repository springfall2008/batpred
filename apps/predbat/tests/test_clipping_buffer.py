import pytest
from unittest.mock import MagicMock
import sys
import os

# Add apps/predbat to sys.path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from predbat import PredBat
from plan import Plan

def test_clipping_buffer_automated_spike_protection():
    # Setup mock PredBat
    pb = MagicMock(spec=PredBat)
    
    # Enable buffer and use a dummy forecast
    pb.clipping_buffer_enable = True
    pb.clipping_buffer_forecast = "pv_estimate90"
    
    # 5kW hardware limit
    pb.clipping_buffer_limit_override = 5000  
    pb.pv_ac_limit = 5.0
    pb.inverter_limit = 5.0
    pb.inverters = []
    
    # No user minimum - testing the automated spike protection
    pb.clipping_buffer_min_kwh = 0.0
    pb.clipping_buffer_max_kwh = 10.0
    
    # Timing
    pb.clipping_buffer_start_time = "10:00:00"
    pb.clipping_buffer_end_time = "14:00:00"
    pb.midnight_utc = None # Not needed for basic math test if we mock time correctly
    pb.forecast_minutes = 24 * 60
    pb.minutes_now = 0
    
    # Set up normal forecast (pv_estimate90): never hits the 5kW limit
    # e.g., maxes out at 4.0kW
    pb.pv_forecast_minute90 = {m: 4.0 / 60.0 for m in range(24 * 60)} 
    
    # Set up Clear Sky forecast (pv_estimateCS): spikes above 5kW limit
    # e.g., hits 6.0kW between minute 660 (11:00) and 780 (13:00)
    cs_forecast = {m: 4.0 / 60.0 for m in range(24 * 60)}
    for m in range(660, 780):
        cs_forecast[m] = 6.0 / 60.0 # 1kW of clipping potential for 120 minutes = 120 kW-minutes = 2 kWh
    pb.pv_forecast_minuteCS = cs_forecast
    
    pb.inverter_hybrid = True
    pb.export_limits = []
    
    # Required for calculate_clipping_buffer logging and math
    pb.log = MagicMock()
    pb.time_abs_str = MagicMock(return_value="12:00:00")
    
    # Call the method
    remaining, start, end, windows = Plan.calculate_clipping_buffer(pb)
    
    # The normal forecast predicted 0 clipping.
    # However, the clear sky forecast predicted 120 mins * 1kW = 120 kW-minutes = 2.0 kWh of clipping.
    # Because of automated spike protection, the buffer should reserve exactly 2.0 kWh.
    assert pb.clipping_buffer_forecast_kwh is not None
    assert round(remaining, 2) == 2.0
    
    # Ensure it's correctly populated backwards in time
    assert round(pb.clipping_buffer_forecast_kwh[600], 2) == 2.0 # Still 2.0 before the spike starts

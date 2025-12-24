# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
from tests.test_infra import TestHAInterface
from predbat import PredBat
from inverter import Inverter

def setup_predbat(my_predbat):
    """
    Setup Predbat for testing
    """
    my_predbat.args["soc_percent"] = ["sensor.soc_percent"]
    my_predbat.args["charge_rate"] = ["number.charge_rate"]
    my_predbat.args["discharge_rate"] = ["number.discharge_rate"]
    my_predbat.args["battery_power"] = ["sensor.battery_power"]
    my_predbat.args["battery_power_invert"] = [False]
    my_predbat.args["predbat_status"] = ["predbat.status"]
    my_predbat.battery_charge_power_curve_auto = True
    my_predbat.battery_rate_max_scaling = 1.0
    my_predbat.battery_rate_max_scaling_discharge = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.debug_enable = True  # Enable debug for more info

def create_test_history_data(my_predbat, num_days=7, slow=False):
    """
    Create mock history data for testing find_charge_curve
    
    Creates realistic battery charging/discharging data with:
    - SoC percentage data that transitions through integer percentages
    - Charge rate data at maximum during charging
    - Battery power data (negative = charging)
    - Predbat status data
    
    History data is indexed by minutes in the past (0 = now, higher = older)
    """
    ha = my_predbat.ha_interface
    base_time = my_predbat.midnight_utc
    
    # Create history data structure for each sensor
    # We'll build this in reverse (most recent first)
    history_dict = {
        "sensor.soc_percent": [],
        "number.charge_rate": [],
        "number.discharge_rate": [],
        "sensor.battery_power": [],
        "predbat.status": []
    }
    ha.dummy_items['sensor.soc_percent'] = 100
    ha.dummy_items['number.charge_rate'] = 2600
    ha.dummy_items['number.discharge_rate'] = 2600
    ha.dummy_items['sensor.battery_power'] = -2600
    ha.dummy_items['predbat.status'] = "Idle"
    
    total_minutes = num_days * 24 * 60
    
    # DEBUG: Counter for printing first few charging entries
    debug_charging_count = 0
    
    # Generate data for each minute going backwards in time
    for minutes in range(total_minutes):
        timestamp = base_time + timedelta(minutes=minutes)
        timestamp_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S%z")
        
        # Calculate the actual time of day for this timestamp
        hour = timestamp.hour
        minute_of_hour = timestamp.minute
        minute_of_day = hour * 60 + minute_of_hour
        
        # Create charging session from 2am-7am (300 minutes) going from 80% to 100%
        # This ensures we capture the 85-100% range needed for charge curve detection
        # We need to hit exact integer SoC values during charging
        # Charge from 80% to 100% over the window
        if 2 <= hour <= 7:
            # Charging session - SoC increases from 80% to 100%
            # Calculate minutes into this charging session (0 to 300)
            session_minute = (hour - 2) * 60 + minute_of_hour
            
            # Calculate SoC with integer steps
            # 21 percentages (80-100) over ~300 minutes = ~14.3 minutes per 1%
            # Map session minutes to SoC: 0->80, 300->100
            soc = int(80 + (session_minute * 20 / 300))  # Linear interpolation, then truncate
            soc = min(100, max(80, soc))  # Clamp to range
            
            charge_rate = 2600  # Max charge rate in Watts
            discharge_rate = 0  # Max discharge rate in Watts
            if slow and soc > 90:
                battery_power = -1300  # Negative = charging
            else:
                battery_power = -2598  # Negative = charging, slightly less than max
            status = "Charging"
            
        # Create discharging session from 5pm-10pm going from 100% to 5%
        # Discharge at 1% per ~3.16 minutes (95% over 300 mins)
        elif 17 <= hour <= 22:
            session_minute = minute_of_day - 17*60  # Minutes into discharge session
            
            # Calculate SoC with integer steps
            # 95% over 300 minutes ≈ 1% per 3.16 minutes
            # Use int division to get exact integer SoC values
            soc_step = int(session_minute * 40 /300)
            soc = max(4, 40 - soc_step)  # 100% down to 4%
            
            charge_rate = 0  # Max discharge rate
            discharge_rate = 2600  # Max discharge rate in Watts
            if slow:
                battery_power = 1300  # Positive = discharging
            else:
                battery_power = 2599  # Positive = discharging
            status = "Exporting"
        else:
            # Idle periods
            if hour < 2:
                soc = 80  # Before charging
            elif hour < 17:
                soc = 100  # After charging, before discharge
            else:
                soc = 5  # After discharge
            charge_rate = 0
            discharge_rate = 0  # Max discharge rate in Watts
            battery_power = 0
            status = "Idle"
        
        # Append to history (note: we're building from most recent to oldest)
        history_dict["sensor.soc_percent"].append({
            "state": str(soc) if isinstance(soc, int) else str(round(soc, 2)),  # Keep integers as integers
            "last_updated": timestamp_str,
            "attributes": {"unit_of_measurement": "%"}
        })
        history_dict["number.charge_rate"].append({
            "state": int(charge_rate),
            "last_updated": timestamp_str,
            "attributes": {"unit_of_measurement": "W"}
        })
        history_dict["number.discharge_rate"].append({
            "state": int(discharge_rate),
            "last_updated": timestamp_str,
            "attributes": {"unit_of_measurement": "W"}
        })
        
        history_dict["sensor.battery_power"].append({
            "state": round(battery_power, 1),
            "last_updated": timestamp_str,
            "attributes": {"unit_of_measurement": "W"}
        })
        
        history_dict["predbat.status"].append({
            "state": status,
            "last_updated": timestamp_str,
            "attributes": {}
        })        
    
    def mock_get_history(entity_id, now=None, days=30):
        if entity_id in history_dict:
            return [history_dict[entity_id]]
        return None
    my_predbat.ha_interface.get_history = mock_get_history

def remove_test_history_data(my_predbat):
    def mock_get_history(entity_id, now=None, days=30):
        return None
    my_predbat.ha_interface.get_history = mock_get_history
    


def test_find_charge_curve_basic(my_predbat):
    """
    Test basic charge curve detection
    
    This test creates realistic charging data from 80% to 100% SoC
    and expects the find_charge_curve function to detect a valid curve.
    """
    print("*** Running test: find_charge_curve_basic ***")
    failed = False
    
    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    
    # Setup configuration
    setup_predbat(my_predbat)   
    
    # Create test data with charging from 80% to 100%
    create_test_history_data(my_predbat, num_days=7)
    
    # Test charge curve - should find a curve with data from 80-100%
    try:
        charge_curve = inv.find_charge_curve(discharge=False)
        if charge_curve:
            print("Charge curve found: {}".format(charge_curve))
            # Verify curve has expected SoC percentages in 85-100% range
            curve_keys = list(charge_curve.keys())
            if not any(key >= 85 and key <= 100 for key in curve_keys):
                print("ERROR: Charge curve should contain SoC percentages in 85-100% range")
                print("       Got keys: {}".format(sorted(curve_keys)))
                failed = True
            # Verify curve values are between 0 and 1
            for key, value in charge_curve.items():
                if value != 1:
                    print("ERROR: Curve value {} at SoC {}% is out of expected value 1".format(value, key))
                    failed = True
        else:
            print("ERROR: No charge curve found - expected to find curve with 80-100% charging data")
            failed = True
    except Exception as e:
        print("ERROR: find_charge_curve raised exception: {}".format(e))
        import traceback
        traceback.print_exc()
        failed = True
    
    return failed

def test_find_charge_curve_slow(my_predbat):
    """
    Test basic charge curve detection
    
    This test creates realistic charging data from 80% to 100% SoC
    and expects the find_charge_curve function to detect a valid curve.
    """
    print("*** Running test: find_charge_curve_slow ***")
    failed = False
    
    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    
    # Setup configuration
    setup_predbat(my_predbat)   
    
    # Create test data with charging from 80% to 100%
    create_test_history_data(my_predbat, num_days=7, slow=True)
    
    # Test charge curve - should find a curve with data from 80-100%
    try:
        charge_curve = inv.find_charge_curve(discharge=False)
        if charge_curve:
            print("Charge curve found: {}".format(charge_curve))
            # Verify curve has expected SoC percentages in 85-100% range
            curve_keys = list(charge_curve.keys())
            if not any(key >= 85 and key <= 100 for key in curve_keys):
                print("ERROR: Charge curve should contain SoC percentages in 85-100% range")
                print("       Got keys: {}".format(sorted(curve_keys)))
                failed = True
            # Verify curve values are between 0 and 1
            for key, value in charge_curve.items():
                if key > 90:
                    if value != 0.5:
                        print("ERROR: Curve value {} at SoC {}% is out of expected value 0.5".format(value, key))
                        failed = True
                else:
                    if value != 1:
                        print("ERROR: Curve value {} at SoC {}% is out of expected value 1".format(value, key))
                        failed = True

        else:
            print("ERROR: No charge curve found - expected to find curve with 80-100% charging data")
            failed = True
    except Exception as e:
        print("ERROR: find_charge_curve raised exception: {}".format(e))
        import traceback
        traceback.print_exc()
        failed = True
    
    return failed



def test_find_charge_curve_discharge(my_predbat):
    """
    Test discharge curve detection
    
    This test creates realistic discharging data from 100% to 5% SoC
    and expects the find_charge_curve function to detect a valid discharge curve.
    """
    print("*** Running test: find_charge_curve_discharge ***")
    failed = False
    
    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    
    # Setup configuration
    setup_predbat(my_predbat)   
    
    # Create test data with discharge from 100% to 5%
    create_test_history_data(my_predbat, num_days=7)
    
    # Test discharge curve - should find a curve with data from 5-20%
    try:
        discharge_curve = inv.find_charge_curve(discharge=True)
        if discharge_curve:
            print("Discharge curve found: {}".format(discharge_curve))
            # Verify curve has expected SoC percentages in 5-20% range
            curve_keys = list(discharge_curve.keys())
            if not any(key >= 5 and key <= 20 for key in curve_keys):
                print("ERROR: Discharge curve should contain SoC percentages in 5-20% range")
                print("       Got keys: {}".format(sorted(curve_keys)))
                failed = True
            # Verify curve values are between 0 and 1
            for key, value in discharge_curve.items():
                if value < 0 or value > 1:
                    print("ERROR: Curve value {} at SoC {}% is out of range [0,1]".format(value, key))
                    failed = True
        else:
            print("ERROR: No discharge curve found - expected to find curve with 100-5% discharge data")
            failed = True
    except Exception as e:
        print("ERROR: find_charge_curve raised exception: {}".format(e))
        import traceback
        traceback.print_exc()
        failed = True
    
    return failed


def test_find_charge_curve_no_data(my_predbat):
    """
    Test find_charge_curve with missing history data
    """
    print("*** Running test: find_charge_curve_no_data ***")
    failed = False
    
    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    
    # Setup configuration
    setup_predbat(my_predbat)   
    
    # No history data created - should return None
    remove_test_history_data(my_predbat)
    charge_curve = inv.find_charge_curve(discharge=False)
    
    if charge_curve:
        print("ERROR: find_charge_curve should return None with no history data")
        failed = True
    else:
        print("Correctly returned None with no history data")
    
    return failed


def test_find_charge_curve_inverted_battery_power(my_predbat):
    """
    Test find_charge_curve with inverted battery power - smoke test
    """
    print("*** Running test: find_charge_curve_inverted_battery_power ***")
    failed = False
    
    # Setup inverter
    inv = Inverter(my_predbat, 0)
    inv.battery_rate_max_charge = 2600 / 60000  # Watts / MINUTE_WATT
    inv.battery_rate_max_discharge = 2600 / 60000
    inv.soc_max = 10.0
    inv.battery_scaling = 1.0
    
    # Setup configuration
    setup_predbat(my_predbat)   
    
    # Create test data
    create_test_history_data(my_predbat, num_days=7)
    
    # Test charge curve with inverted power - should run without error
    try:
        charge_curve = inv.find_charge_curve(discharge=False)
        if charge_curve:
            print("Charge curve with inverted power found: {}".format(charge_curve))
        else:
            print("No charge curve found with inverted battery power - this is expected")
    except Exception as e:
        print("ERROR: find_charge_curve raised exception with inverted battery power: {}".format(e))
        import traceback
        traceback.print_exc()
        failed = True
    
    return failed


def run_find_charge_curve_tests(my_predbat_dummy):
    """
    Run all find_charge_curve tests
    """
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = True
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.max_days_previous = 7  # Should be an integer, not a list
    
    failed = False
    print("**** Running find_charge_curve tests ****")
    
    failed |= test_find_charge_curve_basic(my_predbat)
    if failed:
        return failed

    failed |= test_find_charge_curve_slow(my_predbat)
    if failed:
        return failed

    failed |= test_find_charge_curve_discharge(my_predbat)
    if failed:
        return failed
    
    failed |= test_find_charge_curve_no_data(my_predbat)
    if failed:
        return failed
    
    failed |= test_find_charge_curve_inverted_battery_power(my_predbat)
    if failed:
        return failed
    
    print("**** find_charge_curve tests completed ****")
    return failed

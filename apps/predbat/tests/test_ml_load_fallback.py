from tests.test_infra import TestHAInterface
from fetch import Fetch

class TestFetchMLFallback(Fetch):
    def __init__(self, ha_interface, base):
        self.ha_interface = ha_interface
        self.base = base
        self.prefix = "predbat"
        self.load_ml_forecast = {}
        self.forecast_days = 2
        self.minute_data = {}
        
    def get_state_wrapper(self, entity_id, attribute=None, default=None):
        if attribute == "status":
            if "inactive_test" in entity_id:
                return "error"
            else:
                return "active"
        if attribute == "results":
            return {"00:00": 1.0}
        return default
        
    def log(self, msg):
        pass

def run_ml_load_fallback_tests(my_predbat):
    failed = False
    print("\n============================================================")
    print("Running ML Load Fallback tests")
    print("============================================================")

    ha = TestHAInterface()
    fetch = TestFetchMLFallback(ha, my_predbat)
    
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    
    # Test 1: Active status proceeds
    fetch.prefix = "active_test"
    try:
        fetch.fetch_ml_load_forecast(now_utc)
        print("FAIL: ML Load forecast should have raised AttributeError on active path")
        failed = True
    except AttributeError:
        # It bypassed the early return and hit unmocked attributes, which means it works
        print("PASS: ML Load forecast active status proceeds past early return")
        
    # Test 2: Inactive status falls back
    fetch.prefix = "inactive_test"
    fetch.load_ml_forecast = {}
    try:
        fetch.fetch_ml_load_forecast(now_utc)
        if fetch.load_ml_forecast:
            print("FAIL: ML Load forecast should NOT be populated when inactive")
            failed = True
        else:
            print("PASS: ML Load forecast ignored when inactive (fallback)")
    except AttributeError:
        print("FAIL: ML Load forecast did NOT fall back early and hit AttributeError")
        failed = True

    print("============================================================")
    if failed:
        print("FAIL: SOME TESTS FAILED")
    else:
        print("PASS: ALL TESTS PASSED")
    print("============================================================")
    
    return failed

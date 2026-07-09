from tests.test_infra import TestHAInterface
from fetch import Fetch
from datetime import datetime, timezone

class TestFetchMLFallback(Fetch):
    def __init__(self, ha_interface, base):
        self.ha_interface = ha_interface
        self.base = base
        self.prefix = "predbat"
        self.forecast_days = 2
        self.midnight_utc = datetime.now(timezone.utc)
        self.minutes_now = 0
        self.queried_results = False
        
    def get_state_wrapper(self, entity_id, attribute=None, default=None):
        if attribute is None:
            if "inactive_test" in entity_id:
                return "error"
            else:
                return "active"
        if attribute == "results":
            self.queried_results = True
            # Return an empty dict so minute_data doesn't crash on mocked data
            return {}
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
    now_utc = datetime.now(timezone.utc)
    
    # Test 1: Active status proceeds
    fetch.prefix = "active_test"
    fetch.queried_results = False
    fetch.fetch_ml_load_forecast(now_utc)
    if not fetch.queried_results:
        print("FAIL: ML Load forecast did NOT query 'results' on active path")
        failed = True
    else:
        print("PASS: ML Load forecast active status proceeds past early return")
        
    # Test 2: Inactive status falls back
    fetch.prefix = "inactive_test"
    fetch.queried_results = False
    fetch.fetch_ml_load_forecast(now_utc)
    if fetch.queried_results:
        print("FAIL: ML Load forecast queried 'results' when inactive")
        failed = True
    else:
        print("PASS: ML Load forecast ignored when inactive (fallback)")

    print("============================================================")
    if failed:
        print("FAIL: SOME TESTS FAILED")
    else:
        print("PASS: ALL TESTS PASSED")
    print("============================================================")
    
    return failed

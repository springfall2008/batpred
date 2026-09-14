from tests.test_infra import TestHAInterface
from fetch import Fetch
from datetime import datetime, timezone


class TestFetchMLFallback(Fetch):
    """
    Test harness for ML load forecast fallback logic.
    """

    def __init__(self, ha_interface, base):
        """Initialise the TestFetchMLFallback mock object."""
        self.ha_interface = ha_interface
        self.base = base
        self.prefix = "predbat"
        self.forecast_days = 2
        self.midnight_utc = datetime.now(timezone.utc)
        self.minutes_now = 0
        self.queried_results = False

    def get_state_wrapper(self, entity_id, attribute=None, default=None):
        """Mock get_state_wrapper returning controlled states."""
        if attribute is None:
            if "inactive_test" in entity_id:
                return "error"
            elif "missing_test" in entity_id:
                return default
            else:
                return "active"
        if attribute == "results":
            self.queried_results = True
            # Return an empty dict so minute_data doesn't crash on mocked data
            return {"fake_data": True}
        return default

    def log(self, msg):
        """Mock log handler."""
        pass


def run_ml_load_fallback_tests(my_predbat):
    """Run the ML load forecast fallback test suite."""
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
    res = fetch.fetch_ml_load_forecast(now_utc)
    if res == {}:
        print("FAIL: ML Load forecast returned {} on active path")
        failed = True
    elif not fetch.queried_results:
        print("FAIL: ML Load forecast did NOT query 'results' on active path")
        failed = True
    else:
        print("PASS: ML Load forecast active status proceeds past early return")

    # Test 2: Inactive status falls back
    fetch.prefix = "inactive_test"
    fetch.queried_results = False
    res = fetch.fetch_ml_load_forecast(now_utc)
    if res != {}:
        print(f"FAIL: ML Load forecast returned dict of length {len(res)} when inactive (expected {{}})")
        failed = True
    elif fetch.queried_results:
        print("FAIL: ML Load forecast queried 'results' when inactive")
        failed = True
    else:
        print("PASS: ML Load forecast ignored when inactive (fallback)")

    # Test 3: Missing state falls back
    fetch.prefix = "missing_test"
    fetch.queried_results = False
    res = fetch.fetch_ml_load_forecast(now_utc)
    if res != {}:
        print(f"FAIL: ML Load forecast returned dict of length {len(res)} when state missing (expected {{}})")
        failed = True
    elif fetch.queried_results:
        print("FAIL: ML Load forecast queried 'results' when state missing")
        failed = True
    else:
        print("PASS: ML Load forecast ignored when state missing (fallback)")

    print("============================================================")
    if failed:
        print("FAIL: SOME TESTS FAILED")
    else:
        print("PASS: ALL TESTS PASSED")
    print("============================================================")

    return failed

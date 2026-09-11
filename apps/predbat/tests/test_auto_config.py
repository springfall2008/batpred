# -----------------------------------------------------------------------------
# Predbat Home Battery System
# -----------------------------------------------------------------------------


def run_auto_config_tests(my_predbat):
    """
    Test auto_config retry logic and list/dict matching
    """
    failed = False
    print("\n============================================================")
    print("Running auto_config tests")
    print("============================================================")

    # Backup original
    original_args = my_predbat.args.copy()
    original_unmatched = getattr(my_predbat, "unmatched_args", {}).copy()

    try:
        # Test 1: List matching retains 're:' elements if they fail
        print("\n=== Test 1: List regex matching ===")
        my_predbat.args = {"pv_forecast_raw": ["sensor.inv1", "re:.*inv2"]}
        my_predbat.unmatched_args = {}

        # This simulates < 10 mins, so final=False
        my_predbat.auto_config(final=False)

        # It should still be in args exactly as it was, because matched=True for lists
        val = my_predbat.args.get("pv_forecast_raw")
        if val != ["sensor.inv1", "re:.*inv2"]:
            print(f"FAIL: List regex matching failed to retain string. Got {val}")
            failed = True
        else:
            print("PASS: List regex retained.")

        # Test 2: Dict matching retains 're:' elements if they fail
        print("\n=== Test 2: Dict regex matching ===")
        my_predbat.args = {"my_dict": {"foo": "sensor.foo", "bar": "re:.*bar"}}
        my_predbat.unmatched_args = {}

        my_predbat.auto_config(final=False)

        val = my_predbat.args.get("my_dict")
        if val != {"foo": "sensor.foo", "bar": "re:.*bar"}:
            print(f"FAIL: Dict regex matching failed to retain string. Got {val}")
            failed = True
        else:
            print("PASS: Dict regex retained.")

        # Test 3: Standard unmatched arg is moved to unmatched_args after 10 mins (final=True)
        print("\n=== Test 3: Final=True moves unmatched args ===")
        my_predbat.args = {"inverter_limit": "re:.*missing_limit.*"}
        my_predbat.unmatched_args = {}

        my_predbat.auto_config(final=True)

        if "inverter_limit" in my_predbat.args:
            print(f"FAIL: inverter_limit still in args: {my_predbat.args['inverter_limit']}")
            failed = True
        elif "inverter_limit" not in my_predbat.unmatched_args:
            print("FAIL: inverter_limit not in unmatched_args")
            failed = True
        else:
            print("PASS: Final=True moved unmatched arg correctly.")

        # Test 4: Final=True disables unmatched list items
        print("\n=== Test 4: Final=True disables unmatched list items ===")
        my_predbat.args = {"pv_forecast_raw": ["sensor.inv1", "re:.*inv2_missing"]}
        my_predbat.unmatched_args = {}

        my_predbat.auto_config(final=True)

        val = my_predbat.args.get("pv_forecast_raw")
        if val != ["sensor.inv1", None]:
            print(f"FAIL: List regex matching failed to disable item on final=True. Got {val}")
            failed = True
        else:
            print("PASS: List regex item disabled on final=True.")

        # Test 5: Final=True disables unmatched dict items
        print("\n=== Test 5: Final=True disables unmatched dict items ===")
        my_predbat.args = {"my_dict": {"foo": "sensor.foo", "bar": "re:.*bar_missing"}}
        my_predbat.unmatched_args = {}

        my_predbat.auto_config(final=True)

        val = my_predbat.args.get("my_dict")
        if val != {"foo": "sensor.foo", "bar": None}:
            print(f"FAIL: Dict regex matching failed to disable item on final=True. Got {val}")
            failed = True
        else:
            print("PASS: Dict regex item disabled on final=True.")

        # Test 6: Retry matching resolves previously unmatched scalar arg from unmatched_args
        print("\n=== Test 6: Scalar retry resolution from unmatched_args ===")
        my_predbat.args = {}
        my_predbat.unmatched_args = {"inverter_limit": "re:sensor\\.delayed_limit_.*"}
        my_predbat.auto_config(final=True)
        if "inverter_limit" in my_predbat.args:
            print(f"FAIL: inverter_limit prematurely resolved before entity appeared")
            failed = True

        my_predbat.set_state_wrapper("sensor.delayed_limit_99", "3600")
        my_predbat.auto_config(final=True)
        if my_predbat.args.get("inverter_limit") != "sensor.delayed_limit_99":
            print(f"FAIL: inverter_limit not restored from unmatched_args. Got {my_predbat.args.get('inverter_limit')}")
            failed = True
        elif "inverter_limit" in my_predbat.unmatched_args:
            print("FAIL: inverter_limit still in unmatched_args after retry resolution")
            failed = True
        else:
            print("PASS: Scalar regex resolved and restored from unmatched_args.")

        # Test 7: List retry resolution during startup window (< 10 mins)
        print("\n=== Test 7: List regex retry resolution ===")
        my_predbat.args = {"pv_forecast_raw": ["sensor.inv1", "re:sensor\\.delayed_inv_.*"]}
        my_predbat.unmatched_args = {}
        my_predbat.auto_config(final=False)
        val = my_predbat.args.get("pv_forecast_raw")
        if val != ["sensor.inv1", "re:sensor\\.delayed_inv_.*"]:
            print(f"FAIL: List regex matching failed to retain string before entity appeared. Got {val}")
            failed = True

        my_predbat.set_state_wrapper("sensor.delayed_inv_2", "1200")
        my_predbat.auto_config(final=False)
        val = my_predbat.args.get("pv_forecast_raw")
        if val != ["sensor.inv1", "sensor.delayed_inv_2"]:
            print(f"FAIL: List regex failed to resolve when entity appeared. Got {val}")
            failed = True
        else:
            print("PASS: List regex resolved on retry.")

        # Test 8: Dict retry resolution during startup window (< 10 mins)
        print("\n=== Test 8: Dict regex retry resolution ===")
        my_predbat.args = {"my_dict": {"primary": "sensor.foo", "secondary": "re:sensor\\.delayed_dict_.*"}}
        my_predbat.unmatched_args = {}
        my_predbat.auto_config(final=False)
        val = my_predbat.args.get("my_dict")
        if val != {"primary": "sensor.foo", "secondary": "re:sensor\\.delayed_dict_.*"}:
            print(f"FAIL: Dict regex failed to retain string before entity appeared. Got {val}")
            failed = True

        my_predbat.set_state_wrapper("sensor.delayed_dict_sub", "10")
        my_predbat.auto_config(final=False)
        val = my_predbat.args.get("my_dict")
        if val != {"primary": "sensor.foo", "secondary": "sensor.delayed_dict_sub"}:
            print(f"FAIL: Dict regex failed to resolve when entity appeared. Got {val}")
            failed = True
        else:
            print("PASS: Dict regex resolved on retry.")

        # Test 9: validate_config_check_retry auto_config integration
        print("\n=== Test 9: validate_config_check_retry resolves regex and clears error ===")
        from datetime import timedelta

        my_predbat.args = {"inverter_limit": "re:sensor\\.delayed_valid_.*"}
        my_predbat.unmatched_args = {}
        my_predbat.validate_config_retries_remaining = 2
        my_predbat.validate_config_next_retry_time = my_predbat.now_utc - timedelta(seconds=1)

        my_predbat.set_state_wrapper("sensor.delayed_valid_1", 3600)
        my_predbat.validate_config_check_retry()
        if my_predbat.args.get("inverter_limit") != "sensor.delayed_valid_1":
            print(f"FAIL: validate_config_check_retry did not resolve regex. Got {my_predbat.args.get('inverter_limit')}")
            failed = True
        elif my_predbat.validate_config_retries_remaining != 0 or my_predbat.validate_config_next_retry_time is not None:
            print(f"FAIL: validate_config retry state not cleared: remaining={my_predbat.validate_config_retries_remaining}, next_time={my_predbat.validate_config_next_retry_time}")
            failed = True
        else:
            print("PASS: validate_config_check_retry successfully resolved regex and cleared retry sequence.")

    finally:
        # Cleanup dummy items added during tests
        for dummy_key in ("sensor.delayed_limit_99", "sensor.delayed_inv_2", "sensor.delayed_dict_sub", "sensor.delayed_valid_1"):
            if hasattr(my_predbat, "ha_interface") and hasattr(my_predbat.ha_interface, "dummy_items"):
                my_predbat.ha_interface.dummy_items.pop(dummy_key, None)
        my_predbat.args = original_args
        my_predbat.unmatched_args = original_unmatched

    print("============================================================")
    if failed:
        print("FAIL: SOME TESTS FAILED")
    else:
        print("PASS: ALL TESTS PASSED")
    print("============================================================")

    return failed

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
            print(f"FAIL: inverter_limit not in unmatched_args")
            failed = True
        else:
            print("PASS: Final=True moved unmatched arg correctly.")

    finally:
        my_predbat.args = original_args
        my_predbat.unmatched_args = original_unmatched

    print("============================================================")
    if failed:
        print("FAIL: SOME TESTS FAILED")
    else:
        print("PASS: ALL TESTS PASSED")
    print("============================================================")

    return failed

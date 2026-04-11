# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


from tests.test_infra import reset_rates, reset_inverter
from prediction import Prediction
from marginal import MARGINAL_EXTRA_KWH_LEVELS, MARGINAL_EXTRA_KWH_LEVEL_NAMES


def test_marginal_costs(my_predbat):
    """
    Test the marginal energy cost matrix calculation.

    Verifies that calculate_marginal_costs() produces a correctly structured 4×7 matrix
    (4 load levels: low/med/high/ev × 7 time windows: now through +12 h in 2-hour steps),
    all values are numeric, and min/max bounds are sane.
    """
    failed = False

    print("*** Running test: marginal_costs basic structure")

    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60

    # Build flat pv and load step dicts (relative-minute keys, 5-min steps)
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 0.2  # 0.2 kWh per 5-min step ~ 2.4 kW

    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    # Simple flat import rate, no export
    reset_rates(my_predbat, 20.0, 5.0)

    # Empty charge/export plan (no windows)
    my_predbat.charge_limit_best = []
    my_predbat.charge_window_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.end_record = 24 * 60

    # Run the marginal cost calculation (internal baseline run, no external metric needed)
    my_predbat.calculate_marginal_costs()

    # --- Validate marginal_costs_matrix structure ---
    if not hasattr(my_predbat, "marginal_costs_matrix") or not my_predbat.marginal_costs_matrix:
        print("ERROR: marginal_costs_matrix not set after calculate_marginal_costs()")
        return True

    matrix = my_predbat.marginal_costs_matrix
    expected_kwh_keys = list(MARGINAL_EXTRA_KWH_LEVELS)
    expected_time_keys = getattr(my_predbat, "marginal_time_labels", [])

    for kwh_key in expected_kwh_keys:
        if kwh_key not in matrix:
            print("ERROR: missing row key '{}' in matrix".format(kwh_key))
            failed = True
            continue
        for time_key in expected_time_keys:
            if time_key not in matrix[kwh_key]:
                print("ERROR: missing time key '{}' in matrix['{}']".format(time_key, kwh_key))
                failed = True
                continue
            val = matrix[kwh_key][time_key]
            if not isinstance(val, (int, float)):
                print("ERROR: matrix['{}']['{}'] = {} is not numeric".format(kwh_key, time_key, val))
                failed = True

    # --- Validate min/max bounds ---
    if not hasattr(my_predbat, "marginal_costs_min") or not hasattr(my_predbat, "marginal_costs_max"):
        print("ERROR: marginal_costs_min / marginal_costs_max not set")
        return True

    if my_predbat.marginal_costs_min > my_predbat.marginal_costs_max:
        print("ERROR: marginal_costs_min {} > marginal_costs_max {}".format(my_predbat.marginal_costs_min, my_predbat.marginal_costs_max))
        failed = True

    print("matrix = {}".format(matrix))
    print("min={} max={}".format(my_predbat.marginal_costs_min, my_predbat.marginal_costs_max))

    # --- Validate sensor was published ---
    sensor_key = "sensor." + my_predbat.prefix + "_marginal_energy_costs"
    if sensor_key not in my_predbat.dashboard_values:
        print("ERROR: sensor '{}' not found in dashboard_values".format(sensor_key))
        failed = True
    else:
        state = my_predbat.dashboard_values[sensor_key].get("state")
        try:
            float(state)
        except (TypeError, ValueError):
            print("ERROR: sensor state '{}' is not a valid number".format(state))
            failed = True
        attrs = my_predbat.dashboard_values[sensor_key].get("attributes", {})
        if "baseline_metric" not in attrs:
            print("ERROR: sensor missing 'baseline_metric' attribute")
            failed = True
        else:
            try:
                float(attrs["baseline_metric"])
            except (TypeError, ValueError):
                print("ERROR: baseline_metric '{}' is not numeric".format(attrs["baseline_metric"]))
                failed = True

        # --- Validate new attributes ---
        for attr in ("grid_import", "grid_export"):
            if attr not in attrs:
                print("ERROR: sensor missing '{}' attribute".format(attr))
                failed = True
            elif not isinstance(attrs[attr], dict):
                print("ERROR: sensor '{}' is not a dict".format(attr))
                failed = True
        for attr in ("grid_import_now", "grid_export_now"):
            if attr not in attrs:
                print("ERROR: sensor missing '{}' attribute".format(attr))
                failed = True
            else:
                try:
                    float(attrs[attr])
                except (TypeError, ValueError):
                    print("ERROR: sensor '{}' = '{}' is not numeric".format(attr, attrs[attr]))
                    failed = True
        for state_name in MARGINAL_EXTRA_KWH_LEVEL_NAMES:
            attr = "rate_now_{}_consumption".format(state_name)
            if attr not in attrs:
                print("ERROR: sensor missing '{}' attribute".format(attr))
                failed = True
            else:
                try:
                    float(attrs[attr])
                except (TypeError, ValueError):
                    print("ERROR: '{}' = '{}' is not numeric".format(attr, attrs[attr]))
                    failed = True

    # --- Validate binary sensors ---
    for state_name in MARGINAL_EXTRA_KWH_LEVEL_NAMES:
        for kind in ("cheap", "moderate"):
            bs_key = "binary_sensor.{}_marginal_rate_now_{}_is_{}".format(my_predbat.prefix, state_name, kind)
            if bs_key not in my_predbat.dashboard_values:
                print("ERROR: binary sensor '{}' not found".format(bs_key))
                failed = True
            else:
                bs_state = my_predbat.dashboard_values[bs_key].get("state")
                if bs_state not in ("on", "off"):
                    print("ERROR: binary sensor '{}' state '{}' is not on/off".format(bs_key, bs_state))
                    failed = True
                bs_attrs = my_predbat.dashboard_values[bs_key].get("attributes", {})
                if "cost" not in bs_attrs:
                    print("ERROR: binary sensor '{}' missing 'cost' attribute".format(bs_key))
                    failed = True

    if not failed:
        print("*** test_marginal_costs PASSED ***")
    else:
        print("*** test_marginal_costs FAILED ***")
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the Open-Meteo solar forecast provider in SolarAPI."""

from datetime import datetime
from unittest.mock import patch

from solcast import pvwatts_cell_temperature
from tests.test_solcast import create_test_solar_api
from tests.test_infra import run_async


# ============================================================================
# Helper: build a minimal Open-Meteo hourly forecast response
# ============================================================================


def _make_forecast_response(times=None, gti=None, temp=None, wind=None):
    """Return a dict shaped like an Open-Meteo /v1/forecast hourly response."""
    if times is None:
        times = ["2025-06-15T12:00", "2025-06-15T13:00"]
    if gti is None:
        gti = [500.0, 600.0]
    if temp is None:
        temp = [25.0, 26.0]
    if wind is None:
        wind = [1.0] * len(times)
    return {"hourly": {"time": times, "global_tilted_irradiance": gti, "temperature_2m": temp, "wind_speed_10m": wind}}


def _make_ensemble_response(times=None, members=None):
    """Return a dict shaped like an Open-Meteo ensemble endpoint response."""
    if times is None:
        times = ["2025-06-15T12:00", "2025-06-15T13:00"]
    if members is None:
        members = {
            "global_tilted_irradiance_member01": [400.0, 480.0],
            "global_tilted_irradiance_member02": [450.0, 540.0],
            "global_tilted_irradiance_member03": [480.0, 570.0],
        }
    data = {"hourly": {"time": times}}
    data["hourly"].update(members)
    return data


# ============================================================================
# download_open_meteo_ensemble_data tests
# ============================================================================


def test_ensemble_returns_p10_values(my_predbat):
    """
    download_open_meteo_ensemble_data should return a dict of ts→kW10
    where each value is the 10th-percentile GTI across members, converted to kW.
    """
    print("  - test_ensemble_returns_p10_values")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast_max_age = 1.0
        ensemble_data = _make_ensemble_response()
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_data)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        kwp = 3.0
        system_loss = 0.0  # simplify: 0% loss so kW = GTI_kWm2 * kwp
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.download_open_meteo_ensemble_data(51.5, -0.1, 35, 0, kwp, system_loss))

        # For 3 members at 2025-06-15T12:00: [400, 450, 480] sorted
        # p10_idx = max(0, int(3 * 0.1) - 1) = 0  -> gti_p10 = 400
        # kW = (400 / 1000) * 3.0 * (1 - 0.0) = 1.2
        expected_12 = round((400.0 / 1000.0) * kwp * (1.0 - system_loss), 4)
        if "2025-06-15T12:00" not in result:
            print("ERROR: Expected key '2025-06-15T12:00' in ensemble result")
            failed = True
        elif abs(result["2025-06-15T12:00"] - expected_12) > 0.001:
            print(f"ERROR: ensemble p10 at 12:00: expected {expected_12}, got {result['2025-06-15T12:00']}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_ensemble_empty_on_no_members(my_predbat):
    """
    download_open_meteo_ensemble_data returns empty dict when there are no member keys.
    """
    print("  - test_ensemble_empty_on_no_members")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast_max_age = 1.0
        # Response with no member keys at all
        no_members = {"hourly": {"time": ["2025-06-15T12:00"]}}
        test_api.set_mock_response("ensemble-api.open-meteo.com", no_members)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.download_open_meteo_ensemble_data(51.5, -0.1, 35, 0, 3.0, 0.14))

        if result != {}:
            print(f"ERROR: Expected empty dict, got {result}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_ensemble_empty_on_http_failure(my_predbat):
    """
    download_open_meteo_ensemble_data returns empty dict when HTTP fails.
    """
    print("  - test_ensemble_empty_on_http_failure")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast_max_age = 1.0
        # No mock response registered → cache_get_url will return None

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            result = run_async(test_api.solar.download_open_meteo_ensemble_data(51.5, -0.1, 35, 0, 3.0, 0.14))

        if result != {}:
            print(f"ERROR: Expected empty dict on HTTP failure, got {result}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# download_open_meteo_data tests
# ============================================================================


def test_download_open_meteo_data_basic(my_predbat):
    """
    download_open_meteo_data returns sorted data items with pv_estimate and pv_estimate10.
    """
    print("  - test_download_open_meteo_data_basic")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        forecast_response = _make_forecast_response(
            times=["2025-06-15T12:00", "2025-06-15T13:00", "2025-06-15T14:00"],
            gti=[500.0, 600.0, 550.0],
            temp=[25.0, 25.0, 25.0],
        )
        ensemble_response = _make_ensemble_response(
            times=["2025-06-15T12:00", "2025-06-15T13:00", "2025-06-15T14:00"],
            members={
                "global_tilted_irradiance_member01": [300.0, 360.0, 330.0],
                "global_tilted_irradiance_member02": [350.0, 420.0, 385.0],
                "global_tilted_irradiance_member03": [380.0, 450.0, 415.0],
            },
        )
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, max_kwh = run_async(test_api.solar.download_open_meteo_data())

        if len(sorted_data) != 2:
            print(f"ERROR: Expected 2 data points (12:00 and 13:00), got {len(sorted_data)}")
            failed = True

        if abs(max_kwh - 3.0) > 0.001:
            print(f"ERROR: max_kwh expected 3.0, got {max_kwh}")
            failed = True

        # With trapezoidal integration, the 12:00 slot = 0.5*(pv50_at_12 + pv50_at_13).
        # pv50 = (GTI/1000) * kwp * eta_temp where eta_temp uses cell temperature from SAPM model
        if sorted_data:
            pv50 = sorted_data[0].get("pv_estimate", 0)
            t_cell_12 = pvwatts_cell_temperature(500.0, 25.0, 1.0)
            eta_temp_12 = max(0.5, min(1.1, 1.0 - 0.004 * (t_cell_12 - 25.0)))
            t_cell_13 = pvwatts_cell_temperature(600.0, 25.0, 1.0)
            eta_temp_13 = max(0.5, min(1.1, 1.0 - 0.004 * (t_cell_13 - 25.0)))
            expected_pv50 = round(0.5 * ((500.0 / 1000.0) * 3.0 * eta_temp_12 + (600.0 / 1000.0) * 3.0 * eta_temp_13), 4)
            if abs(pv50 - expected_pv50) > 0.001:
                print(f"ERROR: pv_estimate at 12:00 expected {expected_pv50}, got {pv50}")
                failed = True

            # pv_estimate10 should be from ensemble P10
            pv10 = sorted_data[0].get("pv_estimate10", None)
            if pv10 is None:
                print("ERROR: pv_estimate10 missing from data item")
                failed = True

    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_temperature_derating(my_predbat):
    """
    download_open_meteo_data applies temperature derating above 25°C.
    """
    print("  - test_download_open_meteo_data_temperature_derating")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 4.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        # GTI=1000, T=45°C, wind=1.0 m/s: T_cell via SAPM model, eta = 1 - 0.004*(T_cell-25)
        # pv50 = (1000/1000) * 4.0 * eta_temp  (same at both ends → trapz average equals point value)
        forecast_response = _make_forecast_response(times=["2025-06-15T12:00", "2025-06-15T13:00"], gti=[1000.0, 1000.0], temp=[45.0, 45.0], wind=[1.0, 1.0])
        ensemble_response = _make_ensemble_response(times=["2025-06-15T12:00", "2025-06-15T13:00"], members={"global_tilted_irradiance_member01": [900.0, 900.0]})
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, _ = run_async(test_api.solar.download_open_meteo_data())

        if not sorted_data:
            print("ERROR: No data returned")
            failed = True
        else:
            t_cell = pvwatts_cell_temperature(1000.0, 45.0, 1.0)
            eta_temp = max(0.5, min(1.1, 1.0 - 0.004 * (t_cell - 25.0)))
            expected_pv50 = round(1.0 * 4.0 * eta_temp, 4)
            pv50 = sorted_data[0].get("pv_estimate", 0)
            if abs(pv50 - expected_pv50) > 0.001:
                print(f"ERROR: pv_estimate with temp derating: expected {expected_pv50}, got {pv50}")
                failed = True
    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_multi_config(my_predbat):
    """
    download_open_meteo_data sums kW across multiple panel configurations.
    """
    print("  - test_download_open_meteo_data_multi_config")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Two identical 2 kWp arrays → combined should be 4 kWp
        test_api.solar.open_meteo_forecast = [
            {"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 2.0, "efficiency": 1.0},
            {"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 2.0, "efficiency": 1.0},
        ]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        forecast_response = _make_forecast_response(times=["2025-06-15T12:00", "2025-06-15T13:00"], gti=[1000.0, 1000.0], temp=[25.0, 25.0])
        ensemble_response = _make_ensemble_response(times=["2025-06-15T12:00", "2025-06-15T13:00"], members={"global_tilted_irradiance_member01": [800.0, 800.0]})
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, max_kwh = run_async(test_api.solar.download_open_meteo_data())

        if abs(max_kwh - 4.0) > 0.001:
            print(f"ERROR: max_kwh expected 4.0, got {max_kwh}")
            failed = True

        # Each array: GTI=1000, T=25°C, wind=1.0 m/s → cell temp via SAPM, pv50 = (1000/1000)*2.0*eta
        # Same at both ends → trapz average equals point value
        if sorted_data:
            pv50 = sorted_data[0].get("pv_estimate", 0)
            t_cell = pvwatts_cell_temperature(1000.0, 25.0, 1.0)
            eta_temp = max(0.5, min(1.1, 1.0 - 0.004 * (t_cell - 25.0)))
            expected_pv50 = round(2 * (1000.0 / 1000.0) * 2.0 * eta_temp, 4)
            if abs(pv50 - expected_pv50) > 0.001:
                print(f"ERROR: pv_estimate multi-config: expected {expected_pv50}, got {pv50}")
                failed = True
    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_postcode_lookup(my_predbat):
    """
    download_open_meteo_data resolves postcode to lat/lon and sends correct coordinates.
    """
    print("  - test_download_open_meteo_data_postcode_lookup")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"postcode": "SW1A1AA", "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        postcode_response = {"result": {"latitude": 51.5014, "longitude": -0.1419}}
        forecast_response = _make_forecast_response(times=["2025-06-15T12:00", "2025-06-15T13:00"], gti=[400.0, 400.0], temp=[20.0, 20.0], wind=[1.0, 1.0])
        ensemble_response = _make_ensemble_response(times=["2025-06-15T12:00", "2025-06-15T13:00"], members={"global_tilted_irradiance_member01": [300.0, 300.0]})

        test_api.set_mock_response("postcodes.io", postcode_response)
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, _ = run_async(test_api.solar.download_open_meteo_data())

        # Check postcode lookup was made
        postcode_calls = [r for r in test_api.request_log if "postcodes.io" in r["url"]]
        if len(postcode_calls) == 0:
            print("ERROR: Expected postcode lookup API call")
            failed = True

        if not sorted_data:
            print("ERROR: No data returned after postcode lookup")
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_cool_temp_efficiency(my_predbat):
    """
    At cool ambient temperatures with moderate irradiance, the SAPM cell temperature
    stays below 25 degC so the panel runs more efficiently than at STC.
    The output should be > (GTI/1000 * kwp) because eta_temp > 1.0.
    This proves pvlib produces different results from the old ambient-only formula
    (which clamped to eta=1.0 when ambient < 25 degC but ignored irradiance heating).
    """
    print("  - test_download_open_meteo_data_cool_temp_efficiency")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        # 10 degC ambient, 200 W/m2, 1 m/s wind: SAPM T_cell < 25 degC -> eta > 1.0
        # Same at both ends → trapz average equals point value
        forecast_response = _make_forecast_response(times=["2025-04-15T12:00", "2025-04-15T13:00"], gti=[200.0, 200.0], temp=[10.0, 10.0], wind=[1.0, 1.0])
        ensemble_response = _make_ensemble_response(times=["2025-04-15T12:00", "2025-04-15T13:00"], members={"global_tilted_irradiance_member01": [150.0, 150.0]})
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, _ = run_async(test_api.solar.download_open_meteo_data())

        if not sorted_data:
            print("ERROR: No data returned")
            failed = True
        else:
            pv50 = sorted_data[0].get("pv_estimate", 0)
            t_cell = pvwatts_cell_temperature(200.0, 10.0, 1.0)
            eta_temp = max(0.5, min(1.1, 1.0 - 0.004 * (t_cell - 25.0)))
            unmodified_pv = round((200.0 / 1000.0) * 3.0, 4)
            expected_pv50 = round(unmodified_pv * eta_temp, 4)

            # Verify T_cell is below 25 degC so eta > 1.0 (cool efficiency gain)
            if t_cell >= 25.0:
                print(f"ERROR: Expected T_cell < 25 degC for cool-temp test, got {t_cell:.2f} degC")
                failed = True
            if eta_temp <= 1.0:
                print(f"ERROR: Expected eta_temp > 1.0 at cool cell temp, got {eta_temp:.4f}")
                failed = True
            if abs(pv50 - expected_pv50) > 0.001:
                print(f"ERROR: pv_estimate cool-temp: expected {expected_pv50} (eta={eta_temp:.4f}, T_cell={t_cell:.2f}C), got {pv50}")
                failed = True
    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_no_gti_returns_zero(my_predbat):
    """
    download_open_meteo_data treats None GTI values as zero power output.
    """
    print("  - test_download_open_meteo_data_no_gti_returns_zero")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        # GTI=None at night — same at both ends so trapz average is also zero
        forecast_response = _make_forecast_response(times=["2025-06-15T02:00", "2025-06-15T03:00"], gti=[None, None], temp=[15.0, 15.0])
        ensemble_response = _make_ensemble_response(times=["2025-06-15T02:00", "2025-06-15T03:00"], members={"global_tilted_irradiance_member01": [None, None]})
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, _ = run_async(test_api.solar.download_open_meteo_data())

        if not sorted_data:
            print("ERROR: Expected one data point, got none")
            failed = True
        else:
            pv50 = sorted_data[0].get("pv_estimate", -1)
            if pv50 != 0.0:
                print(f"ERROR: Expected pv_estimate == 0.0 for None GTI, got {pv50}")
                failed = True
    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_two_aspect_configs(my_predbat):
    """
    download_open_meteo_data with two arrays on different aspects makes separate API
    requests (different azimuth params) and sums their outputs correctly.
    Mirrors the real two-roof-aspect setup: tilt 23 az -133 and tilt 45 az +45.
    """
    print("  - test_download_open_meteo_data_two_aspect_configs")
    failed = False

    test_api = create_test_solar_api()
    try:
        # Array 1: WSW-facing, shallow tilt  (az -133 Solcast → convert_azimuth → -47 OM)
        # Array 2: NW-facing, steep tilt     (az +45 Solcast  → convert_azimuth → 135 OM)
        test_api.solar.open_meteo_forecast = [
            {"latitude": 51.49, "longitude": -2.49, "declination": 23.0, "azimuth": -133.0, "kwp": 1.56, "efficiency": 1.0},
            {"latitude": 51.49, "longitude": -2.49, "declination": 45.0, "azimuth": 45.0, "kwp": 2.73, "efficiency": 1.0},
        ]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        times = ["2025-06-15T12:00", "2025-06-15T13:00"]
        # Array 1 (WSW): 400 W/m²   Array 2 (NW): 200 W/m²  (same at both ends → trapz average = point value)
        forecast_wsw = {"hourly": {"time": times, "global_tilted_irradiance": [400.0, 400.0], "temperature_2m": [25.0, 25.0], "wind_speed_10m": [1.0, 1.0]}}
        forecast_nw = {"hourly": {"time": times, "global_tilted_irradiance": [200.0, 200.0], "temperature_2m": [25.0, 25.0], "wind_speed_10m": [1.0, 1.0]}}
        ensemble_wsw = {"hourly": {"time": times, "global_tilted_irradiance_member01": [320.0, 320.0]}}
        ensemble_nw = {"hourly": {"time": times, "global_tilted_irradiance_member01": [160.0, 160.0]}}

        # Use URL-specific mocks keyed on the OM azimuth value in the query string
        test_api.set_mock_response("azimuth=-47.0", forecast_wsw)
        test_api.set_mock_response("azimuth=135.0", forecast_nw)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_wsw)  # same fallback for both

        # Patch mock_aiohttp_session so each array's ensemble call returns consistent data.
        # We override ensemble response per azimuth too.

        # Replace the generic ensemble mock with per-azimuth keys
        del test_api.mock_responses["ensemble-api.open-meteo.com"]
        # Re-register: let both ensemble calls return their respective mocks via URL matching
        test_api.mock_responses["ensemble-api.open-meteo.com/v1/ensemble?models=icon_seamless&latitude=51.49&longitude=-2.49&hourly=global_tilted_irradiance&tilt=23.0&azimuth=-47.0"] = ensemble_wsw
        test_api.mock_responses["ensemble-api.open-meteo.com/v1/ensemble?models=icon_seamless&latitude=51.49&longitude=-2.49&hourly=global_tilted_irradiance&tilt=45.0&azimuth=135.0"] = ensemble_nw

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, max_kwh = run_async(test_api.solar.download_open_meteo_data())

        # max_kwh should be sum of both kwp (no system_loss)
        expected_max_kwh = 1.56 + 2.73
        if abs(max_kwh - expected_max_kwh) > 0.001:
            print(f"ERROR: max_kwh expected {expected_max_kwh}, got {max_kwh}")
            failed = True

        # Both arrays contribute at 2025-06-15T12:00; values should be summed
        # Array 1: (400/1000)*1.56*eta1   Array 2: (200/1000)*2.73*eta2   sum via SAPM cell temp
        if not sorted_data:
            print("ERROR: No data returned for two-aspect config")
            failed = True
        else:
            pv50 = sorted_data[0].get("pv_estimate", 0)
            t1 = pvwatts_cell_temperature(400.0, 25.0, 1.0)
            eta1 = max(0.5, min(1.1, 1.0 - 0.004 * (t1 - 25.0)))
            t2 = pvwatts_cell_temperature(200.0, 25.0, 1.0)
            eta2 = max(0.5, min(1.1, 1.0 - 0.004 * (t2 - 25.0)))
            expected_pv50 = round((400.0 / 1000.0) * 1.56 * eta1 + (200.0 / 1000.0) * 2.73 * eta2, 4)
            if abs(pv50 - expected_pv50) > 0.001:
                print(f"ERROR: combined pv_estimate expected {expected_pv50}, got {pv50}")
                failed = True

        # Verify two distinct forecast API URLs were requested (different azimuth params)
        forecast_calls = [r for r in test_api.request_log if "api.open-meteo.com/v1/forecast" in r["url"]]
        azimuths_called = set()
        for call in forecast_calls:
            url = call["url"]
            if "azimuth=-47.0" in url:
                azimuths_called.add("wsw")
            elif "azimuth=135.0" in url:
                azimuths_called.add("nw")
        if azimuths_called != {"wsw", "nw"}:
            print(f"ERROR: Expected API calls for both azimuth=-47.0 and azimuth=135.0, got calls for: {azimuths_called}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_download_open_meteo_data_http_failure(my_predbat):
    """
    download_open_meteo_data returns empty list and zero max_kwh when HTTP fails.
    """
    print("  - test_download_open_meteo_data_http_failure")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 0.86}]
        test_api.solar.open_meteo_forecast_max_age = 1.0
        # No mocks set → cache_get_url returns None

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            sorted_data, max_kwh = run_async(test_api.solar.download_open_meteo_data())

        if sorted_data:
            print(f"ERROR: Expected empty data on HTTP failure, got {sorted_data}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# fetch_pv_forecast integration tests
# ============================================================================


def test_fetch_pv_forecast_open_meteo(my_predbat):
    """
    Integration: fetch_pv_forecast selects Open-Meteo when configured and publishes dashboard items.
    """
    print("  - test_fetch_pv_forecast_open_meteo")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.solcast_host = None
        test_api.solar.solcast_api_key = None
        test_api.solar.forecast_solar = None
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0

        # Produce 96 hourly slots (4 days) so the forecast covers the full horizon
        base = datetime(2025, 6, 15, 0, 0, 0)
        times = [(base.replace(hour=h % 24) if h < 24 else base.replace(hour=h % 24)).strftime("%Y-%m-%dT%H:%M") for h in range(96)]
        # Build proper 4-day hourly list
        from datetime import timedelta

        times = [(base + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(96)]
        gti = [max(0.0, 600.0 * (1 - abs(h % 24 - 12) / 12.0)) for h in range(96)]
        temp = [20.0] * 96

        wind = [1.0] * 96
        forecast_response = {"hourly": {"time": times, "global_tilted_irradiance": gti, "temperature_2m": temp, "wind_speed_10m": wind}}
        ensemble_response = {
            "hourly": {
                "time": times,
                "global_tilted_irradiance_member01": [v * 0.8 for v in gti],
                "global_tilted_irradiance_member02": [v * 0.9 for v in gti],
            }
        }

        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Verify Open-Meteo API was called
        om_calls = [r for r in test_api.request_log if "open-meteo.com" in r["url"]]
        if len(om_calls) == 0:
            print("ERROR: Expected Open-Meteo API call")
            failed = True

        # Verify dashboard items published
        pv_today_key = f"sensor.{test_api.mock_base.prefix}_pv_today"
        if pv_today_key not in test_api.dashboard_items:
            print(f"ERROR: Expected '{pv_today_key}' to be published, got keys: {list(test_api.dashboard_items.keys())[:5]}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


def test_fetch_pv_forecast_open_meteo_not_selected_when_forecast_solar_configured(my_predbat):
    """
    fetch_pv_forecast should use forecast.solar (not Open-Meteo) when both are configured.
    forecast.solar takes priority since it appears first in the if/elif chain.
    """
    print("  - test_fetch_pv_forecast_open_meteo_not_selected_when_forecast_solar_configured")
    failed = False

    test_api = create_test_solar_api()
    try:
        test_api.solar.solcast_host = None
        test_api.solar.solcast_api_key = None
        test_api.solar.forecast_solar = [{"latitude": 51.5, "longitude": -0.1, "declination": 30, "azimuth": 0, "kwp": 3.0}]
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 180, "kwp": 3.0, "efficiency": 1.0}]

        forecast_solar_response = {
            "result": {"watts": {"2025-06-15T12:00:00+0000": 500}},
            "message": {"info": {"time": "2025-06-15T11:30:00+0000"}},
        }
        test_api.set_mock_response("forecast.solar", forecast_solar_response)

        def create_mock_session(*args, **kwargs):
            return test_api.mock_aiohttp_session()

        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.fetch_pv_forecast())

        # Only forecast.solar should have been called
        forecast_calls = [r for r in test_api.request_log if "forecast.solar" in r["url"]]
        om_calls = [r for r in test_api.request_log if "open-meteo.com" in r["url"]]
        if len(forecast_calls) == 0:
            print("ERROR: Expected forecast.solar API call when both configured")
            failed = True
        if len(om_calls) > 0:
            print(f"ERROR: Expected NO Open-Meteo calls when forecast.solar is configured, got {len(om_calls)}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# azimuth_zero_south tests
# ============================================================================


def test_download_open_meteo_data_azimuth_zero_south(my_predbat):
    """
    When azimuth_zero_south is True the azimuth is passed to the Open-Meteo API
    as-is (0=South convention); when False (default) convert_azimuth is applied first.
    """
    print("  - test_download_open_meteo_data_azimuth_zero_south")
    failed = False

    forecast_response = _make_forecast_response(
        times=["2025-06-15T12:00", "2025-06-15T13:00"],
        gti=[500.0, 500.0],
        temp=[25.0, 25.0],
    )
    ensemble_response = _make_ensemble_response(
        times=["2025-06-15T12:00", "2025-06-15T13:00"],
        members={"global_tilted_irradiance_member01": [400.0, 400.0]},
    )

    def create_mock_session(*args, **kwargs):
        return test_api.mock_aiohttp_session()

    # --- Case 1: azimuth_zero_south=True, azimuth=0 (South in Open-Meteo convention) ---
    # URL should contain azimuth=0
    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 0, "kwp": 3.0, "efficiency": 1.0, "azimuth_zero_south": True}]
        test_api.solar.open_meteo_forecast_max_age = 1.0
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.download_open_meteo_data())
        om_calls = [r for r in test_api.request_log if "api.open-meteo.com" in r["url"]]
        if not om_calls:
            print("ERROR: No Open-Meteo API call made (azimuth_zero_south=True)")
            failed = True
        elif "azimuth=0" not in om_calls[0]["url"]:
            print(f"ERROR: Expected azimuth=0 in URL with azimuth_zero_south=True, got: {om_calls[0]['url']}")
            failed = True
    finally:
        test_api.cleanup()

    # --- Case 2: azimuth_zero_south=False (default), azimuth=0 (North in Predbat convention) ---
    # convert_azimuth(0) → 180; URL should contain azimuth=180
    test_api = create_test_solar_api()
    try:
        test_api.solar.open_meteo_forecast = [{"latitude": 51.5, "longitude": -0.1, "declination": 35, "azimuth": 0, "kwp": 3.0, "efficiency": 1.0}]
        test_api.solar.open_meteo_forecast_max_age = 1.0
        test_api.set_mock_response("api.open-meteo.com", forecast_response)
        test_api.set_mock_response("ensemble-api.open-meteo.com", ensemble_response)
        with patch("solcast.aiohttp.ClientSession", side_effect=create_mock_session):
            run_async(test_api.solar.download_open_meteo_data())
        om_calls = [r for r in test_api.request_log if "api.open-meteo.com" in r["url"]]
        if not om_calls:
            print("ERROR: No Open-Meteo API call made (azimuth_zero_south=False)")
            failed = True
        elif "azimuth=180" not in om_calls[0]["url"]:
            print(f"ERROR: Expected azimuth=180 in URL without azimuth_zero_south, got: {om_calls[0]['url']}")
            failed = True
    finally:
        test_api.cleanup()

    return failed


# ============================================================================
# Test runner
# ============================================================================


def run_open_meteo_tests(my_predbat):
    """Run all Open-Meteo solar forecast provider tests."""
    print("Running Open-Meteo solar forecast tests...")
    failed = False

    failed |= test_ensemble_returns_p10_values(my_predbat)
    failed |= test_ensemble_empty_on_no_members(my_predbat)
    failed |= test_ensemble_empty_on_http_failure(my_predbat)
    failed |= test_download_open_meteo_data_basic(my_predbat)
    failed |= test_download_open_meteo_data_temperature_derating(my_predbat)
    failed |= test_download_open_meteo_data_multi_config(my_predbat)
    failed |= test_download_open_meteo_data_two_aspect_configs(my_predbat)
    failed |= test_download_open_meteo_data_postcode_lookup(my_predbat)
    failed |= test_download_open_meteo_data_cool_temp_efficiency(my_predbat)
    failed |= test_download_open_meteo_data_no_gti_returns_zero(my_predbat)
    failed |= test_download_open_meteo_data_http_failure(my_predbat)
    failed |= test_fetch_pv_forecast_open_meteo(my_predbat)
    failed |= test_fetch_pv_forecast_open_meteo_not_selected_when_forecast_solar_configured(my_predbat)
    failed |= test_download_open_meteo_data_azimuth_zero_south(my_predbat)

    return failed

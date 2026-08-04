# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the shared solar GTI to kW conversion model."""

from datetime import datetime

import pytz

from solar_model import convert_azimuth, gti_hourly_to_period_kwh, pvwatts_cell_temperature

FLAT_TIMES = ["2025-06-01T{:02d}:00".format(hour) for hour in range(4)]


def stamp_for(text):
    """Return the tz-aware UTC datetime for an Open-Meteo timestamp string."""
    return pytz.utc.localize(datetime.strptime(text, "%Y-%m-%dT%H:%M"))


def test_solar_model(my_predbat):
    """Verify the shared solar model against hand-derived values."""
    failed = False
    print("**** Testing solar_model ****")

    print("Test: convert_azimuth maps the Predbat convention onto the Open-Meteo one")
    for predbat_az, expected in [(180, 0), (90, 90), (270, -90), (0, 180)]:
        result = convert_azimuth(predbat_az)
        if result != expected:
            print("  ERROR: convert_azimuth({}) expected {}, got {}".format(predbat_az, expected, result))
            failed = True

    print("Test: pvwatts_cell_temperature matches the SAPM formula")
    # T_cell = 25 + 1000*exp(-3.47 + -0.0594*0) + (1000/1000)*3.0
    #        = 25 + 1000*0.031117 + 3 = 59.117
    hot = pvwatts_cell_temperature(1000.0, 25.0, 0.0)
    if abs(hot - 59.117) > 0.001:
        print("  ERROR: cell temperature expected 59.117, got {}".format(hot))
        failed = True
    if pvwatts_cell_temperature(0.0, 20.0, 1.5) != 20.0:
        print("  ERROR: zero irradiance should give ambient temperature")
        failed = True

    print("Test: a constant-irradiance hour converts to the hand-derived energy")
    # eta = 1 - 0.004*(59.117 - 25) = 0.863532; pv = (1000/1000) * 1 kWp * eta * 1.0
    # Both endpoints are equal so the trapezoid returns the same value.
    flat_gti = [1000.0] * 4
    flat_temp = [25.0] * 4
    flat_wind = [0.0] * 4
    result = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0)
    if len(result) != 3:
        print("  ERROR: 4 samples should yield 3 integrated periods, got {}".format(len(result)))
        failed = True
    first = result.get(stamp_for(FLAT_TIMES[0]))
    if first is None:
        print("  ERROR: missing the first period")
        failed = True
    elif abs(first["pv_estimate"] - 0.8635) > 0.0001:
        print("  ERROR: expected 0.8635 kWh, got {}".format(first["pv_estimate"]))
        failed = True

    print("Test: cold panels are allowed to exceed their STC rating")
    # T_cell = 0 + 200*exp(-3.47 - 0.0594) + 0.6 = 6.4645; eta = 1.074142 (above 1.0)
    cold = gti_hourly_to_period_kwh(FLAT_TIMES, [200.0] * 4, [0.0] * 4, [1.0] * 4, kwp=1.0, system_loss=0.0)
    cold_first = cold[stamp_for(FLAT_TIMES[0])]
    if abs(cold_first["pv_estimate"] - 0.2148) > 0.0001:
        print("  ERROR: expected 0.2148 kWh for cold panels, got {}".format(cold_first["pv_estimate"]))
        failed = True

    print("Test: the trapezoid integrates a rising ramp to the mean of its endpoints")
    ramp = gti_hourly_to_period_kwh(FLAT_TIMES, [0.0, 1000.0, 1000.0, 0.0], [25.0] * 4, [0.0] * 4, kwp=1.0, system_loss=0.0)
    # Endpoints 0.0 and 0.8635 average to 0.43175, rounded to 4 places
    if abs(ramp[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - 0.4318) > 0.0001:
        print("  ERROR: expected 0.4318 kWh across the sunrise hour, got {}".format(ramp[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    print("Test: zero irradiance produces zero energy")
    dark = gti_hourly_to_period_kwh(FLAT_TIMES, [0.0] * 4, [15.0] * 4, [1.0] * 4, kwp=5.0, system_loss=0.05)
    if any(entry["pv_estimate"] != 0.0 for entry in dark.values()):
        print("  ERROR: zero irradiance should give zero energy, got {}".format(dark))
        failed = True

    print("Test: system_loss and kwp scale the output linearly")
    scaled = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=2.0, system_loss=0.5)
    if abs(scaled[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - first["pv_estimate"]) > 0.0001:
        print("  ERROR: doubling kwp and halving efficiency should cancel out, got {}".format(scaled[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    print("Test: p10_fallback scales the P10 series and defaults to 0.7")
    if abs(first["pv_estimate10"] - round(first["pv_estimate"] * 0.7, 4)) > 0.0001:
        print("  ERROR: the default P10 fallback should be 0.7, got {}".format(first["pv_estimate10"]))
        failed = True
    half = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_fallback=0.5)
    half_first = half[stamp_for(FLAT_TIMES[0])]
    if abs(half_first["pv_estimate10"] - round(half_first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: p10_fallback 0.5 not applied, got {}".format(half_first["pv_estimate10"]))
        failed = True

    print("Test: p10_instant overrides the fallback and is capped at P50")
    ensemble = {FLAT_TIMES[index]: 0.1 for index in range(4)}
    with_ensemble = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_instant=ensemble)
    ensemble_first = with_ensemble[stamp_for(FLAT_TIMES[0])]
    if ensemble_first["pv_estimate10"] >= ensemble_first["pv_estimate"]:
        print("  ERROR: an ensemble P10 below P50 should stay below it, got {}".format(ensemble_first["pv_estimate10"]))
        failed = True
    huge = {FLAT_TIMES[index]: 99.0 for index in range(4)}
    capped = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_instant=huge)
    capped_first = capped[stamp_for(FLAT_TIMES[0])]
    if abs(capped_first["pv_estimate10"] - capped_first["pv_estimate"]) > 0.0001:
        print("  ERROR: an ensemble P10 above P50 should be capped at P50, got {}".format(capped_first["pv_estimate10"]))
        failed = True

    print("Test: shading_factors apply the correct month")
    shaded = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, shading_factors=[0.5] * 12)
    if abs(shaded[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - round(first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: a 0.5 shading factor was not applied, got {}".format(shaded[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    print("Test: a gap in the timestamps is not integrated across")
    gapped_times = ["2025-06-01T00:00", "2025-06-01T01:00", "2025-06-01T05:00"]
    gapped = gti_hourly_to_period_kwh(gapped_times, [1000.0] * 3, [25.0] * 3, [0.0] * 3, kwp=1.0, system_loss=0.0)
    if len(gapped) != 1:
        print("  ERROR: only the contiguous hour pair should integrate, got {} periods".format(len(gapped)))
        failed = True

    print("Test: a None irradiance sample is treated as zero rather than raising")
    with_none = gti_hourly_to_period_kwh(FLAT_TIMES, [None, 1000.0, 1000.0, None], [25.0] * 4, [0.0] * 4, kwp=1.0, system_loss=0.0)
    if abs(with_none[stamp_for(FLAT_TIMES[0])]["pv_estimate"] - 0.4318) > 0.0001:
        print("  ERROR: a None sample should behave as zero, got {}".format(with_none[stamp_for(FLAT_TIMES[0])]["pv_estimate"]))
        failed = True

    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the shared solar GTI to kW conversion model."""

from datetime import datetime, timedelta

import pytz

from solar_model import convert_azimuth, gti_hourly_to_period_kwh, pvwatts_cell_temperature

FLAT_TIMES = ["2025-06-01T{:02d}:00".format(hour) for hour in range(4)]


def stamp_for(text):
    """Return the tz-aware UTC datetime for an Open-Meteo timestamp string."""
    return pytz.utc.localize(datetime.strptime(text, "%Y-%m-%dT%H:%M"))


def hour_ending(text):
    """Return the start of the hour a sample stamped text covers (Open-Meteo radiation is a preceding-hour mean)."""
    return stamp_for(text) - timedelta(hours=1)


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
    # Each sample is the mean power of the hour ending at its stamp, so a flat series gives this every hour.
    flat_gti = [1000.0] * 4
    flat_temp = [25.0] * 4
    flat_wind = [0.0] * 4
    result = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0)
    if len(result) != 4:
        print("  ERROR: 4 samples should yield 4 hourly periods, got {}".format(len(result)))
        failed = True
    # The sample at 00:00 covers 23:00-00:00 on the day before
    if pytz.utc.localize(datetime(2025, 5, 31, 23, 0)) not in result:
        print("  ERROR: the 00:00 sample should give the period starting 23:00 the day before, got {}".format(sorted(result)))
        failed = True
    first = result.get(hour_ending(FLAT_TIMES[1]))
    if first is None:
        print("  ERROR: missing the first period")
        failed = True
    elif abs(first["pv_estimate"] - 0.8635) > 0.0001:
        print("  ERROR: expected 0.8635 kWh, got {}".format(first["pv_estimate"]))
        failed = True

    print("Test: cold panels are allowed to exceed their STC rating")
    # T_cell = 0 + 200*exp(-3.47 - 0.0594) + 0.6 = 6.4645; eta = 1.074142 (above 1.0)
    cold = gti_hourly_to_period_kwh(FLAT_TIMES, [200.0] * 4, [0.0] * 4, [1.0] * 4, kwp=1.0, system_loss=0.0)
    cold_first = cold[hour_ending(FLAT_TIMES[1])]
    if abs(cold_first["pv_estimate"] - 0.2148) > 0.0001:
        print("  ERROR: expected 0.2148 kWh for cold panels, got {}".format(cold_first["pv_estimate"]))
        failed = True

    print("Test: a sample is the energy of the hour ENDING at its stamp, not spread over its neighbours")
    # Open-Meteo: "Solar radiation is averaged over the past hour". One sunny sample at 01:00 means
    # 00:00-01:00 was sunny and every other hour dark; reading it as an instant and averaging
    # neighbours would put half of it in 01:00-02:00, half an hour late.
    impulse = gti_hourly_to_period_kwh(FLAT_TIMES, [0.0, 1000.0, 0.0, 0.0], [25.0] * 4, [0.0] * 4, kwp=1.0, system_loss=0.0)
    if abs(impulse.get(hour_ending(FLAT_TIMES[1]), {}).get("pv_estimate", -1) - 0.8635) > 0.0001:
        print("  ERROR: expected 0.8635 kWh in 00:00-01:00, got {}".format(impulse.get(hour_ending(FLAT_TIMES[1]))))
        failed = True
    for other in (0, 2, 3):
        if impulse.get(hour_ending(FLAT_TIMES[other]), {}).get("pv_estimate", -1) != 0.0:
            print("  ERROR: the hour ending {} should be dark, got {}".format(FLAT_TIMES[other], impulse.get(hour_ending(FLAT_TIMES[other]))))
            failed = True

    print("Test: an hour is derated with the average of the temperatures at its two ends")
    # Temperature is an instant, so 00:00-01:00 (the sample at 01:00) sees the mean of 10 and 30 degC
    warming = gti_hourly_to_period_kwh(FLAT_TIMES[:2], [1000.0, 1000.0], [10.0, 30.0], [0.0, 0.0], kwp=1.0, system_loss=0.0)
    middle = gti_hourly_to_period_kwh(FLAT_TIMES[:1], [1000.0], [20.0], [0.0], kwp=1.0, system_loss=0.0)
    if abs(warming[hour_ending(FLAT_TIMES[1])]["pv_estimate"] - middle[hour_ending(FLAT_TIMES[0])]["pv_estimate"]) > 0.0001:
        print("  ERROR: expected the 20 degC derate {}, got {}".format(middle[hour_ending(FLAT_TIMES[0])], warming[hour_ending(FLAT_TIMES[1])]))
        failed = True
    # With no sample an hour earlier, the hour takes the temperature given with its own sample
    first_hour = warming[hour_ending(FLAT_TIMES[0])]["pv_estimate"]
    alone = gti_hourly_to_period_kwh(FLAT_TIMES[:1], [1000.0], [10.0], [0.0], kwp=1.0, system_loss=0.0)[hour_ending(FLAT_TIMES[0])]["pv_estimate"]
    if abs(first_hour - alone) > 0.0001:
        print("  ERROR: the first hour should use its own sample's temperature, got {} vs {}".format(first_hour, alone))
        failed = True

    print("Test: zero irradiance produces zero energy")
    dark = gti_hourly_to_period_kwh(FLAT_TIMES, [0.0] * 4, [15.0] * 4, [1.0] * 4, kwp=5.0, system_loss=0.05)
    if any(entry["pv_estimate"] != 0.0 for entry in dark.values()):
        print("  ERROR: zero irradiance should give zero energy, got {}".format(dark))
        failed = True

    print("Test: system_loss and kwp scale the output linearly")
    scaled = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=2.0, system_loss=0.5)
    if abs(scaled[hour_ending(FLAT_TIMES[1])]["pv_estimate"] - first["pv_estimate"]) > 0.0001:
        print("  ERROR: doubling kwp and halving efficiency should cancel out, got {}".format(scaled[hour_ending(FLAT_TIMES[1])]["pv_estimate"]))
        failed = True

    print("Test: p10_fallback scales the P10 series and defaults to 0.7")
    if abs(first["pv_estimate10"] - round(first["pv_estimate"] * 0.7, 4)) > 0.0001:
        print("  ERROR: the default P10 fallback should be 0.7, got {}".format(first["pv_estimate10"]))
        failed = True
    half = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, p10_fallback=0.5)
    half_first = half[hour_ending(FLAT_TIMES[1])]
    if abs(half_first["pv_estimate10"] - round(half_first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: p10_fallback 0.5 not applied, got {}".format(half_first["pv_estimate10"]))
        failed = True

    print("Test: band_ratio scales P50 into P10 and P90, each held to its own side of P50")
    if "pv_estimate90" in first:
        print("  ERROR: pv_estimate90 should only be returned when band_ratio is given")
        failed = True
    ensemble = {FLAT_TIMES[index]: (0.4, 1.6) for index in range(4)}
    with_ensemble = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, band_ratio=ensemble)
    ensemble_first = with_ensemble[hour_ending(FLAT_TIMES[1])]
    if abs(ensemble_first["pv_estimate10"] - ensemble_first["pv_estimate"] * 0.4) > 0.0002 or abs(ensemble_first["pv_estimate90"] - ensemble_first["pv_estimate"] * 1.6) > 0.0002:
        print("  ERROR: band ratios 0.4 / 1.6 not applied to P50 {}, got {} / {}".format(ensemble_first["pv_estimate"], ensemble_first["pv_estimate10"], ensemble_first["pv_estimate90"]))
        failed = True
    inverted = {FLAT_TIMES[index]: (1.5, 0.5) for index in range(4)}
    capped = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, band_ratio=inverted)
    capped_first = capped[hour_ending(FLAT_TIMES[1])]
    if abs(capped_first["pv_estimate10"] - capped_first["pv_estimate"]) > 0.0001 or abs(capped_first["pv_estimate90"] - capped_first["pv_estimate"]) > 0.0001:
        print("  ERROR: a P10 ratio above 1 and a P90 ratio below 1 should both be held at P50, got {} / {}".format(capped_first["pv_estimate10"], capped_first["pv_estimate90"]))
        failed = True
    # Each ratio belongs to the sample with the same stamp, so it must land on the hour ending there
    per_hour = {FLAT_TIMES[index]: (0.1 * (index + 1), 1.0 + 0.1 * (index + 1)) for index in range(4)}
    aligned = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, band_ratio=per_hour)
    for index, text in enumerate(FLAT_TIMES):
        entry = aligned[hour_ending(text)]
        p10_ratio, p90_ratio = per_hour[text]
        if abs(entry["pv_estimate10"] - round(entry["pv_estimate"] * p10_ratio, 4)) > 0.0002 or abs(entry["pv_estimate90"] - round(entry["pv_estimate"] * p90_ratio, 4)) > 0.0002:
            print("  ERROR: the band ratios stamped {} should apply to the hour ending then, got {}".format(text, entry))
            failed = True
    sparse = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, band_ratio={})
    sparse_first = sparse[hour_ending(FLAT_TIMES[1])]
    if abs(sparse_first["pv_estimate10"] - sparse_first["pv_estimate"] * 0.7) > 0.0002 or abs(sparse_first["pv_estimate90"] - sparse_first["pv_estimate"] * 1.3) > 0.0002:
        print("  ERROR: hours missing from band_ratio should fall back to 0.7 / 1.3, got {} / {}".format(sparse_first["pv_estimate10"], sparse_first["pv_estimate90"]))
        failed = True

    print("Test: shading_factors apply the correct month")
    shaded = gti_hourly_to_period_kwh(FLAT_TIMES, flat_gti, flat_temp, flat_wind, kwp=1.0, system_loss=0.0, shading_factors=[0.5] * 12)
    if abs(shaded[hour_ending(FLAT_TIMES[1])]["pv_estimate"] - round(first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: a 0.5 shading factor was not applied, got {}".format(shaded[hour_ending(FLAT_TIMES[1])]["pv_estimate"]))
        failed = True
    # The month is the one the hour starts in: a sample at 00:00 on 1 July covers 23:00-00:00 on 30 June
    month_edge = gti_hourly_to_period_kwh(["2025-07-01T00:00"], [1000.0], [25.0], [0.0], kwp=1.0, system_loss=0.0, shading_factors=[1.0] * 5 + [0.5] + [1.0] * 6)
    if abs(month_edge.get(hour_ending("2025-07-01T00:00"), {}).get("pv_estimate", -1) - round(first["pv_estimate"] * 0.5, 4)) > 0.0001:
        print("  ERROR: the hour 23:00-00:00 on 30 June should take June's shading factor, got {}".format(month_edge))
        failed = True

    print("Test: samples either side of a gap each keep their own hour, and the gap stays empty")
    gapped_times = ["2025-06-01T00:00", "2025-06-01T01:00", "2025-06-01T05:00"]
    gapped = gti_hourly_to_period_kwh(gapped_times, [1000.0] * 3, [25.0] * 3, [0.0] * 3, kwp=1.0, system_loss=0.0)
    if sorted(gapped) != sorted(hour_ending(text) for text in gapped_times):
        print("  ERROR: expected one period per sample, the hours ending {}, got {}".format(gapped_times, sorted(gapped)))
        failed = True

    print("Test: a None irradiance sample is treated as zero rather than raising")
    with_none = gti_hourly_to_period_kwh(FLAT_TIMES, [None, 1000.0, 1000.0, None], [25.0] * 4, [0.0] * 4, kwp=1.0, system_loss=0.0)
    if with_none[hour_ending(FLAT_TIMES[0])]["pv_estimate"] != 0.0 or abs(with_none[hour_ending(FLAT_TIMES[1])]["pv_estimate"] - 0.8635) > 0.0001:
        print("  ERROR: a None sample should give a dark hour and leave its neighbours alone, got {}".format(with_none))
        failed = True

    return failed

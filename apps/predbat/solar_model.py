# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Shared photovoltaic conversion model.

Converts Open-Meteo global tilted irradiance (GTI) into PV energy, applying a
SAPM/PVWatts cell-temperature derate and integrating each hourly sample pair.
Shared by the live Solcast/Open-Meteo forecast path and the annual prediction
tool so the two cannot drift apart.
"""

import math
from datetime import datetime, timedelta

import pytz

from utils import dp4

# PVWatts / SAPM cell temperature model constants (glass/glass, open rack)
# Equivalent to pvlib.temperature.sapm_cell with open_rack_glass_glass parameters
_SAPM_A = -3.47
_SAPM_B = -0.0594
_SAPM_DELTA_T = 3.0

# c-Si temperature coefficient: -0.4%/degC relative to STC (25degC)
_TEMP_COEFF = 0.004
_STC_TEMP_C = 25.0

# Defaults used when a sample has no measured value
_DEFAULT_TEMP_C = 25.0
_DEFAULT_WIND_MS = 1.0

# Applied when no ensemble P10 data is available
_DEFAULT_P10_FALLBACK = 0.7


def pvwatts_cell_temperature(poa_global, temp_air, wind_speed):
    """Compute PV cell temperature using the SAPM (PVWatts) model.

    Parameters correspond to a glass/glass module on an open rack (the most
    common residential case). Formula: T_cell = T_air + GTI*exp(a + b*wind) + (GTI/1000)*deltaT
    """
    return temp_air + poa_global * math.exp(_SAPM_A + _SAPM_B * wind_speed) + (poa_global / 1000.0) * _SAPM_DELTA_T


def convert_azimuth(az):
    """
    Convert azimuth from Predbat/Solcast convention to Forecast.solar/Open-Meteo convention.
    Predbat/Solcast convention:         0 = North, -90 = East, 90 = West, 180 = South
    Forecast.solar/Open-Meteo convention: 0 = South, -90 = East, 90 = West, +/-180 = North
    """
    if az >= 0:
        az = 180 - az
    else:
        az = -180 - az

    return az


def _temperature_efficiency(gti, temp, wind):
    """Return the cell-temperature efficiency multiplier for one irradiance sample."""
    t_cell = pvwatts_cell_temperature(gti, temp, wind)
    # No lower clamp on (t_cell - 25): cool cells genuinely produce more power.
    # Cap at 1.1 (10% above STC) to prevent unrealistic gains at very cold temperatures.
    return max(0.5, min(1.1, 1.0 - _TEMP_COEFF * (t_cell - _STC_TEMP_C)))


def gti_hourly_to_period_kwh(times, gti_values, temp_values, wind_values, kwp, system_loss, shading_factors=None, p10_instant=None, p10_fallback=_DEFAULT_P10_FALLBACK):
    """Convert hourly GTI samples into per-hour PV energy for a single array.

    Open-Meteo returns point-in-time irradiance (W/m2) at the start of each hour, so the
    samples are integrated trapezoidally across each adjacent pair rather than treated as
    period energy.

    Args:
        times: list of ISO timestamp strings, "%Y-%m-%dT%H:%M", assumed UTC
        gti_values: list of global tilted irradiance values in W/m2, aligned to times
        temp_values: list of air temperatures in degC, aligned to times
        wind_values: list of wind speeds in m/s, aligned to times
        kwp: array peak power in kW
        system_loss: fractional system loss, e.g. 0.05 for 95% efficiency
        shading_factors: optional list of 12 per-month multipliers
        p10_instant: optional dict of timestamp string to raw P10 kW, before temperature derate
        p10_fallback: multiplier applied to P50 when p10_instant has no entry

    Returns:
        dict of tz-aware UTC hour-start datetime to {"pv_estimate": kWh, "pv_estimate10": kWh}
    """
    instant_kw = {}
    instant_stamps = []

    for idx, ts in enumerate(times):
        if idx >= len(gti_values):
            break
        gti = gti_values[idx]
        if gti is None:
            gti = 0.0
        temp = temp_values[idx] if idx < len(temp_values) and temp_values[idx] is not None else _DEFAULT_TEMP_C
        wind = wind_values[idx] if idx < len(wind_values) and wind_values[idx] is not None else _DEFAULT_WIND_MS
        eta_temp = _temperature_efficiency(gti, temp, wind)
        pv50_inst = dp4((gti / 1000.0) * kwp * eta_temp * (1.0 - system_loss))
        raw_p10 = p10_instant.get(ts) if p10_instant else None
        # p10_instant was computed without temperature derating; apply eta_temp now
        pv10_inst = dp4(min(raw_p10 * eta_temp, pv50_inst) if raw_p10 is not None else pv50_inst * p10_fallback)
        try:
            stamp = datetime.strptime(ts, "%Y-%m-%dT%H:%M")
            stamp = stamp.replace(tzinfo=pytz.utc)
        except (ValueError, TypeError):
            continue
        instant_kw[stamp] = (pv50_inst, pv10_inst)
        instant_stamps.append(stamp)

    period_data = {}
    for i in range(len(instant_stamps) - 1):
        stamp = instant_stamps[i]
        next_stamp = instant_stamps[i + 1]
        if (next_stamp - stamp) != timedelta(hours=1):
            continue
        pv50_start, pv10_start = instant_kw[stamp]
        pv50_end, pv10_end = instant_kw[next_stamp]
        pv50 = dp4(0.5 * (pv50_start + pv50_end))
        pv10 = dp4(0.5 * (pv10_start + pv10_end))

        if shading_factors and len(shading_factors) == 12:
            shading_month = shading_factors[stamp.month - 1]
            pv50 = dp4(pv50 * shading_month)
            pv10 = dp4(pv10 * shading_month)

        period_data[stamp] = {"pv_estimate": pv50, "pv_estimate10": pv10}

    return period_data

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""PV calibration must not learn inverter clipping as forecast error.

pv_calibration scales the forecast by actual generation over forecast for the past week. On a system whose
array can out-produce its inverter, actual generation on a sunny day is capped at the inverter limit: the
clipped energy is never generated, so no PV sensor (pv_today or an integrated power sensor) records it. A
sample sitting at the limit only says the array could have made *at least* that much, but calibration reads it
as exact, decides the forecast was too high, and scales the next sunny day down. The planner then predicts
less clipping than will happen and makes too little room in the battery for it.

The ratio is multiplicative, not a cap: after a week of 15 kW forecast against 11.5 kW delivered, an 18 kW
day is calibrated to about 13.8 kW, keeping only about a third of the real excess.

The fix these tests describe: a history sample at (or within a small margin of) the PV ceiling, while the
forecast was above it, must not pull the ratio down. The ceiling is the configured inverter_limit for a hybrid
(summed across inverters by get_arg's combine) and pv_ac_limit for an AC-coupled system, whose battery inverter
limit says nothing about the PV inverter - all read live through get_arg. Samples below the ceiling, and
samples where generation beat the forecast, still calibrate as before.

A clipped sample is treated as if the forecast had been right - its actual is taken to be the forecast - rather
than by lowering its forecast to the actual. Both give that sample a ratio of 1, but the slot and day ratios are
ratios of sums, so they weight it differently against the unclipped days around it. Only the first makes a
clipped week calibrate the same as the same week without an inverter limit, which is what the mixed-week tests
check. The day totals behind the worst and best day factors (the PV10/PV90 band) need the same treatment as
the slot averages behind P50.
"""
from datetime import timedelta
import math

from tests.test_solcast import create_test_solar_api

SUNRISE_MINUTE = 5 * 60
SUNSET_MINUTE = 21 * 60
HISTORY_DAYS = 7


def flat(kw):
    """Return a profile that is kw at every minute of the day."""
    return lambda minute_of_day: kw


def bell(peak_kw):
    """Return a clear-sky profile peaking at 13:00, zero outside 05:00-21:00."""

    def profile(minute_of_day):
        """kW at this minute of the day."""
        if SUNRISE_MINUTE <= minute_of_day <= SUNSET_MINUTE:
            return peak_kw * math.sin(math.pi * (minute_of_day - SUNRISE_MINUTE) / (SUNSET_MINUTE - SUNRISE_MINUTE))
        return 0.0

    return profile


def clipped(profile, limit_kw):
    """Return profile capped at limit_kw, as an inverter that clips would deliver it."""
    return lambda minute_of_day: min(profile(minute_of_day), limit_kw)


def calibrate(history_forecast, history_actual, future_forecast, inverter_limit_kw=None, hybrid=True, pv_ac_limit_kw=0.0, max_kwh=18.27, history_by_day=None):
    """Run pv_calibration over HISTORY_DAYS of history and return (P50 kW, total_adjustment, PV10 kW) for tomorrow.

    The P50 and PV10 results are dicts of kW by minute of day. history_forecast, history_actual and
    future_forecast are functions of minute of day returning kW. history_by_day, when given, overrides the two
    history profiles per day: it takes the number of days ago (1 is yesterday) and returns a
    (forecast, actual) pair of profiles. The forecast history is served as the uncalibrated forecast sensor (the
    series calibration learns from, GH#5116) every 30 minutes; the actual is served as the cumulative pv_today
    counter.

    The inverter settings are served as config through get_arg, as apps.yaml would supply them. inverter_limit_kw
    is one inverter's limit, or a list with one entry per inverter (inverter_limit is a per-inverter list in
    APPS_SCHEMA, so it is a list even with one inverter). None leaves inverter_limit unset.
    """
    if history_by_day is None:
        history_by_day = lambda days_ago: (history_forecast, history_actual)  # noqa: E731
    test_api = create_test_solar_api()
    solar = test_api.solar
    base = test_api.mock_base
    config = {"inverter_hybrid": hybrid, "pv_ac_limit": pv_ac_limit_kw * 1000.0}
    if inverter_limit_kw is not None:
        limits_kw = inverter_limit_kw if isinstance(inverter_limit_kw, list) else [inverter_limit_kw]
        config["inverter_limit"] = [kw * 1000.0 for kw in limits_kw]
    base.mock_config = config

    def mock_get_arg(arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None, _config=config):
        """Serve the scenario config, summing a per-inverter list for combine=True or picking by index, as the real get_arg does."""
        if arg not in _config:
            return default
        value = _config[arg]
        if isinstance(value, list):
            if combine:
                return sum(value)
            position = index if index is not None else 0
            return value[position] if position < len(value) else default
        return value

    base.get_arg = mock_get_arg

    # Uncalibrated forecast sensor history, every 30 minutes, at the forecast for that time of day (UTC, as
    # midnight_utc is)
    entries = []
    start = base.now_utc_exact - timedelta(days=HISTORY_DAYS + 1) + timedelta(minutes=7)
    for step in range((HISTORY_DAYS + 1) * 24 * 2):
        stamp = start + timedelta(minutes=30 * step)
        days_ago = max((base.midnight_utc.date() - stamp.date()).days, 1)
        # Each point holds for the 30 minutes after it, so take the forecast at the middle of that span - the value
        # at its start lags the actual by a quarter of an hour, which on the steep edges of a clear-sky day reads as
        # mornings under-forecast and evenings over-forecast whether or not anything clipped
        minute_of_day = (stamp.hour * 60 + stamp.minute + 15) % 1440
        forecast_kw = history_by_day(days_ago)[0](minute_of_day)
        entries.append({"last_updated": stamp.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": str(round(forecast_kw, 4)), "attributes": {"friendly_name": "PV Forecast Now Uncalibrated"}})
    forecast_history = [entries]

    # Cumulative pv_today kWh keyed by minutes ago, resetting at each midnight
    pv_today = {}
    for day in range(1, HISTORY_DAYS + 1):
        midnight_ago = day * 1440 + base.minutes_now
        cumulative = 0.0
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago >= 0:
                pv_today[minute_ago] = cumulative
            cumulative += history_by_day(day)[1](step) * 5 / 60.0

    def mock_minute_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, _hist=pv_today):
        """Return the synthetic pv_today history."""
        return dict(_hist) if key == "pv_today" else {}

    def mock_get_history(entity_id, days, required=False, _history=forecast_history):
        """Return the synthetic forecast history for the uncalibrated sensor only."""
        return _history if entity_id.endswith("_pv_forecast_h0_uncalibrated") else []

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = mock_get_history

    pv_minute = {minute: future_forecast(minute % 1440) / 60.0 for minute in range(4 * 24 * 60)}
    pv_data = [{"period_start": "2025-06-15T00:00:00+0000", "pv_estimate": 0.5}]
    try:
        with test_api.patch_now_utc_exact():
            adjusted, adjusted10, _, _ = solar.pv_calibration(pv_minute, dict(pv_minute), {}, pv_data, create_pv10=True, divide_by=1.0, max_kwh=max_kwh, forecast_days=solar.forecast_days)
        tomorrow = {minute_of_day: adjusted.get(1440 + minute_of_day, 0) * 60.0 for minute_of_day in range(1440)}
        tomorrow10 = {minute_of_day: adjusted10.get(1440 + minute_of_day, 0) * 60.0 for minute_of_day in range(1440)}
        return tomorrow, solar.pv_calibration_total_adjustment, tomorrow10
    finally:
        test_api.cleanup()


def check(name, got, expected, tolerance):
    """Print and return True when got is outside expected +/- tolerance (a fraction of expected)."""
    if abs(got - expected) > expected * tolerance:
        print("ERROR: {}: got {:.2f}, expected {:.2f} (+/-{:.0f}%)".format(name, got, expected, tolerance * 100))
        return True
    return False


def test_clipped_week_does_not_scale_down_same_day():
    """A week clipped at 11.5 kW against a 15 kW forecast leaves the next 15 kW day at 15 kW, not 11.5 kW."""
    tomorrow, total, _ = calibrate(flat(15.0), flat(11.5), flat(15.0), inverter_limit_kw=11.5)
    print("  clipped week, same forecast: noon {:.2f}kW, total_adjustment {}".format(tomorrow[720], total))
    failed = check("noon forecast after a clipped week", tomorrow[720], 15.0, 0.03)
    failed |= check("total_adjustment after a clipped week", total, 1.0, 0.03)
    return failed


def test_clipped_week_does_not_scale_down_sunnier_day():
    """After the same clipped week an 18 kW day stays at 18 kW, so the planner sees the full excess."""
    tomorrow, total, _ = calibrate(flat(15.0), flat(11.5), flat(18.0), inverter_limit_kw=11.5)
    print("  clipped week, sunnier day: noon {:.2f}kW, total_adjustment {}".format(tomorrow[720], total))
    return check("noon forecast for a sunnier day after a clipped week", tomorrow[720], 18.0, 0.03)


def test_slightly_over_forecast_at_limit_not_learned():
    """Generation pinned at the limit under a forecast only just above it is not learned as a 0.96 ratio."""
    tomorrow, _, _ = calibrate(flat(12.0), flat(11.5), flat(18.0), inverter_limit_kw=11.5)
    print("  forecast just above the limit: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast after generation pinned at the limit", tomorrow[720], 18.0, 0.03)


def test_midday_clipping_on_clear_sky_days():
    """Clear-sky days clipped only around midday calibrate the same as those days unclipped.

    Compared against the unclipped week rather than the raw forecast, so the small timing skew between forecast
    and generation history on the steep edges of a clear-sky day (present with or without clipping) cancels and
    only the effect of clipping is measured - at the clipped midday and at the unclipped shoulders.
    """
    history_forecast = bell(15.0)
    p50, total, _ = calibrate(history_forecast, clipped(history_forecast, 11.5), bell(18.0), inverter_limit_kw=11.5)
    control_p50, control_total, _ = calibrate(history_forecast, history_forecast, bell(18.0), inverter_limit_kw=20.0)
    shoulder = 7 * 60
    print("  clear-sky midday clipping: 13:00 {:.2f}kW vs control {:.2f}kW, 07:00 {:.2f}kW vs control {:.2f}kW, total_adjustment {} vs control {}".format(p50[780], control_p50[780], p50[shoulder], control_p50[shoulder], total, control_total))
    failed = check("13:00 forecast after midday clipping against the unclipped control", p50[780], control_p50[780], 0.03)
    failed |= check("07:00 shoulder forecast after midday clipping against the unclipped control", p50[shoulder], control_p50[shoulder], 0.03)
    failed |= check("total_adjustment after midday clipping against the unclipped control", total, control_total, 0.03)
    return failed


def test_ac_coupled_clipping_at_pv_ac_limit():
    """An AC-coupled array clipped at pv_ac_limit is not learned as forecast error either."""
    tomorrow, _, _ = calibrate(flat(15.0), flat(11.5), flat(18.0), inverter_limit_kw=5.0, hybrid=False, pv_ac_limit_kw=11.5)
    print("  AC-coupled clipped at pv_ac_limit: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast after AC-coupled clipping", tomorrow[720], 18.0, 0.03)


def mixed_week(sunny_actual):
    """Return a history_by_day for a week of clear sunny days and cloudy days at half the forecast.

    Every day is forecast as a 15 kW clear-sky bell. sunny_actual gives what the sunny days delivered; the
    cloudy days (3, 5 and 7 days ago) deliver half the forecast, peaking at 7.5 kW, well below any limit here.
    """
    forecast = bell(15.0)
    cloudy = lambda minute_of_day: 0.5 * forecast(minute_of_day)  # noqa: E731
    return lambda days_ago: (forecast, cloudy if days_ago in (3, 5, 7) else sunny_actual)


def calibrate_mixed_week_and_control():
    """Calibrate the mixed week twice: sunny days clipped at an 11.5 kW limit, and the control where they were not.

    The control delivers the sunny days in full on a 20 kW inverter, so nothing reaches its limit. A clipped
    sample says only that the array made at least the limit, so with the forecast taken as right for those
    samples the clipped week has to calibrate exactly as the control does.
    """
    future = bell(18.0)
    clipped_run = calibrate(None, None, future, inverter_limit_kw=11.5, history_by_day=mixed_week(clipped(bell(15.0), 11.5)))
    control_run = calibrate(None, None, future, inverter_limit_kw=20.0, history_by_day=mixed_week(bell(15.0)))
    return clipped_run, control_run


def test_mixed_week_p50_matches_unclipped_control():
    """P50 after a week of clipped sunny days and cloudy days matches the same week without clipping."""
    (p50, total, _), (control_p50, control_total, _) = calibrate_mixed_week_and_control()
    print("  mixed week P50: 13:00 {:.2f}kW vs control {:.2f}kW, 07:00 {:.2f}kW vs control {:.2f}kW, total_adjustment {} vs control {}".format(p50[780], control_p50[780], p50[420], control_p50[420], total, control_total))
    failed = check("mixed week 13:00 P50 against the unclipped control", p50[780], control_p50[780], 0.03)
    failed |= check("mixed week 07:00 P50 against the unclipped control", p50[420], control_p50[420], 0.03)
    failed |= check("mixed week total_adjustment against the unclipped control", total, control_total, 0.03)
    return failed


def test_mixed_week_pv10_band_matches_unclipped_control():
    """The PV10 band (PV10 over P50) after the mixed week matches the unclipped control.

    The band comes from the worst day factor, built from per-day totals rather than the slot averages behind
    P50. Clipping makes the sunny days look over-forecast, moving the average day towards the worst and
    narrowing the band. Dividing by P50 keeps this independent of the P50 test.
    """
    (p50, _, p10), (control_p50, _, control_p10) = calibrate_mixed_week_and_control()
    band = p10[780] / p50[780] if p50[780] > 0 else 0
    control_band = control_p10[780] / control_p50[780] if control_p50[780] > 0 else 0
    print("  mixed week PV10 band at 13:00: {:.3f} vs control {:.3f}".format(band, control_band))
    return check("mixed week PV10/P50 band against the unclipped control", band, control_band, 0.03)


def test_two_inverters_clip_at_their_combined_limit():
    """Two hybrid inverters clip at the sum of their limits: 6 kW + 5.5 kW behaves as one 11.5 kW inverter."""
    tomorrow, _, _ = calibrate(flat(15.0), flat(11.5), flat(18.0), inverter_limit_kw=[6.0, 5.5])
    print("  two inverters clipped at 6 + 5.5kW: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast after clipping at two inverters' combined limit", tomorrow[720], 18.0, 0.03)


def test_over_forecast_below_limit_still_learned():
    """A genuine over-forecast that never reaches the limit (cloud) is still corrected."""
    tomorrow, _, _ = calibrate(flat(15.0), flat(9.0), flat(18.0), inverter_limit_kw=11.5)
    print("  over-forecast below the limit: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast after a genuine over-forecast", tomorrow[720], 18.0 * 9.0 / 15.0, 0.03)


def test_generation_well_below_a_large_limit_still_learned():
    """With a 20 kW inverter, 11.5 kW against a 15 kW forecast is forecast error, not clipping."""
    tomorrow, _, _ = calibrate(flat(15.0), flat(11.5), flat(18.0), inverter_limit_kw=20.0)
    print("  large inverter, actual below limit: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast with generation well below the limit", tomorrow[720], 18.0 * 11.5 / 15.0, 0.03)


def test_under_forecast_at_limit_still_scales_up():
    """Generation at the limit that beat the forecast still raises it - only the downward pull is distrusted."""
    tomorrow, _, _ = calibrate(flat(10.0), flat(11.5), flat(10.0), inverter_limit_kw=11.5)
    print("  under-forecast at the limit: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast after an under-forecast at the limit", tomorrow[720], 11.5, 0.03)


def test_ac_coupled_ignores_battery_inverter_limit():
    """On an AC-coupled system without pv_ac_limit the battery inverter limit does not cap PV, so nothing is excluded."""
    tomorrow, _, _ = calibrate(flat(15.0), flat(11.5), flat(18.0), inverter_limit_kw=5.0, hybrid=False)
    print("  AC-coupled, battery inverter limit only: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast on AC-coupled without pv_ac_limit", tomorrow[720], 18.0 * 11.5 / 15.0, 0.03)


def test_no_inverter_limit_known_unchanged():
    """With no inverter limit on the base, calibration behaves exactly as before."""
    tomorrow, _, _ = calibrate(flat(15.0), flat(11.5), flat(18.0), inverter_limit_kw=None)
    print("  no inverter limit known: noon {:.2f}kW".format(tomorrow[720]))
    return check("noon forecast with no inverter limit known", tomorrow[720], 18.0 * 11.5 / 15.0, 0.03)


def run_pv_calibration_clipping_tests(my_predbat):
    """Run the PV calibration under clipping tests. Returns True on failure."""
    print("**** Running PV calibration under clipping tests ****")
    failed = False

    # Clipping must not be learned as forecast error
    failed |= test_clipped_week_does_not_scale_down_same_day()
    failed |= test_clipped_week_does_not_scale_down_sunnier_day()
    failed |= test_slightly_over_forecast_at_limit_not_learned()
    failed |= test_midday_clipping_on_clear_sky_days()
    failed |= test_ac_coupled_clipping_at_pv_ac_limit()
    failed |= test_mixed_week_p50_matches_unclipped_control()
    failed |= test_mixed_week_pv10_band_matches_unclipped_control()
    failed |= test_two_inverters_clip_at_their_combined_limit()

    # Everything that is not clipping still calibrates as before
    failed |= test_over_forecast_below_limit_still_learned()
    failed |= test_generation_well_below_a_large_limit_still_learned()
    failed |= test_under_forecast_at_limit_still_scales_up()
    failed |= test_ac_coupled_ignores_battery_inverter_limit()
    failed |= test_no_inverter_limit_known_unchanged()

    if not failed:
        print("**** PV calibration under clipping tests passed ****")
    return failed

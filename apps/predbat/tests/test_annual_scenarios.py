# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction three-scenario day runner."""

from annual import DAY_MINUTES, PLAN_MINUTES, SCENARIO_FIELDS, SCENARIO_KEYS, _blend_results, add_car_to_load, car_charging_schedule, timer_charge_window


def flat_rates(cheap_start, cheap_end, cheap_rate, peak_rate):
    """Build a 48 hour import rate dict with one cheap overnight band per day."""
    rates = {}
    for minute in range(PLAN_MINUTES):
        in_day = minute % DAY_MINUTES
        rates[minute] = cheap_rate if cheap_start <= in_day < cheap_end else peak_rate
    return rates


def test_annual_scenarios(my_predbat):
    """Verify the timer charge window and car load insertion helpers."""
    failed = False
    print("**** Testing annual scenario helpers ****")

    print("Test: timer_charge_window finds the cheapest band and sizes it to the car's energy")
    rates = flat_rates(cheap_start=30, cheap_end=330, cheap_rate=7.0, peak_rate=30.0)
    window = timer_charge_window(rates, car_kwh=14.8, car_rate_kw=7.4)
    if not window:
        print("  ERROR: expected a charge window")
        failed = True
    else:
        first = window[0]
        if first["start"] != 30:
            print("  ERROR: the window should start at the cheap band start 30, got {}".format(first["start"]))
            failed = True
        # 14.8 kWh at 7.4 kW is 2 hours
        if first["end"] - first["start"] != 120:
            print("  ERROR: expected a 120 minute window for 14.8 kWh at 7.4 kW, got {}".format(first["end"] - first["start"]))
            failed = True

    print("Test: a car needing more than the cheap band gets a window extended beyond it")
    long_window = timer_charge_window(rates, car_kwh=74.0, car_rate_kw=7.4)
    if long_window[0]["end"] - long_window[0]["start"] < 300:
        print("  ERROR: a 10 hour charge should extend past the 5 hour cheap band, got {} minutes".format(long_window[0]["end"] - long_window[0]["start"]))
        failed = True

    print("Test: a slower car_rate_kw produces a roughly proportionally longer window for the same energy")
    # Guards the annual.py config-validation fix that lets a configured car_rate_kw actually
    # reach timer_charge_window (previously validate_config() silently dropped it and every
    # run used the 7.4 kW default regardless of what was configured).
    fast_window = timer_charge_window(rates, car_kwh=14.8, car_rate_kw=7.4)
    slow_window = timer_charge_window(rates, car_kwh=14.8, car_rate_kw=3.7)
    fast_length = fast_window[0]["end"] - fast_window[0]["start"]
    slow_length = slow_window[0]["end"] - slow_window[0]["start"]
    if abs(slow_length - 2 * fast_length) > 5:
        print("  ERROR: halving car_rate_kw from 7.4 to 3.7 should roughly double the window length ({} minutes), got {} minutes for the fast case and {} minutes for the slow case".format(2 * fast_length, fast_length, slow_length))
        failed = True

    print("Test: zero car energy produces no window")
    if timer_charge_window(rates, car_kwh=0.0, car_rate_kw=7.4) != []:
        print("  ERROR: no car energy should produce no window")
        failed = True

    print("Test: timer_charge_window rounds the duration up to a 5 minute grid, never under-delivering")
    # 81 minutes of raw need at 60kW/hr rate: 81/60*60 = 81.0 kWh, not a multiple of 5 minutes.
    # step_data_history() only samples load_forecast every 5 minutes, so an unaligned window
    # would be billed at a quantised (and here shorter) length than the car actually needs.
    quantised_window = timer_charge_window(rates, car_kwh=81.0, car_rate_kw=60.0)
    quantised_length = quantised_window[0]["end"] - quantised_window[0]["start"]
    if quantised_length != 85:
        print("  ERROR: an 81 minute need should round up to 85 minutes (next multiple of 5), got {}".format(quantised_length))
        failed = True
    if quantised_window[0]["start"] % 5 != 0:
        print("  ERROR: the window start should be aligned to a 5 minute boundary, got {}".format(quantised_window[0]["start"]))
        failed = True

    print("Test: a cheapest band late in the day is pulled back so the billed day still gets the FULL car session")
    # Regression guard for the bug where a day-0 window could run past DAY_MINUTES: the
    # cheapest half-hour here is at 23:00 (minute 1380), and a 34 kWh session at 7.4 kW needs
    # about 4.6 hours - far more than the 60 minutes left in the day from 23:00. Without
    # clamping, the window would end at 1380 + ~280 = ~1660, so _billed_result (which only
    # costs minutes < DAY_MINUTES) would bill the baselines for only the ~60 minutes that fall
    # before midnight while scenario 3 (with_predbat) is always billed for the full session -
    # phantom baseline saving that can invert a month's headline predbat_vs_baseline_p.
    late_rates = flat_rates(cheap_start=1380, cheap_end=1410, cheap_rate=7.0, peak_rate=30.0)
    late_car_kwh = 34.0
    late_window = timer_charge_window(late_rates, car_kwh=late_car_kwh, car_rate_kw=7.4)
    day_zero_window = late_window[0]
    if day_zero_window["start"] < 0:
        print("  ERROR: the day-0 window start must never go negative, got {}".format(day_zero_window["start"]))
        failed = True
    if day_zero_window["end"] > DAY_MINUTES:
        print("  ERROR: the day-0 window must end within the billed day (< {}), got end={} (start={})".format(DAY_MINUTES, day_zero_window["end"], day_zero_window["start"]))
        failed = True
    late_baseline_load = add_car_to_load({minute: 0.0 for minute in range(DAY_MINUTES + 1)}, late_window, car_kwh=late_car_kwh)
    billed_day_energy = late_baseline_load[DAY_MINUTES]
    if abs(billed_day_energy - late_car_kwh) > 1e-6:
        print("  ERROR: the billed day should receive the full {} kWh session (matching what scenario 3 is always billed for), got {} kWh".format(late_car_kwh, billed_day_energy))
        failed = True

    print("Test: add_car_to_load inserts the car's full energy on EACH day, not split across the plan, and stays cumulative")
    base = {minute: 0.01 * minute for minute in range(PLAN_MINUTES + 1)}
    with_car = add_car_to_load(base, window, car_kwh=14.8)
    added_total = with_car[PLAN_MINUTES] - base[PLAN_MINUTES]
    if abs(added_total - 14.8 * 2) > 1e-6:
        print("  ERROR: expected 14.8 kWh added on each of the two days ({} total), got {}".format(14.8 * 2, added_total))
        failed = True
    added_day_one = with_car[DAY_MINUTES] - base[DAY_MINUTES]
    if abs(added_day_one - 14.8) > 1e-6:
        print("  ERROR: the billed first day (minutes 0..{}) should receive the full 14.8 kWh, not a share of it, got {}".format(DAY_MINUTES - 1, added_day_one))
        failed = True
    for minute in range(1, PLAN_MINUTES + 1):
        if with_car[minute] < with_car[minute - 1] - 1e-12:
            print("  ERROR: the series stopped being cumulative at minute {}".format(minute))
            failed = True
            break

    print("Test: add_car_to_load leaves minutes before the window untouched")
    if abs(with_car[10] - base[10]) > 1e-12:
        print("  ERROR: minutes before the window must be unchanged")
        failed = True

    print("Test: add_car_to_load does not mutate its input")
    if abs(base[PLAN_MINUTES] - 0.01 * PLAN_MINUTES) > 1e-9:
        print("  ERROR: add_car_to_load must not mutate the input series")
        failed = True

    print("Test: the car repeats on the second day of the window")
    second_day = [entry for entry in window if entry["start"] >= DAY_MINUTES]
    if not second_day:
        print("  ERROR: the timer should also charge on day two of the 48 hour window")
        failed = True

    print("Test: car_charging_schedule gives a modest annual figure a single weekly session")
    # 1500 kWh/year at 7.4 kW is a 28.8 kWh/week session, under the 6 hour cap (3.9 hours).
    sessions, session_kwh = car_charging_schedule(annual_kwh=1500, car_rate_kw=7.4)
    if sessions != 1:
        print("  ERROR: expected 1 session/week for a modest annual figure, got {}".format(sessions))
        failed = True
    if abs(session_kwh - 1500 / 52.0) > 1e-9:
        print("  ERROR: a single weekly session should carry the whole week's energy ({}), got {}".format(1500 / 52.0, session_kwh))
        failed = True

    print("Test: a session that would exceed 6 hours splits into enough sessions that each is under 6 hours")
    # 2500 kWh/year at 7.4 kW is a 48.08 kWh/week need - 6.50 hours as one session, so it must split.
    sessions, session_kwh = car_charging_schedule(annual_kwh=2500, car_rate_kw=7.4)
    if sessions <= 1:
        print("  ERROR: a session over 6 hours should have split into more than 1 session/week, got {}".format(sessions))
        failed = True
    session_hours = session_kwh / 7.4
    if session_hours > 6.0 + 1e-9:
        print("  ERROR: every split session should be under 6 hours, got {:.3f} hours across {} sessions".format(session_hours, sessions))
        failed = True

    print("Test: the session count caps at 7 even when that still exceeds 6 hours per session")
    # 8000 kWh/year at 3.0 kW is a 153.8 kWh/week need - 51.3 hours as one session. Even 7
    # sessions (7.3 hours each) cannot get under the 6 hour cap, so the cap must still hold at 7
    # rather than climbing past a session a day.
    sessions, session_kwh = car_charging_schedule(annual_kwh=8000, car_rate_kw=3.0)
    if sessions != 7:
        print("  ERROR: expected the session count to cap at 7/week, got {}".format(sessions))
        failed = True
    if session_kwh / 3.0 <= 6.0:
        print("  ERROR: expected this case to still exceed 6 hours per session even at the cap, got {:.3f} hours".format(session_kwh / 3.0))
        failed = True

    print("Test: sessions_per_week * session_kwh * 52 recovers the annual total")
    for annual_kwh, car_rate_kw in [(1500, 7.4), (2500, 7.4), (8000, 7.4), (1500, 3.0), (2500, 3.0), (8000, 3.0)]:
        sessions, session_kwh = car_charging_schedule(annual_kwh, car_rate_kw)
        recovered = sessions * session_kwh * 52.0
        if abs(recovered - annual_kwh) > 1e-6:
            print("  ERROR: {} kWh/year at {} kW should recover to {} annual kWh, got {} ({} sessions of {:.3f} kWh)".format(annual_kwh, car_rate_kw, annual_kwh, recovered, sessions, session_kwh))
            failed = True

    print("Test: no annual energy produces no sessions")
    sessions, session_kwh = car_charging_schedule(annual_kwh=0, car_rate_kw=7.4)
    if sessions != 0 or session_kwh != 0.0:
        print("  ERROR: zero annual energy should produce 0 sessions of 0 kWh, got {} sessions of {} kWh".format(sessions, session_kwh))
        failed = True

    print("Test: _blend_results blends every field of every scenario as fraction * with + (1 - fraction) * without")
    with_car_results = {key: {field: 10.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}
    without_car_results = {key: {field: 2.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}
    with_car_results["with_predbat"]["cost_p"] = 100.0
    without_car_results["with_predbat"]["cost_p"] = 20.0
    fraction = 3.0 / 7.0
    blended = _blend_results(with_car_results, without_car_results, fraction)
    expected_cost = fraction * 100.0 + (1 - fraction) * 20.0
    if abs(blended["with_predbat"]["cost_p"] - expected_cost) > 1e-9:
        print("  ERROR: expected blended with_predbat cost_p {}, got {}".format(expected_cost, blended["with_predbat"]["cost_p"]))
        failed = True
    expected_other = fraction * 10.0 + (1 - fraction) * 2.0
    if abs(blended["no_pvbat"]["import_kwh"] - expected_other) > 1e-9:
        print("  ERROR: expected blended no_pvbat import_kwh {}, got {}".format(expected_other, blended["no_pvbat"]["import_kwh"]))
        failed = True

    print("Test: _blend_results is a no-op for a field identical in both legs (pv_generated_kwh)")
    identical = {key: {field: 5.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}
    blended = _blend_results(identical, identical, fraction=0.4)
    for key in SCENARIO_KEYS:
        if blended[key]["pv_generated_kwh"] != 5.0:
            print("  ERROR: blending identical legs should be a no-op, got {} for {}".format(blended[key]["pv_generated_kwh"], key))
            failed = True

    return failed

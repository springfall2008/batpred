# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def _assert_window(name, start, end, average, expected_start, expected_end, expected_average=None):
    """Helper: assert window fields and return 0/1 failed."""
    failed = 0
    if start != expected_start:
        print("ERROR: {}: expected start={}, got start={}".format(name, expected_start, start))
        failed = 1
    if end != expected_end:
        print("ERROR: {}: expected end={}, got end={}".format(name, expected_end, end))
        failed = 1
    if expected_average is not None and average != expected_average:
        print("ERROR: {}: expected average={}, got average={}".format(name, expected_average, average))
        failed = 1
    return failed


def test_find_charge_window(my_predbat):
    """
    Comprehensive tests for find_charge_window covering all code paths:

      Path A - mixed rate exceeds combine_rate_threshold → window closed early
      Path B - combine_export_slots=False hits export_slot_split → split
      Path C - combine_charge_slots=False hits charge_slot_split → split
      Path D - manual_all_times slot split at plan_interval_minutes
      Path E - alt_rates alternate_rate_boundary terminates export window;
               also 24-hour export cap
      Path J - pv_light_dark boundary splits a charge window (#4557)
      Path F - window start (rate_low_start set for first time)
      Path G - window continuation and correct average calculation
      Path H - rate outside threshold while window in progress → closes window
      Path I - gap in rates while window in progress → closes window (regression
               for previous break-too-early bug)
      Misc  - no qualifying rates, find_high=False, find_high=True, zero rate
               excluded for find_high, scan from non-zero minute, scan stops at
               forecast boundary
    """
    failed = 0
    low_rate = 5.0
    high_rate = 20.0
    thresh_lo = 10.0   # threshold for charge (find_high=False)
    thresh_hi = 15.0   # threshold for export (find_high=True)

    # Preserve ALL settings we may temporarily change, and set known baselines
    # so the test is independent of whatever previous tests may have modified.
    old_forecast_minutes = my_predbat.forecast_minutes
    old_minutes_now = my_predbat.minutes_now
    old_combine_charge = my_predbat.combine_charge_slots
    old_combine_export = my_predbat.combine_export_slots
    old_combine_thresh = my_predbat.combine_rate_threshold
    old_manual_all_times = my_predbat.manual_all_times
    old_charge_slot_split = my_predbat.charge_slot_split
    old_export_slot_split = my_predbat.export_slot_split
    old_plan_interval = my_predbat.plan_interval_minutes

    # Fix known-good baseline values
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.minutes_now = 12 * 60
    my_predbat.combine_charge_slots = True
    my_predbat.combine_export_slots = True

    scan_end = my_predbat.forecast_minutes + my_predbat.minutes_now + 12 * 60

    # -----------------------------------------------------------------------
    # Test: no rates → no window found
    # -----------------------------------------------------------------------
    print("Test find_charge_window: empty rates dict returns no window")
    s, e, avg = my_predbat.find_charge_window({}, 0, thresh_lo, find_high=False)
    if s != -1 or e != -1:
        print("ERROR: empty rates: expected (-1,-1), got ({},{})".format(s, e))
        failed = 1

    # -----------------------------------------------------------------------
    # Test: simple charge window (find_high=False) — Path F + G
    # Rates start at 0, low for 120 min, high after → window [0, 120]
    # -----------------------------------------------------------------------
    print("Test find_charge_window: simple charge window (find_high=False)")
    rates = {}
    for m in range(0, 120, 5):
        rates[m] = low_rate
    for m in range(120, scan_end, 5):
        rates[m] = high_rate

    s, e, avg = my_predbat.find_charge_window(rates, 0, thresh_lo, find_high=False)
    failed |= _assert_window("simple charge", s, e, avg, 0, 120, low_rate)

    # -----------------------------------------------------------------------
    # Test: simple export window (find_high=True) — Path F + G
    # Rates start at 0, high for 120 min, low after → window [0, 120]
    # -----------------------------------------------------------------------
    print("Test find_charge_window: simple export window (find_high=True)")
    rates2 = {}
    for m in range(0, 120, 5):
        rates2[m] = high_rate
    for m in range(120, scan_end, 5):
        rates2[m] = low_rate

    s, e, avg = my_predbat.find_charge_window(rates2, 0, thresh_hi, find_high=True)
    failed |= _assert_window("simple export", s, e, avg, 0, 120, high_rate)

    # -----------------------------------------------------------------------
    # Test: find_high=True with zero rate — zero should NOT qualify
    # -----------------------------------------------------------------------
    print("Test find_charge_window: find_high with zero rate excluded")
    rates_zero = {0: 0.0, 5: 0.0, 10: 0.0}
    s, e, _ = my_predbat.find_charge_window(rates_zero, 0, thresh_hi, find_high=True)
    if s != -1:
        print("ERROR: zero rate for find_high should not qualify, got start={}".format(s))
        failed = 1

    # -----------------------------------------------------------------------
    # Test: rate goes ABOVE threshold while window in progress — Path H
    # Low rates 0..59, high rate at 60.. → window closes at 60
    # -----------------------------------------------------------------------
    print("Test find_charge_window: rate above threshold closes window (Path H)")
    rates_h = {}
    for m in range(0, 60, 5):
        rates_h[m] = low_rate
    for m in range(60, scan_end, 5):
        rates_h[m] = high_rate

    s, e, avg = my_predbat.find_charge_window(rates_h, 0, thresh_lo, find_high=False)
    failed |= _assert_window("path H charge close", s, e, avg, 0, 60, low_rate)

    # -----------------------------------------------------------------------
    # Test: Path A — combine_rate_threshold: second rate too different
    # Low rate 5 for 0..55, then rate 8 at 60 (diff = 3 > threshold 1.0)
    # → window closes at 60 due to combine_rate_threshold
    # -----------------------------------------------------------------------
    print("Test find_charge_window: combine_rate_threshold closes mixed window (Path A)")
    my_predbat.combine_rate_threshold = 2.0   # allow up to 2p difference
    rates_a = {}
    for m in range(0, 60, 5):
        rates_a[m] = 5.0
    for m in range(60, scan_end, 5):
        rates_a[m] = 8.5  # diff = 3.5 > 2.0

    s, e, _ = my_predbat.find_charge_window(rates_a, 0, thresh_lo, find_high=False)
    failed |= _assert_window("path A combine_rate_threshold", s, e, _, 0, 60)
    my_predbat.combine_rate_threshold = old_combine_thresh

    # -----------------------------------------------------------------------
    # Test: Path C — combine_charge_slots=False splits charge at charge_slot_split
    # Low rates from 0 to 120, charge_slot_split=30 → window [0, 30]
    # -----------------------------------------------------------------------
    print("Test find_charge_window: combine_charge_slots=False splits slot (Path C)")
    my_predbat.combine_charge_slots = False
    my_predbat.charge_slot_split = 30
    rates_c = {m: low_rate for m in range(0, 120, 5)}

    s, e, _ = my_predbat.find_charge_window(rates_c, 0, thresh_lo, find_high=False)
    failed |= _assert_window("path C charge slot split", s, e, _, 0, 30)
    my_predbat.combine_charge_slots = True

    # -----------------------------------------------------------------------
    # Test: Path B — combine_export_slots=False splits export at export_slot_split
    # High rates from 0 to 120, export_slot_split=30 → window [0, 30]
    # -----------------------------------------------------------------------
    print("Test find_charge_window: combine_export_slots=False splits slot (Path B)")
    my_predbat.combine_export_slots = False
    my_predbat.export_slot_split = 30
    rates_b = {m: high_rate for m in range(0, 120, 5)}

    s, e, _ = my_predbat.find_charge_window(rates_b, 0, thresh_hi, find_high=True)
    failed |= _assert_window("path B export slot split", s, e, _, 0, 30)
    my_predbat.combine_export_slots = True

    # -----------------------------------------------------------------------
    # Test: Path D — manual_all_times slot split at plan_interval_minutes
    # rate_low_start=0 is in manual_all_times; low rates from 0 to 120;
    # plan_interval_minutes=30 → window closes at minute 30
    # -----------------------------------------------------------------------
    print("Test find_charge_window: manual_all_times split at plan_interval_minutes (Path D)")
    my_predbat.plan_interval_minutes = 30
    my_predbat.charge_slot_split = 30
    my_predbat.manual_all_times = [0]   # start minute is a manual time
    rates_d = {m: low_rate for m in range(0, 120, 5)}

    s, e, _ = my_predbat.find_charge_window(rates_d, 0, thresh_lo, find_high=False)
    failed |= _assert_window("path D manual split", s, e, _, 0, 30)
    my_predbat.manual_all_times = []

    # -----------------------------------------------------------------------
    # Test: Path E — alternate_rate_boundary splits export window
    # find_high=True; alt_rates changes significantly between minutes 25→30;
    # high export rates throughout; plan_interval_minutes=30;
    # → export window should end at 30 (boundary reached after plan_interval)
    # -----------------------------------------------------------------------
    print("Test find_charge_window: alternate_rate_boundary splits export window (Path E)")
    my_predbat.plan_interval_minutes = 30
    my_predbat.export_slot_split = 30
    # alt_rates min=0, max=20 → alt_rate_threshold=2.0; jump of 10 qualifies
    alt_rates_e = {}
    for m in range(0, 30, 5):
        alt_rates_e[m] = 0.0      # low alt rate
    for m in range(30, scan_end, 5):
        alt_rates_e[m] = 20.0     # high alt rate (diff=20 >= threshold=2.0)

    rates_e = {m: high_rate for m in range(0, scan_end, 5)}  # all qualify

    # Window starts at 0; alternate_rate_boundary is set at minute 30;
    # at minute 30, (30 - 0) = 30 >= plan_interval_minutes=30 → break
    s, e, _ = my_predbat.find_charge_window(rates_e, 0, thresh_hi, find_high=True, alt_rates=alt_rates_e)
    failed |= _assert_window("path E alt rate boundary", s, e, _, 0, 30)

    # -----------------------------------------------------------------------
    # Test: Path J — pv_light_dark boundary splits a charge window at the light/dark
    # transition (regression test for #4557: a single long cheap-rate period
    # spanning sunrise was built as one charge window, which made low power
    # charging get abandoned for the whole thing - including the still-dark
    # hours - just because the tail overlapped PV later on).
    # find_high=False; pv_light_dark goes dark (0) -> light (1) at minute 30;
    # cheap import rate throughout; combine_charge_slots left True so only
    # the pv_light_dark boundary - not charge_slot_split - is under test.
    # -----------------------------------------------------------------------
    print("Test find_charge_window: pv_light_dark boundary splits charge window (Path J)")
    my_predbat.plan_interval_minutes = 30
    my_predbat.combine_charge_slots = True
    pv_light_dark_j = {}
    for m in range(0, 30, 5):
        pv_light_dark_j[m] = 0  # dark
    for m in range(30, scan_end, 5):
        pv_light_dark_j[m] = 1  # light

    rates_j = {m: low_rate for m in range(0, scan_end, 5)}  # cheap throughout

    s, e, _ = my_predbat.find_charge_window(rates_j, 0, thresh_lo, find_high=False, pv_light_dark=pv_light_dark_j)
    failed |= _assert_window("path J pv boundary (dark window)", s, e, _, 0, 30)

    # Scanning onward from the split should then pick up the light portion as its own window
    s2, e2, _ = my_predbat.find_charge_window(rates_j, 30, thresh_lo, find_high=False, pv_light_dark=pv_light_dark_j)
    if s2 != 30:
        print("ERROR: path J pv boundary (light window): expected start=30, got start={}".format(s2))
        failed = 1

    # No pv_light_dark supplied at all → behaves exactly as before, one uninterrupted window
    s3, e3, _ = my_predbat.find_charge_window(rates_j, 0, thresh_lo, find_high=False)
    if e3 != scan_end:
        print("ERROR: path J no pv_light_dark: expected window to run uninterrupted to {}, got end={}".format(scan_end, e3))
        failed = 1

    # -----------------------------------------------------------------------
    # Test: 24-hour export cap (Path E first condition)
    # find_high=True with high rates spanning >24h; window must cap at 24*60
    # -----------------------------------------------------------------------
    print("Test find_charge_window: 24-hour export cap (Path E)")
    rates_24h = {m: high_rate for m in range(0, scan_end, 5)}
    s, e, _ = my_predbat.find_charge_window(rates_24h, 0, thresh_hi, find_high=True)
    if e - s != 24 * 60:
        print("ERROR: 24h cap: expected window length={}, got length={}".format(24 * 60, e - s))
        failed = 1

    # -----------------------------------------------------------------------
    # Test: correct average over varying rates (Path G)
    # Three rate bands: 4, 6, 8 each for 1 slot (5 min) → average = (4+6+8)/3 = 6
    # Raise combine_rate_threshold so all three rates are combined into one window.
    # -----------------------------------------------------------------------
    print("Test find_charge_window: correct average calculation (Path G)")
    my_predbat.combine_rate_threshold = 10.0   # allow wide mix for this test
    rates_avg = {0: 4.0, 5: 6.0, 10: 8.0}
    for m in range(15, scan_end, 5):
        rates_avg[m] = high_rate   # end the window

    s, e, avg = my_predbat.find_charge_window(rates_avg, 0, thresh_lo, find_high=False)
    expected_avg = round((4.0 + 6.0 + 8.0) / 3, 2)
    failed |= _assert_window("path G average", s, e, avg, 0, 15, expected_avg)
    my_predbat.combine_rate_threshold = old_combine_thresh

    # -----------------------------------------------------------------------
    # Test: scan from non-zero starting minute
    # Low rates start at minute 60 but scanning begins from minute 0 (gap
    # before window), versus starting scan at minute 60 directly
    # -----------------------------------------------------------------------
    print("Test find_charge_window: scan from non-zero start minute")
    rates_nz = {}
    for m in range(60, 180, 5):
        rates_nz[m] = low_rate
    for m in range(180, scan_end, 5):
        rates_nz[m] = high_rate

    s, e, _ = my_predbat.find_charge_window(rates_nz, 60, thresh_lo, find_high=False)
    failed |= _assert_window("non-zero scan start", s, e, _, 60, 180)

    # -----------------------------------------------------------------------
    # Test (regression — gap handling): gap BEFORE the qualifying window
    # Rates absent for 0..55, low 60..175, high 180+
    # Scan must continue past missing entries and find window at 60
    # -----------------------------------------------------------------------
    print("Test find_charge_window: gap before qualifying window (regression)")
    rates_gap_before = {}
    for m in range(60, 180, 5):
        rates_gap_before[m] = low_rate
    for m in range(180, scan_end, 5):
        rates_gap_before[m] = high_rate

    s, e, avg = my_predbat.find_charge_window(rates_gap_before, 0, thresh_lo, find_high=False)
    failed |= _assert_window("gap before window", s, e, avg, 60, 180, low_rate)

    # -----------------------------------------------------------------------
    # Test (regression — gap handling): gap WITHIN an active window (Path I)
    # Low 0..115, gap 120..175, high 180+
    # Open window must close at first missing entry = 120
    # -----------------------------------------------------------------------
    print("Test find_charge_window: gap within active window closes it (Path I)")
    rates_gap_within = {}
    for m in range(0, 120, 5):
        rates_gap_within[m] = low_rate
    for m in range(180, scan_end, 5):
        rates_gap_within[m] = high_rate  # gap 120..175

    s, e, _ = my_predbat.find_charge_window(rates_gap_within, 0, thresh_lo, find_high=False)
    failed |= _assert_window("gap within window", s, e, _, 0, 120)

    # -----------------------------------------------------------------------
    # Test (regression): rate_scan_window finds window when initial gap precedes it
    # -----------------------------------------------------------------------
    print("Test find_charge_window: rate_scan_window finds window past initial gap")
    gap_start = my_predbat.minutes_now + 60
    window_start = gap_start + 60
    window_end = window_start + 120

    rates_rsw = {}
    for m in range(window_start, window_end, 5):
        rates_rsw[m] = low_rate
    for m in range(window_end, scan_end, 5):
        rates_rsw[m] = high_rate

    found_rates, _, _ = my_predbat.rate_scan_window(rates_rsw, 5, thresh_lo, find_high=False)
    if not found_rates:
        print("ERROR: rate_scan_window found no windows when gap precedes qualifying rates")
        failed = 1
    elif found_rates[0]["start"] != window_start:
        print("ERROR: rate_scan_window expected first window start={}, got {}".format(window_start, found_rates[0]["start"]))
        failed = 1

    # Restore all settings
    my_predbat.forecast_minutes = old_forecast_minutes
    my_predbat.minutes_now = old_minutes_now
    my_predbat.combine_charge_slots = old_combine_charge
    my_predbat.combine_export_slots = old_combine_export
    my_predbat.combine_rate_threshold = old_combine_thresh
    my_predbat.manual_all_times = old_manual_all_times
    my_predbat.charge_slot_split = old_charge_slot_split
    my_predbat.export_slot_split = old_export_slot_split
    my_predbat.plan_interval_minutes = old_plan_interval

    failed |= test_calc_dawn(my_predbat)
    failed |= test_calc_pv_light_dark(my_predbat)
    return failed


def test_calc_dawn(my_predbat):
    """
    Tests for calc_dawn (#4557): classifies pv_forecast_minute into light/dark buckets of
    plan_interval_minutes, used to split a charge window at the light/dark boundary. Dawn is the first
    bucket that crosses LOW_POWER_PV_LIGHT_FRACTION of the peak PV forecast anywhere in the dict.

      - no PV forecast at all -> empty dict
      - no PV output at all (every value zero) -> all dark, no divide-by-zero on a zero peak
      - noise straddling the threshold within a single bucket does not flip that bucket's
        classification (regression: a raw per-minute threshold would chop the window into several
        small pieces on a noisy forecast)
      - a clean dawn confirms immediately on the first bucket that crosses the threshold
      - once confirmed, a later cloud dip (dark again) does not revert the latch - a dawn detector,
        not a rise/fall tracker
      - the latch resets at each calendar day boundary so the next day's dawn is found independently
      - the same absolute PV value classifies differently depending on the peak elsewhere in the
        forecast - the threshold is a fraction of that peak, not an absolute figure
    """
    failed = 0
    old_pv_forecast_minute = my_predbat.pv_forecast_minute
    old_plan_interval = my_predbat.plan_interval_minutes
    old_low_power = my_predbat.set_charge_low_power
    my_predbat.plan_interval_minutes = 30
    my_predbat.set_charge_low_power = True

    def set_buckets(bucket_values):
        """Fill pv_forecast_minute with one value per plan_interval_minutes bucket, from minute 0."""
        interval = my_predbat.plan_interval_minutes
        my_predbat.pv_forecast_minute = {}
        for bucket_index, value in enumerate(bucket_values):
            for m in range(bucket_index * interval, bucket_index * interval + interval, 5):
                my_predbat.pv_forecast_minute[m] = value

    peak = 1.0  # an arbitrary "midday" peak somewhere in the forecast - threshold is 10% of this
    light = peak * 0.15  # above the 10% threshold
    dark = peak * 0.05  # below it
    dawn_bucket_index = 2  # bucket where dawn happens in most scenarios below; peak sits after it

    print("Test calc_dawn: no PV forecast")
    my_predbat.pv_forecast_minute = {}
    result = my_predbat.calc_dawn()
    if result != {}:
        print("ERROR: calc_dawn: expected empty dict for no PV forecast, got {}".format(result))
        failed = 1

    print("Test calc_dawn: no PV output at all classifies everything dark, no divide-by-zero")
    set_buckets([0.0, 0.0, 0.0])
    result = my_predbat.calc_dawn()
    if any(v != 0 for v in result.values()):
        print("ERROR: calc_dawn: an all-zero forecast should classify everything dark, got {}".format(result))
        failed = 1

    print("Test calc_dawn: noise straddling the threshold within one bucket stays stable")
    my_predbat.pv_forecast_minute = {}
    # Average across the bucket is just below threshold, but individual minutes swing both sides of
    # it - a raw per-minute comparison would flip-flop within this single bucket. A late peak bucket
    # establishes what "threshold" means without affecting the noisy bucket's own average.
    noisy_values = [dark * 1.3, dark * 0.6, dark * 1.2, dark * 0.5, dark * 0.9, dark * 0.7]
    for i, m in enumerate(range(0, 30, 5)):
        my_predbat.pv_forecast_minute[m] = noisy_values[i]
    my_predbat.pv_forecast_minute[30] = peak
    result = my_predbat.calc_dawn()
    classifications = {result[m] for m in range(0, 30, 5)}
    if len(classifications) != 1:
        print("ERROR: calc_dawn: noisy bucket should have a single stable classification, got {}".format({m: result[m] for m in range(0, 30, 5)}))
        failed = 1

    print("Test calc_dawn: clean dawn confirms immediately on the first crossed bucket")
    set_buckets([dark, dark, light, peak])
    result = my_predbat.calc_dawn()
    if result.get(0) != 0 or result.get(30) != 0:
        print("ERROR: calc_dawn: dark buckets before dawn should classify 0, got {}".format({m: result.get(m) for m in (0, 30)}))
        failed = 1
    if result.get(dawn_bucket_index * 30) != 1:
        print("ERROR: calc_dawn: first crossed bucket should confirm dawn immediately, got {}".format(result.get(dawn_bucket_index * 30)))
        failed = 1

    print("Test calc_dawn: a later cloud dip after confirmed dawn stays latched light")
    set_buckets([dark, dark, light, dark, light, peak])
    result = my_predbat.calc_dawn()
    if result.get(60) != 1:
        print("ERROR: calc_dawn: dawn should be confirmed at minute 60, got {}".format(result.get(60)))
        failed = 1
    if result.get(90) != 1:
        print("ERROR: calc_dawn: bucket after a cloud dip should stay latched at 1, got {}".format(result.get(90)))
        failed = 1
    if result.get(120) != 1:
        print("ERROR: calc_dawn: bucket after the dip should classify 1, got {}".format(result.get(120)))
        failed = 1

    print("Test calc_dawn: latch resets at the next calendar day boundary")
    my_predbat.pv_forecast_minute = {}
    day_minutes = 24 * 60
    # Day 1: dawn at minute 60, light for the rest of the day.
    for m in range(0, day_minutes, 5):
        my_predbat.pv_forecast_minute[m] = light if m >= 60 else dark
    my_predbat.pv_forecast_minute[day_minutes - 30] = peak
    # Day 2: dark again until its own dawn at minute 90 into the day.
    for m in range(day_minutes, day_minutes + 150, 5):
        my_predbat.pv_forecast_minute[m] = light if (m - day_minutes) >= 90 else dark
    result = my_predbat.calc_dawn()
    if result.get(day_minutes - 5) != 1:
        print("ERROR: calc_dawn: end of day 1 should still be latched light, got {}".format(result.get(day_minutes - 5)))
        failed = 1
    if result.get(day_minutes) != 0:
        print("ERROR: calc_dawn: start of day 2 should reset to dark, got {}".format(result.get(day_minutes)))
        failed = 1
    if result.get(day_minutes + 60) != 0:
        print("ERROR: calc_dawn: day 2 should stay dark before its own dawn, got {}".format(result.get(day_minutes + 60)))
        failed = 1
    if result.get(day_minutes + 90) != 1:
        print("ERROR: calc_dawn: day 2 should reach its own dawn at minute 90, got {}".format(result.get(day_minutes + 90)))
        failed = 1

    print("Test calc_dawn: threshold scales with the forecast's own peak, not an absolute figure")
    fixed_pv_value = 0.5
    # Against a high peak, fixed_pv_value is well under 10% and should stay dark.
    set_buckets([fixed_pv_value, 10.0])
    result = my_predbat.calc_dawn()
    if result.get(0) != 0:
        print("ERROR: calc_dawn: {}kWh/min should be dark against a peak of 10.0, got {}".format(fixed_pv_value, result.get(0)))
        failed = 1
    # Against a low peak, the same fixed_pv_value comfortably exceeds 10% and should be light.
    set_buckets([fixed_pv_value, 1.0])
    result = my_predbat.calc_dawn()
    if result.get(0) != 1:
        print("ERROR: calc_dawn: {}kWh/min should be light against a peak of 1.0, got {}".format(fixed_pv_value, result.get(0)))
        failed = 1

    my_predbat.pv_forecast_minute = old_pv_forecast_minute
    my_predbat.plan_interval_minutes = old_plan_interval
    my_predbat.set_charge_low_power = old_low_power
    return failed


def test_calc_pv_light_dark(my_predbat):
    """
    Tests for calc_pv_light_dark: decides whether calc_dawn is worth computing at all.

    Only combine_charge_slots can merge a charge window across dawn, so that's what gates the split -
    not set_charge_low_power. With combine_charge_slots off, find_charge_window already forces a break
    every charge_slot_split (=plan_interval_minutes) minutes regardless, so the dawn boundary could
    never be reached and computing it would be a no-op:

      - combine_charge_slots=True, set_charge_low_power=False -> dawn split still runs (the new case -
        the optimizer can pick the dark portion of a combined window on its own merits)
      - combine_charge_slots=True, set_charge_low_power=True -> dawn split runs (unchanged behaviour)
      - combine_charge_slots=False, regardless of set_charge_low_power -> {} (moot, skipped)
    """
    failed = 0
    old_pv_forecast_minute = my_predbat.pv_forecast_minute
    old_plan_interval = my_predbat.plan_interval_minutes
    old_combine_charge = my_predbat.combine_charge_slots
    old_low_power = my_predbat.set_charge_low_power

    my_predbat.plan_interval_minutes = 30
    my_predbat.pv_forecast_minute = {}
    for m in range(0, 30, 5):
        my_predbat.pv_forecast_minute[m] = 0.0  # dark
    for m in range(30, 60, 5):
        my_predbat.pv_forecast_minute[m] = 1.0  # light, crosses the threshold

    print("Test calc_pv_light_dark: combine_charge_slots=True, set_charge_low_power=False -> dawn split still runs")
    my_predbat.combine_charge_slots = True
    my_predbat.set_charge_low_power = False
    result = my_predbat.calc_pv_light_dark()
    if result != my_predbat.calc_dawn():
        print("ERROR: calc_pv_light_dark: expected calc_dawn's result when combine_charge_slots is True, got {}".format(result))
        failed = 1
    if result.get(0) != 0 or result.get(30) != 1:
        print("ERROR: calc_pv_light_dark: expected a dark->light split at minute 30, got {}".format({m: result.get(m) for m in (0, 30)}))
        failed = 1

    print("Test calc_pv_light_dark: combine_charge_slots=True, set_charge_low_power=True -> dawn split runs")
    my_predbat.set_charge_low_power = True
    result = my_predbat.calc_pv_light_dark()
    if result != my_predbat.calc_dawn():
        print("ERROR: calc_pv_light_dark: expected calc_dawn's result when combine_charge_slots is True, got {}".format(result))
        failed = 1

    print("Test calc_pv_light_dark: combine_charge_slots=False, set_charge_low_power=True -> {} (moot, skipped)")
    my_predbat.combine_charge_slots = False
    my_predbat.set_charge_low_power = True
    result = my_predbat.calc_pv_light_dark()
    if result != {}:
        print("ERROR: calc_pv_light_dark: expected {{}} when combine_charge_slots is False, got {}".format(result))
        failed = 1

    print("Test calc_pv_light_dark: combine_charge_slots=False, set_charge_low_power=False -> {}")
    my_predbat.set_charge_low_power = False
    result = my_predbat.calc_pv_light_dark()
    if result != {}:
        print("ERROR: calc_pv_light_dark: expected {{}} when combine_charge_slots is False, got {}".format(result))
        failed = 1

    my_predbat.pv_forecast_minute = old_pv_forecast_minute
    my_predbat.plan_interval_minutes = old_plan_interval
    my_predbat.combine_charge_slots = old_combine_charge
    my_predbat.set_charge_low_power = old_low_power
    return failed

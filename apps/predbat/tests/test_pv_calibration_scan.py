# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for SolarAPI.pv_calibration_scan (history turned into per-slot day evidence) and pv_calibration_settings."""

from datetime import timedelta

from tests.test_pv_calibration_clipping import bell
from tests.test_solcast import create_test_solar_api


def build_history(test_api, days, forecast_kw, actual_kw, pv_scaling=1.0, pv_today_from_minutes_ago=None, calls=None):
    """
    Mock the two history readers for `days` past days: the uncalibrated forecast sensor every 30 minutes (unscaled: pv_forecast_history
    applies pv_scaling) and the cumulative pv_today counter every 5 minutes. pv_today_from_minutes_ago, when
    given, cuts the pv_today history off older than that. calls, when given, records each reader's keyword arguments.
    """
    solar = test_api.solar
    base = test_api.mock_base
    solar.pv_scaling = pv_scaling
    entries = []
    start = base.now_utc_exact - timedelta(days=days + 2) + timedelta(minutes=7)
    for step in range((days + 2) * 48):
        stamp = start + timedelta(minutes=30 * step)
        minute_of_day = (stamp.hour * 60 + stamp.minute + 15) % 1440
        entries.append({"last_updated": stamp.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": str(round(forecast_kw(minute_of_day), 4)), "attributes": {}})
    pv_today = {}
    for day in range(1, days + 1):
        midnight_ago = day * 1440 + base.minutes_now
        cumulative = 0.0
        for step in range(0, 24 * 60, 5):
            minute_ago = midnight_ago - step
            if minute_ago >= 0 and (pv_today_from_minutes_ago is None or minute_ago <= pv_today_from_minutes_ago):
                pv_today[minute_ago] = cumulative
            cumulative += actual_kw(step) * 5 / 60.0

    def mock_minute_import_export(max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, required=True, **kwargs):
        """The synthetic pv_today history."""
        if calls is not None:
            calls.append(("minute_data_import_export", kwargs))
        return dict(pv_today) if key == "pv_today" else {}

    def mock_get_history(entity_id, days, required=False, **kwargs):
        """The synthetic forecast history, for the uncalibrated sensor only."""
        if calls is not None:
            calls.append(("get_history_wrapper", kwargs))
        return [entries] if entity_id.endswith("_pv_forecast_h0_uncalibrated") else []

    base.minute_data_import_export = mock_minute_import_export
    solar.get_history_wrapper = mock_get_history


def check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def day_totals(record):
    """(forecast, actual) kWh summed over a day's slots."""
    return sum(f for f, _ in record["slots"].values()), sum(a for _, a in record["slots"].values())


def test_scan_builds_days():
    """Five days of a 4 kW bell at 90% become five evidence days with every slot, the ratio and the measured peak."""
    test_api = create_test_solar_api()
    failed = False
    try:
        build_history(test_api, 5, bell(4.0), lambda m: 0.9 * bell(4.0)(m))
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(5)
        failed |= check("five days", evidence is not None and len(evidence) == 5, evidence and sorted(evidence))
        slots = 1440 // test_api.solar.plan_interval_minutes
        for day, record in (evidence or {}).items():
            forecast, actual = day_totals(record)
            failed |= check("{} slots".format(day), len(record["slots"]) == slots, len(record["slots"]))
            failed |= check("{} ratio".format(day), forecast > 0 and abs(actual / forecast - 0.9) < 0.01, (actual, forecast))
            failed |= check("{} peak".format(day), abs(record["peak_kw"] - 3.6) < 0.05, record["peak_kw"])
    finally:
        test_api.cleanup()
    return failed


def test_scan_forecast_before_pv_scaling():
    """The stored forecast is the provider's figure: pv_forecast_history's pv_scaling is taken back out."""
    test_api = create_test_solar_api()
    failed = False
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0))
        with test_api.patch_now_utc_exact():
            plain = test_api.solar.pv_calibration_scan(3)
        build_history(test_api, 3, bell(4.0), bell(4.0), pv_scaling=2.0)
        with test_api.patch_now_utc_exact():
            scaled = test_api.solar.pv_calibration_scan(3)
        for day in plain:
            failed |= check("{} unscaled".format(day), abs(day_totals(scaled[day])[0] - day_totals(plain[day])[0]) < 0.01, (day_totals(scaled[day])[0], day_totals(plain[day])[0]))
    finally:
        test_api.cleanup()
    return failed


def test_scan_pv_scaling_zero_stores_nothing():
    """With pv_scaling 0 there is no usable forecast to store."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0), pv_scaling=0.0)
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("pv_scaling 0", evidence == {}, evidence)


def test_scan_skips_partially_covered_day():
    """A day the pv_today history only reaches part of is not stored; the full days after it are."""
    test_api = create_test_solar_api()
    try:
        base = test_api.mock_base
        build_history(test_api, 3, bell(4.0), bell(4.0), pv_today_from_minutes_ago=2 * 1440 + base.minutes_now + 12 * 60)  # from 12:00 three days ago
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("partial day", evidence is not None and len(evidence) == 2, evidence and sorted(evidence))


def test_scan_tracked_false_passed():
    """A long read passes tracked=False to both history readers."""
    test_api = create_test_solar_api()
    calls = []
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0), calls=calls)
        with test_api.patch_now_utc_exact():
            test_api.solar.pv_calibration_scan(3, tracked=False)
    finally:
        test_api.cleanup()
    readers = {name for name, kwargs in calls if kwargs.get("tracked") is False}
    return check("tracked", readers == {"minute_data_import_export", "get_history_wrapper"}, calls)


def test_scan_history_missing_returns_none():
    """No pv_today history means the read failed: None, so the stored evidence is left alone."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0))
        test_api.mock_base.minute_data_import_export = lambda *args, **kwargs: {}
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("missing history", evidence is None, evidence)


def test_settings_defaults_and_overrides():
    """Defaults apply when apps.yaml sets nothing; a set value is used and listed as overridden."""
    test_api = create_test_solar_api()
    failed = False
    try:
        settings = test_api.solar.pv_calibration_settings()
        failed |= check("defaults", (settings["history_days"], settings["scan_days"], settings["half_life"], settings["shrink"], settings["shrink_slot"]) == (28, 2, 3.5, 0.20, 0.20), settings)
        failed |= check("no overrides", settings["overridden"] == set(), settings["overridden"])
        test_api.mock_base.mock_config = {"pv_calibration_shrink": 0.3}
        settings = test_api.solar.pv_calibration_settings()
        failed |= check("override", settings["shrink"] == 0.3 and "shrink" in settings["overridden"], settings)
    finally:
        test_api.cleanup()
    return failed


def test_settings_clamp_and_warn():
    """scan_days is held to history_days, under 3 history days warns, and an invalid value falls back with a warning."""
    test_api = create_test_solar_api()
    logged = []
    failed = False
    try:
        test_api.solar.log = lambda message: logged.append(message)
        test_api.mock_base.mock_config = {"pv_calibration_history_days": 2, "pv_calibration_scan_days": 5, "pv_calibration_half_life": -1}
        settings = test_api.solar.pv_calibration_settings()
        failed |= check("clamp", settings["scan_days"] == 2 and settings["history_days"] == 2, settings)
        failed |= check("half_life fallback", settings["half_life"] == 3.5, settings)
        failed |= check("warn 3 days", any(message.startswith("Warn:") and "3 days" in message for message in logged), logged)
        failed |= check("warn invalid", any(message.startswith("Warn:") and "pv_calibration_half_life" in message for message in logged), logged)
    finally:
        test_api.cleanup()
    return failed


def evidence_days(test_api, ratios, total_kwh=None, peaks=None):
    """
    Evidence for pv_calibration from a list of day ratios (index 0 = yesterday): a 4 kW bell forecast at the plan interval,
    scaled to total_kwh[i] when given, with actual = forecast x ratio, and peak_kw from peaks[i] (default 3.0).
    """
    from pv_calibration import pv_day

    solar = test_api.solar
    base = test_api.mock_base
    step = solar.plan_interval_minutes
    profile = {minute: bell(4.0)(minute + step / 2) * step / 60.0 for minute in range(0, 1440, step)}
    profile_total = sum(profile.values())
    evidence = {}
    for index, ratio in enumerate(ratios):
        scale = (total_kwh[index] / profile_total) if total_kwh else 1.0
        slots = {minute: [kwh * scale, kwh * scale * ratio] for minute, kwh in profile.items()}
        day = pv_day(base.now_utc, base.minutes_now + index * 1440 + 720, base.local_tz)
        evidence[day] = {"slots": slots, "peak_kw": peaks[index] if peaks else 3.0, "clipped_minutes": 0}
    return evidence


def run_calibration(test_api, evidence):
    """Run pv_calibration on a flat future forecast with the given evidence and return the solar API."""
    solar = test_api.solar
    solar.pv_scaling = 1.0
    solar.pv_calibration_evidence = evidence
    pv_minute = {minute: bell(4.0)(minute % 1440) / 60.0 for minute in range(4 * 24 * 60)}
    pv_data = [{"period_start": "2025-06-15T00:00:00+0000", "pv_estimate": 0.5}]
    with test_api.patch_now_utc_exact():
        solar.pv_calibration(pv_minute, dict(pv_minute), {}, pv_data, create_pv10=True, divide_by=1.0, max_kwh=18.0, forecast_days=solar.forecast_days)
    return solar


def no_history(*args, **kwargs):
    """A history reader that must not be called."""
    raise AssertionError("history was read although the store has evidence")


def test_calibration_sunny_after_dull_not_cut():
    """Five sunny-forecast days that turned out dull, after 23 good days, do not cut the next sunny day as hard as the 7-day calibration did."""
    test_api = create_test_solar_api()
    try:
        ratios = [0.5] * 5 + [0.95] * 23
        totals = [12.0] * 5 + [20.0] * 23
        solar = run_calibration(test_api, evidence_days(test_api, ratios, totals))
        print("  sunny after dull: total_adjustment {} (the 7-day calibration gave about 0.62)".format(solar.pv_calibration_total_adjustment))
        failed = check("sunny after dull", solar.pv_calibration_total_adjustment >= 0.72, solar.pv_calibration_total_adjustment)
    finally:
        test_api.cleanup()
    return failed


def test_calibration_uses_store_not_history():
    """With stored evidence the history readers are not touched."""
    test_api = create_test_solar_api()
    try:
        test_api.mock_base.minute_data_import_export = no_history
        test_api.solar.get_history_wrapper = no_history
        solar = run_calibration(test_api, evidence_days(test_api, [0.8] * 10))
        failed = check("store used", abs(solar.pv_calibration_total_adjustment - 0.8) < 0.03, solar.pv_calibration_total_adjustment)
    finally:
        test_api.cleanup()
    return failed


def test_calibration_falls_back_to_scan():
    """With no stored evidence the history is scanned directly, for pv_calibration_history_days."""
    test_api = create_test_solar_api()
    calls = []
    try:
        evidence = evidence_days(test_api, [0.8] * 10)

        def mock_scan(days, tracked=True):
            """Record the call and return the evidence."""
            calls.append(days)
            return evidence

        test_api.solar.pv_calibration_scan = mock_scan
        solar = run_calibration(test_api, None)
        failed = check("scan called", calls == [28], calls)
        failed |= check("scan used", abs(solar.pv_calibration_total_adjustment - 0.8) < 0.03, solar.pv_calibration_total_adjustment)
    finally:
        test_api.cleanup()
    return failed


def test_calibration_band_from_last_seven_days():
    """The P10/P90 band comes from the last 7 days only."""
    from pv_calibration import band_scaling, pv_day

    test_api = create_test_solar_api()
    try:
        ratios = [0.6, 0.7, 0.8, 0.9, 1.0, 0.65, 0.75] + [0.9] * 20
        evidence = evidence_days(test_api, ratios)
        solar = run_calibration(test_api, evidence)
        today = pv_day(test_api.mock_base.now_utc, 0, test_api.mock_base.local_tz)
        worst, best, _ = band_scaling(evidence, today, 1.0, 7)
        failed = check("band worst", abs(solar.pv_calibration_worst_scaling - worst) < 1e-6, (solar.pv_calibration_worst_scaling, worst))
        failed |= check("band best", abs(solar.pv_calibration_best_scaling - best) < 1e-6, (solar.pv_calibration_best_scaling, best))
    finally:
        test_api.cleanup()
    return failed


def test_calibration_off_under_three_days():
    """Two valid days: no calibration, and the default 0.7 / 1.3 band."""
    test_api = create_test_solar_api()
    try:
        solar = run_calibration(test_api, evidence_days(test_api, [0.8, 0.8]))
        failed = check("off total", solar.pv_calibration_total_adjustment == 1.0, solar.pv_calibration_total_adjustment)
        failed |= check("off band", (solar.pv_calibration_worst_scaling, solar.pv_calibration_best_scaling) == (0.7, 1.3), (solar.pv_calibration_worst_scaling, solar.pv_calibration_best_scaling))
    finally:
        test_api.cleanup()
    return failed


def test_calibration_peak_from_last_seven_days():
    """The observed peak behind the array cap comes from the last 7 days, not a summer peak weeks ago."""
    test_api = create_test_solar_api()
    logged = []
    try:
        test_api.solar.log = lambda message: logged.append(message)
        peaks = [3.0] * 7 + [3.0] * 12 + [6.0] + [3.0] * 5
        run_calibration(test_api, evidence_days(test_api, [0.9] * 25, peaks=peaks))
        failed = check("peak", any("max historical power 3.0" in message for message in logged), [m for m in logged if "max historical" in m])
    finally:
        test_api.cleanup()
    return failed


def test_calibration_band_default_when_recent_week_down():
    """A down last week after good older days keeps the default 0.7 / 1.3 band rather than collapsing it to 1.0 / 1.0."""
    test_api = create_test_solar_api()
    try:
        solar = run_calibration(test_api, evidence_days(test_api, [0.05] * 7 + [0.9] * 10))
        failed = check("band default", (solar.pv_calibration_worst_scaling, solar.pv_calibration_best_scaling) == (0.7, 1.3), (solar.pv_calibration_worst_scaling, solar.pv_calibration_best_scaling))
    finally:
        test_api.cleanup()
    return failed


def test_calibration_empty_store_does_not_rescan():
    """An empty store after the update has run (evidence {}) is not followed by a second, tracked history read in pv_calibration."""
    test_api = create_test_solar_api()
    calls = []
    try:

        def mock_scan(days, tracked=True):
            """Record the call."""
            calls.append((days, tracked))
            return {}

        test_api.solar.pv_calibration_scan = mock_scan
        solar = run_calibration(test_api, {})
        failed = check("no rescan", calls == [], calls)
        failed |= check("off", solar.pv_calibration_total_adjustment == 1.0, solar.pv_calibration_total_adjustment)
    finally:
        test_api.cleanup()
    return failed


def test_scan_empty_forecast_history_is_a_failed_read():
    """No forecast history is a failed read (None), so the 28-day first scan is retried rather than marked done."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0))
        test_api.solar.get_history_wrapper = lambda *args, **kwargs: []
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
    finally:
        test_api.cleanup()
    return check("empty forecast history", evidence is None, evidence)


def test_calibration_band_default_when_recent_week_mostly_down():
    """One valid day in the last 7 (and older good days) keeps the default 0.7 / 1.3 band, as the 7-day calibration did."""
    test_api = create_test_solar_api()
    try:
        solar = run_calibration(test_api, evidence_days(test_api, [0.05] * 6 + [0.9] + [0.9] * 14))
        failed = check("band default mostly down", (solar.pv_calibration_worst_scaling, solar.pv_calibration_best_scaling) == (0.7, 1.3), (solar.pv_calibration_worst_scaling, solar.pv_calibration_best_scaling))
    finally:
        test_api.cleanup()
    return failed


def test_scan_clipped_minutes_follow_pv_scaling():
    """Clipped minutes are stored so a later pv_scaling change still applies to them: at the clipped noon slot actual / forecast stays 1."""
    from pv_calibration import fit_pv_calibration, pv_day

    test_api = create_test_solar_api()
    try:
        test_api.mock_base.mock_config = {"inverter_hybrid": True, "inverter_limit": 3000.0}
        build_history(test_api, 5, bell(4.0), lambda m: min(bell(4.0)(m), 3.0))
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(5)
        today = pv_day(test_api.mock_base.now_utc, 0, test_api.mock_base.local_tz)
        model = fit_pv_calibration(evidence, today, 0.8, 3.5, 1e6, 1e6, test_api.solar.plan_interval_minutes)
        failed = check("clipped noon follows pv_scaling", model is not None and abs(model["raw_adj"][720] - 1.0) < 0.05, model and model["raw_adj"][720])
    finally:
        test_api.cleanup()
    return failed


def forecast_from(test_api, minutes_ago):
    """Cut the mocked forecast history so it starts minutes_ago before now (Predbat installed then)."""
    from datetime import timedelta

    solar = test_api.solar
    wrapped = solar.get_history_wrapper
    cut = (test_api.mock_base.now_utc_exact - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")

    def history(entity_id, days, required=False, **kwargs):
        """The forecast history from the cut onwards."""
        rows = wrapped(entity_id, days, required, **kwargs)
        return [[row for row in rows[0] if row["last_updated"] >= cut]] if rows else rows

    solar.get_history_wrapper = history


def test_scan_short_history_keeps_whole_days():
    """Starting from no history (both histories begin at 14:00 three days ago): the part day is skipped and the two full days are stored whole."""
    test_api = create_test_solar_api()
    try:
        start_ago = 2 * 1440 + test_api.mock_base.minutes_now + 10 * 60
        build_history(test_api, 4, bell(4.0), lambda m: 0.9 * bell(4.0)(m), pv_today_from_minutes_ago=start_ago)
        forecast_from(test_api, start_ago)
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(4)
        slots = 1440 // test_api.solar.plan_interval_minutes
        failed = check("two days", evidence is not None and len(evidence) == 2, evidence and sorted(evidence))
        for day, record in (evidence or {}).items():
            forecast, actual = day_totals(record)
            failed |= check("{} whole".format(day), len(record["slots"]) == slots and abs(actual / forecast - 0.9) < 0.01, (len(record["slots"]), actual / forecast if forecast else None))
    finally:
        test_api.cleanup()
    return failed


def test_scan_skips_day_forecast_starts_after_generation():
    """Generation history older than Predbat's forecast history (Predbat installed at 14:00 on an existing system): that day is skipped."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 4, bell(4.0), lambda m: bell(4.0)(m) * (0.7 if m < 780 else 1.2))
        forecast_from(test_api, 2 * 1440 + test_api.mock_base.minutes_now + 10 * 60)
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(4)
        slots = 1440 // test_api.solar.plan_interval_minutes
        failed = check("part day skipped", evidence is not None and len(evidence) == 2 and all(len(record["slots"]) == slots for record in evidence.values()), evidence and {day: len(record["slots"]) for day, record in evidence.items()})
    finally:
        test_api.cleanup()
    return failed


def test_scan_generation_on_its_own_minutes():
    """A 5-minute pv_today increment is put on the minutes it was measured over: generation starting at 10:00 shows in the 10:00 slot, not 09:55."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), lambda m: 2.0 if 600 <= m < 960 else 0.0)
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
        failed = check("scanned", bool(evidence), evidence)
        for day, record in (evidence or {}).items():
            failed |= check("{} nothing before 10:00".format(day), record["slots"][595][1] == 0, record["slots"][595])
            failed |= check("{} generation from 10:00".format(day), record["slots"][600][1] > 0, record["slots"][600])
            failed |= check("{} nothing from 16:00".format(day), record["slots"][960][1] == 0, record["slots"][960])
    finally:
        test_api.cleanup()
    return failed


def test_settings_warn_once():
    """An invalid setting is warned about once, not on every forecast refresh."""
    test_api = create_test_solar_api()
    logged = []
    try:
        test_api.solar.log = lambda message: logged.append(message)
        test_api.mock_base.mock_config = {"pv_calibration_shrink": -1, "pv_calibration_history_days": 2}
        for _ in range(3):
            test_api.solar.pv_calibration_settings()
        failed = check("shrink warned once", sum(1 for message in logged if "pv_calibration_shrink" in message) == 1, logged)
        failed |= check("history warned once", sum(1 for message in logged if "pv_calibration_history_days" in message) == 1, logged)
        test_api.mock_base.mock_config = {"pv_calibration_shrink": -2, "pv_calibration_history_days": 2}
        test_api.solar.pv_calibration_settings()
        failed |= check("a new bad value is warned again", sum(1 for message in logged if "pv_calibration_shrink" in message) == 2, logged)
    finally:
        test_api.cleanup()
    return failed


def test_scan_no_forecast_history_skips_generation_read():
    """With no forecast history the scan fails before the long pv_today read, so an hourly retry stays cheap."""
    test_api = create_test_solar_api()
    try:
        build_history(test_api, 3, bell(4.0), bell(4.0))
        test_api.solar.get_history_wrapper = lambda *args, **kwargs: []
        test_api.mock_base.minute_data_import_export = no_history
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(3)
        failed = check("no forecast history", evidence is None, evidence)
    finally:
        test_api.cleanup()
    return failed


def test_scan_slots_in_local_time_across_clock_change():
    """Days before the autumn clock change keep their local half-hours: generation from 10:00 BST shows at 10:00, not 09:00."""
    from datetime import datetime, timedelta

    import pytz

    test_api = create_test_solar_api()
    try:
        base = test_api.mock_base
        london = pytz.timezone("Europe/London")
        base.local_tz = london
        test_api.solar.local_tz = london
        base.now_utc = datetime(2025, 10, 28, 12, 0, tzinfo=pytz.utc)  # GMT, two days after the change
        base.now_utc_exact = base.now_utc
        base.midnight_utc = datetime(2025, 10, 28, 0, 0, tzinfo=pytz.utc)
        base.minutes_now = 12 * 60
        days = 5

        def local_minute(instant):
            """Local wall-clock minute of day."""
            local = instant.astimezone(london)
            return local.hour * 60 + local.minute

        actual_kw = lambda minute: 2.0 if 600 <= minute < 960 else 0.0  # noqa: E731
        pv_today = {}
        cumulative = 0.0
        last_date = None
        for minutes_ago in range((days + 1) * 1440, -1, -5):
            instant = base.now_utc - timedelta(minutes=minutes_ago)
            local_date = instant.astimezone(london).date()
            if local_date != last_date:
                cumulative, last_date = 0.0, local_date
            pv_today[minutes_ago] = cumulative
            cumulative += actual_kw(local_minute(instant)) * 5 / 60.0
        entries = []
        for step in range((days + 2) * 48):
            instant = base.now_utc - timedelta(days=days + 2) + timedelta(minutes=30 * step + 7)
            entries.append({"last_updated": instant.strftime("%Y-%m-%dT%H:%M:%S+0000"), "state": str(2.0 if 600 <= local_minute(instant + timedelta(minutes=15)) < 960 else 0.0), "attributes": {}})
        test_api.solar.pv_scaling = 1.0
        base.minute_data_import_export = lambda max_days, now_utc, key, *args, **kwargs: dict(pv_today) if key == "pv_today" else {}
        test_api.solar.get_history_wrapper = lambda entity_id, days, required=False, **kwargs: [entries] if entity_id.endswith("_pv_forecast_h0_uncalibrated") else []
        with test_api.patch_now_utc_exact():
            evidence = test_api.solar.pv_calibration_scan(days)
        before = evidence.get("2025-10-24") if evidence else None
        failed = check("BST day stored", before is not None, evidence and sorted(evidence))
        if before:
            failed |= check("BST day nothing at 09:30 local", before["slots"][570][1] == 0, before["slots"][570])
            failed |= check("BST day generation from 10:00 local", before["slots"][600][1] > 0, before["slots"][600])
    finally:
        test_api.cleanup()
    return failed


def run_pv_calibration_scan_tests(my_predbat=None):
    """Run the PV calibration history scan and settings tests."""
    failed = False
    for test in (
        test_scan_builds_days,
        test_scan_forecast_before_pv_scaling,
        test_scan_pv_scaling_zero_stores_nothing,
        test_scan_skips_partially_covered_day,
        test_scan_tracked_false_passed,
        test_scan_history_missing_returns_none,
        test_settings_defaults_and_overrides,
        test_settings_clamp_and_warn,
        test_calibration_sunny_after_dull_not_cut,
        test_calibration_uses_store_not_history,
        test_calibration_falls_back_to_scan,
        test_calibration_band_from_last_seven_days,
        test_calibration_off_under_three_days,
        test_calibration_peak_from_last_seven_days,
        test_calibration_band_default_when_recent_week_down,
        test_calibration_empty_store_does_not_rescan,
        test_scan_empty_forecast_history_is_a_failed_read,
        test_calibration_band_default_when_recent_week_mostly_down,
        test_scan_clipped_minutes_follow_pv_scaling,
        test_scan_short_history_keeps_whole_days,
        test_scan_skips_day_forecast_starts_after_generation,
        test_scan_generation_on_its_own_minutes,
        test_settings_warn_once,
        test_scan_no_forecast_history_skips_generation_read,
        test_scan_slots_in_local_time_across_clock_change,
    ):
        print("**** {} ****".format(test.__name__))
        failed = test() or failed
    return failed

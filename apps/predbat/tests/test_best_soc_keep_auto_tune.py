# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for Output.tune_best_soc_keep().

tune_best_soc_keep() is an observer (#TBD): it computes a suggested best_soc_keep
from yesterday's real import/export rates, real per-minute import, and the real
minimum battery SoC reached, then publishes the suggestion to a dashboard sensor.
It does NOT write the value back to best_soc_keep - that is left for a later patch
once the correction logic has been dogfooded.

Covered scenarios
------------------
1. Switch off (best_soc_keep_auto_tune=False) is a complete no-op.
2. Once-per-day gate: a same-day best_soc_keep_last_tune skips recomputation.
3. Regret-driven (up) correction: an import at a worse rate than the export rate
   available at the same moment produces a positive correction, matching the
   worked example (1kWh bought back at 32p while 16p export was available is
   16p of regret, not 32p).
4. Margin-driven (down) correction: no regret, but the real minimum SoC sat
   above the current keep floor, produces a negative correction.
5. Clamping to [0, soc_max] in both directions.
6. self.best_soc_keep itself is never mutated - the suggestion is observer-only.
"""

from datetime import datetime, timedelta

import pytz

UTC = pytz.UTC


def _index_for_minute(minutes_now, minute):
    """Backwards-indexed cumulative/history lookup key for a given day-relative
    minute (0 = yesterday midnight), mirroring calculate_yesterday()'s own
    `minutes_back + 24*60 - minute - 5` formula."""
    minutes_back = minutes_now + 1
    return minutes_back + 24 * 60 - minute - 5


def _setup_base(my_predbat, minutes_now=360, alpha=0.1, old_keep=1.0, soc_max=10.0):
    """Minimum predbat state needed for tune_best_soc_keep(), with no import
    events and no battery history yet - individual tests add those."""
    my_predbat.now_utc = datetime(2024, 10, 4, 0, 0, 0, tzinfo=UTC) + timedelta(minutes=minutes_now)
    my_predbat.midnight_utc = datetime(2024, 10, 4, 0, 0, 0, tzinfo=UTC)
    my_predbat.minutes_now = minutes_now

    my_predbat.expose_config("best_soc_keep_auto_tune", True)
    my_predbat.expose_config("best_soc_keep_auto_tune_alpha", alpha)
    my_predbat.best_soc_keep_last_tune = None

    my_predbat.soc_max = soc_max
    my_predbat.best_soc_keep = old_keep
    my_predbat.rate_average = 20.0
    my_predbat.rate_min = 1.0

    my_predbat.rate_import = {}
    my_predbat.rate_export = {}
    my_predbat.import_today = {}
    my_predbat.soc_kwh_history = {}

    prefix = my_predbat.prefix
    entity = prefix + ".best_soc_keep_auto_tune"
    my_predbat.dashboard_values.pop(entity, None)

    return prefix, entity


def _set_import_event(my_predbat, minute, kwh):
    """Make the reconstructed per-minute import at day-relative *minute* equal
    *kwh*, via a single one-step jump in the backwards-indexed cumulative
    import_today dict (all other minutes default to a 0 delta)."""
    index_after = _index_for_minute(my_predbat.minutes_now, minute + 1)
    my_predbat.import_today[index_after] = kwh


def _set_rates(my_predbat, minute, import_rate, export_rate):
    """Set the real (already-elapsed) import/export rate at day-relative *minute*,
    via the negative offset history_to_future_rates() reads from."""
    my_predbat.rate_import[minute - 24 * 60] = import_rate
    my_predbat.rate_export[minute - 24 * 60] = export_rate


def _set_soc_min(my_predbat, minute, soc_kwh):
    """Set the single lowest recorded battery SoC yesterday to *soc_kwh*, at
    day-relative *minute*."""
    index = _index_for_minute(my_predbat.minutes_now, minute)
    my_predbat.soc_kwh_history[index] = soc_kwh


def _test_switch_off(my_predbat, failed):
    print("tune_best_soc_keep: Test 1 - switch off is a no-op")
    prefix, entity = _setup_base(my_predbat)
    my_predbat.expose_config("best_soc_keep_auto_tune", False)
    _set_soc_min(my_predbat, 700, 5.0)

    my_predbat.tune_best_soc_keep()

    if my_predbat.best_soc_keep_last_tune is not None:
        print("ERROR: best_soc_keep_last_tune was set despite the switch being off")
        failed = True
    if entity in my_predbat.dashboard_values:
        print("ERROR: dashboard entity was published despite the switch being off")
        failed = True

    return failed


def _test_once_per_day_gate(my_predbat, failed):
    print("tune_best_soc_keep: Test 2 - once-per-day gate skips recomputation")
    prefix, entity = _setup_base(my_predbat)
    _set_soc_min(my_predbat, 700, 5.0)

    already_tuned_today = my_predbat.now_utc - timedelta(minutes=10)
    my_predbat.best_soc_keep_last_tune = already_tuned_today

    my_predbat.tune_best_soc_keep()

    if my_predbat.best_soc_keep_last_tune != already_tuned_today:
        print("ERROR: best_soc_keep_last_tune was changed despite already having run today")
        failed = True
    if entity in my_predbat.dashboard_values:
        print("ERROR: dashboard entity was published despite the once-per-day gate")
        failed = True

    return failed


def _test_regret_correction(my_predbat, failed):
    print("tune_best_soc_keep: Test 3 - regret-driven (up) correction matches the worked example")
    prefix, entity = _setup_base(my_predbat, alpha=0.1, old_keep=1.0, soc_max=10.0)
    # Baseline SoC history so the function doesn't bail out for lack of battery data;
    # kept well above old_keep so it plays no part once the regret branch is taken.
    _set_soc_min(my_predbat, 700, 9.0)

    # 1kWh bought back at 32p while 16p export was available => 16p of regret.
    _set_import_event(my_predbat, 100, 1.0)
    _set_rates(my_predbat, 100, import_rate=32.0, export_rate=16.0)

    my_predbat.tune_best_soc_keep()

    expected_regret = 16.0
    expected_shortfall = expected_regret / my_predbat.rate_average  # 0.8
    expected_suggestion = round(1.0 + 0.1 * expected_shortfall, 2)  # 1.08

    if entity not in my_predbat.dashboard_values:
        print("ERROR: dashboard entity was not published")
        failed = True
    else:
        state = my_predbat.dashboard_values[entity].get("state")
        attrs = my_predbat.dashboard_values[entity].get("attributes", {})
        if abs(state - expected_suggestion) > 1e-6:
            print("ERROR: suggested keep should be {}, got {}".format(expected_suggestion, state))
            failed = True
        if abs(attrs.get("regret", -1) - expected_regret) > 1e-6:
            print("ERROR: regret attribute should be {}, got {}".format(expected_regret, attrs.get("regret")))
            failed = True
        if abs(attrs.get("shortfall_kwh", -1) - expected_shortfall) > 1e-6:
            print("ERROR: shortfall_kwh attribute should be {}, got {}".format(expected_shortfall, attrs.get("shortfall_kwh")))
            failed = True
        if attrs.get("applied", None) is not False:
            print("ERROR: applied attribute should be False, got {}".format(attrs.get("applied")))
            failed = True

    if my_predbat.best_soc_keep != 1.0:
        print("ERROR: best_soc_keep was mutated (expected unchanged 1.0, got {})".format(my_predbat.best_soc_keep))
        failed = True

    return failed


def _test_margin_correction(my_predbat, failed):
    print("tune_best_soc_keep: Test 4 - margin-driven (down) correction when there is no regret")
    prefix, entity = _setup_base(my_predbat, alpha=0.1, old_keep=1.0, soc_max=10.0)
    # A benign rate point just to make rate_import/rate_export non-empty (the function bails
    # out early with no rate history at all); no import event means shortfall_kwh stays 0,
    # so the margin branch is used regardless of the rate values themselves.
    _set_rates(my_predbat, 700, import_rate=20.0, export_rate=5.0)
    _set_soc_min(my_predbat, 700, 3.0)

    my_predbat.tune_best_soc_keep()

    expected_margin = 2.0  # soc_min_yesterday(3.0) - old_keep(1.0)
    expected_suggestion = round(1.0 + 0.1 * (-expected_margin), 2)  # 0.8

    if entity not in my_predbat.dashboard_values:
        print("ERROR: dashboard entity was not published")
        failed = True
    else:
        state = my_predbat.dashboard_values[entity].get("state")
        attrs = my_predbat.dashboard_values[entity].get("attributes", {})
        if abs(state - expected_suggestion) > 1e-6:
            print("ERROR: suggested keep should be {}, got {}".format(expected_suggestion, state))
            failed = True
        if abs(attrs.get("margin_kwh", -1) - expected_margin) > 1e-6:
            print("ERROR: margin_kwh attribute should be {}, got {}".format(expected_margin, attrs.get("margin_kwh")))
            failed = True
        if abs(attrs.get("regret", -1)) > 1e-6:
            print("ERROR: regret attribute should be 0 when no import happened, got {}".format(attrs.get("regret")))
            failed = True

    if my_predbat.best_soc_keep != 1.0:
        print("ERROR: best_soc_keep was mutated (expected unchanged 1.0, got {})".format(my_predbat.best_soc_keep))
        failed = True

    return failed


def _test_clamping(my_predbat, failed):
    print("tune_best_soc_keep: Test 5 - suggestion is clamped to [0, soc_max]")

    # Upper clamp: a large regret should not push the suggestion above soc_max.
    prefix, entity = _setup_base(my_predbat, alpha=1.0, old_keep=9.5, soc_max=10.0)
    _set_soc_min(my_predbat, 700, 9.5)
    _set_import_event(my_predbat, 100, 10.0)
    _set_rates(my_predbat, 100, import_rate=100.0, export_rate=0.0)

    my_predbat.tune_best_soc_keep()

    state = my_predbat.dashboard_values.get(entity, {}).get("state")
    if state is None or state > 10.0 + 1e-9:
        print("ERROR: suggested keep should be clamped to soc_max (10.0), got {}".format(state))
        failed = True

    # Lower clamp: a large margin should not push the suggestion below 0.
    prefix, entity = _setup_base(my_predbat, alpha=1.0, old_keep=0.5, soc_max=10.0)
    _set_rates(my_predbat, 700, import_rate=20.0, export_rate=5.0)
    _set_soc_min(my_predbat, 700, 50.0)

    my_predbat.tune_best_soc_keep()

    state = my_predbat.dashboard_values.get(entity, {}).get("state")
    if state is None or state < -1e-9:
        print("ERROR: suggested keep should be clamped to 0, got {}".format(state))
        failed = True

    return failed


def _test_no_battery_history_skips(my_predbat, failed):
    print("tune_best_soc_keep: Test 6 - no battery SoC history available skips the update")
    prefix, entity = _setup_base(my_predbat)
    # A benign rate point so the function gets past the rate-history check and actually
    # reaches the battery-history check this test targets.
    _set_rates(my_predbat, 700, import_rate=20.0, export_rate=5.0)
    # soc_kwh_history left empty, and get_history_wrapper (HA fallback) also returns nothing.
    my_predbat.get_history_wrapper = lambda *a, **kw: None

    my_predbat.tune_best_soc_keep()

    if entity in my_predbat.dashboard_values:
        print("ERROR: dashboard entity was published despite no battery SoC history being available")
        failed = True
    if my_predbat.best_soc_keep_last_tune is None:
        print("ERROR: best_soc_keep_last_tune should still be set to avoid retrying every cycle")
        failed = True

    if hasattr(my_predbat.__class__, "get_history_wrapper"):
        try:
            del my_predbat.get_history_wrapper
        except AttributeError:
            pass

    return failed


def test_best_soc_keep_auto_tune(my_predbat):
    """
    Unit tests for tune_best_soc_keep(): switch gating, once-per-day gate,
    the regret (up) and margin (down) correction branches, clamping, and the
    observer-only guarantee that best_soc_keep itself is never mutated.
    """
    failed = False
    print("**** Running tune_best_soc_keep tests ****")

    # best_soc_keep_auto_tune (and its alpha) are gated on expert_mode - enable it so
    # expose_config()/get_arg() don't just null the values back out.
    original_expert_mode = my_predbat.config_index["expert_mode"].get("value")
    my_predbat.expose_config("expert_mode", True, force_ha=True)

    try:
        failed = _test_switch_off(my_predbat, failed)
        failed = _test_once_per_day_gate(my_predbat, failed)
        failed = _test_regret_correction(my_predbat, failed)
        failed = _test_margin_correction(my_predbat, failed)
        failed = _test_clamping(my_predbat, failed)
        failed = _test_no_battery_history_skips(my_predbat, failed)
    finally:
        my_predbat.expose_config("expert_mode", original_expert_mode, force_ha=True)

    return failed

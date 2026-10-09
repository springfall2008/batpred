# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for set_rate_thresholds and rate_minmax_excluding_saving (GH#5050).

A saving session / Axle VPP event boosts both the import and export rate tables by the event
reward and tags those minutes "saving" in rate_import_replicated/rate_export_replicated
(load_saving_slot() in octopus.py, load_axle_slot() in axle.py). In automatic threshold mode
(rate_low_threshold=0) set_rate_thresholds() used to compute rate_import_cost_threshold from
self.rate_max, which includes those event-boosted minutes - so a two-rate tariff (25.95p day,
3.49p night) plus a +100p event pushed the threshold to 125.45p, and rate_scan_window()
classified the whole ordinary-price day as "low rate" (binary_sensor.predbat_low_rate_slot stuck
ON for ~24h, the reported symptom).

set_rate_thresholds() now derives its threshold stats from rate_minmax_excluding_saving(), which
maps each minute in rate_import_saving_minutes/rate_export_saving_minutes - a frozen snapshot of
"saving"-tagged minutes taken right after the saving/free/Axle loaders run and before any override
(rates_import_override, manual rates) can overwrite that same minute's rate_import_replicated/
rate_export_replicated tag with its own "increment"/"user" tag - back to its own pre-event base
rate (rate_import_pre_saving/rate_export_pre_saving: the tariff snapshotted after the IOG dispatch
overlay and before the session loaders) rather than excluding it outright. self.rate_max/rate_min/rate_average (and the export equivalents) are deliberately left
untouched - they feed dashboard sensors, graph scaling, and plan.py pricing, where the real boosted
price is what should be shown.

Ahead of a saving session or Axle event priced above the tariff's highest import rate, find_low_rate_windows()
also gives the plan the import windows before the event, so it can still charge to export into it
(#249). The low rate sensors keep the tariff's own cheap windows (low_rates_tariff).
"""

from datetime import timedelta
from axle import load_axle_slot


def _setup_two_rate_tariff(my_predbat, event_start, event_end, event_boost=100.0):
    """Build a 48h two-rate import tariff (25.95p day 06:00-22:00, 3.49p night) plus a flat 15.0p
    export tariff, with a saving-session-style boost applied to import over [event_start, event_end)
    and tagged "saving" in rate_import_replicated - the shape load_saving_slot()/load_axle_slot()
    produce."""
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60

    rate_import_base = {}
    for minute in range(0, 48 * 60):
        hour = (minute // 60) % 24
        rate_import_base[minute] = 25.95 if 6 <= hour < 22 else 3.49
    rate_import = rate_import_base.copy()

    rate_import_replicated = {}
    for minute in range(event_start, event_end):
        rate_import[minute] += event_boost
        rate_import_replicated[minute] = "saving"
    rate_import_saving_minutes = set(range(event_start, event_end))

    rate_export = {minute: 15.0 for minute in range(0, 48 * 60)}

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_base = rate_import_base
    my_predbat.rate_import_replicated = rate_import_replicated
    my_predbat.rate_import_saving_minutes = rate_import_saving_minutes
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_base = rate_export.copy()
    my_predbat.rate_export_replicated = {}
    my_predbat.rate_export_saving_minutes = set()
    # The pre-saving snapshots set_rate_thresholds() reads: on the import side fetch_sensor_data()
    # takes this after the IOG overlay, which this synthetic tariff has none of, so it equals the
    # base curve here. Export has no IOG overlay, so fetch copies rate_export_base - copied here
    # too, so the test cannot pass on an aliasing implementation the production code does not use.
    my_predbat.rate_import_pre_saving = rate_import_base.copy()
    my_predbat.rate_export_pre_saving = my_predbat.rate_export_base.copy()

    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)

    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    return rate_import, rate_import_saving_minutes, rate_import_base


def test_rate_minmax_excluding_saving_maps_boosted_minutes_to_base(my_predbat):
    """rate_minmax_excluding_saving must report the tariff's own min/max/average, not the
    event-inflated figures - reproducing the exact numbers from the GH#5050 triage."""
    print("**** test_rate_minmax_excluding_saving_maps_boosted_minutes_to_base ****")
    failed = False

    rate_import, rate_import_saving_minutes, rate_import_base = _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60)

    if my_predbat.rate_max != 125.95:
        print("ERROR: test setup sanity check failed - contaminated rate_max should be 125.95, got {}".format(my_predbat.rate_max))
        failed = True

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes, rate_import_base)
    if rate_min != 3.49:
        print("ERROR: saving-excluded rate_min should be 3.49, got {}".format(rate_min))
        failed = True
    if rate_max != 25.95:
        print("ERROR: saving-excluded rate_max should be 25.95 (the true day rate), got {}".format(rate_max))
        failed = True
    if abs(rate_average - 18.46) > 0.01:
        print("ERROR: saving-excluded rate_average should be ~18.46, got {}".format(rate_average))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_rate_minmax_excluding_saving_keeps_genuine_free_slots(my_predbat):
    """A "saving"-tagged minute that is CHEAPER than the tariff, not more expensive, must still
    count towards rate_min - the "saving" tag also marks free/discounted Octopus sessions
    (load_free_slot()) and Axle import discounts, not only event boosts.

    With a flat 20p tariff and one free (0p) slot tagged "saving", mapping the tagged minute back
    to its own base rate (20p, unchanged from the flat tariff here since there was no boost) must
    not accidentally raise it back up - the base-rate mapping only ever pulls a minute DOWN to
    its pre-event price, never up, so a genuine discount stays visible.
    """
    print("**** Testing rate_minmax_excluding_saving keeps a genuine free slot ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import_base = {minute: 20.0 for minute in range(0, 24 * 60)}
    rate_import = rate_import_base.copy()
    rate_import[100] = 0.0
    rate_import_saving_minutes = {100}

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes, rate_import_base)

    if rate_min != 0.0:
        print("ERROR: the free slot's 0.0p should still set rate_min, got {}".format(rate_min))
        failed = True
    if rate_max != 20.0:
        print("ERROR: rate_max should be the flat tariff rate 20.0, got {}".format(rate_max))
        failed = True
    if rate_max == rate_min:
        print("ERROR: rate_max == rate_min - the free slot was excluded along with the rest of the flat tariff, which would push the automatic threshold above every rate")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_rate_minmax_excluding_saving_falls_back_without_a_base_curve(my_predbat):
    """A caller with no rate_base available (compare.py, annual.py - neither builds one) must fall
    back to the plain min/max/average rather than raising or silently ignoring every rate."""
    print("**** test_rate_minmax_excluding_saving_falls_back_without_a_base_curve ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import = {minute: (25.95 if 6 <= (minute // 60) % 24 < 22 else 3.49) for minute in range(0, 24 * 60)}

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, set(), {})
    expected_min, expected_max, expected_average, _, _ = my_predbat.rate_minmax(rate_import)
    if (rate_min, rate_max, rate_average) != (expected_min, expected_max, expected_average):
        print("ERROR: no-base-curve call should fall back to the plain scan {}, got {}".format((expected_min, expected_max, expected_average), (rate_min, rate_max, rate_average)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_rate_minmax_excluding_saving_skips_session_minute_with_no_tariff_rate(my_predbat):
    """A session minute the tariff has no rate for (load_axle_slot() builds it from rate_dict.get(minute, 0))
    has no pre-event rate to cap to, so it must not count its synthetic reward as a tariff rate."""
    print("**** test_rate_minmax_excluding_saving_skips_session_minute_with_no_tariff_rate ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_base = {minute: 20.0 for minute in range(0, 24 * 60) if minute != 600}
    rates = rate_base.copy()
    rates[600] = 100.0
    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rates, {600}, rate_base)
    if (rate_min, rate_max, rate_average) != (20.0, 20.0, 20.0):
        print("ERROR: a session minute missing from the base tariff should be ignored, expected (20.0, 20.0, 20.0), got {}".format((rate_min, rate_max, rate_average)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode(my_predbat):
    """End-to-end: with rate_low_threshold=0 (automatic mode), a saving-session boost must not
    raise the low-rate threshold above the tariff's own day rate - the exact mechanism reported in
    GH#5050 (binary_sensor.predbat_low_rate_slot stuck ON through the day rate for ~24h)."""
    print("**** test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode ****")
    failed = False

    _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60)

    my_predbat.set_rate_thresholds()

    # rate_max - 0.5 on the true day rate, not the event-boosted one: 25.95 - 0.5 = 25.45.
    # The unfixed code produced rate_max(125.95) - 0.5 = 125.45, which classified the whole day as low.
    expected_threshold = 25.45
    if abs(my_predbat.rate_import_cost_threshold - expected_threshold) > 0.01:
        print("ERROR: rate_import_cost_threshold should be {} (true day rate - 0.5), got {} (GH#5050)".format(expected_threshold, my_predbat.rate_import_cost_threshold))
        failed = True

    found, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)
    window_averages = sorted(set(window["average"] for window in found))
    if 25.95 in window_averages:
        print("ERROR: the 25.95p day rate was classified as a low-rate window - binary_sensor.predbat_low_rate_slot would misfire (GH#5050)".format())
        failed = True
    if window_averages != [3.49]:
        print("ERROR: expected only the genuine 3.49p night rate to qualify as low-rate, got window averages {}".format(window_averages))
        failed = True

    # rate_max/rate_min/rate_average themselves are deliberately left contaminated - other code
    # (dashboard sensors, graph scaling, plan.py pricing) wants the real boosted price.
    if my_predbat.rate_max != 125.95:
        print("ERROR: rate_max should be left untouched at 125.95 for downstream consumers, got {}".format(my_predbat.rate_max))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_set_rate_thresholds_ignores_small_saving_boost_in_manual_import_mode(my_predbat):
    """Manual import threshold mode must also ignore a saving reward added to a cheap slot.

    +5p on a 3.49p night slot (8.49p total) stays below the 25.95p day-rate max, so a
    global-max-only filter would leave it in rate_average and inflate the manual threshold.
    """
    print("**** test_set_rate_thresholds_ignores_small_saving_boost_in_manual_import_mode ****")
    failed = False

    _setup_two_rate_tariff(my_predbat, event_start=60, event_end=180, event_boost=5.0)
    my_predbat.rate_low_threshold = 1.0

    my_predbat.set_rate_thresholds()

    expected_threshold = 18.46
    if abs(my_predbat.rate_import_cost_threshold - expected_threshold) > 0.01:
        print("ERROR: rate_import_cost_threshold should be {} (clean import average x 1.0), got {} - small boosted saving minutes contaminated manual mode".format(expected_threshold, my_predbat.rate_import_cost_threshold))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_set_rate_thresholds_ignores_export_saving_boost_in_manual_mode(my_predbat):
    """The export side and manual-threshold mode (rate_high_threshold > 0) were both untested -
    every prior test here used rate_import_replicated only and left rate_high_threshold at 0
    (automatic mode). This branch (fetch.py's "rate_export_cost_threshold = dp2(rate_export_average
    * self.rate_high_threshold)") multiplies by rate_export_average directly, so a boosted export
    minute contaminating that average would not be masked by the automatic-mode rate_export_max/
    rate_export_min comparisons the other tests already cover.

    Flat 15p export tariff, +50p Axle export-side event over 2h out of 24 (rate_export_replicated
    tagged "saving", mirroring load_axle_slot()'s export branch): the contaminated average is
    ~19.17p, the clean one is 15.0p exactly. rate_high_threshold=1.0 makes the threshold equal
    whichever average was used, so the two are trivially distinguishable.
    """
    print("**** test_set_rate_thresholds_ignores_export_saving_boost_in_manual_mode ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60

    rate_import = {minute: 20.0 for minute in range(0, 48 * 60)}
    rate_export_base = {minute: 15.0 for minute in range(0, 48 * 60)}
    rate_export = rate_export_base.copy()
    rate_export_replicated = {}
    for minute in range(600, 720):
        rate_export[minute] += 50.0
        rate_export_replicated[minute] = "saving"
    rate_export_saving_minutes = set(range(600, 720))

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_base = rate_import.copy()
    my_predbat.rate_import_replicated = {}
    my_predbat.rate_import_saving_minutes = set()
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_base = rate_export_base
    my_predbat.rate_export_replicated = rate_export_replicated
    my_predbat.rate_export_saving_minutes = rate_export_saving_minutes
    # Set the snapshots set_rate_thresholds() actually reads, so this cannot pass on whatever an
    # earlier test left on the shared fixture
    my_predbat.rate_import_pre_saving = rate_import.copy()
    my_predbat.rate_export_pre_saving = rate_export_base.copy()
    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)
    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 1.0  # manual mode: threshold = rate_export_average * rate_high_threshold
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    my_predbat.set_rate_thresholds()

    if abs(my_predbat.rate_export_average - 19.17) > 0.01:
        print("ERROR: test setup sanity check failed - contaminated rate_export_average should be ~19.17, got {}".format(my_predbat.rate_export_average))
        failed = True

    expected_threshold = 15.0
    if abs(my_predbat.rate_export_cost_threshold - expected_threshold) > 0.01:
        print("ERROR: rate_export_cost_threshold should be {} (clean export average x 1.0), got {} - a boosted export event contaminated the manual-mode threshold".format(expected_threshold, my_predbat.rate_export_cost_threshold))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_rate_minmax_excluding_saving_ignores_overwritten_replicate_tag(my_predbat):
    """A saving-boosted minute must stay mapped to its base rate even after something downstream
    overwrites its rate_replicate tag - a documented, supported combination (an export
    rate_increment override active during the same saving session) rewrites
    rate_replicate[minute] from "saving" to "increment"/"user" (fetch.py's basic_rates()), which
    happens for real in the production fetch path: load_saving_slot()/load_free_slot()/
    load_axle_slot() tag rate_replicate, then basic_rates()/apply_manual_rates() run afterward and
    can overwrite the tag on the very same minute.

    rate_minmax_excluding_saving() must not be fooled by that - it takes the frozen
    rate_import_saving_minutes/rate_export_saving_minutes set (captured before any override can
    run), not the live, possibly-overwritten rate_replicate dict, so this reproduces the
    overwrite directly rather than relying on the full fetch pipeline.
    """
    print("**** test_rate_minmax_excluding_saving_ignores_overwritten_replicate_tag ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import_base = {minute: 20.0 for minute in range(0, 24 * 60)}
    rate_import = rate_import_base.copy()
    rate_import[100] += 50.0  # a saving-session reward, same shape as load_saving_slot()
    rate_import_saving_minutes = {100}
    # Simulate basic_rates()'s rate_increment branch overwriting the same minute's tag afterward -
    # the live rate_replicate dict no longer says "saving" for minute 100 at all.
    rate_import_replicated = {100: "increment"}

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes, rate_import_base)

    if rate_max != 20.0:
        print("ERROR: the boosted minute should still be mapped back to its base rate even though its rate_replicate tag was overwritten, got rate_max {}".format(rate_max))
        failed = True
    if rate_min != 20.0:
        print("ERROR: rate_min should be the flat tariff rate, got {}".format(rate_min))
        failed = True

    # The overwritten dict is passed through unused here (rate_minmax_excluding_saving takes the
    # saving_minutes set, not rate_replicate) - assert on it anyway so the test documents exactly
    # what would have fooled a live-dict-based check.
    if rate_import_replicated.get(100) == "saving":
        print("ERROR: test setup sanity check failed - the simulated overwrite did not actually overwrite the tag")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_saving_minute_capped_against_post_io_rates(my_predbat):
    """A saving minute overlapping an IOG dispatch slot must cap to the discounted price, not the tariff.

    rate_import_base is built before rate_add_io_slots(), so capping against it would discard a
    legitimate IOG discount wherever a session and a dispatch slot overlap, and the automatic
    threshold could then miss that genuinely cheap slot. set_rate_thresholds() reads
    rate_import_pre_saving, snapshotted after the IOG overlay instead.
    """
    print("**** test_saving_minute_capped_against_post_io_rates ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60

    flat = 30.0
    io_rate = 7.0
    io_start, io_end = 2 * 60, 4 * 60

    rate_import_base = {minute: flat for minute in range(0, 48 * 60)}
    # After the IOG overlay the dispatch window is cheap
    rate_import_pre_saving = rate_import_base.copy()
    for minute in range(io_start, io_end):
        rate_import_pre_saving[minute] = io_rate
    # A saving event then boosts the same minutes
    rate_import = rate_import_pre_saving.copy()
    rate_import_replicated = {}
    for minute in range(io_start, io_end):
        rate_import[minute] += 100.0
        rate_import_replicated[minute] = "saving"

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_base = rate_import_base
    my_predbat.rate_import_pre_saving = rate_import_pre_saving
    my_predbat.rate_import_replicated = rate_import_replicated
    my_predbat.rate_import_saving_minutes = set(range(io_start, io_end))

    rate_export = {minute: 15.0 for minute in range(0, 48 * 60)}
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_base = rate_export.copy()
    my_predbat.rate_export_pre_saving = my_predbat.rate_export_base.copy()
    my_predbat.rate_export_replicated = {}
    my_predbat.rate_export_saving_minutes = set()

    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)
    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    # Drive set_rate_thresholds() itself, so this fails if it ever reads rate_import_base again.
    # With the IOG discount preserved the tariff still varies (7p vs 30p), so automatic mode takes
    # the "everything but the most expensive" branch and the threshold lands below the flat rate.
    # Capping against the pre-IOG curve instead flattens every minute to 30p, rate_max == rate_min,
    # and the threshold jumps ABOVE the tariff - the #5050 failure, now via the IOG path.
    my_predbat.set_rate_thresholds()

    if my_predbat.rate_import_cost_threshold >= flat:
        print("ERROR: threshold {} is at or above the flat tariff rate {} - the IOG discount was lost to the saving cap, so the whole day reads as low rate".format(my_predbat.rate_import_cost_threshold, flat))
        failed = True

    rate_min, rate_max, _ = my_predbat.rate_minmax_excluding_saving(rate_import, my_predbat.rate_import_saving_minutes, rate_import_pre_saving)

    if rate_min != io_rate:
        print("ERROR: the IOG discount should survive the saving cap - expected min {}, got {}".format(io_rate, rate_min))
        failed = True
    if rate_max != flat:
        print("ERROR: saving-excluded max should be the flat tariff rate {}, got {}".format(flat, rate_max))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_saving_minute_keeps_user_override(my_predbat):
    """An override on a session minute must reach the threshold stats, not be capped back to the tariff.

    apply_rate_overrides(), which fetch_sensor_data() runs the overrides through, gives
    rate_import_pre_saving the same overrides as rate_import, so the cap takes a session minute back to
    "this rate without the session" rather than to the bare tariff. Before, a fixed 50p the user put
    over a +50p session to discourage charging read as the 25.95p day rate, and a +5p increment on top
    of the event was dropped along with the event.
    """
    print("**** test_saving_minute_keeps_user_override ****")
    failed = False

    session_window = [{"start": "17:00:00", "end": "19:00:00"}]
    cases = [
        # (label, override items, manual rates, expected average). The window is 14h of 25.95p day
        # rate, 8h of 3.49p night rate and the 2h session; a snapshot without the overrides gave 18.46.
        ("fixed 50p override", [dict(session_window[0], rate=50.0)], {}, (14 * 25.95 + 2 * 50.0 + 8 * 3.49) / 24),
        ("+5p increment override", [dict(session_window[0], rate_increment=5.0)], {}, (14 * 25.95 + 2 * 30.95 + 8 * 3.49) / 24),
        ("40p manual rate", [], {minute: 40.0 for minute in range(17 * 60, 19 * 60)}, (14 * 25.95 + 2 * 40.0 + 8 * 3.49) / 24),
    ]
    for label, override, manual, expected in cases:
        _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60, event_boost=50.0)
        my_predbat.rate_import, my_predbat.rate_import_pre_saving = my_predbat.apply_rate_overrides(
            my_predbat.rate_import, my_predbat.rate_import_pre_saving, my_predbat.rate_import_saving_minutes, override, "rates_import_override", manual, True, my_predbat.rate_import_replicated
        )
        my_predbat.rate_low_threshold = 1.0

        my_predbat.set_rate_thresholds()

        if abs(my_predbat.rate_import_cost_threshold - expected) > 0.01:
            print("ERROR: {}: rate_import_cost_threshold should be {:.2f} (average with the override kept), got {}".format(label, expected, my_predbat.rate_import_cost_threshold))
            failed = True

    # With no session minutes nothing reads the snapshot, so none of it is kept
    _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60, event_boost=50.0)
    pre_saving = my_predbat.rate_import_pre_saving
    _, returned = my_predbat.apply_rate_overrides(my_predbat.rate_import, pre_saving, set(), cases[0][1], "rates_import_override", {}, True, my_predbat.rate_import_replicated)
    if returned != {} or pre_saving[17 * 60] != 25.95:
        print("ERROR: with no saving minutes nothing of the snapshot should be kept, nor the caller's snapshot changed, got {} minutes".format(len(returned)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def _setup_export_event(my_predbat, export_boost=50.0):
    """Flat 15p export tariff with a boosted, "saving"-tagged export event 10:00-12:00, and a two-rate import tariff (20p night, 30p day)."""
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60

    rate_import = {minute: (30.0 if 6 <= (minute // 60) % 24 < 22 else 20.0) for minute in range(0, 48 * 60)}
    rate_export_base = {minute: 15.0 for minute in range(0, 48 * 60)}
    rate_export = rate_export_base.copy()
    for minute in range(600, 720):
        rate_export[minute] += export_boost

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_base = rate_import.copy()
    my_predbat.rate_import_pre_saving = rate_import.copy()
    my_predbat.rate_import_replicated = {}
    my_predbat.rate_import_saving_minutes = set()
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_base = rate_export_base
    my_predbat.rate_export_pre_saving = rate_export_base.copy()
    my_predbat.rate_export_replicated = {minute: "saving" for minute in range(600, 720)}
    my_predbat.rate_export_saving_minutes = set(range(600, 720))
    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)
    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0


def test_set_rate_thresholds_ignores_export_saving_boost_in_automatic_mode(my_predbat):
    """Automatic export mode (rate_high_threshold=0) must derive its threshold from the underlying export tariff.

    A +50p event on a flat 15p export tariff gives a boosted export max of 65p. On the unfiltered
    stats that changes both automatic branches: export max != min, so export takes min + 0.5 (15.5)
    instead of the flat-tariff min - 0.1 (14.9); and 65p > the 30p import max takes the "export beats
    import" branch, so import takes max + 0.1 (30.1) instead of max - 0.5 (29.5). The clean stats
    must give 14.9 and 29.5.
    """
    print("**** test_set_rate_thresholds_ignores_export_saving_boost_in_automatic_mode ****")
    failed = False

    _setup_export_event(my_predbat)
    if my_predbat.rate_export_max != 65.0:
        print("ERROR: test setup sanity check failed - contaminated rate_export_max should be 65.0, got {}".format(my_predbat.rate_export_max))
        failed = True

    my_predbat.set_rate_thresholds()

    if abs(my_predbat.rate_export_cost_threshold - 14.9) > 0.01:
        print("ERROR: automatic export threshold should be 14.9 (flat clean export tariff: min - 0.1), got {} - the export boost leaked into the automatic export branch".format(my_predbat.rate_export_cost_threshold))
        failed = True
    if abs(my_predbat.rate_import_cost_threshold - 29.5) > 0.01:
        print("ERROR: import threshold should be 29.5 (clean import max 30 - 0.5), got {} - the export boost leaked into the export-beats-import branch".format(my_predbat.rate_import_cost_threshold))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_plan_charges_ahead_of_a_qualifying_export_event(my_predbat):
    """The plan must be offered import windows before an export event that beats the tariff's highest import price (#249).

    _setup_export_event(): 20p night / 30p day import, flat 15p export with a +50p event at 10:00-12:00,
    so the event pays 65p against a 30p import maximum. find_low_rate_windows() must give the plan the
    30p day-rate window that ends at the event, and only the tariff's cheap windows after it. The low
    rate sensors' windows must stay at the 20p night rate (GH#5050).
    """
    print("**** test_plan_charges_ahead_of_a_qualifying_export_event ****")
    failed = False

    _setup_export_event(my_predbat)
    my_predbat.set_rate_thresholds()
    if my_predbat.rate_import_pre_event_end != 600 or my_predbat.rate_import_pre_event_threshold != 30.1:
        print("ERROR: the 10:00 event paying 65p should qualify, with every tariff slot (up to 30.1p) offered before it, got end {} threshold {}".format(my_predbat.rate_import_pre_event_end, my_predbat.rate_import_pre_event_threshold))
        failed = True

    my_predbat.find_low_rate_windows()
    pre_event = [window for window in my_predbat.low_rates if window["end"] <= 600]
    after_event = [window for window in my_predbat.low_rates if window["end"] > 600]
    if not any(window["average"] == 30.0 for window in pre_event):
        print("ERROR: the 30p day rate before the event should be a plan charge window, got {}".format([(w["start"], w["end"], w["average"]) for w in pre_event]))
        failed = True
    if any(window["average"] != 20.0 for window in after_event):
        print("ERROR: after the event only the tariff's 20p night windows should be offered, got {}".format([(w["start"], w["end"], w["average"]) for w in after_event]))
        failed = True
    if sorted(set(window["average"] for window in my_predbat.low_rates_tariff)) != [20.0]:
        print("ERROR: the low rate sensors' windows should hold only the 20p night rate, got {}".format(sorted(set(window["average"] for window in my_predbat.low_rates_tariff))))
        failed = True
    starts = [window["start"] for window in my_predbat.low_rates]
    if starts != sorted(starts) or any(my_predbat.low_rates[n]["end"] > my_predbat.low_rates[n + 1]["start"] for n in range(len(starts) - 1)):
        print("ERROR: plan windows should be in time order and not overlap, got {}".format([(w["start"], w["end"]) for w in my_predbat.low_rates]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def _setup_same_day_session(my_predbat, export_rate):
    """A same-day session: 7p/30p import (00:00-06:00 night), a +300p saving session at 17:30-18:30, time 10:00.

    load_saving_slot() only adds the session to export when the export tariff pays (fetch_sensor_data()
    checks rate_export_max > 0), so with export_rate 0 the session is on import alone.
    """
    my_predbat.minutes_now = 10 * 60
    my_predbat.forecast_minutes = 24 * 60
    session = set(range(17 * 60 + 30, 18 * 60 + 30))
    rate_import_base = {minute: (7.0 if (minute // 60) % 24 < 6 else 30.0) for minute in range(0, 48 * 60)}
    rate_export_base = {minute: export_rate for minute in range(0, 48 * 60)}
    export_session = session if export_rate > 0 else set()
    rate_import = {minute: rate + (300.0 if minute in session else 0.0) for minute, rate in rate_import_base.items()}
    rate_export = {minute: rate + (300.0 if minute in export_session else 0.0) for minute, rate in rate_export_base.items()}

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_base = rate_import_base
    my_predbat.rate_import_pre_saving = rate_import_base.copy()
    my_predbat.rate_import_replicated = {minute: "saving" for minute in session}
    my_predbat.rate_import_saving_minutes = set(session)
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_base = rate_export_base
    my_predbat.rate_export_pre_saving = rate_export_base.copy()
    my_predbat.rate_export_replicated = {minute: "saving" for minute in export_session}
    my_predbat.rate_export_saving_minutes = set(export_session)
    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)
    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0


def _check_same_day_pre_charge(my_predbat, label):
    """Run the thresholds and window scan, and check the plan gets windows before the 17:30 session and the sensors do not."""
    failed = False
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    before = [window for window in my_predbat.low_rates if window["end"] <= 17 * 60 + 30]
    after_day = [window for window in my_predbat.low_rates if window["start"] >= 18 * 60 + 30 and window["average"] >= 30.0]
    if not before:
        print("ERROR: {}: no charge window before the 17:30 session - the pre-charge is lost, got {}".format(label, [(w["start"], w["end"], w["average"]) for w in my_predbat.low_rates]))
        failed = True
    if after_day:
        print("ERROR: {}: day-rate windows after the session should not be offered, got {}".format(label, [(w["start"], w["end"]) for w in after_day]))
        failed = True
    if any(window["average"] >= 30.0 for window in my_predbat.low_rates_tariff):
        print("ERROR: {}: the low rate sensors should not show the 30p day rate, got {}".format(label, sorted(set(w["average"] for w in my_predbat.low_rates_tariff))))
        failed = True
    # The automatic tightening follows the tariff's own windows, not the pre-event day rate
    if my_predbat.rate_import_cost_threshold != 7.0:
        print("ERROR: {}: the tightened import threshold should be the tariff's 7p, got {} - the pre-event day rate leaked into it".format(label, my_predbat.rate_import_cost_threshold))
        failed = True
    return failed


def test_pre_charge_for_a_same_day_saving_session(my_predbat):
    """A session announced after the night rate has passed must still get a pre-charge (#249).

    15p export, so the session raises both sides. The plan must have charge windows before the session,
    and none at the day rate after it.
    """
    print("**** test_pre_charge_for_a_same_day_saving_session ****")
    _setup_same_day_session(my_predbat, export_rate=15.0)
    failed = _check_same_day_pre_charge(my_predbat, "15p export")
    if not failed:
        print("PASS")
    return failed


def test_pre_charge_for_an_import_only_saving_session(my_predbat):
    """A session on a 0p export tariff raises import only, and must still get a pre-charge to cover the house through it.

    Charging at 30p beforehand avoids importing at 330p during the session, as main allowed.
    """
    print("**** test_pre_charge_for_an_import_only_saving_session ****")
    _setup_same_day_session(my_predbat, export_rate=0.0)
    failed = False
    if my_predbat.rate_export_saving_minutes:
        print("ERROR: test setup sanity check failed - a 0p export tariff should have no export session minutes")
        failed = True
    failed |= _check_same_day_pre_charge(my_predbat, "0p export")
    if not failed:
        print("PASS")
    return failed


def test_earlier_event_is_not_a_charge_window(my_predbat):
    """With two sessions in the horizon, the earlier one's own minutes must not be offered as plan charge windows.

    Sessions at 17:30-18:30 today and tomorrow on a 7p/30p tariff: the pre-event windows run up to
    tomorrow's session, and today's session at 330p sits inside that range.
    """
    print("**** test_earlier_event_is_not_a_charge_window ****")
    failed = False

    _setup_same_day_session(my_predbat, export_rate=15.0)
    my_predbat.forecast_minutes = 36 * 60
    tomorrow = set(range(24 * 60 + 17 * 60 + 30, 24 * 60 + 18 * 60 + 30))
    for minute in tomorrow:
        my_predbat.rate_import[minute] += 300.0
        my_predbat.rate_export[minute] += 300.0
    my_predbat.rate_import_saving_minutes |= tomorrow
    my_predbat.rate_export_saving_minutes |= tomorrow
    my_predbat.rate_import_pre_saving.update({minute: my_predbat.rate_import_base[minute] for minute in tomorrow})
    my_predbat.rate_export_pre_saving.update({minute: 15.0 for minute in tomorrow})
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    if my_predbat.rate_import_pre_event_end != 24 * 60 + 17 * 60 + 30:
        print("ERROR: the pre-event windows should run up to tomorrow's session, got {}".format(my_predbat.rate_import_pre_event_end))
        failed = True
    in_event = [window for window in my_predbat.low_rates if window["average"] > 30.0]
    if in_event:
        print("ERROR: today's session should not be a charge window, got {}".format([(w["start"], w["end"], w["average"]) for w in in_event]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_event_in_progress_needs_no_pre_charge(my_predbat):
    """A session already running has nothing before it to charge in, so it gives no pre-event windows."""
    print("**** test_event_in_progress_needs_no_pre_charge ****")
    failed = False

    _setup_same_day_session(my_predbat, export_rate=15.0)
    my_predbat.minutes_now = 17 * 60 + 45
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    if my_predbat.rate_import_pre_event_end is not None:
        print("ERROR: a session in progress should not count as one to charge ahead of, got start {}".format(my_predbat.rate_import_pre_event_end))
        failed = True
    if my_predbat.low_rates_tariff is not my_predbat.low_rates:
        print("ERROR: with no event to charge ahead of the plan should share the tariff windows")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_pre_saving_snapshot_only_with_overrides_and_session_minutes(my_predbat):
    """apply_rate_overrides() leaves the snapshot alone with no overrides, and fetch keeps only its session minutes.

    Without the guard every cycle with a session ran the overrides a second time for nothing, and the full
    snapshot put two extra per-minute rate tables into every debug dump.
    """
    print("**** test_pre_saving_snapshot_only_with_overrides_and_session_minutes ****")
    failed = False

    _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60, event_boost=50.0)
    calls = []
    real_basic_rates = my_predbat.basic_rates

    def spy(info, rtype, prev=None, rate_replicate=None, include_manual_api=True):
        """Count basic_rates() calls, then run it."""
        calls.append(rtype)
        return real_basic_rates(info, rtype, prev, rate_replicate, include_manual_api)

    my_predbat.basic_rates = spy
    try:
        pre_saving = my_predbat.rate_import_pre_saving
        _, returned = my_predbat.apply_rate_overrides(my_predbat.rate_import, pre_saving, my_predbat.rate_import_saving_minutes, [], "rates_import_override", {}, True, my_predbat.rate_import_replicated)
    finally:
        del my_predbat.basic_rates
    if len(calls) != 1 or returned != {minute: pre_saving[minute] for minute in my_predbat.rate_import_saving_minutes}:
        print("ERROR: with no overrides basic_rates() should run once and the snapshot come back unchanged at the session minutes, got {} calls".format(len(calls)))
        failed = True

    kept = my_predbat.pre_saving_for_session_minutes(pre_saving, my_predbat.rate_import_saving_minutes)
    if set(kept) != my_predbat.rate_import_saving_minutes or kept[17 * 60] != 25.95:
        print("ERROR: the kept snapshot should hold exactly the session minutes at their tariff rate, got {} minutes".format(len(kept)))
        failed = True
    if my_predbat.rate_minmax_excluding_saving(my_predbat.rate_import, my_predbat.rate_import_saving_minutes, kept) != my_predbat.rate_minmax_excluding_saving(my_predbat.rate_import, my_predbat.rate_import_saving_minutes, pre_saving):
        print("ERROR: the threshold stats should be the same from the session-minute snapshot as from the full one")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_events_are_found_from_session_minutes(my_predbat):
    """An event is a run of session minutes; price only decides where it starts.

    Two export events at 10:00-12:00 and 16:00-18:00 boost export only. A dip in the later event's export
    price must not split it, so pre-event windows stop at 16:00, not mid-event, and the day rate between
    the two events is offered ahead of the later one.
    """
    print("**** test_events_are_found_from_session_minutes ****")
    failed = False

    _setup_export_event(my_predbat)
    later = set(range(16 * 60, 18 * 60))
    for minute in later:
        my_predbat.rate_export[minute] = 65.0 if minute < 17 * 60 or minute >= 17 * 60 + 30 else 15.0
    my_predbat.rate_export_saving_minutes |= later
    my_predbat.rate_export_pre_saving.update({minute: 15.0 for minute in later})
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    if my_predbat.rate_import_pre_event_end != 16 * 60:
        print("ERROR: the last event should start at 16:00 despite its price dip, got {}".format(my_predbat.rate_import_pre_event_end))
        failed = True
    if any(window["start"] < 16 * 60 < window["end"] for window in my_predbat.low_rates):
        print("ERROR: no plan window should run across the 16:00 event start, got {}".format([(w["start"], w["end"]) for w in my_predbat.low_rates]))
        failed = True
    if not any(window["start"] >= 720 and window["end"] <= 16 * 60 for window in my_predbat.low_rates):
        print("ERROR: the day rate between the two events should be offered ahead of the later one, got {}".format([(w["start"], w["end"]) for w in my_predbat.low_rates]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_free_session_next_to_a_saving_session_stays_a_window(my_predbat):
    """A free session running straight into a saving session is still a cheap charge window before it.

    The two make one run of session minutes; the event starts at its first minute priced above the tariff,
    so the 0p hour before it stays in the plan's windows.
    """
    print("**** test_free_session_next_to_a_saving_session_stays_a_window ****")
    failed = False

    _setup_same_day_session(my_predbat, export_rate=15.0)
    free = set(range(16 * 60 + 30, 17 * 60 + 30))
    for minute in free:
        my_predbat.rate_import[minute] = 0.0
    my_predbat.rate_import_saving_minutes |= free
    my_predbat.rate_import_pre_saving.update({minute: 30.0 for minute in free})
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    if my_predbat.rate_import_pre_event_end != 17 * 60 + 30:
        print("ERROR: the event should start at the saving session's 17:30, not the free session's 16:30, got {}".format(my_predbat.rate_import_pre_event_end))
        failed = True
    if not any(window["average"] == 0.0 and window["end"] <= 17 * 60 + 30 for window in my_predbat.low_rates):
        print("ERROR: the 0p free session should be a plan charge window, got {}".format([(w["start"], w["end"], w["average"]) for w in my_predbat.low_rates]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_event_off_the_five_minute_grid(my_predbat):
    """An event starting between 5 minute steps must not shift the windows after it off the grid, nor make a sliver window."""
    print("**** test_event_off_the_five_minute_grid ****")
    failed = False

    _setup_same_day_session(my_predbat, export_rate=15.0)
    session = set(range(17 * 60 + 2, 18 * 60 + 2))
    for minute in range(0, 48 * 60):
        my_predbat.rate_import[minute] = my_predbat.rate_import_base[minute] + (300.0 if minute in session else 0.0)
        my_predbat.rate_export[minute] = 15.0 + (300.0 if minute in session else 0.0)
    my_predbat.rate_import_saving_minutes = set(session)
    my_predbat.rate_export_saving_minutes = set(session)
    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(my_predbat.rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(my_predbat.rate_export)
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    off_grid = [(w["start"], w["end"]) for w in my_predbat.low_rates if w["start"] % 5 or w["end"] % 5 or w["end"] - w["start"] < 5]
    if off_grid:
        print("ERROR: windows should stay on the 5 minute grid and be at least 5 minutes, got {}".format(off_grid))
        failed = True
    if not any(window["end"] <= 17 * 60 + 2 and window["average"] == 30.0 for window in my_predbat.low_rates):
        print("ERROR: the day rate before the 17:02 event should still be offered, got {}".format([(w["start"], w["end"], w["average"]) for w in my_predbat.low_rates]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_compare_override_on_session_minutes_uses_whole_table(my_predbat):
    """A compare tariff's override must reach the session minutes' pre-session rates as it reaches the rates.

    The kept snapshot holds only the session minutes; basic_rates() bounds and wraps override ranges by
    a table's last minute, so applied to the bare snapshot an all-day increment landed several times and
    an override starting after the session was skipped.
    """
    print("**** test_compare_override_on_session_minutes_uses_whole_table ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    flat = {minute: 10.0 for minute in range(0, 48 * 60)}
    cases = [
        ("all-day +2p", set(range(4 * 60, 5 * 60)), [{"rate_increment": 2.0}], 12.0),
        ("23:00-05:00 at 50p", set(range(3 * 60, 4 * 60)), [{"start": "23:00:00", "end": "05:00:00", "rate": 50.0}], 50.0),
    ]
    for label, session, override, expected in cases:
        pre_saving = {minute: 10.0 for minute in session}
        kept = my_predbat.override_session_rates(flat, pre_saving, session, override, "rates_import_override", {}, True, include_manual_api=False)
        values = sorted(set(kept.values()))
        if set(kept) != session or values != [expected]:
            print("ERROR: {}: the session minutes should all be {} after the override, got {}".format(label, expected, values))
            failed = True

    # A session minute with no pre-session rate (one a session created) has no tariff rate to be capped to
    created = dict(flat)
    created[4000] = 110.0
    kept = my_predbat.override_session_rates(created, {600: 10.0}, {600, 4000}, [{"rate_increment": 2.0}], "rates_import_override", {}, True, include_manual_api=False)
    if set(kept) != {600}:
        print("ERROR: only session minutes with a pre-session rate should come back, got {}".format(sorted(kept)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_pre_event_window_running_into_the_event_is_cut(my_predbat):
    """A pre-event window whose import price carries on into the event must be cut at the event start, not dropped.

    _setup_export_event() boosts export only, so the 30p day rate runs straight through the 10:00 event.
    With slots combined the day rate is one 06:00-22:00 window; it must come back as 06:00-10:00 at
    30p rather than disappear and take the pre-charge with it.
    """
    print("**** test_pre_event_window_running_into_the_event_is_cut ****")
    failed = False

    _setup_export_event(my_predbat)
    combine_charge_slots = my_predbat.combine_charge_slots
    my_predbat.combine_charge_slots = True
    try:
        my_predbat.set_rate_thresholds()
        my_predbat.find_low_rate_windows()
    finally:
        my_predbat.combine_charge_slots = combine_charge_slots

    cut = [window for window in my_predbat.low_rates if window["start"] == 6 * 60]
    if not cut or cut[0]["end"] != 600 or cut[0]["average"] != 30.0:
        print("ERROR: the 06:00 day-rate window should be cut to end at the 10:00 event at 30p, got {}".format([(w["start"], w["end"], w["average"]) for w in my_predbat.low_rates]))
        failed = True
    if any(window["start"] < 600 < window["end"] for window in my_predbat.low_rates):
        print("ERROR: no plan window should run across the event start, got {}".format([(w["start"], w["end"]) for w in my_predbat.low_rates]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_manual_threshold_gets_no_pre_event_windows(my_predbat):
    """A manual import threshold is the user's cap: no pre-event window above it, however much the event pays."""
    print("**** test_manual_threshold_gets_no_pre_event_windows ****")
    failed = False

    _setup_export_event(my_predbat)
    my_predbat.rate_low_threshold = 0.9
    my_predbat.set_rate_thresholds()
    my_predbat.find_low_rate_windows()

    if my_predbat.rate_import_pre_event_end is not None:
        print("ERROR: manual mode should not look for a pre-event charge, got event start {}".format(my_predbat.rate_import_pre_event_end))
        failed = True
    above = [window for window in my_predbat.low_rates if window["average"] > my_predbat.rate_import_cost_threshold]
    if above:
        print("ERROR: no plan window should be above the manual threshold {}, got {}".format(my_predbat.rate_import_cost_threshold, [(w["start"], w["end"], w["average"]) for w in above]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_car_plan_uses_tariff_windows(my_predbat):
    """Car charging must be planned on the tariff's own windows: a car cannot export into the event."""
    print("**** test_car_plan_uses_tariff_windows ****")
    failed = False

    plan_windows = [{"start": 6 * 60, "end": 10 * 60, "average": 30.0}]
    tariff_windows = [{"start": 22 * 60, "end": 30 * 60, "average": 20.0}]
    my_predbat.low_rates = plan_windows
    my_predbat.low_rates_tariff = tariff_windows
    my_predbat.num_cars = 1
    my_predbat.octopus_intelligent_charging = False
    my_predbat.car_charging_planned = [True] + my_predbat.car_charging_planned[1:]
    my_predbat.car_charging_now = [False] + my_predbat.car_charging_now[1:]
    # fetch_sensor_data_car_planning() writes into these lists in place, so give it copies the snapshot can restore past
    my_predbat.car_charging_slots = list(my_predbat.car_charging_slots)

    seen = []

    def record(car_n, low_rates):
        """Record the windows the car is planned on."""
        seen.append(low_rates)
        return []

    my_predbat.plan_car_charging = record
    try:
        my_predbat.fetch_sensor_data_car_planning()
    finally:
        del my_predbat.plan_car_charging

    if seen != [tariff_windows]:
        print("ERROR: the car should be planned on low_rates_tariff, got {}".format(seen))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_no_pre_charge_for_an_event_below_the_import_price(my_predbat):
    """An export event that does not beat the tariff's highest import price must not widen the plan's windows.

    A +10p event on 15p export pays 25p, below the 30p day import rate, so charging ahead of it cannot pay.
    With no qualifying event the plan and the sensors share the one tariff scan.
    """
    print("**** test_no_pre_charge_for_an_event_below_the_import_price ****")
    failed = False

    _setup_export_event(my_predbat, export_boost=10.0)
    my_predbat.set_rate_thresholds()
    if my_predbat.rate_import_pre_event_end is not None:
        print("ERROR: a 25p event below the 30p import maximum should not qualify, got end {}".format(my_predbat.rate_import_pre_event_end))
        failed = True

    my_predbat.find_low_rate_windows()
    if sorted(set(window["average"] for window in my_predbat.low_rates)) != [20.0]:
        print("ERROR: only the 20p night rate should be a plan charge window, got {}".format(sorted(set(window["average"] for window in my_predbat.low_rates))))
        failed = True
    if my_predbat.low_rates_tariff is not my_predbat.low_rates:
        print("ERROR: with no qualifying event the sensors should share the plan's windows, not a second scan")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_low_rate_sensors_publish_tariff_windows(my_predbat):
    """publish_rates_import() must drive the low rate sensors from low_rates_tariff, not the plan's low_rates (GH#5050)."""
    print("**** test_low_rate_sensors_publish_tariff_windows ****")
    failed = False

    my_predbat.minutes_now = 12 * 60
    # The plan's window covers now at the 30p day rate; the tariff's own next cheap window is tonight
    my_predbat.low_rates = [{"start": 6 * 60, "end": 22 * 60, "average": 30.0}]
    my_predbat.low_rates_tariff = [{"start": 22 * 60, "end": 30 * 60, "average": 20.0}]

    published = {}

    def record(entity, state, attributes=None, app=None):
        """Record each published state instead of sending it."""
        published[entity] = state

    my_predbat.dashboard_item = record
    try:
        my_predbat.publish_rates_import()
    finally:
        del my_predbat.dashboard_item

    slot = published.get("binary_sensor." + my_predbat.prefix + "_low_rate_slot")
    cost = published.get(my_predbat.prefix + ".low_rate_cost")
    if slot != "off":
        print("ERROR: low_rate_slot should be off on the 30p day rate, got {} - it followed the plan's event-widened windows".format(slot))
        failed = True
    if cost != 20.0:
        print("ERROR: low_rate_cost should be the tariff's 20p night window, got {}".format(cost))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_dispatch_gone_priced_at_tariff_max(my_predbat):
    """The PV10 "dispatch gone" worst case must price at the tariff's own maximum, not an event reward (#5392).

    set_rate_thresholds() keeps rate_import_tariff_max for Prediction, which uses it as its rate_max
    (the only thing Prediction's rate_max prices). A rescan resets it to the raw maximum until the
    thresholds are worked out again, so a stale tariff maximum can never undercut the worst case.
    """
    print("**** test_dispatch_gone_priced_at_tariff_max ****")
    failed = False

    from prediction import Prediction

    _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60)
    my_predbat.set_rate_thresholds()
    if my_predbat.rate_import_tariff_max != 25.95:
        print("ERROR: rate_import_tariff_max should be the 25.95p day rate, got {}".format(my_predbat.rate_import_tariff_max))
        failed = True
    if my_predbat.rate_max != 125.95:
        print("ERROR: rate_max should keep the event price 125.95 for the dashboards, got {}".format(my_predbat.rate_max))
        failed = True

    flat = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now, 5)}
    pred = Prediction(my_predbat, flat, flat, flat, flat)
    if pred.rate_max != 25.95:
        print("ERROR: Prediction should price a vanished dispatch at the tariff max 25.95, got {}".format(pred.rate_max))
        failed = True

    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    if my_predbat.rate_import_tariff_max != my_predbat.rate_max:
        print("ERROR: a rescan should reset rate_import_tariff_max to the raw max {}, got {}".format(my_predbat.rate_max, my_predbat.rate_import_tariff_max))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_axle_event_on_flat_export_tariff_admits_ordinary_windows(my_predbat):
    """A real Axle export event on a flat 20p export tariff must not hide the ordinary windows (#4036, #5221).

    Built through load_axle_slot() (not hand-seeded tags), then run through the same
    set_rate_thresholds() -> rate_scan_window() -> ratchet sequence as fetch_sensor_data()
    (fetch.py "Find discharging windows"). Without the saving-excluded stats the +100p event makes
    export max != min, so the automatic threshold is min + 0.5 = 20.5p, every ordinary 20p window
    sits below it, only the two event windows are candidates and the ratchet lifts the threshold
    to the event rate. With them the tariff is flat again: threshold min - 0.1 = 19.9p, the
    ordinary windows are candidates and the ratchet settles on the tariff's own 20p. The last
    block clears the saving state to prove the test would fail on the unfixed behaviour.
    """
    print("**** test_axle_event_on_flat_export_tariff_admits_ordinary_windows ****")
    failed = False

    _setup_export_event(my_predbat)
    event_start, event_end = 600, 720
    rate_export_base = {minute: 20.0 for minute in range(0, 48 * 60)}
    session = {
        "start_time": (my_predbat.midnight_utc + timedelta(minutes=event_start)).isoformat(),
        "end_time": (my_predbat.midnight_utc + timedelta(minutes=event_end)).isoformat(),
        "import_export": "export",
        "pence_per_kwh": 100.0,
    }

    def run_sequence(with_saving_state):
        """Mirror fetch_sensor_data(): pre-saving snapshot, Axle loader, saving-minute snapshot, thresholds, window scan, ratchet."""
        rate_export = rate_export_base.copy()
        replicated = {}
        pre_saving = rate_export_base.copy()
        load_axle_slot(my_predbat, [session], rate_export, export=True, rate_replicate=replicated)
        saving_minutes = {minute for minute, tag in replicated.items() if tag == "saving"}
        my_predbat.rate_export = rate_export
        my_predbat.rate_export_base = rate_export_base.copy()
        my_predbat.rate_export_replicated = replicated
        my_predbat.rate_export_saving_minutes = saving_minutes if with_saving_state else set()
        my_predbat.rate_export_pre_saving = pre_saving if with_saving_state else {}
        my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)
        my_predbat.set_rate_thresholds()
        after_thresholds = my_predbat.rate_export_cost_threshold
        windows, lowest, _ = my_predbat.rate_scan_window(my_predbat.rate_export, 5, after_thresholds, True, alt_rates=my_predbat.rate_import)
        if my_predbat.rate_high_threshold == 0 and lowest <= my_predbat.rate_export_max:
            my_predbat.rate_export_cost_threshold = lowest
        return saving_minutes, after_thresholds, windows, my_predbat.rate_export_cost_threshold

    saving_minutes, threshold, windows, ratcheted = run_sequence(True)
    if saving_minutes != set(range(event_start, event_end)):
        print("ERROR: load_axle_slot() should tag exactly minutes {}-{} as saving, got {} minutes".format(event_start, event_end, len(saving_minutes)))
        failed = True
    if abs(threshold - 19.9) > 0.01:
        print("ERROR: threshold with the event capped should be 19.9 (flat 20p: min - 0.1), got {}".format(threshold))
        failed = True
    if not any(window["end"] <= event_start for window in windows):
        print("ERROR: no ordinary export window before the event was admitted - candidates were {}".format([(w["start"], w["end"]) for w in windows]))
        failed = True
    if abs(ratcheted - 20.0) > 0.01:
        print("ERROR: ratchet should settle on the tariff's own 20p, got {}".format(ratcheted))
        failed = True

    _, threshold, windows, ratcheted = run_sequence(False)
    if abs(threshold - 20.5) > 0.01 or any(window["end"] <= event_start for window in windows) or ratcheted < 90:
        print("ERROR: control run without the saving state should reproduce the unfixed behaviour (20.5p, event windows only, ratchet to event rate) - got threshold {}, ratchet {}, {} windows".format(threshold, ratcheted, len(windows)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_set_rate_thresholds_keeps_stored_stats_for_an_empty_table(my_predbat):
    """An empty rate table must keep that side's stored stats, as before the fix.

    rate_minmax() over an empty table returns the (99999, 0, 0) placeholder, which pushed the
    automatic export threshold to 99998.9 where main gave -0.1.
    """
    print("**** test_set_rate_thresholds_keeps_stored_stats_for_an_empty_table ****")
    failed = False

    _setup_export_event(my_predbat)
    my_predbat.rate_export = {}
    my_predbat.rate_export_pre_saving = {}
    my_predbat.rate_export_saving_minutes = set()
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average = 0, 0, 0

    my_predbat.set_rate_thresholds()

    if abs(my_predbat.rate_export_cost_threshold - (-0.1)) > 0.01:
        print("ERROR: empty export table should give the stored-stats threshold -0.1, got {}".format(my_predbat.rate_export_cost_threshold))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_fetch_snapshots_are_taken_in_the_right_order(my_predbat):
    """Pin where fetch_sensor_data() takes each snapshot, since nothing else does.

    Both snapshots are position-sensitive and a move in either direction is silent - the plan simply
    gets slightly wrong thresholds on the affected days, with no error anywhere:

    - rate_import_pre_saving must sit AFTER the rate_add_io_slots() loop (so an IOG dispatch discount
      survives the saving cap) and BEFORE load_saving_slot()/load_free_slot()/
      load_axle_slot() (so the event boost it is meant to undo is not already baked in).
    - rate_import_saving_minutes must sit AFTER those three loaders (so every tagged minute is
      captured) and BEFORE basic_rates()/apply_manual_rates() (so an override active during a
      session cannot overwrite the "saving" tag before it is read).
    - The overrides go through apply_rate_overrides(), which also puts them on the pre-saving snapshot
      so a saving minute the user overrode is capped to the override, not the bare tariff.

    Driving the whole of fetch_sensor_data() would need the entire sensor/Octopus/Axle surface stood
    up, so assert on the source order instead. That is weaker than a behavioural test, but it is the
    property that actually matters here and it fails loudly if anyone moves either line.

    It matches exact source strings, so reformatting or renaming any of these lines also fails it with
    no change in behaviour. Then update the needles below to the new text; the order is what matters.
    """
    print("**** test_fetch_snapshots_are_taken_in_the_right_order ****")
    failed = False

    import inspect

    from fetch import Fetch

    source = inspect.getsource(Fetch.fetch_sensor_data)
    lines = [line.strip() for line in source.splitlines()]

    def find(needle, label):
        """Return the index of the first source line containing needle, or None if it is missing."""
        for index, line in enumerate(lines):
            if needle in line:
                return index
        print("ERROR: could not find {} in fetch_sensor_data() - this test needs updating".format(label))
        return None

    io_loop = find("import_rates = self.rate_add_io_slots(car_n", "the IOG slot loop")
    pre_saving = find("self.rate_import_pre_saving = import_rates.copy()", "the rate_import_pre_saving snapshot")
    saving_slot = find("self.load_saving_slot(self.octopus_saving_slots, import_rates", "load_saving_slot() for import")
    free_slot = find("self.load_free_slot(self.octopus_free_slots, import_rates", "load_free_slot() for import")
    axle_slot = find("load_axle_slot(self, self.axle_sessions, import_rates", "load_axle_slot() for import")
    saving_minutes = find("self.rate_import_saving_minutes = {minute for minute, tag", "the rate_import_saving_minutes snapshot")
    basic = find("import_rates, self.rate_import_pre_saving = self.apply_rate_overrides(", "the import overrides")

    export_pre_saving = find("self.rate_export_pre_saving = self.rate_export_base.copy()", "the rate_export_pre_saving snapshot")
    export_saving_slot = find("self.load_saving_slot(self.octopus_saving_slots, export_rates", "load_saving_slot() for export")
    export_axle_slot = find("load_axle_slot(self, self.axle_sessions, export_rates", "load_axle_slot() for export")
    export_saving_minutes = find("self.rate_export_saving_minutes = {minute for minute, tag", "the rate_export_saving_minutes snapshot")
    export_basic = find("export_rates, self.rate_export_pre_saving = self.apply_rate_overrides(", "the export overrides")

    if None in (io_loop, pre_saving, saving_slot, free_slot, axle_slot, saving_minutes, basic, export_pre_saving, export_saving_slot, export_axle_slot, export_saving_minutes, export_basic):
        return True

    if not io_loop < pre_saving < saving_slot:
        print("ERROR: rate_import_pre_saving must be snapshotted after the IOG loop and before load_saving_slot() - got IOG at {}, snapshot at {}, saving slot at {}".format(io_loop, pre_saving, saving_slot))
        failed = True

    if not saving_slot < free_slot < axle_slot:
        print("ERROR: load_free_slot() must sit between load_saving_slot() and load_axle_slot(), before the saving-minutes snapshot - got saving {}, free {}, axle {}".format(saving_slot, free_slot, axle_slot))
        failed = True

    if not export_pre_saving < export_saving_slot < export_axle_slot < export_saving_minutes < export_basic:
        print("ERROR: export snapshots out of order - pre_saving {}, saving slot {}, axle {}, saving minutes {}, overrides {}".format(export_pre_saving, export_saving_slot, export_axle_slot, export_saving_minutes, export_basic))
        failed = True

    if not axle_slot < saving_minutes < basic:
        print("ERROR: rate_import_saving_minutes must be snapshotted after the session loaders and before the overrides - got axle at {}, snapshot at {}, overrides at {}".format(axle_slot, saving_minutes, basic))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_compare_and_annual_clear_stale_saving_minutes(my_predbat):
    """The saving-minute sets must not outlive the rates they describe.

    rate_import_saving_minutes/rate_export_saving_minutes are only ever populated in
    fetch_sensor_data(), and they hold absolute minute offsets into the live tariff's rate tables.
    annual.py's _apply_rates() and a compare.py tariff that brings its own rates both replace
    rate_import/rate_export with a different tariff and then call set_rate_thresholds() - so after a
    live cycle containing a saving session, those stale offsets would map whatever unrelated minutes
    happen to sit at the same positions in the new tariff back to a "base" rate that has nothing to do
    with them. (A compare tariff that reuses the live rates keeps the live sets - see
    test_compare_reused_live_rates_keep_saving_minutes.)

    Exercises both paths for real and asserts the sets are empty *at the moment
    set_rate_thresholds() runs, not merely by the time the function returns - a reset that ran
    after the thresholds were computed would satisfy the latter and still be wrong.
    """
    print("**** test_compare_and_annual_clear_stale_saving_minutes ****")
    failed = False

    import annual
    from compare import Compare

    stale = {10, 20, 30}

    def seed_stale():
        """Put live-cycle saving minutes and snapshots on the fixture, as a real cycle with a session would leave them."""
        my_predbat.rate_import_saving_minutes = set(stale)
        my_predbat.rate_export_saving_minutes = set(stale)
        # The snapshots the sets index into must be cleared with them, or the two describe
        # different tariffs
        my_predbat.rate_import_pre_saving = {minute: 99.0 for minute in stale}
        my_predbat.rate_export_pre_saving = {minute: 99.0 for minute in stale}

    observed = {}
    real_set_rate_thresholds = my_predbat.set_rate_thresholds

    def spy():
        """Record the saving sets and snapshots as they stand when set_rate_thresholds() runs, then run it."""
        # Capture what the sets and their snapshots looked like when the thresholds were computed
        observed["import"] = set(my_predbat.rate_import_saving_minutes)
        observed["export"] = set(my_predbat.rate_export_saving_minutes)
        observed["import_snapshot"] = dict(my_predbat.rate_import_pre_saving)
        observed["export_snapshot"] = dict(my_predbat.rate_export_pre_saving)
        return real_set_rate_thresholds()

    rates = {minute: 10.0 for minute in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now)}

    # annual._apply_rates() - call it directly, it is a plain function over the instance
    seed_stale()
    my_predbat.set_rate_thresholds = spy
    try:
        annual._apply_rates(my_predbat, dict(rates), dict(rates))
    finally:
        del my_predbat.set_rate_thresholds

    for direction in ("import", "export"):
        if observed.get(direction) != set():
            print("ERROR: annual._apply_rates() left rate_{}_saving_minutes as {} when set_rate_thresholds() ran - a stale live-cycle offset filters the simulated tariff".format(direction, observed.get(direction)))
            failed = True
        if observed.get(direction + "_snapshot") != {}:
            print("ERROR: annual._apply_rates() left rate_{}_pre_saving populated when set_rate_thresholds() ran - it describes the live tariff, not the simulated one".format(direction))
            failed = True

    # compare.fetch_rates() with a tariff that supplies its own rates on both sides - drive the same
    # assertion through the real method, with the live state run_all() would have captured
    observed.clear()
    seed_stale()
    compare = Compare(my_predbat)
    compare.live_saving_state = {
        "import_minutes": set(stale),
        "export_minutes": set(stale),
        "import_pre_saving": {minute: 99.0 for minute in stale},
        "export_pre_saving": {minute: 99.0 for minute in stale},
    }
    my_predbat.set_rate_thresholds = spy
    try:
        compare.fetch_rates({"id": "test", "name": "test", "rates_import": [{"rate": 25.0}], "rates_export": [{"rate": 5.0}]}, dict(rates), dict(rates))
    except Exception as error:  # pragma: no cover - surfaced as a test failure below
        print("ERROR: compare.fetch_rates() raised {}".format(error))
        failed = True
    finally:
        del my_predbat.set_rate_thresholds

    if observed:
        for direction in ("import", "export"):
            if observed.get(direction) != set():
                print("ERROR: compare.fetch_rates() left rate_{}_saving_minutes as {} when set_rate_thresholds() ran - a stale live-cycle offset filters the simulated tariff".format(direction, observed.get(direction)))
                failed = True
            if observed.get(direction + "_snapshot") != {}:
                print("ERROR: compare.fetch_rates() left rate_{}_pre_saving populated when set_rate_thresholds() ran - it describes the live tariff, not the simulated one".format(direction))
                failed = True
    else:
        print("ERROR: compare.fetch_rates() never called set_rate_thresholds() - the reset assertion did not run")
        failed = True

    # compare.fetch_rates() replacing one side only - the other side reuses the live rates, so it
    # must keep its live sets. Each side is decided on its own.
    for replaced, reused, tariff_rates in (("import", "export", {"rates_import": [{"rate": 25.0}]}), ("export", "import", {"rates_export": [{"rate": 5.0}]})):
        observed.clear()
        seed_stale()
        compare.live_saving_state = {
            "import_minutes": set(stale),
            "export_minutes": set(stale),
            "import_pre_saving": {minute: 99.0 for minute in stale},
            "export_pre_saving": {minute: 99.0 for minute in stale},
        }
        tariff = {"id": "test", "name": "test"}
        tariff.update(tariff_rates)
        my_predbat.set_rate_thresholds = spy
        try:
            compare.fetch_rates(tariff, dict(rates), dict(rates))
        finally:
            del my_predbat.set_rate_thresholds
        if observed.get(replaced) != set() or observed.get(replaced + "_snapshot") != {}:
            print("ERROR: compare.fetch_rates() replacing {} only should clear its saving minutes and snapshot, got {} / {}".format(replaced, observed.get(replaced), observed.get(replaced + "_snapshot")))
            failed = True
        if observed.get(reused) != stale or not observed.get(reused + "_snapshot"):
            print("ERROR: compare.fetch_rates() replacing {} only should keep the live {} saving minutes, got {}".format(replaced, reused, observed.get(reused)))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_compare_reused_live_rates_keep_saving_minutes(my_predbat):
    """A compare tariff with no rate sources reuses the live, already-boosted rates, so it must keep the live saving minutes.

    Clearing them there brought #5050 back inside compare: with 7p/30p import and a +300p session,
    the whole 30p day read as low rate.
    """
    print("**** test_compare_reused_live_rates_keep_saving_minutes ****")
    failed = False

    from compare import Compare

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import_pre_saving = {minute: (30.0 if 6 <= (minute // 60) % 24 < 22 else 7.0) for minute in range(0, 48 * 60)}
    rate_import_live = rate_import_pre_saving.copy()
    session = set(range(17 * 60, 18 * 60))
    for minute in session:
        rate_import_live[minute] += 300.0
    rate_export_live = {minute: 15.0 for minute in range(0, 48 * 60)}

    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    compare = Compare(my_predbat)
    compare.live_saving_state = {
        "import_minutes": set(session),
        "export_minutes": set(),
        "import_pre_saving": rate_import_pre_saving,
        "export_pre_saving": dict(rate_export_live),
    }
    compare.fetch_rates({"id": "test", "name": "test"}, rate_import_live, rate_export_live)

    if my_predbat.rate_import_saving_minutes != session:
        print("ERROR: reused live rates should keep the live saving minutes, got {} minutes".format(len(my_predbat.rate_import_saving_minutes)))
        failed = True
    # The plan may also get windows before the session; the low rate sensors' windows must not take in the 30p day rate
    window_averages = sorted(set(window["average"] for window in my_predbat.low_rates_tariff))
    if window_averages != [7.0]:
        print("ERROR: only the 7p night rate should be low rate on reused live rates, got window averages {} (#5050 inside compare)".format(window_averages))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_compare_override_reaches_kept_pre_saving_snapshot(my_predbat):
    """A compare tariff's own override on reused live rates must also go on the kept pre-saving snapshot.

    Otherwise the cap takes the overridden session minute back to the live tariff.
    """
    print("**** test_compare_override_reaches_kept_pre_saving_snapshot ****")
    failed = False

    from compare import Compare

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import_pre_saving = {minute: (30.0 if 6 <= (minute // 60) % 24 < 22 else 7.0) for minute in range(0, 48 * 60)}
    rate_import_live = rate_import_pre_saving.copy()
    session = set(range(17 * 60, 18 * 60))
    for minute in session:
        rate_import_live[minute] += 300.0
    rate_export_live = {minute: 15.0 for minute in range(0, 48 * 60)}

    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    compare = Compare(my_predbat)
    compare.live_saving_state = {
        "import_minutes": set(session),
        "export_minutes": set(),
        "import_pre_saving": rate_import_pre_saving,
        "export_pre_saving": dict(rate_export_live),
    }
    tariff = {"id": "test", "name": "test", "rates_import_override": [{"start": "17:00:00", "end": "18:00:00", "rate": 40.0}]}
    compare.fetch_rates(tariff, rate_import_live, rate_export_live)

    rate_min, rate_max, _ = my_predbat.rate_minmax_excluding_saving(my_predbat.rate_import, my_predbat.rate_import_saving_minutes, my_predbat.rate_import_pre_saving)
    if rate_max != 40.0:
        print("ERROR: the tariff's 40p override on the session hour should be the threshold max, got {} (capped back to the live tariff)".format(rate_max))
        failed = True
    if rate_import_pre_saving[17 * 60] != 30.0:
        print("ERROR: fetch_rates() modified the caller's live pre-saving snapshot in place")
        failed = True

    if not failed:
        print("PASS")
    return failed


_SNAPSHOT_FIELDS = (
    "minutes_now",
    "forecast_minutes",
    "rate_import",
    "rate_import_base",
    "rate_import_replicated",
    "rate_import_saving_minutes",
    "rate_export",
    "rate_export_base",
    "rate_export_replicated",
    "rate_export_saving_minutes",
    "rate_import_pre_saving",
    "rate_export_pre_saving",
    "rate_min",
    "rate_max",
    "rate_average",
    "rate_export_min",
    "rate_export_max",
    "rate_export_average",
    # Set by rate_scan()/rate_scan_export(), which Compare.fetch_rates() runs on its tariff
    "rate_min_minute",
    "rate_max_minute",
    "rate_export_min_minute",
    "rate_export_max_minute",
    "rate_import_cost_threshold",
    "rate_import_pre_event_end",
    "rate_import_pre_event_threshold",
    "rate_import_tariff_max",
    "rate_export_cost_threshold",
    # test_compare_and_annual_clear_stale_saving_minutes drives the real scan pipeline through
    # annual._apply_rates()/Compare.fetch_rates(), which populate these from its synthetic tariff -
    # unrestored they leak a 48h tariff's windows onto the shared fixture (#5079 class of bug, and
    # test_pv90.py depends on them being empty after reset_inverter()).
    "low_rates",
    "low_rates_tariff",
    "high_export_rates",
    "rate_min_forward",
    "rate_low_threshold",
    "rate_high_threshold",
    "alert_active_keep",
    "manual_soc_keep",
    "num_cars",
    # Set by the window-cutting and car planning tests
    "combine_charge_slots",
    "octopus_intelligent_charging",
    "car_charging_planned",
    "car_charging_now",
    "car_charging_slots",
    # compare.fetch_rates() resets io_adjusted when a tariff replaces the import rates
    "io_adjusted",
)


def run_set_rate_thresholds_tests(my_predbat):
    """Run the set_rate_thresholds / rate_minmax_excluding_saving tests.

    _setup_two_rate_tariff() overwrites minutes_now, the rate tables/statistics, thresholds and
    num_cars directly on the shared my_predbat fixture, and unit_test.py passes that same
    instance through the whole registry - left unrestored, a later test would inherit this
    group's synthetic 48h tariff and faked midnight instead of the fixture's own noon state,
    making the suite order-dependent. Snapshot/restore around the whole group rather than
    per sub-test, since every sub-test here uses the same helper and they already run back-to-back
    with no fixture-clean test expected in between.
    """
    snapshot = {field: getattr(my_predbat, field) for field in _SNAPSHOT_FIELDS}
    try:
        failed = False
        failed |= test_rate_minmax_excluding_saving_maps_boosted_minutes_to_base(my_predbat)
        failed |= test_rate_minmax_excluding_saving_keeps_genuine_free_slots(my_predbat)
        failed |= test_rate_minmax_excluding_saving_falls_back_without_a_base_curve(my_predbat)
        failed |= test_rate_minmax_excluding_saving_skips_session_minute_with_no_tariff_rate(my_predbat)
        failed |= test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode(my_predbat)
        failed |= test_set_rate_thresholds_ignores_small_saving_boost_in_manual_import_mode(my_predbat)
        failed |= test_set_rate_thresholds_ignores_export_saving_boost_in_manual_mode(my_predbat)
        failed |= test_set_rate_thresholds_ignores_export_saving_boost_in_automatic_mode(my_predbat)
        failed |= test_plan_charges_ahead_of_a_qualifying_export_event(my_predbat)
        failed |= test_pre_charge_for_a_same_day_saving_session(my_predbat)
        failed |= test_pre_charge_for_an_import_only_saving_session(my_predbat)
        failed |= test_earlier_event_is_not_a_charge_window(my_predbat)
        failed |= test_event_in_progress_needs_no_pre_charge(my_predbat)
        failed |= test_pre_saving_snapshot_only_with_overrides_and_session_minutes(my_predbat)
        failed |= test_events_are_found_from_session_minutes(my_predbat)
        failed |= test_free_session_next_to_a_saving_session_stays_a_window(my_predbat)
        failed |= test_event_off_the_five_minute_grid(my_predbat)
        failed |= test_compare_override_on_session_minutes_uses_whole_table(my_predbat)
        failed |= test_pre_event_window_running_into_the_event_is_cut(my_predbat)
        failed |= test_manual_threshold_gets_no_pre_event_windows(my_predbat)
        failed |= test_car_plan_uses_tariff_windows(my_predbat)
        failed |= test_no_pre_charge_for_an_event_below_the_import_price(my_predbat)
        failed |= test_low_rate_sensors_publish_tariff_windows(my_predbat)
        failed |= test_dispatch_gone_priced_at_tariff_max(my_predbat)
        failed |= test_axle_event_on_flat_export_tariff_admits_ordinary_windows(my_predbat)
        failed |= test_set_rate_thresholds_keeps_stored_stats_for_an_empty_table(my_predbat)
        failed |= test_rate_minmax_excluding_saving_ignores_overwritten_replicate_tag(my_predbat)
        failed |= test_saving_minute_capped_against_post_io_rates(my_predbat)
        failed |= test_saving_minute_keeps_user_override(my_predbat)
        failed |= test_fetch_snapshots_are_taken_in_the_right_order(my_predbat)
        failed |= test_compare_and_annual_clear_stale_saving_minutes(my_predbat)
        failed |= test_compare_reused_live_rates_keep_saving_minutes(my_predbat)
        failed |= test_compare_override_reaches_kept_pre_saving_snapshot(my_predbat)
        return failed
    finally:
        for field, value in snapshot.items():
            setattr(my_predbat, field, value)

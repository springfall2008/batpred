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
scans the same rates but skips any minute in rate_import_saving_minutes/rate_export_saving_minutes
- a frozen snapshot of "saving"-tagged minutes taken right after the saving/free/Axle loaders run
and before any override (rates_import_override, manual rates) can overwrite that same minute's
rate_import_replicated/rate_export_replicated tag with its own "increment"/"user" tag, silently
losing the "this was a saving minute" provenance the live dict alone can no longer tell (Copilot
review on #5052). self.rate_max/rate_min/rate_average (and the export equivalents) are
deliberately left untouched - they feed dashboard sensors, graph scaling, and plan.py pricing,
where the real boosted price is what should be shown.
"""


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

    my_predbat.rate_min, my_predbat.rate_max, my_predbat.rate_average, _, _ = my_predbat.rate_minmax(rate_import)
    my_predbat.rate_export_min, my_predbat.rate_export_max, my_predbat.rate_export_average, _, _ = my_predbat.rate_minmax(rate_export)

    my_predbat.rate_low_threshold = 0
    my_predbat.rate_high_threshold = 0
    my_predbat.alert_active_keep = {}
    my_predbat.manual_soc_keep = {}
    my_predbat.num_cars = 0

    return rate_import, rate_import_saving_minutes


def test_rate_minmax_excluding_saving_skips_the_boosted_minutes(my_predbat):
    """rate_minmax_excluding_saving must report the tariff's own min/max/average, not the
    event-inflated figures - reproducing the exact numbers from the GH#5050 triage."""
    print("**** test_rate_minmax_excluding_saving_skips_the_boosted_minutes ****")
    failed = False

    rate_import, rate_import_saving_minutes = _setup_two_rate_tariff(my_predbat, event_start=17 * 60, event_end=19 * 60)

    if my_predbat.rate_max != 125.95:
        print("ERROR: test setup sanity check failed - contaminated rate_max should be 125.95, got {}".format(my_predbat.rate_max))
        failed = True

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes)
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
    (load_free_slot()) and Axle import discounts, not only event boosts (Copilot review on
    #5052).

    With a flat 20p tariff and one free (0p) slot tagged "saving", excluding every tagged minute
    regardless of direction previously left only the flat rate - rate_max == rate_min - which
    made set_rate_thresholds() take its "everything but the most expensive" branch and set the
    threshold ABOVE every rate, so the entire ordinary-price day read as low-rate. Reproduced
    with this exact scenario before fixing.
    """
    print("**** Testing rate_minmax_excluding_saving keeps a genuine free slot ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import = {minute: 20.0 for minute in range(0, 24 * 60)}
    rate_import[100] = 0.0
    rate_import_saving_minutes = {100}

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes)

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


def test_rate_minmax_excluding_saving_falls_back_when_everything_is_tagged(my_predbat):
    """If an event covers the whole forecast window there is no genuine tariff minute to scan -
    fall back to the plain min/max/average rather than returning a 99999/0/0 that would make every
    downstream comparison in set_rate_thresholds() behave as if there were no data at all."""
    print("**** test_rate_minmax_excluding_saving_falls_back_when_everything_is_tagged ****")
    failed = False

    rate_import, rate_import_saving_minutes = _setup_two_rate_tariff(my_predbat, event_start=0, event_end=my_predbat.forecast_minutes)

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes)
    expected_min, expected_max, expected_average, _, _ = my_predbat.rate_minmax(rate_import)
    if (rate_min, rate_max, rate_average) != (expected_min, expected_max, expected_average):
        print("ERROR: fully-tagged window should fall back to the plain scan {}, got {}".format((expected_min, expected_max, expected_average), (rate_min, rate_max, rate_average)))
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

    Reproduces the review case from #5052: +5p on a 3.49p night slot (8.49p total) stays below the
    25.95p day-rate max, so a global-max filter leaves it in rate_average and inflates the manual
    threshold.
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
    (automatic mode) (Copilot review on #5052). This branch (fetch.py's
    "rate_export_cost_threshold = dp2(rate_export_average * self.rate_high_threshold)") multiplies
    by rate_export_average directly, so a boosted export minute contaminating that average would
    not be masked by the automatic-mode rate_export_max/rate_export_min comparisons the other
    tests already cover.

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
    rate_export = {minute: 15.0 for minute in range(0, 48 * 60)}
    rate_export_replicated = {}
    for minute in range(600, 720):
        rate_export[minute] += 50.0
        rate_export_replicated[minute] = "saving"
    rate_export_saving_minutes = set(range(600, 720))

    my_predbat.rate_import = rate_import
    my_predbat.rate_import_replicated = {}
    my_predbat.rate_import_saving_minutes = set()
    my_predbat.rate_export = rate_export
    my_predbat.rate_export_replicated = rate_export_replicated
    my_predbat.rate_export_saving_minutes = rate_export_saving_minutes
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
    """A saving-boosted minute must stay excluded even after something downstream overwrites its
    rate_replicate tag - a documented, supported combination (an export rate_increment override
    active during the same saving session) rewrites rate_replicate[minute] from "saving" to
    "increment"/"user" (fetch.py's basic_rates()), which happens for real in the production fetch
    path: load_saving_slot()/load_free_slot()/load_axle_slot() tag rate_replicate, then
    basic_rates()/apply_manual_rates() run afterward and can overwrite the tag on the very same
    minute (Copilot review on #5052).

    rate_minmax_excluding_saving() must not be fooled by that - it takes the frozen
    rate_import_saving_minutes/rate_export_saving_minutes set (captured before any override can
    run), not the live, possibly-overwritten rate_replicate dict, so this reproduces the
    overwrite directly rather than relying on the full fetch pipeline.
    """
    print("**** test_rate_minmax_excluding_saving_ignores_overwritten_replicate_tag ****")
    failed = False

    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 24 * 60
    rate_import = {minute: 20.0 for minute in range(0, 24 * 60)}
    rate_import[100] += 50.0  # a saving-session reward, same shape as load_saving_slot()
    rate_import_saving_minutes = {100}
    # Simulate basic_rates()'s rate_increment branch overwriting the same minute's tag afterward -
    # the live rate_replicate dict no longer says "saving" for minute 100 at all.
    rate_import_replicated = {100: "increment"}

    rate_min, rate_max, rate_average = my_predbat.rate_minmax_excluding_saving(rate_import, rate_import_saving_minutes)

    if rate_max != 20.0:
        print("ERROR: the boosted minute should still be excluded from rate_max even though its rate_replicate tag was overwritten, got {}".format(rate_max))
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


def test_compare_and_annual_clear_stale_saving_minutes(my_predbat):
    """The saving-minute sets must not outlive the rates they describe.

    rate_import_saving_minutes/rate_export_saving_minutes are only ever populated in
    fetch_sensor_data(), and they hold absolute minute offsets into the live tariff's rate tables.
    compare.py's fetch_rates() and annual.py's _apply_rates() both replace rate_import/rate_export
    with a simulated tariff and then call set_rate_thresholds() - so after a live cycle containing a
    saving session, those stale offsets would exclude whatever unrelated minutes happen to sit at
    the same positions in the simulated tariff from the threshold scan (Copilot review on #5052).

    Both resets sit beside the existing rate_low_threshold/rate_high_threshold ones, so assert on
    the source of each rather than running the functions: fetch_rates()/_apply_rates() drive the
    whole scan pipeline and rewrite ~24 fields on the shared my_predbat fixture (including
    dashboard_values and the window lists), which would leak into later tests in the registry.
    """
    print("**** test_compare_and_annual_clear_stale_saving_minutes ****")
    failed = False

    import inspect

    import annual
    from compare import Compare

    for label, func, receiver in (("compare.fetch_rates", Compare.fetch_rates, "pb"), ("annual._apply_rates", annual._apply_rates, "predbat")):
        source = inspect.getsource(func)
        for field in ("rate_import_saving_minutes", "rate_export_saving_minutes"):
            if "{}.{} = set()".format(receiver, field) not in source:
                print("ERROR: {}() must reset {} alongside the rates it replaces, or a stale saving-session offset from a live cycle filters the simulated tariff".format(label, field))
                failed = True

    if not failed:
        print("PASS")
    return failed


_SNAPSHOT_FIELDS = (
    "minutes_now",
    "forecast_minutes",
    "rate_import",
    "rate_import_replicated",
    "rate_import_saving_minutes",
    "rate_export",
    "rate_export_replicated",
    "rate_export_saving_minutes",
    "rate_min",
    "rate_max",
    "rate_average",
    "rate_export_min",
    "rate_export_max",
    "rate_export_average",
    "rate_import_cost_threshold",
    "rate_export_cost_threshold",
    "rate_low_threshold",
    "rate_high_threshold",
    "alert_active_keep",
    "manual_soc_keep",
    "num_cars",
    "rate_import_base",
    "rate_export_base",
)


def run_set_rate_thresholds_tests(my_predbat):
    """Run the set_rate_thresholds / rate_minmax_excluding_saving tests.

    _setup_two_rate_tariff() overwrites minutes_now, the rate tables/statistics, thresholds and
    num_cars directly on the shared my_predbat fixture, and unit_test.py passes that same
    instance through the whole registry - left unrestored, a later test would inherit this
    group's synthetic 48h tariff and faked midnight instead of the fixture's own noon state,
    making the suite order-dependent (Copilot review on #5052). Snapshot/restore around the
    whole group rather than per sub-test, since every sub-test here uses the same helper and
    they already run back-to-back with no fixture-clean test expected in between.
    """
    snapshot = {field: getattr(my_predbat, field) for field in _SNAPSHOT_FIELDS}
    try:
        failed = False
        failed |= test_rate_minmax_excluding_saving_skips_the_boosted_minutes(my_predbat)
        failed |= test_rate_minmax_excluding_saving_keeps_genuine_free_slots(my_predbat)
        failed |= test_rate_minmax_excluding_saving_falls_back_when_everything_is_tagged(my_predbat)
        failed |= test_set_rate_thresholds_ignores_saving_boost_in_automatic_mode(my_predbat)
        failed |= test_set_rate_thresholds_ignores_small_saving_boost_in_manual_import_mode(my_predbat)
        failed |= test_set_rate_thresholds_ignores_export_saving_boost_in_manual_mode(my_predbat)
        failed |= test_rate_minmax_excluding_saving_ignores_overwritten_replicate_tag(my_predbat)
        failed |= test_compare_and_annual_clear_stale_saving_minutes(my_predbat)
        return failed
    finally:
        for field, value in snapshot.items():
            setattr(my_predbat, field, value)

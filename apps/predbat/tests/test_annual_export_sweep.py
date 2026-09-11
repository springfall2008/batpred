# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual engine's multi-export-tariff sweep."""

import asyncio
from datetime import date

import pytz

import annual
from annual import SCENARIO_FIELDS, SCENARIO_KEYS, AnnualConfigError, AnnualPredictor, validate_config


class _StubTariff:
    """A minimal AnnualTariff stand-in: no network, no real rates, records what it is asked.

    Used to prove _plan_one_month/_plan_months/_export_card actually operate on the tariff
    OBJECT they are given rather than silently falling back to self.tariff - the failure mode
    that would collapse an export sweep's three cards into one number while leaving the rest
    of the suite green.
    """

    def __init__(self, standing_charge_p_per_day=50.0, fallback_months=None, unpaid_export_months=None):
        """Configure the stub; fallback_months/unpaid_export_months default to empty sets."""
        self.standing_charge_p_per_day = standing_charge_p_per_day
        self.fallback_months = set() if fallback_months is None else fallback_months
        self.unpaid_export_months = set() if unpaid_export_months is None else unpaid_export_months
        self.fetch_month_calls = []
        self.rates_for_calls = []

    async def fetch_month(self, year, month):
        """Record the call and report every month as available."""
        self.fetch_month_calls.append((year, month))
        return True

    def rates_for(self, midnight_utc, minutes):
        """Record the call and return empty (import, export) per-minute rate dicts."""
        self.rates_for_calls.append((midnight_utc, minutes))
        return {}, {}


def _fake_select_samples(weather, year, month, samples_per_month, has_solar=True, sampling="percentile"):
    """Return exactly one fixed sample day for the month, regardless of weather.

    Lets _plan_one_month/_plan_months run with self.weather left as None - no real solar
    series is ever consulted.
    """
    return [(date(year, month, 15), 1.0)]


def _make_fake_run_day():
    """Return (fake_run_day, calls): calls records the exact tariff object passed each time.

    The zeroed scenario dict matches what _month_scenarios needs: one entry per
    SCENARIO_KEYS, each holding every SCENARIO_FIELDS key.
    """
    calls = []

    def fake_run_day(predbat, config, weather, tariff, load_source, day, midnight_utc, plans=None, baseline_tariff=None):
        # Not async: the real run_day() is a plain sync function - _plan_one_month calls it
        # without awaiting, so an async stub here would hand back an unawaited coroutine.
        calls.append(tariff)
        return {key: {field: 0.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}

    return fake_run_day, calls


def base_config():
    """Return a minimal valid annual config."""
    return {
        "annual": {
            "location": {"latitude": 51.5, "longitude": -0.1},
            "solar": [{"kwp": 5.6}],
            "battery": {"size_kwh": 9.5, "inverter_kw": 5.0},
            "load": {"annual_kwh": 3800},
            "tariff": {"rates_import": [{"rate": 25.0}]},
        }
    }


def expect_error(label, config, fragment, failed):
    """Assert that validate_config rejects the config with a message containing fragment."""
    try:
        validate_config(config)
    except AnnualConfigError as error:
        if fragment.lower() not in str(error).lower():
            print("  ERROR: {} raised '{}', expected it to mention '{}'".format(label, error, fragment))
            return True
        return failed
    print("  ERROR: {} should have raised AnnualConfigError".format(label))
    return True


def test_annual_export_sweep(my_predbat):
    """The export_tariffs config key is validated and normalised."""
    failed = False

    print("Test: export_tariffs defaults to an empty list")
    if validate_config(base_config())["export_tariffs"] != []:
        print("  ERROR: an absent annual.export_tariffs should default to []")
        failed = True

    print("Test: a well-formed export_tariffs list is accepted in order")
    config = base_config()
    config["annual"]["export_tariffs"] = [
        {"id": "outgoing_fixed", "name": "Octopus Outgoing Fixed", "export_octopus_url": "https://example.test/fixed"},
        {"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]},
    ]
    result = validate_config(config)["export_tariffs"]
    if [entry["id"] for entry in result] != ["outgoing_fixed", "seg"]:
        print("  ERROR: export_tariffs should preserve order, got {}".format(result))
        failed = True

    print("Test: export_tariffs rejects malformed entries")
    for bad, fragment in (
        ([{"name": "no id", "rates_export": [{"rate": 4.1}]}], "id"),
        ([{"id": "no_rates", "name": "No rates"}], "rates_export"),
        ([{"id": "dup", "name": "A", "rates_export": [{"rate": 1.0}]}, {"id": "dup", "name": "B", "rates_export": [{"rate": 2.0}]}], "repeat"),
        ("outgoing_fixed", "list"),
    ):
        config = base_config()
        config["annual"]["export_tariffs"] = bad
        failed = expect_error("export_tariffs = {}".format(bad), config, fragment, failed)

    print("Test: an empty export_tariffs list is treated as absent, not as an error")
    config = base_config()
    config["annual"]["export_tariffs"] = []
    try:
        if validate_config(config)["export_tariffs"] != []:
            print("  ERROR: an empty list should normalise to []")
            failed = True
    except AnnualConfigError as error:
        print("  ERROR: an empty export_tariffs list should be accepted, got '{}'".format(error))
        failed = True

    print("Test: export_tariffs forces fast_mode off, mirroring the explicit-months rule")
    # A sweep tariff would only plan the four anchor months, and the by_export branch
    # always reports fast_mode=False/months_interpolated=0 with no interpolation step - so
    # left alone, fast mode would silently under-sample a sweep and report it as complete.
    config = base_config()
    config["annual"]["fast_mode"] = True
    config["annual"]["export_tariffs"] = [{"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]}]
    if validate_config(config)["fast_mode"] is not False:
        print("  ERROR: export_tariffs + fast_mode=True with no months should force fast_mode to False")
        failed = True

    print("Test: export_tariffs still forces fast_mode off alongside an explicit month subset")
    config = base_config()
    config["annual"]["fast_mode"] = True
    config["annual"]["months"] = [7]
    config["annual"]["export_tariffs"] = [{"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]}]
    if validate_config(config)["fast_mode"] is not False:
        print("  ERROR: export_tariffs + fast_mode=True + an explicit month subset should still force fast_mode to False")
        failed = True

    print("Test: negative control - fast_mode survives when there is no export_tariffs sweep")
    # Without this, a validate_config that just hardcoded fast_mode to False would also
    # pass the two tests above.
    config = base_config()
    config["annual"]["fast_mode"] = True
    if validate_config(config)["fast_mode"] is not True:
        print("  ERROR: fast_mode=True with no export_tariffs and no explicit months should stay True")
        failed = True

    print("Test: AnnualPredictor.config carries the validated export_tariffs list, not just validate_config()")
    # This checks the predictor's own wiring (validate_config's return value reaches
    # self.config unchanged via __init__) - it does NOT touch by_export itself, which needs
    # a real tariff and rows to assemble; see test_annual_export_sweep_card_shape for that.
    predictor = AnnualPredictor(base_config())
    if predictor.config["export_tariffs"] != []:
        print("  ERROR: a config with no sweep should carry an empty export_tariffs list")
        failed = True

    config = base_config()
    config["annual"]["export_tariffs"] = [{"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]}]
    predictor = AnnualPredictor(config)
    if [entry["id"] for entry in predictor.config["export_tariffs"]] != ["seg"]:
        print("  ERROR: the predictor should carry the validated sweep list")
        failed = True

    return failed


def test_annual_export_sweep_dno_region(my_predbat):
    """A templated {dno_region} export URL is rejected exactly like the identical URL on annual.tariff.

    Without this check, an entry naming a real Octopus product such as Outgoing Fixed/Prime
    (both {dno_region}-templated in tariff_catalogue.py) against a flat rates_import-only
    config resolves to export_url=None inside AnnualTariff, which prices every export minute
    at zero with no fallback triggered and no caveat raised - a real product silently scoring
    as a legitimate zero-export result.
    """
    failed = False

    print("Test: a templated export URL with no dno_region anywhere is rejected")
    config = base_config()
    config["annual"]["export_tariffs"] = [{"id": "outgoing_fixed", "name": "Octopus Outgoing Fixed", "export_octopus_url": "https://example.test/E-1R-OUTGOING-VAR-24-10-26-{dno_region}/standard-unit-rates/"}]
    failed = expect_error("templated export URL, no dno_region set", config, "dno_region", failed)

    print("Test: a templated export URL is rejected even when other entries in the list are fine")
    config = base_config()
    config["annual"]["export_tariffs"] = [
        {"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]},
        {"id": "outgoing_fixed", "name": "Octopus Outgoing Fixed", "export_octopus_url": "https://example.test/E-1R-OUTGOING-VAR-24-10-26-{dno_region}/standard-unit-rates/"},
    ]
    failed = expect_error("templated export URL among otherwise-valid entries", config, "dno_region", failed)

    print("Test: the same templated export URL is accepted once annual.tariff.dno_region is set")
    config = base_config()
    config["annual"]["tariff"]["dno_region"] = "A"
    config["annual"]["export_tariffs"] = [{"id": "outgoing_fixed", "name": "Octopus Outgoing Fixed", "export_octopus_url": "https://example.test/E-1R-OUTGOING-VAR-24-10-26-{dno_region}/standard-unit-rates/"}]
    try:
        result = validate_config(config)["export_tariffs"]
    except AnnualConfigError as error:
        print("  ERROR: a templated export URL with dno_region set should be accepted, got '{}'".format(error))
        failed = True
    else:
        if [entry["id"] for entry in result] != ["outgoing_fixed"]:
            print("  ERROR: expected the entry to survive validation, got {}".format(result))
            failed = True

    print("Test: an export URL with no {dno_region} placeholder is unaffected by a missing dno_region")
    config = base_config()
    config["annual"]["export_tariffs"] = [{"id": "seg", "name": "SEG", "export_octopus_url": "https://example.test/flat-url/"}]
    try:
        result = validate_config(config)["export_tariffs"]
    except AnnualConfigError as error:
        print("  ERROR: an export URL with no {{dno_region}} placeholder should not require dno_region, got '{}'".format(error))
        failed = True
    else:
        if [entry["id"] for entry in result] != ["seg"]:
            print("  ERROR: expected the entry to survive validation, got {}".format(result))
            failed = True

    return failed


def test_annual_export_sweep_tariff_threading(my_predbat):
    """_plan_one_month and _plan_months price EACH tariff on the exact object they are given.

    Reverting _plan_one_month's tariff parameter back to reading self.tariff would leave the
    rest of the annual suite green (nothing else in it calls _plan_one_month against a tariff
    that differs from self.tariff), while silently collapsing every export-tariff sweep card
    onto one number - exactly the failure mode this feature exists to prevent. Asserted
    differentially: identity (`is`) checks on the object run_day/rates_for actually received,
    not merely "some tariff was passed", which would still pass against that regression.
    """
    failed = False

    config = base_config()
    config["annual"]["solar"] = []  # battery-only: _fake_select_samples ignores has_solar anyway
    config["annual"]["months"] = [6]
    predictor = AnnualPredictor(config)
    predictor.predbat = object()
    predictor.weather = None
    predictor.load_source = object()
    predictor.baseline_tariff = _StubTariff()

    zone = pytz.timezone(predictor.config["timezone"])
    year = predictor.config["year"]
    stub_a = _StubTariff(standing_charge_p_per_day=10.0)
    stub_b = _StubTariff(standing_charge_p_per_day=99.0)

    fake_run_day, run_day_calls = _make_fake_run_day()
    original_select_samples = annual.select_samples
    original_run_day = annual.run_day
    annual.select_samples = _fake_select_samples
    annual.run_day = fake_run_day
    try:
        print("Test: _plan_one_month passes ITS tariff argument to run_day, not self.tariff")
        asyncio.run(predictor._plan_one_month(stub_a, 6, year, zone, 30, 300.0, True))
        if not run_day_calls or run_day_calls[-1] is not stub_a:
            print("  ERROR: run_day should have received stub_a itself, got {!r}".format(run_day_calls[-1] if run_day_calls else None))
            failed = True
        if not stub_a.rates_for_calls:
            print("  ERROR: stub_a.rates_for should have been called for its own month")
            failed = True
        if stub_b.rates_for_calls:
            print("  ERROR: stub_b.rates_for should not have been touched by stub_a's call")
            failed = True

        print("Test: a second tariff object is threaded through independently of the first")
        asyncio.run(predictor._plan_one_month(stub_b, 7, year, zone, 31, 310.0, True))
        if run_day_calls[-1] is not stub_b:
            print("  ERROR: run_day should have received stub_b itself, got {!r}".format(run_day_calls[-1]))
            failed = True
        if not stub_b.rates_for_calls:
            print("  ERROR: stub_b.rates_for should have been called for its own month")
            failed = True
    finally:
        annual.select_samples = original_select_samples
        annual.run_day = original_run_day

    print("Test: _plan_months threads its tariff through fetch_month/_plan_one_month and returns a 4-tuple")
    stub_c = _StubTariff(standing_charge_p_per_day=42.0)
    fake_run_day_2, run_day_calls_2 = _make_fake_run_day()
    annual.select_samples = _fake_select_samples
    annual.run_day = fake_run_day_2
    try:
        result = asyncio.run(predictor._plan_months(stub_c, year, zone, [6], None, 1))
    finally:
        annual.select_samples = original_select_samples
        annual.run_day = original_run_day

    if len(result) != 4:
        print("  ERROR: _plan_months should return a 4-tuple (months, baseline_fallback_months, completed, interpolatable), got {} item(s)".format(len(result)))
        failed = True
    else:
        months, baseline_fallback_months, completed, interpolatable = result
        if not stub_c.fetch_month_calls:
            print("  ERROR: _plan_months should have called fetch_month on the tariff it was given, not self.tariff")
            failed = True
        if not run_day_calls_2 or any(tariff is not stub_c for tariff in run_day_calls_2):
            print("  ERROR: every _plan_one_month call inside _plan_months should have received stub_c, got {!r}".format(run_day_calls_2))
            failed = True
        if completed != 1:
            print("  ERROR: expected 1 completed month, got {}".format(completed))
            failed = True
        if not months or months[0]["month"] != 6:
            print("  ERROR: expected month 6's planned row, got {!r}".format(months))
            failed = True

    return failed


def test_annual_export_sweep_card_shape(my_predbat):
    """_export_card assembles the exact five by_export keys format_table renders, from a stub tariff's own rows.

    This is the coverage the original 'results document gains by_export' test claimed to have
    but did not: that block only re-checked config["export_tariffs"], never touching by_export
    itself.
    """
    failed = False

    predictor = AnnualPredictor(base_config())
    predictor.caveats = []
    year = predictor.config["year"]
    months = [
        {
            "month": 6,
            "status": "ok",
            "days": 30,
            "standing_charge_p": 150.0,
            "scenarios": {key: {field: 1.0 for field in SCENARIO_FIELDS + ["export_credit_p_estimate"]} for key in SCENARIO_KEYS},
        }
    ]

    print("Test: the card carries exactly the five keys format_table renders")
    entry = {"id": "seg", "name": "SEG"}
    card = predictor._export_card(entry, _StubTariff(), months, year)
    expected_keys = {"name", "annual", "months", "caveats", "rates_synthesised"}
    if set(card) != expected_keys:
        print("  ERROR: expected keys {}, got {}".format(sorted(expected_keys), sorted(card)))
        failed = True

    print("Test: name comes from the entry, months are the rows this tariff actually planned")
    if card.get("name") != "SEG":
        print("  ERROR: name should be carried through from entry, got {!r}".format(card.get("name")))
        failed = True
    if card.get("months") != months:
        print("  ERROR: months should be exactly the rows passed in, got {!r}".format(card.get("months")))
        failed = True

    print("Test: annual is built from those same months (one included month, real totals)")
    annual_block = card.get("annual") or {}
    if annual_block.get("months_included") != 1:
        print("  ERROR: expected 1 included month, got {!r}".format(annual_block))
        failed = True

    print("Test: caveats and rates_synthesised reflect this tariff's own fallback state, not a default")
    fallback_card = predictor._export_card(entry, _StubTariff(fallback_months={(year, 6, "export")}), months, year)
    if not fallback_card["caveats"]:
        print("  ERROR: a tariff with an export fallback this year should carry a caveat")
        failed = True
    if fallback_card["rates_synthesised"] is not True:
        print("  ERROR: a tariff with an export fallback this year should have rates_synthesised=True")
        failed = True

    return failed


def test_annual_export_sweep_rates_synthesised(my_predbat):
    """rates_synthesised is export-side only and scoped to the modelled year.

    It exists for exactly one job: mark a card whose EXPORT rates were synthesised from the
    current-rates fallback (Outgoing Prime has no history before June 2026). It must NOT go
    true for an unpaid-export month (priced at zero - a different failure from being
    synthesised), for a fallback on the shared IMPORT side alone (which would flag every card
    in a sweep regardless of that tariff's own export rates), or for a fallback in the
    December-spill fetch of (year + 1, 1) (unrelated to this year's modelled months).
    """
    failed = False

    predictor = AnnualPredictor(base_config())
    predictor.caveats = []
    entry = {"id": "seg", "name": "SEG"}
    year = predictor.config["year"]
    months = []

    cases = (
        ("an export fallback this year", {(year, 6, "export")}, set(), True),
        ("an import fallback this year, no export fallback", {(year, 6, "import")}, set(), False),
        ("both sides fell back this year", {(year, 6, "import"), (year, 6, "export")}, set(), True),
        ("an export fallback only in next year's spill month", {(year + 1, 1, "export")}, set(), False),
        ("an unpaid export month with no current-rates fallback at all", set(), {(year, 6)}, False),
        ("no fallback and nothing unpaid", set(), set(), False),
    )
    for label, fallback_months, unpaid_export_months, expected in cases:
        print("Test: rates_synthesised is {} for {}".format(expected, label))
        stub = _StubTariff(fallback_months=fallback_months, unpaid_export_months=unpaid_export_months)
        card = predictor._export_card(entry, stub, months, year)
        if card["rates_synthesised"] is not expected:
            print("  ERROR: {} should give rates_synthesised={}, got {}".format(label, expected, card["rates_synthesised"]))
            failed = True

    return failed


def test_annual_export_sweep_run(my_predbat):
    """AnnualPredictor.run()'s sweep branch: a failed tariff's caveats land on ITS card, not
    the run-wide list, and the sweep emits a terminal progress event once it finishes.

    Nothing else in the suite calls run() itself - test_annual_export_sweep_card_shape and
    test_annual_export_sweep_tariff_threading exercise _export_card/_plan_months/
    _plan_one_month directly, which is what let both regressions through: _build_results
    (called once per swept tariff by _export_card) appends its "no month produced a usable
    result" and payback caveats onto self.caveats as a side effect, and left alone that
    lands on the shared run-wide list every time it is called - so one failed tariff out of
    four tells every card's reader that NOTHING was modelled, naming no tariff at all. And
    _plan_months only ever reports progress up to (sweep_total - 1, sweep_total), so a
    sweep never closed out its own progress bar.

    Also covers the sibling leak found in review: _plan_months itself appends the
    next-month spill-fetch warning straight onto self.caveats, and that call happens
    BEFORE _export_card's own scratch-list swap even starts, so left alone it would land
    on the run-wide list regardless of _export_card's fix - one tariff's spill warning
    would read as "every card's figures may be wrong" instead of naming the one tariff
    whose download actually failed.

    Fully stubbed - AnnualTariff, run_day, select_samples and create_headless_predbat are
    all replaced, and the config has no solar array so run() never touches the weather
    module (self.weather stays None) - so this needs no network and no real Predbat
    instance.
    """
    failed = False

    class _StubHeadlessPredbat:
        """Stands in for create_headless_predbat()'s return value; nothing reads it here."""

    class _RunStubTariff:
        """A per-instance AnnualTariff stand-in, one call to AnnualTariff() per sweep entry.

        Every fetch_month() fails when this tariff's own config carries the sentinel
        export_octopus_url "FAIL", modelling the "1 of 4 sweep tariffs has no rate data"
        case I1 describes. A second sentinel, "FAIL_SPILL", instead fails only the fetch
        for month 7 - the (year, month + 1) spill month _plan_months fetches after month
        6 (the only month this test plans) succeeds - so that tariff plans its own month
        fine but still hits the spill-warning branch. The remaining two tariffs succeed on
        every fetch_month call and plan a real (stubbed) month.
        """

        def __init__(self, config, log=None, predbat=None, storage=None, timezone=None):
            self.fails = config.get("export_octopus_url") == "FAIL"
            self.fails_spill_month = config.get("export_octopus_url") == "FAIL_SPILL"
            self.fallback_months = set()
            self.unpaid_export_months = set()
            self.standing_charge_p_per_day = 50.0

        async def fetch_month(self, year, month):
            if self.fails:
                return False
            if self.fails_spill_month and month == 7:
                return False
            return True

        def rates_for(self, midnight_utc, minutes):
            return {}, {}

    def fake_run_day(predbat, config, weather, tariff, load_source, day, midnight_utc, plans=None, baseline_tariff=None):
        # Not async: the real run_day() is a plain sync function.
        return {key: {field: 0.0 for field in SCENARIO_FIELDS} for key in SCENARIO_KEYS}

    def fake_select_samples(weather, year, month, samples_per_month, has_solar=True, sampling="percentile"):
        return [(date(year, month, 15), 1.0)]

    config = {
        "annual": {
            "location": {"latitude": 51.5, "longitude": -0.1},
            "battery": {"size_kwh": 9.5, "inverter_kw": 5.0},
            "load": {"annual_kwh": 3800},
            "tariff": {"rates_import": [{"rate": 25.0}]},
            "months": [6],
            "export_tariffs": [
                {"id": "ok_one", "name": "OK One", "rates_export": [{"rate": 4.1}]},
                {"id": "fails", "name": "Fails", "export_octopus_url": "FAIL"},
                {"id": "ok_two", "name": "OK Two", "rates_export": [{"rate": 8.0}]},
                {"id": "spills", "name": "Spills", "export_octopus_url": "FAIL_SPILL"},
            ],
        }
    }
    predictor = AnnualPredictor(config)

    progress_events = []

    def progress(completed, total, message):
        progress_events.append((completed, total, message))

    original_tariff, original_run_day, original_select_samples, original_create_headless = (
        annual.AnnualTariff,
        annual.run_day,
        annual.select_samples,
        annual.create_headless_predbat,
    )
    annual.AnnualTariff = _RunStubTariff
    annual.run_day = fake_run_day
    annual.select_samples = fake_select_samples
    annual.create_headless_predbat = lambda work_dir, timezone, log: _StubHeadlessPredbat()
    try:
        results = asyncio.run(predictor.run(progress=progress))
    finally:
        annual.AnnualTariff = original_tariff
        annual.run_day = original_run_day
        annual.select_samples = original_select_samples
        annual.create_headless_predbat = original_create_headless

    print("Test: the failing tariff's own card says nothing was modelled (I1)")
    failing_caveats = " ".join(results["by_export"]["fails"]["caveats"])
    if "No month produced a usable result" not in failing_caveats:
        print("  ERROR: the failing tariff's card should carry the no-usable-result caveat, got {}".format(results["by_export"]["fails"]["caveats"]))
        failed = True

    print("Test: that caveat does NOT leak onto the run-wide list or the other cards (I1)")
    run_wide_caveats = " ".join(results["caveats"])
    if "No month produced a usable result" in run_wide_caveats:
        print("  ERROR: the failing tariff's caveat leaked into the run-wide caveats, got {}".format(results["caveats"]))
        failed = True
    for tariff_id in ("ok_one", "ok_two", "spills"):
        card_caveats = " ".join(results["by_export"][tariff_id]["caveats"])
        if "No month produced a usable result" in card_caveats:
            print("  ERROR: {}'s card should not carry the failed tariff's caveat, got {}".format(tariff_id, results["by_export"][tariff_id]["caveats"]))
            failed = True

    print("Test: a tariff that fails only its spill-month fetch carries that caveat on its own card (review finding)")
    spill_caveats = " ".join(results["by_export"]["spills"]["caveats"])
    if "could not be downloaded" not in spill_caveats:
        print("  ERROR: the spill tariff's card should carry the spill-month caveat, got {}".format(results["by_export"]["spills"]["caveats"]))
        failed = True

    print("Test: the spill-month caveat does NOT leak onto the run-wide list or the other cards (review finding)")
    if "could not be downloaded" in run_wide_caveats:
        print("  ERROR: the spill tariff's caveat leaked into the run-wide caveats, got {}".format(results["caveats"]))
        failed = True
    for tariff_id in ("ok_one", "ok_two", "fails"):
        card_caveats = " ".join(results["by_export"][tariff_id]["caveats"])
        if "could not be downloaded" in card_caveats:
            print("  ERROR: {}'s card should not carry the spill tariff's caveat, got {}".format(tariff_id, results["by_export"][tariff_id]["caveats"]))
            failed = True

    print("Test: the sweep emits a terminal completed==total 'Complete' progress event (I2)")
    if not progress_events or progress_events[-1][0] != progress_events[-1][1] or progress_events[-1][2] != "Complete":
        print("  ERROR: expected a final (total, total, 'Complete') progress event, got {}".format(progress_events[-1] if progress_events else None))
        failed = True
    # A 1-month x 4-tariff sweep should report a total of 4 throughout, closing at (4, 4).
    if any(total != 4 for _, total, _ in progress_events):
        print("  ERROR: expected every progress event to report total=4, got {}".format(progress_events))
        failed = True

    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction command line output."""

import copy
import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

import annual_cli
from annual import AnnualConfigError
from annual_cli import format_table, make_progress
from storage import StorageLocalFiles


class _StubPredictor:
    """Stands in for AnnualPredictor so main()'s argument wiring can be tested in isolation.

    Records the ``log`` callable it was constructed with rather than doing any real work,
    so a test can assert what main() actually passes through under --quiet without needing
    a real config, the network, or a headless PredBat instance.
    """

    captured_log = None
    captured_storage = None

    def __init__(self, config, log=None, storage=None, work_dir=None):
        """Record the log callable and the storage, and discard everything else."""
        _StubPredictor.captured_log = log
        _StubPredictor.captured_storage = storage

    async def run(self, progress=None):
        """Report one fake progress step (if asked) and return canned results."""
        if progress:
            progress(0, 1, "stub")
        return sample_results()


class _WarningPredictor:
    """Stub predictor whose run() logs a warning before returning results.

    Mirrors a real mid-run warning (a P10 fallback, missing rate data, a failed sample day) so a
    test can check where that warning lands: on stderr, never on stdout, in machine mode. The
    regression this guards against is a stray warning-turned-print inside ``main()``'s machine
    branch, which ``test_annual_cli_machine``'s unit-level checks of ``make_progress`` alone
    cannot see, since that warning is emitted by the engine, not by the progress callback.
    """

    def __init__(self, config, log=None, storage=None, work_dir=None):
        """Record the log callable so run() can use it."""
        self._log = log

    async def run(self, progress=None):
        """Emit one warning through the recorded log callable, then return canned results."""
        if self._log:
            self._log("Warn: stub P10 fallback warning")
        return sample_results()


def sample_results():
    """Return a small results document covering an ok month and an unavailable one."""
    scenarios = {
        "no_pvbat": {"cost_p": 12000.0, "import_kwh": 400.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "battery_cycles": 0.0, "export_credit_p_estimate": 0.0},
        "pv_only": {"cost_p": 10000.0, "import_kwh": 350.0, "export_kwh": 60.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 0.0, "battery_cycles": 0.0, "export_credit_p_estimate": 180.0},
        "without_predbat": {"cost_p": 8000.0, "import_kwh": 300.0, "export_kwh": 20.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 90.0, "battery_cycles": 2.0, "export_credit_p_estimate": 300.0},
        "with_predbat": {"cost_p": 6000.0, "import_kwh": 280.0, "export_kwh": 45.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 140.0, "battery_cycles": 3.0, "export_credit_p_estimate": 675.0},
    }
    return {
        "year": 2025,
        "config": {},
        "months": [
            {"month": 1, "status": "ok", "days": 31, "sampled_days": ["2025-01-08", "2025-01-24"], "standing_charge_p": 1860.0, "scenarios": scenarios},
            {"month": 2, "status": "unavailable", "reason": "no rate data available", "days": 28, "standing_charge_p": 1680.0},
        ],
        "annual": {
            "scenarios": scenarios,
            "standing_charge_p": 1860.0,
            "savings": {"pv_battery_vs_none_p": 4000.0, "predbat_vs_baseline_p": 2000.0},
            "months_included": 1,
            "months_excluded": [2],
        },
        "caveats": ["An example caveat."],
    }


def sample_results_no_usable_month():
    """Return a results document where every month is unavailable and no annual total exists.

    Mirrors what ``AnnualPredictor._build_results`` returns when nothing is included:
    ``annual.scenarios`` and ``annual.standing_charge_p`` are ``None`` and ``savings`` is
    empty. ``format_table`` must not divide those ``None`` values or print them as zeroes.
    """
    return {
        "year": 2025,
        "config": {},
        "months": [
            {"month": 1, "status": "unavailable", "reason": "no rate data available", "days": 31, "standing_charge_p": 1860.0},
            {"month": 2, "status": "unavailable", "reason": "no usable weather days", "days": 28, "standing_charge_p": 1680.0},
        ],
        "annual": {
            "scenarios": None,
            "standing_charge_p": None,
            "savings": {},
            "months_included": 0,
            "months_excluded": [1, 2],
        },
        "caveats": ["No month produced a usable result, so no annual totals or savings could be calculated."],
    }


def sample_results_with_degraded_month():
    """Return a results document containing one 'degraded' month, built from fewer samples.

    Mirrors what ``AnnualPredictor.run()`` emits when some, but not all, of a month's
    sampled days failed to plan: the month still carries real ``scenarios`` figures and
    a non-empty ``failed_days`` list, and - unlike an "unavailable" month - it IS counted
    in ``annual.months_included`` and is absent from ``annual.months_excluded``.
    """
    scenarios = {
        "no_pvbat": {"cost_p": 9000.0, "import_kwh": 300.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 0.0},
        "pv_only": {"cost_p": 7800.0, "import_kwh": 260.0, "export_kwh": 45.0, "pv_generated_kwh": 100.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 135.0},
        "without_predbat": {"cost_p": 7000.0, "import_kwh": 250.0, "export_kwh": 15.0, "pv_generated_kwh": 100.0, "battery_throughput_kwh": 80.0, "export_credit_p_estimate": 200.0},
        "with_predbat": {"cost_p": 5000.0, "import_kwh": 230.0, "export_kwh": 30.0, "pv_generated_kwh": 100.0, "battery_throughput_kwh": 120.0, "export_credit_p_estimate": 450.0},
    }
    return {
        "year": 2025,
        "config": {},
        "months": [
            {"month": 3, "status": "degraded", "days": 31, "sampled_days": ["2025-03-10"], "failed_days": ["2025-03-24"], "standing_charge_p": 1860.0, "scenarios": scenarios},
        ],
        "annual": {
            "scenarios": scenarios,
            "standing_charge_p": 1860.0,
            "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": 2000.0},
            "months_included": 1,
            "months_excluded": [],
        },
        "caveats": [],
    }


def test_annual_cli(my_predbat):
    """Verify the table output reports every month, including excluded ones."""
    failed = False
    print("**** Testing annual CLI output ****")

    table = format_table(sample_results())

    print("Test: the table names the year and every scenario")
    for fragment in ["2025", "No PV/Battery", "Without Predbat", "With Predbat"]:
        if fragment not in table:
            print("  ERROR: the table should mention '{}'".format(fragment))
            failed = True

    print("Test: an unavailable month is shown as excluded, never as zero")
    if "unavailable" not in table.lower():
        print("  ERROR: the table must state that February was unavailable")
        failed = True
    if "no rate data available" not in table:
        print("  ERROR: the table should state why the month was excluded")
        failed = True

    print("Test: annual savings appear")
    if "Savings" not in table:
        print("  ERROR: the table should include a savings section")
        failed = True

    print("Test: caveats are printed rather than buried in the JSON")
    if "An example caveat." not in table:
        print("  ERROR: caveats must be shown to the user")
        failed = True

    print("Test: the excluded-month count is stated alongside the annual totals")
    if "1 of 12" not in table:
        print("  ERROR: the table should state how many months are included, got:\n{}".format(table))
        failed = True

    print("Test: the export credit line warns it is already included in cost, not additional income")
    if "export credit" not in table.lower():
        print("  ERROR: the table should show an export credit line, got:\n{}".format(table))
        failed = True
    if "already included" not in table.lower():
        print("  ERROR: the export credit line must warn it is already counted inside cost, to stop it being double-counted, got:\n{}".format(table))
        failed = True

    print("Test: a degraded month (some sampled days failed) is costed and included, not treated as unavailable")
    degraded_table = format_table(sample_results_with_degraded_month())
    if "unavailable" in degraded_table.lower():
        print("  ERROR: a degraded month must not be rendered as unavailable, got:\n{}".format(degraded_table))
        failed = True
    if "£90.00" not in degraded_table:
        print("  ERROR: the degraded month's no_pvbat cost (9000p = £90.00) should still be rendered, got:\n{}".format(degraded_table))
        failed = True
    if "Excluded months" in degraded_table:
        print("  ERROR: a degraded month must not appear as excluded, got:\n{}".format(degraded_table))
        failed = True
    if "1 of 12" not in degraded_table:
        print("  ERROR: the degraded month should still count towards months_included, got:\n{}".format(degraded_table))
        failed = True
    if "degraded" not in degraded_table.lower():
        print("  ERROR: the table should signal that the month came from fewer samples than planned, got:\n{}".format(degraded_table))
        failed = True

    print("Test: when no month is usable, the table does not fabricate a zero-cost year")
    empty_table = format_table(sample_results_no_usable_month())
    if "0 of 12" not in empty_table:
        print("  ERROR: the table should state 0 of 12 months were used, got:\n{}".format(empty_table))
        failed = True
    if "Savings" in empty_table:
        print("  ERROR: the table must not print a savings section when there is no annual total")
        failed = True
    if "0.00" in empty_table:
        print("  ERROR: the table must not render the missing annual total as a zero cost, got:\n{}".format(empty_table))
        failed = True

    print("Test: --quiet suppresses only progress output, never AnnualPredictor's warnings")
    # Regression guard: --quiet used to pass log=lambda *a, **k: None, silencing the
    # P10-fallback/missing-rate/failed-day/car-shortfall warnings along with progress, which
    # broke the "failures are visible, never silent" contract. main() must still pass
    # log=print through under --quiet, suppressing only make_progress()'s per-month lines.
    original_predictor = annual_cli.AnnualPredictor
    annual_cli.AnnualPredictor = _StubPredictor
    with tempfile.TemporaryDirectory() as work_dir:
        config_path = os.path.join(work_dir, "annual.yaml")
        with open(config_path, "w") as handle:
            handle.write("annual: {}\n")
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                annual_cli.main(["--config", config_path, "--work-dir", os.path.join(work_dir, "work"), "--quiet"])
        finally:
            annual_cli.AnnualPredictor = original_predictor

    if _StubPredictor.captured_log is not print:
        print("  ERROR: --quiet should still construct AnnualPredictor with log=print, got {}".format(_StubPredictor.captured_log))
        failed = True

    print("Test: storage_factory defaults to local files and is called with (work_dir, log)")
    # The default must keep the command line behaving exactly as it did before the factory
    # existed, so a plain run is still backed by StorageLocalFiles rooted at --work-dir.
    original_predictor = annual_cli.AnnualPredictor
    annual_cli.AnnualPredictor = _StubPredictor
    with tempfile.TemporaryDirectory() as work_dir:
        config_path = os.path.join(work_dir, "annual.yaml")
        with open(config_path, "w") as handle:
            handle.write("annual: {}\n")
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                annual_cli.main(["--config", config_path, "--work-dir", os.path.join(work_dir, "work")])
        finally:
            annual_cli.AnnualPredictor = original_predictor

    if not isinstance(_StubPredictor.captured_storage, StorageLocalFiles):
        print("  ERROR: the default storage should be StorageLocalFiles, got {!r}".format(_StubPredictor.captured_storage))
        failed = True

    print("Test: a supplied storage_factory replaces it, receiving the work dir and log")
    captured_args = {}

    class _StubStorage:
        """Records what main() hands the factory, standing in for a non-file backend."""

    def _factory(work_dir, log):
        captured_args["work_dir"] = work_dir
        captured_args["log"] = log
        return _StubStorage()

    original_predictor = annual_cli.AnnualPredictor
    annual_cli.AnnualPredictor = _StubPredictor
    with tempfile.TemporaryDirectory() as work_dir:
        config_path = os.path.join(work_dir, "annual.yaml")
        with open(config_path, "w") as handle:
            handle.write("annual: {}\n")
        expected_work = os.path.join(work_dir, "work")
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                annual_cli.main(["--config", config_path, "--work-dir", expected_work], storage_factory=_factory)
        finally:
            annual_cli.AnnualPredictor = original_predictor

    if not isinstance(_StubPredictor.captured_storage, _StubStorage):
        print("  ERROR: the supplied factory's storage should reach AnnualPredictor, got {!r}".format(_StubPredictor.captured_storage))
        failed = True
    if captured_args.get("work_dir") != expected_work:
        print("  ERROR: the factory should receive --work-dir, got {!r}".format(captured_args.get("work_dir")))
        failed = True
    if captured_args.get("log") is not print:
        print("  ERROR: the factory should receive the run's log callable, got {!r}".format(captured_args.get("log")))
        failed = True

    return failed


def test_annual_cli_fast_flag(my_predbat):
    """--fast sets annual.fast_mode on either config form, and is absent by default.

    Exercises apply_cli_overrides (fast=True/False) rather than a standalone
    apply_fast_override: that function existed only to set fast_mode before
    apply_cli_overrides took over every CLI flag, was left calling only from tests once
    main() switched over, and duplicated the exact two shape guards apply_cli_overrides
    already has to hold for --months, --year and --export-compare - so it was deleted and
    its coverage folded in here instead of kept as a second, narrower path through the
    same two guards.
    """
    failed = False
    print("**** Testing annual CLI --fast flag ****")

    print("Test: --fast sets fast_mode on the wrapped config form")
    config = {"annual": {"location": {"postcode": "SW1A 1AA"}}}
    result = annual_cli.apply_cli_overrides(config, fast=True)
    if result["annual"].get("fast_mode") is not True:
        print("  ERROR: --fast should set annual.fast_mode True, got {!r}".format(result["annual"].get("fast_mode")))
        failed = True

    print("Test: --fast sets fast_mode on the bare inner form")
    # validate_config accepts either shape (raw = config.get("annual", config)), so the
    # override has to reach the same mapping validate_config will read.
    config = {"location": {"postcode": "SW1A 1AA"}}
    result = annual_cli.apply_cli_overrides(config, fast=True)
    if result.get("fast_mode") is not True:
        print("  ERROR: --fast should set fast_mode on an unwrapped config, got {!r}".format(result.get("fast_mode")))
        failed = True

    print("Test: without --fast nothing is added")
    config = {"annual": {"location": {"postcode": "SW1A 1AA"}}}
    result = annual_cli.apply_cli_overrides(config, fast=False)
    if "fast_mode" in result["annual"]:
        print("  ERROR: fast_mode must not be injected when --fast was not given")
        failed = True

    print("Test: a config that is not a mapping is passed through untouched")
    # An empty YAML file loads as None and a malformed one as a list or a string. Any of
    # those must reach validate_config, which explains the problem, rather than dying here
    # with a bare TypeError - which is what --fast used to do on an empty config file.
    # Value equality, not identity: apply_cli_overrides makes a deep copy of its input
    # unconditionally before checking its shape (unlike the deleted apply_fast_override,
    # which checked first), so a mutable non-mapping like [] comes back as an equal but
    # distinct object - only the value is the contract here.
    for broken in (None, [], "not a config", 42):
        try:
            result = annual_cli.apply_cli_overrides(broken, fast=True)
        except Exception as error:  # noqa: BLE001 - the whole point is that nothing raises
            print("  ERROR: apply_cli_overrides({!r}, fast=True) raised {}: {}".format(broken, type(error).__name__, error))
            failed = True
            continue
        if result != broken:
            print("  ERROR: a non-mapping config should be returned unchanged, got {!r} for {!r}".format(result, broken))
            failed = True

    return failed


def test_annual_cli_machine(my_predbat):
    """Verify machine mode emits JSON progress on stderr and nothing human on stdout."""
    import io
    import json
    import sys

    failed = False
    print("**** Testing annual CLI machine mode ****")

    print("Test: machine progress writes one JSON object per line to stderr")
    captured = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = captured
    try:
        progress = make_progress(quiet=False, machine=True)
        progress(3, 12, "Month 03/2025")
    finally:
        sys.stderr = original_stderr

    line = captured.getvalue().strip()
    try:
        parsed = json.loads(line)
    except ValueError:
        print("  ERROR: machine progress should be JSON, got {!r}".format(line))
        parsed = {}
        failed = True
    if parsed.get("completed") != 3 or parsed.get("total") != 12 or parsed.get("message") != "Month 03/2025":
        print("  ERROR: unexpected progress payload {}".format(parsed))
        failed = True

    print("Test: human progress is unchanged when machine mode is off")
    captured = io.StringIO()
    sys.stderr = captured
    try:
        progress = make_progress(quiet=False, machine=False)
        progress(3, 12, "Month 03/2025")
    finally:
        sys.stderr = original_stderr
    if "[3/12]" not in captured.getvalue():
        print("  ERROR: expected the human form, got {!r}".format(captured.getvalue()))
        failed = True

    print("Test: quiet still suppresses progress in both modes")
    if make_progress(quiet=True, machine=False) is not None:
        print("  ERROR: quiet should give no progress callback")
        failed = True
    if make_progress(quiet=True, machine=True) is not None:
        print("  ERROR: quiet should give no progress callback in machine mode either")
        failed = True

    return failed


def test_annual_cli_machine_end_to_end(my_predbat):
    """Verify main() itself, not just make_progress(), keeps stdout pure JSON under --machine.

    Two checks that unit-testing make_progress() alone cannot make:

    1. A real subprocess invocation with a config that fails validation before
       predictor.run() is ever reached - confirming exit code, empty stdout and a readable
       stderr message hold end-to-end, not just when main() is called in-process.
    2. An in-process call to main() with AnnualPredictor stubbed to log a warning from inside
       run() (mirroring a P10 fallback or similar engine warning) - confirming that warning
       lands on stderr and never on stdout, and that stdout still parses as exactly one JSON
       object. This is the actual regression risk: a stray print() reachable only once
       predictor.run() executes, which a bad-config-only check cannot exercise.
    """
    import io
    import json
    import subprocess
    import sys

    failed = False
    print("**** Testing annual CLI machine mode end-to-end (main()) ****")

    print("Test: a real subprocess run with --machine and a bad config emits nothing on stdout")
    with tempfile.TemporaryDirectory() as work_dir:
        config_path = os.path.join(work_dir, "bad.yaml")
        with open(config_path, "w") as handle:
            handle.write("annual: {}\n")
        completed = subprocess.run(
            [sys.executable, annual_cli.__file__, "--config", config_path, "--work-dir", os.path.join(work_dir, "work"), "--machine"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    if completed.returncode != 2:
        print("  ERROR: expected exit code 2 for a config error, got {}".format(completed.returncode))
        failed = True
    if completed.stdout != "":
        print("  ERROR: stdout must be empty on a config error, got {!r}".format(completed.stdout))
        failed = True
    if "annual.location" not in completed.stderr:
        print("  ERROR: expected a readable config error on stderr, got {!r}".format(completed.stderr))
        failed = True

    print("Test: a warning logged mid-run() lands on stderr, never on stdout, alongside clean JSON")
    original_predictor = annual_cli.AnnualPredictor
    annual_cli.AnnualPredictor = _WarningPredictor
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as work_dir:
            config_path = os.path.join(work_dir, "annual.yaml")
            with open(config_path, "w") as handle:
                handle.write("annual: {}\n")
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                exit_code = annual_cli.main(["--config", config_path, "--work-dir", os.path.join(work_dir, "work"), "--machine"])
    finally:
        annual_cli.AnnualPredictor = original_predictor

    if exit_code != 0:
        print("  ERROR: a successful run should exit 0, got {}".format(exit_code))
        failed = True

    stdout_text = stdout_capture.getvalue()
    if "stub P10 fallback warning" in stdout_text:
        print("  ERROR: the engine's warning leaked onto stdout, got {!r}".format(stdout_text))
        failed = True
    try:
        parsed = json.loads(stdout_text)
    except ValueError:
        print("  ERROR: stdout should be exactly one JSON object even when the engine logs mid-run, got {!r}".format(stdout_text))
        parsed = None
        failed = True
    if parsed is not None and parsed != sample_results():
        print("  ERROR: stdout's JSON should match the results document exactly, got {}".format(parsed))
        failed = True

    if "stub P10 fallback warning" not in stderr_capture.getvalue():
        print("  ERROR: the engine's warning should still be visible, on stderr, got {!r}".format(stderr_capture.getvalue()))
        failed = True

    return failed


def test_annual_cli_export_compare_flags(my_predbat):
    """--months, --year and --export-compare override the config file."""
    failed = False
    import annual_cli
    from annual import AnnualConfigError

    print("Test: --months sets annual.months")
    config = {"annual": {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}}}
    merged = annual_cli.apply_cli_overrides(config, months="7", export_compare=False, fast=False)
    if merged["annual"]["months"] != [7]:
        print("  ERROR: --months 7 should set annual.months to [7], got {}".format(merged["annual"].get("months")))
        failed = True

    print("Test: --months accepts a comma-separated list")
    merged = annual_cli.apply_cli_overrides(config, months="6,7", export_compare=False, fast=False)
    if merged["annual"]["months"] != [6, 7]:
        print("  ERROR: --months 6,7 should set [6, 7], got {}".format(merged["annual"].get("months")))
        failed = True

    print("Test: --year sets annual.year, overriding whatever the config file had")
    year_config = {"annual": {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}, "year": 2020}}
    merged = annual_cli.apply_cli_overrides(year_config, year="2026", export_compare=False, fast=False)
    if merged["annual"]["year"] != 2026:
        print("  ERROR: --year 2026 should set annual.year to 2026 (an int), got {!r}".format(merged["annual"].get("year")))
        failed = True

    print("Test: --year sets annual.year on the bare inner form too")
    bare_year_config = {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}}
    merged = annual_cli.apply_cli_overrides(bare_year_config, year="2025")
    if merged.get("year") != 2025 or "annual" in merged:
        print("  ERROR: --year should land directly on the bare mapping, got {}".format(merged))
        failed = True

    print("Test: without --year the config's own year is left alone")
    merged = annual_cli.apply_cli_overrides(year_config, export_compare=False, fast=False)
    if merged["annual"]["year"] != 2020:
        print("  ERROR: omitting --year must not touch annual.year, got {!r}".format(merged["annual"].get("year")))
        failed = True

    print("Test: a malformed --year raises AnnualConfigError, not a bare ValueError")
    try:
        annual_cli.apply_cli_overrides(config, year="not-a-year")
        print("  ERROR: --year not-a-year should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError:
        pass
    except Exception as error:  # noqa: BLE001 - the whole point is that it's AnnualConfigError, nothing else
        print("  ERROR: --year not-a-year raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True

    print("Test: --export-compare sets the sweep, sampling, sample count and disables fast mode")
    merged = annual_cli.apply_cli_overrides(config, months="7", export_compare=True, fast=False)
    annual = merged["annual"]
    if [entry["id"] for entry in annual.get("export_tariffs", [])] != ["outgoing_fixed", "outgoing_prime", "agile_outgoing"]:
        print("  ERROR: --export-compare should set the three Octopus export products, got {}".format(annual.get("export_tariffs")))
        failed = True
    if annual.get("sampling") != "weekday_spread":
        print("  ERROR: --export-compare should set sampling to weekday_spread, got {}".format(annual.get("sampling")))
        failed = True
    if annual.get("samples_per_month") != 5:
        print("  ERROR: --export-compare should set samples_per_month to 5, got {}".format(annual.get("samples_per_month")))
        failed = True
    if annual.get("fast_mode"):
        print("  ERROR: --export-compare must not leave fast_mode on")
        failed = True

    print("Test: no flags leaves the config untouched (wrapped shape, full equality)")
    # A weak "months/export_tariffs absent" check let a real regression through review (a
    # setdefault() that injected an empty "annual" key into a bare-shape config still passed
    # this weaker form) - asserting full equality against an independent deep copy catches
    # any unwanted key, not just the two this test happens to think to name.
    expected = copy.deepcopy(config)
    merged = annual_cli.apply_cli_overrides(config, months=None, export_compare=False, fast=False)
    if merged != expected:
        print("  ERROR: with no flags the wrapped config should be byte-for-byte unchanged, got {} (expected {})".format(merged, expected))
        failed = True

    print("Test: no flags leaves the config untouched (bare shape, full equality)")
    # validate_config accepts the bare inner mapping too (raw = config.get("annual", config)),
    # so the no-op path has to hold for that shape as well, not just the wrapped one above.
    bare_config = {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}}
    expected_bare = copy.deepcopy(bare_config)
    merged_bare = annual_cli.apply_cli_overrides(bare_config, months=None, export_compare=False, fast=False)
    if merged_bare != expected_bare:
        print("  ERROR: with no flags the bare-shape config should be byte-for-byte unchanged, got {} (expected {})".format(merged_bare, expected_bare))
        failed = True

    print("Test: overrides do not mutate the caller's config")
    original = {"annual": {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}}}
    annual_cli.apply_cli_overrides(original, months="7", export_compare=True, fast=False)
    if "months" in original["annual"]:
        print("  ERROR: apply_cli_overrides mutated the caller's config")
        failed = True

    return failed


def test_annual_cli_apply_cli_overrides_config_shapes(my_predbat):
    """apply_cli_overrides must hold its two config-shape guards for every flag, and --months must fail cleanly.

    Regression test for a bug in the first cut of the export-compare flags: that version of
    apply_cli_overrides used ``merged.setdefault("annual", {})`` unconditionally, which (a)
    raised a bare AttributeError on a non-mapping config (an empty/malformed YAML file loads
    as None, a list, or a bare string) and (b) injected a brand-new, near-empty "annual" key
    into a bare-shape config instead of writing into the mapping validate_config actually
    reads - silently discarding the rest of that config even with no flags at all.
    """
    failed = False
    import annual_cli

    print("Test: a non-dict config is returned untouched, not crashed on")
    # Checked with flags ON, since the bug this guards was a bare setdefault() call that ran
    # unconditionally before any flag was even consulted.
    for broken in (None, [], "not a config", 42):
        try:
            result = annual_cli.apply_cli_overrides(broken, months="7", export_compare=True, fast=True)
        except Exception as error:  # noqa: BLE001 - the whole point is that nothing raises
            print("  ERROR: apply_cli_overrides({!r}, ...) raised {}: {}".format(broken, type(error).__name__, error))
            failed = True
            continue
        if result != broken:
            print("  ERROR: a non-mapping config should be returned unchanged, got {!r} for {!r}".format(result, broken))
            failed = True

    print("Test: a bare-shape config with --months writes into the bare mapping, not a new nested 'annual' key")
    bare_config = {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}}
    merged = annual_cli.apply_cli_overrides(bare_config, months="7", export_compare=False, fast=False)
    if merged.get("months") != [7]:
        print("  ERROR: --months should land directly on the bare mapping, got {}".format(merged))
        failed = True
    if "annual" in merged:
        print("  ERROR: a bare-shape config must not gain a new nested 'annual' key, got {}".format(merged))
        failed = True
    if merged.get("location") != bare_config["location"]:
        print("  ERROR: the rest of the bare config must survive untouched, got {}".format(merged))
        failed = True

    print("Test: --export-compare on a bare-shape config also writes into the bare mapping")
    merged = annual_cli.apply_cli_overrides(bare_config, months=None, export_compare=True, fast=False)
    if "annual" in merged:
        print("  ERROR: --export-compare must not create a nested 'annual' key on a bare config, got {}".format(merged))
        failed = True
    if [entry["id"] for entry in merged.get("export_tariffs", [])] != ["outgoing_fixed", "outgoing_prime", "agile_outgoing"]:
        print("  ERROR: --export-compare should set export_tariffs directly on the bare mapping, got {}".format(merged.get("export_tariffs")))
        failed = True

    print("Test: --year on a bare-shape config also writes into the bare mapping")
    merged = annual_cli.apply_cli_overrides(bare_config, year="2024")
    if "annual" in merged:
        print("  ERROR: --year must not create a nested 'annual' key on a bare config, got {}".format(merged))
        failed = True
    if merged.get("year") != 2024:
        print("  ERROR: --year should set year directly on the bare mapping, got {}".format(merged.get("year")))
        failed = True

    print("Test: a malformed --months raises AnnualConfigError, not a bare ValueError")
    config = {"annual": {"location": {"latitude": 51.5, "longitude": -0.1}, "solar": [{"kwp": 5.0}], "load": {"annual_kwh": 3000}, "tariff": {"rates_import": [{"rate": 25.0}]}}}
    try:
        annual_cli.apply_cli_overrides(config, months="abc", export_compare=False, fast=False)
        print("  ERROR: --months abc should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError:
        pass
    except Exception as error:  # noqa: BLE001 - the whole point is that it's AnnualConfigError, nothing else
        print("  ERROR: --months abc raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True

    print("Test: a partially-malformed --months (e.g. '6,abc') also raises AnnualConfigError")
    try:
        annual_cli.apply_cli_overrides(config, months="6,abc", export_compare=False, fast=False)
        print("  ERROR: --months 6,abc should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError:
        pass
    except Exception as error:  # noqa: BLE001
        print("  ERROR: --months 6,abc raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True

    # A config that already carries its own annual.months, distinct from the [3, 4] used
    # elsewhere in this test, so the two cases below can tell "the override was rejected"
    # apart from "the override was silently ignored and the file's own months survived".
    config_with_months = {
        "annual": {
            "location": {"latitude": 51.5, "longitude": -0.1},
            "solar": [{"kwp": 5.0}],
            "load": {"annual_kwh": 3000},
            "tariff": {"rates_import": [{"rate": 25.0}]},
            "months": [3, 4],
        }
    }

    print("Test: --months '' raises AnnualConfigError naming --months, not a silent no-op")
    # A truthiness check (`if months:`) treats "" the same as the flag being absent
    # (argparse's None default for an omitted --months) and silently keeps the file's own
    # months - the user asked for an override and got none, with no message at all. `is not
    # None` distinguishes "provided but empty" from "not provided", so an empty override
    # attempt is rejected here instead of passing through as a no-op.
    try:
        annual_cli.apply_cli_overrides(config_with_months, months="", export_compare=False, fast=False)
        print("  ERROR: --months '' should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError as error:
        if "--months" not in str(error):
            print("  ERROR: the AnnualConfigError should name --months, got '{}'".format(error))
            failed = True
    except Exception as error:  # noqa: BLE001
        print("  ERROR: --months '' raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True

    print("Test: --months ',' (commas, no numbers) also raises AnnualConfigError naming --months, not annual.months")
    # Previously this parsed cleanly to annual["months"] = [] and was only caught later by
    # validate_config, surfacing as an "annual.months" config error - naming a section the
    # user never touched instead of the --months flag that actually caused it.
    try:
        annual_cli.apply_cli_overrides(config_with_months, months=",", export_compare=False, fast=False)
        print("  ERROR: --months ',' should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError as error:
        if "--months" not in str(error):
            print("  ERROR: the AnnualConfigError should name --months, got '{}'".format(error))
            failed = True
    except Exception as error:  # noqa: BLE001
        print("  ERROR: --months ',' raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True

    print("Test: --months omitted entirely (None) still leaves an existing annual.months untouched")
    merged = annual_cli.apply_cli_overrides(config_with_months, months=None, export_compare=False, fast=False)
    if merged["annual"]["months"] != [3, 4]:
        print("  ERROR: omitting --months must not touch an existing annual.months, got {!r}".format(merged["annual"].get("months")))
        failed = True

    print("Test: a malformed --year raises AnnualConfigError, not a bare ValueError")
    try:
        annual_cli.apply_cli_overrides(config, year="not-a-year")
        print("  ERROR: --year not-a-year should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError:
        pass
    except Exception as error:  # noqa: BLE001
        print("  ERROR: --year not-a-year raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True

    print("Test: --export-compare raises AnnualConfigError, not a bare KeyError, when the catalogue is missing an id")
    # export_compare_tariffs() itself raises a bare KeyError if a built-in tariff id it
    # expects (e.g. "outgoing_prime") has been renamed or removed from tariff_catalogue.py.
    # Patched here rather than mutating the real catalogue, which always has all three ids.
    original_export_compare_tariffs = annual_cli.export_compare_tariffs
    annual_cli.export_compare_tariffs = lambda: (_ for _ in ()).throw(KeyError("export-compare tariff ids missing from catalogue: outgoing_prime"))
    try:
        annual_cli.apply_cli_overrides(config, export_compare=True)
        print("  ERROR: a missing catalogue id should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError as error:
        if "outgoing_prime" not in str(error):
            print("  ERROR: the AnnualConfigError should name the missing id, got '{}'".format(error))
            failed = True
    except Exception as error:  # noqa: BLE001 - the whole point is that it's AnnualConfigError, nothing else
        print("  ERROR: a missing catalogue id raised a bare {} ('{}') instead of AnnualConfigError".format(type(error).__name__, error))
        failed = True
    finally:
        annual_cli.export_compare_tariffs = original_export_compare_tariffs

    print("Test: main() itself reports a bad --months cleanly (exit 2, readable stderr, no traceback) rather than a bare crash")
    with tempfile.TemporaryDirectory() as work_dir:
        config_path = os.path.join(work_dir, "annual.yaml")
        with open(config_path, "w") as handle:
            handle.write("annual: {}\n")
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        try:
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                exit_code = annual_cli.main(["--config", config_path, "--work-dir", os.path.join(work_dir, "work"), "--months", "abc"])
        except Exception as error:  # noqa: BLE001 - main() must never let this escape as a raw traceback
            print("  ERROR: main() let a bad --months escape as {}: {}".format(type(error).__name__, error))
            failed = True
            exit_code = None

    if exit_code is not None:
        if exit_code != 2:
            print("  ERROR: a bad --months should exit 2, got {}".format(exit_code))
            failed = True
        if stdout_capture.getvalue() != "":
            print("  ERROR: stdout must stay empty on a --months config error, got {!r}".format(stdout_capture.getvalue()))
            failed = True
        if "--months" not in stderr_capture.getvalue():
            print("  ERROR: the stderr message should name --months as the problem, got {!r}".format(stderr_capture.getvalue()))
            failed = True

    print("Test: main() itself reports a bad --year cleanly (exit 2, readable stderr, no traceback) rather than a bare crash")
    with tempfile.TemporaryDirectory() as work_dir:
        config_path = os.path.join(work_dir, "annual.yaml")
        with open(config_path, "w") as handle:
            handle.write("annual: {}\n")
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        try:
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                exit_code = annual_cli.main(["--config", config_path, "--work-dir", os.path.join(work_dir, "work"), "--year", "abc"])
        except Exception as error:  # noqa: BLE001 - main() must never let this escape as a raw traceback
            print("  ERROR: main() let a bad --year escape as {}: {}".format(type(error).__name__, error))
            failed = True
            exit_code = None

    if exit_code is not None:
        if exit_code != 2:
            print("  ERROR: a bad --year should exit 2, got {}".format(exit_code))
            failed = True
        if stdout_capture.getvalue() != "":
            print("  ERROR: stdout must stay empty on a --year config error, got {!r}".format(stdout_capture.getvalue()))
            failed = True
        if "--year" not in stderr_capture.getvalue():
            print("  ERROR: the stderr message should name --year as the problem, got {!r}".format(stderr_capture.getvalue()))
            failed = True

    print("Test: main() itself reports a missing export-compare catalogue id cleanly (exit 2, readable stderr, no traceback) rather than a bare KeyError")
    original_export_compare_tariffs = annual_cli.export_compare_tariffs
    annual_cli.export_compare_tariffs = lambda: (_ for _ in ()).throw(KeyError("export-compare tariff ids missing from catalogue: outgoing_prime"))
    try:
        with tempfile.TemporaryDirectory() as work_dir:
            config_path = os.path.join(work_dir, "annual.yaml")
            with open(config_path, "w") as handle:
                handle.write("annual: {}\n")
            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()
            try:
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    exit_code = annual_cli.main(["--config", config_path, "--work-dir", os.path.join(work_dir, "work"), "--export-compare"])
            except Exception as error:  # noqa: BLE001 - main() must never let this escape as a raw traceback
                print("  ERROR: main() let a missing catalogue id escape as {}: {}".format(type(error).__name__, error))
                failed = True
                exit_code = None

        if exit_code is not None:
            if exit_code != 2:
                print("  ERROR: a missing catalogue id should exit 2, got {}".format(exit_code))
                failed = True
            if stdout_capture.getvalue() != "":
                print("  ERROR: stdout must stay empty on an --export-compare config error, got {!r}".format(stdout_capture.getvalue()))
                failed = True
            if "outgoing_prime" not in stderr_capture.getvalue():
                print("  ERROR: the stderr message should name the missing id, got {!r}".format(stderr_capture.getvalue()))
                failed = True
    finally:
        annual_cli.export_compare_tariffs = original_export_compare_tariffs

    return failed


def test_annual_cli_export_compare_table(my_predbat):
    """format_table renders a per-tariff table and a comparison summary for a sweep."""
    failed = False
    from annual_cli import format_table

    def scenarios(with_predbat_p, without_predbat_p):
        """Build a minimal four-scenario block with the two figures the summary uses."""
        blank = {"cost_p": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "battery_cycles": 0.0, "export_credit_p_estimate": 0.0}
        return {
            "no_pvbat": dict(blank, cost_p=10000.0),
            "pv_only": dict(blank, cost_p=8000.0),
            "without_predbat": dict(blank, cost_p=without_predbat_p),
            "with_predbat": dict(blank, cost_p=with_predbat_p),
        }

    def tariff_block(name, with_p, without_p):
        """Build one by_export entry covering a single month."""
        return {
            "name": name,
            "annual": {
                "scenarios": scenarios(with_p, without_p),
                "standing_charge_p": 1550.0,
                "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": without_p - with_p},
                "months_included": 1,
                "months_excluded": [],
                "costs": {},
                "payback": {},
            },
            "months": [{"month": 7, "status": "ok", "scenarios": scenarios(with_p, without_p)}],
        }

    results = {
        "year": 2026,
        "months_requested": [7],
        "by_export": {
            "outgoing_fixed": tariff_block("Octopus Outgoing Fixed", 5000.0, 6000.0),
            "outgoing_prime": tariff_block("Octopus Outgoing Prime", 4200.0, 6400.0),
            "agile_outgoing": tariff_block("Octopus Agile Outgoing", 4800.0, 5800.0),
        },
        "caveats": ["a caveat"],
    }

    print("Test: every tariff appears in the rendered table")
    text = format_table(results)
    for name in ("Octopus Outgoing Fixed", "Octopus Outgoing Prime", "Octopus Agile Outgoing"):
        if name not in text:
            print("  ERROR: '{}' missing from the rendered sweep table".format(name))
            failed = True

    print("Test: the summary names the with-Predbat winner")
    # Prime is cheapest with Predbat (4200p) though NOT without it (6400p is the worst).
    if "Octopus Outgoing Prime" not in text.split("Best with Predbat")[-1][:120]:
        print("  ERROR: the summary should name Outgoing Prime as the with-Predbat winner")
        failed = True

    print("Test: the summary names the without-Predbat winner separately")
    if "Best without Predbat" not in text:
        print("  ERROR: the summary should also report the without-Predbat winner")
        failed = True

    print("Test: caveats still render")
    if "a caveat" not in text:
        print("  ERROR: caveats should render for a sweep document too")
        failed = True

    print("Test: a single-tariff document is unaffected")
    legacy = {
        "year": 2025,
        "annual": {
            "scenarios": scenarios(5000.0, 6000.0),
            "standing_charge_p": 1550.0,
            "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": 1000.0},
            "months_included": 12,
            "months_excluded": [],
            "costs": {},
            "payback": {},
        },
        "months": [{"month": month, "status": "ok", "scenarios": scenarios(5000.0, 6000.0)} for month in range(1, 13)],
        "caveats": [],
    }
    legacy_text = format_table(legacy)
    if "Best with Predbat" in legacy_text:
        print("  ERROR: the sweep summary leaked into a single-tariff document")
        failed = True
    if "Annual prediction for 2025" not in legacy_text:
        print("  ERROR: the single-tariff table header changed")
        failed = True

    print("Test: a net-credit annual cost renders sign-outside ('-£X'), not sign-inside ('£-X'), in the comparison summary")
    # A July export-heavy tariff can legitimately cost less than nothing over the year (net
    # credit) - the common case for an export-tariff sweep, not an edge case. _format_pence
    # alone renders a negative value with the sign INSIDE the currency symbol ("£-57.13");
    # the summary's With/Without columns must get the same sign-outside treatment
    # (_format_pence_delta) the Saving column already had. Scoped to the comparison summary
    # section only - the per-tariff card tables above it deliberately still use _format_pence
    # unchanged, so checking the whole rendered text would wrongly fail on that section.
    net_credit_results = {
        "year": 2026,
        "months_requested": [7],
        "by_export": {"net_credit": tariff_block("Net Credit Tariff", -5713.0, -2000.0)},
        "caveats": [],
    }
    net_credit_text = format_table(net_credit_results)
    comparison_section = net_credit_text.split("Export tariff comparison", 1)[-1]
    if "-£57.13" not in comparison_section:
        print("  ERROR: expected '-£57.13' (sign outside) for a -5713p With-Predbat cost in the comparison summary, got:\n{}".format(comparison_section))
        failed = True
    if "£-57.13" in comparison_section:
        print("  ERROR: found '£-57.13' (sign inside) in the comparison summary - With/Without must use _format_pence_delta like Saving does")
        failed = True
    if "-£20.00" not in comparison_section:
        print("  ERROR: expected '-£20.00' (sign outside) for a -2000p Without-Predbat cost in the comparison summary, got:\n{}".format(comparison_section))
        failed = True
    if "£-20.00" in comparison_section:
        print("  ERROR: found '£-20.00' (sign inside) in the comparison summary")
        failed = True

    return failed


def test_annual_cli_export_compare_table_partial_failure(my_predbat):
    """A card with no usable annual result (scenarios: None) must not crash the sweep summary.

    Regression test for a review finding: each swept tariff is planned independently against
    its own rate downloads, so one product can produce annual["scenarios"] = None (the
    documented "nothing usable" shape - see test_annual_results.py) while the other two
    succeed. The comparison summary must skip that one card's ranking, but still name it
    with an explicit failure marker rather than silently dropping it - a reader must be able
    to tell "this tariff failed" apart from "this tariff wasn't in the sweep".
    """
    failed = False
    from annual_cli import format_table

    def scenarios(with_predbat_p, without_predbat_p):
        """Build a minimal four-scenario block with the two figures the summary uses."""
        blank = {"cost_p": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "battery_cycles": 0.0, "export_credit_p_estimate": 0.0}
        return {
            "no_pvbat": dict(blank, cost_p=10000.0),
            "pv_only": dict(blank, cost_p=8000.0),
            "without_predbat": dict(blank, cost_p=without_predbat_p),
            "with_predbat": dict(blank, cost_p=with_predbat_p),
        }

    def ok_tariff_block(name, with_p, without_p):
        """Build a by_export entry that planned successfully."""
        return {
            "name": name,
            "annual": {
                "scenarios": scenarios(with_p, without_p),
                "standing_charge_p": 1550.0,
                "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": without_p - with_p},
                "months_included": 1,
                "months_excluded": [],
                "costs": {},
                "payback": {},
            },
            "months": [{"month": 7, "status": "ok", "scenarios": scenarios(with_p, without_p)}],
            "caveats": [],
        }

    def failed_tariff_block(name):
        """Mirror _build_results' documented 'nothing usable' shape (see test_annual_results.py)."""
        return {
            "name": name,
            "annual": {
                "scenarios": None,
                "standing_charge_p": None,
                "savings": {},
                "months_included": 0,
                "months_excluded": [7],
                "costs": {},
                "payback": {},
            },
            "months": [{"month": 7, "status": "unavailable", "reason": "no rate data available"}],
            "caveats": [],
        }

    results = {
        "year": 2026,
        "months_requested": [7],
        "by_export": {
            # Fixed wins with-Predbat (4800p); Agile wins without-Predbat (5800p) - two
            # distinct winners, so the ranking logic has to pick each independently rather
            # than happening to agree by coincidence.
            "outgoing_fixed": ok_tariff_block("Octopus Outgoing Fixed", 4800.0, 6000.0),
            "outgoing_prime": failed_tariff_block("Octopus Outgoing Prime"),
            "agile_outgoing": ok_tariff_block("Octopus Agile Outgoing", 5000.0, 5800.0),
        },
        "caveats": [],
    }

    print("Test: a failed middle card does not crash the sweep summary")
    try:
        text = format_table(results)
    except Exception as error:  # noqa: BLE001 - the whole point is that nothing raises
        print("  ERROR: format_table raised {} on a card with scenarios=None: {}".format(type(error).__name__, error))
        return True

    print("Test: the two usable tariffs still render and still rank")
    if "Octopus Outgoing Fixed" not in text or "Octopus Agile Outgoing" not in text:
        print("  ERROR: the usable tariffs should still appear")
        failed = True
    if "Best with Predbat:    {}".format("Octopus Outgoing Fixed") not in text:
        print("  ERROR: the cheaper usable tariff (Outgoing Fixed, 4800p with-Predbat) should still win with-Predbat, got:\n{}".format(text))
        failed = True
    if "Best without Predbat: {}".format("Octopus Agile Outgoing") not in text:
        print("  ERROR: the cheaper usable tariff without Predbat (Agile Outgoing, 5800p) should still win, got:\n{}".format(text))
        failed = True

    print("Test: the failed tariff is still named, with an explicit failure marker, not silently dropped")
    if "Octopus Outgoing Prime" not in text:
        print("  ERROR: the failed tariff must still be named in the comparison table")
        failed = True
    if "no usable result" not in text:
        print("  ERROR: the failed tariff should be marked as having no usable result, got:\n{}".format(text))
        failed = True

    return failed


def test_annual_cli_export_compare_table_baseline_fallback(my_predbat):
    """Without an 'outgoing_fixed' entry, the comparison anchors on the first tariff instead of crashing.

    Also covers the sign-formatting minor fix: Agile Outgoing (4800p with-Predbat) is worse
    than the anchor Outgoing Prime (4200p with-Predbat) here, so its "(vs baseline)" delta is
    genuinely negative - it must render as "-£6.00", not "£-6.00".
    """
    failed = False
    from annual_cli import format_table

    def scenarios(with_predbat_p, without_predbat_p):
        """Build a minimal four-scenario block with the two figures the summary uses."""
        blank = {"cost_p": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "battery_cycles": 0.0, "export_credit_p_estimate": 0.0}
        return {
            "no_pvbat": dict(blank, cost_p=10000.0),
            "pv_only": dict(blank, cost_p=8000.0),
            "without_predbat": dict(blank, cost_p=without_predbat_p),
            "with_predbat": dict(blank, cost_p=with_predbat_p),
        }

    def tariff_block(name, with_p, without_p):
        """Build one by_export entry covering a single month."""
        return {
            "name": name,
            "annual": {
                "scenarios": scenarios(with_p, without_p),
                "standing_charge_p": 1550.0,
                "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": without_p - with_p},
                "months_included": 1,
                "months_excluded": [],
                "costs": {},
                "payback": {},
            },
            "months": [{"month": 7, "status": "ok", "scenarios": scenarios(with_p, without_p)}],
            "caveats": [],
        }

    results = {
        "year": 2026,
        "months_requested": [7],
        # Deliberately no "outgoing_fixed" key: order[0] ("outgoing_prime") must become the
        # baseline instead of the lookup crashing or silently anchoring on nothing.
        "by_export": {
            "outgoing_prime": tariff_block("Octopus Outgoing Prime", 4200.0, 6400.0),
            "agile_outgoing": tariff_block("Octopus Agile Outgoing", 4800.0, 5800.0),
        },
        "caveats": [],
    }

    print("Test: a sweep without 'outgoing_fixed' does not crash and both tariffs still render")
    text = format_table(results)
    if "Octopus Outgoing Prime" not in text or "Octopus Agile Outgoing" not in text:
        print("  ERROR: both tariffs should still render, got:\n{}".format(text))
        failed = True

    print("Test: the non-first tariff's row is annotated against the first entry (order[0]), not a hardcoded id")
    if "vs Octopus Outgoing Prime" not in text:
        print("  ERROR: expected the Agile Outgoing row to compare against Outgoing Prime (order[0]), got:\n{}".format(text))
        failed = True

    print("Test: a negative 'vs baseline' delta renders as '-£6.00', not '£-6.00'")
    if "-£6.00" not in text:
        print("  ERROR: expected a '-£6.00' delta (Agile Outgoing is 600p worse with Predbat than the baseline), got:\n{}".format(text))
        failed = True
    if "£-6.00" in text:
        print("  ERROR: the sign must render outside the currency symbol, got:\n{}".format(text))
        failed = True

    return failed


def test_annual_cli_export_compare_table_months_requested_wording(my_predbat):
    """The 'Based on N of M months' line adapts to a sweep's months_requested, and stays 'of 12 months' when absent."""
    failed = False
    from annual_cli import format_table

    def scenarios(with_predbat_p, without_predbat_p):
        """Build a minimal four-scenario block with the two figures the summary uses."""
        blank = {"cost_p": 0.0, "import_kwh": 0.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "battery_cycles": 0.0, "export_credit_p_estimate": 0.0}
        return {
            "no_pvbat": dict(blank, cost_p=10000.0),
            "pv_only": dict(blank, cost_p=8000.0),
            "without_predbat": dict(blank, cost_p=without_predbat_p),
            "with_predbat": dict(blank, cost_p=with_predbat_p),
        }

    print("Test: a sweep's per-tariff table states 'of N requested month(s)', not 'of 12 months'")
    sweep_results = {
        "year": 2026,
        "months_requested": [7],
        "by_export": {
            "outgoing_fixed": {
                "name": "Octopus Outgoing Fixed",
                "annual": {
                    "scenarios": scenarios(5000.0, 6000.0),
                    "standing_charge_p": 1550.0,
                    "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": 1000.0},
                    "months_included": 1,
                    "months_excluded": [],
                    "costs": {},
                    "payback": {},
                },
                "months": [{"month": 7, "status": "ok", "scenarios": scenarios(5000.0, 6000.0)}],
                "caveats": [],
            },
        },
        "caveats": [],
    }
    text = format_table(sweep_results)
    if "Based on 1 of 1 requested month(s)." not in text:
        print("  ERROR: expected 'Based on 1 of 1 requested month(s).', got:\n{}".format(text))
        failed = True
    if "of 12 months" in text:
        print("  ERROR: a sweep card must not use the 'of 12 months' wording, got:\n{}".format(text))
        failed = True

    print("Test: a single-tariff document with no months_requested keeps 'of 12 months' verbatim")
    legacy = {
        "year": 2025,
        "annual": {
            "scenarios": scenarios(5000.0, 6000.0),
            "standing_charge_p": 1550.0,
            "savings": {"pv_battery_vs_none_p": 2000.0, "predbat_vs_baseline_p": 1000.0},
            "months_included": 12,
            "months_excluded": [],
            "costs": {},
            "payback": {},
        },
        "months": [{"month": month, "status": "ok", "scenarios": scenarios(5000.0, 6000.0)} for month in range(1, 13)],
        "caveats": [],
    }
    legacy_text = format_table(legacy)
    if "Based on 12 of 12 months." not in legacy_text:
        print("  ERROR: the legacy wording must be unchanged, got:\n{}".format(legacy_text))
        failed = True
    if "requested month" in legacy_text:
        print("  ERROR: the legacy single-tariff document must not gain sweep wording, got:\n{}".format(legacy_text))
        failed = True

    return failed

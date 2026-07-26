# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual prediction command line output."""

import io
import os
import tempfile
from contextlib import redirect_stderr, redirect_stdout

import annual_cli
from annual_cli import format_table, make_progress


class _StubPredictor:
    """Stands in for AnnualPredictor so main()'s argument wiring can be tested in isolation.

    Records the ``log`` callable it was constructed with rather than doing any real work,
    so a test can assert what main() actually passes through under --quiet without needing
    a real config, the network, or a headless PredBat instance.
    """

    captured_log = None

    def __init__(self, config, log=None, storage=None, work_dir=None):
        """Record the log callable and discard everything else."""
        _StubPredictor.captured_log = log

    async def run(self, progress=None):
        """Report one fake progress step (if asked) and return canned results."""
        if progress:
            progress(0, 1, "stub")
        return sample_results()


def sample_results():
    """Return a small results document covering an ok month and an unavailable one."""
    scenarios = {
        "no_pvbat": {"cost_p": 12000.0, "import_kwh": 400.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 0.0, "self_consumed_kwh": 0.0, "self_consumed_kwh_meaningful": True},
        "without_predbat": {"cost_p": 8000.0, "import_kwh": 300.0, "export_kwh": 20.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 90.0, "export_credit_p_estimate": 300.0, "self_consumed_kwh": 100.0, "self_consumed_kwh_meaningful": True},
        "with_predbat": {"cost_p": 6000.0, "import_kwh": 280.0, "export_kwh": 45.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 140.0, "export_credit_p_estimate": 675.0, "self_consumed_kwh": 75.0, "self_consumed_kwh_meaningful": True},
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
        "no_pvbat": {"cost_p": 9000.0, "import_kwh": 300.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 0.0, "self_consumed_kwh": 0.0, "self_consumed_kwh_meaningful": True},
        "without_predbat": {"cost_p": 7000.0, "import_kwh": 250.0, "export_kwh": 15.0, "pv_generated_kwh": 100.0, "battery_throughput_kwh": 80.0, "export_credit_p_estimate": 200.0, "self_consumed_kwh": 85.0, "self_consumed_kwh_meaningful": True},
        "with_predbat": {"cost_p": 5000.0, "import_kwh": 230.0, "export_kwh": 30.0, "pv_generated_kwh": 100.0, "battery_throughput_kwh": 120.0, "export_credit_p_estimate": 450.0, "self_consumed_kwh": 70.0, "self_consumed_kwh_meaningful": True},
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

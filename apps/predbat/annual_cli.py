#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Command line entry point for the annual prediction tool.

Usage:
    python3 annual_cli.py --config annual.yaml --out results.json
"""

import argparse
import asyncio
import calendar
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from annual import SCENARIO_KEYS, AnnualConfigError, AnnualPredictor  # noqa: E402
from storage import StorageLocalFiles  # noqa: E402

SCENARIO_LABELS = {"no_pvbat": "No PV/Battery", "without_predbat": "Without Predbat", "with_predbat": "With Predbat"}


def _format_pence(pence, currency):
    """Format a pence amount as an explicitly-labelled pounds figure.

    ``cost_p`` and friends are stored in pence throughout the results document.
    A bare "12.34" tells the reader nothing about the unit, so the default
    ``currency="p"`` renders the pounds equivalent with a leading "£" (e.g.
    "£12.34") rather than an unlabelled number. Any other ``currency`` value is
    appended as a label instead, so a caller adapting this for a non-GBP config
    still gets an unambiguous figure.
    """
    amount = pence / 100.0
    if currency == "p":
        return "£{:.2f}".format(amount)
    return "{:.2f} {}".format(amount, currency)


def format_table(results, currency="p"):
    """Render the results document as a human-readable table.

    Handles two cases the original design missed: a "degraded" month (some, but not
    all, of its sampled days failed) is still costed and included in the annual total,
    so it is rendered with its figures rather than being folded into the "unavailable"
    branch; and when no month produced a usable result at all, ``annual["scenarios"]``
    and ``annual["standing_charge_p"]`` are ``None`` and ``annual["savings"]`` is empty,
    so the totals/savings section is skipped entirely instead of dividing ``None`` or
    printing a fabricated zero-cost year.
    """
    lines = []
    lines.append("Annual prediction for {}".format(results["year"]))
    lines.append("")
    header = "{:<6}".format("Month") + "".join("{:>20}".format(SCENARIO_LABELS[key]) for key in SCENARIO_KEYS)
    lines.append(header)
    lines.append("-" * len(header))

    for entry in results["months"]:
        name = calendar.month_abbr[entry["month"]]
        if entry["status"] not in ("ok", "degraded"):
            lines.append("{:<6}{:>60}".format(name, "unavailable - {}".format(entry.get("reason", "unknown"))))
            continue
        row = "{:<6}".format(name)
        for key in SCENARIO_KEYS:
            row += "{:>20}".format(_format_pence(entry["scenarios"][key]["cost_p"], currency))
        if entry["status"] == "degraded":
            row += "  (degraded - some sampled days failed to plan)"
        lines.append(row)

    annual = results["annual"]
    lines.append("-" * len(header))

    if annual["scenarios"] is not None:
        total_row = "{:<6}".format("Year")
        for key in SCENARIO_KEYS:
            total_row += "{:>20}".format(_format_pence(annual["scenarios"][key]["cost_p"], currency))
        lines.append(total_row)
    else:
        lines.append("No annual total available: no month produced a usable result.")

    lines.append("")
    lines.append("Based on {} of 12 months.".format(annual["months_included"]))
    if annual["months_excluded"]:
        lines.append("Excluded months: {}".format(", ".join(calendar.month_abbr[month] for month in annual["months_excluded"])))

    if annual["scenarios"] is not None:
        lines.append("")
        lines.append("Savings")
        lines.append("  PV and battery vs no system: {}".format(_format_pence(annual["savings"].get("pv_battery_vs_none_p", 0.0), currency)))
        lines.append("  Predbat vs without Predbat:  {}".format(_format_pence(annual["savings"].get("predbat_vs_baseline_p", 0.0), currency)))
        lines.append("  Standing charge (all scenarios): {}".format(_format_pence(annual["standing_charge_p"], currency)))
        lines.append("  Export credit (with Predbat, estimate - already included in cost above): {}".format(_format_pence(annual["scenarios"]["with_predbat"]["export_credit_p_estimate"], currency)))

    if results.get("caveats"):
        lines.append("")
        lines.append("Caveats")
        for caveat in results["caveats"]:
            lines.append("  - {}".format(caveat))

    return "\n".join(lines)


def make_progress(quiet):
    """Return a progress callback that writes to stderr, or None when quiet."""
    if quiet:
        return None

    def progress(completed, total, message):
        """Report progress to stderr so stdout stays parseable."""
        sys.stderr.write("[{}/{}] {}\n".format(completed, total, message))
        sys.stderr.flush()

    return progress


def main(argv=None):
    """Parse arguments, run the projection, and write the results. Returns an exit code."""
    parser = argparse.ArgumentParser(description="Project a year of electricity costs using the Predbat engine")
    parser.add_argument("--config", required=True, help="Path to the annual prediction YAML config")
    parser.add_argument("--out", default=None, help="Write the results JSON to this path")
    parser.add_argument("--work-dir", default="./annual_work", help="Working directory for the headless Predbat instance and cache")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = parser.parse_args(argv)

    try:
        with open(args.config, "r") as handle:
            config = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        sys.stderr.write("Could not read config {}: {}\n".format(args.config, error))
        return 2

    storage = StorageLocalFiles(args.work_dir, print)

    try:
        # --quiet suppresses only the per-month progress lines (make_progress() below), never
        # the warnings AnnualPredictor.log emits: P10 fallbacks, missing rate data, failed
        # sample days and car-charging shortfalls must stay visible even in a quiet run, per
        # the "failures are visible, never silent" contract.
        predictor = AnnualPredictor(config, log=print, storage=storage, work_dir=args.work_dir)
        results = asyncio.run(predictor.run(progress=make_progress(args.quiet)))
    except AnnualConfigError as error:
        sys.stderr.write("Config error: {}\n".format(error))
        return 2

    exit_code = 0
    if args.out:
        try:
            with open(args.out, "w") as handle:
                json.dump(results, handle, indent=2)
            sys.stderr.write("Results written to {}\n".format(args.out))
        except (OSError, TypeError, ValueError) as error:
            # The projection just took several minutes to compute; a failed write to
            # --out must not throw the results away. The table below is still printed
            # and the failure is only reported through a non-zero exit code, so a
            # caller relying on --out to have succeeded is not fooled into thinking it did.
            sys.stderr.write("Could not write results to {}: {}\n".format(args.out, error))
            exit_code = 1

    print(format_table(results))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

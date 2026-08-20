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
import contextlib
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from annual import INCLUDED_STATUSES, SCENARIO_KEYS, AnnualConfigError, AnnualPredictor  # noqa: E402
from storage import StorageLocalFiles  # noqa: E402

SCENARIO_LABELS = {"no_pvbat": "No PV/Battery", "pv_only": "PV Only", "without_predbat": "Without Predbat", "with_predbat": "With Predbat"}


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
        if entry["status"] not in INCLUDED_STATUSES:
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


def _stderr_log(message):
    """Write a log message to stderr, so machine mode's stdout stays pure JSON.

    ``StorageLocalFiles`` and ``AnnualPredictor`` are normally given ``log=print``, which
    writes to stdout. Under ``--machine`` that would interleave plain-text warnings - P10
    fallbacks, missing rate data, failed sample days, car-charging shortfalls, postcode
    resolution notices - ahead of the final JSON document, so a parent reading stdout would
    see garbage-then-JSON and fail to parse it. Routing those same warnings to stderr instead
    keeps them visible without ever touching stdout.
    """
    sys.stderr.write("{}\n".format(message))
    sys.stderr.flush()


def make_progress(quiet, machine=False):
    """Return a progress callback writing to stderr, or None when quiet.

    Machine mode emits one JSON object per line so the parent process never has
    to parse prose - the human wording is free to change without breaking a
    caller. Progress always goes to stderr so stdout carries only the result,
    whichever mode is in use.
    """
    if quiet:
        return None

    if machine:

        def progress(completed, total, message):
            """Emit one JSON progress record to stderr."""
            sys.stderr.write(json.dumps({"completed": completed, "total": total, "message": message}) + "\n")
            sys.stderr.flush()

        return progress

    def progress(completed, total, message):
        """Report progress to stderr so stdout stays parseable."""
        sys.stderr.write("[{}/{}] {}\n".format(completed, total, message))
        sys.stderr.flush()

    return progress


def apply_fast_override(config, fast):
    """Set ``annual.fast_mode`` when --fast was given, on whichever config shape was loaded.

    ``validate_config`` accepts both the wrapped ({"annual": {...}}) and the bare inner
    mapping, so the override has to land on the same mapping it will read - writing to the
    outer dict of a wrapped config would be silently ignored.
    """
    if not fast:
        return config
    if not isinstance(config, dict):
        # An empty or malformed YAML file loads as None (or a list, or a bare string).
        # Returning it untouched lets validate_config raise its own actionable message
        # rather than this line failing first with a bare TypeError and a traceback.
        return config
    inner = config["annual"] if isinstance(config.get("annual"), dict) else config
    inner["fast_mode"] = True
    return config


def main(argv=None, storage_factory=StorageLocalFiles):
    """Parse arguments, run the projection, and write the results. Returns an exit code.

    ``storage_factory`` builds the ``StorageBase`` the run caches weather and tariff
    downloads through, and is called as ``storage_factory(work_dir, log)`` - exactly how
    ``StorageLocalFiles`` is constructed, so the default is that class itself and the
    command line behaves as it always has.

    It exists because ``StorageBase`` is already an abstraction (``annual_weather`` and
    ``annual_tariff`` only ever call ``self.storage.load``/``save``), but this entry point
    hard-coded the one implementation, so the only way to run the annual tool against a
    different backend was to fork the CLI. A caller embedding the tool in a long-lived
    service - where a per-process work dir means every process re-downloads the same
    immutable ERA5 and Octopus data, and nothing can be shared between them - can now pass
    a factory for their own backend and reuse everything else here, including the
    ``--machine`` stdout/stderr contract, which is the fiddly part to reimplement.
    """
    parser = argparse.ArgumentParser(description="Project a year of electricity costs using the Predbat engine")
    parser.add_argument("--config", required=True, help="Path to the annual prediction YAML config")
    parser.add_argument("--out", default=None, help="Write the results JSON to this path")
    parser.add_argument("--work-dir", default="./annual_work", help="Working directory for the headless Predbat instance and cache")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    parser.add_argument("--machine", action="store_true", help="Emit results as JSON on stdout and progress as JSON on stderr, for a calling process")
    parser.add_argument("--fast", action="store_true", help="Plan only four seasonal months and interpolate the rest (about 2.5x faster, monthly figures approximate)")
    args = parser.parse_args(argv)

    try:
        with open(args.config, "r") as handle:
            config = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        sys.stderr.write("Could not read config {}: {}\n".format(args.config, error))
        return 2

    config = apply_fast_override(config, args.fast)

    # Under --machine, the engine's log must go to stderr rather than the default print()
    # (stdout): predictor.run() can log warnings - P10 fallbacks, missing rate data, failed
    # sample days, car-charging shortfalls, postcode resolution - and any of those landing on
    # stdout ahead of the final json.dump() would corrupt the one-JSON-object contract a parent
    # process depends on. The default (non-machine) path keeps log=print unchanged.
    log = _stderr_log if args.machine else print

    storage = storage_factory(args.work_dir, log)

    # predictor.run() lazily imports the full Predbat engine (predbat.py) on its first call
    # to create_headless_predbat(); that module's top-level self-update check
    # (download.check_install()) writes plain text straight to real stdout via a bare
    # print(), bypassing the log callable entirely. Routing our own log calls to stderr
    # (above) cannot catch that, so in machine mode stdout itself is redirected to stderr for
    # the duration of construction and run() - any stray print(), from this code or anything
    # it pulls in, lands on stderr instead of corrupting the one-JSON-object stdout contract.
    # The messages stay visible, just on the correct stream; only json.dump() below writes to
    # the real stdout.
    stdout_guard = contextlib.redirect_stdout(sys.stderr) if args.machine else contextlib.nullcontext()
    try:
        with stdout_guard:
            # --quiet suppresses only the per-month progress lines (make_progress() below), never
            # the warnings AnnualPredictor.log emits: P10 fallbacks, missing rate data, failed
            # sample days and car-charging shortfalls must stay visible even in a quiet run, per
            # the "failures are visible, never silent" contract.
            predictor = AnnualPredictor(config, log=log, storage=storage, work_dir=args.work_dir)
            results = asyncio.run(predictor.run(progress=make_progress(args.quiet, machine=args.machine)))
    except AnnualConfigError as error:
        sys.stderr.write("Config error: {}\n".format(error))
        return 2

    exit_code = 0
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(results, handle, indent=2)
            # stderr, not stdout, so this confirmation is safe in both modes: --machine's
            # stdout purity only concerns stdout, and stderr already carries plain text
            # (config errors, progress) in machine mode.
            sys.stderr.write("Results written to {}\n".format(args.out))
        except (OSError, TypeError, ValueError) as error:
            # The projection just took several minutes to compute; a failed write to
            # --out must not throw the results away. The table (or JSON, in machine mode)
            # below is still emitted and the failure is only reported through a non-zero
            # exit code, so a caller relying on --out to have succeeded is not fooled into
            # thinking it did.
            sys.stderr.write("Could not write results to {}: {}\n".format(args.out, error))
            exit_code = 1

    if args.machine:
        # The parent reads exactly one JSON object from stdout; the human table would
        # corrupt it, so it is suppressed rather than merely reordered.
        json.dump(results, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(format_table(results))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

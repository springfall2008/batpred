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
import copy
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from annual import INCLUDED_STATUSES, SCENARIO_KEYS, AnnualConfigError, AnnualPredictor, config_warnings  # noqa: E402
from storage import StorageLocalFiles  # noqa: E402
from tariff_catalogue import export_compare_tariffs  # noqa: E402

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


def _format_pence_delta(pence, currency):
    """Format a pence DELTA (which may be negative) with the sign outside the currency label.

    ``_format_pence`` puts a negative amount's sign after the currency symbol - e.g.
    ``"£{:.2f}".format(-8.0)`` is ``"£-8.00"`` - which is fine for an absolute cost (never
    negative in practice) but reads oddly for a delta, which can genuinely go negative (a
    swept tariff that is worse than the baseline, or a "saving" that is actually a loss).
    Deliberately not fixed inside ``_format_pence`` itself, which every other caller in this
    module depends on rendering exactly as it always has.
    """
    if pence < 0:
        return "-{}".format(_format_pence(-pence, currency))
    return _format_pence(pence, currency)


def _format_single_table(results, currency):
    """Render a single-tariff results document as a human-readable table.

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
    months_requested = results.get("months_requested")
    if months_requested is not None:
        # Carried by any document built from an explicit annual.months subset - a plain
        # single-tariff run (AnnualPredictor._build_results) as well as a sweep card
        # (format_table threads its own run's months_requested through per tariff): a
        # deliberate single-month --months run must not read as "1 of 12 months" - that
        # phrasing implies eleven months failed, when in fact only one was ever asked for. A
        # full twelve month document never carries this key, so this branch never fires
        # there and the wording below is untouched.
        lines.append("Based on {} of {} requested month(s).".format(annual["months_included"], len(months_requested)))
    else:
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


def format_table(results, currency="p"):
    """Render the results document as a human-readable table.

    A sweep document (one produced with annual.export_tariffs) carries several tariffs under
    "by_export" and gets one table each plus a comparison summary. Anything else is a
    single-tariff document and renders exactly as it always has.
    """
    if "by_export" not in results:
        return _format_single_table(results, currency)

    lines = []
    for tariff_id, block in results["by_export"].items():
        lines.append("=== {} ({}) ===".format(block["name"], tariff_id))
        # This tariff's OWN caveats, not the run-wide list: a tariff whose rates were
        # synthesised must say so where its figures are, not in a shared footnote.
        # months_requested is threaded through from the run-wide document so a single
        # requested month is not misreported as "1 of 12" below (see _format_single_table).
        lines.append(
            _format_single_table(
                {"year": results["year"], "annual": block["annual"], "months": block["months"], "caveats": block.get("caveats", []), "months_requested": results.get("months_requested")},
                currency,
            )
        )
        lines.append("")

    lines.append("Export tariff comparison")
    lines.append("")
    header = "{:<32}{:>18}{:>18}{:>18}".format("Tariff", "With Predbat", "Without Predbat", "Predbat saving")
    lines.append(header)
    lines.append("-" * len(header))

    # Outgoing Fixed is the baseline the page quotes the other two against, so if it is in
    # the sweep it anchors the deltas. Falling back to the first entry keeps this working
    # for a sweep that does not include it, e.g. a hand-written CLI config.
    order = list(results["by_export"].keys())
    baseline_id = "outgoing_fixed" if "outgoing_fixed" in results["by_export"] else order[0]

    def cost(tariff_id, scenario):
        """Return one scenario's cost in pence for a tariff in the sweep, or None if that tariff produced no usable annual result.

        ``annual["scenarios"]`` is ``None`` when no month for this tariff planned
        successfully (``_build_results``' documented "nothing usable" case - see
        ``test_annual_results.py``). Each swept tariff is planned independently against its
        own rate downloads, so one product can fail this way while the other two succeed;
        returning ``None`` here (rather than raising) lets the row loop and the ranking below
        skip just that one card instead of the whole comparison blowing up.
        """
        scenarios = results["by_export"][tariff_id]["annual"]["scenarios"]
        if scenarios is None:
            return None
        return scenarios[scenario]["cost_p"]

    # Only tariffs with a usable annual result can be ranked or used as the delta anchor - a
    # card with none is still listed (below), just never picked as "best" or as the baseline
    # for a "(vs baseline)" delta.
    usable = [tariff_id for tariff_id in order if cost(tariff_id, "with_predbat") is not None]

    for tariff_id in order:
        block = results["by_export"][tariff_id]
        with_p = cost(tariff_id, "with_predbat")
        without_p = cost(tariff_id, "without_predbat")
        if with_p is None or without_p is None:
            # Rendered explicitly, not skipped: a reader must be able to tell "this tariff
            # failed" apart from "this tariff was never swept at all".
            lines.append("{:<32}{:>18}".format(block["name"], "no usable result - see its table above"))
            continue
        # _format_pence_delta, not _format_pence, for With/Without too: an annual cost_p can
        # legitimately go negative (a net-credit month/year - an export-heavy tariff in a
        # sunny month is the common case here, not an edge case), and _format_pence renders
        # that as the sign-inside-the-currency "£-57.13". _format_pence_delta puts the sign
        # outside ("-£57.13") - already used below for the Saving column, extended here to
        # the two absolute-cost columns since they can be just as negative as any delta.
        # _format_pence itself is left untouched: it is shared by every other caller in this
        # file, including the per-tariff tables above, and changing it would ripple there too.
        row = "{:<32}{:>18}{:>18}{:>18}".format(block["name"], _format_pence_delta(with_p, currency), _format_pence_delta(without_p, currency), _format_pence_delta(without_p - with_p, currency))
        baseline_with = cost(baseline_id, "with_predbat")
        if tariff_id != baseline_id and baseline_with is not None:
            row += "   ({} vs {})".format(_format_pence_delta(baseline_with - with_p, currency), results["by_export"][baseline_id]["name"])
        if block.get("rates_synthesised"):
            # Marked inline, not footnoted: a synthetic tariff compared against two real ones
            # is the difference between a useful answer and a misleading one. This is the
            # CARD-level bool (by_export[id]["rates_synthesised"]) - distinct from the
            # month-row-level "rates_synthesised", which is a list of sides (e.g. ["export"])
            # and is truthy for a different reason (see _synthesised_sides).
            row += "   [rates synthesised - not this month's real rates]"
        lines.append(row)

    lines.append("")
    if usable:
        best_with = min(usable, key=lambda tariff_id: cost(tariff_id, "with_predbat"))
        best_without = min(usable, key=lambda tariff_id: cost(tariff_id, "without_predbat"))
        lines.append("Best with Predbat:    {}".format(results["by_export"][best_with]["name"]))
        lines.append("Best without Predbat: {}".format(results["by_export"][best_without]["name"]))
        if best_with != best_without:
            lines.append("The best tariff DIFFERS with and without Predbat - optimisation is what makes {} the right choice.".format(results["by_export"][best_with]["name"]))
    else:
        lines.append("Best with Predbat:    no tariff in this sweep produced a usable result.")
        lines.append("Best without Predbat: no tariff in this sweep produced a usable result.")

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


def apply_cli_overrides(config, months=None, export_compare=False, fast=False, year=None):
    """Return a copy of `config` with the CLI flags applied over it.

    Flags win over the file. A copy, not a mutation: the caller's document is also what
    gets echoed back in --machine mode, and silently rewriting it would make the output
    disagree with the file the user passed.

    Every flag handled here clears the same two config-shape hurdles:

    - a non-mapping config (an empty or malformed YAML file loads as None, a list, or a
      bare string) is returned untouched, so validate_config raises its own actionable
      message rather than this function failing first with a bare AttributeError;
    - validate_config accepts either the wrapped ({"annual": {...}}) or the bare inner
      mapping form, so the overrides land on whichever mapping validate_config will
      actually read - writing them under a new top-level "annual" key on a bare-shape
      config would silently discard the rest of that config.

    Raises AnnualConfigError (never a bare ValueError, or a bare KeyError for
    --export-compare) if --months, --year or --export-compare cannot be resolved, so a
    malformed flag or a stale catalogue reaches the user the same way every other config
    problem does.

    --months is checked against `is not None` rather than truthiness, so an override
    attempt is any invocation that actually passed the flag - including `--months ""` -
    and only the flag being absent entirely (argparse's default) leaves the file's own
    months untouched. A truthiness check would treat "" the same as "absent" and silently
    keep the file's months, discarding an override the user did ask for with no message
    at all. Whatever the flag resolves to, an override that names zero month numbers
    (empty, or comma(s) with nothing between them) is rejected here rather than being
    written through as annual["months"] = [] - left to validate_config, that surfaces as
    an "annual.months" config error, naming a section the user never touched instead of
    the --months flag that actually caused it.
    """
    merged = copy.deepcopy(config)
    if not isinstance(merged, dict):
        return merged
    annual = merged["annual"] if isinstance(merged.get("annual"), dict) else merged

    if year is not None:
        try:
            annual["year"] = int(year)
        except (TypeError, ValueError):
            raise AnnualConfigError("--year must be a whole number, got '{}'".format(year))
    if months is not None:
        try:
            month_numbers = [int(part.strip()) for part in str(months).split(",") if part.strip()]
        except ValueError:
            raise AnnualConfigError("--months must be a comma-separated list of month numbers, got '{}'".format(months))
        if not month_numbers:
            raise AnnualConfigError("--months must name at least one month number, got '{}'".format(months))
        annual["months"] = month_numbers
    if fast:
        annual["fast_mode"] = True
    if export_compare:
        # These values are pinned to match what a hosted caller sends for the same
        # comparison, so one flag reproduces that run from the command line. If this flag
        # and that caller are ever changed independently, the two would drift apart and
        # the CLI would stop being a way to reproduce what was run there.
        try:
            annual["export_tariffs"] = export_compare_tariffs()
        except KeyError as error:
            # export_compare_tariffs() raises KeyError with a message naming the missing
            # catalogue ids (e.g. a built-in tariff renamed or removed from
            # tariff_catalogue.py). Re-raised as AnnualConfigError, same as a malformed
            # --months or --year above, so it reaches main() as a clean config error
            # rather than a bare traceback. error.args[0] is the plain message; str(error)
            # would wrap it in quotes, because KeyError's __str__ shows its argument as a
            # repr rather than as plain text.
            raise AnnualConfigError("--export-compare: {}".format(error.args[0]))
        annual["sampling"] = "weekday_spread"
        annual["samples_per_month"] = 5
        annual["fast_mode"] = False
    return merged


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
    parser.add_argument("--months", default=None, help="Plan only these months, as a comma-separated list (e.g. --months 7 or --months 6,7)")
    parser.add_argument("--year", default=None, help="Plan this year instead of the config file's default (the most recent complete calendar year)")
    parser.add_argument("--export-compare", action="store_true", dest="export_compare", help="Evaluate the three Octopus export tariffs against otherwise identical inputs (implies weekday-spread sampling, 5 samples a month, and no fast mode)")
    args = parser.parse_args(argv)

    try:
        with open(args.config, "r") as handle:
            config = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        sys.stderr.write("Could not read config {}: {}\n".format(args.config, error))
        return 2

    try:
        config = apply_cli_overrides(config, months=args.months, export_compare=args.export_compare, fast=args.fast, year=args.year)
    except AnnualConfigError as error:
        sys.stderr.write("Config error: {}\n".format(error))
        return 2

    # Under --machine, the engine's log must go to stderr rather than the default print()
    # (stdout): predictor.run() can log warnings - P10 fallbacks, missing rate data, failed
    # sample days, car-charging shortfalls, postcode resolution - and any of those landing on
    # stdout ahead of the final json.dump() would corrupt the one-JSON-object contract a parent
    # process depends on. The default (non-machine) path keeps log=print unchanged.
    log = _stderr_log if args.machine else print

    # The same sanity warnings the web form surfaces (e.g. a kWp figure that looks like
    # Watts were entered, GH#4858): a headless run must not be the one place a config
    # mistake is invisible. Through log rather than a stdout write, so --machine's
    # one-JSON-object stdout contract holds; machine mode already carries plain text on
    # stderr alongside the JSON progress lines.
    for warning in config_warnings(config):
        log("Warn: Annual: {}".format(warning))

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

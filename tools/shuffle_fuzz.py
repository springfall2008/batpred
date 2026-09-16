#!/usr/bin/env python3
"""Run the unit test suite repeatedly with shuffled test order, looking for shared-fixture
state-leak bugs between tests (see issue #5079) - failures that only reproduce in certain
orderings and would otherwise only be found by manual review.

Usage (from the coverage/ directory, after the usual setup.csh venv is set up):

    python3 ../tools/shuffle_fuzz.py
    python3 ../tools/shuffle_fuzz.py --runs 50 --quick
    python3 ../tools/shuffle_fuzz.py --seed 12345   # replay a whole fuzz session

Every run is seeded: the master seed (random unless --seed is given) is printed up front
so a whole session can be replayed, and each individual run invokes
unit_test.py --shuffle --shuffle-seed <N> so a failure it finds can be reproduced directly
with:

    ./run_all --shuffle --shuffle-seed <N>

A failing shuffle only tells you *an* ordering that triggers the leak, not which earlier
test caused it. --bisect narrows that down automatically:

    python3 ../tools/shuffle_fuzz.py --bisect 2282781232 [--quick] [--keyword PATTERN]

It replays that shuffle-seed to recover the exact test order and which test failed, then
delta-debugs the tests before the failure down to a minimal ordered subsequence that still
reproduces it (ideally just the poisoning test plus the victim).

A single bisection can be misleading - the delta-debugger finds *a* minimal reproducer for
*that* ordering, which might not be the most common leak, or might need an unrelated test
to happen to run between the culprit and victim for unrelated reasons. --campaign repeats
shuffle -> bisect as independent samples and tallies which (culprit(s), victim) pairs recur,
so the frequency table - not any single round - is the signal:

    python3 ../tools/shuffle_fuzz.py --campaign 10 --quick [--keyword PATTERN] [--seed N]

For "just find me the next problem pair" as a single command, --hunt samples shuffled runs
until it finds a failure that bisects to a (culprit, victim) pair not already recorded in
--known-pairs-file (default: ../tools/shuffle_fuzz_known_pairs.txt), prints its minimal repro, records
it, and stops - so running the exact same command again finds a *different* pair next time
instead of rediscovering the same handful:

    python3 ../tools/shuffle_fuzz.py --hunt --quick

A bisected pair is re-run on its own before being recorded. bisect_order() assumes the
failure it is narrowing is deterministic, and a test that fails for some other reason -
depending on the wall clock is the case seen in practice - breaks that assumption: the
search still returns a minimal subset, but its "culprit" is whichever test happened to
precede the victim rather than a cause. Failures that cannot be reduced to a pair that
reproduces are written to --log-dir as unreproducible_<victim>_<seed>.log instead of being
discarded, since a flaky or clock-dependent test is a real bug of a different kind.
"""

import argparse
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter

# A campaign round can spend 10+ minutes inside a single bisection with no output of its own
# (each trial shells out to unit_test.py and only reports back once that finishes) - force
# line buffering so progress prints reach a redirected log/file as they happen, not just at
# exit, so a long run can be tailed live instead of going dark.
sys.stdout.reconfigure(line_buffering=True)


def minimal_repro_cmd(candidates, victim):
    """Build the unit_test.py --test ... command to copy and run for a (candidates, victim) pair."""
    test_flags = " ".join(f"--test {name}" for name in list(candidates) + [victim])
    return f"python3 ../apps/predbat/unit_test.py {test_flags}"


RUNNING_RE = re.compile(r"\*\*\*\* Running: (\S+)")
SKIPPING_RE = re.compile(r"\*\*\*\* Skipping: (\S+)")
FAILED_RE = re.compile(r"\*\*\*\* (?:ERROR: Test (\S+) FAILED|(\S+): FAILED)")


def run_once(seed, quick, keyword, extra_args):
    """Run unit_test.py --shuffle --shuffle-seed <seed> and report whether it passed."""
    cmd = [sys.executable, "../apps/predbat/unit_test.py", "--shuffle", "--shuffle-seed", str(seed)]
    if quick:
        cmd.append("--quick")
    if keyword:
        cmd += ["-k", keyword]
    cmd += extra_args

    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start
    return result.returncode == 0, elapsed, result.stdout, result.stderr


def run_explicit(test_names, extra_args=()):
    """Run an exact, ordered list of tests via repeated --test flags (order is preserved,
    unlike --keyword/full-registry runs) and report whether it failed."""
    cmd = [sys.executable, "../apps/predbat/unit_test.py"]
    for name in test_names:
        cmd += ["--test", name]
    cmd += list(extra_args)

    start = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - start
    return result.returncode == 0, elapsed, result.stdout, result.stderr


def parse_run_order(stdout):
    """Recover the ordered sequence of test names a run actually attempted (Running lines
    only - Skipping lines were never executed) plus the name of the one that failed, if any.

    A "clean" failure prints its own FAILED line via the registry loop's own bookkeeping,
    but a test that raises an uncaught exception (the common case for a fixture state leak -
    e.g. an IndexError from stale shared state) kills the process with a traceback instead,
    and no FAILED line is ever printed. In that case the failing test is simply the last one
    whose "Running:" line was printed but which never got a matching PASSED/FAILED line."""
    order = []
    finished = set()
    for line in stdout.splitlines():
        match = RUNNING_RE.search(line)
        if match:
            order.append(match.group(1))
            continue
        if " PASSED in " in line or " FAILED in " in line:
            for name in order:
                if name not in finished and (line.startswith(f"**** {name}:") or line.startswith(f"**** Test {name} ") or line.startswith(f"**** ERROR: Test {name} ")):
                    finished.add(name)

    failed_name = None
    for line in stdout.splitlines():
        match = FAILED_RE.search(line)
        if match:
            failed_name = match.group(1) or match.group(2)
            break

    if failed_name is None and order:
        unfinished = [name for name in order if name not in finished]
        if unfinished:
            failed_name = unfinished[0]  # crashed mid-test: the last one started is the victim

    return order, failed_name


def bisect_single_culprit(candidates, reproduces, log):
    """Binary search assuming exactly one test among `candidates` is the culprit (the common
    case observed in practice - most failures so far bisect down to a single prior test, not
    a combination). At each step, try dropping one half; if the remaining half alone still
    reproduces, the culprit is in it and we recurse - O(log N) trials instead of delta-debug's
    O(N). Returns the single-element culprit list on success, or None if the assumption
    doesn't hold (neither half reproduces alone, implying a multi-test interaction) so the
    caller can fall back to full delta-debugging."""
    remaining = list(candidates)
    while len(remaining) > 1:
        mid = len(remaining) // 2
        first_half, second_half = remaining[:mid], remaining[mid:]
        if reproduces(second_half):
            log(f"**** Single-culprit search: culprit is among the last {len(second_half)} candidate(s) ****")
            remaining = second_half
        elif reproduces(first_half):
            log(f"**** Single-culprit search: culprit is among the first {len(first_half)} candidate(s) ****")
            remaining = first_half
        else:
            return None  # neither half alone reproduces - not a single isolated culprit
    return remaining if remaining and reproduces(remaining) else None


def bisect_order(order, failed_name, quiet=False):
    """Narrow the tests before failed_name (in order) down to a minimal ordered subsequence
    that still reproduces the failure. Returns (minimal_prefix, victim) on success, or None
    if the failure didn't reproduce via --test at all."""

    def log(msg):
        """Print msg unless quiet was requested (used by --campaign to suppress per-trial noise)."""
        if not quiet:
            print(msg)

    culprit_index = order.index(failed_name)
    candidates = order[:culprit_index]
    victim = failed_name
    log(f"**** Recovered order: {len(order)} test(s) attempted, failure at position {culprit_index + 1}: {victim} ****")
    log(f"**** {len(candidates)} test(s) ran before it - bisecting down to a minimal subsequence ****")

    def reproduces(subset):
        """Run subset + [victim] and report whether victim specifically is what failed.

        Not just any non-zero exit - otherwise an unrelated test in `subset` that happens to
        crash on its own (independent of poisoning victim) would look like a hit, and
        bisection would wrongly credit it as the culprit for a failure it never caused."""
        ok, _, stdout, _ = run_explicit(list(subset) + [victim])
        if ok:
            return False
        _, trial_failed_name = parse_run_order(stdout)
        return trial_failed_name == victim

    # Sanity check: the full prefix + victim must still reproduce before we start trimming.
    if not reproduces(candidates):
        log("**** Full prefix + victim did not reproduce via --test (order/threading side effects from the full run may matter) - cannot bisect further ****")
        return None

    # Try the cheap path first: most failures seen so far come down to a single prior test,
    # not a combination, so a plain binary search finds it in O(log N) trials. Only fall back
    # to the slower general delta-debug (which also catches multi-test interactions) if that
    # single-culprit assumption doesn't hold.
    single = bisect_single_culprit(candidates, reproduces, log)
    if single is not None:
        return single, victim
    log("**** No single prior test reproduces the failure alone - falling back to full delta-debugging (likely a multi-test interaction) ****")

    # Classic delta-debugging: repeatedly try removing chunks of the remaining candidates,
    # shrinking chunk size as progress stalls, until no single test can be removed.
    chunk_size = max(1, len(candidates) // 2)
    while chunk_size >= 1 and len(candidates) > 0:
        removed_something = False
        i = 0
        while i < len(candidates):
            trial = candidates[:i] + candidates[i + chunk_size :]
            if reproduces(trial):
                log(f"**** Dropped {chunk_size} test(s) at position {i}, {len(trial)} candidate(s) remain ****")
                candidates = trial
                removed_something = True
            else:
                i += chunk_size
        if not removed_something:
            chunk_size //= 2

    return candidates, victim


def bisect(shuffle_seed, quick, keyword):
    """CLI entry point for --bisect: replay shuffle_seed, bisect its failure, print the repro, and exit."""
    print(f"**** Bisecting shuffle-seed {shuffle_seed} ****")
    passed, elapsed, stdout, stderr = run_once(shuffle_seed, quick, keyword, [])
    if passed:
        print(f"**** shuffle-seed {shuffle_seed} did not fail (quick={quick}, keyword={keyword}) - nothing to bisect ****")
        sys.exit(1)

    order, failed_name = parse_run_order(stdout)
    if not failed_name or failed_name not in order:
        print("**** Could not parse the failing test name out of the run's output - aborting ****")
        print(stdout[-4000:])
        sys.exit(1)

    result = bisect_order(order, failed_name)
    if result is None:
        print(f"**** Best known repro remains: ./run_all --shuffle --shuffle-seed {shuffle_seed} ****")
        sys.exit(1)

    candidates, victim = result
    print("**** Bisection complete ****")
    print(f"**** Minimal reproducing order ({len(candidates)} test(s) before the victim): {candidates + [victim]} ****")
    print(f"**** Reproduce with: {minimal_repro_cmd(candidates, victim)} ****")
    sys.exit(0)


def confirm_pair(candidates, victim):
    """Check a bisected (culprits, victim) pair really is an ordering bug before recording it.

    Two ways the bisector can hand back a pair that is not one, both seen in practice:

    - The victim fails on its own, so no culprit is needed and the one named is innocent. A test
      that depends on the wall clock does this: it fails according to the time of day, and the
      search still converges on whichever test happened to precede it.
    - The pair does not reproduce when re-run, meaning the failure was not deterministic and the
      reduction was chasing noise.

    Returns True only when the victim passes alone *and* fails after the culprits run, which is
    what an ordering bug actually looks like.
    """
    victim_alone_ok, _elapsed, _stdout, _stderr = run_explicit([victim])
    if not victim_alone_ok:
        return False
    pair_ok, _elapsed, _stdout, _stderr = run_explicit(list(candidates) + [victim])
    return not pair_ok


def log_unreproducible(log_dir, shuffle_seed, victim, reason, stdout, stderr):
    """Write the output of a failure that could not be pinned to an ordering, for later reading.

    These are worth keeping rather than discarding: a failure that the bisector cannot reduce is
    usually a test that is flaky or clock-dependent, which is a real bug of a different kind and
    otherwise leaves no trace at all once --hunt moves on to the next sample.
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f"unreproducible_{victim}_{shuffle_seed}.log")
        with open(path, "w") as handle:
            handle.write(f"victim: {victim}\nshuffle-seed: {shuffle_seed}\nreason: {reason}\n")
            handle.write("=" * 70 + "\n")
            handle.write(stdout)
            handle.write(stderr)
        print(f"****   unreproducible failure logged to {path} ****")
    except Exception as error:
        print(f"Warn: could not log unreproducible failure: {error}")


# Lives in tools/, not coverage/ - coverage/*.txt is gitignored (it's normally scratch/
# generated test output) but this file is a deliberately committed, shared record.
KNOWN_PAIRS_PATH = "../tools/shuffle_fuzz_known_pairs.txt"


def load_known_pairs(path):
    """Parse the "culprit[, culprit2, ...] -> victim" lines in the known-pairs file into a set."""
    known = set()
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                culprit, _, victim = line.partition(" -> ")
                if victim:
                    known.add((culprit, victim))
    return known


def hunt(quick, keyword, max_attempts, known_pairs_path, log_dir):
    """One-liner "find the next problem pair": keep sampling shuffled runs, bisecting each
    failure, until one turns up whose (culprit, victim) pair isn't already in the known-pairs
    file - then print it, append it to the file, and stop. Re-running the same command finds
    a *different* pair next time instead of rediscovering the same handful repeatedly."""
    known = load_known_pairs(known_pairs_path)
    print(f"**** Hunting for a new (culprit, victim) pair - {len(known)} already known from {known_pairs_path} ****")

    for attempt in range(1, max_attempts + 1):
        shuffle_seed = random.randrange(2**32)
        passed, elapsed, stdout, stderr = run_once(shuffle_seed, quick, keyword, [])
        if passed:
            print(f"  [{attempt}/{max_attempts}] shuffle-seed={shuffle_seed} passed in {elapsed:.1f}s, resampling")
            continue

        order, failed_name = parse_run_order(stdout)
        if not failed_name or failed_name not in order:
            print(f"  [{attempt}/{max_attempts}] shuffle-seed={shuffle_seed} failed but could not be parsed, resampling")
            continue

        result = bisect_order(order, failed_name, quiet=True)
        if result is None:
            # The failure did not reproduce from a --test subset at all. That is not an ordering
            # bug: something else about the full run made it fail (most often a test that depends
            # on the wall clock, which fails or passes according to the time of day rather than
            # what ran before it). Worth keeping the evidence rather than silently resampling.
            log_unreproducible(log_dir, shuffle_seed, failed_name, "did not reproduce from any --test subset", stdout, stderr)
            print(f"  [{attempt}/{max_attempts}] shuffle-seed={shuffle_seed} victim={failed_name} did not reproduce via --test - logged as unreproducible, resampling")
            continue

        candidates, victim = result
        pair_key = (", ".join(candidates), victim)
        if pair_key in known:
            print(f"  [{attempt}/{max_attempts}] shuffle-seed={shuffle_seed} -> {list(candidates)} -> {victim} (already known, resampling)")
            continue

        # Confirm the reduced pair before recording it. bisect_order() assumes a failure is
        # deterministic; when it is not, the search still converges on *some* minimal subset, and
        # that subset is an arbitrary innocent test rather than a real culprit. Re-running the
        # exact pair separates the two: a genuine ordering bug reproduces every time.
        if not confirm_pair(candidates, victim):
            log_unreproducible(log_dir, shuffle_seed, victim, "bisected to {} -> {} but that pair passes when re-run".format("+".join(candidates) or "(nothing)", victim), stdout, stderr)
            print(f"  [{attempt}/{max_attempts}] shuffle-seed={shuffle_seed} -> {list(candidates)} -> {victim} did not reproduce on re-run - logged as unreproducible (likely time-of-day or otherwise non-deterministic), resampling")
            continue

        print(f"**** New pair found on attempt {attempt}/{max_attempts} ****")
        print(f"**** Culprit(s): {list(candidates)}  Victim: {victim} ****")
        print(f"**** Reproduce with: {minimal_repro_cmd(candidates, victim)} ****")
        with open(known_pairs_path, "a") as f:
            f.write(f"{pair_key[0]} -> {pair_key[1]}\n")
        print(f"**** Recorded in {known_pairs_path} - next run of --hunt will look for a different pair ****")
        sys.exit(0)

    print(f"**** No new pair found in {max_attempts} attempts (all failures found were already known, or the suite stayed clean) ****")
    sys.exit(1)


def campaign(rounds, quick, keyword, seed):
    """Repeat shuffle -> bisect -> record as a capture-release style sampling process: each
    round finds one failing order and bisects it down to a minimal (culprit-set, victim)
    pair, and we tally which pairs recur across independent rounds. A pair seen repeatedly
    across many independent shuffles is a real, common leak; a pair seen once might be an
    artefact of that particular ordering (e.g. the "culprit" is actually several tests
    which only interact combined) - the frequency table is the signal, not any single round."""
    master_seed = seed if seed is not None else random.randrange(2**32)
    print(f"**** Campaign master seed: {master_seed} (replay with --seed {master_seed}) ****")
    picker = random.Random(master_seed)

    pair_counts = Counter()
    rounds_with_failure = 0

    for round_index in range(rounds):
        print(f"**** Campaign round {round_index + 1}/{rounds} ****")
        found = False
        # Sample shuffle-seeds until one fails, capped so a very clean suite doesn't spin forever.
        for _attempt in range(50):
            shuffle_seed = picker.randrange(2**32)
            passed, elapsed, stdout, stderr = run_once(shuffle_seed, quick, keyword, [])
            if not passed:
                found = True
                break
            print(f"  shuffle-seed={shuffle_seed} passed in {elapsed:.1f}s, resampling ****")

        if not found:
            print(f"**** Round {round_index + 1}: no failure found in 50 samples, skipping ****")
            continue

        order, failed_name = parse_run_order(stdout)
        if not failed_name or failed_name not in order:
            print(f"**** Round {round_index + 1}: shuffle-seed={shuffle_seed} failed but the failing test could not be parsed - skipping ****")
            continue

        rounds_with_failure += 1
        result = bisect_order(order, failed_name, quiet=True)
        if result is None:
            print(f"**** Round {round_index + 1}: shuffle-seed={shuffle_seed} victim={failed_name} did not reproduce via --test - skipping ****")
            continue

        candidates, victim = result
        pair = (tuple(candidates), victim)
        pair_counts[pair] += 1
        print(f"**** Round {round_index + 1}: shuffle-seed={shuffle_seed} -> minimal culprit(s) {list(candidates)} before victim {victim} ****")
        print(f"****   Reproduce with: {minimal_repro_cmd(candidates, victim)} ****")

    print("**** Campaign complete ****")
    print(f"**** {rounds_with_failure}/{rounds} round(s) produced a bisectable failure ****")
    if not pair_counts:
        print("**** No reproducible (culprit, victim) pairs collected ****")
        sys.exit(0)

    print("**** Suspect frequency table (most common first) ****")
    for (candidates, victim), count in pair_counts.most_common():
        print(f"  seen {count}x: {list(candidates)} -> {victim}")
        print(f"    {minimal_repro_cmd(candidates, victim)}")
    sys.exit(0)


def main():
    """Parse CLI args and dispatch to --bisect, --hunt, --campaign, or plain fuzzing."""
    parser = argparse.ArgumentParser(description="Fuzz the unit test suite with random test orderings to find fixture state-leak bugs (#5079)")
    parser.add_argument("--runs", type=int, default=20, metavar="N", help="Number of shuffled runs to try (default: 20)")
    parser.add_argument("--quick", action="store_true", help="Pass --quick through to unit_test.py (skip slow tests)")
    parser.add_argument("--keyword", "-k", metavar="PATTERN", help="Only shuffle tests matching this keyword, passed through as -k")
    parser.add_argument("--seed", type=int, default=None, metavar="N", help="Master seed used to pick each run's shuffle-seed, so a whole fuzz session can be replayed (default: a random master seed, printed at the start of the run)")
    parser.add_argument("--stop-on-failure", action="store_true", default=True, help="Stop at the first failing shuffle order (default: on)")
    parser.add_argument("--keep-going", dest="stop_on_failure", action="store_false", help="Keep fuzzing after a failure instead of stopping at the first one")
    parser.add_argument("--log-dir", default="shuffle_fuzz_logs", metavar="DIR", help="Directory to write full output of any failing run to (default: shuffle_fuzz_logs)")
    parser.add_argument("--bisect", type=int, default=None, metavar="SHUFFLE_SEED", help="Instead of fuzzing, delta-debug a known-failing --shuffle-seed down to the minimal ordered subsequence that still reproduces it")
    parser.add_argument(
        "--campaign",
        type=int,
        default=None,
        metavar="ROUNDS",
        help="Instead of a single fuzz/bisect, repeat shuffle-then-bisect for ROUNDS independent rounds and print a frequency table of (culprit(s), victim) pairs - capture-release style sampling to separate common leaks from one-off orderings",
    )
    parser.add_argument("--hunt", action="store_true", help="One-liner: find the next NEW (culprit, victim) pair not already in --known-pairs-file, print its minimal repro, and stop. Re-run to keep finding more, one at a time.")
    parser.add_argument("--hunt-attempts", type=int, default=50, metavar="N", help="Max shuffled samples to try before giving up on --hunt (default: 50)")
    parser.add_argument("--known-pairs-file", default=KNOWN_PAIRS_PATH, metavar="PATH", help=f"Where --hunt records pairs it has already reported, so the next run finds a different one (default: {KNOWN_PAIRS_PATH})")
    args = parser.parse_args()

    if args.bisect is not None:
        bisect(args.bisect, args.quick, args.keyword)
        return

    if args.hunt:
        hunt(args.quick, args.keyword, args.hunt_attempts, args.known_pairs_file, args.log_dir)
        return

    if args.campaign is not None:
        campaign(args.campaign, args.quick, args.keyword, args.seed)
        return

    master_seed = args.seed if args.seed is not None else random.randrange(2**32)
    print(f"**** Shuffle fuzz master seed: {master_seed} (replay with --seed {master_seed}) ****")
    picker = random.Random(master_seed)
    failures = []

    for run_index in range(args.runs):
        shuffle_seed = picker.randrange(2**32)
        print(f"**** Shuffle fuzz run {run_index + 1}/{args.runs}: shuffle-seed={shuffle_seed} ****")

        passed, elapsed, stdout, stderr = run_once(shuffle_seed, args.quick, args.keyword, [])
        status = "PASSED" if passed else "FAILED"
        print(f"**** Run {run_index + 1}/{args.runs} {status} in {elapsed:.1f}s (shuffle-seed={shuffle_seed}) ****")

        if not passed:
            failures.append(shuffle_seed)
            os.makedirs(args.log_dir, exist_ok=True)
            log_path = os.path.join(args.log_dir, f"shuffle_seed_{shuffle_seed}.log")
            with open(log_path, "w") as f:
                f.write(stdout)
                f.write(stderr)
            print(f"**** Log written to {log_path} - reproduce with: ./run_all --shuffle --shuffle-seed {shuffle_seed} ****")

            if args.stop_on_failure:
                break

    print("**** Shuffle fuzzing complete ****")
    if failures:
        print(f"**** Found {len(failures)} failing shuffle order(s): {failures} ****")
        sys.exit(1)
    else:
        print(f"**** No ordering-dependent failures found in {args.runs} run(s) ****")
        sys.exit(0)


if __name__ == "__main__":
    main()

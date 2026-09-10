#!/bin/sh
# Run one Predbat test module and save the full output to a log file.
#
# usage: tools/triage_test.sh <test-name> [log-file]
#
# This exists for the triage bot (tools/triage_daemon.py). run_all has to
# execute with coverage/ as the working directory, and the obvious way to write
# that - `cd coverage && ./run_all --test X > out.log 2>&1` - is denied under a
# non-interactive permission mode: Claude Code prompts on a `cd` combined with
# an output redirect, because it can't tell which directory the redirect target
# resolves against once the `cd` has run. Keeping the cd and the redirect inside
# this script leaves a single plain command for the permission check to match.
#
# Prints the exit status and the tail of the log; grep the log file for the rest.
# Exits with the test run's own status.

set -u

if [ $# -lt 1 ]; then
    echo "usage: $0 <test-name> [log-file]" >&2
    exit 2
fi

test_name=$1
log_file=${2:-${TMPDIR:-/tmp}/triage-test-$1.log}

cd "$(dirname "$0")/../coverage" || exit 1

./run_all --test "$test_name" > "$log_file" 2>&1
status=$?

echo "test=$test_name exit=$status log=$log_file"
echo "---- last 30 lines ----"
tail -n 30 "$log_file"

exit $status

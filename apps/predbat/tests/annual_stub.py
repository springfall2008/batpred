# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""A stand-in for annual_cli.py --machine, used to drive AnnualJob in tests.

Behaviour is chosen by argv[1] so one script covers every case the job control
has to survive, without ever running the real three-minute engine.
"""

import json
import sys
import time


def main():
    """Emit the behaviour named by argv[1] and exit with a matching code."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

    if mode == "ok":
        for step in range(1, 4):
            sys.stderr.write(json.dumps({"completed": step, "total": 3, "message": "step {}".format(step)}) + "\n")
            sys.stderr.flush()
        json.dump({"year": 2025, "months": [], "annual": {"months_included": 0}}, sys.stdout)
        return 0

    if mode == "garbage_progress":
        sys.stderr.write("not json at all\n")
        sys.stderr.write(json.dumps({"completed": 1, "total": 1, "message": "recovered"}) + "\n")
        sys.stderr.flush()
        json.dump({"year": 2025, "months": [], "annual": {"months_included": 0}}, sys.stdout)
        return 0

    if mode == "fail":
        sys.stderr.write("something went wrong\n")
        return 3

    if mode == "bad_output":
        sys.stdout.write("this is not json")
        return 0

    if mode == "null_output":
        # Valid JSON, but not the object AnnualJob's results document must be.
        json.dump(None, sys.stdout)
        return 0

    if mode == "big_streams":
        # Push well over the default OS pipe buffer (typically 64 KiB) through
        # both stdout and stderr, so a job control that reads one stream to
        # completion before draining the other would deadlock: this child
        # would block writing to the second pipe while nothing reads it.
        # Stderr is spread across many moderate lines rather than one huge
        # one - a single line over the StreamReader's own line-length limit
        # would raise an unrelated error and prove nothing about deadlocking.
        #
        # ~1 MiB, not ~200 KiB: the sequential-blocking failure mode this test
        # exists to catch is "read stdout to completion, THEN read stderr" (or
        # vice versa) - which only actually blocks once the stream being written
        # first fills its OS pipe buffer AND the child still has more to write
        # after that. 200 KiB against a ~192 KiB buffer left only 2% headroom -
        # any small platform difference in pipe sizing could pass a genuinely
        # sequential implementation by accident. 1 MiB is comfortably past any
        # realistic pipe buffer, so the test discriminates reliably instead of
        # by luck.
        line_filler = "x" * 500
        written = 0
        step = 0
        while written < 1000000:
            step += 1
            payload = json.dumps({"completed": step, "total": step, "message": line_filler})
            sys.stderr.write(payload + "\n")
            written += len(payload) + 1
        sys.stderr.flush()
        filler = "x" * 1000000
        json.dump({"year": 2025, "months": [], "annual": {"months_included": 0}, "filler": filler}, sys.stdout)
        return 0

    if mode == "hang":
        while True:
            time.sleep(0.1)

    return 0


if __name__ == "__main__":
    sys.exit(main())

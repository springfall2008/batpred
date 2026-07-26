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

    if mode == "hang":
        while True:
            time.sleep(0.1)

    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Unit tests for the export limit encoding checker."""

import os
import subprocess
import sys
import tempfile

CHECKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_export_limit_encoding.py")

# Built rather than written literally so this file does not itself contain a nested triple quote
q = chr(34) * 3


def run_checker(source):
    """Run the checker over one throwaway file, returning (exit code, output)."""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "sample.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        result = subprocess.run([sys.executable, CHECKER, path], capture_output=True, text=True)
        return result.returncode, result.stdout


def check(name, source, expect_fail):
    """Assert the checker does or does not reject this source, returning True on failure."""
    code, output = run_checker(source)
    failed = bool(code) != expect_fail
    print("{}: {}".format("FAIL" if failed else "PASS", name))
    if failed:
        print("   expected {}, got exit {}".format("a rejection" if expect_fail else "no rejection", code))
        print("   " + output.replace(chr(10), chr(10) + "   "))
    return failed


def main():
    """Run every case, returning 1 if any failed."""
    failed = False

    failed |= check("a sentinel comparison is rejected", "if limit == EXPORT_LIMIT_FREEZE:\n    pass\n", True)
    failed |= check("a reversed sentinel comparison is rejected", "if EXPORT_LIMIT_IDLE <= limit:\n    pass\n", True)
    failed |= check("a bare 99 comparison is rejected", "if export_limits[n] >= 99:\n    pass\n", True)
    failed |= check("a bare 100 comparison is rejected", "if this_export_limit == 100.0:\n    pass\n", True)
    failed |= check("a reversed bare-number comparison is rejected", "if 99 == limits[n]:\n    pass\n", True)

    failed |= check("the accessors are accepted", "if export_mode_of(limit) == EXPORT_MODE_FREEZE:\n    pass\n", False)
    failed |= check("the no-battery predicate is accepted", "if export_limit_exports_no_battery(limit):\n    pass\n", False)

    failed |= check("a hash comment is not scanned", "# limit == EXPORT_LIMIT_FREEZE means a freeze\n", False)
    failed |= check(
        "a docstring is not scanned",
        "def f():\n    " + q + "Anything below EXPORT_LIMIT_FREEZE is a real export." + q + "\n    return 1\n",
        False,
    )
    failed |= check(
        "a multi line docstring is not scanned",
        "def f():\n    " + q + "\n    A limit >= 100 is off.\n    A limit == 99 is a freeze.\n    " + q + "\n    return 1\n",
        False,
    )

    failed |= check(
        "an annotated deliberate use is accepted",
        "if soc_percent == EXPORT_LIMIT_FREEZE:  # encoding-ok: already a decoded percentage\n    pass\n",
        False,
    )

    # A percentage that happens to be 99 or 100 but is not an export limit must not trip the check
    failed |= check("an unrelated percentage is accepted", "if soc_percent >= 100:\n    pass\n", False)

    print("")
    print("Export limit encoding checker tests: {}".format("FAILED" if failed else "all passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

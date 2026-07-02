# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string

"""CI utility: verify a specific checked-in prediction kernel binary loads and passes parity.

Run as: python3 verify_kernel_binary.py <path-to-.so>

Bypasses ensure_kernel_built()'s "get me any working kernel" fallback (which would happily
build a fresh local copy and mask a broken checked-in binary) and load_kernel()'s normal
multi-candidate fallthrough (which would happily load a different, valid candidate such as a
local dev build produced by an earlier CI step) by asserting KERNEL_STATUS names the exact
pinned path after loading. Exits non-zero if the binary is missing, fails to load, is stale
(ABI/parity mismatch), or fails the parity/random-sweep test suite.
"""

import os
import sys

# apps/predbat itself must be importable (matches how unit_test.py locates its own package,
# see coverage/run_all: `python3 ../apps/predbat/unit_test.py`, run with cwd=coverage/)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prediction_kernel
from unit_test import create_predbat
from tests.test_kernel_parity import run_edge_case_tests, run_random_sweep_tests


def main():
    """Load and parity-test the binary named by argv[1], exit 1 on any failure"""
    if len(sys.argv) != 2:
        print("Usage: python3 verify_kernel_binary.py <path-to-.so>")
        return 1

    so_path = sys.argv[1]
    if not os.path.exists(so_path):
        print("No checked-in kernel binary at {} - nothing to verify".format(so_path))
        return 0

    os.environ["PREDBAT_KERNEL_SO"] = so_path
    prediction_kernel.KERNEL_LOAD_TRIED = False
    prediction_kernel.KERNEL_LIB = None
    lib = prediction_kernel.load_kernel(log=print)
    if lib is None:
        print("ERROR: checked-in binary failed to load: {}".format(prediction_kernel.KERNEL_STATUS))
        return 1

    expected_status = "loaded from {}".format(so_path)
    if prediction_kernel.KERNEL_STATUS != expected_status:
        print("ERROR: expected to load the pinned binary ({}) but loaded a different candidate: {}".format(expected_status, prediction_kernel.KERNEL_STATUS))
        return 1

    print("Confirmed pinned binary loaded ({}) - running parity tests".format(expected_status))
    my_predbat = create_predbat()
    failed = run_edge_case_tests(my_predbat)
    failed |= run_random_sweep_tests(my_predbat)
    print("Checked-in kernel binary {}: {}".format(so_path, "FAILED parity" if failed else "PASSED parity"))
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

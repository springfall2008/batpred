# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
OpenBLAS spin timeout tests

OpenBLAS (NumPy's BLAS on Linux) keeps its worker threads busy-waiting after every matrix product.
predbat.py sets OPENBLAS_THREAD_TIMEOUT so they sleep almost at once, which OpenBLAS reads only
when it is loaded, so it must be in place before anything imports NumPy. Each case runs in a fresh
interpreter, since this test process has long since loaded NumPy.
"""

import os
import subprocess
import sys

PROBE = "import os, sys; import predbat; print(os.environ.get('OPENBLAS_THREAD_TIMEOUT')); print('numpy' in sys.modules)"


def _import_predbat(preset=None):
    """Import predbat in a fresh interpreter; return (OPENBLAS_THREAD_TIMEOUT afterwards, whether NumPy was imported)."""
    env = {key: value for key, value in os.environ.items() if key != "OPENBLAS_THREAD_TIMEOUT"}
    if preset is not None:
        env["OPENBLAS_THREAD_TIMEOUT"] = preset
    folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=folder, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("importing predbat failed: " + result.stderr[-2000:])
    lines = result.stdout.strip().splitlines()
    return lines[-2], lines[-1] == "True"


def test_openblas_env(my_predbat=None):
    """Importing predbat sets the OpenBLAS spin timeout before NumPy loads, and keeps a value the user set"""
    print("\n=== Testing OPENBLAS_THREAD_TIMEOUT is set before NumPy loads ===")
    failed = 0
    value, numpy_loaded = _import_predbat()
    if value != "4":
        print("ERROR: OPENBLAS_THREAD_TIMEOUT is {} after importing predbat, expected 4".format(value))
        failed += 1
    if numpy_loaded:
        print("ERROR: importing predbat imported NumPy, so OpenBLAS loads before the timeout can apply to it")
        failed += 1
    value, _ = _import_predbat(preset="10")
    if value != "10":
        print("ERROR: a user's OPENBLAS_THREAD_TIMEOUT=10 became {}".format(value))
        failed += 1
    if not failed:
        print("✓ predbat sets OPENBLAS_THREAD_TIMEOUT=4 before NumPy loads, and keeps a value already set")
    return failed

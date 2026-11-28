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
WARNING = "NumPy was loaded before predbat.py set OPENBLAS_THREAD_TIMEOUT"


def _run_probe(probe, preset=None):
    """Run probe in a fresh interpreter in apps/predbat, without OPENBLAS_THREAD_TIMEOUT unless preset gives one; return its stdout."""
    env = {key: value for key, value in os.environ.items() if key != "OPENBLAS_THREAD_TIMEOUT"}
    if preset is not None:
        env["OPENBLAS_THREAD_TIMEOUT"] = preset
    folder = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, "-c", probe], cwd=folder, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("importing predbat failed: " + result.stderr[-2000:])
    return result.stdout


def _import_predbat(preset=None):
    """Import predbat in a fresh interpreter; return (OPENBLAS_THREAD_TIMEOUT afterwards, whether NumPy was imported, stdout)."""
    stdout = _run_probe(PROBE, preset)
    lines = stdout.strip().splitlines()
    return lines[-2], lines[-1] == "True", stdout


def test_openblas_env(my_predbat=None):
    """Importing predbat sets the OpenBLAS spin timeout before NumPy loads, and keeps a value the user set"""
    print("\n=== Testing OPENBLAS_THREAD_TIMEOUT is set before NumPy loads ===")
    failed = 0
    value, numpy_loaded, stdout = _import_predbat()
    if value != "4":
        print("ERROR: OPENBLAS_THREAD_TIMEOUT is {} after importing predbat, expected 4".format(value))
        failed += 1
    if numpy_loaded:
        print("ERROR: importing predbat imported NumPy, so OpenBLAS loads before the timeout can apply to it")
        failed += 1
    if WARNING in stdout:
        print("ERROR: importing predbat first still warned that NumPy was loaded too early")
        failed += 1
    value, _, _ = _import_predbat(preset="10")
    if value != "10":
        print("ERROR: a user's OPENBLAS_THREAD_TIMEOUT=10 became {}".format(value))
        failed += 1
    # NumPy loaded first: the setting cannot apply, and predbat must say so - unless the user set it, which still applies
    if WARNING not in _run_probe("import numpy; import predbat"):
        print("ERROR: importing predbat after NumPy did not warn that OPENBLAS_THREAD_TIMEOUT will not apply")
        failed += 1
    if WARNING in _run_probe("import numpy; import predbat", preset="10"):
        print("ERROR: warned although the user's OPENBLAS_THREAD_TIMEOUT was in place before NumPy loaded")
        failed += 1
    if not failed:
        print("✓ predbat sets OPENBLAS_THREAD_TIMEOUT=4 before NumPy loads, keeps a value already set, and warns when NumPy came first")
    return failed

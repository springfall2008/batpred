# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Loaded as sitecustomize inside the memory benchmark container only (see container_run.sh).

The container freezes the wall clock with libfaketime. Under it, CPython 3.13's time.sleep (clock_nanosleep with an
absolute deadline) fails with EINVAL, while condition and semaphore waits on the monotonic clock still work. This
replaces time.sleep with such a wait, so code that sleeps behaves as it does on a real clock. Nothing outside the
benchmark loads this file.
"""

import threading
import time

_wait = threading.Event().wait


def _sleep(seconds):
    """Sleep for seconds on the monotonic clock, as time.sleep would."""
    if seconds < 0:
        raise ValueError("sleep length must be non-negative")
    _wait(seconds)


time.sleep = _sleep

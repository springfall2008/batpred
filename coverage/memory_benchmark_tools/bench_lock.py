# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""A machine-wide lock for benchmark runs, so a run's wall time is never measured while another run is going.

    python3 bench_lock.py <lock path> -- <command...>

Runs the command holding an exclusive flock on <lock path>, with BENCH_QUEUED_SECONDS (the seconds spent waiting for the
lock, 1 dp) added to its environment, and exits with the command's exit code. The kernel drops a flock when its holder
dies, so a killed run never blocks the next one. macOS has no flock(1), hence this helper.
"""

import contextlib
import fcntl
import os
import subprocess
import sys
import time


@contextlib.contextmanager
def held(path, notify=print):
    """Hold an exclusive lock on path (created if needed) for the with block; yield the seconds spent waiting for it.

    When the lock is busy, notify is called once with a message saying so before blocking.
    """
    began = time.monotonic()
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            notify("Waiting for the benchmark lock at {} (another benchmark run is going) ...".format(path))
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield time.monotonic() - began
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def main(argv):
    """Run the command after -- under the lock; return its exit code, or 2 on a usage error."""
    if len(argv) < 3 or argv[1] != "--":
        print("usage: bench_lock.py <lock path> -- <command...>", file=sys.stderr)
        return 2
    with held(argv[0], notify=lambda message: print(message, flush=True)) as waited:
        env = dict(os.environ, BENCH_QUEUED_SECONDS="{:.1f}".format(waited))
        return subprocess.run(argv[2:], env=env).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

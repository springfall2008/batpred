#!/bin/bash
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Runs inside the memory benchmark container (started by coverage/memory_benchmark), with the tree's apps/predbat at
# /src (read-only), these tools at /bench (read-only), a scratch copy of the fixture at /fixture and the out dir at /out.
#
# Builds the prediction kernel for this architecture from the tree (on the real clock), then runs the driver with the
# wall clock frozen at the fixture's T0. Monotonic time is left running, or asyncio timers and thread waits never fire.
set -euo pipefail

DRIVER="${BENCH_DRIVER:-/bench/driver.py}"

cp -R /src /work
bash /work/build_kernel.sh > /out/kernel_build.log 2>&1

LIBFAKETIME="$(ls /usr/lib/*-linux-gnu/faketime/libfaketime.so.1 | head -1)"
cd /fixture
set +e
timeout "${BENCH_TIMEOUT:-1500}" env \
    LD_PRELOAD="$LIBFAKETIME" \
    FAKETIME="$BENCH_T0" \
    FAKETIME_DONT_FAKE_MONOTONIC=1 \
    PREDBAT_APPS_FILE=/fixture/apps.yaml \
    PYTHONPATH=/bench/frozen_clock:/work \
    python3 "$DRIVER" --out /out --cycles "${BENCH_CYCLES:-6}" ${BENCH_TRACEMALLOC:+--tracemalloc}
status=$?
set -e
if [ -f /fixture/predbat.log ]; then
    cp /fixture/predbat.log /out/predbat.log
fi
exit $status

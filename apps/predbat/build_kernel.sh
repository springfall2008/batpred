#!/bin/bash
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# -----------------------------------------------------------------------------
# Build the C++ prediction kernel as a shared library for local testing or
# inside the addon Docker image. Falls back to the Python engine when absent.
#
# Usage: bash apps/predbat/build_kernel.sh [output.so]
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
# Note: the library must NOT be named prediction_kernel.so or Python's importer
# would pick it up instead of prediction_kernel.py
OUT="${1:-$DIR/prediction_kernel_lib.so}"
CXX="${CXX:-g++}"
# -ffp-contract=off: no fused multiply-add, so floating point results match the
# Python engine bit-for-bit (CPython never fuses operations)
"$CXX" -std=c++17 -O2 -shared -fPIC -fno-fast-math -ffp-contract=off -Wall -Werror -o "$OUT" "$DIR/prediction_kernel.cpp"
echo "Built $OUT"

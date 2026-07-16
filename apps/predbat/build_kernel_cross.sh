#!/bin/bash
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# -----------------------------------------------------------------------------
# Cross-build the C++ prediction kernel for all supported Linux architectures
# using zig as the cross toolchain (install: brew install zig / apt install zig
# or download from https://ziglang.org/download/).
#
# Outputs are named so the loader (prediction_kernel.py) can pick the binary
# matching the platform and machine at runtime, or skip if missing:
#   prediction_kernel_lib_<machine>.so         (Linux)
#   prediction_kernel_lib_darwin_<machine>.so  (macOS)
# where Linux <machine> is x86_64, aarch64, armv7l or i686, covering the addon's
# Ubuntu (glibc) base images (amd64, aarch64, armv7/armhf, i386; glibc floor 2.17),
# and macOS <machine> is arm64 (Apple Silicon) or x86_64 (Intel).
#
# Usage: bash apps/predbat/build_kernel_cross.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ZIG="${ZIG:-zig}"

if ! command -v "$ZIG" >/dev/null 2>&1; then
    echo "ERROR: zig not found - install it (e.g. brew install zig) or set ZIG=/path/to/zig"
    exit 1
fi

# Flags must match build_kernel.sh: -ffp-contract=off keeps floating point
# bit-identical to the Python engine (no fused multiply-add).
# The nullability warnings come from zig's own libc++ headers, not our code
FLAGS="-std=c++17 -O2 -shared -fPIC -fno-fast-math -ffp-contract=off -Wall -Werror -Wno-nullability-completeness"

# target -> output machine name (as returned by platform.machine().lower())
build() {
    local target="$1"
    local out="$2"
    local strip_flag="$3"
    echo "Building $out (target $target)"
    # shellcheck disable=SC2086
    "$ZIG" c++ $FLAGS $strip_flag -target "$target" -o "$DIR/$out" "$DIR/prediction_kernel.cpp"
}

# Linux (ELF): --strip-all keeps the shipped binaries small
build "x86_64-linux-gnu.2.17" "prediction_kernel_lib_x86_64.so" "-Wl,--strip-all"
build "aarch64-linux-gnu.2.17" "prediction_kernel_lib_aarch64.so" "-Wl,--strip-all"
build "arm-linux-gnueabihf.2.17" "prediction_kernel_lib_armv7l.so" "-Wl,--strip-all"
build "x86-linux-gnu.2.17" "prediction_kernel_lib_i686.so" "-Wl,--strip-all"

# macOS (Mach-O): -S strips debug info
build "aarch64-macos" "prediction_kernel_lib_darwin_arm64.so" "-Wl,-S"
build "x86_64-macos" "prediction_kernel_lib_darwin_x86_64.so" "-Wl,-S"

echo "Done:"
ls -la "$DIR"/prediction_kernel_lib_*.so

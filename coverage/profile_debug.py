#!/usr/bin/env python3
"""
Profile a single Predbat debug case to identify performance bottlenecks.

Usage:
    python3 profile_debug.py cases/predbat_debug_agile1.yaml
    python3 profile_debug.py cases/predbat_debug_agile1.yaml --output agile1_profile.prof
    python3 profile_debug.py cases/predbat_debug_agile1.yaml --view  # Auto-open with snakeviz

Requirements:
    pip install snakeviz  # For visual flame graphs
"""

import argparse
import pstats
import sys
import os
import subprocess
from io import StringIO


def profile_debug_case(debug_file, output_file=None, view=False, sort_by="cumulative", top_n=30):
    """
    Profile a debug case execution by running unit_test.py with profiling.

    Args:
        debug_file: Path to debug YAML file
        output_file: Optional output file for profile data
        view: If True, open profile in snakeviz after completion
        sort_by: Sort key for stats ('cumulative', 'time', 'calls')
        top_n: Number of top functions to display
    """
    if not os.path.exists(debug_file):
        print(f"Error: Debug file not found: {debug_file}")
        sys.exit(1)

    # Determine output file
    if not output_file:
        base_name = os.path.basename(debug_file).replace(".yaml", "")
        output_file = f"{base_name}_profile.prof"

    print(f"Profiling debug case: {debug_file}")
    print("-" * 80)

    # Build command to run unit_test.py with cProfile
    unit_test_path = os.path.join("..", "apps", "predbat", "unit_test.py")
    cmd = ["python3", "-m", "cProfile", "-o", output_file, unit_test_path, "--debug_file", debug_file]

    print(f"Running: {' '.join(cmd)}\n")

    # Run the command
    result = subprocess.run(cmd, capture_output=False)

    if result.returncode != 0:
        print(f"\nError: Debug case failed with exit code {result.returncode}")
        sys.exit(1)

    print(f"\nProfile data saved to: {output_file}")

    # Load and analyze profile
    stats = pstats.Stats(output_file)
    stats.strip_dirs()

    # Print summary statistics
    print("\n" + "=" * 80)
    print(f"TOP {top_n} FUNCTIONS BY {sort_by.upper()} TIME")
    print("=" * 80)

    stream = StringIO()
    stats_copy = pstats.Stats(output_file, stream=stream)
    stats_copy.strip_dirs()
    stats_copy.sort_stats(sort_by)
    stats_copy.print_stats(top_n)
    print(stream.getvalue())

    # Print focused stats on key functions
    print("\n" + "=" * 80)
    print("PREDBAT OPTIMIZATION FUNCTIONS")
    print("=" * 80)

    stream = StringIO()
    stats_copy = pstats.Stats(output_file, stream=stream)
    stats_copy.strip_dirs()
    stats_copy.sort_stats(sort_by)
    stats_copy.print_stats("optimise")

    output = stream.getvalue()
    if output.strip() and "0 function calls" not in output:
        print(output)
    else:
        print("No optimization functions found in profile")

    # Print prediction functions
    print("\n" + "=" * 80)
    print("PREDICTION FUNCTIONS")
    print("=" * 80)

    stream = StringIO()
    stats_copy = pstats.Stats(output_file, stream=stream)
    stats_copy.strip_dirs()
    stats_copy.sort_stats(sort_by)
    stats_copy.print_stats("run_prediction")

    output = stream.getvalue()
    if output.strip() and "0 function calls" not in output:
        print(output)
    else:
        print("No prediction functions found in profile")

    # Open in snakeviz if requested
    if view:
        try:
            print(f"\nOpening profile in snakeviz...")
            subprocess.run(["snakeviz", output_file], check=False)
        except FileNotFoundError:
            print("\nError: snakeviz not found. Install with: pip install snakeviz")
            print(f"You can manually view the profile with: snakeviz {output_file}")
    else:
        print(f"\nTo view interactive flame graph, run: snakeviz {output_file}")

    return output_file


def main():
    parser = argparse.ArgumentParser(
        description="Profile a Predbat debug case execution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 profile_debug.py cases/predbat_debug_agile1.yaml
  python3 profile_debug.py cases/predbat_debug_agile1.yaml --view
  python3 profile_debug.py cases/predbat_debug_agile1.yaml -o custom.prof -s time -n 50
        """,
    )

    parser.add_argument("debug_file", help="Path to debug YAML file (e.g., cases/predbat_debug_agile1.yaml)")

    parser.add_argument("-o", "--output", help="Output file for profile data (default: auto-generated from input file)")

    parser.add_argument("-v", "--view", action="store_true", help="Automatically open profile in snakeviz after completion")

    parser.add_argument("-s", "--sort", default="cumulative", choices=["cumulative", "time", "calls", "ncalls"], help="Sort statistics by this key (default: cumulative)")

    parser.add_argument("-n", "--top", type=int, default=30, help="Number of top functions to display (default: 30)")

    args = parser.parse_args()

    profile_debug_case(debug_file=args.debug_file, output_file=args.output, view=args.view, sort_by=args.sort, top_n=args.top)


if __name__ == "__main__":
    main()

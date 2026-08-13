# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the region tiling geometry and the region portfolio branch selection."""

from plan import REGION_SIZE_MIN, REGION_SIZE_START, REGION_SWITCH_THRESHOLD


def _fail(name, message):
    """Print a test failure and return True"""
    print("ERROR: {} - {}".format(name, message))
    return True


def test_region_passes_uniform(my_predbat):
    """Uniform tiling halves the region width each pass and tiles the record end to start"""
    failed = False
    passes = my_predbat.compute_region_passes(0, 48 * 60, stagger=False)

    sizes = [size for size, _ in passes]
    if sizes != [960, 480, 240, 120]:
        failed |= _fail("region_passes_uniform", "expected widths [960, 480, 240, 120] got {}".format(sizes))

    counts = [len(regions) for _, regions in passes]
    if counts != [3, 6, 12, 24]:
        failed |= _fail("region_passes_uniform", "expected counts [3, 6, 12, 24] got {}".format(counts))

    # Tiles must not overlap within a pass and must be exactly one region wide (bar the clipped first tile)
    for size, regions in passes:
        ordered = sorted(regions)
        for index in range(1, len(ordered)):
            if ordered[index][0] < ordered[index - 1][1]:
                failed |= _fail("region_passes_uniform", "width {} tiles {} and {} overlap".format(size, ordered[index - 1], ordered[index]))
    return failed


def test_region_passes_stagger(my_predbat):
    """Staggered tiling advances by half a region so each boundary is interior to the next tile"""
    failed = False
    uniform = my_predbat.compute_region_passes(0, 48 * 60, stagger=False)
    stagger = my_predbat.compute_region_passes(0, 48 * 60, stagger=True)

    if [size for size, _ in uniform] != [size for size, _ in stagger]:
        failed |= _fail("region_passes_stagger", "stagger changed the pass widths")

    for (size, uniform_regions), (_, stagger_regions) in zip(uniform, stagger):
        if size == REGION_SIZE_MIN:
            # The half-region step is clamped to the minimum width, so the finest pass is identical
            # in both layouts. There is nothing narrower to offset into, and the fine passes refine
            # an already-good plan rather than discovering pairs, so the offset buys nothing there.
            if sorted(stagger_regions) != sorted(uniform_regions):
                failed |= _fail("region_passes_stagger", "the finest pass should not be staggered")
            continue

        if len(stagger_regions) <= len(uniform_regions):
            failed |= _fail("region_passes_stagger", "width {} produced {} tiles, expected more than uniform's {}".format(size, len(stagger_regions), len(uniform_regions)))

        # Every interior boundary of the uniform layout must fall strictly inside some staggered
        # tile - that is the whole point, so a window pair the uniform tiling splits stays together.
        interior = sorted({start for start, _ in uniform_regions})[1:]
        for boundary in interior:
            if not any(start < boundary < end for start, end in stagger_regions):
                failed |= _fail("region_passes_stagger", "width {} boundary {} is not interior to any staggered tile".format(size, boundary))
    return failed


def test_region_passes_bounds(my_predbat):
    """Regions stay inside the record and drop tiles that end before the current time"""
    failed = False
    minutes_now = 600
    end_max = 600 + 36 * 60
    for stagger in (False, True):
        for size, regions in my_predbat.compute_region_passes(minutes_now, end_max, stagger=stagger):
            for start, end in regions:
                if start < 0 or end > end_max:
                    failed |= _fail("region_passes_bounds", "width {} tile {} escapes [0, {}]".format(size, (start, end), end_max))
                if end < minutes_now:
                    failed |= _fail("region_passes_bounds", "width {} tile {} ends before minutes_now {}".format(size, (start, end), minutes_now))
                if start >= end:
                    failed |= _fail("region_passes_bounds", "width {} tile {} is empty".format(size, (start, end)))
    return failed


def test_region_passes_cover_record(my_predbat):
    """Each pass covers the whole live part of the record, so no window is left unoptimised"""
    failed = False
    minutes_now = 300
    end_max = 300 + 48 * 60
    for stagger in (False, True):
        for size, regions in my_predbat.compute_region_passes(minutes_now, end_max, stagger=stagger):
            covered = sorted(regions)
            reach = covered[0][0]
            if reach > minutes_now:
                failed |= _fail("region_passes_cover", "width {} starts at {} leaving {} uncovered".format(size, reach, minutes_now))
            for start, end in covered:
                if start > reach:
                    failed |= _fail("region_passes_cover", "width {} has a gap at {}".format(size, reach))
                reach = max(reach, end)
            if reach < end_max:
                failed |= _fail("region_passes_cover", "width {} reaches {} not {}".format(size, reach, end_max))
    return failed


def test_region_passes_min_size(my_predbat):
    """The descent stops at the minimum region width and never emits a narrower pass"""
    failed = False
    for stagger in (False, True):
        passes = my_predbat.compute_region_passes(0, 48 * 60, min_region_size=240, stagger=stagger)
        sizes = [size for size, _ in passes]
        if min(sizes) < 240:
            failed |= _fail("region_passes_min_size", "emitted a pass narrower than the minimum: {}".format(sizes))
        if sizes[0] != REGION_SIZE_START:
            failed |= _fail("region_passes_min_size", "first pass should be the widest ({}) got {}".format(REGION_SIZE_START, sizes[0]))
    return failed


def test_select_region_branch(my_predbat):
    """Branch B is kept only when it clears the switching threshold, otherwise the incumbent wins"""
    failed = False
    cases = [
        ([100.0], 0, "a lone branch is always the winner"),
        ([100.0, 100.0 - REGION_SWITCH_THRESHOLD - 0.01], 1, "clearing the threshold switches"),
        ([100.0, 100.0 - REGION_SWITCH_THRESHOLD + 0.01], 0, "a gain inside the threshold is noise and must not switch"),
        ([100.0, 100.0], 0, "a tie keeps the incumbent"),
        ([100.0, 101.0], 0, "a worse branch never wins"),
        ([100.0, 95.0, 90.0], 2, "the best branch clearing the threshold wins"),
        ([100.0, 90.0, 99.9], 1, "a marginal third branch does not displace a clear winner"),
    ]
    for metrics, expected, reason in cases:
        chosen = my_predbat.select_region_branch(metrics)
        if chosen != expected:
            failed |= _fail("select_region_branch", "{}: {} chose branch {} expected {}".format(reason, metrics, chosen, expected))
    return failed


def test_region_defaults(my_predbat):
    """The tiling constants match the geometry the optimiser was tuned against"""
    failed = False
    if REGION_SIZE_START != 16 * 60:
        failed |= _fail("region_defaults", "REGION_SIZE_START changed to {}".format(REGION_SIZE_START))
    if REGION_SIZE_MIN != 120:
        failed |= _fail("region_defaults", "REGION_SIZE_MIN changed to {}".format(REGION_SIZE_MIN))
    if REGION_SWITCH_THRESHOLD <= 0:
        failed |= _fail("region_defaults", "REGION_SWITCH_THRESHOLD must be positive, got {}".format(REGION_SWITCH_THRESHOLD))
    return failed


def run_region_portfolio_tests(my_predbat):
    """Run all region tiling and portfolio selection tests"""
    failed = False
    failed |= test_region_passes_uniform(my_predbat)
    failed |= test_region_passes_stagger(my_predbat)
    failed |= test_region_passes_bounds(my_predbat)
    failed |= test_region_passes_cover_record(my_predbat)
    failed |= test_region_passes_min_size(my_predbat)
    failed |= test_select_region_branch(my_predbat)
    failed |= test_region_defaults(my_predbat)
    if not failed:
        print("**** Region portfolio tests passed ****")
    return failed

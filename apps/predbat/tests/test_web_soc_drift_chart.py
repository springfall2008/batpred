# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the SoCPlanDrift chart (WebInterface.get_soc_drift_series, web.py) - the fan chart
overlaying each retained debug-history snapshot's own predict_soc_best against the live
actual SoC, so a plan's forecast for a given period can be seen changing over time rather
than only showing what the most recent replan currently expects.
"""

import asyncio
import datetime
import tempfile
import shutil
from types import SimpleNamespace

from web import WebInterface, SOC_DRIFT_CATEGORICAL
from debug_history import capture_snapshot
from storage import StorageLocalFiles


def _make_web(my_predbat, storage=None):
    """Build a minimal WebInterface bound to my_predbat, bypassing ComponentBase.__init__
    (which would stand up the real aiohttp app) - same pattern as test_web_chart_currency.py.
    """
    w = WebInterface.__new__(WebInterface)
    w.base = my_predbat
    w.log = my_predbat.log
    w.prefix = my_predbat.prefix
    my_predbat.components = SimpleNamespace(get_component=lambda name: storage if name == "storage" else None)
    return w


def _snapshot_yaml(now_utc, points, marker):
    """Build minimal debug-yaml-shaped text with just the fields get_soc_drift_series reads."""
    lines = ["now_utc: {}".format(now_utc.isoformat()), "predict_soc_best:"]
    for minute, kwh in points.items():
        lines.append("  {}: {}".format(minute, kwh))
    lines.append("marker: {}".format(marker))
    return "\n".join(lines) + "\n"


def test_soc_drift_chart(my_predbat):
    """Verify get_soc_drift_series() builds one correctly-shaped, correctly-faded series per
    retained snapshot, skips unusable ones, and that the SoCPlanDrift chart branch renders end to
    end (including the new chart-description text) without needing a live storage backend at
    all when there isn't one.
    """
    failed = False
    print("**** Testing SoCPlanDrift chart ****")

    now = datetime.datetime(2026, 8, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)
    tmpdir = tempfile.mkdtemp(prefix="predbat_test_soc_drift_")
    try:
        # _make_web() sets my_predbat.components as a side effect (there's only one shared
        # my_predbat fixture) - do the no-storage case first, once, so nothing later
        # re-clobbers a working storage wiring back to None.
        print("Test: no storage component available gives an empty series list, not an error")
        w_no_storage = _make_web(my_predbat, storage=None)
        result = asyncio.run(w_no_storage.get_soc_drift_series())
        if result != []:
            print("  ERROR: expected an empty list with no storage, got {}".format(result))
            failed = True

        storage = StorageLocalFiles(tmpdir, my_predbat.log)
        w = _make_web(my_predbat, storage=storage)

        print("Test: an empty buffer (storage present, nothing captured yet) gives an empty series list")
        result = asyncio.run(w.get_soc_drift_series())
        if result != []:
            print("  ERROR: expected an empty list with nothing captured, got {}".format(result))
            failed = True

        print("Test: each retained snapshot becomes one series, newest snapshot most opaque")
        asyncio.run(capture_snapshot(storage, _snapshot_yaml(now - datetime.timedelta(hours=6), {0: 5.0, 5: 5.1}, "oldest"), now - datetime.timedelta(hours=6), max_count=15))
        asyncio.run(capture_snapshot(storage, _snapshot_yaml(now - datetime.timedelta(hours=3), {0: 6.0, 5: 6.1}, "middle"), now - datetime.timedelta(hours=3), max_count=15))
        asyncio.run(capture_snapshot(storage, _snapshot_yaml(now, {0: 7.0, 5: 7.1}, "newest"), now, max_count=15))

        series = asyncio.run(w.get_soc_drift_series())
        if len(series) != 3:
            print("  ERROR: expected 3 series (one per snapshot), got {}".format(len(series)))
            failed = True
        else:
            opacities = [float(s["opacity"]) for s in series]
            if opacities != sorted(opacities, reverse=True):
                print("  ERROR: expected opacity to strictly decrease from newest to oldest, got {}".format(opacities))
                failed = True
            if opacities[0] != 0.9 or opacities[-1] != 0.55:
                print("  ERROR: expected newest opacity 0.9 and oldest 0.55, got {} and {}".format(opacities[0], opacities[-1]))
                failed = True

            colors = [s["color"] for s in series]
            if colors != SOC_DRIFT_CATEGORICAL[:3]:
                print("  ERROR: expected the first 3 categorical hues in snapshot order, got {}".format(colors))
                failed = True
            if len(set(colors)) < 2:
                print("  ERROR: expected distinguishable colours across snapshots, all three came back the same: {}".format(colors))
                failed = True

            newest = series[0]
            expected_first_point_ts = now.strftime("%Y-%m-%dT%H:%M:%S%z")
            if expected_first_point_ts not in newest["data"] or newest["data"][expected_first_point_ts] != 7.0:
                print("  ERROR: expected newest series to have a point at {} = 7.0, got {}".format(expected_first_point_ts, newest["data"]))
                failed = True
            expected_second_point_ts = (now + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
            if newest["data"].get(expected_second_point_ts) != 7.1:
                print("  ERROR: expected newest series point 5 minutes on to be 7.1, got {}".format(newest["data"].get(expected_second_point_ts)))
                failed = True

        print("Test: a snapshot missing predict_soc_best/now_utc is skipped rather than breaking the whole chart")
        asyncio.run(capture_snapshot(storage, "marker: no_soc_fields_here\n", now + datetime.timedelta(hours=1), max_count=15))
        series = asyncio.run(w.get_soc_drift_series())
        if len(series) != 3:
            print("  ERROR: the unusable snapshot should have been skipped, still expected 3 series, got {}".format(len(series)))
            failed = True

        print("Test: colour cycles back to the start past the 8th snapshot rather than running out")
        for offset in range(4, 10):
            asyncio.run(capture_snapshot(storage, _snapshot_yaml(now + datetime.timedelta(hours=offset), {0: float(offset)}, "extra_{}".format(offset)), now + datetime.timedelta(hours=offset), max_count=15))
        series = asyncio.run(w.get_soc_drift_series())
        if len(series) != 9:
            print("  ERROR: expected 9 series after adding 6 more (the unusable one from the previous test stays skipped), got {}".format(len(series)))
            failed = True
        else:
            colors = [s["color"] for s in series]
            if colors[0] != colors[8]:
                print("  ERROR: expected the 9th (index 8) series to cycle back to the same hue as the 1st, got {} vs {}".format(colors[0], colors[8]))
                failed = True

        print("Test: the SoCPlanDrift chart branch renders end to end with its description and at least one 'Plan @' series")
        # get_chart()'s whole elif chain (every chart type, not just SoCPlanDrift) is gated behind
        # soc_kw_best being non-empty - it's read from a live published entity's "results"
        # attribute, not something this test can leave unset without just getting "Loading...".
        my_predbat.dashboard_values = {my_predbat.prefix + ".soc_kw_best": {"attributes": {"results": {now.strftime("%Y-%m-%dT%H:%M:%S%z"): 7.0}}}}
        chart_html = asyncio.run(w.get_chart("SoCPlanDrift"))
        if "Plan @" not in chart_html:
            print("  ERROR: expected at least one 'Plan @ ...' series name in the rendered chart output")
            failed = True
        if "name: 'Actual'" not in chart_html:
            print("  ERROR: expected the live Actual series to still be present alongside the drift series")
            failed = True

        print("Test: deselectAllPlans() is defined and lists every plan series name (but not 'Actual', which should stay visible)")
        if "function deselectAllPlans()" not in chart_html:
            print("  ERROR: expected a deselectAllPlans() function in the rendered chart output")
            failed = True
        else:
            plan_names_in_page = [s["name"] for s in series]
            for name in plan_names_in_page:
                if name not in chart_html:
                    print("  ERROR: expected deselectAllPlans() output to reference plan series {!r}".format(name))
                    failed = True
            deselect_block = chart_html[chart_html.index("function deselectAllPlans()") :]
            if "'Actual'" in deselect_block.split("</script>")[0]:
                print("  ERROR: 'Actual' should not be one of the series deselectAllPlans() hides")
                failed = True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed

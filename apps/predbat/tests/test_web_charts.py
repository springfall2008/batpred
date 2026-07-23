# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from web import WebInterface


def make_web(my_predbat):
    """Create a WebInterface instance bound to the given predbat."""
    return WebInterface(my_predbat, web_port=5053)


def run_web_charts_tests(my_predbat):
    """Unit tests for chart rendering - entities with a '%' unit must still be able to chart."""
    failed = 0
    print("**** Running web charts tests ****")

    web = make_web(my_predbat)
    now_str = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    series_data = [{"name": "SoC", "data": {"2026-07-23T10:00:00+00:00": 45.0}, "chart_type": "line"}]

    # -------------------------------------------------------------------------
    print("Test: render_chart() targets a percent-unit tagname via getElementById, not a CSS id selector")
    html = web.render_chart(series_data, "%", "SoC Chart", now_str, tagname="chart_%")
    if "querySelector('#" in html or 'querySelector("#' in html:
        print("  ERROR: render_chart() still targets the chart element via a '#id' CSS selector, which throws for a tagname like 'chart_%'")
        failed += 1
    if "getElementById('chart_%')" not in html:
        print(f"  ERROR: expected render_chart() to call getElementById('chart_%'), got: {html}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: render_timeline_chart() targets a percent-unit tagname via getElementById, not a CSS id selector")
    timeline_data = [{"name": "Status", "entity_id": "sensor.x", "data": {"2026-07-23T10:00:00+00:00": "on"}}]
    html = web.render_timeline_chart(timeline_data, "chart_%", 7)
    if "querySelector('#" in html or 'querySelector("#' in html:
        print("  ERROR: render_timeline_chart() still targets the chart element via a '#id' CSS selector, which throws for a tagname like 'chart_%'")
        failed += 1
    if "getElementById('chart_%')" not in html:
        print(f"  ERROR: expected render_timeline_chart() to call getElementById('chart_%'), got: {html}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: render_heatmap_chart() targets a percent-unit chart_id via getElementById, not a CSS id selector")
    html = web.render_heatmap_chart([{"name": "SoC", "data": [{"x": "Mon", "y": 45.0}]}], "SoC Heatmap", 0, 100, chart_id="chart_%")
    if "querySelector('#" in html or 'querySelector("#' in html:
        print("  ERROR: render_heatmap_chart() still targets the chart element via a '#id' CSS selector, which throws for a chart_id like 'chart_%'")
        failed += 1
    if "getElementById('chart_%')" not in html:
        print(f"  ERROR: expected render_heatmap_chart() to call getElementById('chart_%'), got: {html}")
        failed += 1

    return failed

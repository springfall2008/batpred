# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import re
from datetime import datetime, timedelta, timezone

from web import WebInterface


def make_web(my_predbat):
    """Create a WebInterface instance bound to the given predbat."""
    return WebInterface(my_predbat, web_port=5053)


def parse_timeline_ranges(html):
    """Extract the rendered timeline ranges as a list of (category, label, start_ms, end_ms)."""
    pattern = r"x: '([^']*)',\s*\n\s*y: \[(\d+), (\d+)\],\s*\n\s*fillColor: '[^']*',\s*\n\s*label: '([^']*)'"
    return [(m.group(1), m.group(4), int(m.group(2)), int(m.group(3))) for m in re.finditer(pattern, html)]


def run_web_timeline_fidelity_tests(web):
    """A flapping state must not be erased from the timeline chart, and same-named entities must not share a row."""
    failed = 0

    # -------------------------------------------------------------------------
    # A status sensor that alternates Normal/Lost every minute for six days. The chart used to
    # downsample the raw samples by array index, so whenever the step landed on one phase of the
    # flap the other state vanished entirely and the chart claimed one continuous multi-day run -
    # contradicting the history table, which samples the same data per 5/30-minute bucket.
    print("Test: render_timeline_chart() does not erase a state when downsampling a long flapping history")
    now = datetime(2026, 7, 25, 14, 0, 0, tzinfo=timezone.utc)
    samples = 8640
    flapping = {}
    stamp = now - timedelta(minutes=samples)
    for index in range(samples):
        flapping[stamp.strftime("%Y-%m-%dT%H:%M:%S%z")] = "Lost" if index % 2 else "Normal"
        stamp += timedelta(minutes=1)

    html = web.render_timeline_chart([{"name": "Status", "entity_id": "sensor.inverter_one_status", "data": flapping}], "chart_status", 7)
    ranges = parse_timeline_ranges(html)
    covered = {}
    for _, label, start, end in ranges:
        covered[label] = covered.get(label, 0) + (end - start)
    span = sum(covered.values())
    lost_share = covered.get("Lost", 0) / span if span else 0
    if lost_share < 0.25:
        print(f"  ERROR: 'Lost' holds half the history but the timeline chart gives it {lost_share:.1%} of the span - the other state was aliased over it")
        failed += 1
    longest = max(((end - start) / 60000 for _, label, start, end in ranges if label == "Normal"), default=0)
    if longest > 60:
        print(f"  ERROR: timeline chart reports a continuous {longest:.0f} minute 'Normal' run, but the state never stayed Normal for more than a minute")
        failed += 1

    # -------------------------------------------------------------------------
    # Two inverters both publish a status sensor whose friendly name is "Status", so using the
    # display name as the rangeBar category collapsed both onto a single y-axis row, making it
    # impossible to tell which bar belonged to which inverter.
    print("Test: render_timeline_chart() gives same-named entities distinct rows")
    both = [
        {"name": "Status", "entity_id": "sensor.inverter_one_status", "data": {"2026-07-23T10:00:00+00:00": "Normal"}},
        {"name": "Status", "entity_id": "sensor.inverter_two_status", "data": {"2026-07-23T10:00:00+00:00": "Lost"}},
    ]
    html = web.render_timeline_chart(both, "chart_status", 7)
    categories = {category for category, _, _, _ in parse_timeline_ranges(html)}
    if len(categories) < 2:
        print(f"  ERROR: two entities sharing the friendly name 'Status' collapsed onto one timeline row: {sorted(categories)}")
        failed += 1

    # -------------------------------------------------------------------------
    # get_history_with_now() appends the current state stamped in local time while HA/DB history
    # is UTC, so sorting the raw timestamp strings orders records by their text, not their instant.
    print("Test: render_timeline_chart() orders records by instant, not by raw timestamp string")
    mixed = {
        "2026-07-23T09:00:00+0000": "Normal",
        "2026-07-23T11:30:00+0200": "Lost",  # 09:30 UTC - sorts after "11:00" as text
        "2026-07-23T10:00:00+0000": "Normal",
    }
    html = web.render_timeline_chart([{"name": "Status", "entity_id": "sensor.inverter_one_status", "data": mixed}], "chart_status", 7)
    mixed_ranges = parse_timeline_ranges(html)
    latest_ms = int(datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    covered_to = max((end for _, _, _, end in mixed_ranges), default=0)
    if covered_to != latest_ms:
        print(f"  ERROR: timeline stops at {datetime.fromtimestamp(covered_to / 1000, timezone.utc)} instead of the newest record at 10:00 UTC - the 11:30+0200 record sorted last as text")
        failed += 1
    if [label for _, label, _, _ in mixed_ranges][:2] != ["Normal", "Lost"]:
        print(f"  ERROR: expected the 09:30 UTC 'Lost' record to sort between the 09:00 and 10:00 UTC records, got range labels {[label for _, label, _, _ in mixed_ranges]}")
        failed += 1

    return failed


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

    # -------------------------------------------------------------------------
    print("Test: render_heatmap_chart() sanitises chart_id before using it as a JS variable name")
    for bad_variable_name in ("height_chart_%", "chart_chart_%"):
        if bad_variable_name in html:
            print(f"  ERROR: render_heatmap_chart() interpolated an unsanitised chart_id into a JS identifier ('{bad_variable_name}'), which is a syntax error")
            failed += 1
    if "height_chart__" not in html:
        print(f"  ERROR: expected render_heatmap_chart() to declare a sanitised 'height_chart__' variable, got: {html}")
        failed += 1
    if "chart_chart__.render()" not in html:
        print(f"  ERROR: expected render_heatmap_chart() to render via a sanitised 'chart_chart__' variable, got: {html}")
        failed += 1

    failed += run_web_timeline_fidelity_tests(web)

    return failed

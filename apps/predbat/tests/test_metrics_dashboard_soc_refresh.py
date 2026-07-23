# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Test that the Metrics Dashboard's battery SoC doughnut chart center text reads live data on
refresh rather than being frozen at whatever it was on first page load - see issue #4151.

There's no JS execution/browser test infra in this codebase, so this can't run the embedded
JavaScript and check actual canvas output. Instead it checks the generated JS source itself:
the afterDraw plugin (Chart.js) must read the chart's own current data array and the always-
current MD_DATA global, not the pct/d values that were closed over when the chart was first
created in mdInitSOCChart - mdUpdateSOCChart (called on every 30s refresh once the chart
already exists) only mutates the chart's data array, it never recreates the chart, so a closure
over the original values would freeze the on-screen text at the first-load reading forever.
"""

from web_metrics_dashboard import get_metrics_dashboard_body


def test_soc_chart_center_text_reads_live_data(my_predbat):
    """
    The afterDraw plugin body must not reference the pct/d parameters closed over at chart
    creation time for the values it draws - it must read chart.data.datasets[0].data (updated
    by mdUpdateSOCChart on every refresh) and the MD_DATA global (reassigned on every refresh).
    """
    print("**** test_soc_chart_center_text_reads_live_data ****")

    body = get_metrics_dashboard_body("{}")

    # Isolate the md-center-text plugin's afterDraw function body, up to its closing "}]"
    marker = "id: 'md-center-text'"
    assert marker in body, "md-center-text plugin not found in dashboard body"
    start = body.index(marker)
    end = body.index("}]", start) + 2
    plugin_block = body[start:end]

    assert "chart.data.datasets[0].data[0]" in plugin_block, "afterDraw should read the live percentage from the chart's own current data, not a closed-over value"
    assert "MD_DATA.battery_soc_kwh" in plugin_block, "afterDraw should read the live kWh figure from the MD_DATA global, not a closed-over value"

    print("✓ afterDraw plugin reads live chart data and MD_DATA, not stale closure values")
    print("✓ Test passed")
    return False

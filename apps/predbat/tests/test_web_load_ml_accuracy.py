"""Human-readable LoadML accuracy status and chart-page integration tests."""

import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from web import WebInterface


def accuracy_web(attributes=None, mode="monitor", age_minutes=0):
    """A read-only web fixture with an accuracy report and no live server or HA calls."""
    now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
    base = SimpleNamespace(now_utc=now, args={"load_ml_enable": True, "load_ml_guard_mode": mode})
    base.get_arg = lambda key, default=None: base.args.get(key, default)
    web = WebInterface.__new__(WebInterface)
    web.base = base
    web.prefix = "predbat"
    report = {"attributes": attributes or {}, "last_updated": (now - timedelta(minutes=age_minutes)).isoformat()}
    web.get_state_wrapper = lambda *args, **kwargs: report
    web.get_header = lambda *args, **kwargs: '<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>'
    web.get_chart = lambda chart: '<p id="original-chart">Existing {} chart</p>'.format(chart)
    return web


def sample_attributes(reason="within_limits", selected="classic"):
    """A report with a genuine zero error, different winning sources and a tie."""
    return {
        "reason": reason,
        "selected_source": selected,
        "recommended_source": "classic",
        "fallback_horizon_hours": 8,
        "scores": {
            "1h": {"samples": 48, "ml_mae_kwh": 0.0, "classic_mae_kwh": 0.3},
            "8h": {"samples": 40, "ml_mae_kwh": 2.5, "classic_mae_kwh": 0.8},
            "24h": {"samples": 24, "ml_mae_kwh": 1.25, "classic_mae_kwh": 1.25},
        },
    }


class WebLoadMLAccuracyTests(unittest.TestCase):
    """Status, warnings, empty states and correct placement on existing chart pages."""

    def test_automatic_initial_state_explains_historical_first(self):
        """Collecting evidence must not imply that Automatic is already using ML."""
        html = accuracy_web(sample_attributes("measuring"), mode="auto").get_load_ml_accuracy_html()
        self.assertIn("Automatic mode starts with historical", html)
        self.assertIn("only permits LoadML after it earns selection", html)
        self.assertIn("Collecting evidence", html)
        self.assertIn('<span class="la-label">Planning source</span><strong>Historical forecast</strong>', html)

    def test_qualification_and_historical_preference_are_explicit(self):
        """The page distinguishes a clear earned advantage from insufficient improvement."""
        html = accuracy_web(sample_attributes("historical_preferred"), mode="auto").get_load_ml_accuracy_html()
        self.assertIn("Historical preferred", html)
        self.assertIn("both the 8-hour and 24-hour checks", html)
        html = accuracy_web(sample_attributes("ml_earned", "ml"), mode="auto").get_load_ml_accuracy_html()
        self.assertIn("LoadML earned selection", html)
        self.assertIn('<span class="la-label">Planning source</span><strong>LoadML</strong>', html)

    def test_both_loadml_pages_show_summary_above_chart(self):
        """The real route renders the summary before the chart placeholder on both ML tabs."""
        web = accuracy_web(sample_attributes())
        for chart in ("LoadML", "LoadMLPower"):
            response = asyncio.run(web.html_charts(SimpleNamespace(query={"chart": chart})))
            self.assertLess(response.text.index('<section class="load-accuracy"'), response.text.index('<div id="chart">'))
            self.assertIn('id="original-chart"', response.text)
            self.assertIn("Monitoring only", response.text)

    def test_other_charts_remain_unchanged(self):
        """Unrelated chart pages never fetch the accuracy report or receive a panel."""
        web = accuracy_web()

        def unexpected_read(*args, **kwargs):
            """Fail if the Battery page starts reading ML data."""
            raise AssertionError("Unrelated chart read accuracy data")

        web.get_state_wrapper = unexpected_read
        response = asyncio.run(web.html_charts(SimpleNamespace(query={"chart": "Battery"})))
        self.assertNotIn("load-accuracy", response.text)
        self.assertIn("Existing Battery chart", response.text)

    def test_empty_report_waits_without_zero_scores(self):
        """Missing results cannot look like perfect forecasting."""
        html = accuracy_web().get_load_ml_accuracy_html()
        self.assertIn("Collecting evidence", html)
        self.assertEqual(html.count("Waiting for results"), 3)
        self.assertNotIn("0.00 kWh", html)
        self.assertIn("Monitoring records and warns", html)

    def test_readable_comparison_and_real_zero(self):
        """Zero error is displayed, both winning sources are distinguished and ties stay neutral."""
        html = accuracy_web(sample_attributes()).get_load_ml_accuracy_html()
        self.assertIn("0.00 kWh", html)
        self.assertIn("2.50 kWh", html)
        self.assertIn("0.80 kWh", html)
        self.assertIn("40 completed windows", html)
        self.assertIn("<td>Historical</td>", html)
        self.assertIn("<td>LoadML</td>", html)
        self.assertIn("<td>Similar</td>", html)

    def test_monitor_warning_does_not_claim_a_switch(self):
        """An ML-selected monitor report warns while honestly showing ML still supplies planning."""
        html = accuracy_web(sample_attributes("ml_worse_than_classic", "ml")).get_load_ml_accuracy_html()
        self.assertIn("Warning: ML is less accurate", html)
        self.assertIn("does not automatically change forecast selection", html)
        self.assertIn("The 8-hour check", html)
        self.assertNotIn("Historical fallback active", html)
        self.assertIn('<span class="la-label">Planning source</span><strong>LoadML</strong>', html)

    def test_automatic_fallback_is_explicit(self):
        """The user sees both the action and the forecast currently used by the plan."""
        html = accuracy_web(sample_attributes("ml_worse_than_classic"), mode="auto").get_load_ml_accuracy_html()
        self.assertIn("Historical fallback active", html)
        self.assertIn("Automatic fallback", html)
        self.assertIn('<span class="la-label">Planning source</span><strong>Historical forecast</strong>', html)

    def test_stale_report_is_not_presented_as_current(self):
        """Old reports show their age problem, not an apparently healthy live source."""
        html = accuracy_web(sample_attributes(), age_minutes=30).get_load_ml_accuracy_html()
        self.assertIn("Report out of date", html)
        self.assertIn("Planning source (last report)", html)
        self.assertIn("Awaiting a fresh report", html)
        self.assertNotIn('role="status">Within guard limits', html)

    def test_current_report_time_overrides_unchanged_sensor(self):
        """Use the backend's report heartbeat, not the HA timestamp of the last value change."""
        from tests.test_load_forecast_guard import GuardBase, curve

        web = accuracy_web(age_minutes=30)
        backend = GuardBase()
        backend.now_utc = web.now_utc
        backend.minutes_now = 720
        backend.update_load_forecast_guard(curve(0.02), curve(0.01))
        attrs = backend.dashboard["sensor.predbat_load_ml_accuracy"]["attributes"]
        self.assertEqual(attrs["updated_at"], web.now_utc.isoformat())
        report = {"attributes": attrs, "last_updated": (web.now_utc - timedelta(minutes=30)).isoformat()}
        web.get_state_wrapper = lambda *args, **kwargs: report
        self.assertNotIn("Report out of date", web.get_load_ml_accuracy_html())

    def test_off_hides_previously_recorded_scores(self):
        """Disabling monitoring must not leave old numbers looking actively measured."""
        html = accuracy_web(sample_attributes(), mode="off").get_load_ml_accuracy_html()
        self.assertIn("Monitoring off", html)
        self.assertNotIn("2.50 kWh", html)
        self.assertIn("Not reported", html)

    def test_bad_data_cannot_break_or_inject_the_page(self):
        """Malformed attributes and non-finite errors render a safe empty state."""
        attrs = {"reason": {}, "selected_source": [], "recommended_source": "<script>alert(1)</script>", "scores": {"1h": {"samples": 2, "ml_mae_kwh": float("nan"), "classic_mae_kwh": float("inf")}, "8h": [], "24h": None}}
        html = accuracy_web(attrs).get_load_ml_accuracy_html()
        self.assertNotIn("<script>", html)
        self.assertEqual(html.count("Waiting for results"), 3)
        web = accuracy_web()
        web.get_state_wrapper = lambda *args, **kwargs: None
        self.assertIn("Collecting evidence", web.get_load_ml_accuracy_html())

    def test_guard_failure_reports_configured_sources(self):
        """A guard error explains the fallback and leaves the mode selector accessible."""
        html = accuracy_web({"reason": "guard_unavailable"}, mode="auto").get_load_ml_accuracy_html()
        self.assertIn("Accuracy guard unavailable", html)
        self.assertIn("Configured load sources", html)
        self.assertIn("Apply mode", html)

    def test_report_read_failure_leaves_mode_selector_accessible(self):
        """The chart still accepts mode changes when its accuracy report is unreadable."""
        web = accuracy_web()
        web.get_state_wrapper = Mock(side_effect=RuntimeError("report unavailable"))
        self.assertIn("Accuracy report unavailable", web.get_load_ml_accuracy_html())
        self.assertIn("Apply mode", web.get_load_ml_accuracy_html())

    def test_read_only_display_does_not_change_guard_or_configuration(self):
        """Rendering cannot change mode, source, stored evidence or the report."""
        attrs = sample_attributes("ml_worse_than_classic")
        original = copy.deepcopy(attrs)
        web = accuracy_web(attrs, mode="auto")
        before = copy.deepcopy(web.base.args)
        web.get_load_ml_accuracy_html()
        self.assertEqual(attrs, original)
        self.assertEqual(web.base.args, before)


def run_web_load_ml_accuracy_tests(my_predbat=None):
    """Run the UI suite from Predbat's standard test registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(WebLoadMLAccuracyTests))
    return not result.wasSuccessful()

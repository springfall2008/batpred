"""Exercise the preview through real native replay and read-only web entry points."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from joint_site_native import evaluate_joint_site
from prediction import Prediction
from tests.test_joint_site_native import _controls, _setup
from web import WebInterface


def test_real_preview_isolation(base):
    """Both displayed trajectories come from fresh native Predictions, never the active one."""
    _setup(base, pv=5)
    before = _controls(base)
    base.prediction = Prediction(base, base.pv_forecast_minute_step, base.pv_forecast_minute10_step, base.load_minutes_step, base.load_minutes_step10)
    active = base.prediction
    active.prediction_cache["preview_sentinel"] = {"unaltered": [1, 2]}
    captures = []
    original = Prediction.run_prediction

    def observe(pred, *args, **kwargs):
        """Retain the exact native trace solely for offline assertions."""
        value = original(pred, *args, **kwargs)
        captures.append((args[4], deepcopy(pred.predict_soc), pred.final_soc))
        return value

    with patch.object(Prediction, "run_prediction", observe), patch("joint_site_native._observation_clock", return_value=base.now_utc):
        report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "candidate", report
    assert set(report["preview"]) == {"nominal", "pv10"}
    assert _controls(base) == before and base.prediction is active
    assert active.prediction_cache["preview_sentinel"] == {"unaltered": [1, 2]}
    for scenario, trace, final in captures[-2:]:
        rows = report["preview"]["pv10" if scenario == 1 else "nominal"]
        assert sum(row["minutes"] for row in rows) == base.forecast_minutes
        assert abs(rows[0]["battery_start_percent"] - trace[0] * 100 / base.soc_max) < 0.011
        assert abs(rows[-1]["battery_end_percent"] - final * 100 / base.soc_max) < 0.011
        check = report["native_checks"]["pv10" if scenario == 1 else "nominal"]
        assert "_preview_rows" not in check
        assert abs(max(row["grid_peak_w"] for row in rows) - check["peak_import_w"]) <= 0.501
    base.args["joint_site"]["mode"] = "off"
    disabled = evaluate_joint_site(base, publish=False)
    assert disabled is None
    assert base.joint_site_report["status"] == "disabled" and "preview" not in base.joint_site_report


def test_preview_failure_is_not_control_failure(base):
    """A broken display adapter neither changes acceptance nor retains an old preview."""
    _setup(base, pv=5)
    base.joint_site_report = {"status": "candidate", "preview": {"nominal": [{"old": True}]}}
    before = _controls(base)
    with patch("joint_site_preview.build_replay_preview", side_effect=RuntimeError("private display failure")), patch("joint_site_native._observation_clock", return_value=base.now_utc):
        report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "candidate" and "preview" not in report
    assert all(check["passed"] and check["preview_unavailable"] for check in report["native_checks"].values())
    assert _controls(base) == before


def test_web_preview_entry_points(base):
    """The web view changes, not the native HA plan; unavailable candidates clear the table."""
    now = datetime.now(timezone.utc)
    native = {"timestamp": now.isoformat(), "rows": [{"soc_percent": 20, "state": "HoldChrg"}]}
    diagnostic = {"state": "exceeded", "attributes": {"limit_w": 6000, "peak_w": 7715.771, "minutes_over": 170, "enforced": False}}
    joint = {"status": "conditional_candidate", "generated_at": now.isoformat(), "uses_stale_soc_assumption": True, "execution_blockers": ["soc_entity_stale_or_future"], "native_checks": {}, "preview": {}}
    holder = SimpleNamespace(joint_site_report=joint, manual_times=lambda *args, **kwargs: [], manual_rates=lambda *args, **kwargs: {})
    page = WebInterface.__new__(WebInterface)
    page.base = holder
    page.prefix = "predbat"
    reads = []

    def read(entity_id, attribute=None, default=None, raw=False):
        """Serve only cached synthetic states; no services or hardware transport exist."""
        reads.append((entity_id, attribute, raw))
        if entity_id == "predbat.plan_html":
            return deepcopy(native)
        if entity_id == "predbat.best_import_limit":
            return deepcopy(diagnostic)
        return default

    page.get_state_wrapper = read
    page.get_header = lambda *args, **kwargs: "<html><body>"
    request = SimpleNamespace(rel_url=SimpleNamespace(query={}))
    before = deepcopy((native, diagnostic, joint))
    response = asyncio.run(page.html_api_plan_data(request))
    data = json.loads(response.text)
    assert data["plan"]["joint_simulation"] is True and data["plan"]["rows"] == []
    assert "joint_site_preview" in data and "170" in data["joint_site_preview"]
    assert "conditional" in data["joint_site_preview"].lower()
    html = asyncio.run(page.html_plan(request)).text
    assert 'id="jointSitePreview"' in html and 'id="planContainer"' in html
    assert "7716" in html or "7.72" in html
    assert "window.planData" in html and json.dumps(native) not in html
    assert '<table class="joint-site-preview-table"' not in html
    assert (native, diagnostic, joint) == before
    # Active reports refresh even with matching timestamps, so expiry clears the table.
    request.rel_url.query = {"newest_timestamp": (now + timedelta(days=1)).isoformat(), "overrides_hash": data["overrides_hash"]}
    cached = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert cached["unchanged"] is False
    holder.joint_site_report = None
    response = json.loads(asyncio.run(page.html_api_plan_data(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text)
    assert response["plan"] == native
    cached = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert cached["unchanged"] is True
    request.rel_url.query["joint_simulation"] = "1"
    transitioned = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert transitioned["unchanged"] is False and transitioned["plan"] == native
    assert any(row[0] == "predbat.best_import_limit" and row[2] for row in reads)


def run_joint_site_preview_integration_tests(base):
    """Run the actual-entry-point regressions using the repository registry convention."""
    failed = False
    for test in (test_real_preview_isolation, test_preview_failure_is_not_control_failure, test_web_preview_entry_points):
        print("Test: " + test.__name__)
        try:
            test(base)
        except Exception as exc:
            print("ERROR: {}: {}".format(test.__name__, exc))
            failed = True
    return failed

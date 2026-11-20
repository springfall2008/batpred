# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the PV Calibration page (WebInterface.get_chart("PVCalibration")): the status table and the slot and day evidence charts."""

import json
import math
import re
from datetime import datetime, timezone

from pv_calibration import fit_pv_calibration
from tests.test_pv_calibration import TODAY, day_before, make_day
from web import WebInterface


class StubSolar:
    """The solar component attributes the page reads."""

    def __init__(self, model=None, evidence=None, overridden=()):
        """A component with the given model and evidence."""
        self.pv_calibration_model = model
        self.pv_calibration_evidence = evidence
        self.pv_calibration_last_scan = datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
        self.pv_calibration_next_scan = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
        self.pv_calibration_last_success = self.pv_calibration_last_scan
        self.pv_calibration_scan_status = "ok: 2 days scanned, 28 stored"
        self.pv_calibration_settings_used = {"history_days": 28, "scan_days": 2, "half_life": 3.5, "shrink": 0.2, "shrink_slot": 0.2, "overridden": set(overridden)}
        self.pv_calibration_worst_scaling = 0.62
        self.pv_calibration_best_scaling = 1.21

    def pv_clipping_ceiling_kw(self):
        """No inverter limit set."""
        return 0


class StubComponents:
    """A components registry holding one solar component."""

    def __init__(self, solar):
        """Hold the solar component."""
        self.solar = solar

    def get_component(self, name):
        """Return the solar component for "solar"."""
        return self.solar if name == "solar" else None


def _make_web(my_predbat):
    """A WebInterface bound to my_predbat without starting the aiohttp app."""
    web = WebInterface.__new__(WebInterface)
    web.base = my_predbat
    web.log = my_predbat.log
    web.prefix = my_predbat.prefix
    web.local_tz = my_predbat.local_tz
    return web


def _data(html, name):
    """The JSON the page embeds as var <name> = ...;, as a dict, or None."""
    match = re.search(r"var {} = (.*?);\n".format(name), html)
    return json.loads(match.group(1)) if match else None


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def _evidence():
    """28 days at 0.8 with one down day."""
    evidence = {day_before(k): make_day(0.8) for k in range(1, 29)}
    evidence[day_before(5)] = make_day(0.02)
    return evidence


def run_web_pv_calibration_chart_tests(my_predbat):
    """Run the PV Calibration page tests."""
    failed = False
    saved_components = getattr(my_predbat, "components", None)
    saved_now_utc = my_predbat.now_utc
    # The stub's scans are fixed dates, and the page only offers a next midnight still ahead of now (#6244): pin now between them
    my_predbat.now_utc = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
    try:
        evidence = _evidence()
        model = fit_pv_calibration(evidence, TODAY, 1.0, 3.5, 0.2, 0.2, 30)
        web = _make_web(my_predbat)

        print("Test 1: the status table with a model")
        my_predbat.components = StubComponents(StubSolar(model, evidence))
        html = web.get_chart("PVCalibration")
        failed |= _check("t1 on", ">On<" in html, html[:1500])
        failed |= _check("t1 factors", "{:.2f}".format(model["total"]) in html and "{:.2f}".format(model["raw_total"]) in html)
        failed |= _check("t1 settings", "3.5" in html and "0.20" in html)
        failed |= _check("t1 last scan", "2026-11-16" in html)

        print("Test 2: the slot data")
        slot_data = _data(html, "pv_calibration_slot_data")
        failed |= _check("t2 present", slot_data is not None)
        if slot_data:
            failed |= _check("t2 points", len(slot_data["points"]) == len(model["slot_points"]), (len(slot_data["points"]), len(model["slot_points"])))
            failed |= _check("t2 adj", abs(slot_data["adj"]["720"] - model["adj"][720]) < 1e-3, slot_data["adj"].get("720"))

        print("Test 3: the day data marks the down day")
        day_data = _data(html, "pv_calibration_day_data")
        failed |= _check("t3 present", day_data is not None)
        if day_data:
            down = [point for point in day_data["points"] if point[3]]
            failed |= _check("t3 down", len(down) == 1 and down[0][0] == day_before(5), down)

        print("Test 4: an apps.yaml override is marked")
        my_predbat.components = StubComponents(StubSolar(model, evidence, overridden=("shrink",)))
        html = web.get_chart("PVCalibration")
        failed |= _check("t4 override", "(apps.yaml)" in html)

        print("Test 5: no evidence yet")
        my_predbat.components = StubComponents(StubSolar(None, {}))
        html = web.get_chart("PVCalibration")
        failed |= _check("t5 message", "not enough evidence" in html.lower(), html[:1500])
        failed |= _check("t5 no plots", _data(html, "pv_calibration_slot_data") is None and _data(html, "pv_calibration_day_data") is None)

        print("Test 6: no solar component")
        my_predbat.components = None
        html = web.get_chart("PVCalibration")
        failed |= _check("t6 message", "Solar forecast component is not running" in html, html[:800])

        print("Test 7: PVAccuracy opens the same page")
        my_predbat.components = StubComponents(StubSolar(model, evidence))
        failed |= _check("t7 alias", web.get_chart("PVAccuracy") == web.get_chart("PVCalibration"))

        print("Test 9: the slot chart plots time as numbers, so the axis runs in time order")
        html = web.get_chart("PVCalibration")
        failed |= _check("t9 numeric x", "tickvals" in html and "x: s.points.map(function(p) { return p[0] / 60; })" in html)

        print("Test 10: next scan shows the after-midnight scan, and the spread is a percentage of the factor")
        html = web.get_chart("PVCalibration")
        failed |= _check("t10 next scan", "2026-11-17 00:00" in html, re.findall(r"Next scan.*?</tr>", html))
        failed |= _check("t10 spread", "\u00b1{:.0f}%".format(100 * (math.exp(model["spread_day"]) - 1)) in html, re.findall(r"Day factor unshrunk.*?</tr>", html))

        print("Test 12: after a failed scan the next scan is the retry, not a midnight already past")
        failing = StubSolar(model, evidence)
        failing.pv_calibration_last_success = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
        failing.pv_calibration_next_scan = datetime(2099, 1, 1, 11, 0, tzinfo=timezone.utc)
        my_predbat.components = StubComponents(failing)
        html = web.get_chart("PVCalibration")
        failed |= _check("t12 no past midnight", "2026-11-15 00:00" not in html and "2099-01-01 11:00" in html, re.findall(r"Next scan.*?</tr>", html))
        my_predbat.components = StubComponents(StubSolar(model, evidence))

        print("Test 11: before the first run the text gives the default days, not blanks")
        quiet = StubSolar(None, {})
        quiet.pv_calibration_settings_used = None
        my_predbat.components = StubComponents(quiet)
        html = web.get_chart("PVCalibration")
        failed |= _check("t11 text", "over the last 28 days" in html and "every 3.5 days" in html, html[-1500:])
        my_predbat.components = StubComponents(StubSolar(model, evidence))

        print("Test 8: the text wraps and the page is width-capped")
        html = web.get_chart("PVCalibration")
        failed |= _check("t8 wrap", ".pv-calibration-page p { white-space: normal; }" in html and "max-width:960px" in html)
    finally:
        my_predbat.components = saved_components
        my_predbat.now_utc = saved_now_utc
    return failed

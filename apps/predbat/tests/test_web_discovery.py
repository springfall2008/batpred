# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the /discovery web page (WebInterface.html_discovery, web.py).

The page is the viewer for the observe-only discovery catalogue - the thing that makes the
catalogue testable by a user rather than only readable inside a debug dump. Two properties matter
more than the markup and are pinned here against the REAL Coordinator rather than a stub, so a
redaction regression fails this test too: the page is redacted by default, and ?raw=1 is the only
way to see identifying values.

The renderer is tested separately, against a hand-made catalogue, since
validate_report() would strip an unknown field long before it reached the page - the staleness this
guards against is a field the coordinator learns LATER, which the page must render without being
taught about it.
"""

import asyncio

from coordinator import Coordinator
from web import WebInterface


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing only the query string the handler reads."""

    def __init__(self, query=None):
        """Store the query string a handler will read."""
        self.query = query or {}


class _StubComponents:
    """Stand-in for components.Components: the coordinator plus the registry surface assemble() reads.

    Coordinator.assemble() builds a status entry for every component the registry knows about, so
    it calls get_all()/is_active()/is_alive()/load_error() - a bare coordinator attribute is not
    enough. Mirrors test_discovery_catalogue.py's own stub.
    """

    def __init__(self, coordinator, names=()):
        """Hold the coordinator; every name in `names` is treated as active and alive."""
        self.coordinator = coordinator
        self._names = list(names)

    def get_all(self):
        """Every component name this registry knows about."""
        return list(self._names)

    def is_active(self, name):
        """Whether the named component was constructed - all of them, in this stub."""
        return name in self._names

    def is_alive(self, name):
        """Whether the named component is running and fresh - all of them, in this stub."""
        return name in self._names

    def load_error(self, name):
        """Why the named component failed to construct - never, in this stub."""
        return None

    def get_component(self, name):
        """The component registered under this name - none, so the coordinator salts per-process."""
        return None


class _StubCoordinator:
    """A coordinator that hands back a fixed catalogue, for testing the renderer alone."""

    def __init__(self, catalogue):
        """Hold the catalogue both accessors will return."""
        self._catalogue = catalogue

    def catalogue(self):
        """The redacted catalogue - the fixed one, in this stub."""
        return self._catalogue

    def catalogue_raw(self):
        """The unredacted catalogue - the same fixed one, in this stub."""
        return self._catalogue


# One import meter whose MPAN must never appear on the redacted page, and one inverter carrying a
# serial, a model and an entity descriptor - between them covering every container class the page
# renders (pseudonymised, clear, numeric and descriptor).
SAMPLE_REPORT = {
    "automatic": True,
    "inverters": [
        {
            "device_id": "givtcp:CE2143G123",
            "inverter_type": "GE",
            "composition": "direct",
            "functions": ["solar", "battery"],
            "capabilities": ["rest_v3", "pause_mode"],
            "hardware_ids": {"serial": "CE2143G123"},
            "info": {"model": "Gen3", "firmware": "D0.450"},
            "ratings": {"battery_kwh": 9.5, "max_charge_w": 3000},
            "entities": {"charge_rate": {"entity_id": "number.givtcp_ce2143g123_charge_rate", "domain": "number", "access": "rw"}},
        }
    ],
    "meters": [
        {
            "device_id": "octopus:1200023305967",
            "direction": "import",
            "account_ids": {"mpan": "1200023305967"},
            "tariff": {"info": {"code": "E-1R-AGILE-24-10-01-A"}},
        }
    ],
}

# The MPAN seeded above - identifying, so the redacted page must not carry it in any form.
SAMPLE_MPAN = "1200023305967"


def _make_web(my_predbat, coordinator, names=("givtcp", "octopus", "gecloud")):
    """Build a minimal WebInterface bound to my_predbat, bypassing ComponentBase.__init__.

    Same pattern as test_web_chart_currency.py and test_web_debug_history_routes.py - the real
    __init__ would stand up the aiohttp app. Note this stubs my_predbat.components, which is
    shared across the whole test run, so every caller must restore it.
    """
    w = WebInterface.__new__(WebInterface)
    w.base = my_predbat
    w.log = my_predbat.log
    w.prefix = my_predbat.prefix
    my_predbat.components = _StubComponents(coordinator, names)
    return w


def _render(my_predbat, coordinator, query=None):
    """Render the discovery page and return its HTML body."""
    w = _make_web(my_predbat, coordinator)
    response = asyncio.run(w.html_discovery(FakeRequest(query)))
    return response.text


def _real_coordinator(my_predbat):
    """A real Coordinator carrying SAMPLE_REPORT, filed under "givtcp"/"octopus"."""
    coordinator = Coordinator(my_predbat)
    coordinator.report("givtcp", {"automatic": True, "inverters": SAMPLE_REPORT["inverters"]})
    coordinator.report("octopus", {"automatic": True, "meters": SAMPLE_REPORT["meters"]})
    return coordinator


def test_discovery_page_renders_the_catalogue(my_predbat):
    """The page shows the sections, the records in them and the per-section counts."""
    print("**** test_discovery_page_renders_the_catalogue ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        body = _render(my_predbat, _real_coordinator(my_predbat))

        for expected in ("Inverters", "Meters", "givtcp:CE2143G123", "Gen3", "D0.450", "rest_v3"):
            if expected not in body:
                print(f"  ERROR: expected the page to show {expected!r}")
                failed = True
        if "number.givtcp_ce2143g123_charge_rate" not in body:
            print("  ERROR: the entity descriptor should be rendered")
            failed = True
        if "E-1R-AGILE-24-10-01-A" not in body:
            print("  ERROR: a tariff code is a clear value and should be shown")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the discovery page renders the catalogue's sections and records")
    return failed


def test_discovery_page_is_redacted_unless_raw_is_asked_for(my_predbat):
    """The MPAN is pseudonymised by default and only visible with ?raw=1.

    Run against the real Coordinator and Redactor, so this fails if redaction regresses, not only
    if the page forgets which accessor to call.
    """
    print("**** test_discovery_page_is_redacted_unless_raw_is_asked_for ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        coordinator = _real_coordinator(my_predbat)

        redacted = _render(my_predbat, coordinator)
        if SAMPLE_MPAN in redacted:
            print(f"  ERROR: the MPAN {SAMPLE_MPAN} reached the default (redacted) page")
            failed = True
        if "#" not in redacted:
            print("  ERROR: expected a pseudonym token on the redacted page")
            failed = True

        raw = _render(my_predbat, coordinator, {"raw": "1"})
        if SAMPLE_MPAN not in raw:
            print(f"  ERROR: the MPAN {SAMPLE_MPAN} should be visible with ?raw=1 - that is the point of the raw view")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the page is redacted by default and shows identifying values only with ?raw=1")
    return failed


def test_discovery_page_labels_the_raw_view(my_predbat):
    """The raw view says so on the page, so a screenshot of it is never mistaken for shareable."""
    print("**** test_discovery_page_labels_the_raw_view ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        coordinator = _real_coordinator(my_predbat)
        raw = _render(my_predbat, coordinator, {"raw": "1"}).lower()
        if "raw" not in raw or "not safe to share" not in raw:
            print("  ERROR: the raw view should carry a visible warning that it is not safe to share")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the raw view is labelled as unsafe to share")
    return failed


def test_discovery_page_without_a_coordinator(my_predbat):
    """With no coordinator (standalone, or a failed load) the page renders an empty state, not a 500."""
    print("**** test_discovery_page_without_a_coordinator ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        w = WebInterface.__new__(WebInterface)
        w.base = my_predbat
        w.log = my_predbat.log
        w.prefix = my_predbat.prefix
        my_predbat.components = None

        response = asyncio.run(w.html_discovery(FakeRequest()))
        if response.status != 200:
            print(f"  ERROR: expected a rendered empty state, got status {response.status}")
            failed = True
        if "not available" not in response.text.lower():
            print("  ERROR: expected the page to explain that discovery is unavailable")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the page renders an empty state when there is no coordinator")
    return failed


def test_discovery_page_shows_conflicts(my_predbat):
    """A conflict is rendered prominently - it is the thing a maintainer most wants to spot."""
    print("**** test_discovery_page_shows_conflicts ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        # The same serial claimed by two components is exactly the duplicate_serial case.
        coordinator = Coordinator(my_predbat)
        inverter = {"device_id": "givtcp:CE2143G123", "inverter_type": "GE", "hardware_ids": {"serial": "CE2143G123"}}
        coordinator.report("givtcp", {"inverters": [inverter]})
        coordinator.report("gecloud", {"inverters": [dict(inverter, device_id="gecloud:CE2143G123")]})

        body = _render(my_predbat, coordinator)
        if "duplicate_serial" not in body:
            print("  ERROR: expected the duplicate_serial conflict to be rendered")
            failed = True
        if "givtcp" not in body or "gecloud" not in body:
            print("  ERROR: expected the conflict to name both claiming components")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: a conflict is rendered on the page")
    return failed


def test_discovery_page_renders_a_field_it_was_never_taught(my_predbat):
    """A field the renderer has never heard of is still shown.

    This is the property the generic renderer exists for. The catalogue is designed to grow new
    fields, and a hand-written per-section table would silently omit anything added later -
    exactly the failure an observe-only release exists to catch. Fed a hand-made catalogue rather
    than a real report, since validate_report() would drop an unknown field long before the page.
    """
    print("**** test_discovery_page_renders_a_field_it_was_never_taught ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        catalogue = {
            "schema_version": 1,
            "generated": "2026-09-20T12:00:00+00:00",
            "components": {},
            "inverters": [{"device_id": "future:1", "a_field_invented_tomorrow": "surprising-value", "nested_future": {"inner": "also-shown"}}],
            "observations": {"conflicts": []},
        }
        body = _render(my_predbat, _StubCoordinator(catalogue))

        for expected in ("a_field_invented_tomorrow", "surprising-value", "nested_future", "also-shown"):
            if expected not in body:
                print(f"  ERROR: the renderer dropped {expected!r} - it is not generic")
                failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the renderer shows fields it was never taught about")
    return failed


def test_discovery_page_escapes_rendered_values(my_predbat):
    """Vendor strings come from third-party APIs, so every rendered value is HTML-escaped."""
    print("**** test_discovery_page_escapes_rendered_values ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        catalogue = {
            "schema_version": 1,
            "generated": "2026-09-20T12:00:00+00:00",
            "components": {},
            "inverters": [{"device_id": "x:1", "info": {"model": "<script>alert('xss')</script>"}}],
            "observations": {"conflicts": []},
        }
        body = _render(my_predbat, _StubCoordinator(catalogue))

        if "<script>alert('xss')</script>" in body:
            print("  ERROR: a vendor-supplied value was rendered unescaped")
            failed = True
        if "&lt;script&gt;" not in body:
            print("  ERROR: expected the value to appear escaped")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: rendered values are HTML-escaped")
    return failed


def test_discovery_route_is_registered(my_predbat):
    """/discovery is wired into the router, so the nav link actually resolves."""
    print("**** test_discovery_route_is_registered ****")
    import inspect

    source = inspect.getsource(WebInterface.start)
    failed = False
    if '"/discovery"' not in source:
        print("  ERROR: expected /discovery to be registered on the router")
        failed = True
    if not failed:
        print("PASS: the /discovery route is registered")
    return failed


def run_web_discovery_tests(my_predbat):
    """Run every discovery web page test."""
    failed = False
    failed |= test_discovery_page_renders_the_catalogue(my_predbat)
    failed |= test_discovery_page_is_redacted_unless_raw_is_asked_for(my_predbat)
    failed |= test_discovery_page_labels_the_raw_view(my_predbat)
    failed |= test_discovery_page_without_a_coordinator(my_predbat)
    failed |= test_discovery_page_shows_conflicts(my_predbat)
    failed |= test_discovery_page_renders_a_field_it_was_never_taught(my_predbat)
    failed |= test_discovery_page_escapes_rendered_values(my_predbat)
    failed |= test_discovery_route_is_registered(my_predbat)
    return failed

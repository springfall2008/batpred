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
import json

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


# One import meter carrying an MPAN and a login email (the email must never appear on the default
# page), and one inverter carrying a serial, a model and an entity descriptor - between them covering
# every container the page renders (account ids, strings, numbers and descriptors).
SAMPLE_REPORT = {
    "automatic": True,
    "inverters": [
        {
            "device_id": "givtcp:CE2143G123",
            "inverter_type": "GE",
            "composition": "direct",
            "functions": ["solar", "battery"],
            "capabilities": {"support_charge_freeze": True, "can_span_midnight": True},
            "flags": ["rest_v3"],
            "hardware_ids": {"serial": "CE2143G123"},
            "info": {"model": "Gen3", "firmware": "D0.450"},
            "ratings": {"soc_max": 9.5, "battery_rate_max": 3000},
            "entities": {"charge_rate": {"entity_id": "number.givtcp_ce2143g123_charge_rate", "domain": "number", "access": "rw"}},
        }
    ],
    "meters": [
        {
            "device_id": "octopus:1200023305967",
            "direction": "import",
            "account_ids": {"mpan": "1200023305967", "login": "jane@example.com"},
            "tariff": {"info": {"code": "E-1R-AGILE-24-10-01-A"}},
        }
    ],
}

# The MPAN seeded above - an identifier, so it is published on both views
SAMPLE_MPAN = "1200023305967"
# The login email seeded above - withheld from the default view, shown only with ?raw=1
SAMPLE_EMAIL = "jane@example.com"


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

        for expected in ("Inverters", "Meters", "givtcp:CE2143G123", "Gen3", "D0.450", "rest_v3", "support_charge_freeze", "soc_max"):
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
    """The email is withheld by default and only visible with ?raw=1; the MPAN is an identifier and shows on both.

    Run against the real Coordinator and Redactor, so this fails if redaction regresses, not only
    if the page forgets which accessor to call.
    """
    print("**** test_discovery_page_is_redacted_unless_raw_is_asked_for ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        coordinator = _real_coordinator(my_predbat)

        redacted = _render(my_predbat, coordinator)
        if SAMPLE_EMAIL in redacted:
            print(f"  ERROR: the email {SAMPLE_EMAIL} reached the default (redacted) page")
            failed = True
        if SAMPLE_MPAN not in redacted:
            print(f"  ERROR: the MPAN {SAMPLE_MPAN} is an identifier and should be readable on the default page")
            failed = True

        raw = _render(my_predbat, coordinator, {"raw": "1"})
        if SAMPLE_EMAIL not in raw:
            print(f"  ERROR: the email {SAMPLE_EMAIL} should be visible with ?raw=1 - that is the point of the raw view")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the page withholds the email by default and shows it only with ?raw=1; the MPAN shows on both")
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
    if '"/api/discovery"' not in source:
        print("  ERROR: expected /api/discovery to be registered on the router")
        failed = True
    if not failed:
        print("PASS: the /discovery route is registered")
    return failed


class _StubComponentsPage(_StubComponents):
    """_StubComponents plus the registry surface the Components page reads to draw its cards."""

    def get_active(self):
        """Every active component name - all of them, in this stub."""
        return list(self._names)

    def can_restart(self, name):
        """Whether the named component can be restarted - never, in this stub."""
        return False

    def last_updated_time(self, name):
        """When the named component last updated - never, in this stub."""
        return None

    def get_error_count(self, name):
        """How many errors the named component has logged - none, in this stub."""
        return 0

    def get_component(self, name):
        """A component instance with no build_discovery(), like Web or Storage on a real install.

        The base stub returns None, which the coordinator counts as reportable (see _can_report),
        so every active component would appear in the status map. A real active component always
        resolves to an instance, and one without a reporter is left out of the map.
        """
        return object()


def _render_component_discovery(my_predbat, coordinator, name, names=("givtcp", "octopus", "gecloud")):
    """Render the per-component discovery pop-up body for `name` and return the response."""
    w = _make_web(my_predbat, coordinator, names)
    return asyncio.run(w.html_component_discovery(FakeRequest({"name": name})))


def _render_components_page(my_predbat, coordinator, names):
    """Render the Components page against a stub registry and return its HTML body."""
    w = _make_web(my_predbat, coordinator, names)
    my_predbat.components = _StubComponentsPage(coordinator, names)
    w.get_arg = lambda *args, **kwargs: None
    w.count_entities_matching_filter = lambda event_filter: 0
    return asyncio.run(w.html_components(FakeRequest())).text


def test_component_discovery_shows_only_that_components_records(my_predbat):
    """The pop-up for one component shows its own devices and nothing another component reported."""
    print("**** test_component_discovery_shows_only_that_components_records ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        coordinator = _real_coordinator(my_predbat)
        response = _render_component_discovery(my_predbat, coordinator, "givtcp")
        body = response.text
        if response.status != 200:
            print(f"  ERROR: expected status 200, got {response.status}")
            failed = True
        for expected in ("givtcp:CE2143G123", "CE2143G123", "Gen3", "D0.450"):
            if expected not in body:
                print(f"  ERROR: expected the GivTCP pop-up to show {expected!r}")
                failed = True
        if SAMPLE_MPAN in body or "E-1R-AGILE-24-10-01-A" in body:
            print("  ERROR: the GivTCP pop-up showed a record the Octopus component reported")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the pop-up shows only the chosen component's records")
    return failed


def test_component_discovery_glance_summarises_the_record(my_predbat):
    """The at-a-glance summary carries identity, ratings, functions, capabilities and what Predbat controls.

    Built by rule from the record's own containers, so every value here comes through without the
    web page naming a single field. Entity ids live only in the folded full record.
    """
    print("**** test_component_discovery_glance_summarises_the_record ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        body = _render_component_discovery(my_predbat, _real_coordinator(my_predbat), "givtcp").text
        details_at = body.find("<details")
        glance = body[:details_at] if details_at >= 0 else body
        # Labels are the field names with underscores turned into spaces
        for expected in ("serial", "CE2143G123", "firmware", "D0.450", "soc max", "9.5", "battery rate max", "3000", "solar", "battery", "support charge freeze", "can span midnight", "rest_v3", "charge rate"):
            if expected not in glance:
                print(f"  ERROR: expected the at-a-glance summary to show {expected!r}")
                failed = True
        if "discovery-chip" not in glance:
            print("  ERROR: expected functions and capabilities to be drawn as chips")
            failed = True
        if "number.givtcp_ce2143g123_charge_rate" in glance:
            print("  ERROR: an entity id belongs in the folded full record, not the summary")
            failed = True
        if details_at < 0 or "number.givtcp_ce2143g123_charge_rate" not in body[details_at:]:
            print("  ERROR: expected the full record, entities included, folded under a <details>")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the summary is built from the record and the full record is folded below it")
    return failed


def test_component_discovery_shows_ids_and_withholds_email(my_predbat):
    """The pop-up is the published view: the MPAN is readable and the login email is not."""
    print("**** test_component_discovery_shows_ids_and_withholds_email ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        body = _render_component_discovery(my_predbat, _real_coordinator(my_predbat), "octopus").text
        if SAMPLE_MPAN not in body:
            print(f"  ERROR: the MPAN {SAMPLE_MPAN} is an identifier and should be readable")
            failed = True
        if SAMPLE_EMAIL in body:
            print(f"  ERROR: the email {SAMPLE_EMAIL} reached the pop-up")
            failed = True
        if "E-1R-AGILE-24-10-01-A" not in body:
            print("  ERROR: expected the meter's tariff to be shown")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the pop-up shows identifiers and withholds the email")
    return failed


def test_component_discovery_explains_a_silent_component(my_predbat):
    """A component that should report but has not says why the pop-up is empty."""
    print("**** test_component_discovery_explains_a_silent_component ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        catalogue = {
            "schema_version": 2,
            "generated": "2026-11-03T01:00:00+00:00",
            "components": {"fox": {"status": "no_report", "reported_at": None}, "solis": {"status": "load_error", "reported_at": None}},
            "inverters": [],
            "observations": {"conflicts": []},
        }
        body = _render_component_discovery(my_predbat, _StubCoordinator(catalogue), "fox").text.lower()
        if "not reported" not in body:
            print("  ERROR: expected a no_report component to say it has not reported yet")
            failed = True
        body = _render_component_discovery(my_predbat, _StubCoordinator(catalogue), "solis").text.lower()
        if "failed to load" not in body:
            print("  ERROR: expected a load_error component to say it failed to load")
            failed = True

        # A component that failed to load is in the status map whether or not it ever had a
        # reporter (there is no instance left to ask), so storage - which never reports - gets the
        # pop-up too. Nothing it says may claim the component was expected to report.
        catalogue["components"]["storage"] = {"status": "load_error", "reported_at": None}
        for name in ("fox", "solis", "storage"):
            body = _render_component_discovery(my_predbat, _StubCoordinator(catalogue), name).text.lower()
            if "expected to report" in body or "has not reported anything" in body:
                print(f"  ERROR: the {name} pop-up claims the component was expected to report, or repeats itself: {body[:200]!r}")
                failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: a silent component explains why it has nothing to show")
    return failed


def test_component_discovery_without_a_coordinator(my_predbat):
    """With no coordinator the pop-up says discovery is unavailable rather than failing."""
    print("**** test_component_discovery_without_a_coordinator ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        w = WebInterface.__new__(WebInterface)
        w.base = my_predbat
        w.log = my_predbat.log
        w.prefix = my_predbat.prefix
        my_predbat.components = None
        response = asyncio.run(w.html_component_discovery(FakeRequest({"name": "givtcp"})))
        if response.status != 200 or "not available" not in response.text.lower():
            print(f"  ERROR: expected a 200 explaining discovery is not available, got {response.status}: {response.text[:120]!r}")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the pop-up degrades to a message without a coordinator")
    return failed


def test_component_discovery_renders_unknown_fields_and_escapes(my_predbat):
    """A field or container the page was never taught still shows in the summary, and vendor strings are escaped."""
    print("**** test_component_discovery_renders_unknown_fields_and_escapes ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        catalogue = {
            "schema_version": 2,
            "generated": "2026-11-03T01:00:00+00:00",
            "components": {"future": {"status": "ok", "reported_at": "2026-11-03T00:59:00+00:00", "counts": {"inverters": 1}}},
            "inverters": [
                {
                    "source": "future",
                    "device_id": "future:1",
                    "a_field_invented_tomorrow": "surprising-value",
                    "container_invented_tomorrow": {"inner_key": "also-shown"},
                    "info": {"model": "<script>alert('xss')</script>"},
                }
            ],
            "observations": {"conflicts": []},
        }
        body = _render_component_discovery(my_predbat, _StubCoordinator(catalogue), "future").text
        glance = body[: body.find("<details")]
        for expected in ("a field invented tomorrow", "surprising-value", "inner key", "also-shown"):
            if expected not in glance:
                print(f"  ERROR: the summary dropped {expected!r} - it must be generic")
                failed = True
        if "<script>alert('xss')</script>" in body or "&lt;script&gt;" not in body:
            print("  ERROR: a vendor-supplied value was not escaped")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: unknown fields reach the summary and values are escaped")
    return failed


def test_component_discovery_places_every_container():
    """Every record container and sub-record is placed in the summary or the fold, so none goes missing unseen.

    A new container in coordinator.CONTAINER_SPEC (or a new sub-record in SECTION_SPEC) fails this
    until someone decides where the pop-up shows it.
    """
    print("**** test_component_discovery_places_every_container ****")
    from coordinator import CONTAINER_SPEC, SECTION_SPEC
    from web import DISCOVERY_FOLDED_CONTAINERS, DISCOVERY_GLANCE_CONTAINERS

    expected = set(CONTAINER_SPEC)
    for spec in SECTION_SPEC.values():
        expected |= set(spec["sub_records"])
    placed = set(DISCOVERY_GLANCE_CONTAINERS) | set(DISCOVERY_FOLDED_CONTAINERS)
    failed = False
    missing = sorted(expected - placed)
    if missing:
        print(f"  ERROR: containers not placed in the pop-up's summary or fold: {missing}")
        failed = True
    both = sorted(set(DISCOVERY_GLANCE_CONTAINERS) & set(DISCOVERY_FOLDED_CONTAINERS))
    if both:
        print(f"  ERROR: containers placed in both the summary and the fold: {both}")
        failed = True
    if not failed:
        print("PASS: every container is placed in the summary or the fold")
    return failed


def test_components_page_offers_discovery_info(my_predbat):
    """A component in the catalogue's status map gets the info button and a Discovery line; one with no reporter gets neither."""
    print("**** test_components_page_offers_discovery_info ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        coordinator = _real_coordinator(my_predbat)
        body = _render_components_page(my_predbat, coordinator, ("givtcp", "octopus", "web"))
        for name in ("givtcp", "octopus"):
            if f"showDiscoveryModal('{name}')" not in body:
                print(f"  ERROR: expected an info button opening {name}'s discovery")
                failed = True
        if "showDiscoveryModal('web')" in body:
            print("  ERROR: a component that never reports should get no info button")
            failed = True
        if "1 inverter" not in body or "1 meter" not in body:
            print("  ERROR: expected each card's Discovery line to summarise what it reported")
            failed = True
        if 'id="discoveryModal"' not in body:
            print("  ERROR: expected the discovery pop-up container on the page")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the Components page offers discovery info on reporting components only")
    return failed


def test_component_discovery_route_is_registered(my_predbat):
    """/component_discovery is wired into the router, so the info button's fetch resolves."""
    print("**** test_component_discovery_route_is_registered ****")
    import inspect

    failed = '"/component_discovery"' not in inspect.getsource(WebInterface.start)
    if failed:
        print("  ERROR: expected /component_discovery to be registered on the router")
    else:
        print("PASS: the /component_discovery route is registered")
    return failed


def test_discovery_api_is_redacted_by_default(my_predbat):
    """The API withholds emails by default and keeps identifiers readable, like the classic page."""
    print("**** test_discovery_api_is_redacted_by_default ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        w = _make_web(my_predbat, _real_coordinator(my_predbat))
        redacted = json.loads(asyncio.run(w.html_api_discovery(FakeRequest())).text)
        raw = json.loads(asyncio.run(w.html_api_discovery(FakeRequest({"raw": "1"}))).text)
        if SAMPLE_EMAIL in json.dumps(redacted):
            print("  ERROR: the default API response exposed an email")
            failed = True
        if SAMPLE_EMAIL not in json.dumps(raw):
            print("  ERROR: raw mode did not return the email")
            failed = True
        if any(SAMPLE_MPAN not in json.dumps(response) for response in (redacted, raw)):
            print("  ERROR: the API hid a readable identifier")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the Discovery API is redacted by default")
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
    failed |= test_discovery_api_is_redacted_by_default(my_predbat)
    failed |= test_component_discovery_shows_only_that_components_records(my_predbat)
    failed |= test_component_discovery_glance_summarises_the_record(my_predbat)
    failed |= test_component_discovery_shows_ids_and_withholds_email(my_predbat)
    failed |= test_component_discovery_explains_a_silent_component(my_predbat)
    failed |= test_component_discovery_without_a_coordinator(my_predbat)
    failed |= test_component_discovery_renders_unknown_fields_and_escapes(my_predbat)
    failed |= test_component_discovery_places_every_container()
    failed |= test_components_page_offers_discovery_info(my_predbat)
    failed |= test_component_discovery_route_is_registered(my_predbat)
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Components page layout at phone width (get_components_css, WebInterface.html_components).

On a phone the page was wider than the screen and each card's header stacked one item per line:
the grid's 400px column minimum (and the auto minimum of a 1fr track) let a card grow past the
screen, the phone media query turned the card header into a column, and the heading row's
no-wrap pieces pushed the page wider still. These pin the three rules that fixed it. They read the
CSS and markup rather than a rendered page, so they guard against a rule being reverted - the
layout itself was checked by rendering the page in a phone-width browser.
"""

import asyncio
import re

from web import WebInterface
from web_helper import get_components_css


class _FakeRequest:
    """A minimal aiohttp-request stand-in with an empty query string."""

    query = {}


class _StubComponents:
    """The registry surface html_components reads, with every named component active and healthy."""

    coordinator = None

    def __init__(self, names):
        """Hold the component names this registry knows about."""
        self._names = list(names)

    def get_all(self):
        """Every component name."""
        return list(self._names)

    def get_active(self):
        """Every active component name - all of them, in this stub."""
        return list(self._names)

    def is_alive(self, name):
        """Whether the named component is running - always, in this stub."""
        return True

    def load_error(self, name):
        """Why the named component failed to construct - never, in this stub."""
        return None

    def can_restart(self, name):
        """Whether the named component can be restarted - always, in this stub."""
        return True

    def last_updated_time(self, name):
        """When the named component last updated - never, in this stub."""
        return None

    def get_error_count(self, name):
        """How many errors the named component has logged - none, in this stub."""
        return 0

    def get_component(self, name):
        """The instance registered under this name - none, so the page header shows no chat link."""
        return None


def _media_block(css, query):
    """Return the body of the first @media block whose query contains `query`, or ""."""
    start = css.find("@media")
    while start >= 0:
        brace = css.find("{", start)
        if query in css[start:brace]:
            depth, index = 1, brace + 1
            while depth and index < len(css):
                depth += {"{": 1, "}": -1}.get(css[index], 0)
                index += 1
            return css[brace + 1 : index - 1]
        start = css.find("@media", brace)
    return ""


def _rule(css, selector):
    """Return the declarations of the first rule for exactly `selector` in `css`, or ""."""
    match = re.search(r"^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.MULTILINE)
    return match.group(1) if match else ""


def test_components_grid_never_wider_than_the_screen():
    """No grid column minimum can exceed the screen: the 400px floor is clamped and the phone track can shrink to zero."""
    print("**** test_components_grid_never_wider_than_the_screen ****")
    css = get_components_css()
    failed = False
    grid = _rule(css, ".components-grid")
    if "minmax(min(400px, 100%), 1fr)" not in grid:
        print(f"  ERROR: expected the desktop grid's 400px column floor to be clamped to the container, got: {grid.strip()}")
        failed = True
    phone_grid = _rule(_media_block(css, "max-width: 768px"), ".components-grid")
    if "minmax(0, 1fr)" not in phone_grid:
        print(f"  ERROR: expected the phone grid track to be able to shrink below its content's width, got: {phone_grid.strip()}")
        failed = True
    if "min-width: 0" not in _rule(css, ".component-card"):
        print("  ERROR: expected .component-card to allow shrinking (min-width: 0)")
        failed = True
    if not failed:
        print("PASS: the components grid cannot grow wider than the screen")
    return failed


def test_components_card_header_is_a_row_on_a_phone():
    """On a phone the card header keeps the status and buttons on one wrapping row, under the name."""
    print("**** test_components_card_header_is_a_row_on_a_phone ****")
    phone = _media_block(get_components_css(), "max-width: 768px")
    failed = False
    header = _rule(phone, ".component-header")
    if "flex-direction: column" in header:
        print("  ERROR: the phone card header is still a column - every item gets its own line")
        failed = True
    if "flex-wrap: wrap" not in header:
        print(f"  ERROR: expected the phone card header to wrap, got: {header.strip()}")
        failed = True
    if "flex-basis: 100%" not in _rule(phone, ".component-header h3"):
        print("  ERROR: expected the component name to take its own line above the status and buttons on a phone")
        failed = True
    if not failed:
        print("PASS: the phone card header is a wrapping row under the name")
    return failed


def test_components_heading_row_wraps(my_predbat):
    """The heading row (title, show-disabled toggle, totals) wraps instead of pushing the page wider than a phone."""
    print("**** test_components_heading_row_wraps ****")
    failed = False
    saved = getattr(my_predbat, "components", None)
    try:
        w = WebInterface.__new__(WebInterface)
        w.base = my_predbat
        w.log = my_predbat.log
        w.prefix = my_predbat.prefix
        w.get_arg = lambda *args, **kwargs: None
        w.count_entities_matching_filter = lambda event_filter: 0
        my_predbat.components = _StubComponents(["givtcp", "web"])
        body = asyncio.run(w.html_components(_FakeRequest())).text
        heading = body[: body.find("Component Status")]
        heading = heading[heading.rfind("<div") :]
        if "flex-wrap: wrap" not in heading:
            print(f"  ERROR: expected the heading row to wrap, got: {heading.strip()}")
            failed = True
    finally:
        my_predbat.components = saved

    if not failed:
        print("PASS: the heading row wraps")
    return failed


def run_web_components_layout_tests(my_predbat):
    """Run every Components page layout test."""
    failed = False
    failed |= test_components_grid_never_wider_than_the_screen()
    failed |= test_components_card_header_is_a_row_on_a_phone()
    failed |= test_components_heading_row_wraps(my_predbat)
    return failed

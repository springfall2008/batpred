# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the three debug-history web routes (#4417) - previously untested per
@springfall2008's #4438 review (item 21). Follows test_web_annual.py's FakeRequest pattern.
"""

import asyncio
import tempfile
import shutil
from types import SimpleNamespace

from web import WebInterface
from debug_history import capture_snapshot
from storage import StorageLocalFiles


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing only what the handlers read."""

    def __init__(self, query=None):
        """Store the query string a handler will read."""
        self.query = query or {}


def _make_web(my_predbat, storage=None):
    """Build a minimal WebInterface bound to my_predbat, bypassing ComponentBase.__init__
    (which would stand up the real aiohttp app) - same pattern as test_web_chart_currency.py.

    Note this replaces my_predbat.components, which is shared with every other test in the run - the
    caller is responsible for putting the original back (see the finally block below).
    """
    w = WebInterface.__new__(WebInterface)
    w.base = my_predbat
    w.log = my_predbat.log
    w.prefix = my_predbat.prefix
    my_predbat.components = SimpleNamespace(get_component=lambda name: storage if name == "storage" else None)
    return w


def test_web_debug_history_routes(my_predbat):
    """Verify the list/download/download-all routes against a real StorageLocalFiles backend:
    empty-storage 404s, a real multi-snapshot round trip, "latest" resolving correctly (and
    matching the snapshot actually loaded, not a separately re-resolved one - #4438 review item
    4), and that an unknown/malicious id is HTML-escaped in the 404 body (item 6).
    """
    failed = False
    print("**** Testing debug-history web routes ****")

    tmpdir = tempfile.mkdtemp(prefix="predbat_test_debug_history_routes_")
    # _make_web() swaps components out for a stub on the shared my_predbat, so it has to go back
    # afterwards. Left in place, the next test to reach is_running() dies on
    # components.is_all_alive(), which the stub does not have - test_web_functions is the one that
    # trips over it, several tests later and with nothing to point back to here.
    saved_components = my_predbat.components
    try:
        print("Test: no storage component available - list is empty, downloads 404 without raising")
        w_no_storage = _make_web(my_predbat, storage=None)
        snapshots = asyncio.run(w_no_storage.html_debug_history_list(FakeRequest()))
        if snapshots.status != 200 or snapshots.text != "[]":
            print("  ERROR: expected an empty JSON list with no storage, got status={} body={!r}".format(snapshots.status, snapshots.text))
            failed = True
        resp = asyncio.run(w_no_storage.html_debug_history_download(FakeRequest()))
        if resp.status != 404:
            print("  ERROR: expected 404 downloading with no storage, got {}".format(resp.status))
            failed = True
        resp = asyncio.run(w_no_storage.html_debug_history_download_all(FakeRequest()))
        if resp.status != 404:
            print("  ERROR: expected 404 for download-all with no storage, got {}".format(resp.status))
            failed = True

        storage = StorageLocalFiles(tmpdir, my_predbat.log)
        w = _make_web(my_predbat, storage=storage)

        print("Test: empty storage (present, nothing captured yet) behaves the same as no storage")
        resp = asyncio.run(w.html_debug_history_download(FakeRequest()))
        if resp.status != 404:
            print("  ERROR: expected 404 downloading with nothing captured, got {}".format(resp.status))
            failed = True

        print("Test: unknown id 404s, and the id is HTML-escaped in the response body (#4438 review item 6)")
        malicious_id = "<script>alert(1)</script>"
        resp = asyncio.run(w.html_debug_history_download(FakeRequest(query={"id": malicious_id})))
        if resp.status != 404:
            print("  ERROR: expected 404 for an unknown id, got {}".format(resp.status))
            failed = True
        if "<script>" in resp.text:
            print("  ERROR: expected the malicious id to be HTML-escaped in the 404 body, got {!r}".format(resp.text))
            failed = True
        if "&lt;script&gt;" not in resp.text:
            print("  ERROR: expected the escaped id to appear in the 404 body, got {!r}".format(resp.text))
            failed = True

        import datetime

        now = datetime.datetime(2026, 8, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)
        first_id = asyncio.run(capture_snapshot(storage, "marker: first\n", now - datetime.timedelta(hours=3), max_count=15))
        newest_id = asyncio.run(capture_snapshot(storage, "marker: newest\n", now, max_count=15))

        print("Test: list returns both snapshots, newest first")
        resp = asyncio.run(w.html_debug_history_list(FakeRequest()))
        if resp.status != 200:
            print("  ERROR: expected 200 from the list route, got {}".format(resp.status))
            failed = True
        if first_id not in resp.text or newest_id not in resp.text:
            print("  ERROR: expected both snapshot ids in the list response, got {!r}".format(resp.text))
            failed = True
        if resp.text.index(newest_id) > resp.text.index(first_id):
            print("  ERROR: expected the newest snapshot to be listed first")
            failed = True

        print("Test: downloading by explicit id returns that snapshot's own data and filename")
        resp = asyncio.run(w.html_debug_history_download(FakeRequest(query={"id": first_id})))
        if resp.status != 200 or resp.body != b"marker: first\n":
            print("  ERROR: expected first_id's own data, got status={} body={!r}".format(resp.status, resp.body))
            failed = True
        if first_id not in resp.headers.get("Content-Disposition", ""):
            print("  ERROR: expected first_id in the Content-Disposition filename, got {!r}".format(resp.headers.get("Content-Disposition")))
            failed = True

        print("Test: 'latest' (explicit and omitted) resolves to the newest snapshot, id and data match (#4438 review item 4)")
        resp = asyncio.run(w.html_debug_history_download(FakeRequest(query={"id": "latest"})))
        if resp.status != 200 or resp.body != b"marker: newest\n":
            print("  ERROR: expected the newest snapshot's data for id=latest, got status={} body={!r}".format(resp.status, resp.body))
            failed = True
        if newest_id not in resp.headers.get("Content-Disposition", ""):
            print("  ERROR: expected the resolved newest id in the filename for id=latest, got {!r}".format(resp.headers.get("Content-Disposition")))
            failed = True
        resp_omitted = asyncio.run(w.html_debug_history_download(FakeRequest()))
        if resp_omitted.body != b"marker: newest\n":
            print("  ERROR: expected omitting id entirely to behave the same as id=latest")
            failed = True

        print("Test: download-all bundles every retained snapshot into one archive")
        resp = asyncio.run(w.html_debug_history_download_all(FakeRequest()))
        if resp.status != 200 or resp.content_type != "application/gzip":
            print("  ERROR: expected a 200 gzip response from download-all, got status={} content_type={}".format(resp.status, resp.content_type))
            failed = True
        if not resp.body or len(resp.body) == 0:
            print("  ERROR: expected a non-empty archive body")
            failed = True

    finally:
        my_predbat.components = saved_components
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed


def test_web_debug_history_routes_restores_components(my_predbat):
    """The routes test must leave my_predbat.components exactly as it found it.

    my_predbat is shared across the whole run, so a stub left behind here surfaces as an
    AttributeError in an unrelated test much later - is_running() calling components.is_all_alive()
    on a SimpleNamespace. Asserted on identity: whatever the caller had before is what it must get
    back, and a different-but-plausible object would be just as wrong for whoever ran first.

    Note is_running() guards with "if self.components", so the harness leaving this as None is
    harmless - it is specifically a truthy stub missing the method that breaks things, which is why
    a straight truthiness check would not catch this.
    """
    print("**** Testing debug-history routes leave components intact ****")
    failed = False
    before = my_predbat.components

    test_web_debug_history_routes(my_predbat)

    if my_predbat.components is not before:
        print("  ERROR: components was not restored, got {} instead of the original".format(type(my_predbat.components).__name__))
        failed = True

    return failed

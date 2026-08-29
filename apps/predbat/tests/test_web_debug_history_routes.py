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

    Note this stubs my_predbat.components, which is shared across the whole test run, so the
    caller must restore it - see the finally block in test_web_debug_history_routes().
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
    # _make_web() stubs my_predbat.components, and my_predbat is shared with every later
    # test in the run - leaving the stub in place breaks anything that calls a real method
    # on it (e.g. is_running() -> components.is_all_alive()).
    saved_components = getattr(my_predbat, "components", None)
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

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
Tests for http_sync, the synchronous HTTP helper built on aiohttp that replaced the requests
library. They run against a real local aiohttp server, so the status, body, header, timeout and
error behaviour checked here is what a caller really gets.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading

from aiohttp import web

import http_sync
from http_sync import HttpConnectionError, HttpError, HttpJSONDecodeError, HttpTimeout, http_get, http_post

BINARY_BODY = bytes(range(256)) * 64
STREAM_BODY = b"0123456789abcdef" * 8192


class LocalServer:
    """A small aiohttp server on a free local port, run on its own thread and event loop."""

    def __init__(self):
        self.seen = []
        self.loop = None
        self.port = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        """Thread body: build the app, bind a free port and serve until stopped."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        app = web.Application()
        app.router.add_get("/json", self.handle_json)
        app.router.add_post("/echo", self.handle_echo)
        app.router.add_get("/status/{code}", self.handle_status)
        app.router.add_get("/binary", self.handle_binary)
        app.router.add_get("/stream", self.handle_stream)
        app.router.add_get("/text", self.handle_text)
        app.router.add_get("/slow", self.handle_slow)
        app.router.add_get("/redirect", self.handle_redirect)
        runner = web.AppRunner(app)
        self.loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", 0)
        self.loop.run_until_complete(site.start())
        self.port = site._server.sockets[0].getsockname()[1]
        self.ready.set()
        self.loop.run_forever()
        self.loop.run_until_complete(runner.cleanup())
        self.loop.close()

    def start(self):
        """Start the server and wait until it is listening."""
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("local test server did not start")
        return self

    def stop(self):
        """Stop the server and wait for its thread."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    def url(self, path):
        """Full URL for a path on this server."""
        return "http://127.0.0.1:{}{}".format(self.port, path)

    async def handle_json(self, request):
        """Answer with JSON describing the query string and selected request headers."""
        self.seen.append(request)
        return web.json_response({"query": dict(request.query), "auth": request.headers.get("Authorization"), "accept": request.headers.get("Accept")})

    async def handle_echo(self, request):
        """Answer with the content type and body that were posted."""
        body = await request.read()
        return web.json_response({"content_type": request.content_type, "body": body.decode("utf-8")}, status=201)

    async def handle_status(self, request):
        """Answer with the requested status code and a short text body."""
        return web.Response(status=int(request.match_info["code"]), text="status body")

    async def handle_binary(self, request):
        """Answer with every byte value, to prove bodies are not decoded on the way through."""
        return web.Response(body=BINARY_BODY, content_type="application/octet-stream")

    async def handle_stream(self, request):
        """Answer with a body large enough to arrive in several chunks."""
        return web.Response(body=STREAM_BODY, content_type="application/gzip")

    async def handle_text(self, request):
        """Answer with text that is not JSON, in a declared non-UTF-8 charset."""
        return web.Response(body="caf\xe9 <html>".encode("latin-1"), headers={"Content-Type": "text/html; charset=latin-1"})

    async def handle_slow(self, request):
        """Answer only after a delay longer than the test's timeout."""
        await asyncio.sleep(2)
        return web.Response(text="late")

    async def handle_redirect(self, request):
        """Redirect to the JSON endpoint."""
        raise web.HTTPFound("/json?from=redirect")


def _free_port():
    """A local port with nothing listening on it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _check(name, condition, detail=""):
    """Print the outcome of one check and return True when it failed."""
    if condition:
        print("✓ {}".format(name))
        return False
    print("ERROR: {} {}".format(name, detail))
    return True


def test_http_sync(my_predbat=None):
    """
    http_get / http_post give a requests-like response and map failures onto HttpTimeout,
    HttpConnectionError and HttpJSONDecodeError, whether or not an event loop is already running.
    """
    print("\n*** Test: http_sync helper against a local server ***")
    failed = False
    server = LocalServer().start()
    try:
        # GET with query parameters and headers, JSON decoded
        r = http_get(server.url("/json"), headers={"Authorization": "Bearer abc", "Accept": "application/json"}, params={"a": "1", "skip": None, "n": 2}, timeout=10)
        data = r.json()
        failed |= _check("get status and ok", r.status_code == 200 and r.ok, "status {}".format(r.status_code))
        failed |= _check("get params sent, None dropped", data["query"] == {"a": "1", "n": "2"}, str(data))
        failed |= _check("get headers sent", data["auth"] == "Bearer abc" and data["accept"] == "application/json", str(data))
        failed |= _check("response headers readable case-insensitively", r.headers.get("content-type", "").startswith("application/json"), str(r.headers))
        failed |= _check("text matches json", json.loads(r.text) == data)
        failed |= _check("repr shows status", "200" in repr(r), repr(r))

        # POST a JSON body, and a form body
        r = http_post(server.url("/echo"), json={"entity": "sensor.x", "value": 1.5}, timeout=10)
        echo = r.json()
        failed |= _check("post json", r.status_code == 201 and echo["content_type"] == "application/json" and json.loads(echo["body"]) == {"entity": "sensor.x", "value": 1.5}, str(echo))
        r = http_post(server.url("/echo"), data={"a": "1", "b": "two words"}, timeout=10)
        echo = r.json()
        failed |= _check("post form", echo["content_type"] == "application/x-www-form-urlencoded" and echo["body"] == "a=1&b=two+words", str(echo))
        r = http_post(server.url("/echo"), timeout=10)
        failed |= _check("post with no body", r.status_code == 201 and r.json()["body"] == "", r.text)

        # Error statuses are returned, not raised
        r = http_get(server.url("/status/404"), timeout=10)
        failed |= _check("404 returned not raised", r.status_code == 404 and not r.ok and r.text == "status body", "{} {}".format(r.status_code, r.text))
        r = http_get(server.url("/status/503"), timeout=10)
        failed |= _check("503 returned not raised", r.status_code == 503 and not r.ok)
        r = http_get(server.url("/status/204"), timeout=10)
        failed |= _check("204 has an empty body", r.status_code == 204 and r.ok and r.content == b"" and r.text == "")

        # Redirects are followed
        r = http_get(server.url("/redirect"), timeout=10)
        failed |= _check("redirect followed", r.status_code == 200 and r.json()["query"] == {"from": "redirect"}, r.text)

        # Bodies arrive byte for byte; text honours the declared charset
        r = http_get(server.url("/binary"), timeout=10)
        failed |= _check("binary content intact", r.content == BINARY_BODY, "{} bytes".format(len(r.content)))
        r = http_get(server.url("/text"), timeout=10)
        failed |= _check("text uses declared charset", r.text == "caf\xe9 <html>", repr(r.text))

        # A body that is not JSON raises HttpJSONDecodeError, which is also a ValueError
        try:
            r.json()
            failed |= _check("non-JSON body raises", False)
        except HttpJSONDecodeError as exc:
            failed |= _check("non-JSON body raises HttpJSONDecodeError (a ValueError and an HttpError)", isinstance(exc, ValueError) and isinstance(exc, HttpError))

        # Streamed download: chunks of the size asked for, capped by max_bytes
        r = http_get(server.url("/stream"), stream=True, timeout=10)
        chunks = list(r.iter_content(chunk_size=4096))
        r.close()
        failed |= _check("stream chunks", b"".join(chunks) == STREAM_BODY and max(len(chunk) for chunk in chunks) <= 4096 and len(chunks) >= len(STREAM_BODY) // 4096, "{} chunks".format(len(chunks)))
        r = http_get(server.url("/stream"), stream=True, timeout=10, max_bytes=10000)
        total = sum(len(chunk) for chunk in r.iter_content(chunk_size=4096))
        r.close()
        failed |= _check("stream stops soon after max_bytes", 10000 < total < len(STREAM_BODY), "read {}".format(total))

        # Failures map onto the helper's own exceptions
        try:
            http_get(server.url("/slow"), timeout=0.3)
            failed |= _check("timeout raises", False)
        except HttpTimeout as exc:
            failed |= _check("slow response raises HttpTimeout", isinstance(exc, HttpError))
        try:
            http_get("http://127.0.0.1:{}/json".format(_free_port()), timeout=5)
            failed |= _check("refused connection raises", False)
        except HttpConnectionError as exc:
            failed |= _check("refused connection raises HttpConnectionError", isinstance(exc, HttpError) and not isinstance(exc, HttpTimeout))
        try:
            http_get("not-a-url", timeout=5)
            failed |= _check("invalid url raises", False)
        except HttpError:
            failed |= _check("invalid URL raises HttpError", True)

        # Called from inside a running event loop: the result and the exception both still arrive
        async def from_loop():
            """Call the sync helper from a coroutine, as the GivTCP component and HA history do."""
            good = http_get(server.url("/json"), params={"loop": "yes"}, timeout=10)
            try:
                http_get(server.url("/slow"), timeout=0.3)
                raised = None
            except HttpError as exc:
                raised = exc
            return good, raised

        good, raised = asyncio.run(from_loop())
        failed |= _check("works inside a running loop", good.status_code == 200 and good.json()["query"] == {"loop": "yes"})
        failed |= _check("exception propagates out of a running loop", isinstance(raised, HttpTimeout), repr(raised))
    finally:
        server.stop()

    # The process-wide session factory from component_base is honoured once it is loaded
    import component_base

    built = []
    original_factory = component_base._http_session_factory

    def counting_factory(**kwargs):
        """Record the session request and build the real session."""
        built.append(kwargs)
        import aiohttp

        return aiohttp.ClientSession(**kwargs)

    server = LocalServer().start()
    try:
        component_base.set_http_session_factory(counting_factory)
        r = http_get(server.url("/json"), timeout=10)
        failed |= _check("session comes from the component_base factory", r.status_code == 200 and len(built) == 1, "built {}".format(len(built)))
    finally:
        component_base.set_http_session_factory(original_factory)
        server.stop()

    # A caller's own open session is used for the request and left open for the next one
    server = LocalServer().start()
    try:

        async def on_own_session():
            """Make two requests on one caller-owned session, and one that times out."""
            import aiohttp

            built = []
            original = http_sync._new_session
            http_sync._new_session = lambda **kwargs: built.append(kwargs)
            try:
                async with aiohttp.ClientSession() as session:
                    first = await http_sync.async_http_request("GET", server.url("/json"), params={"own": "1"}, timeout=10, session=session)
                    open_after_first = not session.closed
                    second = await http_sync.async_http_request("POST", server.url("/echo"), json={"own": 2}, timeout=10, session=session)
                    try:
                        await http_sync.async_http_request("GET", server.url("/slow"), timeout=0.2, session=session)
                        slow = None
                    except HttpError as exc:
                        slow = exc
                    return first, open_after_first, second, slow, not session.closed, built
            finally:
                http_sync._new_session = original

        first, open_after_first, second, slow, open_at_end, built = asyncio.run(on_own_session())
        failed |= _check("request made on the caller's session", first.status_code == 200 and first.json()["query"] == {"own": "1"} and second.ok and "own" in second.json()["body"], "{} {}".format(first, second))
        failed |= _check("caller's session is left open", open_after_first and open_at_end)
        failed |= _check("no session of its own is made", not built, "built {}".format(len(built)))
        failed |= _check("timeout applies per request on a caller's session", isinstance(slow, HttpTimeout), repr(slow))
    finally:
        server.stop()

    return failed


def test_no_requests_import(my_predbat=None):
    """
    Nothing in apps/predbat imports the requests library, and importing predbat does not load it:
    it cost about 6.6 MB of idle memory on top of aiohttp.
    """
    print("\n*** Test: Predbat does not use the requests library ***")
    import ast

    failed = False
    source_dir = os.path.dirname(os.path.abspath(http_sync.__file__))
    offenders = []
    for folder in (source_dir, os.path.join(source_dir, "tests")):
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(folder, name)) as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    modules = [node.module]
                if any(module.split(".")[0] == "requests" for module in modules):
                    offenders.append("{}:{}".format(os.path.relpath(os.path.join(folder, name), source_dir), node.lineno))
    failed |= _check("no module imports requests", not offenders, "use http_sync instead: {}".format(", ".join(offenders)))

    code = "import sys; sys.path.insert(0, {!r}); import predbat; print('REQUESTS_LOADED' if 'requests' in sys.modules else 'REQUESTS_ABSENT')".format(source_dir)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, cwd=source_dir)
    failed |= _check("importing predbat does not load requests", "REQUESTS_ABSENT" in result.stdout, (result.stdout + result.stderr)[-600:])
    return failed


def run_http_sync_tests(my_predbat=None):
    """Run the http_sync helper tests."""
    failed = False
    failed |= test_http_sync(my_predbat)
    failed |= test_no_requests_import(my_predbat)
    return failed

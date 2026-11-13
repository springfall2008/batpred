# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
HTTP requests for synchronous code, built on aiohttp.

Predbat's plan code is synchronous but its components are asynchronous, and every component already
uses aiohttp. This module lets the synchronous code make its few HTTP calls through aiohttp too, so
the requests library (about 6.6 MB of idle memory on top of aiohttp) is not needed at all.

http_get() and http_post() return an HttpResponse with the familiar status_code, ok, headers,
content, text and json(). A response with an error status is returned, not raised. Failures to get
a response raise HttpTimeout or HttpConnectionError (both HttpError), and json() on a body that is
not JSON raises HttpJSONDecodeError (an HttpError and a ValueError).

async_http_request() is the coroutine underneath, for callers that are already asynchronous.

Only the standard library and aiohttp are imported here: download.py uses this module to repair a
broken install, so it must load when other Predbat files are missing or damaged.
"""

import asyncio
import json
import sys
import tempfile
import threading

import aiohttp

# A streamed body is kept in memory up to this size and spooled to a temporary file beyond it
STREAM_SPOOL_BYTES = 1024 * 1024
# Read size while receiving a streamed body
STREAM_READ_BYTES = 64 * 1024


class HttpError(Exception):
    """Base class for a request that got no usable response."""


class HttpTimeout(HttpError):
    """The server did not connect or answer within the timeout."""


class HttpConnectionError(HttpError):
    """The connection could not be made, or was lost before the response was complete."""


class HttpJSONDecodeError(HttpError, ValueError):
    """The response body is not valid JSON."""


class RunThread(threading.Thread):
    """Run a coroutine to completion on a thread of its own, keeping its result."""

    def __init__(self, coro):
        """Remember the coroutine to run."""
        self.coro = coro
        self.result = None
        super().__init__()

    def run(self):
        """Thread body: run the coroutine on a new event loop."""
        self.result = asyncio.run(self.coro)


def run_async(coro):
    """
    Run a coroutine from synchronous code and return its result.

    With no event loop running in this thread the coroutine runs here. When one is running (the
    caller was itself reached from a coroutine) it runs on a short-lived thread instead, since a
    running loop cannot be re-entered.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        thread = RunThread(coro)
        thread.start()
        thread.join()
        return thread.result
    else:
        return asyncio.run(coro)


class HttpResponse:
    """The parts of an HTTP response that Predbat reads, with the body already received."""

    def __init__(self, status_code, headers, url, content=b"", charset=None, body_file=None):
        """Hold a received response; body_file is set instead of content for a streamed body."""
        self.status_code = status_code
        self.headers = headers
        self.url = url
        self.charset = charset
        self._content = content
        self._body_file = body_file

    def __repr__(self):
        """Show the status, as a response printed in a log line should."""
        return "<HttpResponse [{}]>".format(self.status_code)

    @property
    def ok(self):
        """True when the status is not a client or server error."""
        return self.status_code < 400

    @property
    def content(self):
        """The body as bytes, exactly as received."""
        if self._body_file is not None:
            self._body_file.seek(0)
            return self._body_file.read()
        return self._content

    @property
    def text(self):
        """The body as text, in the charset the server declared or UTF-8."""
        try:
            return self.content.decode(self.charset or "utf-8", errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self):
        """The body decoded as JSON; raises HttpJSONDecodeError when it is not JSON."""
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpJSONDecodeError(str(exc)) from exc

    def iter_content(self, chunk_size=STREAM_READ_BYTES):
        """Yield the body in chunks of at most chunk_size bytes."""
        if self._body_file is not None:
            self._body_file.seek(0)
            while True:
                chunk = self._body_file.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        else:
            for start in range(0, len(self._content), chunk_size):
                yield self._content[start : start + chunk_size]

    def close(self):
        """Release a streamed body's temporary file; harmless on any other response."""
        if self._body_file is not None:
            self._body_file.close()
            self._body_file = None
            self._content = b""


def _new_session(**kwargs):
    """
    A new aiohttp session for one request.

    Once component_base is loaded its process-wide factory is used, so a host that installs one
    with set_http_session_factory() sees these requests too. It is looked up rather than imported
    to keep this module loadable on its own.
    """
    component_base = sys.modules.get("component_base")
    if component_base is not None:
        return component_base.new_http_session(**kwargs)
    return aiohttp.ClientSession(**kwargs)


async def async_http_request(method, url, headers=None, params=None, json=None, data=None, timeout=None, stream=False, max_bytes=None):  # pylint: disable=redefined-outer-name
    """
    Make one HTTP request and return an HttpResponse with the body received.

    :param method: "GET" or "POST" (any aiohttp method name works).
    :param headers: Request headers.
    :param params: Query parameters; entries whose value is None are left out.
    :param json: Object to send as a JSON body.
    :param data: Form fields (dict) or raw body to send.
    :param timeout: Seconds allowed to connect, and separately between reads; None waits forever.
    :param stream: Keep the body out of memory: spool it to a temporary file, read with
                   iter_content() and released with close().
    :param max_bytes: With stream, stop receiving soon after this many bytes, so an oversized body
                      is neither downloaded in full nor mistaken for a complete one.
    """
    client_timeout = aiohttp.ClientTimeout(total=None, connect=timeout, sock_read=timeout)
    if params:
        params = {key: value for key, value in params.items() if value is not None}
    body_file = None
    try:
        async with _new_session(timeout=client_timeout, trust_env=True) as session:
            async with session.request(method, url, headers=headers, params=params or None, json=json, data=data) as response:
                status_code = response.status
                response_headers = response.headers.copy()
                final_url = str(response.url)
                charset = response.charset
                if not stream:
                    return HttpResponse(status_code, response_headers, final_url, content=await response.read(), charset=charset)
                body_file = tempfile.SpooledTemporaryFile(max_size=STREAM_SPOOL_BYTES)
                received = 0
                async for chunk in response.content.iter_chunked(STREAM_READ_BYTES):
                    body_file.write(chunk)
                    received += len(chunk)
                    if max_bytes is not None and received > max_bytes:
                        break
                result = HttpResponse(status_code, response_headers, final_url, charset=charset, body_file=body_file)
                body_file = None
                return result
    except asyncio.TimeoutError as exc:
        raise HttpTimeout("Timeout from {} ({})".format(url, type(exc).__name__)) from exc
    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError) as exc:
        raise HttpConnectionError("{}: {}".format(type(exc).__name__, exc)) from exc
    except (aiohttp.ClientError, ValueError, TypeError) as exc:
        raise HttpError("{}: {}".format(type(exc).__name__, exc)) from exc
    finally:
        if body_file is not None:
            body_file.close()


async def _capture(coro):
    """Run a request coroutine, returning (response, None) or (None, exception) so that either survives run_async()'s thread."""
    try:
        return await coro, None
    except Exception as exc:  # pylint: disable=broad-except
        return None, exc


def http_request(method, url, **kwargs):
    """Synchronous form of async_http_request(), usable with or without an event loop running in this thread."""
    outcome = run_async(_capture(async_http_request(method, url, **kwargs)))
    if outcome is None:
        raise HttpError("Request to {} did not complete".format(url))
    response, error = outcome
    if error is not None:
        raise error
    return response


def http_get(url, headers=None, params=None, timeout=None, stream=False, max_bytes=None):
    """HTTP GET from synchronous code; see async_http_request() for the arguments."""
    return http_request("GET", url, headers=headers, params=params, timeout=timeout, stream=stream, max_bytes=max_bytes)


def http_post(url, headers=None, params=None, json=None, data=None, timeout=None):  # pylint: disable=redefined-outer-name
    """HTTP POST from synchronous code; see async_http_request() for the arguments."""
    return http_request("POST", url, headers=headers, params=params, json=json, data=data, timeout=timeout)

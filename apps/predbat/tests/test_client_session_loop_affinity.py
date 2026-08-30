# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for ClientSession loop affinity.

An aiohttp.ClientSession is bound to the event loop that created it — its
connector and transports live on that loop. Reusing one from a different loop
raises at request time, and a caller that swallows the error sees the request
silently do nothing: the user is told their change was applied while the device
was never contacted.

The components' own poll cycles run on one loop, but entity callbacks can be
dispatched from another, so a session is not guaranteed to be used on the loop
that created it.
"""

import asyncio
import os
import sys


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    """Run a coroutine on a fresh event loop and return its result."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubBase:
    """Minimal stand-in so the accessors can run without a live component."""

    def __init__(self):
        self.session = None
        self._session = None
        self._session_loop = None
        self._close_session = False


def _accessor_rebinds_across_loops(make_holder, get_session):
    holder = make_holder()

    async def grab():
        return get_session(holder), asyncio.get_running_loop()

    first, loop_a = _run(grab())
    second, loop_b = _run(grab())

    assert loop_a is not loop_b, "test must exercise two distinct loops"
    assert first is not second, "a session created on another loop must not be reused"


def _accessor_reuses_within_one_loop(make_holder, get_session):
    holder = make_holder()

    async def grab_twice():
        return get_session(holder), get_session(holder)

    first, second = _run(grab_twice())
    assert first is second, "the session must still be reused within a single loop"


def test_ohme_session_rebinds_across_loops():
    from ohme import OhmeApiClient

    _accessor_rebinds_across_loops(_StubBase, lambda h: OhmeApiClient._ensure_session(h))


def test_ohme_session_reused_within_one_loop():
    from ohme import OhmeApiClient

    _accessor_reuses_within_one_loop(_StubBase, lambda h: OhmeApiClient._ensure_session(h))

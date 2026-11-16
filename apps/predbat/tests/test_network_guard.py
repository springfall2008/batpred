# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""Tests for tests/network_guard.py, which keeps the unit tests off the network."""

import asyncio
import socket

from tests import network_guard


def _refused(call):
    """Run call() and return True if the guard refused it as an unreachable host."""
    try:
        call()
    except socket.gaierror:
        return True
    return False


def test_network_guard(my_predbat):
    """Non-local lookups and connects are refused and recorded; loopback, Unix sockets and unencodable names pass through."""
    failed = False
    print("**** Testing the unit test network guard ****")
    # Anything an earlier test left would be blamed on this one
    network_guard.take_attempts()

    for host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "", None):
        if not network_guard.is_local_host(host):
            print("ERROR: {!r} should count as local".format(host))
            failed = True
    for host in ("api.givenergy.cloud", "8.8.8.8", "192.168.1.10", "2001:db8::1"):
        if network_guard.is_local_host(host):
            print("ERROR: {!r} should not count as local".format(host))
            failed = True

    if not _refused(lambda: network_guard.guarded_getaddrinfo("api.example.com", 443)):
        print("ERROR: a lookup of a remote host was not refused")
        failed = True
    attempts = network_guard.take_attempts()
    if len(attempts) != 1 or "api.example.com" not in attempts[0] or "test_network_guard.py" not in attempts[0]:
        print("ERROR: the refused lookup should be recorded once, naming the host and this test, got {}".format(attempts))
        failed = True
    if network_guard.take_attempts():
        print("ERROR: take_attempts() should forget what it returned")
        failed = True

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not _refused(lambda: network_guard.guarded_connect(sock, ("203.0.113.5", 443))):
            print("ERROR: a connect to a remote address was not refused")
            failed = True
        if not _refused(lambda: network_guard.guarded_connect_ex(sock, ("203.0.113.5", 443))):
            print("ERROR: a connect_ex to a remote address was not refused")
            failed = True
    finally:
        sock.close()
    if len(network_guard.take_attempts()) != 2:
        print("ERROR: both refused connects should be recorded")
        failed = True

    # aiohttp resolves through the event loop, whose lookup coroutine runs while the caller is still on the stack
    async def loop_lookup():
        """Resolve a remote host through the running loop."""
        await network_guard.guarded_loop_getaddrinfo(asyncio.get_running_loop(), "api.example.com", 443)

    if not _refused(lambda: asyncio.run(loop_lookup())):
        print("ERROR: an event-loop lookup of a remote host was not refused")
        failed = True
    attempts = network_guard.take_attempts()
    if len(attempts) != 1 or "test_network_guard.py" not in attempts[0]:
        print("ERROR: the event-loop lookup should be recorded naming this test, got {}".format(attempts))
        failed = True

    # Loopback still resolves, and a name too long to IDNA-encode keeps its local error rather than being blamed on the test
    if not network_guard.guarded_getaddrinfo("127.0.0.1", 80):
        print("ERROR: a loopback lookup should resolve")
        failed = True
    try:
        network_guard.guarded_getaddrinfo("a" * 64 + ".example.com", 443)
        print("ERROR: an unencodable name should still fail to resolve")
        failed = True
    except UnicodeError:
        pass
    if network_guard.take_attempts():
        print("ERROR: loopback and unencodable lookups should not be recorded")
        failed = True

    if network_guard._address_host("/tmp/socket") is not None:
        print("ERROR: a Unix socket path has no host, so it is never refused")
        failed = True
    return failed

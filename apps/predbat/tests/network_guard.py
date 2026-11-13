# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Keep the unit tests off the network.

A test that drives a component without stubbing every call it makes sends its fake credentials to the
real vendor API, waits for the 401, and passes anyway because the component handles the failure. That
is slow, fails offline, and puts junk traffic on someone else's service - and nothing notices.

install() makes every DNS lookup or connect to anything but the loopback address fail at once, as
though the host were unreachable, and records it. The runner calls take_attempts() after each test and
fails the test that made any, naming the host and the line that asked for it.
"""

import asyncio
import ipaddress
import os
import socket
import traceback

_ATTEMPTS = []
_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_LOOP_GETADDRINFO = asyncio.base_events.BaseEventLoop.getaddrinfo
LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "", None}


def is_local_host(host):
    """True for a host the tests may reach: the loopback interface, the wildcard address, or no host at all."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _caller():
    """Where the attempt came from: the innermost Predbat source line and, when they differ, the innermost test line."""
    code_line = test_line = None
    for frame in reversed(traceback.extract_stack()[:-3]):
        # Modules can load as tests/../deye.py, which is not a test
        filename = os.path.normpath(frame.filename)
        if "/apps/predbat/" not in filename or os.path.basename(filename) == "network_guard.py":
            continue
        where = "{}:{}".format(filename.split("/apps/predbat/")[-1], frame.lineno)
        if code_line is None:
            code_line = where
        if "/apps/predbat/tests/" in filename:
            test_line = where
            break
    if code_line is None:
        return "a thread whose stack does not reach Predbat code"
    if test_line and test_line != code_line:
        return "{} via {}".format(code_line, test_line)
    return code_line


def _refuse(host):
    """Record a blocked attempt and raise the error an unreachable host would."""
    _ATTEMPTS.append("{} (from {})".format(host, _caller()))
    raise socket.gaierror(socket.EAI_NONAME, "unit tests may not reach the network: {}".format(host))


def guarded_getaddrinfo(host, *args, **kwargs):
    """socket.getaddrinfo that only resolves local hosts."""
    if is_local_host(host) or not _encodable(host):
        return _REAL_GETADDRINFO(host, *args, **kwargs)
    return _refuse(host)


async def guarded_loop_getaddrinfo(self, host, port, *args, **kwargs):
    """
    The event loop's getaddrinfo, refusing non-local hosts while the awaiting coroutines are still on the stack.

    aiohttp resolves through this, and it hands the lookup to an executor thread whose stack does not reach the
    code that made the request - refused here, the record names the line that did.
    """
    if not is_local_host(host) and _encodable(host):
        _refuse(host)
    return await _REAL_LOOP_GETADDRINFO(self, host, port, *args, **kwargs)


def _encodable(host):
    """False for a name that cannot be IDNA-encoded: getaddrinfo fails on it before any query is sent, and tests rely on that error."""
    if isinstance(host, str):
        try:
            host.encode("idna")
        except UnicodeError:
            return False
    return True


def _address_host(address):
    """The host part of a socket address, or None for a Unix socket path."""
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def guarded_connect(self, address):
    """socket.connect that only connects to local hosts."""
    host = _address_host(address)
    if host is not None and not is_local_host(host):
        _refuse(host)
    return _REAL_CONNECT(self, address)


def guarded_connect_ex(self, address):
    """socket.connect_ex that only connects to local hosts."""
    host = _address_host(address)
    if host is not None and not is_local_host(host):
        _refuse(host)
    return _REAL_CONNECT_EX(self, address)


def install():
    """Block non-local network access for the rest of the process."""
    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    asyncio.base_events.BaseEventLoop.getaddrinfo = guarded_loop_getaddrinfo


def take_attempts():
    """Return the blocked attempts recorded since the last call, and forget them."""
    attempts = list(_ATTEMPTS)
    _ATTEMPTS.clear()
    return attempts

# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the glibc allocator helpers in utils: malloc_trim() and limit_malloc_arenas().

Both are thin ctypes wrappers around C library calls that only exist on glibc, so the
tests cover the two things that matter: they never raise on a platform without the
call (macOS, musl), and they pass the right arguments to it when it is there.
"""

import platform
import sys

import utils
from utils import M_ARENA_MAX, MALLOC_ARENA_LIMIT, limit_malloc_arenas, malloc_trim


class _FakeLibc:
    """A C library stand-in whose malloc_trim() and mallopt() record their arguments."""

    def __init__(self, trim_result=1, mallopt_result=1):
        self.trim_calls = []
        self.mallopt_calls = []
        self.trim_result = trim_result
        self.mallopt_result = mallopt_result
        self.malloc_trim = _FakeFunction(self.trim_calls, self.trim_result)
        self.mallopt = _FakeFunction(self.mallopt_calls, self.mallopt_result)


class _FakeFunction:
    """A ctypes-style function pointer: accepts argtypes/restype and logs each call."""

    def __init__(self, calls, result):
        self.calls = calls
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _NoSuchLibc:
    """A loaded library that has neither symbol, as musl and macOS libc report."""


def _with_cdll(replacement, run):
    """Run run() with utils.ctypes.CDLL swapped for replacement, restoring it afterwards."""
    original = utils.ctypes.CDLL
    utils.ctypes.CDLL = replacement
    try:
        return run()
    finally:
        utils.ctypes.CDLL = original


def test_helpers_never_raise_on_this_platform(my_predbat=None):
    """
    Whatever libc this test runs on, both helpers return a plain bool.

    update_pred() calls malloc_trim() every cycle and initialize() calls limit_malloc_arenas()
    once, so a raise on macOS (no malloc_trim) or musl (no mallopt) would take Predbat down.
    On glibc both calls exist and must report success.
    """
    failed = False
    print("**** Testing the allocator helpers on {} ({}) ****".format(sys.platform, platform.libc_ver()[0] or "unknown libc"))

    try:
        trimmed = malloc_trim()
        limited = limit_malloc_arenas()
    except Exception as e:
        print("ERROR: allocator helper raised {}: {}".format(type(e).__name__, e))
        return 1

    for name, result in (("malloc_trim", trimmed), ("limit_malloc_arenas", limited)):
        if not isinstance(result, bool):
            print("ERROR: {}() returned {!r}, expected a bool".format(name, result))
            failed = True

    if platform.libc_ver()[0] == "glibc" and not limited:
        print("ERROR: limit_malloc_arenas() reported failure on glibc")
        failed = True

    if not failed:
        print("PASS: malloc_trim() -> {}, limit_malloc_arenas() -> {}".format(trimmed, limited))
    return 1 if failed else 0


def test_helpers_report_false_without_glibc(my_predbat=None):
    """
    When the C library cannot be loaded, or lacks the symbol, both helpers return False.

    ctypes.CDLL(None) raises OSError on a static build and returns a handle without the
    attribute on musl/macOS; either way the caller just carries on without the optimisation.
    """
    failed = False
    print("**** Testing the allocator helpers without glibc ****")

    def _cannot_load(_name):
        """CDLL that cannot open the process' C library."""
        raise OSError("no shared library")

    for label, cdll in (("a libc that cannot be loaded", _cannot_load), ("a libc without the symbols", lambda _name: _NoSuchLibc())):
        try:
            trimmed, limited = _with_cdll(cdll, lambda: (malloc_trim(), limit_malloc_arenas()))
        except Exception as e:
            print("ERROR: {} made a helper raise {}: {}".format(label, type(e).__name__, e))
            failed = True
            continue
        if trimmed is not False or limited is not False:
            print("ERROR: with {} expected (False, False), got ({}, {})".format(label, trimmed, limited))
            failed = True

    if not failed:
        print("PASS: both helpers return False when glibc is absent")
    return 1 if failed else 0


def test_helpers_call_glibc_correctly(my_predbat=None):
    """
    On a libc with the calls, malloc_trim(0) and mallopt(M_ARENA_MAX, n) are issued and their result reported.

    M_ARENA_MAX is glibc's -8; passing the wrong parameter number would silently tune something
    else, and malloc_trim's pad argument must be 0 to release everything it can.
    """
    failed = False
    print("**** Testing the allocator helpers call glibc correctly ****")

    libc = _FakeLibc()
    trimmed, limited, custom = _with_cdll(lambda _name: libc, lambda: (malloc_trim(), limit_malloc_arenas(), limit_malloc_arenas(5)))

    if libc.trim_calls != [(0,)]:
        print("ERROR: expected malloc_trim(0), got calls {}".format(libc.trim_calls))
        failed = True
    if libc.mallopt_calls != [(M_ARENA_MAX, MALLOC_ARENA_LIMIT), (M_ARENA_MAX, 5)]:
        print("ERROR: expected mallopt({}, {}) then mallopt({}, 5), got {}".format(M_ARENA_MAX, MALLOC_ARENA_LIMIT, M_ARENA_MAX, libc.mallopt_calls))
        failed = True
    if M_ARENA_MAX != -8:
        print("ERROR: M_ARENA_MAX is {}, glibc defines it as -8".format(M_ARENA_MAX))
        failed = True
    if (trimmed, limited, custom) != (True, True, True):
        print("ERROR: expected every call to report success, got {}".format((trimmed, limited, custom)))
        failed = True

    # glibc reports 0 from malloc_trim when nothing could be released and from mallopt on a bad value
    libc = _FakeLibc(trim_result=0, mallopt_result=0)
    trimmed, limited = _with_cdll(lambda _name: libc, lambda: (malloc_trim(), limit_malloc_arenas()))
    if trimmed is not False or limited is not False:
        print("ERROR: expected (False, False) when glibc reports 0, got ({}, {})".format(trimmed, limited))
        failed = True

    if not failed:
        print("PASS: malloc_trim(0) and mallopt({}, {}) are issued and their results reported".format(M_ARENA_MAX, MALLOC_ARENA_LIMIT))
    return 1 if failed else 0


def run_memory_release_tests(my_predbat):
    """Run every allocator helper test, returning a non-zero count on failure."""
    failed = 0
    failed += test_helpers_never_raise_on_this_platform(my_predbat)
    failed += test_helpers_report_false_without_glibc(my_predbat)
    failed += test_helpers_call_glibc_correctly(my_predbat)
    return failed

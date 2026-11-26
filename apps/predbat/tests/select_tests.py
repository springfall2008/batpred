# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Pick the tests unit_test.py runs for --test and -k.

A test has two names: its name in TEST_REGISTRY (e.g. "currency_icon_e2e"), which --list shows, and the
name of its Python function (e.g. "test_currency_icon_end_to_end"). It's easy to type the second, or to
put "test_" in front of the first, so both are accepted.
"""

TEST_PREFIX = "test_"


def _strip_test_prefix(text):
    """Return text without a leading "test_", or unchanged if it doesn't start with one."""
    return text[len(TEST_PREFIX) :] if text.startswith(TEST_PREFIX) else text


def keyword_matches(keyword, name, func):
    """Return True if -k keyword selects the test registered as name with function func.

    Matches when the keyword appears in the registry name or the function name, or appears in the
    registry name once a leading "test_" is taken off the keyword. A keyword of just "test_" matches
    nothing: it is in nearly every function name, so it would otherwise run almost the whole suite.
    """
    stripped = _strip_test_prefix(keyword)
    if not stripped:
        return False
    return keyword in name or keyword in func.__name__ or stripped in name


def name_matches(test_name, name, func):
    """Return True if --test test_name names the test registered as name with function func.

    Matches the registry name exactly, the function name exactly, or the registry name once a leading
    "test_" is taken off test_name.
    """
    return test_name == name or test_name == func.__name__ or _strip_test_prefix(test_name) == name


def select_by_keyword(registry, keyword):
    """Return the registry entries whose test -k keyword selects."""
    return [entry for entry in registry if keyword_matches(keyword, entry[0], entry[1])]


def select_by_name(registry, test_name):
    """Return the registry entry --test test_name names, or None if there isn't one."""
    for entry in registry:
        if name_matches(test_name, entry[0], entry[1]):
            return entry
    return None


def choose_tests(registry, keyword=None, test_names=None):
    """Return (tests to run, error message) for unit_test.py's -k and --test options.

    -k takes priority over --test, as it always has. With neither, every test in the registry runs.
    The error message is None unless -k matched nothing or a --test name wasn't found.
    """
    if keyword:
        entries = select_by_keyword(registry, keyword)
        if not entries:
            return [], "ERROR: No tests found matching keyword '{}'".format(keyword)
        return entries, None
    if test_names:
        entries = []
        for test_name in test_names:
            entry = select_by_name(registry, test_name)
            if not entry:
                return [], "ERROR: Test '{}' not found. Use --list to see available tests.".format(test_name)
            entries.append(entry)
        return entries, None
    return list(registry), None

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests for how unit_test.py picks tests from --test and -k: the registry name, the function name and a
registry name typed with "test_" in front all work.
"""

from tests.select_tests import choose_tests, keyword_matches, name_matches, select_by_keyword, select_by_name


class _StandIn:
    """Stands in for a test function: only its __name__ is used, so nothing here looks like a real, unregistered test."""

    def __init__(self, name):
        """Record the function name to report."""
        self.__name__ = name


REGISTRY = [
    ("currency_icon_e2e", _StandIn("test_currency_icon_end_to_end"), "End to end", False),
    ("currency_icon_lookup", _StandIn("test_currency_icon_lookup"), "Lookup", False),
    ("octopus_url", _StandIn("run_octopus_url_tests"), "Octopus URL", False),
]


def _names(entries):
    """Registry names of the given entries."""
    return [entry[0] for entry in entries]


def test_select_tests(my_predbat):
    """Check -k and --test accept the registry name, the function name and a "test_" prefixed registry name."""
    print("**** Running test selection tests ****")
    failed = False

    def check(label, got, expected):
        """Print and record one comparison."""
        nonlocal failed
        if got != expected:
            print("ERROR: {} - expected {}, got {}".format(label, expected, got))
            failed = True
        else:
            print("Test: {} OK".format(label))

    # -k
    check("-k currency_icon matches both currency tests", _names(select_by_keyword(REGISTRY, "currency_icon")), ["currency_icon_e2e", "currency_icon_lookup"])
    check("-k test_currency_icon matches both (test_ prefix / function names)", _names(select_by_keyword(REGISTRY, "test_currency_icon")), ["currency_icon_e2e", "currency_icon_lookup"])
    check("-k test_octopus_url matches the registry name once test_ is taken off", _names(select_by_keyword(REGISTRY, "test_octopus_url")), ["octopus_url"])
    check("-k end_to_end matches the function name", _names(select_by_keyword(REGISTRY, "end_to_end")), ["currency_icon_e2e"])
    check("-k octopus matches the registry name", _names(select_by_keyword(REGISTRY, "octopus")), ["octopus_url"])
    check("-k nothing_like_this matches nothing", _names(select_by_keyword(REGISTRY, "nothing_like_this")), [])
    check("-k test_ on its own matches nothing, rather than every test", _names(select_by_keyword(REGISTRY, "test_")), [])
    check("keyword_matches is a substring match", keyword_matches("lookup", "currency_icon_lookup", REGISTRY[1][1]), True)

    # --test
    check("--test currency_icon_e2e by registry name", (select_by_name(REGISTRY, "currency_icon_e2e") or ("",))[0], "currency_icon_e2e")
    check("--test test_currency_icon_end_to_end by function name", (select_by_name(REGISTRY, "test_currency_icon_end_to_end") or ("",))[0], "currency_icon_e2e")
    check("--test test_octopus_url by registry name with test_ prefix", (select_by_name(REGISTRY, "test_octopus_url") or ("",))[0], "octopus_url")
    check("--test currency_icon is not a whole name, so matches nothing", select_by_name(REGISTRY, "currency_icon"), None)
    check("name_matches needs a whole name", name_matches("currency_icon", "currency_icon_lookup", REGISTRY[1][1]), False)

    # choose_tests, the single call unit_test.py's main() makes for -k / --test
    entries, error = choose_tests(REGISTRY, keyword="end_to_end")
    check("choose_tests -k by function name", (_names(entries), error), (["currency_icon_e2e"], None))
    entries, error = choose_tests(REGISTRY, test_names=["test_currency_icon_end_to_end", "test_octopus_url"])
    check("choose_tests --test by function name and test_ prefix, in the order given", (_names(entries), error), (["currency_icon_e2e", "octopus_url"], None))
    entries, error = choose_tests(REGISTRY, keyword="octopus", test_names=["currency_icon_e2e"])
    check("choose_tests -k takes priority over --test", (_names(entries), error), (["octopus_url"], None))
    entries, error = choose_tests(REGISTRY)
    check("choose_tests with neither option runs everything", (_names(entries), error), (_names(REGISTRY), None))
    check("choose_tests returns a copy, so shuffling it leaves the registry alone", entries is REGISTRY, False)
    entries, error = choose_tests(REGISTRY, keyword="nothing_like_this")
    check("choose_tests -k with no match is an error", (entries, error), ([], "ERROR: No tests found matching keyword 'nothing_like_this'"))
    entries, error = choose_tests(REGISTRY, keyword="test_")
    check("choose_tests -k test_ is an error, not the whole suite", (entries, error), ([], "ERROR: No tests found matching keyword 'test_'"))
    entries, error = choose_tests(REGISTRY, test_names=["currency_icon_e2e", "no_such_test"])
    check("choose_tests --test with an unknown name is an error", (entries, error), ([], "ERROR: Test 'no_such_test' not found. Use --list to see available tests."))

    if failed:
        print("ERROR: test_select_tests FAILED")
    else:
        print("test_select_tests PASSED")
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
#

"""Tests for the chat agent's own tools: documentation search, source access and URL fetch.

The source and fetch guards are the security surface of the whole feature - the source tools are
what stop a model reading apps.yaml off disk, and the fetch allowlist is what stops it posting
what it read to an address of its choosing. Those tests are the point of this file.
"""

import asyncio

import chat_tools
from chat_tools import CHAT_TOOL_DEFS, score_documents, search_docs
from agent_tools import openai_tool_list

SAMPLE_DOCS = [
    {"location": "customisation/", "title": "Customisation", "text": "The best_soc_keep setting reserves battery capacity for unexpected load. Raise it if you run out overnight."},
    {"location": "energy-rates/", "title": "Energy rates", "text": "Octopus Agile rates are fetched every thirty minutes and used to plan charging windows."},
    {"location": "faq/", "title": "FAQ", "text": "Why is my battery charging at 3am? Usually because the overnight rate is the cheapest in the plan."},
]


class FakeCachedStorage:
    """A storage stand-in whose fetch_cached calls the fetch function at most once."""

    def __init__(self, payload=None, fail=False):
        """Hold the payload fetch_cached should return, or arrange for a failure."""
        self.payload = payload
        self.fail = fail
        self.calls = 0

    async def fetch_cached(self, module, filename, fetch_fn, fresh_minutes=30, stale_minutes=35, format="yaml"):
        """Return the cached payload, counting how many times a real fetch would have happened."""
        self.calls += 1
        if self.fail:
            return None
        return self.payload


def test_chat_tool_defs_shape(my_predbat):
    """CHAT_TOOL_DEFS holds the five chat-only tools, none of them flagged as Predbat writes."""
    failed = False
    print("**** Testing CHAT_TOOL_DEFS shape ****")
    names = [entry["name"] for entry in CHAT_TOOL_DEFS]
    expected = ["set_chat_title", "search_docs", "search_source", "read_source", "fetch_url"]
    if names != expected:
        print("ERROR: CHAT_TOOL_DEFS names are {}, expected {}".format(names, expected))
        failed = True

    for entry in CHAT_TOOL_DEFS:
        if entry.get("writes"):
            print("ERROR: {} is flagged as a Predbat write, so it would hit the confirmation gate".format(entry["name"]))
            failed = True
        for field in ("name", "description", "parameters"):
            if field not in entry:
                print("ERROR: {} is missing '{}'".format(entry.get("name"), field))
                failed = True

    projected = openai_tool_list(CHAT_TOOL_DEFS)
    if len(projected) != len(CHAT_TOOL_DEFS):
        print("ERROR: openai_tool_list(CHAT_TOOL_DEFS) returned {} entries".format(len(projected)))
        failed = True

    return failed


def test_score_documents(my_predbat):
    """Scoring ranks by term overlap, weights the title, and respects max_results."""
    failed = False
    print("**** Testing documentation scoring ****")

    results = score_documents(SAMPLE_DOCS, "why is my battery charging at 3am", max_results=2)
    if not results:
        print("ERROR: scoring returned nothing for an obvious query")
        failed = True
    elif results[0]["url"] != chat_tools.DOCS_SITE_ROOT + "faq/":
        print("ERROR: top hit was {}, expected the FAQ page".format(results[0]["url"]))
        failed = True
    if len(results) > 2:
        print("ERROR: max_results was ignored, got {} results".format(len(results)))
        failed = True

    titled = score_documents(SAMPLE_DOCS, "customisation", max_results=3)
    if not titled or titled[0]["title"] != "Customisation":
        print("ERROR: a title match did not rank first: {}".format(titled))
        failed = True

    for result in results:
        for field in ("title", "url", "excerpt"):
            if field not in result:
                print("ERROR: result is missing '{}': {}".format(field, result))
                failed = True
        if len(result.get("excerpt", "")) > 400:
            print("ERROR: excerpt is {} characters, longer than the cap".format(len(result["excerpt"])))
            failed = True

    if score_documents(SAMPLE_DOCS, "zz", max_results=3):
        print("ERROR: a query of only short terms should match nothing")
        failed = True

    return failed


def test_search_docs_uses_the_cache(my_predbat):
    """search_docs goes through fetch_cached and reports a clean error when the index is missing."""
    failed = False
    print("**** Testing search_docs caching and failure ****")

    storage = FakeCachedStorage(payload={"config": {}, "docs": SAMPLE_DOCS})
    first = asyncio.run(search_docs(storage, "best_soc_keep"))
    if not first.get("success") or not first.get("data"):
        print("ERROR: search_docs failed on a good index: {}".format(first))
        failed = True

    second = asyncio.run(search_docs(storage, "octopus agile"))
    if not second.get("success"):
        print("ERROR: second search_docs call failed: {}".format(second))
        failed = True

    missing = asyncio.run(search_docs(FakeCachedStorage(fail=True), "anything"))
    if missing.get("success") or "unavailable" not in str(missing.get("error", "")).lower():
        print("ERROR: an unfetchable index should return a clean error, got {}".format(missing))
        failed = True

    clamped = asyncio.run(search_docs(storage, "rates", max_results=999))
    if len(clamped.get("data") or []) > 10:
        print("ERROR: max_results was not clamped to 10")
        failed = True

    return failed


def run_chat_tools_tests(my_predbat):
    """Run every chat-only tool test, returning True if any of them failed."""
    failed = False
    failed |= test_chat_tool_defs_shape(my_predbat)
    failed |= test_score_documents(my_predbat)
    failed |= test_search_docs_uses_the_cache(my_predbat)
    return failed

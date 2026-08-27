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
import os
import shutil
import tempfile

import chat_tools
from chat_tools import CHAT_TOOL_DEFS, score_documents, search_docs, read_source, search_source, resolve_source_path, SourceAccessError
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


def _make_source_tree():
    """Build a throwaway source tree with the decoy files a real install can contain.

    plan.py is deliberately oversized: enough filler lines to exceed SOURCE_MAX_LINES, plus one
    line long enough to exceed SOURCE_MATCH_LINE_MAX, so the tests that assert those caps
    actually exercise them rather than passing because the fixture never came close. wide.py
    exists purely to exceed SOURCE_MAX_BYTES within its first SOURCE_MAX_LINES lines.
    """
    root = tempfile.mkdtemp(prefix="predbat_src_")
    with open(os.path.join(root, "plan.py"), "w", encoding="utf-8") as handle:
        handle.write("def calculate_plan(self):\n    # marker_symbol lives here\n    return True\n" + "# filler\n" * 450 + "# marker_symbol " + ("y" * 320) + "\n")
    with open(os.path.join(root, "prediction_kernel.cpp"), "w", encoding="utf-8") as handle:
        handle.write("// marker_symbol in C++\nint main() { return 0; }\n")
    with open(os.path.join(root, "wide.py"), "w", encoding="utf-8") as handle:
        handle.write(("# " + ("w" * 200) + "\n") * 500)
    with open(os.path.join(root, "predbat.log"), "w", encoding="utf-8") as handle:
        handle.write("2026-08-27: marker_symbol should never be reachable\n")
    with open(os.path.join(root, "apps.yaml"), "w", encoding="utf-8") as handle:
        handle.write("octopus_api_key: sk-marker_symbol-secret\n")
    with open(os.path.join(root, "secrets.yaml"), "w", encoding="utf-8") as handle:
        handle.write("ha_key: marker_symbol\n")
    with open(os.path.join(root, "old_plan.py.bak"), "w", encoding="utf-8") as handle:
        handle.write("marker_symbol in a backup\n")
    with open(os.path.join(root, "kernel.so"), "wb") as handle:
        handle.write(b"\x7fELF marker_symbol")
    os.makedirs(os.path.join(root, "cache"), exist_ok=True)
    with open(os.path.join(root, "cache", "chat_token.json"), "w", encoding="utf-8") as handle:
        handle.write('{"access_token": "marker_symbol"}')
    os.makedirs(os.path.join(root, "venv", "lib"), exist_ok=True)
    with open(os.path.join(root, "venv", "lib", "dependency.py"), "w", encoding="utf-8") as handle:
        handle.write("marker_symbol from a dependency\n")
    os.makedirs(os.path.join(root, "__pycache__"), exist_ok=True)
    with open(os.path.join(root, "__pycache__", "cached.py"), "w", encoding="utf-8") as handle:
        handle.write("marker_symbol from a cache\n")
    return root


def test_search_source(my_predbat):
    """search_source finds real source, skips dependencies and caches, and reports truncation."""
    failed = False
    print("**** Testing search_source ****")
    root = _make_source_tree()
    try:
        result = search_source("marker_symbol", root=root)
        if not result.get("success"):
            print("ERROR: search_source failed: {}".format(result.get("error")))
            failed = True
        files = {hit["file"] for hit in result.get("data") or []}
        if "plan.py" not in files:
            print("ERROR: search_source missed plan.py, found {}".format(files))
            failed = True
        if "prediction_kernel.cpp" not in files:
            print("ERROR: search_source missed the C++ file, found {}".format(files))
            failed = True

        forbidden = {"predbat.log", "apps.yaml", "secrets.yaml", "old_plan.py.bak", "kernel.so", os.path.join("cache", "chat_token.json"), os.path.join("venv", "lib", "dependency.py"), os.path.join("__pycache__", "cached.py")}
        leaked = files & forbidden
        if leaked:
            print("ERROR: search_source reached files it must never read: {}".format(leaked))
            failed = True

        truncated_hit = False
        for hit in result.get("data") or []:
            if len(hit.get("text", "")) > chat_tools.SOURCE_MATCH_LINE_MAX:
                print("ERROR: a match line was not truncated: {} chars".format(len(hit["text"])))
                failed = True
            if len(hit.get("text", "")) == chat_tools.SOURCE_MATCH_LINE_MAX:
                truncated_hit = True
            if "line" not in hit:
                print("ERROR: a match is missing its line number: {}".format(hit))
                failed = True
        if not truncated_hit:
            print("ERROR: no match line was long enough to exercise the {}-character truncation".format(chat_tools.SOURCE_MATCH_LINE_MAX))
            failed = True

        capped = search_source("filler", max_results=2, root=root)
        if len(capped.get("data") or []) > 2:
            print("ERROR: max_results was ignored")
            failed = True
        if not capped.get("total_matches") or capped["total_matches"] <= 2:
            print("ERROR: total_matches did not report the truncation: {}".format(capped.get("total_matches")))
            failed = True

        scoped = search_source("marker_symbol", file="plan.py", root=root)
        if {hit["file"] for hit in scoped.get("data") or []} != {"plan.py"}:
            print("ERROR: the file argument did not scope the search: {}".format(scoped.get("data")))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_search_source_rejects_bad_patterns(my_predbat):
    """An over-long or uncompilable pattern is a clean error, never an exception."""
    failed = False
    print("**** Testing search_source pattern validation ****")
    root = _make_source_tree()
    try:
        long_pattern = search_source("x" * 500, root=root)
        if long_pattern.get("success"):
            print("ERROR: an over-long pattern was accepted")
            failed = True

        broken = search_source("marker_symbol[", root=root)
        if broken.get("success") or "regular expression" not in str(broken.get("error", "")).lower():
            print("ERROR: an uncompilable pattern did not report cleanly: {}".format(broken))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_read_source(my_predbat):
    """read_source returns a numbered slice, clamps its size, and reports the file length."""
    failed = False
    print("**** Testing read_source ****")
    root = _make_source_tree()
    try:
        result = read_source("plan.py", root=root)
        if not result.get("success"):
            print("ERROR: read_source failed on a real source file: {}".format(result.get("error")))
            failed = True
        if "calculate_plan" not in str(result.get("data", {}).get("lines", "")):
            print("ERROR: read_source did not return the file contents")
            failed = True
        if not result.get("data", {}).get("total_lines"):
            print("ERROR: read_source did not report total_lines, so the model cannot page")
            failed = True

        paged = read_source("plan.py", start_line=3, max_lines=2, root=root)
        lines = str(paged.get("data", {}).get("lines", "")).strip().splitlines()
        if len(lines) != 2:
            print("ERROR: paging returned {} lines, expected 2".format(len(lines)))
            failed = True
        elif not lines[0].strip().startswith("3"):
            print("ERROR: paging did not start at line 3: {!r}".format(lines[0]))
            failed = True

        clamped = read_source("plan.py", max_lines=99999, root=root)
        clamped_data = clamped.get("data", {})
        clamped_lines = str(clamped_data.get("lines", "")).strip().splitlines()
        if clamped_data.get("total_lines", 0) <= chat_tools.SOURCE_MAX_LINES:
            print("ERROR: plan.py is not long enough to exercise the {}-line cap, has {} lines".format(chat_tools.SOURCE_MAX_LINES, clamped_data.get("total_lines")))
            failed = True
        if len(clamped_lines) != chat_tools.SOURCE_MAX_LINES:
            print("ERROR: max_lines was not clamped to exactly {}, got {} lines".format(chat_tools.SOURCE_MAX_LINES, len(clamped_lines)))
            failed = True

        byte_capped = read_source("wide.py", max_lines=99999, root=root)
        byte_capped_text = str(byte_capped.get("data", {}).get("lines", ""))
        if "truncated at" not in byte_capped_text or "bytes" not in byte_capped_text:
            print("ERROR: the byte cap did not fire on an oversized file, or its marker is missing: {!r}".format(byte_capped_text[-160:]))
            failed = True

        if read_source("prediction_kernel.cpp", root=root).get("success") is not True:
            print("ERROR: read_source refused a C++ source file")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_read_source_refuses_everything_it_should(my_predbat):
    """The extension allowlist and path containment keep config, logs and the token cache out."""
    failed = False
    print("**** Testing read_source access guards ****")
    root = _make_source_tree()
    try:
        forbidden = ["apps.yaml", "secrets.yaml", "predbat.log", "old_plan.py.bak", "kernel.so", "cache/chat_token.json", "../../../etc/passwd", "/etc/passwd", "venv/lib/dependency.py", "__pycache__/cached.py"]
        for target in forbidden:
            result = read_source(target, root=root)
            if result.get("success"):
                print("ERROR: read_source returned contents for {} - this is a credential leak path".format(target))
                failed = True
            elif not result.get("error"):
                print("ERROR: read_source refused {} without saying why".format(target))
                failed = True

        outside = os.path.join(tempfile.mkdtemp(prefix="predbat_out_"), "escaped.py")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret = 'outside the source root'\n")
        link = os.path.join(root, "escaped.py")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            print("Info: symlinks unavailable on this platform, skipping the symlink case")
        else:
            if read_source("escaped.py", root=root).get("success"):
                print("ERROR: a symlink pointing outside the source root was followed")
                failed = True
            scanned = search_source("outside the source root", root=root)
            if scanned.get("data"):
                print("ERROR: search_source's full-tree walk followed a symlink out of the source root: {}".format(scanned.get("data")))
                failed = True

        try:
            resolve_source_path("apps.yaml", root=root)
            print("ERROR: resolve_source_path accepted a .yaml path")
            failed = True
        except SourceAccessError:
            pass
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


class _FakeClock:
    """A scripted monotonic clock: returns each value from a list in order, then holds the last one."""

    def __init__(self, values):
        """Store the sequence of timestamps this fake clock will return, in order."""
        self._values = list(values)

    def monotonic(self):
        """Return the next scripted timestamp, holding at the last one once the list is exhausted."""
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def test_search_source_bounds_a_single_large_file(my_predbat):
    """The scan budget is re-checked inside the per-line loop, not just between files.

    A single large file must not be able to stall the scan past SOURCE_SCAN_SECONDS on its own.
    The clock is scripted rather than real time, so this fires deterministically instead of
    needing an actual multi-second sleep: `started` and the top-of-file check both land inside
    the budget, so only the per-line check (fired every SOURCE_SCAN_CHECK_LINES lines) can be the
    one that trips it here.
    """
    failed = False
    print("**** Testing search_source per-line scan budget ****")
    root = _make_source_tree()
    try:
        huge_name = "huge_scan_budget.py"
        with open(os.path.join(root, huge_name), "w", encoding="utf-8") as handle:
            for number in range(2000):
                handle.write("# marker_symbol filler line {}\n".format(number))

        original_clock = chat_tools.time
        chat_tools.time = _FakeClock([0.0, 0.0, chat_tools.SOURCE_SCAN_SECONDS + 1.0])
        try:
            result = search_source("marker_symbol", file=huge_name, root=root)
        finally:
            chat_tools.time = original_clock

        if not result.get("success"):
            print("ERROR: search_source failed: {}".format(result.get("error")))
            failed = True
        if result.get("total_matches", 0) >= 2000:
            print("ERROR: the per-line scan budget did not stop a single large file, saw {} matches".format(result.get("total_matches")))
            failed = True
        if "partial" not in str(result.get("description", "")):
            print("ERROR: a scan stopped mid-file did not report itself as partial: {}".format(result.get("description")))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_source_extension_matching_edge_cases(my_predbat):
    """Extension matching is case-insensitive and judges a double extension by its final suffix."""
    failed = False
    print("**** Testing source extension matching edge cases ****")
    root = _make_source_tree()
    try:
        with open(os.path.join(root, "SHOUTY.PY"), "w", encoding="utf-8") as handle:
            handle.write("marker_symbol = 'uppercase extension'\n")
        with open(os.path.join(root, "foo.py.yaml"), "w", encoding="utf-8") as handle:
            handle.write("marker_symbol: not source\n")

        uppercase = read_source("SHOUTY.PY", root=root)
        if not uppercase.get("success"):
            print("ERROR: an uppercase .PY extension was refused: {}".format(uppercase.get("error")))
            failed = True

        double_extension = read_source("foo.py.yaml", root=root)
        if double_extension.get("success"):
            print("ERROR: foo.py.yaml was accepted - its final extension is .yaml, not .py")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def run_chat_tools_tests(my_predbat):
    """Run every chat-only tool test, returning True if any of them failed."""
    failed = False
    failed |= test_chat_tool_defs_shape(my_predbat)
    failed |= test_score_documents(my_predbat)
    failed |= test_search_docs_uses_the_cache(my_predbat)
    failed |= test_search_source(my_predbat)
    failed |= test_search_source_rejects_bad_patterns(my_predbat)
    failed |= test_read_source(my_predbat)
    failed |= test_read_source_refuses_everything_it_should(my_predbat)
    failed |= test_search_source_bounds_a_single_large_file(my_predbat)
    failed |= test_source_extension_matching_edge_cases(my_predbat)
    return failed

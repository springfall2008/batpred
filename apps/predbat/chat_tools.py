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
import re
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

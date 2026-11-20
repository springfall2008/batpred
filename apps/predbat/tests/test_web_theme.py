"""Tests for shared theme validation, persistence and concurrent edits."""

import asyncio
import copy
import json
from types import SimpleNamespace

from aiohttp import web
from web_theme import DEFAULT_THEME, WebTheme, valid_theme


class MemoryStorage:
    """Minimal storage used to exercise the web API."""

    def __init__(self):
        """Start without a saved theme."""
        self.data = None
        self.fail = False

    async def load(self, module, filename):
        """Load the saved document."""
        return copy.deepcopy(self.data)

    async def save(self, module, filename, data, **kwargs):
        """Save a copy unless storage has failed."""
        if self.fail:
            return False
        self.data = copy.deepcopy(data)
        return True

    async def age(self, module, filename):
        """Return an age when an entry exists."""
        return 0 if self.data is not None else None


class Request:
    """JSON request stub."""

    def __init__(self, data):
        """Keep an independent copy of the submitted document."""
        self.data = copy.deepcopy(data)

    async def json(self):
        """Return request JSON."""
        return self.data


async def check_theme_api():
    """Check defaults, validation, persistence, conflicts and save errors."""
    storage = MemoryStorage()
    api = WebTheme(SimpleNamespace(storage=storage))
    assert json.loads((await api.get(None)).text) == DEFAULT_THEME
    data = copy.deepcopy(DEFAULT_THEME)
    data["colours"]["light"]["battery-low"] = "#123456"
    data["socBoundaries"] = {"low": 30, "medium": 70}
    data["preset"] = "retro"
    assert valid_theme(data)
    first, second = await asyncio.gather(api.post(Request(data)), api.post(Request(data)))
    assert sorted([first.status, second.status]) == [200, 409]
    assert storage.data["revision"] == 1 and storage.data["colours"]["light"]["battery-low"] == "#123456"
    assert storage.data["preset"] == "retro"
    assert json.loads((await WebTheme(SimpleNamespace(storage=storage)).get(None)).text) == storage.data
    for invalid in ["red", "#fff", "url(https://example.com)"]:
        bad = copy.deepcopy(data)
        bad["colours"]["dark"]["battery-low"] = invalid
        assert not valid_theme(bad)
    bad = copy.deepcopy(data)
    bad["socBoundaries"] = {"low": 70, "medium": 30}
    assert not valid_theme(bad)
    bad = copy.deepcopy(data)
    bad["preset"] = "unknown"
    assert not valid_theme(bad)
    legacy = copy.deepcopy(storage.data)
    legacy.pop("preset")
    storage.data = legacy
    assert json.loads((await api.get(None)).text)["preset"] == "custom"
    storage.data = {**legacy, "preset": "coastal"}
    migrated = json.loads((await api.get(None)).text)
    assert migrated["preset"] == "predbat" and migrated["socBoundaries"]["low"] == 30
    assert not valid_theme(storage.data)
    storage.data = copy.deepcopy(data)
    storage.data["revision"] = 1
    storage.fail = True
    data["revision"] = 1
    try:
        await api.post(Request(data))
        assert False, "failed storage must not report success"
    except web.HTTPServiceUnavailable:
        assert storage.data["revision"] == 1
    storage.data = None
    try:
        await api.get(None)
        assert False, "a previously saved theme must not silently disappear"
    except web.HTTPServiceUnavailable:
        pass


def run_web_theme_tests(my_predbat):
    """Run theme API tests in the standard Predbat test suite."""
    asyncio.run(check_theme_api())
    print("PASS: shared theme validation and persistence")
    return False

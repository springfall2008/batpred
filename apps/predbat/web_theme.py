"""Shared modern UI colour and battery threshold preferences."""

import asyncio
import re

from aiohttp import web


COLOUR_KEYS = {"battery-low", "battery-medium", "battery-high", "charge", "export", "flow-solar", "flow-grid", "flow-home"}
DEFAULT_THEME = {"version": 1, "revision": 0, "preset": "predbat", "colours": {"light": {}, "dark": {}}, "socBoundaries": {"low": 20, "medium": 50}}


def valid_theme(data):
    """Validate a complete theme document without accepting arbitrary CSS."""
    if not isinstance(data, dict) or set(data) not in ({"version", "revision", "colours", "socBoundaries"}, {"version", "revision", "preset", "colours", "socBoundaries"}):
        return False
    if "preset" in data and data["preset"] not in ("predbat", "retro", "custom"):
        return False
    if type(data["version"]) is not int or data["version"] != 1 or type(data["revision"]) is not int or data["revision"] < 0:
        return False
    colours = data["colours"]
    if not isinstance(colours, dict) or set(colours) != {"light", "dark"}:
        return False
    for palette in colours.values():
        if not isinstance(palette, dict) or not set(palette).issubset(COLOUR_KEYS):
            return False
        if any(not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value) for value in palette.values()):
            return False
    bounds = data["socBoundaries"]
    return isinstance(bounds, dict) and set(bounds) == {"low", "medium"} and all(type(bounds[key]) is int for key in bounds) and 0 <= bounds["low"] < bounds["medium"] <= 100


class WebTheme:
    """Read and conditionally save shared UI preferences."""

    def __init__(self, interface):
        """Keep writes serialised within the web server."""
        self.interface = interface
        self.lock = asyncio.Lock()
        self.seen_saved = False

    async def load(self):
        """Return saved preferences or defaults when none have been saved."""
        if not self.interface.storage:
            raise web.HTTPServiceUnavailable(text="Theme storage is unavailable")
        saved = await self.interface.storage.load("web", "theme")
        if saved is None:
            if self.seen_saved or await self.interface.storage.age("web", "theme") is not None:
                raise web.HTTPServiceUnavailable(text="Saved theme settings could not be read")
            return DEFAULT_THEME
        if isinstance(saved, dict) and saved.get("preset") == "coastal":
            saved = {**saved, "preset": "predbat"}
        if not valid_theme(saved):
            raise web.HTTPInternalServerError(text="Stored theme settings are invalid")
        if "preset" not in saved:
            saved = {**saved, "preset": "custom" if any(saved["colours"].values()) else "predbat"}
        self.seen_saved = True
        return saved

    async def get(self, request):
        """Return the currently saved theme."""
        async with self.lock:
            return web.json_response(await self.load(), headers={"Cache-Control": "no-store"})

    async def post(self, request):
        """Save a valid theme only if the supplied revision is current."""
        try:
            data = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid theme settings")
        if not valid_theme(data):
            raise web.HTTPBadRequest(text="Invalid theme settings")
        async with self.lock:
            current = await self.load()
            if data["revision"] != current["revision"]:
                return web.json_response(current, status=409)
            data["revision"] += 1
            if not await self.interface.storage.save("web", "theme", data, format="json", expiry=None):
                raise web.HTTPServiceUnavailable(text="Unable to save theme settings")
            self.seen_saved = True
            return web.json_response(data, headers={"Cache-Control": "no-store"})

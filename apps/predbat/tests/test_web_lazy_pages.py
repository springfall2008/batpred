# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the lazily loaded web pages: the annual and chat pages, ruamel, tarfile and PredHeat are
only imported when something uses them, so a Predbat that never does holds none of that code in memory."""

import asyncio
import inspect
import os
import subprocess
import sys

from web import LazyPage, YAML

# Modules that importing web (and predbat, debug_history, download) must no longer pull in
DEFERRED_MODULES = ["web_chat", "web_annual", "annual", "chat", "chat_tools", "agent_tools", "predheat", "ruamel", "tarfile", "web_helper"]


class FakePage:
    """A page with one route handler and one plain attribute, counting how it is used."""

    built = 0

    def __init__(self):
        """Count the build"""
        FakePage.built += 1
        self.value = 42
        self.requests = []

    async def html_thing(self, request):
        """Echo the request so the test can see it arrived"""
        self.requests.append(request)
        return "response:{}".format(request)


def test_web_lazy_pages_proxy(my_predbat):
    """
    LazyPage builds nothing until a handler is called or a plain attribute is read, and builds only once
    """
    failed = False
    FakePage.built = 0
    lazy = LazyPage(FakePage)

    if lazy.loaded() or FakePage.built != 0:
        print("ERROR: LazyPage built the page before it was used")
        failed = True

    handler = lazy.html_thing
    if FakePage.built != 0 or lazy.loaded():
        print("ERROR: reading a route handler built the page, so registering routes would load it")
        failed = True
    if not inspect.iscoroutinefunction(handler):
        print("ERROR: the route handler is not a coroutine function, aiohttp would reject it")
        failed = True
    if handler.__name__ != "html_thing":
        print("ERROR: the route handler lost its name: {}".format(handler.__name__))
        failed = True

    response = asyncio.run(handler("req1"))
    if response != "response:req1" or FakePage.built != 1 or not lazy.loaded():
        print("ERROR: the first request did not build the page once and reach it: {} built {}".format(response, FakePage.built))
        failed = True

    response = asyncio.run(lazy.html_thing("req2"))
    if response != "response:req2" or FakePage.built != 1:
        print("ERROR: a second request rebuilt the page or was not delegated: {} built {}".format(response, FakePage.built))
        failed = True
    if lazy.get().requests != ["req1", "req2"]:
        print("ERROR: the real page did not see both requests: {}".format(lazy.get().requests))
        failed = True

    plain = LazyPage(FakePage)
    FakePage.built = 0
    if plain.value != 42 or FakePage.built != 1:
        print("ERROR: reading a plain attribute did not build the page and return it: built {}".format(FakePage.built))
        failed = True

    private = LazyPage(FakePage)
    FakePage.built = 0
    try:
        private._missing
        print("ERROR: a private attribute should raise AttributeError")
        failed = True
    except AttributeError:
        pass
    if FakePage.built != 0:
        print("ERROR: probing a private attribute built the page")
        failed = True

    return failed


def test_web_lazy_pages_yaml(my_predbat):
    """
    The YAML factory in web.py returns a working ruamel parser
    """
    failed = False
    parser = YAML()
    data = parser.load("a: 1\nb: [x, y]\n")
    if data["a"] != 1 or list(data["b"]) != ["x", "y"]:
        print("ERROR: the lazy YAML parser did not parse: {}".format(data))
        failed = True
    width_parser = YAML(typ="rt")
    if not hasattr(width_parser, "dump"):
        print("ERROR: YAML(typ=...) did not pass its arguments through")
        failed = True
    return failed


def test_web_lazy_pages_web_helper(my_predbat):
    """
    LazyWebHelper hands back web_helper's own builders, and refuses private names without importing anything
    """
    import web
    import web_helper

    failed = False
    if web.web_helper.get_plan_css is not web_helper.get_plan_css:
        print("ERROR: the lazy web_helper did not return web_helper's own get_plan_css")
        failed = True
    if web.web_helper.get_header_html is not web_helper.get_header_html:
        print("ERROR: the lazy web_helper did not return web_helper's own get_header_html")
        failed = True
    try:
        web.web_helper._missing
        print("ERROR: a private attribute of the lazy web_helper should raise AttributeError")
        failed = True
    except AttributeError:
        pass
    try:
        web.web_helper.get_no_such_builder
        print("ERROR: an unknown builder on the lazy web_helper should raise AttributeError")
        failed = True
    except AttributeError:
        pass
    return failed


def test_web_lazy_pages_routes(my_predbat):
    """
    Importing web, predbat, debug_history and download in a fresh interpreter loads none of the deferred modules
    """
    failed = False
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ("import sys\n" "sys.path.insert(0, {root!r})\n" "import web, predbat, debug_history, download\n" "bad = [m for m in {mods!r} if m in sys.modules]\n" "print('LOADED', ','.join(bad))\n").format(root=root, mods=DEFERRED_MODULES)
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=root, timeout=300)
    loaded_line = [line for line in result.stdout.splitlines() if line.startswith("LOADED")]
    if result.returncode != 0 or not loaded_line:
        print("ERROR: could not import web in a fresh interpreter: {}".format(result.stderr[-2000:]))
        return True
    loaded = [m for m in loaded_line[0][len("LOADED") :].strip().split(",") if m]
    if loaded:
        print("ERROR: importing web still loads modules that should wait until used: {}".format(loaded))
        failed = True
    return failed


def run_web_lazy_pages_tests(my_predbat):
    """
    Run every lazy web page test
    """
    failed = False
    failed |= test_web_lazy_pages_proxy(my_predbat)
    failed |= test_web_lazy_pages_yaml(my_predbat)
    failed |= test_web_lazy_pages_web_helper(my_predbat)
    failed |= test_web_lazy_pages_routes(my_predbat)
    return failed

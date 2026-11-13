# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on
"""Tests for the optional web interface login (web_username / web_password, GH#6103)."""

import asyncio
import base64
import os
import shutil
import tempfile
import time

import requests
from aiohttp import web as aiohttp_web
from aiohttp.test_utils import make_mocked_request

from components import Components
from tests.test_components import LoggingMockBase
from tests.test_validate_config import _run as _run_validate
from tests.test_web_if import _free_port
from web import WebInterface


def _basic(username, password):
    """Return an Authorization header value for HTTP Basic credentials."""
    return "Basic " + base64.b64encode("{}:{}".format(username, password).encode()).decode()


def _status(interface, method, path, authorization=None):
    """Run a request through the interface's auth middleware and return the status code (200 means the handler ran)."""

    async def handler(request):
        """Stand-in page handler that always succeeds."""
        return aiohttp_web.Response(text="ok")

    headers = {"Authorization": authorization} if authorization else {}
    request = make_mocked_request(method, path, headers=headers)
    return asyncio.run(interface.auth_middleware(request, handler)).status


def test_web_auth_middleware(my_predbat):
    """Check which requests the login protects, and what it accepts, without opening a socket."""
    failed = False
    print("**** Testing web login middleware ****")

    protected_gets = [
        "/annual",
        "/annual_run",
        "/chat",
        "/chat/history",
        "/apps",
        "/apps_value",
        "/config",
        "/components",
        "/component_config",
        "/apps_editor",
        "/discovery",
        "/browse",
        "/internals",
        "/api/internals",
        "/api/internals/download",
        "/kraken_key",
        "/download",
        "/debug_yaml",
        "/debug_apps_live",
    ]
    open_gets = ["/", "/dash", "/dash_content", "/metrics_dashboard", "/plan", "/charts", "/entity", "/compare", "/log", "/api/log", "/api/state", "/api/ping", "/api/status", "/api/entities", "/metrics", "/images/bat_logo.svg"]
    protected_posts = ["/apps", "/config", "/dash", "/entity", "/compare", "/plan_override", "/rate_override", "/restart", "/inverter_refresh", "/api/state", "/api/service", "/api/kraken_key", "/some_plugin_post"]

    # No credentials configured: nothing changes
    plain = WebInterface(my_predbat, web_port=5057)
    if plain.web_auth_enabled():
        print("ERROR: login should be off when neither web_username nor web_password is set")
        failed = True
    for method, path in [("GET", p) for p in protected_gets] + [("POST", p) for p in protected_posts]:
        if _status(plain, method, path) != 200:
            print("ERROR: {} {} should be open with no login configured".format(method, path))
            failed = True

    secured = WebInterface(my_predbat, web_port=5058, web_username="admin", web_password="swordfish:pw")
    if not secured.web_auth_enabled():
        print("ERROR: login should be on when web_username and web_password are set")
        failed = True
    good = _basic("admin", "swordfish:pw")

    for path in open_gets:
        if _status(secured, "GET", path) != 200:
            print("ERROR: GET {} should stay open without a login".format(path))
            failed = True
    for method, path in [("GET", p) for p in protected_gets] + [("POST", p) for p in protected_posts]:
        if _status(secured, method, path) != 401:
            print("ERROR: {} {} should need a login".format(method, path))
            failed = True
        if _status(secured, method, path, good) != 200:
            print("ERROR: {} {} should accept the right login".format(method, path))
            failed = True

    # /api/login only passes a proxy cookie along and checks nothing, so it must keep working
    if _status(secured, "POST", "/api/login") != 200:
        print("ERROR: POST /api/login should not need a login")
        failed = True

    bad_logins = {
        "wrong password": _basic("admin", "Swordfish:pw"),
        "wrong username": _basic("root", "swordfish:pw"),
        "password prefix": _basic("admin", "swordfish"),
        "empty": _basic("", ""),
        "not base64": "Basic !!!not-base64!!!",
        "not basic": "Bearer swordfish:pw",
        "no space": "Basic",
        "not utf8": "Basic " + base64.b64encode(b"admin:\xff\xfe").decode(),
    }
    for name, header in bad_logins.items():
        if _status(secured, "POST", "/restart", header) != 401:
            print("ERROR: {} should be refused".format(name))
            failed = True

    # The 401 must carry a challenge or the browser never shows its login box
    request = make_mocked_request("GET", "/apps")

    async def handler(request):
        """Stand-in page handler that always succeeds."""
        return aiohttp_web.Response(text="ok")

    challenge = asyncio.run(secured.auth_middleware(request, handler)).headers.get("WWW-Authenticate", "")
    if not challenge.startswith("Basic realm="):
        print("ERROR: 401 should send a Basic WWW-Authenticate challenge, got {!r}".format(challenge))
        failed = True

    # An all-digit password arrives from YAML as an int
    numeric = WebInterface(my_predbat, web_port=5059, web_username="admin", web_password=123456)
    if _status(numeric, "POST", "/restart", _basic("admin", "123456")) != 200:
        print("ERROR: a numeric YAML password should match its string form")
        failed = True

    # Half configured fails closed, whichever half is missing
    for kwargs in ({"web_username": "admin"}, {"web_password": "swordfish"}):
        half = WebInterface(my_predbat, web_port=5060, **kwargs)
        if not half.web_auth_enabled():
            print("ERROR: {} should still turn the login on".format(list(kwargs)))
            failed = True
        if _status(half, "GET", "/apps", _basic("admin", "swordfish")) != 401 or _status(half, "GET", "/apps", _basic("admin", "")) != 401:
            print("ERROR: {} alone must refuse every login".format(list(kwargs)))
            failed = True
        if _status(half, "GET", "/dash") != 200:
            print("ERROR: {} alone should leave the data views open".format(list(kwargs)))
            failed = True

    return failed


def test_web_auth_server(my_predbat):
    """Start the real web component with a login and check the browser-facing behaviour end to end."""
    failed = False
    print("**** Testing web login against a running server ****")

    original_dir = os.getcwd()
    temp_dir = tempfile.mkdtemp(prefix="predbat_test_")
    orig_ha_if = my_predbat.ha_interface
    try:
        with open(os.path.join(temp_dir, "predbat.log"), "w") as handle:
            handle.write("Predbat debug log\n")
        os.chdir(temp_dir)

        web_port = _free_port()
        base_url = "http://127.0.0.1:{}".format(web_port)
        my_predbat.args["web_port"] = web_port
        my_predbat.args["web_username"] = "admin"
        my_predbat.args["web_password"] = "swordfish"
        my_predbat.components = Components(my_predbat)
        my_predbat.components.initialize()
        my_predbat.components.start("ha_interface")
        my_predbat.components.start("db")
        my_predbat.components.start("web")

        res = requests.get(base_url + "/dash")
        if res.status_code != 200:
            print("ERROR: /dash should be open, got {}".format(res.status_code))
            failed = True

        for method, path in [("GET", "/apps"), ("GET", "/apps_editor"), ("GET", "/annual"), ("GET", "/config"), ("POST", "/restart")]:
            res = requests.request(method, base_url + path)
            if res.status_code != 401 or not res.headers.get("WWW-Authenticate", "").startswith("Basic"):
                print("ERROR: {} {} should ask for a login, got {}".format(method, path, res.status_code))
                failed = True

        res = requests.get(base_url + "/apps", auth=("admin", "wrong"))
        if res.status_code != 401:
            print("ERROR: /apps should refuse a wrong password, got {}".format(res.status_code))
            failed = True

        res = requests.get(base_url + "/apps", auth=("admin", "swordfish"))
        if res.status_code != 200:
            print("ERROR: /apps should open with the right login, got {}".format(res.status_code))
            failed = True

        my_predbat.create_task(my_predbat.components.stop("ha_interface"))
        my_predbat.create_task(my_predbat.components.stop("web"))
        my_predbat.create_task(my_predbat.components.stop("db"))
        time.sleep(0.1)
    finally:
        for key in ("web_port", "web_username", "web_password"):
            my_predbat.args.pop(key, None)
        my_predbat.components = Components(my_predbat)
        my_predbat.ha_interface = orig_ha_if
        os.chdir(original_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)

    return failed


def test_web_enable(my_predbat):
    """web_enable: false leaves the web component out entirely, and anything else keeps it on."""
    failed = False
    print("**** Testing web_enable ****")

    original_components = my_predbat.components
    saved_args = my_predbat.args.copy()
    try:
        # Through the real get_arg: unset, empty (YAML null) and true all keep the web interface
        for name, value in (("unset", "unset"), ("empty", None), ("true", True), ("false", False)):
            my_predbat.args.pop("web_enable", None)
            if value != "unset":
                my_predbat.args["web_enable"] = value
            comps = Components(my_predbat)
            comps.initialize(only="web", phase=0)
            built = comps.get_component("web") is not None
            if built != (value is not False):
                print("ERROR: web_enable {} should {}build the web component".format(name, "" if value is not False else "not "))
                failed = True
            if comps.load_error("web"):
                print("ERROR: web_enable {} should not record a load error, got {}".format(name, comps.load_error("web")))
                failed = True

        # Off means no server at all: start() has nothing to run and the port stays closed
        web_port = _free_port()
        my_predbat.args["web_enable"] = False
        my_predbat.args["web_port"] = web_port
        comps = Components(my_predbat)
        my_predbat.components = comps
        comps.initialize(only="web", phase=0)
        if not comps.start(only="web"):
            print("ERROR: start() should not report a failure for a disabled web interface")
            failed = True
        try:
            requests.get("http://127.0.0.1:{}/dash".format(web_port), timeout=2)
            print("ERROR: nothing should be listening on the web port when web_enable is false")
            failed = True
        except requests.exceptions.ConnectionError:
            pass

        # A deliberate off switch is logged as such, not as missing configuration, even with other web keys left set
        base = LoggingMockBase(web_enable=False, web_port=5070, web_username="admin")
        Components(base).initialize(only="web", phase=0)
        if any(message.startswith("Warn: Skipping") for message in base.log_messages):
            print("ERROR: web_enable false should not warn about missing configuration, got {}".format(base.log_messages))
            failed = True
        if not any(message == "Info: Web Interface interface disabled by web_enable: false in apps.yaml" for message in base.log_messages):
            print("ERROR: web_enable false should log that the web interface is disabled, got {}".format(base.log_messages))
            failed = True

        # The on-by-default resolution must not leak into the off-by-default required_true switches
        base = LoggingMockBase()
        comps = Components(base)
        comps.initialize(only="db", phase=0)
        if comps.get_component("db") is not None:
            print("ERROR: db_enable has no default, so an unset db_enable must leave the database off")
            failed = True
    finally:
        # If the switch was ignored a real server is running; stop it so a failure cannot hang the suite
        if my_predbat.components is not original_components and my_predbat.components.get_component("web") is not None:
            my_predbat.create_task(my_predbat.components.stop("web"))
            time.sleep(0.1)
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        my_predbat.components = original_components

    return failed


def test_web_auth_schema(my_predbat):
    """APPS_SCHEMA accepts the forms WebInterface accepts, so a working login is not reported as a config error."""
    print("**** Testing web login apps.yaml schema ****")
    # An unquoted all-digit username or password arrives from YAML as an int, which initialize() stringifies
    _run_validate(my_predbat, {"web_username": 1234, "web_password": 123456, "web_enable": False}, expect_clean=["web_username", "web_password", "web_enable"])
    _run_validate(my_predbat, {"web_username": "admin", "web_password": "swordfish", "web_enable": True}, expect_clean=["web_username", "web_password", "web_enable"])
    # Still a real check: a mapping (a mis-indented block under the key) is neither a string nor an integer
    _run_validate(my_predbat, {"web_password": {"value": "swordfish"}}, expect_errors=["web_password"])
    return False


def run_web_auth_tests(my_predbat):
    """Run all the web login tests, returning True if any failed."""
    return test_web_auth_middleware(my_predbat) or test_web_auth_server(my_predbat) or test_web_enable(my_predbat) or test_web_auth_schema(my_predbat)

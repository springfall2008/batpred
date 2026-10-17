# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Kraken API key generator (kraken_key.py and the /kraken_key web page).

E.ON Next stopped accepting email/password for the Kraken API in October 2026 and its website
offers no API key, so the page lets a user paste the token from their own browser sign-in and
have Predbat mint a key. Two properties matter more than the markup: the token and the key never
reach the log, and an existing key is never replaced without the user confirming it.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from web import WebInterface

TOKEN = "eyJhbGciOiJSUzI1NiJ9.test-payload.test-signature"
# Not sk_live_-shaped: a realistic-looking key trips secret scanning on push
NEW_KEY = "test-kraken-key-NeWkEy-0123456789"


def fake_kraken_session(responses):
    """Patch target for aiohttp.ClientSession that answers each POST with the next (status, body) in turn.

    Records every request's JSON payload and headers on the returned session as .requests.
    """
    queue = list(responses)
    dumps = json.dumps  # the post() parameter below is called json, as aiohttp's is
    session = MagicMock()
    session.requests = []

    def post(url, json=None, headers=None):
        """Return a context manager yielding the next queued response."""
        session.requests.append({"url": url, "json": json, "headers": headers or {}})
        status, body = queue.pop(0)
        response = MagicMock()
        response.status = status
        response.text = AsyncMock(return_value=body if isinstance(body, str) else dumps(body))
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    session.post = MagicMock(side_effect=post)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def run_kraken(coro_factory, responses):
    """Run a kraken_key coroutine against queued fake Kraken responses; return (result, session)."""
    session = fake_kraken_session(responses)
    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(coro_factory())
    return result, session


class FakePostRequest:
    """A minimal aiohttp-request stand-in for a JSON POST."""

    def __init__(self, body):
        """Store the body a handler will read; a str is returned as invalid JSON."""
        self._body = body

    async def json(self):
        """Return the parsed body, raising as aiohttp does for a body that is not JSON."""
        if isinstance(self._body, str):
            raise json.JSONDecodeError("Expecting value", self._body, 0)
        return self._body


class FakeGetRequest:
    """A minimal aiohttp-request stand-in for a GET with no query string."""

    query = {}


def logged_text(my_predbat_log_mock):
    """Join every message passed to a log mock into one string."""
    return " ".join(str(c.args[0]) for c in my_predbat_log_mock.call_args_list if c.args)


# ---------------------------------------------------------------------------
# kraken_key.py
# ---------------------------------------------------------------------------


def test_normalise_kraken_token():
    """Whitespace and a JWT/Bearer prefix copied along with the header value are stripped."""
    from kraken_key import normalise_kraken_token

    assert normalise_kraken_token(f"  {TOKEN}\n") == TOKEN
    assert normalise_kraken_token(f"JWT {TOKEN}") == TOKEN
    assert normalise_kraken_token(f"Bearer {TOKEN}") == TOKEN
    assert normalise_kraken_token(f"Authorization: {TOKEN}") == TOKEN
    assert normalise_kraken_token("   ") == ""
    assert normalise_kraken_token(None) == ""


def test_check_reports_no_key():
    """An account with no API key reports 'none', sending the token bare as the Authorization header."""
    from kraken_key import check_kraken_key

    result, session = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(200, {"data": {"viewer": {"liveSecretKey": None}}})])

    assert result == {"status": "none"}, result
    assert session.requests[0]["url"] == "https://kraken.example/v1/graphql/"
    assert session.requests[0]["headers"]["Authorization"] == TOKEN
    assert "liveSecretKey" in session.requests[0]["json"]["query"]


def test_check_reports_masked_existing_key():
    """A masked key means one exists that only regenerating can reveal - and regenerating replaces it."""
    from kraken_key import check_kraken_key

    masked = "sk_live_AbCd" + "*" * 28
    result, session = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(200, {"data": {"viewer": {"liveSecretKey": masked}}})])

    assert result == {"status": "existing", "masked": masked}, result
    assert len(session.requests) == 1, "a check must never call regenerateSecretKey"


def test_check_returns_a_full_key_as_is():
    """If Kraken hands back the whole key, it is shown as it is so the user's other apps keep working."""
    from kraken_key import check_kraken_key

    result, _ = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(200, {"data": {"viewer": {"liveSecretKey": NEW_KEY}}})])

    assert result == {"status": "full", "key": NEW_KEY}, result


def test_check_reports_a_rejected_token_without_echoing_it():
    """A rejected token reports Kraken's reason, with the token itself scrubbed from it."""
    from kraken_key import check_kraken_key

    body = {"errors": [{"message": f"Invalid token {TOKEN}", "extensions": {"errorCode": "KT-CT-1139"}}]}
    result, _ = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(200, body)])

    assert result["status"] == "error", result
    assert "KT-CT-1139" in result["error"], result
    assert TOKEN not in result["error"], result


def test_check_reports_http_and_network_failures():
    """A non-JSON reply and an unreachable Kraken both come back as errors, not exceptions."""
    from kraken_key import check_kraken_key

    result, _ = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(502, "<html>Bad gateway</html>")])
    assert result["status"] == "error" and "502" in result["error"], result

    session = MagicMock()
    session.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("boom"))
    session.__aexit__ = AsyncMock(return_value=None)
    with patch("aiohttp.ClientSession", return_value=session):
        result = asyncio.run(check_kraken_key("https://kraken.example", TOKEN))
    assert result["status"] == "error", result


def test_generate_regenerates_and_verifies_the_new_key():
    """Generate calls regenerateSecretKey, then proves the new key signs in with obtainKrakenToken."""
    from kraken_key import generate_kraken_key

    responses = [
        (200, {"data": {"regenerateSecretKey": {"key": NEW_KEY}}}),
        (200, {"data": {"obtainKrakenToken": {"token": "jwt-from-key", "refreshToken": "r", "payload": {"exp": 1}}}}),
    ]
    result, session = run_kraken(lambda: generate_kraken_key("https://kraken.example", TOKEN), responses)

    assert result == {"status": "generated", "key": NEW_KEY, "verified": True}, result
    assert "regenerateSecretKey" in session.requests[0]["json"]["query"]
    assert session.requests[0]["headers"]["Authorization"] == TOKEN
    assert session.requests[1]["json"]["variables"] == {"input": {"APIKey": NEW_KEY}}
    assert "Authorization" not in session.requests[1]["headers"], "the sign-in check must use the new key alone"


def test_generate_still_returns_the_key_when_verification_fails():
    """The old key is already gone once regenerate succeeds, so a failed check must not lose the new one."""
    from kraken_key import generate_kraken_key

    responses = [
        (200, {"data": {"regenerateSecretKey": {"key": NEW_KEY}}}),
        (200, {"errors": [{"message": "Invalid data.", "extensions": {"errorCode": "KT-CT-1138"}}]}),
    ]
    result, _ = run_kraken(lambda: generate_kraken_key("https://kraken.example", TOKEN), responses)

    assert result == {"status": "generated", "key": NEW_KEY, "verified": False}, result


def test_generate_reports_a_refused_regenerate():
    """A refused regenerate is an error, and no sign-in check is attempted."""
    from kraken_key import generate_kraken_key

    body = {"errors": [{"message": "Signature of the JWT has expired.", "extensions": {"errorCode": "KT-CT-1124"}}]}
    result, session = run_kraken(lambda: generate_kraken_key("https://kraken.example", TOKEN), [(200, body)])

    assert result["status"] == "error" and "KT-CT-1124" in result["error"], result
    assert len(session.requests) == 1


def test_normalise_strips_internal_whitespace():
    """A header value copied wrapped across lines loses its line breaks (#5585 review).

    JWTs never contain whitespace, and aiohttp refuses a header containing a newline with a plain
    ValueError, which used to escape the handler as a 500.
    """
    from kraken_key import normalise_kraken_token

    assert normalise_kraken_token("JWT " + TOKEN[:20] + "\n  " + TOKEN[20:] + "\r\n") == TOKEN


def test_check_reports_unknown_when_kraken_does_not_answer_the_question():
    """A 200 with no viewer is 'unknown', not 'no key' - it must not unlock an unconfirmed generate (#5585 review)."""
    from kraken_key import check_kraken_key

    for body in ({"data": None}, {"data": {"viewer": None}}, {"data": {}}):
        result, _ = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(200, body)])
        assert result == {"status": "unknown"}, (body, result)


def test_non_dict_data_is_an_error_not_a_crash():
    """A 200 whose data is a list or scalar is reported as an error on check and generate (#5585 review)."""
    from kraken_key import check_kraken_key, generate_kraken_key

    for data in ([1, 2], "text", 3):
        result, _ = run_kraken(lambda: check_kraken_key("https://kraken.example", TOKEN), [(200, {"data": data})])
        assert result["status"] == "error", (data, result)
        result, _ = run_kraken(lambda: generate_kraken_key("https://kraken.example", TOKEN), [(200, {"data": data})])
        assert result["status"] == "error", (data, result)


# ---------------------------------------------------------------------------
# /api/kraken_key and /kraken_key
# ---------------------------------------------------------------------------


def test_api_check_with_a_wrapped_token_reaches_kraken(web):
    """A token pasted with a line break is cleaned before it becomes a header, so no 500 (#5585 review)."""
    session = fake_kraken_session([(200, {"data": {"viewer": {"liveSecretKey": None}}})])
    with patch("aiohttp.ClientSession", return_value=session):
        status, data, _ = post_api(web, {"provider": "eon", "token": TOKEN[:20] + "\n" + TOKEN[20:], "action": "check"})
    assert status == 200 and data == {"status": "none"}, data
    assert session.requests[0]["headers"]["Authorization"] == TOKEN


def test_api_check_failure_never_logs_the_token(web):
    """The check path logs Kraken's reason on failure - with the token scrubbed even if Kraken echoes it (#5585 review)."""
    web.log = MagicMock()
    body = {"errors": [{"message": "Token {} is invalid".format(TOKEN), "extensions": {"errorCode": "KT-CT-1139"}}]}
    with patch("aiohttp.ClientSession", return_value=fake_kraken_session([(200, body)])):
        status, data, _ = post_api(web, {"provider": "eon", "token": TOKEN, "action": "check"})
    assert status == 200 and data["status"] == "error", data
    text = logged_text(web.log)
    assert "KT-CT-1139" in text, text
    assert TOKEN not in text and "test-payload" not in text, text


def post_api(web, body):
    """Call the API handler and return (status, parsed JSON body, response)."""
    response = asyncio.run(web.html_api_kraken_key(FakePostRequest(body)))
    return response.status, json.loads(response.text), response


def test_api_rejects_bad_requests(web):
    """Invalid JSON, an unknown provider, an empty token and an unknown action are all 400s."""
    for body in ["not json", {"provider": "octopus", "token": TOKEN, "action": "check"}, {"provider": "eon", "token": "  ", "action": "check"}, {"provider": "eon", "token": TOKEN, "action": "delete"}]:
        status, data, response = post_api(web, body)
        assert status == 400, (body, status)
        assert data["status"] == "error", data
        assert response.headers.get("Cache-Control") == "no-store"


def test_api_check_uses_the_provider_base_url(web):
    """A check goes to the chosen provider's Kraken endpoint with the normalised token."""
    from kraken import KRAKEN_BASE_URLS

    with patch("kraken_key.check_kraken_key", new=AsyncMock(return_value={"status": "none"})) as check:
        status, data, response = post_api(web, {"provider": "eon", "token": f"JWT {TOKEN}", "action": "check"})

    assert status == 200 and data == {"status": "none"}, data
    check.assert_awaited_once_with(KRAKEN_BASE_URLS["eon"], TOKEN)
    assert response.headers.get("Cache-Control") == "no-store"


def test_api_generate_never_logs_the_token_or_key(web):
    """Generating logs that it happened, but neither the token nor the key."""
    web.log = MagicMock()
    with patch("kraken_key.generate_kraken_key", new=AsyncMock(return_value={"status": "generated", "key": NEW_KEY, "verified": True})) as generate:
        status, data, _ = post_api(web, {"provider": "edf", "token": TOKEN, "action": "generate"})

    assert status == 200 and data["key"] == NEW_KEY, data
    generate.assert_awaited_once()
    text = logged_text(web.log)
    assert "Kraken API key" in text, text
    assert TOKEN not in text and NEW_KEY not in text and "NeWkEy" not in text, text


def test_page_explains_how_to_find_the_token(web):
    """The page carries the short how-to and posts to the API, with no auto-refresh to wipe a pasted token."""
    response = asyncio.run(web.html_kraken_key(FakeGetRequest()))
    html = response.text

    assert "Kraken API key generator" in html
    assert "Authorization" in html and "graphql" in html
    assert "./api/kraken_key" in html
    assert 'http-equiv="refresh"' not in html.lower() and "http-equiv='refresh'" not in html.lower()
    assert response.headers.get("Cache-Control") == "no-store"


def test_page_snippet_and_messages():
    """The apps.yaml snippet is complete and the page's recovery messages are actionable (#5585 review)."""
    from web_helper import get_kraken_key_page_html

    html = get_kraken_key_page_html()
    # components.py declares kraken_account_id required, so a snippet without it fails validation
    assert "kraken_account_id:" in html
    # An indeterminate check result asks for the same confirmation as an existing key
    assert "result.status === 'unknown'" in html
    # A refused clipboard write says how to copy by hand instead of failing silently
    copy_start = html.index("function krakenKeyCopy")
    assert ".catch(" in html[copy_start : html.index("</script>", copy_start)]
    # Generating again after a failed sign-in check needs a new token from the browser
    assert "fresh token" in html


def unterminated_js_strings(script):
    """Return the line numbers where a '...' or "..." JS string literal runs onto the next line.

    A Python \\n escape inside the page's triple-quoted string becomes a real newline in the
    served script, which breaks the whole <script> block - this catches that without a JS engine.
    """
    bad = []
    quote = None
    escaped = False
    line = 1
    i = 0
    while i < len(script):
        char = script[i]
        if char == "\n":
            if quote in ("'", '"'):
                bad.append(line)
                quote = None
            line += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif script.startswith("//", i):
            while i < len(script) and script[i] != "\n":
                i += 1
            continue
        elif char in ("'", '"', "`"):
            quote = char
        i += 1
    return bad


def test_page_script_has_no_broken_string_literals():
    """Every JS string literal in the page script closes on its own line."""
    from web_helper import get_kraken_key_page_html

    html = get_kraken_key_page_html()
    script = html[html.index("<script>") + len("<script>") : html.index("</script>")]
    assert unterminated_js_strings(script) == [], unterminated_js_strings(script)


# ---------------------------------------------------------------------------
# Advanced menu
# ---------------------------------------------------------------------------


def menu_popup(html, menu_id):
    """Return the markup of one sub-menu popup."""
    start = html.index('id="{}"'.format(menu_id))
    return html[start : html.index("</div>", start)]


def test_menu_groups_advanced_pages():
    """Discovery, Browse, Internals and Kraken API Key live under an Advanced sub-menu, not the top bar."""
    from web_helper import get_menu_html

    html = get_menu_html(False, "./dash", [], "v0.0.0", "")
    dropdown = menu_popup(html, "advanced-menu")
    for href in ("./discovery", "./browse", "./internals", "./kraken_key"):
        assert f"href='{href}'" in dropdown, href
        assert html.count(f"href='{href}'") == 1, f"{href} should appear only inside Advanced"
    # The toggle is a button, not a link, so setActiveMenuItem() cannot match it to every page
    assert '<button class="menu-dropdown-toggle" type="button" data-menu="advanced-menu"' in html


def test_menu_groups_settings_pages():
    """Apps, Config, Components and Editor live under a Settings sub-menu, in that order."""
    from web_helper import get_menu_html

    html = get_menu_html(False, "./dash", [], "v0.0.0", "")
    dropdown = menu_popup(html, "settings-menu")
    positions = []
    for href in ("./apps", "./config", "./components", "./apps_editor"):
        assert f"href='{href}'" in dropdown, href
        assert html.count(f"href='{href}'") == 1, f"{href} should appear only inside Settings"
        positions.append(dropdown.index(f"href='{href}'"))
    assert positions == sorted(positions), "Settings should list Apps, Config, Components, Editor"
    assert '<button class="menu-dropdown-toggle" type="button" data-menu="settings-menu"' in html


def test_menu_top_bar_order():
    """Dash, Metrics, Plan, Charts, Entities ... Log, then Settings, Advanced and Docs."""
    from web_helper import get_menu_html

    html = get_menu_html(False, "./dash", [], "v0.0.0", "", chat_enabled=True)
    order = ["href='./dash'", "href='./metrics_dashboard'", "href='./plan'", "href='./charts'", "href='./entity'", "href='./log'", 'data-menu="settings-menu"', 'data-menu="advanced-menu"', "springfall2008.github.io/batpred/'"]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions), list(zip(order, positions))
    assert html.index("href='./chat'") < html.index("href='./log'")


def test_menu_settings_toggle_carries_the_config_warning():
    """An apps.yaml error flags the Settings toggle too, so the warning is visible with the menu closed."""
    from web_helper import get_menu_html

    warning = "&#9888;"
    clean = get_menu_html(False, "./dash", [], "v0.0.0", "")
    assert warning not in clean
    html = get_menu_html(False, "./dash", {"some_arg": "bad value"}, "v0.0.0", "")
    toggle_start = html.index('data-menu="settings-menu"')
    assert warning in html[toggle_start : html.index("</button>", toggle_start)]
    assert warning in menu_popup(html, "settings-menu"), "the Apps link keeps its warning too"


def test_menu_popup_is_not_inside_the_menu_bar():
    """The Advanced popup must sit outside .menu-bar.

    The bar is position: fixed with overflow-x: auto, and WebKit (Safari, the iOS companion app)
    clips a position: fixed child to such a parent - so inside it the popup opened invisibly on
    iPhones while Chrome showed it fine.
    """
    from html.parser import HTMLParser

    from web_helper import get_menu_html

    class AncestorFinder(HTMLParser):
        """Record the class lists of every open element around each sub-menu popup."""

        VOID = {"img", "br", "input", "meta", "link", "hr"}

        def __init__(self):
            """Start with an empty element stack."""
            super().__init__()
            self.stack = []
            self.ancestors = {}

        def handle_starttag(self, tag, attrs):
            """Push the element; note the ancestors when a popup opens."""
            attrs = dict(attrs)
            if attrs.get("id") in ("advanced-menu", "settings-menu"):
                self.ancestors[attrs["id"]] = [classes for _, classes in self.stack]
            if tag not in self.VOID:
                self.stack.append((tag, attrs.get("class") or ""))

        def handle_endtag(self, tag):
            """Pop back to the matching element."""
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    del self.stack[index:]
                    break

    finder = AncestorFinder()
    finder.feed(get_menu_html(False, "./dash", [], "v0.0.0", ""))
    assert set(finder.ancestors) == {"advanced-menu", "settings-menu"}, finder.ancestors
    for menu_id, ancestors in finder.ancestors.items():
        assert not any("menu-bar" in classes.split() for classes in ancestors), (menu_id, ancestors)


def test_menu_toggle_is_not_painted_by_the_dark_mode_button_rule():
    """The Advanced toggle is a <button>, and dark mode paints every button red with !important.

    Its own dark-mode colours must therefore be !important too, so it looks like the other tabs.
    """
    import re

    from web_helper import get_menu_html

    html = get_menu_html(False, "./dash", [], "v0.0.0", "")
    for selector, colour in (("body.dark-mode .menu-dropdown-toggle", "transparent"), ("body.dark-mode .menu-dropdown-toggle:hover", "#2c652f"), ("body.dark-mode .menu-dropdown-toggle.active", "#4CAF50")):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
        assert match, selector
        rule = match.group(1)
        assert re.search(r"background-color:\s*" + re.escape(colour) + r"\s*!important", rule), (selector, rule)
        assert re.search(r"(^|[;\s])color:\s*white\s*!important", rule), (selector, rule)


def run_kraken_key_tests(my_predbat):
    """Run the Kraken API key generator tests. Returns True on failure, False on success."""
    import inspect
    import sys

    module = sys.modules[__name__]
    web = WebInterface(my_predbat, web_port=5052)
    tests = [obj for name in dir(module) if name.startswith("test_") and callable(obj := getattr(module, name))]
    for test_func in tests:
        try:
            if "web" in inspect.signature(test_func).parameters:
                test_func(web)
            else:
                test_func()
        except Exception as e:
            print(f"  FAIL: {test_func.__name__}: {e}")
            import traceback

            traceback.print_exc()
            return True
        print(f"  OK: {test_func.__name__}")
    return False

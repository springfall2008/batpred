# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the MCP server's OAuth and authorisation checks (#5847).

Dynamic client registration must not hand out a credential, so an unauthenticated caller
cannot turn a registration into an access token.
"""

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from agent_tools import TOOL_DEFS
from web_mcp import PredbatMCPServer, MCPServerWrapper, MCP_STORAGE_MODULE, MCP_SIGNING_KEY_FILE

TEST_SECRET = "a-test-secret-that-is-not-the-default"


class FakeJsonRequest:
    """A minimal aiohttp-request stand-in for the OAuth and MCP handlers."""

    def __init__(self, body=None, form=None, headers=None, method="POST", query=None):
        """Store the JSON body, form data, query string and headers a handler will read."""
        self.body = body or {}
        self.form = form
        self.headers = headers or {"Content-Type": "application/json"}
        self.method = method
        self.remote = "192.0.2.1"
        self.host = "predbat.example:8199"
        self.query = query or {}

    async def json(self):
        """Return the JSON body."""
        return self.body

    async def post(self):
        """Return the form data, falling back to the JSON body."""
        return self.form if self.form is not None else self.body


class FakeStorage:
    """An in-memory stand-in for the Storage component."""

    def __init__(self, save_ok=True):
        """Start empty; save_ok False makes every save fail."""
        self.data = {}
        self.save_ok = save_ok

    async def load(self, module, filename):
        """Return what was saved, or None."""
        return self.data.get((module, filename))

    async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
        """Remember the data, or report failure."""
        if not self.save_ok:
            return False
        self.data[(module, filename)] = data
        return True


def make_server(my_predbat, secret=TEST_SECRET, storage=None):
    """Build an initialised PredbatMCPServer without starting its web server."""
    # A subclass, because ComponentBase.storage is a read-only property looked up through the components registry
    server_class = type("TestMCPServer", (PredbatMCPServer,), {"storage": storage})
    server = server_class.__new__(server_class)
    server.base = my_predbat
    server.log = my_predbat.log
    server.initialize(mcp_enable=True, mcp_secret=secret, mcp_port=8199)
    return server


def call(handler, request):
    """Run an async handler and return (status, decoded JSON body)."""
    response = asyncio.run(handler(request))
    return response.status, json.loads(response.text)


def test_mcp_register_returns_no_secret(my_predbat):
    """A registered client is a public client: no client secret, and only the authorisation code grant (#5847)."""
    failed = False
    print("**** Testing MCP dynamic client registration hands out no credential ****")
    server = make_server(my_predbat)

    status, data = call(server.oauth_register, FakeJsonRequest({"client_name": "probe", "grant_types": ["client_credentials", "authorization_code"], "redirect_uris": ["https://example.com/cb"]}))
    if status != 201:
        print("  ERROR: registration returned {} {}".format(status, data))
        failed = True
    if "client_secret" in data or TEST_SECRET in json.dumps(data):
        print("  ERROR: registration returned a secret: {}".format(data))
        failed = True
    if data.get("token_endpoint_auth_method") != "none":
        print("  ERROR: expected token_endpoint_auth_method none, got {}".format(data.get("token_endpoint_auth_method")))
        failed = True
    if data.get("grant_types") != ["authorization_code"]:
        print("  ERROR: a registered client was granted {}".format(data.get("grant_types")))
        failed = True

    print("Test: the registered client cannot get a token through client credentials")
    status, data_token = call(server.oauth_token, FakeJsonRequest({"grant_type": "client_credentials", "client_id": data.get("client_id"), "client_secret": data.get("client_secret", "")}))
    if status == 200 or "access_token" in data_token:
        print("  ERROR: client credentials with a registered client gave {} {}".format(status, data_token))
        failed = True

    print("Test: client credentials still work with mcp_secret itself")
    status, data_token = call(server.oauth_token, FakeJsonRequest({"grant_type": "client_credentials", "client_id": "operator", "client_secret": TEST_SECRET}))
    if status != 200 or not data_token.get("access_token"):
        print("  ERROR: client credentials with mcp_secret returned {} {}".format(status, data_token))
        failed = True

    print("Test: the server metadata advertises public clients")
    status, metadata = call(server.oauth_metadata, FakeJsonRequest(method="GET"))
    if "none" not in metadata.get("token_endpoint_auth_methods_supported", []):
        print("  ERROR: metadata does not list token_endpoint_auth_method none: {}".format(metadata))
        failed = True
    return failed


def test_mcp_signing_key(my_predbat):
    """The token signing key is random and saved, not derived from mcp_secret (#5847)."""
    failed = False
    print("**** Testing MCP token signing key ****")

    print("Test: a token signed with the old key derived from mcp_secret is rejected")
    server = make_server(my_predbat)
    old_key = hashlib.sha256("jwt_signing_key_{}".format(TEST_SECRET).encode()).hexdigest()
    now = datetime.now(timezone.utc)
    forged = pyjwt.encode({"sub": "x", "client_id": "x", "aud": "http://localhost:8199", "scope": "mcp:read mcp:write mcp:control", "type": "access", "iat": now, "exp": now + timedelta(days=1)}, old_key, algorithm="HS256")
    if server.verify_access_token(forged):
        print("  ERROR: a token signed with the key derived from mcp_secret was accepted")
        failed = True

    print("Test: two servers with the same secret and no saved key do not share a key")
    other = make_server(my_predbat)
    if server.jwt_secret == other.jwt_secret:
        print("  ERROR: two servers derived the same signing key")
        failed = True

    print("Test: the first start saves a key, and the next start loads it, so tokens survive a restart")
    storage = FakeStorage()
    first = make_server(my_predbat, storage=storage)
    asyncio.run(first.load_signing_key())
    saved = storage.data.get((MCP_STORAGE_MODULE, MCP_SIGNING_KEY_FILE))
    if not saved or saved.get("key") != first.jwt_secret:
        print("  ERROR: the signing key was not saved: {}".format(saved))
        failed = True
    token = first.generate_access_token("client")
    second = make_server(my_predbat, storage=storage)
    asyncio.run(second.load_signing_key())
    if second.jwt_secret != first.jwt_secret or not second.verify_access_token(token):
        print("  ERROR: a restarted server did not load the saved key")
        failed = True

    print("Test: a short or malformed saved key is replaced")
    storage = FakeStorage()
    storage.data[(MCP_STORAGE_MODULE, MCP_SIGNING_KEY_FILE)] = {"key": "short"}
    server = make_server(my_predbat, storage=storage)
    asyncio.run(server.load_signing_key())
    if server.jwt_secret == "short" or storage.data[(MCP_STORAGE_MODULE, MCP_SIGNING_KEY_FILE)].get("key") != server.jwt_secret:
        print("  ERROR: a short saved key was used or not replaced")
        failed = True

    print("Test: with no storage, or a failed save, the server keeps its random key")
    for storage in (None, FakeStorage(save_ok=False)):
        server = make_server(my_predbat, storage=storage)
        before = server.jwt_secret
        asyncio.run(server.load_signing_key())
        if server.jwt_secret != before or len(before) < 64:
            print("  ERROR: the per-instance key changed or is too short with storage {}".format(storage))
            failed = True
    return failed


def test_mcp_refuses_default_secret(my_predbat):
    """The server will not run with an empty or default mcp_secret, and reports why without stalling start-up (#5847)."""
    failed = False
    print("**** Testing MCP refuses the default secret ****")

    async def run_briefly(server):
        """Start the server, look at its state while it runs, then stop it."""
        server.api_stop = False
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.05)
        state = (server.api_started, server.is_alive(), server.health_message(), server.mcp_server)
        server.api_stop = True
        await asyncio.wait_for(task, timeout=10)
        return state

    for secret in ("predbat_mcp_secret", "", "   ", None):
        print("Test: mcp_secret {!r} is refused".format(secret))
        server = make_server(my_predbat, secret=secret, storage=FakeStorage())
        started, alive, message, mcp_server = asyncio.run(run_briefly(server))
        if not started:
            print("  ERROR: api_started stayed False, so start-up would wait for the server")
            failed = True
        if alive:
            print("  ERROR: a refused server reported itself healthy")
            failed = True
        if not message or "mcp_secret" not in message:
            print("  ERROR: no health message saying why: {!r}".format(message))
            failed = True
        if mcp_server is not None:
            print("  ERROR: the MCP server was created despite the default secret")
            failed = True
        if server.api_started:
            print("  ERROR: api_started still True after stopping")
            failed = True
    return failed


def test_mcp_scopes_enforced(my_predbat):
    """A token's scopes decide which tools it may call: a read-only token cannot change Predbat (#5847)."""
    failed = False
    print("**** Testing MCP token scopes are enforced ****")
    server = make_server(my_predbat)
    server.mcp_server = MCPServerWrapper(my_predbat, log_func=my_predbat.log)
    write_tools = [entry["name"] for entry in TOOL_DEFS if entry.get("writes")]
    if not write_tools:
        print("  ERROR: no write tools in TOOL_DEFS to test with")
        return True

    def call_tool(bearer, name):
        """Call one tool over POST /mcp with the given bearer token; return (isError, error text)."""
        request = FakeJsonRequest({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}, headers={"Content-Type": "application/json", "Authorization": "Bearer " + bearer})
        status, data = call(server.html_mcp_post, request)
        result = data.get("result", {})
        text = result.get("content", [{}])[0].get("text", "")
        return status, result.get("isError", False), text

    read_token = server.generate_access_token("reader", scopes=["mcp:read"])
    write_token = server.generate_access_token("writer", scopes=["mcp:write"])
    for name in write_tools:
        print("Test: a read-only token cannot call {}".format(name))
        status, is_error, text = call_tool(read_token, name)
        if status != 200 or not is_error or "scope" not in text:
            print("  ERROR: {} with a read-only token gave {} {} {}".format(name, status, is_error, text[:200]))
            failed = True
        for label, bearer in (("an mcp:write token", write_token), ("the legacy bearer token", TEST_SECRET)):
            print("Test: {} passes the scope check for {}".format(label, name))
            status, is_error, text = call_tool(bearer, name)
            if status != 200 or "scope" in text:
                print("  ERROR: {} with {} gave {} {}".format(name, label, status, text[:200]))
                failed = True

    print("Test: a read-only token can still call a read tool")
    status, is_error, text = call_tool(read_token, "get_status")
    if status != 200 or "scope" in text:
        print("  ERROR: get_status with a read-only token gave {} {}".format(status, text[:200]))
        failed = True

    print("Test: a token with no MCP scope cannot call a read tool")
    none_token = server.generate_access_token("nobody", scopes=["other"])
    status, is_error, text = call_tool(none_token, "get_status")
    if not is_error or "scope" not in text:
        print("  ERROR: get_status with no MCP scope gave {} {}".format(is_error, text[:200]))
        failed = True
    return failed


def test_mcp_public_client_flow(my_predbat):
    """A registered public client gets a working token through authorise + PKCE, with no client secret, and the scope it asked for at authorise is kept"""
    failed = False
    print("**** Testing MCP public client authorisation code flow ****")
    server = make_server(my_predbat)
    server.mcp_server = MCPServerWrapper(my_predbat, log_func=my_predbat.log)
    _, client = call(server.oauth_register, FakeJsonRequest({"client_name": "connector", "redirect_uris": ["https://example.com/cb"]}))
    verifier = "a-pkce-code-verifier-that-is-long-enough-for-the-test-0123456789"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    form = {"client_id": client["client_id"], "redirect_uri": "https://example.com/cb", "response_type": "code", "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256", "scope": "mcp:read"}

    def authorise(secret):
        """Submit the authorise form with a secret; return the redirect location, or None."""
        response = asyncio.run(server.oauth_authorize(FakeJsonRequest(form=dict(form, secret=secret))))
        return response.headers.get("Location") if response.status in (302, 303) else None

    print("Test: the wrong secret on the authorise page gives no code")
    if authorise("wrong") is not None:
        print("  ERROR: authorise redirected with the wrong secret")
        failed = True

    print("Test: the right secret gives a code, and the code a read-only token with no client secret")
    location = authorise(TEST_SECRET)
    code = parse_qs(urlparse(location or "").query).get("code", [None])[0]
    if not code:
        print("  ERROR: no code in the authorise redirect {}".format(location))
        return True
    status, token = call(server.oauth_token, FakeJsonRequest({"grant_type": "authorization_code", "code": code, "client_id": client["client_id"], "redirect_uri": "https://example.com/cb", "code_verifier": verifier}))
    if status != 200 or token.get("scope") != "mcp:read":
        print("  ERROR: token exchange gave {} {}".format(status, token))
        return True
    payload = server.verify_access_token(token["access_token"])
    if not payload or payload.get("scope") != "mcp:read":
        print("  ERROR: the token does not carry the scope asked for at authorise: {}".format(payload))
        failed = True

    print("Test: a request naming no MCP scope gets all three, and unknown scopes are dropped")
    if server.granted_scopes("openid profile") != ["mcp:read", "mcp:write", "mcp:control"] or server.granted_scopes("openid mcp:read") != ["mcp:read"] or server.granted_scopes(None) != ["mcp:read", "mcp:write", "mcp:control"]:
        print("  ERROR: granted_scopes gave {} / {}".format(server.granted_scopes("openid profile"), server.granted_scopes("openid mcp:read")))
        failed = True
    return failed


def test_mcp_authorise_page_escapes(my_predbat):
    """Values from the request are escaped on the authorise page, so a crafted link cannot run a script that reads the secret"""
    failed = False
    print("**** Testing MCP authorise page escapes request values ****")
    server = make_server(my_predbat)
    payload = "<script>alert(1)</script>"
    query = {"client_id": payload, "redirect_uri": 'https://example.com/cb"><script>x</script>', "response_type": "code", "state": payload, "code_challenge": "abc", "code_challenge_method": "S256", "scope": payload}
    response = asyncio.run(server.oauth_authorize(FakeJsonRequest(method="GET", query=query)))
    if "<script>alert(1)</script>" in response.text or '"><script>' in response.text:
        print("  ERROR: the authorise page contains an unescaped value")
        failed = True
    if "&lt;script&gt;alert(1)&lt;/script&gt;" not in response.text:
        print("  ERROR: the escaped client_id is missing from the page")
        failed = True
    return failed


def run_web_mcp_auth_tests(my_predbat):
    """Run every MCP OAuth/authorisation test, returning True if any of them failed."""
    failed = False
    failed |= test_mcp_register_returns_no_secret(my_predbat)
    failed |= test_mcp_signing_key(my_predbat)
    failed |= test_mcp_refuses_default_secret(my_predbat)
    failed |= test_mcp_scopes_enforced(my_predbat)
    failed |= test_mcp_public_client_flow(my_predbat)
    failed |= test_mcp_authorise_page_escapes(my_predbat)
    return failed

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
import hashlib
import json
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from web_mcp import PredbatMCPServer, MCP_STORAGE_MODULE, MCP_SIGNING_KEY_FILE

TEST_SECRET = "a-test-secret-that-is-not-the-default"


class FakeJsonRequest:
    """A minimal aiohttp-request stand-in for the OAuth and MCP handlers."""

    def __init__(self, body=None, form=None, headers=None, method="POST"):
        """Store the JSON body, form data and headers a handler will read."""
        self.body = body or {}
        self.form = form
        self.headers = headers or {"Content-Type": "application/json"}
        self.method = method
        self.remote = "192.0.2.1"
        self.host = "predbat.example:8199"
        self.query = {}

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


def run_web_mcp_auth_tests(my_predbat):
    """Run every MCP OAuth/authorisation test, returning True if any of them failed."""
    failed = False
    failed |= test_mcp_register_returns_no_secret(my_predbat)
    failed |= test_mcp_signing_key(my_predbat)
    return failed

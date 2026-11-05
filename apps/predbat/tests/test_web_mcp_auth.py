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
import json

from web_mcp import PredbatMCPServer

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


def make_server(my_predbat, secret=TEST_SECRET):
    """Build an initialised PredbatMCPServer without starting its web server."""
    server = PredbatMCPServer.__new__(PredbatMCPServer)
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


def run_web_mcp_auth_tests(my_predbat):
    """Run every MCP OAuth/authorisation test, returning True if any of them failed."""
    failed = False
    failed |= test_mcp_register_returns_no_secret(my_predbat)
    return failed

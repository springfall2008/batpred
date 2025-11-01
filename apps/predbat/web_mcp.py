# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
#
# This code creates a web server and serves up the Predbat web pages
"""
Model Context Protocol (MCP) Server for Predbat

This module provides an MCP server that integrates with the Predbat web interface
to expose battery prediction data and plan information via the MCP protocol.
"""

import asyncio
import json
from typing import Any, Dict
from datetime import datetime, timezone, timedelta
from utils import calc_percent_limit, get_override_time_from_string
import re
from aiohttp import web
import time
import secrets
import jwt as pyjwt


"""
Example usage in VSCode with OAuth
	"servers": {
		"predbat-mcp": {
			"url": "http://homeassistant.local:8199",
			"type": "http",
			"description": "Predbat Model Context Protocol Server",
			"clientId": "predbat-mcp-client"
		}
	}
"""


class PredbatMCPServer:
    """
    Model Context Protocol (MCP) Server for Predbat with OAuth support
    """

    def __init__(self, enable, mcp_secret, mcp_port, base):
        """Initialize the MCP server component"""
        self.enable = enable
        self.mcp_secret = mcp_secret
        self.mcp_port = mcp_port
        self.base = base
        self.mcp_server = None
        self.log = base.log
        self.api_started = False
        self.abort = False
        self.last_success_timestamp = None

        # OAuth configuration
        # Derive JWT signing key from mcp_secret (but don't use it directly)
        # This keeps it stable across restarts but separate from the client secret
        import hashlib

        self.jwt_secret = hashlib.sha256(f"jwt_signing_key_{self.mcp_secret}".encode()).hexdigest()
        self.jwt_algorithm = "HS256"
        self.access_token_lifetime = timedelta(hours=1)
        self.authorization_code_lifetime = timedelta(minutes=10)

        # Authorization codes storage (code -> user/client info)
        self.authorization_codes = {}

    async def start(self):
        """Start the MCP server if enabled"""
        if self.enable:
            self.mcp_server = create_mcp_server(self.base, self.log)
            await self.mcp_server.start()

            # Now create Web UI on port self.mcp_port
            app = web.Application()

            # OAuth metadata endpoints (for OAuth discovery)
            app.router.add_get("/.well-known/oauth-authorization-server", self.oauth_metadata)
            app.router.add_options("/.well-known/oauth-authorization-server", self.handle_options)
            app.router.add_get("/.well-known/oauth-authorization-server/mcp", self.oauth_metadata_mcp)
            app.router.add_options("/.well-known/oauth-authorization-server/mcp", self.handle_options)
            # Protected Resource Metadata (REQUIRED by MCP spec - RFC 9728)
            app.router.add_get("/.well-known/oauth-protected-resource", self.oauth_protected_resource_metadata)
            app.router.add_options("/.well-known/oauth-protected-resource", self.handle_options)

            # OAuth endpoints
            app.router.add_get("/oauth/authorize", self.oauth_authorize)
            app.router.add_post("/oauth/authorize", self.oauth_authorize)  # Handle form submission
            app.router.add_get("/authorize", self.oauth_authorize)  # VSCode compatibility
            app.router.add_post("/authorize", self.oauth_authorize)  # VSCode compatibility
            app.router.add_post("/oauth/token", self.oauth_token)
            app.router.add_post("/token", self.oauth_token)  # VSCode compatibility
            app.router.add_post("/oauth/register", self.oauth_register)  # Dynamic client registration
            app.router.add_post("/register", self.oauth_register)  # Fallback path

            # MCP endpoints (OAuth protected)
            app.router.add_get("/mcp", self.html_mcp_get)
            app.router.add_post("/mcp", self.html_mcp_post)
            app.router.add_get("/", self.html_mcp_get)
            app.router.add_post("/", self.html_mcp_post)

            # Favicon
            app.router.add_get("/favicon.ico", self.favicon)

            # Add default route for any other paths (MUST BE LAST)
            app.router.add_route("*", "/{tail:.*}", self.default_route)

            runner = web.AppRunner(app)
            await runner.setup()

            site = web.TCPSite(runner, "0.0.0.0", self.mcp_port)
            await site.start()

            print("MCP interface started with OAuth support")
            self.api_started = True
            while not self.abort:
                self.last_success_timestamp = datetime.now(timezone.utc)
                await asyncio.sleep(2)
            await runner.cleanup()

            if self.mcp_server:
                self.mcp_server.stop()

            self.api_started = False
            print("MCP interface stopped")

    async def stop(self):
        print("MCP interface stop called")
        self.abort = True
        await asyncio.sleep(1)

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("MCP: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: MCP: Failed to start")
            return False
        return True

    def is_alive(self):
        return self.api_started

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        return self.last_success_timestamp

    def verify_oauth_client(self, client_id: str, client_secret: str) -> bool:
        """
        Verify OAuth client credentials against configured secret

        Args:
            client_id: The OAuth client ID
            client_secret: The client secret to verify

        Returns:
            True if credentials are valid, False otherwise
        """
        return client_secret == self.mcp_secret

    def get_canonical_server_uri(self, host: str = None) -> str:
        """
        Get the canonical URI of this MCP server (RFC 8707)

        Args:
            host: Optional host header from request

        Returns:
            Canonical server URI without trailing slash
        """
        if host:
            return f"http://{host}"
        return f"http://localhost:{self.mcp_port}"

    def generate_access_token(self, client_id: str, resource: str = None, scopes: list = None) -> str:
        """
        Generate a JWT access token with audience and scope claims

        Args:
            client_id: The OAuth client ID
            resource: The target resource URI (for audience claim)
            scopes: List of scopes to grant

        Returns:
            JWT access token string
        """
        if not pyjwt:
            raise RuntimeError("PyJWT is not installed. Install with: pip install PyJWT")

        # Default scopes if not specified
        if scopes is None:
            scopes = ["mcp:read", "mcp:write", "mcp:control"]

        # Default audience if not specified
        if resource is None:
            resource = f"http://localhost:{self.mcp_port}"

        payload = {
            "sub": client_id,
            "client_id": client_id,
            "aud": resource,  # Audience claim (REQUIRED by MCP spec)
            "scope": " ".join(scopes),  # Space-separated scopes
            "type": "access",
            "iat": datetime.utcnow(),
            "exp": datetime.utcnow() + self.access_token_lifetime,
        }
        return pyjwt.encode(payload, self.jwt_secret, algorithm=self.jwt_algorithm)

    def verify_access_token(self, token: str, expected_audience: str = None) -> Dict[str, Any]:
        """
        Verify and decode a JWT access token with audience validation

        Args:
            token: The JWT token to verify
            expected_audience: Expected audience (this server's URI)

        Returns:
            Token payload if valid, None if invalid
        """
        try:
            # Build list of acceptable audiences for flexibility
            acceptable_audiences = []

            if expected_audience:
                acceptable_audiences.append(expected_audience)

            # Also accept default audience for backward compatibility
            default_audience = f"http://localhost:{self.mcp_port}"
            if default_audience not in acceptable_audiences:
                acceptable_audiences.append(default_audience)

            # Try to verify with each acceptable audience
            payload = None
            last_error = None

            for aud in acceptable_audiences:
                try:
                    payload = pyjwt.decode(token, self.jwt_secret, algorithms=[self.jwt_algorithm], audience=aud)
                    break  # Success!
                except Exception as e:
                    last_error = e
                    continue  # Try next audience

            if not payload:
                # None of the audiences worked
                # Try to decode without verification to see what's in the token (for debugging)
                try:
                    unverified = pyjwt.decode(token, options={"verify_signature": False})
                except Exception as e2:
                    self.log(f"MCP: Could not even decode without verification: {e2}")

                if last_error and "audience" in str(last_error).lower():
                    self.log(f"MCP: Invalid token audience: {last_error}")
                else:
                    self.log(f"MCP: Token verification failed: {last_error}")
                return None

            if payload.get("type") != "access":
                self.log(f"MCP: Token type is not 'access'")
                return None

            return payload
        except (AttributeError, Exception) as e:
            # Handle both ExpiredSignatureError and InvalidTokenError
            print(f"MCP: Exception during token verification: {e}")
            if "expired" in str(e).lower():
                self.log("MCP: Access token expired")
            elif "audience" in str(e).lower():
                self.log(f"MCP: Invalid token audience: {e}")
            else:
                self.log(f"MCP: Invalid access token: {e}")
            return None

    async def handle_options(self, request):
        """Handle CORS preflight OPTIONS requests"""
        self.log("MCP: CORS preflight OPTIONS request from {}".format(request.remote))
        response = web.Response(status=200)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Max-Age"] = "86400"  # 24 hours
        return response

    async def oauth_protected_resource_metadata(self, request):
        """
        OAuth 2.0 Protected Resource Metadata endpoint (RFC 9728)
        REQUIRED by MCP specification

        This endpoint advertises the authorization server location and supported capabilities.
        """
        base_url = f"http://{request.host}"
        self.log(f"MCP: Protected Resource Metadata request from {request.remote}")

        metadata = {"resource": base_url, "authorization_servers": [base_url], "scopes_supported": ["mcp:read", "mcp:write", "mcp:control"], "bearer_methods_supported": ["header"], "resource_documentation": f"{base_url}/docs"}

        self.log(f"MCP: Protected Resource Metadata request from {request.remote}")

        # Add CORS headers for cross-origin requests (needed by ChatGPT, Claude, etc.)
        response = web.json_response(metadata)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    async def oauth_metadata(self, request):
        """
        OAuth 2.0 Authorization Server Metadata endpoint

        This is the well-known endpoint that clients use to discover OAuth configuration.
        See: RFC 8414 - OAuth 2.0 Authorization Server Metadata
        """
        base_url = f"http://{request.host}"
        self.log(f"MCP: OAuth Metadata request from {request.remote}")

        metadata = {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],  # Only S256, plain is deprecated
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "scopes_supported": ["mcp:read", "mcp:write", "mcp:control"],
        }

        # Add CORS headers for cross-origin requests (needed by ChatGPT, Claude, etc.)
        response = web.json_response(metadata)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    async def oauth_metadata_mcp(self, request):
        """
        OAuth 2.0 Authorization Server Metadata endpoint for /mcp path

        This is a ChatGPT-specific endpoint that provides OAuth configuration
        for the /mcp resource path.
        See: RFC 8414 - OAuth 2.0 Authorization Server Metadata
        """
        base_url = f"http://{request.host}"
        self.log("MCP: OAuth MCP Metadata request from {}".format(request.remote))

        metadata = {
            "issuer": base_url,
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
            "registration_endpoint": f"{base_url}/oauth/register",
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],  # Only S256, plain is deprecated
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "scopes_supported": ["mcp:read", "mcp:write", "mcp:control"],
        }

        # Add CORS headers for cross-origin requests (needed by ChatGPT, Claude, etc.)
        response = web.json_response(metadata)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return response

    async def favicon(self, request):
        """Return a bat emoji as favicon"""
        # Use SVG for a simple bat emoji favicon
        self.log("MCP: Favicon request from {}".format(request.remote))
        svg_content = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <text y="85" font-size="85" font-family="Arial, sans-serif">🦇</text>
</svg>"""
        return web.Response(text=svg_content, content_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})  # Cache for 24 hours

    async def default_route(self, request):
        """Handle all unmatched routes with 404"""
        self.log("MCP: Default route for {} - 404 Not Found".format(request.path))
        return web.Response(text="Not Found", status=404)

    async def oauth_authorize(self, request):
        """
        OAuth 2.0 authorization endpoint - handles Authorization Code flow

        This endpoint shows a login page where users enter the secret, just like
        ChatGPT's OAuth flow. After successful login, it redirects back with an
        authorization code.
        """
        self.log("MCP OAuth: Authorization request received from {}".format(request.remote))
        try:
            # Check if this is a POST request (form submission)
            if request.method == "POST":
                # For POST, read from form data
                data = await request.post()
                client_id = data.get("client_id")
                redirect_uri = data.get("redirect_uri")
                response_type = data.get("response_type")
                state = data.get("state", "")
                code_challenge = data.get("code_challenge", "")
                code_challenge_method = data.get("code_challenge_method", "")
                submitted_secret = data.get("secret", "")

                self.log(f"MCP OAuth: POST authorization request from client_id: {client_id}")

                # Validate required parameters
                if not client_id or not redirect_uri or response_type != "code":
                    error_message = "Missing required OAuth parameters"
                    self.log(f"MCP OAuth: {error_message}")
                else:
                    if submitted_secret == self.mcp_secret:
                        self.log(f"MCP OAuth: User authenticated successfully")

                        # Generate authorization code
                        auth_code = secrets.token_urlsafe(32)
                        self.authorization_codes[auth_code] = {
                            "client_id": client_id,
                            "redirect_uri": redirect_uri,
                            "code_challenge": code_challenge if code_challenge else None,
                            "code_challenge_method": code_challenge_method if code_challenge_method else None,
                            "state": state,
                            "expires": datetime.utcnow() + self.authorization_code_lifetime,
                            "used": False,
                        }

                        self.log(f"MCP OAuth: Authorization code generated for {client_id} with state={state}")

                        # Redirect back with code and state
                        separator = "&" if "?" in redirect_uri else "?"
                        redirect_url = f"{redirect_uri}{separator}code={auth_code}"
                        if state:
                            redirect_url += f"&state={state}"

                        self.log(f"MCP OAuth: Redirecting to {redirect_url}")
                        return web.HTTPFound(redirect_url)
                    else:
                        # Invalid secret - show login page with error
                        error_message = "Invalid secret. Please try again."
                        self.log(f"MCP OAuth: Invalid secret provided")
            else:
                # GET request - extract from query string
                client_id = request.query.get("client_id")
                redirect_uri = request.query.get("redirect_uri")
                response_type = request.query.get("response_type")
                state = request.query.get("state", "")
                code_challenge = request.query.get("code_challenge")
                code_challenge_method = request.query.get("code_challenge_method")
                error_message = ""

                self.log(f"MCP OAuth: GET authorization request from client_id: {client_id}")

                # Validate required parameters
                if not client_id or not redirect_uri or response_type != "code":
                    error_uri = redirect_uri if redirect_uri else "about:blank"
                    return web.HTTPFound(f"{error_uri}?error=invalid_request&state={state}")

                # Validate redirect URI (MCP spec requirement)
                if not self.validate_redirect_uri(redirect_uri):
                    self.log(f"MCP OAuth: Invalid redirect URI in GET request")
                    return web.HTTPFound(f"about:blank?error=invalid_request&error_description=Invalid+redirect_uri&state={state}")

                # PKCE is REQUIRED per OAuth 2.1 and MCP spec
                if not code_challenge or not code_challenge_method:
                    error_uri = redirect_uri if redirect_uri else "about:blank"
                    error_desc = "PKCE required: code_challenge and code_challenge_method are required"
                    self.log(f"MCP OAuth: {error_desc}")
                    return web.HTTPFound(f"{error_uri}?error=invalid_request&error_description={error_desc}&state={state}")

                # Only S256 method is supported (plain is deprecated in OAuth 2.1)
                if code_challenge_method != "S256":
                    error_uri = redirect_uri if redirect_uri else "about:blank"
                    error_desc = "code_challenge_method must be S256"
                    self.log(f"MCP OAuth: {error_desc}")
                    return web.HTTPFound(f"{error_uri}?error=invalid_request&error_description={error_desc}&state={state}")

            # Show login page (GET request or failed POST)
            login_html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Predbat MCP - Sign In</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
        }}
        .login-container {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
            padding: 40px;
            max-width: 400px;
            width: 100%;
        }}
        .logo {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .logo h1 {{
            margin: 0;
            color: #333;
            font-size: 28px;
        }}
        .logo p {{
            margin: 5px 0 0 0;
            color: #666;
            font-size: 14px;
        }}
        .client-info {{
            background: #f5f5f5;
            border-radius: 8px;
            padding: 15px;
            margin-bottom: 20px;
        }}
        .client-info p {{
            margin: 5px 0;
            color: #555;
            font-size: 14px;
        }}
        .client-info strong {{
            color: #333;
        }}
        label {{
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 500;
        }}
        input[type="password"] {{
            width: 100%;
            padding: 12px;
            border: 2px solid #ddd;
            border-radius: 6px;
            font-size: 16px;
            box-sizing: border-box;
            transition: border-color 0.3s;
        }}
        input[type="password"]:focus {{
            outline: none;
            border-color: #667eea;
        }}
        .error {{
            background: #fee;
            border: 1px solid #fcc;
            color: #c33;
            padding: 12px;
            border-radius: 6px;
            margin-bottom: 20px;
            font-size: 14px;
        }}
        button {{
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.1s;
        }}
        button:hover {{
            transform: translateY(-1px);
        }}
        button:active {{
            transform: translateY(0);
        }}
        .help-text {{
            margin-top: 20px;
            text-align: center;
            font-size: 13px;
            color: #666;
        }}
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">
            <h1>🔋 Predbat MCP</h1>
            <p>Model Context Protocol Server</p>
        </div>

        <div class="client-info">
            <p><strong>Application:</strong> {client_id}</p>
            <p><strong>Requesting access to:</strong> Battery prediction data</p>
        </div>

        {'<div class="error">' + error_message + '</div>' if error_message else ''}

        <form method="POST">
            <label for="secret">MCP Secret:</label>
            <input type="password" id="secret" name="secret" required autofocus
                   placeholder="Enter your mcp_secret">
            <input type="hidden" name="client_id" value="{client_id}">
            <input type="hidden" name="redirect_uri" value="{redirect_uri}">
            <input type="hidden" name="response_type" value="{response_type}">
            <input type="hidden" name="state" value="{state}">
            <input type="hidden" name="code_challenge" value="{code_challenge or ''}">
            <input type="hidden" name="code_challenge_method" value="{code_challenge_method or ''}">
            <button type="submit">Sign In & Authorize</button>
        </form>

        <div class="help-text">
            This is the secret configured in your<br>
            Home Assistant apps.yaml file
        </div>
    </div>
</body>
</html>
            """

            return web.Response(text=login_html, content_type="text/html")

        except Exception as e:
            self.log(f"MCP OAuth: Error in authorize endpoint: {e}")
            return web.Response(text=f"Authorization error: {str(e)}", status=500)

    async def oauth_register(self, request):
        """
        OAuth 2.0 Dynamic Client Registration endpoint (RFC 7591)

        This endpoint allows MCP clients to automatically register and obtain
        client credentials without manual configuration.

        Note: This is a simplified implementation. In production, you may want to:
        - Store registered clients in a database
        - Implement client metadata validation
        - Add rate limiting
        - Support client authentication for updates/deletions
        """
        self.log("MCP OAuth: Dynamic client registration request received from {}".format(request.remote))
        try:
            # Parse request body
            content_type = request.headers.get("Content-Type", "")

            if "application/json" in content_type:
                data = await request.json()
            else:
                return web.json_response({"error": "invalid_request", "error_description": "Content-Type must be application/json"}, status=400)

            # Extract client metadata (RFC 7591 Section 2)
            client_name = data.get("client_name", "Unnamed MCP Client")
            redirect_uris = data.get("redirect_uris", [])
            grant_types = data.get("grant_types", ["authorization_code"])
            response_types = data.get("response_types", ["code"])
            scope = data.get("scope", "mcp:read mcp:write mcp:control")

            # Validate redirect URIs
            if redirect_uris:
                for uri in redirect_uris:
                    if not self.validate_redirect_uri(uri):
                        return web.json_response({"error": "invalid_redirect_uri", "error_description": f"Invalid redirect URI: {uri}. Must be HTTPS or localhost."}, status=400)

            # Generate client credentials
            # For MCP, we use a shared secret model where all clients use the same secret
            # In a more sophisticated implementation, you'd generate unique credentials per client
            client_id = f"mcp-client-{secrets.token_urlsafe(16)}"

            # Return registration response (RFC 7591 Section 3.2.1)
            response = {
                "client_id": client_id,
                "client_secret": self.mcp_secret,  # Shared secret model
                "client_name": client_name,
                "redirect_uris": redirect_uris,
                "grant_types": grant_types,
                "response_types": response_types,
                "scope": scope,
                "token_endpoint_auth_method": "client_secret_post",
            }

            self.log(f"MCP OAuth: Dynamic client registration successful for {client_name} (client_id: {client_id})")

            return web.json_response(response, status=201)

        except Exception as e:
            self.log(f"MCP OAuth: Error in register endpoint: {e}")
            return web.json_response({"error": "server_error", "error_description": f"Internal server error: {str(e)}"}, status=500)

    async def oauth_token(self, request):
        """
        OAuth 2.0 token endpoint - implements multiple grant types

        Supports:
        - client_credentials: Direct machine-to-machine authentication
        - authorization_code: Browser-based OAuth flow for ChatGPT, Claude, etc.
        """
        self.log("MCP OAuth: Token request received from {}".format(request.remote))
        try:
            # Parse request body
            content_type = request.headers.get("Content-Type", "")

            if "application/json" in content_type:
                data = await request.json()
            else:
                # Support form-encoded as well
                data = await request.post()
                data = dict(data)

            grant_type = data.get("grant_type")

            # Route to appropriate grant type handler
            if grant_type == "client_credentials":
                return await self._handle_client_credentials(data)
            elif grant_type == "authorization_code":
                return await self._handle_authorization_code(data)
            else:
                self.log(f"MCP OAuth: Unsupported grant type: {grant_type}")
                return web.json_response({"error": "unsupported_grant_type", "error_description": f"Grant type '{grant_type}' is not supported. Use 'client_credentials' or 'authorization_code'"}, status=400)

        except Exception as e:
            self.log(f"MCP OAuth: Error in token endpoint: {e}")
            return web.json_response({"error": "server_error", "error_description": f"Internal server error: {str(e)}"}, status=500)

    async def _handle_client_credentials(self, data: dict):
        """Handle client_credentials grant type"""
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        resource = data.get("resource")  # RECOMMENDED by MCP spec (RFC 8707)
        scope = data.get("scope")  # Optional

        # Validate client credentials
        if not client_id or not client_secret:
            self.log("MCP OAuth: Missing client credentials")
            return web.json_response({"error": "invalid_request", "error_description": "client_id and client_secret are required"}, status=400)

        # Resource parameter is recommended but not strictly required for backward compatibility
        # Use default if not provided
        if not resource:
            resource = f"http://localhost:{self.mcp_port}"
            self.log(f"MCP OAuth: No resource parameter provided, using default: {resource}")

        # Verify credentials against configured secret
        if not self.verify_oauth_client(client_id, client_secret):
            self.log(f"MCP OAuth: Invalid credentials for client_id: {client_id}")
            return web.json_response({"error": "invalid_client", "error_description": "Invalid client credentials"}, status=401)

        # Parse scopes if provided
        scopes = None
        if scope:
            scopes = scope.split()

        # Generate access token with audience and scopes
        access_token = self.generate_access_token(client_id, resource=resource, scopes=scopes)

        self.log(f"MCP OAuth: Access token issued for client: {client_id} (client_credentials), resource: {resource}")

        return web.json_response({"access_token": access_token, "token_type": "Bearer", "expires_in": int(self.access_token_lifetime.total_seconds()), "scope": scope if scope else "mcp:read mcp:write mcp:control"}, status=200)

    async def _handle_authorization_code(self, data: dict):
        """Handle authorization_code grant type"""
        code = data.get("code")
        client_id = data.get("client_id")
        client_secret = data.get("client_secret")
        redirect_uri = data.get("redirect_uri")
        code_verifier = data.get("code_verifier")  # For PKCE
        resource = data.get("resource")  # RECOMMENDED by MCP spec (RFC 8707)
        scope = data.get("scope")  # Optional

        # Validate required parameters
        if not code or not client_id or not redirect_uri:
            self.log("MCP OAuth: Missing required parameters for authorization_code")
            return web.json_response({"error": "invalid_request", "error_description": "code, client_id, and redirect_uri are required"}, status=400)

        # Resource parameter is recommended but not strictly required for authorization_code
        # Use default if not provided for compatibility with clients
        if not resource:
            resource = f"http://localhost:{self.mcp_port}"
            self.log(f"MCP OAuth: No resource parameter provided, using default: {resource}")

        # Verify code exists
        code_data = self.authorization_codes.get(code)
        if not code_data:
            self.log(f"MCP OAuth: Invalid authorization code")
            return web.json_response({"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}, status=400)

        # Check if code was already used
        if code_data.get("used"):
            self.log(f"MCP OAuth: Authorization code already used")
            del self.authorization_codes[code]
            return web.json_response({"error": "invalid_grant", "error_description": "Authorization code has already been used"}, status=400)

        # Check expiration
        if datetime.utcnow() > code_data.get("expires"):
            self.log(f"MCP OAuth: Authorization code expired")
            del self.authorization_codes[code]
            return web.json_response({"error": "invalid_grant", "error_description": "Authorization code has expired"}, status=400)

        # Verify client_id matches
        if code_data.get("client_id") != client_id:
            self.log(f"MCP OAuth: client_id mismatch")
            return web.json_response({"error": "invalid_grant", "error_description": "client_id does not match authorization code"}, status=400)

        # Verify redirect_uri matches
        if code_data.get("redirect_uri") != redirect_uri:
            self.log(f"MCP OAuth: redirect_uri mismatch")
            return web.json_response({"error": "invalid_grant", "error_description": "redirect_uri does not match"}, status=400)

        # PKCE verification is REQUIRED (OAuth 2.1 and MCP spec)
        if not code_data.get("code_challenge"):
            self.log(f"MCP OAuth: Authorization code missing PKCE challenge")
            return web.json_response({"error": "invalid_grant", "error_description": "PKCE is required but authorization code has no challenge"}, status=400)

        if not code_verifier:
            return web.json_response({"error": "invalid_request", "error_description": "code_verifier required for PKCE"}, status=400)

        # Verify code_challenge
        import hashlib
        import base64

        verifier_hash = hashlib.sha256(code_verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(verifier_hash).decode().rstrip("=")

        if challenge != code_data.get("code_challenge"):
            self.log(f"MCP OAuth: PKCE verification failed")
            return web.json_response({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status=400)

        # Mark code as used
        code_data["used"] = True

        # Parse scopes if provided
        scopes = None
        if scope:
            scopes = scope.split()

        # Generate access token with audience and scopes
        access_token = self.generate_access_token(client_id, resource=resource, scopes=scopes)

        # Clean up used code after a delay
        async def cleanup_code():
            await asyncio.sleep(60)
            self.authorization_codes.pop(code, None)

        asyncio.create_task(cleanup_code())

        self.log(f"MCP OAuth: Access token issued (authorization_code), resource: {resource}")

        return web.json_response({"access_token": access_token, "token_type": "Bearer", "expires_in": int(self.access_token_lifetime.total_seconds()), "scope": scope if scope else "mcp:read mcp:write mcp:control"}, status=200)

    async def html_mcp_get(self, request):
        """
        Handle GET requests to MCP endpoint - returns server info and available tools
        Supports both OAuth tokens and legacy Bearer token authentication
        """
        self.log("MCP GET: Received request from {}".format(request.remote))
        if not self.mcp_server:
            return web.json_response({"success": False, "error": "MCP server is not available."}, status=503)

        # Check authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header or not auth_header.startswith("Bearer "):
            # REQUIRED: WWW-Authenticate header with resource_metadata (MCP spec)
            base_url = f"http://{request.host}"
            www_auth = f'Bearer realm="{base_url}", ' f'resource_metadata="{base_url}/.well-known/oauth-protected-resource", ' f'scope="mcp:read mcp:write mcp:control"'

            # Return JSON-RPC formatted error for compatibility
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Unauthorized: Missing or invalid Authorization header. Use 'Authorization: Bearer <token>' header."}}, status=401, headers={"WWW-Authenticate": www_auth}
            )

        token = auth_header.replace("Bearer ", "")
        base_url = f"http://{request.host}"

        # Try OAuth token first (with audience validation)
        token_payload = self.verify_access_token(token, expected_audience=base_url)
        if token_payload:
            # Valid OAuth token
            client_id = token_payload.get("client_id", "unknown")
            self.log(f"MCP GET: Authenticated via OAuth token (client: {client_id})")
        elif token == self.mcp_secret:
            # Legacy direct secret authentication (backwards compatible)
            self.log("MCP GET: Authenticated via legacy bearer token")
        else:
            # Invalid token - return JSON-RPC formatted error with WWW-Authenticate
            www_auth = f'Bearer realm="{base_url}", ' f'error="invalid_token", ' f'error_description="Invalid or expired token", ' f'resource_metadata="{base_url}/.well-known/oauth-protected-resource"'

            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Unauthorized: Invalid or expired token."}}, status=401, headers={"WWW-Authenticate": www_auth})

        try:
            result = await self.mcp_server.handle_mcp_request(request, self.mcp_server)
            return web.json_response(result)
        except Exception as e:
            self.log(f"Error in MCP GET endpoint: {e}")
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Server error: {str(e)}"}}, status=500)

    async def html_mcp_post(self, request):
        """
        Handle POST requests to MCP endpoint - executes tools via JSON-RPC 2.0
        Supports both OAuth tokens and legacy Bearer token authentication
        """
        self.log("MCP POST: Received request from {}".format(request.remote))
        if not self.mcp_server:
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "MCP server is not available."}}, status=503)

        # Check authorization header
        auth_header = request.headers.get("Authorization", "")
        if not auth_header or not auth_header.startswith("Bearer "):
            self.log("MCP POST: Missing or invalid Authorization header")

            # REQUIRED: WWW-Authenticate header with resource_metadata (MCP spec)
            base_url = f"http://{request.host}"
            www_auth = f'Bearer realm="{base_url}", ' f'resource_metadata="{base_url}/.well-known/oauth-protected-resource", ' f'scope="mcp:read mcp:write mcp:control"'

            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Unauthorized: Missing or invalid Authorization header. Use 'Authorization: Bearer <token>' header."}}, status=401, headers={"WWW-Authenticate": www_auth}
            )

        token = auth_header.replace("Bearer ", "")
        base_url = f"http://{request.host}"

        # Try OAuth token first (with audience validation)
        token_payload = self.verify_access_token(token, expected_audience=base_url)
        if token_payload:
            # Valid OAuth token
            client_id = token_payload.get("client_id", "unknown")
            self.log(f"MCP POST: Authenticated via OAuth token (client: {client_id})")
        elif token == self.mcp_secret:
            # Legacy direct secret authentication (backwards compatible)
            self.log("MCP POST: Authenticated via legacy bearer token")
        else:
            # Invalid token
            self.log("MCP POST: Invalid token")

            www_auth = f'Bearer realm="{base_url}", ' f'error="invalid_token", ' f'error_description="Token verification failed - please refresh authentication", ' f'resource_metadata="{base_url}/.well-known/oauth-protected-resource"'

            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Unauthorized: Token verification failed."}}, status=401, headers={"WWW-Authenticate": www_auth})

        try:
            result = await self.mcp_server.handle_mcp_request(request, self.mcp_server)
            return web.json_response(result)
        except Exception as e:
            self.log(f"Error in MCP POST endpoint: {e}")
            return web.json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": f"Server error: {str(e)}"}}, status=500)


class MCPServerWrapper:
    """
    Wrapper class for the MCP Server to provide HTTP-based MCP functionality for web interface
    """

    def __init__(self, base, log_func=None):
        """Initialize the MCP server wrapper"""
        self.base = base
        self.log = log_func or print
        self.is_running = False

        if log_func:
            log_func("Creating HTTP MCP Server with Predbat integration")

    async def start(self):
        """Start the MCP server (web interface compatibility)"""
        self.is_running = True
        if self.log:
            self.log("HTTP MCP Server started (web interface integration)")

    async def stop(self):
        """Stop the MCP server (web interface compatibility)"""
        self.is_running = False
        if self.log:
            self.log("HTTP MCP Server stopped")

    async def handle_request(self, request):
        """
        Handle HTTP MCP requests

        Args:
            request: HTTP request object

        Returns:
            JSON response following MCP protocol
        """
        return await self.handle_mcp_request(request, self)

    async def _execute_get_plan(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_plan tool"""
        try:
            # Check if we have plan data available
            if not hasattr(self.base, "raw_plan") or not self.base.raw_plan:
                return {"success": False, "error": "No plan data available", "data": None}

            # Return the complete plan data
            return {"success": True, "error": None, "data": self.base.raw_plan, "timestamp": datetime.now().isoformat(), "description": "Current Predbat battery plan including forecasts, costs, and operational states"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving plan data: {str(e)}", "data": None}

    async def _execute_get_entities(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get current Predbat entities
        """
        try:
            filter = arguments.get("filter", None)
            entities = self.base.dashboard_values
            returned_entities = []
            for entity in entities:
                entity_id = entity.get("entity_id", "")
                if filter:
                    if not re.search(filter, entity_id):
                        continue
                value = {
                    "entity_id": entity.get("entity_id"),
                    "state": entity.get("state"),
                    "friendly_name": entity.get("friendly_name"),
                }
                if "unit_of_measurement" in entity:
                    value["unit_of_measurement"] = entity.get("unit_of_measurement")
                if "device_class" in entity:
                    value["device_class"] = entity.get("device_class")
                if "state_class" in entity:
                    value["state_class"] = entity.get("state_class")
                if "icon" in entity:
                    value["icon"] = entity.get("icon")
                returned_entities.append(value)
            return {"success": True, "error": None, "data": returned_entities, "timestamp": datetime.now().isoformat(), "description": "The current Predbat entities and their states"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving entities data: {str(e)}", "data": None}

    async def _execute_set_plan_override(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a plan override request
        """
        try:
            action = arguments.get("action", None)
            time_str = arguments.get("time", None)

            if not action or not time_str:
                return {"success": False, "error": "Missing required parameters", "data": None}

            action = action.lower()
            action = action.replace(" ", "_")

            now_utc = self.base.now_utc
            override_time = get_override_time_from_string(now_utc, time_str)
            if not override_time:
                return {"success": False, "error": "Invalid time format. Use 'Day HH:MM' format e.g. Sat 14:30", "data": None}

            minutes_from_now = (override_time - now_utc).total_seconds() / 60
            if minutes_from_now >= 17 * 60:
                return {"success": False, "error": "Override time must be within 17 hours from now.", "data": None}

            selection_option = "{}".format(override_time.strftime("%H:%M:%S"))
            clear_option = "[{}]".format(override_time.strftime("%H:%M:%S"))
            if action == "clear":
                await self.base.async_manual_select("manual_demand", selection_option)
                await self.base.async_manual_select("manual_demand", clear_option)
            else:
                if action == "demand":
                    await self.base.async_manual_select("manual_demand", selection_option)
                elif action == "charge":
                    await self.base.async_manual_select("manual_charge", selection_option)
                elif action == "export":
                    await self.base.async_manual_select("manual_export", selection_option)
                elif action == "freeze_charge":
                    await self.base.async_manual_select("manual_freeze_charge", selection_option)
                elif action == "freeze_export":
                    await self.base.async_manual_select("manual_freeze_export", selection_option)
                else:
                    return {"success": False, "error": "Unknown action {}".format(action), "data": None}

            # Refresh plan
            self.base.update_pending = True
            self.base.plan_valid = False
            return {"success": True, "error": None, "data": {"action": action, "time": override_time.isoformat()}, "timestamp": datetime.now().isoformat(), "description": f"Plan override applied: {action} at {override_time.isoformat()}"}
        except Exception as e:
            return {"success": False, "error": f"Error applying plan override: {str(e)}", "data": None}

    async def _execute_get_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get full HA configuration for Predbat
        """
        try:
            entity_id_filter = arguments.get("filter", None)
            config_return = []
            for item in self.base.CONFIG_ITEMS:
                if entity_id_filter:
                    entity_id = item.get("entity", None)
                    if entity_id and re.search(entity_id_filter, entity_id):
                        config_return.append(item)
                else:
                    config_return.append(item)
            return {"success": True, "error": None, "data": config_return, "timestamp": datetime.now().isoformat(), "description": "The contents of the Predbat configuration settings"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving apps.yaml data: {str(e)}", "data": None}

    async def _execute_get_apps(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_apps tool"""
        try:
            configuration = self.base.args
            return_configuration = {}
            config_id_filter = arguments.get("filter", None)
            for key, value in configuration.items():
                if config_id_filter:
                    if re.search(config_id_filter, key):
                        return_configuration[key] = value
                else:
                    return_configuration[key] = value

            return {"success": True, "error": None, "data": return_configuration, "timestamp": datetime.now().isoformat(), "description": "The contents of the Predbat apps.yaml configuration"}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving Predbat apps.yaml data: {str(e)}", "data": None}

    async def _execute_get_status(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the get_status tool"""

        try:
            debug_enable, _ = self.base.get_ha_config("debug_enable", None)
            read_only, _ = self.base.get_ha_config("set_read_only", None)
            predbat_mode, _ = self.base.get_ha_config("mode", None)
            num_cars, _ = self.base.get_ha_config("num_cars", None)
            last_updated = self.base.get_state_wrapper("predbat.status", attribute="last_updated", default=None)
            soc_percent = calc_percent_limit(self.base.soc_kw, self.base.soc_max)
            grid_power = self.base.grid_power
            battery_power = self.base.battery_power
            pv_power = self.base.pv_power
            load_power = self.base.load_power
            status_data = {
                "is_running": self.base.is_running(),
                "status": self.base.get_state_wrapper("predbat.status"),
                "current_soc": self.base.soc_kw,
                "soc_max": self.base.soc_max,
                "soc_percent": soc_percent,
                "reserve": self.base.reserve,
                "mode": predbat_mode,
                "num_cars": num_cars,
                "carbon_enable": self.base.carbon_enable,
                "iboost_enable": self.base.iboost_enable,
                "forecast_minutes": self.base.forecast_minutes,
                "debug_enable": debug_enable,
                "read_only": read_only,
                "last_updated": last_updated,
                "grid_power": grid_power,
                "battery_power": battery_power,
                "pv_power": pv_power,
                "load_power": load_power,
            }

            return {"success": True, "error": None, "data": status_data, "timestamp": datetime.now().isoformat()}

        except Exception as e:
            return {"success": False, "error": f"Error retrieving status: {str(e)}", "data": None}

    async def _execute_set_config(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the set_config tool"""
        try:
            entity_id = arguments.get("entity_id")
            value = arguments.get("value")

            if not entity_id or value is None:
                return {"success": False, "error": "Both 'entity_id' and 'value' must be provided", "data": None}

            # Update the configuration setting
            await self.base.ha_interface.set_state_external(entity_id, value)

            return {"success": True, "error": None, "data": {"entity_id": entity_id, "new_value": value}, "timestamp": datetime.now().isoformat(), "description": f"Configuration setting '{entity_id}' updated successfully"}

        except Exception as e:
            return {"success": False, "error": f"Error setting configuration: {str(e)}", "data": None}

    async def handle_mcp_request(self, request, mcp_server):
        """
        Handle HTTP requests implementing the Model Context Protocol over HTTP

        Args:
            request: Web request object
            mcp_server: MCP server instance

        Returns:
            JSON response following MCP/JSON-RPC 2.0 protocol
        """
        try:
            if request.method == "POST":
                # Handle MCP JSON-RPC requests
                try:
                    request_data = await request.json()
                except Exception:
                    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}

                # Extract JSON-RPC fields
                jsonrpc = request_data.get("jsonrpc", "2.0")
                request_id = request_data.get("id")
                method = request_data.get("method")
                params = request_data.get("params", {})

                # Route to appropriate handler
                if method == "initialize":
                    result = await self._handle_initialize(params)
                elif method == "tools/list":
                    result = await self._handle_tools_list(params)
                elif method == "tools/call":
                    result = await self._handle_tools_call(params)
                elif method == "notifications/initialized":
                    # This is a notification - return empty success response
                    # Some clients expect a response even for notifications over HTTP
                    return {"jsonrpc": jsonrpc, "id": request_id, "result": {}}
                else:
                    # Method not found
                    return {"jsonrpc": jsonrpc, "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}

                # Build success response
                return {"jsonrpc": jsonrpc, "id": request_id, "result": result}

            else:
                # For non-POST requests, return error
                return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": f"Invalid Request: {request.method} not supported, use POST"}}

        except Exception as e:
            return {"jsonrpc": "2.0", "id": request_id if "request_id" in locals() else None, "error": {"code": -32603, "message": f"Internal error: {str(e)}"}}

    async def _handle_initialize(self, params):
        """Handle MCP initialize request"""
        tools = await self._handle_tools_list(params)
        return {"protocolVersion": "2024-11-05", "capabilities": {"tools": tools["tools"]}, "serverInfo": {"name": "Predbat MCP Server", "version": "1.0.1"}}

    async def _handle_tools_list(self, params):
        """Handle MCP tools/list request"""
        return {
            "tools": [
                {"name": "get_plan", "description": "Get the current Predbat battery plan data including forecast, costs, and state information", "inputSchema": {"type": "object", "properties": {}, "required": []}},
                {"name": "get_status", "description": "Get the current Predbat system status and configuration", "inputSchema": {"type": "object", "properties": {}, "required": []}},
                {
                    "name": "get_apps",
                    "description": "Get predbat apps.yaml static configuration data",
                    "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "The configuration item name to filter on, as a Python regex (optional)"}}, "required": []},
                },
                {
                    "name": "get_config",
                    "description": "Get the current Predbat live configuration settings",
                    "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "The entity ID name to filter on, as a Python regex (optional)"}}, "required": []},
                },
                {
                    "name": "get_entities",
                    "description": "Get the current Predbat entities",
                    "inputSchema": {"type": "object", "properties": {"filter": {"type": "string", "description": "The configuration item name to filter on, as a Python regex (optional)"}}, "required": []},
                },
                {
                    "name": "set_config",
                    "description": "Set Predbat configuration setting",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"entity_id": {"type": "string", "description": "The entity ID of the configuration setting to update"}, "value": {"type": "string", "description": "The new value for the configuration setting"}},
                        "required": ["entity_id", "value"],
                    },
                },
                {
                    "name": "set_plan_override",
                    "description": "Override the current Predbat plan for a specific 30 minute period with a manual action",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "description": "The action to perform: demand, charge, export, freeze_charge, freeze_export, clear"},
                            "time": {"type": "string", "description": 'The time at which to perform the action, in "Day HH:MM" format (24-hour), covers one 30-minute period'},
                        },
                        "required": ["action", "time"],
                    },
                },
            ]
        }

    async def _handle_tools_call(self, params):
        """Handle MCP tools/call request"""
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "get_plan":
                result = await self._execute_get_plan(arguments)
            elif tool_name == "get_status":
                result = await self._execute_get_status(arguments)
            elif tool_name == "get_apps":
                result = await self._execute_get_apps(arguments)
            elif tool_name == "get_config":
                result = await self._execute_get_config(arguments)
            elif tool_name == "get_entities":
                result = await self._execute_get_entities(arguments)
            elif tool_name == "set_config":
                result = await self._execute_set_config(arguments)
            elif tool_name == "set_plan_override":
                result = await self._execute_set_plan_override(arguments)
            else:
                return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})}], "isError": True}

            # Return result in MCP format
            return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}

        except Exception as e:
            return {"content": [{"type": "text", "text": json.dumps({"success": False, "error": f"Tool execution failed: {str(e)}"})}], "isError": True}


def create_mcp_server(base, log_func=None):
    """
    Factory function to create an HTTP MCP server instance

    Args:
        base: Predbat base instance
        log_func: Optional logging function

    Returns:
        Configured HTTP MCP Server wrapper instance for web interface compatibility
    """
    return MCPServerWrapper(base, log_func)


async def main():
    """
    Main entry point for testing HTTP MCP server
    This is used when running the MCP server directly for testing
    """

    class MockBase:
        """Mock base for testing"""

        def __init__(self):
            self.raw_plan = {"test": "data", "forecast": [{"time": "10:00", "soc": 50, "cost": 0.15}, {"time": "11:00", "soc": 60, "cost": 0.12}], "totals": {"cost_today": 5.50, "profit_today": 2.30}}
            self.soc_kw = 50
            self.soc_max = 100
            self.reserve = 10
            self.mode = "Automatic"
            self.num_cars = 1
            self.carbon_enable = True
            self.iboost_enable = False
            self.forecast_minutes = 1440

        def is_running(self):
            return True

    base = MockBase()

    # Test the HTTP MCP server wrapper
    wrapper = create_mcp_server(base, print)
    await wrapper.start()

    print("HTTP MCP Server wrapper created and started successfully")

    await wrapper.stop()


if __name__ == "__main__":
    asyncio.run(main())

"""Kraken API component for EDF/E.ON tariff discovery and rate fetching.

Self-contained — discovers tariffs via Kraken GraphQL, constructs rate URLs
from provider base URL + tariff code, fetches rates from public REST API.
No stored URLs, no edge function callbacks.

Auth strategy: SaaS uses OAuthMixin (edge function refresh), OSS uses
KrakenAuthMixin (local API key / email+password → JWT).
"""

import aiohttp
import asyncio

from component_base import ComponentBase

# Auth strategy selection — priority: OAuthMixin > KrakenAuthMixin > no-op
try:
    from oauth_mixin import OAuthMixin

    _AUTH_BASE = OAuthMixin
except ImportError:
    try:
        from kraken_auth_mixin import KrakenAuthMixin

        _AUTH_BASE = KrakenAuthMixin
    except ImportError:

        class _NoAuth:
            def _init_oauth(self, *args, **kwargs):
                self.auth_method = None
                self.oauth_failed = True
                self.access_token = None
                self.token_expires_at = None

            def _init_kraken_auth(self, *args, **kwargs):
                self._init_oauth(*args, **kwargs)

            async def check_and_refresh_oauth_token(self):
                return False

            async def handle_oauth_401(self):
                return False

        _AUTH_BASE = _NoAuth


KRAKEN_BASE_URLS = {
    "edf": "https://api.edfgb-kraken.energy",
    "eon": "https://api.eonnext-kraken.energy",
}

# Auth error codes that trigger token refresh + retry
KRAKEN_AUTH_ERROR_CODES = ("KT-CT-1139", "KT-CT-1111", "KT-CT-1143")


class KrakenAPI(ComponentBase, _AUTH_BASE):
    """Kraken GraphQL component for EDF/E.ON tariff discovery and rate fetching."""

    def initialize(self, provider, account_id, key=None, email=None, password=None, auth_method="oauth", token_expires_at=None):
        """Initialise the Kraken API component with provider, account, and auth config."""
        self.provider = provider
        self.base_url = KRAKEN_BASE_URLS.get(provider)
        if not self.base_url:
            self.log(f"Warn: Kraken: Unknown provider '{provider}', expected 'edf' or 'eon'")
            self.base_url = KRAKEN_BASE_URLS["edf"]

        self.account_id = account_id
        self.current_tariff = None
        self.requests_total = 0
        self.failures_total = 0

        # Init auth — OAuthMixin or KrakenAuthMixin depending on import
        if hasattr(self, "_init_oauth") and _AUTH_BASE.__name__ == "OAuthMixin":
            self._init_oauth(auth_method, key, token_expires_at, "kraken")
        elif hasattr(self, "_init_kraken_auth"):
            self._init_kraken_auth(auth_method, key=key, email=email, password=password)

    async def async_graphql_query(self, query, request_context, _retry_count=0):
        """Execute a GraphQL query with auth and auto-retry on auth errors."""
        token_ok = await self.check_and_refresh_oauth_token()
        if not token_ok:
            self.log(f"Warn: Kraken: Auth not available for {request_context}")
            self.failures_total += 1
            return None

        token = self.access_token
        if not token:
            self.log(f"Warn: Kraken: No access token for {request_context}")
            self.failures_total += 1
            return None

        url = f"{self.base_url}/v1/graphql/"
        headers = {
            "Authorization": f"JWT {token}",
            "Content-Type": "application/json",
        }

        try:
            self.requests_total += 1
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json={"query": query}, headers=headers) as response:
                    if response.status != 200:
                        self.log(f"Warn: Kraken: GraphQL HTTP {response.status} for {request_context}")
                        self.failures_total += 1
                        return None
                    body = await response.json()

            # Check for auth errors — retry once after token refresh
            if body and "errors" in body and _retry_count == 0:
                for error in body.get("errors", []):
                    error_code = error.get("extensions", {}).get("errorCode", "")
                    if error_code in KRAKEN_AUTH_ERROR_CODES:
                        self.log(f"Kraken: Auth error {error_code}, refreshing token and retrying")
                        refreshed = await self.handle_oauth_401()
                        if refreshed:
                            return await self.async_graphql_query(query, request_context, _retry_count=1)
                        self.log(f"Warn: Kraken: Token refresh failed for {request_context}")
                        self.failures_total += 1
                        return None

            if body and "errors" in body:
                self.log(f"Warn: Kraken: GraphQL errors for {request_context}: {body['errors']}")
                self.failures_total += 1
                return None

            if body and "data" in body:
                return body["data"]

            return None

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Kraken: Network error for {request_context}: {e}")
            self.failures_total += 1
            return None

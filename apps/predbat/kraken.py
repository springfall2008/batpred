"""Kraken API component for EDF/E.ON tariff discovery and rate fetching.

Self-contained — discovers tariffs via Kraken GraphQL, constructs rate URLs
from provider base URL + tariff code, fetches rates from public REST API.
No stored URLs, no edge function callbacks.

Auth strategy: SaaS uses OAuthMixin (edge function refresh), OSS uses
KrakenAuthMixin (local API key / email+password → JWT).
"""

import aiohttp
import asyncio
from datetime import datetime

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


# Validated EDF/E.ON Kraken query — uses electricitySupplyPoints (NOT Octopus's
# electricityAgreements/meterPoint schema). Matches _shared/kraken-graphql.ts.
# No accountNumber arg needed — JWT scopes to authenticated account.
KRAKEN_ACCOUNT_QUERY = """{
  account {
    number
    properties {
      electricitySupplyPoints {
        mpan
        agreements {
          validFrom
          validTo
          tariff {
            ... on StandardTariff { tariffCode displayName productCode }
            ... on DayNightTariff { tariffCode displayName productCode }
            ... on ThreeRateTariff { tariffCode displayName productCode }
            ... on HalfHourlyTariff { tariffCode displayName productCode }
            ... on PrepayTariff { tariffCode displayName productCode }
          }
        }
      }
    }
  }
}"""

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

    async def async_find_tariffs(self):
        """Query account to discover current tariff. Returns tariff info if changed, None if same."""
        data = await self.async_graphql_query(KRAKEN_ACCOUNT_QUERY, "find-tariffs")
        if not data:
            return None

        account = data.get("account", {})
        properties = account.get("properties", [])

        # Find first property with electricity supply points (same as kraken-graphql.ts)
        for prop in properties:
            supply_points = prop.get("electricitySupplyPoints", [])
            for sp in supply_points:
                agreements = sp.get("agreements", [])

                # Find active agreement (validTo is None or in the future)
                for agr in agreements:
                    valid_to = agr.get("validTo")
                    if valid_to is not None:
                        try:
                            vt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                            if vt < datetime.now(vt.tzinfo):
                                continue
                        except (ValueError, AttributeError):
                            continue

                    tariff = agr.get("tariff", {})
                    tariff_code = tariff.get("tariffCode")
                    product_code = tariff.get("productCode")

                    if not tariff_code or not product_code:
                        continue

                    new_tariff = {"tariff_code": tariff_code, "product_code": product_code}

                    if self.current_tariff == new_tariff:
                        self.log(f"Kraken: Tariff unchanged — {tariff_code}")
                        return None

                    old = self.current_tariff
                    self.current_tariff = new_tariff
                    self.log(f"Kraken: Tariff {'discovered' if old is None else 'changed'} — {tariff_code} (product {product_code})")
                    return new_tariff

        self.log("Warn: Kraken: No active electricity agreement found")
        return None

    def build_rates_url(self, product_code, tariff_code):
        """Construct public REST rates URL from base URL + tariff info."""
        return f"{self.base_url}/v1/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    def build_standing_charge_url(self, product_code, tariff_code):
        """Construct public REST standing charge URL."""
        return f"{self.base_url}/v1/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"

    async def async_fetch_rates(self):
        """Fetch rates from public REST endpoint. No auth needed. Returns list of rate objects or None."""
        if not self.current_tariff:
            return None

        url = self.build_rates_url(
            self.current_tariff["product_code"],
            self.current_tariff["tariff_code"],
        )

        all_results = []
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while url:
                    async with session.get(url) as response:
                        if response.status != 200:
                            self.log(f"Warn: Kraken: Rates HTTP {response.status} for {url}")
                            self.failures_total += 1
                            return None
                        data = await response.json()

                    all_results.extend(data.get("results", []))
                    url = data.get("next")  # Pagination

            self.log(f"Kraken: Fetched {len(all_results)} rate periods")
            return all_results

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Kraken: Network error fetching rates: {e}")
            self.failures_total += 1
            return None

    def get_entity_name(self, root, suffix):
        """Construct entity name. Same pattern as OctopusAPI.get_entity_name."""
        entity_name = root + ".predbat_kraken_" + self.account_id.replace("-", "_") + "_" + suffix
        return entity_name.lower()

    def set_arg(self, key, value):
        """Set a config arg on the base PredBat instance."""
        self.base.args[key] = value

    async def run(self, seconds, first):
        """Component run method — called by ComponentBase.start() every 60s.

        Timing (mirrors OctopusAPI pattern):
        - First run + every 30 min: discover tariff via GraphQL
        - Every cycle: fetch rates from REST + publish entities
        - First run: wire into fetch.py via set_arg
        """
        now = datetime.now()
        count_minutes = now.minute + now.hour * 60

        # Tariff discovery — first run + every 30 minutes
        if first or (count_minutes % 30) == 0:
            tariff_change = await self.async_find_tariffs()

            if tariff_change:
                self.dashboard_item(
                    self.get_entity_name("sensor", "tariff_code"),
                    tariff_change["tariff_code"],
                    attributes={
                        "friendly_name": "Kraken Tariff Code",
                        "product_code": tariff_change["product_code"],
                        "icon": "mdi:lightning-bolt",
                    },
                    app="kraken",
                )

        # Fetch rates — every cycle (rates update throughout the day)
        if self.current_tariff:
            rates = await self.async_fetch_rates()
            if rates:
                self.dashboard_item(
                    self.get_entity_name("sensor", "import_rates"),
                    state=len(rates),
                    attributes={
                        "friendly_name": "Kraken Import Rates",
                        "rates": rates,
                        "tariff_code": self.current_tariff["tariff_code"],
                        "product_code": self.current_tariff["product_code"],
                        "icon": "mdi:currency-gbp",
                    },
                    app="kraken",
                )

        # Wire into fetch.py on first successful run (same as OctopusAPI.automatic_config)
        if first and self.current_tariff:
            self.set_arg("metric_octopus_import", self.get_entity_name("sensor", "import_rates"))
            self.set_arg("metric_standing_charge", self.get_entity_name("sensor", "import_standing"))

        # Publish account status
        status = "error" if self.oauth_failed else ("connected" if self.current_tariff else "discovering")
        self.dashboard_item(
            self.get_entity_name("sensor", "account_status"),
            state=status,
            attributes={
                "friendly_name": "Kraken Account Status",
                "provider": self.provider,
                "account_id": self.account_id,
                "icon": "mdi:account-check" if status == "connected" else "mdi:account-alert",
            },
            app="kraken",
        )

        return True

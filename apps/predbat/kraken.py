"""Kraken API component for EDF/E.ON tariff discovery and rate fetching.

Self-contained — discovers tariffs via Kraken GraphQL, constructs rate URLs
from provider base URL + tariff code, fetches rates from public REST API.

Auth strategy: SaaS uses OAuthMixin (edge function refresh), OSS uses
KrakenAuthMixin (local API key / email+password → JWT).

GraphQL schema notes (validated against live EDF/E.ON APIs):
  - EDF/E.ON use `electricityMeterPoints` (NOT `electricitySupplyPoints`)
  - `account(accountNumber: ...)` requires explicit account number
  - `viewer { accounts { number } }` discovers all account numbers
  - E.ON may split import/export into separate accounts matched by address
"""

import aiohttp
import asyncio
import types as _types
from datetime import datetime, timedelta, timezone

from component_base import ComponentBase

# Auth strategy selection — OAuthMixin for SaaS OAuth, KrakenAuthMixin for local auth.
# Both may ship in the same release package.  _KrakenAuthMixin is stored at module level
# so that initialize() can bind its methods to instances requiring local email/api_key auth
# even when OAuthMixin occupies the class-level _AUTH_BASE slot.
_KrakenAuthMixin = None
try:
    from kraken_auth_mixin import KrakenAuthMixin as _KrakenAuthMixin
except ImportError:
    pass

try:
    from oauth_mixin import OAuthMixin

    _AUTH_BASE = OAuthMixin
except ImportError:
    if _KrakenAuthMixin is not None:
        _AUTH_BASE = _KrakenAuthMixin
    else:

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


# EDF/E.ON Kraken GraphQL query — uses electricityMeterPoints (NOT Octopus's
# electricitySupplyPoints). Requires explicit accountNumber arg; the JWT does
# NOT scope to a single account. Address is queried for export tariff matching.
KRAKEN_ACCOUNT_QUERY = """{{
  account(accountNumber: "{account_number}") {{
    number
    properties {{
      address
      electricityMeterPoints {{
        mpan
        agreements {{
          validFrom
          validTo
          tariff {{
            ... on StandardTariff {{ tariffCode displayName productCode }}
            ... on DayNightTariff {{ tariffCode displayName productCode }}
            ... on ThreeRateTariff {{ tariffCode displayName productCode }}
            ... on HalfHourlyTariff {{ tariffCode displayName productCode }}
            ... on PrepayTariff {{ tariffCode displayName productCode }}
          }}
        }}
      }}
    }}
  }}
}}"""

# Viewer query to discover all account numbers under the authenticated user
KRAKEN_VIEWER_QUERY = """{ viewer { accounts { number } } }"""

# GraphQL applicableRates query — fallback when REST product endpoint returns 404
# (product code removed/replaced while customer is still on the tariff).
# Returns value (pence/kWh inc VAT), validFrom, validTo for the requested window.
KRAKEN_APPLICABLE_RATES_QUERY = """{{
  applicableRates(
    accountNumber: "{account_number}"
    mpxn: "{mpan}"
    startAt: "{start_at}"
    endAt: "{end_at}"
  ) {{
    value
    validFrom
    validTo
  }}
}}"""

KRAKEN_BASE_URLS = {
    "edf": "https://api.edfgb-kraken.energy",
    "eon": "https://api.eonnext-kraken.energy",
}

# Auth error codes that trigger token refresh + retry
KRAKEN_AUTH_ERROR_CODES = ("KT-CT-1139", "KT-CT-1111", "KT-CT-1143")


class KrakenAPI(ComponentBase, _AUTH_BASE):
    """Kraken GraphQL component for EDF/E.ON tariff discovery and rate fetching."""

    def initialize(
        self,
        provider,
        account_id,
        key=None,
        email=None,
        password=None,
        auth_method="oauth",
        token_expires_at=None,
        token_hash=None,
        mpan=None,
        export_account_id=None,
        export_mpan=None,
        base_url=None,
    ):
        """Initialise the Kraken API component with provider, account, and auth config."""
        self.provider = provider
        self.base_url = base_url or KRAKEN_BASE_URLS.get(provider)
        if not self.base_url:
            self.log(f"Warn: Kraken: Unknown provider '{provider}', expected 'edf' or 'eon'")
            self.base_url = KRAKEN_BASE_URLS["edf"]

        self.account_id = account_id
        self.configured_mpan = mpan  # From SaaS config — preferred MPAN to match
        self.import_mpan = None  # Set after first successful tariff discovery
        self.current_tariff = None
        self.export_tariff = None  # Export tariff (discovered dynamically)
        self.wired = False
        self.export_wired = False
        self.requests_total = 0
        self.failures_total = 0
        self.oauth_failed = False

        # Export account/MPAN from SaaS config (matched by address at onboarding time).
        # Tariff is always discovered dynamically via GraphQL so changes are detected.
        self.export_account_id = export_account_id
        self.export_mpan = export_mpan
        if export_account_id or export_mpan:
            self.log(f"Kraken: Export account configured — {export_account_id or 'same account'} MPAN {export_mpan or 'auto'}")

        # Init auth — prefer KrakenAuthMixin for local email/password or API key auth,
        # use OAuthMixin only for SaaS OAuth mode (where tokens come from edge functions).
        # NOTE: both mixins may ship in the same release, so we check the module-level
        # _KrakenAuthMixin reference rather than hasattr() which only sees the class hierarchy.
        use_local_auth = auth_method in ("email", "api_key") and _KrakenAuthMixin is not None
        if use_local_auth:
            _KrakenAuthMixin._init_kraken_auth(self, auth_method, key=key, email=email, password=password)
            # Bind KrakenAuthMixin token methods directly to this instance so they take
            # priority over OAuthMixin when both are present in the class hierarchy.
            self.check_and_refresh_oauth_token = _types.MethodType(_KrakenAuthMixin.check_and_refresh_oauth_token, self)
            self.handle_oauth_401 = _types.MethodType(_KrakenAuthMixin.handle_oauth_401, self)
            self._kraken_token_request = _types.MethodType(_KrakenAuthMixin._kraken_token_request, self)
        elif hasattr(self, "_init_oauth"):
            self._init_oauth(auth_method, key, token_expires_at, "kraken")
            self.token_hash = token_hash or ""
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
            "Authorization": token,
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

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"Warn: Kraken: Network error for {request_context}: {e}")
            self.failures_total += 1
            return None

    def _find_active_tariff(self, meter_points, preferred_mpan=None, is_export=False):
        """Find the current active tariff from a list of meter points.

        Args:
            meter_points: List of electricityMeterPoints from GraphQL
            preferred_mpan: If set, prefer this MPAN (from SaaS config)
            is_export: If True, only match EXPORT tariff codes; if False, skip them

        Returns:
            dict with tariff_code, product_code, mpan or None
        """
        now = datetime.now(timezone.utc)

        # Sort meter points to prefer configured MPAN
        sorted_mps = sorted(meter_points, key=lambda mp: mp.get("mpan") != preferred_mpan)

        for mp in sorted_mps:
            mpan = mp.get("mpan", "")
            agreements = mp.get("agreements", [])

            for agr in agreements:
                # Check validity window
                valid_from = agr.get("validFrom")
                if valid_from is not None:
                    try:
                        vf = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
                        if now < vf:
                            continue
                    except (ValueError, AttributeError):
                        pass

                valid_to = agr.get("validTo")
                if valid_to is not None:
                    try:
                        vt = datetime.fromisoformat(valid_to.replace("Z", "+00:00"))
                        if vt < now:
                            continue
                    except (ValueError, AttributeError):
                        pass

                tariff = agr.get("tariff", {})
                tariff_code = tariff.get("tariffCode")
                product_code = tariff.get("productCode")

                if not tariff_code or not product_code:
                    continue

                tariff_is_export = "EXPORT" in tariff_code.upper()
                if is_export != tariff_is_export:
                    continue

                return {
                    "tariff_code": tariff_code,
                    "product_code": product_code,
                    "mpan": mpan,
                }

        return None

    async def _discover_export_tariff(self, import_meter_points, import_address):
        """Discover or rediscover export tariff.

        Three strategies, tried in order:
        1. If export_account_id is configured and differs from import, query that account
        2. Check the import account's meter points for an export tariff
        3. Fall back to address-based matching across all accounts (viewer query)
        """
        # Strategy 1: Configured export account (E.ON split accounts)
        if self.export_account_id and self.export_account_id != self.account_id:
            query = KRAKEN_ACCOUNT_QUERY.format(account_number=self.export_account_id)
            data = await self.async_graphql_query(query, f"export-tariff-{self.export_account_id}")
            if data:
                export_mps = []
                for prop in data.get("account", {}).get("properties", []):
                    export_mps.extend(prop.get("electricityMeterPoints", []))
                export_result = self._find_active_tariff(export_mps, preferred_mpan=self.export_mpan, is_export=True)
                if export_result:
                    new_export = {"tariff_code": export_result["tariff_code"], "product_code": export_result["product_code"]}
                    if self.export_tariff != new_export:
                        old = self.export_tariff
                        self.export_tariff = new_export
                        self.export_mpan = export_result["mpan"]
                        self.log(f"Kraken: Export tariff {'discovered' if old is None else 'changed'} on account {self.export_account_id} — {export_result['tariff_code']}")
                    return
                # Query succeeded but no active export tariff found — clear stale
                if self.export_tariff:
                    self.log(f"Warn: Kraken: Export tariff no longer active on account {self.export_account_id}")
                    self.export_tariff = None
            # Network failure on configured export account — don't fall through to Strategy 2
            # (would incorrectly match an export on the import account)
            return

        # Strategy 2: Check import account's meter points (only when no dedicated export account)
        export_result = self._find_active_tariff(import_meter_points, preferred_mpan=self.export_mpan, is_export=True)
        if export_result:
            new_export = {"tariff_code": export_result["tariff_code"], "product_code": export_result["product_code"]}
            if self.export_tariff != new_export:
                old = self.export_tariff
                self.export_tariff = new_export
                self.export_mpan = export_result["mpan"]
                self.log(f"Kraken: Export tariff {'discovered' if old is None else 'changed'} on same account — {export_result['tariff_code']}")
            return

        # Strategy 3: Address-based matching (only on first discovery — no existing export)
        if not self.export_tariff and import_address:
            all_accounts = await self.async_discover_all_accounts()
            for acct in all_accounts:
                if acct["account_number"] == self.account_id:
                    continue
                if acct["address"] == import_address and acct.get("export_tariff"):
                    et = acct["export_tariff"]
                    self.export_tariff = {"tariff_code": et["tariff_code"], "product_code": et["product_code"]}
                    self.export_mpan = et["mpan"]
                    self.export_account_id = acct["account_number"]
                    self.log(f"Kraken: Export tariff discovered on account {acct['account_number']} (address match) — {et['tariff_code']}")
                    return

        # No strategy found an export tariff — clear stale if previously set
        if self.export_tariff:
            self.log("Warn: Kraken: Export tariff no longer discoverable, clearing")
            self.export_tariff = None

    async def async_discover_all_accounts(self):
        """Discover all account numbers via viewer query, then find import+export tariffs.

        Returns list of dicts with account_number, address, import_tariff, export_tariff.
        """
        data = await self.async_graphql_query(KRAKEN_VIEWER_QUERY, "discover-accounts")
        if not data:
            return []

        account_numbers = data.get("viewer", {}).get("accounts", [])
        if not account_numbers:
            self.log("Warn: Kraken: No accounts found for this user")
            return []

        results = []
        for acct in account_numbers:
            acct_num = acct.get("number")
            if not acct_num:
                continue

            query = KRAKEN_ACCOUNT_QUERY.format(account_number=acct_num)
            acct_data = await self.async_graphql_query(query, f"account-{acct_num}")
            if not acct_data:
                continue

            account = acct_data.get("account", {})
            properties = account.get("properties", [])

            for prop in properties:
                meter_points = prop.get("electricityMeterPoints", [])
                if not meter_points:
                    continue

                address_raw = prop.get("address", "")
                if not isinstance(address_raw, str):
                    address_raw = str(address_raw)

                import_tariff = self._find_active_tariff(meter_points, is_export=False)
                export_tariff = self._find_active_tariff(meter_points, is_export=True)

                if import_tariff or export_tariff:
                    results.append(
                        {
                            "account_number": account.get("number", acct_num),
                            "address": address_raw.lower().strip(),
                            "import_tariff": import_tariff,
                            "export_tariff": export_tariff,
                        }
                    )

        return results

    async def async_find_tariffs(self):
        """Query configured account to discover current import tariff.

        Also discovers export tariff dynamically (E.ON splits import/export
        into separate accounts — may need viewer query).

        Returns import tariff info if changed, None if same.
        """
        # Query the configured account directly
        query = KRAKEN_ACCOUNT_QUERY.format(account_number=self.account_id)
        data = await self.async_graphql_query(query, "find-tariffs")
        if not data:
            return None

        account = data.get("account", {})
        properties = account.get("properties", [])

        # Collect all meter points across properties
        all_meter_points = []
        my_address = None
        for prop in properties:
            mps = prop.get("electricityMeterPoints", [])
            if mps:
                all_meter_points.extend(mps)
                if my_address is None:
                    addr = prop.get("address", "")
                    if isinstance(addr, str):
                        my_address = addr.lower().strip()

        if not all_meter_points:
            self.log("Warn: Kraken: No electricity meter points found")
            return None

        # Find import tariff (prefer configured MPAN)
        import_result = self._find_active_tariff(all_meter_points, preferred_mpan=self.configured_mpan, is_export=False)
        if not import_result:
            self.log("Warn: Kraken: No active import tariff found")
            return None

        new_tariff = {"tariff_code": import_result["tariff_code"], "product_code": import_result["product_code"]}

        # Store MPAN for GraphQL fallback in async_fetch_rates_graphql()
        self.import_mpan = import_result["mpan"]

        # Discover export tariff — always re-discover to detect tariff changes
        await self._discover_export_tariff(all_meter_points, my_address)

        if self.current_tariff == new_tariff:
            self.log(f"Kraken: Tariff unchanged — {new_tariff['tariff_code']}")
            return None

        old = self.current_tariff
        self.current_tariff = new_tariff
        self.log(f"Kraken: Tariff {'discovered' if old is None else 'changed'} — {new_tariff['tariff_code']} (product {new_tariff['product_code']})")
        return new_tariff

    def build_rates_url(self, product_code, tariff_code):
        """Construct public REST rates URL from base URL + tariff info."""
        return f"{self.base_url}/v1/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/"

    def build_standing_charge_url(self, product_code, tariff_code):
        """Construct public REST standing charge URL."""
        return f"{self.base_url}/v1/products/{product_code}/electricity-tariffs/{tariff_code}/standing-charges/"

    @staticmethod
    def _normalize_rate_timestamps(results):
        """Normalize Kraken rate results so downstream minute_data() can parse them.

        Kraken's REST API returns null valid_from/valid_to for flat-rate tariffs
        (e.g. fixed export rates). The Octopus API always provides real timestamps,
        so minute_data() expects valid_from to be a parsable ISO timestamp.

        Normalization rules:
        - valid_from=null: set to the earliest valid_to across all results, or
          48h in the past if no valid_to exists (covers the full forecast window).
        - valid_to=null: already handled by minute_data() (extends to end of forecast).

        Note: We have only observed the single-entry flat-rate case from Kraken
        (1 result, both timestamps null). If Kraken later returns multiple entries
        where some have null valid_from (e.g. an open-ended latest rate alongside
        historical rates with real timestamps), this logic should still work -
        but the actual API response shape for that scenario is unverified.
        """
        if not results:
            return results

        has_null_from = any(r.get("valid_from") is None for r in results)
        if not has_null_from:
            return results

        # Find the earliest valid_to to use as a reference point for null valid_from
        earliest_to = None
        for r in results:
            vt = r.get("valid_to")
            if vt:
                if earliest_to is None or vt < earliest_to:
                    earliest_to = vt

        # Default: 48h in the past covers history + forecast window
        fallback_from = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for r in results:
            if r.get("valid_from") is None:
                r["valid_from"] = earliest_to or fallback_from

        return results

    async def async_fetch_rates_graphql(self, mpan):
        """Fetch import rates via GraphQL applicableRates — fallback when REST returns non-200.

        Used when the product code has been removed from the REST API (e.g. product replaced
        mid-agreement). The applicableRates query returns the rates currently applicable to
        the customer regardless of product lifecycle.

        Args:
            mpan: The import MPAN (meter point access number) for the account.

        Returns list of rate dicts with value_inc_vat, value_exc_vat, valid_from, valid_to, or None.
        """
        now = datetime.now(timezone.utc)
        midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Mirror the window used by fetch_octopus_rates → minute_data(forecast_days + 1, midnight_utc).
        # Start one day before midnight so any rate period that began earlier today is included.
        # End at midnight + (forecast_days + 1) to cover the full planning horizon.
        forecast_hours = self.get_arg("forecast_hours", 48)
        forecast_days = int((forecast_hours + 23) / 24)
        start_at = (midnight_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_at = (midnight_utc + timedelta(days=forecast_days + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = KRAKEN_APPLICABLE_RATES_QUERY.format(
            account_number=self.account_id,
            mpan=mpan,
            start_at=start_at,
            end_at=end_at,
        )
        data = await self.async_graphql_query(query, "applicable-rates-graphql")
        if not data:
            return None

        raw_rates = data.get("applicableRates", [])
        if not raw_rates:
            self.log("Warn: Kraken: applicableRates GraphQL returned no rate periods")
            return None

        results = []
        for r in raw_rates:
            value = r.get("value")
            if value is None:
                continue
            value_inc_vat = float(value)
            value_exc_vat = round(value_inc_vat / 1.05, 4)
            results.append(
                {
                    "value_inc_vat": value_inc_vat,
                    "value_exc_vat": value_exc_vat,
                    "valid_from": r.get("validFrom"),
                    "valid_to": r.get("validTo"),
                }
            )

        if not results:
            return None

        results = self._normalize_rate_timestamps(results)
        self.log(f"Kraken: Fetched {len(results)} rate periods via GraphQL applicableRates for MPAN {mpan}")
        return results

    async def async_fetch_rates(self, tariff=None):
        """Fetch rates from public REST endpoint. No auth needed. Returns list of rate objects or None.

        Falls back to GraphQL applicableRates if the REST endpoint returns a non-200 status
        (e.g. 404 when the product code has been removed from the API) and self.import_mpan
        is known. Only applied to import tariff fetches — export rates have no GraphQL fallback.
        """
        tariff = tariff or self.current_tariff
        if not tariff:
            return None

        is_import = tariff == self.current_tariff
        url = self.build_rates_url(tariff["product_code"], tariff["tariff_code"])

        all_results = []
        pages = 0
        http_error_status = None
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while url and pages < 3:
                    async with session.get(url) as response:
                        if response.status != 200:
                            self.log(f"Warn: Kraken: Rates HTTP {response.status} for {url}")
                            self.failures_total += 1
                            http_error_status = response.status
                            break
                        data = await response.json()

                    all_results.extend(data.get("results", []))
                    url = data.get("next")  # Pagination
                    pages += 1

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Kraken: Network error fetching rates: {e}")
            self.failures_total += 1
            return None

        if http_error_status is not None:
            # Only fall back to GraphQL for permanent "product not found" responses (404/410).
            # Transient errors (429, 500, 503, …) should surface as failures, not trigger
            # an extra GraphQL request that would mask the outage.
            if http_error_status in (404, 410) and is_import and self.import_mpan:
                self.log(f"Kraken: REST rates returned HTTP {http_error_status}, falling back to GraphQL applicableRates for MPAN {self.import_mpan}")
                return await self.async_fetch_rates_graphql(self.import_mpan)
            return None

        if url:
            self.log(f"Warn: Kraken: Rate pagination capped at {pages} pages, more data available")

        all_results = self._normalize_rate_timestamps(all_results)
        self.log(f"Kraken: Fetched {len(all_results)} rate periods for {tariff['tariff_code']}")
        return all_results

    def get_entity_name(self, root, suffix):
        """Construct entity name. Same pattern as OctopusAPI.get_entity_name."""
        entity_name = root + "." + self.prefix + "_kraken_" + self.account_id.replace("-", "_") + "_" + suffix
        return entity_name.lower()

    async def async_fetch_standing_charges(self, tariff=None):
        """Fetch standing charges from public REST endpoint. No auth needed."""
        tariff = tariff or self.current_tariff
        if not tariff:
            return None

        url = self.build_standing_charge_url(tariff["product_code"], tariff["tariff_code"])

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        self.log(f"Warn: Kraken: Standing charges HTTP {response.status}")
                        self.failures_total += 1
                        return None
                    data = await response.json()

            results = data.get("results", [])
            if results:
                # API returns pence/day; fetch.py multiplies by 100 expecting pounds/day
                value = results[0].get("value_inc_vat")
                return value / 100.0 if value is not None else None
            return None

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Kraken: Network error fetching standing charges: {e}")
            self.failures_total += 1
            return None

    async def run(self, seconds, first):
        """Component run method — called by ComponentBase.start() every 60s.

        Timing (mirrors OctopusAPI pattern):
        - First run + every 30 min: discover tariff via GraphQL
        - First run + every 10 min: fetch rates + standing charges from REST
        - First run: wire into fetch.py via set_arg
        """
        count_minutes = seconds // 60
        had_success = False

        # Tariff discovery — first run + every 30 minutes
        if first or (count_minutes % 30) == 0:
            tariff_change = await self.async_find_tariffs()

            if tariff_change or self.current_tariff:
                had_success = True
                self.dashboard_item(
                    self.get_entity_name("sensor", "tariff_code"),
                    self.current_tariff["tariff_code"],
                    attributes={
                        "friendly_name": "Kraken Tariff Code",
                        "product_code": self.current_tariff["product_code"],
                        "icon": "mdi:lightning-bolt",
                    },
                    app="kraken",
                )

        # Fetch import rates + standing charges — first run + every 10 minutes
        if self.current_tariff and (first or (count_minutes % 10) == 0):
            rates = await self.async_fetch_rates()
            if rates:
                had_success = True
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

            standing_charge = await self.async_fetch_standing_charges()
            if standing_charge is not None:
                had_success = True
                self.dashboard_item(
                    self.get_entity_name("sensor", "import_standing"),
                    state=standing_charge,
                    attributes={
                        "friendly_name": "Kraken Standing Charge",
                        "unit_of_measurement": "£/day",
                        "tariff_code": self.current_tariff["tariff_code"],
                        "icon": "mdi:currency-gbp",
                    },
                    app="kraken",
                )

            # Fetch export rates if export tariff is known
            if self.export_tariff:
                export_rates = await self.async_fetch_rates(tariff=self.export_tariff)
                if export_rates:
                    had_success = True
                    self.dashboard_item(
                        self.get_entity_name("sensor", "export_rates"),
                        state=len(export_rates),
                        attributes={
                            "friendly_name": "Kraken Export Rates",
                            "rates": export_rates,
                            "tariff_code": self.export_tariff["tariff_code"],
                            "product_code": self.export_tariff["product_code"],
                            "icon": "mdi:currency-gbp",
                        },
                        app="kraken",
                    )

        # Wire import into fetch.py once tariff is discovered (retries until successful)
        if not self.wired and self.current_tariff:
            self.set_arg("metric_octopus_import", self.get_entity_name("sensor", "import_rates"))
            self.set_arg("metric_standing_charge", self.get_entity_name("sensor", "import_standing"))
            self.wired = True

        # Wire export into fetch.py once export tariff is discovered
        if not self.export_wired and self.export_tariff:
            self.set_arg("metric_octopus_export", self.get_entity_name("sensor", "export_rates"))
            self.export_wired = True

        # Publish account status
        status = "error" if self.oauth_failed else ("connected" if self.current_tariff else "discovering")
        self.dashboard_item(
            self.get_entity_name("sensor", "account_status"),
            state=status,
            attributes={
                "friendly_name": "Kraken Account Status",
                "provider": self.provider,
                "account_id": self.account_id,
                "has_export": self.export_tariff is not None,
                "icon": "mdi:account-check" if status == "connected" else "mdi:account-alert",
            },
            app="kraken",
        )

        # Update liveness timestamp if we got data OR tariff is known (prevents
        # spurious liveness failures during transient rate-fetch outages)
        if had_success or self.current_tariff:
            self.update_success_timestamp()

        if first and (self.oauth_failed or not self.current_tariff):
            return False

        return True

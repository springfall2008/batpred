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
import json
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
#
# `direction` is the authoritative IMPORT/EXPORT signal on the meter point (this is
# what the reference EDF HA integration keys off — validated against live EDF). We
# prefer it over the old "EXPORT in tariffCode" heuristic, which silently misses SEG
# and other export tariff codes that don't contain the literal string EXPORT.
#
# The tariff is queried via the `... on TariffType` interface fragment (all concrete
# tariff types implement it) rather than an enumerated list of concrete fragments —
# so an export/SEG tariff whose type wasn't in the old list is no longer dropped.
KRAKEN_ACCOUNT_QUERY = """{{
  account(accountNumber: "{account_number}") {{
    number
    properties {{
      address
      electricityMeterPoints {{
        mpan
        direction
        agreements {{
          validFrom
          validTo
          tariff {{
            ... on TariffType {{ tariffCode displayName productCode }}
          }}
        }}
      }}
    }}
  }}
}}"""

# Viewer query to discover all account numbers under the authenticated user
KRAKEN_VIEWER_QUERY = """{ viewer { accounts { number } } }"""

# GraphQL applicableRates query — fallback when REST product endpoint returns 404
# (product code removed/replaced while customer is still on the tariff, or TOU tariff
# with no /standard-unit-rates/ REST endpoint e.g. E-TOU-* tariffs on E.ON Next).
# Returns value (pence/kWh inc VAT), validFrom, validTo for the requested window.
# applicableRates is a Relay-style connection (ApplicableRateConnectionTypeConnection),
# so the rate fields live under edges { node { ... } }, not directly on the field. The
# connection is mandatory-paginated (KT-CT-1201) — a `first` value is required, and `after`
# carries the cursor for subsequent pages. `{after}` is a literal (null or a quoted cursor).
KRAKEN_APPLICABLE_RATES_QUERY = """{{
  applicableRates(
    accountNumber: "{account_number}"
    mpxn: "{mpan}"
    startAt: "{start_at}"
    endAt: "{end_at}"
    first: {page_size}
    after: {after}
  ) {{
    edges {{
      node {{
        value
        validFrom
        validTo
      }}
    }}
    pageInfo {{
      hasNextPage
      endCursor
    }}
  }}
}}"""

# GraphQL applicableStandingCharges query — fallback when REST /standing-charges/ returns 404
# (same scenarios as KRAKEN_APPLICABLE_RATES_QUERY above — product removed from REST API,
# or TOU tariffs whose /standing-charges/ endpoint is unavailable on the provider API).
# Returns value (pence/day inc VAT) for the requested window. Like applicableRates this is a
# Relay connection, so the fields live under edges { node { ... } }.
KRAKEN_STANDING_CHARGES_QUERY = """{{
  applicableStandingCharges(
    accountNumber: "{account_number}"
    mpxn: "{mpan}"
    startAt: "{start_at}"
    endAt: "{end_at}"
    first: 50
  ) {{
    edges {{
      node {{
        value
        validFrom
        validTo
      }}
    }}
  }}
}}"""

# GraphQL type introspection — used by the CLI --introspect-type diagnostic to discover the
# exact field names on a schema type (e.g. the applicableRates connection node) on a live API.
KRAKEN_INTROSPECT_TYPE_QUERY = """{{
  __type(name: "{type_name}") {{
    name
    kind
    fields {{
      name
      type {{ name kind ofType {{ name kind }} }}
    }}
  }}
}}"""

# SmartFlex device discovery — a connected EV / charge point the customer enrolled for provider-
# managed smart charging (Octopus Intelligent Go, E.ON NextDrive, EDF GoElectric SmartFlex, …).
# Schema matches the reference EDF/E.ON HA integration. Also used by the CLI --dispatches check.
KRAKEN_DEVICES_QUERY = """{{
  devices(accountNumber: "{account_number}") {{
    id
    provider
    deviceType
    status {{ current }}
    __typename
    ... on SmartFlexVehicle {{ make model }}
    ... on SmartFlexChargePoint {{ make model }}
  }}
}}"""

# Planned + completed SmartFlex dispatches for one device — the provider-scheduled cheap-charge
# windows Predbat consumes (via octopus_intelligent_slot) to extend cheap import beyond the fixed
# tariff window, exactly like Octopus Intelligent Go.
KRAKEN_DISPATCHES_QUERY = """{{
  devices(accountNumber: "{account_number}", deviceId: "{device_id}") {{
    id
    status {{ currentState }}
  }}
  flexPlannedDispatches(deviceId: "{device_id}") {{
    start
    end
    type
    energyAddedKwh
  }}
  completedDispatches(accountNumber: "{account_number}") {{
    start
    end
    delta
    meta {{ source location }}
  }}
}}"""

KRAKEN_BASE_URLS = {
    "edf": "https://api.edfgb-kraken.energy",
    "eon": "https://api.eonnext-kraken.energy",
}

# Auth error codes that trigger token refresh + retry
KRAKEN_AUTH_ERROR_CODES = ("KT-CT-1139", "KT-CT-1111", "KT-CT-1143")

# applicableRates connection pagination — request this many nodes per page, cap total pages
# so a runaway cursor can't loop forever. 100 * 20 = 2000 half-hourly periods (~41 days) is far
# more than the ~4-day planning window ever needs.
KRAKEN_RATES_PAGE_SIZE = 100
KRAKEN_RATES_MAX_PAGES = 20

# How stale cached data may get before run() re-queries the API (minutes). Tariff/account data
# changes rarely so it is cached for hours; rates are published roughly daily so a 30-minute
# refresh is ample. Cached data is restored on restart, so these also bound post-restart re-fetch.
KRAKEN_TARIFF_REFRESH_MINUTES = 120
KRAKEN_RATES_REFRESH_MINUTES = 30

# How often to re-fetch SmartFlex intelligent dispatches (minutes). Dispatches change more often
# than rates as the provider re-plans smart charging, but not minute-to-minute. Smart devices
# themselves are only re-discovered on the (slower) tariff cycle, so accounts with no EV enrolment
# never pay the per-device dispatch query.
KRAKEN_DISPATCH_REFRESH_MINUTES = 2
# Drop completed dispatches older than this many days when merging history.
KRAKEN_DISPATCH_HISTORY_DAYS = 5


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
        self.export_rates_available = False  # True once export rates are actually fetched (not just tariff discovered)
        self.requests_total = 0
        self.failures_total = 0
        self.oauth_failed = False

        # Last-fetched data, cached to storage so it survives a restart. Rates are the actual
        # rate-period lists last published to the sensors; the *_fetched_at timestamps drive the
        # age-based refresh in run() so a fast restart does not re-query the API needlessly.
        self.import_rates = None
        self.export_rates = None
        self.import_standing_charge = None
        self.tariff_fetched_at = None  # naive datetime of last successful tariff discovery
        self.rates_fetched_at = None  # naive datetime of last successful import-rate fetch

        # SmartFlex intelligent dispatches (provider-managed smart charging). intelligent_devices
        # maps device_id -> device metadata + planned/completed dispatch lists. Empty for the common
        # case of an account with no connected EV/charger. Cached to storage so it survives restart.
        self.intelligent_devices = {}
        self.dispatch_fetched_at = None

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
                        # Include the response body — Kraken returns GraphQL validation errors
                        # (unknown field / bad argument) as a 400 with the detail in the body.
                        try:
                            err_body = (await response.text())[:500]
                        except (aiohttp.ClientError, UnicodeDecodeError):
                            err_body = "<unreadable>"
                        self.log(f"Warn: Kraken: GraphQL HTTP {response.status} for {request_context}: {err_body}")
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

            # `direction` (IMPORT/EXPORT) is the authoritative signal on the meter point.
            # Fall back to the tariff-code substring only when the API omits it (older
            # responses / mocks), since some export tariff codes don't contain "EXPORT".
            direction = mp.get("direction")
            mp_is_export = None
            if isinstance(direction, str) and direction.strip():
                mp_is_export = direction.strip().upper() == "EXPORT"

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

                # Prefer the meter point's direction; fall back to the tariff-code substring.
                tariff_is_export = mp_is_export if mp_is_export is not None else ("EXPORT" in tariff_code.upper())
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
    def _connection_nodes(field):
        """Extract node dicts from a Relay-style GraphQL connection.

        Kraken's applicableRates / applicableStandingCharges fields are connections shaped as
        {"edges": [{"node": {...}}, ...]}. Returns the list of node dicts. Tolerates the field
        being None, already a plain list (older/mocked shape), or missing edges — returns [].
        """
        if not field:
            return []
        if isinstance(field, list):
            return field
        edges = field.get("edges") if isinstance(field, dict) else None
        if not edges:
            return []
        return [edge.get("node", {}) for edge in edges if edge and edge.get("node")]

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

    async def async_fetch_rates_graphql(self, mpan, account_id=None):
        """Fetch rates via GraphQL applicableRates — fallback when REST returns non-200.

        Used when the product code has been removed from the REST API (e.g. product replaced
        mid-agreement) or the tariff has no /standard-unit-rates/ REST endpoint. The
        applicableRates query returns the rates currently applicable to the customer
        regardless of product lifecycle. Works for both import and export MPANs.

        Args:
            mpan: The MPAN (meter point access number) to query rates for.
            account_id: Account number that owns the MPAN. Defaults to the configured
                import account; pass the export account for E.ON split import/export accounts.

        Returns list of rate dicts with value_inc_vat, value_exc_vat, valid_from, valid_to, or None.
        """
        account_id = account_id or self.account_id
        now = datetime.now(timezone.utc)
        midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Mirror the window used by fetch_octopus_rates → minute_data(forecast_days + 1, midnight_utc).
        # Start one day before midnight so any rate period that began earlier today is included.
        # End at midnight + (forecast_days + 1) to cover the full planning horizon.
        forecast_hours = self.get_arg("forecast_hours", 48)
        forecast_days = int((forecast_hours + 23) / 24)
        start_at = (midnight_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_at = (midnight_utc + timedelta(days=forecast_days + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # applicableRates is a mandatory-paginated connection — walk pages via the cursor until
        # hasNextPage is false (or the page cap is hit), accumulating every node.
        raw_rates = []
        after = "null"  # GraphQL literal for "from the start"
        page = 0
        while page < KRAKEN_RATES_MAX_PAGES:
            query = KRAKEN_APPLICABLE_RATES_QUERY.format(
                account_number=account_id,
                mpan=mpan,
                start_at=start_at,
                end_at=end_at,
                page_size=KRAKEN_RATES_PAGE_SIZE,
                after=after,
            )
            data = await self.async_graphql_query(query, "applicable-rates-graphql")
            if not data:
                # Network/auth failure mid-pagination: return what we have if any, else None.
                return self._finalize_graphql_rates(raw_rates, mpan) if raw_rates else None

            connection = data.get("applicableRates") or {}
            raw_rates.extend(self._connection_nodes(connection))

            # pageInfo is only present on the real connection shape (a dict); a plain-list
            # response (older/mocked) has no further pages.
            page_info = connection.get("pageInfo", {}) if isinstance(connection, dict) else {}
            if not page_info.get("hasNextPage") or not page_info.get("endCursor"):
                break
            after = f'"{page_info["endCursor"]}"'
            page += 1
        else:
            self.log(f"Warn: Kraken: applicableRates pagination capped at {KRAKEN_RATES_MAX_PAGES} pages, more data may exist")

        if not raw_rates:
            self.log("Warn: Kraken: applicableRates GraphQL returned no rate periods")
            return None

        return self._finalize_graphql_rates(raw_rates, mpan)

    def _finalize_graphql_rates(self, raw_rates, mpan):
        """Convert applicableRates connection nodes into rate dicts, or None if none are valid."""
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

    async def _build_rest_auth(self):
        """Build auth for the REST product endpoints.

        Public products (most import tariffs) need no auth, but private products — notably
        EDF SEG / export tariffs — return 404 unless the request is authenticated. Mirrors the
        reference EDF integration: HTTP Basic auth with the API key when one is available,
        otherwise a JWT bearer header built from the current (refreshed) access token.

        Returns a tuple of (aiohttp.BasicAuth or None, headers dict).
        """
        api_key = getattr(self, "_api_key", None)
        if api_key:
            return aiohttp.BasicAuth(api_key, ""), {}

        # OAuth / email (JWT) — ensure the token is fresh, then send it as a bearer header.
        await self.check_and_refresh_oauth_token()
        token = getattr(self, "access_token", None)
        if token:
            return None, {"Authorization": f"JWT {token}"}
        return None, {}

    async def _fetch_rates_rest(self, url, authenticate=False):
        """Fetch (paginated) rate periods from a REST URL.

        Returns a tuple of (results_list_or_None, error_status). error_status is None on
        success, an HTTP status code on a non-200 response, or None with results=None on a
        network error. When authenticate is True the request carries REST auth so private
        products (e.g. SEG export tariffs) are accessible.
        """
        auth = None
        headers = {}
        if authenticate:
            auth, headers = await self._build_rest_auth()

        all_results = []
        pages = 0
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while url and pages < 3:
                    async with session.get(url, auth=auth, headers=headers) as response:
                        if response.status != 200:
                            return all_results, response.status
                        data = await response.json()

                    all_results.extend(data.get("results", []))
                    url = data.get("next")  # Pagination
                    pages += 1
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Kraken: Network error fetching rates: {e}")
            return None, None

        if url:
            self.log(f"Warn: Kraken: Rate pagination capped at {pages} pages, more data available")
        return all_results, None

    async def async_fetch_rates(self, tariff=None):
        """Fetch rates from the REST product endpoint. Returns a list of rate objects or None.

        Strategy (mirrors the reference EDF integration): try the public REST endpoint first,
        and on a permanent "product not found" status (404/410) retry the SAME endpoint with
        REST auth — EDF SEG / export tariffs are private products that 404 unauthenticated.
        Only if the authenticated retry also fails do we fall back to the GraphQL applicableRates
        query (which is not available on every provider), so export rates are recovered wherever
        possible.
        """
        tariff = tariff or self.current_tariff
        if not tariff:
            return None

        is_export = self.export_tariff is not None and tariff == self.export_tariff
        # Pick the MPAN + account the applicableRates fallback should query for this tariff.
        if is_export:
            fallback_mpan = self.export_mpan
            fallback_account = self.export_account_id or self.account_id
        else:
            fallback_mpan = self.import_mpan
            fallback_account = self.account_id
        url = self.build_rates_url(tariff["product_code"], tariff["tariff_code"])

        # 1) Public (unauthenticated) attempt.
        results, err = await self._fetch_rates_rest(url, authenticate=False)

        # 2) On a permanent "not found", the product is likely private — retry authenticated.
        if err in (404, 410):
            self.log(f"Kraken: REST rates HTTP {err} for {tariff['tariff_code']}, retrying authenticated")
            results, err = await self._fetch_rates_rest(url, authenticate=True)

        if err is not None:
            # A 404/410 with a known MPAN is the EXPECTED path for private products — EDF SEG
            # export tariffs are never served by the REST products API, so this fires on every
            # export fetch. Recover via GraphQL applicableRates and only count a failure if that
            # also comes back empty; otherwise the failure counter would climb every cycle
            # despite the fetch succeeding. Transient errors (429/500/503) fall through to the
            # genuine-failure path below.
            if err in (404, 410) and fallback_mpan:
                kind = "export" if is_export else "import"
                self.log(f"Kraken: REST rates HTTP {err} for {tariff['tariff_code']} (private product), using GraphQL applicableRates for {kind} MPAN {fallback_mpan}")
                rates = await self.async_fetch_rates_graphql(fallback_mpan, account_id=fallback_account)
                if rates is None:
                    self.failures_total += 1
                return rates
            self.log(f"Warn: Kraken: Rates HTTP {err} for {url}")
            self.failures_total += 1
            return None

        if results is None:
            self.failures_total += 1
            return None

        results = self._normalize_rate_timestamps(results)
        self.log(f"Kraken: Fetched {len(results)} rate periods for {tariff['tariff_code']}")
        return results

    def get_entity_name(self, root, suffix):
        """Construct entity name. Same pattern as OctopusAPI.get_entity_name."""
        entity_name = root + "." + self.prefix + "_kraken_" + self.account_id.replace("-", "_") + "_" + suffix
        return entity_name.lower()

    async def async_fetch_standing_charges_graphql(self, mpan):
        """Fetch standing charge via GraphQL applicableStandingCharges — fallback when REST returns non-200.

        Used when the product code has been removed from the REST API (e.g. TOU tariffs on E.ON Next
        whose /standing-charges/ REST endpoint returns 404). Returns the standing charge in pounds/day
        (pence/day divided by 100), or None on failure.

        Args:
            mpan: The import MPAN (meter point access number) for the account.

        Returns the standing charge as pounds/day (float), or None.
        """
        now = datetime.now(timezone.utc)
        midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Use a 3-day window (yesterday → tomorrow+1) to ensure the current standing charge
        # is captured regardless of when the agreement period started or ends.
        start_at = (midnight_utc - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_at = (midnight_utc + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = KRAKEN_STANDING_CHARGES_QUERY.format(
            account_number=self.account_id,
            mpan=mpan,
            start_at=start_at,
            end_at=end_at,
        )
        data = await self.async_graphql_query(query, "applicable-standing-charges-graphql")
        if not data:
            return None

        applicable_charges = self._connection_nodes(data.get("applicableStandingCharges"))
        if not applicable_charges:
            self.log("Warn: Kraken: applicableStandingCharges GraphQL returned no results")
            return None

        # Take the first (most applicable) standing charge entry; value is pence/day inc VAT.
        # Divide by 100 to match the units expected by the caller (pounds/day).
        charge = applicable_charges[0]
        value = charge.get("value")
        if value is None:
            return None
        self.log(f"Kraken: Fetched standing charge via GraphQL applicableStandingCharges for MPAN {mpan}: {value}p/day")
        return float(value) / 100.0

    async def async_fetch_standing_charges(self, tariff=None):
        """Fetch standing charges from public REST endpoint. No auth needed.

        Falls back to GraphQL applicableStandingCharges if the REST endpoint returns a non-200
        status (e.g. 404 for TOU tariffs on E.ON Next whose product is not in the REST API)
        and self.import_mpan is known.
        """
        tariff = tariff or self.current_tariff
        if not tariff:
            return None

        url = self.build_standing_charge_url(tariff["product_code"], tariff["tariff_code"])

        http_error_status = None
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        self.log(f"Warn: Kraken: Standing charges HTTP {response.status}")
                        self.failures_total += 1
                        http_error_status = response.status
                    else:
                        data = await response.json()

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Kraken: Network error fetching standing charges: {e}")
            self.failures_total += 1
            return None

        if http_error_status is not None:
            # Only fall back to GraphQL for permanent "product not found" responses (404/410).
            # Transient errors (429, 500, 503, …) should surface as failures, not trigger
            # an extra GraphQL request that would mask the outage.
            if http_error_status in (404, 410) and self.import_mpan:
                self.log(f"Kraken: REST standing charges returned HTTP {http_error_status}, falling back to GraphQL applicableStandingCharges for MPAN {self.import_mpan}")
                return await self.async_fetch_standing_charges_graphql(self.import_mpan)
            return None

        results = data.get("results", [])
        if results:
            # API returns pence/day; fetch.py multiplies by 100 expecting pounds/day
            value = results[0].get("value_inc_vat")
            return value / 100.0 if value is not None else None
        return None

    def _cache_filename(self):
        """Logical storage filename for this account's cache blob."""
        return f"account_{self.account_id}"

    @staticmethod
    def _data_age_minutes(fetched_at):
        """Return minutes since fetched_at, or a large number if it was never fetched.

        fetched_at is a naive local datetime (or an ISO string round-tripped through YAML).
        A None/not parseable value is treated as very old so the data is refreshed.
        """
        if fetched_at is None:
            return 9999.0
        if isinstance(fetched_at, str):
            try:
                fetched_at = datetime.fromisoformat(fetched_at)
            except ValueError:
                return 9999.0
        try:
            now = datetime.now(tz=fetched_at.tzinfo) if getattr(fetched_at, "tzinfo", None) else datetime.now()
            return (now - fetched_at).total_seconds() / 60.0
        except (TypeError, OverflowError):
            return 9999.0

    async def load_kraken_cache(self):
        """Restore tariff, MPANs, rates and fetch timestamps from storage after a restart.

        Populating these before the first run() means the sensors can be published immediately
        from cache and the API is only re-queried once the data is actually stale.
        """
        data = await self.storage.load("kraken", self._cache_filename()) if self.storage else None
        if not data:
            return

        self.current_tariff = data.get("current_tariff")
        self.export_tariff = data.get("export_tariff")
        self.import_mpan = data.get("import_mpan")
        # Prefer cached discovered values but never clobber a configured value with None.
        self.export_mpan = data.get("export_mpan") or self.export_mpan
        self.export_account_id = data.get("export_account_id") or self.export_account_id
        self.import_rates = data.get("import_rates")
        self.export_rates = data.get("export_rates")
        self.import_standing_charge = data.get("import_standing_charge")
        self.export_rates_available = bool(data.get("export_rates_available"))
        self.tariff_fetched_at = data.get("tariff_fetched_at")
        self.rates_fetched_at = data.get("rates_fetched_at")
        if isinstance(data.get("intelligent_devices"), dict):
            self.intelligent_devices = data.get("intelligent_devices")
        self.dispatch_fetched_at = data.get("dispatch_fetched_at")

        if self.current_tariff:
            self.log(
                "Kraken: Restored cache — tariff {} (age {:.0f}m), rates age {:.0f}m".format(
                    self.current_tariff.get("tariff_code"),
                    self._data_age_minutes(self.tariff_fetched_at),
                    self._data_age_minutes(self.rates_fetched_at),
                )
            )
            self.update_success_timestamp()

    async def save_kraken_cache(self):
        """Persist tariff, MPANs, rates and fetch timestamps to storage so they survive a restart."""
        if not self.storage:
            return
        cache = {
            "current_tariff": self.current_tariff,
            "export_tariff": self.export_tariff,
            "import_mpan": self.import_mpan,
            "export_mpan": self.export_mpan,
            "export_account_id": self.export_account_id,
            "import_rates": self.import_rates,
            "export_rates": self.export_rates,
            "import_standing_charge": self.import_standing_charge,
            "export_rates_available": self.export_rates_available,
            "tariff_fetched_at": self.tariff_fetched_at,
            "rates_fetched_at": self.rates_fetched_at,
            "intelligent_devices": self.intelligent_devices,
            "dispatch_fetched_at": self.dispatch_fetched_at,
        }
        await self.storage.save("kraken", self._cache_filename(), cache, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(days=7))

    def _publish_rate_sensors(self):
        """Publish the import/export rate + standing-charge sensors from the current in-memory data.

        Called on the first run (so cached data populates HA immediately) and whenever rates are
        refreshed. No-ops for any datum that is not present yet.
        """
        if self.import_rates:
            self.dashboard_item(
                self.get_entity_name("sensor", "import_rates"),
                state=len(self.import_rates),
                attributes={
                    "friendly_name": "Kraken Import Rates",
                    "rates": self.import_rates,
                    "tariff_code": self.current_tariff["tariff_code"],
                    "product_code": self.current_tariff["product_code"],
                    "icon": "mdi:currency-gbp",
                },
                app="kraken",
            )
        if self.import_standing_charge is not None:
            self.dashboard_item(
                self.get_entity_name("sensor", "import_standing"),
                state=self.import_standing_charge,
                attributes={
                    "friendly_name": "Kraken Standing Charge",
                    "unit_of_measurement": "£/day",
                    "tariff_code": self.current_tariff["tariff_code"],
                    "icon": "mdi:currency-gbp",
                },
                app="kraken",
            )
        if self.export_rates and self.export_tariff:
            self.dashboard_item(
                self.get_entity_name("sensor", "export_rates"),
                state=len(self.export_rates),
                attributes={
                    "friendly_name": "Kraken Export Rates",
                    "rates": self.export_rates,
                    "tariff_code": self.export_tariff["tariff_code"],
                    "product_code": self.export_tariff["product_code"],
                    "icon": "mdi:currency-gbp",
                },
                app="kraken",
            )

    # ------------------------------------------------------------------
    # SmartFlex intelligent dispatches (provider-managed smart charging)
    # ------------------------------------------------------------------

    @staticmethod
    def _device_index_suffix(device_id):
        """Entity-name index suffix for a device id — the last hyphen-separated segment."""
        return device_id.split("-")[-1] if device_id and "-" in device_id else (device_id or "")

    @staticmethod
    def _parse_dispatch_dt(value):
        """Parse an ISO datetime string (tolerating a trailing Z) to an aware datetime, or None."""
        if not value or not isinstance(value, str):
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def async_discover_smart_devices(self):
        """Discover live SmartFlex EV devices (connected car / charge point) on the account.

        Rebuilds self.intelligent_devices with current device metadata, preserving any dispatches
        already fetched for a device and dropping devices that are no longer live. Provider-agnostic
        — accounts with no smart-charging enrolment simply get an empty set (the common case).
        """
        data = await self.async_graphql_query(KRAKEN_DEVICES_QUERY.format(account_number=self.account_id), "devices")
        if data is None:
            return  # Network/auth failure — keep whatever we had (possibly restored from cache)

        live = {}
        for dev in data.get("devices") or []:
            status = (dev.get("status") or {}).get("current")
            if status != "LIVE" or dev.get("deviceType") != "ELECTRIC_VEHICLES":
                continue
            device_id = dev.get("id")
            if not device_id:
                continue
            existing = self.intelligent_devices.get(device_id, {})
            live[device_id] = {
                "device_id": device_id,
                "provider": dev.get("provider"),
                "make": dev.get("make"),
                "model": dev.get("model"),
                "is_charger": dev.get("__typename") == "SmartFlexChargePoint",
                # Product-catalogue lookups aren't available here; predbat falls back to config.
                "vehicle_battery_size_in_kwh": existing.get("vehicle_battery_size_in_kwh"),
                "charge_point_power_in_kw": existing.get("charge_point_power_in_kw"),
                "planned_dispatches": existing.get("planned_dispatches", []),
                "completed_dispatches": existing.get("completed_dispatches", []),
            }

        added = sorted(set(live) - set(self.intelligent_devices))
        removed = sorted(set(self.intelligent_devices) - set(live))
        if added:
            self.log(f"Kraken: SmartFlex devices discovered: {added}")
        if removed:
            self.log(f"Kraken: SmartFlex devices no longer live: {removed}")
        self.intelligent_devices = live

    def _normalize_dispatches(self, raw, completed):
        """Convert raw GraphQL dispatch nodes to predbat's {start,end,charge_in_kwh,source,location} shape."""
        now = datetime.now(timezone.utc)
        result = []
        for d in raw or []:
            start = d.get("start")
            end = d.get("end")
            if not (start and end):
                continue
            if completed:
                delta = d.get("delta")
                meta = d.get("meta") or {}
                source = meta.get("source")
                location = meta.get("location")
            else:
                delta = d.get("energyAddedKwh", d.get("delta"))
                source = d.get("type")
                location = None
            try:
                delta = round(float(delta), 4) if delta is not None else None
            except (ValueError, TypeError):
                delta = None

            # For a PLANNED dispatch already in progress, trim the elapsed portion: advance the
            # start to now and scale the remaining energy. fetch.py's decode_octopus_slot does not
            # trim a started slot when charge_in_kwh > 0, so without this the already-delivered
            # energy would be double-counted, inflating predicted car SoC/cost for the active
            # window. Completed dispatches are historical and left untouched. (Matches OctopusAPI.)
            if not completed:
                start_dt = self._parse_dispatch_dt(start)
                end_dt = self._parse_dispatch_dt(end)
                if start_dt and end_dt and start_dt < now < end_dt:
                    total_minutes = (end_dt - start_dt).total_seconds() / 60.0
                    remaining_minutes = (end_dt - now).total_seconds() / 60.0
                    if total_minutes > 0:
                        if delta is not None:
                            delta = round(delta * remaining_minutes / total_minutes, 4)
                        start = now.replace(microsecond=0).isoformat()

            result.append({"start": start, "end": end, "charge_in_kwh": delta, "source": source, "location": location})
        return result

    def _merge_completed_dispatches(self, cached, new_completed):
        """Merge new completed dispatches with cached history, dedup by start, prune old ones."""
        by_start = {d["start"]: d for d in cached if d.get("start")}
        for d in new_completed:
            if d.get("start"):
                by_start[d["start"]] = d  # newest wins
        cutoff = datetime.now(timezone.utc) - timedelta(days=KRAKEN_DISPATCH_HISTORY_DAYS)
        merged = []
        for d in by_start.values():
            dt = self._parse_dispatch_dt(d.get("start"))
            if dt is None or dt > cutoff:
                merged.append(d)
        merged.sort(key=lambda d: d.get("start") or "")
        return merged

    async def async_fetch_dispatches(self):
        """Fetch planned + completed dispatches for each known device and merge them in.

        Planned dispatches replace the previous set (they are the current optimiser schedule);
        completed dispatches merge with the cached history so metered charging is retained across
        the API's rolling window and restarts.
        """
        for device_id, device in self.intelligent_devices.items():
            data = await self.async_graphql_query(
                KRAKEN_DISPATCHES_QUERY.format(account_number=self.account_id, device_id=device_id),
                "dispatches",
            )
            if data is None:
                continue
            device["planned_dispatches"] = self._normalize_dispatches(data.get("flexPlannedDispatches"), completed=False)
            new_completed = self._normalize_dispatches(data.get("completedDispatches"), completed=True)
            device["completed_dispatches"] = self._merge_completed_dispatches(device.get("completed_dispatches", []), new_completed)

    def _publish_dispatch_sensors(self):
        """Publish an intelligent_dispatch binary_sensor per device and wire octopus_intelligent_slot.

        Sensor format matches OctopusAPI so predbat's fetch consumes it identically: state on/off if a
        dispatch is active now, with planned_dispatches / completed_dispatches attributes (and battery/
        charger sizing) for the planner. Wiring octopus_intelligent_slot makes predbat treat the
        provider-scheduled windows as cheap car-charging slots, exactly like Octopus Intelligent Go.
        """
        if not self.intelligent_devices:
            return
        now = datetime.now(timezone.utc)
        slot_list = []
        for device_id, device in self.intelligent_devices.items():
            index_suffix = self._device_index_suffix(device_id)
            suffix = "intelligent_dispatch_" + index_suffix if index_suffix else "intelligent_dispatch"
            entity = self.get_entity_name("binary_sensor", suffix)
            slot_list.append(entity)

            active = False
            for dispatch in (device.get("planned_dispatches") or []) + (device.get("completed_dispatches") or []):
                start = self._parse_dispatch_dt(dispatch.get("start"))
                end = self._parse_dispatch_dt(dispatch.get("end"))
                if start and end and start <= now < end:
                    active = True
                    break

            attributes = {"friendly_name": "Kraken Intelligent Dispatches", "icon": "mdi:flash", **device}
            self.dashboard_item(entity, "on" if active else "off", attributes=attributes, app="kraken")

        # Wire the dispatch sensors so predbat treats them as car-charging slots (like Octopus IOG),
        # exactly as OctopusAPI.automatic_config does. octopus_intelligent_slot is a per-car sensor
        # list, and fetch.py only reads it for car indices within num_cars — so, like octopus, bump
        # num_cars up to the device count or the sensors would be connected but never consumed.
        self.set_arg("octopus_intelligent_slot", slot_list)
        if self.get_arg("num_cars", 0) < len(slot_list):
            self.set_arg("num_cars", len(slot_list))

    async def run(self, seconds, first):
        """Component run method — called by ComponentBase.start() every 60s.

        Data is cached to storage and only re-fetched when stale, so a restart restores the last
        tariff + rates from disk and re-queries the API only when the cached data has aged out:
        - Tariff discovery: when the cached tariff is >= KRAKEN_TARIFF_REFRESH_MINUTES old (or never fetched)
        - Rates + standing charges: when the cached rates are >= KRAKEN_RATES_REFRESH_MINUTES old (or never fetched)
        - Intelligent dispatches: when >= KRAKEN_DISPATCH_REFRESH_MINUTES old, for accounts with a
          SmartFlex EV device (discovered on the tariff cycle); wired to octopus_intelligent_slot.
        Sensors are always (re)published on the first run so HA is populated immediately from cache.
        """
        if first:
            await self.load_kraken_cache()

        had_success = False

        # Age-based refresh — None (never fetched / cache miss) is treated as very old, so due.
        tariff_due = self._data_age_minutes(self.tariff_fetched_at) >= KRAKEN_TARIFF_REFRESH_MINUTES
        rates_due = self._data_age_minutes(self.rates_fetched_at) >= KRAKEN_RATES_REFRESH_MINUTES
        dispatch_due = self._data_age_minutes(self.dispatch_fetched_at) >= KRAKEN_DISPATCH_REFRESH_MINUTES

        # Tariff discovery
        if tariff_due:
            await self.async_find_tariffs()
            if self.current_tariff:
                self.tariff_fetched_at = datetime.now()
                had_success = True
            # Re-discover SmartFlex devices on the (slow) tariff cycle — cheap and infrequent, so
            # accounts with no EV enrolment never pay the per-device dispatch query below.
            await self.async_discover_smart_devices()

        # Publish tariff sensor from current state (cache or fresh)
        if self.current_tariff and (first or tariff_due):
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

        # Fetch import rates + standing charges + export rates when stale
        if self.current_tariff and rates_due:
            rates = await self.async_fetch_rates()
            if rates:
                had_success = True
                self.import_rates = rates
                self.rates_fetched_at = datetime.now()

            standing_charge = await self.async_fetch_standing_charges()
            if standing_charge is not None:
                had_success = True
                self.import_standing_charge = standing_charge

            # Fetch export rates if export tariff is known
            if self.export_tariff:
                export_rates = await self.async_fetch_rates(tariff=self.export_tariff)
                if export_rates:
                    had_success = True
                    self.export_rates = export_rates
                    self.export_rates_available = True

        # Publish rate sensors from current in-memory data (cache or fresh)
        if self.current_tariff and (first or rates_due):
            self._publish_rate_sensors()

        # Fetch SmartFlex intelligent dispatches for any known devices, then publish + wire them.
        # dispatch_due stays permanently true for accounts with no devices (dispatch_fetched_at
        # never gets set), so track whether we actually refreshed to avoid saving the cache every
        # cycle in the common no-EV case.
        dispatches_refreshed = bool(self.intelligent_devices) and dispatch_due
        if dispatches_refreshed:
            await self.async_fetch_dispatches()
            self.dispatch_fetched_at = datetime.now()
            had_success = True
        if self.intelligent_devices and (first or dispatch_due):
            self._publish_dispatch_sensors()

        # Wire import into fetch.py once tariff is discovered (retries until successful)
        if not self.wired and self.current_tariff:
            self.set_arg("metric_octopus_import", self.get_entity_name("sensor", "import_rates"))
            self.set_arg("metric_standing_charge", self.get_entity_name("sensor", "import_standing"))
            self.wired = True

        # Wire export into fetch.py once export rates are actually available.
        # An export tariff can be discovered (e.g. EDF SEG tariffs registered on the
        # meter point) while the standard-unit-rates endpoint returns HTTP 404, so no
        # rates are ever fetched. Wiring metric_octopus_export to the empty export_rates
        # sensor would make fetch.py take the octopus-export branch and ignore the user's
        # manual rates_export fallback, zeroing out all export in the plan. Only wire once
        # we have real export rate data.
        if not self.export_wired and self.export_tariff and self.export_rates_available:
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

        # Persist to storage whenever we refreshed something, so the next restart restores it.
        # (Not every cycle — if the cache is lost it is simply re-fetched on the next due check.)
        if tariff_due or rates_due or dispatches_refreshed:
            await self.save_kraken_cache()

        # Update liveness timestamp if we got data OR tariff is known (prevents
        # spurious liveness failures during transient rate-fetch outages)
        if had_success or self.current_tariff:
            self.update_success_timestamp()

        if first and (self.oauth_failed or not self.current_tariff):
            return False

        return True


class KrakenMockBase:  # pragma: no cover
    """Minimal mock base object so KrakenAPI can be driven from the command line.

    Mirrors the MockBase used by fox.py — provides just enough of the Predbat base
    interface (logging, args, dashboard publishing) for a standalone KrakenAPI run.
    """

    def __init__(self, user_id=None):
        """Initialise the mock base, optionally seeding a Supabase user_id for OAuth."""
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        if user_id:
            self.args["user_id"] = user_id
        self.midnight_utc = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            print_attrs = dict(attributes)
            if "options" in print_attrs:
                print_attrs["options"] = "..."
            print(f"  Attributes: {json.dumps(print_attrs, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        return default

    def set_arg(self, key, value):
        state = None
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and "." in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")


async def test_kraken_api(
    provider,
    account_id,
    key=None,
    email=None,
    password=None,
    token_hash=None,
    token_expires=None,
    supabase_url=None,
    supabase_key=None,
    user_id=None,
    mpan=None,
    export_account_id=None,
    export_mpan=None,
    base_url=None,
    discover=False,
    introspect_type=None,
    dispatches=False,
):  # pragma: no cover
    """Run a live Kraken (EDF/E.ON) API test from the command line.

    Auth is selected from the supplied credentials: an API key or email+password uses
    local auth; otherwise Supabase details (url + key + user_id) use the SaaS OAuth path.
    The token hash is NOT required — the oauth-refresh edge function looks the token up by
    user_id + provider and returns a fresh one; token_hash is only an optional dedup echo.
    With --discover the viewer query lists every account and its import/export tariffs
    (useful for debugging missing export rates); with --introspect-type it prints the fields
    of a schema type (e.g. an applicableRates connection node); otherwise a single run() runs.
    """
    import os

    # Supabase env vars must be set before KrakenAPI is constructed so OAuthMixin sees them.
    if supabase_url:
        os.environ["SUPABASE_URL"] = supabase_url
    if supabase_key:
        os.environ["SUPABASE_KEY"] = supabase_key

    if key:
        auth_method = "api_key"
    elif email and password:
        auth_method = "email"
    elif supabase_url or user_id or token_hash:
        # OAuth: the edge function fetches the token from Supabase using user_id + provider.
        auth_method = "oauth"
    else:
        auth_method = "api_key"

    if auth_method == "oauth":
        missing = [n for n, v in (("SUPABASE_URL", supabase_url), ("SUPABASE_KEY", supabase_key), ("user-id", user_id)) if not v]
        if missing:
            print(f"Warn: OAuth selected but missing {', '.join(missing)} — token refresh will be skipped and calls will fail.")

    print(f"Testing Kraken API — provider={provider} account={account_id} auth={auth_method}")

    mock_base = KrakenMockBase(user_id=user_id)
    kraken = KrakenAPI(
        mock_base,
        provider=provider,
        account_id=account_id,
        key=key or "",
        email=email,
        password=password,
        auth_method=auth_method,
        token_expires_at=token_expires,
        token_hash=token_hash,
        mpan=mpan,
        export_account_id=export_account_id,
        export_mpan=export_mpan,
        base_url=base_url,
    )

    if dispatches:
        print("\n=== Discovering SmartFlex devices (intelligent dispatch check) ===")
        data = await kraken.async_graphql_query(KRAKEN_DEVICES_QUERY.format(account_number=account_id), "devices")
        devices = (data or {}).get("devices") or []
        if not devices:
            print("No SmartFlex devices found — this account has no intelligent dispatches to integrate.")
            print("(Import/export rates already cover the fixed tariff windows; dispatches only apply to EDF-managed smart charging.)")
            return
        for dev in devices:
            status = dev.get("status") or {}
            print(f"  device id={dev.get('id')} type={dev.get('deviceType')} ({dev.get('__typename')}) make={dev.get('make')} model={dev.get('model')} state={status.get('current')}")
        for dev in devices:
            device_id = dev.get("id")
            print(f"\n=== Dispatches for device {device_id} ===")
            dispatch_data = await kraken.async_graphql_query(KRAKEN_DISPATCHES_QUERY.format(account_number=account_id, device_id=device_id), "dispatches")
            if not dispatch_data:
                print("  (dispatch query returned no data / errored — see log above)")
                continue
            planned = dispatch_data.get("flexPlannedDispatches") or []
            completed = dispatch_data.get("completedDispatches") or []
            print(f"  planned={len(planned)}  completed={len(completed)}")
            for p in planned[:5]:
                print(f"    planned  {p.get('start')} -> {p.get('end')}  +{p.get('energyAddedKwh')}kWh  type={p.get('type')}")
            for c in completed[:5]:
                meta = c.get("meta") or {}
                print(f"    done     {c.get('start')} -> {c.get('end')}  delta={c.get('delta')}  src={meta.get('source')} loc={meta.get('location')}")
        return

    if introspect_type:
        print(f"\n=== Introspecting type {introspect_type} ===")
        data = await kraken.async_graphql_query(KRAKEN_INTROSPECT_TYPE_QUERY.format(type_name=introspect_type), "introspect")
        type_info = (data or {}).get("__type")
        if not type_info:
            print(f"No such type '{introspect_type}' in the schema (or auth failed).")
            return
        print(f"{type_info['name']} ({type_info['kind']}) fields:")
        for field in type_info.get("fields") or []:
            field_type = field.get("type") or {}
            type_name = field_type.get("name") or (field_type.get("ofType") or {}).get("name") or field_type.get("kind")
            print(f"  {field['name']}: {type_name}")
        return

    if discover:
        print("\n=== Discovering all accounts (viewer query) ===")
        accounts = await kraken.async_discover_all_accounts()
        if not accounts:
            print("No accounts discovered — check auth, permissions, or provider.")
        for acct in accounts:
            imp = acct.get("import_tariff")
            exp = acct.get("export_tariff")
            print(f"\nAccount {acct['account_number']}  address={acct['address']!r}")
            print(f"  import : {imp['tariff_code'] if imp else None}  (mpan {imp['mpan'] if imp else '-'})")
            print(f"  export : {exp['tariff_code'] if exp else None}  (mpan {exp['mpan'] if exp else '-'})")
        return

    print("\n=== Running run(first=True) ===")
    await kraken.run(seconds=0, first=True)

    print("\n=== Summary ===")
    print(f"Import tariff          : {kraken.current_tariff}")
    print(f"Import MPAN            : {kraken.import_mpan}")
    print(f"Export tariff          : {kraken.export_tariff}")
    print(f"Export MPAN            : {kraken.export_mpan}")
    print(f"Export account id      : {kraken.export_account_id}")
    print(f"Export rates available : {kraken.export_rates_available}")
    print(f"OAuth failed           : {kraken.oauth_failed}")
    print(f"Requests={kraken.requests_total}  Failures={kraken.failures_total}")


def main():  # pragma: no cover
    """Command-line entry point for driving the Kraken API test harness."""
    import argparse

    parser = argparse.ArgumentParser(description="Test Kraken (EDF/E.ON) API")
    parser.add_argument("--provider", default="edf", help="Provider: edf or eon")
    parser.add_argument("--account-id", required=True, help="Kraken account number (e.g. A-XXXXXXXX)")

    # Local auth: API key, or email+password. OAuth needs no token hash — Supabase
    # url/key + user-id are enough (the edge function fetches the token by user_id + provider).
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument("--api-key", help="Kraken API key (local auth)")
    auth_group.add_argument("--email", help="Kraken account email (local auth, use with --password)")

    parser.add_argument("--password", help="Kraken account password (with --email)")
    parser.add_argument("--supabase-url", help="Supabase URL for OAuth token refresh")
    parser.add_argument("--supabase-key", help="Supabase anon key for OAuth token refresh")
    parser.add_argument("--user-id", help="Supabase user ID (instance id) for OAuth token refresh")
    parser.add_argument("--token-hash", help="OAuth token hash (optional dedup echo — not required)")
    parser.add_argument("--token-expires", help="OAuth token expiry timestamp (optional)")
    parser.add_argument("--mpan", help="Preferred import MPAN to match")
    parser.add_argument("--export-account-id", help="Export account number (E.ON split import/export accounts)")
    parser.add_argument("--export-mpan", help="Export MPAN")
    parser.add_argument("--base-url", help="Override the provider base URL")
    parser.add_argument("--discover", action="store_true", help="List all accounts and their import/export tariffs instead of a full run")
    parser.add_argument("--introspect-type", help="Print the fields of a schema type (e.g. ApplicableRateConnectionType) instead of a full run")
    parser.add_argument("--dispatches", action="store_true", help="Check for SmartFlex devices and print their planned/completed intelligent dispatches")

    args = parser.parse_args()

    # Require at least one usable auth path.
    if not (args.api_key or (args.email and args.password) or args.supabase_url or args.user_id):
        parser.error("provide auth: --api-key, or --email/--password, or OAuth via --supabase-url/--supabase-key/--user-id")

    asyncio.run(
        test_kraken_api(
            provider=args.provider,
            account_id=args.account_id,
            key=args.api_key,
            email=args.email,
            password=args.password,
            token_hash=args.token_hash,
            token_expires=args.token_expires,
            supabase_url=args.supabase_url,
            supabase_key=args.supabase_key,
            user_id=args.user_id,
            mpan=args.mpan,
            export_account_id=args.export_account_id,
            export_mpan=args.export_mpan,
            base_url=args.base_url,
            discover=args.discover,
            introspect_type=args.introspect_type,
            dispatches=args.dispatches,
        )
    )


if __name__ == "__main__":
    main()

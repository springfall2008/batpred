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
from zoneinfo import ZoneInfo

from component_base import ComponentBase
from const import TIME_FORMAT
from utils import (
    filter_payment_method,
    Backoff,
    is_edge_block_body,
    is_kraken_rate_limit_error,
    parse_retry_after,
    GRAPHQL_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    GRAPHQL_RATE_LIMIT_BACKOFF_MAX_SECONDS,
)
from mock_base import MockBase as KrakenMockBase
from utils import str2time

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
#
# standingCharge / preVatStandingCharge are on the same interface (EDF and E.ON alike). Their
# ratio is the VAT multiplier used to bring exc-VAT applicableRates prices up to inc-VAT.
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
            ... on TariffType {{ tariffCode displayName productCode standingCharge preVatStandingCharge }}
          }}
        }}
      }}
    }}
  }}
}}"""

# Viewer query to discover all account numbers under the authenticated user
KRAKEN_VIEWER_QUERY = """{ viewer { accounts { number } } }"""

# GraphQL applicableRates query — fallback when the REST product endpoint cannot serve the
# tariff: 404/410 (product code removed/replaced while customer is still on the tariff, a private
# product such as an EDF SEG export tariff, or a TOU tariff absent from the REST API such as
# E-TOU-* on E.ON Next) or 400 (the product is in the REST API but has no /standard-unit-rates/
# resource, as day/night-structured EDF tariffs answer — GH#5166).
# Returns value (pence/kWh EXC VAT), validFrom, validTo for the requested window. Unlike the
# REST products API there is no inc-VAT field, so the caller scales by the VAT multiplier taken
# from the account agreement (see KrakenAPI._vat_multiplier).
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
# The device provider field is not requested: Kraken deprecated it (removal on or after 2027-04-15)
# and nothing reads it, so the stored provider is filled from make, as the Octopus component does.
KRAKEN_DEVICES_QUERY = """{{
  devices(accountNumber: "{account_number}") {{
    id
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

# Auth error codes that trigger token refresh + retry. KT-CT-1124 is "Signature of the JWT has expired."
KRAKEN_AUTH_ERROR_CODES = ("KT-CT-1139", "KT-CT-1111", "KT-CT-1143", "KT-CT-1124")

KRAKEN_PROVIDER_NAMES = {"edf": "EDF", "eon": "E.ON Next"}

# REST statuses meaning "the products API cannot serve this tariff's rates", as opposed to a
# transient outage (429/500/503) — these are the ones worth falling back to GraphQL for.
#  404/410 — the product is not in the REST API at all: a private product (EDF SEG / export
#            tariffs) or one retired while the customer is still on it.
#  400     — the tariff exists but has no /standard-unit-rates/ resource. Day/night-structured
#            tariffs answer "This tariff has day and night rates, not standard." (GH#5166),
#            which is permanent for the tariff, not a request the customer can fix.
# Auth makes no difference to a 400, so the authenticated retry stays on 404/410 only.
KRAKEN_REST_PRODUCT_NOT_FOUND_STATUSES = (404, 410)
KRAKEN_REST_RATES_UNAVAILABLE_STATUSES = (400,) + KRAKEN_REST_PRODUCT_NOT_FOUND_STATUSES

# applicableRates connection pagination — request this many nodes per page, cap total pages
# so a runaway cursor can't loop forever. 100 * 20 = 2000 half-hourly periods (~41 days) is far
# more than the ~4-day planning window ever needs.
KRAKEN_RATES_PAGE_SIZE = 100
KRAKEN_RATES_MAX_PAGES = 20

# Largest inc-VAT / exc-VAT ratio accepted from an agreement's standing charges. VAT only ever
# adds, and never more than a quarter, so anything outside 1.0 - this is bad data, not a VAT rate.
KRAKEN_VAT_MULTIPLIER_MAX = 1.25

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

# EDF Flextras: free hours the customer books in the EDF app, an hour at a time, and can change
# up to the Thursday before. They are not in the Kraken API; EDF serve them on an edfenergy.com
# server-driven UI screen that accepts the same Kraken token, and that screen carries a
# machine-readable list of the booked slots (BOOKED_HOURS_LIST). The property id in the URL
# comes from the Kraken properties query. EDF only - E.ON Next has no Flextras.
KRAKEN_PROPERTIES_QUERY = """{{
  properties(accountNumber: "{account_number}") {{
    id
  }}
}}"""
KRAKEN_FLEXTRAS_HOURS_URL = "https://www.edfenergy.com/support/energyhub/api/weekend-saver/v1/{account_id}/{property_id}/ui/hours"
KRAKEN_FLEXTRAS_REFRESH_MINUTES = 30
# Booked slot times are UK wall-clock, although the booking call labels them UTC.
KRAKEN_FLEXTRAS_TZ = ZoneInfo("Europe/London")


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
        self.import_vat_multiplier = 1.0  # inc-VAT / exc-VAT ratio from the import agreement, for applicableRates
        self.current_tariff = None
        self.export_tariff = None  # Export tariff (discovered dynamically)
        self.wired = False
        self.export_wired = False
        self.export_rates_available = False  # True once export rates are actually fetched (not just tariff discovered)
        self.requests_total = 0
        self.failures_total = 0
        self.oauth_failed = False
        # Running while GraphQL queries are paused after a rate limit - see async_graphql_query
        self.graphql_backoff = Backoff(GRAPHQL_RATE_LIMIT_BACKOFF_BASE_SECONDS, GRAPHQL_RATE_LIMIT_BACKOFF_MAX_SECONDS)

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

        # EDF Flextras booked free hours, published as free electricity sessions. flextras_sessions
        # is a list of {"start", "end"} UTC strings; flextras_fetched_at stamps every attempt, so an
        # account that is not on Flextras is asked once per refresh period rather than every cycle.
        self.flextras_property_id = None
        self.flextras_sessions = []
        self.flextras_fetched_at = None
        self.flextras_last_status = None

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
            # check_and_refresh_oauth_token calls these, so they must be bound too - binding
            # only the entry points leaves it reaching for attributes this instance lacks.
            self.start_token_mint_backoff = _types.MethodType(_KrakenAuthMixin.start_token_mint_backoff, self)
            self.log_token_mint_backoff = _types.MethodType(_KrakenAuthMixin.log_token_mint_backoff, self)
            self.clear_token_mint_backoff = _types.MethodType(_KrakenAuthMixin.clear_token_mint_backoff, self)
        elif hasattr(self, "_init_oauth"):
            self._init_oauth(auth_method, key, token_expires_at, "kraken")
            self.token_hash = token_hash or ""
        elif hasattr(self, "_init_kraken_auth"):
            self._init_kraken_auth(auth_method, key=key, email=email, password=password)

    def is_alive(self):
        """Report unhealthy once the login has been rejected, however much cached data is still being served."""
        return self.api_started and not self.oauth_failed

    def health_message(self):
        """Say why Kraken is unhealthy so the run status points at the login rather than just naming the component."""
        if not self.oauth_failed:
            return None
        name = KRAKEN_PROVIDER_NAMES.get(self.provider, "Kraken")
        if self.provider == "eon" and self.auth_method == "email":
            # E.ON Next stopped accepting email/password in obtainKrakenToken around 16 Oct 2026 (#5580)
            return f"{name} login failed - email/password sign-in is rejected, switch kraken_auth_method to api_key"
        if self.auth_method == "oauth":
            return f"{name} authorisation failed - reconnect the {name} account"
        return f"{name} login failed - check the Kraken credentials, then restart the Kraken component"

    def start_graphql_rate_limit_backoff(self, reason, retry_after=None):
        """Pause GraphQL queries after a rate limit, so the rest of the poll does not run into the same limit, and log why.

        Args:
            reason: What limited the query - "rate limited" or "edge/WAF blocked".
            retry_after: Seconds the server asked us to wait, or None.
        """
        delay = self.graphql_backoff.start(reason, retry_after)
        self.log(f"Warn: Kraken: GraphQL {reason} (rate limit {self.graphql_backoff.count}) - pausing GraphQL queries for {delay}s")

    def log_graphql_rate_limit_backoff(self):
        """Repeat the pause reason occasionally: held queries send nothing, so would otherwise log nothing"""
        if self.graphql_backoff.due_reminder():
            self.log(f"Warn: Kraken: GraphQL still rate limited (rate limit {self.graphql_backoff.count}) - holding queries until {self.graphql_backoff.until.strftime(TIME_FORMAT)}")

    def clear_graphql_rate_limit_backoff(self):
        """Clear the GraphQL pause after a successful query"""
        count = self.graphql_backoff.clear()
        if count:
            self.log(f"Kraken: GraphQL rate limit cleared after {count} rate limit(s)")

    async def async_graphql_query(self, query, request_context, _retry_count=0):
        """Execute a GraphQL query with auth and auto-retry on auth errors.

        While a rate limit pause is running (see start_graphql_rate_limit_backoff) a query is not
        sent and returns None. A retry after a token refresh is part of the request that was let
        through, so it is not held.
        """
        if _retry_count == 0 and self.graphql_backoff.active():
            self.log_graphql_rate_limit_backoff()
            return None
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
                        if response.status == 429:
                            self.start_graphql_rate_limit_backoff("rate limited", parse_retry_after(response.headers.get("Retry-After")))
                        elif response.status == 403 and is_edge_block_body(err_body):
                            self.start_graphql_rate_limit_backoff("edge/WAF blocked", parse_retry_after(response.headers.get("Retry-After")))
                        return None
                    body = await response.json()

            if body and is_kraken_rate_limit_error(body.get("errors")):
                self.start_graphql_rate_limit_backoff("rate limited")

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
                self.clear_graphql_rate_limit_backoff()
                return body["data"]

            return None

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"Warn: Kraken: Network error for {request_context}: {e}")
            self.failures_total += 1
            return None

    @staticmethod
    def _vat_multiplier(tariff):
        """Return the inc-VAT / exc-VAT ratio for a GraphQL tariff, or 1.0 if it cannot be derived.

        applicableRates publishes exc-VAT prices only, while everything downstream expects the
        inc-VAT basis the REST products API uses. The VAT rate is not hardcoded - GB domestic
        electricity is zero-rated from October 2026 to March 2027 and 5% either side - but taken
        from the tariff's own standingCharge (inc VAT) / preVatStandingCharge pair, which follows
        whatever rate applies now. A missing, zero or implausible pair leaves prices unscaled.
        """
        if not isinstance(tariff, dict):
            return 1.0
        try:
            inc_vat = float(tariff.get("standingCharge"))
            exc_vat = float(tariff.get("preVatStandingCharge"))
        except (TypeError, ValueError):
            return 1.0
        if exc_vat <= 0:
            return 1.0
        ratio = inc_vat / exc_vat
        if 1.0 <= ratio <= KRAKEN_VAT_MULTIPLIER_MAX:
            return ratio
        return 1.0

    def _find_active_tariff(self, meter_points, preferred_mpan=None, is_export=False):
        """Find the current active tariff from a list of meter points.

        Args:
            meter_points: List of electricityMeterPoints from GraphQL
            preferred_mpan: If set, prefer this MPAN (from SaaS config)
            is_export: If True, only match EXPORT tariff codes; if False, skip them

        Returns:
            dict with tariff_code, product_code, mpan, vat_multiplier or None
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
                    "vat_multiplier": self._vat_multiplier(tariff),
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

        # Refreshed on every discovery, not only a tariff change: the VAT rate can move while the
        # tariff stays the same. Kept off the tariff dict so it never reads as a tariff change.
        vat_multiplier = import_result["vat_multiplier"]
        if vat_multiplier != self.import_vat_multiplier:
            self.log(f"Kraken: Import VAT multiplier {self.import_vat_multiplier:.4f} -> {vat_multiplier:.4f} (from agreement standing charge)")
            self.import_vat_multiplier = vat_multiplier

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
        """Construct public REST rates URL from base URL + tariff info.

        Bounded to the window Predbat reads back (_rates_window), otherwise the endpoint returns every
        period since the contract started, which on a long fixed contract grows past the page cap (#5572).
        The API still returns a period that started before the window and is open-ended.
        """
        start, end = (moment.astimezone(timezone.utc) for moment in self._rates_window())
        return f"{self.base_url}/v1/products/{product_code}/electricity-tariffs/{tariff_code}/standard-unit-rates/?period_from={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&period_to={end.strftime('%Y-%m-%dT%H:%M:%SZ')}&page_size=1500"

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
    def _standing_charge_covering(rows, now, from_key, to_key):
        """Pick the standing charge row in force at now (GH#5573).

        Neither the REST /standing-charges/ list nor the GraphQL applicableStandingCharges window
        promises an order, and both can hold a row that starts after now (a price change published
        ahead of time) or one that has already ended, so the first row is not necessarily today's.
        Same rule as OctopusAPI._get_rate_for_time: a row applies when valid_from <= now < valid_to,
        a null or unparseable bound is open, and the latest valid_from wins if several rows cover
        now (the first of equals). Rows that are not dicts are ignored, like filter_payment_method().

        Args:
            rows: Standing charge rows, as dicts.
            now: Timezone-aware datetime to select for.
            from_key: Name of the validity start field in each row (REST valid_from, GraphQL validFrom).
            to_key: Name of the validity end field in each row (REST valid_to, GraphQL validTo).

        Returns the covering row, or None if no row covers now.
        """

        def bound(row, key):
            """Parse one validity timestamp, or None when it is missing or unparseable."""
            text = row.get(key)
            if not text:
                return None
            try:
                return str2time(text)
            except (ValueError, TypeError):
                return None

        open_start = datetime.min.replace(tzinfo=timezone.utc)
        best_row = None
        best_start = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = bound(row, from_key) or open_start
            end = bound(row, to_key)
            if start > now or (end is not None and end <= now):
                continue
            if best_row is None or start > best_start:
                best_row = row
                best_start = start
        return best_row

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

    async def async_fetch_rates_graphql(self, mpan, account_id=None, vat_multiplier=1.0):
        """Fetch rates via GraphQL applicableRates — fallback when REST returns non-200.

        Used when the product code has been removed from the REST API (e.g. product replaced
        mid-agreement) or the tariff has no /standard-unit-rates/ REST endpoint. The
        applicableRates query returns the rates currently applicable to the customer
        regardless of product lifecycle. Works for both import and export MPANs.

        Args:
            mpan: The MPAN (meter point access number) to query rates for.
            account_id: Account number that owns the MPAN. Defaults to the configured
                import account; pass the export account for E.ON split import/export accounts.
            vat_multiplier: inc-VAT / exc-VAT ratio to scale the exc-VAT prices by. Pass the import
                agreement's ratio for import; leave at 1.0 for export, which carries no VAT.

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
                return self._finalize_graphql_rates(raw_rates, mpan, vat_multiplier) if raw_rates else None

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

        return self._finalize_graphql_rates(raw_rates, mpan, vat_multiplier)

    def _finalize_graphql_rates(self, raw_rates, mpan, vat_multiplier=1.0):
        """Convert applicableRates connection nodes into rate dicts, or None if none are valid.

        The node value is exc-VAT; vat_multiplier scales it to the inc-VAT basis used downstream.
        """
        results = []
        for r in raw_rates:
            value = r.get("value")
            if value is None:
                continue
            value_exc_vat = float(value)
            value_inc_vat = round(value_exc_vat * vat_multiplier, 4)
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

                    if not isinstance(data, dict):
                        # A 200 whose JSON is not an object ([], null, a bare string) would raise at data.get(),
                        # so treat it like the undecodable body below: a failed fetch, not an exception.
                        self.log(f"Warn: Kraken: Invalid response fetching rates: expected a JSON object, got {type(data).__name__}")
                        return None, None
                    all_results.extend(data.get("results", []))
                    url = data.get("next")  # Pagination
                    pages += 1
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            # ValueError covers a 200 whose body claims JSON but does not parse (JSONDecodeError).
            self.log(f"Warn: Kraken: Network error or invalid response fetching rates: {e}")
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
        possible. A 400 goes straight to that fallback without the authenticated retry: it means
        the tariff has no /standard-unit-rates/ resource (day/night-structured tariffs), which
        auth cannot change. A network error during the authenticated retry keeps the status the
        public attempt returned, so the fallback still runs for a tariff already proven 404/410.
        """
        tariff = tariff or self.current_tariff
        if not tariff:
            return None

        is_export = self.export_tariff is not None and tariff == self.export_tariff
        # Pick the MPAN + account the applicableRates fallback should query for this tariff.
        if is_export:
            fallback_mpan = self.export_mpan
            fallback_account = self.export_account_id or self.account_id
            fallback_vat_multiplier = 1.0  # export payments carry no VAT
        else:
            fallback_mpan = self.import_mpan
            fallback_account = self.account_id
            fallback_vat_multiplier = self.import_vat_multiplier
        url = self.build_rates_url(tariff["product_code"], tariff["tariff_code"])

        # 1) Public (unauthenticated) attempt.
        results, err = await self._fetch_rates_rest(url, authenticate=False)

        # 2) On a permanent "not found", the product is likely private — retry authenticated.
        if err in KRAKEN_REST_PRODUCT_NOT_FOUND_STATUSES:
            self.log(f"Kraken: REST rates HTTP {err} for {tariff['tariff_code']}, retrying authenticated")
            rest_unavailable_status = err
            results, err = await self._fetch_rates_rest(url, authenticate=True)
            if results is None and err is None:
                # (None, None) is a network error on the retry, which would otherwise erase the
                # status the public attempt just proved and drop this cycle into the generic
                # failure path. The products API still cannot serve this tariff, so keep that
                # status and take the GraphQL fallback rather than losing the rates to a blip.
                self.log(f"Warn: Kraken: Authenticated retry for {tariff['tariff_code']} hit a network error, treating as HTTP {rest_unavailable_status}")
                err = rest_unavailable_status

        if err is not None:
            # A "REST cannot serve this tariff" status with a known MPAN is the EXPECTED path for
            # two tariff families: EDF SEG export tariffs are private products never served by the
            # REST products API (404), and day/night-structured tariffs have no standard-unit-rates
            # resource at all (400). Both fire on every fetch. Recover via GraphQL applicableRates
            # and only count a failure if that also comes back empty; otherwise the failure counter
            # would climb every cycle despite the fetch succeeding. Transient errors (429/500/503)
            # fall through to the genuine-failure path below.
            if err in KRAKEN_REST_RATES_UNAVAILABLE_STATUSES and fallback_mpan:
                kind = "export" if is_export else "import"
                # 404/410 covers both a private product and one retired from the REST API, so the
                # label has to fit both — a retired product is not a private one.
                reason = "product not in the REST API" if err in KRAKEN_REST_PRODUCT_NOT_FOUND_STATUSES else "tariff has no standard-unit-rates endpoint"
                self.log(f"Kraken: REST rates HTTP {err} for {tariff['tariff_code']} ({reason}), using GraphQL applicableRates for {kind} MPAN {fallback_mpan}")
                failures_before = self.failures_total
                rates = await self.async_fetch_rates_graphql(fallback_mpan, account_id=fallback_account, vat_multiplier=fallback_vat_multiplier)
                if rates is None and self.failures_total == failures_before:
                    # async_graphql_query counts its own failures (auth, non-200, GraphQL errors,
                    # network), so only add a count when the fallback came back empty without
                    # counting one — otherwise a down GraphQL would score 2 per cycle.
                    self.failures_total += 1
                return rates
            self.log(f"Warn: Kraken: Rates HTTP {err} for {url}")
            self.failures_total += 1
            return None

        if results is None:
            self.failures_total += 1
            return None

        # Same DIRECT_DEBIT / NON_DIRECT_DEBIT overlap as the standing charges (GH#5502): the rows
        # are merged last-write-wins downstream, so without this the rate depends on row order.
        results = self._normalize_rate_timestamps(filter_payment_method(results))
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

        # The window spans several days, so it can hold both sides of a price change: take the entry
        # in force now, not the first one listed (GH#5573). value is pence/day inc VAT.
        # Divide by 100 to match the units expected by the caller (pounds/day).
        charge = self._standing_charge_covering(applicable_charges, now, "validFrom", "validTo")
        if charge is None:
            self.log("Warn: Kraken: applicableStandingCharges GraphQL returned no standing charge covering now")
            return None
        value = charge.get("value")
        if value is None:
            return None
        self.log(f"Kraken: Fetched standing charge via GraphQL applicableStandingCharges for MPAN {mpan}: {value}p/day")
        return float(value) / 100.0

    async def async_fetch_standing_charges(self, tariff=None):
        """Fetch standing charges from public REST endpoint. No auth needed.

        Falls back to GraphQL applicableStandingCharges only on a permanent "product not found"
        status (404/410 — e.g. TOU tariffs on E.ON Next whose product is not in the REST API)
        when self.import_mpan is known. Transient statuses (429/500/503) are counted as failures
        instead, and unlike the rates endpoint a 400 does not arise here: /standing-charges/ is
        register-independent, so day/night-structured tariffs still serve it (GH#5166).
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
                        http_error_status = response.status
                    else:
                        data = await response.json()

        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            # ValueError covers a 200 whose body claims JSON but does not parse (JSONDecodeError).
            self.log(f"Warn: Kraken: Network error or invalid response fetching standing charges: {e}")
            self.failures_total += 1
            return None

        if http_error_status is not None:
            # Only fall back to GraphQL for permanent "product not found" responses (404/410) —
            # the same statuses the rates fallback retries authenticated on, shared via the
            # constant so the gates cannot drift. Transient errors (429, 500, 503, …) should
            # surface as failures, not trigger an extra GraphQL request that would mask the
            # outage. 400 is deliberately absent: it is a rates-endpoint-only answer.
            if http_error_status in KRAKEN_REST_PRODUCT_NOT_FOUND_STATUSES and self.import_mpan:
                self.log(f"Kraken: REST standing charges returned HTTP {http_error_status}, falling back to GraphQL applicableStandingCharges for MPAN {self.import_mpan}")
                # This 404/410 recurs every cycle for the tariffs it covers, so it is only a failure if the
                # fallback also comes back empty — and async_graphql_query counts its own failures, so
                # add one only when it did not (same guard as async_fetch_rates).
                failures_before = self.failures_total
                value = await self.async_fetch_standing_charges_graphql(self.import_mpan)
                if value is None and self.failures_total == failures_before:
                    self.failures_total += 1
                return value
            self.log(f"Warn: Kraken: Standing charges HTTP {http_error_status}")
            self.failures_total += 1
            return None

        if not isinstance(data, dict):
            # A 200 whose JSON is not an object ([], null, a bare string) would raise at data.get() below.
            self.log(f"Warn: Kraken: Invalid response fetching standing charges: expected a JSON object, got {type(data).__name__}")
            self.failures_total += 1
            return None

        # The REST endpoint returns a DIRECT_DEBIT and a NON_DIRECT_DEBIT row for each period, and
        # EDF lists the NON_DIRECT_DEBIT one first, so keep the Direct Debit row (GH#5502).
        results = filter_payment_method(data.get("results", []))
        if results:
            # The list also holds past periods and any change already published, so take the row
            # in force now rather than the first one (GH#5573).
            charge = self._standing_charge_covering(results, datetime.now(timezone.utc), "valid_from", "valid_to")
            if charge is None:
                self.log("Warn: Kraken: REST standing charges returned no standing charge covering now")
                return None
            # API returns pence/day; fetch.py multiplies by 100 expecting pounds/day
            value = charge.get("value_inc_vat")
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
        # Absent from a cache written before the multiplier existed; the next discovery sets it.
        self.import_vat_multiplier = data.get("import_vat_multiplier") or 1.0
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
        self.flextras_property_id = data.get("flextras_property_id")
        if isinstance(data.get("flextras_sessions"), list):
            self.flextras_sessions = data.get("flextras_sessions")
        self.flextras_fetched_at = data.get("flextras_fetched_at")

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
            "import_vat_multiplier": self.import_vat_multiplier,
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
            "flextras_property_id": self.flextras_property_id,
            "flextras_sessions": self.flextras_sessions,
            "flextras_fetched_at": self.flextras_fetched_at,
        }
        await self.storage.save("kraken", self._cache_filename(), cache, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(days=7))

    @staticmethod
    def _rates_in_window(rates, window_start, window_end):
        """Return the rate periods that overlap [window_start, window_end], in their original order.

        The end is inclusive because minute_data() keeps its last minute too: a period starting
        exactly there still sets it. A period ending exactly at window_start sets nothing inside.

        A period with no valid_to is open-ended and a period with no valid_from has no start, so
        each is kept if the rest of it overlaps. A period whose timestamps cannot be parsed is kept
        too, so that minute_data() meets it just as it did before the trim was added.
        """
        kept = []
        for rate in rates:
            try:
                valid_from = str2time(rate["valid_from"]) if rate.get("valid_from") else None
                valid_to = str2time(rate["valid_to"]) if rate.get("valid_to") else None
            except (ValueError, TypeError):
                kept.append(rate)
                continue
            if valid_from is not None and valid_from > window_end:
                continue
            if valid_to is not None and valid_to <= window_start:
                continue
            kept.append(rate)
        return kept

    def _published_rates(self, rates):
        """Trim a rate list to the window Predbat reads back, for publishing as a sensor attribute.

        A long fixed contract used to return every period since it started - about three a day, so a
        few months reached 255+ periods (~38 KB), past Home Assistant's 16 KB recorder limit for a
        state's attributes (#5534). The REST request is now bounded to this same window (#5572), but
        a cache saved before that can still hold the full history, so trimming here stays. Predbat reads the sensor back through fetch_octopus_rates(),
        whose minute_data() keeps only (forecast_days + 1) days either side of midnight and drops
        the rest, so publish just that and the plan sees exactly the rates it did before. The
        back end is deliberately wider than the Octopus component's -24h: when fetches keep
        failing, rate_replicate() copies the last real day forward from up to two days back, and
        trimming that away would lose the rates sooner than before.

        The forward end has one day to spare: the sensor is republished only on the first run and
        when a rates refresh is due (every 30 minutes), so one published just before midnight can
        still be what Predbat reads just after it, when its own window has moved on a day. forecast_days is worked out as fetch.py does,
        including its 24 hour minimum.
        """
        start, end = self._rates_window()
        return self._rates_in_window(rates, start, end)

    def _rates_window(self):
        """Return the (start, end) UTC window of rates Predbat reads back - see _published_rates for why."""
        forecast_hours = max(self.get_arg("forecast_hours", 48), 24)
        forecast_days = int((forecast_hours + 23) / 24)
        midnight = self.midnight_utc
        return midnight - timedelta(days=forecast_days + 1), midnight + timedelta(days=forecast_days + 2)

    def _publish_rate_sensors(self):
        """Publish the import/export rate + standing-charge sensors from the current in-memory data.

        Called on the first run (so cached data populates HA immediately) and whenever rates are
        refreshed. No-ops for any datum that is not present yet. The rates attributes carry only
        the window Predbat reads back (see _published_rates()), and the state is the number of
        periods published.

        Icons remain hard-coded as currency-gbp and have not been auto-generated as per #5683 as the Kraken
        integration is only used by UK customers so the extra complexity of passing the icon through to this
        component was not justified nor needed.
        """
        if self.import_rates:
            import_rates = self._published_rates(self.import_rates)
            self.dashboard_item(
                self.get_entity_name("sensor", "import_rates"),
                state=len(import_rates),
                attributes={
                    "friendly_name": "Kraken Import Rates",
                    "rates": import_rates,
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
            export_rates = self._published_rates(self.export_rates)
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
                "provider": dev.get("make"),
                "make": dev.get("make"),
                "model": dev.get("model"),
                "is_charger": dev.get("__typename") == "SmartFlexChargePoint",
                # Product-catalogue lookups aren't available here; predbat falls back to config.
                "vehicle_battery_size_in_kwh": existing.get("vehicle_battery_size_in_kwh"),
                "charge_point_power_in_kw": existing.get("charge_point_power_in_kw"),
                "planned_dispatches": existing.get("planned_dispatches", []),
                "completed_dispatches": existing.get("completed_dispatches", []),
                "dispatches_fetched_at": existing.get("dispatches_fetched_at"),
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

        Returns True if at least one device's dispatches were fetched. Each device records its own
        dispatches_fetched_at (published as an attribute), so a device whose query failed keeps its
        cached lists and its old timestamp even when another device refreshed.
        """
        fetched = False
        for device_id, device in self.intelligent_devices.items():
            data = await self.async_graphql_query(
                KRAKEN_DISPATCHES_QUERY.format(account_number=self.account_id, device_id=device_id),
                "dispatches",
            )
            if data is None:
                continue
            fetched = True
            device["dispatches_fetched_at"] = datetime.now(timezone.utc).isoformat()
            device["planned_dispatches"] = self._normalize_dispatches(data.get("flexPlannedDispatches"), completed=False)
            new_completed = self._normalize_dispatches(data.get("completedDispatches"), completed=True)
            device["completed_dispatches"] = self._merge_completed_dispatches(device.get("completed_dispatches", []), new_completed)
        return fetched

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

    # ------------------------------------------------------------------
    # EDF Flextras booked free hours
    # ------------------------------------------------------------------

    @staticmethod
    def _flextras_slot_utc(date_str, time_str):
        """Convert a Flextras slot's date + UK wall-clock time ("2026-10-10", "09:00") to an aware UTC datetime, or None."""
        if not isinstance(date_str, str) or not isinstance(time_str, str):
            return None
        try:
            naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except ValueError:
            return None
        return naive.replace(tzinfo=KRAKEN_FLEXTRAS_TZ).astimezone(timezone.utc)

    @staticmethod
    def parse_flextras_booked_hours(body, log=None):
        """Extract the booked free hours from the Flextras hours screen as merged UTC windows.

        Returns a list of {"start", "end"} strings in TIME_FORMAT, sorted, with back-to-back
        hours joined into one window. Returns None when the payload is not the hours screen at
        all - or its booked list is the wrong shape, or EDF mark that component as failed - so
        the caller keeps what it had rather than treating a bad response as "nothing booked";
        a recognised screen with no bookings returns [].

        Only a slot with status SELECTED (or no status) is a booking. Unbooked entitlement is not
        listed as slots (it is a count elsewhere on the screen), so any other status is skipped
        rather than priced at 0p, and reported once per status through log when one is given.
        """
        if not isinstance(body, dict) or not isinstance(body.get("components"), list):
            return None
        booked = None
        for component in body["components"]:
            if isinstance(component, dict) and component.get("type") == "BOOKED_HOURS_LIST" and isinstance(component.get("props"), dict):
                if component.get("status", "SUCCESS") != "SUCCESS":
                    return None
                booked = component["props"]
                break
        if booked is None:
            return None

        items = booked.get("items") or []
        if not isinstance(items, list):
            return None
        windows = []
        skipped_statuses = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            slots = item.get("rawTimeSlots") or []
            if not isinstance(slots, list):
                return None
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                status = slot.get("status", "SELECTED")
                if status != "SELECTED":
                    if log and status not in skipped_statuses:
                        log(f"Warn: Kraken: Flextras slot {slot.get('date')} {slot.get('startTime')} has status {status!r}, not SELECTED - not treated as a booked free hour")
                    skipped_statuses.add(status)
                    continue
                start = KrakenAPI._flextras_slot_utc(slot.get("date"), slot.get("startTime"))
                end = KrakenAPI._flextras_slot_utc(slot.get("date"), slot.get("endTime"))
                if start is None or end is None:
                    continue
                if end <= start:
                    # An hour that runs over midnight is dated by the day it starts
                    end += timedelta(days=1)
                windows.append((start, end))

        merged = []
        for start, end in sorted(windows):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return [{"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT)} for start, end in merged]

    def _merge_flextras_sessions(self, booked):
        """Combine freshly fetched booked hours with the sessions already held.

        The screen is authoritative for hours that have not started: a customer can move or hand
        back an hour until the Thursday before, so a future hour that is no longer listed is
        dropped. A session that has already started is kept until the day after it ends even if the
        screen stops listing it, so the plan and today's cost do not re-price an hour that was free.
        """
        now = datetime.now(timezone.utc)
        keep_after = self.midnight_utc - timedelta(days=1)
        by_start = {session["start"]: session for session in booked}
        for session in self.flextras_sessions or []:
            try:
                start = str2time(session["start"])
                end = str2time(session["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start <= now and end >= keep_after and session["start"] not in by_start:
                by_start[session["start"]] = session
        return sorted(by_start.values(), key=lambda session: session["start"])

    async def async_find_property_id(self):
        """Return the account's first property id (needed by the Flextras URL), or None if it cannot be read."""
        data = await self.async_graphql_query(KRAKEN_PROPERTIES_QUERY.format(account_number=self.account_id), "properties")
        for prop in (data or {}).get("properties") or []:
            if isinstance(prop, dict) and prop.get("id"):
                return str(prop["id"])
        return None

    async def _async_get_flextras_screen(self):
        """GET the Flextras hours screen, looking up the property id first if it is not known yet.

        Returns (status, body): body is the decoded JSON for a 200, otherwise the response text so
        a caller can show what EDF said. status is None when no request could be made (no property
        id, no token, or a network error).
        """
        if not self.flextras_property_id:
            self.flextras_property_id = await self.async_find_property_id()
            if not self.flextras_property_id:
                return None, None

        token_ok = await self.check_and_refresh_oauth_token()
        if not token_ok or not self.access_token:
            return None, None

        url = KRAKEN_FLEXTRAS_HOURS_URL.format(account_id=self.account_id, property_id=self.flextras_property_id)
        headers = {"Authorization": self.access_token, "Accept": "application/json"}
        try:
            self.requests_total += 1
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    status = response.status
                    body = await response.json(content_type=None) if status == 200 else await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"Warn: Kraken: Flextras hours request failed: {e}")
            return None, None
        return status, body

    async def async_fetch_flextras_hours(self):
        """Fetch the Flextras hours screen and return its booked hours, or None if it could not be read.

        Accounts that are not on Flextras, and tokens edfenergy.com does not accept, both come back
        as a non-200; that is logged when the status changes (not every refresh) and is not counted
        as a Kraken failure, because it says nothing about the tariff and rate data this component
        exists for.
        """
        status, body = await self._async_get_flextras_screen()
        if status is None:
            return None
        if status != 200:
            if status != self.flextras_last_status:
                self.log(f"Kraken: Flextras hours not available (HTTP {status}) - no booked free hours will be read")
            self.flextras_last_status = status
            return None
        self.flextras_last_status = status

        booked = self.parse_flextras_booked_hours(body, log=self.log)
        if booked is None:
            self.log("Warn: Kraken: Flextras hours screen not recognised, keeping the booked hours already held")
        return booked

    def _publish_free_electricity_sensor(self):
        """Publish the Flextras sessions as a free electricity sensor and wire octopus_free_electricity.

        The sensor matches OctopusAPI's free_electricity sensor (state on/off, an events attribute of
        {start, end, code, rate: 0}), so fetch_octopus_sessions() prices the booked hours at 0p exactly
        as it does Octopus free electricity sessions.
        """
        now = datetime.now(timezone.utc)
        events = []
        active = False
        for session in self.flextras_sessions:
            events.append({"start": session["start"], "end": session["end"], "code": "flextras_" + session["start"][:16], "rate": 0})
            try:
                if str2time(session["start"]) <= now < str2time(session["end"]):
                    active = True
            except (KeyError, TypeError, ValueError):
                pass
        entity = self.get_entity_name("sensor", "free_electricity")
        self.dashboard_item(entity, "on" if active else "off", attributes={"friendly_name": "EDF Flextras Free Hours", "icon": "mdi:flash", "events": events}, app="kraken")
        self.set_arg("octopus_free_electricity", entity)

    async def run(self, seconds, first):
        """Component run method — called by ComponentBase.start() every 60s.

        Data is cached to storage and only re-fetched when stale, so a restart restores the last
        tariff + rates from disk and re-queries the API only when the cached data has aged out:
        - Tariff discovery: when the cached tariff is >= KRAKEN_TARIFF_REFRESH_MINUTES old (or never fetched)
        - Rates + standing charges: when the cached rates are >= KRAKEN_RATES_REFRESH_MINUTES old (or never fetched)
        - Intelligent dispatches: when >= KRAKEN_DISPATCH_REFRESH_MINUTES old, for accounts with a
          SmartFlex EV device (discovered on the tariff cycle); wired to octopus_intelligent_slot.
        - EDF Flextras booked free hours: attempted every KRAKEN_FLEXTRAS_REFRESH_MINUTES (EDF only);
          published as a free electricity sensor wired to octopus_free_electricity.
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
        # cycle in the common no-EV case. A fetch where every device's query failed is not a
        # refresh: the cached dispatches keep their old timestamp and are retried next cycle.
        dispatches_refreshed = False
        if self.intelligent_devices and dispatch_due:
            dispatches_refreshed = await self.async_fetch_dispatches()
        if dispatches_refreshed:
            self.dispatch_fetched_at = datetime.now()
            had_success = True
        if self.intelligent_devices and (first or dispatch_due):
            self._publish_dispatch_sensors()

        # EDF Flextras booked free hours. Every attempt is stamped, so an account that is not on
        # Flextras (or whose token edfenergy.com rejects) is asked once per refresh period, not every cycle.
        flextras_refreshed = False
        if self.provider == "edf" and self.current_tariff and self._data_age_minutes(self.flextras_fetched_at) >= KRAKEN_FLEXTRAS_REFRESH_MINUTES:
            self.flextras_fetched_at = datetime.now()
            booked = await self.async_fetch_flextras_hours()
            if booked is not None:
                if booked != self.flextras_sessions:
                    self.log(f"Kraken: Flextras booked free hours: {booked}")
                self.flextras_sessions = self._merge_flextras_sessions(booked)
                flextras_refreshed = True
        if self.provider == "edf" and (flextras_refreshed or (first and self.flextras_sessions)):
            self._publish_free_electricity_sensor()

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
                "requests_total": self.requests_total,
                "failures_total": self.failures_total,
                "icon": "mdi:account-check" if status == "connected" else "mdi:account-alert",
            },
            app="kraken",
        )

        # Persist to storage whenever we refreshed something, so the next restart restores it.
        # (Not every cycle — if the cache is lost it is simply re-fetched on the next due check.)
        if tariff_due or rates_due or dispatches_refreshed or flextras_refreshed:
            await self.save_kraken_cache()

        # Update liveness timestamp if we got data OR tariff is known (prevents
        # spurious liveness failures during transient rate-fetch outages)
        if had_success or self.current_tariff:
            self.update_success_timestamp()

        if first and (self.oauth_failed or not self.current_tariff):
            return False

        return True


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
    flextras=False,
    property_id=None,
    auth_method=None,
):  # pragma: no cover
    """Run a live Kraken (EDF/E.ON) API test from the command line.

    Auth is selected from the supplied credentials: an API key or email+password uses
    local auth; otherwise Supabase details (url + key + user_id) use the SaaS OAuth path.
    The token hash is NOT required — the oauth-refresh edge function looks the token up by
    user_id + provider and returns a fresh one; token_hash is only an optional dedup echo.
    With --discover the viewer query lists every account and its import/export tariffs
    (useful for debugging missing export rates); with --introspect-type it prints the fields
    of a schema type (e.g. an applicableRates connection node); with --flextras it fetches the
    EDF Flextras hours screen through the same request the component makes and prints the raw
    response and the booked hours parsed from it; otherwise a single run() runs.
    """
    import json
    import os

    # Supabase env vars must be set before KrakenAPI is constructed so OAuthMixin sees them.
    if supabase_url:
        os.environ["SUPABASE_URL"] = supabase_url
    if supabase_key:
        os.environ["SUPABASE_KEY"] = supabase_key

    if auth_method:
        # Given explicitly (kraken_auth_method from --config): an OAuth access token also arrives as key
        pass
    elif key:
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

    if flextras:
        print("\n=== EDF Flextras booked free hours ===")
        if provider != "edf":
            print(f"Note: Flextras is an EDF scheme - provider is {provider!r}, so the component would never ask for it.")
        kraken.flextras_property_id = property_id
        if not property_id:
            kraken.flextras_property_id = await kraken.async_find_property_id()
            print(f"Property id (properties query): {kraken.flextras_property_id}")
            if not kraken.flextras_property_id:
                print("No property id found - the properties query failed or returned nothing (see log above). Pass --property-id to skip it.")
                return
        print(f"GET {KRAKEN_FLEXTRAS_HOURS_URL.format(account_id=account_id, property_id=kraken.flextras_property_id)}")
        status, body = await kraken._async_get_flextras_screen()
        if status is None:
            print("No request was made - auth or network failure (see log above).")
            return
        print(f"HTTP {status}")
        if status != 200:
            print(f"Body: {str(body)[:2000]}")
            print("The component treats this as 'Flextras not available' and keeps any booked hours it already holds.")
            return
        print(f"Body: {json.dumps(body, indent=2)}")
        booked = kraken.parse_flextras_booked_hours(body, log=print)
        if booked is None:
            print("\nScreen NOT recognised (no usable BOOKED_HOURS_LIST component) - the component would keep what it had.")
        elif not booked:
            print("\nScreen recognised, no hours booked.")
        else:
            print(f"\nBooked free hours ({len(booked)} session(s), UTC) - these would be priced at 0p:")
            for session in booked:
                print(f"  {session['start']} -> {session['end']}")
        return

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
    print(f"Flextras free hours    : {kraken.flextras_sessions} (property {kraken.flextras_property_id})")
    print(f"OAuth failed           : {kraken.oauth_failed}")
    print(f"Requests={kraken.requests_total}  Failures={kraken.failures_total}")


# Kraken settings as they appear in apps.yaml, mapped to the command line fields they stand in for,
# so --config can supply them instead. Written out by hand rather than read from COMPONENT_LIST:
# components.py pulls predbat in behind it. test_kraken_cli_config_keys_match_component keeps
# this in step with what the component actually declares.
KRAKEN_CLI_CONFIG_KEYS = {
    "provider": "kraken_provider",
    "account_id": "kraken_account_id",
    "api_key": "kraken_key",
    "email": "kraken_email",
    "password": "kraken_password",
    "auth_method": "kraken_auth_method",
    "token_hash": "kraken_token_hash",
    "token_expires": "kraken_token_expires_at",
    "mpan": "kraken_mpan",
    "export_account_id": "kraken_export_account_id",
    "export_mpan": "kraken_export_mpan",
    "base_url": "kraken_base_url",
}

# The OAuth refresh settings, which are not Kraken component args: oauth_mixin reads
# SUPABASE_URL/SUPABASE_KEY from the environment and user_id from base.args, so without them an
# OAuth run cannot refresh its token.
KRAKEN_CLI_OAUTH_KEYS = {
    "user_id": "user_id",
    "supabase_url": "supabase_url",
    "supabase_key": "supabase_key",
}


def merge_kraken_cli_settings(cli, config):
    """Fill in any Kraken setting not given on the command line from an apps.yaml pred_bat section.

    An explicit command line value always wins, so a one-off run against another account does not
    mean editing apps.yaml. Returns (merged, used), where used lists the config keys actually taken
    so the caller can name them - a key the CLI does not look for otherwise fails silently, with an
    auth error further down as its only symptom.

    kraken_auth_method decides which credentials apply, as it does in the component: with api_key
    the email/password are left out, with email the key is, and with oauth kraken_key is the OAuth
    access token. A command line credential overrides the config's method, so --api-key on top of
    an OAuth config tests that key.
    """
    merged = dict(cli)
    used = []
    if merged.get("api_key"):
        merged["auth_method"] = merged.get("auth_method") or "api_key"
    elif merged.get("email") and merged.get("password"):
        merged["auth_method"] = merged.get("auth_method") or "email"

    auth_method = merged.get("auth_method") or config.get("kraken_auth_method")
    skip = set()
    if auth_method == "email":
        skip.add("api_key")
    elif auth_method in ("api_key", "oauth"):
        skip.update(("email", "password"))
    if auth_method != "oauth":
        skip.update(("token_hash", "token_expires"))

    for mapping in (KRAKEN_CLI_CONFIG_KEYS, KRAKEN_CLI_OAUTH_KEYS):
        for cli_name, config_key in mapping.items():
            if merged.get(cli_name) not in (None, "") or cli_name in skip:
                continue
            value = config.get(config_key)
            if value not in (None, "", []):
                merged[cli_name] = str(value) if cli_name == "token_expires" else value
                used.append(config_key)
    return merged, used


def main():  # pragma: no cover
    """Command-line entry point for driving the Kraken API test harness."""
    import argparse

    parser = argparse.ArgumentParser(description="Test Kraken (EDF/E.ON) API")
    parser.add_argument("--provider", default=None, help="Provider: edf or eon (default edf)")
    parser.add_argument("--account-id", help="Kraken account number (e.g. A-XXXXXXXX)")
    parser.add_argument(
        "--config",
        help="Read settings from an apps.yaml-format file instead of passing them here, resolving !secret as Predbat does (a secrets.yaml beside the file is used unless PREDBAT_SECRETS_FILE is set). Reads "
        + ", ".join(list(KRAKEN_CLI_CONFIG_KEYS.values()) + list(KRAKEN_CLI_OAUTH_KEYS.values()))
        + ". Anything also given on the command line wins",
    )

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
    parser.add_argument("--flextras", action="store_true", help="Fetch the EDF Flextras hours screen, print the raw response and the booked free hours parsed from it")
    parser.add_argument("--property-id", help="Kraken property id for --flextras (default: looked up with the properties query)")

    args = parser.parse_args()
    settings = {name: getattr(args, name, None) for name in list(KRAKEN_CLI_CONFIG_KEYS) + list(KRAKEN_CLI_OAUTH_KEYS)}

    if args.config:
        import os
        import yaml
        from utils import load_apps_yaml

        # load_secrets() looks in the working directory and the Home Assistant config paths, so a
        # copy of a config kept elsewhere would lose its secrets - use a secrets.yaml beside it.
        beside = os.path.join(os.path.dirname(os.path.abspath(args.config)), "secrets.yaml")
        if not os.getenv("PREDBAT_SECRETS_FILE") and os.path.isfile(beside):
            os.environ["PREDBAT_SECRETS_FILE"] = beside
        try:
            config, _ = load_apps_yaml(args.config)
        except (yaml.YAMLError, KeyError, OSError) as exc:
            parser.error(f"could not read Kraken settings from {args.config}: {exc}")
        settings, used = merge_kraken_cli_settings(settings, config or {})
        print("Read from {}: {}".format(args.config, ", ".join(used) if used else "nothing - no keys the CLI looks for"))
        if settings.get("auth_method") == "oauth" and not (settings.get("supabase_url") and settings.get("supabase_key") and settings.get("user_id")):
            print("Warning: an OAuth token cannot be refreshed without supabase_url, supabase_key and user_id - expect auth errors once it expires")

    if not settings.get("account_id"):
        parser.error("no account: pass --account-id, or --config pointing at an apps.yaml holding kraken_account_id")
    # Require at least one usable auth path.
    if not (settings.get("api_key") or (settings.get("email") and settings.get("password")) or settings.get("supabase_url") or settings.get("user_id")):
        parser.error("provide auth: --api-key, or --email/--password, or OAuth via --supabase-url/--supabase-key/--user-id, or --config")

    asyncio.run(
        test_kraken_api(
            provider=settings.get("provider") or "edf",
            account_id=settings["account_id"],
            key=settings.get("api_key"),
            email=settings.get("email"),
            password=settings.get("password"),
            token_hash=settings.get("token_hash"),
            token_expires=settings.get("token_expires"),
            supabase_url=settings.get("supabase_url"),
            supabase_key=settings.get("supabase_key"),
            user_id=settings.get("user_id"),
            mpan=settings.get("mpan"),
            export_account_id=settings.get("export_account_id"),
            export_mpan=settings.get("export_mpan"),
            base_url=settings.get("base_url"),
            auth_method=settings.get("auth_method"),
            discover=args.discover,
            introspect_type=args.introspect_type,
            dispatches=args.dispatches,
            flextras=args.flextras,
            property_id=args.property_id,
        )
    )


if __name__ == "__main__":
    main()

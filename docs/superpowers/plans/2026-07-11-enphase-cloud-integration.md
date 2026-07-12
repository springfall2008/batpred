# Enphase Cloud Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `EnphaseAPI` component to Predbat giving full monitoring and battery control of Enphase IQ Battery systems via the unofficial Enlighten cloud API, so an Enphase site works as a Predbat-controlled inverter (`inverter_type: EnphaseCloud`).

**Architecture:** New `apps/predbat/enphase.py` subclassing `ComponentBase` (fox.py pattern: `initialize()` + async `run()` polled every 60s, age-tiered refresh, Storage-backed cache). It publishes HA entities via `dashboard_item()`; Predbat's `Inverter` class drives the published control entities, configured by `INVERTER_DEF["EnphaseCloud"]` + `automatic_config()`. Control maps: Self-Use → profile `self-consumption`; Forced Charge → `cfg` schedule (limit = target SOC); Forced Export → `dtg` schedule (limit = SOC floor, capability-gated); freeze → `rbd` schedule; reserve → `batteryBackupPercentage`.

**Tech Stack:** Python 3, aiohttp, existing Predbat framework (`ComponentBase`, Storage, `dashboard_item`), unit tests via `coverage/unit_test.py` registry.

**Spec:** `docs/superpowers/specs/2026-07-11-enphase-cloud-integration-design.md` — read it before starting.

## Global Constraints

- Work on branch `feature/enphase_cloud` (created in Task 0), NOT `fix/solax_token` or `main`.
- Line length: 256 chars (Black) / 250 (Flake8). Variable naming: `lower_case_with_underscores`.
- **Every function and class needs a docstring** (interrogate enforces 100%).
- British English spellings in comments/docs (CSpell en-gb); add new words (Enphase, Enlighten, Encharge, entrez, enho, enlm) to `.cspell/custom-dictionary-workspace.txt` (keep it alphabetically sorted).
- Tests: run from `coverage/` directory. ALWAYS save test output to a file and grep the file, never pipe to grep. Command pattern:
  `cd coverage && ./run_all --test enphase_api > /tmp/enphase_test.txt 2>&1; grep -E "PASS|FAIL|Error" /tmp/enphase_test.txt`
- Storage abstraction only — no direct file access for caching.
- `initialize(**kwargs)` is the constructor hook — do NOT override `__init__` (ComponentBase calls `initialize`).
- All Enphase writes are change-gated: never PUT a value that already matches the cloud state.
- Commit after every task with a `feat:`/`test:`/`docs:` message ending in the Co-Authored-By line from repo convention.

---

### Task 0: Branch and spec commit

**Files:**

- Commit: `docs/superpowers/specs/2026-07-11-enphase-cloud-integration-design.md`

- [ ] **Step 1: Create the feature branch from main and commit the spec**

```bash
cd /Users/treforsouthwell/predbat/batpred
git stash --include-untracked --quiet || true   # only if fix/solax_token has uncommitted work; check git status first
git checkout main && git pull
git checkout -b feature/enphase_cloud
git add docs/superpowers/specs/2026-07-11-enphase-cloud-integration-design.md
git commit -m "docs: add Enphase cloud integration design spec

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

Expected: branch `feature/enphase_cloud` exists with the spec committed. If `git status` showed unrelated uncommitted changes on `fix/solax_token`, leave them stashed and note the stash ref in the task report.

---

### Task 1: Component skeleton, registration, config schema

**Files:**

- Create: `apps/predbat/enphase.py`
- Modify: `apps/predbat/components.py` (import near line 35; `COMPONENT_LIST` entry after the `"fox"` entry ending at line 242)
- Modify: `apps/predbat/config.py` (APPS_SCHEMA, after `fox_token_hash` at line 2202)
- Create: `apps/predbat/tests/test_enphase_api.py`
- Modify: `apps/predbat/unit_test.py` (import + registry entry — follow the pattern of `run_fox_api_tests` at lines ~107 and ~283)

**Interfaces:**

- Produces: `class EnphaseAPI(ComponentBase)` with `initialize(username, password, site_id=None, automatic=False, automatic_ignore_pv=False)`, `is_alive()`, cache helpers `_save_cache(key, data)`, `_load_cache(key)`, `_needs_refresh(key, max_age_minutes)`, `_data_age_minutes(key)`, `load_cached_data()`; module constants `ENPHASE_REFRESH_STATIC=1440`, `ENPHASE_REFRESH_SETTINGS=5`, `ENPHASE_REFRESH_ENERGY=15`, `ENPHASE_REFRESH_POWER=1`, `ENPHASE_CACHE_KEYS`, `ENPHASE_CACHE_VERSION=1`, URL constants.
- Produces (tests): `MockEnphaseAPI(EnphaseAPI)` test double and `run_enphase_api_tests(my_predbat)` entry point that later tasks extend.

- [ ] **Step 1: Write the failing test file**

Create `apps/predbat/tests/test_enphase_api.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Enphase API functions
# -----------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
import pytz
from enphase import EnphaseAPI, ENPHASE_CACHE_KEYS, ENPHASE_CACHE_VERSION, ENPHASE_REFRESH_SETTINGS
from tests.test_infra import run_async


class MockBase:
    """Mock base object for ComponentBase properties in Enphase API tests."""

    def __init__(self):
        """Initialise MockBase with default config."""
        self.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.config = {}

    def get_arg(self, key, default=None, **kwargs):
        """Return config value or default."""
        return self.config.get(key, default)


class MockEnphaseAPI(EnphaseAPI):
    """Mock EnphaseAPI that avoids ComponentBase construction and real HTTP."""

    def __init__(self):
        """Set up the mock without calling ComponentBase.__init__."""
        self.prefix = "predbat"
        self.base = MockBase()
        self.local_tz = pytz.timezone("Europe/London")
        self.storage = None
        self.api_started = False
        self.initialize(username="user@example.com", password="secret")

        # Test instrumentation
        self.http_responses = {}  # path -> dict(status, json_data, text_data)
        self.request_log = []
        self.dashboard_items = {}
        self.mock_ha_states = {}
        self.args_set = {}

    def log(self, message):
        """Swallow log output in tests."""
        pass

    def record_api_call(self, *args, **kwargs):
        """Swallow telemetry in tests."""
        pass

    def update_success_timestamp(self):
        """Swallow health-tracking in tests."""
        pass

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Record dashboard items instead of publishing to HA."""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes, "app": app}

    def get_state_wrapper(self, entity_id, default=None):
        """Return a mocked HA state."""
        return self.mock_ha_states.get(entity_id, default)

    def set_arg(self, key, value):
        """Record args set by automatic_config."""
        self.args_set[key] = value

    def set_http_response(self, path, status=200, json_data=None, text_data=None):
        """Prime a canned HTTP response for a URL path."""
        self.http_responses[path] = {"status": status, "json_data": json_data, "text_data": text_data}

    async def request_raw(self, method, url, headers=None, data=None, json_body=None, params=None):
        """Return canned responses instead of performing HTTP."""
        path = url.split("enphaseenergy.com", 1)[-1].split("?")[0]
        self.request_log.append({"method": method, "path": path, "json": json_body, "data": data})
        response = self.http_responses.get(path, {"status": 404, "json_data": None, "text_data": "not found"})
        return response["status"], response["json_data"], response.get("text_data") or "", {}


def test_initialize_defaults():
    """initialize() must set all state fields with correct defaults."""
    api = MockEnphaseAPI()
    assert api.username == "user@example.com"
    assert api.password == "secret"
    assert api.site_id is None
    assert api.automatic is False
    assert api.sites == []
    assert api.battery_status == {}
    assert api.schedules == {}
    assert api.data_age == {}
    assert api.login_reject_count == 0


def test_needs_refresh():
    """_needs_refresh returns True when data is absent or stale, False when fresh."""
    api = MockEnphaseAPI()
    assert api._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS) is True
    api.data_age["battery_status"] = datetime.now(timezone.utc)
    assert api._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS) is False
    api.data_age["battery_status"] = datetime.now(timezone.utc) - timedelta(minutes=ENPHASE_REFRESH_SETTINGS + 1)
    assert api._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS) is True


def test_is_alive():
    """is_alive requires api_started and at least one discovered site."""
    api = MockEnphaseAPI()
    assert not api.is_alive()
    api.api_started = True
    assert not api.is_alive()
    api.sites = [{"site_id": "12345"}]
    assert api.is_alive()


def run_enphase_api_tests(my_predbat):
    """Run all Enphase API tests, returning 0 on success."""
    test_initialize_defaults()
    test_needs_refresh()
    test_is_alive()
    print("**** Enphase API tests passed ****")
    return 0
```

- [ ] **Step 2: Register the test and run it to verify failure**

In `apps/predbat/unit_test.py`, next to the fox imports (~line 107) add:

```python
from tests.test_enphase_api import run_enphase_api_tests
```

and in the registry list next to `("fox_api", ...)` (~line 283) add:

```python
        ("enphase_api", run_enphase_api_tests, "Enphase API tests", False),
```

Run: `cd coverage && ./run_all --test enphase_api > /tmp/enphase_t1.txt 2>&1; grep -iE "error|fail|passed" /tmp/enphase_t1.txt`
Expected: FAIL with `ModuleNotFoundError: No module named 'enphase'`.

- [ ] **Step 3: Create `apps/predbat/enphase.py` skeleton**

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Enphase Enlighten cloud component
#
# Talks to the unofficial Enphase Enlighten web-app API (the same endpoints the
# Enlighten web/mobile apps use). There is no official API with battery control.
# Reference behaviour derived from https://github.com/barneyonline/ha-enphase-energy
# -----------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
import asyncio
import random

import aiohttp

from component_base import ComponentBase
from utils import OPTIONS_TIME_FULL  # verify import location: grep "OPTIONS_TIME_FULL" apps/predbat/*.py — fox.py imports it; reuse the same source module

BASE_URL = "https://enlighten.enphaseenergy.com"
LOGIN_PATH = "/login/login.json"
SELF_TOKEN_PATH = "/users/self/token"
SITE_SEARCH_PATH = "/app-api/search_sites.json"
BATTERY_CONFIG_BASE = "/service/batteryConfig/api/v1"

# Refresh ages in minutes for each data category
ENPHASE_REFRESH_STATIC = 24 * 60
ENPHASE_REFRESH_SETTINGS = 5
ENPHASE_REFRESH_ENERGY = 15
ENPHASE_REFRESH_POWER = 1

ENPHASE_CACHE_KEYS = ["sites", "battery_status", "battery_settings", "profile", "schedules", "site_settings", "lifetime_energy", "latest_power"]
ENPHASE_CACHE_VERSION = 1

# Battery profiles accepted by the profile endpoint
PROFILE_SELF_CONSUMPTION = "self-consumption"
PROFILE_COST_SAVINGS = "cost_savings"
PROFILE_BACKUP_ONLY = "backup_only"

# Schedule families
SCHEDULE_CHARGE = "CFG"  # charge from grid
SCHEDULE_EXPORT = "DTG"  # discharge to grid
SCHEDULE_FREEZE = "RBD"  # restrict battery discharge

ENPHASE_RETRIES = 5

# Browser mimicry - Enlighten rejects non-browser requests with 406/login walls
ENPHASE_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
BATTERY_UI_ORIGIN = "https://battery-profile-ui.enphaseenergy.com"


class EnphaseAPI(ComponentBase):
    """Enphase Enlighten cloud API client component."""

    def initialize(self, username, password, site_id=None, automatic=False, automatic_ignore_pv=False):
        """Initialise the Enphase API component state."""
        self.username = username
        self.password = password
        self.site_id = str(site_id) if site_id else None
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv

        # Auth state
        self.cookie_header = ""       # serialised cookie header for Enlighten
        self.eauth_token = None       # JWT from /users/self/token
        self.manager_token = None     # enlighten_manager_token_production cookie JWT
        self.xsrf_token = None
        self.user_id = None           # decoded from JWT, needed by BatteryConfig
        self.token_expires_at = None

        # Login guard rails (avoid Enphase account lockout)
        self.login_last_success = None      # datetime of last successful login
        self.login_cooldown_until = None    # datetime before which logins are banned
        self.login_reject_count = 0         # consecutive rejected logins

        # Cloud data
        self.sites = []
        self.battery_status = {}
        self.battery_settings = {}
        self.profile = {}
        self.schedules = {}
        self.site_settings = {}
        self.lifetime_energy = {}
        self.latest_power = {}

        # Local (HA-side) schedule model, written by events, applied on write switch
        self.local_schedule = {}

        # Derived power state: previous cumulative kWh samples per channel
        self.prev_energy_sample = {}

        # Pending writes awaiting cloud settle confirmation
        self.pending_writes = {}

        # BatteryConfig header variant: "primary" (e-auth-token + requestid) or
        # "cookie_eauth" fallback (cookie + XHR header) needed on some regions/firmware
        self.battery_config_variant = "primary"

        # Age (datetime of last update) per cached data category
        self.data_age = {}
        self.failures_total = 0
        self.requests_today = 0
        self.last_midnight_utc = None

    def is_alive(self):
        """Return True when the component has started and discovered a site."""
        return self.api_started and bool(self.sites)

    def _data_age_minutes(self, key):
        """Return the age in minutes of the in-memory data for a cache key, or None if unknown."""
        timestamp = self.data_age.get(key, None)
        if timestamp is None:
            return None
        return (datetime.now(timezone.utc) - timestamp).total_seconds() / 60.0

    def _needs_refresh(self, key, max_age_minutes):
        """Return True if the data for a cache key is missing or older than max_age_minutes."""
        age = self._data_age_minutes(key)
        return age is None or age >= max_age_minutes

    async def _save_cache(self, key, data):
        """Save data to storage under the enphase module and record its update time."""
        now = datetime.now(timezone.utc)
        self.data_age[key] = now
        if self.storage:
            await self.storage.save("enphase", key, data, format="json", expiry=now + timedelta(days=1))

    async def _load_cache(self, key):
        """Load cached data for a key from storage, recording its age. Returns None if absent."""
        if not self.storage:
            return None
        data = await self.storage.load("enphase", key)
        if data is None:
            return None
        age = await self.storage.age("enphase", key)
        if age is None:
            return None
        self.data_age[key] = datetime.now(timezone.utc) - timedelta(minutes=age)
        return data

    async def load_cached_data(self):
        """Restore cached cloud data from storage on startup to avoid re-polling after a reboot."""
        if not self.storage:
            return
        version = await self.storage.load("enphase", "cache_version")
        if version != ENPHASE_CACHE_VERSION:
            self.log("Enphase: Cache version changed, forcing full refresh")
            await self.storage.save("enphase", "cache_version", ENPHASE_CACHE_VERSION, format="json")
            return
        for key in ENPHASE_CACHE_KEYS:
            data = await self._load_cache(key)
            if data is not None:
                setattr(self, key, data)
        if self.sites:
            self.update_success_timestamp()

    async def run(self, seconds, first):
        """Main polling body, invoked every 60 seconds by ComponentBase."""
        if first:
            await self.load_cached_data()
        # Later tasks fill in: login, per-tier refresh, publishing
        return True
```

Note for the implementer: check where `OPTIONS_TIME_FULL` actually lives (`grep -rn "OPTIONS_TIME_FULL" apps/predbat/fox.py apps/predbat/utils.py apps/predbat/config.py`) and import from the same module fox.py uses. If `record_api_call` in fox is provided by ComponentBase/base, mirror the same call pattern (`grep -n "record_api_call" apps/predbat/fox.py apps/predbat/component_base.py`).

- [ ] **Step 4: Register the component and config schema**

`apps/predbat/components.py` — add import next to `from fox import FoxAPI` (line 35):

```python
from enphase import EnphaseAPI
```

Add to `COMPONENT_LIST` directly after the `"fox"` entry (line 242):

```python
    "enphase": {
        "class": EnphaseAPI,
        "name": "Enphase API",
        "event_filter": "predbat_enphase_",
        "args": {
            "username": {
                "required": True,
                "config": "enphase_username",
            },
            "password": {
                "required": True,
                "config": "enphase_password",
            },
            "site_id": {
                "required": False,
                "config": "enphase_site_id",
            },
            "automatic": {
                "required": False,
                "default": False,
                "config": "enphase_automatic",
            },
            "automatic_ignore_pv": {
                "required": False,
                "default": False,
                "config": "enphase_automatic_ignore_pv",
            },
        },
        "phase": 1,
    },
```

`apps/predbat/config.py` — add to `APPS_SCHEMA` after `"fox_token_hash"` (line 2202):

```python
    "enphase_username": {"type": "string", "empty": False},
    "enphase_password": {"type": "string", "empty": False},
    "enphase_site_id": {"type": "string", "empty": False},
    "enphase_automatic": {"type": "boolean"},
    "enphase_automatic_ignore_pv": {"type": "boolean"},
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd coverage && ./run_all --test enphase_api > /tmp/enphase_t1.txt 2>&1; grep -iE "error|fail|passed" /tmp/enphase_t1.txt`
Expected: `**** Enphase API tests passed ****`, exit success.

- [ ] **Step 6: Commit**

```bash
git add apps/predbat/enphase.py apps/predbat/components.py apps/predbat/config.py apps/predbat/tests/test_enphase_api.py apps/predbat/unit_test.py
git commit -m "feat: add Enphase cloud component skeleton and registration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Authentication — login flow, headers, guard rails

**Files:**

- Modify: `apps/predbat/enphase.py`
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: Task 1 skeleton (`request_raw` overridable seam, auth state fields).
- Produces:
    - `async login(self) -> bool` — full login chain; sets `cookie_header`, `eauth_token`, `manager_token`, `xsrf_token`, `user_id`, `token_expires_at`, `sites` (list of dicts with at least `site_id` and `name`); respects guard rails; returns True on success.
    - `login_allowed(self) -> bool` — guard-rail check.
    - `get_headers(self, family, write=False) -> dict` — `family` in `("site", "battery_config")`.
    - `async request_raw(self, method, url, headers=None, data=None, json_body=None, params=None) -> (status, json_data, text, cookies)` — the only method that touches aiohttp; overridden in tests.
    - Module function `decode_jwt_claims(token) -> dict` (unverified payload decode for `exp` and user id).

- [ ] **Step 1: Write failing tests**

Add to `test_enphase_api.py` (and call them from `run_enphase_api_tests`):

```python
def test_login_success():
    """Successful login mints tokens, extracts user id and discovers sites."""
    api = MockEnphaseAPI()
    # JWT with payload {"user_id": "9999", "exp": 4102444800} (header/sig irrelevant, unverified decode)
    jwt = "eyJhbGciOiJIUzI1NiJ9." + _b64({"user_id": "9999", "exp": 4102444800}) + ".sig"
    api.set_http_response("/login/login.json", 200, {"success": True, "session_id": "sess1"})
    api.set_http_response("/users/self/token", 200, {"token": jwt, "expires_at": 4102444800})
    api.set_http_response("/app-api/search_sites.json", 200, [{"site_id": 12345, "name": "Home"}])
    assert run_async(api.login()) is True
    assert api.eauth_token == jwt
    assert api.user_id == "9999"
    assert api.sites[0]["site_id"] == "12345"
    assert api.login_reject_count == 0


def test_login_mfa_rejected():
    """MFA-required accounts must fail with a fatal error, not retry."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 200, {"requires_mfa": True})
    assert run_async(api.login()) is False
    assert api.login_reject_count == 1
    assert api.login_cooldown_until is not None


def test_login_guard_rails():
    """Three consecutive rejections suspend login for 24 hours."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 401, None)
    for _ in range(3):
        api.login_cooldown_until = None  # expire cooldown to allow next attempt
        run_async(api.login())
    assert api.login_reject_count == 3
    remaining = (api.login_cooldown_until - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 23 * 3600
    # While suspended, login() refuses without making a request
    count = len(api.request_log)
    assert run_async(api.login()) is False
    assert len(api.request_log) == count


def test_login_reuse_window():
    """A login success within 30 seconds is reused, not repeated."""
    api = MockEnphaseAPI()
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    count = len(api.request_log)
    assert run_async(api.login()) is True
    assert len(api.request_log) == count


def test_get_headers_site():
    """Site-family headers carry cookie, tokens and browser mimicry."""
    api = MockEnphaseAPI()
    api.cookie_header = "a=b"
    api.eauth_token = "tok"
    api.xsrf_token = "xs"
    headers = api.get_headers("site")
    assert headers["Cookie"] == "a=b"
    assert headers["e-auth-token"] == "tok"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-CSRF-Token"] == "xs"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert "Mozilla" in headers["User-Agent"]


def test_get_headers_battery_config():
    """BatteryConfig headers use the battery-profile-ui origin, manager token bearer and user id."""
    api = MockEnphaseAPI()
    api.eauth_token = "etok"
    api.manager_token = "mtok"
    api.user_id = "9999"
    headers = api.get_headers("battery_config", write=True)
    assert headers["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert headers["Authorization"] == "Bearer mtok"
    assert headers["e-auth-token"] == "etok"
    assert headers["Username"] == "9999"
    assert "requestid" in headers
```

Add helper at top of the test file:

```python
import base64
import json as json_module


def _b64(payload):
    """Base64url-encode a dict as a JWT payload segment without padding."""
    raw = json_module.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd coverage && ./run_all --test enphase_api > /tmp/enphase_t2.txt 2>&1; grep -iE "error|fail|passed" /tmp/enphase_t2.txt`
Expected: FAIL — `AttributeError` (`login` / `get_headers` not defined).

- [ ] **Step 3: Implement auth in `enphase.py`**

```python
def decode_jwt_claims(token):
    """Decode the payload segment of a JWT without verifying the signature."""
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError):
        return {}
```

Methods on `EnphaseAPI`:

```python
    LOGIN_REUSE_SECONDS = 30
    LOGIN_COOLDOWN_SECONDS = 300
    LOGIN_SUSPEND_SECONDS = 24 * 3600
    LOGIN_MAX_REJECTS = 3

    def login_allowed(self):
        """Return True when a password login attempt is currently permitted by the guard rails."""
        if self.login_cooldown_until and datetime.now(timezone.utc) < self.login_cooldown_until:
            return False
        return True

    def _login_rejected(self, reason):
        """Record a rejected login and set the appropriate cooldown."""
        self.login_reject_count += 1
        if self.login_reject_count >= self.LOGIN_MAX_REJECTS:
            delay = self.LOGIN_SUSPEND_SECONDS
        else:
            delay = self.LOGIN_COOLDOWN_SECONDS
        self.login_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.log(f"Warn: Enphase: Login rejected ({reason}), cooling down for {delay} seconds (rejection {self.login_reject_count})")
        self.fatal_error_occurred(f"Enphase login rejected: {reason}")

    async def login(self):
        """Authenticate with Enlighten: password login, token mint, site discovery."""
        # Reuse a very recent successful login (coalesces concurrent 401 refreshes)
        if self.login_last_success and (datetime.now(timezone.utc) - self.login_last_success).total_seconds() < self.LOGIN_REUSE_SECONDS and self.eauth_token:
            return True
        if not self.login_allowed():
            self.log("Warn: Enphase: Login suppressed by cooldown after previous rejections")
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": ENPHASE_USER_AGENT,
            "Referer": BASE_URL + "/",
        }
        status, data, text, cookies = await self.request_raw("POST", BASE_URL + LOGIN_PATH, headers=headers, data={"user[email]": self.username, "user[password]": self.password})

        if status in (401, 403):
            self._login_rejected("invalid credentials")
            return False
        if isinstance(data, dict) and data.get("requires_mfa"):
            self._login_rejected("account requires MFA - disable MFA on the Enphase account to use this component")
            return False
        if isinstance(data, dict) and data.get("isBlocked"):
            self._login_rejected("account is blocked")
            return False
        if status != 200:
            # Includes "too many active sessions" responses - treat as rejection
            reason = "too many active sessions" if "session" in str(text).lower() else f"http status {status}"
            self._login_rejected(reason)
            return False

        # Persist cookies from the login (session cookie + manager token JWT)
        self._absorb_cookies(cookies)

        # Mint the e-auth/bearer token; Enlighten may rotate the session cookie here
        status, token_data, text, cookies = await self.request_raw("GET", BASE_URL + SELF_TOKEN_PATH, headers=self.get_headers("site"))
        self._absorb_cookies(cookies)
        if status == 200 and isinstance(token_data, dict):
            token = token_data.get("token") or token_data.get("auth_token") or token_data.get("access_token")
            if token:
                self.eauth_token = token
                claims = decode_jwt_claims(token)
                self.user_id = str(claims.get("user_id") or claims.get("userId") or claims.get("sub") or "") or None
                self.token_expires_at = token_data.get("expires_at") or token_data.get("expiresAt") or claims.get("exp")
        if not self.eauth_token:
            self._login_rejected("no auth token returned")
            return False

        # Discover sites
        status, sites_data, text, cookies = await self.request_raw("GET", BASE_URL + SITE_SEARCH_PATH, headers=self.get_headers("site"), params={"searchText": "", "favourite": "false"})
        sites = []
        if status == 200:
            entries = sites_data if isinstance(sites_data, list) else (sites_data or {}).get("sites", [])
            for entry in entries:
                sid = str(entry.get("site_id") or entry.get("id") or "")
                if sid and (not self.site_id or sid == self.site_id):
                    sites.append({"site_id": sid, "name": entry.get("name", sid)})
        if sites:
            self.sites = sites
            await self._save_cache("sites", sites)

        self.login_last_success = datetime.now(timezone.utc)
        self.login_reject_count = 0
        self.login_cooldown_until = None
        self.log(f"Enphase: Login successful, {len(self.sites)} site(s)")
        return True

    def _absorb_cookies(self, cookies):
        """Merge response cookies into the serialised cookie header and pick out special tokens."""
        if not cookies:
            return
        current = {}
        for part in self.cookie_header.split("; "):
            if "=" in part:
                name, value = part.split("=", 1)
                current[name] = value
        current.update(cookies)
        self.cookie_header = "; ".join(f"{k}={v}" for k, v in current.items() if v)
        self.manager_token = current.get("enlighten_manager_token_production", self.manager_token)
        self.xsrf_token = current.get("XSRF-TOKEN", current.get("BP-XSRF-Token", self.xsrf_token))

    def get_headers(self, family, write=False):
        """Build request headers for an endpoint family ('site' or 'battery_config')."""
        import uuid

        if family == "battery_config":
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Origin": BATTERY_UI_ORIGIN,
                "Referer": BATTERY_UI_ORIGIN + "/",
                "User-Agent": ENPHASE_USER_AGENT,
                "e-auth-token": self.eauth_token or "",
            }
            if self.battery_config_variant == "cookie_eauth":
                # Fallback variant needed on some regions/firmware: cookie-backed with XHR marker
                headers["X-Requested-With"] = "XMLHttpRequest"
                if self.cookie_header:
                    headers["Cookie"] = self.cookie_header
            else:
                headers["requestid"] = str(uuid.uuid4())
            bearer = self.manager_token or self.eauth_token
            if bearer:
                headers["Authorization"] = f"Bearer {bearer}"
            if self.user_id:
                headers["Username"] = self.user_id
            if write:
                headers["Content-Type"] = "application/json"
                if self.xsrf_token:
                    headers["X-XSRF-Token"] = self.xsrf_token
            return headers

        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": ENPHASE_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": BASE_URL + "/",
        }
        if self.cookie_header:
            headers["Cookie"] = self.cookie_header
        if self.eauth_token:
            headers["Authorization"] = f"Bearer {self.eauth_token}"
            headers["e-auth-token"] = self.eauth_token
        if self.xsrf_token:
            headers["X-CSRF-Token"] = self.xsrf_token
        return headers

    async def request_raw(self, method, url, headers=None, data=None, json_body=None, params=None):
        """Perform one HTTP request, returning (status, json_or_none, text, cookie_dict). Overridden in tests."""
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, data=data, json=json_body, params=params, timeout=aiohttp.ClientTimeout(total=60)) as response:
                text = await response.text()
                cookies = {key: morsel.value for key, morsel in response.cookies.items()}
                json_data = None
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    try:
                        json_data = await response.json(content_type=None)
                    except ValueError:
                        json_data = None
                return response.status, json_data, text, cookies
```

Note: `fatal_error_occurred` comes from ComponentBase (`component_base.py:268`); `MockEnphaseAPI` must stub it — add `def fatal_error_occurred(self, message): pass` (with docstring) to the mock.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd coverage && ./run_all --test enphase_api > /tmp/enphase_t2.txt 2>&1; grep -iE "error|fail|passed" /tmp/enphase_t2.txt`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py
git commit -m "feat: Enphase Enlighten login flow with lockout guard rails

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Request helper with retries and 401 re-login

**Files:**

- Modify: `apps/predbat/enphase.py`
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: `login()`, `get_headers()`, `request_raw()` from Task 2.
- Produces: `async request_json(self, method, path, family="site", json_body=None, data=None, params=None) -> data_or_None`. Behaviour contract: builds `BASE_URL + path`; on 401 performs one `login()` + one retry; detects HTML login walls (text starting `<!DOCTYPE`/`<html` on a JSON endpoint) as auth failure; retries transient errors (timeouts, 5xx, 429) up to `ENPHASE_RETRIES` with jittered sleep, honouring `Retry-After`; increments `requests_today`; calls `record_api_call`; returns parsed JSON or None on failure (sets `self.last_error_status`).

- [ ] **Step 1: Write failing tests**

```python
def test_request_json_success():
    """request_json returns parsed JSON and counts the request."""
    api = MockEnphaseAPI()
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, {"storages": []})
    result = run_async(api.request_json("GET", "/pv/settings/12345/battery_status.json"))
    assert result == {"storages": []}
    assert api.requests_today == 1


def test_request_json_401_relogin():
    """A 401 triggers one re-login and one retry."""
    api = MockEnphaseAPI()
    api.eauth_token = "expired"
    calls = {"n": 0}

    async def fake_raw(method, url, headers=None, data=None, json_body=None, params=None):
        """Return 401 once then 200, and 200 for the login chain."""
        path = url.split("enphaseenergy.com", 1)[-1].split("?")[0]
        api.request_log.append({"method": method, "path": path})
        if path == "/login/login.json":
            return 200, {"success": True, "session_id": "s"}, "", {}
        if path == "/users/self/token":
            return 200, {"token": "newtok"}, "", {}
        if path == "/app-api/search_sites.json":
            return 200, [{"site_id": 12345, "name": "Home"}], "", {}
        calls["n"] += 1
        if calls["n"] == 1:
            return 401, None, "", {}
        return 200, {"ok": True}, "", {}

    api.request_raw = fake_raw
    result = run_async(api.request_json("GET", "/some/data.json"))
    assert result == {"ok": True}
    assert api.eauth_token == "newtok"


def test_request_json_login_wall():
    """An HTML body on a JSON endpoint is treated as auth failure, not a crash."""
    api = MockEnphaseAPI()
    api.login_cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)  # block re-login
    api.set_http_response("/some/data.json", 200, None, text_data="<!DOCTYPE html><html>login</html>")
    result = run_async(api.request_json("GET", "/some/data.json"))
    assert result is None


def test_battery_config_variant_fallback():
    """A BatteryConfig auth failure switches header variant before re-logging in."""
    api = MockEnphaseAPI()
    api.eauth_token = "tok"
    calls = {"n": 0}

    async def fake_raw(method, url, headers=None, data=None, json_body=None, params=None):
        """Reject the primary variant once, accept the cookie variant."""
        calls["n"] += 1
        if "requestid" in (headers or {}):
            return 401, None, "", {}
        return 200, {"ok": True}, "", {}

    api.request_raw = fake_raw
    result = run_async(api.request_json("GET", "/service/batteryConfig/api/v1/profile/12345", family="battery_config"))
    assert result == {"ok": True}
    assert api.battery_config_variant == "cookie_eauth"
    assert calls["n"] == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `cd coverage && ./run_all --test enphase_api > /tmp/enphase_t3.txt 2>&1; grep -iE "error|fail|passed" /tmp/enphase_t3.txt`
Expected: FAIL — `request_json` not defined.

- [ ] **Step 3: Implement `request_json`**

```python
    def _is_login_wall(self, json_data, text):
        """Return True when a JSON endpoint answered with an HTML login page."""
        if json_data is not None:
            return False
        stripped = (text or "").lstrip().lower()
        return stripped.startswith("<!doctype") or stripped.startswith("<html")

    async def request_json(self, method, path, family="site", json_body=None, data=None, params=None):
        """Perform an authenticated JSON request with retries and single 401 re-login."""
        url = BASE_URL + path
        relogin_done = False
        self.last_error_status = None
        for retry in range(ENPHASE_RETRIES):
            headers = self.get_headers(family, write=(method != "GET"))
            try:
                status, json_data, text, cookies = await self.request_raw(method, url, headers=headers, data=data, json_body=json_body, params=params)
            except (asyncio.TimeoutError, aiohttp.ClientError) as error:
                self.log(f"Warn: Enphase: Request error on {path}: {error}")
                await asyncio.sleep(1 + retry * random.random() * 5)
                continue
            self.requests_today += 1
            self.record_api_call("enphase", path, status)

            auth_failed = status in (401, 403) or self._is_login_wall(json_data, text)
            if auth_failed:
                if family == "battery_config" and self.battery_config_variant == "primary":
                    # Some regions/firmware reject the primary BatteryConfig header shape;
                    # switch to the cookie-backed fallback variant before burning a re-login
                    self.log("Enphase: BatteryConfig auth failed, switching to cookie header variant")
                    self.battery_config_variant = "cookie_eauth"
                    continue
                if relogin_done or not await self.login():
                    self.last_error_status = status
                    self.failures_total += 1
                    return None
                relogin_done = True
                continue
            if status == 429 or status >= 500:
                # Honour Retry-After when present in a headers dict returned via cookies param is not possible;
                # sleep with jittered backoff instead
                await asyncio.sleep(min(30, (retry + 1) * (2 + random.random() * 3)))
                continue
            if status != 200:
                self.log(f"Warn: Enphase: HTTP {status} on {path}")
                self.last_error_status = status
                self.failures_total += 1
                return None
            self.update_success_timestamp()
            return json_data
        self.failures_total += 1
        return None
```

Note: if `record_api_call` is not available on the mock, it is already stubbed in Task 1's `MockEnphaseAPI`. Verify the real method exists in the codebase (`grep -n "def record_api_call" apps/predbat/*.py`) and match its signature; fox calls `record_api_call("fox", ...)` — copy the exact argument shape fox uses.

- [ ] **Step 4: Run tests to verify pass, then commit**

Run: `cd coverage && ./run_all --test enphase_api > /tmp/enphase_t3.txt 2>&1; grep -iE "error|fail|passed" /tmp/enphase_t3.txt`
Expected: PASS.

```bash
git add apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py
git commit -m "feat: Enphase request helper with retries and 401 re-login

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Cloud reads, parsers, and the run() polling loop

**Files:**

- Modify: `apps/predbat/enphase.py`
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: `request_json()` (Task 3), cache helpers (Task 1).
- Produces:
    - `async get_battery_status(site_id)` → stores normalised dict in `self.battery_status[site_id]`: `{"soc_percent": float, "available_energy": float, "max_capacity": float, "max_power_kw": float, "batteries": [...], "status": str, "profile_label": str}` from `GET /pv/settings/<site>/battery_status.json` (site fields `current_charge`, `available_energy`, `max_capacity`, `max_power`; per-battery list under `storages`; aggregate SOC = capacity-weighted `available_energy/max_capacity*100`, fallback site `current_charge`).
    - `async get_lifetime_energy(site_id)` → stores raw payload; module function `energy_today(payload, channel)` returns today's kWh (last element of the `channel` array; today's index = days between `start_date` and `last_report_date`, defensively the last element).
    - `async get_latest_power(site_id)` → stores `{"watts": float, "time": ts}` from `GET /app-api/<site>/get_latest_power` (`latest_power.value`, `latest_power.time`; timestamps may be seconds or milliseconds — treat > 10^12 as ms).
    - `async get_profile(site_id)` → `GET {BATTERY_CONFIG_BASE}/profile/<site>?source=enho&userId=<uid>` (family `battery_config`) → stores `{"profile": str, "reserve": int}` (keys `profile`, `batteryBackupPercentage`).
    - `async get_battery_settings(site_id)` → `GET {BATTERY_CONFIG_BASE}/batterySettings/<site>?source=enlm` → stores `chargeFromGrid`, `veryLowSoc`, `veryLowSocMin`, `veryLowSocMax`.
    - `async get_schedules(site_id)` → `GET {BATTERY_CONFIG_BASE}/battery/sites/<site>/schedules` → stores per-family (`cfg`/`dtg`/`rbd`) dict: `{"id", "startTime", "endTime", "limit", "enabled", "supported"}` — parse each family's `details` list (first entry) plus control flags (`scheduleSupported`, `forceScheduleSupported`).
    - `dtg_supported(site_id) -> bool` — from schedules control flags / site settings.
    - `run(seconds, first)` extended: ensures login when `eauth_token` is None; refresh tiers — sites daily (`ENPHASE_REFRESH_STATIC`), battery_status/profile/settings/schedules every `ENPHASE_REFRESH_SETTINGS`, lifetime_energy every `ENPHASE_REFRESH_ENERGY`, latest_power every `ENPHASE_REFRESH_POWER`; midnight counter reset; each successful fetch `_save_cache`d.

- [ ] **Step 1: Write failing tests** — canned payloads through `set_http_response`, e.g.:

```python
BATTERY_STATUS_PAYLOAD = {
    "current_charge": 55,
    "available_energy": 5.5,
    "max_capacity": 10.0,
    "max_power": 3.84,
    "storages": [
        {"id": 1, "serial_num": "B1", "current_charge": 50, "available_energy": 2.5, "max_capacity": 5.0, "status": "normal"},
        {"id": 2, "serial_num": "B2", "current_charge": 60, "available_energy": 3.0, "max_capacity": 5.0, "status": "normal"},
    ],
}


def test_get_battery_status():
    """battery_status parses site totals and capacity-weighted SOC."""
    api = MockEnphaseAPI()
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, BATTERY_STATUS_PAYLOAD)
    run_async(api.get_battery_status("12345"))
    status = api.battery_status["12345"]
    assert status["max_capacity"] == 10.0
    assert status["soc_percent"] == 55.0  # (2.5+3.0)/(5+5)*100
    assert status["max_power_kw"] == 3.84


def test_energy_today():
    """energy_today returns the final (today's) entry of a channel array."""
    payload = {"start_date": "2026-07-09", "last_report_date": "2026-07-11", "production": [10.0, 12.0, 3.5], "consumption": [8.0, 9.0, 2.2]}
    from enphase import energy_today

    assert energy_today(payload, "production") == 3.5
    assert energy_today(payload, "consumption") == 2.2
    assert energy_today(payload, "import") == 0.0  # missing channel -> 0


def test_get_schedules_parses_families():
    """Schedules read stores cfg/dtg/rbd entries and dtg support flag."""
    api = MockEnphaseAPI()
    payload = {
        "cfg": {"scheduleSupported": True, "details": [{"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "isEnabled": True}]},
        "dtg": {"scheduleSupported": False, "details": []},
        "rbd": {"scheduleSupported": True, "details": []},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, payload)
    run_async(api.get_schedules("12345"))
    cfg = api.schedules["12345"]["cfg"]
    assert cfg["id"] == "u1" and cfg["limit"] == 90 and cfg["enabled"] is True
    assert api.dtg_supported("12345") is False


def test_run_first_polls_all_tiers():
    """First run() logs in, fetches every tier and publishes."""
    api = MockEnphaseAPI()
    # prime auth short-circuit
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, BATTERY_STATUS_PAYLOAD)
    api.set_http_response("/pv/systems/12345/lifetime_energy", 200, {"production": [1.0], "consumption": [1.0], "import": [0.5], "export": [0.2], "charge": [0.1], "discharge": [0.1], "start_date": "2026-07-11"})
    api.set_http_response("/app-api/12345/get_latest_power", 200, {"latest_power": {"value": 450, "units": "w", "time": 1760000000}})
    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {"profile": "self-consumption", "batteryBackupPercentage": 20})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {"chargeFromGrid": True, "veryLowSoc": 10, "veryLowSocMin": 5, "veryLowSocMax": 25})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    result = run_async(api.run(0, True))
    assert result
    assert api.battery_status["12345"]["soc_percent"] == 55.0
    assert api.profile["12345"]["reserve"] == 20
    assert api.latest_power["12345"]["watts"] == 450
```

- [ ] **Step 2: Run to verify failure** (same command pattern; expect AttributeError/ImportError).

- [ ] **Step 3: Implement** the six read methods, `energy_today`, `dtg_supported` and the extended `run()`:

```python
def energy_today(payload, channel):
    """Return today's kWh for a lifetime_energy channel (last array entry), 0.0 if absent."""
    values = (payload or {}).get(channel) or []
    if not values:
        return 0.0
    try:
        return float(values[-1] or 0.0)
    except (TypeError, ValueError):
        return 0.0
```

`run()` body after the Task 1 `load_cached_data` block:

```python
        # Midnight counter reset
        current_midnight = self.midnight_utc
        if self.last_midnight_utc is not None and self.last_midnight_utc != current_midnight:
            self.log(f"Enphase: Midnight reset - requests_today: {self.requests_today}")
            self.requests_today = 0
        self.last_midnight_utc = current_midnight

        # Ensure we are logged in (guard rails inside login())
        if not self.eauth_token:
            if not await self.login():
                return bool(self.sites)  # stay alive on cached data if we have it

        if first or self._needs_refresh("sites", ENPHASE_REFRESH_STATIC):
            if not await self.login():
                return bool(self.sites)

        for site in self.sites:
            site_id = site["site_id"]
            if self._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS):
                await self.get_battery_status(site_id)
                await self.get_profile(site_id)
                await self.get_battery_settings(site_id)
                await self.get_schedules(site_id)
            if self._needs_refresh("lifetime_energy", ENPHASE_REFRESH_ENERGY):
                await self.get_lifetime_energy(site_id)
            if self._needs_refresh("latest_power", ENPHASE_REFRESH_POWER):
                await self.get_latest_power(site_id)
            await self.publish_data(site_id)          # Task 5
            await self.publish_schedule_settings_ha(site_id)  # Task 6 (no-op until then: guard with hasattr or add stub now)
        return True
```

Add stub methods now so run() is complete (filled in Tasks 5/6):

```python
    async def publish_data(self, site_id):
        """Publish monitoring sensors for a site (implemented in a later task)."""
        pass

    async def publish_schedule_settings_ha(self, site_id):
        """Publish schedule control entities for a site (implemented in a later task)."""
        pass
```

Each `get_*` method follows this shape (battery_status shown; the others differ only in path/family/parse):

```python
    async def get_battery_status(self, site_id):
        """Fetch and normalise battery SOC/capacity/power for a site."""
        data = await self.request_json("GET", f"/pv/settings/{site_id}/battery_status.json")
        if data is None:
            return None
        batteries = data.get("storages") or []
        total_capacity = sum(float(b.get("max_capacity", 0) or 0) for b in batteries)
        total_available = sum(float(b.get("available_energy", 0) or 0) for b in batteries)
        if total_capacity > 0:
            soc_percent = round(total_available / total_capacity * 100.0, 1)
        else:
            soc_percent = float(data.get("current_charge", 0) or 0)
        self.battery_status[site_id] = {
            "soc_percent": soc_percent,
            "available_energy": float(data.get("available_energy", total_available) or 0),
            "max_capacity": float(data.get("max_capacity", total_capacity) or 0),
            "max_power_kw": float(data.get("max_power", 0) or 0),
            "status": str(data.get("status", "")),
            "batteries": batteries,
        }
        await self._save_cache("battery_status", self.battery_status)
        return self.battery_status[site_id]
```

Profile GET uses `family="battery_config"` and params `{"source": "enho", "userId": self.user_id}`; batterySettings GET params `{"source": "enlm"}`.

- [ ] **Step 4: Run tests to verify pass, then commit**

```bash
git add apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py
git commit -m "feat: Enphase cloud reads, parsers and polling loop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Monitoring sensor publishing (incl. derived power)

**Files:**

- Modify: `apps/predbat/enphase.py` (replace `publish_data` stub)
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: normalised data stores from Task 4.
- Produces: `publish_data(site_id)` creating (entity ids all prefixed `sensor.{prefix}_enphase_{site_id}_`): `soc_percent` (%), `soc_kw` (kWh available), `battery_capacity` (kWh), `battery_rate_max` (W = `max_power_kw*1000`), `battery_reserve` (%), `battery_reserve_min` (% = `veryLowSocMin` fallback 5), `battery_status`, `battery_profile`, `pv_today`/`load_today`/`import_today`/`export_today`/`battery_charge_today`/`battery_discharge_today` (kWh, `state_class: total_increasing`, `device_class: energy`), `load_power` (W from latest_power), `pv_power`/`grid_power`/`battery_power` (W, derived). Also module function `derive_power(prev_sample, new_kwh, now_utc) -> (watts, new_sample)` where a sample is `(kwh, datetime)`; watts = `(new_kwh - prev_kwh) * 1000 / hours_elapsed`, clamped to 0 when the delta is negative (daily reset) or the window is < 60 seconds.
- Sign conventions: `grid_power` positive = import (derive from `import` minus `export` deltas); `battery_power` positive = discharge (`discharge` minus `charge` deltas); `pv_power` from `production` delta.

- [ ] **Step 1: Write failing tests**

```python
def test_derive_power():
    """derive_power converts kWh deltas over elapsed time into watts."""
    from enphase import derive_power

    now = datetime.now(timezone.utc)
    prev = (1.0, now - timedelta(minutes=5))
    watts, sample = derive_power(prev, 1.1, now)
    assert abs(watts - 1200.0) < 1.0  # 0.1 kWh in 5 min = 1.2 kW
    assert sample == (1.1, now)
    # Negative delta (midnight reset) clamps to zero
    watts, _ = derive_power((5.0, now - timedelta(minutes=5)), 0.0, now)
    assert watts == 0.0
    # No previous sample yields zero
    watts, _ = derive_power(None, 2.0, now)
    assert watts == 0.0


def test_publish_data_sensors():
    """publish_data creates the full monitoring sensor set."""
    api = MockEnphaseAPI()
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSoc": 10, "veryLowSocMin": 5, "veryLowSocMax": 25}
    api.lifetime_energy["12345"] = {"production": [3.5], "consumption": [2.2], "import": [1.0], "export": [0.4], "charge": [0.8], "discharge": [0.6]}
    api.latest_power["12345"] = {"watts": 450.0, "time": 1760000000}
    run_async(api.publish_data("12345"))
    items = api.dashboard_items
    assert items["sensor.predbat_enphase_12345_soc_percent"]["state"] == 55.0
    assert items["sensor.predbat_enphase_12345_battery_capacity"]["state"] == 10.0
    assert items["sensor.predbat_enphase_12345_battery_rate_max"]["state"] == 3840.0
    assert items["sensor.predbat_enphase_12345_pv_today"]["state"] == 3.5
    assert items["sensor.predbat_enphase_12345_load_today"]["state"] == 2.2
    assert items["sensor.predbat_enphase_12345_import_today"]["state"] == 1.0
    assert items["sensor.predbat_enphase_12345_export_today"]["state"] == 0.4
    assert items["sensor.predbat_enphase_12345_load_power"]["state"] == 450.0
    assert items["sensor.predbat_enphase_12345_battery_reserve_min"]["state"] == 5
    assert "sensor.predbat_enphase_12345_pv_power" in items
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Follow fox `publish_data` (fox.py:1744) attribute conventions: energy sensors get `{"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing", "friendly_name": ..., "icon": "mdi:..."}`; power sensors `{"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", ...}`; percentages `{"unit_of_measurement": "%", ...}`. `derive_power`:

```python
def derive_power(prev_sample, new_kwh, now_utc):
    """Estimate average watts from the change in a cumulative kWh counter since the previous sample."""
    new_sample = (new_kwh, now_utc)
    if not prev_sample:
        return 0.0, new_sample
    prev_kwh, prev_time = prev_sample
    seconds = (now_utc - prev_time).total_seconds()
    if seconds < 60:
        return 0.0, prev_sample
    delta = new_kwh - prev_kwh
    if delta < 0:
        return 0.0, new_sample
    return round(delta * 1000.0 * 3600.0 / seconds, 1), new_sample
```

In `publish_data`, keep per-site previous samples in `self.prev_energy_sample[site_id][channel]` and compute: `pv_power` from `production`; `grid_power` = import-watts − export-watts; `battery_power` = discharge-watts − charge-watts.

- [ ] **Step 4: Run tests to verify pass, then commit**

```bash
git add apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py
git commit -m "feat: Enphase monitoring sensors with derived power estimates

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Control entities, local schedule model, HA events

**Files:**

- Modify: `apps/predbat/enphase.py` (replace `publish_schedule_settings_ha` stub; add `get_schedule_settings_ha`, event handlers)
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: `self.schedules`, `self.profile`, `dtg_supported()` (Task 4).
- Produces:
    - `publish_schedule_settings_ha(site_id)` — entities (pattern `{domain}.{prefix}_enphase_{site_id}_battery_schedule_...`):
        - `number ..._reserve` (min = `veryLowSocMin` fallback 5, max 100, step 1, %)
        - per direction `charge` and (only if `dtg_supported`) `export`: `select ..._{direction}_start_time` / `..._end_time` (options `OPTIONS_TIME_FULL`, value "HH:MM:SS"), `number ..._{direction}_soc` (min 5 max 100 step 1 %), `switch ..._{direction}_enable`, `switch ..._{direction}_write` (always published "off")
        - `switch ..._freeze_enable` plus freeze times reuse the charge window (rbd written during apply when freeze enabled)
    - `get_schedule_settings_ha(site_id)` — reads entity states back from HA into `self.local_schedule[site_id]` = `{"reserve": int, "charge": {"start_time", "end_time", "soc", "enable"}, "export": {...}, "freeze": {"enable"}}`.
    - `select_event(entity_id, value)`, `number_event(entity_id, value)`, `switch_event(entity_id, service)` — update `local_schedule`; a `turn_on` of a `_write` switch calls `apply_battery_schedule(site_id)` (Task 7 — stub `async def apply_battery_schedule(self, site_id)` now, docstring + `pass`). Entity id parsing: strip `{domain}.{prefix}_enphase_`, split off `site_id`, remainder names the attribute. `switch` services: `turn_on`/`turn_off`/`toggle` (see fox `apply_service_to_toggle`, fox.py:2004).
    - Times: HA selects hold "HH:MM:SS" (`OPTIONS_TIME_FULL`); Enphase wants "HH:MM" — module functions `ha_time_to_enphase(value)` (`value[:5]`) and `enphase_time_to_ha(value)` (`value + ":00"`).

- [ ] **Step 1: Write failing tests**

```python
def test_publish_schedule_entities():
    """Control entities are published for charge, and export only when dtg supported."""
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": False}, "rbd": {"supported": True}}
    api.battery_settings["12345"] = {"veryLowSocMin": 5}
    run_async(api.publish_schedule_settings_ha("12345"))
    items = api.dashboard_items
    assert "select.predbat_enphase_12345_battery_schedule_charge_start_time" in items
    assert "number.predbat_enphase_12345_battery_schedule_charge_soc" in items
    assert "switch.predbat_enphase_12345_battery_schedule_charge_write" in items
    assert "number.predbat_enphase_12345_battery_schedule_reserve" in items
    assert "select.predbat_enphase_12345_battery_schedule_export_start_time" not in items


def test_event_handlers_update_local_schedule():
    """select/number/switch events mutate the local schedule model."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    run_async(api.select_event("select.predbat_enphase_12345_battery_schedule_charge_start_time", "02:30:00"))
    assert api.local_schedule["12345"]["charge"]["start_time"] == "02:30:00"
    run_async(api.number_event("number.predbat_enphase_12345_battery_schedule_charge_soc", 85))
    assert api.local_schedule["12345"]["charge"]["soc"] == 85
    run_async(api.switch_event("switch.predbat_enphase_12345_battery_schedule_charge_enable", "turn_on"))
    assert api.local_schedule["12345"]["charge"]["enable"] is True


def test_write_switch_triggers_apply():
    """Turning on the write switch calls apply_battery_schedule for the site."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    applied = []

    async def fake_apply(site_id):
        """Record the applied site."""
        applied.append(site_id)

    api.apply_battery_schedule = fake_apply
    run_async(api.switch_event("switch.predbat_enphase_12345_battery_schedule_charge_write", "turn_on"))
    assert applied == ["12345"]
```

Note: check how fox's `switch_event` schedules async work from a sync event callback (`grep -n "def switch_event" -A 15 apps/predbat/fox.py`) — it is a `async def` called by the Components dispatcher, or it uses `create_task`. Match fox exactly (if fox's event handlers are async, make these async and adapt the tests with `run_async`).

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Entity attribute dicts follow fox.py:1290 conventions (friendly names "Enphase {site} Battery Schedule ..."). Core code:

```python
def ha_time_to_enphase(value):
    """Convert an HA 'HH:MM:SS' option time to Enphase 'HH:MM' format."""
    return str(value)[:5]


def enphase_time_to_ha(value):
    """Convert an Enphase 'HH:MM' time to the HA 'HH:MM:SS' option format."""
    text = str(value or "00:00")[:5]
    return text + ":00"
```

Methods on `EnphaseAPI`:

```python
    def _default_local_schedule(self):
        """Return an empty local schedule model."""
        return {"reserve": 0, "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}

    async def publish_schedule_settings_ha(self, site_id):
        """Publish the schedule control entities for a site."""
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        reserve_min = int(self.battery_settings.get(site_id, {}).get("veryLowSocMin", 5) or 5)
        base_name = f"{self.prefix}_enphase_{site_id}_battery_schedule"

        self.dashboard_item(
            f"number.{base_name}_reserve",
            state=local.get("reserve", 0),
            attributes={"min": reserve_min, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Enphase {site_id} Battery Schedule Reserve", "icon": "mdi:gauge"},
            app="enphase",
        )

        directions = ["charge"]
        if self.dtg_supported(site_id):
            directions.append("export")
        for direction in directions:
            window = local.get(direction, {})
            for attribute in ["start_time", "end_time"]:
                value = window.get(attribute, "00:00:00")
                if value not in OPTIONS_TIME_FULL:
                    value = "00:00:00"
                self.dashboard_item(
                    f"select.{base_name}_{direction}_{attribute}",
                    state=value,
                    attributes={"options": OPTIONS_TIME_FULL, "friendly_name": f"Enphase {site_id} Battery Schedule {direction.capitalize()} {attribute.replace('_', ' ').capitalize()}", "icon": "mdi:clock-outline"},
                    app="enphase",
                )
            self.dashboard_item(
                f"number.{base_name}_{direction}_soc",
                state=int(window.get("soc", 100 if direction == "charge" else reserve_min)),
                attributes={"min": 5, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Enphase {site_id} Battery Schedule {direction.capitalize()} Soc", "icon": "mdi:gauge"},
                app="enphase",
            )
            self.dashboard_item(
                f"switch.{base_name}_{direction}_enable",
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": f"Enphase {site_id} Battery Schedule {direction.capitalize()} Enable", "icon": "mdi:check-circle-outline"},
                app="enphase",
            )
            self.dashboard_item(
                f"switch.{base_name}_{direction}_write",
                state="off",
                attributes={"friendly_name": f"Enphase {site_id} Battery Schedule {direction.capitalize()} Write", "icon": "mdi:upload"},
                app="enphase",
            )
        self.dashboard_item(
            f"switch.{base_name}_freeze_enable",
            state="on" if local.get("freeze", {}).get("enable") else "off",
            attributes={"friendly_name": f"Enphase {site_id} Battery Schedule Freeze Enable", "icon": "mdi:snowflake"},
            app="enphase",
        )

    async def get_schedule_settings_ha(self, site_id):
        """Read the current schedule control entity states from HA into the local schedule model."""
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        base_name = f"{self.prefix}_enphase_{site_id}_battery_schedule"
        local["reserve"] = int(float(self.get_state_wrapper(f"number.{base_name}_reserve", local.get("reserve", 0)) or 0))
        for direction in ["charge", "export"]:
            window = local.setdefault(direction, {})
            for attribute in ["start_time", "end_time"]:
                value = self.get_state_wrapper(f"select.{base_name}_{direction}_{attribute}", window.get(attribute, "00:00:00"))
                if value in OPTIONS_TIME_FULL:
                    window[attribute] = value
            window["soc"] = int(float(self.get_state_wrapper(f"number.{base_name}_{direction}_soc", window.get("soc", 100)) or 0))
            window["enable"] = str(self.get_state_wrapper(f"switch.{base_name}_{direction}_enable", "on" if window.get("enable") else "off")).lower() == "on"
        local.setdefault("freeze", {})["enable"] = str(self.get_state_wrapper(f"switch.{base_name}_freeze_enable", "off")).lower() == "on"

    def _parse_entity(self, entity_id):
        """Split a published entity id into (site_id, attribute_name), or (None, None) if not ours."""
        try:
            name = entity_id.split(".", 1)[1]
        except IndexError:
            return None, None
        marker = f"{self.prefix}_enphase_"
        if not name.startswith(marker):
            return None, None
        remainder = name[len(marker):]
        for site in self.sites:
            site_id = site["site_id"]
            if remainder.startswith(site_id + "_"):
                return site_id, remainder[len(site_id) + 1:]
        return None, None

    def _toggle_to_bool(self, service, current):
        """Convert an HA switch service call into the resulting boolean state."""
        if service == "turn_on":
            return True
        if service == "turn_off":
            return False
        return not current

    async def select_event(self, entity_id, value):
        """Handle a select entity change routed from HA."""
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_"):]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        for direction in ["charge", "export"]:
            for time_key in ["start_time", "end_time"]:
                if field == f"{direction}_{time_key}" and value in OPTIONS_TIME_FULL:
                    local[direction][time_key] = value
        await self.publish_schedule_settings_ha(site_id)

    async def number_event(self, entity_id, value):
        """Handle a number entity change routed from HA."""
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_"):]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        if field == "reserve":
            local["reserve"] = int(float(value))
        for direction in ["charge", "export"]:
            if field == f"{direction}_soc":
                local[direction]["soc"] = int(float(value))
        await self.publish_schedule_settings_ha(site_id)

    async def switch_event(self, entity_id, service):
        """Handle a switch service call routed from HA."""
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_"):]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        if field == "freeze_enable":
            local["freeze"]["enable"] = self._toggle_to_bool(service, local["freeze"]["enable"])
        for direction in ["charge", "export"]:
            if field == f"{direction}_enable":
                local[direction]["enable"] = self._toggle_to_bool(service, local[direction]["enable"])
            if field == f"{direction}_write" and self._toggle_to_bool(service, False):
                await self.apply_battery_schedule(site_id)
        await self.publish_schedule_settings_ha(site_id)
```

These are async because ComponentBase declares async event handlers (`component_base.py:340/352/364`) and fox's are async too (`fox.py:1985/1994/2000`) — the Components dispatcher awaits them.

- [ ] **Step 4: Run tests to verify pass, then commit**

```bash
git add apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py
git commit -m "feat: Enphase schedule control entities and HA event handling

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Write path — apply_battery_schedule

**Files:**

- Modify: `apps/predbat/enphase.py`
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: `local_schedule` (Task 6), `schedules`/`profile`/`battery_settings` cloud state (Task 4), `request_json` (Task 3).
- Produces:
    - `async apply_battery_schedule(site_id)` — top-level diff-and-write:
    1. Desired state from `local_schedule[site_id]`.
    2. Reserve/profile: if desired reserve != cloud `profile["reserve"]`, `PUT {BATTERY_CONFIG_BASE}/profile/<site>` body `{"profile": <current or self-consumption>, "batteryBackupPercentage": reserve}` (family `battery_config`, params `{"source": "enho", "userId": self.user_id}`).
    3. Charge: if `charge.enable` — ensure `chargeFromGrid` true first (if false: `POST {BATTERY_CONFIG_BASE}/batterySettings/acceptDisclaimer/<site>` body `{"disclaimer-type": "itc"}` once — track `self.disclaimer_accepted`; then `PUT batterySettings` `{"chargeFromGrid": True}`), then `_write_schedule(site_id, "CFG", start, end, limit=charge.soc, enabled=True)`. If not enabled and a cloud cfg schedule is enabled → `_write_schedule(..., enabled=False)`.
    4. Export: same via `"DTG"` with `limit=export.soc`, only when `dtg_supported(site_id)`.
    5. Freeze: `freeze.enable` → `_write_schedule(site_id, "RBD", charge window times, limit=None, enabled=True)`; else disable if cloud-enabled.
    - `async _write_schedule(site_id, family, start_time_ha, end_time_ha, limit, enabled)` — converts "HH:MM:SS"→"HH:MM"; no-op when the cloud entry already matches (`schedules_equal`); update by id when the family has an existing schedule (`PUT .../schedules/<id>`), else create (`POST .../schedules`); payload `{"timezone": tz, "startTime": "HH:MM", "endTime": "HH:MM", "scheduleType": family, "days": [1,2,3,4,5,6,7], "limit": limit, "isEnabled": enabled}` (omit `limit` when None). Timezone from site settings if present else `str(self.local_tz)`.
    - Module function `schedules_equal(cloud_entry, start_hm, end_hm, limit, enabled) -> bool`.
    - Write-settle: after any write, mark `self.pending_writes[(site_id, family)] = desired`; re-fetch schedules; if the read does not yet reflect the write, keep pending (do not re-PUT) until it confirms or `ENPHASE_PENDING_TIMEOUT_MINUTES = 15` passes. `run()` clears confirmed/expired pendings each cycle.

- [ ] **Step 1: Write failing tests**

```python
def test_schedules_equal():
    """schedules_equal compares window, limit and enable state."""
    from enphase import schedules_equal

    cloud = {"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True}
    assert schedules_equal(cloud, "02:00", "05:00", 90, True)
    assert not schedules_equal(cloud, "02:00", "05:30", 90, True)
    assert not schedules_equal(cloud, "02:00", "05:00", 80, True)
    assert not schedules_equal(cloud, "02:00", "05:00", 90, False)
    assert not schedules_equal(None, "02:00", "05:00", 90, True)


def test_apply_charge_schedule_creates():
    """apply writes a CFG schedule via POST when none exists, enabling charge-from-grid first."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": False, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/12345", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["scheduleType"] == "CFG" and body["startTime"] == "02:00" and body["endTime"] == "05:00" and body["limit"] == 90 and body["isEnabled"] is True
    disclaimers = [r for r in api.request_log if "acceptDisclaimer" in r["path"]]
    assert len(disclaimers) == 1


def test_apply_updates_existing_by_id():
    """apply uses PUT /schedules/<id> when the family already has a schedule."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "01:00", "endTime": "04:00", "limit": 80, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules/u1", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": [{"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "isEnabled": True}]}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/schedules/u1")]
    assert len(puts) == 1
    # After the confirming re-read matches, no pending write remains
    assert ("12345", "CFG") not in api.pending_writes


def test_apply_no_change_no_write():
    """apply issues no schedule writes when cloud already matches the local schedule."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    run_async(api.apply_battery_schedule("12345"))
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT")]
    assert writes == []


def test_pending_write_suppresses_duplicate():
    """While a write is pending confirmation, apply does not re-issue the same PUT."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "01:00", "endTime": "04:00", "limit": 80, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    api.pending_writes[("12345", "CFG")] = {"start": "02:00", "end": "05:00", "limit": 90, "enabled": True, "time": datetime.now(timezone.utc)}
    run_async(api.apply_battery_schedule("12345"))
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT") and "/schedules" in r["path"]]
    assert writes == []
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** Add module constant `ENPHASE_PENDING_TIMEOUT_MINUTES = 15`. Core code:

```python
def schedules_equal(cloud_entry, start_hm, end_hm, limit, enabled):
    """Return True when a cloud schedule entry already matches the desired window/limit/enable state."""
    if not cloud_entry or "startTime" not in cloud_entry:
        # No cloud schedule: equal only when we want it disabled
        return not enabled
    if bool(cloud_entry.get("enabled")) != bool(enabled):
        return False
    if not enabled:
        return True  # both disabled - window/limit are irrelevant
    if str(cloud_entry.get("startTime", ""))[:5] != start_hm or str(cloud_entry.get("endTime", ""))[:5] != end_hm:
        return False
    if limit is not None and int(cloud_entry.get("limit", -1)) != int(limit):
        return False
    return True
```

Methods on `EnphaseAPI`:

```python
    def _site_timezone(self, site_id):
        """Return the IANA timezone to use for schedule writes."""
        timezone_name = self.site_settings.get(site_id, {}).get("timezone")
        return timezone_name or str(self.local_tz)

    def _pending_active(self, site_id, family):
        """Return True when a write for this family is still awaiting cloud confirmation."""
        pending = self.pending_writes.get((site_id, family))
        if not pending:
            return False
        age_minutes = (datetime.now(timezone.utc) - pending["time"]).total_seconds() / 60.0
        if age_minutes > ENPHASE_PENDING_TIMEOUT_MINUTES:
            self.log(f"Warn: Enphase: Pending {family} write for site {site_id} timed out after {ENPHASE_PENDING_TIMEOUT_MINUTES} minutes")
            del self.pending_writes[(site_id, family)]
            return False
        return True

    async def _write_schedule(self, site_id, family, start_time_ha, end_time_ha, limit, enabled):
        """Create/update one Enphase schedule family if it differs from the cloud state. Returns True if a write was issued."""
        start_hm = ha_time_to_enphase(start_time_ha)
        end_hm = ha_time_to_enphase(end_time_ha)
        family_key = family.lower()
        cloud_entry = self.schedules.get(site_id, {}).get(family_key, {})
        if schedules_equal(cloud_entry, start_hm, end_hm, limit, enabled):
            self.pending_writes.pop((site_id, family), None)  # confirmed
            return False
        if self._pending_active(site_id, family):
            return False  # a matching write is still settling; don't spam duplicates

        payload = {"timezone": self._site_timezone(site_id), "startTime": start_hm, "endTime": end_hm, "scheduleType": family, "days": [1, 2, 3, 4, 5, 6, 7], "isEnabled": bool(enabled)}
        if limit is not None:
            payload["limit"] = int(limit)
        schedule_id = cloud_entry.get("id")
        if schedule_id:
            self.log(f"Enphase: Updating {family} schedule {schedule_id} on site {site_id}: {start_hm}-{end_hm} limit={limit} enabled={enabled}")
            result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules/{schedule_id}", family="battery_config", json_body=payload)
        else:
            self.log(f"Enphase: Creating {family} schedule on site {site_id}: {start_hm}-{end_hm} limit={limit} enabled={enabled}")
            result = await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules", family="battery_config", json_body=payload)
        # Record as pending regardless of result - Enphase writes can 400 yet still land
        self.pending_writes[(site_id, family)] = {"start": start_hm, "end": end_hm, "limit": limit, "enabled": enabled, "time": datetime.now(timezone.utc)}
        return result is not None

    async def _ensure_charge_from_grid(self, site_id):
        """Enable the charge-from-grid setting, accepting the one-time ITC disclaimer first."""
        if self.battery_settings.get(site_id, {}).get("chargeFromGrid"):
            return
        self.log(f"Enphase: Enabling charge-from-grid on site {site_id}")
        await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/batterySettings/acceptDisclaimer/{site_id}", family="battery_config", json_body={"disclaimer-type": "itc"})
        await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", json_body={"chargeFromGrid": True})
        self.battery_settings.setdefault(site_id, {})["chargeFromGrid"] = True

    async def apply_battery_schedule(self, site_id):
        """Diff the local schedule model against the cloud and issue only the changed writes."""
        await self.get_schedule_settings_ha(site_id)
        local = self.local_schedule.get(site_id, self._default_local_schedule())
        wrote = False

        # Reserve via profile PUT, preserving the current profile name
        desired_reserve = int(local.get("reserve", 0))
        cloud = self.profile.get(site_id, {})
        if desired_reserve and desired_reserve != int(cloud.get("reserve", -1)):
            profile_name = cloud.get("profile") or PROFILE_SELF_CONSUMPTION
            self.log(f"Enphase: Setting reserve to {desired_reserve}% (profile {profile_name}) on site {site_id}")
            await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/profile/{site_id}", family="battery_config", params={"source": "enho", "userId": self.user_id}, json_body={"profile": profile_name, "batteryBackupPercentage": desired_reserve})
            wrote = True

        # Forced charge window (CFG)
        charge = local.get("charge", {})
        if charge.get("enable"):
            await self._ensure_charge_from_grid(site_id)
        wrote |= await self._write_schedule(site_id, SCHEDULE_CHARGE, charge.get("start_time", "00:00:00"), charge.get("end_time", "00:00:00"), charge.get("soc", 100), charge.get("enable", False))

        # Forced export window (DTG), only where supported
        export = local.get("export", {})
        if self.dtg_supported(site_id):
            wrote |= await self._write_schedule(site_id, SCHEDULE_EXPORT, export.get("start_time", "00:00:00"), export.get("end_time", "00:00:00"), export.get("soc", 5), export.get("enable", False))

        # Freeze (RBD) reuses the charge window times
        freeze_enabled = local.get("freeze", {}).get("enable", False)
        wrote |= await self._write_schedule(site_id, SCHEDULE_FREEZE, charge.get("start_time", "00:00:00"), charge.get("end_time", "00:00:00"), None, freeze_enabled)

        if wrote:
            # Re-read to confirm; writes settle asynchronously so pendings may persist for minutes
            await self.get_schedules(site_id)
            for family in (SCHEDULE_CHARGE, SCHEDULE_EXPORT, SCHEDULE_FREEZE):
                pending = self.pending_writes.get((site_id, family))
                if pending and schedules_equal(self.schedules.get(site_id, {}).get(family.lower(), {}), pending["start"], pending["end"], pending["limit"], pending["enabled"]):
                    del self.pending_writes[(site_id, family)]
            await self.get_profile(site_id)
```

Nuance the tests depend on: `schedules_equal(None, ..., enabled=True)` is False (test asserts `not schedules_equal(None, ...)`) but a missing cloud entry with `enabled=False` desired is equal (no write needed to disable a non-existent schedule) — that is why `test_apply_no_change_no_write` sees zero writes for export/freeze.

- [ ] **Step 4: Run tests to verify pass, then commit**

```bash
git add apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py
git commit -m "feat: Enphase battery schedule write path with settle confirmation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: INVERTER_DEF, automatic_config, template

**Files:**

- Modify: `apps/predbat/config.py` (add `"EnphaseCloud"` after the `"FoxCloud"` dict ending at line 1919)
- Modify: `apps/predbat/enphase.py` (add `automatic_config`)
- Create: `templates/enphase_cloud.yaml` (copy `templates/fox_cloud.yaml` as the base, swap fox keys for `enphase_username`/`enphase_password`/`enphase_automatic`)
- Test: `apps/predbat/tests/test_enphase_api.py`

**Interfaces:**

- Consumes: entity naming from Tasks 5/6.
- Produces: `INVERTER_DEF["EnphaseCloud"]`; `automatic_config()` setting all inverter args.

- [ ] **Step 1: Write failing test**

```python
def test_automatic_config():
    """automatic_config points every inverter arg at the published entities."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    run_async(api.automatic_config())
    args = api.args_set
    assert args["inverter_type"] == ["EnphaseCloud"]
    assert args["num_inverters"] == 1
    assert args["soc_percent"] == ["sensor.predbat_enphase_12345_soc_percent"]
    assert args["soc_max"] == ["sensor.predbat_enphase_12345_battery_capacity"]
    assert args["battery_rate_max"] == ["sensor.predbat_enphase_12345_battery_rate_max"]
    assert args["load_today"] == ["sensor.predbat_enphase_12345_load_today"]
    assert args["import_today"] == ["sensor.predbat_enphase_12345_import_today"]
    assert args["export_today"] == ["sensor.predbat_enphase_12345_export_today"]
    assert args["pv_today"] == ["sensor.predbat_enphase_12345_pv_today"]
    assert args["charge_start_time"] == ["select.predbat_enphase_12345_battery_schedule_charge_start_time"]
    assert args["charge_limit"] == ["number.predbat_enphase_12345_battery_schedule_charge_soc"]
    assert args["scheduled_charge_enable"] == ["switch.predbat_enphase_12345_battery_schedule_charge_enable"]
    assert args["scheduled_discharge_enable"] == ["switch.predbat_enphase_12345_battery_schedule_export_enable"]
    assert args["discharge_start_time"] == ["select.predbat_enphase_12345_battery_schedule_export_start_time"]
    assert args["discharge_target_soc"] == ["number.predbat_enphase_12345_battery_schedule_export_soc"]
    assert args["reserve"] == ["number.predbat_enphase_12345_battery_schedule_reserve"]
    assert args["battery_min_soc"] == ["sensor.predbat_enphase_12345_battery_reserve_min"]
    assert args["schedule_write_button"] == ["switch.predbat_enphase_12345_battery_schedule_charge_write"]
    assert args["export_limit"] == [99999]


def test_automatic_config_no_dtg():
    """Without dtg support, discharge args are not set."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": False}, "rbd": {"supported": True}}
    run_async(api.automatic_config())
    assert "discharge_start_time" not in api.args_set
    assert "scheduled_discharge_enable" not in api.args_set
```

Also add a config-level check:

```python
def test_inverter_def_enphase():
    """EnphaseCloud INVERTER_DEF exists with the agreed capability flags."""
    from config import INVERTER_DEF

    idef = INVERTER_DEF["EnphaseCloud"]
    assert idef["has_rest_api"] is False
    assert idef["has_target_soc"] is True
    assert idef["time_button_press"] is True
    assert idef["charge_time_entity_is_option"] is True
    assert idef["can_span_midnight"] is False
    assert idef["target_soc_used_for_discharge"] is True
    assert idef["has_fox_inverter_mode"] is False
```

- [ ] **Step 2: Run to verify failure.**

- [ ] **Step 3: Implement.** `INVERTER_DEF["EnphaseCloud"]` (config.py, after FoxCloud):

```python
    "EnphaseCloud": {
        "name": "EnphaseCloud",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "none",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "has_timed_pause": False,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "%",
        "num_load_entities": 1,
        "has_ge_inverter_mode": False,
        "has_ge_eco_toggle": False,
        "has_fox_inverter_mode": False,
        "time_button_press": True,
        "clock_time_format": "%Y-%m-%d %H:%M:%S",
        "write_and_poll_sleep": 2,
        "has_time_window": False,
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "has_idle_time": False,
        "can_span_midnight": False,
        "charge_discharge_with_rate": False,
        "target_soc_used_for_discharge": True,
    },
```

**Check before committing:** `grep -n "output_charge_control" apps/predbat/inverter.py | head` — confirm `"none"` is an accepted value (search for how the value is consumed). If only `"power"`/`"current"` are handled, use `"power"` like FoxCloud and simply do not set `charge_rate`/`discharge_rate` args in `automatic_config` (Predbat then uses `battery_rate_max`). Record which option was chosen in the commit message.

`automatic_config` (enphase.py) — mirror fox.py:2158 but single-site:

```python
    async def automatic_config(self):
        """Automatically configure Predbat inverter args from the discovered Enphase site."""
        if not self.sites:
            raise ValueError("Enphase API: No sites found, cannot configure")
        site_id = self.sites[0]["site_id"]
        status = self.battery_status.get(site_id, {})
        if not status.get("max_capacity"):
            raise ValueError("Enphase API: No battery found on site, cannot configure")
        entity = f"{self.prefix}_enphase_{site_id}"
        has_dtg = self.dtg_supported(site_id)

        self.set_arg("inverter_type", ["EnphaseCloud"])
        self.set_arg("num_inverters", 1)
        self.set_arg("load_today", [f"sensor.{entity}_load_today"])
        self.set_arg("import_today", [f"sensor.{entity}_import_today"])
        self.set_arg("export_today", [f"sensor.{entity}_export_today"])
        if not self.automatic_ignore_pv:
            self.set_arg("pv_today", [f"sensor.{entity}_pv_today"])
            self.set_arg("pv_power", [f"sensor.{entity}_pv_power"])
        self.set_arg("soc_percent", [f"sensor.{entity}_soc_percent"])
        self.set_arg("soc_max", [f"sensor.{entity}_battery_capacity"])
        self.set_arg("battery_rate_max", [f"sensor.{entity}_battery_rate_max"])
        self.set_arg("battery_power", [f"sensor.{entity}_battery_power"])
        self.set_arg("grid_power", [f"sensor.{entity}_grid_power"])
        self.set_arg("load_power", [f"sensor.{entity}_load_power"])
        self.set_arg("reserve", [f"number.{entity}_battery_schedule_reserve"])
        self.set_arg("battery_min_soc", [f"sensor.{entity}_battery_reserve_min"])
        self.set_arg("charge_start_time", [f"select.{entity}_battery_schedule_charge_start_time"])
        self.set_arg("charge_end_time", [f"select.{entity}_battery_schedule_charge_end_time"])
        self.set_arg("charge_limit", [f"number.{entity}_battery_schedule_charge_soc"])
        self.set_arg("scheduled_charge_enable", [f"switch.{entity}_battery_schedule_charge_enable"])
        if has_dtg:
            self.set_arg("scheduled_discharge_enable", [f"switch.{entity}_battery_schedule_export_enable"])
            self.set_arg("discharge_start_time", [f"select.{entity}_battery_schedule_export_start_time"])
            self.set_arg("discharge_end_time", [f"select.{entity}_battery_schedule_export_end_time"])
            self.set_arg("discharge_target_soc", [f"number.{entity}_battery_schedule_export_soc"])
        self.set_arg("schedule_write_button", [f"switch.{entity}_battery_schedule_charge_write"])
        self.set_arg("export_limit", [99999])
```

Call it from `run()` on first successful data load when `self.automatic` is true (mirror where fox calls it — `grep -n "automatic_config" apps/predbat/fox.py`).

- [ ] **Step 4: Run tests to verify pass, then commit**

```bash
git add apps/predbat/config.py apps/predbat/enphase.py apps/predbat/tests/test_enphase_api.py templates/enphase_cloud.yaml
git commit -m "feat: EnphaseCloud inverter definition and automatic configuration

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Documentation, spell dictionary, full validation

**Files:**

- Modify: `docs/components.md` (new section after the Fox section ending ~line 523; add TOC entry near line 19)
- Modify: `docs/inverter-setup.md` (new "Enphase Cloud" section near the "Fox Cloud" section at line 233; template table rows near lines 42-43)
- Modify: `docs/apps-yaml.md` (`enphase_username` etc. near fox_key at line 188; `EnphaseCloud` in the inverter_type list near lines 773-774)
- Modify: `.cspell/custom-dictionary-workspace.txt`

**Interfaces:**

- Consumes: everything prior; this task gates the branch on repo-wide checks.

- [ ] **Step 1: Write docs**

`docs/components.md` section content (adapt formatting to the Fox section's exact style):

```markdown
## Enphase API (enphase)

Connects Predbat to the Enphase Enlighten cloud for monitoring and battery control of
Enphase IQ Battery systems, with no local hardware access required.

**Important**: this uses the unofficial Enlighten web-app API (there is no official API
with battery control). Enphase may change it without notice. Accounts with multi-factor
authentication (MFA) enabled are not supported - disable MFA on the Enphase account.

Predbat controls the battery by writing Enphase schedules: charge windows become
charge-from-grid (CFG) schedules with a target SOC, export windows become
discharge-to-grid (DTG) schedules (only in regions where Enphase enables DTG), freeze
modes use restrict-battery-discharge (RBD) schedules, and the reserve is set via the
battery profile. Cloud writes can take a few minutes to settle.

| Option | apps.yaml key | Description |
|--------|---------------|-------------|
| username | enphase_username | Enlighten account e-mail (required) |
| password | enphase_password | Enlighten account password (required) |
| site_id | enphase_site_id | Restrict to one site id (optional, defaults to the first site) |
| automatic | enphase_automatic | Automatically configure Predbat inverter settings |
| automatic_ignore_pv | enphase_automatic_ignore_pv | Skip PV sensors during automatic configuration |
```

Also cover: sensors published, control entities, the login-cooldown behaviour (repeated login failures back off up to 24 h to protect the account).

- [ ] **Step 2: Add dictionary words**

Append to `.cspell/custom-dictionary-workspace.txt` (file is auto-sorted on commit; re-stage after pre-commit runs): `Encharge`, `Enlighten`, `Enpower`, `Enphase`, `enho`, `enlm`, `entrez` (skip any already present — check with grep first).

- [ ] **Step 3: Run the full test suite and pre-commit**

```bash
cd coverage && ./run_all --quick > /tmp/enphase_full.txt 2>&1; grep -iE "fail|error" /tmp/enphase_full.txt | head -30
cd .. && ./run_pre_commit > /tmp/enphase_precommit.txt 2>&1; tail -30 /tmp/enphase_precommit.txt
```

Expected: no test failures; pre-commit clean (interrogate 100%, flake8, black, cspell). Fix anything flagged, re-stage auto-fixed files.

- [ ] **Step 4: Commit and push**

```bash
git add docs/components.md docs/inverter-setup.md docs/apps-yaml.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs: Enphase cloud component documentation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

Do not open a PR yet — the component should first be validated against a real Enphase account (see Verification below).

---

## Verification (post-implementation, needs a real account)

The unofficial API cannot be fully validated by unit tests. Before raising a PR:

1. Configure `enphase_username`/`enphase_password` in a test apps.yaml with `enphase_automatic: true`.
2. Confirm: login succeeds, sensors appear and update, SOC matches the Enlighten app.
3. Trigger a short manual charge window via the published entities and confirm a CFG schedule appears in the Enlighten battery settings UI within ~5 minutes.
4. Watch for HTTP 401/406/429 in the logs over 24 h (header variants may need the `cookie_eauth_compatible` fallback from the reference repo — `_battery_config_cookie_eauth_headers` shape — if BatteryConfig calls fail with auth errors despite a valid login).

## Notes for implementers

- The reference implementation is cloned at `/private/tmp/claude-501/-Users-treforsouthwell-predbat-batpred/1c6147ca-7c80-4457-95be-562fc8092e24/scratchpad/ha-enphase-energy` (custom_components/enphase_ev/api.py, battery_runtime.py). Re-clone from <https://github.com/barneyonline/ha-enphase-energy> if missing. Use it to answer payload-shape questions; do not copy code wholesale (different licence and style).
- Fox reference points: `FoxAPI.initialize` fox.py:338, `run` fox.py:407, cache helpers fox.py:565-643, schedule entities fox.py:1290, events fox.py:1985-2116, `automatic_config` fox.py:2158, registry components.py:204, INVERTER_DEF config.py:1891, schema config.py:2196, mock pattern tests/test_fox_api.py:50.

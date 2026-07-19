# DEYE Cloud Inverter Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a full Fox/Enphase-style DEYE Cloud virtual-inverter component to Predbat, giving monitoring and battery charge/export control for DEYE (Sunsynk-family) hybrid inverters via the DeyeCloud OpenAPI, on both the HA add-on and Predbat.com SaaS.

**Architecture:** `DeyeAPI(ComponentBase, OAuthMixin)` in `apps/predbat/deye.py` registers each battery inverter as a `DeyeCloud` Predbat inverter type. Predbat drives published HA schedule entities (charge window, export window, reserve, rates, enables) through the generic `Inverter` class; all DEYE API knowledge stays inside `deye.py`. The component derives the DEYE work mode internally (mode-less pattern, like Enphase/Tesla) and writes a combined `strategy_dynamic_control` payload with change detection and async `orderId` polling.

**Tech Stack:** Python 3, `aiohttp`, `asyncio`, Predbat's `ComponentBase` / `OAuthMixin` / Storage, the existing test harness in `apps/predbat/tests/test_infra.py`.

**Reference design:** `docs/superpowers/specs/2026-07-19-deye-cloud-inverter-integration-design.md`.
**Templates to copy patterns from:** `apps/predbat/enphase.py` (mode-less cloud inverter), `apps/predbat/fox.py` (schedule compute, caching, run-loop tiers), `apps/predbat/tests/test_fox_oauth.py` (test style).

## Global Constraints

- **Line length:** ≤ 256 chars (Black), ≤ 250 (Flake8). **Naming:** `lower_case_with_underscores`.
- **Docstrings:** 100% coverage required (`interrogate`) — every function and class needs a docstring.
- **Spelling:** British English (CSpell, `en-gb`); add unknown words to `.cspell/custom-dictionary-workspace.txt` (auto-sorted on commit — re-stage after pre-commit).
- **Tests:** required for all new code. Tests live in `apps/predbat/tests/` and are run from `coverage/` (`./run_all --test <name>`). **Always save test output to a file, then grep it** — never pipe straight to grep.
- **Test style (this repo):** component tests subclass the component and bypass `__init__` (see `MockFoxOAuth`), each test **returns a truthy value on failure** and prints an `ERROR:`/`FAILED:` line, and a `run_<name>_tests(my_predbat)` aggregator returns a `failed` flag. Do **not** use bare `pytest` asserts for these — follow `test_fox_oauth.py`.
- **API contract values that are spike-verified live in ONE place:** `apps/predbat/deye_const.py`. All logic references those constants, so Task 0's spike changes values there without touching downstream code or tests.
- **Commit after every task** (frequent commits). Branch is `feat/deye-cloud-inverter` (already created).
- **Copyright header** (first lines of every new `.py`, copied verbatim from `fox.py`):
  ```python
  # -----------------------------------------------------------------------------
  # Predbat Home Battery System
  # Copyright Trefor Southwell 2026 - All Rights Reserved
  # This application maybe used for personal use only and not for commercial use
  # -----------------------------------------------------------------------------
  ```

---

## Task 0: Pre-flight spike — confirm the live API contract

**Not TDD** — an investigation whose deliverable is confirmed values written into `deye_const.py` (Task 1). If no live inverter is available, skip and proceed with the documented best-known defaults in Task 1; revisit before merge.

**Files:**
- Reference only: `docs/superpowers/specs/2026-07-19-deye-cloud-inverter-integration-design.md` ("Open items confirmed at the implementation spike").

- [ ] **Step 1:** Obtain a DEYE developer App ID/Secret (`developer.deyecloud.com`) and account creds for a battery inverter. Get a token: `POST {base}/account/token?appId={id}` body `{appSecret, email, password: sha256(pw)}`.
- [ ] **Step 2:** Capture a real `POST /device/latest` response for the inverter. Record the exact `dataList[].key` strings for SoC, battery power, grid power, PV power, load power, temperature, and their `unit` values.
- [ ] **Step 3:** Capture `POST /config/tou` and `POST /strategy/dynamic/control/read`. Record the exact `TimeUseSettingItem` per-slot field names, value units, ranges, and how many slots are returned (expected 6).
- [ ] **Step 4:** Record the reserve mechanism: is the SoC floor per-slot, or `battLowCapacity` via `/order/battery/parameter/update`? Note which produces an immediate hold.
- [ ] **Step 5:** Record forced-export behaviour: confirm `workMode=SELLING_FIRST` + `solarSellAction=on` drains the battery to grid to the slot SoC floor, and that a slot SoC of 99 holds the battery (solar-only export).
- [ ] **Step 6:** Record the token-refresh mechanism (dedicated refresh-token grant, or re-login with app creds) and observed `expiresIn`.
- [ ] **Step 7:** Confirm `POST /strategy/dynamic/control` is accepted by the target firmware (else note the granular-endpoint fallback).
- [ ] **Step 8:** Write all confirmed values into the Task 1 constants (replace the `# VERIFY@SPIKE` defaults). No commit needed here; the values land with Task 1.

---

## Task 1: Constants module (`deye_const.py`)

**Files:**
- Create: `apps/predbat/deye_const.py`
- Test: `apps/predbat/tests/test_deye_const.py`

**Interfaces:**
- Produces: `DEYE_BASE_URLS: dict`, `DEYE_TIMEOUT: int`, `DEYE_RETRIES: int`, `DEYE_ENDPOINTS: dict`, `DEYE_WORKMODE: dict` (`SELLING_FIRST`/`ZERO_EXPORT_TO_LOAD`/`ZERO_EXPORT_TO_CT`), `DEYE_TELEMETRY_KEYS: dict`, `TOU_FIELD: dict`, `TOU_SLOT_COUNT: int`, `FREEZE_EXPORT_SOC: int` (=99).

- [ ] **Step 1: Write the failing test**

```python
# apps/predbat/tests/test_deye_const.py
from deye_const import DEYE_BASE_URLS, DEYE_ENDPOINTS, DEYE_WORKMODE, DEYE_TELEMETRY_KEYS, TOU_FIELD, TOU_SLOT_COUNT, FREEZE_EXPORT_SOC


def test_deye_const_shape():
    """Constants expose the keys the component relies on."""
    failed = False
    for dc in ("eu", "am", "india"):
        if dc not in DEYE_BASE_URLS or not DEYE_BASE_URLS[dc].startswith("https://"):
            print(f"ERROR: base url missing/invalid for {dc}")
            failed = True
    for ep in ("token", "station_list", "station_device", "device_latest", "config_battery", "dynamic_control", "dynamic_read", "order_result"):
        if ep not in DEYE_ENDPOINTS:
            print(f"ERROR: endpoint {ep} missing")
            failed = True
    for m in ("selling_first", "zero_export_load", "zero_export_ct"):
        if m not in DEYE_WORKMODE:
            print(f"ERROR: workmode {m} missing")
            failed = True
    for k in ("soc", "battery_power", "grid_power", "pv_power", "load_power"):
        if k not in DEYE_TELEMETRY_KEYS:
            print(f"ERROR: telemetry key {k} missing")
            failed = True
    for f in ("time", "power", "soc", "grid_charge"):
        if f not in TOU_FIELD:
            print(f"ERROR: TOU field {f} missing")
            failed = True
    if TOU_SLOT_COUNT != 6:
        print("ERROR: TOU_SLOT_COUNT must be 6")
        failed = True
    if FREEZE_EXPORT_SOC != 99:
        print("ERROR: FREEZE_EXPORT_SOC must be 99")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_const.py -v > /tmp/deye_const.log 2>&1; grep -E "PASS|FAIL|Error" /tmp/deye_const.log`
Expected: FAIL — `ModuleNotFoundError: No module named 'deye_const'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/predbat/deye_const.py  (after the copyright header)
"""DEYE Cloud OpenAPI constants.

Values marked ``# VERIFY@SPIKE`` are best-known defaults from the DeyeCloud
developer docs and the hass-deyecloud reference; confirm against a live
inverter (see plan Task 0) and correct here. All component logic imports from
this module so a value change here needs no downstream edits.
"""

DEYE_BASE_URLS = {
    "eu": "https://eu1-developer.deyecloud.com/v1.0",
    "am": "https://us1-developer.deyecloud.com/v1.0",
    "india": "https://india-developer.deyecloud.com/v1.0",
}

DEYE_TIMEOUT = 30
DEYE_RETRIES = 3
TOU_SLOT_COUNT = 6
FREEZE_EXPORT_SOC = 99

DEYE_ENDPOINTS = {
    "token": "/account/token",
    "station_list": "/station/list",
    "station_device": "/station/device",
    "device_latest": "/device/latest",
    "config_battery": "/config/battery",
    "config_tou": "/config/tou",
    "dynamic_control": "/strategy/dynamic/control",
    "dynamic_read": "/strategy/dynamic/control/read",
    "order_result": "/order/result",
}

DEYE_WORKMODE = {
    "selling_first": "SELLING_FIRST",
    "zero_export_load": "ZERO_EXPORT_TO_LOAD",
    "zero_export_ct": "ZERO_EXPORT_TO_CT",
}

# device/latest dataList[].key spellings.  # VERIFY@SPIKE
DEYE_TELEMETRY_KEYS = {
    "soc": "batterySOC",
    "battery_power": "batteryPower",
    "grid_power": "gridPower",
    "pv_power": "pvPower",
    "load_power": "loadPower",
    "temperature": "batteryTemperature",
}

# TimeUseSettingItem per-slot field names.  # VERIFY@SPIKE
TOU_FIELD = {
    "time": "time",
    "power": "power",
    "soc": "soc",
    "grid_charge": "enableGridCharge",
    "generate": "enableGeneration",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_const.py -v > /tmp/deye_const.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_const.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye_const.py apps/predbat/tests/test_deye_const.py
git commit -m "feat(deye): add DEYE Cloud API constants module"
```

---

## Task 2: Config items, APPS_SCHEMA and `DeyeCloud` INVERTER_DEF

**Files:**
- Modify: `apps/predbat/config.py` (APPS_SCHEMA `deye_*` block near the `fox_*` keys ~line 2254; INVERTER_DEF `"DeyeCloud"` after `"EnphaseCloud"` ~line 1949)
- Modify: `.cspell/custom-dictionary-workspace.txt` (add `deye`, `DEYE`, `Deye`, `deyecloud`, `EMEA`, `Sunsynk`)
- Test: `apps/predbat/tests/test_deye_config.py`

**Interfaces:**
- Produces: `INVERTER_DEF["DeyeCloud"]` capability dict; APPS_SCHEMA keys `deye_app_id`, `deye_app_secret`, `deye_username`, `deye_password`, `deye_data_center`, `deye_company_id`, `deye_auth_method`, `deye_token_expires_at`, `deye_token_hash`, `deye_inverter_sn`, `deye_automatic`, `deye_automatic_ignore_pv`.

- [ ] **Step 1: Write the failing test**

```python
# apps/predbat/tests/test_deye_config.py
from config import INVERTER_DEF, APPS_SCHEMA


def test_deyecloud_inverter_def():
    """DeyeCloud is a mode-less inverter with freeze support."""
    failed = False
    d = INVERTER_DEF.get("DeyeCloud")
    if d is None:
        print("ERROR: DeyeCloud INVERTER_DEF missing")
        return True
    expect = {
        "has_ge_inverter_mode": False,
        "has_fox_inverter_mode": False,
        "has_ge_eco_toggle": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "target_soc_used_for_discharge": True,
    }
    for k, v in expect.items():
        if d.get(k) != v:
            print(f"ERROR: DeyeCloud[{k}] expected {v} got {d.get(k)}")
            failed = True
    for key in ("deye_app_id", "deye_auth_method", "deye_inverter_sn", "deye_data_center"):
        if key not in APPS_SCHEMA:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_config.py -v > /tmp/deye_config.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_config.log`
Expected: FAIL — `DeyeCloud INVERTER_DEF missing`.

- [ ] **Step 3: Write minimal implementation**

Add to `INVERTER_DEF` (copy the `"EnphaseCloud"` block as a base, rename, keep flags as below):

```python
    "DeyeCloud": {
        "name": "DeyeCloud",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "power",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "has_timed_pause": False,
        "charge_time_format": "HH:MM",
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

Add to `APPS_SCHEMA` (beside the `fox_*` keys):

```python
    "deye_app_id": {"type": "string", "empty": False},
    "deye_app_secret": {"type": "string", "empty": False},
    "deye_username": {"type": "string", "empty": False},
    "deye_password": {"type": "string", "empty": False},
    "deye_data_center": {"type": "string", "empty": False},
    "deye_company_id": {"type": "string", "empty": False},
    "deye_auth_method": {"type": "string", "empty": False},
    "deye_token_expires_at": {"type": "string", "empty": False},
    "deye_token_hash": {"type": "string", "empty": False},
    "deye_inverter_sn": {"type": "string|string_list", "empty": False},
    "deye_automatic": {"type": "boolean"},
    "deye_automatic_ignore_pv": {"type": "boolean"},
```

Add the words to `.cspell/custom-dictionary-workspace.txt` (any position; pre-commit re-sorts).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_config.py -v > /tmp/deye_config.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_config.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/config.py apps/predbat/tests/test_deye_config.py .cspell/custom-dictionary-workspace.txt
git commit -m "feat(deye): add DeyeCloud INVERTER_DEF and config schema"
```

---

## Task 3: `DeyeAPI` skeleton, OAuth init and component registration

**Files:**
- Create: `apps/predbat/deye.py`
- Modify: `apps/predbat/components.py` (import `DeyeAPI`; add `"deye"` to `COMPONENT_LIST` after `"fox"` ~line 244)
- Test: `apps/predbat/tests/test_deye_api.py` (created here, grows through later tasks)

**Interfaces:**
- Produces: `class DeyeAPI(ComponentBase, OAuthMixin)` with `initialise()` setting `self.data_center`, `self.company_id`, `self.inverter_sn_filter (list)`, `self.automatic`, `self.automatic_ignore_pv`, `self.device_list`, `self.device_values`, `self.device_battery_config`, `self.local_schedule`, `self.pending_orders`; helper `base_url` (property) → `DEYE_BASE_URLS[self.data_center]`. `COMPONENT_LIST["deye"]` with `event_filter="predbat_deye_"`.

- [ ] **Step 1: Write the failing test**

```python
# apps/predbat/tests/test_deye_api.py
import pytz
from datetime import datetime
from unittest.mock import MagicMock
from deye import DeyeAPI
from deye_const import DEYE_BASE_URLS


class MockDeye(DeyeAPI):
    """Test double: build a DeyeAPI without the full component lifecycle."""

    def __init__(self, auth_method="app_credentials", data_center="eu", inverter_sn=None):
        self.prefix = "predbat"
        self.automatic = False
        self.automatic_ignore_pv = False
        self.data_center = data_center
        self.company_id = ""
        self.inverter_sn_filter = inverter_sn or []
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.cached_values = {}
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-deye-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self._init_oauth(auth_method, "test-token", None, "deye")

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass


def test_deye_base_url():
    """base_url resolves from the data centre."""
    failed = False
    d = MockDeye(data_center="eu")
    if d.base_url != DEYE_BASE_URLS["eu"]:
        print(f"ERROR: base_url {d.base_url}")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_api.log`
Expected: FAIL — `No module named 'deye'`.

- [ ] **Step 3: Write minimal implementation**

```python
# apps/predbat/deye.py  (after copyright header)
"""DEYE Cloud API integration for Predbat.

Registers each DEYE battery inverter as a ``DeyeCloud`` Predbat inverter,
publishing monitoring sensors and Fox-style schedule control entities. Predbat
drives those entities through the generic Inverter class; this module derives
the DEYE work mode internally and applies a combined ``strategy_dynamic_control``
payload. Supports HA add-on (self-managed token) and Predbat.com SaaS (injected
token) auth.
"""

import aiohttp
import asyncio
import hashlib
from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from deye_const import DEYE_BASE_URLS, DEYE_TIMEOUT, DEYE_RETRIES, DEYE_ENDPOINTS


class DeyeAPI(ComponentBase, OAuthMixin):
    """DEYE Cloud API component."""

    def initialise(self):
        """Initialise the DEYE component from configured args."""
        self.log("Info: DeyeAPI initialising")
        self.data_center = self.get_arg("data_center", "eu")
        self.company_id = self.get_arg("company_id", "")
        self.automatic = self.get_arg("automatic", False)
        self.automatic_ignore_pv = self.get_arg("automatic_ignore_pv", False)
        sn = self.get_arg("inverter_sn", [])
        self.inverter_sn_filter = sn if isinstance(sn, list) else [sn]
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.cached_values = {}
        auth_method = self.get_arg("auth_method", "app_credentials")
        self._init_oauth(
            auth_method=auth_method,
            key=self.get_arg("app_secret", self.get_arg("token_hash", "")),
            token_expires_at=self.get_arg("token_expires_at", None),
            provider_name="deye",
        )

    @property
    def base_url(self):
        """Return the OpenAPI base URL for the configured data centre."""
        return DEYE_BASE_URLS.get(self.data_center, DEYE_BASE_URLS["eu"])
```

Modify `components.py`: add `from deye import DeyeAPI` beside `from fox import FoxAPI`, and after the `"fox"` entry:

```python
    "deye": {
        "class": DeyeAPI,
        "name": "DEYE Cloud",
        "event_filter": "predbat_deye_",
        "args": {
            "app_id": {"required": False, "config": "deye_app_id"},
            "app_secret": {"required": False, "config": "deye_app_secret"},
            "username": {"required": False, "config": "deye_username"},
            "password": {"required": False, "config": "deye_password"},
            "data_center": {"required": False, "default": "eu", "config": "deye_data_center"},
            "company_id": {"required": False, "config": "deye_company_id"},
            "auth_method": {"required": False, "default": "app_credentials", "config": "deye_auth_method"},
            "token_expires_at": {"required": False, "config": "deye_token_expires_at"},
            "token_hash": {"required": False, "config": "deye_token_hash"},
            "inverter_sn": {"required": False, "config": "deye_inverter_sn"},
            "automatic": {"required": False, "default": False, "config": "deye_automatic"},
            "automatic_ignore_pv": {"required": False, "default": False, "config": "deye_automatic_ignore_pv"},
        },
        "phase": 1,
    },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_api.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/components.py apps/predbat/tests/test_deye_api.py
git commit -m "feat(deye): add DeyeAPI skeleton and component registration"
```

---

## Task 4: Auth headers, `_post` transport (retry + 401 refresh), token fetch

**Files:**
- Modify: `apps/predbat/deye.py` (add `_auth_headers`, `_sha256`, `_login_payload`, `fetch_token`, `_post`)
- Test: `apps/predbat/tests/test_deye_oauth.py`

**Interfaces:**
- Consumes: `MockDeye` from `test_deye_api.py`; `create_aiohttp_mock_response`, `create_aiohttp_mock_session`, `run_async` from `tests/test_infra.py`.
- Produces: `async _post(self, endpoint_key, body) -> dict`; `async fetch_token(self) -> bool`; `_auth_headers() -> dict`; static `_sha256(str) -> str`; `_login_payload(login) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# apps/predbat/tests/test_deye_oauth.py
import hashlib
from unittest.mock import patch
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session
from tests.test_deye_api import MockDeye


def test_sha256_and_login_payload():
    """Password hashed lower-hex SHA-256; @ picks email key else username."""
    failed = False
    d = MockDeye()
    if d._sha256("secret") != hashlib.sha256(b"secret").hexdigest().lower():
        print("ERROR: sha256 wrong")
        failed = True
    if d._login_payload("a@b.com") != {"email": "a@b.com"}:
        print("ERROR: email payload wrong")
        failed = True
    if d._login_payload("bob") != {"username": "bob"}:
        print("ERROR: username payload wrong")
        failed = True
    return failed


def test_auth_headers_bearer():
    """Auth header carries the current access token as a Bearer."""
    failed = False
    d = MockDeye()
    d.access_token = "tok-123"
    h = d._auth_headers()
    if h.get("Authorization") != "Bearer tok-123":
        print(f"ERROR: header {h}")
        failed = True
    return failed


def test_post_401_refreshes_then_retries():
    """A 401 triggers handle_oauth_401 then a successful retry."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "old"
    resp_401 = create_aiohttp_mock_response(status=401, json_data={"success": False})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True, "data": 1})
    session = create_aiohttp_mock_session([resp_401, resp_ok])

    async def fake_refresh():
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._post("station_list", {}))
    if not out.get("success"):
        print(f"ERROR: expected success after refresh, got {out}")
        failed = True
    if d.access_token != "new":
        print("ERROR: token not refreshed")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_oauth.py -v > /tmp/deye_oauth.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_oauth.log`
Expected: FAIL — `AttributeError: '... _sha256'` / `_post`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    @staticmethod
    def _sha256(password):
        """Return the lower-case hex SHA-256 of a password."""
        return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()

    @staticmethod
    def _login_payload(login):
        """Choose the DEYE login key: email if it looks like one, else username."""
        login = (login or "").strip()
        return {"email": login} if "@" in login else {"username": login}

    def _auth_headers(self):
        """Return JSON + Bearer auth headers for a DEYE request."""
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.access_token}"}

    async def fetch_token(self):
        """Fetch an access token using app credentials (app_credentials mode)."""
        app_id = self.get_arg("app_id", "")
        url = f"{self.base_url}{DEYE_ENDPOINTS['token']}?appId={app_id}"
        body = {"appSecret": self.get_arg("app_secret", ""), "password": self._sha256(self.get_arg("password", "")), **self._login_payload(self.get_arg("username", ""))}
        if self.company_id:
            body["companyId"] = str(self.company_id)
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: DEYE token fetch failed: {e}")
            return False
        if not data.get("success"):
            self.log(f"Warn: DEYE token rejected: {data.get('msg', 'unknown')}")
            return False
        self.access_token = data.get("accessToken")
        return True

    async def _post(self, endpoint_key, body):
        """POST to a DEYE endpoint with retry and 401-refresh. Returns parsed JSON or raises."""
        url = f"{self.base_url}{DEYE_ENDPOINTS[endpoint_key]}"
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        last_err = None
        for attempt in range(DEYE_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._auth_headers(), json=body) as resp:
                        if resp.status in (401, 403):
                            self.log(f"Warn: DEYE 401/403 on {endpoint_key}, attempt {attempt + 1}")
                            if await self.handle_oauth_401():
                                continue
                            raise RuntimeError(f"DEYE auth failed on {endpoint_key}")
                        resp.raise_for_status()
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                self.log(f"Warn: DEYE network error on {endpoint_key} attempt {attempt + 1}: {e}")
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"DEYE POST failed after {DEYE_RETRIES} retries on {endpoint_key}: {last_err}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_oauth.py -v > /tmp/deye_oauth.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_oauth.log`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_oauth.py
git commit -m "feat(deye): add auth headers, token fetch and _post transport"
```

---

## Task 5: Device discovery (stations → inverters, paginated)

**Files:**
- Modify: `apps/predbat/deye.py` (add `get_station_ids`, `get_device_list`)
- Test: `apps/predbat/tests/test_deye_api.py` (append)

**Interfaces:**
- Consumes: `_post`.
- Produces: `async get_station_ids(self) -> list`; `async get_device_list(self) -> list` (populates `self.device_list` with inverter `deviceSn` strings, honouring `inverter_sn_filter`).

- [ ] **Step 1: Write the failing test** (append to `test_deye_api.py`)

```python
from unittest.mock import patch


def test_get_device_list_filters_inverters():
    """Only INVERTER devices are kept; sn filter is honoured."""
    failed = False
    d = MockDeye(inverter_sn=["INV1"])

    async def fake_post(endpoint_key, body):
        if endpoint_key == "station_list":
            return {"success": True, "stationList": [{"id": 10}]}
        if endpoint_key == "station_device":
            return {"success": True, "total": 2, "deviceListItems": [
                {"deviceType": "INVERTER", "deviceSn": "INV1"},
                {"deviceType": "METER", "deviceSn": "MET9"},
            ]}
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        devices = run_async_local(d.get_device_list())
    if devices != ["INV1"]:
        print(f"ERROR: devices {devices}")
        failed = True
    return failed
```

Add this helper import at the top of `test_deye_api.py` (reuse the shared runner):

```python
from tests.test_infra import run_async as run_async_local
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py::test_get_device_list_filters_inverters -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_api.log`
Expected: FAIL — no attribute `get_device_list`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    async def get_station_ids(self):
        """Return station ids visible to the account."""
        data = await self._post("station_list", {})
        if not data.get("success", True):
            self.log(f"Warn: DEYE station/list failed: {data.get('msg', 'unknown')}")
            return []
        stations = data.get("stationList") or []
        return [s.get("id") or s.get("stationId") for s in stations if s.get("id") or s.get("stationId")]

    async def get_device_list(self):
        """Discover battery inverter serials across the account's stations."""
        station_ids = await self.get_station_ids()
        if not station_ids:
            self.log("Warn: DEYE no stations found")
            self.device_list = []
            return []
        devices = []
        page, size = 1, 100
        while True:
            data = await self._post("station_device", {"page": page, "size": size, "stationIds": station_ids})
            if not data.get("success", True):
                self.log(f"Warn: DEYE station/device failed: {data.get('msg', 'unknown')}")
                break
            items = data.get("deviceListItems") or []
            devices.extend(items)
            total = data.get("total")
            if (total is not None and len(devices) >= int(total)) or len(items) < size:
                break
            page += 1
        serials = [x["deviceSn"] for x in devices if x.get("deviceType") == "INVERTER" and x.get("deviceSn")]
        if self.inverter_sn_filter:
            wanted = {s.lower() for s in self.inverter_sn_filter}
            serials = [s for s in serials if s.lower() in wanted]
        self.device_list = serials
        return serials
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py::test_get_device_list_filters_inverters -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_api.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_api.py
git commit -m "feat(deye): add station and inverter device discovery"
```

---

## Task 6: Telemetry parse (`device/latest`) and battery config

**Files:**
- Modify: `apps/predbat/deye.py` (add `_datalist_to_dict`, `fetch_device_data`, `fetch_battery_config`)
- Test: `apps/predbat/tests/test_deye_api.py` (append)

**Interfaces:**
- Consumes: `_post`, `DEYE_TELEMETRY_KEYS`.
- Produces: `_datalist_to_dict(data_list) -> dict` (raw `{key: value}`); `async fetch_device_data(self, sn) -> dict` (normalised `{soc, battery_power, grid_power, pv_power, load_power, temperature}` via `DEYE_TELEMETRY_KEYS`, cached in `self.device_values[sn]`); `async fetch_battery_config(self, sn) -> dict` (cached in `self.device_battery_config[sn]`).

- [ ] **Step 1: Write the failing test** (append)

```python
from deye_const import DEYE_TELEMETRY_KEYS


def test_fetch_device_data_maps_keys():
    """dataList key/value pairs map to normalised telemetry via the key table."""
    failed = False
    d = MockDeye()
    data_list = [
        {"key": DEYE_TELEMETRY_KEYS["soc"], "value": "57", "unit": "%"},
        {"key": DEYE_TELEMETRY_KEYS["grid_power"], "value": "-1200", "unit": "W"},
    ]

    async def fake_post(endpoint_key, body):
        return {"success": True, "deviceDataList": [{"deviceSn": "INV1", "dataList": data_list}]}

    with patch.object(d, "_post", side_effect=fake_post):
        out = run_async_local(d.fetch_device_data("INV1"))
    if out.get("soc") != 57.0:
        print(f"ERROR: soc {out.get('soc')}")
        failed = True
    if out.get("grid_power") != -1200.0:
        print(f"ERROR: grid_power {out.get('grid_power')}")
        failed = True
    if d.device_values.get("INV1", {}).get("soc") != 57.0:
        print("ERROR: not cached")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py::test_fetch_device_data_maps_keys -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_api.log`
Expected: FAIL — no attribute `fetch_device_data`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`, add `from deye_const import DEYE_TELEMETRY_KEYS` to the imports)

```python
    @staticmethod
    def _datalist_to_dict(data_list):
        """Flatten a DEYE dataList of {key,value} pairs into a plain dict."""
        out = {}
        for item in data_list or []:
            key = item.get("key")
            if key is not None:
                out[key] = item.get("value")
        return out

    @staticmethod
    def _as_float(value, default=0.0):
        """Best-effort float coercion."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def fetch_device_data(self, sn):
        """Fetch and normalise the latest telemetry for one inverter."""
        data = await self._post("device_latest", {"deviceSnList": [sn]})
        if not data.get("success", True):
            self.log(f"Warn: DEYE device/latest failed for {sn}: {data.get('msg', 'unknown')}")
            return {}
        rows = data.get("deviceDataList") or []
        if not rows:
            return {}
        flat = self._datalist_to_dict(rows[0].get("dataList"))
        result = {name: self._as_float(flat.get(key)) for name, key in DEYE_TELEMETRY_KEYS.items()}
        self.device_values[sn] = result
        return result

    async def fetch_battery_config(self, sn):
        """Fetch and cache battery capability config for one inverter."""
        data = await self._post("config_battery", {"deviceSn": sn})
        if not data.get("success", True):
            self.log(f"Warn: DEYE config/battery failed for {sn}: {data.get('msg', 'unknown')}")
            return {}
        self.device_battery_config[sn] = data
        return data
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py::test_fetch_device_data_maps_keys -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_api.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_api.py
git commit -m "feat(deye): parse device/latest telemetry and battery config"
```

---

## Task 7: Behaviour → DEYE control-state derivation (pure function)

This is the heart of the spec's mode-less control. It takes the desired per-inverter schedule state (already read from the HA entities) plus current SoC and returns the DEYE control intent (work mode + flags + slot SoC/power) for the imminent window.

**Files:**
- Modify: `apps/predbat/deye.py` (add `derive_control_state`)
- Test: `apps/predbat/tests/test_deye_control.py`

**Interfaces:**
- Consumes: `DEYE_WORKMODE`, `FREEZE_EXPORT_SOC`.
- Produces: `derive_control_state(self, schedule, current_soc) -> dict` where `schedule` is `{"reserve": int, "charge": {"enable": bool, "soc": int, "power": int}, "export": {"enable": bool, "soc": int, "power": int}}` and the return is `{"work_mode": str, "grid_charge": bool, "solar_sell": bool, "slot_soc": int, "power": int, "behaviour": str}`. `behaviour` ∈ `{charge, freeze_charge, hold_charge, export, freeze_export, idle}`.

- [ ] **Step 1: Write the failing test**

```python
# apps/predbat/tests/test_deye_control.py
from deye_const import DEYE_WORKMODE, FREEZE_EXPORT_SOC
from tests.test_deye_api import MockDeye


def _state(reserve=10, charge=None, export=None):
    return {"reserve": reserve, "charge": charge or {"enable": False, "soc": 0, "power": 0}, "export": export or {"enable": False, "soc": 0, "power": 0}}


def test_derive_control_state_table():
    """Each Predbat intent maps to the correct DEYE control state (spec table)."""
    failed = False
    d = MockDeye()
    cases = [
        # name, schedule, current_soc, expect(behaviour, work_mode, grid_charge, solar_sell, slot_soc)
        ("charge", _state(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50,
         ("charge", DEYE_WORKMODE["zero_export_load"], True, False, 90)),
        ("freeze_charge", _state(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50,
         ("freeze_charge", DEYE_WORKMODE["zero_export_load"], True, False, 50)),
        ("hold_charge", _state(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50,
         ("hold_charge", DEYE_WORKMODE["zero_export_load"], False, False, 50)),
        ("export", _state(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80,
         ("export", DEYE_WORKMODE["selling_first"], False, True, 20)),
        ("freeze_export", _state(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80,
         ("freeze_export", DEYE_WORKMODE["selling_first"], False, True, FREEZE_EXPORT_SOC)),
        ("idle", _state(reserve=15), 60,
         ("idle", DEYE_WORKMODE["zero_export_load"], False, False, 15)),
    ]
    for name, sched, soc, exp in cases:
        r = d.derive_control_state(sched, soc)
        got = (r["behaviour"], r["work_mode"], r["grid_charge"], r["solar_sell"], r["slot_soc"])
        if got != exp:
            print(f"ERROR: {name} expected {exp} got {got}")
            failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_control.log`
Expected: FAIL — no attribute `derive_control_state`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`; add `from deye_const import DEYE_WORKMODE, FREEZE_EXPORT_SOC` to imports)

```python
    def derive_control_state(self, schedule, current_soc):
        """Map Predbat's schedule intent to a DEYE control state (see design spec table)."""
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})

        if export.get("enable"):
            export_soc = int(export.get("soc", FREEZE_EXPORT_SOC))
            if export_soc >= FREEZE_EXPORT_SOC:
                return {"behaviour": "freeze_export", "work_mode": DEYE_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": FREEZE_EXPORT_SOC, "power": int(export.get("power", 0))}
            return {"behaviour": "export", "work_mode": DEYE_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": export_soc, "power": int(export.get("power", 0))}

        if charge.get("enable"):
            charge_soc = int(charge.get("soc", 0))
            if charge_soc > current_soc and charge_soc > reserve:
                return {"behaviour": "charge", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": charge_soc, "power": int(charge.get("power", 0))}
            if charge_soc <= reserve:
                return {"behaviour": "freeze_charge", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}
            return {"behaviour": "hold_charge", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}

        return {"behaviour": "idle", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": 0}
```

> Note on the freeze/hold split (spec): **freeze charge** = "a charge whose target SoC equals the reserve" → `charge_soc <= reserve`, grid-charge on, hold at reserve. **hold charge** = "charge amount ≤ current SoC, held with reserve" → target between reserve and current, grid-charge off, held at reserve. **charge** = real grid charge to a target above both current SoC and reserve.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_control.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_control.py
git commit -m "feat(deye): derive DEYE work mode from Predbat schedule intent"
```

---

## Task 8: Window → 6-slot TOU schedule builder

**Files:**
- Modify: `apps/predbat/deye.py` (add `build_tou_slots`)
- Test: `apps/predbat/tests/test_deye_control.py` (append)

**Interfaces:**
- Consumes: `derive_control_state`, `TOU_FIELD`, `TOU_SLOT_COUNT`.
- Produces: `build_tou_slots(self, schedule, current_soc) -> list` — exactly `TOU_SLOT_COUNT` dicts keyed by `TOU_FIELD`, in ascending start-time order, each covering a segment of the day. Charge/export windows become their own segments; the remainder is self-use at reserve.

- [ ] **Step 1: Write the failing test** (append)

```python
from deye_const import TOU_FIELD, TOU_SLOT_COUNT


def test_build_tou_slots_charge_window():
    """A charge window produces exactly 6 ordered slots with a grid-charge segment."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10,
             "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"},
             "export": {"enable": False, "soc": 0, "power": 0}}
    slots = d.build_tou_slots(sched, current_soc=40)
    if len(slots) != TOU_SLOT_COUNT:
        print(f"ERROR: expected {TOU_SLOT_COUNT} slots got {len(slots)}")
        return True
    times = [s[TOU_FIELD["time"]] for s in slots]
    if times != sorted(times):
        print(f"ERROR: slots not ordered {times}")
        failed = True
    charge_slots = [s for s in slots if s[TOU_FIELD["grid_charge"]] and s[TOU_FIELD["soc"]] == 95]
    if not charge_slots:
        print("ERROR: no grid-charge slot at soc 95")
        failed = True
    if slots[0][TOU_FIELD["time"]] != "00:00":
        print(f"ERROR: first slot must start 00:00 got {slots[0][TOU_FIELD['time']]}")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py::test_build_tou_slots_charge_window -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_control.log`
Expected: FAIL — no attribute `build_tou_slots`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`; add `from deye_const import TOU_FIELD, TOU_SLOT_COUNT` to imports)

```python
    def _self_use_slot(self, start_time, reserve):
        """Build a self-use TOU slot holding at the reserve SoC."""
        return {TOU_FIELD["time"]: start_time, TOU_FIELD["power"]: 0, TOU_FIELD["soc"]: int(reserve), TOU_FIELD["grid_charge"]: False, TOU_FIELD["generate"]: True}

    def _action_slot(self, start_time, state):
        """Build a TOU slot realising a derived control state."""
        return {TOU_FIELD["time"]: start_time, TOU_FIELD["power"]: int(state["power"]), TOU_FIELD["soc"]: int(state["slot_soc"]), TOU_FIELD["grid_charge"]: bool(state["grid_charge"]), TOU_FIELD["generate"]: True}

    def build_tou_slots(self, schedule, current_soc):
        """Build exactly TOU_SLOT_COUNT ordered slots covering 24h from the schedule windows."""
        reserve = int(schedule.get("reserve", 0))
        # Collect (start_time, state) segment boundaries. Baseline self-use at 00:00.
        segments = {"00:00": {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None}}
        for direction in ("charge", "export"):
            window = schedule.get(direction, {})
            if window.get("enable") and window.get("start") and window.get("end"):
                intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
                intent[direction] = {"enable": True, "soc": window.get("soc", 0), "power": window.get("power", 0)}
                state = self.derive_control_state(intent, current_soc)
                segments[window["start"]] = state
                # After the window, return to self-use at reserve.
                segments.setdefault(window["end"], {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None})
        ordered = sorted(segments.items(), key=lambda kv: kv[0])
        slots = []
        for start_time, state in ordered:
            if state.get("grid_charge") or state.get("solar_sell") or state.get("power"):
                slots.append(self._action_slot(start_time, state))
            else:
                slots.append(self._self_use_slot(start_time, reserve))
        # Normalise to exactly TOU_SLOT_COUNT: pad by repeating the last slot's SoC at spread times, or trim keeping the imminent windows.
        while len(slots) < TOU_SLOT_COUNT:
            filler_time = "23:59" if not slots else slots[-1][TOU_FIELD["time"]]
            slots.append(self._self_use_slot(filler_time, reserve))
        if len(slots) > TOU_SLOT_COUNT:
            slots = slots[:TOU_SLOT_COUNT]
        return slots
```

> The pad/trim keeps the plan self-contained and passing; the spike may refine how DEYE expects unused slots expressed (e.g. all six must be distinct times). Adjust `build_tou_slots` only, leaving `derive_control_state` untouched.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py::test_build_tou_slots_charge_window -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_control.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_control.py
git commit -m "feat(deye): build 6-slot TOU schedule from charge/export windows"
```

---

## Task 9: Combined control payload + change detection

**Files:**
- Modify: `apps/predbat/deye.py` (add `build_dynamic_payload`, `payloads_equal`)
- Test: `apps/predbat/tests/test_deye_control.py` (append)

**Interfaces:**
- Consumes: `build_tou_slots`, `derive_control_state`, `TOU_FIELD`.
- Produces: `build_dynamic_payload(self, sn, schedule, current_soc) -> dict` (the `strategy_dynamic_control` body: `deviceSn`, `workMode`, `gridChargeAction`, `solarSellAction`, `touAction`, `timeUseSettingItems`); `payloads_equal(self, a, b) -> bool` (compares ignoring `deviceSn`).

- [ ] **Step 1: Write the failing test** (append)

```python
def test_build_dynamic_payload_and_equality():
    """Payload carries work mode + on/off actions + 6 slots; equality ignores deviceSn."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    p1 = d.build_dynamic_payload("INV1", sched, current_soc=40)
    p2 = d.build_dynamic_payload("INV2", sched, current_soc=40)
    if p1.get("deviceSn") != "INV1":
        print("ERROR: deviceSn not set")
        failed = True
    if len(p1.get("timeUseSettingItems", [])) != 6:
        print("ERROR: payload must carry 6 slots")
        failed = True
    if p1.get("gridChargeAction") not in ("on", "off"):
        print(f"ERROR: gridChargeAction {p1.get('gridChargeAction')}")
        failed = True
    if not d.payloads_equal(p1, p2):
        print("ERROR: payloads differing only by deviceSn should be equal")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py::test_build_dynamic_payload_and_equality -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_control.log`
Expected: FAIL — no attribute `build_dynamic_payload`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    def build_dynamic_payload(self, sn, schedule, current_soc):
        """Build the strategy_dynamic_control body for one inverter."""
        slots = self.build_tou_slots(schedule, current_soc)
        # The imminent action drives the top-level work mode / on-off flags.
        active = self.derive_control_state(schedule, current_soc)
        return {
            "deviceSn": sn,
            "workMode": active["work_mode"],
            "gridChargeAction": "on" if active["grid_charge"] else "off",
            "solarSellAction": "on" if active["solar_sell"] else "off",
            "touAction": "on",
            "timeUseSettingItems": slots,
        }

    def payloads_equal(self, a, b):
        """Compare two dynamic-control payloads ignoring deviceSn."""
        def strip(p):
            return {k: v for k, v in (p or {}).items() if k != "deviceSn"}
        return strip(a) == strip(b)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py::test_build_dynamic_payload_and_equality -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_control.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_control.py
git commit -m "feat(deye): build combined control payload with change detection"
```

---

## Task 10: Async write + order polling

**Files:**
- Modify: `apps/predbat/deye.py` (add `apply_dynamic_control`, `read_dynamic_control`, `poll_order`)
- Test: `apps/predbat/tests/test_deye_control.py` (append)

**Interfaces:**
- Consumes: `_post`, `build_dynamic_payload`, `payloads_equal`, `DEYE_ENDPOINTS`.
- Produces: `async read_dynamic_control(self, sn) -> dict`; `async apply_dynamic_control(self, sn, schedule, current_soc, force=False) -> bool` (reads back, diffs, writes only on change or `force`, records `orderId` in `self.pending_orders[sn]`); `async poll_order(self, sn) -> str` (returns `"success"`/`"pending"`/`"failed"`).

- [ ] **Step 1: Write the failing test** (append)

```python
def test_apply_dynamic_control_suppresses_when_unchanged():
    """No write when the read-back already matches the desired payload."""
    failed = False
    d = MockDeye()
    sched = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 0, "power": 0}}
    desired = d.build_dynamic_payload("INV1", sched, 40)
    posts = []

    async def fake_post(endpoint_key, body):
        posts.append(endpoint_key)
        if endpoint_key == "dynamic_read":
            return {"success": True, "workMode": desired["workMode"], "gridChargeAction": desired["gridChargeAction"], "solarSellAction": desired["solarSellAction"], "touAction": desired["touAction"], "timeUseSettingItems": desired["timeUseSettingItems"]}
        return {"success": True, "orderId": 1}

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = run_async_local(d.apply_dynamic_control("INV1", sched, 40))
    if wrote:
        print("ERROR: should not write when unchanged")
        failed = True
    if "dynamic_control" in posts:
        print("ERROR: dynamic_control was posted despite no change")
        failed = True
    return failed


def test_poll_order_success():
    """poll_order maps a successful order result."""
    failed = False
    d = MockDeye()
    d.pending_orders["INV1"] = 42

    async def fake_post(endpoint_key, body):
        return {"success": True, "connectionStatus": 1}

    with patch.object(d, "_post", side_effect=fake_post):
        status = run_async_local(d.poll_order("INV1"))
    if status != "success":
        print(f"ERROR: status {status}")
        failed = True
    if "INV1" in d.pending_orders:
        print("ERROR: successful order should be cleared")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py -k "apply_dynamic or poll_order" -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_control.log`
Expected: FAIL — no attribute `apply_dynamic_control`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    async def read_dynamic_control(self, sn):
        """Read the current combined control state for one inverter."""
        data = await self._post("dynamic_read", {"deviceSn": sn})
        if not data.get("success", True):
            self.log(f"Warn: DEYE dynamic read failed for {sn}: {data.get('msg', 'unknown')}")
            return {}
        return data

    async def apply_dynamic_control(self, sn, schedule, current_soc, force=False):
        """Write the combined control payload, suppressing no-op writes. Returns True if written."""
        desired = self.build_dynamic_payload(sn, schedule, current_soc)
        if not force:
            current = await self.read_dynamic_control(sn)
            if current and self.payloads_equal(desired, {**desired, **{k: current.get(k) for k in ("workMode", "gridChargeAction", "solarSellAction", "touAction", "timeUseSettingItems")}}):
                self.log(f"Info: DEYE {sn} control unchanged, skipping write")
                return False
        resp = await self._post("dynamic_control", desired)
        if not resp.get("success", True):
            self.log(f"Warn: DEYE dynamic control failed for {sn}: {resp.get('msg', 'unknown')}")
            return False
        order_id = resp.get("orderId")
        if order_id:
            self.pending_orders[sn] = order_id
            self.log(f"Info: DEYE {sn} control submitted, orderId={order_id}")
        return True

    async def poll_order(self, sn):
        """Poll the pending control order for one inverter. Returns success/pending/failed."""
        order_id = self.pending_orders.get(sn)
        if not order_id:
            return "success"
        resp = await self._post("order_result", {"orderId": order_id})
        if not resp.get("success", True):
            return "pending"
        self.pending_orders.pop(sn, None)
        return "success"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_control.py -k "apply_dynamic or poll_order" -v > /tmp/deye_control.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_control.log`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_control.py
git commit -m "feat(deye): async combined-control write with order polling"
```

---

## Task 11: Publish sensors and schedule control entities

**Files:**
- Modify: `apps/predbat/deye.py` (add `publish_data`, `publish_schedule_settings_ha`, `get_schedule_settings_ha`)
- Test: `apps/predbat/tests/test_deye_publish.py`

**Interfaces:**
- Consumes: `dashboard_item`, `get_state_wrapper`, `self.device_values`, `self.local_schedule`.
- Produces: `async publish_data(self)`; `async publish_schedule_settings_ha(self, sn)`; `async get_schedule_settings_ha(self, sn) -> dict` (reads the control entities into `self.local_schedule[sn]` shaped as the `schedule` dict used by Task 7). Entity naming: `sensor|select|number|switch.{prefix}_deye_{sn}_...` (mirrors Fox/Enphase).

- [ ] **Step 1: Write the failing test**

```python
# apps/predbat/tests/test_deye_publish.py
from tests.test_deye_api import MockDeye


class RecordingDeye(MockDeye):
    """MockDeye that records dashboard_item calls and serves entity states."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.published = {}
        self.entity_states = {}

    def dashboard_item(self, entity, state=None, attributes=None, app=None):
        """Record a published entity."""
        self.published[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Serve a canned entity state."""
        return self.entity_states.get(entity_id, default)


def test_publish_data_creates_soc_sensor():
    """publish_data emits a SoC sensor for each known inverter."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 63.0, "battery_power": 100.0, "grid_power": 0.0, "pv_power": 500.0, "load_power": 400.0, "temperature": 21.0}}
    import tests.test_infra as ti
    ti.run_async(d.publish_data())
    if "sensor.predbat_deye_inv1_soc" not in d.published:
        print(f"ERROR: soc sensor not published; got {list(d.published)[:5]}")
        failed = True
    elif d.published["sensor.predbat_deye_inv1_soc"] != 63.0:
        print("ERROR: soc value wrong")
        failed = True
    return failed


def test_schedule_roundtrip():
    """Published control entities read back into the schedule shape used by control derivation."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.local_schedule = {"INV1": {"reserve": 10, "charge": {"enable": True, "soc": 90, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 99, "power": 0}}}
    import tests.test_infra as ti
    ti.run_async(d.publish_schedule_settings_ha("INV1"))
    # Feed the published states back as HA state, then read them.
    for entity, state in list(d.published.items()):
        d.entity_states[entity] = "on" if state is True else ("off" if state is False else state)
    got = ti.run_async(d.get_schedule_settings_ha("INV1"))
    if got.get("charge", {}).get("soc") != 90:
        print(f"ERROR: charge soc round-trip {got.get('charge')}")
        failed = True
    if got.get("reserve") != 10:
        print(f"ERROR: reserve round-trip {got.get('reserve')}")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_publish.py -v > /tmp/deye_publish.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_publish.log`
Expected: FAIL — no attribute `publish_data`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    def _sensor_name(self, sn, leaf):
        """Return a namespaced DEYE sensor entity id."""
        return f"sensor.{self.prefix}_deye_{sn.lower()}_{leaf}"

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced DEYE control entity id."""
        return f"{domain}.{self.prefix}_deye_{sn.lower()}_{leaf}"

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "temperature": "°C"}
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=values[leaf], attributes={"unit_of_measurement": unit, "friendly_name": f"DEYE {sn} {leaf.replace('_', ' ').title()}"}, app="deye")

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        reserve = int(local.get("reserve", 0))
        self.dashboard_item(self._control_name("number", sn, "battery_schedule_reserve"), state=reserve, attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"DEYE {sn} Battery Schedule Reserve", "icon": "mdi:gauge"}, app="deye")
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            self.dashboard_item(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), state=window.get("start", "00:00"), attributes={"friendly_name": f"DEYE {sn} {direction.title()} Start", "icon": "mdi:clock-outline"}, app="deye")
            self.dashboard_item(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), state=window.get("end", "00:00"), attributes={"friendly_name": f"DEYE {sn} {direction.title()} End", "icon": "mdi:clock-outline"}, app="deye")
            self.dashboard_item(self._control_name("number", sn, f"battery_schedule_{direction}_soc"), state=int(window.get("soc", 0)), attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"DEYE {sn} {direction.title()} SoC", "icon": "mdi:gauge"}, app="deye")
            self.dashboard_item(self._control_name("number", sn, f"battery_schedule_{direction}_power"), state=int(window.get("power", 0)), attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": f"DEYE {sn} {direction.title()} Power", "icon": "mdi:flash"}, app="deye")
            self.dashboard_item(self._control_name("switch", sn, f"battery_schedule_{direction}_enable"), state="on" if window.get("enable") else "off", attributes={"friendly_name": f"DEYE {sn} {direction.title()} Enable", "icon": "mdi:check-circle-outline"}, app="deye")
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": f"DEYE {sn} Schedule Write", "icon": "mdi:content-save"}, app="deye")

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape used by control derivation."""
        schedule = {"reserve": int(float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0) or 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, f"battery_schedule_{direction}_enable"), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), default="00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), default="00:00"),
                "soc": int(float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_soc"), default=0) or 0)),
                "power": int(float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_power"), default=0) or 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_publish.py -v > /tmp/deye_publish.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_publish.log`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_publish.py
git commit -m "feat(deye): publish sensors and schedule control entities"
```

---

## Task 12: Event routing + reserve live-write

**Files:**
- Modify: `apps/predbat/deye.py` (add `select_event`, `number_event`, `switch_event`, `apply_reserve_live`, `apply_schedule`)
- Test: `apps/predbat/tests/test_deye_publish.py` (append)

**Interfaces:**
- Consumes: `get_schedule_settings_ha`, `apply_dynamic_control`, `device_values`.
- Produces: overrides of `select_event`/`number_event`/`switch_event` that update local schedule and route; `async apply_reserve_live(self, sn, reserve)` (immediate write path); `async apply_schedule(self, sn, force=True)` (called on write-button press).

- [ ] **Step 1: Write the failing test** (append)

```python
from unittest.mock import patch


def test_reserve_event_writes_immediately():
    """A reserve number_event pushes to DEYE at once (freeze-charge relies on it)."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    calls = []

    async def fake_apply_reserve(sn, reserve):
        calls.append((sn, reserve))
        return True

    import tests.test_infra as ti
    with patch.object(d, "apply_reserve_live", side_effect=fake_apply_reserve):
        ti.run_async(d.number_event("number.predbat_deye_inv1_battery_schedule_reserve", 25))
    if calls != [("INV1", 25)]:
        print(f"ERROR: reserve not written immediately: {calls}")
        failed = True
    return failed


def test_write_button_applies_schedule():
    """The write switch triggers a forced schedule apply."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    applied = []

    async def fake_apply(sn, force=True):
        applied.append((sn, force))
        return True

    import tests.test_infra as ti
    with patch.object(d, "apply_schedule", side_effect=fake_apply):
        ti.run_async(d.switch_event("switch.predbat_deye_inv1_battery_schedule_charge_write", "turn_on"))
    if applied != [("INV1", True)]:
        print(f"ERROR: write button did not apply: {applied}")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_publish.py -k "reserve_event or write_button" -v > /tmp/deye_publish.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_publish.log`
Expected: FAIL — `apply_reserve_live` / `apply_schedule` missing.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    def _sn_from_entity(self, entity_id):
        """Extract the inverter serial embedded in a DEYE entity id."""
        marker = f"_deye_"
        if marker not in entity_id:
            return None
        tail = entity_id.split(marker, 1)[1]
        for sn in self.device_list:
            if tail.startswith(sn.lower()):
                return sn
        return None

    async def apply_reserve_live(self, sn, reserve):
        """Write the reserve immediately via a forced control apply (freeze-charge hold)."""
        schedule = self.local_schedule.get(sn, {})
        schedule["reserve"] = int(reserve)
        self.local_schedule[sn] = schedule
        current_soc = self.device_values.get(sn, {}).get("soc", reserve)
        return await self.apply_dynamic_control(sn, schedule, current_soc, force=True)

    async def apply_schedule(self, sn, force=True):
        """Recompute from HA entities and push the schedule for one inverter."""
        schedule = await self.get_schedule_settings_ha(sn)
        current_soc = self.device_values.get(sn, {}).get("soc", schedule.get("reserve", 0))
        return await self.apply_dynamic_control(sn, schedule, current_soc, force=force)

    async def select_event(self, entity_id, value):
        """Handle a select (time) change: refresh local schedule from HA."""
        sn = self._sn_from_entity(entity_id)
        if sn:
            await self.get_schedule_settings_ha(sn)

    async def number_event(self, entity_id, value):
        """Handle a number change: reserve is written live, others just refresh local state."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if entity_id.endswith("battery_schedule_reserve"):
            await self.apply_reserve_live(sn, int(float(value)))
        else:
            await self.get_schedule_settings_ha(sn)

    async def switch_event(self, entity_id, service):
        """Handle a switch: the write button applies the schedule; enables refresh local state."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if entity_id.endswith("_write") and service in ("turn_on", "on"):
            await self.apply_schedule(sn, force=True)
        else:
            await self.get_schedule_settings_ha(sn)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_publish.py -k "reserve_event or write_button" -v > /tmp/deye_publish.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_publish.log`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_publish.py
git commit -m "feat(deye): route control events and write reserve live"
```

---

## Task 13: `automatic_config` (multi-inverter arg mapping)

**Files:**
- Modify: `apps/predbat/deye.py` (add `automatic_config`)
- Test: `apps/predbat/tests/test_deye_publish.py` (append)

**Interfaces:**
- Consumes: `set_arg`, `self.device_list`.
- Produces: `async automatic_config(self)` setting `inverter_type=["DeyeCloud", …]`, `num_inverters`, and the arg → entity map for every discovered inverter.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_automatic_config_maps_all_inverters():
    """automatic_config registers each inverter and maps the core control args."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INVA", "INVB"]
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti
    ti.run_async(d.automatic_config())
    if d.set_args.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {d.set_args.get('num_inverters')}")
        failed = True
    if d.set_args.get("inverter_type") != ["DeyeCloud", "DeyeCloud"]:
        print(f"ERROR: inverter_type {d.set_args.get('inverter_type')}")
        failed = True
    cs = d.set_args.get("charge_start_time")
    if not cs or cs[0] != "select.predbat_deye_inva_battery_schedule_charge_start_time":
        print(f"ERROR: charge_start_time map {cs}")
        failed = True
    if "inverter_mode" in d.set_args:
        print("ERROR: DEYE must not set inverter_mode (mode-less)")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_publish.py::test_automatic_config_maps_all_inverters -v > /tmp/deye_publish.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_publish.log`
Expected: FAIL — no attribute `automatic_config`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    async def automatic_config(self):
        """Register every discovered inverter as a DeyeCloud Predbat inverter."""
        devices = [sn.lower() for sn in self.device_list]
        n = len(devices)
        if not n:
            self.log("Warn: DEYE automatic_config found no inverters")
            return
        self.set_arg("inverter_type", ["DeyeCloud" for _ in devices])
        self.set_arg("num_inverters", n)
        self.set_arg("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        self.set_arg("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])
        self.set_arg("battery_temperature", [self._sensor_name(sn, "temperature") for sn in devices])
        self.set_arg("reserve", [self._control_name("number", sn, "battery_schedule_reserve") for sn in devices])
        self.set_arg("charge_start_time", [self._control_name("select", sn, "battery_schedule_charge_start_time") for sn in devices])
        self.set_arg("charge_end_time", [self._control_name("select", sn, "battery_schedule_charge_end_time") for sn in devices])
        self.set_arg("charge_limit", [self._control_name("number", sn, "battery_schedule_charge_soc") for sn in devices])
        self.set_arg("charge_rate", [self._control_name("number", sn, "battery_schedule_charge_power") for sn in devices])
        self.set_arg("scheduled_charge_enable", [self._control_name("switch", sn, "battery_schedule_charge_enable") for sn in devices])
        self.set_arg("discharge_start_time", [self._control_name("select", sn, "battery_schedule_export_start_time") for sn in devices])
        self.set_arg("discharge_end_time", [self._control_name("select", sn, "battery_schedule_export_end_time") for sn in devices])
        self.set_arg("discharge_target_soc", [self._control_name("number", sn, "battery_schedule_export_soc") for sn in devices])
        self.set_arg("discharge_rate", [self._control_name("number", sn, "battery_schedule_export_power") for sn in devices])
        self.set_arg("scheduled_discharge_enable", [self._control_name("switch", sn, "battery_schedule_export_enable") for sn in devices])
        self.set_arg("schedule_write_button", [self._control_name("switch", sn, "battery_schedule_charge_write") for sn in devices])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_publish.py::test_automatic_config_maps_all_inverters -v > /tmp/deye_publish.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_publish.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_publish.py
git commit -m "feat(deye): automatic_config multi-inverter arg mapping"
```

---

## Task 14: `run()` loop, token bootstrap, refresh tiers and `final()`

**Files:**
- Modify: `apps/predbat/deye.py` (add `run`, `final`)
- Test: `apps/predbat/tests/test_deye_api.py` (append)

**Interfaces:**
- Consumes: everything above.
- Produces: `async run(self, seconds, first) -> bool` (bootstrap token in `app_credentials` mode; discover devices on first/slow tier; poll telemetry + battery config; publish; `automatic_config` on first when `automatic`); `async final(self)`.

- [ ] **Step 1: Write the failing test** (append)

```python
def test_run_first_cycle_publishes_and_configures():
    """First run discovers, publishes and (when automatic) configures."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "tok"
    d.automatic = True
    seq = {"published": 0, "configured": 0}

    async def fake_dev_list():
        d.device_list = ["INV1"]
        return ["INV1"]

    async def fake_data(sn):
        d.device_values[sn] = {"soc": 55.0}
        return d.device_values[sn]

    async def fake_batt(sn):
        return {}

    async def fake_publish():
        seq["published"] += 1

    async def fake_pub_sched(sn):
        pass

    async def fake_get_sched(sn):
        return {}

    async def fake_auto():
        seq["configured"] += 1

    from unittest.mock import patch
    with patch.multiple(d, get_device_list=fake_dev_list, fetch_device_data=fake_data, fetch_battery_config=fake_batt, publish_data=fake_publish, publish_schedule_settings_ha=fake_pub_sched, get_schedule_settings_ha=fake_get_sched, automatic_config=fake_auto):
        ok = run_async_local(d.run(0, True))
    if not ok:
        print("ERROR: run returned falsy")
        failed = True
    if seq["published"] == 0 or seq["configured"] == 0:
        print(f"ERROR: run did not publish/configure {seq}")
        failed = True
    return failed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py::test_run_first_cycle_publishes_and_configures -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|Error" /tmp/deye_api.log`
Expected: FAIL — no attribute `run`.

- [ ] **Step 3: Write minimal implementation** (append to `DeyeAPI`)

```python
    async def run(self, seconds, first):
        """Main component loop: auth, discover, poll, publish, configure."""
        if self.oauth_auth_method == "app_credentials" and not getattr(self, "access_token", None):
            if not await self.fetch_token():
                self.log("Warn: DEYE token unavailable, skipping run")
                return False
        if not await self.check_and_refresh_oauth_token():
            self.log("Warn: DEYE OAuth token invalid, skipping run")
            return False

        if first or not self.device_list:
            await self.get_device_list()
        if not self.device_list:
            self.log("Error: DEYE no inverters found")
            return False

        for sn in self.device_list:
            try:
                await self.fetch_battery_config(sn)
                await self.fetch_device_data(sn)
                await self.get_schedule_settings_ha(sn)
            except Exception as e:
                self.log(f"Warn: DEYE poll failed for {sn}: {e}")

        await self.publish_data()
        for sn in self.device_list:
            await self.publish_schedule_settings_ha(sn)

        if first and self.automatic:
            await self.automatic_config()
        return True

    async def final(self):
        """Cleanup on shutdown."""
        self.log("Info: DeyeAPI shutdown")
```

> `self.oauth_auth_method` is set by `_init_oauth`; confirm the attribute name in `oauth_mixin.py` and adjust if the mixin exposes it differently (e.g. `self.auth_method`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd coverage && python -m pytest ../apps/predbat/tests/test_deye_api.py::test_run_first_cycle_publishes_and_configures -v > /tmp/deye_api.log 2>&1; grep -E "passed|failed|ERROR" /tmp/deye_api.log`
Expected: `1 passed`.

- [ ] **Step 5: Commit**

```bash
git add apps/predbat/deye.py apps/predbat/tests/test_deye_api.py
git commit -m "feat(deye): add run loop, token bootstrap and shutdown"
```

---

## Task 15: Register tests, full suite, pre-commit, docs, close PR #3917

**Files:**
- Modify: `apps/predbat/unit_test.py` (import + `TEST_REGISTRY` entries)
- Modify: `docs/inverter-setup.md`, `docs/components.md`, `docs/apps-yaml.md`
- Test: whole DEYE suite via `run_all`

**Interfaces:**
- Consumes: all DEYE test modules.
- Produces: registered `deye_*` test entries; user docs.

- [ ] **Step 1: Add aggregators to each DEYE test module**

Add a `run_<name>_tests(my_predbat)` aggregator to `test_deye_api.py`, `test_deye_oauth.py`, `test_deye_control.py`, `test_deye_publish.py`, `test_deye_const.py`, `test_deye_config.py`, following `test_fox_oauth.py:342`. Example for `test_deye_control.py`:

```python
def run_deye_control_tests(my_predbat):
    """Run all DEYE control-logic tests."""
    failed = False
    for name, fn in [
        ("derive_table", test_derive_control_state_table),
        ("tou_slots", test_build_tou_slots_charge_window),
        ("payload", test_build_dynamic_payload_and_equality),
        ("apply_suppress", test_apply_dynamic_control_suppresses_when_unchanged),
        ("poll_order", test_poll_order_success),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_control.{name}: {e}")
            import traceback
            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register in `unit_test.py`**

Add imports beside the Fox ones and entries to `TEST_REGISTRY`:

```python
from tests.test_deye_const import run_deye_const_tests
from tests.test_deye_config import run_deye_config_tests
from tests.test_deye_api import run_deye_api_tests
from tests.test_deye_oauth import run_deye_oauth_tests
from tests.test_deye_control import run_deye_control_tests
from tests.test_deye_publish import run_deye_publish_tests
```

```python
        ("deye_const", run_deye_const_tests, "DEYE constants tests", False),
        ("deye_config", run_deye_config_tests, "DEYE config/INVERTER_DEF tests", False),
        ("deye_api", run_deye_api_tests, "DEYE API tests", False),
        ("deye_oauth", run_deye_oauth_tests, "DEYE auth tests", False),
        ("deye_control", run_deye_control_tests, "DEYE control-logic tests", False),
        ("deye_publish", run_deye_publish_tests, "DEYE publish/config tests", False),
```

- [ ] **Step 3: Run the DEYE suite via run_all**

Run: `cd coverage && ./run_all -k deye > /tmp/deye_all.log 2>&1; grep -iE "fail|error|pass|deye_" /tmp/deye_all.log | tail -40`
Expected: all `deye_*` tests pass, no failures/exceptions.

- [ ] **Step 4: Write the docs**

- `docs/inverter-setup.md`: a "DEYE Cloud" section — create a developer app at `developer.deyecloud.com` (App ID/Secret), pick the data centre (`eu`/`am`/`india`), add-on config (`deye_app_id`/`deye_app_secret`/`deye_username`/`deye_password`/`deye_data_center`, optional `deye_company_id`) vs Predbat.com (token injected), and that Predbat auto-configures the inverter when `deye_automatic: True`.
- `docs/components.md`: add a DEYE Cloud row to the component table (monitoring + battery control, both deployment modes).
- `docs/apps-yaml.md`: document the `deye_*` args with a minimal example block.

- [ ] **Step 5: Pre-commit and commit**

Run: `./run_pre_commit > /tmp/deye_precommit.log 2>&1; tail -30 /tmp/deye_precommit.log` (re-stage cspell dictionary if auto-sorted).

```bash
git add apps/predbat/unit_test.py apps/predbat/tests/test_deye_*.py docs/inverter-setup.md docs/components.md docs/apps-yaml.md .cspell/custom-dictionary-workspace.txt
git commit -m "test(deye): register DEYE tests; docs(deye): inverter setup, components, apps.yaml"
```

- [ ] **Step 6: Close PR #3917**

```bash
gh pr close 3917 --comment "Superseded by the DEYE Cloud integration built to the design in docs/superpowers/specs/2026-07-19-deye-cloud-inverter-integration-design.md (branch feat/deye-cloud-inverter). The value-injection component change is out of scope and can land separately if wanted."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task(s) |
|---|---|
| `DeyeCloud` INVERTER_DEF, mode-less flags | 2 |
| Config items / args, both auth modes | 2, 3, 4 |
| Auth (`app_credentials` + `oauth`, sha256, email/username, 401 refresh) | 4 |
| Data-centre base URLs | 1, 3 |
| Device discovery (stations → inverters, pagination, filter) | 5 |
| Telemetry parse (`dataList` dynamic keys) + battery config | 6 |
| Internal work-mode derivation table (charge/freeze/hold/export/freeze-export/idle) | 7 |
| Reserve as freeze-charge mechanism + 99% freeze-export sentinel | 7, 12 |
| Window → 6-slot mapping | 8 |
| Combined `strategy_dynamic_control` write + change detection | 9, 10 |
| Async order polling (`orderId` / `get_order_result`) | 10 |
| Sensors + schedule control entities, Fox-style naming | 11 |
| Event routing + reserve live-write | 12 |
| `automatic_config` multi-inverter | 13 |
| `run()` loop, caching/refresh tiers, `final()` | 14 |
| Tests registered + docs + close PR #3917 | 15 |
| Spike-verified contract items | 0 (values isolated in `deye_const.py`, Task 1) |

**Placeholder scan:** No `TODO`/`TBD`/"handle edge cases"; the `# VERIFY@SPIKE` markers are concrete default values with a named verification task (0), not placeholders.

**Type consistency:** the `schedule` dict shape (`reserve`, `charge`/`export` → `enable/soc/power/start/end`) is produced by `get_schedule_settings_ha` (Task 11) and consumed identically by `derive_control_state`/`build_tou_slots`/`build_dynamic_payload` (Tasks 7–9). The control-state dict (`behaviour/work_mode/grid_charge/solar_sell/slot_soc/power`) is consistent across Tasks 7–9. Entity-name helpers `_sensor_name`/`_control_name` (Task 11) are reused by Tasks 12–13.

**Caveats folded into tasks:** `oauth_mixin` attribute name (`oauth_auth_method` vs `auth_method`) flagged in Task 14; DEYE refresh-token mechanism flagged in Tasks 0/4; per-slot field names and telemetry keys isolated to `deye_const.py`.

## Notes for execution

- Some tests reference the shared harness (`run_async`, `create_aiohttp_mock_session`, `create_aiohttp_mock_response`) in `tests/test_infra.py` — confirm those helpers exist and match the signatures used in `test_fox_oauth.py`; if a helper differs, adapt the test, not the harness.
- After the Task 0 spike, only `deye_const.py` values should need changing; if a wire contract turns out structurally different (e.g. per-slot grid-charge is a top-level action only), adjust `build_tou_slots`/`build_dynamic_payload` in isolation and keep `derive_control_state` and its table test intact.

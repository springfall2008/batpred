# AlphaESS Cloud Component Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an AlphaESS Open API cloud component to Predbat so an AlphaESS SMILE/Storion hybrid inverter can be monitored and driven from a developer AppID and AppSecret, with no local hardware access.

**Architecture:** A standalone phase-1 `ComponentBase` subclass (`alphaess.py`) plus its constants module (`alphaess_const.py`), modelled on `sunsynk.py`/`deye.py`. A native `aiohttp` client signs every request with `sha512(appId + appSecret + timeStamp)`. Predbat's control entities map straight onto the AlphaESS schedule fields — the inverter does the timing — with a four-tier poll loop (static / config / power / energy) and writes gated by change detection, a minimum interval, and Predbat's read-only switch.

**Tech Stack:** Python 3, `aiohttp`, `hashlib`, Predbat's `ComponentBase` / `MockBase` / Storage component, `TestHAInterface` from `tests/test_infra.py`.

**Spec:** `docs/superpowers/specs/2026-08-22-alphaess-cloud-integration-design.md`

## Global Constraints

- **Branch:** `feat/alphaess-cloud-component` (already created; the spec is committed there).
- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every function *and* every class.
- **Spell check:** British English (`en-gb`) via CSpell. Add unknown words to `.cspell/custom-dictionary-workspace.txt`. That file is auto-sorted on commit, so **re-stage it after running pre-commit**.
- **Naming:** `lower_case_with_underscores`.
- **Tests:** run from the `coverage/` directory. **Always save test output to a file and grep the file afterwards** — never pipe straight to grep.
- **Every `run_*_tests` function takes `my_predbat` and returns truthy on failure.** `unit_test.py` calls `test_failed = func(my_predbat)`.
- **Individual test functions** end with `assert not failed, "test_name"`; the runner catches the exception and reports it.
- **Storage:** never touch the filesystem directly — use the Storage component via `self.storage`.
- **Unit tests are required for all new code** (CLAUDE.md).
- **Pre-commit:** CLAUDE.md says `./run_pre_commit`, but **that script does not exist in
  this repo**. The working invocation is
  `coverage/venv/bin/pre-commit run --all-files`
  (`pre-commit` is not on PATH — it lives in the coverage venv). Confirm the exit status;
  "files were modified by this hook" is a FAILURE, not a pass.
- **ruff is configured `--select=F401 --fix`**, so it silently auto-DELETES an unused
  import and then reports failure. Add each import in the same task that first uses it,
  never ahead of time.
- **Copyright header** on every new file — copy the 5-line banner from `apps/predbat/sunsynk.py`.
- **API constant values, copied verbatim from the spec:**
  - Base URL: `https://openapi.alphaess.com/api`
  - Sign: `sha512(appId + appSecret + timeStamp)`, lower-case hex
  - Times: `HH:mm`, 15-minute grid, min `00:00`, max `23:45`
  - Grid sign: `pgrid` positive = importing (Predbat wants negative on import)
  - Return codes: `200` ok, `6001` param, `6002` SN not bound, `6003` already bound, `6004` checkcode, `6005` appId not bound, `6006` timestamp, `6007`/`6010`/`6012` sign, `6008` set failed, `6009` whitelist, `6017` no permission, `6038` SN unknown, `6042` offline, `6046` verification code, `6053` too fast

---

## Setup

- [ ] **Step 0: Confirm the branch and environment**

```bash
cd /Users/treforsouthwell/predbat/batpred
git branch --show-current          # expect: feat/alphaess-cloud-component
ls docs/superpowers/specs/2026-08-22-alphaess-cloud-integration-design.md
```

Read the spec before starting. Every task below argues from it.

```bash
cd coverage && source setup.csh     # first time only: creates venv, installs deps
```

---

## Task 1: Constants module

**Files:**
- Create: `apps/predbat/alphaess_const.py`
- Test: `apps/predbat/tests/test_alphaess_const.py`
- Modify: `apps/predbat/unit_test.py` (import + registry entry)

**Interfaces:**
- Consumes: nothing.
- Produces: `ALPHAESS_BASE_URL: str`, `ALPHAESS_ENDPOINTS: dict[str, str]`, `ALPHAESS_RETURN_CODES: dict[int, str]`, `ALPHAESS_TIMEOUT: int`, `ALPHAESS_RETRIES: int`, `ALPHAESS_TTL_STATIC/CONFIG/POWER/ENERGY: int` (minutes), `ALPHAESS_STORAGE_MODULE: str`, `ALPHAESS_CACHE_STATIC/CONFIG/RATINGS/CONTROL: str`, `ALPHAESS_TELEMETRY: dict`, `ALPHAESS_ENERGY: dict`, `ALPHAESS_HISTORY: dict`, `ALPHAESS_AC_COUPLED_MODELS: list`, `ALPHAESS_DEBUG_REDACT_KEYS: tuple`, `ALPHAESS_SETTLE_POLLS: int`, `ALPHAESS_LIVE_FAIL_LIMIT: int`, `snap_time_grid(hm: str, direction: str) -> str`, `hhmmss_to_hhmm(value: str) -> str`, `hm_to_minutes(hm: str) -> int`, `window_is_empty(start: str, end: str) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_alphaess_const.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS Cloud constants
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS Cloud constants module (alphaess_const.py)."""

from alphaess_const import (
    ALPHAESS_BASE_URL,
    ALPHAESS_ENDPOINTS,
    ALPHAESS_RETURN_CODES,
    ALPHAESS_TELEMETRY,
    ALPHAESS_ENERGY,
    ALPHAESS_HISTORY,
    ALPHAESS_AC_COUPLED_MODELS,
    snap_time_grid,
    hhmmss_to_hhmm,
    hm_to_minutes,
    window_is_empty,
)


def test_alphaess_base_url_and_endpoints():
    """Every endpoint the component calls is declared and hangs off the documented host."""
    failed = False
    if ALPHAESS_BASE_URL != "https://openapi.alphaess.com/api":
        print(f"ERROR: base url {ALPHAESS_BASE_URL}")
        failed = True
    for endpoint in (
        "ess_list",
        "last_power",
        "one_day_power",
        "one_date_energy",
        "sum_data",
        "charge_config",
        "update_charge_config",
        "discharge_config",
        "update_discharge_config",
        "time_charge",
        "set_time_charge",
        "verification_code",
        "bind",
        "unbind",
    ):
        if endpoint not in ALPHAESS_ENDPOINTS:
            print(f"ERROR: endpoint {endpoint} missing")
            failed = True
    assert not failed, "test_alphaess_base_url_and_endpoints"


def test_alphaess_return_codes_cover_the_documented_table():
    """Every code the component branches on carries a human description."""
    failed = False
    for code in (6001, 6002, 6003, 6004, 6005, 6006, 6007, 6008, 6009, 6010, 6012, 6017, 6038, 6042, 6046, 6053):
        if code not in ALPHAESS_RETURN_CODES:
            print(f"ERROR: return code {code} missing")
            failed = True
    assert not failed, "test_alphaess_return_codes_cover_the_documented_table"


def test_snap_time_grid_rounds_inward():
    """Windows snap INWARD - start up, end down - so Predbat never claims time the device ignores.

    Off-grid values are accepted by the API and silently ignored by the inverter, which is
    the worst possible failure mode, so this is enforced here rather than hoped for.
    """
    failed = False
    cases = [
        ("00:00", "start", "00:00"),
        ("01:07", "start", "01:15"),
        ("01:15", "start", "01:15"),
        ("05:01", "end", "05:00"),
        ("05:45", "end", "05:45"),
        ("23:59", "end", "23:45"),
        ("24:00", "end", "23:45"),
        ("23:50", "start", "23:45"),
    ]
    for value, direction, expect in cases:
        got = snap_time_grid(value, direction)
        if got != expect:
            print(f"ERROR: snap_time_grid({value!r}, {direction!r}) = {got} != {expect}")
            failed = True
    assert not failed, "test_snap_time_grid_rounds_inward"


def test_hhmmss_to_hhmm():
    """Predbat's HH:MM:SS entities convert to the API's HH:mm."""
    failed = False
    for value, expect in (("01:30:00", "01:30"), ("01:30", "01:30"), ("", "00:00"), (None, "00:00")):
        got = hhmmss_to_hhmm(value)
        if got != expect:
            print(f"ERROR: hhmmss_to_hhmm({value!r}) = {got} != {expect}")
            failed = True
    assert not failed, "test_hhmmss_to_hhmm"


def test_window_is_empty_detects_disabled_and_collapsed():
    """start == end is the documented 'disabled', and an inverted window counts as empty too."""
    failed = False
    for start, end, expect in (("00:00", "00:00", True), ("01:00", "01:00", True), ("05:00", "04:00", True), ("01:00", "02:00", False)):
        got = window_is_empty(start, end)
        if got != expect:
            print(f"ERROR: window_is_empty({start}, {end}) = {got} != {expect}")
            failed = True
    if hm_to_minutes("02:15") != 135:
        print("ERROR: hm_to_minutes('02:15') != 135")
        failed = True
    assert not failed, "test_window_is_empty_detects_disabled_and_collapsed"


def test_field_maps_cover_the_predbat_args():
    """Telemetry, energy and history maps name every arg automatic_config binds."""
    failed = False
    for leaf in ("soc", "battery_power", "grid_power", "pv_power", "load_power"):
        if leaf not in ALPHAESS_TELEMETRY:
            print(f"ERROR: telemetry leaf {leaf} missing")
            failed = True
    for leaf in ("import_today", "export_today", "pv_today"):
        if leaf not in ALPHAESS_ENERGY:
            print(f"ERROR: energy leaf {leaf} missing")
            failed = True
    for leaf in ("soc", "pv_power", "load_power"):
        if leaf not in ALPHAESS_HISTORY:
            print(f"ERROR: history leaf {leaf} missing")
            failed = True
    # Ships empty on purpose: AlphaESS model names do not encode coupling, so an invented
    # table would be a guess. Entries are added only as testers confirm them.
    if ALPHAESS_AC_COUPLED_MODELS:
        print(f"ERROR: ALPHAESS_AC_COUPLED_MODELS should ship empty, got {ALPHAESS_AC_COUPLED_MODELS}")
        failed = True
    assert not failed, "test_field_maps_cover_the_predbat_args"


def run_alphaess_const_tests(my_predbat):
    """Run all AlphaESS constants tests."""
    failed = False
    for name, fn in [
        ("base_url_and_endpoints", test_alphaess_base_url_and_endpoints),
        ("return_codes", test_alphaess_return_codes_cover_the_documented_table),
        ("snap_time_grid", test_snap_time_grid_rounds_inward),
        ("hhmmss_to_hhmm", test_hhmmss_to_hhmm),
        ("window_is_empty", test_window_is_empty_detects_disabled_and_collapsed),
        ("field_maps", test_field_maps_cover_the_predbat_args),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_const > /tmp/t1.log 2>&1; tail -30 /tmp/t1.log
```

Expected: FAIL — the test is not registered yet, so `--test alphaess_const` matches nothing, and `alphaess_const` does not import.

- [ ] **Step 3: Write `alphaess_const.py`**

Create `apps/predbat/alphaess_const.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# AlphaESS Open API constants
# -----------------------------------------------------------------------------

"""Constants and pure helpers for the AlphaESS Open API component.

Kept separate from alphaess.py so the wire format, the documented return codes and the
15-minute time grid can be tested without constructing a component.
"""

ALPHAESS_BASE_URL = "https://openapi.alphaess.com/api"

ALPHAESS_ENDPOINTS = {
    "ess_list": "/getEssList",
    "last_power": "/getLastPowerData",
    "one_day_power": "/getOneDayPowerBySn",
    "one_date_energy": "/getOneDateEnergyBySn",
    "sum_data": "/getSumDataForCustomer",
    "charge_config": "/getChargeConfigInfo",
    "update_charge_config": "/updateChargeConfigInfo",
    "discharge_config": "/getDisChargeConfigInfo",
    "update_discharge_config": "/updateDisChargeConfigInfo",
    "time_charge": "/getTimeChargeBySn",
    "set_time_charge": "/setTimeChargeBySn",
    "verification_code": "/getVerificationCode",
    "bind": "/bindSn",
    "unbind": "/unBindSn",
}

# Published on the developer portal. 6017 and 10001 are returned by the live API but are
# not in the portal's table - kept here anyway because the component branches on 6017.
ALPHAESS_RETURN_CODES = {
    6001: "Parameter error",
    6002: "The SN is not bound to the user",
    6003: "You have bound this SN",
    6004: "CheckCode error",
    6005: "This appId is not bound to the SN",
    6006: "Timestamp error",
    6007: "Sign verification error",
    6008: "Set failed",
    6009: "Whitelist verification failed",
    6010: "Sign is empty",
    6011: "timestamp is empty",
    6012: "AppId is empty",
    6016: "Data does not exist or has been deleted",
    6017: "No operation permissions",
    6026: "internal error",
    6029: "operation failed",
    6038: "system sn does not exist",
    6042: "system offline",
    6046: "Verification code error",
    6053: "The request was too fast, please try again later",
    10001: "Parameter error (malformed request body)",
}

ALPHAESS_CODE_OK = 200
ALPHAESS_CODE_ALREADY_BOUND = 6003
ALPHAESS_CODE_NOT_BOUND = 6005
ALPHAESS_CODE_NO_PERMISSION = 6017
ALPHAESS_CODE_OFFLINE = 6042
ALPHAESS_CODE_TOO_FAST = 6053
ALPHAESS_CODE_TIMESTAMP = 6006
ALPHAESS_CODE_SIGN = (6007, 6010, 6012)

ALPHAESS_TIMEOUT = 30
ALPHAESS_RETRIES = 3
# The signature is only valid within 300 seconds of server time.
ALPHAESS_SIGN_WINDOW_SECONDS = 300

# Tier TTLs in minutes.
ALPHAESS_TTL_STATIC = 8 * 60
ALPHAESS_TTL_CONFIG = 30
ALPHAESS_TTL_POWER = 1
ALPHAESS_TTL_ENERGY = 5
# Power tier interval once a serial has been demoted to the history path - matches the
# resolution getOneDayPowerBySn actually has, and keeps the ~288-record payload off a
# 60-second loop.
ALPHAESS_TTL_POWER_DEMOTED = 5
# Consecutive getLastPowerData failures before a serial is demoted to history.
ALPHAESS_LIVE_FAIL_LIMIT = 3

ALPHAESS_STORAGE_MODULE = "alphaess"
ALPHAESS_CACHE_STATIC = "static"
ALPHAESS_CACHE_CONFIG = "config"
ALPHAESS_CACHE_RATINGS = "ratings"
ALPHAESS_CACHE_CONTROL = "control"

# Polls a written payload is considered pending for before an unexpected read-back counts
# as external interference (the phone app, or another Predbat instance).
ALPHAESS_SETTLE_POLLS = 3

ALPHAESS_DEBUG_REDACT_KEYS = ("appSecret", "sign", "app_secret", "code", "checkCode")

# getLastPowerData field -> published sensor leaf. Watts, except soc which is a percent.
ALPHAESS_TELEMETRY = {
    "soc": "soc",
    "battery_power": "pbat",
    "grid_power": "pgrid",
    "pv_power": "ppv",
    "load_power": "pload",
    "ev_power": "pev",
}

# pgrid is positive on IMPORT; Predbat wants negative on import, so this one is negated
# on publish. Listed explicitly rather than inferred so the intent survives a refactor.
ALPHAESS_TELEMETRY_NEGATE = ("grid_power",)

# Daily kWh counters. load_today comes from getSumDataForCustomer (eload); the rest come
# from getOneDateEnergyBySn. getOneDateEnergyBySn has no load field at all.
ALPHAESS_ENERGY = {
    "import_today": "eInput",
    "export_today": "eOutput",
    "pv_today": "epv",
    "battery_charge_today": "eCharge",
    "battery_discharge_today": "eDischarge",
    "grid_charge_today": "eGridCharge",
}
ALPHAESS_ENERGY_LOAD_FIELD = "eload"

# getOneDayPowerBySn sample field -> sensor leaf, used when getLastPowerData is unavailable.
# cbat carries SOC. The portal documents it as "cobat" and the live API returns "cbat" -
# reading the portal name silently yields None, which looks exactly like "no SOC here", so
# both spellings are tried, cbat first.
ALPHAESS_HISTORY = {
    "soc": ("cbat", "cobat"),
    "pv_power": ("ppv",),
    "load_power": ("load",),
}
ALPHAESS_HISTORY_FEED_IN = "feedIn"
ALPHAESS_HISTORY_GRID_CHARGE = "gridCharge"

# Ships EMPTY on purpose. AlphaESS model names do not encode coupling the way GivEnergy's
# do (there is no "ac" substring to match), and the Home Assistant integration's known
# model list contains no confirmed AC-coupled entry. Inventing a table here would be a
# guess; entries go in only as testers confirm them.
ALPHAESS_AC_COUPLED_MODELS = []

ALPHAESS_TIME_STEP_MINUTES = 15
ALPHAESS_TIME_MAX = "23:45"
ALPHAESS_TIME_DISABLED = "00:00"


def hhmmss_to_hhmm(value):
    """Convert Predbat's HH:MM:SS control-entity value to the API's HH:mm.

    Returns "00:00" for anything unusable, which is the documented "disabled" value, so a
    missing or malformed entity disables a window rather than raising inside the poll loop.
    """
    text = str(value or "").strip()
    if not text:
        return ALPHAESS_TIME_DISABLED
    parts = text.split(":")
    if len(parts) < 2:
        return ALPHAESS_TIME_DISABLED
    try:
        return "{:02d}:{:02d}".format(int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        return ALPHAESS_TIME_DISABLED


def hm_to_minutes(hm):
    """Convert HH:mm to minutes since midnight, returning 0 for anything unusable."""
    text = hhmmss_to_hhmm(hm)
    hours, _, mins = text.partition(":")
    try:
        return int(hours) * 60 + int(mins)
    except (TypeError, ValueError):
        return 0


def snap_time_grid(value, direction):
    """Snap HH:mm onto the API's 15-minute grid, INWARD.

    Start times round up and end times round down, so a snapped window is never wider than
    the one Predbat asked for. Off-grid values are accepted by the API and then silently
    ignored by the inverter, so getting this wrong produces a window that appears to have
    been written and never runs - hence snapping here rather than trusting the caller.

    23:45 is the documented maximum, so a 24:00 end (Predbat's midnight) lands on 23:45.
    """
    minutes = hm_to_minutes(value)
    step = ALPHAESS_TIME_STEP_MINUTES
    if direction == "start":
        snapped = ((minutes + step - 1) // step) * step
    else:
        snapped = (minutes // step) * step
    max_minutes = hm_to_minutes(ALPHAESS_TIME_MAX)
    snapped = max(0, min(snapped, max_minutes))
    return "{:02d}:{:02d}".format(snapped // 60, snapped % 60)


def window_is_empty(start, end):
    """Return True when a window carries no time the inverter would act on.

    Covers both the documented "disabled" form (start == end) and a window that snapping
    has collapsed or inverted. An inverted window must NOT be written as a wrap-around:
    wrap behaviour is undocumented, so it is disabled instead.
    """
    return hm_to_minutes(end) <= hm_to_minutes(start)
```

- [ ] **Step 4: Register the test module**

In `apps/predbat/unit_test.py`, add the import next to the other cloud-inverter test imports (near line 190):

```python
from tests.test_alphaess_const import run_alphaess_const_tests
```

And add to `TEST_REGISTRY` next to the Sunsynk entries (near line 495):

```python
        ("alphaess_const", run_alphaess_const_tests, "AlphaESS constants tests", False),
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_const > /tmp/t1.log 2>&1; grep -E "PASSED|FAILED|ERROR|EXCEPTION" /tmp/t1.log
```

Expected: `**** alphaess_const: PASSED ...`

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess_const.py apps/predbat/tests/test_alphaess_const.py apps/predbat/unit_test.py
git commit -m "feat(alphaess): add AlphaESS Open API constants and time-grid helpers"
```

---

## Task 2: Signed client core

**Files:**
- Create: `apps/predbat/alphaess.py`
- Test: `apps/predbat/tests/test_alphaess_api.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: everything from Task 1.
- Produces:
  - `class AlphaESSAPI(ComponentBase)` with `initialize(app_id="", app_secret="", inverter_sn=None, automatic=False, automatic_ignore_pv=False, control_enable=True, battery_rate_max=None, api_delay=2, min_write_interval=300, **kwargs)`
  - `AlphaESSAPI.api_debug: bool = True` (class attribute)
  - `_headers(self) -> dict`
  - `async _request(self, method, endpoint_key, params=None, body=None) -> tuple[int, object]` returning `(code, data)`; `code` is the envelope code, `-1` for a transport failure
  - `async _get(self, endpoint_key, params=None) -> tuple[int, object]`
  - `async _post(self, endpoint_key, body=None) -> tuple[int, object]`
  - `self.last_api_error: str`, `self.discovery_ok: bool | None`
  - `class MockAlphaESS(AlphaESSAPI)` in the test module — **every later test module imports this**

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_alphaess_api.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS Cloud API component (``alphaess.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import hashlib
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from alphaess import AlphaESSAPI
from alphaess_const import ALPHAESS_BASE_URL, ALPHAESS_ENDPOINTS
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockAlphaESS(AlphaESSAPI):
    """Test double: build an AlphaESSAPI without the full component lifecycle."""

    def __init__(self, app_id="alphatestappid00000", app_secret="secret0000000000", inverter_sn=None, control_enable=True, automatic=False):
        """Set up a minimal AlphaESSAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-alphaess-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.minutes_now = 0
        self.state = {}
        self.published = {}
        self.external_state = {}
        self.initialize(
            app_id=app_id,
            app_secret=app_secret,
            inverter_sn=inverter_sn,
            automatic=automatic,
            control_enable=control_enable,
        )

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity instead of reaching Home Assistant."""
        self.published[entity] = {"state": state, "attributes": attributes}
        self.state[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back whatever the test (or dashboard_item) put in self.state."""
        return self.state.get(entity_id, default)

    async def set_state_external(self, entity_id, state, attributes={}):
        """Record a Predbat CONFIG_ITEMS switch change instead of reaching Home Assistant."""
        self.external_state[entity_id] = state

    def set_arg_auto(self, arg, value):
        """Record an auto-discovered apps.yaml binding."""
        self.base.args[arg] = value

    @property
    def storage(self):
        """No Storage component in unit tests - matches a standalone CLI run."""
        return None


def _envelope(code=200, data=None, msg=None, exp_msg=None):
    """Build an AlphaESS response envelope.

    msg defaults to "Success" ONLY for code 200. The client treats msg == "Success" as
    success regardless of code (the periodic endpoints report status in msg/info rather
    than code), so a helper that defaulted every envelope to "Success" would make a
    failure envelope read as a success and let tests pass for the wrong reason.
    """
    if msg is None:
        msg = "Success" if code == 200 else "Failed"
    return {"code": code, "msg": msg, "expMsg": exp_msg, "extra": None, "data": data}


def test_alphaess_sign_matches_the_documented_algorithm():
    """sign is sha512(appId + appSecret + timeStamp), lower-case hex, with both spellings sent."""
    failed = False
    client = MockAlphaESS(app_id="alphaef7900ee81dbbce9", app_secret="c2d2ef6c047c49678e2c332fb2d74c3c")
    with patch("alphaess.time.time", return_value=1676353875):
        headers = client._headers()
    expect = hashlib.sha512(b"alphaef7900ee81dbbce9c2d2ef6c047c49678e2c332fb2d74c3c1676353875").hexdigest()
    if headers.get("sign") != expect:
        print(f"ERROR: sign {headers.get('sign')} != {expect}")
        failed = True
    if headers.get("appId") != "alphaef7900ee81dbbce9":
        print(f"ERROR: appId {headers.get('appId')}")
        failed = True
    # The reference client sends both spellings; the API is documented with timeStamp.
    if headers.get("timeStamp") != "1676353875" or headers.get("timestamp") != "1676353875":
        print(f"ERROR: timestamp headers {headers}")
        failed = True
    assert not failed, "test_alphaess_sign_matches_the_documented_algorithm"


def test_alphaess_request_returns_code_and_data():
    """_request surfaces the envelope code, which is the only way to judge a write."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, [{"sysSn": "AL70"}]))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._get("ess_list"))
    if code != 200:
        print(f"ERROR: code {code}")
        failed = True
    if not data or data[0].get("sysSn") != "AL70":
        print(f"ERROR: data {data}")
        failed = True
    assert not failed, "test_alphaess_request_returns_code_and_data"


def test_alphaess_write_failure_is_distinguishable_from_success():
    """A write answers data:null either way, so the code alone separates 6008 from 200."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6008, None, msg="Set failed"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._post("update_charge_config", body={"sysSn": "AL70"}))
    if code != 6008:
        print(f"ERROR: code {code} should be 6008")
        failed = True
    if data is not None:
        print(f"ERROR: data {data} should be None")
        failed = True
    if "Set failed" not in client.last_api_error:
        print(f"ERROR: last_api_error {client.last_api_error!r}")
        failed = True
    assert not failed, "test_alphaess_write_failure_is_distinguishable_from_success"


def test_alphaess_info_field_counts_as_success():
    """The periodic endpoints report status in `info`, not `msg`."""
    failed = False
    client = MockAlphaESS()
    body = {"code": 200, "info": "Success", "expMsg": None, "data": {"sysSn": "AL70"}}
    response = create_aiohttp_mock_response(status=200, json_data=body)
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._get("time_charge", params={"sysSn": "AL70"}))
    if code != 200 or not data:
        print(f"ERROR: code {code} data {data}")
        failed = True
    assert not failed, "test_alphaess_info_field_counts_as_success"


def test_alphaess_clock_skew_is_reported_as_a_clock_problem():
    """6006 must not read as bad credentials - the symptoms are otherwise identical."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6006, None, msg="Timestamp error"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, _ = run_async_local(client._get("ess_list"))
    if code != 6006:
        print(f"ERROR: code {code}")
        failed = True
    if not any("clock" in message.lower() for message in client.log_messages):
        print(f"ERROR: no clock-skew log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_clock_skew_is_reported_as_a_clock_problem"


def test_alphaess_expmsg_is_logged_when_present():
    """expMsg is the only field that names the bad parameter; msg just says 'Parameter error'."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(6001, None, msg="Parameter error", exp_msg="time list is null"))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client._post("set_time_charge", body={"sysSn": "AL70"}))
    if not any("time list is null" in message for message in client.log_messages):
        print(f"ERROR: expMsg not logged, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_expmsg_is_logged_when_present"


def test_alphaess_secret_never_reaches_the_log():
    """api_debug traces every call, so the redaction has to actually work."""
    failed = False
    client = MockAlphaESS(app_secret="hunter2secretvalue")
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client._get("ess_list"))
    for message in client.log_messages:
        if "hunter2secretvalue" in message:
            print(f"ERROR: secret leaked in log: {message}")
            failed = True
    assert not failed, "test_alphaess_secret_never_reaches_the_log"


def test_alphaess_transport_failure_returns_minus_one():
    """A transport error is not an API verdict; it must be distinguishable from one."""
    failed = False
    client = MockAlphaESS()
    session = create_aiohttp_mock_session(exception=Exception("connection reset"))
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        code, data = run_async_local(client._get("ess_list"))
    if code != -1:
        print(f"ERROR: transport failure code {code} should be -1")
        failed = True
    if data is not None:
        print(f"ERROR: data {data} should be None")
        failed = True
    assert not failed, "test_alphaess_transport_failure_returns_minus_one"


def run_alphaess_api_tests(my_predbat):
    """Run all AlphaESS API tests."""
    failed = False
    for name, fn in [
        ("sign", test_alphaess_sign_matches_the_documented_algorithm),
        ("request_code_and_data", test_alphaess_request_returns_code_and_data),
        ("write_failure_distinguishable", test_alphaess_write_failure_is_distinguishable_from_success),
        ("info_field_success", test_alphaess_info_field_counts_as_success),
        ("clock_skew", test_alphaess_clock_skew_is_reported_as_a_clock_problem),
        ("expmsg_logged", test_alphaess_expmsg_is_logged_when_present),
        ("secret_redacted", test_alphaess_secret_never_reaches_the_log),
        ("transport_failure", test_alphaess_transport_failure_returns_minus_one),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t2.log 2>&1; tail -20 /tmp/t2.log
```

Expected: FAIL — `alphaess` module does not exist.

- [ ] **Step 3: Write the client core**

Create `apps/predbat/alphaess.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# AlphaESS Open API Library
# -----------------------------------------------------------------------------

"""AlphaESS Open API integration for Predbat.

Registers each discovered AlphaESS system as an ``AlphaESSCloud`` Predbat inverter,
publishing monitoring sensors and schedule control entities. Predbat's controls map
straight onto the AlphaESS schedule fields - gridCharge/timeChaf/batHighCap for charging
and ctrDis/timeDisf/batUseCap for export - and the inverter does the timing, so there is
no per-instant work mode to derive.

Auth is stateless: every request carries appId, timeStamp and
sign = sha512(appId + appSecret + timeStamp). There is no token and no refresh, so the
self-hosted add-on and the Predbat.com SaaS path share one code path.
"""

import asyncio
import hashlib
import json
import time
import aiohttp
from component_base import ComponentBase
from alphaess_const import (
    ALPHAESS_BASE_URL,
    ALPHAESS_ENDPOINTS,
    ALPHAESS_RETURN_CODES,
    ALPHAESS_CODE_OK,
    ALPHAESS_CODE_TIMESTAMP,
    ALPHAESS_CODE_TOO_FAST,
    ALPHAESS_DEBUG_REDACT_KEYS,
    ALPHAESS_RETRIES,
    ALPHAESS_TIMEOUT,
)


class AlphaESSAPI(ComponentBase):
    """AlphaESS Open API cloud component."""

    # Trace every API request/response while the AlphaESS integration beds in. Nobody on
    # the project has an AlphaESS account, so a tester's log is the only evidence available
    # for the inferred behaviour; flip to False once it is confirmed.
    api_debug = True

    def initialize(
        self,
        app_id="",
        app_secret="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=True,
        battery_rate_max=None,
        api_delay=2,
        min_write_interval=300,
        **kwargs,
    ):
        """Initialise the AlphaESS component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has
        already resolved each arg from its alphaess_* config key and passes it BY ARG NAME
        (e.g. app_id <- alphaess_app_id), exactly like fox/deye/sunsynk. Consume the kwargs
        directly - do NOT re-derive with get_arg("app_id"): that bare name is not in
        apps.yaml (the key is alphaess_app_id), so it would always return the default.
        """
        self.log("Info: AlphaESSAPI initialising")
        self.app_id = app_id or ""
        self.app_secret = app_secret or ""
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_rate_max_override = float(battery_rate_max) if battery_rate_max else 0.0
        self.api_delay = max(0, float(api_delay or 0))
        self.min_write_interval = max(0, int(min_write_interval or 0))
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_energy = {}
        self.device_config = {}
        self.local_schedule = {}
        self.applied_payload = {}
        self.last_write_time = {}
        self.settle_count = {}
        # Serials Predbat has actually been asked to drive, i.e. ones whose write button has
        # been pressed at least once. The reconcile loop only re-applies for these, so a
        # startup cycle can never clobber an inverter before there is a plan to apply.
        self.control_active = set()
        # Per-serial verdicts, all cached to disk so a restart does not re-learn them.
        self._periodic_ok = {}
        self._live_ok = {}
        self._live_fail_count = {}
        self._unbind_done = set()
        self._tier_refreshed = {}
        self._cache_restored = False
        self._restore_had_error = False
        # The most recent body-level API failure message (msg/expMsg only - never a
        # credential), and whether the last discovery attempt actually reached the API. Both
        # exist so the standalone CLI can name precisely which stage failed.
        self.last_api_error = ""
        self.discovery_ok = None
        if not self.control_enable:
            self.log("Info: AlphaESS control is disabled (alphaess_control_enable is false); monitoring only")

    def _headers(self):
        """Return the signed headers every AlphaESS request carries.

        sign is sha512(appId + appSecret + timeStamp) as lower-case hex. Both `timeStamp`
        (documented) and `timestamp` (what the reference client sends) are included, since
        the API accepts either and it costs nothing to send both.
        """
        timestamp = str(int(time.time()))
        raw = "{}{}{}".format(self.app_id, self.app_secret, timestamp)
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "appId": self.app_id,
            "timeStamp": timestamp,
            "timestamp": timestamp,
            "sign": hashlib.sha512(raw.encode("ascii")).hexdigest(),
        }

    @staticmethod
    def redact(payload):
        """Return a log-safe copy of a payload with secrets and one-time codes removed."""
        if not isinstance(payload, dict):
            return payload
        return {key: ("***" if key in ALPHAESS_DEBUG_REDACT_KEYS else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response while api_debug is on."""
        if not self.api_debug:
            return
        if payload is None:
            self.log("Info: AlphaESS API {} {}".format(direction, what))
            return
        try:
            rendered = json.dumps(self.redact(payload), default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(payload)[:2000]
        self.log("Info: AlphaESS API {} {} {}".format(direction, what, rendered))

    def describe_code(self, code):
        """Return 'code (description)' for a return code, or just the code when unknown."""
        description = ALPHAESS_RETURN_CODES.get(code)
        return "{} ({})".format(code, description) if description else str(code)

    def _note_failure(self, code, body, path):
        """Record and log one API-level failure, without ever logging a credential.

        expMsg is the only field that names a bad parameter - msg just says
        "Parameter error" - so it is always included when present. 6006 is called out as a
        host clock problem because it otherwise looks exactly like a bad AppSecret.
        """
        msg = body.get("msg") or body.get("info") or ""
        exp_msg = body.get("expMsg") or ""
        detail = "{}{}".format(msg, " - {}".format(exp_msg) if exp_msg else "")
        self.last_api_error = detail or self.describe_code(code)
        if code == ALPHAESS_CODE_TIMESTAMP:
            self.log("Warn: AlphaESS rejected the request timestamp ({}) - this host's clock is more than 300 seconds from AlphaESS server time, it is a clock problem and not a credentials problem".format(self.describe_code(code)))
            return
        self.log("Warn: AlphaESS {} returned {} {}".format(path, self.describe_code(code), detail))

    async def _request(self, method, endpoint_key, params=None, body=None):
        """Perform one signed API call, returning (code, data).

        Two failure modes are kept apart deliberately. An API-level failure returns the
        envelope's own code, because the write endpoints answer data:null whether they
        succeeded or not and the code is the only way to tell - that is the whole reason
        this component has its own client rather than using the PyPI package. A transport
        failure returns -1, since it affects every endpoint and says nothing about the
        request itself.
        """
        path = ALPHAESS_ENDPOINTS[endpoint_key]
        url = "{}{}".format(ALPHAESS_BASE_URL, path)
        self.debug_api("request", "{} {}".format(method, path), body if body is not None else params)
        for attempt in range(ALPHAESS_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=ALPHAESS_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    call = session.post(url, headers=self._headers(), json=body or {}) if method == "POST" else session.get(url, headers=self._headers(), params=params or {})
                    async with call as response:
                        if response.status != 200:
                            self.log("Warn: AlphaESS {} returned HTTP {}".format(path, response.status))
                            return -1, None
                        data = await response.json()
            except Exception as error:
                if attempt + 1 >= ALPHAESS_RETRIES:
                    self.log("Warn: AlphaESS {} transport failure: {}".format(path, error))
                    return -1, None
                await asyncio.sleep(1 + attempt)
                continue

            if not isinstance(data, dict):
                self.log("Warn: AlphaESS {} returned a non-object body".format(path))
                return -1, None
            self.debug_api("response", path, data)
            code = data.get("code")
            # The periodic endpoints report status in `info` where the rest use `msg`.
            if code == ALPHAESS_CODE_OK or data.get("msg") == "Success" or data.get("info") == "Success":
                return ALPHAESS_CODE_OK, data.get("data")
            self._note_failure(code, data, path)
            return code, None
        return -1, None

    async def _get(self, endpoint_key, params=None):
        """Perform a signed GET, returning (code, data)."""
        return await self._request("GET", endpoint_key, params=params)

    async def _post(self, endpoint_key, body=None):
        """Perform a signed POST, returning (code, data)."""
        return await self._request("POST", endpoint_key, body=body)
```

- [ ] **Step 4: Register the test module**

In `apps/predbat/unit_test.py`:

```python
from tests.test_alphaess_api import run_alphaess_api_tests
```

```python
        ("alphaess_api", run_alphaess_api_tests, "AlphaESS API tests", False),
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t2.log 2>&1; grep -E "PASSED|FAILED|ERROR|EXCEPTION" /tmp/t2.log
```

Expected: both `alphaess_const` and `alphaess_api` PASSED.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py apps/predbat/unit_test.py
git commit -m "feat(alphaess): add signed AlphaESS client with code-preserving envelope handling"
```

---

## Task 3: Component registration, INVERTER_DEF and schema

**Files:**
- Modify: `apps/predbat/components.py` (import + `COMPONENT_LIST` entry)
- Modify: `apps/predbat/config.py` (`INVERTER_DEF["AlphaESSCloud"]`, `APPS_SCHEMA` keys)
- Test: `apps/predbat/tests/test_alphaess_config.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `AlphaESSAPI` from Task 2.
- Produces: the `alphaess` component key with `event_filter: "predbat_alphaess_"`, and the `AlphaESSCloud` inverter type.

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_alphaess_config.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS config and INVERTER_DEF registration
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS component registration, INVERTER_DEF entry and APPS_SCHEMA keys."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST


def test_alphaess_component_registered():
    """The component is registered in phase 1 with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("alphaess")
    if not entry:
        print("ERROR: alphaess not in COMPONENT_LIST")
        assert False, "test_alphaess_component_registered"
    if entry.get("event_filter") != "predbat_alphaess_":
        print(f"ERROR: event_filter {entry.get('event_filter')}")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')}")
        failed = True
    if not entry.get("can_restart"):
        print("ERROR: alphaess should be restartable")
        failed = True
    # Without required_or the component would start for every Predbat instance, since all
    # individual args are optional.
    if entry.get("required_or") != ["app_id"]:
        print(f"ERROR: required_or {entry.get('required_or')}")
        failed = True
    for arg, config_key, default in [
        ("app_id", "alphaess_app_id", None),
        ("app_secret", "alphaess_app_secret", None),
        ("inverter_sn", "alphaess_inverter_sn", None),
        ("automatic", "alphaess_automatic", False),
        ("automatic_ignore_pv", "alphaess_automatic_ignore_pv", False),
        ("control_enable", "alphaess_control_enable", True),
        ("battery_rate_max", "alphaess_battery_rate_max", None),
        ("api_delay", "alphaess_api_delay", 2),
        ("min_write_interval", "alphaess_min_write_interval", 300),
    ]:
        info = entry["args"].get(arg)
        if not info:
            print(f"ERROR: arg {arg} missing")
            failed = True
            continue
        if info.get("config") != config_key:
            print(f"ERROR: arg {arg} config {info.get('config')} != {config_key}")
            failed = True
        if default is not None and info.get("default") != default:
            print(f"ERROR: arg {arg} default {info.get('default')} != {default}")
            failed = True
    assert not failed, "test_alphaess_component_registered"


def test_alphaess_control_enable_defaults_true():
    """An inverter component that does not drive the inverter is not what a user expects."""
    failed = False
    info = COMPONENT_LIST["alphaess"]["args"]["control_enable"]
    if info.get("default") is not True:
        print(f"ERROR: control_enable default {info.get('default')} should be True")
        failed = True
    assert not failed, "test_alphaess_control_enable_defaults_true"


def test_alphaess_inverter_def_complete():
    """AlphaESSCloud declares every key the other cloud inverter types declare."""
    failed = False
    entry = INVERTER_DEF.get("AlphaESSCloud")
    if not entry:
        print("ERROR: AlphaESSCloud not in INVERTER_DEF")
        assert False, "test_alphaess_inverter_def_complete"
    reference = set(INVERTER_DEF["SunsynkCloud"].keys())
    missing = reference - set(entry.keys())
    if missing:
        print(f"ERROR: AlphaESSCloud missing keys {sorted(missing)}")
        failed = True
    expected = {
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "power",
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        # No pause endpoint exists, so Predbat expresses freeze through the rate entities.
        "has_timed_pause": False,
        # Anything else makes inverter.py replace the published select entities with its
        # own dummies and the window never reaches the component.
        "charge_time_format": "HH:MM:SS",
        "soc_units": "%",
        "time_button_press": True,
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        # Wrap-around behaviour is undocumented for timeChaf1/timeChae1, so Predbat splits.
        "can_span_midnight": False,
        "target_soc_used_for_discharge": True,
    }
    for key, value in expected.items():
        if entry.get(key) != value:
            print(f"ERROR: AlphaESSCloud[{key}] = {entry.get(key)} != {value}")
            failed = True
    assert not failed, "test_alphaess_inverter_def_complete"


def test_alphaess_apps_schema_keys():
    """Every alphaess_* key a user may set is declared with the right type."""
    failed = False
    expected = {
        "alphaess_app_id": "string",
        "alphaess_app_secret": "string",
        "alphaess_inverter_sn": "string|string_list",
        "alphaess_automatic": "boolean",
        "alphaess_automatic_ignore_pv": "boolean",
        "alphaess_control_enable": "boolean",
        "alphaess_battery_rate_max": "float",
        "alphaess_api_delay": "float",
        "alphaess_min_write_interval": "integer",
    }
    for key, kind in expected.items():
        entry = APPS_SCHEMA.get(key)
        if not entry:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
            continue
        if entry.get("type") != kind:
            print(f"ERROR: APPS_SCHEMA[{key}] type {entry.get('type')} != {kind}")
            failed = True
    assert not failed, "test_alphaess_apps_schema_keys"


def run_alphaess_config_tests(my_predbat):
    """Run all AlphaESS config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("component_registered", test_alphaess_component_registered),
        ("control_enable_default", test_alphaess_control_enable_defaults_true),
        ("inverter_def", test_alphaess_inverter_def_complete),
        ("apps_schema", test_alphaess_apps_schema_keys),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_config > /tmp/t3.log 2>&1; tail -20 /tmp/t3.log
```

Expected: FAIL — not registered yet.

- [ ] **Step 3: Register the component**

In `apps/predbat/components.py`, add the import next to the other cloud components (near line 36):

```python
from alphaess import AlphaESSAPI
```

Add to `COMPONENT_LIST`, next to the `sunsynk` entry:

```python
    "alphaess": {
        "class": AlphaESSAPI,
        "name": "AlphaESS Cloud API",
        "event_filter": "predbat_alphaess_",
        "args": {
            "app_id": {"required": False, "config": "alphaess_app_id"},
            "app_secret": {"required": False, "config": "alphaess_app_secret"},
            "inverter_sn": {"required": False, "config": "alphaess_inverter_sn"},
            "automatic": {"required": False, "default": False, "config": "alphaess_automatic"},
            "automatic_ignore_pv": {"required": False, "default": False, "config": "alphaess_automatic_ignore_pv"},
            # On by default, matching sunsynk_control_enable: an inverter component that does
            # not drive the inverter is not what a user configuring it expects. Set false for
            # monitoring only. switch.predbat_set_read_only still gates every write.
            "control_enable": {"required": False, "default": True, "config": "alphaess_control_enable"},
            # The API reports no battery power limit and no pack current/voltage to derive
            # one from, so it is estimated from poinv. This is the escape hatch for a user
            # who knows their pack's real limit.
            "battery_rate_max": {"required": False, "config": "alphaess_battery_rate_max"},
            "api_delay": {"required": False, "default": 2, "config": "alphaess_api_delay"},
            "min_write_interval": {"required": False, "default": 300, "config": "alphaess_min_write_interval"},
        },
        # Gate activation on having an AppID. Without this the component would start for
        # every instance, since all individual args are optional.
        "required_or": ["app_id"],
        "phase": 1,
        "can_restart": True,
    },
```

- [ ] **Step 4: Add the inverter definition and schema**

In `apps/predbat/config.py`, add to `INVERTER_DEF` immediately after the `SunsynkCloud` entry:

```python
    "AlphaESSCloud": {
        "name": "AlphaESSCloud",
        "has_rest_api": False,
        "has_mqtt_api": False,
        # The periodic path carries a real chargePower setpoint. On the legacy path a
        # non-zero rate just means "unrestricted"; a rate of ZERO is meaningful on both
        # paths and is how Predbat signals freeze (see the component's payload builder).
        "output_charge_control": "power",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        # There is no pause endpoint, so Predbat expresses freeze via the rate entities.
        "has_timed_pause": False,
        # Anything other than HH:MM:SS makes inverter.py replace the published select
        # entities with its own dummies and the window never reaches the component. The
        # API wants HH:mm; the conversion happens at the payload boundary.
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
        # Wrap-around behaviour is undocumented for timeChaf1/timeChae1, so Predbat splits
        # the window and period 2 carries the remainder.
        "can_span_midnight": False,
        "charge_discharge_with_rate": False,
        "target_soc_used_for_discharge": True,
    },
```

Add to `APPS_SCHEMA`, next to the `sunsynk_*` keys (near line 2542):

```python
    "alphaess_app_id": {"type": "string", "empty": False},
    "alphaess_app_secret": {"type": "string", "empty": False},
    "alphaess_inverter_sn": {"type": "string|string_list", "empty": False},
    "alphaess_automatic": {"type": "boolean"},
    "alphaess_automatic_ignore_pv": {"type": "boolean"},
    "alphaess_control_enable": {"type": "boolean"},
    "alphaess_battery_rate_max": {"type": "float"},
    "alphaess_api_delay": {"type": "float"},
    "alphaess_min_write_interval": {"type": "integer"},
```

- [ ] **Step 5: Register the test module**

In `apps/predbat/unit_test.py`:

```python
from tests.test_alphaess_config import run_alphaess_config_tests
```

```python
        ("alphaess_config", run_alphaess_config_tests, "AlphaESS config/INVERTER_DEF tests", False),
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t3.log 2>&1; grep -E "PASSED|FAILED|ERROR|EXCEPTION" /tmp/t3.log
```

Expected: `alphaess_const`, `alphaess_api`, `alphaess_config` all PASSED.

Then confirm nothing else regressed from touching `config.py`/`components.py`:

```bash
./run_all --quick > /tmp/t3-quick.log 2>&1; grep -E "FAILED|ERROR" /tmp/t3-quick.log | head -20
```

Expected: no FAILED lines.

- [ ] **Step 7: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/components.py apps/predbat/config.py apps/predbat/tests/test_alphaess_config.py apps/predbat/unit_test.py
git commit -m "feat(alphaess): register the component and add the AlphaESSCloud inverter type"
```

---

## Task 4: Discovery, ratings and the no-battery filter

**Files:**
- Modify: `apps/predbat/alphaess.py` (append methods to `AlphaESSAPI`)
- Modify: `apps/predbat/tests/test_alphaess_api.py` (append tests + runner entries)

**Interfaces:**
- Consumes: `_get` from Task 2.
- Produces:
  - `async get_device_list(self) -> list[str]` — sets `self.device_list`, `self.device_detail`, `self.discovery_ok`
  - `def battery_capacity(self, sn) -> float` (kWh)
  - `def inverter_limit(self, sn) -> float` (W)
  - `def battery_rate_max(self, sn) -> float` (W)
  - `def has_battery(detail: dict) -> bool` (staticmethod)
  - `async refresh_static(self) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_api.py`:

```python
ESS_LIST_SAMPLE = [
    {"sysSn": "AL70110230306xx", "popv": 9.0, "minv": "SMILE5-INV", "poinv": 5.0, "cobat": 13.34, "mbat": "SMILE-BAT-13.3P", "surplusCobat": 13.34, "usCapacity": 100.0, "emsStatus": "Normal"},
    {"sysSn": "AL70110230302xx", "popv": 5.0, "minv": "SMILE5-INV", "poinv": 5.0, "cobat": 10.1, "mbat": "SMILE-BAT-10.1P", "surplusCobat": 9.09, "usCapacity": 90.0, "emsStatus": "Normal"},
]


def test_alphaess_discovery_maps_ratings():
    """cobat becomes soc_max kWh and poinv becomes inverter_limit W."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["AL70110230306xx", "AL70110230302xx"]:
        print(f"ERROR: serials {serials}")
        failed = True
    if abs(client.battery_capacity("AL70110230306xx") - 13.34) > 0.001:
        print(f"ERROR: capacity {client.battery_capacity('AL70110230306xx')}")
        failed = True
    if abs(client.inverter_limit("AL70110230306xx") - 5000.0) > 0.1:
        print(f"ERROR: inverter_limit {client.inverter_limit('AL70110230306xx')}")
        failed = True
    if client.discovery_ok is not True:
        print(f"ERROR: discovery_ok {client.discovery_ok}")
        failed = True
    assert not failed, "test_alphaess_discovery_maps_ratings"


def test_alphaess_battery_rate_max_defaults_to_the_inverter_limit():
    """Leaving battery_rate_max unmapped is NOT neutral - inverter.py:410 falls back to
    a hard-coded 2600W, roughly half a SMILE5's rate, silently, on every plan. poinv is
    the best available estimate and an over-estimate self-reports via
    battery_rate_max_scaling, so it is used rather than nothing.
    """
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        run_async_local(client.get_device_list())
    if abs(client.battery_rate_max("AL70110230306xx") - 5000.0) > 0.1:
        print(f"ERROR: derived battery_rate_max {client.battery_rate_max('AL70110230306xx')}")
        failed = True
    # The explicit override wins outright.
    override = MockAlphaESS()
    override.battery_rate_max_override = 3600.0
    override.device_detail = client.device_detail
    if abs(override.battery_rate_max("AL70110230306xx") - 3600.0) > 0.1:
        print(f"ERROR: override battery_rate_max {override.battery_rate_max('AL70110230306xx')}")
        failed = True
    assert not failed, "test_alphaess_battery_rate_max_defaults_to_the_inverter_limit"


def test_alphaess_systems_without_a_battery_are_skipped():
    """AlphaESS also sells plug-in solar, which has nothing for Predbat to drive.

    A capability filter rather than a model filter, so it catches every non-battery
    product AlphaESS ships now or later with no table to maintain.
    """
    failed = False
    client = MockAlphaESS()
    payload = list(ESS_LIST_SAMPLE) + [
        {"sysSn": "VT100000000001", "minv": "VT1000", "poinv": 0.8, "popv": 0.8, "cobat": 0},
        {"sysSn": "VT100000000002", "minv": "VT1000", "poinv": 0.8, "popv": 0.8},
    ]
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, payload))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if "VT100000000001" in serials or "VT100000000002" in serials:
        print(f"ERROR: battery-less systems registered: {serials}")
        failed = True
    if len(serials) != 2:
        print(f"ERROR: expected 2 serials, got {serials}")
        failed = True
    # Logged once by serial and model, so the user can see it was recognised and passed
    # over rather than silently missing.
    if not any("VT100000000001" in message and "batter" in message.lower() for message in client.log_messages):
        print(f"ERROR: no skip log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_systems_without_a_battery_are_skipped"


def test_alphaess_serial_filter_restricts_discovery():
    """alphaess_inverter_sn narrows discovery, case-insensitively."""
    failed = False
    client = MockAlphaESS(inverter_sn="al70110230302XX")
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    session = create_aiohttp_mock_session(response)
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["AL70110230302xx"]:
        print(f"ERROR: filtered serials {serials}")
        failed = True
    assert not failed, "test_alphaess_serial_filter_restricts_discovery"


def test_alphaess_refresh_static_never_clears_a_working_device_list():
    """Absence of a result is not a result.

    This tier re-runs every 8 hours in a long-lived process; one transient failure must
    not take a working component down until the next success, and it must not write an
    empty list to the cache and then stamp it fresh.
    """
    failed = False
    client = MockAlphaESS()
    ok = create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok)):
        run_async_local(client.refresh_static())
    before = list(client.device_list)
    tier_before = client._tier_refreshed.get("static")
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        result = run_async_local(client.refresh_static())
    if client.device_list != before:
        print(f"ERROR: device_list cleared by a transient failure: {client.device_list} != {before}")
        failed = True
    # The return value and the tier clock are the parts that actually catch the bug:
    # device_list being unchanged is trivially true on the failure path, so a test that
    # checked only that would pass even with the guard unreachable.
    if result is not False:
        print(f"ERROR: refresh_static returned {result!r} after a failed discovery, must be False")
        failed = True
    if client._tier_refreshed.get("static") != tier_before:
        print("ERROR: the static tier clock advanced despite a failed discovery")
        failed = True
    assert not failed, "test_alphaess_refresh_static_never_clears_a_working_device_list"


def test_alphaess_discovery_distinguishes_empty_account_from_failure():
    """An empty account and a failed call look identical without this, and the CLI needs
    to name which one actually happened."""
    failed = False
    empty = MockAlphaESS()
    ok = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok)):
        run_async_local(empty.get_device_list())
    if empty.discovery_ok is not True:
        print(f"ERROR: empty account discovery_ok {empty.discovery_ok} should be True")
        failed = True
    broken = MockAlphaESS()
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6007, None, msg="Sign verification error"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        run_async_local(broken.get_device_list())
    if broken.discovery_ok is not False:
        print(f"ERROR: failed discovery_ok {broken.discovery_ok} should be False")
        failed = True
    assert not failed, "test_alphaess_discovery_distinguishes_empty_account_from_failure"
```

Add to the `run_alphaess_api_tests` list:

```python
        ("discovery_ratings", test_alphaess_discovery_maps_ratings),
        ("battery_rate_max_from_poinv", test_alphaess_battery_rate_max_defaults_to_the_inverter_limit),
        ("no_battery_skipped", test_alphaess_systems_without_a_battery_are_skipped),
        ("serial_filter", test_alphaess_serial_filter_restricts_discovery),
        ("refresh_static_keeps_list", test_alphaess_refresh_static_never_clears_a_working_device_list),
        ("empty_vs_failed_discovery", test_alphaess_discovery_distinguishes_empty_account_from_failure),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t4.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t4.log | head
```

Expected: EXCEPTION — `get_device_list` does not exist.

- [ ] **Step 3: Implement discovery and ratings**

Append to `AlphaESSAPI` in `apps/predbat/alphaess.py`:

```python
    @staticmethod
    def _as_float(value, default=0.0):
        """Coerce an API value to float, returning default for None/'unknown'/junk."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def has_battery(detail):
        """Return True when a discovered system actually has a battery.

        AlphaESS also sells plug-in solar (the VT1000 family), which has nothing for
        Predbat to drive. This is a capability check rather than a model blacklist so it
        catches every non-battery product AlphaESS ships now or later without anyone
        maintaining a table.
        """
        try:
            return float(detail.get("cobat") or 0) > 0
        except (TypeError, ValueError):
            return False

    async def get_device_list(self):
        """Discover every battery system bound to the AppID, returning its serials.

        Sets discovery_ok so an empty account can be told apart from a failed call - the
        two are indistinguishable from the returned list alone, and the CLI has to name
        which one happened.
        """
        code, data = await self._get("ess_list")
        if code != ALPHAESS_CODE_OK:
            self.discovery_ok = False
            return list(self.device_list)
        self.discovery_ok = True
        wanted = [str(sn).lower() for sn in self.inverter_sn_filter]
        serials = []
        detail = {}
        for entry in data or []:
            sn = entry.get("sysSn")
            if not sn:
                continue
            if wanted and str(sn).lower() not in wanted:
                continue
            if not self.has_battery(entry):
                # Logged by serial and model so it reads as recognised-and-passed-over
                # rather than silently missing. Said even for an explicitly requested
                # serial: there is no plan to apply to a system with no battery, and a
                # filter matching nothing otherwise looks identical to an empty account.
                self.log("Info: AlphaESS skipping {} (model {}) - it reports no battery capacity, so there is nothing for Predbat to control".format(sn, entry.get("minv", "unknown")))
                continue
            serials.append(sn)
            detail[sn] = entry
        self.device_list = serials
        self.device_detail = detail
        return serials

    def battery_capacity(self, sn):
        """Return the battery capacity in kWh from getEssList's cobat."""
        return self._as_float(self.device_detail.get(sn, {}).get("cobat"), 0.0)

    def inverter_limit(self, sn):
        """Return the inverter's nominal AC power in watts, from getEssList's poinv (kW)."""
        return self._as_float(self.device_detail.get(sn, {}).get("poinv"), 0.0) * 1000.0

    def battery_rate_max(self, sn):
        """Return the battery charge/discharge power limit in watts.

        The API reports no battery power limit and no pack current or voltage to derive one
        from, so this falls back to the inverter's nominal AC power. That is deliberate
        rather than lazy: leaving battery_rate_max unmapped is NOT neutral, because
        inverter.py:410 then uses a hard-coded 2600W - roughly half a SMILE5's real rate -
        silently, on every plan. inverter.py:423 makes battery_rate_max the governing term
        in min(inverter_limit_charge, battery_rate_max_raw), so mapping inverter_limit
        alone does not rescue it.

        On a matched AlphaESS package poinv is close to the battery rate. Where it is not,
        the estimate is high, and that is the safer error: inverter.py measures the achieved
        rate and logs a battery_rate_max_scaling suggestion, so an over-estimate reports
        itself while the 2600W default never does.
        """
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        return self.inverter_limit(sn)

    async def refresh_static(self):
        """Re-discover systems and refresh their static detail. True when discovery worked.

        Deliberately does NOT assign an empty discovery result over a working device_list.
        This tier re-runs every 8 hours in a long-lived process, so one transient failure
        must not take a working component down until the next success - and assigning the
        empty result would additionally write {'device_list': []} to the cache and stamp it
        fresh, so a restart would restore nothing and skip re-discovery for a full TTL.
        Absence of a result is not a result.
        """
        previous = list(self.device_list)
        serials = await self.get_device_list()
        # Branch on discovery_ok, NOT on the list being empty. get_device_list returns the
        # EXISTING list on failure, and `previous` was captured before the call, so
        # `if not serials` can never fire once a device_list exists - the guard would be
        # unreachable and the tier would be stamped fresh on every failed poll, making an
        # ongoing outage look like a steady stream of successful refreshes.
        if self.discovery_ok is False:
            self.device_list = previous
            if previous:
                self.log("Warn: AlphaESS discovery call failed; keeping the {} previously known inverter(s)".format(len(previous)))
            else:
                self.log("Warn: AlphaESS inverter discovery failed; the account may still have systems")
            return False
        if not serials:
            # Discovery SUCCEEDED and returned nothing. A filter matching nothing looks
            # identical to an empty account from the list alone, so say which it was.
            if self.inverter_sn_filter:
                self.log("Warn: AlphaESS discovery succeeded but the configured serial filter {} matched no system on this account".format(self.inverter_sn_filter))
            else:
                self.log("Warn: AlphaESS this account has no battery systems bound to it")
            return False
        self.mark_refreshed("static")
        return True
```

Add the `ALPHAESS_CODE_OK` import if not already present (it is, from Task 2).

- [ ] **Step 4: Add the tier-clock helpers**

These are needed by `refresh_static` and every later tier. Append to `AlphaESSAPI`:

```python
    def tier_expired(self, tier, ttl_minutes):
        """Return True when a refresh tier is due, or has never run."""
        age = self._tier_refreshed.get(tier)
        if age is None:
            return True
        return (time.time() - age) >= (ttl_minutes * 60)

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Start a tier's clock. Called ONLY on a successful refresh.

        Marking unconditionally would defeat the first-cycle checks in run(): a retry after
        a deferred startup would find the tier "fresh", skip the poll entirely and then run
        automatic_config() with no data after all - the very thing those checks exist to
        prevent.
        """
        self._tier_refreshed[tier] = time.time() - (age_minutes * 60)
```

- [ ] **Step 5: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t4.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t4.log
```

Expected: PASSED.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py
git commit -m "feat(alphaess): discovery, ratings and the no-battery capability filter"
```

---

## Task 5: Telemetry with history fallback

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_api.py`

**Interfaces:**
- Consumes: `_get`, `_as_float`, tier helpers from Tasks 2 and 4.
- Produces:
  - `async fetch_device_data(self, sn) -> bool` — fills `self.device_values[sn]`
  - `async fetch_device_history(self, sn) -> bool`
  - `def _history_query_date(self) -> str`
  - `async refresh_power(self) -> bool`
  - `self._live_ok: dict[str, bool]`, `self._live_fail_count: dict[str, int]`
  - `device_values[sn]` keys: `soc`, `battery_power`, `grid_power`, `pv_power`, `load_power`, `ev_power` (Predbat sign conventions applied)

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_api.py`:

```python
LAST_POWER_SAMPLE = {
    "ppv": 0.0,
    "ppvDetail": {"ppv1": 0.0, "ppv2": 0.0, "ppv3": 0.0, "ppv4": 0.0, "pmeterDc": 0.0},
    "soc": 56.0,
    "pev": 0,
    "pevDetail": {"ev1Power": None, "ev2Power": None, "ev3Power": None, "ev4Power": None},
    "prealL1": 1159.0,
    "pgrid": 11.0,
    "pgridDetail": {"pmeterL1": 11.0, "pmeterL2": 0.0, "pmeterL3": 0.0},
    "pload": 1275.0,
    "pbat": 1264.0,
}


def test_alphaess_telemetry_applies_predbat_sign_conventions():
    """pgrid is positive on IMPORT and Predbat wants negative on import, so it is negated.

    pbat needs NO negation: the API doc's own live sample balances as
    pgrid 11 + pbat 1264 = pload 1275 with ppv 0, so a positive pbat is discharge, which
    is already Predbat's convention.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, LAST_POWER_SAMPLE))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        ok = run_async_local(client.fetch_device_data("AL70"))
    values = client.device_values.get("AL70", {})
    if not ok:
        print("ERROR: fetch_device_data returned False")
        failed = True
    for leaf, expect in (("soc", 56.0), ("battery_power", 1264.0), ("grid_power", -11.0), ("pv_power", 0.0), ("load_power", 1275.0)):
        if abs(values.get(leaf, 0.0) - expect) > 0.001:
            print(f"ERROR: {leaf} {values.get(leaf)} != {expect}")
            failed = True
    assert not failed, "test_alphaess_telemetry_applies_predbat_sign_conventions"


def test_alphaess_falls_back_to_history_when_live_data_is_unavailable():
    """Not every system serves getLastPowerData (Storion-S5 is the known case).

    Decided on BEHAVIOUR, not on a model list: a list only covers models someone already
    wrote down, while this covers Storion-S5, any unlisted model with the same gap, and a
    system that simply stops answering.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    history = [
        {"sysSn": "AL70", "uploadTime": "2026-08-22 20:04:04", "ppv": 10.0, "load": 900.0, "cbat": 40.0, "feedIn": 0.0, "gridCharge": 100.0},
        {"sysSn": "AL70", "uploadTime": "2026-08-22 20:09:04", "ppv": 0.0, "load": 1218.0, "cbat": 56.8, "feedIn": 0.0, "gridCharge": 2.0},
    ]
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, history)),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_device_data("AL70"))
    values = client.device_values.get("AL70", {})
    if not ok:
        print("ERROR: history fallback returned False")
        failed = True
    # The MOST RECENT sample, not the first.
    if abs(values.get("soc", 0.0) - 56.8) > 0.001:
        print(f"ERROR: soc from history {values.get('soc')} != 56.8")
        failed = True
    # feedIn and gridCharge are separate positive-only fields, so grid is reconstructed
    # as gridCharge - feedIn and THEN negated for Predbat.
    if abs(values.get("grid_power", 0.0) - (-2.0)) > 0.001:
        print(f"ERROR: grid_power from history {values.get('grid_power')} != -2.0")
        failed = True
    if abs(values.get("load_power", 0.0) - 1218.0) > 0.001:
        print(f"ERROR: load_power from history {values.get('load_power')}")
        failed = True
    assert not failed, "test_alphaess_falls_back_to_history_when_live_data_is_unavailable"


def test_alphaess_history_reads_cbat_not_the_portal_spelling():
    """The portal documents the SOC field as cobat; the live API returns cbat.

    Reading the portal name silently yields None, which looks exactly like "this system
    has no SOC either" and would send a working serial down the skip path.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    for field in ("cbat", "cobat"):
        sample = [{"uploadTime": "2026-08-22 20:09:04", "ppv": 0.0, "load": 100.0, field: 42.0, "feedIn": 0.0, "gridCharge": 0.0}]
        responses = [
            create_aiohttp_mock_response(status=200, json_data=_envelope(-1, None)),
            create_aiohttp_mock_response(status=200, json_data=_envelope(200, sample)),
        ]
        client.device_values = {}
        client._live_fail_count = {}
        client._live_ok = {}
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
            run_async_local(client.fetch_device_data("AL70"))
        if abs(client.device_values.get("AL70", {}).get("soc", 0.0) - 42.0) > 0.001:
            print(f"ERROR: soc from {field} = {client.device_values.get('AL70', {}).get('soc')}")
            failed = True
    assert not failed, "test_alphaess_history_reads_cbat_not_the_portal_spelling"


def test_alphaess_live_demotion_latches_and_is_reversible():
    """~288 records is too big for a 60-second loop, so demotion latches after N failures
    and is re-probed on the config tier so a transient failure self-heals."""
    failed = False
    from alphaess_const import ALPHAESS_LIVE_FAIL_LIMIT

    client = MockAlphaESS()
    client.device_list = ["AL70"]
    history = [{"uploadTime": "2026-08-22 20:09:04", "ppv": 0.0, "load": 10.0, "cbat": 50.0, "feedIn": 0.0, "gridCharge": 0.0}]
    for _ in range(ALPHAESS_LIVE_FAIL_LIMIT):
        responses = [
            create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None)),
            create_aiohttp_mock_response(status=200, json_data=_envelope(200, history)),
        ]
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
            run_async_local(client.fetch_device_data("AL70"))
    if client._live_ok.get("AL70") is not False:
        print(f"ERROR: not demoted after {ALPHAESS_LIVE_FAIL_LIMIT} failures: {client._live_ok}")
        failed = True
    # Once demoted, the live endpoint is not called again on the power tier: a single
    # history response is enough to satisfy the whole call.
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, history)))):
        ok = run_async_local(client.fetch_device_data("AL70"))
    if not ok:
        print("ERROR: demoted fetch failed")
        failed = True
    # The config tier re-probes, and success restores 60-second live data.
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, LAST_POWER_SAMPLE)))):
        run_async_local(client.reprobe_live("AL70"))
    if client._live_ok.get("AL70") is not True:
        print(f"ERROR: re-probe did not restore live data: {client._live_ok}")
        failed = True
    assert not failed, "test_alphaess_live_demotion_latches_and_is_reversible"


def test_alphaess_serial_with_no_soc_on_either_path_is_reported():
    """No SOC on either path means the serial cannot be driven - say so, do not invent one."""
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, [])),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_device_data("AL70"))
    if ok:
        print("ERROR: fetch_device_data claimed success with no SOC")
        failed = True
    if "soc" in client.device_values.get("AL70", {}):
        print(f"ERROR: a SOC was invented: {client.device_values}")
        failed = True
    if not any("AL70" in message and "soc" in message.lower() for message in client.log_messages):
        print(f"ERROR: no explanatory log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_serial_with_no_soc_on_either_path_is_reported"
```

Add to the `run_alphaess_api_tests` list:

```python
        ("telemetry_signs", test_alphaess_telemetry_applies_predbat_sign_conventions),
        ("history_fallback", test_alphaess_falls_back_to_history_when_live_data_is_unavailable),
        ("history_cbat_spelling", test_alphaess_history_reads_cbat_not_the_portal_spelling),
        ("live_demotion_reversible", test_alphaess_live_demotion_latches_and_is_reversible),
        ("no_soc_reported", test_alphaess_serial_with_no_soc_on_either_path_is_reported),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t5.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t5.log | head
```

Expected: EXCEPTION — `fetch_device_data` does not exist.

- [ ] **Step 3: Implement telemetry and the history fallback**

Add the imports to `alphaess.py`:

```python
from datetime import datetime
from alphaess_const import (
    ALPHAESS_TELEMETRY,
    ALPHAESS_TELEMETRY_NEGATE,
    ALPHAESS_HISTORY,
    ALPHAESS_HISTORY_FEED_IN,
    ALPHAESS_HISTORY_GRID_CHARGE,
    ALPHAESS_LIVE_FAIL_LIMIT,
    ALPHAESS_TTL_POWER,
    ALPHAESS_TTL_POWER_DEMOTED,
)
```

Append to `AlphaESSAPI`:

```python
    def _history_query_date(self):
        """Return today's date as yyyy-MM-dd in the user's timezone, for the day endpoints."""
        return datetime.now(self.local_tz).strftime("%Y-%m-%d")

    def power_tier_ttl(self):
        """Return the power tier interval, longer once any serial is on the history path.

        getOneDayPowerBySn returns ~288 records for a full day, so it must not sit on a
        60-second loop. Five minutes is the resolution the history actually has anyway.
        """
        if any(ok is False for ok in self._live_ok.values()):
            return ALPHAESS_TTL_POWER_DEMOTED
        return ALPHAESS_TTL_POWER

    def _apply_live_payload(self, sn, payload):
        """Map a getLastPowerData object into device_values, or return False without a SOC."""
        if not isinstance(payload, dict) or payload.get("soc") is None:
            return False
        values = {}
        for leaf, field in ALPHAESS_TELEMETRY.items():
            value = self._as_float(payload.get(field), 0.0)
            if leaf in ALPHAESS_TELEMETRY_NEGATE:
                # pgrid is positive on IMPORT; Predbat's convention is negative on import.
                # pbat needs no negation - the API's own live sample balances as
                # pgrid + pbat = pload, so a positive pbat is already discharge.
                value = -value
            values[leaf] = value
        self.device_values[sn] = values
        return True

    def _apply_history_payload(self, sn, samples):
        """Map the most recent getOneDayPowerBySn sample into device_values.

        Returns False when the history carries no SOC, which is the only thing that makes
        a serial undriveable - everything else can be defaulted.
        """
        if not isinstance(samples, list) or not samples:
            return False
        sample = samples[-1]
        soc = None
        for field in ALPHAESS_HISTORY["soc"]:
            # cbat is what the live API returns; the portal documents cobat and reading
            # that name silently yields None. Try both, cbat first.
            if sample.get(field) is not None:
                soc = self._as_float(sample.get(field), None)
                break
        if soc is None:
            return False
        feed_in = self._as_float(sample.get(ALPHAESS_HISTORY_FEED_IN), 0.0)
        grid_charge = self._as_float(sample.get(ALPHAESS_HISTORY_GRID_CHARGE), 0.0)
        self.device_values[sn] = {
            "soc": soc,
            "pv_power": self._as_float(sample.get("ppv"), 0.0),
            "load_power": self._as_float(sample.get("load"), 0.0),
            # The history has no signed grid field, so it is reconstructed from the two
            # positive-only fields and then negated for Predbat's convention.
            "grid_power": -(grid_charge - feed_in),
        }
        return True

    async def fetch_device_history(self, sn):
        """Populate device_values for one serial from today's power history."""
        code, data = await self._get("one_day_power", params={"sysSn": sn, "queryDate": self._history_query_date()})
        if code != ALPHAESS_CODE_OK:
            return False
        return self._apply_history_payload(sn, data)

    async def reprobe_live(self, sn):
        """Re-test getLastPowerData for a demoted serial, restoring it on success.

        Runs on the config tier, so a system that was merely offline or briefly failing
        climbs back to 60-second live data by itself, and a genuinely incapable one costs
        two extra calls an hour rather than one per minute.
        """
        code, data = await self._get("last_power", params={"sysSn": sn})
        if code == ALPHAESS_CODE_OK and self._apply_live_payload(sn, data):
            if self._live_ok.get(sn) is False:
                self.log("Info: AlphaESS {} is serving live power data again, returning it to the live telemetry path".format(sn))
            self._live_ok[sn] = True
            self._live_fail_count[sn] = 0
            return True
        return False

    async def fetch_device_data(self, sn):
        """Populate device_values for one serial, preferring live data over history.

        The rule is behavioural, not model-based: if live data is not present, use the
        history. That covers the models known not to serve getLastPowerData, any unlisted
        model with the same gap, and a system that has simply stopped answering - none of
        which a model list would catch.
        """
        if self._live_ok.get(sn) is not False:
            code, data = await self._get("last_power", params={"sysSn": sn})
            if code == ALPHAESS_CODE_OK and self._apply_live_payload(sn, data):
                self._live_ok[sn] = True
                self._live_fail_count[sn] = 0
                return True
            self._live_fail_count[sn] = self._live_fail_count.get(sn, 0) + 1
            if self._live_fail_count[sn] >= ALPHAESS_LIVE_FAIL_LIMIT:
                self._live_ok[sn] = False
                self.log("Info: AlphaESS {} has not served live power data {} times running; using the 5-minute history instead. It is re-probed every config refresh, so this reverses by itself if the system recovers.".format(sn, self._live_fail_count[sn]))

        if await self.fetch_device_history(sn):
            return True

        # No SOC on either path means Predbat cannot plan for this serial. Say which call
        # failed rather than registering it with a fabricated SOC.
        self.log("Warn: AlphaESS {} returned no usable soc from getLastPowerData and no cbat in its power history, so Predbat cannot drive it this cycle".format(sn))
        return False

    async def refresh_power(self):
        """Poll telemetry for every inverter, reporting whether anything came back.

        The tier clock is started only when a poll actually succeeded - see mark_refreshed.
        """
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_device_data(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS telemetry poll failed for {}: {}".format(sn, error))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("power")
        return got_any
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t5.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t5.log
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py
git commit -m "feat(alphaess): live telemetry with behavioural fallback to 5-minute history"
```

---

## Task 6: Daily energy counters

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_api.py`

**Interfaces:**
- Consumes: `_get`, `_as_float`, `_history_query_date` from Tasks 2, 4, 5.
- Produces: `async fetch_device_energy(self, sn) -> bool`, `async refresh_energy(self) -> bool`, `self.device_energy[sn]` keyed by the `ALPHAESS_ENERGY` leaves plus `load_today`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_api.py`:

```python
ONE_DATE_ENERGY_SAMPLE = {"sysSn": "AL70", "theDate": "2026-08-22", "eCharge": 12.2, "epv": 10.6, "eOutput": 0.42, "eInput": 14.41, "eGridCharge": 9.1, "eDischarge": 7.1, "eChargingPile": 0.0}


def test_alphaess_energy_counters_map_to_predbat_args():
    """The daily kWh counters that feed Predbat's load/import/export/PV learning."""
    failed = False
    client = MockAlphaESS()
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, ONE_DATE_ENERGY_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, {"eload": 19.49, "epvtoday": 10.6})),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.fetch_device_energy("AL70"))
    energy = client.device_energy.get("AL70", {})
    if not ok:
        print("ERROR: fetch_device_energy returned False")
        failed = True
    for leaf, expect in (("import_today", 14.41), ("export_today", 0.42), ("pv_today", 10.6), ("load_today", 19.49)):
        if abs(energy.get(leaf, -1) - expect) > 0.001:
            print(f"ERROR: {leaf} {energy.get(leaf)} != {expect}")
            failed = True
    assert not failed, "test_alphaess_energy_counters_map_to_predbat_args"


def test_alphaess_load_today_falls_back_to_the_energy_balance():
    """getOneDateEnergyBySn has no load field at all, and most SumData fields come back
    null without a configured tariff, so eload needs a fallback."""
    failed = False
    client = MockAlphaESS()
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, ONE_DATE_ENERGY_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, {"eload": None})),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        run_async_local(client.fetch_device_energy("AL70"))
    # epv + eInput - eOutput - eCharge + eDischarge = 10.6 + 14.41 - 0.42 - 12.2 + 7.1
    expect = 10.6 + 14.41 - 0.42 - 12.2 + 7.1
    got = client.device_energy.get("AL70", {}).get("load_today")
    if got is None or abs(got - expect) > 0.001:
        print(f"ERROR: load_today fallback {got} != {expect}")
        failed = True
    assert not failed, "test_alphaess_load_today_falls_back_to_the_energy_balance"
```

Add to the runner list:

```python
        ("energy_counters", test_alphaess_energy_counters_map_to_predbat_args),
        ("load_today_fallback", test_alphaess_load_today_falls_back_to_the_energy_balance),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t6.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t6.log | head
```

Expected: EXCEPTION — `fetch_device_energy` does not exist.

- [ ] **Step 3: Implement the energy tier**

Add to the `alphaess_const` import list in `alphaess.py`:

```python
from alphaess_const import ALPHAESS_ENERGY, ALPHAESS_ENERGY_LOAD_FIELD
```

Append to `AlphaESSAPI`:

```python
    async def fetch_device_energy(self, sn):
        """Populate device_energy for one serial from today's energy totals.

        Two calls, because getOneDateEnergyBySn has no load field at all - load energy
        only exists on getSumDataForCustomer as eload. These counters reset at midnight;
        minute_data/clean_incrementing_reverse absorbs that.
        """
        code, data = await self._get("one_date_energy", params={"sysSn": sn, "queryDate": self._history_query_date()})
        if code != ALPHAESS_CODE_OK or not isinstance(data, dict):
            return False
        energy = {}
        for leaf, field in ALPHAESS_ENERGY.items():
            value = data.get(field)
            if value is not None:
                energy[leaf] = self._as_float(value, 0.0)

        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        sum_code, sum_data = await self._get("sum_data", params={"sysSn": sn})
        load_today = None
        if sum_code == ALPHAESS_CODE_OK and isinstance(sum_data, dict):
            raw_load = sum_data.get(ALPHAESS_ENERGY_LOAD_FIELD)
            if raw_load is not None:
                load_today = self._as_float(raw_load, None)
        if load_today is None:
            # The API docs warn that most SumData fields are null without a configured
            # tariff. Derive it rather than leaving load_today unmapped, which would cost
            # Predbat its load learning entirely.
            derived = self._as_float(data.get("epv"), 0.0) + self._as_float(data.get("eInput"), 0.0) - self._as_float(data.get("eOutput"), 0.0) - self._as_float(data.get("eCharge"), 0.0) + self._as_float(data.get("eDischarge"), 0.0)
            load_today = max(0.0, derived)
        energy["load_today"] = load_today
        self.device_energy[sn] = energy
        return True

    async def refresh_energy(self):
        """Poll the daily energy counters for every inverter."""
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_device_energy(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS energy poll failed for {}: {}".format(sn, error))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("energy")
        return got_any
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t6.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t6.log
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py
git commit -m "feat(alphaess): daily energy counters with an eload energy-balance fallback"
```

---

## Task 7: Publishing, automatic_config and hybrid detection

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Create: `apps/predbat/tests/test_alphaess_publish.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `device_values`, `device_energy`, `device_detail`, `battery_capacity`, `inverter_limit`, `battery_rate_max` from Tasks 4–6.
- Produces:
  - `def _sensor_name(self, sn, leaf) -> str` — `sensor.predbat_alphaess_<sn>_<leaf>`
  - `def _control_name(self, domain, sn, leaf) -> str`
  - `async publish_data(self) -> None`
  - `def detect_ac_coupled(self, sn) -> bool | None` — `True`/`False`/`None` (undecided)
  - `async apply_hybrid_verdict(self) -> None`
  - `async automatic_config(self) -> None`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_alphaess_publish.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS publishing and automatic configuration
# -----------------------------------------------------------------------------

"""Tests for AlphaESS sensor publishing, arg auto-mapping and hybrid detection."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from tests.test_alphaess_api import MockAlphaESS, ESS_LIST_SAMPLE
from tests.test_infra import run_async as run_async_local


def _ready_client(automatic=True, ignore_pv=False):
    """Build a client with two discovered inverters and a full set of readings."""
    client = MockAlphaESS(automatic=automatic)
    client.automatic_ignore_pv = ignore_pv
    client.device_detail = {entry["sysSn"]: entry for entry in ESS_LIST_SAMPLE}
    client.device_list = [entry["sysSn"] for entry in ESS_LIST_SAMPLE]
    for sn in client.device_list:
        client.device_values[sn] = {"soc": 56.0, "battery_power": 1264.0, "grid_power": -11.0, "pv_power": 0.0, "load_power": 1275.0, "ev_power": 0.0}
        client.device_energy[sn] = {"import_today": 14.41, "export_today": 0.42, "pv_today": 10.6, "load_today": 19.49, "battery_charge_today": 12.2, "battery_discharge_today": 7.1, "grid_charge_today": 9.1}
    return client


def test_alphaess_publishes_every_monitoring_sensor():
    """Power, energy and ratings sensors all reach the dashboard under the expected names."""
    failed = False
    client = _ready_client()
    run_async_local(client.publish_data())
    sn = "AL70110230306xx".lower()
    for leaf in ("soc", "battery_power", "grid_power", "pv_power", "load_power", "import_today", "export_today", "pv_today", "load_today", "battery_capacity", "inverter_limit", "battery_rate_max"):
        entity = f"sensor.predbat_alphaess_{sn}_{leaf}"
        if entity not in client.published:
            print(f"ERROR: {entity} not published")
            failed = True
    energy = client.published.get(f"sensor.predbat_alphaess_{sn}_load_today", {})
    # Daily counters reset at midnight; the recorder needs these to keep them.
    if energy.get("attributes", {}).get("device_class") != "energy":
        print(f"ERROR: load_today device_class {energy.get('attributes')}")
        failed = True
    assert not failed, "test_alphaess_publishes_every_monitoring_sensor"


def test_alphaess_automatic_config_forces_the_invert_flags_off():
    """base.args is shared and NOT namespaced per inverter type.

    A Teslemetry or Fox install that legitimately inverts its own grid sensor leaves the
    flag set for every index; an AlphaESS inverter that never claims it inherits the flip,
    the already-correct sensor is negated a second time, and an export reads as an import.
    """
    failed = False
    client = _ready_client()
    client.base.args["grid_power_invert"] = True
    client.base.args["battery_power_invert"] = True
    run_async_local(client.automatic_config())
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        value = client.base.args.get(flag)
        if value != [False, False]:
            print(f"ERROR: {flag} = {value} should be [False, False]")
            failed = True
    assert not failed, "test_alphaess_automatic_config_forces_the_invert_flags_off"


def test_alphaess_automatic_config_maps_the_expected_args():
    """Every arg Predbat needs is bound, and inverter_type/num_inverters are set."""
    failed = False
    client = _ready_client()
    run_async_local(client.automatic_config())
    args = client.base.args
    if args.get("inverter_type") != ["AlphaESSCloud", "AlphaESSCloud"]:
        print(f"ERROR: inverter_type {args.get('inverter_type')}")
        failed = True
    if args.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {args.get('num_inverters')}")
        failed = True
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "inverter_limit", "battery_rate_max", "load_today", "import_today", "export_today", "pv_today"):
        if arg not in args:
            print(f"ERROR: arg {arg} not mapped")
            failed = True
    for arg in ("reserve", "charge_start_time", "charge_end_time", "charge_limit", "charge_rate", "scheduled_charge_enable", "discharge_start_time", "discharge_end_time", "discharge_target_soc", "discharge_rate", "scheduled_discharge_enable", "schedule_write_button"):
        if arg not in args:
            print(f"ERROR: control arg {arg} not mapped")
            failed = True
    assert not failed, "test_alphaess_automatic_config_maps_the_expected_args"


def test_alphaess_export_limit_is_not_guessed():
    """poinv is the inverter rating, not the site's grid-connection limit.

    A G98/G99-capped site can sit far below it, and unlike battery_rate_max nothing
    measures and reports the error back, so guessing here would over-export silently.
    """
    failed = False
    client = _ready_client()
    run_async_local(client.automatic_config())
    if "export_limit" in client.base.args:
        print("ERROR: export_limit should not be auto-mapped")
        failed = True
    if not any("export_limit" in message for message in client.log_messages):
        print(f"ERROR: no export_limit warning, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_export_limit_is_not_guessed"


def test_alphaess_battery_min_soc_is_not_mapped():
    """batUseCap is a field Predbat WRITES; reading it back as the floor would be circular."""
    failed = False
    client = _ready_client()
    run_async_local(client.automatic_config())
    if "battery_min_soc" in client.base.args:
        print("ERROR: battery_min_soc must not be mapped from batUseCap")
        failed = True
    assert not failed, "test_alphaess_battery_min_soc_is_not_mapped"


def test_alphaess_hybrid_switch_only_moves_on_agreeing_evidence():
    """The two errors are NOT symmetric.

    hybrid=False on a real hybrid stops PV counting against inverter_limit, so Predbat
    plans past what the inverter passes and the surplus is clipped with targets silently
    missed. hybrid=True on an AC-coupled system merely under-uses the battery. Predbat
    defaults to True and every mainstream AlphaESS unit is a hybrid, so the switch moves
    only when both signals agree.
    """
    failed = False
    entity = "switch.predbat_inverter_hybrid"

    # Both signals agree on AC coupling: ppvDetail all null AND popv 0 while epv > 0.
    ac = _ready_client()
    for sn in ac.device_list:
        ac.device_detail[sn] = dict(ac.device_detail[sn], popv=0.0)
        ac.device_values[sn]["ppv_detail_all_null"] = True
        ac.device_energy[sn]["pv_today"] = 6.2
    run_async_local(ac.apply_hybrid_verdict())
    if ac.external_state.get(entity) is not False:
        print(f"ERROR: agreeing AC evidence did not flip the switch: {ac.external_state}")
        failed = True

    # A hybrid AT NIGHT reports ZEROS, not nulls. It must never be misread as AC-coupled.
    night = _ready_client()
    for sn in night.device_list:
        night.device_values[sn]["ppv_detail_all_null"] = False
        night.device_energy[sn]["pv_today"] = 0.0
    run_async_local(night.apply_hybrid_verdict())
    if entity in night.external_state:
        print(f"ERROR: a hybrid at night moved the switch: {night.external_state}")
        failed = True

    # Signals disagree: leave Predbat's default alone and say what was seen.
    mixed = _ready_client()
    for sn in mixed.device_list:
        mixed.device_values[sn]["ppv_detail_all_null"] = True
        mixed.device_detail[sn] = dict(mixed.device_detail[sn], popv=9.0)
    run_async_local(mixed.apply_hybrid_verdict())
    if entity in mixed.external_state:
        print(f"ERROR: disagreeing signals moved the switch: {mixed.external_state}")
        failed = True
    if not any("inverter_hybrid" in message for message in mixed.log_messages):
        print(f"ERROR: no advisory log on an undecided verdict, got {mixed.log_messages}")
        failed = True
    assert not failed, "test_alphaess_hybrid_switch_only_moves_on_agreeing_evidence"


def test_alphaess_args_only_mapped_when_every_inverter_reports_them():
    """An arg pointing at a sensor that never appears is worse than an absent arg the
    user can fill in themselves."""
    failed = False
    client = _ready_client()
    # One inverter is missing its load counter entirely.
    del client.device_energy["AL70110230302xx"]["load_today"]
    run_async_local(client.automatic_config())
    if "load_today" in client.base.args:
        print("ERROR: load_today mapped despite one inverter not reporting it")
        failed = True
    if not any("load_today" in message for message in client.log_messages):
        print(f"ERROR: no warning naming load_today, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_args_only_mapped_when_every_inverter_reports_them"


def run_alphaess_publish_tests(my_predbat):
    """Run all AlphaESS publish/config tests."""
    failed = False
    for name, fn in [
        ("monitoring_sensors", test_alphaess_publishes_every_monitoring_sensor),
        ("invert_flags", test_alphaess_automatic_config_forces_the_invert_flags_off),
        ("automatic_args", test_alphaess_automatic_config_maps_the_expected_args),
        ("export_limit_not_guessed", test_alphaess_export_limit_is_not_guessed),
        ("battery_min_soc_not_mapped", test_alphaess_battery_min_soc_is_not_mapped),
        ("hybrid_verdict", test_alphaess_hybrid_switch_only_moves_on_agreeing_evidence),
        ("map_only_when_all_report", test_alphaess_args_only_mapped_when_every_inverter_reports_them),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_publish > /tmp/t7.log 2>&1; tail -20 /tmp/t7.log
```

Expected: FAIL — not registered, and `publish_data` does not exist.

- [ ] **Step 3: Record the ppvDetail signal during telemetry**

`detect_ac_coupled` needs to know whether `ppvDetail` was all-null. Modify `_apply_live_payload` in `alphaess.py` — add this immediately before `self.device_values[sn] = values`:

```python
        # AlphaESS uses null-for-absent in these detail objects: the API docs state
        # pevDetail values are "null when no charger is fitted". A unit with no DC strings
        # should therefore report nulls, while a hybrid at NIGHT reports zeros. Null versus
        # zero is the discriminator, and unlike a PV-power threshold it works at any hour.
        # Applying the pevDetail convention to ppvDetail is inference - VERIFY@FIELD.
        pv_detail = payload.get("ppvDetail") or {}
        strings = [pv_detail.get("ppv{}".format(index)) for index in range(1, 5)]
        values["ppv_detail_all_null"] = bool(pv_detail) and all(value is None for value in strings)
```

- [ ] **Step 4: Implement publishing, hybrid detection and automatic_config**

Append to `AlphaESSAPI` in `alphaess.py`:

```python
    def _sensor_name(self, sn, leaf):
        """Return a namespaced AlphaESS sensor entity id."""
        return "sensor.{}_alphaess_{}_{}".format(self.prefix, sn.lower(), leaf)

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced AlphaESS control entity id."""
        return "{}.{}_alphaess_{}_{}".format(domain, self.prefix, sn.lower(), leaf)

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "ev_power": "W"}
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(
                        self._sensor_name(sn, leaf),
                        state=values[leaf],
                        attributes={"unit_of_measurement": unit, "friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())},
                        app="alphaess",
                    )

            # Ratings are published only when actually derivable - an arg pointing at a
            # sensor that never appears is worse than an absent arg the user can fill in.
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=round(capacity, 3), attributes={"unit_of_measurement": "kWh", "friendly_name": "AlphaESS {} Battery Capacity".format(sn)}, app="alphaess")
            limit = self.inverter_limit(sn)
            if limit > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=round(limit), attributes={"unit_of_measurement": "W", "friendly_name": "AlphaESS {} Inverter Limit".format(sn)}, app="alphaess")
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": "AlphaESS {} Battery Rate Max".format(sn)}, app="alphaess")

            detail = self.device_detail.get(sn, {})
            for leaf, field in (("inverter_model", "minv"), ("battery_model", "mbat"), ("ems_status", "emsStatus")):
                if detail.get(field) is not None:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=detail[field], attributes={"friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())}, app="alphaess")
            # Published but deliberately NOT mapped to soc_percent/soc_kw: the arithmetic in
            # the API docs' live samples fits both "current SOC" and "configured usable
            # depth" equally well, and this is an 8-hour tier anyway. Live SOC comes from
            # LastPower.soc where there is no ambiguity.
            for leaf, field, unit in (("pv_nominal", "popv", "kW"), ("usable_capacity", "usCapacity", "%"), ("surplus_capacity", "surplusCobat", "kWh")):
                if detail.get(field) is not None:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=detail[field], attributes={"unit_of_measurement": unit, "friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())}, app="alphaess")

            # Daily energy counters feed Predbat's load/import/export learning. They reset
            # at midnight; minute_data/clean_incrementing_reverse absorbs that.
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "AlphaESS {} {}".format(sn, leaf.replace("_", " ").title())},
                    app="alphaess",
                )

    def detect_ac_coupled(self, sn):
        """Return True (AC-coupled), False (hybrid) or None (undecided) for one serial.

        Two signals must AGREE before a verdict is returned, because the errors are not
        symmetric - see apply_hybrid_verdict.
        """
        detail = self.device_detail.get(sn, {})
        if str(detail.get("minv", "")) in ALPHAESS_AC_COUPLED_MODELS:
            return True
        values = self.device_values.get(sn, {})
        strings_absent = values.get("ppv_detail_all_null")
        if strings_absent is None:
            return None
        pv_nameplate = self._as_float(detail.get("popv"), 0.0)
        pv_energy = self._as_float(self.device_energy.get(sn, {}).get("pv_today"), 0.0)
        # Signal 2 needs daylight to mean anything: with no PV energy yet today, "popv is
        # zero" says nothing about where the PV is.
        nameplate_says_ac = pv_nameplate <= 0 and pv_energy > 0
        if strings_absent and nameplate_says_ac:
            return True
        if not strings_absent:
            return False
        return None

    async def apply_hybrid_verdict(self):
        """Move switch.predbat_inverter_hybrid only on positive evidence of AC coupling.

        inverter_hybrid is one of Predbat's OWN CONFIG_ITEMS switches rather than an
        apps.yaml arg, so it is written with set_state_external - writing the entity state
        alone would move the displayed switch without changing the value the planner reads.

        The two errors are NOT symmetric. inverter_hybrid False on an actually-hybrid
        system stops PV counting against inverter_limit, so Predbat plans charge-plus-PV
        beyond what the inverter can pass and the surplus is clipped with targets silently
        missed. True on an actually-AC-coupled system merely under-uses the battery.
        Predbat defaults to True and every mainstream AlphaESS unit is a hybrid, so the
        switch is only ever moved towards AC-coupled, and only on agreeing evidence.
        """
        if not self.device_list:
            return
        verdicts = [self.detect_ac_coupled(sn) for sn in self.device_list]
        entity = "switch.{}_inverter_hybrid".format(self.prefix)
        if verdicts and all(verdict is True for verdict in verdicts):
            models = ", ".join(str(self.device_detail.get(sn, {}).get("minv", "unknown")) for sn in self.device_list)
            self.log("Info: AlphaESS detected AC coupling (no DC strings reported, and PV energy with no PV nameplate) for model(s) {}; setting {} off".format(models, entity))
            await self.set_state_external(entity, False)
            return
        if any(verdict is None for verdict in verdicts):
            models = ", ".join(str(self.device_detail.get(sn, {}).get("minv", "unknown")) for sn in self.device_list)
            self.log("Info: AlphaESS could not determine hybrid versus AC coupling for model(s) {}; leaving {} at its current value. If this is an AC-coupled retrofit, turn that switch off by hand.".format(models, entity))

    async def automatic_config(self):
        """Register every discovered inverter as an AlphaESSCloud Predbat inverter."""
        devices = list(self.device_list)
        if not devices:
            self.log("Warn: AlphaESS automatic_config found no inverters")
            return
        self.set_arg_auto("inverter_type", ["AlphaESSCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))
        self.set_arg_auto("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg_auto("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg_auto("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        self.set_arg_auto("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])
        # Own the sign flags rather than leaving them to whatever else configured this
        # install. base.args is shared and NOT namespaced per inverter type, so a component
        # that legitimately inverts its own grid sensor - teslemetry sets grid_power_invert
        # True, fox does the same - leaves that key set for every inverter index, and an
        # AlphaESS inverter that never claims it inherits the flip. The published sensor is
        # then correct and inverter.py negates it again, so an export reads as an import.
        # All three are False because publish_data already emits Predbat's conventions.
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])

        # Only map an arg when EVERY inverter reports the underlying value.
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in devices):
                self.set_arg_auto(leaf, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log("Warn: AlphaESS not every inverter reports {}, it must be set manually in apps.yaml".format(leaf))

        if all(self.battery_capacity(sn) > 0 for sn in devices):
            self.set_arg_auto("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: AlphaESS no battery capacity available for every inverter, soc_max must be set manually in apps.yaml")
        if all(self.inverter_limit(sn) > 0 for sn in devices):
            self.set_arg_auto("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: AlphaESS no poinv reported, inverter_limit must be set manually in apps.yaml")
        if all(self.battery_rate_max(sn) > 0 for sn in devices):
            self.set_arg_auto("battery_rate_max", [self._sensor_name(sn, "battery_rate_max") for sn in devices])
        else:
            self.log("Warn: AlphaESS no battery rate available, battery_rate_max must be set manually in apps.yaml")
        # Deliberately NOT auto-mapped. poinv is the inverter rating, not the site's
        # grid-connection limit, and a G98/G99-capped site can sit far below it. Unlike
        # battery_rate_max, nothing measures and reports this error back, so a guess would
        # over-export silently. Predbat falls back to 99999W until the user sets it.
        self.log("Warn: AlphaESS does not report an export power limit; set export_limit in apps.yaml if your grid connection is capped below the inverter rating, otherwise Predbat will plan exports it cannot deliver")
        # battery_min_soc is deliberately NOT mapped: batUseCap is a field Predbat writes,
        # so reading it back as the floor would be circular.

        self.set_arg_auto("reserve", [self._control_name("number", sn, "battery_schedule_reserve") for sn in devices])
        self.set_arg_auto("charge_start_time", [self._control_name("select", sn, "battery_schedule_charge_start_time") for sn in devices])
        self.set_arg_auto("charge_end_time", [self._control_name("select", sn, "battery_schedule_charge_end_time") for sn in devices])
        self.set_arg_auto("charge_limit", [self._control_name("number", sn, "battery_schedule_charge_soc") for sn in devices])
        self.set_arg_auto("charge_rate", [self._control_name("number", sn, "battery_schedule_charge_power") for sn in devices])
        self.set_arg_auto("scheduled_charge_enable", [self._control_name("switch", sn, "battery_schedule_charge_enable") for sn in devices])
        self.set_arg_auto("discharge_start_time", [self._control_name("select", sn, "battery_schedule_export_start_time") for sn in devices])
        self.set_arg_auto("discharge_end_time", [self._control_name("select", sn, "battery_schedule_export_end_time") for sn in devices])
        self.set_arg_auto("discharge_target_soc", [self._control_name("number", sn, "battery_schedule_export_soc") for sn in devices])
        self.set_arg_auto("discharge_rate", [self._control_name("number", sn, "battery_schedule_export_power") for sn in devices])
        self.set_arg_auto("scheduled_discharge_enable", [self._control_name("switch", sn, "battery_schedule_export_enable") for sn in devices])
        self.set_arg_auto("schedule_write_button", [self._control_name("switch", sn, "battery_schedule_charge_write") for sn in devices])

        await self.apply_hybrid_verdict()
```

Add `ALPHAESS_AC_COUPLED_MODELS` to the `alphaess_const` imports.

- [ ] **Step 5: Register the test module**

In `apps/predbat/unit_test.py`:

```python
from tests.test_alphaess_publish import run_alphaess_publish_tests
```

```python
        ("alphaess_publish", run_alphaess_publish_tests, "AlphaESS publish/config tests", False),
```

- [ ] **Step 6: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t7.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t7.log
```

Expected: all four AlphaESS suites PASSED.

- [ ] **Step 7: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_publish.py apps/predbat/unit_test.py
git commit -m "feat(alphaess): publish monitoring sensors, auto-map args and detect AC coupling"
```

---

## Task 8: Control entities and event routing

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Create: `apps/predbat/tests/test_alphaess_control.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `_control_name` from Task 7.
- Produces:
  - `async publish_schedule_settings_ha(self, sn) -> None`
  - `async get_schedule_settings_ha(self, sn) -> dict`
  - `def _empty_schedule() -> dict` (staticmethod)
  - `def _sn_from_entity(self, entity_id) -> str | None`
  - `def _to_bool(value, current=False) -> bool` (staticmethod)
  - `def update_local_schedule(self, sn, entity_id, value) -> None`
  - `async select_event/number_event/switch_event(self, entity_id, value)`
  - Schedule shape: `{"reserve": int, "charge": {"enable": bool, "soc": int, "power": int, "start": "HH:MM:SS", "end": "HH:MM:SS"}, "export": {...}}`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_alphaess_control.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS control derivation and write path
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS control entities, payload derivation and write gating."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from tests.test_alphaess_api import MockAlphaESS
from tests.test_infra import run_async as run_async_local


def _schedule(reserve=10, charge=None, export=None, charge_power=3000, export_power=3000):
    """Build a schedule dict in the shape the control entities produce.

    A DISABLED window still carries a power, because the real control entities do:
    adjust_charge_rate/adjust_discharge_rate write Predbat's rates every cycle whether or
    not a window is enabled. That matters because a ZERO rate is precisely how Predbat
    signals freeze on this inverter (there is no pause endpoint), so a fixture leaving
    both at zero would silently be testing a freeze instead of the demand state it reads as.
    """
    idle_charge = {"enable": False, "soc": 0, "power": charge_power, "start": "00:00:00", "end": "00:00:00"}
    idle_export = {"enable": False, "soc": 0, "power": export_power, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": charge or idle_charge, "export": export or idle_export}


def _client(sn="AL70"):
    """Build a client with one discovered inverter ready for control tests."""
    client = MockAlphaESS()
    client.device_list = [sn]
    client.device_detail = {sn: {"sysSn": sn, "cobat": 13.34, "poinv": 5.0, "popv": 9.0, "minv": "SMILE5-INV"}}
    client.device_values = {sn: {"soc": 50.0}}
    client.device_config[sn] = {
        "charge": {"gridCharge": 0, "timeChaf1": "00:00", "timeChae1": "00:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 100},
        "discharge": {"ctrDis": 0, "timeDisf1": "00:00", "timeDise1": "00:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 10},
    }
    return client


def test_alphaess_control_entities_round_trip():
    """Published control entities read back into exactly the schedule shape they came from."""
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule(reserve=12, charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes Predbat
    # replace these entities with its own dummies and the window never arrives.
    start = client.published.get("select.predbat_alphaess_al70_battery_schedule_charge_start_time", {}).get("state")
    if start != "01:00:00":
        print(f"ERROR: published start {start}")
        failed = True
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["charge"]["enable"] is not True or read_back["charge"]["soc"] != 90 or read_back["reserve"] != 12:
        print(f"ERROR: read back {read_back}")
        failed = True
    assert not failed, "test_alphaess_control_entities_round_trip"


def test_alphaess_reserve_entity_is_published_unclamped():
    """Predbat writes then reads back to confirm (write_and_poll_value).

    Publishing anything other than what was written guarantees a mismatch and a retry
    storm; the floor is enforced at the API boundary in the payload builder instead.
    """
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule(reserve=3)
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    state = client.published.get("number.predbat_alphaess_al70_battery_schedule_reserve", {}).get("state")
    if state != 3:
        print(f"ERROR: reserve published as {state}, should echo the written 3 unclamped")
        failed = True
    assert not failed, "test_alphaess_reserve_entity_is_published_unclamped"


def test_alphaess_entity_routing_does_not_confuse_prefixed_serials():
    """An entity for AL701 must never route to AL70 - that would write to the wrong inverter."""
    failed = False
    client = _client()
    client.device_list = ["AL70", "AL701"]
    if client._sn_from_entity("number.predbat_alphaess_al701_battery_schedule_reserve") != "AL701":
        print("ERROR: AL701 entity routed to the wrong serial")
        failed = True
    if client._sn_from_entity("number.predbat_alphaess_al70_battery_schedule_reserve") != "AL70":
        print("ERROR: AL70 entity misrouted")
        failed = True
    if client._sn_from_entity("number.predbat_alphaess_zz99_battery_schedule_reserve") is not None:
        print("ERROR: unknown serial should not resolve")
        failed = True
    assert not failed, "test_alphaess_entity_routing_does_not_confuse_prefixed_serials"


def test_alphaess_update_local_schedule_applies_each_field():
    """Each control entity change lands on the right field of the held schedule."""
    failed = False
    client = _client()
    for entity, value, path in [
        ("number.predbat_alphaess_al70_battery_schedule_reserve", 15, ("reserve",)),
        ("select.predbat_alphaess_al70_battery_schedule_charge_start_time", "02:30:00", ("charge", "start")),
        ("number.predbat_alphaess_al70_battery_schedule_charge_soc", 85, ("charge", "soc")),
        ("number.predbat_alphaess_al70_battery_schedule_export_power", 4000, ("export", "power")),
        ("switch.predbat_alphaess_al70_battery_schedule_export_enable", "turn_on", ("export", "enable")),
    ]:
        client.update_local_schedule("AL70", entity, value)
    schedule = client.local_schedule["AL70"]
    if schedule["reserve"] != 15 or schedule["charge"]["start"] != "02:30:00" or schedule["charge"]["soc"] != 85:
        print(f"ERROR: schedule {schedule}")
        failed = True
    if schedule["export"]["power"] != 4000 or schedule["export"]["enable"] is not True:
        print(f"ERROR: export {schedule['export']}")
        failed = True
    assert not failed, "test_alphaess_update_local_schedule_applies_each_field"


def run_alphaess_control_tests(my_predbat):
    """Run all AlphaESS control-logic tests."""
    failed = False
    for name, fn in [
        ("entities_round_trip", test_alphaess_control_entities_round_trip),
        ("reserve_unclamped", test_alphaess_reserve_entity_is_published_unclamped),
        ("entity_routing", test_alphaess_entity_routing_does_not_confuse_prefixed_serials),
        ("update_local_schedule", test_alphaess_update_local_schedule_applies_each_field),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t8.log 2>&1; tail -20 /tmp/t8.log
```

Expected: FAIL.

- [ ] **Step 3: Implement the control entities**

Append to `AlphaESSAPI` in `alphaess.py`:

```python
    @staticmethod
    def _empty_schedule():
        """Return a fresh, disabled schedule shape - the single source of truth for its defaults.

        Used where a schedule has to be seeded from nothing: a control event arriving for a
        serial local_schedule has not seen yet. Kept as one helper rather than a literal
        repeated at each call site, so adding or renaming a field cannot silently diverge
        between copies. run() deliberately does NOT seed from here - it reads the control
        entities instead, whose per-field defaults produce exactly this shape when nothing
        has been published yet, but which hold Predbat's live plan after a restart.
        """
        return {
            "reserve": 0,
            "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        }

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        # Deliberately NOT clamped to any floor. This entity is Predbat's control surface:
        # it writes a value then reads it back to confirm (write_and_poll_value), so
        # publishing anything other than what was written guarantees a mismatch and a retry
        # storm. Clamping happens at the API boundary in the payload builder.
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "AlphaESS {} Battery Schedule Reserve".format(sn), "icon": "mdi:gauge"},
            app="alphaess",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes
            # Predbat replace these entities with its own dummies (inverter.py, the
            # inv_charge_time_format != "HH:MM:SS" branch) and the window never arrives.
            self.dashboard_item(self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), state=window.get("start", "00:00:00"), attributes={"friendly_name": "AlphaESS {} {} Start".format(sn, direction.title()), "icon": "mdi:clock-outline"}, app="alphaess")
            self.dashboard_item(self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), state=window.get("end", "00:00:00"), attributes={"friendly_name": "AlphaESS {} {} End".format(sn, direction.title()), "icon": "mdi:clock-outline"}, app="alphaess")
            self.dashboard_item(self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)), state=int(window.get("soc", 0)), attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "AlphaESS {} {} SoC".format(sn, direction.title()), "icon": "mdi:gauge"}, app="alphaess")
            self.dashboard_item(self._control_name("number", sn, "battery_schedule_{}_power".format(direction)), state=int(window.get("power", 0)), attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": "AlphaESS {} {} Power".format(sn, direction.title()), "icon": "mdi:flash"}, app="alphaess")
            self.dashboard_item(self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), state="on" if window.get("enable") else "off", attributes={"friendly_name": "AlphaESS {} {} Enable".format(sn, direction.title()), "icon": "mdi:check-circle-outline"}, app="alphaess")
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": "AlphaESS {} Schedule Write".format(sn), "icon": "mdi:content-save"}, app="alphaess")

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape the payload builder consumes.

        Numeric casts route through _as_float so an entity legitimately reporting
        "unknown"/"unavailable" - for instance right after a HA restart, before Predbat
        republishes - falls back to 0 rather than raising and killing the control loop.
        """
        schedule = {"reserve": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), default="00:00:00"),
                "soc": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)), default=0), 0)),
                "power": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_{}_power".format(direction)), default=0), 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule

    def _sn_from_entity(self, entity_id):
        """Extract the serial from an AlphaESS entity id, or None if unresolvable.

        Entity ids are always {domain}.{prefix}_alphaess_{sn}_{leaf}, so the serial is
        always followed by "_". Matching sn + "_" rather than a bare prefix keeps
        prefix-colliding serials apart - an entity for AL701 must never route to AL70,
        which would send a control write to the wrong inverter.
        """
        text = str(entity_id).lower()
        for sn in self.device_list:
            if "_alphaess_{}_".format(sn.lower()) in text:
                return sn
        return None

    @staticmethod
    def _to_bool(value, current=False):
        """Coerce a switch service or state to a boolean, keeping current when unknown."""
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("turn_on", "on", "true", "1"):
            return True
        if text in ("turn_off", "off", "false", "0"):
            return False
        if text == "toggle":
            return not current
        return current

    def update_local_schedule(self, sn, entity_id, value):
        """Apply one control-entity change to the locally held schedule."""
        schedule = self.local_schedule.setdefault(sn, self._empty_schedule())
        leaf = str(entity_id).split("_alphaess_{}_".format(sn.lower()), 1)[-1]
        if leaf == "battery_schedule_reserve":
            schedule["reserve"] = int(self._as_float(value, 0))
            return
        for direction in ("charge", "export"):
            prefix = "battery_schedule_{}_".format(direction)
            if not leaf.startswith(prefix):
                continue
            field = leaf[len(prefix) :]
            window = schedule.setdefault(direction, {})
            if field in ("start_time", "end_time"):
                window[field.replace("_time", "")] = str(value)
            elif field in ("soc", "power"):
                window[field] = int(self._as_float(value, 0))
            elif field == "enable":
                window["enable"] = self._to_bool(value, window.get("enable", False))
            return

    async def select_event(self, entity_id, value):
        """Handle a select entity change."""
        await self._handle_control_event(entity_id, value)

    async def number_event(self, entity_id, value):
        """Handle a number entity change."""
        await self._handle_control_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle a switch entity service call."""
        await self._handle_control_event(entity_id, service)
```

`_handle_control_event` is written in Task 10 (it needs `apply_schedule`). Add this placeholder now so the tests import cleanly, and replace it in Task 10:

```python
    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right inverter and apply it."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log("Warn: AlphaESS could not resolve an inverter for {}".format(entity_id))
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)
```

- [ ] **Step 4: Register the test module**

In `apps/predbat/unit_test.py`:

```python
from tests.test_alphaess_control import run_alphaess_control_tests
```

```python
        ("alphaess_control", run_alphaess_control_tests, "AlphaESS control-logic tests", False),
```

- [ ] **Step 5: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t8.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t8.log
```

Expected: all five AlphaESS suites PASSED.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_control.py apps/predbat/unit_test.py
git commit -m "feat(alphaess): publish control entities and route control events"
```

---

## Task 9: Payload building — the control mapping

This is the heart of the component. Predbat's controls map straight onto the AlphaESS
schedule fields and the inverter does the timing; there is no per-instant state machine.

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_control.py`

**Interfaces:**
- Consumes: schedule shape from Task 8, `snap_time_grid`/`window_is_empty`/`hhmmss_to_hhmm` from Task 1, `device_config` (read in Task 10).
- Produces:
  - `def split_window(self, start, end) -> tuple[tuple[str, str], tuple[str, str]]` — period 1 and period 2 as `HH:mm` pairs
  - `def build_charge_payload(self, sn, schedule) -> dict`
  - `def build_discharge_payload(self, sn, schedule) -> dict`
  - `def payloads_equal(a, b) -> bool` (staticmethod)

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_control.py`:

```python
def test_alphaess_controls_pass_straight_through():
    """Predbat's controls map onto the schedule fields verbatim; the inverter does timing.

    execute.py:514 already gates how far ahead a window is programmed
    ((minutes_start - minutes_now) <= set_window_minutes), so Predbat never hands the
    component a window hours in advance. That is why the naive pass-through is safe and no
    window-blanking state machine is needed.
    """
    failed = False
    client = _client()
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "17:00:00", "end": "19:00:00"},
    )
    charge = client.build_charge_payload("AL70", schedule)
    discharge = client.build_discharge_payload("AL70", schedule)
    for key, expect in (("sysSn", "AL70"), ("gridCharge", 1), ("timeChaf1", "01:00"), ("timeChae1", "05:00"), ("batHighCap", 90)):
        if charge.get(key) != expect:
            print(f"ERROR: charge[{key}] = {charge.get(key)} != {expect}")
            failed = True
    for key, expect in (("sysSn", "AL70"), ("ctrDis", 1), ("timeDisf1", "17:00"), ("timeDise1", "19:00")):
        if discharge.get(key) != expect:
            print(f"ERROR: discharge[{key}] = {discharge.get(key)} != {expect}")
            failed = True
    # batUseCap is the EXPORT TARGET while an export window is programmed.
    if discharge.get("batUseCap") != 20:
        print(f"ERROR: batUseCap {discharge.get('batUseCap')} should be the export target 20")
        failed = True
    # Period 2 is the midnight split, not a state - disabled when no split occurred.
    for key in ("timeChaf2", "timeChae2"):
        if charge.get(key) != "00:00":
            print(f"ERROR: {key} = {charge.get(key)} should be disabled")
            failed = True
    assert not failed, "test_alphaess_controls_pass_straight_through"


def test_alphaess_batusecap_is_the_reserve_outside_an_export_window():
    """batUseCap serves two Predbat concepts because the API has one field for the floor."""
    failed = False
    client = _client()
    schedule = _schedule(reserve=25, charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    discharge = client.build_discharge_payload("AL70", schedule)
    if discharge.get("batUseCap") != 25:
        print(f"ERROR: batUseCap {discharge.get('batUseCap')} should be the reserve 25")
        failed = True
    # With no export window and a non-zero discharge rate, discharge time control is OFF -
    # that is demand mode, the battery covers the house normally.
    if discharge.get("ctrDis") != 0:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should be 0 in demand mode")
        failed = True
    assert not failed, "test_alphaess_batusecap_is_the_reserve_outside_an_export_window"


def test_alphaess_rate_zero_is_freeze():
    """AlphaESS has no pause endpoint, so Predbat expresses freeze by zeroing the rates
    (execute.py:491-495). Zero is a distinct instruction, not just 'slow'."""
    failed = False
    client = _client()

    # charge_rate == 0 -> no grid charging, overriding the charge window (freeze charge,
    # and no cross-charging during an export).
    frozen_charge = _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 0, "start": "01:00:00", "end": "05:00:00"})
    charge = client.build_charge_payload("AL70", frozen_charge)
    if charge.get("gridCharge") != 0:
        print(f"ERROR: gridCharge {charge.get('gridCharge')} should be 0 when charge_rate is 0")
        failed = True

    # discharge_rate == 0 -> ctrDis 1 with BOTH periods disabled, so the battery holds SOC.
    frozen_export = _schedule(reserve=10, export_power=0)
    discharge = client.build_discharge_payload("AL70", frozen_export)
    if discharge.get("ctrDis") != 1:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should be 1 to hold SOC")
        failed = True
    for key in ("timeDisf1", "timeDise1", "timeDisf2", "timeDise2"):
        if discharge.get(key) != "00:00":
            print(f"ERROR: {key} = {discharge.get(key)} should be disabled to hold SOC")
            failed = True
    assert not failed, "test_alphaess_rate_zero_is_freeze"


def test_alphaess_both_rates_zero_still_holds_the_battery():
    """Both rates zero is a HOLD, not an absence of a plan.

    Predbat reaches this state through ordinary settings, so it must not be read as "no
    plan": set_freeze_export_during_demand zeroes the charge rate (execute.py:532/538 -
    AlphaESSCloud has has_timed_pause False, so the else branch fires), while
    car-charging-from-battery-disable (execute.py:564) or iboost_prevent_discharge
    (execute.py:591) zeroes the discharge rate in the same pass. Emitting ctrDis 0 here
    would discharge the battery into the EV or iBoost load - the exact hold Predbat asked
    to prevent, and silently. Stranding an undriven system is prevented by the
    control_active gate in _reconcile_control instead.
    """
    failed = False
    client = _client()
    schedule = _schedule(reserve=15, charge_power=0, export_power=0)
    discharge = client.build_discharge_payload("AL70", schedule)
    if discharge.get("ctrDis") != 1:
        print(f"ERROR: ctrDis {discharge.get('ctrDis')} should be 1 - both rates zero is a hold")
        failed = True
    for key in ("timeDisf1", "timeDise1", "timeDisf2", "timeDise2"):
        if discharge.get(key) != "00:00":
            print(f"ERROR: {key} = {discharge.get(key)} should be disabled to hold SOC")
            failed = True
    assert not failed, "test_alphaess_both_rates_zero_still_holds_the_battery"


def test_alphaess_times_snap_inward_to_the_15_minute_grid():
    """Off-grid values are accepted by the API and silently ignored by the device."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:07:00", "end": "05:53:00"})
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("timeChaf1") != "01:15" or charge.get("timeChae1") != "05:45":
        print(f"ERROR: snapped window {charge.get('timeChaf1')}-{charge.get('timeChae1')} should be 01:15-05:45")
        failed = True
    assert not failed, "test_alphaess_times_snap_inward_to_the_15_minute_grid"


def test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped():
    """An inverted window must NOT be written as a wrap-around - wrap behaviour is
    undocumented, so it is disabled instead and the decision is logged."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:05:00", "end": "01:10:00"})
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("gridCharge") != 0 or charge.get("timeChaf1") != "00:00" or charge.get("timeChae1") != "00:00":
        print(f"ERROR: collapsed window not disabled: {charge}")
        failed = True
    if not any("collaps" in message.lower() or "too short" in message.lower() for message in client.log_messages):
        print(f"ERROR: no log for the collapsed window, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped"


def test_alphaess_midnight_end_snaps_to_the_maximum():
    """23:45 is the documented maximum, so Predbat's midnight end lands there."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "22:00:00", "end": "24:00:00"})
    charge = client.build_charge_payload("AL70", schedule)
    if charge.get("timeChae1") != "23:45":
        print(f"ERROR: 24:00 end snapped to {charge.get('timeChae1')} != 23:45")
        failed = True
    assert not failed, "test_alphaess_midnight_end_snaps_to_the_maximum"


def test_alphaess_period_two_carries_the_midnight_split():
    """can_span_midnight is False, so Predbat splits and period 2 takes the remainder."""
    failed = False
    client = _client()
    (start1, end1), (start2, end2) = client.split_window("23:00:00", "26:00:00")
    if (start1, end1) != ("23:00", "23:45"):
        print(f"ERROR: period 1 {start1}-{end1}")
        failed = True
    if (start2, end2) != ("00:00", "02:00"):
        print(f"ERROR: period 2 {start2}-{end2}")
        failed = True
    assert not failed, "test_alphaess_period_two_carries_the_midnight_split"


def test_alphaess_reserve_is_clamped_at_the_api_boundary():
    """The entity is published unclamped; the payload is where the API's limits apply."""
    failed = False
    client = _client()
    low = client.build_discharge_payload("AL70", _schedule(reserve=0))
    if not 0 <= low.get("batUseCap", -1) <= 100:
        print(f"ERROR: batUseCap {low.get('batUseCap')} out of range")
        failed = True
    high = client.build_charge_payload("AL70", _schedule(charge={"enable": True, "soc": 150, "power": 3000, "start": "01:00:00", "end": "05:00:00"}))
    if high.get("batHighCap") != 100:
        print(f"ERROR: batHighCap {high.get('batHighCap')} should clamp to 100")
        failed = True
    assert not failed, "test_alphaess_reserve_is_clamped_at_the_api_boundary"


def test_alphaess_payload_is_a_full_replacement():
    """update*ConfigInfo replaces the whole object - all seven fields must be present or
    the omitted ones are silently reset."""
    failed = False
    client = _client()
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    charge = client.build_charge_payload("AL70", schedule)
    for key in ("sysSn", "batHighCap", "gridCharge", "timeChaf1", "timeChae1", "timeChaf2", "timeChae2"):
        if key not in charge:
            print(f"ERROR: charge payload missing {key}")
            failed = True
    discharge = client.build_discharge_payload("AL70", schedule)
    for key in ("sysSn", "batUseCap", "ctrDis", "timeDisf1", "timeDise1", "timeDisf2", "timeDise2"):
        if key not in discharge:
            print(f"ERROR: discharge payload missing {key}")
            failed = True
    assert not failed, "test_alphaess_payload_is_a_full_replacement"
```

Add to the `run_alphaess_control_tests` list:

```python
        ("controls_pass_through", test_alphaess_controls_pass_straight_through),
        ("batusecap_is_reserve", test_alphaess_batusecap_is_the_reserve_outside_an_export_window),
        ("rate_zero_is_freeze", test_alphaess_rate_zero_is_freeze),
        ("both_rates_zero_holds", test_alphaess_both_rates_zero_still_holds_the_battery),
        ("snap_inward", test_alphaess_times_snap_inward_to_the_15_minute_grid),
        ("collapsed_disabled", test_alphaess_window_collapsed_by_snapping_is_disabled_not_wrapped),
        ("midnight_end_snaps", test_alphaess_midnight_end_snaps_to_the_maximum),
        ("midnight_split", test_alphaess_period_two_carries_the_midnight_split),
        ("clamped_at_boundary", test_alphaess_reserve_is_clamped_at_the_api_boundary),
        ("full_replacement", test_alphaess_payload_is_a_full_replacement),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t9.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t9.log | head
```

Expected: EXCEPTION — `build_charge_payload` does not exist.

- [ ] **Step 3: Implement the payload builders**

Add to the `alphaess_const` imports in `alphaess.py`:

```python
from alphaess_const import (
    snap_time_grid,
    hhmmss_to_hhmm,
    hm_to_minutes,
    window_is_empty,
    ALPHAESS_TIME_DISABLED,
    ALPHAESS_TIME_MAX,
)
```

Append to `AlphaESSAPI`:

```python
    @staticmethod
    def _clamp_percent(value, low=0, high=100):
        """Clamp a SOC percentage to the API's accepted range."""
        try:
            return int(max(low, min(high, int(float(value)))))
        except (TypeError, ValueError):
            return low

    def split_window(self, start, end):
        """Split a window at midnight, returning ((start1, end1), (start2, end2)) in HH:mm.

        INVERTER_DEF sets can_span_midnight False because wrap-around behaviour is
        undocumented for timeChaf1/timeChae1, so Predbat splits rather than writing a
        window whose meaning is unknown. Period 2 exists for the remainder and for nothing
        else - it is the midnight split, not a state.
        """
        start_min = hm_to_minutes(hhmmss_to_hhmm(start))
        end_text = str(end or "")
        end_hours = end_text.split(":")[0] if ":" in end_text else "0"
        try:
            end_min = int(end_hours) * 60 + int(end_text.split(":")[1])
        except (TypeError, ValueError, IndexError):
            end_min = start_min
        if end_min <= 24 * 60:
            return (hhmmss_to_hhmm(start), hhmmss_to_hhmm(end)), (ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED)
        wrapped = end_min - 24 * 60
        second_end = "{:02d}:{:02d}".format(wrapped // 60, wrapped % 60)
        return (hhmmss_to_hhmm(start), ALPHAESS_TIME_MAX), (ALPHAESS_TIME_DISABLED, second_end)

    def _snapped_periods(self, sn, direction, start, end, enabled):
        """Return two snapped HH:mm period pairs, disabling anything with no usable time.

        Times snap INWARD (start up, end down) so a snapped window is never wider than the
        one Predbat asked for. If snapping collapses or inverts a window it is written as
        disabled rather than as a wrap-around, and the decision is logged - a silently
        ignored off-grid window would look written and never run.
        """
        if not enabled:
            return (ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED), (ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED)
        (raw_start1, raw_end1), (raw_start2, raw_end2) = self.split_window(start, end)
        periods = []
        for index, (raw_start, raw_end) in enumerate(((raw_start1, raw_end1), (raw_start2, raw_end2)), start=1):
            if raw_start == ALPHAESS_TIME_DISABLED and raw_end == ALPHAESS_TIME_DISABLED:
                periods.append((ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED))
                continue
            snapped_start = snap_time_grid(raw_start, "start")
            snapped_end = snap_time_grid(raw_end, "end")
            if window_is_empty(snapped_start, snapped_end):
                if index == 1:
                    self.log("Info: AlphaESS {} {} window {}-{} collapsed to nothing on the API's 15-minute grid, so it is written as disabled rather than as an undocumented wrap-around".format(sn, direction, raw_start, raw_end))
                periods.append((ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED))
                continue
            periods.append((snapped_start, snapped_end))
        return periods[0], periods[1]

    def build_charge_payload(self, sn, schedule):
        """Build the full updateChargeConfigInfo body for one inverter.

        A FULL REPLACEMENT, not a patch: all seven fields are always present, because the
        endpoint silently resets anything omitted.
        """
        window = schedule.get("charge", {}) or {}
        enabled = bool(window.get("enable"))
        # charge_rate zero is Predbat signalling freeze charge / no cross-charging. There
        # is no pause endpoint, so a zeroed rate is the only signal available and it
        # overrides the window outright.
        rate = self._as_float(window.get("power"), 0.0)
        if enabled and rate <= 0:
            enabled = False
        (start1, end1), (start2, end2) = self._snapped_periods(sn, "charge", window.get("start"), window.get("end"), enabled)
        # BOTH periods must be empty before the payload is disabled. Period 2 is an
        # independent window carrying the midnight-split remainder, so a period 1 that
        # snapping collapsed must not take a valid period 2 down with it - a 23:50-26:00
        # window collapses period 1 to 23:45-23:45 and leaves a real 00:00-02:00 in
        # period 2, which would otherwise be written and then never run.
        if window_is_empty(start1, end1) and window_is_empty(start2, end2):
            enabled = False
        return {
            "sysSn": sn,
            "gridCharge": 1 if enabled else 0,
            "timeChaf1": start1,
            "timeChae1": end1,
            "timeChaf2": start2,
            "timeChae2": end2,
            "batHighCap": self._clamp_percent(window.get("soc", 100) if enabled else 100),
        }

    def build_discharge_payload(self, sn, schedule):
        """Build the full updateDisChargeConfigInfo body for one inverter.

        batUseCap serves two Predbat concepts because the API has only one field for the
        discharge floor: it is the export target while an export window is programmed and
        the reserve otherwise.
        """
        window = schedule.get("export", {}) or {}
        enabled = bool(window.get("enable"))
        reserve = self._clamp_percent(schedule.get("reserve", 0))
        export_rate = self._as_float(window.get("power"), 0.0)

        # discharge_rate zero means hold SOC (freeze export): discharge time control ON with
        # no permitted period, so the battery cannot discharge at all. This is NOT
        # conditioned on the charge rate. Predbat reaches both-rates-zero through ordinary
        # settings - set_freeze_export_during_demand zeroes the charge rate
        # (execute.py:532/538, and AlphaESSCloud has has_timed_pause False so the else
        # branch fires), while car-charging-from-battery-disable (execute.py:564) or
        # iboost_prevent_discharge (execute.py:591) zeroes the discharge rate in the same
        # pass. Treating that as "no plan" would emit ctrDis 0 and discharge the battery
        # into the EV or iBoost load, defeating the exact hold Predbat asked for. Stranding
        # an undriven system is instead prevented by the control_active gate in
        # _reconcile_control, which only re-applies for a serial Predbat has been asked to
        # drive.
        if export_rate <= 0:
            return {
                "sysSn": sn,
                "ctrDis": 1,
                "timeDisf1": ALPHAESS_TIME_DISABLED,
                "timeDise1": ALPHAESS_TIME_DISABLED,
                "timeDisf2": ALPHAESS_TIME_DISABLED,
                "timeDise2": ALPHAESS_TIME_DISABLED,
                "batUseCap": reserve,
            }

        (start1, end1), (start2, end2) = self._snapped_periods(sn, "export", window.get("start"), window.get("end"), enabled)
        # Both periods, for the same reason as the charge side: a collapsed period 1 must
        # not discard a valid midnight-split period 2, which here costs a peak-rate export.
        if window_is_empty(start1, end1) and window_is_empty(start2, end2):
            enabled = False
        return {
            "sysSn": sn,
            # With no export window, discharge time control is OFF: that is demand mode,
            # where the battery covers the house normally down to batUseCap.
            "ctrDis": 1 if enabled else 0,
            "timeDisf1": start1,
            "timeDise1": end1,
            "timeDisf2": start2,
            "timeDise2": end2,
            "batUseCap": self._clamp_percent(window.get("soc", reserve)) if enabled else reserve,
        }

    @staticmethod
    def payloads_equal(first, second):
        """Return True when two payloads are byte-identical, ignoring key order."""
        if not isinstance(first, dict) or not isinstance(second, dict):
            return False
        return json.dumps(first, sort_keys=True, default=str) == json.dumps(second, sort_keys=True, default=str)
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t9.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t9.log
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_control.py
git commit -m "feat(alphaess): map Predbat controls onto the AlphaESS schedule fields"
```

---

## Task 10: Write path — minimisation, read-only gating, config reads

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_control.py`

**Interfaces:**
- Consumes: payload builders from Task 9.
- Produces:
  - `async fetch_config(self, sn) -> bool` — fills `self.device_config[sn]`
  - `async refresh_config(self) -> bool`
  - `def _is_read_only(self) -> bool`
  - `def _write_allowed(self, sn, direction) -> bool`
  - `async apply_settings(self, sn, schedule, force=False) -> bool`
  - `async apply_schedule(self, sn, force=False) -> bool`
  - `async _reconcile_control(self, sn) -> None`
  - `async _handle_control_event(self, entity_id, value)` (replaces the Task 8 placeholder)

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_control.py`:

```python
from unittest.mock import patch
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session
from tests.test_alphaess_api import _envelope


def _writable(sn="AL70"):
    """A client whose serial Predbat has already been asked to drive."""
    client = _client(sn)
    client.control_active.add(sn)
    return client


def test_alphaess_identical_payload_is_not_rewritten():
    """The write button is pressed EVERY cycle as Predbat's normal 'apply' action, not only
    when the plan changed.

    DEYE hit this first: PR #4371 (commit 3e1de759) measured 40 button presses producing 36
    byte-identical control orders over two hours on a live site once the button forced the
    write. The applied-payload cache is the single source of truth for whether a write is
    needed.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)) as session:
        run_async_local(client.apply_settings("AL70", schedule))
        first_calls = session.return_value.post.call_count if hasattr(session.return_value, "post") else None
        run_async_local(client.apply_settings("AL70", schedule))
    # Second identical apply must send nothing.
    if client.applied_payload.get("AL70", {}).get("charge") is None:
        print("ERROR: applied payload not cached")
        failed = True
    if not any("unchanged" in message.lower() or "no change" in message.lower() for message in client.log_messages):
        print(f"ERROR: no 'unchanged' log on the second apply, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_identical_payload_is_not_rewritten"


def test_alphaess_charge_and_discharge_gated_independently():
    """A charge-only change must not consume a discharge write.

    Both endpoints are documented as writable once per 24 hours, so a shared gate would
    burn half the budget for nothing.
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    base = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client.apply_settings("AL70", base))
        discharge_before = dict(client.applied_payload["AL70"]["discharge"])
        changed = _schedule(charge={"enable": True, "soc": 80, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
        run_async_local(client.apply_settings("AL70", changed))
    if client.applied_payload["AL70"]["charge"]["batHighCap"] != 80:
        print(f"ERROR: charge payload not updated: {client.applied_payload['AL70']['charge']}")
        failed = True
    if client.applied_payload["AL70"]["discharge"] != discharge_before:
        print("ERROR: discharge payload rewritten by a charge-only change")
        failed = True
    assert not failed, "test_alphaess_charge_and_discharge_gated_independently"


def test_alphaess_reconcile_is_gated_on_predbat_read_only():
    """execute.py:145 covers every write that ORIGINATES FROM A PLAN, but not one the
    component initiates itself - and _reconcile_control is exactly that.

    The payload is time-dependent because batUseCap switches between the export target and
    the reserve, so a window transition changes it with no plan change at all. Without this
    gate that transition would write during read-only. GH#4436, fixed for DEYE and Sunsynk
    after the fact (deye.py:1661, sunsynk.py:1346).
    """
    failed = False
    client = _writable()
    client.min_write_interval = 0
    client.state["switch.predbat_set_read_only"] = "on"
    client.local_schedule["AL70"] = _schedule(export={"enable": True, "soc": 20, "power": 3000, "start": "17:00:00", "end": "19:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client._reconcile_control("AL70"))
    if client.applied_payload.get("AL70"):
        print(f"ERROR: wrote while read-only: {client.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_reconcile_is_gated_on_predbat_read_only"


def test_alphaess_reconcile_is_gated_on_control_enable():
    """control_enable false means monitoring only."""
    failed = False
    client = _writable()
    client.control_enable = False
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client._reconcile_control("AL70"))
    if client.applied_payload.get("AL70"):
        print(f"ERROR: wrote with control disabled: {client.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_reconcile_is_gated_on_control_enable"


def test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive():
    """A startup cycle must never clobber an inverter before there is a plan to apply."""
    failed = False
    client = _client()  # NOT in control_active
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client._reconcile_control("AL70"))
    if client.applied_payload.get("AL70"):
        print(f"ERROR: wrote for an undriven serial: {client.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive"


def test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it():
    """The 24h documented write limit is treated as a real budget, but a held change must
    be applied on the next eligible tick - not lost."""
    failed = False
    client = _writable()
    client.min_write_interval = 300
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    first = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    second = _schedule(charge={"enable": True, "soc": 70, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        with patch("alphaess.time.time", return_value=1000.0):
            run_async_local(client.apply_settings("AL70", first))
        with patch("alphaess.time.time", return_value=1010.0):
            run_async_local(client.apply_settings("AL70", second))
        held = client.applied_payload["AL70"]["charge"]["batHighCap"]
        if held != 90:
            print(f"ERROR: write not held inside the interval, batHighCap {held}")
            failed = True
        # Past the interval, the held change goes out.
        with patch("alphaess.time.time", return_value=1400.0):
            run_async_local(client.apply_settings("AL70", second))
    if client.applied_payload["AL70"]["charge"]["batHighCap"] != 70:
        print(f"ERROR: held change never applied: {client.applied_payload['AL70']['charge']}")
        failed = True
    assert not failed, "test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it"


def test_alphaess_6053_backs_off_rather_than_counting_as_a_failure():
    """Too-fast is a pacing signal, not a broken component."""
    failed = False
    client = _writable()
    client.min_write_interval = 0
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    busy = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="The request was too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(busy)):
        run_async_local(client.apply_settings("AL70", schedule))
    # A rejected write must NOT be cached as applied, or the retry never happens.
    if client.applied_payload.get("AL70", {}).get("charge") is not None:
        print("ERROR: a 6053-rejected write was cached as applied")
        failed = True
    assert not failed, "test_alphaess_6053_backs_off_rather_than_counting_as_a_failure"


def test_alphaess_write_button_is_not_forced():
    """Predbat presses this every cycle as its normal apply action (time_button_press), so
    force=True here would bypass the change-detection gate on every single cycle."""
    failed = False
    client = _client()
    client.min_write_interval = 0
    client.local_schedule["AL70"] = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    captured = {}

    async def fake_apply(sn, force=False):
        """Record how apply_schedule was called."""
        captured["force"] = force
        return True

    client.apply_schedule = fake_apply
    run_async_local(client._handle_control_event("switch.predbat_alphaess_al70_battery_schedule_charge_write", "turn_on"))
    if captured.get("force") is not False:
        print(f"ERROR: the write button forced the write: {captured}")
        failed = True
    # Pressing it marks the serial as driven, on the press itself: a write that failed
    # still means Predbat owns this inverter and the next tick should retry.
    if "AL70" not in client.control_active:
        print("ERROR: the write button did not mark the serial as driven")
        failed = True
    assert not failed, "test_alphaess_write_button_is_not_forced"
```

Add to the `run_alphaess_control_tests` list:

```python
        ("identical_not_rewritten", test_alphaess_identical_payload_is_not_rewritten),
        ("independent_gating", test_alphaess_charge_and_discharge_gated_independently),
        ("read_only_gate", test_alphaess_reconcile_is_gated_on_predbat_read_only),
        ("control_enable_gate", test_alphaess_reconcile_is_gated_on_control_enable),
        ("undriven_serial_skipped", test_alphaess_reconcile_skips_a_serial_predbat_has_not_been_asked_to_drive),
        ("min_write_interval", test_alphaess_minimum_write_interval_holds_a_change_rather_than_dropping_it),
        ("6053_backoff", test_alphaess_6053_backs_off_rather_than_counting_as_a_failure),
        ("write_button_not_forced", test_alphaess_write_button_is_not_forced),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t10.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t10.log | head
```

Expected: EXCEPTION — `apply_settings` does not exist.

- [ ] **Step 3: Implement the config read and write path**

Append to `AlphaESSAPI` in `alphaess.py`:

```python
    async def fetch_config(self, sn):
        """Read the current charge and discharge config, the read-modify-write baseline."""
        ok = False
        code, charge = await self._get("charge_config", params={"sysSn": sn})
        entry = self.device_config.setdefault(sn, {})
        if code == ALPHAESS_CODE_OK and isinstance(charge, dict):
            entry["charge"] = charge
            ok = True
        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        code, discharge = await self._get("discharge_config", params={"sysSn": sn})
        if code == ALPHAESS_CODE_OK and isinstance(discharge, dict):
            entry["discharge"] = discharge
            ok = True
        return ok

    async def refresh_config(self):
        """Refresh the config baseline for every inverter, and re-probe demoted telemetry.

        The live re-probe lives here rather than on the power tier so a demoted serial
        costs two extra calls an hour instead of one a minute, while still self-healing.
        """
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_config(sn):
                    got_any = True
            except Exception as error:
                self.log("Warn: AlphaESS config read failed for {}: {}".format(sn, error))
            if self._live_ok.get(sn) is False:
                try:
                    await self.reprobe_live(sn)
                except Exception as error:
                    self.log("Warn: AlphaESS live re-probe failed for {}: {}".format(sn, error))
            if self.api_delay:
                await asyncio.sleep(self.api_delay)
        if got_any:
            self.mark_refreshed("config")
        return got_any

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the inverter."""
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    def _write_allowed(self, sn, direction, force=False):
        """Return True when a write for one serial and direction may go out now.

        The minimum interval treats the documented 24-hour write limit as a real budget.
        A change arriving inside the window is HELD, not dropped: apply_settings leaves the
        applied-payload cache untouched, so the next eligible tick rebuilds and sends it.
        """
        if force or not self.min_write_interval:
            return True
        last = self.last_write_time.get((sn, direction))
        if last is None:
            return True
        return (time.time() - last) >= self.min_write_interval

    async def _write_payload(self, sn, direction, endpoint_key, payload, force=False):
        """Send one payload if it differs from the last applied one and pacing allows."""
        cache = self.applied_payload.setdefault(sn, {})
        if not force and self.payloads_equal(cache.get(direction), payload):
            self.log("Info: AlphaESS {} {} settings unchanged, nothing sent".format(sn, direction))
            return True
        if not self._write_allowed(sn, direction, force=force):
            self.log("Info: AlphaESS {} {} change is held by alphaess_min_write_interval ({}s) and will be applied on the next eligible cycle".format(sn, direction, self.min_write_interval))
            return True
        code, _ = await self._post(endpoint_key, body=payload)
        if code != ALPHAESS_CODE_OK:
            if code == ALPHAESS_CODE_TOO_FAST:
                # A pacing signal, not a broken component. Deliberately NOT cached as
                # applied, so the next cycle retries.
                self.log("Info: AlphaESS rate-limited the {} write for {}; it will be retried".format(direction, sn))
            else:
                self.log("Warn: AlphaESS {} write for {} was rejected with {}".format(direction, sn, self.describe_code(code)))
            return False
        cache[direction] = payload
        self.last_write_time[(sn, direction)] = time.time()
        self.settle_count[(sn, direction)] = 0
        self.log("Info: AlphaESS wrote {} settings for {}".format(direction, sn))
        return True

    async def apply_settings(self, sn, schedule, force=False):
        """Build and send both payloads for one inverter, gated independently.

        Charge and discharge are gated separately so a charge-only change does not consume
        a discharge write - both endpoints are documented as writable once per 24 hours.
        """
        if not self.control_enable:
            return False
        charge_payload = self.build_charge_payload(sn, schedule)
        discharge_payload = self.build_discharge_payload(sn, schedule)
        charge_ok = await self._write_payload(sn, "charge", "update_charge_config", charge_payload, force=force)
        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        discharge_ok = await self._write_payload(sn, "discharge", "update_discharge_config", discharge_payload, force=force)
        return charge_ok and discharge_ok

    async def apply_schedule(self, sn, force=False):
        """Apply the locally held schedule for one inverter."""
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        return await self.apply_settings(sn, schedule, force=force)

    async def _reconcile_control(self, sn):
        """Re-apply sn's schedule if Predbat already controls it, unforced.

        Gated on read-only for a specific reason. Predbat's own read-only handling
        (execute.py:145) covers every write that originates from a plan, but NOT one this
        component initiates itself - and this is exactly that. The payload is time-aware
        because batUseCap switches between the export target and the reserve, so a window
        transition changes it with no plan change at all, and without this gate that
        transition would write to the inverter while Predbat was in read-only mode. This is
        GH#4436 (deye.py:1661, sunsynk.py:1346).
        """
        if sn not in self.control_active or self._is_read_only() or not self.control_enable:
            return
        try:
            await self.apply_schedule(sn)
        except Exception as error:
            self.log("Warn: AlphaESS schedule apply failed for {}: {}".format(sn, error))

    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right inverter and apply it."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log("Warn: AlphaESS could not resolve an inverter for {}".format(entity_id))
            return
        if str(entity_id).endswith("_unbind"):
            await self._handle_unbind_event(sn, value)
            return
        # The write button is NOT forced. Predbat presses this on every cycle as its normal
        # "apply the schedule" action (INVERTER_DEF time_button_press), not only when the
        # plan actually changed, so force=True here would bypass the applied-payload
        # change-detection gate on every single cycle. DEYE hit this exact bug first:
        # PR #4371 (commit 3e1de759) measured 40 button presses producing 36 byte-identical
        # control orders over two hours on a live site once the button forced the write.
        # Do not reintroduce force=True here.
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                # Predbat is now actively driving this inverter, so the reconcile loop may
                # re-apply from here on. Marked on the press itself rather than on a
                # successful write: a write that failed still means Predbat owns this
                # inverter and the next tick should retry.
                self.control_active.add(sn)
                await self.apply_schedule(sn)
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)
```

`_handle_unbind_event` arrives in Task 12. Add a temporary stub now:

```python
    async def _handle_unbind_event(self, sn, value):
        """Handle the unbind toggle. Replaced with the real implementation in Task 12."""
        return
```

Remove the Task 8 placeholder `_handle_control_event` when adding this one — there must be exactly one definition.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t10.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t10.log
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_control.py
git commit -m "feat(alphaess): write path with change detection, pacing and read-only gating"
```

---

## Task 10b: External interference detection

The spec's "External interference" and "Cloud latency" requirements. Without this,
`settle_count` is initialised and never used, and a user who changes settings in the
AlphaESS phone app gets no indication that Predbat's plan was overwritten.

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_control.py`

**Interfaces:**
- Consumes: `device_config` (Task 10), `applied_payload` (Task 10), `ALPHAESS_SETTLE_POLLS` (Task 1).
- Produces:
  - `def _owned_fields(direction) -> tuple[str, ...]` (staticmethod)
  - `def note_external_change(self, sn, direction, observed) -> None`
- `fetch_config` calls `note_external_change` after each successful read.

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_control.py`:

```python
from alphaess_const import ALPHAESS_SETTLE_POLLS


def test_alphaess_settle_window_suppresses_a_stale_read_back():
    """Settings reach the inverter on its NEXT cloud poll, typically one to five minutes
    after Predbat writes them.

    So a read-back immediately after a write shows the old values. That is not
    interference and must not be reported as such, or every single write would produce a
    false alarm.
    """
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    client.settle_count[("AL70", "charge")] = 0
    stale = {"gridCharge": 0, "timeChaf1": "00:00", "timeChae1": "00:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 100}
    client.note_external_change("AL70", "charge", stale)
    if any("overwritten" in message.lower() or "interference" in message.lower() for message in client.log_messages):
        print(f"ERROR: a stale read-back inside the settle window was reported as interference: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_settle_window_suppresses_a_stale_read_back"


def test_alphaess_external_change_reported_after_the_settle_window():
    """Past the settle window, a Predbat-owned field that does not match what Predbat
    wrote means something else changed it - the phone app, or another Predbat instance.

    The write endpoints are whole-object replacements, so the last writer wins.
    """
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    changed = {"gridCharge": 1, "timeChaf1": "02:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "charge", changed)
    if not any("timeChaf1" in message for message in client.log_messages):
        print(f"ERROR: the changed field was not named: {client.log_messages}")
        failed = True
    if not any("AlphaESS app" in message or "another" in message.lower() for message in client.log_messages):
        print(f"ERROR: no interference explanation: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_external_change_reported_after_the_settle_window"


def test_alphaess_only_predbat_owned_fields_count_as_interference():
    """A field Predbat never writes changing is not interference with Predbat's plan."""
    failed = False
    client = _writable()
    client.applied_payload["AL70"] = {"charge": {"sysSn": "AL70", "gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90}}
    observed = {"gridCharge": 1, "timeChaf1": "01:00", "timeChae1": "05:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 90, "someOtherField": "changed"}
    for _ in range(ALPHAESS_SETTLE_POLLS + 1):
        client.note_external_change("AL70", "charge", observed)
    if any("overwritten" in message.lower() for message in client.log_messages):
        print(f"ERROR: an unowned field was reported as interference: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_only_predbat_owned_fields_count_as_interference"


def test_alphaess_no_interference_check_before_predbat_has_written():
    """With nothing in applied_payload there is no Predbat intent to compare against, so
    whatever the inverter reports is simply the user's own configuration."""
    failed = False
    client = _writable()
    observed = {"gridCharge": 1, "timeChaf1": "03:00", "timeChae1": "04:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 55}
    for _ in range(ALPHAESS_SETTLE_POLLS + 2):
        client.note_external_change("AL70", "charge", observed)
    if any("overwritten" in message.lower() for message in client.log_messages):
        print(f"ERROR: reported interference with no prior Predbat write: {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_no_interference_check_before_predbat_has_written"
```

Add to the `run_alphaess_control_tests` list:

```python
        ("settle_suppresses_stale", test_alphaess_settle_window_suppresses_a_stale_read_back),
        ("external_change_reported", test_alphaess_external_change_reported_after_the_settle_window),
        ("only_owned_fields", test_alphaess_only_predbat_owned_fields_count_as_interference),
        ("no_check_before_writing", test_alphaess_no_interference_check_before_predbat_has_written),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t10b.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t10b.log | head
```

Expected: EXCEPTION — `note_external_change` does not exist.

- [ ] **Step 3: Implement interference detection**

Add to the `alphaess_const` imports:

```python
from alphaess_const import ALPHAESS_SETTLE_POLLS
```

Append to `AlphaESSAPI`:

```python
    @staticmethod
    def _owned_fields(direction):
        """Return the payload fields Predbat writes for one direction.

        Only these count as interference: a field Predbat never writes changing is the
        user configuring their own inverter, not something overwriting Predbat's plan.
        """
        if direction == "charge":
            return ("gridCharge", "timeChaf1", "timeChae1", "timeChaf2", "timeChae2", "batHighCap")
        return ("ctrDis", "timeDisf1", "timeDise1", "timeDisf2", "timeDise2", "batUseCap")

    def note_external_change(self, sn, direction, observed):
        """Report when a Predbat-owned field no longer matches what Predbat last wrote.

        Suppressed for the first ALPHAESS_SETTLE_POLLS reads after a write, because
        settings only reach the inverter on its next cloud poll - typically one to five
        minutes later - so an immediate read-back legitimately shows the old values.
        Without that window every single write would raise a false alarm.

        Does nothing before Predbat has written anything for this serial and direction:
        with no recorded intent there is nothing to have been overwritten, and what the
        inverter reports is simply the user's own configuration.
        """
        applied = self.applied_payload.get(sn, {}).get(direction)
        if not applied or not isinstance(observed, dict):
            return
        key = (sn, direction)
        polls = self.settle_count.get(key, 0) + 1
        self.settle_count[key] = polls
        if polls <= ALPHAESS_SETTLE_POLLS:
            return
        differing = [field for field in self._owned_fields(direction) if field in observed and str(observed.get(field)) != str(applied.get(field))]
        if not differing:
            return
        detail = ", ".join("{}={} (Predbat wrote {})".format(field, observed.get(field), applied.get(field)) for field in differing)
        self.log("Warn: AlphaESS {} {} settings no longer match what Predbat wrote: {}. The AlphaESS app, another Predbat instance, or the installer portal may have changed them - these endpoints are whole-object replacements, so the last writer wins.".format(sn, direction, detail))
        # Clear the recorded intent so the next cycle re-applies rather than deciding the
        # payload is unchanged and leaving the inverter on someone else's settings.
        self.applied_payload.get(sn, {}).pop(direction, None)
```

Now wire it into `fetch_config`. Replace the two assignment blocks so each successful read
is compared first:

```python
        if code == ALPHAESS_CODE_OK and isinstance(charge, dict):
            self.note_external_change(sn, "charge", charge)
            entry["charge"] = charge
            ok = True
```

```python
        if code == ALPHAESS_CODE_OK and isinstance(discharge, dict):
            self.note_external_change(sn, "discharge", discharge)
            entry["discharge"] = discharge
            ok = True
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t10b.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t10b.log
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_control.py
git commit -m "feat(alphaess): detect external settings changes outside the settle window"
```

---

## Task 11: Periodic API probe and write path

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_control.py`

**Interfaces:**
- Consumes: `_snapped_periods`, `_clamp_percent` from Task 9; `_write_payload` from Task 10.
- Produces:
  - `async probe_periodic(self, sn) -> bool | None`
  - `def build_periodic_payload(self, sn, schedule) -> dict`
  - `apply_settings` gains a periodic branch.

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_control.py`:

```python
def test_alphaess_periodic_6017_is_cached_and_never_retried():
    """The API docs are explicit that 6017 is an ENTITLEMENT verdict, not a transient
    error. Retrying it every config tier would burn calls forever on a system that will
    never answer differently."""
    failed = False
    client = _client()
    refused = create_aiohttp_mock_response(status=200, json_data=_envelope(6017, None, msg="No operation permissions"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(refused)):
        verdict = run_async_local(client.probe_periodic("AL70"))
    if verdict is not False:
        print(f"ERROR: 6017 verdict {verdict} should be False")
        failed = True
    if client._periodic_ok.get("AL70") is not False:
        print(f"ERROR: verdict not cached: {client._periodic_ok}")
        failed = True
    # A second probe must not call the API at all.
    with patch("alphaess.aiohttp.ClientSession", side_effect=AssertionError("probe_periodic must not re-call after 6017")):
        again = run_async_local(client.probe_periodic("AL70"))
    if again is not False:
        print(f"ERROR: cached verdict not reused, got {again}")
        failed = True
    assert not failed, "test_alphaess_periodic_6017_is_cached_and_never_retried"


def test_alphaess_periodic_other_failures_leave_the_verdict_unknown():
    """Only 6017 is an entitlement verdict; a transient failure must be re-probed."""
    failed = False
    client = _client()
    busy = create_aiohttp_mock_response(status=200, json_data=_envelope(6053, None, msg="too fast"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(busy)):
        verdict = run_async_local(client.probe_periodic("AL70"))
    if verdict is not None:
        print(f"ERROR: transient failure verdict {verdict} should be None")
        failed = True
    if "AL70" in client._periodic_ok:
        print(f"ERROR: transient failure cached a verdict: {client._periodic_ok}")
        failed = True
    assert not failed, "test_alphaess_periodic_other_failures_leave_the_verdict_unknown"


def test_alphaess_periodic_payload_shape():
    """Six windows and a per-window chargePower, with the constraints the API enforces."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 4000, "start": "17:00:00", "end": "19:00:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    if payload.get("executeCycleType") != 0:
        print(f"ERROR: executeCycleType {payload.get('executeCycleType')} should be 0 (daily)")
        failed = True
    charge_list = payload.get("chargeTimeList") or []
    discharge_list = payload.get("dischargeTimeList") or []
    # BOTH lists need at least one element: [] is rejected with 6001 "time list is null",
    # and omitting the key gets 10001.
    if not charge_list or not discharge_list:
        print(f"ERROR: empty list in payload: {payload}")
        failed = True
    first = charge_list[0]
    if first.get("beginTime") != "01:00" or first.get("endTime") != "05:00":
        print(f"ERROR: charge period {first}")
        failed = True
    # chargeLimit range is [10,100] - 6001 otherwise.
    if not 10 <= first.get("chargeLimit", 0) <= 100:
        print(f"ERROR: chargeLimit {first.get('chargeLimit')} out of the [10,100] range")
        failed = True
    if first.get("chargePower") != 3000:
        print(f"ERROR: chargePower {first.get('chargePower')} should carry Predbat's rate")
        failed = True
    if discharge_list[0].get("chargePower") != 4000:
        print(f"ERROR: discharge chargePower {discharge_list[0].get('chargePower')}")
        failed = True
    if payload.get("gridChargeCycle") != 1 or payload.get("ctrDisCycle") != 1:
        print(f"ERROR: cycle flags {payload.get('gridChargeCycle')}/{payload.get('ctrDisCycle')}")
        failed = True
    assert not failed, "test_alphaess_periodic_payload_shape"


def test_alphaess_periodic_disabled_direction_uses_the_cycle_flag():
    """Both lists must be non-empty, so a disabled direction is expressed by its FLAG and
    a filler period rather than by an empty list the API rejects."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    payload = client.build_periodic_payload("AL70", schedule)
    if not payload.get("dischargeTimeList"):
        print("ERROR: dischargeTimeList must not be empty - [] gets 6001 'time list is null'")
        failed = True
    if payload.get("ctrDisCycle") != 0:
        print(f"ERROR: ctrDisCycle {payload.get('ctrDisCycle')} should be 0 when no export is planned")
        failed = True
    if payload.get("gridChargeCycle") != 1:
        print(f"ERROR: gridChargeCycle {payload.get('gridChargeCycle')}")
        failed = True
    assert not failed, "test_alphaess_periodic_disabled_direction_uses_the_cycle_flag"


def test_alphaess_periodic_windows_do_not_overlap():
    """Charge and discharge periods must not overlap - 6008 otherwise."""
    failed = False
    client = _client()
    client._periodic_ok["AL70"] = True
    schedule = _schedule(
        charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "06:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "05:00:00", "end": "08:00:00"},
    )
    payload = client.build_periodic_payload("AL70", schedule)
    charge_end = payload["chargeTimeList"][0]["endTime"]
    discharge_start = payload["dischargeTimeList"][0]["beginTime"]
    if charge_end > discharge_start:
        print(f"ERROR: overlapping periods {charge_end} > {discharge_start}")
        failed = True
    if not any("overlap" in message.lower() for message in client.log_messages):
        print(f"ERROR: no overlap-trim log, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_periodic_windows_do_not_overlap"
```

Add to the runner list:

```python
        ("periodic_6017_cached", test_alphaess_periodic_6017_is_cached_and_never_retried),
        ("periodic_transient_unknown", test_alphaess_periodic_other_failures_leave_the_verdict_unknown),
        ("periodic_payload", test_alphaess_periodic_payload_shape),
        ("periodic_disabled_flag", test_alphaess_periodic_disabled_direction_uses_the_cycle_flag),
        ("periodic_no_overlap", test_alphaess_periodic_windows_do_not_overlap),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t11.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t11.log | head
```

Expected: EXCEPTION — `probe_periodic` does not exist.

- [ ] **Step 3: Implement the periodic path**

Add to the `alphaess_const` imports:

```python
from alphaess_const import ALPHAESS_CODE_NO_PERMISSION
```

Append to `AlphaESSAPI`:

```python
    async def probe_periodic(self, sn):
        """Return True/False/None for whether this system may use the periodic API.

        6017 is an ENTITLEMENT verdict, not a transient error - the API docs are explicit
        that the endpoint is live and the SN binding check passes, and what fails is a
        check on the account tier or hardware. So it is cached and never retried. Any other
        failure leaves the verdict unknown so the next config tier re-probes.
        """
        if sn in self._periodic_ok:
            return self._periodic_ok[sn]
        code, _ = await self._get("time_charge", params={"sysSn": sn})
        if code == ALPHAESS_CODE_OK:
            self._periodic_ok[sn] = True
            self.log("Info: AlphaESS {} is entitled to the periodic schedule API; up to six windows and a power setpoint are available".format(sn))
            return True
        if code == ALPHAESS_CODE_NO_PERMISSION:
            self._periodic_ok[sn] = False
            self.log("Info: AlphaESS {} is not entitled to the periodic schedule API ({}), using the two-window legacy endpoints. This is a permanent verdict and is not retried.".format(sn, self.describe_code(code)))
            return False
        return None

    def _periodic_entry(self, start, end, soc, power):
        """Build one chargeTimeList/dischargeTimeList element.

        chargeLimit is documented as [10,100] and anything outside gets 6001, so it is
        clamped to 10 at the bottom rather than passed through as Predbat's 0.
        """
        entry = {"beginTime": start, "endTime": end, "chargeLimit": self._clamp_percent(soc, low=10, high=100)}
        if power > 0:
            entry["chargePower"] = int(power)
        return entry

    def build_periodic_payload(self, sn, schedule):
        """Build the setTimeChargeBySn body for one inverter.

        executeCycleType 0 (daily) only: Predbat replans continuously, so a weekday-aware
        schedule has nothing to express.

        Both lists must carry at least one element - an empty list is rejected with 6001
        "time list is null", and omitting the key gets 10001 - so a direction with no plan
        gets a filler period and is disabled via its cycle flag instead.
        """
        charge = schedule.get("charge", {}) or {}
        export = schedule.get("export", {}) or {}
        charge_rate = self._as_float(charge.get("power"), 0.0)
        export_rate = self._as_float(export.get("power"), 0.0)
        charge_on = bool(charge.get("enable")) and charge_rate > 0
        export_on = bool(export.get("enable")) and export_rate > 0

        (charge_start, charge_end), _ = self._snapped_periods(sn, "charge", charge.get("start"), charge.get("end"), charge_on)
        (export_start, export_end), _ = self._snapped_periods(sn, "export", export.get("start"), export.get("end"), export_on)
        if window_is_empty(charge_start, charge_end):
            charge_on = False
        if window_is_empty(export_start, export_end):
            export_on = False

        # Charge and discharge periods must not overlap or the write is rejected with 6008.
        # Trim the export start rather than dropping either window - Predbat's charge
        # window is the one with a hard SOC target to hit.
        if charge_on and export_on and hm_to_minutes(export_start) < hm_to_minutes(charge_end):
            self.log("Info: AlphaESS {} charge window ends {} and export starts {}, which the periodic API rejects as overlapping; trimming the export start to {}".format(sn, charge_end, export_start, charge_end))
            export_start = charge_end
            if window_is_empty(export_start, export_end):
                export_on = False

        charge_list = [self._periodic_entry(charge_start, charge_end, charge.get("soc", 100), charge_rate)] if charge_on else [self._periodic_entry(ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED, 10, 0)]
        discharge_list = [self._periodic_entry(export_start, export_end, export.get("soc", schedule.get("reserve", 10)), export_rate)] if export_on else [self._periodic_entry(ALPHAESS_TIME_DISABLED, ALPHAESS_TIME_DISABLED, 10, 0)]
        return {
            "sysSn": sn,
            "executeCycleType": 0,
            "chargeTimeList": charge_list,
            "dischargeTimeList": discharge_list,
            "gridChargeCycle": 1 if charge_on else 0,
            "ctrDisCycle": 1 if export_on else 0,
        }
```

Now extend `apply_settings` to take the periodic branch. Replace its body with:

```python
    async def apply_settings(self, sn, schedule, force=False):
        """Build and send the settings for one inverter, gated independently per direction.

        Entitled systems use the periodic API, which carries up to six windows and a real
        power setpoint in one call. Everyone else uses the legacy pair, where charge and
        discharge are gated separately so a charge-only change does not consume a discharge
        write - both endpoints are documented as writable once per 24 hours.
        """
        if not self.control_enable:
            return False
        if self._periodic_ok.get(sn) is True:
            payload = self.build_periodic_payload(sn, schedule)
            return await self._write_payload(sn, "periodic", "set_time_charge", payload, force=force)
        charge_payload = self.build_charge_payload(sn, schedule)
        discharge_payload = self.build_discharge_payload(sn, schedule)
        charge_ok = await self._write_payload(sn, "charge", "update_charge_config", charge_payload, force=force)
        if self.api_delay:
            await asyncio.sleep(self.api_delay)
        discharge_ok = await self._write_payload(sn, "discharge", "update_discharge_config", discharge_payload, force=force)
        return charge_ok and discharge_ok
```

Also add the probe to `refresh_config`, immediately after the `fetch_config` call:

```python
            if sn not in self._periodic_ok:
                try:
                    await self.probe_periodic(sn)
                except Exception as error:
                    self.log("Warn: AlphaESS periodic probe failed for {}: {}".format(sn, error))
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_control > /tmp/t11.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t11.log
```

Expected: PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_control.py
git commit -m "feat(alphaess): probe the periodic schedule API and fall back to the legacy pair"
```

---

## Task 12: Storage and cache persistence

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Create: `apps/predbat/tests/test_alphaess_storage.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: everything cached so far.
- Produces:
  - `async load_cache(self, name) -> dict`, `async save_cache(self, name, data) -> None`
  - `async save_static/save_config/save_ratings/save_control(self) -> None`
  - `async restore_state(self) -> None`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_alphaess_storage.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS cache persistence
# -----------------------------------------------------------------------------

"""Tests for AlphaESS storage-backed cache persistence across restarts."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from tests.test_alphaess_api import MockAlphaESS
from tests.test_infra import run_async as run_async_local


class FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self, fail=False):
        """Set up an empty store, optionally one that raises on every call."""
        self.data = {}
        self.fail = fail

    async def load(self, module, name):
        """Return a previously saved entry, or {} when absent."""
        if self.fail:
            raise IOError("storage unavailable")
        return self.data.get((module, name), {})

    async def save(self, module, name, payload):
        """Record an entry."""
        if self.fail:
            raise IOError("storage unavailable")
        self.data[(module, name)] = payload


class StoredAlphaESS(MockAlphaESS):
    """MockAlphaESS with a working Storage component attached."""

    def __init__(self, store=None, **kwargs):
        """Attach a FakeStorage so the cache paths are actually exercised."""
        super().__init__(**kwargs)
        self._store = store if store is not None else FakeStorage()

    @property
    def storage(self):
        """Return the fake store."""
        return self._store


def test_alphaess_cache_round_trip():
    """Every verdict the component learns survives a restart."""
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    client.device_config = {"AL70": {"charge": {"gridCharge": 1}}}
    client._periodic_ok = {"AL70": False}
    client._live_ok = {"AL70": False}
    client.control_active = {"AL70"}
    client._unbind_done = {"AL99"}
    client.applied_payload = {"AL70": {"charge": {"sysSn": "AL70", "gridCharge": 1}}}
    run_async_local(client.save_static())
    run_async_local(client.save_config())
    run_async_local(client.save_ratings())
    run_async_local(client.save_control())

    restored = StoredAlphaESS(store=store)
    run_async_local(restored.restore_state())
    if restored.device_list != ["AL70"]:
        print(f"ERROR: device_list {restored.device_list}")
        failed = True
    if restored._periodic_ok.get("AL70") is not False:
        print(f"ERROR: periodic verdict not restored: {restored._periodic_ok}")
        failed = True
    if restored._live_ok.get("AL70") is not False:
        print(f"ERROR: live verdict not restored: {restored._live_ok}")
        failed = True
    if "AL70" not in restored.control_active:
        print(f"ERROR: control_active not restored: {restored.control_active}")
        failed = True
    if "AL99" not in restored._unbind_done:
        print(f"ERROR: unbind latch not restored: {restored._unbind_done}")
        failed = True
    if restored.applied_payload.get("AL70", {}).get("charge", {}).get("gridCharge") != 1:
        print(f"ERROR: applied payload not restored: {restored.applied_payload}")
        failed = True
    assert not failed, "test_alphaess_cache_round_trip"


def test_alphaess_no_storage_component_is_silent():
    """self.storage being None is the normal state for a standalone CLI run.

    It is a permanent, by-design condition rather than a transient fault, so it must not
    warn and must not flag the restore as incomplete.
    """
    failed = False
    client = MockAlphaESS()  # storage property returns None
    run_async_local(client.save_control())
    data = run_async_local(client.load_cache("control"))
    if data != {}:
        print(f"ERROR: load_cache returned {data}")
        failed = True
    if any("Warn" in message for message in client.log_messages):
        print(f"ERROR: warned about an absent Storage component: {client.log_messages}")
        failed = True
    if client._restore_had_error:
        print("ERROR: an absent Storage component flagged a restore error")
        failed = True
    assert not failed, "test_alphaess_no_storage_component_is_silent"


def test_alphaess_real_storage_failure_is_flagged_for_retry():
    """A genuine storage fault must warn AND leave the restore marked incomplete, so a
    transient outage is retried rather than silently marked done with nothing restored."""
    failed = False
    client = StoredAlphaESS(store=FakeStorage(fail=True))
    run_async_local(client.restore_state())
    if not any("Warn" in message for message in client.log_messages):
        print(f"ERROR: no warning on a real storage failure: {client.log_messages}")
        failed = True
    if client._cache_restored:
        print("ERROR: a failed restore was marked complete")
        failed = True
    assert not failed, "test_alphaess_real_storage_failure_is_flagged_for_retry"


def test_alphaess_empty_discovery_is_not_persisted():
    """Writing {'device_list': []} and stamping the tier fresh would make a restart restore
    nothing and skip re-discovery for a full 8-hour TTL."""
    failed = False
    store = FakeStorage()
    client = StoredAlphaESS(store=store)
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    run_async_local(client.save_static())
    saved = store.data.get(("alphaess", "static"), {})
    if saved.get("device_list") != ["AL70"]:
        print(f"ERROR: static cache {saved}")
        failed = True
    # An empty in-memory list must not overwrite a good cache.
    client.device_list = []
    run_async_local(client.save_static())
    saved = store.data.get(("alphaess", "static"), {})
    if saved.get("device_list") != ["AL70"]:
        print(f"ERROR: empty discovery overwrote the cache: {saved}")
        failed = True
    assert not failed, "test_alphaess_empty_discovery_is_not_persisted"


def run_alphaess_storage_tests(my_predbat):
    """Run all AlphaESS storage tests."""
    failed = False
    for name, fn in [
        ("cache_round_trip", test_alphaess_cache_round_trip),
        ("no_storage_silent", test_alphaess_no_storage_component_is_silent),
        ("real_failure_flagged", test_alphaess_real_storage_failure_is_flagged_for_retry),
        ("empty_discovery_not_persisted", test_alphaess_empty_discovery_is_not_persisted),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_storage.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_storage.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_storage > /tmp/t12.log 2>&1; tail -20 /tmp/t12.log
```

Expected: FAIL.

- [ ] **Step 3: Implement the cache layer**

Add to the `alphaess_const` imports:

```python
from alphaess_const import (
    ALPHAESS_STORAGE_MODULE,
    ALPHAESS_CACHE_STATIC,
    ALPHAESS_CACHE_CONFIG,
    ALPHAESS_CACHE_RATINGS,
    ALPHAESS_CACHE_CONTROL,
)
```

Append to `AlphaESSAPI`:

```python
    async def load_cache(self, name):
        """Load one cache file, returning {} when absent or unreadable.

        self.storage being None is checked FIRST and returns silently, with no warning and
        no _restore_had_error: it means there is simply no Storage component configured
        (the normal state for a standalone CLI run), which is a permanent, by-design
        condition rather than a transient fault worth retrying or warning about. Only a
        REAL failure below flags the restore as incomplete.
        """
        if self.storage is None:
            return {}
        try:
            data = await self.storage.load(ALPHAESS_STORAGE_MODULE, name)
        except Exception as error:
            self.log("Warn: AlphaESS could not load cache {}: {}".format(name, error))
            self._restore_had_error = True
            return {}
        return data if isinstance(data, dict) else {}

    async def save_cache(self, name, data):
        """Save one cache file, tolerating a storage failure.

        Silently does nothing when self.storage is None - there is nothing to warn about.
        """
        if self.storage is None:
            return
        try:
            await self.storage.save(ALPHAESS_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log("Warn: AlphaESS could not save cache {}: {}".format(name, error))

    async def save_static(self):
        """Persist discovery. Refuses to overwrite a good cache with an empty result.

        Writing {'device_list': []} and stamping the tier fresh would make a restart
        restore nothing and skip re-discovery for a full TTL. Absence of a result is not
        a result.
        """
        if not self.device_list:
            return
        await self.save_cache(ALPHAESS_CACHE_STATIC, {"device_list": self.device_list, "device_detail": self.device_detail})

    async def save_config(self):
        """Persist the config baseline and the periodic entitlement verdicts."""
        await self.save_cache(ALPHAESS_CACHE_CONFIG, {"device_config": self.device_config, "periodic_ok": self._periodic_ok})

    async def save_ratings(self):
        """Persist the live-telemetry capability verdicts."""
        await self.save_cache(ALPHAESS_CACHE_RATINGS, {"live_ok": self._live_ok})

    async def save_control(self):
        """Persist the control state that must survive a restart."""
        await self.save_cache(
            ALPHAESS_CACHE_CONTROL,
            {
                "local_schedule": self.local_schedule,
                "applied_payload": self.applied_payload,
                "control_active": sorted(self.control_active),
                "unbind_done": sorted(self._unbind_done),
            },
        )

    async def restore_state(self):
        """Restore every cached verdict so a restart does not re-learn them from scratch.

        _cache_restored is set only when nothing failed, so a transient storage outage is
        retried on a later cycle rather than silently marked done with nothing restored.
        """
        self._restore_had_error = False
        static = await self.load_cache(ALPHAESS_CACHE_STATIC)
        if static.get("device_list"):
            self.device_list = list(static["device_list"])
            self.device_detail = dict(static.get("device_detail") or {})
        config = await self.load_cache(ALPHAESS_CACHE_CONFIG)
        self.device_config = dict(config.get("device_config") or {})
        self._periodic_ok = dict(config.get("periodic_ok") or {})
        ratings = await self.load_cache(ALPHAESS_CACHE_RATINGS)
        self._live_ok = dict(ratings.get("live_ok") or {})
        control = await self.load_cache(ALPHAESS_CACHE_CONTROL)
        self.local_schedule = dict(control.get("local_schedule") or {})
        self.applied_payload = dict(control.get("applied_payload") or {})
        self.control_active = set(control.get("control_active") or [])
        self._unbind_done = set(control.get("unbind_done") or [])
        self._cache_restored = not self._restore_had_error
```

- [ ] **Step 4: Register the test module**

In `apps/predbat/unit_test.py`:

```python
from tests.test_alphaess_storage import run_alphaess_storage_tests
```

```python
        ("alphaess_storage", run_alphaess_storage_tests, "AlphaESS storage tests", False),
```

- [ ] **Step 5: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t12.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t12.log
```

Expected: all six AlphaESS suites PASSED.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_storage.py apps/predbat/unit_test.py
git commit -m "feat(alphaess): persist discovery, verdicts and control state through Storage"
```

---

## Task 13: Bind, unbind and the unbind switch

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_api.py` (return-code mapping)
- Modify: `apps/predbat/tests/test_alphaess_control.py` (switch behaviour)

**Interfaces:**
- Consumes: `_get`, `_post`, `_to_bool`, `save_control` from earlier tasks.
- Produces:
  - `async request_verification_code(self, sn, check_code) -> tuple[bool, str]`
  - `async bind_system(self, sn, code) -> tuple[bool, str]`
  - `async unbind_system(self, sn) -> tuple[bool, str]`
  - `async _handle_unbind_event(self, sn, value)` (replaces the Task 10 stub)
  - Unbind switch published by `publish_schedule_settings_ha`

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_api.py`:

```python
def test_alphaess_bind_treats_already_bound_as_success():
    """bindSn answers data:null on success AND on failure, so only the code separates them.

    6003 "You have bound this SN" is an idempotent success, not an error to show a user.
    """
    failed = False
    for code, expect_ok in ((200, True), (6003, True), (6046, False), (6038, False)):
        client = MockAlphaESS()
        response = create_aiohttp_mock_response(status=200, json_data=_envelope(code, None))
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
            ok, message = run_async_local(client.bind_system("AL70", "123456"))
        if ok is not expect_ok:
            print(f"ERROR: bind code {code} -> ok {ok}, expected {expect_ok}")
            failed = True
        if not message:
            print(f"ERROR: bind code {code} produced no message")
            failed = True
    assert not failed, "test_alphaess_bind_treats_already_bound_as_success"


def test_alphaess_unbind_treats_not_bound_as_success():
    """6005 means the AppID was not bound to that SN in the first place - already gone."""
    failed = False
    for code, expect_ok in ((200, True), (6005, True), (6042, False)):
        client = MockAlphaESS()
        response = create_aiohttp_mock_response(status=200, json_data=_envelope(code, None))
        with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
            ok, _ = run_async_local(client.unbind_system("AL70"))
        if ok is not expect_ok:
            print(f"ERROR: unbind code {code} -> ok {ok}, expected {expect_ok}")
            failed = True
    assert not failed, "test_alphaess_unbind_treats_not_bound_as_success"


def test_alphaess_verification_code_is_a_get():
    """The portal describes a JSON body, which reads like a POST. It is GET with query
    parameters - a POST returns HTTP 405."""
    failed = False
    client = MockAlphaESS()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))
    with patch("alphaess.aiohttp.ClientSession", return_value=session):
        ok, _ = run_async_local(client.request_verification_code("AL70", "CHECKCODE"))
    if not ok:
        print("ERROR: verification request failed")
        failed = True
    if session.post.called:
        print("ERROR: getVerificationCode was sent as a POST; it is GET only")
        failed = True
    assert not failed, "test_alphaess_verification_code_is_a_get"


def test_alphaess_bind_code_is_never_logged():
    """The one-time code is a credential-grade secret while it is valid."""
    failed = False
    client = MockAlphaESS()
    response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(response)):
        run_async_local(client.bind_system("AL70", "987654"))
    for message in client.log_messages:
        if "987654" in message:
            print(f"ERROR: bind code leaked in log: {message}")
            failed = True
    assert not failed, "test_alphaess_bind_code_is_never_logged"
```

Add to the `run_alphaess_api_tests` list:

```python
        ("bind_already_bound_ok", test_alphaess_bind_treats_already_bound_as_success),
        ("unbind_not_bound_ok", test_alphaess_unbind_treats_not_bound_as_success),
        ("verification_is_get", test_alphaess_verification_code_is_a_get),
        ("bind_code_redacted", test_alphaess_bind_code_is_never_logged),
```

Append to `apps/predbat/tests/test_alphaess_control.py`:

```python
def test_alphaess_unbind_switch_is_published_for_every_serial():
    """One toggle per discovered serial, default off, following the Sigenergy offboard
    pattern."""
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule()
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    entity = "switch.predbat_alphaess_al70_unbind"
    published = client.published.get(entity)
    if not published:
        print(f"ERROR: {entity} not published")
        failed = True
    elif published.get("state") != "off":
        print(f"ERROR: unbind default {published.get('state')} should be off")
        failed = True
    # The switch is one-way from Home Assistant: undoing it needs a code emailed to the
    # system owner, so the friendly name has to say so.
    name = (published or {}).get("attributes", {}).get("friendly_name", "")
    if "one-way" not in name.lower() and "cannot be undone" not in name.lower():
        print(f"ERROR: unbind friendly_name does not warn it is one-way: {name!r}")
        failed = True
    assert not failed, "test_alphaess_unbind_switch_is_published_for_every_serial"


def test_alphaess_unbind_latches_and_removes_the_serial():
    """A successful unbind must not re-fire on the next tick or after a restart, and the
    serial has to leave device_list - the API will refuse every call for it now."""
    failed = False
    client = _client()
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if "AL70" not in client._unbind_done:
        print(f"ERROR: unbind not latched: {client._unbind_done}")
        failed = True
    if "AL70" in client.device_list:
        print(f"ERROR: unbound serial still in device_list: {client.device_list}")
        failed = True
    if not any("num_inverters" in message or "apps.yaml" in message for message in client.log_messages):
        print(f"ERROR: no warning that auto-config args now point at a dead system: {client.log_messages}")
        failed = True
    # A second turn-on must not call the API again.
    with patch("alphaess.aiohttp.ClientSession", side_effect=AssertionError("unbind must not repeat once latched")):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    assert not failed, "test_alphaess_unbind_latches_and_removes_the_serial"


def test_alphaess_failed_unbind_leaves_the_latch_clear_for_retry():
    """Matches _offboard_system_if_needed: a failure must be retried on the next tick."""
    failed = False
    client = _client()
    bad = create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline"))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(bad)):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if "AL70" in client._unbind_done:
        print("ERROR: a failed unbind was latched")
        failed = True
    if "AL70" not in client.device_list:
        print("ERROR: a failed unbind removed the serial anyway")
        failed = True
    assert not failed, "test_alphaess_failed_unbind_leaves_the_latch_clear_for_retry"


def test_alphaess_unbind_toggle_off_clears_the_latch():
    """Turning it back off lets discovery pick the system up again if it was re-bound via
    the CLI or the AlphaESS portal. It does NOT re-bind - that needs the emailed code."""
    failed = False
    client = _client()
    client._unbind_done.add("AL70")
    run_async_local(client._handle_unbind_event("AL70", "turn_off"))
    if "AL70" in client._unbind_done:
        print(f"ERROR: latch not cleared: {client._unbind_done}")
        failed = True
    assert not failed, "test_alphaess_unbind_toggle_off_clears_the_latch"


def test_alphaess_unbind_is_not_gated_by_read_only():
    """Read-only guards writes to the INVERTER; unbinding is account management.

    Asserted so nobody 'fixes' this later by folding it into the control gate.
    """
    failed = False
    client = _client()
    client.state["switch.predbat_set_read_only"] = "on"
    client.control_enable = False
    ok_response = create_aiohttp_mock_response(status=200, json_data=_envelope(200, None))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(ok_response)):
        run_async_local(client._handle_unbind_event("AL70", "turn_on"))
    if "AL70" not in client._unbind_done:
        print("ERROR: unbind was blocked by read-only or control_enable")
        failed = True
    assert not failed, "test_alphaess_unbind_is_not_gated_by_read_only"
```

Add to the `run_alphaess_control_tests` list:

```python
        ("unbind_switch_published", test_alphaess_unbind_switch_is_published_for_every_serial),
        ("unbind_latches", test_alphaess_unbind_latches_and_removes_the_serial),
        ("unbind_failure_retries", test_alphaess_failed_unbind_leaves_the_latch_clear_for_retry),
        ("unbind_toggle_off", test_alphaess_unbind_toggle_off_clears_the_latch),
        ("unbind_not_read_only_gated", test_alphaess_unbind_is_not_gated_by_read_only),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t13.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t13.log | head
```

Expected: EXCEPTION — `bind_system` does not exist.

- [ ] **Step 3: Implement bind/unbind**

Add to the `alphaess_const` imports:

```python
from alphaess_const import ALPHAESS_CODE_ALREADY_BOUND, ALPHAESS_CODE_NOT_BOUND
```

Append to `AlphaESSAPI`:

```python
    async def request_verification_code(self, sn, check_code):
        """Ask AlphaESS to email a verification code to the system owner.

        GET, despite the portal describing the payload as "request parameter (Json)" - a
        POST returns HTTP 405. The code is emailed to the END USER's registered address and
        is never returned here, which is why binding cannot be driven from a toggle.
        """
        code, _ = await self._get("verification_code", params={"sysSn": sn, "checkCode": check_code})
        if code == ALPHAESS_CODE_OK:
            return True, "AlphaESS has emailed a verification code to the registered owner of {}".format(sn)
        return False, "Could not request a verification code for {}: {}".format(sn, self.describe_code(code))

    async def bind_system(self, sn, code):
        """Bind a system to this AppID using the emailed verification code.

        Judged on the response code alone: the endpoint answers data:null whether it
        succeeded or not. 6003 "You have bound this SN" is an idempotent success.
        The one-time code is never logged - _post redacts it.
        """
        result, _ = await self._post("bind", body={"sysSn": sn, "code": code})
        if result == ALPHAESS_CODE_OK:
            return True, "Bound {} to this AppID".format(sn)
        if result == ALPHAESS_CODE_ALREADY_BOUND:
            return True, "{} was already bound to this AppID".format(sn)
        return False, "Could not bind {}: {}".format(sn, self.describe_code(result))

    async def unbind_system(self, sn):
        """Unbind a system from this AppID.

        6005 "This appId is not bound to the SN" means it was already gone, which is the
        outcome the caller wanted.
        """
        result, _ = await self._post("unbind", body={"sysSn": sn})
        if result == ALPHAESS_CODE_OK:
            return True, "Unbound {} from this AppID".format(sn)
        if result == ALPHAESS_CODE_NOT_BOUND:
            return True, "{} was not bound to this AppID".format(sn)
        return False, "Could not unbind {}: {}".format(sn, self.describe_code(result))

    async def _handle_unbind_event(self, sn, value):
        """Drive the per-serial unbind toggle, following Sigenergy's offboard pattern.

        Deliberately NOT gated on switch.predbat_set_read_only or alphaess_control_enable:
        both guard writes to the INVERTER, and unbinding is account management rather than
        an inverter write.

        Turning the switch back off clears the latch so discovery picks the system up again
        if it was re-bound via the CLI or the AlphaESS portal. It does NOT re-bind - that
        is impossible without the code emailed to the system owner, so the switch is
        one-way from Home Assistant.
        """
        if not self._to_bool(value):
            self._unbind_done.discard(sn)
            await self.save_control()
            return
        if sn in self._unbind_done:
            return
        ok, message = await self.unbind_system(sn)
        if not ok:
            # Leave the latch clear so the next tick retries, matching
            # _offboard_system_if_needed.
            self.log("Warn: AlphaESS {}".format(message))
            return
        self._unbind_done.add(sn)
        if sn in self.device_list:
            self.device_list = [serial for serial in self.device_list if serial != sn]
        self.log("Info: AlphaESS {}. Predbat can no longer read or control it, so num_inverters and the auto-configured args in apps.yaml now reference a system it cannot reach - re-bind it from the portal or the CLI, or update apps.yaml.".format(message))
        await self.save_control()
```

Add the unbind switch to `publish_schedule_settings_ha`, immediately after the write button:

```python
        self.dashboard_item(
            self._control_name("switch", sn, "unbind"),
            state="on" if sn in self._unbind_done else "off",
            attributes={"friendly_name": "AlphaESS {} Unbind (one-way - re-binding needs a code emailed to the system owner)".format(sn), "icon": "mdi:link-off"},
            app="alphaess",
        )
```

Remove the Task 10 stub `_handle_unbind_event`.

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t13.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t13.log
```

Expected: all six AlphaESS suites PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py apps/predbat/tests/test_alphaess_control.py
git commit -m "feat(alphaess): bind/unbind with code-aware results and a one-way unbind switch"
```

---

## Task 14: The run loop

**Files:**
- Modify: `apps/predbat/alphaess.py`
- Modify: `apps/predbat/tests/test_alphaess_api.py`

**Interfaces:**
- Consumes: every refresh and publish method.
- Produces: `async run(self, seconds, first) -> bool`, `async final(self) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_alphaess_api.py`:

```python
def test_alphaess_run_defers_startup_without_telemetry():
    """automatic_config() runs on the FIRST cycle alone.

    Returning True with no telemetry would map only the args backed by cached ratings and
    permanently skip soc_max and the energy args for the whole session. Returning False
    leaves ComponentBase's `first` flag set so the whole startup path is retried.
    """
    failed = False
    client = MockAlphaESS(automatic=True)
    responses = [
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, ESS_LIST_SAMPLE)),
        create_aiohttp_mock_response(status=200, json_data=_envelope(6042, None, msg="system offline")),
        create_aiohttp_mock_response(status=200, json_data=_envelope(200, [])),
    ]
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(responses)):
        ok = run_async_local(client.run(seconds=0, first=True))
    if ok:
        print("ERROR: run() reported success with no telemetry on the first cycle")
        failed = True
    if "inverter_type" in client.base.args:
        print("ERROR: automatic_config ran without telemetry")
        failed = True
    assert not failed, "test_alphaess_run_defers_startup_without_telemetry"


def test_alphaess_run_returns_false_when_the_account_has_no_systems():
    """Deliberately explicit rather than falling through to an implicit None.

    ComponentBase only clears its `first` flag on a truthy return, so an accidental None
    would strand the component in the ever-growing startup backoff forever.
    """
    failed = False
    client = MockAlphaESS()
    empty = create_aiohttp_mock_response(status=200, json_data=_envelope(200, []))
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(empty)):
        ok = run_async_local(client.run(seconds=0, first=True))
    if ok is not False:
        print(f"ERROR: run() returned {ok!r}, must be an explicit False")
        failed = True
    assert not failed, "test_alphaess_run_returns_false_when_the_account_has_no_systems"


def test_alphaess_run_reads_control_entities_on_every_tick_including_the_first():
    """Home Assistant retains the control entities across a Predbat restart, so on restart
    they already hold the live plan.

    Seeding local_schedule from _empty_schedule() and publishing that back would reach
    set_state_wrapper and cancel an in-flight charge until Predbat next replanned.
    """
    failed = False
    client = MockAlphaESS()
    client.device_list = ["AL70"]
    client.device_detail = {"AL70": {"sysSn": "AL70", "cobat": 13.34, "poinv": 5.0}}
    client.mark_refreshed("static")
    client.mark_refreshed("config")
    client.mark_refreshed("power")
    client.mark_refreshed("energy")
    client.device_values["AL70"] = {"soc": 50.0}
    # A retained plan, as HA would hold it after a restart.
    client.state["switch.predbat_alphaess_al70_battery_schedule_charge_enable"] = "on"
    client.state["number.predbat_alphaess_al70_battery_schedule_charge_soc"] = 88
    with patch("alphaess.aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data=_envelope(200, None)))):
        run_async_local(client.run(seconds=0, first=True))
    schedule = client.local_schedule.get("AL70", {})
    if schedule.get("charge", {}).get("soc") != 88:
        print(f"ERROR: retained plan was overwritten: {schedule}")
        failed = True
    assert not failed, "test_alphaess_run_reads_control_entities_on_every_tick_including_the_first"
```

Add to the runner list:

```python
        ("run_defers_without_telemetry", test_alphaess_run_defers_startup_without_telemetry),
        ("run_empty_account", test_alphaess_run_returns_false_when_the_account_has_no_systems),
        ("run_reads_controls_first_tick", test_alphaess_run_reads_control_entities_on_every_tick_including_the_first),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test alphaess_api > /tmp/t14.log 2>&1; grep -E "FAILED|EXCEPTION" /tmp/t14.log | head
```

Expected: EXCEPTION — `run` does not exist.

- [ ] **Step 3: Implement run() and final()**

Add to the `alphaess_const` imports:

```python
from alphaess_const import ALPHAESS_TTL_STATIC, ALPHAESS_TTL_CONFIG, ALPHAESS_TTL_ENERGY
```

Append to `AlphaESSAPI`:

```python
    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any schedule change.

        Returns True on a completed cycle, False on a failure that should hold the
        component in ComponentBase's startup backoff and be retried. Deliberately explicit
        rather than falling through to Python's implicit `None`: ComponentBase only clears
        its `first` flag and moves to the normal cadence when this returns something
        truthy, so an accidental `None` here would strand the component in the
        ever-growing startup backoff (60s doubling to 128 minutes) forever, even though
        every cycle after the first was actually working.
        """
        if first:
            await self.restore_state()
        if not self.app_id or not self.app_secret:
            self.log("Warn: AlphaESS needs both alphaess_app_id and alphaess_app_secret; get them from https://open.alphaess.com/")
            return False

        if self.tier_expired("static", ALPHAESS_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: AlphaESS found no battery systems on this account")
            return False
        if self.tier_expired("config", ALPHAESS_TTL_CONFIG):
            await self.refresh_config()
        live_ok = True
        if self.tier_expired("power", self.power_tier_ttl()):
            live_ok = await self.refresh_power()
        if self.tier_expired("energy", ALPHAESS_TTL_ENERGY):
            await self.refresh_energy()

        for sn in list(self.device_list):
            # Read the control entities EVERY tick, the first one included. Home Assistant
            # retains them across a Predbat restart, so on a restart they already hold the
            # live plan; seeding local_schedule from _empty_schedule() and publishing that
            # back would overwrite it (dashboard_item reaches set_state_wrapper) and cancel
            # an in-flight charge until Predbat next replanned. get_schedule_settings_ha
            # falls back to the disabled default per field, so a genuinely cold start still
            # lands on exactly _empty_schedule()'s shape.
            try:
                await self.get_schedule_settings_ha(sn)
            except Exception as error:
                self.log("Warn: AlphaESS schedule read failed for {}: {}".format(sn, error))
            await self._reconcile_control(sn)
            # Published every tick, not just first: this is Predbat's control surface and
            # must keep reflecting local_schedule as it changes.
            await self.publish_schedule_settings_ha(sn)

        await self.publish_data()

        if first and not live_ok:
            # Startup has not really succeeded without telemetry: automatic_config() runs
            # on the first cycle ALONE, so it would map only the args backed by cached
            # ratings and permanently skip soc_max and the energy args for the whole
            # session. Returning False leaves ComponentBase's `first` flag set, so the
            # entire startup path is retried on its backoff until a poll comes back.
            self.log("Warn: AlphaESS first telemetry poll returned nothing, deferring startup; it will be retried after a backoff")
            return False

        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state on shutdown so a restart resumes without re-polling."""
        await self.save_static()
        await self.save_config()
        await self.save_ratings()
        await self.save_control()
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t14.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t14.log
```

Expected: all six AlphaESS suites PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py apps/predbat/tests/test_alphaess_api.py
git commit -m "feat(alphaess): tiered run loop with deferred startup and shutdown persistence"
```

---

## Task 15: Standalone CLI

**Files:**
- Modify: `apps/predbat/alphaess.py` (append module-level functions)

**Interfaces:**
- Consumes: `AlphaESSAPI`, `MockBase`.
- Produces: `_build_alphaess(mock_base, args)`, `async test_alphaess_api(args)`, `main()`, `__main__` guard.

- [ ] **Step 1: Write the CLI**

Append to `apps/predbat/alphaess.py`, after the class:

```python
def _build_alphaess(mock_base, args):  # pragma: no cover
    """Construct an AlphaESSAPI around a MockBase for standalone command-line use.

    Passed into the constructor in a single call: ComponentBase.__init__ already calls
    initialize(**kwargs), so a separate follow-up call to initialize() would re-run it a
    second time and print a duplicate banner before the CLI has reported anything.
    """
    return AlphaESSAPI(
        mock_base,
        app_id=args.app_id,
        app_secret=args.app_secret,
        inverter_sn=args.serial,
        automatic=False,
        control_enable=False,
        api_delay=args.api_delay,
    )


def _confirm(prompt):  # pragma: no cover
    """Ask for confirmation, treating a closed stdin and Ctrl-C as 'no'.

    This is the only verification tool a remote tester has, so a closed or redirected
    stdin (SSH, CI, a container with no TTY) must not crash it with a raw traceback -
    EOFError and Ctrl-C both mean "no", cleanly.
    """
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\nNo input available, nothing sent.")
        return False
    return answer.strip().lower() == "y"


async def test_alphaess_api(args):  # pragma: no cover
    """Run one AlphaESS diagnostic pass, or a single bind/unbind action."""
    from mock_base import MockBase

    client = _build_alphaess(MockBase(), args)

    if args.verify:
        print(f"This asks AlphaESS to EMAIL a verification code to the registered owner of {args.serial}.")
        if not _confirm("Send that request? [y/N] "):
            return
        ok, message = await client.request_verification_code(args.serial, args.check_code)
        print(("OK: " if ok else "FAILED: ") + message)
        return

    if args.bind:
        print(f"This binds {args.serial} to AppID {args.app_id}.")
        if not _confirm("Send the bind request? [y/N] "):
            return
        ok, message = await client.bind_system(args.serial, args.code)
        print(("OK: " if ok else "FAILED: ") + message)
        return

    if args.unbind:
        print(f"This UNBINDS {args.serial} from AppID {args.app_id}.")
        print("Predbat will no longer be able to read or control it, and re-binding needs a code emailed to the system owner.")
        if not _confirm("Send the unbind request? [y/N] "):
            return
        ok, message = await client.unbind_system(args.serial)
        print(("OK: " if ok else "FAILED: ") + message)
        return

    print("Calling run() once (read-only: discover, poll config/telemetry, publish)...")
    ok = await client.run(seconds=0, first=True)
    if not ok:
        if client.discovery_ok is False:
            print("\nDISCOVERY FAILED - the getEssList call itself was rejected.")
            if client.last_api_error:
                print(f"  AlphaESS said: {client.last_api_error}")
            print("  Check --app-id and --app-secret, and check this host's clock: the signature is")
            print("  only valid within 300 seconds of AlphaESS server time.")
        elif not client.device_list:
            print("\nCREDENTIALS OK, but no battery systems were found on this account.")
            if args.serial:
                print(f"  --serial {args.serial!r} was set - a filter matching nothing looks identical to an empty")
                print("  account; retry without --serial to check.")
            print("  Systems reporting no battery capacity (plug-in solar) are skipped by design.")
        else:
            print(f"\nDISCOVERY OK ({len(client.device_list)} system(s): {client.device_list}), but the first telemetry poll came back empty.")
            print("  Check the Warn: lines above for which endpoint failed.")
        await client.final()
        return

    print(f"Systems: {client.device_list}")
    for sn in client.device_list:
        print(f"\n--- {sn} detail ---")
        print(json.dumps(client.device_detail.get(sn, {}), indent=2, default=str))
        print(f"\n--- {sn} telemetry ---")
        print(json.dumps(client.device_values.get(sn, {}), indent=2, default=str))
        print(f"\n--- {sn} energy ---")
        print(json.dumps(client.device_energy.get(sn, {}), indent=2, default=str))
        if args.dump_settings:
            print(f"\n--- {sn} charge/discharge config ---")
            print(json.dumps(client.device_config.get(sn, {}), indent=2, default=str))
        live = "live (getLastPowerData)" if client._live_ok.get(sn) is not False else "history (getOneDayPowerBySn, 5 minute)"
        periodic = {True: "yes", False: "no (6017)", None: "unknown"}[client._periodic_ok.get(sn)]
        print(f"\nDerived: capacity={client.battery_capacity(sn):.2f} kWh, inverter_limit={client.inverter_limit(sn):.0f} W, battery_rate_max={client.battery_rate_max(sn):.0f} W")
        print(f"Telemetry source: {live}; periodic schedule API entitled: {periodic}")

    await client.final()
    print("Done")


def main():  # pragma: no cover
    """Command-line entry point for AlphaESS diagnostics and system binding."""
    import argparse

    parser = argparse.ArgumentParser(description="AlphaESS Open API diagnostics")
    parser.add_argument("--app-id", required=True, help="AlphaESS developer AppID from https://open.alphaess.com/")
    parser.add_argument("--app-secret", required=True, help="AlphaESS developer AppSecret")
    parser.add_argument("--serial", default=None, help="Restrict to one system serial (sysSn)")
    parser.add_argument("--api-delay", type=float, default=2, help="Seconds to wait between API calls")
    parser.add_argument("--dump-settings", action="store_true", help="Print the full charge/discharge config objects")
    parser.add_argument("--verify", action="store_true", help="Ask AlphaESS to email a verification code to the system owner (needs --serial and --check-code)")
    parser.add_argument("--check-code", default=None, help="The system's CheckCode, from the device label or the installer")
    parser.add_argument("--bind", action="store_true", help="Bind --serial to this AppID (needs --code from the verification email)")
    parser.add_argument("--code", default=None, help="Verification code from the email triggered by --verify")
    parser.add_argument("--unbind", action="store_true", help="Unbind --serial from this AppID")
    args = parser.parse_args()

    if (args.verify or args.bind or args.unbind) and not args.serial:
        parser.error("--serial is required with --verify, --bind or --unbind")
    if args.verify and not args.check_code:
        parser.error("--check-code is required with --verify")
    if args.bind and not args.code:
        parser.error("--code is required with --bind (run --verify first to have one emailed)")

    asyncio.run(test_alphaess_api(args))


if __name__ == "__main__":  # pragma: no cover
    main()
```

- [ ] **Step 2: Verify the CLI parses and refuses bad argument combinations**

```bash
cd /Users/treforsouthwell/predbat/batpred/apps/predbat
python3 alphaess.py --help > /tmp/t15.log 2>&1; grep -E "unbind|verify|bind|serial" /tmp/t15.log
python3 alphaess.py --app-id x --app-secret y --bind 2>&1 | tail -2
```

Expected: the help lists every flag; the second command errors with `--serial is required`.

- [ ] **Step 3: Verify a destructive action refuses to run without a TTY**

```bash
cd /Users/treforsouthwell/predbat/batpred/apps/predbat
python3 alphaess.py --app-id x --app-secret y --serial AL70 --unbind < /dev/null 2>&1 | tail -3
```

Expected: it prints the warning, then `No input available, nothing sent.` — **no traceback, and no API call**.

- [ ] **Step 4: Run the suite to confirm nothing broke**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all -k alphaess > /tmp/t15.log 2>&1; grep -E "PASSED|FAILED|EXCEPTION" /tmp/t15.log
```

Expected: all six AlphaESS suites PASSED.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/alphaess.py
git commit -m "feat(alphaess): standalone CLI for diagnostics, verify, bind and unbind"
```

---

## Task 16: Documentation, template and final checks

**Files:**
- Create: `templates/alphaess_cloud.yaml`
- Modify: `docs/apps-yaml.md`
- Modify: `docs/inverter-setup.md`
- Modify: `.cspell/custom-dictionary-workspace.txt`

- [ ] **Step 1: Create the example configuration**

Copy `templates/sunsynk.yaml` to `templates/alphaess_cloud.yaml` and replace the
inverter-specific block. The AlphaESS-specific part must read:

```yaml
  # AlphaESS Open API - get your AppID and AppSecret from https://open.alphaess.com/
  # EXPERIMENTAL: nobody on the Predbat project has AlphaESS hardware, so please report
  # what you see. Every request and response is traced to the log while this beds in.
  alphaess_app_id: 'xxxx'
  alphaess_app_secret: 'xxxx'

  # Let Predbat configure the sensors and inverter settings for you
  alphaess_automatic: True

  # Set False for monitoring only - Predbat will not write to the inverter.
  # switch.predbat_set_read_only also holds back every write, including Predbat's own
  # periodic re-apply.
  alphaess_control_enable: True

  # Optional: restrict to one system when the account has several
  #alphaess_inverter_sn: 'AL70110230306xx'

  # Optional: the API reports no battery power limit, so Predbat estimates it from the
  # inverter's nominal power (poinv). Set this if you know your pack's real limit.
  #alphaess_battery_rate_max: 3600

  # Optional pacing. AlphaESS advise a minimum 10-second polling interval, and both write
  # endpoints are documented as writable once per 24 hours.
  #alphaess_api_delay: 2
  #alphaess_min_write_interval: 300

  # REQUIRED: the API does not report a grid export limit. If your grid connection is
  # capped below the inverter rating (G98/G99), set it here or Predbat will plan exports
  # it cannot deliver.
  #export_limit: 3680
```

- [ ] **Step 2: Add the documentation section**

In `docs/apps-yaml.md`, after the "Sunsynk Cloud API" section, add an "AlphaESS Cloud API"
section covering, in this order:

1. **EXPERIMENTAL** banner — nobody on the project has AlphaESS hardware; the wire
   behaviour is inferred from the published API docs and the Home Assistant integration,
   and every request/response is traced to the log by default.
2. Obtaining the AppID and AppSecret from <https://open.alphaess.com/>.
3. Every `alphaess_*` arg with its default, matching the template above.
4. That `alphaess_control_enable` defaults to **true**, and how to set it false for
   monitoring only.
5. That `switch.predbat_set_read_only` holds back every write, **including Predbat's own
   periodic re-apply**.
6. Cloud-to-inverter latency: settings reach the inverter on its next cloud poll, typically
   one to five minutes after Predbat writes them, so a read-back immediately after a write
   shows the old values and that is not a failure.
7. Last-writer-wins: using the AlphaESS phone app while Predbat runs overwrites Predbat's
   settings and vice versa — the endpoints are whole-object replacements.
8. That windows are snapped to the API's 15-minute grid, and why (off-grid values are
   accepted and silently ignored by the device).
9. That a **non-zero** charge rate is not honoured on the legacy two-window path — only on
   systems entitled to the periodic API — while a **zero** rate is meaningful on both, as
   it is how Predbat signals freeze.
10. That `battery_rate_max` is estimated from the inverter rating, and how to correct it
    with `battery_rate_max_scaling` or `alphaess_battery_rate_max`.
11. That `export_limit` **must** be set by hand for a G98/G99-capped site.
12. That `switch.predbat_inverter_hybrid` is only moved on positive evidence of AC coupling
    and should be checked by hand on a retrofit system.
13. That systems reporting no battery capacity (plug-in solar such as the VT1000 family)
    are skipped by design.
14. That a system not serving `getLastPowerData` automatically falls back to five-minute
    history data, and re-probes itself back to live data if it recovers.
15. That the unbind switch is **one-way** from Home Assistant: re-binding needs a code
    emailed to the system owner, via `python3 alphaess.py --verify` then `--bind`, or the
    AlphaESS portal.

In `docs/inverter-setup.md`, add an AlphaESS entry pointing at that section.

- [ ] **Step 3: Add the dictionary words**

Append to `.cspell/custom-dictionary-workspace.txt` (one per line):

```
alphaess
appid
appsecret
batusecap
bathighcap
cbat
chargelimit
chargepower
cobat
ctrdis
gridcharge
mbat
minv
openapi
poinv
popv
sysdn
syssn
timechae
timechaf
timedise
timedisf
uscapacity
```

**The file is auto-sorted on commit, so re-stage it after running pre-commit.**

- [ ] **Step 4: Run the full test suite**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all > /tmp/t16-full.log 2>&1; tail -5 /tmp/t16-full.log
grep -E "FAILED|ERROR" /tmp/t16-full.log | head -20
```

Expected: `**** All tests passed ...`, and no FAILED lines.

- [ ] **Step 5: Run pre-commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
coverage/venv/bin/pre-commit run --all-files > /tmp/t16-pre.log 2>&1; echo "exit=$?"; tail -30 /tmp/t16-pre.log
```

Exit 0 is the only pass. "files were modified by this hook" means a hook rewrote your
files — re-stage and re-run until it is clean.

Expected: everything passes. If `interrogate` reports a missing docstring, add it — 100%
coverage is required for every function *and* class. If CSpell reports an unknown word,
add it to the dictionary and re-run.

```bash
git add .cspell/custom-dictionary-workspace.txt   # re-stage after the auto-sort
```

- [ ] **Step 6: Verify the docs build**

```bash
cd /Users/treforsouthwell/predbat/batpred
mkdocs build --strict > /tmp/t16-docs.log 2>&1; tail -10 /tmp/t16-docs.log
```

Expected: no warnings about the new content. (No `mkdocs.yml` change is needed — the
AlphaESS content lives inside existing pages.)

- [ ] **Step 7: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add templates/alphaess_cloud.yaml docs/apps-yaml.md docs/inverter-setup.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(alphaess): add the AlphaESS Cloud API guide, template and dictionary words"
```

- [ ] **Step 8: Final verification before opening a PR**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all > /tmp/final.log 2>&1; tail -3 /tmp/final.log
cd /Users/treforsouthwell/predbat/batpred && coverage/venv/bin/pre-commit run --all-files > /tmp/final-pre.log 2>&1; echo "exit=$?"; tail -5 /tmp/final-pre.log
git log --oneline main..HEAD
```

Confirm: the full suite passes, pre-commit passes, and the branch holds the spec commit
plus one commit per task. **Do not claim completion without pasting the actual tail of
both logs** — evidence before assertions.

---

## Field verification checklist

Carry these into the PR description. Each is marked `VERIFY@FIELD` in `alphaess_const.py`
and needs a tester's log, since nobody on the project has AlphaESS hardware.

1. **`pbat` sign on charge.** Discharge-positive is confirmed by arithmetic from the API
   docs' live sample; charge-negative is inferred.
2. **`ctrDis = 1` with both periods disabled means "never discharge".** Freeze export
   depends on it.
3. **Whether surplus above house load reaches the grid during a discharge window.**
   `ctrDis` is "Battery Discharge Time Control", so this may depend on the unit's working
   mode. The writes are identical either way.
4. **Whether the 24-hour write limit is enforced.** If a tester sees `6008` or `6053` on a
   second same-day write, `alphaess_min_write_interval` becomes the primary defence and its
   default should rise.
5. **`usCapacity` semantics** — current SOC or configured usable depth. Not relied on
   either way.
6. **How closely `poinv` tracks the real battery rate.** If testers'
   `battery_rate_max_scaling` suggestions land consistently below 1.0, the derivation
   should apply that factor rather than `poinv` raw.
7. **`ppvDetail` null-versus-zero on an AC-coupled unit.** The hybrid inference rests on it.
8. **Which models are actually AC-coupled.** `ALPHAESS_AC_COUPLED_MODELS` ships empty.
9. **Whether a `Storion-S5`'s history carries `cbat`.** If it does not, that model cannot
   be driven at all and users must be told before it is called supported.
10. **Periodic entitlement in the wild.** How many real systems answer `200` rather than
    `6017` for `getTimeChargeBySn`.

Once the format is confirmed, set `AlphaESSAPI.api_debug = False`.

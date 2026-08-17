# Sunsynk Cloud Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Sunsynk Connect cloud component to Predbat so a Sunsynk hybrid inverter can be monitored and battery-controlled with no local hardware access.

**Architecture:** A standalone `SunsynkAPI(ComponentBase, OAuthMixin)` in `apps/predbat/sunsynk.py` with its constants and pure-Python RSA helper in `apps/predbat/sunsynk_const.py`, structured in `deye.py`'s image but sharing no code with it. Predbat's charge and export windows are derived into Sunsynk's six time-of-use slots and applied by read-modify-write of the whole settings object. `deye.py` is not modified.

**Tech Stack:** Python 3, `aiohttp`, Predbat's `ComponentBase` / `OAuthMixin` / `MockBase` / Storage, and Predbat's own test runner (not pytest).

**Spec:** [docs/superpowers/specs/2026-08-17-sunsynk-cloud-integration-design.md](../specs/2026-08-17-sunsynk-cloud-integration-design.md)

## Global Constraints

Every task's requirements implicitly include this section.

- **Line length:** 256 characters (Black), 250 (Flake8). Long dict literals stay on one line — that is the house style, see `deye_const.py`.
- **Docstrings:** 100% coverage enforced by `interrogate` with `ignore-nested-functions = false`. **Every** function, method and class needs one, including nested test helpers such as `async def fake_get(...)`. `ignore-module = true`, so module docstrings are optional but every file in this plan has one anyway.
- **Spelling:** British English via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit — re-stage after running pre-commit.
- **Naming:** `lower_case_with_underscores`.
- **Tests are not pytest.** Each test function returns nothing and ends with `assert not failed, "test_name"`. Each module exposes `run_<name>_tests(my_predbat)` returning a `failed` boolean, registered in `TEST_REGISTRY` in `unit_test.py`.
- **Run tests from `coverage/`,** and per `CLAUDE.md` always redirect output to a file and grep the file — never pipe straight to grep.
- **Verification unknowns:** every value the spec marks as an inferred *encoding* claim carries a `# VERIFY@SPIKE` comment. Do not remove them; nobody has tested against live hardware.
- **Fail closed:** any response that does not parse as expected must skip the write and leave the inverter in self-use. Never fall through into an assumed-DEYE behaviour.

**Pre-commit:** `./run_pre_commit` (binary lives at `./coverage/venv/bin/pre-commit`).

---

### Task 1: Constants module

Everything downstream imports from here, so a value correction is a one-line change in one file.

**Files:**

- Create: `apps/predbat/sunsynk_const.py`
- Create: `apps/predbat/tests/test_sunsynk_const.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: nothing.
- Produces: `SUNSYNK_REGIONS`, `SUNSYNK_ENDPOINTS`, `SUNSYNK_WORKMODE`, `TOU_FIELD`, `TOU_SLOT_COUNT`, `TOU_FILLER_TIMES`, `FREEZE_EXPORT_SOC`, `SUNSYNK_TELEMETRY`, `SUNSYNK_ENERGY`, `SUNSYNK_BOOL_FIELDS`, `SUNSYNK_DAY_FIELDS`, `encode_setting(name, value)`, the TTL and cache-name constants, and `SUNSYNK_AUTH_ERROR_MARKERS`.

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_sunsynk_const.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud constants
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk Cloud constants module (sunsynk_const.py)."""

from sunsynk_const import (
    SUNSYNK_REGIONS,
    SUNSYNK_ENDPOINTS,
    SUNSYNK_WORKMODE,
    SUNSYNK_TELEMETRY,
    SUNSYNK_ENERGY,
    SUNSYNK_BOOL_FIELDS,
    SUNSYNK_DAY_FIELDS,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    TOU_FILLER_TIMES,
    FREEZE_EXPORT_SOC,
    encode_setting,
)


def test_sunsynk_regions():
    """Both regions expose an https host and the source token the login signs with."""
    failed = False
    for region, expect_source in (("sunsynk", "sunsynk"), ("inteless", "elinter")):
        entry = SUNSYNK_REGIONS.get(region)
        if not entry:
            print(f"ERROR: region {region} missing")
            failed = True
            continue
        if not entry.get("host", "").startswith("https://"):
            print(f"ERROR: region {region} host not https: {entry.get('host')}")
            failed = True
        if entry.get("source") != expect_source:
            print(f"ERROR: region {region} source {entry.get('source')} != {expect_source}")
            failed = True
    assert not failed, "test_sunsynk_regions"


def test_sunsynk_endpoints():
    """Every endpoint the component calls is declared, and templated ones carry {sn}."""
    failed = False
    for endpoint in ("public_key", "token", "token_legacy", "inverter_list", "inverter_detail", "battery", "grid", "load", "input", "settings_read", "settings_set"):
        if endpoint not in SUNSYNK_ENDPOINTS:
            print(f"ERROR: endpoint {endpoint} missing")
            failed = True
    for endpoint in ("inverter_detail", "battery", "grid", "load", "input", "settings_read", "settings_set"):
        if "{sn}" not in SUNSYNK_ENDPOINTS.get(endpoint, ""):
            print(f"ERROR: endpoint {endpoint} has no {{sn}} placeholder")
            failed = True
    assert not failed, "test_sunsynk_endpoints"


def test_sunsynk_workmode_semantics():
    """The three DEYE-equivalent work modes exist and have distinct wire values."""
    failed = False
    for mode in ("selling_first", "zero_export_load", "zero_export_ct"):
        if mode not in SUNSYNK_WORKMODE:
            print(f"ERROR: work mode {mode} missing")
            failed = True
    values = list(SUNSYNK_WORKMODE.values())
    if len(set(values)) != len(values):
        print(f"ERROR: work mode wire values not distinct: {values}")
        failed = True
    assert not failed, "test_sunsynk_workmode_semantics"


def test_tou_field_templates():
    """Each per-slot field is a template that renders a distinct name for all 6 slots."""
    failed = False
    if TOU_SLOT_COUNT != 6:
        print(f"ERROR: TOU_SLOT_COUNT must be 6, got {TOU_SLOT_COUNT}")
        failed = True
    for concept in ("time", "power", "soc", "grid_charge"):
        template = TOU_FIELD.get(concept)
        if not template:
            print(f"ERROR: TOU_FIELD {concept} missing")
            failed = True
            continue
        if "{n}" not in template:
            print(f"ERROR: TOU_FIELD {concept} is not a template: {template}")
            failed = True
            continue
        names = [template.format(n=n) for n in range(1, TOU_SLOT_COUNT + 1)]
        if len(set(names)) != TOU_SLOT_COUNT:
            print(f"ERROR: TOU_FIELD {concept} does not render 6 distinct names: {names}")
            failed = True
    assert not failed, "test_tou_field_templates"


def test_tou_filler_times_sufficient():
    """There are enough distinct filler times to pad any schedule out to 6 slots."""
    failed = False
    if len(set(TOU_FILLER_TIMES)) != len(TOU_FILLER_TIMES):
        print(f"ERROR: filler times not distinct: {TOU_FILLER_TIMES}")
        failed = True
    # A schedule contributes at most 4 boundary times (charge start/end, export start/end),
    # so padding needs TOU_SLOT_COUNT distinct fillers to survive every one colliding.
    if len(TOU_FILLER_TIMES) <= TOU_SLOT_COUNT:
        print(f"ERROR: need more than {TOU_SLOT_COUNT} filler times, got {len(TOU_FILLER_TIMES)}")
        failed = True
    if TOU_FILLER_TIMES != sorted(TOU_FILLER_TIMES):
        print(f"ERROR: filler times not ascending: {TOU_FILLER_TIMES}")
        failed = True
    if TOU_FILLER_TIMES[0] != "00:00":
        print(f"ERROR: first filler time must be 00:00, got {TOU_FILLER_TIMES[0]}")
        failed = True
    assert not failed, "test_tou_filler_times_sufficient"


def test_telemetry_maps_cover_predbat_args():
    """Every sensor Predbat needs is sourced from a declared endpoint and field."""
    failed = False
    valid_sources = ("battery", "grid", "load", "input", "detail")
    for leaf in ("soc", "battery_power", "grid_power", "load_power", "pv_power", "temperature"):
        if leaf not in SUNSYNK_TELEMETRY:
            print(f"ERROR: telemetry {leaf} missing")
            failed = True
    for leaf in ("pv_today", "import_today", "export_today", "load_today", "battery_charge_today", "battery_discharge_today"):
        if leaf not in SUNSYNK_ENERGY:
            print(f"ERROR: energy counter {leaf} missing")
            failed = True
    for name, mapping in list(SUNSYNK_TELEMETRY.items()) + list(SUNSYNK_ENERGY.items()):
        source, field = mapping
        if source not in valid_sources:
            print(f"ERROR: {name} reads from unknown source {source}")
            failed = True
        if not field:
            print(f"ERROR: {name} has an empty field name")
            failed = True
    assert not failed, "test_telemetry_maps_cover_predbat_args"


def test_encode_setting_types():
    """Boolean fields serialise bare, everything else serialises as a string."""
    failed = False
    # All six grid-charge flags and all seven day flags must be declared boolean.
    for n in range(1, TOU_SLOT_COUNT + 1):
        name = TOU_FIELD["grid_charge"].format(n=n)
        if name not in SUNSYNK_BOOL_FIELDS:
            print(f"ERROR: {name} must be declared a boolean field")
            failed = True
    if len(SUNSYNK_DAY_FIELDS) != 7:
        print(f"ERROR: expected 7 day fields, got {len(SUNSYNK_DAY_FIELDS)}")
        failed = True
    for day in SUNSYNK_DAY_FIELDS:
        if day not in SUNSYNK_BOOL_FIELDS:
            print(f"ERROR: day field {day} must be declared a boolean field")
            failed = True
    cases = [
        ("time1on", True, True),
        ("time1on", "true", True),
        ("time1on", 1, True),
        ("time1on", False, False),
        ("time1on", "false", False),
        ("time1on", 0, False),
        ("mondayOn", True, True),
        ("cap1", 95, "95"),
        ("cap1", "95", "95"),
        ("sellTime1", "02:00", "02:00"),
        ("sellTime1Pac", 3000, "3000"),
        ("sysWorkMode", "1", "1"),
    ]
    for name, value, expect in cases:
        got = encode_setting(name, value)
        if got != expect or not isinstance(got, type(expect)):
            print(f"ERROR: encode_setting({name!r}, {value!r}) = {got!r} ({type(got).__name__}), expected {expect!r} ({type(expect).__name__})")
            failed = True
    assert not failed, "test_encode_setting_types"


def test_freeze_export_soc():
    """Freeze-export holds at 99% so the battery neither charges nor discharges."""
    failed = False
    if FREEZE_EXPORT_SOC != 99:
        print(f"ERROR: FREEZE_EXPORT_SOC must be 99, got {FREEZE_EXPORT_SOC}")
        failed = True
    assert not failed, "test_freeze_export_soc"


def run_sunsynk_const_tests(my_predbat):
    """Run all Sunsynk constants tests."""
    failed = False
    for name, fn in [
        ("regions", test_sunsynk_regions),
        ("endpoints", test_sunsynk_endpoints),
        ("workmode_semantics", test_sunsynk_workmode_semantics),
        ("tou_field_templates", test_tou_field_templates),
        ("tou_filler_times", test_tou_filler_times_sufficient),
        ("telemetry_maps", test_telemetry_maps_cover_predbat_args),
        ("encode_setting", test_encode_setting_types),
        ("freeze_export_soc", test_freeze_export_soc),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register the suite in the test runner**

In `apps/predbat/unit_test.py`, add the import next to the DEYE imports (around line 158):

```python
from tests.test_sunsynk_const import run_sunsynk_const_tests
```

and the registry entry next to the DEYE entries (around line 435):

```python
        ("sunsynk_const", run_sunsynk_const_tests, "Sunsynk constants tests", False),
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd coverage && ./run_all --test sunsynk_const > /tmp/predbat_sunsynk_const.log 2>&1; grep -iE "error|fail|exception|no module" /tmp/predbat_sunsynk_const.log | head -20
```

Expected: `ModuleNotFoundError: No module named 'sunsynk_const'`.

- [ ] **Step 4: Write the constants module**

Create `apps/predbat/sunsynk_const.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sunsynk Cloud API constants
# -----------------------------------------------------------------------------

"""Sunsynk Connect API constants and the RSA login helper.

Sunsynk publishes no API documentation, so values marked ``# VERIFY@SPIKE`` are
inferred from third-party clients (solarsynkv3 and synkctl) and have not been
confirmed against live hardware. Per the design spec, DEYE is assumed for
*semantics* (the six sequential time-of-use slots, the three work modes and what
they do) but never for *encoding* — every field name and wire value here comes
from a Sunsynk source. All component logic imports from this module so a
correction needs no downstream edits.
"""

SUNSYNK_REGIONS = {
    "sunsynk": {"host": "https://api.sunsynk.net", "source": "sunsynk"},
    "inteless": {"host": "https://pv.inteless.com", "source": "elinter"},
}

SUNSYNK_ENDPOINTS = {
    "public_key": "/anonymous/publicKey",
    "token": "/oauth/token/new",
    "token_legacy": "/oauth/token",
    "inverter_list": "/api/v1/inverters",
    "inverter_detail": "/api/v1/inverter/{sn}",
    "battery": "/api/v1/inverter/battery/{sn}/realtime",
    "grid": "/api/v1/inverter/grid/{sn}/realtime",
    "load": "/api/v1/inverter/load/{sn}/realtime",
    "input": "/api/v1/inverter/{sn}/realtime/input",
    "settings_read": "/api/v1/common/setting/{sn}/read",
    "settings_set": "/api/v1/common/setting/{sn}/set",
}

SUNSYNK_TIMEOUT = 30
SUNSYNK_RETRIES = 3
SUNSYNK_PAGE_SIZE = 10
SUNSYNK_CLIENT_ID = "csp-web"

TOU_SLOT_COUNT = 6
FREEZE_EXPORT_SOC = 99

# Distinct ascending start times used to pad a schedule out to TOU_SLOT_COUNT.
# Sunsynk's slots are sequential intervals ("from this start until the next slot's
# start"), so every start must be unique — duplicates create zero-length intervals.
# Seven options guarantee TOU_SLOT_COUNT distinct times survive even if all four
# of a schedule's own window boundaries collide with fillers.
TOU_FILLER_TIMES = ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00", "23:00"]

# VERIFY@SPIKE — that Sunsynk has these three modes is a semantic claim inherited
# from DEYE and is safe. That they are numbered 0/1/2 IN DEYE'S ORDER is an
# ENCODING claim and is the single highest-cost unknown in this integration:
# getting it wrong silently swaps export for charge. Confirm before enabling control.
SUNSYNK_WORKMODE = {
    "selling_first": "0",
    "zero_export_load": "1",
    "zero_export_ct": "2",
}

# Per-slot field name templates, rendered with n = 1..TOU_SLOT_COUNT.
TOU_FIELD = {
    "time": "sellTime{n}",
    "power": "sellTime{n}Pac",
    "soc": "cap{n}",
    "grid_charge": "time{n}on",
}

# Slot fields Predbat does NOT own and must preserve verbatim from the read.
TOU_FIELD_PRESERVED = ["genTime{n}on", "sellTime{n}Volt"]

SUNSYNK_DAY_FIELDS = ["mondayOn", "tuesdayOn", "wednesdayOn", "thursdayOn", "fridayOn", "saturdayOn", "sundayOn"]

# Top-level settings keys Predbat owns.
SUNSYNK_WORKMODE_FIELD = "sysWorkMode"
SUNSYNK_SOLAR_SELL_FIELD = "solarSell"
SUNSYNK_TOU_ENABLE_FIELD = "peakAndVallery"
SUNSYNK_SERIAL_FIELD = "sn"

# VERIFY@SPIKE — solarsynkv3 carries a ReplaceTRUE() helper that rewrites the string
# "true" to a bare true before posting, which is strong evidence the API needs real
# JSON booleans for the per-slot and day flags while numeric fields stay quoted
# strings. Declared per field here rather than guessed at each call site.
SUNSYNK_BOOL_FIELDS = frozenset([TOU_FIELD["grid_charge"].format(n=n) for n in range(1, TOU_SLOT_COUNT + 1)] + SUNSYNK_DAY_FIELDS)

# Values that mean False when Sunsynk hands a flag back as a string.
SUNSYNK_FALSE_STRINGS = frozenset(["false", "0", "", "none", "off", "no"])


def encode_setting(name, value):
    """Serialise one settings value the way Sunsynk expects it on the wire.

    Boolean fields (per-slot grid charge, day-of-week enables) go bare; every other
    field is quoted, because Sunsynk returns and accepts its numerics as strings.
    """
    if name in SUNSYNK_BOOL_FIELDS:
        if isinstance(value, str):
            return value.strip().lower() not in SUNSYNK_FALSE_STRINGS
        return bool(value)
    return str(value)


# Telemetry: Predbat sensor leaf -> (endpoint key, response field).
SUNSYNK_TELEMETRY = {
    "soc": ("battery", "soc"),
    "battery_power": ("battery", "power"),
    "battery_voltage": ("battery", "voltage"),
    "temperature": ("battery", "temp"),
    "grid_power": ("grid", "pac"),
    "load_power": ("load", "totalPower"),
    "pv_power": ("input", "pac"),
}

# Daily energy counters: Predbat arg -> (endpoint key, response field).
SUNSYNK_ENERGY = {
    "pv_today": ("input", "etoday"),
    "import_today": ("grid", "etodayFrom"),
    "export_today": ("grid", "etodayTo"),
    "load_today": ("load", "dailyUsed"),
    "battery_charge_today": ("battery", "etodayChg"),
    "battery_discharge_today": ("battery", "etodayDischg"),
}

# VERIFY@SPIKE — sign convention. DEYE reports battery power positive on discharge;
# if Sunsynk agrees this stays empty, otherwise add "battery_power" here.
SUNSYNK_TELEMETRY_NEGATE = ()

# Fields used to derive ratings rather than published directly.
SUNSYNK_CAPACITY_AH_FIELD = "capacity"  # battery realtime, amp-hours
SUNSYNK_PACK_VOLTAGE_FIELD = "voltage"  # battery realtime, live pack volts
SUNSYNK_CHARGE_VOLT_FIELD = "chargeVolt"  # battery realtime, BMS charge target
SUNSYNK_MAX_CHARGE_CURRENT_FIELD = "maxChargeCurrentLimit"  # battery realtime, amps
SUNSYNK_RATED_POWER_FIELD = "ratePower"  # inverter detail, watts
SUNSYNK_BATTERY_LOW_CAP_FIELD = "batteryLowCap"  # settings, percent floor

# LiFePO4 cell voltages used to infer the pack's nominal voltage from its BMS charge
# target, so an amp-hour capacity can become kWh. Same derivation deye.py uses.
LIFEPO4_CHARGE_VOLTS_PER_CELL = 3.55
LIFEPO4_NOMINAL_VOLTS_PER_CELL = 3.2

# Refresh cadence per class of state, in minutes. ComponentBase ticks run() every 60
# seconds; these are the maximum ages a cached tier may reach before it is re-polled.
SUNSYNK_TTL_STATIC = 8 * 60  # inverter list and detail — changes when hardware does
SUNSYNK_TTL_CONFIG = 15  # the settings object — installer settings, effectively static
SUNSYNK_TTL_LIVE = 5  # telemetry; four endpoint calls per inverter, so slower than DEYE's

# Restore bound in minutes for the applied-payload cache. It is a change-detection cache
# with no read-back, so restoring it asserts the inverter still holds what Predbat last
# wrote. If it was changed externally while Predbat was down that assertion is false, the
# next write is wrongly SKIPPED and the battery silently diverges from the plan. A
# redundant write is cheap; a skipped one is not.
SUNSYNK_RESTORE_MAX_CONTROL = 15

# Cycles a written payload may remain absent from the read-back before warning. Sunsynk
# acknowledges a write at the cloud, but the dongle only collects it on its next poll —
# typically one to five minutes — so divergence within this bound is normal latency.
SUNSYNK_SETTLE_POLLS = 3

SUNSYNK_STORAGE_MODULE = "sunsynk"
SUNSYNK_CACHE_STATIC = "static"  # inverter serials, detail
SUNSYNK_CACHE_CONFIG = "config"  # last-read settings object
SUNSYNK_CACHE_RATINGS = "ratings"  # derived capacity, pack voltage, rated power
SUNSYNK_CACHE_CONTROL = "control"  # last-applied payload for change detection

# Sunsynk answers an expired token with HTTP 200 carrying a body-level failure, so
# status-code-only handling never triggers a refresh and the component stays broken
# until restart. Matched lower-cased against the body's msg. Keep these narrow enough
# that a genuine non-auth failure is never retried as one.
SUNSYNK_AUTH_ERROR_MARKERS = (
    "invalid token",
    "token invalid",
    "token expired",
    "token is expired",
    "expired token",
    "unauthorized",
    "unauthorised",
    "auth failed",
    "not logged in",
)

# Maximum characters of a request/response body written to the log when debug tracing.
SUNSYNK_DEBUG_MAX_CHARS = 20000

# Body keys redacted from debug traces: these carry credentials or bearer tokens and
# the logs are routinely pasted into issue reports.
SUNSYNK_DEBUG_REDACT_KEYS = ("password", "access_token", "refresh_token", "token", "Authorization", "sign")
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd coverage && ./run_all --test sunsynk_const > /tmp/predbat_sunsynk_const.log 2>&1; tail -20 /tmp/predbat_sunsynk_const.log
```

Expected: the suite passes with no `ERROR:` lines.

- [ ] **Step 6: Add new words to the spell-check dictionary**

Append to `.cspell/custom-dictionary-workspace.txt` (the file is auto-sorted on commit, so re-stage afterwards):

```text
elinter
inteless
solarsynk
synkctl
Vallery
```

- [ ] **Step 7: Run pre-commit and commit**

```bash
./run_pre_commit
git add apps/predbat/sunsynk_const.py apps/predbat/tests/test_sunsynk_const.py apps/predbat/unit_test.py .cspell/custom-dictionary-workspace.txt
git commit -m "feat(sunsynk): add Sunsynk Cloud constants module

Endpoints, regions, the six-slot TOU field templates, telemetry maps and
per-field wire serialisation for the Sunsynk Connect API. Inferred encoding
values carry VERIFY@SPIKE; no live hardware has confirmed them."
```

---

### Task 2: Pure-Python RSA login helper

Sunsynk RSA-encrypts the account password. This avoids adding `cryptography` to `requirements.txt`, which would pull a Rust-built binary wheel onto the armv7/armhf add-on targets. Only public-key encryption is performed — no private keys, no timing-sensitive operations.

**Files:**

- Modify: `apps/predbat/sunsynk_const.py` (append)
- Create: `apps/predbat/tests/test_sunsynk_auth.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: nothing from Task 1 (same file, independent functions).
- Produces: `parse_rsa_public_key(der_bytes) -> (modulus: int, exponent: int)` and `rsa_encrypt_pkcs1v15(public_key_b64: str, plaintext: str) -> str` returning base64 ciphertext.

- [ ] **Step 1: Write the failing test**

The keypair below is a real 1024-bit RSA key generated for this test. `SUNSYNK_TEST_PRIVATE_D` is its private exponent, present **only** so the test can decrypt and check the padding structure. PKCS#1 v1.5 encryption is randomised, so a fixed-ciphertext assertion would be either flaky or vacuous — the test round-trips instead.

Create `apps/predbat/tests/test_sunsynk_auth.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud authentication
# -----------------------------------------------------------------------------

"""Tests for Sunsynk login: the pure-Python RSA helper and the three auth methods."""

import base64
from sunsynk_const import parse_rsa_public_key, rsa_encrypt_pkcs1v15

# A real 1024-bit RSA public key, DER SubjectPublicKeyInfo, base64 — the same shape
# Sunsynk's /anonymous/publicKey returns (no PEM armour).
SUNSYNK_TEST_PUBLIC_KEY = (
    "MIGeMA0GCSqGSIb3DQEBAQUAA4GMADCBiAKBgFwp+M48x3PUYA63ZF2xEl4pFrh+1qQuk4B0UeTKCAqU51A8BURJdRs4ECXJEdJnxgO3hlkJyjVgBaeJgajxTu+c1oyOtQn9KVvW"
    "/Se0LEytkZRABnOsJkGprKWuNDm6N5YXPEH5yfnAfCL7Drnsn8rj3RjPmkzCg8XHI6xHGQD3AgMBAAE="
)
SUNSYNK_TEST_MODULUS = 0x5C29F8CE3CC773D4600EB7645DB1125E2916B87ED6A42E93807451E4CA080A94E7503C054449751B381025C911D267C603B7865909CA356005A78981A8F14EEF9CD68C8EB509FD295BD6FD27B42C4CAD9194400673AC2641A9ACA5AE3439BA3796173C41F9C9F9C07C22FB0EB9EC9FCAE3DD18CF9A4CC283C5C723AC471900F7
# Private exponent, used ONLY by this test to decrypt and inspect the padding.
SUNSYNK_TEST_PRIVATE_D = 0x438FD67F5964328527E5A1E046CE87A87F2128C927E53394ED95AD1DB5A784C4F8CCD888593180521E71B7EC0379E54398CB4606AA2691A4D28053F7B8E12CA643EC0257950C49747469A6092B548F9358DCBD311FC69088457B4A76213C07F7937C8745144F9F8EF7DA792DA35AA4FB5E5458B6A36ACDAD3327C4066AF8A01
SUNSYNK_TEST_KEY_BYTES = 128


def _decrypt(ciphertext_b64):
    """Decrypt with the test private key, returning the raw padded encryption block."""
    raw = base64.b64decode(ciphertext_b64)
    plain = pow(int.from_bytes(raw, "big"), SUNSYNK_TEST_PRIVATE_D, SUNSYNK_TEST_MODULUS)
    return plain.to_bytes(SUNSYNK_TEST_KEY_BYTES, "big")


def test_parse_rsa_public_key():
    """The DER SubjectPublicKeyInfo parser recovers the exact modulus and exponent."""
    failed = False
    modulus, exponent = parse_rsa_public_key(base64.b64decode(SUNSYNK_TEST_PUBLIC_KEY))
    if modulus != SUNSYNK_TEST_MODULUS:
        print(f"ERROR: modulus mismatch, got {hex(modulus)}")
        failed = True
    if exponent != 65537:
        print(f"ERROR: exponent expected 65537, got {exponent}")
        failed = True
    assert not failed, "test_parse_rsa_public_key"


def test_parse_rsa_public_key_rejects_rubbish():
    """A body that is not a DER key raises rather than returning a bogus modulus."""
    failed = False
    for name, payload in (("empty", b""), ("not a sequence", b"\x02\x01\x05"), ("truncated", base64.b64decode(SUNSYNK_TEST_PUBLIC_KEY)[:20])):
        try:
            parse_rsa_public_key(payload)
            print(f"ERROR: {name} was accepted as a public key")
            failed = True
        except (ValueError, IndexError):
            pass
    assert not failed, "test_parse_rsa_public_key_rejects_rubbish"


def test_rsa_encrypt_round_trip():
    """Encryption produces a well-formed PKCS#1 v1.5 type-2 block recovering the password."""
    failed = False
    for password in ("hunter2", "a", "x" * 117, "pa55 w0rd! £é"):
        block = _decrypt(rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, password))
        if block[0] != 0x00 or block[1] != 0x02:
            print(f"ERROR: {password!r} block header {block[:2].hex()} is not 0002")
            failed = True
            continue
        separator = block.index(b"\x00", 2)
        padding = block[2:separator]
        if len(padding) < 8:
            print(f"ERROR: {password!r} padding only {len(padding)} bytes, PKCS#1 requires >= 8")
            failed = True
        if not all(padding):
            print(f"ERROR: {password!r} padding contains a zero byte, which truncates the message")
            failed = True
        recovered = block[separator + 1 :].decode("utf-8")
        if recovered != password:
            print(f"ERROR: {password!r} round-tripped to {recovered!r}")
            failed = True
    assert not failed, "test_rsa_encrypt_round_trip"


def test_rsa_encrypt_is_randomised():
    """The same password encrypts differently each time, as PKCS#1 v1.5 requires."""
    failed = False
    first = rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "hunter2")
    second = rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "hunter2")
    if first == second:
        print("ERROR: identical ciphertexts, padding is not randomised")
        failed = True
    if len(base64.b64decode(first)) != SUNSYNK_TEST_KEY_BYTES:
        print(f"ERROR: ciphertext is {len(base64.b64decode(first))} bytes, expected {SUNSYNK_TEST_KEY_BYTES}")
        failed = True
    assert not failed, "test_rsa_encrypt_is_randomised"


def test_rsa_encrypt_rejects_oversize_password():
    """A password too long for the key is refused rather than silently truncated."""
    failed = False
    try:
        rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "x" * 118)
        print("ERROR: oversize password was accepted")
        failed = True
    except ValueError:
        pass
    assert not failed, "test_rsa_encrypt_rejects_oversize_password"


def run_sunsynk_auth_tests(my_predbat):
    """Run all Sunsynk authentication tests."""
    failed = False
    for name, fn in [
        ("parse_public_key", test_parse_rsa_public_key),
        ("parse_rejects_rubbish", test_parse_rsa_public_key_rejects_rubbish),
        ("encrypt_round_trip", test_rsa_encrypt_round_trip),
        ("encrypt_randomised", test_rsa_encrypt_is_randomised),
        ("encrypt_oversize", test_rsa_encrypt_rejects_oversize_password),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_auth.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_auth.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register the suite**

In `apps/predbat/unit_test.py`, add:

```python
from tests.test_sunsynk_auth import run_sunsynk_auth_tests
```

and, after the `sunsynk_const` entry:

```python
        ("sunsynk_auth", run_sunsynk_auth_tests, "Sunsynk auth tests", False),
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd coverage && ./run_all --test sunsynk_auth > /tmp/predbat_sunsynk_auth.log 2>&1; grep -iE "error|cannot import|exception" /tmp/predbat_sunsynk_auth.log | head
```

Expected: `ImportError: cannot import name 'parse_rsa_public_key' from 'sunsynk_const'`.

- [ ] **Step 4: Append the RSA helper to the constants module**

Add to the top of `apps/predbat/sunsynk_const.py` imports:

```python
import base64
import os
```

and append to the end of the file. This implementation has been verified to parse the DER above, round-trip all four passwords, reject an oversize password, and randomise its padding:

```python
def _read_tlv(data, offset):
    """Read one DER tag-length-value at offset, returning (tag, value, next_offset)."""
    tag = data[offset]
    offset += 1
    length = data[offset]
    offset += 1
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    return tag, data[offset : offset + length], offset + length


def parse_rsa_public_key(der_bytes):
    """Extract (modulus, exponent) from a DER SubjectPublicKeyInfo RSA public key.

    Sunsynk's /anonymous/publicKey returns the key base64-encoded with no PEM armour,
    so it is decoded and walked directly: SEQUENCE { AlgorithmIdentifier, BIT STRING {
    RSAPublicKey SEQUENCE { INTEGER modulus, INTEGER exponent } } }. Raises ValueError
    on anything that is not that structure rather than returning a bogus key — a
    silently wrong modulus would encrypt the password to something unrecoverable.
    """
    if not der_bytes:
        raise ValueError("Sunsynk public key response was empty")
    tag, spki, _ = _read_tlv(der_bytes, 0)
    if tag != 0x30:
        raise ValueError("Sunsynk public key is not a DER SEQUENCE")
    tag, _algorithm, offset = _read_tlv(spki, 0)
    if tag != 0x30:
        raise ValueError("Sunsynk public key has no AlgorithmIdentifier")
    tag, bit_string, _ = _read_tlv(spki, offset)
    if tag != 0x03:
        raise ValueError("Sunsynk public key has no BIT STRING")
    # The BIT STRING's first byte counts unused trailing bits and is always 0 here.
    tag, rsa_key, _ = _read_tlv(bit_string[1:], 0)
    if tag != 0x30:
        raise ValueError("Sunsynk public key BIT STRING is not an RSAPublicKey")
    tag, modulus, offset = _read_tlv(rsa_key, 0)
    if tag != 0x02:
        raise ValueError("Sunsynk public key has no modulus")
    tag, exponent, _ = _read_tlv(rsa_key, offset)
    if tag != 0x02:
        raise ValueError("Sunsynk public key has no exponent")
    return int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


def rsa_encrypt_pkcs1v15(public_key_b64, plaintext):
    """RSA-encrypt plaintext with PKCS#1 v1.5 type-2 padding, returning base64 ciphertext.

    This replaces a `cryptography` dependency, which would add a Rust-built binary wheel
    to every architecture the add-on targets. Only public-key encryption happens here —
    there is no private key and no secret-dependent branch, so the usual cautions about
    hand-rolled crypto (timing side channels, padding oracles) do not apply.
    """
    modulus, exponent = parse_rsa_public_key(base64.b64decode(public_key_b64))
    size = (modulus.bit_length() + 7) // 8
    message = plaintext.encode("utf-8")
    # PKCS#1 v1.5 needs 3 framing bytes and at least 8 padding bytes.
    if len(message) > size - 11:
        raise ValueError(f"Sunsynk password is too long for a {size * 8} bit key")
    needed = size - len(message) - 3
    padding = bytearray()
    # Padding must be non-zero: a zero byte would be read as the message separator and
    # truncate the password. Rejection-sample until enough non-zero bytes are collected.
    while len(padding) < needed:
        padding.extend(byte for byte in os.urandom(needed) if byte)
    block = b"\x00\x02" + bytes(padding[:needed]) + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), exponent, modulus)
    return base64.b64encode(cipher.to_bytes(size, "big")).decode("ascii")
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd coverage && ./run_all --test sunsynk_auth > /tmp/predbat_sunsynk_auth.log 2>&1; tail -20 /tmp/predbat_sunsynk_auth.log
```

Expected: all five tests pass.

- [ ] **Step 6: Commit**

```bash
./run_pre_commit
git add apps/predbat/sunsynk_const.py apps/predbat/tests/test_sunsynk_auth.py apps/predbat/unit_test.py
git commit -m "feat(sunsynk): add pure-Python RSA PKCS#1 v1.5 login helper

Sunsynk RSA-encrypts the account password at login. Implemented directly
rather than adding a cryptography dependency, which would put a Rust-built
wheel on the armv7/armhf add-on targets. Public-key encryption only.

Tested against a fixed 1024-bit key: DER parsing, padding structure,
round-trip recovery, randomisation, and oversize rejection."
```

---

**Remaining tasks 3-9** cover the component itself and are specified in the sections below. Task 1 and 2 are self-contained and can be executed immediately; each later task consumes only the interfaces its predecessors declare.

---

### Task 3: Component skeleton, transport and the three auth flows

**Files:**

- Create: `apps/predbat/sunsynk.py`
- Create: `apps/predbat/tests/test_sunsynk_api.py`
- Modify: `apps/predbat/tests/test_sunsynk_auth.py` (add the auth-flow tests)
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: everything from Tasks 1 and 2.
- Produces: `SunsynkAPI` with `initialize(...)`, properties `base_url` / `source`, `_auth_headers()`, `debug_api(direction, what, payload=None)`, `is_auth_error_body(data)` (static), `async fetch_public_key()`, `async fetch_token()`, `async _request(method, endpoint_key, sn=None, params=None, body=None)`, `async _get(endpoint_key, sn=None, params=None)`, `async _post(endpoint_key, sn=None, body=None)`. All request helpers return the response's `data` dict, or `{}` on failure. Also produces the `MockSunsynk` test double used by every later test module.

- [ ] **Step 1: Write the failing transport test**

Create `apps/predbat/tests/test_sunsynk_api.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk Cloud API component (``sunsynk.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from sunsynk import SunsynkAPI
from sunsynk_const import SUNSYNK_REGIONS, SUNSYNK_ENDPOINTS
from tests.test_infra import run_async as run_async_local


class MockSunsynk(SunsynkAPI):
    """Test double: build a SunsynkAPI without the full component lifecycle."""

    def __init__(self, auth_method="password", region="sunsynk", inverter_sn=None, control_enable=True):
        """Set up a minimal SunsynkAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.automatic = False
        self.automatic_ignore_pv = False
        self.region = region
        self.username = "test@example.com"
        self.password = "hunter2"
        self.auth_method = auth_method
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn or []
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_capacity = {}
        self.device_pack_voltage = {}
        self.device_energy = {}
        self.device_rated_power = {}
        self.battery_nominal_voltage = 0.0
        self.local_schedule = {}
        self.applied_payload = {}
        self.settle_count = {}
        self.control_active = set()
        self.cached_values = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        self._soc_floor_warned = set()
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-sunsynk-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.minutes_now = 0  # local minutes-since-midnight; tests set this for time-aware control
        # Straight through, like the component: _init_oauth owns self.auth_method, so
        # collapsing the modes here would hide password_legacy from fetch_token.
        self._init_oauth(auth_method, "test-token", None, "sunsynk")

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass


def test_sunsynk_base_url_and_source():
    """base_url and source resolve from the configured region."""
    failed = False
    for region in ("sunsynk", "inteless"):
        s = MockSunsynk(region=region)
        if s.base_url != SUNSYNK_REGIONS[region]["host"]:
            print(f"ERROR: {region} base_url {s.base_url}")
            failed = True
        if s.source != SUNSYNK_REGIONS[region]["source"]:
            print(f"ERROR: {region} source {s.source}")
            failed = True
    # An unknown region must fall back to the default rather than crash on startup.
    s = MockSunsynk(region="nonsense")
    if s.base_url != SUNSYNK_REGIONS["sunsynk"]["host"]:
        print(f"ERROR: unknown region did not fall back, got {s.base_url}")
        failed = True
    assert not failed, "test_sunsynk_base_url_and_source"


def test_is_auth_error_body():
    """Body-level auth failures are detected; genuine non-auth failures are not."""
    failed = False
    auth_bodies = [
        {"success": False, "msg": "Invalid Token"},
        {"success": False, "msg": "token expired"},
        {"success": False, "msg": "Unauthorized"},
    ]
    other_bodies = [
        {"success": True, "msg": "Success"},
        {"success": False, "msg": "inverter offline"},
        {"success": False, "msg": "parameter error"},
        {},
    ]
    for body in auth_bodies:
        if not SunsynkAPI.is_auth_error_body(body):
            print(f"ERROR: {body} not detected as an auth error")
            failed = True
    for body in other_bodies:
        if SunsynkAPI.is_auth_error_body(body):
            print(f"ERROR: {body} wrongly detected as an auth error")
            failed = True
    assert not failed, "test_is_auth_error_body"


def test_request_returns_data_on_success():
    """A successful envelope yields its data dict."""
    failed = False
    s = MockSunsynk()

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Fake transport returning a successful battery payload."""
        return {"soc": 55, "power": -1200}

    with patch.object(s, "_request", side_effect=fake_request):
        out = run_async_local(s._get("battery", sn="INV1"))
    if out.get("soc") != 55:
        print(f"ERROR: expected soc 55, got {out}")
        failed = True
    assert not failed, "test_request_returns_data_on_success"


def test_request_returns_empty_on_failure():
    """A failed envelope yields {} so callers fail closed rather than act on junk."""
    failed = False
    s = MockSunsynk()

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Fake transport returning a failure."""
        return {}

    with patch.object(s, "_request", side_effect=fake_request):
        out = run_async_local(s._get("battery", sn="INV1"))
    if out != {}:
        print(f"ERROR: expected {{}} on failure, got {out}")
        failed = True
    assert not failed, "test_request_returns_empty_on_failure"


def test_endpoint_paths_render_serial():
    """Templated endpoints render the serial into the path."""
    failed = False
    for endpoint in ("battery", "grid", "load", "input", "settings_read", "settings_set", "inverter_detail"):
        path = SUNSYNK_ENDPOINTS[endpoint].format(sn="ABC123")
        if "ABC123" not in path or "{sn}" in path:
            print(f"ERROR: endpoint {endpoint} rendered as {path}")
            failed = True
    assert not failed, "test_endpoint_paths_render_serial"


def run_sunsynk_api_tests(my_predbat):
    """Run all Sunsynk API tests."""
    failed = False
    for name, fn in [
        ("base_url_and_source", test_sunsynk_base_url_and_source),
        ("is_auth_error_body", test_is_auth_error_body),
        ("request_success", test_request_returns_data_on_success),
        ("request_failure", test_request_returns_empty_on_failure),
        ("endpoint_paths", test_endpoint_paths_render_serial),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Add the auth-flow tests to `test_sunsynk_auth.py`**

Append these to `apps/predbat/tests/test_sunsynk_auth.py`, and add `from tests.test_sunsynk_api import MockSunsynk` plus `from tests.test_infra import run_async as run_async_local` and `from unittest.mock import patch` to its imports:

```python
def test_password_login_uses_rsa_and_signs():
    """The default method fetches a public key, encrypts the password and signs both calls."""
    failed = False
    s = MockSunsynk(auth_method="password")
    seen = {}

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Record each auth call and return a plausible Sunsynk response."""
        seen[endpoint_key] = {"method": method, "params": params, "body": body}
        if endpoint_key == "public_key":
            return SUNSYNK_TEST_PUBLIC_KEY
        return {"access_token": "tok-abc", "refresh_token": "ref-abc", "expires_in": 3600}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if not ok:
        print("ERROR: fetch_token returned False")
        failed = True
    if "public_key" not in seen:
        print("ERROR: the public key endpoint was never called")
        failed = True
    else:
        params = seen["public_key"].get("params") or {}
        for key in ("nonce", "source", "sign"):
            if key not in params:
                print(f"ERROR: public key request missing {key}")
                failed = True
    token_body = (seen.get("token") or {}).get("body") or {}
    if token_body.get("password") == s.password:
        print("ERROR: the plaintext password was sent on the RSA path")
        failed = True
    if not token_body.get("password"):
        print("ERROR: no encrypted password in the token request")
        failed = True
    for key in ("nonce", "sign", "source", "client_id", "grant_type", "username"):
        if key not in token_body:
            print(f"ERROR: token request missing {key}")
            failed = True
    if s.access_token != "tok-abc":
        print(f"ERROR: access token not stored, got {s.access_token!r}")
        failed = True
    assert not failed, "test_password_login_uses_rsa_and_signs"


def test_legacy_login_sends_plaintext_and_skips_public_key():
    """password_legacy posts once, with the plaintext password and no public-key call."""
    failed = False
    s = MockSunsynk(auth_method="password_legacy")
    seen = {}

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Record each auth call and return a plausible Sunsynk response."""
        seen[endpoint_key] = {"method": method, "params": params, "body": body}
        return {"access_token": "tok-legacy", "refresh_token": "ref-legacy", "expires_in": 3600}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if not ok:
        print("ERROR: legacy fetch_token returned False")
        failed = True
    if "public_key" in seen:
        print("ERROR: the legacy path fetched a public key")
        failed = True
    if "token_legacy" not in seen:
        print("ERROR: the legacy token endpoint was not called")
        failed = True
    body = (seen.get("token_legacy") or {}).get("body") or {}
    if body.get("password") != "hunter2":
        print(f"ERROR: legacy password should be plaintext, got {body.get('password')!r}")
        failed = True
    if body.get("areaCode") != "sunsynk":
        print(f"ERROR: legacy request missing areaCode, got {body.get('areaCode')!r}")
        failed = True
    if s.access_token != "tok-legacy":
        print(f"ERROR: access token not stored, got {s.access_token!r}")
        failed = True
    assert not failed, "test_legacy_login_sends_plaintext_and_skips_public_key"


def test_rsa_login_never_falls_back_to_plaintext():
    """A failing public-key step must not downgrade to sending the plaintext password.

    Auto-downgrade would turn any externally-triggerable failure of the public-key call
    into a plaintext credential transmission. TLS-intercepting middleboxes are common,
    and against one of those the RSA layer is the only thing protecting the password.
    """
    failed = False
    s = MockSunsynk(auth_method="password")
    seen = []

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Fail the public key call, and record anything sent afterwards."""
        seen.append((endpoint_key, body))
        if endpoint_key == "public_key":
            return {}
        return {"access_token": "tok-should-not-happen", "expires_in": 3600}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if ok:
        print("ERROR: fetch_token reported success after the public key call failed")
        failed = True
    for endpoint_key, body in seen:
        if endpoint_key == "token_legacy":
            print("ERROR: the RSA path fell back to the legacy endpoint")
            failed = True
        if body and body.get("password") == "hunter2":
            print(f"ERROR: plaintext password sent to {endpoint_key}")
            failed = True
    if not any("legacy" in str(m).lower() for m in s.log_messages):
        print("ERROR: no diagnostic pointing the user at password_legacy")
        failed = True
    assert not failed, "test_rsa_login_never_falls_back_to_plaintext"


def test_oauth_method_skips_login_entirely():
    """The Predbat.com path uses the injected token and never calls a login endpoint."""
    failed = False
    s = MockSunsynk(auth_method="oauth")
    seen = []

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Record any call, which for this method should never happen."""
        seen.append(endpoint_key)
        return {}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if not ok:
        print("ERROR: oauth fetch_token should succeed with an injected token")
        failed = True
    if seen:
        print(f"ERROR: oauth path called login endpoints: {seen}")
        failed = True
    if s.access_token != "test-token":
        print(f"ERROR: injected token not used, got {s.access_token!r}")
        failed = True
    assert not failed, "test_oauth_method_skips_login_entirely"


def test_debug_trace_redacts_credentials():
    """Debug tracing never writes a password or bearer token to the log."""
    failed = False
    s = MockSunsynk()
    s.debug_api("POST", "token", {"username": "test@example.com", "password": "hunter2", "sign": "abc123", "access_token": "tok-abc"})
    joined = " ".join(str(m) for m in s.log_messages)
    for secret in ("hunter2", "tok-abc", "abc123"):
        if secret in joined:
            print(f"ERROR: {secret!r} leaked into the debug log")
            failed = True
    if "test@example.com" not in joined:
        print("ERROR: non-secret fields should still be traced")
        failed = True
    assert not failed, "test_debug_trace_redacts_credentials"
```

Extend that module's `run_sunsynk_auth_tests` list with:

```python
        ("password_login_rsa", test_password_login_uses_rsa_and_signs),
        ("legacy_login_plaintext", test_legacy_login_sends_plaintext_and_skips_public_key),
        ("no_plaintext_fallback", test_rsa_login_never_falls_back_to_plaintext),
        ("oauth_skips_login", test_oauth_method_skips_login_entirely),
        ("debug_redaction", test_debug_trace_redacts_credentials),
```

- [ ] **Step 3: Register the API suite and run both to verify they fail**

In `apps/predbat/unit_test.py` add `from tests.test_sunsynk_api import run_sunsynk_api_tests` and the entry `("sunsynk_api", run_sunsynk_api_tests, "Sunsynk API tests", False),`. Then:

```bash
cd coverage && ./run_all --test sunsynk_api --test sunsynk_auth > /tmp/predbat_sunsynk_t3.log 2>&1; grep -iE "no module|cannot import|error" /tmp/predbat_sunsynk_t3.log | head
```

Expected: `ModuleNotFoundError: No module named 'sunsynk'`.

- [ ] **Step 4: Write the component skeleton**

Create `apps/predbat/sunsynk.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sunsynk Cloud API Library
# -----------------------------------------------------------------------------

"""Sunsynk Connect cloud API integration for Predbat.

Registers each discovered Sunsynk inverter as a ``SunsynkCloud`` Predbat inverter,
publishing monitoring sensors and DEYE-style schedule control entities. Predbat drives
those entities through the generic Inverter class; this module derives the Sunsynk work
mode internally and applies it by read-modify-write of the whole settings object.

Three auth methods: ``password`` (RSA-encrypted login, the default), ``password_legacy``
(the pre-2025 plaintext login, opt-in) and ``oauth`` (token injected by Predbat.com).
The RSA path never downgrades to the plaintext one — see ``fetch_token``.
"""

import asyncio
import hashlib
import json
import time
import aiohttp
from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from sunsynk_const import (
    SUNSYNK_REGIONS,
    SUNSYNK_ENDPOINTS,
    SUNSYNK_TIMEOUT,
    SUNSYNK_RETRIES,
    SUNSYNK_CLIENT_ID,
    SUNSYNK_AUTH_ERROR_MARKERS,
    SUNSYNK_DEBUG_MAX_CHARS,
    SUNSYNK_DEBUG_REDACT_KEYS,
    rsa_encrypt_pkcs1v15,
)


class SunsynkAPI(ComponentBase, OAuthMixin):
    """Sunsynk Connect cloud API component."""

    # Trace every API request/response while the Sunsynk integration beds in. Nobody on
    # the project has a Sunsynk account, so a tester's log is the only evidence available
    # for the inferred wire format; flip to False once the format is confirmed.
    api_debug = True

    def initialize(
        self,
        username="",
        password="",
        key="",
        region="sunsynk",
        auth_method="password",
        token_expires_at=None,
        token_hash="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=False,
        battery_nominal_voltage=None,
        **kwargs,
    ):
        """Initialise the Sunsynk component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has
        already resolved each arg from its sunsynk_* config key and passes it BY ARG NAME
        (e.g. region <- sunsynk_region), exactly like fox/deye/enphase. Consume the kwargs
        directly — do NOT re-derive with get_arg("region"): that bare name is not in
        apps.yaml (the key is sunsynk_region), so it would always return the default.
        """
        self.log("Info: SunsynkAPI initialising")
        self.username = username
        self.password = password
        self.region = region or "sunsynk"
        self.auth_method = auth_method or "password"
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_nominal_voltage = float(battery_nominal_voltage) if battery_nominal_voltage else 0.0
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_capacity = {}
        self.device_pack_voltage = {}
        self.device_energy = {}
        self.device_rated_power = {}
        self.local_schedule = {}
        self.applied_payload = {}
        self.settle_count = {}
        self.control_active = set()
        self.cached_values = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        self._soc_floor_warned = set()
        if self.region not in SUNSYNK_REGIONS:
            self.log(f"Warn: Sunsynk unknown region '{self.region}', falling back to sunsynk")
            self.region = "sunsynk"
        if self.auth_method == "password_legacy":
            self.log("Warn: Sunsynk auth_method 'password_legacy' sends your password in plaintext over TLS. It exists for regions still on the pre-2025 login; prefer 'password' where it works.")
        if not self.control_enable:
            self.log("Info: Sunsynk control is disabled (sunsynk_control_enable is false); monitoring only. The write format has not been confirmed against live hardware.")
        # Pass auth_method STRAIGHT THROUGH, exactly as deye.py does. _init_oauth owns
        # self.auth_method (`self.auth_method = auth_method or "api_key"`, oauth_mixin.py),
        # so collapsing the three modes to two here would rewrite "password_legacy" to
        # "password" and make the plaintext login silently unreachable — fetch_token would
        # take the RSA branch for a user who deliberately asked for the legacy one.
        # _init_oauth leaves access_token None for every non-oauth value, which is right
        # for both self-hosted modes: they obtain their token in fetch_token.
        self._init_oauth(auth_method=self.auth_method, key=key, token_expires_at=token_expires_at, provider_name="sunsynk")
        # _init_oauth resets token_hash to "" (see oauth_mixin.py), so a configured value
        # must be applied AFTER it, exactly as fox.py and deye.py do — otherwise the
        # Predbat.com SaaS refresh dedup keyed on it breaks.
        self.token_hash = token_hash

    @property
    def base_url(self):
        """Return the API host for the configured region."""
        return SUNSYNK_REGIONS.get(self.region, SUNSYNK_REGIONS["sunsynk"])["host"]

    @property
    def source(self):
        """Return the 'source' token this region's login signs with."""
        return SUNSYNK_REGIONS.get(self.region, SUNSYNK_REGIONS["sunsynk"])["source"]

    def _auth_headers(self):
        """Return the bearer headers used for every authenticated call."""
        return {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.access_token}"}

    @staticmethod
    def redact(payload):
        """Return payload with credential-bearing keys replaced, for safe logging."""
        if not isinstance(payload, dict):
            return payload
        return {key: ("<redacted>" if key in SUNSYNK_DEBUG_REDACT_KEYS else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response when api_debug is on, with secrets redacted."""
        if not self.api_debug:
            return
        if payload is None:
            self.log(f"Debug: Sunsynk {direction} {what}")
            return
        text = json.dumps(self.redact(payload), default=str)
        if len(text) > SUNSYNK_DEBUG_MAX_CHARS:
            text = text[:SUNSYNK_DEBUG_MAX_CHARS] + f"... (truncated, {len(text)} chars)"
        self.log(f"Debug: Sunsynk {direction} {what} {text}")

    @staticmethod
    def is_auth_error_body(data):
        """Return True if a 200-with-failure body means the token needs refreshing.

        Sunsynk does not answer an expired token with HTTP 401 — it returns HTTP 200
        carrying {"success": false, "msg": "..."}. Status-code-only handling therefore
        never triggers a refresh and the component stays broken until a restart.
        """
        if not isinstance(data, dict) or data.get("success"):
            return False
        message = str(data.get("msg", "")).lower()
        return any(marker in message for marker in SUNSYNK_AUTH_ERROR_MARKERS)

    @staticmethod
    def _nonce():
        """Return the millisecond nonce Sunsynk's login signs over."""
        return int(time.time() * 1000)

    def _sign(self, nonce, suffix):
        """Return the md5 signature Sunsynk's login expects for a nonce and suffix."""
        return hashlib.md5(f"nonce={nonce}&source={self.source}{suffix}".encode("utf-8")).hexdigest()

    async def fetch_public_key(self):
        """Fetch the RSA public key the password is encrypted with. Returns "" on failure."""
        nonce = self._nonce()
        params = {"source": self.source, "nonce": str(nonce), "sign": self._sign(nonce, "POWER_VIEW")}
        data = await self._request("GET", "public_key", params=params)
        # This endpoint returns the key as a bare string in `data`, not a dict.
        return data if isinstance(data, str) else ""

    async def fetch_token(self):
        """Log in by the configured method and store the access token. Returns True on success.

        The RSA path NEVER falls back to the plaintext one. Auto-downgrade would convert
        any externally-triggerable failure of the public-key step into a plaintext
        credential transmission, and against a TLS-intercepting middlebox the RSA layer is
        the only thing protecting the password. A failure logs a diagnostic pointing at
        sunsynk_auth_method: password_legacy so the user can choose it deliberately.
        """
        if self.auth_method == "oauth":
            # OAuthMixin already holds the injected token; nothing to log in with.
            return bool(self.access_token)

        if self.auth_method == "password_legacy":
            body = {"areaCode": "sunsynk", "client_id": SUNSYNK_CLIENT_ID, "grant_type": "password", "password": self.password, "source": self.source, "username": self.username}
            data = await self._request("POST", "token_legacy", body=body)
        else:
            public_key = await self.fetch_public_key()
            if not public_key:
                self.log("Warn: Sunsynk could not fetch the login public key, so the RSA login cannot proceed. It is NOT retried in plaintext. If your region still serves the older login, set sunsynk_auth_method: password_legacy in apps.yaml.")
                return False
            try:
                encrypted = rsa_encrypt_pkcs1v15(public_key, self.password)
            except ValueError as error:
                self.log(f"Warn: Sunsynk could not encrypt the password for login: {error}")
                return False
            nonce = self._nonce()
            body = {
                "client_id": SUNSYNK_CLIENT_ID,
                "grant_type": "password",
                "password": encrypted,
                "source": self.source,
                "username": self.username,
                "nonce": nonce,
                "sign": self._sign(nonce, public_key[:10]),
            }
            data = await self._request("POST", "token", body=body)

        token = (data or {}).get("access_token")
        if not token:
            self.log("Warn: Sunsynk login did not return an access token; check the username and password")
            return False
        self.access_token = token
        self.refresh_token = data.get("refresh_token", "")
        expires_in = data.get("expires_in")
        if expires_in:
            self.token_expires_at = time.time() + float(expires_in)
        self.log("Info: Sunsynk login succeeded")
        return True

    async def _request(self, method, endpoint_key, sn=None, params=None, body=None):
        """Perform one API call, returning the response's `data` payload, or None on failure.

        Retries transport errors and non-200 responses with backoff, and treats a
        body-level auth failure as a token refresh followed by exactly one retry.

        Failure is None, NOT {} — a successful call can legitimately carry no data (the
        settings write is one), so {} would be ambiguous between "worked, nothing to
        return" and "failed", and a write failure would be silently read as success.
        Read callers go through _get, which coerces None to {} because they all want a
        dict; the write path uses the None directly to detect failure. Never raises, so
        every caller fails closed.
        """
        path = SUNSYNK_ENDPOINTS[endpoint_key]
        if sn:
            path = path.format(sn=sn)
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=SUNSYNK_TIMEOUT)
        # Login endpoints are unauthenticated; everything else carries the bearer token.
        anonymous = endpoint_key in ("public_key", "token", "token_legacy")
        headers = {"Accept": "application/json", "Content-Type": "application/json"} if anonymous else self._auth_headers()
        refreshed = False

        for attempt in range(SUNSYNK_RETRIES):
            self.debug_api(method, url, body if body is not None else params)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers, params=params, json=body) as response:
                        if response.status != 200:
                            self.log(f"Warn: Sunsynk {method} {path} returned HTTP {response.status}")
                            await asyncio.sleep(2**attempt)
                            continue
                        payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                self.log(f"Warn: Sunsynk {method} {path} failed: {error}")
                await asyncio.sleep(2**attempt)
                continue

            self.debug_api("<-", url, payload if isinstance(payload, dict) else {"data": payload})
            if self.is_auth_error_body(payload) and not anonymous and not refreshed:
                refreshed = True
                self.log(f"Info: Sunsynk {path} reported an auth failure, refreshing the token")
                if await self.fetch_token():
                    headers = self._auth_headers()
                    continue
                return None
            if not isinstance(payload, dict) or not payload.get("success"):
                message = payload.get("msg") if isinstance(payload, dict) else payload
                self.log(f"Warn: Sunsynk {method} {path} was unsuccessful: {message}")
                return None
            data = payload.get("data")
            return data if data is not None else {}

        return None

    async def _get(self, endpoint_key, sn=None, params=None):
        """GET one endpoint, returning its `data` payload, or {} on failure.

        Read callers all want a dict and treat an empty one as "nothing usable", so the
        None that _request reports on failure is coerced here.
        """
        result = await self._request("GET", endpoint_key, sn=sn, params=params)
        return result if result is not None else {}

    async def _post(self, endpoint_key, sn=None, body=None):
        """POST one endpoint, returning its `data` payload, or None on failure.

        None is passed straight through, unlike _get: the settings write succeeds with no
        data payload, so only None can distinguish a failed write from a successful one.
        """
        return await self._request("POST", endpoint_key, sn=sn, body=body)
```

- [ ] **Step 5: Run both suites to verify they pass**

```bash
cd coverage && ./run_all --test sunsynk_api --test sunsynk_auth > /tmp/predbat_sunsynk_t3.log 2>&1; tail -25 /tmp/predbat_sunsynk_t3.log
```

Expected: all tests in both suites pass.

- [ ] **Step 6: Commit**

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_api.py apps/predbat/tests/test_sunsynk_auth.py apps/predbat/unit_test.py
git commit -m "feat(sunsynk): add component skeleton, transport and the three auth flows

RSA login (default), the pre-2025 plaintext login (opt-in) and the
Predbat.com injected-token path. Transport retries with backoff, detects
Sunsynk's 200-with-failure auth errors from the body, and returns {} on
any failure so callers fail closed.

The RSA path never downgrades to plaintext: that would turn an
externally-triggerable public-key failure into a credential leak."
```

---

### Task 4: Discovery, telemetry and derived ratings

**Files:**

- Modify: `apps/predbat/sunsynk.py`
- Modify: `apps/predbat/tests/test_sunsynk_api.py`

**Interfaces:**

- Consumes: `_get`, `_post` from Task 3; `SUNSYNK_TELEMETRY`, `SUNSYNK_ENERGY`, `SUNSYNK_PAGE_SIZE` and the ratings field names from Task 1.
- Produces: `async get_device_list()` returning a list of serial strings; `async fetch_device_detail(sn)`; `async fetch_device_data(sn)` populating `self.device_values[sn]` and `self.device_energy[sn]`; `async fetch_settings(sn)` populating `self.device_settings[sn]`; `nominal_pack_voltage(charge_volts)`; `battery_capacity(sn)` in kWh; `battery_rate_max(sn)` in watts; `battery_reserve_min(sn)` as a percent.

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_sunsynk_api.py`:

```python
def test_get_device_list_pages_and_filters():
    """Discovery pages through /inverters and honours the serial filter."""
    failed = False
    s = MockSunsynk(inverter_sn=["INV1"])
    calls = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return two pages of inverters, one of which is filtered out."""
        calls.append((endpoint_key, dict(params or {})))
        if endpoint_key != "inverter_list":
            return {}
        page = int((params or {}).get("page", 1))
        if page == 1:
            return {"total": 3, "infos": [{"sn": "INV1"}, {"sn": "INV2"}]}
        return {"total": 3, "infos": [{"sn": "INV3"}]}

    with patch.object(s, "_get", side_effect=fake_get):
        devices = run_async_local(s.get_device_list())
    if devices != ["INV1"]:
        print(f"ERROR: expected ['INV1'] after filtering, got {devices}")
        failed = True
    if len(calls) < 2:
        print(f"ERROR: expected pagination, only {len(calls)} call(s) made")
        failed = True
    assert not failed, "test_get_device_list_pages_and_filters"


def test_get_device_list_unfiltered_returns_all():
    """With no filter, every discovered serial is registered."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a single page of two inverters."""
        return {"total": 2, "infos": [{"sn": "INV1"}, {"sn": "INV2"}]}

    with patch.object(s, "_get", side_effect=fake_get):
        devices = run_async_local(s.get_device_list())
    if devices != ["INV1", "INV2"]:
        print(f"ERROR: expected both serials, got {devices}")
        failed = True
    assert not failed, "test_get_device_list_unfiltered_returns_all"


def test_fetch_device_data_maps_telemetry_and_energy():
    """Telemetry from four endpoints is flattened onto the Predbat sensor leaves."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a realistic payload for each realtime endpoint."""
        if endpoint_key == "battery":
            return {"soc": 62, "power": -1500, "voltage": 51.2, "temp": 21.5, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100, "etodayChg": 7.4, "etodayDischg": 5.1}
        if endpoint_key == "grid":
            return {"pac": 430, "etodayFrom": 3.2, "etodayTo": 1.1}
        if endpoint_key == "load":
            return {"totalPower": 900, "dailyUsed": 12.6}
        if endpoint_key == "input":
            return {"pac": 2100, "etoday": 9.8}
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.fetch_device_data("INV1"))
    values = s.device_values.get("INV1", {})
    for leaf, expect in (("soc", 62), ("battery_power", -1500), ("grid_power", 430), ("load_power", 900), ("pv_power", 2100), ("temperature", 21.5)):
        if values.get(leaf) != expect:
            print(f"ERROR: telemetry {leaf} = {values.get(leaf)}, expected {expect}")
            failed = True
    energy = s.device_energy.get("INV1", {})
    for leaf, expect in (("pv_today", 9.8), ("import_today", 3.2), ("export_today", 1.1), ("load_today", 12.6), ("battery_charge_today", 7.4), ("battery_discharge_today", 5.1)):
        if energy.get(leaf) != expect:
            print(f"ERROR: energy {leaf} = {energy.get(leaf)}, expected {expect}")
            failed = True
    assert not failed, "test_fetch_device_data_maps_telemetry_and_energy"


def test_fetch_device_data_absent_fields_are_not_invented():
    """A model that omits a counter leaves it absent rather than publishing a zero."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a battery payload with no energy counters at all."""
        if endpoint_key == "battery":
            return {"soc": 50, "power": 0}
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.fetch_device_data("INV1"))
    energy = s.device_energy.get("INV1", {})
    for leaf in ("battery_charge_today", "pv_today", "import_today"):
        if leaf in energy:
            print(f"ERROR: {leaf} was invented as {energy[leaf]} when the API omitted it")
            failed = True
    if s.device_values.get("INV1", {}).get("soc") != 50:
        print("ERROR: present fields should still be mapped")
        failed = True
    assert not failed, "test_fetch_device_data_absent_fields_are_not_invented"


def test_nominal_pack_voltage_variants():
    """Pack voltage is inferred from the BMS charge target across common stack sizes."""
    failed = False
    s = MockSunsynk()
    # 16 cells -> 51.2V nominal, 24 -> 76.8V, 32 -> 102.4V.
    for charge_volts, expect in ((56.8, 51.2), (85.2, 76.8), (113.6, 102.4)):
        got = s.nominal_pack_voltage(charge_volts)
        if abs(got - expect) > 0.5:
            print(f"ERROR: chargeVolt {charge_volts} gave {got}V nominal, expected about {expect}V")
            failed = True
    # No charge target and no override means no guess.
    if s.nominal_pack_voltage(0) != 0:
        print("ERROR: a missing charge target must not produce a guessed voltage")
        failed = True
    # The explicit override wins.
    s.battery_nominal_voltage = 48.0
    if s.nominal_pack_voltage(0) != 48.0:
        print("ERROR: sunsynk_battery_nominal_voltage override was ignored")
        failed = True
    assert not failed, "test_nominal_pack_voltage_variants"


def test_battery_capacity_amp_hours_to_kwh():
    """Amp-hour capacity becomes kWh using the inferred pack voltage."""
    failed = False
    s = MockSunsynk()
    s.device_values["INV1"] = {"capacity": 280, "chargeVolt": 56.8}
    # 280Ah at 51.2V nominal = 14.336 kWh.
    capacity = s.battery_capacity("INV1")
    if abs(capacity - 14.336) > 0.05:
        print(f"ERROR: capacity {capacity} kWh, expected about 14.34")
        failed = True
    # Without a voltage there must be no guess — a wrong soc_max is worse than none.
    s.device_values["INV2"] = {"capacity": 280}
    if s.battery_capacity("INV2") != 0:
        print(f"ERROR: capacity guessed without a pack voltage: {s.battery_capacity('INV2')}")
        failed = True
    assert not failed, "test_battery_capacity_amp_hours_to_kwh"


def test_battery_rate_max_from_charge_current():
    """The charge-current limit becomes a watt rate using the same pack voltage."""
    failed = False
    s = MockSunsynk()
    s.device_values["INV1"] = {"maxChargeCurrentLimit": 100, "chargeVolt": 56.8}
    # 100A at 51.2V = 5120W.
    rate = s.battery_rate_max("INV1")
    if abs(rate - 5120) > 50:
        print(f"ERROR: battery_rate_max {rate}W, expected about 5120W")
        failed = True
    s.device_values["INV2"] = {"chargeVolt": 56.8}
    if s.battery_rate_max("INV2") != 0:
        print("ERROR: a missing current limit must not produce a guessed rate")
        failed = True
    assert not failed, "test_battery_rate_max_from_charge_current"


def test_battery_reserve_min_from_settings():
    """The inverter's own SOC floor is read from the settings object."""
    failed = False
    s = MockSunsynk()
    s.device_settings["INV1"] = {"batteryLowCap": "14"}
    if s.battery_reserve_min("INV1") != 14:
        print(f"ERROR: reserve min {s.battery_reserve_min('INV1')}, expected 14")
        failed = True
    if s.battery_reserve_min("UNKNOWN") != 0:
        print("ERROR: an unknown serial should report no floor, not crash")
        failed = True
    assert not failed, "test_battery_reserve_min_from_settings"
```

Extend that module's `run_sunsynk_api_tests` list with:

```python
        ("device_list_paging", test_get_device_list_pages_and_filters),
        ("device_list_unfiltered", test_get_device_list_unfiltered_returns_all),
        ("telemetry_mapping", test_fetch_device_data_maps_telemetry_and_energy),
        ("telemetry_absent", test_fetch_device_data_absent_fields_are_not_invented),
        ("nominal_pack_voltage", test_nominal_pack_voltage_variants),
        ("capacity_ah_to_kwh", test_battery_capacity_amp_hours_to_kwh),
        ("battery_rate_max", test_battery_rate_max_from_charge_current),
        ("battery_reserve_min", test_battery_reserve_min_from_settings),
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd coverage && ./run_all --test sunsynk_api > /tmp/predbat_sunsynk_t4.log 2>&1; grep -iE "attributeerror|error|exception" /tmp/predbat_sunsynk_t4.log | head
```

Expected: `AttributeError: 'MockSunsynk' object has no attribute 'get_device_list'`.

- [ ] **Step 3: Implement discovery, telemetry and ratings**

Add to the imports in `apps/predbat/sunsynk.py`:

```python
from sunsynk_const import (
    SUNSYNK_PAGE_SIZE,
    SUNSYNK_TELEMETRY,
    SUNSYNK_ENERGY,
    SUNSYNK_TELEMETRY_NEGATE,
    SUNSYNK_CAPACITY_AH_FIELD,
    SUNSYNK_CHARGE_VOLT_FIELD,
    SUNSYNK_MAX_CHARGE_CURRENT_FIELD,
    SUNSYNK_RATED_POWER_FIELD,
    SUNSYNK_BATTERY_LOW_CAP_FIELD,
    LIFEPO4_CHARGE_VOLTS_PER_CELL,
    LIFEPO4_NOMINAL_VOLTS_PER_CELL,
)
```

Append these methods to `SunsynkAPI`:

```python
    @staticmethod
    def _as_float(value, default=0.0):
        """Coerce an API value to float, tolerating strings and nulls."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def get_device_list(self):
        """Discover every inverter on the account, honouring the serial filter."""
        serials = []
        page = 1
        while True:
            params = {"page": str(page), "limit": str(SUNSYNK_PAGE_SIZE), "type": "-2", "status": "-1"}
            data = await self._get("inverter_list", params=params)
            infos = data.get("infos") or []
            for info in infos:
                serial = info.get("sn")
                if serial:
                    serials.append(str(serial))
            total = int(self._as_float(data.get("total"), len(serials)))
            if len(infos) < SUNSYNK_PAGE_SIZE or len(serials) >= total or not infos:
                break
            page += 1
        if self.inverter_sn_filter:
            wanted = {str(sn).lower() for sn in self.inverter_sn_filter}
            serials = [sn for sn in serials if sn.lower() in wanted]
        self.device_list = serials
        return serials

    async def fetch_device_detail(self, sn):
        """Fetch static inverter detail, capturing the rated power for inverter_limit."""
        data = await self._get("inverter_detail", sn=sn)
        if not data:
            return {}
        self.device_detail[sn] = data
        rated = self._as_float(data.get(SUNSYNK_RATED_POWER_FIELD))
        # Only overwrite a known rating — a payload that omits it must not clear it.
        if rated > 0:
            self.device_rated_power[sn] = rated
        return data

    async def fetch_device_data(self, sn):
        """Poll the four realtime endpoints and flatten them onto Predbat's sensor leaves."""
        responses = {}
        for endpoint in ("battery", "grid", "load", "input"):
            params = {"sn": sn, "lan": "en"} if endpoint == "battery" else {"sn": sn}
            responses[endpoint] = await self._get(endpoint, sn=sn, params=params)

        values = {}
        for leaf, (endpoint, field) in SUNSYNK_TELEMETRY.items():
            payload = responses.get(endpoint) or {}
            if field not in payload or payload[field] is None:
                continue
            value = self._as_float(payload[field])
            if leaf in SUNSYNK_TELEMETRY_NEGATE:
                value = -value
            # Preserve integers as integers so SOC publishes as 62 rather than 62.0.
            values[leaf] = int(value) if float(value).is_integer() and leaf in ("soc",) else value
        # Ratings inputs are kept raw on device_values so the derivations below can read them.
        battery = responses.get("battery") or {}
        for field in (SUNSYNK_CAPACITY_AH_FIELD, SUNSYNK_CHARGE_VOLT_FIELD, SUNSYNK_MAX_CHARGE_CURRENT_FIELD):
            if field in battery and battery[field] is not None:
                values[field] = self._as_float(battery[field])
        self.device_values[sn] = values

        energy = {}
        for leaf, (endpoint, field) in SUNSYNK_ENERGY.items():
            payload = responses.get(endpoint) or {}
            # Absent counters stay absent: an arg pointing at a sensor that is never
            # published is worse than an absent arg, which the user can fill in themselves.
            if field in payload and payload[field] is not None:
                energy[leaf] = self._as_float(payload[field])
        self.device_energy[sn] = energy
        return values

    async def fetch_settings(self, sn):
        """Read the whole settings object, which is both config and the write baseline."""
        data = await self._get("settings_read", sn=sn)
        if data:
            self.device_settings[sn] = data
        return data

    def nominal_pack_voltage(self, charge_volts):
        """Infer the pack's nominal voltage from its BMS charge target.

        Sunsynk reports battery capacity in amp-hours, so a voltage is needed to reach kWh.
        A LiFePO4 stack charges to about 3.55V per cell and sits at about 3.2V nominal, so
        the cell count follows from the charge target and the nominal voltage from that.
        Returns 0 when neither an override nor a charge target is available — a wrong
        soc_max is worse than none, because Predbat would plan against a battery that
        does not exist.
        """
        if self.battery_nominal_voltage > 0:
            return self.battery_nominal_voltage
        charge_volts = self._as_float(charge_volts)
        if charge_volts <= 0:
            return 0.0
        cells = round(charge_volts / LIFEPO4_CHARGE_VOLTS_PER_CELL)
        if cells <= 0:
            return 0.0
        return cells * LIFEPO4_NOMINAL_VOLTS_PER_CELL

    def battery_capacity(self, sn):
        """Return the usable battery capacity in kWh, or 0 when it cannot be derived."""
        values = self.device_values.get(sn, {})
        amp_hours = self._as_float(values.get(SUNSYNK_CAPACITY_AH_FIELD))
        volts = self.nominal_pack_voltage(values.get(SUNSYNK_CHARGE_VOLT_FIELD))
        if amp_hours <= 0 or volts <= 0:
            return 0.0
        return amp_hours * volts / 1000.0

    def battery_rate_max(self, sn):
        """Return the maximum charge rate in watts, or 0 when it cannot be derived."""
        values = self.device_values.get(sn, {})
        amps = self._as_float(values.get(SUNSYNK_MAX_CHARGE_CURRENT_FIELD))
        volts = self.nominal_pack_voltage(values.get(SUNSYNK_CHARGE_VOLT_FIELD))
        if amps <= 0 or volts <= 0:
            return 0.0
        return amps * volts

    def battery_reserve_min(self, sn):
        """Return the inverter's own SOC floor as a percent, or 0 when unknown."""
        settings = self.device_settings.get(sn, {})
        return int(self._as_float(settings.get(SUNSYNK_BATTERY_LOW_CAP_FIELD)))
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd coverage && ./run_all --test sunsynk_api > /tmp/predbat_sunsynk_t4.log 2>&1; tail -25 /tmp/predbat_sunsynk_t4.log
```

Expected: all thirteen API tests pass.

- [ ] **Step 5: Commit**

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_api.py
git commit -m "feat(sunsynk): add inverter discovery, telemetry and derived ratings

Pages /inverters with an optional serial filter, polls the four realtime
endpoints per inverter, and derives soc_max and battery_rate_max from an
amp-hour capacity and a pack voltage inferred from the BMS charge target.

Absent fields stay absent rather than publishing an invented zero, and a
capacity with no derivable voltage reports nothing rather than a guess."
```
---

### Task 5: Control derivation and the settings write

The heart of the integration. Predbat's charge and export windows become six time-of-use slots plus a work mode, applied by read-modify-write of the whole settings object.

**Files:**

- Modify: `apps/predbat/sunsynk.py`
- Create: `apps/predbat/tests/test_sunsynk_control.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: `fetch_settings(sn)`, `battery_reserve_min(sn)`, `_post` from Tasks 3-4; `TOU_FIELD`, `SUNSYNK_WORKMODE`, `encode_setting` and the top-level field names from Task 1.
- Produces: `derive_control_state(schedule, current_soc)` returning `{behaviour, work_mode, grid_charge, solar_sell, slot_soc, power}`; `build_tou_slots(schedule, current_soc)` returning exactly `TOU_SLOT_COUNT` dicts of `{time, power, soc, grid_charge}`; `build_settings_payload(sn, schedule, current_soc, now_minutes=None)` returning the full object to POST; `payloads_equal(a, b)`; `async apply_settings(sn, schedule, current_soc, force=False)`; `note_settle(sn, settings)`; `_owned_fields()`; `note_external_change(sn, before, after)`.

A **schedule** dict is the shape the control entities produce, and is what every function here consumes:

```python
{
    "reserve": 10,                                                                       # percent
    "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"},
    "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
}
```

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_sunsynk_control.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk control-state derivation
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk intent to settings-object derivation."""

from unittest.mock import patch
from sunsynk_const import (
    SUNSYNK_WORKMODE,
    SUNSYNK_WORKMODE_FIELD,
    SUNSYNK_SOLAR_SELL_FIELD,
    SUNSYNK_TOU_ENABLE_FIELD,
    SUNSYNK_SERIAL_FIELD,
    SUNSYNK_DAY_FIELDS,
    FREEZE_EXPORT_SOC,
    TOU_FIELD,
    TOU_SLOT_COUNT,
)
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


def _schedule(reserve=10, charge=None, export=None):
    """Build a schedule dict in the shape the control entities produce."""
    idle = {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": charge or dict(idle), "export": export or dict(idle)}


def test_derive_control_state_table():
    """Each Predbat intent maps to the work mode and flags the spec's table requires."""
    failed = False
    s = MockSunsynk()
    cases = [
        ("charge", _schedule(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50, ("charge", SUNSYNK_WORKMODE["zero_export_load"], True, False, 90)),
        ("freeze_charge", _schedule(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, ("freeze_charge", SUNSYNK_WORKMODE["zero_export_load"], True, False, 50)),
        ("hold_charge", _schedule(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, ("hold_charge", SUNSYNK_WORKMODE["zero_export_load"], False, False, 50)),
        ("export", _schedule(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, ("export", SUNSYNK_WORKMODE["selling_first"], False, True, 20)),
        ("freeze_export", _schedule(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, ("freeze_export", SUNSYNK_WORKMODE["selling_first"], False, True, FREEZE_EXPORT_SOC)),
        ("idle", _schedule(reserve=15), 60, ("idle", SUNSYNK_WORKMODE["zero_export_load"], False, False, 15)),
    ]
    for name, schedule, soc, expect in cases:
        result = s.derive_control_state(schedule, soc)
        got = (result["behaviour"], result["work_mode"], result["grid_charge"], result["solar_sell"], result["slot_soc"])
        if got != expect:
            print(f"ERROR: {name} expected {expect} got {got}")
            failed = True
    assert not failed, "test_derive_control_state_table"


def test_build_tou_slots_shape():
    """A charge window yields exactly 6 slots with distinct ascending times from 00:00."""
    failed = False
    s = MockSunsynk()
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    slots = s.build_tou_slots(schedule, current_soc=40)
    if len(slots) != TOU_SLOT_COUNT:
        print(f"ERROR: expected {TOU_SLOT_COUNT} slots, got {len(slots)}")
        failed = True
    times = [slot["time"] for slot in slots]
    if len(set(times)) != len(times):
        print(f"ERROR: duplicate slot start times: {times}")
        failed = True
    if times != sorted(times):
        print(f"ERROR: slot times not ascending: {times}")
        failed = True
    if times and times[0] != "00:00":
        print(f"ERROR: first slot must start at 00:00, got {times[0]}")
        failed = True
    charging = [slot for slot in slots if slot["grid_charge"] and slot["soc"] == 95]
    if not charging:
        print(f"ERROR: no grid-charge slot at soc 95 in {slots}")
        failed = True
    assert not failed, "test_build_tou_slots_shape"


def test_build_tou_slots_seconds_are_dropped():
    """Entity times carry seconds (HH:MM:SS); slots must be HH:MM."""
    failed = False
    s = MockSunsynk()
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:30:00", "end": "05:45:00"})
    times = [slot["time"] for slot in s.build_tou_slots(schedule, current_soc=40)]
    for time_text in times:
        if len(time_text) != 5 or time_text.count(":") != 1:
            print(f"ERROR: slot time {time_text!r} is not HH:MM")
            failed = True
    if "02:30" not in times or "05:45" not in times:
        print(f"ERROR: window boundaries missing from {times}")
        failed = True
    assert not failed, "test_build_tou_slots_seconds_are_dropped"


def test_build_tou_slots_idle_is_still_six_distinct():
    """An empty schedule still yields six distinct self-use slots."""
    failed = False
    s = MockSunsynk()
    slots = s.build_tou_slots(_schedule(reserve=15), current_soc=50)
    times = [slot["time"] for slot in slots]
    if len(set(times)) != TOU_SLOT_COUNT:
        print(f"ERROR: idle schedule produced {len(set(times))} distinct times: {times}")
        failed = True
    if any(slot["grid_charge"] for slot in slots):
        print("ERROR: an idle schedule must not enable grid charge")
        failed = True
    if any(slot["soc"] != 15 for slot in slots):
        print(f"ERROR: idle slots should all hold at the reserve: {slots}")
        failed = True
    assert not failed, "test_build_tou_slots_idle_is_still_six_distinct"


def test_active_window_drives_the_global_mode():
    """The top-level mode follows the window active NOW, not a static precedence.

    Sunsynk has one global work mode. If an export window enabled elsewhere in the day
    pinned the mode to selling-first, it would block the charge window's grid charging.
    """
    failed = False
    s = MockSunsynk()
    schedule = _schedule(
        reserve=10,
        charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"},
        export={"enable": True, "soc": 20, "power": 3000, "start": "16:00:00", "end": "19:00:00"},
    )
    # 03:00 -> inside the charge window.
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["zero_export_load"]:
        print(f"ERROR: at 03:00 expected zero_export_load, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    # 17:00 -> inside the export window.
    payload = s.build_settings_payload("INV1", schedule, current_soc=80, now_minutes=17 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["selling_first"]:
        print(f"ERROR: at 17:00 expected selling_first, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    # 12:00 -> neither window, so self-use.
    payload = s.build_settings_payload("INV1", schedule, current_soc=60, now_minutes=12 * 60)
    if payload[SUNSYNK_WORKMODE_FIELD] != SUNSYNK_WORKMODE["zero_export_load"]:
        print(f"ERROR: at 12:00 expected zero_export_load, got {payload[SUNSYNK_WORKMODE_FIELD]}")
        failed = True
    assert not failed, "test_active_window_drives_the_global_mode"


def test_window_active_handles_midnight_wrap():
    """A window running past midnight is active on both sides of it."""
    failed = False
    s = MockSunsynk()
    window = {"enable": True, "start": "23:00", "end": "02:00"}
    for minutes, expect in ((23 * 60 + 30, True), (60, True), (12 * 60, False), (22 * 60, False)):
        got = s._window_active(window, minutes)
        if got != expect:
            print(f"ERROR: minute {minutes} active={got}, expected {expect}")
            failed = True
    # A zero-length window is never active.
    if s._window_active({"enable": True, "start": "05:00", "end": "05:00"}, 5 * 60):
        print("ERROR: a zero-length window must never be active")
        failed = True
    assert not failed, "test_window_active_handles_midnight_wrap"


def test_payload_renders_indexed_fields_and_types():
    """Slots become sellTimeN/capN/timeNon with the right wire types."""
    failed = False
    s = MockSunsynk()
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    for n in range(1, TOU_SLOT_COUNT + 1):
        for concept in ("time", "power", "soc", "grid_charge"):
            name = TOU_FIELD[concept].format(n=n)
            if name not in payload:
                print(f"ERROR: payload missing {name}")
                failed = True
                continue
            value = payload[name]
            if concept == "grid_charge":
                if not isinstance(value, bool):
                    print(f"ERROR: {name} = {value!r} should be a bare JSON boolean")
                    failed = True
            elif not isinstance(value, str):
                print(f"ERROR: {name} = {value!r} should be a string")
                failed = True
    for day in SUNSYNK_DAY_FIELDS:
        if payload.get(day) is not True:
            print(f"ERROR: {day} should be True, got {payload.get(day)!r}")
            failed = True
    if payload.get(SUNSYNK_TOU_ENABLE_FIELD) != "1":
        print(f"ERROR: TOU master enable should be '1', got {payload.get(SUNSYNK_TOU_ENABLE_FIELD)!r}")
        failed = True
    if payload.get(SUNSYNK_SERIAL_FIELD) != "INV1":
        print(f"ERROR: serial should be echoed back, got {payload.get(SUNSYNK_SERIAL_FIELD)!r}")
        failed = True
    if payload.get(SUNSYNK_SOLAR_SELL_FIELD) not in ("0", "1"):
        print(f"ERROR: solarSell should be '0' or '1', got {payload.get(SUNSYNK_SOLAR_SELL_FIELD)!r}")
        failed = True
    assert not failed, "test_payload_renders_indexed_fields_and_types"


def test_payload_preserves_unowned_settings():
    """Read-modify-write leaves every field Predbat does not own exactly as it was."""
    failed = False
    s = MockSunsynk()
    s.device_settings["INV1"] = {
        "sn": "INV1",
        "batteryShutdownCap": "5",
        "batteryLowCap": "10",
        "safetyType": "3",
        "zeroExportPower": "20",
        "solarMaxSellPower": "8000",
        "genTime1on": False,
        "sellTime1Volt": "49.0",
        "batteryMaxCurrentCharge": "100",
    }
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    for key, expect in (("batteryShutdownCap", "5"), ("safetyType", "3"), ("zeroExportPower", "20"), ("solarMaxSellPower", "8000"), ("batteryMaxCurrentCharge", "100"), ("genTime1on", False), ("sellTime1Volt", "49.0")):
        if payload.get(key) != expect:
            print(f"ERROR: unowned field {key} became {payload.get(key)!r}, expected {expect!r}")
            failed = True
    assert not failed, "test_payload_preserves_unowned_settings"


def test_payload_clamps_to_the_inverter_soc_floor():
    """No slot may ask for less than the installer-set floor the inverter reports."""
    failed = False
    s = MockSunsynk()
    s.device_settings["INV1"] = {"batteryLowCap": "20"}
    # Predbat's control entities start at 0 and only reach real values once written.
    schedule = _schedule(reserve=0, charge={"enable": True, "soc": 5, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    payload = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    for n in range(1, TOU_SLOT_COUNT + 1):
        value = int(payload[TOU_FIELD["soc"].format(n=n)])
        if value < 20:
            print(f"ERROR: slot {n} soc {value} is below the inverter's 20% floor")
            failed = True
    if not any("floor" in str(m).lower() for m in s.log_messages):
        print("ERROR: clamping to the floor should be logged once")
        failed = True
    assert not failed, "test_payload_clamps_to_the_inverter_soc_floor"


def test_payloads_equal_ignores_nothing_material():
    """Change detection sees a real difference and ignores an identical rewrite."""
    failed = False
    s = MockSunsynk()
    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    first = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    same = s.build_settings_payload("INV1", schedule, current_soc=40, now_minutes=3 * 60)
    if not s.payloads_equal(first, same):
        print("ERROR: two identical payloads compared unequal")
        failed = True
    other = _schedule(reserve=10, charge={"enable": True, "soc": 80, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    changed = s.build_settings_payload("INV1", other, current_soc=40, now_minutes=3 * 60)
    if s.payloads_equal(first, changed):
        print("ERROR: a changed target SOC compared equal")
        failed = True
    assert not failed, "test_payloads_equal_ignores_nothing_material"


def test_apply_settings_skips_an_unchanged_payload():
    """An unchanged plan does not re-post, so the dongle is not churned every cycle."""
    failed = False
    s = MockSunsynk()
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object for the read half of read-modify-write."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write."""
        posts.append((endpoint_key, sn))
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if len(posts) != 1:
        print(f"ERROR: expected exactly 1 write for an unchanged plan, got {len(posts)}")
        failed = True
    # force must override the diff gate.
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40, force=True))
    if len(posts) != 2:
        print(f"ERROR: force=True should have written again, total writes {len(posts)}")
        failed = True
    assert not failed, "test_apply_settings_skips_an_unchanged_payload"


def test_apply_settings_fails_closed_without_a_read():
    """If the settings read fails, nothing is written — the write baseline is unknown."""
    failed = False
    s = MockSunsynk()
    posts = []

    async def fake_get_empty(endpoint_key, sn=None, params=None):
        """Simulate a failed settings read."""
        return {}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write, which must not happen here."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get_empty), patch.object(s, "_post", side_effect=fake_post):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if applied:
        print("ERROR: apply_settings reported success without a settings read")
        failed = True
    if posts:
        print(f"ERROR: wrote {posts} despite having no baseline to modify")
        failed = True
    assert not failed, "test_apply_settings_fails_closed_without_a_read"


def test_apply_settings_respects_control_enable():
    """With control disabled, the component derives but never writes."""
    failed = False
    s = MockSunsynk(control_enable=False)
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record each write, which must not happen here."""
        posts.append(endpoint_key)
        return {"msg": "Success"}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if posts:
        print(f"ERROR: control is disabled but {posts} was written")
        failed = True
    assert not failed, "test_apply_settings_respects_control_enable"


def test_apply_settings_reports_a_failed_write():
    """A failed write is detected and does not update the applied-payload cache.

    _post reports failure as None; a successful settings write carries no data payload,
    so {} means "written, nothing returned". Confusing the two would let a failed write
    be cached as applied, and the diff gate would then never retry it.
    """
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a minimal settings object."""
        return {"sn": "INV1", "batteryLowCap": "10"}

    async def fake_post_fails(endpoint_key, sn=None, body=None):
        """Simulate a failed write."""
        return None

    async def fake_post_empty(endpoint_key, sn=None, body=None):
        """Simulate a successful write that returns no data payload."""
        return {}

    schedule = _schedule(reserve=10, charge={"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"})
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post_fails):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if applied:
        print("ERROR: a failed write reported success")
        failed = True
    if "INV1" in s.applied_payload:
        print("ERROR: a failed write was cached as applied, so it would never be retried")
        failed = True

    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post_empty):
        applied = run_async_local(s.apply_settings("INV1", schedule, current_soc=40))
    if not applied:
        print("ERROR: a successful write returning no data was read as a failure")
        failed = True
    if "INV1" not in s.applied_payload:
        print("ERROR: a successful write was not cached")
        failed = True
    assert not failed, "test_apply_settings_reports_a_failed_write"


def test_external_changes_are_logged():
    """A setting changed outside Predbat is reported, not silently overwritten."""
    failed = False
    s = MockSunsynk()
    before = {"sn": "INV1", "batteryLowCap": "10", "zeroExportPower": "20", "cap1": "50"}
    after = {"sn": "INV1", "batteryLowCap": "10", "zeroExportPower": "80", "cap1": "95"}
    s.note_external_change("INV1", before, after)
    joined = " ".join(str(m) for m in s.log_messages)
    if "zeroExportPower" not in joined:
        print(f"ERROR: an unowned field change was not reported: {joined}")
        failed = True
    # cap1 is Predbat's own field, so its change is expected and must not be reported.
    if "cap1" in joined:
        print("ERROR: a field Predbat owns was reported as an external change")
        failed = True
    # No change at all must stay quiet.
    quiet = MockSunsynk()
    quiet.note_external_change("INV1", before, dict(before))
    if quiet.log_messages:
        print(f"ERROR: an unchanged read logged anyway: {quiet.log_messages}")
        failed = True
    assert not failed, "test_external_changes_are_logged"


def run_sunsynk_control_tests(my_predbat):
    """Run all Sunsynk control-logic tests."""
    failed = False
    for name, fn in [
        ("derive_state_table", test_derive_control_state_table),
        ("tou_slots_shape", test_build_tou_slots_shape),
        ("tou_slots_seconds", test_build_tou_slots_seconds_are_dropped),
        ("tou_slots_idle", test_build_tou_slots_idle_is_still_six_distinct),
        ("active_window_mode", test_active_window_drives_the_global_mode),
        ("midnight_wrap", test_window_active_handles_midnight_wrap),
        ("payload_field_types", test_payload_renders_indexed_fields_and_types),
        ("payload_preserves", test_payload_preserves_unowned_settings),
        ("payload_soc_floor", test_payload_clamps_to_the_inverter_soc_floor),
        ("payloads_equal", test_payloads_equal_ignores_nothing_material),
        ("apply_skips_unchanged", test_apply_settings_skips_an_unchanged_payload),
        ("apply_fails_closed", test_apply_settings_fails_closed_without_a_read),
        ("apply_control_enable", test_apply_settings_respects_control_enable),
        ("apply_failed_write", test_apply_settings_reports_a_failed_write),
        ("external_changes", test_external_changes_are_logged),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register and run to verify it fails**

Add `from tests.test_sunsynk_control import run_sunsynk_control_tests` and `("sunsynk_control", run_sunsynk_control_tests, "Sunsynk control-logic tests", False),` to `apps/predbat/unit_test.py`. Then:

```bash
cd coverage && ./run_all --test sunsynk_control > /tmp/predbat_sunsynk_t5.log 2>&1; grep -iE "attributeerror|error|exception" /tmp/predbat_sunsynk_t5.log | head
```

Expected: `AttributeError: 'MockSunsynk' object has no attribute 'derive_control_state'`.

- [ ] **Step 3: Implement the control derivation**

Add to the `sunsynk_const` imports in `apps/predbat/sunsynk.py`:

```python
from sunsynk_const import (
    SUNSYNK_WORKMODE,
    SUNSYNK_WORKMODE_FIELD,
    SUNSYNK_SOLAR_SELL_FIELD,
    SUNSYNK_TOU_ENABLE_FIELD,
    SUNSYNK_SERIAL_FIELD,
    SUNSYNK_DAY_FIELDS,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    TOU_FILLER_TIMES,
    FREEZE_EXPORT_SOC,
    SUNSYNK_SETTLE_POLLS,
    encode_setting,
)
```

Append to `SunsynkAPI`:

```python
    def derive_control_state(self, schedule, current_soc):
        """Map Predbat's schedule intent to a Sunsynk control state (see the spec's table).

        Semantics are inherited from DEYE — the same registers sit behind both clouds —
        but every wire value comes from SUNSYNK_WORKMODE, never from DEYE's enum.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})

        if export.get("enable"):
            export_soc = int(export.get("soc", FREEZE_EXPORT_SOC))
            behaviour = "freeze_export" if export_soc >= FREEZE_EXPORT_SOC else "export"
            slot_soc = FREEZE_EXPORT_SOC if export_soc >= FREEZE_EXPORT_SOC else export_soc
            return {"behaviour": behaviour, "work_mode": SUNSYNK_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": slot_soc, "power": int(export.get("power", 0))}

        if charge.get("enable"):
            charge_soc = int(charge.get("soc", 0))
            if charge_soc > current_soc and charge_soc > reserve:
                return {"behaviour": "charge", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": charge_soc, "power": int(charge.get("power", 0))}
            if charge_soc == reserve:
                return {"behaviour": "freeze_charge", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}
            return {"behaviour": "hold_charge", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}

        return {"behaviour": "idle", "work_mode": SUNSYNK_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": 0}

    @staticmethod
    def _to_slot_time(value):
        """Normalise a schedule time to the HH:MM Sunsynk's slots require.

        The control entities carry HH:MM:SS because that is what Predbat writes (see
        INVERTER_DEF charge_time_format), so the seconds are dropped here — at the one
        point a schedule time becomes a slot time.
        """
        parts = str(value or "00:00").split(":")
        if len(parts) < 2:
            return "00:00"
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except ValueError:
            return "00:00"

    @staticmethod
    def _hm_to_minutes(hm):
        """Convert an HH:MM string to minutes since midnight (0 on bad input)."""
        try:
            parts = str(hm).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    def _window_active(self, window, now_minutes):
        """Return True if an enabled window covers now_minutes, handling a midnight wrap."""
        if not window.get("enable") or not window.get("start") or not window.get("end"):
            return False
        start = self._hm_to_minutes(self._to_slot_time(window["start"]))
        end = self._hm_to_minutes(self._to_slot_time(window["end"]))
        if start == end:
            return False
        if start < end:
            return start <= now_minutes < end
        return now_minutes >= start or now_minutes < end

    def _self_use_slot(self, start_time, reserve):
        """Build a self-use slot holding at the reserve SOC."""
        return {"time": start_time, "power": 0, "soc": int(reserve), "grid_charge": False}

    def _action_slot(self, start_time, state):
        """Build a slot realising a derived control state."""
        return {"time": start_time, "power": int(state["power"]), "soc": int(state["slot_soc"]), "grid_charge": bool(state["grid_charge"])}

    def build_tou_slots(self, schedule, current_soc):
        """Build exactly TOU_SLOT_COUNT ordered slots covering 24h from the schedule windows.

        Slots are sequential intervals, so every start time must be distinct and ascending.
        Segment boundaries are collected from a 00:00 self-use baseline plus each enabled
        window's start (its action) and end (back to self-use), then padded with fillers
        and trimmed to the earliest, most imminent TOU_SLOT_COUNT.
        """
        reserve = int(schedule.get("reserve", 0))
        idle = {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None}
        segments = {"00:00": dict(idle)}
        for direction in ("charge", "export"):
            window = schedule.get(direction, {})
            if not (window.get("enable") and window.get("start") and window.get("end")):
                continue
            intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
            intent[direction] = {"enable": True, "soc": window.get("soc", 0), "power": window.get("power", 0)}
            segments[self._to_slot_time(window["start"])] = self.derive_control_state(intent, current_soc)
            segments.setdefault(self._to_slot_time(window["end"]), dict(idle))

        slots = []
        for start_time, state in sorted(segments.items(), key=lambda item: item[0]):
            if state.get("grid_charge") or state.get("solar_sell") or state.get("power"):
                slots.append(self._action_slot(start_time, state))
            else:
                slots.append(self._self_use_slot(start_time, reserve))

        used = {slot["time"] for slot in slots}
        for filler in TOU_FILLER_TIMES:
            if len(slots) >= TOU_SLOT_COUNT:
                break
            if filler not in used:
                slots.append(self._self_use_slot(filler, reserve))
                used.add(filler)
        return sorted(slots, key=lambda slot: slot["time"])[:TOU_SLOT_COUNT]

    def _now_minutes(self):
        """Return minutes since local midnight, for time-aware window selection."""
        try:
            return int(self.minutes_now)
        except (TypeError, ValueError):
            return 0

    def _active_state(self, schedule, current_soc, now_minutes):
        """Derive the control state for the window active at now_minutes, else idle.

        Sunsynk has a single global work mode, so the top-level mode must follow the
        window active RIGHT NOW rather than a static export-first precedence: otherwise
        an export window enabled elsewhere in the day would pin the mode to selling-first
        and block the charge window's grid charging.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})
        intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
        if self._window_active(export, now_minutes):
            intent["export"] = {"enable": True, "soc": export.get("soc", 0), "power": export.get("power", 0)}
        elif self._window_active(charge, now_minutes):
            intent["charge"] = {"enable": True, "soc": charge.get("soc", 0), "power": charge.get("power", 0)}
        return self.derive_control_state(intent, current_soc)

    def build_settings_payload(self, sn, schedule, current_soc, now_minutes=None):
        """Build the full settings object to POST for one inverter.

        Read-modify-write: start from the last-read settings so every field Predbat does
        not own survives verbatim, then overwrite only the slots, mode and flags it does.
        """
        if now_minutes is None:
            now_minutes = self._now_minutes()
        slots = self.build_tou_slots(schedule, current_soc)
        active = self._active_state(schedule, current_soc, now_minutes)

        # Never ask the battery to go below the floor its own installer settings declare.
        # Predbat's control entities start at 0 and only reach their real values once it
        # has written them, so without this the first write of a cycle sends slot SOC 0 to
        # a pack whose floor is 14%. Applied last so no caller can bypass it.
        floor = self.battery_reserve_min(sn)
        if floor > 0:
            lifted = False
            for slot in slots:
                if slot["soc"] < floor:
                    slot["soc"] = floor
                    lifted = True
            if lifted and sn not in self._soc_floor_warned:
                self._soc_floor_warned.add(sn)
                self.log(f"Info: Sunsynk {sn} raising requested slot SOC to the inverter's {floor}% floor (batteryLowCap)")

        payload = dict(self.device_settings.get(sn, {}))
        payload[SUNSYNK_SERIAL_FIELD] = sn
        payload[SUNSYNK_WORKMODE_FIELD] = active["work_mode"]
        payload[SUNSYNK_SOLAR_SELL_FIELD] = encode_setting(SUNSYNK_SOLAR_SELL_FIELD, "1" if active["solar_sell"] else "0")
        payload[SUNSYNK_TOU_ENABLE_FIELD] = encode_setting(SUNSYNK_TOU_ENABLE_FIELD, "1")
        for day in SUNSYNK_DAY_FIELDS:
            payload[day] = encode_setting(day, True)
        for index, slot in enumerate(slots, start=1):
            payload[TOU_FIELD["time"].format(n=index)] = encode_setting(TOU_FIELD["time"].format(n=index), slot["time"])
            payload[TOU_FIELD["power"].format(n=index)] = encode_setting(TOU_FIELD["power"].format(n=index), slot["power"])
            payload[TOU_FIELD["soc"].format(n=index)] = encode_setting(TOU_FIELD["soc"].format(n=index), slot["soc"])
            payload[TOU_FIELD["grid_charge"].format(n=index)] = encode_setting(TOU_FIELD["grid_charge"].format(n=index), slot["grid_charge"])
        return payload

    def payloads_equal(self, a, b):
        """Compare two settings payloads for change detection."""
        return dict(a or {}) == dict(b or {})

    async def apply_settings(self, sn, schedule, current_soc, force=False):
        """Read, modify and write the settings object for one inverter.

        Returns True if a write was performed. Fails closed: without a fresh read there is
        no baseline to modify, so nothing is written rather than posting a payload that
        would drop every field Predbat does not own.
        """
        if not self.control_enable:
            return False

        # Re-read immediately before writing so the race with the Sunsynk phone app is as
        # small as possible, and so unowned fields carry the newest values.
        previous_settings = dict(self.device_settings.get(sn, {}))
        settings = await self.fetch_settings(sn)
        if not settings:
            self.log(f"Warn: Sunsynk {sn} settings read failed, skipping the write (no baseline to modify)")
            return False
        self.note_external_change(sn, previous_settings, settings)

        payload = self.build_settings_payload(sn, schedule, current_soc)
        previous = self.applied_payload.get(sn)
        if previous and self.payloads_equal(previous, payload) and not force:
            # Nothing changed, so do not churn the dongle. Track how long the read-back has
            # disagreed; normal cloud-to-dongle latency is one to five minutes.
            return False

        # _post reports failure as None. A successful settings write carries no data
        # payload, so {} means "written, nothing returned" and only None means failure.
        response = await self._post("settings_set", sn=sn, body=payload)
        if response is None:
            self.log(f"Warn: Sunsynk {sn} settings write failed, the plan has not reached the inverter")
            return False
        self.applied_payload[sn] = payload
        self.settle_count[sn] = 0
        self.log(f"Info: Sunsynk {sn} settings written ({self._active_state(schedule, current_soc, self._now_minutes())['behaviour']})")
        return True

    def note_settle(self, sn, settings):
        """Track how many cycles the inverter has disagreed with the last write.

        A write is acknowledged by the cloud long before the dongle collects it, so
        divergence within SUNSYNK_SETTLE_POLLS cycles is normal latency, not a failure.
        """
        applied = self.applied_payload.get(sn)
        if not applied or not settings:
            return
        owned = [SUNSYNK_WORKMODE_FIELD] + [TOU_FIELD[c].format(n=n) for n in range(1, TOU_SLOT_COUNT + 1) for c in ("time", "soc", "grid_charge")]
        if all(str(settings.get(key)) == str(applied.get(key)) for key in owned):
            self.settle_count[sn] = 0
            return
        self.settle_count[sn] = self.settle_count.get(sn, 0) + 1
        if self.settle_count[sn] > SUNSYNK_SETTLE_POLLS:
            self.log(f"Warn: Sunsynk {sn} has not applied Predbat's settings after {self.settle_count[sn]} cycles; check the inverter is online in the Sunsynk app")

    def _owned_fields(self):
        """Return every settings key this component writes, so the rest can be watched."""
        owned = {SUNSYNK_SERIAL_FIELD, SUNSYNK_WORKMODE_FIELD, SUNSYNK_SOLAR_SELL_FIELD, SUNSYNK_TOU_ENABLE_FIELD}
        owned.update(SUNSYNK_DAY_FIELDS)
        for n in range(1, TOU_SLOT_COUNT + 1):
            owned.update(TOU_FIELD[concept].format(n=n) for concept in ("time", "power", "soc", "grid_charge"))
        return owned

    def note_external_change(self, sn, before, after):
        """Log when someone else changed a setting Predbat does not own.

        There is one whole-object write endpoint, so a race with the Sunsynk phone app is
        unavoidable and last writer wins. Predbat cannot prevent it, but it can say so —
        otherwise a user's app change silently disappearing into a read-modify-write looks
        like the inverter losing settings by itself.
        """
        if not before or not after:
            return
        owned = self._owned_fields()
        changed = [key for key, value in after.items() if key not in owned and key in before and str(before[key]) != str(value)]
        if changed:
            self.log(f"Info: Sunsynk {sn} settings changed outside Predbat since the last read: {', '.join(sorted(changed))}")
```

- [ ] **Step 4: Run to verify they pass**

```bash
cd coverage && ./run_all --test sunsynk_control > /tmp/predbat_sunsynk_t5.log 2>&1; tail -25 /tmp/predbat_sunsynk_t5.log
```

Expected: all fifteen control tests pass.

- [ ] **Step 5: Commit**

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_control.py apps/predbat/unit_test.py
git commit -m "feat(sunsynk): derive six TOU slots and write the settings object

Maps Predbat's charge and export windows onto sellTimeN/sellTimeNPac/capN/
timeNon plus a global work mode, applied by read-modify-write so every
installer setting Predbat does not own survives verbatim.

The global mode follows the window active now, not a static precedence,
because Sunsynk has only one. Writes are diff-gated, clamped to the
inverter's own SOC floor, and skipped entirely if the read fails."
```
---

### Task 6: Publishing sensors and control entities

**Files:**

- Modify: `apps/predbat/sunsynk.py`
- Create: `apps/predbat/tests/test_sunsynk_publish.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: `device_values`, `device_energy`, `battery_capacity(sn)`, `battery_rate_max(sn)`, `battery_reserve_min(sn)`, `device_rated_power` from Task 4; `apply_settings` from Task 5.
- Produces: `_sensor_name(sn, leaf)` → `sensor.predbat_sunsynk_<sn>_<leaf>`; `_control_name(domain, sn, leaf)` → `<domain>.predbat_sunsynk_<sn>_<leaf>`; `async publish_data()`; `async publish_schedule_settings_ha(sn)`; `async get_schedule_settings_ha(sn)`; `_sn_from_entity(entity_id)`; `update_local_schedule(sn, entity_id, value)`; `async apply_schedule(sn, force=False)`; and the `select_event` / `number_event` / `switch_event` handlers.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_sunsynk_publish.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk sensor and control entity publishing
# -----------------------------------------------------------------------------

"""Tests for Sunsynk entity publishing and the control-entity round trip."""

from unittest.mock import patch
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


class PublishingSunsynk(MockSunsynk):
    """MockSunsynk that records dashboard_item calls and serves entity reads back."""

    def __init__(self, **kwargs):
        """Set up the recorder alongside the normal test double."""
        super().__init__(**kwargs)
        self.published = {}

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Record a published entity."""
        self.published[entity_id] = {"state": state, "attributes": attributes or {}}

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back a previously published entity, as Home Assistant would."""
        if entity_id in self.published:
            return self.published[entity_id]["state"]
        return default


def test_entity_names_are_namespaced():
    """Sensor and control entity ids carry the component prefix and the serial."""
    failed = False
    s = PublishingSunsynk()
    if s._sensor_name("INV1", "soc") != "sensor.predbat_sunsynk_inv1_soc":
        print(f"ERROR: sensor name {s._sensor_name('INV1', 'soc')}")
        failed = True
    if s._control_name("number", "INV1", "battery_schedule_reserve") != "number.predbat_sunsynk_inv1_battery_schedule_reserve":
        print(f"ERROR: control name {s._control_name('number', 'INV1', 'battery_schedule_reserve')}")
        failed = True
    assert not failed, "test_entity_names_are_namespaced"


def test_publish_data_emits_telemetry_and_ratings():
    """Telemetry, energy counters and derived ratings all publish with units."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {"soc": 62, "battery_power": -1500, "grid_power": 430, "load_power": 900, "pv_power": 2100, "temperature": 21.5, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_energy["INV1"] = {"pv_today": 9.8, "import_today": 3.2}
    s.device_rated_power["INV1"] = 8000.0
    run_async_local(s.publish_data())
    for leaf in ("soc", "battery_power", "grid_power", "load_power", "pv_power", "temperature", "pv_today", "import_today", "battery_capacity", "battery_rate_max", "inverter_limit"):
        entity = s._sensor_name("INV1", leaf)
        if entity not in s.published:
            print(f"ERROR: {leaf} was not published")
            failed = True
        elif not s.published[entity]["attributes"].get("unit_of_measurement"):
            print(f"ERROR: {leaf} published without a unit")
            failed = True
    if s.published.get(s._sensor_name("INV1", "soc"), {}).get("state") != 62:
        print("ERROR: soc state wrong")
        failed = True
    assert not failed, "test_publish_data_emits_telemetry_and_ratings"


def test_publish_data_omits_underivable_ratings():
    """A rating that cannot be derived is not published as a zero."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    # No chargeVolt, so no pack voltage, so neither capacity nor rate is derivable.
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "maxChargeCurrentLimit": 100}
    s.device_energy["INV1"] = {}
    run_async_local(s.publish_data())
    for leaf in ("battery_capacity", "battery_rate_max", "inverter_limit"):
        if s._sensor_name("INV1", leaf) in s.published:
            print(f"ERROR: {leaf} was published despite being underivable")
            failed = True
    assert not failed, "test_publish_data_omits_underivable_ratings"


def test_schedule_entities_round_trip():
    """Control entities publish in the format Predbat expects and read back unchanged."""
    failed = False
    s = PublishingSunsynk()
    s.local_schedule["INV1"] = {
        "reserve": 12,
        "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"},
        "export": {"enable": False, "soc": 20, "power": 2500, "start": "16:00:00", "end": "19:00:00"},
    }
    run_async_local(s.publish_schedule_settings_ha("INV1"))
    # Times must be HH:MM:SS to match INVERTER_DEF charge_time_format; anything else makes
    # Predbat substitute its own dummy entities and the window never reaches the inverter.
    start = s.published[s._control_name("select", "INV1", "battery_schedule_charge_start_time")]["state"]
    if start != "02:00:00" or start.count(":") != 2:
        print(f"ERROR: charge start published as {start!r}, expected HH:MM:SS")
        failed = True
    if s.published[s._control_name("switch", "INV1", "battery_schedule_charge_enable")]["state"] != "on":
        print("ERROR: charge enable should be 'on'")
        failed = True
    if s.published[s._control_name("switch", "INV1", "battery_schedule_export_enable")]["state"] != "off":
        print("ERROR: export enable should be 'off'")
        failed = True
    read_back = run_async_local(s.get_schedule_settings_ha("INV1"))
    if read_back["reserve"] != 12:
        print(f"ERROR: reserve round-tripped to {read_back['reserve']}")
        failed = True
    if read_back["charge"] != {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"}:
        print(f"ERROR: charge round-tripped to {read_back['charge']}")
        failed = True
    assert not failed, "test_schedule_entities_round_trip"


def test_reserve_entity_is_not_clamped_to_the_floor():
    """The reserve entity publishes what Predbat wrote, not the inverter's floor.

    This entity is Predbat's control surface: it writes a value then reads it back to
    confirm, so publishing anything else guarantees a mismatch and a retry storm. The
    floor is enforced at the API boundary in build_settings_payload instead.
    """
    failed = False
    s = PublishingSunsynk()
    s.device_settings["INV1"] = {"batteryLowCap": "20"}
    s.local_schedule["INV1"] = {"reserve": 5, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    run_async_local(s.publish_schedule_settings_ha("INV1"))
    published = s.published[s._control_name("number", "INV1", "battery_schedule_reserve")]["state"]
    if published != 5:
        print(f"ERROR: reserve entity published as {published}, expected the written 5")
        failed = True
    assert not failed, "test_reserve_entity_is_not_clamped_to_the_floor"


def test_sn_from_entity_disambiguates_prefixes():
    """A serial that is a prefix of another never mis-routes a control write."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1", "INV11"]
    cases = [
        (s._control_name("number", "INV1", "battery_schedule_reserve"), "INV1"),
        (s._control_name("number", "INV11", "battery_schedule_reserve"), "INV11"),
        ("number.predbat_sunsynk_unknown_battery_schedule_reserve", None),
    ]
    for entity_id, expect in cases:
        got = s._sn_from_entity(entity_id)
        if got != expect:
            print(f"ERROR: {entity_id} resolved to {got!r}, expected {expect!r}")
            failed = True
    assert not failed, "test_sn_from_entity_disambiguates_prefixes"


def test_control_events_update_the_local_schedule():
    """Select/number/switch events land in local_schedule without writing immediately."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.local_schedule["INV1"] = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    applied = []

    async def fake_apply(sn, force=False):
        """Record schedule applications."""
        applied.append((sn, force))
        return True

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s.select_event(s._control_name("select", "INV1", "battery_schedule_charge_start_time"), "02:30:00"))
        run_async_local(s.number_event(s._control_name("number", "INV1", "battery_schedule_charge_soc"), 88))
        run_async_local(s.switch_event(s._control_name("switch", "INV1", "battery_schedule_charge_enable"), "turn_on"))
    schedule = s.local_schedule["INV1"]
    if schedule["charge"]["start"] != "02:30:00":
        print(f"ERROR: start not updated, got {schedule['charge']['start']}")
        failed = True
    if schedule["charge"]["soc"] != 88:
        print(f"ERROR: soc not updated, got {schedule['charge']['soc']}")
        failed = True
    if schedule["charge"]["enable"] is not True:
        print(f"ERROR: enable not updated, got {schedule['charge']['enable']}")
        failed = True
    assert not failed, "test_control_events_update_the_local_schedule"


def test_write_button_forces_an_apply():
    """Toggling the schedule-write switch forces a write even with no diff."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.local_schedule["INV1"] = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    applied = []

    async def fake_apply(sn, force=False):
        """Record schedule applications."""
        applied.append((sn, force))
        return True

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s.switch_event(s._control_name("switch", "INV1", "battery_schedule_charge_write"), "turn_on"))
    if applied != [("INV1", True)]:
        print(f"ERROR: expected a forced apply for INV1, got {applied}")
        failed = True
    assert not failed, "test_write_button_forces_an_apply"


def run_sunsynk_publish_tests(my_predbat):
    """Run all Sunsynk publishing tests."""
    failed = False
    for name, fn in [
        ("entity_names", test_entity_names_are_namespaced),
        ("publish_telemetry", test_publish_data_emits_telemetry_and_ratings),
        ("publish_omits_underivable", test_publish_data_omits_underivable_ratings),
        ("schedule_round_trip", test_schedule_entities_round_trip),
        ("reserve_not_clamped", test_reserve_entity_is_not_clamped_to_the_floor),
        ("sn_from_entity", test_sn_from_entity_disambiguates_prefixes),
        ("control_events", test_control_events_update_the_local_schedule),
        ("write_button", test_write_button_forces_an_apply),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register and run to verify it fails**

Add `from tests.test_sunsynk_publish import run_sunsynk_publish_tests` and `("sunsynk_publish", run_sunsynk_publish_tests, "Sunsynk publish tests", False),` to `apps/predbat/unit_test.py`, then:

```bash
cd coverage && ./run_all --test sunsynk_publish > /tmp/predbat_sunsynk_t6.log 2>&1; grep -iE "attributeerror|error|exception" /tmp/predbat_sunsynk_t6.log | head
```

Expected: `AttributeError: 'PublishingSunsynk' object has no attribute '_sensor_name'`.

- [ ] **Step 3: Implement publishing and the event handlers**

Append to `SunsynkAPI` in `apps/predbat/sunsynk.py`:

```python
    def _sensor_name(self, sn, leaf):
        """Return a namespaced Sunsynk sensor entity id."""
        return f"sensor.{self.prefix}_sunsynk_{sn.lower()}_{leaf}"

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced Sunsynk control entity id."""
        return f"{domain}.{self.prefix}_sunsynk_{sn.lower()}_{leaf}"

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "temperature": "°C", "battery_voltage": "V"}
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=values[leaf], attributes={"unit_of_measurement": unit, "friendly_name": f"Sunsynk {sn} {leaf.replace('_', ' ').title()}"}, app="sunsynk")

            # Ratings are published only when actually derivable. An arg pointing at a
            # sensor that never appears is worse than an absent arg, which the user can
            # fill in via apps.yaml.
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=round(capacity, 3), attributes={"unit_of_measurement": "kWh", "friendly_name": f"Sunsynk {sn} Battery Capacity"}, app="sunsynk")
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_rate_max"), state=round(rate_max), attributes={"unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} Battery Rate Max"}, app="sunsynk")
            rated_power = self.device_rated_power.get(sn, 0.0)
            if rated_power > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=rated_power, attributes={"unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} Inverter Limit"}, app="sunsynk")
            floor = self.battery_reserve_min(sn)
            if floor > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_reserve_min"), state=floor, attributes={"unit_of_measurement": "%", "friendly_name": f"Sunsynk {sn} Battery Reserve Min"}, app="sunsynk")

            # Daily energy counters feed Predbat's load/import/export history learning.
            # They reset at midnight; minute_data/clean_incrementing_reverse absorb that.
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": f"Sunsynk {sn} {leaf.replace('_', ' ').title()}"},
                    app="sunsynk",
                )

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        # Deliberately NOT clamped to the inverter floor. This entity is Predbat's control
        # surface: it writes a value then reads it back to confirm (write_and_poll_value),
        # so publishing anything other than what was written guarantees a mismatch and a
        # retry storm. The floor is enforced at the API boundary in build_settings_payload.
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Sunsynk {sn} Battery Schedule Reserve", "icon": "mdi:gauge"},
            app="sunsynk",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes
            # Predbat replace these entities with its own dummies (inverter.py, the
            # inv_charge_time_format != "HH:MM:SS" branch) and the window never arrives.
            self.dashboard_item(
                self._control_name("select", sn, f"battery_schedule_{direction}_start_time"),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": f"Sunsynk {sn} {direction.title()} Start", "icon": "mdi:clock-outline"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("select", sn, f"battery_schedule_{direction}_end_time"),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": f"Sunsynk {sn} {direction.title()} End", "icon": "mdi:clock-outline"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("number", sn, f"battery_schedule_{direction}_soc"),
                state=int(window.get("soc", 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Sunsynk {sn} {direction.title()} SoC", "icon": "mdi:gauge"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("number", sn, f"battery_schedule_{direction}_power"),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": f"Sunsynk {sn} {direction.title()} Power", "icon": "mdi:flash"},
                app="sunsynk",
            )
            self.dashboard_item(
                self._control_name("switch", sn, f"battery_schedule_{direction}_enable"),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": f"Sunsynk {sn} {direction.title()} Enable", "icon": "mdi:check-circle-outline"},
                app="sunsynk",
            )
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": f"Sunsynk {sn} Schedule Write", "icon": "mdi:content-save"}, app="sunsynk")

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape control derivation consumes.

        Numeric casts route through _as_float so an entity legitimately reporting
        "unknown"/"unavailable" — for instance right after a HA restart, before Predbat
        republishes — falls back to 0 rather than raising and killing the control loop.
        """
        schedule = {"reserve": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, f"battery_schedule_{direction}_enable"), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), default="00:00:00"),
                "soc": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_soc"), default=0), 0)),
                "power": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_power"), default=0), 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule

    def _sn_from_entity(self, entity_id):
        """Extract the inverter serial from a Sunsynk entity id, or None if unresolvable.

        Entity ids are always {domain}.{prefix}_sunsynk_{sn}_{leaf}, so the serial is
        always followed by "_". Matching sn + "_" rather than a bare prefix keeps
        prefix-colliding serials apart — an entity for INV11 must never route to INV1,
        which would send a control write to the wrong inverter.
        """
        text = str(entity_id).lower()
        for sn in self.device_list:
            if f"_sunsynk_{sn.lower()}_" in text:
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
        schedule = self.local_schedule.setdefault(sn, {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}})
        leaf = str(entity_id).split(f"_sunsynk_{sn.lower()}_", 1)[-1]
        if leaf == "battery_schedule_reserve":
            schedule["reserve"] = int(self._as_float(value, 0))
            return
        for direction in ("charge", "export"):
            prefix = f"battery_schedule_{direction}_"
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

    async def apply_schedule(self, sn, force=False):
        """Apply the locally held schedule for one inverter."""
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        current_soc = int(self._as_float(self.device_values.get(sn, {}).get("soc"), 0))
        return await self.apply_settings(sn, schedule, current_soc, force=force)

    async def _handle_control_event(self, entity_id, value):
        """Route one control-entity event to the right inverter and apply it."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log(f"Warn: Sunsynk could not resolve an inverter for {entity_id}")
            return
        # The write button forces an apply; everything else updates state and lets the
        # normal diff-gated write in run() pick it up.
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                await self.apply_schedule(sn, force=True)
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

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

- [ ] **Step 4: Run to verify they pass, then commit**

```bash
cd coverage && ./run_all --test sunsynk_publish > /tmp/predbat_sunsynk_t6.log 2>&1; tail -20 /tmp/predbat_sunsynk_t6.log
```

Expected: all eight publish tests pass.

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_publish.py apps/predbat/unit_test.py
git commit -m "feat(sunsynk): publish monitoring sensors and schedule control entities

Sensors carry units and are omitted entirely when underivable, so Predbat
is never pointed at an entity that never appears. Control entities use
HH:MM:SS to match INVERTER_DEF, and the reserve entity deliberately
publishes what Predbat wrote rather than the inverter floor, to avoid a
write-read-mismatch retry storm."
```

---

### Task 7: Storage caching

**Files:**

- Modify: `apps/predbat/sunsynk.py`
- Create: `apps/predbat/tests/test_sunsynk_storage.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: `self.storage` from `ComponentBase`; the cache-name and TTL constants from Task 1.
- Produces: `async load_cache(name)`, `async save_cache(name, data)`, `async save_static()`, `async save_config()`, `async save_ratings()`, `async save_control()`, `async restore_state()`, `tier_expired(tier, ttl_minutes)`, `mark_refreshed(tier, age_minutes=0.0)`.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_sunsynk_storage.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk storage persistence
# -----------------------------------------------------------------------------

"""Tests for Sunsynk cache save, restore and per-tier age tracking."""

from sunsynk_const import SUNSYNK_CACHE_STATIC, SUNSYNK_CACHE_CONFIG, SUNSYNK_CACHE_RATINGS, SUNSYNK_CACHE_CONTROL, SUNSYNK_RESTORE_MAX_CONTROL, SUNSYNK_TTL_STATIC, SUNSYNK_TTL_LIVE
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


class FakeStorage:
    """In-memory stand-in for the Storage component, tracking each file's age."""

    def __init__(self, ages=None):
        """Start empty, with optional pre-set ages in minutes per cache name."""
        self.files = {}
        self.ages = ages or {}
        self.saves = []

    async def save(self, module, name, data):
        """Record a save."""
        self.files[name] = data
        self.saves.append(name)
        return True

    async def load(self, module, name, default=None):
        """Return previously saved data, or the default."""
        return self.files.get(name, default)

    async def age(self, module, name):
        """Return the configured age in minutes, or None when never written."""
        return self.ages.get(name)


class StoredSunsynk(MockSunsynk):
    """MockSunsynk with a fake Storage attached."""

    def __init__(self, ages=None, **kwargs):
        """Attach the fake storage."""
        super().__init__(**kwargs)
        self._storage = FakeStorage(ages=ages)

    @property
    def storage(self):
        """Return the fake storage."""
        return self._storage


def test_each_tier_saves_to_its_own_file():
    """One file per tier, so storage.age() gives each an independent clock."""
    failed = False
    s = StoredSunsynk()
    s.device_list = ["INV1"]
    s.device_detail = {"INV1": {"ratePower": 8000}}
    s.device_settings = {"INV1": {"batteryLowCap": "10"}}
    s.device_capacity = {"INV1": 14.3}
    s.device_rated_power = {"INV1": 8000.0}
    s.applied_payload = {"INV1": {"sysWorkMode": "1"}}
    run_async_local(s.save_static())
    run_async_local(s.save_config())
    run_async_local(s.save_ratings())
    run_async_local(s.save_control())
    for name in (SUNSYNK_CACHE_STATIC, SUNSYNK_CACHE_CONFIG, SUNSYNK_CACHE_RATINGS, SUNSYNK_CACHE_CONTROL):
        if name not in s.storage.files:
            print(f"ERROR: tier {name} was not saved")
            failed = True
    if s.storage.files.get(SUNSYNK_CACHE_STATIC, {}).get("device_list") != ["INV1"]:
        print(f"ERROR: static cache contents {s.storage.files.get(SUNSYNK_CACHE_STATIC)}")
        failed = True
    assert not failed, "test_each_tier_saves_to_its_own_file"


def test_restore_reinstates_static_and_config():
    """A restart restores discovery and settings without re-polling."""
    failed = False
    s = StoredSunsynk(ages={SUNSYNK_CACHE_STATIC: 5.0, SUNSYNK_CACHE_CONFIG: 3.0})
    s.storage.files[SUNSYNK_CACHE_STATIC] = {"device_list": ["INV1"], "device_detail": {"INV1": {"ratePower": 8000}}}
    s.storage.files[SUNSYNK_CACHE_CONFIG] = {"device_settings": {"INV1": {"batteryLowCap": "12"}}}
    run_async_local(s.restore_state())
    if s.device_list != ["INV1"]:
        print(f"ERROR: device_list not restored, got {s.device_list}")
        failed = True
    if s.battery_reserve_min("INV1") != 12:
        print(f"ERROR: settings not restored, floor is {s.battery_reserve_min('INV1')}")
        failed = True
    assert not failed, "test_restore_reinstates_static_and_config"


def test_control_cache_restore_is_time_bounded():
    """A stale applied-payload cache is discarded so the next write is forced.

    It is a change-detection cache with no read-back, so restoring it asserts the inverter
    still holds what Predbat last wrote. After a long outage that assertion is false, the
    next write would be wrongly skipped and the battery would silently diverge.
    """
    failed = False
    fresh = StoredSunsynk(ages={SUNSYNK_CACHE_CONTROL: SUNSYNK_RESTORE_MAX_CONTROL - 1})
    fresh.storage.files[SUNSYNK_CACHE_CONTROL] = {"applied_payload": {"INV1": {"sysWorkMode": "1"}}}
    run_async_local(fresh.restore_state())
    if fresh.applied_payload.get("INV1") != {"sysWorkMode": "1"}:
        print("ERROR: a fresh control cache should be restored")
        failed = True

    stale = StoredSunsynk(ages={SUNSYNK_CACHE_CONTROL: SUNSYNK_RESTORE_MAX_CONTROL + 1})
    stale.storage.files[SUNSYNK_CACHE_CONTROL] = {"applied_payload": {"INV1": {"sysWorkMode": "1"}}}
    run_async_local(stale.restore_state())
    if stale.applied_payload:
        print(f"ERROR: a stale control cache was restored: {stale.applied_payload}")
        failed = True
    assert not failed, "test_control_cache_restore_is_time_bounded"


def test_tier_expiry_uses_the_seeded_clock():
    """Tier clocks are seeded from storage age, so cadence survives a restart."""
    failed = False
    s = StoredSunsynk(ages={SUNSYNK_CACHE_STATIC: 10.0})
    s.storage.files[SUNSYNK_CACHE_STATIC] = {"device_list": ["INV1"]}
    run_async_local(s.restore_state())
    # 10 minutes old against an 8-hour TTL: not expired, so no re-poll on startup.
    if s.tier_expired("static", SUNSYNK_TTL_STATIC):
        print("ERROR: a 10-minute-old static tier should not be expired")
        failed = True
    # The live tier was never seeded, so it must be treated as expired.
    if not s.tier_expired("live", SUNSYNK_TTL_LIVE):
        print("ERROR: an unseeded tier must be expired so the first poll happens")
        failed = True
    s.mark_refreshed("live")
    if s.tier_expired("live", SUNSYNK_TTL_LIVE):
        print("ERROR: a just-refreshed tier should not be expired")
        failed = True
    assert not failed, "test_tier_expiry_uses_the_seeded_clock"


def test_telemetry_is_not_cached():
    """Live telemetry is never written to storage."""
    failed = False
    s = StoredSunsynk()
    s.device_list = ["INV1"]
    s.device_values = {"INV1": {"soc": 62}}
    s.device_energy = {"INV1": {"pv_today": 9.8}}
    run_async_local(s.save_static())
    run_async_local(s.save_ratings())
    blob = str(s.storage.files)
    if "\"soc\"" in blob or "'soc'" in blob:
        print("ERROR: telemetry leaked into a cache file")
        failed = True
    assert not failed, "test_telemetry_is_not_cached"


def run_sunsynk_storage_tests(my_predbat):
    """Run all Sunsynk storage tests."""
    failed = False
    for name, fn in [
        ("tier_files", test_each_tier_saves_to_its_own_file),
        ("restore_static_config", test_restore_reinstates_static_and_config),
        ("control_restore_bounded", test_control_cache_restore_is_time_bounded),
        ("tier_expiry", test_tier_expiry_uses_the_seeded_clock),
        ("telemetry_not_cached", test_telemetry_is_not_cached),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_storage.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_storage.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register and run to verify it fails**

Add `from tests.test_sunsynk_storage import run_sunsynk_storage_tests` and `("sunsynk_storage", run_sunsynk_storage_tests, "Sunsynk storage tests", False),` to `apps/predbat/unit_test.py`, then run `./run_all --test sunsynk_storage` as above. Expected: `AttributeError: ... 'save_static'`.

- [ ] **Step 3: Implement caching**

Add `import time` (already present) and the cache constants to the `sunsynk_const` imports, then append to `SunsynkAPI`:

```python
    async def load_cache(self, name):
        """Load one cache file, returning {} when absent or unreadable."""
        try:
            data = await self.storage.load(SUNSYNK_STORAGE_MODULE, name, default=None)
        except Exception as error:
            self.log(f"Warn: Sunsynk could not load cache {name}: {error}")
            return {}
        return data if isinstance(data, dict) else {}

    async def save_cache(self, name, data):
        """Save one cache file, tolerating a storage failure."""
        try:
            await self.storage.save(SUNSYNK_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log(f"Warn: Sunsynk could not save cache {name}: {error}")

    async def save_static(self):
        """Persist discovery results, which change only when the hardware does."""
        await self.save_cache(SUNSYNK_CACHE_STATIC, {"device_list": self.device_list, "device_detail": self.device_detail})

    async def save_config(self):
        """Persist the last-read settings object, the baseline for read-modify-write."""
        await self.save_cache(SUNSYNK_CACHE_CONFIG, {"device_settings": self.device_settings})

    async def save_ratings(self):
        """Persist derived ratings so automatic_config can map args at startup."""
        await self.save_cache(SUNSYNK_CACHE_RATINGS, {"device_rated_power": self.device_rated_power})

    async def save_control(self):
        """Persist the applied-payload cache used for write change detection."""
        await self.save_cache(SUNSYNK_CACHE_CONTROL, {"applied_payload": self.applied_payload})

    async def restore_state(self):
        """Restore cached state at startup and seed each tier's clock from its file age.

        Telemetry is deliberately not cached: the live tier polls every few minutes, Home
        Assistant already retains the last published value of every entity, and
        publish_data only writes a sensor when it has a value — so a failed poll leaves
        the previous reading in place rather than overwriting it.
        """
        if self._cache_restored:
            return
        self._cache_restored = True

        static = await self.load_cache(SUNSYNK_CACHE_STATIC)
        if static:
            self.device_list = static.get("device_list", []) or []
            self.device_detail = static.get("device_detail", {}) or {}
            age = await self.storage.age(SUNSYNK_STORAGE_MODULE, SUNSYNK_CACHE_STATIC)
            if age is not None:
                self.mark_refreshed("static", age)

        config = await self.load_cache(SUNSYNK_CACHE_CONFIG)
        if config:
            self.device_settings = config.get("device_settings", {}) or {}
            age = await self.storage.age(SUNSYNK_STORAGE_MODULE, SUNSYNK_CACHE_CONFIG)
            if age is not None:
                self.mark_refreshed("config", age)

        ratings = await self.load_cache(SUNSYNK_CACHE_RATINGS)
        if ratings:
            self.device_rated_power = ratings.get("device_rated_power", {}) or {}

        # Bounded: restoring this asserts the inverter still holds what Predbat last wrote.
        # A redundant write is cheap; a skipped one lets the battery diverge from the plan.
        control_age = await self.storage.age(SUNSYNK_STORAGE_MODULE, SUNSYNK_CACHE_CONTROL)
        if control_age is not None and control_age <= SUNSYNK_RESTORE_MAX_CONTROL:
            control = await self.load_cache(SUNSYNK_CACHE_CONTROL)
            self.applied_payload = control.get("applied_payload", {}) or {}
        elif control_age is not None:
            self.log(f"Info: Sunsynk control cache is {control_age:.1f} minutes old (limit {SUNSYNK_RESTORE_MAX_CONTROL}), forcing a rewrite")

    def tier_expired(self, tier, ttl_minutes):
        """Return True if a tier has never run or is older than its TTL."""
        last = self._tier_refreshed.get(tier)
        if last is None:
            return True
        return (time.time() - last) / 60.0 >= ttl_minutes

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Record that a tier just refreshed, or seed its clock from a cache age."""
        self._tier_refreshed[tier] = time.time() - (age_minutes * 60.0)
```

- [ ] **Step 4: Run, then commit**

```bash
cd coverage && ./run_all --test sunsynk_storage > /tmp/predbat_sunsynk_t7.log 2>&1; tail -20 /tmp/predbat_sunsynk_t7.log
```

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py apps/predbat/tests/test_sunsynk_storage.py apps/predbat/unit_test.py
git commit -m "feat(sunsynk): persist discovery, settings, ratings and control state

One file per tier so each gets an independent storage.age() clock, seeded
at startup so the refresh cadence survives a restart. Telemetry is not
cached; HA already retains the last published value.

The applied-payload cache restores only within a 15-minute window: it
asserts the inverter still holds what Predbat wrote, and after a longer
outage that assertion is false and the next write must not be skipped."
```
---

### Task 8: The run loop, registration and automatic configuration

**Files:**

- Modify: `apps/predbat/sunsynk.py`
- Modify: `apps/predbat/components.py`
- Modify: `apps/predbat/config.py`
- Create: `apps/predbat/tests/test_sunsynk_config.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**

- Consumes: everything from Tasks 3-7.
- Produces: `async automatic_config()`, `async refresh_static()`, `async refresh_config()`, `async refresh_live()`, `async run(seconds, first)`, `async final()`; `INVERTER_DEF["SunsynkCloud"]`; the `sunsynk` entry in `COMPONENT_LIST`; the `sunsynk_*` keys in `APPS_SCHEMA`.

- [ ] **Step 1: Write the failing tests**

Create `apps/predbat/tests/test_sunsynk_config.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk automatic configuration and registration
# -----------------------------------------------------------------------------

"""Tests for Sunsynk automatic_config, INVERTER_DEF and component registration."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from unittest.mock import patch
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


class ConfigSunsynk(MockSunsynk):
    """MockSunsynk that records set_arg calls."""

    def __init__(self, **kwargs):
        """Set up the recorder."""
        super().__init__(**kwargs)
        self.args_set = {}

    def set_arg(self, key, value):
        """Record an arg assignment."""
        self.args_set[key] = value


def test_inverter_def_registered():
    """SunsynkCloud exists and declares the capabilities the control model relies on."""
    failed = False
    definition = INVERTER_DEF.get("SunsynkCloud")
    if not definition:
        print("ERROR: INVERTER_DEF['SunsynkCloud'] is missing")
        assert False, "test_inverter_def_registered"
    expected = {
        "output_charge_control": "power",
        "charge_control_immediate": False,
        "has_charge_enable_time": True,
        "has_discharge_enable_time": True,
        "has_target_soc": True,
        "has_reserve_soc": True,
        "charge_time_format": "HH:MM:SS",
        "charge_time_entity_is_option": True,
        "soc_units": "%",
        "support_charge_freeze": True,
        "support_discharge_freeze": True,
        "can_span_midnight": False,
        "target_soc_used_for_discharge": True,
    }
    for key, value in expected.items():
        if definition.get(key) != value:
            print(f"ERROR: SunsynkCloud {key} = {definition.get(key)!r}, expected {value!r}")
            failed = True
    assert not failed, "test_inverter_def_registered"


def test_component_registered():
    """The sunsynk component is registered with its event filter and auth gate."""
    failed = False
    entry = COMPONENT_LIST.get("sunsynk")
    if not entry:
        print("ERROR: COMPONENT_LIST['sunsynk'] is missing")
        assert False, "test_component_registered"
    if entry.get("event_filter") != "predbat_sunsynk_":
        print(f"ERROR: event_filter {entry.get('event_filter')!r}")
        failed = True
    if entry.get("phase") != 1:
        print(f"ERROR: phase {entry.get('phase')!r}, expected 1")
        failed = True
    # Activation must be gated on having at least one usable auth path; every individual
    # arg is optional so either auth mode can be configured alone.
    if sorted(entry.get("required_or", [])) != ["key", "username"]:
        print(f"ERROR: required_or {entry.get('required_or')!r}")
        failed = True
    for arg in ("username", "password", "key", "region", "auth_method", "inverter_sn", "automatic", "control_enable"):
        if arg not in entry.get("args", {}):
            print(f"ERROR: component arg {arg} not registered")
            failed = True
    assert not failed, "test_component_registered"


def test_apps_schema_keys():
    """Every sunsynk_* config key is declared for apps.yaml validation."""
    failed = False
    expected = {
        "sunsynk_username": "string",
        "sunsynk_password": "string",
        "sunsynk_key": "string",
        "sunsynk_region": "string",
        "sunsynk_auth_method": "string",
        "sunsynk_token_expires_at": "string",
        "sunsynk_token_hash": "string",
        "sunsynk_inverter_sn": "string|string_list",
        "sunsynk_automatic": "boolean",
        "sunsynk_automatic_ignore_pv": "boolean",
        "sunsynk_control_enable": "boolean",
        "sunsynk_battery_nominal_voltage": "float",
    }
    for key, kind in expected.items():
        entry = APPS_SCHEMA.get(key)
        if not entry:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
        elif entry.get("type") != kind:
            print(f"ERROR: {key} type {entry.get('type')!r}, expected {kind!r}")
            failed = True
    assert not failed, "test_apps_schema_keys"


def test_automatic_config_maps_control_entities():
    """Every inverter is registered as SunsynkCloud with its sensors and controls."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    for sn in s.device_list:
        s.device_values[sn] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
        s.device_energy[sn] = {"pv_today": 1.0, "import_today": 1.0, "export_today": 1.0, "load_today": 1.0, "battery_charge_today": 1.0, "battery_discharge_today": 1.0}
        s.device_rated_power[sn] = 8000.0
        s.device_settings[sn] = {"batteryLowCap": "10"}
    run_async_local(s.automatic_config())
    if s.args_set.get("inverter_type") != ["SunsynkCloud", "SunsynkCloud"]:
        print(f"ERROR: inverter_type {s.args_set.get('inverter_type')}")
        failed = True
    if s.args_set.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {s.args_set.get('num_inverters')}")
        failed = True
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "battery_rate_max", "inverter_limit", "battery_min_soc"):
        if arg not in s.args_set:
            print(f"ERROR: sensor arg {arg} not mapped")
            failed = True
    for arg in ("reserve", "charge_start_time", "charge_end_time", "charge_limit", "charge_rate", "scheduled_charge_enable", "discharge_start_time", "discharge_end_time", "discharge_target_soc", "discharge_rate", "scheduled_discharge_enable", "schedule_write_button"):
        if arg not in s.args_set:
            print(f"ERROR: control arg {arg} not mapped")
            failed = True
    # Control args must point at the control entities, not sensors.
    if not str(s.args_set.get("charge_start_time", [""])[0]).startswith("select."):
        print(f"ERROR: charge_start_time should be a select entity, got {s.args_set.get('charge_start_time')}")
        failed = True
    assert not failed, "test_automatic_config_maps_control_entities"


def test_automatic_config_skips_partial_capabilities():
    """An arg is only mapped when every inverter reports the underlying value."""
    failed = False
    s = ConfigSunsynk()
    s.device_list = ["INV1", "INV2"]
    # INV2 has no chargeVolt, so its capacity and rate cannot be derived.
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_values["INV2"] = {"soc": 50, "capacity": 280, "maxChargeCurrentLimit": 100}
    s.device_energy = {"INV1": {"pv_today": 1.0}, "INV2": {}}
    s.device_rated_power = {"INV1": 8000.0}
    run_async_local(s.automatic_config())
    for arg in ("soc_max", "battery_rate_max", "inverter_limit", "pv_today"):
        if arg in s.args_set:
            print(f"ERROR: {arg} was mapped although not every inverter reports it")
            failed = True
    if not any("manually" in str(m) for m in s.log_messages):
        print("ERROR: skipping an arg should warn the user to set it in apps.yaml")
        failed = True
    assert not failed, "test_automatic_config_skips_partial_capabilities"


def test_automatic_config_respects_ignore_pv():
    """automatic_ignore_pv leaves the PV args for another component to own."""
    failed = False
    s = ConfigSunsynk()
    s.automatic_ignore_pv = True
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100}
    s.device_energy["INV1"] = {"pv_today": 1.0}
    s.device_rated_power["INV1"] = 8000.0
    run_async_local(s.automatic_config())
    for arg in ("pv_power", "pv_today"):
        if arg in s.args_set:
            print(f"ERROR: {arg} mapped despite automatic_ignore_pv")
            failed = True
    if "soc_percent" not in s.args_set:
        print("ERROR: non-PV args should still be mapped")
        failed = True
    assert not failed, "test_automatic_config_respects_ignore_pv"


def test_run_first_cycle_polls_and_publishes():
    """The first run restores caches, discovers, polls and publishes."""
    failed = False
    s = ConfigSunsynk()
    calls = []

    async def fake_restore():
        """Record the cache restore."""
        calls.append("restore")

    async def fake_token():
        """Record the login."""
        calls.append("token")
        return True

    async def fake_device_list():
        """Record discovery and return one inverter."""
        calls.append("discover")
        s.device_list = ["INV1"]
        return ["INV1"]

    async def fake_detail(sn):
        """Record the detail fetch."""
        calls.append("detail")
        return {"ratePower": 8000}

    async def fake_device_data(sn):
        """Record the telemetry poll."""
        calls.append("telemetry")
        s.device_values[sn] = {"soc": 50}
        return {"soc": 50}

    async def fake_settings(sn):
        """Record the settings read."""
        calls.append("settings")
        return {"batteryLowCap": "10"}

    async def fake_publish():
        """Record publishing."""
        calls.append("publish")

    with (
        patch.object(s, "restore_state", side_effect=fake_restore),
        patch.object(s, "fetch_token", side_effect=fake_token),
        patch.object(s, "get_device_list", side_effect=fake_device_list),
        patch.object(s, "fetch_device_detail", side_effect=fake_detail),
        patch.object(s, "fetch_device_data", side_effect=fake_device_data),
        patch.object(s, "fetch_settings", side_effect=fake_settings),
        patch.object(s, "publish_data", side_effect=fake_publish),
    ):
        run_async_local(s.run(0, True))
    for step in ("restore", "discover", "telemetry", "publish"):
        if step not in calls:
            print(f"ERROR: first cycle never did {step}; calls were {calls}")
            failed = True
    assert not failed, "test_run_first_cycle_polls_and_publishes"


def run_sunsynk_config_tests(my_predbat):
    """Run all Sunsynk configuration tests."""
    failed = False
    for name, fn in [
        ("inverter_def", test_inverter_def_registered),
        ("component_registered", test_component_registered),
        ("apps_schema", test_apps_schema_keys),
        ("automatic_config", test_automatic_config_maps_control_entities),
        ("partial_capabilities", test_automatic_config_skips_partial_capabilities),
        ("ignore_pv", test_automatic_config_respects_ignore_pv),
        ("run_first_cycle", test_run_first_cycle_polls_and_publishes),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
```

- [ ] **Step 2: Register and run to verify it fails**

Add `from tests.test_sunsynk_config import run_sunsynk_config_tests` and `("sunsynk_config", run_sunsynk_config_tests, "Sunsynk config/INVERTER_DEF tests", False),` to `apps/predbat/unit_test.py`, then run `./run_all --test sunsynk_config`. Expected: `INVERTER_DEF['SunsynkCloud'] is missing`.

- [ ] **Step 3: Add the inverter definition**

In `apps/predbat/config.py`, immediately after the `"DeyeCloud"` entry (around line 2191), add. The flags are identical to DeyeCloud because the same registers sit behind both clouds:

```python
    "SunsynkCloud": {
        "name": "SunsynkCloud",
        "has_rest_api": False,
        "has_mqtt_api": False,
        "output_charge_control": "power",
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

- [ ] **Step 4: Add the APPS_SCHEMA keys**

In `apps/predbat/config.py`, next to the `deye_*` keys (around line 2492), add:

```python
    "sunsynk_username": {"type": "string", "empty": False},
    "sunsynk_password": {"type": "string", "empty": False},
    "sunsynk_key": {"type": "string", "empty": False},
    "sunsynk_region": {"type": "string", "empty": False},
    "sunsynk_auth_method": {"type": "string", "empty": False},
    "sunsynk_token_expires_at": {"type": "string", "empty": False},
    "sunsynk_token_hash": {"type": "string", "empty": False},
    "sunsynk_inverter_sn": {"type": "string|string_list", "empty": False},
    "sunsynk_automatic": {"type": "boolean"},
    "sunsynk_automatic_ignore_pv": {"type": "boolean"},
    "sunsynk_control_enable": {"type": "boolean"},
    "sunsynk_battery_nominal_voltage": {"type": "float"},
```

- [ ] **Step 5: Register the component**

In `apps/predbat/components.py`, add `from sunsynk import SunsynkAPI` beside `from deye import DeyeAPI`, and add this entry after `"deye"`:

```python
    "sunsynk": {
        "class": SunsynkAPI,
        "name": "Sunsynk Cloud",
        "event_filter": "predbat_sunsynk_",
        "args": {
            "username": {"required": False, "config": "sunsynk_username"},
            "password": {"required": False, "config": "sunsynk_password"},
            # In oauth mode OAuthMixin assigns 'key' straight to access_token (see
            # oauth_mixin._init_oauth). Predbat.com injects the access token as
            # sunsynk_key; without this entry it is dropped and every call is rejected.
            "key": {"required": False, "config": "sunsynk_key"},
            "region": {"required": False, "default": "sunsynk", "config": "sunsynk_region"},
            "auth_method": {"required": False, "default": "password", "config": "sunsynk_auth_method"},
            "token_expires_at": {"required": False, "config": "sunsynk_token_expires_at"},
            "token_hash": {"required": False, "config": "sunsynk_token_hash"},
            "inverter_sn": {"required": False, "config": "sunsynk_inverter_sn"},
            "automatic": {"required": False, "default": False, "config": "sunsynk_automatic"},
            "automatic_ignore_pv": {"required": False, "default": False, "config": "sunsynk_automatic_ignore_pv"},
            # Off by default: the write format is inferred from third-party clients and
            # has not been confirmed against live hardware. Monitoring needs no opt-in.
            "control_enable": {"required": False, "default": False, "config": "sunsynk_control_enable"},
            # Battery capacity arrives in amp-hours, so it needs a pack voltage to become
            # kWh. Normally inferred from the BMS charge target; this is the escape hatch
            # for a pack that does not report one.
            "battery_nominal_voltage": {"required": False, "config": "sunsynk_battery_nominal_voltage"},
        },
        # Gate activation on having at least one auth path — a username (self-hosted) OR
        # an injected SaaS access token. Without this the component would start for every
        # instance, since all individual args are optional to allow either auth mode.
        "required_or": ["username", "key"],
        "phase": 1,
    },
```

- [ ] **Step 6: Implement automatic_config and the run loop**

Append to `SunsynkAPI` in `apps/predbat/sunsynk.py`:

```python
    async def automatic_config(self):
        """Register every discovered inverter as a SunsynkCloud Predbat inverter."""
        devices = [sn.lower() for sn in self.device_list]
        if not devices:
            self.log("Warn: Sunsynk automatic_config found no inverters")
            return
        self.set_arg("inverter_type", ["SunsynkCloud" for _ in devices])
        self.set_arg("num_inverters", len(devices))
        self.set_arg("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        self.set_arg("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        self.set_arg("battery_temperature", [self._sensor_name(sn, "temperature") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])

        # Only map an arg when EVERY inverter reports the underlying value. An arg aimed at
        # a sensor that is never published is worse than an absent arg, which the user can
        # fill in via apps.yaml.
        for leaf in SUNSYNK_ENERGY:
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in self.device_list):
                self.set_arg(leaf, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log(f"Warn: Sunsynk not every inverter reports {leaf}, it must be set manually in apps.yaml")

        if all(self.battery_capacity(sn) > 0 for sn in self.device_list):
            self.set_arg("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: Sunsynk no battery capacity available for every inverter, soc_max must be set manually in apps.yaml")
        if all(self.battery_rate_max(sn) > 0 for sn in self.device_list):
            self.set_arg("battery_rate_max", [self._sensor_name(sn, "battery_rate_max") for sn in devices])
        else:
            self.log("Warn: Sunsynk no battery charge-current limit available, battery_rate_max must be set manually in apps.yaml")
        if all(self.device_rated_power.get(sn, 0.0) > 0 for sn in self.device_list):
            self.set_arg("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: Sunsynk no ratePower reported, inverter_limit must be set manually in apps.yaml")
        if all(self.battery_reserve_min(sn) > 0 for sn in self.device_list):
            self.set_arg("battery_min_soc", [self._sensor_name(sn, "battery_reserve_min") for sn in devices])

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

    async def refresh_static(self):
        """Re-discover inverters and refresh their static detail."""
        await self.get_device_list()
        for sn in self.device_list:
            await self.fetch_device_detail(sn)
        self.mark_refreshed("static")
        await self.save_static()
        await self.save_ratings()

    async def refresh_config(self):
        """Re-read the settings object for every inverter."""
        for sn in self.device_list:
            settings = await self.fetch_settings(sn)
            # Track whether the inverter has caught up with the last write. Latency of a
            # few minutes is normal; persistent divergence is worth surfacing.
            self.note_settle(sn, settings)
        self.mark_refreshed("config")
        await self.save_config()

    async def refresh_live(self):
        """Poll telemetry for every inverter."""
        for sn in self.device_list:
            await self.fetch_device_data(sn)
        self.mark_refreshed("live")

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and apply any schedule change."""
        if first:
            await self.restore_state()
            if not await self.fetch_token():
                self.log("Warn: Sunsynk login failed, the component will retry on the next cycle")
                return

        if self.tier_expired("static", SUNSYNK_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: Sunsynk found no inverters on this account")
            return
        if self.tier_expired("config", SUNSYNK_TTL_CONFIG):
            await self.refresh_config()
        if self.tier_expired("live", SUNSYNK_TTL_LIVE):
            await self.refresh_live()

        for sn in self.device_list:
            if first:
                # Seed the control entities before Predbat reads them, so the first plan
                # does not act on entities that do not exist yet.
                self.local_schedule.setdefault(sn, {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}})
                await self.publish_schedule_settings_ha(sn)
            else:
                await self.get_schedule_settings_ha(sn)
                if await self.apply_schedule(sn):
                    await self.save_control()

        await self.publish_data()
        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()

    async def final(self):
        """Persist state on shutdown so a restart resumes without re-polling."""
        await self.save_static()
        await self.save_config()
        await self.save_ratings()
        await self.save_control()
```

Add the TTL names to the `sunsynk_const` imports: `SUNSYNK_TTL_STATIC`, `SUNSYNK_TTL_CONFIG`, `SUNSYNK_TTL_LIVE`, `SUNSYNK_STORAGE_MODULE`, `SUNSYNK_CACHE_STATIC`, `SUNSYNK_CACHE_CONFIG`, `SUNSYNK_CACHE_RATINGS`, `SUNSYNK_CACHE_CONTROL`, `SUNSYNK_RESTORE_MAX_CONTROL`.

- [ ] **Step 7: Run the whole Sunsynk suite, then commit**

```bash
cd coverage && ./run_all -k sunsynk > /tmp/predbat_sunsynk_all.log 2>&1; tail -40 /tmp/predbat_sunsynk_all.log
```

Expected: all seven Sunsynk suites pass. Then confirm nothing else regressed:

```bash
cd coverage && ./run_all --quick > /tmp/predbat_quick.log 2>&1; tail -30 /tmp/predbat_quick.log
```

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py apps/predbat/components.py apps/predbat/config.py apps/predbat/tests/test_sunsynk_config.py apps/predbat/unit_test.py
git commit -m "feat(sunsynk): add the run loop, registration and automatic configuration

Registers SunsynkCloud in INVERTER_DEF (identical capability flags to
DeyeCloud, because the same registers sit behind both clouds), adds the
sunsynk_* apps.yaml schema, and wires the component into COMPONENT_LIST
gated on having at least one usable auth path.

automatic_config maps an arg only when every discovered inverter reports
the underlying value, warning the user to set the rest in apps.yaml."
```

---

### Task 9: Standalone CLI and documentation

Nobody on the project has a Sunsynk account, so this task is what makes remote verification possible: a tester can dump raw traffic without running Predbat at all.

**Files:**

- Modify: `apps/predbat/sunsynk.py` (append the CLI)
- Modify: `docs/components.md`
- Modify: `docs/inverter-setup.md`
- Modify: `docs/apps-yaml.md`

**Interfaces:**

- Consumes: the whole component.
- Produces: `python3 sunsynk.py --username ... --password ...` with `--auth-method`, `--region`, `--serial`, `--dump-settings`, `--write-test`.

- [ ] **Step 1: Append the CLI entry point**

At the end of `apps/predbat/sunsynk.py`, following **deye.py's harness pattern** (`deye.py:1435`) — add `import argparse` and `from mock_base import MockBase` to the imports.

`MockBase` is the *base object* a component is constructed around, **not** a mixin. Do not inherit
from it: that would put `ComponentBase` ahead of `MockBase` in the MRO, so `get_state_wrapper` and
`dashboard_item` would resolve to `ComponentBase`'s versions and hit an unset `self.base`. Building
the component around a `MockBase()` instance also gives the documented standalone behaviour — its
`components` is None, so `ComponentBase.storage` resolves to None and the disk cache is skipped.

```python
def _build_sunsynk(mock_base, args):  # pragma: no cover
    """Construct a SunsynkAPI around a MockBase for standalone command-line use."""
    client = SunsynkAPI(mock_base)
    client.initialize(
        username=args.username,
        password=args.password,
        region=args.region,
        auth_method=args.auth_method,
        # The CLI is the verification tool, so control is on — but run_cli still asks
        # before it sends anything to a real inverter.
        control_enable=True,
        automatic=False,
    )
    return client


async def run_cli(args):  # pragma: no cover
    """Log in, dump what the account exposes, and optionally round-trip one write."""
    mock_base = MockBase()
    client = _build_sunsynk(mock_base, args)
    print(f"Region {args.region} -> {client.base_url} (source={client.source}), auth={args.auth_method}")
    if not await client.fetch_token():
        print("Login FAILED. If your region still serves the older login, retry with --auth-method password_legacy.")
        return
    serials = [args.serial] if args.serial else await client.get_device_list()
    print(f"Inverters: {serials}")
    for sn in serials:
        client.device_list = [sn]
        print(f"\n--- {sn} detail ---")
        print(json.dumps(await client.fetch_device_detail(sn), indent=2, default=str))
        print(f"\n--- {sn} telemetry ---")
        print(json.dumps(await client.fetch_device_data(sn), indent=2, default=str))
        settings = await client.fetch_settings(sn)
        if args.dump_settings:
            print(f"\n--- {sn} settings ---")
            print(json.dumps(settings, indent=2, default=str))
        print(f"\nDerived: capacity={client.battery_capacity(sn):.2f} kWh, rate_max={client.battery_rate_max(sn):.0f} W, floor={client.battery_reserve_min(sn)}%")
        if args.write_test:
            # A deliberately harmless schedule: a self-use day at the inverter's own floor.
            schedule = {"reserve": max(client.battery_reserve_min(sn), 10), "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
            payload = client.build_settings_payload(sn, schedule, current_soc=50)
            print(f"\n--- {sn} would write ---")
            print(json.dumps(payload, indent=2, default=str))
            confirm = input("Send this to the inverter? [y/N] ")
            if confirm.strip().lower() == "y":
                await client.apply_settings(sn, schedule, current_soc=50, force=True)
                print("Written. Re-reading in 60 seconds is the only way to confirm the dongle collected it.")


def main():
    """Command-line entry point for Sunsynk diagnostics."""
    parser = argparse.ArgumentParser(description="Sunsynk Cloud API diagnostics")
    parser.add_argument("--username", required=True, help="Sunsynk Connect account e-mail")
    parser.add_argument("--password", required=True, help="Sunsynk Connect account password")
    parser.add_argument("--region", default="sunsynk", choices=sorted(SUNSYNK_REGIONS), help="API region")
    parser.add_argument("--auth-method", default="password", choices=["password", "password_legacy"], help="Login flow: RSA-encrypted (default) or the pre-2025 plaintext one")
    parser.add_argument("--serial", default=None, help="Restrict to one inverter serial")
    parser.add_argument("--dump-settings", action="store_true", help="Print the full settings object")
    parser.add_argument("--write-test", action="store_true", help="Build a harmless self-use payload and offer to send it")
    asyncio.run(run_cli(parser.parse_args()))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the CLI parses and imports cleanly**

```bash
cd apps/predbat && python3 sunsynk.py --help > /tmp/predbat_sunsynk_cli.log 2>&1; cat /tmp/predbat_sunsynk_cli.log
```

Expected: the argparse help text, with no import errors.

- [ ] **Step 3: Document the component**

In `docs/components.md`, add a "Sunsynk Cloud API (sunsynk)" section following the DEYE section's structure (see line 763), with a configuration table covering every arg from Task 8 Step 5, and these notes:

- Three auth methods: `password` (RSA-encrypted, the default), `password_legacy` (the pre-2025 plaintext login, for regions still serving it) and `oauth` (Predbat.com injects the token).
- `password` never falls back to `password_legacy` automatically; choosing the plaintext login is deliberate.
- `sunsynk_control_enable` defaults to `false`. Monitoring works immediately; inverter writes need an explicit opt-in until the wire format is confirmed against live hardware.
- Settings changes reach the inverter via the dongle's next poll, typically one to five minutes after Predbat writes them.
- Using the Sunsynk phone app while Predbat is running can overwrite Predbat's settings, and vice versa.

Add the doc to the table of contents at the top of the file, add a Sunsynk Cloud row to the inverter table in `docs/inverter-setup.md` (line 42) plus a setup walkthrough section, and add the `sunsynk_*` keys to `docs/apps-yaml.md`. A minimal working configuration to include:

```yaml
  sunsynk_username: 'you@example.com'
  sunsynk_password: 'your-password'
  sunsynk_region: 'sunsynk'
  sunsynk_automatic: true
  sunsynk_control_enable: false
```

- [ ] **Step 4: Build the docs and commit**

```bash
mkdocs build --strict > /tmp/predbat_mkdocs.log 2>&1; tail -20 /tmp/predbat_mkdocs.log
```

```bash
./run_pre_commit
git add apps/predbat/sunsynk.py docs/
git commit -m "feat(sunsynk): add the diagnostics CLI and documentation

The CLI logs in, dumps detail, telemetry and the settings object, and can
build a harmless self-use payload and offer to send it. --auth-method lets
a tester establish which login their region serves in one command.

Nobody on the project has a Sunsynk account, so this is the tool that makes
remote verification of the inferred wire format possible."
```

---

## Verification checklist for the first tester

Work through these in order. Stop and report if any step disagrees with what is written here — every one of them is an inferred value, not a documented one.

1. **Login.** `python3 sunsynk.py --username ... --password ...`. If it fails, retry with `--auth-method password_legacy`. Report which one worked and for which region. (Resolves unknown 6.)
2. **Telemetry.** Check the dumped `soc`, `battery_power`, `grid_power`, `load_power` and `pv_power` against the Sunsynk app. In particular note **whether `battery_power` is positive while charging or while discharging** — the sign convention is unknown 4 and getting it wrong inverts Predbat's whole model.
3. **Ratings.** Confirm the derived `capacity` in kWh matches the real battery, and whether `capacity` is per-battery or the pack total on a multi-battery install. (Unknown 5.)
4. **Settings.** `--dump-settings`. Confirm `sysWorkMode`'s current value against the mode shown in the app, which establishes the enum ordering. (Unknown 1 — the highest-cost one.) Note whether `timeNon` and the day flags come back as bare booleans or as strings. (Unknown 2.)
5. **Write.** `--write-test`, which builds a self-use-at-floor payload and shows it before sending. Confirm the inverter still looks correct in the app afterwards, and how long it took to appear. (Unknown 3 and the latency figure.)

Only once steps 1-4 are confirmed should `sunsynk_control_enable: true` be documented as safe.

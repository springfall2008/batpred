# myenergi Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `myenergi` Predbat component that monitors Zappi and Eddi devices over either of myenergi's two APIs, auto-wires their energy sensors into `car_charging_energy` and `iboost_energy_today`, and exposes send-boost / cancel-boost controls.

**Architecture:** One module `apps/predbat/myenergi.py` holding a `MyEnergiAPI(ComponentBase, OAuthMixin)` component over a `MyEnergiTransport` abstraction with two implementations — `MyEnergiDirectTransport` (HTTP digest against `director.myenergi.net`, `/cgi-*` endpoints, the default) and `MyEnergiCloudTransport` (bearer JWT against `api.s18.myenergi.net`, REST endpoints). Both normalise to a shared `MyEnergiDevice` dataclass, so publishing, auto-config, controls and tests are written once.

**Tech Stack:** Python 3, `aiohttp` (including `aiohttp.DigestAuthMiddleware`), `dataclasses`, `abc`, existing Predbat infrastructure (`ComponentBase`, `OAuthMixin`, `MockBase`, `dashboard_item`, `set_arg_auto`), `unittest.mock` for tests.

**Spec:** `docs/superpowers/specs/2026-08-23-myenergi-integration-design.md`

## Global Constraints

- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage required (`interrogate`) — every function, method and class needs one, including nested helpers and test functions.
- **Spelling:** British English (`en-gb`) via CSpell. New words go in `.cspell/custom-dictionary-workspace.txt`, which is auto-sorted on commit, so re-stage after running pre-commit. `Eddi`, `myenergi` and `zappi` are already present; `libbi`, `jstatus`, `jdayhour`, `harvi` and `asn` are not.
- **Variable naming:** `lower_case_with_underscores`.
- **aiohttp floor:** `aiohttp.DigestAuthMiddleware` and the `ClientSession(middlewares=...)` parameter require **aiohttp >= 3.12**. `requirements.txt` currently lists `aiohttp` unpinned and must be changed to `aiohttp>=3.12`.
- **Test invocation:** always redirect test output to a file and grep the file afterwards — never pipe straight to grep. Use the scratchpad directory for output files.
- **Shared fixture:** Predbat tests share one `my_predbat` fixture. Never mutate it in a way that leaks into later tests; construct components against `MockBase` wherever a full Predbat instance is not needed.
- **Every new code path needs a unit test** (repository rule in `CLAUDE.md`).
- **Pre-commit:** `./run_pre_commit` must pass before any commit.
- **Commit messages:** descriptive sentence style matching repository history (e.g. "Retain the last good GE Cloud reading when leaf values come back null"), ending with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  ```
- **Branch:** all work happens on a feature branch off `main`, not on `main` itself.

### Reference: myenergi wire formats

Direct API (`director.myenergi.net`, digest auth, username = hub serial, password = API key):

| Purpose | Path |
|---|---|
| ASN discovery | `GET /cgi-jstatus-E` against `https://director.myenergi.net` |
| All device status | `GET /cgi-jstatus-*` |
| Zappi manual boost | `GET /cgi-zappi-mode-Z{serial}-0-10-{kwh}-0000` |
| Zappi smart boost | `GET /cgi-zappi-mode-Z{serial}-0-11-{kwh}-{hhmm}` |
| Zappi cancel boost | `GET /cgi-zappi-mode-Z{serial}-0-2-0-0000` |
| Eddi boost | `GET /cgi-eddi-boost-E{serial}-10-{target}-{minutes}` |
| Eddi cancel boost | `GET /cgi-eddi-boost-E{serial}-1-{target}-0` |

`/cgi-jstatus-*` returns a **list of single-key dicts**, e.g.
`[{"eddi": [{...}]}, {"zappi": [{...}]}, {"harvi": [...]}, {"asn": "s18.myenergi.net"}, {"fwv": "3560S5.036"}]`.

Every response carries an `X_MYENERGI-asn` header naming the real host. Its absence means bad credentials.

Cloud API (`https://api.s18.myenergi.net`, `Authorization: Bearer <jwt>`):

| Purpose | Path |
|---|---|
| Device list | `GET /devices` → `{"sites": [{"devices": [{"deviceId", "model", "alias", "serialNumber", "online", ...}]}]}` |
| Device status | `GET /devices/{id}/status` |
| Send boost | `POST /devices/{id}/boost` |
| Cancel boost | `DELETE /devices/{id}/boost` |

Zappi boost body: `{"mode": "normal", "parameters": {"energy": N}}` (N is 1–99 kWh) or `{"mode": "smart", "parameters": {"energy": N, "targetTime": "<ISO-8601>"}}`.
Eddi boost body: `{"durationMinutes": M}` (M is 0–240). Cross-sending these fields is a 400.

---

### Task 1: Module foundation — constants, device model and normalisers

**Files:**
- Create: `apps/predbat/myenergi.py`
- Create: `apps/predbat/tests/test_myenergi.py`
- Modify: `apps/predbat/unit_test.py` (import near line 226, `TEST_REGISTRY` entry near line 540)
- Modify: `.cspell/custom-dictionary-workspace.txt`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MyEnergiError`, `MyEnergiAuthError(MyEnergiError)`, `MyEnergiApiError(MyEnergiError)` — exception classes.
  - `MyEnergiDevice` — frozen-by-convention dataclass, fields listed in step 3.
  - `normalise_direct_device(raw: dict, kind: str) -> MyEnergiDevice`
  - `normalise_cloud_device(raw: dict, meta: dict) -> MyEnergiDevice`
  - Constants: `MYENERGI_DIRECTOR_URL`, `MYENERGI_CLOUD_URL`, `DEVICE_KIND_ZAPPI`, `DEVICE_KIND_EDDI`, `SUPPORTED_KINDS`, `DIRECT_PREFIX`, `CLOUD_PREFIX`, `ZAPPI_CHARGE_MODES`, `ZAPPI_STATES`, `EDDI_STATES`, `ZAPPI_PLUG_STATES`, `EDDI_BOOST_TARGETS`, `CLOUD_MODE_TO_NAME`, `CLOUD_STATUS_TO_NAME`, `API_TIMEOUT`, `USER_AGENT`.
  - `test_myenergi(my_predbat=None) -> bool` — returns `False` on success, matching `test_axle`.

- [ ] **Step 1: Create the module with its header, imports and constants**

Create `apps/predbat/myenergi.py`:

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# myenergi API library.
# Supports both the direct "director" API (digest auth, /cgi-* endpoints) that
# pymyenergi and the ha-myenergi integration use, and the official 3rd party API
# documented at https://api-docs.s18.myenergi.net/
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""myenergi Zappi and Eddi integration.

Provides monitoring of myenergi Zappi EV chargers and Eddi hot water diverters,
automatic wiring of their energy sensors into Predbat's car charging and iboost
inputs, and send/cancel boost controls. Two interchangeable transports cover the
two myenergi APIs: a direct digest-authenticated transport that any myenergi owner
can configure today, and a bearer-token transport for the official 3rd party API.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import argparse
import asyncio

import aiohttp

from component_base import ComponentBase
from mock_base import MockBase
from oauth_mixin import OAuthMixin
from predbat_metrics import record_api_call

MYENERGI_DIRECTOR_URL = "https://director.myenergi.net"
MYENERGI_CLOUD_URL = "https://api.s18.myenergi.net"

API_TIMEOUT = 30
USER_AGENT = "Wget/1.14 (linux-gnu)"

DEVICE_KIND_ZAPPI = "zappi"
DEVICE_KIND_EDDI = "eddi"
SUPPORTED_KINDS = (DEVICE_KIND_ZAPPI, DEVICE_KIND_EDDI)

# Device id prefixes. The direct API uses a single letter, the cloud API two letters.
DIRECT_PREFIX = {DEVICE_KIND_ZAPPI: "Z", DEVICE_KIND_EDDI: "E"}
CLOUD_PREFIX = {DEVICE_KIND_ZAPPI: "ZA", DEVICE_KIND_EDDI: "ED"}

# Index tables used by the direct API's numeric status fields.
ZAPPI_CHARGE_MODES = ["None", "Fast", "Eco", "Eco+", "Stopped"]
ZAPPI_STATES = ["Unknown", "Paused", "Unknown", "Charging", "Boosting", "Completed"]
EDDI_STATES = ["Unknown", "Paused", "Unknown", "Diverting", "Boosting", "Max temp reached", "Stopped"]

ZAPPI_PLUG_STATES = {
    "A": "EV Disconnected",
    "B1": "EV Connected",
    "B2": "Waiting for EV",
    "C1": "EV ready to charge",
    "C2": "Charging",
    "D1": "EV ready to charge",
    "D2": "Charging",
    "F": "Fault",
}

EDDI_BOOST_TARGETS = {"heater1": 1, "heater2": 2, "relay1": 11, "relay2": 12}
EDDI_DEFAULT_BOOST_TARGET = "heater1"

# The cloud API reports modes and statuses as strings. These maps translate them into
# the same vocabulary the direct API's index tables produce, so both transports emit
# identical MyEnergiDevice values for equivalent device states.
CLOUD_MODE_TO_NAME = {"fast": "Fast", "eco": "Eco", "eco+": "Eco+", "stop": "Stopped"}

CLOUD_STATUS_TO_NAME = {
    "ev_not_connected": "Paused",
    "waiting_for_surplus": "Paused",
    "waiting_for_ev": "Paused",
    "charge_delayed": "Paused",
    "smart_charge_delay": "Paused",
    "charge_complete": "Completed",
    "charging": "Charging",
    "boosting": "Boosting",
    "stopped": "Stopped",
    "diverting": "Diverting",
    "hot": "Max temp reached",
    "starting": "Paused",
    "dsr": "Paused",
}

# Zappi boost energy limits, from the 3rd party API schema. The direct API accepts the
# same range in practice, so both transports validate against these.
BOOST_ENERGY_MIN = 1
BOOST_ENERGY_MAX = 99
BOOST_MINUTES_MIN = 0
BOOST_MINUTES_MAX = 240

# Boosting a Zappi is only accepted while it is in one of the green-energy modes.
ZAPPI_BOOSTABLE_MODES = ("Eco", "Eco+")


class MyEnergiError(Exception):
    """Base class for every myenergi transport failure."""


class MyEnergiAuthError(MyEnergiError):
    """Raised when myenergi rejects the supplied credentials."""


class MyEnergiApiError(MyEnergiError):
    """Raised when a myenergi request fails for a non-authentication reason."""
```

- [ ] **Step 2: Write the failing normalisation tests**

Create `apps/predbat/tests/test_myenergi.py`:

```python
# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the myenergi Zappi and Eddi integration
"""

import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from myenergi import (
    DEVICE_KIND_EDDI,
    DEVICE_KIND_ZAPPI,
    normalise_cloud_device,
    normalise_direct_device,
)

# ============================================================================
# Mock data constants
# ============================================================================

# One entry from the "zappi" group of a direct /cgi-jstatus-* response
MOCK_DIRECT_ZAPPI = {
    "sno": 12345678,
    "sta": 3,
    "zmo": 2,
    "pst": "C2",
    "div": 7360,
    "che": 4.25,
    "grd": 120,
    "gen": 3400,
    "vol": 2405,
    "frq": 50.02,
}

# One entry from the "eddi" group of a direct /cgi-jstatus-* response
MOCK_DIRECT_EDDI = {
    "sno": 87654321,
    "sta": 3,
    "div": 1500,
    "che": 2.5,
    "grd": -40,
    "gen": 3400,
    "vol": 2401,
    "bsm": 0,
    "rbt": 0,
    "tp1": 54,
    "tp2": 127,
    "hno": 1,
}

# GET /devices/{id}/status for the same Zappi, plus its GET /devices metadata
MOCK_CLOUD_ZAPPI_STATUS = {
    "deviceClass": "ZAPPI",
    "status": "active",
    "state": "charging",
    "deviceStatus": "charging",
    "supplyMode": "eco",
    "pilotState": "C2",
    "boostCharge": False,
    "actualPower": 7.36,
    "gridPower": 0.12,
    "genPower": 3.4,
    "sessionEnergy": 4.25,
    "energyDelivered": 0.12,
}

MOCK_CLOUD_ZAPPI_META = {
    "deviceId": "ZA12345678",
    "model": "zappi",
    "alias": "Driveway",
    "serialNumber": 12345678,
    "online": True,
}

MOCK_CLOUD_EDDI_STATUS = {
    "deviceClass": "EDDI",
    "status": "active",
    "state": "waiting_for_surplus",
    "deviceStatus": "diverting",
    "boostActive": False,
    "actualPower": 1.5,
    "gridPower": -0.04,
    "genPower": 3.4,
    "sessionEnergy": 2.5,
}

MOCK_CLOUD_EDDI_META = {
    "deviceId": "ED87654321",
    "model": "eddi",
    "alias": "Hot water",
    "serialNumber": 87654321,
    "online": True,
}


def test_normalise_direct_zappi():
    """Direct Zappi payloads normalise into the shared device model."""
    device = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    assert device.device_id == "Z12345678"
    assert device.kind == DEVICE_KIND_ZAPPI
    assert device.serial == "12345678"
    assert device.status == "Charging"
    assert device.mode == "Eco"
    assert device.plug_status == "Charging"
    assert device.power_w == 7360
    assert device.grid_power_w == 120
    assert device.generation_w == 3400
    assert device.voltage == 240.5
    assert device.session_energy_kwh == 4.25
    assert device.boost_active is False
    assert device.temp_1 is None
    print("  ✓ Direct Zappi normalisation")


def test_normalise_direct_eddi():
    """Direct Eddi payloads normalise, including probe temperature handling."""
    device = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    assert device.device_id == "E87654321"
    assert device.kind == DEVICE_KIND_EDDI
    assert device.status == "Diverting"
    assert device.power_w == 1500
    assert device.session_energy_kwh == 2.5
    assert device.boost_active is False
    assert device.plug_status == ""
    assert device.temp_1 == 54
    # 127 is myenergi's "probe not connected" sentinel and must not be published
    assert device.temp_2 is None
    print("  ✓ Direct Eddi normalisation")


def test_normalise_direct_eddi_boosting():
    """An Eddi mid-boost reports boost_active and remaining minutes."""
    raw = dict(MOCK_DIRECT_EDDI, sta=4, bsm=1, rbt=1800)
    device = normalise_direct_device(raw, DEVICE_KIND_EDDI)
    assert device.status == "Boosting"
    assert device.boost_active is True
    assert device.boost_remaining_mins == 30
    print("  ✓ Direct Eddi boost state")


def test_normalise_cloud_matches_direct():
    """Cloud and direct payloads for the same device produce equal values."""
    direct_zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    cloud_zappi = normalise_cloud_device(MOCK_CLOUD_ZAPPI_STATUS, MOCK_CLOUD_ZAPPI_META)
    assert cloud_zappi.kind == direct_zappi.kind
    assert cloud_zappi.serial == direct_zappi.serial
    assert cloud_zappi.status == direct_zappi.status
    assert cloud_zappi.mode == direct_zappi.mode
    assert cloud_zappi.plug_status == direct_zappi.plug_status
    # Cloud reports kW, direct reports W - both land in W
    assert cloud_zappi.power_w == direct_zappi.power_w
    assert cloud_zappi.generation_w == direct_zappi.generation_w
    assert cloud_zappi.session_energy_kwh == direct_zappi.session_energy_kwh
    # The cloud device id keeps its two letter prefix and the friendly alias is used
    assert cloud_zappi.device_id == "ZA12345678"
    assert cloud_zappi.name == "Driveway"

    direct_eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    cloud_eddi = normalise_cloud_device(MOCK_CLOUD_EDDI_STATUS, MOCK_CLOUD_EDDI_META)
    assert cloud_eddi.kind == direct_eddi.kind
    assert cloud_eddi.status == direct_eddi.status
    assert cloud_eddi.power_w == direct_eddi.power_w
    assert cloud_eddi.session_energy_kwh == direct_eddi.session_energy_kwh
    print("  ✓ Cloud and direct normalisation agree")


def test_normalise_handles_bad_values():
    """Out of range indices and missing fields fall back rather than raising."""
    device = normalise_direct_device({"sno": 1, "sta": 99, "zmo": "x"}, DEVICE_KIND_ZAPPI)
    assert device.status == "Unknown"
    assert device.mode == "Unknown"
    assert device.power_w == 0
    assert device.session_energy_kwh == 0

    device = normalise_direct_device({}, DEVICE_KIND_EDDI)
    assert device.serial == ""
    assert device.temp_1 is None

    device = normalise_cloud_device({"deviceClass": "EDDI"}, {})
    assert device.kind == DEVICE_KIND_EDDI
    assert device.power_w == 0
    print("  ✓ Malformed payloads degrade safely")


def test_myenergi(my_predbat=None):
    """
    ======================================================================
    MYENERGI TEST SUITE
    ======================================================================
    Comprehensive test suite for the myenergi Zappi and Eddi integration including:
    - Payload normalisation for both transports
    """
    print("\n" + "=" * 70)
    print("MYENERGI TEST SUITE")
    print("=" * 70)

    test_normalise_direct_zappi()
    test_normalise_direct_eddi()
    test_normalise_direct_eddi_boosting()
    test_normalise_cloud_matches_direct()
    test_normalise_handles_bad_values()

    print("=" * 70)
    return False
```

- [ ] **Step 3: Register the test suite**

In `apps/predbat/unit_test.py`, add the import alongside the other component test imports (near the `from tests.test_ohme import test_ohme` line):

```python
from tests.test_myenergi import test_myenergi
```

And add to `TEST_REGISTRY`, near the `("ohme", ...)` entry:

```python
        # myenergi Zappi and Eddi unit tests
        ("myenergi", test_myenergi, "myenergi Zappi and Eddi comprehensive tests (normalisation, transports, publishing, auto-config, controls)", False),
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t1.log 2>&1; grep -iE "error|fail|ImportError|cannot import" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t1.log | head -20
```

Expected: FAIL with `ImportError: cannot import name 'normalise_direct_device' from 'myenergi'`.

- [ ] **Step 5: Implement the device model and normalisers**

Append to `apps/predbat/myenergi.py`:

```python
@dataclass
class MyEnergiDevice:
    """One normalised myenergi device, identical in shape across both transports."""

    device_id: str
    kind: str
    serial: str
    name: str
    online: bool
    status: str
    mode: str
    plug_status: str
    power_w: float
    grid_power_w: float
    generation_w: float
    voltage: float
    session_energy_kwh: float
    boost_active: bool
    boost_remaining_mins: int
    temp_1: Optional[float]
    temp_2: Optional[float]


def _to_float(value, default=0.0):
    """Coerce a raw API value to float, returning default for None or junk."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _index_lookup(table, index, default="Unknown"):
    """Look a numeric status code up in one of the direct API's index tables."""
    try:
        position = int(index)
    except (TypeError, ValueError):
        return default
    if 0 <= position < len(table):
        return table[position]
    return default


def _optional_temp(value):
    """Return an Eddi probe temperature, or None when no probe is connected.

    myenergi reports 127 for an unconnected probe and a negative value when the
    reading is unknown; publishing either as a temperature would be misleading.
    """
    if value is None:
        return None
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None
    if temperature >= 127 or temperature < 0:
        return None
    return temperature


def normalise_direct_device(raw, kind):
    """Convert one direct API device record into a MyEnergiDevice.

    Args:
        raw: A single device dict from a /cgi-jstatus-* response.
        kind: Either DEVICE_KIND_ZAPPI or DEVICE_KIND_EDDI.
    """
    serial = str(raw.get("sno", "") or "")
    if kind == DEVICE_KIND_ZAPPI:
        status = _index_lookup(ZAPPI_STATES, raw.get("sta"))
        mode = _index_lookup(ZAPPI_CHARGE_MODES, raw.get("zmo"))
        plug_status = ZAPPI_PLUG_STATES.get(str(raw.get("pst", "") or ""), "")
        boost_active = status == "Boosting"
        boost_remaining_mins = 0
        temp_1 = None
        temp_2 = None
    else:
        status = _index_lookup(EDDI_STATES, raw.get("sta"))
        mode = "Stopped" if status == "Stopped" else "Normal"
        plug_status = ""
        boost_active = int(_to_float(raw.get("bsm"))) == 1
        boost_remaining_mins = int(round(_to_float(raw.get("rbt")) / 60.0))
        temp_1 = _optional_temp(raw.get("tp1"))
        temp_2 = _optional_temp(raw.get("tp2"))

    return MyEnergiDevice(
        device_id=DIRECT_PREFIX[kind] + serial,
        kind=kind,
        serial=serial,
        name="{}-{}".format(kind, serial),
        online=True,
        status=status,
        mode=mode,
        plug_status=plug_status,
        power_w=_to_float(raw.get("div")),
        grid_power_w=_to_float(raw.get("grd")),
        generation_w=_to_float(raw.get("gen")),
        voltage=_to_float(raw.get("vol")) / 10.0,
        session_energy_kwh=_to_float(raw.get("che")),
        boost_active=boost_active,
        boost_remaining_mins=boost_remaining_mins,
        temp_1=temp_1,
        temp_2=temp_2,
    )


def normalise_cloud_device(raw, meta):
    """Convert one cloud API status response into a MyEnergiDevice.

    Args:
        raw: The body of GET /devices/{id}/status.
        meta: The matching device entry from GET /devices, used for the id, alias
              and online flag that the status response does not carry.
    """
    kind = DEVICE_KIND_ZAPPI if str(raw.get("deviceClass", "")).upper() == "ZAPPI" else DEVICE_KIND_EDDI
    serial = str(meta.get("serialNumber", "") or "")
    device_id = str(meta.get("deviceId", "") or "")
    if not serial and device_id:
        serial = device_id[2:]

    status = CLOUD_STATUS_TO_NAME.get(str(raw.get("deviceStatus", "") or "").lower(), "Unknown")
    if kind == DEVICE_KIND_ZAPPI:
        mode = CLOUD_MODE_TO_NAME.get(str(raw.get("supplyMode", "") or "").lower(), "Unknown")
        plug_status = ZAPPI_PLUG_STATES.get(str(raw.get("pilotState", "") or ""), "")
        boost_active = bool(raw.get("boostCharge", False))
    else:
        mode = "Stopped" if status == "Stopped" else "Normal"
        plug_status = ""
        boost_active = bool(raw.get("boostActive", False))

    return MyEnergiDevice(
        device_id=device_id or (CLOUD_PREFIX[kind] + serial),
        kind=kind,
        serial=serial,
        name=meta.get("alias") or "{}-{}".format(kind, serial),
        online=bool(meta.get("online", True)),
        status=status,
        mode=mode,
        plug_status=plug_status,
        # The cloud API reports power in kW, the direct API in W. Normalise to W.
        power_w=_to_float(raw.get("actualPower")) * 1000.0,
        grid_power_w=_to_float(raw.get("gridPower")) * 1000.0,
        generation_w=_to_float(raw.get("genPower")) * 1000.0,
        voltage=0.0,
        session_energy_kwh=_to_float(raw.get("sessionEnergy")),
        boost_active=boost_active,
        boost_remaining_mins=0,
        # The cloud API does not expose Eddi probe temperatures
        temp_1=None,
        temp_2=None,
    )
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t1.log 2>&1; tail -20 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t1.log
```

Expected: PASS, all five normalisation checks printed.

Note: every kW→W conversion in the mock payloads is exact in IEEE 754 (`7.36 * 1000.0 == 7360.0`, `3.4 * 1000.0 == 3400.0`, `1.5 * 1000.0 == 1500.0`, `0.12 * 1000.0 == 120.0`), so the equality assertions hold as written. If you add a mock value whose conversion is not exact, compare with `abs(a - b) < 0.001` instead of `==`.

- [ ] **Step 7: Pin aiohttp and add the new dictionary words**

In `requirements.txt`, change the `aiohttp` line to:

```
aiohttp>=3.12
```

Add to `.cspell/custom-dictionary-workspace.txt` (the file is auto-sorted on commit, so append and re-stage):

```
asn
harvi
jdayhour
jstatus
libbi
```

- [ ] **Step 8: Run pre-commit and commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py apps/predbat/unit_test.py .cspell/custom-dictionary-workspace.txt requirements.txt
git commit -m "Add myenergi device model and payload normalisation

Introduces the myenergi module with the shared MyEnergiDevice dataclass and
normalisers for both the direct and cloud API payload shapes, so that the
transports added next can share every layer above the wire format.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Transport abstraction and stubbed controls

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: `MyEnergiDevice`, `MyEnergiError` from Task 1.
- Produces: `MyEnergiTransport` ABC with abstract `connect()`, `fetch_devices()`, `send_boost(device, amount, target_time=None)`, `cancel_boost(device)`; concrete stub methods `set_mode(device, mode)`, `set_priority(device, priority)`, `set_min_green_level(device, level)`, `set_phase_setting(device, phase)`, `get_schedule(device)`, `set_schedule(device, schedule)`, each returning `False`; helper `_not_implemented(what) -> bool`.

- [ ] **Step 1: Write the failing stub tests**

Append to `apps/predbat/tests/test_myenergi.py`, above `test_myenergi()`:

```python
class _StubTransport(MyEnergiTransport):
    """Minimal concrete transport used to exercise the abstract base's stubs."""

    async def connect(self):
        """Pretend to connect."""
        return True

    async def fetch_devices(self):
        """Return no devices."""
        return []

    async def send_boost(self, device, amount, target_time=None):
        """Pretend to send a boost."""
        return True

    async def cancel_boost(self, device):
        """Pretend to cancel a boost."""
        return True


def test_transport_stubs():
    """Every unimplemented control returns False and warns exactly once."""
    messages = []
    transport = _StubTransport(messages.append)

    assert run_async(transport.set_mode(None, "Eco")) is False
    assert run_async(transport.set_priority(None, 1)) is False
    assert run_async(transport.set_min_green_level(None, 50)) is False
    assert run_async(transport.set_phase_setting(None, "1")) is False
    assert run_async(transport.get_schedule(None)) is False
    assert run_async(transport.set_schedule(None, [])) is False

    assert len(messages) == 6, "Each stub should warn once, got {}".format(messages)
    assert all("not implemented" in message for message in messages)

    # A second call must not warn again
    assert run_async(transport.set_mode(None, "Eco")) is False
    assert len(messages) == 6, "Repeat calls must not warn again"
    print("  ✓ Stubbed controls warn once and return False")
```

Extend the imports at the top of the test file:

```python
from tests.test_infra import run_async

from myenergi import (
    DEVICE_KIND_EDDI,
    DEVICE_KIND_ZAPPI,
    MyEnergiTransport,
    normalise_cloud_device,
    normalise_direct_device,
)
```

And call it from `test_myenergi()`:

```python
    test_transport_stubs()
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t2.log 2>&1; grep -iE "error|fail|cannot import" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t2.log | head
```

Expected: FAIL with `ImportError: cannot import name 'MyEnergiTransport'`.

- [ ] **Step 3: Implement the transport base class**

Append to `apps/predbat/myenergi.py`:

```python
class MyEnergiTransport(ABC):
    """Wire-format adapter for one of the two myenergi APIs.

    This is the only layer that knows how a myenergi request is shaped. Everything
    above it works in terms of MyEnergiDevice, so adding or changing a transport
    never touches publishing, auto-configuration or the controls.
    """

    def __init__(self, log):
        """Store the logging function and initialise the one-shot warning set."""
        self.log = log
        self._warned_stubs = set()

    @abstractmethod
    async def connect(self):
        """Establish and validate the connection. Returns True on success."""

    @abstractmethod
    async def fetch_devices(self):
        """Return a list of MyEnergiDevice for every supported device found."""

    @abstractmethod
    async def send_boost(self, device, amount, target_time=None):
        """Start a boost on a device.

        Args:
            device: The MyEnergiDevice to boost.
            amount: kWh for a Zappi, minutes for an Eddi.
            target_time: Optional "HH:MM" completion time, Zappi smart boost only.
        """

    @abstractmethod
    async def cancel_boost(self, device):
        """Cancel an active boost on a device."""

    def _not_implemented(self, what):
        """Warn once that a control is not implemented in this release, and return False."""
        if what not in self._warned_stubs:
            self._warned_stubs.add(what)
            self.log("Warn: myenergi: {} is not implemented in this release".format(what))
        return False

    async def set_mode(self, device, mode):
        """Set the device supply mode. Not implemented in this release."""
        return self._not_implemented("set_mode")

    async def set_priority(self, device, priority):
        """Set the device diversion priority. Not implemented in this release."""
        return self._not_implemented("set_priority")

    async def set_min_green_level(self, device, level):
        """Set the Zappi minimum green level. Not implemented in this release."""
        return self._not_implemented("set_min_green_level")

    async def set_phase_setting(self, device, phase):
        """Set the Zappi phase setting. Not implemented in this release."""
        return self._not_implemented("set_phase_setting")

    async def get_schedule(self, device):
        """Read the device charging schedule. Not implemented in this release."""
        return self._not_implemented("get_schedule")

    async def set_schedule(self, device, schedule):
        """Write the device charging schedule. Not implemented in this release."""
        return self._not_implemented("set_schedule")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t2.log 2>&1; tail -20 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t2.log
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py
git commit -m "Add the myenergi transport abstraction with stubbed controls

Fixes the interface both transports implement, and lands the controls that are
out of scope for this release as single-warning stubs so the follow-up work is
purely additive.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Direct transport

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: `MyEnergiTransport`, `MyEnergiDevice`, `normalise_direct_device`, exceptions, constants from Tasks 1–2.
- Produces: `MyEnergiDirectTransport(log, hub_serial, api_key)` with `connect()`, `fetch_devices()`, `send_boost()`, `cancel_boost()`, and the internal `_request(path)` that performs ASN resolution. Exposes `self.base_url` and `self.needs_asn_refresh` for tests.

**Behaviour to implement**, exactly matching what the myenergi service does:

1. When `base_url` is unset or `needs_asn_refresh` is True, GET `https://director.myenergi.net/cgi-jstatus-E` with digest auth and read `X_MYENERGI-asn` from the response headers; `base_url` becomes `https://<asn>`.
2. Issue the real request against `base_url + path`.
3. Re-read `X_MYENERGI-asn` from every response and follow it if it changed.
4. A missing `X_MYENERGI-asn` header means bad credentials → `MyEnergiAuthError`.
5. HTTP 401 → `MyEnergiAuthError`. Any other non-200 → `MyEnergiApiError`, and set `needs_asn_refresh` so the next call re-resolves.
6. A timeout sets `needs_asn_refresh` and raises `MyEnergiApiError`.

- [ ] **Step 1: Write the failing direct transport tests**

Append to `apps/predbat/tests/test_myenergi.py`:

```python
def _direct_response(json_data, asn="s18.myenergi.net", status=200):
    """Build a mock aiohttp response carrying an X_MYENERGI-asn header."""
    response = MagicMock()
    response.status = status
    response.headers = {"X_MYENERGI-asn": asn} if asn else {}
    response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _direct_session(responses):
    """Build a mock aiohttp session whose get() returns the next queued response.

    Returns (session, calls), where calls records every requested URL in order.
    """
    calls = []
    queue = list(responses)

    def _get(url, **kwargs):
        calls.append(url)
        return queue.pop(0) if queue else _direct_response({})

    session = MagicMock()
    session.get = _get
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


MOCK_JSTATUS_ALL = [
    {"eddi": [MOCK_DIRECT_EDDI]},
    {"zappi": [MOCK_DIRECT_ZAPPI]},
    {"harvi": [{"sno": 11112222}]},
    {"asn": "s18.myenergi.net"},
    {"fwv": "3560S5.036"},
]


def test_direct_fetch_devices():
    """The direct transport resolves the ASN then parses the jstatus device groups."""
    session, calls = _direct_session([_direct_response([]), _direct_response(MOCK_JSTATUS_ALL)])
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")

    with patch("aiohttp.ClientSession", return_value=session):
        devices = run_async(transport.fetch_devices())

    assert calls[0].startswith("https://director.myenergi.net/cgi-jstatus-E"), calls
    assert calls[1] == "https://s18.myenergi.net/cgi-jstatus-*", calls
    assert transport.base_url == "https://s18.myenergi.net"
    # harvi is not a supported kind and must be skipped
    assert len(devices) == 2, [device.kind for device in devices]
    kinds = sorted(device.kind for device in devices)
    assert kinds == [DEVICE_KIND_EDDI, DEVICE_KIND_ZAPPI]
    print("  ✓ Direct transport resolves ASN and parses devices")


def test_direct_missing_asn_is_auth_error():
    """A response without the ASN header means bad credentials."""
    session, _calls = _direct_session([_direct_response([], asn=None)])
    transport = MyEnergiDirectTransport(print, "12345678", "wrong-key")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError:
            pass
    print("  ✓ Missing ASN header raises MyEnergiAuthError")


def test_direct_boost_urls():
    """Boost and cancel produce the exact documented URLs for both device kinds."""
    zappi = normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, zmo=2), DEVICE_KIND_ZAPPI)
    eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    # Pre-resolve the active server so the requests under test are the only ones made
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0}) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.send_boost(zappi, 10))
        run_async(transport.cancel_boost(zappi))
        run_async(transport.send_boost(eddi, 60))
        run_async(transport.cancel_boost(eddi))

    assert calls[0] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-0-10-10-0000", calls[0]
    assert calls[1] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-0-2-0-0000", calls[1]
    assert calls[2] == "https://s18.myenergi.net/cgi-eddi-boost-E87654321-10-1-60", calls[2]
    assert calls[3] == "https://s18.myenergi.net/cgi-eddi-boost-E87654321-1-1-0", calls[3]
    print("  ✓ Direct transport boost URLs")


def test_direct_smart_boost_url():
    """A Zappi boost with a target time uses the smart boost command."""
    zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    transport = MyEnergiDirectTransport(print, "12345678", "secret-key")
    transport.base_url = "https://s18.myenergi.net"
    transport.needs_asn_refresh = False

    session, calls = _direct_session([_direct_response({"status": 0})])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.send_boost(zappi, 15, target_time="07:30"))

    assert calls[0] == "https://s18.myenergi.net/cgi-zappi-mode-Z12345678-0-11-15-0730", calls[0]
    print("  ✓ Direct transport smart boost URL")
```

Extend the test file imports:

```python
from unittest.mock import AsyncMock, MagicMock, patch

from myenergi import (
    DEVICE_KIND_EDDI,
    DEVICE_KIND_ZAPPI,
    MyEnergiAuthError,
    MyEnergiDirectTransport,
    MyEnergiTransport,
    normalise_cloud_device,
    normalise_direct_device,
)
```

And call them from `test_myenergi()`:

```python
    test_direct_fetch_devices()
    test_direct_missing_asn_is_auth_error()
    test_direct_boost_urls()
    test_direct_smart_boost_url()
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t3.log 2>&1; grep -iE "error|fail|cannot import" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t3.log | head
```

Expected: FAIL with `ImportError: cannot import name 'MyEnergiDirectTransport'`.

- [ ] **Step 3: Implement the direct transport**

Append to `apps/predbat/myenergi.py`:

```python
class MyEnergiDirectTransport(MyEnergiTransport):
    """Transport for the direct myenergi API used by pymyenergi and ha-myenergi.

    Authenticates with HTTP digest, using the hub serial as the username and the
    API key generated at myaccount.myenergi.com as the password. myenergi shards
    accounts across servers, so the first request goes to director.myenergi.net,
    whose X_MYENERGI-asn response header names the host to use from then on.
    """

    def __init__(self, log, hub_serial, api_key):
        """Store credentials and start with an unresolved active server."""
        super().__init__(log)
        self.hub_serial = str(hub_serial)
        self.api_key = api_key
        self.base_url = None
        self.needs_asn_refresh = True

    def _new_session(self):
        """Create an aiohttp session carrying the digest auth middleware."""
        digest = aiohttp.DigestAuthMiddleware(self.hub_serial, self.api_key)
        return aiohttp.ClientSession(middlewares=(digest,), headers={"User-Agent": USER_AGENT})

    def _update_asn(self, headers):
        """Follow the active server named by the X_MYENERGI-asn response header."""
        asn = headers.get("X_MYENERGI-asn")
        if not asn:
            raise MyEnergiAuthError("no X_MYENERGI-asn header returned - check the hub serial and API key")
        new_url = "https://" + asn
        if new_url != self.base_url:
            self.log("Info: myenergi: active server is {}".format(new_url))
            self.base_url = new_url

    async def _resolve_asn(self):
        """Ask director.myenergi.net which server this account lives on."""
        async with self._new_session() as session:
            async with session.get(MYENERGI_DIRECTOR_URL + "/cgi-jstatus-E", timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                self._update_asn(response.headers)
        self.needs_asn_refresh = False

    async def _request(self, path):
        """Perform one GET against the active server, resolving the ASN if needed."""
        if self.base_url is None or self.needs_asn_refresh:
            await self._resolve_asn()
        url = self.base_url + path
        try:
            async with self._new_session() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    self._update_asn(response.headers)
                    if response.status == 401:
                        record_api_call("myenergi", success=False, reason="unauthorised")
                        raise MyEnergiAuthError("myenergi rejected the credentials for {}".format(path))
                    if response.status != 200:
                        self.needs_asn_refresh = True
                        record_api_call("myenergi", success=False, reason="http_{}".format(response.status))
                        raise MyEnergiApiError("HTTP {} from {}".format(response.status, path))
                    record_api_call("myenergi", success=True)
                    return await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            self.needs_asn_refresh = True
            record_api_call("myenergi", success=False, reason="timeout")
            raise MyEnergiApiError("timed out calling {}".format(path)) from exc
        except aiohttp.ClientError as exc:
            self.needs_asn_refresh = True
            record_api_call("myenergi", success=False, reason="client_error")
            raise MyEnergiApiError("request to {} failed: {}".format(path, exc)) from exc

    async def connect(self):
        """Resolve the active server, which also validates the credentials."""
        await self._resolve_asn()
        return True

    async def fetch_devices(self):
        """Fetch every device in one /cgi-jstatus-* call and normalise the supported ones.

        The response is a list of single-key dicts, one per device family, plus
        housekeeping entries such as {"asn": ...} and {"fwv": ...} that are skipped.
        """
        payload = await self._request("/cgi-jstatus-*")
        devices = []
        if not isinstance(payload, list):
            return devices
        for group in payload:
            if not isinstance(group, dict):
                continue
            for kind, records in group.items():
                if kind not in SUPPORTED_KINDS or not isinstance(records, list):
                    continue
                for raw in records:
                    if isinstance(raw, dict):
                        devices.append(normalise_direct_device(raw, kind))
        return devices

    async def send_boost(self, device, amount, target_time=None):
        """Start a boost, choosing the manual or smart command for a Zappi."""
        if device.kind == DEVICE_KIND_ZAPPI:
            energy = int(amount)
            if target_time:
                when = str(target_time).replace(":", "")
                await self._request("/cgi-zappi-mode-Z{}-0-11-{}-{}".format(device.serial, energy, when))
            else:
                await self._request("/cgi-zappi-mode-Z{}-0-10-{}-0000".format(device.serial, energy))
        else:
            target = EDDI_BOOST_TARGETS[EDDI_DEFAULT_BOOST_TARGET]
            await self._request("/cgi-eddi-boost-E{}-10-{}-{}".format(device.serial, target, int(amount)))
        return True

    async def cancel_boost(self, device):
        """Cancel an active boost."""
        if device.kind == DEVICE_KIND_ZAPPI:
            await self._request("/cgi-zappi-mode-Z{}-0-2-0-0000".format(device.serial))
        else:
            target = EDDI_BOOST_TARGETS[EDDI_DEFAULT_BOOST_TARGET]
            await self._request("/cgi-eddi-boost-E{}-1-{}-0".format(device.serial, target))
        return True
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t3.log 2>&1; tail -25 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t3.log
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py
git commit -m "Add the direct myenergi transport with ASN resolution

Digest-authenticated access to the director API, following the X_MYENERGI-asn
header to the account's active server and treating its absence as a credential
failure rather than a transport error.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Cloud transport

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `MyEnergiCloudTransport(log, access_token_getter)` where `access_token_getter` is a zero-argument callable returning the current bearer token. Methods: `connect()`, `fetch_devices()`, `send_boost()`, `cancel_boost()`, plus `self.device_meta` (a dict keyed by `deviceId`) and `self.meta_age_seconds` for the 30 minute device-list cache.

Taking the token through a callable rather than a stored string means `OAuthMixin` can refresh it on the component without the transport holding a stale copy.

- [ ] **Step 1: Write the failing cloud transport tests**

Append to `apps/predbat/tests/test_myenergi.py`:

```python
MOCK_CLOUD_DEVICES = {
    "sites": [
        {
            "siteId": "site-1",
            "name": "Home",
            "gridLimit": 15,
            "devices": [
                MOCK_CLOUD_ZAPPI_META,
                MOCK_CLOUD_EDDI_META,
                {"deviceId": "HA11112222", "model": "harvi", "alias": "CT", "serialNumber": 11112222, "online": True},
            ],
        }
    ]
}


def _cloud_response(json_data, status=200):
    """Build a mock aiohttp response for the cloud API."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _cloud_session(responses):
    """Patch aiohttp.ClientSession recording (method, url, json) for each request."""
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return queue.pop(0) if queue else _cloud_response({})

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def test_cloud_fetch_devices():
    """The cloud transport lists devices then polls status for supported ones only."""
    session, calls = _cloud_session(
        [
            _cloud_response(MOCK_CLOUD_DEVICES),
            _cloud_response(MOCK_CLOUD_ZAPPI_STATUS),
            _cloud_response(MOCK_CLOUD_EDDI_STATUS),
        ]
    )
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    with patch("aiohttp.ClientSession", return_value=session):
        devices = run_async(transport.fetch_devices())

    assert calls[0] == ("GET", "https://api.s18.myenergi.net/devices", None), calls[0]
    assert calls[1][1] == "https://api.s18.myenergi.net/devices/ZA12345678/status", calls[1]
    assert calls[2][1] == "https://api.s18.myenergi.net/devices/ED87654321/status", calls[2]
    # harvi is unsupported and must never be polled
    assert len(calls) == 3, calls
    assert len(devices) == 2
    assert devices[0].name == "Driveway"
    print("  ✓ Cloud transport lists and polls supported devices")


def test_cloud_boost_bodies():
    """Boost bodies are shaped per device class, never mixing the two forms."""
    zappi = normalise_cloud_device(MOCK_CLOUD_ZAPPI_STATUS, MOCK_CLOUD_ZAPPI_META)
    eddi = normalise_cloud_device(MOCK_CLOUD_EDDI_STATUS, MOCK_CLOUD_EDDI_META)
    transport = MyEnergiCloudTransport(print, lambda: "jwt-token")

    session, calls = _cloud_session([_cloud_response({"commandId": "c1"}) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(transport.send_boost(zappi, 10))
        run_async(transport.send_boost(eddi, 60))
        run_async(transport.cancel_boost(zappi))
        run_async(transport.cancel_boost(eddi))

    assert calls[0] == ("POST", "https://api.s18.myenergi.net/devices/ZA12345678/boost", {"mode": "normal", "parameters": {"energy": 10}}), calls[0]
    assert calls[1] == ("POST", "https://api.s18.myenergi.net/devices/ED87654321/boost", {"durationMinutes": 60}), calls[1]
    assert calls[2][0] == "DELETE" and calls[2][1].endswith("/devices/ZA12345678/boost"), calls[2]
    assert calls[3][0] == "DELETE" and calls[3][1].endswith("/devices/ED87654321/boost"), calls[3]

    # A Zappi body must never carry durationMinutes, an Eddi body never mode/parameters
    assert "durationMinutes" not in calls[0][2]
    assert "mode" not in calls[1][2] and "parameters" not in calls[1][2]
    print("  ✓ Cloud transport boost bodies")


def test_cloud_sets_bearer_header():
    """Requests carry the current bearer token from the supplied callable."""
    tokens = ["first-token"]
    session, _calls = _cloud_session([_cloud_response(MOCK_CLOUD_DEVICES)])
    transport = MyEnergiCloudTransport(print, lambda: tokens[0])

    captured = {}

    def _client_session(**kwargs):
        captured.update(kwargs.get("headers") or {})
        return session

    with patch("aiohttp.ClientSession", side_effect=_client_session):
        run_async(transport._request("GET", "/devices"))

    assert captured.get("Authorization") == "Bearer first-token", captured
    print("  ✓ Cloud transport sends the bearer token")


def test_cloud_unauthorised_raises_auth_error():
    """An HTTP 401 from the cloud API surfaces as MyEnergiAuthError."""
    session, _calls = _cloud_session([_cloud_response({"message": "nope", "code": "UNAUTHORISED"}, status=401)])
    transport = MyEnergiCloudTransport(print, lambda: "stale-token")

    with patch("aiohttp.ClientSession", return_value=session):
        try:
            run_async(transport.fetch_devices())
            raise AssertionError("Expected MyEnergiAuthError")
        except MyEnergiAuthError:
            pass
    print("  ✓ Cloud transport 401 raises MyEnergiAuthError")
```

Add `MyEnergiCloudTransport` to the test file's `myenergi` imports, and call the four new tests from `test_myenergi()`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t4.log 2>&1; grep -iE "error|fail|cannot import" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t4.log | head
```

Expected: FAIL with `ImportError: cannot import name 'MyEnergiCloudTransport'`.

- [ ] **Step 3: Implement the cloud transport**

Append to `apps/predbat/myenergi.py`:

```python
# The cloud device list changes rarely, so it is cached between polls.
CLOUD_DEVICE_LIST_MAX_AGE = 30 * 60

# Model names in GET /devices that map onto the kinds this release supports.
CLOUD_MODEL_TO_KIND = {"zappi": DEVICE_KIND_ZAPPI, "eddi": DEVICE_KIND_EDDI}


class MyEnergiCloudTransport(MyEnergiTransport):
    """Transport for the official myenergi 3rd party API.

    Authenticates with a bearer JWT obtained through the OAuth2 authorization code
    flow. The token is read through a callable on every request so that a refresh
    performed by OAuthMixin on the component takes effect immediately.
    """

    def __init__(self, log, access_token_getter):
        """Store the token accessor and initialise the device list cache."""
        super().__init__(log)
        self.access_token_getter = access_token_getter
        self.device_meta = {}
        self.meta_age_seconds = CLOUD_DEVICE_LIST_MAX_AGE

    def _headers(self):
        """Build the request headers, including the current bearer token."""
        return {
            "Authorization": "Bearer {}".format(self.access_token_getter() or ""),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    async def _request(self, method, path, body=None):
        """Perform one cloud API request and return the decoded JSON body."""
        url = MYENERGI_CLOUD_URL + path
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.request(method, url, json=body, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    if response.status == 401:
                        record_api_call("myenergi", success=False, reason="unauthorised")
                        raise MyEnergiAuthError("myenergi rejected the access token for {}".format(path))
                    if response.status not in (200, 201, 202, 204):
                        record_api_call("myenergi", success=False, reason="http_{}".format(response.status))
                        raise MyEnergiApiError("HTTP {} from {} {}".format(response.status, method, path))
                    record_api_call("myenergi", success=True)
                    if response.status == 204:
                        return {}
                    return await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            record_api_call("myenergi", success=False, reason="timeout")
            raise MyEnergiApiError("timed out calling {} {}".format(method, path)) from exc
        except aiohttp.ClientError as exc:
            record_api_call("myenergi", success=False, reason="client_error")
            raise MyEnergiApiError("request to {} {} failed: {}".format(method, path, exc)) from exc

    async def _refresh_device_list(self):
        """Reload GET /devices, keeping only the Zappi and Eddi entries."""
        payload = await self._request("GET", "/devices")
        meta = {}
        for site in payload.get("sites", []) or []:
            for entry in site.get("devices", []) or []:
                kind = CLOUD_MODEL_TO_KIND.get(str(entry.get("model", "")).lower())
                device_id = entry.get("deviceId")
                if kind and device_id:
                    meta[device_id] = entry
        self.device_meta = meta
        self.meta_age_seconds = 0

    async def connect(self):
        """Load the device list, which also validates the access token."""
        await self._refresh_device_list()
        return True

    async def fetch_devices(self):
        """Poll status for every cached Zappi and Eddi, refreshing the list when stale."""
        if not self.device_meta or self.meta_age_seconds >= CLOUD_DEVICE_LIST_MAX_AGE:
            await self._refresh_device_list()
        devices = []
        for device_id, meta in self.device_meta.items():
            status = await self._request("GET", "/devices/{}/status".format(device_id))
            if status:
                devices.append(normalise_cloud_device(status, meta))
        return devices

    async def send_boost(self, device, amount, target_time=None):
        """Start a boost, selecting the request body shape by device class."""
        if device.kind == DEVICE_KIND_ZAPPI:
            body = {"mode": "normal", "parameters": {"energy": int(amount)}}
            if target_time:
                body = {"mode": "smart", "parameters": {"energy": int(amount), "targetTime": target_time}}
        else:
            body = {"durationMinutes": int(amount)}
        await self._request("POST", "/devices/{}/boost".format(device.device_id), body=body)
        return True

    async def cancel_boost(self, device):
        """Cancel an active boost."""
        await self._request("DELETE", "/devices/{}/boost".format(device.device_id))
        return True
```

Note: `target_time` for the cloud transport is an ISO-8601 timestamp, not the `HH:MM` the direct transport takes. The component only issues untimed boosts in this release, so the difference is confined to the transports; Task 8's controls never pass `target_time`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t4.log 2>&1; tail -25 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t4.log
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py
git commit -m "Add the cloud myenergi transport for the 3rd party API

Bearer-token access to api.s18.myenergi.net with a cached device list and
per-device-class boost bodies, so a Zappi never receives durationMinutes and an
Eddi never receives mode or parameters.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Component core — initialisation, transport selection, run loop and publishing

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: `MyEnergiAPI(ComponentBase, OAuthMixin)` with `initialize(auth_method, hub_serial, api_key, key, token_expires_at, token_hash, automatic, enable_controls, poll_seconds)`, `run(seconds, first)`, `publish_data()`, `entity_prefix(device)`, and the attributes `self.transport`, `self.devices` (dict keyed by `device_id`), `self.boost_amounts` (dict keyed by `device_id`), `self.queued_events`.
- Also produces the module-level `myenergi_attribute_table` used by publishing.

- [ ] **Step 1: Write the failing component tests**

Append to `apps/predbat/tests/test_myenergi.py`:

```python
def _make_component(**overrides):
    """Build a MyEnergiAPI against MockBase with a stub transport already attached."""
    base = MockBase()
    args = {
        "auth_method": "direct",
        "hub_serial": "12345678",
        "api_key": "secret-key",
        "key": None,
        "token_expires_at": None,
        "token_hash": None,
        "automatic": True,
        "enable_controls": True,
        "poll_seconds": 60,
    }
    args.update(overrides)
    return MyEnergiAPI(base, **args)


def test_component_selects_transport():
    """auth_method picks the transport, and missing credentials refuse to start."""
    component = _make_component()
    assert isinstance(component.transport, MyEnergiDirectTransport)

    component = _make_component(auth_method="oauth", hub_serial=None, api_key=None, key="jwt-token")
    assert isinstance(component.transport, MyEnergiCloudTransport)

    # No credentials at all - no transport, and the reason is logged
    component = _make_component(hub_serial=None, api_key=None)
    assert component.transport is None
    print("  ✓ Transport selection and credential validation")


def test_component_publishes_entities():
    """A poll publishes the documented entity set for each device."""
    component = _make_component()
    component.devices = {
        "Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI),
        "E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI),
    }
    run_async(component.publish_data())

    entities = component.base.entities
    assert "sensor.predbat_myenergi_zappi_12345678_status" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_mode" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_plug_status" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_power" in entities
    assert "sensor.predbat_myenergi_zappi_12345678_session_energy" in entities
    assert "binary_sensor.predbat_myenergi_zappi_12345678_charging" in entities
    assert "switch.predbat_myenergi_zappi_12345678_boost" in entities
    assert "number.predbat_myenergi_zappi_12345678_boost_energy" in entities

    assert "sensor.predbat_myenergi_eddi_87654321_status" in entities
    assert "sensor.predbat_myenergi_eddi_87654321_power" in entities
    assert "sensor.predbat_myenergi_eddi_87654321_session_energy" in entities
    assert "sensor.predbat_myenergi_eddi_87654321_temp_1" in entities
    assert "switch.predbat_myenergi_eddi_87654321_boost" in entities
    assert "number.predbat_myenergi_eddi_87654321_boost_minutes" in entities

    # tp2 was the 127 sentinel, so no entity should exist for it
    assert "sensor.predbat_myenergi_eddi_87654321_temp_2" not in entities

    # The boost switch reflects the device, not a locally held value
    assert component.base.get_state_wrapper("switch.predbat_myenergi_zappi_12345678_boost") == "off"

    # Units come from the attribute table
    power = component.base.entities["sensor.predbat_myenergi_zappi_12345678_power"]
    assert power["attributes"]["unit_of_measurement"] == "W"
    assert power["attributes"]["device_class"] == "power"
    energy = component.base.entities["sensor.predbat_myenergi_zappi_12345678_session_energy"]
    assert energy["attributes"]["unit_of_measurement"] == "kWh"
    print("  ✓ Component publishes the expected entities")


def test_component_retains_last_good_reading():
    """A failed poll leaves the previously published values alone."""
    component = _make_component()
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    assert run_async(component.run(0, True)) is True
    good = component.base.get_state_wrapper("sensor.predbat_myenergi_zappi_12345678_session_energy")
    assert good == 4.25

    component.transport.fetch_devices = AsyncMock(side_effect=MyEnergiApiError("boom"))
    assert run_async(component.run(60, False)) is False
    still_good = component.base.get_state_wrapper("sensor.predbat_myenergi_zappi_12345678_session_energy")
    assert still_good == 4.25, "A failed poll must not overwrite the last good reading"
    print("  ✓ Failed polls retain the last good reading")


def test_component_poll_seconds_rounding():
    """poll_seconds is clamped to a whole number of base loop intervals."""
    assert _make_component(poll_seconds=1).poll_seconds == 60
    assert _make_component(poll_seconds=90).poll_seconds == 120
    assert _make_component(poll_seconds=300).poll_seconds == 300
    print("  ✓ poll_seconds rounds to a multiple of 60")
```

Extend the test imports with `MockBase`, `MyEnergiAPI`, `MyEnergiApiError`, `MyEnergiCloudTransport`, and register the four tests in `test_myenergi()`:

```python
from mock_base import MockBase
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t5.log 2>&1; grep -iE "error|fail|cannot import" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t5.log | head
```

Expected: FAIL with `ImportError: cannot import name 'MyEnergiAPI'`.

- [ ] **Step 3: Implement the attribute table and component core**

Append to `apps/predbat/myenergi.py`:

```python
# Attribute table for the published Home Assistant entities, in the style of ohme.py
myenergi_attribute_table = {
    "status": {"friendly_name": "myenergi Status", "icon": "mdi:information-outline"},
    "mode": {"friendly_name": "myenergi Mode", "icon": "mdi:ev-station"},
    "plug_status": {"friendly_name": "myenergi Plug Status", "icon": "mdi:ev-plug-type2"},
    "power": {"friendly_name": "myenergi Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "session_energy": {"friendly_name": "myenergi Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    "charging": {"friendly_name": "myenergi Charging", "icon": "mdi:battery-charging"},
    "boost": {"friendly_name": "myenergi Boost", "icon": "mdi:rocket-launch"},
    "boost_energy": {"friendly_name": "myenergi Boost Energy", "icon": "mdi:rocket-launch", "unit_of_measurement": "kWh", "min": BOOST_ENERGY_MIN, "max": BOOST_ENERGY_MAX, "step": 1},
    "boost_minutes": {"friendly_name": "myenergi Boost Minutes", "icon": "mdi:rocket-launch", "unit_of_measurement": "minutes", "min": BOOST_MINUTES_MIN, "max": BOOST_MINUTES_MAX, "step": 5},
    "temp_1": {"friendly_name": "myenergi Temperature 1", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "temp_2": {"friendly_name": "myenergi Temperature 2", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
}

DEFAULT_ZAPPI_BOOST_KWH = 10
DEFAULT_EDDI_BOOST_MINUTES = 60


class MyEnergiAPI(ComponentBase, OAuthMixin):
    """myenergi component providing Zappi and Eddi monitoring and boost control."""

    def initialize(self, auth_method=None, hub_serial=None, api_key=None, key=None, token_expires_at=None, token_hash=None, automatic=True, enable_controls=True, poll_seconds=60):
        """Select a transport from the configured credentials and set up component state."""
        self.auth_method = (auth_method or "direct").lower()
        self.hub_serial = hub_serial
        self.api_key = api_key
        self.automatic = automatic
        self.enable_controls = enable_controls
        # ComponentBase.start() calls run() on a fixed 60 second cadence, so the poll
        # interval can only be a whole number of those intervals.
        self.poll_seconds = max(60, int(round(_to_float(poll_seconds, 60) / 60.0)) * 60)

        self.devices = {}
        self.boost_amounts = {}
        self.queued_events = []
        self._auto_configured = False
        self.transport = None

        if self.auth_method == "oauth":
            self._init_oauth("oauth", key, token_expires_at, "myenergi")
            self.token_hash = token_hash or ""
            if not key and not token_hash:
                self.log("Error: myenergi: auth_method is 'oauth' but neither myenergi_key nor myenergi_token_hash is set")
                return
            self.transport = MyEnergiCloudTransport(self.log, lambda: self.access_token)
        else:
            self._init_oauth("api_key", None, None, "myenergi")
            if not hub_serial or not api_key:
                self.log("Error: myenergi: auth_method is 'direct' but myenergi_hub_serial and myenergi_api_key are not both set")
                return
            self.transport = MyEnergiDirectTransport(self.log, hub_serial, api_key)

    def entity_prefix(self, device):
        """Return the entity name prefix for a device, e.g. predbat_myenergi_zappi_12345678."""
        return "{}_myenergi_{}_{}".format(self.prefix, device.kind, device.serial)

    def boost_amount_for(self, device):
        """Return the currently selected boost amount for a device."""
        default = DEFAULT_ZAPPI_BOOST_KWH if device.kind == DEVICE_KIND_ZAPPI else DEFAULT_EDDI_BOOST_MINUTES
        return self.boost_amounts.get(device.device_id, default)

    async def run(self, seconds, first):
        """Process queued control events, then poll and publish."""
        if first:
            self.log("Info: myenergi: starting with the {} transport".format(self.auth_method))
        if not self.transport:
            return False

        if self.auth_method == "oauth":
            await self.check_and_refresh_oauth_token()

        refresh = False
        while self.queued_events:
            handler, *event_args = self.queued_events.pop(0)
            try:
                await handler(*event_args)
            except MyEnergiError as exc:
                self.log("Warn: myenergi: control failed: {}".format(exc))
            refresh = True

        if first or refresh or (seconds % self.poll_seconds) == 0:
            try:
                devices = await self.transport.fetch_devices()
            except MyEnergiError as exc:
                self.log("Warn: myenergi: poll failed: {}".format(exc))
                return False
            if devices:
                self.devices = {device.device_id: device for device in devices}
                await self.publish_data()
            elif first:
                self.log("Warn: myenergi: connected but no Zappi or Eddi devices were found")

        self.update_success_timestamp()
        return True

    async def publish_data(self):
        """Publish every known device as Predbat entities."""
        for device in self.devices.values():
            prefix = self.entity_prefix(device)
            self.dashboard_item("sensor.{}_status".format(prefix), state=device.status, attributes=myenergi_attribute_table["status"], app="myenergi")
            self.dashboard_item("sensor.{}_power".format(prefix), state=device.power_w, attributes=myenergi_attribute_table["power"], app="myenergi")
            self.dashboard_item("sensor.{}_session_energy".format(prefix), state=device.session_energy_kwh, attributes=myenergi_attribute_table["session_energy"], app="myenergi")
            self.dashboard_item("switch.{}_boost".format(prefix), state="on" if device.boost_active else "off", attributes=myenergi_attribute_table["boost"], app="myenergi")

            if device.kind == DEVICE_KIND_ZAPPI:
                self.dashboard_item("sensor.{}_mode".format(prefix), state=device.mode, attributes=myenergi_attribute_table["mode"], app="myenergi")
                self.dashboard_item("sensor.{}_plug_status".format(prefix), state=device.plug_status, attributes=myenergi_attribute_table["plug_status"], app="myenergi")
                self.dashboard_item("binary_sensor.{}_charging".format(prefix), state="on" if device.status == "Charging" else "off", attributes=myenergi_attribute_table["charging"], app="myenergi")
                self.dashboard_item("number.{}_boost_energy".format(prefix), state=self.boost_amount_for(device), attributes=myenergi_attribute_table["boost_energy"], app="myenergi")
            else:
                self.dashboard_item("number.{}_boost_minutes".format(prefix), state=self.boost_amount_for(device), attributes=myenergi_attribute_table["boost_minutes"], app="myenergi")
                if device.temp_1 is not None:
                    self.dashboard_item("sensor.{}_temp_1".format(prefix), state=device.temp_1, attributes=myenergi_attribute_table["temp_1"], app="myenergi")
                if device.temp_2 is not None:
                    self.dashboard_item("sensor.{}_temp_2".format(prefix), state=device.temp_2, attributes=myenergi_attribute_table["temp_2"], app="myenergi")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t5.log 2>&1; tail -30 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t5.log
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py
git commit -m "Add the myenergi component core with polling and entity publishing

Selects a transport from the configured credentials, polls on the component base
cadence and publishes per-device entities. A failed poll returns False without
republishing, so the last good reading survives a transient outage.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Component registration and configuration schema

**Files:**
- Modify: `apps/predbat/components.py` (import block near line 36, `COMPONENT_LIST` — add after the `"ohme"` entry)
- Modify: `apps/predbat/config.py` (`APPS_SCHEMA`, near the existing `fox_*` keys around line 2510)
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: `MyEnergiAPI` from Task 5.
- Produces: the `"myenergi"` key in `COMPONENT_LIST` with `event_filter` `"predbat_myenergi_"`; nine `myenergi_*` keys in `APPS_SCHEMA`.

- [ ] **Step 1: Write the failing registration test**

Append to `apps/predbat/tests/test_myenergi.py`:

```python
def test_component_registration():
    """The component is registered with matching config keys and event filter."""
    from components import COMPONENT_LIST
    from config import APPS_SCHEMA

    entry = COMPONENT_LIST["myenergi"]
    assert entry["class"] is MyEnergiAPI
    assert entry["event_filter"] == "predbat_myenergi_"
    assert entry["phase"] == 1
    assert entry["can_restart"] is True
    assert entry["required_or"] == ["api_key", "key"]

    # Every declared arg must name a config key that exists in the schema, and every
    # arg must be accepted by initialize()
    import inspect

    parameters = inspect.signature(MyEnergiAPI.initialize).parameters
    for arg_name, spec in entry["args"].items():
        assert arg_name in parameters, "initialize() has no parameter '{}'".format(arg_name)
        assert spec["config"] in APPS_SCHEMA, "{} missing from APPS_SCHEMA".format(spec["config"])
    print("  ✓ Component registration and schema keys")
```

Register it in `test_myenergi()`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t6.log 2>&1; grep -iE "KeyError|error|fail" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t6.log | head
```

Expected: FAIL with `KeyError: 'myenergi'`.

- [ ] **Step 3: Register the component**

In `apps/predbat/components.py`, add to the import block:

```python
from myenergi import MyEnergiAPI
```

And add to `COMPONENT_LIST`, after the `"ohme"` entry:

```python
    "myenergi": {
        "class": MyEnergiAPI,
        "name": "myenergi",
        "event_filter": "predbat_myenergi_",
        "args": {
            "auth_method": {"required": False, "config": "myenergi_auth_method", "default": "direct"},
            "hub_serial": {"required": False, "config": "myenergi_hub_serial"},
            "api_key": {"required": False, "config": "myenergi_api_key"},
            "key": {"required": False, "config": "myenergi_key"},
            "token_expires_at": {"required": False, "config": "myenergi_token_expires_at"},
            "token_hash": {"required": False, "config": "myenergi_token_hash"},
            "automatic": {"required": False, "config": "myenergi_automatic", "default": True},
            "enable_controls": {"required": False, "config": "myenergi_enable_controls", "default": True},
            "poll_seconds": {"required": False, "config": "myenergi_poll_seconds", "default": 60},
        },
        "required_or": ["api_key", "key"],
        "phase": 1,
        "can_restart": True,
    },
```

- [ ] **Step 4: Add the schema keys**

In `apps/predbat/config.py`, add to `APPS_SCHEMA` near the `fox_*` keys:

```python
    "myenergi_auth_method": {"type": "string", "empty": False},
    "myenergi_hub_serial": {"type": "string", "empty": False},
    "myenergi_api_key": {"type": "string", "empty": False},
    "myenergi_key": {"type": "string", "empty": False},
    "myenergi_token_expires_at": {"type": "string", "empty": False},
    "myenergi_token_hash": {"type": "string", "empty": False},
    "myenergi_automatic": {"type": "boolean"},
    "myenergi_enable_controls": {"type": "boolean"},
    "myenergi_poll_seconds": {"type": "integer", "zero": False},
```

- [ ] **Step 5: Run the myenergi tests and the config validation tests**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi --test validate_config --test plugin_startup > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t6.log 2>&1; tail -30 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t6.log
```

Expected: PASS for all three.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/components.py apps/predbat/config.py apps/predbat/tests/test_myenergi.py
git commit -m "Register the myenergi component and its apps.yaml schema

Adds the COMPONENT_LIST entry with required_or on the two credential sets so the
component only starts when one transport is fully configured, plus the matching
APPS_SCHEMA keys.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Automatic configuration

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: `MyEnergiAPI`, `entity_prefix()` from Task 5.
- Produces: `MyEnergiAPI.automatic_config()`, called once from `run()` after the first successful poll.

Behaviour: Zappi session energy entities go to `car_charging_energy` (a list when there is more than one Zappi, since `minute_data_import_export` accepts and sums a list); the first Eddi's session energy entity goes to `iboost_energy_today`. Both use `set_arg_auto()` so an explicit apps.yaml value is reported rather than silently replaced.

- [ ] **Step 1: Write the failing auto-config tests**

Append to `apps/predbat/tests/test_myenergi.py`:

```python
def test_automatic_config():
    """Zappis wire into car_charging_energy and the Eddi into iboost_energy_today."""
    component = _make_component()
    second_zappi = dict(MOCK_DIRECT_ZAPPI, sno=22223333)
    component.devices = {
        "Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI),
        "Z22223333": normalise_direct_device(second_zappi, DEVICE_KIND_ZAPPI),
        "E87654321": normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI),
    }
    component.automatic_config()

    assert component.base.args["car_charging_energy"] == [
        "sensor.predbat_myenergi_zappi_12345678_session_energy",
        "sensor.predbat_myenergi_zappi_22223333_session_energy",
    ], component.base.args["car_charging_energy"]
    assert component.base.args["iboost_energy_today"] == "sensor.predbat_myenergi_eddi_87654321_session_energy"
    print("  ✓ Automatic configuration wires both energy inputs")


def test_automatic_config_single_zappi_is_still_a_list():
    """A single Zappi still produces a list, so adding a second changes nothing else."""
    component = _make_component()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.automatic_config()
    assert component.base.args["car_charging_energy"] == ["sensor.predbat_myenergi_zappi_12345678_session_energy"]
    assert "iboost_energy_today" not in component.base.args
    print("  ✓ Single Zappi auto-config")


def test_automatic_config_disabled():
    """With automatic off, nothing is wired even after a successful poll."""
    component = _make_component(automatic=False)
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    run_async(component.run(0, True))
    assert "car_charging_energy" not in component.base.args
    print("  ✓ Automatic configuration respects the off switch")


def test_automatic_config_runs_once():
    """Auto-config runs after the first poll and is not repeated."""
    component = _make_component()
    component.transport.fetch_devices = AsyncMock(return_value=[normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)])
    run_async(component.run(0, True))
    assert component._auto_configured is True
    component.base.args["car_charging_energy"] = ["sensor.user_override"]
    run_async(component.run(60, False))
    assert component.base.args["car_charging_energy"] == ["sensor.user_override"], "Auto-config must not run twice"
    print("  ✓ Automatic configuration runs exactly once")
```

Register the four tests in `test_myenergi()`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t7.log 2>&1; grep -iE "AttributeError|error|fail" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t7.log | head
```

Expected: FAIL with `AttributeError: 'MyEnergiAPI' object has no attribute 'automatic_config'`.

- [ ] **Step 3: Implement automatic configuration**

Add to `MyEnergiAPI` in `apps/predbat/myenergi.py`:

```python
    def automatic_config(self):
        """Wire the device energy sensors into Predbat's load inputs.

        Zappi charging energy is subtracted from house load as car charging, so it
        goes to car_charging_energy - as a list, because minute_data_import_export
        accepts one and sums the entities. Eddi diverted energy feeds the iboost
        model instead.

        Note that these sensors are session-scoped and reset to zero when a session
        ends. get_from_incrementing() clamps negative deltas to zero so the per-minute
        subtraction is unaffected, but the iboost_today total derived in fetch.py from
        the midnight-to-now difference will under-report after a mid-day Eddi reset.
        This is a known limitation, documented in docs/components.md.
        """
        zappi_entities = []
        eddi_entity = None
        for device in sorted(self.devices.values(), key=lambda item: item.serial):
            entity = "sensor.{}_session_energy".format(self.entity_prefix(device))
            if device.kind == DEVICE_KIND_ZAPPI:
                zappi_entities.append(entity)
            elif eddi_entity is None:
                eddi_entity = entity

        if zappi_entities:
            self.log("Info: myenergi: setting car_charging_energy to {}".format(zappi_entities))
            self.set_arg_auto("car_charging_energy", zappi_entities)
        if eddi_entity:
            self.log("Info: myenergi: setting iboost_energy_today to {}".format(eddi_entity))
            self.set_arg_auto("iboost_energy_today", eddi_entity)
```

And call it from `run()`, immediately after the successful publish:

```python
            if devices:
                self.devices = {device.device_id: device for device in devices}
                await self.publish_data()
                if self.automatic and not self._auto_configured:
                    self.automatic_config()
                    self._auto_configured = True
            elif first:
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t7.log 2>&1; tail -30 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t7.log
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py
git commit -m "Wire myenergi devices into car_charging_energy and iboost_energy_today

Zappi session energy sensors are set as a list on car_charging_energy so several
chargers sum, and the first Eddi feeds iboost_energy_today. Uses set_arg_auto so
an explicit apps.yaml value is reported rather than silently replaced.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Boost controls

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `apps/predbat/tests/test_myenergi.py`

**Interfaces:**
- Consumes: `MyEnergiAPI`, transports' `send_boost`/`cancel_boost`.
- Produces: `MyEnergiAPI.switch_event(entity_id, service)`, `number_event(entity_id, value)`, `switch_event_handler(entity_id, service)`, `number_event_handler(entity_id, value)`, `device_for_entity(entity_id)`.

Events are queued onto `self.queued_events` rather than actioned inline, so API calls never run on the event thread — the same pattern `ohme.py` uses.

- [ ] **Step 1: Write the failing control tests**

Append to `apps/predbat/tests/test_myenergi.py`:

```python
def test_controls_queue_rather_than_call():
    """Switch and number events queue for the run loop instead of calling inline."""
    component = _make_component()
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    assert len(component.queued_events) == 1
    component.transport.send_boost.assert_not_called()

    component.transport.fetch_devices = AsyncMock(return_value=list(component.devices.values()))
    run_async(component.run(60, False))
    component.transport.send_boost.assert_called_once()
    assert component.queued_events == []
    print("  ✓ Control events queue for the run loop")


def test_boost_uses_number_entity_value():
    """The boost amount comes from the companion number entity."""
    component = _make_component()
    device = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    component.devices = {"Z12345678": device}
    component.transport.send_boost = AsyncMock(return_value=True)

    # number_event only queues, so drain the queue the way run() would
    run_async(component.number_event("number.predbat_myenergi_zappi_12345678_boost_energy", 25))
    handler, *event_args = component.queued_events.pop(0)
    run_async(handler(*event_args))

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))

    component.transport.send_boost.assert_called_once_with(device, 25)
    print("  ✓ Boost uses the number entity value")


def test_boost_refused_in_fast_mode():
    """A Zappi outside Eco or Eco+ is not boosted, and no API call is made."""
    component = _make_component()
    fast = normalise_direct_device(dict(MOCK_DIRECT_ZAPPI, zmo=1), DEVICE_KIND_ZAPPI)
    assert fast.mode == "Fast"
    component.devices = {"Z12345678": fast}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    component.transport.send_boost.assert_not_called()
    print("  ✓ Boost refused outside Eco and Eco+")


def test_cancel_boost():
    """Turning the switch off cancels the boost."""
    component = _make_component()
    device = normalise_direct_device(dict(MOCK_DIRECT_EDDI, bsm=1, sta=4), DEVICE_KIND_EDDI)
    component.devices = {"E87654321": device}
    component.transport.cancel_boost = AsyncMock(return_value=True)

    run_async(component.switch_event_handler("switch.predbat_myenergi_eddi_87654321_boost", "turn_off"))
    component.transport.cancel_boost.assert_called_once_with(device)
    print("  ✓ Cancel boost")


def test_controls_disabled():
    """With enable_controls off, events are ignored entirely."""
    component = _make_component(enable_controls=False)
    component.devices = {"Z12345678": normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)}
    component.transport.send_boost = AsyncMock(return_value=True)

    run_async(component.switch_event("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    assert component.queued_events == []
    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_12345678_boost", "turn_on"))
    component.transport.send_boost.assert_not_called()
    print("  ✓ Controls respect enable_controls")


def test_control_for_unknown_entity_is_ignored():
    """An event for a device that is not known does nothing and does not raise."""
    component = _make_component()
    component.transport.send_boost = AsyncMock(return_value=True)
    run_async(component.switch_event_handler("switch.predbat_myenergi_zappi_99999999_boost", "turn_on"))
    component.transport.send_boost.assert_not_called()
    print("  ✓ Unknown entity events are ignored")
```

Register the six tests in `test_myenergi()`.

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t8.log 2>&1; grep -iE "AttributeError|error|fail" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t8.log | head
```

Expected: FAIL — `switch_event_handler` does not exist (the base class's no-op `switch_event` swallows the call, so `queued_events` stays empty).

- [ ] **Step 3: Implement the controls**

Add to `MyEnergiAPI` in `apps/predbat/myenergi.py`:

```python
    def device_for_entity(self, entity_id):
        """Find the device an entity belongs to, or None when it is not known."""
        for device in self.devices.values():
            if self.entity_prefix(device) in entity_id:
                return device
        return None

    async def switch_event(self, entity_id, service):
        """Queue a switch service call for the run loop."""
        if not self.enable_controls:
            return
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    async def number_event(self, entity_id, value):
        """Queue a number change for the run loop."""
        if not self.enable_controls:
            return
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def number_event_handler(self, entity_id, value):
        """Record a new boost amount for the device the entity belongs to."""
        device = self.device_for_entity(entity_id)
        if not device:
            return
        if device.kind == DEVICE_KIND_ZAPPI:
            amount = int(_to_float(value, DEFAULT_ZAPPI_BOOST_KWH))
            amount = max(BOOST_ENERGY_MIN, min(BOOST_ENERGY_MAX, amount))
        else:
            amount = int(_to_float(value, DEFAULT_EDDI_BOOST_MINUTES))
            amount = max(BOOST_MINUTES_MIN, min(BOOST_MINUTES_MAX, amount))
        self.boost_amounts[device.device_id] = amount

    async def switch_event_handler(self, entity_id, service):
        """Send or cancel a boost in response to the boost switch."""
        if not self.enable_controls:
            return
        if not entity_id.endswith("_boost"):
            return
        device = self.device_for_entity(entity_id)
        if not device:
            self.log("Warn: myenergi: no known device for {}".format(entity_id))
            return

        if service == "turn_on":
            # myenergi rejects a boost unless the Zappi is in one of the green modes
            if device.kind == DEVICE_KIND_ZAPPI and device.mode not in ZAPPI_BOOSTABLE_MODES:
                self.log("Warn: myenergi: cannot boost {} while it is in {} mode - boost needs Eco or Eco+".format(device.name, device.mode))
                return
            amount = self.boost_amount_for(device)
            self.log("Info: myenergi: boosting {} by {}".format(device.name, amount))
            await self.transport.send_boost(device, amount)
        elif service == "turn_off":
            self.log("Info: myenergi: cancelling boost on {}".format(device.name))
            await self.transport.cancel_boost(device)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --test myenergi > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t8.log 2>&1; tail -35 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t8.log
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py apps/predbat/tests/test_myenergi.py
git commit -m "Add myenergi send and cancel boost controls

Boost switches with a companion number entity for the amount, queued onto the run
loop so API calls never run on the event thread. A Zappi outside Eco or Eco+ is
refused locally rather than issuing a call myenergi would reject.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Command line test interface and documentation

**Files:**
- Modify: `apps/predbat/myenergi.py`
- Modify: `docs/components.md`
- Modify: `docs/apps-yaml.md`

**Interfaces:**
- Consumes: `MyEnergiAPI`, `MockBase`.
- Produces: module-level `test_myenergi_api(...)` coroutine and `main()`, guarded by `if __name__ == "__main__":`. Both are `# pragma: no cover`.

- [ ] **Step 1: Implement the CLI harness**

Append to `apps/predbat/myenergi.py`:

```python
async def run_myenergi_cli(args):  # pragma: no cover
    """Run one myenergi poll, and optionally a boost command, against the live API."""
    mock_base = MockBase()
    arg_dict = {
        "auth_method": "oauth" if args.token else "direct",
        "hub_serial": args.hub_serial,
        "api_key": args.api_key,
        "key": args.token,
        "token_hash": args.token_hash,
        "automatic": False,
        "enable_controls": True,
    }
    component = MyEnergiAPI(mock_base, **arg_dict)
    if not component.transport:
        print("No usable credentials - pass --hub-serial and --api-key, or --token")
        return

    print("Connecting with the {} transport...".format(component.auth_method))
    devices = await component.transport.fetch_devices()
    if not devices:
        print("No Zappi or Eddi devices found")
        return

    if args.raw:
        for device in devices:
            print(device)
    else:
        print("{:<12} {:<10} {:<16} {:<10} {:>10} {:>12}".format("DEVICE", "KIND", "STATUS", "MODE", "POWER W", "SESSION kWh"))
        for device in devices:
            print("{:<12} {:<10} {:<16} {:<10} {:>10.0f} {:>12.2f}".format(device.device_id, device.kind, device.status, device.mode, device.power_w, device.session_energy_kwh))

    target_kind = args.boost or args.cancel_boost
    if target_kind:
        device = next((item for item in devices if item.kind == target_kind), None)
        if not device:
            print("No {} device found to control".format(target_kind))
            return
        if args.boost:
            print("Boosting {} by {}...".format(device.name, args.amount))
            await component.transport.send_boost(device, args.amount)
        else:
            print("Cancelling boost on {}...".format(device.name))
            await component.transport.cancel_boost(device)
        print("Done")


def main():  # pragma: no cover
    """Main function for command line execution."""
    parser = argparse.ArgumentParser(description="Test the myenergi API")
    parser.add_argument("--hub-serial", action="store", default=None, help="myenergi hub serial number (direct transport)")
    parser.add_argument("--api-key", action="store", default=None, help="myenergi API key from myaccount.myenergi.com (direct transport)")
    parser.add_argument("--token", action="store", default=None, help="myenergi OAuth access token (cloud transport)")
    parser.add_argument("--token-hash", action="store", default=None, help="myenergi OAuth token hash for refresh (cloud transport)")
    parser.add_argument("--boost", choices=SUPPORTED_KINDS, default=None, help="Send a boost to the first matching device")
    parser.add_argument("--cancel-boost", choices=SUPPORTED_KINDS, default=None, help="Cancel a boost on the first matching device")
    parser.add_argument("--amount", type=int, default=DEFAULT_ZAPPI_BOOST_KWH, help="Boost amount: kWh for a Zappi, minutes for an Eddi")
    parser.add_argument("--raw", action="store_true", help="Print the full normalised device records")

    args = parser.parse_args()
    asyncio.run(run_myenergi_cli(args))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the harness runs**

```bash
cd /Users/treforsouthwell/batpred2/apps/predbat && ../../coverage/venv/bin/python myenergi.py --help > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t9.log 2>&1; cat /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t9.log
```

Expected: the argparse help text, listing every option above. No credentials are needed for `--help`.

Then confirm the no-credentials path exits cleanly:

```bash
cd /Users/treforsouthwell/batpred2/apps/predbat && ../../coverage/venv/bin/python myenergi.py >> /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t9.log 2>&1; tail -5 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/t9.log
```

Expected: `No usable credentials - pass --hub-serial and --api-key, or --token`, exit 0.

- [ ] **Step 3: Document the component**

Add to `docs/components.md`, following the layout of the existing sections (`### <Name> (<key>)`, then `#### What it does`, `#### When to enable`, `#### Configuration Options`, and any extra subsections, each heading suffixed with the component key):

````markdown
### myenergi (myenergi)

#### What it does (myenergi)

Monitors myenergi Zappi EV chargers and Eddi hot water diverters, publishing their
status, power and session energy as Predbat entities, and provides send-boost and
cancel-boost controls.

Predbat supports both of myenergi's APIs:

- **Direct** (default) — HTTP digest authentication against `director.myenergi.net`,
  using your hub serial number and an API key you generate yourself. This is the same
  API the `ha-myenergi` Home Assistant integration uses, and any myenergi owner can
  set it up today.
- **Cloud OAuth** — the official 3rd party API at `api.s18.myenergi.net`. This needs
  credentials issued by myenergi through their partner registration process.

#### When to enable (myenergi)

Enable it if you own a Zappi or an Eddi and want Predbat to account for their energy
use when planning. With `myenergi_automatic` on (the default), Predbat wires the
sensors up for you:

- Zappi session energy is set as `car_charging_energy`, so charging is subtracted
  from your house load rather than being learnt as base load. Turn on
  `car_charging_hold` for that subtraction to take effect.
- The first Eddi's session energy is set as `iboost_energy_today`, feeding the
  iboost model.

#### Configuration Options (myenergi)

| Option | Default | Description |
|---|---|---|
| `myenergi_auth_method` | `direct` | `direct` or `oauth` |
| `myenergi_hub_serial` | — | Hub serial number, direct transport |
| `myenergi_api_key` | — | API key from myaccount.myenergi.com, direct transport |
| `myenergi_key` | — | OAuth access token, cloud transport |
| `myenergi_token_hash` | — | OAuth token hash used for refresh, cloud transport |
| `myenergi_token_expires_at` | — | OAuth access token expiry, cloud transport |
| `myenergi_automatic` | `True` | Wire the energy sensors into Predbat automatically |
| `myenergi_enable_controls` | `True` | Set to `False` for monitor-only operation |
| `myenergi_poll_seconds` | `60` | Poll interval, rounded up to a multiple of 60 |

Example for the direct transport:

```yaml
myenergi_hub_serial: '12345678'
myenergi_api_key: 'your-api-key'
```

#### How to get your API key (myenergi)

1. Sign in at <https://myaccount.myenergi.com>.
2. Open **Advanced** then **API Key**.
3. Generate a key for your hub and copy it.
4. Your hub serial number is printed on the hub and shown in the myenergi app.

#### Published entities (myenergi)

Per Zappi (`{sn}` is the device serial number):

- `sensor.predbat_myenergi_zappi_{sn}_status`, `_mode`, `_plug_status`, `_power`, `_session_energy`
- `binary_sensor.predbat_myenergi_zappi_{sn}_charging`
- `switch.predbat_myenergi_zappi_{sn}_boost`, `number.predbat_myenergi_zappi_{sn}_boost_energy`

Per Eddi:

- `sensor.predbat_myenergi_eddi_{sn}_status`, `_power`, `_session_energy`, `_temp_1`, `_temp_2`
- `switch.predbat_myenergi_eddi_{sn}_boost`, `number.predbat_myenergi_eddi_{sn}_boost_minutes`

Temperature sensors are only published when a probe is connected.

#### Controls (myenergi)

Turning a boost switch on sends a boost of the amount selected on the companion
number entity — kWh for a Zappi, minutes for an Eddi. Turning it off cancels the
boost. The switch state is read back from the device, so a boost started or stopped
in the myenergi app is reflected here too.

myenergi only accepts a Zappi boost while the charger is in Eco or Eco+ mode.
Predbat checks this first and logs a warning rather than issuing a call that would
be rejected.

Not implemented in this release: mode selection, priority, minimum green level,
phase setting, charging schedules, super schedules, managed mode, and Libbi
batteries. Attempting one of these logs a warning.

#### Known limitation (myenergi)

The session energy sensors reset to zero when a charging or heating session ends.
Predbat's per-minute load subtraction handles that correctly, so `car_charging_energy`
is unaffected. However, the `iboost_today` total is derived from the difference
between the midnight and current readings, so it will under-report if an Eddi session
resets part-way through the day. The planner's behaviour is unaffected; only the
reported daily iboost total is.
````

Add the section to the Table of Contents at the top of `docs/components.md`.

- [ ] **Step 4: Document the apps.yaml keys**

Add the nine `myenergi_*` keys to `docs/apps-yaml.md`, following the format used for the neighbouring `fox_*` and `ohme_*` keys in that file.

- [ ] **Step 5: Run the full test suite**

```bash
cd /Users/treforsouthwell/batpred2/coverage && ./run_all --quick > /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/full.log 2>&1; tail -30 /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/full.log; grep -icE "^FAILED|Traceback" /private/tmp/claude-501/-Users-treforsouthwell-batpred2/7ef137ca-438c-4434-b5ef-9af8b509c595/scratchpad/full.log
```

Expected: every test passes and the grep count is 0. Any pre-existing failure must be confirmed as pre-existing by checking out `main` and re-running before it is dismissed.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/batpred2 && ./run_pre_commit
git add apps/predbat/myenergi.py docs/components.md docs/apps-yaml.md
git commit -m "Add the myenergi command line harness and documentation

Standalone CLI for exercising either transport against the live API, plus the
components and apps.yaml documentation, including the iboost_today limitation
that follows from the session-scoped energy sensors.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

Checked against the spec:

- §2 both APIs → Tasks 3 and 4, with the exact endpoints in the Global Constraints reference table.
- §3 architecture and transport selection → Tasks 2, 3, 4 and 5.
- §3.1 normalised device model → Task 1.
- §4 configuration → Task 6.
- §5 published entities → Task 5. The `_boosting` binary sensors are absent, matching the spec's note that the boost switch already carries that state.
- §6 automatic configuration and §6.1 the known limitation → Task 7 (docstring) and Task 9 (documentation).
- §7 controls and §7.1 stubs → Tasks 8 and 2.
- §8 polling and error handling → Task 5 (`poll_seconds` rounding, last-good-reading retention, `record_api_call` in Tasks 3 and 4).
- §9 CLI → Task 9.
- §10 testing — all nine listed areas are covered: normalisation (1), direct transport (3), cloud transport (4), transport selection (5), publishing (5), auto-config (7), controls (8), stubs (2), error handling (5).
- §11 documentation → Tasks 1 (cspell) and 9 (components.md, apps-yaml.md).
- §12 out of scope — nothing in the plan implements Libbi, webhooks or schedules.

Type consistency: `MyEnergiDevice` field names are used identically in Tasks 1, 3, 4, 5, 7 and 8. `send_boost(device, amount, target_time=None)` and `cancel_boost(device)` keep the same signature in the ABC (Task 2) and both implementations (Tasks 3, 4), and are called with two positional arguments from Task 8. `entity_prefix(device)` is defined in Task 5 and used in Tasks 7 and 8.

One deliberate cross-transport asymmetry is flagged in Task 4: `target_time` is `HH:MM` for the direct transport and ISO-8601 for the cloud one. Nothing in this release passes it, so the divergence stays inside the transports; unifying it is a prerequisite for any future smart-boost control.

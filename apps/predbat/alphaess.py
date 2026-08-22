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

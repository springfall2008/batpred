# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# EcoFlow IoT Developer Platform Library
# -----------------------------------------------------------------------------

"""EcoFlow IoT Developer Platform integration for Predbat.

Registers each discovered EcoFlow device as an ``EcoFlowCloud`` Predbat inverter and
publishes monitoring sensors from the device's quota. Aimed at the Power Ocean home battery,
with STREAM the other relevant line.

BYOK, NOT A PLATFORM ACCOUNT. ``/device/list`` returns "only the device bound to itself, not
by share", so a developer key sees only the devices on its own EcoFlow account. Each user
therefore supplies their own accessKey/secretKey, which is the opposite of the AlphaESS
shared-developer-account model - there is no shared serial-slot ceiling and no consent
ambiguity, but there is also no way for the project to test against a fleet.

Auth is stateless: every request carries accessKey, nonce, timestamp and an HMAC-SHA256
signature over the sorted, flattened request params. There is no token and no refresh, so the
self-hosted add-on and the SaaS path share one code path - as with AlphaESS.

MONITORING ONLY, TODAY. This component reads and publishes; it does not yet drive the
battery, and ``ecoflow_control_enable`` defaults to False for that reason. The write
transport - ``PUT /iot-open/sign/device/quota`` - is implemented and tested in
``set_device_quota()``, and every write is gated on ``switch.predbat_set_read_only`` exactly
as AlphaESS's is. What is missing is not code: it is the cmdSet/id pairs that address a Power
Ocean charge window, target SoC or work mode, which the published documentation does not
give. ``ECOFLOW_SCHEDULE_COMMANDS`` ships empty rather than carrying a guess, and
``control_bindable()`` reads that table, so the day a real pair is confirmed the control
surface turns on without the rest of this file moving.

TELEMETRY KEY NAMES ARE UNVERIFIED. Every entry in ``ECOFLOW_TELEMETRY`` is a candidate, not
a fact - see the long comment on it. A leaf that does not resolve publishes no sensor at all,
rather than a zero that would read like a real measurement, and this component publishes a
``quota_keys`` diagnostic sensor listing the keys the device actually returned so the first
tester can correct the table (or set ``ecoflow_key_map`` and correct it without a code change).

HTTP POLLING, MQTT LATER. Telemetry is polled from ``GET /quota/all``. The platform also
pushes it over MQTT, which costs no API calls - the credentials endpoint and topic shapes are
already declared in ecoflow_const.py, and ``fetch_quota()`` is the only place a push payload
would have to land, so adding the push path does not mean rewriting this component.
"""

import asyncio
import json
import random
import time
import aiohttp
from component_base import ComponentBase
from coordinator import inverter_record
from ecoflow_const import (
    ECOFLOW_BASE_URL,
    ECOFLOW_ENDPOINTS,
    ECOFLOW_METHODS,
    ECOFLOW_NONCE_DIGITS,
    ECOFLOW_CODE_OK,
    ECOFLOW_REPLY_OK,
    ECOFLOW_REPLY_NOT_OWNED,
    ECOFLOW_REPLY_OFFLINE,
    ECOFLOW_RETRIES,
    ECOFLOW_TIMEOUT,
    ECOFLOW_TELEMETRY,
    ECOFLOW_TELEMETRY_UNITS,
    ECOFLOW_SCHEDULE_COMMANDS,
    ECOFLOW_DEBUG_REDACT_KEYS,
    ECOFLOW_DEBUG_REDACT_KEYS_RESPONSE,
    ECOFLOW_STORAGE_MODULE,
    ECOFLOW_CACHE_STATIC,
    ECOFLOW_CACHE_CONTROL,
    ECOFLOW_TTL_STATIC,
    ECOFLOW_TTL_QUOTA,
    flatten_params,
    looks_like_home_battery,
    reply_text,
    resolve_telemetry,
    sign_request,
)

# The behaviour an EcoFlowCloud inverter has, stated for the discovery record's capabilities.
# A literal, never read back from INVERTER_DEF: the record has to rebuild the row on its own,
# or the completeness test proves nothing.
#
# Every freeze/span capability is False because they describe writes, and this component does
# not write - see ECOFLOW_SCHEDULE_COMMANDS. They turn on together with the command table, not
# before it: a capability claimed ahead of the command that delivers it is how Predbat ends up
# planning a freeze nothing performs.
ECOFLOW_CAPABILITIES = {
    "support_charge_freeze": False,
    "support_discharge_freeze": False,
    "support_feedin_first": False,
    "can_span_midnight": False,
    "charge_discharge_with_rate": False,
    "charge_control_immediate": False,
    "target_soc_used_for_discharge": False,
}


class EcoFlowAPI(ComponentBase):
    """EcoFlow IoT Developer Platform cloud component."""

    # Trace every API request/response while the EcoFlow integration beds in. Nobody on the
    # project has an EcoFlow account or a Power Ocean, so a tester's log is the only evidence
    # available for the inferred quota field names; flip to False once they are confirmed.
    api_debug = True

    def initialize(
        self,
        access_key="",
        secret_key="",
        device_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=False,
        key_map=None,
        battery_capacity=None,
        inverter_limit=None,
        api_delay=2,
        min_write_interval=300,
        **kwargs,
    ):
        """Initialise the EcoFlow component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components registry has
        already resolved each arg from its ecoflow_* config key and passes it BY ARG NAME
        (e.g. access_key <- ecoflow_access_key), exactly like alphaess/sunsynk. Consume the
        kwargs directly - do NOT re-derive with get_arg("access_key"): that bare name is not
        in apps.yaml (the key is ecoflow_access_key), so it would always return the default.
        """
        self.log("Info: EcoFlowAPI initialising")
        self.access_key = access_key or ""
        self.secret_key = secret_key or ""
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.device_sn_filter = device_sn if isinstance(device_sn, list) else ([device_sn] if device_sn else [])
        # Per-install override for the unverified quota key names. A dict of
        # {predbat_leaf: quota_key}; normalised to the tuple-of-candidates shape
        # resolve_telemetry() expects so one code path serves both.
        self.key_map = {leaf: (name,) if isinstance(name, str) else tuple(name) for leaf, name in (key_map or {}).items()}
        # The API reports neither a pack size nor an inverter rating in any documented field,
        # so these are the user's own figures. Without them Predbat falls back to
        # inverter.py's hard-coded defaults, which is why automatic_config() says so out loud
        # rather than leaving the user to discover a silently wrong plan.
        self.battery_capacity_override = float(battery_capacity) if battery_capacity else 0.0
        self.inverter_limit_override = float(inverter_limit) if inverter_limit else 0.0
        self.api_delay = max(0, float(api_delay or 0))
        self.min_write_interval = max(0, int(min_write_interval or 0))
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_quota_keys = {}
        self.last_write_time = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        self._restore_had_error = False
        # The most recent body-level API failure message (the envelope's own `message` only -
        # never a credential), and whether the last discovery attempt actually reached the
        # API. Both exist so a support log can name precisely which stage failed.
        self.last_api_error = ""
        self.discovery_ok = None
        if not self.control_bindable():
            self.log("Info: EcoFlow is monitoring only - the published documentation gives no Power Ocean write command ids, so ECOFLOW_SCHEDULE_COMMANDS is empty and Predbat will not drive this battery. Set predbat_mode to Monitor.")
        elif not self.control_enable:
            self.log("Info: EcoFlow control is disabled (ecoflow_control_enable is false); monitoring only")

    @staticmethod
    def control_bindable():
        """Return True when a confirmed write command exists for Predbat's controls.

        Reads ECOFLOW_SCHEDULE_COMMANDS rather than a flag of its own, so there is exactly one
        place that decides whether this component drives a battery. The table ships empty, so
        this is False today and the control entities are neither published nor bound.
        """
        return bool(ECOFLOW_SCHEDULE_COMMANDS)

    def _nonce(self):
        """Return a fresh nonce: the documented six random digits, as a string.

        A string, not an int, because the signature covers the nonce verbatim and a leading
        zero must survive into both the header and the signed string.
        """
        return "".join(str(random.randint(0, 9)) for _ in range(ECOFLOW_NONCE_DIGITS))

    @staticmethod
    def _timestamp():
        """Return the current UTC time in milliseconds, as a string, as the docs require."""
        return str(int(time.time() * 1000))

    def _auth_headers(self, params):
        """Return the signed headers one request carries, for this request's exact params.

        The signature covers the params, so the headers cannot be built once and reused
        across calls - a header set built for a different body verifies against nothing and is
        indistinguishable from a bad secret key. Hence params in, headers out, every time.
        """
        nonce = self._nonce()
        timestamp = self._timestamp()
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "accessKey": self.access_key,
            "nonce": nonce,
            "timestamp": timestamp,
            "sign": sign_request(params, self.access_key, self.secret_key, nonce, timestamp),
        }

    @staticmethod
    def redact(payload, direction="request"):
        """Return a log-safe copy of a payload with secrets redacted.

        Never masks `code` or `message`: with no account to reproduce a failure against, those
        two fields are the only diagnostics a tester can send back.
        """
        if not isinstance(payload, dict):
            return payload
        redact_keys = ECOFLOW_DEBUG_REDACT_KEYS if direction == "request" else ECOFLOW_DEBUG_REDACT_KEYS_RESPONSE
        return {key: ("***" if key in redact_keys else value) for key, value in payload.items()}

    def debug_api(self, direction, what, payload=None):
        """Trace one API request or response while api_debug is on."""
        if not self.api_debug:
            return
        if payload is None:
            self.log("Info: EcoFlow API {} {}".format(direction, what))
            return
        try:
            rendered = json.dumps(self.redact(payload, direction), default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(payload)[:2000]
        self.log("Info: EcoFlow API {} {} {}".format(direction, what, rendered))

    @staticmethod
    def _code_is_ok(code):
        """Return True when an envelope's code means success.

        Accepts 0 and "0" alike: the REST examples return the code as a string while the MQTT
        set_reply carries an integer, and a component that compared against only one of them
        would read every successful call of the other kind as a failure.
        """
        return str(code) == str(ECOFLOW_CODE_OK)

    def _note_failure(self, code, body, path):
        """Record and log one API-level failure, without ever logging a credential.

        The reply-code table is consulted first because -1 (not your device) and -2 (offline)
        are the two failures a user can actually act on, and both read as a generic error
        otherwise. -1 in particular means the accessKey belongs to a different EcoFlow account
        from the device, which is the single most likely BYOK setup mistake.
        """
        message = body.get("message") or body.get("msg") or ""
        self.last_api_error = message or reply_text(code)
        try:
            numeric = int(code)
        except (TypeError, ValueError):
            numeric = None
        if numeric == ECOFLOW_REPLY_NOT_OWNED:
            self.log("Warn: EcoFlow {} reported that the device does not belong to this account ({}) - ecoflow_access_key must come from the same EcoFlow account the battery is registered to; a shared device does not count".format(path, reply_text(code)))
            return
        if numeric == ECOFLOW_REPLY_OFFLINE:
            # Routine and transient - a device that has dropped off the cloud, not a fault.
            self.log("Info: EcoFlow {} reported the device offline ({}); this is routine and not treated as a failure".format(path, reply_text(code)))
            return
        self.log("Warn: EcoFlow {} returned {} {}".format(path, code, message))

    async def _request(self, operation, params=None):
        """Perform one signed API call, returning (ok, data).

        Two failure modes are kept apart deliberately. An API-level failure is reported
        through _note_failure and returns ok False with the envelope's own message retained in
        last_api_error, because a write answers with a code and nothing else and the code is
        the only way to tell. A transport failure retries and then returns ok False too, but
        says nothing about the request itself.

        The same params are BOTH signed and sent - as a query string for GET and as a JSON
        body for POST/PUT. They are deliberately not built twice: signing one dict and sending
        another is a failure that looks exactly like a bad secret key.
        """
        path = ECOFLOW_ENDPOINTS[operation]
        method = ECOFLOW_METHODS[operation]
        url = "{}{}".format(ECOFLOW_BASE_URL, path)
        params = params or {}
        self.debug_api("request", "{} {}".format(method, path), params)
        for attempt in range(ECOFLOW_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=ECOFLOW_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    headers = self._auth_headers(params)
                    if method == "GET":
                        call = session.get(url, headers=headers, params=self._query_params(params))
                    elif method == "PUT":
                        call = session.put(url, headers=headers, json=params)
                    else:
                        call = session.post(url, headers=headers, json=params)
                    async with call as response:
                        if response.status != 200:
                            self.log("Warn: EcoFlow {} returned HTTP {}".format(path, response.status))
                            return False, None
                        data = await response.json()
            except Exception as error:
                if attempt + 1 >= ECOFLOW_RETRIES:
                    self.log("Warn: EcoFlow {} transport failure: {}".format(path, error))
                    return False, None
                await asyncio.sleep(1 + attempt)
                continue

            if not isinstance(data, dict):
                self.log("Warn: EcoFlow {} returned a non-object body".format(path))
                return False, None
            self.debug_api("response", path, data)
            if self._code_is_ok(data.get("code")):
                return True, data.get("data")
            self._note_failure(data.get("code"), data, path)
            return False, None
        return False, None

    @staticmethod
    def _query_params(params):
        """Flatten params for a GET query string the way the signature flattens them.

        aiohttp refuses a non-string query value, and more importantly the server rebuilds the
        signed string from what it received - so the query string has to carry the same
        flattened keys the signature was taken over, not a nested structure aiohttp would
        render some other way.
        """
        return {key: value for key, value in flatten_params(params)}

    async def get_device_list(self):
        """Discover every device on the account, returning its serials.

        Sets discovery_ok so an empty account can be told apart from a failed call - the two
        are indistinguishable from the returned list alone.

        Field names in the list entry are tolerated rather than required: the captured spec
        documents the endpoint but not its response shape, so `sn` and `deviceSn` are both
        accepted and a device with neither is skipped instead of raising inside the poll loop.
        """
        ok, data = await self._request("device_list")
        if not ok:
            self.discovery_ok = False
            return list(self.device_list)
        self.discovery_ok = True
        wanted = [str(sn).lower() for sn in self.device_sn_filter]
        serials = []
        detail = {}
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            sn = entry.get("sn") or entry.get("deviceSn")
            if not sn:
                continue
            if wanted and str(sn).lower() not in wanted:
                continue
            serials.append(sn)
            detail[sn] = entry
            product = entry.get("productName") or entry.get("deviceName") or "unknown"
            if not looks_like_home_battery(product):
                # Logged, never skipped. EcoFlow sell power stations and micro-inverters on
                # the same API, and a name table that filtered would stop working the day a
                # new home battery ships, so the quota poll decides instead: a device with no
                # resolvable telemetry publishes no sensors and binds nothing.
                self.log("Info: EcoFlow {} reports product '{}', which is not a recognised home-battery line. It is still polled - whether Predbat can use it depends on the telemetry it returns.".format(sn, product))
        self.device_list = serials
        self.device_detail = detail
        return serials

    async def fetch_quota(self, sn):
        """Fetch and store one device's full quota, returning True when telemetry resolved.

        The key list is stored alongside the resolved values and published as a diagnostic
        sensor. That is the whole point of it: with no hardware on the project, the keys a
        real device returns are the missing evidence, and a user can read them off the
        dashboard instead of being asked to capture an API trace.
        """
        ok, data = await self._request("quota_all", params={"sn": sn})
        if not ok or not isinstance(data, dict):
            return False
        self.device_quota_keys[sn] = sorted(str(key) for key in data.keys())
        values = resolve_telemetry(data, self.key_map)
        self.device_values[sn] = values
        if not values:
            self.log(
                "Warn: EcoFlow {} returned {} quota fields but none matched a known telemetry key, so no sensors were published. The field names in ECOFLOW_TELEMETRY are unverified against hardware - read sensor.{}_ecoflow_{}_quota_keys and set ecoflow_key_map in apps.yaml to the correct names.".format(
                    sn, len(self.device_quota_keys[sn]), self.prefix, str(sn).lower()
                )
            )
            return False
        missing = [leaf for leaf in ECOFLOW_TELEMETRY if leaf not in values]
        if missing:
            self.log("Info: EcoFlow {} resolved {} of {} telemetry values; {} did not match any candidate key and are not published".format(sn, len(values), len(ECOFLOW_TELEMETRY), sorted(missing)))
        return True

    async def refresh_static(self):
        """Re-discover devices. True when discovery worked.

        Branches on discovery_ok rather than on list emptiness: both a failed call and an
        empty account produce an empty list but require different handling. Deliberately does
        NOT assign an empty discovery result over a working device_list - this tier re-runs
        every 8 hours, so one transient failure must not take a working component down, and
        assigning the empty result would additionally write an empty cache and stamp it fresh,
        so a restart would restore nothing and skip re-discovery for a full TTL. Absence of a
        result is not a result.
        """
        previous = list(self.device_list)
        serials = await self.get_device_list()
        if self.discovery_ok is False:
            self.device_list = previous
            if previous:
                self.log("Warn: EcoFlow discovery failed; keeping the {} previously known device(s)".format(len(previous)))
            else:
                self.log("Warn: EcoFlow device discovery failed; the account may still have devices")
            return False
        if not serials:
            if self.device_sn_filter:
                self.log("Warn: EcoFlow discovery succeeded but the configured serial filter {} matched nothing in this account".format(self.device_sn_filter))
            else:
                self.log("Warn: EcoFlow this account has no devices bound to it. Devices SHARED with the account do not count - /device/list returns only devices bound to the account itself.")
            return False
        self.mark_refreshed("static")
        await self.save_static()
        return True

    async def refresh_quota(self):
        """Poll every device's quota. True when at least one returned usable telemetry."""
        got_any = False
        for index, sn in enumerate(list(self.device_list)):
            if index and self.api_delay:
                # A courtesy pause between consecutive calls. EcoFlow publish no rate limit
                # for the open API, so this is deliberately conservative rather than tuned.
                await asyncio.sleep(self.api_delay)
            if await self.fetch_quota(sn):
                got_any = True
        if got_any:
            self.mark_refreshed("quota")
        return got_any

    def tier_expired(self, tier, ttl_minutes):
        """Return True when a refresh tier is due, or has never run."""
        age = self._tier_refreshed.get(tier)
        if age is None:
            return True
        return (time.time() - age) >= (ttl_minutes * 60)

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Start a tier's clock. Called ONLY on a successful refresh.

        Marking unconditionally would defeat the first-cycle checks in run(): a retry after a
        deferred startup would find the tier "fresh", skip the poll and then run
        automatic_config() with no data after all - the very thing those checks prevent.
        """
        self._tier_refreshed[tier] = time.time() - (age_minutes * 60)

    def battery_capacity(self, sn):
        """Return the battery capacity in kWh, or 0 when it is not known.

        No documented quota field carries a pack size, so this is the user's own
        ecoflow_battery_capacity or nothing. Returning 0 rather than a plausible default is
        deliberate: automatic_config() then declines to bind soc_max and says so, whereas a
        guessed capacity would be silently wrong in every plan.
        """
        return self.battery_capacity_override

    def inverter_limit(self, sn):
        """Return the inverter's AC power limit in watts, or 0 when it is not known.

        As with battery_capacity, the user's figure or nothing - for the same reason.
        """
        return self.inverter_limit_override

    def _sensor_name(self, sn, leaf):
        """Return a namespaced EcoFlow sensor entity id."""
        return "sensor.{}_ecoflow_{}_{}".format(self.prefix, str(sn).lower(), leaf)

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced EcoFlow control entity id.

        Nothing calls this today - control_bindable() is False - but it is defined alongside
        _sensor_name so the control surface is named consistently the day the command table
        fills in, rather than invented under pressure at that point.
        """
        return "{}.{}_ecoflow_{}_{}".format(domain, self.prefix, str(sn).lower(), leaf)

    async def publish_data(self):
        """Publish monitoring sensors for each device.

        A telemetry leaf that did not resolve publishes NOTHING. That is the whole contract
        with the unverified key table: an absent sensor is visibly absent and
        automatic_config() declines to bind it, whereas a zero would read like a measurement.
        """
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            for leaf, unit in ECOFLOW_TELEMETRY_UNITS.items():
                if leaf in values:
                    self.dashboard_item(
                        self._sensor_name(sn, leaf),
                        state=values[leaf],
                        attributes={"unit_of_measurement": unit, "friendly_name": "EcoFlow {} {}".format(sn, leaf.replace("_", " ").title())},
                        app="ecoflow",
                    )

            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=round(capacity, 3), attributes={"unit_of_measurement": "kWh", "friendly_name": "EcoFlow {} Battery Capacity".format(sn)}, app="ecoflow")
            limit = self.inverter_limit(sn)
            if limit > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=round(limit), attributes={"unit_of_measurement": "W", "friendly_name": "EcoFlow {} Inverter Limit".format(sn)}, app="ecoflow")

            detail = self.device_detail.get(sn, {})
            for leaf, field in (("product", "productName"), ("device_name", "deviceName"), ("online", "online")):
                if detail.get(field) is not None:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=detail[field], attributes={"friendly_name": "EcoFlow {} {}".format(sn, leaf.replace("_", " ").title())}, app="ecoflow")

            # The diagnostic that makes the unverified key table fixable by a user rather than
            # only by whoever can capture an API trace. Published as a count with the names in
            # an attribute, because the state field is length-limited and a Power Ocean quota
            # runs to well over a hundred keys.
            keys = self.device_quota_keys.get(sn)
            if keys:
                self.dashboard_item(
                    self._sensor_name(sn, "quota_keys"),
                    state=len(keys),
                    attributes={"friendly_name": "EcoFlow {} Quota Keys".format(sn), "keys": keys, "resolved": sorted(self.device_values.get(sn, {}).keys())},
                    app="ecoflow",
                )

    async def automatic_config(self):
        """Register every discovered device as an EcoFlowCloud Predbat inverter.

        Only binds a monitoring arg that EVERY device actually reported, and binds no control
        arg at all while control_bindable() is False. Binding a control entity that no write
        can reach would leave Predbat believing it is driving the battery: inverter.py
        substitutes a dummy entity for an unbound control (inverter.py:612-655), which is
        visibly inert, and that is the safer of the two failures by a wide margin.
        """
        devices = list(self.device_list)
        if not devices:
            self.log("Warn: EcoFlow automatic_config found no devices")
            return
        self.set_arg_auto("inverter_type", ["EcoFlowCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))

        for leaf, arg in (("soc", "soc_percent"), ("battery_power", "battery_power"), ("grid_power", "grid_power"), ("load_power", "load_power"), ("pv_power", "pv_power")):
            if leaf == "pv_power" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_values.get(sn, {}) for sn in devices):
                self.set_arg_auto(arg, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log("Warn: EcoFlow not every device reports {}, so {} is not auto-configured. The quota field names are unverified - check sensor.{}_ecoflow_<serial>_quota_keys and set ecoflow_key_map.".format(leaf, arg, self.prefix))

        # Own the sign flags rather than leaving them to whatever else configured this install.
        # base.args is shared and NOT namespaced per inverter type, so a component that
        # legitimately inverts its own grid sensor - teslemetry sets grid_power_invert True,
        # fox does the same - leaves that key set for every inverter index, and an EcoFlow
        # inverter that never claims it inherits the flip. All three are False because
        # publish_data() emits whatever the device reported, and ECOFLOW_TELEMETRY_NEGATE - the
        # one place a sign correction belongs - ships empty until a real reading settles it.
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])

        if all(self.battery_capacity(sn) > 0 for sn in devices):
            self.set_arg_auto("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: EcoFlow reports no battery capacity in any documented quota field, so soc_max cannot be auto-configured. Set ecoflow_battery_capacity to your pack size in kWh, or soc_max in apps.yaml - Predbat cannot plan sensibly without it.")
        if all(self.inverter_limit(sn) > 0 for sn in devices):
            self.set_arg_auto("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: EcoFlow reports no inverter power rating in any documented quota field, so inverter_limit cannot be auto-configured. Set ecoflow_inverter_limit in watts, or inverter_limit in apps.yaml.")

        # Deliberately NOT auto-mapped, each for a different reason:
        # - the daily energy counters (load_today/import_today/export_today/pv_today): no
        #   documented quota field is known to be a cumulative kWh counter, and pointing
        #   Predbat's load learning at a field that turns out to be instantaneous would
        #   corrupt every forecast rather than merely fail.
        # - export_limit: the API reports no grid-connection cap, and a guess over-exports
        #   silently. Predbat falls back to 99999W until the user sets it.
        # - battery_rate_max: no pack power limit and no pack current/voltage to derive one
        #   from, so inverter.py's 2600W default stands. Said out loud below because that
        #   default is roughly half a Power Ocean's real rate and never reports itself.
        self.log(
            "Warn: EcoFlow does not report a battery power limit, an export limit, or daily energy counters. Set battery_rate_max, export_limit and the load_today/import_today/export_today entities in apps.yaml, or Predbat will plan against inverter.py's defaults."
        )

        if not self.control_bindable():
            self.log("Warn: EcoFlow bound no control entities - the published documentation gives no Power Ocean write command ids, so Predbat can monitor this battery but cannot charge or discharge it. Set predbat_mode to Monitor.")

    def _discovery_entities(self, sn):
        """The settings automatic_config() binds for one device, as discovery entity descriptors.

        Each entity id is formed with the same _sensor_name() call automatic_config() makes,
        so the two cannot drift apart. The record describes this device alone: a setting whose
        figure is known for this device is described even where automatic_config() binds it
        only once every device reports it. pv_power is described even under
        automatic_ignore_pv, which is the user's opt-out rather than a fact about the device.

        No control descriptor appears while control_bindable() is False, which is what makes
        the rebuilt definition report output_charge_control "none" and time_button_press False
        rather than claiming a control surface this component does not have.
        """
        entities = {}
        for leaf, arg in (("soc", "soc_percent"), ("battery_power", "battery_power"), ("grid_power", "grid_power"), ("load_power", "load_power"), ("pv_power", "pv_power")):
            if leaf in self.device_values.get(sn, {}):
                entities[arg] = {"entity_id": self._sensor_name(sn, leaf), "access": "r", "unit": "%" if leaf == "soc" else "W"}
        if self.battery_capacity(sn) > 0:
            entities["soc_max"] = {"entity_id": self._sensor_name(sn, "battery_capacity"), "access": "r", "unit": "kWh"}
        if self.inverter_limit(sn) > 0:
            entities["inverter_limit"] = {"entity_id": self._sensor_name(sn, "inverter_limit"), "access": "r", "unit": "W"}
        return entities

    def build_discovery(self):
        """Describe the discovered EcoFlow devices for the discovery catalogue.

        Reads only what get_device_list() and fetch_quota() already hold, so this adds no API
        calls and cannot change what EcoFlow does. Reporting is independent of self.automatic:
        the catalogue records the hardware, and the report's own "automatic" flag says whether
        Predbat wired apps.yaml to it.

        control is False on every record, which inverter_record() keeps rather than drops -
        "monitor-only" is a fact about this integration today, not an absence. It flips with
        ECOFLOW_SCHEDULE_COMMANDS, via control_bindable().

        Deliberately not reported:
        - ratings, unless the USER supplied them. No documented quota field carries a pack
          size or an inverter rating, so a rating here would be the user's own figure echoed
          back - which is worth reporting, because it is what Predbat is planning against -
          but never an invented one.
        - firmware: no documented field exposes it.
        - account_ids: every endpoint is keyed by serial alone; there is no site or plant id.
        - the online flag: it changes, and refresh_discovery() compares whole reports, so a
          changing value would re-file the report on every transition.

        Returns None when no device has been discovered yet, which refresh_discovery() treats
        as "nothing to report, ask again next cycle".
        """
        if not self.device_list:
            return None

        inverters = []
        for sn in self.device_list:
            detail = self.device_detail.get(sn, {}) or {}
            values = self.device_values.get(sn, {})

            functions = ["battery"]
            if "pv_power" in values:
                functions.append("solar")

            info = {}
            if detail.get("productName"):
                info["model"] = str(detail["productName"])

            ratings = {}
            if self.inverter_limit(sn) > 0:
                ratings["inverter_limit"] = self.inverter_limit(sn)
            if self.battery_capacity(sn) > 0:
                ratings["soc_max"] = self.battery_capacity(sn)

            inverters.append(
                inverter_record(
                    "ecoflow:{}".format(sn),
                    inverter_type="EcoFlowCloud",
                    control=self.control_bindable(),
                    composition="direct",
                    functions=functions,
                    capabilities=dict(ECOFLOW_CAPABILITIES),
                    hardware_ids={"serial": sn},
                    info=info,
                    ratings=ratings,
                    entities=self._discovery_entities(sn),
                )
            )

        return {"automatic": self.automatic, "inverters": inverters}

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the device."""
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    def _write_allowed(self, sn, command):
        """Return True when a write for one device and command may go out now.

        The minimum interval is pacing, not a budget: EcoFlow publish no documented write
        limit, so unlike AlphaESS there is no 24-hour ceiling to respect - but a rejected
        write that re-sends every tick is a fault in its own right, and the device is someone's
        house battery. A change arriving inside the window is HELD, not dropped: the caller
        does not cache it as applied, so the next eligible tick rebuilds and sends it.
        """
        if not self.min_write_interval:
            return True
        last = self.last_write_time.get((sn, command))
        if last is None:
            return True
        return (time.time() - last) >= self.min_write_interval

    async def set_device_quota(self, sn, cmd_set, cmd_id, params=None, command="quota"):
        """Write one device function via PUT /iot-open/sign/device/quota. True when accepted.

        The generic writer. It owns the envelope, the gates and the reply handling; it does NOT
        know what any particular cmdSet/id means, which is why it is complete and tested while
        ECOFLOW_SCHEDULE_COMMANDS is still empty. A caller supplies a confirmed pair and the
        extra fields that pair takes.

        Three gates, in the order a write meets them:
        1. switch.predbat_set_read_only - Predbat's own global write inhibit. Checked FIRST,
           before control_enable, so read-only mode holds back a write on an install that has
           deliberately enabled control.
        2. ecoflow_control_enable - the user's per-component opt-in, default False.
        3. min_write_interval - pacing, per (device, command).

        `command` names the logical operation for pacing, so a target-SoC write and a
        charge-window write are paced independently rather than starving each other.
        """
        if self._is_read_only():
            self.log("Info: EcoFlow write to {} suppressed: switch.{}_set_read_only is on".format(sn, self.prefix))
            return False
        if not self.control_enable:
            self.log("Info: EcoFlow write to {} suppressed: ecoflow_control_enable is false".format(sn))
            return False
        if not self._write_allowed(sn, command):
            self.log("Info: EcoFlow {} {} change is held by ecoflow_min_write_interval ({}s) and will be applied on the next eligible cycle".format(sn, command, self.min_write_interval))
            return False

        body = {"sn": sn, "params": dict(params or {})}
        body["params"]["cmdSet"] = cmd_set
        body["params"]["id"] = cmd_id
        ok, data = await self._request("quota_set", params=body)
        # Stamp every attempt, success or not. A persistently rejected write must still be
        # paced by ecoflow_min_write_interval, or a re-apply loop re-sends it on every tick
        # forever. Nothing is cached as applied on a failure path, so it keeps retrying - just
        # paced, not hammering.
        self.last_write_time[(sn, command)] = time.time()
        await self.save_control()
        if not ok:
            self.log("Warn: EcoFlow {} write for {} was rejected ({})".format(command, sn, self.last_api_error or "no message"))
            return False
        # A 200 with a success envelope means the platform ACCEPTED the command, not that the
        # device performed it - the device answers separately on .../set_reply over MQTT, which
        # this component does not yet subscribe to. So a True here is "sent and accepted", and
        # a caller must not treat it as "the battery is now doing this".
        code = data.get("code") if isinstance(data, dict) else None
        if code is not None and int(code) != ECOFLOW_REPLY_OK:
            self.log("Warn: EcoFlow {} write for {} was accepted by the platform but the device replied {}".format(command, sn, reply_text(code)))
            return False
        self.log("Info: EcoFlow sent {} to {} (cmdSet {} id {}); the platform accepted it. Whether the device performed it arrives on the MQTT set_reply topic, which Predbat does not yet read.".format(command, sn, cmd_set, cmd_id))
        return True

    async def load_cache(self, name):
        """Load one cache file, returning {} when absent or unreadable.

        self.storage being None is checked FIRST and returns silently: it means there is no
        Storage component configured, which is a permanent by-design condition rather than a
        transient fault worth retrying or warning about. Only a REAL failure flags the restore
        as incomplete.
        """
        if self.storage is None:
            return {}
        try:
            data = await self.storage.load(ECOFLOW_STORAGE_MODULE, name)
        except Exception as error:
            self.log("Warn: EcoFlow could not load cache {}: {}".format(name, error))
            self._restore_had_error = True
            return {}
        return data if isinstance(data, dict) else {}

    async def save_cache(self, name, data):
        """Save one cache file, tolerating a storage failure."""
        if self.storage is None:
            return
        try:
            await self.storage.save(ECOFLOW_STORAGE_MODULE, name, data)
        except Exception as error:
            self.log("Warn: EcoFlow could not save cache {}: {}".format(name, error))

    async def save_static(self):
        """Persist discovery. Refuses to overwrite a good cache with an empty result.

        Writing an empty device list and stamping the tier fresh would make a restart restore
        nothing and skip re-discovery for a full TTL. Absence of a result is not a result.
        """
        if not self.device_list:
            return
        await self.save_cache(ECOFLOW_CACHE_STATIC, {"device_list": self.device_list, "device_detail": self.device_detail})

    async def save_control(self):
        """Persist the write pacing timestamps so a restart does not reset them.

        last_write_time is keyed (sn, command), which JSON cannot carry as a dict key, so it is
        flattened to "sn|command" strings. Without persisting it, a restart loop bypasses
        ecoflow_min_write_interval entirely.
        """
        await self.save_cache(ECOFLOW_CACHE_CONTROL, {"last_write_time": {"{}|{}".format(sn, command): timestamp for (sn, command), timestamp in self.last_write_time.items()}})

    async def restore_state(self):
        """Restore the cached discovery and pacing state.

        _cache_restored is set only when nothing failed, so a transient storage outage is
        retried on a later cycle rather than silently marked done with nothing restored.
        """
        self._restore_had_error = False
        static = await self.load_cache(ECOFLOW_CACHE_STATIC)
        if static.get("device_list"):
            self.device_list = list(static["device_list"])
            self.device_detail = dict(static.get("device_detail") or {})
        control = await self.load_cache(ECOFLOW_CACHE_CONTROL)
        self.last_write_time = {}
        for key, timestamp in (control.get("last_write_time") or {}).items():
            sn, separator, command = str(key).partition("|")
            if separator:
                self.last_write_time[(sn, command)] = timestamp
        self._cache_restored = not self._restore_had_error

    async def run(self, seconds, first):
        """Main component tick: refresh by tier, publish, and file a discovery report.

        Returns True on a completed cycle, False on a failure that should hold the component
        in ComponentBase's startup backoff and be retried. Deliberately explicit rather than
        falling through to Python's implicit `None`: ComponentBase only clears its `first` flag
        when this returns something truthy, so an accidental `None` would strand the component
        in the ever-growing startup backoff forever even though every later cycle worked.
        """
        if first and not self._cache_restored:
            # Guarded on _cache_restored, not just `first`: a deferred startup returns False
            # below and leaves ComponentBase's `first` flag set, so run() is retried with
            # first still True. Without this guard a SUCCESSFUL restore would re-run on every
            # retry, redoing storage reads for nothing.
            await self.restore_state()
        if not self.access_key or not self.secret_key:
            self.log("Warn: EcoFlow needs both ecoflow_access_key and ecoflow_secret_key. They come from the IoT developer console of the EcoFlow account the battery is registered to - a key from another account cannot see it.")
            return False

        if self.tier_expired("static", ECOFLOW_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Warn: EcoFlow found no devices on this account")
            return False
        quota_ok = True
        if self.tier_expired("quota", ECOFLOW_TTL_QUOTA):
            quota_ok = await self.refresh_quota()

        await self.publish_data()

        # Filed right after this cycle's publish and BEFORE the deferred-startup branch below,
        # so a report is filed even on exactly the installs whose dump most needs to say what
        # hardware was found. refresh_discovery() owns the compare/retry loop and never raises.
        self.refresh_discovery()

        if first and not quota_ok:
            # Startup has not really succeeded without telemetry: automatic_config() runs on
            # the first cycle ALONE, so it would bind nothing and permanently skip every arg
            # for the whole session. Returning False leaves ComponentBase's `first` flag set,
            # so the startup path is retried on its backoff until a poll comes back.
            self.log("Warn: EcoFlow first quota poll returned no usable telemetry, deferring startup; it will be retried after a backoff")
            return False

        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()
        return True

    async def final(self):
        """Persist state on shutdown so a restart resumes without re-polling."""
        await self.save_static()
        await self.save_control()

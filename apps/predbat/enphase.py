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

"""Enphase Enlighten cloud API client component.

Talks to the unofficial Enphase Enlighten web-app API used by the Enlighten
web/mobile apps, since there is no official API offering battery control.
"""

from datetime import datetime, timedelta, timezone
import asyncio
import base64
import json
import random
import ssl
import uuid
from urllib.parse import urlencode

import aiohttp

from component_base import ComponentBase
from mock_base import MockBase
from predbat_metrics import record_api_call

try:
    import enphase_livestream_pb2 as livestream_pb

    HAS_LIVESTREAM_PROTOBUF = True
except (ImportError, Exception):
    livestream_pb = None
    HAS_LIVESTREAM_PROTOBUF = False

try:
    import aiomqtt

    HAS_AIOMQTT = True
except (ImportError, Exception):
    aiomqtt = None
    HAS_AIOMQTT = False

# Defined locally (not imported from utils) - every cloud component defines its own
# copy of this table rather than sharing one, matching the pattern used by fox.py.
BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME_FULL = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M") + ":00") for minute in range(0, 24 * 60, 1)]

BASE_URL = "https://enlighten.enphaseenergy.com"
LOGIN_PATH = "/login/login.json"
SELF_TOKEN_PATH = "/users/self/token"
SITE_SEARCH_PATH = "/app-api/search_sites.json"
BATTERY_CONFIG_BASE = "/service/batteryConfig/api/v1"

# Refresh ages in minutes for each data category
ENPHASE_REFRESH_STATIC = 24 * 60  # sites list - rarely changes
ENPHASE_REFRESH_SETTINGS = 30  # profile, battery settings, schedule config - change rarely / only via our own writes
ENPHASE_REFRESH_STATUS = 5  # battery SOC/available energy - needs to stay fresh for planning
ENPHASE_REFRESH_ENERGY = 5  # today energy totals
ENPHASE_REFRESH_POWER = 5  # latest instantaneous power
# How often apply_battery_schedule reconciles even without an explicit write-switch trigger, so a
# missed/dropped trigger cannot leave the cloud diverged from the plan indefinitely - see #4461.
ENPHASE_REFRESH_SCHEDULE_SYNC = 5
# How many read-clean-write passes apply_battery_schedule attempts, with a short backoff between,
# before giving up on this call and leaving it to the next periodic sync or trigger. See
# docs/superpowers/specs/2026-08-08-enphase-schedule-reconcile-design.md.
ENPHASE_RECONCILE_MAX_ATTEMPTS = 3
# How many intra-day buckets to step back when deriving power from the /today energy arrays.
# 1 would be the just-closed bucket, which the cloud is still back-filling; 2 is settled.
ENPHASE_SETTLED_BUCKETS = 2
ENPHASE_LIVESTREAM_TIMEOUT = 15  # seconds to wait for a livestream message before giving up
# How long a livestream reading stays usable. Holding it over a missed poll avoids flipping the
# sensors onto the 15-30 minute bucket fallback for a single blip, but it is instantaneous data
# with no timestamp of its own, so it must not be published indefinitely either.
ENPHASE_LIVE_MAX_AGE_MINUTES = 15
LIVESTREAM_BOOTSTRAP = "/pv/aws_sigv4/livestream.json"  # returns the AWS IoT endpoint, topic and authorizer credentials

# live_power is deliberately absent: livestream readings are instantaneous and carry no usable
# timestamp, so a restored one would be republished as if current. In-memory only.
ENPHASE_CACHE_KEYS = ["sites", "battery_status", "battery_settings", "profile", "schedules", "site_settings", "today", "latest_power"]
ENPHASE_CACHE_VERSION = 2

# Battery profiles accepted by the profile endpoint
PROFILE_SELF_CONSUMPTION = "self-consumption"
PROFILE_COST_SAVINGS = "cost_savings"
PROFILE_BACKUP_ONLY = "backup_only"

# Schedule families
SCHEDULE_CHARGE = "CFG"  # charge from grid
SCHEDULE_EXPORT = "DTG"  # discharge to grid
SCHEDULE_FREEZE = "RBD"  # restrict battery discharge
FAMILY_ORDER = ("cfg", "dtg", "rbd")  # lower-case schedule family keys, in a fixed processing order

ENPHASE_RETRIES = 5

# Browser mimicry - Enlighten rejects non-browser requests with 406/login walls
ENPHASE_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
BATTERY_UI_ORIGIN = "https://battery-profile-ui.enphaseenergy.com"

# Format for the published inverter_time sensor - matches INVERTER_DEF["EnphaseCloud"] clock_time_format
ENPHASE_CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"


def safe_float(value, default=0.0):
    """Convert a value to float, returning default for None or non-numeric values.

    The Enphase cloud returns strings like "N/A" (fields it cannot report), blanks, and percentages
    with a trailing "%" (e.g. current_charge is "0%"/"50%"), so a bare float() would raise; this
    coerces "N/A"/blank to the default and strips a trailing "%" so a percentage parses to its number.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%").strip()
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def safe_int(value, default=0):
    """Convert a value to int, returning default for None or non-numeric values (e.g. Enphase 'N/A'/'0%')."""
    result = safe_float(value, None)
    return default if result is None else int(result)


# Battery/grid flow decomposition in the /today totals. Enphase reports energy as source_dest
# pairs rather than single "charge"/"discharge"/"export" totals, so those channels are summed from
# their component flows when a direct total is not present. (source_dest = energy from source to
# dest, in Wh.) Verified key names against a battery account; the summed values themselves still
# need confirmation on a healthy (non-error) battery.
TODAY_FLOW_COMPONENTS = {
    "charge": ("solar_battery", "grid_battery"),  # energy into the battery
    "discharge": ("battery_home", "battery_grid"),  # energy out of the battery
    "export": ("solar_grid", "battery_grid", "generator_grid"),  # energy to the grid
}


def today_channel_kwh(today_data, channel):
    """Return today's kWh total for a channel from a /today payload's totals (Wh), 0.0 if absent.

    The /today endpoint reports each channel's running total for the current day in Wh under
    `stats[0].totals`; Predbat works in kWh so the value is divided by 1000. This is cadence-
    independent (unlike indexing the daily lifetime_energy array) - the cloud provides the total
    directly regardless of whether the site buckets energy per 15 minutes or per day. Battery
    charge/discharge and export have no single total key, so they are summed from their component
    source-to-destination flows (see TODAY_FLOW_COMPONENTS) when no direct total is present.
    """
    totals = (today_data or {}).get("totals") or {}
    if channel in totals:
        return round(safe_float(totals.get(channel)) / 1000.0, 3)
    components = TODAY_FLOW_COMPONENTS.get(channel)
    if components:
        total_wh = sum(safe_float(totals.get(component)) for component in components)
        return round(total_wh / 1000.0, 3)
    return 0.0


def interval_power(values, start_time, interval_length, now_ts):
    """Estimate current watts from the most recent SETTLED intra-day energy bucket.

    `values` is the /today array of per-interval energy in Wh (each bucket covers `interval_length`
    seconds starting at `start_time`, a Unix timestamp at local midnight). The bucket index for the
    current time is (now - start_time) / interval_length.

    The bucket that has only just closed is NOT usable: the cloud keeps back-filling it for several
    minutes, so its first read returns roughly a third of the eventual figure and later corrects
    upward. Reading it made every power sensor saw-tooth by ~3x on each bucket rollover. Observed
    revisions only ever touched the just-closed bucket, so stepping back ENPHASE_SETTLED_BUCKETS
    gives a value that has stopped changing, at the cost of up to one extra bucket of lag.

    Returns 0.0 when the data is missing/empty or the timing is unusable.
    """
    if not values or not interval_length or start_time is None or now_ts is None:
        return 0.0
    hours = interval_length / 3600.0
    if hours <= 0:
        return 0.0
    index = int((now_ts - start_time) / interval_length) - ENPHASE_SETTLED_BUCKETS
    if index < 0:
        index = 0
    if index >= len(values):
        index = len(values) - 1
    return round(safe_float(values[index]) / hours, 1)


def ha_time_to_enphase(value):
    """Convert an HA 'HH:MM:SS' option time to Enphase 'HH:MM' format."""
    return str(value)[:5]


def enphase_time_to_ha(value):
    """Convert an Enphase 'HH:MM' time to the HA 'HH:MM:SS' option format."""
    text = str(value or "00:00")[:5]
    return text + ":00"


def gateway_serial(today):
    """Return the gateway (Envoy) serial recorded by get_today, or None."""
    return (today or {}).get("serial")


def livestream_username(boot, site_id):
    """Build the MQTT CONNECT username that AWS IoT's custom authorizer expects.

    The livestream WebSocket carries no query parameters and no password - a browser cannot set
    custom headers on a WebSocket - so the authorizer name, the token and the token's signature all
    travel in the username as a leading-'?' query string. Field order matches the Enlighten web app.
    """
    return "?" + urlencode(
        [
            ("x-amz-customauthorizer-name", boot.get("aws_authorizer", "")),
            (boot.get("aws_token_key", "enph_token"), boot.get("aws_token_value", "")),
            ("site-id", str(site_id)),
            ("x-amz-customauthorizer-signature", boot.get("aws_digest", "")),
            ("evse-count", "0"),
            ("env", "prod"),
        ]
    )


def decode_livestream_message(payload):
    """Decode one livestream DataMsg into per-channel watts plus battery SOC.

    ``agg_p_mw`` is real power in milliwatts. The channels are measured, not derived, and satisfy
    load = pv + grid + battery exactly. Signs already match Predbat's convention (grid negative when
    exporting, battery positive when discharging). Returns None if the payload will not decode.
    """
    if not HAS_LIVESTREAM_PROTOBUF or not payload:
        return None
    try:
        message = livestream_pb.DataMsg()
        message.ParseFromString(payload)
    except Exception:
        return None
    meters = message.meters
    watts = lambda channel: round(channel.agg_p_mw / 1000.0, 1)  # noqa: E731 - milliwatts -> watts
    return {
        "pv": watts(meters.pv),
        "battery": watts(meters.storage),
        "grid": watts(meters.grid),
        "load": watts(meters.load),
        "soc": int(meters.soc),
    }


def _schedule_id_of(entry):
    """Return the cloud id of a schedule detail entry ('scheduleId', or 'id' on older shapes)."""
    return entry.get("scheduleId") or entry.get("id")


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
    cloud_limit = cloud_entry.get("limit")
    if limit is not None and (cloud_limit is None or int(cloud_limit) != int(limit)):
        return False
    return True


def decode_jwt_claims(token):
    """Decode the payload segment of a JWT without verifying the signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError):
        return {}


def is_too_many_sessions(text):
    """Return True only when a login response body reports Enlighten's 'too many active sessions' error.

    Matches the specific phrase, not a bare 'session' substring: a successful login body contains keys
    like 'session_id' and Enlighten session cookies, so a loose match would falsely reject valid logins.
    """
    lowered = str(text or "").lower()
    if "too many active sessions" in lowered:
        return True
    return "active sessions" in lowered and "too many" in lowered


class EnphaseAPI(ComponentBase):
    """Enphase Enlighten cloud API client component."""

    # Login guard-rail tunables - protect the Enphase account from lockout
    LOGIN_REUSE_SECONDS = 30
    LOGIN_COOLDOWN_SECONDS = 300
    LOGIN_SUSPEND_SECONDS = 24 * 3600
    LOGIN_MAX_REJECTS = 3

    def initialize(self, username, password, site_id=None, automatic=False, automatic_ignore_pv=False):
        """Initialise the Enphase API component state."""
        self.username = username
        self.password = password
        self.site_id = str(site_id) if site_id else None
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv

        # Verbose API-call logging (each request + a truncated, token-redacted response).
        # On for now to aid diagnosis of the unofficial API's real responses; can be disabled later.
        self.debug_api = True

        # Auth state
        self.cookie_header = ""  # serialised cookie header for Enlighten
        self.eauth_token = None  # JWT from /users/self/token
        self.manager_token = None  # enlighten_manager_token_production cookie JWT
        self.xsrf_token = None
        self.user_id = None  # decoded from JWT, needed by BatteryConfig
        self.token_expires_at = None

        # Login guard rails (avoid Enphase account lockout)
        self.login_last_success = None  # datetime of last successful login
        self.login_cooldown_until = None  # datetime before which logins are banned
        self.login_reject_count = 0  # consecutive rejected logins

        # Cloud data
        self.sites = []
        self.battery_status = {}
        self.battery_settings = {}
        self.profile = {}
        self.schedules = {}
        self.site_settings = {}
        self.today = {}  # per-site today totals (Wh) + intra-day 15-minute buckets, from /today
        self.latest_power = {}
        self.live_power = {}  # measured pv/grid/battery/load watts + soc from the Enlighten livestream

        # Local (HA-side) schedule model, written by events, applied on write switch
        self.local_schedule = {}
        # Sites whose local schedule/control model has been seeded from the cloud state (once),
        # so the control entities start out mirroring the inverter's real schedule/reserve
        self._schedule_seeded = set()

        # BatteryConfig header variant: "primary" (e-auth-token + requestid) or
        # "cookie_eauth" fallback (cookie + XHR header) needed on some regions/firmware
        self.battery_config_variant = "primary"

        # Age (datetime of last update) per cached data category
        self.data_age = {}
        self.failures_total = 0
        self.requests_today = 0
        self.last_midnight_utc = None
        self.last_error_status = None  # HTTP status (or None) of the most recent request_json() failure

        # site_id -> {family_key: True} for schedule/reserve writes that failed outright (all
        # retries exhausted), so the failure is visible on the dashboard instead of only in the
        # log - see _note_schedule_write_result / _publish_schedule_write_health.
        self.schedule_write_failed = {}

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

        # Predbat controls a single battery system: operate on one active site (the configured
        # enphase_site_id, else the first discovered). The per-category refresh gates below are
        # keyed globally, so processing one site per cycle also keeps them correct.
        site_id = self.sites[0]["site_id"] if self.sites else None
        if site_id:
            # SOC/available energy must stay fresh for planning, so it is on the fast tier;
            # the profile/settings/schedule config changes rarely (or only via our own writes),
            # so it polls on the slower settings tier. (gated on "profile", which get_profile stamps)
            if self._needs_refresh("battery_status", ENPHASE_REFRESH_STATUS):
                await self.get_battery_status(site_id)
            if self._needs_refresh("profile", ENPHASE_REFRESH_SETTINGS):
                await self.get_profile(site_id)
                await self.get_battery_settings(site_id)
                await self.get_site_settings(site_id)
                await self.get_schedules(site_id)
            if self._needs_refresh("today", ENPHASE_REFRESH_ENERGY):
                await self.get_today(site_id)
            if self._needs_refresh("latest_power", ENPHASE_REFRESH_POWER):
                await self.get_latest_power(site_id)
                # Measured instantaneous power; falls back to the /today buckets if unavailable.
                await self.get_live_power(site_id)
            self.sync_local_schedule_from_cloud(site_id)
            await self.publish_data(site_id)
            await self.publish_schedule_settings_ha(site_id)
            # Periodic reconcile, independent of the write-switch trigger - so a missed/dropped
            # trigger (e.g. an HA restart mid-cycle) cannot leave the cloud diverged from the plan
            # indefinitely. apply_battery_schedule is a no-op when nothing has changed. See #4461.
            if self._needs_refresh("schedule_sync", ENPHASE_REFRESH_SCHEDULE_SYNC):
                await self.apply_battery_schedule(site_id)
                self.data_age["schedule_sync"] = datetime.now(timezone.utc)

        # Automatic configuration on first successful data load. A site with no controllable
        # battery (e.g. PV-only) cannot be configured as a Predbat inverter - log and report
        # not-ready rather than letting the ValueError abort the whole poll.
        if first and self.automatic:
            try:
                await self.automatic_config()
            except ValueError as error:
                self.log(f"Warn: Enphase: Automatic configuration skipped - {error}")
                return False

        return True

    async def publish_data(self, site_id):
        """Publish battery, energy-today and derived instantaneous power sensors for a site.

        Reads from the normalised per-site data stores populated by the various `get_*()`
        methods, guarding every lookup with `.get()` defaults so a site missing one data
        category (e.g. no today data fetched yet) cannot crash the whole publish. The
        battery profile name is read from `self.profile`, not `self.battery_status` (the latter
        has no profile field - see the note in `get_battery_status`).
        """
        entity_base = f"sensor.{self.prefix}_enphase_{site_id}"
        now_utc = datetime.now(timezone.utc)

        status = self.battery_status.get(site_id, {})
        profile = self.profile.get(site_id, {})
        settings = self.battery_settings.get(site_id, {})
        today = self.today.get(site_id, {})

        self.dashboard_item(
            f"{entity_base}_soc_percent",
            state=status.get("soc_percent"),
            attributes={"unit_of_measurement": "%", "friendly_name": "Enphase Battery SOC", "icon": "mdi:battery-50"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_soc_kw",
            state=status.get("available_energy"),
            attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "Enphase Battery Available Energy", "icon": "mdi:battery-charging-50"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_battery_capacity",
            state=status.get("max_capacity"),
            attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "Enphase Battery Capacity", "icon": "mdi:battery-high"},
            app="enphase",
        )
        max_power_kw = status.get("max_power_kw")
        battery_rate_max = max_power_kw * 1000.0 if max_power_kw is not None else None
        self.dashboard_item(
            f"{entity_base}_battery_rate_max",
            state=battery_rate_max,
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase Battery Max Rate", "icon": "mdi:battery-charging-high"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_battery_status",
            state=status.get("status"),
            attributes={"friendly_name": "Enphase Battery Status", "icon": "mdi:information-outline"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_battery_profile",
            state=profile.get("profile"),
            attributes={"friendly_name": "Enphase Battery Profile", "icon": "mdi:cog-outline"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_battery_reserve",
            state=profile.get("reserve"),
            attributes={"unit_of_measurement": "%", "friendly_name": "Enphase Battery Reserve", "icon": "mdi:battery-lock"},
            app="enphase",
        )
        reserve_min = settings.get("veryLowSocMin")
        if reserve_min is None:
            reserve_min = 5
        self.dashboard_item(
            f"{entity_base}_battery_reserve_min",
            state=reserve_min,
            attributes={"unit_of_measurement": "%", "friendly_name": "Enphase Battery Reserve Minimum", "icon": "mdi:battery-alert"},
            app="enphase",
        )

        # Site communication/health status (siteStatus "normal"/"comm" etc.), with the cloud's
        # human-readable description as an attribute so a gateway-not-reporting fault is visible.
        self.dashboard_item(
            f"{entity_base}_system_status",
            state=today.get("site_status"),
            attributes={"friendly_name": "Enphase System Status", "icon": "mdi:cloud-check-outline", "severity": today.get("status_severity"), "description": today.get("status_desc")},
            app="enphase",
        )

        # Inverter time: the last time the battery/gateway actually reported to the Enphase cloud.
        # Predbat uses this for liveness - it stays current while the system is online and freezes
        # when the gateway goes offline, so its growing skew tells Predbat the inverter is stale.
        last_report_ts = status.get("last_report") or today.get("last_report_date")
        if last_report_ts:
            inverter_time = datetime.fromtimestamp(float(last_report_ts), self.local_tz)
        else:
            inverter_time = datetime.now(self.local_tz)
        self.dashboard_item(
            f"{entity_base}_inverter_time",
            state=inverter_time.strftime(ENPHASE_CLOCK_FORMAT),
            attributes={"friendly_name": "Enphase Inverter Time", "icon": "mdi:clock-outline"},
            app="enphase",
        )

        # Today's energy totals (kWh), one dashboard sensor per channel, sourced from the /today
        # totals (which are cadence-independent - the cloud reports the running daily total directly).
        energy_channels = {
            "production": ("pv_today", "Enphase PV Today", "mdi:solar-power"),
            "consumption": ("load_today", "Enphase Load Today", "mdi:home-lightning-bolt"),
            "import": ("import_today", "Enphase Import Today", "mdi:transmission-tower-import"),
            "export": ("export_today", "Enphase Export Today", "mdi:transmission-tower-export"),
            "charge": ("battery_charge_today", "Enphase Battery Charge Today", "mdi:battery-plus"),
            "discharge": ("battery_discharge_today", "Enphase Battery Discharge Today", "mdi:battery-minus"),
        }
        for channel, (name, friendly, icon) in energy_channels.items():
            self.dashboard_item(
                f"{entity_base}_{name}",
                state=today_channel_kwh(today, channel),
                attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing", "friendly_name": friendly, "icon": icon},
                app="enphase",
            )

        # Fallback when the livestream is unavailable: instantaneous power from the most recent
        # settled intra-day 15-minute energy bucket of
        # the /today arrays (Wh per interval -> average watts over that interval). This reads a
        # single bucket value per poll, so it is inherently stable within an interval and needs no
        # cross-poll delta tracking.
        arrays = today.get("arrays", {})
        start_time = today.get("start_time")
        interval_length = today.get("interval_length")
        now_ts = now_utc.timestamp()
        channel_watts = {channel: interval_power(arrays.get(channel, []), start_time, interval_length, now_ts) for channel in ("production", "import", "export", "charge", "discharge")}

        pv_power = channel_watts.get("production", 0.0)
        # Predbat's convention is grid positive when EXPORTING and battery positive when
        # DISCHARGING (see the power flow in web.py and the charge detection in inverter.py), so
        # export leads the grid subtraction and discharge leads the battery one.
        grid_power = round(channel_watts.get("export", 0.0) - channel_watts.get("import", 0.0), 1)
        battery_power = round(channel_watts.get("discharge", 0.0) - channel_watts.get("charge", 0.0), 1)
        # House load is the energy-balance residual of the other three, taken from the same settled
        # bucket so the four sensors agree and a power-flow display balances. This is exactly how the
        # cloud derives its own consumption channel (verified equal to within 1 Wh on 80 of 96
        # buckets), so nothing is gained by reading that channel instead.
        #
        # Being the residual, it also absorbs all the timing/rounding skew between the micros, the
        # CT clamps and the battery telemetry. While the battery is cycling hard those terms dwarf
        # the house term and the residual can go unphysical, so it is clamped at zero - a negative
        # house load would render nonsensically. load_today remains the trustworthy energy figure.
        # In Predbat's signs (grid +export, battery +discharge) the balance is pv + battery - grid.
        load_power = max(0.0, round(pv_power + battery_power - grid_power, 1))

        # Prefer the livestream when we have one: those four channels are separately metered and
        # instantaneous, where the buckets are a 15-minute average and load is only ever a residual.
        # The bucket values above stay as the fallback for when the stream is unavailable.
        live = self.live_power.get(site_id) or {}
        if live and (now_ts - live.get("read_ts", 0)) > ENPHASE_LIVE_MAX_AGE_MINUTES * 60:
            live = {}  # too old to present as current; fall back to the bucket values below
        if live:
            pv_power = live.get("pv", pv_power)
            # The livestream reports grid negative while exporting, the opposite of Predbat's sign.
            grid_power = round(-live["grid"], 1) if "grid" in live else grid_power
            battery_power = live.get("battery", battery_power)
            load_power = live.get("load", load_power)

        self.dashboard_item(
            f"{entity_base}_load_power",
            state=load_power,
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase Load Power", "icon": "mdi:home-lightning-bolt"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_pv_power",
            state=pv_power,
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase PV Power", "icon": "mdi:solar-power"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_grid_power",
            state=grid_power,
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase Grid Power", "icon": "mdi:transmission-tower"},
            app="enphase",
        )
        self.dashboard_item(
            f"{entity_base}_battery_power",
            state=battery_power,
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase Battery Power", "icon": "mdi:battery-charging"},
            app="enphase",
        )

    def _default_local_schedule(self):
        """Return an empty local schedule model."""
        return {
            "reserve": 0,
            "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
            "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False},
        }

    def sync_local_schedule_from_cloud(self, site_id):
        """One-time seed of the local schedule/control model from the current cloud state.

        So the published control entities (reserve, charge/export window times, target SOC and
        enable) start out reflecting the inverter's real schedule and reserve rather than the
        defaults. Runs only once per site (the first time cloud data is available); after that the
        control entities are driven by Predbat/user writes, so a later external change in the app
        is shown by the monitoring sensors but does not clobber Predbat's desired control values.
        """
        if site_id in self._schedule_seeded:
            return
        profile = self.profile.get(site_id)
        schedules = self.schedules.get(site_id)
        if not profile and not schedules:
            return  # no cloud data yet - nothing to seed from
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        reserve = (profile or {}).get("reserve")
        if reserve:
            local["reserve"] = reserve
        for direction, family_key in (("charge", "cfg"), ("export", "dtg")):
            entry = (schedules or {}).get(family_key, {})
            if entry.get("startTime"):
                local[direction]["start_time"] = enphase_time_to_ha(entry.get("startTime"))
                local[direction]["end_time"] = enphase_time_to_ha(entry.get("endTime"))
                if entry.get("limit") is not None:
                    local[direction]["soc"] = entry.get("limit")
                local[direction]["enable"] = bool(entry.get("enabled"))
        self._schedule_seeded.add(site_id)

    async def _publish_schedule_write_health(self, site_id):
        """Publish a dedicated sensor reporting whether recent schedule/reserve writes landed.

        A write that fails outright (a conflict surviving every retry, a failed delete or
        activation) previously only ever appeared as a log warning - `predbat.status` kept
        reporting the *intended* plan with no visible sign the cloud never actually accepted it
        (#4461). This sensor stays "on" (ok) until a write fails, and reports exactly which
        families/writes are currently failing so the mismatch is visible on the dashboard.
        """
        failures = self.schedule_write_failed.get(site_id, {})
        self.dashboard_item(
            f"binary_sensor.{self.prefix}_enphase_{site_id}_schedule_write_ok",
            state="off" if failures else "on",
            attributes={
                "friendly_name": f"Enphase {site_id} Schedule Write Ok",
                "icon": "mdi:cloud-check-outline" if not failures else "mdi:cloud-alert-outline",
                "failed": sorted(failures.keys()),
            },
            app="enphase",
        )

    async def publish_schedule_settings_ha(self, site_id):
        """Publish the schedule control entities for a site.

        Publishes the reserve control plus both the charge-from-grid and export (discharge-to-grid)
        window controls (a configured inverter always supports both - automatic_config requires
        DTG). There is no separate freeze control: Predbat freezes charge via the reserve, and
        freeze-export is derived automatically from an export target SOC of 99%. Also (re)publishes
        the schedule-write-health sensor, so a write failure is reflected immediately after the
        attempt (switch_event) and kept current on every subsequent poll (run()).
        """
        await self._publish_schedule_write_health(site_id)
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        reserve_min = int(self.battery_settings.get(site_id, {}).get("veryLowSocMin", 5) or 5)
        base_name = f"{self.prefix}_enphase_{site_id}_battery_schedule"

        self.dashboard_item(
            f"number.{base_name}_reserve",
            state=local.get("reserve", 0),
            attributes={"min": reserve_min, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"Enphase {site_id} Battery Schedule Reserve", "icon": "mdi:gauge"},
            app="enphase",
        )

        # A configured Enphase inverter always supports both charge and export (automatic_config
        # requires DTG), so both window controls are always published.
        for direction in ["charge", "export"]:
            window = local.get(direction, {})
            for attribute in ["start_time", "end_time"]:
                value = window.get(attribute, "00:00:00")
                if value not in OPTIONS_TIME_FULL:
                    value = "00:00:00"
                self.dashboard_item(
                    f"select.{base_name}_{direction}_{attribute}",
                    state=value,
                    attributes={
                        "options": OPTIONS_TIME_FULL,
                        "friendly_name": f"Enphase {site_id} Battery Schedule {direction.capitalize()} {attribute.replace('_', ' ').capitalize()}",
                        "icon": "mdi:clock-outline",
                    },
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

    def _parse_entity(self, entity_id):
        """Split a published entity id into (site_id, attribute_name), or (None, None) if not ours."""
        try:
            name = entity_id.split(".", 1)[1]
        except IndexError:
            return None, None
        marker = f"{self.prefix}_enphase_"
        if not name.startswith(marker):
            return None, None
        remainder = name[len(marker) :]
        for site in self.sites:
            site_id = site["site_id"]
            if remainder.startswith(site_id + "_"):
                return site_id, remainder[len(site_id) + 1 :]
        return None, None

    def _toggle_to_bool(self, service, current):
        """Convert an HA switch service call into the resulting boolean state."""
        if service == "turn_on":
            return True
        if service == "turn_off":
            return False
        return not current

    async def select_event(self, entity_id, value):
        """Handle a select entity change routed from HA, updating the local schedule model."""
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_") :]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        for direction in ["charge", "export"]:
            for time_key in ["start_time", "end_time"]:
                if field == f"{direction}_{time_key}" and value in OPTIONS_TIME_FULL:
                    local[direction][time_key] = value
        await self.publish_schedule_settings_ha(site_id)

    async def number_event(self, entity_id, value):
        """Handle a number entity change routed from HA, updating the local schedule model.

        The reserve is a live setting (Predbat's freeze-charge relies on it taking effect at once),
        so a reserve change is written to Enphase immediately here - like Fox - rather than waiting
        for the write button. The per-window target-SOC numbers are staged and applied on the button.
        """
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_") :]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        if field == "reserve":
            local["reserve"] = int(float(value))
            # Apply immediately (skipping a redundant write when it already matches the cached cloud value)
            if local["reserve"] and local["reserve"] != int(self.profile.get(site_id, {}).get("reserve", -1)):
                await self.set_reserve(site_id, local["reserve"])
        for direction in ["charge", "export"]:
            if field == f"{direction}_soc":
                local[direction]["soc"] = int(float(value))
        await self.publish_schedule_settings_ha(site_id)

    async def switch_event(self, entity_id, service):
        """Handle a switch service call routed from HA, updating the local schedule model.

        Turning on a "_write" switch triggers `apply_battery_schedule(site_id)`, which reconciles
        the cloud schedule/reserve state to match this model - the same reconcile that also runs
        periodically from `run()`.
        """
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_") :]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        for direction in ["charge", "export"]:
            if field == f"{direction}_enable":
                local[direction]["enable"] = self._toggle_to_bool(service, local[direction]["enable"])
            if field == f"{direction}_write" and self._toggle_to_bool(service, False):
                await self.apply_battery_schedule(site_id)
        await self.publish_schedule_settings_ha(site_id)

    def _site_timezone(self, site_id):
        """Return the IANA timezone to use for schedule writes."""
        timezone_name = self.site_settings.get(site_id, {}).get("timezone")
        return timezone_name or str(self.local_tz)

    def _is_read_only(self):
        """Return True when Predbat is in read-only mode and must not write to the account."""
        return self.get_state_wrapper(f"switch.{self.prefix}_set_read_only", default="off") == "on"

    async def _delete_schedule(self, site_id, schedule_id, context=None):
        """Delete one schedule by id. Returns True on success.

        Deletion is a POST to the schedule's /delete sub-resource. The gateway does not allow the
        DELETE verb here - it rejects it with "403 Invalid CORS request" - and because a 403 counts
        as an auth failure, using it also burned a re-login on every attempt.
        """
        result = await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules/{schedule_id}/delete", family="battery_config", allow_empty=True, context=context)
        return result is not None

    async def _prune_sibling_schedules(self, site_id, family_key, details, keep):
        """Delete every schedule in a family except the one Predbat drives, and return the survivors.

        Predbat owns one window per direction. A schedule it does not track cannot be updated or
        cleared by it, but the cloud still enforces it and still rejects any overlapping write, so
        a stray sibling wedges the family permanently. Read-only mode deletes nothing.
        """
        keep_id = _schedule_id_of(keep)
        siblings = [item for item in details if _schedule_id_of(item) != keep_id]
        if not siblings or self._is_read_only():
            return details
        survivors = [item for item in details if _schedule_id_of(item) == keep_id]
        for item in siblings:
            schedule_id = _schedule_id_of(item)
            self.log(f"Enphase: Deleting duplicate {family_key.upper()} schedule {schedule_id} on site {site_id}: {item.get('startTime')}-{item.get('endTime')} (Predbat drives one window per direction)")
            if await self._delete_schedule(site_id, schedule_id):
                continue
            self.log(f"Warn: Enphase: Failed to delete duplicate {family_key.upper()} schedule {schedule_id} on site {site_id}; it may conflict with Predbat's writes")
            survivors.append(item)
        return survivors

    def _is_schedule_pending(self, site_id, family_key):
        """Return True when the cached cloud schedule exists but is stuck in pending status.

        Reflects whatever `get_schedules` last read - `_reconcile_once` always re-reads before
        checking this, so it is never more than one API call stale.
        """
        entry = self.schedules.get(site_id, {}).get(family_key.lower(), {})
        # get_schedules always stores a "status" key (None when the cloud omits scheduleStatus),
        # so coalesce to "" before comparing rather than relying on the dict-get default.
        return isinstance(entry, dict) and (entry.get("status") or "").lower() == "pending"

    def _note_schedule_write_result(self, site_id, family_key, ok):
        """Track whether the most recent write/activation for a schedule family landed on the cloud.

        A write that fails outright (a conflict surviving every retry, a failed delete, a failed
        activation PUT) otherwise only ever shows up as a generic HTTP warning in the log -
        `predbat.status` keeps reporting the *intended* plan with no visible sign the device never
        actually changed (#4461). ``count_errors`` feeds the existing per-component error count on
        the `components_healthy` sensor; ``schedule_write_failed`` backs the dedicated per-family
        warning published by `_publish_schedule_write_health`. Cleared on the next successful write.
        """
        failures = self.schedule_write_failed.setdefault(site_id, {})
        if ok:
            failures.pop(family_key, None)
        else:
            failures[family_key] = True
            self.count_errors += 1

    async def _activate_control_mode(self, site_id, family, body, apply_cache, label):
        """Commit a freshly written schedule to the gateway via a batterySettings PUT.

        A schedule write leaves the schedule in "pending" status; this follow-up PUT
        (carrying a per-mode ``body``) transitions it to active so the gateway acts on it.
        On success it clears the cached pending marker and calls ``apply_cache`` to
        optimistically record the change in ``battery_settings``; on failure it just logs -
        `_reconcile_once` always re-reads from the cloud on its next attempt, so there is no local
        cache to invalidate. Returns True if the PUT succeeded.
        """
        params = {"source": "enho"}
        if self.user_id:
            params["userId"] = self.user_id
        result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", params=params, json_body=body, context=f"{label} activation")
        family_key = family.lower()
        if result is not None:
            apply_cache(self.battery_settings.setdefault(site_id, {}))
            entry = self.schedules.get(site_id, {}).get(family_key)
            if isinstance(entry, dict):
                entry.pop("status", None)
        else:
            self.log(f"Warn: Enphase: {label} activation failed for site {site_id}")
        self._note_schedule_write_result(site_id, family_key, result is not None)
        return result is not None

    async def _activate_cfg_mode(self, site_id, family=SCHEDULE_CHARGE):
        """Activate charge-from-grid (CFG) after writing its schedule.

        The activation PUT both accepts the ITC disclaimer inline (``acceptedItcDisclaimer``)
        and enables ``chargeFromGrid``, transitioning the pending schedule to active so the
        gateway starts charging. See _activate_control_mode for success/failure handling.
        """
        body = {"chargeFromGrid": True, "acceptedItcDisclaimer": datetime.now(timezone.utc).isoformat()}

        def apply_cache(settings):
            """Record the enabled charge-from-grid setting in the battery-settings cache."""
            settings["chargeFromGrid"] = True

        return await self._activate_control_mode(site_id, family, body, apply_cache, "CFG")

    async def _activate_dtg_mode(self, site_id, family=SCHEDULE_EXPORT):
        """Activate discharge-to-grid (DTG) after writing its schedule; see _activate_control_mode."""

        def apply_cache(settings):
            """Record the enabled dtgControl setting in the battery-settings cache."""
            settings.setdefault("dtgControl", {})["enabled"] = True

        return await self._activate_control_mode(site_id, family, {"dtgControl": {"enabled": True}}, apply_cache, "DTG")

    async def _activate_rbd_mode(self, site_id, family=SCHEDULE_FREEZE):
        """Activate restrict-battery-discharge (RBD) after writing its schedule; see _activate_control_mode."""

        def apply_cache(settings):
            """Record the enabled rbdControl setting in the battery-settings cache."""
            settings.setdefault("rbdControl", {})["enabled"] = True

        return await self._activate_control_mode(site_id, family, {"rbdControl": {"enabled": True}}, apply_cache, "RBD")

    async def _ensure_charge_from_grid(self, site_id):
        """Accept the one-time ITC disclaimer required before charge-from-grid can be enabled.

        The ``chargeFromGrid`` battery setting itself is enabled by the CFG activation PUT
        (_activate_cfg_mode), so this only performs the disclaimer acceptance and avoids a
        redundant second batterySettings write on the enable path.
        """
        if self.battery_settings.get(site_id, {}).get("chargeFromGrid"):
            return
        self.log(f"Enphase: Accepting ITC disclaimer on site {site_id}")
        await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/batterySettings/acceptDisclaimer/{site_id}", family="battery_config", json_body={"disclaimer-type": "itc"})

    async def set_reserve(self, site_id, reserve):
        """Write the battery backup reserve (batteryBackupPercentage) via a profile PUT.

        Preserves the current profile name (so only the reserve changes). Returns the parsed
        response, or None on failure.
        """
        # Bootstrap a fresh XSRF token immediately before the write (its x-csrf-token response header
        # and XSRF cookie are absorbed for the double-submit the PUT needs).
        await self.get_site_settings(site_id)
        cloud = self.profile.get(site_id, {})
        profile_name = cloud.get("profile") or PROFILE_SELF_CONSUMPTION
        self.log(f"Enphase: Setting reserve to {int(reserve)}% (profile {profile_name}) on site {site_id}")
        params = {"source": "enho"}
        if self.user_id:
            params["userId"] = self.user_id
        result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/profile/{site_id}", family="battery_config", params=params, json_body={"profile": profile_name, "batteryBackupPercentage": int(reserve)}, context="reserve update")
        if result is not None:
            # Optimistically cache the written reserve; the periodic profile re-read will correct
            # it if the write did not actually land (e.g. the gateway never activated it).
            self.profile.setdefault(site_id, {})["reserve"] = int(reserve)
        self._note_schedule_write_result(site_id, "reserve", result is not None)
        return result

    def _desired_schedule_families(self, local):
        """Compute the desired {enabled, start, end, limit} state for cfg/dtg/rbd from `local`.

        Mirrors Predbat's SOC-target encoding for the export direction: target below 99% -> a
        real forced export to that floor (DTG); exactly 99% -> "freeze export" (hold, don't
        discharge), mapped to restrict-battery-discharge (RBD); 100% or disabled -> neither. DTG
        and RBD always share the same window and are mutually exclusive by construction (a target
        cannot be both <99 and ==99), so at most one of them is ever enabled - see the "Update
        strategy" note in docs/superpowers/specs/2026-08-08-enphase-schedule-reconcile-design.md.
        Freeze *charge* is not handled here - Predbat freezes charge via the reserve (raising it
        to the current SOC) and disabling the charge window, using the existing reserve/charge
        controls. Start/end are converted to Enphase "HH:MM" format; everything downstream works
        in that format only.
        """
        charge = local.get("charge", {})
        export = local.get("export", {})
        export_enabled = bool(export.get("enable", False))
        export_soc = int(export.get("soc", 5))
        export_start = ha_time_to_enphase(export.get("start_time", "00:00:00"))
        export_end = ha_time_to_enphase(export.get("end_time", "00:00:00"))
        # Clamp the DTG floor to at least the reserve: Enphase will not discharge below the backup
        # reserve, and Predbat's own discharge target is max(export, reserve), so keep the written
        # limit consistent rather than requesting an export below the reserve it can never reach.
        dtg_limit = max(export_soc, int(local.get("reserve", 0)))
        return {
            "cfg": {"enabled": bool(charge.get("enable")), "start": ha_time_to_enphase(charge.get("start_time", "00:00:00")), "end": ha_time_to_enphase(charge.get("end_time", "00:00:00")), "limit": charge.get("soc", 100)},
            "dtg": {"enabled": export_enabled and export_soc < 99, "start": export_start, "end": export_end, "limit": dtg_limit},
            "rbd": {"enabled": export_enabled and export_soc == 99, "start": export_start, "end": export_end, "limit": None},
        }

    async def _cleanup_family(self, site_id, family_key, target, force_recreate):
        """Delete a family's existing schedule when it must not survive into the write phase.

        Covers two cases: the family is being disabled (the cloud ignores isEnabled=False on a
        PUT, so deleting is the only way to retire a window), or - per the "Update strategy" rule
        in the design doc - it is one of several families changing in this pass, so it is cleared
        before any write happens anywhere. That closes the cross-family overlap gap from #4461: a
        new window for one family can otherwise collide with a *different* family's old,
        not-yet-updated one even when neither family's new windows overlap each other. A single
        family changing alone skips this - `_converge_family`'s PUT-in-place is safe when nothing
        else is moving.
        """
        entry = self.schedules.get(site_id, {}).get(family_key, {})
        schedule_id = entry.get("id")
        if not schedule_id or (target["enabled"] and not force_recreate):
            return True
        family = family_key.upper()
        reason = "window no longer required" if not target["enabled"] else "clearing before a multi-family change"
        self.log(f"Enphase: Deleting {family} schedule {schedule_id} on site {site_id} ({reason})")
        if not await self._delete_schedule(site_id, schedule_id, context=f"{family} {reason}"):
            # Always recorded, even when target["enabled"] - a failure here is real even if the
            # family's own _converge_family call (still to come this pass) goes on to self-heal it
            # with a fallback PUT to the same id, which would then clear this again.
            self._note_schedule_write_result(site_id, family_key, False)
            return False
        # Drop the id/window so a re-enable creates a fresh schedule rather than PUTting an id the
        # cloud no longer knows about.
        cleared = dict(entry)
        for field in ("id", "startTime", "endTime", "limit"):
            cleared.pop(field, None)
        cleared["enabled"] = False
        self.schedules.setdefault(site_id, {})[family_key] = cleared
        if not target["enabled"]:
            self._note_schedule_write_result(site_id, family_key, True)
        return True

    async def _converge_family(self, site_id, family_key, target, force_activate=False):
        """Write and activate one family to match `target`, if it still needs it after cleanup.

        Any id present here already survived `_cleanup_family`, so PUT-in-place is safe; its
        absence means create. Activates when the schedule was just written, when
        `force_activate` says the underlying battery setting still needs enabling (CFG's
        chargeFromGrid), or when the cache already matches but the cloud still reports the family
        pending (written on an earlier pass, activation never landed).
        """
        if not target["enabled"]:
            return True  # nothing to write or activate; _cleanup_family handles disabling
        family = family_key.upper()
        entry = self.schedules.get(site_id, {}).get(family_key, {})
        wrote = False
        if not schedules_equal(entry, target["start"], target["end"], target["limit"], True):
            payload = {"timezone": self._site_timezone(site_id), "startTime": target["start"], "endTime": target["end"], "scheduleType": family, "days": [1, 2, 3, 4, 5, 6, 7], "isEnabled": True}
            if target["limit"] is not None:
                payload["limit"] = int(target["limit"])
            schedule_id = entry.get("id")
            if schedule_id:
                self.log(f"Enphase: Updating {family} schedule {schedule_id} on site {site_id}: {target['start']}-{target['end']} limit={target['limit']}")
                result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules/{schedule_id}", family="battery_config", json_body=payload, context=f"{family} schedule update")
                if result is not None:
                    updated = dict(entry)
                    updated.update({"startTime": target["start"], "endTime": target["end"], "limit": target["limit"], "enabled": True})
                    self.schedules.setdefault(site_id, {})[family_key] = updated
            else:
                self.log(f"Enphase: Creating {family} schedule on site {site_id}: {target['start']}-{target['end']} limit={target['limit']}")
                result = await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules", family="battery_config", json_body=payload, context=f"{family} schedule create")
                if result is not None:
                    # Re-read once so we learn the new schedule's cloud-assigned id for future
                    # edits - the create response does not return it.
                    await self.get_schedules(site_id)
            wrote = result is not None
            self._note_schedule_write_result(site_id, family_key, wrote)
            if not wrote:
                return False

        if wrote or force_activate or self._is_schedule_pending(site_id, family_key):
            activate = {"cfg": self._activate_cfg_mode, "dtg": self._activate_dtg_mode, "rbd": self._activate_rbd_mode}[family_key]
            if family_key == "cfg":
                await self._ensure_charge_from_grid(site_id)
            return await activate(site_id)
        return True

    async def _converge_reserve(self, site_id, local):
        """Write the reserve via a profile PUT if it differs from the cached cloud value."""
        desired_reserve = int(local.get("reserve", 0))
        cloud = self.profile.get(site_id, {})
        if not desired_reserve or desired_reserve == int(cloud.get("reserve", -1)):
            return True
        return await self.set_reserve(site_id, desired_reserve) is not None

    async def _reconcile_once(self, site_id):
        """Run one read-clean-write pass, converging the cloud to Predbat's desired local model.

        Always starts from a fresh cloud read - no state is trusted between passes. Cleanup
        (deleting schedules that must not survive) runs for every changing family before any
        family writes anything, so a cross-family collision can never happen; see
        `_cleanup_family`. Returns True only if every step this pass succeeded;
        `apply_battery_schedule` retries a bounded number of times on False.
        """
        await self.get_schedule_settings_ha(site_id)
        # Bootstrap a fresh XSRF token before writing (the web app GETs siteSettings first); its
        # x-csrf-token response header is absorbed by request_json for the writes below.
        await self.get_site_settings(site_id)
        await self.get_schedules(site_id)
        local = self.local_schedule.get(site_id, self._default_local_schedule())
        desired = self._desired_schedule_families(local)

        changed = [key for key in FAMILY_ORDER if not schedules_equal(self.schedules.get(site_id, {}).get(key, {}), desired[key]["start"], desired[key]["end"], desired[key]["limit"], desired[key]["enabled"])]
        # PUT-in-place is only safe when nothing else is moving in the same pass - see
        # _cleanup_family's docstring for why a single family changing alone is fine.
        force_recreate = len(changed) > 1

        ok = True
        for key in changed:
            if not await self._cleanup_family(site_id, key, desired[key], force_recreate):
                ok = False

        cfg_force_activate = desired["cfg"]["enabled"] and not self.battery_settings.get(site_id, {}).get("chargeFromGrid")
        for key in FAMILY_ORDER:
            force_activate = cfg_force_activate if key == "cfg" else False
            if not await self._converge_family(site_id, key, desired[key], force_activate):
                ok = False

        if not await self._converge_reserve(site_id, local):
            ok = False
        return ok

    async def apply_battery_schedule(self, site_id):
        """Reconcile the cloud schedule/reserve state to match Predbat's desired local model.

        Runs `_reconcile_once` up to `ENPHASE_RECONCILE_MAX_ATTEMPTS` times, with a short jittered
        backoff between attempts on failure (e.g. a conflict, or a family still settling from a
        previous write). If it still cannot converge, it gives up for this call - the next
        periodic call (`run()`, every `ENPHASE_REFRESH_SCHEDULE_SYNC` minutes) or the next
        explicit trigger (the write switch) starts over from a clean read. Does nothing in
        read-only mode. See docs/superpowers/specs/2026-08-08-enphase-schedule-reconcile-design.md
        for the design. Returns True if the site converged to the desired state.
        """
        if self._is_read_only():
            return True
        for attempt in range(ENPHASE_RECONCILE_MAX_ATTEMPTS):
            if await self._reconcile_once(site_id):
                return True
            if attempt < ENPHASE_RECONCILE_MAX_ATTEMPTS - 1:
                await asyncio.sleep(1 + attempt * random.random() * 3)
        return False

    async def get_battery_status(self, site_id):
        """Fetch and normalise battery SOC/capacity/power for a site."""
        data = await self.request_json("GET", f"/pv/settings/{site_id}/battery_status.json")
        if data is None:
            return None
        batteries = data.get("storages") or []
        total_capacity = sum(safe_float(b.get("max_capacity")) for b in batteries)
        total_available = sum(safe_float(b.get("available_energy")) for b in batteries)
        if total_capacity > 0:
            soc_percent = round(total_available / total_capacity * 100.0, 1)
        else:
            soc_percent = safe_float(data.get("current_charge"))
        # Most recent per-battery report time (Unix seconds) - the "last time the battery was
        # online". This stays fresh while the battery reports and freezes when the gateway goes
        # offline, so it drives Predbat's inverter-time liveness/skew detection.
        report_times = [safe_float(b.get("last_report"), None) for b in batteries]
        last_report = max([t for t in report_times if t], default=None)
        self.battery_status[site_id] = {
            "soc_percent": soc_percent,
            "available_energy": safe_float(data.get("available_energy"), total_available),
            "max_capacity": safe_float(data.get("max_capacity"), total_capacity),
            "max_power_kw": safe_float(data.get("max_power")),
            "status": str(data.get("status", "")),
            "last_report": last_report,
            # Note: this payload has no "profile" key. The battery profile name is
            # sourced from self.profile[site_id]["profile"], populated by get_profile().
            "batteries": batteries,
        }
        await self._save_cache("battery_status", self.battery_status)
        return self.battery_status[site_id]

    async def get_today(self, site_id):
        """Fetch today's per-channel totals and intra-day 15-minute buckets for a site.

        Uses GET /pv/systems/<site>/today, whose stats[0].totals gives each channel's running
        total for today (in Wh) and whose per-channel arrays are intra-day energy buckets of
        stats[0].interval_length seconds starting at stats[0].start_time. Stored normalised for
        publish_data to turn into the *_today (kWh) and instantaneous power (W) sensors.
        """
        data = await self.request_json("GET", f"/pv/systems/{site_id}/today")
        if data is None:
            return None
        stats = data.get("stats") or []
        stat = stats[0] if isinstance(stats, list) and stats else {}
        channels = ("production", "consumption", "import", "export", "charge", "discharge")
        status_details = data.get("statusDetails") or {}
        self.today[site_id] = {
            "totals": stat.get("totals") or {},
            "arrays": {channel: (stat.get(channel) or []) for channel in channels},
            "start_time": stat.get("start_time"),
            "interval_length": stat.get("interval_length"),
            # Site health: siteStatus is "normal"/"comm" (communication fault) etc., with a
            # human-readable status description when there is a problem (e.g. gateway not reporting).
            # Gateway (Envoy) serial, needed to bootstrap the livestream - saves a separate call.
            "serial": ((data.get("connectionDetails") or [{}])[0] or {}).get("serial_num"),
            "site_status": data.get("siteStatus"),
            "status_severity": status_details.get("statusSeverity"),
            "status_desc": status_details.get("statusDesc"),
            "last_report_date": data.get("last_report_date"),
        }
        await self._save_cache("today", self.today)
        return self.today[site_id]

    async def get_live_power(self, site_id):
        """Fetch one instantaneous, measured power reading from the Enlighten livestream.

        The Enlighten app streams a protobuf `DataMsg` once a second over MQTT-on-WebSockets from
        AWS IoT, carrying separately METERED pv/storage/grid/load channels plus SOC. That is the
        only source of a real house-load figure: the /today energy buckets can only yield load as
        the residual of much larger numbers, which is unusable while the battery cycles, and
        get_latest_power reports production rather than consumption.

        Predbat only needs one sample per cycle, so this connects, takes the first message and
        disconnects - the same lifecycle the web app uses - rather than holding the stream open and
        re-authorising every `live_stream_duration` (900s). Returns the reading, or None on any
        failure, leaving the caller to fall back to the bucket-derived values.
        """
        reading = await self._fetch_live_power(site_id)
        if reading:
            # Stamped on arrival: DataMsg.timestamp is a constant, so the payload cannot date itself.
            reading["read_ts"] = datetime.now(timezone.utc).timestamp()
            self.live_power[site_id] = reading
        # A failure deliberately leaves any previous reading alone - publish_data ages it out after
        # ENPHASE_LIVE_MAX_AGE_MINUTES rather than dropping to the lagging buckets over one blip.
        return reading

    async def _fetch_live_power(self, site_id):
        """Bootstrap the livestream and return one decoded reading, or None if unavailable."""
        if not (HAS_AIOMQTT and HAS_LIVESTREAM_PROTOBUF):
            return None
        serial = gateway_serial(self.today.get(site_id, {}))
        if not serial:
            return None
        boot = await self.request_json("GET", LIVESTREAM_BOOTSTRAP, params={"serial_num": serial})
        if not boot or not boot.get("aws_iot_endpoint") or not boot.get("live_stream_topic"):
            return None
        return await self._read_livestream(site_id, boot, serial)

    async def _read_livestream(self, site_id, boot, serial):
        """Connect to AWS IoT, take the first livestream message for a site, then disconnect.

        Credentials ride in the MQTT CONNECT username (see livestream_username) because the
        WebSocket carries no query string and no password. Any failure is logged and swallowed -
        the livestream is an enhancement, never a reason to fail a cycle.
        """
        timeout = safe_float(boot.get("timeout"), ENPHASE_LIVESTREAM_TIMEOUT) or ENPHASE_LIVESTREAM_TIMEOUT
        topic = boot.get("live_stream_topic")

        async def consume():
            """Subscribe and return the first decodable reading."""
            async with aiomqtt.Client(
                hostname=boot["aws_iot_endpoint"],
                port=443,
                transport="websockets",
                websocket_path="/mqtt",
                tls_context=ssl.create_default_context(),
                identifier=f"em-paho-mqtt-{random.randint(10000, 99999)}-{serial}",
                username=livestream_username(boot, site_id),
                clean_session=True,
                keepalive=60,
            ) as client:
                await client.subscribe(topic, qos=0)
                async for message in client.messages:
                    reading = decode_livestream_message(bytes(message.payload))
                    if reading:
                        return reading
            return None

        try:
            reading = await asyncio.wait_for(consume(), timeout=timeout)
        except asyncio.TimeoutError:
            self.log(f"Warn: Enphase: Livestream timed out after {timeout}s for site {site_id}")
            record_api_call("enphase", False, "livestream_timeout")
            return None
        except Exception as error:
            self.log(f"Warn: Enphase: Livestream failed for site {site_id}: {error}")
            record_api_call("enphase", False, "livestream_error")
            return None
        record_api_call("enphase", True)
        return reading

    async def get_latest_power(self, site_id):
        """Fetch and normalise the latest instantaneous power reading for a site."""
        data = await self.request_json("GET", f"/app-api/{site_id}/get_latest_power")
        if data is None:
            return None
        latest_power = data.get("latest_power") or {}
        timestamp = safe_float(latest_power.get("time"), None)
        if timestamp is not None and timestamp > 1e12:
            timestamp = timestamp / 1000.0
        self.latest_power[site_id] = {
            "watts": safe_float(latest_power.get("value")),
            "time": timestamp,
        }
        await self._save_cache("latest_power", self.latest_power)
        return self.latest_power[site_id]

    async def get_profile(self, site_id):
        """Fetch and store the battery operating profile and backup reserve for a site."""
        params = {"source": "enho"}
        if self.user_id:
            params["userId"] = self.user_id
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/profile/{site_id}", family="battery_config", params=params)
        if data is None:
            return None
        # The real response wraps the fields in a "data" object: {"type": "profile-details", "data": {...}}
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        self.profile[site_id] = {
            "profile": str(body.get("profile", "")),
            "reserve": safe_int(body.get("batteryBackupPercentage")),
        }
        await self._save_cache("profile", self.profile)
        return self.profile[site_id]

    async def get_battery_settings(self, site_id):
        """Fetch and store the battery charge-from-grid and low-SOC settings for a site."""
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", params={"source": "enlm"})
        if data is None:
            return None
        # The real response wraps the fields in a "data" object: {"type": "battery-details", "data": {...}}
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        self.battery_settings[site_id] = {
            "chargeFromGrid": bool(body.get("chargeFromGrid", False)),
            "veryLowSoc": safe_int(body.get("veryLowSoc"), None),
            "veryLowSocMin": safe_int(body.get("veryLowSocMin"), None),
            "veryLowSocMax": safe_int(body.get("veryLowSocMax"), None),
        }
        await self._save_cache("battery_settings", self.battery_settings)
        return self.battery_settings[site_id]

    async def get_site_settings(self, site_id):
        """Fetch the BatteryConfig site feature/capability flags for a site.

        GET /service/batteryConfig/api/v1/siteSettings/<site>?userId=<uid>. Returns capability
        flags under a "data" object (hasEncharge, hasAcb, showChargeFromGrid, isEnsemble, ...).
        This is also the web app's primary XSRF bootstrap: the response's x-csrf-token header (and
        cookies) are absorbed by request_json, refreshing the token used for subsequent writes.
        """
        params = {}
        if self.user_id:
            params["userId"] = self.user_id
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/siteSettings/{site_id}", family="battery_config", params=params or None)
        if data is None:
            return None
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        self.site_settings[site_id] = body
        await self._save_cache("site_settings", self.site_settings)
        return self.site_settings[site_id]

    async def get_schedules(self, site_id):
        """Fetch and normalise the charge/export/freeze battery schedules for a site.

        Each family (cfg/dtg/rbd) reports ``scheduleStatus``, ``count`` and, when count > 0, a
        ``details`` list of schedule objects. A schedule object (confirmed against a battery
        account) carries ``scheduleId``, ``startTime``/``endTime`` (HH:MM), ``limit``, ``days``,
        ``isEnabled`` and ``isDeleted``. Predbat drives one window per direction, so exactly one
        schedule per family is used: the one already adopted (matched by ``scheduleId``, which the
        write path then updates in place), else the first non-deleted entry. The list order is not
        stable - the cloud sorts by ``updatedAt`` - so the adopted id must be matched rather than
        the position taken. In write mode any other schedule in the family is deleted, because a
        schedule Predbat does not track still blocks overlapping writes with HTTP 409.
        """
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules", family="battery_config")
        if data is None:
            return None
        parsed = {}
        for family_key in ("cfg", "dtg", "rbd"):
            family_data = data.get(family_key) or {}
            details = [item for item in (family_data.get("details") or []) if isinstance(item, dict) and not item.get("isDeleted")]
            # Stay on the schedule we already adopted. The cloud orders details by updatedAt, so
            # writing to our schedule pushes it behind any sibling and taking details[0] would swap
            # us onto that sibling; we would then write a window overlapping the one we just wrote
            # and the cloud would reject it with HTTP 409 (CONFLICTING_SCHEDULE_*).
            adopted_id = (self.schedules.get(site_id, {}).get(family_key) or {}).get("id")
            entry = next((item for item in details if _schedule_id_of(item) == adopted_id), None) if adopted_id else None
            if entry is None:
                # Nothing adopted yet, or the adopted schedule was removed outside Predbat.
                entry = details[0] if details else {}
            details = await self._prune_sibling_schedules(site_id, family_key, details, entry)
            # "supported" gates whether Predbat can use this schedule family. Real accounts report
            # a per-family scheduleStatus; "active" and "pending" have both been seen, and
            # "not_supported" is how a genuinely unavailable family reports. "pending" only means a
            # schedule change is still settling on the gateway - which is the normal state straight
            # after any write Predbat makes - so it must not be read as unsupported, or Predbat
            # decides mid-run that the site cannot charge from grid and abandons configuration.
            # A family that actually holds a schedule is supported whatever the status says.
            status_text = str(family_data.get("scheduleStatus", "")).strip().lower()
            supported = status_text in ("active", "enabled", "supported", "available", "pending") or bool(details) or bool(family_data.get("scheduleSupported") or family_data.get("forceScheduleSupported"))
            parsed[family_key] = {
                "id": entry.get("scheduleId") or entry.get("id"),
                "startTime": entry.get("startTime"),
                "endTime": entry.get("endTime"),
                "limit": safe_int(entry.get("limit"), None),
                "enabled": bool(entry.get("isEnabled", False)),
                "supported": supported,
                "count": len(details),
                "status": family_data.get("scheduleStatus"),
            }
        self.schedules[site_id] = parsed
        await self._save_cache("schedules", self.schedules)
        return parsed

    def dtg_supported(self, site_id):
        """Return True when the export-to-grid (dtg) schedule family is supported for a site."""
        return bool(self.schedules.get(site_id, {}).get("dtg", {}).get("supported", False))

    async def automatic_config(self):
        """Automatically configure Predbat inverter args from the discovered Enphase site.

        Single-site only: the first discovered site is used. Points every generic Predbat
        inverter arg at the entities published by `publish_data()` / `publish_schedule_settings_ha()`
        for that site. Export/discharge args are only set when the site supports the "dtg"
        (discharge-to-grid) schedule family, since not every Enphase system offers it.
        """
        if not self.sites:
            raise ValueError("Enphase API: No sites found, cannot configure")
        site_id = self.sites[0]["site_id"]
        status = self.battery_status.get(site_id, {})
        if not status.get("max_capacity"):
            raise ValueError("Enphase API: No battery found on site, cannot configure")
        # Predbat needs both charge and export control to plan properly, so require the
        # charge-from-grid (CFG) and discharge-to-grid (DTG) schedule families. If either is
        # unsupported, fail configuration rather than publishing an inverter Predbat cannot drive.
        if not self.schedules.get(site_id, {}).get("cfg", {}).get("supported", False):
            raise ValueError("Enphase API: Charge-from-grid (CFG) scheduling not supported on this site, cannot configure")
        if not self.dtg_supported(site_id):
            raise ValueError("Enphase API: Discharge-to-grid (DTG) scheduling not supported on this site, cannot configure")
        entity = f"{self.prefix}_enphase_{site_id}"

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
        self.set_arg("inverter_time", [f"sensor.{entity}_inverter_time"])
        self.set_arg("charge_start_time", [f"select.{entity}_battery_schedule_charge_start_time"])
        self.set_arg("charge_end_time", [f"select.{entity}_battery_schedule_charge_end_time"])
        self.set_arg("charge_limit", [f"number.{entity}_battery_schedule_charge_soc"])
        self.set_arg("scheduled_charge_enable", [f"switch.{entity}_battery_schedule_charge_enable"])
        self.set_arg("scheduled_discharge_enable", [f"switch.{entity}_battery_schedule_export_enable"])
        self.set_arg("discharge_start_time", [f"select.{entity}_battery_schedule_export_start_time"])
        self.set_arg("discharge_end_time", [f"select.{entity}_battery_schedule_export_end_time"])
        self.set_arg("discharge_target_soc", [f"number.{entity}_battery_schedule_export_soc"])
        self.set_arg("schedule_write_button", [f"switch.{entity}_battery_schedule_charge_write"])
        # export_limit is deliberately not set here: the Enphase cloud does not report a grid
        # export power limit, and hardcoding one would override the user's apps.yaml export_limit.
        # Leaving it unset lets the user configure it (Predbat defaults to unlimited otherwise).

    def login_allowed(self):
        """Return True when a password login attempt is currently permitted by the guard rails."""
        if self.login_cooldown_until and datetime.now(timezone.utc) < self.login_cooldown_until:
            return False
        return True

    def _login_rejected(self, reason, unrecoverable=False):
        """Record a rejected login and set the appropriate cooldown.

        Fatal (app-wide) error signalling is reserved for genuinely unrecoverable
        states (MFA required, account blocked) or once the suspend tier is reached
        after repeated transient rejections - a single 401/403/no-token/session
        rejection must not mark the whole app as not-running.
        """
        self.login_reject_count += 1
        if self.login_reject_count >= self.LOGIN_MAX_REJECTS:
            delay = self.LOGIN_SUSPEND_SECONDS
        else:
            delay = self.LOGIN_COOLDOWN_SECONDS
        self.login_cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=delay)
        self.log(f"Warn: Enphase: Login rejected ({reason}), cooling down for {delay} seconds (rejection {self.login_reject_count})")
        if unrecoverable or self.login_reject_count >= self.LOGIN_MAX_REJECTS:
            self.fatal_error_occurred()

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
        # Log the response only - the request body (password) is never passed to the logger.
        self._log_api_call("POST", LOGIN_PATH, None, status, data, text)

        if status in (401, 403):
            self._login_rejected("invalid credentials")
            return False
        if isinstance(data, dict) and data.get("requires_mfa"):
            self._login_rejected("account requires MFA - disable MFA on the Enphase account to use this component", unrecoverable=True)
            return False
        if isinstance(data, dict) and data.get("isBlocked"):
            self._login_rejected("account is blocked", unrecoverable=True)
            return False
        if is_too_many_sessions(text):
            # "Too many active sessions" - detect regardless of HTTP status, Enlighten sometimes returns 200
            self._login_rejected("too many active sessions")
            return False
        if status != 200:
            self._login_rejected(f"http status {status}")
            return False

        # Persist cookies from the login (session cookie + manager token JWT)
        self._absorb_cookies(cookies)

        # Mint the e-auth/bearer token; Enlighten may rotate the session cookie here
        status, token_data, text, cookies = await self.request_raw("GET", BASE_URL + SELF_TOKEN_PATH, headers=self.get_headers("site"))
        self._log_api_call("GET", SELF_TOKEN_PATH, None, status, token_data, text)
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
        self._log_api_call("GET", SITE_SEARCH_PATH, None, status, sites_data, text)
        sites = []
        if status == 200:
            entries = sites_data if isinstance(sites_data, list) else (sites_data or {}).get("sites", [])
            for entry in entries:
                sid = str(entry.get("site_id") or entry.get("id") or "")
                if sid and (not self.site_id or sid == self.site_id):
                    sites.append({"site_id": sid, "name": entry.get("name", sid)})
        if sites:
            # Deduplicate by site id preserving order - Enlighten can return the same site more than once
            seen = set()
            deduped = []
            for site in sites:
                if site["site_id"] not in seen:
                    seen.add(site["site_id"])
                    deduped.append(site)
            self.sites = deduped
            await self._save_cache("sites", self.sites)
            if len(self.sites) > 1 and not self.site_id:
                self.log(f"Warn: Enphase: {len(self.sites)} sites found; using the first ({self.sites[0]['site_id']}). Set enphase_site_id to choose a specific site.")

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
        # The XSRF token cookie is named XSRF-TOKEN or BP-XSRF-Token (case varies); take the first
        # match. It is used for BatteryConfig writes as both the X-XSRF-Token header and cookie.
        for name, value in current.items():
            if value and "xsrf" in name.lower() and "token" in name.lower():
                self.xsrf_token = value
                break

    def get_headers(self, family, write=False):
        """Build request headers for an endpoint family ('site' or 'battery_config')."""
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
                # The BatteryConfig service returns a fresh XSRF token in the x-csrf-token response
                # header on every call; fold it into the cookie dict so _absorb_cookies keeps our
                # X-XSRF-Token current for the next write (the web app's primary bootstrap mechanism).
                csrf_header = response.headers.get("x-csrf-token") or response.headers.get("X-CSRF-Token")
                if csrf_header:
                    cookies["XSRF-TOKEN"] = csrf_header
                json_data = None
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type:
                    try:
                        json_data = await response.json(content_type=None)
                    except ValueError:
                        json_data = None
                return response.status, json_data, text, cookies

    def _log_api_call(self, method, path, params, status, json_data, text):
        """Log one API call and a truncated, token-redacted view of its response (when debug_api is on)."""
        if not self.debug_api:
            return
        if isinstance(json_data, dict):
            redacted = dict(json_data)
            # aws_token_value/aws_digest are the livestream's AWS IoT credentials - short-lived,
            # but Predbat logs get shared for debugging, so they must never be written out.
            for key in ("token", "auth_token", "access_token", "aws_token_value", "aws_digest"):
                if key in redacted:
                    redacted[key] = "***redacted***"
            preview = json.dumps(redacted, default=str)
        elif json_data is not None:
            preview = json.dumps(json_data, default=str)
        elif self._is_login_wall(json_data, text):
            # Don't dump the full HTML login/marketing page (tens of KB) - it is noise, and it is
            # the expected trigger for the BatteryConfig header-variant fallback.
            preview = f"(HTML page, {len(text or '')} chars - login wall / variant fallback)"
        else:
            preview = text or ""
        param_str = f" params={params}" if params else ""
        self.log(f"Enphase API: {method} {path}{param_str} -> {status} {preview}")

    def _is_login_wall(self, json_data, text):
        """Return True when a JSON endpoint answered with an HTML login page instead of JSON.

        Enlighten sometimes responds to an expired/invalid session with a 200 status and an
        HTML login page body rather than a 401, so this must be checked independently of status.
        """
        if json_data is not None:
            return False
        stripped = (text or "").lstrip().lower()
        return stripped.startswith("<!doctype") or stripped.startswith("<html")

    async def request_json(self, method, path, family="site", json_body=None, data=None, params=None, allow_empty=False, context=None):
        """Perform an authenticated JSON request with retries and a single 401 re-login.

        Builds the request URL from BASE_URL + path and attaches family-appropriate headers
        via get_headers(). Handles failure modes as follows:
        - A 401/403 status, or an HTML login-wall body on what should be a JSON endpoint, is
          treated as an auth failure: for the "battery_config" family this first tries the
          "cookie_eauth" header variant fallback (without consuming the single re-login
          attempt); otherwise it performs one login() and retries once.
        - HTTP 429 and 5xx responses, plus timeouts/connection errors, are retried with
          jittered backoff up to ENPHASE_RETRIES times.
        - Any other non-200 status is treated as a terminal failure, logged as
          "HTTP {method} {path} -> {status}" (plus ``context`` when the caller gave one, e.g. the
          schedule family/window being written) so a bare-path 409 is never ambiguous between the
          GET, the POST create and the PUT update that all share that path shape.
        ``allow_empty`` additionally accepts a 204/empty body as success (returning {} rather than
        None), as a DELETE returns no content.
        Every outcome is recorded via record_api_call("enphase", ...) for metrics/health.
        Returns the parsed JSON body on success, or None on failure (self.last_error_status is
        set to the last HTTP status seen, where available).
        """
        url = BASE_URL + path
        relogin_done = False
        self.last_error_status = None
        for retry in range(ENPHASE_RETRIES):
            headers = self.get_headers(family, write=(method != "GET"))
            try:
                status, json_data, text, cookies = await self.request_raw(method, url, headers=headers, data=data, json_body=json_body, params=params)
            except (asyncio.TimeoutError, aiohttp.ClientError) as error:
                self.log(f"Warn: Enphase: Request error on {path}: {error}")
                record_api_call("enphase", False, "connection_error")
                await asyncio.sleep(1 + retry * random.random() * 5)
                continue

            self.requests_today += 1
            self._log_api_call(method, path, params, status, json_data, text)
            auth_failed = status in (401, 403) or self._is_login_wall(json_data, text)
            if auth_failed:
                record_api_call("enphase", False, "auth_error")
                if family == "battery_config" and self.battery_config_variant == "primary":
                    # Some regions/firmware reject the primary BatteryConfig header shape;
                    # switch to the cookie-backed fallback variant before burning a re-login.
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
                record_api_call("enphase", False, "rate_limit" if status == 429 else "server_error")
                await asyncio.sleep(min(30, (retry + 1) * (2 + random.random() * 3)))
                continue

            if status != 200 and not (allow_empty and status == 204):
                context_suffix = f" ({context})" if context else ""
                self.log(f"Warn: Enphase: HTTP {method} {path} -> {status}{context_suffix}")
                record_api_call("enphase", False, "client_error")
                self.last_error_status = status
                self.failures_total += 1
                return None

            # Absorb cookies from a genuine success only: this keeps the session cookie current
            # and captures the fresh XSRF token (into both self.cookie_header and self.xsrf_token),
            # which BatteryConfig writes require as a double-submit (XSRF-TOKEN cookie + X-XSRF-Token
            # header). It is deliberately NOT done for login-wall/error responses (handled above),
            # whose anonymous cookies would otherwise corrupt our authenticated session.
            self._absorb_cookies(cookies)

            record_api_call("enphase", True)
            self.update_success_timestamp()
            # A DELETE succeeds with 204/no body; callers passing allow_empty want success, not None.
            if json_data is None and allow_empty:
                return {}
            return json_data

        self.failures_total += 1
        return None


async def test_enphase_api(username, password, site_id):  # pragma: no cover
    """Log in and run one poll cycle, printing the discovered sites and read data."""
    mock_base = MockBase()
    api = EnphaseAPI(mock_base, username=username, password=password, site_id=site_id, automatic=True)

    print("Calling run() once (login, reads, publish, automatic_config)...")
    result = await api.run(seconds=0, first=True)
    print(f"run() returned: {result}")
    print(f"Discovered sites: {api.sites}")

    for site in api.sites:
        sid = site["site_id"]
        print(f"\n--- Site {sid} ({site.get('name', '')}) ---")
        print(f"battery_status: {json.dumps(api.battery_status.get(sid, {}), default=str, indent=2)}")
        print(f"profile: {api.profile.get(sid)}")
        print(f"battery_settings: {api.battery_settings.get(sid)}")
        print(f"schedules: {json.dumps(api.schedules.get(sid, {}), default=str, indent=2)}")
        print(f"dtg_supported: {api.dtg_supported(sid)}")

        # Dump the normalised today data: per-channel totals (Wh) and 15-minute bucket metadata,
        # so the published *_today (kWh) and instantaneous power values can be sanity-checked.
        today = api.today.get(sid, {})
        print(f"today totals (Wh): {today.get('totals')}")
        print(f"today interval_length={today.get('interval_length')} start_time={today.get('start_time')}")
        for channel, values in (today.get("arrays") or {}).items():
            if values:
                print(f"  {channel}: len={len(values)} last5={values[-5:]}")

        # Livestream: prove the connect -> read one message -> disconnect cycle works against the
        # real account, and cross-check it against the bucket-derived values it replaces.
        print(f"\ngateway serial: {gateway_serial(today)}")
        print(f"protobuf available: {HAS_LIVESTREAM_PROTOBUF}   aiomqtt available: {HAS_AIOMQTT}")
        for attempt in range(1, 4):
            started = datetime.now(timezone.utc)
            reading = await api.get_live_power(sid)
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            if not reading:
                print(f"  livestream attempt {attempt}: FAILED after {elapsed:.1f}s")
                continue
            balance = reading["pv"] + reading["grid"] + reading["battery"] - reading["load"]
            print(f"  livestream attempt {attempt} ({elapsed:.1f}s): pv={reading['pv']}W grid={reading['grid']}W battery={reading['battery']}W load={reading['load']}W soc={reading['soc']}%")
            print(f"    energy balance (pv+grid+battery-load) = {balance:.1f} W  <- expect ~0")
        arrays = today.get("arrays") or {}
        now_ts = datetime.now(timezone.utc).timestamp()
        bucket = {channel: interval_power(arrays.get(channel, []), today.get("start_time"), today.get("interval_length"), now_ts) for channel in ("production", "import", "export", "charge", "discharge")}
        print(f"  bucket-derived for comparison: pv={bucket['production']}W grid={bucket['import'] - bucket['export']}W battery={bucket['discharge'] - bucket['charge']}W")
        print("    (buckets are a 15-minute average and lag; large differences are expected)")

    print("\nDone")


async def test_write_schedule(username, password, site_id, start_time, end_time, soc):  # pragma: no cover
    """Write a test charge-from-grid window via the control entities, read it back, then disable it.

    Drives the same path Predbat uses: it sets the published control entities to the requested
    window, calls apply_battery_schedule (which reads those entities and writes an Enphase CFG
    schedule), reads the schedules back, then restores by disabling the charge window again.
    """
    mock_base = MockBase()
    api = EnphaseAPI(mock_base, username=username, password=password, site_id=site_id, automatic=False)

    print("Logging in and loading current state...")
    await api.run(seconds=0, first=True)
    if not api.sites:
        print("No sites found")
        return
    sid = api.sites[0]["site_id"]
    print(f"Existing schedules for {sid}:\n{json.dumps(api.schedules.get(sid, {}), default=str, indent=2)}")

    base_name = f"{api.prefix}_enphase_{sid}_battery_schedule"

    # Set the charge control entities to the requested window and enable it
    mock_base.set_state_wrapper(f"select.{base_name}_charge_start_time", start_time)
    mock_base.set_state_wrapper(f"select.{base_name}_charge_end_time", end_time)
    mock_base.set_state_wrapper(f"number.{base_name}_charge_soc", soc)
    mock_base.set_state_wrapper(f"switch.{base_name}_charge_enable", "on")
    print(f"Applying test CFG charge window {start_time}-{end_time} @ {soc}%...")
    await api.apply_battery_schedule(sid)
    await api.get_schedules(sid)
    print(f"Read-back schedules after write:\n{json.dumps(api.schedules.get(sid, {}), default=str, indent=2)}")

    # Restore: disable the test charge window again
    print("Restoring: disabling the test charge window...")
    mock_base.set_state_wrapper(f"switch.{base_name}_charge_enable", "off")
    await api.apply_battery_schedule(sid)
    print("Done")


async def test_write_reserve(username, password, site_id, value):  # pragma: no cover
    """Write the battery reserve to a test value, read it back, then restore the original.

    A minimal, safe real-write test: it changes only the reserve (batteryBackupPercentage),
    prints the before/after values, and always puts the original value back so the customer's
    system is left as it was.
    """
    mock_base = MockBase()
    api = EnphaseAPI(mock_base, username=username, password=password, site_id=site_id, automatic=False)

    print("Logging in and loading current state...")
    await api.run(seconds=0, first=True)
    if not api.sites:
        print("No sites found")
        return
    sid = api.sites[0]["site_id"]

    original = api.profile.get(sid, {}).get("reserve")
    print(f"Current reserve on site {sid}: {original}%")
    if original is None:
        print("Could not read current reserve - aborting without writing")
        return

    try:
        print(f"Writing test reserve {value}%...")
        await api.set_reserve(sid, value)
        await api.get_profile(sid)
        print(f"Read-back reserve after write: {api.profile.get(sid, {}).get('reserve')}% (note: may lag by minutes)")
    finally:
        print(f"Restoring original reserve {original}%...")
        await api.set_reserve(sid, original)
        await api.get_profile(sid)
        print(f"Read-back reserve after restore: {api.profile.get(sid, {}).get('reserve')}%")
    print("Done")


async def test_schedule_reconcile(username, password, site_id):  # pragma: no cover
    """Drive a scripted sequence of schedule changes against a real account.

    Exercises apply_battery_schedule's reconcile path end-to-end, including the cross-family case
    that motivated the "Update strategy" rule in
    docs/superpowers/specs/2026-08-08-enphase-schedule-reconcile-design.md: one family's window
    moving so that its *old* window overlaps a *different* family's brand new one, even though
    neither family's *new* windows overlap each other. Every window used is between midnight and
    05:00, and the export-family steps use RBD (freeze-export/restrict-discharge, soc=99) rather
    than a real DTG target, so nothing here can actually charge from or export to the grid - it
    only ever writes schedules a real overnight off-peak window would already contain. The final
    step disables everything again, leaving the account in a known, inert state.
    """
    mock_base = MockBase()
    api = EnphaseAPI(mock_base, username=username, password=password, site_id=site_id, automatic=False)

    print("Logging in and loading current state...")
    await api.run(seconds=0, first=True)
    if not api.sites:
        print("No sites found")
        return
    sid = api.sites[0]["site_id"]
    base_name = f"{api.prefix}_enphase_{sid}_battery_schedule"

    def _set_window(direction, start, end, soc, enabled):
        """Push one direction's control entities into the mock HA state, as Predbat's UI would."""
        mock_base.set_state_wrapper(f"select.{base_name}_{direction}_start_time", start)
        mock_base.set_state_wrapper(f"select.{base_name}_{direction}_end_time", end)
        mock_base.set_state_wrapper(f"number.{base_name}_{direction}_soc", soc)
        mock_base.set_state_wrapper(f"switch.{base_name}_{direction}_enable", "on" if enabled else "off")

    steps = [
        ("enable charge alone (create)", {"charge": ("00:30:00", "01:00:00", 50, True)}),
        ("move charge alone (PUT-in-place)", {"charge": ("01:00:00", "01:30:00", 50, True)}),
        ("move charge + enable freeze-export together (crosses charge's OLD window)", {"charge": ("02:00:00", "02:30:00", 50, True), "export": ("01:15:00", "01:45:00", 99, True)}),
        ("move both again, crossing back the other way", {"charge": ("01:30:00", "02:00:00", 50, True), "export": ("02:15:00", "02:45:00", 99, True)}),
        ("disable everything", {"charge": ("00:00:00", "00:00:00", 50, False), "export": ("00:00:00", "00:00:00", 99, False)}),
    ]

    for label, changes in steps:
        print(f"\n--- {label} ---")
        for direction, (start, end, soc, enabled) in changes.items():
            _set_window(direction, start, end, soc, enabled)
        ok = await api.apply_battery_schedule(sid)
        await api.get_schedules(sid)
        print(f"apply_battery_schedule -> {ok}")
        print(f"schedules: {json.dumps(api.schedules.get(sid, {}), default=str, indent=2)}")
        if api.schedule_write_failed.get(sid):
            print(f"WRITE FAILURES: {api.schedule_write_failed[sid]}")

    print("\nDone")


def main():  # pragma: no cover
    """Command-line entry point for exercising the Enphase component standalone."""
    import argparse

    parser = argparse.ArgumentParser(description="Test the Enphase Enlighten cloud component")
    parser.add_argument("--username", required=True, help="Enlighten account e-mail")
    parser.add_argument("--password", required=True, help="Enlighten account password")
    parser.add_argument("--site-id", default=None, help="Restrict to a single Enphase site id")
    parser.add_argument("--write-schedule", action="store_true", help="Write a test charge window and read it back instead of a read-only run")
    parser.add_argument("--start-time", default="02:00:00", help="Test charge window start (HH:MM:SS)")
    parser.add_argument("--end-time", default="05:00:00", help="Test charge window end (HH:MM:SS)")
    parser.add_argument("--soc", type=int, default=80, help="Test charge target SOC percent")
    parser.add_argument("--write-reserve", type=int, default=None, help="Write this reserve %% (e.g. 25), then restore the original - a safe real-write test")
    parser.add_argument("--reconcile-sequence", action="store_true", help="Drive a scripted sequence of schedule changes (midnight-05:00 only) to exercise the reconcile path live, then disable everything again")

    args = parser.parse_args()

    if args.write_reserve is not None:
        asyncio.run(test_write_reserve(args.username, args.password, args.site_id, args.write_reserve))
    elif args.write_schedule:
        asyncio.run(test_write_schedule(args.username, args.password, args.site_id, args.start_time, args.end_time, args.soc))
    elif args.reconcile_sequence:
        asyncio.run(test_schedule_reconcile(args.username, args.password, args.site_id))
    else:
        asyncio.run(test_enphase_api(args.username, args.password, args.site_id))


if __name__ == "__main__":
    main()

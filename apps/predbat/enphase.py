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
import uuid

import aiohttp

from component_base import ComponentBase
from predbat_metrics import record_api_call

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
    """Estimate current watts from the most recent completed intra-day energy bucket.

    `values` is the /today array of per-interval energy in Wh (each bucket covers `interval_length`
    seconds starting at `start_time`, a Unix timestamp at local midnight). The bucket index for the
    current time is (now - start_time) / interval_length; the last COMPLETED bucket is one before
    that. Its energy divided by the bucket duration in hours gives average watts over that bucket -
    a stable, correct instantaneous estimate that needs no cross-poll delta tracking. Returns 0.0
    when the data is missing/empty or the timing is unusable.
    """
    if not values or not interval_length or start_time is None or now_ts is None:
        return 0.0
    hours = interval_length / 3600.0
    if hours <= 0:
        return 0.0
    index = int((now_ts - start_time) / interval_length) - 1
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
            self.sync_local_schedule_from_cloud(site_id)
            await self.publish_data(site_id)
            await self.publish_schedule_settings_ha(site_id)

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
        power = self.latest_power.get(site_id, {})

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

        self.dashboard_item(
            f"{entity_base}_load_power",
            state=power.get("watts"),
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase Load Power", "icon": "mdi:home-lightning-bolt"},
            app="enphase",
        )

        # Instantaneous power from the most recent completed intra-day 15-minute energy bucket of
        # the /today arrays (Wh per interval -> average watts over that interval). This reads a
        # single bucket value per poll, so it is inherently stable within an interval and needs no
        # cross-poll delta tracking.
        arrays = today.get("arrays", {})
        start_time = today.get("start_time")
        interval_length = today.get("interval_length")
        now_ts = now_utc.timestamp()
        channel_watts = {channel: interval_power(arrays.get(channel, []), start_time, interval_length, now_ts) for channel in ("production", "import", "export", "charge", "discharge")}

        pv_power = channel_watts.get("production", 0.0)
        grid_power = channel_watts.get("import", 0.0) - channel_watts.get("export", 0.0)
        battery_power = channel_watts.get("discharge", 0.0) - channel_watts.get("charge", 0.0)

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

    async def publish_schedule_settings_ha(self, site_id):
        """Publish the schedule control entities for a site.

        Publishes the reserve control plus both the charge-from-grid and export (discharge-to-grid)
        window controls (a configured inverter always supports both - automatic_config requires
        DTG). There is no separate freeze control: Predbat freezes charge via the reserve, and
        freeze-export is derived automatically from an export target SOC of 99%.
        """
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

        Turning on a "_write" switch triggers `apply_battery_schedule(site_id)`, which
        diffs the local schedule model against the cloud and issues only the changed writes.
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

    async def _write_schedule(self, site_id, family, start_time_ha, end_time_ha, limit, enabled):
        """Create/update one Enphase schedule family if it differs from our cached cloud state.

        Converts the HA "HH:MM:SS" option times to Enphase "HH:MM" format, then compares against
        the cached cloud schedule via `schedules_equal()`. A matching schedule is a no-op. Otherwise
        it updates the existing schedule by id (`PUT`) or creates a new one (`POST`). On success it
        optimistically updates our cached copy to the written state and returns - the periodic
        settings re-read will correct it if the write did not actually land. A create additionally
        re-reads the schedules once, to capture the cloud-assigned scheduleId (so later edits update
        in place rather than creating duplicates - the write responses do not return the id).
        Returns True if a write was issued.
        """
        start_hm = ha_time_to_enphase(start_time_ha)
        end_hm = ha_time_to_enphase(end_time_ha)
        family_key = family.lower()
        cloud_entry = self.schedules.get(site_id, {}).get(family_key, {})
        if schedules_equal(cloud_entry, start_hm, end_hm, limit, enabled):
            return False  # already matches our cached state - nothing to do

        payload = {"timezone": self._site_timezone(site_id), "startTime": start_hm, "endTime": end_hm, "scheduleType": family, "days": [1, 2, 3, 4, 5, 6, 7], "isEnabled": bool(enabled)}
        if limit is not None:
            payload["limit"] = int(limit)
        schedule_id = cloud_entry.get("id")
        if schedule_id:
            self.log(f"Enphase: Updating {family} schedule {schedule_id} on site {site_id}: {start_hm}-{end_hm} limit={limit} enabled={enabled}")
            result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules/{schedule_id}", family="battery_config", json_body=payload)
            if result is not None:
                # Optimistically cache the new state, preserving the id and other fields.
                updated = dict(cloud_entry)
                updated.update({"startTime": start_hm, "endTime": end_hm, "limit": limit, "enabled": bool(enabled)})
                self.schedules.setdefault(site_id, {})[family_key] = updated
        else:
            self.log(f"Enphase: Creating {family} schedule on site {site_id}: {start_hm}-{end_hm} limit={limit} enabled={enabled}")
            result = await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules", family="battery_config", json_body=payload)
            if result is not None:
                # Re-read once so we learn the new schedule's cloud-assigned id for future edits.
                await self.get_schedules(site_id)
        return result is not None

    def _is_schedule_pending(self, site_id, family):
        """Return True when the cached cloud schedule exists but is stuck in pending status."""
        family_key = family.lower()
        entry = self.schedules.get(site_id, {}).get(family_key, {})
        return isinstance(entry, dict) and entry.get("status", "").lower() == "pending"

    async def _set_charge_from_grid(self, site_id, params=None, **extra_body):
        """PUT batterySettings chargeFromGrid:True and cache the result on success.

        Accepts optional extra body keys (e.g. acceptedItcDisclaimer) and optional
        query params (e.g. source/userId for activation). chargeFromGrid is always
        forced to True regardless of extra_body. Returns True if the API call succeeded.
        """
        body = dict(extra_body)
        body["chargeFromGrid"] = True
        result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", params=params, json_body=body)
        if result is not None:
            self.battery_settings.setdefault(site_id, {})["chargeFromGrid"] = True
        return result is not None

    def _invalidate_cached_schedule(self, site_id, family):
        """Clear the cached cloud schedule so the next apply detects a diff and retries.

        Sets startTime to an empty string (rather than removing the key) so that
        schedules_equal returns ``not enabled`` (False when enabled=True), causing
        _write_schedule to re-issue the update on the next apply. Removing the key
        would also return ``not enabled``, which incorrectly no-ops a disable
        request after an activation failure (False for enabled=False means True —
        equal, so no write is issued).
        """
        family_key = family.lower()
        entry = self.schedules.get(site_id, {}).get(family_key)
        if isinstance(entry, dict):
            entry["startTime"] = ""

    async def _activate_cfg_mode(self, site_id, family=SCHEDULE_CHARGE):
        """Activate charge-from-grid mode after writing the CFG schedule.

        The CFG schedule write puts the schedule into "pending" status;
        a subsequent batterySettings PUT with acceptedItcDisclaimer
        is required to transition it to "active" so the Enphase gateway
        picks it up and starts charging.

        On failure, clears the cached schedule startTime so the next
        apply re-detects a diff and retries write + activation.

        Returns True if the activation call succeeded.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        params = {"source": "enho"}
        if self.user_id:
            params["userId"] = self.user_id
        ok = await self._set_charge_from_grid(site_id, params=params, acceptedItcDisclaimer=now_iso)
        if not ok:
            self.log(f"Warn: Enphase: CFG activation failed for site {site_id}")
            self._invalidate_cached_schedule(site_id, family)
        return ok

    async def _activate_dtg_mode(self, site_id, family=SCHEDULE_EXPORT):
        """Activate discharge-to-grid mode after writing the DTG schedule.

        The DTG schedule write puts the schedule into "pending" status;
        a subsequent batterySettings PUT with dtgControl.enabled is
        required to transition it to "active" so the Enphase gateway
        picks it up.

        On failure, clears the cached schedule startTime so the next
        apply re-detects a diff and retries write + activation.

        Returns True if the activation call succeeded.
        """
        params = {"source": "enho"}
        if self.user_id:
            params["userId"] = self.user_id
        body = {"dtgControl": {"enabled": True}}
        result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", params=params, json_body=body)
        if result is not None:
            self.battery_settings.setdefault(site_id, {}).setdefault("dtgControl", {})["enabled"] = True
        else:
            self.log(f"Warn: Enphase: DTG activation failed for site {site_id}")
            self._invalidate_cached_schedule(site_id, family)
        return result is not None

    async def _activate_rbd_mode(self, site_id, family=SCHEDULE_FREEZE):
        """Activate restrict-battery-discharge mode after writing the RBD schedule.

        The RBD schedule write puts the schedule into "pending" status;
        a subsequent batterySettings PUT with rbdControl.enabled is
        required to transition it to "active" so the Enphase gateway
        picks it up.

        On failure, clears the cached schedule startTime so the next
        apply re-detects a diff and retries write + activation.

        Returns True if the activation call succeeded.
        """
        params = {"source": "enho"}
        if self.user_id:
            params["userId"] = self.user_id
        body = {"rbdControl": {"enabled": True}}
        result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", params=params, json_body=body)
        if result is not None:
            self.battery_settings.setdefault(site_id, {}).setdefault("rbdControl", {})["enabled"] = True
        else:
            self.log(f"Warn: Enphase: RBD activation failed for site {site_id}")
            self._invalidate_cached_schedule(site_id, family)
        return result is not None

    async def _ensure_charge_from_grid(self, site_id):
        """Enable the charge-from-grid setting, accepting the one-time ITC disclaimer first."""
        if self.battery_settings.get(site_id, {}).get("chargeFromGrid"):
            return
        self.log(f"Enphase: Enabling charge-from-grid on site {site_id}")
        await self.request_json("POST", f"{BATTERY_CONFIG_BASE}/batterySettings/acceptDisclaimer/{site_id}", family="battery_config", json_body={"disclaimer-type": "itc"})
        await self._set_charge_from_grid(site_id)

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
        result = await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/profile/{site_id}", family="battery_config", params=params, json_body={"profile": profile_name, "batteryBackupPercentage": int(reserve)})
        if result is not None:
            # Optimistically cache the written reserve; the periodic profile re-read will correct
            # it if the write did not actually land (e.g. the gateway never activated it).
            self.profile.setdefault(site_id, {})["reserve"] = int(reserve)
        return result

    async def apply_battery_schedule(self, site_id):
        """Diff the local schedule model against the cloud and issue only the changed writes.

        Reads the latest control-entity state into `local_schedule` first, then writes (only
        where the desired state differs from the cached cloud state):
        1. Reserve, via a profile PUT that preserves the current profile name.
        2. Forced charge-from-grid window (schedule family "CFG"), enabling the
           `chargeFromGrid` battery setting first if required.
        3. The export window, derived from Predbat's export/discharge target SOC:
           - target below 99% -> a real forced export to that floor (schedule family "DTG"),
             only on sites that support it;
           - target of exactly 99% -> "freeze export" (hold, don't discharge), mapped to the
             restrict-battery-discharge family ("RBD") over the export window;
           - target of 100% -> disabled (same as no export).
        Each write optimistically updates our cached cloud copy on success and moves on; the
        periodic settings re-read reconciles it later if a write did not actually land. Returns
        True if any write was issued.

        Freeze *charge* is not handled here - Predbat freezes charge via the reserve (raising it
        to the current SOC) and disabling the charge window, using the existing reserve/charge
        controls.
        """
        await self.get_schedule_settings_ha(site_id)
        # Bootstrap a fresh XSRF token before writing (the web app GETs siteSettings first); its
        # x-csrf-token response header is absorbed by request_json for the writes below.
        await self.get_site_settings(site_id)
        local = self.local_schedule.get(site_id, self._default_local_schedule())
        wrote = False

        # Reserve via profile PUT, preserving the current profile name
        desired_reserve = int(local.get("reserve", 0))
        cloud = self.profile.get(site_id, {})
        if desired_reserve and desired_reserve != int(cloud.get("reserve", -1)):
            await self.set_reserve(site_id, desired_reserve)
            wrote = True

        # Forced charge window (CFG)
        charge = local.get("charge", {})
        if charge.get("enable"):
            await self._ensure_charge_from_grid(site_id)
        wrote_cfg = await self._write_schedule(site_id, SCHEDULE_CHARGE, charge.get("start_time", "00:00:00"), charge.get("end_time", "00:00:00"), charge.get("soc", 100), charge.get("enable", False))
        if charge.get("enable"):
            if wrote_cfg:
                await self._activate_cfg_mode(site_id)
            elif self._is_schedule_pending(site_id, SCHEDULE_CHARGE):
                # Schedule matches but is stuck pending — activate without rewriting
                self.log(f"Enphase: CFG schedule for site {site_id} is pending; activating without rewrite")
                await self._activate_cfg_mode(site_id)
        wrote |= wrote_cfg

        # Export window. Predbat encodes the mode in the export/discharge target SOC:
        #   < 99  -> real forced export to that floor (DTG)
        #   == 99 -> freeze export / restrict discharge (RBD)
        #   == 100 (or disabled) -> no export
        export = local.get("export", {})
        export_enabled = bool(export.get("enable", False))
        export_soc = int(export.get("soc", 5))
        export_start = export.get("start_time", "00:00:00")
        export_end = export.get("end_time", "00:00:00")
        real_export = export_enabled and export_soc < 99
        freeze_export = export_enabled and export_soc == 99

        # Clamp the DTG floor to at least the reserve: Enphase will not discharge below the backup
        # reserve, and Predbat's own discharge target is max(export, reserve), so keep the written
        # limit consistent rather than requesting an export below the reserve it can never reach.
        dtg_limit = max(export_soc, int(local.get("reserve", 0)))

        # Forced export to a target (DTG). A configured inverter always supports DTG.
        wrote_dtg = await self._write_schedule(site_id, SCHEDULE_EXPORT, export_start, export_end, dtg_limit, real_export)
        if real_export:
            if wrote_dtg:
                await self._activate_dtg_mode(site_id)
            elif self._is_schedule_pending(site_id, SCHEDULE_EXPORT):
                self.log(f"Enphase: DTG schedule for site {site_id} is pending; activating without rewrite")
                await self._activate_dtg_mode(site_id)
        wrote |= wrote_dtg

        # Freeze export = restrict battery discharge (RBD) over the export window
        wrote_rbd = await self._write_schedule(site_id, SCHEDULE_FREEZE, export_start, export_end, None, freeze_export)
        if freeze_export:
            if wrote_rbd:
                await self._activate_rbd_mode(site_id)
            elif self._is_schedule_pending(site_id, SCHEDULE_FREEZE):
                self.log(f"Enphase: RBD schedule for site {site_id} is pending; activating without rewrite")
                await self._activate_rbd_mode(site_id)
        wrote |= wrote_rbd
        return wrote

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
            "site_status": data.get("siteStatus"),
            "status_severity": status_details.get("statusSeverity"),
            "status_desc": status_details.get("statusDesc"),
            "last_report_date": data.get("last_report_date"),
        }
        await self._save_cache("today", self.today)
        return self.today[site_id]

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
        ``isEnabled`` and ``isDeleted``. Only the first schedule per family is used (Predbat drives
        one window per direction); the ``scheduleId`` is what the write path updates in place.
        """
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules", family="battery_config")
        if data is None:
            return None
        parsed = {}
        for family_key in ("cfg", "dtg", "rbd"):
            family_data = data.get(family_key) or {}
            details = family_data.get("details") or []
            # Prefer the first non-deleted schedule
            entry = next((item for item in details if isinstance(item, dict) and not item.get("isDeleted")), details[0] if details else {})
            # "supported" gates whether Predbat can use this schedule family. Real accounts report
            # a per-family scheduleStatus ("active" seen so far); treat the usable statuses as
            # supported, with a fallback to the (unverified) boolean flags.
            status_text = str(family_data.get("scheduleStatus", "")).strip().lower()
            supported = status_text in ("active", "enabled", "supported", "available") or bool(family_data.get("scheduleSupported") or family_data.get("forceScheduleSupported"))
            parsed[family_key] = {
                "id": entry.get("scheduleId") or entry.get("id"),
                "startTime": entry.get("startTime"),
                "endTime": entry.get("endTime"),
                "limit": safe_int(entry.get("limit"), None),
                "enabled": bool(entry.get("isEnabled", False)),
                "supported": supported,
                "count": safe_int(family_data.get("count"), 0),
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
            for key in ("token", "auth_token", "access_token"):
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

    async def request_json(self, method, path, family="site", json_body=None, data=None, params=None):
        """Perform an authenticated JSON request with retries and a single 401 re-login.

        Builds the request URL from BASE_URL + path and attaches family-appropriate headers
        via get_headers(). Handles failure modes as follows:
        - A 401/403 status, or an HTML login-wall body on what should be a JSON endpoint, is
          treated as an auth failure: for the "battery_config" family this first tries the
          "cookie_eauth" header variant fallback (without consuming the single re-login
          attempt); otherwise it performs one login() and retries once.
        - HTTP 429 and 5xx responses, plus timeouts/connection errors, are retried with
          jittered backoff up to ENPHASE_RETRIES times.
        - Any other non-200 status is treated as a terminal failure.
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

            if status != 200:
                self.log(f"Warn: Enphase: HTTP {status} on {path}")
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
            return json_data

        self.failures_total += 1
        return None


class MockBase:  # pragma: no cover
    """Minimal stand-in for the Predbat base object so EnphaseAPI can run standalone.

    Provides just the attributes and methods ComponentBase and EnphaseAPI read
    from ``self.base`` (prefix, args, timezone/clock, state/arg accessors and a
    logger). It deliberately has no ``components`` attribute, so ``self.storage``
    resolves to None and the disk cache is skipped for a standalone run.
    """

    def __init__(self):
        """Initialise the mock base with the current clock and empty state stores."""
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = self.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.fatal_error = False
        self.had_errors = False
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return the stored state (or full record when raw) for an entity id."""
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Store an entity's state and attributes in memory."""
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        """Print a timestamped log line to stdout."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print and store a published entity, so a standalone run shows what it publishes."""
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if "options" in attributes:
                attributes["options"] = "..."
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return the default for any requested arg (no real config in standalone mode)."""
        return default

    def set_arg(self, key, value):
        """Print an arg that automatic_config would set, resolving any referenced entity state."""
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

    args = parser.parse_args()

    if args.write_reserve is not None:
        asyncio.run(test_write_reserve(args.username, args.password, args.site_id, args.write_reserve))
    elif args.write_schedule:
        asyncio.run(test_write_schedule(args.username, args.password, args.site_id, args.start_time, args.end_time, args.soc))
    else:
        asyncio.run(test_enphase_api(args.username, args.password, args.site_id))


if __name__ == "__main__":
    main()

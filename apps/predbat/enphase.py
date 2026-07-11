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
This module currently provides the component skeleton: initialisation,
cache load/save helpers and staleness checks. Login, data reads, sensor
publishing and battery control are added by later tasks.
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

# How long a schedule/profile write is kept as "pending" awaiting cloud confirmation
# before it is dropped and eligible to be re-attempted.
ENPHASE_PENDING_TIMEOUT_MINUTES = 15

# Browser mimicry - Enlighten rejects non-browser requests with 406/login walls
ENPHASE_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
BATTERY_UI_ORIGIN = "https://battery-profile-ui.enphaseenergy.com"


def safe_float(value, default=0.0):
    """Convert a value to float, returning default for None or non-numeric values.

    The Enphase cloud returns the string "N/A" (and occasionally blank strings) for fields it
    cannot report, so a bare float() would raise; this coerces those to the supplied default.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    """Convert a value to int, returning default for None or non-numeric values (e.g. Enphase 'N/A')."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def energy_today(payload, channel):
    """Return today's kWh for a lifetime_energy channel, 0.0 if absent.

    The lifetime_energy endpoint returns per-day energy increments expressed in Wh: the array is
    indexed daily from start_date, so the last element is today's running total (in Wh). Predbat
    works in kWh, so the raw value is divided by 1000. Note: this assumes the daily cadence seen on
    real accounts; a site configured for sub-daily (15-minute) buckets would need summing today's
    buckets instead - see the /pv/systems/<site>/today endpoint for a cadence-independent total.
    """
    values = (payload or {}).get(channel) or []
    if not values:
        return 0.0
    return round(safe_float(values[-1]) / 1000.0, 3)


def derive_power(prev_sample, new_kwh, now_utc):
    """Estimate average watts from the change in a cumulative kWh counter since the previous sample.

    A sample is a `(kwh, datetime)` tuple. Returns `(watts, new_sample)`. The baseline sample is
    only advanced (`new_sample != prev_sample`) when the cumulative kWh value has actually
    changed, so a caller that polls far more often than the underlying counter refreshes (e.g.
    every 60 seconds against a value that only updates every 15 minutes) can tell "no change yet"
    apart from "just changed" and compute watts over the true elapsed time since the last change,
    rather than diluting a full-window delta by a much shorter tick interval. Watts is clamped to
    zero, with the previous sample kept unchanged (caller should hold its last computed watts),
    when there is no previous sample, when the kWh value is unchanged, or when the window since
    the previous sample is under 60 seconds. When the counter has gone backwards (e.g. a daily
    reset at midnight) watts is clamped to zero and the baseline is re-seeded to the new value.
    """
    new_sample = (new_kwh, now_utc)
    if not prev_sample:
        return 0.0, new_sample
    prev_kwh, prev_time = prev_sample
    delta = new_kwh - prev_kwh
    if delta == 0:
        return 0.0, prev_sample
    if delta < 0:
        return 0.0, new_sample
    seconds = (now_utc - prev_time).total_seconds()
    if seconds < 60:
        return 0.0, prev_sample
    return round(delta * 1000.0 * 3600.0 / seconds, 1), new_sample


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
        self.lifetime_energy = {}
        self.latest_power = {}

        # Local (HA-side) schedule model, written by events, applied on write switch
        self.local_schedule = {}

        # Derived power state: previous cumulative kWh samples per channel, and the last
        # computed watts per channel (republished when the kWh value hasn't changed yet)
        self.prev_energy_sample = {}
        self.last_power_w = {}

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

        # Drop any schedule/profile writes that never got confirmed by the cloud within
        # the settle timeout, so a future apply_battery_schedule() can retry them.
        self.clear_expired_pending_writes()

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
            if self._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS):
                await self.get_battery_status(site_id)
                await self.get_profile(site_id)
                await self.get_battery_settings(site_id)
                await self.get_schedules(site_id)
            if self._needs_refresh("lifetime_energy", ENPHASE_REFRESH_ENERGY):
                await self.get_lifetime_energy(site_id)
            if self._needs_refresh("latest_power", ENPHASE_REFRESH_POWER):
                await self.get_latest_power(site_id)
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
        category (e.g. no lifetime_energy fetched yet) cannot crash the whole publish. The
        battery profile name is read from `self.profile`, not `self.battery_status` (the latter
        has no profile field - see the note in `get_battery_status`).
        """
        entity_base = f"sensor.{self.prefix}_enphase_{site_id}"
        now_utc = datetime.now(timezone.utc)

        status = self.battery_status.get(site_id, {})
        profile = self.profile.get(site_id, {})
        settings = self.battery_settings.get(site_id, {})
        energy = self.lifetime_energy.get(site_id, {})
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

        # Today's cumulative energy totals, one dashboard sensor per lifetime_energy channel.
        energy_channels = {
            "production": ("pv_today", "Enphase PV Today", "mdi:solar-power"),
            "consumption": ("load_today", "Enphase Load Today", "mdi:home-lightning-bolt"),
            "import": ("import_today", "Enphase Import Today", "mdi:transmission-tower-import"),
            "export": ("export_today", "Enphase Export Today", "mdi:transmission-tower-export"),
            "charge": ("battery_charge_today", "Enphase Battery Charge Today", "mdi:battery-plus"),
            "discharge": ("battery_discharge_today", "Enphase Battery Discharge Today", "mdi:battery-minus"),
        }
        energy_values = {}
        for channel, (name, friendly, icon) in energy_channels.items():
            value = energy_today(energy, channel)
            energy_values[channel] = value
            self.dashboard_item(
                f"{entity_base}_{name}",
                state=value,
                attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing", "friendly_name": friendly, "icon": icon},
                app="enphase",
            )

        self.dashboard_item(
            f"{entity_base}_load_power",
            state=power.get("watts"),
            attributes={"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "friendly_name": "Enphase Load Power", "icon": "mdi:home-lightning-bolt"},
            app="enphase",
        )

        # Derived instantaneous power from cumulative kWh deltas, kept per site/channel. A
        # channel's baseline sample only advances when its cumulative kWh has actually changed
        # (see derive_power); when it hasn't (or the window since the last change is under 60s)
        # the previously computed watts value is republished rather than recomputed or zeroed,
        # which avoids the ~15x inflation that would otherwise occur when lifetime_energy (only
        # refreshed every 15 minutes) is read on a component polled every 60 seconds.
        samples = self.prev_energy_sample.setdefault(site_id, {})
        channel_watts = self.last_power_w.setdefault(site_id, {})
        for channel in ("production", "import", "export", "charge", "discharge"):
            prev_sample = samples.get(channel)
            watts, new_sample = derive_power(prev_sample, energy_values[channel], now_utc)
            if new_sample == prev_sample:
                watts = channel_watts.get(channel, 0.0)
            else:
                channel_watts[channel] = watts
            samples[channel] = new_sample

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
            "freeze": {"enable": False},
        }

    async def publish_schedule_settings_ha(self, site_id):
        """Publish the schedule control entities for a site.

        Always publishes the reserve control plus the charge-from-grid window controls.
        The export-to-grid (discharge-to-grid) window controls are only published when
        `dtg_supported(site_id)` is True, since not every Enphase system supports that
        schedule family. A freeze-enable switch is also published; its window reuses the
        charge window and is written to the "rbd" schedule family at apply time (Task 7).
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
        """Handle a number entity change routed from HA, updating the local schedule model."""
        site_id, attribute = self._parse_entity(entity_id)
        if not site_id or not attribute.startswith("battery_schedule_"):
            return
        field = attribute[len("battery_schedule_") :]
        local = self.local_schedule.setdefault(site_id, self._default_local_schedule())
        if field == "reserve":
            local["reserve"] = int(float(value))
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
        if field == "freeze_enable":
            local["freeze"]["enable"] = self._toggle_to_bool(service, local["freeze"]["enable"])
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
        """Create/update one Enphase schedule family if it differs from the cloud state.

        Converts the HA "HH:MM:SS" option times to Enphase "HH:MM" format, then compares
        against the cached cloud schedule via `schedules_equal()`. A matching schedule is a
        no-op (and clears any stale pending record); a write already pending confirmation for
        this family is left alone rather than re-issued. Otherwise updates the existing
        schedule by id (`PUT`) or creates a new one (`POST`), then records the write as pending
        regardless of the HTTP result, since Enphase writes can settle even after a non-200
        response. Returns True if a write was issued (not whether it was confirmed).
        """
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
        """Diff the local schedule model against the cloud and issue only the changed writes.

        Reads the latest control-entity state into `local_schedule` first, then writes (only
        where the desired state differs from the cached cloud state):
        1. Reserve, via a profile PUT that preserves the current profile name.
        2. Forced charge-from-grid window (schedule family "CFG"), enabling the
           `chargeFromGrid` battery setting first if required.
        3. Forced export-to-grid window (schedule family "DTG"), only on sites that support it.
        4. Freeze/restrict-discharge (schedule family "RBD"), reusing the charge window times.
        After any write, schedules and profile are re-fetched so pending writes can be
        confirmed (and cleared) as soon as the cloud reflects them.
        """
        await self.get_schedule_settings_ha(site_id)
        local = self.local_schedule.get(site_id, self._default_local_schedule())
        wrote = False

        # Reserve via profile PUT, preserving the current profile name
        desired_reserve = int(local.get("reserve", 0))
        cloud = self.profile.get(site_id, {})
        if desired_reserve and desired_reserve != int(cloud.get("reserve", -1)):
            profile_name = cloud.get("profile") or PROFILE_SELF_CONSUMPTION
            self.log(f"Enphase: Setting reserve to {desired_reserve}% (profile {profile_name}) on site {site_id}")
            params = {"source": "enho"}
            if self.user_id:
                params["userId"] = self.user_id
            await self.request_json("PUT", f"{BATTERY_CONFIG_BASE}/profile/{site_id}", family="battery_config", params=params, json_body={"profile": profile_name, "batteryBackupPercentage": desired_reserve})
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
            # Re-read to confirm; writes settle asynchronously so pending writes may persist for minutes
            await self.get_schedules(site_id)
            for family in (SCHEDULE_CHARGE, SCHEDULE_EXPORT, SCHEDULE_FREEZE):
                pending = self.pending_writes.get((site_id, family))
                if pending and schedules_equal(self.schedules.get(site_id, {}).get(family.lower(), {}), pending["start"], pending["end"], pending["limit"], pending["enabled"]):
                    del self.pending_writes[(site_id, family)]
            await self.get_profile(site_id)

    def clear_expired_pending_writes(self):
        """Drop any pending schedule writes whose settle timeout has elapsed.

        Called once per `run()` cycle so a write that never gets confirmed by the cloud
        (e.g. it was rejected server-side) does not block a future re-attempt forever.
        """
        for key in list(self.pending_writes.keys()):
            site_id, family = key
            self._pending_active(site_id, family)  # drops the entry itself if expired

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
        self.battery_status[site_id] = {
            "soc_percent": soc_percent,
            "available_energy": safe_float(data.get("available_energy"), total_available),
            "max_capacity": safe_float(data.get("max_capacity"), total_capacity),
            "max_power_kw": safe_float(data.get("max_power")),
            "status": str(data.get("status", "")),
            # Note: this payload has no "profile" key. The battery profile name is
            # sourced from self.profile[site_id]["profile"], populated by get_profile().
            "batteries": batteries,
        }
        await self._save_cache("battery_status", self.battery_status)
        return self.battery_status[site_id]

    async def get_lifetime_energy(self, site_id):
        """Fetch and store the raw lifetime energy payload for a site."""
        data = await self.request_json("GET", f"/pv/systems/{site_id}/lifetime_energy")
        if data is None:
            return None
        self.lifetime_energy[site_id] = data
        await self._save_cache("lifetime_energy", self.lifetime_energy)
        return data

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
        self.profile[site_id] = {
            "profile": str(data.get("profile", "")),
            "reserve": safe_int(data.get("batteryBackupPercentage")),
        }
        await self._save_cache("profile", self.profile)
        return self.profile[site_id]

    async def get_battery_settings(self, site_id):
        """Fetch and store the battery charge-from-grid and low-SOC settings for a site."""
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/batterySettings/{site_id}", family="battery_config", params={"source": "enlm"})
        if data is None:
            return None
        self.battery_settings[site_id] = {
            "chargeFromGrid": bool(data.get("chargeFromGrid", False)),
            "veryLowSoc": safe_int(data.get("veryLowSoc"), None),
            "veryLowSocMin": safe_int(data.get("veryLowSocMin"), None),
            "veryLowSocMax": safe_int(data.get("veryLowSocMax"), None),
        }
        await self._save_cache("battery_settings", self.battery_settings)
        return self.battery_settings[site_id]

    async def get_schedules(self, site_id):
        """Fetch and normalise the charge/export/freeze battery schedules for a site.

        NOTE: the real response for a battery-less account is
        ``{"cfg": {"scheduleStatus": "active", "count": 0}, "dtg": {...}, "rbd": {...}}`` -
        the per-schedule window/limit fields (when count > 0) have not yet been observed against
        a real battery account, so the ``details``/``scheduleSupported`` parsing below is a
        best-effort placeholder that must be verified once a battery site is available. ``count``
        and ``scheduleStatus`` are captured for diagnostics/gating in the meantime.
        """
        data = await self.request_json("GET", f"{BATTERY_CONFIG_BASE}/battery/sites/{site_id}/schedules", family="battery_config")
        if data is None:
            return None
        parsed = {}
        for family_key in ("cfg", "dtg", "rbd"):
            family_data = data.get(family_key) or {}
            details = family_data.get("details") or []
            entry = details[0] if details else {}
            # "supported" gates whether Predbat can use this schedule family. Real accounts report
            # a per-family scheduleStatus ("active" seen so far); treat the usable statuses as
            # supported, with a fallback to the (unverified) boolean flags. Needs confirmation
            # against a battery account where an unsupported family's status value is known.
            status_text = str(family_data.get("scheduleStatus", "")).strip().lower()
            supported = status_text in ("active", "enabled", "supported", "available") or bool(family_data.get("scheduleSupported") or family_data.get("forceScheduleSupported"))
            parsed[family_key] = {
                "id": entry.get("id"),
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
        # Predbat controls charging via the charge-from-grid (CFG) schedule family. If the site
        # does not support CFG scheduling there is nothing to control, so fail configuration
        # rather than publishing an inverter Predbat cannot drive.
        if not self.schedules.get(site_id, {}).get("cfg", {}).get("supported", False):
            raise ValueError("Enphase API: Charge-from-grid (CFG) scheduling not supported on this site, cannot configure")
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
        self.xsrf_token = current.get("XSRF-TOKEN", current.get("BP-XSRF-Token", self.xsrf_token))

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
        else:
            preview = text or ""
        if len(preview) > 600:
            preview = preview[:600] + f"...(+{len(preview) - 600} more chars)"
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
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None, **kwargs):
        """Return the default for any requested arg (no real config in standalone mode)."""
        return default

    def set_arg(self, key, value):
        """Print an arg that automatic_config would set on the real base object."""
        print(f"Set arg {key} = {value}")


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

        # Dump the raw lifetime_energy channels so the units (Wh vs kWh) and whether the arrays
        # are per-day increments or cumulative totals can be checked against the published *_today values.
        le = api.lifetime_energy.get(sid, {})
        print("lifetime_energy raw (last 5 entries per channel):")
        for channel in ("production", "consumption", "import", "export", "charge", "discharge"):
            values = le.get(channel) or []
            print(f"  {channel}: len={len(values)} last5={values[-5:]}")
        print(f"  start_date={le.get('start_date')} last_report_date={le.get('last_report_date')} interval_minutes={le.get('interval_minutes')}")

        # Read-only probe of the /today endpoint (a cadence-independent source of today's totals),
        # so its response shape can be inspected before wiring the *_today sensors to it.
        today = await api.request_json("GET", f"/pv/systems/{sid}/today")
        print(f"today endpoint raw: {json.dumps(today, default=str)[:1500] if today is not None else None}")

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

    args = parser.parse_args()

    if args.write_schedule:
        asyncio.run(test_write_schedule(args.username, args.password, args.site_id, args.start_time, args.end_time, args.soc))
    else:
        asyncio.run(test_enphase_api(args.username, args.password, args.site_id))


if __name__ == "__main__":
    main()

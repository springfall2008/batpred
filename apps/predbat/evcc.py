# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# evcc EV charge controller API library
# -----------------------------------------------------------------------------

# API Documentation: https://docs.evcc.io/integrations/rest-api/

"""evcc integration.

Reads the state of an evcc instance over its REST API and feeds it into Predbat's car
model, and optionally writes the loadpoint charging mode back so Predbat's own solar/grid
decision is applied without a Home Assistant automation in between.

The headline capability is that evcc keeps charging plans on the *vehicle*, not the
loadpoint, so a departure time and target SoC can be read even while the car is away.
That is what lets Predbat plan the car's demand - and pre-charge the home battery while
import is cheap - before the car is ever plugged in.

Auto-configuration is deliberately additive: any car_charging_* key the user set in
apps.yaml themselves is left alone, because evcc is not the best source for all of them
(a Home Assistant car integration reports a live SoC while the car is away, where evcc
only knows the SoC it last observed while connected).
"""

from datetime import datetime, timedelta
import asyncio
import json
import aiohttp

from component_base import ComponentBase
from mock_base import MockBase as SharedMockBase
from predbat_metrics import record_api_call
from utils import str2time, dp2, dp3
from const import CAR_MODE_NOW, CAR_MODE_SOLAR

# Modes evcc accepts on POST /api/loadpoints/{id}/mode/{mode}
EVCC_MODE_OFF = "off"
EVCC_MODE_NOW = "now"
EVCC_MODE_MINPV = "minpv"
EVCC_MODE_PV = "pv"
EVCC_MODES = [EVCC_MODE_OFF, EVCC_MODE_NOW, EVCC_MODE_MINPV, EVCC_MODE_PV]

# The sun-following resting modes. These are the only ones Predbat will take a loadpoint out of:
# off and now are settings the user made deliberately, and evcc is left holding them.
EVCC_RESTING_MODES = [EVCC_MODE_PV, EVCC_MODE_MINPV]

# Predbat reasons that evcc cannot reach on its own, and the mode each one borrows the loadpoint for.
# Everything else - the resting state, the home battery priority, an unplugged car - evcc does itself.
EVCC_TAKEOVER_REASONS = {"grid_slot": EVCC_MODE_NOW, "export_better": EVCC_MODE_OFF}

# Published when no mode is being held on Predbat's behalf, matching the "off" spelling used elsewhere
NO_RESTORE = "none"

# Go serialises an unset time.Time as its zero value rather than null, and it parses
# perfectly well into year 1 - so it has to be rejected by prefix, not by exception
GO_ZERO_TIME_PREFIX = "0001-01-01"

# evcc weekdays are 0 = Sunday .. 6 = Saturday; Python's weekday() is 0 = Monday .. 6 = Sunday
EVCC_WEEKDAY_LOOKAHEAD_DAYS = 8

# Predbat's car_charging_plan_time is a time of day wrapped into a single 24h window by
# car_ready_minutes(), so a plan further out than this cannot be represented
PLAN_HORIZON_HOURS = 24

# Values that mean "no override" when published as a Predbat sensor, matching parse_car_ready_time
NO_PLAN = "off"


def parse_evcc_time(value, log=None):
    """
    Parse an evcc timestamp, returning an aware datetime or None when it is unset.

    Handles the three "no time" spellings seen in the wild: null, the empty string, and Go's
    zero time "0001-01-01T00:00:00Z" (which would otherwise parse cleanly into year 1).
    """
    if not value or not isinstance(value, str):
        return None
    if value.startswith(GO_ZERO_TIME_PREFIX):
        return None
    try:
        return str2time(value)
    except (ValueError, TypeError):
        if log:
            log("Warn: EvccAPI: could not parse timestamp '{}'".format(value))
        return None


def evcc_weekday(date_value):
    """Convert a Python date to evcc's weekday numbering (0 = Sunday .. 6 = Saturday)"""
    return (date_value.weekday() + 1) % 7


def plan_timezone(name, fallback_tz, log=None):
    """
    Resolve an IANA timezone name from a repeating plan, falling back when it is unknown.
    """
    if not name:
        return fallback_tz
    try:
        import pytz

        return pytz.timezone(name)
    except Exception:
        if log:
            log("Warn: EvccAPI: unknown timezone '{}' in repeating plan, using the local timezone".format(name))
        return fallback_tz


def localise(tz, naive_dt):
    """
    Attach a timezone to a naive datetime, using pytz's localize when the object provides it.
    """
    if hasattr(tz, "localize"):
        return tz.localize(naive_dt)
    return naive_dt.replace(tzinfo=tz)


def next_repeating_plan(plans, now, fallback_tz, log=None):
    """
    Find the next occurrence of an active repeating plan, returning (datetime, soc) or (None, None).

    evcc numbers weekdays 0 = Sunday, so the Python weekday has to be rotated. The search runs
    over eight days rather than seven so a plan that fires only on today's weekday rolls to next
    week once today's time has passed, instead of being missed.
    """
    best_time = None
    best_soc = None

    for plan in plans or []:
        if not isinstance(plan, dict) or not plan.get("active", False):
            continue
        weekdays = plan.get("weekdays") or []
        try:
            hour, minute = [int(part) for part in str(plan.get("time", "")).split(":")[:2]]
        except (ValueError, TypeError):
            if log:
                log("Warn: EvccAPI: could not parse repeating plan time '{}'".format(plan.get("time")))
            continue

        tz = plan_timezone(plan.get("tz"), fallback_tz, log=log)
        now_local = now.astimezone(tz)

        for days_ahead in range(EVCC_WEEKDAY_LOOKAHEAD_DAYS):
            candidate_date = (now_local + timedelta(days=days_ahead)).date()
            if evcc_weekday(candidate_date) not in weekdays:
                continue
            candidate = localise(tz, datetime.combine(candidate_date, datetime.min.time()).replace(hour=hour, minute=minute))
            if candidate <= now:
                continue
            if best_time is None or candidate < best_time:
                best_time = candidate
                best_soc = plan.get("soc")
            break

    return best_time, best_soc


def round_down_5min(when):
    """
    Round a datetime down to a 5-minute boundary.

    Predbat's time selects are built on a 5-minute grid, and writing an off-grid value would
    permanently add it to the shared OPTIONS_TIME list.
    """
    return when.replace(minute=(when.minute // 5) * 5, second=0, microsecond=0)


def plan_time_to_ready(plan_dt, now, local_tz):
    """
    Map a plan datetime onto Predbat's time-of-day ready time, or NO_PLAN when it cannot be used.

    Returns NO_PLAN for a missing plan, a plan already in the past, and a plan more than
    PLAN_HORIZON_HOURS out - car_ready_minutes() wraps a time of day into a single 24 hour window,
    so a plan several days away would otherwise be read as "needed tomorrow".
    """
    if plan_dt is None:
        return NO_PLAN
    if plan_dt <= now:
        return NO_PLAN
    if (plan_dt - now) > timedelta(hours=PLAN_HORIZON_HOURS):
        return NO_PLAN
    return round_down_5min(plan_dt.astimezone(local_tz)).strftime("%H:%M:%S")


def resolve_plan(vehicle, loadpoint, now, local_tz, log=None):
    """
    Resolve the charging plan that applies to a car, highest precedence first.

    1. The loadpoint's effective plan - evcc's own resolution, authoritative while connected
    2. The vehicle's one-off plan, if it is still in the future
    3. The next occurrence of an active repeating plan
    4. Nothing

    Returns {"time": datetime|None, "soc": int|None, "source": str}.
    """
    loadpoint = loadpoint or {}
    vehicle = vehicle or {}

    effective = parse_evcc_time(loadpoint.get("effectivePlanTime"), log=log)
    if effective and effective > now:
        return {"time": effective, "soc": loadpoint.get("effectivePlanSoc") or None, "source": "loadpoint"}

    plan = vehicle.get("plan")
    if isinstance(plan, dict):
        one_off = parse_evcc_time(plan.get("time"), log=log)
        if one_off and one_off > now:
            return {"time": one_off, "soc": plan.get("soc"), "source": "vehicle"}

    repeat_time, repeat_soc = next_repeating_plan(vehicle.get("repeatingPlans"), now, local_tz, log=log)
    if repeat_time:
        return {"time": repeat_time, "soc": repeat_soc, "source": "repeating"}

    return {"time": None, "soc": None, "source": "none"}


def effective_limit(loadpoint, vehicle):
    """
    Resolve the standing SoC limit evcc will actually stop at, or None when there is none.

    evcc's own effectiveLimitSoc already resolves the loadpoint's session limit over the vehicle's
    standing one, so it is preferred; the vehicle value is only a fallback for an older evcc that
    does not report the effective field. A 0 means "no limit" rather than "stop now", and so does
    anything outside 0-100, which is why this returns None instead of a number to clamp with.
    """
    for source in (loadpoint or {}, vehicle or {}):
        for field in ("effectiveLimitSoc", "limitSoc"):
            if field not in source:
                continue
            try:
                value = float(source[field])
            except (TypeError, ValueError):
                continue
            if 0 < value <= 100:
                return value
    return None


def derive_power_band(loadpoint, voltage):
    """
    Derive the charger's (min_kw, max_kw, step_kw) from evcc's current and phase configuration.

    step_kw is one ampere, which is the granularity a charger can actually switch in - the
    remainder it leaves behind is what Predbat's solar diversion model needs to account for.
    Returns (None, None, None) when the loadpoint does not report currents.
    """
    loadpoint = loadpoint or {}
    min_current = loadpoint.get("effectiveMinCurrent", loadpoint.get("minCurrent"))
    max_current = loadpoint.get("effectiveMaxCurrent", loadpoint.get("maxCurrent"))
    phases = loadpoint.get("phasesActive") or loadpoint.get("phasesConfigured") or 0

    if not min_current or not max_current or not phases:
        return None, None, None

    step = phases * voltage / 1000.0
    return dp3(min_current * step), dp3(max_current * step), dp3(step)


class EvccClient:
    """Minimal aiohttp client for the evcc REST API."""

    def __init__(self, host, api_key=None, timeout=15, log=None):
        """Store the normalised base URL and optional bearer key."""
        self.host = self.normalise_host(host)
        self.api_key = api_key
        self.timeout = timeout
        self.log = log or (lambda message: None)

    @staticmethod
    def normalise_host(host):
        """
        Normalise a configured host into a base URL, adding the scheme and default port if absent.
        """
        host = str(host or "").strip().rstrip("/")
        if not host:
            return ""
        if "://" not in host:
            host = "http://" + host
        # Add evcc's default port when the user gave a bare host
        remainder = host.split("://", 1)[1]
        if ":" not in remainder.split("/", 1)[0]:
            host += ":7070"
        return host

    def headers(self):
        """Build the request headers, omitting the Authorization header when no key is set."""
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer {}".format(self.api_key)
        return headers

    async def request(self, method, path, max_retries=3):
        """
        Perform one API request with retry, returning the decoded payload or None.

        Retries on 5xx and on connection errors with exponential backoff; a 4xx is not retried.
        evcc wraps some responses as {"result": ...} and others not, so the wrapper is unwrapped
        here rather than at every call site.
        """
        if not self.host:
            return None

        url = "{}/api{}".format(self.host, path)
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    call = session.post if method == "POST" else session.get
                    async with call(url, headers=self.headers()) as response:
                        if response.status == 200:
                            try:
                                data = await response.json()
                            except Exception as error:
                                self.log("Warn: EvccAPI: failed to decode {} {}: {}".format(method, path, error))
                                record_api_call("evcc", False, "decode_error")
                                return None
                            record_api_call("evcc")
                            if isinstance(data, dict) and "result" in data and len(data) == 1:
                                return data["result"]
                            return data
                        if response.status in (401, 403):
                            self.log("Warn: EvccAPI: {} {} refused with status {} - check evcc_api_key".format(method, path, response.status))
                            record_api_call("evcc", False, "auth_error")
                            return None
                        if response.status >= 500:
                            record_api_call("evcc", False, "server_error")
                            if attempt < max_retries - 1:
                                await asyncio.sleep(2**attempt)
                                continue
                            self.log("Warn: EvccAPI: {} {} failed with status {} after {} attempts".format(method, path, response.status, max_retries))
                            return None
                        self.log("Warn: EvccAPI: {} {} failed with status {}".format(method, path, response.status))
                        record_api_call("evcc", False, "client_error")
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                self.log("Warn: EvccAPI: {} {} failed after {} attempts: {}".format(method, path, max_retries, error))
                record_api_call("evcc", False, "connection_error")
                return None
        return None

    async def get_state(self):
        """Fetch the full evcc state, or None when it could not be read."""
        return await self.request("GET", "/state")

    async def set_mode(self, loadpoint_id, mode):
        """
        Set a loadpoint's charging mode. loadpoint_id is evcc's 1-based id. Returns True on success.

        The response body is not relied on - confirmation comes from the next state fetch.
        """
        if mode not in EVCC_MODES:
            self.log("Warn: EvccAPI: refusing to set unknown mode '{}'".format(mode))
            return False
        result = await self.request("POST", "/loadpoints/{}/mode/{}".format(loadpoint_id, mode))
        return result is not None


class EvccAPI(ComponentBase):
    """evcc REST API client that feeds Predbat's car model and optionally drives the charge mode."""

    def initialize(self, host, api_key=None, automatic=False, control=False, loadpoints=None, solar=True, use_minpv=False, poll_seconds=60, mode_refresh_minutes=15, override_minutes=60, phase_voltage=230, soc_max_age_hours=24, timeout=15):
        """
        Initialise the evcc component from its apps.yaml arguments.

        Args:
            host: evcc base URL or host[:port]
            api_key: optional long-lived evcc API key; no Authorization header is sent without it
            automatic: auto-configure the car_charging_* keys from evcc
            control: fallback for switch.predbat_evcc_control[_n], used only while those config
                switches do not exist yet (no cars configured)
            loadpoints: optional per-car list of evcc loadpoint ids (1-based) or titles
            solar: model evcc's PV diversion (sets car_charging_solar)
            use_minpv: prefer evcc's minpv mode over pv when Predbat wants solar charging
            poll_seconds: how often to fetch the evcc state
            mode_refresh_minutes: how often to re-assert the mode even when unchanged
            override_minutes: how long to back off after the mode is changed outside Predbat
            phase_voltage: mains voltage used to convert amps to kW
            soc_max_age_hours: age past which a remembered vehicle SoC is flagged stale
            timeout: HTTP timeout in seconds
        """
        self.client = EvccClient(host, api_key=api_key, timeout=timeout, log=self.log)
        self.automatic = bool(automatic)
        self.control = bool(control)
        self.loadpoints_config = loadpoints
        self.solar = bool(solar)
        self.use_minpv = bool(use_minpv)
        self.poll_seconds = max(int(poll_seconds or 60), 15)
        self.mode_refresh_minutes = int(mode_refresh_minutes or 0)
        self.override_minutes = int(override_minutes or 0)
        self.phase_voltage = float(phase_voltage or 230)
        self.soc_max_age_hours = float(soc_max_age_hours or 0)

        self.state = {}
        self.state_time = None
        self.loadpoint_map = {}
        self.config_signature = None
        self.queued_events = []
        self.force_refresh = False
        self.missing_fields = []
        self.overridden_keys = []
        self.unknown_loadpoints = []

        # Per-car runtime state, keyed by Predbat car index
        self.sticky_soc = {}
        self.sticky_soc_time = {}
        self.written_mode = {}
        self.restore_mode = {}
        self.written_priority_soc = None
        self.capped_state = {}
        self.written_solar_enabled = {}
        self.last_write = {}
        self.override_until = {}
        self.last_connected = {}
        self.guest_charging = {}
        # Last seen state of the Predbat config switches, so read_switches() can spot a change.
        # Guest hold is off by default: evcc loses identification of a car from time to time (its
        # API goes down), and holding the battery on that alone would force expensive import
        self.control_enabled = {}
        self.guest_hold_enabled = False

        # Snapshot apps.yaml before any set_arg, so auto-config can tell a user's own value from
        # one it wrote itself on an earlier cycle
        self.args_at_startup = dict(getattr(self.base, "args", {}) or {})

        if not self.client.host:
            self.log("Warn: EvccAPI: evcc_host is not set, the component cannot start")

    # ------------------------------------------------------------------ helpers

    def car_postfix(self, car_n):
        """Return the entity postfix for a car index, matching publish_car_plan's convention."""
        return "" if car_n == 0 else "_{}".format(car_n)

    def entity(self, domain, name, car_n):
        """Build a published entity id for a car."""
        return "{}.{}_evcc_{}{}".format(domain, self.prefix, name, self.car_postfix(car_n))

    def config_entity(self, name):
        """Build the entity id Predbat gives one of our CONFIG_ITEMS switches."""
        return "switch.{}_{}".format(self.prefix, name)

    def control_key(self, car_n):
        """Config key for this car's control switch, switch.predbat_evcc_control[_n]."""
        return "evcc_control" + self.car_postfix(car_n)

    def car_controlled(self, car_n):
        """
        True when Predbat may write the charge mode for this car's loadpoint.

        Per loadpoint, not per instance: one evcc drives several loadpoints and only some of them
        are car chargers, so a heat pump alongside the car must be left to evcc whatever is done
        with the car. evcc_control in apps.yaml is only the fallback when the switch does not
        exist yet (no cars configured), which is also what the tests drive it with.
        """
        return bool(self.get_arg(self.control_key(car_n), self.control))

    def user_configured(self, key):
        """
        True when the user set this key in apps.yaml themselves, so auto-config must not clobber it.

        Uses the startup snapshot rather than the live args, because set_arg writes into the same
        dict - without it the second cycle would treat its own values as user configuration.
        """
        return self.args_at_startup.get(key) is not None

    def auto_set(self, key, value):
        """Set an apps.yaml key from evcc unless the user configured it themselves."""
        if self.user_configured(key):
            if key not in self.overridden_keys:
                self.overridden_keys.append(key)
                self.log("EvccAPI: leaving {} as configured in apps.yaml, not overriding from evcc".format(key))
            return False
        self.set_arg(key, value)
        return True

    def loadpoint_index(self, spec, loadpoints):
        """
        Resolve one evcc_loadpoints entry to a 0-based index into the loadpoints array.

        Accepts a 1-based id (int or numeric string, matching evcc's own UI and REST paths) or a
        loadpoint title matched case-insensitively. Returns None for "off"/null and for anything
        that cannot be resolved.
        """
        if spec is None:
            return None
        if isinstance(spec, bool):
            return None
        text = str(spec).strip()
        if not text or text.lower() in ("off", "none", "null"):
            return None

        try:
            index = int(text) - 1
        except ValueError:
            for index, loadpoint in enumerate(loadpoints):
                if str(loadpoint.get("title", "")).strip().lower() == text.lower():
                    return index
            if text not in self.unknown_loadpoints:
                self.unknown_loadpoints.append(text)
                self.log("Warn: EvccAPI: loadpoint '{}' not found in evcc (have: {})".format(text, [lp.get("title") for lp in loadpoints]))
            return None

        if 0 <= index < len(loadpoints):
            return index
        if text not in self.unknown_loadpoints:
            self.unknown_loadpoints.append(text)
            self.log("Warn: EvccAPI: loadpoint id {} is out of range, evcc has {}".format(text, len(loadpoints)))
        return None

    def resolve_mapping(self, loadpoints):
        """
        Build the Predbat car index -> loadpoint index map.

        Unset means identity: car n maps to loadpoint n. A configured list maps one entry per car
        in car order, so an evcc car can be mixed with a car managed some other way.
        """
        mapping = {}
        if self.loadpoints_config:
            specs = self.loadpoints_config if isinstance(self.loadpoints_config, list) else [self.loadpoints_config]
            for car_n, spec in enumerate(specs):
                index = self.loadpoint_index(spec, loadpoints)
                if index is not None:
                    mapping[car_n] = index
        else:
            for car_n in range(len(loadpoints)):
                mapping[car_n] = car_n
        return mapping

    def vehicle_for(self, loadpoint):
        """
        Return (vehicle_key, vehicle_dict) for a loadpoint, or (None, {}) when nothing is known.

        evcc clears vehicleName on disconnect, so the last seen vehicle is remembered - that is
        what makes the plan readable while the car is away.

        A loadpoint that is connected with no vehicleName is a car evcc could not identify - a guest,
        or one whose API is down. That must never resolve to the configured vehicle: evcc gives an
        unidentified car the loadpoint's own default mode rather than the vehicle's, so it charges to
        somebody else's plan, and planning it as the known car would buy import for a battery Predbat
        knows nothing about. The single-vehicle shortcut is therefore only safe while disconnected.
        """
        vehicles = self.state.get("vehicles") or {}
        key = loadpoint.get("vehicleName") or None
        if key and key in vehicles:
            return key, vehicles[key]
        if key:
            return key, {}
        if loadpoint.get("connected"):
            return None, {}
        if len(vehicles) == 1:
            # A single configured vehicle is unambiguous while nothing is plugged in
            only_key = list(vehicles.keys())[0]
            return only_key, vehicles[only_key]
        return None, {}

    # ------------------------------------------------------------------ sticky SoC

    def restore_sticky(self, car_n):
        """
        Seed the remembered SoC from our own published sensor, so a Predbat restart is not a reset.
        """
        if car_n in self.sticky_soc:
            return
        state = self.get_state_wrapper(entity_id=self.entity("sensor", "soc", car_n))
        try:
            value = float(state)
        except (TypeError, ValueError):
            return
        if value > 0:
            self.sticky_soc[car_n] = value
            self.sticky_soc_time[car_n] = self.now_utc

    def update_sticky_soc(self, car_n, loadpoint, vehicle, known=True):
        """
        Track the vehicle SoC, keeping the last observed value while the car is disconnected.

        Returns (soc, stale, age_minutes). evcc only reports the SoC of a connected vehicle, so
        without this the value would drop to zero the moment the car is unplugged.

        An unidentified car contributes nothing: whatever evcc reports for it belongs to somebody
        else's battery, and letting it land here would overwrite the remembered SoC of the car the
        plan is actually built around.
        """
        self.restore_sticky(car_n)

        soc = vehicle.get("soc") if known else None
        if not soc and known and loadpoint.get("connected"):
            soc = loadpoint.get("vehicleSoc")

        if soc:
            self.sticky_soc[car_n] = float(soc)
            self.sticky_soc_time[car_n] = self.now_utc

        remembered = self.sticky_soc.get(car_n)
        if remembered is None:
            # Never observed - report it as stale rather than letting a 0 look like a fresh reading
            return None, True, None

        seen_at = self.sticky_soc_time.get(car_n, self.now_utc)
        age_minutes = int((self.now_utc - seen_at).total_seconds() / 60)
        stale = bool(self.soc_max_age_hours) and age_minutes > self.soc_max_age_hours * 60
        return remembered, stale, age_minutes

    # ------------------------------------------------------------------ publishing

    def log_cap(self, car_n, plan_soc, standing, capped):
        """
        Log the plan target being capped by the standing limit, once per transition.

        Only on change, like publish_priority_soc and publish_solar_enabled - the poll runs every
        minute and a line each time would bury everything else in the log. Not capped is the
        assumed starting point, so an ordinary car does not announce itself on the first poll.
        """
        if self.capped_state.get(car_n, False) == capped:
            return
        self.capped_state[car_n] = capped
        if capped:
            self.log("EvccAPI: car {} plan target {}% capped to the evcc limit of {}%".format(car_n, dp2(plan_soc), dp2(standing)))
        else:
            self.log("EvccAPI: car {} plan target is no longer capped by the evcc limit".format(car_n))

    def publish_car(self, car_n, loadpoint):
        """Publish every entity for one car from its loadpoint and vehicle."""
        vehicle_key, vehicle = self.vehicle_for(loadpoint)
        connected = bool(loadpoint.get("connected"))
        charging = bool(loadpoint.get("charging"))
        # Only a car evcc has identified is one Predbat can plan for - see vehicle_for
        known = connected and bool(vehicle_key)

        self.dashboard_item(self.entity("binary_sensor", "connected", car_n), state="on" if connected else "off", attributes={"friendly_name": "Predbat evcc car connected", "icon": "mdi:ev-plug-type2", "loadpoint": loadpoint.get("title")}, app="evcc")
        self.dashboard_item(
            self.entity("binary_sensor", "known_car", car_n),
            state="on" if known else "off",
            attributes={"friendly_name": "Predbat evcc known car connected", "icon": "mdi:car-key", "vehicle": vehicle_key, "connected": connected},
            app="evcc",
        )
        # A car evcc cannot identify, drawing power right now. Nothing in Predbat's plan accounts for
        # it, so left alone the home battery would quietly cover somebody else's charge
        guest = connected and charging and not known
        self.guest_charging[car_n] = guest
        self.dashboard_item(
            self.entity("binary_sensor", "guest_charging", car_n),
            state="on" if guest else "off",
            attributes={"friendly_name": "Predbat evcc guest car charging", "icon": "mdi:account-question", "vehicle": vehicle_key, "connected": connected},
            app="evcc",
        )
        self.dashboard_item(self.entity("binary_sensor", "charging", car_n), state="on" if charging else "off", attributes={"friendly_name": "Predbat evcc car charging", "icon": "mdi:ev-station"}, app="evcc")

        soc, stale, age_minutes = self.update_sticky_soc(car_n, loadpoint, vehicle, known=known or not connected)
        self.dashboard_item(
            self.entity("sensor", "soc", car_n),
            state=dp2(soc) if soc is not None else 0,
            attributes={"friendly_name": "Predbat evcc car SoC", "unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:battery-charging", "stale": stale, "age_minutes": age_minutes, "connected": connected, "observed": soc is not None},
            app="evcc",
        )

        capacity = vehicle.get("capacity")
        if capacity:
            self.dashboard_item(self.entity("sensor", "battery_size", car_n), state=dp2(capacity), attributes={"friendly_name": "Predbat evcc car battery size", "unit_of_measurement": "kWh", "icon": "mdi:car-battery"}, app="evcc")

        plan = resolve_plan(vehicle, loadpoint, self.now_utc, self.local_tz, log=self.log)
        ready = plan_time_to_ready(plan["time"], self.now_utc, self.local_tz)
        self.dashboard_item(
            self.entity("sensor", "plan_time", car_n),
            state=ready,
            attributes={"friendly_name": "Predbat evcc car ready time", "icon": "mdi:clock-end", "plan_source": plan["source"], "plan_datetime": plan["time"].isoformat() if plan["time"] else None},
            app="evcc",
        )

        # Never publish 0 here - get_arg would take it at face value and plan nothing. When there
        # is no usable plan the standing limit applies on its own instead.
        standing = effective_limit(loadpoint, vehicle)
        limit = plan["soc"] if (ready != NO_PLAN and plan["soc"]) else None
        capped = bool(limit and standing and standing < limit)
        if capped:
            # evcc stops at the standing limit whatever the plan asks for, so planning grid slots
            # for the difference would buy import the car is never going to take
            limit = standing
        if not limit:
            limit = standing or 100
        self.log_cap(car_n, plan["soc"], standing, capped)
        self.dashboard_item(
            self.entity("sensor", "plan_soc", car_n),
            state=dp2(limit),
            attributes={
                "friendly_name": "Predbat evcc car charge limit",
                "unit_of_measurement": "%",
                "icon": "mdi:battery-charging-100",
                "plan_source": plan["source"],
                "plan_soc": plan["soc"],
                "effective_limit_soc": standing,
                "capped": capped,
            },
            app="evcc",
        )

        self.dashboard_item(self.entity("sensor", "limit_soc", car_n), state=dp2(standing or 100), attributes={"friendly_name": "Predbat evcc car solar limit", "unit_of_measurement": "%", "icon": "mdi:solar-power-variant"}, app="evcc")

        min_kw, max_kw, step_kw = derive_power_band(loadpoint, self.phase_voltage)
        if max_kw:
            phases = loadpoint.get("phasesActive") or loadpoint.get("phasesConfigured")
            power_attributes = {"unit_of_measurement": "kW", "icon": "mdi:flash", "phases": phases, "voltage": self.phase_voltage}
            self.dashboard_item(self.entity("sensor", "max_power", car_n), state=max_kw, attributes=dict(power_attributes, friendly_name="Predbat evcc charger max power"), app="evcc")
            self.dashboard_item(self.entity("sensor", "min_power", car_n), state=min_kw, attributes=dict(power_attributes, friendly_name="Predbat evcc charger min power"), app="evcc")
            self.dashboard_item(self.entity("sensor", "power_step", car_n), state=step_kw, attributes=dict(power_attributes, friendly_name="Predbat evcc charger power step"), app="evcc")

        self.dashboard_item(
            self.entity("sensor", "charge_power", car_n),
            state=dp2(loadpoint.get("chargePower") or 0),
            attributes={"friendly_name": "Predbat evcc charge power", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement", "icon": "mdi:flash"},
            app="evcc",
        )
        self.dashboard_item(
            self.entity("sensor", "mode", car_n),
            state=loadpoint.get("mode") or "unknown",
            attributes={
                "friendly_name": "Predbat evcc charge mode",
                "icon": "mdi:ev-station",
                "loadpoint_id": self.loadpoint_map.get(car_n, 0) + 1,
                "title": loadpoint.get("title"),
                "predbat_written": self.written_mode.get(car_n),
                # What evcc applies by itself when this vehicle connects - so a mode Predbat never
                # wrote is not blamed on Predbat
                "vehicle_default_mode": vehicle.get("mode"),
            },
            app="evcc",
        )
        self.dashboard_item(
            self.entity("sensor", "vehicle", car_n),
            state=vehicle.get("title") or loadpoint.get("vehicleTitle") or "unknown",
            attributes={"friendly_name": "Predbat evcc vehicle", "icon": "mdi:car", "key": vehicle_key, "connected": connected, "known": known},
            app="evcc",
        )
        self.dashboard_item(self.entity("binary_sensor", "plan_active", car_n), state="on" if loadpoint.get("planActive") else "off", attributes={"friendly_name": "Predbat evcc plan active", "icon": "mdi:calendar-clock"}, app="evcc")

    def publish_priority_soc(self):
        """
        Take the home battery priority SoC from evcc's site configuration.

        evcc's prioritySoc is the level below which PV surplus fills the home battery before the car,
        which is exactly what input_number.predbat_car_charging_solar_min_soc means to the diversion
        model - so with automatic configuration on, evcc owns it rather than the user keeping two
        numbers in step by hand. Written only when it changes, so it does not fight the UI every poll.

        evcc also enforces it, which Predbat's own home_battery_low decision then does not need to -
        see Output.publish_car_solar_slot. Flagged here rather than inferred from the component being
        loaded, because a second car on a plain charger still needs Predbat to enforce it.
        """
        priority = self.state.get("prioritySoc")
        try:
            priority = float(priority)
        except (TypeError, ValueError):
            return None
        if priority < 0 or priority > 100:
            return None

        self.dashboard_item(
            self.entity("sensor", "priority_soc", 0),
            state=dp2(priority),
            attributes={"friendly_name": "Predbat evcc home battery priority SoC", "unit_of_measurement": "%", "icon": "mdi:home-battery", "state_class": "measurement"},
            app="evcc",
        )

        if not self.automatic or not self.solar:
            return priority
        # Only on change, so a value the user adjusts in Home Assistant is not overwritten every poll -
        # evcc wins when evcc's own setting moves, and stays out of the way otherwise
        if self.written_priority_soc is not None and abs(self.written_priority_soc - priority) < 0.01:
            return priority

        self.log("EvccAPI: home battery priority SoC {}% from evcc".format(dp2(priority)))
        self.base.expose_config("car_charging_solar_min_soc", priority)
        self.base.car_charging_solar_min_soc_external = True
        self.written_priority_soc = priority
        return priority

    def publish_status(self, status):
        """Publish the component's own health sensor."""
        self.dashboard_item(
            "sensor.{}_evcc_status".format(self.prefix),
            state=status,
            attributes={
                "friendly_name": "Predbat evcc status",
                "icon": "mdi:api",
                "version": self.state.get("version"),
                "site": self.state.get("siteTitle"),
                "loadpoints": len(self.state.get("loadpoints") or []),
                "vehicles": len(self.state.get("vehicles") or {}),
                "cars": sorted(self.loadpoint_map.keys()),
                "last_update": self.state_time.isoformat() if self.state_time else None,
                "missing_fields": self.missing_fields,
                "overridden": self.overridden_keys,
                "errors": self.get_error_count(),
                "control": [car_n for car_n in sorted(self.loadpoint_map) if self.car_controlled(car_n)],
            },
            app="evcc",
        )

    # ------------------------------------------------------------------ auto configuration

    def automatic_config(self):
        """
        Point Predbat's car keys at the entities this component publishes.

        Additive by design: any key the user set in apps.yaml themselves is left alone, so a live
        SoC from a car integration beats evcc's last observed value. See auto_set().
        """
        cars = sorted(self.loadpoint_map.keys())
        if not cars:
            return
        count = max(cars) + 1

        if self.get_arg("num_cars", 0) < count:
            self.set_arg("num_cars", count)

        def per_car(domain, name):
            """Build the per-car entity list in car order, padding gaps with None."""
            return [self.entity(domain, name, car_n) if car_n in self.loadpoint_map else None for car_n in range(count)]

        # Planned and plugged drive the plan itself, so they follow the identified car rather than the
        # cable: a guest on the loadpoint is somebody else's charge and must not pull grid slots
        self.auto_set("car_charging_planned", per_car("binary_sensor", "known_car"))
        self.auto_set("car_charging_plugged", per_car("binary_sensor", "known_car"))
        self.auto_set("car_charging_soc", per_car("sensor", "soc"))
        self.auto_set("car_charging_battery_size", per_car("sensor", "battery_size"))
        self.auto_set("car_charging_limit", per_car("sensor", "plan_soc"))
        self.auto_set("car_charging_ready_time", per_car("sensor", "plan_time"))
        self.auto_set("car_charging_planned_response", ["yes", "on", "true", "connected", "charging"])

        # For a car Predbat drives, car_charging_now would be a mirror of Predbat's own decision
        # and the plan would chase its own tail - so it is wired up only for the cars whose control
        # switch is off, and taken back off a car whose switch is turned on later
        charging_now = [self.entity("binary_sensor", "charging", car_n) if (car_n in self.loadpoint_map and not self.car_controlled(car_n)) else None for car_n in range(count)]
        if any(charging_now):
            self.auto_set("car_charging_now", charging_now)
        elif not self.user_configured("car_charging_now"):
            self.set_arg("car_charging_now", None)

        if self.solar:
            self.publish_solar_enabled(count)
            self.auto_set("car_charging_solar_limit", per_car("sensor", "limit_soc"))
            self.auto_set("car_charging_solar_max_power", per_car("sensor", "max_power"))
            self.auto_set("car_charging_solar_min_power", per_car("sensor", "min_power"))
            self.auto_set("car_charging_solar_power_step", per_car("sensor", "power_step"))

        self.log("EvccAPI: auto-configured {} car(s) from evcc{}".format(len(cars), " (left alone: {})".format(self.overridden_keys) if self.overridden_keys else ""))

    def publish_solar_enabled(self, count):
        """
        Turn the per-car solar charging switch on for the cars evcc actually drives a loadpoint for.

        car_charging_solar is a Home Assistant switch rather than an apps.yaml key, so this writes it
        the way publish_priority_soc writes the priority SoC: only when evcc's own answer changes, so a
        user who turns the switch off in Home Assistant is not overruled on the next poll. evcc wins
        again when its topology moves - a loadpoint appearing or going away.
        """
        for car_n in range(count):
            wired = car_n in self.loadpoint_map
            if self.written_solar_enabled.get(car_n) == wired:
                continue
            self.written_solar_enabled[car_n] = wired
            key = "car_charging_solar" + self.car_postfix(car_n)
            self.log("EvccAPI: car {} solar charging {} from evcc".format(car_n, "on" if wired else "off"))
            self.base.expose_config(key, wired)

    def signature(self, loadpoints):
        """Build a signature of the evcc topology, so auto-config re-runs when it changes."""
        return frozenset((index, str(loadpoint.get("title")), str(loadpoint.get("vehicleName") or "")) for index, loadpoint in enumerate(loadpoints))

    # ------------------------------------------------------------------ mode control

    def desired_mode(self, car_n):
        """
        Work out the evcc mode Predbat wants for a car, returning (mode, reason).

        The decision itself is Predbat's, published as sensor.<prefix>_car_charging_mode during its
        own cycle - see Output.publish_car_charging_mode - so evcc and a plain Home Assistant
        automation act on identical logic. This only maps those three states onto evcc's own modes.
        A missing sensor means Predbat has not planned yet, which must never be read as "off".

        Solar maps to pv (or minpv), and it is the resting state rather than off. off is safe for the
        loadpoint's own departure plan - evcc keeps it, still reports it here, and it stays editable;
        it only stops evcc acting on it itself, which Predbat is doing anyway through now. The reason
        to rest in pv is what happens when Predbat is not publishing at all: a loadpoint left in pv
        still charges from the sun, where one left off would sit idle until Predbat came back.
        """
        postfix = self.car_postfix(car_n)
        entity_id = "sensor.{}_car_charging_mode{}".format(self.prefix, postfix)
        mode = self.get_state_wrapper(entity_id=entity_id)
        if mode is None:
            return None, "no_plan_yet"
        reason = self.get_state_wrapper(entity_id=entity_id, attribute="reason") or str(mode)

        mode = str(mode).lower()
        if mode == CAR_MODE_NOW:
            return EVCC_MODE_NOW, reason
        if mode == CAR_MODE_SOLAR:
            return (EVCC_MODE_MINPV if self.use_minpv else EVCC_MODE_PV), reason
        return EVCC_MODE_OFF, reason

    def should_write(self, car_n, mode, reason, connected):
        """
        Decide whether Predbat may write to this loadpoint at all, returning (allowed, reason).

        This is the "can it" gate; what to write, if anything, is write_target's decision. Every
        refusal carries a distinct reason so "why is it not doing anything" can be answered from
        sensor.predbat_evcc_target_mode alone.

        An empty loadpoint is evcc's own business: evcc resets it to the loadpoint's configured mode
        on disconnect and applies the vehicle's default mode on connect, and writing over either
        would undo a setting the user chose.
        """
        if mode is None:
            return False, reason
        if not self.car_controlled(car_n):
            return False, "control_disabled"
        if not connected:
            return False, "not_connected"
        if self.get_arg("set_read_only", False):
            return False, "read_only"
        if not getattr(self.base, "plan_valid", False):
            return False, "plan_invalid"
        if self.state_time is None or (self.now_utc - self.state_time) > timedelta(seconds=2 * self.poll_seconds):
            return False, "state_stale"
        return True, reason

    def restore_saved_mode(self, car_n):
        """
        Seed the borrowed mode from our own published sensor, so a restart mid-episode still hands back.

        Without this a Predbat restart during a grid slot would leave the loadpoint in now with nothing
        left that knows to undo it - the same reason restore_sticky reads its own sensor back.
        """
        if car_n in self.restore_mode:
            return
        state = self.get_state_wrapper(entity_id=self.entity("sensor", "restore_mode", car_n))
        if state in EVCC_RESTING_MODES:
            self.restore_mode[car_n] = state
            self.log("EvccAPI: car {} resuming an unfinished takeover, {} is still owed back".format(car_n, state))

    def write_target(self, car_n, reason, observed):
        """
        Work out what to write, returning (mode|None, reason).

        Predbat borrows a loadpoint that is resting in a sun-following mode, and only for a decision
        evcc cannot reach on its own: a planned grid slot, or an export price that beats putting the
        surplus in the car. When that ends, the borrowed mode is handed straight back.

        Everything else stays evcc's: a loadpoint the user put in off or now is a deliberate setting
        and is never taken over, the resting state itself is what evcc would do anyway, and the home
        battery priority is enforced by evcc's own prioritySoc - which is why home_battery_low cannot
        even arise for an evcc car, see publish_priority_soc.
        """
        borrowed = self.restore_mode.get(car_n)
        takeover = EVCC_TAKEOVER_REASONS.get(reason)

        if takeover:
            if borrowed:
                # Mid-episode: keep asserting, so an evcc restart cannot quietly drop it
                return takeover, reason
            if observed in EVCC_RESTING_MODES:
                return takeover, reason
            return None, "not_resting"

        if borrowed:
            return borrowed, "restore"
        return None, "left_to_evcc"

    def drop_takeover(self, car_n, why):
        """Forget the borrowed mode without handing it back, logging why when there was one."""
        if self.restore_mode.pop(car_n, None):
            self.log("EvccAPI: car {} takeover ended ({})".format(car_n, why))

    def detect_override(self, car_n, observed):
        """
        Detect that somebody changed the mode outside Predbat and back off for a while.

        The backoff is cleared on a disconnect->connect transition: a new session is a new intent,
        so Predbat resumes control for the next car rather than staying overridden forever.
        """
        until = self.override_until.get(car_n)
        if until and self.now_utc < until:
            return True
        if until:
            self.override_until.pop(car_n, None)

        last = self.written_mode.get(car_n)
        if last is not None and observed is not None and observed != last and self.override_minutes:
            self.override_until[car_n] = self.now_utc + timedelta(minutes=self.override_minutes)
            self.written_mode[car_n] = None
            self.log("Warn: EvccAPI: car {} mode changed externally to '{}' (Predbat set '{}') - backing off for {} minutes".format(car_n, observed, last, self.override_minutes))
            return True
        return False

    async def apply_modes(self):
        """Compute, publish and where allowed write the evcc charge mode for every mapped car."""
        for car_n, lp_index in sorted(self.loadpoint_map.items()):
            loadpoints = self.state.get("loadpoints") or []
            if lp_index >= len(loadpoints):
                continue
            loadpoint = loadpoints[lp_index]
            observed = loadpoint.get("mode")

            # Seeded before any gate: plan_valid is False on the first cycle after a restart, which is
            # exactly the cycle where an unfinished takeover has to be recovered rather than published away
            self.restore_saved_mode(car_n)

            # A new session clears any standing override
            connected = bool(loadpoint.get("connected"))
            if connected and not self.last_connected.get(car_n, False):
                self.override_until.pop(car_n, None)
            self.last_connected[car_n] = connected

            if not connected:
                # Nothing is owed back to an empty loadpoint: evcc resets the mode itself on
                # disconnect, which also means detect_override would read that reset as somebody
                # overruling Predbat, and that the first poll of the next session must write even
                # when the mode happens to match what was written a session ago.
                self.written_mode.pop(car_n, None)
                self.last_write.pop(car_n, None)
                self.drop_takeover(car_n, "disconnected")

            mode, decision = self.desired_mode(car_n)
            allowed, reason = self.should_write(car_n, mode, decision, connected)
            target = None
            written = False

            if allowed:
                target, reason = self.write_target(car_n, decision, observed)

            if target and self.restore_mode.get(car_n) and self.detect_override(car_n, observed):
                # Only mid-episode is there anything to override - outside one Predbat is not writing,
                # so a mode the user sets is simply the state it will next borrow from, or not
                self.drop_takeover(car_n, "overridden in evcc")
                target, reason = None, "overridden"

            if target:
                refresh_due = self.mode_refresh_minutes and (car_n not in self.last_write or (self.now_utc - self.last_write[car_n]) > timedelta(minutes=self.mode_refresh_minutes))
                if self.written_mode.get(car_n) != target or refresh_due:
                    if await self.client.set_mode(lp_index + 1, target):
                        self.written_mode[car_n] = target
                        self.last_write[car_n] = self.now_utc
                        written = True
                        if reason == "restore":
                            self.drop_takeover(car_n, "handed back")
                        elif not self.restore_mode.get(car_n):
                            self.restore_mode[car_n] = observed
                            self.log("EvccAPI: car {} borrowing loadpoint {} from {} ({})".format(car_n, lp_index + 1, observed, reason))
                        self.log("EvccAPI: loadpoint {} mode -> {} ({})".format(lp_index + 1, target, reason))
                    else:
                        reason = "write_failed"

            # The mode that actually applies: the borrowed one while Predbat is holding the loadpoint,
            # evcc's own the rest of the time. Predbat's untranslated decision stays in predbat_mode,
            # so a takeover that was refused is still visible without the state having to lie about it
            self.dashboard_item(
                self.entity("sensor", "target_mode", car_n),
                state=target or observed or "unknown",
                attributes={
                    "friendly_name": "Predbat evcc target mode",
                    "icon": "mdi:ev-station",
                    "reason": reason,
                    "decision": decision,
                    "predbat_mode": mode,
                    "write_target": target,
                    "written": written,
                    "evcc_mode": observed,
                    "borrowed": bool(self.restore_mode.get(car_n)),
                    "allowed": allowed,
                },
                app="evcc",
            )
            self.dashboard_item(
                self.entity("sensor", "restore_mode", car_n),
                state=self.restore_mode.get(car_n) or NO_RESTORE,
                attributes={"friendly_name": "Predbat evcc mode to restore", "icon": "mdi:backup-restore", "borrowed": bool(self.restore_mode.get(car_n))},
                app="evcc",
            )
            self.dashboard_item(
                self.entity("binary_sensor", "override", car_n),
                state="on" if self.override_until.get(car_n) else "off",
                attributes={"friendly_name": "Predbat evcc override", "icon": "mdi:hand-back-left", "until": self.override_until[car_n].isoformat() if self.override_until.get(car_n) else None, "observed_mode": observed},
                app="evcc",
            )

    def read_switches(self):
        """
        Notice a change to the user switches Predbat keeps for us in its own config.

        They are CONFIG_ITEMS rather than entities this component publishes: that is what puts them
        alongside every other Predbat switch in the web UI and in the saved config. Nothing is read
        from them here beyond noticing a change - car_controlled() reads each one live - but a
        change has to be acted on, so it is logged and the auto-configuration is redone.
        """
        for car_n in sorted(self.loadpoint_map):
            enabled = self.car_controlled(car_n)
            if enabled != self.control_enabled.get(car_n, enabled):
                self.log("EvccAPI: charge mode control on car {}'s loadpoint switched {}".format(car_n, "on" if enabled else "off"))
                # car_charging_now follows the switch, so the auto-configuration has to be redone
                self.config_signature = None
            self.control_enabled[car_n] = enabled

        guest_hold = bool(self.get_arg("evcc_guest_hold", False))
        if guest_hold != self.guest_hold_enabled:
            self.log("EvccAPI: guest car battery hold switched {}".format("on" if guest_hold else "off"))
        self.guest_hold_enabled = guest_hold

    def guest_hold(self):
        """
        Tell Predbat whether the home battery must be held right now for an unidentified car.

        Predbat gets one boolean rather than the switch and the state separately, because the decision
        is the component's: only it knows which car on which loadpoint evcc failed to identify. The
        switch itself lives in the config (see read_switches); what is published here is what the
        component did with it, so "why is the battery held" is answerable from one entity.
        """
        holding = self.guest_hold_enabled and any(self.guest_charging.get(car_n, False) for car_n in self.loadpoint_map)
        if holding != bool(getattr(self.base, "evcc_guest_charging", False)):
            self.log("EvccAPI: home battery hold for an unidentified car {}".format("on" if holding else "off"))
        self.base.evcc_guest_charging = holding

        self.dashboard_item(
            self.entity("binary_sensor", "guest_hold", 0),
            state="on" if holding else "off",
            attributes={"friendly_name": "Predbat evcc holding battery for a guest car", "icon": "mdi:home-battery-outline", "enabled": self.guest_hold_enabled},
            app="evcc",
        )
        return holding

    # ------------------------------------------------------------------ events

    async def switch_event(self, entity_id, service):
        """Queue a switch change for the component's own loop to handle."""
        self.queued_events.append((entity_id, service))

    def handle_switch_event(self, entity_id, service):
        """
        React to one of our config switches being toggled.

        The value itself is read back from the config item by read_switches - all this does is bring
        the next cycle forward, so a kill switch takes effect now rather than at the next poll.
        """
        if entity_id == self.config_entity("evcc_guest_hold") or entity_id.startswith(self.config_entity("evcc_control")):
            self.force_refresh = True

    # ------------------------------------------------------------------ main loop

    def check_fields(self, loadpoints):
        """Record which expected fields the evcc state did not carry, to make drift diagnosable."""
        missing = []
        if not loadpoints:
            missing.append("loadpoints")
        else:
            for field in ("connected", "charging", "mode", "minCurrent", "maxCurrent", "phasesActive", "effectiveLimitSoc"):
                if field not in loadpoints[0]:
                    missing.append("loadpoints[0].{}".format(field))
        if "vehicles" not in self.state:
            missing.append("vehicles")
        self.missing_fields = missing

    async def run(self, seconds, first):
        """
        Fetch the evcc state, publish it, auto-configure Predbat and apply the charge mode.

        Called once a minute by ComponentBase.start(); the poll interval is honoured inside.
        """
        while self.queued_events:
            entity_id, service = self.queued_events.pop(0)
            self.handle_switch_event(entity_id, service)

        self.read_switches()

        due = first or self.force_refresh or (seconds % self.poll_seconds) < 60
        if not due:
            return True
        self.force_refresh = False

        state = await self.client.get_state()
        if not state:
            self.publish_status("unreachable")
            return False

        self.state = state
        self.state_time = self.now_utc
        loadpoints = state.get("loadpoints") or []
        self.check_fields(loadpoints)
        self.loadpoint_map = self.resolve_mapping(loadpoints)

        for car_n, lp_index in sorted(self.loadpoint_map.items()):
            if lp_index < len(loadpoints):
                try:
                    self.publish_car(car_n, loadpoints[lp_index])
                except Exception as error:
                    # One malformed loadpoint must not stop the others or the component
                    self.log("Warn: EvccAPI: failed to publish car {}: {}".format(car_n, error))

        self.publish_priority_soc()
        self.guest_hold()

        signature = self.signature(loadpoints)
        if self.automatic and signature != self.config_signature:
            self.config_signature = signature
            self.automatic_config()

        await self.apply_modes()
        self.publish_status("ok" if not self.missing_fields else "degraded")
        self.update_success_timestamp()
        return True


class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the evcc command-line harness, with its own cache directory."""

    def __init__(self):
        """Initialise the shared mock with the evcc cache root."""
        super().__init__(config_root="./temp_evcc")


async def test_evcc_api(host, api_key):  # pragma: no cover
    """
    Run one evcc fetch cycle against a real instance and print what Predbat would use.
    """
    print("Testing evcc API at {}".format(host))
    mock_base = MockBase()
    api = EvccAPI(mock_base, host=host, api_key=api_key, automatic=True, control=False)

    ok = await api.run(0, True)
    print("\nrun() returned {}".format(ok))
    print("evcc version {} site '{}'".format(api.state.get("version"), api.state.get("siteTitle")))
    print("loadpoint map: {}".format(api.loadpoint_map))
    print("\nPublished entities:")
    for entity_id in sorted(mock_base.entities):
        if "_evcc_" in entity_id:
            print("  {} = {}".format(entity_id, mock_base.entities[entity_id]["state"]))
    print("\nApps.yaml keys that would be set:")
    print(json.dumps({key: value for key, value in mock_base.args.items() if key.startswith("car_charging") or key == "num_cars"}, indent=2, default=str))
    await api.final()


def main():  # pragma: no cover
    """
    Main function for command line execution to test the evcc API.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Test the evcc API")
    parser.add_argument("--host", required=True, help="evcc base URL, e.g. http://192.168.1.50:7070")
    parser.add_argument("--api-key", default=None, help="Optional evcc API key")

    args = parser.parse_args()
    asyncio.run(test_evcc_api(args.host, args.api_key))


if __name__ == "__main__":
    main()

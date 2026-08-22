# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the evcc component.

SAMPLE_STATE is trimmed from a real evcc 0.313.2 /api/state response (1-phase 6-16A charger,
a PHEV with three repeating plans and no vehicle-level SoC), which is the shape the component
has to keep working against as evcc's schema drifts.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytz

from components import COMPONENT_LIST
from config import APPS_SCHEMA
from evcc import (
    EVCC_MODE_MINPV,
    EVCC_MODE_NOW,
    EVCC_MODE_OFF,
    EVCC_MODE_PV,
    EvccAPI,
    EvccClient,
    derive_power_band,
    evcc_weekday,
    next_repeating_plan,
    parse_evcc_time,
    plan_time_to_ready,
    resolve_plan,
    round_down_5min,
)
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session, run_async

COPENHAGEN = pytz.timezone("Europe/Copenhagen")

REPEATING_PLANS = [
    {"weekdays": [1, 2, 3, 4, 5], "time": "07:30", "tz": "Europe/Copenhagen", "soc": 100, "active": True},
    {"weekdays": [1, 2, 3, 4, 5], "time": "15:00", "tz": "Europe/Copenhagen", "soc": 70, "active": True},
    {"weekdays": [6, 0], "time": "10:00", "tz": "Europe/Copenhagen", "soc": 100, "active": True},
]

SAMPLE_STATE = {
    "version": "0.313.2",
    "siteTitle": "Home",
    "vehicles": {"db:3": {"title": "VW Passat GTE", "capacity": 11, "icon": "car", "repeatingPlans": REPEATING_PLANS}},
    "loadpoints": [
        {
            "title": "Garage",
            "mode": "pv",
            "connected": False,
            "charging": False,
            "vehicleName": "",
            "vehicleSoc": 0,
            "limitSoc": 0,
            "effectiveLimitSoc": 100,
            "planTime": None,
            "effectivePlanTime": None,
            "planActive": False,
            "phasesActive": 1,
            "phasesConfigured": 1,
            "minCurrent": 6,
            "maxCurrent": 16,
            "effectiveMinCurrent": 6,
            "effectiveMaxCurrent": 16,
            "chargePower": 0,
        }
    ],
}


class MockEvccAPI(EvccAPI):
    """EvccAPI without ComponentBase, so the pure logic can be driven directly."""

    def __init__(self, now=None, **kwargs):
        """Build the component with in-memory entity/arg stores and a fixed clock."""
        self._now = now or datetime(2026, 8, 11, 13, 54, tzinfo=timezone.utc)
        self.prefix = "predbat"
        self.local_tz = COPENHAGEN
        self.entities = {}
        self.args = dict(kwargs.pop("args", {}) or {})
        self.log_messages = []
        self.count_errors = 0
        self.base = self
        self.plan_valid = True
        self.config_written = {}
        self.initialize(**kwargs)

    def expose_config(self, name, value, **kwargs):
        """Record a config write instead of updating a Home Assistant entity."""
        self.config_written[name] = value

    @property
    def now_utc(self):
        """Fixed clock for deterministic plan resolution."""
        return self._now

    def log(self, message, quiet=True):
        """Record a log line."""
        self.log_messages.append(message)

    def dashboard_item(self, entity, state, attributes, app=None):
        """Store a published entity."""
        self.entities[entity] = {"state": state, "attributes": attributes}

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, **kwargs):
        """Read back a stored entity state or attribute."""
        entity = self.entities.get(entity_id)
        if entity is None:
            return default
        if attribute is not None:
            return entity.get("attributes", {}).get(attribute, default)
        return entity.get("state", default)

    def get_arg(self, arg, default=None, **kwargs):
        """Read an apps.yaml arg."""
        return self.args.get(arg, default)

    def set_arg(self, arg, value):
        """Write an apps.yaml arg."""
        self.args[arg] = value

    def update_success_timestamp(self):
        """No-op for the mock."""

    def get_error_count(self):
        """Report the recorded error count."""
        return self.count_errors


def check(name, got, expected, failures):
    """Compare a value and record a failure line when it differs."""
    if got != expected:
        print("ERROR: {}: expected {!r} got {!r}".format(name, expected, got))
        failures.append(name)
    return failures


def test_parse_evcc_time():
    """Unset timestamps in all three spellings evcc uses must parse as None"""
    print("**** Running Test: evcc_parse_time ****")
    failures = []
    check("None", parse_evcc_time(None), None, failures)
    check("empty", parse_evcc_time(""), None, failures)
    # Go serialises an unset time.Time as its zero value, which would otherwise parse into year 1
    check("go_zero", parse_evcc_time("0001-01-01T00:00:00Z"), None, failures)
    check("garbage", parse_evcc_time("not a time"), None, failures)
    check("non_string", parse_evcc_time(12345), None, failures)

    parsed = parse_evcc_time("2026-08-11T15:00:00+02:00")
    if parsed is None or parsed.hour != 15 or parsed.tzinfo is None:
        print("ERROR: evcc_parse_time: RFC3339 with offset did not parse to an aware 15:00, got {}".format(parsed))
        failures.append("rfc3339")
    return failures


def test_weekday_conversion():
    """evcc numbers weekdays 0 = Sunday, Python numbers them 0 = Monday"""
    print("**** Running Test: evcc_weekday ****")
    failures = []
    # 2026-08-10 is a Monday
    check("monday", evcc_weekday(datetime(2026, 8, 10).date()), 1, failures)
    check("friday", evcc_weekday(datetime(2026, 8, 14).date()), 5, failures)
    check("saturday", evcc_weekday(datetime(2026, 8, 15).date()), 6, failures)
    check("sunday", evcc_weekday(datetime(2026, 8, 16).date()), 0, failures)
    return failures


def test_next_repeating_plan():
    """The next occurrence of a repeating plan, over the user's real three-plan configuration"""
    print("**** Running Test: evcc_next_repeating_plan ****")
    failures = []

    def next_at(local_dt):
        """Resolve the next plan as seen from a local wall-clock time."""
        return next_repeating_plan(REPEATING_PLANS, COPENHAGEN.localize(local_dt), COPENHAGEN)

    # Tuesday lunchtime -> the same day's 15:00 plan
    when, soc = next_at(datetime(2026, 8, 11, 12, 0))
    check("tue_noon_time", (when.hour, when.minute), (15, 0), failures)
    check("tue_noon_soc", soc, 70, failures)

    # Tuesday evening -> 15:00 has passed, so Wednesday 07:30
    when, soc = next_at(datetime(2026, 8, 11, 17, 0))
    check("tue_evening_day", when.day, 12, failures)
    check("tue_evening_time", (when.hour, when.minute), (7, 30), failures)
    check("tue_evening_soc", soc, 100, failures)

    # Saturday -> the weekend plan
    when, soc = next_at(datetime(2026, 8, 15, 8, 0))
    check("sat_time", (when.hour, when.minute), (10, 0), failures)
    check("sat_soc", soc, 100, failures)

    # An inactive plan is ignored
    inactive = [dict(REPEATING_PLANS[1], active=False)]
    when, _ = next_repeating_plan(inactive, COPENHAGEN.localize(datetime(2026, 8, 11, 12, 0)), COPENHAGEN)
    check("inactive", when, None, failures)

    # repeatingPlans is nullable
    when, _ = next_repeating_plan(None, COPENHAGEN.localize(datetime(2026, 8, 11, 12, 0)), COPENHAGEN)
    check("null_plans", when, None, failures)

    # A plan for today only, after its time, must roll to next week rather than being missed
    today_only = [{"weekdays": [2], "time": "09:00", "tz": "Europe/Copenhagen", "soc": 80, "active": True}]
    when, _ = next_repeating_plan(today_only, COPENHAGEN.localize(datetime(2026, 8, 11, 12, 0)), COPENHAGEN)
    check("rolls_a_week", (when.day, when.hour), (18, 9), failures)

    # An unparsable time is skipped rather than raising
    when, _ = next_repeating_plan([{"weekdays": [1, 2], "time": "nonsense", "active": True}], COPENHAGEN.localize(datetime(2026, 8, 11, 12, 0)), COPENHAGEN)
    check("bad_time", when, None, failures)
    return failures


def test_plan_time_to_ready():
    """Mapping a plan datetime onto Predbat's time-of-day ready time"""
    print("**** Running Test: evcc_plan_time_to_ready ****")
    failures = []
    now = COPENHAGEN.localize(datetime(2026, 8, 11, 12, 0))

    check("none", plan_time_to_ready(None, now, COPENHAGEN), "off", failures)
    check("past", plan_time_to_ready(now - timedelta(hours=1), now, COPENHAGEN), "off", failures)
    # Beyond the 24h window car_ready_minutes can represent, a time of day would be read as tomorrow
    check("too_far", plan_time_to_ready(now + timedelta(days=3), now, COPENHAGEN), "off", failures)
    check("future", plan_time_to_ready(now + timedelta(hours=3), now, COPENHAGEN), "15:00:00", failures)
    # Off-grid minutes must round down, or the value pollutes the shared OPTIONS_TIME list
    check("rounded", plan_time_to_ready(now + timedelta(hours=3, minutes=23), now, COPENHAGEN), "15:20:00", failures)
    check("round_down", round_down_5min(datetime(2026, 8, 11, 15, 23, 45)).strftime("%H:%M:%S"), "15:20:00", failures)
    return failures


def test_resolve_plan_precedence():
    """Loadpoint plan beats vehicle plan beats repeating plan beats nothing"""
    print("**** Running Test: evcc_resolve_plan ****")
    failures = []
    now = COPENHAGEN.localize(datetime(2026, 8, 11, 12, 0))
    vehicle = {"repeatingPlans": REPEATING_PLANS, "plan": {"soc": 90, "time": "2026-08-11T18:00:00+02:00"}}

    plan = resolve_plan(vehicle, {"effectivePlanTime": "2026-08-11T16:00:00+02:00", "effectivePlanSoc": 55}, now, COPENHAGEN)
    check("loadpoint_source", plan["source"], "loadpoint", failures)
    check("loadpoint_soc", plan["soc"], 55, failures)

    plan = resolve_plan(vehicle, {"effectivePlanTime": None}, now, COPENHAGEN)
    check("vehicle_source", plan["source"], "vehicle", failures)
    check("vehicle_soc", plan["soc"], 90, failures)

    plan = resolve_plan({"repeatingPlans": REPEATING_PLANS}, {}, now, COPENHAGEN)
    check("repeating_source", plan["source"], "repeating", failures)
    check("repeating_soc", plan["soc"], 70, failures)

    check("none_source", resolve_plan({}, {}, now, COPENHAGEN)["source"], "none", failures)

    # The Go zero time must not be mistaken for a real loadpoint plan
    plan = resolve_plan({"repeatingPlans": REPEATING_PLANS}, {"effectivePlanTime": "0001-01-01T00:00:00Z"}, now, COPENHAGEN)
    check("go_zero_falls_through", plan["source"], "repeating", failures)

    # A stale plan left behind after a completed session must not be used
    plan = resolve_plan({"plan": {"soc": 90, "time": "2026-08-10T06:00:00+02:00"}}, {}, now, COPENHAGEN)
    check("stale_vehicle_plan", plan["source"], "none", failures)
    return failures


def test_power_band():
    """The solar power band is derived from evcc's current and phase configuration"""
    print("**** Running Test: evcc_power_band ****")
    failures = []
    # The user's real charger: single phase, 6-16A at 230V
    min_kw, max_kw, step_kw = derive_power_band(SAMPLE_STATE["loadpoints"][0], 230)
    check("1ph_min", min_kw, 1.38, failures)
    check("1ph_max", max_kw, 3.68, failures)
    check("1ph_step", step_kw, 0.23, failures)

    min_kw, max_kw, step_kw = derive_power_band({"minCurrent": 6, "maxCurrent": 16, "phasesActive": 3}, 230)
    check("3ph_min", min_kw, 4.14, failures)
    check("3ph_max", max_kw, 11.04, failures)
    check("3ph_step", step_kw, 0.69, failures)

    check("missing", derive_power_band({}, 230), (None, None, None), failures)
    return failures


def test_host_normalisation():
    """A configured host is normalised into a base URL with evcc's default port"""
    print("**** Running Test: evcc_host ****")
    failures = []
    check("bare", EvccClient.normalise_host("192.168.1.159"), "http://192.168.1.159:7070", failures)
    check("with_port", EvccClient.normalise_host("192.168.1.159:7070"), "http://192.168.1.159:7070", failures)
    check("full", EvccClient.normalise_host("http://evcc.local:7070/"), "http://evcc.local:7070", failures)
    check("https", EvccClient.normalise_host("https://evcc.example.com"), "https://evcc.example.com:7070", failures)
    check("empty", EvccClient.normalise_host(None), "", failures)
    return failures


def test_loadpoint_mapping():
    """evcc_loadpoints accepts 1-based ids and titles, and maps unresolvable entries to nothing"""
    print("**** Running Test: evcc_loadpoint_mapping ****")
    failures = []
    loadpoints = [{"title": "Garage"}, {"title": "Carport"}]
    api = MockEvccAPI(host="http://evcc")

    check("id_1", api.loadpoint_index(1, loadpoints), 0, failures)
    check("id_2", api.loadpoint_index("2", loadpoints), 1, failures)
    check("title", api.loadpoint_index("carport", loadpoints), 1, failures)
    check("off", api.loadpoint_index("off", loadpoints), None, failures)
    check("null", api.loadpoint_index(None, loadpoints), None, failures)
    check("unknown_title", api.loadpoint_index("Shed", loadpoints), None, failures)
    check("out_of_range", api.loadpoint_index(9, loadpoints), None, failures)

    # Unset means identity
    check("identity", MockEvccAPI(host="http://evcc").resolve_mapping(loadpoints), {0: 0, 1: 1}, failures)
    # A configured list can skip a car so it can be managed some other way
    check("configured", MockEvccAPI(host="http://evcc", loadpoints=["off", "Garage"]).resolve_mapping(loadpoints), {1: 0}, failures)
    return failures


def test_publish_and_auto_config():
    """Publishing the real sample state, and the apps.yaml-wins rule"""
    print("**** Running Test: evcc_publish ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", automatic=True, now=datetime(2026, 8, 11, 11, 54, tzinfo=timezone.utc))
    api.state = SAMPLE_STATE
    api.loadpoint_map = {0: 0}
    api.publish_car(0, SAMPLE_STATE["loadpoints"][0])

    check("battery_size", api.entities["sensor.predbat_evcc_battery_size"]["state"], 11, failures)
    # 11:54 UTC is 13:54 in Copenhagen, so the next plan is the weekday 15:00 one
    check("plan_time", api.entities["sensor.predbat_evcc_plan_time"]["state"], "15:00:00", failures)
    check("plan_soc", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 70, failures)
    check("max_power", api.entities["sensor.predbat_evcc_max_power"]["state"], 3.68, failures)
    check("connected", api.entities["binary_sensor.predbat_evcc_connected"]["state"], "off", failures)
    # The single configured vehicle is resolved even though the loadpoint reports no vehicleName
    check("vehicle", api.entities["sensor.predbat_evcc_vehicle"]["state"], "VW Passat GTE", failures)
    # A SoC that has never been observed must not look like a fresh zero
    check("soc_stale", api.entities["sensor.predbat_evcc_soc"]["attributes"]["stale"], True, failures)

    api.automatic_config()
    check("num_cars", api.args["num_cars"], 1, failures)
    check("wired_soc", api.args["car_charging_soc"], ["sensor.predbat_evcc_soc"], failures)
    check("wired_ready", api.args["car_charging_ready_time"], ["sensor.predbat_evcc_plan_time"], failures)
    # car_charging_solar is a Home Assistant switch now, so evcc writes the config item rather than an arg
    check("wired_solar", api.config_written["car_charging_solar"], True, failures)
    check("wired_now", api.args["car_charging_now"], ["binary_sensor.predbat_evcc_charging"], failures)

    # A user's own live SoC entity must survive auto-config, while the rest is still filled in
    user = MockEvccAPI(host="http://evcc", automatic=True, args={"car_charging_soc": ["sensor.my_car_soc"]})
    user.state = SAMPLE_STATE
    user.loadpoint_map = {0: 0}
    user.automatic_config()
    check("user_soc_kept", user.args["car_charging_soc"], ["sensor.my_car_soc"], failures)
    check("user_other_set", user.args["car_charging_battery_size"], ["sensor.predbat_evcc_battery_size"], failures)
    check("user_reported", user.overridden_keys, ["car_charging_soc"], failures)

    # Running auto-config twice must not treat the first run's own values as user configuration
    user.overridden_keys = []
    user.args["car_charging_battery_size"] = ["sensor.stale"]
    user.automatic_config()
    check("second_run_updates", user.args["car_charging_battery_size"], ["sensor.predbat_evcc_battery_size"], failures)
    check("second_run_still_keeps", user.args["car_charging_soc"], ["sensor.my_car_soc"], failures)

    # With control on, car_charging_now is left off so Predbat does not read back its own decision
    controlled = MockEvccAPI(host="http://evcc", automatic=True, control=True)
    controlled.state = SAMPLE_STATE
    controlled.loadpoint_map = {0: 0}
    controlled.automatic_config()
    check("no_now_when_control", "car_charging_now" in controlled.args, False, failures)
    return failures


def test_sticky_soc():
    """The vehicle SoC is remembered across disconnect, and goes stale after the configured age"""
    print("**** Running Test: evcc_sticky_soc ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", soc_max_age_hours=24)

    soc, stale, _ = api.update_sticky_soc(0, {"connected": True, "vehicleSoc": 42}, {})
    check("observed", soc, 42, failures)
    check("fresh", stale, False, failures)

    # Disconnected: evcc reports 0, the remembered value must survive
    soc, stale, age = api.update_sticky_soc(0, {"connected": False, "vehicleSoc": 0}, {})
    check("remembered", soc, 42, failures)
    check("still_fresh", stale, False, failures)
    check("age", age, 0, failures)

    # Age it past the limit
    api.sticky_soc_time[0] = api.now_utc - timedelta(hours=30)
    _, stale, _ = api.update_sticky_soc(0, {"connected": False, "vehicleSoc": 0}, {})
    check("gone_stale", stale, True, failures)

    # A vehicle-level SoC, if a future evcc provides one, wins over the loadpoint
    soc, _, _ = api.update_sticky_soc(1, {"connected": False}, {"soc": 77})
    check("vehicle_soc", soc, 77, failures)
    return failures


def test_desired_mode_and_gates():
    """The mode decision from Predbat's two binary sensors, and every write gate"""
    print("**** Running Test: evcc_mode ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", control=True)
    api.loadpoint_map = {0: 0}
    api.state_time = api.now_utc

    def set_mode(mode, reason=None):
        """Publish the charging-mode sensor Predbat writes, which is all this component reads."""
        if mode is not None:
            api.entities["sensor.predbat_car_charging_mode"] = {"state": mode, "attributes": {"reason": reason or mode}}
        else:
            api.entities.pop("sensor.predbat_car_charging_mode", None)

    set_mode("now", "grid_slot")
    check("grid_slot", api.desired_mode(0), (EVCC_MODE_NOW, "grid_slot"), failures)
    set_mode("solar", "solar")
    check("solar", api.desired_mode(0), (EVCC_MODE_PV, "solar"), failures)
    # Resting in pv rather than off, so evcc keeps its own departure plan alive
    set_mode("solar", "idle")
    check("idle", api.desired_mode(0), (EVCC_MODE_PV, "idle"), failures)
    # Off is a decision, not an absence: selling the surplus pays better, or the car does no solar
    set_mode("off", "export_better")
    check("export_better", api.desired_mode(0), (EVCC_MODE_OFF, "export_better"), failures)
    set_mode("off", "solar_disabled")
    check("solar_disabled", api.desired_mode(0), (EVCC_MODE_OFF, "solar_disabled"), failures)
    # minpv replaces pv everywhere pv would be used, resting state included
    api.use_minpv = True
    set_mode("solar", "idle")
    check("idle_minpv", api.desired_mode(0), (EVCC_MODE_MINPV, "idle"), failures)
    api.use_minpv = False
    # No mode sensor means Predbat has not planned yet - that must never read as "off"
    set_mode(None)
    check("no_plan_yet", api.desired_mode(0), (None, "no_plan_yet"), failures)

    set_mode("solar", "idle")
    check("allowed", api.should_write(0, EVCC_MODE_OFF, "idle"), (True, "idle"), failures)
    check("mode_none", api.should_write(0, None, "no_plan_yet")[1], "no_plan_yet", failures)

    api.control = False
    check("control_disabled", api.should_write(0, EVCC_MODE_OFF, "idle"), (False, "control_disabled"), failures)
    api.control = True

    api.control_enabled[0] = False
    check("switch_off", api.should_write(0, EVCC_MODE_OFF, "idle"), (False, "switch_off"), failures)
    api.control_enabled[0] = True

    api.args["set_read_only"] = True
    check("read_only", api.should_write(0, EVCC_MODE_OFF, "idle"), (False, "read_only"), failures)
    api.args["set_read_only"] = False

    api.plan_valid = False
    check("plan_invalid", api.should_write(0, EVCC_MODE_OFF, "idle"), (False, "plan_invalid"), failures)
    api.plan_valid = True

    api.state_time = api.now_utc - timedelta(hours=1)
    check("state_stale", api.should_write(0, EVCC_MODE_OFF, "idle"), (False, "state_stale"), failures)
    return failures


def test_override_detection():
    """A mode changed outside Predbat backs the component off until the next session"""
    print("**** Running Test: evcc_override ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", control=True, override_minutes=60)

    # Nothing written yet, so an unexpected mode is not an override
    check("no_baseline", api.detect_override(0, "pv"), False, failures)

    api.written_mode[0] = EVCC_MODE_PV
    check("agrees", api.detect_override(0, "pv"), False, failures)
    check("differs", api.detect_override(0, "now"), True, failures)
    check("cleared_written", api.written_mode[0], None, failures)
    check("still_backed_off", api.detect_override(0, "now"), True, failures)

    # The backoff expires
    api.override_until[0] = api.now_utc - timedelta(minutes=1)
    check("expired", api.detect_override(0, "now"), False, failures)

    # Zero disables the mechanism entirely
    never = MockEvccAPI(host="http://evcc", control=True, override_minutes=0)
    never.written_mode[0] = EVCC_MODE_PV
    check("disabled", never.detect_override(0, "now"), False, failures)
    return failures


def test_apply_modes_writes():
    """apply_modes writes on change, re-asserts periodically, and clears the override on connect"""
    print("**** Running Test: evcc_apply_modes ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", control=True, mode_refresh_minutes=15)
    api.state = {"loadpoints": [dict(SAMPLE_STATE["loadpoints"][0], mode="off")]}
    api.state_time = api.now_utc
    api.loadpoint_map = {0: 0}
    api.entities["sensor.predbat_car_charging_mode"] = {"state": "now", "attributes": {"reason": "grid_slot"}}

    writes = []

    async def fake_set_mode(loadpoint_id, mode):
        """Record the write instead of performing it."""
        writes.append((loadpoint_id, mode))
        return True

    api.client.set_mode = fake_set_mode

    run_async(api.apply_modes())
    check("wrote_now", writes, [(1, EVCC_MODE_NOW)], failures)
    check("target_sensor", api.entities["sensor.predbat_evcc_target_mode"]["state"], EVCC_MODE_NOW, failures)

    # Unchanged and not yet due for a refresh -> no second write
    api.state["loadpoints"][0]["mode"] = EVCC_MODE_NOW
    run_async(api.apply_modes())
    check("no_repeat", len(writes), 1, failures)

    # Past the refresh interval it is re-asserted, because an evcc restart resets the mode
    api.last_write[0] = api.now_utc - timedelta(minutes=20)
    run_async(api.apply_modes())
    check("reasserted", len(writes), 2, failures)

    # A user changing the mode in evcc stops further writes
    api.state["loadpoints"][0]["mode"] = "off"
    run_async(api.apply_modes())
    check("override_no_write", len(writes), 2, failures)
    check("override_sensor", api.entities["binary_sensor.predbat_evcc_override"]["state"], "on", failures)

    # Plugging a car in is a new intent, so control resumes
    api.state["loadpoints"][0]["connected"] = True
    run_async(api.apply_modes())
    check("resumed_on_connect", len(writes), 3, failures)
    return failures


def test_client_http():
    """The HTTP client unwraps result payloads, sends auth only when configured, and fails soft"""
    print("**** Running Test: evcc_http ****")
    failures = []

    client = EvccClient("http://evcc:7070", log=print)
    check("no_auth_header", "Authorization" in client.headers(), False, failures)
    check("auth_header", EvccClient("http://evcc", api_key="evcc_abc").headers()["Authorization"], "Bearer evcc_abc", failures)

    # evcc wraps some responses as {"result": ...} and others not, so both must work
    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(json_data={"result": {"version": "1.2.3"}}))):
        check("unwrapped", run_async(client.get_state()), {"version": "1.2.3"}, failures)

    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=SAMPLE_STATE))):
        state = run_async(client.get_state())
        check("unwrapped_plain", state.get("version"), "0.313.2", failures)

    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=401))):
        check("auth_error", run_async(client.get_state()), None, failures)

    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(status=404))):
        check("client_error", run_async(client.get_state()), None, failures)

    # A successful mode write goes out as a POST
    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(create_aiohttp_mock_response(json_data={"result": "pv"}))):
        check("set_mode", run_async(client.set_mode(1, EVCC_MODE_PV)), True, failures)

    # An unknown mode is refused before any request is made
    check("bad_mode", run_async(client.set_mode(1, "turbo")), False, failures)
    return failures


def test_registry_and_schema():
    """The component registry entry and its apps.yaml schema keys"""
    print("**** Running Test: evcc_registry ****")
    failures = []
    info = COMPONENT_LIST.get("evcc", {})
    check("registered", bool(info), True, failures)
    check("event_filter", info.get("event_filter"), "predbat_evcc_", failures)
    check("phase", info.get("phase"), 1, failures)

    args = info.get("args", {})
    check("host_required", args.get("host", {}).get("required"), True, failures)
    for name in ("api_key", "automatic", "control", "loadpoints", "solar", "use_minpv"):
        check("optional_" + name, args.get(name, {}).get("required", False), False, failures)

    for key in (
        "evcc_host",
        "evcc_api_key",
        "evcc_automatic",
        "evcc_control",
        "evcc_loadpoints",
        "evcc_solar",
        "evcc_use_minpv",
        "evcc_poll_seconds",
        "evcc_mode_refresh_minutes",
        "evcc_override_minutes",
        "evcc_phase_voltage",
        "evcc_soc_max_age_hours",
        "evcc_timeout",
        "car_charging_ready_time",
    ):
        if key not in APPS_SCHEMA:
            print("ERROR: evcc_registry: APPS_SCHEMA is missing {}".format(key))
            failures.append(key)

    # Every arg's config key must be declared, or the web config editor cannot type it
    for name, spec in args.items():
        if spec.get("config") not in APPS_SCHEMA:
            print("ERROR: evcc_registry: arg {} config key {} not in APPS_SCHEMA".format(name, spec.get("config")))
            failures.append(name)
    return failures


def test_run_cycle():
    """A full run() over the real sample state publishes, auto-configures and stays healthy"""
    print("**** Running Test: evcc_run ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", automatic=True, now=datetime(2026, 8, 11, 11, 54, tzinfo=timezone.utc))

    async def fake_get_state():
        """Return the sample state without any HTTP."""
        return SAMPLE_STATE

    api.client.get_state = fake_get_state
    check("ok", run_async(api.run(0, True)), True, failures)
    check("status", api.entities["sensor.predbat_evcc_status"]["state"], "ok", failures)
    check("no_missing", api.entities["sensor.predbat_evcc_status"]["attributes"]["missing_fields"], [], failures)
    check("mapped", api.loadpoint_map, {0: 0}, failures)
    check("configured", api.args.get("car_charging_ready_time"), ["sensor.predbat_evcc_plan_time"], failures)

    # An unreachable evcc returns False so ComponentBase counts it as an error, and says so
    async def no_state():
        """Simulate an unreachable evcc."""
        return None

    api.client.get_state = no_state
    api.force_refresh = True
    check("unreachable", run_async(api.run(60, False)), False, failures)
    check("unreachable_status", api.entities["sensor.predbat_evcc_status"]["state"], "unreachable", failures)

    # Missing fields are reported rather than crashing
    api.client.get_state = fake_get_state
    api.force_refresh = True
    trimmed = {"version": "9.9", "loadpoints": [{"title": "X"}], "vehicles": {}}
    api.client.get_state = lambda: _immediate(trimmed)
    check("degraded", run_async(api.run(120, False)), True, failures)
    check("degraded_status", api.entities["sensor.predbat_evcc_status"]["state"], "degraded", failures)
    return failures


async def _immediate(value):
    """Return a value from a coroutine, for stubbing async client methods."""
    return value


def test_solar_enabled_switch():
    """The per-car solar charging switch is written from evcc's loadpoint wiring, only on change"""
    print("**** Running Test: evcc_solar_enabled_switch ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", automatic=True)
    api.loadpoint_map = {0: 0}

    api.publish_solar_enabled(2)
    check("car0_on", api.config_written.get("car_charging_solar"), True, failures)
    # The second car has no loadpoint, so its switch is turned off rather than left to guess
    check("car1_off", api.config_written.get("car_charging_solar_1"), False, failures)

    # Unchanged wiring must not be written again, or a switch turned off in HA is fought every poll
    api.config_written.clear()
    api.publish_solar_enabled(2)
    check("no_repeat", api.config_written, {}, failures)

    # A loadpoint appearing in evcc does win
    api.loadpoint_map = {0: 0, 1: 1}
    api.publish_solar_enabled(2)
    check("car1_on", api.config_written.get("car_charging_solar_1"), True, failures)

    return failures


def test_priority_soc():
    """The home battery priority SoC is taken from evcc's site configuration"""
    print("**** Running Test: evcc_priority_soc ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", automatic=True)

    api.state = {"prioritySoc": 35.0}
    api.publish_priority_soc()
    check("published", api.entities["sensor.predbat_evcc_priority_soc"]["state"], 35.0, failures)
    check("written", api.config_written.get("car_charging_solar_min_soc"), 35.0, failures)

    # Unchanged value must not be written again, so a user editing it in HA is not fought every poll
    api.config_written.pop("car_charging_solar_min_soc")
    api.publish_priority_soc()
    check("no_repeat", "car_charging_solar_min_soc" in api.config_written, False, failures)

    # A change in evcc does win
    api.state = {"prioritySoc": 50.0}
    api.publish_priority_soc()
    check("changed", api.config_written.get("car_charging_solar_min_soc"), 50.0, failures)

    # Nothing usable in the state leaves Predbat's own setting alone
    for bad in ({}, {"prioritySoc": None}, {"prioritySoc": "abc"}, {"prioritySoc": 150}):
        api.config_written.clear()
        api.state = bad
        check("ignored_{}".format(bad.get("prioritySoc")), api.publish_priority_soc(), None, failures)
        check("not_written_{}".format(bad.get("prioritySoc")), api.config_written, {}, failures)

    # Without automatic configuration evcc reports the value but does not own it
    manual = MockEvccAPI(host="http://evcc", automatic=False)
    manual.state = {"prioritySoc": 20.0}
    manual.publish_priority_soc()
    check("manual_published", manual.entities["sensor.predbat_evcc_priority_soc"]["state"], 20.0, failures)
    check("manual_not_written", manual.config_written, {}, failures)

    return failures


def test_evcc(my_predbat=None):
    """
    Run the evcc component tests, returns True on failure
    """
    print("**** Running evcc tests ****")
    failures = []
    for test in (
        test_parse_evcc_time,
        test_weekday_conversion,
        test_next_repeating_plan,
        test_plan_time_to_ready,
        test_resolve_plan_precedence,
        test_power_band,
        test_host_normalisation,
        test_loadpoint_mapping,
        test_publish_and_auto_config,
        test_sticky_soc,
        test_desired_mode_and_gates,
        test_solar_enabled_switch,
        test_priority_soc,
        test_override_detection,
        test_apply_modes_writes,
        test_client_http,
        test_registry_and_schema,
        test_run_cycle,
    ):
        failures += test()

    if failures:
        print("**** ERROR: evcc tests failed: {} ****".format(failures))
        return True
    print("**** evcc tests passed ****")
    return False

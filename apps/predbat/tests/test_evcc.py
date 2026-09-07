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
import copy

import pytz

from components import COMPONENT_LIST
from config import APPS_SCHEMA, CONFIG_ITEMS
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
        """Write an apps.yaml arg, or remove it when set to None, as the real set_arg does."""
        if value is None:
            self.args.pop(arg, None)
        else:
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
    # 70 is the plan's own target here, not the 100% loadpoint limit cutting it down
    check("plan_uncapped", api.entities["sensor.predbat_evcc_plan_soc"]["attributes"]["capped"], False, failures)
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

    # Control is a config switch, so it can come on after car_charging_now was already wired up
    switched = MockEvccAPI(host="http://evcc", automatic=True, control=False)
    switched.state = SAMPLE_STATE
    switched.loadpoint_map = {0: 0}
    switched.automatic_config()
    check("now_wired_when_free", switched.args["car_charging_now"], ["binary_sensor.predbat_evcc_charging"], failures)
    switched.args["evcc_control"] = True
    switched.read_switches()
    switched.automatic_config()
    check("now_unwired_when_control", switched.args.get("car_charging_now"), None, failures)

    # ...and it is per loadpoint, so a controlled car 0 must not take car 1 off it
    mixed = MockEvccAPI(host="http://evcc", automatic=True, args={"evcc_control": True})
    mixed.state = SAMPLE_STATE
    mixed.loadpoint_map = {0: 0, 1: 0}
    mixed.automatic_config()
    check("now_per_car", mixed.args["car_charging_now"], [None, "binary_sensor.predbat_evcc_charging_1"], failures)
    check("status_control_list", [car_n for car_n in sorted(mixed.loadpoint_map) if mixed.car_controlled(car_n)], [0], failures)
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
    check("allowed", api.should_write(0, EVCC_MODE_OFF, "export_better", True), (True, "export_better"), failures)
    check("mode_none", api.should_write(0, None, "no_plan_yet", True)[1], "no_plan_yet", failures)
    # An empty loadpoint is evcc's own business
    check("not_connected", api.should_write(0, EVCC_MODE_PV, "idle", False), (False, "not_connected"), failures)

    # What to write, given evcc's current mode, is write_target's decision
    check("takeover_now", api.write_target(0, "grid_slot", EVCC_MODE_PV), (EVCC_MODE_NOW, "grid_slot"), failures)
    check("takeover_from_minpv", api.write_target(0, "export_better", EVCC_MODE_MINPV), (EVCC_MODE_OFF, "export_better"), failures)
    # A loadpoint the user put in off or now is a deliberate setting and is never taken over
    check("not_resting_off", api.write_target(0, "grid_slot", EVCC_MODE_OFF), (None, "not_resting"), failures)
    check("not_resting_now", api.write_target(0, "export_better", EVCC_MODE_NOW), (None, "not_resting"), failures)
    # Everything else evcc does itself - the resting state, a car doing no solar, the battery priority
    for reason in ("idle", "solar", "solar_disabled", "home_battery_low"):
        check("left_to_evcc_{}".format(reason), api.write_target(0, reason, EVCC_MODE_PV), (None, "left_to_evcc"), failures)
    # Mid-episode the borrowed mode is asserted, then handed straight back when the reason goes away
    api.restore_mode[0] = EVCC_MODE_PV
    check("holds", api.write_target(0, "grid_slot", EVCC_MODE_NOW), (EVCC_MODE_NOW, "grid_slot"), failures)
    check("restores", api.write_target(0, "idle", EVCC_MODE_NOW), (EVCC_MODE_PV, "restore"), failures)
    api.restore_mode.pop(0)

    api.control = False
    check("control_disabled", api.should_write(0, EVCC_MODE_OFF, "export_better", True), (False, "control_disabled"), failures)
    api.control = True

    # Control is per loadpoint: turning car 0's switch off leaves any other car alone
    api.args["evcc_control"] = False
    api.args["evcc_control_1"] = True
    check("switch_off_car0", api.should_write(0, EVCC_MODE_OFF, "export_better", True), (False, "control_disabled"), failures)
    check("switch_on_car1", api.should_write(1, EVCC_MODE_OFF, "export_better", True)[0], True, failures)
    del api.args["evcc_control"]
    del api.args["evcc_control_1"]

    api.args["set_read_only"] = True
    check("read_only", api.should_write(0, EVCC_MODE_OFF, "export_better", True), (False, "read_only"), failures)
    api.args["set_read_only"] = False

    api.plan_valid = False
    check("plan_invalid", api.should_write(0, EVCC_MODE_OFF, "export_better", True), (False, "plan_invalid"), failures)
    api.plan_valid = True

    api.state_time = api.now_utc - timedelta(hours=1)
    check("state_stale", api.should_write(0, EVCC_MODE_OFF, "export_better", True), (False, "state_stale"), failures)
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
    """A takeover is asserted, re-asserted, handed back, and abandoned when the user overrules it"""
    print("**** Running Test: evcc_apply_modes ****")
    failures = []
    api = MockEvccAPI(host="http://evcc", control=True, mode_refresh_minutes=15)
    # Resting in pv and connected: the one state Predbat is allowed to borrow from
    api.state = {"loadpoints": [dict(SAMPLE_STATE["loadpoints"][0], mode=EVCC_MODE_PV, connected=True)]}
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
    check("target_predbat_mode", api.entities["sensor.predbat_evcc_target_mode"]["attributes"]["predbat_mode"], EVCC_MODE_NOW, failures)
    # pv is now owed back, and published so a restart can still hand it over
    check("borrowed", api.restore_mode.get(0), EVCC_MODE_PV, failures)
    check("restore_sensor", api.entities["sensor.predbat_evcc_restore_mode"]["state"], EVCC_MODE_PV, failures)

    # Unchanged and not yet due for a refresh -> no second write
    api.state["loadpoints"][0]["mode"] = EVCC_MODE_NOW
    run_async(api.apply_modes())
    check("no_repeat", len(writes), 1, failures)

    # Past the refresh interval it is re-asserted, because an evcc restart resets the mode
    api.last_write[0] = api.now_utc - timedelta(minutes=20)
    run_async(api.apply_modes())
    check("reasserted", len(writes), 2, failures)
    check("still_borrowed", api.restore_mode.get(0), EVCC_MODE_PV, failures)

    # The slot ends: the borrowed mode goes straight back and nothing is owed any more
    api.entities["sensor.predbat_car_charging_mode"] = {"state": "solar", "attributes": {"reason": "idle"}}
    run_async(api.apply_modes())
    check("handed_back", writes[-1], (1, EVCC_MODE_PV), failures)
    check("nothing_owed", api.restore_mode.get(0), None, failures)
    check("restore_cleared", api.entities["sensor.predbat_evcc_restore_mode"]["state"], "none", failures)

    # Resting again: no further writes
    api.state["loadpoints"][0]["mode"] = EVCC_MODE_PV
    run_async(api.apply_modes())
    check("resting_quiet", len(writes), 3, failures)

    # A user changing the mode mid-takeover stops the writes and abandons the hand-back
    api.entities["sensor.predbat_car_charging_mode"] = {"state": "now", "attributes": {"reason": "grid_slot"}}
    run_async(api.apply_modes())
    check("borrowed_again", writes[-1], (1, EVCC_MODE_NOW), failures)
    api.state["loadpoints"][0]["mode"] = EVCC_MODE_OFF
    run_async(api.apply_modes())
    check("override_no_write", len(writes), 4, failures)
    check("override_sensor", api.entities["binary_sensor.predbat_evcc_override"]["state"], "on", failures)
    check("override_forgets", api.restore_mode.get(0), None, failures)
    return failures


def test_only_borrows_a_resting_loadpoint():
    """Predbat writes only to take a pv loadpoint for a slot or an export, and only until it ends"""
    print("**** Running Test: evcc_takeover ****")
    failures = []

    writes = []

    async def fake_set_mode(loadpoint_id, mode):
        """Record the write instead of performing it."""
        writes.append((loadpoint_id, mode))
        return True

    def build(mode_state, reason, connected=True, observed=EVCC_MODE_PV):
        """Drive apply_modes once for one Predbat decision, returning (component, writes)."""
        del writes[:]
        api = MockEvccAPI(host="http://evcc", control=True, mode_refresh_minutes=15)
        api.state = {"loadpoints": [dict(SAMPLE_STATE["loadpoints"][0], mode=observed, connected=connected)], "vehicles": SAMPLE_STATE["vehicles"]}
        api.state_time = api.now_utc
        api.loadpoint_map = {0: 0}
        api.entities["sensor.predbat_car_charging_mode"] = {"state": mode_state, "attributes": {"reason": reason}}
        api.client.set_mode = fake_set_mode
        run_async(api.apply_modes())
        return api, list(writes)

    # No car on the loadpoint: evcc's own reset-on-disconnect mode stands, whatever Predbat rests at
    api, done = build("solar", "idle", connected=False)
    check("empty_no_write", done, [], failures)
    target = api.entities["sensor.predbat_evcc_target_mode"]
    check("empty_reason", target["attributes"]["reason"], "not_connected", failures)
    check("empty_not_allowed", target["attributes"]["allowed"], False, failures)
    # The sensor shows the mode that actually applies - evcc's own - not the one Predbat rests at,
    # which stays visible in predbat_mode
    check("empty_shows_evcc", target["state"], EVCC_MODE_PV, failures)
    check("empty_predbat_mode", target["attributes"]["predbat_mode"], EVCC_MODE_PV, failures)

    # The two decisions evcc cannot reach on its own, from either resting mode
    for observed in (EVCC_MODE_PV, EVCC_MODE_MINPV):
        _, done = build("now", "grid_slot", observed=observed)
        check("slot_from_{}".format(observed), done, [(1, EVCC_MODE_NOW)], failures)
        _, done = build("off", "export_better", observed=observed)
        check("export_from_{}".format(observed), done, [(1, EVCC_MODE_OFF)], failures)

    # Everything else is evcc's own job and is never written
    for state, reason in (("solar", "idle"), ("solar", "solar"), ("off", "solar_disabled"), ("off", "home_battery_low")):
        api, done = build(state, reason)
        check("quiet_{}".format(reason), done, [], failures)
        check("quiet_reason_{}".format(reason), api.entities["sensor.predbat_evcc_target_mode"]["attributes"]["reason"], "left_to_evcc", failures)
        check("quiet_state_{}".format(reason), api.entities["sensor.predbat_evcc_target_mode"]["state"], EVCC_MODE_PV, failures)

    # A loadpoint the user has set to off or now is a deliberate choice, so even a slot leaves it alone
    for observed in (EVCC_MODE_OFF, EVCC_MODE_NOW):
        api, done = build("now", "grid_slot", observed=observed)
        check("not_resting_{}".format(observed), done, [], failures)
        target = api.entities["sensor.predbat_evcc_target_mode"]
        check("not_resting_reason_{}".format(observed), target["attributes"]["reason"], "not_resting", failures)
        # The state follows evcc, while the refused takeover stays readable in the attributes
        check("not_resting_state_{}".format(observed), target["state"], observed, failures)
        check("not_resting_wanted_{}".format(observed), target["attributes"]["predbat_mode"], EVCC_MODE_NOW, failures)

    # evcc resetting the mode itself on disconnect must not read as somebody overruling Predbat
    api = MockEvccAPI(host="http://evcc", control=True, override_minutes=60)
    api.state = {"loadpoints": [dict(SAMPLE_STATE["loadpoints"][0], mode=EVCC_MODE_PV, connected=True)]}
    api.state_time = api.now_utc
    api.loadpoint_map = {0: 0}
    api.entities["sensor.predbat_car_charging_mode"] = {"state": "now", "attributes": {"reason": "grid_slot"}}
    del writes[:]
    api.client.set_mode = fake_set_mode
    run_async(api.apply_modes())
    check("session_wrote", writes, [(1, EVCC_MODE_NOW)], failures)

    # Unplug: evcc puts the loadpoint back to its own default and nothing is owed back any more
    api.state["loadpoints"][0].update(connected=False, mode=EVCC_MODE_OFF)
    run_async(api.apply_modes())
    check("reset_no_override", api.entities["binary_sensor.predbat_evcc_override"]["state"], "off", failures)
    check("reset_no_write", len(writes), 1, failures)
    check("reset_forgets", api.restore_mode.get(0), None, failures)

    # A new session resting in pv borrows again straight away
    api.state["loadpoints"][0].update(connected=True, mode=EVCC_MODE_PV)
    run_async(api.apply_modes())
    check("new_session_writes", len(writes), 2, failures)

    # A restart mid-takeover recovers what is owed from the sensor it published
    restarted = MockEvccAPI(host="http://evcc", control=True)
    restarted.state = {"loadpoints": [dict(SAMPLE_STATE["loadpoints"][0], mode=EVCC_MODE_NOW, connected=True)]}
    restarted.state_time = restarted.now_utc
    restarted.loadpoint_map = {0: 0}
    restarted.entities["sensor.predbat_evcc_restore_mode"] = {"state": EVCC_MODE_PV, "attributes": {}}
    restarted.entities["sensor.predbat_car_charging_mode"] = {"state": "solar", "attributes": {"reason": "idle"}}
    del writes[:]
    restarted.client.set_mode = fake_set_mode
    run_async(restarted.apply_modes())
    check("restart_hands_back", writes, [(1, EVCC_MODE_PV)], failures)

    # The vehicle's own default mode is published, so a mode Predbat never wrote is not blamed on it
    api = MockEvccAPI(host="http://evcc")
    state = copy.deepcopy(SAMPLE_STATE)
    state["vehicles"]["db:3"]["mode"] = EVCC_MODE_PV
    api.state = state
    api.loadpoint_map = {0: 0}
    api.publish_car(0, state["loadpoints"][0])
    check("vehicle_default", api.entities["sensor.predbat_evcc_mode"]["attributes"]["vehicle_default_mode"], EVCC_MODE_PV, failures)
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

    # The two user switches are Predbat config items, which is what puts them on the config page
    config_index = {item["name"]: item for item in CONFIG_ITEMS}
    check("config_item_guest_hold", config_index.get("evcc_guest_hold", {}).get("type"), "switch", failures)
    check("config_default_guest_hold", config_index.get("evcc_guest_hold", {}).get("default"), False, failures)

    # Control is per loadpoint - one switch per car, so a heat pump on another loadpoint can be
    # left to evcc while the car charger is driven by Predbat
    for car_n in range(8):
        name = "evcc_control" + ("" if car_n == 0 else "_{}".format(car_n))
        item = config_index.get(name, {})
        check("config_item_" + name, item.get("type"), "switch", failures)
        check("config_default_" + name, item.get("default"), False, failures)
        check("config_enable_" + name, item.get("enable_condition"), "num_cars > {}".format(car_n), failures)
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


def build_state(loadpoint=None, vehicle=None, remove=()):
    """Copy SAMPLE_STATE with the loadpoint and vehicle fields a limit test needs changed."""
    state = copy.deepcopy(SAMPLE_STATE)
    lp = state["loadpoints"][0]
    for key in remove:
        lp.pop(key, None)
    lp.update(loadpoint or {})
    state["vehicles"]["db:3"].update(vehicle or {})
    return state


def publish(state, now=None):
    """Publish one car from a state and hand back the component so its entities can be read."""
    api = MockEvccAPI(host="http://evcc", now=now)
    api.state = state
    api.loadpoint_map = {0: 0}
    api.publish_car(0, state["loadpoints"][0])
    return api


def test_effective_limit_caps_plan():
    """The loadpoint limit caps the plan target - evcc stops there whatever the plan asks for"""
    print("**** Running Test: evcc_effective_limit ****")
    failures = []

    # The reported case: a 100% departure plan with the loadpoint dialled down to 70%. The default
    # clock is 15:54 in Copenhagen on a Tuesday, so the next plan is tomorrow's 07:30 one at 100%
    api = publish(build_state(loadpoint={"effectiveLimitSoc": 70}))
    plan_soc = api.entities["sensor.predbat_evcc_plan_soc"]
    check("capped_state", plan_soc["state"], 70, failures)
    check("capped_flag", plan_soc["attributes"]["capped"], True, failures)
    check("capped_raw_plan", plan_soc["attributes"]["plan_soc"], 100, failures)
    check("capped_limit_attr", plan_soc["attributes"]["effective_limit_soc"], 70, failures)
    # The departure itself is untouched - only the target it charges to moves
    check("capped_plan_time", api.entities["sensor.predbat_evcc_plan_time"]["state"], "07:30:00", failures)
    check("capped_logged", any("capped to the evcc limit of 70" in line for line in api.log_messages), True, failures)

    # A limit above the plan changes nothing. 13:54 local picks the 15:00 plan at 70%
    api = publish(build_state(loadpoint={"effectiveLimitSoc": 90}), now=datetime(2026, 8, 11, 11, 54, tzinfo=timezone.utc))
    check("above_state", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 70, failures)
    check("above_flag", api.entities["sensor.predbat_evcc_plan_soc"]["attributes"]["capped"], False, failures)
    check("above_quiet", any("capped to the evcc limit" in line for line in api.log_messages), False, failures)

    # 0 means "no limit", not "stop now" - the plan target has to survive it
    api = publish(build_state(loadpoint={"effectiveLimitSoc": 0}))
    check("zero_state", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 100, failures)
    check("zero_flag", api.entities["sensor.predbat_evcc_plan_soc"]["attributes"]["capped"], False, failures)
    check("zero_solar", api.entities["sensor.predbat_evcc_limit_soc"]["state"], 100, failures)

    # An older evcc without the effective field falls back to the vehicle's own limit
    api = publish(build_state(vehicle={"limitSoc": 80}, remove=("effectiveLimitSoc",)))
    check("fallback_state", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 80, failures)
    check("fallback_solar", api.entities["sensor.predbat_evcc_limit_soc"]["state"], 80, failures)

    # ...and with nothing to go on at all, neither sensor invents a limit below 100
    api = publish(build_state(remove=("effectiveLimitSoc", "limitSoc")))
    check("none_state", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 100, failures)
    check("none_solar", api.entities["sensor.predbat_evcc_limit_soc"]["state"], 100, failures)

    # evcc's effectiveLimitSoc already resolves the session limit over the vehicle's standing one,
    # so it wins - reading the vehicle first would report 100 for a loadpoint that stops at 70
    api = publish(build_state(loadpoint={"effectiveLimitSoc": 70}, vehicle={"limitSoc": 100}))
    check("precedence_plan", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 70, failures)
    check("precedence_solar", api.entities["sensor.predbat_evcc_limit_soc"]["state"], 70, failures)

    # With no plan at all the limit applies on its own, as it always has
    state = build_state(loadpoint={"effectiveLimitSoc": 70})
    state["vehicles"]["db:3"]["repeatingPlans"] = None
    api = publish(state)
    check("no_plan_time", api.entities["sensor.predbat_evcc_plan_time"]["state"], "off", failures)
    check("no_plan_state", api.entities["sensor.predbat_evcc_plan_soc"]["state"], 70, failures)
    check("no_plan_flag", api.entities["sensor.predbat_evcc_plan_soc"]["attributes"]["capped"], False, failures)

    # The log line is once per transition, not once per poll
    api = MockEvccAPI(host="http://evcc")
    capped_state = build_state(loadpoint={"effectiveLimitSoc": 70})
    api.state = capped_state
    api.loadpoint_map = {0: 0}
    api.publish_car(0, capped_state["loadpoints"][0])
    api.publish_car(0, capped_state["loadpoints"][0])
    check("logged_once", len([line for line in api.log_messages if "capped to the evcc limit" in line]), 1, failures)
    # ...and raising the limit back up says so, so the log explains both directions
    open_state = build_state(loadpoint={"effectiveLimitSoc": 100})
    api.state = open_state
    api.publish_car(0, open_state["loadpoints"][0])
    check("uncapped_logged", any("no longer capped" in line for line in api.log_messages), True, failures)
    return failures


def test_only_plans_for_a_known_car():
    """A car evcc cannot identify is somebody else's charge, and is never planned for"""
    print("**** Running Test: evcc_known_car ****")
    failures = []

    def publish_with(connected, vehicle_name=""):
        """Publish one car with the loadpoint in a given connection state."""
        state = copy.deepcopy(SAMPLE_STATE)
        state["loadpoints"][0].update(connected=connected, vehicleName=vehicle_name)
        api = MockEvccAPI(host="http://evcc", automatic=True)
        api.state = state
        api.loadpoint_map = {0: 0}
        api.publish_car(0, state["loadpoints"][0])
        return api, state

    # Disconnected with one configured vehicle: still resolved, so the departure plan stays readable
    api, state = publish_with(connected=False)
    check("away_resolves", api.vehicle_for(state["loadpoints"][0])[0], "db:3", failures)
    check("away_not_known", api.entities["binary_sensor.predbat_evcc_known_car"]["state"], "off", failures)

    # Connected but unidentified - the single-vehicle shortcut must not claim it
    api, state = publish_with(connected=True)
    check("guest_unresolved", api.vehicle_for(state["loadpoints"][0]), (None, {}), failures)
    check("guest_not_known", api.entities["binary_sensor.predbat_evcc_known_car"]["state"], "off", failures)
    # The cable is still reported as connected - that is what evcc says, and the mode gate needs it
    check("guest_connected", api.entities["binary_sensor.predbat_evcc_connected"]["state"], "on", failures)
    check("guest_vehicle_attr", api.entities["sensor.predbat_evcc_vehicle"]["attributes"]["known"], False, failures)

    # Connected and identified
    api, state = publish_with(connected=True, vehicle_name="db:3")
    check("known_resolves", api.vehicle_for(state["loadpoints"][0])[0], "db:3", failures)
    check("known_on", api.entities["binary_sensor.predbat_evcc_known_car"]["state"], "on", failures)

    # The plan follows the identified car, not the cable
    api.automatic_config()
    check("planned_wired", api.args["car_charging_planned"], ["binary_sensor.predbat_evcc_known_car"], failures)
    check("plugged_wired", api.args["car_charging_plugged"], ["binary_sensor.predbat_evcc_known_car"], failures)

    # A guest's SoC must not overwrite the remembered SoC of the car the plan is built around
    api = MockEvccAPI(host="http://evcc", soc_max_age_hours=24)
    check("own_soc", api.update_sticky_soc(0, {"connected": True, "vehicleSoc": 55}, {"soc": 55})[0], 55, failures)
    check("guest_ignored", api.update_sticky_soc(0, {"connected": True, "vehicleSoc": 9}, {}, known=False)[0], 55, failures)
    return failures


def test_guest_car_battery_hold():
    """An unidentified car drawing power holds the home battery, but only when the switch is on"""
    print("**** Running Test: evcc_guest_hold ****")
    failures = []

    def publish(connected, charging, vehicle_name=""):
        """Publish one car in a given state and return the component."""
        state = copy.deepcopy(SAMPLE_STATE)
        state["loadpoints"][0].update(connected=connected, charging=charging, vehicleName=vehicle_name)
        api = MockEvccAPI(host="http://evcc")
        api.state = state
        api.loadpoint_map = {0: 0}
        api.publish_car(0, state["loadpoints"][0])
        return api

    # A car evcc could not identify, actually drawing power
    api = publish(connected=True, charging=True)
    check("guest_on", api.entities["binary_sensor.predbat_evcc_guest_charging"]["state"], "on", failures)
    # ...but the hold is opt-in, so nothing is asked of Predbat until the config switch is turned on
    check("switch_defaults_off", api.guest_hold(), False, failures)
    check("base_flag_off", api.base.evcc_guest_charging, False, failures)
    check("hold_published_off", api.entities["binary_sensor.predbat_evcc_guest_hold"]["state"], "off", failures)

    api.guest_hold_enabled = True
    check("holds", api.guest_hold(), True, failures)
    check("base_flag_on", api.base.evcc_guest_charging, True, failures)
    check("hold_published_on", api.entities["binary_sensor.predbat_evcc_guest_hold"]["state"], "on", failures)

    # Every state that is not a guest drawing power leaves the battery alone
    for label, connected, charging, vehicle in (("known", True, True, "db:3"), ("guest_idle", True, False, ""), ("empty", False, False, "")):
        api = publish(connected=connected, charging=charging, vehicle_name=vehicle)
        api.guest_hold_enabled = True
        check("no_hold_{}".format(label), api.guest_hold(), False, failures)
        check("sensor_off_{}".format(label), api.entities["binary_sensor.predbat_evcc_guest_charging"]["state"], "off", failures)

    # Both switches are Predbat config items, so they show up on the config page and survive a
    # restart with the rest of the config rather than being read back off our own entity
    restarted = MockEvccAPI(host="http://evcc", control=False, args={"evcc_guest_hold": True, "evcc_control": True})
    restarted.loadpoint_map = {0: 0}
    restarted.read_switches()
    check("hold_from_config", restarted.guest_hold_enabled, True, failures)
    check("control_from_config", restarted.car_controlled(0), True, failures)

    # Turning one off in Home Assistant just brings the next cycle forward - the value itself
    # comes from the config item on the next read_switches()
    run_async(restarted.switch_event("switch.predbat_evcc_guest_hold", "turn_off"))
    restarted.handle_switch_event(*restarted.queued_events.pop(0))
    check("refresh_forced", restarted.force_refresh, True, failures)
    restarted.args["evcc_guest_hold"] = False
    restarted.read_switches()
    check("switched_off", restarted.guest_hold_enabled, False, failures)

    # An unset config item falls back to what the component was built with
    unset = MockEvccAPI(host="http://evcc", control=True)
    unset.read_switches()
    check("control_default_kept", unset.car_controlled(0), True, failures)
    check("hold_default_off", unset.guest_hold_enabled, False, failures)
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
        test_effective_limit_caps_plan,
        test_sticky_soc,
        test_desired_mode_and_gates,
        test_solar_enabled_switch,
        test_priority_soc,
        test_override_detection,
        test_apply_modes_writes,
        test_only_borrows_a_resting_loadpoint,
        test_only_plans_for_a_known_car,
        test_guest_car_battery_hold,
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

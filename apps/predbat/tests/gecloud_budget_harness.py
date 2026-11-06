# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
GE Cloud API budget harness: one simulated day of a GE Cloud inverter, counting every API call.

GivEnergy is introducing a monthly API call limit, so this measures what Predbat really spends. A real
PredBat and a real GECloudDirect run against FakeGECloud (tests/gecloud_fake_cloud.py) on a simulated
clock. gecloud cold-starts and ge_cloud_automatic wires the inverter, a random scenario is planned once
at midnight, and the plan is then advanced 5 minutes at a time - gecloud's own loop each minute, then
fetch, calculate_plan(recompute=False) and execute_plan - with execute's writes reaching gecloud through
the same HA service events a real install uses.
"""

import contextlib
import copy
import json
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

from gecloud import GECloudDirect
from inverter import Inverter
from tests.gecloud_fake_cloud import CALL_CONTEXT, FakeGECloud
from tests.test_infra import TestHAInterface, reset_inverter, run_async
from tests.test_random_scenarios import RANDOM_TEMPLATE, apply_scenario_to_predbat, expand_load_minutes, expand_pv_forecast

# Inverter entity args the GivTCP template carries. async_automatic_config sets most of them again, but
# any it leaves alone would point execute at a mock entity whose writes never reach the fake cloud.
TEMPLATE_INVERTER_ARGS = [
    "battery_power",
    "battery_rate_max",
    "battery_scaling",
    "battery_temperature",
    "battery_temperature_history",
    "charge_end_time",
    "charge_limit",
    "charge_limit_enable",
    "charge_rate",
    "charge_rate_percent",
    "charge_start_time",
    "discharge_end_time",
    "discharge_rate",
    "discharge_rate_percent",
    "discharge_start_time",
    "discharge_target_soc",
    "export_today",
    "grid_power",
    "idle_end_time",
    "idle_start_time",
    "import_today",
    "inverter_limit",
    "inverter_mode",
    "inverter_time",
    "load_power",
    "load_today",
    "pause_end_time",
    "pause_mode",
    "pause_start_time",
    "pv_power",
    "pv_today",
    "reserve",
    "scheduled_charge_enable",
    "scheduled_discharge_enable",
    "soc_kw",
    "soc_max",
    "soc_percent",
]

# A GivEnergy month, and the monthly budgets the day is measured against
MONTH_DAYS = 30
BUDGETS = (10000, 20000)
STEPS_PER_DAY = 288

# The HA services execute uses on an inverter entity, and the gecloud handler each one reaches
ROUTED_SERVICES = {
    "switch/turn_on": "switch",
    "switch/turn_off": "switch",
    "switch/toggle": "switch",
    "number/set_value": "number",
    "select/select_option": "select",
}


async def no_sleep(*args, **kwargs):
    """Stand-in for asyncio.sleep: gecloud's retry back-off and sweep pacing cost no wall-clock time."""
    return None


class MemoryStorage:
    """An in-memory stand-in for the Storage component, so a test can hand gecloud a settings cache."""

    def __init__(self):
        """Start empty; data is keyed by (module, filename)."""
        self.data = {}

    async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
        """Keep a copy of the data."""
        self.data[(module, filename)] = copy.deepcopy(data)
        return True

    async def load(self, module, filename):
        """Return a copy of what was saved, or None."""
        return copy.deepcopy(self.data.get((module, filename), None))

    async def age(self, module, filename):
        """Saved data is always fresh by file age; gecloud's own read stamps carry the real age."""
        return 0 if (module, filename) in self.data else None


class HarnessGECloud(GECloudDirect):
    """The real GECloudDirect, on the harness's simulated clock and with an optional test storage."""

    sim_now = None
    test_storage = None

    @property
    def now_utc_exact(self):
        """The simulated time, in place of the wall clock."""
        return self.sim_now

    @property
    def storage(self):
        """The test's storage, if it gave one; otherwise none, as a fresh install without a cache."""
        return self.test_storage

    async def read_inverter_settings(self, serial, previous=None, keys=None, first=False):
        """The real register read, with gecloud's own reason for it added to CALL_CONTEXT - the per-register reads run in tasks, which inherit it."""
        token = CALL_CONTEXT.set("{} | sweep: {}".format(CALL_CONTEXT.get(), self.settings_read_reason))
        try:
            return await super().read_inverter_settings(serial, previous=previous, keys=keys, first=first)
        finally:
            CALL_CONTEXT.reset(token)


class HarnessHAInterface(TestHAInterface):
    """TestHAInterface that delivers service calls on gecloud entities to gecloud's event handlers, as HA would."""

    gecloud = None

    def call_service(self, service, **kwargs):
        """Route an inverter write on a gecloud entity to gecloud, labelling the calls it makes; anything else as before."""
        entity_id = kwargs.get("entity_id", None)
        domain = service.split("/")[0]
        if self.gecloud and service in ROUTED_SERVICES and isinstance(entity_id, str) and entity_id.startswith("{}.{}_gecloud_".format(domain, self.gecloud.prefix)):
            old = self.dummy_items.get(entity_id, None)
            if isinstance(old, dict):
                old = old.get("state", None)
            if domain == "switch":
                new = service.split("/")[1]
                coroutine = self.gecloud.switch_event(entity_id, new)
            elif domain == "number":
                new = kwargs.get("value", None)
                coroutine = self.gecloud.number_event(entity_id, new)
            else:
                new = kwargs.get("option", None)
                coroutine = self.gecloud.select_event(entity_id, new)
            token = CALL_CONTEXT.set("execute {} {}->{}".format(entity_id, old, new))
            try:
                run_async(coroutine)
            finally:
                CALL_CONTEXT.reset(token)
            return True
        return super().call_service(service, **kwargs)


def reset_config_to_defaults(my_predbat):
    """Put every setting back to its default, so the simulated user is a default GE Cloud user and not the template's."""
    for item in my_predbat.CONFIG_ITEMS:
        if "default" in item:
            my_predbat.expose_config(item["name"], item["default"])
    my_predbat.fetch_config_options()


@dataclass
class DayResult:
    """What one scenario's simulated day cost."""

    scenario_id: int
    startup_calls: list = field(default_factory=list)
    day_calls: list = field(default_factory=list)
    register_count: int = 0
    steps: int = 0
    recomputes: int = 0


class GECloudBudgetHarness:
    """One scenario's simulated day against the fake GE Cloud."""

    def __init__(self, scenario, template_path=RANDOM_TEMPLATE, storage=None, extra_args=None):
        """scenario is one entry from load_scenarios(); template_path is the debug dump the random scenarios build on; storage and extra_args (apps.yaml) are optional."""
        self.scenario = scenario
        self.template_path = template_path
        self.test_storage = storage
        self.extra_args = extra_args or {}
        self.my_predbat = None
        self.gecloud = None
        self.fake = None
        self.midnight = None
        self.startup_calls = []
        self.recompute_log = []
        self.steps = 0
        self.recomputes = 0
        self.plan_stamp = None
        self.planned_soc_percent = None

    def sim_time(self, minute):
        """The simulated time minute minutes after the day's midnight."""
        return self.midnight + timedelta(minutes=minute)

    def capture_recompute_logs(self):
        """Keep every log line that mentions a recompute, so a test can show the plan was only ever advanced."""
        original_log = self.my_predbat.log

        def log(message, *args, **kwargs):
            """Pass the line on, keeping it if it is about recomputing the plan."""
            if "recompute" in str(message).lower():
                self.recompute_log.append(str(message))
            return original_log(message, *args, **kwargs)

        self.my_predbat.log = log

    def record_plan(self):
        """After a plan or advance: count a new optimisation if the plan was re-made, and keep the SOC it predicts 5 minutes on."""
        stamp = self.my_predbat.plan_last_updated_minutes
        if stamp != self.plan_stamp:
            self.recomputes += 1
            self.plan_stamp = stamp
        predicted_kwh = self.my_predbat.predict_soc_best.get(5, self.my_predbat.soc_kw)
        self.planned_soc_percent = predicted_kwh / self.my_predbat.soc_max * 100 if self.my_predbat.soc_max else self.fake.soc_percent

    def next_soc_percent(self):
        """The SOC the inverter reports for the next step. Ideal mode: exactly what the plan predicted (the SOC-drift follow-on overrides this)."""
        return self.planned_soc_percent

    def advance_meter(self):
        """Move the meter counters on by one 5-minute share of the scenario's daily load and PV; they only need to move."""
        load_kwh = self.scenario["params"]["load"]["daily_kwh"] / STEPS_PER_DAY
        pv_kwh = sum(self.scenario["data"]["pv_day_kw"]) / 60.0 / STEPS_PER_DAY
        for counters in (self.fake.meter_today, self.fake.meter_total):
            counters["consumption"] += load_kwh
            counters["grid"]["import"] += load_kwh
            counters["solar"] += pv_kwh

    def apply_scenario_state(self, minute):
        """
        After each fetch: keep the plan modelling the scenario's system, with its load history and PV running from now.

        fetch_inverter_data() rebuilds the inverter limits from what gecloud publishes, which knows nothing of the
        scenario's export or inverter limits, and the load history the scenario was expanded with is "minutes ago"
        from midnight. Without this the advanced plan would predict for a different system and for the 00:00 load.
        """
        my_predbat = self.my_predbat
        params = self.scenario["params"]
        data = self.scenario["data"]
        my_predbat.battery_rate_max_charge = params["battery"]["rate_max_charge_kw"] / 60.0
        my_predbat.battery_rate_max_discharge = params["battery"]["rate_max_discharge_kw"] / 60.0
        my_predbat.inverter_hybrid = params["inverter"]["hybrid"]
        my_predbat.inverter_limit = params["inverter"]["inverter_limit_kw"] / 60.0
        my_predbat.export_limit = params["inverter"]["export_limit_kw"] / 60.0
        my_predbat.inverter_support_feedin_first = params["inverter"].get("support_feedin_first", False)
        my_predbat.load_minutes = expand_load_minutes(data["load_day_kwh"], minutes_now=minute)
        my_predbat.pv_forecast_minute, my_predbat.pv_forecast_minute10, my_predbat.pv_forecast_minute90 = expand_pv_forecast(data["pv_day_kw"], my_predbat.forecast_minutes, minutes_now=minute)

    def run_labelled(self, context, function, *args):
        """Call function(*args) with CALL_CONTEXT set, so any API call it makes carries the label."""
        token = CALL_CONTEXT.set(context)
        try:
            return function(*args)
        finally:
            CALL_CONTEXT.reset(token)

    def poll(self, first_minute, last_minute):
        """gecloud's own loop once per simulated minute, as component_base runs it every 60 seconds."""
        for loop_minute in range(first_minute, last_minute + 1):
            self.gecloud.sim_now = self.sim_time(loop_minute)
            self.run_labelled("poll seconds={}".format(60 * loop_minute), lambda seconds: run_async(self.gecloud.run(seconds, False)), 60 * loop_minute)

    def step(self, minute):
        """One 5-minute Predbat cycle ending at minute: gecloud's loop each minute, then fetch, advance the plan, execute."""
        self.fake.soc_percent = self.next_soc_percent()
        self.advance_meter()
        self.poll(minute - 4, minute)
        my_predbat = self.my_predbat
        my_predbat.update_time(print=False, now=self.sim_time(minute))
        self.run_labelled("fetch", my_predbat.fetch_inverter_data)
        self.apply_scenario_state(minute)
        # recompute=False drops charge/export windows that have ended, as a real cycle that does not re-plan does
        self.run_labelled("plan advance", my_predbat.calculate_plan, False)
        self.record_plan()
        self.run_labelled("execute {}".format(self.sim_time(minute).strftime("%H:%M")), my_predbat.execute_plan)
        self.steps += 1

    def simulate_day(self, last_minute=STEPS_PER_DAY * 5 - 5, poll_until=STEPS_PER_DAY * 5):
        """
        Start up at midnight, then step every 5 minutes up to last_minute (23:55 by default).

        gecloud's own loop carries on alone to poll_until (24:00 by default), so the day counts a whole 24
        hours of polling - including the midnight sweep, site and account refresh - while the plan itself
        never crosses midnight.
        """
        self.startup()
        with self.patched():
            for minute in range(5, last_minute + 1, 5):
                self.step(minute)
            self.poll(last_minute + 1, poll_until)
        return DayResult(
            scenario_id=self.scenario["id"],
            startup_calls=self.startup_calls,
            day_calls=list(self.fake.calls),
            register_count=self.fake.register_count(),
            steps=self.steps,
            recomputes=self.recomputes,
        )

    @contextlib.contextmanager
    def patched(self):
        """Route aiohttp to the fake cloud and take every sleep out, for as long as the block runs."""
        with patch("aiohttp.ClientSession", self.fake.session_factory()), patch("asyncio.sleep", no_sleep), patch.object(Inverter, "sleep", lambda inverter, seconds: None):
            yield

    def startup(self):
        """Build Predbat and gecloud, cold-start gecloud against the fake, then plan and execute once at midnight."""
        # Imported here: unit_test imports this module's tests, so a module level import would be circular
        from unit_test import create_predbat

        my_predbat = create_predbat()
        ha_interface = HarnessHAInterface()
        ha_interface.dummy_items = my_predbat.ha_interface.dummy_items
        ha_interface.history = getattr(my_predbat.ha_interface, "history", None)
        ha_interface.base = my_predbat
        my_predbat.ha_interface = ha_interface
        reset_inverter(my_predbat)
        my_predbat.read_debug_yaml(self.template_path)
        my_predbat.config_root = "./"
        my_predbat.save_restore_dir = "./"
        reset_config_to_defaults(my_predbat)
        for arg in TEMPLATE_INVERTER_ARGS:
            my_predbat.args.pop(arg, None)
        for arg in [arg for arg, value in my_predbat.args.items() if "givtcp" in str(value) or "predbat_GE_0" in str(value)]:
            del my_predbat.args[arg]
        my_predbat.args.update(self.extra_args)
        self.my_predbat = my_predbat
        # Predbat's own clock follows the simulated one too, or the inverter clock check sees a skew of years
        self.midnight = my_predbat.midnight_utc
        my_predbat.update_time(print=False, now=self.midnight)

        battery = self.scenario["params"]["battery"]
        # One symmetric rate, as real GivEnergy hardware reports (max_charge_rate == max_discharge_rate): execute writes the
        # discharge rate from the charge-rate sensor, so a narrower discharge register would clamp every write and retry it
        # ten times a cycle. The plan still models the scenario's own charge and discharge rates - see apply_scenario_state().
        rate_w = int(max(battery["rate_max_charge_kw"], battery["rate_max_discharge_kw"]) * 1000)
        self.fake = FakeGECloud(battery_kwh=battery["soc_max_kwh"], rate_w=rate_w, clock=lambda: self.gecloud.sim_now)
        self.fake.scenario_id = self.scenario["id"]
        self.fake.soc_percent = battery["initial_soc_percent"]
        # The apps.yaml settings components.py would pass to the component
        self.gecloud = HarnessGECloud(my_predbat, ge_cloud_direct=True, api_key="harness-key", automatic=True, poll_settings=my_predbat.args.get("ge_cloud_poll_settings", False))
        self.gecloud.sim_now = self.midnight
        self.gecloud.test_storage = self.test_storage
        ha_interface.gecloud = self.gecloud

        with self.patched():
            token = CALL_CONTEXT.set("startup")
            try:
                run_async(self.gecloud.run(0, True))
            finally:
                CALL_CONTEXT.reset(token)
            self.startup_calls = list(self.fake.calls)
            self.fake.calls.clear()

            my_predbat.inverters = []
            my_predbat.fetch_inverter_data()

            scenario = copy.deepcopy(self.scenario)
            scenario["params"]["clock"] = {"minutes_now": 0}
            apply_scenario_to_predbat(my_predbat, scenario)
            my_predbat.args["threads"] = 0
            my_predbat.calculate_plan(recompute=True)
            self.record_plan()
            # Only from here: the midnight optimisation itself logs a start-of-day recompute (the template's stamp is later than 00:00)
            self.capture_recompute_logs()
            self.run_labelled("execute 00:00", my_predbat.execute_plan)
            self.steps = 1


def count_by(records, key):
    """Count records by an attribute, largest first."""
    counts = {}
    for record in records:
        value = getattr(record, key)
        counts[value] = counts.get(value, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))


def budget_text(month_total):
    """The month total set against each budget, e.g. '3600/month = 36.0% of 10000, 18.0% of 20000'."""
    shares = ", ".join("{:.1f}% of {}".format(month_total * 100.0 / budget, budget) for budget in BUDGETS)
    return "{}/month = {}".format(month_total, shares)


def summarise(results):
    """A plain-text report of what each scenario's day cost, and the worst case."""
    lines = ["GE Cloud API budget - simulated day 00:00-23:55, one GE Cloud inverter, default settings"]
    for result in results:
        day_total = len(result.day_calls)
        lines.append("")
        lines.append("Scenario {}: {} executes, plan optimised {} time(s), {} readable registers".format(result.scenario_id, result.steps, result.recomputes, result.register_count))
        lines.append("  Start-up (each restart): {} calls".format(len(result.startup_calls)))
        lines.append("  Steady-state day: {} calls, {}".format(day_total, budget_text(day_total * MONTH_DAYS)))
        for category, count in count_by(result.day_calls, "category"):
            lines.append("    {:<16} {:>6}".format(category, count))
        writes = [record for record in result.day_calls if record.category == "register_write"]
        lines.append("  Writes per day: {}".format(len(writes)))
        for name, count in count_by(writes, "setting_name"):
            lines.append("    {:<40} {:>4}".format(name, count))
    if results:
        worst = max(results, key=lambda result: len(result.day_calls))
        lines.append("")
        lines.append("Worst case: scenario {} with {} calls/day, {}".format(worst.scenario_id, len(worst.day_calls), budget_text(len(worst.day_calls) * MONTH_DAYS)))
    lines.append("SOC follows the plan exactly (ideal mode): write counts are a floor.")
    lines.append("GECloudData history download: not simulated (not used).")
    return "\n".join(lines)


def write_call_log(results, path):
    """Write every call, start-up first, as JSON lines."""
    with open(path, "w") as file_handle:
        for result in results:
            for record in result.startup_calls + result.day_calls:
                file_handle.write(json.dumps(record.to_dict()) + "\n")

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
Teslemetry hold harness: a real PredBat and a real TeslemetryAPI against a fake Powerwall, on a simulated clock.

The same shape as the GE Cloud API budget harness (tests/gecloud_budget_harness.py), with the shared pieces in
tests/component_sim_harness.py. teslemetry cold-starts against FakeTeslemetry (tests/teslemetry_fake_cloud.py)
and wires the TESLA inverter through automatic_config; then each simulated minute the battery runs on and
teslemetry's own loop polls and asserts, and every 5 minutes Predbat fetches and executes, its writes reaching
teslemetry through the same HA service events a real install uses.

The plan is scripted rather than optimised - a list of charge windows with their limits - so a scenario can
put a freeze exactly where the question is, and what it measures is how the battery follows the commands.
"""

import contextlib
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import patch

from inverter import Inverter
from teslemetry import TeslemetryAPI
from tests.component_sim_harness import ComponentRoutingHAInterface, MemoryStorage, no_sleep, reset_config_to_defaults
from tests.gecloud_budget_harness import TEMPLATE_INVERTER_ARGS
from tests.teslemetry_fake_cloud import FakeTeslemetry
from tests.test_infra import reset_inverter, run_async
from tests.test_random_scenarios import RANDOM_TEMPLATE


class HarnessTeslemetry(TeslemetryAPI):
    """The real TeslemetryAPI, on the harness's simulated clock and with an in-memory storage."""

    sim_now = None
    test_storage = None

    def get_minutes_now(self):
        """Minutes since midnight on the simulated clock, in place of the wall clock."""
        return self.sim_now.hour * 60 + self.sim_now.minute

    @property
    def storage(self):
        """The harness's storage, where the committed schedule is kept."""
        return self.test_storage


class TeslemetryHAInterface(ComponentRoutingHAInterface):
    """ComponentRoutingHAInterface for teslemetry's virtual entities."""

    entity_marker = "teslemetry"


class NoComponents:
    """Stands in for Predbat's component registry, which a test build leaves unset.

    automatic_config writes Predbat's own inverter_hybrid switch through set_state_external, and the user
    interface then offers the event to every component; the harness's one component is driven directly.
    """

    def get_component(self, name):
        """No other component is running."""
        return None

    async def switch_event(self, entity_id, service):
        """No other component to tell."""

    async def number_event(self, entity_id, value):
        """No other component to tell."""

    async def select_event(self, entity_id, value):
        """No other component to tell."""


@dataclass
class PlanWindow:
    """One scripted charge window: start and end in minutes from midnight, and its target in percent (None for a freeze)."""

    start: int
    end: int
    target_percent: object = None


@dataclass
class HoldResult:
    """What a simulated run did, over the window the scenario asked about."""

    trace: list = field(default_factory=list)
    band_writes: list = field(default_factory=list)
    reserve_writes: list = field(default_factory=list)
    grid_charge_kwh: float = 0.0

    @property
    def soc_min(self):
        """The lowest SoC in the window."""
        return min(row.soc_percent for row in self.trace)

    @property
    def soc_max(self):
        """The highest SoC in the window."""
        return max(row.soc_percent for row in self.trace)


class TeslemetryHoldHarness:
    """One scripted run of a TESLA inverter driven by teslemetry against the fake Powerwall."""

    def __init__(self, soc_percent, windows, reserve=4, capacity_kwh=13.5, load_w=None, pv_w=None, tbc_control=True, extra_args=None):
        """soc_percent is the battery at the start, windows the scripted PlanWindow list, reserve Predbat's set_reserve_min."""
        self.initial_soc_percent = soc_percent
        self.windows = windows
        self.reserve_min = reserve
        self.capacity_kwh = capacity_kwh
        self.load_w = load_w
        self.pv_w = pv_w
        self.tbc_control = tbc_control
        self.extra_args = extra_args or {}
        self.my_predbat = None
        self.teslemetry = None
        self.fake = None
        self.midnight = None
        self.statuses = []

    def sim_time(self, minute):
        """The simulated time minute minutes after the day's midnight."""
        return self.midnight + timedelta(minutes=minute)

    @contextlib.contextmanager
    def patched(self):
        """Route aiohttp to the fake and take every sleep out, for as long as the block runs."""
        with patch("aiohttp.ClientSession", self.fake.session_factory()), patch("asyncio.sleep", no_sleep), patch.object(Inverter, "sleep", lambda inverter, seconds: None):
            yield

    def poll(self, minute):
        """One minute: the battery runs on at its settings, then teslemetry's own loop runs as component_base does every 60 seconds."""
        self.teslemetry.sim_now = self.sim_time(minute)
        self.fake.advance(1)
        run_async(self.teslemetry.run(60 * minute, False))

    def apply_plan(self):
        """Hand execute the scripted windows that have not ended yet, as a plan advanced to now would."""
        my_predbat = self.my_predbat
        windows = [window for window in self.windows if window.end > my_predbat.minutes_now]
        my_predbat.charge_window_best = [{"start": window.start, "end": window.end, "average": 0} for window in windows]
        # A freeze is a charge limit equal to the reserve (execute.is_freeze_charge)
        my_predbat.charge_limit_best = [my_predbat.reserve if window.target_percent is None else window.target_percent * my_predbat.soc_max / 100.0 for window in windows]
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        my_predbat.set_charge_window = True
        my_predbat.set_export_window = True
        my_predbat.set_soc_enable = True
        my_predbat.set_reserve_enable = True
        my_predbat.set_reserve_hold = True

    def step(self, minute):
        """One 5-minute Predbat cycle ending at minute: teslemetry's loop each minute, then fetch, the scripted plan, execute."""
        for loop_minute in range(minute - 4, minute + 1):
            self.poll(loop_minute)
        my_predbat = self.my_predbat
        my_predbat.update_time(print=False, now=self.sim_time(minute))
        my_predbat.fetch_inverter_data()
        self.apply_plan()
        status, _ = my_predbat.execute_plan()
        self.statuses.append((self.sim_time(minute), status))

    def startup(self, start_minute):
        """Build Predbat and teslemetry, cold-start teslemetry against the fake, then execute once at start_minute."""
        # Imported here: unit_test imports this module's tests, so a module level import would be circular
        from unit_test import create_predbat

        my_predbat = create_predbat()
        ha_interface = TeslemetryHAInterface()
        ha_interface.dummy_items = my_predbat.ha_interface.dummy_items
        ha_interface.history = my_predbat.ha_interface.history
        ha_interface.base = my_predbat
        my_predbat.ha_interface = ha_interface
        reset_inverter(my_predbat)
        my_predbat.read_debug_yaml(RANDOM_TEMPLATE)
        my_predbat.config_root = "./"
        my_predbat.save_restore_dir = "./"
        reset_config_to_defaults(my_predbat)
        my_predbat.expose_config("set_reserve_min", self.reserve_min)
        for arg in TEMPLATE_INVERTER_ARGS:
            my_predbat.args.pop(arg, None)
        for arg in [arg for arg, value in my_predbat.args.items() if "givtcp" in str(value) or "predbat_GE_0" in str(value)]:
            del my_predbat.args[arg]
        my_predbat.args.update(self.extra_args)
        my_predbat.components = NoComponents()
        self.my_predbat = my_predbat
        self.midnight = my_predbat.midnight_utc
        my_predbat.update_time(print=False, now=self.sim_time(start_minute))

        self.fake = FakeTeslemetry(clock=lambda: self.teslemetry.sim_now, capacity_kwh=self.capacity_kwh, soc_percent=self.initial_soc_percent, reserve=self.reserve_min, load_w=self.load_w, pv_w=self.pv_w)
        self.teslemetry = HarnessTeslemetry(my_predbat, key="harness-key", automatic=True, tbc_control=self.tbc_control)
        self.teslemetry.sim_now = self.sim_time(start_minute)
        self.teslemetry.test_storage = MemoryStorage()
        ha_interface.component = self.teslemetry

        with self.patched():
            run_async(self.teslemetry.run(60 * start_minute, True))
            my_predbat.inverters = []
            my_predbat.fetch_inverter_data()
            self.apply_plan()
            status, _ = my_predbat.execute_plan()
            self.statuses.append((self.sim_time(start_minute), status))

    def simulate(self, start_minute, end_minute):
        """Start up at start_minute, then step every 5 minutes up to end_minute; returns the HoldResult for the whole run."""
        self.startup(start_minute)
        with self.patched():
            for minute in range(start_minute + 5, end_minute + 1, 5):
                self.step(minute)
        return self.result(start_minute, end_minute)

    def result(self, from_minute, to_minute):
        """The HoldResult for the trace between two simulated minutes."""
        start, end = self.sim_time(from_minute), self.sim_time(to_minute)
        trace = [row for row in self.fake.trace if start <= row.sim_time < end]
        reserve_writes = [(record.sim_time, int(record.body["backup_reserve_percent"])) for record in self.fake.commands if record.endpoint == "backup" and start <= record.sim_time < end]
        # Grid energy that went into the battery, not into the house
        grid_charge_kwh = sum(min(max(row.grid_w, 0.0), max(-row.battery_w, 0.0)) for row in trace) / 60.0 / 1000.0
        return HoldResult(trace=trace, band_writes=[write for write in self.fake.band_writes if start <= write[0] < end], reserve_writes=reserve_writes, grid_charge_kwh=grid_charge_kwh)

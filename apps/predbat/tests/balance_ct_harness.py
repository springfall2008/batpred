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
One CT, several inverters, second by second: the physics that make paralleled inverters cross-charge.

balance_sim_harness.py shares each minute's load evenly between the batteries - the steady state their control
loops settle on. Real paralleled inverters never quite get there. Every one of them chases the same grid CT, but
each samples it at its own moment and then takes a while to act on what it saw, and the house load moves faster
than that loop. So one inverter is still ramping up to cover a kettle when another, sampling a little later, sees
the overshoot as export and starts charging - from its neighbour. That is cross-charging, and it is the reason
balance_inverters has a cross-charge correction at all.

This module simulates that at one-second resolution:

- The site has one grid CT. Grid power is PV + battery discharge - load (Predbat's sign: +ve export).
- Each inverter samples the CT every sample_period_s seconds, offset by sample_phase_s, and moves its target by
  gain x the error it saw (within deadband_w it leaves the target alone). Its actual output follows the target
  with a first-order lag of response_s seconds. The target is clamped to what the rate registers allow.
- The inverter mode decides the target: "eco" (self-use) chases the CT; "charge" charges at the charge register
  whatever the CT says (a grid charge window); "export" discharges at the discharge register (a forced export).
- Predbat's real 60s loop runs on top - fetch_inverter_data(quick=True) then rebalance_inverter_rates() - and
  reads each battery's instantaneous power exactly as a sensor poll would, so it sees one moment of the hunting,
  not an average.

The load is made of a noisy baseline plus appliances that switch on and off faster than the loop: a kettle, an
oven element cycling 40s on / 20s off, a washing machine heater cycling 15s / 15s. PV can have clouds.
"""

import contextlib
import io
import math
import random
from dataclasses import dataclass, field

from tests.balance_sim_harness import IDEAL, RATE_KINDS, SimInverter, routed_to_sims
from tests.fleet_rig import FleetInverter, entity_suffix, setup_fleet

# Predbat's intent owner for each inverter mode: what execute_plan would have stored for that slot
MODE_OWNER = {"eco": "demand", "charge": "charge", "export": "export"}


@dataclass
class Dynamics:
    """
    How one inverter's control loop chases the CT.
    """

    sample_period_s: float = 5.0
    sample_phase_s: float = 0.0
    response_s: float = 8.0
    deadband_w: float = 25.0
    gain: float = 1.0


def staggered(count, period=5.0, response=8.0):
    """
    Default dynamics for a fleet: the same loop on each inverter, sampling the CT at evenly staggered moments.
    """
    return [Dynamics(sample_period_s=period, sample_phase_s=period * index / count, response_s=response) for index in range(count)]


def mixed(count):
    """
    A fleet whose loops differ, as real mixed fleets do: a fast hybrid, a slower AC-coupled unit, an older one with a
    wide deadband and a low gain. Only the SUM of paralleled CT-chasing loops is pinned to the grid error, so when
    they differ the split between them drifts - which is where sustained cross-charging comes from.
    """
    loops = [
        Dynamics(sample_period_s=3.0, sample_phase_s=0.0, response_s=5.0, deadband_w=20.0, gain=1.0),
        Dynamics(sample_period_s=7.0, sample_phase_s=2.0, response_s=15.0, deadband_w=40.0, gain=0.9),
        Dynamics(sample_period_s=5.0, sample_phase_s=4.0, response_s=10.0, deadband_w=80.0, gain=0.7),
    ]
    return loops[:count]


@dataclass
class House:
    """
    A load and PV profile, sampled once a second.
    """

    base_w: float = 400.0
    noise_w: float = 120.0
    noise_tau_s: float = 20.0
    # (start s, end s, W, on s, off s): an appliance switching on and off between start and end; off 0 is a steady load
    appliances: list = field(default_factory=list)
    pv_w: float = 0.0
    # (start s, end s, fraction of pv_w): clouds
    clouds: list = field(default_factory=list)
    seed: int = 7

    def build(self, seconds):
        """
        Return (load, pv) lists, one value per second.
        """
        rng = random.Random(self.seed)
        load = []
        pv = []
        noise = 0.0
        alpha = 1.0 / self.noise_tau_s
        for t in range(seconds):
            noise += -alpha * noise + self.noise_w * math.sqrt(2 * alpha) * rng.gauss(0.0, 1.0)
            watts = max(self.base_w + noise, 50.0)
            for start, end, power, on_s, off_s in self.appliances:
                if start <= t < end and (off_s == 0 or (t - start) % (on_s + off_s) < on_s):
                    watts += power
            load.append(watts)
            sun = self.pv_w
            for start, end, fraction in self.clouds:
                if start <= t < end:
                    sun = self.pv_w * fraction
            pv.append(sun)
        return load, pv


def busy_evening():
    """
    An evening: kettle, oven element, a microwave, a washing machine heater - all faster than the inverters' loop.
    """
    return House(
        base_w=500.0,
        appliances=[
            (300, 450, 2800.0, 150, 0),  # kettle
            (900, 2400, 2000.0, 40, 20),  # oven element
            (1500, 1590, 1200.0, 90, 0),  # microwave
            (2700, 3300, 2000.0, 15, 15),  # washing machine heater
        ],
    )


def cloudy_midday():
    """
    Midday: 5kW of PV with passing clouds, a light load with the washing machine on.
    """
    return House(
        base_w=350.0,
        appliances=[(600, 1500, 2000.0, 15, 15), (2000, 2150, 2800.0, 150, 0)],
        pv_w=5000.0,
        clouds=[(400, 520, 0.3), (1100, 1160, 0.2), (1800, 2100, 0.45), (2900, 2960, 0.25)],
    )


@dataclass
class CTScenario:
    """
    One run of the CT model: the fleet, its loops, what the house does, the inverter mode and whether Predbat balances.
    """

    name: str
    mode: str
    soc: list
    house: House = field(default_factory=busy_evening)
    dynamics: list = None
    behaviours: list = None
    balance: bool = True
    rate_min: float = 200.0
    soc_max: float = 9.5
    rate_max: float = 3000
    minutes: int = 60
    threshold: float = 1.0
    seed: int = 1

    @property
    def direction(self):
        """The direction balancing works in: discharge for eco with no PV and export, charge otherwise."""
        if self.mode == "export":
            return "discharge"
        if self.mode == "charge":
            return "charge"
        return "charge" if self.house.pv_w > 0 else "discharge"


class CTInverter:
    """
    One inverter's control loop on top of a SimInverter's battery and registers.
    """

    def __init__(self, sim, dynamics):
        """
        Wrap the battery with a control loop starting at rest.
        """
        self.sim = sim
        self.dynamics = dynamics
        self.target = 0.0
        self.power = 0.0

    def limits(self):
        """
        (most it may charge, most it may discharge) in W this second, from the registers and the SoC.
        """
        return self.sim.limit("charge"), self.sim.limit("discharge")

    def control(self, t, ct_import, mode):
        """
        Sample the CT if this is one of the inverter's sampling moments, and set a new target.
        """
        charge_limit, discharge_limit = self.limits()
        if mode == "charge":
            self.target = -charge_limit
        elif mode == "export":
            self.target = discharge_limit
        else:
            dynamics = self.dynamics
            if (t - dynamics.sample_phase_s) % dynamics.sample_period_s < 1.0 and abs(ct_import) > dynamics.deadband_w:
                self.target = self.power + dynamics.gain * ct_import
        self.target = min(max(self.target, -charge_limit), discharge_limit)

    def respond(self):
        """
        Move the output one second towards the target, within what the registers and SoC allow right now.
        """
        charge_limit, discharge_limit = self.limits()
        self.power += (self.target - self.power) * min(1.0 / max(self.dynamics.response_s, 1.0), 1.0)
        self.power = min(max(self.power, -charge_limit), discharge_limit)
        self.sim.battery_power = self.power
        self.sim.soc_kwh = min(max(self.sim.soc_kwh - self.power / 3600.0 / 1000.0, 0.0), self.sim.soc_max)


def run_ct_scenario(my_predbat, scenario):
    """
    Run one CT scenario through Predbat's real 60s rebalance loop. Returns the integrated energy flows and the
    balancing statistics.
    """
    count = len(scenario.soc)
    rng = random.Random(scenario.seed)
    behaviours = scenario.behaviours or [IDEAL] * count
    dynamics = scenario.dynamics or staggered(count)
    fleet = [FleetInverter(scenario.soc_max, rate_max=scenario.rate_max, inverter_limit=max(5000, scenario.rate_max), rate_min=scenario.rate_min) for _ in range(count)]
    sims = [SimInverter(scenario.soc_max, scenario.rate_max, scenario.soc[index], behaviours[index], rng) for index in range(count)]
    loops = [CTInverter(sim, dynamics[index]) for index, sim in enumerate(sims)]
    seconds = scenario.minutes * 60
    load, pv = scenario.house.build(seconds)
    clock = {"s": 0.0}
    ha = my_predbat.ha_interface
    owner = MODE_OWNER[scenario.mode]

    saved = {
        name: getattr(my_predbat, name)
        for name in (
            "balance_inverters_enable",
            "balance_inverters_charge",
            "balance_inverters_discharge",
            "balance_inverters_crosscharge",
            "balance_inverters_threshold_charge",
            "balance_inverters_threshold_discharge",
            "set_read_only",
            "inverter_rate_intent",
            "inverter_balance_overridden",
            "inverters",
        )
    }
    flows = {"import_kwh": 0.0, "export_kwh": 0.0, "cross_kwh": 0.0, "battery_out_kwh": 0.0, "battery_in_kwh": 0.0}
    held = [0] * count
    held_edges = [0] * count
    previous_held = [False] * count
    socs_by_minute = []
    log_lines = []
    original_log = my_predbat.log
    my_predbat.log = log_lines.append
    grid = 0.0
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            setup_fleet(my_predbat, fleet, soc=scenario.soc, charge_rate=scenario.rate_max, discharge_rate=scenario.rate_max)
            for inverter in my_predbat.inverters:
                inverter.sleep = lambda seconds_slept: clock.__setitem__("s", clock["s"] + seconds_slept)
            my_predbat.balance_inverters_enable = scenario.balance
            my_predbat.balance_inverters_charge = True
            my_predbat.balance_inverters_discharge = True
            my_predbat.balance_inverters_crosscharge = True
            my_predbat.balance_inverters_threshold_charge = scenario.threshold
            my_predbat.balance_inverters_threshold_discharge = scenario.threshold
            my_predbat.set_read_only = False
            my_predbat.inverter_rate_intent = {index: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": owner} for index in range(count)}
            my_predbat.inverter_balance_overridden = {}
            with routed_to_sims(ha, sims, clock, scenario.rate_max):
                for t in range(seconds):
                    ct_import = -grid
                    for loop in loops:
                        loop.control(t, ct_import, scenario.mode)
                    for loop in loops:
                        loop.respond()
                    battery = [loop.power for loop in loops]
                    grid = pv[t] + sum(battery) - load[t]
                    flows["import_kwh"] += max(-grid, 0.0) / 3600000.0
                    flows["export_kwh"] += max(grid, 0.0) / 3600000.0
                    out = sum(power for power in battery if power > 0)
                    into = -sum(power for power in battery if power < 0)
                    flows["battery_out_kwh"] += out / 3600000.0
                    flows["battery_in_kwh"] += into / 3600000.0
                    flows["cross_kwh"] += min(out, into) / 3600000.0
                    if t % 60 == 59:
                        clock["s"] = max(clock["s"], float(t))
                        for index, sim in enumerate(sims):
                            suffix = entity_suffix(index)
                            ha.dummy_items["sensor.soc_percent" + suffix] = sim.soc_percent_reported()
                            ha.dummy_items["sensor.battery_power" + suffix] = sim.battery_power_reported()
                            ha.dummy_items["sensor.grid_power" + suffix] = round(grid, 1) if index == 0 else 0
                            ha.dummy_items["sensor.load_power" + suffix] = round(load[t], 1) if index == 0 else 0
                            ha.dummy_items["sensor.pv_power" + suffix] = round(pv[t], 1) if index == 0 else 0
                            ha.dummy_items["number.reserve" + suffix] = sim.reserve_percent
                        my_predbat.fetch_inverter_data(quick=True)
                        my_predbat.rebalance_inverter_rates()
                        for index, sim in enumerate(sims):
                            is_held = any(sim.registers[kind] <= 0 for kind in RATE_KINDS)
                            held[index] += 1 if is_held else 0
                            held_edges[index] += 1 if (is_held and not previous_held[index]) else 0
                            previous_held[index] = is_held
                        socs_by_minute.append([sim.soc_kwh / sim.soc_max * 100.0 for sim in sims])
    finally:
        my_predbat.log = original_log
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    spread = [max(socs) - min(socs) for socs in socs_by_minute]
    return {
        "name": scenario.name,
        "mode": scenario.mode,
        "balance": scenario.balance,
        "inverters": count,
        "import_kwh": round(flows["import_kwh"], 3),
        "export_kwh": round(flows["export_kwh"], 3),
        "cross_kwh": round(flows["cross_kwh"], 3),
        "battery_out_kwh": round(flows["battery_out_kwh"], 3),
        "battery_in_kwh": round(flows["battery_in_kwh"], 3),
        "spread_start": round(max(scenario.soc) - min(scenario.soc), 2),
        "spread_end": round(spread[-1], 2) if spread else None,
        "held_minutes": held,
        "holds": held_edges,
        "writes": sum(sim.writes[kind] for sim in sims for kind in RATE_KINDS),
        # Balancer decisions by branch, from its own log lines: cross-charge holds, and SoC holds
        "cross_holds": sum(1 for line in log_lines if "working against the fleet" in str(line)),
        "soc_holds": sum(1 for line in log_lines if "holding its discharge" in str(line) or "holding its charge" in str(line)),
    }


def ct_matrix():
    """
    Every mode with balancing off and on, for two and three inverters.

    SoCs start spread out in the direction each mode would balance: the emptiest discharging last, the fullest
    charging last.
    """
    scenarios = []
    for count in (2, 3):
        low_high = [80, 70] if count == 2 else [80, 75, 70]
        high_low = [70, 80] if count == 2 else [70, 75, 80]
        modes = [
            ("eco, busy evening", "eco", low_high, busy_evening),
            ("eco, cloudy midday", "eco", high_low, cloudy_midday),
            ("grid charge, busy evening", "charge", [40, 50] if count == 2 else [40, 45, 50], busy_evening),
            ("forced export, busy evening", "export", low_high, busy_evening),
        ]
        for label, mode, soc, house in modes:
            for loops_name, loops in (("same loops", staggered), ("mixed loops", mixed)):
                for balance in (False, True):
                    scenarios.append(CTScenario(name="{} inv, {}, {}, balance {}".format(count, label, loops_name, "on" if balance else "off"), mode=mode, soc=soc, house=house(), dynamics=loops(count), balance=balance))
    return scenarios


def format_ct_report(results):
    """
    One line per CT scenario.
    """
    lines = ["{:<52} {:>7} {:>7} {:>7} {:>6} {:>6} {:>14} {:>6}".format("scenario", "import", "export", "cross", "spread0", "spread1", "holds", "writes")]
    for result in results:
        lines.append(
            "{:<52} {:>7} {:>7} {:>7} {:>6} {:>6} {:>14} {:>6}".format(
                result["name"],
                result["import_kwh"],
                result["export_kwh"],
                result["cross_kwh"],
                result["spread_start"],
                result["spread_end"],
                str(result["holds"]),
                result["writes"],
            )
        )
    return "\n".join(lines)

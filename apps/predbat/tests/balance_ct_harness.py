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
from datetime import timedelta
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
    # How this inverter's own reading of the shared CT differs from the truth: an offset in W (+ve reads more
    # import than there is) and a gain error. Each inverter reads the clamp through its own input, so they never
    # quite agree - and integrating loops that disagree about where zero is cannot all settle: the one that reads
    # import winds up to full discharge and the one that reads export winds down until it is charging from it.
    ct_offset_w: float = 0.0
    ct_gain: float = 1.0


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


def disagreeing(count, offsets=(45.0, -40.0, 15.0), gains=(1.02, 0.98, 1.0)):
    """
    The mixed loops, each reading the shared CT a little differently - tens of watts of offset and a couple of
    percent of gain, which is ordinary for separate current inputs on one clamp.
    """
    loops = mixed(count)
    for index, loop in enumerate(loops):
        loop.ct_offset_w = offsets[index]
        loop.ct_gain = gains[index]
    return loops


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
    # Broken cloud: random dips, each drawn from these (min, max) ranges - clear sky between them, how long the
    # dip lasts, the fraction of PV it takes away, and how long the edge takes to pass. A maximum gap of 0 turns
    # it off. irradiance_noise adds a slow wander on top, as a fraction of pv_w.
    cloud_gap_s: tuple = (0.0, 0.0)
    cloud_duration_s: tuple = (10.0, 90.0)
    cloud_depth: tuple = (0.25, 0.75)
    cloud_ramp_s: tuple = (2.0, 8.0)
    irradiance_noise: float = 0.0
    seed: int = 7

    def sky(self, seconds, rng):
        """
        The fraction of clear-sky PV reaching the panels, one value per second, from the broken-cloud settings.
        """
        sky = [1.0] * seconds
        if self.cloud_gap_s[1] <= 0:
            return sky
        t = rng.uniform(*self.cloud_gap_s)
        while t < seconds:
            duration = rng.uniform(*self.cloud_duration_s)
            depth = rng.uniform(*self.cloud_depth)
            ramp = rng.uniform(*self.cloud_ramp_s)
            for second in range(int(t), min(int(t + duration + 2 * ramp), seconds)):
                into = second - t
                if into < ramp:
                    cover = depth * into / ramp
                elif into < ramp + duration:
                    cover = depth
                else:
                    cover = depth * max(0.0, 1.0 - (into - ramp - duration) / ramp)
                sky[second] = min(sky[second], 1.0 - cover)
            t += duration + 2 * ramp + rng.uniform(*self.cloud_gap_s)
        return sky

    def build(self, seconds):
        """
        Return (load, pv) lists, one value per second.
        """
        rng = random.Random(self.seed)
        sky = self.sky(seconds, random.Random(self.seed + 1))
        load = []
        pv = []
        noise = 0.0
        wander = 0.0
        alpha = 1.0 / self.noise_tau_s
        for t in range(seconds):
            if self.irradiance_noise:
                wander += -wander / 30.0 + self.irradiance_noise * math.sqrt(2 / 30.0) * rng.gauss(0.0, 1.0)
            noise += -alpha * noise + self.noise_w * math.sqrt(2 * alpha) * rng.gauss(0.0, 1.0)
            watts = max(self.base_w + noise, 50.0)
            for start, end, power, on_s, off_s in self.appliances:
                if start <= t < end and (off_s == 0 or (t - start) % (on_s + off_s) < on_s):
                    watts += power
            load.append(watts)
            sun = self.pv_w * max(sky[t] + wander, 0.0)
            for start, end, fraction in self.clouds:
                if start <= t < end:
                    sun = min(sun, self.pv_w * fraction)
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


def broken_cloud_midday(pv_w=5000.0, base_w=350.0):
    """
    Midday under broken cumulus: a dip of a quarter to three quarters of the PV every 10-90 seconds, each lasting
    10-90 seconds with edges of a few seconds, and irradiance wandering on top - faster than the inverters' loops
    and far faster than Predbat's 60s pass, so the fleet keeps flipping between soaking up a surplus and covering
    a deficit. A light load with the washing machine on.
    """
    return House(
        base_w=base_w,
        appliances=[(600, 1500, 2000.0, 15, 15), (2000, 2150, 2800.0, 150, 0)],
        pv_w=pv_w,
        cloud_gap_s=(10.0, 90.0),
        irradiance_noise=0.03,
        seed=11,
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
    # The three balancing switches, applied when balance is on. All on by default here; a default install has
    # only the cross-charge correction on (switch.predbat_balance_inverters_crosscharge), with charge and
    # discharge SoC balancing off.
    balance_charge: bool = True
    balance_discharge: bool = True
    balance_crosscharge: bool = True
    rate_min: float = 200.0
    soc_max: float = 9.5
    rate_max: float = 3000
    minutes: int = 60
    threshold: float = 1.0
    seed: int = 1
    # The share of the house PV on each inverter's own DC side, or None for PV that is AC-coupled at the site.
    # A hybrid's own PV goes through its own AC output, which is capped at ac_limit_w: whatever will not fit has
    # to go into its battery, or is clipped - which is what a charge hold on that inverter can cost.
    pv_split: list = None
    ac_limit_w: float = 5000.0
    # For mode "export": Predbat's planned export limit, packed as the plan stores it - 20.0 exports to 20% at full
    # power, 20.5 to 20% at half power. Every 5 minutes, as the plan does, the executor's real
    # allocate_fleet_export_rates() splits that power between the inverters by how much each still has to shed,
    # and sets each one's own target; the 60s rebalance then balances on top of those claimed rates. None exports
    # at whatever the registers allow with nothing claimed.
    export_limit: float = None

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

    def __init__(self, sim, dynamics, pv_share=0.0, ac_limit_w=5000.0):
        """
        Wrap the battery with a control loop starting at rest. pv_share is the fraction of the house PV on this
        inverter's own DC side, and ac_limit_w caps its AC output.
        """
        self.sim = sim
        self.dynamics = dynamics
        self.pv_share = pv_share
        self.ac_limit_w = ac_limit_w
        self.target = 0.0
        self.power = 0.0
        self.pv_w = 0.0
        # A forced export stops at this SoC (kWh); 0 exports down to the reserve
        self.export_target_kwh = 0.0

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
            self.target = discharge_limit if self.sim.soc_kwh > self.export_target_kwh else 0.0
        else:
            dynamics = self.dynamics
            seen = ct_import * dynamics.ct_gain + dynamics.ct_offset_w
            if (t - dynamics.sample_phase_s) % dynamics.sample_period_s < 1.0 and abs(seen) > dynamics.deadband_w:
                self.target = self.power + dynamics.gain * seen
        self.target = min(max(self.target, -charge_limit), discharge_limit)

    def respond(self, pv_total=0.0):
        """
        Move the battery one second towards the target, within what the registers and SoC allow right now, and
        return (AC output, PV clipped) in W.

        The inverter's own PV and its battery share one AC output capped at ac_limit_w. When its PV alone would
        exceed that, the battery has to take the excess whatever the loop wants - and if its charge is held, the
        excess is clipped.
        """
        charge_limit, discharge_limit = self.limits()
        self.pv_w = pv_total * self.pv_share
        self.power += (self.target - self.power) * min(1.0 / max(self.dynamics.response_s, 1.0), 1.0)
        self.power = min(max(self.power, -charge_limit), discharge_limit)
        # The AC output may not exceed the limit in either direction
        self.power = min(self.power, max(self.ac_limit_w - self.pv_w, -charge_limit))
        self.power = max(self.power, -self.ac_limit_w - self.pv_w)
        ac_output = self.pv_w + self.power
        clipped = max(ac_output - self.ac_limit_w, 0.0)
        ac_output -= clipped
        self.sim.battery_power = self.power
        self.sim.soc_kwh = min(max(self.sim.soc_kwh - self.power / 3600.0 / 1000.0, 0.0), self.sim.soc_max)
        return ac_output, clipped


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
    pv_split = scenario.pv_split or [0.0] * count
    site_pv_share = max(1.0 - sum(pv_split), 0.0)
    loops = [CTInverter(sim, dynamics[index], pv_share=pv_split[index], ac_limit_w=scenario.ac_limit_w) for index, sim in enumerate(sims)]
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
            "balance_held_previous",
            "balance_ceilings_previous",
            "balance_last_decisions",
            "balance_events",
            "balance_counts",
            "balance_restored",
            "inverters",
            "now_utc",
            "export_limits_best",
            "set_export_window",
            "set_export_low_power",
        )
    }
    balance_sensor = None
    flows = {"import_kwh": 0.0, "export_kwh": 0.0, "cross_kwh": 0.0, "battery_out_kwh": 0.0, "battery_in_kwh": 0.0, "clipped_kwh": 0.0}
    # Instantaneous cross-charging: the worst second, and how long it ran above 1kW and 2kW - a fleet stuck with
    # one battery charging hard from another looks very different from small hunting, though both add up to kWh
    cross_peak = {"w": 0.0, "t": 0, "powers": []}
    cross_over = {1000: 0, 2000: 0}
    power_trace = []
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
            my_predbat.balance_inverters_charge = scenario.balance_charge
            my_predbat.balance_inverters_discharge = scenario.balance_discharge
            my_predbat.balance_inverters_crosscharge = scenario.balance_crosscharge
            my_predbat.balance_inverters_threshold_charge = scenario.threshold
            my_predbat.balance_inverters_threshold_discharge = scenario.threshold
            my_predbat.set_read_only = False
            my_predbat.inverter_rate_intent = {index: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": owner} for index in range(count)}
            my_predbat.inverter_balance_overridden = {}
            # Balancing's own record starts empty, so the balance sensor reports this run alone
            ha.dummy_items.pop("sensor.{}_balance_inverters".format(my_predbat.prefix), None)
            my_predbat.balance_held_previous = {}
            my_predbat.balance_ceilings_previous = []
            my_predbat.balance_last_decisions = None
            my_predbat.balance_events = []
            my_predbat.balance_counts = {}
            my_predbat.balance_restored = False
            if scenario.export_limit is not None:
                my_predbat.export_limits_best = [scenario.export_limit]
                my_predbat.set_export_window = True
                my_predbat.set_export_low_power = True
            start_utc = my_predbat.now_utc
            with routed_to_sims(ha, sims, clock, scenario.rate_max):
                for t in range(seconds):
                    ct_import = -grid
                    for loop in loops:
                        loop.control(t, ct_import, scenario.mode)
                    outputs = [loop.respond(pv[t]) for loop in loops]
                    battery = [loop.power for loop in loops]
                    grid = pv[t] * site_pv_share + sum(ac for ac, _clipped in outputs) - load[t]
                    flows["clipped_kwh"] += sum(clipped for _ac, clipped in outputs) / 3600000.0
                    flows["import_kwh"] += max(-grid, 0.0) / 3600000.0
                    flows["export_kwh"] += max(grid, 0.0) / 3600000.0
                    out = sum(power for power in battery if power > 0)
                    into = -sum(power for power in battery if power < 0)
                    flows["battery_out_kwh"] += out / 3600000.0
                    flows["battery_in_kwh"] += into / 3600000.0
                    flows["cross_kwh"] += min(out, into) / 3600000.0
                    cross_now = min(out, into)
                    if cross_now > cross_peak["w"]:
                        cross_peak.update(w=cross_now, t=t, powers=[round(power) for power in battery])
                    for level in cross_over:
                        cross_over[level] += 1 if cross_now > level else 0
                    if t % 10 == 0:
                        power_trace.append((t, [round(power) for power in battery], round(load[t]), round(grid)))
                    if t % 60 == 59:
                        clock["s"] = max(clock["s"], float(t))
                        # Predbat's clock moves with the simulation, so a learned ceiling expires as it would live
                        my_predbat.now_utc = start_utc + timedelta(seconds=t)
                        for index, sim in enumerate(sims):
                            suffix = entity_suffix(index)
                            ha.dummy_items["sensor.soc_percent" + suffix] = sim.soc_percent_reported()
                            ha.dummy_items["sensor.battery_power" + suffix] = sim.battery_power_reported()
                            ha.dummy_items["sensor.grid_power" + suffix] = round(grid, 1) if index == 0 else 0
                            ha.dummy_items["sensor.load_power" + suffix] = round(load[t], 1) if index == 0 else 0
                            ha.dummy_items["sensor.pv_power" + suffix] = round(loops[index].pv_w + (pv[t] * site_pv_share if index == 0 else 0.0), 1)
                            ha.dummy_items["number.reserve" + suffix] = sim.reserve_percent
                        my_predbat.fetch_inverter_data(quick=True)
                        if scenario.export_limit is not None and t % 300 == 299:
                            # The plan run: the executor's own split of the planned fleet export power, and each
                            # inverter's own export target, stored as the intent the 60s rebalance balances on top of
                            allocation = my_predbat.allocate_fleet_export_rates()
                            target_percent = my_predbat.export_target_soc_percent()
                            for index, inverter in enumerate(my_predbat.inverters):
                                loops[index].export_target_kwh = my_predbat.adjust_battery_target_multi(inverter, target_percent, False, True, check=True) * inverter.soc_max / 100.0
                                my_predbat.inverter_rate_intent[index] = {
                                    "charge_rate": None,
                                    "discharge_rate": allocation.get(index),
                                    "pause_charge": False,
                                    "pause_discharge": False,
                                    "owner": "export",
                                }
                            # execute_plan's tail: balance a copy of the stored baseline and apply it, whether or
                            # not balancing is on - the claimed rates reach the registers either way
                            plan_intent = {index: dict(value) for index, value in my_predbat.inverter_rate_intent.items()}
                            my_predbat.balance_inverter_rates(plan_intent)
                            my_predbat.apply_rate_intent(plan_intent)
                        else:
                            my_predbat.rebalance_inverter_rates()
                        for index, sim in enumerate(sims):
                            is_held = any(sim.registers[kind] <= 0 for kind in RATE_KINDS)
                            held[index] += 1 if is_held else 0
                            held_edges[index] += 1 if (is_held and not previous_held[index]) else 0
                            previous_held[index] = is_held
                        socs_by_minute.append([sim.soc_kwh / sim.soc_max * 100.0 for sim in sims])
            sensor = ha.dummy_items.get("sensor.{}_balance_inverters".format(my_predbat.prefix))
            balance_sensor = dict(sensor) if isinstance(sensor, dict) else None
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
        "clipped_kwh": round(flows["clipped_kwh"], 3),
        "spread_start": round(max(scenario.soc) - min(scenario.soc), 2),
        "spread_end": round(spread[-1], 2) if spread else None,
        "held_minutes": held,
        "holds": held_edges,
        "writes": sum(sim.writes[kind] for sim in sims for kind in RATE_KINDS),
        # What sensor.<prefix>_balance_inverters reported at the end of the run, for checking it against the physics
        "balance_sensor": balance_sensor,
        "cross_peak_w": round(cross_peak["w"]),
        "cross_peak_at_s": cross_peak["t"],
        "cross_peak_powers": cross_peak["powers"],
        "cross_over_1kw_s": cross_over[1000],
        "cross_over_2kw_s": cross_over[2000],
        "power_trace": power_trace,
        # Balancer decisions by branch, from its own log lines: cross-chargers newly caught (a latched hold is not
        # logged again, so this is not a count of passes holding), and SoC holds
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
            for loops_name, loops in (("same loops", staggered), ("mixed loops", mixed), ("CT disagree", disagreeing)):
                for balance in (False, True):
                    scenarios.append(CTScenario(name="{} inv, {}, {}, balance {}".format(count, label, loops_name, "on" if balance else "off"), mode=mode, soc=soc, house=house(), dynamics=loops(count), balance=balance))
    return scenarios


def format_ct_report(results):
    """
    One line per CT scenario.
    """
    lines = ["{:<66} {:>7} {:>7} {:>7} {:>6} {:>6} {:>14} {:>6}".format("scenario", "import", "export", "cross", "spread0", "spread1", "holds", "writes")]
    for result in results:
        lines.append(
            "{:<66} {:>7} {:>7} {:>7} {:>6} {:>6} {:>14} {:>6}".format(
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


def cloud_matrix():
    """
    Midday under broken cloud, the case that keeps flipping the fleet between charging and discharging. PV is
    either AC-coupled at the site or split across the inverters' own DC sides; the last scenario puts a large
    array on the FULLER inverter, so a charge hold on it has to clip PV once the array exceeds its AC limit.
    """
    scenarios = []
    for count in (2, 3):
        soc = [70, 80] if count == 2 else [70, 75, 80]
        split = [0.4, 0.6] if count == 2 else [0.3, 0.3, 0.4]
        big = [0.25, 0.75] if count == 2 else [0.15, 0.15, 0.7]
        for label, house, pv_split in (
            ("broken cloud, AC-coupled PV", broken_cloud_midday(), None),
            ("broken cloud, DC PV", broken_cloud_midday(), split),
            ("broken cloud, big array on fullest", broken_cloud_midday(pv_w=9000.0), big),
            # PV close to the house load, so every cloud tips the site between surplus and deficit
            ("broken cloud, PV close to load", broken_cloud_midday(pv_w=2200.0, base_w=900.0), split),
        ):
            for loops_name, loops in (("mixed loops", mixed), ("CT disagree", disagreeing)):
                for balance in (False, True):
                    scenarios.append(
                        CTScenario(
                            name="{} inv, {}, {}, balance {}".format(count, label, loops_name, "on" if balance else "off"),
                            mode="eco",
                            soc=soc,
                            house=house,
                            dynamics=loops(count),
                            balance=balance,
                            pv_split=pv_split,
                        )
                    )
    return scenarios

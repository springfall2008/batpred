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
Closed-loop balancing harness: two simulated inverters on one house, driven by the real 60s rebalance loop.

The pure balancer tests (test_balance_pure.py) and the fleet rig (fleet_rig.py) each check one pass against
readings a test wrote by hand. Balancing faults are rarely one pass: a hold is written, the battery stops,
the next status refresh reads the stopped battery back, and the next pass decides from THAT whether to keep
the hold. This harness runs that loop for a simulated hour or two so the second and later passes are tested.

Each simulated minute:

1. The house load (or PV surplus) is shared between the two simulated batteries according to what each
   inverter's rate REGISTER allows - not what Predbat thinks it wrote. The grid takes the rest.
2. The batteries' SoC, power and rate READBACK are published on the mock HA interface.
3. Predbat runs quick_inverter_data_update() exactly as update_time_loop does every
   INVERTER_QUICK_UPDATE_SECONDS: fetch_inverter_data(quick=True) -> Inverter.update_status() ->
   rebalance_inverter_rates() -> build_inverter_snapshot() -> utils.balance_inverters() -> apply_rate_intent().
4. Every rate write reaches the simulated register through the HA number/set_value service, where the
   inverter's misbehaviour (Behaviour below) decides what actually lands and when the readback shows it.

The rate intent is the one execute_plan stores for a demand slot with nothing claimed, so what is measured is
balancing alone. Nothing below status refresh is mocked: real Inverter objects, real snapshot, real balancer,
real rate writes with their own write_and_poll_value verification.

The site grid meter is reported on inverter 0 only, and the house load and PV likewise, so the fleet totals
the balancer sums are the site's, not double counted.
"""

import contextlib
import io
import random
from dataclasses import dataclass, field

from tests.fleet_rig import FleetInverter, entity_suffix, setup_fleet

RATE_KINDS = ("charge", "discharge")


@dataclass
class Behaviour:
    """
    How one simulated inverter departs from an ideal one. The defaults are an ideal inverter.
    """

    # Power the battery still moves with its rate register at 0. A GivEnergy inverter trickles ~150-200W;
    # 0 means a true pause, as a gateway-held or paused battery reaches.
    trickle_w: float = 0.0
    # The register will not hold 0 and stores this instead - an inverter with a minimum programmable rate.
    zero_floor_w: float = 0.0
    # Seconds before a value written to the rate register shows up in its readback entity: a hub or cloud
    # feed that lags its own writes. Rate writes are verified by write_and_poll_value against this readback.
    readback_lag_s: float = 0.0
    # Refuse every Nth rate write (0 = never), as a hub answering modbus_write_failed.
    refuse_every: int = 0
    # Uniform +/- jitter on the reported battery power, in W.
    power_noise_w: float = 0.0
    # Report SoC as a whole percent, as most inverters do.
    soc_whole_percent: bool = True
    # The most the battery will really deliver in W, whatever its register says (0 = no limit): a BMS or
    # temperature derate, or an AC limit below the rate Predbat was told. Predbat still sees the full rate.
    derate_w: float = 0.0


IDEAL = Behaviour()


@dataclass
class Scenario:
    """
    One closed-loop run: the fleet, how each inverter misbehaves, and what the house is doing.
    """

    name: str
    # "discharge": the house draws load_w from the batteries. "charge": PV surplus of surplus_w is soaked up.
    direction: str
    soc: list
    behaviours: list = field(default_factory=lambda: [IDEAL, IDEAL])
    # inverter_battery_rate_min in W, per inverter. The GE Cloud, GE EMS, Fox Cloud and Solis Cloud
    # templates ship 200; the GE Cloud Octopus standalone template ships 25.
    rate_min: float = 0.0
    soc_max: float = 9.5
    rate_max: float = 3000
    load_w: float = 1600
    surplus_w: float = 3000
    minutes: int = 90
    threshold: float = 1.0
    seed: int = 1
    # (minute, W) changes to the house load (discharge) or PV surplus (charge), applied from that minute on
    steps: list = field(default_factory=list)
    # Battery temperature in C per inverter, and the battery_temperature_discharge_curve / _charge_curve Predbat
    # is configured with ({temperature: C-rate}); empty means no temperature derating is configured
    temperatures: list = None
    temperature_curve: dict = field(default_factory=dict)

    def demand_at(self, minute):
        """
        The house load (discharge) or PV surplus (charge) in W at the given minute.
        """
        demand = self.load_w if self.direction == "discharge" else self.surplus_w
        for step_minute, watts in sorted(self.steps):
            if minute >= step_minute:
                demand = watts
        return demand


class SimInverter:
    """
    The hardware behind one inverter's entities: a battery, its two rate registers, and their readbacks.
    """

    def __init__(self, soc_max, rate_max, soc_percent, behaviour, rng, reserve_percent=4.0):
        """
        Start the battery at soc_percent with both rate registers at their maximum.
        """
        self.soc_max = soc_max
        self.rate_max = rate_max
        self.reserve_percent = reserve_percent
        self.soc_kwh = soc_max * soc_percent / 100.0
        self.behaviour = behaviour
        self.rng = rng
        self.registers = {kind: float(rate_max) for kind in RATE_KINDS}
        # (time the value was written in seconds, value) - the readback shows the latest one old enough
        self.history = {kind: [(-1e9, float(rate_max))] for kind in RATE_KINDS}
        self.writes = {kind: 0 for kind in RATE_KINDS}
        self.refused = {kind: 0 for kind in RATE_KINDS}
        self.battery_power = 0.0

    def write(self, kind, value, now_s):
        """
        Apply one rate write as the hardware would: maybe refused, maybe floored, capped at the maximum.
        """
        self.writes[kind] += 1
        if self.behaviour.refuse_every and self.writes[kind] % self.behaviour.refuse_every == 0:
            self.refused[kind] += 1
            return
        value = min(max(float(value), 0.0), self.rate_max)
        if value <= 0 and self.behaviour.zero_floor_w:
            value = float(self.behaviour.zero_floor_w)
        self.registers[kind] = value
        self.history[kind].append((now_s, value))

    def readback(self, kind, now_s):
        """
        The rate register as its entity reports it at now_s, which lags the register by readback_lag_s.
        """
        visible = self.history[kind][0][1]
        for written_s, value in self.history[kind]:
            if written_s + self.behaviour.readback_lag_s <= now_s:
                visible = value
        return visible

    def limit(self, kind):
        """
        The power in W this battery can move this minute in the given direction: its register, or its
        trickle when the register is 0, and nothing at all once it is full or down to its reserve.
        """
        if kind == "discharge" and self.soc_kwh <= self.soc_max * self.reserve_percent / 100.0:
            return 0.0
        if kind == "charge" and self.soc_kwh >= self.soc_max:
            return 0.0
        rate = self.registers[kind]
        if rate <= 0:
            return self.behaviour.trickle_w
        rate = min(rate, self.rate_max)
        if self.behaviour.derate_w:
            rate = min(rate, self.behaviour.derate_w)
        return rate

    def soc_percent_reported(self):
        """
        SoC as the inverter reports it.
        """
        percent = self.soc_kwh / self.soc_max * 100.0
        return round(percent) if self.behaviour.soc_whole_percent else round(percent, 2)

    def battery_power_reported(self):
        """
        Battery power as the inverter reports it (+ve discharging), with the configured jitter.
        """
        noise = self.behaviour.power_noise_w
        return round(self.battery_power + (self.rng.uniform(-noise, noise) if noise else 0.0), 1)


def share_power(demand_w, sims, kind):
    """
    Share demand_w between the batteries the way paralleled inverters chasing one grid meter do.

    Each inverter pushes towards zero grid, so they settle on an equal share; one that is capped below its
    share (a low register, a trickle, a full battery) gives the rest of it to the others. That is water-filling
    on equal shares, and it is why a battery held to a low register still delivers all of it. Returns W per
    battery.
    """
    limits = [sim.limit(kind) for sim in sims]
    alloc = [0.0] * len(sims)
    open_members = [index for index, limit in enumerate(limits) if limit > 0]
    remaining = max(demand_w, 0.0)
    while open_members and remaining > 1e-9:
        share = remaining / len(open_members)
        capped = [index for index in open_members if limits[index] - alloc[index] <= share]
        if not capped:
            for index in open_members:
                alloc[index] += share
            break
        for index in capped:
            remaining -= limits[index] - alloc[index]
            alloc[index] = limits[index]
            open_members.remove(index)
    return alloc


def step_house(sims, scenario, demand):
    """
    Advance the house by one minute at the given load (discharge) or PV surplus (charge) in W. Returns grid
    power in W, Predbat's convention (+ve export, -ve import).
    """
    if scenario.direction == "discharge":
        alloc = share_power(demand, sims, "discharge")
        for sim, power in zip(sims, alloc):
            sim.battery_power = power
        grid = -(demand - sum(alloc))
    else:
        alloc = share_power(demand, sims, "charge")
        for sim, power in zip(sims, alloc):
            sim.battery_power = -power
        grid = demand - sum(alloc)
    for sim in sims:
        sim.soc_kwh = min(max(sim.soc_kwh - sim.battery_power / 1000.0 / 60.0, 0.0), sim.soc_max)
    return grid


@contextlib.contextmanager
def routed_to_sims(ha, sims, clock, rate_max):
    """
    Route the rate entities' reads and writes on the mock HA interface to the simulated registers.

    Reads of number.charge_rate[_N] / number.discharge_rate[_N] return the lagged readback, with the max
    attribute the GivEnergy rate ceiling is read from. A number/set_value to one of them is a register write.
    """
    entities = {}
    for index, _sim in enumerate(sims):
        for kind in RATE_KINDS:
            entities["number.{}_rate{}".format(kind, entity_suffix(index))] = (index, kind)
    original_get_state = ha.get_state
    original_call_service = ha.call_service

    def get_state(entity_id=None, default=None, attribute=None, refresh=False, raw=False):
        """Lagged register readback for the rate entities, the mock's own state for anything else."""
        if entity_id in entities:
            index, kind = entities[entity_id]
            item = {"state": sims[index].readback(kind, clock["s"]), "max": rate_max}
            if raw:
                return item
            return item.get(attribute, "") if attribute else item["state"]
        return original_get_state(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh, raw=raw)

    def call_service(service, **kwargs):
        """A set_value on a rate entity is a register write; anything else goes to the mock."""
        entity_id = kwargs.get("entity_id", "")
        if service == "number/set_value" and entity_id in entities:
            index, kind = entities[entity_id]
            sims[index].write(kind, kwargs.get("value", 0), clock["s"])
            return True
        return original_call_service(service, **kwargs)

    ha.get_state = get_state
    ha.call_service = call_service
    try:
        yield
    finally:
        ha.get_state = original_get_state
        ha.call_service = original_call_service


def run_scenario(my_predbat, scenario):
    """
    Run one scenario through the real rebalance loop and return what happened, minute by minute and in total.

    The trace has one entry per minute: true SoC, reported SoC, battery power and the two registers of each
    inverter, and the site grid power.
    """
    rng = random.Random(scenario.seed)
    count = len(scenario.soc)
    temperatures = scenario.temperatures or [20.0] * count
    fleet = [FleetInverter(scenario.soc_max, rate_max=scenario.rate_max, inverter_limit=max(5000, scenario.rate_max), rate_min=scenario.rate_min, temperature=temperatures[index]) for index in range(count)]
    sims = [SimInverter(scenario.soc_max, scenario.rate_max, scenario.soc[index], scenario.behaviours[index], rng) for index in range(count)]
    clock = {"s": 0.0}
    ha = my_predbat.ha_interface

    def publish():
        """Put the batteries' live readings on the mock HA interface."""
        for index, sim in enumerate(sims):
            suffix = entity_suffix(index)
            ha.dummy_items["sensor.soc_percent" + suffix] = sim.soc_percent_reported()
            ha.dummy_items["sensor.battery_power" + suffix] = sim.battery_power_reported()
            ha.dummy_items["sensor.grid_power" + suffix] = round(grid, 1) if index == 0 else 0
            ha.dummy_items["sensor.load_power" + suffix] = demand if (index == 0 and scenario.direction == "discharge") else 0
            ha.dummy_items["sensor.pv_power" + suffix] = demand if (index == 0 and scenario.direction == "charge") else 0
            ha.dummy_items["number.reserve" + suffix] = sim.reserve_percent

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
            "battery_temperature_charge_curve",
            "battery_temperature_discharge_curve",
        )
    }
    trace = []
    log = io.StringIO()
    log_lines = []
    grid = 0.0
    demand = scenario.demand_at(0)
    original_log = my_predbat.log
    my_predbat.log = log_lines.append
    try:
        with contextlib.redirect_stdout(log):
            setup_fleet(my_predbat, fleet, soc=scenario.soc, charge_rate=scenario.rate_max, discharge_rate=scenario.rate_max)
            for inverter in my_predbat.inverters:
                # write_and_poll_value's poll interval passes on the simulated clock, so a lagging readback
                # fails verification exactly as it would live
                inverter.sleep = lambda seconds: clock.__setitem__("s", clock["s"] + seconds)
            my_predbat.balance_inverters_enable = True
            my_predbat.balance_inverters_charge = True
            my_predbat.balance_inverters_discharge = True
            my_predbat.balance_inverters_crosscharge = True
            my_predbat.balance_inverters_threshold_charge = scenario.threshold
            my_predbat.balance_inverters_threshold_discharge = scenario.threshold
            my_predbat.set_read_only = False
            my_predbat.battery_temperature_charge_curve = dict(scenario.temperature_curve)
            my_predbat.battery_temperature_discharge_curve = dict(scenario.temperature_curve)
            my_predbat.inverter_rate_intent = {index: {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"} for index in range(count)}
            my_predbat.inverter_balance_overridden = {}
            with routed_to_sims(ha, sims, clock, scenario.rate_max):
                for minute in range(scenario.minutes):
                    clock["s"] = max(clock["s"], minute * 60.0)
                    demand = scenario.demand_at(minute)
                    grid = step_house(sims, scenario, demand)
                    publish()
                    my_predbat.fetch_inverter_data(quick=True)
                    my_predbat.rebalance_inverter_rates()
                    trace.append(
                        {
                            "minute": minute,
                            "soc": [round(sim.soc_kwh / sim.soc_max * 100.0, 2) for sim in sims],
                            "soc_reported": [sim.soc_percent_reported() for sim in sims],
                            "battery_power": [round(sim.battery_power, 1) for sim in sims],
                            "charge_register": [sim.registers["charge"] for sim in sims],
                            "discharge_register": [sim.registers["discharge"] for sim in sims],
                            "grid": round(grid, 1),
                            "demand": demand,
                        }
                    )
    finally:
        my_predbat.log = original_log
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return summarise(scenario, sims, trace, "\n".join(str(line) for line in log_lines))


def summarise(scenario, sims, trace, log_text):
    """
    Reduce a trace to the numbers that say whether balancing behaved.

    A release is a register going from held (<= the zero floor) back up. It is a SWAP when another inverter
    is newly held in the same pass - with three or more inverters the hold moves to whichever is now lowest,
    which is balancing working, not failing. Otherwise it is a BAD release when the inverter is still out of
    balance by the threshold in the direction that justified the hold: the hold was lost, not finished.
    holds counts the edges the other way.
    """
    kind = scenario.direction
    register_key = kind + "_register"
    count = len(sims)
    holds = [0] * count
    releases = [0] * count
    bad_releases = [0] * count
    swaps = [0] * count
    held_minutes = [0] * count
    previous = [scenario.rate_max] * count
    floor = [max(sim.behaviour.zero_floor_w, 0.0) for sim in sims]
    for entry in trace:
        newly_held = {index for index in range(count) if entry[register_key][index] <= floor[index] < previous[index]}
        for index in range(count):
            value = entry[register_key][index]
            was_held = previous[index] <= floor[index]
            is_held = value <= floor[index]
            if is_held:
                held_minutes[index] += 1
            if is_held and not was_held:
                holds[index] += 1
            if was_held and not is_held and newly_held - {index}:
                releases[index] += 1
                swaps[index] += 1
            elif was_held and not is_held:
                releases[index] += 1
                socs = entry["soc_reported"]
                others = [socs[other] for other in range(count) if other != index]
                if kind == "discharge":
                    still_needed = socs[index] <= max(others) - scenario.threshold
                else:
                    still_needed = socs[index] >= min(others) + scenario.threshold
                if still_needed:
                    bad_releases[index] += 1
            previous[index] = value
    spreads = [max(entry["soc"]) - min(entry["soc"]) for entry in trace]
    converged_at = next((entry["minute"] for entry in trace if max(entry["soc"]) - min(entry["soc"]) <= scenario.threshold), None)
    # Energy the batteries left to the grid: import while discharging, and while charging the surplus they
    # failed to soak up, exported instead
    unserved_kwh = sum(max(-entry["grid"] if kind == "discharge" else entry["grid"], 0.0) for entry in trace) / 60000.0
    # After the first step: minutes until no inverter is held any more, and the worst single minute's shortfall
    release_after = None
    worst_unserved_w = 0.0
    if scenario.steps:
        step_minute = min(minute for minute, _watts in scenario.steps)
        for entry in trace:
            if entry["minute"] < step_minute:
                continue
            worst_unserved_w = max(worst_unserved_w, -entry["grid"] if kind == "discharge" else entry["grid"])
            if release_after is None and all(entry[register_key][index] > floor[index] for index in range(count)):
                release_after = entry["minute"] - step_minute
    return {
        "name": scenario.name,
        "release_after": release_after,
        "worst_unserved_w": round(worst_unserved_w, 1),
        "direction": kind,
        "holds": holds,
        "releases": releases,
        "bad_releases": bad_releases,
        "swaps": swaps,
        "held_minutes": held_minutes,
        "writes": [sim.writes[kind] for sim in sims],
        "refused": [sim.refused[kind] for sim in sims],
        "write_verify_failures": log_text.count("didn't complete"),
        "spread_start": round(spreads[0], 2) if spreads else None,
        "spread_end": round(spreads[-1], 2) if spreads else None,
        "converged_at": converged_at,
        "unserved_kwh": round(unserved_kwh, 3),
        "trace": trace,
    }


# Two GivEnergy-sized inverters with 9.5 kWh batteries, five points apart - the shape of the reports. Every
# behaviour is put on the inverter the balancer will hold (the low one discharging, the high one charging),
# since that is the one whose readback decides whether the hold survives.
PAUSE = Behaviour(trickle_w=0.0)
GE_TRICKLE = Behaviour(trickle_w=150.0)


def standard_scenarios():
    """
    The scenario matrix: each misbehaviour, on each side, with and without an inverter_battery_rate_min.
    """
    behaviours = [
        ("ideal, rate_min 0", IDEAL, 0),
        ("true pause, rate_min 200", PAUSE, 200),
        ("GE trickle 150W, rate_min 200", GE_TRICKLE, 200),
        ("true pause, rate_min 25", PAUSE, 25),
        ("true pause + readback lag 90s, rate_min 200", Behaviour(readback_lag_s=90), 200),
        ("true pause + every 3rd write refused, rate_min 200", Behaviour(refuse_every=3), 200),
        ("true pause + power noise 60W, rate_min 200", Behaviour(power_noise_w=60), 200),
        ("register floors 0 at 95W, rate_min 200", Behaviour(zero_floor_w=95), 200),
    ]
    scenarios = []
    for direction, soc in (("discharge", [75, 70]), ("charge", [70, 75])):
        held = 1
        for label, behaviour, rate_min in behaviours:
            per_inverter = [IDEAL, IDEAL]
            per_inverter[held] = behaviour
            scenarios.append(Scenario(name="{}: {}".format(direction, label), direction=direction, soc=soc, behaviours=per_inverter, rate_min=rate_min))
    # The guard the fix must not defeat: with more load (or surplus) than one inverter can carry, holding
    # either would put the difference on the grid, so nothing should be held at all.
    scenarios.append(Scenario(name="discharge: true pause, load beyond one inverter", direction="discharge", soc=[75, 70], behaviours=[IDEAL, PAUSE], rate_min=200, load_w=3500, minutes=40))
    scenarios.append(Scenario(name="charge: true pause, surplus beyond one inverter", direction="charge", soc=[70, 75], behaviours=[IDEAL, PAUSE], rate_min=200, surplus_w=3500, minutes=40))
    return scenarios


def format_report(results):
    """
    One line per scenario, for comparing two trees by eye.
    """
    lines = ["{:<62} {:>6} {:>8} {:>5} {:>7} {:>7} {:>6} {:>9} {:>8}".format("scenario", "holds", "releases", "bad", "held_m", "writes", "failed", "converged", "import")]
    for result in results:
        held = 1
        lines.append(
            "{:<62} {:>6} {:>8} {:>5} {:>7} {:>7} {:>6} {:>9} {:>8}".format(
                result["name"],
                result["holds"][held],
                result["releases"][held],
                result["bad_releases"][held],
                result["held_minutes"][held],
                result["writes"][held],
                result["write_verify_failures"],
                "-" if result["converged_at"] is None else result["converged_at"],
                result["unserved_kwh"],
            )
        )
    return "\n".join(lines)


def load_step_scenarios():
    """
    A hold is established on a light load, then the load (or PV surplus) steps up so both inverters are needed.

    The balancer's capacity guard reasons from MEASURED battery power, and while one battery is held its peer
    is capped at its own rate, so the guard only ever sees up to that cap - whatever the house actually wants.
    These check the hold is let go once the peer can no longer carry the house alone, and how long the grid
    covers the gap first.
    """
    scenarios = []
    for label, behaviour in (("true pause", PAUSE), ("GE trickle 150W", GE_TRICKLE), ("true pause + readback lag 90s", Behaviour(readback_lag_s=90)), ("true pause + every 3rd write refused", Behaviour(refuse_every=3))):
        for watts in (4000, 2900, 2700):
            scenarios.append(Scenario(name="discharge {}: 1600W -> {}W at 6m".format(label, watts), direction="discharge", soc=[75, 70], behaviours=[IDEAL, behaviour], rate_min=200, load_w=1600, steps=[(6, watts)], minutes=40))
        scenarios.append(Scenario(name="charge {}: 2000W -> 4500W at 6m".format(label), direction="charge", soc=[70, 75], behaviours=[IDEAL, behaviour], rate_min=200, surplus_w=2000, steps=[(6, 4500)], minutes=40))
    # The peer cannot really deliver its configured rate. The capacity guard compares that configured rate with
    # MEASURED battery power, and a derated peer never measures more than its derate, so the guard cannot see
    # the shortfall - only the grid can.
    for label, behaviour in (("true pause", PAUSE), ("GE trickle 150W", GE_TRICKLE)):
        scenarios.append(Scenario(name="discharge {}, peer derated to 2500W: 1600W -> 4000W".format(label), direction="discharge", soc=[75, 70], behaviours=[Behaviour(derate_w=2500), behaviour], rate_min=200, load_w=1600, steps=[(6, 4000)], minutes=40))
        scenarios.append(Scenario(name="charge {}, peer derated to 2500W: 2000W -> 4500W".format(label), direction="charge", soc=[70, 75], behaviours=[Behaviour(derate_w=2500), behaviour], rate_min=200, surplus_w=2000, steps=[(6, 4500)], minutes=40))
    # And back down again: the hold should come back once one inverter can carry the house on its own
    scenarios.append(Scenario(name="discharge true pause: 1600W -> 4000W at 6m -> 1600W at 12m", direction="discharge", soc=[80, 70], behaviours=[IDEAL, PAUSE], rate_min=200, load_w=1600, steps=[(6, 4000), (12, 1600)], minutes=40))
    return scenarios


def format_step_report(results):
    """
    One line per load-step scenario: release latency after the step, worst minute and total left to the grid.
    """
    lines = ["{:<62} {:>6} {:>5} {:>7} {:>8} {:>10} {:>9}".format("scenario", "holds", "bad", "writes", "release", "worst_W", "unserved")]
    for result in results:
        lines.append(
            "{:<62} {:>6} {:>5} {:>7} {:>8} {:>10} {:>9}".format(
                result["name"],
                result["holds"][1],
                result["bad_releases"][1],
                result["writes"][1],
                "-" if result["release_after"] is None else "{}m".format(result["release_after"]),
                result["worst_unserved_w"],
                result["unserved_kwh"],
            )
        )
    return "\n".join(lines)


def derated_scenarios():
    """
    A peer that cannot deliver the rate Predbat was told - a cold battery, a BMS derate, a lower AC limit.

    The capacity guards compare the peers' CONFIGURED rates with what the house needs, so a peer that really
    delivers less leaves the house on the grid for as long as the hold lasts - until SoC converges, which with a
    badly derated peer can be hours. Each scenario names the derate, the inverter count and the demand. In every
    one the hold should be let go within a pass or two of the shortfall showing at the grid meter, and only
    re-taken once the peer has shown it can carry the house again.
    """
    scenarios = []
    for derate in (2000, 1500, 200):
        for direction in ("discharge", "charge"):
            soc = [80, 70] if direction == "discharge" else [70, 80]
            for demand in (2600, 4000):
                demand_kw = {"load_w": demand} if direction == "discharge" else {"surplus_w": demand}
                scenarios.append(
                    Scenario(
                        name="2 inv {} peer derated {}W, {}W".format(direction, derate, demand),
                        direction=direction,
                        soc=soc,
                        behaviours=[Behaviour(derate_w=derate), PAUSE],
                        rate_min=200,
                        minutes=30 if direction == "charge" else 60,
                        **demand_kw,
                    )
                )
    for derate in (2000, 1000, 200):
        scenarios.append(Scenario(name="3 inv discharge high one derated {}W, 1600W".format(derate), direction="discharge", soc=[80, 70, 70], behaviours=[Behaviour(derate_w=derate), PAUSE, PAUSE], rate_min=200, load_w=1600, minutes=60))
        scenarios.append(Scenario(name="3 inv charge low one derated {}W, 2600W".format(derate), direction="charge", soc=[70, 80, 80], behaviours=[Behaviour(derate_w=derate), PAUSE, PAUSE], rate_min=200, surplus_w=2600, minutes=30))
    scenarios.append(Scenario(name="3 inv discharge middle derated 1000W, 2600W", direction="discharge", soc=[80, 75, 70], behaviours=[PAUSE, Behaviour(derate_w=1000), PAUSE], rate_min=200, load_w=2600, minutes=60))
    scenarios.append(Scenario(name="3 inv discharge GE trickle mix, high one derated 1000W, 2600W", direction="discharge", soc=[80, 70, 70], behaviours=[Behaviour(derate_w=1000), GE_TRICKLE, PAUSE], rate_min=200, load_w=2600, minutes=60))
    return scenarios

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
Heterogeneous multi-inverter fleet test rig (#4856 workstream 0).

Every multi-inverter test up to now has used two identical inverters, which is exactly the
case in which the fleet seams behave correctly, so none of the reported multi-inverter faults
are reachable from the test suite. This builds a fleet of arbitrary count, capacity, rate and
starting SoC out of the TestHAInterface mock, using the real Inverter class and the real
fetch_inverter_data() aggregation rather than hand-set fleet totals.

Fixtures for the fleets in the open reports live in FLEET_FIXTURES.
"""

from inverter import Inverter
from utils import calc_percent_limit

# Entity args that are per-inverter lists of entity ids, and the entity name each one is built from.
# The suffix convention matches Predbat's own docs: no suffix for the first inverter, then _2, _3...
FLEET_ENTITY_ARGS = [
    ("soc_percent", "sensor.soc_percent"),
    ("battery_power", "sensor.battery_power"),
    ("pv_power", "sensor.pv_power"),
    ("load_power", "sensor.load_power"),
    ("grid_power", "sensor.grid_power"),
    ("charge_rate", "number.charge_rate"),
    ("discharge_rate", "number.discharge_rate"),
    ("reserve", "number.reserve"),
    ("soc_max", "sensor.soc_max"),
    ("battery_rate_max", "sensor.battery_rate_max"),
    ("charge_start_time", "select.charge_start_time"),
    ("charge_end_time", "select.charge_end_time"),
    ("discharge_start_time", "select.discharge_start_time"),
    ("discharge_end_time", "select.discharge_end_time"),
    ("scheduled_charge_enable", "switch.scheduled_charge_enable"),
    ("scheduled_discharge_enable", "switch.scheduled_discharge_enable"),
]

# Args that would otherwise be inherited from the single-inverter apps.yaml and confuse a fleet.
FLEET_CONFLICTING_ARGS = ["pause_mode", "inverter_time", "soc_kw", "battery_power_invert"]


def entity_suffix(index):
    """
    Entity name suffix for inverter <index> - the first inverter has no suffix.
    """
    return "" if index == 0 else "_{}".format(index + 1)


class FleetInverter:
    """
    The static configuration of one inverter in a test fleet - what the hardware is, as opposed
    to what it happens to be doing in a given scenario.
    """

    def __init__(self, soc_max, rate_max=2600, inverter_limit=5000, rate_min=100, battery_scaling=1.0, temperature=20.0):
        """
        Define one fleet member: battery capacity in kWh and charge/discharge rate ceiling in W.
        """
        self.soc_max = soc_max
        self.rate_max = rate_max
        self.inverter_limit = inverter_limit
        self.rate_min = rate_min
        self.battery_scaling = battery_scaling
        self.temperature = temperature


# Named fleets taken from the open multi-inverter reports, so a test can say which real system it
# is reproducing. Capacities and rates are as stated by the reporters in each issue.
FLEET_FIXTURES = {
    # The case every existing multi-inverter test uses - a fleet that cannot show any of the faults.
    "matched_pair": [FleetInverter(9.5), FleetInverter(9.5)],
    # #830 - 9.5 and 5.2 kWh batteries, both inverters rated 2.6 kW.
    "issue_830": [FleetInverter(9.5), FleetInverter(5.2)],
    # #4842 - 2x GivEnergy Gen1 hybrid plus 1x AC3.
    "issue_4842": [FleetInverter(12.4), FleetInverter(9.52), FleetInverter(8.19, rate_max=3000)],
    # #3172 - an AC3 carrying two 9.5 kWh batteries alongside a HY5.0 carrying one.
    "issue_3172": [FleetInverter(19.0, rate_max=3000), FleetInverter(9.5, rate_max=3000)],
}


def broadcast(value, count, default=None):
    """
    Resolve a per-inverter scenario argument that may be a list, a scalar to apply to every
    inverter, or None to fall back to a default (itself a list or a scalar).
    """
    if value is None:
        value = default
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value] * count


def setup_fleet(
    my_predbat,
    fleet,
    soc=50,
    reserve=4,
    battery_power=0,
    pv_power=0,
    load_power=500,
    grid_power=0,
    charge_rate=None,
    discharge_rate=None,
    charge_start_time="01:00:00",
    charge_end_time="05:00:00",
    discharge_start_time="00:00:00",
    discharge_end_time="00:00:00",
    scheduled_charge_enable="off",
    scheduled_discharge_enable="off",
    aggregate=True,
):
    """
    Build a fleet of real Inverter objects on the mock HA interface.

    fleet is a list of FleetInverter (or the name of an entry in FLEET_FIXTURES). Every scenario
    argument may be a single value applied to the whole fleet or a per-inverter list. charge_rate
    and discharge_rate default to each inverter's own rate ceiling.

    With aggregate set the fleet totals on my_predbat (soc_kw, soc_max, battery_rate_max_charge
    and the rest) are then produced by the real fetch_inverter_data() accumulation, so a test
    reads the same numbers the executor would.
    """
    if isinstance(fleet, str):
        fleet = FLEET_FIXTURES[fleet]
    count = len(fleet)

    socs = broadcast(soc, count)
    reserves = broadcast(reserve, count)
    battery_powers = broadcast(battery_power, count)
    pv_powers = broadcast(pv_power, count)
    load_powers = broadcast(load_power, count)
    grid_powers = broadcast(grid_power, count)
    charge_rates = broadcast(charge_rate, count, default=[member.rate_max for member in fleet])
    discharge_rates = broadcast(discharge_rate, count, default=[member.rate_max for member in fleet])
    charge_starts = broadcast(charge_start_time, count)
    charge_ends = broadcast(charge_end_time, count)
    discharge_starts = broadcast(discharge_start_time, count)
    discharge_ends = broadcast(discharge_end_time, count)
    charge_enables = broadcast(scheduled_charge_enable, count)
    discharge_enables = broadcast(scheduled_discharge_enable, count)

    ha = my_predbat.ha_interface
    ha.service_store_enable = True
    ha.service_store = []

    for id in range(count):
        suffix = entity_suffix(id)
        ha.dummy_items["sensor.soc_percent" + suffix] = socs[id]
        ha.dummy_items["sensor.battery_power" + suffix] = battery_powers[id]
        ha.dummy_items["sensor.pv_power" + suffix] = pv_powers[id]
        ha.dummy_items["sensor.load_power" + suffix] = load_powers[id]
        ha.dummy_items["sensor.grid_power" + suffix] = grid_powers[id]
        # Modelled as real number entities with a max attribute rather than bare values: on a GE
        # inverter the rate ceiling is read from number.charge_rate's max, so a scalar here would
        # silently give every inverter in the fleet the same 2600W default.
        ha.dummy_items["number.charge_rate" + suffix] = {"state": charge_rates[id], "max": fleet[id].rate_max}
        ha.dummy_items["number.discharge_rate" + suffix] = {"state": discharge_rates[id], "max": fleet[id].rate_max}
        ha.dummy_items["number.reserve" + suffix] = reserves[id]
        ha.dummy_items["sensor.soc_max" + suffix] = fleet[id].soc_max
        ha.dummy_items["sensor.battery_rate_max" + suffix] = fleet[id].rate_max
        ha.dummy_items["select.charge_start_time" + suffix] = charge_starts[id]
        ha.dummy_items["select.charge_end_time" + suffix] = charge_ends[id]
        ha.dummy_items["select.discharge_start_time" + suffix] = discharge_starts[id]
        ha.dummy_items["select.discharge_end_time" + suffix] = discharge_ends[id]
        ha.dummy_items["switch.scheduled_charge_enable" + suffix] = charge_enables[id]
        ha.dummy_items["switch.scheduled_discharge_enable" + suffix] = discharge_enables[id]

    # Configure args before creating the inverters - Inverter() reads them in its constructor
    my_predbat.args["num_inverters"] = count
    my_predbat.num_inverters = count
    for arg, entity in FLEET_ENTITY_ARGS:
        my_predbat.args[arg] = [entity + entity_suffix(id) for id in range(count)]
    my_predbat.args["battery_scaling"] = [member.battery_scaling for member in fleet]
    my_predbat.args["battery_temperature"] = [member.temperature for member in fleet]
    my_predbat.args["inverter_limit"] = [member.inverter_limit for member in fleet]
    my_predbat.args["inverter_battery_rate_min"] = [member.rate_min for member in fleet]
    my_predbat.args["inverter_limit_charge"] = [member.rate_max for member in fleet]
    my_predbat.args["inverter_limit_discharge"] = [member.rate_max for member in fleet]
    for arg in FLEET_CONFLICTING_ARGS:
        if arg in my_predbat.args:
            del my_predbat.args[arg]

    my_predbat.inverters = []
    for id in range(count):
        inverter = Inverter(my_predbat, id, quiet=True)
        inverter.sleep = dummy_sleep
        inverter.update_status(my_predbat.minutes_now, quiet=True)
        my_predbat.inverters.append(inverter)

    if aggregate:
        # Re-run the real accumulation over the inverters just built, so the fleet totals a test
        # asserts against are the ones the executor computes rather than ones the test invented.
        my_predbat.fetch_inverter_data(create=False)

    return my_predbat.inverters


def dummy_sleep(seconds):
    """
    Dummy sleep function so inverter writes don't stall the tests.
    """
    pass


def fleet_soc_percent(my_predbat):
    """
    Per-inverter SoC percentages, in inverter order.
    """
    return [inverter.soc_percent for inverter in my_predbat.inverters]


def fleet_spread(my_predbat):
    """
    Difference in percentage points between the fullest and emptiest inverter in the fleet.
    """
    socs = fleet_soc_percent(my_predbat)
    return max(socs) - min(socs)


def split_fleet_target(my_predbat, target_percent, is_charging=False, is_exporting=False):
    """
    Ask the executor to split a fleet SoC target across the fleet, without writing to the
    inverters, and report what that split would actually achieve.

    Returns (targets, spread, fleet_percent) where targets is the per-inverter target percentage,
    spread is the gap in percentage points those targets would leave between fullest and emptiest,
    and fleet_percent is the fleet SoC the split really reaches - which is not always the target
    that was asked for.
    """
    targets = [my_predbat.adjust_battery_target_multi(inverter, target_percent, is_charging, is_exporting, check=True) for inverter in my_predbat.inverters]
    reached_kwh = sum(target / 100.0 * inverter.soc_max for target, inverter in zip(targets, my_predbat.inverters))
    return targets, max(targets) - min(targets), calc_percent_limit(reached_kwh, my_predbat.soc_max)


def fleet_balance_power(fleet):
    """
    Highest fleet power in W deliverable while every inverter holds the same C-rate, and so
    stays in balance: min(P_i / E_i) x sum(E_j). Collapses to the fleet maximum when every
    inverter is matched, which is why a matched-pair test can never see this limit.
    """
    if isinstance(fleet, str):
        fleet = FLEET_FIXTURES[fleet]
    min_c_rate = min(member.rate_max / member.soc_max for member in fleet)
    return min_c_rate * sum(member.soc_max for member in fleet)

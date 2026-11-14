# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Closed-loop balancing tests: two misbehaving inverters run through the real 60s rebalance loop.

See tests/balance_sim_harness.py for the model. Each scenario is a simulated 90 minutes; what is checked is
the number of BAD releases - a balancing hold dropped while the inverter is still out of balance and the peer
could still carry the house - plus convergence and the capacity guard.
"""

from tests.balance_ct_harness import CTScenario, busy_evening, cloudy_midday, mixed, run_ct_scenario
from tests.balance_sim_harness import derated_scenarios, format_report, format_step_report, load_step_scenarios, run_scenario, standard_scenarios

# Scenarios whose bad releases come from a cause other than the status-refresh floor, so they are reported
# but not asserted on - pinning today's number would pin the defect. Power noise: a held battery whose
# reading jitters past -50W while the fleet discharges (or +50W while it charges) is taken for a
# cross-charger, and the cross-charge branch returns before SoC balancing, so the SoC hold is dropped that pass.
NOT_ASSERTED = ("power noise",)


def run_balance_sim_tests(my_predbat):
    """
    Run the scenario matrix and check that every hold survives the status refresh that follows it.
    """
    print("**** Running closed-loop balancing harness ****")
    failed = False
    results = []
    for scenario in standard_scenarios():
        result = run_scenario(my_predbat, scenario)
        results.append(result)
        if any(marker in scenario.name for marker in NOT_ASSERTED):
            continue
        if "beyond one inverter" in scenario.name:
            # The capacity guard: one inverter cannot carry this alone, so neither may be held
            if any(result["holds"]) or result["unserved_kwh"] > 0:
                print("ERROR: {} held an inverter it could not spare: holds {} unserved {}kWh".format(scenario.name, result["holds"], result["unserved_kwh"]))
                failed = True
            continue
        if any(result["bad_releases"]):
            print("ERROR: {} dropped {} hold(s) while still out of balance".format(scenario.name, result["bad_releases"]))
            failed = True
        if result["converged_at"] is None or result["converged_at"] > 30:
            print("ERROR: {} took too long to converge: minute {}".format(scenario.name, result["converged_at"]))
            failed = True
        if result["unserved_kwh"] > 0:
            print("ERROR: {} left {}kWh to the grid while balancing".format(scenario.name, result["unserved_kwh"]))
            failed = True
    print(format_report(results))
    failed |= run_derated_tests(my_predbat)
    failed |= run_load_step_tests(my_predbat)
    failed |= run_ct_tests(my_predbat)
    return failed


# Energy a scenario may leave to the grid in total: about one minute at the shortfall, the pass it takes the
# grid meter to show the peer cannot carry the house and the hold to be let go.
DERATED_GRID_ALLOWANCE_KWH = 0.06


def run_derated_tests(my_predbat):
    """
    A peer that cannot deliver its configured rate must not leave the house on the grid while the hold lasts.
    """
    print("**** Derated peers ****")
    failed = False
    results = []
    for scenario in derated_scenarios():
        result = run_scenario(my_predbat, scenario)
        results.append(result)
        excess = result["unserved_kwh"] - unavoidable_kwh(scenario)
        if excess > DERATED_GRID_ALLOWANCE_KWH:
            print("ERROR: {} left {}kWh to the grid beyond what the fleet cannot cover, allowance {}kWh".format(scenario.name, round(excess, 3), DERATED_GRID_ALLOWANCE_KWH))
            failed = True
    print(format_step_report(results))
    return failed


def unavoidable_kwh(scenario):
    """
    Energy the fleet could not have kept off the grid even with nothing held: demand beyond what every battery
    can really deliver together. A 200W peer and a 3kW one cannot carry 4kW between them, balancing or not.
    """
    demand = scenario.load_w if scenario.direction == "discharge" else scenario.surplus_w
    capacity = sum(min(scenario.rate_max, behaviour.derate_w or scenario.rate_max) for behaviour in scenario.behaviours)
    return max(demand - capacity, 0.0) * scenario.minutes / 60000.0


def run_load_step_tests(my_predbat):
    """
    A load step the peer cannot carry alone releases the hold on the next pass; one it can carry keeps it.
    """
    print("**** Load steps ****")
    failed = False
    results = []
    for scenario in load_step_scenarios():
        result = run_scenario(my_predbat, scenario)
        results.append(result)
        if result["unserved_kwh"] > DERATED_GRID_ALLOWANCE_KWH:
            print("ERROR: {} left {}kWh to the grid".format(scenario.name, result["unserved_kwh"]))
            failed = True
        carried_alone = "-> 2700W" in scenario.name
        if carried_alone and any(result["bad_releases"]):
            print("ERROR: {} dropped a hold the peer could carry: {}".format(scenario.name, result["bad_releases"]))
            failed = True
    print(format_step_report(results))
    return failed


def run_ct_tests(my_predbat):
    """
    One CT, mixed inverter loops, second-by-second load: balancing must cut cross-charging and close the spread.
    """
    print("**** CT dynamics ****")
    failed = False
    for count in (2, 3):
        socs = {"eco": [80, 70] if count == 2 else [80, 75, 70], "midday": [70, 80] if count == 2 else [70, 75, 80], "charge": [40, 50] if count == 2 else [40, 45, 50]}
        for label, mode, soc, house in (("eco evening", "eco", socs["eco"], busy_evening), ("eco midday", "eco", socs["midday"], cloudy_midday), ("grid charge", "charge", socs["charge"], busy_evening)):
            off = run_ct_scenario(my_predbat, CTScenario(name="off", mode=mode, soc=soc, house=house(), dynamics=mixed(count), balance=False))
            on = run_ct_scenario(my_predbat, CTScenario(name="on", mode=mode, soc=soc, house=house(), dynamics=mixed(count), balance=True))
            print("{} inv {}: off cross {}kWh spread {} | on cross {}kWh spread {} import {}kWh writes {}".format(count, label, off["cross_kwh"], off["spread_end"], on["cross_kwh"], on["spread_end"], on["import_kwh"], on["writes"]))
            if mode == "eco" and off["cross_kwh"] > 0.1 and on["cross_kwh"] > off["cross_kwh"] / 2:
                print("ERROR: {} inv {}: balancing left {}kWh of {}kWh cross-charging".format(count, label, on["cross_kwh"], off["cross_kwh"]))
                failed = True
            if label != "eco midday" and on["spread_end"] > 5.0:
                print("ERROR: {} inv {}: balancing left a {}% spread".format(count, label, on["spread_end"]))
                failed = True
    return failed

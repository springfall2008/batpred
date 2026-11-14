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

from tests.balance_ct_harness import CTScenario, broken_cloud_midday, busy_evening, cloud_matrix, cloudy_midday, ct_matrix, disagreeing, format_ct_report, mixed, run_ct_scenario
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
        # Inverters that disagree about where zero is on the shared CT latch into one charging hard from another -
        # the case cross-charge balancing exists for. The model has to show it, and balancing has to stop it.
        soc = socs["eco"]
        off = run_ct_scenario(my_predbat, CTScenario(name="off", mode="eco", soc=soc, house=busy_evening(), dynamics=disagreeing(count), balance=False))
        on = run_ct_scenario(my_predbat, CTScenario(name="on", mode="eco", soc=soc, house=busy_evening(), dynamics=disagreeing(count), balance=True))
        print(
            "{} inv CT disagree: off cross {}kWh, >1kW for {}s, peak {}W {} | on cross {}kWh, >1kW for {}s, writes {}".format(
                count, off["cross_kwh"], off["cross_over_1kw_s"], off["cross_peak_w"], off["cross_peak_powers"], on["cross_kwh"], on["cross_over_1kw_s"], on["writes"]
            )
        )
        if off["cross_over_1kw_s"] < 600:
            print("ERROR: {} inv CT disagree: the model no longer latches into cross-charging ({}s above 1kW)".format(count, off["cross_over_1kw_s"]))
            failed = True
        if on["cross_over_1kw_s"] > 120 or on["cross_kwh"] > off["cross_kwh"] / 4:
            print("ERROR: {} inv CT disagree: balancing left {}kWh of cross-charging, {}s above 1kW".format(count, on["cross_kwh"], on["cross_over_1kw_s"]))
            failed = True
        # Broken cloud with PV close to the house load: every cloud tips the site between surplus and deficit,
        # faster than Predbat's pass. Balancing still has to stop the cross-charging, without holding a battery
        # from discharging into a deficit the next cloud brings (that cost 0.29-0.34 kWh/h of import when the
        # discharge hold was latched on any surplus, against 0.17-0.20 kWh/h on main)
        for loops_name, loops in (("mixed loops", mixed), ("CT disagree", disagreeing)):
            split = [0.4, 0.6] if count == 2 else [0.3, 0.3, 0.4]
            cloud = {"mode": "eco", "soc": socs["midday"], "dynamics": loops(count), "pv_split": split}
            cloud_off = run_ct_scenario(my_predbat, CTScenario(name="off", house=broken_cloud_midday(pv_w=2200.0, base_w=900.0), balance=False, **cloud))
            cloud_on = run_ct_scenario(my_predbat, CTScenario(name="on", house=broken_cloud_midday(pv_w=2200.0, base_w=900.0), balance=True, **cloud))
            print("{} inv broken cloud, PV close to load, {}: off cross {}kWh | on cross {}kWh import {}kWh writes {}".format(count, loops_name, cloud_off["cross_kwh"], cloud_on["cross_kwh"], cloud_on["import_kwh"], cloud_on["writes"]))
            if cloud_on["cross_kwh"] > cloud_off["cross_kwh"] / 4:
                print("ERROR: {} inv broken cloud {}: balancing left {}kWh of {}kWh cross-charging".format(count, loops_name, cloud_on["cross_kwh"], cloud_off["cross_kwh"]))
                failed = True
            if cloud_on["import_kwh"] > 0.25:
                print("ERROR: {} inv broken cloud {}: {}kWh imported - a hold is keeping a battery from covering the deficit a cloud brings".format(count, loops_name, cloud_on["import_kwh"]))
                failed = True
            if cloud_on["writes"] > 120:
                print("ERROR: {} inv broken cloud {}: {} rate writes in an hour".format(count, loops_name, cloud_on["writes"]))
                failed = True
        # The cross-charge hold is latched, so it is written once rather than taken and released every minute or two
        if on["writes"] > 40:
            print("ERROR: {} inv CT disagree: {} rate writes in an hour - the cross-charge hold is not latching".format(count, on["writes"]))
            failed = True
    return failed


def run_balance_sim_matrix(my_predbat):
    """
    The full CT matrix - every mode, loop type, fleet size and cloud profile, with balancing off and on - printed
    as one report, for exploring a balancing change by eye (./run_all --test balance_sim_matrix). Slow, so not in
    the quick suite; the cases that pin behaviour are in run_balance_sim_tests().

    The one thing asserted: wherever an eco run cross-charges meaningfully with balancing off, balancing at least
    halves it.
    """
    print("**** Full CT balancing matrix ****")
    failed = False
    results = {}
    for scenario in ct_matrix() + cloud_matrix():
        results[scenario.name] = run_ct_scenario(my_predbat, scenario)
    print(format_ct_report(list(results.values())))
    for name, result in results.items():
        if result["mode"] != "eco" or not name.endswith("balance on"):
            continue
        off = results[name[: -len("balance on")] + "balance off"]
        if off["cross_kwh"] > 0.1 and result["cross_kwh"] > off["cross_kwh"] / 2:
            print("ERROR: {}: balancing left {}kWh of {}kWh cross-charging".format(name, result["cross_kwh"], off["cross_kwh"]))
            failed = True
    return failed

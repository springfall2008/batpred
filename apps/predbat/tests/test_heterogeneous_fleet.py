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
Heterogeneous multi-inverter fleet tests (#4856 workstream 0).

These exercise the fleet rig against the fleets in the open multi-inverter reports and pin down
what Predbat does with them today. Several of the expectations below record behaviour that #4856
argues is wrong - they are written as characterisation tests so that the later workstreams have
to change them deliberately, and so the size of each fault is visible in the diff when it is
fixed. Where that is the case the expectation says so and names the finding.
"""

from tests.fleet_rig import FLEET_FIXTURES, fleet_balance_power, fleet_spread, setup_fleet, split_fleet_target

# What each fixture aggregates to: (fleet soc_max kWh, per-inverter soc_max, per-inverter rate W).
# Guards the rig itself - a fleet whose members quietly collapse to the same capacity or the same
# rate would make every test below pass for the wrong reason, which is how F10 went unnoticed.
EXPECTED_FLEETS = {
    "matched_pair": (19.0, [9.5, 9.5], [2600, 2600]),
    "issue_830": (14.7, [9.5, 5.2], [2600, 2600]),
    "issue_4842": (30.11, [12.4, 9.52, 8.19], [2600, 2600, 3000]),
    "issue_3172": (28.5, [19.0, 9.5], [3000, 3000]),
}

# Target split from a perfectly balanced fleet at 50%: (fleet target %, per-inverter target %,
# spread in points, fleet % the split really reaches).
#
# Only matched_pair is correct. On every mixed fleet the split is by charge-rate share and never
# reads soc_max (F1), so it opens a spread from a balanced start, and once the smallest battery
# saturates the surplus is not redistributed and the fleet lands short of the target the plan was
# costed against (F2). A capacity-proportional split would give every inverter the fleet target
# exactly, zero spread, and reach the target on every row.
EXPECTED_TARGET_SPLIT = {
    "matched_pair": [
        (60, [60, 60], 0, 60),
        (70, [70, 70], 0, 70),
        (80, [80, 80], 0, 80),
        (90, [90, 90], 0, 90),
        (95, [95, 95], 0, 95),
        (100, [100.0, 100.0], 0.0, 100),
    ],
    "issue_830": [
        (60, [58, 64], 6, 60),
        (70, [65, 78], 13, 70),
        (80, [73, 92], 19, 80),
        (90, [81, 100], 19, 88),  # F2: asked for 90%, reaches 88%
        (95, [85, 100], 15, 90),  # F2: asked for 95%, reaches 90%
        (100, [100.0, 100.0], 0.0, 100),
    ],
    "issue_4842": [
        (60, [58, 60, 63], 5, 60),
        (70, [65, 70, 77], 12, 70),
        (80, [73, 80, 90], 17, 80),
        (90, [81, 90, 100], 19, 89),  # F2
        (95, [85, 95, 100], 15, 92),  # F2
        (100, [100.0, 100.0, 100.0], 0.0, 100),
    ],
    "issue_3172": [
        (60, [58, 65], 7, 60),
        (70, [65, 80], 15, 70),
        (80, [73, 95], 22, 80),
        (90, [80, 100], 20, 87),  # F2
        (95, [84, 100], 16, 89),  # F2
        (100, [100.0, 100.0], 0.0, 100),
    ],
}

# Highest power the fleet can deliver while every inverter holds the same C-rate, as a fraction of
# the fleet maximum: (P_balance W, P_max W, ratio). Every mixed fleet lands at 75-77%, which falls
# between the export ladder's 70% and 100% rungs, so the most useful point on the curve cannot
# currently be selected. A matched fleet lands at 100% and the whole property disappears, which is
# why none of this is visible from the existing tests.
EXPECTED_BALANCE_POWER = {
    "matched_pair": (5200, 5200, 1.000),
    "issue_830": (4023, 5200, 0.774),
    "issue_4842": (6313, 8200, 0.770),
    "issue_3172": (4500, 6000, 0.750),
}


def report(condition, message):
    """
    Print and count one failed expectation, returning True when it failed.
    """
    if condition:
        print("ERROR: {}".format(message))
        return True
    return False


def rates_watts(my_predbat):
    """
    Per-inverter charge rate ceiling in W, rounded, in inverter order.
    """
    return [round(inverter.battery_rate_max_charge * 60 * 1000) for inverter in my_predbat.inverters]


def zeroed_rates(services, kind):
    """
    Entity ids of the given rate kind ("charge" or "discharge") that were written to zero.
    """
    prefix = "number.{}_rate".format(kind)
    return [kwargs.get("entity_id") for service, kwargs in services if service == "number/set_value" and kwargs.get("entity_id", "").startswith(prefix) and kwargs.get("value") == 0]


def enable_balance(my_predbat, discharge=False, charge=False, crosscharge=False, threshold=1):
    """
    Set the balancer's effective config directly, as the balancer reads these attributes rather
    than re-reading the args.
    """
    my_predbat.balance_inverters_discharge = discharge
    my_predbat.balance_inverters_charge = charge
    my_predbat.balance_inverters_crosscharge = crosscharge
    my_predbat.balance_inverters_threshold_charge = threshold
    my_predbat.balance_inverters_threshold_discharge = threshold


def run_balance(my_predbat):
    """
    Run one balancer pass against the fleet and return the services it called.
    """
    ha = my_predbat.ha_interface
    ha.service_store_enable = True
    ha.get_service_store()
    my_predbat.balance_inverters(test_mode=True)
    services = ha.get_service_store()
    ha.service_store_enable = False
    return services


def test_fleet_fixtures(my_predbat):
    """
    Each fixture builds a fleet with the capacities and rates its issue reports.
    """
    print("Test: fleet fixtures build the reported fleets")
    failed = False
    for name, (soc_max, per_soc_max, per_rate) in EXPECTED_FLEETS.items():
        setup_fleet(my_predbat, name, soc=50)
        failed |= report(round(my_predbat.soc_max, 2) != soc_max, "{}: fleet soc_max {} should be {}".format(name, my_predbat.soc_max, soc_max))
        failed |= report([inverter.soc_max for inverter in my_predbat.inverters] != per_soc_max, "{}: per-inverter soc_max {} should be {}".format(name, [inverter.soc_max for inverter in my_predbat.inverters], per_soc_max))
        failed |= report(rates_watts(my_predbat) != per_rate, "{}: per-inverter rate {} should be {}".format(name, rates_watts(my_predbat), per_rate))
        failed |= report(fleet_spread(my_predbat) != 0, "{}: fleet set up at 50% should start balanced, spread is {}".format(name, fleet_spread(my_predbat)))
    return failed


def test_target_split(my_predbat):
    """
    Splitting a fleet SoC target across a mixed fleet opens a spread from a balanced start and
    undershoots the target once the smallest battery saturates (F1, F2).
    """
    print("Test: fleet target split across heterogeneous fleets")
    failed = False
    for name, expected in EXPECTED_TARGET_SPLIT.items():
        setup_fleet(my_predbat, name, soc=50)
        for target, want_targets, want_spread, want_reached in expected:
            targets, spread, reached = split_fleet_target(my_predbat, target)
            failed |= report(targets != want_targets, "{} at {}%: split {} should be {}".format(name, target, targets, want_targets))
            failed |= report(spread != want_spread, "{} at {}%: spread {} should be {}".format(name, target, spread, want_spread))
            failed |= report(reached != want_reached, "{} at {}%: fleet reaches {}% should be {}%".format(name, target, reached, want_reached))
    return failed


def test_balance_power_envelope(my_predbat):
    """
    The fleet's balance-preserving power ceiling, and the fact that a matched fleet has none.
    """
    print("Test: fleet balance power envelope")
    failed = False
    for name, (want_balance, want_max, want_ratio) in EXPECTED_BALANCE_POWER.items():
        balance_power = fleet_balance_power(name)
        max_power = sum(member.rate_max for member in FLEET_FIXTURES[name])
        failed |= report(round(balance_power) != want_balance, "{}: P_balance {} should be {}".format(round(balance_power), want_balance, name))
        failed |= report(max_power != want_max, "{}: P_max {} should be {}".format(name, max_power, want_max))
        failed |= report(round(balance_power / max_power, 3) != want_ratio, "{}: ratio {} should be {}".format(name, round(balance_power / max_power, 3), want_ratio))
    return failed


def test_balance_hair_trigger(my_predbat):
    """
    The balancer acts on the smallest imbalance the SoC percentage can express.

    out_of_balance is a plain integer inequality and both thresholds default to 1, so on the
    #3172 fleet a single point - 0.19 kWh of a 19 kWh battery - is enough to zero an inverter's
    discharge rate and hand its share of the house load to the grid (F4).
    """
    print("Test: balancer acts on a one point imbalance")
    setup_fleet(my_predbat, "issue_3172", soc=[50, 51], battery_power=1000, load_power=1000)
    enable_balance(my_predbat, discharge=True, threshold=1)
    services = run_balance(my_predbat)
    return report("number.discharge_rate" not in zeroed_rates(services, "discharge"), "one point of imbalance should have zeroed inverter 0's discharge rate, services were {}".format(services))


def test_force_export_balance_unreachable(my_predbat):
    """
    Discharge balancing is structurally unreachable during a force export (F6).

    can_power_house asks whether the rest of the fleet could carry the current battery power on
    its own. Under a force export every inverter is already at its max rate, so total battery
    power is the sum of the rates and removing any one of them always fails the test - however
    far out of balance the fleet is. The same fleet at the same spread does balance under an
    ordinary house load, which is what makes this a wrong predicate rather than a safe refusal.
    """
    print("Test: discharge balancing during force export")
    failed = False
    fleet = FLEET_FIXTURES["issue_4842"]
    export_power = [member.rate_max for member in fleet]

    # Force export: every inverter discharging flat out into the grid
    setup_fleet(my_predbat, "issue_4842", soc=[46, 100, 45], battery_power=export_power, load_power=0, grid_power=[-power for power in export_power])
    enable_balance(my_predbat, discharge=True, threshold=1)
    services = run_balance(my_predbat)
    failed |= report(zeroed_rates(services, "discharge"), "no discharge rate should be zeroed during a force export today (F6), services were {}".format(services))

    # Same fleet, same 55 point spread, ordinary house load - balancing does happen
    setup_fleet(my_predbat, "issue_4842", soc=[46, 100, 45], battery_power=1000, load_power=1000)
    enable_balance(my_predbat, discharge=True, threshold=1)
    services = run_balance(my_predbat)
    failed |= report(not zeroed_rates(services, "discharge"), "the same spread under house load should balance, services were {}".format(services))
    return failed


def test_heterogeneous_fleet(my_predbat):
    """
    Run the heterogeneous multi-inverter fleet tests.
    """
    print("**** Running heterogeneous fleet tests ****\n")
    failed = False
    for test in (test_fleet_fixtures, test_target_split, test_balance_power_envelope, test_balance_hair_trigger, test_force_export_balance_unreachable):
        failed |= test(my_predbat)
    return failed

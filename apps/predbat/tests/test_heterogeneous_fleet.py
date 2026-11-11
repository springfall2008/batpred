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

import random

from execute import split_change_by_rate
from tests.fleet_rig import FLEET_FIXTURES, fleet_balance_power, fleet_spread, setup_fleet, split_fleet_target
from utils import dp3

# What each fixture aggregates to: (fleet soc_max kWh, per-inverter soc_max, per-inverter rate W).
# Guards the rig itself - a fleet whose members quietly collapse to the same capacity or the same
# rate would make every test below pass for the wrong reason, which is how F10 went unnoticed.
EXPECTED_FLEETS = {
    "matched_pair": (19.0, [9.5, 9.5], [2600, 2600]),
    "issue_830": (14.7, [9.5, 5.2], [2600, 2600]),
    "issue_4842": (30.11, [12.4, 9.52, 8.19], [2600, 2600, 3000]),
    "issue_3172": (28.5, [19.0, 9.5], [3000, 3000]),
    # battery_scaling 0.8 on the 5.2 kWh battery: 5.2 x 0.8 = 4.16 kWh usable
    "issue_830_scaled": (13.66, [9.5, 4.16], [2600, 2600]),
    "aio_hybrid": (32.5, [13.5, 19.0], [6000, 4000]),
}

# Target split from a perfectly balanced fleet at 50%: (fleet target %, per-inverter target %,
# spread in points, fleet % the split really reaches).
#
# The fleet's change is split by charge-rate share so the inverters finish together (#1492, #1484), so
# on a mixed fleet it opens a spread from a balanced start, and that is deliberate. Balancing the
# inverters is not the aim of the split: they do not share load evenly, so a level SoC does not last,
# and a split that levels them (the same % on each) holds back an inverter that is ahead while another
# catches up, which is the premature pause #1484 describes.
#
# What an inverter has no room for is given to the others (#4856 F2). Before that, once the smallest
# battery saturated the surplus was lost and the fleet landed short of the target the plan was costed
# against. Only the 90% and 95% rows of the mixed fleets change, to the targets below; for the record the
# rows were: issue_830 90 [81,100] reaching 88, 95 [85,100] reaching 90; issue_4842 90 [81,90,100]
# reaching 89, 95 [85,95,100] reaching 92; issue_3172 90 [80,100] reaching 87, 95 [84,100] reaching 89.
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
        (90, [85, 100], 15, 90),
        (95, [92, 100], 8, 95),
        (100, [100.0, 100.0], 0.0, 100),
    ],
    "issue_4842": [
        (60, [58, 60, 63], 5, 60),
        (70, [65, 70, 77], 12, 70),
        (80, [73, 80, 90], 17, 80),
        (90, [82, 92, 100], 18, 90),
        (95, [88, 100, 100], 12, 95),
        (100, [100.0, 100.0, 100.0], 0.0, 100),
    ],
    "issue_3172": [
        (60, [58, 65], 7, 60),
        (70, [65, 80], 15, 70),
        (80, [73, 95], 22, 80),
        (90, [85, 100], 15, 90),
        (95, [92, 100], 8, 95),
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


def run_balance(my_predbat, owner="demand"):
    """
    Run one balancer pass against the fleet and return the services it called.

    Drives the path execute_plan takes: a rate intent with nothing claimed, owned by owner
    ("demand", "export" or "charge"), mutated by balance_inverter_rates() and applied once.
    """
    ha = my_predbat.ha_interface
    ha.service_store_enable = True
    ha.get_service_store()
    my_predbat.balance_inverters_enable = True
    my_predbat.set_read_only = False
    intent = {}
    for inverter in my_predbat.inverters:
        intent[inverter.id] = {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": owner}
    my_predbat.balance_inverter_rates(intent)
    for inverter in my_predbat.inverters:
        my_predbat.apply_inverter_rates(inverter, intent[inverter.id])
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
    Splitting a fleet SoC target across a mixed fleet is by rate share, so the inverters finish together,
    and what an inverter has no room for goes to the others so the fleet reaches the target (F2).
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


def test_target_split_saturation(my_predbat):
    """
    An inverter with no room is held at its limit and the others take the rest, so the fleet still reaches
    its target (F2), whether the limit is a full battery or one that has nothing left to give.
    """
    print("Test: fleet target split when an inverter has no room")
    failed = False

    # 9.5 + 5.2 kWh at 45% / 75% asked for 80%: the small battery's share is more than it has room for, so it is
    # filled and the large one takes the remainder. A split that levels them would send 80% to both
    setup_fleet(my_predbat, "issue_830", soc=[45, 75])
    targets, _spread, reached = split_fleet_target(my_predbat, 80)
    failed |= report(targets != [69, 100] or reached != 80, "issue_830 from 45/75 at 80%: expected [69, 100] reaching 80%, got {} reaching {}%".format(targets, reached))

    # Two equal batteries at 50% and 100% asked for 80%: only one has any room, so it takes all the change and the full
    # one stays at 100% (it does not discharge into the other)
    setup_fleet(my_predbat, "matched_pair", soc=[50, 100])
    targets, _spread, reached = split_fleet_target(my_predbat, 80)
    failed |= report(targets != [60, 100] or reached != 80, "matched_pair from 50/100 at 80%: expected [60, 100] reaching 80%, got {} reaching {}%".format(targets, reached))

    # A 6 kW and a 4 kW inverter from 50% to 90%: the faster one's share is more than its battery holds
    setup_fleet(my_predbat, "aio_hybrid", soc=50)
    targets, _spread, reached = split_fleet_target(my_predbat, 90)
    failed |= report(targets != [100, 83] or reached != 90, "aio_hybrid from 50% at 90%: expected [100, 83] reaching 90%, got {} reaching {}%".format(targets, reached))
    return failed


def test_target_split_ahead_is_not_held(my_predbat):
    """
    An inverter that is ahead of the fleet's SoC % but still has room keeps charging (#1484, fixed by #1492).

    Before #1492 every inverter was sent the same % target, so the fuller 9.5 kWh battery reached a step target
    while the 5.2 kWh was still at 17%, and Predbat paused it. The target sent to the inverter that is ahead must
    stay well above what it has now, and above the fleet target itself.
    """
    print("Test: fleet target split does not hold an inverter that is ahead")
    failed = False
    setup_fleet(my_predbat, "issue_830", soc=[61, 17])
    targets, _spread, reached = split_fleet_target(my_predbat, 62)
    failed |= report(targets != [74, 40] or reached != 62, "issue_830 at 61/17 with a 62% fleet target: expected [74, 40] reaching 62%, got {} reaching {}%".format(targets, reached))
    failed |= report(targets[0] <= 62, "the inverter ahead of the fleet % must be sent more than the fleet target of 62%, or it is paused while the other catches up (#1484), got {}%".format(targets[0]))
    return failed


def test_target_split_battery_scaling(my_predbat):
    """
    The split works in usable energy: battery_scaling reduces soc_max, so a 5.2 kWh battery that is 80% usable
    is treated as 4.16 kWh and the fleet target is a percentage of that.

    #830's own fleet - 9.5 kWh at 50% and 4.16 kWh usable at 70%, fleet target 66% - needs 1.35 kWh and neither
    inverter is taken past the target.
    """
    print("Test: fleet target split with battery scaling")
    setup_fleet(my_predbat, "issue_830_scaled", soc=[50, 70])
    targets, _spread, reached = split_fleet_target(my_predbat, 66)
    return report(targets != [57, 86] or reached != 66, "issue_830_scaled from 50/70 at 66%: expected [57, 86] reaching 66%, got {} reaching {}%".format(targets, reached))


def test_target_split_exporting(my_predbat):
    """
    An export is split by export rate and stops at each inverter's own reserve, the energy it cannot give
    being taken from the others. It used to be split by charge rate and the reserve only clamped the result, so
    the fleet delivered less than the plan assumed (F2).
    """
    print("Test: fleet target split for an export")
    # A backup battery with a 25% reserve next to a hybrid that can run down to 4%: the backup one stops at its reserve
    # and the hybrid goes lower to deliver the fleet target. Before this the split was [25, 39], leaving the fleet at 10.8 kWh against 6.5 kWh
    inverters = setup_fleet(my_predbat, "aio_hybrid", soc=80)
    for inverter, reserve_percent in zip(inverters, [25, 4]):
        inverter.reserve = dp3(inverter.soc_max * reserve_percent / 100.0)
    targets, _spread, reached = split_fleet_target(my_predbat, 20, False, True)
    return report(targets != [25, 16] or reached != 20, "aio_hybrid export from 80% to 20%: expected [25, 16] reaching 20%, got {} reaching {}%".format(targets, reached))


def test_target_split_exporting_rate(my_predbat):
    """
    A forced export is shared by export rate (inverter_limit_export), which is what the plan models and what the export
    power is allocated by, and any other discharge by discharge rate. The two are the same unless inverter_limit_export
    is set, and the rig fixtures leave both equal to the charge rate, so they are set here on the inverters themselves.
    """
    print("Test: fleet target split for an export follows the export rate")
    failed = False

    # Two equal batteries at 80% exporting down to a 50% fleet target, which takes 5.7 kWh from the pair
    # (description, export rates W, discharge rates W, is_exporting, expected targets)
    cases = [
        ("a forced export follows the export rate, 3 kW / 1 kW share it 75% / 25%", [3000, 1000], [2600, 2600], True, [35, 65]),
        ("other discharge does not use the export rate", [3000, 1000], [2600, 2600], False, [50, 50]),
        ("a forced export does not use the discharge rate", [2600, 2600], [3000, 1000], True, [50, 50]),
        ("other discharge follows the discharge rate", [2600, 2600], [3000, 1000], False, [35, 65]),
        ("an inverter limited to an export rate of 0 is given none of the export", [0, 2600], [2600, 2600], True, [80, 20]),
    ]
    for description, export_watts, discharge_watts, is_exporting, expected in cases:
        inverters = setup_fleet(my_predbat, "matched_pair", soc=80)
        for inverter, export_rate, discharge_rate in zip(inverters, export_watts, discharge_watts):
            inverter.battery_rate_max_export = export_rate / 60000.0
            inverter.battery_rate_max_discharge = discharge_rate / 60000.0
        targets, _spread, reached = split_fleet_target(my_predbat, 50, False, is_exporting)
        failed |= report(targets != expected or reached != 50, "{}: expected {} reaching 50%, got {} reaching {}%".format(description, expected, targets, reached))
    return failed


def test_split_change_by_rate(my_predbat):
    """
    The split itself, on plain numbers: (soc_kw, soc_max, reserve, rate) per inverter.
    """
    print("Test: split_change_by_rate")
    failed = False
    # (name, members, fleet change in kWh, expected per-inverter kWh)
    cases = [
        ("no change", [(5.0, 10.0, 0.0, 2.0), (5.0, 10.0, 0.0, 2.0)], 0.0, [5.0, 5.0]),
        ("equal rates, equal shares", [(5.0, 10.0, 0.0, 2.0), (5.0, 10.0, 0.0, 2.0)], 2.0, [6.0, 6.0]),
        ("shares follow the rates", [(0.0, 10.0, 0.0, 3.0), (0.0, 10.0, 0.0, 1.0)], 4.0, [3.0, 1.0]),
        ("charge: one fills, the other takes the rest", [(8.0, 10.0, 0.0, 2.0), (0.0, 10.0, 0.0, 2.0)], 6.0, [10.0, 4.0]),
        ("export: one stops at its reserve, the other gives the rest", [(8.0, 10.0, 5.0, 2.0), (8.0, 10.0, 0.0, 2.0)], -8.0, [5.0, 3.0]),
        ("more than there is room for takes each to its limit", [(9.0, 10.0, 0.0, 2.0), (9.0, 10.0, 0.0, 2.0)], 5.0, [10.0, 10.0]),
        ("an inverter with no rate gets nothing", [(0.0, 10.0, 0.0, 0.0), (0.0, 10.0, 0.0, 2.0)], 3.0, [0.0, 3.0]),
    ]
    for name, members, change, expected in cases:
        got = [round(kwh, 6) for kwh in split_change_by_rate(change, members)]
        failed |= report(got != expected, "{}: expected {} got {}".format(name, expected, got))
    return failed


def test_split_change_by_rate_invariants(my_predbat):
    """
    Properties of split_change_by_rate() that must hold for any fleet, over a fixed set of random fleets of 1 to 4 inverters:

    - no inverter goes above its capacity or below its reserve, and each moves in the direction of the change
    - the whole change is delivered whenever the fleet has room for it
    - when no inverter reaches a limit the result is exactly the plain split by rate share, as it was before F2

    The generator is seeded and local to the test, so every run uses the same fleets and nothing else's random numbers are touched.
    A failure prints the fleet and the change so it can be reproduced.
    """
    print("Test: split_change_by_rate invariants over random fleets")
    failed = False
    generator = random.Random(1484)
    reported = 0
    for _case in range(2000):
        members = []
        for _inverter in range(generator.randint(1, 4)):
            capacity = generator.choice([4.16, 5.2, 8.19, 9.5, 12.4, 13.5, 19.0])
            reserve = capacity * generator.choice([0.0, 0.04, 0.25])
            soc_kw = max(capacity * generator.random(), reserve * generator.random())
            members.append((soc_kw, capacity, reserve, generator.choice([2.4, 2.6, 3.0, 4.0, 6.0])))
        change = generator.uniform(-1.0, 1.0) * sum(member[1] for member in members) * 0.8
        result = split_change_by_rate(change, members)

        problems = []
        for new_kwh, (soc_kw, capacity, reserve, _rate) in zip(result, members):
            if new_kwh > capacity + 1e-9:
                problems.append("went above its capacity")
            if change < 0 and new_kwh < min(reserve, soc_kw) - 1e-9:
                problems.append("went below its reserve")
            if (change >= 0 and new_kwh < soc_kw - 1e-9) or (change < 0 and new_kwh > soc_kw + 1e-9):
                problems.append("moved against the change")
        room = sum(max(capacity - soc_kw, 0.0) if change >= 0 else max(soc_kw - reserve, 0.0) for soc_kw, capacity, reserve, _rate in members)
        delivered = sum(new_kwh - member[0] for new_kwh, member in zip(result, members))
        if abs(change) <= room - 1e-9 and abs(delivered - change) > 1e-6:
            problems.append("did not deliver the whole change of {} kWh (delivered {})".format(round(change, 3), round(delivered, 3)))
        total_rate = sum(member[3] for member in members)
        plain = [soc_kw + change * rate / total_rate for soc_kw, _capacity, _reserve, rate in members]
        if all(reserve - 1e-9 <= kwh <= capacity + 1e-9 for kwh, (_soc_kw, capacity, reserve, _rate) in zip(plain, members)):
            if any(abs(new_kwh - plain_kwh) > 1e-6 for new_kwh, plain_kwh in zip(result, plain)):
                problems.append("differs from the plain rate split although no inverter reaches a limit")

        if problems:
            failed = True
            if reported < 5:
                reported += 1
                print("ERROR: {} for change {} kWh on fleet (soc_kw, soc_max, reserve, rate) {}".format("; ".join(problems), round(change, 3), [tuple(round(value, 3) for value in member) for member in members]))
    return failed


def test_balance_power_envelope(my_predbat):
    """
    The fleet's balance-preserving power ceiling, and the fact that a matched fleet has none.

    This pins the rig's own fleet_balance_power() arithmetic, the reference a balance-preserving
    allocator would be checked against. No Predbat code runs here.
    """
    print("Test: fleet balance power envelope")
    failed = False
    for name, (want_balance, want_max, want_ratio) in EXPECTED_BALANCE_POWER.items():
        balance_power = fleet_balance_power(name)
        max_power = sum(member.rate_max for member in FLEET_FIXTURES[name])
        failed |= report(round(balance_power) != want_balance, "{}: P_balance {} should be {}".format(name, round(balance_power), want_balance))
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

    The discharge hold in utils.balance_inverters() asks whether the other inverters' usable
    discharge rate (less 200W) could carry the current battery power on their own. Under a force
    export every inverter is already at its max rate, so total battery power is the sum of the
    rates and removing any one of them always fails that check - however far out of balance the
    fleet is. The same fleet at the same spread does balance under an
    ordinary house load, which is what makes this a wrong predicate rather than a safe refusal.
    """
    print("Test: discharge balancing during force export")
    failed = False
    fleet = FLEET_FIXTURES["issue_4842"]
    export_power = [member.rate_max for member in fleet]

    # Force export: every inverter discharging flat out into the grid (grid power is +ve export),
    # with the executor's intent owned by the export
    setup_fleet(my_predbat, "issue_4842", soc=[46, 100, 45], battery_power=export_power, load_power=0, grid_power=export_power)
    enable_balance(my_predbat, discharge=True, threshold=1)
    services = run_balance(my_predbat, owner="export")
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
    for test in (
        test_fleet_fixtures,
        test_target_split,
        test_target_split_saturation,
        test_target_split_ahead_is_not_held,
        test_target_split_battery_scaling,
        test_target_split_exporting,
        test_target_split_exporting_rate,
        test_split_change_by_rate,
        test_split_change_by_rate_invariants,
        test_balance_power_envelope,
        test_balance_hair_trigger,
        test_force_export_balance_unreachable,
    ):
        failed |= test(my_predbat)
    return failed

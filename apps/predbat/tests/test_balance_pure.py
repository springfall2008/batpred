# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

import random

from utils import balance_inverters


def make_snapshot(soc_percent, battery_power, charge_rate_now=2600.0, discharge_rate_now=2600.0, reserve_percent=4.0, pv_power=0.0, rate_max=2600.0, in_calibration=False, grid_power=0.0):
    """
    Build one inverter's snapshot entry with sensible defaults.

    Returns:
    - dict: the plain per-inverter readings balance_inverters() consumes
    """
    return {
        "soc_percent": soc_percent,
        "reserve_percent": reserve_percent,
        "battery_power": battery_power,
        "pv_power": pv_power,
        "grid_power": grid_power,
        "charge_rate_now": charge_rate_now,
        "discharge_rate_now": discharge_rate_now,
        "battery_rate_max_charge": rate_max,
        "battery_rate_max_discharge": rate_max,
        "in_calibration": in_calibration,
    }


def make_intent(count, overrides=None):
    """
    Build a default intent for count inverters, where nothing has been claimed.

    Returns:
    - dict: inverter id -> rate intent
    """
    overrides = overrides or {}
    intent = {}
    for inverter_id in range(count):
        entry = {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"}
        entry.update(overrides.get(inverter_id, {}))
        intent[inverter_id] = entry
    return intent


def test_freeze_hold_is_not_raised():
    """
    F5 / #829 regression: a deliberate rate 0 hold must survive balancing.

    The old reset loop raised any zero rate to max whenever another inverter was non-zero, which
    is how freeze charge got undone on the 22 inverter types without timed pause.
    """
    intent = make_intent(2, {0: {"discharge_rate": 0, "owner": "freeze"}})
    snapshot = [make_snapshot(50.0, 1000.0, discharge_rate_now=0.0), make_snapshot(50.0, 1000.0)]
    balance_inverters(intent, snapshot, False, True, True, 1.0, 1.0)
    assert intent[0]["discharge_rate"] == 0, "a deliberate hold must not be raised to max"


def test_calibration_blocks_all_balancing():
    """
    Any inverter in calibration disables balancing entirely, matching the old return False.
    """
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(80.0, 1000.0), make_snapshot(20.0, -1000.0, in_calibration=True)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent == before, "calibration must leave intent untouched"


def test_crosscharge_during_discharge_stops_the_charging_inverter():
    """
    Inverter 1 is charging while the fleet is net discharging, and is the lower of the two,
    so it is the one stopped rather than throttling the inverter doing the work.
    """
    intent = make_intent(2)
    snapshot = [make_snapshot(80.0, 2000.0), make_snapshot(20.0, -500.0)]
    balance_inverters(intent, snapshot, False, False, True, 1.0, 1.0)
    assert intent[1]["charge_rate"] == 0, "the cross-charging inverter must be stopped"


def test_crosscharge_off_leaves_it_alone():
    """
    The same fleet with cross-charge balancing disabled must not be touched.
    """
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(80.0, 2000.0), make_snapshot(20.0, -500.0)]
    balance_inverters(intent, snapshot, False, False, False, 1.0, 1.0)
    assert intent == before, "cross-charge balancing off must be a no-op"


def test_balanced_fleet_is_left_alone():
    """
    Equal SoC means nothing to do, whatever the switches say.
    """
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(50.0, 1000.0), make_snapshot(50.0, 1000.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent == before, "a balanced fleet must not be touched"


def test_rebalance_returns_to_intent_not_max():
    """
    Once balanced, balancing against a fresh snapshot must leave the executor's planned rate in
    place. The old code restored to max here, which is what broke low power charging and freeze.
    """
    intent = make_intent(2, {0: {"charge_rate": 1200, "owner": "charge"}, 1: {"charge_rate": 1200, "owner": "charge"}})
    snapshot = [make_snapshot(50.0, -1000.0, charge_rate_now=1200.0), make_snapshot(50.0, -1000.0, charge_rate_now=1200.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent[0]["charge_rate"] == 1200, "a balanced fleet must keep the planned rate, not jump to max"
    assert intent[1]["charge_rate"] == 1200


def test_discharge_balance_stops_the_low_inverter():
    """
    During discharge the inverter that is lower on SoC stops discharging so the fuller one carries
    the house and the two converge.
    """
    intent = make_intent(2)
    snapshot = [make_snapshot(20.0, 500.0), make_snapshot(80.0, 500.0)]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent[0]["discharge_rate"] == 0, "the lower inverter must stop discharging"
    assert intent[1]["discharge_rate"] is None, "the fuller inverter must be left to carry the load"


def test_threshold_suppresses_a_small_divergence():
    """
    A divergence below the threshold is not worth acting on.
    """
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(49.0, 500.0), make_snapshot(50.0, 500.0)]
    balance_inverters(intent, snapshot, True, True, True, 5.0, 5.0)
    assert intent == before, "a divergence under the threshold must be ignored"


def test_single_inverter_is_a_no_op():
    """
    A fleet of one can never be out of balance.
    """
    intent = make_intent(1)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(50.0, 1000.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent == before, "a single inverter must not be touched"


def test_only_one_direction_of_pause_per_pass():
    """
    A pass must never pause a charger and a discharger at the same time.

    The inverters share an AC bus, so the four are coupled: pausing one of each changes the net
    balance in both directions at once and the remaining units simply absorb or supply the
    difference. Each guard is also evaluated as though its own pause were the only change.

    Fleet net discharging with a cross-charger present: only the cross-charger is paused.
    """
    intent = make_intent(4)
    snapshot = [
        make_snapshot(20.0, 1000.0),  # low and discharging - would be an SoC-balance candidate
        make_snapshot(80.0, 1000.0),
        make_snapshot(50.0, -500.0),  # charging while the fleet discharges - the real problem
        make_snapshot(50.0, 1000.0),
    ]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent[2]["charge_rate"] == 0, "the cross-charger must be stopped"
    paused_discharge = [id for id, value in intent.items() if value["discharge_rate"] == 0]
    assert paused_discharge == [], "no discharge may be paused in the same pass as a charge, got {}".format(paused_discharge)


def test_every_cross_charger_is_stopped():
    """
    More than one inverter can be going against the fleet; all of them are stopped, and they are
    all in the same direction so there is no coupling between the actions.
    """
    intent = make_intent(4)
    snapshot = [make_snapshot(50.0, 2000.0), make_snapshot(50.0, -500.0), make_snapshot(50.0, -500.0), make_snapshot(50.0, 2000.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent[1]["charge_rate"] == 0
    assert intent[2]["charge_rate"] == 0
    assert intent[0]["discharge_rate"] is None
    assert intent[3]["discharge_rate"] is None


def test_soc_balance_runs_only_when_nothing_is_cross_charging():
    """
    With no inverter going against the fleet, SoC balancing is free to act in the fleet's own
    direction - this is the case the discharge branch exists for.
    """
    intent = make_intent(3)
    snapshot = [make_snapshot(20.0, 1000.0), make_snapshot(80.0, 1000.0), make_snapshot(80.0, 1000.0)]
    balance_inverters(intent, snapshot, False, True, True, 1.0, 1.0)
    assert intent[0]["discharge_rate"] == 0, "the lowest inverter should stop discharging"
    assert intent[1]["discharge_rate"] is None
    assert intent[2]["discharge_rate"] is None


def test_pauses_are_applied_cumulatively_against_the_rate_guard():
    """
    Two low inverters could each pass the rate check alone while failing it together.

    can_power_house asks "if I stop this one, can the rest cover?" - so pausing two of them in one
    pass leaves the house short even though both guards passed. Later pauses must account for the
    earlier ones.

    Fleet of three at 2600W each drawing 5000W: stopping one leaves 5200 - 2600 - 200 = 2400W of
    headroom, which is under the 5000W draw, so nothing may be paused at all.
    """
    intent = make_intent(3)
    snapshot = [
        make_snapshot(20.0, 1700.0, discharge_rate_now=2600.0),
        make_snapshot(20.0, 1700.0, discharge_rate_now=2600.0),
        make_snapshot(80.0, 1600.0, discharge_rate_now=2600.0),
    ]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    paused = [id for id, value in intent.items() if value["discharge_rate"] == 0]
    assert len(paused) <= 1, "pausing two dischargers must not both pass a guard computed for one, got {}".format(paused)


def test_outcome_does_not_depend_on_fleet_ordering():
    """
    F7: partner selection used to be a fixed (i + 1) % n ring, so each inverter was judged against
    its index neighbour rather than against the fleet. Which inverter got held therefore depended
    on the order they happened to be configured in.

    Same logical fleet, two different index orderings - the same logical inverters must be held.
    """
    # Only "full" is above its reserve, so whether a low inverter may be held depends on whether
    # ANY other inverter can carry the load - not on whichever one happens to sit next in the list.
    fleet = [("lowA", 20.0, 18.0), ("full", 80.0, 4.0), ("lowC", 25.0, 22.0)]
    results = []
    for order in ([0, 1, 2], [0, 2, 1], [2, 1, 0]):
        names = [fleet[position][0] for position in order]
        intent = make_intent(3)
        snapshot = [make_snapshot(fleet[position][1], 800.0, reserve_percent=fleet[position][2]) for position in order]
        balance_inverters(intent, snapshot, False, True, False, 5.0, 5.0)
        held = {names[id] for id, value in intent.items() if value["discharge_rate"] == 0}
        results.append((order, held))
    first = results[0][1]
    for order, held in results[1:]:
        assert held == first, "ordering {} held {} but ordering {} held {}".format(results[0][0], first, order, held)


def test_surplus_pv_is_not_thrown_away():
    """
    A PV surplus means the fleet SHOULD be charging, so a charging inverter is doing the right
    thing even when the batteries happen to net out as discharging.

    Sunny export window: 7.3kW of PV against a 283W house load, so ~7kW is going spare. Three
    inverters are correctly soaking some of it up while a fourth discharges into the surplus.
    Choosing direction from the sign of total battery power alone blames the three chargers and
    drops PV absorption to zero. The actual anomaly is the inverter discharging into a surplus.
    """
    intent = make_intent(4)
    # Predbat grid convention: POSITIVE is export. spare PV = grid - net battery = 8708 - 1723 = 6985W
    snapshot = [
        make_snapshot(50.0, -781.0, rate_max=3600.0, grid_power=8708.0),
        make_snapshot(50.0, -437.0, rate_max=1000.0),
        make_snapshot(50.0, -517.0, rate_max=3600.0),
        make_snapshot(50.0, 3459.0, rate_max=5000.0),
    ]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    for inverter_id in (0, 1, 2):
        assert intent[inverter_id]["charge_rate"] is None, "inverter {} was absorbing surplus PV and must not be held".format(inverter_id)
    assert intent[3]["discharge_rate"] == 0, "the inverter discharging into a PV surplus is the anomaly"


def test_grid_charge_is_not_mistaken_for_cross_charging():
    """
    Importing to charge the whole fleet is a planned charge, not cross-charging - nothing is
    working against the fleet, so nothing may be held.
    """
    intent = make_intent(2)
    snapshot = [make_snapshot(50.0, -2000.0, grid_power=-4000.0), make_snapshot(50.0, -2000.0)]  # negative grid = importing to charge
    before = {key: dict(value) for key, value in intent.items()}
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert intent == before, "a planned grid charge must not be disturbed"


def build_random_fleet(rng):
    """
    Build one physically consistent random fleet.

    PV and load are FLEET quantities and grid closes the balance, because that is the only part of
    the picture that can be trusted: a per-inverter load reading is skewed by the other inverters
    (one discharging looks like less load to its neighbour), PV wiring is not declared in apps.yaml,
    and an AC-coupled unit has no PV of its own. Predbat's grid convention is positive for export.

        load = total_pv + total_battery_power - total_grid

    Returns:
    - tuple: (snapshot, intent, total_pv, total_load, total_grid, net_battery)
    """
    count = rng.randint(2, 6)
    snapshot = []
    for _ in range(count):
        rate_max = rng.choice([1000.0, 2600.0, 3600.0, 5000.0])
        soc = rng.uniform(0.0, 100.0)
        roll = rng.random()
        if roll < 0.40:
            battery_power = -rng.uniform(50.0, rate_max)
        elif roll < 0.85:
            battery_power = rng.uniform(50.0, rate_max)
        else:
            battery_power = rng.uniform(-49.0, 49.0)
        snapshot.append(
            make_snapshot(
                soc,
                battery_power,
                charge_rate_now=rate_max,
                discharge_rate_now=rate_max,
                reserve_percent=rng.uniform(0.0, min(20.0, soc)),
                rate_max=rate_max,
            )
        )
    total_pv = rng.choice([0.0, rng.uniform(0.0, 8000.0)])
    total_load = rng.uniform(0.0, 10000.0)
    net_battery = sum(entry["battery_power"] for entry in snapshot)
    total_grid = total_pv + net_battery - total_load
    snapshot[0]["grid_power"] = total_grid
    return snapshot, make_intent(count), total_pv, total_load, total_grid, net_battery


def test_soc_balancing_still_runs_when_crosscharge_correction_is_off():
    """
    Turning cross-charge correction off must not silently disable SoC balancing too.

    An inverter working against the fleet is a reason to skip SoC balancing only because holding
    it is the action we are taking instead. With that switch off we hold nothing in the charge
    direction, so holding a low discharger is still a single-direction pass and remains allowed.
    """
    intent = make_intent(3)
    snapshot = [
        make_snapshot(20.0, 1000.0),  # low and discharging - the SoC-balance candidate
        make_snapshot(80.0, 1000.0),
        make_snapshot(50.0, -300.0),  # charging against the fleet, but correction is off
    ]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent[2]["charge_rate"] is None, "cross-charge correction is off, so the charger must be left alone"
    assert intent[0]["discharge_rate"] == 0, "SoC balancing was enabled and must still act"


def test_charge_balance_stops_the_high_inverter():
    """
    The charge-side mirror of test_discharge_balance_stops_the_low_inverter.

    While the fleet charges, the inverter that is further ahead stops taking charge so the one
    behind catches up. Until this test existed the entire charge branch could be deleted without
    a single pure test noticing.
    """
    intent = make_intent(2)
    snapshot = [make_snapshot(80.0, -1000.0), make_snapshot(20.0, -1000.0)]
    balance_inverters(intent, snapshot, True, False, False, 1.0, 1.0)
    assert intent[0]["charge_rate"] == 0, "the fuller inverter must stop charging"
    assert intent[1]["charge_rate"] is None, "the inverter behind must be left to catch up"


def test_charge_holds_are_applied_cumulatively_against_the_pv_guard():
    """
    The charge-side counterpart of the cumulative rate guard.

    The PV check asks "if I stop this one, can the rest still absorb the surplus?", so holding two
    in one pass can strand PV even though both checks passed individually.

    Three inverters charging at 1000W each with no grid flow, so spare PV is 3000W, against 2600W
    of charge capacity each (7800W total). Holding one leaves 5200W of capacity, which still
    covers the surplus; holding a second would leave 2600W, which does not.

    Note the guard reads spare_pv - derived from grid and battery totals - not the per-inverter
    pv_power field, which cannot be trusted to attribute PV to an inverter.
    """
    intent = make_intent(3)
    snapshot = [
        make_snapshot(80.0, -1000.0),
        make_snapshot(80.0, -1000.0),
        make_snapshot(20.0, -1000.0),
    ]
    balance_inverters(intent, snapshot, True, False, False, 1.0, 1.0)
    held = [id for id, value in intent.items() if value["charge_rate"] == 0]
    assert len(held) == 1, "holding two chargers would strand the PV, got {}".format(held)


def test_the_soc_switches_gate_independently():
    """
    The charge and discharge switches must gate their own direction only.

    Enabling charge balancing must not act during a discharge, and vice versa - otherwise a user
    who turned on one would silently get the other.
    """
    # Charge balancing on, fleet discharging out of balance - nothing may happen
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(20.0, 1000.0), make_snapshot(80.0, 1000.0)]
    balance_inverters(intent, snapshot, True, False, False, 1.0, 1.0)
    assert intent == before, "charge balancing must not act while the fleet is discharging"

    # Discharge balancing on, fleet charging out of balance - nothing may happen
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(80.0, -1000.0), make_snapshot(20.0, -1000.0)]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent == before, "discharge balancing must not act while the fleet is charging"


def test_charge_threshold_suppresses_a_small_divergence():
    """
    The charge-side mirror of test_threshold_suppresses_a_small_divergence, which only ever
    exercised threshold_discharge because its fleet was discharging.

    balance_inverters_threshold_charge is a user-facing setting that could have been ignored
    entirely with every test still green. The thresholds are deliberately different here so it is
    unambiguous which one is under test.
    """
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(50.0, -1000.0), make_snapshot(51.0, -1000.0)]
    balance_inverters(intent, snapshot, True, False, False, 5.0, 1.0)
    assert intent == before, "a 1% divergence under a 5% charge threshold must be ignored"

    # The same fleet with the threshold lowered does act, so the test cannot pass by inaction
    intent = make_intent(2)
    balance_inverters(intent, snapshot, True, False, False, 1.0, 1.0)
    assert intent[1]["charge_rate"] == 0, "the fuller inverter must be held once the divergence clears the threshold"


def test_discharge_hold_needs_another_inverter_above_reserve():
    """
    Holding a low inverter's discharge only makes sense if somebody else has the energy to take
    over the house. If every other inverter is down at its reserve, holding this one just moves
    the shortfall onto the grid.

    Deleting that guard changed nothing in the suite before this test existed.
    """
    # Inverter 1 is the fuller one but sits on its reserve, so it cannot take over
    intent = make_intent(2)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [make_snapshot(20.0, 1000.0, reserve_percent=4.0), make_snapshot(80.0, 1000.0, reserve_percent=78.0)]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent == before, "nothing may be held when no other inverter is above its reserve"

    # Same fleet, but the fuller inverter now has energy to spare - the hold goes ahead
    intent = make_intent(2)
    snapshot = [make_snapshot(20.0, 1000.0, reserve_percent=4.0), make_snapshot(80.0, 1000.0, reserve_percent=4.0)]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent[0]["discharge_rate"] == 0, "the low inverter must be held once another can carry the house"


def test_capacity_guard_uses_the_rates_about_to_be_applied():
    """
    The capacity guard has to reason about the rates that will be in force after this pass, not
    the ones currently read from hardware.

    Balancing runs before the apply pass, so on a transition to lower rates - an export allocation
    stepping down, say - the measured rates overstate what the fleet will actually be able to
    deliver. Holding one inverter on that basis leaves the rest applying smaller rates and the
    fleet short.

    Measured 2600W each (7800W) but the executor is about to apply 1000W each (3000W), against a
    2000W draw. On the measured rates a hold looks fine; on the intended ones it does not.
    """
    intent = make_intent(3, {id: {"discharge_rate": 1000, "owner": "export"} for id in range(3)})
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [
        make_snapshot(20.0, 1000.0, discharge_rate_now=2600.0),
        make_snapshot(80.0, 500.0, discharge_rate_now=2600.0),
        make_snapshot(80.0, 500.0, discharge_rate_now=2600.0),
    ]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent == before, "the hold must be refused against the rates about to be applied"

    # With nothing claimed the fleet really will run at max, and the same hold is fine
    intent = make_intent(3)
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent[0]["discharge_rate"] == 0, "with max rates in force the hold is within capacity"


def test_a_planned_export_is_not_cancelled_by_its_own_pv_surplus():
    """
    A planned export must survive the PV surplus it is exporting into.

    Deciding direction from the site energy balance alone reads a sunny export window as "the
    fleet should be charging", because grid export exceeds what the batteries are supplying. Every
    inverter carrying out the planned export then looks like an anomaly and gets held - cancelling
    the export, and the 60s poll repeats that until the next plan run.

    The executor already knows what the fleet is meant to be doing. Where it has claimed rates the
    owner is authoritative; the energy balance is only a fallback for demand and idle.
    """
    intent = {id: {"charge_rate": 0, "discharge_rate": 2600, "pause_charge": False, "pause_discharge": False, "owner": "export"} for id in range(2)}
    snapshot = [
        make_snapshot(90.0, 1500.0, grid_power=9500.0),
        make_snapshot(90.0, 1500.0),
    ]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    for inverter_id in (0, 1):
        assert intent[inverter_id]["discharge_rate"] == 2600, "inverter {} is carrying out a planned export and must not be held".format(inverter_id)


def test_a_planned_charge_is_not_cancelled_by_the_energy_balance():
    """
    The mirror: during a planned charge an inverter that is charging is doing what it was told,
    whatever the site balance happens to read.
    """
    intent = {id: {"charge_rate": 2600, "discharge_rate": 0, "pause_charge": False, "pause_discharge": False, "owner": "charge"} for id in range(2)}
    snapshot = [
        make_snapshot(30.0, -1500.0, grid_power=-3500.0),
        make_snapshot(30.0, -1500.0),
    ]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    for inverter_id in (0, 1):
        assert intent[inverter_id]["charge_rate"] == 2600, "inverter {} is carrying out a planned charge and must not be held".format(inverter_id)


def test_discharge_capacity_counts_only_peers_above_reserve():
    """
    The energy check and the capacity sum must be about the SAME peers.

    "somebody is above reserve" and "the fleet has rate headroom" can be satisfied by two different
    inverters: one peer holds energy but almost no discharge rate, while the peer supplying the
    counted rate is sitting on its reserve and has nothing to give. Holding the low inverter then
    leaves only unusable capacity behind and the shortfall comes off the grid.

    Peer 1 is above reserve but can only manage 100W; peer 2 has 2600W of rate but is at reserve.
    """
    intent = make_intent(3)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [
        make_snapshot(20.0, 500.0, discharge_rate_now=2600.0, reserve_percent=4.0),
        make_snapshot(80.0, 100.0, discharge_rate_now=100.0, reserve_percent=4.0, rate_max=100.0),
        # High SoC so it is not itself a candidate, but sitting on its reserve so it has nothing
        # to give - its 2600W of rate must not count as capacity that could take over
        make_snapshot(80.0, 500.0, discharge_rate_now=2600.0, reserve_percent=78.0),
    ]
    balance_inverters(intent, snapshot, False, True, False, 1.0, 1.0)
    assert intent == before, "the only peer above reserve cannot carry the load, so nothing may be held"


def test_charge_capacity_counts_only_peers_below_full():
    """
    The charge-side counterpart: a battery already at 100% contributes no usable absorption, so
    its rate must not be counted towards the capacity left to soak up the PV surplus.

    Peer 1 is below full but tiny; peer 2 has the capacity but is full.
    """
    intent = make_intent(3)
    before = {key: dict(value) for key, value in intent.items()}
    snapshot = [
        make_snapshot(80.0, -500.0, charge_rate_now=2600.0),
        make_snapshot(20.0, -100.0, charge_rate_now=100.0, rate_max=100.0),
        # Full, and idle so it is not itself a candidate - its 2600W of rate absorbs nothing
        make_snapshot(100.0, 0.0, charge_rate_now=2600.0),
    ]
    balance_inverters(intent, snapshot, True, False, False, 1.0, 1.0)
    assert intent == before, "the only peer below full cannot absorb the surplus, so nothing may be held"


def test_random_fleets_hold_the_physical_invariants():
    """
    Property test over random fleets of 2-6 inverters in random charge/discharge states, with
    random SoC, random PV and random house load.

    Three things must hold however the fleet is arranged:

    a) the house load stays covered - holding inverters must not leave the fleet unable to supply
       what its batteries were already supplying
    b) no PV is thrown away - when there is surplus PV, the fleet must keep enough charge capacity
       to absorb it
    c) cross-charging is prevented - nobody is left charging off the grid or off another battery

    and, underpinning all three, a pass never holds in both directions at once.
    """
    failed = False
    rng = random.Random(20260919)
    for trial in range(3000):
        snapshot, intent, total_pv, total_load, total_grid, net_battery = build_random_fleet(rng)
        count = len(snapshot)
        balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)

        held_discharge = [id for id in intent if intent[id]["discharge_rate"] == 0]
        held_charge = [id for id in intent if intent[id]["charge_rate"] == 0]
        spare_pv = total_grid - net_battery
        context = "trial {} count {} pv {:.0f}W load {:.0f}W grid {:.0f}W battery {:.0f}W spare {:.0f}W".format(trial, count, total_pv, total_load, total_grid, net_battery, spare_pv)

        if held_discharge and held_charge:
            print("ERROR: held both directions in one pass - {} charge {} discharge {}".format(context, held_charge, held_discharge))
            failed = True

        # a) the house load must still be servable without forcing grid import that was not
        #    already there. Holding a battery that was discharging into an EXPORT is correct, so
        #    the test is against the load, not against what the batteries happened to be supplying.
        if held_discharge and total_grid >= 0:
            remaining = sum(snapshot[id]["discharge_rate_now"] for id in range(count) if id not in held_discharge)
            if total_pv + remaining < total_load:
                print("ERROR: holding forces grid import - {} pv+remaining {:.0f}W < load".format(context, total_pv + remaining))
                failed = True

        # b) surplus PV must still have somewhere to go
        if spare_pv > 0 and held_charge:
            capacity_before = sum(snapshot[id]["charge_rate_now"] for id in range(count))
            capacity_after = sum(snapshot[id]["charge_rate_now"] for id in range(count) if id not in held_charge)
            if capacity_after < min(spare_pv, capacity_before):
                print("ERROR: surplus PV can no longer be absorbed - {} capacity {:.0f}W -> {:.0f}W".format(context, capacity_before, capacity_after))
                failed = True

        # c) nobody charging off the grid or off another battery
        if net_battery > 0 and spare_pv <= 0:
            for id in range(count):
                if snapshot[id]["battery_power"] <= -50.0 and intent[id]["charge_rate"] != 0:
                    print("ERROR: inverter {} left cross-charging - {}".format(id, context))
                    failed = True
                    break

    assert not failed, "random fleet invariants violated - see the ERROR lines above"


def run_balance_pure_tests(my_predbat):
    """
    Run the pure balance_inverters tests.

    my_predbat is accepted for registry compatibility but unused - these tests need no PredBat
    instance and no mock Home Assistant, which is the point of extracting the algorithm.

    Returns:
    - bool: True if any test failed
    """
    print("**** Running pure balance_inverters tests ****\n")
    failed = False
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            try:
                function()
                print("Test {}: PASSED".format(name))
            except AssertionError as error:
                print("ERROR: Test {} FAILED: {}".format(name, error))
                failed = True
    return failed

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

from utils import balance_inverters


def make_snapshot(soc_percent, battery_power, charge_rate_now=2600.0, discharge_rate_now=2600.0, reserve_percent=4.0, pv_power=0.0, rate_max=2600.0, in_calibration=False):
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

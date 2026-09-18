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


def test_three_inverters_do_not_crash():
    """
    The (i + 1) % n partner ring is retained verbatim from the original, so 3+ inverters must at
    least be safe even though the pairing is arbitrary (F7, deferred).
    """
    intent = make_intent(3)
    snapshot = [make_snapshot(90.0, 1000.0), make_snapshot(50.0, 1000.0), make_snapshot(10.0, 1000.0)]
    balance_inverters(intent, snapshot, True, True, True, 1.0, 1.0)
    assert len(intent) == 3


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

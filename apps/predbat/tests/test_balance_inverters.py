# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.fleet_rig import FleetInverter, setup_fleet


def run_balance(my_predbat):
    """
    Drive the balance path the way execute_plan does: build a default intent where nothing has
    been claimed, let balancing mutate it, then apply it through the single write point.

    Replaces the old balance_inverters(test_mode=True) entry point, which built its own Inverter
    objects and wrote rates directly.
    """
    my_predbat.balance_inverters_enable = True
    my_predbat.set_read_only = False
    intent = {}
    for inverter in my_predbat.inverters:
        intent[inverter.id] = {"charge_rate": None, "discharge_rate": None, "pause_charge": False, "pause_discharge": False, "owner": "demand"}
    my_predbat.balance_inverter_rates(intent)
    for inverter in my_predbat.inverters:
        my_predbat.apply_inverter_rates(inverter, intent[inverter.id])


def balance_sensor(my_predbat):
    """
    The balance sensor's state and attributes as published, or None if it was not.
    """
    item = my_predbat.ha_interface.dummy_items.get("sensor.{}_balance_inverters".format(my_predbat.prefix))
    if not isinstance(item, dict):
        return None
    return item


def test_balance_sensor(my_predbat):
    """
    sensor.<prefix>_balance_inverters follows a cross-charge hold through being caught, latched and released.

    The latched pass must not count as a second hold, the release must be counted once, and switching balancing
    off reports status "off" with nothing held. A single inverter gets no sensor at all.
    """
    failed = False
    my_predbat.balance_held_previous = {}
    my_predbat.balance_ceilings_previous = []
    my_predbat.balance_last_decisions = None
    my_predbat.balance_events = []
    my_predbat.balance_counts = {}
    my_predbat.balance_restored = False
    my_predbat.ha_interface.dummy_items.pop("sensor.{}_balance_inverters".format(my_predbat.prefix), None)
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True

    def expect(condition, message):
        """Record a failed expectation."""
        nonlocal failed
        if not condition:
            print("ERROR: balance sensor: {} - {}".format(message, balance_sensor(my_predbat)))
            failed = True

    # Caught: inverter 1 charging while the fleet discharges
    setup_two_inverters(my_predbat, soc1=50, soc2=40, battery_power1=1000, battery_power2=-100)
    run_balance(my_predbat)
    sensor = balance_sensor(my_predbat)
    expect(sensor is not None, "not published")
    if sensor:
        expect(sensor["state"] == 1 and sensor["status"] == "holding", "one inverter should be held")
        expect(sensor["inverters"][1]["charge_hold"] == "cross_charge" and sensor["inverters"][0]["charge_hold"] is None, "inverter 1 should be held from charging, caught")
        expect(sensor["fleet_direction"] == "discharge", "the fleet should be discharging")
        expect(sensor["holds_24h"] == 1 and sensor["cross_charges_caught_24h"] == 1 and sensor["releases_24h"] == 0, "one hold, caught cross-charging")
        expect(sensor["events"] and sensor["events"][-1]["action"] == "hold" and sensor["events"][-1]["inverter"] == 1, "the hold should be the last event")

    # Latched: held, so it now reads 0W and a charge rate of 0, while the fleet still discharges
    setup_two_inverters(my_predbat, soc1=50, soc2=40, battery_power1=1000, battery_power2=0, charge_rate2=0)
    for inverter in my_predbat.inverters:
        inverter.charge_rate_is_zero = inverter.id == 1
    run_balance(my_predbat)
    sensor = balance_sensor(my_predbat)
    if sensor:
        expect(sensor["state"] == 1 and sensor["inverters"][1]["charge_hold"] == "cross_charge_latched", "the hold should be kept, latched")
        expect(sensor["holds_24h"] == 1, "a latched hold must not count as another hold")

    # Released: the fleet turns to soaking up PV surplus
    setup_two_inverters(my_predbat, soc1=50, soc2=40, battery_power1=-1000, battery_power2=0, charge_rate2=0)
    run_balance(my_predbat)
    sensor = balance_sensor(my_predbat)
    if sensor:
        expect(sensor["state"] == 0 and sensor["status"] == "balanced", "nothing should be held once the fleet charges")
        expect(sensor["releases_24h"] == 1 and sensor["events"][-1]["action"] == "release", "the release should be counted once")
        expect(isinstance(sensor["history"], dict) and len(sensor["history"]) == 1, "the hourly counts should be published")

    # Restart: Predbat's in-memory record is gone, the sensor is not. The next pass reads it back and carries on.
    my_predbat.balance_counts = {}
    my_predbat.balance_events = []
    my_predbat.balance_held_previous = {}
    my_predbat.balance_ceilings_previous = []
    my_predbat.balance_restored = False
    setup_two_inverters(my_predbat, soc1=50, soc2=40, battery_power1=1000, battery_power2=-100)
    run_balance(my_predbat)
    sensor = balance_sensor(my_predbat)
    if sensor:
        expect(sensor["holds_24h"] == 2 and sensor["releases_24h"] == 1 and sensor["cross_charges_caught_24h"] == 2, "the counts should carry on from before the restart")
        expect(len(sensor["events"]) == 3 and [event["action"] for event in sensor["events"]] == ["hold", "release", "hold"], "the events from before the restart should be kept")

    # Balancing switched off
    my_predbat.balance_inverters_enable = False
    my_predbat.balance_inverter_rates({inverter.id: {"charge_rate": None, "discharge_rate": None, "owner": "demand"} for inverter in my_predbat.inverters})
    sensor = balance_sensor(my_predbat)
    if sensor:
        expect(sensor["state"] == 0 and sensor["status"] == "off", "balancing off should report status off")

    # A single inverter has nothing to balance, so no sensor
    del my_predbat.ha_interface.dummy_items["sensor.{}_balance_inverters".format(my_predbat.prefix)]
    my_predbat.inverters = my_predbat.inverters[:1]
    my_predbat.balance_inverters_enable = True
    my_predbat.balance_inverter_rates({0: {"charge_rate": None, "discharge_rate": None, "owner": "demand"}})
    expect(balance_sensor(my_predbat) is None, "a single inverter should get no sensor")
    if not failed:
        print("✓ Test passed: balance sensor follows hold, latch, release, off and single inverter")
    return failed


def run_balance_inverters_tests(my_predbat):
    """
    Test the balance_inverters function with various scenarios
    """
    print("**** Running balance_inverters tests ****\n")
    failed = False

    # Test 1: Two inverters, one low SoC during discharge
    print("Test 1: Two inverters, one low SoC during discharge")
    failed |= test_balance_discharge_low_soc(my_predbat)
    if failed:
        return failed

    # Test 2: Two inverters, one high SoC during charge
    print("Test 2: Two inverters, one high SoC during charge")
    failed |= test_balance_charge_high_soc(my_predbat)
    if failed:
        return failed

    # Test 3: Cross charging during discharge
    print("Test 3.1: Cross charging during discharge")
    failed |= test_balance_cross_charging1(my_predbat)
    if failed:
        return failed
    # Test 3.2: Cross charging during discharge
    print("Test 3.2: Cross charging during discharge")
    failed |= test_balance_cross_charging2(my_predbat)
    if failed:
        return failed

    # Test 4: Cross discharging during charge
    print("Test 4: Cross discharging during charge")
    failed |= test_balance_cross_discharging(my_predbat)
    if failed:
        return failed

    # Test 5: Already balanced inverters
    print("Test 5: Already balanced inverters")
    failed |= test_balance_already_balanced(my_predbat)
    if failed:
        return failed

    # Test 6: Below threshold difference
    print("Test 6: Below threshold difference")
    failed |= test_balance_below_threshold(my_predbat)
    if failed:
        return failed

    # Test 7: One inverter at reserve
    print("Test 7: One inverter at reserve during discharge")
    failed |= test_balance_at_reserve(my_predbat)
    if failed:
        return failed

    # Test 8: Balance disabled
    print("Test 8: Balance disabled")
    failed |= test_balance_disabled(my_predbat)
    if failed:
        return failed

    # Test 9: Inverter in calibration mode
    print("Test 9: Inverter in calibration mode")
    failed |= test_balance_calibration_mode(my_predbat)
    if failed:
        return failed

    # Test 10: Insufficient power to balance
    print("Test 10: Insufficient power to balance")
    failed |= test_balance_insufficient_power(my_predbat)
    if failed:
        return failed

    # Test 11: Reset balance
    print("Test 11: Reset balance when already balanced, charge")
    failed |= test_balance_reset_balanced_charge(my_predbat)
    failed |= test_balance_charge_hold_latched_while_discharging(my_predbat)
    failed |= test_balance_sensor(my_predbat)
    if failed:
        return failed

    # Test 11: Reset balance
    print("Test 12: Reset balance when already balanced, discharge")
    failed |= test_balance_reset_balanced_discharge(my_predbat)
    if failed:
        return failed

    # Test 13: Peer close to a high reserve cannot take over
    print("Test 13: Peer close to a high reserve cannot take over the discharge")
    failed |= test_balance_peer_near_high_reserve(my_predbat)
    if failed:
        return failed

    return failed


def setup_two_inverters(my_predbat, soc1=50, soc2=50, reserve1=4, reserve2=4, battery_power1=0, battery_power2=0, pv_power1=0, pv_power2=0, load_power1=500, load_power2=500, charge_rate1=2600, charge_rate2=2600, discharge_rate1=2600, discharge_rate2=2600):
    """
    Helper function to set up two matched 10kWh inverters with specified parameters

    A thin wrapper over the fleet rig, kept for these tests' per-inverter argument style. New
    multi-inverter tests should call setup_fleet() directly so they can vary the fleet.
    """
    setup_fleet(
        my_predbat,
        [FleetInverter(10.0), FleetInverter(10.0)],
        soc=[soc1, soc2],
        reserve=[reserve1, reserve2],
        battery_power=[battery_power1, battery_power2],
        pv_power=[pv_power1, pv_power2],
        load_power=[load_power1, load_power2],
        charge_rate=[charge_rate1, charge_rate2],
        discharge_rate=[discharge_rate1, discharge_rate2],
        aggregate=False,
    )


def test_balance_discharge_low_soc(my_predbat):
    """
    Test balancing when one inverter has low SoC during discharge
    Expected: Low SoC inverter discharge rate should be set to 0
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=1000,  # Discharging (positive = discharging)
        battery_power2=1000,
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    print(services)
    ha.service_store_enable = False

    # Should have set discharge rate to 0 for inverter 0
    discharge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate" and kwargs.get("value") == 0:
            discharge_set = True
            break

    if not discharge_set:
        print("ERROR: Expected inverter 0 discharge rate to be set to 0")
        return True

    print("✓ Test passed: Low SoC inverter discharge rate set to 0")
    return False


def test_balance_charge_high_soc(my_predbat):
    """
    Test balancing when one inverter has high SoC during charge
    Expected: High SoC inverter charge rate should be set to 0
    """
    # Setup: Inverter 0 at 80%, Inverter 1 at 60%, both charging
    setup_two_inverters(
        my_predbat,
        soc1=80,
        soc2=60,
        battery_power1=-1000,  # Charging (negative = charging)
        battery_power2=-1000,
        charge_rate1=2600,
        charge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should have set charge rate to 0 for inverter 0
    charge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate" and kwargs.get("value") == 0:
            charge_set = True
            break

    if not charge_set:
        print("ERROR: Expected inverter 0 charge rate to be set to 0")
        return True

    print("✓ Test passed: High SoC inverter charge rate set to 0")
    return False


def test_balance_cross_charging1(my_predbat):
    """
    Test balancing when one inverter is cross-charging during discharge
    Expected: Charging inverter should have charge rate set to 0
    """
    # Setup: Inverter 0 discharging, Inverter 1 charging during discharge
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=40,
        battery_power1=1000,  # Discharging
        battery_power2=-100,  # Charging (cross-charge)
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should turn off charge for inverter 2
    charge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate_2" and kwargs.get("value") == 0:
            charge_set = True
            break

    if not charge_set:
        print("ERROR: Expected inverter 2 charge rate to be set to 0 to stop cross-charging")
        return True

    print("✓ Test passed: Cross-charging inverter charge rate set to 0")
    return False


def test_balance_cross_charging2(my_predbat):
    """
    Test balancing when one inverter is cross-charging during discharge
    Expected: Charging inverter should have charge rate set to 0
    """
    # Setup: Inverter 0 discharging, Inverter 1 charging during discharge
    setup_two_inverters(
        my_predbat,
        soc1=40,
        soc2=50,
        battery_power1=1000,  # Discharging
        battery_power2=-100,  # Charging (cross-charge)
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Stop the SINK, not the source. Inverter 1 (SoC 50) is charging off inverter 0 (SoC 40), so
    # the emptier battery is feeding the fuller one. Cutting inverter 0's discharge - which is what
    # this used to do - leaves nothing covering the ~900W house load while inverter 1 is still
    # drawing, so the whole lot comes in from the grid (GH#3172). Cutting inverter 1's charge only
    # removes load, so it can never import.
    charge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate_2" and kwargs.get("value") == 0:
            charge_set = True
            break

    if not charge_set:
        print("ERROR: Expected inverter 1 charge rate to be set to 0 to stop cross-charging")
        return True

    print("✓ Test passed: Cross-charging inverter discharge rate set to 0")
    return False


def test_balance_cross_discharging(my_predbat):
    """
    Test balancing when one inverter is cross-discharging during charge
    Expected: Discharging inverter should have discharge rate set to 0
    """
    # Setup: Inverter 0 charging, Inverter 1 discharging during charge
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=60,
        battery_power1=-1000,  # Charging
        battery_power2=100,  # Discharging (cross-discharge)
        charge_rate1=2600,
        charge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should have set discharge rate to 0 for inverter 1
    discharge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate_2" and kwargs.get("value") == 0:
            discharge_set = True
            break

    if not discharge_set:
        print("ERROR: Expected inverter 1 discharge rate to be set to 0 to stop cross-discharging")
        return True

    print("✓ Test passed: Cross-discharging inverter discharge rate set to 0")
    return False


def test_balance_already_balanced(my_predbat):
    """
    Test when inverters are already balanced
    Expected: No rate adjustments should be made
    """
    # Setup: Both inverters at 50%
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=50,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")) and kwargs.get("value") == 0:
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when inverters are balanced")
        return True

    print("✓ Test passed: No adjustments made when already balanced")
    return False


def test_balance_reset_balanced_charge(my_predbat):
    """
    Test when inverters are already balanced, resetting charge/discharge rates
    Expected: with the cross-charge correction OFF the rate goes back to full. With it ON (see
    test_balance_charge_hold_latched_while_discharging) a zero charge rate while the fleet discharges is a latched
    cross-charge hold and is kept.
    """
    # Setup: Both inverters at 50%
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=50,
        battery_power1=0,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    my_predbat.inverters[0].charge_rate_now = 0  # Simulate previously balanced state
    ha.dummy_items["number.charge_rate"] = 0

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    charge_reset = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate" and kwargs.get("value") == 2600:
            charge_reset = True
            break

    if not charge_reset:
        print("ERROR: Expected rate adjustments when inverters are balanced")
        return True

    print("✓ Test passed: Rate adjustments made when already balanced")
    return False


def test_balance_charge_hold_latched_while_discharging(my_predbat):
    """
    A battery whose charge rate reads back 0 while the fleet discharges keeps that hold with the cross-charge
    correction on. Released, a battery whose loop reads the shared CT as export winds straight back into charging
    from its neighbours (tests/balance_ct_harness.py, CT disagree).
    """
    setup_two_inverters(my_predbat, soc1=50, soc2=50, battery_power1=0, battery_power2=1000)
    ha = my_predbat.ha_interface
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    my_predbat.inverters[0].charge_rate_now = 0
    my_predbat.inverters[0].charge_rate_is_zero = True
    ha.dummy_items["number.charge_rate"] = 0
    ha.service_store_enable = True
    ha.get_service_store()
    run_balance(my_predbat)
    services = ha.get_service_store()
    ha.service_store_enable = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.charge_rate" and kwargs.get("value") != 0:
            print("ERROR: the latched charge hold was released to {}W while the fleet discharges".format(kwargs.get("value")))
            return True
    print("✓ Test passed: latched charge hold kept while the fleet discharges")
    return False


def test_balance_reset_balanced_discharge(my_predbat):
    """
    Test when inverters are already balanced, resetting charge/discharge rates
    Expected: Rate goes back to full
    """
    # Setup: Both inverters at 50%
    setup_two_inverters(
        my_predbat,
        soc1=50,
        soc2=50,
        battery_power1=1000,
        battery_power2=0,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = True
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    my_predbat.inverters[1].discharge_rate_now = 0  # Simulate previously balanced state
    ha.dummy_items["number.discharge_rate_2"] = 0

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    # Run balance
    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    discharge_reset = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate_2" and kwargs.get("value") == 2600:
            discharge_reset = True
            break

    if not discharge_reset:
        print("ERROR: Expected rate adjustments when inverters are balanced")
        return True

    print("✓ Test passed: Rate adjustments made when already balanced")
    return False


def test_balance_below_threshold(my_predbat):
    """
    Test when SoC difference is below threshold
    Expected: No rate adjustments should be made
    """
    # Setup: Inverter 0 at 48%, Inverter 1 at 52% (4% difference, below 5% threshold)
    setup_two_inverters(
        my_predbat,
        soc1=48,
        soc2=52,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance with 5% threshold
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = True
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates to 0
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")) and kwargs.get("value") == 0:
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when difference below threshold")
        return True

    print("✓ Test passed: No adjustments made when below threshold")
    return False


def test_balance_peer_near_high_reserve(my_predbat):
    """
    Test that a peer sitting just above a high reserve does not count as able to take over (#5835)
    Expected: the low SoC inverter keeps discharging, as the only peer is 2 points above its 38% reserve

    The balancer requires a peer to be at least 4 SoC points above its reserve. With the reserve
    passed in kWh (3.8 kWh of a 10 kWh battery) the peer looked 36 points clear, so the low
    inverter was held and the house load fell on a battery with almost nothing left to give.
    """
    # Setup: Inverter 0 at 30% (reserve 4%), Inverter 1 at 40% with a 38% backup reserve, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=40,
        reserve1=4,
        reserve2=38,
        battery_power1=1000,
        battery_power2=1000,
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate" and kwargs.get("value") == 0:
            print("ERROR: Inverter 0 discharge was held although its only peer is 2 points above its 38% reserve, services {}".format(services))
            return True

    print("✓ Test passed: Low SoC inverter keeps discharging when the peer is close to its reserve")
    return False


def test_balance_at_reserve(my_predbat):
    """
    Test when one inverter is at reserve level
    Expected: Inverter at reserve should not be forced to discharge
    """
    # Setup: Inverter 0 at reserve (4%), Inverter 1 at 50%, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=4,
        soc2=50,
        reserve1=4,
        reserve2=4,
        battery_power1=100,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should have set discharge rate to 0 for inverter 0 (at reserve)
    discharge_set = False
    for service, kwargs in services:
        if service == "number/set_value" and kwargs.get("entity_id") == "number.discharge_rate" and kwargs.get("value") == 0:
            discharge_set = True
            break

    if not discharge_set:
        print("ERROR: Expected inverter 0 at reserve to have discharge stopped")
        return True

    print("✓ Test passed: Inverter at reserve has discharge stopped")
    return False


def test_balance_disabled(my_predbat):
    """
    Test when balancing is disabled
    Expected: No rate adjustments should be made
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%, both discharging
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Disable all balance features
    my_predbat.balance_inverters_discharge = False
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5
    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")):
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when balancing disabled")
        return True

    print("✓ Test passed: No adjustments made when balancing disabled")
    return False


def test_balance_calibration_mode(my_predbat):
    """
    Test when one inverter is in calibration mode
    Expected: Balance function should return early without adjustments
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=1000,
        battery_power2=1000,
    )
    ha = my_predbat.ha_interface

    # Set inverter 0 in calibration mode
    my_predbat.inverters[0].in_calibration = True

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted any rates
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")):
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when in calibration mode")
        return True

    # Reset calibration mode
    my_predbat.inverters[0].in_calibration = False

    print("✓ Test passed: No adjustments made when in calibration mode")
    return False


def test_balance_insufficient_power(my_predbat):
    """
    Test when inverter power is below threshold for balancing
    Expected: No rate adjustments when power too low
    """
    # Setup: Inverter 0 at 30%, Inverter 1 at 50%, but low power (< 50W)
    setup_two_inverters(
        my_predbat,
        soc1=30,
        soc2=50,
        battery_power1=20,  # Below 50W threshold
        battery_power2=20,
        discharge_rate1=2600,
        discharge_rate2=2600,
    )
    ha = my_predbat.ha_interface

    # Enable balance
    my_predbat.balance_inverters_discharge = True
    my_predbat.balance_inverters_charge = False
    my_predbat.balance_inverters_crosscharge = False
    my_predbat.balance_inverters_threshold_charge = 5
    my_predbat.balance_inverters_threshold_discharge = 5

    # Clear service store
    ha.service_store_enable = True
    ha.get_service_store()

    run_balance(my_predbat)

    # Check services called
    services = ha.get_service_store()
    ha.service_store_enable = False

    # Should not have adjusted rates due to insufficient power
    rate_adjusted = False
    for service, kwargs in services:
        if service == "number/set_value" and ("charge_rate" in kwargs.get("entity_id", "") or "discharge_rate" in kwargs.get("entity_id", "")) and kwargs.get("value") == 0:
            rate_adjusted = True
            break

    if rate_adjusted:
        print("ERROR: Expected no rate adjustments when power below threshold")
        return True

    print("✓ Test passed: No adjustments made when power insufficient")
    return False

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

from utils import allocate_export_rates


def test_full_rate_is_a_no_op():
    """
    With low power off, p_fleet equals the sum of the ceilings, so every inverter runs at its own
    maximum and the allocation is identical to today's uniform scaling. This is the property that
    lets the allocator ship without a switch.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 5200.0)
    assert alloc == [2600.0, 2600.0], alloc


def test_matched_fleet_equal_needs_is_uniform():
    """
    Equal needs and equal ceilings give the uniform split, matching today exactly.
    """
    alloc = allocate_export_rates([2.0, 2.0], [2600.0, 2600.0], 3640.0)
    assert abs(alloc[0] - 1820.0) < 0.01, alloc
    assert abs(alloc[1] - 1820.0) < 0.01, alloc


def test_sum_is_preserved():
    """
    The planner costed p_fleet, so the allocation must deliver exactly that - only the split varies.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 3640.0)
    assert abs(sum(alloc) - 3640.0) < 0.01, alloc


def test_clamped_surplus_spills_to_the_other_inverter():
    """
    A 3:1 need split of 3640W would give 2730W to the first inverter, above its 2600W ceiling.
    The 130W surplus must spill to the other rather than be lost.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 3640.0)
    assert abs(alloc[0] - 2600.0) < 0.01, alloc
    assert abs(alloc[1] - 1040.0) < 0.01, alloc


def test_at_target_inverter_gets_nothing_and_spills_its_share():
    """
    An inverter already at its export target holds none of the budget, so the fleet keeps
    delivering the planned power instead of sagging as inverters finish one by one.
    """
    alloc = allocate_export_rates([0.0, 2.0], [2600.0, 2600.0], 2000.0)
    assert alloc[0] == 0.0, alloc
    assert abs(alloc[1] - 2000.0) < 0.01, alloc


def test_no_need_falls_back_to_uniform():
    """
    Nothing to shed anywhere - fall back to the uniform split rather than dividing by zero.
    """
    alloc = allocate_export_rates([0.0, 0.0], [2600.0, 2600.0], 2600.0)
    assert abs(sum(alloc) - 2600.0) < 0.01, alloc
    assert abs(alloc[0] - alloc[1]) < 0.01, alloc


def test_single_inverter():
    """
    Degenerate fleet of one takes the whole budget.
    """
    alloc = allocate_export_rates([1.0], [2600.0], 1300.0)
    assert abs(alloc[0] - 1300.0) < 0.01, alloc


def test_demand_above_fleet_capacity_is_capped():
    """
    p_fleet can never exceed the sum of the ceilings.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 99999.0)
    assert alloc == [2600.0, 2600.0], alloc


def test_heterogeneous_ceilings():
    """
    Unequal rate ceilings as well as unequal needs - the smaller inverter clamps first and the
    larger one absorbs the remainder.
    """
    alloc = allocate_export_rates([2.0, 2.0], [2600.0, 1000.0], 3000.0)
    assert abs(alloc[1] - 1000.0) < 0.01, alloc
    assert abs(alloc[0] - 2000.0) < 0.01, alloc
    assert abs(sum(alloc) - 3000.0) < 0.01, alloc


def test_empty_fleet():
    """
    No inverters at all must not raise.
    """
    assert allocate_export_rates([], [], 1000.0) == []


def test_zero_fleet_power():
    """
    A planned fleet power of zero allocates nothing to anybody.
    """
    alloc = allocate_export_rates([3.0, 1.0], [2600.0, 2600.0], 0.0)
    assert abs(sum(alloc)) < 0.01, alloc


def test_full_rate_is_a_no_op_even_with_a_zero_need():
    """
    The full-rate no-op has to hold whatever the needs are.

    Zero-need inverters are filtered out of the water-fill, so at full fleet power their share had
    nowhere to go and the result broke both the documented no-op and the return contract that the
    sum is min(p_fleet, sum(max_rates)). An inverter already at its export target is stopped by
    its own target, not by having its rate zeroed, so at full power it still gets its maximum.
    """
    alloc = allocate_export_rates([0.0, 2.0], [2600.0, 2600.0], 5200.0)
    assert alloc == [2600.0, 2600.0], alloc
    assert abs(sum(alloc) - 5200.0) < 0.01, alloc


def test_sum_contract_holds_when_every_need_is_zero():
    """
    Nothing to shed anywhere at full power is still the full-power no-op.
    """
    alloc = allocate_export_rates([0.0, 0.0], [2600.0, 1000.0], 3600.0)
    assert abs(sum(alloc) - 3600.0) < 0.01, alloc


def run_allocate_export_tests(my_predbat):
    """
    Run the export rate allocator tests.

    my_predbat is accepted for registry compatibility but unused - these tests need no PredBat
    instance and no mock Home Assistant.

    Returns:
    - bool: True if any test failed
    """
    print("**** Running allocate_export_rates tests ****\n")
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

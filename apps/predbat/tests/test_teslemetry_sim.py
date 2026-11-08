# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Simulated Powerwall runs through the real execute -> teslemetry -> device chain (tests/teslemetry_sim_harness.py).

Each scenario scripts a freeze charge from 03:00 to 05:30 with a steady 400 W house load and no solar, then
checks what the battery did: the GH#6046 question is whether a freeze above 80% holds near its level or fills
the Powerwall to 100% from the grid.
"""

from tests.teslemetry_sim_harness import PlanWindow, TeslemetryHoldHarness

FREEZE_START = 3 * 60
FREEZE_END = 5 * 60 + 30


def run_freeze(soc_percent, tbc_control=True, restart_at=None):
    """Freeze at soc_percent from 03:00 to 05:30, restarting teslemetry at restart_at if given; return the harness and the result over the freeze."""
    harness = TeslemetryHoldHarness(soc_percent=soc_percent, windows=[PlanWindow(FREEZE_START, FREEZE_END)], tbc_control=tbc_control, restart_at=restart_at)
    harness.simulate(FREEZE_START, FREEZE_END + 15)
    # From the first execute's write: before it the reserve is the cold-start one
    return harness, harness.result(FREEZE_START + 2, FREEZE_END)


def describe(result):
    """One line per 10 minutes of the trace, for a failing assertion's message."""
    return "\n".join("{} soc {:.1f}% reserve {} {} grid_charging {} battery {:.0f}W grid {:.0f}W".format(row.sim_time.strftime("%H:%M"), row.soc_percent, row.reserve, row.mode, row.grid_charging, row.battery_w, row.grid_w) for row in result.trace[::10])


def test_teslemetry_sim_freeze_in_the_band_holds_near_its_level():
    """GH#6046: a freeze at 86% stays within the top-up margin of 86% - it neither fills to 100% from the grid nor drifts to 80%."""
    harness, result = run_freeze(86)
    assert any(status == "Freeze charging" for _, status in harness.statuses), harness.statuses
    assert result.soc_max <= 87.0, "freeze at 86% rose to {:.1f}%\n{}".format(result.soc_max, describe(result))
    assert result.soc_min >= 82.0, "freeze at 86% fell to {:.1f}%\n{}".format(result.soc_min, describe(result))
    assert result.band_writes == [], "a reserve in the 81-99 band reached the device: {}".format(result.band_writes)


def test_teslemetry_sim_freeze_in_the_band_tops_up_only_what_the_house_used():
    """The grid energy put into the battery during the freeze replaces what the house took from it, no more:
    the 400 W load over 2.5 h is 1.0 kWh, and on unchanged code the fill to 100% put in 1.9 kWh."""
    _, result = run_freeze(86)
    assert result.grid_charge_kwh <= 1.0, "grid put {:.2f} kWh into the battery\n{}".format(result.grid_charge_kwh, describe(result))


def soc_at(result, when):
    """The SoC on the trace row for the minute of when."""
    return [row.soc_percent for row in result.trace if row.sim_time <= when][-1]


def test_teslemetry_sim_freeze_in_the_band_tops_up_below_the_threshold():
    """The top-up happens, and on the right edges: the freeze holds at 80, switches to 100 only once the SoC
    is more than 3% below the 86% hold, and back to 80 once it is at 86% again."""
    harness, _ = run_freeze(86)
    # From 03:01, the first write after the cold-start one at 03:00
    result = harness.result(FREEZE_START + 1, FREEZE_END)
    writes = []
    for when, reserve in result.reserve_writes:
        # The 2-hourly forced re-assert repeats the standing value; only the changes matter here
        if not writes or writes[-1][1] != reserve:
            writes.append((when, reserve))
    sequence = [reserve for _, reserve in writes]
    assert sequence[:3] == [80, 100, 80], "reserve writes during the freeze were {}\n{}".format(sequence, describe(result))
    for when, reserve in writes:
        soc = soc_at(result, when)
        if reserve == 100:
            assert soc < 83.0, "top-up started at {} with the SoC at {:.2f}%".format(when.strftime("%H:%M"), soc)
        elif writes.index((when, reserve)) > 0:
            assert soc >= 86.0, "top-up stopped at {} with the SoC at {:.2f}%".format(when.strftime("%H:%M"), soc)


def test_teslemetry_sim_freeze_in_the_band_survives_a_restart():
    """A teslemetry restart part way through the drift (03:42, SoC ~84%) keeps the 86% hold level, so the top-up
    still comes below 83% - rather than the hold restarting from 84% and the battery settling at 80%."""
    harness, result = run_freeze(86, restart_at=FREEZE_START + 42)
    assert result.soc_min >= 82.0, "after the restart the freeze fell to {:.1f}%\n{}".format(result.soc_min, describe(result))
    restart = harness.sim_time(FREEZE_START + 42)
    top_ups = [(when, soc_at(result, when)) for when, reserve in result.reserve_writes if reserve == 100 and when > restart]
    assert top_ups and top_ups[0][1] < 83.0, "top-ups after the restart: {}\n{}".format(top_ups, describe(result))


def test_teslemetry_sim_freeze_near_full_tops_up_to_100():
    """A freeze at 98% uses reserve 100: the top-up is under 0.3 kWh and the battery ends full."""
    _, result = run_freeze(98)
    assert result.soc_max >= 99.9, describe(result)
    assert result.grid_charge_kwh <= 0.3, "grid put {:.2f} kWh into the battery\n{}".format(result.grid_charge_kwh, describe(result))


def test_teslemetry_sim_freeze_below_the_band_holds_flat():
    """A freeze at 74% is below the band: the reserve holds the battery where it is and nothing is imported into it."""
    _, result = run_freeze(74)
    assert 73.0 <= result.soc_min and result.soc_max <= 74.5, describe(result)
    assert result.grid_charge_kwh <= 0.05, describe(result)


def test_teslemetry_sim_real_rate_freeze_in_the_band_holds_near_its_level():
    """The real-rate (non-TBC) path holds a freeze in the band the same way, never leaving the device to snap 81-99."""
    _, result = run_freeze(86, tbc_control=False)
    assert result.soc_max <= 87.0 and result.soc_min >= 82.0, describe(result)
    assert result.band_writes == [], "a reserve in the 81-99 band reached the device: {}".format(result.band_writes)


def run_teslemetry_sim_tests(my_predbat=None):
    """Run every simulated Powerwall scenario, reporting each failure; returns True on failure, as the registry expects."""
    print("**** Running Teslemetry simulated Powerwall tests ****")
    failed = False
    for test in (
        test_teslemetry_sim_freeze_in_the_band_holds_near_its_level,
        test_teslemetry_sim_freeze_in_the_band_tops_up_only_what_the_house_used,
        test_teslemetry_sim_freeze_in_the_band_tops_up_below_the_threshold,
        test_teslemetry_sim_freeze_in_the_band_survives_a_restart,
        test_teslemetry_sim_freeze_near_full_tops_up_to_100,
        test_teslemetry_sim_freeze_below_the_band_holds_flat,
        test_teslemetry_sim_real_rate_freeze_in_the_band_holds_near_its_level,
    ):
        try:
            test()
        except AssertionError as error:
            print("ERROR: {} failed: {}".format(test.__name__, error))
            failed = True
    return failed

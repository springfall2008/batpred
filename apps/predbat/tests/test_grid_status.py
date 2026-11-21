# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for grid outage (power cut) handling in grid_status.py and its effect on plan execution."""

from datetime import datetime, timedelta, timezone

from grid_status import GRID_RESTORE_DELAY_DEFAULT, grid_restore_delay_minutes, grid_status_on_values, grid_status_poll, grid_status_reading, update_grid_outage
from tests.test_execute import ActiveTestInverter, run_execute_test

GRID_ENTITY = "sensor.sigen_plant_on_off_grid_status"


class FakeGridBase:
    """The parts of PredBat that grid_status.py uses: get_arg, get_state_wrapper, log and the reset flags."""

    def __init__(self, config=None, states=None):
        """Build a base with the given apps.yaml config and entity states."""
        self.config = config if config is not None else {"grid_status": GRID_ENTITY}
        self.states = states if states is not None else {GRID_ENTITY: "on_grid"}
        self.grid_outage_active = False
        self.grid_restore_seen = None
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""
        self.update_pending = False
        self.logs = []

    def get_arg(self, arg, default=None, indirect=True, **kwargs):
        """Return the config value, or default."""
        return self.config.get(arg, default)

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None):
        """Return the entity state, or default."""
        return self.states.get(entity_id, default)

    def log(self, message):
        """Record a log line."""
        self.logs.append(message)

    def set_grid(self, state):
        """Set the grid status entity's state."""
        self.states[GRID_ENTITY] = state

    def clear_reset(self):
        """Forget any reset request, as Execute.reset_inverter() does once it has run."""
        self.inverter_needs_reset = False
        self.inverter_needs_reset_force = ""


def check(condition, message):
    """Print an error and return True (failed) when condition is False."""
    if not condition:
        print("ERROR: " + message)
        return True
    return False


def test_grid_status_reading():
    """grid_status_reading() maps entity states to True (on grid), False (off grid) or None (no evidence)."""
    failed = False
    print("*** grid_status_reading ***")

    # Not configured, or a literal rather than an entity: no evidence
    failed |= check(grid_status_reading(FakeGridBase(config={})) is None, "no grid_status configured should read None")
    failed |= check(grid_status_reading(FakeGridBase(config={"grid_status": "on_grid"})) is None, "a literal grid_status should read None")
    failed |= check(grid_status_reading(FakeGridBase(states={})) is None, "a missing entity should read None")

    base = FakeGridBase()
    for state in ["unknown", "unavailable", "None", "", "  "]:
        base.set_grid(state)
        failed |= check(grid_status_reading(base) is None, "state {!r} should read None".format(state))

    # The Sigenergy local Modbus integration's enum states, and common binary sensor states
    for state in ["on_grid", "On Grid", "ON_GRID", "on", "true", "connected"]:
        base.set_grid(state)
        failed |= check(grid_status_reading(base) is True, "state {!r} should read on grid".format(state))
    for state in ["off_grid_auto", "off_grid_manual", "Off Grid (Auto)", "off", "false", "disconnected"]:
        base.set_grid(state)
        failed |= check(grid_status_reading(base) is False, "state {!r} should read off grid".format(state))

    # Custom on-grid values, as a string or a list - here a binary sensor that is "off" when there is no outage
    base = FakeGridBase(config={"grid_status": GRID_ENTITY, "grid_status_on_grid": "off"})
    base.set_grid("off")
    failed |= check(grid_status_reading(base) is True, "custom string on value 'off' should read on grid")
    base.set_grid("on")
    failed |= check(grid_status_reading(base) is False, "custom string on value 'off' should make 'on' read off grid")
    base = FakeGridBase(config={"grid_status": GRID_ENTITY, "grid_status_on_grid": ["Normal", "GRID"]})
    base.set_grid("grid")
    failed |= check(grid_status_reading(base) is True, "custom list on value should match case-insensitively")
    base.set_grid("on_grid")
    failed |= check(grid_status_reading(base) is False, "custom list replaces the default on values")

    # Empty or junk on values fall back to the defaults
    for value in [[], "", None, 5, [None, " "]]:
        base = FakeGridBase(config={"grid_status": GRID_ENTITY, "grid_status_on_grid": value})
        failed |= check(grid_status_on_values(base) == grid_status_on_values(FakeGridBase()), "on value {!r} should fall back to the defaults".format(value))
    return failed


def test_grid_restore_delay():
    """grid_restore_delay_minutes() defaults, parses and never goes negative."""
    failed = False
    print("*** grid_restore_delay_minutes ***")
    failed |= check(grid_restore_delay_minutes(FakeGridBase(config={})) == GRID_RESTORE_DELAY_DEFAULT, "delay should default")
    failed |= check(grid_restore_delay_minutes(FakeGridBase(config={"grid_restore_delay": 12})) == 12, "delay should be read")
    failed |= check(grid_restore_delay_minutes(FakeGridBase(config={"grid_restore_delay": "3"})) == 3, "a string delay should be parsed")
    failed |= check(grid_restore_delay_minutes(FakeGridBase(config={"grid_restore_delay": -4})) == 0, "a negative delay should be 0")
    failed |= check(grid_restore_delay_minutes(FakeGridBase(config={"grid_restore_delay": "soon"})) == GRID_RESTORE_DELAY_DEFAULT, "a junk delay should default")
    return failed


def test_update_grid_outage():
    """update_grid_outage() starts an outage at once, ends it only after the restore delay, and asks for resets."""
    failed = False
    print("*** update_grid_outage ***")
    now = datetime(2026, 11, 21, 2, 0, tzinfo=timezone.utc)
    base = FakeGridBase(config={"grid_status": GRID_ENTITY, "grid_restore_delay": 10})

    # On grid: nothing happens
    failed |= check(update_grid_outage(base, now) is False, "on grid should not be an outage")
    failed |= check(not base.inverter_needs_reset, "on grid should not ask for a reset")

    # Unknown before any outage: still nothing
    base.set_grid("unavailable")
    failed |= check(update_grid_outage(base, now) is False, "an unavailable sensor should not start an outage")

    # Grid fails: outage at once, with a forced reset
    base.set_grid("off_grid_auto")
    failed |= check(update_grid_outage(base, now) is True, "off grid should start an outage")
    failed |= check(base.inverter_needs_reset and base.inverter_needs_reset_force == "set_read_only", "an outage should ask for a forced reset")
    failed |= check(any("Grid outage detected" in line for line in base.logs), "the outage should be logged")

    # Still out: no second reset
    base.clear_reset()
    failed |= check(update_grid_outage(base, now + timedelta(minutes=5)) is True, "the outage should continue")
    failed |= check(not base.inverter_needs_reset, "the reset should only be asked for once")

    # Unknown mid-outage: the outage holds
    base.set_grid("unknown")
    failed |= check(update_grid_outage(base, now + timedelta(minutes=6)) is True, "an unknown sensor should not end an outage")

    # Grid back, but not for long enough
    base.set_grid("on_grid")
    restore = now + timedelta(minutes=10)
    failed |= check(update_grid_outage(base, restore) is True, "the outage should hold during the restore delay")
    failed |= check(base.grid_restore_seen == restore, "the restore time should be recorded")
    failed |= check(update_grid_outage(base, restore + timedelta(minutes=9)) is True, "the outage should hold until the delay has passed")
    failed |= check(base.grid_restore_seen == restore, "the restore time should not move while the grid stays up")

    # It flickers off: the restore timer starts again
    base.set_grid("off_grid_auto")
    failed |= check(update_grid_outage(base, restore + timedelta(minutes=9, seconds=30)) is True, "a flicker should keep the outage")
    failed |= check(base.grid_restore_seen is None, "a flicker should clear the restore time")
    failed |= check(not base.inverter_needs_reset, "a flicker within an outage should not ask for another reset")

    # Back again, and stays up for the delay
    base.set_grid("on_grid")
    restore2 = restore + timedelta(minutes=10)
    failed |= check(update_grid_outage(base, restore2) is True, "the outage should hold when the grid comes back again")
    failed |= check(update_grid_outage(base, restore2 + timedelta(minutes=10)) is False, "the outage should end after the restore delay")
    failed |= check(base.inverter_needs_reset and base.inverter_needs_reset_force == "", "ending the outage should ask for an ordinary reset")
    failed |= check(base.grid_restore_seen is None, "the restore time should be cleared when the outage ends")

    # A zero delay ends the outage on the first on-grid reading
    base = FakeGridBase(config={"grid_status": GRID_ENTITY, "grid_restore_delay": 0})
    base.set_grid("off_grid_manual")
    update_grid_outage(base, now)
    base.set_grid("on_grid")
    failed |= check(update_grid_outage(base, now + timedelta(seconds=1)) is False, "a zero delay should end the outage at once")

    # Not configured: never an outage
    base = FakeGridBase(config={})
    failed |= check(update_grid_outage(base, now) is False, "no grid_status should never be an outage")
    return failed


def test_grid_status_poll():
    """grid_status_poll() asks for an update only when an outage starts, and never in manual read-only."""
    failed = False
    print("*** grid_status_poll ***")
    base = FakeGridBase()
    failed |= check(grid_status_poll(base) is False and not base.update_pending, "on grid should not ask for an update")

    base.set_grid("off_grid_auto")
    failed |= check(grid_status_poll(base) is True and base.update_pending, "a new outage should ask for an update")

    base.update_pending = False
    base.grid_outage_active = True
    failed |= check(grid_status_poll(base) is False and not base.update_pending, "an outage already acted on should not ask again")

    base.grid_outage_active = False
    base.config["set_read_only"] = True
    failed |= check(grid_status_poll(base) is False and not base.update_pending, "manual read-only should not ask for an update")

    base = FakeGridBase(config={})
    failed |= check(grid_status_poll(base) is False, "no grid_status should not ask for an update")
    return failed


def test_grid_outage_execute(my_predbat):
    """A grid outage frees a held battery and stops a charge window, then leaves the inverter alone until the grid is back.

    The Sigenergy case behind this: with no reserve register, Predbat holds the reserve with a zero
    discharge rate, so a power cut during the hold left the house without the battery.
    """
    failed = False
    print("*** grid outage execution ***")
    saved_inverters = my_predbat.inverters
    saved_num_inverters = my_predbat.args.get("num_inverters")
    my_predbat.inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc)]
    my_predbat.args["num_inverters"] = 1
    # Sigenergy-like: no timed pause, no reserve register, reserve held with the discharge rate
    sig = dict(set_charge_window=True, set_export_window=True, has_timed_pause=False, set_reserve_enable=False, reserve=0.4, reserve_via_discharge_rate=True, assert_reserve=0)
    read_only = dict(read_only=True, assert_immediate_charge_soc_target_array=[-1], assert_immediate_discharge_soc_target_array=[-1])
    # The forced reset clears any immediate charge and puts export back to idle (EXPORT_LIMIT_IDLE)
    read_only_reset = dict(read_only=True, assert_immediate_charge_soc_target_array=[0], assert_immediate_discharge_soc_target_array=[100])
    try:
        # Overnight at the reserve: the battery is held with a zero discharge rate
        failed |= run_execute_test(my_predbat, "grid_hold_before_outage", soc_kw=0.4, assert_discharge_rate=0, assert_immediate_soc_target=0, **sig)
        if failed:
            return failed

        # Plain read-only with no reset leaves the hold in place - the behaviour the outage handling fixes
        failed |= run_execute_test(my_predbat, "grid_read_only_keeps_hold", soc_kw=0.4, assert_status="Read-Only", assert_discharge_rate=0, **sig, **read_only)

        # Power cut: the forced reset frees the battery, then Predbat is read-only
        failed |= run_execute_test(my_predbat, "grid_outage_frees_battery", soc_kw=0.4, set_read_only_grid=True, inverter_needs_reset_force="set_read_only", assert_status="Read-Only (Grid outage)", assert_discharge_rate=1000, **sig, **read_only_reset)
        failed |= check(not my_predbat.inverter_needs_reset, "the reset should have run")

        # Later in the outage: nothing is written, so the battery stays free
        failed |= run_execute_test(my_predbat, "grid_outage_stays_free", soc_kw=0.3, set_read_only_grid=True, assert_status="Read-Only (Grid outage)", assert_discharge_rate=1000, **sig, **read_only)

        # Grid back: an ordinary reset, then Predbat holds the reserve again
        failed |= run_execute_test(my_predbat, "grid_restored_holds_again", soc_kw=0.4, inverter_needs_reset_force="", assert_discharge_rate=0, assert_immediate_soc_target=0, **sig)
        if failed:
            return failed

        # A power cut in a charge window: the charge is stopped as well
        charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
        failed |= run_execute_test(
            my_predbat,
            "grid_charging_before_outage",
            charge_window_best=charge_window,
            charge_limit_best=[10],
            soc_kw=0.4,
            assert_status="Charging",
            assert_charge_time_enable=True,
            assert_charge_start_time_minutes=-1,
            assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
            assert_discharge_rate=1000,
            assert_immediate_soc_target=100,
            **sig,
        )
        failed |= run_execute_test(
            my_predbat,
            "grid_outage_stops_charge",
            charge_window_best=charge_window,
            charge_limit_best=[10],
            soc_kw=0.4,
            set_read_only_grid=True,
            inverter_needs_reset_force="set_read_only",
            assert_status="Read-Only (Grid outage)",
            assert_charge_time_enable=False,
            assert_discharge_rate=1000,
            **sig,
            **read_only_reset,
        )
        failed |= check(not my_predbat.isCharging, "Predbat should not report charging during an outage")
    finally:
        my_predbat.inverters = saved_inverters
        my_predbat.args["num_inverters"] = saved_num_inverters
        my_predbat.set_read_only = False
        my_predbat.set_read_only_grid = False
        my_predbat.inverter_needs_reset = False
        my_predbat.inverter_needs_reset_force = ""
    return failed


def run_grid_status_tests(my_predbat):
    """Run all grid outage tests; returns True on failure."""
    print("**** Running grid status tests ****")
    failed = False
    failed |= test_grid_status_reading()
    failed |= test_grid_restore_delay()
    failed |= test_update_grid_outage()
    failed |= test_grid_status_poll()
    failed |= test_grid_outage_execute(my_predbat)
    if not failed:
        print("**** All grid status tests passed ****")
    return failed

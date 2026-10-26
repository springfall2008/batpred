# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Predheat scheduler, its predheat_enable gate (#4670) and the smart thermostat pre-heat model."""

import asyncio
from datetime import datetime, timedelta

import pytz

from const import TIME_FORMAT
from predheat import PredHeat

# Smart thermostat simulation fixture: two forecast days from midnight, 16 °C outside the heating periods
SIM_DAYS = 2
SIM_MINUTES = SIM_DAYS * 24 * 60
BASE_TEMP = 16.0
# 06:00-09:00 at 20 °C and 17:00-22:00 at 21 °C on both days
DAILY_PERIODS = [(360, 540, 20.0), (1020, 1320, 21.0), (1800, 1980, 20.0), (2460, 2760, 21.0)]


class FakeComponents:
    """Minimal stand-in for the component registry so switch_event can be driven in tests."""

    async def switch_event(self, entity_id, service):
        """Swallow the component-level switch routing, which is not under test here."""
        return None


def _make_predheat(my_predbat, captured):
    """Build a PredHeat bound to my_predbat with its log captured and update_pred stubbed."""
    my_predbat.args["predheat"] = {"forecast_days": 2, "run_every": 5, "mode": "pump"}
    my_predbat.log = lambda msg, quiet=True: captured.append(msg)
    predheat = PredHeat(my_predbat)
    predheat.initialize()
    predheat.runs = []
    predheat.update_pred = lambda scheduled: predheat.runs.append(scheduled)
    return predheat


async def _turn_switch(my_predbat, service):
    """Drive the real HA switch-event path for switch.predbat_predheat_enable."""
    await my_predbat.switch_event(service, {"service": service, "service_data": {"entity_id": "switch.predbat_predheat_enable"}}, None)


def _run_due_now(my_predbat, predheat):
    """Make every registered timer due and run one timer tick."""
    for item in my_predbat.run_list:
        item["next_time"] = datetime.now() - timedelta(seconds=1)
    asyncio.get_event_loop().run_until_complete(my_predbat.timer_tick())


def test_predheat(my_predbat):
    """Predheat scheduler registration and the predheat_enable gate."""
    failed = False

    original_log = my_predbat.log
    original_components = my_predbat.components
    original_run_list = my_predbat.run_list
    original_predheat_args = my_predbat.args.get("predheat", None)
    original_enable = my_predbat.config_index["predheat_enable"].get("value", None)

    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        my_predbat.components = FakeComponents()

        print("**** Running Predheat tests ****")

        # ------------------------------------------------------------------
        print("Test: initialize registers both Predheat timers")
        my_predbat.run_list = []
        my_predbat.expose_config("predheat_enable", False)
        captured = []
        predheat = _make_predheat(my_predbat, captured)

        registered = {item["callback"].__name__: item for item in my_predbat.run_list}
        if "run_time_loop" not in registered or "update_time_loop" not in registered:
            print("  ERROR: expected both Predheat timers to be registered, got {}".format(list(registered)))
            failed = True
        elif registered["run_time_loop"]["run_every"] != 300:
            print("  ERROR: expected run_time_loop every 300 seconds, got {}".format(registered["run_time_loop"]["run_every"]))
            failed = True

        # ------------------------------------------------------------------
        print("Test: while disabled Predheat does not run but says so in the log (#4670)")
        _run_due_now(my_predbat, predheat)
        if predheat.runs:
            print("  ERROR: Predheat ran while predheat_enable was off: {}".format(predheat.runs))
            failed = True
        disabled_logs = [msg for msg in captured if "Predheat" in msg and "disabled" in msg.lower()]
        if len(disabled_logs) != 1:
            print("  ERROR: expected exactly one 'disabled' log line explaining why Predheat is idle, captured: {}".format(captured))
            failed = True
        elif "predheat_enable" not in disabled_logs[0]:
            print("  ERROR: expected the disabled log line to name the switch to turn on, got: {}".format(disabled_logs[0]))
            failed = True

        # The pending first run must survive the disabled ticks, or enabling Predheat later
        # would silently wait for the next 5 minute boundary instead of running promptly.
        if not predheat.update_pending:
            print("  ERROR: expected update_pending to survive while disabled")
            failed = True

        print("Test: the disabled message is not repeated on every tick")
        _run_due_now(my_predbat, predheat)
        _run_due_now(my_predbat, predheat)
        if len([msg for msg in captured if "Predheat" in msg and "disabled" in msg.lower()]) != 1:
            print("  ERROR: expected the disabled log line to be logged once, captured: {}".format(captured))
            failed = True

        # ------------------------------------------------------------------
        print("Test: turning switch.predbat_predheat_enable on starts Predheat")
        asyncio.get_event_loop().run_until_complete(_turn_switch(my_predbat, "turn_on"))
        if not my_predbat.get_arg("predheat_enable"):
            print("  ERROR: the switch event did not enable predheat_enable")
            failed = True
        _run_due_now(my_predbat, predheat)
        if not predheat.runs:
            print("  ERROR: Predheat did not run after being enabled")
            failed = True
        enabled_logs = [msg for msg in captured if "Predheat" in msg and "enabled" in msg.lower()]
        if len(enabled_logs) != 1:
            print("  ERROR: expected exactly one 'enabled' log line, captured: {}".format(captured))
            failed = True

        # ------------------------------------------------------------------
        print("Test: re-enabling after a run schedules a prompt update rather than waiting 5 minutes")
        predheat.update_pending = False
        asyncio.get_event_loop().run_until_complete(_turn_switch(my_predbat, "turn_off"))
        _run_due_now(my_predbat, predheat)
        runs_while_off = len(predheat.runs)

        asyncio.get_event_loop().run_until_complete(_turn_switch(my_predbat, "turn_on"))

        # Only the fast 5 second update loop is due here, so a run proves re-enabling asked for
        # an immediate update rather than leaving the user waiting for the next 5 minute boundary
        for item in my_predbat.run_list:
            item["next_time"] = datetime.now() if item["callback"].__name__ == "update_time_loop" else datetime.now() + timedelta(minutes=5)
        asyncio.get_event_loop().run_until_complete(my_predbat.timer_tick())
        if len(predheat.runs) <= runs_while_off:
            print("  ERROR: Predheat did not run promptly after being re-enabled")
            failed = True

    finally:
        my_predbat.log = original_log
        my_predbat.components = original_components
        my_predbat.run_list = original_run_list
        my_predbat.config_index["predheat_enable"]["value"] = original_enable
        if original_predheat_args is None:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = original_predheat_args

    return failed


def _setpoint(periods, minute):
    """Scheduled setpoint at a forecast minute: the period's setpoint inside a heating period, otherwise BASE_TEMP."""
    for start, end, setpoint in periods:
        if start <= minute < end:
            return setpoint
    return BASE_TEMP


def _make_simulation(my_predbat, periods, smart_thermostat=True, max_minutes=None):
    """Build a PredHeat with a fixed thermal model and a two day setpoint schedule, ready for run_simulation().

    With days_previous [2] get_historical() reads forecast minute m from history index 2880 - m, so the
    target history is laid out back to front to replay the schedule. Published sensors are captured in
    predheat.published instead of being written to HA.
    """
    predheat = PredHeat(my_predbat)
    predheat.prefix = "predheat"
    predheat.published = {}
    predheat.set_state = lambda entity_id, state=None, attributes=None: predheat.published.__setitem__(entity_id, attributes or {})

    predheat.forecast_days = SIM_DAYS
    predheat.minutes_now = 0
    predheat.midnight_utc = datetime(2026, 1, 5, tzinfo=pytz.utc)
    predheat.days_previous = [2]
    predheat.days_previous_weight = [1]
    predheat.minute_data_age = 2
    predheat.target_temperature = {index: _setpoint(periods, SIM_MINUTES - index) for index in range(0, SIM_MINUTES + 1)}
    predheat.internal_temperature = {0: BASE_TEMP}
    predheat.external_temperature = {0: 5.0}
    predheat.temperatures = {}
    predheat.heat_energy_today = 0
    predheat.import_today_cost = 0
    predheat.rate_import = {}

    predheat.mode = "pump"
    predheat.flow_temp = 40.0
    predheat.flow_difference_target = 5
    predheat.heat_loss_watts = 120
    predheat.heat_loss_degrees = 0.03
    predheat.watt_per_degree = predheat.heat_loss_watts / predheat.heat_loss_degrees
    predheat.heat_gain_static = 0
    predheat.heat_output = 7000
    predheat.heat_volume = 75
    predheat.heat_max_power = 7000
    predheat.heat_min_power = 2000
    predheat.heat_cop = 4.0
    predheat.smart_thermostat = smart_thermostat
    predheat.smart_thermostat_max_minutes = max_minutes
    return predheat


def _first_pass_prediction(spans):
    """A first pass internal temperature forecast at BASE_TEMP, below every raised setpoint, except inside each (start, end, temperature) span."""
    return {minute: _setpoint(spans, minute) for minute in range(0, SIM_MINUTES, 5)}


def _published_targets(predheat, last_predict_minute):
    """Run the pre-heat simulation pass and return the published target setpoint keyed by forecast minute (10 minute resolution)."""
    predheat.run_simulation(BASE_TEMP, False, last_predict_minute=last_predict_minute)
    results = predheat.published[predheat.prefix + ".target_temp"]["results"]
    return {minute: results[(predheat.midnight_utc + timedelta(minutes=minute)).strftime(TIME_FORMAT)] for minute in range(0, SIM_MINUTES, 10)}


def _check_targets(name, periods, windows, targets):
    """Compare the published target with the schedule raised to each (start, end, setpoint) pre-heat window, return True on failure."""
    mismatches = []
    for minute, got in sorted(targets.items()):
        expected = _setpoint(periods, minute)
        for start, end, setpoint in windows:
            if start <= minute < end:
                expected = max(expected, setpoint)
        if got != expected:
            mismatches.append((minute, got, expected))
    if mismatches:
        print("  ERROR: {} - published target differs at {} sampled minutes, first (minute, got, expected): {}".format(name, len(mismatches), mismatches[:8]))
        return True
    return False


def _shift(spans, offset):
    """Move every (start, end, temperature) span later by offset minutes."""
    return [(start + offset, end + offset, temperature) for start, end, temperature in spans]


def _check_case(my_predbat, name, periods, reached_spans, windows, smart_thermostat=True, max_minutes=None):
    """Run one pre-heat scenario and check the published target against the expected windows, return True on failure.

    The published target is sampled every 10 minutes, so the scenario is run a second time one 5 minute
    step later to check every window boundary at the simulation's full resolution.
    """
    failed = False
    for offset in (0, 5):
        predheat = _make_simulation(my_predbat, _shift(periods, offset), smart_thermostat=smart_thermostat, max_minutes=max_minutes)
        targets = _published_targets(predheat, _first_pass_prediction(_shift(reached_spans, offset)))
        failed |= _check_targets("{} (offset {})".format(name, offset), _shift(periods, offset), _shift(windows, offset), targets)
    return failed


def test_predheat_smart_thermostat(my_predbat):
    """Smart thermostat pre-heat windows modelled by PredHeat.run_simulation() and the smart_thermostat_max_minutes setting."""
    failed = False
    print("**** Running Predheat smart thermostat tests ****")

    # The first pass reaches each raised setpoint 60, 90, 40 and 120 minutes after its rise
    reached_spans = [(420, 540, 20.0), (1110, 1320, 21.0), (1840, 1980, 20.0), (2580, 2760, 21.0)]

    # ------------------------------------------------------------------
    print("Test: every setpoint rise in the horizon gets its own pre-heat window")
    failed |= _check_case(my_predbat, "each rise pre-heated", DAILY_PERIODS, reached_spans, [(300, 360, 20.0), (930, 1020, 21.0), (1760, 1800, 20.0), (2340, 2460, 21.0)])

    # ------------------------------------------------------------------
    print("Test: smart_thermostat_max_minutes caps each lead time")
    failed |= _check_case(my_predbat, "lead time capped at 60", DAILY_PERIODS, reached_spans, [(300, 360, 20.0), (960, 1020, 21.0), (1760, 1800, 20.0), (2400, 2460, 21.0)], max_minutes=60)

    # ------------------------------------------------------------------
    print("Test: a rise never reached within its period pre-heats for the whole period length when uncapped")
    failed |= _check_case(my_predbat, "never reached, uncapped", DAILY_PERIODS, [], [(180, 360, 20.0), (720, 1020, 21.0), (1620, 1800, 20.0), (2160, 2460, 21.0)])

    print("Test: a rise never reached within its period pre-heats for the cap when one is set")
    failed |= _check_case(my_predbat, "never reached, capped", DAILY_PERIODS, [], [(300, 360, 20.0), (960, 1020, 21.0), (1740, 1800, 20.0), (2400, 2460, 21.0)], max_minutes=60)

    print("Test: a period shorter than the cap and never reached pre-heats for the full cap")
    failed |= _check_case(my_predbat, "short period never reached, capped", [(360, 390, 20.0)], [], [(300, 360, 20.0)], max_minutes=60)

    print("Test: a heating period ends where the setpoint first drops below the raised value, not where it returns to the earlier setpoint")
    # 16 °C to 21 °C at 06:00, down to 19 °C at 08:00 and back to 16 °C at 10:00: the 21 °C period is 06:00-08:00
    failed |= _check_case(my_predbat, "partial drop ends the period", [(360, 480, 21.0), (480, 600, 19.0)], [], [(240, 360, 21.0)])

    # ------------------------------------------------------------------
    print("Test: a rise only reached in a later heating period is bounded by its own period")
    # 15:00-17:00 at 21 °C on both days, the first pass only reaching 21 °C at 15:30 on the second day
    failed |= _check_case(my_predbat, "reached next day", [(900, 1020, 21.0), (2340, 2460, 21.0)], [(2370, 2460, 21.0)], [(780, 900, 21.0), (2310, 2340, 21.0)])

    print("Test: a single rise reached only after its period ends does not raise the target from now")
    failed |= _check_case(my_predbat, "single rise reached next day", [(900, 1020, 21.0)], [(2370, 2460, 21.0)], [(780, 900, 21.0)])

    # ------------------------------------------------------------------
    print("Test: a further rise inside a heating period pre-heats from the end of the earlier rise, not before it")
    # 16 °C to 19 °C at 06:00, then 21 °C at 07:00 until 09:00: the 07:00 window would start at 05:00 but is clamped to 06:00
    failed |= _check_case(my_predbat, "back to back rises", [(360, 420, 19.0), (420, 540, 21.0)], [], [(180, 360, 19.0), (360, 420, 21.0)])

    # ------------------------------------------------------------------
    print("Test: a pre-heat window never lowers an earlier, higher setpoint")
    # 06:00-09:00 at 21 °C, 16 °C until 10:00, then 20 °C until 12:00: the second window reaches back into the first period
    failed |= _check_case(my_predbat, "window overlapping a higher setpoint", [(360, 540, 21.0), (600, 720, 20.0)], [], [(180, 360, 21.0), (480, 600, 20.0)])

    # ------------------------------------------------------------------
    print("Test: with smart_thermostat off the target follows the schedule and the pre-heat pass changes nothing")
    failed |= _check_case(my_predbat, "smart_thermostat off", DAILY_PERIODS, reached_spans, [], smart_thermostat=False, max_minutes=60)
    predheat = _make_simulation(my_predbat, DAILY_PERIODS, smart_thermostat=False, max_minutes=60)
    _, plain_predict = predheat.run_simulation(BASE_TEMP, False, save="none")
    _, second_predict = predheat.run_simulation(BASE_TEMP, False, save="none", last_predict_minute=plain_predict)
    if second_predict != plain_predict:
        print("  ERROR: with smart_thermostat off the second simulation pass changed the internal temperature forecast")
        failed = True

    # ------------------------------------------------------------------
    print("Test: smart_thermostat_max_minutes is read as whole minutes, and unset or not a number leaves the lead time uncapped")
    original_predheat_args = my_predbat.args.get("predheat", None)
    try:
        predheat = PredHeat(my_predbat)
        for value, expected in [(None, None), (60, 60), ("45", 45), (45.5, 45), (-10, 0), ("soon", None), (float("inf"), None), ("inf", None), ("nan", None)]:
            my_predbat.args["predheat"] = {} if value is None else {"smart_thermostat_max_minutes": value}
            got = predheat.get_smart_thermostat_max_minutes()
            if got != expected:
                print("  ERROR: smart_thermostat_max_minutes {} read as {}, expected {}".format(repr(value), repr(got), repr(expected)))
                failed = True
    finally:
        if original_predheat_args is None:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = original_predheat_args

    return failed

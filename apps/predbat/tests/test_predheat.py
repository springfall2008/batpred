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
from datetime import datetime, timedelta, timezone

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


def test_predheat_volume_temp(my_predbat):
    """Check sensor, numeric and fallback volume temperature initialisation."""
    failed = False
    sensor = "sensor.predheat_test_flow_temp"
    sensor_f = "sensor.predheat_test_volume_f"
    flow_f = "sensor.predheat_test_flow_f"
    flow_helper = "input_number.predheat_test_flow"
    flow_dead = "sensor.predheat_test_flow_dead"
    weather_f = "weather.predheat_test_f"
    flow_plain = "sensor.predheat_test_flow_plain"
    volume_plain = "input_number.predheat_test_volume_plain"
    flow_plain_dead = "sensor.predheat_test_flow_plain_dead"
    flow_attribute = "climate.predheat_test_boiler"
    missing = object()

    old_args = my_predbat.args.get("predheat", missing)
    old_enable = my_predbat.config_index["predheat_enable"].get("value")
    old_next = my_predbat.config_index["next_volume_temp"].get("value")
    old_sensor = my_predbat.ha_interface.dummy_items.get(sensor, missing)

    histories = {
        "external_temperature": ({0: 7.5}, 7),
        "internal_temperature": ({0: 19.9}, 7),
        "target_temperature": ({0: 20.0}, 7),
        "heating_energy": ({}, 0),
    }

    cases = [
        ("HA sensor", {"volume_temp": sensor}, 24.598, 39.7, 40.0),
        ("numeric value", {"volume_temp": 35.0}, 24.598, 35.0, 40.0),
        ("persisted fallback", {}, 24.598, 24.598, 40.0),
        ("internal fallback", {}, None, 19.9, 40.0),
        ("sensors in °F (#5153)", {"volume_temp": sensor_f, "flow_temp": flow_f}, 24.598, 60.0, 50.0),
        ("entity with no unit, Home Assistant in °C", {"flow_temp": flow_helper}, 24.598, 24.598, 45.0),
        ("entities with no unit, Home Assistant in °F", {"weather": weather_f, "flow_temp": flow_plain, "volume_temp": volume_plain}, 24.598, 60.0, 50.0),
        ("unreadable entity with no unit, Home Assistant in °F, gives the default unconverted", {"weather": weather_f, "flow_temp": flow_plain_dead}, 24.598, 24.598, 40.0),
        ("entity$attribute in °F", {"flow_temp": flow_attribute + "$setpoint"}, 24.598, 24.598, 50.0),
        ("fixed number, Home Assistant in °F, is still °C", {"weather": weather_f, "flow_temp": 45}, 24.598, 24.598, 45.0),
        ("fixed flow number is °C", {"flow_temp": 45}, 24.598, 24.598, 45.0),
        ("unreadable flow sensor gives the default", {"flow_temp": flow_dead}, 24.598, 24.598, 40.0),
    ]
    extra_sensors = {
        sensor_f: {"state": "140", "unit_of_measurement": "°F"},
        flow_f: {"state": "122", "unit_of_measurement": "°F"},
        flow_helper: "45",
        flow_dead: {"state": "unavailable", "unit_of_measurement": "°F"},
        weather_f: {"state": "sunny", "temperature_unit": "°F"},
        flow_plain: "122",
        volume_plain: "140",
        flow_plain_dead: "unavailable",
        flow_attribute: {"state": "on", "setpoint": "122", "unit_of_measurement": "°F"},
    }
    old_items = dict(my_predbat.ha_interface.dummy_items)

    try:
        # next_volume_temp is only accessible when Predheat is enabled.
        my_predbat.config_index["predheat_enable"]["value"] = True
        my_predbat.ha_interface.dummy_items[sensor] = "39.7"
        my_predbat.ha_interface.dummy_items.update(extra_sensors)

        for label, options, persisted, expected, expected_flow in cases:
            print("Test: volume_temp {}".format(label))

            my_predbat.args["predheat"] = {
                "mode": "pump",
                "forecast_days": 2,
                "days_previous": [7],
                **options,
            }
            my_predbat.config_index["next_volume_temp"]["value"] = persisted

            predheat = PredHeat(my_predbat)
            predheat.reset()

            predheat.minute_data_entity = lambda now, key, **kwargs: histories[key]
            predheat.get_weather_data = lambda now: None
            predheat.today_cost = lambda history: 0

            initial_temps = []

            def simulate(volume_temp, heating_active, save="best", last_predict_minute=None):
                initial_temps.append(volume_temp)
                return volume_temp, {}

            predheat.run_simulation = simulate
            predheat.update_pred(scheduled=False)

            if len(initial_temps) != 2 or any(abs(float(temp) - expected) > 0.001 for temp in initial_temps):
                print("  ERROR: {} expected {}, got {}".format(label, expected, initial_temps))
                failed = True
            if abs(float(predheat.flow_temp) - expected_flow) > 0.001:
                print("  ERROR: {} expected flow_temp {}, got {}".format(label, expected_flow, predheat.flow_temp))
                failed = True

    finally:
        if old_args is missing:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = old_args

        my_predbat.config_index["predheat_enable"]["value"] = old_enable
        my_predbat.config_index["next_volume_temp"]["value"] = old_next

        my_predbat.ha_interface.dummy_items.clear()
        my_predbat.ha_interface.dummy_items.update(old_items)
        if old_sensor is missing:
            my_predbat.ha_interface.dummy_items.pop(sensor, None)
        else:
            my_predbat.ha_interface.dummy_items[sensor] = old_sensor

    return failed


def test_predheat_volume_temp_persistence(my_predbat):
    """Check the persisted volume temperature survives a config reload across the range a wet heating system reaches."""
    failed = False
    item = my_predbat.config_index["next_volume_temp"]

    old_enable = my_predbat.config_index["predheat_enable"].get("value")
    old_next = item.get("value")
    old_had_errors = my_predbat.had_errors
    old_record_status = my_predbat.record_status
    statuses = []

    def record_status(message, *args, **kwargs):
        """Capture each status message, then record it as normal."""
        statuses.append(message)
        return old_record_status(message, *args, **kwargs)

    # The volume heats towards the flow temperature, so the value saved after a run sits well above
    # room temperature. Anything beyond the boiling point of water is still clamped.
    cases = [
        ("flow temperature of an electric boiler", 55.5, 55.5, False),
        ("flow temperature of a gas boiler", 72.25, 72.25, False),
        ("declared maximum", 100, 100, False),
        ("above the declared maximum", 150, 100, True),
    ]

    try:
        # next_volume_temp is only accessible when Predheat is enabled.
        my_predbat.expose_config("predheat_enable", True, force_ha=True)
        my_predbat.record_status = record_status

        for label, persisted, expected, expect_warning in cases:
            print("Test: next_volume_temp reload with {}".format(label))

            my_predbat.expose_config("next_volume_temp", persisted, force_ha=True)
            my_predbat.had_errors = False
            statuses.clear()
            my_predbat.load_user_config()

            got = my_predbat.get_arg("next_volume_temp")
            if got is None or abs(float(got) - expected) > 0.001:
                print("  ERROR: {} expected {} after reload, got {}".format(label, expected, got))
                failed = True

            warnings = [message for message in statuses if "next_volume_temp" in message]
            if bool(warnings) != expect_warning or my_predbat.had_errors is not expect_warning:
                print("  ERROR: {} expected warning {}, got {} with had_errors {}".format(label, expect_warning, warnings, my_predbat.had_errors))
                failed = True

    finally:
        my_predbat.record_status = old_record_status
        my_predbat.expose_config("next_volume_temp", old_next if old_next is not None else item.get("default"), force_ha=True)
        my_predbat.expose_config("predheat_enable", bool(old_enable), force_ha=True)
        my_predbat.load_user_config()
        my_predbat.had_errors = old_had_errors

    return failed


def test_predheat_optional_energy_history(my_predbat):
    """Missing heating-energy history must not disable thermostat schedule history."""
    failed = False
    original_args = my_predbat.args.get("predheat")
    try:
        my_predbat.args["predheat"] = {"mode": "gas", "forecast_days": 2, "days_previous": [7]}
        predheat = PredHeat(my_predbat)
        predheat.reset()

        # The target schedule has seven days of history; gas energy is unavailable.
        histories = {
            "external_temperature": ({0: 10.0}, 7),
            "internal_temperature": ({0: 20.0}, 7),
            "target_temperature": ({0: 21.0, 7 * 24 * 60: 21.0}, 7),
            "heating_energy": ({}, 0),
        }
        predheat.minute_data_entity = lambda now, key, **kwargs: histories[key]
        predheat.get_weather_data = lambda now: None
        predheat.today_cost = lambda history: 0
        predicted_targets = []

        def simulate(volume_temp, heating_active, save="best", last_predict_minute=None):
            """Record the replayed setpoint and pass volume temperature through both simulation passes."""
            predicted_targets.append(predheat.get_historical(predheat.target_temperature, 0, default=20.0))
            return volume_temp, {}

        predheat.run_simulation = simulate

        print("Test: missing heating-energy history preserves scheduled target history")
        predheat.update_pred(scheduled=False)
        if predheat.minute_data_age != 7 or predicted_targets != [21.0, 21.0]:
            print("  ERROR: expected seven days and 21 C targets; got age {} targets {}".format(predheat.minute_data_age, predicted_targets))
            failed = True

        print("Test: available heating-energy history continues to limit history age")
        histories["heating_energy"] = ({0: 1.0}, 3)
        predicted_targets.clear()
        predheat.update_pred(scheduled=False)
        if predheat.minute_data_age != 3:
            print("  ERROR: expected three days, got {}".format(predheat.minute_data_age))
            failed = True

        print("Test: absent thermostat history still produces zero history age")
        histories["heating_energy"] = ({}, 0)
        histories["target_temperature"] = ({}, 0)
        predheat.update_pred(scheduled=False)
        if predheat.minute_data_age != 0:
            print("  ERROR: expected zero days, got {}".format(predheat.minute_data_age))
            failed = True
    finally:
        if original_args is None:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = original_args

    return failed


def test_predheat_temperature_units(my_predbat):
    """
    Predheat models in °C: temperatures from °F entities - the weather forecast, temperature sensors, a flow
    temperature sensor - are converted from each entity's own unit, and the published temperatures follow
    the internal temperature sensor's unit (#5153)
    """
    print("**** Running Test: predheat_temperature_units ****")
    failed = False
    original_predheat_args = my_predbat.args.get("predheat", None)
    try:
        units = {"weather.home": None, "sensor.inside_f": "°F", "sensor.inside_c": "°C"}
        my_predbat.args["predheat"] = {"weather": "weather.home", "internal_temperature": ["sensor.inside_f", "sensor.inside_c"]}
        now_utc = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)

        def make(weather_unit):
            """Build a PredHeat with stubbed HA state."""
            predheat = PredHeat(my_predbat)
            units["weather.home"] = weather_unit

            def get_state(entity_id=None, default=None, attribute=None, **kwargs):
                """Stand-in for get_state_wrapper: units from the table."""
                if attribute in ("unit_of_measurement", "temperature_unit"):
                    return units.get(entity_id)
                return default

            predheat.get_state = get_state
            predheat.set_units()
            return predheat

        # Weather forecast in °F reaches the model in °C; in °C it is passed through
        for unit, raw, expected in [("°F", 64.0, 17.78), ("°C", 18.0, 18.0)]:
            predheat = make(unit)
            predheat.forecast_days = 1
            predheat.external_temperature = {0: expected}
            forecast = [{"datetime": (now_utc + timedelta(hours=hour)).isoformat(), "temperature": raw} for hour in range(0, 26)]
            predheat.call_service = lambda *args, **kwargs: {"weather.home": {"forecast": forecast}}
            predheat.get_weather_data(now_utc)
            got = predheat.temperatures.get(120)
            if got is None or abs(got - expected) > 0.01:
                print("ERROR: a {} forecast of {} should reach the model as {} °C, got {}".format(unit, raw, expected, got))
                failed = True

        # Sensor histories in different units are each converted before they are averaged: 68 °F and 20 °C
        predheat = make("°C")
        predheat.max_days_previous = 1
        history = {
            "sensor.inside_f": [{"state": "68", "last_updated": (now_utc - timedelta(minutes=minute)).isoformat()} for minute in range(120, -1, -30)],
            "sensor.inside_c": [{"state": "20", "last_updated": (now_utc - timedelta(minutes=minute)).isoformat()} for minute in range(120, -1, -30)],
        }
        predheat.get_history = lambda entity_id=None, days=None, **kwargs: [history[entity_id]]
        data, _ = predheat.minute_data_entity(now_utc, "internal_temperature", smoothing=True, temperature=True)
        if abs(data.get(0, 0) - 20.0) > 0.01:
            print("ERROR: 68 °F and 20 °C should average to 20 °C, got {}".format(data.get(0)))
            failed = True

        # set_units: the display unit follows the first internal temperature sensor, else Home Assistant's unit
        # from the weather entity (sensor.inside_f is °F, the weather entity °C here)
        predheat = make("°C")
        if predheat.system_unit != "°C" or predheat.display_unit != "°F":
            print("ERROR: expected Home Assistant's unit °C and display unit °F (internal sensor), got {} and {}".format(predheat.system_unit, predheat.display_unit))
            failed = True
        units["sensor.inside_f"] = None
        predheat = make("°F")
        if predheat.display_unit != "°F":
            print("ERROR: an internal sensor with no unit should display in Home Assistant's unit (°F), got {}".format(predheat.display_unit))
            failed = True
        units["sensor.inside_f"] = "°F"

        # The display unit follows the first internal temperature sensor, and a sensor with no unit is read in it
        if predheat.first_entity("internal_temperature") != "sensor.inside_f":
            print("ERROR: the first internal temperature sensor should be sensor.inside_f, got {}".format(predheat.first_entity("internal_temperature")))
            failed = True
        predheat.system_unit = None
        predheat.display_unit = "°F"
        if predheat.temperature_unit("sensor.no_unit") != "°F":
            print("ERROR: with Home Assistant's unit unknown, a sensor with no unit should read in the display unit")
            failed = True

        # The published unit is a canonical label, and an unrecognised internal unit falls through to Home Assistant's
        for internal, weather, expected in [("degC", "°F", "°C"), ("degrees", "°F", "°F"), (None, "°F", "°F"), ("F", None, "°F"), ("degrees", None, "°C")]:
            units["sensor.inside_f"] = internal
            predheat = make(weather)
            if predheat.display_unit != expected:
                print("ERROR: internal unit {} with Home Assistant in {} should publish in {}, got {}".format(internal, weather, expected, predheat.display_unit))
                failed = True
        units["sensor.inside_f"] = "°F"
        predheat = make("°F")

        # The weather entity losing its unit while unavailable keeps the last one known
        units["weather.home"] = None
        predheat.set_units()
        if predheat.system_unit != "°F":
            print("ERROR: the last known Home Assistant unit should be kept, got {}".format(predheat.system_unit))
            failed = True

        # The default for a missing entity$attribute is °C already: Home Assistant returns it as the state, and it must not be converted
        units["sensor.boiler_f"] = "°F"
        my_predbat.args["predheat"]["flow_temp"] = "sensor.boiler_f$setpoint"
        real_get_state = my_predbat.ha_interface.get_state
        my_predbat.ha_interface.get_state = lambda entity_id=None, default=None, attribute=None, refresh=False, raw=False: (
            default if attribute == "setpoint" else ("°F" if attribute == "unit_of_measurement" else real_get_state(entity_id, default, attribute, refresh, raw))
        )
        try:
            if predheat.get_temperature_arg("flow_temp", 40.0) != 40.0:
                print("ERROR: a missing attribute should give the 40 °C default unconverted, got {}".format(predheat.get_temperature_arg("flow_temp", 40.0)))
                failed = True
        finally:
            my_predbat.ha_interface.get_state = real_get_state
        del my_predbat.args["predheat"]["flow_temp"]
        predheat.system_unit = None
        predheat.display_unit = "°F"

        # A sensor with no unit is read in Home Assistant's unit, from the weather entity, first
        predheat.system_unit = "°F"
        predheat.display_unit = "°C"
        if predheat.temperature_unit("sensor.template_no_unit") != "°F":
            print("ERROR: a sensor with no unit should be read in Home Assistant's unit (°F)")
            failed = True
        predheat.system_unit = None
        predheat.display_unit = "°F"

        # Spellings from hand-written templates, including this page's own 'c', are read, and a unit that names no
        # scale takes the fallback: neither loses the readings (the docs' template used to, and read 0 °C all day)
        predheat.get_history = lambda entity_id=None, days=None, **kwargs: [[{"state": "7", "last_updated": (now_utc - timedelta(minutes=minute)).isoformat(), "attributes": {"unit_of_measurement": spelling}} for minute in range(120, -1, -30)]]
        predheat.max_days_previous = 1
        predheat.display_unit = "°C"
        for spelling in ("c", "ºC", "degC", "degrees"):
            my_predbat.args["predheat"]["internal_temperature"] = "sensor.spelled"
            data, _ = predheat.minute_data_entity(now_utc, "internal_temperature", smoothing=True, temperature=True)
            if abs(data.get(0, 0) - 7.0) > 0.01:
                print("ERROR: a 7 degree reading with unit {} should be kept as 7 °C, got {}".format(spelling, data.get(0)))
                failed = True
        predheat.display_unit = "°F"

        # A null forecast from an integration with no hourly data still reaches the warning path
        predheat.call_service = lambda *args, **kwargs: {"weather.home": {"forecast": None}}
        predheat.get_weather_data(now_utc)
        if predheat.temperatures != {}:
            print("ERROR: a null forecast should leave no temperatures, got {}".format(predheat.temperatures))
            failed = True

        # Each history record in its own unit where it has one
        mixed = [{"state": "20", "last_updated": (now_utc - timedelta(minutes=minute)).isoformat(), "attributes": {"unit_of_measurement": "°C"}} for minute in range(240, 119, -30)]
        mixed += [{"state": "68", "last_updated": (now_utc - timedelta(minutes=minute)).isoformat(), "attributes": {"unit_of_measurement": "°F"}} for minute in range(90, -1, -30)]
        predheat.max_days_previous = 1
        predheat.get_history = lambda entity_id=None, days=None, **kwargs: [mixed]
        my_predbat.args["predheat"]["internal_temperature"] = "sensor.inside_f"
        data, _ = predheat.minute_data_entity(now_utc, "internal_temperature", smoothing=True, temperature=True)
        if abs(data.get(0, 0) - 20.0) > 0.01 or abs(data.get(200, 0) - 20.0) > 0.01:
            print("ERROR: 20 °C records and 68 °F records should all read 20 °C, got {} now and {} earlier".format(data.get(0), data.get(200)))
            failed = True

        # Published temperatures follow the display unit, except a 0 'heating off' marker
        if predheat.display_temperature(20.0) != 68.0 or predheat.display_series({"t": 0.0}) != {"t": 32.0} or predheat.display_series({"t": 0, "u": 20.0}, zero_is_off=True) != {"t": 0, "u": 68.0}:
            print("ERROR: temperatures should be published in °F when the internal sensor is in °F, keeping a heating-off 0")
            failed = True

        # Through a real run, every published temperature is labelled and converted together
        predheat = _make_simulation(my_predbat, DAILY_PERIODS)
        predheat.display_unit = "°F"
        predheat.run_simulation(BASE_TEMP, False, save="best")
        for name in ("internal_temp", "external_temp", "target_temp", "heat_to_temp", "internal_temp_h1", "internal_temp_h2", "internal_temp_h8", "volume_temp"):
            attributes = predheat.published.get("predheat." + name, {})
            if attributes.get("unit_of_measurement") != "°F":
                print("ERROR: predheat.{} should be published in °F, got {}".format(name, attributes.get("unit_of_measurement")))
                failed = True
        internal_results = list(predheat.published.get("predheat.internal_temp", {}).get("results", {}).values())
        if not internal_results or abs(internal_results[0] - (BASE_TEMP * 9 / 5 + 32)) > 0.1:
            print("ERROR: the internal temperature series should start at {} °F, got {}".format(BASE_TEMP * 9 / 5 + 32, internal_results[:1]))
            failed = True
    finally:
        if original_predheat_args is None:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = original_predheat_args
    return failed

# -----------------------------------------------------------------------------
# Predheat Climate Scheduler regression coverage
# -----------------------------------------------------------------------------
from datetime import datetime, timedelta
import pytz

from predheat import PredHeat
from const import TIME_FORMAT
from tests.test_predheat import _make_simulation, BASE_TEMP
from unittest.mock import patch


def test_predheat_climate_scheduler(my_predbat):
    """Check schedule decoding, local time, fallbacks and simulation usage."""
    missing = object()
    old_args = my_predbat.args.get("predheat", missing)
    old_log = my_predbat.log
    tz = pytz.timezone("Europe/London")
    failed = False

    def local(y, m, d, hour, minute):
        return tz.localize(datetime(y, m, d, hour, minute))

    def make(now, response, live=20.0, settings=None, days=2, scheduler_settings=None, workday_dates=None, hvac_mode="heat"):
        my_predbat.args["predheat"] = {
            "forecast_days": days,
            "target_temperature": "sensor.test_room_target",
            "target_temperature_schedule": "climate.test_scheduler",
            **(settings or {}),
        }
        # Keep the usual fixture quiet during construction.
        my_predbat.log = lambda *args, **kwargs: None
        try:
            heat = PredHeat(my_predbat)
        finally:
            my_predbat.log = old_log
        heat.log = lambda *args, **kwargs: None
        heat.forecast_days = days
        heat.forecast_minutes = days * 1440
        heat.target_temperature = {}
        heat.get_historical = lambda data, minute, default=20.0: 17.5
        state_calls = []
        service_calls = []

        def get_state(entity_id=None, default=None):
            state_calls.append(entity_id)
            if entity_id == "climate.test_scheduler":
                return hvac_mode
            return live

        def call_service(service, **kwargs):
            service_calls.append((service, kwargs))
            if service == "climate_scheduler/get_settings":
                if isinstance(scheduler_settings, Exception):
                    raise scheduler_settings
                return {"settings": scheduler_settings or {}}
            if service == "workday/check_date":
                if isinstance(workday_dates, Exception):
                    raise workday_dates
                if workday_dates is None:
                    return None
                return {"binary_sensor.workday_sensor": {"workday": kwargs["check_date"] in workday_dates}}
            if isinstance(response, Exception):
                raise response
            return response

        heat.get_state = get_state
        heat.call_service = call_service
        heat.load_target_schedule(now)
        return heat, service_calls, state_calls

    def check(label, test):
        nonlocal failed
        print("Test: Climate Scheduler " + label)
        try:
            test()
        except Exception as exc:
            print("  ERROR: {}: {}: {}".format(label, type(exc).__name__, exc))
            failed = True

    def response(nodes, mode="all_days", enabled=True):
        return {"enabled": enabled, "schedule_mode": mode, "schedules": nodes}

    def values(heat, samples):
        for minute, expected in samples:
            actual = heat.get_target_temperature(minute)
            assert actual == expected, "at minute {}: {} != {}".format(minute, actual, expected)

    def all_days():
        nodes = [
            {"time": "22:00", "temp": 16},
            {"time": "21:30", "temp": "19"},
            {"time": "07:30", "temp": 21},
            {"time": "12:00", "noChange": True},
            {"time": "25:00", "temp": 50},
            {"time": "23:45", "temp": "nan"},
            {"time": "bad", "temp": 50},
        ]
        heat, calls, states = make(local(2026, 11, 7, 21, 15), response({"all_days": nodes}), live="20.75")
        assert calls == [("climate_scheduler/get_schedule", {"schedule_id": "climate.test_scheduler", "return_response": True})]
        assert states == ["sensor.test_room_target", "climate.test_scheduler"]
        assert heat.schedule_change_minutes[:3] == [15.0, 45.0, 615.0]
        values(heat, [(0, 20.75), (14, 20.75), (15, 19), (44, 19), (45, 16), (614, 16), (615, 21)])

    def weekdays():
        nodes = {
            "weekday": [{"time": "06:00", "temp": 19}],
            "weekend": [{"time": "00:15", "temp": 16}],
        }
        heat, _, _ = make(
            local(2026, 11, 6, 23, 30),
            response(nodes, "5/2"),
            days=4,
            scheduler_settings={"workdays": ["mon", "tue", "wed", "thu", "fri"]},
        )
        values(heat, [(0, 20), (44, 20), (45, 16), (3269, 16), (3270, 19)])

    def configurable_workdays():
        nodes = {
            "weekday": [{"time": "00:15", "temp": 21}],
            "weekend": [{"time": "00:15", "temp": 17}],
        }
        # Friday 23:30 -> Saturday workday, Sunday and Monday non-workdays.
        heat, calls, _ = make(
            local(2026, 11, 6, 23, 30),
            response(nodes, "5/2"),
            days=4,
            scheduler_settings={"workdays": ["fri", "sat"]},
        )
        values(heat, [(0, 20), (44, 20), (45, 21), (1484, 21), (1485, 17), (2925, 17)])
        assert any(service == "climate_scheduler/get_settings" for service, _ in calls)
        assert not any(service == "workday/check_date" for service, _ in calls)

    def workday_holidays():
        nodes = {
            "weekday": [{"time": "00:15", "temp": 21}],
            "weekend": [{"time": "00:15", "temp": 17}],
        }
        # Workday integration overrides the standard week: Saturday is a
        # working day, while Monday is a holiday; normal weekday rules fail.
        heat, calls, _ = make(
            local(2026, 11, 6, 23, 30),
            response(nodes, "5/2"),
            days=4,
            scheduler_settings={
                "workdays": ["mon", "tue", "wed", "thu", "fri"],
                "use_workday_integration": True,
            },
            workday_dates={"2026-11-07"},
        )
        values(heat, [(0, 20), (44, 20), (45, 21), (1484, 21), (1485, 17), (2925, 17)])
        checks = [params for service, params in calls if service == "workday/check_date"]
        assert checks, "Expected future workday checks"
        assert all(check["target"] == {"entity_id": "binary_sensor.workday_sensor"} and check["return_response"] is True for check in checks)
        assert {"2026-11-07", "2026-11-09"}.issubset({check["check_date"] for check in checks})

    def workday_failure():
        nodes = {
            "weekday": [{"time": "00:15", "temp": 21}],
            "weekend": [{"time": "00:15", "temp": 17}],
        }
        heat, _, _ = make(
            local(2026, 11, 6, 23, 30),
            response(nodes, "5/2"),
            scheduler_settings={
                "workdays": ["mon", "tue", "wed", "thu", "fri"],
                "use_workday_integration": True,
            },
            workday_dates=RuntimeError("unavailable"),
        )
        assert heat.schedule_change_minutes is None
        values(heat, [(0, 17.5), (120, 17.5)])

    def settings_failure():
        nodes = {
            "weekday": [{"time": "00:15", "temp": 21}],
            "weekend": [{"time": "00:15", "temp": 17}],
        }
        cases = [
            ("service exception", RuntimeError("unavailable")),
            ("missing workdays", {}),
            ("invalid workdays", {"workdays": ["invalid"]}),
            (
                "invalid Workday setting",
                {
                    "workdays": ["mon", "tue", "wed", "thu", "fri"],
                    "use_workday_integration": "yes",
                },
            ),
        ]
        for name, scheduler_settings in cases:
            heat, calls, _ = make(
                local(2026, 11, 6, 23, 30),
                response(nodes, "5/2"),
                scheduler_settings=scheduler_settings,
            )
            assert heat.schedule_change_minutes is None, name
            values(heat, [(0, 17.5), (120, 17.5)])
            assert any(service == "climate_scheduler/get_settings" for service, _ in calls), name
            assert not any(service == "workday/check_date" for service, _ in calls), name

    def individual():
        nodes = {
            "sun": [{"time": "00:15", "temp": 17}, {"time": "12:00", "temp": 21}],
            "mon": [{"time": "06:00", "temp": 19}],
        }
        heat, _, _ = make(local(2026, 11, 7, 23, 30), response(nodes, "individual"))
        values(heat, [(0, 20), (44, 20), (45, 17), (749, 17), (750, 21), (1829, 21), (1830, 19)])

    def numeric_live():
        nodes = {"all_days": [{"time": "22:00", "temp": 18}]}
        heat, _, state_calls = make(local(2026, 11, 7, 21, 15), response(nodes), live=100, settings={"target_temperature": 21.25})
        assert state_calls == ["climate.test_scheduler"]
        values(heat, [(0, 21.25), (44, 21.25), (45, 18)])

    def failures():
        when = local(2026, 11, 7, 21, 15)
        cases = [
            ("not configured", response({"all_days": []}), 20, {"target_temperature_schedule": None}),
            ("service error", RuntimeError("offline"), 20, {}),
            ("missing response", None, 20, {}),
            ("disabled", response({"all_days": []}, enabled=False), 20, {}),
            ("invalid mode", response({"all_days": []}, mode="unknown"), 20, {}),
            ("invalid schedules", {"enabled": True, "schedules": []}, 20, {}),
            ("empty schedule", response({}), 20, {}),
            ("unavailable setpoint", response({"all_days": [{"time": "22:00", "temp": 19}]}), "unavailable", {}),
            ("non-finite setpoint", response({"all_days": [{"time": "22:00", "temp": 19}]}), "nan", {}),
            ("unsupported HVAC", response({"all_days": [{"time": "22:00", "temp": 19, "hvac_mode": "cool"}]}), 20, {}),
        ]
        for name, payload, live, settings in cases:
            heat, calls, _ = make(when, payload, live=live, settings=settings)
            assert heat.schedule_change_minutes is None, "{} did not fall back".format(name)
            values(heat, [(0, 17.5), (180, 17.5)])
            assert bool(calls) == (name != "not configured"), "{} unexpected service call".format(name)

    def reset_after_failure():
        heat, _, _ = make(local(2026, 11, 7, 21, 15), response({"all_days": [{"time": "22:00", "temp": 18}]}))
        assert heat.schedule_change_minutes
        heat.call_service = lambda *args, **kwargs: None
        heat.load_target_schedule(local(2026, 11, 7, 21, 20))
        assert heat.schedule_change_minutes is None
        values(heat, [(100, 17.5)])

    def dst():
        # Crossing BST start removes an hour; crossing BST end adds an hour.
        spring = response({"all_days": [{"time": "03:30", "temp": 21}]})
        heat, _, _ = make(local(2026, 3, 28, 23, 30), spring)
        values(heat, [(179, 20), (180, 21)])
        autumn = response(
            {
                "all_days": [
                    {"time": "01:30", "temp": 18},
                    {"time": "03:30", "temp": 21},
                ]
            }
        )
        heat, _, _ = make(local(2026, 10, 24, 23, 30), autumn)
        assert heat.schedule_change_minutes[:2] == [120.0, 300.0]
        values(heat, [(119, 20), (120, 18), (299, 18), (300, 21)])

    def hvac_modes():
        # "No Change" applies to temperature only; mode transitions remain.
        now = local(2026, 11, 7, 21, 15)
        nodes = {"all_days": [
            {"time": "21:30", "temp": None, "noChange": True, "hvac_mode": "off"},
            {"time": "21:45", "temp": 21},
            {"time": "22:00", "temp": None, "noChange": True, "hvac_mode": "heat"},
            {"time": "22:30", "temp": 18},
        ]}
        heat, _, states = make(now, response(nodes))
        assert states == ["sensor.test_room_target", "climate.test_scheduler"], states
        assert heat.schedule_mode_minutes[:2] == [15.0, 45.0]
        assert heat.schedule_mode_enabled[:2] == [False, True]
        for minute, expected in [(0, True), (14, True), (15, False),
                                 (30, False), (44, False), (45, True), (90, True)]:
            assert heat.get_heating_enabled(minute) is expected, minute
        values(heat, [(0, 20), (29, 20), (30, 21), (74, 21), (75, 18)])

        # Current HVAC-off state must be used until the first future change.
        off, _, _ = make(now, response(nodes), hvac_mode="off")
        assert not off.get_heating_enabled(0)
        assert not off.get_heating_enabled(40)
        assert off.get_heating_enabled(45)

        # Inability to read the live mode safely returns to history and clears
        # both channels, rather than assuming that heating is enabled.
        unknown, _, _ = make(now, response(nodes), hvac_mode="unavailable")
        assert unknown.schedule_change_minutes is None
        assert unknown.schedule_mode_minutes is None
        values(unknown, [(0, 17.5), (30, 17.5)])

        heat.call_service = lambda *args, **kwargs: None
        heat.load_target_schedule(now + timedelta(minutes=5))
        assert heat.schedule_change_minutes is None
        assert heat.schedule_mode_minutes is None
        assert heat.get_heating_enabled(30)

    def current_mode_without_transitions():
        # Temperature-only schedules must respect a manual HVAC-off state;
        # temperature changes must not implicitly re-enable the heating.
        now = local(2026, 11, 7, 21, 15)
        nodes = {"all_days": [
            {"time": "21:30", "temp": 21},
            {"time": "22:00", "temp": 18},
        ]}
        for mode, enabled in (("off", False), ("heat", True)):
            heat, _, states = make(now, response(nodes), hvac_mode=mode)
            assert states == ["sensor.test_room_target", "climate.test_scheduler"], states
            assert heat.schedule_change_minutes[:2] == [15.0, 45.0]
            assert heat.schedule_mode_minutes == [], (mode, heat.schedule_mode_minutes)
            assert heat.schedule_mode_enabled == []
            assert heat.schedule_initial_enabled is enabled
            for minute in (0, 15, 45, 2000):
                assert heat.get_heating_enabled(minute) is enabled, (mode, minute)
            values(heat, [(0, 20), (15, 21), (45, 18)])

        # Unknown or unsupported current modes must not break a
        # temperature-only schedule or accidentally disable the heat source.
        for mode in ("unavailable", "auto", None):
            heat, _, _ = make(now, response(nodes), hvac_mode=mode)
            assert heat.schedule_change_minutes is not None, mode
            assert heat.schedule_mode_minutes is None, mode
            assert heat.get_heating_enabled(0), mode
            assert heat.get_heating_enabled(2000), mode
            values(heat, [(0, 20), (15, 21), (45, 18)])

        # A failed HVAC state lookup also leaves a temperature-only
        # forecast intact, with the existing heating assumption.
        heat, _, _ = make(now, response(nodes), hvac_mode="heat")
        def missing_mode(entity_id=None, default=None):
            if entity_id == "climate.test_scheduler":
                raise RuntimeError("Home Assistant unavailable")
            return 20.0
        heat.get_state = missing_mode
        heat.load_target_schedule(now)
        assert heat.schedule_change_minutes is not None
        assert heat.schedule_mode_minutes is None
        assert heat.get_heating_enabled(0)
        values(heat, [(0, 20), (15, 21), (45, 18)])

    def hvac_simulation():
        # Exercise the actual thermal model, including an off interval and
        # subsequent restart. No heat is produced during the off interval.
        heat = _make_simulation(my_predbat, [], smart_thermostat=False)
        heat.schedule_initial_temperature = 24
        heat.schedule_change_minutes = []
        heat.schedule_change_temperatures = []
        heat.schedule_initial_enabled = True
        heat.schedule_mode_minutes = [30, 90]
        heat.schedule_mode_enabled = [False, True]
        heat.run_simulation(BASE_TEMP, True)
        heating = heat.published[heat.prefix + ".heat_to_temp"]["results"]
        energy = heat.published[heat.prefix + ".heat_energy"]["results"]
        stamp = lambda minute: (heat.midnight_utc + timedelta(minutes=minute)).strftime(TIME_FORMAT)
        assert heating[stamp(10)] > 0, "heating should initially run"
        assert heating[stamp(40)] == 0 and heating[stamp(80)] == 0, "off must gate the heat source"
        assert heating[stamp(100)] > 0, "heating should restart after heat mode resumes"
        assert energy[stamp(80)] == energy[stamp(70)], "off interval must use no heat-source energy"

        # The smart thermostat may raise the requested target early, but must
        # never override an explicit HVAC-off interval.
        heat = _make_simulation(my_predbat, [], smart_thermostat=True)
        heat.schedule_initial_temperature = 18
        heat.schedule_change_minutes = [90]
        heat.schedule_change_temperatures = [24]
        heat.schedule_initial_enabled = False
        heat.schedule_mode_minutes = [90]
        heat.schedule_mode_enabled = [True]
        first_pass = {minute: BASE_TEMP for minute in range(0, heat.forecast_days * 1440, 5)}
        heat.run_simulation(BASE_TEMP, False, last_predict_minute=first_pass)
        heating = heat.published[heat.prefix + ".heat_to_temp"]["results"]
        assert heating[stamp(80)] == 0, "preheat must respect HVAC-off"
        assert heating[stamp(100)] > 0, "preheat may run once heating is enabled"

    def update_pred_wiring():
        # Exercise the production update_pred path, not just load_target_schedule.
        my_predbat.args["predheat"] = {
            "mode": "pump",
            "forecast_days": 2,
            "days_previous": [7],
            "target_temperature": "sensor.test_room_target",
            "target_temperature_schedule": "climate.test_scheduler",
        }
        heat = PredHeat(my_predbat)
        heat.reset()
        histories = {
            "external_temperature": ({0: 7.0}, 7),
            "internal_temperature": ({0: 19.5}, 7),
            "target_temperature": ({0: 17.0, 10080: 17.0}, 7),
            "heating_energy": ({}, 0),
        }
        heat.minute_data_entity = lambda now, key, **kwargs: histories[key]
        heat.get_weather_data = lambda now: None
        heat.today_cost = lambda history: 0
        heat.get_state = lambda entity_id=None, default=None: 22.0
        calls = []

        def service(name, **kwargs):
            calls.append((name, kwargs))
            return response({"all_days": [{"time": "21:22", "temp": 18}]})

        heat.call_service = service
        captured = []

        def simulate(volume_temp, heating_active, save="best", last_predict_minute=None):
            captured.append(
                [heat.get_target_temperature(m) for m in (0, 5, 10)]
            )
            return volume_temp, {}

        heat.run_simulation = simulate

        # Run between five-minute boundaries to test schedule alignment.
        with patch("predheat.datetime", wraps=datetime) as clock:
            clock.now.return_value = local(2026, 11, 7, 21, 17).replace(second=30)
            heat.update_pred(scheduled=False)

        assert calls == [
            ("climate_scheduler/get_schedule", {
                "schedule_id": "climate.test_scheduler",
                "return_response": True,
            })
        ]

        assert heat.schedule_change_minutes == [7.0, 1447.0]
        assert captured == [[22.0, 22.0, 18.0]] * 2, captured

    def simulation():
        # Verify that the simulation itself uses the new targets, not just the helper.
        heat = _make_simulation(my_predbat, [], smart_thermostat=False)
        heat.schedule_initial_temperature = 18
        heat.schedule_change_minutes = [360, 540]
        heat.schedule_change_temperatures = [21, 16]
        heat.run_simulation(BASE_TEMP, False)
        results = heat.published[heat.prefix + ".target_temp"]["results"]
        for minute, expected in [(350, 18), (360, 21), (530, 21), (540, 16)]:
            timestamp = (heat.midnight_utc + timedelta(minutes=minute)).strftime(TIME_FORMAT)
            assert results[timestamp] == expected, "simulation at {} expected {}, got {}".format(minute, expected, results.get(timestamp))

    try:
        check("all_days, overrides and filtering", all_days)
        check("weekday/weekend across Monday", weekdays)
        check("custom manually selected workdays", configurable_workdays)
        check("Workday future dates and holidays", workday_holidays)
        check("Climate Scheduler settings failure fallback", settings_failure)
        check("Workday service failure fallback", workday_failure)
        check("individual days across midnight", individual)
        check("numeric live setpoint", numeric_live)
        check("failure and compatibility fallbacks", failures)
        check("discard stale schedule on failure", reset_after_failure)
        check("UK daylight-saving transitions", dst)
        check("HVAC mode parsing and fallback", hvac_modes)
        check("current HVAC mode without transitions", current_mode_without_transitions)
        check("HVAC off/on thermal simulation and preheat", hvac_simulation)
        check("update_pred integration", update_pred_wiring)
        check("simulation uses scheduled targets", simulation)
    finally:
        my_predbat.log = old_log
        if old_args is missing:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = old_args
    return failed

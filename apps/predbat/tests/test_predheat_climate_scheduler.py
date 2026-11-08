# -----------------------------------------------------------------------------
# Predheat Climate Scheduler regression coverage
# -----------------------------------------------------------------------------
from datetime import datetime, timedelta
import pytz

from predheat import PredHeat
from const import TIME_FORMAT
from tests.test_predheat import _make_simulation, BASE_TEMP


def test_predheat_climate_scheduler(my_predbat):
    """Check schedule decoding, local time, fallbacks and simulation usage."""
    missing = object()
    old_args = my_predbat.args.get("predheat", missing)
    old_log = my_predbat.log
    tz = pytz.timezone("Europe/London")
    failed = False

    def local(y, m, d, hour, minute):
        return tz.localize(datetime(y, m, d, hour, minute))

    def make(now, response, live=20.0, settings=None, days=2, scheduler_settings=None, workday_dates=None):
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
        assert states == ["sensor.test_room_target"]
        assert heat.schedule_change_minutes[:3] == [15.0, 45.0, 615.0]
        values(heat, [(0, 20.75), (14, 20.75), (15, 19), (44, 19), (45, 16), (614, 16), (615, 21)])

    def weekdays():
        nodes = {
            "weekday": [{"time": "06:00", "temp": 19}],
            "weekend": [{"time": "00:15", "temp": 16}],
        }
        heat, _, _ = make(local(2026, 11, 6, 23, 30), response(nodes, "5/2"), days=4, scheduler_settings={"workdays": ["mon", "tue", "wed", "thu", "fri"]},)
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
            ("invalid workdays", {"workdays": ["noday"]}),
            ("invalid Workday setting", {
                "workdays": ["mon", "tue", "wed", "thu", "fri"],
                "use_workday_integration": "yes",
            }),
        ]
        for name, scheduler_settings in cases:
            heat, calls, _ = make(
                local(2026, 11, 6, 23, 30),
                response(nodes, "5/2"),
                scheduler_settings=scheduler_settings,
            )
            assert heat.schedule_change_minutes is None, name
            values(heat, [(0, 17.5), (120, 17.5)])
            assert any(
                service == "climate_scheduler/get_settings"
                for service, _ in calls
            ), name
            assert not any(
                service == "workday/check_date"
                for service, _ in calls
            ), name


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
        assert state_calls == []
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
            ("HVAC off", response({"all_days": [{"time": "22:00", "temp": 19, "hvac_mode": "off"}]}), 20, {}),
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
            return response({"all_days": [{"time": "00:00", "temp": 18}]})

        heat.call_service = service
        captured = []

        def simulate(volume_temp, heating_active, save="best", last_predict_minute=None):
            captured.append(heat.get_target_temperature(0))
            return volume_temp, {}

        heat.run_simulation = simulate
        heat.update_pred(scheduled=False)
        assert calls == [("climate_scheduler/get_schedule", {"schedule_id": "climate.test_scheduler", "return_response": True})]
        assert captured == [22.0, 22.0], "both passes must use the live scheduled setpoint: {}".format(captured)

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
        check("update_pred integration", update_pred_wiring)
        check("simulation uses scheduled targets", simulation)
    finally:
        my_predbat.log = old_log
        if old_args is missing:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = old_args
    return failed

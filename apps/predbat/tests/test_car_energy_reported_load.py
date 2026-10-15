# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""
Tests for check_car_energy_reported_load() (GH#5318): a warning when switch.predbat_car_energy_reported_load is On
but car_charging_energy shows the load sensor cannot include the car charger - the misconfiguration behind #5317,
where the low-load test cancelled Octopus Intelligent dispatches that were really charging.
"""

from unittest.mock import patch

HOUSE_KW = 0.3
CAR_KW = 7.0


def _incrementing(per_minute_kwh, minutes):
    """
    Build an incrementing kWh series indexed in minutes back from now, from a function giving the energy used in the
    minute that many minutes ago.
    """
    series = {}
    total = 0.0
    # data[i] - data[i + 1] is the energy in the minute i minutes ago, as get_from_incrementing() reads it
    for minute in range(minutes, -1, -1):
        series[minute] = round(total, 6)
        total += per_minute_kwh(minute - 1) if minute > 0 else 0.0
    return series


def _charging(minute, sessions):
    """
    Whether the car is charging the given number of minutes ago, for sessions of (start, end) minutes ago.
    """
    return any(start <= minute < end for start, end in sessions)


def _setup(my_predbat, load_includes_car, lag=0, sessions=((60, 180),), car_kw=CAR_KW, history=24 * 60 + 60, load_gap=None):
    """
    Give my_predbat a car_charging_energy series and a load series. By default the car charges from one to three hours ago.
    lag shifts the load sensor's view of the charge by that many minutes, and load_gap is a (start, end) stretch where
    the load sensor reported nothing.
    """
    my_predbat.car_charging_energy = _incrementing(lambda minute: car_kw / 60 if _charging(minute, sessions) else 0.0, history)

    def load(minute):
        """
        The house load the load sensor saw in the minute that many minutes ago, with the car in it when it includes the charger.
        """
        if load_gap and load_gap[0] <= minute < load_gap[1]:
            return 0.0
        car = car_kw if (load_includes_car and _charging(minute + lag, sessions)) else 0.0
        return (HOUSE_KW + car) / 60

    my_predbat.load_minutes = _incrementing(load, history)


def _warnings(logged):
    """
    The mismatch warnings among the logged messages.
    """
    return [message for message in logged if "car_energy_reported_load is On" in message]


def test_car_energy_reported_load(my_predbat):
    """
    Warn when car_charging_energy exceeds the house load in enough windows while car_energy_reported_load is On.
    """
    print("*** Running test: car_energy_reported_load mismatch warning")
    failed = False
    saved = {field: getattr(my_predbat, field, None) for field in ("car_energy_reported_load", "car_charging_energy", "load_minutes", "car_energy_reported_load_warned", "log")}
    logged = []

    def check(name, condition, detail=""):
        """
        Print an error for a failed condition and record it.
        """
        nonlocal failed
        if not condition:
            print("ERROR: {} {}".format(name, detail))
            failed = True

    try:
        my_predbat.log = lambda message, *args, **kwargs: logged.append(message)
        my_predbat.car_energy_reported_load = True
        my_predbat.car_energy_reported_load_warned = False

        print("Test 1: a load sensor that excludes the charger warns, once")
        _setup(my_predbat, load_includes_car=False)
        my_predbat.check_car_energy_reported_load()
        check("t1 warns", len(_warnings(logged)) == 1, "logged {}".format(logged))
        check("t1 counts the half hours", "in 4 half hours" in "".join(_warnings(logged)), "logged {}".format(logged))
        my_predbat.check_car_energy_reported_load()
        check("t1 not repeated", len(_warnings(logged)) == 1, "logged {}".format(logged))

        print("Test 2: once the mismatch clears the warning can fire again")
        _setup(my_predbat, load_includes_car=True)
        my_predbat.check_car_energy_reported_load()
        check("t2 cleared", not my_predbat.car_energy_reported_load_warned)
        _setup(my_predbat, load_includes_car=False)
        my_predbat.check_car_energy_reported_load()
        check("t2 warns again", len(_warnings(logged)) == 2, "logged {}".format(logged))

        logged.clear()
        my_predbat.car_energy_reported_load_warned = False
        print("Test 3: a load sensor that includes the charger is silent, even when it lags the charger by 5 minutes")
        _setup(my_predbat, load_includes_car=True)
        my_predbat.check_car_energy_reported_load()
        _setup(my_predbat, load_includes_car=True, lag=5)
        my_predbat.check_car_energy_reported_load()
        _setup(my_predbat, load_includes_car=True, lag=-5)
        my_predbat.check_car_energy_reported_load()
        check("t3 silent", not _warnings(logged), "logged {}".format(logged))

        print("Test 4: the switch Off is silent")
        my_predbat.car_energy_reported_load = False
        _setup(my_predbat, load_includes_car=False)
        my_predbat.check_car_energy_reported_load()
        check("t4 silent", not _warnings(logged) and not my_predbat.car_energy_reported_load_warned, "logged {}".format(logged))
        my_predbat.car_energy_reported_load = True

        print("Test 5: a single mismatched half hour is not enough")
        _setup(my_predbat, load_includes_car=False, sessions=((60, 90),))
        my_predbat.check_car_energy_reported_load()
        check("t5 silent", not _warnings(logged), "logged {}".format(logged))

        print("Test 6: no load or car data is silent, and minutes past the end of the load history are not evidence")
        _setup(my_predbat, load_includes_car=False)
        my_predbat.car_charging_energy = {}
        my_predbat.check_car_energy_reported_load()
        _setup(my_predbat, load_includes_car=False)
        my_predbat.load_minutes = {}
        my_predbat.check_car_energy_reported_load()
        check("t6 silent without data", not _warnings(logged), "logged {}".format(logged))
        # The car charges from one to three hours ago, but the load history only starts two hours ago: the two half
        # hours before that have car energy and no load, and must not count
        _setup(my_predbat, load_includes_car=False)
        my_predbat.load_minutes = _incrementing(lambda minute: HOUSE_KW / 60, 120)
        windows = my_predbat.car_energy_exceeds_load_windows()
        check("t6 end of history", windows == 2, "windows {}".format(windows))

        print("Test 7: an 11 kW charger over two sessions, with the load sensor lagging 5 or 10 minutes, is silent")
        for lag in (5, 10, -5, -10):
            _setup(my_predbat, load_includes_car=True, lag=lag, sessions=((60, 120), (300, 360)), car_kw=11.0)
            check("t7 lag {}".format(lag), my_predbat.car_energy_exceeds_load_windows() == 0, "windows {}".format(my_predbat.car_energy_exceeds_load_windows()))
        check("t7 silent", not _warnings(logged), "logged {}".format(logged))

        print("Test 8: a stretch where the load sensor reported nothing is not evidence")
        _setup(my_predbat, load_includes_car=True, load_gap=(30, 210))
        check("t8 gap skipped", my_predbat.car_energy_exceeds_load_windows() == 0, "windows {}".format(my_predbat.car_energy_exceeds_load_windows()))

        print("Test 9: once warned, a count that dips below the threshold does not re-arm the warning; none at all does")
        _setup(my_predbat, load_includes_car=False)
        my_predbat.check_car_energy_reported_load()
        count = len(_warnings(logged))
        _setup(my_predbat, load_includes_car=False, sessions=((60, 90),))
        my_predbat.check_car_energy_reported_load()
        check("t9 still warned on one window", my_predbat.car_energy_reported_load_warned)
        _setup(my_predbat, load_includes_car=False)
        my_predbat.check_car_energy_reported_load()
        check("t9 not repeated", len(_warnings(logged)) == count, "logged {}".format(logged))

        print("Test 10: the window in progress is left out, as a cloud load sensor can lag it")
        _setup(my_predbat, load_includes_car=True, sessions=((0, 30), (120, 150)), load_gap=None)
        # The load sensor has not reported the last 20 minutes of the charge yet
        my_predbat.load_minutes = {minute: (value if minute >= 20 else my_predbat.load_minutes[20]) for minute, value in my_predbat.load_minutes.items()}
        check("t10 skipped", my_predbat.car_energy_exceeds_load_windows() == 0, "windows {}".format(my_predbat.car_energy_exceeds_load_windows()))

        print("Test 11: sessions that start and end part-way through a window, with the load sensor up to 15 minutes either side, are silent")
        # The window a session starts in holds car minutes the lagging load sensor has not reported yet, and with the
        # car sensor late the same happens in the window it ends in - only a load window shifted by the lag lines them up
        for car_kw in (7.0, 11.0):
            for lag in (10, 15, -10, -15):
                for offset in range(0, 30, 5):
                    sessions = ((60 + offset, 120 + offset), (240 + offset, 300 + offset), (420 + offset, 480 + offset))
                    _setup(my_predbat, load_includes_car=True, lag=lag, sessions=sessions, car_kw=car_kw)
                    windows = my_predbat.car_energy_exceeds_load_windows()
                    check("t11 {} kW lag {} offset {}".format(car_kw, lag, offset), windows == 0, "windows {}".format(windows))

        print("Test 12: a load sensor that excludes the charger still warns when the sessions are not aligned to the windows")
        _setup(my_predbat, load_includes_car=False, sessions=((70, 190),))
        windows = my_predbat.car_energy_exceeds_load_windows()
        check("t12 counts", windows == 5, "windows {}".format(windows))

        print("Test 14: a cycle whose history fetch came back empty does not re-arm the warning")
        logged.clear()
        my_predbat.car_energy_reported_load_warned = False
        _setup(my_predbat, load_includes_car=False)
        my_predbat.check_car_energy_reported_load()
        check("t14 warns", len(_warnings(logged)) == 1, "logged {}".format(logged))
        for field in ("car_charging_energy", "load_minutes"):
            _setup(my_predbat, load_includes_car=False)
            setattr(my_predbat, field, {})
            my_predbat.check_car_energy_reported_load()
            check("t14 {} empty keeps the flag".format(field), my_predbat.car_energy_reported_load_warned)
            _setup(my_predbat, load_includes_car=False)
            my_predbat.check_car_energy_reported_load()
            check("t14 not repeated after {} empty".format(field), len(_warnings(logged)) == 1, "logged {}".format(logged))
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)

    failed |= _test_fetch_only_checks_live_plan()

    print("*** car_energy_reported_load mismatch warning test {}".format("FAILED" if failed else "PASSED"))
    return failed


def _test_fetch_only_checks_live_plan():
    """
    Test 13: fetch_sensor_data() runs the check for the live plan (save=True) but not for compare.py's per-tariff
    re-fetches (save=False). A comparison tariff that turns car_energy_reported_load Off must not clear the live
    warned flag, or the warning would re-arm and be logged again after every comparison run.

    Drives the real fetch_sensor_data() on a fresh PredBat, so the attributes it resets do not leak into the shared
    fixture (#5079). It does still set the process-wide metrics() gauges (load/import/export/PV today, data age), as
    every fetch does; the metrics tests set their own values before reading them.
    """
    # Imported here: unit_test imports this module
    from unit_test import create_predbat

    print("Test 13: fetch_sensor_data() only runs the check for the live plan, not compare.py's save=False runs")
    failed = False
    fresh = create_predbat()
    # Configure the two sensors the stand-ins answer for, rather than relying on coverage/apps.yaml, and keep the
    # load history off the GE Cloud path, which would bypass minute_data_load()
    fresh.args["load_today"] = "sensor.test_load_today"
    fresh.args["car_charging_energy"] = "sensor.test_car_charging_energy"
    fresh.args.pop("ge_cloud_data", None)
    # Sensor history where the charger is evidently outside the load sensor. The test HA history carries no
    # last_updated, so the history readers are stood in for: load_today and car_charging_energy get these series,
    # everything else nothing.
    _setup(fresh, load_includes_car=False)
    load_series = dict(fresh.load_minutes)
    car_series = dict(fresh.car_charging_energy)
    logged = []

    def minute_data_load(now_utc, entity_name, *args, **kwargs):
        """
        Stand-in for minute_data_load(): the load series for load_today, nothing for anything else.
        """
        return (dict(load_series) if entity_name == "load_today" else {}), 0

    def minute_data_import_export(max_days_previous, now_utc, key, *args, **kwargs):
        """
        Stand-in for minute_data_import_export(): the car series for car_charging_energy, nothing for anything else.
        """
        return dict(car_series) if key == "car_charging_energy" else {}

    def step(name, save, switch, calls, warnings, warned):
        """
        Run one fetch with the switch as given and check the running totals of check calls and warnings, and the flag.
        """
        nonlocal failed
        fresh.car_energy_reported_load = switch
        fresh.fetch_sensor_data(save=save)
        result = (checked.call_count, len(_warnings(logged)), fresh.car_energy_reported_load_warned)
        if result != (calls, warnings, warned):
            print("ERROR: t13 {} gave (check calls, warnings, warned flag) {}, expected {}".format(name, result, (calls, warnings, warned)))
            failed = True

    with patch.object(fresh, "minute_data_load", side_effect=minute_data_load), patch.object(fresh, "minute_data_import_export", side_effect=minute_data_import_export), patch.object(
        fresh, "log", side_effect=lambda message, *args, **kwargs: logged.append(message)
    ), patch.object(fresh, "check_car_energy_reported_load", wraps=fresh.check_car_energy_reported_load) as checked:
        fresh.car_energy_reported_load_warned = False
        step("live cycle", save=True, switch=True, calls=1, warnings=1, warned=True)
        # A comparison tariff whose config turns the switch Off
        step("comparison run", save=False, switch=False, calls=1, warnings=1, warned=True)
        step("next live cycle", save=True, switch=True, calls=2, warnings=1, warned=True)
    return failed

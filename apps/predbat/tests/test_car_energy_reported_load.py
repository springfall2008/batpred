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

        print("Test 6: no load or car data is silent, and a short load history is scanned only as far as it goes")
        _setup(my_predbat, load_includes_car=False)
        my_predbat.car_charging_energy = {}
        my_predbat.check_car_energy_reported_load()
        _setup(my_predbat, load_includes_car=False)
        my_predbat.load_minutes = {}
        my_predbat.check_car_energy_reported_load()
        check("t6 silent without data", not _warnings(logged), "logged {}".format(logged))
        _setup(my_predbat, load_includes_car=False, history=130)
        check("t6 short history", my_predbat.car_energy_exceeds_load_windows() == 2, "windows {}".format(my_predbat.car_energy_exceeds_load_windows()))

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
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)

    print("*** car_energy_reported_load mismatch warning test {}".format("FAILED" if failed else "PASSED"))
    return failed

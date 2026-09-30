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


def _charging(minute, start=60, end=180):
    """
    The car charges from end to start minutes ago (two hours, by default one to three hours ago).
    """
    return start <= minute < end


def _setup(my_predbat, load_includes_car, lag=0, charge_end=180, history=24 * 60 + 60):
    """
    Give my_predbat a car_charging_energy series and a load series, with the car charging for charge_end - 60 minutes.
    """
    my_predbat.car_charging_energy = _incrementing(lambda minute: CAR_KW / 60 if _charging(minute, end=charge_end) else 0.0, history)
    if load_includes_car:
        my_predbat.load_minutes = _incrementing(lambda minute: (HOUSE_KW + (CAR_KW if _charging(minute + lag, end=charge_end) else 0.0)) / 60, history)
    else:
        my_predbat.load_minutes = _incrementing(lambda minute: HOUSE_KW / 60, history)


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
        _setup(my_predbat, load_includes_car=False, charge_end=90)
        my_predbat.check_car_energy_reported_load()
        check("t5 silent", not _warnings(logged), "logged {}".format(logged))

        print("Test 6: no load or car data is silent, and a short load history is scanned only as far as it goes")
        my_predbat.car_charging_energy = {}
        _setup(my_predbat, load_includes_car=False)
        my_predbat.car_charging_energy = {}
        my_predbat.check_car_energy_reported_load()
        _setup(my_predbat, load_includes_car=False)
        my_predbat.load_minutes = {}
        my_predbat.check_car_energy_reported_load()
        check("t6 silent without data", not _warnings(logged), "logged {}".format(logged))
        _setup(my_predbat, load_includes_car=False, history=130)
        check("t6 short history", my_predbat.car_energy_exceeds_load_windows() == 2, "windows {}".format(my_predbat.car_energy_exceeds_load_windows()))
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)

    print("*** car_energy_reported_load mismatch warning test {}".format("FAILED" if failed else "PASSED"))
    return failed

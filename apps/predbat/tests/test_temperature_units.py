# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for temperature support in the shared unit conversion (#5153): unit_conversion() and minute_data(required_unit=...)."""

from datetime import datetime, timedelta, timezone

from utils import convert_temperature, minute_data


def _record(now, minutes_ago, state, unit):
    """Build one history record with its unit of measurement."""
    return {"state": str(state), "last_updated": (now - timedelta(minutes=minutes_ago)).isoformat(), "attributes": {"unit_of_measurement": unit}}


def _load(records, now, required_unit, scale=1.0):
    """Load history records through minute_data as the entity loaders do."""
    data, _ = minute_data(records, 1, now, "state", "last_updated", backwards=True, smoothing=False, scale=scale, required_unit=required_unit)
    return data


def test_temperature_unit_conversion(my_predbat):
    """Temperatures convert between °C, °F and K through the shared paths, and every other unit behaves as before."""
    print("**** Running Test: temperature_units ****")
    failed = False

    def check(label, got, expected):
        """Report a value that differs from what is expected."""
        nonlocal failed
        if got is None or expected is None or isinstance(got, str) or isinstance(expected, str):
            matches = got == expected
        else:
            matches = abs(got - expected) < 0.001
        if not matches:
            print("ERROR: {} expected {}, got {}".format(label, expected, got))
            failed = True

    # The conversion itself
    check("68 °F", convert_temperature(68.0, "°F", "°C"), 20.0)
    check("293.15 K", convert_temperature(293.15, "K", "°C"), 20.0)
    check("20 °C to °F", convert_temperature(20.0, "°C", "°F"), 68.0)
    check("°F to K", convert_temperature(32.0, "°F", "K"), 273.15)
    check("C spelled without the degree sign", convert_temperature(20.0, "C", "°C"), 20.0)
    for spelling in ("c", "°c", "ºC", "˚C", "° C", "degC", "Celsius", "℃"):
        check("spelled {}".format(spelling), convert_temperature(20.0, spelling, "°C"), 20.0)
    for spelling in ("f", "°f", "ºF", "Fahrenheit", "℉"):
        check("spelled {}".format(spelling), convert_temperature(68.0, spelling, "°C"), 20.0)
    check("degrees names no scale", convert_temperature(20.0, "degrees", "°C"), None)
    check("not a temperature unit", convert_temperature(20.0, "kW", "°C"), None)
    check("no unit", convert_temperature(20.0, None, "°C"), None)

    # unit_conversion: what get_arg and get_state_wrapper use
    convert = my_predbat.unit_conversion
    check("unit_conversion °F", convert("sensor.t", 68.0, "°F", "°C"), 20.0)
    check("unit_conversion numeric string", convert("sensor.t", "68", "°F", "°C"), 20.0)
    check("unit_conversion K (not the kilo prefix)", convert("sensor.t", 293.15, "K", "°C"), 20.0)
    check("unit_conversion same unit", convert("sensor.t", 20.0, "°C", "°C"), 20.0)
    check("unit_conversion going to °F", convert("sensor.t", 20.0, "°F", "°C", going_to=True), 68.0)
    check("unit_conversion unavailable", convert("sensor.t", "unavailable", "°F", "°C"), "unavailable")

    # Every other unit keeps its path, including the leading K that means kilo
    check("kW to W", convert("sensor.p", 5.0, "kW", "W"), 5000.0)
    check("KW to W", convert("sensor.p", 5.0, "KW", "W"), 5000.0)
    check("W to kW", convert("sensor.p", 1500.0, "W", "kW"), 1.5)
    check("Wh to kWh", convert("sensor.e", 1500.0, "Wh", "kWh"), 1.5)
    check("MW to kW", convert("sensor.p", 2.0, "MW", "kW"), 2000.0)
    check("unconvertible units unchanged", convert("sensor.p", 5.0, "%", "°C"), 5.0)

    # minute_data: what history loading uses
    now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
    check("history in °F", _load([_record(now, 60, 68, "°F"), _record(now, 0, 68, "°F")], now, "°C").get(0), 20.0)
    check("history in °C", _load([_record(now, 60, 20, "°C"), _record(now, 0, 20, "°C")], now, "°C").get(0), 20.0)
    check("history in K", _load([_record(now, 60, 293.15, "K"), _record(now, 0, 293.15, "K")], now, "°C").get(0), 20.0)
    # Averaging scales each sensor: the offset in °F must be removed before scaling, not after
    check("scaled history in °F", _load([_record(now, 60, 68, "°F"), _record(now, 0, 68, "°F")], now, "°C", scale=0.5).get(0), 10.0)
    # Each record is converted from the unit on it
    mixed = _load([_record(now, 120, 20, "°C"), _record(now, 60, 68, "°F"), _record(now, 0, 68, "°F")], now, "°C")
    check("mixed history newest", mixed.get(0), 20.0)
    check("mixed history oldest", mixed.get(119), 20.0)

    # Other units in minute_data behave as before
    check("history kW to W", _load([_record(now, 60, 2, "kW"), _record(now, 0, 2, "kW")], now, "W").get(0), 2000.0)
    check("scaled history kW to W", _load([_record(now, 60, 2, "kW"), _record(now, 0, 2, "kW")], now, "W", scale=0.5).get(0), 1000.0)
    check("history W to kW", _load([_record(now, 60, 1500, "W"), _record(now, 0, 1500, "W")], now, "kW").get(0), 1.5)
    if _load([_record(now, 60, 50, "%"), _record(now, 0, 50, "%")], now, "°C").get(0):
        print("ERROR: a record in a unit that can't be converted should still be dropped")
        failed = True
    return failed

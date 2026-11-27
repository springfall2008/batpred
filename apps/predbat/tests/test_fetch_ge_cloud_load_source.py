# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""
The load source fetch_sensor_data() chooses with ge_cloud_data on: GE Cloud Data's history only while the GE Cloud
Data component exists and is active(); behind a GivEnergy EMS it declines, and the load_today path is used exactly
as if ge_cloud_data were off.
"""

from fetch import LoadSourceMissing
from tests.test_ge_cloud import _cloud_data, _direct_devices


class _LoadSourceChosen(Exception):
    """Raised by the test's hooks to stop fetch_sensor_data() at the load source it chose."""


class _Components:
    """A components registry holding GE Cloud Data only (None: not running)."""

    def __init__(self, ge_cloud_data):
        """Hold the component."""
        self.ge_cloud_data = ge_cloud_data

    def get_component(self, name):
        """The GE Cloud Data component by its registry name; None for any other."""
        return self.ge_cloud_data if name == "gecloud_data" else None


EMS_DEVICES = {"ems": "ems001", "gateway": None, "battery": ["battery001", "battery002"], "pv": [], "battery_meters": {}}


def _load_source(my_predbat, ge_cloud_data, args, outcome=None):
    """
    Run the real fetch_sensor_data() with the given args and GE Cloud Data component, and return (source, logs):
    source is "ge_cloud" or "load_today", whichever the fetch chose, stopped there. Restores the instance and its args.

    With outcome (a dict) the recorded status messages are collected in outcome["statuses"] and a LoadSourceMissing
    is caught into outcome["raised"] rather than raised.
    """
    saved_state = dict(my_predbat.__dict__)
    saved_args = dict(my_predbat.args)
    logs = []
    chosen = []
    try:
        for key in ("load_today", "load_forecast", "ge_cloud_data", "load_power", "load_ml_enable", "load_ml_source", "iboost_energy_today"):
            my_predbat.args.pop(key, None)
        my_predbat.args.update(args)
        my_predbat.components = _Components(ge_cloud_data)
        my_predbat.fetch_extra_load_forecast = lambda *a, **k: ({}, [])
        my_predbat.log = lambda message, *a, **k: logs.append(message)
        real_download = my_predbat.download_ge_data

        def download(now_utc):
            """The GE Cloud path: run the real download, then stop."""
            chosen.append("ge_cloud")
            real_download(now_utc)
            raise _LoadSourceChosen()

        def load_today():
            """The load_today path: stop."""
            chosen.append("load_today")
            raise _LoadSourceChosen()

        my_predbat.download_ge_data = download
        my_predbat.fetch_load_today = load_today
        if outcome is not None:
            outcome["statuses"] = []
            outcome["raised"] = None
            my_predbat.record_status = lambda message, **k: outcome["statuses"].append(message)
        try:
            my_predbat.fetch_sensor_data(save=False)
        except _LoadSourceChosen:
            pass
        except LoadSourceMissing as e:
            if outcome is None:
                raise
            outcome["raised"] = e
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        for name in [name for name in my_predbat.__dict__ if name not in saved_state]:
            delattr(my_predbat, name)
        my_predbat.__dict__.update(saved_state)
    return (chosen[0] if chosen else None), logs


def test_fetch_ge_cloud_load_source(my_predbat):
    """fetch_sensor_data() takes GE Cloud Data's history only while the component is active; behind an EMS, load_today."""
    failed = 0
    print("Test fetch_ge_cloud_load_source: behind an EMS ge_cloud_data falls back to load_today")
    args = {"ge_cloud_data": True, "load_today": "sensor.load_today"}
    no_data_warning = "Warn: No GE Cloud data returned from GECloudData component, check if it is configured correctly"

    # No EMS: the GE Cloud path, as before
    source, logs = _load_source(my_predbat, _cloud_data(_direct_devices("battery001")), args)
    if source != "ge_cloud":
        print("ERROR: no EMS: expected the GE Cloud path, got {}".format(source))
        failed = 1

    # Behind an EMS with ge_cloud_data still True: load_today, and no "no GE Cloud data" warning
    ems_data = _cloud_data(EMS_DEVICES)
    source, logs = _load_source(my_predbat, ems_data, args)
    if source != "load_today":
        print("ERROR: behind an EMS: expected the load_today path, got {}".format(source))
        failed = 1
    if no_data_warning in logs:
        print("ERROR: behind an EMS the GE Cloud data warning should not be logged: {}".format(logs))
        failed = 1
    if ems_data.config_args.get("ge_cloud_data", None) is not None:
        print("ERROR: ge_cloud_data must not be written: {}".format(ems_data.config_args))
        failed = 1

    # ge_cloud_data on but GE Cloud Data not running: load_today, as with it off
    source, _ = _load_source(my_predbat, None, args)
    if source != "load_today":
        print("ERROR: no GE Cloud Data component: expected the load_today path, got {}".format(source))
        failed = 1

    # ge_cloud_data off: load_today whatever the component says
    source, _ = _load_source(my_predbat, _cloud_data(_direct_devices("battery001")), {"ge_cloud_data": False, "load_today": "sensor.load_today"})
    if source != "load_today":
        print("ERROR: ge_cloud_data off: expected the load_today path, got {}".format(source))
        failed = 1

    # Behind an EMS with no load_today either: LoadSourceMissing, and the error says why GE Cloud Data is not the source
    hint = "GE Cloud Data is not used behind a GivEnergy EMS - set load_today"
    outcome = {}
    source, logs = _load_source(my_predbat, _cloud_data(EMS_DEVICES), {"ge_cloud_data": True}, outcome=outcome)
    if not isinstance(outcome["raised"], LoadSourceMissing) or hint not in str(outcome["raised"]):
        print("ERROR: behind an EMS with no load_today: expected LoadSourceMissing naming the EMS, got {!r}".format(outcome["raised"]))
        failed = 1
    if not outcome["statuses"] or hint not in outcome["statuses"][-1]:
        print("ERROR: behind an EMS with no load_today: status {} does not contain {!r}".format(outcome["statuses"], hint))
        failed = 1
    if not [line for line in logs if line.startswith("Error:") and hint in line]:
        print("ERROR: behind an EMS with no load_today: no Error line naming the EMS: {}".format(logs))
        failed = 1

    # Not declining (ge_cloud_data off, or GE Cloud Data not running): the error says nothing about an EMS
    for name, ge_cloud_data, args in (("ge_cloud_data off", _cloud_data(EMS_DEVICES), {"ge_cloud_data": False}), ("no GE Cloud Data component", None, {"ge_cloud_data": True})):
        outcome = {}
        source, logs = _load_source(my_predbat, ge_cloud_data, args, outcome=outcome)
        if not isinstance(outcome["raised"], LoadSourceMissing):
            print("ERROR: {}: expected LoadSourceMissing, got {!r}".format(name, outcome["raised"]))
            failed = 1
        if "EMS" in str(outcome["raised"]) or any("EMS" in status for status in outcome["statuses"]) or any("behind a GivEnergy EMS" in line for line in logs):
            print("ERROR: {}: the error should not mention an EMS: {!r} {} {}".format(name, outcome["raised"], outcome["statuses"], logs))
            failed = 1

    return failed

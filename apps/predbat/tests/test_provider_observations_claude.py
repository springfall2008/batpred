# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test telemetry observations from the GivEnergy Cloud, Teslemetry, Hanchu and Sunsynk components
# -----------------------------------------------------------------------------

"""Tests that GE Cloud, Teslemetry, Hanchu and Sunsynk report only SoC accepted from the current response.

Each test drives the real poll method through the existing provider test doubles and records what an
optional base.component_telemetry_observer receives. Covered per provider: an accepted reading, a valid
zero, invalid or missing readings (which must never become a measured zero), held or fallback values
(never observed), unknown sample time (None), replays (reported again) and a failing observer (which
must not change the method's return value or what it publishes).
"""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import copy
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from hanchu_const import HANCHU_TELEMETRY, HANCHU_SOC_SCALE, HANCHU_DEVICE_STATUS_FIELD, HANCHU_DEVICE_STATUS_OFFLINE
from tests.test_infra import run_async
from tests.test_ge_cloud import MockGECloudDirect
from tests.test_teslemetry import MockTeslemetryAPI, LIVE_STATUS
from tests.test_hanchu_api import MockHanchu
from tests.test_sunsynk_api import MockSunsynk

GE_SERIAL = "AB1234G567"
SUNSYNK_SN = "2405116013"
HANCHU_SN = "HC0001"
LIVE_STATUS_PATH = "/api/1/energy_sites/123456/live_status"


class ObservationRecorder:
    """Callable observer that records every event, optionally raising after recording it."""

    def __init__(self, fail=False):
        """Start with no events; fail makes every call raise after recording."""
        self.events = []
        self.fail = fail

    def __call__(self, event):
        """Record one event, then raise when configured to fail."""
        self.events.append(event)
        if self.fail:
            raise RuntimeError("observer exploded with secret-token")


def _check(failures, condition, message):
    """Append message to failures when condition is false."""
    if not condition:
        failures.append(message)


def _check_event(failures, label, event, device_id, source, field, value, sample_time):
    """Check one observation event's identity, field, value and sample time."""
    _check(failures, event["device_id"] == device_id, "{}: device_id {!r}".format(label, event["device_id"]))
    _check(failures, event["source"] == source, "{}: source {!r}".format(label, event["source"]))
    _check(failures, event["values"].get(field) == value, "{}: values {!r}".format(label, event["values"]))
    _check(failures, field in event["updated_fields"], "{}: updated_fields {!r}".format(label, event["updated_fields"]))
    _check(failures, event["held_fields"] == frozenset(event["values"]) - event["updated_fields"], "{}: held_fields {!r}".format(label, event["held_fields"]))
    _check(failures, event["fallback"] is False, "{}: fallback {!r}".format(label, event["fallback"]))
    _check(failures, event["sample_time"] == sample_time, "{}: sample_time {!r}".format(label, event["sample_time"]))


def _check_failure_logged(failures, label, log_messages):
    """Check that an observer failure was logged by type name only, without its message."""
    joined = "\n".join(str(message) for message in log_messages)
    _check(failures, "telemetry observer failed (RuntimeError)" in joined, "{}: observer failure not logged".format(label))
    _check(failures, "secret-token" not in joined, "{}: observer exception text leaked into the log".format(label))


# --- GivEnergy Cloud ------------------------------------------------------------------------


def _ge_status(api, result, previous=None):
    """Run the real async_get_inverter_status against a canned raw response."""
    api.async_get_inverter_data_retry = AsyncMock(return_value=copy.deepcopy(result))
    return run_async(api.async_get_inverter_status(GE_SERIAL, previous))


def _ge_api(observer):
    """Build a GE Cloud test double, attaching observer when it is not None."""
    api = MockGECloudDirect()
    if observer is not None:
        api.base.component_telemetry_observer = observer
    return api


def test_ge_observations(failures):
    """GE Cloud observes only the raw battery percent of this status response."""
    stamp = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    accepted = {"time": "2026-11-04T10:00:00Z", "battery": {"percent": 57, "power": 100}}

    observer = ObservationRecorder()
    api = _ge_api(observer)
    previous = {"battery": {"percent": 80}, "time": "2026-11-04T09:55:00Z"}
    merged = _ge_status(api, accepted, previous)
    _check(failures, len(observer.events) == 1, "ge accepted: {} events".format(len(observer.events)))
    if observer.events:
        _check_event(failures, "ge accepted", observer.events[0], GE_SERIAL.lower(), "inverter_status", "soc_percent", 57.0, stamp)

    # Replay: the identical response is reported again.
    _ge_status(api, accepted, merged)
    _check(failures, len(observer.events) == 2, "ge replay: {} events".format(len(observer.events)))

    # A valid zero is a measurement.
    observer = ObservationRecorder()
    api = _ge_api(observer)
    _ge_status(api, {"time": "2026-11-04T10:00:00+00:00", "battery": {"percent": 0}})
    _check(failures, len(observer.events) == 1 and observer.events[0]["values"] == {"soc_percent": 0.0}, "ge zero: {!r}".format(observer.events))

    # Unknown or naive time is None, never invented.
    for label, stamp_value in (("missing", None), ("naive", "2026-11-04T10:00:00"), ("garbage", "yesterday")):
        observer = ObservationRecorder()
        api = _ge_api(observer)
        result = {"battery": {"percent": 40}}
        if stamp_value is not None:
            result["time"] = stamp_value
        _ge_status(api, result)
        _check(failures, len(observer.events) == 1 and observer.events[0]["sample_time"] is None, "ge {} time: {!r}".format(label, observer.events))

    # Held, missing and invalid readings emit nothing; the merged return still carries the held value.
    for label, result in (
        ("no battery", {"time": "2026-11-04T10:00:00Z", "solar": {"power": 1}}),
        ("null battery", {"battery": None}),
        ("null percent", {"battery": {"percent": None}}),
        ("nan percent", {"battery": {"percent": float("nan")}}),
        ("text percent", {"battery": {"percent": "abc"}}),
        ("bool percent", {"battery": {"percent": True}}),
        ("over range", {"battery": {"percent": 150}}),
        ("failed read", None),
        ("auth denied", {}),
    ):
        observer = ObservationRecorder()
        api = _ge_api(observer)
        held = {"battery": {"percent": 80}}
        returned = _ge_status(api, result, held)
        _check(failures, observer.events == [], "ge {}: emitted {!r}".format(label, observer.events))
        if result is None:
            _check(failures, returned is held, "ge {}: previous not returned".format(label))

    # A failing observer changes neither the return nor the display sample.
    baseline = _ge_api(None)
    expected = _ge_status(baseline, accepted, {"battery": {"percent": 80}})
    observer = ObservationRecorder(fail=True)
    api = _ge_api(observer)
    returned = _ge_status(api, accepted, {"battery": {"percent": 80}})
    _check(failures, returned == expected, "ge failing observer changed return {!r}".format(returned))
    _check(failures, api.live_display_samples == baseline.live_display_samples, "ge failing observer changed display samples")
    _check(failures, len(observer.events) == 1, "ge failing observer not called")
    _check_failure_logged(failures, "ge", api.log_messages)


# --- Teslemetry ----------------------------------------------------------------------------


def _tesla_api(observer, live_status):
    """Build a Teslemetry test double with a canned live_status, attaching observer when not None."""
    api = MockTeslemetryAPI()
    if observer is not None:
        api.base.component_telemetry_observer = observer
    api.mock_responses[LIVE_STATUS_PATH] = copy.deepcopy(live_status)
    return api


def test_tesla_observations(failures):
    """Teslemetry observes only the response's own percentage_charged, never last_soc."""
    stamp = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    timed = copy.deepcopy(LIVE_STATUS)
    timed["response"]["timestamp"] = "2026-11-04T10:00:00Z"

    observer = ObservationRecorder()
    api = _tesla_api(observer, timed)
    ok = run_async(api.fetch_live_status())
    _check(failures, ok is True, "tesla accepted returned {!r}".format(ok))
    _check(failures, len(observer.events) == 1, "tesla accepted: {} events".format(len(observer.events)))
    if observer.events:
        _check_event(failures, "tesla accepted", observer.events[0], "123456", "live_status", "percentage_charged", 55.5, stamp)
        _check(failures, observer.events[0]["values"]["load_power"] == timed["response"]["load_power"], "tesla progress input differs from applied load")
        _check(failures, "energy_left" in observer.events[0]["held_fields"], "missing Tesla energy is not a measured input")

    # Replay: the identical response is reported again, with no deduplication.
    run_async(api.fetch_live_status())
    _check(failures, len(observer.events) == 2, "tesla replay: {} events".format(len(observer.events)))

    # No timestamp means unknown time.
    observer = ObservationRecorder()
    api = _tesla_api(observer, LIVE_STATUS)
    run_async(api.fetch_live_status())
    _check(failures, len(observer.events) == 1 and observer.events[0]["sample_time"] is None, "tesla unknown time: {!r}".format(observer.events))

    # A valid zero is a measurement.
    zero = copy.deepcopy(LIVE_STATUS)
    zero["response"]["percentage_charged"] = 0
    observer = ObservationRecorder()
    api = _tesla_api(observer, zero)
    run_async(api.fetch_live_status())
    _check(failures, len(observer.events) == 1 and observer.events[0]["values"]["percentage_charged"] == 0.0, "tesla zero: {!r}".format(observer.events))

    # The last_soc fallback is published but never observed.
    held = copy.deepcopy(LIVE_STATUS)
    del held["response"]["percentage_charged"]
    observer = ObservationRecorder()
    api = _tesla_api(observer, held)
    api.last_soc = 40
    ok = run_async(api.fetch_live_status())
    _check(failures, ok is True and observer.events == [], "tesla held: ok={!r} events={!r}".format(ok, observer.events))

    # Invalid values, error bodies and failed requests emit nothing.
    invalid_cases = []
    # Text is left out: the existing publish path calls float() on it, which is unchanged behaviour.
    for label, value in (("null", None), ("nan", float("nan")), ("bool", True), ("negative", -5), ("over range", 101)):
        bad = copy.deepcopy(LIVE_STATUS)
        bad["response"]["percentage_charged"] = value
        invalid_cases.append((label, bad))
    invalid_cases.append(("error body", {"error": "vehicle unavailable", "response": None}))
    invalid_cases.append(("failed request", None))
    for label, live in invalid_cases:
        observer = ObservationRecorder()
        api = _tesla_api(observer, live)
        if live is None:
            api.mock_responses.pop(LIVE_STATUS_PATH, None)
        run_async(api.fetch_live_status())
        _check(failures, observer.events == [], "tesla {}: emitted {!r}".format(label, observer.events))

    # A failing observer changes neither the return nor what is published.
    baseline = _tesla_api(None, timed)
    expected_ok = run_async(baseline.fetch_live_status())
    observer = ObservationRecorder(fail=True)
    api = _tesla_api(observer, timed)
    ok = run_async(api.fetch_live_status())
    _check(failures, ok == expected_ok, "tesla failing observer changed return {!r}".format(ok))
    _check(failures, api.dashboard_items == baseline.dashboard_items, "tesla failing observer changed publication")
    _check(failures, len(observer.events) == 1, "tesla failing observer not called")
    _check_failure_logged(failures, "tesla", api.log_messages)


# --- Hanchu ---------------------------------------------------------------------------------


def _hanchu_api(observer):
    """Build a Hanchu test double, attaching observer when it is not None."""
    api = MockHanchu()
    if observer is not None:
        api.base.component_telemetry_observer = observer
    return api


def _hanchu_payload(soc, status=1):
    """Return a minimal getDeviceStatus payload with the given raw SoC fraction and devStatus."""
    payload = {HANCHU_TELEMETRY["soc"]: soc}
    if status is not None:
        payload[HANCHU_DEVICE_STATUS_FIELD] = status
    return payload


def test_hanchu_observations(failures):
    """Hanchu observes the normalised SoC only after a payload from an online device is applied."""
    observer = ObservationRecorder()
    api = _hanchu_api(observer)
    ok = api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.62))
    expected = round(0.62 * HANCHU_SOC_SCALE, 1)
    _check(failures, ok is True, "hanchu accepted returned {!r}".format(ok))
    _check(failures, len(observer.events) == 1, "hanchu accepted: {} events".format(len(observer.events)))
    if observer.events:
        _check_event(failures, "hanchu accepted", observer.events[0], HANCHU_SN, "device_status", "soc", expected, None)
        _check(failures, observer.events[0]["values"]["soc"] == api.device_values[HANCHU_SN]["soc"], "hanchu value differs from applied value")

    # Replay and an unknown devStatus are both still reported.
    api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.62, status=None))
    _check(failures, len(observer.events) == 2, "hanchu replay: {} events".format(len(observer.events)))

    # A valid zero is a measurement.
    observer = ObservationRecorder()
    api = _hanchu_api(observer)
    api._apply_status_payload(HANCHU_SN, _hanchu_payload(0))
    _check(failures, len(observer.events) == 1 and observer.events[0]["values"] == {"soc": 0.0}, "hanchu zero: {!r}".format(observer.events))

    # Offline means cached readings: applied as before, never observed.
    observer = ObservationRecorder()
    api = _hanchu_api(observer)
    ok = api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.5, status=HANCHU_DEVICE_STATUS_OFFLINE))
    _check(failures, ok is True and observer.events == [], "hanchu offline: ok={!r} events={!r}".format(ok, observer.events))

    api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.5, status="0"))
    _check(failures, observer.events == [], "hanchu string offline emitted a sample")
    api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.5, status=None))
    _check(failures, len(observer.events) == 1, "hanchu unknown status inherited a stale offline flag")

    # Invalid fractions are applied as the component always has, but never observed.
    for label, soc in (("text", "abc"), ("nan", float("nan")), ("over range", 1.5), ("negative", -0.1), ("bool", True)):
        observer = ObservationRecorder()
        api = _hanchu_api(observer)
        api._apply_status_payload(HANCHU_SN, _hanchu_payload(soc))
        _check(failures, observer.events == [], "hanchu {}: emitted {!r}".format(label, observer.events))

    # A payload that is not applied emits nothing.
    for label, payload in (("no soc", {HANCHU_DEVICE_STATUS_FIELD: 1}), ("not a dict", None)):
        observer = ObservationRecorder()
        api = _hanchu_api(observer)
        ok = api._apply_status_payload(HANCHU_SN, payload)
        _check(failures, ok is False and observer.events == [], "hanchu {}: ok={!r} events={!r}".format(label, ok, observer.events))

    # A failing observer changes neither the return nor the applied values.
    baseline = _hanchu_api(None)
    expected_ok = baseline._apply_status_payload(HANCHU_SN, _hanchu_payload(0.62))
    observer = ObservationRecorder(fail=True)
    api = _hanchu_api(observer)
    ok = api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.62))
    _check(failures, ok == expected_ok, "hanchu failing observer changed return {!r}".format(ok))
    _check(failures, api.device_values == baseline.device_values, "hanchu failing observer changed applied values")
    _check(failures, len(observer.events) == 1, "hanchu failing observer not called")
    _check_failure_logged(failures, "hanchu", api.log_messages)


# --- Sunsynk --------------------------------------------------------------------------------


def _sunsynk_api(observer, battery):
    """Build a Sunsynk test double whose realtime reads return battery and fixed grid/load/input data."""
    api = MockSunsynk()
    if observer is not None:
        api.base.component_telemetry_observer = observer
    responses = {"battery": battery, "grid": {"pac": -500}, "load": {"totalPower": 300}, "input": {"pac": 900}}

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a copy of the canned payload, or {} as _get does on failure."""
        return copy.deepcopy(responses.get(endpoint_key) or {})

    api._get = fake_get
    return api


def test_sunsynk_observations(failures):
    """Sunsynk observes only the validated soc of this poll's battery response."""
    observer = ObservationRecorder()
    api = _sunsynk_api(observer, {"soc": 62, "power": 400, "voltage": 52.1})
    values = run_async(api.fetch_device_data(SUNSYNK_SN))
    _check(failures, values.get("soc") == 62, "sunsynk accepted values {!r}".format(values))
    _check(failures, len(observer.events) == 1, "sunsynk accepted: {} events".format(len(observer.events)))
    if observer.events:
        _check_event(failures, "sunsynk accepted", observer.events[0], SUNSYNK_SN, "battery", "soc", 62, None)

    # Replay: an identical reading on the next poll is reported again.
    run_async(api.fetch_device_data(SUNSYNK_SN))
    _check(failures, len(observer.events) == 2, "sunsynk replay: {} events".format(len(observer.events)))

    # Fractional and string readings keep their measured value.
    observer = ObservationRecorder()
    api = _sunsynk_api(observer, {"soc": "62.5"})
    run_async(api.fetch_device_data(SUNSYNK_SN))
    _check(failures, len(observer.events) == 1 and observer.events[0]["values"] == {"soc": 62.5}, "sunsynk fractional: {!r}".format(observer.events))

    # A valid zero is a measurement.
    observer = ObservationRecorder()
    api = _sunsynk_api(observer, {"soc": 0, "power": 0})
    run_async(api.fetch_device_data(SUNSYNK_SN))
    _check(failures, len(observer.events) == 1 and observer.events[0]["values"] == {"soc": 0}, "sunsynk zero: {!r}".format(observer.events))

    # A failed read, a missing soc and invalid values emit nothing, even where the published value coerces to 0.
    for label, battery in (
        ("failed read", {}),
        ("no soc", {"power": 400}),
        ("null soc", {"soc": None}),
        ("text soc", {"soc": "abc"}),
        ("nan soc", {"soc": float("nan")}),
        ("bool soc", {"soc": True}),
        ("over range", {"soc": 250}),
    ):
        observer = ObservationRecorder()
        api = _sunsynk_api(observer, battery)
        run_async(api.fetch_device_data(SUNSYNK_SN))
        _check(failures, observer.events == [], "sunsynk {}: emitted {!r}".format(label, observer.events))

    # A failing observer changes neither the return nor the stored telemetry.
    battery = {"soc": 62, "power": 400}
    baseline = _sunsynk_api(None, battery)
    expected = run_async(baseline.fetch_device_data(SUNSYNK_SN))
    observer = ObservationRecorder(fail=True)
    api = _sunsynk_api(observer, battery)
    returned = run_async(api.fetch_device_data(SUNSYNK_SN))
    _check(failures, returned == expected, "sunsynk failing observer changed return {!r}".format(returned))
    _check(failures, api.device_values == baseline.device_values and api.device_energy == baseline.device_energy, "sunsynk failing observer changed stored telemetry")
    _check(failures, len(observer.events) == 1, "sunsynk failing observer not called")
    _check_failure_logged(failures, "sunsynk", api.log_messages)


def test_no_observer_no_work(failures):
    """Without an observer every provider behaves exactly as before and nothing is reported."""
    api = _ge_api(None)
    merged = _ge_status(api, {"battery": {"percent": 57}}, {"battery": {"percent": 80}})
    _check(failures, isinstance(merged, dict) and "battery" in merged, "ge without observer returned {!r}".format(merged))
    api = _tesla_api(None, LIVE_STATUS)
    _check(failures, run_async(api.fetch_live_status()) is True, "tesla without observer failed")
    api = _hanchu_api(None)
    _check(failures, api._apply_status_payload(HANCHU_SN, _hanchu_payload(0.5)) is True, "hanchu without observer failed")
    api = _sunsynk_api(None, {"soc": 50})
    _check(failures, run_async(api.fetch_device_data(SUNSYNK_SN)).get("soc") == 50, "sunsynk without observer failed")


def run_provider_observations_claude_tests(my_predbat):
    """Run the GE Cloud, Teslemetry, Hanchu and Sunsynk observation tests; return True on failure."""
    print("**** Running provider telemetry observation tests (GE Cloud, Teslemetry, Hanchu, Sunsynk) ****")
    failures = []
    for test in (test_ge_observations, test_tesla_observations, test_hanchu_observations, test_sunsynk_observations, test_no_observer_no_work):
        try:
            test(failures)
        except Exception as exc:
            failures.append("{} raised {}: {}".format(test.__name__, type(exc).__name__, exc))
    for failure in failures:
        print("ERROR: {}".format(failure))
    return bool(failures)

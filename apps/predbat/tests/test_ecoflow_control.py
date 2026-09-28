# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test EcoFlow write path and control gates
# -----------------------------------------------------------------------------

"""Tests for the EcoFlow write path (``set_device_quota``) and the gates in front of it.

The write TRANSPORT is complete and proved here; what is missing is the Power Ocean cmdSet/id
pairs, which the published documentation does not give. These tests therefore drive the writer
with an arbitrary pair, exactly as a caller will once a real one is confirmed, and assert the
envelope, the three gates and the reply handling rather than any particular command's meaning.
"""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import time
from unittest.mock import patch
from ecoflow import EcoFlowAPI
from ecoflow_const import ECOFLOW_SCHEDULE_COMMANDS
from tests.test_ecoflow_api import MockEcoFlow, _envelope, _session_with_put
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response


def _writer(min_write_interval=300):
    """A client with control enabled and read-only off - the only state that permits a write."""
    client = MockEcoFlow(control_enable=True)
    client.min_write_interval = min_write_interval
    client.state["switch.predbat_set_read_only"] = "off"
    client.device_list = ["SN1"]
    return client


def test_ecoflow_write_sends_the_documented_envelope():
    """A write PUTs {"sn": ..., "params": {"cmdSet": N, "id": M, ...}} to /device/quota.

    The caller's extra fields sit alongside cmdSet and id inside params, not beside them: a
    field hoisted to the top level would be signed and sent in a shape the device never reads.
    """
    failed = False
    client = _writer()
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope(None)))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.set_device_quota("SN1", 11, 24, params={"eps": 0}, command="test"))
    if not ok:
        print(f"ERROR: write should have succeeded: {client.log_messages}")
        failed = True
    if not session.put.called:
        print("ERROR: the write must be a PUT")
        assert False, "test_ecoflow_write_sends_the_documented_envelope"
    args = session.put.call_args
    if not str(args.args[0]).endswith("/iot-open/sign/device/quota"):
        print(f"ERROR: url {args.args[0]}")
        failed = True
    body = args.kwargs["json"]
    if body != {"sn": "SN1", "params": {"eps": 0, "cmdSet": 11, "id": 24}}:
        print(f"ERROR: body {body}")
        failed = True
    assert not failed, "test_ecoflow_write_sends_the_documented_envelope"


def test_ecoflow_write_is_blocked_by_set_read_only():
    """switch.predbat_set_read_only holds back a write even with control enabled.

    Checked FIRST, before ecoflow_control_enable: read-only is Predbat's global write inhibit
    and has to hold on an install that has deliberately turned control on.
    """
    failed = False
    client = _writer()
    client.state["switch.predbat_set_read_only"] = "on"
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope(None)))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.set_device_quota("SN1", 11, 24))
    if ok:
        print("ERROR: read-only mode must block the write")
        failed = True
    if session.put.called:
        print("ERROR: nothing should have reached the network")
        failed = True
    if not any("set_read_only" in line for line in client.log_messages):
        print(f"ERROR: the suppression was not explained: {client.log_messages}")
        failed = True
    assert not failed, "test_ecoflow_write_is_blocked_by_set_read_only"


def test_ecoflow_write_is_blocked_when_control_is_disabled():
    """ecoflow_control_enable defaults False, and a disabled component never writes."""
    failed = False
    client = MockEcoFlow(control_enable=False)
    client.state["switch.predbat_set_read_only"] = "off"
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope(None)))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.set_device_quota("SN1", 11, 24))
    if ok or session.put.called:
        print("ERROR: a disabled component must not write")
        failed = True
    if not any("ecoflow_control_enable" in line for line in client.log_messages):
        print(f"ERROR: the suppression was not explained: {client.log_messages}")
        failed = True
    assert not failed, "test_ecoflow_write_is_blocked_when_control_is_disabled"


def test_ecoflow_write_is_paced_per_device_and_command():
    """A second write inside the interval is HELD, and a different command is unaffected.

    Held, not dropped: nothing is cached as applied, so the next eligible tick rebuilds and
    sends it. Pacing per (device, command) so a target-SoC write and a window write do not
    starve each other.
    """
    failed = False
    client = _writer(min_write_interval=300)
    session = _session_with_put([create_aiohttp_mock_response(json_data=_envelope(None)) for _ in range(4)])
    with patch("aiohttp.ClientSession", return_value=session):
        first = run_async_local(client.set_device_quota("SN1", 11, 24, command="soc"))
        second = run_async_local(client.set_device_quota("SN1", 11, 24, command="soc"))
        other = run_async_local(client.set_device_quota("SN1", 12, 1, command="window"))
    if not first:
        print("ERROR: the first write should go out")
        failed = True
    if second:
        print("ERROR: the second write inside the interval should be held")
        failed = True
    if not other:
        print("ERROR: a different command must be paced independently")
        failed = True
    if not any("held by ecoflow_min_write_interval" in line for line in client.log_messages):
        print("ERROR: the hold was not explained")
        failed = True
    # Once the interval has passed the held change goes out on the next attempt.
    client.last_write_time[("SN1", "soc")] = time.time() - 301
    with patch("aiohttp.ClientSession", return_value=_session_with_put(create_aiohttp_mock_response(json_data=_envelope(None)))):
        if not run_async_local(client.set_device_quota("SN1", 11, 24, command="soc")):
            print("ERROR: the write should be allowed once the interval has passed")
            failed = True
    assert not failed, "test_ecoflow_write_is_paced_per_device_and_command"


def test_ecoflow_rejected_write_is_still_paced():
    """A REJECTED write stamps the pacing clock too.

    Otherwise a persistently rejected write - an offline device, a bad command id - is re-sent
    on every tick forever against somebody's house battery. Nothing is cached as applied on a
    failure path, so it keeps retrying; just paced, not hammering.
    """
    failed = False
    client = _writer(min_write_interval=300)
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope(None, code="-2", message="offline")))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.set_device_quota("SN1", 11, 24, command="soc"))
    if ok:
        print("ERROR: a rejected write must report failure")
        failed = True
    if ("SN1", "soc") not in client.last_write_time:
        print("ERROR: a rejected write must still stamp the pacing clock")
        failed = True
    assert not failed, "test_ecoflow_rejected_write_is_still_paced"


def test_ecoflow_write_reports_a_device_level_reply_code():
    """A platform-accepted write whose payload carries a non-zero reply code is a failure.

    A 200 with a success envelope means the PLATFORM accepted the command, not that the device
    performed it. Where the response does carry a device reply code, a non-zero one is honoured
    rather than read as success.
    """
    failed = False
    client = _writer(min_write_interval=0)
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope({"code": -2})))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.set_device_quota("SN1", 11, 24))
    if ok:
        print("ERROR: a device reply of -2 is not a success")
        failed = True
    if not any("the device replied" in line and "offline" in line for line in client.log_messages):
        print(f"ERROR: the device reply was not reported: {client.log_messages}")
        failed = True
    # And a reply of 0 is a success.
    client = _writer(min_write_interval=0)
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope({"code": 0})))
    with patch("aiohttp.ClientSession", return_value=session):
        if not run_async_local(client.set_device_quota("SN1", 11, 24)):
            print("ERROR: a device reply of 0 is a success")
            failed = True
    assert not failed, "test_ecoflow_write_reports_a_device_level_reply_code"


def test_ecoflow_write_success_does_not_claim_the_device_acted():
    """The success log says the PLATFORM accepted it, and names where confirmation comes from.

    Overstating this is how a user concludes the battery is charging when nothing happened: the
    device's own answer arrives on the MQTT set_reply topic, which this component does not read.
    """
    failed = False
    client = _writer(min_write_interval=0)
    session = _session_with_put(create_aiohttp_mock_response(json_data=_envelope(None)))
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client.set_device_quota("SN1", 11, 24))
    success = [line for line in client.log_messages if "accepted it" in line]
    if not success:
        print(f"ERROR: no success line: {client.log_messages}")
        failed = True
    elif "set_reply" not in success[-1]:
        print(f"ERROR: the success line does not say where confirmation comes from: {success[-1]}")
        failed = True
    assert not failed, "test_ecoflow_write_success_does_not_claim_the_device_acted"


def test_ecoflow_control_bindable_follows_the_command_table():
    """control_bindable() is False today, and reads the command table rather than a flag.

    One place decides whether this component drives a battery, so the control surface and the
    discovery record's control flag cannot disagree with the table that backs them.
    """
    failed = False
    if EcoFlowAPI.control_bindable() is not False:
        print("ERROR: control_bindable should be False while ECOFLOW_SCHEDULE_COMMANDS is empty")
        failed = True
    with patch.dict(ECOFLOW_SCHEDULE_COMMANDS, {"charge_window": {"cmdSet": 1, "id": 2}}, clear=False):
        if EcoFlowAPI.control_bindable() is not True:
            print("ERROR: control_bindable should follow a populated command table")
            failed = True
    if EcoFlowAPI.control_bindable() is not False:
        print("ERROR: control_bindable should be False again once the patch is undone")
        failed = True
    assert not failed, "test_ecoflow_control_bindable_follows_the_command_table"


def test_ecoflow_pacing_state_round_trips_through_the_cache():
    """last_write_time survives a save/restore, keyed back to (device, command).

    JSON cannot carry a tuple key, so it is flattened to "sn|command". Without this a restart
    loop bypasses ecoflow_min_write_interval entirely.
    """
    failed = False
    client = MockEcoFlow()
    saved = {}

    class _Storage:
        """Minimal in-memory Storage stand-in."""

        async def save(self, module, name, data):
            """Record one saved blob."""
            saved[(module, name)] = data

        async def load(self, module, name):
            """Return a previously saved blob, or {}."""
            return saved.get((module, name), {})

    with patch.object(MockEcoFlow, "storage", property(lambda self: _Storage())):
        client.last_write_time = {("SN1", "soc"): 1234.0, ("SN2", "window"): 5678.0}
        run_async_local(client.save_control())
        restored = MockEcoFlow()
        run_async_local(restored.restore_state())
    if restored.last_write_time != {("SN1", "soc"): 1234.0, ("SN2", "window"): 5678.0}:
        print(f"ERROR: restored {restored.last_write_time}")
        failed = True
    if restored._cache_restored is not True:
        print("ERROR: a clean restore should mark the cache restored")
        failed = True
    assert not failed, "test_ecoflow_pacing_state_round_trips_through_the_cache"


def test_ecoflow_save_static_refuses_to_wipe_a_good_cache():
    """An empty device list is never written over a good cache.

    Absence of a result is not a result: writing an empty list and stamping the tier fresh would
    make a restart restore nothing and skip re-discovery for a full TTL.
    """
    failed = False
    saved = {}

    class _Storage:
        """Minimal in-memory Storage stand-in."""

        async def save(self, module, name, data):
            """Record one saved blob."""
            saved[(module, name)] = data

        async def load(self, module, name):
            """Return a previously saved blob, or {}."""
            return saved.get((module, name), {})

    with patch.object(MockEcoFlow, "storage", property(lambda self: _Storage())):
        client = MockEcoFlow()
        client.device_list = ["SN1"]
        client.device_detail = {"SN1": {"sn": "SN1"}}
        run_async_local(client.save_static())
        empty = MockEcoFlow()
        run_async_local(empty.save_static())
    if saved.get(("ecoflow", "static"), {}).get("device_list") != ["SN1"]:
        print(f"ERROR: the good cache was overwritten: {saved.get(('ecoflow', 'static'))}")
        failed = True
    assert not failed, "test_ecoflow_save_static_refuses_to_wipe_a_good_cache"


def run_ecoflow_control_tests(my_predbat):
    """Run all EcoFlow control/write-path tests."""
    failed = False
    for name, fn in [
        ("write_envelope", test_ecoflow_write_sends_the_documented_envelope),
        ("read_only_gate", test_ecoflow_write_is_blocked_by_set_read_only),
        ("control_enable_gate", test_ecoflow_write_is_blocked_when_control_is_disabled),
        ("pacing", test_ecoflow_write_is_paced_per_device_and_command),
        ("rejected_still_paced", test_ecoflow_rejected_write_is_still_paced),
        ("device_reply_code", test_ecoflow_write_reports_a_device_level_reply_code),
        ("success_is_honest", test_ecoflow_write_success_does_not_claim_the_device_acted),
        ("control_bindable", test_ecoflow_control_bindable_follows_the_command_table),
        ("pacing_round_trip", test_ecoflow_pacing_state_round_trips_through_the_cache),
        ("save_static_guard", test_ecoflow_save_static_refuses_to_wipe_a_good_cache),
    ]:
        try:
            if fn():
                print(f"  FAILED: ecoflow_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in ecoflow_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

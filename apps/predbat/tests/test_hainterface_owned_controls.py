# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
"""
Unit tests for HAInterface.call_service() routing of Predbat-published controls (#6223).

A component control made with dashboard_item(..., app=...) is published to Home Assistant as
state only, with no select/number/switch platform behind it. Sent through HA, a write to it
fires the call_service event (which is what has been completing the write) and then HA logs
"Referenced entities ... are missing". call_service() must therefore loop such writes back to
Predbat directly, for every component, while real HA entities still go through the websocket.
"""

import asyncio
import threading
import time
from unittest.mock import patch

from tests.test_hainterface_common import MockBase, create_ha_interface


def _make_ha_interface(published):
    """Build an HAInterface with an active websocket, the given published controls and a recording websocket stub."""
    mock_base = MockBase()
    mock_base.dashboard_index_app = dict(published)
    ha_interface = create_ha_interface(mock_base, ha_key="test_key", websocket_active=True)
    websocket_calls = []

    def mock_call_service_websocket_command(domain, service, data):
        """Record a websocket service call instead of sending it."""
        websocket_calls.append((domain, service, data))
        return True

    ha_interface.call_service_websocket_command = mock_call_service_websocket_command
    return ha_interface, mock_base, websocket_calls


def test_owned_controls_loop_back_for_every_component(my_predbat=None):
    """select/number/switch writes to controls published by any component are looped back, not sent to HA"""
    print("\n=== Testing call_service() loops back Predbat-published controls for every component ===")
    failed = 0
    published = {
        "select.predbat_givtcp_0_discharge_end_time": "givtcp",
        "number.predbat_sunsynk_0_reserve": "sunsynk",
        "switch.predbat_solis_0_scheduled_charge_enable": "solis",
        "switch.predbat_wallbox_control": "wallbox",
    }
    ha_interface, mock_base, websocket_calls = _make_ha_interface(published)

    ha_interface.call_service("select/select_option", entity_id="select.predbat_givtcp_0_discharge_end_time", option="19:01")
    ha_interface.call_service("number/set_value", entity_id="number.predbat_sunsynk_0_reserve", value=10)
    ha_interface.call_service("switch/turn_on", entity_id="switch.predbat_solis_0_scheduled_charge_enable")
    ha_interface.call_service("switch.turn_off", entity_id=["switch.predbat_wallbox_control"])

    if websocket_calls:
        print("ERROR: Predbat-published controls were sent to HA: {}".format(websocket_calls))
        failed += 1
    domains = [call.get("domain") for call in mock_base.callback_calls]
    if domains != ["select", "number", "switch", "switch"]:
        print("ERROR: Expected four looped-back callbacks select/number/switch/switch, got {}".format(domains))
        failed += 1
    elif mock_base.callback_calls[0]["service_data"].get("option") != "19:01" or mock_base.callback_calls[1]["service_data"].get("value") != 10:
        print("ERROR: Looped-back service data was not passed through: {}".format(mock_base.callback_calls))
        failed += 1
    elif mock_base.callback_calls[3].get("service") != "turn_off":
        print("ERROR: Expected switch.turn_off to loop back as turn_off, got {}".format(mock_base.callback_calls[3]))
        failed += 1

    return failed


def test_real_ha_entities_still_use_websocket(my_predbat=None):
    """Writes that are not wholly to Predbat-published controls still go through the HA websocket"""
    print("\n=== Testing call_service() sends real HA entities through the websocket ===")
    failed = 0
    published = {"select.predbat_givtcp_0_inverter_mode": "givtcp"}
    ha_interface, mock_base, websocket_calls = _make_ha_interface(published)

    # A real HA entity of a looped-back domain
    ha_interface.call_service("select/select_option", entity_id="select.givtcp_real_mode", option="Eco")
    # A mixed target: one published control and one real entity must not be split or looped back
    ha_interface.call_service("select/select_option", entity_id=["select.predbat_givtcp_0_inverter_mode", "select.givtcp_real_mode"], option="Eco")
    # A published entity id under a domain that is not looped back
    ha_interface.call_service("input_number/set_value", entity_id="select.predbat_givtcp_0_inverter_mode", value=1)
    # No entity_id at all (e.g. a notify or weather call)
    ha_interface.call_service("notify/notify", message="hello")

    if len(websocket_calls) != 4:
        print("ERROR: Expected 4 websocket calls, got {}: {}".format(len(websocket_calls), websocket_calls))
        failed += 1
    elif websocket_calls[0] != ("select", "select_option", {"entity_id": "select.givtcp_real_mode", "option": "Eco"}):
        print("ERROR: Real HA entity call was not passed through unchanged: {}".format(websocket_calls[0]))
        failed += 1
    if mock_base.callback_calls:
        print("ERROR: Expected no looped-back callbacks, got {}".format(mock_base.callback_calls))
        failed += 1

    return failed


def test_owned_control_runs_on_thread_and_returns_result(my_predbat=None):
    """A looped-back write runs off the caller's thread and call_service returns the handler's result once it completes"""
    print("\n=== Testing call_service() runs a Predbat-published control write on a thread and waits for it ===")
    failed = 0
    ha_interface, mock_base, websocket_calls = _make_ha_interface({"number.predbat_fox_0_reserve": "fox"})
    handler_threads = []

    async def trigger_callback(service_data):
        """Record which thread the handler ran on and report a matched listener."""
        handler_threads.append(threading.current_thread())
        await asyncio.sleep(0.05)
        return True

    mock_base.trigger_callback = trigger_callback
    result = ha_interface.call_service("number/set_value", entity_id="number.predbat_fox_0_reserve", value=20)

    if result is not True:
        print("ERROR: Expected call_service to return the handler result True, got {}".format(result))
        failed += 1
    if len(handler_threads) != 1 or handler_threads[0] is threading.current_thread():
        print("ERROR: Expected the handler to run once on another thread, ran on {}".format(handler_threads))
        failed += 1
    if websocket_calls:
        print("ERROR: Expected no websocket call, got {}".format(websocket_calls))
        failed += 1
    return failed


def test_owned_control_handler_exception_returns_none(my_predbat=None):
    """An exception raised by a component handler is logged and returned as None (failure), not raised into the caller"""
    print("\n=== Testing call_service() catches a Predbat-published control handler exception ===")
    failed = 0
    ha_interface, mock_base, _ = _make_ha_interface({"switch.predbat_deye_0_write": "deye"})

    async def trigger_callback(service_data):
        """Fail the way a cloud write does once its retries are exhausted."""
        raise RuntimeError("cloud write failed")

    mock_base.trigger_callback = trigger_callback
    try:
        result = ha_interface.call_service("switch/turn_on", entity_id="switch.predbat_deye_0_write")
    except Exception as e:
        print("ERROR: Handler exception escaped call_service: {}".format(e))
        return failed + 1

    if result is not None:
        print("ERROR: Expected None after a handler exception, got {}".format(result))
        failed += 1
    if not any("Warn:" in message and "cloud write failed" in message for message in mock_base.log_messages):
        print("ERROR: Expected a Warn naming the exception, got {}".format(mock_base.log_messages))
        failed += 1
    return failed


def test_owned_control_timeout_returns_none(my_predbat=None):
    """A handler that outlasts the service call timeout makes call_service return None with a Warn, as the websocket wait did"""
    print("\n=== Testing call_service() gives up on a slow Predbat-published control write after the timeout ===")
    failed = 0
    ha_interface, mock_base, _ = _make_ha_interface({"select.predbat_gecloud_0_mode": "gecloud"})
    finished = threading.Event()

    async def trigger_callback(service_data):
        """Take longer than the patched timeout, then note that the write still completed."""
        await asyncio.sleep(0.5)
        finished.set()
        return True

    mock_base.trigger_callback = trigger_callback
    start = time.monotonic()
    with patch("ha.HA_SERVICE_CALL_TIMEOUT", 0.1):
        result = ha_interface.call_service("select/select_option", entity_id="select.predbat_gecloud_0_mode", option="Eco")
    elapsed = time.monotonic() - start

    if result is not None:
        print("ERROR: Expected None after the timeout, got {}".format(result))
        failed += 1
    if elapsed >= 0.45:
        print("ERROR: call_service waited {:.2f}s, expected it to return at the 0.1s timeout".format(elapsed))
        failed += 1
    if not any("Warn:" in message and "timeout" in message for message in mock_base.log_messages):
        print("ERROR: Expected a timeout Warn, got {}".format(mock_base.log_messages))
        failed += 1
    if not finished.wait(timeout=2):
        print("ERROR: The handler did not carry on to completion after call_service returned")
        failed += 1
    return failed


def run_hainterface_owned_controls_tests(my_predbat):
    """Run the HAInterface Predbat-published control routing tests and return the failure count"""
    print("\n" + "=" * 80)
    print("HAInterface Predbat-published control routing tests")
    print("=" * 80)

    failed = 0
    failed += test_owned_controls_loop_back_for_every_component(my_predbat)
    failed += test_real_ha_entities_still_use_websocket(my_predbat)
    failed += test_owned_control_runs_on_thread_and_returns_result(my_predbat)
    failed += test_owned_control_handler_exception_returns_none(my_predbat)
    failed += test_owned_control_timeout_returns_none(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All HAInterface Predbat-published control routing tests passed!")
    else:
        print("❌ {} HAInterface Predbat-published control routing test(s) failed".format(failed))
    print("=" * 80)
    return failed

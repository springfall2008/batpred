# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Predheat scheduler and its predheat_enable gate (#4670)."""

import asyncio
from datetime import datetime, timedelta

from predheat import PredHeat


class FakeComponents:
    """Minimal stand-in for the component registry so switch_event can be driven in tests."""

    async def switch_event(self, entity_id, service):
        """Swallow the component-level switch routing, which is not under test here."""
        return None


def _make_predheat(my_predbat, captured):
    """Build a PredHeat bound to my_predbat with its log captured and update_pred stubbed."""
    my_predbat.args["predheat"] = {"forecast_days": 2, "run_every": 5, "mode": "pump"}
    my_predbat.log = lambda msg, quiet=True: captured.append(msg)
    predheat = PredHeat(my_predbat)
    predheat.initialize()
    predheat.runs = []
    predheat.update_pred = lambda scheduled: predheat.runs.append(scheduled)
    return predheat


async def _turn_switch(my_predbat, service):
    """Drive the real HA switch-event path for switch.predbat_predheat_enable."""
    await my_predbat.switch_event(service, {"service": service, "service_data": {"entity_id": "switch.predbat_predheat_enable"}}, None)


def _run_due_now(my_predbat, predheat):
    """Make every registered timer due and run one timer tick."""
    for item in my_predbat.run_list:
        item["next_time"] = datetime.now() - timedelta(seconds=1)
    asyncio.get_event_loop().run_until_complete(my_predbat.timer_tick())


def test_predheat(my_predbat):
    """Predheat scheduler registration and the predheat_enable gate."""
    failed = False

    original_log = my_predbat.log
    original_components = my_predbat.components
    original_run_list = my_predbat.run_list
    original_predheat_args = my_predbat.args.get("predheat", None)
    original_enable = my_predbat.config_index["predheat_enable"].get("value", None)

    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        my_predbat.components = FakeComponents()

        print("**** Running Predheat tests ****")

        # ------------------------------------------------------------------
        print("Test: initialize registers both Predheat timers")
        my_predbat.run_list = []
        my_predbat.expose_config("predheat_enable", False)
        captured = []
        predheat = _make_predheat(my_predbat, captured)

        registered = {item["callback"].__name__: item for item in my_predbat.run_list}
        if "run_time_loop" not in registered or "update_time_loop" not in registered:
            print("  ERROR: expected both Predheat timers to be registered, got {}".format(list(registered)))
            failed = True
        elif registered["run_time_loop"]["run_every"] != 300:
            print("  ERROR: expected run_time_loop every 300 seconds, got {}".format(registered["run_time_loop"]["run_every"]))
            failed = True

        # ------------------------------------------------------------------
        print("Test: while disabled Predheat does not run but says so in the log (#4670)")
        _run_due_now(my_predbat, predheat)
        if predheat.runs:
            print("  ERROR: Predheat ran while predheat_enable was off: {}".format(predheat.runs))
            failed = True
        disabled_logs = [msg for msg in captured if "Predheat" in msg and "disabled" in msg.lower()]
        if len(disabled_logs) != 1:
            print("  ERROR: expected exactly one 'disabled' log line explaining why Predheat is idle, captured: {}".format(captured))
            failed = True
        elif "predheat_enable" not in disabled_logs[0]:
            print("  ERROR: expected the disabled log line to name the switch to turn on, got: {}".format(disabled_logs[0]))
            failed = True

        # The pending first run must survive the disabled ticks, or enabling Predheat later
        # would silently wait for the next 5 minute boundary instead of running promptly.
        if not predheat.update_pending:
            print("  ERROR: expected update_pending to survive while disabled")
            failed = True

        print("Test: the disabled message is not repeated on every tick")
        _run_due_now(my_predbat, predheat)
        _run_due_now(my_predbat, predheat)
        if len([msg for msg in captured if "Predheat" in msg and "disabled" in msg.lower()]) != 1:
            print("  ERROR: expected the disabled log line to be logged once, captured: {}".format(captured))
            failed = True

        # ------------------------------------------------------------------
        print("Test: turning switch.predbat_predheat_enable on starts Predheat")
        asyncio.get_event_loop().run_until_complete(_turn_switch(my_predbat, "turn_on"))
        if not my_predbat.get_arg("predheat_enable"):
            print("  ERROR: the switch event did not enable predheat_enable")
            failed = True
        _run_due_now(my_predbat, predheat)
        if not predheat.runs:
            print("  ERROR: Predheat did not run after being enabled")
            failed = True
        enabled_logs = [msg for msg in captured if "Predheat" in msg and "enabled" in msg.lower()]
        if len(enabled_logs) != 1:
            print("  ERROR: expected exactly one 'enabled' log line, captured: {}".format(captured))
            failed = True

        # ------------------------------------------------------------------
        print("Test: re-enabling after a run schedules a prompt update rather than waiting 5 minutes")
        predheat.update_pending = False
        asyncio.get_event_loop().run_until_complete(_turn_switch(my_predbat, "turn_off"))
        _run_due_now(my_predbat, predheat)
        runs_while_off = len(predheat.runs)

        asyncio.get_event_loop().run_until_complete(_turn_switch(my_predbat, "turn_on"))

        # Only the fast 5 second update loop is due here, so a run proves re-enabling asked for
        # an immediate update rather than leaving the user waiting for the next 5 minute boundary
        for item in my_predbat.run_list:
            item["next_time"] = datetime.now() if item["callback"].__name__ == "update_time_loop" else datetime.now() + timedelta(minutes=5)
        asyncio.get_event_loop().run_until_complete(my_predbat.timer_tick())
        if len(predheat.runs) <= runs_while_off:
            print("  ERROR: Predheat did not run promptly after being re-enabled")
            failed = True

    finally:
        my_predbat.log = original_log
        my_predbat.components = original_components
        my_predbat.run_list = original_run_list
        my_predbat.config_index["predheat_enable"]["value"] = original_enable
        if original_predheat_args is None:
            my_predbat.args.pop("predheat", None)
        else:
            my_predbat.args["predheat"] = original_predheat_args

    return failed

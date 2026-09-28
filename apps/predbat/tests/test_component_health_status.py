# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on

import threading
import time

from output import split_status_warning


class FakeComponent:
    """Minimal component exposing its calculation state."""

    def __init__(self, is_calculating=False):
        self.calculating = is_calculating

    def is_calculating(self):
        """Return the configured calculation state."""
        return self.calculating


class FakeComponents:
    """Minimal stand-in for the Components registry, driven by a name -> is_alive map."""

    def __init__(self, alive_map, calculating_map=None):
        self.alive_map = alive_map
        self.calculating_map = calculating_map or {}

    def get_all(self):
        return list(self.alive_map.keys())

    def get_active(self):
        return list(self.alive_map.keys())

    def is_active(self, name):
        return True

    def is_alive(self, name):
        return self.alive_map[name]

    def load_error(self, name):
        """Every fake component loaded; the load-failure path is covered in test_components."""
        return None

    def get_error_count(self, name):
        return 0 if self.alive_map[name] else 1

    def get_component(self, name):
        """Return a fake component with the configured calculation state."""
        return FakeComponent(self.calculating_map.get(name, False))


def test_record_status_state_clamped(my_predbat):
    """
    Verify record_status() clamps the state written to the status sensor at 255 characters, the
    most Home Assistant will accept, while current_status keeps the full text.

    Motivated by #4990: the window warnings list every configured inverter component, so three or
    more push the message past 255 and an unclamped write would fail, leaving the dashboard with a
    stale status on exactly the cycles the warning matters.
    """
    print("*** Running test: record_status clamps the status sensor state at 255 characters")
    failed = 0

    try:
        long_message = "Warn: Inverter 0 unable to read charge window time - " + "x" * 300
        my_predbat.current_status = ""
        my_predbat.record_status(long_message, had_errors=True)

        state = my_predbat.dashboard_values.get(my_predbat.prefix + ".status", {}).get("state", None)
        if state is None:
            print("ERROR: status sensor was not published at all")
            failed = 1
        elif len(state) > 255:
            print("ERROR: status state is {} characters, Home Assistant rejects anything over 255".format(len(state)))
            failed = 1
        elif not state.startswith("Warn: Inverter 0 unable to read charge window time"):
            print("ERROR: clamped state lost the start of the message: {}".format(state[:80]))
            failed = 1
        elif len(my_predbat.current_status) <= 255:
            print("ERROR: current_status should keep the full unclamped message for the log, got {} characters".format(len(my_predbat.current_status)))
            failed = 1
        else:
            print("OK: state clamped to 255 characters ({}), full text kept in current_status ({})".format(len(state), len(my_predbat.current_status)))
    finally:
        my_predbat.current_status = ""
        my_predbat.had_errors = False
        my_predbat.status_warning = None

    return failed


def test_record_status_under_warning(my_predbat):
    """
    Verify a run that raised a warning ends with its executed state in front of the warning on the
    status sensor ("Exporting, Warn: ..."), keeping the warning's debug, without counting the
    warning again - and that a run whose had_errors came from somewhere that recorded no warning
    (a component thread) leaves the sensor alone rather than failing.

    A warning recorded during a run takes over the status sensor, and the run's own state used to be
    only logged, so a warning that recurred every cycle erased the executed state from the sensor's
    history and the History view rebuilt a day of force exports as charge holds.
    """
    print("*** Running test: a run's state is recorded in front of the warning it raised")
    failed = 0
    status_entity = my_predbat.prefix + ".status"
    saved_item = my_predbat.ha_interface.dummy_items.get(status_entity)
    saved_value = my_predbat.dashboard_values.get(status_entity)
    saved = (my_predbat.current_status, my_predbat.had_errors, my_predbat.status_warning, my_predbat.status_warning_debug, my_predbat.components)
    warning = "Warn: Return bad float value unavailable from car_charging_soc"

    try:
        my_predbat.components = None
        my_predbat.had_errors = False
        my_predbat.status_warning = None
        my_predbat.record_status(warning, debug="https://example.invalid/failing", had_errors=True)
        published = my_predbat.dashboard_values[status_entity]
        error_count_before = published["attributes"]["error_count"]

        my_predbat.record_final_run_status("Exporting", "target 20%")
        published = my_predbat.dashboard_values[status_entity]
        attributes = published["attributes"]
        if published["state"] != "Exporting, " + warning:
            print("ERROR: expected the state in front of the warning, got {!r}".format(published["state"]))
            failed = 1
        elif attributes["debug"] != "https://example.invalid/failing":
            print("ERROR: the warning's own debug should be kept, got {!r}".format(attributes["debug"]))
            failed = 1
        elif attributes["error_count"] != error_count_before:
            print("ERROR: the same warning should not be counted again: {} -> {}".format(error_count_before, attributes["error_count"]))
            failed = 1
        elif not attributes["error"]:
            print("ERROR: the sensor should still show the run as in error")
            failed = 1
        elif my_predbat.current_status != "Exporting, " + warning:
            print("ERROR: current_status should follow the sensor, got {!r}".format(my_predbat.current_status))
            failed = 1
        else:
            print("OK: state recorded in front of the warning, debug and error_count kept")

        # The next run raises the same warning mid-run: the state stays in front of it, so the sensor
        # doesn't flip to the bare warning and back every cycle
        my_predbat.had_errors = False
        my_predbat.status_warning = None
        my_predbat.record_status(warning, had_errors=True)
        if my_predbat.dashboard_values[status_entity]["state"] != "Exporting, " + warning:
            print("ERROR: a repeated warning should keep the state in front of it, got {!r}".format(my_predbat.dashboard_values[status_entity]["state"]))
            failed = 1

        # A different warning or error - e.g. a run bailing out without executing - is shown bare
        my_predbat.record_status("Error: Failed to fetch inverter data, not able to execute the plan", had_errors=True)
        if my_predbat.dashboard_values[status_entity]["state"] != "Error: Failed to fetch inverter data, not able to execute the plan":
            print("ERROR: a different error should be shown bare, got {!r}".format(my_predbat.dashboard_values[status_entity]["state"]))
            failed = 1

        # had_errors set by a component thread with no warning recorded, right after a restart
        my_predbat.dashboard_values[status_entity] = {"state": "Demand", "attributes": {}}
        my_predbat.current_status = None
        my_predbat.had_errors = True
        my_predbat.status_warning = None
        my_predbat.record_final_run_status("Exporting", "")
        if my_predbat.dashboard_values[status_entity]["state"] != "Demand":
            print("ERROR: with no warning recorded the sensor should be left alone, got {!r}".format(my_predbat.dashboard_values[status_entity]["state"]))
            failed = 1
        else:
            print("OK: no warning recorded, sensor left alone")
    finally:
        my_predbat.current_status, my_predbat.had_errors, my_predbat.status_warning, my_predbat.status_warning_debug, my_predbat.components = saved
        if saved_item is None:
            my_predbat.ha_interface.dummy_items.pop(status_entity, None)
        else:
            my_predbat.ha_interface.dummy_items[status_entity] = saved_item
        if saved_value is None:
            my_predbat.dashboard_values.pop(status_entity, None)
        else:
            my_predbat.dashboard_values[status_entity] = saved_value

    return failed


def test_record_status_concurrent(my_predbat):
    """
    Verify record_status() calls from several threads at once don't lose an error_count increment,
    and that the status notification is sent with the lock released.

    Component threads (GE Cloud, Solis, ...) record status alongside the main loop, and error_count is
    a read-modify-write of the sensor's own attribute. The read is slowed here so that, without the
    status lock, the threads reliably read the same count and overwrite each other's increment.
    """
    print("*** Running test: concurrent record_status calls keep every error_count increment")
    failed = 0
    status_entity = my_predbat.prefix + ".status"
    saved_item = my_predbat.ha_interface.dummy_items.get(status_entity)
    saved_value = my_predbat.dashboard_values.get(status_entity)
    saved = (my_predbat.current_status, my_predbat.had_errors, my_predbat.status_warning, my_predbat.status_warning_debug)
    original_get_state_wrapper = my_predbat.get_state_wrapper
    original_call_notify = my_predbat.call_notify
    saved_notify_flag = my_predbat.set_status_notify
    saved_previous_status = my_predbat.previous_status
    threads_count, calls_each = 4, 10

    def _slow_get_state_wrapper(*args, **kwargs):
        value = original_get_state_wrapper(*args, **kwargs)
        time.sleep(0.001)
        return value

    def _record_warnings():
        for _ in range(calls_each):
            my_predbat.record_status("Warn: concurrent test", had_errors=True)

    try:
        my_predbat.dashboard_values.pop(status_entity, None)
        my_predbat.ha_interface.dummy_items.pop(status_entity, None)
        my_predbat.get_state_wrapper = _slow_get_state_wrapper
        threads = [threading.Thread(target=_record_warnings) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        error_count = my_predbat.dashboard_values[status_entity]["attributes"]["error_count"]
        if error_count != threads_count * calls_each:
            print("ERROR: expected error_count {} after {} concurrent warnings, got {}".format(threads_count * calls_each, threads_count * calls_each, error_count))
            failed = 1
        else:
            print("OK: all {} concurrent warnings counted".format(error_count))
        my_predbat.get_state_wrapper = original_get_state_wrapper

        # The notification is sent with the lock released - over the websocket it can wait minutes
        # on another thread, and every other status would stall behind it
        notified = []

        def _fake_notify(message):
            result = {}

            def _try_lock():
                result["free"] = my_predbat.status_lock.acquire(blocking=False)
                if result["free"]:
                    my_predbat.status_lock.release()

            checker = threading.Thread(target=_try_lock)
            checker.start()
            checker.join()
            notified.append((message, result["free"]))

        my_predbat.call_notify = _fake_notify
        my_predbat.set_status_notify = True
        my_predbat.previous_status = None
        my_predbat.had_errors = False
        my_predbat.record_status("Exporting", notify=True)
        if len(notified) != 1 or "Exporting" not in notified[0][0]:
            print("ERROR: expected one 'Exporting' notification, got {}".format(notified))
            failed = 1
        elif not notified[0][1]:
            print("ERROR: the notification was sent while the status lock was held")
            failed = 1
        else:
            print("OK: status notification sent with the lock released")
    finally:
        my_predbat.get_state_wrapper = original_get_state_wrapper
        my_predbat.call_notify = original_call_notify
        my_predbat.set_status_notify = saved_notify_flag
        my_predbat.previous_status = saved_previous_status
        my_predbat.current_status, my_predbat.had_errors, my_predbat.status_warning, my_predbat.status_warning_debug = saved
        if saved_item is None:
            my_predbat.ha_interface.dummy_items.pop(status_entity, None)
        else:
            my_predbat.ha_interface.dummy_items[status_entity] = saved_item
        if saved_value is None:
            my_predbat.dashboard_values.pop(status_entity, None)
        else:
            my_predbat.dashboard_values[status_entity] = saved_value

    return failed


def test_component_health_status(my_predbat):
    """
    Verify record_final_run_status() marks the run as an error, naming the failed component(s),
    when a component is active but not alive - even though the plan itself computed successfully.
    """
    print("*** Running test: Component errors fail the recorded run status")
    failed = 0

    recorded_statuses = []
    original_record_status = my_predbat.record_status
    my_predbat.record_status = lambda message, debug="", had_errors=False, notify=False, extra="": recorded_statuses.append((message, had_errors))

    try:
        # --- All components healthy: final status should be the plan's own success status ---
        my_predbat.had_errors = False
        my_predbat.components = FakeComponents({"octopus": True, "gecloud": True})
        recorded_statuses.clear()
        my_predbat.record_final_run_status("Idle", "")

        if len(recorded_statuses) != 1 or recorded_statuses[0] != ("Idle", False):
            print("ERROR: Expected a single success status record, got {}".format(recorded_statuses))
            failed = 1
        else:
            print("OK: All components healthy -> success status recorded")

        # --- Octopus component in error (active but not alive): run must be recorded as an error ---
        my_predbat.had_errors = False
        my_predbat.components = FakeComponents({"octopus": False, "gecloud": True})
        recorded_statuses.clear()
        my_predbat.record_final_run_status("Idle", "")

        if len(recorded_statuses) != 1:
            print("ERROR: Expected a single status record for a failed component, got {}".format(recorded_statuses))
            failed = 1
        else:
            message, had_errors = recorded_statuses[0]
            if not had_errors:
                print("ERROR: Component error did not mark the run as an error")
                failed = 1
            elif "Octopus Energy Direct" not in message:
                print("ERROR: Failed component name not present in recorded status message: {}".format(message))
                failed = 1
            else:
                print("OK: Component error correctly recorded as an error, naming the component")

        # --- Multiple components in error: all should be listed ---
        my_predbat.had_errors = False
        my_predbat.components = FakeComponents({"octopus": False, "gecloud": False})
        recorded_statuses.clear()
        my_predbat.record_final_run_status("Idle", "")

        if len(recorded_statuses) != 1:
            print("ERROR: Expected a single status record for multiple failed components, got {}".format(recorded_statuses))
            failed = 1
        else:
            message, had_errors = recorded_statuses[0]
            if not had_errors or "Octopus Energy Direct" not in message or "GivEnergy Cloud" not in message:
                print("ERROR: Not all failed components listed in status message: {}".format(message))
                failed = 1
            else:
                print("OK: All failed components listed in the recorded error status")
            # The History view unwraps the run's own status from this summary, so the two must agree
            if split_status_warning(message) != ("Idle", message):
                print("ERROR: History should read the run status 'Idle' back out of {!r}, got {!r}".format(message, split_status_warning(message)))
                failed = 1
            else:
                print("OK: History reads the run status back out of the component error summary")

        # --- LoadML can appear unhealthy during a calculation without failing the run status ---
        my_predbat.had_errors = False
        my_predbat.components = FakeComponents({"load_ml": False}, calculating_map={"load_ml": True})
        recorded_statuses.clear()
        my_predbat.record_final_run_status("Export", "")

        if len(recorded_statuses) != 1 or recorded_statuses[0] != ("Export", False):
            print("ERROR: Calculating LoadML component should not fail the run status: {}".format(recorded_statuses))
            failed = 1
        else:
            print("OK: Calculating LoadML component does not fail the run status")

        # --- A LoadML failure outside calculation must still fail the run status ---
        my_predbat.had_errors = False
        my_predbat.components = FakeComponents({"load_ml": False}, calculating_map={"load_ml": False})
        recorded_statuses.clear()
        my_predbat.record_final_run_status("Demand", "")

        if len(recorded_statuses) != 1 or not recorded_statuses[0][1] or "ML Load Forecaster" not in recorded_statuses[0][0]:
            print("ERROR: Non-calculating failed LoadML component did not fail the run status: {}".format(recorded_statuses))
            failed = 1
        else:
            print("OK: LoadML failure outside calculation still fails the run status")

        # --- Pre-existing error takes precedence, and is not overwritten by the component check ---
        my_predbat.had_errors = True
        my_predbat.status_warning = None
        my_predbat.components = FakeComponents({"octopus": False})
        recorded_statuses.clear()
        my_predbat.record_final_run_status("Idle", "")

        if recorded_statuses:
            print("ERROR: record_status should not be called again when had_errors was already set: {}".format(recorded_statuses))
            failed = 1
        else:
            print("OK: Pre-existing error state left untouched by the component health check")
    finally:
        my_predbat.had_errors = False
        my_predbat.status_warning = None
        my_predbat.components = None
        my_predbat.record_status = original_record_status

    return failed

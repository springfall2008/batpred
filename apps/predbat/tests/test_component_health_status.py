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


class FakeComponents:
    """Minimal stand-in for the Components registry, driven by a name -> is_alive map."""

    def __init__(self, alive_map, exempt_map=None):
        self.alive_map = alive_map
        self.exempt_map = exempt_map or {}

    def get_all(self):
        return list(self.alive_map.keys())

    def get_active(self):
        return list(self.alive_map.keys())

    def is_active(self, name):
        return True

    def is_alive(self, name):
        return self.alive_map[name]

    def get_error_count(self, name):
        return 0 if self.alive_map[name] else 1

    def health_exempt(self, name):
        return self.exempt_map.get(name, False)


def test_component_health_status(my_predbat):
    """
    Verify record_final_run_status() marks the run as an error, naming the failed component(s),
    when a component is active but not alive - even though the plan itself computed successfully.
    """
    print("*** Running test: Component errors fail the recorded run status")
    failed = 0

    recorded_statuses = []
    my_predbat.record_status = lambda message, debug="", had_errors=False, notify=False, extra="": recorded_statuses.append((message, had_errors))

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

    # --- Health-exempt component in error (user disabled it): run must NOT fail ---
    # e.g. Axle automation turned off but a rotated or expired key is rejected (HTTP 401) every fetch.
    my_predbat.had_errors = False
    my_predbat.components = FakeComponents({"axle": False, "gecloud": True}, exempt_map={"axle": True})
    recorded_statuses.clear()
    my_predbat.record_final_run_status("Idle", "")

    if len(recorded_statuses) != 1 or recorded_statuses[0] != ("Idle", False):
        print("ERROR: Health-exempt unhealthy component should not fail the run, got {}".format(recorded_statuses))
        failed = 1
    else:
        print("OK: Health-exempt unhealthy component recorded as success (not a run error)")

    # --- A non-exempt component in error alongside a health-exempt one: still fails, naming only the non-exempt ---
    my_predbat.had_errors = False
    my_predbat.components = FakeComponents({"axle": False, "octopus": False}, exempt_map={"axle": True})
    recorded_statuses.clear()
    my_predbat.record_final_run_status("Idle", "")

    if len(recorded_statuses) != 1:
        print("ERROR: Expected a single status record, got {}".format(recorded_statuses))
        failed = 1
    else:
        message, had_errors = recorded_statuses[0]
        if not had_errors or "Octopus Energy Direct" not in message or "Axle" in message:
            print("ERROR: Non-exempt failure should be named and exempt component excluded: {}".format(message))
            failed = 1
        else:
            print("OK: Non-exempt component fails the run; exempt component excluded from the error")

    # --- Pre-existing error takes precedence, and is not overwritten by the component check ---
    my_predbat.had_errors = True
    my_predbat.components = FakeComponents({"octopus": False})
    recorded_statuses.clear()
    my_predbat.record_final_run_status("Idle", "")

    if recorded_statuses:
        print("ERROR: record_status should not be called again when had_errors was already set: {}".format(recorded_statuses))
        failed = 1
    else:
        print("OK: Pre-existing error state left untouched by the component health check")

    my_predbat.had_errors = False
    my_predbat.components = None

    return failed

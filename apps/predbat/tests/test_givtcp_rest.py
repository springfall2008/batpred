# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for givtcp_rest.py's REST client.

Covers the write methods' retry-until-verified loop and the transport underneath it. Every write
follows the same shape - POST, re-read the whole status, compare, retry up to
INVERTER_MAX_RETRY_REST times - so they are driven from one table rather than a test each: what
differs between them is only the endpoint, the payload, and which field proves the write landed.

The client is given a scripted transport (rest_postCommand/rest_getData) rather than a live
GivTCP, and an InverterRestState to hold the snapshot - the same stand-in GivTCPComponent uses.
"""

import copy

from givtcp_rest import GivTCPRest, InverterRestState
from const import INVERTER_MAX_RETRY_REST


class _RestBase:
    """Minimal Predbat stand-in: collects log lines and status records."""

    def __init__(self):
        self.logs = []
        self.status = []

    def log(self, message):
        """Collect a log line."""
        self.logs.append(message)

    def record_status(self, message, had_errors=False, **kwargs):
        """Collect a status record and whether it was flagged as an error."""
        self.status.append((message, had_errors))


class _Response:
    """Stand-in for the requests.Response a POST returns; only its truthiness is used."""

    status_code = 200


def _blob(control=None, timeslots=None):
    """A GivTCP status snapshot, with optional Control/Timeslots overrides merged in."""
    data = {
        "Control": {
            "Battery_Power_Reserve": 4,
            "Battery_Charge_Rate": 1000,
            "Battery_Discharge_Rate": 1000,
            "Target_SOC": 50,
            "Mode": "Eco",
            "Battery_pause_mode": "Disabled",
            "Enable_Charge_Schedule": "disable",
            "Enable_Discharge_Schedule": "disable",
            "Enable_Charge_Target": "disable",
            "Discharge_Target_SOC_1": 100,
        },
        "Timeslots": {
            "Charge_start_time_slot_1": "00:00:00",
            "Charge_end_time_slot_1": "00:00:00",
            "Discharge_start_time_slot_1": "00:00:00",
            "Discharge_end_time_slot_1": "00:00:00",
            "Battery_pause_start_time_slot": "00:00:00",
            "Battery_pause_end_time_slot": "00:00:00",
        },
        "Invertor_Details": {"Invertor_Max_Bat_Rate": 6000},
        "raw": {"invertor": {"discharge_target_soc_1": 100, "firmware_version": "D0.449-A0.450", "serial_number": "CE1234G567"}},
        "Stats": {"GivTCP_Version": "3.0.4"},
    }
    data["Control"].update(control or {})
    data["Timeslots"].update(timeslots or {})
    return data


class _Transport:
    """Scripted REST transport: records POSTs and serves a queue of GET responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.posts = []
        self.gets = []

    def post(self, url, json=None):
        """Record the POST and report success."""
        self.posts.append((url, json))
        return _Response()

    def get(self, url):
        """Serve the next scripted response, repeating the last one once exhausted."""
        self.gets.append(url)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _client(responses, rest_data=None):
    """A GivTCPRest wired to a scripted transport, with sleeps counted rather than taken."""
    base = _RestBase()
    state = InverterRestState(id=0, rest_api="http://givtcp:6345")
    state.rest_data = copy.deepcopy(rest_data if rest_data is not None else _blob())
    state.slept = []
    state.sleep = lambda seconds: state.slept.append(seconds)
    transport = _Transport(responses)
    client = GivTCPRest(base, state, rest_postCommand=transport.post, rest_getData=transport.get)
    return base, state, transport, client


# name -> (call, endpoint, expected payload, the Control/Timeslots state that proves it landed)
WRITE_CASES = {
    "set_charge_target": (lambda c: c.set_charge_target(80), "/setChargeTarget", {"chargeToPercent": 80}, {"control": {"Target_SOC": 80}}),
    "set_charge_rate": (lambda c: c.set_charge_rate(2000), "/setChargeRate", {"chargeRate": 2000}, {"control": {"Battery_Charge_Rate": 2000}}),
    "set_discharge_rate": (lambda c: c.set_discharge_rate(2000), "/setDischargeRate", {"dischargeRate": 2000}, {"control": {"Battery_Discharge_Rate": 2000}}),
    "set_battery_mode": (lambda c: c.set_battery_mode("Timed Export"), "/setBatteryMode", {"mode": "Timed Export"}, {"control": {"Mode": "Timed Export"}}),
    "set_battery_pause_mode": (lambda c: c.set_battery_pause_mode("PauseBoth"), "/setBatteryPauseMode", {"state": "PauseBoth"}, {"control": {"Battery_pause_mode": "PauseBoth"}}),
    "set_reserve": (lambda c: c.set_reserve(20), "/setBatteryReserve", {"reservePercent": 20}, {"control": {"Battery_Power_Reserve": 20}}),
    "enable_charge_target": (lambda c: c.enable_charge_target(True), "/enableChargeTarget", {"state": "enable"}, {"control": {"Enable_Charge_Target": "enable"}}),
    "enable_charge_schedule": (lambda c: c.enable_charge_schedule(True), "/enableChargeSchedule", {"state": "enable"}, {"control": {"Enable_Charge_Schedule": "enable"}}),
    "enable_discharge_schedule": (lambda c: c.enable_discharge_schedule(True), "/enableDischargeSchedule", {"state": "enable"}, {"control": {"Enable_Discharge_Schedule": "enable"}}),
    "set_pause_slot": (lambda c: c.set_pause_slot("01:00:00", "02:00:00"), "/setPauseSlot", {"start": "01:00", "finish": "02:00"}, {"timeslots": {"Battery_pause_start_time_slot": "01:00:00", "Battery_pause_end_time_slot": "02:00:00"}}),
    "set_charge_slot1": (lambda c: c.set_charge_slot1("01:00:00", "02:00:00"), "/setChargeSlot1", {"start": "01:00", "finish": "02:00"}, {"timeslots": {"Charge_start_time_slot_1": "01:00:00", "Charge_end_time_slot_1": "02:00:00"}}),
    "set_discharge_slot1": (lambda c: c.set_discharge_slot1("01:00:00", "02:00:00"), "/setDischargeSlot1", {"start": "01:00", "finish": "02:00"}, {"timeslots": {"Discharge_start_time_slot_1": "01:00:00", "Discharge_end_time_slot_1": "02:00:00"}}),
}


def test_writes_succeed_when_the_value_reads_back(my_predbat=None):
    """
    Each write posts to its own endpoint with its own payload and confirms against the read-back.

    The endpoint and payload are the part of each method that cannot be checked by any other test:
    a typo in either reaches GivTCP as a silently ignored call, and the read-back then fails for a
    reason that looks like hardware.
    """
    failed = False
    print("**** Testing every REST write posts correctly and verifies its read-back ****")

    for name, (call, endpoint, payload, applied) in WRITE_CASES.items():
        base, state, transport, client = _client([_blob(**applied)])
        result = call(client)

        if result is not True:
            print("ERROR: {} returned {} for a write that landed".format(name, result))
            failed = True
        if transport.posts != [("http://givtcp:6345" + endpoint, payload)]:
            print("ERROR: {} posted {}, expected one call to {} with {}".format(name, transport.posts, endpoint, payload))
            failed = True
        if state.count_register_writes != 1:
            print("ERROR: {} recorded {} register writes, expected 1".format(name, state.count_register_writes))
            failed = True
        if state.slept:
            print("ERROR: {} slept {} on a write that landed first time".format(name, state.slept))
            failed = True
        if base.status:
            print("ERROR: {} recorded a status on success: {}".format(name, base.status))
            failed = True

    if not failed:
        print("PASS: all {} REST writes post and verify correctly".format(len(WRITE_CASES)))
    return 1 if failed else 0


def test_writes_retry_until_the_value_appears(my_predbat=None):
    """
    A write the inverter has not applied yet is retried, not reported as failed.

    GivTCP acknowledges a POST before the register has necessarily changed, which is the whole
    reason these methods re-read and loop.
    """
    failed = False
    print("**** Testing a REST write retries until the value appears ****")

    for name, (call, endpoint, payload, applied) in WRITE_CASES.items():
        # First read-back still shows the old value, the second shows the write applied
        base, state, transport, client = _client([_blob(), _blob(**applied)])
        result = call(client)

        if result is not True:
            print("ERROR: {} returned {} for a write that landed on the second look".format(name, result))
            failed = True
        if len(transport.posts) != 2:
            print("ERROR: {} posted {} times, expected 2 (one per retry)".format(name, len(transport.posts)))
            failed = True
        if state.slept != [2]:
            print("ERROR: {} slept {}, expected a single 2s wait between attempts".format(name, state.slept))
            failed = True
        if state.count_register_writes != 1:
            print("ERROR: {} recorded {} register writes, expected 1".format(name, state.count_register_writes))
            failed = True

    if not failed:
        print("PASS: every REST write retries until the read-back agrees")
    return 1 if failed else 0


def test_writes_give_up_and_report_after_the_retry_ladder(my_predbat=None):
    """
    A write that never lands returns False and records an error status, rather than reporting success.

    The caller decides whether to re-issue on the next cycle from this return value, so a write
    silently reported as successful would leave the inverter on the wrong setting until something
    else happened to change it.
    """
    failed = False
    print("**** Testing a REST write that never lands is reported as failed ****")

    for name, (call, endpoint, payload, applied) in WRITE_CASES.items():
        # The read-back never shows the new value
        base, state, transport, client = _client([_blob()])
        result = call(client)

        if result is not False:
            print("ERROR: {} returned {} for a write that never landed".format(name, result))
            failed = True
        if len(transport.posts) != INVERTER_MAX_RETRY_REST:
            print("ERROR: {} posted {} times, expected {}".format(name, len(transport.posts), INVERTER_MAX_RETRY_REST))
            failed = True
        if state.count_register_writes != 0:
            print("ERROR: {} counted a register write for a write that never landed".format(name))
            failed = True
        if not any(had_errors for _, had_errors in base.status):
            print("ERROR: {} recorded no error status for a failed write, got {}".format(name, base.status))
            failed = True

    if not failed:
        print("PASS: every REST write gives up and reports after the retry ladder")
    return 1 if failed else 0


def test_read_data_retries_a_bad_response_then_gives_up(my_predbat=None):
    """
    read_data retries a response with no Control block, then reports the read as skipped.

    A GivTCP that is still starting up answers with something that is not a status, and treating
    that as data would publish an inverter's worth of missing fields as though they were real.
    """
    failed = False
    print("**** Testing read_data's retry ladder ****")

    # Succeeds first time
    base, state, transport, client = _client([_blob()])
    if client.read_data() is None:
        print("ERROR: read_data returned None for a valid response")
        failed = True
    if state.slept:
        print("ERROR: read_data slept {} on a first-time success".format(state.slept))
        failed = True

    # A junk response is retried, and the delays lengthen after the first
    base, state, transport, client = _client([{"nonsense": True}, _blob()])
    if client.read_data() is None:
        print("ERROR: read_data returned None when the retry succeeded")
        failed = True
    if state.slept != [20]:
        print("ERROR: read_data slept {}, expected a single 20s first delay".format(state.slept))
        failed = True

    # Never valid: gives up after the ladder and records an error
    base, state, transport, client = _client([{"nonsense": True}])
    if client.read_data() is not None:
        print("ERROR: read_data returned data for a response that never had a Control block")
        failed = True
    if state.slept != [20] + [40] * (INVERTER_MAX_RETRY_REST - 1):
        print("ERROR: read_data slept {}, expected 20s then 40s per further retry".format(state.slept))
        failed = True
    if not any(had_errors for _, had_errors in base.status):
        print("ERROR: read_data recorded no error status after exhausting its retries")
        failed = True

    # retry=False takes one attempt only - run_all uses this, inside the write loop
    base, state, transport, client = _client([{"nonsense": True}])
    if client.read_data(api="runAll", retry=False) is not None:
        print("ERROR: read_data(retry=False) returned data for a junk response")
        failed = True
    if len(transport.gets) != 1 or state.slept:
        print("ERROR: read_data(retry=False) made {} gets and slept {}, expected one attempt and no sleep".format(len(transport.gets), state.slept))
        failed = True

    if not failed:
        print("PASS: read_data retries, backs off and gives up correctly")
    return 1 if failed else 0


def test_run_all_keeps_the_previous_snapshot_when_the_read_fails(my_predbat=None):
    """
    run_all falls back to the snapshot it was given rather than returning None.

    Every write loop assigns its result straight back to inverter.rest_data, so a None here would
    replace a good snapshot with nothing and make the very next comparison raise instead of retry.
    """
    failed = False
    print("**** Testing run_all's fallback ****")

    previous = _blob(control={"Target_SOC": 42})
    base, state, transport, client = _client([{"nonsense": True}])
    result = client.run_all(previous)
    if result is not previous:
        print("ERROR: run_all returned {} instead of the previous snapshot".format(result))
        failed = True

    base, state, transport, client = _client([_blob(control={"Target_SOC": 77})])
    result = client.run_all(previous)
    if result["Control"]["Target_SOC"] != 77:
        print("ERROR: run_all did not return the fresh snapshot, got Target_SOC {}".format(result["Control"]["Target_SOC"]))
        failed = True

    if not failed:
        print("PASS: run_all keeps the previous snapshot when the read fails")
    return 1 if failed else 0


def test_transport_failures_are_contained(my_predbat=None):
    """
    A POST that raises returns None instead of propagating, and a failed GET reads as no data.

    GivTCP is a local network service that can disappear mid-cycle; an exception escaping here
    would take down the whole update rather than one write.
    """
    failed = False
    print("**** Testing the REST transport contains its failures ****")

    base = _RestBase()
    state = InverterRestState(id=0, rest_api="http://givtcp:6345")
    state.rest_data = _blob()
    client = GivTCPRest(base, state)

    class _BoomSession:
        """Stands in for requests, refusing every call the way an unreachable host does."""

        @staticmethod
        def post(url, json=None, timeout=None):
            """Raise as requests does when the host is unreachable."""
            raise OSError("connection refused")

        @staticmethod
        def get(url, timeout=None):
            """Raise as requests does when the host is unreachable."""
            raise OSError("connection refused")

    import givtcp_rest

    original = givtcp_rest.requests
    givtcp_rest.requests = _BoomSession
    try:
        if client.post_command("http://givtcp:6345/setChargeRate", json={"chargeRate": 1}) is not None:
            print("ERROR: post_command returned a response when the POST raised")
            failed = True
        if client.get_data("http://givtcp:6345/readData") is not None:
            print("ERROR: get_data returned data when the GET raised")
            failed = True
    finally:
        givtcp_rest.requests = original

    if not any("failed" in message or "Exception" in message for message in base.logs):
        print("ERROR: transport failures were not logged, got {}".format(base.logs))
        failed = True

    if not failed:
        print("PASS: transport failures are contained and logged")
    return 1 if failed else 0


def test_window_times_need_a_snapshot(my_predbat=None):
    """The window readers return None before any status has been read, and parsed times after."""
    failed = False
    print("**** Testing the charge/export window readers ****")

    base, state, transport, client = _client([_blob()], rest_data=None)
    state.rest_data = None
    if client.charge_window_times() is not None or client.discharge_window_times() is not None:
        print("ERROR: window readers returned a value with no snapshot")
        failed = True

    state.rest_data = _blob(timeslots={"Charge_start_time_slot_1": "01:30:00", "Charge_end_time_slot_1": "04:00:00", "Discharge_start_time_slot_1": "16:00:00", "Discharge_end_time_slot_1": "19:00:00"})
    start, end = client.charge_window_times()
    if start.hour != 1 or start.minute != 30 or end.hour != 4:
        print("ERROR: charge window parsed as {} - {}".format(start, end))
        failed = True
    start, end = client.discharge_window_times()
    if start.hour != 16 or end.hour != 19:
        print("ERROR: export window parsed as {} - {}".format(start, end))
        failed = True

    if not failed:
        print("PASS: window readers need a snapshot and parse it correctly")
    return 1 if failed else 0


def run_givtcp_rest_tests(my_predbat):
    """Run every GivTCPRest test, returning a non-zero count on failure."""
    failed = 0
    failed += test_writes_succeed_when_the_value_reads_back(my_predbat)
    failed += test_writes_retry_until_the_value_appears(my_predbat)
    failed += test_writes_give_up_and_report_after_the_retry_ladder(my_predbat)
    failed += test_read_data_retries_a_bad_response_then_gives_up(my_predbat)
    failed += test_run_all_keeps_the_previous_snapshot_when_the_read_fails(my_predbat)
    failed += test_transport_failures_are_contained(my_predbat)
    failed += test_window_times_need_a_snapshot(my_predbat)
    return failed

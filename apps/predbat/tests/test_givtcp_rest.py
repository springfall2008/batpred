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


def _blob(control=None, timeslots=None, raw=None):
    """A GivTCP status snapshot, with optional Control/Timeslots/raw.invertor overrides merged in."""
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
    data["raw"]["invertor"].update(raw or {})
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


# name -> (call, endpoint, expected payload, the state that proves it landed, pre-read settle delay)
#
# set_discharge_target is the only one that waits before looking: everything else reads back
# immediately. See its own tests below for the Control-over-raw preference that wait exists for.
WRITE_CASES = {
    "set_charge_target": (lambda c: c.set_charge_target(80), "/setChargeTarget", {"chargeToPercent": 80}, {"control": {"Target_SOC": 80}}, 0),
    "set_charge_rate": (lambda c: c.set_charge_rate(2000), "/setChargeRate", {"chargeRate": 2000}, {"control": {"Battery_Charge_Rate": 2000}}, 0),
    "set_discharge_rate": (lambda c: c.set_discharge_rate(2000), "/setDischargeRate", {"dischargeRate": 2000}, {"control": {"Battery_Discharge_Rate": 2000}}, 0),
    "set_battery_mode": (lambda c: c.set_battery_mode("Timed Export"), "/setBatteryMode", {"mode": "Timed Export"}, {"control": {"Mode": "Timed Export"}}, 0),
    "set_battery_pause_mode": (lambda c: c.set_battery_pause_mode("PauseBoth"), "/setBatteryPauseMode", {"state": "PauseBoth"}, {"control": {"Battery_pause_mode": "PauseBoth"}}, 0),
    "set_reserve": (lambda c: c.set_reserve(20), "/setBatteryReserve", {"reservePercent": 20}, {"control": {"Battery_Power_Reserve": 20}}, 0),
    "enable_charge_target": (lambda c: c.enable_charge_target(True), "/enableChargeTarget", {"state": "enable"}, {"control": {"Enable_Charge_Target": "enable"}}, 0),
    "enable_charge_schedule": (lambda c: c.enable_charge_schedule(True), "/enableChargeSchedule", {"state": "enable"}, {"control": {"Enable_Charge_Schedule": "enable"}}, 0),
    "enable_discharge_schedule": (lambda c: c.enable_discharge_schedule(True), "/enableDischargeSchedule", {"state": "enable"}, {"control": {"Enable_Discharge_Schedule": "enable"}}, 0),
    "set_pause_slot": (lambda c: c.set_pause_slot("01:00:00", "02:00:00"), "/setPauseSlot", {"start": "01:00", "finish": "02:00"}, {"timeslots": {"Battery_pause_start_time_slot": "01:00:00", "Battery_pause_end_time_slot": "02:00:00"}}, 0),
    "set_charge_slot1": (lambda c: c.set_charge_slot1("01:00:00", "02:00:00"), "/setChargeSlot1", {"start": "01:00", "finish": "02:00"}, {"timeslots": {"Charge_start_time_slot_1": "01:00:00", "Charge_end_time_slot_1": "02:00:00"}}, 0),
    "set_discharge_slot1": (lambda c: c.set_discharge_slot1("01:00:00", "02:00:00"), "/setDischargeSlot1", {"start": "01:00", "finish": "02:00"}, {"timeslots": {"Discharge_start_time_slot_1": "01:00:00", "Discharge_end_time_slot_1": "02:00:00"}}, 0),
    "set_discharge_target": (lambda c: c.set_discharge_target(20), "/setDischargeTarget", {"dischargeToPercent": 20, "slot": 1}, {"control": {"Discharge_Target_SOC_1": 20}}, 1),
}


def _expected_sleeps(settle, attempts):
    """The sleeps a write should have taken over `attempts` tries, the last of which may succeed.

    A settling write waits before each read-back and again between attempts; everything else only
    waits between attempts.
    """
    if settle:
        return ([settle, 2] * (attempts - 1)) + [settle]
    return [2] * (attempts - 1)


def test_writes_succeed_when_the_value_reads_back(my_predbat=None):
    """
    Each write posts to its own endpoint with its own payload and confirms against the read-back.

    The endpoint and payload are the part of each method that cannot be checked by any other test:
    a typo in either reaches GivTCP as a silently ignored call, and the read-back then fails for a
    reason that looks like hardware.
    """
    failed = False
    print("**** Testing every REST write posts correctly and verifies its read-back ****")

    for name, (call, endpoint, payload, applied, settle) in WRITE_CASES.items():
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
        if state.slept != _expected_sleeps(settle, 1):
            print("ERROR: {} slept {} on a write that landed first time, expected {}".format(name, state.slept, _expected_sleeps(settle, 1)))
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

    for name, (call, endpoint, payload, applied, settle) in WRITE_CASES.items():
        # First read-back still shows the old value, the second shows the write applied
        base, state, transport, client = _client([_blob(), _blob(**applied)])
        result = call(client)

        if result is not True:
            print("ERROR: {} returned {} for a write that landed on the second look".format(name, result))
            failed = True
        if len(transport.posts) != 2:
            print("ERROR: {} posted {} times, expected 2 (one per retry)".format(name, len(transport.posts)))
            failed = True
        if state.slept != _expected_sleeps(settle, 2):
            print("ERROR: {} slept {}, expected {}".format(name, state.slept, _expected_sleeps(settle, 2)))
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

    for name, (call, endpoint, payload, applied, settle) in WRITE_CASES.items():
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


def test_export_target_prefers_control_over_a_stale_raw(my_predbat=None):
    """
    The export target is confirmed from Control first, falling back to raw.invertor (#4517/#4421).

    GivTCP updates Control.Discharge_Target_SOC_1 synchronously when it accepts the write, but
    raw.invertor.discharge_target_soc_1 only refreshes on its separate background poll. Reading raw
    alone made the write look unapplied on hardware where that field never caught up, so the caller
    re-issued it every cycle forever. The settle sleep before the read exists for the much smaller
    residual gap and is asserted here so it cannot be dropped silently.
    """
    failed = False
    print("**** Testing the export target's Control-over-raw preference ****")

    # Control has the new value, raw is still stale - this is the #4517 case
    base, state, transport, client = _client([_blob(control={"Discharge_Target_SOC_1": 20}, raw={"discharge_target_soc_1": 0})])
    if client.set_discharge_target(20) is not True:
        print("ERROR: a write confirmed by Control was reported as failed because raw was stale")
        failed = True
    if state.slept != [1]:
        print("ERROR: expected a single 1s settle before the read-back, got {}".format(state.slept))
        failed = True

    # Control does not carry the key at all - raw is the fallback
    blob = _blob(raw={"discharge_target_soc_1": 20})
    del blob["Control"]["Discharge_Target_SOC_1"]
    base, state, transport, client = _client([blob])
    if client.set_discharge_target(20) is not True:
        print("ERROR: a write confirmed by raw.invertor was reported as failed when Control had no key")
        failed = True

    # GivTCP reports these as strings; comparing without coercion never matches the int target
    base, state, transport, client = _client([_blob(control={"Discharge_Target_SOC_1": "20"}, raw={"discharge_target_soc_1": "20"})])
    if client.set_discharge_target(20) is not True:
        print("ERROR: a string read-back of the target was not coerced and never matched")
        failed = True

    # A value that is neither numeric nor present is not mistaken for a match
    base, state, transport, client = _client([_blob(control={"Discharge_Target_SOC_1": "unavailable"}, raw={"discharge_target_soc_1": None})])
    if client.set_discharge_target(20) is not False:
        print("ERROR: an unreadable target was treated as a successful write")
        failed = True

    if not failed:
        print("PASS: the export target prefers Control, falls back to raw, and coerces strings")
    return 1 if failed else 0


def test_readers_return_nothing_without_a_snapshot(my_predbat=None):
    """
    Every reader answers None/False before the first status read, rather than raising.

    Inverter and GivTCPComponent both call these on a cycle that may run before GivTCP has
    answered - on a fresh start, or after a poll failure left rest_data unset - and each one
    documents that "not read yet" is a state callers already handle.
    """
    failed = False
    print("**** Testing every reader copes with no snapshot ****")

    base, state, transport, client = _client([_blob()])
    state.rest_data = None

    none_readers = [
        "charge_enable_time",
        "charge_target_enabled",
        "discharge_enable_time",
        "pause_mode_supported",
        "pause_slots_supported",
        "soc_kwh",
        "target_soc",
    ]
    for name in none_readers:
        value = getattr(client, name)
        if value is not None:
            print("ERROR: {} returned {} with no snapshot, expected None".format(name, value))
            failed = True

    none_methods = ["power_readings", "battery_temperature", "battery_soh", "nominal_capacity", "battery_capacity_kwh", "max_battery_rate", "read_discharge_target"]
    for name in none_methods:
        value = getattr(client, name)()
        if value is not None:
            print("ERROR: {}() returned {} with no snapshot, expected None".format(name, value))
            failed = True

    if client.inverter_details() != {}:
        print("ERROR: inverter_details() returned {} with no snapshot, expected an empty dict".format(client.inverter_details()))
        failed = True
    if client.in_calibration() is not False:
        print("ERROR: in_calibration() returned {} with no snapshot, expected False".format(client.in_calibration()))
        failed = True
    # The version fields are what the component logs at discovery, so they must read as unknown
    for name in ("givtcp_version", "firmware_version", "serial_number"):
        if getattr(client, name) != "Unknown":
            print("ERROR: {} returned {} with no snapshot, expected 'Unknown'".format(name, getattr(client, name)))
            failed = True
    if client.rest_v3 is not False:
        print("ERROR: rest_v3 was True with no snapshot")
        failed = True

    # And with a snapshot they read it, so the None above is the absence of data not a broken reader
    state.rest_data = _blob(control={"Target_SOC": 88})
    if client.target_soc != 88.0:
        print("ERROR: target_soc read {} from a snapshot, expected 88.0".format(client.target_soc))
        failed = True
    if client.serial_number != "CE1234G567":
        print("ERROR: serial_number read {} from a snapshot".format(client.serial_number))
        failed = True

    if not failed:
        print("PASS: every reader copes with no snapshot and reads one when present")
    return 1 if failed else 0


def test_reader_edge_cases(my_predbat=None):
    """
    The branches a well-formed GivTCP response never reaches.

    Every one of these is a shape some real install has produced: a boolean where the schema says
    string, a key present but null, a temperature nested one level deeper on v3, a module with a
    non-numeric capacity. They are cheap to get wrong and, being fallbacks, silent when they are.
    """
    failed = False
    print("**** Testing the readers' edge branches ****")

    base, state, transport, client = _client([_blob()])

    def check(what, got, expected):
        """Compare one reading against what it should be."""
        nonlocal failed
        if got != expected:
            print("ERROR: {} read {}, expected {}".format(what, got, expected))
            failed = True

    # A non-string Enable_Charge_Target (some builds report a real boolean)
    state.rest_data = _blob(control={"Enable_Charge_Target": 1})
    check("charge_target_enabled from an int", client.charge_target_enabled, True)

    # Keys present but null - "reported as nothing" is not "reported as off"
    state.rest_data = _blob(control={"Enable_Discharge_Schedule": None})
    check("discharge_enable_time from a null", client.discharge_enable_time, None)

    state.rest_data = _blob()
    state.rest_data["Power"] = {"Power": {}}
    check("soc_kwh with no SOC_kWh", client.soc_kwh, None)

    state.rest_data = _blob(control={"Target_SOC": "unavailable"})
    check("target_soc from an unparseable value", client.target_soc, None)

    # v3 reports nominal capacity in kWh already; v2 reports Ah and needs the 51.2V scaling
    state.rest_data = _blob(raw={"battery_nominal_capacity": 9.52})
    check("nominal_capacity on v3", client.nominal_capacity(), 9.52)
    state.rest_data = _blob(raw={"battery_nominal_capacity": 186.0})
    state.rest_data["Stats"]["GivTCP_Version"] = "2.4.0"
    check("nominal_capacity on v2", client.nominal_capacity(), 186.0 / 19.53125)

    # v3 nests the pack temperature one level down, under a stack
    state.rest_data = _blob()
    state.rest_data["Battery_Details"] = {"Battery_Stack_1": {"DF2234G370": {"Battery_Temperature": 24.0}, "DF2234G371": {"Battery_Temperature": 26.0}}}
    check("battery_temperature from a v3 stack", client.battery_temperature(), 25.0)

    # A module whose capacity will not parse is skipped, not counted as zero
    state.rest_data = _blob()
    state.rest_data["Battery_Details"] = {"A": {"Battery_Capacity": "bad", "Battery_Design_Capacity": 186.0}, "B": {"Battery_Capacity": 186.0, "Battery_Design_Capacity": 186.0}}
    check("battery_soh skipping a bad module", client.battery_soh(), 1.0)

    # read_discharge_target with neither field usable
    state.rest_data = _blob(control={"Discharge_Target_SOC_1": "unavailable"}, raw={"discharge_target_soc_1": "unavailable"})
    check("read_discharge_target with nothing readable", client.read_discharge_target(), None)

    # Already at the requested state: no POST at all
    state.rest_data = _blob(control={"Enable_Charge_Target": "enable"})
    if client.enable_charge_target(True) is not True:
        print("ERROR: enable_charge_target did not report success when already enabled")
        failed = True
    if transport.posts:
        print("ERROR: enable_charge_target posted {} when the register already matched".format(transport.posts))
        failed = True

    if not failed:
        print("PASS: the readers' edge branches behave")
    return 1 if failed else 0


def test_transport_success_paths(my_predbat=None):
    """
    post_command returns the response and get_data decodes JSON only on a 200.

    These are the real requests calls, which every other test replaces with an injected transport -
    so without this the only exercised path through them is the exception handler.
    """
    failed = False
    print("**** Testing the real transport's success and non-200 paths ****")

    base = _RestBase()
    state = InverterRestState(id=0, rest_api="http://givtcp:6345")
    state.rest_data = _blob()
    client = GivTCPRest(base, state)

    class _Fake:
        """Stands in for the requests module, returning a scripted status code."""

        def __init__(self, status_code):
            self.status_code = status_code

        def post(self, url, json=None, timeout=None):
            """Return self as the response object."""
            return self

        def get(self, url, timeout=None):
            """Return self as the response object."""
            return self

        def json(self):
            """Decode as a status blob."""
            return {"Control": {"ok": True}}

    import givtcp_rest

    original = givtcp_rest.requests
    try:
        givtcp_rest.requests = _Fake(200)
        response = client.post_command("http://givtcp:6345/setChargeRate", json={"chargeRate": 1})
        if response is None or response.status_code != 200:
            print("ERROR: post_command did not return the response on success")
            failed = True
        if client.get_data("http://givtcp:6345/readData") != {"Control": {"ok": True}}:
            print("ERROR: get_data did not decode the JSON of a 200 response")
            failed = True

        # A non-200 is not data, however well-formed its body
        givtcp_rest.requests = _Fake(503)
        if client.get_data("http://givtcp:6345/readData") is not None:
            print("ERROR: get_data returned a body from a non-200 response")
            failed = True
    finally:
        givtcp_rest.requests = original

    if not failed:
        print("PASS: the real transport returns responses and rejects non-200")
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
    failed += test_export_target_prefers_control_over_a_stale_raw(my_predbat)
    failed += test_readers_return_nothing_without_a_snapshot(my_predbat)
    failed += test_reader_edge_cases(my_predbat)
    failed += test_transport_success_paths(my_predbat)
    return failed

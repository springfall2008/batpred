# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for how long Inverter waits after writing a control entity.

inv_write_and_poll_sleep is a timeout, not a known duration: the HA service call returns as
soon as Home Assistant accepts it, and the value only appears once whatever owns the entity
has actually applied it. Sleeping the whole interval before the first look therefore charges
every write the worst case.
"""

from unittest.mock import patch

import inverter as inverter_module
from config import INVERTER_DEF
from inverter import Inverter
from const import INVERTER_MAX_RETRY, INVERTER_WRITE_BACKOFF_FAILURES, INVERTER_WRITE_DEGRADED_INTERVAL


class _PollBase:
    """A minimal Predbat stand-in whose entity state appears after a scripted number of polls."""

    def __init__(self, entity_id, final_value, polls_until_visible, published_by=None, initial="old"):
        self.entity_id = entity_id
        self.final_value = final_value
        self.polls_until_visible = polls_until_visible
        self.initial = initial
        self.reads = 0
        self.writes = 0
        self.control_ledger = None
        self.dashboard_index_app = {entity_id: published_by} if published_by else {}

    def log(self, message):
        """Swallow the log output."""
        return None

    def record_status(self, message, had_errors=False, **kwargs):
        """Swallow the status record."""
        return None

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Return the old value until polls_until_visible reads have happened, then the new one."""
        self.reads += 1
        return self.final_value if self.reads > self.polls_until_visible else self.initial

    def set_state_wrapper(self, entity_id=None, state=None, attributes=None, required_unit=None):
        """Swallow a direct state write."""
        return None

    def call_service_wrapper(self, service, **kwargs):
        """Count the service calls so a test can tell writes from polls."""
        self.writes += 1
        return True

    def unit_conversion(self, entity_id, state, units, required_unit, going_to=False):
        """No conversion in these tests."""
        return state


def _stub_inverter(base, poll_sleep=10):
    """A real (uninitialised) Inverter wired to a scripted base, with sleeps accounted not taken."""
    stub = Inverter.__new__(Inverter)
    stub.base = base
    stub.log = base.log
    stub.id = 0
    stub.count_register_writes = 0
    stub.registers_moved = 0
    stub.created_attributes = {}
    stub.inv_write_and_poll_sleep = poll_sleep
    stub.slept = []
    stub.sleep = lambda seconds: stub.slept.append(seconds)
    return stub


def test_predbat_published_entity_is_not_waited_out_in_full(my_predbat=None):
    """
    A write to an entity Predbat itself publishes returns as soon as the value appears.

    GivTCPComponent applies its REST write and republishes the entity inline off the HA event,
    which takes well under a second. Sleeping the full 10s before the first look cost about 50s
    of every switch to export (roughly five entity writes), all of it spent waiting for
    something that had already happened.
    """
    failed = False
    print("**** Testing a Predbat-published entity is polled, not waited out ****")

    base = _PollBase("select.predbat_givtcp_0_inverter_mode", "Timed Export", polls_until_visible=1, published_by="givtcp")
    stub = _stub_inverter(base)

    if not stub.write_and_poll_option("inverter_mode", base.entity_id, "Timed Export"):
        print("ERROR: write_and_poll_option reported failure")
        failed = True

    total_slept = sum(stub.slept)
    if total_slept >= stub.inv_write_and_poll_sleep:
        print("ERROR: slept {}s of a {}s budget for a value that appeared immediately".format(total_slept, stub.inv_write_and_poll_sleep))
        failed = True
    if base.writes != 1:
        print("ERROR: expected exactly one service call, got {}".format(base.writes))
        failed = True

    if not failed:
        print("PASS: returned after {}s rather than the full {}s".format(total_slept, stub.inv_write_and_poll_sleep))
    return 1 if failed else 0


def test_third_party_entity_is_polled_too(my_predbat=None):
    """
    An entity Predbat does not publish is polled on exactly the same terms.

    The poll is only ever reached once the caller has established the entity did NOT already
    hold the target, so a matching read is a transition observed after our own write, not a
    stale value that happened to agree - which holds whoever owns the entity. Every inverter
    type paid the flat interval, so every inverter type gets the write back sooner.
    """
    failed = False
    print("**** Testing a third-party entity is polled on the same terms ****")

    base = _PollBase("select.solis_inverter_mode", "Timed Export", polls_until_visible=1, published_by=None)
    stub = _stub_inverter(base)

    if not stub.write_and_poll_option("inverter_mode", base.entity_id, "Timed Export"):
        print("ERROR: write_and_poll_option reported failure")
        failed = True

    total_slept = sum(stub.slept)
    if total_slept >= stub.inv_write_and_poll_sleep:
        print("ERROR: slept {}s of a {}s budget for a value that appeared immediately".format(total_slept, stub.inv_write_and_poll_sleep))
        failed = True

    if not failed:
        print("PASS: a third-party entity returned after {}s rather than the full {}s".format(total_slept, stub.inv_write_and_poll_sleep))
    return 1 if failed else 0


def test_a_write_that_never_lands_still_fails_within_budget(my_predbat=None):
    """
    Polling must not extend the total wait for a write that never appears.

    The retry ladder is INVERTER_MAX_RETRY write attempts of inv_write_and_poll_sleep each; the
    change is meant to make a successful write cheap, not a failing one more expensive.
    """
    failed = False
    print("**** Testing a write that never lands still fails within its budget ****")

    base = _PollBase("select.predbat_givtcp_0_inverter_mode", "Timed Export", polls_until_visible=10**6, published_by="givtcp")
    stub = _stub_inverter(base)

    if stub.write_and_poll_option("inverter_mode", base.entity_id, "Timed Export"):
        print("ERROR: write_and_poll_option reported success for a write that never landed")
        failed = True

    budget = INVERTER_MAX_RETRY * stub.inv_write_and_poll_sleep
    total_slept = sum(stub.slept)
    if total_slept > budget:
        print("ERROR: slept {}s, over the {}s budget".format(total_slept, budget))
        failed = True
    if base.writes != INVERTER_MAX_RETRY:
        print("ERROR: expected {} write attempts, got {}".format(INVERTER_MAX_RETRY, base.writes))
        failed = True

    if not failed:
        print("PASS: failed after {} attempts and {}s, within the {}s budget".format(base.writes, total_slept, budget))
    return 1 if failed else 0


def test_value_and_switch_helpers_poll_too(my_predbat=None):
    """The same treatment applies to write_and_poll_value and write_and_poll_switch.

    All three helpers shared the same flat sleep, and a switch to export goes through all of
    them - the slot times are options, scheduled_discharge_enable is a switch, the rates and
    targets are values.
    """
    failed = False
    print("**** Testing the value and switch helpers poll as well ****")

    base = _PollBase("number.predbat_givtcp_0_charge_rate", 3000, polls_until_visible=1, published_by="givtcp", initial=0)
    stub = _stub_inverter(base)
    if not stub.write_and_poll_value("charge_rate", base.entity_id, 3000, fuzzy=0):
        print("ERROR: write_and_poll_value reported failure")
        failed = True
    if sum(stub.slept) >= stub.inv_write_and_poll_sleep:
        print("ERROR: write_and_poll_value slept {}s of {}s".format(sum(stub.slept), stub.inv_write_and_poll_sleep))
        failed = True

    base = _PollBase("switch.predbat_givtcp_0_scheduled_discharge_enable", "on", polls_until_visible=1, published_by="givtcp", initial="off")
    stub = _stub_inverter(base)
    if not stub.write_and_poll_switch("scheduled_discharge_enable", base.entity_id, True):
        print("ERROR: write_and_poll_switch reported failure")
        failed = True
    if sum(stub.slept) >= stub.inv_write_and_poll_sleep:
        print("ERROR: write_and_poll_switch slept {}s of {}s".format(sum(stub.slept), stub.inv_write_and_poll_sleep))
        failed = True

    if not failed:
        print("PASS: all three write helpers poll rather than waiting the interval out")
    return 1 if failed else 0


class _LoggingPollBase(_PollBase):
    """A _PollBase that keeps its log lines and error statuses, so a test can count them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.messages = []
        self.errors = []

    def log(self, message):
        """Keep the log line."""
        self.messages.append(message)

    def record_status(self, message, had_errors=False, **kwargs):
        """Keep the error statuses."""
        if had_errors:
            self.errors.append(message)


def _gateway_stub(base):
    """A stub Inverter with the GWMQTT write policy, recording the seconds slept before each write."""
    row = INVERTER_DEF["GWMQTT"]
    stub = _stub_inverter(base, poll_sleep=row["write_and_poll_sleep"])
    stub.inv_write_max_retry = row["write_max_retry"]
    stub.inv_write_backoff = row["write_backoff"]
    stub.write_backoff = {}
    write_times = []
    service = base.call_service_wrapper

    def timed_service(service_name, **kwargs):
        write_times.append(sum(stub.slept))
        return service(service_name, **kwargs)

    base.call_service_wrapper = timed_service
    return stub, write_times


def test_gateway_retries_are_fewer_and_spaced(my_predbat=None):
    """
    A gateway write that never verifies is sent at most write_max_retry times, write_and_poll_sleep apart.

    The hub applies register writes one at a time and each can take 10-20s, so the default ladder
    (INVERTER_MAX_RETRY attempts 2s apart) queued far more commands than the hub could work through
    and the read-back fell further behind with every one.
    """
    failed = False
    print("**** Testing gateway write retries are capped and spaced ****")
    row = INVERTER_DEF["GWMQTT"]
    if row["write_max_retry"] > 3 or row["write_and_poll_sleep"] < 10:
        print("ERROR: GWMQTT policy is {} retries {}s apart, expected at most 3 at least 10s apart".format(row["write_max_retry"], row["write_and_poll_sleep"]))
        failed = True

    cases = [
        ("value", "number.predbat_gateway_reserve", 5, 98, lambda stub, e, v: stub.write_and_poll_value("reserve", e, v)),
        ("switch", "switch.predbat_gateway_charge_enabled", True, "off", lambda stub, e, v: stub.write_and_poll_switch("scheduled_charge_enable", e, v)),
        ("option", "select.predbat_gateway_discharge_slot1_start", "16:25:00", "17:50:00", lambda stub, e, v: stub.write_and_poll_option("discharge_start_time", e, v)),
    ]
    for label, entity_id, value, stale, write in cases:
        base = _PollBase(entity_id, value, polls_until_visible=10**6, initial=stale)
        stub, write_times = _gateway_stub(base)
        if write(stub, entity_id, value):
            print("ERROR: {} write reported success for a value that never landed".format(label))
            failed = True
        if base.writes != row["write_max_retry"]:
            print("ERROR: {} write sent {} times, expected {}".format(label, base.writes, row["write_max_retry"]))
            failed = True
        gaps = [later - earlier for earlier, later in zip(write_times, write_times[1:])]
        if any(gap < row["write_and_poll_sleep"] for gap in gaps):
            print("ERROR: {} writes only {}s apart, expected at least {}s".format(label, gaps, row["write_and_poll_sleep"]))
            failed = True

    if not failed:
        print("PASS: gateway writes are sent at most {} times, {}s apart".format(row["write_max_retry"], row["write_and_poll_sleep"]))
    return 1 if failed else 0


def test_other_inverters_keep_the_default_policy(my_predbat=None):
    """
    Every other inverter type keeps INVERTER_MAX_RETRY attempts and never backs off.

    The gateway settings are opt-in per INVERTER_DEF row; nothing about a GivTCP, Solis or cloud
    write may change because of them.
    """
    failed = False
    print("**** Testing non-gateway inverters keep the default write policy ****")
    for inverter_type, row in INVERTER_DEF.items():
        if inverter_type == "GWMQTT":
            continue
        if row.get("write_max_retry", INVERTER_MAX_RETRY) != INVERTER_MAX_RETRY or row.get("write_backoff", False):
            print("ERROR: {} has a non-default write policy".format(inverter_type))
            failed = True

    # The stub carries the class defaults, as an inverter of any other type does after refresh_config()
    base = _PollBase("number.solis_reserve", 5, polls_until_visible=10**6, initial=98)
    stub = _stub_inverter(base, poll_sleep=2)
    for cycle in range(4):
        base.writes = 0
        stub.write_and_poll_value("reserve", base.entity_id, 5)
        if base.writes != INVERTER_MAX_RETRY:
            print("ERROR: cycle {} sent {} writes, expected {}".format(cycle, base.writes, INVERTER_MAX_RETRY))
            failed = True
    if stub.slept and max(stub.slept) > 2:
        print("ERROR: polled in steps up to {}s, expected the 2s interval".format(max(stub.slept)))
        failed = True

    if not failed:
        print("PASS: other inverter types still retry {} times and never back off".format(INVERTER_MAX_RETRY))
    return 1 if failed else 0


# Each case: label, entity, write(stub, entity, value), a target, its stale read-back, a second target
# (a stop: rate to 0, enable to off) and a read-back that is stale for that one too
_GATEWAY_CONTROLS = [
    ("number", "number.predbat_gateway_charge_rate", lambda stub, e, v: stub.write_and_poll_value("charge_rate", e, v), 3000, 0, 0, 3000),
    ("switch", "switch.predbat_gateway_discharge_enabled", lambda stub, e, v: stub.write_and_poll_switch("scheduled_discharge_enable", e, v), True, "off", False, "on"),
    ("option", "select.predbat_gateway_discharge_slot1_start", lambda stub, e, v: stub.write_and_poll_option("discharge_start_time", e, v), "16:25:00", "17:50:00", "18:00:00", "17:50:00"),
]


class _Clock:
    """A monotonic clock the test moves by hand, patched in for the degraded publish interval."""

    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def patch(self):
        return patch.object(inverter_module.time, "monotonic", self)


def _degrade(label, entity_id, write, value, stale):
    """A gateway stub whose control has failed INVERTER_WRITE_BACKOFF_FAILURES writes of value in a row."""
    base = _LoggingPollBase(entity_id, value, polls_until_visible=10**6, initial=stale)
    stub, write_times = _gateway_stub(base)
    for _ in range(INVERTER_WRITE_BACKOFF_FAILURES):
        write(stub, entity_id, value)
    return base, stub, write_times


def _sent(base, stub, write, entity_id, value):
    """Run one write, returning (result, service calls it made)."""
    before = base.writes
    result = write(stub, entity_id, value)
    return result, base.writes - before


def _degraded_logs(base):
    return sum("sending it at most once every" in message for message in base.messages)


def _recovered_logs(base):
    return sum("retrying writes normally again" in message for message in base.messages)


def test_gateway_degraded_control_is_sent_every_interval(my_predbat=None):
    """
    A gateway control that keeps failing is sent once per INVERTER_WRITE_DEGRADED_INTERVAL, not every cycle.

    The quick update re-applies an unconfirmed control every minute, and a hub write under an EMS
    can take longer than that, so even one send per call piles up. In between sends the call still
    checks the read-back and reports the mismatch as an error, so the cycle cannot look healthy
    while the control was never applied.
    """
    failed = False
    print("**** Testing a failing gateway control is sent once per interval ****")
    for label, entity_id, write, value, stale, _other, _other_stale in _GATEWAY_CONTROLS:
        clock = _Clock()
        with clock.patch():
            base, stub, _ = _degrade(label, entity_id, write, value, stale)
            if _degraded_logs(base) != 1:
                print("ERROR: {} becoming degraded logged {} times, expected once".format(label, _degraded_logs(base)))
                failed = True
            # The failing call that degraded it has just published, so the interval runs from here:
            # the calls at 60-240 s send nothing, the first degraded publish is at 300 s
            start = clock.now
            publish_times = []
            for minute in range(1, 11):
                clock.now = start + minute * 60
                errors_before = len(base.errors)
                result, writes = _sent(base, stub, write, entity_id, value)
                if writes:
                    publish_times.append(clock.now - start)
                if result or writes > 1 or len(base.errors) != errors_before + 1:
                    print("ERROR: {} minute {} returned {} after {} writes and {} new errors".format(label, minute, result, writes, len(base.errors) - errors_before))
                    failed = True
            expected = [float(INVERTER_WRITE_DEGRADED_INTERVAL), float(2 * INVERTER_WRITE_DEGRADED_INTERVAL)]
            if publish_times != expected:
                print("ERROR: {} published at {}s, expected {}".format(label, publish_times, expected))
                failed = True
            if _degraded_logs(base) != 1:
                print("ERROR: {} degraded mode logged {} times, expected once".format(label, _degraded_logs(base)))
                failed = True

    if not failed:
        print("PASS: a failing gateway control is sent every {}s and still reports the failure each call".format(INVERTER_WRITE_DEGRADED_INTERVAL))
    return 1 if failed else 0


def test_gateway_degraded_control_recovers(my_predbat=None):
    """
    Degraded mode ends on a verified write, a matching read-back, or a new target.

    A new target - including a stop - is published straight away with the full retry ladder, even
    inside the interval, and a control that verified and later drifts is not degraded, so the
    drift is corrected at once too. Each recovery is logged once.
    """
    failed = False
    print("**** Testing a gateway control leaves degraded mode ****")
    retries = INVERTER_DEF["GWMQTT"]["write_max_retry"]
    for label, entity_id, write, value, stale, other, other_stale in _GATEWAY_CONTROLS:
        clock = _Clock()
        with clock.patch():
            # A write that verifies, then drifts back: corrected at once with the full ladder
            base, stub, _ = _degrade(label, entity_id, write, value, stale)
            clock.now += INVERTER_WRITE_DEGRADED_INTERVAL  # the next degraded publish
            base.polls_until_visible = base.reads + 1
            result, writes = _sent(base, stub, write, entity_id, value)
            if not result or writes != 1 or entity_id in stub.write_backoff or _recovered_logs(base) != 1:
                print("ERROR: {} verified write did not recover (result {}, writes {})".format(label, result, writes))
                failed = True
            clock.now += 60
            base.polls_until_visible = 10**6
            base.reads = 0
            result, writes = _sent(base, stub, write, entity_id, value)
            if writes != retries:
                print("ERROR: {} drift after a verify sent {} writes, expected {} straight away".format(label, writes, retries))
                failed = True

            # Inside the interval, a read-back that now matches clears it without a publish
            base, stub, _ = _degrade(label, entity_id, write, value, stale)
            clock.now += 60
            base.polls_until_visible = 0
            result, writes = _sent(base, stub, write, entity_id, value)
            if not result or writes or entity_id in stub.write_backoff or _recovered_logs(base) != 1:
                print("ERROR: {} matching read did not recover (result {}, writes {})".format(label, result, writes))
                failed = True

            # Inside the interval, a new target (a stop) gets the full ladder at once
            base, stub, _ = _degrade(label, entity_id, write, value, stale)
            clock.now += 60
            base.final_value = other
            base.initial = other_stale
            result, writes = _sent(base, stub, write, entity_id, other)
            if writes != retries or _recovered_logs(base) != 1:
                print("ERROR: {} new target sent {} writes inside the interval, expected {}".format(label, writes, retries))
                failed = True

            # After recovering, a single failure does not degrade it again
            base, stub, _ = _degrade(label, entity_id, write, value, stale)
            base.polls_until_visible = 0
            write(stub, entity_id, value)
            base.polls_until_visible = 10**6
            base.reads = 0
            _sent(base, stub, write, entity_id, value)
            result, writes = _sent(base, stub, write, entity_id, value)
            if writes != retries:
                print("ERROR: {} one failure after recovering already cut the retries to {}".format(label, writes))
                failed = True

    if not failed:
        print("PASS: degraded mode ends on a verified write, a matching read or a new target")
    return 1 if failed else 0

def run_inverter_write_poll_tests(my_predbat):
    """Run every write-and-poll timing test, returning a non-zero count on failure."""
    failed = 0
    failed += test_predbat_published_entity_is_not_waited_out_in_full(my_predbat)
    failed += test_third_party_entity_is_polled_too(my_predbat)
    failed += test_a_write_that_never_lands_still_fails_within_budget(my_predbat)
    failed += test_value_and_switch_helpers_poll_too(my_predbat)
    failed += test_gateway_retries_are_fewer_and_spaced(my_predbat)
    failed += test_other_inverters_keep_the_default_policy(my_predbat)
    failed += test_gateway_degraded_control_is_sent_every_interval(my_predbat)
    failed += test_gateway_degraded_control_recovers(my_predbat)
    return failed

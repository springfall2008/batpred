# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for how long Inverter waits after writing a control entity.

inv_write_and_poll_sleep is a timeout, not a known duration: the HA service call returns as
soon as Home Assistant accepts it, and the value only appears once whatever owns the entity
has actually applied it. Sleeping the whole interval before the first look therefore charges
every write the worst case.
"""

from inverter import Inverter
from const import INVERTER_MAX_RETRY


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


def run_inverter_write_poll_tests(my_predbat):
    """Run every write-and-poll timing test, returning a non-zero count on failure."""
    failed = 0
    failed += test_predbat_published_entity_is_not_waited_out_in_full(my_predbat)
    failed += test_third_party_entity_is_polled_too(my_predbat)
    failed += test_a_write_that_never_lands_still_fails_within_budget(my_predbat)
    failed += test_value_and_switch_helpers_poll_too(my_predbat)
    return failed

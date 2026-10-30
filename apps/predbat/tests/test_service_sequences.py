# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Observed service contracts through real Inverter methods and argument resolution."""

import copy
from contextlib import ExitStack
from unittest.mock import patch

from inverter import Inverter


class SequenceFixture:
    """An isolated HA boundary with explicit accepted, delayed, rejected and partial writes."""

    def __init__(self, base):
        """Prepare a real inverter without unrelated hardware discovery."""
        self.base = base
        self.states = {
            "select.source": {"state": "Grid", "options": ["Disabled", "Grid"]},
            "number.target": {"state": 40, "min": 0, "max": 100, "step": 1},
        }
        self.calls, self.errors, self.logs = [], [], []
        self.reject, self.unobserved = set(), set()
        self.pending = {}
        self.delayed = False
        self.revert_on_number = False
        self.raise_on_number = False
        self.sleeps = 0
        self.inv = Inverter.__new__(Inverter)
        self.inv.base, self.inv.id, self.inv.log = base, 0, self.logs.append
        self.inv.reserve_percent = 10
        self.inv.count_register_writes = 0
        self.inv.inv_write_and_poll_sleep = 1
        self.inv.sleep = self.sleep

    def __enter__(self):
        """Keep the real template resolver, replacing only its configuration and HA boundary."""
        self.stack = ExitStack()
        for name, value in {
            "args": {},
            "last_service_hash": {},
            "get_state_wrapper": self.read,
            "call_service_wrapper": self.call,
            "record_status": self.status,
            "log": self.logs.append,
        }.items():
            self.stack.enter_context(patch.object(self.base, name, value))
        return self

    def __exit__(self, *exception):
        """Restore every patched base attribute, including on assertion failure."""
        return self.stack.__exit__(*exception)

    def read(self, entity_id=None, default=None, attribute=None, **kwargs):
        """Expose attributes unchanged by writes, as HA does."""
        return self.states.get(entity_id, {}).get(attribute or "state", default)

    def call(self, service, **data):
        """Dispatch once, retaining metadata and modelling partial application explicitly."""
        self.calls.append((service, copy.deepcopy(data)))
        entities = data["entity_id"].split(",")
        if self.raise_on_number and service == "number/set_value":
            raise RuntimeError("transport failure")
        if self.reject.intersection(entities):
            return False
        value = data.get("option", data.get("value", "on" if service.endswith("turn_on") else "off"))
        for entity in entities:
            if entity not in self.unobserved:
                if self.delayed:
                    self.pending[entity] = value
                else:
                    self.states[entity]["state"] = value
        if self.revert_on_number and service == "number/set_value":
            self.states["select.source"]["state"] = "Grid"
        return True

    def sleep(self, seconds):
        """Publish delayed observations only after dispatch and a polling wait."""
        self.sleeps += 1
        for entity, value in self.pending.items():
            self.states[entity]["state"] = value
        self.pending.clear()

    def status(self, message, had_errors=False):
        """Retain failures independently of whether a service was configured."""
        if had_errors:
            self.errors.append(message)

    def run(self, steps, data=None, extra_data=None):
        """Call the public hook without clearing its repeat memory between cycles."""
        self.base.args["charge_start_service"] = steps
        return self.inv.call_service_template("charge_start_service", data or {}, extra_data=extra_data)


def sequence_steps(target=80, source="select.source", number="number.target"):
    """A repeated source around a target write, using native field names."""
    return [
        {"service": "select.select_option", "entity_id": source, "option": "Disabled", "verify": True},
        {"service": "number.set_value", "entity_id": number, "value": target, "verify": True},
        {"service": "select.select_option", "entity_id": source, "option": "Grid", "verify": True},
    ]


def test_sequence_observation_and_drift(base):
    """Immediate/delayed observations, idempotence, numeric drift and changed target ordering."""
    for delayed in (False, True):
        with SequenceFixture(base) as fixture:
            fixture.delayed = delayed
            steps = sequence_steps()
            assert fixture.run(steps) is True
            assert not fixture.errors
            assert [data.get("option", data.get("value")) for _, data in fixture.calls] == ["Disabled", 80, "Grid"]
            assert fixture.inv.count_register_writes == 3
            assert fixture.sleeps == (3 if delayed else 0)
            assert fixture.states["number.target"]["max"] == 100
            fixture.calls.clear()
            fixture.run(steps)
            assert not fixture.calls
            fixture.states["number.target"]["state"] = 30
            fixture.run(steps)
            assert [data.get("option", data.get("value")) for _, data in fixture.calls] == ["Disabled", 80, "Grid"]
            fixture.calls.clear()
            fixture.run(sequence_steps(90))
            assert [data.get("option", data.get("value")) for _, data in fixture.calls] == ["Disabled", 90, "Grid"]
            fixture.calls.clear()
            fixture.states["select.source"]["state"] = "Disabled"
            fixture.run(sequence_steps(90))
            assert len(fixture.calls) == 1 and fixture.calls[0][1]["option"] == "Grid"


def test_sequence_failure_barriers(base):
    """Reject, timeout, exception and reverted earlier state must prevent later writes."""
    for failure, expected_calls in (("source_reject", 1), ("source_timeout", 1), ("number_reject", 2), ("number_timeout", 2), ("exception", 2), ("reversion", 2)):
        with SequenceFixture(base) as fixture:
            if failure.endswith("reject"):
                fixture.reject.add("select.source" if failure.startswith("source") else "number.target")
            elif failure.endswith("timeout"):
                fixture.unobserved.add("select.source" if failure.startswith("source") else "number.target")
            fixture.raise_on_number = failure == "exception"
            fixture.revert_on_number = failure == "reversion"
            assert fixture.run(sequence_steps()) is True
            assert len(fixture.calls) == expected_calls, failure
            assert fixture.errors, failure
            assert not any(data.get("option") == "Grid" for _, data in fixture.calls), failure
            fixture.reject.clear()
            fixture.unobserved.clear()
            fixture.raise_on_number = fixture.revert_on_number = False
            fixture.calls.clear()
            fixture.errors.clear()
            fixture.run(sequence_steps())
            # The reversion can itself leave the final state correct; then retry is a no-op.
            assert not fixture.errors, failure
            assert fixture.calls or failure == "reversion", failure
            assert fixture.read("number.target") == 80 and fixture.read("select.source") == "Grid"


def test_sequence_preflight(base):
    """An invalid LAST step or entity must prevent even a valid first write."""
    for case in ("service", "missing", "unavailable", "option", "options", "bounds", "step", "boolean", "nan", "infinite", "range", "off_step", "mixed", "verify_false", "target", "repeat", "entity", "duplicate", "nested"):
        with SequenceFixture(base) as fixture:
            steps = sequence_steps()
            if case == "service":
                steps[-1]["service"] = "homeassistant.restart"
            elif case == "missing":
                del fixture.states["number.target"]
            elif case == "unavailable":
                fixture.states["number.target"]["state"] = "unavailable"
            elif case == "option":
                steps[-1]["option"] = "unsupported"
            elif case == "options":
                del fixture.states["select.source"]["options"]
            elif case == "bounds":
                del fixture.states["number.target"]["max"]
            elif case == "step":
                fixture.states["number.target"]["step"] = 0
            elif case in ("boolean", "nan", "infinite", "range", "off_step"):
                steps[1]["value"] = {"boolean": True, "nan": float("nan"), "infinite": float("inf"), "range": 101, "off_step": 80.5}[case]
            elif case == "mixed":
                del steps[-1]["verify"]
            elif case == "verify_false":
                steps[-1]["verify"] = False
            elif case in ("target", "repeat"):
                steps[-1][case] = True
            elif case == "entity":
                steps[-1]["entity_id"] = "number.target"
            elif case == "duplicate":
                steps[-1]["entity_id"] = "select.source,select.source"
            elif case == "nested":
                steps[-1]["entity_id"] = [["select.source"]]
            assert fixture.run(steps) is True, case
            assert not fixture.calls and fixture.errors, case


def test_sequence_partial_group(base):
    """A partially applied group cannot enable charging; a later cycle can finish it."""
    with SequenceFixture(base) as fixture:
        fixture.states["number.other"] = dict(fixture.states["number.target"])
        fixture.unobserved.add("number.other")
        steps = sequence_steps(number="number.target, number.other")
        fixture.run(steps)
        assert len(fixture.calls) == 2 and fixture.errors
        assert fixture.read("number.target") == 80 and fixture.read("number.other") == 40
        assert fixture.read("select.source") == "Disabled"
        fixture.unobserved.clear()
        fixture.calls.clear()
        fixture.errors.clear()
        fixture.run(steps)
        assert len(fixture.calls) == 2 and not fixture.errors
        assert fixture.read("number.other") == 80 and fixture.read("select.source") == "Grid"


def test_sequence_resolution_and_noop(base):
    """Zero values, effective floors, multi-inverter fields and handled no-op semantics."""
    with SequenceFixture(base) as fixture:
        assert fixture.inv.call_service_template("missing", {}) is False
        assert fixture.run([]) is True and not fixture.calls
        fixture.inv.reserve_percent = 10.2
        fixture.run(sequence_steps("{reserve_soc}"))
        assert fixture.read("number.target") == 11
        fixture.inv.reserve_percent = 20
        fixture.run(sequence_steps("{reserve_soc}"))
        assert fixture.read("number.target") == 20
        fixture.run(sequence_steps("{requested}"), extra_data={"requested": 0})
        assert fixture.read("number.target") == 0 and not fixture.errors
        fixture.states["number.other"] = dict(fixture.states["number.target"])
        fixture.inv.id = 1
        fixture.calls.clear()
        steps = [{"service": "number.set_value", "entity_id": ["number.target", "number.other"], "value": [40, 60], "verify": True}]
        fixture.run(steps)
        assert fixture.read("number.target") == 0 and fixture.read("number.other") == 60
        assert len(fixture.calls) == 1


def test_sequence_service_domains(base):
    """Exercise every supported domain with native values rather than only number/select."""
    for service, entity, before, expected, attributes, field in (
        ("input_number.set_value", "input_number.limit", 40, 0, {"min": 0, "max": 100, "step": 1}, "value"),
        ("input_select.select_option", "input_select.mode", "A", "B", {"options": ["A", "B"]}, "option"),
        ("switch.turn_on", "switch.enable", "off", "on", {}, None),
        ("switch.turn_off", "switch.enable", "on", "off", {}, None),
        ("input_boolean.turn_on", "input_boolean.enable", "off", "on", {}, None),
        ("input_boolean.turn_off", "input_boolean.enable", "on", "off", {}, None),
    ):
        with SequenceFixture(base) as fixture:
            fixture.states[entity] = {"state": before, **attributes}
            step = {"service": service, "entity_id": entity, "verify": True}
            if field:
                step[field] = expected
            fixture.run([step])
            assert len(fixture.calls) == 1 and fixture.read(entity) == expected and not fixture.errors, service


def run_service_sequence_tests(my_predbat):
    """Report all contract groups without hiding later groups after an assertion failure."""
    failed = False
    for test in (test_sequence_observation_and_drift, test_sequence_failure_barriers, test_sequence_preflight, test_sequence_partial_group, test_sequence_resolution_and_noop, test_sequence_service_domains):
        try:
            test(my_predbat)
            print("PASS: " + test.__name__)
        except AssertionError as error:
            print("ERROR: {}: {}".format(test.__name__, error))
            failed = True
    return failed

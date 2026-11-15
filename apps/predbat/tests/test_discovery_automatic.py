# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the coordinator's automatic inverter configuration: opting in, configure() and its triggers, and GE Cloud's acceptance against its previous automatic configuration.

The coordinator is real; the base and the component registry are small stubs, so every write to args is
seen through the recorded set_arg calls.
"""

import asyncio
import copy
import os
import subprocess
import sys
import threading
import time

import coordinator as coordinator_module
from bindings import BINDING_SETTINGS, COMPACT_SETTINGS, INVERT_SETTINGS, SITE_SETTINGS, slot_value
from config import APPS_SCHEMA
from coordinator import Coordinator
from gecloud import DEVICE_REFRESH_SECONDS, GE_CLOUD_DUMMIED_SETTINGS, GECloudData, apply_legacy_bindings
from tests.test_ge_cloud import GEC_FULL_REGISTERS, GEC_INFO, GEC_MIXED_REGISTERS, MockGECloudDirect, _gec_devices, _make_rediscovery_ge_cloud
from tests.test_infra import run_async
from userinterface import UserInterface


class _Base:
    """Stand-in for PredBat: args, the apps.yaml snapshot, a recording log and Fetch.set_arg semantics."""

    def __init__(self, args=None, apps_yaml=None):
        """Seed args (and the snapshot, a copy of args unless given)."""
        self.args = copy.deepcopy(args or {})
        self.args_from_apps_yaml = copy.deepcopy(apps_yaml if apps_yaml is not None else self.args)
        self.apps_yaml_override_warned = set()
        self.prefix = "predbat"
        self.log_messages = []
        self.set_calls = []
        self.fail_set_arg = None

    def log(self, message):
        """Record a log line."""
        self.log_messages.append(message)

    def get_arg(self, name, default=None, **kwargs):
        """Read args, falling back to default."""
        return self.args.get(name, default)

    def set_arg(self, arg, value, index=None):
        """PredBat.set_arg: None deletes the key, an index pads the list with None and sets that entry, otherwise the whole value."""
        if self.fail_set_arg is not None:
            raise self.fail_set_arg
        self.set_calls.append((arg, copy.deepcopy(value), index))
        if value is None:
            self.args.pop(arg, None)
        elif index is not None:
            if arg not in self.args:
                self.args[arg] = []
            while len(self.args[arg]) <= index:
                self.args[arg].append(None)
            self.args[arg][index] = value
        else:
            self.args[arg] = value


class _TemplatingBase(_Base):
    """A _Base whose get_arg expands {name} templates against args with PredBat's own resolve_arg, as apps.yaml written from a template needs; never looks up HA state."""

    resolve_arg = UserInterface.resolve_arg

    def get_arg(self, name, default=None, index=None, **kwargs):
        """args[name] (default when absent) through the real resolve_arg, indirect off: the template expanded, the entity id itself returned."""
        return self.resolve_arg(name, self.args.get(name, default), default=default, indirect=False, index=index)


class _Components:
    """Stand-in for components.Components: the real Coordinator, a name -> component map and the status lookups assemble() reads."""

    def __init__(self, base, components=None):
        """Build the real Coordinator on base and register this registry on it."""
        self.base = base
        base.components = self
        self.coordinator = Coordinator(base)
        self.components = dict(components or {})

    def get_all(self):
        """Every registered component name."""
        return sorted(self.components)

    def is_active(self, name):
        """Whether the named component is registered and not stopped."""
        return self.components.get(name) is not None

    def is_alive(self, name):
        """Same as is_active, in this stub."""
        return self.is_active(name)

    def load_error(self, name):
        """Never a load error, in this stub."""
        return None

    def get_component(self, name):
        """The registered component, None when unknown or stopped."""
        return self.components.get(name)


class _GECloud:
    """A stub component whose check_automatic_config records its calls."""

    def __init__(self):
        """No calls yet."""
        self.checks = []

    def build_discovery(self):
        """Present so the catalogue treats this as a reporter."""
        return None

    def check_automatic_config(self, slots):
        """Record the slots the coordinator passes."""
        self.checks.append(list(slots))


def _rw(entity_id):
    """A writable entity descriptor."""
    return {"entity_id": entity_id, "access": "rw"}


def _ro(entity_id):
    """A read-only entity descriptor."""
    return {"entity_id": entity_id, "access": "r"}


def _entities(serial):
    """GE Cloud's usual bindings for one battery."""
    return {
        "soc_percent": _ro("sensor.predbat_gecloud_{}_soc".format(serial)),
        "charge_rate": _rw("number.predbat_gecloud_{}_battery_charge_rate".format(serial)),
        "reserve": _rw("number.predbat_gecloud_{}_reserve".format(serial)),
    }


def _record(serial, entities=None, inverter_type="GEC"):
    """One GE Cloud inverter record."""
    record = {"device_id": "gecloud:" + serial, "hardware_ids": {"serial": serial}, "entities": entities if entities is not None else _entities(serial)}
    if inverter_type:
        record["inverter_type"] = inverter_type
    return record


def _report(serials, automatic=True):
    """A GE-Cloud-shaped report with one driven record per serial."""
    return {"automatic": automatic, "inverters": [_record(serial) for serial in serials]}


def _setup(args=None, apps_yaml=None, components=None):
    """(base, registry, coordinator) with gecloud registered unless components says otherwise."""
    base = _Base(args=args, apps_yaml=apps_yaml)
    registry = _Components(base, components if components is not None else {"gecloud": _GECloud()})
    return base, registry, registry.coordinator


def _pairs(calls):
    """set_arg calls as (setting, index) pairs."""
    return [(arg, index) for arg, _, index in calls]


def test_configures_on_report():
    """An opted-in component's report configures its inverter at once, before any explicit configure()."""
    base, registry, coordinator = _setup()
    assert coordinator.allow_automatic("gecloud") is True
    assert base.set_calls == []  # opting in with nothing reported writes nothing
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert base.args["inverter_type"] == ["GEC"], base.args.get("inverter_type")
    assert base.args["num_inverters"] == 1, base.args.get("num_inverters")
    assert base.args["charge_rate"] == ["number.predbat_gecloud_ce1111g111_battery_charge_rate"]
    assert coordinator.slot_map == {"gecloud:ce1111g111": 0}
    assert "Coordinator: inverter slot 0 configured from gecloud (gecloud:ce1111g111)" in base.log_messages, base.log_messages
    print("PASS: configures on report")
    return 0


def test_not_opted_in_is_not_configured():
    """A component that never opted in is not configured: with nothing opted in a pass does not even run."""
    base, registry, coordinator = _setup()
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.set_calls == [], base.set_calls
    assert coordinator.bindings is None and coordinator.slot_map == {}
    assert "inverter_type" not in base.args
    print("PASS: not opted in is not configured")
    return 0


def test_unchanged_report_writes_nothing():
    """The same report twice: the second writes nothing and logs nothing but report()'s own line."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    calls = list(base.set_calls)
    lines = len(base.log_messages)
    assert calls
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert base.set_calls == calls, base.set_calls
    assert base.log_messages[lines:] == ["Coordinator: gecloud reported 1 inverters"], base.log_messages[lines:]
    # An explicit pass with nothing new is silent too
    coordinator.configure()
    assert base.set_calls == calls and len(base.log_messages) == lines + 1
    print("PASS: unchanged report writes nothing")
    return 0


def test_second_device_leaves_first_alone():
    """A second inverter is written at index 1 only; the first slot's entries are not rewritten."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["a11111"]))
    first = len(base.set_calls)
    coordinator.report("gecloud", _report(["a11111", "b22222"]))
    second = base.set_calls[first:]
    assert second, "the second device must be written"
    assert all(index == 1 or arg == "num_inverters" for arg, _, index in second), second
    assert ("num_inverters", 2, None) in second, second
    assert coordinator.slot_map == {"gecloud:a11111": 0, "gecloud:b22222": 1}, coordinator.slot_map
    assert base.args["inverter_type"] == ["GEC", "GEC"]
    assert "Coordinator: inverter slot 1 configured from gecloud (gecloud:b22222)" in base.log_messages
    assert base.log_messages.count("Coordinator: inverter slot 0 configured from gecloud (gecloud:a11111)") == 1
    print("PASS: second device leaves the first alone")
    return 0


def test_unreported_source_left_out():
    """Opting in before any report is silent; a later report with no driven inverter warns once and keeps the slot."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    assert not [line for line in base.log_messages if "reported no inverter" in line], base.log_messages
    assert base.set_calls == [], base.set_calls
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert coordinator.slot_map == {"gecloud:ce1111g111": 0}, coordinator.slot_map
    calls = list(base.set_calls)
    empty = {"automatic": True, "inverters": [_record("pv3333", {"pv_power": _ro("sensor.pv3333_pv_power")}, inverter_type=None)]}
    coordinator.report("gecloud", empty)
    coordinator.configure()
    warning = "Warn: Coordinator: gecloud reported no inverter to configure - keeping its existing slots"
    assert base.log_messages.count(warning) == 1, base.log_messages
    assert coordinator.slot_map == {"gecloud:ce1111g111": 0}, coordinator.slot_map
    assert base.set_calls == calls, base.set_calls[len(calls):]
    print("PASS: unreported source left out")
    return 0


def test_configure_fills_in_on_second_report():
    """A record that gains a binding later gets exactly that binding written (Review Focus 2)."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", {"automatic": True, "inverters": [_record("ce1111g111", {"soc_percent": _ro("sensor.ce1111g111_soc")})]})
    first = len(base.set_calls)
    assert base.args["soc_percent"] == ["sensor.ce1111g111_soc"] and "charge_rate" not in base.args
    entities = {"soc_percent": _ro("sensor.ce1111g111_soc"), "charge_rate": _rw("number.ce1111g111_rate")}
    coordinator.report("gecloud", {"automatic": True, "inverters": [_record("ce1111g111", entities)]})
    assert _pairs(base.set_calls[first:]) == [("charge_rate", 0)], base.set_calls[first:]
    assert base.args["charge_rate"] == ["number.ce1111g111_rate"]
    print("PASS: configure fills in on a second report")
    return 0


def test_checker_called_only_on_change():
    """check_automatic_config gets the record slots after a pass that wrote, and is not called after one that did not."""
    gecloud = _GECloud()
    base, registry, coordinator = _setup(components={"gecloud": gecloud})
    coordinator.allow_automatic("gecloud")
    assert gecloud.checks == []
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert gecloud.checks == [[0]], gecloud.checks
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert gecloud.checks == [[0]], gecloud.checks
    # A PV-only record has no slot and is left out; the slots follow report order
    report = _report(["a11111", "b22222"])
    report["inverters"].insert(1, _record("pv3333", {"pv_power": _ro("sensor.pv3333_pv_power")}, inverter_type=None))
    coordinator.report("gecloud", report)
    assert gecloud.checks == [[0], [0, 1]], gecloud.checks
    print("PASS: checker called only on change")
    return 0


def test_checker_failure_is_logged():
    """A checker that raises is logged; the pass's writes stand and configure() does not raise."""
    gecloud = _GECloud()

    def broken(slots):
        """Fail."""
        raise ValueError("boom")

    gecloud.check_automatic_config = broken
    base, registry, coordinator = _setup(components={"gecloud": gecloud})
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert base.args["inverter_type"] == ["GEC"]
    assert "Warn: Coordinator: gecloud automatic configuration check failed: boom" in base.log_messages, base.log_messages
    assert coordinator.slot_map == {"gecloud:ce1111g111": 0}
    print("PASS: checker failure is logged")
    return 0


def test_unconverted_component_warns():
    """A component configuring inverters itself alongside an opted-in one is warned about, once."""
    base, registry, coordinator = _setup(components={"gecloud": _GECloud(), "fox": object()})
    coordinator.report("fox", {"automatic": True, "inverters": [{"device_id": "fox:f11111", "inverter_type": "FoxCloud", "hardware_ids": {"serial": "f11111"}}]})
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    warning = "Warn: Coordinator: fox configures inverters itself and is not yet handled by discovery - running it alongside gecloud is not supported"
    assert warning in base.log_messages, base.log_messages
    coordinator.report("gecloud", _report(["ce1111g111", "ce2222g222"]))
    assert base.log_messages.count(warning) == 1, base.log_messages
    # fox's records are not configured
    assert "fox:f11111" not in coordinator.slot_map
    print("PASS: unconverted component warns")
    return 0


def test_late_unconverted_component_warns():
    """A component reporting automatic inverters after the opted-in one is configured is warned about on the next pass, once."""
    base, registry, coordinator = _setup(components={"gecloud": _GECloud(), "fox": object()})
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    calls = list(base.set_calls)
    coordinator.report("fox", {"automatic": True, "inverters": [{"device_id": "fox:f11111", "inverter_type": "FoxCloud", "hardware_ids": {"serial": "f11111"}}]})
    coordinator.configure()
    warning = "Warn: Coordinator: fox configures inverters itself and is not yet handled by discovery - running it alongside gecloud is not supported"
    assert base.log_messages.count(warning) == 1, base.log_messages
    assert base.set_calls == calls, base.set_calls[len(calls):]  # the pass that warned wrote nothing
    lines = len(base.log_messages)
    coordinator.configure()
    assert base.log_messages[lines:] == [], base.log_messages[lines:]
    print("PASS: late unconverted component warns")
    return 0


def test_modules_import_standalone():
    """bindings, coordinator and the reporters that import them each import in a fresh process, as the standalone CLIs do."""
    predbat_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    failures = 0
    for module in ("bindings", "coordinator", "components", "gecloud", "fox"):
        result = subprocess.run([sys.executable, "-c", "import " + module], cwd=predbat_dir, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print("ERROR: import {} in a fresh process exited {}:\n{}".format(module, result.returncode, result.stderr))
            failures += 1
    assert failures == 0, "{} module(s) failed to import standalone".format(failures)
    print("PASS: modules import standalone")
    return 0


def test_unconverted_without_automatic_is_quiet():
    """A component reporting inverters with automatic false, or reporting no driven inverter, is not warned about."""
    base, registry, coordinator = _setup(components={"gecloud": _GECloud()})
    coordinator.report("givtcp", {"automatic": False, "inverters": [{"device_id": "givtcp:s11111", "inverter_type": "GE"}]})
    coordinator.report("solar", {"automatic": True, "forecasts": [{"device_id": "solar:site", "kind": "pv"}]})
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert not [line for line in base.log_messages if "configures inverters itself" in line], base.log_messages
    print("PASS: unconverted without automatic is quiet")
    return 0


def test_stop_keeps_slots():
    """A stopped component keeps its slots: nothing is written, and the checker is not looked up on None."""
    gecloud = _GECloud()
    base, registry, coordinator = _setup(components={"gecloud": gecloud})
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    slots = dict(coordinator.slot_map)
    calls = list(base.set_calls)
    registry.components["gecloud"] = None
    coordinator.configure()
    assert base.set_calls == calls, base.set_calls
    assert coordinator.slot_map == slots, coordinator.slot_map
    # Something else overwrites an entry while it is stopped: the pass repairs it, with no checker to call
    base.args["charge_rate"] = ["number.other"]
    coordinator.configure()
    assert base.args["charge_rate"] == ["number.predbat_gecloud_ce1111g111_battery_charge_rate"]
    assert coordinator.slot_map == slots
    assert gecloud.checks == [[0]], gecloud.checks
    assert not [line for line in base.log_messages if "failed" in line], base.log_messages
    print("PASS: stop keeps slots")
    return 0


def test_configure_never_raises():
    """set_arg raising: configure() returns, logs the failure and leaves args as they were."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    base.fail_set_arg = RuntimeError("no writes today")
    before = copy.deepcopy(base.args)
    bindings = copy.deepcopy(coordinator.bindings)
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.args == before, base.args
    assert "Warn: Coordinator: automatic configuration failed: no writes today" in base.log_messages, base.log_messages
    assert coordinator.bindings == bindings and coordinator.slot_map == {}, coordinator.bindings  # the failed pass's bindings are not kept
    # Recovered: the next pass configures normally
    base.fail_set_arg = None
    coordinator.configure()
    assert base.args["inverter_type"] == ["GEC"]
    print("PASS: configure never raises")
    return 0


def test_configure_failure_rolls_back_partial_writes():
    """A set_arg that fails part way through a pass: the writes already made in that pass are undone."""
    base, registry, coordinator = _setup(args={"num_inverters": 1})
    coordinator.allow_automatic("gecloud")
    real = base.set_arg
    count = {"n": 0}

    def flaky(arg, value, index=None):
        """Fail on the third write."""
        count["n"] += 1
        if count["n"] == 3:
            raise RuntimeError("third write")
        real(arg, value, index=index)

    base.set_arg = flaky
    before = copy.deepcopy(base.args)
    coordinator.report("gecloud", _report(["ce1111g111"]))
    assert count["n"] == 3
    assert base.args == before, base.args
    assert "Warn: Coordinator: automatic configuration failed: third write" in base.log_messages, base.log_messages
    print("PASS: configure failure rolls back partial writes")
    return 0


def test_configure_is_serialised():
    """A second configure() does not start building or replaying until the first has returned (Review Focus 3)."""
    events = []

    class _Checker(_GECloud):
        """Records its check, the last step of a pass, in the shared event list."""

        def check_automatic_config(self, slots):
            """Record which thread finished a pass."""
            events.append(("check", threading.current_thread().name))

    base, registry, coordinator = _setup(components={"gecloud": _Checker()})
    coordinator.allow_automatic("gecloud")
    release = threading.Event()
    blocked = threading.Event()
    real_set_arg = base.set_arg
    real_build = coordinator_module.build_bindings

    def blocking_set_arg(arg, value, index=None):
        """Block the first writer until released."""
        events.append(("set_arg", threading.current_thread().name))
        if threading.current_thread().name == "first":
            blocked.set()
            release.wait(5)
        real_set_arg(arg, value, index=index)

    def recording_build(*args, **kwargs):
        """Record which thread entered a pass."""
        events.append(("build", threading.current_thread().name))
        return real_build(*args, **kwargs)

    def first():
        """Report, which configures and blocks in set_arg."""
        coordinator.report("gecloud", _report(["ce1111g111"]))

    def second():
        """Configure while the first pass is blocked."""
        coordinator.configure()

    base.set_arg = blocking_set_arg
    coordinator_module.build_bindings = recording_build
    try:
        thread_one = threading.Thread(target=first, name="first")
        thread_one.start()
        assert blocked.wait(5), "first pass never reached set_arg"
        thread_two = threading.Thread(target=second, name="second")
        thread_two.start()
        time.sleep(0.2)
        assert thread_two.is_alive(), "second configure() must wait for the first"
        assert ("build", "second") not in events, events
        release.set()
        thread_one.join(5)
        thread_two.join(5)
        assert not thread_one.is_alive() and not thread_two.is_alive()
    finally:
        release.set()
        coordinator_module.build_bindings = real_build
        base.set_arg = real_set_arg
    # The first pass ran start to finish (build, every write, its check) before the second built anything
    second_build = events.index(("build", "second"))
    assert events[:second_build][0] == ("build", "first") and events[second_build - 1] == ("check", "first"), events
    assert all(name == "first" for _, name in events[:second_build]), events
    assert events[second_build:] == [("build", "second")], events  # nothing left to write
    assert base.args["charge_rate"] == ["number.predbat_gecloud_ce1111g111_battery_charge_rate"]
    print("PASS: configure is serialised")
    return 0


def test_catalogue_carries_slots():
    """The catalogue gives each configured inverter its slot and carries the bindings in observations."""
    base, registry, coordinator = _setup()
    assert "bindings" not in coordinator.assemble()["observations"]
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    catalogue = coordinator.assemble()
    assert catalogue["inverters"][0]["slot"] == 0, catalogue["inverters"][0]
    assert catalogue["observations"]["bindings"]["inverters"][0]["device_id"] == "gecloud:ce1111g111"
    # The catalogue's copy is not the coordinator's own
    catalogue["observations"]["bindings"]["inverters"][0]["device_id"] = "changed"
    assert coordinator.bindings["inverters"][0]["device_id"] == "gecloud:ce1111g111"
    # An unconfigured record carries no slot
    coordinator.report("other", {"inverters": [{"device_id": "other:x1", "inverter_type": "GE"}]})
    entries = {entry["device_id"]: entry for entry in coordinator.assemble()["inverters"]}
    assert "slot" not in entries["other:x1"], entries
    print("PASS: catalogue carries slots")
    return 0


def test_takeover_and_hole_warnings_logged():
    """build_bindings warnings reach the log prefixed, each distinct text once."""
    apps = {"num_inverters": 3, "inverter_type": ["GE", "", "GE"], "charge_rate": ["number.a_charge_rate", "number.b_charge_rate", "number.givtcp_ce1111g111_charge_rate"]}
    base, registry, coordinator = _setup(args=apps)
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    takeover = "Warn: Coordinator: Inverter slot 2: gecloud inverter ce1111g111 is also configured manually in apps.yaml - discovery is taking over this slot"
    hole = "Warn: Coordinator: Inverter slot 1 is empty below a configured inverter - it has no inverter_type and Predbat will stop on it"
    assert takeover in base.log_messages, base.log_messages
    assert hole in base.log_messages, base.log_messages
    assert coordinator.slot_map == {"gecloud:ce1111g111": 2}
    coordinator.report("gecloud", _report(["ce1111g111", "ce2222g222"]))
    assert base.log_messages.count(takeover) == 1 and base.log_messages.count(hole) == 1, base.log_messages
    print("PASS: takeover and hole warnings logged")
    return 0


def test_summed_lists_keep_resolved_patterns():
    """A manual inverter's resolved re: summed entry is kept: no unresolved pattern is ever written (amendment 2)."""
    raw = {"num_inverters": 1, "inverter_type": ["GE"], "charge_rate": ["number.givtcp_sa1_charge_rate"], "load_today": ["re:sensor.givtcp_(.+)_load_today"]}
    live = copy.deepcopy(raw)
    live["load_today"] = ["sensor.givtcp_sa1_load_today"]
    base, registry, coordinator = _setup(args=live, apps_yaml=raw)
    coordinator.allow_automatic("gecloud")
    entities = _entities("ce1111g111")
    entities["load_today"] = _ro("sensor.predbat_gecloud_ce1111g111_load_today")
    coordinator.report("gecloud", {"automatic": True, "inverters": [_record("ce1111g111", entities)]})
    assert base.args["load_today"] == ["sensor.givtcp_sa1_load_today", "sensor.predbat_gecloud_ce1111g111_load_today"], base.args["load_today"]
    assert coordinator.slot_map == {"gecloud:ce1111g111": 1}
    written = [value for _, value, _ in base.set_calls]
    assert not [value for value in written if "re:" in str(value)], base.set_calls
    calls = list(base.set_calls)
    coordinator.report("gecloud", {"automatic": True, "inverters": [_record("ce1111g111", copy.deepcopy(entities))]})
    coordinator.configure()
    assert base.set_calls == calls, base.set_calls[len(calls):]
    print("PASS: summed lists keep resolved patterns")
    return 0


def test_baseline_drops_unresolved_patterns():
    """A re: pattern still unresolved when the baseline is taken counts as unset: a list entry becomes None, a scalar drops the key, and none is ever written."""
    raw = {
        "num_inverters": 1,
        "inverter_type": ["GE"],
        "charge_rate": ["number.givtcp_sa1_charge_rate"],
        "load_today": ["re:sensor.givtcp_(.+)_load_today", "sensor.extra_load_today"],
        "pv_today": "re:sensor.givtcp_(.+)_pv_today",
    }
    base, registry, coordinator = _setup(args=raw, apps_yaml=raw)
    coordinator.allow_automatic("gecloud")
    baseline = coordinator.args_baseline
    assert baseline["load_today"] == [None, "sensor.extra_load_today"], baseline
    assert "pv_today" not in baseline, baseline
    assert coordinator_module.capture_baseline({"battery_temperature": "re:sensor.givtcp_(.+)_temperature", "soc_percent": ("re:x", "sensor.b_soc"), "inverter_limit_charge": [1]}) == {"soc_percent": [None, "sensor.b_soc"]}
    assert baseline["charge_rate"] == ["number.givtcp_sa1_charge_rate"] and baseline["inverter_type"] == ["GE"], baseline
    assert set(baseline) <= BINDING_SETTINGS | {"inverter_type"}, sorted(baseline)
    baseline["charge_rate"].append("changed")
    assert raw["charge_rate"] == ["number.givtcp_sa1_charge_rate"] and base.args["charge_rate"] == ["number.givtcp_sa1_charge_rate"]  # a deep copy
    del baseline["charge_rate"][1]
    entities = dict(_entities("ce1111g111"), load_today=_ro("sensor.predbat_gecloud_ce1111g111_load_today"))
    coordinator.report("gecloud", {"automatic": True, "inverters": [_record("ce1111g111", entities)]})
    assert coordinator.slot_map == {"gecloud:ce1111g111": 1}, coordinator.slot_map
    assert base.args["load_today"] == ["sensor.predbat_gecloud_ce1111g111_load_today", "sensor.extra_load_today"], base.args["load_today"]
    assert not [value for _, value, _ in base.set_calls if "re:" in str(value)], base.set_calls
    print("PASS: baseline drops unresolved patterns")
    return 0


def test_baseline_expands_templates():
    """
    The baseline holds {name} templates expanded as get_arg reads them, each value keeping its shape: a list stays a list, a scalar
    a scalar, an absent key absent, an entry with no template exactly as it was. A template that names a still-unresolved re:
    pattern, or a name args do not have, stays raw - expanding it would give no entity.
    """
    raw = {
        "geserial": "ce1111g111",
        "tcp_serial": "re:sensor.givtcp_(.+)_soc_kwh",
        "num_inverters": 1,
        "inverter_type": ["GEC"],
        "charge_rate": ["number.ge_inverter_{geserial}_battery_charge_power"],
        "soc_percent": "sensor.ge_inverter_{geserial}_battery_soc",
        "load_today": ["sensor.ge_inverter_{geserial}_consumption_today", "sensor.extra_load", None],
        "pv_today": ["sensor.givtcp_{tcp_serial}_pv_today"],
        "import_today": ["sensor.ge_inverter_{missing_name}_grid_import_today"],
        "inverter_limit": [3600],
        "battery_power_invert": True,
        "reserve": ["number.my_reserve"],
    }
    base = _TemplatingBase(args=raw)
    coordinator = _Components(base, {"gecloud": _GECloud()}).coordinator
    coordinator.allow_automatic("gecloud")
    baseline = coordinator.args_baseline
    assert baseline["charge_rate"] == ["number.ge_inverter_ce1111g111_battery_charge_power"], baseline["charge_rate"]
    assert baseline["soc_percent"] == "sensor.ge_inverter_ce1111g111_battery_soc", baseline["soc_percent"]
    assert baseline["load_today"] == ["sensor.ge_inverter_ce1111g111_consumption_today", "sensor.extra_load", None], baseline["load_today"]
    assert baseline["pv_today"] == ["sensor.givtcp_{tcp_serial}_pv_today"], baseline["pv_today"]
    assert baseline["import_today"] == ["sensor.ge_inverter_{missing_name}_grid_import_today"], baseline["import_today"]
    assert baseline["inverter_limit"] == [3600] and baseline["battery_power_invert"] is True and baseline["reserve"] == ["number.my_reserve"], baseline
    assert baseline["inverter_type"] == ["GEC"] and "export_today" not in baseline and "geserial" not in baseline, baseline
    assert base.args == raw  # args themselves keep the templates
    # Without a reader, or with one that gives nothing, the templates are copied as they are
    assert coordinator_module.capture_baseline(raw)["charge_rate"] == raw["charge_rate"]
    assert coordinator_module.capture_baseline(raw, lambda setting: None)["soc_percent"] == raw["soc_percent"]
    print("PASS: baseline expands templates")
    return 0


def test_release_compacts():
    """A device released below a used slot is closed up: the one above moves down, the move is warned about once, and later passes write nothing."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["a11111", "b22222"]))
    assert coordinator.slot_map == {"gecloud:a11111": 0, "gecloud:b22222": 1}, coordinator.slot_map
    coordinator.report("gecloud", _report(["b22222"]))
    assert coordinator.slot_map == {"gecloud:b22222": 0}, coordinator.slot_map
    assert base.args["num_inverters"] == 1 and base.args["inverter_type"] == ["GEC"], (base.args["num_inverters"], base.args["inverter_type"])
    assert base.args["charge_rate"] == ["number.predbat_gecloud_b22222_battery_charge_rate"], base.args["charge_rate"]
    moved = "Warn: Coordinator: Inverter slot 1 moved to slot 0: gecloud inverter gecloud:b22222 closes the gap left by a device that is no longer reported"
    assert base.log_messages.count(moved) == 1, base.log_messages
    assert not [line for line in base.log_messages if "is empty below" in line], base.log_messages
    calls = list(base.set_calls)
    coordinator.configure()
    coordinator.configure()
    assert base.set_calls == calls, base.set_calls[len(calls):]
    print("PASS: release compacts")
    return 0


def test_assemble_slots_follow_bindings():
    """assemble() takes each record's slot from the bindings it reads, never from a slot map out of step with them."""
    base, registry, coordinator = _setup()
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.slot_map = {"gecloud:ce1111g111": 3}  # stale, as a reader between two separate assignments could once see
    assert coordinator.assemble()["inverters"][0]["slot"] == 0
    print("PASS: assemble slots follow bindings")
    return 0


class _Bare:
    """A base with only log and args, as the standalone harnesses build."""

    def __init__(self):
        """Nothing but a log list and args."""
        self.args = {"num_inverters": 1}
        self.messages = []

    def log(self, message):
        """Record a log line."""
        self.messages.append(message)


def test_bare_base_is_inert():
    """On a base with no get_arg, args_from_apps_yaml or components, nothing is configured and nothing raises (amendment 4)."""
    base = _Bare()
    coordinator = Coordinator(base)
    assert coordinator.allow_automatic("gecloud") is False  # a pass could not configure this base, so it is not accepted
    assert coordinator.automatic == {}
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.args == {"num_inverters": 1}
    assert not [line for line in base.messages if "failed" in line], base.messages
    # get_arg present, but no snapshot, no set_arg and no registry: still nothing
    base.get_arg = lambda name, default=None, **kwargs: base.args.get(name, default)
    assert coordinator.allow_automatic("gecloud") is False
    coordinator.report("gecloud", _report(["ce1111g111", "ce2222g222"]))
    coordinator.configure()
    assert base.args == {"num_inverters": 1}
    assert coordinator.bindings is None
    # Opted in by hand on that base, a pass still finds nothing to configure with and neither writes nor raises
    coordinator.automatic["gecloud"] = {"skip_settings": frozenset(), "keep_user_settings": frozenset(), "include_pv_only": False}
    coordinator.configure()
    assert base.args == {"num_inverters": 1} and coordinator.bindings is None
    assert not [line for line in base.messages if "failed" in line], base.messages
    print("PASS: bare base is inert")
    return 0


def test_allow_automatic_config_wrapper():
    """ComponentBase.allow_automatic_config() reaches the coordinator, and is False with no base, registry or coordinator."""
    from component_base import ComponentBase

    class _Component(ComponentBase):
        """A concrete ComponentBase, built without __init__ as the component test mocks are."""

        def initialize(self, **kwargs):
            """Nothing to set up."""

    base, registry, coordinator = _setup()
    component = _Component.__new__(_Component)
    assert component.allow_automatic_config() is False  # no base at all
    component.base = base
    component.component_name = "gecloud"
    assert component.allow_automatic_config(skip_settings=("load_today",), keep_user_settings=("inverter_mode",), include_pv_only=True) is True
    assert coordinator.automatic["gecloud"] == {"skip_settings": frozenset({"load_today"}), "keep_user_settings": frozenset({"inverter_mode"}), "include_pv_only": True}
    component.base = _Bare()
    assert component.allow_automatic_config() is False
    print("PASS: allow_automatic_config wrapper")
    return 0


# ---------------------------------------------------------------------------------------------
# The hybrid site rule: inverter_hybrid from every automatic report's driven records
# ---------------------------------------------------------------------------------------------

HYBRID_ENTITY = "switch.predbat_inverter_hybrid"


class _HAInterface:
    """Stand-in for Predbat's HA interface: records each set_state_external call and, unless told not to, applies it to args."""

    def __init__(self, base, apply=True, fail=None):
        """Bind to base; apply says whether a write changes args["inverter_hybrid"], fail is an exception to raise instead."""
        self.base = base
        self.apply = apply
        self.fail = fail
        self.calls = []

    async def set_state_external(self, entity_id, state, attributes=None):
        """Record the write, then raise or apply it as configured."""
        self.calls.append((entity_id, state))
        if self.fail is not None:
            raise self.fail
        if self.apply:
            self.base.args["inverter_hybrid"] = state


def _hybrid_setup(current=True, apply=True, fail=None):
    """(base, coordinator, ha) with inverter_hybrid starting at current and the recording HA interface on base."""
    base, registry, coordinator = _setup(args={"inverter_hybrid": current})
    ha = _HAInterface(base, apply=apply, fail=fail)
    base.ha_interface = ha
    return base, coordinator, ha


def _hybrid_report(records, automatic=True):
    """A report of (device_id, hybrid, inverter_type) records; hybrid None leaves the field out, inverter_type None makes a PV-only record."""
    inverters = []
    for device_id, hybrid, inverter_type in records:
        record = {"device_id": device_id, "entities": _entities(device_id.split(":")[-1])}
        if inverter_type:
            record["inverter_type"] = inverter_type
        if hybrid is not None:
            record["hybrid"] = hybrid
        inverters.append(record)
    return {"automatic": automatic, "inverters": inverters}


def _hybrid_lines(base):
    """The coordinator's inverter_hybrid log lines."""
    return [line for line in base.log_messages if "inverter_hybrid" in line]


def test_hybrid_one_false_turns_off():
    """One automatic driven record saying not hybrid turns inverter_hybrid off, even beside a hybrid one; opted in, configure() does not write it twice."""
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", True, "GEC")]))
    assert ha.calls == [], ha.calls  # all known records are hybrid and the switch is already on
    coordinator.report("alphaess", _hybrid_report([("alphaess:b", False, "AlphaESS")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    assert coordinator.hybrid_decision() == (False, ["alphaess:b"]), coordinator.hybrid_decision()
    coordinator.configure()
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    print("PASS: hybrid one false turns off")
    return 0


def test_hybrid_all_true_turns_on():
    """Every known driven record hybrid, the switch off: one write turning it on."""
    base, coordinator, ha = _hybrid_setup(current=False)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", True, "GEC"), ("gecloud:b", True, "GEC")]))
    assert ha.calls == [(HYBRID_ENTITY, True)], ha.calls
    assert base.args["inverter_hybrid"] is True
    print("PASS: hybrid all true turns on")
    return 0


def test_hybrid_nothing_known_untouched():
    """Records that do not say: the switch is left alone, either way round."""
    for current in (True, False):
        base, coordinator, ha = _hybrid_setup(current=current)
        coordinator.report("gecloud", _hybrid_report([("gecloud:a", None, "GEC"), ("gecloud:b", None, "GEC")]))
        assert ha.calls == [], ha.calls
        assert coordinator.hybrid_decision() == (None, []), coordinator.hybrid_decision()
        assert _hybrid_lines(base) == [], base.log_messages
    print("PASS: hybrid nothing known untouched")
    return 0


def test_hybrid_ignores_pv_only():
    """A PV-only record (no inverter_type) does not count: alone it decides nothing, beside a hybrid driven record it does not turn the switch off."""
    base, coordinator, ha = _hybrid_setup(current=False)
    coordinator.report("gecloud", _hybrid_report([("gecloud:pv", False, None)]))
    assert ha.calls == [], ha.calls
    coordinator.report("gecloud", _hybrid_report([("gecloud:pv", False, None), ("gecloud:a", True, "GEC")]))
    assert ha.calls == [(HYBRID_ENTITY, True)], ha.calls
    print("PASS: hybrid ignores pv only")
    return 0


def test_hybrid_ignores_non_automatic_report():
    """A report with automatic false, or with no automatic at all, does not change Predbat's settings."""
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")], automatic=False))
    assert ha.calls == [], ha.calls
    coordinator.report("givtcp", {"inverters": [{"device_id": "givtcp:b", "inverter_type": "GE", "hybrid": False}]})
    assert ha.calls == [], ha.calls
    assert coordinator.hybrid_decision() == (None, []), coordinator.hybrid_decision()
    print("PASS: hybrid ignores non automatic report")
    return 0


def test_hybrid_no_write_when_unchanged():
    """The decision already matches the switch: nothing is written or logged."""
    for value in (True, False):
        base, coordinator, ha = _hybrid_setup(current=value)
        coordinator.report("gecloud", _hybrid_report([("gecloud:a", value, "GEC")]))
        assert ha.calls == [], ha.calls
        assert _hybrid_lines(base) == [], base.log_messages
    print("PASS: hybrid no write when unchanged")
    return 0


def _deferred_writes(base):
    """Give base a create_task that holds each coroutine instead of running it; returns the list they are held in."""
    held = []

    def create_task(coro, name="TaskCreate"):
        """Hold the coroutine, as a task not yet run."""
        held.append(coro)

    base.create_task = create_task
    return held


def _report_in_loop(coordinator, name, report):
    """File a report from inside a running event loop, as a component's run() does, so the write is handed to base.create_task."""

    async def component_run():
        """A component's run() filing its report."""
        coordinator.report(name, report)

    run_async(component_run())


def _land(coro):
    """Run one held write to completion on a loop of its own."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def test_hybrid_write_not_repeated_while_pending():
    """A write still in flight is not sent again by a second changed report; once it lands it is no longer pending."""
    base, coordinator, ha = _hybrid_setup(current=True)
    held = _deferred_writes(base)
    _report_in_loop(coordinator, "gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    _report_in_loop(coordinator, "alphaess", _hybrid_report([("alphaess:b", False, "AlphaESS")]))
    assert len(held) == 1 and ha.calls == [], (held, ha.calls)
    assert coordinator.hybrid_pending is False
    _land(held.pop())
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    assert base.args["inverter_hybrid"] is False and coordinator.hybrid_pending is None
    print("PASS: hybrid write not repeated while pending")
    return 0


def test_hybrid_user_override_corrected():
    """The write lands, the user turns the switch back, and the very next changed report sets it again."""
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    assert ha.calls == [(HYBRID_ENTITY, False)] and base.args["inverter_hybrid"] is False, ha.calls
    assert coordinator.hybrid_pending is None
    base.args["inverter_hybrid"] = True
    coordinator.report("alphaess", _hybrid_report([("alphaess:b", None, "AlphaESS")]))
    assert ha.calls == [(HYBRID_ENTITY, False), (HYBRID_ENTITY, False)], ha.calls
    assert base.args["inverter_hybrid"] is False
    # A write that completes without changing the setting is not pending either: the next changed report sends it again
    base, coordinator, ha = _hybrid_setup(current=True, apply=False)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    coordinator.report("alphaess", _hybrid_report([("alphaess:b", None, "AlphaESS")]))
    assert ha.calls == [(HYBRID_ENTITY, False), (HYBRID_ENTITY, False)], ha.calls
    print("PASS: hybrid user override corrected")
    return 0


def test_hybrid_old_failure_keeps_newer_pending():
    """An older write failing after a newer one was scheduled leaves the newer value pending; the newer one landing clears it."""
    base, coordinator, ha = _hybrid_setup(current=True)
    held = _deferred_writes(base)
    _report_in_loop(coordinator, "gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    assert coordinator.hybrid_pending is False and len(held) == 1
    # The setting moves to False by other means, then the decision becomes True: a newer write, of True, is scheduled
    base.args["inverter_hybrid"] = False
    _report_in_loop(coordinator, "gecloud", _hybrid_report([("gecloud:a", True, "GEC")]))
    assert coordinator.hybrid_pending is True and len(held) == 2
    ha.fail = RuntimeError("late")
    _land(held.pop(0))  # the older write, of False, fails
    assert coordinator.hybrid_pending is True, coordinator.hybrid_pending
    assert [line for line in base.log_messages if line.startswith("Warn:")] == ["Warn: Coordinator: could not set inverter_hybrid: late"], base.log_messages
    ha.fail = None
    _land(held.pop(0))
    assert base.args["inverter_hybrid"] is True and coordinator.hybrid_pending is None
    print("PASS: hybrid old failure keeps newer pending")
    return 0


def test_hybrid_runs_without_opt_in():
    """A component that never called allow_automatic() still drives the rule, and an unchanged report does not re-run it."""
    base, coordinator, ha = _hybrid_setup(current=True)
    assert coordinator.automatic == {}
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    assert base.set_calls == [], base.set_calls  # no slot configuration ran
    base.args["inverter_hybrid"] = True
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))  # unchanged
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    print("PASS: hybrid runs without opt in")
    return 0


def test_hybrid_never_raises():
    """The HA write raising: no exception, one Warn line, and the pending value clears so a later changed report tries again."""
    base, coordinator, ha = _hybrid_setup(current=True, fail=RuntimeError("boom"))
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    warnings = [line for line in base.log_messages if line.startswith("Warn:")]
    assert warnings == ["Warn: Coordinator: could not set inverter_hybrid: boom"], base.log_messages
    assert coordinator.hybrid_pending is None
    assert not [line for line in base.log_messages if line.startswith("Error:")], base.log_messages
    # Scheduling itself failing is caught the same way
    base, coordinator, ha = _hybrid_setup(current=True)
    base.prefix = None
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    assert ha.calls == [], ha.calls  # no prefix: a thin base, nothing to do
    base.prefix = "predbat"

    def broken_get_arg(name, default=None, **kwargs):
        """A get_arg that fails."""
        raise ValueError("no args")

    base.get_arg = broken_get_arg
    coordinator.report("gecloud", _hybrid_report([("gecloud:b", False, "GEC")]))
    assert ha.calls == [], ha.calls
    assert [line for line in base.log_messages if line.startswith("Warn:")] == ["Warn: Coordinator: could not set inverter_hybrid: no args"], base.log_messages
    print("PASS: hybrid never raises")
    return 0


def test_hybrid_log_line():
    """The exact line naming the deciding records, for off and for on."""
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC"), ("gecloud:b", True, "GEC")]))
    assert _hybrid_lines(base) == ["Coordinator: inverter_hybrid set off - gecloud:a reports it is not a hybrid inverter"], base.log_messages
    base, coordinator, ha = _hybrid_setup(current=False)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", True, "GEC"), ("gecloud:b", True, "GEC")]))
    assert _hybrid_lines(base) == ["Coordinator: inverter_hybrid set on - gecloud:a, gecloud:b report they are hybrid inverters"], base.log_messages
    base, coordinator, ha = _hybrid_setup(current=False)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", True, "GEC")]))
    assert _hybrid_lines(base) == ["Coordinator: inverter_hybrid set on - gecloud:a reports it is a hybrid inverter"], base.log_messages
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC"), ("gecloud:b", False, "GEC")]))
    assert _hybrid_lines(base) == ["Coordinator: inverter_hybrid set off - gecloud:a, gecloud:b report they are not hybrid inverters"], base.log_messages
    print("PASS: hybrid log line")
    return 0


def test_hybrid_thin_base_inert():
    """No ha_interface, get_arg or prefix: the rule does nothing and logs nothing."""
    base = _Bare()
    coordinator = Coordinator(base)
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    coordinator.apply_hybrid()
    assert not [line for line in base.messages if "inverter_hybrid" in line], base.messages
    base, registry, coordinator = _setup(args={"inverter_hybrid": True})  # get_arg and prefix but no ha_interface
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC")]))
    assert _hybrid_lines(base) == [] and coordinator.hybrid_pending is None, base.log_messages
    print("PASS: hybrid thin base inert")
    return 0


def test_hybrid_scheduled_from_a_running_loop():
    """Called inside a running event loop (a component's thread) the write is handed to base.create_task, or to a thread of its own without one - never awaited inline."""
    base, coordinator, ha = _hybrid_setup(current=True)
    threads = []

    def create_task(coro, name="TaskCreate"):
        """hass.Hass.create_task: run the coroutine to completion on a new thread."""
        thread = threading.Thread(target=lambda: asyncio.run(coro), name=name)
        thread.start()
        threads.append(thread)
        return thread

    base.create_task = create_task

    async def component_run(report):
        """A component's run(), filing its report from inside its loop."""
        coordinator.report("gecloud", report)
        return asyncio.get_running_loop()

    run_async(component_run(_hybrid_report([("gecloud:a", False, "GEC")])))
    for thread in threads:
        thread.join(5)
    assert len(threads) == 1, threads
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    assert base.args["inverter_hybrid"] is False
    # No create_task on the base (the command-line harness): the coordinator runs the write on its own thread
    del base.create_task
    before = set(threading.enumerate())
    run_async(component_run(_hybrid_report([("gecloud:a", True, "GEC")])))
    for thread in set(threading.enumerate()) - before:
        thread.join(5)
    assert ha.calls == [(HYBRID_ENTITY, False), (HYBRID_ENTITY, True)], ha.calls
    assert base.args["inverter_hybrid"] is True
    print("PASS: hybrid scheduled from a running loop")
    return 0


def test_hybrid_lost_value_keeps_last_known():
    """A mixed fleet whose not-hybrid record loses its value on alternate reports: one write (off) and no flapping."""
    base, coordinator, ha = _hybrid_setup(current=True)
    for rai in (False, None, False, None, False, None):
        coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud"), ("solis:B", rai, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    assert base.args["inverter_hybrid"] is False
    assert coordinator.hybrid_decision() == (False, ["solis:B"]), coordinator.hybrid_decision()
    assert _hybrid_lines(base) == ["Coordinator: inverter_hybrid set off - solis:B reports it is not a hybrid inverter"], base.log_messages
    print("PASS: hybrid lost value keeps last known")
    return 0


def test_hybrid_removed_device_forgotten():
    """A device no longer in its source's report stops counting: with only the hybrid device left the switch goes on, and the removed one does not come back."""
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud"), ("solis:B", False, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, False), (HYBRID_ENTITY, True)], ha.calls
    assert coordinator.hybrid_decision() == (True, ["solis:A"]), coordinator.hybrid_decision()
    # Back without a value: it was forgotten when it left, so it is unknown and the switch stays on
    coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud"), ("solis:B", None, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, False), (HYBRID_ENTITY, True)], ha.calls
    assert coordinator.hybrid_decision() == (True, ["solis:A"]), coordinator.hybrid_decision()
    # The same device_id under another source is a different device: one source's memory is not another's
    coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud"), ("solis:B", False, "SolisCloud")]))
    coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud")]))
    coordinator.report("other", _hybrid_report([("solis:B", None, "SolisCloud")]))
    assert coordinator.hybrid_decision() == (True, ["solis:A"]), coordinator.hybrid_decision()
    print("PASS: hybrid removed device forgotten")
    return 0


def test_hybrid_never_known_stays_unknown():
    """A device whose value was never known stays unknown beside one that is; a report without automatic still feeds nothing into the decision."""
    base, coordinator, ha = _hybrid_setup(current=False)
    for _ in range(3):
        coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud"), ("solis:B", None, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, True)], ha.calls
    assert coordinator.hybrid_decision() == (True, ["solis:A"]), coordinator.hybrid_decision()
    # A report with automatic false still decides nothing, with a remembered value or without
    base, coordinator, ha = _hybrid_setup(current=True)
    coordinator.report("solis", _hybrid_report([("solis:B", False, "SolisCloud")], automatic=False))
    coordinator.report("solis", _hybrid_report([("solis:B", None, "SolisCloud")], automatic=False))
    assert ha.calls == [] and coordinator.hybrid_decision() == (None, []), (ha.calls, coordinator.hybrid_decision())
    print("PASS: hybrid never known stays unknown")
    return 0


OVERRIDE_LINE = "Coordinator: inverter_hybrid set {} - inverter_hybrid_override in apps.yaml"


def _override_setup(override, current=True, apps=None):
    """(base, coordinator, ha) as _hybrid_setup, with inverter_hybrid_override (unless override is None) and any other apps.yaml settings in args."""
    args = dict(apps or {}, inverter_hybrid=current)
    if override is not None:
        args["inverter_hybrid_override"] = override
    base, registry, coordinator = _setup(args=args)
    ha = _HAInterface(base)
    base.ha_interface = ha
    return base, coordinator, ha


def _hybrid_gecloud_report(serials_hybrid):
    """A GE-Cloud-shaped automatic report of (serial, hybrid) records, bound to slots when GE Cloud is opted in; hybrid None leaves the field out."""
    records = []
    for serial, hybrid in serials_hybrid:
        record = _record(serial)
        if hybrid is not None:
            record["hybrid"] = hybrid
        records.append(record)
    return {"automatic": True, "inverters": records}


def test_hybrid_override_single_holds():
    """A single inverter_hybrid_override holds the switch whatever the records say, and a later report does not undo it."""
    for value, record_value in ((True, False), (False, True)):
        base, coordinator, ha = _override_setup(value, current=not value)
        coordinator.report("solis", _hybrid_report([("solis:A", record_value, "SolisCloud")]))
        assert ha.calls == [(HYBRID_ENTITY, value)], ha.calls
        assert _hybrid_lines(base) == [OVERRIDE_LINE.format("on" if value else "off")], base.log_messages
        assert coordinator.hybrid_decision() == (value, ["inverter_hybrid_override in apps.yaml"]), coordinator.hybrid_decision()
        # Later reports, still saying the opposite, write nothing; a hand-set value is put back on the next changed one
        coordinator.report("solis", _hybrid_report([("solis:A", record_value, "SolisCloud"), ("solis:B", record_value, "SolisCloud")]))
        assert ha.calls == [(HYBRID_ENTITY, value)], ha.calls
        base.args["inverter_hybrid"] = not value
        coordinator.report("solis", _hybrid_report([("solis:A", record_value, "SolisCloud")]))
        assert ha.calls == [(HYBRID_ENTITY, value), (HYBRID_ENTITY, value)], ha.calls
    # A recognisable boolean string counts as the boolean
    base, coordinator, ha = _override_setup("off", current=True)
    coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    # A site with no discovery report at all: the startup pass (configure(), nothing opted in) still applies it
    base, coordinator, ha = _override_setup(False, current=True, apps={"num_inverters": 1, "inverter_type": ["GE"]})
    coordinator.configure()
    assert ha.calls == [(HYBRID_ENTITY, False)] and base.args["inverter_hybrid"] is False, ha.calls
    coordinator.configure()
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    print("PASS: hybrid override single holds")
    return 0


def test_hybrid_override_list_discovered_slot():
    """A list entry replaces the verdict of the discovered record in its slot: overriding a False to True turns the switch on."""
    base, coordinator, ha = _override_setup([True], current=False)
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _hybrid_gecloud_report([("ce1111g111", False)]))
    assert coordinator.slot_map == {"gecloud:ce1111g111": 0}, coordinator.slot_map
    assert ha.calls == [(HYBRID_ENTITY, True)], ha.calls
    assert coordinator.hybrid_decision() == (True, ["inverter_hybrid_override[0]"]), coordinator.hybrid_decision()
    assert _hybrid_lines(base) == ["Coordinator: inverter_hybrid set on - inverter_hybrid_override[0] reports it is a hybrid inverter"], base.log_messages
    # Only that slot is overridden: a second record saying not hybrid, in slot 1, still turns the switch off
    coordinator.report("gecloud", _hybrid_gecloud_report([("ce1111g111", False), ("ce2222g222", False)]))
    assert coordinator.hybrid_decision() == (False, ["gecloud:ce2222g222"]), coordinator.hybrid_decision()
    assert ha.calls == [(HYBRID_ENTITY, True), (HYBRID_ENTITY, False)], ha.calls
    # The user's entry counts even when the slot's component reports with automatic off
    base, coordinator, ha = _override_setup([False], current=True)
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", dict(_hybrid_gecloud_report([("ce1111g111", True)]), automatic=False))
    assert coordinator.slot_map == {"gecloud:ce1111g111": 0}, coordinator.slot_map
    assert coordinator.hybrid_decision() == (False, ["inverter_hybrid_override[0]"]), coordinator.hybrid_decision()
    print("PASS: hybrid override list discovered slot")
    return 0


def test_hybrid_override_list_manual_slot():
    """A list entry for a manual slot counts as that inverter's verdict, with or without a configure pass; a free slot or one past the inverters counts for nothing."""
    manual = {"num_inverters": 1, "inverter_type": ["GE"], "charge_rate": ["number.a_charge_rate"]}
    base, coordinator, ha = _override_setup([False], current=True, apps=manual)
    coordinator.allow_automatic("gecloud")
    coordinator.report("gecloud", _hybrid_gecloud_report([("ce1111g111", True)]))
    assert coordinator.slot_map == {"gecloud:ce1111g111": 1}, coordinator.slot_map
    assert coordinator.bindings["inverters"][0]["origin"] == "manual", coordinator.bindings
    assert coordinator.hybrid_decision() == (False, ["inverter_hybrid_override[0]"]), coordinator.hybrid_decision()
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    # Nothing opted in, so no bindings: the apps.yaml inverters (num_inverters) are the manual slots
    base, coordinator, ha = _override_setup([True], current=False, apps=manual)
    coordinator.configure()
    assert coordinator.bindings is None
    assert ha.calls == [(HYBRID_ENTITY, True)], ha.calls
    assert coordinator.hybrid_decision() == (True, ["inverter_hybrid_override[0]"]), coordinator.hybrid_decision()
    # An entry past the configured inverters names no inverter and decides nothing
    base, coordinator, ha = _override_setup([None, False], current=True, apps=manual)
    coordinator.configure()
    assert ha.calls == [] and coordinator.hybrid_decision() == (None, []), (ha.calls, coordinator.hybrid_decision())
    print("PASS: hybrid override list manual slot")
    return 0


def test_hybrid_override_none_entries_leave_records():
    """None entries, a missing entry or an empty list leave each slot to its record."""
    for override in ([None], [None, None], []):
        base, coordinator, ha = _override_setup(override, current=True)
        coordinator.allow_automatic("gecloud")
        coordinator.report("gecloud", _hybrid_gecloud_report([("ce1111g111", False), ("ce2222g222", True)]))
        assert coordinator.hybrid_decision() == (False, ["gecloud:ce1111g111"]), (override, coordinator.hybrid_decision())
        assert ha.calls == [(HYBRID_ENTITY, False)], (override, ha.calls)
        assert not [line for line in base.log_messages if line.startswith("Warn:")], base.log_messages
    # The I1 fix holds with an override list in place: a record that loses its value keeps its last one
    base, coordinator, ha = _override_setup([None], current=True)
    for rai in (False, None, False, None):
        coordinator.report("solis", _hybrid_report([("solis:A", True, "SolisCloud"), ("solis:B", rai, "SolisCloud")]))
    assert ha.calls == [(HYBRID_ENTITY, False)], ha.calls
    print("PASS: hybrid override none entries leave records")
    return 0


def test_hybrid_override_not_the_switch(my_predbat):
    """The inverter_hybrid switch's own value (its CONFIG_ITEM, which wins in get_arg) is never read as the override."""
    # The stub base: args carry inverter_hybrid only
    base, coordinator, ha = _override_setup(None, current=False)
    assert coordinator.hybrid_decision() == (None, []), coordinator.hybrid_decision()
    coordinator.configure()
    assert ha.calls == [], ha.calls
    # The real PredBat get_arg: the switch's CONFIG_ITEM answers for inverter_hybrid, and nothing for the override key
    if my_predbat is None:
        print("PASS: hybrid override not the switch (stub base only, no PredBat given)")
        return 0
    saved_args = dict(my_predbat.args)
    saved_errors = copy.deepcopy(getattr(my_predbat, "arg_errors", {}))
    saved_warnings = copy.deepcopy(getattr(my_predbat, "arg_warnings", {}))
    try:
        my_predbat.args.pop("inverter_hybrid_override", None)
        assert isinstance(my_predbat.get_arg("inverter_hybrid", True), bool)
        coordinator = Coordinator(my_predbat)
        assert coordinator.hybrid_override() is None, coordinator.hybrid_override()
        my_predbat.args["inverter_hybrid_override"] = False
        assert coordinator.hybrid_override() is False, coordinator.hybrid_override()
        my_predbat.args["inverter_hybrid_override"] = [True, None]
        assert coordinator.hybrid_override() == [True, None], coordinator.hybrid_override()
        # APPS_SCHEMA takes both forms, a list with None entries included
        assert APPS_SCHEMA["inverter_hybrid_override"]["type"] == "boolean|boolean_list", APPS_SCHEMA.get("inverter_hybrid_override")
        for value in (True, [True, None]):
            my_predbat.args["inverter_hybrid_override"] = value
            my_predbat.validate_config()
            assert "inverter_hybrid_override" not in my_predbat.arg_errors, (value, my_predbat.arg_errors)
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        my_predbat.arg_errors = saved_errors
        my_predbat.arg_warnings = saved_warnings
    print("PASS: hybrid override not the switch")
    return 0


def test_hybrid_override_malformed_ignored():
    """A value that is not a boolean, or a list of booleans and None, is ignored with one Warn line naming the key; the records decide."""
    for override, warning in (
        ("maybe", "Warn: Coordinator: inverter_hybrid_override in apps.yaml is 'maybe', not true, false or a list of them - ignored"),
        ({"slot": 0}, "Warn: Coordinator: inverter_hybrid_override in apps.yaml is {'slot': 0}, not true, false or a list of them - ignored"),
        (1, "Warn: Coordinator: inverter_hybrid_override in apps.yaml is 1, not true, false or a list of them - ignored"),
    ):
        base, coordinator, ha = _override_setup(override, current=True)
        coordinator.report("solis", _hybrid_report([("solis:A", False, "SolisCloud")]))
        coordinator.report("solis", _hybrid_report([("solis:A", False, "SolisCloud"), ("solis:B", None, "SolisCloud")]))
        assert ha.calls == [(HYBRID_ENTITY, False)], (override, ha.calls)
        assert [line for line in base.log_messages if line.startswith("Warn:")] == [warning], base.log_messages
    # One bad entry in a list is ignored on its own; the good ones still count
    base, coordinator, ha = _override_setup(["maybe", True], current=False, apps={"num_inverters": 2, "inverter_type": ["GE", "GE"]})
    coordinator.configure()
    coordinator.configure()
    assert ha.calls == [(HYBRID_ENTITY, True)], ha.calls
    assert [line for line in base.log_messages if line.startswith("Warn:")] == ["Warn: Coordinator: inverter_hybrid_override[0] in apps.yaml is 'maybe', not true, false or None - ignored"], base.log_messages
    # YAML reads a bare None as the string "None": as a list entry or the whole value it means not set, quietly
    base, coordinator, ha = _override_setup(["None", "null", True], current=False, apps={"num_inverters": 3, "inverter_type": ["GE", "GE", "GE"]})
    assert coordinator.hybrid_override() == [None, None, True], coordinator.hybrid_override()
    base, coordinator, ha = _override_setup("None", current=True)
    assert coordinator.hybrid_override() is None
    assert not [line for line in base.log_messages if line.startswith("Warn:")], base.log_messages
    print("PASS: hybrid override malformed ignored")
    return 0


def test_inverter_records_copy():
    """Coordinator.inverter_records() is a deep copy of one source's validated records; ComponentBase.discovery_inverter_records() wraps it, [] without a coordinator."""
    from component_base import ComponentBase

    class _Component(ComponentBase):
        """A concrete ComponentBase, built without __init__ as the component test mocks are."""

        def initialize(self, **kwargs):
            """Nothing to set up."""

    base, registry, coordinator = _setup()
    assert coordinator.inverter_records("gecloud") == []
    coordinator.report("gecloud", _hybrid_report([("gecloud:a", False, "GEC"), ("gecloud:b", None, "GEC")]))
    records = coordinator.inverter_records("gecloud")
    assert [record["device_id"] for record in records] == ["gecloud:a", "gecloud:b"], records
    assert records[0]["hybrid"] is False and "hybrid" not in records[1]
    records[0]["entities"]["reserve"]["entity_id"] = "changed"
    assert coordinator.reports["gecloud"]["inverters"][0]["entities"]["reserve"]["entity_id"] != "changed"
    component = _Component.__new__(_Component)
    assert component.discovery_inverter_records("gecloud") == []  # no base at all
    component.base = _Bare()
    assert component.discovery_inverter_records("gecloud") == []  # no registry
    component.base = base
    assert [record["device_id"] for record in component.discovery_inverter_records("gecloud")] == ["gecloud:a", "gecloud:b"]
    print("PASS: inverter records copy")
    return 0


# ---------------------------------------------------------------------------------------------
# Acceptance: GE Cloud's previous automatic configuration against discovery's, from the same args
# ---------------------------------------------------------------------------------------------

# What the comparison covers: the whole inverter configuration
CONFIGURATION_SETTINGS = ("num_inverters", "inverter_type") + tuple(sorted(BINDING_SETTINGS | SITE_SETTINGS))

# The per-slot settings: every binding setting but the compact summed lists, whose index is not a slot
SLOT_SETTINGS = ("inverter_type",) + tuple(sorted(BINDING_SETTINGS - set(COMPACT_SETTINGS)))


def _attach(ge, base, name="gecloud"):
    """Put a MockGECloudDirect on a _Base: base.args is its args, set_arg and log go through the base, and the base gains the mock base's stand-ins."""
    for attribute, value in vars(ge.base).items():
        if not hasattr(base, attribute):
            setattr(base, attribute, value)
    ge.base = base
    ge.config_args = base.args
    ge.args = base.args
    ge.component_name = name
    ge.set_arg = base.set_arg

    def log(message):
        """Record the line on the component and in the base's log, which the coordinator writes to as well."""
        ge.log_messages.append(message)
        base.log(message)

    ge.log = log


class _AutoGECloud(MockGECloudDirect):
    """The real GECloudDirect on a _Base: only the HTTP and Home Assistant layers are MockGECloudDirect's stand-ins."""

    def __init__(self, base):
        """Build the mock and attach it to base as the "gecloud" component."""
        super().__init__()
        _attach(self, base)


def _entity(domain, device, suffix):
    """A GE Cloud entity id."""
    return "{}.predbat_gecloud_{}_{}".format(domain, device, suffix)


def _install(ge, devices, settings, info, site):
    """Give ge the discovered install: devices, settings, info and, when site is (limit_w, inverters), the site's export limit."""
    ge.devices_dict = copy.deepcopy(devices)
    ge.settings = copy.deepcopy(settings)
    ge.info = copy.deepcopy(info or {})
    if site is not None:
        ge.site_id = "site001"
        ge.site_export_limit, ge.site_inverters = site[0], list(site[1])


def _automatic_run(devices, settings, info, start, site=None, base_class=_Base):
    """
    One GE Cloud automatic configuration through a real Coordinator from args `start` (also the apps.yaml snapshot).

    Runs the real async_automatic_config(), then the real refresh_discovery(), as run() does in one cycle. Then
    two more configure() passes, as Predbat runs after phase 1 and at the startup barrier, which must write
    nothing: a pass that rebuilds from args an earlier pass rewrote must reach the same configuration. base_class builds
    the base (_TemplatingBase to expand {name} templates as PredBat does). Returns (ge, base).
    """
    base = base_class(args=start, apps_yaml=start)
    ge = _AutoGECloud(base)
    _install(ge, devices, settings, info, site)
    _Components(base, {"gecloud": ge})
    run_async(ge.async_automatic_config(ge.devices_dict))
    ge.refresh_discovery()
    settled = copy.deepcopy(base.args)
    calls = len(base.set_calls)
    base.components.coordinator.configure()
    base.components.coordinator.configure()
    assert base.set_calls[calls:] == [], "a later configure() pass wrote {}".format(base.set_calls[calls:])
    assert base.args == settled, "a later configure() pass changed args: {}".format(compare_configuration(settled, base.args))
    return ge, base


def _starting_args(config=None, apps_yaml=None):
    """The args both runs start from: the component's config settings, then the user's apps.yaml entries."""
    start = copy.deepcopy(config or {})
    start.update(copy.deepcopy(apps_yaml or {}))
    return start


def run_new(devices, settings, info=None, config=None, apps_yaml=None, base_class=_Base):
    """Run GE Cloud's automatic configuration through a real Coordinator, and return (new_args, new_run_log)."""
    ge, base = _automatic_run(devices, settings, info, _starting_args(config, apps_yaml), base_class=base_class)
    return base.args, list(base.log_messages)


def _legacy_run(devices, settings, info, start, site=None):
    """The previous automatic configuration from args `start` (also the apps.yaml snapshot): _legacy_bindings() applied to a copy of start by the rules it was written with."""
    ge = _AutoGECloud(_Base(args=start, apps_yaml=start))
    _install(ge, devices, settings, info, site)
    return apply_legacy_bindings(ge._legacy_bindings(ge.devices_dict), start, start)


def run_old_and_new(devices, settings, info=None, config=None, apps_yaml=None, site=None) -> tuple[dict, dict, list[str]]:
    """GE Cloud's configuration twice from the same starting args - the previous automatic configuration, then the real one through a Coordinator - as (old_args, new_args, new_run_log); site is (limit_w, inverters) or None."""
    start = _starting_args(config, apps_yaml)
    old_args = _legacy_run(devices, settings, info, copy.deepcopy(start), site=site)
    _, new_base = _automatic_run(devices, settings, info, copy.deepcopy(start), site=site)
    return old_args, new_base.args, list(new_base.log_messages)


def _normal(value):
    """An args value as the comparison sees it: trailing None entries dropped, and absent, None or all-None all None."""
    if isinstance(value, (list, tuple)):
        value = list(value)
        while value and value[-1] is None:
            value.pop()
        return value or None
    return value


def _all_false(value):
    """Whether a normalised invert setting value is all-False: False, or a list whose every entry is False or None."""
    if isinstance(value, list):
        return all(entry is False or entry is None for entry in value)
    return value is False


def compare_configuration(old_args, new_args, known=()) -> list[str]:
    """
    Every difference in num_inverters, inverter_type, BINDING_SETTINGS and SITE_SETTINGS, except the settings named in `known`.

    Trailing None entries and an all-None key against an absent key are equal. An unset INVERT_SETTINGS member against an
    all-False one is equal too: discovery writes False per slot where the old configuration left the key unset, and
    inverter.py reads both as False. Nothing else is normalised.
    """
    differences = []
    for setting in CONFIGURATION_SETTINGS:
        if setting in known:
            continue
        old, new = _normal(old_args.get(setting)), _normal(new_args.get(setting))
        if setting in INVERT_SETTINGS and ((old is None and _all_false(new)) or (new is None and _all_false(old))):
            continue
        if old != new:
            differences.append("{}: old {!r} new {!r}".format(setting, old, new))
    return differences


def _assert_new_run_clean(log):
    """The new run's checker ran and logged no unexpected difference, and the coordinator warned about nothing."""
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery set")], log
    assert any(line.startswith("Info: GECloud: discovery configuration matches") for line in log), log
    assert not [line for line in log if line.startswith("Warn: Coordinator")], log


def _assert_accepted(old, new, log, known=None):
    """
    The old and new configurations are equal apart from `known` ({setting: (old value, new value)}), each of
    which must differ in exactly that way, and the new run is clean.
    """
    known = known or {}
    differences = compare_configuration(old, new, known=tuple(known))
    assert differences == [], "\n".join(differences)
    for setting, (old_value, new_value) in known.items():
        assert _normal(old_value) != _normal(new_value), "{} is listed as known but the expected values do not differ".format(setting)
        assert _normal(old.get(setting)) == _normal(old_value), "{}: old run {!r}, documented {!r}".format(setting, old.get(setting), old_value)
        assert _normal(new.get(setting)) == _normal(new_value), "{}: new run {!r}, documented {!r}".format(setting, new.get(setting), new_value)
    _assert_new_run_clean(log)


def _one_battery(pv=None):
    """(devices, settings, info) for battery001 with every control register and a meter, plus PV-only devices."""
    devices = _gec_devices(battery=["battery001"], pv=pv, meters={"battery001": [1001]})
    info = {"battery001": GEC_INFO}
    info.update({device: {"info": {"model": "GIV-PV"}} for device in pv or []})
    return devices, {"battery001": dict(GEC_FULL_REGISTERS)}, info


def _two_batteries(meters, second=None, gateway=None, ems=None):
    """(devices, settings, info) for battery001 and battery002 with the given meter serial lists; second is battery002's registers."""
    devices = _gec_devices(battery=["battery001", "battery002"], meters={"battery001": meters[0], "battery002": meters[1]} if meters else None, gateway=gateway, ems=ems)
    settings = {"battery001": dict(GEC_FULL_REGISTERS), "battery002": dict(second or GEC_FULL_REGISTERS)}
    return devices, settings, {"battery001": GEC_INFO, "battery002": GEC_INFO}


def test_accept_single():
    """One battery inverter: the base case."""
    old, new, log = run_old_and_new(*_one_battery())
    _assert_accepted(old, new, log)
    assert new["num_inverters"] == 1 and new["inverter_type"] == ["GEC"], (new.get("num_inverters"), new.get("inverter_type"))
    # GE Cloud marks nothing inverted, so discovery writes an explicit False per slot where the old configuration left the keys unset
    for setting in INVERT_SETTINGS:
        assert new.get(setting) == [False] and old.get(setting) is None, (setting, old.get(setting), new.get(setting))
    # Only unset against all-False is equal: a True, or a mixed list, against unset is still a difference
    assert compare_configuration(old, dict(new, grid_power_invert=[True])) == ["grid_power_invert: old None new [True]"]
    assert compare_configuration(dict(old, load_power_invert=[False, True]), new) == ["load_power_invert: old [False, True] new [False]"]
    assert compare_configuration(old, dict(new, battery_power_invert=[None, True])) == ["battery_power_invert: old None new [None, True]"]
    print("PASS: accept single")
    return 0


def test_accept_two_inverters():
    """Two battery inverters with a meter each: the per-slot lists line up."""
    old, new, log = run_old_and_new(*_two_batteries(([1001], [1002])))
    _assert_accepted(old, new, log)
    assert new["num_inverters"] == 2
    for setting in SLOT_SETTINGS + COMPACT_SETTINGS:
        value = new.get(setting)
        if value is not None:
            assert isinstance(value, list) and len(value) == 2, (setting, value)
    print("PASS: accept two inverters")
    return 0


def test_accept_mixed_registers():
    """One battery with the power rates, pause and discharge target; one with only the percentage rates: the per-device rule and its named exceptions."""
    old, new, log = run_old_and_new(*_two_batteries(([1001], [1002]), second=GEC_MIXED_REGISTERS))
    known = {
        setting: ([_entity(domain, "battery001", suffix), _entity(domain, "battery002", suffix)], [_entity(domain, "battery001", suffix), None])
        for setting, domain, suffix in (
            ("pause_mode", "select", "pause_battery"),
            ("pause_start_time", "select", "pause_battery_start_time"),
            ("pause_end_time", "select", "pause_battery_end_time"),
            ("discharge_target_soc", "number", "dc_discharge_1_lower_soc_percent_limit"),
        )
    }
    _assert_accepted(old, new, log, known)
    assert isinstance(slot_value(new["pause_mode"], 0), str) and slot_value(new["pause_mode"], 1) is None, new["pause_mode"]
    assert all(isinstance(entry, str) for entry in old["pause_mode"]) and len(old["pause_mode"]) == 2, old["pause_mode"]
    # The percentage rates are per device in both runs, so they are compared, not known
    assert _normal(new["charge_rate"]) == [_entity("number", "battery001", "battery_charge_power")], new["charge_rate"]
    assert _normal(new["charge_rate_percent"]) == [None, _entity("number", "battery002", "inverter_charge_power_percentage")], new["charge_rate_percent"]
    assert _normal(new["discharge_rate_percent"]) == [None, _entity("number", "battery002", "inverter_discharge_power_percentage")], new["discharge_rate_percent"]
    print("PASS: accept mixed registers")
    return 0


def test_accept_pv_only_split_off():
    """A PV-only device with ge_cloud_automatic_split_pv off adds nothing."""
    old, new, log = run_old_and_new(*_one_battery(pv=["pv001"]))
    _assert_accepted(old, new, log)
    assert new["pv_power"] == [_entity("sensor", "battery001", "solar_power")], new["pv_power"]
    assert new["pv_today"] == [_entity("sensor", "battery001", "solar_total")], new["pv_today"]
    assert new["num_inverters"] == 1
    print("PASS: accept PV-only split off")
    return 0


def test_accept_pv_only_split_on():
    """ge_cloud_automatic_split_pv on: pv_power and pv_today gain the PV-only device, num_inverters stays 1."""
    old, new, log = run_old_and_new(*_one_battery(pv=["pv001"]), config={"ge_cloud_automatic_split_pv": True})
    _assert_accepted(old, new, log)
    assert new["pv_power"] == [_entity("sensor", "battery001", "solar_power"), _entity("sensor", "pv001", "solar_power")], new["pv_power"]
    assert new["pv_today"] == [_entity("sensor", "battery001", "solar_total"), _entity("sensor", "pv001", "solar_total")], new["pv_today"]
    assert new["num_inverters"] == 1
    print("PASS: accept PV-only split on")
    return 0


def _assert_shared_ct(new):
    """Grid and load from the first inverter, zeros after, and one entry in each summed energy list."""
    assert new["grid_power"] == [_entity("sensor", "battery001", "grid_power"), 0], new["grid_power"]
    assert new["load_power"] == [_entity("sensor", "battery001", "consumption_power"), 0], new["load_power"]
    assert new["import_today"] == [_entity("sensor", "battery001", "grid_import_total")], new["import_today"]
    assert new["export_today"] == [_entity("sensor", "battery001", "grid_export_total")], new["export_today"]
    assert new["load_today"] == [_entity("sensor", "battery001", "consumption_total")], new["load_today"]


def _assert_separate_ct(new):
    """Every inverter contributes grid, load and the summed energy lists."""
    assert new["grid_power"] == [_entity("sensor", "battery001", "grid_power"), _entity("sensor", "battery002", "grid_power")], new["grid_power"]
    assert new["load_power"] == [_entity("sensor", "battery001", "consumption_power"), _entity("sensor", "battery002", "consumption_power")], new["load_power"]
    assert new["import_today"] == [_entity("sensor", "battery001", "grid_import_total"), _entity("sensor", "battery002", "grid_import_total")], new["import_today"]


def test_accept_ct_shared_detected():
    """Two inverters reporting the same meter serial share one CT clamp."""
    old, new, log = run_old_and_new(*_two_batteries(([9999], [9999])))
    _assert_accepted(old, new, log)
    _assert_shared_ct(new)
    print("PASS: accept CT shared detected")
    return 0


def test_accept_ct_separate_detected():
    """Two inverters with different meter serials each contribute."""
    old, new, log = run_old_and_new(*_two_batteries(([1001], [1002])))
    _assert_accepted(old, new, log)
    _assert_separate_ct(new)
    print("PASS: accept CT separate detected")
    return 0


def test_accept_ct_shared_override():
    """ge_cloud_automatic_shared_ct forces a shared CT over different meter serials."""
    old, new, log = run_old_and_new(*_two_batteries(([1001], [1002])), config={"ge_cloud_automatic_shared_ct": True})
    _assert_accepted(old, new, log)
    _assert_shared_ct(new)
    print("PASS: accept CT shared override")
    return 0


def test_accept_ct_split_override():
    """ge_cloud_automatic_split_ct forces separate CTs over a shared meter serial, and wins when both overrides are set."""
    for config in ({"ge_cloud_automatic_split_ct": True}, {"ge_cloud_automatic_split_ct": True, "ge_cloud_automatic_shared_ct": True}):
        old, new, log = run_old_and_new(*_two_batteries(([9999], [9999])), config=config)
        _assert_accepted(old, new, log)
        _assert_separate_ct(new)
    print("PASS: accept CT split override")
    return 0


def test_accept_gateway():
    """A gateway in front of two batteries is one logical inverter, bound to the gateway's entities."""
    devices, _, info = _two_batteries(None, gateway="gw001")
    old, new, log = run_old_and_new(devices, {"gw001": dict(GEC_FULL_REGISTERS)}, info)
    _assert_accepted(old, new, log)
    assert new["num_inverters"] == 1 and new["inverter_type"] == ["GEC"], (new["num_inverters"], new["inverter_type"])
    bound = [entry for setting in CONFIGURATION_SETTINGS if setting not in ("num_inverters", "inverter_type") for entry in (new.get(setting) if isinstance(new.get(setting), list) else [new.get(setting)]) if isinstance(entry, str)]
    assert bound and all("_gw001_" in entry for entry in bound), bound
    assert new["charge_rate"] == [_entity("number", "gw001", "battery_charge_power")], new["charge_rate"]
    print("PASS: accept gateway")
    return 0


def test_accept_ems():
    """An EMS with two batteries: type GEE, the EMS's entities, power sensors on the first slot only, and GE Cloud Data declining from the records with ge_cloud_data untouched."""
    old, new, log = run_old_and_new(*_two_batteries(None, ems="ems001"))
    known = {
        setting: ([_entity(domain, "battery001", suffix), _entity(domain, "battery002", suffix)], None)
        for setting, domain, suffix in (
            ("scheduled_charge_enable", "switch", "enable_force_charge"),
            ("scheduled_discharge_enable", "switch", "enable_dc_discharge"),
            ("pause_mode", "select", "pause_battery"),
            ("inverter_mode", "switch", "enable_eco_mode"),
        )
    }
    assert sorted(known) == sorted(GE_CLOUD_DUMMIED_SETTINGS["GEE"]), sorted(known)
    _assert_accepted(old, new, log, known)
    assert new["inverter_type"] == ["GEE", "GEE"], new["inverter_type"]
    assert new["battery_power"] == [_entity("sensor", "ems001", "battery_power"), 0], new["battery_power"]
    assert new["charge_start_time"] == [_entity("select", "ems001", "charge_start_time_slot_1")] * 2, new["charge_start_time"]
    # Neither run writes ge_cloud_data or ge_cloud_serial: GE Cloud Data declines behind the EMS from GE Cloud's records itself
    for setting in ("ge_cloud_data", "ge_cloud_serial"):
        assert setting not in old and setting not in new, (setting, old.get(setting), new.get(setting))
    _, base = _automatic_run(*_two_batteries(None, ems="ems001"), _starting_args())
    cloud_data = GECloudData.__new__(GECloudData)
    cloud_data.base = base
    assert cloud_data.behind_ems() is True, cloud_data.discovery_inverter_records("gecloud")
    print("PASS: accept EMS")
    return 0


def test_accept_opt_outs():
    """ge_cloud_load_today_ignore and the user's own inverter_mode and export_limit survive in both runs."""
    apps_yaml = {"load_today": ["sensor.mine"], "inverter_mode": ["select.mine"], "export_limit": [3600]}
    old, new, log = run_old_and_new(*_one_battery(), config={"ge_cloud_load_today_ignore": True}, apps_yaml=apps_yaml)
    _assert_accepted(old, new, log)
    for setting, value in apps_yaml.items():
        assert old[setting] == value and new[setting] == value, (setting, old.get(setting), new.get(setting))
    print("PASS: accept opt-outs")
    return 0


def test_accept_single_kept_values_two_batteries():
    """
    A single apps.yaml value for a kept setting on two batteries is kept whole, for both inverters, exactly as the previous
    configuration kept it: export_limit 0 with and without a site limit, 3000 with one, and one inverter_mode switch.
    """
    site = (8000, ["battery001", "battery002"])
    for apps_yaml, site_limit in (({"export_limit": 0}, site), ({"export_limit": 0}, None), ({"export_limit": 3000}, site), ({"inverter_mode": "switch.mine"}, None)):
        old, new, log = run_old_and_new(*_two_batteries(([1001], [1002])), apps_yaml=apps_yaml, site=site_limit)
        _assert_accepted(old, new, log)
        for setting, value in apps_yaml.items():
            assert old[setting] == value and new[setting] == value, (apps_yaml, site_limit, old.get(setting), new.get(setting))
        assert not [line for line in log if line.startswith("Warn: GECloud")], (apps_yaml, site_limit, log)
        if site_limit is not None:
            assert "keeping your apps.yaml setting" in " ".join(log), (apps_yaml, log)
    print("PASS: accept single kept values on two batteries")
    return 0


def test_accept_manual_plus_discovered():
    """A manual inverter of another type keeps slot 0 unchanged; the GE Cloud inverter lands in slot 1, bound as in the single run."""
    apps_yaml = {"num_inverters": 1, "inverter_type": ["SolisCloud"], "charge_rate": ["number.solis_charge"], "soc_percent": ["sensor.solis_soc"]}
    single, _ = run_new(*_one_battery())
    new, log = run_new(*_one_battery(), apps_yaml=apps_yaml)
    assert new["num_inverters"] == 2, new["num_inverters"]
    assert new["inverter_type"] == ["SolisCloud", "GEC"], new["inverter_type"]
    for setting in SLOT_SETTINGS:
        assert slot_value(new.get(setting), 0) == slot_value(apps_yaml.get(setting), 0), (setting, new.get(setting))
        assert slot_value(new.get(setting), 1) == slot_value(single.get(setting), 0), (setting, new.get(setting), single.get(setting))
    for setting in COMPACT_SETTINGS + tuple(sorted(SITE_SETTINGS)):
        assert _normal(new.get(setting)) == _normal(single.get(setting)), (setting, new.get(setting), single.get(setting))
    assert not [line for line in log if "taking over" in line], log
    assert "Coordinator: inverter slot 1 configured from gecloud (gecloud:battery001)" in log, log
    _assert_new_run_clean(log)
    print("PASS: accept manual plus discovered")
    return 0


def _template_apps_yaml(*serials):
    """The inverter bindings of templates/givenergy_cloud.yaml with geserial (and geserial2, ...) resolved, one inverter per serial."""
    slots = [_template_slot(serial) for serial in serials]
    return {setting: (len(serials) if setting == "num_inverters" else [entry for slot in slots for entry in slot[setting]]) for setting in slots[0]}


def _template_slot(serial):
    """templates/givenergy_cloud.yaml's bindings for one inverter."""
    return {
        "num_inverters": 1,
        "inverter_type": ["GEC"],
        "load_today": ["sensor.ge_inverter_{}_consumption_today".format(serial)],
        "import_today": ["sensor.ge_inverter_{}_grid_import_today".format(serial)],
        "export_today": ["sensor.ge_inverter_{}_grid_export_today".format(serial)],
        "pv_today": ["sensor.ge_inverter_{}_solar_today".format(serial)],
        "charge_rate": ["number.ge_inverter_{}_battery_charge_power".format(serial)],
        "discharge_rate": ["number.ge_inverter_{}_battery_discharge_power".format(serial)],
        "battery_power": ["sensor.ge_inverter_{}_battery_power".format(serial)],
        "pv_power": ["sensor.ge_inverter_{}_solar_power".format(serial)],
        "load_power": ["sensor.ge_inverter_{}_consumption_power".format(serial)],
        "grid_power": ["sensor.ge_inverter_{}_grid_power".format(serial)],
        "soc_percent": ["sensor.ge_inverter_{}_battery_soc".format(serial)],
        "soc_max": ["sensor.ge_inverter_{}_battery_size".format(serial)],
        "reserve": ["number.ge_inverter_{}_battery_reserve_limit".format(serial)],
        "inverter_time": ["sensor.ge_inverter_{}_inverter_time".format(serial)],
        "charge_start_time": ["select.ge_inverter_{}_ac_charge_1_start_time".format(serial)],
        "charge_end_time": ["select.ge_inverter_{}_ac_charge_1_end_time".format(serial)],
        "charge_limit": ["number.ge_inverter_{}_ac_charge_upper_limit".format(serial)],
        "discharge_start_time": ["select.ge_inverter_{}_dc_discharge_1_start_time".format(serial)],
        "discharge_end_time": ["select.ge_inverter_{}_dc_discharge_1_end_time".format(serial)],
        "scheduled_charge_enable": ["switch.ge_inverter_{}_ac_charge_enable".format(serial)],
        "scheduled_discharge_enable": ["switch.ge_inverter_{}_enable_dc_discharge".format(serial)],
        "pause_mode": ["select.ge_inverter_{}_pause_battery".format(serial)],
        "pause_start_time": ["select.ge_inverter_{}_pause_battery_start_time".format(serial)],
        "pause_end_time": ["select.ge_inverter_{}_pause_battery_end_time".format(serial)],
        "inverter_limit": [3600],
        "inverter_battery_rate_min": [200],
        "battery_scaling": [1.0],
    }


def test_accept_template_takeover():
    """The givenergy_cloud.yaml bindings naming the same serial: discovery takes the slot over and the result is the single run's."""
    takeover = "Warn: Coordinator: Inverter slot 0: gecloud inverter battery001 is also configured manually in apps.yaml - discovery is taking over this slot"
    single, _ = run_new(*_one_battery())
    new, log = run_new(*_one_battery(), apps_yaml=_template_apps_yaml("battery001"))
    differences = compare_configuration(single, new)
    assert differences == [], "\n".join(differences)
    assert takeover in log, log
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery set")], log

    # A template that sets inverter_mode keeps the user's value; everything else is still the single run's
    apps_yaml = dict(_template_apps_yaml("battery001"), inverter_mode=["switch.my_eco_mode"])
    new, log = run_new(*_one_battery(), apps_yaml=apps_yaml)
    assert new["inverter_mode"] == ["switch.my_eco_mode"], new["inverter_mode"]
    assert single["inverter_mode"] == [_entity("switch", "battery001", "enable_eco_mode")], single["inverter_mode"]
    differences = compare_configuration(single, new, known=("inverter_mode",))
    assert differences == [], "\n".join(differences)
    assert takeover in log, log
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery set")], log
    assert any(line.startswith("Info: GECloud: discovery configuration matches") for line in log), log
    print("PASS: accept template takeover")
    return 0


def _assert_gateway_template_collapse(gateway):
    """The two-serial givenergy_cloud.yaml bindings with a gateway in front of both batteries: one GEC slot, the gateway's, and it stays that way."""
    devices, _, info = _two_batteries(None, gateway=gateway)
    settings = {gateway: dict(GEC_FULL_REGISTERS)}
    single, _ = run_new(devices, settings, info)
    # run_new's harness runs two more configure() passes and fails if either writes anything
    new, log = run_new(devices, settings, info, apps_yaml=_template_apps_yaml("battery001", "battery002"))
    assert new["num_inverters"] == 1 and new["inverter_type"] == ["GEC"], (gateway, new["num_inverters"], new["inverter_type"])
    assert new["load_today"] == [_entity("sensor", gateway, "consumption_total")], (gateway, new["load_today"])
    differences = compare_configuration(single, new)
    assert differences == [], "\n".join(differences)
    assert "Warn: Coordinator: Inverter slot 0: gecloud inverter battery001 is also configured manually in apps.yaml - discovery is taking over this slot" in log, log
    assert "Warn: Coordinator: Inverter slot 1: also names battery002, already configured in slot 0 - clearing this slot" in log, log
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery")], log


def test_accept_template_gateway():
    """Two template slots naming battery001 and battery002, behind gateway gw00000001: slot 0 is taken over, slot 1 cleared, on every pass."""
    _assert_gateway_template_collapse("gw00000001")
    print("PASS: accept template gateway")
    return 0


def test_accept_template_short_gateway():
    """The same with a gateway serial shorter than MIN_SERIAL_LENGTH, so slot 0 can only ever match through the template's battery serial."""
    _assert_gateway_template_collapse("gw001")
    print("PASS: accept template short gateway")
    return 0


def test_accept_template_unexpanded():
    """
    The givenergy_cloud.yaml bindings as apps.yaml really holds them: every entity keeps the literal {geserial}, with the serial in
    geserial, and Predbat expands it only when the value is read. The serial is still found, so discovery takes the slot over and
    the result is the single run's - never a second GEC slot beside the template's.
    """
    takeover = "Warn: Coordinator: Inverter slot 0: gecloud inverter battery001 is also configured manually in apps.yaml - discovery is taking over this slot"
    single, _ = run_new(*_one_battery())
    apps_yaml = dict(_template_apps_yaml("{geserial}"), geserial="battery001")
    assert apps_yaml["charge_rate"] == ["number.ge_inverter_{geserial}_battery_charge_power"], apps_yaml["charge_rate"]
    new, log = run_new(*_one_battery(), apps_yaml=apps_yaml, base_class=_TemplatingBase)
    assert new["num_inverters"] == 1 and new["inverter_type"] == ["GEC"], (new["num_inverters"], new["inverter_type"])
    differences = compare_configuration(single, new)
    assert differences == [], "\n".join(differences)
    assert takeover in log, log
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery set")], log

    # Two template slots, geserial and geserial2, behind one gateway: slot 0 taken over, slot 1 cleared, as with the serials written out
    devices, _, info = _two_batteries(None, gateway="gw00000001")
    settings = {"gw00000001": dict(GEC_FULL_REGISTERS)}
    single, _ = run_new(devices, settings, info)
    apps_yaml = dict(_template_apps_yaml("{geserial}", "{geserial2}"), geserial="battery001", geserial2="battery002")
    new, log = run_new(devices, settings, info, apps_yaml=apps_yaml, base_class=_TemplatingBase)
    assert new["num_inverters"] == 1 and new["inverter_type"] == ["GEC"], (new["num_inverters"], new["inverter_type"])
    differences = compare_configuration(single, new)
    assert differences == [], "\n".join(differences)
    assert takeover in log, log
    assert "Warn: Coordinator: Inverter slot 1: also names battery002, already configured in slot 0 - clearing this slot" in log, log

    # A templated ge_cloud_serial names the inverter for slot 0 too, though no entity there carries the serial
    apps_yaml = {"num_inverters": 1, "inverter_type": ["GEC"], "charge_rate": ["number.my_charge_rate"], "geserial": "battery001", "ge_cloud_serial": "{geserial}"}
    new, log = run_new(*_one_battery(), apps_yaml=apps_yaml, base_class=_TemplatingBase)
    assert new["num_inverters"] == 1 and new["charge_rate"] == [_entity("number", "battery001", "battery_charge_power")], (new["num_inverters"], new["charge_rate"])
    assert takeover in log, log
    print("PASS: accept template unexpanded")
    return 0


def test_accept_givtcp_rest_manual():
    """A manual GivTCP inverter with givtcp_rest: the old run deletes the key and takes over, the new run keeps slot 0 and clears only its own slot (a known difference)."""
    rest = "http://192.168.1.10:6345"
    apps_yaml = {"num_inverters": 1, "inverter_type": ["GE"], "givtcp_rest": [rest]}
    old, new, log = run_old_and_new(*_one_battery(), apps_yaml=apps_yaml)
    assert "givtcp_rest" not in old and old["inverter_type"] == ["GEC"] and old["num_inverters"] == 1, old
    assert _normal(new.get("givtcp_rest")) == [rest], new.get("givtcp_rest")
    assert new["inverter_type"] == ["GE", "GEC"] and new["num_inverters"] == 2, new
    single, _ = run_new(*_one_battery())
    for setting in SLOT_SETTINGS:
        assert slot_value(new.get(setting), 1) == slot_value(single.get(setting), 0), (setting, new.get(setting), single.get(setting))
    assert any(line.startswith("Info: GECloud: discovery differs on givtcp_rest (") for line in log), log
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery") or line.startswith("Warn: Coordinator")], log
    print("PASS: accept givtcp_rest manual")
    return 0


def test_accept_battery_removed():
    """
    Two GE Cloud batteries configured, then GE Cloud reports only battery002: discovery closes the gap, and the result is
    exactly the previous automatic configuration of a battery002-only install, which always rebuilt from slot 0.
    """
    devices, settings, info = _two_batteries(([1001], [1002]))
    start = _starting_args()
    ge, base = _automatic_run(devices, settings, info, copy.deepcopy(start))
    assert base.components.coordinator.slot_map == {"gecloud:battery001": 0, "gecloud:battery002": 1}
    # The run loop's view of the account after battery001 has gone: devices change, settings stay
    remaining = _gec_devices(battery=["battery002"], meters={"battery002": [1002]})
    mark = len(base.log_messages)
    ge.devices_dict = copy.deepcopy(remaining)
    run_async(ge.async_automatic_config(ge.devices_dict))
    ge.refresh_discovery()
    assert base.components.coordinator.slot_map == {"gecloud:battery002": 0}, base.components.coordinator.slot_map
    old = _legacy_run(remaining, settings, info, copy.deepcopy(start))
    differences = compare_configuration(old, base.args)
    assert differences == [], "\n".join(differences)
    log = base.log_messages[mark:]  # this run's lines only, not the two-battery run's
    assert "Warn: Coordinator: Inverter slot 1 moved to slot 0: gecloud inverter gecloud:battery002 closes the gap left by a device that is no longer reported" in log, log
    assert not [line for line in log if line.startswith("Warn: GECloud: discovery") or "is empty below" in line], log
    assert any(line.startswith("Info: GECloud: discovery configuration matches") for line in log), log
    settled = copy.deepcopy(base.args)
    calls = len(base.set_calls)
    base.components.coordinator.configure()
    base.components.coordinator.configure()
    assert base.set_calls[calls:] == [] and base.args == settled, base.set_calls[calls:]
    print("PASS: accept battery removed")
    return 0


def test_accept_run_loop_incremental():
    """The real run() through a coordinator: a battery added to the account later takes slot 1, and slot 0 is never written again."""
    base = _Base(args={})
    ge, api, calls = _make_rediscovery_ge_cloud(_gec_devices(battery=["battery001"]))
    _attach(ge, base)
    # The real automatic configuration, not the helper's recording stand-in
    del ge.async_automatic_config

    async def get_settings(device, first, previous):
        """Every control register, as the API would report them."""
        return dict(GEC_FULL_REGISTERS)

    ge.async_get_inverter_settings = get_settings
    _Components(base, {"gecloud": ge})
    coordinator = base.components.coordinator

    assert run_async(ge.run(seconds=0, first=True)) is True
    assert coordinator.slot_map == {"gecloud:battery001": 0}, coordinator.slot_map
    assert base.args["charge_rate"] == [_entity("number", "battery001", "battery_charge_power")], base.args.get("charge_rate")
    assert calls["automatic"] == [], calls["automatic"]
    slot0 = {setting: slot_value(base.args.get(setting), 0) for setting in SLOT_SETTINGS + COMPACT_SETTINGS}
    mark = len(base.set_calls)

    api["devices"] = _gec_devices(battery=["battery001", "battery002"])
    assert run_async(ge.run(seconds=DEVICE_REFRESH_SECONDS, first=False)) is True
    assert coordinator.slot_map == {"gecloud:battery001": 0, "gecloud:battery002": 1}, coordinator.slot_map
    written = base.set_calls[mark:]
    assert [call for call in written if call[0] == "inverter_type"], written
    assert not [call for call in written if call[2] == 0], written
    assert {setting: slot_value(base.args.get(setting), 0) for setting in slot0} == slot0
    assert base.args["num_inverters"] == 2 and base.args["inverter_type"] == ["GEC", "GEC"], base.args
    assert base.args["charge_rate"] == [_entity("number", "battery001", "battery_charge_power"), _entity("number", "battery002", "battery_charge_power")], base.args["charge_rate"]
    assert not [line for line in base.log_messages if line.startswith("Warn: GECloud: discovery set")], base.log_messages
    assert any(line.startswith("Info: GECloud: discovery configuration matches") for line in base.log_messages), base.log_messages
    print("PASS: accept run loop incremental")
    return 0


def test_discovery_automatic_all(my_predbat=None):
    """Run every automatic configuration test, returning the number of failures."""
    failures = 0
    failures += test_configures_on_report()
    failures += test_not_opted_in_is_not_configured()
    failures += test_unchanged_report_writes_nothing()
    failures += test_second_device_leaves_first_alone()
    failures += test_unreported_source_left_out()
    failures += test_configure_fills_in_on_second_report()
    failures += test_checker_called_only_on_change()
    failures += test_checker_failure_is_logged()
    failures += test_unconverted_component_warns()
    failures += test_late_unconverted_component_warns()
    failures += test_modules_import_standalone()
    failures += test_unconverted_without_automatic_is_quiet()
    failures += test_stop_keeps_slots()
    failures += test_configure_never_raises()
    failures += test_configure_failure_rolls_back_partial_writes()
    failures += test_configure_is_serialised()
    failures += test_catalogue_carries_slots()
    failures += test_takeover_and_hole_warnings_logged()
    failures += test_summed_lists_keep_resolved_patterns()
    failures += test_baseline_drops_unresolved_patterns()
    failures += test_baseline_expands_templates()
    failures += test_release_compacts()
    failures += test_assemble_slots_follow_bindings()
    failures += test_bare_base_is_inert()
    failures += test_allow_automatic_config_wrapper()
    # The hybrid site rule
    failures += test_hybrid_one_false_turns_off()
    failures += test_hybrid_all_true_turns_on()
    failures += test_hybrid_nothing_known_untouched()
    failures += test_hybrid_ignores_pv_only()
    failures += test_hybrid_ignores_non_automatic_report()
    failures += test_hybrid_no_write_when_unchanged()
    failures += test_hybrid_write_not_repeated_while_pending()
    failures += test_hybrid_user_override_corrected()
    failures += test_hybrid_old_failure_keeps_newer_pending()
    failures += test_hybrid_runs_without_opt_in()
    failures += test_hybrid_never_raises()
    failures += test_hybrid_log_line()
    failures += test_hybrid_thin_base_inert()
    failures += test_hybrid_scheduled_from_a_running_loop()
    failures += test_hybrid_lost_value_keeps_last_known()
    failures += test_hybrid_removed_device_forgotten()
    failures += test_hybrid_never_known_stays_unknown()
    failures += test_hybrid_override_single_holds()
    failures += test_hybrid_override_list_discovered_slot()
    failures += test_hybrid_override_list_manual_slot()
    failures += test_hybrid_override_none_entries_leave_records()
    failures += test_hybrid_override_not_the_switch(my_predbat)
    failures += test_hybrid_override_malformed_ignored()
    failures += test_inverter_records_copy()
    # Acceptance: discovery against the old automatic configuration
    failures += test_accept_single()
    failures += test_accept_two_inverters()
    failures += test_accept_mixed_registers()
    failures += test_accept_pv_only_split_off()
    failures += test_accept_pv_only_split_on()
    failures += test_accept_ct_shared_detected()
    failures += test_accept_ct_separate_detected()
    failures += test_accept_ct_shared_override()
    failures += test_accept_ct_split_override()
    failures += test_accept_gateway()
    failures += test_accept_ems()
    failures += test_accept_opt_outs()
    failures += test_accept_single_kept_values_two_batteries()
    failures += test_accept_manual_plus_discovered()
    failures += test_accept_template_takeover()
    failures += test_accept_template_gateway()
    failures += test_accept_template_short_gateway()
    failures += test_accept_template_unexpanded()
    failures += test_accept_givtcp_rest_manual()
    failures += test_accept_battery_removed()
    failures += test_accept_run_loop_incremental()
    return failures

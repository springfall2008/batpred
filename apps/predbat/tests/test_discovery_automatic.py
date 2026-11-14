# fmt: off
# pylint: disable=line-too-long
"""Unit tests for the coordinator's automatic inverter configuration: the discovery_automatic switch, opting in, configure() and its triggers.

The coordinator is real; the base and the component registry are small stubs, so every write to args is
seen through the recorded set_arg calls.
"""

import copy
import os
import subprocess
import sys
import threading
import time

import coordinator as coordinator_module
from coordinator import Coordinator


class _Base:
    """Stand-in for PredBat: args, the apps.yaml snapshot, a recording log and Fetch.set_arg semantics."""

    def __init__(self, args=None, apps_yaml=None, switch=True):
        """Seed args (and the snapshot, a copy of args unless given); switch sets discovery_automatic."""
        self.args = copy.deepcopy(args or {})
        if switch is not None:
            self.args["discovery_automatic"] = switch
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


def _setup(args=None, apps_yaml=None, switch=True, components=None):
    """(base, registry, coordinator) with gecloud registered unless components says otherwise."""
    base = _Base(args=args, apps_yaml=apps_yaml, switch=switch)
    registry = _Components(base, components if components is not None else {"gecloud": _GECloud()})
    return base, registry, registry.coordinator


def _pairs(calls):
    """set_arg calls as (setting, index) pairs."""
    return [(arg, index) for arg, _, index in calls]


def test_switch_off_is_inert():
    """discovery_automatic unset: nothing opts in and nothing writes."""
    base, registry, coordinator = _setup(switch=None)
    assert coordinator.allow_automatic("gecloud") is False
    assert coordinator.automatic == {}
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.set_calls == [], base.set_calls
    assert coordinator.bindings is None and coordinator.slot_map == {}
    assert "inverter_type" not in base.args
    # Switched off explicitly behaves the same
    base, registry, coordinator = _setup(switch=False)
    assert coordinator.allow_automatic("gecloud") is False
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.set_calls == [], base.set_calls
    print("PASS: switch off is inert")
    return 0


def test_switch_string_value():
    """A string switch value is read the way get_arg reads a boolean: "true" is on, "false" off."""
    base, registry, coordinator = _setup(switch="false")
    assert coordinator.allow_automatic("gecloud") is False
    base, registry, coordinator = _setup(switch="true")
    assert coordinator.allow_automatic("gecloud") is True
    print("PASS: switch string value")
    return 0


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
    """With the switch on, a component that never opted in is not configured."""
    base, registry, coordinator = _setup()
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.set_calls == [], base.set_calls
    assert coordinator.slot_map == {}
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


class _Bare:
    """A base with only log and args, as the standalone harnesses build."""

    def __init__(self):
        """Nothing but a log list and args."""
        self.args = {"discovery_automatic": True}
        self.messages = []

    def log(self, message):
        """Record a log line."""
        self.messages.append(message)


def test_bare_base_is_inert():
    """On a base with no get_arg, args_from_apps_yaml or components, nothing is configured and nothing raises (amendment 4)."""
    base = _Bare()
    coordinator = Coordinator(base)
    assert coordinator.allow_automatic("gecloud") is False
    coordinator.report("gecloud", _report(["ce1111g111"]))
    coordinator.configure()
    assert base.args == {"discovery_automatic": True}
    assert not [line for line in base.messages if "failed" in line], base.messages
    # get_arg present and on, but no snapshot, no set_arg and no registry: still nothing
    base.get_arg = lambda name, default=None, **kwargs: base.args.get(name, default)
    assert coordinator.allow_automatic("gecloud") is True
    coordinator.report("gecloud", _report(["ce1111g111", "ce2222g222"]))
    coordinator.configure()
    assert base.args == {"discovery_automatic": True}
    assert coordinator.bindings is None
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


def test_discovery_automatic_all(my_predbat=None):
    """Run every automatic configuration test, returning the number of failures."""
    failures = 0
    failures += test_switch_off_is_inert()
    failures += test_switch_string_value()
    failures += test_configures_on_report()
    failures += test_not_opted_in_is_not_configured()
    failures += test_unchanged_report_writes_nothing()
    failures += test_second_device_leaves_first_alone()
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
    failures += test_bare_base_is_inert()
    failures += test_allow_automatic_config_wrapper()
    return failures

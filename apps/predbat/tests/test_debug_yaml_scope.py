# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for what create_debug_yaml() is allowed to reach.

The debug yaml is walked with yaml.dump(), whose default Dumper serialises arbitrary
objects through __reduce_ex__(). That makes every object it emits a doorway into
whatever that object references, so the dump's contents are decided by reachability,
not by the key filter alone.
"""

import io
import os
import re
import tracemalloc

import yaml

from inverter import Inverter
from userinterface import dump_debug_yaml


def _plant_inverter(my_predbat):
    """Give my_predbat one real Inverter, as a live GivTCP install has."""
    my_predbat.args["inverter_type"] = "GE"
    my_predbat.inverters = [Inverter(my_predbat, 0, quiet=True)]
    return my_predbat.inverters[0]


def test_inverter_dump_does_not_reach_the_base_object(my_predbat=None):
    """
    No inverter attribute may drag the PredBat object into the debug yaml.

    Inverter.givtcp (the GivTCP REST client) kept a back-reference to the base object, and
    create_debug_yaml()'s inverter walk only skipped keys literally named "base*". yaml.dump()
    then followed inverter.givtcp.base into the whole PredBat graph, which either killed the
    dump on the first unpicklable thing it met - an in-flight coroutine on a live system,
    "cannot pickle 'coroutine' object" - or, worse, succeeded and wrote ha_interface's access
    token and every other member is_debug_excluded_key() deliberately drops into the file
    users attach to public bug reports.

    Inverter has since stopped holding a REST client at all (GivTCPComponent owns it), so the
    original offender is gone. The guard and this test stay: the hazard is any helper object
    with a back-reference, not that one attribute, which is why both are expressed over every
    attribute rather than against "givtcp" by name. One is planted below so the test is
    exercising the guard rather than passing because there is nothing left to catch.

    Mutation check: removing the back-reference guard from create_debug_yaml() fails this.
    """
    failed = False
    print("**** Testing the inverter debug dump cannot reach the base object ****")

    original_inverters = my_predbat.inverters
    try:
        inverter = _plant_inverter(my_predbat)

        # Stand-in for the next component client someone attaches to an Inverter
        class _ClientWithBackReference:
            """A helper object holding the base, exactly as GivTCPRest used to."""

            def __init__(self, base):
                self.base = base

        inverter.some_component_client = _ClientWithBackReference(my_predbat)

        text = my_predbat.create_debug_yaml(write_file=False)
        debug = yaml.unsafe_load(text)
        dumped = debug["inverters"][0]

        for key, value in dumped.items():
            if getattr(value, "base", None) is not None:
                print("ERROR: inverter attribute '{}' carries a back-reference to {} into the dump".format(key, type(value).__name__))
                failed = True

        # The dump is still worth having - the plain data fields survive
        for key in ("id", "soc_max", "inverter_type"):
            if key not in dumped:
                print("ERROR: the guard dropped '{}', which is ordinary debug data".format(key))
                failed = True
    finally:
        my_predbat.inverters = original_inverters

    if not failed:
        print("PASS: no inverter attribute reaches the base object")
    return 1 if failed else 0


def test_debug_yaml_survives_an_unpicklable_member(my_predbat=None):
    """
    An unpicklable object behind an excluded member must not take the debug dump down with it.

    This was the live failure: "Warning: Failed to capture debug history snapshot: cannot pickle
    'coroutine' object" every capture interval, with switch.predbat_debug_enable and the web UI's
    debug download broken the same way. The coroutine is ordinary - an async component holding an
    in-flight call - and it sits on ha_interface, which is_debug_excluded_key() already drops. It
    was only fatal because a back-reference routed the dump around that exclusion, which is also
    how the access token planted alongside it here would have escaped.
    """
    failed = False
    print("**** Testing the debug yaml survives an unpicklable member behind an excluded one ****")

    async def _pending():
        """A stand-in for an in-flight component call."""
        return None

    coro = _pending()
    original_inverters = my_predbat.inverters
    original_key = getattr(my_predbat.ha_interface, "ha_key", None)
    try:
        _plant_inverter(my_predbat)
        my_predbat.ha_interface.debug_yaml_scope_pending_call = coro
        my_predbat.ha_interface.ha_key = "REAL-HA-TOKEN-DEBUG-SCOPE"

        try:
            text = my_predbat.create_debug_yaml(write_file=False)
        except Exception as e:
            print("ERROR: create_debug_yaml raised {}: {}".format(type(e).__name__, e))
            return 1

        if "REAL-HA-TOKEN-DEBUG-SCOPE" in text:
            print("ERROR: the debug yaml leaked the Home Assistant access token")
            failed = True
    finally:
        my_predbat.inverters = original_inverters
        my_predbat.ha_interface.ha_key = original_key
        my_predbat.ha_interface.__dict__.pop("debug_yaml_scope_pending_call", None)
        coro.close()

    if not failed:
        print("PASS: the debug yaml is produced without leaking the access token")
    return 1 if failed else 0


def _dict_with_aliases():
    """
    A debug-shaped dict where two top-level keys each alias the same object internally.

    Both keys need an anchor of their own, which is exactly the case a per-key dump gets
    wrong: each dumper numbers its anchors from one, so the second key repeats "&id001".
    """
    tariff = [0.1, 0.2, 0.3]
    return {
        "rate_import": {"today": tariff, "tomorrow": tariff, "windows": [tariff]},
        "rate_export": {"today": tariff, "tomorrow": tariff},
        "plan_ready": True,
    }


def test_per_key_dump_loads_as_one_document(my_predbat=None):
    """
    dump_debug_yaml() must write the dict one key at a time and still produce a file that loads.

    The dump is streamed per key so the YAML node tree in memory is bounded by the largest
    member rather than the whole dict (~150MB for a live system, whose freed pages then sit
    on the heap as fragmentation for the rest of the run). Every top-level key is written by
    a fresh Dumper, and a fresh Dumper restarts its anchor numbering, so two keys that each
    contain an alias both write "&id001" and the file fails to load with "found duplicate
    anchor". The shared anchor counter is what keeps the pieces one valid document.

    Mutation check: dropping anchor_ids from DebugYamlDumper, or replacing dump_debug_yaml()'s
    partial with plain yaml.Dumper, fails this.
    """
    failed = False
    print("**** Testing the per-key debug yaml dump loads as a single document ****")

    debug = _dict_with_aliases()

    # First confirm the hazard is real, so a pass below means the guard did something
    naive = io.StringIO()
    for key in sorted(debug):
        yaml.dump({key: debug[key]}, naive, Dumper=yaml.Dumper)
    try:
        yaml.safe_load(naive.getvalue())
        print("ERROR: a per-key dump with plain yaml.Dumper loaded, so the duplicate-anchor hazard this guards is gone")
        failed = True
    except yaml.YAMLError:
        pass

    stream = io.StringIO()
    dump_debug_yaml(debug, stream)
    text = stream.getvalue()

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as e:
        print("ERROR: the per-key debug yaml does not load: {}".format(e))
        return 1

    if loaded != debug:
        print("ERROR: the per-key debug yaml did not round-trip, got {}".format(loaded))
        failed = True
    if list(loaded) != sorted(debug):
        print("ERROR: expected the keys in sorted order, got {}".format(list(loaded)))
        failed = True

    anchors = re.findall(r"&(id\d+)", text)
    if len(anchors) < 2:
        print("ERROR: expected an anchor in each aliased key, found {}".format(anchors))
        failed = True
    if len(anchors) != len(set(anchors)):
        print("ERROR: anchor names repeat across keys: {}".format(anchors))
        failed = True

    if not failed:
        print("PASS: the per-key dump loads with {} unique anchors".format(len(anchors)))
    return 1 if failed else 0


def test_per_key_dump_bounds_the_node_tree(my_predbat=None):
    """
    The per-key dump's peak allocation must be a fraction of a whole-dict yaml.dump().

    This is the reason dump_debug_yaml() exists: yaml.dump() represents the entire dict as
    a tree of Node objects (~30x the size of the text) before emitting any of it, and that
    transient peak is what a live system pays every debug snapshot.

    Mutation check: replacing dump_debug_yaml()'s loop with yaml.dump(debug, stream) fails this.
    """
    failed = False
    print("**** Testing the per-key debug yaml dump bounds the node tree ****")

    debug = {"member_{}".format(n): [float(v) / 7 for v in range(2000)] for n in range(8)}

    def peak_of(dump):
        """Peak traced allocation while dump() writes debug to a throwaway stream."""
        tracemalloc.start()
        try:
            dump(debug, io.StringIO())
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return peak

    whole = peak_of(lambda debug, stream: yaml.dump(debug, stream, Dumper=yaml.Dumper))
    per_key = peak_of(dump_debug_yaml)

    if per_key > whole * 0.5:
        print("ERROR: per-key dump peaked at {} bytes against {} for the whole dict, expected under half".format(per_key, whole))
        failed = True

    if not failed:
        print("PASS: per-key dump peaked at {:.0f}% of the whole-dict dump".format(100.0 * per_key / whole))
    return 1 if failed else 0


def test_create_debug_yaml_file_matches_the_string(my_predbat=None):
    """
    create_debug_yaml() must produce the same loadable document whether it writes a file or returns text.

    The web UI download and the debug history capture take the string, the debug_enable switch
    writes the file; both go through dump_debug_yaml() and both are loaded back by
    read_debug_yaml() with yaml.unsafe_load(), so both have to be one valid document carrying
    the same members.
    """
    failed = False
    print("**** Testing create_debug_yaml() writes the document it returns ****")

    original_inverters = my_predbat.inverters
    filename = my_predbat.config_root + "/debug/predbat_debug_{}.yaml".format(my_predbat.now_utc.strftime("%H_%M_%S"))
    try:
        _plant_inverter(my_predbat)

        text = my_predbat.create_debug_yaml(write_file=False)
        from_text = yaml.unsafe_load(text)

        my_predbat.create_debug_yaml(write_file=True)
        with open(filename) as handle:
            from_file = yaml.unsafe_load(handle)

        for key in ("CONFIG_ITEMS", "inverters", "args"):
            if key not in from_text:
                print("ERROR: the returned debug yaml lacks '{}'".format(key))
                failed = True
        if sorted(from_text) != list(from_text):
            print("ERROR: the returned debug yaml is not in key order")
            failed = True
        if set(from_file) != set(from_text):
            print("ERROR: the written debug yaml has different members from the returned one: {}".format(set(from_file) ^ set(from_text)))
            failed = True
        if from_file["CONFIG_ITEMS"] != from_text["CONFIG_ITEMS"]:
            print("ERROR: CONFIG_ITEMS differ between the written and returned debug yaml")
            failed = True
    finally:
        my_predbat.inverters = original_inverters
        if os.path.exists(filename):
            os.remove(filename)

    if not failed:
        print("PASS: the written and returned debug yaml carry the same {} members".format(len(from_text)))
    return 1 if failed else 0


def run_debug_yaml_scope_tests(my_predbat):
    """Run every create_debug_yaml() scope test, returning a non-zero count on failure."""
    failed = 0
    failed += test_inverter_dump_does_not_reach_the_base_object(my_predbat)
    failed += test_debug_yaml_survives_an_unpicklable_member(my_predbat)
    failed += test_per_key_dump_loads_as_one_document(my_predbat)
    failed += test_per_key_dump_bounds_the_node_tree(my_predbat)
    failed += test_create_debug_yaml_file_matches_the_string(my_predbat)
    return failed

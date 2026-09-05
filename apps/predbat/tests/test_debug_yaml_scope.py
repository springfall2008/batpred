# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for what create_debug_yaml() is allowed to reach.

The debug yaml is walked with yaml.dump(), whose default Dumper serialises arbitrary
objects through __reduce_ex__(). That makes every object it emits a doorway into
whatever that object references, so the dump's contents are decided by reachability,
not by the key filter alone.
"""

import yaml

from inverter import Inverter


def _plant_inverter(my_predbat):
    """Give my_predbat one real Inverter, as a live GivTCP install has."""
    my_predbat.args["inverter_type"] = "GE"
    my_predbat.inverters = [Inverter(my_predbat, 0, quiet=True)]
    return my_predbat.inverters[0]


def test_inverter_dump_does_not_reach_the_base_object(my_predbat=None):
    """
    No inverter attribute may drag the PredBat object into the debug yaml.

    Inverter.givtcp (the GivTCP REST client) keeps a back-reference to the base object, and
    create_debug_yaml()'s inverter walk only skipped keys literally named "base*". yaml.dump()
    then followed inverter.givtcp.base into the whole PredBat graph, which either killed the
    dump on the first unpicklable thing it met - an in-flight coroutine on a live system,
    "cannot pickle 'coroutine' object" - or, worse, succeeded and wrote ha_interface's access
    token and every other member is_debug_excluded_key() deliberately drops into the file
    users attach to public bug reports.

    Asserted as an invariant over every attribute rather than against "givtcp" by name, so the
    next component client attached to an Inverter is caught here too.

    Mutation check: removing the back-reference guard from create_debug_yaml() fails this.
    """
    failed = False
    print("**** Testing the inverter debug dump cannot reach the base object ****")

    original_inverters = my_predbat.inverters
    try:
        inverter = _plant_inverter(my_predbat)

        # The client that started this: present, and genuinely holding the base object, so the
        # test cannot quietly pass because the attribute went away.
        if getattr(inverter, "givtcp", None) is None or inverter.givtcp.base is not my_predbat:
            print("ERROR: Inverter.givtcp no longer holds a back-reference - update this test's premise")
            failed = True

        text = my_predbat.create_debug_yaml(write_file=False)
        debug = yaml.unsafe_load(text)
        dumped = debug["inverters"][0]

        for key, value in dumped.items():
            if getattr(value, "base", None) is not None:
                print("ERROR: inverter attribute '{}' carries a back-reference to {} into the dump".format(key, type(value).__name__))
                failed = True

        # The dump is still worth having - the plain data fields survive
        for key in ("id", "soc_max", "rest_v3"):
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

    This is the live failure: "Warning: Failed to capture debug history snapshot: cannot pickle
    'coroutine' object" every capture interval, with switch.predbat_debug_enable and the web UI's
    debug download broken the same way. The coroutine is ordinary - an async component holding an
    in-flight call - and it sits on ha_interface, which is_debug_excluded_key() already drops. It
    was only fatal because inverter.givtcp.base routed the dump back around that exclusion, which
    is also how the access token planted alongside it here would have escaped.
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


def run_debug_yaml_scope_tests(my_predbat):
    """Run every create_debug_yaml() scope test, returning a non-zero count on failure."""
    failed = 0
    failed += test_inverter_dump_does_not_reach_the_base_object(my_predbat)
    failed += test_debug_yaml_survives_an_unpicklable_member(my_predbat)
    return failed

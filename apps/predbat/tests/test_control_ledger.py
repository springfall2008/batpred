# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test control ownership ledger
# -----------------------------------------------------------------------------

"""Tests for the control ownership ledger.

Most of these assert SILENCE. The expensive failure for this feature is telling a
customer that someone else changed their inverter when in fact our own write failed,
the read was stale, or the inverter returned garbage.
"""

from control_ledger import (
    ControlLedger,
    values_match,
    is_plausible,
    generation_from_state,
    OWNED,
    UNOWNED,
    STALE,
    IMPLAUSIBLE,
    SETTLING,
    EXTERNAL,
    UNAVAILABLE,
)
from const import INVERTER_MAX_RETRY


def test_values_match_uses_fuzzy_for_numerics():
    """A read inside the write path's tolerance is a match, not a change."""
    failed = False
    if not values_match(2950, 3000, fuzzy=130):
        print("ERROR: 2950 vs 3000 within fuzzy 130 should match")
        failed = True
    if values_match(2000, 3000, fuzzy=130):
        print("ERROR: 2000 vs 3000 outside fuzzy 130 should not match")
        failed = True
    if not values_match("23:30", "23:30"):
        print("ERROR: identical strings should match")
        failed = True
    if values_match("19:42", "23:30"):
        print("ERROR: different times should not match")
        failed = True
    assert not failed, "test_values_match_uses_fuzzy_for_numerics"


def test_is_plausible_rejects_impossible_times():
    """00:80 and 00:60 are not times - the inverter returned garbage, not a change."""
    failed = False
    for bad in ("00:80", "00:60", "25:00", "not-a-time"):
        if is_plausible("charge_start_time", bad):
            print(f"ERROR: {bad} accepted as a plausible time")
            failed = True
    for good in ("23:30", "07:00", "00:00", "23:30:00"):
        if not is_plausible("charge_start_time", good):
            print(f"ERROR: {good} rejected as implausible")
            failed = True
    if is_plausible("charge_limit", 150):
        print("ERROR: 150 accepted as a plausible percentage")
        failed = True
    if not is_plausible("charge_limit", 90):
        print("ERROR: 90 rejected as a percentage")
        failed = True
    if not is_plausible("some_unknown_control", "anything"):
        print("ERROR: unvalidated controls must default to plausible")
        failed = True
    assert not failed, "test_is_plausible_rejects_impossible_times"


def test_generation_from_state():
    """Timing metadata is parsed where present and refused where not."""
    failed = False
    if generation_from_state({"state": "23:30", "last_updated": 1234.5}) != 1234.5:
        print("ERROR: a float last_updated was not returned")
        failed = True
    if generation_from_state({"state": "23:30", "last_updated": "2026-08-21T07:07:00+00:00"}) is None:
        print("ERROR: an ISO last_updated was not parsed")
        failed = True
    if generation_from_state({"state": "23:30", "last_updated": "2026-08-21T07:07:00Z"}) is None:
        print("ERROR: a Z-suffixed ISO last_updated was not parsed")
        failed = True
    for bad in (None, "not a dict", {}, {"state": "23:30"}, {"last_updated": "gibberish"}):
        if generation_from_state(bad) is not None:
            print(f"ERROR: {bad!r} should yield None")
            failed = True
    assert not failed, "test_generation_from_state"


def test_generation_from_state_reads_engine_record_shape():
    """The record the ENGINE produces must yield a generation, not just a fabricated one.

    ha.py's update_state_item() is the only writer of the state table a raw read returns, and
    it stores {"state", "attributes", "last_changed"} - there is no "last_updated" key. Reading
    only "last_updated" made this return None on every production call, so confirmed_generation
    was always None and the freshness gate never fired, leaving the cycle counter as the sole
    defence against a vendor cloud serving a cached read of the pre-write value. Fabricated
    {"last_updated": ...} records assert the key the code wants rather than the one it will be
    handed, so this test uses the real shape and keeps a last_updated case for the raw HA API
    records that genuinely carry that key instead.
    """
    failed = False
    engine_record = {"state": "23:30", "attributes": {}, "last_changed": "2026-08-21T07:00:00+00:00"}
    generation = generation_from_state(engine_record)
    if not isinstance(generation, float):
        print(f"ERROR: the engine's own record shape yielded {generation!r}, not a usable float")
        failed = True
    if generation_from_state({"state": "23:30", "attributes": {}, "last_changed": 1234.5}) != 1234.5:
        print("ERROR: a float last_changed was not returned")
        failed = True
    # ha.py falls back to last_updated when HA supplied only that, and stores the result as
    # last_changed=None; the raw HA API records at ha.py's own fetch layer use last_updated.
    if generation_from_state({"state": "23:30", "last_updated": 1234.5}) != 1234.5:
        print("ERROR: the last_updated fallback was not honoured")
        failed = True
    if generation_from_state({"state": "23:30", "last_changed": None, "last_updated": 1234.5}) != 1234.5:
        print("ERROR: a null last_changed did not fall back to last_updated")
        failed = True
    if generation_from_state({"state": "23:30", "attributes": {}, "last_changed": None}) is not None:
        print("ERROR: a record with no usable timing metadata should yield None")
        failed = True
    assert not failed, "test_generation_from_state_reads_engine_record_shape"


def test_is_plausible_accepts_time_ranges():
    """charge_time/discharge_time hold a RANGE, not a single time.

    Inverters using inv_charge_time_format "H:M-H:M" (Solis, Solax) write one entity holding
    both ends - "01:30-05:00" from adjust_charge_window(), "00:00-00:00" to disable. Validating
    those against the single-time regex made them permanently implausible, so every real
    external change to that whole family was swallowed.
    """
    failed = False
    for control in ("charge_time", "discharge_time"):
        for good in ("01:30-05:00", "00:00-00:00", "23:30-07:00", "01:30:00-05:00:00"):
            if not is_plausible(control, good):
                print(f"ERROR: {control} rejected the valid range {good}")
                failed = True
        # A range is only as good as both halves, and a truncated read of half a range is a
        # corrupt read - not somebody shortening the window - so it must stay implausible.
        for bad in ("01:30-99:99", "99:99-05:00", "noon-05:00", "01:30", "01:30-05:00-09:00", "", "unavailable"):
            if is_plausible(control, bad):
                print(f"ERROR: {control} accepted the invalid value {bad!r}")
                failed = True
    # The single-time controls keep the stricter single-time rule.
    if not is_plausible("charge_start_time", "23:30"):
        print("ERROR: a single time was rejected for charge_start_time")
        failed = True
    if is_plausible("charge_start_time", "01:30-05:00"):
        print("ERROR: charge_start_time accepted a range, which it never holds")
        failed = True
    assert not failed, "test_is_plausible_accepts_time_ranges"


def test_unowned_never_reports():
    """A value we never confirmed is not ours to claim was taken."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    if ledger.observe("select.charge_start", "charge_start_time", "19:42") != UNOWNED:
        print("ERROR: an unconfirmed control reported something other than UNOWNED")
        failed = True
    if ledger.events:
        print(f"ERROR: an unconfirmed control produced events: {ledger.events}")
        failed = True
    assert not failed, "test_unowned_never_reports"


def test_confirmed_then_changed_is_external():
    """The core case: we set it, proved it landed, and next cycle it is something else."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    ledger.begin_cycle()
    verdict = ledger.observe("select.charge_start", "charge_start_time", "19:42", now=400.0, generation=400.0)
    if verdict != EXTERNAL:
        print(f"ERROR: expected EXTERNAL, got {verdict}")
        failed = True
    if len(ledger.events) != 1:
        print(f"ERROR: expected exactly one event, got {ledger.events}")
        failed = True
    else:
        event = ledger.events[0]
        if event["we_set"] != "23:30" or event["now_reads"] != "19:42":
            print(f"ERROR: event did not carry both values: {event}")
            failed = True
    assert not failed, "test_confirmed_then_changed_is_external"


def test_matching_read_stays_owned():
    """Reading back what we set is the normal case and must be silent."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    ledger.begin_cycle()
    if ledger.observe("select.charge_start", "charge_start_time", "23:30", now=400.0, generation=400.0) != OWNED:
        print("ERROR: an unchanged value was not reported as OWNED")
        failed = True
    if ledger.events:
        print(f"ERROR: an unchanged value produced events: {ledger.events}")
        failed = True
    assert not failed, "test_matching_read_stays_owned"


def test_failed_write_never_owns():
    """A write that did not verify cannot make us the owner of anything."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.clear("select.charge_start")
    ledger.begin_cycle()
    if ledger.observe("select.charge_start", "charge_start_time", "19:42", now=400.0, generation=400.0) != UNOWNED:
        print("ERROR: a failed write conferred ownership")
        failed = True
    assert not failed, "test_failed_write_never_owns"


def test_ignore_fail_write_never_owns():
    """ignore_fail returns success without ever polling, so the helper clears rather than records."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.clear("select.pause_start")
    ledger.begin_cycle()
    if ledger.observe("select.pause_start", "pause_start_time", "05:00", now=400.0, generation=400.0) != UNOWNED:
        print("ERROR: an ignore_fail write conferred ownership")
        failed = True
    assert not failed, "test_ignore_fail_write_never_owns"


def test_same_cycle_read_is_stale():
    """A read taken in the same cycle as the confirmation proves nothing new."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    if ledger.observe("select.charge_start", "charge_start_time", "19:42", now=110.0, generation=110.0) != STALE:
        print("ERROR: a same-cycle divergence was not treated as stale")
        failed = True
    assert not failed, "test_same_cycle_read_is_stale"


def test_older_generation_is_stale():
    """GE serves settings reads from cache, so a read older than our confirmation is not evidence."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=500.0, generation=500.0)
    ledger.begin_cycle()
    if ledger.observe("select.charge_start", "charge_start_time", "19:42", now=800.0, generation=400.0) != STALE:
        print("ERROR: a read generation older than the confirmation was not treated as stale")
        failed = True
    if ledger.events:
        print("ERROR: a stale read produced an event")
        failed = True
    assert not failed, "test_older_generation_is_stale"


def test_implausible_read_is_suppressed():
    """00:80 is a corrupt read, not a third party."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.discharge_start", "discharge_start_time", "00:00", now=100.0, generation=100.0)
    ledger.begin_cycle()
    if ledger.observe("select.discharge_start", "discharge_start_time", "00:80", now=400.0, generation=400.0) != IMPLAUSIBLE:
        print("ERROR: an impossible time was not suppressed")
        failed = True
    if ledger.events:
        print("ERROR: an impossible time produced an event")
        failed = True
    assert not failed, "test_implausible_read_is_suppressed"


def test_write_in_flight_is_settling():
    """If we wrote since the confirmation, divergence is our own change propagating."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("number.charge_limit", "charge_limit", 90, now=100.0, generation=100.0)
    ledger.begin_cycle()
    ledger.note_write_attempt("number.charge_limit")
    if ledger.observe("number.charge_limit", "charge_limit", 50, now=400.0, generation=400.0) != SETTLING:
        print("ERROR: an in-flight write was not treated as settling")
        failed = True
    assert not failed, "test_write_in_flight_is_settling"


def test_event_clears_ownership_so_it_counts_once():
    """One externally-set value must be one event, not one per cycle forever."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    for n in range(1, 6):
        ledger.begin_cycle()
        ledger.observe("select.charge_start", "charge_start_time", "19:42", now=100.0 + 300 * n, generation=100.0 + 300 * n)
    if len(ledger.events) != 1:
        print(f"ERROR: a persistent external value produced {len(ledger.events)} events, expected 1")
        failed = True
    assert not failed, "test_event_clears_ownership_so_it_counts_once"


def test_second_event_requires_reconfirmation():
    """PredBat must regain the control before losing it can count again."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    ledger.begin_cycle()
    ledger.observe("select.charge_start", "charge_start_time", "19:42", now=400.0, generation=400.0)
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=420.0, generation=420.0)
    ledger.begin_cycle()
    ledger.observe("select.charge_start", "charge_start_time", "19:42", now=700.0, generation=700.0)
    if len(ledger.events) != 2:
        print(f"ERROR: expected 2 events after a re-confirmation, got {len(ledger.events)}")
        failed = True
    assert not failed, "test_second_event_requires_reconfirmation"


def test_clear_drops_ownership():
    """Read-only or calibration means we stopped controlling; later changes are not ours to claim."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    ledger.clear("select.charge_start")
    ledger.begin_cycle()
    if ledger.observe("select.charge_start", "charge_start_time", "19:42", now=400.0, generation=400.0) != UNOWNED:
        print("ERROR: ownership survived an explicit clear")
        failed = True
    assert not failed, "test_clear_drops_ownership"


def test_echo_component_never_fires():
    """Deye/Fox/Enphase republish our own write, so the read always matches - silent, not wrong."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.deye_charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    for n in range(1, 11):
        ledger.begin_cycle()
        ledger.observe("select.deye_charge_start", "charge_start_time", "23:30", now=100.0 + 300 * n, generation=100.0 + 300 * n)
    if ledger.events:
        print(f"ERROR: an echoing component produced events: {ledger.events}")
        failed = True
    assert not failed, "test_echo_component_never_fires"


def test_shared_ems_entity_is_one_record():
    """A GE EMS assigns one entity to every inverter index; our own write must not look external."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    # Inverter 0 and inverter 1 both drive the same entity id.
    ledger.record_write("select.ems_charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
    ledger.record_write("select.ems_charge_start", "charge_start_time", "01:00", now=110.0, generation=110.0)
    ledger.begin_cycle()
    if ledger.observe("select.ems_charge_start", "charge_start_time", "01:00", now=400.0, generation=400.0) != OWNED:
        print("ERROR: the second inverter's own write was not recognised as ours")
        failed = True
    if ledger.events:
        print("ERROR: PredBat's own write on a shared EMS entity was reported as external")
        failed = True
    assert not failed, "test_shared_ems_entity_is_one_record"


def test_unavailable_read_is_suppressed():
    """An entity that dropped out is not a third party changing the setting.

    inverter_mode has no plausibility rule, so without an explicit rung this falls all the
    way through to EXTERNAL and accuses somebody of an outage.
    """
    failed = False
    for dropout in (None, "unavailable", "unknown", "", "None"):
        ledger = ControlLedger()
        ledger.begin_cycle()
        ledger.record_write("select.inverter_mode", "inverter_mode", "Eco", now=100.0, generation=100.0)
        ledger.begin_cycle()
        verdict = ledger.observe("select.inverter_mode", "inverter_mode", dropout, now=400.0, generation=400.0)
        if verdict != UNAVAILABLE:
            print(f"ERROR: a read of {dropout!r} gave {verdict}, expected UNAVAILABLE")
            failed = True
        if ledger.events:
            print(f"ERROR: a read of {dropout!r} produced an event: {ledger.events}")
            failed = True
    assert not failed, "test_unavailable_read_is_suppressed"


def test_unavailable_readback_never_owns():
    """Confirming that the inverter holds "unavailable" is meaningless, so it cannot own."""
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("select.inverter_mode", "inverter_mode", "unavailable", now=100.0, generation=100.0)
    ledger.begin_cycle()
    if ledger.observe("select.inverter_mode", "inverter_mode", "Eco", now=400.0, generation=400.0) != UNOWNED:
        print("ERROR: an unavailable read-back conferred ownership")
        failed = True
    assert not failed, "test_unavailable_readback_never_owns"


def test_ledger_generation_reads_last_updated():
    """The generation helper pulls timing metadata off the raw HA state record."""
    failed = False
    from inverter import Inverter

    class _Base:
        def __init__(self, entities):
            self.entities = entities

        def get_state_wrapper(self, entity_id, raw=False, **kwargs):
            record = self.entities.get(entity_id)
            return record if raw else (record.get("state") if record else None)

    class _Stub:
        def __init__(self, entities):
            self.base = _Base(entities)

    stub = _Stub({"select.charge_start": {"state": "23:30", "last_updated": 1234.5}})
    if Inverter._ledger_generation(stub, "select.charge_start") != 1234.5:
        print("ERROR: last_updated was not returned")
        failed = True
    if Inverter._ledger_generation(stub, "select.missing") is not None:
        print("ERROR: a missing entity should yield None")
        failed = True

    class _Raiser:
        base = type("B", (), {"get_state_wrapper": lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))})()

    if Inverter._ledger_generation(_Raiser(), "select.charge_start") is not None:
        print("ERROR: a raising state read should yield None, not propagate")
        failed = True
    assert not failed, "test_ledger_generation_reads_last_updated"


class _RecordingLedger:
    """Records the ledger calls a write helper makes, so the wiring itself can be asserted.

    The manufacturer suites all run with control_ledger unset, so they only ever exercise the
    `ledger is None` no-op branches - they would pass identically against wiring that called
    observe()/record_write() with swapped arguments, the wrong value, or not at all. This fake
    is what actually watches the hooks fire.
    """

    def __init__(self):
        self.calls = []

    def owned_value(self, entity_id):
        return None

    def observe(self, entity_id, control, value, now=0.0, generation=None):
        self.calls.append(("observe", entity_id, control, value))
        return UNOWNED

    def note_write_attempt(self, entity_id):
        self.calls.append(("note_write_attempt", entity_id))

    def record_write(self, entity_id, control, read_back, fuzzy=0, now=0.0, generation=None):
        self.calls.append(("record_write", entity_id, control, read_back))

    def clear(self, entity_id=None):
        self.calls.append(("clear", entity_id))


class _ScriptedBase:
    """Minimal stand-in for the predbat instance a write helper reaches through self.base.

    raw=True calls (from _ledger_generation) get a record in the shape ha.py's
    update_state_item() actually stores - state, attributes and last_changed - rather than an
    invented one, so a helper driven through this fake exercises the same generation-parsing
    path production does. `generation` is per-instance so a test can run one phase of a
    scenario, then a later phase with a NEWER generation; leaving it fixed would let the
    ledger's freshness gate suppress everything and make a dropout test pass for the wrong
    reason. Plain calls pop through a scripted sequence of state reads and then repeat the
    last one, so a test only has to script the values it cares about and a retry loop can run
    to exhaustion without running out of script. non_raw_reads counts every non-raw call, so a
    test can pin the exact number of live state fetches a helper makes - the only way to catch
    a superfluous extra fetch when the fake would otherwise return the same repeated value
    either way.
    """

    def __init__(self, state_sequence, ledger=None, generation=1000.0):
        self._state_sequence = list(state_sequence)
        self.control_ledger = ledger
        self.generation = generation
        self.non_raw_reads = 0
        # call_service_template() reaches for these on self.base.
        self.args = {}
        self.last_service_hash = {}
        # Control name -> entity id, for helpers that resolve a control by name.
        self.arg_entities = {}
        self.logs = []

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        if raw:
            return {"state": self._state_sequence[0], "attributes": {}, "last_changed": self.generation}
        self.non_raw_reads += 1
        if len(self._state_sequence) > 1:
            return self._state_sequence.pop(0)
        return self._state_sequence[0]

    def log(self, msg):
        self.logs.append(msg)

    def get_arg(self, name, indirect=True, index=None, default=None, **kwargs):
        return self.arg_entities.get(name, default)

    def record_status(self, msg, had_errors=False):
        pass

    def set_state_wrapper(self, entity_id=None, state=None, attributes=None, required_unit=None):
        pass

    def call_service_wrapper(self, service, **kwargs):
        pass

    def unit_conversion(self, entity_id, state, units, required_unit, going_to=False):
        return state

    def resolve_arg(self, template, value, indirect=True, index=None, default="", extra_args=None):
        return value


def _stub_inverter(state_sequence, ledger=None, generation=1000.0):
    """Build a real (uninitialised) Inverter instance wired to a scripted base.

    Using Inverter.__new__ rather than a duck-typed class means self._ledger_generation(...)
    inside the helpers resolves to the real bound method, not a hand-copied stand-in - the same
    reasoning that ruled out copying _ledger_generation into a test fake applies to the helpers
    that call it internally.
    """
    from inverter import Inverter

    stub = Inverter.__new__(Inverter)
    stub.base = _ScriptedBase(state_sequence, ledger=ledger, generation=generation)
    stub.log = stub.base.log
    stub.id = 0
    stub.count_register_writes = 0
    stub.created_attributes = {}
    stub.inv_write_and_poll_sleep = 0
    stub.sleep = lambda seconds: None
    stub.inv_has_mqtt_api = False
    stub.inv_mqtt_topic = "test"
    return stub


def test_wiring_no_write_needed_skips_note_and_record():
    """A read that already matches must not be treated as a write attempt.

    This is the property that matters most: if note_write_attempt fired here with no matching
    record_write, the control would be stuck reporting SETTLING forever (correction 2 in the
    task brief). write_and_poll_option has no such fast path - it always issues at least one
    write regardless of whether old_value already equals new_value - so this property does not
    apply to it.
    """
    failed = False
    ledger = _RecordingLedger()
    stub = _stub_inverter([True], ledger=ledger)
    if not stub.write_and_poll_switch("enable", "switch.charge_enable", True):
        print("ERROR: write_and_poll_switch should report success with nothing to do")
        failed = True
    if ledger.calls != [("observe", "switch.charge_enable", "enable", True)]:
        print(f"ERROR: write_and_poll_switch no-write-needed call list was {ledger.calls}")
        failed = True

    ledger = _RecordingLedger()
    stub = _stub_inverter([50.0], ledger=ledger)
    if not stub.write_and_poll_value("charge_limit", "number.charge_limit", 50.0, fuzzy=0):
        print("ERROR: write_and_poll_value should report success with nothing to do")
        failed = True
    if ledger.calls != [("observe", "number.charge_limit", "charge_limit", 50.0)]:
        print(f"ERROR: write_and_poll_value no-write-needed call list was {ledger.calls}")
        failed = True

    assert not failed, "test_wiring_no_write_needed_skips_note_and_record"


def test_wiring_successful_write_records_actual_read_back():
    """record_write must carry the value actually read back, not the value requested.

    write_and_poll_value's fuzzy tolerance is the only one of the three helpers where a
    successful write can read back something other than new_value - switch and option only
    ever succeed on an exact match, so their read_back is structurally equal to new_value at
    that point and cannot be distinguished from it there. Their assertions below still confirm
    record_write fires with the correct value, just not that it differs from new_value.

    write_and_poll_switch records the RAW read ("on"), not the coerced bool it compares
    against new_value. The owned value has to be the same shape as the reads it will later be
    compared with: storing True and then observing "on" makes every subsequent healthy read
    look like a change, which is PredBat reporting its own successful write as a third party.
    """
    failed = False
    ledger = _RecordingLedger()
    stub = _stub_inverter([2000.0, 2950.0], ledger=ledger)
    if not stub.write_and_poll_value("charge_rate", "number.charge_rate", 3000, fuzzy=130):
        print("ERROR: write_and_poll_value should report success")
        failed = True
    record_calls = [c for c in ledger.calls if c[0] == "record_write"]
    if record_calls != [("record_write", "number.charge_rate", "charge_rate", 2950.0)]:
        print(f"ERROR: write_and_poll_value success record_write was {record_calls}")
        failed = True
    if record_calls and record_calls[0][3] == 3000:
        print("ERROR: record_write was passed the requested value, not the read-back")
        failed = True

    ledger = _RecordingLedger()
    stub = _stub_inverter(["off", "on"], ledger=ledger)
    if not stub.write_and_poll_switch("enable", "switch.charge_enable", True):
        print("ERROR: write_and_poll_switch should report success")
        failed = True
    record_calls = [c for c in ledger.calls if c[0] == "record_write"]
    if record_calls != [("record_write", "switch.charge_enable", "enable", "on")]:
        print(f"ERROR: write_and_poll_switch success record_write was {record_calls}")
        failed = True

    ledger = _RecordingLedger()
    stub = _stub_inverter(["Normal", "Eco"], ledger=ledger)
    if not stub.write_and_poll_option("inverter_mode", "select.inverter_mode", "Eco"):
        print("ERROR: write_and_poll_option should report success")
        failed = True
    record_calls = [c for c in ledger.calls if c[0] == "record_write"]
    if record_calls != [("record_write", "select.inverter_mode", "inverter_mode", "Eco")]:
        print(f"ERROR: write_and_poll_option success record_write was {record_calls}")
        failed = True

    assert not failed, "test_wiring_successful_write_records_actual_read_back"


def test_wiring_ignore_fail_drops_ownership():
    """ignore_fail returns success without ever polling, so it must not confer ownership.

    write_and_poll_switch has no ignore_fail parameter at all, so this property applies only
    to write_and_poll_value and write_and_poll_option.
    """
    failed = False
    ledger = _RecordingLedger()
    stub = _stub_inverter([10.0], ledger=ledger)
    if not stub.write_and_poll_value("charge_limit", "number.charge_limit", 50.0, fuzzy=0, ignore_fail=True):
        print("ERROR: write_and_poll_value with ignore_fail should report success")
        failed = True
    if ledger.calls != [
        ("observe", "number.charge_limit", "charge_limit", 10.0),
        ("note_write_attempt", "number.charge_limit"),
        ("clear", "number.charge_limit"),
    ]:
        print(f"ERROR: write_and_poll_value ignore_fail call list was {ledger.calls}")
        failed = True

    ledger = _RecordingLedger()
    stub = _stub_inverter(["Normal"], ledger=ledger)
    if not stub.write_and_poll_option("inverter_mode", "select.inverter_mode", "Eco", ignore_fail=True):
        print("ERROR: write_and_poll_option with ignore_fail should report success")
        failed = True
    if ledger.calls != [
        ("observe", "select.inverter_mode", "inverter_mode", "Normal"),
        ("note_write_attempt", "select.inverter_mode"),
        ("clear", "select.inverter_mode"),
    ]:
        print(f"ERROR: write_and_poll_option ignore_fail call list was {ledger.calls}")
        failed = True

    assert not failed, "test_wiring_ignore_fail_drops_ownership"


def test_wiring_failed_write_clears_ownership():
    """A write that never verifies must CLEAR ownership, not record anything.

    Expressing this as record_write(read_back=..., ok=False) passed the ledger a value it
    then discarded; clear(entity_id) says what is meant. Either way the property under test is
    the same and is the one that matters: a control PredBat has just failed to set must not be
    left owned, or the next read of it is reported as somebody else's change.

    The write_and_poll_option assertion also pins the fix for the superfluous live fetch an
    earlier round found - non_raw_reads pins the exact number of live state reads (1 initial +
    one per retry + 1 in the failure log line), so a reintroduced extra fetch there fails this
    test even though the fake would return the same value either way.
    """
    failed = False
    for label, entity_id, first_read, drive in (
        ("write_and_poll_switch", "switch.charge_enable", False, lambda stub, e="switch.charge_enable": stub.write_and_poll_switch("enable", e, True)),
        ("write_and_poll_value", "number.charge_limit", 10.0, lambda stub, e="number.charge_limit": stub.write_and_poll_value("charge_limit", e, 50.0, fuzzy=0)),
        ("write_and_poll_option", "select.inverter_mode", "Normal", lambda stub, e="select.inverter_mode": stub.write_and_poll_option("inverter_mode", e, "Eco")),
    ):
        ledger = _RecordingLedger()
        stub = _stub_inverter([first_read], ledger=ledger)
        if drive(stub):
            print(f"ERROR: {label} should report failure when the read-back never matches")
            failed = True
        if [c for c in ledger.calls if c[0] == "record_write"]:
            print(f"ERROR: {label} recorded ownership for a write that never verified: {ledger.calls}")
            failed = True
        if ("clear", entity_id) not in ledger.calls:
            print(f"ERROR: {label} failure did not clear ownership: {ledger.calls}")
            failed = True
        if label == "write_and_poll_option":
            expected_reads = INVERTER_MAX_RETRY + 2  # 1 initial + one per retry + 1 in the failure log line
            if stub.base.non_raw_reads != expected_reads:
                print(f"ERROR: write_and_poll_option failure path made {stub.base.non_raw_reads} live state reads, expected {expected_reads} - a superfluous extra fetch has crept back in")
                failed = True

    assert not failed, "test_wiring_failed_write_clears_ownership"


def test_wiring_observe_precedes_note_write_attempt():
    """The read being classified must not be contaminated by the write-attempt flag it is
    about to set - observe() must be called, and recorded, before note_write_attempt() for
    the same entity.
    """
    failed = False
    ledger = _RecordingLedger()
    stub = _stub_inverter(["off", "on"], ledger=ledger)
    stub.write_and_poll_switch("enable", "switch.charge_enable", True)
    order = [c[0] for c in ledger.calls]
    if "observe" not in order or "note_write_attempt" not in order or order.index("observe") > order.index("note_write_attempt"):
        print(f"ERROR: write_and_poll_switch call order was {ledger.calls}")
        failed = True

    ledger = _RecordingLedger()
    stub = _stub_inverter([2000.0, 2950.0], ledger=ledger)
    stub.write_and_poll_value("charge_rate", "number.charge_rate", 3000, fuzzy=130)
    order = [c[0] for c in ledger.calls]
    if "observe" not in order or "note_write_attempt" not in order or order.index("observe") > order.index("note_write_attempt"):
        print(f"ERROR: write_and_poll_value call order was {ledger.calls}")
        failed = True

    ledger = _RecordingLedger()
    stub = _stub_inverter(["Normal", "Eco"], ledger=ledger)
    stub.write_and_poll_option("inverter_mode", "select.inverter_mode", "Eco")
    order = [c[0] for c in ledger.calls]
    if "observe" not in order or "note_write_attempt" not in order or order.index("observe") > order.index("note_write_attempt"):
        print(f"ERROR: write_and_poll_option call order was {ledger.calls}")
        failed = True

    assert not failed, "test_wiring_observe_precedes_note_write_attempt"


def test_wiring_dropout_read_is_never_reported_external():
    """An entity dropout must produce NO event, driven through the REAL write helpers.

    write_and_poll_value coerces a failed read - "unavailable", "unknown", a missing entity -
    to 0.0 before comparing it to new_value, and write_and_poll_switch maps the same reads to
    False. Both are plausible-looking values: 0.0 passes is_reading(), is a valid percentage,
    and rates have no plausibility rule at all, so a coerced dropout walks every suppression
    rung and lands on EXTERNAL. Customer-facing that reads as "a third party set your charge
    rate to 0 W", manufactured by a routine integration dropout.

    Ownership is established through the helper itself rather than a hand-written
    record_write, so the owned value has exactly the shape production stores - a fabricated
    record could match a coerced read by luck and hide the bug.
    """
    failed = False
    for dropout in ("unavailable", "unknown", None):
        ledger = ControlLedger()
        ledger.begin_cycle()
        # Reads 2000, writes 3000, reads back 2950 (inside fuzzy) - confirmed and owned.
        stub = _stub_inverter([2000.0, 2950.0], ledger=ledger, generation=1000.0)
        stub.write_and_poll_value("charge_rate", "number.charge_rate", 3000, fuzzy=130)
        if "number.charge_rate" not in ledger.records:
            print(f"ERROR: the confirming write did not confer ownership, so {dropout!r} proves nothing")
            failed = True
        # Next cycle, on a NEWER generation so the freshness gate cannot be what suppresses it.
        ledger.begin_cycle()
        stub = _stub_inverter([dropout], ledger=ledger, generation=2000.0)
        stub.write_and_poll_value("charge_rate", "number.charge_rate", 3000, fuzzy=130)
        if ledger.events:
            print(f"ERROR: a numeric read of {dropout!r} was reported as external: {ledger.events}")
            failed = True

        ledger = ControlLedger()
        ledger.begin_cycle()
        stub = _stub_inverter(["off", "on"], ledger=ledger, generation=1000.0)
        stub.write_and_poll_switch("enable", "switch.charge_enable", True)
        if "switch.charge_enable" not in ledger.records:
            print(f"ERROR: the confirming switch write did not confer ownership, so {dropout!r} proves nothing")
            failed = True
        ledger.begin_cycle()
        stub = _stub_inverter([dropout], ledger=ledger, generation=2000.0)
        stub.write_and_poll_switch("enable", "switch.charge_enable", True)
        if ledger.events:
            print(f"ERROR: a switch read of {dropout!r} was reported as external: {ledger.events}")
            failed = True
    assert not failed, "test_wiring_dropout_read_is_never_reported_external"


def test_wiring_freshness_gate_survives_a_long_quiet_interval():
    """A divergence an hour and a dozen cycles after the confirmation must still report.

    Fox is not an echo component the way Deye is - compute_schedule() rebuilds local_schedule
    from real vendor scheduler data, and the settings poll is hourly, so vendor state can reach
    the control entity up to an hour after Predbat's write. That makes the freshness gate
    load-bearing for Fox rather than incidental, and it has to cut both ways: suppress a read
    OLDER than the confirmation, and pass a genuinely newer one however long the wait.

    The quiet cycles in the middle deliberately keep last_changed pinned at the confirmation
    time, because Home Assistant only moves it when the value actually changes - so the gate is
    being fed a generation EQUAL to its confirmation for an hour, and ownership has to survive
    that intact. Generations are ISO strings here, which is what ha.py actually stores.
    """
    failed = False
    confirmed_at = "2026-08-21T07:00:00+00:00"
    hourly_poll = "2026-08-21T07:55:00+00:00"  # vendor state finally reaches the entity
    cached_read = "2026-08-21T06:30:00+00:00"  # a read older than our confirmation

    for divergent_generation, expected_events, label in ((hourly_poll, 1, "newer"), (cached_read, 0, "older")):
        ledger = ControlLedger()
        ledger.begin_cycle()
        stub = _stub_inverter([50.0, 90.0], ledger=ledger, generation=confirmed_at)
        stub.write_and_poll_value("charge_limit", "number.charge_limit", 90, fuzzy=0)
        # An hour of 5-minute runs with nothing changing. The value matches, so no write is
        # needed and last_changed never moves - ownership must ride through all of it.
        for _ in range(11):
            ledger.begin_cycle()
            stub = _stub_inverter([90.0], ledger=ledger, generation=confirmed_at)
            stub.write_and_poll_value("charge_limit", "number.charge_limit", 90, fuzzy=0)
        if ledger.events:
            print(f"ERROR: eleven unchanged cycles produced events: {ledger.events}")
            failed = True
        if "number.charge_limit" not in ledger.records:
            print("ERROR: ownership did not survive an hour of unchanged reads, so the case below proves nothing")
            failed = True
        ledger.begin_cycle()
        stub = _stub_inverter([20.0, 90.0], ledger=ledger, generation=divergent_generation)
        stub.write_and_poll_value("charge_limit", "number.charge_limit", 90, fuzzy=0)
        if len(ledger.events) != expected_events:
            print(f"ERROR: a divergent read on a {label} last_changed produced {len(ledger.events)} events, expected {expected_events}")
            failed = True
    assert not failed, "test_wiring_freshness_gate_survives_a_long_quiet_interval"


def test_wiring_genuine_external_change_still_reports():
    """The suppression above must not have been bought by going blind.

    Every other fix in this area moves toward silence, so the one test that has to fail loudly
    if they went too far is the one that drives a real third-party change through the real
    helpers and demands an event.
    """
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    stub = _stub_inverter([2000.0, 2950.0], ledger=ledger, generation=1000.0)
    stub.write_and_poll_value("charge_rate", "number.charge_rate", 3000, fuzzy=130)
    ledger.begin_cycle()
    # Somebody has dropped the rate to 500 W between cycles.
    stub = _stub_inverter([500.0, 2950.0], ledger=ledger, generation=2000.0)
    stub.write_and_poll_value("charge_rate", "number.charge_rate", 3000, fuzzy=130)
    if len(ledger.events) != 1:
        print(f"ERROR: a genuine external numeric change produced {len(ledger.events)} events, expected 1")
        failed = True

    ledger = ControlLedger()
    ledger.begin_cycle()
    stub = _stub_inverter(["off", "on"], ledger=ledger, generation=1000.0)
    stub.write_and_poll_switch("enable", "switch.charge_enable", True)
    ledger.begin_cycle()
    # Somebody has turned the switch back off between cycles.
    stub = _stub_inverter(["off", "on"], ledger=ledger, generation=2000.0)
    stub.write_and_poll_switch("enable", "switch.charge_enable", True)
    if len(ledger.events) != 1:
        print(f"ERROR: a genuine external switch change produced {len(ledger.events)} events, expected 1")
        failed = True
    else:
        event = ledger.events[0]
        if event["we_set"] != "on" or event["now_reads"] != "off":
            print(f"ERROR: the switch event did not carry the raw read on both sides: {event}")
            failed = True

    # And a healthy unchanged read must still be silent, which is what proves the owned value
    # and the later read are the same shape rather than bool-vs-string.
    ledger = ControlLedger()
    ledger.begin_cycle()
    stub = _stub_inverter(["off", "on"], ledger=ledger, generation=1000.0)
    stub.write_and_poll_switch("enable", "switch.charge_enable", True)
    ledger.begin_cycle()
    stub = _stub_inverter(["on"], ledger=ledger, generation=2000.0)
    stub.write_and_poll_switch("enable", "switch.charge_enable", True)
    if ledger.events:
        print(f"ERROR: an unchanged healthy switch read was reported as external: {ledger.events}")
        failed = True
    assert not failed, "test_wiring_genuine_external_change_still_reports"


def test_wiring_other_write_paths_leave_ownership_alone():
    """Predbat's OTHER write paths must not touch the ledger at all.

    Three mechanisms were tried here - a whole-ledger clear, a narrowed clear, and a deferred
    invalidation - each against theoretically-constructed configs, and each produced its own crop
    of defects. None is needed, because the detection pattern is transport-agnostic: read, write,
    confirm, and report a later differing read. HOW Predbat drove the inverter does not enter into
    it.

    An MQTT publish reaches a broker topic and no Home Assistant entity, so it has no bearing at
    all. A service template that writes the value Predbat asked for produces no divergence and so
    reports nothing on its own; a template that writes something ELSE is a genuine disagreement
    between the user's automation and Predbat, which is a finding worth surfacing rather than a
    false positive to suppress.
    """
    failed = False

    def _owned():
        ledger = ControlLedger()
        ledger.begin_cycle()
        ledger.record_write("select.charge_start", "charge_start_time", "23:30", now=100.0, generation=100.0)
        return ledger

    for label, drive in (
        ("a configured service template", lambda stub: stub.call_service_template("charge_start_service", {"device_id": "abc"}, domain="charge")),
        ("an MQTT publish", lambda stub: stub.mqtt_message("set/charge", payload=3000)),
    ):
        ledger = _owned()
        stub = _stub_inverter(["23:30"], ledger=ledger)
        stub.base.args = {"charge_start_service": "script.my_charge_start"}
        stub.inv_has_mqtt_api = True
        drive(stub)
        if "select.charge_start" not in ledger.records:
            print(f"ERROR: {label} dropped ownership the write helpers had confirmed")
            failed = True
        # And the next cycle is live, not suppressed.
        ledger.begin_cycle()
        if ledger.observe("select.charge_start", "charge_start_time", "19:42", now=200.0, generation=200.0) != EXTERNAL:
            print(f"ERROR: {label} suppressed the following cycle")
            failed = True

    # A template that writes the value Predbat asked for is silent on its own merits - no
    # suppression needed, because there is no divergence to explain away.
    ledger = _owned()
    stub = _stub_inverter(["23:30"], ledger=ledger)
    stub.base.args = {"charge_start_service": "script.my_charge_start"}
    stub.call_service_template("charge_start_service", {"device_id": "abc"}, domain="charge")
    ledger.begin_cycle()
    if ledger.observe("select.charge_start", "charge_start_time", "23:30", now=200.0, generation=200.0) != OWNED:
        print("ERROR: a template that wrote the value Predbat asked for was not silent")
        failed = True
    if ledger.events:
        print(f"ERROR: an agreeing template produced an event: {ledger.events}")
        failed = True

    # call_service_template still reports whether it called anything - unchanged behaviour.
    ledger = _owned()
    stub = _stub_inverter(["23:30"], ledger=ledger)
    stub.base.args = {"charge_start_service": "script.my_charge_start"}
    if not stub.call_service_template("charge_start_service", {"device_id": "abc"}, domain="charge"):
        print("ERROR: call_service_template should report that it called something")
        failed = True
    stub = _stub_inverter(["23:30"], ledger=ledger)
    if stub.call_service_template("charge_start_service", {"device_id": "abc"}, domain="charge"):
        print("ERROR: call_service_template should report that an unconfigured service called nothing")
        failed = True
    assert not failed, "test_wiring_other_write_paths_leave_ownership_alone"


def test_clear_drops_every_control():
    """Calibration and read-only clear the WHOLE ledger, not one entity.

    Calibration writes charge rate, battery target and reserve through the ordinary helpers,
    so they confer ownership - in exactly the mode where the inverter's own firmware is driving
    its settings and a value found moved next cycle is the inverter, not a third party. That
    branch (and read-only, belt and braces) therefore calls clear() with no argument, so this
    pins that the no-argument form drops everything rather than only the last control written.
    """
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("number.charge_rate", "charge_rate", 3000, now=100.0, generation=100.0)
    ledger.record_write("number.charge_limit", "charge_limit", 100.0, now=100.0, generation=100.0)
    ledger.record_write("number.reserve", "reserve", 0.0, now=100.0, generation=100.0)
    ledger.clear()
    if ledger.records:
        print(f"ERROR: clear() left records behind: {ledger.records}")
        failed = True
    ledger.begin_cycle()
    for entity_id, control, value in [("number.charge_rate", "charge_rate", 500), ("number.charge_limit", "charge_limit", 20.0), ("number.reserve", "reserve", 50.0)]:
        if ledger.observe(entity_id, control, value, now=400.0, generation=400.0) != UNOWNED:
            print(f"ERROR: ownership of {entity_id} survived a whole-ledger clear")
            failed = True
    if ledger.events:
        print(f"ERROR: a cleared ledger still produced events: {ledger.events}")
        failed = True
    assert not failed, "test_clear_drops_every_control"


def test_restore_orders_events_oldest_first():
    """restore() runs AFTER execute_plan(), so the newest event is the one at risk.

    predbat.py appends this cycle's events during the run and only then restores history, then
    publishes recent_events()[-20:]. Concatenating history onto the end leaves the newest event
    FIRST, so on the first run after a restart with a full 24h of history that tail publishes
    the 20 oldest and silently discards the very event the customer-facing copy quotes.
    """
    failed = False
    ledger = ControlLedger()
    # This cycle's event, already appended by execute_plan() before restore() runs.
    ledger.events.append({"at": 100000.0, "control": "charge_start_time", "entity_id": "e_new", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 99000.0})
    ledger.restore([{"at": 20000.0 + n, "control": "charge_start_time", "entity_id": f"h{n}", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 19000.0} for n in range(20)])
    if [event["at"] for event in ledger.events] != sorted(event["at"] for event in ledger.events):
        print(f"ERROR: restored events are not oldest-first: {[e['entity_id'] for e in ledger.events]}")
        failed = True
    published = ledger.recent_events(now=100000.0, window_s=86400)[-20:]
    if not any(event["entity_id"] == "e_new" for event in published):
        print(f"ERROR: the newest event was dropped by the 20-event publish cap: {[e['entity_id'] for e in published]}")
        failed = True

    # History that interleaves with events already held must land in the right places.
    ledger = ControlLedger()
    ledger.events.append({"at": 500.0, "control": "reserve", "entity_id": "live"})
    ledger.restore([{"at": 900.0, "entity_id": "later"}, {"at": 100.0, "entity_id": "earlier"}])
    if [event["entity_id"] for event in ledger.events] != ["earlier", "live", "later"]:
        print(f"ERROR: restore did not interleave by time: {[e['entity_id'] for e in ledger.events]}")
        failed = True

    # An "at" that survived a round trip through HA as something unsortable must not raise -
    # a crash here would take out the whole plan run.
    ledger = ControlLedger()
    ledger.events.append({"at": 5.0, "entity_id": "live"})
    try:
        ledger.restore([{"at": "not-a-timestamp", "entity_id": "junk"}, {"at": None, "entity_id": "null"}, {"at": 1.0, "entity_id": "early"}])
    except Exception as e:
        print(f"ERROR: sorting a malformed 'at' raised: {e}")
        failed = True
    if len(ledger.events) != 4:
        print(f"ERROR: expected all 4 events to survive the sort, got {len(ledger.events)}")
        failed = True
    assert not failed, "test_restore_orders_events_oldest_first"


def test_recent_events_filters_by_window():
    """Only events inside the rolling window count."""
    failed = False
    ledger = ControlLedger()
    ledger.events = [
        {"at": 1000.0, "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 900.0},
        {"at": 90000.0, "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 89000.0},
    ]
    recent = ledger.recent_events(now=90000.0, window_s=86400)
    if len(recent) != 1:
        print(f"ERROR: expected 1 event inside the window, got {len(recent)}")
        failed = True
    assert not failed, "test_recent_events_filters_by_window"


def test_restore_rehydrates_and_filters():
    """The 24h window survives a restart by reloading the entity's own attributes."""
    failed = False
    ledger = ControlLedger()
    ledger.restore(
        [
            {"at": 1000.0, "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 900.0},
            {"at": 89000.0, "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 88000.0},
            "not a dict",
            {"missing": "at"},
        ]
    )
    if len(ledger.events) != 2:
        print(f"ERROR: expected 2 restored events, got {len(ledger.events)}")
        failed = True
    ledger.restore(None)
    if len(ledger.events) != 2:
        print("ERROR: restoring None should be a no-op, not a wipe")
        failed = True
    assert not failed, "test_restore_rehydrates_and_filters"


def test_restore_ignores_non_list_attribute():
    """A corrupt read-back of the "events" attribute must not crash the plan run.

    The attribute round-trips through Home Assistant, so restore() may be handed
    anything - here a bare int, which a naive `for event in events` would raise on.
    """
    failed = False
    ledger = ControlLedger()
    for garbage in (42, {"not": "a list"}, "a bare string"):
        try:
            ledger.restore(garbage)
        except Exception as e:
            print(f"ERROR: restore({garbage!r}) raised: {e}")
            failed = True
    if ledger.events:
        print(f"ERROR: non-list input should not populate events, got {ledger.events}")
        failed = True
    assert not failed, "test_restore_ignores_non_list_attribute"


def test_sustained_lists_repeat_offenders():
    """Three or more events on one control in the window is the reportable state."""
    failed = False
    ledger = ControlLedger()
    ledger.events = [{"at": 1000.0 + n, "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 900.0} for n in range(3)]
    ledger.events.append({"at": 1010.0, "control": "reserve", "entity_id": "e2", "we_set": 4, "now_reads": 50, "confirmed_at": 900.0})
    sustained = ledger.sustained_controls(ledger.recent_events(now=1100.0), threshold=3)
    if sustained != ["charge_start_time"]:
        print(f"ERROR: expected only charge_start_time sustained, got {sustained}")
        failed = True
    assert not failed, "test_sustained_lists_repeat_offenders"


def test_restored_event_with_string_at_does_not_raise():
    """A restored event round-tripped through HA is not guaranteed to keep its type.

    restore() only checks for a dict with an "at" key, so a malformed "at" (a
    non-numeric string, here) can still land in self.events. recent_events(), prune()
    and sustained_controls() must not raise on it - a crash here would take out the
    whole plan run, which is far worse than losing one event's history.
    """
    failed = False
    ledger = ControlLedger()
    ledger.restore(
        [
            {"at": "not-a-timestamp", "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 900.0},
            {"at": "1000.0", "control": "charge_start_time", "entity_id": "e1", "we_set": "23:30", "now_reads": "19:42", "confirmed_at": 900.0},
        ]
    )
    if len(ledger.events) != 2:
        print(f"ERROR: expected both malformed-but-dict events to be restored, got {len(ledger.events)}")
        failed = True
    try:
        recent = ledger.recent_events(now=1000.0, window_s=86400)
        sustained = ledger.sustained_controls(ledger.recent_events(now=1000.0))
        ledger.prune(now=1000.0, window_s=86400)
    except Exception as e:
        print(f"ERROR: window methods raised on a string 'at': {e}")
        failed = True
        recent = []
        sustained = []
    # The non-numeric string can never be judged inside the window, so it is dropped
    # rather than trusted. The numeric-looking string ("1000.0") is still usable.
    if len(recent) != 1 or recent[0]["at"] != "1000.0":
        print(f"ERROR: expected only the numeric-string event to survive, got {recent}")
        failed = True
    if sustained:
        print(f"ERROR: expected no sustained controls from a single surviving event, got {sustained}")
        failed = True
    assert not failed, "test_restored_event_with_string_at_does_not_raise"


def test_sustained_controls_survives_event_missing_control_key():
    """restore() only requires an "at" key, so a restored event can carry no "control".

    sustained_controls() sorts control names; Python 3 refuses to order None against a
    str, so a mix of a control=None event and a real control name must not crash the
    whole plan run.
    """
    failed = False
    ledger = ControlLedger()
    ledger.restore(
        [
            {"at": 100.0},
            {"at": 101.0},
            {"at": 102.0},
            {"at": 103.0, "control": "charge_start_time"},
            {"at": 104.0, "control": "charge_start_time"},
            {"at": 105.0, "control": "charge_start_time"},
        ]
    )
    try:
        sustained = ledger.sustained_controls(ledger.recent_events(now=200.0), threshold=3)
    except Exception as e:
        print(f"ERROR: sustained_controls raised on a control=None event: {e}")
        failed = True
        sustained = []
    if "charge_start_time" not in sustained:
        print(f"ERROR: expected charge_start_time to still be reported, got {sustained}")
        failed = True
    # The list is written straight into the customer-facing "sustained" attribute and
    # interpolated into a log line, so a nameless event must not reach it at all - counting
    # them under None produced ["sustained on [None, 'charge_start_time']"].
    for nothing in (None, "", "   "):
        if nothing in sustained:
            print(f"ERROR: {nothing!r} reached the customer-facing sustained list: {sustained}")
            failed = True
    # An event with no usable name must not be able to reach the threshold on its own either.
    ledger = ControlLedger()
    ledger.restore([{"at": 100.0} for _ in range(5)] + [{"at": 100.0, "control": ""} for _ in range(5)])
    if ledger.sustained_controls(ledger.recent_events(now=200.0), threshold=3):
        print(f"ERROR: nameless events alone produced a sustained list: {ledger.sustained_controls(ledger.recent_events(now=200.0))}")
        failed = True
    assert not failed, "test_sustained_controls_survives_event_missing_control_key"


def test_future_dated_events_are_not_counted():
    """An event stamped in the future cannot be judged, so it must not reach the customer count.

    recent_events() bounded the window only from above, and `(now - at) <= window_s` is satisfied
    by ANY negative age, so a future "at" counted for ever - and restore() reloaded it from the
    published attribute, so a restart did not clear it either. A pod that starts before NTP sync,
    or a corrupt "events" attribute, reaches that. The count is customer facing, so a permanent
    phantom event is a permanent false accusation.

    This is a READ-path rule only. prune() deliberately keeps these - see
    test_prune_keeps_future_dated_events() for why deleting them destroys real history.
    """
    failed = False
    ledger = ControlLedger()
    ledger.events = [{"at": 10**12, "control": "reserve"}, {"at": 950.0, "control": "reserve"}]
    if [event["at"] for event in ledger.recent_events(now=1000.0)] != [950.0]:
        print(f"ERROR: a far-future event was counted: {ledger.recent_events(now=1000.0)}")
        failed = True
    if ledger.sustained_controls(ledger.recent_events(now=1000.0), threshold=1) != ["reserve"]:
        print("ERROR: the sustained list did not match the events inside the window")
        failed = True

    # Ordinary clock jitter between the write and the publish must NOT be thrown away.
    ledger = ControlLedger()
    ledger.events = [{"at": 1030.0, "control": "reserve"}]
    if len(ledger.recent_events(now=1000.0)) != 1:
        print("ERROR: a 30s forward jitter was discarded as future-dated")
        failed = True

    # And an event that has genuinely aged out is still excluded from the other end.
    ledger = ControlLedger()
    ledger.events = [{"at": 100.0, "control": "reserve"}, {"at": 90000.0, "control": "reserve"}]
    if [event["at"] for event in ledger.recent_events(now=100000.0)] != [90000.0]:
        print("ERROR: the upper window bound stopped working")
        failed = True
    assert not failed, "test_future_dated_events_are_not_counted"


def test_recent_events_survives_a_malformed_event():
    """A non-dict event must be dropped, not raise out of the middle of a plan run.

    restore() deliberately does not filter a bad "at", and the attribute round-trips through
    Home Assistant, so anything can be in the list. _event_time() already tolerated that for
    sorting; recent_events() re-implemented the parse with narrower exception handling, so a
    non-dict sorted perfectly happily and then raised AttributeError on the next filter.
    """
    failed = False
    for junk in ("not-a-dict", 42, None, ["at", 100.0]):
        ledger = ControlLedger()
        ledger.events = [junk, {"at": 950.0, "control": "reserve"}]
        try:
            recent = ledger.recent_events(now=1000.0)
        except Exception as e:
            print(f"ERROR: recent_events raised on {junk!r}: {type(e).__name__}: {e}")
            failed = True
            continue
        if recent != [{"at": 950.0, "control": "reserve"}]:
            print(f"ERROR: {junk!r} was not dropped from the window: {recent}")
            failed = True
        try:
            ledger.sustained_controls(ledger.recent_events(now=1000.0))
        except Exception as e:
            print(f"ERROR: sustained_controls raised on {junk!r}: {type(e).__name__}: {e}")
            failed = True
    assert not failed, "test_recent_events_survives_a_malformed_event"


def test_is_plausible_validates_hour_and_minute_components():
    """The H M charge-window family carries clock semantics and must be validated.

    adjust_charge_window() writes charge_start_hour/minute and the discharge equivalents
    whenever inv_charge_time_format is "H M". They fell through to True, so a Modbus or GivTCP
    glitch returning 255 or -1 walked every suppression rung and became an accusation. The
    FULL time string is a legitimate read of the same control on `time.` entities (FB00
    firmware), so it has to stay plausible or that firmware goes silently undetectable.
    """
    failed = False
    for control in ("charge_start_hour", "charge_end_hour", "discharge_start_hour", "discharge_end_hour"):
        for good in (0, 23, "07", 7.0, "23:30", "23:30:00"):
            if not is_plausible(control, good):
                print(f"ERROR: {control} rejected a valid {good!r}")
                failed = True
        for bad in (255, 24, -1, "24", "not-an-hour", None):
            if is_plausible(control, bad):
                print(f"ERROR: {control} accepted {bad!r}")
                failed = True
    for control in ("charge_start_minute", "charge_end_minute", "discharge_start_minute", "discharge_end_minute"):
        for good in (0, 59, "30", "23:30"):
            if not is_plausible(control, good):
                print(f"ERROR: {control} rejected a valid {good!r}")
                failed = True
        for bad in (255, 60, -1, "not-a-minute", None):
            if is_plausible(control, bad):
                print(f"ERROR: {control} accepted {bad!r}")
                failed = True
    assert not failed, "test_is_plausible_validates_hour_and_minute_components"


def test_implausible_hour_read_is_suppressed_and_logged():
    """The rule above must actually suppress through the real helper - and say so in the log.

    All three call sites discard observe()'s verdict, so every suppression rung was invisible:
    a customer reporting interference against an entity reading 0 gave no way to tell whether
    the ledger owned nothing, suppressed on the freshness gate, refused the read, or had been
    wiped. This drives a glitched 255 through write_and_poll_value and demands both silence
    and a diagnostic.
    """
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    stub = _stub_inverter([1.0, 5.0], ledger=ledger, generation=1000.0)
    stub.write_and_poll_value("charge_start_hour", "number.charge_start_hour", 5)
    if "number.charge_start_hour" not in ledger.records:
        print("ERROR: the confirming write did not confer ownership, so the case below proves nothing")
        failed = True
    ledger.begin_cycle()
    stub = _stub_inverter([255.0, 5.0], ledger=ledger, generation=2000.0)
    stub.write_and_poll_value("charge_start_hour", "number.charge_start_hour", 5)
    if ledger.events:
        print(f"ERROR: a glitched hour read of 255 was reported as external: {ledger.events}")
        failed = True
    if not any("control ledger" in line and IMPLAUSIBLE in line for line in stub.base.logs):
        print(f"ERROR: the implausible verdict was suppressed silently: {stub.base.logs}")
        failed = True

    # A verdict of STALE - the other rung a support engineer needs to be able to see.
    ledger = ControlLedger()
    ledger.begin_cycle()
    stub = _stub_inverter([50.0, 90.0], ledger=ledger, generation=1000.0)
    stub.write_and_poll_value("charge_limit", "number.charge_limit", 90)
    stub = _stub_inverter([20.0, 90.0], ledger=ledger, generation=2000.0)  # same cycle
    stub.write_and_poll_value("charge_limit", "number.charge_limit", 90)
    if not any("control ledger" in line and STALE in line for line in stub.base.logs):
        print(f"ERROR: the stale verdict was suppressed silently: {stub.base.logs}")
        failed = True

    # A healthy owned read must NOT log - the diagnostic has to stay readable.
    ledger = ControlLedger()
    ledger.begin_cycle()
    stub = _stub_inverter([50.0, 90.0], ledger=ledger, generation=1000.0)
    stub.write_and_poll_value("charge_limit", "number.charge_limit", 90)
    ledger.begin_cycle()
    stub = _stub_inverter([90.0], ledger=ledger, generation=2000.0)
    stub.write_and_poll_value("charge_limit", "number.charge_limit", 90)
    if any("control ledger" in line for line in stub.base.logs):
        print(f"ERROR: a healthy owned read produced a ledger log line: {stub.base.logs}")
        failed = True
    assert not failed, "test_implausible_hour_read_is_suppressed_and_logged"


def test_steady_control_stays_owned_indefinitely():
    """A control Predbat sets once and never rewrites must still be watched days later.

    adjust_reserve (inverter.py:1794), adjust_battery_target (:1963) and adjust_charge_window
    (:3157) only call the write helper when the value DIFFERS, so a reserve sitting at its
    minimum produces no observe() and no record_write for days on end. Ageing the confirmation
    out therefore blinds precisely the steady controls most likely to be quietly changed by
    somebody else, every single day, in silence.

    Elapsed time cannot distinguish "Predbat stopped controlling this" from "Predbat is
    controlling it and has nothing to change", so the ledger does not try. The residual risk - a
    user disables a switch, hand-edits the value, re-enables - produces a SINGLE event, and single
    events are recorded but never surfaced: the customer-visible list needs three on one control
    inside 24h.
    """
    failed = False
    ledger = ControlLedger()
    ledger.begin_cycle()
    ledger.record_write("number.reserve", "reserve", 4.0, now=100.0, generation=100.0)
    # A week of 5-minute cycles in which the caller never reaches the write helper at all.
    for _ in range(2016):
        ledger.begin_cycle()
    a_week = 100.0 + 7 * 86400.0
    if ledger.observe("number.reserve", "reserve", 50.0, now=a_week, generation=a_week) != EXTERNAL:
        print("ERROR: a week-old confirmation stopped reporting - an age-out has come back")
        failed = True
    if len(ledger.events) != 1:
        print(f"ERROR: expected one event from the week-later change, got {ledger.events}")
        failed = True
    assert not failed, "test_steady_control_stays_owned_indefinitely"


def test_prune_keeps_future_dated_events():
    """prune() maintains the DURABLE store, so it must not delete what it merely cannot judge.

    The publisher writes the pruned list back to the entity attribute restore() reads, so
    anything prune() drops is gone for good. A pod that starts before NTP sync with a slow clock
    makes every restored event compute as future-dated - pruning on that basis deletes a real 24
    hours of history irrecoverably on the very first publish. The clock corrects; the events have
    to still be there when it does.
    """
    failed = False
    ledger = ControlLedger()
    ledger.restore([{"at": 5000.0, "control": "reserve"}, {"at": 5100.0, "control": "reserve"}])
    # A clock ten minutes slow makes both look future-dated.
    slow_clock = 4400.0
    ledger.prune(now=slow_clock)
    if len(ledger.events) != 2:
        print(f"ERROR: prune() on a slow clock deleted real history: {ledger.events}")
        failed = True
    # They are still not COUNTED while they cannot be judged - the count is a claim, the store is not.
    if ledger.recent_events(now=slow_clock):
        print("ERROR: future-dated events were counted as if they could be judged")
        failed = True
    # Once the clock corrects they come back.
    if len(ledger.recent_events(now=5200.0)) != 2:
        print("ERROR: events were not judged again once the clock corrected")
        failed = True

    # What HAS genuinely aged out still goes, or the list grows without bound.
    ledger = ControlLedger()
    ledger.restore([{"at": 100.0, "control": "reserve"}, {"at": 90000.0, "control": "reserve"}])
    ledger.prune(now=100000.0)
    if [event["at"] for event in ledger.events] != [90000.0]:
        print(f"ERROR: prune() did not drop a genuinely aged-out event: {ledger.events}")
        failed = True

    # An "at" that cannot be parsed IS garbage - no clock correction fixes it.
    ledger = ControlLedger()
    ledger.events = ["junk", {"at": "banana"}, {"at": 950.0, "control": "reserve"}]
    ledger.prune(now=1000.0)
    if [event["at"] for event in ledger.events] != [950.0]:
        print(f"ERROR: prune() kept unparseable events: {ledger.events}")
        failed = True
    assert not failed, "test_prune_keeps_future_dated_events"


def test_minute_control_rejects_a_wall_clock_on_an_integer_entity():
    """Which shape is legitimate is decided by the ENTITY, so it is decided by what we own.

    adjust_charge_window() writes int(new_start[3:5]) to a number/select minute entity and the
    whole time string to a `time.` one, so the control NAME alone cannot say which is valid.
    An entity we confirmed holding 30 has no business reading "01:30" - that is a corrupt read,
    not somebody changing the window. An entity we confirmed holding "23:30:00" legitimately
    reads wall-clock and must not be blinded.
    """
    failed = False
    for control, integer_owned in (("charge_start_minute", 30), ("charge_end_minute", 45), ("charge_start_hour", 7), ("discharge_end_hour", 19)):
        if is_plausible(control, "01:30", integer_owned):
            print(f"ERROR: {control} owning {integer_owned} accepted a wall-clock read")
            failed = True
        if not is_plausible(control, "01:30", "23:30:00"):
            print(f"ERROR: {control} owning a time string rejected a wall-clock read - FB00 firmware is blinded")
            failed = True
        # With no ownership context both shapes stay acceptable: permissive means fewer accusations.
        if not is_plausible(control, "01:30"):
            print(f"ERROR: {control} rejected a wall-clock read with no ownership context")
            failed = True
        # And the glitch values are refused whatever we own.
        for bad in (255, -1):
            for owned in (integer_owned, "23:30:00", None):
                if is_plausible(control, bad, owned):
                    print(f"ERROR: {control} owning {owned!r} accepted {bad}")
                    failed = True
    assert not failed, "test_minute_control_rejects_a_wall_clock_on_an_integer_entity"


def test_publish_cap_never_drops_an_event_we_just_detected():
    """The published attribute IS the durable store, so what the cap drops is gone for good.

    Sorting by time does NOT fix this and neither does the list tail - restore() already leaves
    the store sorted, so the two are identical. On a pod whose clock is behind, this run's real
    event is stamped with a small "at" and is genuinely the OLDEST by timestamp, so ANY by-time
    cap discards precisely the event just detected. Events this process detected are therefore
    protected from the cap whatever their timestamp claims.
    """
    failed = False

    def _detect(ledger, at, entity_id):
        """Drive a real detection so the event is created the way production creates it."""
        ledger.begin_cycle()
        ledger.record_write(entity_id, "reserve", 4.0, now=at - 1.0, generation=at - 1.0)
        ledger.begin_cycle()
        ledger.observe(entity_id, "reserve", 50.0, now=at, generation=at)

    # A clock an hour behind: the just-detected event looks older than every restored one.
    ledger = ControlLedger()
    _detect(ledger, 1000.0, "number.detected_now")
    ledger.restore([{"at": 5000.0 + n, "control": "reserve", "entity_id": f"h{n}"} for n in range(25)])
    published = ledger.newest_events(20)
    if len(published) != 20:
        print(f"ERROR: expected 20 published events, got {len(published)}")
        failed = True
    if not any(event["entity_id"] == "number.detected_now" for event in published):
        print(f"ERROR: the just-detected event was dropped by the publish cap: {[e['entity_id'] for e in published]}")
        failed = True
    if [event["at"] for event in published] != sorted(event["at"] for event in published):
        print("ERROR: published events are not oldest-first")
        failed = True
    # The room it took must come off the OLDEST history, not the newest.
    if any(event["entity_id"] == "h0" for event in published):
        print("ERROR: the oldest history survived ahead of newer history")
        failed = True
    if not any(event["entity_id"] == "h24" for event in published):
        print("ERROR: the newest history was dropped")
        failed = True

    # With a correct clock nothing changes: newest wins, oldest goes.
    ledger = ControlLedger()
    _detect(ledger, 100000.0, "number.newest")
    ledger.restore([{"at": 20000.0 + n, "control": "reserve", "entity_id": f"h{n}"} for n in range(25)])
    published = ledger.newest_events(20)
    if not any(event["entity_id"] == "number.newest" for event in published):
        print("ERROR: the newest event was dropped by the publish cap")
        failed = True
    if any(event["entity_id"] == "h0" for event in published):
        print("ERROR: the oldest event survived the publish cap ahead of newer ones")
        failed = True

    # Under the cap, everything is published and still oldest-first.
    ledger = ControlLedger()
    _detect(ledger, 1000.0, "number.detected_now")
    ledger.restore([{"at": 5000.0 + n, "control": "reserve", "entity_id": f"h{n}"} for n in range(5)])
    published = ledger.newest_events(20)
    if len(published) != 6 or published[0]["entity_id"] != "number.detected_now":
        print(f"ERROR: an under-cap publish was not the whole store oldest-first: {[e['entity_id'] for e in published]}")
        failed = True

    # And prune() must not leave the protected set holding events that have aged out.
    ledger = ControlLedger()
    _detect(ledger, 1000.0, "number.detected_now")
    ledger.prune(now=1000.0 + 2 * 86400.0)
    if ledger.events or ledger.session_events:
        print(f"ERROR: prune() left aged-out events protected: {ledger.events} {ledger.session_events}")
        failed = True
    assert not failed, "test_publish_cap_never_drops_an_event_we_just_detected"


def test_plausibility_is_owned_aware_in_both_directions():
    """A `time.` entity holds whole times, so a bare component read is truncated, not a change.

    The rule was only tight in one direction: an integer entity refused "01:30", but an entity
    confirmed holding "05:00:00" still accepted "5". Both are corrupt reads of the shape Predbat
    proved the entity holds, and both must be refused.
    """
    failed = False
    for control in ("charge_start_hour", "charge_end_hour", "charge_start_minute", "discharge_end_minute"):
        # Confirmed holding a whole time: a bare component is a truncated read.
        for truncated in ("5", 5, "30", 30):
            if is_plausible(control, truncated, "05:00:00"):
                print(f"ERROR: {control} owning '05:00:00' accepted a truncated {truncated!r}")
                failed = True
        if not is_plausible(control, "07:30", "05:00:00"):
            print(f"ERROR: {control} owning '05:00:00' rejected a whole time")
            failed = True

        # Confirmed holding an integer: a whole time is a corrupt read.
        if is_plausible(control, "01:30", 30):
            print(f"ERROR: {control} owning 30 accepted a wall-clock read")
            failed = True
        if not is_plausible(control, 15, 30):
            print(f"ERROR: {control} owning 30 rejected a valid component")
            failed = True

        # No ownership context: both shapes stay acceptable, glitches still refused.
        if not is_plausible(control, "01:30") or not is_plausible(control, 15):
            print(f"ERROR: {control} rejected a valid read with no ownership context")
            failed = True
        for bad in (255, -1, "not-a-clock"):
            for owned in (30, "05:00:00", None):
                if is_plausible(control, bad, owned):
                    print(f"ERROR: {control} owning {owned!r} accepted {bad!r}")
                    failed = True
    assert not failed, "test_plausibility_is_owned_aware_in_both_directions"


def test_begin_cycle_precedes_every_inverter_read():
    """Every write in a run must share that run's cycle number.

    fetch_inverter_data() runs update_status(), which writes scheduled_charge_enable through
    write_and_poll_switch and therefore CONFIRMS ownership. With begin_cycle() called after the
    fetch, those read-path confirmations were stamped with the PREVIOUS cycle number, so the next
    observe() of them hit the STALE rung whenever execute_plan() had written the entity in the
    prior run - the feature silently blind on exactly the entity the read path touches.

    Checked against the source because the ordering has no runtime observable: the cycle counter
    is correct either way, it is only the ordering relative to the fetch that is wrong. Also pins
    that begin_cycle() is called exactly ONCE per run - the second execute_plan() deliberately
    shares the cycle, so that a value confirmed in pass 1 and seen diverged in pass 2 classifies
    STALE rather than being reported.
    """
    failed = False
    import os

    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "predbat.py")).read()
    begins = [i for i in range(len(source)) if source.startswith("self.control_ledger.begin_cycle()", i)]
    fetches = [i for i in range(len(source)) if source.startswith("self.fetch_inverter_data()", i)]
    if len(begins) != 1:
        print(f"ERROR: expected exactly one begin_cycle() call, found {len(begins)}")
        failed = True
    if not fetches:
        print("ERROR: found no fetch_inverter_data() calls, so this test proves nothing")
        failed = True
    if begins and fetches and begins[0] > min(fetches):
        print("ERROR: begin_cycle() runs after an inverter fetch, so read-path writes are stamped with the previous cycle")
        failed = True
    assert not failed, "test_begin_cycle_precedes_every_inverter_read"


def run_control_ledger_tests(my_predbat):
    """Run all control ledger tests."""
    failed = False
    for name, fn in [
        ("values_match_fuzzy", test_values_match_uses_fuzzy_for_numerics),
        ("is_plausible", test_is_plausible_rejects_impossible_times),
        ("generation_from_state", test_generation_from_state),
        ("generation_engine_record_shape", test_generation_from_state_reads_engine_record_shape),
        ("is_plausible_time_ranges", test_is_plausible_accepts_time_ranges),
        ("unowned_never_reports", test_unowned_never_reports),
        ("confirmed_then_changed", test_confirmed_then_changed_is_external),
        ("matching_read_stays_owned", test_matching_read_stays_owned),
        ("failed_write_never_owns", test_failed_write_never_owns),
        ("ignore_fail_never_owns", test_ignore_fail_write_never_owns),
        ("same_cycle_is_stale", test_same_cycle_read_is_stale),
        ("older_generation_is_stale", test_older_generation_is_stale),
        ("implausible_suppressed", test_implausible_read_is_suppressed),
        ("write_in_flight_settling", test_write_in_flight_is_settling),
        ("event_counts_once", test_event_clears_ownership_so_it_counts_once),
        ("second_event_needs_reconfirm", test_second_event_requires_reconfirmation),
        ("clear_drops_ownership", test_clear_drops_ownership),
        ("echo_component_silent", test_echo_component_never_fires),
        ("shared_ems_entity", test_shared_ems_entity_is_one_record),
        ("unavailable_read_suppressed", test_unavailable_read_is_suppressed),
        ("unavailable_readback_never_owns", test_unavailable_readback_never_owns),
        ("ledger_generation", test_ledger_generation_reads_last_updated),
        ("wiring_no_write_needed", test_wiring_no_write_needed_skips_note_and_record),
        ("wiring_successful_write", test_wiring_successful_write_records_actual_read_back),
        ("wiring_ignore_fail", test_wiring_ignore_fail_drops_ownership),
        ("wiring_failed_write", test_wiring_failed_write_clears_ownership),
        ("wiring_observe_before_note", test_wiring_observe_precedes_note_write_attempt),
        ("wiring_dropout_never_external", test_wiring_dropout_read_is_never_reported_external),
        ("wiring_freshness_long_interval", test_wiring_freshness_gate_survives_a_long_quiet_interval),
        ("wiring_genuine_change_reports", test_wiring_genuine_external_change_still_reports),
        ("wiring_other_write_paths", test_wiring_other_write_paths_leave_ownership_alone),
        ("clear_drops_every_control", test_clear_drops_every_control),
        ("restore_orders_oldest_first", test_restore_orders_events_oldest_first),
        ("recent_events_window", test_recent_events_filters_by_window),
        ("restore_rehydrates", test_restore_rehydrates_and_filters),
        ("restore_ignores_non_list", test_restore_ignores_non_list_attribute),
        ("sustained_controls", test_sustained_lists_repeat_offenders),
        ("restore_string_at_safe", test_restored_event_with_string_at_does_not_raise),
        ("sustained_survives_missing_control", test_sustained_controls_survives_event_missing_control_key),
        ("future_dated_events_not_counted", test_future_dated_events_are_not_counted),
        ("recent_events_malformed_safe", test_recent_events_survives_a_malformed_event),
        ("is_plausible_hour_minute", test_is_plausible_validates_hour_and_minute_components),
        ("implausible_hour_logged", test_implausible_hour_read_is_suppressed_and_logged),
        ("steady_control_stays_owned", test_steady_control_stays_owned_indefinitely),
        ("prune_keeps_future_events", test_prune_keeps_future_dated_events),
        ("minute_rejects_wall_clock", test_minute_control_rejects_a_wall_clock_on_an_integer_entity),
        ("plausibility_both_directions", test_plausibility_is_owned_aware_in_both_directions),
        ("publish_cap_keeps_detected", test_publish_cap_never_drops_an_event_we_just_detected),
        ("begin_cycle_ordering", test_begin_cycle_precedes_every_inverter_read),
    ]:
        try:
            if fn():
                print(f"  FAILED: control_ledger.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in control_ledger.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

# fmt: off
"""
History Chunking Tests

Tests for fetching long history windows from Home Assistant in time-ordered chunks
rather than one request, so only one chunk's response is resident at a time:
- get_history() splits long windows and covers them exactly
- the synthesised record Home Assistant returns at each chunk start is dropped
"""

from datetime import datetime, timedelta, timezone

from tests.test_hainterface_common import MockBase, create_ha_interface


NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)


def _interface():
    """Build an HAInterface against the shared mock base."""
    return create_ha_interface(MockBase(), ha_key="test_key", db_enable=False, db_mirror_ha=False, db_primary=False)


def _record(stamp, state):
    """Build one history record."""
    return {"state": state, "last_updated": stamp.isoformat(), "attributes": {"unit_of_measurement": "W"}}


def _capture(interface, responder):
    """Point api_call at a responder and record the windows requested."""
    calls = []

    def api_call(endpoint, data_in=None, **kwargs):
        """Stand in for the REST call, recording the requested window."""
        start = endpoint.rsplit("/", 1)[1]
        calls.append((start, data_in.get("end_time")))
        return responder(start, data_in.get("end_time"))

    interface.api_call = api_call
    return calls


def test_get_history_drops_synthesised_boundary_record(my_predbat=None):
    """Home Assistant's state-at-window-start record must not be added twice"""
    print("\n=== Testing get_history() boundary record handling ===")
    failed = 0

    interface = _interface()

    def responder(start, end):
        """Return a synthesised record at the window start plus one real record."""
        start_dt = datetime.fromisoformat(start)
        # Home Assistant reports the state in effect at start_time, exactly on the boundary
        # and with no sub-second precision, then any real changes within the window
        return [[_record(start_dt, "500"), _record(start_dt + timedelta(hours=1), "600")]]

    _capture(interface, responder)

    result = interface.get_history("sensor.load_power", NOW, days=9, chunk_days=3)

    if not result or not result[0]:
        print("ERROR: expected history back, got {}".format(result))
        return failed + 1

    items = result[0]
    stamps = [item["last_updated"] for item in items]
    if len(stamps) != len(set(stamps)):
        duplicates = sorted({s for s in stamps if stamps.count(s) > 1})
        print("ERROR: chunk boundaries duplicated records: {}".format(duplicates))
        failed += 1
    else:
        print("✓ no duplicate timestamps across chunk boundaries")

    # 3 chunks: chunk 1 contributes both records, chunks 2 and 3 contribute only their real one
    if len(items) != 4:
        print("ERROR: expected 4 records (2 from the first chunk, 1 from each later chunk), got {}".format(len(items)))
        print("       records: {}".format(stamps))
        failed += 1
    else:
        print("✓ synthesised boundary record dropped for chunks after the first")

    if stamps != sorted(stamps):
        print("ERROR: records not in oldest-first order: {}".format(stamps))
        failed += 1
    else:
        print("✓ records stay oldest-first across chunks")

    return failed


def test_get_history_short_window_makes_one_request(my_predbat=None):
    """A window inside the chunk size is fetched in a single request as before"""
    print("\n=== Testing get_history() short window ===")
    failed = 0

    interface = _interface()
    calls = _capture(interface, lambda start, end: [[_record(NOW - timedelta(hours=1), "500")]])

    interface.get_history("sensor.load_power", NOW, days=2, chunk_days=7)

    if len(calls) != 1:
        print("ERROR: expected 1 request for a 2 day window, got {}".format(len(calls)))
        failed += 1
    else:
        print("✓ short window still fetched in one request")

    return failed


def test_get_history_chunks_cover_the_window_exactly(my_predbat=None):
    """Chunks tile the requested window end to end with no gaps or overlap"""
    print("\n=== Testing get_history() window coverage ===")
    failed = 0

    interface = _interface()
    calls = _capture(interface, lambda start, end: [[_record(datetime.fromisoformat(start) + timedelta(hours=1), "500")]])

    interface.get_history("sensor.load_power", NOW, days=10, chunk_days=3)

    if len(calls) != 4:
        print("ERROR: expected 4 chunks for 10 days at 3 days each, got {}".format(len(calls)))
        return failed + 1
    print("✓ 10 day window split into 4 chunks of at most 3 days")

    starts = [datetime.fromisoformat(start) for start, _ in calls]
    ends = [datetime.fromisoformat(end) for _, end in calls]

    if starts[0] != NOW - timedelta(days=10):
        print("ERROR: first chunk starts at {}, expected {}".format(starts[0], NOW - timedelta(days=10)))
        failed += 1
    elif ends[-1] != NOW:
        print("ERROR: last chunk ends at {}, expected {}".format(ends[-1], NOW))
        failed += 1
    else:
        print("✓ chunks span exactly the requested window")

    for index in range(1, len(starts)):
        if starts[index] != ends[index - 1]:
            print("ERROR: chunk {} starts at {} but previous ended at {}".format(index, starts[index], ends[index - 1]))
            failed += 1
            break
    else:
        print("✓ chunks are contiguous with no gaps or overlap")

    if max((ends[i] - starts[i]) for i in range(len(starts))) > timedelta(days=3):
        print("ERROR: a chunk exceeded the 3 day limit")
        failed += 1
    else:
        print("✓ no chunk exceeds the requested chunk size")

    return failed


def run_history_chunking_tests(my_predbat=None):
    """Run all history chunking tests"""
    print("\n" + "=" * 80)
    print("History Chunking Tests")
    print("=" * 80)

    failed = 0
    failed += test_get_history_drops_synthesised_boundary_record(my_predbat)
    failed += test_get_history_short_window_makes_one_request(my_predbat)
    failed += test_get_history_chunks_cover_the_window_exactly(my_predbat)

    print("\n" + "=" * 80)
    if failed == 0:
        print("✅ All history chunking tests passed!")
    else:
        print(f"❌ {failed} history chunking test(s) failed")
    print("=" * 80)

    return failed

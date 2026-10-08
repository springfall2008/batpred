"""Pure effective-rate history regression tests, without HA or provider APIs."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone

from rate_history import RateHistory, _checksum


ORIGIN = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)


def _at(minutes=0):
    """Return an injected absolute test clock."""
    return ORIGIN + timedelta(minutes=minutes)


def _segment(start=0, end=30, rate=20, **extra):
    """Build a price interval at the injected origin."""
    return dict(start=_at(start), end=_at(end), rate=rate, **extra)


def _period(start=0, end=30):
    """Build explicit billing metadata, not inferred equal-price runs."""
    return dict(start=_at(start), end=_at(end), source_id="test:tariff")


def _prices(history, direction="import", start=0, end=60, **extra):
    """Return scopes and prices in elapsed minutes for readable assertions."""
    return [((datetime.fromisoformat(item["start"]) - ORIGIN).total_seconds() / 60, (datetime.fromisoformat(item["end"]) - ORIGIN).total_seconds() / 60, item["rate"]) for item in history.lookup(direction, _at(start), _at(end), **extra)]


def _observe(history, minute, manual=None, direction="import", periods=None, automatic=None, **extra):
    """Submit one realistic current observation with an explicit base profile."""
    history.observe(direction, periods or [_period()], automatic or [_segment()], manual or [], _at(minute), **extra)


class _MemoryStorage:
    """In-memory Storage contract with failure and corruption injection."""

    def __init__(self):
        """Initialise snapshot state and save behaviour."""
        self.data = {}
        self.result = True
        self.error = False
        self.calls = []
        self.mutate = None

    async def load(self, module, filename):
        """Return an isolated cached transport payload."""
        assert module == "rate_history"
        if self.error:
            raise OSError("load failure")
        return copy.deepcopy(self.data.get(filename))

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Simulate successful, false, empty and exception Storage writes."""
        assert module == "rate_history" and format == "json" and expiry is None
        self.calls.append(filename)
        if self.error:
            raise OSError("save failure")
        if self.result is True:
            self.data[filename] = copy.deepcopy(data)
        if self.mutate:
            self.mutate()
            self.mutate = None
        return self.result


def _test_manual():
    """Manual closing replacement, removal, expiry and increment precedence."""
    history = RateHistory()
    _observe(history, 1, [_segment(rate=10, kind="replace", source="manual")])
    _observe(history, 15, [_segment(rate=15, kind="replace", source="manual")])
    assert _prices(history) == [(0, 30, 15)]
    # A boundary-time removal must close the previous observation first.
    _observe(history, 30, periods=[_period(), _period(30, 60)], automatic=[_segment(), _segment(30, 60, 25)])
    assert _prices(history) == [(0, 30, 15), (30, 60, 25)]
    _observe(history, 45, periods=[_period()], automatic=[_segment(rate=99)])
    assert _prices(history, end=30) == [(0, 30, 15)]

    restored = RateHistory()
    restored.restore(history.snapshot())
    assert _prices(restored) == _prices(history)
    removed = RateHistory()
    _observe(removed, 1, [_segment(rate=10, kind="replace")])
    _observe(removed, 20)
    removed.close_elapsed(_at(30))
    assert _prices(removed) == [(0, 30, 20)]

    expiry = RateHistory()
    _observe(expiry, 10, [_segment(0, 20, 7, kind="replace")])
    assert _prices(expiry) == [(0, 30, 7)]
    expiry.close_elapsed(_at(40))
    assert _prices(expiry) == [(0, 30, 20)]

    event = [_segment(10, 20, 35, sources=["axle"])]
    increment = RateHistory()
    _observe(increment, 12, [_segment(rate=3, kind="increment")], base_segments=[_segment()], event_segments=event)
    _observe(increment, 25, [_segment(rate=4, kind="increment")], base_segments=[_segment()])
    increment.close_elapsed(_at(30))
    assert _prices(increment) == [(0, 10, 24), (10, 20, 39), (20, 30, 24)]
    ordered = RateHistory()
    _observe(ordered, 5, [_segment(rate=3, kind="increment"), _segment(rate=8, kind="replace"), _segment(rate=2, kind="increment")])
    ordered.close_elapsed(_at(30))
    assert _prices(ordered) == [(0, 30, 10)]


def _test_events_and_gaps():
    """Active scopes survive disappearance, while future offers never mature."""
    for width in (15, 30, 60):
        history = RateHistory()
        event = [_segment(7, 13, -3, sources=["axle"])]
        _observe(history, 2, periods=[_period(0, width)], base_segments=[_segment(0, width)], event_segments=event)
        assert _prices(history, end=width) == [(0, width, 20)]
        _observe(history, 8, periods=[_period(0, width)], base_segments=[_segment(0, width)], event_segments=event)
        # Successful empty response or failure: retained active bounds stay exact.
        _observe(history, 10, periods=[_period(0, width)], base_segments=[_segment(0, width)], event_segments=[])
        _observe(history, width - 1, periods=[_period(0, width)], base_segments=[_segment(0, width)])
        history.close_elapsed(_at(width))
        assert _prices(history, end=width) == [(0, 7, 20), (7, 13, -3), (13, width, 20)]

    future = RateHistory()
    _observe(future, 1, periods=[_period(), _period(30, 60)], automatic=[_segment(), _segment(30, 60, 1)], event_segments=[_segment(15, 25, 0)])
    future.close_elapsed(_at(90))
    assert _prices(future, end=90) == [(0, 30, 20)]
    assert future.lookup("import", _at(30), _at(90)) == []
    # Partial automatic evidence without explicit base creates gaps, not estimates.
    partial = RateHistory()
    _observe(partial, 12, automatic=[_segment(10, 20, 5), _segment(20, 30, 0)])
    partial.close_elapsed(_at(30))
    assert _prices(partial) == [(10, 20, 5)]
    assert partial.lookup("import", _at(0), _at(10)) == []
    assert partial.lookup("import", _at(0), _at(30), include_open=False)

    masked = RateHistory()
    _observe(masked, 12, [_segment(rate=1, kind="replace")], base_segments=[_segment()], event_segments=[_segment(10, 20, 35)])
    _observe(masked, 25, base_segments=[_segment()])
    masked.close_elapsed(_at(30))
    assert _prices(masked) == [(0, 10, 20), (10, 20, 35), (20, 30, 20)]
    # Explicit base is the latest provisional profile, not retained event evidence.
    # A withdrawn unconfirmed IOG portion must disappear even after its scope ends.
    provisional = RateHistory()
    _observe(provisional, 5, base_segments=[_segment(0, 10, 6), _segment(10, 30, 20)])
    _observe(provisional, 15, base_segments=[_segment()])
    provisional.close_elapsed(_at(30))
    assert _prices(provisional) == [(0, 30, 20)]
    # Without explicit base, observed elapsed automatic scopes remain historical.
    sampled = RateHistory()
    _observe(sampled, 5, automatic=[_segment(0, 10, 6), _segment(10, 30, 20)])
    _observe(sampled, 15, automatic=[_segment()])
    sampled.close_elapsed(_at(30))
    assert _prices(sampled) == [(0, 10, 6), (10, 30, 20)]


def _test_confirmed():
    """Source-confirmed whole half-hours persist below manual and scoped events."""
    history = RateHistory()
    _observe(history, 2)
    assert history.confirm_iog(_at(), _at(30), 6, _at(10), source_id="meter", car_n=0, cap_bucket="noon")
    assert not history.confirm_iog(_at(30), _at(60), 6, _at(10))
    assert not history.confirm_iog(_at(), _at(60), 6, _at(10))
    assert not history.confirm_iog(_at(1), _at(31), 6, _at(10))
    assert history.confirm_iog(_at(), _at(30), 6, _at(11), source_id="meter", car_n=1, cap_bucket="noon")
    assert history.confirm_iog(_at(), _at(30), 6, _at(12), source_id="meter", car_n=1, cap_bucket="noon")
    assert len(history.snapshot()["records"][0]["allocations"]) == 2
    _observe(history, 15, [_segment(rate=9, kind="replace")])
    assert _prices(history) == [(0, 30, 9)]
    assert history.lookup("import", _at(), _at(30))[0]["quality"] == "observed"
    _observe(history, 25)
    assert _prices(history) == [(0, 30, 6)]
    restored = RateHistory()
    restored.restore(history.snapshot())
    assert len(restored.snapshot()["records"][0]["allocations"]) == 2
    restored.close_elapsed(_at(60))
    assert _prices(restored) == [(0, 30, 6)]
    assert restored.lookup("import", _at(), _at(30))[0]["quality"] == "confirmed"
    assert not restored.confirm_iog(_at(), _at(30), 2, _at(61))
    assert restored.confirm_iog(_at(), _at(30), 6, _at(61))
    assert _prices(restored) == [(0, 30, 6)]
    closed_prices = restored.lookup("import", _at(), _at(30))
    assert restored.confirm_iog(_at(), _at(30), 6, _at(61), source_id="meter-late", car_n=2, cap_bucket="noon")
    assert restored.lookup("import", _at(), _at(30)) == closed_prices
    allocation_count = len(restored.snapshot()["records"][0]["allocations"])
    assert allocation_count == 4  # Two cars, an unassigned proof and a late third car.
    revision = restored._revision
    assert restored.confirm_iog(_at(), _at(30), 6, _at(62), source_id="other-feed", car_n=2, cap_bucket="noon")
    assert restored._revision == revision
    multi_car = RateHistory()
    multi_car.restore(restored.snapshot())
    proof_by_car = {item["car_n"]: item for item in multi_car.snapshot()["records"][0]["allocations"] if item["car_n"] is not None}
    assert set(proof_by_car) == {0, 1, 2}
    assert all(item["rate"] == 6 and item["cap_bucket"] == "noon" for item in proof_by_car.values())
    legacy_proof = copy.deepcopy(multi_car.snapshot())
    for allocation in legacy_proof["records"][0]["allocations"]:
        del allocation["rate"]
    legacy_restored = RateHistory()
    legacy_restored.restore(legacy_proof)
    assert legacy_restored.snapshot() == multi_car.snapshot()
    bad_proof = copy.deepcopy(multi_car.snapshot())
    bad_proof["records"][0]["allocations"].append(copy.deepcopy(bad_proof["records"][0]["allocations"][0]))
    try:
        multi_car.restore(bad_proof)
    except ValueError:
        pass
    else:
        raise AssertionError("Duplicate car cap allocation accepted")
    # A just-finished sample is accepted before close_elapsed, not after closure.
    previous = RateHistory()
    _observe(previous, 25)
    assert previous.confirm_iog(_at(), _at(30), 6, _at(31))
    previous.close_elapsed(_at(31))
    assert _prices(previous) == [(0, 30, 6)]
    manual = RateHistory()
    _observe(manual, 25, [_segment(rate=9, kind="replace")])
    manual.confirm_iog(_at(), _at(30), 6, _at(26))
    manual.close_elapsed(_at(30))
    assert _prices(manual) == [(0, 30, 9)]
    assert manual.lookup("import", _at(), _at(30))[0]["quality"] == "observed"
    missing = RateHistory()
    assert missing.confirm_iog(_at(), _at(30), 6, _at(31))
    missing.close_elapsed(_at(31))
    assert _prices(missing) == [(0, 30, 6)]


def _test_time_and_baseline():
    """Independent directions, UTC DST identities, baseline and calendar pruning."""
    history = RateHistory()
    _observe(history, 1, periods=[_period(), _period(30, 60)], baseline_segments=[_segment(rate=27)])
    _observe(history, 31, periods=[_period(30, 60)], automatic=[_segment(30, 60)])
    _observe(history, 31, direction="export", periods=[_period(0, 60)], automatic=[_segment(0, 60, 5)])
    history.close_elapsed(_at(60))
    assert _prices(history) == [(0, 30, 20), (30, 60, 20)]
    assert _prices(history, direction="export") == [(0, 60, 5)]
    assert _prices(history, end=30, baseline=True) == [(0, 30, 27)]
    # Repeated London 01:00 instants are two distinct UTC records.
    repeated = RateHistory()
    starts = [datetime.fromisoformat("2026-10-25T01:00:00+01:00"), datetime.fromisoformat("2026-10-25T01:00:00+00:00")]
    for index, start in enumerate(starts):
        end = start + timedelta(minutes=30)
        repeated.observe("import", [dict(start=start, end=end)], [dict(start=start, end=end, rate=index)], [], start)
    repeated.close_elapsed(datetime(2026, 10, 25, 3, tzinfo=timezone.utc))
    assert len(repeated.records) == 2
    assert [item["rate"] for item in repeated.lookup("import", starts[0], starts[1] + timedelta(minutes=30))] == [0, 1]

    for today, expected_hours in ((datetime(2026, 3, 30, 12, tzinfo=timezone.utc), 23), (datetime(2026, 10, 26, 12, tzinfo=timezone.utc), 25)):
        ledger = RateHistory(retention_days=1)
        zone = ledger._zone
        yesterday = today.astimezone(zone).date() - timedelta(days=1)
        start = datetime.combine(yesterday, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(yesterday + timedelta(days=1), datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
        assert (end - start).total_seconds() / 3600 == expected_hours
        ledger.observe("import", [dict(start=start, end=end)], [dict(start=start, end=end, rate=0)], [], start + timedelta(minutes=5))
        ledger.close_elapsed(today)
        assert ledger.lookup("import", start, end)
        ledger.prune(today + timedelta(days=1))
        assert not ledger.lookup("import", start, end)
    # An open long source period is not pruned merely because its start is old.
    old = RateHistory()
    old.observe("import", [_period(0, 10000)], [_segment(0, 10000)], [], _at(1))
    old.prune(_at(6000))
    assert len(old.records) == 1


def _test_context():
    """Daily standing charge and premium coverage survive restore and retention."""
    history = RateHistory()
    history.set_context("2026-10-03", 50, premium=True)
    assert history.contexts == {"2026-10-03": {"standing_charge": 50, "car_premium_present": True}}
    assert history.accounting_context is history.contexts
    assert history.snapshot()["accounting_context"] == history.contexts
    revision = history._revision
    history.set_context(ORIGIN.date(), 50, premium=True)
    assert history._revision == revision
    history.set_context("2026-10-03", 55)
    assert history.accounting_context["2026-10-03"] == {"standing_charge": 55, "car_premium_present": True}
    history.set_context("2026-10-04", 60, premium=True)
    restored = RateHistory()
    restored.restore(history.snapshot())
    assert restored.contexts == history.contexts
    restored.prune(datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc))
    assert restored.contexts == {"2026-10-04": {"standing_charge": 60, "car_premium_present": True}}
    before = restored.snapshot()
    for charge, premium in ((float("inf"), False), (10, "false")):
        try:
            restored.set_context("2026-10-04", charge, premium)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid daily context accepted")
        assert restored.snapshot() == before
    broken = copy.deepcopy(before)
    broken["accounting_context"]["2026-10-04"]["car_premium_present"] = 1
    try:
        restored.restore(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid persisted context accepted")
    assert restored.snapshot() == before
    # Legacy schema-one snapshots without accounting context stay readable,
    # but correctly retain missing coverage rather than fabricating a charge.
    legacy = copy.deepcopy(before)
    del legacy["accounting_context"]
    restored.restore(legacy)
    assert restored.contexts == {}


def _test_offers():
    """Full active offers resolve across parents/restart on the latest base."""
    history = RateHistory()
    offer = _segment(20, 70, 100, direction="import", kind="increment", source="axle", stage=3, identity="offer-a")
    assert not history.retain_offer(offer, _at(10))
    assert not history.records and not history.offers
    assert history.retain_offer(offer, _at(25))
    assert not history.records
    assert len(history.active_offers("import", _at(25))) == 1
    assert not history.active_offers("export", _at(25))
    future = _segment(40, 50, 5, direction="import", kind="replace", source="axle", stage=3, identity="offer-b")
    assert not history.retain_offer(future, _at(25))
    _observe(history, 25, base_segments=[_segment()], offer_ids=["offer-a"])
    restarted = RateHistory()
    restarted.restore(history.snapshot())
    retained = restarted.active_offers("import", _at(35))
    assert len(retained) == 1 and retained[0]["start"] == _at(20).isoformat() and retained[0]["end"] == _at(70).isoformat()
    retained[0]["rate"] = 999
    assert restarted.active_offers("import", _at(35))[0]["rate"] == 100
    _observe(restarted, 35, periods=[_period(30, 60)], base_segments=[_segment(30, 60, 30)], offer_ids=["offer-a"])
    _observe(restarted, 65, periods=[_period(60, 90)], base_segments=[_segment(60, 90, 40)], offer_ids=["offer-a"])
    assert not restarted.active_offers("import", _at(70))
    assert len(restarted.offers_for("import", _at(60), _at(90))) == 1
    assert not restarted.retain_offer(dict(offer, rate=200), _at(75))
    restarted.close_elapsed(_at(90))
    assert _prices(restarted, end=90) == [(0, 20, 20), (20, 30, 120), (30, 60, 130), (60, 70, 140), (70, 90, 40)]
    before = restarted.snapshot()
    for field, value in (("observed_at", _at(15).isoformat()), ("rate", float("nan")), ("kind", "unknown"), ("stage", 4), ("stage", 1.0), ("sticky", "false")):
        malformed = copy.deepcopy(before)
        malformed["offers"][0][field] = value
        try:
            restarted.restore(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid retained offer accepted")
        assert restarted.snapshot() == before
    restarted.prune(_at(3 * 1440))
    assert restarted.offers == []


def _test_offer_resolution():
    """Stage precedence, immutable ended children, mutable null and price gaps."""
    history = RateHistory()
    outer = _segment(0, 60, 10, direction="import", kind="increment", source="axle", stage=3, identity="outer")
    inner = _segment(10, 20, 30, direction="import", kind="increment", source="axle_managed", stage=3, identity="inner", sticky=False)
    history.retain_offer(outer, _at(5))
    assert not history.retain_offer(inner, _at(5))
    _observe(history, 5, periods=[_period(0, 60)], base_segments=[_segment(0, 60)], offer_ids=["outer"])
    assert _prices(history) == [(0, 60, 30)]
    history.retain_offer(inner, _at(15))
    _observe(history, 15, periods=[_period(0, 60)], base_segments=[_segment(0, 60)], offer_ids=["outer", "inner"])
    history.retain_offer(dict(outer, rate=15), _at(25))
    assert not history.retain_offer(dict(inner, rate=99), _at(25))
    assert not history.withdraw_offer("inner", _at(25))
    # Caller supplies active outer only; ended observed child must remain.
    _observe(history, 25, periods=[_period(0, 60)], base_segments=[_segment(0, 60)], offer_ids=["outer"])
    assert _prices(history) == [(0, 10, 35), (10, 20, 65), (20, 60, 35)]
    history.close_elapsed(_at(60))
    assert _prices(history) == [(0, 10, 35), (10, 20, 65), (20, 60, 35)]

    precedence = RateHistory()
    precedence.confirm_iog(_at(), _at(30), 6, _at(5))
    operations = [
        _segment(rate=5, direction="import", kind="increment", source="axle", stage=3, identity="axle"),
        _segment(rate=0, direction="import", kind="minimum", source="free", stage=2, identity="free"),
        _segment(rate=4, direction="import", kind="minimum", source="saving", stage=1, identity="saving"),
    ]
    for operation in operations:
        precedence.retain_offer(operation, _at(5))
    ids = [operation["identity"] for operation in operations]
    _observe(precedence, 5, base_segments=[_segment()], offer_ids=ids)
    assert _prices(precedence) == [(0, 30, 5)]
    _observe(precedence, 10, [_segment(rate=9, kind="replace")], base_segments=[_segment()], offer_ids=ids)
    assert _prices(precedence) == [(0, 30, 9)]
    _observe(precedence, 25, base_segments=[_segment()], offer_ids=ids)
    precedence.close_elapsed(_at(30))
    assert _prices(precedence) == [(0, 30, 5)]
    assert not precedence.withdraw_offer("axle", _at(10))  # Sticky source disappearance.

    mutable = RateHistory()
    curve = _segment(0, 60, 50, direction="export", kind="replace", source="axle_managed", stage=3, identity="curve", sticky=False)
    mutable.retain_offer(curve, _at(5))
    _observe(mutable, 5, direction="export", base_segments=[_segment()], offer_ids=["curve"])
    mutable.close_elapsed(_at(30))
    closed = _prices(mutable, direction="export")
    _observe(mutable, 35, direction="export", periods=[_period(30, 60)], base_segments=[_segment(30, 60, 25)], offer_ids=["curve"])
    assert mutable.withdraw_offer("curve", _at(35))
    assert _prices(mutable, direction="export") == closed + [(30, 60, 25)]
    restored = RateHistory()
    restored.restore(mutable.snapshot())  # Closed IDs may reference a removed operation.
    assert _prices(restored, direction="export") == _prices(mutable, direction="export")
    null = RateHistory()
    null.retain_offer(dict(curve, direction="import"), _at(5))
    _observe(null, 5, base_segments=[_segment(10, 20, 6)], offer_ids=["curve"])
    assert _prices(null) == [(10, 20, 50)]  # Replacement never fills unknown gaps.
    assert null.withdraw_offer("curve", _at(10))
    assert _prices(null) == [(10, 20, 6)]
    bad_ids = copy.deepcopy(null.snapshot())
    bad_ids["records"][0]["offer_ids"] = ["unobserved"]
    try:
        null.restore(bad_ids)
    except ValueError:
        pass
    else:
        raise AssertionError("Unobserved parent offer accepted")


def _test_future_manual():
    """Future or expired operator proposals never become observed manual state."""
    history = RateHistory()
    _observe(history, 5, [_segment(20, 30, 10, kind="replace")])
    assert _prices(history) == [(0, 30, 20)]
    assert history.snapshot()["records"][0]["manual"] == []
    restarted = RateHistory()
    restarted.restore(history.snapshot())
    restarted.close_elapsed(_at(35))
    assert _prices(restarted) == [(0, 30, 20)]
    active = RateHistory()
    _observe(active, 5, [_segment(0, 20, 10, kind="replace")])
    assert _prices(active) == [(0, 30, 10)]
    active.close_elapsed(_at(35))
    assert _prices(active) == [(0, 30, 20)]
    # Current sampling does not preserve an operator after known expiry.
    expired = RateHistory()
    _observe(expired, 25, [_segment(0, 20, 10, kind="replace")])
    assert _prices(expired) == [(0, 30, 20)]


def _test_validation():
    """Reject malformed snapshots atomically without erasing good observations."""
    history = RateHistory()
    _observe(history, 1)
    original = history.snapshot()
    bad_payloads = []
    newer = copy.deepcopy(original)
    newer["schema_version"] = 99
    bad_payloads.append(newer)
    for field, value in (("rate", float("nan")), ("rate", float("inf")), ("start", "2026-10-03T09:00:00"), ("end", _at().isoformat())):
        bad = copy.deepcopy(original)
        bad["records"][0]["automatic"][0][field] = value
        bad_payloads.append(bad)
    future = copy.deepcopy(original)
    future["records"][0]["last_observed_at"] = _at(-1).isoformat()
    bad_payloads.append(future)
    projection = copy.deepcopy(original)
    projection["records"][0]["events"] = [dict(start=_at(10).isoformat(), end=_at(20).isoformat(), rate=1)]
    bad_payloads.append(projection)
    duplicate = copy.deepcopy(original)
    duplicate["records"].append(copy.deepcopy(duplicate["records"][0]))
    bad_payloads.append(duplicate)
    for bad in bad_payloads:
        try:
            history.restore(bad)
        except (ValueError, KeyError, TypeError):
            pass
        else:
            raise AssertionError("Malformed history accepted")
        assert history.snapshot() == original
    try:
        _observe(history, 2, [_segment(kind="unknown")])
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid override kind accepted")


async def _test_storage():
    """A/B writes, retries, corrupt fallback, unsupported schema and restart."""
    storage = _MemoryStorage()
    history = RateHistory()
    _observe(history, 1)
    history.set_context("2026-10-03", 55, premium=True)
    assert history.retain_offer(_segment(0, 60, 5, direction="export", kind="increment", source="axle", stage=3, identity="storage-offer"), _at(1))
    assert await history.save_dirty(storage)
    assert not history.dirty and history.generation == 1
    assert not await history.save_dirty(storage)
    _observe(history, 20, [_segment(rate=7, kind="replace")])
    for result in (False, None):
        storage.result = result
        assert not await history.save_dirty(storage)
        assert history.dirty and history.generation == 1
    storage.error = True
    assert not await history.save_dirty(storage)
    assert history.dirty
    storage.error = False
    storage.result = True
    assert await history.save_dirty(storage)
    assert storage.calls == ["snapshot_a", "snapshot_b", "snapshot_b", "snapshot_b", "snapshot_b"]
    restarted = RateHistory()
    assert await restarted.load(storage)
    assert restarted.contexts == {"2026-10-03": {"standing_charge": 55, "car_premium_present": True}}
    assert restarted.active_offers("export", _at(40))[0]["rate"] == 5
    restarted.close_elapsed(_at(40))
    assert _prices(restarted) == [(0, 30, 7)]
    assert restarted.lookup("import", _at(), _at(30))[0]["quality"] == "last_known"
    # Corrupt newest generation; choose spare based on actual winner, not parity.
    storage.data["snapshot_b"]["checksum"] = "broken"
    fallback = RateHistory()
    assert await fallback.load(storage)
    assert fallback.generation == 1 and _prices(fallback) == [(0, 30, 20)]
    _observe(fallback, 25)
    assert await fallback.save_dirty(storage)
    assert storage.calls[-1] == "snapshot_b"
    assert storage.data["snapshot_a"]["generation"] == 1
    # A checksum-valid but structurally truncated newest payload also falls back.
    broken = copy.deepcopy(storage.data["snapshot_b"])
    del broken["payload"]["records"][0]["automatic"]
    broken["checksum"] = _checksum(broken["payload"], broken["generation"])
    storage.data["snapshot_b"] = broken
    truncated = RateHistory()
    assert await truncated.load(storage)
    assert truncated.generation == 1
    # Preserve newer unknown schema instead of overwriting it with old code.
    newer = copy.deepcopy(storage.data["snapshot_a"])
    newer["generation"] = 9
    newer["payload"]["schema_version"] = 2
    newer["checksum"] = _checksum(newer["payload"], newer["generation"])
    storage.data["snapshot_b"] = newer
    protected = RateHistory()
    assert await protected.load(storage) and protected.write_disabled
    _observe(protected, 25)
    assert not await protected.save_dirty(storage)
    assert storage.data["snapshot_b"] == newer
    # Persisted mutation while awaiting Storage must remain dirty for next save.
    concurrent_storage = _MemoryStorage()
    concurrent = RateHistory()
    _observe(concurrent, 1)
    concurrent_storage.mutate = lambda: _observe(concurrent, 2)
    assert await concurrent.save_dirty(concurrent_storage)
    assert concurrent.dirty
    assert await concurrent.save_dirty(concurrent_storage)
    # Fast IOG evidence polls must not rewrite an identical full snapshot.
    confirmed_storage = _MemoryStorage()
    confirmed = RateHistory()
    assert confirmed.confirm_iog(_at(), _at(30), 6, _at(10), source_id="meter", car_n=0, cap_bucket="noon")
    assert await confirmed.save_dirty(confirmed_storage)
    revision = confirmed._revision
    saved_snapshot = confirmed.snapshot()
    assert confirmed.confirm_iog(_at(), _at(30), 6, _at(10.25), source_id="meter", car_n=0, cap_bucket="noon")
    assert confirmed.confirm_iog(_at(), _at(30), 6, _at(10.5), source_id="meter", car_n=0, cap_bucket="noon")
    assert confirmed._revision == revision and not confirmed.dirty
    assert confirmed.snapshot() == saved_snapshot
    assert not await confirmed.save_dirty(confirmed_storage)
    assert confirmed_storage.calls == ["snapshot_a"]
    # New car allocation is new evidence and must still be persisted.
    assert confirmed.confirm_iog(_at(), _at(30), 6, _at(11), source_id="meter", car_n=1, cap_bucket="noon")
    assert confirmed.dirty and await confirmed.save_dirty(confirmed_storage)
    assert confirmed_storage.calls == ["snapshot_a", "snapshot_b"]
    # An inaccessible backend must not permit replacement of unknown contents.
    inaccessible = _MemoryStorage()
    inaccessible.error = True
    blocked = RateHistory()
    assert not await blocked.load(inaccessible) and blocked.write_disabled


def run_rate_history_tests(my_predbat=None):
    """Run focused pure-core regression tests and return the failed-test count."""
    print("--- Rate history tests ---")
    failed = 0
    for test in (_test_manual, _test_events_and_gaps, _test_confirmed, _test_time_and_baseline, _test_context, _test_offers, _test_offer_resolution, _test_future_manual, _test_validation):
        try:
            test()
            print("PASS", test.__name__)
        except Exception as error:
            failed += 1
            print("FAIL", test.__name__, repr(error))
            import traceback

            traceback.print_exc()
    try:
        asyncio.run(_test_storage())
        print("PASS _test_storage")
    except Exception as error:
        failed += 1
        print("FAIL _test_storage", repr(error))
        import traceback

        traceback.print_exc()
    print("Rate history failures:", failed)
    return failed


if __name__ == "__main__":
    raise SystemExit(run_rate_history_tests())

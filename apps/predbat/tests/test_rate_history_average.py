# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Tests for rate_history_days_average — the historical-mean fallback used by
rate_replicate() when the rate sensor doesn't expose 24h-old slots.
"""

from datetime import datetime, timedelta, timezone


def _seed_history(my_predbat, samples):
    """
    Replace TestHAInterface.history with the supplied list of dicts
    (state, last_changed).
    """
    my_predbat.ha_interface.history = list(samples)
    my_predbat.ha_interface.history_enable = True


def _half_hour_walk(start_utc, count):
    """Yield count half-hour-aligned UTC timestamps starting at start_utc."""
    for i in range(count):
        yield start_utc + timedelta(minutes=30 * i)


def _bucket_of(local_dt):
    return local_dt.hour * 2 + (1 if local_dt.minute >= 30 else 0)


def _test_disabled_returns_none(my_predbat):
    """rate_history_days_average=0 → builder returns None even with seeded history."""
    _seed_history(my_predbat, [{"state": 0.20, "last_changed": datetime.now(timezone.utc)}])
    result = my_predbat.build_rate_history_buckets("sensor.fake_rate", days=0, scaling=100.0)
    assert result is None, "days=0 should disable the feature"


def _test_no_history_returns_none(my_predbat):
    """Empty history → None."""
    _seed_history(my_predbat, [])
    result = my_predbat.build_rate_history_buckets("sensor.fake_rate", days=7, scaling=100.0)
    assert result is None, "empty history should yield None"


def _test_only_unavailable_returns_none(my_predbat):
    """All states unavailable/unknown → None."""
    now = my_predbat.now_utc
    samples = [
        {"state": "unavailable", "last_changed": now - timedelta(hours=1)},
        {"state": "unknown",     "last_changed": now - timedelta(hours=2)},
        {"state": None,           "last_changed": now - timedelta(hours=3)},
    ]
    _seed_history(my_predbat, samples)
    result = my_predbat.build_rate_history_buckets("sensor.fake_rate", days=7, scaling=100.0)
    assert result is None, "only-bad states should yield None"


def _test_scaling_applied(my_predbat):
    """A constant $/kWh history with scaling=100 yields constant c/kWh buckets."""
    now = my_predbat.now_utc
    samples = []
    for ts in _half_hour_walk(now - timedelta(days=3), 48 * 3):
        samples.append({"state": 0.20, "last_changed": ts})
    _seed_history(my_predbat, samples)
    result = my_predbat.build_rate_history_buckets("sensor.fake_rate", days=3, scaling=100.0)
    assert result is not None, "expected buckets"
    assert len(result) == 48, "expected 48 buckets"
    for hh, mean in result.items():
        assert abs(mean - 20.0) < 0.01, "bucket {} expected ~20 c got {}".format(hh, mean)


def _test_diurnal_shape_preserved(my_predbat):
    """Seed an artificial diurnal curve and confirm buckets recover the shape."""
    now = my_predbat.now_utc
    # Build 5 days of synthetic data: each slot's rate depends only on bucket.
    samples = []
    for ts in _half_hour_walk(now - timedelta(days=5), 48 * 5):
        local = ts.astimezone(my_predbat.local_tz)
        bucket = _bucket_of(local)
        # Pattern: cheap overnight (10), expensive evening peak (50), midday trough (15)
        if bucket >= 36 and bucket < 44:        # 18:00 - 22:00 local
            rate = 0.50
        elif bucket >= 22 and bucket < 30:       # 11:00 - 15:00 local
            rate = 0.15
        else:
            rate = 0.20
        samples.append({"state": rate, "last_changed": ts})
    _seed_history(my_predbat, samples)
    result = my_predbat.build_rate_history_buckets("sensor.fake_rate", days=5, scaling=100.0)
    assert result is not None
    assert abs(result[40] - 50.0) < 1.0, "evening peak bucket should be ~50c, got {}".format(result[40])
    assert abs(result[24] - 15.0) < 1.0, "midday bucket should be ~15c, got {}".format(result[24])
    assert abs(result[0]  - 20.0) < 1.0, "overnight bucket should be ~20c, got {}".format(result[0])


def _test_spike_resistance(my_predbat):
    """A single 5-minute spike should not dominate the bucket mean."""
    now = my_predbat.now_utc
    samples = []
    # 7 days of steady 0.15 $/kWh at half-hour cadence
    for ts in _half_hour_walk(now - timedelta(days=7), 48 * 7):
        samples.append({"state": 0.15, "last_changed": ts})
    # Inject a transient spike (a single state-change event) at one moment
    spike_time = now - timedelta(days=2, hours=12, minutes=2)
    samples.append({"state": 1.50, "last_changed": spike_time})
    # And a recovery sample 5 min later
    samples.append({"state": 0.15, "last_changed": spike_time + timedelta(minutes=5)})
    samples.sort(key=lambda r: r["last_changed"])
    _seed_history(my_predbat, samples)
    result = my_predbat.build_rate_history_buckets("sensor.fake_rate", days=7, scaling=100.0)
    assert result is not None
    # Every bucket should be very close to 15c — the spike fell between two slot boundaries.
    for hh, mean in result.items():
        assert mean < 25.0, "bucket {} got {}, spike contaminated it".format(hh, mean)


def _test_replicate_uses_history(my_predbat):
    """rate_replicate fills unknown slots from history_buckets and propagates 24h forward.

    Note on tagging: rate_replicate walks minute-by-minute and adds each filled value
    back into the rates dict. Once a history-derived value lands at minute M, the
    slot at M+1440 will be filled via the 24h-back branch (tag ``copy``), not the
    history branch — but the *value* is identical because it was sourced from history
    one cycle earlier. So the ``history_avg`` tag is expected only within the first
    24 hours of the gap; later slots carry it forward as ``copy`` with the same value.
    """
    rates = {0: 25.0}                                # one known slot at minute=0
    history_buckets = {i: float(i) for i in range(48)}  # distinct per-bucket markers
    out_rates, out_replicated = my_predbat.rate_replicate(
        rates, is_import=True, history_buckets=history_buckets
    )

    # The first filled slot via history is the one immediately after the seed where
    # neither 24h-back nor modulo match — verify at least one minute carries the tag.
    history_tagged = [m for m, tag in out_replicated.items() if tag == "history_avg"]
    assert history_tagged, "expected at least one minute tagged history_avg, got tags={}".format(set(out_replicated.values()))

    # And the value at a future minute should match the historical bucket for the
    # corresponding local time-of-day (regardless of whether the tag is history_avg
    # or 'copy' from a 24h-back propagation of the same bucket).
    sample_minute = 5 * 60        # +5h from midnight_utc
    slot_local = (my_predbat.midnight_utc + timedelta(minutes=sample_minute)).astimezone(my_predbat.local_tz)
    expected_bucket = _bucket_of(slot_local)
    assert out_rates[sample_minute] == float(expected_bucket), (
        "minute {} expected bucket {} value {} got {}".format(sample_minute, expected_bucket, float(expected_bucket), out_rates[sample_minute])
    )


def _test_replicate_prefers_24h_back(my_predbat):
    """When 24h-back is present, history_buckets must NOT be used."""
    # Seed a known 24h-back rate, plus a history_buckets with a clearly different value.
    yesterday_minute = -24 * 60                       # one day before midnight_utc
    rates = {yesterday_minute: 99.0}                  # 24h-back known
    history_buckets = {i: 0.0 for i in range(48)}     # all zeros — would be obvious if used
    out_rates, out_replicated = my_predbat.rate_replicate(
        rates, is_import=True, history_buckets=history_buckets
    )
    # minute = 0 is exactly 24h after yesterday_minute; should copy the 99.0
    assert out_rates[0] == 99.0, "24h-back copy should win over history_avg, got {}".format(out_rates[0])
    assert out_replicated.get(0) == "copy", \
        "expected 'copy' tag for 24h-back, got {}".format(out_replicated.get(0))


def _test_replicate_without_history_unchanged(my_predbat):
    """Default behavior (history_buckets=None) is unchanged: falls to rate_last."""
    rates = {0: 25.0}
    out_rates, out_replicated = my_predbat.rate_replicate(rates, is_import=True)
    # When history is absent and no other branch matches, the existing rate_last
    # fallback applies — minute 60 should equal rate_last (25.0).
    assert out_rates[60] == 25.0, "without history, fallback should be rate_last (25.0), got {}".format(out_rates[60])
    assert out_replicated.get(60) == "copy"


def test_rate_history_average(my_predbat):
    """Top-level harness compatible with the unit_test.py registry."""
    sub_tests = [
        ("disabled_returns_none",            _test_disabled_returns_none),
        ("no_history_returns_none",          _test_no_history_returns_none),
        ("only_unavailable_returns_none",    _test_only_unavailable_returns_none),
        ("scaling_applied",                  _test_scaling_applied),
        ("diurnal_shape_preserved",          _test_diurnal_shape_preserved),
        ("spike_resistance",                 _test_spike_resistance),
        ("replicate_uses_history",           _test_replicate_uses_history),
        ("replicate_prefers_24h_back",       _test_replicate_prefers_24h_back),
        ("replicate_without_history_unchanged", _test_replicate_without_history_unchanged),
    ]

    print("\n" + "=" * 70)
    print("RATE HISTORY AVERAGE TEST SUITE")
    print("=" * 70)

    failed = 0
    passed = 0
    for name, func in sub_tests:
        try:
            func(my_predbat)
            print("  PASS  {}".format(name))
            passed += 1
        except AssertionError as exc:
            print("  FAIL  {}: {}".format(name, exc))
            failed += 1
        except Exception as exc:  # pylint: disable=broad-except
            print("  ERROR {}: {!r}".format(name, exc))
            failed += 1

    print("Result: {} passed, {} failed".format(passed, failed))
    return failed

"""Regression checks for externally supplied intervals crossing planning bounds."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from axle import load_axle_slot
from utils import minute_data


def test_interval_bounds(my_predbat=None):
    """Bound extreme timestamps and retain overlap and interpolation semantics."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def row(start, end, value):
        """Build a rate record at offsets from the fixed baseline."""
        return {"from": (now + timedelta(minutes=start)).isoformat(), "to": (now + timedelta(minutes=end)).isoformat(), "value": value}

    def convert(rows, **kwargs):
        """Convert a bounded one-day rate window using the real helper."""
        return minute_data(rows, 1, now, "value", "from", to_key="to", **kwargs)[0]

    # One tiny record must not traverse centuries before reaching the retained window.
    extreme = [{"from": "1900-01-01T00:00:00+00:00", "to": "2100-01-01T00:00:00+00:00", "value": 7}]
    data = convert(extreme)
    assert len(data) == 2881 and data[-1440] == data[1440] == 7
    assert convert([row(-2, 3, 9)]) == dict.fromkeys(range(-2, 3), 9)
    assert convert([row(1441, 1441, 9)]) == {}
    assert convert([row(-1441, -1441, 9)]) == {}

    # Truncating traversal must not restart interpolation at the clipped boundary.
    data = convert([row(-1501, -1500, 0), row(-1500, 0, 60)], smoothing=True)
    assert abs(data[-1440] - 2.4) < 1e-9 and abs(data[-1] - 59.96) < 1e-9
    data = convert([row(1501, 1500, 0), row(1500, 0, 60)], smoothing=True, backwards=True)
    assert abs(data[-1440] - 57.6) < 1e-9 and abs(data[-1] - 0.04) < 1e-9
    data = convert([row(4, 2, 5), row(2, 0, 8)], backwards=True)
    assert data[-4] == 5 and data[-3] == 5 and data[-2] == 8 and data[-1] == 5 and data[0] == 5

    for direction, export, expected in (("export", True, 2), ("export", False, 2), ("import", False, -2)):
        base = SimpleNamespace(midnight_utc=now, minutes_now=0, forecast_minutes=60, load_scaling_dynamic={}, load_scaling_saving=0.5, load_scaling_free=0, log=lambda message: None, time_abs_str=str)
        event = {"start_time": "1900-01-01T00:00:00+00:00", "end_time": "2100-01-01T00:00:00+00:00", "import_export": direction, "pence_per_kwh": 2}
        rates = {}
        load_axle_slot(base, [event], rates, export)
        assert rates == dict.fromkeys(range(60), expected)
        # Yesterday's overlays still feed historical cost calculations.
        rates = dict.fromkeys(range(-1440, 60), 0)
        load_axle_slot(base, [event], rates, export)
        assert rates == dict.fromkeys(range(-1440, 60), expected)
        event.update(start_time=(now + timedelta(minutes=10)).isoformat(), end_time=(now + timedelta(minutes=20)).isoformat())
        rates = {}
        load_axle_slot(base, [event], rates, export)
        assert rates == dict.fromkeys(range(10, 20), expected)
    return False

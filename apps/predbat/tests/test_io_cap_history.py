"""Regression tests for price-only confirmed IO cap allocations."""

from octopus import Octopus


class CapReader(Octopus):
    """Exercise the real price overlay with isolated dispatch dependencies."""

    def __init__(self, cap=1, minutes_now=610):
        """Supply only the price overlay's existing dependencies."""
        self.cap = cap
        self.minutes_now = minutes_now
        self.plan_interval_minutes = 30
        self.forecast_minutes = 2880
        self.rate_min_base = 5.0
        self.rate_max_base = 10.0
        self.rate_min = 5.0
        self.io_adjusted = {}
        self.octopus_surplus = set()
        self.cancelled = False
        self.car_charging_slots = [{"start": 600, "end": 660}]

    def get_arg(self, name, default=None):
        """Keep the existing low-rate switch enabled."""
        return default

    def get_octopus_slot_max(self):
        """Return this isolated car's cap."""
        return self.cap

    def dynamic_load_car_strip_from(self, car_n):
        """Represent command cancellation without touching persisted prices."""
        return self.minutes_now if self.cancelled else None

    def decode_octopus_slot(self, car_n, slot, raw=False):
        """Return already-decoded dispatch bounds for the price algorithm."""
        return slot["start"], slot["end"], 1.0, "smart-charge", "AT_HOME"

    def dispatch_billed_off_peak(self, source, location, end_minutes):
        """Make these test dispatches eligible, independently of price retention."""
        return True

    def time_abs_str(self, minute):
        """Format a diagnostic minute without a real Predbat clock."""
        return str(minute)

    def log(self, message):
        """Discard diagnostics from isolated tests."""
        pass


def run_io_cap_history_tests(my_predbat=None):
    """Run reservation tests without mutating a shared Predbat fixture."""
    failed = 0
    for check in (test_reserved_cap_survives_withdrawal, test_confirmed_tail_survives_cancellation, test_unique_reservations_and_allocations, test_original_bucket_and_car_isolation, test_exact_rate_and_surplus_compatibility):
        try:
            check()
            print("PASS:", check.__name__)
        except AssertionError as error:
            print("ERROR:", check.__name__, error)
            failed += 1
    return failed


def test_reserved_cap_survives_withdrawal():
    """A vanished confirmed morning block consumes the same-bucket future cap."""
    reader = CapReader(minutes_now=650)
    rates = dict.fromkeys(range(600, 700), 10.0)
    allocations = {}
    reader.rate_add_io_slots(0, rates, [{"start": 660, "end": 690}], history_reservations=[{"slot_start": 600, "day_offset": -1, "rate": 5.0}], history_allocations=allocations)
    assert all(rates[minute] == 10.0 for minute in range(660, 690))
    assert allocations == {600: {"day_offset": -1, "rate": 5.0}}
    assert rates[600] == 10.0  # Never rewrite the past from reservation data.


def test_confirmed_tail_survives_cancellation():
    """An unplugged/cancelled car retains its whole current confirmed half-hour."""
    reader = CapReader()
    reader.cancelled = True
    commands = list(reader.car_charging_slots)
    rates = dict.fromkeys(range(600, 690), 10.0)
    reservations = [{"slot_start": 600, "day_offset": -1, "rate": 4.0}, {"slot_start": 660, "day_offset": -1, "rate": 4.0}]
    reader.io_adjusted = {600: True, 610: True, 629: True, 630: True, 660: True}
    reader.rate_add_io_slots(0, rates, [], history_reservations=reservations)
    assert all(rates[minute] == 4.0 for minute in range(600, 630))
    assert all(rates[minute] == 10.0 for minute in range(630, 690))
    assert reader.car_charging_slots == commands
    assert reader.io_adjusted == {630: True, 660: True}
    # Raw strip_from cannot override confirmed pricing either.
    rates = dict.fromkeys(range(600, 690), 10.0)
    reader.rate_add_io_slots(0, rates, [{"start": 600, "end": 630}], history_reservations=reservations[:1])
    assert rates[600] == rates[610] == rates[629] == 4.0 and rates[630] == 10.0


def test_unique_reservations_and_allocations():
    """Duplicate reservations and matching API rows consume the cap only once."""
    reader = CapReader(cap=2)
    rates = dict.fromkeys(range(600, 700), 10.0)
    reservation = {"slot_start": 600, "day_offset": -1, "rate": 4.0}
    allocations = {}
    reader.rate_add_io_slots(0, rates, [{"start": 600, "end": 690}], history_reservations=[reservation, dict(reservation)], history_allocations=allocations)
    assert rates[610] == 4.0 and rates[630] == rates[659] == 5.0 and rates[660] == 10.0
    assert allocations == {600: {"day_offset": -1, "rate": 4.0}, 630: {"day_offset": -1, "rate": 5.0}}
    # Allocation-only callers retain the default price algorithm.
    reader = CapReader(cap=1)
    rates = dict.fromkeys(range(600, 690), 10.0)
    allocations = {}
    reader.rate_add_io_slots(0, rates, [{"start": 630, "end": 690}], history_allocations=allocations)
    assert rates[630] == 5.0 and rates[660] == 10.0
    assert allocations == {630: {"day_offset": -1, "rate": 5.0}}


def test_exact_rate_and_surplus_compatibility():
    """Reservations keep the exact price winner and don't trust surplus forecasts."""
    reader = CapReader(cap=2)
    reader.rate_min_base = 5.23
    rates = dict.fromkeys(range(600, 690), 5.2314)
    allocations = {}
    reader.rate_add_io_slots(0, rates, [{"start": 600, "end": 630}], history_allocations=allocations)
    assert rates[600] == allocations[600]["rate"] == 5.2314
    assert not reader.io_adjusted
    reader.rate_add_io_slots(0, rates, [], history_reservations=[dict(allocations[600], slot_start=600)])
    assert all(rates[minute] == 5.2314 for minute in range(600, 630))
    reader = CapReader(cap=2)
    reader.octopus_surplus = set(range(600, 690))
    rates = dict.fromkeys(range(600, 690), 10.0)
    allocations = {}
    reader.rate_add_io_slots(0, rates, [{"start": 600, "end": 690}], history_reservations=[{"slot_start": 600, "day_offset": -1, "rate": 5.0}], history_allocations=allocations)
    assert all(rates[minute] == 5.0 for minute in range(600, 630))
    assert all(rates[minute] == 10.0 for minute in range(630, 690))
    assert set(allocations) == {600}


def test_original_bucket_and_car_isolation():
    """Reserve the original bucket, not a recomputed noon or account-wide cap."""
    reader = CapReader(minutes_now=730)
    rates = dict.fromkeys(range(720, 810), 10.0)
    allocations = {}
    reader.rate_add_io_slots(0, rates, [{"start": 750, "end": 780}], history_reservations=[{"slot_start": 720, "day_offset": -1, "rate": 4.0}], history_allocations=allocations)
    assert rates[730] == 4.0 and rates[750] == 5.0
    assert allocations[720]["day_offset"] == -1 and allocations[750]["day_offset"] == 0
    other_car_rates = dict.fromkeys(range(600, 690), 10.0)
    reader.rate_add_io_slots(1, other_car_rates, [{"start": 600, "end": 630}])
    assert other_car_rates[600] == 5.0  # Reservations supplied only for their car.


if __name__ == "__main__":
    raise SystemExit(run_io_cap_history_tests())

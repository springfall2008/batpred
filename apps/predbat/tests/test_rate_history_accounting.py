"""Regression tests for isolated historical-price cost accounting."""

import copy
from datetime import datetime, timedelta, timezone

import pytz
from types import SimpleNamespace

from output import Output, historical_cost_series
from tests.test_calculate_yesterday import _setup_base, _apply_mocks


def _increment(data, index):
    """Read one minute from a backwards cumulative energy series."""
    return max(data.get(index, 0) - data.get(index + 1, 0), 0)


def _test_endpoints():
    """A terminal endpoint includes the final minute and negative prices."""
    imported = {0: 1, 1: 2, 2: 3}
    exported = {0: 0, 1: 1, 2: 2}
    rates_import = {0: 10, 1: 0, 2: -5}
    rates_export = {0: 2, 1: 3, 2: 4}
    series = historical_cost_series(imported, exported, rates_import, rates_export, 0, 3, {0: 50})
    assert series[0] == {0: 0, 1: 60, 2: 57, 3: 34}
    assert series[1][3] == -5
    assert series[2][3] == -11
    assert series[0][3] - series[0][2] == -23
    assert historical_cost_series(imported, exported, {0: 10}, rates_export, 0, 3, {}) is None
    assert historical_cost_series({}, exported, rates_import, rates_export, 0, 3, {}) is None
    imported[2] = 4
    assert historical_cost_series(imported, exported, rates_import, rates_export, 0, 3, {0: 50})[0][3] == 29


def _test_today():
    """Today/hour use archived prices, late energy recalculates, save=False isolates."""
    published = {}
    pb = SimpleNamespace(
        minutes_now=30,
        midnight_utc=datetime(2024, 10, 4, tzinfo=timezone.utc),
        currency_symbols=["£", "p"],
        soc_kwh_history={},
        metric_battery_value_scaling=1,
        battery_value_rate=lambda minute: 20,
        carbon_enable=False,
        num_cars=0,
        car_charging_slots=[],
        metric_standing_charge=50,
        prefix="predbat",
        log=lambda message: None,
        filtered_times=lambda results: results,
        get_from_incrementing=_increment,
        dashboard_item=lambda entity, state, attributes: published.update({entity: (state, attributes)}),
        rate_import={minute: 20 for minute in range(-60, 31)},
        rate_export={minute: 5 for minute in range(-60, 31)},
        rate_history_import={minute: 7 for minute in range(-60, 30)},
        rate_history_export={minute: 10 for minute in range(-60, 30)},
        rate_history_accounting_enabled=True,
    )
    energy = {minute: max(30 - minute, 0) / 30 for minute in range(61)}
    exported = {minute: max(30 - minute, 0) / 60 for minute in range(61)}
    live_before = copy.deepcopy((pb.rate_import, pb.rate_export))
    historical_before = copy.deepcopy((pb.rate_history_import, pb.rate_history_export))
    cost, _ = Output.today_cost(pb, energy, exported, {}, energy)
    assert abs(cost - 52) < 1e-8
    assert published["predbat.cost_hour"][0] == 2
    count = len(published)
    cost, _ = Output.today_cost(pb, energy, exported, {}, energy, save=False)
    assert abs(cost - 67.5) < 1e-8
    assert len(published) == count
    energy[0] += 1
    cost, _ = Output.today_cost(pb, energy, exported, {}, energy)
    assert abs(cost - 59) < 1e-8
    assert (pb.rate_import, pb.rate_export) == live_before
    assert (pb.rate_history_import, pb.rate_history_export) == historical_before
    # Preserve the existing scheduled-kWh capped-car premium formula.
    pb.num_cars = 1
    pb.car_charging_slots = [[{"start": 0, "average": 12, "kwh": 2}]]
    assert abs(Output.today_cost(pb, energy, exported, energy, energy)[0] - 69) < 1e-8
    pb.num_cars = 0
    pb.rate_history_standing_charge = {0: 60}
    assert abs(Output.today_cost(pb, energy, exported, {}, energy)[0] - 69) < 1e-8
    pb.rate_history_accounting_enabled = False
    assert abs(Output.today_cost(pb, energy, exported, {}, energy)[0] - 87.5) < 1e-8


def _test_yesterday(pb):
    """Actual prices, cost and terminal curve agree despite stale recorded costs."""
    original = pb.__dict__.copy()
    try:
        now = _setup_base(pb)
        _apply_mocks(pb, now, cost_value=999)
        horizon = 1440 + pb.minutes_now
        pb.import_today = {index: (horizon - index) / 1440 for index in range(horizon + 1)}
        pb.export_today = {index: 0 for index in range(horizon + 1)}
        pb.load_minutes = dict(pb.import_today)
        pb.rate_history_accounting_enabled = True
        pb.rate_history_calendar_day_minutes = 1440
        pb.rate_history_calendar_origin_valid = True
        pb.rate_history_import = {minute: 7 for minute in range(-1440, pb.minutes_now)}
        pb.rate_history_export = {minute: 5 for minute in range(-1440, pb.minutes_now)}
        pb.rate_history_no_io = {minute: 20 for minute in range(-1440, pb.minutes_now)}
        pb.rate_history_standing_charge = {-1440: 50, 0: 50}
        pb.rate_history_coverage_import = set(pb.rate_history_import)
        pb.rate_history_coverage_export = set(pb.rate_history_export)
        published = {}
        curves = []
        pb.dashboard_item = lambda entity, state, attributes: published.update({entity: (state, attributes)})

        def capture_plan(*args, **kwargs):
            """Capture actual metric endpoints and the displayed price tables."""
            curves.append((dict(pb.predict_metric_best), dict(pb.rate_import)))
            return "", "{}"

        pb.publish_html_plan = capture_plan
        live_before = pb.rate_import
        pb.calculate_yesterday()
        assert abs(pb.savings_today_actual - 57) < 1e-7
        assert published[pb.prefix + ".cost_yesterday"][0] == 57
        curve, rates = curves[-1]
        assert curve[0] == 0
        assert abs(curve[1440] - 57) < 1e-7
        assert abs(curve[1440] - curve[1439] - 7 / 1440) < 1e-7
        assert abs(curve[horizon] - (107 + 7 * pb.minutes_now / 1440)) < 1e-7
        assert rates[0] == 7
        assert pb.rate_import is live_before
        # Late readings change yesterday without altering the saved prices.
        index = pb.minutes_now
        pb.import_today[index] += 1
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert abs(pb.savings_today_actual - 64) < 1e-7
        assert all(rate == 7 for rate in pb.rate_history_import.values())
        # Complete archive inputs work without recorded cost_today history.
        history = pb.get_history_wrapper
        pb.get_history_wrapper = lambda entity_id, **kwargs: None if entity_id == pb.prefix + ".cost_today" else history(entity_id, **kwargs)
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert abs(pb.savings_today_actual - 64) < 1e-7
        # Baseline window detection uses the distinct archived no-IO profile.
        scans = []
        pb.calculate_savings_max_charge_slots = 1
        pb.rate_history_no_io[-1] = 10
        pb.rate_scan_window = lambda rates, *args, **kwargs: (scans.append(dict(rates)) or [], 0, 0)
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert scans[-1][0] == 20
        assert scans[-1][1439] == 10
        assert pb.rate_history_import[-1] == 7
        pb.calculate_savings_max_charge_slots = 0
        # Missing standing-charge evidence retains recorded total fallback.
        pb.get_history_wrapper = history
        pb.rate_history_standing_charge = {}
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert pb.savings_today_actual == 999
        # An archived price gap is never promoted to zero-priced evidence.
        pb.rate_history_standing_charge = {-1440: 50, 0: 50}
        pb.rate_history_coverage_import.remove(-1)
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert pb.savings_today_actual == 999
        # Ordinary IOG/Ohme car energy still gets archived house-price costs.
        pb.rate_history_coverage_import.add(-1)
        pb.num_cars = 1
        pb.car_charging_slots = [[]]
        pb.car_charging_soc = [0]
        pb.car_charging_limit = [0]
        pb.car_charging_energy = {index: (horizon - index) / 5760 for index in range(horizon + 1)}
        pb.rate_history_car_premium_present = {-1440: False, 0: False}
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert abs(pb.savings_today_actual - 64) < 1e-7
        assert abs(pb.cost_yesterday_car - 1.75) < 1e-7
        # Known capped-car premium remains the existing recorded-total fallback.
        pb.rate_history_car_premium_present[-1440] = True
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert pb.savings_today_actual == 999
        # Unknown premium evidence also retains the recorded fallback.
        pb.rate_history_car_premium_present = {}
        pb.savings_last_updated = None
        pb.calculate_yesterday()
        assert pb.savings_today_actual == 999
    finally:
        pb.__dict__.clear()
        pb.__dict__.update(original)


def _test_calendar_guard(pb):
    """Full archives on actual DST days cannot justify partial 1440-minute costs.

    The legacy renderer remains unchanged: spring/autumn and shifted table
    origins must use recorded HA costs until an exact-day axis is supported.
    """
    original = pb.__dict__.copy()
    zone = pytz.timezone("Europe/London")
    try:
        for yesterday_date, expected_minutes, shifted in ((datetime(2024, 3, 31), 1380, False), (datetime(2024, 10, 27), 1500, False), (datetime(2024, 10, 3), 1440, True)):
            _setup_base(pb)
            yesterday_midnight = zone.localize(yesterday_date)
            today_midnight = zone.localize(yesterday_date + timedelta(days=1))
            day_minutes = int((today_midnight.astimezone(timezone.utc) - yesterday_midnight.astimezone(timezone.utc)).total_seconds() / 60)
            assert day_minutes == expected_minutes
            pb.midnight_utc = today_midnight + (timedelta(minutes=30) if shifted else timedelta())
            pb.now_utc = today_midnight + timedelta(minutes=pb.minutes_now)
            _apply_mocks(pb, pb.now_utc, cost_value=999)
            horizon = max(day_minutes, 1440) + pb.minutes_now
            pb.import_today = {index: (horizon - index) / 1440 for index in range(horizon + 1)}
            pb.export_today = {index: 0 for index in range(horizon + 1)}
            pb.load_minutes = dict(pb.import_today)
            pb.rate_history_accounting_enabled = True
            pb.rate_history_calendar_day_minutes = day_minutes
            pb.rate_history_calendar_origin_valid = not shifted
            # Cover both the complete calendar day and the legacy axis, so
            # coverage/energy gaps cannot accidentally mask the DST defect.
            pb.rate_history_import = {minute: 7 for minute in range(-max(day_minutes, 1440), pb.minutes_now)}
            pb.rate_history_export = {minute: 5 for minute in pb.rate_history_import}
            pb.rate_history_no_io = {minute: 20 for minute in pb.rate_history_import}
            pb.rate_history_coverage_import = set(pb.rate_history_import)
            pb.rate_history_coverage_export = set(pb.rate_history_export)
            pb.rate_history_standing_charge = {-1440: 50, 0: 50}
            pb.rate_history_car_premium_present = {-1440: False, 0: False}
            messages = []
            pb.log = messages.append
            pb.calculate_yesterday()
            assert pb.savings_today_actual == 999
            assert any("legacy 1440-minute history axis requires recorded cost fallback" in message for message in messages)
    finally:
        pb.__dict__.clear()
        pb.__dict__.update(original)


def run_rate_history_accounting_tests(my_predbat):
    """Run the accounting regressions using the project's Predbat fixture."""
    failed = False
    for name, test in (("endpoints", _test_endpoints), ("today", _test_today), ("yesterday", lambda: _test_yesterday(my_predbat)), ("calendar_guard", lambda: _test_calendar_guard(my_predbat))):
        try:
            test()
            print("rate_history_accounting: {} passed".format(name))
        except Exception:
            import traceback

            traceback.print_exc()
            print("ERROR: rate_history_accounting: {} failed".format(name))
            failed = True
    return failed

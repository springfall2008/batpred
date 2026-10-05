"""Tests for source-aware history integration and live/replay isolation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rate_history import RateHistory
from rate_history_adapter import current_periods, history_reservations, observation_time, prepare_history_inputs, record_iog_confirmation, record_metered_dispatches, update_rate_history


def _time(hour=10, minute=5, day=3):
    """Return an unambiguous test instant."""
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc)


def _base():
    """Build only the state consumed by the adapter."""
    midnight = _time(0, 0)
    rates = dict.fromkeys(range(-2880, 2880), 20.0)
    base = SimpleNamespace(
        args={"timezone": "UTC"},
        plan_interval_minutes=30,
        midnight_utc=midnight,
        now_utc=_time(),
        now_utc_real=_time(),
        minutes_now=605,
        rate_import=rates.copy(),
        rate_export=dict.fromkeys(rates, 5.0),
        rate_import_no_io=rates.copy(),
        rate_export_base=dict.fromkeys(rates, 5.0),
        rate_history=RateHistory(timezone="UTC"),
        rate_history_replay=False,
        rate_history_iog_allocations={},
        rate_history_iog_allocation_origin=midnight,
        metric_standing_charge=50.0,
        car_charging_slots=[],
        dynamic_load_car_confirmed={},
        dynamic_load_car_sensors={},
        axle_sessions=[],
        octopus_saving_slots=[],
        octopus_free_slots=[],
        components=None,
    )
    base.get_arg = lambda key, default=None, **kwargs: base.args.get(key, default)
    return base


def _update(base, now, replacement=None, automatic=None):
    """Run one live observation with the existing parser's closing trace."""
    base.now_utc_real, base.now_utc = now, now
    base.minutes_now = int((now - base.midnight_utc).total_seconds() / 60)
    inputs = prepare_history_inputs(base, [], [], now)
    if replacement is not None:
        for minute in inputs["import"]["trace"]["minutes"]:
            parent = inputs["import"]["periods"][0]
            start = int((parent["start"] - base.midnight_utc).total_seconds() // 60)
            end = int((parent["end"] - base.midnight_utc).total_seconds() // 60)
            inputs["import"]["trace"]["operations"].append({"minute": minute, "start": start, "end": end, "kind": "replace", "rate": replacement, "source": "manual"})
    automatic = automatic or base.rate_import_no_io
    update_rate_history(base, inputs, base.rate_import_no_io, automatic, base.rate_export_base, now)
    return inputs


def test_manual_closing():
    """Manual changes and removal determine the whole period at its close."""
    base = _base()
    _update(base, _time(10, 5), 10)
    _update(base, _time(10, 15), 15)
    _update(base, _time(10, 20))
    _update(base, _time(10, 30), 99)
    closed = base.rate_history.lookup("import", _time(10, 0), _time(10, 30), include_open=False)
    assert len(closed) == 1 and closed[0]["rate"] == 20
    assert base.rate_import[600] == 20, "Historical values must not overwrite planning tables"
    assert base.rate_history_standing_charge[0] == 50

    base = _base()
    _update(base, _time(10, 5), 10)
    _update(base, _time(10, 25), 15)
    _update(base, _time(10, 30))
    assert base.rate_history.lookup("import", _time(10, 0), _time(10, 30), False)[0]["rate"] == 15


def test_exact_events():
    """An observed short event survives a rebuild after its end."""
    base = _base()
    base.axle_sessions = [{"start_time": _time(10, 10).isoformat(), "end_time": _time(10, 20).isoformat(), "import_export": "export", "pence_per_kwh": 100.0}]
    automatic = base.rate_import_no_io.copy()
    automatic.update(dict.fromkeys(range(610, 620), 120.0))
    _update(base, _time(10, 15), automatic=automatic)
    base.axle_sessions = []
    _update(base, _time(10, 25))
    _update(base, _time(10, 30))
    segments = base.rate_history.lookup("import", _time(10, 0), _time(10, 30), False)
    assert [segment["rate"] for segment in segments] == [20, 120, 20]
    assert segments[1]["start"] == _time(10, 10).isoformat()
    assert segments[1]["end"] == _time(10, 20).isoformat()


def test_future_events_and_periods():
    """A future offer or forecast period isn't promoted after downtime."""
    base = _base()
    base.axle_sessions = [{"start_time": _time(10, 20).isoformat(), "end_time": _time(10, 30).isoformat(), "import_export": "export", "pence_per_kwh": 100.0}]
    automatic = base.rate_import_no_io.copy()
    automatic.update(dict.fromkeys(range(620, 630), 120.0))
    _update(base, _time(10, 5), automatic=automatic)
    base.rate_history.close_elapsed(_time(11, 0))
    assert all(segment["rate"] == 20 for segment in base.rate_history.lookup("import", _time(10, 0), _time(11, 0)))
    assert not base.rate_history.lookup("import", _time(10, 30), _time(11, 0))


def test_cross_parent_offer_restart():
    """An active offer survives disappearance across slots and changed tariffs."""
    base = _base()
    base.axle_sessions = [{"start_time": _time(10, 20).isoformat(), "end_time": _time(11, 10).isoformat(), "import_export": "export", "pence_per_kwh": 100.0}]
    _update(base, _time(10, 25))
    restored = RateHistory(timezone="UTC")
    restored.restore(base.rate_history.snapshot())
    base.rate_history = restored
    base.axle_sessions = []
    base.rate_import_no_io.update(dict.fromkeys(range(630, 720), 30.0))
    _update(base, _time(10, 35))
    assert base.rate_history_import[635] == 130.0
    assert base.rate_history_export[635] == 105.0
    assert not base.rate_history.lookup("import", _time(11, 0), _time(11, 30))
    _update(base, _time(11, 15))
    assert base.rate_history_import[665] == 130.0
    assert base.rate_history_import[675] == 30.0


def test_future_overlap_not_retained():
    """An unobserved future event cannot split away an observed offer's tail."""
    base = _base()
    base.axle_sessions = [
        {"start_time": _time(10, 0).isoformat(), "end_time": _time(10, 30).isoformat(), "import_export": "export", "pence_per_kwh": 20.0},
        {"start_time": _time(10, 20).isoformat(), "end_time": _time(10, 30).isoformat(), "import_export": "export", "pence_per_kwh": 70.0},
    ]
    _update(base, _time(10, 5))
    base.axle_sessions = []
    _update(base, _time(10, 25))
    assert base.rate_history_import[625] == 40.0


def test_managed_null_withdrawal():
    """Managed null is a withdrawal, not a sticky BYOK guarantee."""
    base = _base()
    rows = [{"start_time": _time(10, 0).isoformat(), "end_time": _time(10, 30).isoformat(), "pence_per_kwh": 100.0}]
    component = SimpleNamespace(managed_mode=True, managed_price_periods=rows)
    base.components = SimpleNamespace(get_component=lambda name: component if name == "axle" else None)
    _update(base, _time(10, 5))
    assert base.rate_history_import[605] == 120.0
    rows[0]["pence_per_kwh"] = None
    _update(base, _time(10, 15))
    assert base.rate_history_import[615] == 20.0
    _update(base, _time(10, 35))
    assert base.rate_history_import[605] == 20.0


def test_premium_presence_retained():
    """Known applied premiums remain a coverage limit after dispatch expiry."""
    base = _base()
    base.car_charging_slots = [[{"start": 600, "end": 630, "average": 30.0, "kwh": 1.0}]]
    _update(base, _time(10, 5))
    assert base.rate_history_car_premium_present[0]
    base.car_charging_slots = [[]]
    _update(base, _time(10, 35))
    assert base.rate_history_car_premium_present[0]
    restored = RateHistory(timezone="UTC")
    restored.restore(base.rate_history.snapshot())
    assert restored.accounting_context["2026-01-03"]["car_premium_present"]


def test_provider_bounds_and_clock():
    """Supplied periods override UI width; observations aren't model-rounded."""
    base = _base()
    base.plan_interval_minutes = 60
    supplied = [{"start": _time(10, 0), "end": _time(10, 15), "kind": "interval", "source_id": "provider"}]
    periods = current_periods(base, supplied, _time(10, 5), "import")
    assert periods[0]["end"] == _time(10, 15)
    base.plan_interval_minutes = 15
    inferred = [{"start": _time(10, 0), "end": _time(11, 0), "kind": "interval", "bounds": "cadence"}]
    periods = current_periods(base, inferred, _time(10, 5), "import")
    assert periods[0]["end"] == _time(10, 15) and periods[0]["boundary_origin"] == "configured"
    base.now_utc_real = _time(10, 7).replace(second=45)
    base.args["clock_skew"] = 2
    assert observation_time(base) == _time(10, 9).replace(second=45)
    base.args = {"timezone": "Europe/London"}
    base.midnight_utc = datetime(2026, 10, 25, tzinfo=timezone.utc)
    start = datetime(2026, 10, 25, tzinfo=timezone.utc)
    base.rate_history = RateHistory(timezone="Europe/London")
    base.rate_history.confirm_iog(start, start + timedelta(minutes=30), 6.0, start + timedelta(minutes=5), car_n=0, cap_bucket="2026-10-24T11:00:00+00:00")
    assert history_reservations(base, 0)[0]["day_offset"] == -1, "DST must not move the original noon bucket into another date"


def test_confirmation_evidence():
    """Only fresh actual car readings plus accepted cap evidence confirm prices."""
    base = _base()
    base.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_slots"]
    base.dynamic_load_car_sensors = {0: "sensor.car_power"}
    base.rate_history_iog_eligibility = {0: [{"start": _time(10, 0), "end": _time(10, 30)}]}
    base.rate_history_iog_allocations = {0: {600: {"day_offset": -1, "rate": 6.0}}}
    raw = {"state": "3200", "last_changed": _time(10, 3).isoformat()}
    base.get_state_wrapper = lambda entity, raw=False, **kwargs: dict(raw_state) if raw else raw_state["state"]
    raw_state = raw
    base.car_charging_now_value = lambda value: float(value) >= 200
    assert record_iog_confirmation(base, 0, _time(10, 30), _time(10, 5))
    assert base.rate_history.lookup("import", _time(10, 0), _time(10, 30))[0]["rate"] == 6
    raw_state["state"] = "0"
    assert not record_iog_confirmation(base, 0, _time(10, 30), _time(10, 6))
    raw_state["state"] = "3200"
    raw_state["last_changed"] = _time(9, 55).isoformat()
    assert not record_iog_confirmation(base, 0, _time(10, 30), _time(10, 6))
    raw_state["last_changed"] = _time(10, 31).isoformat()
    assert not record_iog_confirmation(base, 0, _time(10, 30), _time(10, 31))
    raw_state["last_changed"] = _time(10, 3).isoformat()
    base.rate_history_iog_eligibility = {0: [{"start": _time(10, 20), "end": _time(10, 30)}]}
    assert not record_iog_confirmation(base, 0, _time(10, 30), _time(10, 5))
    base.rate_history_iog_eligibility = {0: [{"start": _time(10, 0), "end": _time(10, 30)}]}
    base.rate_history_replay = True
    assert not record_iog_confirmation(base, 0, _time(10, 30), _time(10, 6))


def test_metered_completed_dispatches():
    """A direct metered row qualifies; elapsed schedules and long totals don't."""
    base = _base()
    entity = "binary_sensor.predbat_octopus_intelligent_dispatch"
    base.args["octopus_intelligent_slot"] = [entity]
    base.rate_history_iog_allocations = {0: {600: {"day_offset": -1, "rate": 6.0}}}
    rows = [{"start": _time(10, 5).isoformat(), "end": _time(10, 15).isoformat(), "charge_in_kwh": 1.0, "source": "smart-charge"}]
    component = SimpleNamespace(
        get_intelligent_devices=lambda: {"device": {"completed_dispatches": rows}},
        get_entity_name=lambda *args, **kwargs: entity,
        device_id_to_index_suffix=lambda device: "",
    )
    base.components = SimpleNamespace(get_component=lambda name: component if name == "octopus" else None)
    record_metered_dispatches(base, _time(10, 20))
    assert base.rate_history.lookup("import", _time(10, 0), _time(10, 30))[0]["rate"] == 6
    base.rate_history = RateHistory(timezone="UTC")
    rows[0]["end"] = _time(11, 0).isoformat()
    record_metered_dispatches(base, _time(11, 5))
    assert not base.rate_history.records
    rows[0]["end"] = _time(10, 15).isoformat()
    rows[0]["source"] = "BOOST"
    record_metered_dispatches(base, _time(10, 20))
    assert not base.rate_history.records
    rows[0]["source"] = "smart-charge"
    base.components = SimpleNamespace(get_component=lambda name: component if name == "ohme" else None)
    record_metered_dispatches(base, _time(10, 20))
    assert not base.rate_history.records


def test_fetch_readonly():
    """A comparison fetch doesn't load, close, observe or write live history."""
    from fetch import Fetch

    base = _base()
    _update(base, _time())
    before = base.rate_history.snapshot()
    base.octopus_slots = []
    base.num_cars = 0
    base.currency_symbols = ["GBP", "p"]
    base.rate_history_accounting_enabled = True

    class StopFetch(Exception):
        """Stop after the fetch's isolation guard without invoking live APIs."""

    def stop():
        """Abort after the initial state reset."""
        raise StopFetch()

    base.combine_active_keep = stop
    try:
        Fetch.fetch_sensor_data(base, save=False)
    except StopFetch:
        pass
    assert base.rate_history.snapshot() == before
    assert not base.rate_history_accounting_enabled


def _live_base():
    """Prepare real fetch orchestration with deterministic local data readers."""
    from unit_test import create_predbat

    base = create_predbat()
    base.args = {"timezone": "UTC", "load_today": "sensor.load", "import_today": "sensor.import", "export_today": "sensor.export", "rates_import": [{"rate": 20.0}], "rates_export": [{"rate": 5.0}]}
    base.midnight_utc = _time(0, 0)
    base.now_utc = base.now_utc_real = _time(10, 5)
    base.minutes_now = 605
    base.num_cars = 0
    base.components = None
    base.plan_interval_minutes = 30
    base.rate_history = RateHistory(timezone="UTC")
    base.load_forecast_only = True
    base.minute_data_load = lambda *args, **kwargs: ({0: 1.0, 1: 0.99, 5: 0.95}, 2)
    base.minute_data_import_export = lambda *args, **kwargs: {minute: (605 - minute) * 0.01 for minute in range(606)}
    base.fetch_extra_load_forecast = lambda *args, **kwargs: ({}, [])
    base.load_car_energy = lambda *args: {}
    base.get_history_wrapper = lambda *args, **kwargs: []
    base.fetch_sensor_data_cars = lambda **kwargs: None
    base.dynamic_load_car_check = lambda **kwargs: False
    base.fetch_octopus_sessions = lambda *args: ([], [])
    base.fetch_pv_forecast_and_dawn = lambda: {}
    base.fetch_sensor_data_car_planning = lambda: None
    base.publish_car_plan = lambda: None
    base.log_dispatch_timelines = lambda: None
    base.load_today_comparison = lambda *args, **kwargs: 1.0
    return base


def test_live_fetch_closing():
    """The real fetch parser retains old prices without changing its live tables."""
    from unittest.mock import patch

    base = _live_base()
    base.manual_import_rates = dict.fromkeys(range(600, 630), 6.0)
    with patch("fetch.FutureRate") as future:
        future.return_value.futurerate_analysis.return_value = ({}, {})
        base.fetch_sensor_data()
        assert base.rate_history_import[600] == 6.0
        base.now_utc = base.now_utc_real = _time(10, 30)
        base.minutes_now = 630
        base.manual_import_rates = {}
        base.args["rates_import"] = [{"rate": 40.0}]
        base.fetch_sensor_data()
        assert base.rate_import[600] == 40.0
        assert base.rate_history_import[600] == 6.0
        assert base.rate_history_import[629] == 6.0
        assert base.rate_history_import[630] == 40.0
        before = base.rate_history.snapshot()
        base.fetch_sensor_data(save=False)
        assert base.rate_history.snapshot() == before
        assert not base.rate_history_accounting_enabled


def test_live_api_override_bounds():
    """Current short overrides apply; unobserved future overrides never close."""
    from unittest.mock import patch

    with patch("fetch.FutureRate") as future:
        future.return_value.futurerate_analysis.return_value = ({}, {})
        base = _live_base()
        base.args["rates_import_override"] = [{"start": "10:00", "end": "10:20", "rate": 10.0}]
        base.fetch_sensor_data()
        assert base.rate_import[605] == base.rate_history_import[605] == 10.0
        base.now_utc = base.now_utc_real = _time(10, 35)
        base.minutes_now = 635
        base.args["rates_import_override"] = []
        base.fetch_sensor_data()
        assert base.rate_history_import[605] == 20.0, "Known expiry before closing restores the tariff"
        base = _live_base()
        base.args["rates_import_override"] = [{"start": "10:20", "end": "10:30", "rate": 10.0}]
        base.fetch_sensor_data()
        assert base.rate_import[605] == base.rate_history_import[605] == 20.0
        base.now_utc = base.now_utc_real = _time(10, 35)
        base.minutes_now = 635
        base.args["rates_import_override"] = []
        base.fetch_sensor_data()
        assert base.rate_history_import[625] == 20.0, "Future override was never observed while active"


def test_live_iog_withdrawal():
    """Actual charging keeps price and cap usage after dispatch withdrawal."""
    from unittest.mock import patch

    base = _live_base()
    base.num_cars = 1
    base.args["octopus_intelligent_slot"] = ["binary_sensor.octopus_iog"]
    base.args["octopus_slot_max"] = 1
    base.args["rates_import"] = [{"rate": 20.0}, {"start": "00:00", "end": "05:30", "rate": 6.0}]
    base.dynamic_load_car_sensors = {0: "sensor.car_power"}
    dispatches = [{"start": _time(10, 0).isoformat(), "end": _time(10, 30).isoformat(), "charge_in_kwh": 1.0, "source": "smart-charge", "location": "AT_HOME"}]
    state = {"state": "3200", "last_changed": _time(10, 3).isoformat()}
    original_state = base.get_state_wrapper

    def read_state(entity_id=None, raw=False, **kwargs):
        """Read fresh actual car power, leaving other fixture sensors unchanged."""
        if entity_id == "sensor.car_power":
            return dict(state) if raw else state["state"]
        return original_state(entity_id=entity_id, raw=raw, **kwargs)

    def fetch_cars(**kwargs):
        """Only the current API dispatch list drives the car command list."""
        base.octopus_slots = [list(dispatches)]
        base.car_charging_slots = [[]]

    base.get_state_wrapper = read_state
    base.fetch_sensor_data_cars = fetch_cars
    with patch("fetch.FutureRate") as future:
        future.return_value.futurerate_analysis.return_value = ({}, {})
        base.fetch_sensor_data()
        assert base.rate_import[600] == 6.0
        assert record_iog_confirmation(base, 0, _time(10, 30), _time(10, 5))
        dispatches.clear()
        state.update(state="0", last_changed=_time(10, 15).isoformat())
        base.now_utc = base.now_utc_real = _time(10, 15)
        base.minutes_now = 615
        base.fetch_sensor_data()
        assert base.rate_import[615] == 6.0
        assert base.rate_history_import[600] == 6.0 and base.rate_history_import[629] == 6.0
        assert base.rate_import[630] == 20.0
        assert base.car_charging_slots == [[]]
        dispatches.append({"start": _time(10, 40).isoformat(), "end": _time(11, 0).isoformat(), "charge_in_kwh": 1.0, "source": "smart-charge", "location": "AT_HOME"})
        base.now_utc = base.now_utc_real = _time(10, 35)
        base.minutes_now = 635
        base.fetch_sensor_data()
        assert base.rate_history_import[600] == 6.0
        assert base.rate_import[640] == 20.0, "Withdrawn confirmed dispatch still consumes its cheap-slot allowance"


def run_rate_history_adapter_tests(my_predbat=None):
    """Run adapter regression groups through the repository test harness."""
    failed = False
    for test in (
        test_manual_closing,
        test_exact_events,
        test_future_events_and_periods,
        test_cross_parent_offer_restart,
        test_future_overlap_not_retained,
        test_managed_null_withdrawal,
        test_premium_presence_retained,
        test_provider_bounds_and_clock,
        test_confirmation_evidence,
        test_metered_completed_dispatches,
        test_fetch_readonly,
        test_live_fetch_closing,
        test_live_api_override_bounds,
        test_live_iog_withdrawal,
    ):
        try:
            test()
            print("{} passed".format(test.__name__))
        except Exception as error:
            import traceback

            failed = True
            print("{} failed: {}".format(test.__name__, error))
            traceback.print_exc()
    return failed

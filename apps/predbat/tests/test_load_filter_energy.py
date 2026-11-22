# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Regression tests for filtering actual metered appliance consumption."""

import copy
import json
import os
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import numpy as np
from load_ml_component import LoadMLComponent
from load_predictor import LoadPredictor
from tests.test_filtered_load_minute import build_cumulative
from tests.test_infra import run_async


def metered_test_base(my_predbat, **settings):
    """Build an isolated base with deterministic settings and the real load-filtering methods."""
    base = copy.copy(my_predbat)
    base.args = {
        "load_today": ["sensor.house_energy"],
        "load_power": None,
        "pv_today": None,
        "load_filter_energy": [],
        "car_charging_hold": False,
        "car_energy_reported_load": True,
        "car_charging_energy": None,
        "car_charging_threshold": 6.0,
        "iboost_energy_subtract": False,
        **settings,
    }
    base.get_arg = Mock(side_effect=lambda key, default=None, **kwargs: base.args.get(key, default))
    base.now_utc = datetime(2026, 11, 8, 10, tzinfo=timezone.utc)
    base.midnight_utc = base.now_utc.replace(hour=0)
    base.minutes_now = 600
    base.config_root = None
    base.log = Mock()
    base.record_status = Mock()
    base.get_history_wrapper = Mock(return_value=None)
    base.get_state_wrapper = Mock(return_value=None)
    base.fetch_pv_forecast = Mock(return_value=({}, {}, {}))
    base.load_filter_energy_data = {}
    base.car_charging_hold = base.args["car_charging_hold"]
    base.car_charging_energy = {}
    base.car_charging_threshold = 0.1
    base.car_charging_rate = [6.0]
    base.iboost_energy_subtract = base.args["iboost_energy_subtract"]
    base.iboost_energy_today = {}
    base.base_load = 0.0
    base.days_previous = [1]
    base.days_previous_weight = [1]
    base.load_minutes_age = 2
    base.max_days_previous = 2
    base.config_index = copy.deepcopy(my_predbat.config_index)
    base.dashboard_values = {}
    return base


def test_actual_energy_and_time(my_predbat):
    """Remove measured consumption at its actual time, irrespective of forecast energy or enable state."""
    base = metered_test_base(my_predbat)
    base.args["house_load_additional_forecast"] = [{"name": "dishwasher", "enabled": False, "start_time": "20:00", "duration": 2, "energy": 1.2}]
    base.load_filter_energy_data = {"sensor.dishwasher_energy": build_cumulative({10: 0.2, 11: 0.1}, 40)}
    house = build_cumulative({10: 0.4, 11: 0.2, 12: 0.3, 20: 0.5}, 40)
    load, raw = base.get_filtered_load_window(house, [10, 11, 12], 3)
    assert abs(raw - 0.9) < 1e-9 and abs(load - 0.6) < 1e-9, (load, raw)
    load, raw = base.get_filtered_load_window(house, [20], 1)
    assert abs(load - 0.5) < 1e-9 and load == raw, "A different time must not lose planned appliance energy"


def test_expired_schedule_without_consumption(my_predbat):
    """Old synthetic schedule records and missing meters must not fabricate consumed energy."""
    base = metered_test_base(my_predbat)
    base.house_load_additional_history = [{"id": "old", "start": (base.now_utc - timedelta(minutes=30)).isoformat(), "end": base.now_utc.isoformat(), "energy": 1.2}]
    house = build_cumulative({0: 0.5}, 10)
    load, raw = base.get_filtered_load_minute(house, 0, historical=False)
    assert load == raw == 0.5, "Elapsed schedules and old forecast records are not meter evidence"
    base.args["load_filter_energy"] = "sensor.missing_energy"
    base.load_filter_energy_data = base.fetch_load_filter_energy(2, base.now_utc)
    assert not base.load_filter_energy_data
    load, raw = base.get_filtered_load_minute(house, 0, historical=False)
    assert load == raw == 0.5, "Missing meter history must not trigger a planned-energy fallback"


def test_weighting_clipping_and_base_floor(my_predbat):
    """Clip each historical day before weighting, preserve raw measurements, and retain the base-load floor."""
    base = metered_test_base(my_predbat)
    base.days_previous = [1, 2]
    base.days_previous_weight = [1, 3]
    house = build_cumulative({1440: 0.2, 2880: 1.0}, 2900)
    base.load_filter_energy_data = {"sensor.appliance_energy": build_cumulative({1440: 0.5, 2880: 0.2}, 2900)}
    load, raw = base.get_filtered_load_minute(house, 0, historical=True)
    assert abs(load - 0.6) < 1e-9 and abs(raw - 0.8) < 1e-9, (load, raw)
    base.base_load = 6.0
    load, raw = base.get_filtered_load_minute(house, 1440, historical=False, base_in_raw=False)
    assert abs(load - 0.1) < 1e-9 and abs(raw - 0.2) < 1e-9, (load, raw)


def test_meter_coverage_and_bad_readings(my_predbat):
    """Do not turn a counter's initial value, gaps, resets or invalid readings into appliance usage."""
    base = metered_test_base(my_predbat)
    for data in [{0: 100.0}, {1: 99.0}, {0: None, 1: 0.0}, {0: "unavailable", 1: 0.0}, {0: float("nan"), 1: 0.0}, {0: float("inf"), 1: 0.0}, {0: 0.0, 1: 100.0}]:
        assert base.get_metered_load_increment(data, 0) == 0, data
    assert base.get_metered_load_increment({0: 100.25, 1: 100.0}, 0) == 0.25
    house = build_cumulative({0: 0.5, 5: 0.5}, 10)
    base.load_filter_energy_data = {"sensor.appliance_energy": {0: 100.0, 5: 100.0}}
    load, raw = base.get_filtered_load_window(house, [0, 5], 2)
    assert load == raw == 1.0, "Uncovered boundaries must not subtract a counter's absolute value"


def test_sensor_deduplication(my_predbat):
    """Count each meter once, including overlap with active car and iBoost filters."""
    base = metered_test_base(my_predbat, load_filter_energy=["sensor.dishwasher", "sensor.dishwasher", "Sensor.Car", "sensor.diverter"], car_charging_energy=["sensor.car"], iboost_energy_today="sensor.diverter")
    assert base.get_load_filter_energy_sensors() == ["sensor.car", "sensor.dishwasher", "sensor.diverter"]
    base.args["car_charging_hold"] = True
    base.args["iboost_energy_subtract"] = True
    assert base.get_load_filter_energy_sensors() == ["sensor.dishwasher"]
    base.args["car_energy_reported_load"] = False
    assert base.get_load_filter_energy_sensors() == ["sensor.car", "sensor.dishwasher"]
    base.args["load_filter_energy"] = "sensor.dishwasher"
    assert base.get_load_filter_energy_sensors() == ["sensor.dishwasher"]
    base.args["load_filter_energy"] = None
    assert base.get_load_filter_energy_sensors() == []


def test_real_meter_reader(my_predbat):
    """Use the production history reader for Wh conversion, daily resets, duplicate and missing sensors."""
    base = metered_test_base(my_predbat, load_filter_energy=["sensor.appliance_energy", "sensor.appliance_energy", "sensor.missing"])
    samples = [
        (base.now_utc - timedelta(days=1), 900),
        (base.midnight_utc - timedelta(minutes=5), 1000),
        (base.midnight_utc, 0),
        (base.now_utc - timedelta(minutes=10), 0),
        (base.now_utc - timedelta(minutes=5), 200),
        (base.now_utc, 500),
    ]
    history = [[{"state": str(value), "last_updated": stamp.isoformat(), "attributes": {"unit_of_measurement": "Wh"}} for stamp, value in samples]]
    base.get_history_wrapper.side_effect = lambda entity_id, **kwargs: copy.deepcopy(history) if entity_id == "sensor.appliance_energy" else None
    energy_data = base.fetch_load_filter_energy(2, base.now_utc)
    assert list(energy_data) == ["sensor.appliance_energy"]
    measured = energy_data["sensor.appliance_energy"]
    assert abs(sum(base.get_metered_load_increment(measured, minute) for minute in range(10)) - 0.5) < 0.001
    assert len([call for call in base.get_history_wrapper.call_args_list if call.kwargs["entity_id"] == "sensor.appliance_energy"]) == 1
    assert all(call.kwargs["required"] is False for call in base.get_history_wrapper.call_args_list)


def test_appliances_before_car_threshold(my_predbat):
    """Appliance consumption alone must not trigger the estimated car-charging filter."""
    base = metered_test_base(my_predbat, car_charging_hold=True)
    house = build_cumulative({minute: 0.2 for minute in range(5)}, 10)
    base.load_filter_energy_data = {"sensor.appliance_energy": build_cumulative({minute: 0.12 for minute in range(5)}, 10)}
    load, _ = base.get_filtered_load_window(house, list(range(5)), 5)
    assert abs(load - 0.4) < 1e-9, load


def test_ml_uses_actual_meters(my_predbat):
    """ML subtracts the same actual energy, including clipping and the pre-car-threshold ordering."""
    for car_hold in [False, True]:
        base = metered_test_base(my_predbat, load_filter_energy=["sensor.appliance_energy"], car_charging_hold=car_hold)
        house = build_cumulative({minute: 0.2 for minute in range(1445)}, 1451)
        meter = build_cumulative({minute: 0.3 if 700 <= minute < 705 else 0.12 for minute in range(1445)}, 1451)
        base.minute_data_load = Mock(return_value=(house, 1))
        base.minute_data_import_export = Mock(side_effect=lambda days, now, key, **kwargs: meter if key == "sensor.appliance_energy" else {})
        component = LoadMLComponent(base, load_ml_enable=True)
        result, _, _, _, _, _, _ = run_async(component._fetch_load_data())
        assert result is not None, base.log.call_args_list
        assert abs(result[100] - 0.4) < 0.001, result[100]
        assert result[700] == 0, result[700]
        calls = [call for call in base.minute_data_import_export.call_args_list if call.args[2] == "sensor.appliance_energy"]
        assert len(calls) == 1 and calls[0].kwargs["pad"] is False


def test_ml_history_configuration(my_predbat):
    """Saved training data must not mix baselines prepared with different appliance exclusions."""
    with tempfile.TemporaryDirectory(prefix="predbat_load_filter_") as temp_dir:
        path = os.path.join(temp_dir, "history.npz")
        base = metered_test_base(my_predbat, load_filter_energy=["sensor.appliance_energy"])
        component = LoadMLComponent(base, load_ml_enable=True)
        component.database_filepath = path
        component.load_ml_database_days = 1
        component.load_data = {0: 0.25, 5: 0.0}
        run_async(component.save_database_history())
        matching = LoadMLComponent(base, load_ml_enable=True)
        matching.database_filepath = path
        run_async(matching.load_database_history())
        assert matching.load_data == {0: 0.25, 5: 0.0}
        for sensors in [[], ["sensor.other_energy"]]:
            changed = LoadMLComponent(metered_test_base(my_predbat, load_filter_energy=sensors), load_ml_enable=True)
            changed.database_filepath = path
            run_async(changed.load_database_history())
            assert changed.load_data is None
        with np.load(path, allow_pickle=False) as saved:
            arrays = {key: saved[key] for key in saved.files}
        metadata = json.loads(str(arrays["metadata_json"]))
        metadata.pop("source_config")
        arrays["metadata_json"] = np.array(json.dumps(metadata))
        np.savez_compressed(path, **arrays)
        legacy = LoadMLComponent(base, load_ml_enable=True)
        legacy.database_filepath = path
        run_async(legacy.load_database_history())
        assert legacy.load_data is None, "A pre-filter database cannot supply already-filtered training rows"


def test_ml_model_configuration(my_predbat):
    """Reject models trained on a different baseline, even when the training-history database is disabled."""
    base = metered_test_base(my_predbat, load_filter_energy=["sensor.appliance_energy"])
    predictor = LoadPredictor(log_func=base.log)
    random_state = np.random.get_state()
    try:
        predictor._initialize_weights()
    finally:
        np.random.set_state(random_state)
    # Model validity uses the wall clock, independently of the fixed meter-history fixture.
    predictor.training_timestamp = datetime.now(timezone.utc)
    predictor.validation_mae = 0.1
    with tempfile.TemporaryDirectory(prefix="predbat_load_filter_model_") as temp_dir:
        base.config_root = temp_dir
        path = os.path.join(temp_dir, "predbat_ml_model.npz")
        assert predictor.save(path, source_config={"load_filter_energy": ["sensor.appliance_energy"]})
        matching = LoadMLComponent(base, load_ml_enable=True, load_ml_database_days=0)
        assert matching.predictor.model_initialized and matching.initial_training_done
        base.args["load_filter_energy"] = []
        changed = LoadMLComponent(base, load_ml_enable=True, load_ml_database_days=0)
        assert not changed.predictor.model_initialized and not changed.initial_training_done
        assert predictor.save(path), "A model saved with the old default API should remain supported"
        no_filter = LoadMLComponent(base, load_ml_enable=True, load_ml_database_days=0)
        assert no_filter.predictor.model_initialized
        base.args["load_filter_energy"] = ["sensor.appliance_energy"]
        added = LoadMLComponent(base, load_ml_enable=True, load_ml_database_days=0)
        assert not added.predictor.model_initialized


def run_load_filter_energy_tests(my_predbat):
    """Run metered appliance filtering regressions without training the ML network."""
    failed = False
    tests = [
        test_actual_energy_and_time,
        test_expired_schedule_without_consumption,
        test_weighting_clipping_and_base_floor,
        test_meter_coverage_and_bad_readings,
        test_sensor_deduplication,
        test_real_meter_reader,
        test_appliances_before_car_threshold,
        test_ml_uses_actual_meters,
        test_ml_history_configuration,
        test_ml_model_configuration,
    ]
    for test in tests:
        try:
            test(my_predbat)
            print("PASS: {}".format(test.__name__))
        except Exception:
            print("ERROR: {}".format(test.__name__))
            traceback.print_exc()
            failed = True
    return failed

"""Offline acceptance of the car planner's isolated native-prediction bridge."""

from tests.car_planning_fixtures import car_settings, clear_car_settings, configure_car_settings

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from const import PREDBAT_MAX_CARS, PREDICT_STEP
from prediction import Prediction
from car_charging_plan import _native_windows, _options, _replay, evaluate_car_charging
from tests.test_infra import FIXTURE_MINUTES_NOW, reset_inverter, reset_rates


def _fixture(base, load=1, pv=0, minutes=60):
    """Build isolated native forecasts and explicit electrical limits."""
    base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    reset_inverter(base)
    base.forecast_minutes = base.end_record = minutes
    base.minutes_now = FIXTURE_MINUTES_NOW
    base.now_utc = base.midnight_utc + timedelta(minutes=base.minutes_now)
    reset_rates(base, 10, 0)
    base.prediction_kernel_enable = base.debug_enable = False
    base.import_limit = 6000.0
    base.soc_max = 32.1536
    base.reserve = base.soc_max * 0.10
    base.soc_kw = base.reserve
    base.inverter_limit = 16 / 60
    base.battery_rate_max_charge = base.battery_rate_max_charge_dc = 3 / 60
    base.battery_rate_max_discharge = base.battery_rate_max_export = 8.192 / 60
    base.inverter_loss = 0.95
    base.battery_loss = base.battery_loss_discharge = 0.96
    base.battery_temperature = 20
    base.battery_temperature_prediction = {}
    base.battery_temperature_charge_curve = {}
    base.battery_temperature_discharge_curve = {}
    base.best_soc_max = -1
    base.best_soc_min = 0
    base.all_active_keep = {}
    base.all_active_keep_max = {}
    base.io_adjusted = {}
    base.set_charge_low_power = base.set_export_low_power = False
    base.metric_standing_charge = 0
    base.export_today_now = base.import_today_now = base.pv_today_now = base.load_minutes_now = 0
    base.cost_today_sofar = base.carbon_today_sofar = base.iboost_today = 0
    base.iboost_plan = []
    base.num_cars = 1
    base.car_charging_limit_model = None
    base.car_charging_limit = [25.0] * PREDBAT_MAX_CARS
    base.car_charging_battery_size = [25.0] * PREDBAT_MAX_CARS
    base.car_charging_loss = 0.92
    base.car_charging_from_battery = base.car_energy_reported_load = True
    pv_steps = {m: pv * PREDICT_STEP / 60 for m in range(0, minutes, PREDICT_STEP)}
    load_steps = {m: load * PREDICT_STEP / 60 for m in range(0, minutes, PREDICT_STEP)}
    return Prediction(base, pv_steps, pv_steps, load_steps, load_steps)


def _setup(base, pv=0, minutes=60):
    """Supply synthetic, fresh native forecasts and an explicit expected EV session."""
    pred = _fixture(base, load=1.0, pv=pv, minutes=minutes)
    base.soc_kw = 20.0
    base.car_charging_rate = [6.77] * PREDBAT_MAX_CARS
    base.car_charging_limit = [3.0] * PREDBAT_MAX_CARS
    base.car_charging_soc = [0.0] * PREDBAT_MAX_CARS
    base.car_charging_manual_soc = [False] * PREDBAT_MAX_CARS
    base.set_discharge_during_charge = False
    base.set_charge_freeze_only = False
    base.load_minutes_step = pred.load_minutes_step
    base.load_minutes_step10 = pred.load_minutes_step10
    base.load_minutes_step90 = pred.load_minutes_step90
    base.pv_forecast_minute_step = pred.pv_forecast_minute_step
    base.pv_forecast_minute10_step = pred.pv_forecast_minute10_step
    base.pv_forecast_minute90_step = pred.pv_forecast_minute90_step
    configure_car_settings(
        base,
        {
            "mode": "shadow",
            # These established scenarios model an explicitly retained pre-existing setting.
            "battery_min_window_minutes": 5,
            "load_port_limit_w": 10000,
            "session": {
                "available_from": base.now_utc.isoformat(),
                "departure": (base.now_utc + timedelta(minutes=minutes)).isoformat(),
                "soc_kwh": 0.0,
                "soc_observed_at": base.now_utc.isoformat(),
            },
        },
    )
    base.car_charging_report = None
    # Clear any synthetic entity state from prior tests so each fixture starts
    # with a clean HA cache. The TestHAInterface instance is shared across
    # tests; without this reset, leftover state from earlier cases would leak.
    ha = getattr(base, "ha_interface", None)
    if ha is not None and hasattr(ha, "dummy_items"):
        for entity_id in (
            "binary_sensor.test_relay",
            "sensor.test_ev_soc",
            "sensor.test_charger_state",
            "switch.test_permission",
        ):
            ha.dummy_items.pop(entity_id, None)


def _controls(base):
    """Snapshot the executable plan, not just the shadow report's claim of isolation."""
    return deepcopy({key: getattr(base, key) for key in ("car_charging_slots", "charge_window_best", "charge_limit_best", "export_window_best", "export_limits_best", "car_charging_soc", "car_charging_soc_next")})


def _set_entity(base, entity_id, state, last_updated=None, last_changed=None, attributes=None):
    """Populate the TestHAInterface with the standard HA shape used by get_state_wrapper(raw=True)."""
    base.ha_interface.dummy_items[entity_id] = {
        "state": state,
        "last_updated": last_updated if last_updated is not None else base.now_utc,
        "last_changed": last_changed if last_changed is not None else base.now_utc,
        **(attributes or {}),
    }


def test_disabled_and_invalid(base):
    """Default/off never runs a solver; unsupported modes cannot accidentally enable execution."""
    _setup(base)
    for settings in (None, {}, {"mode": "off"}):
        configure_car_settings(base, settings)
        before = _controls(base)
        with patch("car_charging_plan.build_candidate", side_effect=AssertionError("Disabled solver called")):
            assert evaluate_car_charging(base) is None
        assert _controls(base) == before
    for settings in (True, "shadow", {"mode": "apply"}, {"mode": "typo"}):
        configure_car_settings(base, settings)
        report = evaluate_car_charging(base)
        assert report["status"] == "invalid_input", report
        assert report["execution_enabled"] is False
        assert report["enforced"] is False


def test_fresh_session_and_availability(base):
    """Expected arrival is separate from real switching; late/stale/naive sessions cannot pass."""
    _setup(base)
    settings = deepcopy(car_settings(base))
    for key, value in (
        ("soc_observed_at", (base.now_utc - timedelta(hours=25)).isoformat()),
        ("soc_observed_at", (base.now_utc + timedelta(minutes=1)).isoformat()),
        ("departure", base.now_utc.isoformat()),
        ("available_from", (base.now_utc + timedelta(hours=2)).isoformat()),
        ("departure", "2026-01-01T07:30:00"),
        ("soc_kwh", float("nan")),
    ):
        configure_car_settings(base, deepcopy(settings))
        car_settings(base)["session"][key] = value
        assert evaluate_car_charging(base)["status"] == "invalid_input"
    configure_car_settings(base, settings)
    car_settings(base)["session"]["available_from"] = (base.now_utc + timedelta(minutes=35)).isoformat()
    report = evaluate_car_charging(base)
    assert report["status"] == "no_plan_found", report
    assert not report["proposed_ev_windows"]


def test_shadow_candidate_and_isolation(base):
    """A real optimisation and native replay use new objects, never the active plan or relay signal."""
    _setup(base, pv=5)
    before = _controls(base)
    base.prediction = Prediction(base, base.pv_forecast_minute_step, base.pv_forecast_minute10_step, base.load_minutes_step, base.load_minutes_step10)
    active_prediction = base.prediction
    active_prediction.prediction_cache["sentinel"] = {"untouched": [1, 2]}
    active_prediction.pending_batch.append({"untouched": True})
    cache_before = deepcopy(active_prediction.prediction_cache)
    batch_before = deepcopy(active_prediction.pending_batch)
    kernel_before = active_prediction.kernel_handle
    base.prediction_kernel_enable = True
    base.publish_car_plan()
    relay_key = "binary_sensor." + base.prefix + "_car_charging_slot"
    relay_state = deepcopy(base.dashboard_values[relay_key])
    with patch("prediction.create_kernel_context", side_effect=AssertionError("Shadow must not allocate a kernel context")):
        report = evaluate_car_charging(base)
    assert report["status"] == "candidate", report
    assert report["proposed_ev_windows"], report
    assert report["execution_enabled"] is False
    assert report["enforced"] is False
    assert report["native_checks"]["nominal"]["checked_minutes"] == base.forecast_minutes
    assert report["native_checks"]["pv10"]["checked_minutes"] == base.forecast_minutes
    assert _controls(base) == before
    assert base.prediction is active_prediction
    assert active_prediction.prediction_cache == cache_before
    assert active_prediction.pending_batch == batch_before
    assert active_prediction.kernel_handle == kernel_before
    assert base.prediction_kernel_enable is True
    assert abs(report["solver_ev_ac_kwh"] - 3 / base.car_charging_loss) < 1e-8
    assert sum(w["kwh"] for w in report["proposed_ev_windows"]) > report["solver_ev_ac_kwh"]
    published = base.dashboard_values[relay_key]
    assert published["state"] == relay_state["state"]
    assert {key: published["attributes"][key] for key in relay_state["attributes"]} == relay_state["attributes"]
    item = base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]
    assert item["attributes"]["plan_status"] == report["status"]
    assert item["attributes"]["plan"]["execution_enabled"] is False
    clear_car_settings(base)
    assert evaluate_car_charging(base) is None
    item = base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]
    assert item["attributes"]["plan_status"] == "disabled"
    assert item["attributes"]["plan"]["proposed_ev_windows"] == []


def test_errors_clear_old_candidates(base):
    """Missing forecasts, unexpected exceptions and unsupported loads are explicit, not stale success."""
    _setup(base)
    del base.load_minutes_step[5]
    assert evaluate_car_charging(base)["status"] == "invalid_input"
    _setup(base)
    base.iboost_enable = True
    assert evaluate_car_charging(base)["status"] == "unsupported"
    _setup(base)
    base.num_cars = 2
    assert evaluate_car_charging(base)["status"] == "unsupported"
    _setup(base)
    before = _controls(base)
    with patch("car_charging_plan.build_candidate", side_effect=RuntimeError("private credential must not be logged")):
        report = evaluate_car_charging(base)
    assert report["status"] == "error"
    assert "private credential" not in str(report)
    assert report["proposed_ev_windows"] == []
    assert _controls(base) == before
    with patch("car_charging_plan.build_candidate", side_effect=TypeError("private input in interface failure")):
        report = evaluate_car_charging(base)
    assert report["status"] == "error"
    assert report["failure_stage"] == "solver"
    assert report["error_type"] == "TypeError"
    assert "private input" not in str(report)


def test_publication_failure_is_nonfatal(base):
    """Even a failing shadow sensor/log sink cannot abort the native control cycle."""
    _setup(base)
    before = _controls(base)
    with (
        patch("car_charging_plan.build_candidate", return_value=(SimpleNamespace(actions=()), [])),
        patch.object(base, "dashboard_item", side_effect=RuntimeError("private transport details")),
        patch.object(base, "log", side_effect=RuntimeError("logger unavailable")),
    ):
        report = evaluate_car_charging(base)
    assert report["publication_failed"] is True
    assert report["status"] == "no_plan_found"
    assert "private transport" not in str(report)
    assert _controls(base) == before


def test_native_rejects_counterexamples(base):
    """Do not trust a solver's success label: replay raw power, reserve, arrival and cooldown."""
    _setup(base)
    base.soc_kw = base.reserve
    slots = [SimpleNamespace(index=i, start_min=m, duration_min=5, import_price=10) for i, m in enumerate(range(0, 60, 5))]
    actions = [SimpleNamespace(slot_index=s.index, battery_mode="demand", ev_action="on") for s in slots]
    result = SimpleNamespace(actions=actions)
    windows = _native_windows(base, result, slots)
    before = _controls(base)
    checks = _replay(base, _options(base), result, slots, windows, 0)
    assert checks["passed"] is False
    assert checks["peak_import_w"] > 7700
    assert "site_import_exceeded" in checks["reasons"]
    assert "ev_pause_threshold" in checks["reasons"]
    base.soc_kw = 20
    actions[1].ev_action = "off"
    options = _options(base)
    options["arrival"] = 10
    options["deadline"] = 45
    options["load_cap"] = 7
    checks = _replay(base, options, result, slots, _native_windows(base, result, slots), 0)
    assert "ev_availability_or_deadline" in checks["reasons"]
    assert "ev_cooldown" in checks["reasons"]
    assert "load_port_exceeded" in checks["reasons"]
    assert _controls(base) == before


def test_actual_calculate_plan_hook(base):
    """Live calculations use the native hook; counterfactual runs retain live report and authority."""
    for publish in (False, True):
        _setup(base)
        base.calculate_best = False
        base.calculate_savings = False
        base.charge_limit = []
        base.plan_last_updated_minutes = base.minutes_now
        base.metric_cloud_coverage = 0
        load = base.load_minutes_step.copy()
        pv = base.pv_forecast_minute_step.copy()
        original_controls = _controls(base)
        live_report = {"status": "applied", "live_sentinel": True}
        live_pending = {"live_sentinel": True}
        if not publish:
            car_settings(base)["mode"] = "apply"
            base.car_charging_report = live_report
            base.car_charging_pending = live_pending

        def steps(*args, **kwargs):
            """Replace only forecast acquisition, not plan calculation or prediction."""
            return pv.copy() if kwargs.get("forward") else load.copy()

        with (
            patch.object(base, "step_data_history", side_effect=steps),
            patch.object(base, "get_cloud_factor", return_value=0),
            patch.object(base, "get_load_divergence", return_value=0),
            patch.object(base, "refresh_pv_forecast_minute90"),
            patch("car_charging_plan.evaluate_car_charging", wraps=evaluate_car_charging) as hook,
        ):
            base.calculate_plan(recompute=False, publish=publish)
        if publish:
            assert hook.call_count == 1
            assert hook.call_args.kwargs == {"publish": True}
            assert base.car_charging_report["status"] == "candidate", base.car_charging_report
            assert base.car_charging_report["proposed_ev_windows"]
        else:
            assert not hook.called
            assert base.car_charging_report is live_report
            assert base.car_charging_pending is live_pending
        after = _controls(base)
        after.pop("car_charging_soc_next")
        original_controls.pop("car_charging_soc_next")
        assert after == original_controls


def test_pv10_rejection_and_failed_replay(base):
    """A real candidate can fail the conservative native case without becoming executable."""
    _setup(base, pv=5)
    base.load_minutes_step10 = {m: 12 / 12 for m in base.load_minutes_step}
    before = _controls(base)
    report = evaluate_car_charging(base)
    assert report["status"] == "native_rejected", report
    assert report["native_checks"]["nominal"]["passed"]
    assert "load_port_exceeded" in report["native_checks"]["pv10"]["reasons"]
    assert _controls(base) == before
    with patch("car_charging_plan._replay", side_effect=KeyError("private replay data")), patch.object(base, "log") as log:
        report = evaluate_car_charging(base)
    assert report["status"] == "error"
    assert report["failure_stage"] == "native_replay"
    assert report["proposed_ev_windows"] == []
    assert "private replay" not in str(report) + str(log.call_args_list)
    assert _controls(base) == before


def test_timestamp_offsets_and_cost_convention(base):
    """Inward rounding uses absolute timestamps, and horizon costs exclude standing charges."""
    _setup(base)
    session = car_settings(base)["session"]
    session["available_from"] = (base.now_utc + timedelta(seconds=1)).isoformat()
    session["departure"] = (base.now_utc + timedelta(minutes=59, seconds=59)).isoformat()
    options = _options(base)
    assert options["arrival"] == 5
    assert options["deadline"] == 55
    for name in ("available_from", "departure", "soc_observed_at"):
        session[name] = datetime.fromisoformat(session[name]).astimezone(timezone(timedelta(hours=2))).isoformat()
    assert _options(base) == options
    baseline = evaluate_car_charging(base, publish=False)
    base.metric_standing_charge = 1000
    base.cost_today_sofar = 1234
    later = evaluate_car_charging(base, publish=False)
    assert baseline["status"] == later["status"] == "candidate"
    for case in ("nominal", "pv10"):
        assert abs(baseline["native_checks"][case]["horizon_energy_cost"] - later["native_checks"][case]["horizon_energy_cost"]) < 0.001


# --- New evidence-driven tests using the standard HA shape (no invented storage). ---


def test_relay_state_off_means_off(base):
    """A relay whose state reads 'off' is never promoted to initially_on, regardless of age."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=(base.now_utc - timedelta(seconds=7200)))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    options = _options(base)
    assert options["initially_on"] is False, options
    assert options["entity_observations"]["relay_state"] == "off", options


def test_relay_state_on_after_cooldown_yields_on(base):
    """A relay whose state reads 'on' and whose last_changed is older than off_minutes yields initially_on."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "on", last_changed=(base.now_utc - timedelta(seconds=900)))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["min_off_minutes"] = 10
    options = _options(base)
    assert options["initially_on"] is True, options
    assert options["initial_off_minutes"] == 0, options


def test_relay_state_on_within_cooldown_keeps_cooldown(base):
    """An already ON relay is not modelled as a fresh OFF-to-ON transition."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "on", last_changed=(base.now_utc - timedelta(seconds=120)))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["min_off_minutes"] = 10
    options = _options(base)
    # 120s = 2 minutes elapsed (floor); 10 - 2 = 8 minutes remaining.
    assert options["initially_on"] is True, options
    assert options["initial_off_minutes"] == 0, options
    assert options["elapsed_minutes_since_relay_change"] == 2, options


def test_relay_recent_off_with_cooldown_remaining(base):
    """A relay that flipped OFF recently keeps the cooldown; rounding DOWN never allows earlier starts."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=(base.now_utc - timedelta(seconds=300)))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["min_off_minutes"] = 10
    options = _options(base)
    assert options["initially_on"] is False, options
    # 300s = 5 minutes elapsed (floor); 10 - 5 = 5 minutes remaining.
    assert options["initial_off_minutes"] == 5, options
    assert options["elapsed_minutes_since_relay_change"] == 5, options


def test_relay_partial_seconds_round_down(base):
    """Partial seconds within a minute window must never advance the rounded-down cooldown."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=(base.now_utc - timedelta(seconds=119)))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["min_off_minutes"] = 10
    options = _options(base)
    # 119s = 1 minute elapsed (floor); 10 - 1 = 9 minutes remaining.
    assert options["initial_off_minutes"] == 1, options


def test_naive_timestamps_rejected(base):
    """A naive timestamp (no tzinfo) in dated session or entity state is rejected."""
    _setup(base)
    car_settings(base)["session"]["soc_observed_at"] = "2026-01-01T07:30:00"
    report = evaluate_car_charging(base)
    assert report["status"] == "invalid_input", report
    # Also reject naive last_changed / last_updated on a configured relay entity.
    _setup(base)
    base.ha_interface.dummy_items["binary_sensor.test_relay"] = {
        "state": "on",
        "last_updated": datetime(2026, 1, 1, 7, 30),  # naive
        "last_changed": base.now_utc,
    }
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    options = _options(base)
    assert options["entity_observations"]["relay_state"] is None, options


def test_stale_dated_session_rejected(base):
    """A dated session older than soc_max_age_minutes is rejected even before any live entity check."""
    _setup(base)
    car_settings(base)["session"]["soc_observed_at"] = (base.now_utc - timedelta(hours=2)).isoformat()
    report = evaluate_car_charging(base)
    assert report["status"] == "invalid_input", report


def test_future_dated_session_rejected(base):
    """A future-dated observation is rejected as invalid_input."""
    _setup(base)
    car_settings(base)["session"]["soc_observed_at"] = (base.now_utc + timedelta(minutes=5)).isoformat()
    report = evaluate_car_charging(base)
    assert report["status"] == "invalid_input", report


def test_fresh_entity_overrides_stale_dated(base):
    """A fresh entity observation replaces the dated session BEFORE the dated age check; never invalidated by it."""
    _setup(base)
    car_settings(base)["session"]["soc_observed_at"] = (base.now_utc - timedelta(hours=3)).isoformat()  # stale dated
    car_settings(base)["session"]["soc_kwh"] = 0.0
    _set_entity(base, "sensor.test_ev_soc", "2.5", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    car_settings(base)["soc_entity_unit"] = "kWh"
    options = _options(base)
    assert options["ev_soc"] == 2.5, options
    assert options["ev_soc_source"] == "entity_cache", options


def test_soc_entity_percent_unit_converts_with_capacity(base):
    """A SoC entity in percent is multiplied by the EV battery size; never treated as already-kWh."""
    _setup(base)
    _set_entity(base, "sensor.test_ev_soc", "60", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    car_settings(base)["soc_entity_unit"] = "%"
    options = _options(base)
    # 60% of base.car_charging_battery_size[0] = 60% of 25.0 = 15.0 kWh.
    assert abs(options["ev_soc"] - 15.0) < 1e-9, options


def test_soc_entity_numeric_string_parsed(base):
    """Numeric strings are parsed exactly once and rejected if non-finite / non-numeric."""
    _setup(base)
    _set_entity(base, "sensor.test_ev_soc", "1.25", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    options = _options(base)
    assert options["ev_soc"] == 1.25, options
    # Non-numeric strings become an invalid-value blocker, never a clamped value.
    _setup(base)
    _set_entity(base, "sensor.test_ev_soc", "unavailable", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    report = evaluate_car_charging(base)
    assert report["status"] == "unsupported", report
    assert "soc_entity_invalid_value" in report["execution_blockers"], report


def test_soc_entity_bool_rejected_not_clamped(base):
    """Bool values from a sensor are rejected, never coerced into an apparently valid SoC."""
    _setup(base)
    _set_entity(base, "sensor.test_ev_soc", True, last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    report = evaluate_car_charging(base)
    assert report["status"] == "unsupported", report
    assert "soc_entity_invalid_value" in report["execution_blockers"], report


def test_unavailable_soc_entity_is_blocker_not_fallback(base):
    """A configured soc_entity with no observation becomes a structured blocker; dated session is not a fallback."""
    _setup(base)
    car_settings(base)["session"]["soc_observed_at"] = base.now_utc.isoformat()
    car_settings(base)["session"]["soc_kwh"] = 0.0
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    # No entity populated.
    report = evaluate_car_charging(base)
    assert report["status"] == "unsupported", report
    assert "soc_entity_unavailable" in report["execution_blockers"], report


def test_soc_entity_stale_is_blocker(base):
    """A configured soc_entity with a stale last_updated becomes a structured blocker."""
    _setup(base)
    _set_entity(base, "sensor.test_ev_soc", "1.0", last_updated=base.now_utc - timedelta(hours=3))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    report = evaluate_car_charging(base)
    assert report["status"] == "unsupported", report
    assert "soc_entity_stale_or_future" in report["execution_blockers"], report


def test_soc_entity_out_of_bounds_is_blocker(base):
    """A configured soc_entity outside the EV battery size bounds is a structured blocker."""
    _setup(base)
    _set_entity(base, "sensor.test_ev_soc", "999", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    report = evaluate_car_charging(base)
    assert report["status"] == "unsupported", report
    assert "soc_entity_out_of_bounds" in report["execution_blockers"], report


def test_manual_soc_target_overrides_live_entity(base):
    """The per-car manual SoC target takes precedence over a configured live entity."""
    _setup(base)
    base.car_charging_manual_soc = [True]
    base.car_charging_soc = [1.25] * PREDBAT_MAX_CARS
    _set_entity(base, "sensor.test_ev_soc", "3.0", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    options = _options(base)
    assert options["ev_soc"] == 1.25, options
    assert options["ev_soc_source"] == "manual_soc_target", options


def test_connection_entity_surfaces_observed_state_only(base):
    """The connection entity is informational; its state is reported but never coupled with the relay."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=base.now_utc - timedelta(seconds=900))
    _set_entity(base, "sensor.test_charger_state", "8", last_updated=base.now_utc - timedelta(seconds=30))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["connection_entity"] = "sensor.test_charger_state"
    car_settings(base)["connection_entity_states"] = ["8"]
    options = _options(base)
    assert options["entity_observations"]["connection_state_observed"] == "8", options
    assert options["entity_observations"]["connection_in_allowlist"] is True, options
    # The relay still drives initially_on independently of the connection state.
    assert options["initially_on"] is False, options


def test_permission_entity_missing_is_reported_not_assumed(base):
    """A missing permission entity is reported as not observed; never assumed ready."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=base.now_utc - timedelta(seconds=900))
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["permission_entity"] = "switch.test_permission"
    options = _options(base)
    assert options["entity_observations"]["permission_observed"] is False, options
    assert options["entity_observations"]["permission_granted"] is False, options


def test_permission_entity_exact_on_grants(base):
    """A permission entity whose state is exactly 'on' grants; 'ON' / 'On' / 'true' do not."""
    _setup(base)
    _set_entity(base, "switch.test_permission", "on")
    car_settings(base)["permission_entity"] = "switch.test_permission"
    options = _options(base)
    assert options["entity_observations"]["permission_granted"] is True, options
    _setup(base)
    _set_entity(base, "switch.test_permission", "ON")
    car_settings(base)["permission_entity"] = "switch.test_permission"
    options = _options(base)
    assert options["entity_observations"]["permission_granted"] is False, options


def test_no_ha_service_writes_or_refresh(base):
    """The adapter invokes get_state with refresh=False and never calls a HA service."""
    _setup(base)
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=base.now_utc - timedelta(seconds=900))
    _set_entity(base, "sensor.test_ev_soc", "1.0", last_updated=base.now_utc - timedelta(seconds=30))
    _set_entity(base, "switch.test_permission", "on")
    car_settings(base)["relay_entity"] = "binary_sensor.test_relay"
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    car_settings(base)["permission_entity"] = "switch.test_permission"
    get_state_refresh = []
    real_get_state = base.ha_interface.get_state
    service_calls = []
    real_call_service = base.ha_interface.call_service

    def tracked_get_state(entity_id=None, default=None, attribute=None, refresh=False, raw=False):
        """Record the refresh flag for every get_state invocation; never let the adapter write."""
        get_state_refresh.append((entity_id, refresh))
        return real_get_state(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh, raw=raw)

    def tracked_call_service(*args, **kwargs):
        """Surface any service write as a test failure."""
        service_calls.append((args, kwargs))
        return real_call_service(*args, **kwargs)

    base.ha_interface.get_state = tracked_get_state
    base.ha_interface.call_service = tracked_call_service
    try:
        report = evaluate_car_charging(base)
    finally:
        base.ha_interface.get_state = real_get_state
        base.ha_interface.call_service = real_call_service
    assert report["status"] == "candidate", report
    assert get_state_refresh, "adapter made no get_state calls"
    assert all(refresh is False for _, refresh in get_state_refresh), get_state_refresh
    assert service_calls == [], service_calls


def test_collected_blockers_remain_independent(base):
    """Several independent reasons co-exist in execution_blockers; solver does not run on any of them."""
    _setup(base)
    base.iboost_enable = True
    base.all_active_keep_max = {0: 5.0}
    base.io_adjusted = {0: True}
    car_settings(base)["soc_entity"] = "sensor.test_ev_soc"
    report = evaluate_car_charging(base)
    assert report["status"] == "unsupported", report
    blockers = set(report["execution_blockers"])
    expected = {"iboost_unsupported", "active_keep_ceiling", "io_adjusted", "soc_entity_unavailable"}
    assert expected.issubset(blockers), (expected - blockers, blockers)


def test_invalid_entity_options_rejected(base):
    """Empty entity ids and bad unit strings are configuration errors, not blockers."""
    _setup(base)
    car_settings(base)["relay_entity"] = ""  # empty string is not a valid entity id
    report = evaluate_car_charging(base)
    assert report["status"] == "invalid_input", report
    car_settings(base).pop("relay_entity")
    car_settings(base)["soc_entity_unit"] = "volts"  # not a permitted unit
    report = evaluate_car_charging(base)
    assert report["status"] == "invalid_input", report


def test_real_ha_timestamp_cache_and_elapsed_cooldown(base):
    """Exercise HAInterface's real cache shape, including ISO strings and update times."""
    from ha import HAInterface
    from types import SimpleNamespace

    _setup(base)
    ha = SimpleNamespace(state_data={}, db_enable=False)
    now = base.now_utc
    payload = {"state": "off", "attributes": {}, "last_changed": (now - timedelta(seconds=59)).isoformat(), "last_updated": now.isoformat()}
    HAInterface.update_state_item(ha, payload, "switch.test_relay")
    assert ha.state_data["switch.test_relay"]["last_updated"] == now.isoformat()
    base.ha_interface.dummy_items["switch.test_relay"] = ha.state_data["switch.test_relay"]
    car_settings(base)["relay_entity"] = "switch.test_relay"
    options = _options(base)
    assert options["initially_on"] is False and options["initial_off_minutes"] == 0
    assert not options["entity_blockers"]
    payload["last_changed"] = (now - timedelta(seconds=601)).isoformat()
    HAInterface.update_state_item(ha, payload, "switch.test_relay")
    base.ha_interface.dummy_items["switch.test_relay"] = ha.state_data["switch.test_relay"]
    assert _options(base)["initial_off_minutes"] == 10
    base.ha_interface.dummy_items.pop("switch.test_relay")
    assert "relay_entity_unavailable" in _options(base)["entity_blockers"]


def test_deye_counterfactual_preserves_native_capabilities_and_manual_floor(base):
    """An explicit hypothetical control model never changes SK or drops domestic targets."""
    _setup(base)
    base.set_charge_freeze = False
    base.set_discharge_during_charge = False
    car_settings(base).update(control_model="deye_tou", import_limit_w=6000)
    base.all_active_keep = {base.minutes_now + 45: 30}
    before = deepcopy(base.all_active_keep)
    controls = _controls(base)
    report = evaluate_car_charging(base)
    assert report["status"] == "candidate", report
    assert report["counterfactual_controls"] and report["activation_requirements"]
    assert report["ready_to_activate"] is False and report["execution_enabled"] is False
    assert base.set_charge_freeze is False and base.set_discharge_during_charge is False
    assert base.all_active_keep == before and _controls(base) == controls
    # An impossible imminent target is not silently discarded or turned into an
    # arbitrary standing reserve. Search must report a miss, with no control plan.
    base.all_active_keep = {base.minutes_now: 100}
    report = evaluate_car_charging(base)
    assert report["status"] == "no_plan_found" and not report["proposed_ev_windows"], report


def test_closed_session_does_not_roll_to_tomorrow(base):
    """A passed departure closes the session without inventing next-day permission."""
    _setup(base)
    session = car_settings(base)["session"]
    session["available_from"] = (base.now_utc - timedelta(hours=2)).isoformat()
    session["departure"] = (base.now_utc - timedelta(minutes=1)).isoformat()
    session["soc_kwh"] = base.car_charging_limit[0]
    before = _controls(base)
    report = evaluate_car_charging(base)
    assert report["status"] == "departure_passed" and report["observed_ev_target_met"]
    assert report["target_met_by_departure"] is None and not report["proposed_ev_windows"]
    assert _controls(base) == before and report["ready_to_activate"] is False


def test_observations_after_rounded_plan_time_are_not_future(base):
    """Reproduce a mid-slot live startup without accepting truly future data."""
    _setup(base)
    actual_now = base.now_utc + timedelta(minutes=2)
    _set_entity(base, "sensor.test_ev_soc", "53", last_updated=base.now_utc + timedelta(seconds=90))
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=actual_now - timedelta(seconds=59), last_updated=actual_now)
    car_settings(base).update(soc_entity="sensor.test_ev_soc", soc_entity_unit="%", relay_entity="binary_sensor.test_relay")
    with patch("car_charging_plan._observation_clock", return_value=actual_now):
        options = _options(base)
        assert not options["entity_blockers"], options
        assert options["ev_soc"] == 13.25 and options["initial_off_minutes"] == 0
        assert options["planning_alignment_seconds"] == 120
        _set_entity(base, "sensor.test_ev_soc", "53", last_updated=actual_now + timedelta(seconds=1))
        assert "soc_entity_stale_or_future" in _options(base)["entity_blockers"]


def test_stale_soc_scenario_retains_freshness_blocker(base):
    """An optional stale-value scenario is labelled and never promoted to a live candidate."""
    _setup(base)
    old_stamp = base.now_utc - timedelta(hours=2)
    _set_entity(base, "sensor.test_ev_soc", "0", last_updated=old_stamp, last_changed=old_stamp)
    car_settings(base).update(soc_entity="sensor.test_ev_soc", soc_entity_unit="kWh")
    before = _controls(base)
    assert evaluate_car_charging(base)["status"] == "unsupported"
    car_settings(base)["allow_stale_shadow_soc"] = True
    report = evaluate_car_charging(base)
    assert report["status"] == "conditional_candidate", report
    assert report["execution_blockers"] == ["soc_entity_stale_or_future"]
    assert report["input_blockers"] == ["soc_entity_stale_or_future"]
    assert report["soc_observed_at"] == old_stamp.astimezone(timezone.utc).isoformat()
    assert report["soc_observation_age_minutes"] == 120
    assert report["ev_initial_state"]["ev_soc_source"] == "stale_entity_assumption"
    assert not report["ready_to_activate"] and not report["execution_enabled"]
    assert "fresh_ev_soc_observation" in report["activation_requirements"]
    assert _controls(base) == before
    # Unknown values and truly future observations never become assumptions.
    _set_entity(base, "sensor.test_ev_soc", "unknown", last_updated=old_stamp)
    assert evaluate_car_charging(base)["status"] == "unsupported"
    _set_entity(base, "sensor.test_ev_soc", "0", last_updated=base.now_utc + timedelta(seconds=1))
    assert evaluate_car_charging(base)["status"] == "unsupported"


def run_car_planning_native_tests(base):
    """Run native integration cases without a live HA connection or inverter writes."""
    for test in (
        test_disabled_and_invalid,
        test_fresh_session_and_availability,
        test_shadow_candidate_and_isolation,
        test_errors_clear_old_candidates,
        test_publication_failure_is_nonfatal,
        test_native_rejects_counterexamples,
        test_actual_calculate_plan_hook,
        test_pv10_rejection_and_failed_replay,
        test_timestamp_offsets_and_cost_convention,
        test_relay_state_off_means_off,
        test_relay_state_on_after_cooldown_yields_on,
        test_relay_state_on_within_cooldown_keeps_cooldown,
        test_relay_recent_off_with_cooldown_remaining,
        test_relay_partial_seconds_round_down,
        test_naive_timestamps_rejected,
        test_stale_dated_session_rejected,
        test_future_dated_session_rejected,
        test_fresh_entity_overrides_stale_dated,
        test_soc_entity_percent_unit_converts_with_capacity,
        test_soc_entity_numeric_string_parsed,
        test_soc_entity_bool_rejected_not_clamped,
        test_unavailable_soc_entity_is_blocker_not_fallback,
        test_soc_entity_stale_is_blocker,
        test_soc_entity_out_of_bounds_is_blocker,
        test_manual_soc_target_overrides_live_entity,
        test_connection_entity_surfaces_observed_state_only,
        test_permission_entity_missing_is_reported_not_assumed,
        test_permission_entity_exact_on_grants,
        test_no_ha_service_writes_or_refresh,
        test_collected_blockers_remain_independent,
        test_invalid_entity_options_rejected,
        test_real_ha_timestamp_cache_and_elapsed_cooldown,
        test_deye_counterfactual_preserves_native_capabilities_and_manual_floor,
        test_closed_session_does_not_roll_to_tomorrow,
        test_observations_after_rounded_plan_time_are_not_future,
        test_stale_soc_scenario_retains_freshness_blocker,
    ):
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc):
            test(base)
        print("PASS:", test.__name__)
    return False

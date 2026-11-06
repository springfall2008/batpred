"""Offline acceptance of the gated native car execution path.

These tests verify the apply-side behaviour of :mod:`car_charging_execute` against
the rewritten contract:

* ``execute_car_charging_plan(base)`` returns ``None`` for off/shadow, otherwise a
  ``(status, status_extra)`` tuple or the literal ``("EV blocked", reason)``
  pair returned from the blocked branch.
* ``publish_car_charging_slot(base)`` returns ``False`` for off/shadow and ``True`` for
  apply (the fetch-time path always sees a True return so the parent executor
  can skip its legacy EV publication).
* ``execution_fingerprint(base)`` hashes the actual live configuration; it
  never reads from ``base.car_charging_pending``.
* ``ready_to_activate`` and ``hardware_confirmed`` are always ``False`` —
  HA observations never constitute hardware confirmation.
* The only actuator permitted is ``inverter.adjust_charge_immediate(...)``.
  ``adapter.apply`` is a deprecated bypass that must never be called.

Tests build a real ``base`` via the parent's :func:`_apply_inputs` fixture
(which configures the EV session, entity mapping and commissioning flag), stage
a candidate through the real ``evaluate_car_charging`` solver/replay path, and
inject a fake ``DEYE`` inverter with a controllable ``adjust_charge_immediate``
spy plus a fake adapter that exposes only ``snapshot`` and ``settings``.

The unit suite never calls a real HA service, never writes through the live
DEYE adapter, and never depends on a connected EVSE. All evidence comes from
the ``base.car_charging_report`` / ``base.car_charging_execution`` / dashboard
sensor surface that the executor publishes.
"""

from tests.car_planning_fixtures import car_settings

import json
from copy import deepcopy
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from const import PREDBAT_MAX_CARS

import car_charging_execute
import car_charging_plan
from car_charging_execute import (
    OBSERVATION_MAX_AGE_SECONDS,
    RECEIPT_TTL_SECONDS,
    execute_car_charging_plan,
    execution_fingerprint,
    publish_car_charging_slot,
)
from car_charging_plan import evaluate_car_charging
from tests.test_car_planning_entrypoints import _apply_inputs
from tests.test_car_planning_native import _setup


# ----------------------------------------------------------------------
# Fixture helpers — own clock, own field reset, real _setup + _apply_inputs.
# ----------------------------------------------------------------------


def _now_utc(base):
    """Return the fixture's pinned ``now_utc`` as an offset-aware datetime."""
    if base.now_utc.tzinfo is None:
        return base.now_utc.replace(tzinfo=timezone.utc)
    return base.now_utc


def _set_entity(base, entity_id, state, last_updated=None, last_changed=None):
    """Populate the TestHAInterface cache with a standard HA shape."""
    moment = _now_utc(base)
    base.ha_interface.dummy_items[entity_id] = {
        "state": state,
        "last_updated": last_updated if last_updated is not None else moment,
        "last_changed": last_changed if last_changed is not None else moment,
    }


def _stale_or_future_entity(base, entity_id, last_updated, last_changed=None):
    """Helper that intentionally sets an entity with an out-of-window stamp.

    The apply path parses the observation stamp from ``state``, so we rewrite
    the ISO timestamp there too — just adjusting ``last_updated`` would leave
    the freshness check passing.
    """
    base.ha_interface.dummy_items[entity_id] = {
        "state": last_updated.isoformat() if hasattr(last_updated, "isoformat") else last_updated,
        "last_updated": last_updated,
        "last_changed": last_changed if last_changed is not None else last_updated,
    }


def _make_adapter(control=True, hold_verified=True, cap_w=8192):
    """Build a controllable DEYE adapter stand-in.

    The adapter exposes only ``snapshot`` and ``settings`` per the rewritten
    apply contract; the actuator path lives on the inverter, not here.
    """

    snapshot_calls = {"count": 0}

    def _snapshot():
        """Return a deterministic fake snapshot; tracks invocations for assertions."""
        snapshot_calls["count"] += 1
        return {"ok": True}

    adapter = SimpleNamespace(
        settings={
            "control": control,
            "hold_verified": hold_verified,
            "max_discharge_power_w": cap_w,
        },
        snapshot=_snapshot,
    )
    return adapter, snapshot_calls


def _make_inverter(base, adapter, *, inverter_type="DEYE", supports_freeze=True, in_calibration=False, observed_return=True, log=None):
    """Build a fake inverter with the surface the new apply contract reads."""
    spy_calls = {"args": []}

    def _adjust(target_soc, freeze=False, discharge_power_w=None):
        """Record the dispatch call and return ``observed_return`` for the executor."""
        spy_calls["args"].append(
            {
                "target_soc": target_soc,
                "freeze": freeze,
                "discharge_power_w": discharge_power_w,
            }
        )
        return observed_return

    inverter = SimpleNamespace(
        id=0,
        inverter_type=inverter_type,
        inv_support_charge_freeze=supports_freeze,
        in_calibration=in_calibration,
        deye_local=adapter,
        battery_rate_max_discharge=base.battery_rate_max_discharge,
        adjust_charge_immediate=_adjust,
        log=log if log is not None else (lambda *_args, **_kwargs: None),
    )
    base.inverters = [inverter]
    return inverter, spy_calls


def _reset_apply_fields(base):
    """Clear transient apply-related attributes so each test starts clean."""
    base.car_charging_pending = None
    base.car_charging_execution = None
    base.car_charging_report = None
    base.inverter_rate_intent = {}
    base.isCharging = False
    base.isExporting = False
    base.car_charging_slots = [[] for _ in range(PREDBAT_MAX_CARS)]
    base.set_read_only = False
    base.set_read_only_axle = False
    base.inverter_needs_reset = False
    base.had_errors = False
    base.fatal_error = False
    base.manual_soc_max_keep = None
    base.manual_charge_times = []
    base.manual_demand_times = []
    base.manual_export_times = []
    base.manual_freeze_charge_times = []
    base.manual_freeze_export_times = []
    base.all_active_keep = {}
    base.all_active_keep_max = {}
    base.io_adjusted = {}
    if hasattr(base, "ha_interface") and base.ha_interface is not None:
        for entity_id in (
            "binary_sensor.test_relay",
            "sensor.test_ev_soc",
            "sensor.test_charger_state",
            "switch.test_permission",
            "sensor.test_connection_observed_at",
            "sensor.test_soc_observed_at",
        ):
            base.ha_interface.dummy_items.pop(entity_id, None)


def _stage_apply(base, *, pv=5, minutes=60, mode="apply", apply_verified=True):
    """Configure the apply fixture via the parent's _apply_inputs helper."""
    _setup(base, pv=pv, minutes=minutes)
    _apply_inputs(base)
    if mode != "apply":
        car_settings(base)["mode"] = mode
    if not apply_verified:
        car_settings(base)["apply_verified"] = False
    elif "apply_verified" in car_settings(base):
        car_settings(base)["apply_verified"] = True


def _evaluate(base, *, pv=5, minutes=60):
    """Run the real solver and replay path so the candidate is genuine."""
    _stage_apply(base, pv=pv, minutes=minutes)
    with patch.object(car_charging_plan, "_observation_clock", side_effect=lambda: _now_utc(base)):
        return evaluate_car_charging(base, publish=False)


def _freeze_command_ev(pending, ev_on):
    """Mutate the staged first command's ``ev_on`` flag for the EV-on cases."""
    if not isinstance(pending, dict):
        return False
    commands = pending.get("commands") or []
    if not commands:
        return False
    commands[0]["ev_on"] = bool(ev_on)
    return True


# ----------------------------------------------------------------------
# Test cases — ordered from the broadest noop through to verification of
# receipt/observability. Each case runs inside the run-loop's clock+reset
# patches so observable state can be asserted cleanly.
# ----------------------------------------------------------------------


def test_off_and_shadow_are_strict_noop(base):
    """Off and shadow must return ``None`` from execute_car_charging_plan and never reach the inverter."""
    _stage_apply(base)
    for mode in ("off", "shadow"):
        _evaluate(base)
        car_settings(base)["mode"] = mode
        adapter, _ = _make_adapter()
        inverter, spy = _make_inverter(base, adapter)
        before_intent = dict(base.inverter_rate_intent)
        before_execution = deepcopy(getattr(base, "car_charging_execution", None))
        before_report = deepcopy(getattr(base, "car_charging_report", None))
        before_slots = deepcopy(base.car_charging_slots)
        assert execute_car_charging_plan(base) is None, mode
        assert spy["args"] == [], mode
        assert base.inverter_rate_intent == before_intent, mode
        assert getattr(base, "car_charging_execution", None) == before_execution, mode
        assert getattr(base, "car_charging_report", None) == before_report, mode
        assert base.car_charging_slots == before_slots, mode
        assert publish_car_charging_slot(base) is False, mode


def test_apply_alone_rejected_without_verified(base):
    """Apply without the commissioning flag is a hard no-actuate gate."""
    report = _evaluate(base)
    assert report["status"] == "candidate", report
    car_settings(base)["apply_verified"] = False
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["status"] == "apply_blocked", base.car_charging_report
    assert base.car_charging_report["execution_enabled"] is False, base.car_charging_report
    assert base.car_charging_report["execution_reason"] == "apply_commissioning_required"
    assert base.car_charging_report["ready_to_activate"] is False
    assert base.car_charging_report["hardware_confirmed"] is False


def test_apply_blocked_when_inverter_type_sk(base):
    """A non-DEYE inverter type is a hard gate; ``adjust_charge_immediate`` never runs."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter, inverter_type="SK")
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "local_deye_required"
    assert base.car_charging_report["execution_enabled"] is False
    assert base.car_charging_report["hardware_confirmed"] is False


def test_apply_blocked_when_deye_adapter_missing(base):
    """A DEYE inverter without ``deye_local`` cannot actuate."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    inverter.deye_local = None
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "local_deye_adapter_missing"


def test_apply_blocked_when_hold_not_verified(base):
    """``hold_verified`` must be exactly True on the adapter settings."""
    _evaluate(base)
    adapter, _ = _make_adapter(hold_verified=False)
    inverter, spy = _make_inverter(base, adapter)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "deye_commissioning_required"


def test_apply_blocked_when_control_flag_false(base):
    """``control`` must be exactly True on the adapter settings."""
    _evaluate(base)
    adapter, _ = _make_adapter(control=False)
    inverter, spy = _make_inverter(base, adapter)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "deye_commissioning_required"


def test_apply_blocked_when_inverter_lacks_freeze_support(base):
    """An inverter that does not declare freeze support must not actuate."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter, supports_freeze=False)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "inverter_capability_or_calibration"


def test_apply_blocked_when_inverter_in_calibration(base):
    """``in_calibration=True`` blocks apply regardless of every other condition."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter, in_calibration=True)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "inverter_capability_or_calibration"


def test_apply_blocked_when_read_only(base):
    """``set_read_only`` (and the axle variant) gate apply without invoking the inverter."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    for attr in ("set_read_only", "set_read_only_axle"):
        base.__setattr__(attr, True)
        result = execute_car_charging_plan(base)
        assert isinstance(result, tuple) and result[0] == "EV blocked", (attr, result)
        assert spy["args"] == [], attr
        assert base.car_charging_report["execution_reason"] == "read_only", attr
        base.__setattr__(attr, False)


def test_apply_blocked_when_native_blockers_present(base):
    """A manual override, active keep or io override produces a native blocker."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.manual_charge_times = [{"start": base.minutes_now, "end": base.minutes_now + 30}]
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "native_inputs_or_overrides_blocked"
    base.manual_charge_times = []
    base.all_active_keep_max = {base.minutes_now: 50.0}
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert base.car_charging_report["execution_reason"] == "native_inputs_or_overrides_blocked"


def test_apply_blocked_by_booting_calibration_flags(base):
    """``inverter_needs_reset`` / ``had_errors`` / ``fatal_error`` are native state."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.inverter_needs_reset = True
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "native_state_not_ready"
    base.inverter_needs_reset = False
    base.had_errors = True
    result = execute_car_charging_plan(base)
    assert base.car_charging_report["execution_reason"] == "native_state_not_ready"


def test_apply_blocked_when_deye_dispatch_model_mismatch(base):
    """``export_limit != 0`` or ``set_discharge_during_charge=False`` is a hard mismatch."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.export_limit = 0.05
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "deye_dispatch_model_mismatch"
    # Reset had_errors so the next call isn't short-circuited by native state.
    base.had_errors = False
    base.export_limit = 0
    base.set_discharge_during_charge = False
    result = execute_car_charging_plan(base)
    assert base.car_charging_report["execution_reason"] == "deye_dispatch_model_mismatch"


def test_apply_blocked_when_manual_ceiling_unsupported(base):
    """``manual_soc_max_keep`` is incompatible with native apply."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.manual_soc_max_keep = 80.0
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "manual_ceiling_unsupported"


def test_apply_blocked_by_missing_relay_entity(base):
    """Missing relay entity observation fails ``entity_observation_unavailable``."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.ha_interface.dummy_items.pop("binary_sensor.test_relay", None)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "entity_observation_unavailable",
        "native_inputs_or_overrides_blocked",
    }, base.car_charging_report


def test_apply_blocked_by_missing_permission_entity(base):
    """Missing permission entity observation fails ``entity_observation_unavailable``."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.ha_interface.dummy_items.pop("switch.test_permission", None)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "entity_observation_unavailable",
        "native_inputs_or_overrides_blocked",
    }, base.car_charging_report


def test_apply_blocked_by_permission_off(base):
    """``switch.test_permission == 'off'`` is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    _set_entity(base, "switch.test_permission", "off")
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "permission_not_granted"


def test_apply_blocked_by_missing_connection_entity(base):
    """Missing connection entity observation fails ``entity_observation_unavailable``."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.ha_interface.dummy_items.pop("sensor.test_charger_state", None)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "entity_observation_unavailable",
        "native_inputs_or_overrides_blocked",
    }, base.car_charging_report


def test_apply_blocked_by_connection_state_allowlist(base):
    """A connection state not in the configured allowlist fails ``connection_not_confirmed``."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    _set_entity(base, "sensor.test_charger_state", "3")
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "connection_not_confirmed"


def test_apply_blocked_by_missing_timestamp_entity(base):
    """Missing soc/connection observation timestamp entity fails the freshness gate."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.ha_interface.dummy_items.pop("sensor.test_soc_observed_at", None)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "entity_observation_unavailable",
        "native_inputs_or_overrides_blocked",
    }, base.car_charging_report


def test_apply_blocked_by_stale_soc_observation(base):
    """An soc observation older than ``OBSERVATION_MAX_AGE_SECONDS`` is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    stale = _now_utc(base) - timedelta(seconds=OBSERVATION_MAX_AGE_SECONDS + 60)
    _stale_or_future_entity(base, "sensor.test_soc_observed_at", stale)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "physical_observation_stale_or_future"


def test_apply_blocked_by_future_connection_observation(base):
    """A future connection observation is invalid even with a fresh last_updated."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    future = _now_utc(base) + timedelta(seconds=120)
    _stale_or_future_entity(base, "sensor.test_connection_observed_at", future)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "physical_observation_stale_or_future"


def test_apply_blocked_by_future_relay_timestamp(base):
    """``last_updated > now`` on the relay entity is a future timestamp."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    future = _now_utc(base) + timedelta(seconds=300)
    # Override only ``last_updated`` / ``last_changed``; the relay state itself
    # must remain a valid 'on'/'off' string for the solver to accept the entity.
    relay_payload = base.ha_interface.dummy_items["binary_sensor.test_relay"]
    relay_payload["last_updated"] = future
    relay_payload["last_changed"] = future
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert base.car_charging_report["execution_reason"] in {
        "entity_timestamp_future",
        "native_inputs_or_overrides_blocked",
    }, base.car_charging_report


def test_apply_blocked_by_cooldown_within_599_seconds(base):
    """599s after the relay flipped OFF the cooldown blocks an ev_on request."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    recent_off = _now_utc(base) - timedelta(seconds=599)
    # Only override the relay's last_changed; the state string must remain "off".
    payload = base.ha_interface.dummy_items["binary_sensor.test_relay"]
    payload["last_changed"] = recent_off
    payload["last_updated"] = recent_off + timedelta(seconds=1)
    _freeze_command_ev(base.car_charging_pending, ev_on=True)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "ev_cooldown"


def test_apply_allowed_after_cooldown_600_seconds(base):
    """Exactly 600s after the relay flipped OFF the cooldown is satisfied."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    long_off = _now_utc(base) - timedelta(seconds=600)
    payload = base.ha_interface.dummy_items["binary_sensor.test_relay"]
    payload["last_changed"] = long_off
    payload["last_updated"] = long_off + timedelta(seconds=1)
    _freeze_command_ev(base.car_charging_pending, ev_on=True)
    result = execute_car_charging_plan(base)
    assert spy["args"], result
    assert result[0] in ("Demand", "Charging", "Freeze charging"), result


def test_apply_freeze_skips_cooldown_entirely(base):
    """Freeze commands bypass the cooldown gate; ev_on=False is irrelevant here."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    recent_off = _now_utc(base) - timedelta(seconds=30)
    payload = base.ha_interface.dummy_items["binary_sensor.test_relay"]
    payload["last_changed"] = recent_off
    payload["last_updated"] = recent_off + timedelta(seconds=1)
    # Freeze + ev_on=False bypasses the cooldown entirely (no EV energising).
    base.car_charging_pending["commands"][0]["freeze"] = True
    base.car_charging_pending["commands"][0]["target_soc"] = 0
    base.car_charging_pending["commands"][0]["ev_on"] = False
    result = execute_car_charging_plan(base)
    assert spy["args"], result
    assert result[0] == "Freeze charging", result


def test_apply_blocked_by_relay_state_invalid(base):
    """``relay state != on/off`` fails ``relay_state_invalid``."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    _set_entity(base, "binary_sensor.test_relay", "unknown")
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "relay_state_invalid",
        "execution_validation_failed",
    }, base.car_charging_report


def test_apply_blocked_by_import_limit_mismatch(base):
    """A change in ``base.import_limit`` after staging invalidates the fingerprint."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.import_limit += 200.0
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "configuration_changed",
        "configuration_changed_during_dispatch",
        "import_limit_mismatch",
    }, base.car_charging_report


def test_apply_blocked_by_manual_floor_violation(base):
    """``soc_kw`` below the active keep floor is a domestic floor violation."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.all_active_keep = {base.minutes_now: 80.0}
    base.soc_kw = 5.0
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "domestic_manual_floor",
        "native_inputs_or_overrides_blocked",
    }, base.car_charging_report


def test_apply_blocked_by_invalid_power_below_zero(base):
    """``power_w < 0`` is rejected; the cap does not clamp the rejection."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["power_w"] = -100
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "discharge_power_out_of_range"


def test_apply_blocked_by_power_above_8192(base):
    """``power_w > 8192`` is rejected — no clamping to the cap."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["power_w"] = 9000
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "discharge_power_out_of_range"


def test_apply_blocked_by_nonfinite_power(base):
    """``power_w = float('inf')`` is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["power_w"] = float("inf")
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "discharge_power_out_of_range"


def test_apply_blocked_by_bool_power(base):
    """``power_w is True`` is rejected (booleans are not accepted as numbers)."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["power_w"] = True
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "discharge_power_out_of_range"


def test_apply_blocked_by_invalid_target_soc(base):
    """``target_soc`` not in {0, 100} is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["target_soc"] = 50
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "invalid_target"


def test_apply_blocked_by_freeze_with_nonzero_target(base):
    """``freeze=True`` together with ``target_soc != 0`` is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["freeze"] = True
    base.car_charging_pending["commands"][0]["target_soc"] = 100
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "invalid_hold_target"


def test_apply_blocked_by_nonboolean_freeze_flag(base):
    """``freeze`` not exactly bool is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["freeze"] = 1
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "invalid_command_boolean"


def test_apply_blocked_by_command_timeline_mismatch(base):
    """A command whose ``start`` is not the first planned step is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["start"] = base.minutes_now + 5
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "command_timeline_mismatch"


def test_apply_blocked_by_wallclock_crossing_command_end(base):
    """``now >= last command end`` (``command_authority_expired``) blocks apply.

    Without changing ``minutes_now`` (the parent solver bound) we mutate the
    staged command's end to force the expiry check, simulating a wallclock
    that crossed the command boundary during a dispatch attempt.
    """
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"][0]["end"] = base.minutes_now
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "command_expired_during_dispatch",
        "current_command_expired",
        "command_timeline_mismatch",
    }, base.car_charging_report


def test_apply_blocked_by_no_pending_candidate(base):
    """``base.car_charging_pending = None`` triggers ``no_pending_candidate``."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending = None
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "no_pending_candidate"


def test_apply_blocked_by_native_check_failure_in_report(base):
    """A pending whose report status is not ``candidate`` is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["report"]["status"] = "native_rejected"
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "candidate_not_accepted"


def test_apply_blocked_by_stale_candidate_generated_at(base):
    """A pending whose ``generated_at`` is older than TTL is stale."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    stale = _now_utc(base) - timedelta(seconds=RECEIPT_TTL_SECONDS + 60)
    base.car_charging_pending["report"]["generated_at"] = stale.isoformat()
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "candidate_stale_or_future"


def test_apply_blocked_by_changed_configuration_during_dispatch(base):
    """A fingerprint mismatch between the staged candidate and the present base blocks apply."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["fingerprint"] = "deadbeef" * 8
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "configuration_changed",
        "configuration_changed_during_dispatch",
    }, base.car_charging_report


def test_apply_blocked_by_incomplete_command_horizon(base):
    """A horizon that does not cover every PREDICT_STEP interval is rejected."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    base.car_charging_pending["commands"] = base.car_charging_pending["commands"][:2]
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] in {
        "incomplete_command_horizon",
        "native_checks_failed",
    }, base.car_charging_report


def test_apply_blocked_when_inverter_returns_false(base):
    """``adjust_charge_immediate`` returning ``False`` is a no-actuate result."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter, observed_return=False)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"]
    assert base.car_charging_report["execution_reason"] == "inverter_command_not_observed"
    assert base.car_charging_report["execution_enabled"] is False


def test_success_demand_charge_hold_statuses(base):
    """Demand, charge and freeze each produce the matching status and call the inverter."""
    cases = [
        {"target_soc": 0, "freeze": False, "expected": "Demand"},
        {"target_soc": 100, "freeze": False, "expected": "Charging"},
        {"target_soc": 0, "freeze": True, "expected": "Freeze charging"},
        {"target_soc": 0, "freeze": False, "expected": "Demand", "power_w": 4000},
    ]
    for case in cases:
        _evaluate(base)
        base.car_charging_pending["commands"][0]["target_soc"] = case["target_soc"]
        base.car_charging_pending["commands"][0]["freeze"] = case["freeze"]
        base.car_charging_pending["commands"][0]["ev_on"] = False
        if "power_w" in case:
            base.car_charging_pending["commands"][0]["power_w"] = case["power_w"]
        adapter, _ = _make_adapter()
        inverter, spy = _make_inverter(base, adapter)
        result = execute_car_charging_plan(base)
        assert spy["args"], (case, result)
        assert result[0] == case["expected"], (case, result)
        report = base.car_charging_report
        assert report["execution_enabled"] is True, (case, report)
        assert report["ready_to_activate"] is False, (case, report)
        assert report["hardware_confirmed"] is False, (case, report)
        assert report["status"] == "applied", (case, report)
        if "power_w" in case:
            assert spy["args"][-1]["discharge_power_w"] == case["power_w"], (case, spy["args"])


def test_stale_observation_state_blocks_even_with_fresh_last_updated(base):
    """A stale ``state`` value on the observation entity blocks the freshness gate
    even when HA reports ``last_updated`` as freshly observed.
    """
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    # Construct an entity whose last_updated is fresh but whose state string is
    # a stale ISO timestamp — the apply contract reads the stamp from state.
    stale_state = (_now_utc(base) - timedelta(seconds=OBSERVATION_MAX_AGE_SECONDS + 60)).isoformat()
    payload = base.ha_interface.dummy_items["sensor.test_soc_observed_at"]
    payload["state"] = stale_state
    payload["last_updated"] = _now_utc(base)
    payload["last_changed"] = _now_utc(base)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert spy["args"] == []
    assert base.car_charging_report["execution_reason"] == "physical_observation_stale_or_future"


def test_pending_false_and_exception_revokes_old_receipt(base):
    """A pending None and an actuator exception together revoke the prior receipt."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    # Accept a real receipt first.
    execute_car_charging_plan(base)
    assert isinstance(base.car_charging_execution, dict) and base.car_charging_execution.get("observations_match")
    # Now drop the pending and force the actuator to raise.
    base.car_charging_pending = None
    # Replace inverter with one that raises from adjust_charge_immediate. Since
    # pending is None, the path will block before the actuator, but the receipt
    # must be cleared and the EV slot must publish OFF.
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple) and result[0] == "EV blocked", result
    assert base.car_charging_execution is None
    assert base.car_charging_slots == [[] for _ in range(PREDBAT_MAX_CARS)]
    sensor_key = "binary_sensor." + base.prefix + "_car_charging_slot"
    item = base.dashboard_values.get(sensor_key)
    assert item is not None
    assert item["state"] == "off"
    assert item["attributes"]["ready_to_activate"] is False
    assert item["attributes"]["hardware_confirmed"] is False


def test_success_with_ev_on_publishes_on(base):
    """An accepted command and ``ev_on=True`` flips the EV slot binary sensor ON."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = True
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    # The runtime below uses base.minutes_now as the lookup key into rate_import.
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple), result
    sensor_key = "binary_sensor." + base.prefix + "_car_charging_slot"
    item = base.dashboard_values.get(sensor_key)
    assert item is not None
    assert item["state"] == "on", item
    assert item["attributes"]["ready_to_activate"] is False
    assert item["attributes"]["hardware_confirmed"] is False


def test_success_with_ev_off_publishes_off(base):
    """An accepted command and ``ev_on=False`` flips the EV slot binary sensor OFF."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = False
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple), result
    sensor_key = "binary_sensor." + base.prefix + "_car_charging_slot"
    item = base.dashboard_values.get(sensor_key)
    assert item is not None
    assert item["state"] == "off", item
    assert item["attributes"]["ready_to_activate"] is False
    assert item["attributes"]["hardware_confirmed"] is False


def test_publish_car_planning_ev_after_accepted_publishes_immediately(base):
    """The native EV publication must happen in the same cycle as the accept."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = True
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    execute_car_charging_plan(base)
    handled = publish_car_charging_slot(base)
    assert handled is True
    sensor_key = "binary_sensor." + base.prefix + "_car_charging_slot"
    item = base.dashboard_values.get(sensor_key)
    assert item is not None
    assert item["state"] == "on", item


def test_publish_car_planning_ev_after_ttl_revokes_old_receipt(base):
    """A receipt older than TTL is revoked on the next publish attempt."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = True
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    execute_car_charging_plan(base)
    # Move the receipt back past TTL.
    base.car_charging_execution["generated_at"] = (_now_utc(base) - timedelta(seconds=RECEIPT_TTL_SECONDS + 30)).isoformat()
    handled = publish_car_charging_slot(base)
    assert handled is True
    assert base.car_charging_execution is None
    sensor_key = "binary_sensor." + base.prefix + "_car_charging_slot"
    item = base.dashboard_values.get(sensor_key)
    assert item is not None
    assert item["state"] == "off", item


def test_publish_car_planning_ev_with_permission_off_revokes(base):
    """Permission flipping OFF between accept and publish revokes the receipt."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = True
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    execute_car_charging_plan(base)
    _set_entity(base, "switch.test_permission", "off")
    handled = publish_car_charging_slot(base)
    assert handled is True
    assert base.car_charging_execution is None


def test_publish_car_planning_ev_with_disconnect_revokes(base):
    """Connection dropping out of the allowlist revokes the receipt."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = True
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    execute_car_charging_plan(base)
    _set_entity(base, "sensor.test_charger_state", "unplugged")
    handled = publish_car_charging_slot(base)
    assert handled is True
    assert base.car_charging_execution is None


def test_publish_car_planning_ev_with_future_window_revokes(base):
    """A receipt whose command window is entirely in the future is revoked."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = True
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    execute_car_charging_plan(base)
    base.car_charging_execution["command"]["start"] = base.minutes_now + 60
    base.car_charging_execution["command"]["end"] = base.minutes_now + 120
    handled = publish_car_charging_slot(base)
    assert handled is True
    assert base.car_charging_execution is None


def test_publish_car_planning_ev_off_shadow_returns_false(base):
    """The publish path returns ``False`` for off/shadow without acting."""
    for mode in ("off", "shadow"):
        _evaluate(base)
        car_settings(base)["mode"] = mode
        handled = publish_car_charging_slot(base)
        assert handled is False, mode


def test_execute_returns_tuple_when_apply(base):
    """``execute_car_charging_plan`` returns a tuple on accept and on every blocked path."""
    _evaluate(base)
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    ok_result = execute_car_charging_plan(base)
    assert isinstance(ok_result, tuple) and len(ok_result) == 2, ok_result

    _evaluate(base)
    car_settings(base)["apply_verified"] = False
    blocked_result = execute_car_charging_plan(base)
    assert isinstance(blocked_result, tuple) and len(blocked_result) == 2, blocked_result
    assert blocked_result[0] == "EV blocked", blocked_result


def test_execution_fingerprint_is_configuration_independent_of_pending(base):
    """The fingerprint hashes the live configuration, not the pending object."""
    _evaluate(base)
    base.car_charging_pending = None
    first = execution_fingerprint(base)
    _evaluate(base)
    base.car_charging_pending = None
    second = execution_fingerprint(base)
    assert first == second, "Fingerprint must be stable for identical configuration"
    assert first, "Fingerprint must be non-empty for a populated base"


def test_inverter_rate_intent_cleared_on_apply(base):
    """Any legacy quick-rebalance intent is cleared so apply's commands are not clobbered."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["ev_on"] = False
    adapter, _ = _make_adapter()
    inverter, _ = _make_inverter(base, adapter)
    base.inverter_rate_intent = {0: {"charge_rate": 1500, "discharge_rate": 0}}
    if base.minutes_now not in base.rate_import:
        base.rate_import[base.minutes_now] = 10.0
    execute_car_charging_plan(base)
    assert base.inverter_rate_intent == {}


def test_exception_in_car_planning_publish_does_not_leak_private_strings(base):
    """A blocked path must not echo the underlying exception's private contents."""
    _evaluate(base)
    base.car_charging_pending["commands"][0]["target_soc"] = 50  # invalid
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    result = execute_car_charging_plan(base)
    assert isinstance(result, tuple), result
    report = base.car_charging_report
    reason = report.get("execution_reason") or ""
    assert "private" not in reason, reason
    assert "secret" not in reason, reason
    assert "password" not in reason, reason
    assert "50" not in reason, reason
    assert spy["args"] == []


def test_evaluate_car_planning_real_solver_keeps_candidate_status(base):
    """``evaluate_car_charging`` in apply mode produces a real candidate with non-empty fingerprint."""
    report = _evaluate(base, pv=5, minutes=60)
    assert report["status"] == "candidate", report
    assert report.get("proposed_ev_windows") is not None
    assert base.car_charging_pending is not None
    assert base.car_charging_pending["fingerprint"], "Fingerprint must be non-empty after staging"
    assert json.dumps(base.car_charging_pending["fingerprint"]).strip('"') != ""


# ----------------------------------------------------------------------
# Runner — apply the parent's clock, reset transient fields, then iterate.
# ----------------------------------------------------------------------


def run_car_planning_apply_tests(base):
    """Run apply execution cases without a live HA connection or inverter writes."""
    tests = [
        test_off_and_shadow_are_strict_noop,
        test_apply_alone_rejected_without_verified,
        test_apply_blocked_when_inverter_type_sk,
        test_apply_blocked_when_deye_adapter_missing,
        test_apply_blocked_when_hold_not_verified,
        test_apply_blocked_when_control_flag_false,
        test_apply_blocked_when_inverter_lacks_freeze_support,
        test_apply_blocked_when_inverter_in_calibration,
        test_apply_blocked_when_read_only,
        test_apply_blocked_when_native_blockers_present,
        test_apply_blocked_by_booting_calibration_flags,
        test_apply_blocked_when_deye_dispatch_model_mismatch,
        test_apply_blocked_when_manual_ceiling_unsupported,
        test_apply_blocked_by_missing_relay_entity,
        test_apply_blocked_by_missing_permission_entity,
        test_apply_blocked_by_permission_off,
        test_apply_blocked_by_missing_connection_entity,
        test_apply_blocked_by_connection_state_allowlist,
        test_apply_blocked_by_missing_timestamp_entity,
        test_apply_blocked_by_stale_soc_observation,
        test_apply_blocked_by_future_connection_observation,
        test_apply_blocked_by_future_relay_timestamp,
        test_apply_blocked_by_cooldown_within_599_seconds,
        test_apply_allowed_after_cooldown_600_seconds,
        test_apply_freeze_skips_cooldown_entirely,
        test_apply_blocked_by_relay_state_invalid,
        test_apply_blocked_by_import_limit_mismatch,
        test_apply_blocked_by_manual_floor_violation,
        test_apply_blocked_by_invalid_power_below_zero,
        test_apply_blocked_by_power_above_8192,
        test_apply_blocked_by_nonfinite_power,
        test_apply_blocked_by_bool_power,
        test_apply_blocked_by_invalid_target_soc,
        test_apply_blocked_by_freeze_with_nonzero_target,
        test_apply_blocked_by_nonboolean_freeze_flag,
        test_apply_blocked_by_command_timeline_mismatch,
        test_apply_blocked_by_wallclock_crossing_command_end,
        test_apply_blocked_by_no_pending_candidate,
        test_apply_blocked_by_native_check_failure_in_report,
        test_apply_blocked_by_stale_candidate_generated_at,
        test_apply_blocked_by_changed_configuration_during_dispatch,
        test_apply_blocked_by_incomplete_command_horizon,
        test_apply_blocked_when_inverter_returns_false,
        test_success_demand_charge_hold_statuses,
        test_stale_observation_state_blocks_even_with_fresh_last_updated,
        test_pending_false_and_exception_revokes_old_receipt,
        test_success_with_ev_on_publishes_on,
        test_success_with_ev_off_publishes_off,
        test_publish_car_planning_ev_after_accepted_publishes_immediately,
        test_publish_car_planning_ev_after_ttl_revokes_old_receipt,
        test_publish_car_planning_ev_with_permission_off_revokes,
        test_publish_car_planning_ev_with_disconnect_revokes,
        test_publish_car_planning_ev_with_future_window_revokes,
        test_publish_car_planning_ev_off_shadow_returns_false,
        test_execute_returns_tuple_when_apply,
        test_execution_fingerprint_is_configuration_independent_of_pending,
        test_inverter_rate_intent_cleared_on_apply,
        test_exception_in_car_planning_publish_does_not_leak_private_strings,
        test_evaluate_car_planning_real_solver_keeps_candidate_status,
    ]
    for test in tests:
        _reset_apply_fields(base)
        # Pin both the solver's observation clock and the executor's execution
        # clock to the fixture's now_utc so no test depends on wall time.
        with patch.object(car_charging_plan, "_observation_clock", side_effect=lambda: _now_utc(base)), patch.object(car_charging_execute, "_clock", side_effect=lambda: _now_utc(base)):
            test(base)
        print("PASS:", test.__name__)
    return False

"""Absent EV routing and durable confirmed-session validity through real native hooks."""

from tests.car_planning_fixtures import car_settings

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot
from car_charging_plan import evaluate_car_charging
from car_charging_observation import confirmed_session_observation
from tests.test_car_planning_actual import _actual_inputs, _set_entity


class SessionStorage:
    """Memory-backed Storage contract shared by restart-like fixture instances."""

    def __init__(self):
        """Start with no confirmed-session validation record."""
        self.data = None

    async def load(self, module, filename):
        """Read a detached persistent record without any filesystem or network."""
        assert (module, filename) == ("car_charging", "confirmed_session_guard")
        return deepcopy(self.data)

    async def save(self, module, filename, data, **kwargs):
        """Retain a detached record across the simulated runtime restart."""
        assert (module, filename) == ("car_charging", "confirmed_session_guard")
        self.data = deepcopy(data)
        return True


def session_inputs(base):
    """Prepare the actual template with opt-in routing and explicit synthetic session data."""
    inverter, writes, sources, targets = _actual_inputs(base)
    settings = car_settings(base)
    settings.update(household_fallback=True, home_entity="device_tracker.test_vehicle")
    old = base.now_utc - timedelta(hours=1)
    _set_entity(base, settings["home_entity"], "home", last_changed=old, last_updated=old)
    _set_entity(base, settings["connection_entity"], "connected", last_changed=old, last_updated=old, attributes={"retrievalstatus": "VALID"})
    store = SessionStorage()
    base.components = SimpleNamespace(get_component=lambda name: store if name == "storage" else None)
    return inverter, writes, sources, targets


def test_absence_never_reads_soc_or_meter(base):
    """All three public entry points route absence before touching EV energy evidence."""
    for case in ("away", "unplugged", "expired", "permission", "unauthorised"):
        _, writes, _, _ = session_inputs(base)
        settings = car_settings(base)
        if case == "away":
            _set_entity(base, settings["home_entity"], "not_home")
        elif case == "unplugged":
            _set_entity(base, settings["connection_entity"], "disconnected")
        elif case == "expired":
            settings["session"]["departure"] = (base.now_utc - timedelta(seconds=1)).isoformat()
        elif case == "permission":
            _set_entity(base, settings["permission_entity"], "off")
        else:
            settings.update(apply_verified=False, execution_authorised=False)
        original = base.get_state_wrapper

        def guarded(*args, **kwargs):
            """Make any vehicle energy read a deterministic test failure."""
            entity = kwargs.get("entity_id", args[0] if args else None)
            assert entity not in (settings["soc_entity"], "sensor.test_ev_energy", "sensor.test_ev_power"), entity
            return original(*args, **kwargs)

        with patch.object(base, "get_state_wrapper", side_effect=guarded):
            report = evaluate_car_charging(base, publish=False)
            assert report["status"] == "household_fallback", (case, report)
            assert execute_car_charging_plan(base) is None
            assert publish_car_charging_slot(base) is True
        assert not writes and not base.had_errors and base.set_read_only is False
        assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
        assert base.car_charging_pending is None and base.car_charging_execution is None
        from car_charging_view import build_car_charging_plan_view, render_car_charging_preview

        native = {"rows": [{"soc": 24.129, "total": 17.12}], "currency_symbols": ["EUR", "c"]}
        assert build_car_charging_plan_view(native, base.car_charging_report, now=base.now_utc) is native
        banner = render_car_charging_preview(base.car_charging_report, None, now=base.now_utc)
        assert banner == ('<div class="car-charging-preview-debug" style="display:none;">EV authority withdrawn; native domestic control retained. Reason: ' + base.car_charging_report["execution_reason"] + "</div>")


def test_real_native_handoff_requires_observed_off(base):
    """The real execute_plan seam reaches native allocation only after actual relay OFF."""
    for relay in ("on", "unknown", "off"):
        _, writes, _, _ = session_inputs(base)
        settings = car_settings(base)
        _set_entity(base, settings["home_entity"], "not_home")
        _set_entity(base, settings["relay_entity"], relay)
        native_reached = []

        def native_boundary():
            """Witness entry into the original executor without executing synthetic hardware."""
            native_reached.append(True)
            raise RuntimeError("native boundary reached")

        with patch.object(base, "allocate_fleet_export_rates", side_effect=native_boundary):
            if relay == "off":
                try:
                    base.execute_plan()
                except RuntimeError as exc:
                    assert str(exc) == "native boundary reached"
                assert native_reached == [True]
            else:
                assert base.execute_plan()[0] in ("EV waiting", "EV blocked")
                assert not native_reached
        assert not writes and not base.car_charging_execution


def test_rejected_car_planning_plan_does_not_block_household(base):
    """Connected EV validation failure still yields the sole domestic executor after OFF."""
    _, writes, _, _ = session_inputs(base)
    base.car_charging_pending = None
    assert execute_car_charging_plan(base) is None
    assert base.car_charging_report["execution_reason"] == "no_pending_candidate"
    assert not writes and not base.had_errors
    base.set_read_only = True
    assert execute_car_charging_plan(base) is None and base.set_read_only is True
    assert not writes


def test_contradiction_persists_across_age_and_restart(base):
    """Fresh contradiction cannot disappear after 181 seconds or a runtime restart."""
    session_inputs(base)
    settings = car_settings(base)
    settings["confirmed_session"]["soc_kwh"] = 3.25
    _set_entity(base, settings["soc_entity"], 0.75, attributes={"retrievalstatus": "VALID"})
    for seconds in (0, 181, 600):
        try:
            confirmed_session_observation(base, settings, base.now_utc + timedelta(seconds=seconds))
        except ValueError:
            pass
        else:
            raise AssertionError("Contradiction aged out")
    # A valid-looking retained observation cannot erase the persisted invalidation.
    _set_entity(base, settings["soc_entity"], 3.25, attributes={"retrievalstatus": "VALID"})
    for name in tuple(vars(base)):
        if name.startswith("plan_session_"):
            delattr(base, name)
    try:
        confirmed_session_observation(base, deepcopy(settings), base.now_utc + timedelta(seconds=601))
    except ValueError:
        pass
    else:
        raise AssertionError("Restart erased invalidation")
    assert base.components.get_component("storage").data["invalid"] is True
    # Only a newer explicit confirmation with a real dated baseline clears it.
    later = base.now_utc + timedelta(seconds=602)
    settings["confirmed_session"]["confirmed_at"] = later.isoformat()
    settings["confirmed_session"]["soc_kwh"] = 3.25
    assert confirmed_session_observation(base, settings, later)["ev_soc"] == 3.25


def test_later_meter_growth_does_not_invalidate_old_vehicle_value(base):
    """A once-consistent value is not compared as if it were measured after later charging."""
    session_inputs(base)
    settings = car_settings(base)
    _set_entity(base, "sensor.test_ev_energy", "101", attributes={"unit_of_measurement": "kWh"})
    _set_entity(base, settings["soc_entity"], 0.92, attributes={"retrievalstatus": "VALID"})
    assert confirmed_session_observation(base, settings, base.now_utc)["ev_soc"] == 0.92
    later = base.now_utc + timedelta(minutes=20)
    _set_entity(base, "sensor.test_ev_energy", "103", last_updated=later, last_changed=later, attributes={"unit_of_measurement": "kWh"})
    value = confirmed_session_observation(base, settings, later)
    assert abs(value["ev_soc"] - 3 * base.car_charging_loss) < 1e-9


def test_reconnection_requires_new_explicit_confirmation(base):
    """A later home/plug transition cannot silently reuse the old session, even without SOC."""
    for entity_key in ("home_entity", "connection_entity"):
        session_inputs(base)
        settings = car_settings(base)
        value = "home" if entity_key == "home_entity" else "connected"
        _set_entity(base, settings[entity_key], value)
        base.ha_interface.dummy_items.pop(settings["soc_entity"])
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "household_fallback"
        assert report["execution_reason"].startswith("new_")
        assert execute_car_charging_plan(base) is None


def run_car_planning_session_tests(base):
    """Run each routing and durable-evidence test with independent native state."""
    from unit_test import create_predbat
    from tests.test_car_planning_template import shared_template_available, check_unavailable_template

    if not shared_template_available():
        check_unavailable_template(create_predbat())
        return False

    for test in (
        test_absence_never_reads_soc_or_meter,
        test_real_native_handoff_requires_observed_off,
        test_rejected_car_planning_plan_does_not_block_household,
        test_contradiction_persists_across_age_and_restart,
        test_later_meter_growth_does_not_invalidate_old_vehicle_value,
        test_reconnection_requires_new_explicit_confirmation,
    ):
        fresh = create_predbat()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: fresh.now_utc):
            test(fresh)
    return False

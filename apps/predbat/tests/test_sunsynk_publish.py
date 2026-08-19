# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk sensor and control entity publishing
# -----------------------------------------------------------------------------

"""Tests for Sunsynk entity publishing and the control-entity round trip."""

from unittest.mock import patch
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local


class PublishingSunsynk(MockSunsynk):
    """MockSunsynk that records dashboard_item calls and serves entity reads back."""

    def __init__(self, **kwargs):
        """Set up the recorder alongside the normal test double."""
        super().__init__(**kwargs)
        self.published = {}

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Record a published entity."""
        self.published[entity_id] = {"state": state, "attributes": attributes or {}}

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back a previously published entity, as Home Assistant would."""
        if entity_id in self.published:
            return self.published[entity_id]["state"]
        return default


def test_entity_names_are_namespaced():
    """Sensor and control entity ids carry the component prefix and the serial."""
    failed = False
    s = PublishingSunsynk()
    if s._sensor_name("INV1", "soc") != "sensor.predbat_sunsynk_inv1_soc":
        print(f"ERROR: sensor name {s._sensor_name('INV1', 'soc')}")
        failed = True
    if s._control_name("number", "INV1", "battery_schedule_reserve") != "number.predbat_sunsynk_inv1_battery_schedule_reserve":
        print(f"ERROR: control name {s._control_name('number', 'INV1', 'battery_schedule_reserve')}")
        failed = True
    assert not failed, "test_entity_names_are_namespaced"


def test_publish_data_emits_telemetry_and_ratings():
    """Telemetry, energy counters and derived ratings all publish with units."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {
        "soc": 62,
        "battery_power": -1500,
        "grid_power": 430,
        "load_power": 900,
        "pv_power": 2100,
        "temperature": 21.5,
        "battery_voltage": 52.3,
        "capacity": 280,
        "chargeVolt": 56.8,
        "maxChargeCurrentLimit": 100,
    }
    s.device_energy["INV1"] = {"pv_today": 9.8, "import_today": 3.2}
    s.device_rated_power["INV1"] = 8000.0
    # batteryLowCap makes battery_reserve_min derivable too, alongside capacity/rate_max/limit.
    s.device_settings["INV1"] = {"batteryLowCap": "15"}
    run_async_local(s.publish_data())
    for leaf in ("soc", "battery_power", "grid_power", "load_power", "pv_power", "temperature", "battery_voltage", "pv_today", "import_today", "battery_capacity", "battery_rate_max", "inverter_limit", "battery_reserve_min"):
        entity = s._sensor_name("INV1", leaf)
        if entity not in s.published:
            print(f"ERROR: {leaf} was not published")
            failed = True
        elif not s.published[entity]["attributes"].get("unit_of_measurement"):
            print(f"ERROR: {leaf} published without a unit")
            failed = True
    if s.published.get(s._sensor_name("INV1", "soc"), {}).get("state") != 62:
        print("ERROR: soc state wrong")
        failed = True
    assert not failed, "test_publish_data_emits_telemetry_and_ratings"


def test_publish_data_omits_underivable_ratings():
    """A rating that cannot be derived is not published as a zero."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    # No chargeVolt, so no pack voltage, so neither capacity nor rate is derivable. No
    # batteryLowCap in device_settings either, so the reserve-min floor is unknown too.
    s.device_values["INV1"] = {"soc": 50, "capacity": 280, "maxChargeCurrentLimit": 100}
    s.device_energy["INV1"] = {}
    run_async_local(s.publish_data())
    for leaf in ("battery_capacity", "battery_rate_max", "inverter_limit", "battery_reserve_min"):
        if s._sensor_name("INV1", leaf) in s.published:
            print(f"ERROR: {leaf} was published despite being underivable")
            failed = True
    assert not failed, "test_publish_data_omits_underivable_ratings"


def test_schedule_entities_round_trip():
    """Control entities publish in the format Predbat expects and read back unchanged."""
    failed = False
    s = PublishingSunsynk()
    s.local_schedule["INV1"] = {
        "reserve": 12,
        "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"},
        "export": {"enable": False, "soc": 20, "power": 2500, "start": "16:00:00", "end": "19:00:00"},
    }
    run_async_local(s.publish_schedule_settings_ha("INV1"))
    # Times must be HH:MM:SS to match INVERTER_DEF charge_time_format; anything else makes
    # Predbat substitute its own dummy entities and the window never reaches the inverter.
    start = s.published[s._control_name("select", "INV1", "battery_schedule_charge_start_time")]["state"]
    if start != "02:00:00" or start.count(":") != 2:
        print(f"ERROR: charge start published as {start!r}, expected HH:MM:SS")
        failed = True
    if s.published[s._control_name("switch", "INV1", "battery_schedule_charge_enable")]["state"] != "on":
        print("ERROR: charge enable should be 'on'")
        failed = True
    if s.published[s._control_name("switch", "INV1", "battery_schedule_export_enable")]["state"] != "off":
        print("ERROR: export enable should be 'off'")
        failed = True
    read_back = run_async_local(s.get_schedule_settings_ha("INV1"))
    if read_back["reserve"] != 12:
        print(f"ERROR: reserve round-tripped to {read_back['reserve']}")
        failed = True
    if read_back["charge"] != {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"}:
        print(f"ERROR: charge round-tripped to {read_back['charge']}")
        failed = True
    assert not failed, "test_schedule_entities_round_trip"


def test_reserve_entity_is_not_clamped_to_the_floor():
    """The reserve entity publishes what Predbat wrote, not the inverter's floor.

    This entity is Predbat's control surface: it writes a value then reads it back to
    confirm, so publishing anything else guarantees a mismatch and a retry storm. The
    floor is enforced at the API boundary in build_settings_payload instead.
    """
    failed = False
    s = PublishingSunsynk()
    s.device_settings["INV1"] = {"batteryLowCap": "20"}
    s.local_schedule["INV1"] = {"reserve": 5, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    run_async_local(s.publish_schedule_settings_ha("INV1"))
    published = s.published[s._control_name("number", "INV1", "battery_schedule_reserve")]["state"]
    if published != 5:
        print(f"ERROR: reserve entity published as {published}, expected the written 5")
        failed = True
    assert not failed, "test_reserve_entity_is_not_clamped_to_the_floor"


def test_sn_from_entity_disambiguates_prefixes():
    """A serial that is a prefix of another never mis-routes a control write."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1", "INV11"]
    cases = [
        (s._control_name("number", "INV1", "battery_schedule_reserve"), "INV1"),
        (s._control_name("number", "INV11", "battery_schedule_reserve"), "INV11"),
        ("number.predbat_sunsynk_unknown_battery_schedule_reserve", None),
    ]
    for entity_id, expect in cases:
        got = s._sn_from_entity(entity_id)
        if got != expect:
            print(f"ERROR: {entity_id} resolved to {got!r}, expected {expect!r}")
            failed = True
    assert not failed, "test_sn_from_entity_disambiguates_prefixes"


def test_control_events_update_the_local_schedule():
    """Select/number/switch events land in local_schedule without writing immediately."""
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.local_schedule["INV1"] = {"reserve": 0, "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    applied = []

    async def fake_apply(sn, force=False):
        """Record schedule applications."""
        applied.append((sn, force))
        return True

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s.select_event(s._control_name("select", "INV1", "battery_schedule_charge_start_time"), "02:30:00"))
        run_async_local(s.number_event(s._control_name("number", "INV1", "battery_schedule_charge_soc"), 88))
        run_async_local(s.switch_event(s._control_name("switch", "INV1", "battery_schedule_charge_enable"), "turn_on"))
    schedule = s.local_schedule["INV1"]
    if schedule["charge"]["start"] != "02:30:00":
        print(f"ERROR: start not updated, got {schedule['charge']['start']}")
        failed = True
    if schedule["charge"]["soc"] != 88:
        print(f"ERROR: soc not updated, got {schedule['charge']['soc']}")
        failed = True
    if schedule["charge"]["enable"] is not True:
        print(f"ERROR: enable not updated, got {schedule['charge']['enable']}")
        failed = True
    assert not failed, "test_control_events_update_the_local_schedule"


def test_write_button_applies_and_is_not_stored_as_schedule():
    """The write button applies the schedule UNFORCED and stores no state of its own.

    Predbat presses this switch on every cycle as its normal "apply the schedule" action
    (INVERTER_DEF time_button_press), not only when the plan actually changed, so
    force=True here would bypass apply_settings' applied-payload change-detection gate on
    every cycle rather than just the cycles that need a write. deye.py hit this exact bug
    first: PR #4371 (commit 3e1de759) measured 40 button presses producing 36
    byte-identical control orders over two hours on a live site once the button forced the
    write. Unforced, apply_settings' applied-payload cache is the single source of truth
    for whether a write is needed - see test_write_button_writes_when_changed_and_suppresses_unchanged_repeats
    below for proof the button still gets a real write through when one is actually due.
    Do not reintroduce force=True on this path.

    Its entity id contains "_charge_", so it must be handled before the direction matching
    in update_local_schedule or it would be mistaken for a schedule field.
    """
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.local_schedule["INV1"] = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    applied = []

    async def fake_apply(sn, force=False):
        """Record schedule applications."""
        applied.append((sn, force))
        return True

    with patch.object(s, "apply_schedule", side_effect=fake_apply):
        run_async_local(s.switch_event(s._control_name("switch", "INV1", "battery_schedule_charge_write"), "turn_on"))
    if applied != [("INV1", False)]:
        print(f"ERROR: expected an unforced apply for INV1, got {applied}")
        failed = True
    if s.local_schedule.get("INV1", {}).get("charge", {}).get("start") != "02:00:00":
        print(f"ERROR: the write button corrupted local_schedule: {s.local_schedule}")
        failed = True
    assert not failed, "test_write_button_applies_and_is_not_stored_as_schedule"


def test_write_button_writes_when_changed_and_suppresses_unchanged_repeats():
    """Unforced does not mean inert: the button still reaches the API through the real gate.

    Drives switch_event -> apply_schedule -> apply_settings with nothing mocked below the
    transport, so this is the actual gate the deye.py regression (PR #4371) was about, not
    a stand-in. The first press has no applied_payload cached yet, so it must perform a
    real settings read-modify-write. The second press, with nothing in the plan changed, is
    exactly Predbat's routine per-cycle button press (INVERTER_DEF time_button_press) and
    must NOT write again - two writes here would be the same 40-presses/36-orders bug.
    """
    failed = False
    s = PublishingSunsynk()
    s.device_list = ["INV1"]
    s.device_values["INV1"] = {"soc": 50}
    s.device_settings["INV1"] = {"batteryLowCap": "10", "sn": "INV1"}
    s.local_schedule["INV1"] = {"reserve": 10, "charge": {"enable": True, "soc": 95, "power": 3000, "start": "02:00:00", "end": "05:00:00"}, "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"}}
    posts = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Serve the settings baseline apply_settings reads before writing."""
        return dict(s.device_settings.get(sn, {})) if endpoint_key == "settings_read" else {}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record a settings write and report success with no data payload."""
        posts.append((endpoint_key, sn))
        return {}

    write_button = s._control_name("switch", "INV1", "battery_schedule_charge_write")
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post):
        run_async_local(s.switch_event(write_button, "turn_on"))
        run_async_local(s.switch_event(write_button, "turn_on"))
    if posts != [("settings_set", "INV1")]:
        print(f"ERROR: expected exactly one settings_set write for INV1, got {posts}")
        failed = True
    assert not failed, "test_write_button_writes_when_changed_and_suppresses_unchanged_repeats"


def run_sunsynk_publish_tests(my_predbat):
    """Run all Sunsynk publishing tests."""
    failed = False
    for name, fn in [
        ("entity_names", test_entity_names_are_namespaced),
        ("publish_telemetry", test_publish_data_emits_telemetry_and_ratings),
        ("publish_omits_underivable", test_publish_data_omits_underivable_ratings),
        ("schedule_round_trip", test_schedule_entities_round_trip),
        ("reserve_not_clamped", test_reserve_entity_is_not_clamped_to_the_floor),
        ("sn_from_entity", test_sn_from_entity_disambiguates_prefixes),
        ("control_events", test_control_events_update_the_local_schedule),
        ("write_button_unforced", test_write_button_applies_and_is_not_stored_as_schedule),
        ("write_button_gate", test_write_button_writes_when_changed_and_suppresses_unchanged_repeats),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

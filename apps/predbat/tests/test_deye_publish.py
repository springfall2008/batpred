# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE Cloud publish sensors and schedule control entities
# -----------------------------------------------------------------------------

"""Tests for DEYE publish sensors and schedule control entities (``deye.py``)."""

from unittest.mock import patch

from tests.test_deye_api import MockDeye


class RecordingDeye(MockDeye):
    """MockDeye that records dashboard_item calls and serves entity states."""

    def __init__(self, **kw):
        """Set up a RecordingDeye with empty published/entity_states stores."""
        super().__init__(**kw)
        self.published = {}
        self.entity_states = {}

    def dashboard_item(self, entity, state=None, attributes=None, app=None):
        """Record a published entity."""
        self.published[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Serve a canned entity state."""
        return self.entity_states.get(entity_id, default)


def test_publish_data_creates_soc_sensor():
    """publish_data emits a SoC sensor for each known inverter."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 63.0, "battery_power": 100.0, "grid_power": 0.0, "pv_power": 500.0, "load_power": 400.0, "temperature": 21.0}}
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    if "sensor.predbat_deye_inv1_soc" not in d.published:
        print(f"ERROR: soc sensor not published; got {list(d.published)[:5]}")
        failed = True
    elif d.published["sensor.predbat_deye_inv1_soc"] != 63.0:
        print("ERROR: soc value wrong")
        failed = True
    assert not failed, "test_publish_data_creates_soc_sensor"


def test_schedule_roundtrip():
    """Published control entities read back into the schedule shape used by control derivation."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.local_schedule = {"INV1": {"reserve": 10, "charge": {"enable": True, "soc": 90, "power": 3000, "start": "02:00", "end": "05:00"}, "export": {"enable": False, "soc": 99, "power": 0}}}
    import tests.test_infra as ti

    ti.run_async(d.publish_schedule_settings_ha("INV1"))
    # Feed the published states back as HA state, then read them.
    for entity, state in list(d.published.items()):
        d.entity_states[entity] = "on" if state is True else ("off" if state is False else state)
    got = ti.run_async(d.get_schedule_settings_ha("INV1"))
    if got.get("charge", {}).get("soc") != 90:
        print(f"ERROR: charge soc round-trip {got.get('charge')}")
        failed = True
    if got.get("reserve") != 10:
        print(f"ERROR: reserve round-trip {got.get('reserve')}")
        failed = True
    assert not failed, "test_schedule_roundtrip"


def test_get_schedule_settings_ha_survives_unavailable():
    """Numeric reads fall back to 0 when HA reports 'unavailable' instead of raising."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    # Simulate a HA restart: number entities report the string "unavailable" before republish.
    d.entity_states = {
        "number.predbat_deye_inv1_battery_schedule_reserve": "unavailable",
        "number.predbat_deye_inv1_battery_schedule_charge_soc": "unavailable",
    }
    import tests.test_infra as ti

    try:
        got = ti.run_async(d.get_schedule_settings_ha("INV1"))
    except (ValueError, TypeError) as error:
        print(f"ERROR: get_schedule_settings_ha raised on unavailable state: {error}")
        return True
    if got.get("reserve") != 0 or not isinstance(got.get("reserve"), int):
        print(f"ERROR: reserve did not fall back to int 0: {got.get('reserve')!r}")
        failed = True
    charge_soc = got.get("charge", {}).get("soc")
    if charge_soc != 0 or not isinstance(charge_soc, int):
        print(f"ERROR: charge soc did not fall back to int 0: {charge_soc!r}")
        failed = True
    assert not failed, "test_get_schedule_settings_ha_survives_unavailable"


def test_reserve_event_writes_immediately():
    """A reserve number_event pushes to DEYE at once (freeze-charge relies on it)."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    calls = []

    async def fake_apply_reserve(sn, reserve):
        """Record the (sn, reserve) pair the live-reserve path was called with."""
        calls.append((sn, reserve))
        return True

    import tests.test_infra as ti

    with patch.object(d, "apply_reserve_live", side_effect=fake_apply_reserve):
        ti.run_async(d.number_event("number.predbat_deye_inv1_battery_schedule_reserve", 25))
    if calls != [("INV1", 25)]:
        print(f"ERROR: reserve not written immediately: {calls}")
        failed = True
    assert not failed, "test_reserve_event_writes_immediately"


def test_write_button_applies_schedule():
    """The write switch triggers a forced schedule apply."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    applied = []

    async def fake_apply(sn, force=True):
        """Record the (sn, force) pair the schedule-apply path was called with."""
        applied.append((sn, force))
        return True

    import tests.test_infra as ti

    with patch.object(d, "apply_schedule", side_effect=fake_apply):
        ti.run_async(d.switch_event("switch.predbat_deye_inv1_battery_schedule_charge_write", "turn_on"))
    if applied != [("INV1", True)]:
        print(f"ERROR: write button did not apply: {applied}")
        failed = True
    assert not failed, "test_write_button_applies_schedule"


def test_sn_from_entity_disambiguates_prefix_colliding_serials():
    """_sn_from_entity resolves prefix-colliding serials (INV1 vs INV11) to the correct inverter."""
    failed = False
    d = RecordingDeye()
    # INV1 is a literal prefix of INV11: a bare startswith would mis-route INV11 events to INV1.
    d.device_list = ["INV1", "INV11"]
    got_11 = d._sn_from_entity("number.predbat_deye_inv11_battery_schedule_reserve")
    if got_11 != "INV11":
        print(f"ERROR: inv11 entity mis-routed to: {got_11!r}")
        failed = True
    got_1 = d._sn_from_entity("number.predbat_deye_inv1_battery_schedule_reserve")
    if got_1 != "INV1":
        print(f"ERROR: inv1 entity mis-routed to: {got_1!r}")
        failed = True
    assert not failed, "test_sn_from_entity_disambiguates_prefix_colliding_serials"


def test_automatic_config_maps_all_inverters():
    """automatic_config registers each inverter and maps the core control args."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INVA", "INVB"]
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.automatic_config())
    if d.set_args.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {d.set_args.get('num_inverters')}")
        failed = True
    if d.set_args.get("inverter_type") != ["DeyeCloud", "DeyeCloud"]:
        print(f"ERROR: inverter_type {d.set_args.get('inverter_type')}")
        failed = True
    cs = d.set_args.get("charge_start_time")
    if not cs or cs[0] != "select.predbat_deye_inva_battery_schedule_charge_start_time":
        print(f"ERROR: charge_start_time map {cs}")
        failed = True
    if "inverter_mode" in d.set_args:
        print("ERROR: DEYE must not set inverter_mode (mode-less)")
        failed = True
    assert not failed, "test_automatic_config_maps_all_inverters"


def test_apply_reserve_live_forces_write_despite_noop():
    """apply_reserve_live posts a dynamic_control even when the payload is unchanged (force=True bypass)."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    d.local_schedule = {"INV1": {"reserve": 25, "charge": {"enable": False}, "export": {"enable": False}}}
    # Pre-seed the applied-payload cache with EXACTLY the payload this call will build, so a
    # non-forced write would be suppressed as a no-op. force=True must override that suppression.
    d.applied_payload = {"INV1": d.build_dynamic_payload("INV1", d.local_schedule["INV1"], 50.0)}
    posts = []

    async def fake_post(endpoint_key, body):
        """Record each DEYE POST and report success."""
        posts.append((endpoint_key, body))
        return {"success": True}

    import tests.test_infra as ti

    with patch.object(d, "_post", side_effect=fake_post):
        wrote = ti.run_async(d.apply_reserve_live("INV1", 25))
    if not wrote:
        print(f"ERROR: apply_reserve_live did not write (no-op suppression not bypassed): {wrote}")
        failed = True
    if not any(endpoint == "dynamic_control" for endpoint, _ in posts):
        print(f"ERROR: no dynamic_control post despite forced write: {posts}")
        failed = True
    assert not failed, "test_apply_reserve_live_forces_write_despite_noop"


def run_deye_publish_tests(my_predbat):
    """Run all DEYE publish/config tests."""
    failed = False
    for name, fn in [
        ("publish_data_soc", test_publish_data_creates_soc_sensor),
        ("schedule_roundtrip", test_schedule_roundtrip),
        ("schedule_unavailable", test_get_schedule_settings_ha_survives_unavailable),
        ("reserve_event_immediate", test_reserve_event_writes_immediately),
        ("write_button", test_write_button_applies_schedule),
        ("sn_from_entity", test_sn_from_entity_disambiguates_prefix_colliding_serials),
        ("automatic_config", test_automatic_config_maps_all_inverters),
        ("apply_reserve_live_force", test_apply_reserve_live_forces_write_despite_noop),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

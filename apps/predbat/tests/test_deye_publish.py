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


def test_publish_data_publishes_battery_capacity():
    """publish_data emits battery-capability sensors (capacity/reserve_min/currents) from config_battery."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    # battCapacity is Ah: 200Ah on a 51.2V nominal pack is 10.24 kWh
    d.device_battery_config = {"INV1": {"battCapacity": 200, "battLowCapacity": 10, "maxChargeCurrent": 50, "maxDischargeCurrent": 50}}
    d.device_pack_voltage = {"INV1": 51.2}
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    if d.published.get("sensor.predbat_deye_inv1_battery_capacity") != 10.24:
        print(f"ERROR: battery_capacity not published correctly; got {d.published.get('sensor.predbat_deye_inv1_battery_capacity')!r}")
        failed = True
    if d.published.get("sensor.predbat_deye_inv1_battery_reserve_min") != 10.0:
        print(f"ERROR: battery_reserve_min not published correctly; got {d.published.get('sensor.predbat_deye_inv1_battery_reserve_min')!r}")
        failed = True
    if d.published.get("sensor.predbat_deye_inv1_max_charge_current") != 50.0:
        print(f"ERROR: max_charge_current not published correctly; got {d.published.get('sensor.predbat_deye_inv1_max_charge_current')!r}")
        failed = True
    if d.published.get("sensor.predbat_deye_inv1_max_discharge_current") != 50.0:
        print(f"ERROR: max_discharge_current not published correctly; got {d.published.get('sensor.predbat_deye_inv1_max_discharge_current')!r}")
        failed = True
    assert not failed, "test_publish_data_publishes_battery_capacity"


def test_publish_data_skips_battery_capacity_when_config_missing():
    """publish_data does not emit battery-capability sensors for an inverter with no cached config_battery."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 63.0}}
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    if "sensor.predbat_deye_inv1_battery_capacity" in d.published:
        print(f"ERROR: battery_capacity published despite missing config_battery: {d.published}")
        failed = True
    assert not failed, "test_publish_data_skips_battery_capacity_when_config_missing"


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
    # Both capability sensors have a source, so both args are mapped
    d.device_capacity = {"INVA": 61.44, "INVB": 61.44}
    d.device_battery_config = {"INVA": {}, "INVB": {}}
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
    soc_max = d.set_args.get("soc_max")
    if not soc_max or soc_max[0] != "sensor.predbat_deye_inva_battery_capacity":
        print(f"ERROR: soc_max map {soc_max}")
        failed = True
    battery_min_soc = d.set_args.get("battery_min_soc")
    if not battery_min_soc or battery_min_soc[0] != "sensor.predbat_deye_inva_battery_reserve_min":
        print(f"ERROR: battery_min_soc map {battery_min_soc}")
        failed = True
    if "inverter_mode" in d.set_args:
        print("ERROR: DEYE must not set inverter_mode (mode-less)")
        failed = True
    assert not failed, "test_automatic_config_maps_all_inverters"


def test_sign_flags_are_claimed_not_inherited():
    """DEYE owns the invert flags, so another component cannot flip its sign.

    base.args is shared and NOT namespaced per inverter type. teslemetry and fox both set
    grid_power_invert True for their own hardware, quite correctly - but that leaves the key
    set for every inverter index, and a DEYE inverter that never claimed it inherited the
    flip. inverter.py then negated an already-correct sensor, so an export read as an import
    and the power-flow arrow pointed the wrong way on any install running both.

    All three are False because publish_data already emits Predbat's conventions: grid
    negative on import (DEYE_TELEMETRY_NEGATE), battery positive on discharge, load positive.
    """
    failed = False
    d = RecordingDeye()
    d.device_list = ["INVA", "INVB"]
    d.device_capacity = {"INVA": 61.44, "INVB": 61.44}
    d.device_battery_config = {"INVA": {}, "INVB": {}}
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.automatic_config())
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        if flag not in d.set_args:
            print(f"ERROR: {flag} was never set, so it is inherited from whatever else configured the install")
            failed = True
        elif d.set_args[flag] != [False, False]:
            print(f"ERROR: {flag} = {d.set_args[flag]}, expected [False, False] - one entry per inverter")
            failed = True
    assert not failed, "test_sign_flags_are_claimed_not_inherited"


def test_automatic_config_skips_unavailable_capability_args():
    """With no capacity source, soc_max/battery_min_soc are left unset rather than aimed at missing sensors."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INVA"]
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.automatic_config())
    if "soc_max" in d.set_args:
        print(f"ERROR: soc_max should be unset without a capacity source, got {d.set_args['soc_max']}")
        failed = True
    if "battery_min_soc" in d.set_args:
        print(f"ERROR: battery_min_soc should be unset without config/battery, got {d.set_args['battery_min_soc']}")
        failed = True
    if not any("soc_max must be set manually" in m for m in d.log_messages):
        print(f"ERROR: expected a warning telling the user to set soc_max, got {d.log_messages}")
        failed = True
    # The rest of the mapping still happens - one missing capability must not block control
    if d.set_args.get("num_inverters") != 1:
        print(f"ERROR: num_inverters {d.set_args.get('num_inverters')}")
        failed = True
    if not d.set_args.get("charge_start_time"):
        print("ERROR: control args should still be mapped")
        failed = True
    assert not failed, "test_automatic_config_skips_unavailable_capability_args"


def test_automatic_config_maps_ratings():
    """battery_rate_max and inverter_limit are published in watts and mapped."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 100.0}}
    d.device_pack_voltage = {"INV1": 51.2}
    d.device_battery_config = {"INV1": {"maxChargeCurrent": 185, "maxDischargeCurrent": 185, "battCapacity": 1200, "battLowCapacity": 14}}
    d.device_rated_power = {"INV1": 8000.0}
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    ti.run_async(d.automatic_config())

    if d.published.get("sensor.predbat_deye_inv1_battery_rate_max") != 9472:
        print(f"ERROR: battery_rate_max published as {d.published.get('sensor.predbat_deye_inv1_battery_rate_max')}, expected 9472")
        failed = True
    if d.published.get("sensor.predbat_deye_inv1_inverter_limit") != 8000.0:
        print(f"ERROR: inverter_limit published as {d.published.get('sensor.predbat_deye_inv1_inverter_limit')}, expected 8000.0")
        failed = True
    if d.set_args.get("battery_rate_max") != ["sensor.predbat_deye_inv1_battery_rate_max"]:
        print(f"ERROR: battery_rate_max arg {d.set_args.get('battery_rate_max')}")
        failed = True
    if d.set_args.get("inverter_limit") != ["sensor.predbat_deye_inv1_inverter_limit"]:
        print(f"ERROR: inverter_limit arg {d.set_args.get('inverter_limit')}")
        failed = True
    assert not failed, "test_automatic_config_maps_ratings"


def test_automatic_config_skips_missing_ratings():
    """Without a rating source the args are left unset rather than aimed at missing sensors."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    ti.run_async(d.automatic_config())

    for leaf in ("battery_rate_max", "inverter_limit"):
        if leaf in d.set_args:
            print(f"ERROR: {leaf} should be unset, got {d.set_args[leaf]}")
            failed = True
        if f"sensor.predbat_deye_inv1_{leaf}" in d.published:
            print(f"ERROR: {leaf} sensor should not be published without a source")
            failed = True
    if not any("battery_rate_max must be set manually" in m for m in d.log_messages):
        print(f"ERROR: expected a battery_rate_max warning, got {d.log_messages}")
        failed = True
    if not any("inverter_limit must be set manually" in m for m in d.log_messages):
        print(f"ERROR: expected an inverter_limit warning, got {d.log_messages}")
        failed = True
    assert not failed, "test_automatic_config_skips_missing_ratings"


def test_automatic_config_maps_energy_counters():
    """The energy counters are published and mapped to Predbat's history args."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 100.0}}
    # Daily-register magnitudes, matching DEYE_ENERGY_KEYS' Daily* sources.
    d.device_energy = {"INV1": {"import_today": 7.0, "export_today": 0.0, "pv_today": 4.5, "load_today": 12.8}}
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    ti.run_async(d.automatic_config())

    for leaf, value in (("import_today", 7.0), ("export_today", 0.0), ("pv_today", 4.5), ("load_today", 12.8)):
        entity = f"sensor.predbat_deye_inv1_{leaf}"
        if d.published.get(entity) != value:
            print(f"ERROR: {entity} published as {d.published.get(entity)}, expected {value}")
            failed = True
        if d.set_args.get(leaf) != [entity]:
            print(f"ERROR: {leaf} arg mapped to {d.set_args.get(leaf)}")
            failed = True
    assert not failed, "test_automatic_config_maps_energy_counters"


def test_automatic_config_skips_missing_energy_counters():
    """An inverter that reports no energy counters leaves those args unset."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_energy = {"INV1": {"import_today": 100.0}}
    d.set_args = {}
    d.set_arg = lambda k, v: d.set_args.__setitem__(k, v)
    import tests.test_infra as ti

    ti.run_async(d.automatic_config())
    if d.set_args.get("import_today") != ["sensor.predbat_deye_inv1_import_today"]:
        print(f"ERROR: import_today should still be mapped, got {d.set_args.get('import_today')}")
        failed = True
    for leaf in ("export_today", "pv_today", "load_today"):
        if leaf in d.set_args:
            print(f"ERROR: {leaf} has no source and must not be mapped, got {d.set_args[leaf]}")
            failed = True
    assert not failed, "test_automatic_config_skips_missing_energy_counters"


def test_publish_data_publishes_derived_capacity():
    """The derived capacity is published as the battery_capacity sensor without config/battery."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 94.0}}
    d.device_capacity = {"INV1": 61.44}
    import tests.test_infra as ti

    ti.run_async(d.publish_data())
    published = d.published
    capacity = published.get("sensor.predbat_deye_inv1_battery_capacity")
    if capacity != 61.44:
        print(f"ERROR: expected 61.44 kWh published, got {capacity}")
        failed = True
    if "sensor.predbat_deye_inv1_battery_reserve_min" in published:
        print("ERROR: reserve_min has no source and must not be published")
        failed = True
    assert not failed, "test_publish_data_publishes_derived_capacity"


def test_apply_reserve_live_forces_write_despite_noop():
    """apply_reserve_live posts a dynamic_control even when the payload is unchanged (force=True bypass)."""
    failed = False
    d = RecordingDeye().with_rating("INV1")
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


def test_control_entity_writes_are_republished():
    """Every control entity must be republished with the value Predbat wrote.

    Regression: the event handlers discarded the incoming value and re-read the entity
    instead, so nothing ever changed the published state. Predbat writes with
    number/set_value, select/select_option or switch/turn_*, then reads the entity back to
    confirm (inverter.write_and_poll_value / write_and_poll_switch), so every write was
    reported failed: "Trying to write 9472 to charge_rate didn't complete got 0.0".
    """
    failed = False
    import tests.test_infra as ti

    cases = [
        ("number", "battery_schedule_charge_power", 9472, 9472, ti.run_async, "number"),
        ("number", "battery_schedule_charge_soc", 100, 100, ti.run_async, "number"),
        ("number", "battery_schedule_export_power", 3000, 3000, ti.run_async, "number"),
        ("number", "battery_schedule_export_soc", 20, 20, ti.run_async, "number"),
        ("select", "battery_schedule_charge_start_time", "01:30", "01:30", ti.run_async, "select"),
        ("select", "battery_schedule_charge_end_time", "05:00", "05:00", ti.run_async, "select"),
        ("select", "battery_schedule_export_start_time", "16:00", "16:00", ti.run_async, "select"),
    ]
    for domain, leaf, written, expected, runner, kind in cases:
        d = RecordingDeye()
        d.device_list = ["INV1"]
        entity = f"{domain}.predbat_deye_inv1_{leaf}"
        if kind == "number":
            runner(d.number_event(entity, written))
        else:
            runner(d.select_event(entity, written))
        got = d.published.get(entity)
        if got != expected:
            print(f"ERROR: {leaf} published as {got!r}, expected {expected!r}")
            failed = True

    # Switch enable, written by write_and_poll_switch as turn_on / turn_off
    for service, expected_state in (("turn_on", "on"), ("turn_off", "off")):
        d = RecordingDeye()
        d.device_list = ["INV1"]
        entity = "switch.predbat_deye_inv1_battery_schedule_charge_enable"
        ti.run_async(d.switch_event(entity, service))
        if d.published.get(entity) != expected_state:
            print(f"ERROR: enable {service} published as {d.published.get(entity)!r}, expected {expected_state!r}")
            failed = True
    assert not failed, "test_control_entity_writes_are_republished"


def test_write_button_applies_and_is_not_stored_as_schedule():
    """The write button is momentary: it applies the schedule and stores no state.

    Its entity id contains "_charge_", so it must be handled before the direction matching
    in update_local_schedule or it would be mistaken for a schedule field.
    """
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    applied = []

    async def fake_apply(sn, force=False):
        """Record an apply_schedule call."""
        applied.append((sn, force))

    import tests.test_infra as ti

    with patch.object(d, "apply_schedule", side_effect=fake_apply):
        ti.run_async(d.switch_event("switch.predbat_deye_inv1_battery_schedule_charge_write", "turn_on"))
    # Unforced: Predbat presses this every cycle, so forcing would re-send an unchanged
    # payload each time. Change detection decides whether a write is actually needed.
    if applied != [("INV1", False)]:
        print(f"ERROR: expected an unforced apply, got {applied}")
        failed = True
    if d.local_schedule.get("INV1", {}).get("charge", {}):
        print(f"ERROR: the write button must not be stored as schedule state: {d.local_schedule}")
        failed = True
    assert not failed, "test_write_button_applies_and_is_not_stored_as_schedule"


def test_reserve_entity_echoes_the_written_value_even_below_the_floor():
    """The reserve entity must echo exactly what Predbat wrote, floor or no floor.

    Predbat writes this entity then reads it back to confirm (write_and_poll_value), so
    republishing a clamped value can never match what was written and would retry until it
    gave up — the same "didn't complete got 0.0" failure this component already had. The
    floor is enforced at the API boundary in build_dynamic_payload instead, which is what
    actually protects the battery.
    """
    failed = False
    d = RecordingDeye().with_rating("INV1")
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    d.device_battery_config = {"INV1": {"battLowCapacity": 14}}
    entity = "number.predbat_deye_inv1_battery_schedule_reserve"
    import tests.test_infra as ti

    async def fake_apply(sn, schedule, current_soc, force=False):
        """Stand in for the live control write."""
        return True

    # A below-floor write is echoed verbatim so the read-back matches
    with patch.object(d, "apply_dynamic_control", side_effect=fake_apply):
        ti.run_async(d.number_event(entity, 4))
    if d.published.get(entity) != 4:
        print(f"ERROR: expected the entity to echo 4, got {d.published.get(entity)!r}")
        failed = True

    # ...but the payload that reaches the inverter is still lifted to the floor
    socs = [s["soc"] for s in d.build_dynamic_payload("INV1", d.local_schedule["INV1"], 50)["timeUseSettingItems"]]
    if any(s < 14 for s in socs):
        print(f"ERROR: the payload must still respect the 14% floor: {socs}")
        failed = True
    assert not failed, "test_reserve_entity_echoes_the_written_value_even_below_the_floor"


def test_reserve_write_is_republished():
    """A reserve change is pushed to the inverter and republished for the read-back."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    d.device_values = {"INV1": {"soc": 50.0}}
    import tests.test_infra as ti

    async def fake_apply(sn, schedule, current_soc, force=False):
        """Stand in for the live control write."""
        return True

    entity = "number.predbat_deye_inv1_battery_schedule_reserve"
    with patch.object(d, "apply_dynamic_control", side_effect=fake_apply):
        ti.run_async(d.number_event(entity, 14))
    if d.published.get(entity) != 14:
        print(f"ERROR: reserve published as {d.published.get(entity)!r}, expected 14")
        failed = True
    if d.local_schedule.get("INV1", {}).get("reserve") != 14:
        print(f"ERROR: reserve not stored: {d.local_schedule}")
        failed = True
    assert not failed, "test_reserve_write_is_republished"


def test_unrelated_entity_does_not_corrupt_schedule():
    """An entity that matches no schedule field falls back to a plain state refresh."""
    failed = False
    d = RecordingDeye()
    d.device_list = ["INV1"]
    if d.update_local_schedule("INV1", "number.predbat_deye_inv1_something_else", 5):
        print("ERROR: an unknown entity must not be applied")
        failed = True
    if d.update_local_schedule("INV1", "number.predbat_deye_inv1_battery_schedule_charge_mystery", 5):
        print("ERROR: an unknown charge field must not be applied")
        failed = True
    assert not failed, "test_unrelated_entity_does_not_corrupt_schedule"


def run_deye_publish_tests(my_predbat):
    """Run all DEYE publish/config tests."""
    failed = False
    for name, fn in [
        ("publish_data_soc", test_publish_data_creates_soc_sensor),
        ("publish_data_battery_capacity", test_publish_data_publishes_battery_capacity),
        ("publish_data_battery_capacity_missing", test_publish_data_skips_battery_capacity_when_config_missing),
        ("schedule_roundtrip", test_schedule_roundtrip),
        ("schedule_unavailable", test_get_schedule_settings_ha_survives_unavailable),
        ("reserve_event_immediate", test_reserve_event_writes_immediately),
        ("write_button", test_write_button_applies_schedule),
        ("sn_from_entity", test_sn_from_entity_disambiguates_prefix_colliding_serials),
        ("automatic_config", test_automatic_config_maps_all_inverters),
        ("sign_flags_claimed", test_sign_flags_are_claimed_not_inherited),
        ("automatic_config_skips_capability", test_automatic_config_skips_unavailable_capability_args),
        ("publish_derived_capacity", test_publish_data_publishes_derived_capacity),
        ("automatic_config_ratings", test_automatic_config_maps_ratings),
        ("automatic_config_ratings_missing", test_automatic_config_skips_missing_ratings),
        ("automatic_config_energy", test_automatic_config_maps_energy_counters),
        ("automatic_config_energy_missing", test_automatic_config_skips_missing_energy_counters),
        ("apply_reserve_live_force", test_apply_reserve_live_forces_write_despite_noop),
        ("control_writes_republished", test_control_entity_writes_are_republished),
        ("write_button_not_stored", test_write_button_applies_and_is_not_stored_as_schedule),
        ("reserve_republished", test_reserve_write_is_republished),
        ("reserve_echoes_written", test_reserve_entity_echoes_the_written_value_even_below_the_floor),
        ("unknown_entity_ignored", test_unrelated_entity_does_not_corrupt_schedule),
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

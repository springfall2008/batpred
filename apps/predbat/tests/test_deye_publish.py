# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE Cloud publish sensors and schedule control entities
# -----------------------------------------------------------------------------

"""Tests for DEYE publish sensors and schedule control entities (``deye.py``)."""

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

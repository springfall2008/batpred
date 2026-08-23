# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the myenergi Zappi and Eddi integration
"""

import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async

from myenergi import (
    DEVICE_KIND_EDDI,
    DEVICE_KIND_ZAPPI,
    MyEnergiTransport,
    normalise_cloud_device,
    normalise_direct_device,
)

# ============================================================================
# Mock data constants
# ============================================================================

# One entry from the "zappi" group of a direct /cgi-jstatus-* response
MOCK_DIRECT_ZAPPI = {
    "sno": 12345678,
    "sta": 3,
    "zmo": 2,
    "pst": "C2",
    "div": 7360,
    "che": 4.25,
    "grd": 120,
    "gen": 3400,
    "vol": 2405,
    "frq": 50.02,
}

# One entry from the "eddi" group of a direct /cgi-jstatus-* response
MOCK_DIRECT_EDDI = {
    "sno": 87654321,
    "sta": 3,
    "div": 1500,
    "che": 2.5,
    "grd": -40,
    "gen": 3400,
    "vol": 2401,
    "bsm": 0,
    "rbt": 0,
    "tp1": 54,
    "tp2": 127,
    "hno": 1,
}

# GET /devices/{id}/status for the same Zappi, plus its GET /devices metadata
MOCK_CLOUD_ZAPPI_STATUS = {
    "deviceClass": "ZAPPI",
    "status": "active",
    "state": "charging",
    "deviceStatus": "charging",
    "supplyMode": "eco",
    "pilotState": "C2",
    "boostCharge": False,
    "actualPower": 7.36,
    "gridPower": 0.12,
    "genPower": 3.4,
    "sessionEnergy": 4.25,
    "energyDelivered": 0.12,
}

MOCK_CLOUD_ZAPPI_META = {
    "deviceId": "ZA12345678",
    "model": "zappi",
    "alias": "Driveway",
    "serialNumber": 12345678,
    "online": True,
}

MOCK_CLOUD_EDDI_STATUS = {
    "deviceClass": "EDDI",
    "status": "active",
    "state": "waiting_for_surplus",
    "deviceStatus": "diverting",
    "boostActive": False,
    "actualPower": 1.5,
    "gridPower": -0.04,
    "genPower": 3.4,
    "sessionEnergy": 2.5,
}

MOCK_CLOUD_EDDI_META = {
    "deviceId": "ED87654321",
    "model": "eddi",
    "alias": "Hot water",
    "serialNumber": 87654321,
    "online": True,
}


def test_normalise_direct_zappi():
    """Direct Zappi payloads normalise into the shared device model."""
    device = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    assert device.device_id == "Z12345678"
    assert device.kind == DEVICE_KIND_ZAPPI
    assert device.serial == "12345678"
    assert device.status == "Charging"
    assert device.mode == "Eco"
    assert device.plug_status == "Charging"
    assert device.power_w == 7360
    assert device.grid_power_w == 120
    assert device.generation_w == 3400
    assert device.voltage == 240.5
    assert device.session_energy_kwh == 4.25
    assert device.boost_active is False
    assert device.temp_1 is None
    print("  ✓ Direct Zappi normalisation")


def test_normalise_direct_eddi():
    """Direct Eddi payloads normalise, including probe temperature handling."""
    device = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    assert device.device_id == "E87654321"
    assert device.kind == DEVICE_KIND_EDDI
    assert device.status == "Diverting"
    assert device.power_w == 1500
    assert device.session_energy_kwh == 2.5
    assert device.boost_active is False
    assert device.plug_status == ""
    assert device.temp_1 == 54
    # 127 is myenergi's "probe not connected" sentinel and must not be published
    assert device.temp_2 is None
    print("  ✓ Direct Eddi normalisation")


def test_normalise_direct_eddi_boosting():
    """An Eddi mid-boost reports boost_active and remaining minutes."""
    raw = dict(MOCK_DIRECT_EDDI, sta=4, bsm=1, rbt=1800)
    device = normalise_direct_device(raw, DEVICE_KIND_EDDI)
    assert device.status == "Boosting"
    assert device.boost_active is True
    assert device.boost_remaining_mins == 30
    print("  ✓ Direct Eddi boost state")


def test_normalise_cloud_matches_direct():
    """Cloud and direct payloads for the same device produce equal values."""
    direct_zappi = normalise_direct_device(MOCK_DIRECT_ZAPPI, DEVICE_KIND_ZAPPI)
    cloud_zappi = normalise_cloud_device(MOCK_CLOUD_ZAPPI_STATUS, MOCK_CLOUD_ZAPPI_META)
    assert cloud_zappi.kind == direct_zappi.kind
    assert cloud_zappi.serial == direct_zappi.serial
    assert cloud_zappi.status == direct_zappi.status
    assert cloud_zappi.mode == direct_zappi.mode
    assert cloud_zappi.plug_status == direct_zappi.plug_status
    # Cloud reports kW, direct reports W - both land in W
    assert cloud_zappi.power_w == direct_zappi.power_w
    assert cloud_zappi.generation_w == direct_zappi.generation_w
    assert cloud_zappi.session_energy_kwh == direct_zappi.session_energy_kwh
    # The cloud device id keeps its two letter prefix and the friendly alias is used
    assert cloud_zappi.device_id == "ZA12345678"
    assert cloud_zappi.name == "Driveway"

    direct_eddi = normalise_direct_device(MOCK_DIRECT_EDDI, DEVICE_KIND_EDDI)
    cloud_eddi = normalise_cloud_device(MOCK_CLOUD_EDDI_STATUS, MOCK_CLOUD_EDDI_META)
    assert cloud_eddi.kind == direct_eddi.kind
    assert cloud_eddi.status == direct_eddi.status
    assert cloud_eddi.power_w == direct_eddi.power_w
    assert cloud_eddi.session_energy_kwh == direct_eddi.session_energy_kwh
    print("  ✓ Cloud and direct normalisation agree")


def test_normalise_handles_bad_values():
    """Out of range indices and missing fields fall back rather than raising."""
    device = normalise_direct_device({"sno": 1, "sta": 99, "zmo": "x"}, DEVICE_KIND_ZAPPI)
    assert device.status == "Unknown"
    assert device.mode == "Unknown"
    assert device.power_w == 0
    assert device.session_energy_kwh == 0

    device = normalise_direct_device({}, DEVICE_KIND_EDDI)
    assert device.serial == ""
    assert device.temp_1 is None

    device = normalise_cloud_device({"deviceClass": "EDDI"}, {})
    assert device.kind == DEVICE_KIND_EDDI
    assert device.power_w == 0
    print("  ✓ Malformed payloads degrade safely")


class _StubTransport(MyEnergiTransport):
    """Minimal concrete transport used to exercise the abstract base's stubs."""

    async def connect(self):
        """Pretend to connect."""
        return True

    async def fetch_devices(self):
        """Return no devices."""
        return []

    async def send_boost(self, device, amount, target_time=None):
        """Pretend to send a boost."""
        return True

    async def cancel_boost(self, device):
        """Pretend to cancel a boost."""
        return True


def test_transport_stubs():
    """Every unimplemented control returns False and warns exactly once."""
    messages = []
    transport = _StubTransport(messages.append)

    assert run_async(transport.set_mode(None, "Eco")) is False
    assert run_async(transport.set_priority(None, 1)) is False
    assert run_async(transport.set_min_green_level(None, 50)) is False
    assert run_async(transport.set_phase_setting(None, "1")) is False
    assert run_async(transport.get_schedule(None)) is False
    assert run_async(transport.set_schedule(None, [])) is False

    assert len(messages) == 6, "Each stub should warn once, got {}".format(messages)
    assert all("not implemented" in message for message in messages)

    # A second call must not warn again
    assert run_async(transport.set_mode(None, "Eco")) is False
    assert len(messages) == 6, "Repeat calls must not warn again"
    print("  ✓ Stubbed controls warn once and return False")


def test_myenergi(my_predbat=None):
    """
    ======================================================================
    MYENERGI TEST SUITE
    ======================================================================
    Comprehensive test suite for the myenergi Zappi and Eddi integration including:
    - Payload normalisation for both transports
    """
    print("\n" + "=" * 70)
    print("MYENERGI TEST SUITE")
    print("=" * 70)

    test_normalise_direct_zappi()
    test_normalise_direct_eddi()
    test_normalise_direct_eddi_boosting()
    test_normalise_cloud_matches_direct()
    test_normalise_handles_bad_values()
    test_transport_stubs()

    print("=" * 70)
    return False

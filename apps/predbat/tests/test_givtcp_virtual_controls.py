import unittest
from unittest.mock import MagicMock

from ha import HAInterface


class _FakeBase:
    def __init__(self):
        self.dashboard_index_app = {
            "select.predbat_givtcp_0_discharge_end_time": "givtcp",
            "number.predbat_givtcp_0_reserve": "givtcp",
            "switch.predbat_givtcp_0_scheduled_charge_enable": "givtcp",
        }
        self.callbacks = []

    async def trigger_callback(self, service_data):
        self.callbacks.append(service_data)
        return True


def _make_ha_interface():
    ha = HAInterface.__new__(HAInterface)
    ha.base = _FakeBase()
    ha.websocket_active = True
    ha.call_service_websocket_command = MagicMock(return_value=True)
    return ha


class TestGivtcpVirtualControlRouting(unittest.TestCase):
    def test_virtual_givtcp_controls_route_locally_without_ha_websocket(self):
        ha = _make_ha_interface()

        assert ha.call_service(
            "select/select_option",
            entity_id="select.predbat_givtcp_0_discharge_end_time",
            option="19:01",
        ) is True
        assert ha.call_service(
            "number/set_value",
            entity_id="number.predbat_givtcp_0_reserve",
            value=10,
        ) is True
        assert ha.call_service(
            "switch/turn_on",
            entity_id="switch.predbat_givtcp_0_scheduled_charge_enable",
        ) is True

        ha.call_service_websocket_command.assert_not_called()
        assert [c["domain"] for c in ha.base.callbacks] == ["select", "number", "switch"]
        assert ha.base.callbacks[0]["service_data"]["option"] == "19:01"
        assert ha.base.callbacks[1]["service_data"]["value"] == 10

    def test_real_ha_entity_still_uses_websocket(self):
        ha = _make_ha_interface()

        assert ha.call_service(
            "select/select_option",
            entity_id="select.givtcp_real_mode",
            option="Eco",
        ) is True

        ha.call_service_websocket_command.assert_called_once_with(
            "select",
            "select_option",
            {"entity_id": "select.givtcp_real_mode", "option": "Eco"},
        )
        assert ha.base.callbacks == []

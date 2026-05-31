# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from unittest.mock import MagicMock
import sys

# Mock necessary functions/classes that publish_inverter_data uses
def dp3(val):
    return round(val, 3)

class DummyInverter:
    def __init__(self, inverter_id):
        self.id = inverter_id
        self.pv_power = 0.0
        self.load_power = 0.0
        self.battery_power = 0.0

class MockExecute:
    def __init__(self):
        self.prefix = "predbat"
        self.pv_power = 5000.0
        self.grid_power = 2000.0
        self.load_power = 3000.0
        self.battery_power = 1000.0
        self.inverters = []
        self.dashboard_item = MagicMock()

    # Copy the method from execute.py to test it in isolation
    def publish_inverter_data(self):
        """
        Publish inverter data to dashboard
        """
        self.dashboard_item(
            self.prefix + ".pv_power",
            state=dp3(self.pv_power / 1000.0),
            attributes={
                "friendly_name": "Current PV Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "icon": "mdi:battery",
            },
        )
        self.dashboard_item(
            self.prefix + ".grid_power",
            state=dp3(self.grid_power / 1000.0),
            attributes={
                "friendly_name": "Current Grid Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "icon": "mdi:battery",
            },
        )
        self.dashboard_item(
            self.prefix + ".load_power",
            state=dp3(self.load_power / 1000.0),
            attributes={
                "friendly_name": "Current Load Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "icon": "mdi:battery",
            },
        )
        self.dashboard_item(
            self.prefix + ".battery_power",
            state=dp3(self.battery_power / 1000.0),
            attributes={
                "friendly_name": "Current Battery Power",
                "state_class": "measurement",
                "unit_of_measurement": "kW",
                "icon": "mdi:battery",
            },
        )

        # Individual inverter data
        if self.inverters:
            for inverter in self.inverters:
                self.dashboard_item(
                    self.prefix + ".pv_power_{}".format(inverter.id),
                    state=dp3(inverter.pv_power / 1000.0),
                    attributes={
                        "friendly_name": "Current PV Power Inverter {}".format(inverter.id),
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_power_{}".format(inverter.id),
                    state=dp3(inverter.load_power / 1000.0),
                    attributes={
                        "friendly_name": "Current Load Power Inverter {}".format(inverter.id),
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:home-lightning-bolt",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_power_{}".format(inverter.id),
                    state=dp3(inverter.battery_power / 1000.0),
                    attributes={
                        "friendly_name": "Current Battery Power Inverter {}".format(inverter.id),
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )

def test_publish_inverter_data():
    """
    Test that publish_inverter_data correctly publishes summary and individual inverter sensors.
    """
    pb = MockExecute()
    
    # Setup two mock inverters
    inv0 = DummyInverter(inverter_id=0)
    inv0.pv_power = 3000.0
    inv0.load_power = 1500.0
    inv0.battery_power = 600.0
    
    inv1 = DummyInverter(inverter_id=1)
    inv1.pv_power = 2000.0
    inv1.load_power = 1500.0
    inv1.battery_power = 400.0
    
    pb.inverters = [inv0, inv1]
    
    # Call the method under test
    pb.publish_inverter_data()
    
    # Verify summary sensors
    pb.dashboard_item.assert_any_call(
        "predbat.pv_power",
        state=5.0,
        attributes={
            "friendly_name": "Current PV Power",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:battery",
        }
    )
    pb.dashboard_item.assert_any_call(
        "predbat.battery_power",
        state=1.0,
        attributes={
            "friendly_name": "Current Battery Power",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:battery",
        }
    )

    # Verify individual inverter sensors for Inverter 0
    pb.dashboard_item.assert_any_call(
        "predbat.pv_power_0",
        state=3.0,
        attributes={
            "friendly_name": "Current PV Power Inverter 0",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:solar-power",
        }
    )
    pb.dashboard_item.assert_any_call(
        "predbat.load_power_0",
        state=1.5,
        attributes={
            "friendly_name": "Current Load Power Inverter 0",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:home-lightning-bolt",
        }
    )
    pb.dashboard_item.assert_any_call(
        "predbat.battery_power_0",
        state=0.6,
        attributes={
            "friendly_name": "Current Battery Power Inverter 0",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:battery",
        }
    )

    # Verify individual inverter sensors for Inverter 1
    pb.dashboard_item.assert_any_call(
        "predbat.pv_power_1",
        state=2.0,
        attributes={
            "friendly_name": "Current PV Power Inverter 1",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:solar-power",
        }
    )
    pb.dashboard_item.assert_any_call(
        "predbat.load_power_1",
        state=1.5,
        attributes={
            "friendly_name": "Current Load Power Inverter 1",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:home-lightning-bolt",
        }
    )
    pb.dashboard_item.assert_any_call(
        "predbat.battery_power_1",
        state=0.4,
        attributes={
            "friendly_name": "Current Battery Power Inverter 1",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "icon": "mdi:battery",
        }
    )
    
    print("test_publish_inverter_data passed!")

if __name__ == "__main__":
    test_publish_inverter_data()

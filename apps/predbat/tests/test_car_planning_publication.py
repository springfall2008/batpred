"""Native car diagnostic publication cannot create or renew charger permission."""

from copy import deepcopy
import unittest

from car_charging_publish import publish_car_charging_report


class PublicationBase:
    """Record ordinary dashboard publications without any HA or device transport."""

    def __init__(self):
        """Start with an empty native dashboard cache."""
        self.prefix = "test"
        self.dashboard_values = {}

    def dashboard_item(self, entity, state, attributes):
        """Retain the desired publication for exact authority comparisons."""
        self.dashboard_values[entity] = {"state": state, "attributes": deepcopy(attributes)}


class CarPlanningPublicationTests(unittest.TestCase):
    """Forecast visibility remains independent of physical execution permission."""

    def test_report_alone_cannot_start_charger(self):
        """A successful future candidate still publishes no live ON authority."""
        base = PublicationBase()
        publish_car_charging_report(base, {"status": "candidate", "generated_at": "synthetic", "proposed_ev_windows": [{"start": 60, "end": 90}]})
        self.assertEqual(list(base.dashboard_values), ["binary_sensor.test_car_charging_slot"])
        item = base.dashboard_values["binary_sensor.test_car_charging_slot"]
        self.assertEqual(item["state"], "off")
        self.assertEqual(item["attributes"]["planned"], [])
        self.assertIsNone(item["attributes"]["valid_until"])
        self.assertEqual(item["attributes"]["plan_status"], "candidate")

    def test_disabled_planning_does_not_create_a_car_entity(self):
        """An ordinary installation without car publication stays unchanged."""
        base = PublicationBase()
        publish_car_charging_report(base, {"status": "disabled"})
        self.assertEqual(base.dashboard_values, {})

    def test_diagnostics_cannot_renew_or_change_existing_authority(self):
        """Preserve exact immutable lease fields even when plan metadata is updated."""
        base = PublicationBase()
        authority = {"planned": [{"start": 0, "end": 5}], "source": "predbat", "lease_version": 1, "valid_from": "start", "valid_until": "expiry"}
        base.dashboard_item("binary_sensor.test_car_charging_slot", "on", authority)
        publish_car_charging_report(base, {"status": "candidate", "preview": {"large": "not recorded"}})
        item = base.dashboard_values["binary_sensor.test_car_charging_slot"]
        self.assertEqual(item["state"], "on")
        self.assertEqual({key: item["attributes"][key] for key in authority}, authority)
        self.assertNotIn("preview", item["attributes"]["plan"])

    def test_native_slot_refresh_retains_diagnostics_without_a_lease(self):
        """Ordinary slot publication keeps the functional report and source tag, not authority."""
        from unit_test import create_predbat
        from tests.test_car_planning_native import _setup

        base = create_predbat()
        _setup(base, pv=5)
        report = {"status": "candidate", "generated_at": base.now_utc.isoformat()}
        base.car_charging_report = report
        publish_car_charging_report(base, report)
        base.publish_car_plan()
        item = base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]
        self.assertEqual(item["attributes"]["source"], "predbat")
        self.assertEqual(item["attributes"]["plan_status"], "candidate")
        self.assertIsNone(item["attributes"].get("valid_until"))


def run_car_planning_publication_tests(base):
    """Expose publication authority tests through the ordinary registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CarPlanningPublicationTests))
    return not result.wasSuccessful()

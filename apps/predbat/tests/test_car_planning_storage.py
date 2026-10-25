"""Storage migration preserves energy, invalidations and idempotent recovery."""

import asyncio
from copy import deepcopy
import unittest

from car_charging_storage import CAR_STORAGE_MODULE, migrate_car_storage


class MemoryCarStorage:
    """Detached async Storage contract; no filesystem, controller or HA access."""

    def __init__(self):
        """Seed failed physical evidence rather than an empty successful session."""
        self.records = {
            ("previous_planner", "recurring_session"): {"active": True, "invalid": "recurring_meter_contradiction", "session": {"meter_baseline": 100, "last_energy": 101.5, "initial_kwh": 3}, "latest_deadline": {"met": None}},
            ("previous_planner", "confirmed_session_guard"): {"identity": "synthetic", "invalid": True},
        }
        self.writes = []
        self.fail_second = False

    async def load_checked(self, module, name):
        """Return a detached record so in-memory mutation cannot fake a migration."""
        return deepcopy(self.records.get((module, name)))

    async def save(self, module, name, data, **kwargs):
        """Emulate acknowledged writes and an interrupted second-record migration."""
        assert kwargs == {"format": "json", "expiry": None}
        if self.fail_second and name == "confirmed_session_guard":
            return False
        self.writes.append((module, name))
        self.records[(module, name)] = deepcopy(data)
        return True


class CarPlanningStorageTests(unittest.TestCase):
    """Migration never treats a persisted failure as a fresh physical baseline."""

    def test_preserves_full_evidence_and_originals(self):
        """All delivered energy, fault markers and prior deadlines survive byte-equivalent data copy."""
        storage = MemoryCarStorage()
        originals = deepcopy(storage.records)
        receipt = asyncio.run(migrate_car_storage(storage, "previous_planner"))
        self.assertFalse(receipt["execution_authorised"])
        for (module, name), record in originals.items():
            self.assertEqual(storage.records[(module, name)], record)
            self.assertEqual(storage.records[(CAR_STORAGE_MODULE, name)], record)

    def test_conflict_is_rejected_before_any_write(self):
        """One conflicting destination blocks the entire proposed migration."""
        storage = MemoryCarStorage()
        storage.records[(CAR_STORAGE_MODULE, "confirmed_session_guard")] = {"invalid": False}
        with self.assertRaises(ValueError):
            asyncio.run(migrate_car_storage(storage, "previous_planner"))
        self.assertEqual(storage.writes, [])

    def test_interruption_is_recoverable_and_idempotent(self):
        """A partial destination is not erased and identical records are not written twice."""
        storage = MemoryCarStorage()
        storage.fail_second = True
        with self.assertRaises(ValueError):
            asyncio.run(migrate_car_storage(storage, "previous_planner"))
        storage.fail_second = False
        asyncio.run(migrate_car_storage(storage, "previous_planner"))
        before = list(storage.writes)
        asyncio.run(migrate_car_storage(storage, "previous_planner"))
        self.assertEqual(storage.writes, before)
        self.assertEqual(storage.records[(CAR_STORAGE_MODULE, "recurring_session")]["invalid"], "recurring_meter_contradiction")

    def test_nonfinite_evidence_fails_before_any_write(self):
        """Malformed physical records must not create even a partial migration."""
        storage = MemoryCarStorage()
        storage.records[("previous_planner", "confirmed_session_guard")]["invalid"] = float("nan")
        with self.assertRaises(ValueError):
            asyncio.run(migrate_car_storage(storage, "previous_planner"))
        self.assertEqual(storage.writes, [])


def run_car_planning_storage_tests(base):
    """Expose migration tests through the repository's normal registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CarPlanningStorageTests))
    return not result.wasSuccessful()

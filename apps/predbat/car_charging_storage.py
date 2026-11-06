"""Explicit Storage-based migration of retained EV energy and validity evidence."""

from copy import deepcopy
import hashlib
import json
import re


CAR_STORAGE_MODULE = "car_charging"
CAR_STORAGE_RECORDS = ("recurring_session", "confirmed_session_guard")


async def migrate_car_storage(storage, source_module):
    """Copy supplied source records without deleting originals, changing faults or rebasing energy.

    Run while the application is stopped. No live authority is created by this
    operation. Conflicting destinations fail before the first write; interrupted
    migrations can be retried because identical destination records are accepted.
    """
    if not isinstance(source_module, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", source_module) or source_module == CAR_STORAGE_MODULE:
        raise ValueError("An explicit, distinct source Storage module is required")
    pending = []
    for name in CAR_STORAGE_RECORDS:
        record = await storage.load_checked(source_module, name)
        if record is None:
            continue
        if not isinstance(record, dict):
            raise ValueError("Source car evidence is malformed")
        destination = await storage.load_checked(CAR_STORAGE_MODULE, name)
        if destination is not None and destination != record:
            raise ValueError("Destination car evidence conflicts with source")
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, allow_nan=False).encode()).hexdigest()
        pending.append((name, deepcopy(record), destination is None, digest))
    receipt = {"records": {}, "source_preserved": True, "execution_authorised": False}
    for name, record, write, digest in pending:
        if write and await storage.save(CAR_STORAGE_MODULE, name, record, format="json", expiry=None) is not True:
            raise ValueError("Car evidence migration write failed")
        if await storage.load_checked(CAR_STORAGE_MODULE, name) != record:
            raise ValueError("Car evidence migration readback failed")
        if await storage.load_checked(source_module, name) != record:
            raise ValueError("Source car evidence changed during migration")
        receipt["records"][name] = digest
    return receipt

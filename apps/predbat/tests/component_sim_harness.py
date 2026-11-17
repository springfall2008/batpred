# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Pieces shared by the simulated-day harnesses that run a real PredBat against a real cloud component and a fake cloud.

The GE Cloud API budget harness (tests/gecloud_budget_harness.py) and the Teslemetry hold harness
(tests/teslemetry_sim_harness.py) both build a PredBat, hand a component a test storage, take every sleep out,
and deliver execute's inverter writes to the component through the same HA service events a real install uses.
"""

import copy
import json

from tests.test_infra import TestHAInterface, run_async

# The HA services execute uses on an inverter entity, and the component handler each one reaches
ROUTED_SERVICES = {
    "switch/turn_on": "switch",
    "switch/turn_off": "switch",
    "switch/toggle": "switch",
    "number/set_value": "number",
    "select/select_option": "select",
}


async def no_sleep(*args, **kwargs):
    """Stand-in for asyncio.sleep: retry back-off and pacing cost no wall-clock time."""
    return None


class MemoryStorage:
    """An in-memory stand-in for the Storage component, so a test can hand a component a settings cache."""

    def __init__(self):
        """Start empty; data is keyed by (module, filename)."""
        self.data = {}

    async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
        """Keep a copy of the data - through JSON when that is the format, as the real storage does, so integer dict keys come back as strings."""
        self.data[(module, filename)] = json.loads(json.dumps(data)) if format == "json" else copy.deepcopy(data)
        return True

    async def load(self, module, filename):
        """Return a copy of what was saved, or None."""
        return copy.deepcopy(self.data.get((module, filename), None))

    async def age(self, module, filename):
        """Saved data is always fresh by file age; a component's own read stamps carry the real age."""
        return 0 if (module, filename) in self.data else None


def reset_config_to_defaults(my_predbat):
    """Put every setting back to its default, so the simulated user is a default user and not the template's."""
    for item in my_predbat.CONFIG_ITEMS:
        if "default" in item:
            my_predbat.expose_config(item["name"], item["default"])
    my_predbat.fetch_config_options()


class ComponentRoutingHAInterface(TestHAInterface):
    """TestHAInterface that delivers service calls on one component's entities to its event handlers, as HA would.

    component is the component to route to and entity_marker the name its entities carry after the prefix,
    e.g. "gecloud" for switch.predbat_gecloud_*. call_routed() runs each delivered event and is the hook a
    harness overrides to label the API calls the event makes.
    """

    component = None
    entity_marker = None

    def routes(self, service, entity_id):
        """True when this service call targets one of the component's own entities."""
        if not self.component or service not in ROUTED_SERVICES or not isinstance(entity_id, str):
            return False
        domain = service.split("/")[0]
        return entity_id.startswith("{}.{}_{}_".format(domain, self.component.prefix, self.entity_marker))

    def call_routed(self, entity_id, old, new, coroutine):
        """Run one delivered event to completion."""
        run_async(coroutine)

    def call_service(self, service, **kwargs):
        """Route an inverter write on a component entity to the component; anything else as before."""
        entity_id = kwargs.get("entity_id", None)
        if self.routes(service, entity_id):
            domain = service.split("/")[0]
            old = self.dummy_items.get(entity_id, None)
            if isinstance(old, dict):
                old = old.get("state", None)
            if domain == "switch":
                new = service.split("/")[1]
                coroutine = self.component.switch_event(entity_id, new)
            elif domain == "number":
                new = kwargs.get("value", None)
                coroutine = self.component.number_event(entity_id, new)
            else:
                new = kwargs.get("option", None)
                coroutine = self.component.select_event(entity_id, new)
            self.call_routed(entity_id, old, new, coroutine)
            return True
        return super().call_service(service, **kwargs)

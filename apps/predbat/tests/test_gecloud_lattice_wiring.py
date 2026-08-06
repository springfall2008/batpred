# cspell:ignore autoconfig gecloud
"""Focused tests for default-off GE Cloud Lattice live wiring."""

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gecloud import GECloudDirect  # noqa: E402


class RecordingRuntime:
    """Record synchronous runtime calls made through ``asyncio.to_thread``."""

    def __init__(self, enabled=True, active=True):
        self.enabled = enabled
        self.active = active
        self.ingests = []
        self.health = []

    def ingest_gecloud_state(self, devices, settings, info, **kwargs):
        """Record one complete provider state."""
        self.ingests.append((devices, settings, info, kwargs))

    def set_gecloud_liveness(self, health):
        """Record one provider health transition."""
        self.health.append(health)

    def provider_active(self, provider_id):
        return self.active and provider_id == "ge-cloud"


def component(runtime, automatic=True):
    """Build a minimal GE component for the isolated wiring helpers."""
    ge = object.__new__(GECloudDirect)
    ge.automatic = automatic
    ge.base = SimpleNamespace(lattice_autoconfig_runtime=runtime)
    ge.prefix = "site"
    ge.devices_dict = {
        "battery": ["bat1"],
        "pv": [],
        "gateway": None,
        "ems": None,
        "battery_meters": {"bat1": [42]},
    }
    ge.settings = {"bat1": {1: {"name": "Battery Charge Power"}}}
    ge.info = {"bat1": {"info": {"model": "Hybrid"}}}
    flags = {
        "ge_cloud_load_today_ignore": True,
        "ge_cloud_automatic_split_pv": False,
        "ge_cloud_automatic_split_ct": True,
        "ge_cloud_automatic_shared_ct": False,
    }
    ge.get_arg = lambda name, default=None, **_kwargs: flags.get(name, default)
    ge.async_automatic_config = AsyncMock()
    return ge


def steady_state_component(runtime):
    """Extend the helper with the state needed by one two-minute poll."""
    ge = component(runtime)
    ge.device_list = ["bat1"]
    ge.evc_device_list = []
    ge.status = {}
    ge.meter = {}
    ge.pending_writes = {"bat1": []}
    ge.api_auth_failed = False
    ge.auth_denied_reported = False
    ge.last_success_timestamp = object()
    ge.async_get_inverter_meter = AsyncMock(return_value={})
    ge.async_get_device_info = AsyncMock(
        return_value={"info": {"model": "Hybrid"}},
    )
    ge.publish_status = AsyncMock()
    ge.publish_meter = AsyncMock()
    ge.publish_info = AsyncMock()
    return ge


class TestGECloudLatticeWriterSelection(unittest.TestCase):
    """One and only one automatic-configuration writer is selected."""

    def test_enabled_runtime_replaces_legacy_writer_with_complete_state(self):
        runtime = RecordingRuntime()
        ge = component(runtime)

        self.assertTrue(asyncio.run(ge._refresh_automatic_config()))

        ge.async_automatic_config.assert_not_awaited()
        self.assertEqual(len(runtime.ingests), 1)
        devices, settings, info, flags = runtime.ingests[0]
        self.assertIs(devices, ge.devices_dict)
        self.assertIs(settings, ge.settings)
        self.assertIs(info, ge.info)
        self.assertEqual(
            flags,
            {
                "prefix": "site",
                "load_today_ignore": True,
                "split_pv": False,
                "split_ct": True,
                "shared_ct": False,
            },
        )

    def test_absent_disabled_or_nonautomatic_runtime_preserves_safe_path(self):
        for runtime in (
            None,
            RecordingRuntime(enabled=False),
            RecordingRuntime(active=False),
        ):
            with self.subTest(runtime=runtime):
                ge = component(runtime)
                self.assertTrue(asyncio.run(ge._refresh_automatic_config()))
                ge.async_automatic_config.assert_awaited_once_with(ge.devices_dict)
                if runtime is not None:
                    self.assertEqual(runtime.ingests, [])

        runtime = RecordingRuntime()
        ge = component(runtime, automatic=False)
        self.assertFalse(asyncio.run(ge._refresh_automatic_config()))
        ge.async_automatic_config.assert_not_awaited()
        self.assertEqual(runtime.ingests, [])

    def test_liveness_is_only_published_for_enabled_automatic_runtime(self):
        runtime = RecordingRuntime()
        ge = component(runtime)

        self.assertTrue(asyncio.run(ge._set_lattice_liveness(None)))
        self.assertTrue(asyncio.run(ge._set_lattice_liveness(False)))
        self.assertEqual(runtime.health, [None, False])

        disabled = component(RecordingRuntime(), automatic=False)
        self.assertFalse(asyncio.run(disabled._set_lattice_liveness(False)))
        self.assertEqual(disabled.base.lattice_autoconfig_runtime.health, [])

    def test_successful_two_minute_poll_republishes_complete_state_once(self):
        runtime = RecordingRuntime()
        ge = steady_state_component(runtime)

        async def successful_status(_device, _previous):
            ge.last_success_timestamp = object()
            return {"status": "NORMAL"}

        ge.async_get_inverter_status = successful_status

        self.assertTrue(asyncio.run(ge.run(seconds=120, first=False)))

        self.assertEqual(len(runtime.ingests), 1)
        self.assertEqual(runtime.health, [])
        ge.async_automatic_config.assert_not_awaited()

    def test_denied_core_poll_marks_offline_without_rewriting_config(self):
        runtime = RecordingRuntime()
        ge = steady_state_component(runtime)
        ge.base.record_status = lambda *_args, **_kwargs: None

        async def denied_status(_device, _previous):
            ge.api_auth_failed = True
            return {}

        ge.async_get_inverter_status = denied_status

        self.assertTrue(asyncio.run(ge.run(seconds=120, first=False)))

        self.assertEqual(runtime.ingests, [])
        self.assertEqual(runtime.health, [False])
        ge.async_automatic_config.assert_not_awaited()

    def test_stop_invalidates_long_lived_cloud_provider(self):
        runtime = RecordingRuntime()
        ge = component(runtime)

        asyncio.run(ge.stop())

        self.assertEqual(runtime.health, [False])


if __name__ == "__main__":
    unittest.main()

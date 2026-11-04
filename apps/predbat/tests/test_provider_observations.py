"""Accepted observations for the multi-device and HTTP-only provider paths."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from component_base import telemetry_number, telemetry_time
from deye import DeyeAPI
from enphase import EnphaseAPI
from solax import SolaxAPI
from solis import SolisAPI
from tests.test_deye_api import MockDeye


def _observe(api):
    """Attach an isolated event collector to a minimal real provider instance."""
    events = []
    api.base = SimpleNamespace(component_telemetry_observer=events.append)
    return events


async def _call(operation):
    """Execute a retry operation without adding timing to this parser test."""
    return await operation()


async def _provider_checks():
    """Exercise actual parser entry points, accepted zeros and held multi-battery state."""
    for invalid in (None, True, float("nan"), float("inf"), "bad"):
        assert telemetry_number(invalid) is None
    assert telemetry_number("0") == 0
    at = datetime(2026, 11, 4, tzinfo=timezone.utc)
    assert telemetry_time(at.isoformat()) == at
    assert telemetry_time(at.isoformat().replace("+00:00", "Z")) == at
    assert telemetry_time(int(at.timestamp() * 1000), "milliseconds") == at
    assert telemetry_time(int(at.timestamp() * 1000), "auto") == at
    for invalid in (None, True, "bad", "2026-11-04T00:00:00", float("inf")):
        assert telemetry_time(invalid) is None

    deye = MockDeye()
    events = _observe(deye)
    deye._post = AsyncMock(return_value={"success": True, "deviceDataList": [{"deviceSn": "A", "collectionTime": int(at.timestamp()), "dataList": [{"key": "SOC", "value": 0}]}]})
    await DeyeAPI.fetch_device_data(deye, "A")
    assert events[-1]["values"]["soc"] == 0 and "soc" in events[-1]["updated_fields"]
    assert events[-1]["sample_time"] == at
    deye._post.return_value["deviceDataList"][0]["deviceSn"] = "OTHER"
    await DeyeAPI.fetch_device_data(deye, "A")
    assert len(events) == 1
    deye._post.return_value["deviceDataList"][0]["deviceSn"] = "A"
    deye._post.return_value["deviceDataList"][0]["dataList"].append({"key": "SOC", "value": 50})
    await DeyeAPI.fetch_device_data(deye, "A")
    assert "soc" not in events[-1]["updated_fields"]

    solis = SolisAPI.__new__(SolisAPI)
    events = _observe(solis)
    solis._with_retry = _call
    solis._execute_request = AsyncMock(return_value={"sn": "A", "batteryCapacitySoc": 0, "batteryVoltage": 50, "dataTimestamp": int(at.timestamp() * 1000)})
    await solis.get_inverter_detail("A")
    assert events[-1]["sample_time"] == at and "batteryCapacitySoc" in events[-1]["updated_fields"]
    solis._execute_request.return_value["batteryVoltage"] = 0
    await solis.get_inverter_detail("A")
    assert not events[-1]["updated_fields"]

    enphase = EnphaseAPI.__new__(EnphaseAPI)
    events = _observe(enphase)
    enphase.battery_status = {}
    enphase._save_cache = AsyncMock()
    enphase.request_json = AsyncMock(return_value={"storages": [{"max_capacity": 1000, "available_energy": 500, "last_report": int(at.timestamp())}, {"max_capacity": 1000, "available_energy": 500, "last_report": int(at.timestamp()) + 60}]})
    await enphase.get_battery_status("SITE")
    await enphase.get_battery_status("SITE")
    assert len(events) == 2 and events[-1]["sample_time"] == at
    assert events[-1]["values"]["soc_percent"] == 50
    enphase.request_json.return_value["storages"][0]["last_report"] = None
    await enphase.get_battery_status("SITE")
    assert events[-1]["sample_time"] is None

    solax = SolaxAPI.__new__(SolaxAPI)
    events = _observe(solax)
    solax.device_info = {key: {"deviceType": 2} for key in ("A", "B")}
    solax.plant_batteries = {"PLANT": ["A", "B"]}
    solax.realtime_device_data = {key: {"deviceSn": key, "batterySOC": 50, "batteryRemainings": 5, "batteryVoltage": 400, "dataTime": at.isoformat()} for key in ("A", "B")}
    solax.query_device_realtime_data = AsyncMock(return_value=[{}])
    await solax.query_device_realtime_data_all("PLANT")
    assert events[-1]["device_id"] == "PLANT" and events[-1]["values"]["batteryRemainings"] == 10
    assert events[-1]["sample_time"] == at
    solax.query_device_realtime_data.side_effect = [[{}], None]
    await solax.query_device_realtime_data_all("PLANT")
    assert len(events) == 1  # A held pack cannot renew a plant aggregate.
    solax.query_device_realtime_data.side_effect = None
    solax.realtime_device_data["A"]["deviceSn"] = "FOREIGN"
    await solax.query_device_realtime_data_all("PLANT")
    assert len(events) == 1

    # Exercise the real fetch/apply path for a synthetic battery and an empty response.
    solax.query_device_realtime_data = SolaxAPI.query_device_realtime_data.__get__(solax)
    solax.device_info = {"INV": {"deviceType": 1, "registerNo": "REG"}, "INV_battery": {"deviceType": 2}}
    solax.plant_batteries = {"PLANT": ["INV_battery"]}
    solax.realtime_device_failed = set()
    solax.log = lambda message: None
    battery = {"deviceSn": "PACK", "registerNo": "REG", "batterySOC": 50, "batteryRemainings": 5, "batteryVoltage": 400, "dataTime": at.isoformat()}
    solax.fetch_single_result = AsyncMock(side_effect=[([{}], None), ([battery], None)])
    await solax.query_device_realtime_data_all("PLANT")
    assert len(events) == 2 and events[-1]["sample_time"] == at
    solax.fetch_single_result.side_effect = [([{}], None), ([], None)]
    await solax.query_device_realtime_data_all("PLANT")
    assert len(events) == 2 and "INV_battery" in solax.realtime_device_failed


def run_provider_observation_tests(my_predbat):
    """Run the timestamp and applied-sample regression checks through the standard harness."""
    asyncio.run(_provider_checks())
    return False

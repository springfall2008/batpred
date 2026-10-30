"""Meter-scope regressions using separate raw, cached and published provider data."""

import asyncio
import copy
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from gecloud import GECloudDirect, GE_API_INVERTER_STATUS, SharedMeterLoad
from tests.test_ge_cloud import MockGECloudDirect, _discovery_component


def packet(stamp, ac, grid, consumption=0):
    """Build a synthetic raw Cloud sample with explicit source provenance."""
    return {"time": stamp.isoformat(), "status": "Normal", "inverter": {"power": ac}, "grid": {"power": grid}, "consumption": consumption}


def configured(devices=None, config=None):
    """Create a component with two declared shared-meter inverters and separate raw samples."""
    ge = MockGECloudDirect()
    ge.devices_dict = devices or {"battery": ["inverter_a", "inverter_b"], "pv": [], "ems": None, "gateway": None, "battery_meters": {}}
    ge.config_args = {"ge_cloud_automatic_shared_ct": True, **(config or {})}
    ge.settings = {sn: {} for sn in ge.devices_dict["battery"]}
    ge._now_utc_exact = datetime(2026, 10, 27, tzinfo=timezone.utc)
    ge.live_display_samples = {"inverter_a": packet(ge.now_utc_exact, -2500, -12000, 9500), "inverter_b": packet(ge.now_utc_exact, -2500, -12000, 9500)}
    return ge


def observed_frames():
    """Check the collected charging/supply relationships and reject misaligned export observations."""
    fixture = json.loads((Path(__file__).parent / "fixtures/gecloud_shared_meter.json").read_text())
    for row in fixture["shared_meter"]:
        raw = row["devices"]
        now = max(datetime.fromisoformat(d["time"].replace("Z", "+00:00")) for d in raw.values())
        meter = SharedMeterLoad(raw)
        actual, error = meter.sample(raw, now)
        if row["source_skew_seconds"] > 30:
            assert actual is None and "30 seconds" in error
        else:
            # The oracle is the complete AC balance, not a clipped native consumption sum.
            ac = sum(d["inverter"]["power"] for d in raw.values())
            expected = ac - raw["inverter_a"]["grid"]["power"]
            assert error is None and actual == expected, (row["scenario"], actual, expected)
            if row["scenario"] in ["both_batteries_charging", "both_batteries_supplying_load"]:
                assert actual != raw["inverter_a"]["consumption"]

    # Synthetic synchronous export based on the observed clipping failure. It is not
    # presented as an independently calibrated, synchronous customer observation.
    now = datetime(2026, 10, 27, 19, tzinfo=timezone.utc)
    raw = {"a": packet(now, 2200, 3400), "b": packet(now, 2200, 3400)}
    assert SharedMeterLoad(raw).sample(raw, now) == (1000, None)
    # PV-only AC is included, while its grid scalar must never replace the battery's meter.
    raw["0_pv"] = packet(now, 600, 0)
    assert SharedMeterLoad(raw, grid_member="a").sample(raw, now) == (1600, None)


def invalid_samples_and_energy():
    """Unknown, stale and repeated input must not become zero load or extra measured energy."""
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    original = {"a": packet(now, 1000, -1000), "b": packet(now, 1000, -1000)}
    mutations = [
        lambda d: d.pop("b"),
        lambda d: d["b"].pop("inverter"),
        lambda d: d["b"]["inverter"].update(power=None),
        lambda d: d["b"]["inverter"].update(power=math.nan),
        lambda d: d["b"]["inverter"].update(power=True),
        lambda d: d["a"]["grid"].update(power=math.inf),
        lambda d: d["b"].update(time="not a date"),
        lambda d: d["b"].update(time=now.replace(tzinfo=None).isoformat()),
        lambda d: d["b"].update(time=(now - timedelta(seconds=301)).isoformat()),
        lambda d: d["b"].update(time=(now + timedelta(seconds=31)).isoformat()),
        lambda d: d["b"].update(status="Offline"),
        lambda d: d["a"]["grid"].update(power=5000),
    ]
    for change in mutations:
        raw = copy.deepcopy(original)
        change(raw)
        meter = SharedMeterLoad(original)
        assert meter.sample(raw, now)[0] is None
        assert meter.total_kwh == 0 and meter.previous is None

    meter = SharedMeterLoad(original)
    assert meter.sample(original, now) == (3000, None)
    for _ in range(3):
        assert meter.sample(original, now) == (3000, None)
    assert meter.total_kwh == 0
    next_time = now + timedelta(seconds=120)
    raw = {k: packet(next_time, 1000, -1000) for k in original}
    assert meter.sample(raw, next_time) == (3000, None)
    assert math.isclose(meter.total_kwh, 0.1)
    # A gap does not get filled by assuming the last observed flow held throughout.
    later = next_time + timedelta(hours=1)
    assert meter.sample({k: packet(later, 1000, -1000) for k in original}, later)[0] == 3000
    assert math.isclose(meter.total_kwh, 0.1)
    assert meter.sample({"a": packet(later, 1000, -1000)}, later)[0] is None
    later += timedelta(seconds=120)
    meter.sample({k: packet(later, 1000, -1000) for k in original}, later)
    assert math.isclose(meter.total_kwh, 0.1), "Recovery must establish a new baseline"
    backwards = later - timedelta(seconds=10)
    assert meter.sample({k: packet(backwards, 1000, -1000) for k in original}, later)[0] is None
    midnight = datetime(2026, 10, 27, 23, 59, tzinfo=timezone.utc)
    meter = SharedMeterLoad(original, total_kwh=12)
    meter.sample({k: packet(midnight, 1000, -1000) for k in original}, midnight)
    midnight += timedelta(minutes=2)
    meter.sample({k: packet(midnight, 1000, -1000) for k in original}, midnight)
    assert math.isclose(meter.total_kwh, 12.1), "A lifetime measurement counter stays continuous through local midnight"


async def configuration_and_restart():
    """Exercise real auto-config, raw provenance, storage and restart without any API writes."""
    ge = configured(config={"ge_cloud_data": True})
    await ge.async_automatic_config(ge.devices_dict)
    assert ge.config_args["grid_power"] == ["sensor.predbat_gecloud_inverter_a_grid_power", 0]
    assert ge.config_args["load_power"] == ["sensor.predbat_gecloud_shared_load_power", 0]
    assert ge.config_args["load_today"] == ["sensor.predbat_gecloud_shared_load_total"]
    assert ge.config_args["ge_cloud_data"] is False, "Do not feed the old per-inverter consumption download into the corrected planner"
    assert ge.config_args["battery_power"] == ["sensor.predbat_gecloud_inverter_a_battery_power", "sensor.predbat_gecloud_inverter_b_battery_power"]
    # Reordering devices must not change which lifetime grid counter is read.
    ge.devices_dict["battery"].reverse()
    await ge.async_automatic_config(ge.devices_dict)
    assert ge.config_args["import_today"] == ["sensor.predbat_gecloud_inverter_a_grid_import_total"]
    saved = {}

    async def load(namespace, key):
        """Return a copy so an in-memory counter cannot mutate the fake disk state."""
        return copy.deepcopy(saved.get(key))

    async def save(namespace, key, value, **kwargs):
        """Persist only data the real component explicitly sends to Storage."""
        saved[key] = copy.deepcopy(value)
        return True

    from types import SimpleNamespace

    ge._mock_storage = SimpleNamespace(load=load, save=save)
    assert await ge.publish_shared_meter_load()
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_power"]["state"] == 7000
    ge._now_utc_exact += timedelta(seconds=120)
    ge.live_display_samples = {sn: packet(ge.now_utc_exact, -2500, -12000) for sn in ["inverter_a", "inverter_b"]}
    assert await ge.publish_shared_meter_load()
    total = saved["shared_meter_load"]["total_kwh"]
    assert math.isclose(total, 7000 * 120 / 3600000)

    restarted = configured()
    restarted._mock_storage = ge._mock_storage
    restarted._now_utc_exact = ge.now_utc_exact + timedelta(seconds=120)
    restarted.live_display_samples = {sn: packet(restarted.now_utc_exact, -2500, -12000) for sn in ["inverter_a", "inverter_b"]}
    assert await restarted.publish_shared_meter_load()
    assert math.isclose(restarted._shared_meter_load.total_kwh, total), "Restart restores energy, never bridges downtime"
    # A fresh envelope with an old AC field in the merged control cache is not usable.
    restarted.status = copy.deepcopy(restarted.live_display_samples)
    restarted.live_display_samples["inverter_b"].pop("inverter")
    assert not await restarted.publish_shared_meter_load()
    assert restarted.dashboard_items["sensor.predbat_gecloud_shared_load_power"]["state"] == "unavailable"
    assert restarted.dashboard_items["sensor.predbat_gecloud_shared_load_total"]["state"] == "unavailable"
    assert math.isclose(saved["shared_meter_load"]["total_kwh"], total)
    ge.config_args["ge_cloud_automatic_split_ct"] = True
    await ge.async_automatic_config(ge.devices_dict)
    assert ge.config_args["ge_cloud_data"] is True
    assert await ge.publish_shared_meter_load() and ge._shared_meter_load is None

    custom = configured(config={"ge_cloud_load_today_ignore": True, "load_today": ["sensor.own_load_total"], "ge_cloud_data": False})
    await custom.async_automatic_config(custom.devices_dict)
    assert custom.config_args["load_today"] == ["sensor.own_load_total"]
    assert custom.config_args["ge_cloud_data"] is False


async def phase_and_coordinator_scope():
    """Already aggregated three-phase scalars, independent phases and coordinators keep their scope."""
    fixture = json.loads((Path(__file__).parent / "fixtures/gecloud_shared_meter.json").read_text())
    for site in fixture["three_phase"]:
        ge = configured({"battery": ["three_phase"], "pv": [], "ems": None, "gateway": None, "battery_meters": {"three_phase": [136]}})
        await ge.async_automatic_config(ge.devices_dict)
        assert ge.config_args["grid_power"] == ["sensor.predbat_gecloud_three_phase_grid_power"]
        for s in site["samples"]:
            values = s["power_W"]
            await ge.publish_status("three_phase", {"grid": {"power": values["grid_power"]}, "inverter": {"power": values["inverter_power"]}, "consumption": values["consumption_power"]})
            assert ge.dashboard_items["sensor.predbat_gecloud_three_phase_grid_power"]["state"] == values["grid_power"]
            assert ge.dashboard_items["sensor.predbat_gecloud_three_phase_consumption_power"]["state"] == values["consumption_power"]
        assert await ge.publish_shared_meter_load() and not hasattr(ge, "_shared_meter_error")
    # Synthetic independent phase topology: no live per-phase fixture was collected.
    devices = {"battery": ["phase_a", "phase_b", "phase_c"], "pv": [], "ems": None, "gateway": None, "battery_meters": {"phase_a": [1], "phase_b": [2], "phase_c": [3]}}
    ge = configured(devices, {"ge_cloud_automatic_shared_ct": False})
    await ge.async_automatic_config(devices)
    assert ge.config_args["grid_power"] == [f"sensor.predbat_gecloud_{sn}_grid_power" for sn in devices["battery"]]
    devices["battery_meters"]["phase_b"] = [1]
    assert not ge._shared_ct(devices, devices["battery"]), "A duplicate subset must not collapse three separate phase/meter scopes"
    devices["battery_meters"] = {sn: [None, "", False] for sn in devices["battery"]}
    assert not ge._shared_ct(devices, devices["battery"]), "Missing meter identifiers are not evidence of a shared supply"
    devices["battery_meters"] = {sn: [1] for sn in devices["battery"]}
    assert ge._shared_ct(devices, devices["battery"])
    for coordinator in ["ems", "gateway"]:
        devices[coordinator] = "coordinator"
        assert not ge._shared_ct(devices, devices["battery"])
        devices[coordinator] = None


async def persistence_and_pv_failures():
    """Failed storage or an undeclared PV contribution remains unknown, with no lost-total overwrite."""
    from types import SimpleNamespace

    ge = configured()
    saved = {"members": ["inverter_a", "inverter_b"], "grid_member": "inverter_a", "total_kwh": 12}
    ge._mock_storage = SimpleNamespace(load=AsyncMock(return_value=saved), save=AsyncMock(side_effect=OSError("disk unavailable")))
    ge.dashboard_items["sensor.predbat_gecloud_shared_load_power"] = {"state": 1234}
    assert not await ge.publish_shared_meter_load()
    assert ge._shared_meter_load is None
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_power"]["state"] == "unavailable"
    ge._mock_storage.save = AsyncMock(return_value=False)
    assert not await ge.publish_shared_meter_load()
    assert ge._shared_meter_load is None
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_total"]["state"] == "unavailable"
    ge._mock_storage.save = AsyncMock(return_value=True)
    assert await ge.publish_shared_meter_load()
    assert ge._shared_meter_load.total_kwh == 12
    ge._shared_meter_load = None
    ge._mock_storage.load = AsyncMock(side_effect=OSError("read unavailable"))
    assert not await ge.publish_shared_meter_load()
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_total"]["state"] == "unavailable"

    ge = configured()
    ge.devices_dict["pv"] = ["0_pv"]
    ge.live_display_samples["0_pv"] = packet(ge.now_utc_exact, 600, 0)
    assert not await ge.publish_shared_meter_load()
    ge.config_args["ge_cloud_automatic_split_pv"] = True
    assert await ge.publish_shared_meter_load()
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_power"]["state"] == 7600


async def polling_uses_raw_sources():
    """The real component loop publishes only after both raw reads, never from a merged fallback."""
    from types import MethodType

    devices = {"battery": ["a", "b"], "pv": [], "ems": None, "gateway": None, "battery_meters": {"a": [1], "b": [1]}}
    ge = _discovery_component(devices)
    now = ge.now_utc_exact
    responses = {"a": packet(now, -2500, -12000), "b": packet(now, -2500, -12000)}

    async def response(endpoint, serial="", **kwargs):
        """Stand in for transport, retaining the real raw-vs-merged status reader."""
        return responses.get(serial) if endpoint == GE_API_INVERTER_STATUS else None

    ge.async_get_inverter_data_retry = response
    ge.async_get_inverter_status = MethodType(GECloudDirect.async_get_inverter_status, ge)
    assert await ge.run(0, True)
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_power"]["state"] == 7000
    # The merged native control cache keeps b's old fields after a failed read, but the
    # corrected site meter must become unknown rather than borrowing that old AC flow.
    responses["b"] = None
    assert await ge.run(120, False), "Missing load samples must not back off all Cloud polling/control"
    assert ge.status["b"]["inverter"]["power"] == -2500
    assert ge.live_display_samples["b"] is None
    assert ge.dashboard_items["sensor.predbat_gecloud_shared_load_power"]["state"] == "unavailable"


def run_shared_meter_tests():
    """Run observed and synthetic regressions through the normal GE test harness."""
    observed_frames()
    invalid_samples_and_energy()
    asyncio.run(configuration_and_restart())
    asyncio.run(phase_and_coordinator_scope())
    asyncio.run(persistence_and_pv_failures())
    asyncio.run(polling_uses_raw_sources())
    print("PASS: shared meter scope, AC power, measured energy, raw freshness and three-phase regressions")
    return 0

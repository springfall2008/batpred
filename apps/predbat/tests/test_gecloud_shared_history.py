"""Known energy-flow cases for declared shared-grid history and real fetch integration."""

import asyncio
import copy
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from gecloud import SharedMeterHistory
from tests.test_ge_cloud import MockGECloudData
from tests.test_gecloud_shared_meter import configured


START = datetime(2026, 10, 27, tzinfo=timezone.utc)


def record(minute, consumption, imported, exported, pv=20):
    """Construct synthetic lifetime counters at an explicit source clock."""
    return {"last_updated": (START + timedelta(minutes=minute)).isoformat(), "consumption": consumption, "import": imported, "export": exported, "pv": pv}


def history_sources():
    """Describe charging, export and idle flows with independently specified site energy.

    First interval: grid imports 5 kWh; inverters absorb 1 and 2 kWh; house uses 2.
    Next: each inverter supplies 2 kWh, grid exports 3; house uses 1.
    Last: inverters idle, grid supplies the house's 1 kWh.
    """
    return {
        "inverter_a": [record(0, 100, 50, 10), record(60, 104, 55, 10), record(120, 103, 55, 13, 22), record(180, 104, 56, 13, 22)],
        "inverter_b": [record(0, 200, 70, 30), record(60, 203, 75, 30), record(120, 202, 75, 33, 22), record(180, 203, 76, 33, 22)],
    }


def expect_invalid(sources):
    """Unknown history must raise instead of becoming zero energy or a native fallback."""
    try:
        SharedMeterHistory.combine(sources, START + timedelta(days=1))
    except ValueError:
        return
    raise AssertionError("Invalid shared history accepted")


def counter_cases():
    """Exercise signed export, offsets, resets, asynchronous sources, gaps and daily rollover."""
    sources = history_sources()
    original = copy.deepcopy(sources)
    data = SharedMeterHistory.combine(sources, START + timedelta(days=1))
    assert sources == original
    assert [round(row["consumption"] - data[0]["consumption"], 6) for row in data] == [0, 2, 3, 4]
    assert data[-1]["import"] - data[0]["import"] == 6
    assert data[-1]["export"] - data[0]["export"] == 3
    assert data[-1]["pv"] - data[0]["pv"] == 4
    assert max(row["source_gap_seconds"] for row in data) == 3600
    assert data[-1]["timing_estimated"]
    assert SharedMeterHistory.combine(dict(reversed(list(sources.items()))), START + timedelta(days=1)) == data

    # Only the common 02-10 minute window is observable. Constant known rates give 1.6 kWh.
    skewed = {"a": [record(0, 100, 50, 10), record(10, 104, 55, 10)], "b": [record(2, 200, 70, 30), record(12, 203, 75, 30)]}
    aligned = SharedMeterHistory.combine(skewed, START + timedelta(days=1))
    assert math.isclose(aligned[-1]["consumption"] - aligned[0]["consumption"], 1.6)
    assert aligned[0]["last_updated"] == record(2, 0, 0, 0)["last_updated"]
    assert aligned[-1]["last_updated"] == record(10, 0, 0, 0)["last_updated"]
    # Rounding dips must plateau until recovery, not accumulate artificial reset energy.
    rounding = {"a": [record(0, 100, 50, 10), record(1, 100, 50, 10), record(2, 100.1, 50, 10)], "b": [record(0, 200, 70, 30), record(1, 199.9, 70, 30), record(2, 200.1, 70, 30)]}
    smoothed = SharedMeterHistory.combine(rounding, START + timedelta(days=1))
    assert math.isclose(smoothed[-1]["consumption"] - smoothed[0]["consumption"], 0.2)
    assert smoothed[1]["consumption"] == smoothed[0]["consumption"]
    # Lifetime offsets and local midnight do not reset the combined series.
    shifted = copy.deepcopy(sources)
    for rows in shifted.values():
        for row in rows:
            row["last_updated"] = (datetime.fromisoformat(row["last_updated"]) + timedelta(hours=23)).isoformat()
    midnight = SharedMeterHistory.combine(shifted, START + timedelta(days=2))
    assert midnight[-1]["consumption"] - midnight[0]["consumption"] == 4

    for value in (math.nan, math.inf, True, None, "200"):
        broken = copy.deepcopy(sources)
        broken["inverter_b"][1]["consumption"] = value
        expect_invalid(broken)
    for key in ("import", "export", "pv"):
        broken = copy.deepcopy(sources)
        broken["inverter_b"][2][key] = 0
        expect_invalid(broken)
    broken = copy.deepcopy(sources)
    broken["inverter_b"][1].pop("export")
    expect_invalid(broken)
    expect_invalid({"a": [], "b": sources["inverter_b"]})
    expect_invalid({"a": sources["inverter_a"]})
    broken = copy.deepcopy(sources)
    broken["inverter_b"].append(record(60, 999, 75, 30))
    expect_invalid(broken)
    broken = copy.deepcopy(sources)
    broken["inverter_b"][2]["consumption"] = 0
    expect_invalid(broken)
    # Future packets are excluded, not used as hindsight in a replay.
    truncated = SharedMeterHistory.combine(sources, START + timedelta(minutes=60))
    assert truncated[-1]["consumption"] - truncated[0]["consumption"] == 2


async def download_cases():
    """Use real manual configuration, paginated downloads, raw caching and failure handling."""
    direct = configured()
    direct.config_args["ge_cloud_shared_meter_history"] = True
    data = MockGECloudData()
    data.config_args = direct.config_args
    data.base = SimpleNamespace(components=SimpleNamespace(get_component=lambda name: direct if name == "gecloud" else None))
    data.max_days_previous = 0
    rows = history_sources()
    data._now_utc_exact = START + timedelta(hours=3)
    for serial, records in rows.items():
        row = records[-1]
        direct.meter[serial] = {"time": row["last_updated"], "total": {"consumption": row["consumption"], "grid": {"import": row["import"], "export": row["export"]}, "solar": row["pv"]}}
    data.get_ge_url = AsyncMock(side_effect=[(rows["inverter_a"][:2], "https://api.givenergy.cloud/next"), (rows["inverter_a"][2:], None), (rows["inverter_b"], None)])
    data._mock_storage = SimpleNamespace(load=AsyncMock(return_value=None), save=AsyncMock(return_value=True))
    assert data.shared_meter_members() == ("inverter_a", "inverter_b")
    assert await data.download_ge_data(data.now_utc_exact)
    combined, oldest = data.get_data()
    assert combined[-1]["consumption"] - combined[0]["consumption"] == 4
    assert oldest == START
    assert data.get_ge_url.await_count == 3
    assert data.shared_history_key == ("inverter_a", "inverter_b")
    assert not data.shared_history_error
    data._mock_storage.save.assert_awaited_once()
    direct.devices_dict["battery"].append("inverter_c")
    try:
        data.get_data()
        raise AssertionError("Old cohort accepted after topology changed")
    except ValueError:
        pass
    direct.devices_dict["battery"].pop()
    data.get_ge_url = AsyncMock(side_effect=[(rows["inverter_a"], None), ({}, None)])
    assert not await data.download_ge_data(data.now_utc_exact)
    assert data.get_data()[0] == combined, "A failed refresh must retain a complete, fresh last-good cohort"
    data._now_utc_exact += timedelta(minutes=6)
    try:
        data.get_data()
        raise AssertionError("Stale latest counters must not be treated as zero or current")
    except ValueError:
        pass
    data._now_utc_exact -= timedelta(minutes=6)
    data._now_utc_exact += timedelta(minutes=31)
    try:
        data.get_data()
        raise AssertionError("Last-good historical cohort retained beyond its bounded age")
    except ValueError as exc:
        assert "expired" in str(exc)
    data._now_utc_exact -= timedelta(minutes=31)
    direct.config_args["ge_cloud_automatic_split_ct"] = True
    assert data.shared_meter_members() == ()
    direct.config_args["ge_cloud_automatic_split_ct"] = False
    direct.config_args["ge_cloud_load_today_ignore"] = True
    assert data.shared_meter_members() == ()
    direct.config_args["ge_cloud_load_today_ignore"] = False
    direct.devices_dict["battery"] = ["three_phase_aggregate"]
    assert data.shared_meter_members() == ()
    direct.config_args["ge_cloud_shared_meter_history"] = False
    direct.devices_dict["pv"] = ["additional_pv"]
    assert data.shared_meter_members() == (), "Existing shared-CT users must opt in to reconstructed history"
    return data


def fetch_case():
    """The actual fetch path must retain corrected history instead of overriding it with native load."""
    from unit_test import create_predbat

    base = create_predbat()
    base.now_utc = START + timedelta(hours=3)
    base.minutes_now = 180
    base.max_days_previous = 1
    base.import_export_scaling = 1
    data = MockGECloudData()
    direct = configured(config={"ge_cloud_shared_meter_history": True})
    data.config_args = direct.config_args
    data._now_utc_exact = base.now_utc
    sources = history_sources()
    data.shared_history_snapshot = {"members": ("inverter_a", "inverter_b"), "sources": sources, "downloaded_at": base.now_utc}
    for serial, records in sources.items():
        row = records[-1]
        direct.meter[serial] = {"time": row["last_updated"], "total": {"consumption": row["consumption"], "grid": {"import": row["import"], "export": row["export"]}, "solar": row["pv"]}}
    base.components = SimpleNamespace(get_component=lambda name: data if name == "gecloud_data" else direct if name == "gecloud" else None)
    data.base = SimpleNamespace(components=base.components)
    base.args.update({key: ["sensor.native_must_not_override"] for key in ("load_today", "import_today", "export_today", "pv_today")})
    base.minute_data_load = Mock(side_effect=AssertionError("Native load overrides corrected source"))
    base.minute_data_import_export = Mock(side_effect=AssertionError("Native counters override corrected cohort"))
    assert base.download_ge_data(base.now_utc)
    assert base.load_minutes_source == "ge_cloud_shared_meter"
    assert math.isclose(base.load_minutes_now, 4, abs_tol=0.1)
    assert math.isclose(base.import_today_now, 6, abs_tol=0.1)
    assert math.isclose(base.export_today_now, 3, abs_tol=0.1)
    # A two-minute source lag must use the last observed period, not the held-flat tail.
    base.now_utc += timedelta(minutes=2)
    base.minutes_now += 2
    data._now_utc_exact = base.now_utc
    assert base.download_ge_data(base.now_utc)
    assert base.load_last_period > 0.9


async def startup_and_transition_case():
    """Discovery finishing after startup is retried within one minute, not ten."""
    direct = configured(config={"ge_cloud_shared_meter_history": True})
    direct.devices_dict = {}
    data = MockGECloudData()
    data.config_args = direct.config_args
    data.base = SimpleNamespace(components=SimpleNamespace(get_component=lambda name: direct))
    data._now_utc_exact = START + timedelta(hours=3)
    assert await data.run(0, True)
    assert data.shared_history_error
    data.download_ge_data = AsyncMock(return_value=True)
    assert await data.run(60, False)
    data.download_ge_data.assert_awaited_once()
    # Turning correction off also schedules a native-history refresh within a minute.
    data.shared_history_snapshot = {"members": ("a", "b")}
    data.config_args["ge_cloud_shared_meter_history"] = False
    assert await data.run(120, False)
    assert data.download_ge_data.await_count == 2


def run_shared_history_tests():
    """Run the counter and fetch regressions through the existing GE suite."""
    counter_cases()
    asyncio.run(download_cases())
    asyncio.run(startup_and_transition_case())
    fetch_case()
    return 0

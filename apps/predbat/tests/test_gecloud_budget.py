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
Tests for the GE Cloud API budget harness - the fake GE Cloud, the simulated day and its summary.

The harness exists to count how many GivEnergy cloud API calls Predbat makes in a day (GivEnergy is
introducing a monthly limit), so these tests pin that it counts every call, attributes each one to the
code that made it, and drives the real execute -> gecloud path rather than a shortcut.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from config import CONFIG_ITEMS
from gecloud import GE_API_URL, regname_to_ha
from tests.gecloud_budget_harness import TEMPLATE_INVERTER_ARGS, DayResult, GECloudBudgetHarness, MemoryStorage, summarise, write_call_log
from tests.gecloud_fake_cloud import CALL_CONTEXT, CAPTURED_REGISTERS, HARNESS_SERIAL, HARNESS_SITE_ID, CallRecord, FakeGECloud
from tests.test_infra import run_async
from tests.test_random_scenarios import RANDOM_SCENARIOS, load_scenarios

GECLOUD_ENTITY_STEM = "predbat_gecloud_{}".format(HARNESS_SERIAL.lower())

# The scenarios the baseline runs (ids in coverage/cases/random_scenarios.yaml), chosen to spread plan shapes:
#   0  - IOG dispatch run, negative-price half-hourly import, overnight charge plus some export
#   5  - two cars, dual-rate import and export, 13.5 kWh battery, ~10 kWh export
#   9  - IOG dispatch run, dual-rate import, heavy export (~22 kWh) and charge
#   17 - one car plus IOG, half-hourly (Agile-like) import, heavy export and charge
#   19 - negative-price half-hourly import, 20 kWh battery, the heaviest charging day
DEFAULT_SCENARIO_IDS = (0, 5, 9, 17, 19)
# Written into coverage/, where the tests run; git-ignored
BUDGET_LOG_PATH = "gecloud_api_budget_log.jsonl"


def _fake(soc_percent=50.0):
    """Build a fake cloud on a fixed simulated clock."""
    clock_time = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    fake = FakeGECloud(battery_kwh=9.5, rate_w=3600, clock=lambda: clock_time)
    fake.soc_percent = soc_percent
    return fake


def _request(fake, method, path, body=None, url=None):
    """Make one request through the fake's aiohttp stand-in, the way gecloud does, and return (status, json)."""

    async def go():
        """Open a session and send the request."""
        session_class = fake.session_factory()
        async with session_class(timeout=None) as session:
            target = url if url else GE_API_URL + path
            if method == "GET":
                async with session.get(target, headers={}) as response:
                    return response.status, await response.json()
            if body is None:
                async with session.post(target, headers={}) as response:
                    return response.status, await response.json()
            async with session.post(target, headers={}, json=body) as response:
                return response.status, await response.json()

    return run_async(go())


def _expect(condition, message):
    """Print a failure message and return True when the condition does not hold."""
    if not condition:
        print("ERROR: {}".format(message))
        return True
    return False


def test_register_fixture():
    """The captured register list matches the log it was taken from: 81 registers, one write-only."""
    failed = False
    failed |= _expect(len(CAPTURED_REGISTERS) == 81, "expected 81 captured registers, got {}".format(len(CAPTURED_REGISTERS)))
    ha_names = {regname_to_ha(name) for name, _kind, _value in CAPTURED_REGISTERS}
    for wanted in ("ac_charge_1_start_time", "battery_reserve_percent_limit", "enable_eco_mode", "ac_charge_upper_percent_limit", "pause_battery"):
        failed |= _expect(wanted in ha_names, "captured registers have no {}".format(wanted))
    failed |= _expect(_fake().register_count() == 80, "expected 80 readable registers, got {}".format(_fake().register_count()))
    return failed


def test_routes_status_and_meter():
    """Status carries the fake's SOC and meter carries today/total counters, each recorded under its own category."""
    failed = False
    fake = _fake(soc_percent=42.0)
    status, body = _request(fake, "GET", "inverter/{}/system-data/latest".format(HARNESS_SERIAL))
    failed |= _expect(status == 200, "status endpoint returned {}".format(status))
    failed |= _expect(body["data"]["battery"]["percent"] == 42.0, "status SOC was {}".format(body["data"]["battery"]))
    status, body = _request(fake, "GET", "inverter/{}/meter-data/latest".format(HARNESS_SERIAL))
    failed |= _expect(status == 200, "meter endpoint returned {}".format(status))
    failed |= _expect(isinstance(body["data"].get("today"), dict) and isinstance(body["data"].get("total"), dict), "meter body lacks today/total: {}".format(body))
    failed |= _expect([call.category for call in fake.calls] == ["status", "meter"], "categories were {}".format([call.category for call in fake.calls]))
    return failed


def test_read_returns_written_value():
    """A register write is stored, so the next read returns it, and both are recorded against the register's name."""
    failed = False
    fake = _fake()
    status, body = _request(fake, "POST", "inverter/{}/settings/5/write".format(HARNESS_SERIAL), body={"value": "55", "context": "predbat"})
    failed |= _expect(status == 200 and body["data"]["value"] == "55", "write answered {} {}".format(status, body))
    status, body = _request(fake, "POST", "inverter/{}/settings/5/read".format(HARNESS_SERIAL))
    failed |= _expect(body["data"]["value"] == "55", "read after write returned {}".format(body))
    failed |= _expect([call.category for call in fake.calls] == ["register_write", "register_read"], "categories were {}".format([call.category for call in fake.calls]))
    failed |= _expect(all(call.setting_name == CAPTURED_REGISTERS[4][0] for call in fake.calls), "setting names were {}".format([call.setting_name for call in fake.calls]))
    return failed


def test_written_value_reads_back_typed():
    """Reads return a register's own type after a write, as GE Cloud does - gecloud compares numbers numerically."""
    failed = False
    fake = _fake()
    reserve_id = [index + 1 for index, (name, _kind, _value) in enumerate(CAPTURED_REGISTERS) if name == "Battery Reserve % Limit"][0]
    eco_id = [index + 1 for index, (name, _kind, _value) in enumerate(CAPTURED_REGISTERS) if name == "Enable Eco Mode"][0]
    _request(fake, "POST", "inverter/{}/settings/{}/write".format(HARNESS_SERIAL, reserve_id), body={"value": "55.0", "context": "predbat"})
    _request(fake, "POST", "inverter/{}/settings/{}/write".format(HARNESS_SERIAL, eco_id), body={"value": "False", "context": "predbat"})
    _status, body = _request(fake, "POST", "inverter/{}/settings/{}/read".format(HARNESS_SERIAL, reserve_id))
    failed |= _expect(body["data"]["value"] == 55 and not isinstance(body["data"]["value"], str), "reserve read back as {!r}".format(body["data"]["value"]))
    _status, body = _request(fake, "POST", "inverter/{}/settings/{}/read".format(HARNESS_SERIAL, eco_id))
    failed |= _expect(body["data"]["value"] is False, "eco mode read back as {!r}".format(body["data"]["value"]))
    return failed


def test_device_list_shape():
    """One battery inverter on one site, stamped with the real time so gecloud does not skip it as stale."""
    failed = False
    fake = _fake()
    _status, body = _request(fake, "GET", "communication-device")
    devices = body["data"]
    failed |= _expect(len(devices) == 1, "expected one device, got {}".format(devices))
    inverter = devices[0]["inverter"]
    failed |= _expect(inverter["serial"] == HARNESS_SERIAL and devices[0]["site_id"] == HARNESS_SITE_ID, "device identity was {}".format(devices[0]))
    failed |= _expect(len(inverter["connections"]["batteries"]) == 1, "expected one battery: {}".format(inverter["connections"]))
    stamp = datetime.fromisoformat(inverter["last_updated"].replace("Z", "+00:00"))
    failed |= _expect(abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=1), "last_updated {} is not real now".format(stamp))
    return failed


def test_unmatched_request_raises():
    """Anything the fake does not model fails loudly, and nothing may reach another host."""
    failed = False
    for kwargs in ({"path": "nonsense"}, {"path": "", "url": "https://example.com/v1/account"}):
        try:
            _request(fake=_fake(), method="GET", **kwargs)
            failed |= _expect(False, "request {} did not raise".format(kwargs))
        except AssertionError:
            pass
    return failed


def test_context_recorded():
    """The reason the harness sets is carried onto each record."""
    fake = _fake()
    token = CALL_CONTEXT.set("poll seconds=120")
    try:
        _request(fake, "GET", "account")
    finally:
        CALL_CONTEXT.reset(token)
    return _expect(fake.calls[0].context == "poll seconds=120", "context was {}".format(fake.calls[0].context))


def test_device_info_vs_device_list_category():
    """communication-device serves both the device list and device info, told apart by the gecloud caller."""
    failed = False
    fake = _fake()
    failed |= _expect(fake.categorise("GET", "communication-device", caller="run > async_get_device_info") == "device_info", "device info call was not categorised device_info")
    failed |= _expect(fake.categorise("GET", "communication-device", caller="run > async_get_devices") == "device_list", "device list call was not categorised device_list")
    return failed


def test_startup_finds_device(harness):
    """Start-up finds the one inverter in polling mode and makes the cold-start calls, kept apart from the day."""
    failed = False
    failed |= _expect(harness.gecloud.device_list == [HARNESS_SERIAL.lower()], "device list was {}".format(harness.gecloud.device_list))
    failed |= _expect(harness.gecloud.polling_mode is False, "polling_mode should be off by default, was {}".format(harness.gecloud.polling_mode))
    categories = {call.category for call in harness.startup_calls}
    for wanted in ("account", "device_list", "register_list", "register_read"):
        failed |= _expect(wanted in categories, "start-up made no {} call, made {}".format(wanted, sorted(categories)))
    failed |= _expect(all(call.context.startswith("startup") for call in harness.startup_calls), "a start-up call is not labelled startup")
    return failed


def test_inverter_args_point_at_gecloud(harness):
    """No inverter entity arg is left pointing at the GivTCP template's entities, so every write goes to the fake."""
    failed = False
    args = harness.my_predbat.args
    for arg in TEMPLATE_INVERTER_ARGS:
        if arg not in args:
            continue
        values = args[arg] if isinstance(args[arg], list) else [args[arg]]
        for value in values:
            # Predbat's own emulated entities (sensor.predbat_GEC_0_idle_start_time and the like) cost no API calls
            if isinstance(value, str) and "." in value and not value.startswith("sensor.predbat_"):
                failed |= _expect(GECLOUD_ENTITY_STEM in value, "arg {} still points at {}".format(arg, value))
    leftovers = [arg for arg, value in args.items() if "givtcp" in str(value)]
    failed |= _expect(not leftovers, "args still mention givtcp: {}".format(leftovers))
    failed |= _expect(harness.my_predbat.inverters[0].inverter_type == "GEC", "inverter type was {}".format(harness.my_predbat.inverters[0].inverter_type))
    return failed


def test_config_defaults(harness):
    """The simulated user has the default settings, not the template user's."""
    default = next(item["default"] for item in CONFIG_ITEMS if item["name"] == "set_charge_low_power")
    value = harness.my_predbat.get_arg("set_charge_low_power")
    return _expect(value == default, "set_charge_low_power was {}, default {}".format(value, default))


def test_inverter_reads_fake_soc(harness):
    """The inverter takes its SOC from what gecloud polled from the fake."""
    harness.fake.soc_percent = 37
    with harness.patched():
        run_async(harness.gecloud.run(300, False))
        harness.my_predbat.fetch_inverter_data()
    percent = round(harness.my_predbat.soc_kw / harness.my_predbat.soc_max * 100)
    return _expect(percent == 37, "inverter SOC was {}%".format(percent))


def test_execute_write_reaches_fake(harness):
    """A charge window change from the inverter layer travels HA event -> gecloud -> HTTP, and read-back costs no call."""
    failed = False
    my_predbat = harness.my_predbat
    harness.fake.calls.clear()
    start = harness.sim_time(3 * 60 + 30)
    end = harness.sim_time(4 * 60 + 30)
    with harness.patched():
        my_predbat.inverters[0].adjust_charge_window(start, end, my_predbat.minutes_now)
    writes = [call for call in harness.fake.calls if call.category == "register_write" and call.setting_name == "AC Charge 1 Start Time"]
    failed |= _expect(len(writes) >= 1, "no write reached AC Charge 1 Start Time; calls were {}".format([(call.category, call.setting_name) for call in harness.fake.calls]))
    for call in writes:
        failed |= _expect(call.context.startswith("execute select.{}_ac_charge_1_start_time".format(GECLOUD_ENTITY_STEM)), "write context was {}".format(call.context))
        failed |= _expect("select_event" in call.caller, "write caller was {}".format(call.caller))
    reads = [call for call in harness.fake.calls if call.category == "register_read"]
    failed |= _expect(not reads, "write read-back cost {} API reads".format(len(reads)))
    return failed


def _record(category, minute=0, setting_name="", scenario_id=0):
    """A hand-made CallRecord for the summary tests."""
    when = datetime(2026, 11, 6, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return CallRecord(when, scenario_id, "GET", "endpoint", "path", "", "", setting_name, category, "test", "", 200, False)


def test_summary_maths():
    """Per-day totals project to a 30-day month and are set against the 10k budget; the worst scenario is named."""
    failed = False
    first = DayResult(scenario_id=3, startup_calls=[_record("account")], day_calls=[_record("status")] * 100 + [_record("register_write", setting_name="Battery Reserve % Limit")] * 20, register_count=80, steps=288, recomputes=1)
    second = DayResult(scenario_id=7, startup_calls=[], day_calls=[_record("status")] * 300, register_count=80, steps=288, recomputes=1)
    text = summarise([first, second])
    failed |= _expect("3600" in text, "first scenario's month projection (120 x 30 = 3600) missing from:\n{}".format(text))
    failed |= _expect("36.0%" in text, "3600 / 10000 = 36.0% missing from:\n{}".format(text))
    failed |= _expect("Worst case: scenario 7" in text, "worst case is not scenario 7 in:\n{}".format(text))
    failed |= _expect("write counts are a floor" in text, "ideal-mode caveat missing")
    failed |= _expect("Battery Reserve % Limit" in text, "writes by setting missing")
    return failed


def test_short_day_cadence(day):
    """Over the first hour gecloud polls on its real cadence: status and meter every 5 minutes, device info not again after start-up (it is daily), and no register is read again."""
    failed = False
    counts = {}
    for call in day.day_calls:
        counts[call.category] = counts.get(call.category, 0) + 1
    failed |= _expect(day.steps == 13, "expected 13 executes (00:00..01:00), got {}".format(day.steps))
    for category in ("status", "meter"):
        failed |= _expect(counts.get(category, 0) == 12, "expected 12 {} calls in an hour, got {}".format(category, counts.get(category, 0)))
    failed |= _expect(counts.get("device_info", 0) == 0, "expected no device info call in the first hour, got {}".format(counts.get("device_info", 0)))
    failed |= _expect(counts.get("register_read", 0) == 0, "expected no register reads after start-up in the first hour, got {}".format(counts.get("register_read", 0)))
    return failed


def test_short_day_never_recomputes(day, harness):
    """Advancing the plan never re-optimises it, and the start-of-day forced recompute never fires."""
    failed = False
    failed |= _expect(day.recomputes == 1, "plan was optimised {} times".format(day.recomputes))
    failed |= _expect(not [line for line in harness.recompute_log if "Force recompute" in line], "forced recompute logged: {}".format(harness.recompute_log))
    return failed


def test_fetch_makes_no_calls(day):
    """Reading the inverter and advancing the plan read only what gecloud already published."""
    calls = [call for call in day.day_calls if call.context in ("fetch", "plan advance")]
    return _expect(not calls, "fetch / plan advance made {} API calls: {}".format(len(calls), [(call.context, call.category) for call in calls[:5]]))


def test_call_log_round_trip(day):
    """Every call, start-up included, is written as one JSON line with the CallRecord fields."""
    failed = False
    handle, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(handle)
    try:
        write_call_log([day], path)
        with open(path) as file_handle:
            lines = [json.loads(line) for line in file_handle if line.strip()]
    finally:
        os.remove(path)
    failed |= _expect(len(lines) == len(day.startup_calls) + len(day.day_calls), "log has {} lines for {} calls".format(len(lines), len(day.startup_calls) + len(day.day_calls)))
    failed |= _expect(all(set(line) == set(day.day_calls[0].to_dict()) for line in lines), "a log line lacks the CallRecord fields")
    return failed


def test_scenario_state_follows_clock(day, harness):
    """After an hour the plan still models the scenario's system, and its load history and PV run from the current time."""
    failed = False
    my_predbat = harness.my_predbat
    params = harness.scenario["params"]
    data = harness.scenario["data"]
    for attribute, wanted in (
        ("export_limit", params["inverter"]["export_limit_kw"] / 60.0),
        ("inverter_limit", params["inverter"]["inverter_limit_kw"] / 60.0),
        ("battery_rate_max_discharge", params["battery"]["rate_max_discharge_kw"] / 60.0),
        ("battery_rate_max_charge", params["battery"]["rate_max_charge_kw"] / 60.0),
    ):
        got = getattr(my_predbat, attribute)
        failed |= _expect(abs(got - wanted) < 1e-6, "{} drifted to {} from the scenario's {}".format(attribute, got, wanted))
    minute = my_predbat.minutes_now
    last_five = my_predbat.load_minutes[0] - my_predbat.load_minutes[5]
    wanted_load = sum(data["load_day_kwh"][(minute - offset) % 1440] for offset in range(5))
    failed |= _expect(abs(last_five - wanted_load) < 1e-6, "load history at minute {} gives {} for the last 5 minutes, scenario says {}".format(minute, last_five, wanted_load))
    failed |= _expect((minute + my_predbat.forecast_minutes) in my_predbat.pv_forecast_minute, "PV forecast does not reach minute {}".format(minute + my_predbat.forecast_minutes))
    return failed


def test_sweep_reason_names_branch(day):
    """Each register read says why gecloud read it, not just the poll time."""
    reads = [call for call in day.startup_calls if call.category == "register_read"]
    unlabelled = [call.context for call in reads if "sweep: start-up" not in call.context]
    return _expect(reads and not unlabelled, "{} of {} sweep reads lack the sweep reason, e.g. {}".format(len(unlabelled), len(reads), unlabelled[:2]))


def test_no_write_retry_loop(day):
    """No register is written more than twice in one cycle - a repeat means execute's write-and-poll is retrying a write the inverter refused."""
    per_cycle = {}
    for call in day.day_calls:
        if call.category == "register_write":
            key = (call.sim_time.strftime("%H:%M"), call.setting_name)
            per_cycle[key] = per_cycle.get(key, 0) + 1
    loops = {key: count for key, count in per_cycle.items() if count > 2}
    return _expect(not loops, "write-and-poll retry loops: {}".format(sorted(loops.items())[:5]))


def test_day_tail_polls(tail_day):
    """Polling continues for the 5 minutes after the last execute, so a full run covers a whole 24 hours of gecloud's loop."""
    counts = {}
    for call in tail_day.day_calls:
        counts[call.category] = counts.get(call.category, 0) + 1
    return _expect(counts.get("status", 0) == 13 and tail_day.steps == 13, "expected 13 status polls to minute 65 with 13 executes, got {} and {}".format(counts.get("status", 0), tail_day.steps))


# The registers Predbat drives on the captured inverter - the ones ge_cloud_automatic wires into apps.yaml
CONTROL_NAMES = {
    "Enable Eco Mode",
    "Battery Charge Power",
    "Battery Discharge Power",
    "Battery Reserve % Limit",
    "AC Charge 1 Start Time",
    "AC Charge 1 End Time",
    "AC Charge Upper % Limit",
    "Enable AC Charge Upper % Limit",
    "DC Discharge 1 Start Time",
    "DC Discharge 1 End Time",
    "AC Charge Enable",
    "Enable DC Discharge",
    "Pause Battery",
    "Pause Battery Start Time",
    "Pause Battery End Time",
    "DC Discharge 1 Lower SOC % Limit",
}


def _reads(calls):
    """The register names read by these calls, in order."""
    return [call.setting_name for call in calls if call.category == "register_read"]


def _polled(harness, first_minute, last_minute):
    """Run gecloud's own loop over the minutes given and return the calls it made (the plan is not involved)."""
    harness.fake.calls.clear()
    with harness.patched():
        harness.poll(first_minute, last_minute)
    return list(harness.fake.calls)


def _started(scenario, storage=None, args=None):
    """A harness started at midnight, optionally with a settings cache and extra apps.yaml args."""
    harness = GECloudBudgetHarness(scenario, storage=storage, extra_args=args)
    harness.startup()
    return harness


def _cache_from(harness, full_age_days):
    """A storage holding the harness's settings, as if a full read had happened full_age_days ago."""
    storage = MemoryStorage()
    stamp = (harness.midnight - timedelta(days=full_age_days)).isoformat()
    # Saved as gecloud saves them, so the cache holds what real storage would hand back
    run_async(storage.save("gecloud", "settings", harness.gecloud.settings, format="json"))
    run_async(storage.save("gecloud", "settings_read", {serial: {"full": stamp, "control": stamp} for serial in harness.gecloud.settings}, format="json"))
    return storage


def test_cold_start_reads_everything_once(scenario):
    """With nothing stored, start-up reads every register once; the next hours read none."""
    failed = False
    harness = _started(scenario)
    failed |= _expect(len(_reads(harness.startup_calls)) == harness.fake.register_count(), "cold start read {} registers, expected {}".format(len(_reads(harness.startup_calls)), harness.fake.register_count()))
    reads = _reads(_polled(harness, 1, 11 * 60))
    failed |= _expect(not reads, "the first 11 hours read {} registers, expected none".format(len(reads)))
    return failed


def test_device_info_daily(scenario):
    """Device info (battery size, rates, model) is fetched at start-up and then once a day; status and meter every 5 minutes."""
    failed = False
    harness = _started(scenario)
    failed |= _expect(sum(1 for call in harness.startup_calls if call.category == "device_info") == 1, "start-up should fetch device info once")
    calls = _polled(harness, 1, 24 * 60)
    info = [call.sim_time.strftime("%H:%M") for call in calls if call.category == "device_info"]
    failed |= _expect(info == ["00:00"], "device info should be fetched once, at the 24 hour mark, got {}".format(info))
    status = sum(1 for call in calls if call.category == "status")
    failed |= _expect(status == 288, "expected 288 status polls in 24 hours, got {}".format(status))
    return failed


def test_control_registers_every_12_hours(scenario):
    """At 12 hours only the registers Predbat drives are read again."""
    harness = _started(scenario)
    _polled(harness, 1, 12 * 60 - 1)
    reads = _reads(_polled(harness, 12 * 60, 12 * 60))
    return _expect(sorted(reads) == sorted(CONTROL_NAMES), "the 12-hour read covered {} (extra {}, missing {})".format(len(reads), sorted(set(reads) - CONTROL_NAMES), sorted(CONTROL_NAMES - set(reads))))


def test_warm_start_reads_controls_only(scenario):
    """A cache whose full read is under 7 days old is trusted: start-up re-reads only the control registers."""
    cache = _cache_from(_started(scenario), full_age_days=6)
    harness = _started(scenario, storage=cache)
    reads = _reads(harness.startup_calls)
    return _expect(sorted(reads) == sorted(CONTROL_NAMES), "warm start read {} registers, expected the {} control registers".format(len(reads), len(CONTROL_NAMES)))


def test_stale_cache_reads_everything(scenario):
    """A cache whose last full read is over 7 days old is read again in full."""
    cache = _cache_from(_started(scenario), full_age_days=8)
    harness = _started(scenario, storage=cache)
    reads = _reads(harness.startup_calls)
    return _expect(len(reads) == harness.fake.register_count(), "stale cache start read {} registers, expected {}".format(len(reads), harness.fake.register_count()))


def test_failed_full_read_keeps_cached_values(scenario):
    """A register whose read times out during a full read keeps the value restored from the cache, rather than becoming None."""
    failed = False
    first = _started(scenario)
    serial = HARNESS_SERIAL.lower()
    setting_id = next(sid for sid, name in first.fake.register_names.items() if name == "AC Charge 1 Start Time")
    cached_value = first.gecloud.settings[serial][setting_id]["value"]
    cache = _cache_from(first, full_age_days=8)

    harness = GECloudBudgetHarness(scenario, storage=cache, read_timeouts=[setting_id])
    harness.startup()
    settings = harness.gecloud.settings[serial]
    failed |= _expect(all(isinstance(key, int) for key in settings), "restored settings keep string register ids: {}".format(sorted(str(key) for key in settings if not isinstance(key, int))[:5]))
    value = settings.get(setting_id, {}).get("value", None)
    failed |= _expect(value == cached_value, "AC Charge 1 Start Time read timed out and became {}, expected the cached {}".format(value, cached_value))
    return failed


def test_full_read_after_7_days_running(scenario):
    """A Predbat left running reads everything again once its last full read is 7 days old."""
    harness = _started(scenario)
    harness.gecloud.sim_now = harness.midnight
    stamps = harness.gecloud.settings_read_stamps[HARNESS_SERIAL.lower()]
    stamps["full"] = harness.midnight - timedelta(days=7)
    stamps["control"] = harness.midnight
    reads = _reads(_polled(harness, 1, 10))
    return _expect(len(reads) == harness.fake.register_count(), "a 7-day-old full read was followed by {} reads, expected {}".format(len(reads), harness.fake.register_count()))


def test_control_failure_rereads_once_per_4_hours(scenario):
    """A control failure re-reads the control registers once; more failures within 4 hours do not."""
    failed = False
    harness = _started(scenario)
    # One quiet minute first: execute's midnight writes are answered from gecloud's pending writes until the next loop ends
    _polled(harness, 1, 1)
    monitor = harness.my_predbat.control_monitor
    monitor.total += 1
    reads = _reads(_polled(harness, 2, 2))
    failed |= _expect(sorted(reads) == sorted(CONTROL_NAMES), "a control failure re-read {} registers, expected the control set".format(len(reads)))
    monitor.total += 1
    reads = _reads(_polled(harness, 3, 4 * 60 + 1))
    failed |= _expect(not reads, "a second failure within 4 hours re-read {} registers".format(len(reads)))
    monitor.total += 1
    reads = _reads(_polled(harness, 4 * 60 + 2, 4 * 60 + 2))
    failed |= _expect(sorted(reads) == sorted(CONTROL_NAMES), "a failure after 4 hours re-read {} registers, expected the control set".format(len(reads)))
    return failed


def test_leaving_read_only_rereads_controls(scenario):
    """Coming out of read only re-reads the control registers, which may have been changed meanwhile."""
    failed = False
    harness = _started(scenario)
    _polled(harness, 1, 1)
    harness.my_predbat.set_read_only = True
    reads = _reads(_polled(harness, 2, 5))
    failed |= _expect(not reads, "entering read only read {} registers".format(len(reads)))
    harness.my_predbat.set_read_only = False
    reads = _reads(_polled(harness, 6, 6))
    failed |= _expect(sorted(reads) == sorted(CONTROL_NAMES), "leaving read only re-read {} registers, expected the control set".format(len(reads)))
    return failed


def test_leaving_read_only_restores_eco(scenario):
    """An inverter that exports at full power in eco mode has eco put back on when Predbat leaves read only, in case it was turned off meanwhile."""
    failed = False
    harness = _started(scenario)
    eco_id = [index + 1 for index, (name, _kind, _value) in enumerate(CAPTURED_REGISTERS) if name == "Enable Eco Mode"][0]
    _polled(harness, 1, 1)
    harness.my_predbat.set_read_only = True
    harness.fake.register_values[eco_id] = False
    _polled(harness, 2, 5)
    harness.my_predbat.set_read_only = False
    calls = _polled(harness, 6, 6)
    failed |= _expect("Enable Eco Mode" in _reads(calls), "leaving read only did not re-read Enable Eco Mode")
    writes = [call for call in calls if call.category == "register_write" and call.setting_name == "Enable Eco Mode"]
    failed |= _expect(len(writes) == 1 and harness.fake.register_values[eco_id] is True, "eco should be written back on once, got {} writes, value {}".format(len(writes), harness.fake.register_values[eco_id]))
    return failed


def test_poll_settings_opt_in(scenario):
    """ge_cloud_poll_settings brings back the full sweep every 10 minutes."""
    harness = _started(scenario, args={"ge_cloud_poll_settings": True})
    reads = _reads(_polled(harness, 1, 10))
    return _expect(len(reads) == harness.fake.register_count(), "with ge_cloud_poll_settings the 10-minute sweep read {}, expected {}".format(len(reads), harness.fake.register_count()))


def run_gecloud_budget_harness_tests(my_predbat):
    """Unit tests for the GE Cloud API budget harness. Returns True on failure."""
    print("**** Running GE Cloud API budget harness tests ****")
    failed = False
    for test in (
        test_register_fixture,
        test_routes_status_and_meter,
        test_read_returns_written_value,
        test_written_value_reads_back_typed,
        test_device_list_shape,
        test_unmatched_request_raises,
        test_context_recorded,
        test_device_info_vs_device_list_category,
        test_summary_maths,
    ):
        if test():
            print("ERROR: {} failed".format(test.__name__))
            failed = True

    # One harness start-up shared by the start-up tests: it loads the scenario template and plans once
    scenario = [item for item in load_scenarios(RANDOM_SCENARIOS) if item["id"] == 0][0]
    harness = GECloudBudgetHarness(scenario)
    harness.startup()
    for test in (
        test_startup_finds_device,
        test_inverter_args_point_at_gecloud,
        test_config_defaults,
        test_inverter_reads_fake_soc,
        test_execute_write_reaches_fake,
    ):
        if test(harness):
            print("ERROR: {} failed".format(test.__name__))
            failed = True

    # The first simulated hour, on a fresh harness
    short = GECloudBudgetHarness(scenario)
    day = short.simulate_day(last_minute=60, poll_until=60)
    for test in (test_short_day_cadence, test_fetch_makes_no_calls, test_call_log_round_trip, test_sweep_reason_names_branch, test_no_write_retry_loop):
        if test(day):
            print("ERROR: {} failed".format(test.__name__))
            failed = True
    for test in (test_short_day_never_recomputes, test_scenario_state_follows_clock):
        if test(day, short):
            print("ERROR: {} failed".format(test.__name__))
            failed = True

    tail_day = GECloudBudgetHarness(scenario).simulate_day(last_minute=60, poll_until=65)
    if test_day_tail_polls(tail_day):
        print("ERROR: test_day_tail_polls failed")
        failed = True

    # Register polling rules, driven through gecloud's own loop
    for test in (
        test_cold_start_reads_everything_once,
        test_device_info_daily,
        test_control_registers_every_12_hours,
        test_warm_start_reads_controls_only,
        test_stale_cache_reads_everything,
        test_failed_full_read_keeps_cached_values,
        test_full_read_after_7_days_running,
        test_control_failure_rereads_once_per_4_hours,
        test_leaving_read_only_rereads_controls,
        test_leaving_read_only_restores_eco,
        test_poll_settings_opt_in,
    ):
        if test(scenario):
            print("ERROR: {} failed".format(test.__name__))
            failed = True
    return failed


def run_gecloud_api_budget(my_predbat):
    """
    Slow: simulate a full day for each default scenario and report the GE Cloud API calls it cost (the baseline).

    Deliberately asserts no budget - current main would fail any sensible one. Returns True only if a
    simulation raised, so the run itself is checked but the numbers are reported, not judged.
    """
    print("**** Running GE Cloud API budget baseline ****")
    scenarios = {item["id"]: item for item in load_scenarios(RANDOM_SCENARIOS)}
    results = []
    for scenario_id in DEFAULT_SCENARIO_IDS:
        print("Simulating scenario {}".format(scenario_id))
        results.append(GECloudBudgetHarness(scenarios[scenario_id]).simulate_day())
    print(summarise(results))
    write_call_log(results, BUDGET_LOG_PATH)
    print("Per-call log written to {}".format(BUDGET_LOG_PATH))
    return False

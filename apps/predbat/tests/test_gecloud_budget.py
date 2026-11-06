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

from datetime import datetime, timedelta, timezone

from config import CONFIG_ITEMS
from gecloud import GE_API_URL, regname_to_ha
from tests.gecloud_budget_harness import TEMPLATE_INVERTER_ARGS, GECloudBudgetHarness
from tests.gecloud_fake_cloud import CALL_CONTEXT, CAPTURED_REGISTERS, HARNESS_SERIAL, HARNESS_SITE_ID, FakeGECloud
from tests.test_infra import run_async
from tests.test_random_scenarios import RANDOM_SCENARIOS, load_scenarios

GECLOUD_ENTITY_STEM = "predbat_gecloud_{}".format(HARNESS_SERIAL.lower())


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
    failed |= _expect(harness.gecloud.polling_mode is True, "polling_mode was {}".format(harness.gecloud.polling_mode))
    categories = {call.category for call in harness.startup_calls}
    for wanted in ("account", "device_list", "register_list", "register_read"):
        failed |= _expect(wanted in categories, "start-up made no {} call, made {}".format(wanted, sorted(categories)))
    failed |= _expect(all(call.context == "startup" for call in harness.startup_calls), "a start-up call is not labelled startup")
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
        run_async(harness.gecloud.run(120, False))
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


def run_gecloud_budget_harness_tests(my_predbat):
    """Unit tests for the GE Cloud API budget harness. Returns True on failure."""
    print("**** Running GE Cloud API budget harness tests ****")
    failed = False
    for test in (
        test_register_fixture,
        test_routes_status_and_meter,
        test_read_returns_written_value,
        test_device_list_shape,
        test_unmatched_request_raises,
        test_context_recorded,
        test_device_info_vs_device_list_category,
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
    return failed

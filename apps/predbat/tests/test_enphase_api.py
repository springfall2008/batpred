# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Enphase API functions
# -----------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone
import base64
import json as json_module
import pytz
from enphase import EnphaseAPI, ENPHASE_REFRESH_SETTINGS
from tests.test_infra import run_async


def _b64(payload):
    """Base64url-encode a dict as a JWT payload segment without padding."""
    raw = json_module.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class MockBase:
    """Mock base object for ComponentBase properties in Enphase API tests."""

    def __init__(self):
        """Initialise MockBase with default config."""
        self.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.config = {}

    def get_arg(self, key, default=None, **kwargs):
        """Return config value or default."""
        return self.config.get(key, default)


class MockEnphaseAPI(EnphaseAPI):
    """Mock EnphaseAPI that avoids ComponentBase construction and real HTTP."""

    def __init__(self):
        """Set up the mock without calling ComponentBase.__init__."""
        self.prefix = "predbat"
        self.base = MockBase()
        self.local_tz = pytz.timezone("Europe/London")
        # Note: "storage" is a read-only property on ComponentBase (base.components.get_component("storage")),
        # so it cannot be assigned directly here. MockBase has no "components" attribute, so the inherited
        # property naturally evaluates to None for these tests.
        self.api_started = False
        self.initialize(username="user@example.com", password="secret")

        # Test instrumentation
        self.http_responses = {}  # path -> dict(status, json_data, text_data)
        self.request_log = []
        self.dashboard_items = {}
        self.mock_ha_states = {}
        self.args_set = {}
        self.fatal_signalled = False

    def log(self, message):
        """Swallow log output in tests."""
        pass

    def update_success_timestamp(self):
        """Swallow health-tracking in tests."""
        pass

    def fatal_error_occurred(self):
        """Record that a fatal error was signalled, for test assertions."""
        self.fatal_signalled = True

    def dashboard_item(self, entity_id, state, attributes, app=None):
        """Record dashboard items instead of publishing to HA."""
        self.dashboard_items[entity_id] = {"state": state, "attributes": attributes, "app": app}

    def get_state_wrapper(self, entity_id, default=None):
        """Return a mocked HA state."""
        return self.mock_ha_states.get(entity_id, default)

    def set_arg(self, key, value):
        """Record args set by automatic_config."""
        self.args_set[key] = value

    def set_http_response(self, path, status=200, json_data=None, text_data=None):
        """Prime a canned HTTP response for a URL path."""
        self.http_responses[path] = {"status": status, "json_data": json_data, "text_data": text_data}

    async def request_raw(self, method, url, headers=None, data=None, json_body=None, params=None):
        """Return canned responses instead of performing HTTP."""
        path = url.split("enphaseenergy.com", 1)[-1].split("?")[0]
        self.request_log.append({"method": method, "path": path, "json": json_body, "data": data})
        response = self.http_responses.get(path, {"status": 404, "json_data": None, "text_data": "not found"})
        return response["status"], response["json_data"], response.get("text_data") or "", {}


def test_initialize_defaults():
    """initialize() must set all state fields with correct defaults."""
    api = MockEnphaseAPI()
    assert api.username == "user@example.com"
    assert api.password == "secret"
    assert api.site_id is None
    assert api.automatic is False
    assert api.sites == []
    assert api.battery_status == {}
    assert api.schedules == {}
    assert api.data_age == {}
    assert api.login_reject_count == 0


def test_needs_refresh():
    """_needs_refresh returns True when data is absent or stale, False when fresh."""
    api = MockEnphaseAPI()
    assert api._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS) is True
    api.data_age["battery_status"] = datetime.now(timezone.utc)
    assert api._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS) is False
    api.data_age["battery_status"] = datetime.now(timezone.utc) - timedelta(minutes=ENPHASE_REFRESH_SETTINGS + 1)
    assert api._needs_refresh("battery_status", ENPHASE_REFRESH_SETTINGS) is True


def test_is_alive():
    """is_alive requires api_started and at least one discovered site."""
    api = MockEnphaseAPI()
    assert not api.is_alive()
    api.api_started = True
    assert not api.is_alive()
    api.sites = [{"site_id": "12345"}]
    assert api.is_alive()


def test_login_success():
    """Successful login mints tokens, extracts user id and discovers sites."""
    api = MockEnphaseAPI()
    # JWT with payload {"user_id": "9999", "exp": 4102444800} (header/sig irrelevant, unverified decode)
    jwt = "eyJhbGciOiJIUzI1NiJ9." + _b64({"user_id": "9999", "exp": 4102444800}) + ".sig"
    api.set_http_response("/login/login.json", 200, {"success": True, "session_id": "sess1"})
    api.set_http_response("/users/self/token", 200, {"token": jwt, "expires_at": 4102444800})
    api.set_http_response("/app-api/search_sites.json", 200, [{"site_id": 12345, "name": "Home"}])
    assert run_async(api.login()) is True
    assert api.eauth_token == jwt
    assert api.user_id == "9999"
    assert api.sites[0]["site_id"] == "12345"
    assert api.login_reject_count == 0


def test_login_mfa_rejected():
    """MFA-required accounts must fail with a fatal error immediately, not retry."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 200, {"requires_mfa": True})
    assert run_async(api.login()) is False
    assert api.login_reject_count == 1
    assert api.login_cooldown_until is not None
    assert api.fatal_signalled is True


def test_login_transient_rejection_not_fatal():
    """A single transient 401 must set a cooldown but must NOT signal a fatal, app-wide error."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 401, None)
    assert run_async(api.login()) is False
    assert api.login_reject_count == 1
    assert api.login_cooldown_until is not None
    assert api.fatal_signalled is False


def test_login_guard_rails():
    """Three consecutive rejections suspend login for 24 hours and only then signal fatal."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 401, None)
    for i in range(3):
        api.login_cooldown_until = None  # expire cooldown to allow next attempt
        run_async(api.login())
        if i == 0:
            # A single transient rejection must not be fatal on its own.
            assert api.fatal_signalled is False
    assert api.login_reject_count == 3
    assert api.fatal_signalled is True
    remaining = (api.login_cooldown_until - datetime.now(timezone.utc)).total_seconds()
    assert remaining > 23 * 3600
    # While suspended, login() refuses without making a request
    count = len(api.request_log)
    assert run_async(api.login()) is False
    assert len(api.request_log) == count


def test_login_reuse_window():
    """A login success within 30 seconds is reused, not repeated."""
    api = MockEnphaseAPI()
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    count = len(api.request_log)
    assert run_async(api.login()) is True
    assert len(api.request_log) == count


def test_get_headers_site():
    """Site-family headers carry cookie, tokens and browser mimicry."""
    api = MockEnphaseAPI()
    api.cookie_header = "a=b"
    api.eauth_token = "tok"
    api.xsrf_token = "xs"
    headers = api.get_headers("site")
    assert headers["Cookie"] == "a=b"
    assert headers["e-auth-token"] == "tok"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-CSRF-Token"] == "xs"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert "Mozilla" in headers["User-Agent"]


def test_get_headers_battery_config():
    """BatteryConfig headers use the battery-profile-ui origin, manager token bearer and user id."""
    api = MockEnphaseAPI()
    api.eauth_token = "etok"
    api.manager_token = "mtok"
    api.user_id = "9999"
    headers = api.get_headers("battery_config", write=True)
    assert headers["Origin"] == "https://battery-profile-ui.enphaseenergy.com"
    assert headers["Authorization"] == "Bearer mtok"
    assert headers["e-auth-token"] == "etok"
    assert headers["Username"] == "9999"
    assert "requestid" in headers


def test_request_json_success():
    """request_json returns parsed JSON and counts the request."""
    api = MockEnphaseAPI()
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, {"storages": []})
    result = run_async(api.request_json("GET", "/pv/settings/12345/battery_status.json"))
    assert result == {"storages": []}
    assert api.requests_today == 1


def test_request_json_401_relogin():
    """A 401 triggers one re-login and one retry."""
    api = MockEnphaseAPI()
    api.eauth_token = "expired"
    calls = {"n": 0}

    async def fake_raw(method, url, headers=None, data=None, json_body=None, params=None):
        """Return 401 once then 200, and 200 for the login chain."""
        path = url.split("enphaseenergy.com", 1)[-1].split("?")[0]
        api.request_log.append({"method": method, "path": path})
        if path == "/login/login.json":
            return 200, {"success": True, "session_id": "s"}, "", {}
        if path == "/users/self/token":
            return 200, {"token": "newtok"}, "", {}
        if path == "/app-api/search_sites.json":
            return 200, [{"site_id": 12345, "name": "Home"}], "", {}
        calls["n"] += 1
        if calls["n"] == 1:
            return 401, None, "", {}
        return 200, {"ok": True}, "", {}

    api.request_raw = fake_raw
    result = run_async(api.request_json("GET", "/some/data.json"))
    assert result == {"ok": True}
    assert api.eauth_token == "newtok"


def test_request_json_login_wall():
    """An HTML body on a JSON endpoint is treated as auth failure, not a crash."""
    api = MockEnphaseAPI()
    api.login_cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)  # block re-login
    api.set_http_response("/some/data.json", 200, None, text_data="<!DOCTYPE html><html>login</html>")
    result = run_async(api.request_json("GET", "/some/data.json"))
    assert result is None


def test_battery_config_variant_fallback():
    """A BatteryConfig auth failure switches header variant before re-logging in."""
    api = MockEnphaseAPI()
    api.eauth_token = "tok"
    calls = {"n": 0}

    async def fake_raw(method, url, headers=None, data=None, json_body=None, params=None):
        """Reject the primary variant once, accept the cookie variant."""
        calls["n"] += 1
        if "requestid" in (headers or {}):
            return 401, None, "", {}
        return 200, {"ok": True}, "", {}

    api.request_raw = fake_raw
    result = run_async(api.request_json("GET", "/service/batteryConfig/api/v1/profile/12345", family="battery_config"))
    assert result == {"ok": True}
    assert api.battery_config_variant == "cookie_eauth"
    assert calls["n"] == 2


BATTERY_STATUS_PAYLOAD = {
    "current_charge": 55,
    "available_energy": 5.5,
    "max_capacity": 10.0,
    "max_power": 3.84,
    "storages": [
        {"id": 1, "serial_num": "B1", "current_charge": 50, "available_energy": 2.5, "max_capacity": 5.0, "status": "normal"},
        {"id": 2, "serial_num": "B2", "current_charge": 60, "available_energy": 3.0, "max_capacity": 5.0, "status": "normal"},
    ],
}


def test_get_battery_status():
    """battery_status parses site totals and capacity-weighted SOC."""
    api = MockEnphaseAPI()
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, BATTERY_STATUS_PAYLOAD)
    run_async(api.get_battery_status("12345"))
    status = api.battery_status["12345"]
    assert status["max_capacity"] == 10.0
    assert status["soc_percent"] == 55.0  # (2.5+3.0)/(5+5)*100
    assert status["max_power_kw"] == 3.84


def test_energy_today():
    """energy_today returns the final (today's) entry of a channel array."""
    payload = {"start_date": "2026-07-09", "last_report_date": "2026-07-11", "production": [10.0, 12.0, 3.5], "consumption": [8.0, 9.0, 2.2]}
    from enphase import energy_today

    assert energy_today(payload, "production") == 3.5
    assert energy_today(payload, "consumption") == 2.2
    assert energy_today(payload, "import") == 0.0  # missing channel -> 0


def test_get_schedules_parses_families():
    """Schedules read stores cfg/dtg/rbd entries and dtg support flag."""
    api = MockEnphaseAPI()
    payload = {
        "cfg": {"scheduleSupported": True, "details": [{"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "isEnabled": True}]},
        "dtg": {"scheduleSupported": False, "details": []},
        "rbd": {"scheduleSupported": True, "details": []},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, payload)
    run_async(api.get_schedules("12345"))
    cfg = api.schedules["12345"]["cfg"]
    assert cfg["id"] == "u1" and cfg["limit"] == 90 and cfg["enabled"] is True
    assert api.dtg_supported("12345") is False


def test_run_first_polls_all_tiers():
    """First run() logs in, fetches every tier and publishes."""
    api = MockEnphaseAPI()
    # prime auth short-circuit
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, BATTERY_STATUS_PAYLOAD)
    api.set_http_response("/pv/systems/12345/lifetime_energy", 200, {"production": [1.0], "consumption": [1.0], "import": [0.5], "export": [0.2], "charge": [0.1], "discharge": [0.1], "start_date": "2026-07-11"})
    api.set_http_response("/app-api/12345/get_latest_power", 200, {"latest_power": {"value": 450, "units": "w", "time": 1760000000}})
    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {"profile": "self-consumption", "batteryBackupPercentage": 20})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {"chargeFromGrid": True, "veryLowSoc": 10, "veryLowSocMin": 5, "veryLowSocMax": 25})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    result = run_async(api.run(0, True))
    assert result
    assert api.battery_status["12345"]["soc_percent"] == 55.0
    assert api.profile["12345"]["reserve"] == 20
    assert api.latest_power["12345"]["watts"] == 450


def test_derive_power():
    """derive_power converts kWh deltas over elapsed time into watts."""
    from enphase import derive_power

    now = datetime.now(timezone.utc)
    prev = (1.0, now - timedelta(minutes=5))
    watts, sample = derive_power(prev, 1.1, now)
    assert abs(watts - 1200.0) < 1.0  # 0.1 kWh in 5 min = 1.2 kW
    assert sample == (1.1, now)
    # Negative delta (midnight reset) clamps to zero
    watts, _ = derive_power((5.0, now - timedelta(minutes=5)), 0.0, now)
    assert watts == 0.0
    # No previous sample yields zero
    watts, _ = derive_power(None, 2.0, now)
    assert watts == 0.0


def test_publish_data_sensors():
    """publish_data creates the full monitoring sensor set."""
    api = MockEnphaseAPI()
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSoc": 10, "veryLowSocMin": 5, "veryLowSocMax": 25}
    api.lifetime_energy["12345"] = {"production": [3.5], "consumption": [2.2], "import": [1.0], "export": [0.4], "charge": [0.8], "discharge": [0.6]}
    api.latest_power["12345"] = {"watts": 450.0, "time": 1760000000}
    run_async(api.publish_data("12345"))
    items = api.dashboard_items
    assert items["sensor.predbat_enphase_12345_soc_percent"]["state"] == 55.0
    assert items["sensor.predbat_enphase_12345_battery_capacity"]["state"] == 10.0
    assert items["sensor.predbat_enphase_12345_battery_rate_max"]["state"] == 3840.0
    assert items["sensor.predbat_enphase_12345_pv_today"]["state"] == 3.5
    assert items["sensor.predbat_enphase_12345_load_today"]["state"] == 2.2
    assert items["sensor.predbat_enphase_12345_import_today"]["state"] == 1.0
    assert items["sensor.predbat_enphase_12345_export_today"]["state"] == 0.4
    assert items["sensor.predbat_enphase_12345_load_power"]["state"] == 450.0
    assert items["sensor.predbat_enphase_12345_battery_reserve_min"]["state"] == 5
    assert "sensor.predbat_enphase_12345_pv_power" in items


def test_publish_schedule_entities():
    """Control entities are published for charge, and export only when dtg supported."""
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": False}, "rbd": {"supported": True}}
    api.battery_settings["12345"] = {"veryLowSocMin": 5}
    run_async(api.publish_schedule_settings_ha("12345"))
    items = api.dashboard_items
    assert "select.predbat_enphase_12345_battery_schedule_charge_start_time" in items
    assert "number.predbat_enphase_12345_battery_schedule_charge_soc" in items
    assert "switch.predbat_enphase_12345_battery_schedule_charge_write" in items
    assert "number.predbat_enphase_12345_battery_schedule_reserve" in items
    assert "select.predbat_enphase_12345_battery_schedule_export_start_time" not in items


def test_event_handlers_update_local_schedule():
    """select/number/switch events mutate the local schedule model."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False},
        "freeze": {"enable": False},
    }
    run_async(api.select_event("select.predbat_enphase_12345_battery_schedule_charge_start_time", "02:30:00"))
    assert api.local_schedule["12345"]["charge"]["start_time"] == "02:30:00"
    run_async(api.number_event("number.predbat_enphase_12345_battery_schedule_charge_soc", 85))
    assert api.local_schedule["12345"]["charge"]["soc"] == 85
    run_async(api.switch_event("switch.predbat_enphase_12345_battery_schedule_charge_enable", "turn_on"))
    assert api.local_schedule["12345"]["charge"]["enable"] is True


def test_write_switch_triggers_apply():
    """Turning on the write switch calls apply_battery_schedule for the site."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    applied = []

    async def fake_apply(site_id):
        """Record the applied site."""
        applied.append(site_id)

    api.apply_battery_schedule = fake_apply
    run_async(api.switch_event("switch.predbat_enphase_12345_battery_schedule_charge_write", "turn_on"))
    assert applied == ["12345"]


def test_schedules_equal():
    """schedules_equal compares window, limit and enable state."""
    from enphase import schedules_equal

    cloud = {"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True}
    assert schedules_equal(cloud, "02:00", "05:00", 90, True)
    assert not schedules_equal(cloud, "02:00", "05:30", 90, True)
    assert not schedules_equal(cloud, "02:00", "05:00", 80, True)
    assert not schedules_equal(cloud, "02:00", "05:00", 90, False)
    assert not schedules_equal(None, "02:00", "05:00", 90, True)


def test_apply_charge_schedule_creates():
    """apply writes a CFG schedule via POST when none exists, enabling charge-from-grid first."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": False, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/12345", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    assert len(posts) == 1
    body = posts[0]["json"]
    assert body["scheduleType"] == "CFG" and body["startTime"] == "02:00" and body["endTime"] == "05:00" and body["limit"] == 90 and body["isEnabled"] is True
    disclaimers = [r for r in api.request_log if "acceptDisclaimer" in r["path"]]
    assert len(disclaimers) == 1


def test_apply_updates_existing_by_id():
    """apply uses PUT /schedules/<id> when the family already has a schedule."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "01:00", "endTime": "04:00", "limit": 80, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules/u1", 200, {})
    api.set_http_response(
        "/service/batteryConfig/api/v1/battery/sites/12345/schedules",
        200,
        {"cfg": {"scheduleSupported": True, "details": [{"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "isEnabled": True}]}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}},
    )
    run_async(api.apply_battery_schedule("12345"))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/schedules/u1")]
    assert len(puts) == 1
    # After the confirming re-read matches, no pending write remains
    assert ("12345", "CFG") not in api.pending_writes


def test_apply_no_change_no_write():
    """apply issues no schedule writes when cloud already matches the local schedule."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    run_async(api.apply_battery_schedule("12345"))
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT")]
    assert writes == []


def test_pending_write_suppresses_duplicate():
    """While a write is pending confirmation, apply does not re-issue the same PUT."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "01:00", "endTime": "04:00", "limit": 80, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}, "freeze": {"enable": False}}
    api.pending_writes[("12345", "CFG")] = {"start": "02:00", "end": "05:00", "limit": 90, "enabled": True, "time": datetime.now(timezone.utc)}
    run_async(api.apply_battery_schedule("12345"))
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT") and "/schedules" in r["path"]]
    assert writes == []


def run_enphase_api_tests(my_predbat):
    """Run all Enphase API tests, returning 0 on success."""
    test_initialize_defaults()
    test_needs_refresh()
    test_is_alive()
    test_login_success()
    test_login_mfa_rejected()
    test_login_transient_rejection_not_fatal()
    test_login_guard_rails()
    test_login_reuse_window()
    test_get_headers_site()
    test_get_headers_battery_config()
    test_request_json_success()
    test_request_json_401_relogin()
    test_request_json_login_wall()
    test_battery_config_variant_fallback()
    test_get_battery_status()
    test_energy_today()
    test_get_schedules_parses_families()
    test_run_first_polls_all_tiers()
    test_derive_power()
    test_publish_data_sensors()
    test_publish_schedule_entities()
    test_event_handlers_update_local_schedule()
    test_write_switch_triggers_apply()
    test_schedules_equal()
    test_apply_charge_schedule_creates()
    test_apply_updates_existing_by_id()
    test_apply_no_change_no_write()
    test_pending_write_suppresses_duplicate()
    print("**** Enphase API tests passed ****")
    return 0

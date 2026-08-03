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
        self.http_sequences = {}  # path -> list of (status, json_data), consumed one per request
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

    def set_http_sequence(self, path, responses):
        """Prime a series of (status, json_data) responses returned one per request to a path.

        The final entry is repeated once the sequence is exhausted, so a test only has to list
        the responses it cares about.
        """
        self.http_sequences[path] = list(responses)

    async def request_raw(self, method, url, headers=None, data=None, json_body=None, params=None):
        """Return canned responses instead of performing HTTP."""
        path = url.split("enphaseenergy.com", 1)[-1].split("?")[0]
        self.request_log.append({"method": method, "path": path, "json": json_body, "data": data, "params": params})
        sequence = self.http_sequences.get(path)
        if sequence:
            status, json_data = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            return status, json_data, "", {}
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


def test_is_too_many_sessions():
    """is_too_many_sessions matches the specific phrase, never a bare 'session' substring."""
    from enphase import is_too_many_sessions

    assert is_too_many_sessions("Too many active sessions") is True
    assert is_too_many_sessions("Error: too many active sessions for this account") is True
    assert is_too_many_sessions("too many logins; active sessions exceeded") is True
    # Happy-path bodies contain 'session_id'/'session' but must NOT match:
    assert is_too_many_sessions('{"success": true, "session_id": "abc"}') is False
    assert is_too_many_sessions("session created") is False
    assert is_too_many_sessions("") is False
    assert is_too_many_sessions(None) is False


def test_login_happy_path_with_session_text():
    """A successful login whose response body text contains 'session_id' must not be mis-read as 'too many sessions'."""
    api = MockEnphaseAPI()
    jwt = "eyJhbGciOiJIUzI1NiJ9." + _b64({"user_id": "9999", "exp": 4102444800}) + ".sig"
    # Real Enlighten returns the JSON body as text too - it contains 'session_id' and session cookies.
    api.set_http_response("/login/login.json", 200, {"success": True, "session_id": "sess1"}, text_data='{"success": true, "session_id": "sess1", "message": "session created"}')
    api.set_http_response("/users/self/token", 200, {"token": jwt, "expires_at": 4102444800})
    api.set_http_response("/app-api/search_sites.json", 200, [{"site_id": 12345, "name": "Home"}])
    assert run_async(api.login()) is True
    assert api.login_reject_count == 0
    assert api.fatal_signalled is False


def test_login_too_many_sessions():
    """A body reporting 'too many active sessions' (even with HTTP 200) rejects the login and cools down."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 200, None, text_data="Error: Too many active sessions for this account")
    assert run_async(api.login()) is False
    assert api.login_reject_count == 1
    assert api.login_cooldown_until is not None


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
        {"id": 1, "serial_num": "B1", "current_charge": 50, "available_energy": 2.5, "max_capacity": 5.0, "status": "normal", "last_report": 1783548194},
        {"id": 2, "serial_num": "B2", "current_charge": 60, "available_energy": 3.0, "max_capacity": 5.0, "status": "normal", "last_report": 1783549411},
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
    assert status["last_report"] == 1783549411  # most recent per-battery report time


def test_inverter_time_and_system_status_sensors():
    """publish_data publishes system_status from siteStatus and inverter_time from the last report."""
    api = MockEnphaseAPI()
    site_id = "12345"
    # An offline system: batteries last reported days ago; siteStatus reports a comm fault.
    api.battery_status[site_id] = {"soc_percent": 0.0, "available_energy": 0.0, "max_capacity": 20.0, "max_power_kw": 6.33, "status": "", "last_report": 1783549411, "batteries": []}
    api.profile[site_id] = {"profile": "self-consumption", "reserve": 30}
    api.today[site_id] = {"totals": {"production": 0}, "arrays": {}, "start_time": None, "interval_length": 900, "site_status": "comm", "status_severity": "warning", "status_desc": "Your gateway has not reported since Jul 8"}
    api.latest_power[site_id] = {"watts": 100.0, "time": 1760000000}
    run_async(api.publish_data(site_id))
    items = api.dashboard_items

    status_item = items["sensor.predbat_enphase_12345_system_status"]
    assert status_item["state"] == "comm"
    assert status_item["attributes"]["severity"] == "warning"
    assert "gateway" in status_item["attributes"]["description"]

    # inverter_time = the battery last_report (1783549411) formatted with the clock format, in local tz.
    from enphase import ENPHASE_CLOCK_FORMAT

    expected = datetime.fromtimestamp(1783549411, api.local_tz).strftime(ENPHASE_CLOCK_FORMAT)
    assert items["sensor.predbat_enphase_12345_inverter_time"]["state"] == expected


def test_log_api_call_suppresses_html():
    """The verbose logger must not dump a full HTML login/marketing page - just a short marker."""
    api = MockEnphaseAPI()
    captured = []
    api.log = lambda message: captured.append(message)
    api.debug_api = True
    html = "<!DOCTYPE html><html>" + ("x" * 50000) + "</html>"
    api._log_api_call("GET", "/service/batteryConfig/api/v1/profile/12345", None, 200, None, html)
    assert len(captured) == 1
    assert "<html>" not in captured[0]
    assert "HTML page" in captured[0]
    assert "xxxxx" not in captured[0]


def test_safe_float_int_handle_na():
    """safe_float/safe_int coerce Enphase 'N/A'/blank/None strings to the default without raising."""
    from enphase import safe_float, safe_int

    assert safe_float("N/A") == 0.0
    assert safe_float("N/A", None) is None
    assert safe_float(None) == 0.0
    assert safe_float("") == 0.0
    assert safe_float("3.5") == 3.5
    assert safe_float(4) == 4.0
    # Percentage strings (e.g. battery current_charge is "0%"/"50%")
    assert safe_float("0%") == 0.0
    assert safe_float("50%") == 50.0
    assert safe_int("0%") == 0
    assert safe_int("55%") == 55
    assert safe_int("N/A") == 0
    assert safe_int("N/A", None) is None
    assert safe_int(None) == 0
    assert safe_int("5.0") == 5
    assert safe_int(7) == 7


def test_get_battery_status_handles_na():
    """A real-world battery_status payload with 'N/A' numeric fields must not crash get_battery_status."""
    api = MockEnphaseAPI()
    payload = {
        "current_charge": "N/A",
        "available_energy": "N/A",
        "max_capacity": "N/A",
        "max_power": "N/A",
        "status": "unknown",
        "storages": [{"id": 1, "serial_num": "B1", "current_charge": "N/A", "available_energy": "N/A", "max_capacity": "N/A", "status": "sleeping"}],
    }
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, payload)
    result = run_async(api.get_battery_status("12345"))
    assert result is not None
    status = api.battery_status["12345"]
    # Total capacity is 0 (all N/A -> 0), so SOC falls back to site current_charge (also N/A -> 0)
    assert status["soc_percent"] == 0.0
    assert status["max_capacity"] == 0.0
    assert status["max_power_kw"] == 0.0


def test_reads_handle_na_values():
    """latest_power, profile and battery_settings tolerate 'N/A' numeric fields from the live API."""
    api = MockEnphaseAPI()
    api.set_http_response("/app-api/12345/get_latest_power", 200, {"latest_power": {"value": "N/A", "time": "N/A"}})
    run_async(api.get_latest_power("12345"))
    assert api.latest_power["12345"]["watts"] == 0.0
    assert api.latest_power["12345"]["time"] is None

    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {"profile": "self-consumption", "batteryBackupPercentage": "N/A"})
    run_async(api.get_profile("12345"))
    assert api.profile["12345"]["reserve"] == 0

    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {"chargeFromGrid": True, "veryLowSoc": "N/A", "veryLowSocMin": "N/A", "veryLowSocMax": "N/A"})
    run_async(api.get_battery_settings("12345"))
    assert api.battery_settings["12345"]["veryLowSocMin"] is None
    # Publishing must not crash when veryLowSocMin is None (reserve_min falls back to 5)
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.today["12345"] = {"totals": {"production": 1000}, "arrays": {}, "start_time": None, "interval_length": 900}
    api.latest_power["12345"] = {"watts": 100.0, "time": 1760000000}
    run_async(api.publish_data("12345"))
    assert api.dashboard_items["sensor.predbat_enphase_12345_battery_reserve_min"]["state"] == 5


def test_reads_nested_data_shape():
    """profile and batterySettings responses wrap their fields in a 'data' object - parse it."""
    api = MockEnphaseAPI()
    # Real profile response shape from a battery account
    api.set_http_response(
        "/service/batteryConfig/api/v1/profile/12345",
        200,
        {"type": "profile-details", "data": {"profile": "self-consumption", "batteryBackupPercentage": 30, "batteryBackupPercentageMin": 5, "batteryBackupPercentageMax": 100}},
    )
    run_async(api.get_profile("12345"))
    assert api.profile["12345"]["profile"] == "self-consumption"
    assert api.profile["12345"]["reserve"] == 30

    api.set_http_response(
        "/service/batteryConfig/api/v1/batterySettings/12345",
        200,
        {"type": "battery-details", "data": {"chargeFromGrid": True, "veryLowSoc": 5, "veryLowSocMin": 5, "veryLowSocMax": 25}},
    )
    run_async(api.get_battery_settings("12345"))
    settings = api.battery_settings["12345"]
    assert settings["chargeFromGrid"] is True
    assert settings["veryLowSocMin"] == 5
    assert settings["veryLowSocMax"] == 25


def test_get_site_settings():
    """siteSettings parses the nested 'data' capability flags."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.set_http_response(
        "/service/batteryConfig/api/v1/siteSettings/12345",
        200,
        {"type": "site-settings", "data": {"hasEncharge": True, "hasAcb": False, "showChargeFromGrid": True, "isEnsemble": True, "countryCode": "GB"}},
    )
    run_async(api.get_site_settings("12345"))
    flags = api.site_settings["12345"]
    assert flags["hasEncharge"] is True
    assert flags["showChargeFromGrid"] is True
    assert flags["countryCode"] == "GB"


def test_set_reserve_writes_profile_put():
    """set_reserve PUTs the profile with the new batteryBackupPercentage, preserving the profile name."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.profile["12345"] = {"profile": "cost_savings", "reserve": 30}
    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {})
    run_async(api.set_reserve("12345", 25))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/profile/12345")]
    assert len(puts) == 1
    body = puts[0]["json"]
    assert body["batteryBackupPercentage"] == 25
    assert body["profile"] == "cost_savings"  # existing profile preserved


def test_request_json_absorbs_xsrf_token():
    """A successful BatteryConfig response refreshes the XSRF token AND puts it in the cookie header.

    BatteryConfig writes need it as a double-submit: the X-XSRF-Token header (from self.xsrf_token)
    and the XSRF-TOKEN cookie (in the Cookie header) must both be present and match.
    """
    api = MockEnphaseAPI()
    api.eauth_token = "tok"
    api.battery_config_variant = "cookie_eauth"

    async def raw_with_xsrf(method, url, headers=None, data=None, json_body=None, params=None):
        """Return a 200 with a fresh XSRF token in the cookie dict (as request_raw folds the header)."""
        return 200, {"ok": True}, "", {"XSRF-TOKEN": "fresh-token-123"}

    api.request_raw = raw_with_xsrf
    run_async(api.request_json("GET", "/service/batteryConfig/api/v1/siteSettings/12345", family="battery_config"))
    assert api.xsrf_token == "fresh-token-123"
    assert "XSRF-TOKEN=fresh-token-123" in api.cookie_header  # cookie side of the double-submit
    write_headers = api.get_headers("battery_config", write=True)
    assert write_headers["X-XSRF-Token"] == "fresh-token-123"  # header side
    assert "XSRF-TOKEN=fresh-token-123" in write_headers["Cookie"]


def test_absorb_cookies_bp_xsrf_name():
    """The XSRF token cookie is captured whatever its exact name (e.g. BP-XSRF-Token)."""
    api = MockEnphaseAPI()
    api._absorb_cookies({"BP-XSRF-Token": "bp-token-9"})
    assert api.xsrf_token == "bp-token-9"


def test_login_wall_does_not_corrupt_session():
    """An HTML login-wall response must NOT merge its anonymous cookies into our live session."""
    api = MockEnphaseAPI()
    api.eauth_token = "tok"
    api.cookie_header = "_enlighten_4_session=good; e-auth=x"
    api.battery_config_variant = "cookie_eauth"  # already on fallback, so no variant switch
    api.login_cooldown_until = datetime.now(timezone.utc) + timedelta(hours=1)  # block re-login

    async def raw_login_wall(method, url, headers=None, data=None, json_body=None, params=None):
        """Return an HTML login wall carrying a fresh anonymous session cookie."""
        return 200, None, "<!DOCTYPE html><html>please sign in</html>", {"_enlighten_4_session": "ANONYMOUS", "XSRF-TOKEN": "wall"}

    api.request_raw = raw_login_wall
    result = run_async(api.request_json("GET", "/service/batteryConfig/api/v1/profile/12345", family="battery_config"))
    assert result is None  # login wall is treated as failure
    assert api.cookie_header == "_enlighten_4_session=good; e-auth=x"  # session cookie NOT clobbered
    assert api.xsrf_token != "wall"  # no XSRF captured from a failed/login-wall response


def test_get_battery_status_percent_soc():
    """Site current_charge like '0%' must parse; capacity-weighted SOC uses storages."""
    api = MockEnphaseAPI()
    payload = {
        "current_charge": "50%",
        "available_energy": 10.0,
        "max_capacity": 20.0,
        "max_power": 6.33,
        "storages": [],  # no per-battery breakdown -> fall back to current_charge
    }
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, payload)
    run_async(api.get_battery_status("12345"))
    assert api.battery_status["12345"]["soc_percent"] == 50.0  # "50%" fallback parsed
    assert api.battery_status["12345"]["max_power_kw"] == 6.33


def test_today_channel_kwh():
    """today_channel_kwh reads today's per-channel total (Wh) and converts to kWh, 0.0 if absent."""
    from enphase import today_channel_kwh

    today = {"totals": {"production": 45287, "consumption": 12000}}
    assert today_channel_kwh(today, "production") == 45.287  # 45287 Wh -> 45.287 kWh
    assert today_channel_kwh(today, "consumption") == 12.0
    assert today_channel_kwh(today, "import") == 0.0  # channel absent -> 0
    assert today_channel_kwh({"totals": {"production": "N/A"}}, "production") == 0.0
    assert today_channel_kwh({}, "production") == 0.0
    # Battery charge/discharge/export have no single total - summed from source_dest flow components.
    flows = {"totals": {"solar_battery": 3000, "grid_battery": 1000, "battery_home": 2500, "battery_grid": 500, "solar_grid": 4000}}
    assert today_channel_kwh(flows, "charge") == 4.0  # solar_battery + grid_battery = 4000 Wh
    assert today_channel_kwh(flows, "discharge") == 3.0  # battery_home + battery_grid = 3000 Wh
    assert today_channel_kwh(flows, "export") == 4.5  # solar_grid + battery_grid = 4500 Wh
    # A direct total key wins over the flow sum when present.
    assert today_channel_kwh({"totals": {"charge": 9000, "solar_battery": 1000}}, "charge") == 9.0


# A real DataMsg captured from the Enlighten livestream topic (site exporting on a sunny afternoon):
# pv 4632.8 W, storage 32.0 W, grid -2686.1 W, load 1978.7 W, soc 100%.
LIVESTREAM_FRAME = base64.b64decode(
    "CAEQwIQ9GogBChYI7+GaAhDv4ZoCGgTv4ZoCIgTv4ZoCEhIIgPoBEPP6ARoDgPoBIgPz+gEaLgjZhtz+//////8BELLiwf7//////wEaCtmG3P7//////wEiCrLiwf7//////wEiEgjI4ngQlL9eGgPI4ngiA5S/XigCMGQ6BhoBACIBAEAGSAFaBghkEKCcASABKAUyAhACMgQIARACMgQIAhABMgQIAxAB"
)


def test_decode_livestream_message():
    """A livestream DataMsg decodes to watts per channel plus SOC.

    agg_p_mw is milliwatts. Signs follow Predbat's convention: grid negative when exporting,
    battery positive when discharging.
    """
    from enphase import decode_livestream_message

    reading = decode_livestream_message(LIVESTREAM_FRAME)
    assert reading == {"pv": 4632.8, "battery": 32.0, "grid": -2686.1, "load": 1978.7, "soc": 100}


def test_decode_livestream_message_balances():
    """The decoded channels satisfy the energy balance load = pv + grid + battery."""
    from enphase import decode_livestream_message

    r = decode_livestream_message(LIVESTREAM_FRAME)
    assert abs((r["pv"] + r["grid"] + r["battery"]) - r["load"]) < 0.05


def test_decode_livestream_message_rejects_rubbish():
    """A payload that is not a DataMsg returns None rather than raising."""
    from enphase import decode_livestream_message

    assert decode_livestream_message(b"\xff\xff\xff\xff not protobuf") is None


def test_livestream_username_carries_the_authorizer_credentials():
    """The MQTT username is the query-string blob AWS IoT's custom authorizer expects.

    The WebSocket URL carries no query parameters - the browser cannot set custom headers on a
    WebSocket - so the authorizer name, token and signature all travel in the CONNECT username.
    """
    from enphase import livestream_username

    boot = {
        "aws_authorizer": "aws-lambda-authoriser-prod",
        "aws_token_key": "enph_token",
        "aws_token_value": "tok123",
        "aws_digest": "sig+with/reserved=chars",
    }
    user = livestream_username(boot, "5667604")
    assert user.startswith("?")
    assert "x-amz-customauthorizer-name=aws-lambda-authoriser-prod" in user
    assert "enph_token=tok123" in user
    assert "site-id=5667604" in user
    # The digest is base64 and must be percent-encoded, not passed raw
    from urllib.parse import quote_plus

    assert f"x-amz-customauthorizer-signature={quote_plus(boot['aws_digest'])}" in user
    assert boot["aws_digest"] not in user


def test_gateway_serial_read_from_today():
    """The livestream bootstrap needs the gateway serial, which /today already carries."""
    from enphase import gateway_serial

    assert gateway_serial({"serial": "122530006866"}) == "122530006866"
    assert gateway_serial({}) is None


BUCKET_START = 1783724400  # local midnight of the day the /today buckets belong to
FROZEN_NOW_TS = BUCKET_START + int(82.5 * 900)  # inside index 82, so the settled bucket read is 80


def _publish_with_buckets(api, production=250, imp=100, exp=0, charge=200, discharge=0):
    """Publish a site from /today buckets; the defaults give pv 1000 W, importing 400 W, charging 800 W."""
    start = BUCKET_START

    def bucket(value):
        """Return a 96-slot Wh array with the bucket read by publish_data set to value."""
        out = [0] * 96
        out[80] = value
        return out

    api.today["12345"] = {
        "totals": {},
        "arrays": {"production": bucket(production), "import": bucket(imp), "export": bucket(exp), "charge": bucket(charge), "discharge": bucket(discharge)},
        "start_time": start,
        "interval_length": 900,
    }
    import enphase as enphase_module

    original = enphase_module.datetime

    class _Fixed(original):
        @classmethod
        def now(cls, tz=None):
            """Return a time inside index 82, so the settled bucket publish_data reads is 80."""
            return original.fromtimestamp(FROZEN_NOW_TS, tz)

    enphase_module.datetime = _Fixed
    try:
        run_async(api.publish_data("12345"))
    finally:
        enphase_module.datetime = original
    base = "sensor.predbat_enphase_12345"
    return {name: api.dashboard_items[f"{base}_{name}_power"]["state"] for name in ("pv", "grid", "battery", "load")}


def test_publish_prefers_the_measured_livestream_reading():
    """When a livestream reading is available the power sensors use it, not the energy buckets.

    The livestream channels are metered, instantaneous and include a real house load, so they beat
    the 15-minute buckets on every count.
    """
    api = MockEnphaseAPI()
    api.live_power["12345"] = {"pv": 4632.8, "battery": 32.0, "grid": -2686.1, "load": 1978.7, "soc": 100, "read_ts": FROZEN_NOW_TS - 60}
    published = _publish_with_buckets(api)
    assert published == {"pv": 4632.8, "grid": 2686.1, "battery": 32.0, "load": 1978.7}  # grid flipped to +export


def test_live_power_is_never_persisted():
    """Livestream readings stay in memory only.

    They are instantaneous and carry no usable timestamp of their own, so restoring one from the
    cache after a restart would republish an old measurement as if it were current - and the
    refresh gate can be satisfied by the restored age, so nothing would immediately correct it.
    """
    from enphase import ENPHASE_CACHE_KEYS

    assert "live_power" not in ENPHASE_CACHE_KEYS


def test_failed_live_read_keeps_the_recent_reading():
    """A failed read leaves the last reading in place so a blip does not flip the sensors.

    The bucket fallback lags 15-30 minutes, so bouncing onto it for one missed cycle would be a
    bigger step than simply holding the measurement a little longer.
    """
    api = MockEnphaseAPI()
    api.today["12345"] = {"serial": "122530006866"}
    reading = {"pv": 4632.8, "battery": 32.0, "grid": -2686.1, "load": 1978.7, "soc": 100, "read_ts": FROZEN_NOW_TS - 60}
    api.live_power["12345"] = dict(reading)
    # No canned response for the bootstrap, so it 404s and the read fails
    assert run_async(api.get_live_power("12345")) is None
    assert api.live_power["12345"] == reading


def test_live_reading_older_than_the_window_falls_back_to_buckets():
    """Once a reading passes ENPHASE_LIVE_MAX_AGE it is ignored in favour of the bucket values.

    Livestream readings are instantaneous and carry no usable timestamp of their own, so an old one
    must not keep being published as though it were current.
    """
    from enphase import ENPHASE_LIVE_MAX_AGE_MINUTES

    api = MockEnphaseAPI()
    api.live_power["12345"] = {"pv": 4632.8, "battery": 32.0, "grid": -2686.1, "load": 1978.7, "soc": 100, "read_ts": FROZEN_NOW_TS - (ENPHASE_LIVE_MAX_AGE_MINUTES * 60 + 1)}
    published = _publish_with_buckets(api)
    assert published["pv"] == 1000.0  # bucket value, not the stale 4632.8
    assert published["grid"] == -400.0


def test_grid_power_is_positive_when_exporting():
    """Grid power follows Predbat's convention: positive exporting, negative importing.

    web.py's power flow reads `grid_power >= 10` as exporting, and sigenergy documents the same.
    The Enphase channels are the other way round (a livestream grid reading is negative while
    exporting, and the /today buckets give import - export), so both paths have to be flipped.
    """
    api = MockEnphaseAPI()
    api.live_power["12345"] = {"pv": 4632.8, "battery": 32.0, "grid": -2686.1, "load": 1978.7, "soc": 100, "read_ts": FROZEN_NOW_TS - 60}
    published = _publish_with_buckets(api)
    assert published["grid"] == 2686.1  # exporting 2.7 kW -> positive


def test_grid_power_from_buckets_is_positive_when_exporting():
    """The bucket fallback follows the same convention as the livestream."""
    api = MockEnphaseAPI()
    published = _publish_with_buckets(api, production=250, imp=0, exp=100, charge=0, discharge=0)
    assert published["grid"] == 400.0  # 100 Wh exported over a 15-min bucket


def test_published_power_satisfies_the_predbat_energy_balance():
    """With Predbat's signs the balance is load = pv + battery - grid, not pv + battery + grid."""
    api = MockEnphaseAPI()
    published = _publish_with_buckets(api)
    assert published["load"] == published["pv"] + published["battery"] - published["grid"]


def test_publish_falls_back_to_buckets_without_a_livestream_reading():
    """With no livestream reading all four sensors fall back to the settled bucket values.

    Load falls back to the energy-balance residual, so the four still agree and a power-flow
    display still balances - a livestream failure degrades the sensors rather than blanking them.
    """
    api = MockEnphaseAPI()
    published = _publish_with_buckets(api)
    assert published["pv"] == 1000.0
    assert published["grid"] == -400.0  # importing
    assert published["battery"] == -800.0  # charging
    assert published["load"] == 600.0  # 1000 - 800 + 400
    assert published["load"] == published["pv"] + published["battery"] - published["grid"]


def test_interval_power():
    """interval_power converts a settled 15-minute Wh bucket into watts.

    The bucket that has only just closed is still being back-filled by the cloud - first read it
    returns roughly a third of its eventual value, then it corrects upward - so reading it makes
    the power sensors saw-tooth on every bucket rollover. Step back to a bucket that has stopped
    changing instead.
    """
    from enphase import interval_power

    # 96 fifteen-minute buckets from midnight; interval_length 900s.
    start = 1783724400
    interval = 900
    values = [0] * 96
    values[80] = 199  # settled bucket: 199 Wh / 0.25h = 796 W
    values[81] = 61  # just-closed bucket, still filling - must not be used
    now_ts = start + int(82.5 * interval)  # current index 82, just-closed 81, settled 80
    assert interval_power(values, start, interval, now_ts) == 796.0
    # Missing/empty data -> 0
    assert interval_power([], start, interval, now_ts) == 0.0
    assert interval_power(values, None, interval, now_ts) == 0.0
    assert interval_power(values, start, 0, now_ts) == 0.0


def test_interval_power_clamps_at_start_of_day():
    """Before enough buckets exist to settle, interval_power clamps to the first bucket."""
    from enphase import interval_power

    start = 1783724400
    values = [7] + [0] * 95
    assert interval_power(values, start, 900, start + 450) == 28.0  # 7 Wh / 0.25h, index clamped to 0


def _api_with_today_buckets(production, imp, exp, charge, discharge):
    """Build a mock API whose settled bucket holds the given per-channel Wh values."""
    api = MockEnphaseAPI()
    now = datetime.now(timezone.utc).timestamp()
    interval = 900
    settled = 80  # current index 82, just-closed 81, settled 80
    start = now - 82.5 * interval

    def arr(value, filler):
        """Return a 96-bucket array with the settled bucket set and a decoy in the just-closed one."""
        out = [0] * 96
        out[settled] = value
        out[settled + 1] = filler  # partially filled bucket that must be ignored
        return out

    api.today["12345"] = {
        "totals": {},
        "arrays": {
            "production": arr(production, 9999),
            "import": arr(imp, 9999),
            "export": arr(exp, 0),
            "charge": arr(charge, 0),
            "discharge": arr(discharge, 9999),
        },
        "start_time": start,
        "interval_length": interval,
    }
    return api


def _published_power(api):
    """Return the four published power sensor states as floats."""
    base = "sensor.predbat_enphase_12345"
    return tuple(float(api.dashboard_items[f"{base}_{name}_power"]["state"]) for name in ("pv", "grid", "battery", "load"))


def test_load_power_is_derived_from_the_other_channels():
    """Load is the energy-balance residual: pv + battery - grid, in Predbat's signs.

    The cloud's own consumption channel is exactly this sum, and `get_latest_power` reports
    production rather than consumption, so publishing that as load made the load sensor track PV.
    """
    # 250 Wh pv, 100 Wh import, 0 export, 0 charge, 25 Wh discharge over a 15-min bucket (x4 -> W)
    api = _api_with_today_buckets(production=250, imp=100, exp=0, charge=0, discharge=25)
    run_async(api.publish_data("12345"))
    pv, grid, battery, load = _published_power(api)
    assert (pv, grid, battery) == (1000.0, -400.0, 100.0)  # grid negative: importing
    assert load == 1500.0
    assert load == pv + battery - grid  # the power-flow card must balance


def test_load_power_never_goes_negative():
    """A negative residual is clamped to zero rather than published as negative house load.

    Measurement skew between the micros, CT clamps and battery telemetry makes the residual
    unphysical while the battery is cycling hard; a negative house load breaks the power flow card.
    """
    # Heavy grid charging: import 1461 Wh, charge 1796 Wh -> residual is negative
    api = _api_with_today_buckets(production=0, imp=1461, exp=714, charge=1796, discharge=203)
    run_async(api.publish_data("12345"))
    pv, grid, battery, load = _published_power(api)
    assert pv + battery - grid < 0  # the raw residual really is negative
    assert load == 0.0


def test_power_sensors_ignore_the_unsettled_bucket():
    """Every power sensor reads the settled bucket, not the one that just closed."""
    api = _api_with_today_buckets(production=250, imp=100, exp=0, charge=0, discharge=25)
    run_async(api.publish_data("12345"))
    pv, _, _, _ = _published_power(api)
    assert pv == 1000.0  # 9999 Wh decoy sits in the just-closed bucket


def test_get_schedules_parses_families():
    """Schedules read stores cfg/dtg/rbd entries (real detail shape with scheduleId) and dtg support."""
    api = MockEnphaseAPI()
    # Real detail shape from a battery account: scheduleId (not id), scheduleStatus per family.
    payload = {
        "type": "BATTERY_SCHEDULES_CONFIG",
        "cfg": {"scheduleStatus": "active", "count": 1, "details": [{"scheduleId": "2e2e08a8-b3b7", "startTime": "01:10", "endTime": "05:29", "limit": 100, "scheduleType": "CFG", "days": [1, 2, 3, 4, 5, 6, 7], "isDeleted": False, "isEnabled": True}]},
        "dtg": {"scheduleStatus": "not_supported", "count": 0, "details": []},
        "rbd": {"scheduleStatus": "active", "count": 0, "details": []},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, payload)
    run_async(api.get_schedules("12345"))
    cfg = api.schedules["12345"]["cfg"]
    assert cfg["id"] == "2e2e08a8-b3b7"  # sourced from scheduleId
    assert cfg["startTime"] == "01:10" and cfg["endTime"] == "05:29"
    assert cfg["limit"] == 100 and cfg["enabled"] is True and cfg["supported"] is True
    assert cfg["count"] == 1
    assert api.dtg_supported("12345") is False  # 'not_supported' status


def test_automatic_config():
    """automatic_config points every inverter arg at the published entities."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    run_async(api.automatic_config())
    args = api.args_set
    assert args["inverter_type"] == ["EnphaseCloud"]
    assert args["num_inverters"] == 1
    assert args["soc_percent"] == ["sensor.predbat_enphase_12345_soc_percent"]
    assert args["soc_max"] == ["sensor.predbat_enphase_12345_battery_capacity"]
    assert args["battery_rate_max"] == ["sensor.predbat_enphase_12345_battery_rate_max"]
    assert args["load_today"] == ["sensor.predbat_enphase_12345_load_today"]
    assert args["import_today"] == ["sensor.predbat_enphase_12345_import_today"]
    assert args["export_today"] == ["sensor.predbat_enphase_12345_export_today"]
    assert args["pv_today"] == ["sensor.predbat_enphase_12345_pv_today"]
    assert args["charge_start_time"] == ["select.predbat_enphase_12345_battery_schedule_charge_start_time"]
    assert args["charge_limit"] == ["number.predbat_enphase_12345_battery_schedule_charge_soc"]
    assert args["scheduled_charge_enable"] == ["switch.predbat_enphase_12345_battery_schedule_charge_enable"]
    assert args["scheduled_discharge_enable"] == ["switch.predbat_enphase_12345_battery_schedule_export_enable"]
    assert args["discharge_start_time"] == ["select.predbat_enphase_12345_battery_schedule_export_start_time"]
    assert args["discharge_target_soc"] == ["number.predbat_enphase_12345_battery_schedule_export_soc"]
    assert args["reserve"] == ["number.predbat_enphase_12345_battery_schedule_reserve"]
    assert args["battery_min_soc"] == ["sensor.predbat_enphase_12345_battery_reserve_min"]
    assert args["inverter_time"] == ["sensor.predbat_enphase_12345_inverter_time"]
    assert args["schedule_write_button"] == ["switch.predbat_enphase_12345_battery_schedule_charge_write"]
    # export_limit is left unset so the user's apps.yaml value (if any) is respected.
    assert "export_limit" not in args


def test_automatic_config_no_dtg_raises():
    """A site without DTG (export) support must fail to configure - Predbat needs export control."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": False}, "rbd": {"supported": True}}
    raised = False
    try:
        run_async(api.automatic_config())
    except ValueError as error:
        raised = True
        assert "DTG" in str(error) or "export" in str(error).lower()
    assert raised
    assert "inverter_type" not in api.args_set


def test_automatic_config_no_charge_support_raises():
    """A battery site that does not support CFG (charge-from-grid) scheduling must fail to configure."""
    api = MockEnphaseAPI()
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    # Has a battery, but charge scheduling is not supported
    api.schedules["12345"] = {"cfg": {"supported": False}, "dtg": {"supported": False}, "rbd": {"supported": True}}
    raised = False
    try:
        run_async(api.automatic_config())
    except ValueError as error:
        raised = True
        assert "CFG" in str(error) or "charge" in str(error).lower()
    assert raised
    assert "inverter_type" not in api.args_set


def test_get_schedules_supported_from_status():
    """scheduleStatus 'active' marks a family supported; the real battery-less response is handled."""
    api = MockEnphaseAPI()
    # Real response shape for a site (no scheduleSupported / details fields, just status + count)
    payload = {"type": "BATTERY_SCHEDULES_CONFIG", "cfg": {"scheduleStatus": "active", "count": 0}, "dtg": {"scheduleStatus": "not_supported", "count": 0}, "rbd": {"scheduleStatus": "active", "count": 0}}
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, payload)
    run_async(api.get_schedules("12345"))
    assert api.schedules["12345"]["cfg"]["supported"] is True
    assert api.schedules["12345"]["cfg"]["count"] == 0
    assert api.schedules["12345"]["cfg"]["status"] == "active"
    assert api.dtg_supported("12345") is False  # 'not_supported' status


def test_get_schedules_pending_family_is_still_supported():
    """A family whose scheduleStatus is 'pending' is supported - a write is in flight, that is all.

    The cloud reports a family as 'pending' while a schedule change settles on the gateway, which
    happens right after any write Predbat makes. Treating that as unsupported made Predbat decide
    the site could not do charge-from-grid at all and abandon automatic configuration, even with an
    active schedule sitting in the family.
    """
    api = MockEnphaseAPI()
    detail = {"scheduleId": "c1", "startTime": "04:30", "endTime": "04:40", "limit": 5, "scheduleType": "CFG", "isDeleted": False, "isEnabled": True, "scheduleStatus": "active"}
    payload = {
        "type": "BATTERY_SCHEDULES_CONFIG",
        "cfg": {"scheduleStatus": "pending", "count": 1, "details": [detail]},
        "dtg": {"scheduleStatus": "pending", "count": 1, "details": [dict(detail, scheduleId="d1", scheduleType="DTG")]},
        "rbd": {"scheduleStatus": "active", "count": 0},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, payload)
    run_async(api.get_schedules("12345"))
    assert api.schedules["12345"]["cfg"]["supported"] is True
    assert api.dtg_supported("12345") is True


def test_inverter_def_enphase():
    """EnphaseCloud INVERTER_DEF exists with the agreed capability flags."""
    from config import INVERTER_DEF

    idef = INVERTER_DEF["EnphaseCloud"]
    assert idef["has_rest_api"] is False
    assert idef["has_target_soc"] is True
    assert idef["time_button_press"] is True
    assert idef["charge_time_entity_is_option"] is True
    assert idef["can_span_midnight"] is False
    assert idef["target_soc_used_for_discharge"] is True
    assert idef["has_fox_inverter_mode"] is False


def test_run_first_polls_all_tiers():
    """First run() logs in, fetches every tier and publishes."""
    api = MockEnphaseAPI()
    # prime auth short-circuit
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.set_http_response("/pv/settings/12345/battery_status.json", 200, BATTERY_STATUS_PAYLOAD)
    api.set_http_response("/pv/systems/12345/today", 200, {"stats": [{"totals": {"production": 1000}, "production": [1000], "start_time": 1783724400, "interval_length": 900}]})
    api.set_http_response("/app-api/12345/get_latest_power", 200, {"latest_power": {"value": 450, "units": "w", "time": 1760000000}})
    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {"profile": "self-consumption", "batteryBackupPercentage": 20})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {"chargeFromGrid": True, "veryLowSoc": 10, "veryLowSocMin": 5, "veryLowSocMax": 25})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    result = run_async(api.run(0, True))
    assert result
    assert api.battery_status["12345"]["soc_percent"] == 55.0
    assert api.profile["12345"]["reserve"] == 20
    assert api.latest_power["12345"]["watts"] == 450


def test_log_api_call_redacts_token():
    """API-call logging redacts JWT token fields and truncates long bodies, and is gated by debug_api."""
    api = MockEnphaseAPI()
    captured = []
    api.log = lambda message: captured.append(message)

    api.debug_api = True
    api._log_api_call("GET", "/users/self/token", None, 200, {"token": "secret-jwt-value", "expires_at": 123}, "")
    assert len(captured) == 1
    assert "secret-jwt-value" not in captured[0]
    assert "***redacted***" in captured[0]
    assert "expires_at" in captured[0]

    # When disabled, nothing is logged
    captured.clear()
    api.debug_api = False
    api._log_api_call("GET", "/pv/settings/1/battery_status.json", None, 200, {"x": 1}, "")
    assert captured == []


def test_log_api_call_redacts_livestream_credentials():
    """The livestream bootstrap's token and signature must never reach the log.

    Predbat logs are routinely shared for debugging, and this response carries live credentials
    for the account's AWS IoT stream.
    """
    api = MockEnphaseAPI()
    captured = []
    api.log = lambda message: captured.append(message)
    api.debug_api = True
    api._log_api_call(
        "GET",
        "/pv/aws_sigv4/livestream.json",
        {"serial_num": "122530006866"},
        200,
        {"aws_token_value": "token-must-not-be-logged", "aws_digest": "signature-must-not-be-logged", "aws_iot_endpoint": "iot.example.com", "live_stream_topic": "v1/live-stream/abc123"},
        "",
    )
    assert "token-must-not-be-logged" not in captured[0]
    assert "signature-must-not-be-logged" not in captured[0]
    # Non-secret fields are still logged, so the call remains diagnosable
    assert "iot.example.com" in captured[0]
    assert "v1/live-stream/abc123" in captured[0]


def test_login_dedupes_sites():
    """Duplicate sites in the search response collapse to a single entry (no double-publish)."""
    api = MockEnphaseAPI()
    jwt = "eyJhbGciOiJIUzI1NiJ9." + _b64({"user_id": "9999", "exp": 4102444800}) + ".sig"
    api.set_http_response("/login/login.json", 200, {"success": True, "session_id": "sess1"})
    api.set_http_response("/users/self/token", 200, {"token": jwt})
    # Enlighten returns the same site twice
    api.set_http_response("/app-api/search_sites.json", 200, [{"site_id": 2627346, "name": "Home"}, {"site_id": 2627346, "name": "Home"}])
    assert run_async(api.login()) is True
    assert len(api.sites) == 1
    assert api.sites[0]["site_id"] == "2627346"


def test_run_single_site_publishes_once():
    """run() operates on one active site, so duplicate site entries publish sensors only once."""
    api = MockEnphaseAPI()
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    # Two identical site entries (as an un-deduped cache might hold)
    api.sites = [{"site_id": "2627346", "name": "Home"}, {"site_id": "2627346", "name": "Home"}]
    api.set_http_response("/pv/settings/2627346/battery_status.json", 200, {"current_charge": "N/A", "storages": []})
    api.set_http_response("/pv/systems/2627346/today", 200, {"stats": [{"totals": {"production": 1000}, "production": [1000], "start_time": 1783724400, "interval_length": 900}]})
    api.set_http_response("/app-api/2627346/get_latest_power", 200, {"latest_power": {"value": 454, "time": 1760000000}})
    api.set_http_response("/service/batteryConfig/api/v1/profile/2627346", 200, {"profile": "self-consumption", "batteryBackupPercentage": 0})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/2627346", 200, {"chargeFromGrid": False})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/2627346/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": False, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    # Count how many times the soc_percent sensor is published in one run cycle
    published = []
    real_dashboard_item = api.dashboard_item

    def counting_dashboard_item(entity_id, state, attributes, app=None):
        """Record publications of the soc_percent sensor."""
        if entity_id.endswith("_soc_percent"):
            published.append(entity_id)
        real_dashboard_item(entity_id, state, attributes, app)

    api.dashboard_item = counting_dashboard_item
    assert run_async(api.run(0, True)) is True
    assert published == ["sensor.predbat_enphase_2627346_soc_percent"]  # published exactly once, not twice


def test_run_no_battery_returns_false_without_raising():
    """A PV-only site (no controllable battery) must not crash automatic_config - run() returns False."""
    api = MockEnphaseAPI()
    api.automatic = True
    api.login_last_success = datetime.now(timezone.utc)
    api.eauth_token = "tok"
    api.sites = [{"site_id": "2627346", "name": "Home"}]
    # PV-only: battery_status has no capacity
    api.set_http_response("/pv/settings/2627346/battery_status.json", 200, {"current_charge": "N/A", "storages": []})
    api.set_http_response("/pv/systems/2627346/today", 200, {"stats": [{"totals": {"production": 1000}, "production": [1000], "start_time": 1783724400, "interval_length": 900}]})
    api.set_http_response("/app-api/2627346/get_latest_power", 200, {"latest_power": {"value": 454, "time": 1760000000}})
    api.set_http_response("/service/batteryConfig/api/v1/profile/2627346", 200, {"profile": "self-consumption", "batteryBackupPercentage": 0})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/2627346", 200, {"chargeFromGrid": False})
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/2627346/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": False, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    # Must return False (not raise), and must not have set inverter_type
    assert run_async(api.run(0, True)) is False
    assert "inverter_type" not in api.args_set
    # Sensors were still published (monitoring works)
    assert "sensor.predbat_enphase_2627346_pv_today" in api.dashboard_items


def test_get_today():
    """get_today normalises the /today payload into totals (Wh) and intra-day bucket metadata."""
    api = MockEnphaseAPI()
    payload = {"stats": [{"totals": {"production": 45287}, "production": [0, 100, 200], "start_time": 1783724400, "interval_length": 900}]}
    api.set_http_response("/pv/systems/12345/today", 200, payload)
    run_async(api.get_today("12345"))
    today = api.today["12345"]
    assert today["totals"] == {"production": 45287}
    assert today["arrays"]["production"] == [0, 100, 200]
    assert today["arrays"]["consumption"] == []  # missing channel -> empty
    assert today["interval_length"] == 900
    assert today["start_time"] == 1783724400


def test_publish_data_sensors():
    """publish_data creates the full monitoring sensor set from the /today totals and buckets."""
    api = MockEnphaseAPI()
    api.battery_status["12345"] = {"soc_percent": 55.0, "available_energy": 5.5, "max_capacity": 10.0, "max_power_kw": 3.84, "status": "normal", "batteries": []}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSoc": 10, "veryLowSocMin": 5, "veryLowSocMax": 25}
    # today.totals are per-channel Wh totals for today; publish converts to kWh.
    start = 1783724400

    def _bucket(value):
        """Return a 96-bucket Wh array with the settled bucket (80) set to value."""
        out = [0] * 96
        out[80] = value
        return out

    api.today["12345"] = {
        "totals": {"production": 3500, "consumption": 2200, "import": 1000, "export": 400, "charge": 800, "discharge": 600},
        # 15-min buckets: pv 4000 W, import 400 W, charge 800 W -> load = 4000 + 400 - 800 = 3600 W
        "arrays": {"production": _bucket(1000), "import": _bucket(100), "export": _bucket(0), "charge": _bucket(200), "discharge": _bucket(0)},
        "start_time": start,
        "interval_length": 900,
    }
    # No livestream reading here, so the power sensors fall back to the /today buckets.
    # Freeze "now" so interval_power selects settled bucket 80 (current 82, just-closed 81).
    import enphase as enphase_module

    original_datetime = enphase_module.datetime

    class _FixedDatetime(original_datetime):
        @classmethod
        def now(cls, tz=None):
            """Return a fixed time inside interval index 82."""
            return original_datetime.fromtimestamp(start + int(82.5 * 900), tz)

    enphase_module.datetime = _FixedDatetime
    try:
        run_async(api.publish_data("12345"))
    finally:
        enphase_module.datetime = original_datetime
    items = api.dashboard_items
    assert items["sensor.predbat_enphase_12345_soc_percent"]["state"] == 55.0
    assert items["sensor.predbat_enphase_12345_battery_capacity"]["state"] == 10.0
    assert items["sensor.predbat_enphase_12345_battery_rate_max"]["state"] == 3840.0
    assert items["sensor.predbat_enphase_12345_pv_today"]["state"] == 3.5  # 3500 Wh -> 3.5 kWh
    assert items["sensor.predbat_enphase_12345_load_today"]["state"] == 2.2
    assert items["sensor.predbat_enphase_12345_import_today"]["state"] == 1.0
    assert items["sensor.predbat_enphase_12345_export_today"]["state"] == 0.4
    assert items["sensor.predbat_enphase_12345_battery_reserve_min"]["state"] == 5
    assert items["sensor.predbat_enphase_12345_pv_power"]["state"] == 4000.0  # 1000 Wh / 0.25h
    assert items["sensor.predbat_enphase_12345_grid_power"]["state"] == -400.0  # importing
    assert items["sensor.predbat_enphase_12345_battery_power"]["state"] == -800.0  # charging
    assert items["sensor.predbat_enphase_12345_load_power"]["state"] == 3600.0  # derived residual


def test_sync_local_schedule_from_cloud():
    """Control entities are seeded (once) from the real cloud reserve and schedule windows."""
    api = MockEnphaseAPI()
    site_id = "12345"
    api.profile[site_id] = {"profile": "self-consumption", "reserve": 30}
    api.schedules[site_id] = {
        "cfg": {"id": "c1", "startTime": "01:10", "endTime": "05:29", "limit": 100, "enabled": True, "supported": True},
        "dtg": {"id": "d1", "startTime": "23:30", "endTime": "00:10", "limit": 30, "enabled": False, "supported": True},
        "rbd": {"id": None, "startTime": None, "endTime": None, "limit": None, "enabled": False, "supported": True},
    }
    api.sync_local_schedule_from_cloud(site_id)
    local = api.local_schedule[site_id]
    assert local["reserve"] == 30  # matches the cloud batteryBackupPercentage, not the default 0
    assert local["charge"]["start_time"] == "01:10:00"
    assert local["charge"]["end_time"] == "05:29:00"
    assert local["charge"]["soc"] == 100
    assert local["charge"]["enable"] is True
    assert local["export"]["start_time"] == "23:30:00"
    assert local["export"]["enable"] is False

    # Published control entities now reflect the seeded values, not defaults.
    api.battery_settings[site_id] = {"veryLowSocMin": 5}
    run_async(api.publish_schedule_settings_ha(site_id))
    items = api.dashboard_items
    assert items["number.predbat_enphase_12345_battery_schedule_reserve"]["state"] == 30
    assert items["select.predbat_enphase_12345_battery_schedule_charge_start_time"]["state"] == "01:10:00"
    assert items["switch.predbat_enphase_12345_battery_schedule_charge_enable"]["state"] == "on"

    # Seeding is one-time: a later cloud change (or a user edit) is not clobbered by re-sync.
    local["reserve"] = 45  # simulate a Predbat/user edit
    api.profile[site_id]["reserve"] = 99  # cloud changed externally
    api.sync_local_schedule_from_cloud(site_id)
    assert api.local_schedule[site_id]["reserve"] == 45  # not re-seeded


def test_publish_schedule_entities():
    """Both the charge and export window controls are published (a configured inverter has DTG)."""
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.battery_settings["12345"] = {"veryLowSocMin": 5}
    run_async(api.publish_schedule_settings_ha("12345"))
    items = api.dashboard_items
    assert "select.predbat_enphase_12345_battery_schedule_charge_start_time" in items
    assert "number.predbat_enphase_12345_battery_schedule_charge_soc" in items
    assert "switch.predbat_enphase_12345_battery_schedule_charge_write" in items
    assert "number.predbat_enphase_12345_battery_schedule_reserve" in items
    assert "select.predbat_enphase_12345_battery_schedule_export_start_time" in items
    assert "number.predbat_enphase_12345_battery_schedule_export_soc" in items


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


def test_reserve_number_event_writes_immediately():
    """A reserve number change is written to Enphase at once (like Fox), not deferred to the button."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 30}
    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {"message": "success"})
    run_async(api.number_event("number.predbat_enphase_12345_battery_schedule_reserve", 25))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/profile/12345")]
    assert len(puts) == 1  # written immediately
    assert puts[0]["json"]["batteryBackupPercentage"] == 25
    assert api.profile["12345"]["reserve"] == 25  # cached
    # A no-op change (already 25) does not write again
    api.request_log.clear()
    run_async(api.number_event("number.predbat_enphase_12345_battery_schedule_reserve", 25))
    assert [r for r in api.request_log if r["method"] == "PUT"] == []


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


def test_schedules_equal_none_limit():
    """schedules_equal must not crash when the cloud entry has a present-but-None limit key."""
    from enphase import schedules_equal

    cloud = {"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": None, "enabled": True}
    # We want a specific limit but the cloud entry has none - not equal, no crash.
    assert not schedules_equal(cloud, "02:00", "05:00", 90, True)
    # We don't require a limit at all and windows match - equal.
    assert schedules_equal(cloud, "02:00", "05:00", None, True)


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


def _apply_export_case(export_soc):
    """Run apply with a given export target SOC and return the schedule POST scheduleTypes."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "23:00:00", "end_time": "23:30:00", "soc": export_soc, "enable": True},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    return posts


def test_apply_export_target_selects_dtg_rbd_or_none():
    """Export target drives the family: <99 -> DTG (real export), ==99 -> RBD (freeze), ==100 -> none."""
    # Freeze export: target exactly 99 -> restrict battery discharge (RBD), no DTG.
    freeze_posts = _apply_export_case(99)
    freeze_types = [p["json"]["scheduleType"] for p in freeze_posts]
    assert "RBD" in freeze_types and "DTG" not in freeze_types
    rbd = next(p["json"] for p in freeze_posts if p["json"]["scheduleType"] == "RBD")
    assert rbd["startTime"] == "23:00" and rbd["endTime"] == "23:30" and rbd["isEnabled"] is True

    # Real export: target below 99 -> DTG to that floor, no RBD.
    export_posts = _apply_export_case(30)
    export_types = [p["json"]["scheduleType"] for p in export_posts]
    assert "DTG" in export_types and "RBD" not in export_types
    dtg = next(p["json"] for p in export_posts if p["json"]["scheduleType"] == "DTG")
    assert dtg["limit"] == 30 and dtg["isEnabled"] is True

    # Target of 100 is the same as disabled: neither DTG nor RBD is written.
    none_posts = _apply_export_case(100)
    none_types = [p["json"]["scheduleType"] for p in none_posts]
    assert "DTG" not in none_types and "RBD" not in none_types


def test_apply_export_dtg_limit_clamped_to_reserve():
    """An export target below the reserve is clamped up to the reserve (Enphase won't go below it)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 30}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 30,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "16:00:00", "end_time": "19:00:00", "soc": 10, "enable": True},  # target 10 < reserve 30
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    dtg = next(r["json"] for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules") and r["json"]["scheduleType"] == "DTG")
    assert dtg["limit"] == 30  # clamped up to the reserve, not the requested 10
    assert dtg["isEnabled"] is True


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
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})  # activation PUT
    api.set_http_response(
        "/service/batteryConfig/api/v1/battery/sites/12345/schedules",
        200,
        {"cfg": {"scheduleSupported": True, "details": [{"id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "isEnabled": True}]}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}},
    )
    run_async(api.apply_battery_schedule("12345"))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/schedules/u1")]
    assert len(puts) == 1
    # On success the cached cloud copy is optimistically updated to the written state (no re-read on update).
    cfg = api.schedules["12345"]["cfg"]
    assert cfg["startTime"] == "02:00" and cfg["endTime"] == "05:00" and cfg["limit"] == 90 and cfg["enabled"] is True
    assert cfg["id"] == "u1"  # id preserved


def test_apply_caches_write_no_rewrite():
    """After a successful update, a second apply with the same desired state issues no write (cache hit)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "01:00", "endTime": "04:00", "limit": 80, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules/u1", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})  # activation PUT
    run_async(api.apply_battery_schedule("12345"))
    run_async(api.apply_battery_schedule("12345"))  # second apply, unchanged desired state
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/schedules/u1")]
    assert len(puts) == 1  # written once, not re-written (optimistic cache prevents churn)


def test_set_reserve_caches_written_value():
    """set_reserve optimistically caches the written reserve on success (no confirm re-read needed)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 30}
    api.set_http_response("/service/batteryConfig/api/v1/profile/12345", 200, {"message": "success"})
    run_async(api.set_reserve("12345", 25))
    assert api.profile["12345"]["reserve"] == 25


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


def test_activate_cfg_mode():
    """_activate_cfg_mode PUTs batterySettings with acceptedItcDisclaimer to transition CFG from pending to active."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.battery_settings.clear()
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api._activate_cfg_mode("12345"))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    assert len(puts) == 1
    body = puts[0]["json"]
    assert body["chargeFromGrid"] is True
    assert "acceptedItcDisclaimer" in body
    # Verify required query params are sent
    assert puts[0].get("params") == {"source": "enho", "userId": "9999"}
    # Timestamp must be a recent UTC ISO-8601 string (e.g. "2026-07-14T20:25:00+00:00")
    from datetime import datetime as dt, timezone as tz

    parsed = dt.fromisoformat(body["acceptedItcDisclaimer"])
    age = (dt.now(tz.utc) - parsed).total_seconds()
    assert -5 <= age <= 60, f"acceptedItcDisclaimer age {age}s outside [-5, 60] — stale or future timestamp"
    # Cached battery settings updated optimistically on success
    assert api.battery_settings["12345"]["chargeFromGrid"] is True


def test_apply_activates_cfg_after_write():
    """apply_battery_schedule activates CFG mode after a schedule write when charge is enabled."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": False, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    # _ensure_charge_from_grid paths
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/12345", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    # _write_schedule (POST) path
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    # CFG schedule was created (POST to schedules)
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    assert len(posts) == 1
    # Exactly one PUT to batterySettings: the activation call. The one-time disclaimer is a
    # separate acceptDisclaimer POST, and enabling chargeFromGrid is folded into the activation
    # PUT, so there is no redundant second batterySettings write.
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    assert len(puts) == 1
    activation_puts = [p for p in puts if p.get("json", {}).get("acceptedItcDisclaimer")]
    assert len(activation_puts) == 1
    assert activation_puts[0]["json"]["chargeFromGrid"] is True
    assert activation_puts[0].get("params") == {"source": "enho", "userId": "9999"}


def test_apply_no_activate_cfg_when_charge_disabled():
    """_activate_cfg_mode is NOT called when the charge window is disabled."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    # Only the CFG write (no cloud cfg exists, so it will POST to schedules).
    # charge is disabled, so no activation call.
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    # No PUTs to batterySettings (no _ensure_charge_from_grid, no _activate_cfg_mode)
    puts = [r for r in api.request_log if r["method"] == "PUT" and "batterySettings" in r["path"]]
    assert puts == []


def test_apply_no_activate_cfg_when_unchanged():
    """_activate_cfg_mode is NOT called when the CFG schedule already matches (no-op write)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    # Cloud CFG already matches the local desired state
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    run_async(api.apply_battery_schedule("12345"))
    # No writes at all (schedules_match -> True, _ensure_charge_from_grid already done)
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT")]
    assert writes == []


def test_activate_dtg_mode():
    """_activate_dtg_mode PUTs batterySettings with dtgControl.enabled to transition DTG from pending to active."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.battery_settings.clear()
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api._activate_dtg_mode("12345"))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    assert len(puts) == 1
    body = puts[0]["json"]
    assert body == {"dtgControl": {"enabled": True}}
    # Verify required query params are sent
    assert puts[0].get("params") == {"source": "enho", "userId": "9999"}
    # Cached battery settings updated on success
    assert api.battery_settings["12345"]["dtgControl"]["enabled"] is True


def test_activate_rbd_mode():
    """_activate_rbd_mode PUTs batterySettings with rbdControl.enabled to transition RBD from pending to active."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.battery_settings.clear()
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api._activate_rbd_mode("12345"))
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    assert len(puts) == 1
    body = puts[0]["json"]
    assert body == {"rbdControl": {"enabled": True}}
    # Verify required query params are sent
    assert puts[0].get("params") == {"source": "enho", "userId": "9999"}
    assert api.battery_settings["12345"]["rbdControl"]["enabled"] is True


def test_apply_activates_dtg_after_write():
    """apply_battery_schedule activates DTG mode after a schedule write when real export is enabled."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "16:00:00", "end_time": "19:00:00", "soc": 60, "enable": True},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api.apply_battery_schedule("12345"))
    # DTG schedule was created (POST to schedules with scheduleType DTG)
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    assert len(posts) == 1
    assert posts[0]["json"]["scheduleType"] == "DTG"
    # Activation PUT with dtgControl.enabled
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    activation = [p for p in puts if p.get("json", {}).get("dtgControl", {}).get("enabled") is True]
    assert len(activation) == 1
    assert activation[0].get("params") == {"source": "enho", "userId": "9999"}


def test_apply_activates_rbd_after_write():
    """apply_battery_schedule activates RBD mode after a schedule write when freeze export is enabled."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "22:00:00", "end_time": "23:00:00", "soc": 99, "enable": True},
    }
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api.apply_battery_schedule("12345"))
    # RBD schedule was created (POST to schedules with scheduleType RBD)
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    assert len(posts) == 1
    assert posts[0]["json"]["scheduleType"] == "RBD"
    # Activation PUT with rbdControl.enabled
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    activation = [p for p in puts if p.get("json", {}).get("rbdControl", {}).get("enabled") is True]
    assert len(activation) == 1
    assert activation[0].get("params") == {"source": "enho", "userId": "9999"}


def test_apply_no_activate_dtg_when_export_disabled():
    """_activate_dtg_mode is NOT called when export is disabled (no real or freeze export)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False},
    }
    # No schedule write (no cloud DTG, export disabled → enabled=False, _write_schedule returns False)
    # No HTTP needed — nothing is written.
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    run_async(api.apply_battery_schedule("12345"))
    # No PUTs to batterySettings (no activation for DTG or RBD)
    puts = [r for r in api.request_log if r["method"] == "PUT" and "batterySettings" in r["path"]]
    assert puts == []


def test_apply_no_activate_dtg_when_freeze_not_real_export():
    """_activate_dtg_mode is NOT called for freeze export (soc == 99 triggers RBD, not DTG)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "22:00:00", "end_time": "23:00:00", "soc": 99, "enable": True},
    }
    # DTG is not written (real_export=False when soc==99), but RBD is.
    api.set_http_response("/service/batteryConfig/api/v1/battery/sites/12345/schedules", 200, {"cfg": {"scheduleSupported": True, "details": []}, "dtg": {"scheduleSupported": True, "details": []}, "rbd": {"scheduleSupported": True, "details": []}})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api.apply_battery_schedule("12345"))
    # RBD schedule was created
    posts = [r for r in api.request_log if r["method"] == "POST" and r["path"].endswith("/schedules")]
    assert len(posts) == 1
    assert posts[0]["json"]["scheduleType"] == "RBD"
    # Activation PUTs: only rbdControl, no dtgControl
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    dtg_activation = [p for p in puts if p.get("json", {}).get("dtgControl")]
    rbd_activation = [p for p in puts if p.get("json", {}).get("rbdControl", {}).get("enabled") is True]
    assert dtg_activation == []
    assert len(rbd_activation) == 1


def test_apply_no_activate_dtg_when_unchanged():
    """_activate_dtg_mode is NOT called when the DTG schedule already matches (no-op write)."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    # Cloud DTG already matches local desired state
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True, "id": "d1", "startTime": "16:00", "endTime": "19:00", "limit": 60, "enabled": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {
        "reserve": 20,
        "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False},
        "export": {"start_time": "16:00:00", "end_time": "19:00:00", "soc": 60, "enable": True},
    }
    run_async(api.apply_battery_schedule("12345"))
    # No writes at all (all schedules match cloud state, no reserve change)
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT")]
    assert writes == []


def test_activate_cfg_mode_invalidates_cache_on_failure():
    """Cache is cleared on CFG activation failure so next apply retries write+activation."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.battery_settings.clear()
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True}}
    # Set up a 404 response — activation will fail
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 404, {})
    ok = run_async(api._activate_cfg_mode("12345"))
    assert ok is False
    # Cached startTime must be cleared so schedules_equal detects a diff next time
    cached = api.schedules["12345"]["cfg"]
    assert cached["startTime"] == ""
    assert "id" in cached  # id preserved for PUT update


def test_activate_dtg_mode_invalidates_cache_on_failure():
    """Cache is cleared on DTG activation failure so next apply retries write+activation."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.battery_settings.clear()
    api.schedules["12345"] = {"dtg": {"supported": True, "id": "d1", "startTime": "16:00", "endTime": "19:00", "limit": 60, "enabled": True}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 404, {})
    ok = run_async(api._activate_dtg_mode("12345"))
    assert ok is False
    cached = api.schedules["12345"]["dtg"]
    assert cached["startTime"] == ""
    assert "id" in cached


def test_activate_rbd_mode_invalidates_cache_on_failure():
    """Cache is cleared on RBD activation failure so next apply retries write+activation."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.battery_settings.clear()
    api.schedules["12345"] = {"rbd": {"supported": True, "id": "r1", "startTime": "22:00", "endTime": "23:00", "limit": 99, "enabled": True}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 404, {})
    ok = run_async(api._activate_rbd_mode("12345"))
    assert ok is False
    cached = api.schedules["12345"]["rbd"]
    assert cached["startTime"] == ""
    assert "id" in cached


def test_apply_activates_pending_schedule_without_rewrite():
    """When a schedule matches but status is pending, activation is called without re-writing."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    # Cloud CFG already matches desired state BUT status is pending
    api.schedules["12345"] = {"cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True, "status": "pending"}, "dtg": {"supported": True}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    # Activation PUT succeeds
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api.apply_battery_schedule("12345"))
    # No schedule write (schedules equal) but activation was called
    writes = [r for r in api.request_log if r["method"] in ("POST", "PUT") and "schedules" in r["path"]]
    assert writes == []
    puts = [r for r in api.request_log if r["method"] == "PUT" and r["path"] == "/service/batteryConfig/api/v1/batterySettings/12345"]
    assert len(puts) == 1  # activation PUT, not the _ensure_charge_from_grid one


def test_is_schedule_pending_null_status_is_not_pending():
    """_is_schedule_pending must treat a None status as not-pending, not crash on None.lower().

    get_schedules always writes a "status" key (set to None when the cloud omits scheduleStatus),
    so the present-but-None case is the common one and must be handled without raising.
    """
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"supported": True, "status": None}}
    assert api._is_schedule_pending("12345", "CFG") is False
    # And the genuine pending case still returns True
    api.schedules["12345"]["cfg"]["status"] = "PENDING"
    assert api._is_schedule_pending("12345", "CFG") is True


def test_apply_no_crash_when_cached_status_none():
    """apply_battery_schedule must not raise when a matching cached schedule has status None.

    Reproduces the get_schedules cache shape (status key present, value None) for a matching,
    enabled CFG schedule. Before the fix this hit None.lower() in _is_schedule_pending and
    aborted the whole apply.
    """
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {
        "cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True, "status": None},
        "dtg": {"supported": True, "status": None},
        "rbd": {"supported": True, "status": None},
    }
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    run_async(api.apply_battery_schedule("12345"))  # must not raise
    # Matching schedule + status None (not "pending") => no activation PUT
    puts = [r for r in api.request_log if r["method"] == "PUT" and "batterySettings" in r["path"]]
    assert puts == []


def test_activate_dtg_mode_clears_pending_status_on_success():
    """A successful DTG activation clears the cached pending marker, like CFG does."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.schedules["12345"] = {"dtg": {"supported": True, "id": "d1", "startTime": "16:00", "endTime": "19:00", "limit": 60, "enabled": True, "status": "pending"}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    assert run_async(api._activate_dtg_mode("12345")) is True
    assert "status" not in api.schedules["12345"]["dtg"]


def test_activate_rbd_mode_clears_pending_status_on_success():
    """A successful RBD activation clears the cached pending marker, like CFG does."""
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.schedules["12345"] = {"rbd": {"supported": True, "id": "r1", "startTime": "22:00", "endTime": "23:00", "limit": 99, "enabled": True, "status": "pending"}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    assert run_async(api._activate_rbd_mode("12345")) is True
    assert "status" not in api.schedules["12345"]["rbd"]


def test_apply_pending_dtg_not_reactivated_after_success():
    """A pending DTG is activated once; the next unchanged apply issues no further activation PUT.

    Without clearing the pending marker on success, every subsequent apply would re-fire the
    activation PUT while the cached status stayed "pending".
    """
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {"cfg": {"supported": True}, "dtg": {"supported": True, "id": "d1", "startTime": "16:00", "endTime": "19:00", "limit": 60, "enabled": True, "status": "pending"}, "rbd": {"supported": True}}
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": True, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": False}, "export": {"start_time": "16:00:00", "end_time": "19:00:00", "soc": 60, "enable": True}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api.apply_battery_schedule("12345"))
    run_async(api.apply_battery_schedule("12345"))
    activation = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/batterySettings/12345") and r.get("json", {}).get("dtgControl")]
    assert len(activation) == 1


def test_apply_activates_cfg_when_setting_off_but_schedule_matches():
    """When the CFG schedule already matches but chargeFromGrid is off, activation still fires.

    Enabling chargeFromGrid is the activation PUT, so a matching schedule that was never activated
    must still be activated rather than silently skipped. Also covers the status-None path (#1).
    """
    api = MockEnphaseAPI()
    api.user_id = "9999"
    api.sites = [{"site_id": "12345", "name": "Home"}]
    api.schedules["12345"] = {
        "cfg": {"supported": True, "id": "u1", "startTime": "02:00", "endTime": "05:00", "limit": 90, "enabled": True, "status": None},
        "dtg": {"supported": True},
        "rbd": {"supported": True},
    }
    api.profile["12345"] = {"profile": "self-consumption", "reserve": 20}
    api.battery_settings["12345"] = {"chargeFromGrid": False, "veryLowSocMin": 5}
    api.local_schedule["12345"] = {"reserve": 20, "charge": {"start_time": "02:00:00", "end_time": "05:00:00", "soc": 90, "enable": True}, "export": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 5, "enable": False}}
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/12345", 200, {})
    api.set_http_response("/service/batteryConfig/api/v1/batterySettings/12345", 200, {})
    run_async(api.apply_battery_schedule("12345"))
    # No schedule write (already matches), but activation fired because chargeFromGrid was off
    schedule_writes = [r for r in api.request_log if r["method"] in ("POST", "PUT") and "schedules" in r["path"]]
    assert schedule_writes == []
    activation = [r for r in api.request_log if r["method"] == "PUT" and r["path"].endswith("/batterySettings/12345") and r.get("json", {}).get("acceptedItcDisclaimer")]
    assert len(activation) == 1
    assert api.battery_settings["12345"]["chargeFromGrid"] is True


SCHEDULES_PATH = "/service/batteryConfig/api/v1/battery/sites/12345/schedules"


def _cfg_detail(schedule_id, start, end, limit=100, deleted=False):
    """Build one CFG schedule detail entry in the shape the cloud returns."""
    return {"scheduleId": schedule_id, "startTime": start, "endTime": end, "limit": limit, "scheduleType": "CFG", "days": [1, 2, 3, 4, 5, 6, 7], "isDeleted": deleted, "isEnabled": True}


def _schedules_payload(cfg_details):
    """Wrap CFG detail entries in a full schedules response with empty dtg/rbd families."""
    return {
        "type": "BATTERY_SCHEDULES_CONFIG",
        "cfg": {"scheduleStatus": "active", "count": len(cfg_details), "details": cfg_details},
        "dtg": {"scheduleStatus": "active", "count": 0, "details": []},
        "rbd": {"scheduleStatus": "active", "count": 0, "details": []},
    }


def test_get_schedules_pins_adopted_schedule_id():
    """A re-read keeps the already-adopted schedule id even when the cloud reorders the list.

    The cloud returns details ordered by updatedAt, so writing to the adopted schedule pushes it
    behind its sibling. Taking details[0] would make Predbat swap to the sibling and then write a
    window that overlaps the one it just wrote, which the cloud rejects with HTTP 409.
    """
    api = MockEnphaseAPI()
    api.mock_ha_states["switch.predbat_set_read_only"] = "on"  # isolate pinning from sibling pruning
    api.schedules["12345"] = {"cfg": {"id": "adopted", "startTime": "02:00", "endTime": "02:20"}}
    # Cloud lists the sibling first, as it does after we write to "adopted"
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("sibling", "05:30", "07:00"), _cfg_detail("adopted", "02:00", "02:20")]))
    run_async(api.get_schedules("12345"))
    cfg = api.schedules["12345"]["cfg"]
    assert cfg["id"] == "adopted"
    assert cfg["startTime"] == "02:00" and cfg["endTime"] == "02:20"


def test_get_schedules_adopts_first_when_none_adopted_yet():
    """With no schedule adopted yet the first non-deleted entry is taken."""
    api = MockEnphaseAPI()
    api.mock_ha_states["switch.predbat_set_read_only"] = "on"
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("gone", "01:00", "02:00", deleted=True), _cfg_detail("first", "05:30", "07:00")]))
    run_async(api.get_schedules("12345"))
    assert api.schedules["12345"]["cfg"]["id"] == "first"


def test_get_schedules_readopts_when_pinned_id_disappears():
    """If the adopted schedule is deleted outside Predbat, the remaining one is adopted instead."""
    api = MockEnphaseAPI()
    api.mock_ha_states["switch.predbat_set_read_only"] = "on"
    api.schedules["12345"] = {"cfg": {"id": "adopted", "startTime": "02:00", "endTime": "02:20"}}
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("survivor", "05:30", "07:00")]))
    run_async(api.get_schedules("12345"))
    assert api.schedules["12345"]["cfg"]["id"] == "survivor"


def test_delete_schedule_posts_to_the_delete_endpoint():
    """Deletion goes through POST /schedules/<id>/delete, the endpoint the cloud actually exposes.

    The API gateway does not allow the DELETE verb on the schedules resource - it answers
    "403 Invalid CORS request" - and a 403 is treated as an auth failure, so every attempt also
    burned a re-login and risked tripping the account's too-many-sessions guard.
    """
    api = MockEnphaseAPI()
    api.set_http_response(SCHEDULES_PATH + "/sched-1/delete", 200, {})
    assert run_async(api._delete_schedule("12345", "sched-1")) is True
    assert [(r["method"], r["path"]) for r in api.request_log] == [("POST", SCHEDULES_PATH + "/sched-1/delete")]


def test_get_schedules_deletes_sibling_schedules_in_write_mode():
    """In write mode Predbat owns one schedule per family and deletes any extras.

    A schedule Predbat does not track keeps a stale window that it can never clear, and that
    window collides with whatever Predbat writes next.
    """
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"id": "adopted", "startTime": "02:00", "endTime": "02:20"}}
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("sibling", "05:30", "07:00"), _cfg_detail("adopted", "02:00", "02:20")]))
    api.set_http_response(SCHEDULES_PATH + "/sibling/delete", 204, None)
    run_async(api.get_schedules("12345"))
    deletes = [r["path"] for r in api.request_log if r["path"].endswith("/delete")]
    assert deletes == [SCHEDULES_PATH + "/sibling/delete"]
    assert api.schedules["12345"]["cfg"]["id"] == "adopted"
    assert api.schedules["12345"]["cfg"]["count"] == 1  # pruned count, not the stale cloud count


def test_get_schedules_keeps_sibling_schedules_in_read_only_mode():
    """Read-only mode must not delete anything on the account."""
    api = MockEnphaseAPI()
    api.mock_ha_states["switch.predbat_set_read_only"] = "on"
    api.schedules["12345"] = {"cfg": {"id": "adopted", "startTime": "02:00", "endTime": "02:20"}}
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("sibling", "05:30", "07:00"), _cfg_detail("adopted", "02:00", "02:20")]))
    run_async(api.get_schedules("12345"))
    assert [r for r in api.request_log if r["path"].endswith("/delete")] == []


def test_write_schedule_disable_deletes_the_schedule():
    """Disabling a window deletes the schedule rather than PUTting isEnabled=False.

    The cloud ignores isEnabled=False on a PUT (it echoes isEnabled true and the window survives),
    so a disabled window would otherwise linger and conflict with later writes.
    """
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"id": "sched-1", "startTime": "02:00", "endTime": "02:20", "limit": 100, "enabled": True}}
    api.set_http_response(SCHEDULES_PATH + "/sched-1/delete", 204, None)
    wrote = run_async(api._write_schedule("12345", "CFG", "00:00:00", "00:00:00", 100, False))
    assert wrote is True
    assert [(r["method"], r["path"]) for r in api.request_log] == [("POST", SCHEDULES_PATH + "/sched-1/delete")]
    assert api.schedules["12345"]["cfg"].get("id") is None  # nothing left to update in place


def test_write_schedule_retries_once_after_conflict():
    """A 409 conflict triggers a schedules re-read and one retry of the write."""
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"id": "sched-1", "startTime": "02:00", "endTime": "02:20", "limit": 100, "enabled": True}}
    api.set_http_sequence(SCHEDULES_PATH + "/sched-1", [(409, {"error": {"status": "CONFLICTING_SCHEDULE_CFG"}}), (200, {"scheduleId": "sched-1"})])
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("sched-1", "02:00", "02:20")]))
    wrote = run_async(api._write_schedule("12345", "CFG", "03:00:00", "04:00:00", 100, True))
    assert wrote is True
    assert [r["method"] for r in api.request_log] == ["PUT", "GET", "PUT"]  # re-read between the attempts
    assert api.schedules["12345"]["cfg"]["startTime"] == "03:00"


def test_write_schedule_gives_up_after_second_conflict():
    """A conflict that survives the re-read fails the write instead of looping."""
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"id": "sched-1", "startTime": "02:00", "endTime": "02:20", "limit": 100, "enabled": True}}
    api.set_http_response(SCHEDULES_PATH + "/sched-1", 409, {"error": {"status": "CONFLICTING_SCHEDULE_CFG"}})
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("sched-1", "02:00", "02:20")]))
    wrote = run_async(api._write_schedule("12345", "CFG", "03:00:00", "04:00:00", 100, True))
    assert wrote is False
    assert [r["method"] for r in api.request_log].count("PUT") == 2  # one retry only


def test_consecutive_writes_stay_on_one_schedule_across_a_reorder():
    """Replay of the live failure: two writes either side of a re-read must target the same schedule.

    Observed on a site with two CFG schedules: Predbat wrote 22:35-23:30 to one, the re-read
    returned the pair in the opposite order, Predbat swapped to the other and wrote 22:50-23:30 -
    which the cloud rejected with CONFLICTING_SCHEDULE_CFG against the window Predbat had set
    itself five minutes earlier. Every subsequent cycle then failed the same way.
    """
    api = MockEnphaseAPI()
    api.schedules["12345"] = {"cfg": {"id": "first", "startTime": "20:30", "endTime": "21:00", "limit": 5, "enabled": True}}
    api.set_http_response(SCHEDULES_PATH + "/first", 200, {"scheduleId": "first"})
    api.set_http_response(SCHEDULES_PATH + "/second/delete", 204, None)
    run_async(api._write_schedule("12345", "CFG", "22:35:00", "23:30:00", 5, True))
    # Cloud re-read now lists the sibling first, because our write made "first" the most recent
    api.set_http_response(SCHEDULES_PATH, 200, _schedules_payload([_cfg_detail("second", "20:30", "21:00", limit=5), _cfg_detail("first", "22:35", "23:30", limit=5)]))
    run_async(api.get_schedules("12345"))
    run_async(api._write_schedule("12345", "CFG", "22:50:00", "23:30:00", 5, True))
    written = [r["path"] for r in api.request_log if r["method"] == "PUT"]
    assert written == [SCHEDULES_PATH + "/first", SCHEDULES_PATH + "/first"]  # never swapped onto the sibling


def run_enphase_api_tests(my_predbat):
    """Run all Enphase API tests, returning 0 on success."""
    test_consecutive_writes_stay_on_one_schedule_across_a_reorder()
    test_get_schedules_pins_adopted_schedule_id()
    test_get_schedules_adopts_first_when_none_adopted_yet()
    test_get_schedules_readopts_when_pinned_id_disappears()
    test_get_schedules_deletes_sibling_schedules_in_write_mode()
    test_get_schedules_keeps_sibling_schedules_in_read_only_mode()
    test_write_schedule_disable_deletes_the_schedule()
    test_write_schedule_retries_once_after_conflict()
    test_write_schedule_gives_up_after_second_conflict()
    test_initialize_defaults()
    test_needs_refresh()
    test_is_alive()
    test_login_success()
    test_is_too_many_sessions()
    test_login_happy_path_with_session_text()
    test_login_too_many_sessions()
    test_login_mfa_rejected()
    test_safe_float_int_handle_na()
    test_get_battery_status_handles_na()
    test_reads_handle_na_values()
    test_log_api_call_redacts_token()
    test_log_api_call_redacts_livestream_credentials()
    test_login_dedupes_sites()
    test_run_single_site_publishes_once()
    test_run_no_battery_returns_false_without_raising()
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
    test_inverter_time_and_system_status_sensors()
    test_log_api_call_suppresses_html()
    test_reads_nested_data_shape()
    test_get_site_settings()
    test_set_reserve_writes_profile_put()
    test_request_json_absorbs_xsrf_token()
    test_login_wall_does_not_corrupt_session()
    test_absorb_cookies_bp_xsrf_name()
    test_get_battery_status_percent_soc()
    test_today_channel_kwh()
    test_interval_power()
    test_interval_power_clamps_at_start_of_day()
    test_load_power_is_derived_from_the_other_channels()
    test_load_power_never_goes_negative()
    test_power_sensors_ignore_the_unsettled_bucket()
    test_decode_livestream_message()
    test_decode_livestream_message_balances()
    test_decode_livestream_message_rejects_rubbish()
    test_livestream_username_carries_the_authorizer_credentials()
    test_gateway_serial_read_from_today()
    test_publish_prefers_the_measured_livestream_reading()
    test_publish_falls_back_to_buckets_without_a_livestream_reading()
    test_live_power_is_never_persisted()
    test_failed_live_read_keeps_the_recent_reading()
    test_live_reading_older_than_the_window_falls_back_to_buckets()
    test_grid_power_is_positive_when_exporting()
    test_grid_power_from_buckets_is_positive_when_exporting()
    test_published_power_satisfies_the_predbat_energy_balance()
    test_get_schedules_parses_families()
    test_automatic_config()
    test_automatic_config_no_dtg_raises()
    test_automatic_config_no_charge_support_raises()
    test_get_schedules_supported_from_status()
    test_get_schedules_pending_family_is_still_supported()
    test_inverter_def_enphase()
    test_run_first_polls_all_tiers()
    test_get_today()
    test_publish_data_sensors()
    test_sync_local_schedule_from_cloud()
    test_publish_schedule_entities()
    test_event_handlers_update_local_schedule()
    test_reserve_number_event_writes_immediately()
    test_write_switch_triggers_apply()
    test_schedules_equal()
    test_schedules_equal_none_limit()
    test_apply_charge_schedule_creates()
    test_apply_export_target_selects_dtg_rbd_or_none()
    test_apply_export_dtg_limit_clamped_to_reserve()
    test_apply_updates_existing_by_id()
    test_apply_caches_write_no_rewrite()
    test_set_reserve_caches_written_value()
    test_apply_no_change_no_write()
    test_activate_cfg_mode()
    test_apply_activates_cfg_after_write()
    test_apply_no_activate_cfg_when_charge_disabled()
    test_apply_no_activate_cfg_when_unchanged()
    test_activate_dtg_mode()
    test_activate_rbd_mode()
    test_apply_activates_dtg_after_write()
    test_apply_activates_rbd_after_write()
    test_apply_no_activate_dtg_when_export_disabled()
    test_apply_no_activate_dtg_when_freeze_not_real_export()
    test_apply_no_activate_dtg_when_unchanged()
    test_activate_cfg_mode_invalidates_cache_on_failure()
    test_activate_dtg_mode_invalidates_cache_on_failure()
    test_activate_rbd_mode_invalidates_cache_on_failure()
    test_apply_activates_pending_schedule_without_rewrite()
    test_is_schedule_pending_null_status_is_not_pending()
    test_apply_no_crash_when_cached_status_none()
    test_activate_dtg_mode_clears_pending_status_on_success()
    test_activate_rbd_mode_clears_pending_status_on_success()
    test_apply_pending_dtg_not_reactivated_after_success()
    test_apply_activates_cfg_when_setting_off_but_schedule_matches()
    print("**** Enphase API tests passed ****")
    return 0

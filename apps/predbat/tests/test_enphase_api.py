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

    def log(self, message):
        """Swallow log output in tests."""
        pass

    def record_api_call(self, *args, **kwargs):
        """Swallow telemetry in tests."""
        pass

    def update_success_timestamp(self):
        """Swallow health-tracking in tests."""
        pass

    def fatal_error_occurred(self):
        """Swallow fatal-error signalling in tests."""
        pass

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
    """MFA-required accounts must fail with a fatal error, not retry."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 200, {"requires_mfa": True})
    assert run_async(api.login()) is False
    assert api.login_reject_count == 1
    assert api.login_cooldown_until is not None


def test_login_guard_rails():
    """Three consecutive rejections suspend login for 24 hours."""
    api = MockEnphaseAPI()
    api.set_http_response("/login/login.json", 401, None)
    for _ in range(3):
        api.login_cooldown_until = None  # expire cooldown to allow next attempt
        run_async(api.login())
    assert api.login_reject_count == 3
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


def run_enphase_api_tests(my_predbat):
    """Run all Enphase API tests, returning 0 on success."""
    test_initialize_defaults()
    test_needs_refresh()
    test_is_alive()
    test_login_success()
    test_login_mfa_rejected()
    test_login_guard_rails()
    test_login_reuse_window()
    test_get_headers_site()
    test_get_headers_battery_config()
    print("**** Enphase API tests passed ****")
    return 0

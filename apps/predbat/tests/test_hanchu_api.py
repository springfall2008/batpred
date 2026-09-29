# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Hanchu ESS cloud API and auth
# -----------------------------------------------------------------------------

"""Tests for the Hanchu cloud component's request layer, auth and poll tiers (``hanchu.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from hanchu import HanchuAPI
from hanchu_const import (
    HANCHU_KEY_CHARGE_POWER,
    HANCHU_KEY_CHARGE_SOC,
    HANCHU_TOKEN_REFRESH_SECONDS,
    HANCHU_WRITE_RETRY_DELAY,
)
from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session, run_async as run_async_local


class MockHanchu(HanchuAPI):
    """Test double: build a HanchuAPI without the full component lifecycle."""

    def __init__(self, account="tester@example.com", password="hunter2", inverter_sn=None, control_enable=True, automatic=False, automatic_ignore_pv=False, work_mode_control=True, min_write_interval=0):
        """Set up a minimal HanchuAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-hanchu-1"}
        # A real clock, not a MagicMock attribute: ComponentBase.minutes_now derives from
        # base.now_utc, so the mock base has to carry a coherent now_utc/midnight_utc pair.
        # set_mock_clock() below moves it; writing base.minutes_now alone does nothing.
        self.base.now_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.midnight_utc = self.base.now_utc
        self.base.minutes_now = 0
        self.state = {}
        self.published = {}
        # api_delay 0 rather than initialize()'s real default: it is a courtesy pause between
        # consecutive calls to a real cloud, and nothing here talks to one - every request is a
        # scripted mock. Left at 1 it would be seconds of dead wall-clock per multi-call test.
        # min_write_interval defaults to 0 so a test exercises the write path rather than pacing;
        # a pacing test passes its own value.
        self.initialize(
            account=account,
            password=password,
            inverter_sn=inverter_sn,
            automatic=automatic,
            automatic_ignore_pv=automatic_ignore_pv,
            control_enable=control_enable,
            work_mode_control=work_mode_control,
            api_delay=0,
            min_write_interval=min_write_interval,
        )
        # The write-retry pause is a real 5s wait against a real cloud. Nothing here talks to one,
        # so it is zeroed - a test that wants to prove the retry happens counts the requests, which
        # the pause does not affect. test_hanchu_retry_delay_default asserts the shipped value.
        self.write_retry_delay = 0

    def set_mock_clock(self, minutes_now):
        """Move the mock base's clock to minutes_now past midnight."""
        self.base.now_utc = self.base.midnight_utc + timedelta(minutes=minutes_now)
        self.base.minutes_now = minutes_now

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity instead of reaching Home Assistant."""
        self.published[entity] = {"state": state, "attributes": attributes}
        self.state[entity] = state

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read back whatever the test (or dashboard_item) put in self.state."""
        return self.state.get(entity_id, default)

    def set_arg_auto(self, arg, value):
        """Record an auto-discovered apps.yaml binding."""
        self.base.args[arg] = value

    def refresh_discovery(self):
        """No-op: the discovery record has its own tests and needs no coordinator here."""
        pass

    @property
    def storage(self):
        """No Storage component in unit tests - matches a standalone CLI run."""
        return None


def envelope(data=None, success=True, code=200, msg=None):
    """Build a Hanchu response envelope.

    success defaults to True only for code 200. The client treats `success` as the verdict, so a
    helper that defaulted every envelope to success would make a failure envelope read as a success
    and let tests pass for the wrong reason.
    """
    if code != 200:
        success = False
    return {"code": code, "success": success, "msg": msg if msg is not None else ("Success" if success else "Failed"), "data": data}


def logged(client, fragment):
    """Whether any captured log line contains fragment."""
    return any(fragment in message for message in client.log_messages)


def mock_json_response(status=200, json_data=None):
    """Build a mock aiohttp response whose .json() tolerates hanchu.py's content_type kwarg.

    ``create_aiohttp_mock_response()``'s json() coroutine takes no arguments, but hanchu.py always
    calls ``response.json(content_type=None)``: the Hanchu API does not reliably set an
    application/json Content-Type header, so the real code asks aiohttp to parse the body whatever
    the header says. The coroutine is rebound here to accept and ignore whatever kwargs it is
    called with - the same fix test_sunsynk_api.py and test_sigenergy.py already apply for exactly
    the same reason.
    """
    response = create_aiohttp_mock_response(status=status, json_data=json_data)

    async def _json(*args, **kwargs):
        """Return the canned JSON payload regardless of the content_type kwarg passed."""
        return {} if json_data is None else json_data

    response.json = _json
    return response


def patched_session(responses):
    """Patch aiohttp.ClientSession so the component's posts get `responses` in order."""
    mock_responses = [mock_json_response(status=status, json_data=body) for status, body in responses]
    return patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(mock_responses))


DEVICE_LIST_SAMPLE = [
    {"sn": "HC240100001", "devType": "2"},
    {"sn": "HC240100002", "devType": "2"},
    # A non-DTU entry, which must be skipped: only device type 2 accepts the control endpoints.
    {"sn": "BATTERY0001", "devType": "3"},
]

STATUS_SAMPLE = {
    # A FRACTION, not a percent - the component multiplies by 100.
    "batSoc": 0.62,
    # Positive on CHARGE, in kW per its own unit sibling. Predbat wants negative on charge.
    "batP": 2.4,
    "batPUnit": "kW",
    # Positive on IMPORT, in watts. Predbat wants negative on import.
    "meterPPwr": 1500,
    "meterPPwrUnit": "W",
    "pvTtPwr": 3.1,
    "pvTtPwrUnit": "kW",
    "loadPwr": 900,
    "bmsDesignCap": 10.24,
    "stationId": "ST-9001",
}

STATISTICS_SAMPLE = {"load": 12.5, "gridImport": 4.2, "gridExport": 1.1, "pv": 9.8, "batCharge": 5.5, "batDisCharge": 4.9}

MENU_SAMPLE = {
    "data": {
        "energy": {
            "items": [
                [
                    {"itemCodeSignal": "CHG_PWR_LMT", "minVal": "0", "maxVal": "7000"},
                    {"itemCodeSignal": "CHG_BAT_SOC_LMT", "minVal": "40", "maxVal": "100"},
                ],
                [
                    # An item for a key Predbat never writes must not create a range entry.
                    {"itemCodeSignal": "SOMETHING_ELSE", "minVal": "1", "maxVal": "2"},
                    # Unparseable bounds are skipped rather than defaulted, because a wrong range
                    # silently clamps a good plan.
                    {"itemCodeSignal": "DSCHG_PWR_LMT", "minVal": "low", "maxVal": "high"},
                ],
            ]
        }
    }
}


def test_hanchu_login_sends_the_account_and_stores_the_bare_token():
    """The login body is {account, pwd} and the token arrives as a bare string in `data`."""
    failed = False
    client = MockHanchu()
    with patched_session([(200, envelope(data="tok-abc123"))]) as session_cls:
        ok = run_async_local(client.fetch_token())
    if not ok or client._token != "tok-abc123":
        print(f"ERROR: login did not store the token: ok={ok} token={client._token!r}")
        failed = True
    call = session_cls.return_value.post.call_args
    if call.args[0] != "https://iess3.hanchuess.com/gateway/identify/auth/token":
        print(f"ERROR: login URL {call.args[0]}")
        failed = True
    if call.kwargs["json"] != {"account": "tester@example.com", "pwd": "hunter2"}:
        print(f"ERROR: login body {call.kwargs['json']}")
        failed = True
    # The login itself is anonymous, so it must not carry a token header.
    if "access-token" in call.kwargs["headers"]:
        print("ERROR: the login request carried an access-token header")
        failed = True
    if call.kwargs["headers"].get("appPlat") != "ha":
        print(f"ERROR: appPlat header {call.kwargs['headers'].get('appPlat')}")
        failed = True
    assert not failed, "test_hanchu_login_sends_the_account_and_stores_the_bare_token"


def test_hanchu_login_failure_names_the_credentials():
    """A rejected login points the user at the two config keys rather than at a trace."""
    failed = False
    client = MockHanchu()
    with patched_session([(200, envelope(success=False, code=100, msg="account or password error"))]):
        ok = run_async_local(client.fetch_token())
    if ok or client._token:
        print(f"ERROR: a rejected login must store no token: ok={ok} token={client._token!r}")
        failed = True
    if not logged(client, "hanchu_account"):
        print("ERROR: the failure message should name hanchu_account")
        failed = True
    if client.last_api_error != "account or password error":
        print(f"ERROR: last_api_error {client.last_api_error!r} should carry the API's own msg")
        failed = True
    assert not failed, "test_hanchu_login_failure_names_the_credentials"


def test_hanchu_password_is_never_logged():
    """The trace masks the password, and nothing else in the envelope."""
    failed = False
    client = MockHanchu(password="super-secret-password")
    with patched_session([(200, envelope(data="tok-abc123"))]):
        run_async_local(client.fetch_token())
    joined = "\n".join(client.log_messages)
    if "super-secret-password" in joined:
        print("ERROR: the password reached the log")
        failed = True
    if "tok-abc123" in joined:
        print("ERROR: the token reached the log")
        failed = True
    # The trace has to remain useful: nobody on the project has Hanchu hardware, so a tester's log
    # is the only evidence there is.
    if not logged(client, "identify/auth/token"):
        print("ERROR: the request itself was not traced at all")
        failed = True
    assert not failed, "test_hanchu_password_is_never_logged"


def test_hanchu_token_refresh_falls_back_to_a_full_login():
    """A refusal to refresh logs in again rather than giving up - Predbat holds the credentials.

    The reference integration has to ask Home Assistant to re-authenticate here, because it holds
    only a token. Predbat holds the account itself, so this path is always recoverable.
    """
    failed = False
    client = MockHanchu()
    client._token = "tok-old"
    client._token_time = time.time() - HANCHU_TOKEN_REFRESH_SECONDS - 1
    with patched_session([(200, envelope(success=False, code=100, msg="token invalid")), (200, envelope(data="tok-new"))]):
        ok = run_async_local(client.refresh_token())
    if not ok or client._token != "tok-new":
        print(f"ERROR: refresh fallback did not re-login: ok={ok} token={client._token!r}")
        failed = True
    if not logged(client, "logging in again"):
        print("ERROR: the fallback to a full login was not explained")
        failed = True
    assert not failed, "test_hanchu_token_refresh_falls_back_to_a_full_login"


def test_hanchu_refresh_is_suppressed_inside_its_window():
    """A burst of expiries cannot become a refresh storm."""
    failed = False
    client = MockHanchu()
    client._token = "tok-old"
    client._last_refresh_attempt = time.time()
    with patched_session([(200, envelope(data="tok-new"))]) as session_cls:
        ok = run_async_local(client.refresh_token())
    if not ok:
        print("ERROR: a suppressed refresh should still report the held token as usable")
        failed = True
    if session_cls.return_value.post.call_count:
        print(f"ERROR: {session_cls.return_value.post.call_count} request(s) went out inside the suppression window")
        failed = True
    if client._token != "tok-old":
        print(f"ERROR: token changed to {client._token!r} without a request")
        failed = True
    assert not failed, "test_hanchu_refresh_is_suppressed_inside_its_window"


def test_hanchu_ensure_token_refreshes_only_when_due():
    """A fresh token is left alone; an old one is refreshed; none at all triggers a login."""
    failed = False
    client = MockHanchu()
    client._token = "tok-fresh"
    client._token_time = time.time()
    with patched_session([]) as session_cls:
        ok = run_async_local(client.ensure_token())
    if not ok or session_cls.return_value.post.call_count:
        print(f"ERROR: a fresh token should need no request: ok={ok} calls={session_cls.return_value.post.call_count}")
        failed = True

    client = MockHanchu()
    with patched_session([(200, envelope(data="tok-first"))]) as session_cls:
        ok = run_async_local(client.ensure_token())
    if not ok or client._token != "tok-first":
        print(f"ERROR: no token should trigger a login: ok={ok} token={client._token!r}")
        failed = True
    if session_cls.return_value.post.call_args.args[0].endswith("/refresh"):
        print("ERROR: with no token at all the component must log in, not call refresh")
        failed = True
    assert not failed, "test_hanchu_ensure_token_refreshes_only_when_due"


def test_hanchu_401_inside_a_200_triggers_one_reauth_and_one_retry():
    """This API answers an expired session as HTTP 200 carrying code 401, not only as a status."""
    failed = False
    client = MockHanchu()
    client._token = "tok-old"
    client._token_time = time.time()
    responses = [
        # The real call, refused for an expired session.
        (200, envelope(success=False, code=401)),
        # The re-login.
        (200, envelope(data="tok-new")),
        # The retry, which succeeds.
        (200, envelope(data=DEVICE_LIST_SAMPLE)),
    ]
    with patched_session(responses) as session_cls:
        serials = run_async_local(client.get_device_list())
    if serials != ["HC240100001", "HC240100002"]:
        print(f"ERROR: the retry after re-auth did not return the device list: {serials}")
        failed = True
    if session_cls.return_value.post.call_count != 3:
        print(f"ERROR: expected 3 requests (call, re-auth, retry), got {session_cls.return_value.post.call_count}")
        failed = True
    # The retry has to carry the NEW token, not the dead one.
    if session_cls.return_value.post.call_args.kwargs["headers"].get("access-token") != "tok-new":
        print(f"ERROR: the retry carried {session_cls.return_value.post.call_args.kwargs['headers'].get('access-token')!r}")
        failed = True
    assert not failed, "test_hanchu_401_inside_a_200_triggers_one_reauth_and_one_retry"


def test_hanchu_reauth_is_attempted_only_once_per_call():
    """A persistent 401 must not loop between the call and the re-auth."""
    failed = False
    client = MockHanchu()
    client._token = "tok-old"
    client._token_time = time.time()
    responses = [
        (200, envelope(success=False, code=401)),
        (200, envelope(data="tok-new")),
        # Still refused after the re-auth: the component must give up rather than re-auth again.
        (200, envelope(success=False, code=401)),
    ]
    with patched_session(responses) as session_cls:
        ok, _data = run_async_local(client._post("device_list"))
    if ok:
        print("ERROR: a persistent 401 should not report success")
        failed = True
    if session_cls.return_value.post.call_count != 3:
        print(f"ERROR: expected exactly 3 requests, got {session_cls.return_value.post.call_count} - the re-auth loop is not capped")
        failed = True
    assert not failed, "test_hanchu_reauth_is_attempted_only_once_per_call"


def test_hanchu_device_list_keeps_only_the_dtu_and_honours_the_filter():
    """Only device type 2 accepts the control endpoints; anything else is named and skipped."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    with patched_session([(200, envelope(data=DEVICE_LIST_SAMPLE))]):
        serials = run_async_local(client.get_device_list())
    if serials != ["HC240100001", "HC240100002"]:
        print(f"ERROR: device list {serials}")
        failed = True
    if not logged(client, "BATTERY0001"):
        print("ERROR: the skipped non-DTU entry was not named in the log")
        failed = True
    if client.discovery_ok is not True:
        print(f"ERROR: discovery_ok {client.discovery_ok}")
        failed = True

    client = MockHanchu(inverter_sn="HC240100002")
    client._token = "tok"
    client._token_time = time.time()
    with patched_session([(200, envelope(data=DEVICE_LIST_SAMPLE))]):
        serials = run_async_local(client.get_device_list())
    if serials != ["HC240100002"]:
        print(f"ERROR: filtered device list {serials}")
        failed = True
    assert not failed, "test_hanchu_device_list_keeps_only_the_dtu_and_honours_the_filter"


def test_hanchu_failed_discovery_keeps_the_known_devices():
    """A transient discovery failure must not take a working component down. Absence is not a result."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    with patched_session([(200, envelope(success=False, code=100, msg="server busy"))]):
        ok = run_async_local(client.refresh_static())
    if ok:
        print("ERROR: a failed discovery must not report success")
        failed = True
    if client.device_list != ["HC240100001"]:
        print(f"ERROR: the known device list was lost: {client.device_list}")
        failed = True
    if client.tier_expired("static", 480) is not True:
        print("ERROR: the static tier clock must not advance on a failure, or a restart skips re-discovery")
        failed = True
    assert not failed, "test_hanchu_failed_discovery_keeps_the_known_devices"


def test_hanchu_empty_account_is_distinguished_from_a_failed_call():
    """Both produce an empty list, so discovery_ok is what tells them apart."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    with patched_session([(200, envelope(data=[]))]):
        ok = run_async_local(client.refresh_static())
    if ok:
        print("ERROR: an empty account is not a successful startup")
        failed = True
    if client.discovery_ok is not True:
        print("ERROR: discovery_ok should be True - the call itself worked")
        failed = True
    if not logged(client, "no DTU devices"):
        print("ERROR: the empty account was not explained")
        failed = True
    assert not failed, "test_hanchu_empty_account_is_distinguished_from_a_failed_call"


def test_hanchu_status_payload_applies_scale_units_and_sign():
    """SoC is a fraction, powers carry their own units, and two fields have Predbat's sign flipped."""
    failed = False
    client = MockHanchu()
    if not client._apply_status_payload("HC240100001", STATUS_SAMPLE):
        print("ERROR: a payload with a SoC should be accepted")
        failed = True
    values = client.device_values.get("HC240100001", {})
    expected = {
        # 0.62 * 100
        "soc": 62.0,
        # 2.4 kW positive-on-charge becomes -2400 W, which is Predbat's convention
        "battery_power": -2400.0,
        # 1500 W positive-on-import becomes -1500 W
        "grid_power": -1500.0,
        # 3.1 kW, not negated
        "pv_power": 3100.0,
        # 900 with no unit sibling: above the heuristic threshold, so watts
        "load_power": 900.0,
    }
    for leaf, value in expected.items():
        if values.get(leaf) != value:
            print(f"ERROR: {leaf} = {values.get(leaf)} != {value}")
            failed = True
    detail = client.device_detail.get("HC240100001", {})
    if detail.get("capacity_kwh") != 10.24:
        print(f"ERROR: capacity {detail.get('capacity_kwh')} != 10.24")
        failed = True
    if detail.get("station_id") != "ST-9001":
        print(f"ERROR: station {detail.get('station_id')} != ST-9001")
        failed = True
    # No SoC means nothing Predbat can plan with, so the payload is refused outright.
    if client._apply_status_payload("HC240100002", {"batP": 1.0}):
        print("ERROR: a payload with no SoC should be refused")
        failed = True
    assert not failed, "test_hanchu_status_payload_applies_scale_units_and_sign"


def test_hanchu_energy_counters_are_mapped():
    """The six daily counters land under the Predbat leaf names automatic_config binds."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    with patched_session([(200, envelope(data=STATISTICS_SAMPLE))]):
        ok = run_async_local(client.refresh_energy())
    if not ok:
        print("ERROR: refresh_energy reported failure")
        failed = True
    energy = client.device_energy.get("HC240100001", {})
    for leaf, value in (("load_today", 12.5), ("import_today", 4.2), ("export_today", 1.1), ("pv_today", 9.8), ("battery_charge_today", 5.5), ("battery_discharge_today", 4.9)):
        if energy.get(leaf) != value:
            print(f"ERROR: {leaf} = {energy.get(leaf)} != {value}")
            failed = True
    assert not failed, "test_hanchu_energy_counters_are_mapped"


def test_hanchu_menu_ranges_override_the_defaults():
    """The menu's own per-device bounds win, and an unparseable or unknown item is ignored."""
    failed = False
    client = MockHanchu()
    ranges = client._parse_menu_ranges(MENU_SAMPLE)
    if ranges.get(HANCHU_KEY_CHARGE_POWER) != (0, 7000):
        print(f"ERROR: menu charge power range {ranges.get(HANCHU_KEY_CHARGE_POWER)} != (0, 7000)")
        failed = True
    if ranges.get(HANCHU_KEY_CHARGE_SOC) != (40, 100):
        print(f"ERROR: menu charge SoC range {ranges.get(HANCHU_KEY_CHARGE_SOC)} != (40, 100)")
        failed = True
    if "SOMETHING_ELSE" in ranges:
        print("ERROR: a menu item for a key Predbat never writes created a range")
        failed = True
    if "DSCHG_PWR_LMT" in ranges:
        print("ERROR: unparseable bounds should be skipped, not accepted")
        failed = True
    # ranges_for merges the menu over the built-in defaults, so the unparseable key keeps its
    # default and the derived ratings follow the menu where it spoke.
    client.device_ranges["HC240100001"] = ranges
    merged = client.ranges_for("HC240100001")
    if merged.get(HANCHU_KEY_CHARGE_POWER) != (0, 7000) or merged.get("DSCHG_PWR_LMT") != (0, 5000):
        print(f"ERROR: merged ranges {merged.get(HANCHU_KEY_CHARGE_POWER)} {merged.get('DSCHG_PWR_LMT')}")
        failed = True
    if client.battery_rate_max("HC240100001") != 7000.0:
        print(f"ERROR: battery_rate_max {client.battery_rate_max('HC240100001')} should follow the menu's charge power ceiling")
        failed = True
    if client.inverter_limit("HC240100001") != 5000.0:
        print(f"ERROR: inverter_limit {client.inverter_limit('HC240100001')} should follow the discharge power ceiling")
        failed = True
    # Junk must not raise inside the poll loop.
    for junk in (None, {}, {"data": None}, {"data": {"energy": []}}):
        if client._parse_menu_ranges(junk) != {}:
            print(f"ERROR: _parse_menu_ranges({junk!r}) should be empty")
            failed = True
    assert not failed, "test_hanchu_menu_ranges_override_the_defaults"


def test_hanchu_battery_rate_max_override_wins():
    """A user who knows their pack's real limit overrides the derived one."""
    failed = False
    client = MockHanchu()
    client.battery_rate_max_override = 3600.0
    if client.battery_rate_max("HC240100001") != 3600.0:
        print(f"ERROR: override ignored: {client.battery_rate_max('HC240100001')}")
        failed = True
    assert not failed, "test_hanchu_battery_rate_max_override_wins"


def test_hanchu_config_read_is_one_call_per_device():
    """Nineteen keys are read in ONE iotGet - the same batching rationale as the write path."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    settings = {"WORK_MODE_CMB": "3", "CHG_PWR_LMT": "2500", "TCT_START_1": 3600, "TCT_END_1": 18000}
    with patched_session([(200, envelope(data=MENU_SAMPLE["data"])), (200, envelope(data=settings))]) as session_cls:
        ok = run_async_local(client.refresh_config())
    if not ok:
        print("ERROR: refresh_config reported failure")
        failed = True
    # Two calls only: one menu, one iotGet. Not one call per key.
    if session_cls.return_value.post.call_count != 2:
        print(f"ERROR: expected 2 requests (menu, iotGet), got {session_cls.return_value.post.call_count}")
        failed = True
    iot_call = session_cls.return_value.post.call_args
    body = iot_call.kwargs["json"]
    if body.get("devType") != "2" or body.get("sn") != "HC240100001":
        print(f"ERROR: iotGet body {body}")
        failed = True
    if len(body.get("keys") or []) != 18:
        print(f"ERROR: iotGet asked for {len(body.get('keys') or [])} keys, expected all 18 in one call")
        failed = True
    if client.device_settings.get("HC240100001", {}).get("WORK_MODE_CMB") != "3":
        print(f"ERROR: settings not stored: {client.device_settings}")
        failed = True
    assert not failed, "test_hanchu_config_read_is_one_call_per_device"


def test_hanchu_transport_failure_is_retried_then_reported():
    """A transport error retries within its budget and then fails closed, never raising."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    with patch("aiohttp.ClientSession", return_value=create_aiohttp_mock_session(exception=OSError("connection reset"))) as session_cls:
        ok, data = run_async_local(client._post("device_list"))
    if ok or data is not None:
        print(f"ERROR: a transport failure should fail closed: ok={ok} data={data!r}")
        failed = True
    if session_cls.return_value.post.call_count != 3:
        print(f"ERROR: expected 3 transport attempts, got {session_cls.return_value.post.call_count}")
        failed = True
    if not logged(client, "transport failure"):
        print("ERROR: the transport failure was not logged")
        failed = True
    assert not failed, "test_hanchu_transport_failure_is_retried_then_reported"


def test_hanchu_run_refuses_to_start_without_credentials():
    """No account or password is a configuration error named in the log, not a crash."""
    failed = False
    client = MockHanchu(account="", password="")
    ok = run_async_local(client.run(0, True))
    if ok:
        print("ERROR: run() should defer startup with no credentials")
        failed = True
    if not logged(client, "hanchu_account"):
        print("ERROR: run() did not name the missing config keys")
        failed = True
    if client.health_message() is None:
        print("ERROR: health_message should explain the missing credentials")
        failed = True
    assert not failed, "test_hanchu_run_refuses_to_start_without_credentials"


def test_hanchu_retry_delay_default():
    """MockHanchu zeroes the write-retry pause, so the shipped value is pinned here instead.

    Without this the tests would be free of the pause AND free of any check on it, and it could
    quietly become zero in production - turning the one retry into an immediate re-post at a device
    that has just said it was busy.
    """
    failed = False
    client = HanchuAPI.__new__(HanchuAPI)
    client.log = lambda message: None
    client.initialize(account="a@b.c", password="p")
    if client.write_retry_delay != HANCHU_WRITE_RETRY_DELAY or not HANCHU_WRITE_RETRY_DELAY:
        print(f"ERROR: the shipped write retry delay is {client.write_retry_delay}, expected a non-zero {HANCHU_WRITE_RETRY_DELAY}")
        failed = True
    assert not failed, "test_hanchu_retry_delay_default"


def run_hanchu_api_tests(my_predbat):
    """Run all Hanchu API and auth tests."""
    failed = False
    for name, fn in [
        ("login", test_hanchu_login_sends_the_account_and_stores_the_bare_token),
        ("login_failure", test_hanchu_login_failure_names_the_credentials),
        ("password_redaction", test_hanchu_password_is_never_logged),
        ("refresh_fallback", test_hanchu_token_refresh_falls_back_to_a_full_login),
        ("refresh_suppression", test_hanchu_refresh_is_suppressed_inside_its_window),
        ("ensure_token", test_hanchu_ensure_token_refreshes_only_when_due),
        ("reauth_retry", test_hanchu_401_inside_a_200_triggers_one_reauth_and_one_retry),
        ("reauth_capped", test_hanchu_reauth_is_attempted_only_once_per_call),
        ("device_list", test_hanchu_device_list_keeps_only_the_dtu_and_honours_the_filter),
        ("discovery_failure", test_hanchu_failed_discovery_keeps_the_known_devices),
        ("empty_account", test_hanchu_empty_account_is_distinguished_from_a_failed_call),
        ("status_payload", test_hanchu_status_payload_applies_scale_units_and_sign),
        ("energy", test_hanchu_energy_counters_are_mapped),
        ("menu_ranges", test_hanchu_menu_ranges_override_the_defaults),
        ("rate_max_override", test_hanchu_battery_rate_max_override_wins),
        ("config_batched_read", test_hanchu_config_read_is_one_call_per_device),
        ("transport_failure", test_hanchu_transport_failure_is_retried_then_reported),
        ("no_credentials", test_hanchu_run_refuses_to_start_without_credentials),
        ("retry_delay_default", test_hanchu_retry_delay_default),
    ]:
        try:
            if fn():
                print(f"  FAILED: hanchu_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in hanchu_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

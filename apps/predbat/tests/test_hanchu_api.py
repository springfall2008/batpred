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
from unittest.mock import AsyncMock, MagicMock, patch
from hanchu import HanchuAPI
from hanchu_const import (
    HANCHU_KEY_CHARGE_POWER,
    HANCHU_KEY_CHARGE_SOC,
    HANCHU_KEY_WORK_MODE,
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
    """The trace masks the password and the account e-mail, and nothing else in the envelope."""
    failed = False
    client = MockHanchu(account="owner@example.com", password="super-secret-password")
    with patched_session([(200, envelope(data="tok-abc123"))]):
        run_async_local(client.fetch_token())
    joined = "\n".join(client.log_messages)
    if "super-secret-password" in joined:
        print("ERROR: the password reached the log")
        failed = True
    if "tok-abc123" in joined:
        print("ERROR: the token reached the log")
        failed = True
    # Testers post these logs on GitHub, so the account e-mail is masked too.
    if "owner@example.com" in joined:
        print("ERROR: the account e-mail reached the log")
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


def test_hanchu_inverter_limit_override_wins():
    """Hanchu units come in 3.68, 5, 6 and 10 kW; a user who knows theirs overrides the derived one."""
    failed = False
    client = MockHanchu()
    client.device_ranges["HC240100001"] = {"DSCHG_PWR_LMT": (0, 5000)}
    if client.inverter_limit("HC240100001") != 5000.0:
        print(f"ERROR: derived limit wrong: {client.inverter_limit('HC240100001')}")
        failed = True
    client.inverter_limit_override = 3680.0
    if client.inverter_limit("HC240100001") != 3680.0:
        print(f"ERROR: override ignored: {client.inverter_limit('HC240100001')}")
        failed = True
    assert not failed, "test_hanchu_inverter_limit_override_wins"


def test_hanchu_default_power_limit_is_warned_once():
    """A menu that answers without usable power bounds leaves the 5 kW default in force - say so, once."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    # MENU_SAMPLE carries a charge power bound but an unparseable discharge one.
    for _ in range(2):
        with patched_session([(200, envelope(data=MENU_SAMPLE["data"])), (200, envelope(data={}))]):
            run_async_local(client.refresh_config())
    warnings = [message for message in client.log_messages if "did not report its power limits" in message]
    if len(warnings) != 1 or "DSCHG_PWR_LMT" not in warnings[0] or "hanchu_inverter_limit" not in warnings[0]:
        print(f"ERROR: expected one warning naming DSCHG_PWR_LMT and the override, got {warnings}")
        failed = True
    # A device whose menu reports both bounds is not warned about.
    quiet = MockHanchu()
    quiet._warn_if_default_power("HC240100002", {"CHG_PWR_LMT": (0, 6000), "DSCHG_PWR_LMT": (0, 6000)})
    if logged(quiet, "did not report its power limits"):
        print("ERROR: warned although both power bounds were reported")
        failed = True
    assert not failed, "test_hanchu_default_power_limit_is_warned_once"


def test_hanchu_menu_answered_by_code_alone_is_used():
    """The menu answers code 200 with no success flag; its ranges must still be applied (batpred#5305, hardware).

    The real envelope is {"code": 200, "msg": ..., "data": {"energy": {...}}} - no success key - and
    the parser receives the inner data, not the whole envelope. Either half wrong and every device
    silently runs on the 5 kW defaults.
    """
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    menu = {"code": 200, "msg": "success", "data": MENU_SAMPLE["data"]}
    with patched_session([(200, menu), (200, envelope(data={"WORK_MODE_CMB": "3"}))]):
        run_async_local(client.refresh_config())
    ranges = client.device_ranges.get("HC240100001", {})
    if ranges.get(HANCHU_KEY_CHARGE_POWER) != (0, 7000) or ranges.get(HANCHU_KEY_CHARGE_SOC) != (40, 100):
        print(f"ERROR: the menu's ranges were not applied: {ranges}")
        failed = True
    if logged(client, "/gateway/app/ha/menu was unsuccessful"):
        print("ERROR: a code-200 menu was logged as a failure")
        failed = True
    if client.battery_rate_max("HC240100001") != 7000.0:
        print(f"ERROR: battery_rate_max {client.battery_rate_max('HC240100001')} should follow the menu")
        failed = True
    assert not failed, "test_hanchu_menu_answered_by_code_alone_is_used"


def test_hanchu_code_alone_is_success_only_for_the_menu():
    """Every other endpoint still needs success: true - a bare code 200 there is not taken as success."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    with patched_session([(200, {"code": 200, "msg": "?", "data": DEVICE_LIST_SAMPLE})]):
        serials = run_async_local(client.get_device_list())
    if serials or client.discovery_ok is not False:
        print(f"ERROR: a device list without success was accepted: {serials} discovery_ok={client.discovery_ok}")
        failed = True
    assert not failed, "test_hanchu_code_alone_is_success_only_for_the_menu"


def test_hanchu_failed_menu_warns_the_default_power_is_in_use():
    """A menu call that fails leaves the 5 kW default in force too, so it is warned about, once."""
    failed = False
    client = MockHanchu()
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    for _ in range(2):
        with patched_session([(200, envelope(code=500, msg="busy")), (200, envelope(data={}))]):
            run_async_local(client.refresh_config())
    warnings = [message for message in client.log_messages if "did not report its power limits" in message]
    if len(warnings) != 1:
        print(f"ERROR: expected one default-power warning after a failed menu, got {warnings}")
        failed = True
    assert not failed, "test_hanchu_failed_menu_warns_the_default_power_is_in_use"


def test_hanchu_ac_coupled_pv_is_counted_in_pv_power():
    """On an AC-coupled system the solar is only in bypMeterTotalPower, so pv_power must include it (batpred#5305, hardware).

    bypMeterSwitch is 1 on AC-coupled systems and 0 on DC-coupled ones. The readings are upton68's
    from a real AC-coupled unit and a contributor's DC-coupled one.
    """
    failed = False
    client = MockHanchu()
    ac_coupled = {"batSoc": 0.5, "batP": 408.0, "batPUnit": "W", "meterPPwr": 0, "meterPPwrUnit": "W", "pvTtPwr": 0.0, "pvTtPwrUnit": "W", "bypMeterTotalPower": 686.8, "bypMeterTotalPowerUnit": "W", "bypMeterSwitch": 1, "loadPwr": 278.8}
    client._apply_status_payload("HC1", ac_coupled)
    values = client.device_values["HC1"]
    if values.get("pv_power") != 686.8 or values.get("ac_pv_power") != 686.8:
        print(f"ERROR: AC-coupled pv_power {values.get('pv_power')} ac_pv_power {values.get('ac_pv_power')}, expected both 686.8")
        failed = True
    dc_coupled = dict(ac_coupled, pvTtPwr=15.0, bypMeterTotalPower=0, bypMeterSwitch=0)
    client._apply_status_payload("HC2", dc_coupled)
    if client.device_values["HC2"].get("pv_power") != 15.0:
        print(f"ERROR: DC-coupled pv_power {client.device_values['HC2'].get('pv_power')}, expected 15.0")
        failed = True
    # A bypass reading on a unit that says it has no bypass meter is not added: the switch decides.
    stray = dict(dc_coupled, bypMeterTotalPower=500.0)
    client._apply_status_payload("HC3", stray)
    if client.device_values["HC3"].get("pv_power") != 15.0:
        print(f"ERROR: pv_power {client.device_values['HC3'].get('pv_power')} took a bypass reading with bypMeterSwitch 0")
        failed = True
    # With no switch reported at all, behaviour is unchanged.
    unknown = {key: value for key, value in ac_coupled.items() if key != "bypMeterSwitch"}
    client._apply_status_payload("HC4", unknown)
    if client.device_values["HC4"].get("pv_power") != 0.0:
        print(f"ERROR: pv_power {client.device_values['HC4'].get('pv_power')} changed without a bypMeterSwitch")
        failed = True
    assert not failed, "test_hanchu_ac_coupled_pv_is_counted_in_pv_power"


# The phone app's menu as a device using the TCT_/TDT_ slot keys describes it: a time item carries both
# of its signals comma-separated (the vendor card splits them), and its listener names the work modes
# the slots are shown - and so run - in.
MENU_SLOTS = {
    "energy": {
        "items": [
            [
                {"itemCodeSignal": "WORK_MODE_CMB", "itemType": "3"},
                {"itemCode": "chg_time_1", "itemCodeSignal": "TCT_START_1,TCT_END_1", "itemType": "6", "listener": {"code": "WORK_MODE_CMB", "show": "3"}},
                {"itemCode": "dschg_time_1", "itemCodeSignal": "TDT_START_1,TDT_END_1", "itemType": "6", "listener": {"code": "WORK_MODE_CMB", "show": "3"}},
            ]
        ]
    }
}

# The other slot format the vendor card handles: array-valued period items (types 82/83) and a
# FLAG_ENABLE_CYCLE enable list, with no TCT_/TDT_ keys at all.
MENU_ARRAY_SLOTS = {
    "energy": {
        "items": [
            [
                {"itemCode": "TCT_CHG1", "itemCodeSignal": "CHG_PERIOD_1", "itemType": "82", "structure": [{"itemCode": "charge_mode"}, {"itemCode": "chg_pwr_lmt"}, {"itemCode": "start_time"}, {"itemCode": "end_time"}]},
                {"itemCodeSignal": "FLAG_ENABLE_CYCLE", "itemType": "4"},
            ]
        ]
    }
}


def ready_client(**kwargs):
    """Return a MockHanchu past login with one device, ready to poll."""
    client = MockHanchu(**kwargs)
    client._token = "tok"
    client._token_time = time.time()
    client.device_list = ["HC240100001"]
    return client


def test_hanchu_menu_slot_model_is_recognised():
    """The menu says which slot format a device uses, and which work modes run the TCT_/TDT_ slots."""
    failed = False
    client = ready_client()
    with patched_session([(200, {"code": 200, "data": MENU_SLOTS}), (200, envelope(data={}))]):
        run_async_local(client.refresh_config())
    if client.slot_model.get("HC240100001") != "slots" or client.slot_work_modes.get("HC240100001") != [3]:
        print(f"ERROR: slot model {client.slot_model} modes {client.slot_work_modes}, expected slots / [3]")
        failed = True
    if client.slot_work_mode("HC240100001") != 3:
        print(f"ERROR: slot work mode {client.slot_work_mode('HC240100001')}, expected 3")
        failed = True

    array = ready_client()
    with patched_session([(200, {"code": 200, "data": MENU_ARRAY_SLOTS}), (200, envelope(data={}))]):
        run_async_local(array.refresh_config())
    if array.slot_model.get("HC240100001") != "array":
        print(f"ERROR: an array-slot menu read as {array.slot_model}")
        failed = True

    # A menu showing neither format is unknown, not unsupported: control stays allowed.
    unknown = ready_client()
    with patched_session([(200, {"code": 200, "data": MENU_SAMPLE["data"]}), (200, envelope(data={}))]):
        run_async_local(unknown.refresh_config())
    if unknown.slot_model.get("HC240100001") not in (None, "unknown"):
        print(f"ERROR: a menu with no slot items read as {unknown.slot_model}")
        failed = True
    assert not failed, "test_hanchu_menu_slot_model_is_recognised"


def test_hanchu_slot_work_mode_follows_the_menu():
    """User-defined (3) is written where the menu allows it or says nothing; otherwise the menu's own mode is, and that is logged."""
    failed = False
    client = ready_client()
    if client.slot_work_mode("HC240100001") != 3:
        print(f"ERROR: with no menu the slot work mode is {client.slot_work_mode('HC240100001')}, expected 3")
        failed = True
    client.slot_work_modes["HC240100001"] = [3, 4]
    if client.slot_work_mode("HC240100001") != 3:
        print("ERROR: 3 is listed, so it should be kept")
        failed = True
    client.slot_work_modes["HC240100001"] = [4]
    if client.slot_work_mode("HC240100001") != 4 or not logged(client, "work mode 4"):
        print(f"ERROR: the menu only runs the slots in mode 4, got {client.slot_work_mode('HC240100001')}; logs {client.log_messages[-2:]}")
        failed = True
    payload = client.build_write_payload("HC240100001", {"reserve": 10, "charge": {"enable": True, "soc": 80, "power": 2000, "start": "01:00:00", "end": "02:00:00"}, "export": {"enable": False}})
    if payload.get(HANCHU_KEY_WORK_MODE) != 4:
        print(f"ERROR: payload work mode {payload.get(HANCHU_KEY_WORK_MODE)}, expected the menu's 4")
        failed = True
    assert not failed, "test_hanchu_slot_work_mode_follows_the_menu"


def test_hanchu_array_slot_device_is_not_controlled():
    """A device on the array slot format would accept TCT_ writes and ignore them, so nothing is written - and the user is told once."""
    failed = False
    client = ready_client()
    client.slot_model["HC240100001"] = "array"
    client.local_schedule["HC240100001"] = {"reserve": 10, "charge": {"enable": True, "soc": 80, "power": 2000, "start": "01:00:00", "end": "02:00:00"}, "export": {"enable": False}}
    with patched_session([(200, envelope(data={}))]) as session_cls:
        first = run_async_local(client.apply_schedule("HC240100001"))
        second = run_async_local(client.apply_schedule("HC240100001"))
    if first or second or session_cls.return_value.post.call_count:
        print(f"ERROR: an array-slot device was written to: {first} {second} calls={session_cls.return_value.post.call_count}")
        failed = True
    warnings = [message for message in client.log_messages if "different time-slot format" in message]
    if len(warnings) != 1:
        print(f"ERROR: expected one warning, got {warnings}")
        failed = True
    if "time-slot format" not in (client.health_message() or ""):
        print(f"ERROR: health message {client.health_message()!r} should name the unsupported slot format")
        failed = True
    assert not failed, "test_hanchu_array_slot_device_is_not_controlled"


def test_hanchu_offline_device_is_reported():
    """devStatus 0 means the datalogger is offline and the readings are stale - say so, once per outage."""
    failed = False
    client = ready_client()
    client._apply_status_payload("HC240100001", dict(STATUS_SAMPLE, devStatus=1))
    if client.device_online.get("HC240100001") is not True or client.health_message():
        print(f"ERROR: online device: {client.device_online} health {client.health_message()!r}")
        failed = True
    for _ in range(2):
        client._apply_status_payload("HC240100001", dict(STATUS_SAMPLE, devStatus=0))
    if client.device_online.get("HC240100001") is not False or "offline" not in (client.health_message() or ""):
        print(f"ERROR: offline device: {client.device_online} health {client.health_message()!r}")
        failed = True
    if len([message for message in client.log_messages if "is offline" in message]) != 1:
        print(f"ERROR: the outage should be logged once: {client.log_messages}")
        failed = True
    client._apply_status_payload("HC240100001", dict(STATUS_SAMPLE, devStatus=1))
    if client.health_message() or not logged(client, "is back online"):
        print(f"ERROR: recovery not reported: health {client.health_message()!r}")
        failed = True
    # 99 is pending, and no devStatus at all is unknown: neither is called offline.
    client._apply_status_payload("HC240100001", dict(STATUS_SAMPLE, devStatus=99))
    if client.health_message():
        print(f"ERROR: pending read as offline: {client.health_message()!r}")
        failed = True
    assert not failed, "test_hanchu_offline_device_is_reported"


def test_hanchu_http_session_is_reused_and_closed():
    """One open session serves every call (upton68 2.0.2: far fewer timeouts than a session per call), and final() closes it."""
    failed = False
    client = ready_client()
    session = create_aiohttp_mock_session([mock_json_response(json_data=envelope(data=STATUS_SAMPLE)), mock_json_response(json_data=envelope(data=STATISTICS_SAMPLE))])
    session.closed = False
    session.close = AsyncMock()
    with patch("aiohttp.ClientSession", return_value=session) as session_cls:
        run_async_local(client.refresh_power())
        run_async_local(client.refresh_energy())
        if session_cls.call_count != 1 or session.post.call_count != 2:
            print(f"ERROR: {session_cls.call_count} sessions for {session.post.call_count} calls, expected 1 for 2")
            failed = True
        run_async_local(client.final())
    if not session.close.await_count:
        print("ERROR: final() did not close the session")
        failed = True
    assert not failed, "test_hanchu_http_session_is_reused_and_closed"


def test_hanchu_generator_fields_are_read():
    """The generator's live power and daily energy are read and published, so a live test can show what they mean."""
    failed = False
    client = ready_client()
    client._apply_status_payload("HC240100001", dict(STATUS_SAMPLE, hasDg=True, dgPAcTotal=1.2, dgPAcTotalUnit="kW"))
    if client.device_values["HC240100001"].get("generator_power") != 1200.0 or client.device_detail["HC240100001"].get("has_generator") is not True:
        print(f"ERROR: generator status {client.device_values['HC240100001']} {client.device_detail['HC240100001']}")
        failed = True
    with patched_session([(200, envelope(data=dict(STATISTICS_SAMPLE, dgEp=3.4)))]):
        run_async_local(client.refresh_energy())
    if client.device_energy["HC240100001"].get("generator_today") != 3.4:
        print(f"ERROR: generator energy {client.device_energy['HC240100001']}")
        failed = True
    run_async_local(client.publish_data())
    for leaf in ("generator_power", "generator_today"):
        if client._sensor_name("HC240100001", leaf) not in client.published:
            print(f"ERROR: {leaf} was not published")
            failed = True
    assert not failed, "test_hanchu_generator_fields_are_read"


def test_hanchu_power_limit_follows_a_higher_value_the_device_holds():
    """The menu's 0-5000 W power range is shared by 2-10 kW models, so a higher value the device already holds wins.

    Hardware (batpred#5305): a HESS-HY-S-6.0K, nameplate 6000 W grid output, holds DSCHG_PWR_LMT 6000
    while its menu says 0-5000. Taken at the menu's word, inverter_limit came out at 5000 and every
    discharge would be capped below what the inverter does.
    """
    failed = False
    client = ready_client()
    sn = "HC240100001"
    client.device_ranges[sn] = {"CHG_PWR_LMT": (0, 5000), "DSCHG_PWR_LMT": (0, 5000)}
    client.device_settings[sn] = {"CHG_PWR_LMT": "5000", "DSCHG_PWR_LMT": "6000", "DSCHG_BAT_SOC_LMT": "10.00"}
    ranges = client.ranges_for(sn)
    if ranges.get("DSCHG_PWR_LMT") != (0, 6000) or ranges.get("CHG_PWR_LMT") != (0, 5000):
        print(f"ERROR: power ranges {ranges.get('CHG_PWR_LMT')} / {ranges.get('DSCHG_PWR_LMT')}, expected 0-5000 / 0-6000")
        failed = True
    if client.inverter_limit(sn) != 6000.0 or client.battery_rate_max(sn) != 5000.0:
        print(f"ERROR: inverter_limit {client.inverter_limit(sn)} battery_rate_max {client.battery_rate_max(sn)}, expected 6000 / 5000")
        failed = True
    client.ranges_for(sn)
    raised = [message for message in client.log_messages if "above the menu" in message]
    if len(raised) != 1 or "6000" not in raised[0]:
        print(f"ERROR: the raise should be logged once, naming 6000: {raised}")
        failed = True
    # A held value below the menu's maximum leaves the menu's range alone.
    client.device_settings[sn]["DSCHG_PWR_LMT"] = "3000"
    if client.ranges_for(sn).get("DSCHG_PWR_LMT") != (0, 5000):
        print(f"ERROR: a lower held value changed the range: {client.ranges_for(sn).get('DSCHG_PWR_LMT')}")
        failed = True
    # Each published power control advertises its own direction's maximum.
    client.device_settings[sn]["DSCHG_PWR_LMT"] = "6000"
    run_async_local(client.publish_schedule_settings_ha(sn))
    charge = client.published.get(client._control_name("number", sn, "battery_schedule_charge_power"), {}).get("attributes", {})
    export = client.published.get(client._control_name("number", sn, "battery_schedule_export_power"), {}).get("attributes", {})
    if charge.get("max") != 5000 or export.get("max") != 6000:
        print(f"ERROR: power controls advertise charge max {charge.get('max')} export max {export.get('max')}, expected 5000 / 6000")
        failed = True
    assert not failed, "test_hanchu_power_limit_follows_a_higher_value_the_device_holds"


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
        ("inverter_limit_override", test_hanchu_inverter_limit_override_wins),
        ("default_power_warning", test_hanchu_default_power_limit_is_warned_once),
        ("config_batched_read", test_hanchu_config_read_is_one_call_per_device),
        ("menu_code_alone", test_hanchu_menu_answered_by_code_alone_is_used),
        ("code_alone_menu_only", test_hanchu_code_alone_is_success_only_for_the_menu),
        ("failed_menu_warning", test_hanchu_failed_menu_warns_the_default_power_is_in_use),
        ("ac_coupled_pv", test_hanchu_ac_coupled_pv_is_counted_in_pv_power),
        ("slot_model", test_hanchu_menu_slot_model_is_recognised),
        ("slot_work_mode", test_hanchu_slot_work_mode_follows_the_menu),
        ("array_slots_blocked", test_hanchu_array_slot_device_is_not_controlled),
        ("offline", test_hanchu_offline_device_is_reported),
        ("session_reuse", test_hanchu_http_session_is_reused_and_closed),
        ("generator", test_hanchu_generator_fields_are_read),
        ("held_power_limit", test_hanchu_power_limit_follows_a_higher_value_the_device_holds),
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

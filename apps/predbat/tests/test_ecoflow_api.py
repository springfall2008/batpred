# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test EcoFlow API component
# -----------------------------------------------------------------------------

"""Tests for the EcoFlow cloud component's request, discovery and telemetry paths (ecoflow.py)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from ecoflow import EcoFlowAPI
from ecoflow_const import ECOFLOW_SIGN_VECTOR, sign_request
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockEcoFlow(EcoFlowAPI):
    """Test double: build an EcoFlowAPI without the full component lifecycle."""

    def __init__(self, access_key="Fp4SvIprYSDPXtYJidEtUAd1o", secret_key="WIbFEKre0s6sLnh4ei7SPUeYnptHG6V", device_sn=None, control_enable=False, automatic=False, key_map=None, battery_capacity=None, inverter_limit=None):  # cspell:disable-line
        """Set up a minimal EcoFlowAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-ecoflow-1"}
        # A real clock, not a MagicMock attribute: ComponentBase.minutes_now derives from
        # base.now_utc, so the mock base has to carry a coherent now_utc/midnight_utc pair.
        self.base.now_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.midnight_utc = self.base.now_utc
        self.base.minutes_now = 0
        self.state = {}
        self.published = {}
        self.reports = []
        # 0 rather than initialize()'s real 2s default: api_delay is a courtesy pause between
        # consecutive cloud calls, and nothing here talks to the real cloud - every request is
        # a scripted mock. Left at 2 it is several seconds of dead wall-clock per test.
        self.initialize(
            access_key=access_key,
            secret_key=secret_key,
            device_sn=device_sn,
            automatic=automatic,
            control_enable=control_enable,
            key_map=key_map,
            battery_capacity=battery_capacity,
            inverter_limit=inverter_limit,
            api_delay=0,
        )

    def log(self, message):
        """Record a log line instead of writing it out."""
        self.log_messages.append(message)

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity instead of pushing it to Home Assistant."""
        self.published[entity] = {"state": state, "attributes": attributes}

    def get_state_wrapper(self, entity_id=None, default=None, attribute=None, refresh=False, required_unit=None, raw=False):
        """Read from the in-memory state dict."""
        return self.state.get(entity_id, default)

    def set_arg_auto(self, arg, value, overwrite=True):
        """Record an auto-configured arg."""
        self.base.args[arg] = value

    def update_success_timestamp(self):
        """No-op success stamp."""
        return None

    def refresh_discovery(self):
        """Record the built discovery report instead of filing it."""
        self.reports.append(self.build_discovery())

    @property
    def storage(self):
        """No Storage component in tests - load/save become no-ops."""
        return None


def _session_with_put(mock_response=None, exception=None):
    """A mock aiohttp session whose put() behaves like its post().

    create_aiohttp_mock_session() in test_infra only wires get and post, because no component
    before this one used PUT. Aliasing keeps the shared helper untouched: without the alias, put()
    returns a bare MagicMock and `async with` on it raises, which would look like a bug in the
    write path rather than a gap in the test double.
    """
    session = create_aiohttp_mock_session(mock_response, exception=exception)
    session.put = session.post
    return session


def _envelope(data, code="0", message="Success"):
    """Build an EcoFlow REST envelope, whose success code is the STRING "0"."""
    return {"code": code, "message": message, "data": data}


def _device_list(serials=("HW51ZOH4EF2A0001",), product="PowerOcean"):
    """Build a /device/list response body."""
    return _envelope([{"sn": sn, "deviceName": "Home", "productName": product, "online": 1} for sn in serials])


def _quota(soc=57, battery=-1200, grid=800, pv=2400, load=1900):
    """Build a /quota/all response body using the candidate (unverified) key names."""
    return _envelope({"bpSoc": soc, "bpPwr": battery, "sysGridPwr": grid, "mpptPwr": pv, "sysLoadPwr": load})


def test_ecoflow_request_signs_the_body_it_sends():
    """The signature covers the exact params sent, and the headers carry the documented set.

    Signing one dict and sending another is a failure that looks exactly like a bad secret key,
    so the sent body and the signed string are checked to be the same thing - the signature is
    recomputed from the captured body and the captured nonce/timestamp and must match.
    """
    failed = False
    client = MockEcoFlow()
    response = create_aiohttp_mock_response(json_data=_envelope({"bpSoc": 50}))
    session = _session_with_put(response)
    with patch("aiohttp.ClientSession", return_value=session):
        ok, data = run_async_local(client._request("quota_set", params={"sn": "SN1", "params": {"cmdSet": 11, "id": 24}}))
    if not ok:
        print("ERROR: request should have succeeded")
        failed = True
    # quota_set is a PUT - a POST here would be the read path, not the write path.
    if not session.put.called:
        print(f"ERROR: quota_set must use PUT, calls were get={session.get.called} post={session.post.called} put={session.put.called}")
        failed = True
        assert not failed, "test_ecoflow_request_signs_the_body_it_sends"
    kwargs = session.put.call_args.kwargs
    headers = kwargs["headers"]
    for header in ("accessKey", "nonce", "timestamp", "sign"):
        if header not in headers:
            print(f"ERROR: header {header} missing")
            failed = True
    if len(headers.get("nonce", "")) != 6 or not headers.get("nonce", "").isdigit():
        print(f"ERROR: nonce {headers.get('nonce')} is not six digits")
        failed = True
    expected = sign_request(kwargs["json"], client.access_key, client.secret_key, headers["nonce"], headers["timestamp"])
    if headers["sign"] != expected:
        print("ERROR: the signature does not cover the body that was sent")
        failed = True
    if data != {"bpSoc": 50}:
        print(f"ERROR: data {data}")
        failed = True
    assert not failed, "test_ecoflow_request_signs_the_body_it_sends"


def test_ecoflow_get_sends_flattened_query_params():
    """A GET's query string carries the same flattened keys the signature was taken over.

    The server rebuilds the signed string from what it received, so a nested structure aiohttp
    rendered some other way would verify against nothing.
    """
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_device_list()))
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client._request("quota_all", params={"sn": "SN1"}))
    if not session.get.called:
        print("ERROR: quota_all must use GET")
        failed = True
        assert not failed, "test_ecoflow_get_sends_flattened_query_params"
    if session.get.call_args.kwargs.get("params") != {"sn": "SN1"}:
        print(f"ERROR: query params {session.get.call_args.kwargs.get('params')}")
        failed = True
    assert not failed, "test_ecoflow_get_sends_flattened_query_params"


def test_ecoflow_accepts_string_and_integer_success_codes():
    """The envelope's success code is accepted as "0" and as 0.

    The REST examples return a string and the MQTT set_reply an integer; a component comparing
    against only one of them reads every success of the other kind as a failure.
    """
    failed = False
    client = MockEcoFlow()
    for code in ("0", 0):
        session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope({"x": 1}, code=code)))
        with patch("aiohttp.ClientSession", return_value=session):
            ok, _ = run_async_local(client._request("quota_all", params={"sn": "SN1"}))
        if not ok:
            print(f"ERROR: code {code!r} should be success")
            failed = True
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope(None, code="-1", message="device not bound")))
    with patch("aiohttp.ClientSession", return_value=session):
        ok, _ = run_async_local(client._request("quota_all", params={"sn": "SN1"}))
    if ok:
        print("ERROR: -1 should be a failure")
        failed = True
    # -1 is the single most likely BYOK setup mistake, so it must be named as one rather than
    # logged as a generic error.
    if not any("does not belong to this account" in line for line in client.log_messages):
        print(f"ERROR: -1 was not explained as an account mismatch: {client.log_messages[-1:]}")
        failed = True
    assert not failed, "test_ecoflow_accepts_string_and_integer_success_codes"


def test_ecoflow_offline_is_not_a_warning():
    """A -2 offline reply is routine and transient, not a component fault."""
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope(None, code="-2", message="offline")))
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client._request("quota_all", params={"sn": "SN1"}))
    offline = [line for line in client.log_messages if "offline" in line]
    if not offline:
        print("ERROR: no offline log line")
        failed = True
    elif any(line.startswith("Warn:") for line in offline):
        print(f"ERROR: offline should not be a Warn: {offline}")
        failed = True
    assert not failed, "test_ecoflow_offline_is_not_a_warning"


def test_ecoflow_http_error_and_transport_failure():
    """A non-200 and a transport exception both fail closed without raising."""
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=500))
    with patch("aiohttp.ClientSession", return_value=session):
        ok, _ = run_async_local(client._request("quota_all", params={"sn": "SN1"}))
    if ok:
        print("ERROR: HTTP 500 should fail")
        failed = True
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(exception=Exception("connection reset"))
    with patch("aiohttp.ClientSession", return_value=session), patch("asyncio.sleep", return_value=None):
        ok, _ = run_async_local(client._request("quota_all", params={"sn": "SN1"}))
    if ok:
        print("ERROR: a transport failure should fail")
        failed = True
    assert not failed, "test_ecoflow_http_error_and_transport_failure"


def test_ecoflow_discovery_filters_and_records():
    """Discovery keeps the serials, honours the filter, and distinguishes empty from failed."""
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_device_list(("SN1", "SN2"))))
    with patch("aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["SN1", "SN2"] or client.discovery_ok is not True:
        print(f"ERROR: serials {serials} discovery_ok {client.discovery_ok}")
        failed = True

    client = MockEcoFlow(device_sn="sn2")
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_device_list(("SN1", "SN2"))))
    with patch("aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["SN2"]:
        print(f"ERROR: filter gave {serials}")
        failed = True

    # A failed call and an empty account are indistinguishable from the list alone, so
    # discovery_ok has to separate them.
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=500))
    with patch("aiohttp.ClientSession", return_value=session):
        run_async_local(client.get_device_list())
    if client.discovery_ok is not False:
        print(f"ERROR: failed discovery gave discovery_ok {client.discovery_ok}")
        failed = True
    assert not failed, "test_ecoflow_discovery_filters_and_records"


def test_ecoflow_unrecognised_product_is_still_polled():
    """A device whose product name is not a known home battery is logged, never dropped.

    A name table that filtered would stop working the day EcoFlow ship a new home battery, so
    the quota poll decides instead - a device with no resolvable telemetry publishes nothing.
    """
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_device_list(("SN1",), product="DELTA Pro 3")))
    with patch("aiohttp.ClientSession", return_value=session):
        serials = run_async_local(client.get_device_list())
    if serials != ["SN1"]:
        print(f"ERROR: an unrecognised product must still be kept, got {serials}")
        failed = True
    if not any("not a recognised home-battery line" in line for line in client.log_messages):
        print("ERROR: no log line explaining the unrecognised product")
        failed = True
    assert not failed, "test_ecoflow_unrecognised_product_is_still_polled"


def test_ecoflow_refresh_static_keeps_a_working_list_on_failure():
    """A transient discovery failure must not wipe a working device list.

    Absence of a result is not a result: assigning the empty list would also write an empty
    cache and stamp the tier fresh, so a restart would restore nothing and skip re-discovery
    for a full TTL.
    """
    failed = False
    client = MockEcoFlow()
    client.device_list = ["SN1"]
    client.device_detail = {"SN1": {"sn": "SN1"}}
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=500))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.refresh_static())
    if ok:
        print("ERROR: refresh_static should report failure")
        failed = True
    if client.device_list != ["SN1"]:
        print(f"ERROR: device_list was clobbered: {client.device_list}")
        failed = True
    if client.tier_expired("static", 480) is not True:
        print("ERROR: a failed refresh must not advance the tier clock")
        failed = True
    assert not failed, "test_ecoflow_refresh_static_keeps_a_working_list_on_failure"


def test_ecoflow_fetch_quota_resolves_and_records_keys():
    """A quota poll resolves telemetry and records the key list for the diagnostic sensor."""
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_quota()))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.fetch_quota("SN1"))
    if not ok:
        print("ERROR: fetch_quota should have resolved telemetry")
        failed = True
    values = client.device_values.get("SN1", {})
    for leaf, expected in (("soc", 57.0), ("battery_power", -1200.0), ("grid_power", 800.0), ("pv_power", 2400.0), ("load_power", 1900.0)):
        if values.get(leaf) != expected:
            print(f"ERROR: {leaf} = {values.get(leaf)} != {expected}")
            failed = True
    if client.device_quota_keys.get("SN1") != ["bpPwr", "bpSoc", "mpptPwr", "sysGridPwr", "sysLoadPwr"]:
        print(f"ERROR: quota keys {client.device_quota_keys.get('SN1')}")
        failed = True
    assert not failed, "test_ecoflow_fetch_quota_resolves_and_records_keys"


def test_ecoflow_unmatched_quota_names_the_fix():
    """A quota that matches nothing fails the poll and tells the user exactly what to do.

    This is the expected outcome on real hardware if the candidate key names are wrong, which
    nobody has been able to check - so the log line has to name the diagnostic sensor and
    ecoflow_key_map rather than merely report a miss.
    """
    failed = False
    client = MockEcoFlow()
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope({"someOtherName": 1, "andAnother": 2})))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.fetch_quota("SN1"))
    if ok:
        print("ERROR: a quota matching nothing is not a usable poll")
        failed = True
    if client.device_values.get("SN1"):
        print("ERROR: no telemetry should have resolved")
        failed = True
    warning = " ".join(line for line in client.log_messages if line.startswith("Warn:"))
    for needle in ("quota_keys", "ecoflow_key_map", "unverified"):
        if needle not in warning:
            print(f"ERROR: the warning does not mention {needle}: {warning}")
            failed = True
    # The keys are still recorded, because they are the evidence the fix needs.
    if client.device_quota_keys.get("SN1") != ["andAnother", "someOtherName"]:
        print(f"ERROR: keys not recorded: {client.device_quota_keys.get('SN1')}")
        failed = True
    assert not failed, "test_ecoflow_unmatched_quota_names_the_fix"


def test_ecoflow_key_map_rescues_unknown_field_names():
    """A user's ecoflow_key_map makes a device with entirely different field names work."""
    failed = False
    client = MockEcoFlow(key_map={"soc": "myStateOfCharge", "load_power": "housePower"})
    session = create_aiohttp_mock_session(create_aiohttp_mock_response(json_data=_envelope({"myStateOfCharge": 33, "housePower": 700})))
    with patch("aiohttp.ClientSession", return_value=session):
        ok = run_async_local(client.fetch_quota("SN1"))
    if not ok:
        print("ERROR: the key map should have rescued the poll")
        failed = True
    if client.device_values.get("SN1") != {"soc": 33.0, "load_power": 700.0}:
        print(f"ERROR: values {client.device_values.get('SN1')}")
        failed = True
    assert not failed, "test_ecoflow_key_map_rescues_unknown_field_names"


def test_ecoflow_run_defers_startup_without_telemetry():
    """run() returns False on a first cycle with no usable telemetry, so startup is retried.

    automatic_config() runs on the first cycle ALONE, so a cycle that proceeded with no
    telemetry would bind nothing and permanently skip every arg for the whole session.
    """
    failed = False
    client = MockEcoFlow(automatic=True)
    responses = [create_aiohttp_mock_response(json_data=_device_list(("SN1",))), create_aiohttp_mock_response(json_data=_envelope({"nothingUseful": 1}))]
    session = create_aiohttp_mock_session(responses)
    with patch("aiohttp.ClientSession", return_value=session):
        result = run_async_local(client.run(0, True))
    if result is not False:
        print(f"ERROR: run returned {result}, expected False")
        failed = True
    if "inverter_type" in client.base.args:
        print("ERROR: automatic_config must not have run")
        failed = True
    assert not failed, "test_ecoflow_run_defers_startup_without_telemetry"


def test_ecoflow_run_without_credentials_is_explicit():
    """Missing credentials fail the cycle with a message that says where the keys come from.

    BYOK is the unusual part of this integration, so the message has to say the keys belong to
    the account the battery is registered to - a shared device is the trap.
    """
    failed = False
    client = MockEcoFlow(access_key="", secret_key="")
    result = run_async_local(client.run(0, True))
    if result is not False:
        print(f"ERROR: run returned {result}, expected False")
        failed = True
    if not any("ecoflow_access_key" in line and "account" in line for line in client.log_messages):
        print(f"ERROR: unhelpful credential message: {client.log_messages}")
        failed = True
    assert not failed, "test_ecoflow_run_without_credentials_is_explicit"


def test_ecoflow_run_completes_and_files_a_report():
    """A good cycle publishes, files a discovery report and returns True.

    Explicitly True rather than a falling-through None: ComponentBase only clears its `first`
    flag on a truthy return, so a None would strand the component in the startup backoff.
    """
    failed = False
    client = MockEcoFlow(automatic=True, battery_capacity=10, inverter_limit=5000)
    responses = [create_aiohttp_mock_response(json_data=_device_list(("SN1",))), create_aiohttp_mock_response(json_data=_quota())]
    session = create_aiohttp_mock_session(responses)
    with patch("aiohttp.ClientSession", return_value=session):
        result = run_async_local(client.run(0, True))
    if result is not True:
        print(f"ERROR: run returned {result!r}, expected True")
        failed = True
    if "sensor.predbat_ecoflow_sn1_soc" not in client.published:
        print(f"ERROR: soc sensor not published: {sorted(client.published)}")
        failed = True
    if not client.reports or not client.reports[-1]:
        print("ERROR: no discovery report filed")
        failed = True
    elif client.reports[-1]["inverters"][0]["inverter_type"] != "EcoFlowCloud":
        print(f"ERROR: report {client.reports[-1]}")
        failed = True
    assert not failed, "test_ecoflow_run_completes_and_files_a_report"


def test_ecoflow_sign_vector_constant_is_what_the_component_uses():
    """The component's signer and the constants module's vector are the same implementation.

    Guards against a second, divergent signing implementation being added inside ecoflow.py -
    the headers must be produced by the same function the documented vector proves.
    """
    failed = False
    client = MockEcoFlow(access_key=ECOFLOW_SIGN_VECTOR["access_key"], secret_key=ECOFLOW_SIGN_VECTOR["secret_key"])
    with patch.object(EcoFlowAPI, "_nonce", return_value=ECOFLOW_SIGN_VECTOR["nonce"]), patch.object(EcoFlowAPI, "_timestamp", return_value=ECOFLOW_SIGN_VECTOR["timestamp"]):
        headers = client._auth_headers(ECOFLOW_SIGN_VECTOR["params"])
    if headers["sign"] != ECOFLOW_SIGN_VECTOR["sign"]:
        print(f"ERROR: component headers signed {headers['sign']}, documented {ECOFLOW_SIGN_VECTOR['sign']}")
        failed = True
    if headers["accessKey"] != ECOFLOW_SIGN_VECTOR["access_key"]:
        print(f"ERROR: accessKey {headers['accessKey']}")
        failed = True
    assert not failed, "test_ecoflow_sign_vector_constant_is_what_the_component_uses"


def run_ecoflow_api_tests(my_predbat):
    """Run all EcoFlow API tests."""
    failed = False
    for name, fn in [
        ("request_signs_body", test_ecoflow_request_signs_the_body_it_sends),
        ("get_query_params", test_ecoflow_get_sends_flattened_query_params),
        ("success_codes", test_ecoflow_accepts_string_and_integer_success_codes),
        ("offline_not_warn", test_ecoflow_offline_is_not_a_warning),
        ("transport_failures", test_ecoflow_http_error_and_transport_failure),
        ("discovery", test_ecoflow_discovery_filters_and_records),
        ("unrecognised_product", test_ecoflow_unrecognised_product_is_still_polled),
        ("refresh_static_failure", test_ecoflow_refresh_static_keeps_a_working_list_on_failure),
        ("fetch_quota", test_ecoflow_fetch_quota_resolves_and_records_keys),
        ("unmatched_quota", test_ecoflow_unmatched_quota_names_the_fix),
        ("key_map_rescue", test_ecoflow_key_map_rescues_unknown_field_names),
        ("run_defers_startup", test_ecoflow_run_defers_startup_without_telemetry),
        ("run_no_credentials", test_ecoflow_run_without_credentials_is_explicit),
        ("run_completes", test_ecoflow_run_completes_and_files_a_report),
        ("component_uses_documented_signer", test_ecoflow_sign_vector_constant_is_what_the_component_uses),
    ]:
        try:
            if fn():
                print(f"  FAILED: ecoflow_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in ecoflow_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

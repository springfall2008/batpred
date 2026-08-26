import asyncio
import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from deye import DeyeAPI


def test_deyecloud_inverter_def():
    """DeyeCloud is a mode-less inverter with freeze support."""
    failed = False
    d = INVERTER_DEF.get("DeyeCloud")
    if d is None:
        print("ERROR: DeyeCloud INVERTER_DEF missing")
        failed = True
    else:
        expect = {
            "has_ge_inverter_mode": False,
            "has_fox_inverter_mode": False,
            "has_ge_eco_toggle": False,
            "has_charge_enable_time": True,
            "has_discharge_enable_time": True,
            "has_target_soc": True,
            "has_reserve_soc": True,
            "support_charge_freeze": True,
            "support_discharge_freeze": True,
            "target_soc_used_for_discharge": True,
            # Load-bearing, not cosmetic. inverter.py replaces charge/discharge
            # start/end_time with its own dummy sensor.predbat_<type>_<id>_* entities for
            # ANY format other than "HH:MM:SS", discarding whatever automatic_config
            # mapped. DEYE was the only inverter declaring "HH:MM", a value handled
            # nowhere else in inverter.py, so Predbat wrote the window times to dummies
            # while this component read its own untouched selects — every control payload
            # went out with no charge or export window at all.
            "charge_time_format": "HH:MM:SS",
        }
        for k, v in expect.items():
            if d.get(k) != v:
                print(f"ERROR: DeyeCloud[{k}] expected {v} got {d.get(k)}")
                failed = True
    for key in ("deye_app_id", "deye_auth_method", "deye_inverter_sn", "deye_data_center", "deye_key", "deye_battery_nominal_voltage"):
        if key not in APPS_SCHEMA:
            print(f"ERROR: APPS_SCHEMA missing {key}")
            failed = True
    assert not failed, "test_deyecloud_inverter_def"


def test_initialize_preserves_configured_token_hash():
    """A configured token_hash must survive _init_oauth()'s internal reset to "" (Predbat.com SaaS dedup is keyed on it)."""
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.initialize(app_id="id", app_secret="sec", username="user@example.com", password="pw", auth_method="app_credentials", token_hash="configured-hash")
    if d.token_hash != "configured-hash":
        print(f"ERROR: token_hash expected 'configured-hash' got {d.token_hash!r}")
        failed = True
    assert not failed, "test_initialize_preserves_configured_token_hash"


def _bare_deye():
    """Build a DeyeAPI without the component lifecycle, capturing its log."""
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    return d


def test_injected_key_becomes_the_access_token():
    """The injected access token must reach access_token, NOT the dedup hash.

    Regression: initialize() passed key=app_secret or token_hash into _init_oauth(), which
    assigns key straight to access_token in oauth mode. With no app_secret in SaaS mode
    that handed DEYE the refresh dedup HASH as its bearer token, so every call came back
    "auth invalid token".
    """
    failed = False
    d = _bare_deye()
    d.initialize(key="real-access-token", token_hash="dedup-hash", auth_method="oauth")
    if d.access_token != "real-access-token":
        print(f"ERROR: access_token should be the injected key, got {d.access_token!r}")
        failed = True
    if d.token_hash != "dedup-hash":
        print(f"ERROR: token_hash should still be preserved, got {d.token_hash!r}")
        failed = True
    assert not failed, "test_injected_key_becomes_the_access_token"


def test_auth_method_inferred_when_key_supplied_without_app_id():
    """An injected token with no app credentials can only be oauth, whatever the default says.

    auth_method defaults to app_credentials, and _init_oauth() discards the key in that
    mode, so a host that omits deye_auth_method would authenticate with nothing.
    """
    failed = False
    d = _bare_deye()
    d.initialize(key="real-access-token")  # auth_method left at its app_credentials default
    if d.auth_method != "oauth":
        print(f"ERROR: expected auth_method to be inferred as oauth, got {d.auth_method!r}")
        failed = True
    if d.access_token != "real-access-token":
        print(f"ERROR: access_token should survive the inference, got {d.access_token!r}")
        failed = True
    # App credentials must NOT be re-routed to oauth
    d2 = _bare_deye()
    d2.initialize(app_id="id", app_secret="sec", username="u", password="p")
    if d2.auth_method != "app_credentials":
        print(f"ERROR: app credentials should stay app_credentials, got {d2.auth_method!r}")
        failed = True
    assert not failed, "test_auth_method_inferred_when_key_supplied_without_app_id"


def test_fetch_token_fails_soft_when_credentials_missing():
    """A misconfigured app_credentials instance returns False, it does not raise.

    Every app_credentials arg is optional, so _sha256(None) would raise AttributeError out
    of run() on every 5-minute cycle and bury the real problem in a traceback.
    """
    failed = False
    d = _bare_deye()
    d.initialize(auth_method="app_credentials")
    try:
        result = asyncio.run(d.fetch_token())
    except Exception as e:
        print(f"ERROR: fetch_token should fail soft, raised {type(e).__name__}: {e}")
        return True
    if result is not False:
        print(f"ERROR: expected False, got {result!r}")
        failed = True
    if not any("missing" in m for m in d.log_messages):
        print(f"ERROR: expected a warning naming the missing args, got {d.log_messages}")
        failed = True
    assert not failed, "test_fetch_token_fails_soft_when_credentials_missing"


def test_deye_component_gated_by_required_or():
    """DEYE must only activate when an auth path is configured (app_id OR key).

    All individual args are optional (to allow either auth mode), so without a
    required_or gate the component would start for every Predbat instance.
    """
    failed = False
    info = COMPONENT_LIST.get("deye", {})
    required_or = info.get("required_or")
    if not required_or:
        print("ERROR: deye component has no required_or gate — it would activate for every instance")
        return True
    # Gate on key, not token_hash: the hash is only a refresh dedup handle, so gating on it
    # starts the component for an instance that has no usable token.
    if set(required_or) != {"app_id", "key"}:
        print(f"ERROR: deye required_or should gate on app_id/key, got {required_or}")
        failed = True
    # Every individual arg must stay optional (the required_or is the only activation gate).
    for arg, spec in info.get("args", {}).items():
        if spec.get("required"):
            print(f"ERROR: deye arg {arg} is required=True; activation must come from required_or, not a single arg")
            failed = True
    assert not failed, "test_deye_component_gated_by_required_or"


def run_deye_config_tests(my_predbat):
    """Run all DEYE config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("inverter_def", test_deyecloud_inverter_def),
        ("initialize_token_hash_order", test_initialize_preserves_configured_token_hash),
        ("injected_key_is_access_token", test_injected_key_becomes_the_access_token),
        ("auth_method_inferred", test_auth_method_inferred_when_key_supplied_without_app_id),
        ("fetch_token_soft_fail", test_fetch_token_fails_soft_when_credentials_missing),
        ("required_or_gate", test_deye_component_gated_by_required_or),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_config.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_config.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

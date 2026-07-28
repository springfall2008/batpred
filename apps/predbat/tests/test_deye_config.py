import asyncio

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA
from components import COMPONENT_LIST
from deye import DeyeAPI
from deye_const import DEYE_TELEMETRY_KEYS, DEYE_ENERGY_KEYS


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
        }
        for k, v in expect.items():
            if d.get(k) != v:
                print(f"ERROR: DeyeCloud[{k}] expected {v} got {d.get(k)}")
                failed = True
    for key in ("deye_app_id", "deye_auth_method", "deye_inverter_sn", "deye_data_center"):
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


def test_deye_component_gated_by_required_or():
    """DEYE must only activate when an auth path is configured (app_id OR token_hash).

    All individual args are optional (to allow either auth mode), so without a
    required_or gate the component would start for every Predbat instance.
    """
    failed = False
    info = COMPONENT_LIST.get("deye", {})
    required_or = info.get("required_or")
    if not required_or:
        print("ERROR: deye component has no required_or gate — it would activate for every instance")
        return True
    if set(required_or) != {"app_id", "key"}:
        print(f"ERROR: deye required_or should gate on app_id/key, got {required_or}")
        failed = True
    # Every individual arg must stay optional (the required_or is the only activation gate).
    for arg, spec in info.get("args", {}).items():
        if spec.get("required"):
            print(f"ERROR: deye arg {arg} is required=True; activation must come from required_or, not a single arg")
            failed = True
    assert not failed, "test_deye_component_gated_by_required_or"


def test_oauth_mode_uses_injected_access_token_not_hash():
    """In oauth mode access_token must be the injected deye_key, never token_hash.

    OAuthMixin._init_oauth() assigns its key argument straight to access_token, so
    handing it token_hash makes DEYE reject every call with "auth invalid token" --
    and a far-future token_expires_at means the refresh that would replace it never
    runs. Predbat.com maps the real token to deye_key.
    """
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.initialize(key="real-access-token", auth_method="oauth", token_hash="dedup-hash", token_expires_at="2099-01-01T00:00:00+00:00")
    if d.access_token != "real-access-token":
        print(f"ERROR: oauth access_token expected 'real-access-token' got {d.access_token!r}")
        failed = True
    if d.token_hash != "dedup-hash":
        print(f"ERROR: token_hash expected 'dedup-hash' got {d.token_hash!r}")
        failed = True
    # The component must also be gated on the token being present, not on the hash.
    info = COMPONENT_LIST.get("deye", {})
    if "key" not in info.get("args", {}):
        print("ERROR: deye component has no 'key' arg - the injected deye_key would be dropped")
        failed = True
    assert not failed, "test_oauth_mode_uses_injected_access_token_not_hash"


def test_telemetry_and_energy_keys_match_live_response():
    """Pin the key spellings confirmed against a live SUN-8K on 2026-07-28.

    The original guesses matched nothing and _as_float() zeroed them, so this
    guards the exact strings -- including DEYE's casing and the space after the
    hyphen in the temperature key.
    """
    failed = False
    expect_telemetry = {
        "soc": "SOC",
        "battery_power": "BatteryPower",
        "grid_power": "TotalGridPower",
        "pv_power": "TotalSolarPower",
        "load_power": "TotalConsumptionPower",
        "temperature": "Temperature- Battery",
    }
    expect_energy = {
        "load_today": "DailyConsumption",
        "import_today": "DailyEnergyPurchased",
        "export_today": "DailyGridFeedIn",
        "pv_today": "DailyActiveProduction",
    }
    if DEYE_TELEMETRY_KEYS != expect_telemetry:
        print(f"ERROR: DEYE_TELEMETRY_KEYS drifted from the live response: {DEYE_TELEMETRY_KEYS}")
        failed = True
    if DEYE_ENERGY_KEYS != expect_energy:
        print(f"ERROR: DEYE_ENERGY_KEYS drifted from the live response: {DEYE_ENERGY_KEYS}")
        failed = True
    assert not failed, "test_telemetry_and_energy_keys_match_live_response"


def test_battery_capacity_converts_amp_hours_to_kwh():
    """battCapacity is Ah; soc_max is kWh. 1200 Ah must not publish as 1200 kWh."""
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.device_battery_config = {"SN1": {"battCapacity": 1200}}
    kwh = d._battery_capacity_kwh("SN1")
    if kwh == 1200:
        print("ERROR: raw Ah published as kWh - a 1200 kWh battery")
        return True
    if not 55.0 < kwh < 70.0:
        print(f"ERROR: 1200 Ah at ~51.2 V should be ~61 kWh, got {kwh}")
        failed = True
    assert not failed, "test_battery_capacity_converts_amp_hours_to_kwh"


def test_unmatched_telemetry_keys_are_reported():
    """A dataList that matches no DEYE_TELEMETRY_KEYS must log the real key names.

    _as_float() turns an absent key into 0.0, so a wrong spelling publishes SOC 0%
    and temperature 0C instead of failing -- indistinguishable from a flat battery.
    The log must name the keys DEYE actually returned, and must not repeat per cycle.
    """
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.initialize(key="tok", auth_method="oauth")
    d.log_messages.clear()  # drop the "DeyeAPI initialising" line so counts below are exact
    live = {"batteryCapacitySoc": 55, "batteryTemp": 21.4}
    d._report_unmatched_telemetry("SN1", live)
    if len(d.log_messages) != 1:
        print(f"ERROR: expected exactly 1 warning, got {d.log_messages}")
        return True
    warning = d.log_messages[0]
    for key in live:
        if key not in warning:
            print(f"ERROR: warning does not name the live key {key}: {warning}")
            failed = True
    if "SOC" not in warning:
        print(f"ERROR: warning does not name the expected-but-missing key: {warning}")
        failed = True
    # Second call for the same serial must stay quiet (this runs every poll cycle).
    d._report_unmatched_telemetry("SN1", live)
    if len(d.log_messages) != 1:
        print(f"ERROR: warning repeated for the same serial: {d.log_messages}")
        failed = True
    # A fully-matching dataList must never warn.
    d._report_unmatched_telemetry("SN2", {key: 1 for key in DEYE_TELEMETRY_KEYS.values()})
    if len(d.log_messages) != 1:
        print(f"ERROR: warned on a fully-matching dataList: {d.log_messages}")
        failed = True
    assert not failed, "test_unmatched_telemetry_keys_are_reported"


def test_fetch_token_without_credentials_fails_soft():
    """Missing app_credentials args must return False, not raise out of run().

    A SaaS instance whose config omits deye_auth_method falls back to the
    app_credentials default with every credential unset; hashing the None
    password used to raise AttributeError on every 5-minute cycle.
    """
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    # None (not "") mirrors production: the SaaS config layer supplies JSON null for
    # every app_credentials key, and hashing that None was the actual crash.
    d.initialize(app_id=None, app_secret=None, username=None, password=None, auth_method="app_credentials", token_hash="saas-hash")
    try:
        result = asyncio.run(d.fetch_token())
    except Exception as e:
        print(f"ERROR: fetch_token raised {type(e).__name__}: {e} - it must fail soft")
        return True
    if result is not False:
        print(f"ERROR: fetch_token expected False with no credentials, got {result!r}")
        failed = True
    if not any("missing" in message for message in d.log_messages):
        print(f"ERROR: fetch_token logged no explanation, got {d.log_messages}")
        failed = True
    assert not failed, "test_fetch_token_without_credentials_fails_soft"


def run_deye_config_tests(my_predbat):
    """Run all DEYE config/INVERTER_DEF tests."""
    failed = False
    for name, fn in [
        ("inverter_def", test_deyecloud_inverter_def),
        ("initialize_token_hash_order", test_initialize_preserves_configured_token_hash),
        ("required_or_gate", test_deye_component_gated_by_required_or),
        ("oauth_access_token_not_hash", test_oauth_mode_uses_injected_access_token_not_hash),
        ("live_key_spellings", test_telemetry_and_energy_keys_match_live_response),
        ("capacity_ah_to_kwh", test_battery_capacity_converts_amp_hours_to_kwh),
        ("unmatched_telemetry_keys", test_unmatched_telemetry_keys_are_reported),
        ("fetch_token_no_credentials", test_fetch_token_without_credentials_fails_soft),
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

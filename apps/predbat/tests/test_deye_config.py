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
        "battery_voltage": "BatteryVoltage",
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


def test_injected_key_without_app_credentials_infers_oauth():
    """A host that injects deye_key but forgets deye_auth_method must still authenticate.

    auth_method defaults to app_credentials, and in that mode _init_oauth() discards
    the key entirely -- so trusting the default leaves the component with no
    credential at all and every DEYE call rejected.
    """
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.initialize(key="injected-token", token_hash="hash")  # no auth_method, no app_id
    if d.auth_method != "oauth":
        print(f"ERROR: auth_method should be inferred as oauth, got {d.auth_method!r}")
        failed = True
    if d.access_token != "injected-token":
        print(f"ERROR: injected token discarded, access_token={d.access_token!r}")
        failed = True
    # A genuine self-hosted add-on config must NOT be hijacked into oauth.
    e = DeyeAPI.__new__(DeyeAPI)
    e.log_messages = []
    e.log = lambda message: e.log_messages.append(message)
    e.initialize(app_id="id", app_secret="sec", username="u@e.com", password="pw")
    if e.auth_method != "app_credentials":
        print(f"ERROR: app_credentials config was hijacked to {e.auth_method!r}")
        failed = True
    assert not failed, "test_injected_key_without_app_credentials_infers_oauth"


def test_daily_energy_resets_to_zero_after_rollover():
    """A counter that vanishes after being seen is midnight, not 'keep yesterday's total'."""
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.initialize(key="tok", auth_method="oauth")
    # Never-seen counters are omitted, so no sensor is published for a model that
    # does not report them at all.
    first = d._daily_energy("SN1", {"DailyConsumption": "15.6"})
    if first.get("load_today") != 15.6:
        print(f"ERROR: load_today not read: {first}")
        failed = True
    if "import_today" in first:
        print(f"ERROR: never-seen counter should be omitted, got {first}")
        failed = True
    # Same key absent on a later poll = rollover, so it must read zero rather than
    # leaving the previously published 15.6 kWh standing as today's load.
    after = d._daily_energy("SN1", {})
    if after.get("load_today") != 0.0:
        print(f"ERROR: load_today should reset to 0.0 at rollover, got {after}")
        failed = True
    assert not failed, "test_daily_energy_resets_to_zero_after_rollover"


def test_capacity_warns_when_pack_voltage_contradicts_nominal():
    """A high-voltage stack must not silently convert at the 48 V default."""
    failed = False
    d = DeyeAPI.__new__(DeyeAPI)
    d.log_messages = []
    d.log = lambda message: d.log_messages.append(message)
    d.initialize(key="tok", auth_method="oauth")
    d.device_battery_config = {"HV1": {"battCapacity": 100}}
    d.device_values = {"HV1": {"battery_voltage": 300.0}}
    d.log_messages.clear()
    d._battery_capacity_kwh("HV1")
    if not any("nominal" in message for message in d.log_messages):
        print(f"ERROR: no warning for a 300 V pack converted at 51.2 V: {d.log_messages}")
        failed = True
    # An explicit override must be honoured and must not warn.
    hv = DeyeAPI.__new__(DeyeAPI)
    hv.log_messages = []
    hv.log = lambda message: hv.log_messages.append(message)
    hv.initialize(key="tok", auth_method="oauth", battery_nominal_voltage=300)
    hv.device_battery_config = {"HV1": {"battCapacity": 100}}
    hv.device_values = {"HV1": {"battery_voltage": 300.0}}
    hv.log_messages.clear()
    kwh = hv._battery_capacity_kwh("HV1")
    if kwh != 30.0:
        print(f"ERROR: 100 Ah at 300 V should be 30.0 kWh, got {kwh}")
        failed = True
    if hv.log_messages:
        print(f"ERROR: warned despite an explicit nominal voltage: {hv.log_messages}")
        failed = True
    assert not failed, "test_capacity_warns_when_pack_voltage_contradicts_nominal"


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
        ("infer_oauth_from_key", test_injected_key_without_app_credentials_infers_oauth),
        ("daily_energy_rollover", test_daily_energy_resets_to_zero_after_rollover),
        ("capacity_voltage_warning", test_capacity_warns_when_pack_voltage_contradicts_nominal),
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

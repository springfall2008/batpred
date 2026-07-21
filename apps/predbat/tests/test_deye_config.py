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
    if set(required_or) != {"app_id", "token_hash"}:
        print(f"ERROR: deye required_or should gate on app_id/token_hash, got {required_or}")
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

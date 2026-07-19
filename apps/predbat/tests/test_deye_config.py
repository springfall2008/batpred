import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from config import INVERTER_DEF, APPS_SCHEMA


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

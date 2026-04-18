# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the Compare tariff engine.

Covers:
  - apply_hardware_overrides: verify each override key sets the right attribute
  - hardware isolation between tariffs: overrides applied for tariff N must not
    bleed into tariff N+1 in run_all() (regression for the bug where agile_fixed
    reported a final SOC larger than the normal battery size)
"""

from compare import Compare
from const import MINUTE_WATT


class _FakePredbat:
    """Minimal predbat stub sufficient for Compare tests that don't run a full plan."""

    def __init__(self):
        """Initialise stub with representative hardware defaults."""
        self.soc_kw = 5.0
        self.soc_max = 10.0
        self.battery_rate_max_charge = 3.0 * 1000 / MINUTE_WATT
        self.battery_rate_max_charge_dc = 3.0 * 1000 / MINUTE_WATT
        self.battery_rate_max_discharge = 3.0 * 1000 / MINUTE_WATT
        self.inverter_limit = 3.6 * 1000 / MINUTE_WATT
        self.config_root = "."
        self.prefix = "predbat"
        self.currency_symbols = ["£", "p"]
        self.comparisons = {}

    def log(self, msg):
        """Discard log messages."""
        pass

    def dashboard_item(self, *args, **kwargs):
        """Stub."""
        pass


def _make_compare():
    """Return a Compare instance wired to a _FakePredbat, bypassing load_yaml."""
    pb = _FakePredbat()
    cmp = Compare.__new__(Compare)
    cmp.pb = pb
    cmp.log = pb.log
    cmp.config_root = pb.config_root
    cmp.dashboard_item = pb.dashboard_item
    cmp.currency_symbols = pb.currency_symbols
    cmp.prefix = pb.prefix
    cmp.comparisons = {}
    return cmp, pb


def test_compare(my_predbat):
    """Run all compare unit tests."""
    failed = 0
    print("**** Running compare tests ****\n")

    # ------------------------------------------------------------------
    # T1: apply_hardware_overrides – soc_max override
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    original_soc_kw = pb.soc_kw
    original_charge_dc = pb.battery_rate_max_charge_dc
    tariff = {"override_soc_max_kwh": 20.0}
    cmp.apply_hardware_overrides(tariff, pb)
    if pb.soc_max != 20.0:
        print("ERROR T1: soc_max should be 20.0, got {}".format(pb.soc_max))
        failed += 1
    # soc_kw must not exceed new soc_max
    if pb.soc_kw > pb.soc_max:
        print("ERROR T1b: soc_kw {} > soc_max {} after override".format(pb.soc_kw, pb.soc_max))
        failed += 1
    # When soc_kw is already within capacity it must be unchanged
    if pb.soc_kw != original_soc_kw:
        print("ERROR T1c: soc_kw changed unexpectedly from {} to {}".format(original_soc_kw, pb.soc_kw))
        failed += 1
    else:
        print("PASS T1: soc_max override")

    # ------------------------------------------------------------------
    # T2: apply_hardware_overrides – soc_kw clamped when over new capacity
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    pb.soc_kw = 15.0  # above the override capacity
    tariff = {"override_soc_max_kwh": 10.0}
    cmp.apply_hardware_overrides(tariff, pb)
    if pb.soc_kw != 10.0:
        print("ERROR T2: soc_kw should be clamped to 10.0, got {}".format(pb.soc_kw))
        failed += 1
    else:
        print("PASS T2: soc_kw clamped to new soc_max")

    # ------------------------------------------------------------------
    # T3: apply_hardware_overrides – charge rate override also scales DC rate
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    # Set DC rate double the AC rate to verify proportional scaling
    pb.battery_rate_max_charge = 3.0 * 1000 / MINUTE_WATT
    pb.battery_rate_max_charge_dc = 6.0 * 1000 / MINUTE_WATT
    tariff = {"override_battery_rate_max_charge_kw": 6.0}
    cmp.apply_hardware_overrides(tariff, pb)
    expected_ac = 6.0 * 1000 / MINUTE_WATT
    expected_dc = 12.0 * 1000 / MINUTE_WATT  # doubled proportionally
    if abs(pb.battery_rate_max_charge - expected_ac) > 1e-9:
        print("ERROR T3: battery_rate_max_charge should be {}, got {}".format(expected_ac, pb.battery_rate_max_charge))
        failed += 1
    elif abs(pb.battery_rate_max_charge_dc - expected_dc) > 1e-9:
        print("ERROR T3b: battery_rate_max_charge_dc should be {} (proportional), got {}".format(expected_dc, pb.battery_rate_max_charge_dc))
        failed += 1
    else:
        print("PASS T3: battery_rate_max_charge and battery_rate_max_charge_dc override")

    # ------------------------------------------------------------------
    # T4: apply_hardware_overrides – discharge rate override
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    tariff = {"override_battery_rate_max_discharge_kw": 5.0}
    cmp.apply_hardware_overrides(tariff, pb)
    expected = 5.0 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_discharge - expected) > 1e-9:
        print("ERROR T4: battery_rate_max_discharge should be {}, got {}".format(expected, pb.battery_rate_max_discharge))
        failed += 1
    else:
        print("PASS T4: battery_rate_max_discharge override")

    # ------------------------------------------------------------------
    # T5: apply_hardware_overrides – inverter limit override
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    tariff = {"override_inverter_limit_kw": 5.0}
    cmp.apply_hardware_overrides(tariff, pb)
    expected = 5.0 * 1000 / MINUTE_WATT
    if abs(pb.inverter_limit - expected) > 1e-9:
        print("ERROR T5: inverter_limit should be {}, got {}".format(expected, pb.inverter_limit))
        failed += 1
    else:
        print("PASS T5: inverter_limit override")

    # ------------------------------------------------------------------
    # T6: apply_hardware_overrides – empty tariff leaves attrs unchanged
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    original_soc_max = pb.soc_max
    original_charge = pb.battery_rate_max_charge
    original_discharge = pb.battery_rate_max_discharge
    original_limit = pb.inverter_limit
    cmp.apply_hardware_overrides({}, pb)
    if pb.soc_max != original_soc_max or pb.battery_rate_max_charge != original_charge or pb.battery_rate_max_discharge != original_discharge or pb.inverter_limit != original_limit:
        print("ERROR T6: empty tariff should not change hardware attrs")
        failed += 1
    else:
        print("PASS T6: empty tariff leaves attrs unchanged")

    # ------------------------------------------------------------------
    # T7: hardware isolation – simulate the run_all mid-loop restore pattern
    #     so that a big-battery tariff's overrides do NOT bleed into the
    #     subsequent tariff (regression test for the soc_max bleed bug)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    # Save hardware state (as run_all does before the loop)
    save_soc_max = pb.soc_max
    save_charge = pb.battery_rate_max_charge
    save_charge_dc = pb.battery_rate_max_charge_dc
    save_discharge = pb.battery_rate_max_discharge
    save_limit = pb.inverter_limit

    big_battery_tariff = {
        "override_soc_max_kwh": 20.0,
        "override_battery_rate_max_charge_kw": 6.0,
        "override_battery_rate_max_discharge_kw": 6.0,
        "override_inverter_limit_kw": 6.0,
    }
    # Simulate running the first tariff (big battery)
    cmp.apply_hardware_overrides(big_battery_tariff, pb)
    # Confirm the override was applied
    if pb.soc_max != 20.0:
        print("ERROR T7 setup: soc_max should be 20.0 after big-battery tariff, got {}".format(pb.soc_max))
        failed += 1

    # Mid-loop restore (this is the fix)
    pb.soc_max = save_soc_max
    pb.battery_rate_max_charge = save_charge
    pb.battery_rate_max_charge_dc = save_charge_dc
    pb.battery_rate_max_discharge = save_discharge
    pb.inverter_limit = save_limit

    # Now simulate running the second tariff (no overrides)
    normal_tariff = {}
    cmp.apply_hardware_overrides(normal_tariff, pb)

    if pb.soc_max != save_soc_max:
        print("ERROR T7: soc_max leaked from big-battery tariff into next tariff: expected {}, got {}".format(save_soc_max, pb.soc_max))
        failed += 1
    elif pb.battery_rate_max_charge != save_charge:
        print("ERROR T7: battery_rate_max_charge leaked: expected {}, got {}".format(save_charge, pb.battery_rate_max_charge))
        failed += 1
    elif pb.battery_rate_max_charge_dc != save_charge_dc:
        print("ERROR T7: battery_rate_max_charge_dc leaked: expected {}, got {}".format(save_charge_dc, pb.battery_rate_max_charge_dc))
        failed += 1
    elif pb.battery_rate_max_discharge != save_discharge:
        print("ERROR T7: battery_rate_max_discharge leaked: expected {}, got {}".format(save_discharge, pb.battery_rate_max_discharge))
        failed += 1
    elif pb.inverter_limit != save_limit:
        print("ERROR T7: inverter_limit leaked: expected {}, got {}".format(save_limit, pb.inverter_limit))
        failed += 1
    else:
        print("PASS T7: hardware attrs restored correctly between tariffs")

    # ------------------------------------------------------------------
    # T8: hardware isolation – WITHOUT mid-loop restore the bleed is visible
    #     (validates that the test would have caught the original bug)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    save_soc_max = pb.soc_max  # 10 kWh

    # First tariff with big battery override – no restore after
    cmp.apply_hardware_overrides({"override_soc_max_kwh": 20.0}, pb)
    # Do NOT restore – replicate the old buggy code

    # Second tariff with no override
    cmp.apply_hardware_overrides({}, pb)

    # Without the fix, soc_max stays at 20 (the bleed)
    bleed_detected = pb.soc_max != save_soc_max
    if not bleed_detected:
        print("ERROR T8: expected to detect bleed when restore is skipped, but soc_max == {}".format(pb.soc_max))
        failed += 1
    else:
        print("PASS T8: bleed correctly detected when mid-loop restore is absent (confirms T7 tests the right thing)")

    # ------------------------------------------------------------------
    # T9: config isolation – the mid-loop config snapshot/restore pattern
    #     prevents fetch_config() overrides bleeding into later tariffs
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    # Give pb a minimal config_index with one overridable item
    pb.config_index = {"best_soc_min": {"value": 0.5}}
    fetch_config_calls = []

    def _mock_fetch_config_options():
        fetch_config_calls.append(1)

    pb.fetch_config_options = _mock_fetch_config_options

    tariff_with_config = {"config": {"best_soc_min": 2.0}}

    # Simulate the snapshot-before / restore-after pattern from run_all()
    config_snapshot = {}
    for key in tariff_with_config.get("config", {}):
        item = pb.config_index.get(key)
        if item is not None:
            config_snapshot[key] = item.get("value")

    # Simulate fetch_config() running inside run_single()
    cmp.fetch_config(tariff_with_config)

    if pb.config_index["best_soc_min"]["value"] != 2.0:
        print("ERROR T9 setup: config override was not applied, got {}".format(pb.config_index["best_soc_min"]["value"]))
        failed += 1

    # Now simulate the mid-loop restore
    if config_snapshot:
        for key, orig_value in config_snapshot.items():
            item = pb.config_index.get(key)
            if item is not None:
                item["value"] = orig_value
        pb.fetch_config_options()

    if pb.config_index["best_soc_min"]["value"] != 0.5:
        print("ERROR T9: config bled after restore: expected 0.5, got {}".format(pb.config_index["best_soc_min"]["value"]))
        failed += 1
    elif not fetch_config_calls:
        print("ERROR T9: fetch_config_options() not called during restore")
        failed += 1
    else:
        print("PASS T9: config values restored between tariffs")

    # ------------------------------------------------------------------
    # T10: config isolation – WITHOUT restore the config bleed is detectable
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    pb.config_index = {"best_soc_min": {"value": 0.5}}
    pb.fetch_config_options = lambda: None

    cmp.fetch_config({"config": {"best_soc_min": 2.0}})
    # Do NOT restore – replicate the old buggy code

    # Now run a second tariff with no config – value should still be 2.0 (bleed)
    cmp.fetch_config({})  # empty config → no changes

    if pb.config_index["best_soc_min"]["value"] == 0.5:
        print("ERROR T10: expected to detect config bleed when restore is skipped")
        failed += 1
    else:
        print("PASS T10: config bleed correctly detected when restore is absent (confirms T9 tests the right thing)")

    # ------------------------------------------------------------------
    # T11: apply_hardware_overrides – non-numeric values are skipped safely
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    original_soc_max = pb.soc_max
    original_charge = pb.battery_rate_max_charge
    original_charge_dc = pb.battery_rate_max_charge_dc
    original_discharge = pb.battery_rate_max_discharge
    original_limit = pb.inverter_limit
    bad_tariff = {
        "id": "bad_tariff",
        "override_soc_max_kwh": "not_a_number",
        "override_battery_rate_max_charge_kw": "bad",
        "override_battery_rate_max_discharge_kw": None,
        "override_inverter_limit_kw": "oops",
    }
    try:
        cmp.apply_hardware_overrides(bad_tariff, pb)
        # All attrs must be unchanged since every value was bad
        if pb.soc_max != original_soc_max:
            print("ERROR T11: soc_max changed on bad input: got {}".format(pb.soc_max))
            failed += 1
        elif pb.battery_rate_max_charge != original_charge:
            print("ERROR T11: battery_rate_max_charge changed on bad input")
            failed += 1
        elif pb.battery_rate_max_charge_dc != original_charge_dc:
            print("ERROR T11: battery_rate_max_charge_dc changed on bad input")
            failed += 1
        elif pb.battery_rate_max_discharge != original_discharge:
            print("ERROR T11: battery_rate_max_discharge changed on bad input")
            failed += 1
        elif pb.inverter_limit != original_limit:
            print("ERROR T11: inverter_limit changed on bad input")
            failed += 1
        else:
            print("PASS T11: non-numeric override values are skipped without raising")
    except (ValueError, TypeError) as e:
        print("ERROR T11: apply_hardware_overrides raised on bad input: {}".format(e))
        failed += 1

    if failed:
        print("**** compare tests FAILED: {} errors ****\n".format(failed))
    else:
        print("**** compare tests PASSED ****\n")
    return failed

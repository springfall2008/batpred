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
  - apply_hardware_overrides: verify each override key sets the right attribute, including
    that the export rate follows the discharge override unless overridden explicitly (#4895)
  - hardware isolation between tariffs: overrides applied for tariff N must not
    bleed into tariff N+1 in run_all() (regression for the bug where agile_fixed
    reported a final SOC larger than the normal battery size)
"""

import inspect
import os
import tempfile
from unittest.mock import MagicMock, patch

from compare import Compare, check_compare_list, valid_compare_list
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
        # Deliberately lower than the discharge rate, as real hardware often is: it is derived from
        # min(inverter_limit_export, battery_rate_max_raw), so a discharge override that failed to
        # move it left export windows pinned at the real rate (issue #4895)
        self.battery_rate_max_export = 2.6 * 1000 / MINUTE_WATT
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
    cmp.live_saving_state = None
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
    original_export = pb.battery_rate_max_export
    original_limit = pb.inverter_limit
    cmp.apply_hardware_overrides({}, pb)
    if pb.soc_max != original_soc_max or pb.battery_rate_max_charge != original_charge or pb.battery_rate_max_discharge != original_discharge or pb.battery_rate_max_export != original_export or pb.inverter_limit != original_limit:
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
    save_export = pb.battery_rate_max_export
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
    pb.battery_rate_max_export = save_export
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
    elif pb.battery_rate_max_export != save_export:
        print("ERROR T7: battery_rate_max_export leaked: expected {}, got {}".format(save_export, pb.battery_rate_max_export))
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
    original_export = pb.battery_rate_max_export
    original_limit = pb.inverter_limit
    bad_tariff = {
        "id": "bad_tariff",
        "override_soc_max_kwh": "not_a_number",
        "override_battery_rate_max_charge_kw": "bad",
        "override_battery_rate_max_discharge_kw": None,
        "override_battery_rate_max_export_kw": "nope",
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
        elif pb.battery_rate_max_export != original_export:
            print("ERROR T11: battery_rate_max_export changed on bad input")
            failed += 1
        elif pb.inverter_limit != original_limit:
            print("ERROR T11: inverter_limit changed on bad input")
            failed += 1
        else:
            print("PASS T11: non-numeric override values are skipped without raising")
    except (ValueError, TypeError) as e:
        print("ERROR T11: apply_hardware_overrides raised on bad input: {}".format(e))
        failed += 1

    # ------------------------------------------------------------------
    # T12: run_all() source actually contains the mid-loop config restore
    #      (T9/T10 above only prove the pattern works in isolation - this
    #      closes the gap by checking it's genuinely wired into run_all()
    #      itself, not just demonstrated as a standalone snippet - #4156)
    # ------------------------------------------------------------------
    run_all_source = inspect.getsource(Compare.run_all)
    # The restore block must appear between the "restore hardware settings" comment (which is
    # inside the per-tariff loop) and the loop's closing use of publish_data(), otherwise it's
    # not actually running once per tariff.
    hardware_restore_idx = run_all_source.find("Restore hardware settings after each tariff")
    publish_data_idx = run_all_source.find("self.publish_data()")
    config_restore_idx = run_all_source.find("config_snapshot", hardware_restore_idx + 1) if hardware_restore_idx >= 0 else -1

    if hardware_restore_idx < 0 or publish_data_idx < 0:
        print("ERROR T12: could not locate expected markers in run_all() source - test may need updating")
        failed += 1
    elif not (hardware_restore_idx < config_restore_idx < publish_data_idx):
        print("ERROR T12: config_snapshot restore is not positioned inside the per-tariff loop in run_all() - the #4156 leak is not actually fixed")
        failed += 1
    else:
        print("PASS T12: run_all() restores config: overrides inside the per-tariff loop, not just once at the end")

    # ------------------------------------------------------------------
    # T13: run_single() re-plans iBoost against the tariff's own rates
    #      instead of leaving it stuck with the plan computed for the live
    #      tariff (the compare-plan iBoost timing bug)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    pb.iboost_enable = True
    pb.iboost_solar = False
    pb.iboost_charging = False
    pb.iboost_smart = True
    pb.iboost_today = 3.0
    pb.import_today_now = 0
    pb.export_today_now = 0
    pb.cost_today_sofar = 0
    pb.carbon_today_sofar = 0
    pb.forecast_plan_hours = 48
    pb.manual_charge_times = None
    pb.manual_export_times = None
    pb.manual_freeze_charge_times = None
    pb.manual_freeze_export_times = None
    pb.manual_demand_times = None
    pb.manual_all_times = None
    pb.octopus_intelligent_charging = False
    pb.iboost_plan = ["stale_plan_from_live_tariff"]

    tariff_plan_calls = []

    def _mock_plan_iboost_smart():
        tariff_plan_calls.append(1)
        return ["fresh_plan_for_this_tariff"]

    pb.plan_iboost_smart = _mock_plan_iboost_smart

    cmp.fetch_config = lambda tariff: None
    cmp.apply_hardware_overrides = lambda tariff, pb: None
    cmp.fetch_rates = lambda tariff, base_import, base_export, io_adjusted_base=None: "existing"
    cmp.recompute_car_charging = lambda slots: None
    cmp.run_scenario = lambda end_record: {"cost": 0, "metric": 0}

    cmp.run_single({"name": "test_tariff", "id": "test"}, {}, {}, 48 * 60, fetch_sensor=False)

    if not tariff_plan_calls:
        print("ERROR T13: plan_iboost_smart() was not called during run_single()")
        failed += 1
    elif pb.iboost_plan != ["fresh_plan_for_this_tariff"]:
        print("ERROR T13: iboost_plan should be replaced with the tariff-specific plan, got {}".format(pb.iboost_plan))
        failed += 1
    else:
        print("PASS T13: run_single() re-plans iBoost using the compared tariff's own rates")

    # ------------------------------------------------------------------
    # T14: run_single() clears iboost_plan (rather than reusing a stale one)
    #      when iBoost smart-rate planning isn't applicable for this tariff run
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    pb.iboost_enable = True
    pb.iboost_solar = True
    pb.iboost_charging = True
    pb.iboost_smart = False
    pb.iboost_today = 0
    pb.import_today_now = 0
    pb.export_today_now = 0
    pb.cost_today_sofar = 0
    pb.carbon_today_sofar = 0
    pb.forecast_plan_hours = 48
    pb.manual_charge_times = None
    pb.manual_export_times = None
    pb.manual_freeze_charge_times = None
    pb.manual_freeze_export_times = None
    pb.manual_demand_times = None
    pb.manual_all_times = None
    pb.octopus_intelligent_charging = False
    pb.iboost_plan = ["stale_plan_from_live_tariff"]
    pb.plan_iboost_smart = lambda: tariff_plan_calls.append(1) or []

    cmp.fetch_config = lambda tariff: None
    cmp.apply_hardware_overrides = lambda tariff, pb: None
    cmp.fetch_rates = lambda tariff, base_import, base_export, io_adjusted_base=None: "existing"
    cmp.recompute_car_charging = lambda slots: None
    cmp.run_scenario = lambda end_record: {"cost": 0, "metric": 0}

    tariff_plan_calls.clear()
    cmp.run_single({"name": "test_tariff2", "id": "test2"}, {}, {}, 48 * 60, fetch_sensor=False)

    if tariff_plan_calls:
        print("ERROR T14: plan_iboost_smart() should not be called when iboost_charging/iboost_solar handle boosting and iboost_smart is off")
        failed += 1
    elif pb.iboost_plan != []:
        print("ERROR T14: stale iboost_plan from the live tariff should be cleared, got {}".format(pb.iboost_plan))
        failed += 1
    else:
        print("PASS T14: run_single() clears stale iboost_plan when smart-rate planning isn't applicable")

    # ------------------------------------------------------------------
    # T15: apply_hardware_overrides - a discharge override also moves the
    #      export rate, otherwise export windows stay pinned at the real
    #      hardware rate and export earnings are understated (#4895)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    real_export = pb.battery_rate_max_export
    cmp.apply_hardware_overrides({"override_battery_rate_max_discharge_kw": 30.0}, pb)
    expected = 30.0 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_export - expected) > 1e-9:
        print("ERROR T15: battery_rate_max_export should follow the discharge override to {}, got {} (real hardware was {})".format(expected, pb.battery_rate_max_export, real_export))
        failed += 1
    else:
        print("PASS T15: discharge override carries battery_rate_max_export with it")

    # ------------------------------------------------------------------
    # T16: apply_hardware_overrides - an explicit export override wins over
    #      the discharge override, so export-limited hardware can be modelled
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    cmp.apply_hardware_overrides({"override_battery_rate_max_discharge_kw": 30.0, "override_battery_rate_max_export_kw": 5.0}, pb)
    expected_discharge = 30.0 * 1000 / MINUTE_WATT
    expected_export = 5.0 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_discharge - expected_discharge) > 1e-9:
        print("ERROR T16: battery_rate_max_discharge should be {}, got {}".format(expected_discharge, pb.battery_rate_max_discharge))
        failed += 1
    elif abs(pb.battery_rate_max_export - expected_export) > 1e-9:
        print("ERROR T16: explicit battery_rate_max_export override should be {}, got {}".format(expected_export, pb.battery_rate_max_export))
        failed += 1
    else:
        print("PASS T16: explicit export override takes precedence over the discharge override")

    # ------------------------------------------------------------------
    # T17: apply_hardware_overrides - export override on its own leaves the
    #      discharge rate alone
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    original_discharge = pb.battery_rate_max_discharge
    cmp.apply_hardware_overrides({"override_battery_rate_max_export_kw": 8.0}, pb)
    expected_export = 8.0 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_export - expected_export) > 1e-9:
        print("ERROR T17: battery_rate_max_export should be {}, got {}".format(expected_export, pb.battery_rate_max_export))
        failed += 1
    elif pb.battery_rate_max_discharge != original_discharge:
        print("ERROR T17: battery_rate_max_discharge should be untouched, got {}".format(pb.battery_rate_max_discharge))
        failed += 1
    else:
        print("PASS T17: export override alone leaves the discharge rate unchanged")

    # ------------------------------------------------------------------
    # T18: apply_hardware_overrides - a non-numeric export override falls
    #      back to the discharge override rather than silently reinstating
    #      the real hardware export rate
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    real_export = pb.battery_rate_max_export
    cmp.apply_hardware_overrides({"id": "typo", "override_battery_rate_max_discharge_kw": 12.0, "override_battery_rate_max_export_kw": "twelve"}, pb)
    expected = 12.0 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_export - expected) > 1e-9:
        print("ERROR T18: bad export override should fall back to the discharge rate {}, got {} (real hardware was {})".format(expected, pb.battery_rate_max_export, real_export))
        failed += 1
    else:
        print("PASS T18: bad export override value falls back to the discharge override")

    # ------------------------------------------------------------------
    # T19: run_all() actually saves and restores battery_rate_max_export,
    #      both mid-loop and at the end, so an export override can't bleed
    #      into the next tariff (T7 only proves the pattern in isolation).
    #      Positional checks (T12-style) so the snapshot must sit before the
    #      tariff loop, not just anywhere in the source.
    # ------------------------------------------------------------------
    run_all_source = inspect.getsource(Compare.run_all)
    loop_idx = run_all_source.find("for tariff in compare_list:")
    snapshot_idx = run_all_source.find("save_battery_rate_max_export = my_predbat.battery_rate_max_export")
    if loop_idx < 0 or snapshot_idx < 0:
        print("ERROR T19: could not locate the tariff loop or the battery_rate_max_export snapshot in run_all() source - test may need updating")
        failed += 1
    elif not (snapshot_idx < loop_idx):
        print("ERROR T19: battery_rate_max_export snapshot must appear before the tariff loop in run_all()")
        failed += 1
    elif run_all_source.count("my_predbat.battery_rate_max_export = save_battery_rate_max_export") < 2:
        print("ERROR T19: run_all() must restore battery_rate_max_export both inside the tariff loop and at the end")
        failed += 1
    else:
        print("PASS T19: run_all() snapshots and restores battery_rate_max_export")

    # ------------------------------------------------------------------
    # T20: apply_hardware_overrides - a non-numeric export override with no
    #      discharge override leaves the real hardware export rate in place
    #      (there is nothing to fall back to, and the guard must not regress
    #      to `not export_overridden`, which would copy the discharge rate)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    real_export = pb.battery_rate_max_export
    real_discharge = pb.battery_rate_max_discharge
    cmp.apply_hardware_overrides({"id": "typo", "override_battery_rate_max_export_kw": "twelve"}, pb)
    if abs(pb.battery_rate_max_export - real_export) > 1e-9:
        print("ERROR T20: export rate changed to {} on a bad export-only override, expected real hardware {}".format(pb.battery_rate_max_export, real_export))
        failed += 1
    elif pb.battery_rate_max_discharge != real_discharge:
        print("ERROR T20: discharge rate changed to {} on a bad export-only override".format(pb.battery_rate_max_discharge))
        failed += 1
    else:
        print("PASS T20: bad export-only override keeps the real hardware export rate")

    # ------------------------------------------------------------------
    # T21: apply_hardware_overrides - a numeric-string discharge override
    #      ("12.0" from a template) parses, applies, and correctly sets the
    #      flag so the export rate follows it - previously the raw-string log
    #      format raised and left the flag unset so export kept real hardware
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    cmp.apply_hardware_overrides({"override_battery_rate_max_discharge_kw": "12.0"}, pb)
    expected = 12.0 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_discharge - expected) > 1e-9:
        print("ERROR T21: numeric-string discharge override should apply as {}, got {}".format(expected, pb.battery_rate_max_discharge))
        failed += 1
    elif abs(pb.battery_rate_max_export - expected) > 1e-9:
        print("ERROR T21: numeric-string discharge override must still carry the export rate with it, export got {}".format(pb.battery_rate_max_export))
        failed += 1
    else:
        print("PASS T21: numeric-string discharge override applies and carries the export rate")

    # ------------------------------------------------------------------
    # T22: apply_hardware_overrides - a numeric-string export override on
    #      its own applies without raising
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    original_discharge = pb.battery_rate_max_discharge
    cmp.apply_hardware_overrides({"override_battery_rate_max_export_kw": "7.5"}, pb)
    expected = 7.5 * 1000 / MINUTE_WATT
    if abs(pb.battery_rate_max_export - expected) > 1e-9:
        print("ERROR T22: numeric-string export override should apply as {}, got {}".format(expected, pb.battery_rate_max_export))
        failed += 1
    elif pb.battery_rate_max_discharge != original_discharge:
        print("ERROR T22: discharge rate should be untouched, got {}".format(pb.battery_rate_max_discharge))
        failed += 1
    else:
        print("PASS T22: numeric-string export override applies")

    # ------------------------------------------------------------------
    # T23: apply_hardware_overrides - non-finite values (nan/inf, which
    #      float() accepts) are rejected like any other bad value
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    original_discharge = pb.battery_rate_max_discharge
    original_export = pb.battery_rate_max_export
    cmp.apply_hardware_overrides({"override_battery_rate_max_discharge_kw": "nan", "override_battery_rate_max_export_kw": "inf"}, pb)
    if pb.battery_rate_max_discharge != original_discharge:
        print("ERROR T23: nan discharge override should be skipped, got {}".format(pb.battery_rate_max_discharge))
        failed += 1
    elif pb.battery_rate_max_export != original_export:
        print("ERROR T23: inf export override should be skipped, got {}".format(pb.battery_rate_max_export))
        failed += 1
    else:
        print("PASS T23: non-finite override values are skipped")

    # ------------------------------------------------------------------
    # T24: recompute_car_charging() drops the live cycle's modelled
    #      car_charging_now slots - the compared tariff's car plan is rebuilt
    #      from scratch, so a charging-now slot judged against the live plan
    #      must not be layered on top of it (#5245 review)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    pb.num_cars = 1
    pb.car_charging_slots = [[]]
    pb.car_charging_now_slots = [[{"start": 730, "end": 750, "kwh": 2.4, "octopus": False}]]
    pb.car_charging_planned = [False]
    pb.car_charging_now = [False]
    pb.car_charging_soc = [0.0]
    pb.car_charging_limit = [50.0]
    pb.car_charging_battery_size = [100.0]
    pb.car_charging_exclusive = [False]
    cmp.recompute_car_charging([[]])
    if pb.car_charging_now_slots != [[]]:
        print("ERROR T24: recompute_car_charging() should clear car_charging_now_slots, got {}".format(pb.car_charging_now_slots))
        failed += 1
    else:
        print("PASS T24: recompute_car_charging() clears the live car_charging_now slots")

    # ------------------------------------------------------------------
    # T25: run_all() snapshots car_charging_now_slots before the tariff loop and
    #      restores it after it, as it does car_charging_slots, so the live plan
    #      gets its modelled slots back after a comparison (#5245 review)
    # ------------------------------------------------------------------
    snapshot_idx = run_all_source.find("save_car_charging_now_slots = copy.deepcopy(my_predbat.car_charging_now_slots)")
    if snapshot_idx < 0 or not (snapshot_idx < loop_idx):
        print("ERROR T25: car_charging_now_slots must be snapshotted before the tariff loop in run_all()")
        failed += 1
    elif run_all_source.find("my_predbat.car_charging_now_slots = save_car_charging_now_slots") < loop_idx:
        print("ERROR T25: run_all() must restore car_charging_now_slots after the tariff loop")
        failed += 1
    else:
        print("PASS T25: run_all() snapshots and restores car_charging_now_slots")

    # ------------------------------------------------------------------
    # T26: fetch_rates() - a tariff with its own import rates must not inherit the
    #      live tariff's Intelligent Octopus dispatch markers, while one that reuses
    #      the live import rates keeps them (#5286). This checks which markers each
    #      tariff is given, not their effect on the plan - empty rates skip the scans.
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    pb.minutes_now = 0
    pb.forecast_minutes = 0
    pb.basic_rates = lambda *args, **kwargs: {}
    live_markers = {900: True}

    pb.io_adjusted = dict(live_markers)
    cmp.fetch_rates({"id": "flat", "rates_import": [{"rate": 20.0}]}, {}, {}, live_markers)
    own_markers = pb.io_adjusted

    pb.io_adjusted = {}
    cmp.fetch_rates({"id": "existing"}, {}, {}, live_markers)
    existing_markers = pb.io_adjusted

    # A bad entity id keeps the live import rates, so it keeps their markers too
    pb.resolve_arg = lambda *args, **kwargs: None
    pb.io_adjusted = {}
    cmp.fetch_rates({"id": "bad", "metric_octopus_import": "sensor.missing"}, {}, {}, live_markers)
    bad_entity_markers = pb.io_adjusted

    run_all_source = inspect.getsource(Compare.run_all)
    restore_idx = run_all_source.find("my_predbat.io_adjusted = io_adjusted_base")
    if own_markers:
        print("ERROR T26: a tariff with its own import rates inherited dispatch markers {}".format(own_markers))
        failed += 1
    elif existing_markers != live_markers or existing_markers is live_markers:
        print("ERROR T26: a tariff reusing the live import rates should get a copy of the live markers, got {}".format(existing_markers))
        failed += 1
    elif bad_entity_markers != live_markers:
        print("ERROR T26: a bad import entity id keeps the live rates, so should keep the live markers, got {}".format(bad_entity_markers))
        failed += 1
    elif restore_idx < run_all_source.find("for tariff in compare_list:"):
        print("ERROR T26: run_all() must restore the live io_adjusted after the tariff loop")
        failed += 1
    else:
        print("PASS T26: only a tariff reusing the live import rates keeps the live dispatch markers")

    # ------------------------------------------------------------------
    # T27: valid_compare_list() keeps only the entries run_all() can compare -
    #      skipping, with a warning naming the entry's position, one that isn't a
    #      dict, has no id, repeats an earlier id or has no name - and turns a
    #      numeric id into a string (#5472 - run_all() used to index tariff["id"]
    #      directly, raising KeyError every cycle, and a numeric id still crashed
    #      publish_data() building the sensor name)
    # ------------------------------------------------------------------
    logged = []
    entries = [
        "not_a_dict",
        {"name": "No Id Tariff"},
        {"id": "  ", "name": "Blank Id Tariff"},
        {"id": "agile", "name": "Agile"},
        {"id": "agile", "name": "Agile Again"},
        {"id": "no_name"},
        {"id": 2024, "name": "Numeric Id Tariff"},
        {"id": " agile ", "name": "Padded Agile"},
        {"id": "numeric_name", "name": 1234},
        {"id": "blank_name", "name": "   "},
        {"id": "bad_config", "name": "Bad Config", "config": ["metric_battery_cycle"]},
        {"id": "empty_config", "name": "Empty Config", "config": None},
    ]
    kept = valid_compare_list(entries, log=logged.append)
    kept_ids = [entry["id"] for entry in kept]
    skipped_positions = sorted(int(msg.split("compare_list entry ")[1].split(" ")[0]) for msg in logged)

    if kept_ids != ["agile", "2024", "numeric_name", "empty_config"]:
        print("ERROR T27: valid_compare_list() should keep only 'agile', '2024', 'numeric_name' and 'empty_config' (ids as strings), got {}".format(kept_ids))
        failed += 1
    elif skipped_positions != [1, 2, 3, 5, 6, 8, 10, 11]:
        print("ERROR T27: valid_compare_list() should warn for entries 1, 2, 3, 5, 6, 8 (padded duplicate id), 10 (blank name) and 11 (non-mapping config), got {}".format(logged))
        failed += 1
    elif "config" in kept[3]:
        print("ERROR T27: an empty 'config:' (None) should be dropped as no overrides, got {}".format(kept[3]))
        failed += 1
    elif kept[2]["name"] != "1234":
        print("ERROR T27: a numeric name should become a string, or publish_data() can't build the friendly name, got {!r}".format(kept[2]["name"]))
        failed += 1
    elif kept[0] is entries[3] or entries[6]["id"] != 2024:
        print("ERROR T27: valid_compare_list() must return copies, not rewrite the user's compare_list in place")
        failed += 1
    elif valid_compare_list(None) != [] or valid_compare_list([{"id": 0, "name": "Zero"}]) != [{"id": "0", "name": "Zero"}]:
        print("ERROR T27: valid_compare_list() should accept a missing list and an id of 0")
        failed += 1
    elif check_compare_list("agile") != ([], ["compare_list must be a list of tariffs, each with an id and a name"]) or check_compare_list(5)[0] != []:
        print("ERROR T27: a compare_list that isn't a list should give no entries and one problem, got {} / {}".format(check_compare_list("agile"), check_compare_list(5)))
        failed += 1
    else:
        print("PASS T27: valid_compare_list() skips malformed entries with a positioned warning and normalises ids to strings")

    # ------------------------------------------------------------------
    # T28: run_all() skips an id-less compare_list entry and still compares the
    #      valid one after it - run against the real fixture with the expensive
    #      collaborators stubbed, so only the loop's own handling is exercised (#5472)
    # ------------------------------------------------------------------
    cmp = Compare.__new__(Compare)
    cmp.pb = my_predbat
    run_all_logged = []
    cmp.log = run_all_logged.append
    cmp.comparisons = {}
    cmp.live_saving_state = None
    run_single_ids = []
    saved_comparisons = []
    cmp.run_single = lambda tariff, *args, **kwargs: run_single_ids.append(tariff.get("id")) or {"date": "2026-10-11", "soc": 1.0, "soc_start": 1.0}
    cmp.select_best = lambda compare_list, results: None
    cmp.save_yaml = lambda: saved_comparisons.append(dict(cmp.comparisons))
    cmp.publish_data = lambda: None
    compare_list = [{"name": "No Id Tariff"}, "not_a_dict", {"id": "valid", "name": "Valid Tariff"}, {"id": "valid", "name": "Duplicate Tariff"}, {"id": 2024, "name": "Numeric Id Tariff"}]
    real_get_arg = my_predbat.get_arg

    def fake_get_arg(key, default=None, *args, **kwargs):
        """Return the test compare_list, deferring every other key to the real get_arg."""
        if key == "compare_list":
            return compare_list
        return real_get_arg(key, default, *args, **kwargs)

    try:
        with patch.object(my_predbat, "get_arg", side_effect=fake_get_arg):
            cmp.run_all(fetch_sensor=False)
        run_all_error = None
    except Exception as exc:
        run_all_error = exc

    if run_all_error is not None:
        print("ERROR T28: run_all() raised {!r} on a compare_list with malformed entries".format(run_all_error))
        failed += 1
    elif run_single_ids != ["valid", "2024"]:
        print("ERROR T28: only the valid tariffs should reach run_single(), ids as strings, got {}".format(run_single_ids))
        failed += 1
    elif not saved_comparisons or set(saved_comparisons[-1].keys()) != {"valid", "2024"}:
        print("ERROR T28: only the valid tariffs' results should be persisted, keyed by string id, got {}".format(saved_comparisons))
        failed += 1
    elif sorted(int(msg.split("compare_list entry ")[1].split(" ")[0]) for msg in run_all_logged if "skipping it" in msg) != [1, 2, 4]:
        print("ERROR T28: run_all() should log a warning for each skipped entry (1, 2 and 4), got {}".format(run_all_logged))
        failed += 1
    else:
        print("PASS T28: run_all() skips malformed entries and compares and persists the valid ones by string id")

    # ------------------------------------------------------------------
    # T29: run_comparison() clears compare_active even when run_all() raises -
    #      otherwise the flag stays on and the crashed comparison re-triggers
    #      itself every 5-minute cycle (#5472)
    # ------------------------------------------------------------------
    exposed = []
    real_expose_config = my_predbat.expose_config

    def record_expose_config(name, value, *args, **kwargs):
        """Record compare_active writes, then apply them for real."""
        if name == "compare_active":
            exposed.append(value)
        return real_expose_config(name, value, *args, **kwargs)

    prior_compare_active = my_predbat.get_arg("compare_active", False)
    raising_comparison = MagicMock()
    raising_comparison.run_all.side_effect = KeyError("id")
    raised = False
    with patch.object(my_predbat, "comparison", raising_comparison), patch.object(my_predbat, "expose_config", side_effect=record_expose_config):
        try:
            my_predbat.run_comparison()
        except KeyError:
            raised = True
    compare_active_after = my_predbat.get_arg("compare_active", False)
    # Shared fixture: put the flag back as the suite had it, so test order can't matter (#5079)
    my_predbat.expose_config("compare_active", prior_compare_active)

    if not raised:
        print("ERROR T29: run_comparison() should let run_all()'s exception propagate")
        failed += 1
    elif exposed != [True, False]:
        print("ERROR T29: compare_active should be set then cleared despite the exception, got {}".format(exposed))
        failed += 1
    elif compare_active_after:
        print("ERROR T29: compare_active is still on after run_all() raised")
        failed += 1
    else:
        print("PASS T29: run_comparison() clears compare_active even when run_all() raises")

    # ------------------------------------------------------------------
    # T30: load_yaml() turns a numeric result key saved before ids were
    #      normalised back into a string, so publish_data() can build the
    #      sensor name and the result still matches its compare_list entry (#5472)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    published = []
    pb.get_arg = lambda key, default=None: [{"id": 2024, "name": "Numeric Id Tariff"}] if key == "compare_list" else default
    cmp.dashboard_item = lambda entity_id, **kwargs: published.append(entity_id)
    with tempfile.TemporaryDirectory() as temp_dir:
        with open(os.path.join(temp_dir, "comparisons.yaml"), "w") as f:
            f.write("comparisons:\n  2024:\n    name: Numeric Id Tariff\n    cost: 1.0\n    metric: 1.0\n")
        cmp.config_root = temp_dir
        try:
            cmp.load_yaml()
            load_error = None
        except Exception as exc:
            load_error = exc

    if load_error is not None:
        print("ERROR T30: load_yaml() raised {!r} on a numeric result key".format(load_error))
        failed += 1
    elif list(cmp.comparisons.keys()) != ["2024"] or not cmp.comparisons["2024"].get("best"):
        print("ERROR T30: the numeric key should load as '2024' and be matched as the best tariff, got {}".format(cmp.comparisons))
        failed += 1
    elif published != ["predbat.compare_tariff_2024"]:
        print("ERROR T30: expected sensor predbat.compare_tariff_2024 to be published, got {}".format(published))
        failed += 1
    else:
        print("PASS T30: load_yaml() normalises a numeric result key to a string and publishes it")

    # ------------------------------------------------------------------
    # T31: publish_data() copes with a numeric name saved to comparisons.yaml
    #      before names were normalised - otherwise "Compare " + name raised
    #      TypeError inside Compare.__init__ and Predbat failed to start (#5472)
    # ------------------------------------------------------------------
    cmp, pb = _make_compare()
    published = {}
    cmp.dashboard_item = lambda entity_id, **kwargs: published.update({entity_id: kwargs.get("attributes", {})})
    cmp.comparisons = {"numeric_name": {"name": 2024, "cost": 1.0}}
    try:
        cmp.publish_data()
        publish_error = None
    except Exception as exc:
        publish_error = exc

    if publish_error is not None:
        print("ERROR T31: publish_data() raised {!r} on a saved numeric name".format(publish_error))
        failed += 1
    elif published.get("predbat.compare_tariff_numeric_name", {}).get("friendly_name") != "Compare 2024":
        print("ERROR T31: expected friendly name 'Compare 2024', got {}".format(published))
        failed += 1
    else:
        print("PASS T31: publish_data() publishes a saved numeric name as text")

    if failed:
        print("**** compare tests FAILED: {} errors ****\n".format(failed))
    else:
        print("**** compare tests PASSED ****\n")
    return failed

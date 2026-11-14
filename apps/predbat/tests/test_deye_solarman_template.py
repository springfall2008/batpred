# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Exercise the shared YAML through real native inverter and executor paths, without hardware."""

import copy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from config import INVERTER_DEF
from inverter import Inverter
from tests.test_infra import reset_inverter


def run_deye_solarman_template_tests(my_predbat):
    """Validate six-row charge/demand, failure barriers and the absence of unrelated writes."""
    template_path = Path(__file__).resolve().parents[3] / "templates" / "deye_solarman.yaml"
    with template_path.open() as stream:
        template = yaml.safe_load(stream)["pred_bat"]
    saved_definition = copy.deepcopy(INVERTER_DEF.get("DeyeSolarman"))
    saved_call = my_predbat.call_service_wrapper
    writes = []
    rejected = set()
    failed = False
    ha = my_predbat.ha_interface
    sources = ["select.test_deye_program_{}_charging".format(row) for row in range(1, 7)]
    targets = ["number.test_deye_program_{}_soc".format(row) for row in range(1, 7)]
    protected = {"time.test_deye_program_{}_time".format(row): "{:02d}:00:00".format((row - 1) * 4) for row in range(1, 7)}
    protected.update({"number.test_deye_program_{}_power".format(row): 3000 for row in range(1, 7)})
    protected.update(
        {
            "select.test_deye_work_mode": "Zero Export To CT",
            "select.test_deye_time_of_use": "Week",
            "number.test_deye_battery_max_charging_current": 59,
            "number.test_deye_battery_max_discharging_current": 160,
            "number.test_deye_battery_shutdown_soc": 5,
        }
    )

    def check(condition, message):
        """Collect assertions so one failing case does not hide the remaining evidence."""
        nonlocal failed
        if not condition:
            print("ERROR: Deye/Solarman template: " + message)
            failed = True

    def service_call(service, **data):
        """Model HA's grouped state-setting services, retaining entity attributes."""
        entities = data.get("entity_id", "").split(",")
        writes.append((service, copy.deepcopy(data)))
        if rejected.intersection(entities):
            return False
        for entity in entities:
            check(entity in sources + targets, "unexpected physical write to " + entity)
            if entity in sources + targets:
                ha.dummy_items[entity]["state"] = data.get("option", data.get("value"))
        return True

    def rows(source, target):
        """Check all six observations, never just the first row."""
        return all(ha.get_state(entity) == source for entity in sources) and all(float(ha.get_state(entity)) == target for entity in targets)

    try:
        reset_inverter(my_predbat)
        my_predbat.args.clear()
        my_predbat.args.update(template)
        my_predbat.args["deye_entity_prefix"] = "test_deye"
        my_predbat.args_from_apps_yaml = copy.deepcopy(my_predbat.args)
        my_predbat.last_service_hash = {}
        ha.dummy_items.update(protected)
        for entity in sources:
            ha.dummy_items[entity] = {"state": "Grid", "options": ["Disabled", "Grid", "Generator", "Both"]}
        for entity in targets:
            ha.dummy_items[entity] = {"state": 40, "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}
        for suffix, value in (("battery", 30), ("battery_voltage", 51.2), ("battery_temperature", 20), ("battery_power", 0), ("pv_power", 0), ("load_power", 500), ("grid_power", 500)):
            ha.dummy_items["sensor.test_deye_" + suffix] = value
        my_predbat.call_service_wrapper = service_call
        inv = Inverter(my_predbat, 0, quiet=True)
        inv.sleep = lambda seconds: None
        inv.inv_write_and_poll_sleep = 0
        inv.update_status(my_predbat.minutes_now)
        my_predbat.inverters = [inv]
        check(not writes, "construction must not send hardware services")
        check(template["template"] is True, "shipped example must stay in template mode")
        check(template.get("inverter_hybrid") is True and template.get("num_cars") == 0, "blank template must model a hybrid without enabling an EV")
        check(template.get("days_previous") == [7] and template.get("forecast_hours") == 48, "complete template must include load history and forecast horizon")
        check(template.get("currency_symbols") == ["£", "p"], "complete template must include editable currency symbols")
        check(not any(key in template for key in ("givtcp_rest", "deye_automatic", "ohme_control", "wallbox_control", "joint_site")), "blank local template must not activate another control path")
        check(not inv.inv_support_charge_freeze and not inv.inv_support_discharge_freeze and not inv.inv_has_timed_pause, "unsupported freeze/pause must remain disabled")
        check(inv.inv_output_charge_control == "none", "global rate controls must remain unmapped")
        check("discharge_start_service" not in template, "forced export must be absent")

        # Both window bookkeeping and rate/target updates are private sensors, not ToU rows.
        inv.adjust_charge_window(my_predbat.midnight_utc + timedelta(hours=23), my_predbat.midnight_utc + timedelta(hours=25), 23 * 60)
        inv.adjust_charge_rate(1000)
        inv.adjust_discharge_rate(0)
        inv.adjust_battery_target(80, isCharging=True)
        check(not writes, "native bookkeeping must not write physical time, current or SOC controls")
        for key in ("charge_start_time", "charge_end_time", "discharge_start_time", "discharge_end_time"):
            check(my_predbat.get_arg(key, indirect=False, index=0) in inv.created_attributes, "time bookkeeping must use created sensors")

        inv.adjust_charge_immediate(80)
        check(rows("Grid", 80), "charge must set all six targets and sources")
        check([data.get("option", data.get("value")) for _, data in writes] == ["Disabled", 80.0, "Grid"], "charge must observe disable → SOC → enable")
        writes.clear()
        inv.adjust_charge_immediate(80)
        inv.adjust_export_immediate(100)
        check(not writes and rows("Grid", 80), "repeated charge and idle export hook must not clobber a charge")

        inv.adjust_charge_immediate(0)
        check(rows("Disabled", 10), "demand must disable charging and restore the domestic floor, not shutdown")
        my_predbat.expose_config("set_reserve_min", 25)
        inv.refresh_config()
        inv.adjust_charge_immediate(0)
        check(rows("Disabled", 25), "demand must follow the current manual domestic minimum")

        writes.clear()
        rejected.add(targets[-1])
        inv.adjust_charge_immediate(90)
        check(not any(data.get("option") == "Grid" for _, data in writes), "failed last target must prevent source enable")
        rejected.clear()
        inv.adjust_charge_immediate(90)
        check(rows("Grid", 90), "next cycle must retry a failed sequence")

        # Native reset writes 100 to its private target, never to the physical ToU targets.
        my_predbat.set_read_only = False
        my_predbat.set_charge_window = True
        my_predbat.set_export_window = False
        my_predbat.reset_inverter()
        check(rows("Disabled", 25), "native reset must leave demand, not grid-charge to 100")

        # Drive an active native plan, including the trailing discharge-stop hook and rates.
        my_predbat.args["template"] = False
        my_predbat.inverter_hybrid = True
        my_predbat.set_soc_enable = True
        my_predbat.set_reserve_enable = False
        my_predbat.set_charge_freeze = False
        my_predbat.set_charge_low_power = False
        my_predbat.set_discharge_during_charge = True
        my_predbat.inverter_needs_reset = False
        my_predbat.fetch_inverter_data()
        inv = my_predbat.inverters[0]
        inv.sleep = lambda seconds: None
        my_predbat.charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
        my_predbat.charge_limit_best = [8]
        my_predbat.charge_limit_percent_best = [80]
        my_predbat.execute_plan()
        check(rows("Grid", 80), "native active charge plan must reach all six rows")
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.execute_plan()
        check(rows("Disabled", 25), "native demand plan must stop grid charging")
        writes.clear()
        my_predbat.set_read_only = True
        my_predbat.execute_plan()
        check(not writes, "read-only native plan must not dispatch services")
        my_predbat.set_read_only = False
        my_predbat.set_charge_window = False
        my_predbat.set_export_window = False
        my_predbat.set_soc_enable = False
        my_predbat.execute_plan()
        check(not writes, "Monitor must not dispatch services")
        check(all(ha.get_state(entity) == value for entity, value in protected.items()), "all unmapped installation settings must stay untouched")

        # A transport ValueError is not one of our local diagnostics. Never echo its body.
        with patch.object(my_predbat, "call_service_wrapper", side_effect=ValueError("private-response-marker")), patch.object(inv, "log") as log, patch.object(my_predbat, "record_status") as status:
            check(inv.call_service_template("charge_start_service", {"target_soc": 95}) is True, "a transport exception must remain handled")
            check(status.called, "transport exception must record an error")
            check("private-response-marker" not in str(log.call_args_list) + str(status.call_args_list), "transport response must not leak into diagnostics")
    finally:
        my_predbat.call_service_wrapper = saved_call
        if saved_definition is None:
            INVERTER_DEF.pop("DeyeSolarman", None)
        else:
            INVERTER_DEF["DeyeSolarman"] = saved_definition
    print("Deye/Solarman template tests: {}".format("FAILED" if failed else "PASSED"))
    return failed

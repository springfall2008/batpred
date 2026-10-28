# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk parallel (master/slave) inverter groups
# -----------------------------------------------------------------------------

"""Tests for Sunsynk parallel master/slave groups (GH#5594).

The figures are taken from a live 2 x 3.6 kW parallel system on one shared 400 Ah bank,
with serials and the plant id anonymised.
"""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from datetime import datetime, timedelta
from unittest.mock import PropertyMock, patch
import pytz
import sunsynk
from tests.discovery_contract import assert_record_agrees, assert_record_binds_nothing_extra, capture_automatic_config, validated_inverters
from tests.test_sunsynk_config import RunSunsynk, _fake_token, _live_plan_entities
from tests.test_sunsynk_publish import PublishingSunsynk
from tests.test_infra import run_async as run_async_local

MASTER = "2200000001"
SLAVE = "2200000002"
PLANT = 400001

LONDON = pytz.timezone("Europe/London")


def _clock(s, when):
    """Freeze the component's local clock (ComponentBase.now_utc_exact) at when."""
    return patch.object(type(s), "now_utc_exact", new_callable=PropertyMock, return_value=when)


def _infos(*units):
    """Build /api/v1/inverters entries from (serial, equipMode, plant id) tuples."""
    return [{"sn": sn, "equipMode": mode, "plant": {"id": plant, "type": 2, "master": None}} for sn, mode, plant in units]


def _list_get(infos):
    """Return a fake _get serving only the inverter list."""

    async def fake_get(endpoint_key, sn=None, params=None):
        """Serve one page of the inverter list."""
        if endpoint_key == "inverter_list":
            return {"total": len(infos), "infos": infos}
        return {}

    return fake_get


def _parallel_pair(s=None):
    """A component holding the live master/slave pair as one poll left them."""
    s = s or PublishingSunsynk()
    s.device_list = [MASTER, SLAVE]
    s.device_roles = {MASTER: {"equip_mode": "M1", "plant": str(PLANT)}, SLAVE: {"equip_mode": "S2", "plant": str(PLANT)}}
    # 18 Oct ~10:00: the master charging from its own PV; the slave's battery block is garbage.
    s.device_values[MASTER] = {
        "soc": 80,
        "battery_power": -1220.0,
        "battery_voltage": 54.1,
        "temperature": 29.0,
        "grid_power": 6.0,
        "load_power": 306.0,
        "pv_power": 1562.0,
        "capacity": 400.0,
        "chargeVolt": 58.0,
        "chargeCurrentLimit": 200.0,
        "maxChargeCurrentLimit": 0.0,
        "bmsCurrent": 23.0,
        "bmsVolt": 53.54,
        "bmsSoc": 80.0,
    }
    s.device_values[SLAVE] = {
        "soc": 80,
        "battery_power": 6007.0,
        "battery_voltage": 54.0,
        "temperature": 0.0,
        "grid_power": -2.0,
        "load_power": 305.0,
        "pv_power": 504.0,
        "capacity": 400.0,
        "chargeVolt": 0.0,
        "chargeCurrentLimit": 0.0,
        "maxChargeCurrentLimit": 0.0,
        "bmsCurrent": 0.0,
        "bmsVolt": 0.0,
        "bmsSoc": 0.0,
    }
    s.device_energy[MASTER] = {"pv_today": 1.1, "import_today": 21.1, "export_today": 4.7, "load_today": 10.1, "battery_charge_today": 12.7, "battery_discharge_today": 6.2}
    s.device_energy[SLAVE] = {"pv_today": 0.4, "import_today": 21.2, "export_today": 0.0, "load_today": 10.2, "battery_charge_today": 0.0, "battery_discharge_today": 57.8}
    s.device_settings[MASTER] = {"sn": MASTER, "equipMode": "1", "modbusSn": "1", "parallel": "1", "pvMaxLimit": "4600", "importPower": "10350", "batteryLowCap": "15", "batteryMaxCurrentCharge": "65", "chargeCurrentLimit": "200"}
    s.device_settings[SLAVE] = {"sn": SLAVE, "equipMode": "0", "modbusSn": "2", "parallel": "1", "pvMaxLimit": "4600", "importPower": "10350", "batteryLowCap": "15", "batteryMaxCurrentCharge": "65", "chargeCurrentLimit": "0"}
    s.device_rated_power = {MASTER: 3600.0, SLAVE: 3600.0}
    return s


def test_parallel_group_detected_from_the_inverter_list():
    """equipMode M1/S2 within one plant makes one group, driven through the master."""
    failed = False
    s = PublishingSunsynk()
    with patch.object(s, "_get", side_effect=_list_get(_infos((SLAVE, "S2", PLANT), (MASTER, "M1", PLANT)))):
        devices = run_async_local(s.get_device_list())
    if sorted(devices) != sorted([MASTER, SLAVE]):
        print(f"ERROR: both units must be polled, got {devices}")
        failed = True
    if s.parallel_groups() != {MASTER: [SLAVE]}:
        print(f"ERROR: expected one group under the master, got {s.parallel_groups()}")
        failed = True
    if s.inverter_list != [MASTER]:
        print(f"ERROR: only the master is a Predbat inverter, got {s.inverter_list}")
        failed = True
    return failed


def test_parallel_serial_filter_on_the_master_pulls_in_its_slave():
    """Configuring the master's serial alone must still poll the slave, or its grid and PV flows are lost."""
    failed = False
    s = PublishingSunsynk(inverter_sn=[MASTER])
    other = "2200000099"
    with patch.object(s, "_get", side_effect=_list_get(_infos((MASTER, "M1", PLANT), (SLAVE, "S2", PLANT), (other, "M1", 400002)))):
        devices = run_async_local(s.get_device_list())
    if devices != [MASTER, SLAVE]:
        print(f"ERROR: expected the master and its slave only, got {devices}")
        failed = True
    if s.inverter_list != [MASTER]:
        print(f"ERROR: expected the master as the only inverter, got {s.inverter_list}")
        failed = True
    return failed


def test_parallel_single_inverter_is_unchanged():
    """A standalone inverter, with or without an equipMode, stays a plain single inverter."""
    failed = False
    for infos in ([{"sn": "INV1"}], _infos(("INV1", "M1", PLANT))):
        s = PublishingSunsynk()
        with patch.object(s, "_get", side_effect=_list_get(infos)):
            run_async_local(s.get_device_list())
        if s.parallel_groups() != {} or s.inverter_list != ["INV1"]:
            print(f"ERROR: {infos} should be one standalone inverter, got groups {s.parallel_groups()} inverters {s.inverter_list}")
            failed = True
    return failed


def test_parallel_two_groups_in_two_plants():
    """Two parallel groups on one account are two Predbat inverters, each with its own slave."""
    failed = False
    s = PublishingSunsynk()
    m2, s2 = "2200000011", "2200000012"
    with patch.object(s, "_get", side_effect=_list_get(_infos((MASTER, "M1", PLANT), (SLAVE, "S2", PLANT), (m2, "M1", 400002), (s2, "S2", 400002)))):
        run_async_local(s.get_device_list())
    if s.parallel_groups() != {MASTER: [SLAVE], m2: [s2]}:
        print(f"ERROR: expected two groups, got {s.parallel_groups()}")
        failed = True
    if s.inverter_list != [MASTER, m2]:
        print(f"ERROR: expected both masters as inverters, got {s.inverter_list}")
        failed = True
    return failed


def test_parallel_two_masters_in_one_plant():
    """Two masters in one plant are both driven; the slave goes to the one whose settings say parallel, else to neither."""
    failed = False
    standalone = "2200000021"
    s = _parallel_pair()
    s.device_list = [MASTER, SLAVE, standalone]
    s.device_roles[standalone] = {"equip_mode": "M1", "plant": str(PLANT)}
    s.device_settings[standalone] = {"sn": standalone, "parallel": "0", "equipMode": "1"}
    if s.parallel_groups() != {MASTER: [SLAVE]}:
        print(f"ERROR: the slave belongs to the master in parallel mode, got {s.parallel_groups()}")
        failed = True
    if s.inverter_list != [MASTER, standalone]:
        print(f"ERROR: both masters must be inverters, got {s.inverter_list}")
        failed = True

    # Both masters in parallel mode: nothing says which one the slave follows.
    s.device_settings[standalone]["parallel"] = "1"
    if s.parallel_groups() != {}:
        print(f"ERROR: an ambiguous slave must not be paired, got {s.parallel_groups()}")
        failed = True
    if s.inverter_list != [MASTER, standalone]:
        print(f"ERROR: an unpaired slave must not become an inverter, got {s.inverter_list}")
        failed = True
    if not any("could not be paired" in message for message in s.log_messages):
        print(f"ERROR: an unpaired slave must be warned about, got {s.log_messages}")
        failed = True
    return failed


def test_parallel_roles_fall_back_to_the_settings_read():
    """With no equipMode in the list, the settings read's parallel/equipMode flags identify the roles."""
    failed = False
    s = _parallel_pair()
    s.device_roles = {MASTER: {"equip_mode": "", "plant": str(PLANT)}, SLAVE: {"equip_mode": "", "plant": str(PLANT)}}
    if s.parallel_groups() != {MASTER: [SLAVE]}:
        print(f"ERROR: settings equipMode 1/0 with parallel 1 should pair the units, got {s.parallel_groups()}")
        failed = True
    return failed


def test_parallel_slave_with_its_own_battery_is_independent():
    """A slave with its own BMS link has its own battery, so it stays a separate inverter."""
    failed = False
    s = _parallel_pair()
    s.device_values[SLAVE].update({"bmsSoc": 64.0, "bmsVolt": 52.1, "bmsCurrent": -10.0})
    if s.parallel_groups() != {}:
        print(f"ERROR: a slave with its own BMS must not be grouped, got {s.parallel_groups()}")
        failed = True
    if s.inverter_list != [MASTER, SLAVE]:
        print(f"ERROR: both units must be inverters, got {s.inverter_list}")
        failed = True
    return failed


def test_parallel_filter_leaves_out_a_slave_with_its_own_battery():
    """A slave the master's filter pulled in is polled, but one with its own battery is not driven unless it is named."""
    failed = False
    s = PublishingSunsynk(inverter_sn=[MASTER])
    with patch.object(s, "_get", side_effect=_list_get(_infos((MASTER, "M1", PLANT), (SLAVE, "S2", PLANT)))):
        run_async_local(s.get_device_list())
    pair = _parallel_pair()
    s.device_values = pair.device_values
    s.device_values[SLAVE].update({"bmsSoc": 64.0, "bmsVolt": 52.1, "bmsCurrent": -10.0})
    if s.inverter_list != [MASTER]:
        print(f"ERROR: an unnamed slave with its own battery must not be driven, got {s.inverter_list}")
        failed = True

    # Naming it as well makes it a second inverter, as before the filter grew the group.
    s = PublishingSunsynk(inverter_sn=[MASTER, SLAVE])
    with patch.object(s, "_get", side_effect=_list_get(_infos((MASTER, "M1", PLANT), (SLAVE, "S2", PLANT)))):
        run_async_local(s.get_device_list())
    s.device_values = pair.device_values
    if s.inverter_list != [MASTER, SLAVE]:
        print(f"ERROR: a named slave with its own battery is its own inverter, got {s.inverter_list}")
        failed = True
    return failed


def test_parallel_filter_warns_when_the_list_has_no_roles():
    """With a filter set and no equipMode in the list, a same-plant unit left out may be a slave, so it is warned about."""
    failed = False
    no_roles = [{"sn": MASTER, "plant": {"id": PLANT}}, {"sn": SLAVE, "plant": {"id": PLANT}}]
    s = PublishingSunsynk(inverter_sn=[MASTER])
    with patch.object(s, "_get", side_effect=_list_get(no_roles)):
        devices = run_async_local(s.get_device_list())
    if devices != [MASTER]:
        print(f"ERROR: without roles only the named serial is polled, got {devices}")
        failed = True
    if not any("no parallel roles" in message and SLAVE in message for message in s.log_messages):
        print(f"ERROR: the left-out same-plant unit must be warned about, got {s.log_messages}")
        failed = True

    # A unit in another plant cannot be this master's slave: no warning.
    s = PublishingSunsynk(inverter_sn=[MASTER])
    with patch.object(s, "_get", side_effect=_list_get([{"sn": MASTER, "plant": {"id": PLANT}}, {"sn": SLAVE, "plant": {"id": 400002}}])):
        run_async_local(s.get_device_list())
    if any("no parallel roles" in message for message in s.log_messages):
        print(f"ERROR: a unit in another plant must not be warned about, got {s.log_messages}")
        failed = True
    return failed


def test_parallel_group_values():
    """Grid, PV and load are summed; SoC and temperature come from the master; battery power from the BMS."""
    failed = False
    s = _parallel_pair()
    values = s.group_values(MASTER)
    expected = {"soc": 80, "battery_voltage": 54.1, "temperature": 29.0, "grid_power": 4.0, "load_power": 611.0, "pv_power": 2066.0, "battery_power": -1244.0}
    for leaf, want in expected.items():
        if values.get(leaf) != want:
            print(f"ERROR: group {leaf} {values.get(leaf)!r}, expected {want!r}")
            failed = True
    # A standalone inverter's values are its own, untouched.
    s.device_list = ["INV1"]
    s.device_roles = {}
    s.device_values["INV1"] = {"battery_power": -500.0, "bmsCurrent": 99.0, "battery_voltage": 50.0}
    if s.group_values("INV1") != s.device_values["INV1"]:
        print(f"ERROR: a standalone inverter's values must be unchanged, got {s.group_values('INV1')}")
        failed = True
    return failed


def test_parallel_battery_power_follows_the_bms_sign():
    """bmsCurrent is positive while charging, so battery power is its negation (positive = discharge)."""
    failed = False
    s = _parallel_pair()
    # 15 Oct 20:00, the bank discharging: power +3326 W on the master, bmsCurrent -133 A.
    s.device_values[MASTER].update({"battery_power": 3326.0, "bmsCurrent": -133.0, "battery_voltage": 51.1})
    if s.group_values(MASTER).get("battery_power") != 6796.0:
        print(f"ERROR: discharging bank should read +6796 W, got {s.group_values(MASTER).get('battery_power')}")
        failed = True
    return failed


def test_parallel_battery_power_without_a_bms_link_falls_back_to_the_master():
    """With no BMS reading the master's own power is used, never the slave's, and the shortfall is warned once."""
    failed = False
    s = _parallel_pair()
    for field in ("bmsCurrent", "bmsVolt", "bmsSoc"):
        s.device_values[MASTER].pop(field)
    for _ in range(2):
        power = s.group_values(MASTER).get("battery_power")
    if power != -1220.0:
        print(f"ERROR: expected the master's own -1220 W, got {power}")
        failed = True
    warnings = [message for message in s.log_messages if "bmsCurrent" in message]
    if len(warnings) != 1:
        print(f"ERROR: expected one warning about the missing BMS reading, got {warnings}")
        failed = True
    return failed


def test_parallel_group_value_missing_on_one_unit_is_withheld():
    """A summed value one unit did not report is left out rather than published as the master's share."""
    failed = False
    s = _parallel_pair()
    s.device_values[SLAVE].pop("pv_power")
    s.device_energy[SLAVE].pop("import_today")
    if "pv_power" in s.group_values(MASTER):
        print(f"ERROR: pv_power must be withheld, got {s.group_values(MASTER).get('pv_power')}")
        failed = True
    if "import_today" in s.group_energy(MASTER):
        print(f"ERROR: import_today must be withheld, got {s.group_energy(MASTER).get('import_today')}")
        failed = True
    return failed


def test_parallel_group_energy_and_balance():
    """Daily counters are summed and the battery's comes from the integrator; the sums close the energy balance.

    18 Oct 00:00-09:46: import ~ load + net battery (AC) - PV + export = 20.3 + 13.0 - 1.5 + 4.7 = 36.5 kWh.
    The summed import (42.3 kWh) is close; the master's alone (21.1 kWh) is not.
    """
    failed = False
    s = _parallel_pair()
    now = LONDON.localize(datetime(2026, 10, 18, 9, 46))
    s.battery_energy[MASTER] = {"date": "2026-10-18", "charge": 19.2, "discharge": 6.2, "time": now.isoformat(), "power": -1244.0}
    with _clock(s, now):
        energy = s.group_energy(MASTER)
    expected = {"pv_today": 1.5, "import_today": 42.3, "export_today": 4.7, "load_today": 20.3, "battery_charge_today": 19.2, "battery_discharge_today": 6.2}
    for leaf, want in expected.items():
        if energy.get(leaf) != want:
            print(f"ERROR: group {leaf} {energy.get(leaf)!r}, expected {want!r}")
            failed = True
    balance = energy["load_today"] + (energy["battery_charge_today"] - energy["battery_discharge_today"]) - energy["pv_today"] + energy["export_today"]
    if abs(energy["import_today"] - balance) > 0.2 * balance:
        print(f"ERROR: summed import {energy['import_today']} should be within 20% of the balance {balance:.1f}")
        failed = True
    if abs(s.device_energy[MASTER]["import_today"] - balance) <= 0.2 * balance:
        print("ERROR: the fixture no longer separates the master-only import from the balance")
        failed = True
    return failed


def test_parallel_battery_integrator():
    """Trapezoidal integration, split at a sign change, skipping gaps and restarting at local midnight."""
    failed = False
    s = _parallel_pair()
    start = LONDON.localize(datetime(2026, 10, 18, 2, 0))

    s._integrate_group_battery(MASTER, -6000.0, start)
    s._integrate_group_battery(MASTER, -6000.0, start + timedelta(minutes=5))
    state = s.battery_energy[MASTER]
    if round(state["charge"], 3) != 0.5 or state["discharge"] != 0.0:
        print(f"ERROR: 6 kW for 5 minutes is 0.5 kWh charged, got {state}")
        failed = True

    # -6000 W to +6000 W over 10 minutes crosses zero half-way: 0.25 kWh each side.
    s._integrate_group_battery(MASTER, 6000.0, start + timedelta(minutes=15))
    if round(state["charge"], 3) != 0.75 or round(state["discharge"], 3) != 0.25:
        print(f"ERROR: a sign change should split the interval, got {state}")
        failed = True

    # An outage longer than the gap limit is not filled with a stale reading.
    s._integrate_group_battery(MASTER, 6000.0, start + timedelta(hours=2))
    if round(state["discharge"], 3) != 0.25:
        print(f"ERROR: a long gap must not be integrated, got {state}")
        failed = True

    # Across local midnight the day restarts, keeping only the part after midnight.
    late = LONDON.localize(datetime(2026, 10, 18, 23, 58))
    s._integrate_group_battery(MASTER, 6000.0, late)
    s._integrate_group_battery(MASTER, 6000.0, late + timedelta(minutes=5))
    state = s.battery_energy[MASTER]
    if state["date"] != "2026-10-19" or round(state["discharge"], 3) != 0.3 or state["charge"] != 0.0:
        print(f"ERROR: expected a fresh day with 3 minutes at 6 kW (0.3 kWh), got {state}")
        failed = True

    # Yesterday's totals are not published as today's.
    with _clock(s, LONDON.localize(datetime(2026, 10, 20, 0, 1))):
        energy = s.group_energy(MASTER)
    if energy.get("battery_discharge_today") != 0.0:
        print(f"ERROR: a stale day must publish 0, got {energy.get('battery_discharge_today')}")
        failed = True
    return failed


def test_parallel_ratings():
    """Capacity and floor from the master once; rating and export cap summed per unit; charge rate held by the per-unit current cap."""
    failed = False
    s = _parallel_pair()
    checks = {
        "battery_capacity": (s.battery_capacity(MASTER), 20.48),
        "inverter_limit": (s.inverter_limit(MASTER), 7200.0),
        # Each unit exports up to min(pvMaxLimit 4600, rating 3600).
        "export_limit": (s.export_limit(MASTER), 7200.0),
        # min(BMS 200 A, 2 x 65 A) x 51.2 V nominal.
        "battery_rate_max": (s.battery_rate_max(MASTER), 6656.0),
        "battery_reserve_min": (s.battery_reserve_min(MASTER), 15),
    }
    for name, (got, want) in checks.items():
        if round(got, 3) != want:
            print(f"ERROR: group {name} {got}, expected {want}")
            failed = True
    # A unit whose rating is unknown makes the group's unknown, rather than understated.
    del s.device_rated_power[SLAVE]
    if s.inverter_limit(MASTER) != 0.0:
        print(f"ERROR: a missing slave rating should leave inverter_limit unknown, got {s.inverter_limit(MASTER)}")
        failed = True
    return failed


def test_parallel_automatic_config_registers_one_inverter():
    """automatic_config() binds one SunsynkCloud inverter, on the master's entities only."""
    failed = False
    s = _parallel_pair()
    s.battery_energy[MASTER] = {"date": "2026-10-18", "charge": 1.0, "discharge": 1.0, "time": LONDON.localize(datetime(2026, 10, 18, 9, 0)).isoformat(), "power": 0.0}
    with _clock(s, LONDON.localize(datetime(2026, 10, 18, 9, 5))):
        captured = capture_automatic_config(s)
    if captured.get("num_inverters") != 1 or captured.get("inverter_type") != ["SunsynkCloud"]:
        print(f"ERROR: expected one SunsynkCloud inverter, got {captured.get('num_inverters')} {captured.get('inverter_type')}")
        failed = True
    for setting, value in captured.items():
        if isinstance(value, list) and any(SLAVE in str(item) for item in value):
            print(f"ERROR: {setting} is bound to the slave: {value}")
            failed = True
    for setting in ("soc_max", "battery_rate_max", "inverter_limit", "export_limit", "import_today", "export_today", "battery_charge_today", "battery_discharge_today", "schedule_write_button"):
        if setting not in captured:
            print(f"ERROR: {setting} was not bound")
            failed = True
    return failed


def test_parallel_discovery_record():
    """build_discovery() files one record per group, listing every member serial and agreeing with automatic_config()."""
    failed = False
    s = _parallel_pair()
    with _clock(s, LONDON.localize(datetime(2026, 10, 18, 9, 5))):
        records = validated_inverters(s.build_discovery())
        captured = capture_automatic_config(s)
    if [record["device_id"] for record in records] != [f"sunsynk:{MASTER}"]:
        print(f"ERROR: expected one record for the master, got {[record['device_id'] for record in records]}")
        return True
    if records[0].get("serials") != [MASTER, SLAVE]:
        print(f"ERROR: the record should list both serials, got {records[0].get('serials')}")
        failed = True
    if records[0]["ratings"].get("inverter_limit") != 7200.0:
        print(f"ERROR: the record's inverter_limit should be the group's, got {records[0]['ratings']}")
        failed = True
    assert_record_agrees(records[0], captured, index=0)
    assert_record_binds_nothing_extra(records[0], captured, index=0)
    return failed


def test_parallel_publish():
    """The master's sensors carry the group; the slave's raw readings are published for diagnosis, marked as such."""
    failed = False
    s = _parallel_pair()
    with _clock(s, LONDON.localize(datetime(2026, 10, 18, 9, 5))):
        run_async_local(s.publish_data())
    if s.published.get(s._sensor_name(MASTER, "grid_power"), {}).get("state") != 4.0:
        print(f"ERROR: the master's grid_power should be the group sum, got {s.published.get(s._sensor_name(MASTER, 'grid_power'))}")
        failed = True
    if s.published.get(s._sensor_name(MASTER, "import_today"), {}).get("state") != 42.3:
        print(f"ERROR: the master's import_today should be the group sum, got {s.published.get(s._sensor_name(MASTER, 'import_today'))}")
        failed = True
    slave_export = s.published.get(s._sensor_name(SLAVE, "export_today"))
    if not slave_export or slave_export["state"] != 0.0 or slave_export["attributes"].get("parallel_master") != MASTER:
        print(f"ERROR: the slave's raw export_today should be published and marked, got {slave_export}")
        failed = True
    for leaf in ("battery_capacity", "battery_rate_max", "inverter_limit", "export_limit"):
        if s._sensor_name(SLAVE, leaf) in s.published:
            print(f"ERROR: the slave must not publish a {leaf} rating")
            failed = True
    return failed


def test_parallel_run_drives_the_master_only():
    """End to end: one inverter is configured, the slave gets no control entities, and writes reach only the master."""
    failed = False
    s = RunSunsynk(control_enable=True)
    s.automatic = True
    _live_plan_entities(s, MASTER)
    posts = []
    fake_get, fake_post = _pair_transport(posts)

    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        run_async_local(s.run(0, True))
        run_async_local(s.switch_event(s._control_name("switch", MASTER, "battery_schedule_charge_write"), "turn_on"))
        run_async_local(s.run(0, False))
    if s.args_set.get("num_inverters") != 1:
        print(f"ERROR: expected one inverter, got {s.args_set.get('num_inverters')}")
        failed = True
    if any(SLAVE.lower() in entity and "battery_schedule" in entity for entity in s.published):
        print("ERROR: the slave must not get control entities")
        failed = True
    if not posts or any(sn != MASTER for _, sn, _ in posts):
        print(f"ERROR: writes must reach the master only, got {[(key, sn) for key, sn, _ in posts]}")
        failed = True
    if s._sn_from_entity(f"switch.predbat_sunsynk_{SLAVE}_battery_schedule_charge_write") is not None:
        print("ERROR: a slave entity must not route to an inverter")
        failed = True
    if s.published.get(s._sensor_name(MASTER, "pv_power"), {}).get("state") != 2066.0:
        print(f"ERROR: the master's pv_power should be the group sum, got {s.published.get(s._sensor_name(MASTER, 'pv_power'))}")
        failed = True
    return failed


def test_parallel_first_cycle_waits_for_the_slaves_battery_reading():
    """Startup waits for a slave's battery reading, which decides whether it is grouped; a slave that never answers is given up on."""
    failed = False
    slave_up = [False]
    s = RunSunsynk()
    s.automatic = True
    fake_get, fake_post = _pair_transport([], slave_battery=lambda: slave_up[0])
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        if run_async_local(s.run(0, True)) is not False:
            print("ERROR: startup must wait while the slave's battery reading is missing")
            failed = True
        if "num_inverters" in s.args_set:
            print(f"ERROR: automatic_config() must not run before the slave is read, got {s.args_set.get('num_inverters')}")
            failed = True
        if not any("waiting for parallel slave" in message for message in s.log_messages):
            print(f"ERROR: the wait must be logged, got {s.log_messages}")
            failed = True
        slave_up[0] = True
        if run_async_local(s.run(0, True)) is not True:
            print("ERROR: startup must complete once the slave answers")
            failed = True
    if s.args_set.get("num_inverters") != 1:
        print(f"ERROR: expected one inverter once the slave is read, got {s.args_set.get('num_inverters')}")
        failed = True

    # A slave that never answers must not keep the master uncontrolled for ever.
    s = RunSunsynk()
    s.automatic = True
    fake_get, fake_post = _pair_transport([], slave_battery=lambda: False)
    results = []
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        for _ in range(sunsynk.SUNSYNK_SLAVE_STARTUP_RETRIES + 1):
            results.append(run_async_local(s.run(0, True)))
    if results != [False] * sunsynk.SUNSYNK_SLAVE_STARTUP_RETRIES + [True]:
        print(f"ERROR: expected {sunsynk.SUNSYNK_SLAVE_STARTUP_RETRIES} waits and then a start, got {results}")
        failed = True
    if s.args_set.get("num_inverters") != 1:
        print(f"ERROR: the master must be configured after giving up on the slave, got {s.args_set.get('num_inverters')}")
        failed = True
    if not any("treated as sharing" in message for message in s.log_messages):
        print(f"ERROR: giving up on the slave must be logged, got {s.log_messages}")
        failed = True

    # Without automatic config nothing is bound once, so inverter_list corrects itself and startup does not wait.
    s = RunSunsynk()
    fake_get, fake_post = _pair_transport([], slave_battery=lambda: False)
    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "_post", side_effect=fake_post), patch.object(s, "fetch_token", side_effect=_fake_token):
        if run_async_local(s.run(0, True)) is not True:
            print("ERROR: startup must not wait for a slave when automatic config is off")
            failed = True
    return failed


def _pair_transport(posts, slave_battery=None):
    """Return (fake_get, fake_post) serving the live pair; slave_battery() False makes the slave's battery read fail."""
    master_settings = {
        "sn": MASTER,
        "sysWorkMode": "2",
        "peakAndVallery": "0",
        "batteryLowCap": "15",
        "sellTime1": "01:00",
        "cap1": "80",
        "time1on": True,
        "mondayOn": False,
        "parallel": "1",
        "equipMode": "1",
        "pvMaxLimit": "4600",
        "batteryMaxCurrentCharge": "65",
    }

    async def fake_get(endpoint_key, sn=None, params=None):
        """Serve the pair as the live API returned it."""
        if endpoint_key == "inverter_list":
            return {"total": 2, "infos": _infos((MASTER, "M1", PLANT), (SLAVE, "S2", PLANT))}
        if endpoint_key == "inverter_detail":
            return {"ratePower": 3600}
        if endpoint_key == "battery":
            if sn == SLAVE and slave_battery is not None and not slave_battery():
                return {}
            if sn == MASTER:
                return {"soc": 80, "power": -1220, "voltage": 54.1, "capacity": 400, "chargeVolt": 58.0, "chargeCurrentLimit": 200, "bmsCurrent": 23.0, "bmsVolt": 53.5, "bmsSoc": 80.0, "etodayChg": 12.7, "etodayDischg": 6.2}
            return {"soc": 80, "power": 6007, "voltage": 54.0, "capacity": 400, "chargeVolt": 0.0, "chargeCurrentLimit": 0, "bmsCurrent": 0.0, "bmsVolt": 0.0, "bmsSoc": 0.0, "etodayChg": 0.0, "etodayDischg": 57.8}
        if endpoint_key == "grid":
            return {"pac": -6, "etodayFrom": 21.1, "etodayTo": 4.7} if sn == MASTER else {"pac": 2, "etodayFrom": 21.2, "etodayTo": 0.0}
        if endpoint_key == "load":
            return {"totalPower": 306 if sn == MASTER else 305, "dailyUsed": 10.1 if sn == MASTER else 10.2}
        if endpoint_key == "input":
            return {"pac": 1562, "etoday": 1.1} if sn == MASTER else {"pac": 504, "etoday": 0.4}
        if endpoint_key == "settings_read":
            settings = dict(master_settings)
            if sn == SLAVE:
                settings.update({"sn": SLAVE, "equipMode": "0"})
            return settings
        return {}

    async def fake_post(endpoint_key, sn=None, body=None):
        """Record every write."""
        posts.append((endpoint_key, sn, body))
        return {}

    return fake_get, fake_post


def test_parallel_roles_and_battery_energy_survive_a_restart():
    """The roles are cached with the device list, and a cache without them forces rediscovery rather than an unpaired start."""
    failed = False
    s = _parallel_pair()
    saved = {}

    async def fake_save(name, data):
        """Capture what would be written to storage."""
        saved[name] = data

    async def fake_load(name):
        """Serve back what was saved."""
        return saved.get(name, {})

    async def fake_age(name):
        """Report every cache as one minute old."""
        return 1.0 if name in saved else None

    s.battery_energy[MASTER] = {"date": "2026-10-18", "charge": 3.0, "discharge": 1.0, "time": LONDON.localize(datetime(2026, 10, 18, 9, 0)).isoformat(), "power": -500.0}
    with patch.object(s, "save_cache", side_effect=fake_save):
        run_async_local(s.save_static())
        run_async_local(s.save_battery_energy())

    restored = PublishingSunsynk()
    with patch.object(restored, "load_cache", side_effect=fake_load), patch.object(restored, "age_cache", side_effect=fake_age):
        run_async_local(restored.restore_state())
    if restored.device_roles != s.device_roles:
        print(f"ERROR: roles not restored, got {restored.device_roles}")
        failed = True
    if restored.battery_energy.get(MASTER, {}).get("charge") != 3.0:
        print(f"ERROR: battery energy not restored, got {restored.battery_energy}")
        failed = True
    if restored.tier_expired("static", 60):
        print("ERROR: a cache with roles should seed the static tier")
        failed = True

    # A cache written before roles existed must not be trusted to describe the groups.
    saved["static"] = {"device_list": [MASTER, SLAVE], "device_detail": {}}
    legacy = PublishingSunsynk()
    with patch.object(legacy, "load_cache", side_effect=fake_load), patch.object(legacy, "age_cache", side_effect=fake_age):
        run_async_local(legacy.restore_state())
    if not legacy.tier_expired("static", 60):
        print("ERROR: a cache without roles must leave the static tier expired so discovery re-runs")
        failed = True
    return failed


def test_parallel_slave_settings_are_not_reported_as_external_changes():
    """The master copies its settings to the slave, so a slave change is not a change made outside Predbat."""
    failed = False
    s = _parallel_pair()
    reads = {MASTER: dict(s.device_settings[MASTER]), SLAVE: dict(s.device_settings[SLAVE], importPower="9000")}

    async def fake_get(endpoint_key, sn=None, params=None):
        """Serve each unit's settings read."""
        return dict(reads[sn]) if endpoint_key == "settings_read" else {}

    with patch.object(s, "_get", side_effect=fake_get), patch.object(s, "save_config", side_effect=_noop):
        run_async_local(s.refresh_config())
    if any("changed outside Predbat" in message for message in s.log_messages):
        print(f"ERROR: the slave's settings change was logged as external: {s.log_messages}")
        failed = True
    if s.device_settings[SLAVE].get("importPower") != "9000":
        print("ERROR: the slave's settings must still be read")
        failed = True
    return failed


async def _noop(*args, **kwargs):
    """Do nothing, asynchronously."""
    return None


def test_parallel_cli_never_writes_to_a_slave():
    """The standalone CLI's --tou-test round trip reaches the master only, and names the slave as one."""
    import argparse
    import io
    from contextlib import redirect_stdout
    from tests.test_sunsynk_api import CLIMockSunsynk
    from sunsynk import test_sunsynk_api as run_cli_once

    failed = False
    client = _parallel_pair(CLIMockSunsynk())
    client.access_token = "tok"
    tested = []

    async def fake_round_trip(cli_client, sn):
        """Record which serial the TOU round trip was run against."""
        tested.append(sn)
        return False

    args = argparse.Namespace(username=None, password=None, region="sunsynk", auth_method="password", serial=None, dump_settings=False, write_test=False, tou_test=True, automatic=False)
    buffer = io.StringIO()
    with patch("sunsynk._build_sunsynk", return_value=client), patch.object(client, "run", side_effect=_async_true), patch("sunsynk._tou_round_trip", side_effect=fake_round_trip), _clock(
        client, LONDON.localize(datetime(2026, 10, 18, 9, 5))
    ), redirect_stdout(buffer):
        run_async_local(run_cli_once(args))
    output = buffer.getvalue()
    if tested != [MASTER]:
        print(f"ERROR: the TOU round trip must run against the master only, ran against {tested}")
        failed = True
    if f"Parallel group: master {MASTER}" not in output or f"{SLAVE} is a parallel slave of {MASTER}" not in output:
        print(f"ERROR: the CLI should describe the group:\n{output}")
        failed = True
    return failed


def test_parallel_cli_automatic_flag():
    """--automatic builds the CLI's component with automatic config on; without it, off."""
    import argparse
    from mock_base import MockBase
    from sunsynk import _build_sunsynk

    failed = False
    for flag in (True, False):
        args = argparse.Namespace(username="someone@example.com", password="secret", region="sunsynk", auth_method="password", serial=None, credentials=None, automatic=flag)
        client = _build_sunsynk(MockBase(), args)
        if client.automatic is not flag:
            print(f"ERROR: --automatic {flag} built a component with automatic={client.automatic}")
            failed = True
    return failed


async def _async_true(*args, **kwargs):
    """Stand in for a successful run()."""
    return True


def run_sunsynk_parallel_tests(my_predbat):
    """Run all Sunsynk parallel-group tests."""
    failed = False
    for name, fn in [
        ("detected_from_list", test_parallel_group_detected_from_the_inverter_list),
        ("filter_pulls_in_slave", test_parallel_serial_filter_on_the_master_pulls_in_its_slave),
        ("single_inverter_unchanged", test_parallel_single_inverter_is_unchanged),
        ("two_groups", test_parallel_two_groups_in_two_plants),
        ("two_masters_one_plant", test_parallel_two_masters_in_one_plant),
        ("settings_fallback", test_parallel_roles_fall_back_to_the_settings_read),
        ("slave_own_battery", test_parallel_slave_with_its_own_battery_is_independent),
        ("filter_own_battery_slave", test_parallel_filter_leaves_out_a_slave_with_its_own_battery),
        ("filter_no_roles_warns", test_parallel_filter_warns_when_the_list_has_no_roles),
        ("group_values", test_parallel_group_values),
        ("battery_sign", test_parallel_battery_power_follows_the_bms_sign),
        ("battery_fallback", test_parallel_battery_power_without_a_bms_link_falls_back_to_the_master),
        ("missing_value_withheld", test_parallel_group_value_missing_on_one_unit_is_withheld),
        ("energy_and_balance", test_parallel_group_energy_and_balance),
        ("battery_integrator", test_parallel_battery_integrator),
        ("ratings", test_parallel_ratings),
        ("automatic_config", test_parallel_automatic_config_registers_one_inverter),
        ("discovery_record", test_parallel_discovery_record),
        ("publish", test_parallel_publish),
        ("run_drives_master_only", test_parallel_run_drives_the_master_only),
        ("first_cycle_waits_for_slave", test_parallel_first_cycle_waits_for_the_slaves_battery_reading),
        ("restart", test_parallel_roles_and_battery_energy_survive_a_restart),
        ("slave_settings_not_external", test_parallel_slave_settings_are_not_reported_as_external_changes),
        ("cli_never_writes_slave", test_parallel_cli_never_writes_to_a_slave),
        ("cli_automatic_flag", test_parallel_cli_automatic_flag),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_parallel.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_parallel.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

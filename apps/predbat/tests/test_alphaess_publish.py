# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS publishing and automatic configuration
# -----------------------------------------------------------------------------

"""Tests for AlphaESS sensor publishing, arg auto-mapping and hybrid detection."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from tests.test_alphaess_api import MockAlphaESS, ESS_LIST_SAMPLE
from tests.test_infra import run_async as run_async_local


def _ready_client(automatic=True, ignore_pv=False):
    """Build a client with two discovered inverters and a full set of readings."""
    client = MockAlphaESS(automatic=automatic)
    client.automatic_ignore_pv = ignore_pv
    client.device_detail = {entry["sysSn"]: entry for entry in ESS_LIST_SAMPLE}
    client.device_list = [entry["sysSn"] for entry in ESS_LIST_SAMPLE]
    for sn in client.device_list:
        client.device_values[sn] = {"soc": 56.0, "battery_power": 1264.0, "grid_power": -11.0, "pv_power": 0.0, "load_power": 1275.0, "ev_power": 0.0}
        client.device_energy[sn] = {"import_today": 14.41, "export_today": 0.42, "pv_today": 10.6, "load_today": 19.49, "battery_charge_today": 12.2, "battery_discharge_today": 7.1, "grid_charge_today": 9.1}
    return client


def test_alphaess_publishes_every_monitoring_sensor():
    """Power, energy and ratings sensors all reach the dashboard under the expected names."""
    failed = False
    client = _ready_client()
    run_async_local(client.publish_data())
    sn = "AL70110230306xx".lower()
    for leaf in ("soc", "battery_power", "grid_power", "pv_power", "load_power", "import_today", "export_today", "pv_today", "load_today", "battery_capacity", "inverter_limit", "battery_rate_max"):
        entity = f"sensor.predbat_alphaess_{sn}_{leaf}"
        if entity not in client.published:
            print(f"ERROR: {entity} not published")
            failed = True
    energy = client.published.get(f"sensor.predbat_alphaess_{sn}_load_today", {})
    # Daily counters reset at midnight; the recorder needs these to keep them.
    if energy.get("attributes", {}).get("device_class") != "energy":
        print(f"ERROR: load_today device_class {energy.get('attributes')}")
        failed = True
    assert not failed, "test_alphaess_publishes_every_monitoring_sensor"


def test_alphaess_automatic_config_forces_the_invert_flags_off():
    """base.args is shared and NOT namespaced per inverter type.

    A Teslemetry or Fox install that legitimately inverts its own grid sensor leaves the
    flag set for every index; an AlphaESS inverter that never claims it inherits the flip,
    the already-correct sensor is negated a second time, and an export reads as an import.
    """
    failed = False
    client = _ready_client()
    client.base.args["grid_power_invert"] = True
    client.base.args["battery_power_invert"] = True
    run_async_local(client.automatic_config())
    for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
        value = client.base.args.get(flag)
        if value != [False, False]:
            print(f"ERROR: {flag} = {value} should be [False, False]")
            failed = True
    assert not failed, "test_alphaess_automatic_config_forces_the_invert_flags_off"


def test_alphaess_automatic_config_maps_the_expected_args():
    """Every arg Predbat needs is bound, and inverter_type/num_inverters are set."""
    failed = False
    client = _ready_client()
    run_async_local(client.automatic_config())
    args = client.base.args
    if args.get("inverter_type") != ["AlphaESSCloud", "AlphaESSCloud"]:
        print(f"ERROR: inverter_type {args.get('inverter_type')}")
        failed = True
    if args.get("num_inverters") != 2:
        print(f"ERROR: num_inverters {args.get('num_inverters')}")
        failed = True
    for arg in ("soc_percent", "battery_power", "grid_power", "load_power", "pv_power", "soc_max", "inverter_limit", "battery_rate_max", "load_today", "import_today", "export_today", "pv_today"):
        if arg not in args:
            print(f"ERROR: arg {arg} not mapped")
            failed = True
    for arg in ("reserve", "charge_start_time", "charge_end_time", "charge_limit", "charge_rate", "scheduled_charge_enable", "discharge_start_time", "discharge_end_time", "discharge_target_soc", "discharge_rate", "scheduled_discharge_enable", "schedule_write_button"):
        if arg not in args:
            print(f"ERROR: control arg {arg} not mapped")
            failed = True
    assert not failed, "test_alphaess_automatic_config_maps_the_expected_args"


def test_alphaess_export_limit_is_not_guessed():
    """poinv is the inverter rating, not the site's grid-connection limit.

    A G98/G99-capped site can sit far below it, and unlike battery_rate_max nothing
    measures and reports the error back, so guessing here would over-export silently.
    """
    failed = False
    client = _ready_client()
    run_async_local(client.automatic_config())
    if "export_limit" in client.base.args:
        print("ERROR: export_limit should not be auto-mapped")
        failed = True
    if not any("export_limit" in message for message in client.log_messages):
        print(f"ERROR: no export_limit warning, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_export_limit_is_not_guessed"


def test_alphaess_battery_min_soc_is_not_mapped():
    """batUseCap is a field Predbat WRITES; reading it back as the floor would be circular."""
    failed = False
    client = _ready_client()
    run_async_local(client.automatic_config())
    if "battery_min_soc" in client.base.args:
        print("ERROR: battery_min_soc must not be mapped from batUseCap")
        failed = True
    assert not failed, "test_alphaess_battery_min_soc_is_not_mapped"


def test_alphaess_hybrid_switch_only_moves_on_agreeing_evidence():
    """The two errors are NOT symmetric.

    hybrid=False on a real hybrid stops PV counting against inverter_limit, so Predbat
    plans past what the inverter passes and the surplus is clipped with targets silently
    missed. hybrid=True on an AC-coupled system merely under-uses the battery. Predbat
    defaults to True and every mainstream AlphaESS unit is a hybrid, so the switch moves
    only when both signals agree.
    """
    failed = False
    entity = "switch.predbat_inverter_hybrid"

    # Both signals agree on AC coupling: ppvDetail all null AND popv 0 while epv > 0.
    ac = _ready_client()
    for sn in ac.device_list:
        ac.device_detail[sn] = dict(ac.device_detail[sn], popv=0.0)
        ac.device_values[sn]["ppv_detail_all_null"] = True
        ac.device_energy[sn]["pv_today"] = 6.2
    run_async_local(ac.apply_hybrid_verdict())
    if ac.external_state.get(entity) is not False:
        print(f"ERROR: agreeing AC evidence did not flip the switch: {ac.external_state}")
        failed = True

    # A hybrid AT NIGHT reports ZEROS, not nulls. It must never be misread as AC-coupled.
    night = _ready_client()
    for sn in night.device_list:
        night.device_values[sn]["ppv_detail_all_null"] = False
        night.device_energy[sn]["pv_today"] = 0.0
    run_async_local(night.apply_hybrid_verdict())
    if entity in night.external_state:
        print(f"ERROR: a hybrid at night moved the switch: {night.external_state}")
        failed = True

    # Signals disagree: leave Predbat's default alone and say what was seen.
    mixed = _ready_client()
    for sn in mixed.device_list:
        mixed.device_values[sn]["ppv_detail_all_null"] = True
        mixed.device_detail[sn] = dict(mixed.device_detail[sn], popv=9.0)
    run_async_local(mixed.apply_hybrid_verdict())
    if entity in mixed.external_state:
        print(f"ERROR: disagreeing signals moved the switch: {mixed.external_state}")
        failed = True
    if not any("inverter_hybrid" in message for message in mixed.log_messages):
        print(f"ERROR: no advisory log on an undecided verdict, got {mixed.log_messages}")
        failed = True
    assert not failed, "test_alphaess_hybrid_switch_only_moves_on_agreeing_evidence"


def test_alphaess_args_only_mapped_when_every_inverter_reports_them():
    """An arg pointing at a sensor that never appears is worse than an absent arg the
    user can fill in themselves."""
    failed = False
    client = _ready_client()
    # One inverter is missing its load counter entirely.
    del client.device_energy["AL70110230302xx"]["load_today"]
    run_async_local(client.automatic_config())
    if "load_today" in client.base.args:
        print("ERROR: load_today mapped despite one inverter not reporting it")
        failed = True
    if not any("load_today" in message for message in client.log_messages):
        print(f"ERROR: no warning naming load_today, got {client.log_messages}")
        failed = True
    assert not failed, "test_alphaess_args_only_mapped_when_every_inverter_reports_them"


def run_alphaess_publish_tests(my_predbat):
    """Run all AlphaESS publish/config tests."""
    failed = False
    for name, fn in [
        ("monitoring_sensors", test_alphaess_publishes_every_monitoring_sensor),
        ("invert_flags", test_alphaess_automatic_config_forces_the_invert_flags_off),
        ("automatic_args", test_alphaess_automatic_config_maps_the_expected_args),
        ("export_limit_not_guessed", test_alphaess_export_limit_is_not_guessed),
        ("battery_min_soc_not_mapped", test_alphaess_battery_min_soc_is_not_mapped),
        ("hybrid_verdict", test_alphaess_hybrid_switch_only_moves_on_agreeing_evidence),
        ("map_only_when_all_report", test_alphaess_args_only_mapped_when_every_inverter_reports_them),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_publish.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_publish.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

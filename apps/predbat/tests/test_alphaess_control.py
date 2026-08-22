# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS control derivation and write path
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS control entities, payload derivation and write gating."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from tests.test_alphaess_api import MockAlphaESS
from tests.test_infra import run_async as run_async_local


def _schedule(reserve=10, charge=None, export=None, charge_power=3000, export_power=3000):
    """Build a schedule dict in the shape the control entities produce.

    A DISABLED window still carries a power, because the real control entities do:
    adjust_charge_rate/adjust_discharge_rate write Predbat's rates every cycle whether or
    not a window is enabled. That matters because a ZERO rate is precisely how Predbat
    signals freeze on this inverter (there is no pause endpoint), so a fixture leaving
    both at zero would silently be testing a freeze instead of the demand state it reads as.
    """
    idle_charge = {"enable": False, "soc": 0, "power": charge_power, "start": "00:00:00", "end": "00:00:00"}
    idle_export = {"enable": False, "soc": 0, "power": export_power, "start": "00:00:00", "end": "00:00:00"}
    return {"reserve": reserve, "charge": charge or idle_charge, "export": export or idle_export}


def _client(sn="AL70"):
    """Build a client with one discovered inverter ready for control tests."""
    client = MockAlphaESS()
    client.device_list = [sn]
    client.device_detail = {sn: {"sysSn": sn, "cobat": 13.34, "poinv": 5.0, "popv": 9.0, "minv": "SMILE5-INV"}}
    client.device_values = {sn: {"soc": 50.0}}
    client.device_config[sn] = {
        "charge": {"gridCharge": 0, "timeChaf1": "00:00", "timeChae1": "00:00", "timeChaf2": "00:00", "timeChae2": "00:00", "batHighCap": 100},
        "discharge": {"ctrDis": 0, "timeDisf1": "00:00", "timeDise1": "00:00", "timeDisf2": "00:00", "timeDise2": "00:00", "batUseCap": 10},
    }
    return client


def test_alphaess_control_entities_round_trip():
    """Published control entities read back into exactly the schedule shape they came from."""
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule(reserve=12, charge={"enable": True, "soc": 90, "power": 3000, "start": "01:00:00", "end": "05:00:00"})
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes Predbat
    # replace these entities with its own dummies and the window never arrives.
    start = client.published.get("select.predbat_alphaess_al70_battery_schedule_charge_start_time", {}).get("state")
    if start != "01:00:00":
        print(f"ERROR: published start {start}")
        failed = True
    read_back = run_async_local(client.get_schedule_settings_ha("AL70"))
    if read_back["charge"]["enable"] is not True or read_back["charge"]["soc"] != 90 or read_back["reserve"] != 12:
        print(f"ERROR: read back {read_back}")
        failed = True
    assert not failed, "test_alphaess_control_entities_round_trip"


def test_alphaess_reserve_entity_is_published_unclamped():
    """Predbat writes then reads back to confirm (write_and_poll_value).

    Publishing anything other than what was written guarantees a mismatch and a retry
    storm; the floor is enforced at the API boundary in the payload builder instead.
    """
    failed = False
    client = _client()
    client.local_schedule["AL70"] = _schedule(reserve=3)
    run_async_local(client.publish_schedule_settings_ha("AL70"))
    state = client.published.get("number.predbat_alphaess_al70_battery_schedule_reserve", {}).get("state")
    if state != 3:
        print(f"ERROR: reserve published as {state}, should echo the written 3 unclamped")
        failed = True
    assert not failed, "test_alphaess_reserve_entity_is_published_unclamped"


def test_alphaess_entity_routing_does_not_confuse_prefixed_serials():
    """An entity for AL701 must never route to AL70 - that would write to the wrong inverter."""
    failed = False
    client = _client()
    client.device_list = ["AL70", "AL701"]
    if client._sn_from_entity("number.predbat_alphaess_al701_battery_schedule_reserve") != "AL701":
        print("ERROR: AL701 entity routed to the wrong serial")
        failed = True
    if client._sn_from_entity("number.predbat_alphaess_al70_battery_schedule_reserve") != "AL70":
        print("ERROR: AL70 entity misrouted")
        failed = True
    if client._sn_from_entity("number.predbat_alphaess_zz99_battery_schedule_reserve") is not None:
        print("ERROR: unknown serial should not resolve")
        failed = True
    assert not failed, "test_alphaess_entity_routing_does_not_confuse_prefixed_serials"


def test_alphaess_update_local_schedule_applies_each_field():
    """Each control entity change lands on the right field of the held schedule."""
    failed = False
    client = _client()
    for entity, value, path in [
        ("number.predbat_alphaess_al70_battery_schedule_reserve", 15, ("reserve",)),
        ("select.predbat_alphaess_al70_battery_schedule_charge_start_time", "02:30:00", ("charge", "start")),
        ("number.predbat_alphaess_al70_battery_schedule_charge_soc", 85, ("charge", "soc")),
        ("number.predbat_alphaess_al70_battery_schedule_export_power", 4000, ("export", "power")),
        ("switch.predbat_alphaess_al70_battery_schedule_export_enable", "turn_on", ("export", "enable")),
    ]:
        client.update_local_schedule("AL70", entity, value)
    schedule = client.local_schedule["AL70"]
    if schedule["reserve"] != 15 or schedule["charge"]["start"] != "02:30:00" or schedule["charge"]["soc"] != 85:
        print(f"ERROR: schedule {schedule}")
        failed = True
    if schedule["export"]["power"] != 4000 or schedule["export"]["enable"] is not True:
        print(f"ERROR: export {schedule['export']}")
        failed = True
    assert not failed, "test_alphaess_update_local_schedule_applies_each_field"


def run_alphaess_control_tests(my_predbat):
    """Run all AlphaESS control-logic tests."""
    failed = False
    for name, fn in [
        ("entities_round_trip", test_alphaess_control_entities_round_trip),
        ("reserve_unclamped", test_alphaess_reserve_entity_is_published_unclamped),
        ("entity_routing", test_alphaess_entity_routing_does_not_confuse_prefixed_serials),
        ("update_local_schedule", test_alphaess_update_local_schedule_applies_each_field),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_control.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_control.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed

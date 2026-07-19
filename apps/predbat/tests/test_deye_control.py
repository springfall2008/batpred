# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE control-state derivation
# -----------------------------------------------------------------------------

"""Tests for the DEYE behaviour to work-mode derivation (``derive_control_state``)."""

from deye_const import DEYE_WORKMODE, FREEZE_EXPORT_SOC
from tests.test_deye_api import MockDeye


def _state(reserve=10, charge=None, export=None):
    """Build a schedule dict in the shape ``derive_control_state`` expects."""
    return {"reserve": reserve, "charge": charge or {"enable": False, "soc": 0, "power": 0}, "export": export or {"enable": False, "soc": 0, "power": 0}}


def test_derive_control_state_table():
    """Each Predbat intent maps to the correct DEYE control state (spec table)."""
    failed = False
    d = MockDeye()
    cases = [
        # name, schedule, current_soc, expect(behaviour, work_mode, grid_charge, solar_sell, slot_soc)
        ("charge", _state(reserve=10, charge={"enable": True, "soc": 90, "power": 3000}), 50, ("charge", DEYE_WORKMODE["zero_export_load"], True, False, 90)),
        ("freeze_charge", _state(reserve=50, charge={"enable": True, "soc": 50, "power": 3000}), 50, ("freeze_charge", DEYE_WORKMODE["zero_export_load"], True, False, 50)),
        ("hold_charge", _state(reserve=50, charge={"enable": True, "soc": 40, "power": 3000}), 50, ("hold_charge", DEYE_WORKMODE["zero_export_load"], False, False, 50)),
        ("export", _state(reserve=10, export={"enable": True, "soc": 20, "power": 3000}), 80, ("export", DEYE_WORKMODE["selling_first"], False, True, 20)),
        ("freeze_export", _state(reserve=10, export={"enable": True, "soc": FREEZE_EXPORT_SOC, "power": 3000}), 80, ("freeze_export", DEYE_WORKMODE["selling_first"], False, True, FREEZE_EXPORT_SOC)),
        ("idle", _state(reserve=15), 60, ("idle", DEYE_WORKMODE["zero_export_load"], False, False, 15)),
    ]
    for name, sched, soc, exp in cases:
        r = d.derive_control_state(sched, soc)
        got = (r["behaviour"], r["work_mode"], r["grid_charge"], r["solar_sell"], r["slot_soc"])
        if got != exp:
            print(f"ERROR: {name} expected {exp} got {got}")
            failed = True
    assert not failed, "test_derive_control_state_table"

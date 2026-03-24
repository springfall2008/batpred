# apps/predbat/tests/test_arbitrage.py
# fmt: off
# pylint: disable=line-too-long
"""Unit tests for ArbitrageEngine — no HA or AppDaemon dependency."""

from arbitrage import ArbitrageEngine


def _make_engine(**kwargs) -> ArbitrageEngine:
    """Build an ArbitrageEngine with sensible defaults, overridable per-test."""
    defaults = {
        "rate_import": {i: 15.0 for i in range(0, 1440)},  # 15p flat — no spread
        "rate_export": {i: 5.0 for i in range(0, 1440)},   # 5p flat
        "solar_forecast": {i: 0.0 for i in range(0, 1440)},
        "load_forecast": {i: 0.3 for i in range(0, 1440)},
        "battery_soc_percent": 50.0,
        "battery_capacity_kwh": 10.0,
        "charge_rate_kw": 3.6,
        "discharge_rate_kw": 3.6,
        "battery_efficiency": 0.9,
        "profit_target_daily": 1.0,
        "arbitrage_reserve_percent": 20.0,
        "minutes_now": 0,
    }
    defaults.update(kwargs)
    return ArbitrageEngine(**defaults)


# ---------------------------------------------------------------------------
# score_slots() tests
# ---------------------------------------------------------------------------

def _test_score_no_spread(my_predbat=None):
    """With import >= export after efficiency losses, no profitable slots."""
    slots = _make_engine().score_slots()
    if slots != []:
        print(f"ERROR: Expected [], got {slots[:3]}")
        return True
    return False


def _test_score_single_profitable_pair(my_predbat=None):
    """One cheap import slot + one expensive export slot produces a scored pair."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[0] = 3.0
    rate_export[60] = 40.0
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export).score_slots()
    if len(slots) < 1:
        print("ERROR: Expected at least one profitable slot pair")
        return True
    best = slots[0]
    if best["charge_minute"] != 0 or best["export_minute"] != 60:
        print(f"ERROR: Expected charge=0, export=60, got charge={best['charge_minute']}, export={best['export_minute']}")
        return True
    if best["net_profit_gbp"] <= 0:
        print(f"ERROR: Expected positive net_profit_gbp, got {best['net_profit_gbp']}")
        return True
    return False


def _test_score_sorted_descending(my_predbat=None):
    """Slots must be sorted descending by net_profit_gbp."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[0] = 3.0
    rate_export[60] = 40.0
    rate_import[30] = 8.0
    rate_export[90] = 20.0
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export).score_slots()
    profits = [s["net_profit_gbp"] for s in slots]
    if profits != sorted(profits, reverse=True):
        print(f"ERROR: Slots not sorted descending: {profits[:5]}")
        return True
    return False


def _test_score_export_after_charge(my_predbat=None):
    """Export slot must be strictly after charge slot — time travel not allowed."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[120] = 3.0
    rate_export[60] = 50.0  # export BEFORE the cheap import — invalid
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export).score_slots()
    for s in slots:
        if s["charge_minute"] == 120 and s["export_minute"] == 60:
            print("ERROR: Found export-before-charge pair in results")
            return True
    return False


def _test_score_future_slots_only(my_predbat=None):
    """Both charge and export slots must be >= minutes_now."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export, minutes_now=120).score_slots()
    for s in slots:
        if s["charge_minute"] < 120:
            print(f"ERROR: charge_minute {s['charge_minute']} < minutes_now 120")
            return True
        if s["export_minute"] < 120:
            print(f"ERROR: export_minute {s['export_minute']} < minutes_now 120")
            return True
    return False


def _test_score_confidence_near_beats_far(my_predbat=None):
    """Identical spreads at different horizons: nearer slot scores >= farther slot."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[30] = 3.0
    rate_export[60] = 40.0   # near pair (1h ahead)
    rate_import[660] = 3.0
    rate_export[690] = 40.0  # far pair (11.5h ahead)
    slots = _make_engine(rate_import=rate_import, rate_export=rate_export, minutes_now=0).score_slots()
    near = next((s for s in slots if s["charge_minute"] == 30 and s["export_minute"] == 60), None)
    far = next((s for s in slots if s["charge_minute"] == 660 and s["export_minute"] == 690), None)
    if near is None or far is None:
        print("ERROR: Could not find near or far slot pair in results")
        return True
    if near["net_profit_gbp"] < far["net_profit_gbp"]:
        print(f"ERROR: Near ({near['net_profit_gbp']:.4f}) scored lower than far ({far['net_profit_gbp']:.4f})")
        return True
    return False


# ---------------------------------------------------------------------------
# schedule_to_target() tests
# ---------------------------------------------------------------------------

def _test_schedule_no_profitable_slots(my_predbat=None):
    """When there are no profitable slots, return empty schedule."""
    result = _make_engine().schedule_to_target()
    if result != []:
        print(f"ERROR: Expected [], got {result}")
        return True
    return False


def _test_schedule_contains_charge_and_export(my_predbat=None):
    """Each scheduled pair must produce one charge and one export slot."""
    rate_import = {i: 15.0 for i in range(0, 1440)}
    rate_export = {i: 5.0 for i in range(0, 1440)}
    rate_import[0] = 3.0
    rate_export[60] = 40.0
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    schedule = eng.schedule_to_target()
    types = {s["type"] for s in schedule}
    if "charge" not in types:
        print("ERROR: Schedule missing 'charge' slot")
        return True
    if "export" not in types:
        print("ERROR: Schedule missing 'export' slot")
        return True
    return False


def _test_schedule_slot_structure(my_predbat=None):
    """Each slot dict must have all required keys with correct types."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    failed = False
    for slot in eng.schedule_to_target():
        if not isinstance(slot.get("start"), int):
            print(f"ERROR: start missing or wrong type in {slot}")
            failed = True
        if not isinstance(slot.get("end"), int):
            print(f"ERROR: end missing or wrong type in {slot}")
            failed = True
        if slot.get("type") not in ("charge", "export"):
            print(f"ERROR: type invalid in {slot}")
            failed = True
        if not isinstance(slot.get("target_soc"), float):
            print(f"ERROR: target_soc missing or wrong type in {slot}")
            failed = True
        if slot.get("end", 0) <= slot.get("start", 0):
            print(f"ERROR: end <= start in {slot}")
            failed = True
    return failed


def _test_schedule_no_overlaps(my_predbat=None):
    """Slots in the schedule must not overlap each other."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=2.0)
    schedule = eng.schedule_to_target()
    for i, a in enumerate(schedule):
        for j, b in enumerate(schedule):
            if i >= j:
                continue
            overlap = a["start"] < b["end"] and b["start"] < a["end"]
            if overlap:
                print(f"ERROR: Slots overlap: {a} and {b}")
                return True
    return False


def _test_schedule_sorted_chronologically(my_predbat=None):
    """Schedule must be ordered by start time."""
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.50)
    schedule = eng.schedule_to_target()
    starts = [s["start"] for s in schedule]
    if starts != sorted(starts):
        print(f"ERROR: Schedule not sorted: {starts}")
        return True
    return False


def _test_schedule_meets_target(my_predbat=None):
    """With very profitable rates, projected_gain should approximately meet the target."""
    rate_import = {i: 1.0 for i in range(0, 1440)}
    rate_export = {i: 50.0 for i in range(0, 1440)}
    target = 0.50
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=target)
    # Use projected_gain() which uses raw (undiscounted) profit for monetary reporting
    gain = eng.projected_gain()
    if gain < target * 0.90:
        print(f"ERROR: projected_gain {gain:.3f} < 90% of target {target}")
        return True
    return False


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

def _test_error_empty_rate_import(my_predbat=None):
    result = _make_engine(rate_import={}).schedule_to_target()
    if result != []:
        print(f"ERROR: Expected [] with empty rate_import, got {result}")
        return True
    return False


def _test_error_empty_rate_export(my_predbat=None):
    result = _make_engine(rate_export={}).schedule_to_target()
    if result != []:
        print(f"ERROR: Expected [] with empty rate_export, got {result}")
        return True
    return False


def _test_error_partial_rates_no_exception(my_predbat=None):
    """Missing slots in rate dicts are skipped without error."""
    try:
        eng = _make_engine(rate_import={0: 3.0}, rate_export={60: 40.0}, profit_target_daily=0.01)
        result = eng.schedule_to_target()
        if not isinstance(result, list):
            print("ERROR: Expected list result")
            return True
    except Exception as e:
        print(f"ERROR: Unexpected exception with partial rates: {e}")
        return True
    return False


def _test_error_unachievable_target_no_exception(my_predbat=None):
    """Unachievable target returns best-effort schedule without raising."""
    rate_import = {i: 14.9 for i in range(0, 1440)}
    rate_export = {i: 15.1 for i in range(0, 1440)}
    try:
        result = _make_engine(rate_import=rate_import, rate_export=rate_export,
                              profit_target_daily=1000.0).schedule_to_target()
        if not isinstance(result, list):
            print("ERROR: Expected list")
            return True
    except Exception as e:
        print(f"ERROR: Unexpected exception: {e}")
        return True
    return False


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------

def _test_metrics_projected_gain_zero_no_spread(my_predbat=None):
    gain = _make_engine().projected_gain()
    if gain != 0.0:
        print(f"ERROR: Expected 0.0, got {gain}")
        return True
    return False


def _test_metrics_projected_gain_positive_with_spread(my_predbat=None):
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    gain = _make_engine(rate_import=rate_import, rate_export=rate_export).projected_gain()
    if gain <= 0:
        print(f"ERROR: Expected positive gain, got {gain}")
        return True
    return False


def _test_metrics_opportunity_score_zero_no_spread(my_predbat=None):
    score = _make_engine().opportunity_score()
    if score != 0:
        print(f"ERROR: Expected 0, got {score}")
        return True
    return False


def _test_metrics_opportunity_score_bounded(my_predbat=None):
    rate_import = {i: 0.1 for i in range(0, 1440)}
    rate_export = {i: 100.0 for i in range(0, 1440)}
    score = _make_engine(rate_import=rate_import, rate_export=rate_export).opportunity_score()
    if not (0 <= score <= 100):
        print(f"ERROR: Score {score} out of range 0-100")
        return True
    return False


# ---------------------------------------------------------------------------
# plan_constraints() tests
# ---------------------------------------------------------------------------

def _test_constraints_returns_list(my_predbat=None):
    result = _make_engine().plan_constraints()
    if not isinstance(result, list):
        print(f"ERROR: Expected list, got {type(result)}")
        return True
    return False


def _test_constraints_empty_no_spread(my_predbat=None):
    if _make_engine().plan_constraints() != []:
        print("ERROR: Expected [] with no spread")
        return True
    return False


def _test_constraints_structure(my_predbat=None):
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    constraints = eng.plan_constraints()
    if not constraints:
        print("ERROR: Expected at least one constraint")
        return True
    failed = False
    for c in constraints:
        for key in ("start", "end", "average", "min", "max", "constraint_type"):
            if key not in c:
                print(f"ERROR: Missing key '{key}' in constraint {c}")
                failed = True
        if c.get("constraint_type") not in ("charge", "export"):
            print(f"ERROR: Invalid constraint_type: {c.get('constraint_type')}")
            failed = True
        if c.get("end", 0) <= c.get("start", 0):
            print(f"ERROR: end <= start in {c}")
            failed = True
    return failed


def _test_constraints_charge_uses_import_rate(my_predbat=None):
    rate_import = {i: 7.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    charge_cs = [c for c in eng.plan_constraints() if c["constraint_type"] == "charge"]
    if not charge_cs:
        print("ERROR: No charge constraints found")
        return True
    if charge_cs[0]["average"] != 7.0:
        print(f"ERROR: Charge constraint average should be 7.0, got {charge_cs[0]['average']}")
        return True
    return False


def _test_constraints_export_uses_export_rate(my_predbat=None):
    rate_import = {i: 3.0 for i in range(0, 1440)}
    rate_export = {i: 40.0 for i in range(0, 1440)}
    eng = _make_engine(rate_import=rate_import, rate_export=rate_export, profit_target_daily=0.01)
    export_cs = [c for c in eng.plan_constraints() if c["constraint_type"] == "export"]
    if not export_cs:
        print("ERROR: No export constraints found")
        return True
    if export_cs[0]["average"] != 40.0:
        print(f"ERROR: Export constraint average should be 40.0, got {export_cs[0]['average']}")
        return True
    return False


def test_arbitrage(my_predbat=None):
    """Main arbitrage test runner. Returns True if any test failed."""
    sub_tests = [
        ("score_slots: no spread returns empty", _test_score_no_spread),
        ("score_slots: single profitable pair", _test_score_single_profitable_pair),
        ("score_slots: sorted descending", _test_score_sorted_descending),
        ("score_slots: export after charge only", _test_score_export_after_charge),
        ("score_slots: future slots only", _test_score_future_slots_only),
        ("score_slots: confidence discount near > far", _test_score_confidence_near_beats_far),
        ("schedule_to_target: no profitable slots returns empty", _test_schedule_no_profitable_slots),
        ("schedule_to_target: contains charge and export", _test_schedule_contains_charge_and_export),
        ("schedule_to_target: slot structure correct", _test_schedule_slot_structure),
        ("schedule_to_target: no overlaps", _test_schedule_no_overlaps),
        ("schedule_to_target: sorted chronologically", _test_schedule_sorted_chronologically),
        ("schedule_to_target: meets profit target", _test_schedule_meets_target),
        # Error handling
        ("error: empty rate_import returns empty", _test_error_empty_rate_import),
        ("error: empty rate_export returns empty", _test_error_empty_rate_export),
        ("error: partial rates no exception", _test_error_partial_rates_no_exception),
        ("error: unachievable target no exception", _test_error_unachievable_target_no_exception),
        # Metrics
        ("metrics: projected_gain zero with no spread", _test_metrics_projected_gain_zero_no_spread),
        ("metrics: projected_gain positive with spread", _test_metrics_projected_gain_positive_with_spread),
        ("metrics: opportunity_score zero with no spread", _test_metrics_opportunity_score_zero_no_spread),
        ("metrics: opportunity_score bounded 0-100", _test_metrics_opportunity_score_bounded),
        # plan_constraints
        ("plan_constraints: returns list", _test_constraints_returns_list),
        ("plan_constraints: empty with no spread", _test_constraints_empty_no_spread),
        ("plan_constraints: structure correct", _test_constraints_structure),
        ("plan_constraints: charge uses import rate", _test_constraints_charge_uses_import_rate),
        ("plan_constraints: export uses export rate", _test_constraints_export_uses_export_rate),
    ]
    failed = 0
    for name, fn in sub_tests:
        print(f"*** Running: {name}")
        try:
            result = fn(my_predbat)
            if result:
                print(f"FAILED: {name}")
                failed += 1
            else:
                print(f"PASSED: {name}")
        except Exception as e:
            print(f"EXCEPTION in {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"RESULTS: {len(sub_tests) - failed} passed, {failed} failed out of {len(sub_tests)} tests")
    return failed > 0

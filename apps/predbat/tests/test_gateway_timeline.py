"""Display timeline: hold and freeze slots must be distinguishable from active charge/export.

"timeline" keeps the original codes so devices already in the field draw exactly what
they always have. "timeline_sub" carries a per-slot modifier saying whether a charge or
export slot is held (Hold) or frozen (Freeze).
"""

import gateway
from gateway import (
    TIMELINE_CHARGE,
    TIMELINE_EXPORT,
    TIMELINE_IDLE,
    TIMELINE_IMPORT,
    TIMELINE_SOLAR,
    TIMELINE_STATE_CODES,
    TIMELINE_SUB_FREEZE,
    TIMELINE_SUB_HOLD,
    TIMELINE_SUB_NONE,
    timeline_slot_codes,
)

# Every raw_state the plan table emits (output.py)
PLAN_RAW_STATES = ["Demand", "Chrg", "FrzChrg", "HoldChrg", "Exp", "FrzExp", "HoldExp"]
# The original code for each state before timeline_sub existed
LEGACY_CODES = {"Chrg": 1, "FrzChrg": 1, "HoldChrg": 1, "Exp": 2, "HoldExp": 2, "FrzExp": 2}


def _helpers():
    """Payload helpers shared with the existing predbat_data tests."""
    from tests.test_gateway import TestPublishPredbatData

    return TestPublishPredbatData()


class TestTimelineCodeContract:
    """The code values are a wire contract with the Hub firmware."""

    def test_code_values_are_fixed(self):
        """Firmware hard-codes these values; renumbering breaks every Hub."""
        assert (TIMELINE_IDLE, TIMELINE_CHARGE, TIMELINE_EXPORT, TIMELINE_SOLAR, TIMELINE_IMPORT) == (0, 1, 2, 3, 4)
        assert (TIMELINE_SUB_NONE, TIMELINE_SUB_HOLD, TIMELINE_SUB_FREEZE) == (0, 1, 2)

    def test_timeline_codes_are_unchanged(self):
        """Adding timeline_sub must not change a single timeline code old firmware sees."""
        for state, legacy in LEGACY_CODES.items():
            assert TIMELINE_STATE_CODES[state][0] == legacy, state

    def test_timeline_never_emits_codes_above_4(self):
        """Old firmware draws anything above 4 as idle."""
        assert {code for code, _ in TIMELINE_STATE_CODES.values()} <= {TIMELINE_CHARGE, TIMELINE_EXPORT}

    def test_modifiers_by_state(self):
        assert TIMELINE_STATE_CODES == {
            "Chrg": (1, 0),
            "HoldChrg": (1, 1),
            "FrzChrg": (1, 2),
            "Exp": (2, 0),
            "HoldExp": (2, 1),
            "FrzExp": (2, 2),
        }

    def test_hold_and_freeze_are_distinct(self):
        """The point of a modifier over a flag: Hold and Freeze stay apart."""
        assert TIMELINE_STATE_CODES["HoldChrg"] != TIMELINE_STATE_CODES["FrzChrg"]
        assert TIMELINE_STATE_CODES["HoldExp"] != TIMELINE_STATE_CODES["FrzExp"]

    def test_only_active_states_have_no_modifier(self):
        for state, (_, sub) in TIMELINE_STATE_CODES.items():
            assert (sub == TIMELINE_SUB_NONE) == (state in ("Chrg", "Exp")), state

    def test_every_plan_state_is_classified(self):
        """Only Demand falls through to the solar/import split; every other plan state is mapped."""
        for state in PLAN_RAW_STATES:
            assert (state in TIMELINE_STATE_CODES) == (state != "Demand"), state


class TestTimelineSlotCodes:
    """timeline_slot_codes() for a single plan row."""

    def test_active_charge(self):
        assert timeline_slot_codes("Chrg", 0) == (TIMELINE_CHARGE, TIMELINE_SUB_NONE)

    def test_active_export(self):
        assert timeline_slot_codes("Exp", 0) == (TIMELINE_EXPORT, TIMELINE_SUB_NONE)

    def test_hold_charge(self):
        assert timeline_slot_codes("HoldChrg", 0) == (TIMELINE_CHARGE, TIMELINE_SUB_HOLD)

    def test_freeze_charge(self):
        assert timeline_slot_codes("FrzChrg", 0) == (TIMELINE_CHARGE, TIMELINE_SUB_FREEZE)

    def test_hold_export(self):
        assert timeline_slot_codes("HoldExp", 0) == (TIMELINE_EXPORT, TIMELINE_SUB_HOLD)

    def test_freeze_export(self):
        assert timeline_slot_codes("FrzExp", 0) == (TIMELINE_EXPORT, TIMELINE_SUB_FREEZE)

    def test_hold_ignores_pv(self):
        """A held slot with sun is still a hold, not solar."""
        assert timeline_slot_codes("HoldChrg", 3.5) == (TIMELINE_CHARGE, TIMELINE_SUB_HOLD)
        assert timeline_slot_codes("FrzExp", 3.5) == (TIMELINE_EXPORT, TIMELINE_SUB_FREEZE)

    def test_demand_with_pv_is_solar(self):
        assert timeline_slot_codes("Demand", 0.5) == (TIMELINE_SOLAR, TIMELINE_SUB_NONE)

    def test_demand_without_pv_is_import(self):
        assert timeline_slot_codes("Demand", 0.0) == (TIMELINE_IMPORT, TIMELINE_SUB_NONE)

    def test_pv_threshold_is_exclusive(self):
        """Exactly 0.1 kW is not enough to call the slot solar."""
        assert timeline_slot_codes("Demand", 0.1) == (TIMELINE_IMPORT, TIMELINE_SUB_NONE)
        assert timeline_slot_codes("Demand", 0.11) == (TIMELINE_SOLAR, TIMELINE_SUB_NONE)

    def test_pv_as_string(self):
        assert timeline_slot_codes("Demand", "0.5") == (TIMELINE_SOLAR, TIMELINE_SUB_NONE)

    def test_pv_missing_or_bad(self):
        """Missing or unparseable PV reads as none, rather than failing the whole publish."""
        for pv in (None, "", "n/a", [], {}):
            assert timeline_slot_codes("Demand", pv) == (TIMELINE_IMPORT, TIMELINE_SUB_NONE), repr(pv)

    def test_empty_and_unknown_state(self):
        assert timeline_slot_codes("", 0) == (TIMELINE_IMPORT, TIMELINE_SUB_NONE)
        assert timeline_slot_codes("Idle", 0.5) == (TIMELINE_SOLAR, TIMELINE_SUB_NONE)

    def test_state_names_are_case_sensitive(self):
        """Plan states are exact tokens; a near miss is not a hold."""
        assert timeline_slot_codes("HoldChrg".lower(), 0) == (TIMELINE_IMPORT, TIMELINE_SUB_NONE)

    def test_html_decorated_state_is_not_matched(self):
        """The display column ("HoldChrg&rarr;") is not the raw state and must not match."""
        assert timeline_slot_codes("HoldChrg&rarr;", 0) == (TIMELINE_IMPORT, TIMELINE_SUB_NONE)


class TestTimelineSubPayload:
    """timeline_sub in the published predbat_data payload."""

    def _payload(self, specs):
        h = _helpers()
        return h._get_payload_with_plan(h._make_rows(specs))

    def test_default_timeline_sub_is_twelve_zeros(self):
        """No plan: timeline_sub is present with no modifiers, like timeline."""
        h = _helpers()
        gw = h._make_gateway()
        h._run(gw._publish_predbat_data())
        payload = h._get_published_payload(gw)
        assert payload["timeline_sub"] == [0] * 12
        assert payload["timeline"] == [0] * 12

    def test_timeline_and_sub_always_published_together(self):
        payload = self._payload([{"state": "Chrg"}])
        assert len(payload["timeline"]) == len(payload["timeline_sub"]) == 12

    def test_each_state_in_payload(self):
        """Every plan state lands in slot 0 of both arrays with the right values."""
        for state in PLAN_RAW_STATES:
            payload = self._payload([{"state": state, "pv_forecast": 0}])
            assert (payload["timeline"][0], payload["timeline_sub"][0]) == timeline_slot_codes(state, 0), state

    def test_evening_hold_then_overnight_charge(self):
        """The reported case: holding at reserve through the peak, then a real cheap charge.

        Before the fix the Hub drew all 12 slots as charging during a 33p hold.
        """
        specs = [{"state": "HoldChrg", "soc_percent": 7}] * 8 + [{"state": "Chrg", "soc_percent": 40}] * 4
        payload = self._payload(specs)
        assert payload["timeline"] == [1] * 12, "timeline must be unchanged for old firmware"
        assert payload["timeline_sub"] == [1] * 8 + [0] * 4

    def test_all_hold_has_no_active_charge_slot(self):
        """Firmware computes time-to-cheap from the first unmodified charge slot; a pure hold has none."""
        payload = self._payload([{"state": "HoldChrg"}] * 12)
        active = [c == TIMELINE_CHARGE and s == TIMELINE_SUB_NONE for c, s in zip(payload["timeline"], payload["timeline_sub"])]
        assert not any(active)

    def test_hold_and_freeze_export(self):
        """Held/frozen export is Demand mode; the modifier marks it so it is not read as exporting."""
        payload = self._payload([{"state": "HoldExp"}] * 3 + [{"state": "FrzExp"}] * 3)
        assert payload["timeline"][:6] == [TIMELINE_EXPORT] * 6
        assert payload["timeline_sub"][:6] == [TIMELINE_SUB_HOLD] * 3 + [TIMELINE_SUB_FREEZE] * 3

    def test_mixed_day(self):
        """A full mixed sequence keeps slot positions aligned between the two arrays."""
        specs = [
            {"state": "Demand", "pv_forecast": 0.0},
            {"state": "Demand", "pv_forecast": 1.2},
            {"state": "FrzChrg"},
            {"state": "Chrg"},
            {"state": "HoldChrg"},
            {"state": "Exp"},
            {"state": "HoldExp"},
            {"state": "FrzExp"},
            {"state": "Demand", "pv_forecast": 0.05},
            {"state": "Chrg"},
            {"state": "Exp"},
            {"state": "HoldChrg", "pv_forecast": 2.0},
        ]
        payload = self._payload(specs)
        assert payload["timeline"] == [4, 3, 1, 1, 1, 2, 2, 2, 4, 1, 2, 1]
        assert payload["timeline_sub"] == [0, 0, 2, 0, 1, 0, 1, 2, 0, 0, 0, 1]

    def test_sub_is_zero_wherever_code_is_not_charge_or_export(self):
        specs = [{"state": s, "pv_forecast": pv} for s in PLAN_RAW_STATES for pv in (0, 1.0)][:12]
        payload = self._payload(specs)
        for code, sub in zip(payload["timeline"], payload["timeline_sub"]):
            if code not in (TIMELINE_CHARGE, TIMELINE_EXPORT):
                assert sub == TIMELINE_SUB_NONE

    def test_sub_only_fills_12_slots(self):
        payload = self._payload([{"state": "HoldChrg"}] * 20)
        assert payload["timeline_sub"] == [TIMELINE_SUB_HOLD] * 12

    def test_short_plan_pads_sub_with_none(self):
        payload = self._payload([{"state": "FrzChrg"}] * 3)
        assert payload["timeline_sub"] == [2, 2, 2] + [0] * 9
        assert payload["timeline"] == [1, 1, 1] + [0] * 9

    def test_old_rows_skipped_in_sub(self):
        """Rows more than 30 minutes old are dropped from both arrays alike."""
        import datetime as dt_mod

        h = _helpers()
        now = dt_mod.datetime.now(dt_mod.timezone.utc)
        rows = [
            {"time": (now - dt_mod.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Chrg", "soc_percent": 50, "pv_forecast": 0},
            {"time": (now + dt_mod.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "HoldChrg", "soc_percent": 50, "pv_forecast": 0},
        ]
        payload = h._get_payload_with_plan(rows)
        assert (payload["timeline"][0], payload["timeline_sub"][0]) == (TIMELINE_CHARGE, TIMELINE_SUB_HOLD)

    def test_bad_time_rows_skipped_in_sub(self):
        import datetime as dt_mod

        h = _helpers()
        now = dt_mod.datetime.now(dt_mod.timezone.utc)
        rows = [
            {"time": "not-a-date", "state": "Chrg", "soc_percent": 50, "pv_forecast": 0},
            {"time": (now + dt_mod.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "FrzExp", "soc_percent": 40, "pv_forecast": 0},
        ]
        payload = h._get_payload_with_plan(rows)
        assert (payload["timeline"][0], payload["timeline_sub"][0]) == (TIMELINE_EXPORT, TIMELINE_SUB_FREEZE)

    def test_bad_pv_does_not_drop_the_publish(self):
        """An unparseable pv_forecast on one row no longer aborts the whole payload."""
        payload = self._payload([{"state": "Demand", "pv_forecast": "n/a"}, {"state": "HoldChrg"}])
        assert payload["timeline"][:2] == [TIMELINE_IMPORT, TIMELINE_CHARGE]
        assert payload["timeline_sub"][:2] == [TIMELINE_SUB_NONE, TIMELINE_SUB_HOLD]

    def test_block_state_still_reports_raw_state(self):
        """The label the Hub shows for the current block is untouched by the modifier."""
        payload = self._payload([{"state": "HoldChrg"}, {"state": "Chrg"}])
        assert payload["block_state"] == "HoldChrg"

    def test_hold_to_charge_change_republishes(self):
        """timeline is identical when a hold becomes a charge, so dedup must see the timeline_sub change."""
        h = _helpers()
        first = h._make_gateway({"predbat.plan_html#raw": {"rows": h._make_rows([{"state": "HoldChrg"}] * 12)}})
        h._run(first._publish_predbat_data())
        assert len(first._published) == 1

        second = h._make_gateway({"predbat.plan_html#raw": {"rows": h._make_rows([{"state": "Chrg"}] * 12)}})
        second._last_predbat_data = first._last_predbat_data
        h._run(second._publish_predbat_data())
        assert len(second._published) == 1, "A hold turning into a charge must reach the Hub"

    def test_hold_to_freeze_change_republishes(self):
        h = _helpers()
        first = h._make_gateway({"predbat.plan_html#raw": {"rows": h._make_rows([{"state": "HoldChrg"}] * 12)}})
        h._run(first._publish_predbat_data())
        second = h._make_gateway({"predbat.plan_html#raw": {"rows": h._make_rows([{"state": "FrzChrg"}] * 12)}})
        second._last_predbat_data = first._last_predbat_data
        h._run(second._publish_predbat_data())
        assert len(second._published) == 1

    def test_identical_plan_is_not_republished(self):
        h = _helpers()
        rows = h._make_rows([{"state": "HoldChrg"}] * 4 + [{"state": "Chrg"}] * 8)
        gw = h._make_gateway({"predbat.plan_html#raw": {"rows": rows}})
        h._run(gw._publish_predbat_data())
        h._run(gw._publish_predbat_data())
        assert len(gw._published) == 1

    def test_module_exports(self):
        """The helper is importable for other display producers."""
        assert gateway.timeline_slot_codes is timeline_slot_codes

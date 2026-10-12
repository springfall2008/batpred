"""Isolation regressions for the optional DEYE native-command preview."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from deye_local_shadow import preview_deye_local, check_joint_deye_commands, DeyeShadowState
from inverter import Inverter
from const import MINUTE_WATT


class ShadowSource:
    """A read-only HA cache whose real service endpoint must never be reached."""

    def __init__(self):
        """Build anonymous six-period observations and unrelated native settings."""
        self.prefix = "test"
        self.manual_soc_max_keep = {}
        self.published = []
        rows = [{field: domain + ".example_" + str(index) + "_" + field for field, domain in (("time", "time"), ("soc", "number"), ("power", "number"), ("charging", "select"))} for index in range(6)]
        self.args = {
            "inverter_type": "SK",
            "deye_shadow": {
                "programs": rows,
                "control": False,
                "hold_verified": False,
                "max_discharge_power_w": 2000,
                "time_of_use": "select.example_tou",
                "work_mode": "select.example_mode",
                "energy_pattern": "select.example_pattern",
                "telemetry_time": "sensor.example_time",
            },
        }
        self.states = {
            "select.example_tou": {"state": "Week"},
            "select.example_mode": {"state": "Zero Export To CT"},
            "select.example_pattern": {"state": "Load First"},
            "sensor.example_time": {"state": datetime.now(timezone.utc).isoformat()},
        }
        for index, row in enumerate(rows):
            self.states[row["time"]] = {"state": "{:02d}:00:00".format(index * 4)}
            self.states[row["charging"]] = {"state": "Grid", "attributes": {"options": ["Disabled", "Grid", "Generator", "Both"]}}
            self.states[row["soc"]] = {"state": "30", "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}}
            self.states[row["power"]] = {"state": "2000", "attributes": {"min": 0, "max": 12000, "step": 1, "unit_of_measurement": "W"}}

    def get_arg(self, name, default=None, **kwargs):
        """Read static configuration without resolving or changing HA entities."""
        return "Europe/London" if name == "timezone" else self.args.get(name, default)

    def get_state_wrapper(self, entity, raw=False, **kwargs):
        """Expose the original cache so accidental in-place mutation is detectable."""
        assert not kwargs.get("refresh"), "Shadow must not request device polling"
        return self.states.get(entity)

    def call_service_wrapper(self, *args, **kwargs):
        """Fail if simulation attempts to use a real control endpoint."""
        raise AssertionError("Physical service reached from shadow")

    def dashboard_item(self, entity, state, attributes):
        """Capture the one public diagnostic, not a hardware operation."""
        self.published.append((entity, state, attributes))


def run_deye_shadow_tests(my_predbat):
    """Check actual translator reuse, isolation and the native immediate hook."""
    tests = 0
    try:
        for target, freeze, hold_verified in ((0, False, False), (45, False, False), (50, True, True), (50, True, False)):
            source = ShadowSource()
            source.args["deye_shadow"]["hold_verified"] = hold_verified
            original = deepcopy((source.args, source.states, source.manual_soc_max_keep))
            inverter = SimpleNamespace(base=source, inverter_type="SK", reserve_percent=10, battery_rate_max_discharge=2500 / MINUTE_WATT)
            report = preview_deye_local(inverter, target, freeze)
            assert original == (source.args, source.states, source.manual_soc_max_keep)
            assert not report["execution_enabled"] and not report["actuator_signals_modified"] and not report["ready_to_activate"]
            assert not report["hardware_confirmed"]
            assert source.published[-1][1] == ("blocked" if freeze and not hold_verified else "simulated")
            if freeze and hold_verified:
                values = report["proposed_writes"]
                assert all(item["value"] == "Disabled" for item in values[:6])
                assert all(item["value"] == 0 for item in values if item["entity_id"].endswith("_power"))
                assert all(item["value"] == 100 for item in values if item["entity_id"].endswith("_soc"))
            tests += 1

        for defect in ("table", "stale", "manual_max", "enable_control", "bad_input"):
            source = ShadowSource()
            settings = source.args["deye_shadow"]
            if defect == "table":
                source.states[settings["programs"][2]["time"]]["state"] = "23:55:00"
            elif defect == "stale":
                source.states[settings["telemetry_time"]]["state"] = "2000-01-01T00:00:00+00:00"
            elif defect == "manual_max":
                source.manual_soc_max_keep = {60: 75}
            elif defect == "enable_control":
                settings["control"] = True
            else:
                source.args["deye_shadow"] = []
            inverter = SimpleNamespace(base=source, inverter_type="SK", reserve_percent=10, battery_rate_max_discharge=2000 / MINUTE_WATT)
            report = preview_deye_local(inverter, 45)
            assert source.published[-1][1] == "blocked" and not report["proposed_writes"]
            if defect == "table":
                repair = report["table_repair_hypothesis"]
                assert repair["translation_succeeded"] and not repair["physical_table_changed"]
                assert repair["required_before_activation"] and repair["proposed_writes"]
                assert source.states[settings["programs"][2]["time"]]["state"] == "23:55:00"
            tests += 1

        # Call the real existing SK immediate-control method, comparing all native
        # service-template calls with preview off/on and a broken publication path.
        reference = None
        for preview in (False, True, "broken"):
            source = ShadowSource()
            if not preview:
                del source.args["deye_shadow"]
            calls = []
            inverter = Inverter.__new__(Inverter)
            inverter.base = source
            inverter.id = 0
            inverter.inverter_type = "SK"
            inverter.deye_local = None
            inverter.reserve_percent = 10
            inverter.battery_rate_max_discharge = 2000 / MINUTE_WATT
            inverter.inv_has_target_soc = True
            inverter.inv_has_solis_energy_control = False
            inverter.soc_percent = 25
            inverter.get_current_charge_rate = lambda: 2000
            inverter.call_service_template = lambda *args, **kwargs: calls.append((args, kwargs)) or True
            inverter.log = lambda *args: None
            if preview == "broken":
                with patch.object(source, "dashboard_item", side_effect=RuntimeError("publication failed")):
                    inverter.adjust_charge_immediate(45)
            else:
                inverter.adjust_charge_immediate(45)
            if reference is None:
                reference = calls
            assert calls == reference and calls
            tests += 1

        source = ShadowSource()
        source.reserve = 1
        source.soc_max = 10
        source.battery_rate_max_discharge = 3000 / MINUTE_WATT
        source.all_active_keep = {125: 30.25}
        source.minutes_now = 120
        before = deepcopy((source.states, source.args, source.all_active_keep))
        rows = source.args["deye_shadow"]["programs"]
        source.states[rows[2]["time"]]["state"] = "23:55:00"
        before = deepcopy((source.states, source.args, source.all_active_keep))
        steps = [
            {"start": 120, "target_soc": 100, "freeze": False, "power_w": None},
            {"start": 125, "target_soc": 0, "freeze": False, "power_w": 1500},
            {"start": 130, "target_soc": 0, "freeze": True, "power_w": None},
            {"start": 135, "target_soc": 0, "freeze": False, "power_w": 1800},
        ]
        calls = []
        original_call = DeyeShadowState.call_service_wrapper

        def record_command(simulated, service, **data):
            """Observe every simulated command without providing real service authority."""
            calls.append((service, dict(data)))
            return original_call(simulated, service, **data)

        with patch.object(DeyeShadowState, "call_service_wrapper", new=record_command):
            checked = check_joint_deye_commands(source, steps)
        assert checked["passed"] and checked["checked_steps"] == 4, checked
        assert len(checked["hypotheses"]) == 2 and not checked["hardware_confirmed"]
        assert before == (source.states, source.args, source.all_active_keep)
        for row in rows:
            assert ("number/set_value", {"entity_id": row["soc"], "value": 31}) in calls
            assert ("number/set_value", {"entity_id": row["power"], "value": 1500}) in calls
        tests += 1
        for bad_power in (-1, 2001, 1500.5, float("nan"), True):
            checked = check_joint_deye_commands(source, [{**steps[1], "power_w": bad_power}])
            assert not checked["passed"] and checked["checked_steps"] == 0, checked
            assert before == (source.states, source.args, source.all_active_keep)
            tests += 1
        print("DEYE shadow: {} isolation checks passed".format(tests))
        return False
    except Exception as exc:
        print("DEYE shadow failed after {} checks: {}".format(tests, exc))
        raise

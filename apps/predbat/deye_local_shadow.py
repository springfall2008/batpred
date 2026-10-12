"""Exercise the DEYE command translator against an isolated HA state copy."""

from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

from deye_local import DeyeLocal


class DeyeShadowState:
    """A narrow in-memory interface with no route to a real control service."""

    def __init__(self, base, settings):
        """Copy the required observations once; never retain the real base object."""
        self.prefix = base.prefix
        self.set_read_only = False
        self.set_read_only_axle = False
        self.manual_soc_max_keep = deepcopy(base.manual_soc_max_keep)
        self.timezone = base.get_arg("timezone", "Europe/London")
        self.states = {}
        self.writes = []
        self.report = {}
        self.errors = []
        self.allowed = {row[field] for row in settings["programs"] for field in ("soc", "power", "charging")}
        entities = {entity for row in settings["programs"] for entity in row.values()}
        entities.update(settings[key] for key in ("time_of_use", "work_mode", "energy_pattern", "telemetry_time"))
        for entity in entities:
            self.states[entity] = deepcopy(base.get_state_wrapper(entity, raw=True))

    def get_arg(self, name, default=None):
        """Expose only the timezone needed to parse the existing clock reading."""
        return self.timezone if name == "timezone" else default

    def get_state_wrapper(self, entity, raw=False):
        """Read a copied observation, not Home Assistant or a device."""
        return deepcopy(self.states.get(entity))

    def call_service_wrapper(self, service, **data):
        """Record the proposed write and simulate acceptance only in this copy."""
        entity = data["entity_id"]
        if entity not in self.allowed or service not in ("number/set_value", "select/select_option"):
            raise ValueError("Unexpected DEYE shadow control")
        value = data["value"] if service == "number/set_value" else data["option"]
        self.writes.append({"service": service, "entity_id": entity, "value": value})
        self.states[entity]["state"] = value
        return True

    def record_status(self, message, had_errors=False):
        """Retain isolated failures without changing the native planner's status."""
        self.errors.append(message)

    def dashboard_item(self, entity, state, attributes):
        """Capture adapter output without publishing intermediate simulation states."""
        self.report = {"state": state, **attributes}


def preview_deye_local(inverter, target_soc, freeze=False):
    """Publish a non-executing trace alongside the unchanged native controller.

    Success means the translator completed against simulated acknowledgements,
    not that the real inverter accepted or executed anything. No capabilities,
    manual targets, windows, rate intents or actual observations are changed.
    """
    base = inverter.base
    settings = base.args.get("deye_shadow")
    if settings is None:
        return None
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execution_enabled": False,
        "actuator_signals_modified": False,
        "ready_to_activate": False,
        "hardware_confirmed": False,
        "target_soc": target_soc,
        "freeze": bool(freeze),
        "native_inverter_type": inverter.inverter_type,
        "simulated_acknowledgements": True,
        "proposed_writes": [],
    }
    state = "blocked"
    try:
        if not isinstance(settings, dict) or settings.get("control", False) is not False:
            raise ValueError("deye_shadow cannot enable physical control")
        settings = DeyeLocal.validate_settings({**settings, "control": True})
        simulated = DeyeShadowState(base, settings)
        fake_inverter = SimpleNamespace(
            base=simulated,
            reserve_percent=inverter.reserve_percent,
            battery_rate_max_discharge=inverter.battery_rate_max_discharge,
            inv_write_and_poll_sleep=0,
            count_register_writes=0,
        )
        adapter = DeyeLocal(fake_inverter)
        adapter.settings = settings
        success = adapter.apply(target_soc, freeze)
        state = "simulated" if success else "blocked"
        report["reason"] = simulated.report.get("reason", "Physical commissioning and executable integration remain required")
        report["mode"] = simulated.report.get("mode", "none")
        report["proposed_writes"] = deepcopy(simulated.writes)
        report["proposed_write_count"] = len(simulated.writes)
        report["hold_verified"] = settings.get("hold_verified", False)
        if not success and "boundaries must be strictly increasing" in report["reason"]:
            # Keep the REAL result blocked. Also exercise the same translator
            # under an explicitly labelled table-repair hypothesis, so a legacy
            # two-boundary SK mapping does not prevent useful offline traces.
            times = [simulated.states[row["time"]]["state"] for row in settings["programs"]]
            minutes = [int(stamp[:2]) * 60 + int(stamp[3:5]) for stamp in times]
            if len(set(minutes)) == 6:
                ordered = [stamp for _, stamp in sorted(zip(minutes, times))]
                for row, stamp in zip(settings["programs"], ordered):
                    simulated.states[row["time"]]["state"] = stamp
                simulated.writes.clear()
                repaired_success = adapter.apply(target_soc, freeze)
                report["table_repair_hypothesis"] = {
                    "physical_table_changed": False,
                    "required_before_activation": True,
                    "ordered_start_times": ordered,
                    "translation_succeeded": repaired_success,
                    "proposed_writes": simulated.writes,
                }
    except Exception as exc:
        # A preview must never interrupt SK's actual charge/stop action.
        report["reason"] = str(exc) if isinstance(exc, ValueError) else "DEYE shadow input or simulation error"
    base.dashboard_item(base.prefix + ".deye_shadow", state=state, attributes=report)
    return report

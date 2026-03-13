"""ESP32 Gateway MQTT component.

Provides full inverter telemetry and control via the ESP32 gateway's
MQTT interface. Registered in COMPONENT_LIST as 'gateway'. This is
the sole data source and control interface for SaaS users with a
gateway — no Home Assistant in the loop.
"""

import json
import time
import uuid
from proto import gateway_status_pb2 as pb


# Entity mapping: protobuf field path → entity name
ENTITY_MAP = {
    # Battery
    "battery.soc_percent": "predbat_gateway_soc",
    "battery.power_w": "predbat_gateway_battery_power",
    "battery.voltage_v": "predbat_gateway_battery_voltage",
    "battery.current_a": "predbat_gateway_battery_current",
    "battery.temperature_c": "predbat_gateway_battery_temp",
    "battery.soh_percent": "predbat_gateway_battery_soh",
    "battery.cycle_count": "predbat_gateway_battery_cycles",
    "battery.capacity_wh": "predbat_gateway_battery_capacity",
    # Power flows
    "pv.power_w": "predbat_gateway_pv_power",
    "grid.power_w": "predbat_gateway_grid_power",
    "grid.voltage_v": "predbat_gateway_grid_voltage",
    "grid.frequency_hz": "predbat_gateway_grid_frequency",
    "load.power_w": "predbat_gateway_load_power",
    "inverter.active_power_w": "predbat_gateway_inverter_power",
    "inverter.temperature_c": "predbat_gateway_inverter_temp",
    # Control
    "control.mode": "predbat_gateway_mode",
    "control.charge_enabled": "predbat_gateway_charge_enabled",
    "control.discharge_enabled": "predbat_gateway_discharge_enabled",
    "control.charge_rate_w": "predbat_gateway_charge_rate",
    "control.discharge_rate_w": "predbat_gateway_discharge_rate",
    "control.reserve_soc": "predbat_gateway_reserve",
    "control.target_soc": "predbat_gateway_target_soc",
    "control.force_power_w": "predbat_gateway_force_power",
    "control.command_expires": "predbat_gateway_command_expires",
    # Schedule
    "schedule.charge_start": "predbat_gateway_charge_start",
    "schedule.charge_end": "predbat_gateway_charge_end",
    "schedule.discharge_start": "predbat_gateway_discharge_start",
    "schedule.discharge_end": "predbat_gateway_discharge_end",
}


class GatewayMQTT:
    """ESP32 Gateway MQTT component for PredBat.

    Static methods handle data transformation (protobuf ↔ entities/commands).
    Instance methods handle MQTT lifecycle and ComponentBase integration.
    """

    @staticmethod
    def decode_telemetry(data):
        """Decode protobuf GatewayStatus → dict of entity_name: value.

        Args:
            data: Raw protobuf bytes from /status topic.

        Returns:
            Dict mapping entity names to values. Uses first inverter entry.
        """
        status = pb.GatewayStatus()
        status.ParseFromString(data)

        if len(status.inverters) == 0:
            return {}

        inv = status.inverters[0]
        entities = {}

        for field_path, entity_name in ENTITY_MAP.items():
            parts = field_path.split(".")
            obj = inv
            for part in parts:
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj is not None:
                entities[entity_name] = obj

        return entities

    @staticmethod
    def build_execution_plan(entries, plan_version, timezone):
        """Build protobuf ExecutionPlan from a list of plan entry dicts.

        Args:
            entries: List of dicts with keys matching PlanEntry fields.
            plan_version: Monotonic version number.
            timezone: IANA timezone string (e.g. "Europe/London").

        Returns:
            Serialized protobuf bytes.
        """
        plan = pb.ExecutionPlan()
        plan.timestamp = int(time.time())
        plan.plan_version = plan_version
        plan.timezone = timezone

        for entry_dict in entries:
            pe = plan.entries.add()
            pe.enabled = entry_dict.get("enabled", True)
            pe.start_hour = entry_dict.get("start_hour", 0)
            pe.start_minute = entry_dict.get("start_minute", 0)
            pe.end_hour = entry_dict.get("end_hour", 0)
            pe.end_minute = entry_dict.get("end_minute", 0)
            pe.mode = entry_dict.get("mode", 0)
            pe.power_w = entry_dict.get("power_w", 0)
            pe.target_soc = entry_dict.get("target_soc", 0)
            pe.days_of_week = entry_dict.get("days_of_week", 0x7F)
            pe.use_native = entry_dict.get("use_native", False)

        return plan.SerializeToString()

    @staticmethod
    def build_command(command, **kwargs):
        """Build JSON command string for ad-hoc control.

        Args:
            command: Command name (set_mode, set_charge_rate, etc.)
            **kwargs: Command-specific fields (mode, power_w, target_soc).

        Returns:
            JSON string ready to publish to /command topic.
        """
        cmd = {
            "command": command,
            "command_id": str(uuid.uuid4()),
        }

        if "mode" in kwargs:
            cmd["mode"] = kwargs["mode"]
        if "power_w" in kwargs:
            cmd["power_w"] = kwargs["power_w"]
        if "target_soc" in kwargs:
            cmd["target_soc"] = kwargs["target_soc"]

        # Mode commands need expires_at (5-minute deadman)
        if command == "set_mode":
            cmd["expires_at"] = int(time.time()) + 300

        return json.dumps(cmd)

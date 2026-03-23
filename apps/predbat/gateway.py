"""ESP32 Gateway MQTT component.

Provides full inverter telemetry and control via the ESP32 gateway's
MQTT interface. Registered in COMPONENT_LIST as 'gateway'. This is
the sole data source and control interface for SaaS users with a
gateway — no Home Assistant in the loop.
"""

import asyncio
import json
import os
import ssl
import time
import uuid
import traceback

from datetime import datetime
from component_base import ComponentBase

try:
    from proto import gateway_status_pb2 as pb

    HAS_PROTOBUF = True
except (ImportError, Exception):
    pb = None
    HAS_PROTOBUF = False

try:
    import aiohttp

    HAS_AIOHTTP = True
except ImportError:
    aiohttp = None
    HAS_AIOHTTP = False

try:
    import aiomqtt

    HAS_AIOMQTT = True
except ImportError:
    HAS_AIOMQTT = False

if not HAS_PROTOBUF:
    raise ImportError("GatewayMQTT requires the 'protobuf' package: pip install protobuf")


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
    "battery.rate_max_w": "predbat_gateway_battery_rate_max",
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

# Plan re-publish interval (seconds)
_PLAN_REPUBLISH_INTERVAL = 5 * 60

# Telemetry staleness threshold (seconds)
_TELEMETRY_STALE_THRESHOLD = 120


class GatewayMQTT(ComponentBase):
    """ESP32 Gateway MQTT component for PredBat.

    Static methods handle data transformation (protobuf <-> entities/commands).
    Instance methods handle MQTT lifecycle and ComponentBase integration.
    """

    def initialize(self, gateway_device_id=None, mqtt_host=None, mqtt_port=8883, mqtt_token=None, **kwargs):
        """Initialize gateway configuration and build MQTT topic strings.

        Args:
            gateway_device_id: The gateway's device ID (e.g. "pbgw_abc123").
            mqtt_host: MQTT broker hostname.
            mqtt_port: MQTT broker port (default 8883 for TLS).
            mqtt_token: JWT access token for MQTT authentication.
            **kwargs: Additional keyword arguments (ignored).
        """
        self.gateway_device_id = gateway_device_id
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_token = mqtt_token
        self.mqtt_token_expires_at = 0

        # MQTT topic strings
        self._topic_base = f"predbat/devices/{gateway_device_id}" if gateway_device_id else "predbat/devices/unknown"
        self.topic_status = f"{self._topic_base}/status"
        self.topic_online = f"{self._topic_base}/online"
        self.topic_schedule = f"{self._topic_base}/schedule"
        self.topic_command = f"{self._topic_base}/command"

        # Runtime state
        self._mqtt_client = None
        self._mqtt_task = None
        self._mqtt_connected = False
        self._gateway_online = False
        self._last_telemetry_time = 0
        self._last_plan_data = None
        self._last_plan_publish_time = 0
        self._plan_version = 0
        self._refresh_in_progress = False
        self._error_count = 0

        # Entity naming prefix
        self.prefix = "predbat"

        # Auto-config state
        self._last_status = None
        self._auto_configured = False
        self._last_published_plan = None
        self._pending_plan = None

        # Register for plan execution hook so we receive plan updates generically
        if hasattr(self.base, "register_hook"):
            self.base.register_hook("on_plan_executed", self._on_plan_executed)

    def _on_plan_executed(self, charge_windows=None, charge_limits=None, export_windows=None, export_limits=None, charge_rate_w=0, discharge_rate_w=0, timezone="Europe/London"):
        """Handle plan execution hook — convert PredBat plan to gateway protobuf format.

        Called by the plugin system after execute_plan() completes. Converts
        the optimizer's charge/export windows into ExecutionPlan entries and
        queues them for MQTT publishing on the next run() cycle.
        """
        plan_entries = []

        # Convert charge windows to plan entries
        for i, window in enumerate(charge_windows or []):
            limit = charge_limits[i] if i < len(charge_limits or []) else 100
            if limit <= 0:
                continue
            start_minutes = window.get("start", 0)
            end_minutes = window.get("end", 0)
            plan_entries.append(
                {
                    "enabled": True,
                    "start_hour": start_minutes // 60,
                    "start_minute": start_minutes % 60,
                    "end_hour": end_minutes // 60,
                    "end_minute": end_minutes % 60,
                    "mode": 1,  # charge
                    "power_w": charge_rate_w,
                    "target_soc": int(limit),
                    "days_of_week": 0x7F,
                    "use_native": True,
                }
            )

        # Convert export/discharge windows to plan entries
        for i, window in enumerate(export_windows or []):
            limit = export_limits[i] if i < len(export_limits or []) else 0
            if limit >= 100:
                continue
            start_minutes = window.get("start", 0)
            end_minutes = window.get("end", 0)
            plan_entries.append(
                {
                    "enabled": True,
                    "start_hour": start_minutes // 60,
                    "start_minute": start_minutes % 60,
                    "end_hour": end_minutes // 60,
                    "end_minute": end_minutes % 60,
                    "mode": 2,  # discharge
                    "power_w": discharge_rate_w,
                    "target_soc": int(limit),
                    "days_of_week": 0x7F,
                    "use_native": True,
                }
            )

        # Cap at 6 entries (firmware PlanEntry entries[6] fixed array)
        MAX_PLAN_ENTRIES = 6
        if len(plan_entries) > MAX_PLAN_ENTRIES:
            self.log(f"Warn: GatewayMQTT: Plan has {len(plan_entries)} entries, capping to {MAX_PLAN_ENTRIES}")
            plan_entries = plan_entries[:MAX_PLAN_ENTRIES]

        # Queue plan for async publishing (picked up by run() cycle)
        if self._plan_changed(plan_entries):
            self._pending_plan = (plan_entries, timezone)

    async def run(self, seconds, first):
        """Component run loop — called every 60 seconds by ComponentBase.start().

        On the first call, starts the background MQTT listener task.
        Subsequent calls perform housekeeping: token refresh checks and
        plan re-publishing if stale.

        Args:
            seconds: Elapsed seconds since component start.
            first: True on the first invocation.

        Returns:
            True on success, False on failure.
        """
        if not HAS_AIOMQTT:
            self.log("Error: GatewayMQTT: aiomqtt not installed — cannot start")
            return False

        if not self.gateway_device_id or not self.mqtt_host:
            self.log("Error: GatewayMQTT: gateway_device_id and mqtt_host are required")
            return False

        if first:
            # Start MQTT listener as a background task
            self._mqtt_task = asyncio.ensure_future(self._mqtt_loop())
            self.log("Info: GatewayMQTT: MQTT listener task started")
            return True

        # Housekeeping on subsequent runs
        try:
            # Check if MQTT task died unexpectedly
            if self._mqtt_task and self._mqtt_task.done():
                exc = self._mqtt_task.exception() if not self._mqtt_task.cancelled() else None
                if exc:
                    self.log(f"Warn: GatewayMQTT: MQTT task died with: {exc}")
                self.log("Info: GatewayMQTT: Restarting MQTT listener task")
                self._mqtt_task = asyncio.ensure_future(self._mqtt_loop())

            # Publish any queued plan from on_plan_executed hook
            if self._pending_plan:
                plan_entries, tz = self._pending_plan
                self._pending_plan = None
                await self.publish_plan(plan_entries, tz)

            # Token refresh check
            await self._check_token_refresh()

            # Re-publish plan if stale
            if self._last_plan_data and self._mqtt_connected:
                elapsed = time.time() - self._last_plan_publish_time
                if elapsed > _PLAN_REPUBLISH_INTERVAL:
                    await self._publish_raw(self.topic_schedule, self._last_plan_data, retain=True)
                    self._last_plan_publish_time = time.time()
                    self.log("Info: GatewayMQTT: Re-published execution plan (stale)")

            # Mark component as alive on successful housekeeping
            self.update_success_timestamp()

        except Exception as e:
            self.log(f"Warn: GatewayMQTT: housekeeping error: {e}")

        return True

    async def _mqtt_loop(self):
        """Continuous MQTT listener with automatic reconnection.

        Connects to the broker with TLS, subscribes to status and online
        topics, and dispatches incoming messages. Reconnects on failure
        with exponential backoff.
        """
        backoff = 5
        max_backoff = 60

        while not self.api_stop:
            try:
                tls_context = ssl.create_default_context()

                client_id = f"predbat-{self.gateway_device_id}-{uuid.uuid4().hex[:8]}"

                async with aiomqtt.Client(
                    hostname=self.mqtt_host,
                    port=self.mqtt_port,
                    username=self.gateway_device_id,
                    password=self.mqtt_token,
                    tls_context=tls_context,
                    identifier=client_id,
                    keepalive=60,
                ) as client:
                    self._mqtt_client = client
                    self._mqtt_connected = True
                    backoff = 5  # Reset backoff on successful connection
                    self.log(f"Info: GatewayMQTT: Connected to {self.mqtt_host}:{self.mqtt_port}")

                    # Subscribe to status and LWT topics
                    await client.subscribe(self.topic_status, qos=1)
                    await client.subscribe(self.topic_online, qos=1)
                    self.log(f"Info: GatewayMQTT: Subscribed to {self.topic_status} and {self.topic_online}")

                    async for message in client.messages:
                        if self.api_stop:
                            break
                        await self._handle_message(message)

            except asyncio.CancelledError:
                self.log("Info: GatewayMQTT: MQTT loop cancelled")
                break
            except Exception as e:
                self._error_count += 1
                self.log(f"Warn: GatewayMQTT: MQTT connection error: {e}")
                self._mqtt_connected = False
                self._mqtt_client = None

                if self.api_stop:
                    break

                self.log(f"Info: GatewayMQTT: Reconnecting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        self._mqtt_connected = False
        self._mqtt_client = None

    async def _handle_message(self, message):
        """Dispatch an incoming MQTT message to the appropriate handler.

        Args:
            message: An aiomqtt.Message with topic and payload.
        """
        topic = str(message.topic)

        try:
            if topic == self.topic_status:
                self._process_telemetry(message.payload)
            elif topic == self.topic_online:
                payload = message.payload.decode("utf-8", errors="replace").strip()
                was_online = self._gateway_online
                self._gateway_online = payload == "1"
                if self._gateway_online != was_online:
                    self.log(f"Info: GatewayMQTT: Gateway is {'online' if self._gateway_online else 'offline'}")
                    self.set_state_wrapper(
                        f"binary_sensor.{self.prefix}_gateway_online",
                        self._gateway_online,
                        attributes={"friendly_name": "Gateway Online"},
                    )
        except Exception as e:
            self._error_count += 1
            self.log(f"Warn: GatewayMQTT: Error handling message on {topic}: {e}")
            self.log(f"Warn: {traceback.format_exc()}")

    def _process_telemetry(self, data):
        """Decode telemetry protobuf and inject per-inverter entities.

        Args:
            data: Raw protobuf bytes from the /status topic.
        """
        try:
            status = pb.GatewayStatus()
            status.ParseFromString(data)
        except Exception as e:
            self._error_count += 1
            self.log(f"Warn: GatewayMQTT: Failed to decode telemetry: {e}")
            return

        if len(status.inverters) == 0:
            return

        self._last_status = status
        self._last_telemetry_time = time.time()
        self.update_success_timestamp()

        if not self.api_started:
            self.api_started = True
            self.log("Info: GatewayMQTT: First telemetry received, API started")

        self._inject_entities(status)

        if not self._auto_configured:
            self.automatic_config()

    def _inject_entities(self, status):
        """Inject inverter entities into PredBat state cache.

        Maps GatewayStatus fields to PredBat entity format using HA-style
        entity naming: {type}.{prefix}_gateway_{suffix}_{attribute}
        """
        device_id = status.device_id
        firmware = status.firmware

        self.set_state_wrapper(
            f"binary_sensor.{self.prefix}_gateway_online",
            True,
            attributes={"device_id": device_id, "firmware": firmware},
        )

        # Inverter time from gateway timestamp
        if status.timestamp > 0 and len(status.inverters) > 0:
            dt = datetime.fromtimestamp(status.timestamp)
            ts_suffix = status.inverters[0].serial[-6:].lower()
            self.set_state_wrapper(
                f"sensor.{self.prefix}_gateway_{ts_suffix}_inverter_time",
                dt.strftime("%Y-%m-%d %H:%M:%S"),
            )

        for inv in status.inverters:
            suffix = inv.serial[-6:].lower() if len(inv.serial) > 6 else inv.serial.lower()
            self._inject_inverter_entities(inv, suffix)

        # EMS aggregate entities (when type is GIVENERGY_EMS)
        inv0 = status.inverters[0]
        if inv0.type == pb.INVERTER_TYPE_GIVENERGY_EMS and inv0.ems.num_inverters > 0:
            pfx = f"{self.prefix}_gateway"
            self.set_state_wrapper(f"sensor.{pfx}_ems_total_soc", inv0.ems.total_soc)
            self.set_state_wrapper(f"sensor.{pfx}_ems_total_charge", inv0.ems.total_charge_w)
            self.set_state_wrapper(f"sensor.{pfx}_ems_total_discharge", inv0.ems.total_discharge_w)
            self.set_state_wrapper(f"sensor.{pfx}_ems_total_grid", inv0.ems.total_grid_w)
            self.set_state_wrapper(f"sensor.{pfx}_ems_total_pv", inv0.ems.total_pv_w)
            self.set_state_wrapper(f"sensor.{pfx}_ems_total_load", inv0.ems.total_load_w)

            for idx, sub in enumerate(inv0.ems.sub_inverters):
                sp = f"sensor.{pfx}_sub{idx}"
                self.set_state_wrapper(f"{sp}_soc", sub.soc)
                self.set_state_wrapper(f"{sp}_battery_power", sub.battery_w)
                self.set_state_wrapper(f"{sp}_pv_power", sub.pv_w)
                self.set_state_wrapper(f"{sp}_grid_power", sub.grid_w)
                self.set_state_wrapper(f"{sp}_temp", sub.temp_c)

    def _inject_inverter_entities(self, inv, suffix):
        """Inject entities for a single inverter using HA-style naming.

        Entity naming pattern: {type}.{prefix}_gateway_{suffix}_{attribute}
        """
        pfx = f"{self.prefix}_gateway_{suffix}"

        bat = inv.battery
        self.set_state_wrapper(f"sensor.{pfx}_soc", bat.soc_percent)
        # Negate battery_power: firmware uses +ve=charging, PredBat uses +ve=discharging
        self.set_state_wrapper(f"sensor.{pfx}_battery_power", -bat.power_w)
        self.set_state_wrapper(f"sensor.{pfx}_battery_voltage", bat.voltage_v)
        self.set_state_wrapper(f"sensor.{pfx}_battery_current", bat.current_a)
        self.set_state_wrapper(f"sensor.{pfx}_battery_temperature", bat.temperature_c)
        if bat.capacity_wh:
            self.set_state_wrapper(f"sensor.{pfx}_battery_capacity", round(bat.capacity_wh / 1000.0, 2))
        if bat.soh_percent > 0:
            self.set_state_wrapper(f"sensor.{pfx}_battery_soh", bat.soh_percent)
        if bat.rate_max_w > 0:
            self.set_state_wrapper(f"sensor.{pfx}_battery_rate_max", bat.rate_max_w)

        self.set_state_wrapper(f"sensor.{pfx}_pv_power", inv.pv.power_w)

        grid = inv.grid
        self.set_state_wrapper(f"sensor.{pfx}_grid_power", grid.power_w)
        if grid.voltage_v:
            self.set_state_wrapper(f"sensor.{pfx}_grid_voltage", grid.voltage_v)
        if grid.frequency_hz:
            self.set_state_wrapper(f"sensor.{pfx}_grid_frequency", grid.frequency_hz)

        self.set_state_wrapper(f"sensor.{pfx}_load_power", inv.load.power_w)

        self.set_state_wrapper(f"sensor.{pfx}_inverter_power", inv.inverter.active_power_w)
        if inv.inverter.temperature_c:
            self.set_state_wrapper(f"sensor.{pfx}_inverter_temperature", inv.inverter.temperature_c)

        control = inv.control
        self.set_state_wrapper(f"switch.{pfx}_charge_enabled", control.charge_enabled)
        self.set_state_wrapper(f"switch.{pfx}_discharge_enabled", control.discharge_enabled)
        self.set_state_wrapper(f"number.{pfx}_charge_rate", control.charge_rate_w)
        self.set_state_wrapper(f"number.{pfx}_discharge_rate", control.discharge_rate_w)
        self.set_state_wrapper(f"number.{pfx}_reserve_soc", control.reserve_soc)
        self.set_state_wrapper(f"number.{pfx}_target_soc", control.target_soc)

        # Schedule times (convert HHMM uint32 → HH:MM:SS string)
        if inv.schedule.ByteSize() > 0:
            sched = inv.schedule
            for field, name in [
                ("charge_start", "charge_slot1_start"),
                ("charge_end", "charge_slot1_end"),
                ("discharge_start", "discharge_slot1_start"),
                ("discharge_end", "discharge_slot1_end"),
            ]:
                hhmm = getattr(sched, field, 0)
                hours = hhmm // 100
                minutes = hhmm % 100
                time_str = f"{hours:02d}:{minutes:02d}:00"
                self.set_state_wrapper(f"select.{pfx}_{name}", time_str)

        # Inverter time (from GatewayStatus timestamp for clock drift detection)
        if self._last_status and self._last_status.timestamp:
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(self._last_status.timestamp, tz=timezone.utc)
            self.set_state_wrapper(f"sensor.{pfx}_inverter_time", dt.strftime("%Y-%m-%d %H:%M:%S"))

        # Battery scaling (depth of discharge) — from firmware pct, apps.yaml override, or 0.95 default
        dod_pct = 0
        if inv.battery.ByteSize() > 0 and inv.battery.depth_of_discharge_pct > 0:
            dod_pct = inv.battery.depth_of_discharge_pct
        if dod_pct <= 0:
            dod_pct = int(self.args.get("gateway_battery_dod_pct", 95)) if isinstance(self.args, dict) else 95
        self.set_state_wrapper(f"sensor.{pfx}_battery_dod", round(dod_pct / 100.0, 3))

        # Energy counters (Wh → kWh)
        if inv.energy.ByteSize() > 0:
            energy = inv.energy
            self.set_state_wrapper(f"sensor.{pfx}_pv_today", round(energy.pv_today_wh / 1000.0, 2))
            self.set_state_wrapper(f"sensor.{pfx}_import_today", round(energy.grid_import_today_wh / 1000.0, 2))
            self.set_state_wrapper(f"sensor.{pfx}_export_today", round(energy.grid_export_today_wh / 1000.0, 2))
            self.set_state_wrapper(f"sensor.{pfx}_load_today", round(energy.consumption_today_wh / 1000.0, 2))
            self.set_state_wrapper(f"sensor.{pfx}_battery_charge_today", round(energy.battery_charge_today_wh / 1000.0, 2))
            self.set_state_wrapper(f"sensor.{pfx}_battery_discharge_today", round(energy.battery_discharge_today_wh / 1000.0, 2))

    def automatic_config(self):
        """Register gateway entities with PredBat's inverter model.

        Called once after first telemetry is received. Maps proto fields
        to PredBat args so the plan engine has data to work with.
        """
        if not self._last_status:
            self.log("Error: GatewayMQTT: automatic_config called with no status data")
            return

        status = self._last_status
        inverters = status.inverters

        if not inverters:
            self.log("Error: GatewayMQTT: no inverters in gateway status")
            return

        num_inverters = len(inverters)
        self.log(f"Info: GatewayMQTT: auto-config: {num_inverters} inverter(s)")

        # Set inverter type
        self.set_arg("inverter_type", ["GWMQTT"] * num_inverters)
        self.set_arg("num_inverters", num_inverters)

        # Per-inverter entity mappings
        soc_entities = []
        soc_max_entities = []
        battery_power_entities = []
        pv_power_entities = []
        grid_power_entities = []
        load_power_entities = []
        charge_rate_entities = []
        discharge_rate_entities = []
        reserve_entities = []
        charge_limit_entities = []
        battery_temp_entities = []
        charge_start_entities = []
        charge_end_entities = []
        discharge_start_entities = []
        discharge_end_entities = []
        charge_enable_entities = []
        discharge_enable_entities = []

        for inv in inverters:
            suffix = inv.serial[-6:].lower()
            base = f"{self.prefix}_gateway_{suffix}"

            soc_entities.append(f"sensor.{base}_soc")
            battery_power_entities.append(f"sensor.{base}_battery_power")
            pv_power_entities.append(f"sensor.{base}_pv_power")
            grid_power_entities.append(f"sensor.{base}_grid_power")
            load_power_entities.append(f"sensor.{base}_load_power")
            charge_rate_entities.append(f"number.{base}_charge_rate")
            discharge_rate_entities.append(f"number.{base}_discharge_rate")
            reserve_entities.append(f"number.{base}_reserve_soc")
            charge_limit_entities.append(f"number.{base}_target_soc")
            battery_temp_entities.append(f"sensor.{base}_battery_temperature")
            charge_start_entities.append(f"select.{base}_charge_slot1_start")
            charge_end_entities.append(f"select.{base}_charge_slot1_end")
            discharge_start_entities.append(f"select.{base}_discharge_slot1_start")
            discharge_end_entities.append(f"select.{base}_discharge_slot1_end")
            charge_enable_entities.append(f"switch.{base}_charge_enabled")
            discharge_enable_entities.append(f"switch.{base}_discharge_enabled")

            # soc_max: from battery capacity entity
            if inv.battery.capacity_wh > 0:
                soc_max_entities.append(f"sensor.{base}_battery_capacity")
            else:
                self.log(f"Warn: GatewayMQTT: inverter {inv.serial} has no battery capacity, using fallback")
                soc_max_entities.append(None)

        # Map entity lists to PredBat args
        self.set_arg("soc_percent", soc_entities)
        self.set_arg("soc_max", soc_max_entities)
        self.set_arg("battery_power", battery_power_entities)
        self.set_arg("pv_power", pv_power_entities)
        self.set_arg("grid_power", grid_power_entities)
        self.set_arg("load_power", load_power_entities)
        self.set_arg("charge_rate", charge_rate_entities)
        self.set_arg("discharge_rate", discharge_rate_entities)
        self.set_arg("reserve", reserve_entities)
        self.set_arg("charge_limit", charge_limit_entities)
        self.set_arg("battery_temperature", battery_temp_entities)
        self.set_arg("charge_start_time", charge_start_entities)
        self.set_arg("charge_end_time", charge_end_entities)
        self.set_arg("discharge_start_time", discharge_start_entities)
        self.set_arg("discharge_end_time", discharge_end_entities)
        self.set_arg("scheduled_charge_enable", charge_enable_entities)
        self.set_arg("scheduled_discharge_enable", discharge_enable_entities)

        # Energy counters (first inverter)
        suffix0 = inverters[0].serial[-6:].lower()
        base0 = f"{self.prefix}_gateway_{suffix0}"
        self.set_arg("pv_today", [f"sensor.{base0}_pv_today"])
        self.set_arg("import_today", [f"sensor.{base0}_import_today"])
        self.set_arg("export_today", [f"sensor.{base0}_export_today"])
        self.set_arg("load_today", [f"sensor.{base0}_load_today"])

        # Battery health (first inverter)
        self.set_arg("battery_temperature_history", f"sensor.{base0}_battery_temperature")
        self.set_arg("battery_scaling", [f"sensor.{base0}_battery_dod"])

        # Battery rate max
        rate_max = inverters[0].battery.rate_max_w
        if rate_max > 0:
            self.set_arg("battery_rate_max", [f"sensor.{base0}_battery_rate_max"])
        else:
            self.log("Warn: GatewayMQTT: no battery_rate_max from gateway, using default 6000W")
            self.set_arg("battery_rate_max", [6000])

        # Inverter time (clock drift detection — uses GatewayStatus.timestamp)
        self.set_arg("inverter_time", [f"sensor.{base0}_inverter_time"])

        # EMS aggregate entities (GivEnergy EMS only)
        inv0 = inverters[0]
        if inv0.type == pb.INVERTER_TYPE_GIVENERGY_EMS and inv0.ems.num_inverters > 0:
            pfx = f"{self.prefix}_gateway"
            self.set_arg("ems_total_soc", f"sensor.{pfx}_ems_total_soc")
            self.set_arg("ems_total_charge", f"sensor.{pfx}_ems_total_charge")
            self.set_arg("ems_total_discharge", f"sensor.{pfx}_ems_total_discharge")
            self.set_arg("ems_total_grid", f"sensor.{pfx}_ems_total_grid")
            self.set_arg("ems_total_pv", f"sensor.{pfx}_ems_total_pv")
            self.set_arg("ems_total_load", f"sensor.{pfx}_ems_total_load")
            # EMS idle slots (discharge pause windows) — use same slot entities for all inverters
            self.set_arg(
                "idle_start_time",
                [f"select.{base0}_discharge_slot1_start" for _ in range(num_inverters)],
            )
            self.set_arg(
                "idle_end_time",
                [f"select.{base0}_discharge_slot1_end" for _ in range(num_inverters)],
            )
            self.log(f"Info: GatewayMQTT: EMS mode with {inv0.ems.num_inverters} sub-inverters")

        # Explicitly set unsupported features to None
        self.set_arg("givtcp_rest", None)
        self.set_arg("charge_rate_percent", None)
        self.set_arg("discharge_rate_percent", None)
        self.set_arg("pause_mode", None)
        self.set_arg("pause_start_time", None)
        self.set_arg("pause_end_time", None)
        self.set_arg("discharge_target_soc", None)

        self._auto_configured = True
        self.log(f"Info: GatewayMQTT: auto-config complete: {num_inverters} inverter(s) registered")

    def _plan_changed(self, plan_entries):
        """Check if the plan differs from the last published plan."""
        if self._last_published_plan is None:
            return True
        return plan_entries != self._last_published_plan

    async def publish_plan(self, plan_entries, timezone_str):
        """Build and publish an ExecutionPlan protobuf to the gateway.

        Args:
            plan_entries: List of plan entry dicts.
            timezone_str: IANA timezone string (e.g. "Europe/London").
        """
        if not self._plan_changed(plan_entries):
            return  # No change, skip publish

        self._plan_version += 1
        data = self.build_execution_plan(plan_entries, plan_version=self._plan_version, timezone=timezone_str)
        self._last_plan_data = data
        self._last_plan_publish_time = time.time()

        if self._mqtt_connected:
            await self._publish_raw(self.topic_schedule, data, retain=True)
            self._last_published_plan = plan_entries
            self.log(f"Info: GatewayMQTT: Published execution plan v{self._plan_version} ({len(plan_entries)} entries)")
        else:
            self.log("Warn: GatewayMQTT: Not connected — plan queued for next publish")

    async def publish_command(self, command, **kwargs):
        """Build and publish a JSON command to the gateway.

        Args:
            command: Command name (set_mode, set_charge_rate, etc.)
            **kwargs: Command-specific fields (mode, power_w, target_soc).
        """
        cmd_json = self.build_command(command, **kwargs)

        if self._mqtt_connected:
            await self._publish_raw(self.topic_command, cmd_json.encode("utf-8"))
            self.log(f"Info: GatewayMQTT: Published command: {command}")
        else:
            self.log(f"Warn: GatewayMQTT: Not connected — cannot publish command: {command}")

    async def _publish_raw(self, topic, payload, retain=False):
        """Publish raw bytes to an MQTT topic.

        Args:
            topic: MQTT topic string.
            payload: Bytes to publish.
            retain: Whether to set the retain flag.
        """
        if self._mqtt_client and self._mqtt_connected:
            await self._mqtt_client.publish(topic, payload, qos=1, retain=retain)

    def is_alive(self):
        """Check if the gateway component is alive and receiving data.

        Returns True when MQTT is connected AND either the gateway is
        offline (LWT says so — we're still connected, just no data) OR
        we've received telemetry within the last 2 minutes.

        Returns:
            bool: True if healthy, False otherwise.
        """
        if not self._mqtt_connected:
            return False

        if not self._gateway_online:
            # Gateway is offline but we're connected to broker — that's OK
            return True

        # Gateway is online — check telemetry freshness
        if self._last_telemetry_time == 0:
            return False

        return (time.time() - self._last_telemetry_time) < _TELEMETRY_STALE_THRESHOLD

    def get_error_count(self):
        """Return the cumulative error count (decode failures, MQTT disconnects, publish failures)."""
        return self._error_count

    async def select_event(self, entity_id, value):
        """Handle select entity changes (mode, schedule times).

        Args:
            entity_id: The entity ID that changed.
            value: The new selected value (HH:MM:SS for times).
        """
        if "gateway_mode" in entity_id:
            mode_map = {"auto": 0, "charge": 1, "discharge": 2, "idle": 3}
            mode_val = mode_map.get(str(value).lower())
            if mode_val is not None:
                await self.publish_command("set_mode", mode=mode_val)
                self.log(f"Info: GatewayMQTT: Mode set to {value} ({mode_val})")
            return

        # Schedule time changes — convert HH:MM:SS to HHMM and send slot command
        time_str = str(value).strip()
        parts = time_str.split(":")
        if len(parts) >= 2:
            try:
                hhmm = int(parts[0]) * 100 + int(parts[1])
            except (ValueError, IndexError):
                return

            if "charge_slot1_start" in entity_id or "charge_slot1_end" in entity_id:
                # Read current charge slot times to send both start and end
                await self._update_charge_slot(entity_id, hhmm)
            elif "discharge_slot1_start" in entity_id or "discharge_slot1_end" in entity_id:
                await self._update_discharge_slot(entity_id, hhmm)

    async def _update_charge_slot(self, entity_id, hhmm):
        """Send set_charge_slot command with updated start or end time."""
        # Determine which field changed
        if "start" in entity_id:
            schedule = {"start": hhmm}
        else:
            schedule = {"end": hhmm}
        await self.publish_command("set_charge_slot", schedule_json=json.dumps(schedule))
        self.log(f"Info: GatewayMQTT: Charge slot update: {schedule}")

    async def _update_discharge_slot(self, entity_id, hhmm):
        """Send set_discharge_slot command with updated start or end time."""
        if "start" in entity_id:
            schedule = {"start": hhmm}
        else:
            schedule = {"end": hhmm}
        await self.publish_command("set_discharge_slot", schedule_json=json.dumps(schedule))
        self.log(f"Info: GatewayMQTT: Discharge slot update: {schedule}")

    async def number_event(self, entity_id, value):
        """Handle number entity changes (e.g. charge rate, target SOC).

        Args:
            entity_id: The entity ID that changed.
            value: The new numeric value.
        """
        try:
            val = int(float(value))
        except (ValueError, TypeError):
            self.log(f"Warn: GatewayMQTT: Invalid number value: {value}")
            return

        if "charge_rate" in entity_id:
            await self.publish_command("set_charge_rate", power_w=val)
        elif "discharge_rate" in entity_id:
            await self.publish_command("set_discharge_rate", power_w=val)
        elif "reserve" in entity_id:
            await self.publish_command("set_reserve", target_soc=val)
        elif "target_soc" in entity_id:
            await self.publish_command("set_target_soc", target_soc=val)

    async def switch_event(self, entity_id, service):
        """Handle switch entity service calls (charge/discharge enable).

        Maps enable/disable to set_mode commands:
        - charge_enabled off → idle mode
        - discharge_enabled off → charge mode (hold, no discharge)
        - either on → auto mode (resume normal operation)

        Args:
            entity_id: The entity ID being controlled.
            service: The service being called (turn_on/turn_off).
        """
        is_on = service == "turn_on"

        if "charge_enabled" in entity_id:
            if is_on:
                await self.publish_command("set_mode", mode=0)  # auto
                self.log("Info: GatewayMQTT: Charge enabled → AUTO mode")
            else:
                await self.publish_command("set_mode", mode=3)  # idle
                self.log("Info: GatewayMQTT: Charge disabled → IDLE mode")
        elif "discharge_enabled" in entity_id:
            if is_on:
                await self.publish_command("set_mode", mode=0)  # auto
                self.log("Info: GatewayMQTT: Discharge enabled → AUTO mode")
            else:
                await self.publish_command("set_mode", mode=1)  # charge (prevents discharge)
                self.log("Info: GatewayMQTT: Discharge disabled → CHARGE mode")

    async def final(self):
        """Cleanup: send AUTO mode, cancel listener task, disconnect."""
        try:
            # Send AUTO mode before disconnecting
            if self._mqtt_connected:
                await self.publish_command("set_mode", mode=0)
                self.log("Info: GatewayMQTT: Sent AUTO mode on shutdown")
        except Exception as e:
            self.log(f"Warn: GatewayMQTT: Error sending final AUTO mode: {e}")

        # Cancel the MQTT listener task
        if self._mqtt_task and not self._mqtt_task.done():
            self._mqtt_task.cancel()
            try:
                await self._mqtt_task
            except (asyncio.CancelledError, Exception):
                pass

        self._mqtt_connected = False
        self._mqtt_client = None
        self.log("Info: GatewayMQTT: Finalized")

    async def _check_token_refresh(self):
        """Check if the MQTT JWT token needs refreshing and refresh if needed.

        Uses the oauth-refresh edge function (same pattern as OAuthMixin) to
        obtain a new access token before the current one expires. The refresh
        token is held server-side in instance secrets.
        """
        if not HAS_AIOHTTP:
            return

        # Extract expiry from JWT if not yet known
        if not self.mqtt_token_expires_at and self.mqtt_token:
            exp = self.extract_jwt_expiry(self.mqtt_token)
            if exp:
                self.mqtt_token_expires_at = exp
            else:
                # Parse failed — set sentinel to avoid retrying every cycle
                self.mqtt_token_expires_at = -1
                self.log("Warn: GatewayMQTT: could not extract JWT expiry, token refresh disabled")
                return

        if self.mqtt_token_expires_at and self.mqtt_token_expires_at > 0 and not self.token_needs_refresh(self.mqtt_token_expires_at):
            return

        if self.mqtt_token_expires_at == -1:
            return

        if self._refresh_in_progress:
            return

        self._refresh_in_progress = True
        try:
            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")
            instance_id = self.args.get("user_id", "") if isinstance(self.args, dict) else ""

            if not supabase_url or not supabase_key or not instance_id:
                self.log("Warn: GatewayMQTT: Token refresh skipped — missing env vars or instance_id")
                return

            url = f"{supabase_url}/functions/v1/oauth-refresh"
            headers = {
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "instance_id": instance_id,
                "provider": "predbat_gateway",
            }

            self.log("Info: GatewayMQTT: Refreshing MQTT token")

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        self.log(f"Warn: GatewayMQTT: Token refresh HTTP {response.status}")
                        return

                    data = await response.json()

            if data.get("success"):
                self.mqtt_token = data["access_token"]
                if data.get("expires_at"):
                    try:
                        if isinstance(data["expires_at"], (int, float)):
                            self.mqtt_token_expires_at = float(data["expires_at"])
                        else:
                            dt = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                            self.mqtt_token_expires_at = dt.timestamp()
                    except (ValueError, AttributeError):
                        self.mqtt_token_expires_at = 0
                self.log("Info: GatewayMQTT: MQTT token refreshed")
            else:
                self.log(f"Warn: GatewayMQTT: Token refresh failed: {data.get('error', 'unknown')}")

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: GatewayMQTT: Token refresh network error: {e}")
        except Exception as e:
            self.log(f"Warn: GatewayMQTT: Token refresh error: {e}")
        finally:
            self._refresh_in_progress = False

    @staticmethod
    def decode_telemetry(data):
        """Decode protobuf GatewayStatus -> dict of entity_name: value.

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
                # Convert Wh to kWh for capacity
                if field_path == "battery.capacity_wh" and obj:
                    obj = round(obj / 1000.0, 2)
                entities[entity_name] = obj

        # EMS aggregate entities (when type is GIVENERGY_EMS)
        if inv.type == pb.INVERTER_TYPE_GIVENERGY_EMS and inv.ems.num_inverters > 0:
            entities["predbat_gateway_ems_total_soc"] = inv.ems.total_soc
            entities["predbat_gateway_ems_total_charge"] = inv.ems.total_charge_w
            entities["predbat_gateway_ems_total_discharge"] = inv.ems.total_discharge_w
            entities["predbat_gateway_ems_total_grid"] = inv.ems.total_grid_w
            entities["predbat_gateway_ems_total_pv"] = inv.ems.total_pv_w
            entities["predbat_gateway_ems_total_load"] = inv.ems.total_load_w

            # Per-sub-inverter entities
            for idx, sub in enumerate(inv.ems.sub_inverters):
                prefix = f"predbat_gateway_sub{idx}"
                entities[f"{prefix}_soc"] = sub.soc
                entities[f"{prefix}_battery_power"] = sub.battery_w
                entities[f"{prefix}_pv_power"] = sub.pv_w
                entities[f"{prefix}_grid_power"] = sub.grid_w
                entities[f"{prefix}_temp"] = sub.temp_c

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
    def extract_jwt_expiry(jwt_token):
        """Extract the exp claim from a JWT without verifying signature.

        Args:
            jwt_token: JWT string (header.payload.signature).

        Returns:
            Unix timestamp of expiry, or 0 if parsing fails.
        """
        import base64

        try:
            parts = jwt_token.split(".")
            if len(parts) != 3:
                return 0
            # Add padding
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            return payload.get("exp", 0)
        except Exception:
            return 0

    @staticmethod
    def token_needs_refresh(exp_epoch):
        """Check if token should be refreshed (1 hour before expiry).

        Args:
            exp_epoch: Unix timestamp of token expiry.

        Returns:
            True if token expires within 1 hour.
        """
        return (exp_epoch - int(time.time())) < 3600

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
        if "schedule_json" in kwargs:
            cmd["schedule_json"] = kwargs["schedule_json"]

        # Mode commands need expires_at (5-minute deadman)
        if command == "set_mode":
            cmd["expires_at"] = int(time.time()) + 300

        return json.dumps(cmd)

"""ESP32 Gateway MQTT component.

Provides full inverter telemetry and control via the ESP32 gateway's
MQTT interface. Registered in COMPONENT_LIST as 'gateway'. This is
the sole data source and control interface for SaaS users with a
gateway — no Home Assistant in the loop.
"""

import asyncio
import datetime
import json
import os
import ssl
import time
import uuid
import traceback
from utils import calc_percent_limit
import pytz as _pytz

from component_base import ComponentBase

try:
    import gateway_status_pb2 as pb

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

# Plan re-publish interval (seconds)
_PLAN_REPUBLISH_INTERVAL = 5 * 60

# Telemetry staleness threshold (seconds)
_TELEMETRY_STALE_THRESHOLD = 120

# Time options for schedule select entities (HH:MM:SS, one per minute across 24 h)
_GATEWAY_BASE_TIME = datetime.datetime.strptime("00:00", "%H:%M")
_GATEWAY_OPTIONS_TIME = [(_GATEWAY_BASE_TIME + datetime.timedelta(seconds=m * 60)).strftime("%H:%M:%S") for m in range(0, 24 * 60, 5)]


# Operating mode selector (0=AUTO, 1=MANUAL; higher values reserved)
GATEWAY_OPERATING_MODE_NAMES = {0: "AUTO", 1: "MANUAL"}
GATEWAY_OPERATING_MODE_VALUES = {"AUTO": 0, "MANUAL": 1}
GATEWAY_OPERATING_MODE_OPTIONS = ["AUTO", "MANUAL"]

PLAN_MODE_AUTO = 0
PLAN_MODE_CHARGE = 1
PLAN_MODE_DISCHARGE = 2

# Entity attribute table — keyed by the semantic suffix used in dashboard_item calls
GATEWAY_ATTRIBUTE_TABLE = {
    # Binary sensors
    "gateway_online": {"friendly_name": "Gateway Online", "device_class": "connectivity"},
    # Timestamps
    "inverter_time": {"friendly_name": "Inverter Time", "icon": "mdi:clock", "device_class": "timestamp"},
    # Battery state
    "soc": {"friendly_name": "Battery SOC", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"},
    "battery_power": {"friendly_name": "Battery Power", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "battery_voltage": {"friendly_name": "Battery Voltage", "icon": "mdi:battery", "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement"},
    "battery_current": {"friendly_name": "Battery Current", "icon": "mdi:battery", "unit_of_measurement": "A", "device_class": "current", "state_class": "measurement"},
    "battery_temperature": {"friendly_name": "Battery Temperature", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "battery_capacity": {"friendly_name": "Battery Capacity", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_soh": {"friendly_name": "Battery State of Health", "icon": "mdi:battery-heart", "unit_of_measurement": "%", "state_class": "measurement"},
    "battery_rate_max": {"friendly_name": "Battery Max Charge Rate", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power"},
    "battery_dod": {"friendly_name": "Battery Depth of Discharge", "icon": "mdi:battery", "unit_of_measurement": "*"},
    "battery_charge_today": {"friendly_name": "Battery Charge Today", "icon": "mdi:battery-plus", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    "battery_discharge_today": {"friendly_name": "Battery Discharge Today", "icon": "mdi:battery-minus", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    # PV
    "pv_power": {"friendly_name": "PV Power", "icon": "mdi:solar-power", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "pv_today": {"friendly_name": "PV Today", "icon": "mdi:solar-power", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    # Grid
    "grid_power": {"friendly_name": "Grid Power", "icon": "mdi:transmission-tower", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "grid_voltage": {"friendly_name": "Grid Voltage", "icon": "mdi:transmission-tower", "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement"},
    "grid_frequency": {"friendly_name": "Grid Frequency", "icon": "mdi:transmission-tower", "unit_of_measurement": "Hz", "device_class": "frequency", "state_class": "measurement"},
    "import_today": {"friendly_name": "Grid Import Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    "export_today": {"friendly_name": "Grid Export Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    # Load
    "load_power": {"friendly_name": "Load Power", "icon": "mdi:flash", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "load_today": {"friendly_name": "Load Today", "icon": "mdi:flash", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    # Inverter
    "inverter_power": {"friendly_name": "Inverter Power", "icon": "mdi:flash", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "inverter_temperature": {"friendly_name": "Inverter Temperature", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    # Control switches
    "charge_enabled": {"friendly_name": "Charge Enabled", "icon": "mdi:battery-plus"},
    "discharge_enabled": {"friendly_name": "Discharge Enabled", "icon": "mdi:battery-minus"},
    # Operating mode selector
    "mode_select": {"friendly_name": "Operating Mode", "icon": "mdi:cog", "options": GATEWAY_OPERATING_MODE_OPTIONS},
    # Control numbers
    "charge_rate": {"friendly_name": "Charge Rate", "icon": "mdi:battery-plus", "unit_of_measurement": "W", "min": 0, "max": 10000, "step": 10},
    "discharge_rate": {"friendly_name": "Discharge Rate", "icon": "mdi:battery-minus", "unit_of_measurement": "W", "min": 0, "max": 10000, "step": 10},
    "reserve_soc": {"friendly_name": "Reserve SOC", "icon": "mdi:battery-lock", "unit_of_measurement": "%", "min": 0, "max": 100, "step": 1},
    "target_soc": {"friendly_name": "Target SOC", "icon": "mdi:battery-arrow-up", "unit_of_measurement": "%", "min": 0, "max": 100, "step": 1},
    # Schedule selects (HH:MM:SS options)
    "charge_slot1_start": {"friendly_name": "Charge Slot 1 Start", "icon": "mdi:clock-start", "options": _GATEWAY_OPTIONS_TIME},
    "charge_slot1_end": {"friendly_name": "Charge Slot 1 End", "icon": "mdi:clock-end", "options": _GATEWAY_OPTIONS_TIME},
    "discharge_slot1_start": {"friendly_name": "Discharge Slot 1 Start", "icon": "mdi:clock-start", "options": _GATEWAY_OPTIONS_TIME},
    "discharge_slot1_end": {"friendly_name": "Discharge Slot 1 End", "icon": "mdi:clock-end", "options": _GATEWAY_OPTIONS_TIME},
    # EMS aggregate entities
    "ems_total_soc": {"friendly_name": "EMS Total SOC", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"},
    "ems_total_charge": {"friendly_name": "EMS Total Charge Power", "icon": "mdi:battery-plus", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "ems_total_discharge": {"friendly_name": "EMS Total Discharge Power", "icon": "mdi:battery-minus", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "ems_total_grid": {"friendly_name": "EMS Total Grid Power", "icon": "mdi:transmission-tower", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "ems_total_pv": {"friendly_name": "EMS Total PV Power", "icon": "mdi:solar-power", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "ems_total_load": {"friendly_name": "EMS Total Load Power", "icon": "mdi:flash", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    # Sub-inverter temperature (entity suffix is "_temp")
    "temp": {"friendly_name": "Sub-Inverter Temperature", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
}


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
        self._suffix_to_serial = {}  # maps entity suffix (last 6 chars of serial) -> full serial string

        # Predbat data publish state (price/timeline for device display)
        self._last_predbat_data = None

        # Register for plan execution hook so we receive plan updates generically
        if hasattr(self.base, "register_hook"):
            self.base.register_hook("on_plan_executed", self._on_plan_executed)

    def _on_plan_executed(self, charge_windows=None, charge_limits=None, export_windows=None, export_limits=None, charge_rate_w=0, discharge_rate_w=0, soc_max=10, reserve=0, timezone="Europe/London"):
        """Handle plan execution hook — convert PredBat plan to gateway protobuf format.

        Called by the plugin system after execute_plan() completes. Converts
        the optimizer's charge/export windows into ExecutionPlan entries and
        queues them for MQTT publishing on the next run() cycle.
        """
        plan_entries = []

        # Convert charge windows to plan entries
        for i, window in enumerate(charge_windows or []):
            limit_kwh = charge_limits[i] if i < len(charge_limits or []) else soc_max
            if limit_kwh <= 0:
                continue
            # XXX: If limit_khw == reserve then its a hold charge, need logic for this to be added
            limit = calc_percent_limit(limit_kwh, soc_max)
            start_minutes = window.get("start", 0)
            end_minutes = window.get("end", 0)
            # Work out hours and minutes
            start_hour = start_minutes // 60
            start_minute = start_minutes % 60
            end_hour = end_minutes // 60
            end_minute = end_minutes % 60
            # Skip zero-duration windows (start == end after midnight wrap)
            if start_hour == end_hour and start_minute == end_minute:
                self.log(f"Warn: GatewayMQTT: Skipping zero-duration charge window {i} ({start_minutes}-{end_minutes})")
                continue
            plan_entries.append(
                {
                    "enabled": True,
                    "start_hour": start_hour,
                    "start_minute": start_minute,
                    "end_hour": end_hour,
                    "end_minute": end_minute,
                    "mode": PLAN_MODE_CHARGE,  # charge
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
            # Work out hours and minutes
            start_hour = start_minutes // 60
            start_minute = start_minutes % 60
            end_hour = end_minutes // 60
            end_minute = end_minutes % 60
            # Skip zero-duration windows
            if start_hour == end_hour and start_minute == end_minute:
                self.log(f"Warn: GatewayMQTT: Skipping zero-duration discharge window {i} ({start_minutes}-{end_minutes})")
                continue
            plan_entries.append(
                {
                    "enabled": True,
                    "start_hour": start_hour,
                    "start_minute": start_minute,
                    "end_hour": end_hour,
                    "end_minute": end_minute,
                    "mode": PLAN_MODE_DISCHARGE,  # discharge
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

        self.log(f"Info: GatewayMQTT: Plan entries ({len(plan_entries)}): " + ", ".join(f"mode={e['mode']} {e['start_hour']:02d}:{e['start_minute']:02d}-{e['end_hour']:02d}:{e['end_minute']:02d}" for e in plan_entries))

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

            # Publish predbat data (price, timeline) to device display
            if self._mqtt_connected and self._auto_configured:
                await self._publish_predbat_data()

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
                    self.dashboard_item(
                        f"binary_sensor.{self.prefix}_gateway_online",
                        self._gateway_online,
                        attributes=GATEWAY_ATTRIBUTE_TABLE.get("gateway_online", {}),
                        app="gateway",
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

        self.dashboard_item(
            f"binary_sensor.{self.prefix}_gateway_online",
            True,
            attributes={**GATEWAY_ATTRIBUTE_TABLE.get("gateway_online", {}), "device_id": device_id, "firmware": firmware},
            app="gateway",
        )

        # Inverter time from gateway timestamp — use first primary inverter's serial
        if status.timestamp > 0 and len(status.inverters) > 0:
            primary_inv = next((inv for inv in status.inverters if inv.primary), status.inverters[0])
            ts_suffix = primary_inv.serial[-6:].lower() if len(primary_inv.serial) > 6 else primary_inv.serial.lower()
            dt = datetime.datetime.fromtimestamp(status.timestamp, tz=self.local_tz)
            self.dashboard_item(
                f"sensor.{self.prefix}_gateway_{ts_suffix}_inverter_time",
                dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
                attributes=GATEWAY_ATTRIBUTE_TABLE.get("inverter_time", {}),
                app="gateway",
            )

        for inv in status.inverters:
            # Skip non-primary inverters — EMS/gateway units report overlapping
            # power readings that would cause doubled values on the dashboard.
            if not inv.primary:
                continue
            suffix = inv.serial[-6:].lower() if len(inv.serial) > 6 else inv.serial.lower()
            self._inject_inverter_entities(inv, suffix)

        # EMS aggregate entities (when type is GIVENERGY_EMS)
        inv0 = status.inverters[0]
        if inv0.type == pb.INVERTER_TYPE_GIVENERGY_EMS and inv0.ems.num_inverters > 0:
            pfx = f"{self.prefix}_gateway"
            self.dashboard_item(f"sensor.{pfx}_ems_total_soc", inv0.ems.total_soc, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ems_total_soc", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_ems_total_charge", inv0.ems.total_charge_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ems_total_charge", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_ems_total_discharge", inv0.ems.total_discharge_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ems_total_discharge", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_ems_total_grid", inv0.ems.total_grid_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ems_total_grid", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_ems_total_pv", inv0.ems.total_pv_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ems_total_pv", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_ems_total_load", inv0.ems.total_load_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ems_total_load", {}), app="gateway")

            for idx, sub in enumerate(inv0.ems.sub_inverters):
                sp = f"sensor.{pfx}_sub{idx}"
                self.dashboard_item(f"{sp}_soc", sub.soc, attributes=GATEWAY_ATTRIBUTE_TABLE.get("soc", {}), app="gateway")
                self.dashboard_item(f"{sp}_battery_power", sub.battery_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_power", {}), app="gateway")
                self.dashboard_item(f"{sp}_pv_power", sub.pv_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("pv_power", {}), app="gateway")
                self.dashboard_item(f"{sp}_grid_power", sub.grid_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("grid_power", {}), app="gateway")
                self.dashboard_item(f"{sp}_temp", sub.temp_c, attributes=GATEWAY_ATTRIBUTE_TABLE.get("temp", {}), app="gateway")

    def _inject_inverter_entities(self, inv, suffix):
        """Inject entities for a single inverter using HA-style naming.

        Entity naming pattern: {type}.{prefix}_gateway_{suffix}_{attribute}
        """
        pfx = f"{self.prefix}_gateway_{suffix}"

        bat = inv.battery
        self.dashboard_item(f"sensor.{pfx}_soc", bat.soc_percent, attributes=GATEWAY_ATTRIBUTE_TABLE.get("soc", {}), app="gateway")
        # Negate battery_power: firmware uses +ve=charging, PredBat uses +ve=discharging
        self.dashboard_item(f"sensor.{pfx}_battery_power", -bat.power_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_power", {}), app="gateway")
        self.dashboard_item(f"sensor.{pfx}_battery_voltage", bat.voltage_v, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_voltage", {}), app="gateway")
        self.dashboard_item(f"sensor.{pfx}_battery_current", bat.current_a, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_current", {}), app="gateway")
        self.dashboard_item(f"sensor.{pfx}_battery_temperature", bat.temperature_c, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_temperature", {}), app="gateway")
        if bat.capacity_wh:
            self.dashboard_item(f"sensor.{pfx}_battery_capacity", round(bat.capacity_wh / 1000.0, 2), attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_capacity", {}), app="gateway")
        if bat.soh_percent > 0:
            self.dashboard_item(f"sensor.{pfx}_battery_soh", bat.soh_percent, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_soh", {}), app="gateway")
        if bat.rate_max_w > 0:
            self.dashboard_item(f"sensor.{pfx}_battery_rate_max", bat.rate_max_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_rate_max", {}), app="gateway")

        self.dashboard_item(f"sensor.{pfx}_pv_power", inv.pv.power_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("pv_power", {}), app="gateway")

        grid = inv.grid
        self.dashboard_item(f"sensor.{pfx}_grid_power", grid.power_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("grid_power", {}), app="gateway")
        if grid.voltage_v:
            self.dashboard_item(f"sensor.{pfx}_grid_voltage", grid.voltage_v, attributes=GATEWAY_ATTRIBUTE_TABLE.get("grid_voltage", {}), app="gateway")
        if grid.frequency_hz:
            self.dashboard_item(f"sensor.{pfx}_grid_frequency", grid.frequency_hz, attributes=GATEWAY_ATTRIBUTE_TABLE.get("grid_frequency", {}), app="gateway")

        self.dashboard_item(f"sensor.{pfx}_load_power", inv.load.power_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("load_power", {}), app="gateway")

        self.dashboard_item(f"sensor.{pfx}_inverter_power", inv.inverter.active_power_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("inverter_power", {}), app="gateway")
        if inv.inverter.temperature_c:
            self.dashboard_item(f"sensor.{pfx}_inverter_temperature", inv.inverter.temperature_c, attributes=GATEWAY_ATTRIBUTE_TABLE.get("inverter_temperature", {}), app="gateway")

        control = inv.control
        self.dashboard_item(f"switch.{pfx}_charge_enabled", "on" if control.charge_enabled else "off", attributes=GATEWAY_ATTRIBUTE_TABLE.get("charge_enabled", {}), app="gateway")
        self.dashboard_item(f"switch.{pfx}_discharge_enabled", "on" if control.discharge_enabled else "off", attributes=GATEWAY_ATTRIBUTE_TABLE.get("discharge_enabled", {}), app="gateway")
        self.dashboard_item(f"number.{pfx}_charge_rate", control.charge_rate_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("charge_rate", {}), app="gateway")
        self.dashboard_item(f"number.{pfx}_discharge_rate", control.discharge_rate_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("discharge_rate", {}), app="gateway")
        self.dashboard_item(f"number.{pfx}_reserve_soc", control.reserve_soc, attributes=GATEWAY_ATTRIBUTE_TABLE.get("reserve_soc", {}), app="gateway")
        self.dashboard_item(f"number.{pfx}_target_soc", control.target_soc, attributes=GATEWAY_ATTRIBUTE_TABLE.get("target_soc", {}), app="gateway")
        mode_name = GATEWAY_OPERATING_MODE_NAMES.get(getattr(control, "mode", 0), "AUTO")
        self.dashboard_item(f"select.{pfx}_mode_select", mode_name, attributes=GATEWAY_ATTRIBUTE_TABLE.get("mode_select", {}), app="gateway")

        # Schedule times (convert HHMM uint32 → HH:MM:SS string)
        # Always set with defaults so PredBat doesn't crash on missing charge_start_time
        sched = inv.schedule if inv.schedule.ByteSize() > 0 else None
        for field, name in [
            ("charge_start", "charge_slot1_start"),
            ("charge_end", "charge_slot1_end"),
            ("discharge_start", "discharge_slot1_start"),
            ("discharge_end", "discharge_slot1_end"),
        ]:
            hhmm = getattr(sched, field, 0) if sched else 0
            hours = hhmm // 100
            minutes = hhmm % 100
            if hours >= 24:
                hours = 0  # firmware sends 2400 for midnight end-of-day
            time_str = f"{hours:02d}:{minutes:02d}:00"
            self.dashboard_item(f"select.{pfx}_{name}", time_str, attributes=GATEWAY_ATTRIBUTE_TABLE.get(name, {}), app="gateway")

        # Inverter time (from GatewayStatus timestamp for clock drift detection)
        if self._last_status and self._last_status.timestamp:
            dt = datetime.datetime.fromtimestamp(self._last_status.timestamp, tz=self.local_tz)
            self.dashboard_item(f"sensor.{pfx}_inverter_time", dt.strftime("%Y-%m-%dT%H:%M:%S%z"), attributes=GATEWAY_ATTRIBUTE_TABLE.get("inverter_time", {}), app="gateway")

        # Battery scaling (depth of discharge) — from firmware pct, apps.yaml override, or 1.0 default
        dod_pct = 0
        if inv.battery.ByteSize() > 0 and inv.battery.depth_of_discharge_pct > 0:
            dod_pct = inv.battery.depth_of_discharge_pct
        if dod_pct <= 0:
            dod_pct = int(self.args.get("gateway_battery_dod_pct", 100)) if isinstance(self.args, dict) else 100
        self.dashboard_item(f"sensor.{pfx}_battery_dod", round(dod_pct / 100.0, 3), attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_dod", {}), app="gateway")

        # Energy counters (Wh → kWh)
        # Always set with defaults so PredBat doesn't crash on missing load_today
        energy = inv.energy if inv.energy.ByteSize() > 0 else None
        self.dashboard_item(f"sensor.{pfx}_pv_today", round(getattr(energy, "pv_today_wh", 0) / 1000.0, 2) if energy else 0, attributes=GATEWAY_ATTRIBUTE_TABLE.get("pv_today", {}), app="gateway")
        self.dashboard_item(f"sensor.{pfx}_import_today", round(getattr(energy, "grid_import_today_wh", 0) / 1000.0, 2) if energy else 0, attributes=GATEWAY_ATTRIBUTE_TABLE.get("import_today", {}), app="gateway")
        self.dashboard_item(f"sensor.{pfx}_export_today", round(getattr(energy, "grid_export_today_wh", 0) / 1000.0, 2) if energy else 0, attributes=GATEWAY_ATTRIBUTE_TABLE.get("export_today", {}), app="gateway")
        self.dashboard_item(f"sensor.{pfx}_load_today", round(getattr(energy, "consumption_today_wh", 0) / 1000.0, 2) if energy else 0, attributes=GATEWAY_ATTRIBUTE_TABLE.get("load_today", {}), app="gateway")
        if energy:
            self.dashboard_item(f"sensor.{pfx}_battery_charge_today", round(energy.battery_charge_today_wh / 1000.0, 2), attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_charge_today", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_battery_discharge_today", round(energy.battery_discharge_today_wh / 1000.0, 2), attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_discharge_today", {}), app="gateway")

    def automatic_config(self):
        """Register gateway entities with PredBat's inverter model.

        Called once after first telemetry is received. Maps proto fields
        to PredBat args so the plan engine has data to work with.
        """
        if not self._last_status:
            self.log("Error: GatewayMQTT: automatic_config called with no status data")
            return

        status = self._last_status
        all_inverters = list(status.inverters)

        if not all_inverters:
            self.log("Error: GatewayMQTT: no inverters in gateway status")
            return

        # Filter to primary inverters with battery data for planning.
        # EMS/gateway units may be primary but lack battery — they provide
        # monitoring entities but shouldn't be registered as plan inverters.
        any_primary = any(inv.primary for inv in all_inverters)
        if any_primary:
            inverters = [inv for inv in all_inverters if inv.primary and inv.battery.ByteSize() > 0]
            if not inverters:
                # All primary inverters lack battery — fall back to any with battery
                inverters = [inv for inv in all_inverters if inv.battery.capacity_wh > 0]
        else:
            # Old firmware: no primary flags, use all with battery data
            inverters = [inv for inv in all_inverters if inv.battery.ByteSize() > 0]
        if not inverters:
            inverters = all_inverters  # last resort

        num_inverters = len(inverters)
        self.log(f"Info: GatewayMQTT: auto-config: {num_inverters} primary inverter(s) of {len(all_inverters)} total")

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
            self._suffix_to_serial[suffix] = inv.serial

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

        # Gateway provides all inverter data directly — disable GE Cloud to
        # prevent double-counting (cloud API + MQTT would sum the same energy).
        self.set_arg("ge_cloud_data", False)
        self.set_arg("ge_cloud_direct", False)

        self._auto_configured = True
        self.log(f"Info: GatewayMQTT: auto-config complete: {num_inverters} inverter(s) registered")

    async def _publish_predbat_data(self):
        """Publish price/timeline data to the gateway device for display.

        Reads PredBat entities (rates, cost_today, ppkwh_today, plan_html raw)
        and publishes JSON to predbat/devices/{device_id}/predbat_data.
        """
        try:
            # Current price from rates entity (current half-hour import rate)
            current_price = float(self.get_state_wrapper(self.prefix + ".rates") or 0)
            cost_today = self.get_state_wrapper(self.prefix + ".cost_today")
            avg_price = self.get_state_wrapper(self.prefix + ".ppkwh_today")

            timeline = [0] * 12  # 12 x 30-min slots, default idle
            block_soc = []  # SOC % values for current plan block only
            block_state_name = ""  # Current block state for display label

            # Build timeline and extract current block SOC from plan JSON rows
            plan_raw = self.get_state_wrapper(self.prefix + ".plan_html", attribute="raw")
            if plan_raw and isinstance(plan_raw, dict) and "rows" in plan_raw:
                rows = plan_raw["rows"]
                now = datetime.datetime.now(self.local_tz)

                state_map = {
                    "Chrg": 1,
                    "FrzChrg": 1,
                    "HoldChrg": 1,
                    "Exp": 2,
                    "HoldExp": 2,
                    "FrzExp": 2,
                }

                slot_idx = 0
                for row in rows:
                    try:
                        row_time = datetime.datetime.strptime(row.get("time", ""), "%Y-%m-%dT%H:%M:%S%z")
                    except (ValueError, TypeError):
                        continue

                    if row_time < now - datetime.timedelta(minutes=30):
                        continue

                    state = row.get("state", "")
                    code = state_map.get(state, 0)
                    if code == 0:
                        pv = float(row.get("pv_forecast", 0) or 0)
                        if pv > 0.1:
                            code = 3  # Solar
                        else:
                            code = 4  # Grid import (consuming)

                    if slot_idx < 12:
                        timeline[slot_idx] = code

                    if slot_idx == 0:
                        block_state_name = state

                    if slot_idx < 24:
                        block_soc.append(int(float(row.get("soc_percent", 0) or 0)))

                    slot_idx += 1

                # Ensure at least 2 points so sparkline renderers have a valid range
                if len(block_soc) == 1:
                    block_soc.append(block_soc[0])

            # Build payload
            saving_start_date = self.get_state_wrapper(self.prefix + ".savings_total_predbat", attribute="start_date")
            saving_total = self.get_state_wrapper(self.prefix + ".savings_total_predbat") or 0
            saving_yesterday = self.get_state_wrapper(self.prefix + ".savings_yesterday_predbat") or 0
            predbat_status = self.get_state_wrapper(self.prefix + ".status") or "Unknown"
            predbat_status_detail = self.get_state_wrapper(self.prefix + ".status", attribute="detail") or ""
            if "error" in predbat_status.lower():
                # Remove long complex text
                predbat_status_detail = predbat_status
                predbat_status = "Server Error"
            if "warn" in predbat_status.lower():
                predbat_status_detail = predbat_status
                predbat_status = "Server warning"

            try:
                saving_total = float(saving_total) / 100.0  # pence → pounds
                saving_yesterday = float(saving_yesterday) / 100.0  # pence → pounds
            except ValueError:
                saving_total = 0
                saving_yesterday = 0
            total_days_of_savings = 1
            if saving_start_date:
                try:
                    start_date = datetime.datetime.strptime(saving_start_date, "%Y-%m-%d").date()
                    total_days_of_savings = max((datetime.date.today() - start_date).days, 1)
                except ValueError:
                    pass
            saving_month_average = round(float(saving_total) * 365 / 12 / total_days_of_savings, 2)

            payload = {
                "current_price": round(float(current_price), 1),
                "avg_price": round(float(avg_price or 0), 1),
                "total_cost": round(float(cost_today or 0) / 100.0, 2),  # pence → pounds
                "timeline": timeline,
                "block_soc": block_soc,
                "block_state": block_state_name,
                "savings_yesterday": saving_yesterday,
                "savings_total": saving_total,
                "savings_total_days": total_days_of_savings,
                "savings_month_average": saving_month_average,
                "predbat_status": predbat_status,
                "predbat_status_detail": predbat_status_detail,
            }

            # Only publish if data changed
            if payload == self._last_predbat_data:
                return

            self._last_predbat_data = dict(payload)  # store copy without timestamp so dedup works next cycle
            payload["timestamp"] = int(time.time())
            payload_json = json.dumps(payload)
            topic = f"{self._topic_base}/predbat_data"

            await self._publish_raw(topic, payload_json.encode(), retain=True)
            self.log(f"Info: Published predbat_data: price={payload['current_price']}p avg={payload['avg_price']}p cost=£{payload['total_cost']}")

        except Exception as e:
            self.log(f"Warn: Failed to publish predbat_data: {e}")

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

    def _serial_from_entity_id(self, entity_id):
        """Extract the full inverter serial from a gateway entity_id.

        Entity IDs follow the pattern {domain}.{prefix}_gateway_{suffix}_{attribute}
        where suffix is the last 6 chars of the inverter serial, lowercased.
        Returns the full serial string, or None if the suffix is not in the map.
        """
        marker = "_gateway_"
        idx = entity_id.find(marker)
        if idx == -1:
            return None
        after = entity_id[idx + len(marker) :]
        # Extract everything up to the next underscore (handles serials shorter than 6 chars)
        underscore = after.find("_")
        suffix = after[:underscore].lower() if underscore != -1 else after.lower()
        serial = self._suffix_to_serial.get(suffix)
        if serial is None:
            self.log(f"Warn: GatewayMQTT: _serial_from_entity_id: no serial found for suffix '{suffix}' in entity '{entity_id}'")
        return serial

    async def select_event(self, entity_id, value):
        """Handle select entity changes (mode, schedule times).

        Args:
            entity_id: The entity ID that changed.
            value: The new selected value (HH:MM:SS for times, or mode name).
        """

        self.log("Info: GatewayMQTT: select_event: entity_id={}, value={}".format(entity_id, value))
        serial = self._serial_from_entity_id(entity_id)
        # Operating mode selector
        if "_mode_select" in entity_id:
            mode_int = GATEWAY_OPERATING_MODE_VALUES.get(str(value).strip(), 0)
            await self.publish_command("set_mode", mode=mode_int, **({"serial": serial} if serial else {}))
            self.log(f"Info: GatewayMQTT: Operating mode set to {value} ({mode_int})")
            return

        # Schedule time changes — convert HH:MM:SS to HHMM and send slot command
        time_str = str(value).strip()
        parts = time_str.split(":")
        if len(parts) >= 2:
            try:
                hhmm = int(parts[0]) * 100 + int(parts[1])
            except (ValueError, IndexError):
                return

            if "_discharge_slot1_start" in entity_id or "_discharge_slot1_end" in entity_id:
                await self._update_discharge_slot(entity_id, hhmm, serial=serial)
            elif "_charge_slot1_start" in entity_id or "_charge_slot1_end" in entity_id:
                # Read current charge slot times to send both start and end
                await self._update_charge_slot(entity_id, hhmm, serial=serial)

    async def _update_charge_slot(self, entity_id, hhmm, serial=None):
        """Send set_charge_slot command with updated start or end time."""
        # Determine which field changed
        if "_start" in entity_id:
            schedule = {"start": hhmm}
        else:
            schedule = {"end": hhmm}
        await self.publish_command("set_charge_slot", schedule_json=json.dumps(schedule), **({"serial": serial} if serial else {}))
        self.log(f"Info: GatewayMQTT: Charge slot update: {schedule}")

    async def _update_discharge_slot(self, entity_id, hhmm, serial=None):
        """Send set_discharge_slot command with updated start or end time."""
        if "_start" in entity_id:
            schedule = {"start": hhmm}
        else:
            schedule = {"end": hhmm}
        await self.publish_command("set_discharge_slot", schedule_json=json.dumps(schedule), **({"serial": serial} if serial else {}))
        self.log(f"Info: GatewayMQTT: Discharge slot update: {schedule}")

    async def number_event(self, entity_id, value):
        """Handle number entity changes (e.g. charge rate, target SOC).

        Args:
            entity_id: The entity ID that changed.
            value: The new numeric value.
        """

        self.log("Info: GatewayMQTT: number_event: entity_id={}, value={}".format(entity_id, value))
        try:
            val = int(float(value))
        except (ValueError, TypeError):
            self.log(f"Warn: GatewayMQTT: Invalid number value: {value}")
            return

        serial = self._serial_from_entity_id(entity_id)
        serial_kwarg = {"serial": serial} if serial else {}
        if "_discharge_rate" in entity_id:
            await self.publish_command("set_discharge_rate", power_w=val, **serial_kwarg)
        elif "_charge_rate" in entity_id:
            await self.publish_command("set_charge_rate", power_w=val, **serial_kwarg)
        elif "_reserve" in entity_id:
            await self.publish_command("set_reserve", target_soc=val, **serial_kwarg)
        elif "_target_soc" in entity_id:
            await self.publish_command("set_target_soc", target_soc=val, **serial_kwarg)

    async def switch_event(self, entity_id, service):
        """Handle switch entity service calls (charge/discharge enable).

        Args:
            entity_id: The entity ID being controlled.
            service: The service being called (turn_on/turn_off).
        """

        self.log("Info: GatewayMQTT: switch_event: entity_id={}, service={}".format(entity_id, service))

        old_value = self.get_state_wrapper(entity_id)
        old_value = True if old_value in [True, "on"] else False
        if service == "turn_on":
            is_on = True
        elif service == "turn_off":
            is_on = False
        elif service == "toggle":
            is_on = not old_value
        else:
            self.log("Warn: GatewayMQTT: switch_event: Unsupported service={} for entity_id={}".format(service, entity_id))
            return

        serial = self._serial_from_entity_id(entity_id)
        serial_kwarg = {"serial": serial} if serial else {}
        if "_charge_enabled" in entity_id:
            await self.publish_command("set_charge_enable", enable=is_on, **serial_kwarg)
            self.log(f"Info: GatewayMQTT: Charge {'enabled' if is_on else 'disabled'}")
        elif "_discharge_enabled" in entity_id:
            await self.publish_command("set_discharge_enable", enable=is_on, **serial_kwarg)
            self.log(f"Info: GatewayMQTT: Discharge {'enabled' if is_on else 'disabled'}")

    async def final(self):
        """Cleanup: cancel listener task, disconnect."""
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
                            dt = datetime.datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
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
    def iana_to_posix_tz(iana_tz):
        """Convert an IANA timezone name to a POSIX TZ string for ESP32 firmware.

        TZif v2/v3 binary files embed the POSIX TZ string as the last newline-delimited
        record. This method looks up the tzfile in the pytz package's bundled
        ``zoneinfo`` directory and reads the string directly — no lookup table
        required, works for any valid zone.

        Args:
            iana_tz: IANA timezone string (e.g. "Europe/London").

        Returns:
            POSIX TZ string (e.g. "GMT0BST,M3.5.0/1,M10.5.0"), or "UTC0" on failure.
        """

        iana_tz = str(iana_tz)
        rel_path = iana_tz.replace("/", os.sep)
        pytz_zones = os.path.join(os.path.dirname(_pytz.__file__), "zoneinfo")
        path = os.path.join(pytz_zones, rel_path)

        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                # TZif v2/v3: POSIX string is appended after the main data block as \nSTRING\n
                if data[:4] == b"TZif" and data[4:5] in (b"2", b"3") and data[-1:] == b"\n":
                    nl = data.rfind(b"\n", 0, -1)
                    if nl >= 0:
                        posix = data[nl + 1 : -1].decode("ascii", errors="replace").strip()
                        if posix:
                            return posix
            except OSError:
                pass

        # Fall back: derive a no-DST POSIX string from the pytz UTC offset
        try:
            tz = _pytz.timezone(iana_tz)
            # Probe both hemispheres' winters to find standard time (smaller UTC offset).
            # Jan is winter in NH but summer in SH, so we take whichever probe gives the
            # smaller (less positive) offset — that is always the non-DST offset.
            dt_jan = datetime.datetime(2024, 1, 15, 12, 0, 0)
            dt_jul = datetime.datetime(2024, 7, 15, 12, 0, 0)
            off_jan = tz.utcoffset(dt_jan).total_seconds()
            off_jul = tz.utcoffset(dt_jul).total_seconds()
            std_dt = dt_jan if off_jan <= off_jul else dt_jul
            offset = tz.utcoffset(std_dt)
            total_minutes = int(offset.total_seconds() / 60)
            # POSIX sign is opposite to UTC offset; use divmod to avoid floor-division
            # errors on negative fractional offsets (e.g. UTC-03:30 → -210 min)
            posix_total_minutes = -total_minutes
            posix_hours, posix_mins = divmod(abs(posix_total_minutes), 60)
            if posix_total_minutes < 0:
                posix_hours = -posix_hours
            abbr = tz.tzname(std_dt) or "TZ"
            if posix_mins:
                return "{}{:d}:{:02d}".format(abbr, posix_hours, posix_mins)
            return "{}{:d}".format(abbr, posix_hours)
        except Exception:
            return "UTC0"

    @staticmethod
    def build_execution_plan(entries, plan_version, timezone):
        """Build protobuf ExecutionPlan from a list of plan entry dicts.

        Args:
            entries: List of dicts with keys matching PlanEntry fields.
            plan_version: Monotonic version number.
            timezone: IANA timezone string (e.g. "Europe/London") — converted to POSIX format internally.

        Returns:
            Serialized protobuf bytes.
        """
        plan = pb.ExecutionPlan()
        plan.timestamp = int(time.time())
        plan.plan_version = plan_version
        plan.timezone = GatewayMQTT.iana_to_posix_tz(timezone)

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
            command: Command name (set_charge_enable, set_charge_rate, etc.)
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
        if "enable" in kwargs:
            cmd["enable"] = bool(kwargs["enable"])
        if "serial" in kwargs:
            cmd["serial"] = kwargs["serial"]

        return json.dumps(cmd)

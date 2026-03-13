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

import aiohttp
from datetime import datetime
from component_base import ComponentBase
from proto import gateway_status_pb2 as pb

try:
    import aiomqtt

    HAS_AIOMQTT = True
except ImportError:
    HAS_AIOMQTT = False


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

# Plan re-publish interval (seconds)
_PLAN_REPUBLISH_INTERVAL = 5 * 60

# Telemetry staleness threshold (seconds)
_TELEMETRY_STALE_THRESHOLD = 120


class GatewayMQTT(ComponentBase):
    """ESP32 Gateway MQTT component for PredBat.

    Static methods handle data transformation (protobuf <-> entities/commands).
    Instance methods handle MQTT lifecycle and ComponentBase integration.
    """

    def initialize(self, gateway_device_id=None, mqtt_host=None, mqtt_port=8883, mqtt_token=None, mqtt_refresh_token=None, **kwargs):
        """Initialize gateway configuration and build MQTT topic strings.

        Args:
            gateway_device_id: The gateway's device ID (e.g. "pbgw_abc123").
            mqtt_host: MQTT broker hostname.
            mqtt_port: MQTT broker port (default 8883 for TLS).
            mqtt_token: JWT access token for MQTT authentication.
            mqtt_refresh_token: Refresh token for token renewal.
            **kwargs: Additional keyword arguments (ignored).
        """
        self.gateway_device_id = gateway_device_id
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_token = mqtt_token
        self.mqtt_refresh_token = mqtt_refresh_token
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

            # Token refresh check
            await self._check_token_refresh()

            # Re-publish plan if stale
            if self._last_plan_data and self._mqtt_connected:
                elapsed = time.time() - self._last_plan_publish_time
                if elapsed > _PLAN_REPUBLISH_INTERVAL:
                    await self._publish_raw(self.topic_schedule, self._last_plan_data, retain=True)
                    self._last_plan_publish_time = time.time()
                    self.log("Info: GatewayMQTT: Re-published execution plan (stale)")

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
        max_backoff = 120

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
                    state = "online" if self._gateway_online else "offline"
                    self.log(f"Info: GatewayMQTT: Gateway is {state}")
                    self.set_state_wrapper(
                        f"sensor.{self.prefix}predbat_gateway_online",
                        state,
                        attributes={"friendly_name": "Gateway Online"},
                    )
        except Exception as e:
            self._error_count += 1
            self.log(f"Warn: GatewayMQTT: Error handling message on {topic}: {e}")
            self.log(f"Warn: {traceback.format_exc()}")

    def _process_telemetry(self, data):
        """Decode telemetry protobuf and publish entities via set_state_wrapper.

        Args:
            data: Raw protobuf bytes from the /status topic.
        """
        entities = self.decode_telemetry(data)
        if not entities:
            return

        self._last_telemetry_time = time.time()
        self.update_success_timestamp()

        if not self.api_started:
            self.api_started = True
            self.log("Info: GatewayMQTT: First telemetry received, API started")

        for entity_name, value in entities.items():
            self.set_state_wrapper(
                f"sensor.{self.prefix}{entity_name}",
                value,
                attributes={"friendly_name": entity_name.replace("predbat_gateway_", "Gateway ").replace("_", " ").title()},
            )

    async def publish_plan(self, plan_entries, timezone_str):
        """Build and publish an ExecutionPlan protobuf to the gateway.

        Args:
            plan_entries: List of plan entry dicts.
            timezone_str: IANA timezone string (e.g. "Europe/London").
        """
        self._plan_version += 1
        data = self.build_execution_plan(plan_entries, plan_version=self._plan_version, timezone=timezone_str)
        self._last_plan_data = data
        self._last_plan_publish_time = time.time()

        if self._mqtt_connected:
            await self._publish_raw(self.topic_schedule, data, retain=True)
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
        """Handle select entity changes (e.g. mode selection).

        Args:
            entity_id: The entity ID that changed.
            value: The new selected value.
        """
        if "gateway_mode" in entity_id:
            mode_map = {"auto": 0, "charge": 1, "discharge": 2, "idle": 3}
            mode_val = mode_map.get(str(value).lower())
            if mode_val is not None:
                await self.publish_command("set_mode", mode=mode_val)
                self.log(f"Info: GatewayMQTT: Mode set to {value} ({mode_val})")

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
        """Handle switch entity service calls. Stub for v1.

        Args:
            entity_id: The entity ID being controlled.
            service: The service being called (turn_on/turn_off).
        """
        pass

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

        Uses the Supabase edge function (same pattern as OAuthMixin) to
        obtain a new access token before the current one expires.
        """
        if not self.mqtt_refresh_token:
            return

        # Extract expiry from JWT if not yet known
        if not self.mqtt_token_expires_at and self.mqtt_token:
            self.mqtt_token_expires_at = self.extract_jwt_expiry(self.mqtt_token)

        if self.mqtt_token_expires_at and not self.token_needs_refresh(self.mqtt_token_expires_at):
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

            url = f"{supabase_url}/functions/v1/refresh-mqtt-token"
            headers = {
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "instance_id": instance_id,
                "refresh_token": self.mqtt_refresh_token,
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
                if data.get("refresh_token"):
                    self.mqtt_refresh_token = data["refresh_token"]
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

        # Mode commands need expires_at (5-minute deadman)
        if command == "set_mode":
            cmd["expires_at"] = int(time.time()) + 300

        return json.dumps(cmd)

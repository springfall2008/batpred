"""ESP32 Gateway MQTT component.

Provides full inverter telemetry and control via the ESP32 gateway's
MQTT interface. Registered in COMPONENT_LIST as 'gateway'. This is
the sole data source and control interface for SaaS users with a
gateway — no Home Assistant in the loop.
"""

import asyncio
import datetime
import json
import math
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
    from google.protobuf import json_format as _pb_json_format

    HAS_PROTOBUF = True
except (ImportError, Exception):
    pb = None
    _pb_json_format = None
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
    "inverter_rate_max": {"friendly_name": "Inverter Max Rate", "icon": "mdi:flash", "unit_of_measurement": "W", "device_class": "power"},
    # Control switches
    "charge_enabled": {"friendly_name": "Charge Enabled", "icon": "mdi:battery-plus"},
    "discharge_enabled": {"friendly_name": "Discharge Enabled", "icon": "mdi:battery-minus"},
    # Operating mode selector
    "mode_select": {"friendly_name": "Operating Mode", "icon": "mdi:cog", "options": GATEWAY_OPERATING_MODE_OPTIONS},
    # Control numbers
    "export_limit_w": {"friendly_name": "Export Limit", "icon": "mdi:transmission-tower", "unit_of_measurement": "W", "device_class": "power"},
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
    # EV charger (OCPP) — device-level entities, present when a charge point is connected
    "ev_online": {"friendly_name": "EV Charger Online", "icon": "mdi:ev-station", "device_class": "connectivity"},
    "ev_connected": {"friendly_name": "EV Car Connected", "icon": "mdi:car-electric", "device_class": "plug"},
    "ev_session_active": {"friendly_name": "EV Charging Active", "icon": "mdi:ev-station", "device_class": "battery_charging"},
    "ev_status": {"friendly_name": "EV Charger Status", "icon": "mdi:ev-station"},
    "ev_power": {"friendly_name": "EV Charge Power", "icon": "mdi:ev-station", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "ev_session_energy": {"friendly_name": "EV Session Energy", "icon": "mdi:ev-station", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total"},
    "ev_soc": {"friendly_name": "EV Battery SOC", "icon": "mdi:car-electric", "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"},
    "ev_current_limit": {"friendly_name": "EV Current Limit", "icon": "mdi:current-ac", "unit_of_measurement": "A", "device_class": "current"},
    "ev_max_current": {"friendly_name": "EV Max Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "device_class": "current"},
    "ev_voltage": {"friendly_name": "EV Supply Voltage", "icon": "mdi:flash", "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement"},
    "ev_eco_mode": {"friendly_name": "EV Eco Mode", "icon": "mdi:leaf"},
    "ev_charge_rate": {"friendly_name": "EV Charge Rate", "icon": "mdi:ev-station", "unit_of_measurement": "kW", "device_class": "power"},
}


def extract_rate_anchors(rate_min, rate_max, import_rate, export_rate):
    """Validate and round the four rate anchors for the device payload.

    Returns a dict with rate_min / rate_max / import_rate / export_rate rounded
    to 0.01 p/kWh (matching the marginal-cost matrix precision), or None if any
    value is missing, non-numeric, or non-finite (NaN/Inf) so the device falls
    back to its plan-aware RAG and the payload never carries invalid JSON
    tokens.
    """
    try:
        rate_min = round(float(rate_min), 2)
        rate_max = round(float(rate_max), 2)
        rate_import = round(float(import_rate), 2)
        rate_export = round(float(export_rate), 2)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(rate_min) and math.isfinite(rate_max) and math.isfinite(rate_import) and math.isfinite(rate_export)):
        return None
    return {"rate_min": rate_min, "rate_max": rate_max, "import_rate": rate_import, "export_rate": rate_export}


class GatewayMQTT(ComponentBase):
    """ESP32 Gateway MQTT component for PredBat.

    Static methods handle data transformation (protobuf <-> entities/commands).
    Instance methods handle MQTT lifecycle and ComponentBase integration.
    """

    def initialize(self, gateway_device_id=None, mqtt_host=None, mqtt_port=8883, mqtt_token=None, gateway_inverter_serial=None, gateway_evc_automatic=False, gateway_evc_control=False, **kwargs):
        """Initialize gateway configuration and build MQTT topic strings.

        Args:
            gateway_device_id: The gateway's device ID (e.g. "pbgw_abc123").
            mqtt_host: MQTT broker hostname.
            mqtt_port: MQTT broker port (default 8883 for TLS).
            mqtt_token: JWT access token for MQTT authentication.
            gateway_inverter_serial: Optional serial number(s) to restrict which inverters are configured.
                If not set, all inverters are used. May be a string or a list of strings.
            gateway_evc_automatic: When True, automatically register a connected OCPP EV charger as a
                PredBat car so the optimizer plans for it. Off by default so existing gateway users are
                unaffected. Set to ``true`` in apps.yaml to enable.
            gateway_evc_control: When True (requires gateway_evc_automatic), check once per minute whether
                the current time falls inside a planned car-charging window and send RemoteStartTransaction
                plus SetChargingProfile on window entry, or RemoteStopTransaction on window exit. When
                enabled, car_charging_now is omitted from auto-config to prevent a feedback loop.
            **kwargs: Additional keyword arguments (ignored).
        """
        self.gateway_device_id = gateway_device_id
        self.gateway_evc_automatic = bool(gateway_evc_automatic)
        self.gateway_evc_control = bool(gateway_evc_control)
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_token = mqtt_token

        # Normalise serial filter to a list (or None if not set)
        if gateway_inverter_serial is None:
            self.gateway_inverter_serial = []
        elif isinstance(gateway_inverter_serial, list):
            self.gateway_inverter_serial = gateway_inverter_serial
        else:
            if isinstance(gateway_inverter_serial, str) and (gateway_inverter_serial.startswith("{") or gateway_inverter_serial.startswith("[")):
                try:
                    parsed = json.loads(gateway_inverter_serial)
                    if isinstance(parsed, list):
                        self.gateway_inverter_serial = [str(s) for s in parsed]
                    else:
                        self.gateway_inverter_serial = [str(parsed)]
                except json.JSONDecodeError:
                    self.gateway_inverter_serial = [str(gateway_inverter_serial)]
            else:
                self.gateway_inverter_serial = [gateway_inverter_serial]
        self.mqtt_token_expires_at = 0

        # MQTT topic strings
        self._topic_base = f"predbat/devices/{gateway_device_id}" if gateway_device_id else "predbat/devices/unknown"
        self.topic_status = f"{self._topic_base}/status"
        self.topic_online = f"{self._topic_base}/online"
        self.topic_schedule = f"{self._topic_base}/schedule"
        self.topic_command = f"{self._topic_base}/command"
        self.topic_ev_command = f"{self._topic_base}/ev/command"

        # Runtime state
        self._mqtt_client = None
        self._mqtt_task = None
        self._mqtt_connected = False
        self._gateway_online = False
        self._last_telemetry_time = 0
        self._last_plan_data = None
        self._last_plan_publish_time = 0
        # Entries and timezone of the last built plan, kept so the periodic re-publish
        # can rebuild the protobuf with a fresh timestamp (re-sending the cached bytes
        # would keep the original timestamp and the device would think the plan is stale)
        self._last_plan_entries = None
        self._last_plan_timezone = None
        self._plan_version = 0
        self._refresh_in_progress = False
        self._error_count = 0

        # Entity naming prefix
        self.prefix = "predbat"

        # Auto-config state
        self._last_status = None
        self._auto_configured = False
        self._configured_inverter_serials = frozenset()  # serials discovered at the last auto-config
        self._configured_ev_chargers = frozenset()  # EV charge point ids registered at the last auto-config
        self._last_published_plan = None
        self._pending_plan = None
        self._ev_windows: list = []  # parsed (start_dt, end_dt) charge windows from HA plan attribute
        self._ev_charging_active: bool = False  # last commanded state; avoids duplicate start/stop sends
        self._ev_max_current: dict = {}  # charge_point_id → last known max_current_a from telemetry
        self._suffix_to_serial = {}  # maps entity suffix (last 6 chars of serial) -> full serial string
        self._command_id = 0  # incrementing counter included in every published command

        # Predbat data publish state (price/timeline for device display)
        self._last_predbat_data = None

        # Track which inverter serials have received an inverter_reset command
        self._inverter_reset_done = set()

        # Last read-only state sent to the gateway (None until the first send,
        # so the current state is always pushed once on startup)
        self._last_read_only = None
        # Monotonic-ish timestamp of the last set_read_only send, used to force a
        # periodic re-send so the gateway re-syncs even if a command was missed
        self._last_read_only_sent_time = 0

        # Set once the first MQTT connection attempt has completed (success or failure)
        self._first_connection_attempted = False

        # Verbose telemetry/command logging for debugging. Enable by setting
        # `gateway_debug: True` in apps.yaml; off by default to avoid log spam.
        if isinstance(self.args, dict):
            debug_val = self.args.get("gateway_debug", False)
            self._debug = str(debug_val).strip().lower() in ["on", "true", "yes", "enabled", "enable", "connected", "1"]
        else:
            self._debug = False

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

        # Reserve as a percentage — used to detect freeze charge (charge limit == reserve)
        reserve_percent = calc_percent_limit(reserve, soc_max)

        # Convert charge windows to plan entries
        for i, window in enumerate(charge_windows or []):
            limit_kwh = charge_limits[i] if i < len(charge_limits or []) else soc_max
            if limit_kwh <= 0:
                continue
            limit = calc_percent_limit(limit_kwh, soc_max)
            charge_power_w = charge_rate_w
            # Freeze charge (charge limit == reserve): hold the current SoC rather than
            # charge to a target. There is no freeze mode, so express it as a charge entry
            # with rate 0 and target 100% — the inverter holds (won't discharge) and solar
            # may still charge it, but no grid charging happens.
            if limit == reserve_percent:
                limit = 100
                charge_power_w = 0
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
                    "power_w": charge_power_w,
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
            target_soc = int(limit)
            export_power_w = discharge_rate_w
            # Freeze export (export limit == 99): hold SoC and export only surplus PV
            # rather than force-discharge. There is no freeze mode, so express it as a
            # discharge entry with rate 0 and target = reserve. Match core's exact == 99
            # check — a fractional limit (e.g. 99.5) is a normal export, not a freeze.
            if limit == 99:
                target_soc = reserve_percent
                export_power_w = 0
            else:
                # Low-power export: the planner encodes the chosen export rate in the
                # fractional part of the limit (e.g. 5.3 -> 70% of max), and execute applies
                # rate_scale = 1 - frac (see plan.py / execute.py). With low power off there
                # is no fraction, so this is full rate.
                export_power_w = round(discharge_rate_w * (1 - (limit - int(limit))))
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
                    "power_w": export_power_w,
                    "target_soc": int(target_soc),
                    "days_of_week": 0x7F,
                    "use_native": True,
                }
            )

        # Sort entries chronologically (earliest start first) — charge and export windows
        # are appended in separate passes above, so without this the gateway would receive
        # them grouped by type rather than in time order. Sorting before the cap also keeps
        # the earliest slots when there are more than the firmware can hold. Tomorrow's
        # windows keep hours >= 24 deliberately so they stay distinct from today's and don't
        # overlap, and so the sort orders today's 22:00 before tomorrow's 06:00 (hour 30).
        plan_entries.sort(key=lambda e: (e["start_hour"], e["start_minute"]))

        # Cap at 6 entries (firmware PlanEntry entries[6] fixed array)
        MAX_PLAN_ENTRIES = 6
        if len(plan_entries) > MAX_PLAN_ENTRIES:
            plan_entries = plan_entries[:MAX_PLAN_ENTRIES]

        self.log(f"Info: GatewayMQTT: Plan entries ({len(plan_entries)}): " + ", ".join(f"mode={e['mode']} {e['start_hour']:02d}:{e['start_minute']:02d}-{e['end_hour']:02d}:{e['end_minute']:02d}" for e in plan_entries))

        # Queue plan for async publishing (picked up by run() cycle)
        if self._plan_changed(plan_entries):
            self._pending_plan = (plan_entries, timezone)

    def _refresh_ev_windows(self):
        """Re-read the PredBat car-charging-slot planned windows from HA and cache them.

        Called once per run() cycle so the minute-level check always has up-to-date window
        boundaries without polling HA every second.  The ``planned`` attribute of
        ``binary_sensor.<prefix>_car_charging_slot`` is a list of ``{start, end, ...}`` dicts
        with datetime strings in ``"%m-%d %H:%M:%S"`` format (produced by output.py).

        Datetimes are localized using ``self.local_tz`` so comparisons respect the component
        timezone and DST transitions.  Year boundaries are handled: if a parsed start is more
        than 23 hours in the past (i.e. the plan was built on Dec 31 and contains Jan 1
        windows), the year is bumped forward.  Similarly, if end falls before start after
        localization the end year is incremented to handle windows that straddle midnight
        on New Year's Eve.
        """
        planned = self.get_state_wrapper(f"binary_sensor.{self.prefix}_car_charging_slot", attribute="planned") or []
        now = datetime.datetime.now(self.local_tz)
        current_year = now.year
        windows = []
        for w in planned:
            try:
                start_naive = datetime.datetime.strptime(w["start"], "%m-%d %H:%M:%S").replace(year=current_year)
                end_naive = datetime.datetime.strptime(w["end"], "%m-%d %H:%M:%S").replace(year=current_year)
                start_dt = self.local_tz.localize(start_naive)
                end_dt = self.local_tz.localize(end_naive)
                # If start is far in the past the plan crossed a year boundary (Dec 31 → Jan 1)
                if start_dt < now - datetime.timedelta(hours=23):
                    start_dt = start_dt.replace(year=start_dt.year + 1)
                    end_dt = end_dt.replace(year=end_dt.year + 1)
                elif end_dt < start_dt:
                    # end crossed into the new year but start did not (e.g. 23:30 → 00:30)
                    end_dt = end_dt.replace(year=end_dt.year + 1)
                windows.append((start_dt, end_dt))
            except (KeyError, ValueError):
                continue
        self._ev_windows = windows

    def _should_ev_charge_now(self):
        """Return True if the current local time falls inside any planned charge window."""
        now = datetime.datetime.now(self.local_tz)
        for start_dt, end_dt in self._ev_windows:
            if start_dt <= now < end_dt:
                return True
        return False

    async def _apply_ev_charging_state(self):
        """Start or stop EVC charging when the window state transitions.

        Called every run() cycle (once per minute).  On a start transition, sets the
        charging profile to the configured max current then issues RemoteStartTransaction
        so the charger begins a session at that rate.  On a stop transition, issues
        RemoteStopTransaction; the firmware falls back to its internal transaction ID when
        none is supplied.  No command is sent when the desired state already matches
        ``self._ev_charging_active``.
        """
        if not self._configured_ev_chargers:
            return
        should_charge = self._should_ev_charge_now()
        if should_charge == self._ev_charging_active:
            return
        # min() gives a deterministic choice when multiple chargers are present
        cp_id = min(self._configured_ev_chargers)
        if should_charge:
            current_a = self._ev_max_current.get(cp_id, 32)
            self.log(f"Info: GatewayMQTT: EVC start charging — current_a={current_a} for '{cp_id}'")
            await self._send_ev_command({"action": "SetChargingProfile", "current_a": int(current_a)})
            await self._send_ev_command({"action": "RemoteStartTransaction", "id_tag": "predbat"})
        else:
            self.log(f"Info: GatewayMQTT: EVC stop charging for '{cp_id}'")
            await self._send_ev_command({"action": "RemoteStopTransaction"})
        self._ev_charging_active = should_charge

    async def _send_ev_command(self, command):
        """Publish a single EV command dict as JSON to the EV command topic.

        Args:
            command: Dict with ``action`` key and any command-specific fields.
        """
        await self._publish_raw(self.topic_ev_command, json.dumps(command).encode())

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
            # Wait up to a minute for first connection attempt before declaring started
            for _ in range(60 * 2):
                if self._first_connection_attempted or self.api_stop:
                    break
                await asyncio.sleep(0.5)
            else:
                self.log("Warn: GatewayMQTT: First connection attempt not yet complete, continuing startup")
            # After a successful connection, wait briefly for the first telemetry → auto-config so
            # that inverter args are wired up before other components start.  Cap at 60 s so an
            # offline gateway device does not stall startup indefinitely.
            if self._mqtt_connected and not self.api_stop:
                for _ in range(60 * 2):
                    if self._auto_configured or self.api_stop:
                        break
                    await asyncio.sleep(0.5)
                else:
                    self.log("Warn: GatewayMQTT: Auto-config not complete after 60s — gateway device may be offline, continuing startup")
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

            # EVC minute-level control: refresh windows from HA plan, then start/stop if needed
            if self.gateway_evc_control and self._auto_configured:
                self._refresh_ev_windows()
                await self._apply_ev_charging_state()

            # Tell the gateway the current read-only state (once on startup, then on every change)
            await self._check_read_only_state()

            # Send inverter_reset for any inverter not yet reset when not in read-only mode
            await self._check_inverter_resets()

            # Publish predbat data (price, timeline) to device display
            if self._mqtt_connected and self._auto_configured:
                await self._publish_predbat_data()

            # Token refresh check
            await self._check_token_refresh()

            # Re-publish the plan periodically so its embedded timestamp stays fresh
            await self._republish_plan_if_stale()

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
                    self._first_connection_attempted = True
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
                self._first_connection_attempted = True

                if self.api_stop:
                    break

                # If the broker rejected our credentials (e.g. the MQTT JWT expired),
                # refresh the token before reconnecting — otherwise we would retry the
                # same rejected token forever and lose all gateway control.
                await self._maybe_refresh_on_auth_error(e)

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

    def _debug_dump(self, label, message=None, raw=None, message_type=None):
        """Log a protobuf message as readable text when debug logging is enabled.

        Pass an already-decoded `message`, or `raw` bytes plus a `message_type` to
        decode them. The raw length is logged as a size reference when provided.

        Args:
            label: Short description of what is being dumped (e.g. "RX telemetry").
            message: A decoded protobuf message to render as text (optional).
            raw: Optional raw bytes, whose length is logged for size reference.
            message_type: Protobuf class used to decode `raw` when `message` is None.
        """
        if not self._debug:
            return
        try:
            if message is None and raw is not None and message_type is not None:
                message = message_type()
                message.ParseFromString(raw)
            size = f" ({len(raw)} bytes)" if raw is not None else ""
            text = _pb_json_format.MessageToJson(message, always_print_fields_with_no_presence=True, preserving_proto_field_name=True)
            self.log(f"Debug: GatewayMQTT: {label}{size}:\n{text}")
        except Exception as e:
            self.log(f"Warn: GatewayMQTT: failed to dump {label}: {e}")

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

        self._debug_dump("RX telemetry", status, raw=data)

        if len(status.inverters) == 0:
            return

        self._last_status = status
        self._last_telemetry_time = time.time()
        self.update_success_timestamp()

        if not self.api_started:
            self.api_started = True
            self.log("Info: GatewayMQTT: First telemetry received, API started")

        self._inject_entities(status)

        if self._needs_reconfigure(status):
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

        # EV charger entities (device-level, present only when a charge point is connected)
        self._inject_ev_entities(status)

        # EMS aggregate entities (when a GIVENERGY_EMS unit is present). The EMS is not
        # guaranteed to be status.inverters[0] — discovery order is unstable (see the
        # 2026-06-04 incident note in automatic_config) — so locate it by type, mirroring
        # the config path which binds inverters = ems_units[:1].
        inv0 = next((inv for inv in status.inverters if inv.type == pb.INVERTER_TYPE_GIVENERGY_EMS), None)
        if inv0 is not None and inv0.ems.num_inverters > 0:
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
                self.dashboard_item(f"{sp}_battery_power", -sub.battery_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_power", {}), app="gateway")
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
        if bat.soh_percent:
            self.dashboard_item(f"sensor.{pfx}_battery_soh", bat.soh_percent, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_soh", {}), app="gateway")
        battery_rate_max = bat.rate_max_w or 6000
        self.dashboard_item(f"sensor.{pfx}_battery_rate_max", battery_rate_max, attributes=GATEWAY_ATTRIBUTE_TABLE.get("battery_rate_max", {}), app="gateway")

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
        inverter_rate_max = inv.inverter.rate_max_w or battery_rate_max
        if inverter_rate_max:
            self.dashboard_item(f"sensor.{pfx}_inverter_rate_max", inverter_rate_max, attributes=GATEWAY_ATTRIBUTE_TABLE.get("inverter_rate_max", {}), app="gateway")

        control = inv.control
        self.dashboard_item(f"switch.{pfx}_charge_enabled", "on" if control.charge_enabled else "off", attributes=GATEWAY_ATTRIBUTE_TABLE.get("charge_enabled", {}), app="gateway")
        self.dashboard_item(f"switch.{pfx}_discharge_enabled", "on" if control.discharge_enabled else "off", attributes=GATEWAY_ATTRIBUTE_TABLE.get("discharge_enabled", {}), app="gateway")
        # Proto sentinel: 0=undefined→publish 99999 (unlimited), 1=zero limit→publish 0, otherwise use value as-is
        _raw_export_limit = control.export_limit_w
        export_limit_publish = 99999 if _raw_export_limit == 0 else (0 if _raw_export_limit == 1 else _raw_export_limit)
        self.dashboard_item(f"sensor.{pfx}_export_limit_w", export_limit_publish, attributes=GATEWAY_ATTRIBUTE_TABLE.get("export_limit_w", {}), app="gateway")
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

    @staticmethod
    def _ev_suffix(ev, multi):
        """Build the entity suffix for an EV charger.

        Single charger (the v1 case) uses a stable "ev" suffix; with more than one
        charger present, disambiguate by the last 6 chars of the OCPP charge point id.

        Args:
            ev: An EvCharger protobuf message.
            multi: True when more than one charger is present in the status.

        Returns:
            str: The entity suffix (e.g. "ev" or "ev_b749").
        """
        if not multi:
            return "ev"
        charge_point_id = ev.charge_point_id or ""
        return f"ev_{charge_point_id[-6:].lower()}" if charge_point_id else "ev"

    @staticmethod
    def _ev_charge_rate_kw(ev):
        """Return the max charge rate in kW from EV charger telemetry.

        Uses max_current_a * voltage_v, falling back to 230 V when voltage is not
        reported, and to 7.4 kW when current is also absent.
        """
        if ev.max_current_a > 0 and ev.voltage_v > 0:
            return round(ev.max_current_a * ev.voltage_v / 1000.0, 2)
        if ev.max_current_a > 0:
            return round(ev.max_current_a * 230 / 1000.0, 2)
        return 7.4

    def _inject_ev_entities(self, status):
        """Inject EV charger entities from GatewayStatus.ev_chargers.

        EV chargers are device-level (not per-inverter). Entities are named
        {type}.{prefix}_gateway_{suffix}_{attribute}, where suffix is "ev" for the
        single-charger case. Fields documented as "0 = not reported" / "" are skipped
        so they surface as unavailable rather than a misleading zero.

        Args:
            status: A decoded GatewayStatus protobuf message.
        """
        chargers = list(status.ev_chargers)
        multi = len(chargers) > 1
        for ev in chargers:
            suffix = self._ev_suffix(ev, multi)
            pfx = f"{self.prefix}_gateway_{suffix}"

            self.dashboard_item(f"binary_sensor.{pfx}_online", ev.connected, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_online", {}), app="gateway")
            ev_car_connected = ev.connected and ev.status in {"Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"}
            self.dashboard_item(f"binary_sensor.{pfx}_connected", ev_car_connected, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_connected", {}), app="gateway")
            self.dashboard_item(f"binary_sensor.{pfx}_session_active", ev.session_active, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_session_active", {}), app="gateway")
            if ev.status:
                self.dashboard_item(f"sensor.{pfx}_status", ev.status, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_status", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_power", ev.power_w, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_power", {}), app="gateway")
            self.dashboard_item(f"sensor.{pfx}_session_energy", round(ev.session_energy_wh / 1000.0, 2), attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_session_energy", {}), app="gateway")
            if ev.current_limit_a:
                self.dashboard_item(f"sensor.{pfx}_current_limit", ev.current_limit_a, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_current_limit", {}), app="gateway")
            if ev.soc_percent:
                ev_soc = ev.soc_percent
            else:
                # SoC not reported by this charger — estimate from session energy and configured battery size
                # so the sensor always exists and the optimizer sees progress rather than a stuck 0%.
                battery_size_kwh = self.get_arg("car_charging_battery_size", 100)
                try:
                    battery_size_kwh = float(battery_size_kwh)
                except (ValueError, TypeError):
                    battery_size_kwh = 100.0
                ev_soc = round(min((ev.session_energy_wh / 1000.0) / battery_size_kwh * 100.0, 100.0), 1)
            self.dashboard_item(f"sensor.{pfx}_soc", ev_soc, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_soc", {}), app="gateway")
            if ev.max_current_a:
                self._ev_max_current[ev.charge_point_id or ""] = ev.max_current_a
                self.dashboard_item(f"sensor.{pfx}_max_current", ev.max_current_a, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_max_current", {}), app="gateway")
            if ev.voltage_v:
                self.dashboard_item(f"sensor.{pfx}_voltage", ev.voltage_v, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_voltage", {}), app="gateway")
            if ev.eco_mode:
                self.dashboard_item(f"sensor.{pfx}_eco_mode", ev.eco_mode, attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_eco_mode", {}), app="gateway")

            self.dashboard_item(f"sensor.{pfx}_charge_rate", self._ev_charge_rate_kw(ev), attributes=GATEWAY_ATTRIBUTE_TABLE.get("ev_charge_rate", {}), app="gateway")

    def _needs_reconfigure(self, status):
        """Whether automatic_config should (re-)run for this status.

        Runs on first telemetry, and again when a *new* inverter serial appears — e.g.
        a second AIO is discovered later, which (per GivTCP) moves the control point
        from the AIO to the Gateway. Removals are deliberately ignored so a transient
        drop-out does not thrash the config; a permanent removal is handled by the
        onboarding/restart path. (NOTE: re-running re-selects the control target and
        rewrites the inverter args; whether PredBat core re-reads ``num_inverters`` at
        runtime vs. needing a component restart is tracked separately.)
        """
        if not self._auto_configured:
            return True
        new_serials = frozenset(inv.serial for inv in status.inverters) - self._configured_inverter_serials
        if new_serials:
            self.log(f"Info: GatewayMQTT: new inverter(s) discovered {sorted(new_serials)} — re-running auto-config")
            return True
        # Re-run when an EV charger appears that we have not configured yet, so a charge
        # point connecting after first telemetry triggers car registration.
        new_chargers = frozenset(ev.charge_point_id for ev in status.ev_chargers if ev.charge_point_id) - self._configured_ev_chargers
        if new_chargers:
            self.log(f"Info: GatewayMQTT: new EV charger(s) discovered {sorted(new_chargers)} — re-running auto-config")
            return True
        return False

    def automatic_config(self):
        """Register gateway entities with PredBat's inverter model.

        Called on first telemetry and re-run when a new inverter serial appears
        (see ``_needs_reconfigure``). Maps proto fields to PredBat args so the plan
        engine has data to work with.
        """
        if not self._last_status:
            self.log("Error: GatewayMQTT: automatic_config called with no status data")
            return

        status = self._last_status
        all_inverters = list(status.inverters)

        if not all_inverters:
            self.log("Error: GatewayMQTT: no inverters in gateway status")
            return

        # Classify discovered units and route control per GivTCP / GE-Cloud. Neither a
        # GivEnergy Gateway (INVERTER_TYPE_GIVENERGY_GATEWAY) nor a Plant EMS
        # (INVERTER_TYPE_GIVENERGY_EMS) is itself a battery inverter — they are
        # coordinators with no usable charge window of their own. The battery inverters
        # (AIO / AC / AC3 / hybrid / HV — all reported as INVERTER_TYPE_GIVENERGY) are
        # the controllable units. Control-target priority mirrors GivTCP:
        #   * Plant EMS present     -> control the EMS (it manages all inverters)
        #   * Gateway + >=2 AIOs    -> control the Gateway (it coordinates the parallel AIOs)
        #   * Gateway + 1 AIO / none-> control the AIO(s) directly
        # Selection is by type/count and slots are bound by serial below, so a
        # re-discovery that reorders the units never changes which unit is controlled.
        # (The 2026-06-04 incident bound the Gateway as "inverter 0" — which has no
        # charge window — and broke control after an NVS wipe re-ordered discovery.)
        ems_units = [inv for inv in all_inverters if inv.type == pb.INVERTER_TYPE_GIVENERGY_EMS]
        gateway_units = [inv for inv in all_inverters if inv.type == pb.INVERTER_TYPE_GIVENERGY_GATEWAY]
        coordinator_types = (pb.INVERTER_TYPE_GIVENERGY_EMS, pb.INVERTER_TYPE_GIVENERGY_GATEWAY)
        candidate_aios = [inv for inv in all_inverters if inv.type not in coordinator_types]

        # Filter AIOs to primary units with battery data for planning.
        any_primary = any(inv.primary for inv in candidate_aios)
        if any_primary:
            aios = [inv for inv in candidate_aios if inv.primary and inv.battery.ByteSize() > 0]
            if not aios:
                # All primary AIOs lack battery — fall back to any with battery
                aios = [inv for inv in candidate_aios if inv.battery.capacity_wh > 0]
        else:
            # Old firmware: no primary flags, use all with battery data
            aios = [inv for inv in candidate_aios if inv.battery.ByteSize() > 0]

        if ems_units:
            # A Plant EMS is the single control point for the whole system.
            inverters = ems_units[:1]
        elif gateway_units and len(aios) > 1:
            # Multiple AIOs behind a Gateway: the Gateway is the single control point.
            # NOTE: control commands are addressed to the Gateway/EMS serial — the firmware
            # must fan these out to the AIOs (tracked separately in command_handler.cpp).
            inverters = gateway_units[:1]
        else:
            inverters = aios
        if not inverters:
            inverters = candidate_aios or list(all_inverters)  # last resort

        # Apply serial filter if configured. A no-match is an error — configuring the
        # wrong inverter set is worse than not configuring at all. Leave _auto_configured
        # False so the run loop stays blocked and we retry on the next telemetry.
        if self.gateway_inverter_serial:
            serial_set = set(s.upper() for s in self.gateway_inverter_serial)
            filtered = [inv for inv in inverters if inv.serial.upper() in serial_set]
            if filtered:
                inverters = filtered
            else:
                available = [inv.serial for inv in inverters]
                self.log(f"Error: GatewayMQTT: gateway_inverter_serial filter {self.gateway_inverter_serial} matched no inverters" f" (available serials: {available}); auto-config aborted — will retry on next telemetry")
                self._auto_configured = False
                return

        # Bind PredBat inverter slots to a stable key (serial), not discovery order, so
        # a given physical inverter always maps to the same slot/entities across a
        # re-discovery (NVS wipe, gateway reboot, repair). PredBat core is positional;
        # sorting by serial makes the slot index a deterministic function of identity.
        inverters = sorted(inverters, key=lambda inv: inv.serial)

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
        export_limit_entities = []
        inverter_limit_entities = []

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
            export_limit_entities.append(f"sensor.{base}_export_limit_w")

            # soc_max: from battery capacity entity
            if inv.battery.capacity_wh > 0:
                soc_max_entities.append(f"sensor.{base}_battery_capacity")
            else:
                soc_max_entities.append(None)
                self.log(f"Warn: GatewayMQTT: inverter {inv.serial} has no battery capacity, setting to None for automatic discovery")

            inverter_limit_entities.append(f"sensor.{base}_inverter_rate_max")

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
        self.set_arg("export_limit", export_limit_entities)
        self.set_arg("inverter_limit", inverter_limit_entities)

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
        self.set_arg("battery_rate_max", [f"sensor.{base0}_battery_rate_max"])

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

        # AC-coupled vs hybrid: mirror gecloud — if any battery inverter reports an
        # AC / AIO / All-in-One model, the system is AC-coupled, so turn the PredBat
        # hybrid switch off; a Hybrid/HV unit turns it on. Needs the gateway firmware
        # to report inv.model; left untouched on older firmware that omits it (empty).
        ac_coupled = False
        model_name = ""
        for inv in candidate_aios:
            model = (inv.model or "").lower()
            if model:
                model_name = inv.model
                if ("ac" in model) or ("aio" in model) or ("all-in-one" in model):
                    ac_coupled = True
                    break
        if model_name:
            hybrid_entity = f"switch.{self.prefix}_inverter_hybrid"
            self.set_state_wrapper(hybrid_entity, "off" if ac_coupled else "on")
            self.log(f"Info: GatewayMQTT: model '{model_name}' -> ac_coupled={ac_coupled}, set {hybrid_entity} {'off' if ac_coupled else 'on'}")

        # Register the gateway's OCPP EV charger as a PredBat car (opt-in). Done after the
        # inverter config so a failure here never blocks battery control.
        self._register_ev_car(status)

        self._auto_configured = True
        self._configured_inverter_serials = frozenset(inv.serial for inv in all_inverters)
        self._configured_ev_chargers = frozenset(ev.charge_point_id for ev in status.ev_chargers if ev.charge_point_id)
        self.log(f"Info: GatewayMQTT: auto-config complete: {num_inverters} inverter(s) registered")
        return num_inverters

    def _register_ev_car(self, status):
        """Register a connected OCPP EV charger as a PredBat car.

        Maps the gateway's EV telemetry entities onto the ``car_charging_*`` args so the
        optimizer plans for the charger.  Gated behind ``gateway_evc_automatic``; does
        nothing when no EV charger is present in telemetry.  Battery size, target SoC and
        ready-time have no source from the charger and are left to the existing
        ``car_charging_battery_size`` / ``car_charging_limit`` settings.

        Args:
            status: A decoded GatewayStatus protobuf message.
        """
        if not self.gateway_evc_automatic:
            return

        chargers = list(status.ev_chargers)
        if not chargers:
            return

        # For now we overwrite the first charger only; multi-charger support needs work
        ev = chargers[0]
        pfx = f"{self.prefix}_gateway_{self._ev_suffix(ev, multi=False)}"
        self.set_arg("num_cars", 1)
        # Entity-reference args (resolved by get_arg indirect lookup at fetch time).
        # Battery size and target limit are deliberately left to the existing
        # car_charging_battery_size / car_charging_limit settings — the charger cannot
        # report them, so overwriting them here would only swap one default for another.
        # "Planned"/"now" both derive from the connected binary sensor; many OCPP cars do
        # not report SoC, so the manual-SoC path supplies a starting value.
        self.set_arg("car_charging_planned", [f"binary_sensor.{pfx}_connected"])
        if not self.gateway_evc_control:
            self.set_arg("car_charging_now", [f"binary_sensor.{pfx}_session_active"])
        self.set_arg("car_charging_soc", [f"sensor.{pfx}_soc"])
        self.set_arg("car_charging_energy", f"sensor.{pfx}_session_energy")
        # car_charging_rate is a UI config item (input_number), so update it via
        # expose_config. get_arg() consults HA/UI config before args, so set_arg() would
        # be ignored (and indirect entity resolution would never run).
        self.base.expose_config("car_charging_rate", self._ev_charge_rate_kw(ev))

        self.log(f"Info: GatewayMQTT: registered EV charger '{ev.charge_point_id or 'unknown'}' as car 1")

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

            # Marginal cost matrix — "what does an extra 1/2/4/8 kWh of load cost
            # me right now and in each of the next 6 two-hour windows?". Computed
            # by the Marginal mixin via what-if prediction runs. Used by the
            # gateway's appliance RAG to pick a colour that actually reflects the
            # cost of running the dryer/EV/etc, rather than inferring from slot
            # categories alone.
            rate_anchors = extract_rate_anchors(
                self.get_state_wrapper("sensor." + self.prefix + "_marginal_energy_costs", attribute="rate_min"),
                self.get_state_wrapper("sensor." + self.prefix + "_marginal_energy_costs", attribute="rate_max"),
                self.get_state_wrapper("sensor." + self.prefix + "_marginal_energy_costs", attribute="import_rate_base"),
                self.get_state_wrapper("sensor." + self.prefix + "_marginal_energy_costs", attribute="export_rate_base"),
            )
            marginal_costs = []
            marginal_time_labels = []
            try:
                matrix = self.get_state_wrapper("sensor." + self.prefix + "_marginal_energy_costs", attribute="matrix")
                if isinstance(matrix, dict) and matrix:
                    # Canonical level order matches MARGINAL_EXTRA_KWH_LEVELS in marginal.py.
                    # Keys are integers when the HA state cache holds the dict directly;
                    # defensive fallback to the string form covers any JSON-round-tripped path.
                    levels = [1, 2, 4, 8]
                    # Determine the column shape first from the first non-empty row, then
                    # build all rows against that fixed set of labels so the matrix stays
                    # rectangular even when lower levels are missing.
                    time_labels = []
                    for lvl in levels:
                        row = matrix.get(lvl) or matrix.get(str(lvl)) or {}
                        if isinstance(row, dict) and row:
                            time_labels = list(row.keys())
                            break

                    # Only publish once we have something meaningful.
                    if time_labels:
                        tmp_costs = []
                        for lvl in levels:
                            row = matrix.get(lvl) or matrix.get(str(lvl)) or {}
                            if not isinstance(row, dict):
                                row = {}
                            # Missing rows or columns are padded with 0 rather than dropped.
                            tmp_costs.append([round(float(row.get(tl, 0) or 0), 2) for tl in time_labels])

                        marginal_time_labels = time_labels
                        marginal_costs = tmp_costs
            except (TypeError, ValueError, AttributeError, KeyError) as exc:
                self.log(f"Warn: GatewayMQTT: failed to read marginal costs: {exc}")
                marginal_costs = []
                marginal_time_labels = []

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
                "marginal_costs": marginal_costs,
                "marginal_time_labels": marginal_time_labels,
                "rate_min": (rate_anchors or {}).get("rate_min"),
                "rate_max": (rate_anchors or {}).get("rate_max"),
                "import_rate": (rate_anchors or {}).get("import_rate"),
                "export_rate": (rate_anchors or {}).get("export_rate"),
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

    async def _check_read_only_state(self):
        """Publish a set_read_only command whenever PredBat's read-only mode changes.

        Called on every run() cycle. Sends the current state once on startup (when
        _last_read_only is still None) and again on each subsequent transition, so the
        gateway firmware always knows whether PredBat is permitted to control the
        inverter. To guard against the gateway losing sync (the command is not retained,
        so a single missed message would leave it stale), the state is also re-sent at
        least every 30 minutes even when unchanged. The command is gateway-wide, so it
        carries no inverter serial. Gated only on an active MQTT connection — it does not
        depend on auto-config, so the state is established as early as possible.
        """
        if not self._mqtt_connected:
            # Force a re-send after reconnect (command is not retained).
            self._last_read_only = None
            return
        read_only = bool(self.get_arg("set_read_only", False))
        now = time.time()
        changed = read_only != self._last_read_only
        stale = now - self._last_read_only_sent_time >= 30 * 60
        if not changed and not stale:
            return
        await self.publish_command("set_read_only", enable=read_only)
        self._last_read_only = read_only
        self._last_read_only_sent_time = now
        self.log(f"Info: GatewayMQTT: set_read_only command sent (read_only={read_only})")

    async def _check_inverter_resets(self):
        """Send inverter_reset for each configured inverter not yet reset in control mode.

        Called on every run() cycle. Skips when is_alive() is False, auto-config has
        not yet run, or set_read_only is active. Each serial is reset at most once per
        process lifetime (tracked in _inverter_reset_done).
        """
        if self.get_arg("set_read_only", False):
            self._inverter_reset_done.clear()  # allow re-send if read-only is later disabled
            return
        if not self.is_alive() or not self._auto_configured:
            return
        for suffix, serial in self._suffix_to_serial.items():
            if serial not in self._inverter_reset_done:
                await self.publish_command("inverter_reset", serial=serial)
                self._inverter_reset_done.add(serial)
                self.log(f"Info: GatewayMQTT: inverter_reset sent for inverter {serial}")

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

        if not self._mqtt_connected:
            # Re-queue so the next run() cycle retries once reconnected. Crucially, none
            # of the publish state (version, timestamp, cached plan) is mutated here, so
            # the periodic re-publish gate stays armed and fires immediately on reconnect.
            self._pending_plan = (plan_entries, timezone_str)
            self.log("Warn: GatewayMQTT: Not connected — plan queued for next publish")
            return

        self._plan_version += 1
        data = self.build_execution_plan(plan_entries, plan_version=self._plan_version, timezone=timezone_str)
        self._last_plan_data = data
        self._last_plan_entries = plan_entries
        self._last_plan_timezone = timezone_str
        self._last_plan_publish_time = time.time()

        self._debug_dump(f"TX execution plan v{self._plan_version}", raw=data, message_type=pb.ExecutionPlan)
        await self._publish_raw(self.topic_schedule, data, retain=True)
        self._last_published_plan = plan_entries
        self.log(f"Info: GatewayMQTT: Published execution plan v{self._plan_version} ({len(plan_entries)} entries)")

    async def _republish_plan_if_stale(self):
        """Re-publish the last plan periodically so its embedded timestamp stays fresh.

        Called on every run() cycle. When more than _PLAN_REPUBLISH_INTERVAL has elapsed
        since the last publish, the plan protobuf is rebuilt (which stamps a fresh
        timestamp) and re-published. Rebuilding is essential: re-sending the cached bytes
        would carry the original timestamp, so the device would still treat the plan as
        stale. The version is left unchanged because the plan content has not changed.
        Gated on an active MQTT connection and a previously built plan.
        """
        if self._last_plan_entries is None or self._last_plan_timezone is None or not self._mqtt_connected:
            return
        if time.time() - self._last_plan_publish_time <= _PLAN_REPUBLISH_INTERVAL:
            return
        data = self.build_execution_plan(self._last_plan_entries, plan_version=self._plan_version, timezone=self._last_plan_timezone)
        self._debug_dump("TX execution plan (re-publish)", raw=data, message_type=pb.ExecutionPlan)
        await self._publish_raw(self.topic_schedule, data, retain=True)
        self._last_plan_data = data
        self._last_plan_publish_time = time.time()
        self.log("Info: GatewayMQTT: Re-published execution plan (refreshed timestamp)")

    async def publish_command(self, command, **kwargs):
        """Build and publish a JSON command to the gateway.

        Args:
            command: Command name (set_mode, set_charge_rate, etc.)
            **kwargs: Command-specific fields (mode, power_w, target_soc).
        """
        self._command_id += 1
        cmd_json = self.build_command(command, command_id=self._command_id, **kwargs)

        self.log("Info: GatewayMQTT: publish_command: command={}, payload={}".format(command, cmd_json))

        if self._mqtt_connected:
            await self._publish_raw(self.topic_command, cmd_json.encode("utf-8"))
            self.log(f"Info: GatewayMQTT: Published command: {command} payload={cmd_json}")
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
        e.g. select.predbat_gateway_30g499_charge_slot1_start
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
        if serial is None:
            self.log(f"Warn: GatewayMQTT: select_event: cannot resolve serial for entity '{entity_id}' — command not sent")
            return
        # Operating mode selector
        if "_mode_select" in entity_id:
            mode_int = GATEWAY_OPERATING_MODE_VALUES.get(str(value).strip(), 0)
            await self.publish_command("set_mode", mode=mode_int, serial=serial)
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

    async def _update_charge_slot(self, entity_id, hhmm, serial):
        """Send set_charge_slot command with updated start or end time."""
        # Determine which field changed
        if "_start" in entity_id:
            schedule = {"start": hhmm}
        else:
            schedule = {"end": hhmm}
        await self.publish_command("set_charge_slot", schedule_json=json.dumps(schedule), serial=serial)
        self.log(f"Info: GatewayMQTT: Charge slot update: {schedule}")

    async def _update_discharge_slot(self, entity_id, hhmm, serial):
        """Send set_discharge_slot command with updated start or end time."""
        if "_start" in entity_id:
            schedule = {"start": hhmm}
        else:
            schedule = {"end": hhmm}
        await self.publish_command("set_discharge_slot", schedule_json=json.dumps(schedule), serial=serial)
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
        if serial is None:
            self.log(f"Warn: GatewayMQTT: number_event: cannot resolve serial for entity '{entity_id}' — command not sent")
            return
        if "_discharge_rate" in entity_id:
            await self.publish_command("set_discharge_rate", power_w=val, serial=serial)
        elif "_charge_rate" in entity_id:
            await self.publish_command("set_charge_rate", power_w=val, serial=serial)
        elif "_reserve" in entity_id:
            await self.publish_command("set_reserve", target_soc=val, serial=serial)
        elif "_target_soc" in entity_id:
            await self.publish_command("set_target_soc", target_soc=val, serial=serial)

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
        if serial is None:
            self.log(f"Warn: GatewayMQTT: switch_event: cannot resolve serial for entity '{entity_id}' — command not sent")
            return
        if "_charge_enabled" in entity_id:
            await self.publish_command("set_charge_enable", enable=is_on, serial=serial)
            self.log(f"Info: GatewayMQTT: Charge {'enabled' if is_on else 'disabled'}")
        elif "_discharge_enabled" in entity_id:
            await self.publish_command("set_discharge_enable", enable=is_on, serial=serial)
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
        """Proactively refresh the MQTT JWT when it nears expiry (housekeeping path).

        Runs every cycle from run(); only triggers a refresh once the token is
        within token_needs_refresh() of expiry. The reconnect loop additionally
        forces a refresh on broker auth-failure via _maybe_refresh_on_auth_error().
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

        if self.mqtt_token_expires_at == -1:
            return

        if self.mqtt_token_expires_at and self.mqtt_token_expires_at > 0 and not self.token_needs_refresh(self.mqtt_token_expires_at):
            return

        await self._do_token_refresh()

    @staticmethod
    def _is_auth_failure(error):
        """True if a broker error means our MQTT credentials were rejected.

        Matches MQTT CONNACK auth reason codes 134 (bad user name or password) and
        135 (not authorized) plus their text. An expired MQTT JWT shows up as code
        134, so the reconnect loop uses this to decide it must refresh the token
        rather than retry the same rejected one forever.
        """
        text = str(error).lower()
        return "bad user name or password" in text or "not authorized" in text or "not authorised" in text or "unauthorized" in text or "code:134" in text or "code:135" in text

    async def _maybe_refresh_on_auth_error(self, error):
        """Force an MQTT token refresh when the broker rejected authentication.

        Returns True if a refresh was attempted and succeeded. Non-auth errors
        (network drops, "Disconnected during message iteration") are ignored so we
        don't hammer the refresh endpoint for problems a new token cannot fix.
        """
        if not self._is_auth_failure(error):
            return False
        self.log("Info: GatewayMQTT: Broker rejected auth — refreshing MQTT token before reconnect")
        return await self._do_token_refresh()

    def _apply_refresh_response(self, data):
        """Apply an oauth-refresh JSON reply to the in-memory token. Returns success."""
        if not data.get("success"):
            self.log(f"Warn: GatewayMQTT: Token refresh failed: {data.get('error', 'unknown')}")
            return False

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
        return True

    async def _do_token_refresh(self):
        """Refresh the MQTT JWT via the oauth-refresh edge function, unconditionally.

        Shared by the proactive near-expiry check and the auth-failure reconnect
        path. The refresh token is held server-side in instance secrets. Returns
        True if a new access token was obtained and applied.
        """
        if not HAS_AIOHTTP:
            return False

        if self._refresh_in_progress:
            return False

        self._refresh_in_progress = True
        try:
            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")
            instance_id = self.args.get("user_id", "") if isinstance(self.args, dict) else ""

            if not supabase_url or not supabase_key or not instance_id:
                self.log("Warn: GatewayMQTT: Token refresh skipped — missing env vars or instance_id")
                return False

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
                        return False

                    data = await response.json()

            return self._apply_refresh_response(data)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: GatewayMQTT: Token refresh network error: {e}")
            return False
        except Exception as e:
            self.log(f"Warn: GatewayMQTT: Token refresh error: {e}")
            return False
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
            "command_id": "PBAT" + str(kwargs.get("command_id", 0)),
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
            cmd["dongle_serial"] = kwargs["serial"]

        return json.dumps(cmd)

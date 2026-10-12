"""Local Deye ToU actuation, called only by the native inverter executor.

Native windows are virtual bookkeeping, not two boundaries borrowed from the
inverter's six-period table. All six physical periods carry the current intent.
This adapter owns only their SOC, discharge power and charging-source controls.
It never writes current limits, BMS settings, work mode, ToU times or peak shaving.
"""

import math
import re
import time
from datetime import datetime, timezone

import pytz

from const import MINUTE_WATT


class DeyeLocal:
    """Translate native charge/demand/freeze into explicitly mapped HA controls."""

    def __init__(self, inverter):
        """Keep accepted-write tracking across native cycles, without a worker thread."""
        self.inverter = inverter
        self.base = inverter.base
        self.pending = {}
        self.settings = {}
        self.status = "uninitialised"

    def prepare(self):
        """Reject conflicting legacy writers before creating native virtual controls."""
        if int(self.base.get_arg("num_inverters", 1)) != 1 or self.inverter.id != 0:
            raise ValueError("DEYE local currently requires one inverter per Predbat instance")
        settings = self.base.args.get("deye_local")
        normalised = self.validate_settings(settings)
        if self.base.args.get("inverter"):
            raise ValueError("DEYE local does not accept capability overrides")
        for key in ("charge_start_service", "charge_stop_service", "charge_freeze_service", "discharge_start_service", "discharge_stop_service", "discharge_freeze_service"):
            if self.base.args.get(key):
                raise ValueError("Remove legacy service templates before selecting DEYE local")
        outputs = (
            "charge_start_time",
            "charge_end_time",
            "discharge_start_time",
            "discharge_end_time",
            "charge_limit",
            "reserve",
            "charge_rate",
            "discharge_rate",
            "timed_charge_current",
            "timed_discharge_current",
            "charge_rate_percent",
            "discharge_rate_percent",
            "charge_limit_enable",
            "pause_mode",
            "charge_start_hour",
            "charge_end_hour",
            "charge_start_minute",
            "charge_end_minute",
            "discharge_start_hour",
            "discharge_end_hour",
            "discharge_start_minute",
            "discharge_end_minute",
        )
        for key in outputs:
            if self.inverter.user_configured_entity(key):
                raise ValueError("DEYE local cannot share generic output mappings: " + key)
        if normalised != self.settings:
            self.pending.clear()
        self.settings = normalised
        self.inverter.create_missing_arg("charge_limit", 100)
        self.base.args["charge_limit"][self.inverter.id] = self.inverter.create_entity("charge_limit", 100, uom="%")
        # These are effective capabilities, not writes to the user's UI settings.
        # ToU source permission is not force export or a variable grid-charge rate.
        self.base.set_export_window = False
        self.base.set_export_freeze = False
        self.base.set_export_freeze_only = False
        self.base.set_charge_low_power = False
        self.base.set_freeze_export_during_demand = False
        if not settings.get("control", False):
            self._report("disabled", "none", "Experimental control has not been enabled")
        elif self.base.set_read_only:
            self._report("read_only", "none")

    @staticmethod
    def validate_settings(settings):
        """Validate explicit mappings without changing inverter or planner state."""
        required = {"programs", "time_of_use", "work_mode", "energy_pattern", "max_discharge_power_w"}
        if not isinstance(settings, dict) or not required.issubset(settings) or set(settings) - required - {"control", "hold_verified", "telemetry_time"}:
            raise ValueError("DEYE local requires explicit programs, operating-mode entities and discharge power cap")
        if any(type(settings.get(key, False)) is not bool for key in ("control", "hold_verified")):
            raise ValueError("DEYE control and hold_verified must be booleans")
        programs = settings["programs"]
        if not isinstance(programs, list) or len(programs) != 6:
            raise ValueError("DEYE local requires all six physical ToU programs")
        entities = []
        for row in programs:
            if not isinstance(row, dict) or set(row) != {"time", "soc", "power", "charging"}:
                raise ValueError("Each DEYE program needs time, soc, power and charging entities")
            for field, domain in (("time", "time"), ("soc", "number"), ("power", "number"), ("charging", "select")):
                DeyeLocal._entity(row[field], domain)
                entities.append(row[field])
        for key in ("time_of_use", "work_mode", "energy_pattern"):
            DeyeLocal._entity(settings[key], "select")
            entities.append(settings[key])
        if settings.get("control", False):
            DeyeLocal._entity(settings.get("telemetry_time"), "(?:sensor|datetime)")
        if len(entities) != len(set(entities)):
            raise ValueError("DEYE local control entities must be distinct")
        cap = DeyeLocal._number(settings["max_discharge_power_w"], 1, 100000, "discharge power cap")
        return {**settings, "max_discharge_power_w": cap, "programs": [dict(row) for row in programs]}

    @staticmethod
    def _entity(value, domain):
        """Require a concrete integration entity, never a literal or regex alias."""
        if not isinstance(value, str) or not re.fullmatch(domain + r"\.[a-z0-9_]+", value):
            raise ValueError("DEYE local requires concrete " + domain + " entities")

    @staticmethod
    def _number(value, minimum, maximum, label):
        """Reject missing, non-finite, fractional and out-of-range control values."""
        if isinstance(value, bool):
            raise ValueError("Invalid DEYE " + label)
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise ValueError("Invalid DEYE " + label) from None
        if not math.isfinite(result) or result != int(result) or not minimum <= result <= maximum:
            raise ValueError("Invalid DEYE " + label)
        return int(result)

    def _state(self, entity):
        """Read HA's existing observation cache, never request a hardware refresh."""
        raw = self.base.get_state_wrapper(entity, raw=True)
        if not isinstance(raw, dict) or raw.get("state") in (None, "unknown", "unavailable", ""):
            raise ValueError("DEYE local control observation is unavailable")
        return raw

    def snapshot(self):
        """Validate the whole table and operating modes before any physical write."""
        controls = self.settings
        if controls.get("control", False):
            stamp = self._state(controls["telemetry_time"])["state"]
            try:
                observed_at = datetime.fromisoformat(stamp)
                if observed_at.tzinfo is None:
                    observed_at = pytz.timezone(self.base.get_arg("timezone", "Europe/London")).localize(observed_at, is_dst=None)
                age = (datetime.now(timezone.utc) - observed_at).total_seconds()
            except (ValueError, TypeError, pytz.exceptions.Error):
                raise ValueError("DEYE telemetry clock is invalid or ambiguous") from None
            if not -30 <= age <= 180:
                raise ValueError("DEYE telemetry clock is stale or ahead; no controls were sent")
        if self._state(controls["time_of_use"])["state"] != "Week":
            raise ValueError("DEYE local requires ToU enabled for the whole week")
        if self._state(controls["work_mode"])["state"] not in ("Zero Export To CT", "Zero Export To Load"):
            raise ValueError("DEYE local requires an existing zero-export work mode")
        if self._state(controls["energy_pattern"])["state"] != "Load First":
            raise ValueError("DEYE local currently models Load First only")
        snapshot = {}
        times = []
        for row in controls["programs"]:
            stamp = self._state(row["time"])["state"]
            if not isinstance(stamp, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::00)?", stamp):
                raise ValueError("Invalid DEYE ToU boundary")
            times.append(int(stamp[:2]) * 60 + int(stamp[3:5]))
            for field in ("soc", "power", "charging"):
                entity = row[field]
                raw = self._state(entity)
                attrs = raw.get("attributes", {})
                if not isinstance(attrs, dict):
                    raise ValueError("Invalid DEYE control attributes")
                if field == "charging":
                    if raw["state"] not in ("Disabled", "Grid", "Generator", "Both") or not {"Disabled", "Grid"}.issubset(attrs.get("options", [])):
                        raise ValueError("Invalid DEYE charging-source control")
                    snapshot[entity] = raw["state"]
                else:
                    maximum = 100 if field == "soc" else 100000
                    snapshot[entity] = self._number(raw["state"], 0, maximum, field)
                    wanted_max = 100 if field == "soc" else controls["max_discharge_power_w"]
                    if attrs.get("unit_of_measurement") != ("%" if field == "soc" else "W"):
                        raise ValueError("Incorrect DEYE control unit")
                    if self._number(attrs.get("min"), 0, maximum, "control minimum") != 0 or self._number(attrs.get("max"), 0, maximum, "control maximum") < wanted_max:
                        raise ValueError("DEYE control range cannot represent the requested modes")
                    if self._number(attrs.get("step", 1), 1, maximum, "control step") != 1:
                        raise ValueError("DEYE local requires whole-unit controls")
        # Six distinct starts partition one day cyclically; the first need not be midnight.
        if times != sorted(set(times)):
            raise ValueError("DEYE ToU boundaries must be strictly increasing; times were not modified")
        return snapshot

    def validate_limits(self):
        """Keep the native model inside the explicit per-period discharge ceiling."""
        self.snapshot()
        cap = self.settings["max_discharge_power_w"] / MINUTE_WATT
        self.inverter.battery_rate_max_discharge = min(self.inverter.battery_rate_max_discharge, cap)
        self.inverter.battery_rate_max_export = 0

    def _phase(self, desired, snapshot):
        """Submit differing controls and require observed read-back before advancing.

        One shared settle interval uses the existing HA cache, not faster Modbus
        polling. Accepted-but-unobserved writes wait for a later native cycle;
        retries are bounded and never escalate to another register or transport.
        """
        wrote = False
        for entity, wanted in desired.items():
            previous = self.pending.get(entity)
            superseded = previous and previous["value"] != wanted
            if snapshot[entity] == wanted and not superseded:
                self.pending.pop(entity, None)
                continue
            now = time.monotonic()
            attempts = 0
            if previous and previous["value"] == wanted:
                attempts = previous["attempts"]
                if attempts >= 3:
                    raise ValueError("DEYE control was not observed after three attempts")
                if now - previous["at"] < 300:
                    continue
            data = {"entity_id": entity}
            if entity.startswith("number."):
                service = "number/set_value"
                data["value"] = wanted
            else:
                service = "select/select_option"
                data["option"] = wanted
            self.pending[entity] = {"value": wanted, "at": now, "attempts": attempts + 1}
            try:
                accepted = self.base.call_service_wrapper(service, **data)
            except Exception:
                raise ValueError("DEYE control service raised an error; no later phase was sent") from None
            if not accepted:
                raise ValueError("DEYE control service was rejected; no later phase was sent")
            self.inverter.count_register_writes += 1
            wrote = True
        if wrote:
            time.sleep(self.inverter.inv_write_and_poll_sleep)
        observed = self.snapshot()
        return all(observed[entity] == value for entity, value in desired.items()), observed

    def _report(self, status, mode, reason=None):
        """Publish control confirmation separately from the predicted charging status."""
        self.status = status
        attrs = {"mode": mode, "observations_match": status == "observed", "hardware_confirmed": False, "source": "home_assistant_observations", "hardware_commissioning_required": True}
        if reason:
            attrs["reason"] = reason
        self.base.dashboard_item(self.base.prefix + ".deye_local_control", state=status, attributes=attrs)

    def apply(self, target_soc, freeze=False):
        """Apply one native intent with grid-source interlock and no global-limit writes."""
        mode = "invalid"
        if self.base.set_read_only or getattr(self.base, "set_read_only_axle", False):
            self._report("read_only", "none")
            return False
        try:
            target = self._number(target_soc, 0, 100, "target SOC")
            mode = "hold" if freeze else "charge" if target > 0 else "demand"
            if not self.settings.get("control", False):
                self._report("disabled", mode, "Experimental control has not been enabled")
                return False
            if freeze and not self.settings.get("hold_verified", False):
                raise ValueError("DEYE hold needs commissioning on this inverter and firmware")
            if self.base.manual_soc_max_keep:
                raise ValueError("DEYE local cannot enforce a manual solar charge ceiling")
            floor = self._number(self.inverter.reserve_percent, 0, 100, "reserve")
            soc = 100 if freeze else max(target, floor)
            cap = min(self.settings["max_discharge_power_w"], int(round(self.inverter.battery_rate_max_discharge * MINUTE_WATT)))
            power = 0 if freeze else cap
            source = "Grid" if mode == "charge" else "Disabled"
            rows = self.settings["programs"]
            numbers = {row[field]: value for row in rows for field, value in (("power", power), ("soc", soc))}
            sources = {row["charging"]: source for row in rows}
            snapshot = self.snapshot()
            # Disarm ALL sources before changing either floor or discharge power.
            # In particular, hold SOC=100 must never land while Grid is enabled.
            if any(snapshot[entity] != value or self.pending.get(entity, {}).get("value", value) != value for entity, value in numbers.items()):
                ready, snapshot = self._phase({row["charging"]: "Disabled" for row in rows}, snapshot)
                if not ready:
                    self._report("pending", mode, "Waiting for all charging sources to be disabled")
                    return False
                ready, snapshot = self._phase(numbers, snapshot)
                if not ready:
                    self._report("pending", mode, "Waiting for all SOC and power observations")
                    return False
            ready, snapshot = self._phase(sources, snapshot)
            self._report("observed" if ready else "pending", mode)
            return ready
        except (ValueError, TypeError, KeyError) as exc:
            self.base.record_status("Error: " + str(exc), had_errors=True)
            self._report("failed", mode, str(exc))
            return False

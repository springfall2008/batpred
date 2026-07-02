# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# pyright: reportAttributeAccessIssue=false


"""Named additional house load forecasts.

Handles static apps.yaml and dynamic Home Assistant API load forecast deltas:
parsing and windowing of named loads, fixed/flexible slot energy distribution,
flexible start-time selection via the full prediction metric, one-shot lifecycle
(suggest/lock/expire), completed-load history persistence, and HA publishing.
"""

import copy
import re
from datetime import datetime, timedelta
from const import PREDICT_STEP
from utils import dp2, dp4, time_string_to_stamp, minutes_to_time
from prediction import Prediction
from ha import run_async


class AdditionalLoad:
    """Mixin providing named additional house load forecast handling for PredBat."""

    def parse_additional_load_weighting(self, weighting, periods):
        """
        Parse additional load forecast weighting into per-slot multipliers.
        """
        if periods <= 0:
            return []
        if weighting is None:
            return [1.0 for _ in range(periods)]
        if isinstance(weighting, (int, float)):
            return [float(weighting) for _ in range(periods)]

        weighting = str(weighting).strip()
        if not weighting:
            return [1.0 for _ in range(periods)]

        weights = []
        weight_separator = ","
        if "|" in weighting:
            weight_separator = "|"
        for weight in weighting.split(weight_separator):
            weight = weight.strip()
            if weight == "*":
                weights.append(1.0)
            else:
                try:
                    weights.append(float(weight))
                except (ValueError, TypeError):
                    self.log("Warn: Bad weighting {} provided in house_load_additional_forecast, using 1.0".format(weight))
                    weights.append(1.0)

        if not weights:
            weights = [1.0]
        while len(weights) < periods:
            weights.append(weights[-1])
        return weights[:periods]

    def get_additional_load_time_minutes(self, load_item, key, default=None):
        """
        Resolve a time field on an additional load forecast item to minutes from midnight.
        """
        value = load_item.get(key, default)
        if value is None:
            return None
        value = self.resolve_arg(key, value, default)
        if value is None:
            return None
        value = str(value)
        if value.count(":") < 2:
            value += ":00"
        try:
            stamp = time_string_to_stamp(value)
        except (ValueError, TypeError):
            self.log("Warn: Bad {} {} provided in house_load_additional_forecast".format(key, value))
            self.record_status("Warn: Bad {} {} provided in house_load_additional_forecast".format(key, value), had_errors=True)
            return None
        return minutes_to_time(stamp, time_string_to_stamp("00:00:00"))

    def get_additional_load_float(self, load_item, key, default=0.0):
        """
        Resolve a numeric field on an additional load forecast item.
        """
        value = load_item.get(key, default)
        if isinstance(value, str):
            try:
                return float(value)
            except (ValueError, TypeError):
                pass
        value = self.resolve_arg(key, value, default)
        try:
            return float(value)
        except (ValueError, TypeError):
            self.log("Warn: Bad {} {} provided in house_load_additional_forecast".format(key, value))
            self.record_status("Warn: Bad {} {} provided in house_load_additional_forecast".format(key, value), had_errors=True)
            return float(default)

    def get_additional_load_bool(self, load_item, key, default=True):
        """
        Resolve a boolean field on an additional load forecast item.
        """
        value = load_item.get(key, default)
        value = self.resolve_arg(key, value, default)
        if isinstance(value, str):
            return value.lower() in ["on", "true", "yes", "enable", "enabled", "1"]
        return bool(value)

    def additional_load_entity_name(self, name):
        """
        Make the binary sensor entity name for a named additional load forecast.
        """
        safe_name = self.additional_load_safe_name(name)
        return "binary_sensor.{}_load_forecast_delta_{}".format(self.prefix, safe_name)

    def additional_load_safe_name(self, name):
        """
        Return the Home Assistant-safe suffix for a named additional load forecast.
        """
        safe_name = re.sub(r"[^a-z0-9_]+", "_", str(name).lower()).strip("_")
        if not safe_name:
            safe_name = "unknown"
        return safe_name

    def additional_load_delete_entity_name(self, name):
        """
        Make the delete button entity name for a named additional load forecast.
        """
        return self.additional_load_entity_name(name).replace("binary_sensor.", "button.", 1) + "_delete"

    def additional_load_name_from_entity(self, entity_id):
        """
        Return additional load forecast name from a binary sensor or button entity id.
        """
        marker = "_load_forecast_delta_"
        if entity_id and marker in entity_id:
            safe_name = entity_id.split(marker, 1)[1].replace("_delete", "")
            for name in list(getattr(self, "house_load_additional_forecasts", {}).keys()) + list(getattr(self, "house_load_additional_forecast_overrides", {}).keys()):
                if self.additional_load_safe_name(name) == safe_name:
                    return str(name)
            return self.resolve_additional_load_name(safe_name)
        return None

    def additional_load_command_name(self, value):
        """
        Return the forecast name from a load_forecast_delta_api command.
        """
        return value.split("?", 1)[0].split("=", 1)[0].replace("[", "").replace("]", "")

    def additional_load_command_args(self, value):
        """
        Return a load_forecast_delta_api command name and query arguments.
        """
        value = value.replace("[", "").replace("]", "")
        if "?" not in value:
            return self.additional_load_command_name(value), {}
        name, command_args = value.split("?", 1)
        args = {}
        for arg in command_args.split("&"):
            arg_split = arg.split("=", 1)
            if len(arg_split) > 1:
                args[arg_split[0]] = arg_split[1]
            else:
                args[arg_split[0]] = True
        return name, args

    def additional_load_build_api_command(self, name, args):
        """
        Build a load_forecast_delta_api command from a name and arguments.
        """
        return "{}?{}".format(name, "&".join("{}={}".format(key, value) if value is not True else key for key, value in args.items()))

    def additional_load_minutes_to_stamp(self, minutes):
        """
        Convert forecast minutes from midnight into a durable timestamp string.
        """
        return str(int((self.midnight_utc + timedelta(minutes=int(minutes))).timestamp()))

    def additional_load_stamp_to_minutes(self, stamp):
        """
        Convert a durable timestamp string into forecast minutes from the current midnight.
        """
        try:
            stamp_datetime = datetime.fromtimestamp(int(stamp), tz=self.midnight_utc.tzinfo) if str(stamp).isdigit() else datetime.fromisoformat(str(stamp))
        except (ValueError, TypeError):
            return None
        return int((stamp_datetime - self.midnight_utc).total_seconds() / 60)

    def additional_load_minutes_to_iso(self, minutes):
        """
        Convert forecast minutes from midnight into an ISO timestamp.
        """
        return (self.midnight_utc + timedelta(minutes=minutes)).isoformat() if minutes is not None else None

    def additional_load_forecast_record(
        self,
        entity_id,
        enabled,
        mode,
        energy_total,
        slot_energy,
        duration,
        weighting,
        load_mode,
        plan_interval,
        requested_start_minutes,
        requested_end_minutes,
        periods,
        weights,
        weight_total,
        source,
        auto_expire,
        expires_minutes,
        target_times=None,
        total_energy=0.0,
        suggested_start_minutes=None,
        suggested_end_minutes=None,
        selection_reason=None,
        candidate_count=0,
        selected_metric=None,
        baseline_metric=None,
        selection_locked=False,
        state=None,
    ):
        """
        Build the published and internal metadata for one additional load forecast.
        """
        if target_times is None:
            target_times = []
        if state is None:
            state = "on" if target_times else "off"
        return {
            "entity_id": entity_id,
            "state": state,
            "target_times": target_times,
            "enabled": enabled,
            "mode": mode,
            "energy": energy_total,
            "slot_energy": slot_energy,
            "duration": duration,
            "weighting": weighting,
            "load_mode": load_mode,
            "plan_interval_minutes": plan_interval,
            "slots": len(target_times),
            "total_energy": dp4(total_energy),
            "requested_start": self.additional_load_minutes_to_iso(requested_start_minutes),
            "requested_end": self.additional_load_minutes_to_iso(requested_end_minutes),
            "suggested_start": self.additional_load_minutes_to_iso(suggested_start_minutes),
            "suggested_end": self.additional_load_minutes_to_iso(suggested_end_minutes),
            "selection_reason": selection_reason,
            "candidate_count": candidate_count,
            "selected_metric": selected_metric,
            "baseline_metric": baseline_metric,
            "selection_locked": selection_locked,
            "source": source,
            "auto_expire": auto_expire,
            "expires_at": self.additional_load_minutes_to_iso(expires_minutes),
            "_requested_start_minutes": requested_start_minutes,
            "_requested_end_minutes": requested_end_minutes,
            "_periods": periods,
            "_weights": weights,
            "_weight_total": weight_total,
        }

    def additional_load_api_metadata(self, name):
        """
        Return persisted hidden metadata for a stored load_forecast_delta_api command.
        """
        item = self.config_index.get("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else None
        if not item:
            return {}
        values = (item.get("value", "") or "").replace("+", "")
        for value in values.split(",") if values else []:
            command_name, args = self.additional_load_command_args(value)
            if command_name == name or self.additional_load_safe_name(command_name) == self.additional_load_safe_name(name):
                return {key: value for key, value in args.items() if str(key).startswith("_")}
        return {}

    def preserve_additional_load_api_metadata(self, value):
        """
        Preserve hidden one-shot metadata when an active API command is sent again.
        """
        name, args = self.additional_load_command_args(value)
        metadata = self.additional_load_api_metadata(name)
        if not metadata:
            return value
        for key, metadata_value in metadata.items():
            args.setdefault(key, metadata_value)
        return self.additional_load_build_api_command(name, args)

    def update_additional_load_api_command_metadata(self, name, metadata):
        """
        Persist one-shot runtime metadata into the stored API selector command.
        """
        item = self.config_index.get("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else None
        if not item:
            return
        values = (item.get("value", "") or "").replace("+", "")
        if not values:
            return
        changed = False
        new_values = []
        for value in values.split(","):
            if value == "off":
                continue
            command_name, args = self.additional_load_command_args(value)
            if command_name == name or self.additional_load_safe_name(command_name) == self.additional_load_safe_name(name):
                for key, metadata_value in metadata.items():
                    if metadata_value is not None and args.get(key) != str(metadata_value):
                        args[key] = str(metadata_value)
                        changed = True
                value = self.additional_load_build_api_command(command_name, args)
            new_values.append(value)
        if changed:
            self.api_select_update("load_forecast_delta_api", new_value="+" + ",".join(new_values) if new_values else "off")

    def resolve_additional_load_name(self, name):
        """
        Resolve a forecast name or safe entity suffix to the active configured name.
        """
        name = str(name)
        safe_name = self.additional_load_safe_name(name)
        candidates = list(getattr(self, "house_load_additional_forecasts", {}).keys()) + list(getattr(self, "house_load_additional_forecast_overrides", {}).keys())
        item = self.config_index.get("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else None
        if item:
            values = (item.get("value", "") or "").replace("+", "")
            candidates += [self.additional_load_command_name(value) for value in values.split(",") if value and value != "off"]
        for candidate in candidates:
            if str(candidate) == name or self.additional_load_safe_name(candidate) == safe_name:
                return str(candidate)
        return name

    def delete_additional_load_forecast(self, name):
        """
        Delete a named one-shot additional load forecast.
        """
        name = self.resolve_additional_load_name(name)
        if not self.has_additional_load_api_command(name) and name not in self.house_load_additional_forecast_overrides:
            self.log("Warn: Ignoring delete for inactive additional load forecast {}".format(name))
            self.unpublish_additional_load_name(name)
            return False
        self.house_load_additional_forecast_overrides.pop(name, None)
        self.remove_additional_load_api_command(name)
        self.refresh_additional_load_forecast_api()
        return True

    def unpublish_additional_load_name(self, name):
        """
        Remove stale additional load forecast entities for a named forecast without replanning.
        """
        for entity_id in [self.additional_load_entity_name(name), self.additional_load_delete_entity_name(name)]:
            self.unpublish_additional_load_entity(entity_id)
            if hasattr(self, "house_load_additional_forecast_entities"):
                self.house_load_additional_forecast_entities.discard(entity_id)

    def has_additional_load_api_command(self, name):
        """
        Return True if a named forecast command is active in the load_forecast_delta_api selector.
        """
        item = self.config_index.get("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else None
        if not item:
            return False
        values = item.get("value", "") or ""
        values = values.replace("+", "")
        values_list = values.split(",") if values else []
        for value in values_list:
            if value == "off":
                continue
            command_name = self.additional_load_command_name(value)
            if command_name == name or self.additional_load_safe_name(command_name) == self.additional_load_safe_name(name):
                return True
        return False

    def remove_additional_load_api_command(self, name):
        """
        Remove a named forecast command from the load_forecast_delta_api selector.
        """
        item = self.config_index.get("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else None
        if not item:
            return
        values = item.get("value", "") or ""
        values = values.replace("+", "")
        values_list = values.split(",") if values else []
        new_values_list = []
        for value in values_list:
            if value == "off":
                continue
            command_name = self.additional_load_command_name(value)
            if command_name != name and self.additional_load_safe_name(command_name) != self.additional_load_safe_name(name):
                new_values_list.append(value)
        new_value = "+" + ",".join(new_values_list) if new_values_list else "off"
        self.api_select_update("load_forecast_delta_api", new_value=new_value)

    def additional_load_slot_energies(self, energy_total, slot_energy, weights, weight_total, period, slot_minutes, plan_interval):
        """
        Return the published slot energy and adjustment rate for one forecast slot.
        """
        if energy_total is not None:
            target_energy = dp4(energy_total * weights[period] / weight_total) if weight_total else 0.0
        else:
            target_energy = dp4(slot_energy * weights[period])
        adjustment_energy = dp4(target_energy * plan_interval / float(slot_minutes)) if slot_minutes else 0.0
        return target_energy, adjustment_energy

    def get_additional_load_window(self, load_item, mode, duration, plan_interval, minutes_now_slot):
        """
        Return start/end minutes for fixed or flexible additional load scheduling.
        """
        start_minutes = self.get_additional_load_time_minutes(load_item, "start_time") if "start_time" in load_item else load_item.get("_requested_start_minutes", None)
        end_minutes = self.get_additional_load_time_minutes(load_item, "end_time") if "end_time" in load_item else None
        duration_minutes = int(duration * 60)

        if mode == "flexible":
            if start_minutes is None and end_minutes is None:
                return minutes_now_slot, minutes_now_slot + self.forecast_minutes
            if start_minutes is None:
                start_minutes = minutes_now_slot
            if end_minutes is None:
                end_minutes = start_minutes + self.forecast_minutes

            windows = []
            for day_offset in [0, 24 * 60]:
                window_start = start_minutes + day_offset
                window_end = end_minutes + day_offset
                if window_end <= window_start:
                    window_end += 24 * 60
                windows.append((window_start, window_end))
                if end_minutes <= start_minutes:
                    windows.append((window_start - 24 * 60, window_end - 24 * 60))

            for window_start, window_end in sorted(windows):
                usable_start = max(window_start, minutes_now_slot)
                if usable_start + duration_minutes > window_end:
                    window_end += 24 * 60
                if usable_start + duration_minutes <= window_end and usable_start < minutes_now_slot + self.forecast_minutes:
                    return usable_start, window_end
            return None, None

        if start_minutes is None:
            return None, end_minutes
        if end_minutes is None:
            end_minutes = start_minutes + int(duration * 60)
        elif end_minutes <= start_minutes:
            end_minutes += 24 * 60

        if end_minutes <= minutes_now_slot:
            start_minutes += 24 * 60
            end_minutes += 24 * 60
        return start_minutes, end_minutes

    def parse_additional_load_api_command(self, api_command):
        """
        Parse one load_forecast_delta_api command into a forecast override.
        """
        if "?" not in api_command:
            self.log("Warn: Bad load_forecast_delta_api command {}, expected name?start_time=...&duration=...".format(api_command))
            return None

        name, command_args = self.additional_load_command_args(api_command)
        if not name:
            self.log("Warn: Bad load_forecast_delta_api command {}, missing name".format(api_command))
            return None

        override = {"name": name, "_source": "api", "_auto_expire": True}
        override.update(command_args)
        requested_start_minutes = self.additional_load_stamp_to_minutes(override.get("_requested_start", None)) if "_requested_start" in override else None
        selected_start_minutes = self.additional_load_stamp_to_minutes(override.get("_selected_start", None)) if "_selected_start" in override else None
        expires_minutes = self.additional_load_stamp_to_minutes(override.get("_expires_at", None)) if "_expires_at" in override else None
        if requested_start_minutes is not None:
            override["_requested_start_minutes"] = requested_start_minutes
        if selected_start_minutes is not None:
            override["_selected_start_minutes"] = selected_start_minutes
        if expires_minutes is not None:
            override["_expires_minutes"] = expires_minutes
        for key in ["_candidate_count"]:
            if key in override:
                try:
                    override[key] = int(override[key])
                except (ValueError, TypeError):
                    override.pop(key, None)
        for key in ["_selected_metric", "_baseline_metric"]:
            if key in override:
                try:
                    override[key] = float(override[key])
                except (ValueError, TypeError):
                    override.pop(key, None)
        if "start_time" not in override:
            existing_override = self.house_load_additional_forecast_overrides.get(str(name), {})
            requested_start_minutes = override.get("_requested_start_minutes", existing_override.get("_requested_start_minutes", None))
            if requested_start_minutes is None:
                plan_interval = self.get_arg("plan_interval_minutes", 30)
                requested_start_minutes = int(self.minutes_now / plan_interval) * plan_interval
            override["_requested_start_minutes"] = requested_start_minutes
            self.house_load_additional_forecast_overrides.setdefault(str(name), {"name": str(name)})["_requested_start_minutes"] = requested_start_minutes
            self.update_additional_load_api_command_metadata(str(name), {"_requested_start": self.additional_load_minutes_to_stamp(requested_start_minutes)})
        return override

    def expire_additional_load_api_commands(self):
        """
        Remove expired one-shot additional load forecast API commands.
        """
        expired_names = []
        minutes_now_slot = int(self.minutes_now / self.get_arg("plan_interval_minutes", 30)) * self.get_arg("plan_interval_minutes", 30)
        item = self.config_index.get("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else None
        values = (item.get("value", "") or "").replace("+", "") if item else ""
        for value in values.split(",") if values else []:
            name, args = self.additional_load_command_args(value)
            expires_minutes = self.additional_load_stamp_to_minutes(args.get("_expires_at", None)) if "_expires_at" in args else None
            if expires_minutes is not None and expires_minutes <= minutes_now_slot:
                override = self.parse_additional_load_api_command(value)
                if override:
                    self.archive_completed_additional_load_item(override, minutes_now_slot=minutes_now_slot)
                expired_names.append(name)
        for name, override in list(self.house_load_additional_forecast_overrides.items()):
            expires_minutes = override.get("_expires_minutes", None)
            if expires_minutes is not None and expires_minutes <= minutes_now_slot:
                self.archive_completed_additional_load_item(override, minutes_now_slot=minutes_now_slot)
                expired_names.append(name)
        for name in set(expired_names):
            self.log("Expired additional load forecast {}".format(name))
            self.house_load_additional_forecast_overrides.pop(name, None)
            self.remove_additional_load_api_command(name)
        if expired_names:
            self.publish_additional_load_history()

    def get_additional_load_api_overrides(self):
        """
        Return load_forecast_delta_api overrides by name.
        """
        api_forecast_overrides = {}
        api_overrides = self.api_select_update("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else []
        for api_command in api_overrides:
            override = self.parse_additional_load_api_command(api_command)
            if override:
                api_forecast_overrides[str(override["name"])] = override
        return api_forecast_overrides

    def refresh_additional_load_forecast_api(self):
        """
        Rebuild additional load forecast data after the HA select API changes.
        """
        self.load_forecast_delta_api = self.api_select_update("load_forecast_delta_api") if "load_forecast_delta_api" in self.config_index else []
        self.house_load_additional_forecast_adjust, self.house_load_additional_forecasts = self.fetch_additional_load_forecast()
        self.publish_additional_load_forecasts()

    def additional_load_history_entity(self):
        """
        Return the summary sensor for completed additional load exclusions.
        """
        return "sensor." + self.prefix + "_load_forecast_delta_history"

    def get_additional_load_storage(self):
        """
        Return the storage component used to persist completed additional load records.
        """
        return self.components.get_component("storage") if getattr(self, "components", None) else None

    def additional_load_history_record_minutes(self, record):
        """
        Return record start/end minutes relative to the current midnight.
        """
        try:
            start = datetime.fromisoformat(record.get("start"))
            end = datetime.fromisoformat(record.get("end"))
        except (TypeError, ValueError):
            return None, None
        return int((start - self.midnight_utc).total_seconds() / 60), int((end - self.midnight_utc).total_seconds() / 60)

    def load_additional_load_history(self):
        """
        Load completed additional load exclusion records from persistent storage.
        """
        if getattr(self, "house_load_additional_history_loaded", False):
            return
        records = []
        storage = self.get_additional_load_storage()
        if storage:
            try:
                stored = run_async(storage.load("additional_load", "history"))
                if isinstance(stored, list):
                    records = stored
            except Exception as e:
                self.log("Warn: Failed to load additional load history from storage: {}".format(e))
        self.house_load_additional_history = []
        for record in records:
            if not isinstance(record, dict):
                continue
            start_minutes, end_minutes = self.additional_load_history_record_minutes(record)
            if start_minutes is None or end_minutes is None or end_minutes <= start_minutes:
                continue
            try:
                energy = float(record.get("energy", 0.0))
            except (TypeError, ValueError):
                continue
            if energy <= 0:
                continue
            record = record.copy()
            record["energy"] = dp4(energy)
            self.house_load_additional_history.append(record)
        self.house_load_additional_history_loaded = True
        self.prune_additional_load_history()

    def prune_additional_load_history(self):
        """
        Prune completed additional load exclusion records outside the load-history lookback.
        """
        keep_after = self.now_utc - timedelta(days=getattr(self, "max_days_previous", max(self.days_previous) + 1))
        pruned = []
        seen = set()
        for record in getattr(self, "house_load_additional_history", []):
            record_id = record.get("id")
            if not record_id or record_id in seen:
                continue
            seen.add(record_id)
            try:
                end = datetime.fromisoformat(record.get("end"))
            except (TypeError, ValueError):
                continue
            if end >= keep_after:
                pruned.append(record)
        self.house_load_additional_history = sorted(pruned, key=lambda record: record.get("start", ""))[-1000:]

    def publish_additional_load_history(self):
        """
        Persist completed additional load records to storage and publish a summary sensor.
        """
        self.prune_additional_load_history()
        storage = self.get_additional_load_storage()
        if storage:
            try:
                run_async(storage.save("additional_load", "history", self.house_load_additional_history, format="json"))
            except Exception as e:
                self.log("Warn: Failed to save additional load history to storage: {}".format(e))
        # Storage is the source of truth; the sensor publishes the count and a small recent preview to avoid HA recorder bloat.
        self.dashboard_item(
            self.additional_load_history_entity(),
            state=len(self.house_load_additional_history),
            attributes={
                "friendly_name": "Predbat load forecast delta history",
                "icon": "mdi:history",
                "records": self.house_load_additional_history[-20:],
            },
        )

    def archive_additional_load_slot(self, name, source, mode, slot_start, slot_end, energy, plan_interval):
        """
        Archive one completed additional load slot for future historical load filtering.
        """
        if energy <= 0 or slot_end <= slot_start:
            return False
        self.load_additional_load_history()
        start = self.midnight_utc + timedelta(minutes=slot_start)
        end = self.midnight_utc + timedelta(minutes=slot_end)
        record_id = "{}:{}:{}".format(name, start.isoformat(), end.isoformat())
        if any(record.get("id") == record_id for record in self.house_load_additional_history):
            return False
        self.house_load_additional_history.append(
            {
                "id": record_id,
                "name": name,
                "source": source,
                "mode": mode,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "start_minutes": slot_start,
                "end_minutes": slot_end,
                "energy": dp4(energy),
                "plan_interval_minutes": plan_interval,
            }
        )
        self.prune_additional_load_history()
        return True

    def archive_completed_additional_load_item(self, load_item, minutes_now_slot=None):
        """
        Archive completed slots for one additional load item before it is removed.
        """
        if minutes_now_slot is None:
            plan_interval_now = self.get_arg("plan_interval_minutes", 30)
            minutes_now_slot = int(self.minutes_now / plan_interval_now) * plan_interval_now
        name = str(load_item.get("name", ""))
        if not name:
            return False
        plan_interval = self.get_arg("plan_interval_minutes", 30)
        mode = str(self.resolve_arg("mode", load_item.get("mode", "fixed"), "fixed")).lower()
        duration = self.get_additional_load_float(load_item, "duration", 0.0)
        slot_energy = self.get_additional_load_float(load_item, "slot_energy", 0.0)
        energy_total = self.get_additional_load_float(load_item, "energy", 0.0) if "energy" in load_item else None
        if (energy_total is None and slot_energy == 0) or energy_total == 0:
            return False
        start_minutes = load_item.get("_selected_start_minutes", None)
        if start_minutes is None:
            start_minutes = self.get_additional_load_time_minutes(load_item, "start_time") if "start_time" in load_item else load_item.get("_requested_start_minutes", None)
        if start_minutes is None:
            return False
        start_minutes = int(start_minutes)
        end_minutes = load_item.get("_expires_minutes", None)
        if end_minutes is None:
            end_minutes = self.get_additional_load_time_minutes(load_item, "end_time") if "end_time" in load_item else None
        if end_minutes is None:
            end_minutes = start_minutes + int(duration * 60)
        end_minutes = int(end_minutes)
        if end_minutes <= start_minutes:
            end_minutes += 24 * 60
        if duration <= 0:
            duration = (end_minutes - start_minutes) / 60.0
        if duration <= 0:
            return False
        periods = int((int(duration * 60) + plan_interval - 1) / plan_interval)
        weights = self.parse_additional_load_weighting(self.resolve_arg("weighting", load_item.get("weighting", None), None), periods)
        weight_total = sum(weights)
        source = load_item.get("_source", "yaml")
        changed = False
        for period in range(periods):
            slot_start = start_minutes + period * plan_interval
            slot_end = min(slot_start + plan_interval, end_minutes)
            if slot_end > minutes_now_slot:
                continue
            slot_minutes = slot_end - slot_start
            energy, _ = self.additional_load_slot_energies(energy_total, slot_energy, weights, weight_total, period, slot_minutes, plan_interval)
            changed |= self.archive_additional_load_slot(name, source, mode, slot_start, slot_end, energy, plan_interval)
        return changed

    def get_additional_load_history_energy(self, minute_previous, historical, step=1):
        """
        Return completed additional load energy to exclude from historical load.
        """
        self.load_additional_load_history()
        if not self.house_load_additional_history:
            return 0

        subtract_energy = 0
        if historical:
            for this_point, days in enumerate(self.days_previous):
                use_days = max(min(days, self.load_minutes_age), 1)
                weight = self.days_previous_weight[this_point]
                total_weight = sum(self.days_previous_weight)
                if total_weight == 0:
                    continue
                sample_start = self.minutes_now + minute_previous - (24 * 60 * use_days)
                sample_end = sample_start + step
                for record in self.house_load_additional_history:
                    start_minutes, end_minutes = self.additional_load_history_record_minutes(record)
                    if start_minutes is None:
                        continue
                    overlap = max(0, min(sample_end, end_minutes) - max(sample_start, start_minutes))
                    if overlap:
                        subtract_energy += float(record.get("energy", 0.0)) * overlap / float(end_minutes - start_minutes) * weight / float(total_weight)
        else:
            sample_end = self.minutes_now - minute_previous
            sample_start = sample_end - step
            for record in self.house_load_additional_history:
                start_minutes, end_minutes = self.additional_load_history_record_minutes(record)
                if start_minutes is None:
                    continue
                overlap = max(0, min(sample_end, end_minutes) - max(sample_start, start_minutes))
                if overlap:
                    subtract_energy += float(record.get("energy", 0.0)) * overlap / float(end_minutes - start_minutes)

        return dp4(subtract_energy)

    def get_additional_load_forecast_config(self):
        """
        Return additional load forecast config with runtime API overrides applied by name.
        """
        config = self.get_arg("house_load_additional_forecast", [], indirect=False)
        if not config:
            config = []
        if isinstance(config, dict):
            config = [config]
        if isinstance(config, str):
            self.log("Warn: house_load_additional_forecast should be a list of dictionaries")
            return []

        forecast_items = []
        for load_item in config:
            if not isinstance(load_item, dict):
                self.log("Warn: Bad house_load_additional_forecast item {}, expected dictionary".format(load_item))
                continue
            load_item = load_item.copy()
            load_item["_source"] = "yaml"
            load_item["_auto_expire"] = False
            forecast_items.append(load_item)

        self.expire_additional_load_api_commands()
        runtime_overrides = self.get_additional_load_api_overrides()
        for name, override in self.house_load_additional_forecast_overrides.items():
            runtime_overrides.setdefault(name, {}).update(override)
        for name, override in runtime_overrides.items():
            found = False
            for index, load_item in enumerate(forecast_items):
                if str(load_item.get("name", "")) == name:
                    forecast_items[index].update(override)
                    found = True
                    break
            if not found:
                forecast_items.append(override.copy())
        return forecast_items

    def fetch_additional_load_forecast(self, selected_flexible=None):
        """
        Build per-minute additional load adjustments from named forecast config.
        """
        self.load_additional_load_history()
        if selected_flexible is None:
            selected_flexible = {}
        load_adjust = {}
        forecasts = {}
        history_changed = False
        plan_interval = self.get_arg("plan_interval_minutes", 30)
        minutes_now_slot = int(self.minutes_now / plan_interval) * plan_interval

        for load_item in self.get_additional_load_forecast_config():
            name = load_item.get("name")
            if not name:
                self.log("Warn: house_load_additional_forecast item missing name")
                continue
            name = str(name)
            entity_id = self.additional_load_entity_name(name)
            mode = str(self.resolve_arg("mode", load_item.get("mode", "fixed"), "fixed")).lower()
            if mode not in ["fixed", "flexible"]:
                self.log("Warn: Bad mode {} provided in house_load_additional_forecast {}, using fixed".format(mode, name))
                mode = "fixed"
            if mode == "flexible" and name in selected_flexible:
                load_item.update(selected_flexible[name])
            enabled = self.get_additional_load_bool(load_item, "enabled", True)
            duration_configured = "duration" in load_item
            duration = self.get_additional_load_float(load_item, "duration", 0.0)
            slot_energy = self.get_additional_load_float(load_item, "slot_energy", 0.0)
            energy_total = self.get_additional_load_float(load_item, "energy", 0.0) if "energy" in load_item else None
            weighting = self.resolve_arg("weighting", load_item.get("weighting", None), None)
            source = load_item.get("_source", "yaml")
            auto_expire = load_item.get("_auto_expire", False)
            expires_minutes = load_item.get("_expires_minutes", None)
            target_times = []
            load_mode = "total_energy" if energy_total is not None else "slot_energy"
            total_energy = 0.0
            start_minutes, end_minutes = self.get_additional_load_window(load_item, mode, duration, plan_interval, minutes_now_slot)
            requested_start_minutes = load_item.get("_requested_start_minutes", start_minutes) if "start_time" not in load_item else start_minutes
            requested_end_minutes = end_minutes
            if mode == "fixed" and duration <= 0 and not duration_configured and start_minutes is not None and end_minutes is not None:
                duration = (end_minutes - start_minutes) / 60.0
            periods = int((int(duration * 60) + plan_interval - 1) / plan_interval) if duration > 0 else 0
            weights = self.parse_additional_load_weighting(weighting, periods)
            weight_total = sum(weights)

            selected_start_minutes = load_item.get("_selected_start_minutes", None)
            selection_locked = False
            if selected_start_minutes is not None:
                start_minutes = int(selected_start_minutes)
                if requested_start_minutes is not None and start_minutes < requested_start_minutes:
                    start_minutes = requested_start_minutes
                end_minutes = start_minutes + int(duration * 60)
                selection_locked = mode == "flexible" and minutes_now_slot >= start_minutes and minutes_now_slot < end_minutes
                if auto_expire:
                    expires_minutes = end_minutes
                if selection_locked and source != "yaml":
                    self.house_load_additional_forecast_overrides.setdefault(name, {"name": name})["_selection_locked"] = True
            if auto_expire and expires_minutes is None and end_minutes is not None:
                expires_minutes = end_minutes
            if auto_expire and source != "yaml" and expires_minutes is not None:
                self.house_load_additional_forecast_overrides.setdefault(name, {"name": name})["_expires_minutes"] = expires_minutes
                self.update_additional_load_api_command_metadata(name, {"_expires_at": self.additional_load_minutes_to_stamp(expires_minutes)})

            if source == "yaml" and energy_total is None and slot_energy == 0 and duration == 0 and not duration_configured:
                continue

            # Positional arguments shared by every published record variant for this load
            record_args = (
                entity_id,
                enabled,
                mode,
                energy_total,
                slot_energy,
                duration,
                weighting,
                load_mode,
                plan_interval,
                requested_start_minutes,
                requested_end_minutes,
                periods,
                weights,
                weight_total,
                source,
                auto_expire,
                expires_minutes,
            )
            combined_selection_locked = load_item.get("_selection_locked", False) or selection_locked

            if not enabled or start_minutes is None or (energy_total is None and slot_energy == 0) or (energy_total == 0) or duration == 0 or end_minutes is None:
                forecasts[name] = self.additional_load_forecast_record(*record_args, selection_locked=combined_selection_locked, state="off")
                continue

            if mode == "flexible" and selected_start_minutes is None:
                forecasts[name] = self.additional_load_forecast_record(*record_args, selection_reason="pending_prediction_metric", selection_locked=combined_selection_locked, state="off")
                continue

            if mode == "flexible" and selected_start_minutes is not None and not selection_locked:
                forecasts[name] = self.additional_load_forecast_record(
                    *record_args,
                    suggested_start_minutes=start_minutes,
                    suggested_end_minutes=end_minutes,
                    selection_reason=load_item.get("_selection_reason", "prediction_metric"),
                    candidate_count=load_item.get("_candidate_count", 0),
                    selected_metric=load_item.get("_selected_metric", None),
                    baseline_metric=load_item.get("_baseline_metric", None),
                    state="off",
                )
                continue

            for period in range(periods):
                slot_start = start_minutes + period * plan_interval
                slot_end = min(slot_start + plan_interval, end_minutes)
                slot_minutes = slot_end - slot_start
                energy, adjustment_energy = self.additional_load_slot_energies(energy_total, slot_energy, weights, weight_total, period, slot_minutes, plan_interval)
                if slot_end <= minutes_now_slot:
                    history_changed |= self.archive_additional_load_slot(name, source, mode, slot_start, slot_end, energy, plan_interval)
                    continue
                if (slot_start - minutes_now_slot) >= self.forecast_minutes:
                    continue
                total_energy += energy
                for minute in range(slot_start, slot_end):
                    load_adjust[minute] = dp4(load_adjust.get(minute, 0.0) + adjustment_energy)
                target_times.append(
                    {
                        "start": self.additional_load_minutes_to_iso(slot_start),
                        "end": self.additional_load_minutes_to_iso(slot_end),
                        "energy": energy,
                    }
                )

            forecasts[name] = self.additional_load_forecast_record(
                *record_args,
                target_times=target_times,
                total_energy=total_energy,
                suggested_start_minutes=start_minutes if mode == "flexible" and target_times else None,
                suggested_end_minutes=end_minutes if mode == "flexible" and target_times else None,
                selection_reason=load_item.get("_selection_reason", "prediction_metric" if mode == "flexible" and target_times else None),
                candidate_count=load_item.get("_candidate_count", 0),
                selected_metric=load_item.get("_selected_metric", None),
                baseline_metric=load_item.get("_baseline_metric", None),
                selection_locked=combined_selection_locked,
            )

        if history_changed:
            self.publish_additional_load_history()
        return load_adjust, forecasts

    def publish_additional_load_forecasts(self):
        """
        Publish named additional load forecast binary sensors for visibility and automation targeting.
        """
        if not hasattr(self, "house_load_additional_forecast_entities"):
            self.house_load_additional_forecast_entities = set()
        published_entities = set()
        for name, forecast in self.house_load_additional_forecasts.items():
            attributes = {
                "friendly_name": "Predbat load forecast delta {}".format(name),
                "icon": "mdi:home-lightning-bolt",
                "name": name,
                "enabled": forecast.get("enabled", False),
                "mode": forecast.get("mode", "fixed"),
                "energy": forecast.get("energy", None),
                "slot_energy": forecast.get("slot_energy", 0.0),
                "duration": forecast.get("duration", 0.0),
                "weighting": forecast.get("weighting", None),
                "load_mode": forecast.get("load_mode", "total_energy"),
                "plan_interval_minutes": forecast.get("plan_interval_minutes", self.plan_interval_minutes),
                "slots": forecast.get("slots", 0),
                "total_energy": forecast.get("total_energy", 0.0),
                "requested_start": forecast.get("requested_start", None),
                "requested_end": forecast.get("requested_end", None),
                "suggested_start": forecast.get("suggested_start", None),
                "suggested_end": forecast.get("suggested_end", None),
                "selection_reason": forecast.get("selection_reason", None),
                "candidate_count": forecast.get("candidate_count", 0),
                "selected_metric": forecast.get("selected_metric", None),
                "baseline_metric": forecast.get("baseline_metric", None),
                "selection_locked": forecast.get("selection_locked", False),
                "source": forecast.get("source", "yaml"),
                "auto_expire": forecast.get("auto_expire", False),
                "expires_at": forecast.get("expires_at", None),
                "target_times": forecast.get("target_times", []),
            }
            self.dashboard_item(forecast["entity_id"], state=forecast.get("state", "off"), attributes=attributes)
            published_entities.add(forecast["entity_id"])
            if forecast.get("source", "yaml") != "yaml":
                delete_entity = self.additional_load_delete_entity_name(name)
                self.dashboard_item(
                    delete_entity,
                    state="idle",
                    attributes={
                        "friendly_name": "Delete Predbat load forecast delta {}".format(name),
                        "icon": "mdi:delete",
                        "name": name,
                        "source": forecast.get("source", "api"),
                    },
                )
                published_entities.add(delete_entity)
        for entity_id in self.house_load_additional_forecast_entities - published_entities:
            self.unpublish_additional_load_entity(entity_id)
        self.house_load_additional_forecast_entities = published_entities

    def unpublish_additional_load_entity(self, entity_id):
        """
        Remove a stale additional load forecast entity from HA and the local dashboard cache.
        """
        if hasattr(self, "delete_state_wrapper"):
            self.delete_state_wrapper(entity_id)
        self.dashboard_values.pop(entity_id, None)
        if entity_id in self.dashboard_index:
            self.dashboard_index.remove(entity_id)

    def additional_load_candidate_profile(self, forecast, start_minutes):
        """
        Build absolute-minute adjustment and target metadata for one flexible load candidate.
        """
        plan_interval = forecast.get("plan_interval_minutes", self.plan_interval_minutes)
        duration_minutes = int(forecast.get("duration", 0.0) * 60)
        end_minutes = start_minutes + duration_minutes
        periods = forecast.get("_periods", 0)
        weights = forecast.get("_weights", [])
        weight_total = forecast.get("_weight_total", sum(weights))
        energy_total = forecast.get("energy", None)
        slot_energy = forecast.get("slot_energy", 0.0)
        load_adjust = {}
        target_times = []
        total_energy = 0.0

        for period in range(periods):
            slot_start = start_minutes + period * plan_interval
            slot_end = min(slot_start + plan_interval, end_minutes)
            slot_minutes = slot_end - slot_start
            if slot_end <= self.minutes_now:
                continue
            if (slot_start - self.minutes_now) >= self.forecast_minutes:
                continue
            energy, adjustment_energy = self.additional_load_slot_energies(energy_total, slot_energy, weights, weight_total, period, slot_minutes, plan_interval)
            total_energy += energy
            for minute in range(slot_start, slot_end):
                load_adjust[minute] = dp4(load_adjust.get(minute, 0.0) + adjustment_energy)
            target_times.append({"start": (self.midnight_utc + timedelta(minutes=slot_start)).isoformat(), "end": (self.midnight_utc + timedelta(minutes=slot_end)).isoformat(), "energy": energy})

        return load_adjust, target_times, dp4(total_energy)

    def add_additional_load_to_step_data(self, load_minutes_step, load_adjust):
        """
        Add absolute-minute additional load adjustment into prediction step data.
        """
        modified_load = copy.deepcopy(load_minutes_step)
        if not load_adjust:
            return modified_load
        for minute_relative in range(0, self.forecast_minutes, PREDICT_STEP):
            minute_absolute = self.minutes_now + minute_relative
            bucket_energy = 0
            for offset in range(PREDICT_STEP):
                bucket_energy += load_adjust.get(minute_absolute + offset, 0.0) / float(self.plan_interval_minutes)
            if bucket_energy <= 0:
                continue
            modified_load[minute_relative] = dp4(modified_load.get(minute_relative, 0.0) + bucket_energy)
        return modified_load

    def select_flexible_additional_loads(self, load_minutes_step, load_minutes_step10, pv_forecast_minute_step, pv_forecast_minute10_step):
        """
        Select flexible additional load start times using full prediction metric impact.
        """
        self.house_load_additional_flexible_selection_changed = False
        flexible_forecasts = {name: forecast for name, forecast in self.house_load_additional_forecasts.items() if forecast.get("enabled") and forecast.get("mode") == "flexible" and not forecast.get("selection_locked", False)}
        if not flexible_forecasts:
            return False, load_minutes_step, load_minutes_step10

        selected_flexible = {}
        working_load_step = load_minutes_step
        working_load_step10 = load_minutes_step10

        # Cap how far ahead a flexible load may be searched/placed, defaults to the forecast horizon (typically 48 hours).
        # Lowering this in apps.yaml bounds the number of prediction passes for deadline-less flexible loads.
        flexible_max_hours = self.get_arg("house_load_additional_flexible_max_hours", int(self.forecast_minutes / 60))
        flexible_max_minutes = min(max(int(flexible_max_hours * 60), self.plan_interval_minutes), self.forecast_minutes)

        for name, forecast in flexible_forecasts.items():
            start_minutes = forecast.get("_requested_start_minutes", None)
            end_minutes = forecast.get("_requested_end_minutes", None)
            duration_minutes = int(forecast.get("duration", 0.0) * 60)
            plan_interval = forecast.get("plan_interval_minutes", self.plan_interval_minutes)
            if start_minutes is None or end_minutes is None or duration_minutes <= 0:
                continue

            candidate = max(start_minutes, self.minutes_now)
            candidate = int((candidate + plan_interval - 1) / plan_interval) * plan_interval
            latest_start = min(end_minutes - duration_minutes, self.minutes_now + flexible_max_minutes - duration_minutes)
            if latest_start < candidate:
                continue

            baseline_prediction = Prediction(self, pv_forecast_minute_step, pv_forecast_minute10_step, working_load_step, working_load_step10)
            baseline_metric = self.score_flexible_additional_load_prediction(baseline_prediction)
            best_start = None
            best_metric = None
            candidate_count = 0

            while candidate <= latest_start:
                candidate_adjust, _, _ = self.additional_load_candidate_profile(forecast, candidate)
                candidate_load_step = self.add_additional_load_to_step_data(working_load_step, candidate_adjust)
                candidate_load_step10 = self.add_additional_load_to_step_data(working_load_step10, candidate_adjust)
                candidate_prediction = Prediction(self, pv_forecast_minute_step, pv_forecast_minute10_step, candidate_load_step, candidate_load_step10)
                candidate_metric = self.score_flexible_additional_load_prediction(candidate_prediction)
                candidate_count += 1
                if best_metric is None or candidate_metric < best_metric:
                    best_metric = candidate_metric
                    best_start = candidate
                candidate += plan_interval

            if best_start is not None:
                existing_start = forecast.get("_selected_start_minutes", None)
                if existing_start is None and forecast.get("suggested_start"):
                    existing_start = self.additional_load_stamp_to_minutes(forecast.get("suggested_start"))
                if existing_start != best_start:
                    self.house_load_additional_flexible_selection_changed = True
                best_adjust, _, _ = self.additional_load_candidate_profile(forecast, best_start)
                working_load_step = self.add_additional_load_to_step_data(working_load_step, best_adjust)
                working_load_step10 = self.add_additional_load_to_step_data(working_load_step10, best_adjust)
                selected_flexible[name] = {
                    "_requested_start_minutes": start_minutes,
                    "_requested_end_minutes": end_minutes,
                    "_selected_start_minutes": best_start,
                    "_selection_reason": "prediction_metric",
                    "_candidate_count": candidate_count,
                    "_selected_metric": dp2(best_metric) if best_metric is not None else None,
                    "_baseline_metric": dp2(baseline_metric),
                    "_expires_minutes": best_start + duration_minutes if forecast.get("auto_expire", False) else None,
                }
                if forecast.get("auto_expire", False):
                    self.house_load_additional_forecast_overrides[name] = {"name": name, **selected_flexible[name]}
                    self.update_additional_load_api_command_metadata(
                        name,
                        {
                            "_requested_start": self.additional_load_minutes_to_stamp(start_minutes),
                            "_requested_end": self.additional_load_minutes_to_stamp(end_minutes),
                            "_selected_start": self.additional_load_minutes_to_stamp(best_start),
                            "_selected_end": self.additional_load_minutes_to_stamp(best_start + duration_minutes),
                            "_expires_at": self.additional_load_minutes_to_stamp(best_start + duration_minutes),
                            "_selection_reason": "prediction_metric",
                            "_candidate_count": candidate_count,
                            "_selected_metric": dp2(best_metric) if best_metric is not None else None,
                            "_baseline_metric": dp2(baseline_metric),
                        },
                    )
                self.log("Flexible additional load {} selected {}-{} using prediction metric {} from {} candidates".format(name, self.time_abs_str(best_start), self.time_abs_str(best_start + duration_minutes), dp2(best_metric), candidate_count))

        if not selected_flexible:
            return False, load_minutes_step, load_minutes_step10

        self.house_load_additional_forecast_adjust, self.house_load_additional_forecasts = self.fetch_additional_load_forecast(selected_flexible=selected_flexible)
        self.publish_additional_load_forecasts()
        return True, working_load_step, working_load_step10

    def score_flexible_additional_load_prediction(self, prediction):
        """
        Score a flexible additional load candidate using the same full metric as the optimiser.
        """
        (
            cost10,
            _import_kwh_battery10,
            _import_kwh_house10,
            _export_kwh10,
            _soc_min10,
            soc10,
            _soc_min_minute10,
            _battery_cycle10,
            _metric_keep10,
            final_iboost10,
            _final_carbon_g10,
            *_prediction_detail10,
        ) = prediction.run_prediction(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, True, self.end_record)
        (
            cost,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            _soc_min,
            soc,
            _soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
            *_prediction_detail,
        ) = prediction.run_prediction(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, self.end_record)
        metric, _battery_value = self.compute_metric(
            self.end_record,
            soc,
            soc10,
            cost,
            cost10,
            final_iboost,
            final_iboost10,
            battery_cycle,
            metric_keep,
            final_carbon_g,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
        )
        return metric

    def additional_load_plan_time(self, timestamp):
        """
        Return a compact local time string for an additional load timestamp.
        """
        return datetime.fromisoformat(timestamp).strftime("%H:%M")

    def get_additional_load_text(self):
        """
        Return a textual summary of planned and suggested additional load forecasts.
        """
        planned_loads = []
        for name, forecast in sorted(getattr(self, "house_load_additional_forecasts", {}).items()):
            if not forecast.get("enabled", False):
                continue
            target_times = forecast.get("target_times", [])
            total_energy = forecast.get("total_energy", 0.0)
            if target_times and total_energy > 0:
                running = forecast.get("selection_locked", False)
                start = forecast.get("suggested_start") if running and forecast.get("suggested_start") else target_times[0].get("start")
                end = forecast.get("suggested_end") if running and forecast.get("suggested_end") else target_times[-1].get("end")
                if running:
                    total_energy = forecast.get("energy", total_energy) or total_energy
                    status = "running"
                    text = "{} is running from {} to {} using {:.2f} kWh".format(name, self.additional_load_plan_time(start), self.additional_load_plan_time(end), dp2(total_energy)) if start and end else None
                else:
                    status = "planned"
                    text = "{} from {} to {} using {:.2f} kWh is planned".format(name, self.additional_load_plan_time(start), self.additional_load_plan_time(end), dp2(total_energy)) if start and end else None
            else:
                start = forecast.get("suggested_start")
                end = forecast.get("suggested_end")
                total_energy = forecast.get("energy", 0.0)
                if not total_energy:
                    plan_interval = forecast.get("plan_interval_minutes", self.plan_interval_minutes)
                    periods = int((int(forecast.get("duration", 0.0) * 60) + plan_interval - 1) / plan_interval) if plan_interval > 0 else 0
                    total_energy = forecast.get("slot_energy", 0.0) * periods
                status = "suggested"
                text = "{} is suggested from {} to {} using {:.2f} kWh".format(name, self.additional_load_plan_time(start), self.additional_load_plan_time(end), dp2(total_energy)) if start and end and total_energy > 0 else None

            if not start or not end:
                continue
            if not text:
                continue
            planned_loads.append(
                {
                    "name": name,
                    "start": start,
                    "end": end,
                    "status": status,
                    "text": text,
                }
            )

        if not planned_loads:
            return ""

        planned_loads = sorted(planned_loads, key=lambda load: load["start"])
        if len(planned_loads) == 1:
            return "- Additional load {}.\n".format(planned_loads[0]["text"])
        return "- Additional loads are planned/suggested: {}.\n".format("; ".join(load["text"] for load in planned_loads))

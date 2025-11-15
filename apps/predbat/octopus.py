# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import requests
import re
from datetime import datetime, timedelta, timezone
from config import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2, dp4, minute_data
import aiohttp
import asyncio
import json
import time
import os
import yaml
import traceback
from config import TIME_FORMAT
import json
import pytz

user_agent_value = "predbat-octopus-energy"
integration_context_header = "Ha-Integration-Context"

DATE_STR_FORMAT = "%Y-%m-%d"
DATE_TIME_STR_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Hard-wired smart meter day/night rate times
OCTOPUS_NIGHT_RATE_START_HOUR = 0
OCTOPUS_NIGHT_RATE_START_MINUTE = 30
OCTOPUS_NIGHT_RATE_END_HOUR = 7
OCTOPUS_NIGHT_RATE_END_MINUTE = 30

OCTOPUS_DAY_RATE_START_HOUR = 7
OCTOPUS_DAY_RATE_START_MINUTE = 30
OCTOPUS_DAY_RATE_END_HOUR = 0
OCTOPUS_DAY_RATE_END_MINUTE = 30

BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(4 * 60, 11 * 60, 30)]


def is_active(now_utc, activeFrom, activeTo):
    if not activeFrom:
        return False
    if now_utc < activeFrom:
        return False
    if not activeTo:
        return True
    if now_utc > activeTo:
        return False
    return True


def parse_date(dt_str):
    """Convert a date string to a date object."""
    try:
        return datetime.strptime(dt_str, DATE_STR_FORMAT)
    except (ValueError, TypeError):
        return None


def parse_date_time(dt_str):
    """Convert a date string to a date object."""
    try:
        return datetime.strptime(dt_str, DATE_TIME_STR_FORMAT)
    except (ValueError, TypeError):
        return None


api_token_query = """mutation {{
	obtainKrakenToken(input: {{ APIKey: "{api_key}" }}) {{
		token
	}}
}}"""

account_query = """query {{
  octoplusAccountInfo(accountNumber: "{account_id}") {{
    isOctoplusEnrolled
  }}
  octoHeatPumpControllerEuids(accountNumber: "{account_id}")
  account(accountNumber: "{account_id}") {{
    electricityAgreements(active: true) {{
			meterPoint {{
				mpan
				meters(includeInactive: false) {{
          activeFrom
          activeTo
          makeAndType
					serialNumber
          makeAndType
          meterType
          smartExportElectricityMeter {{
						deviceId
            manufacturer
            model
            firmwareVersion
					}}
          smartImportElectricityMeter {{
						deviceId
            manufacturer
            model
            firmwareVersion
					}}
				}}
				agreements(includeInactive: true) {{
					validFrom
					validTo
          tariff {{
            ... on TariffType {{
              productCode
              tariffCode
            }}
          }}
				}}
			}}
    }}
    gasAgreements(active: true) {{
			meterPoint {{
				mprn
				meters(includeInactive: false) {{
          activeFrom
          activeTo
					serialNumber
          consumptionUnits
          modelName
          mechanism
          smartGasMeter {{
						deviceId
            manufacturer
            model
            firmwareVersion
					}}
				}}
				agreements(includeInactive: true) {{
					validFrom
					validTo
					tariff {{
						tariffCode
            productCode
					}}
				}}
			}}
    }}
  }}
}}"""

intelligent_device_query = """query {{
  electricVehicles {{
		make
		models {{
			model
			batterySize
		}}
	}}
	chargePointVariants {{
		make
		models {{
			model
			powerInKw
		}}
	}}
  devices(accountNumber: "{account_id}") {{
		id
		provider
		deviceType
    status {{
      current
    }}
		__typename
		... on SmartFlexVehicle {{
			make
			model
		}}
		... on SmartFlexChargePoint {{
			make
			model
		}}
	}}
}}"""

intelligent_dispatches_query = """query {{
  devices(accountNumber: "{account_id}", deviceId: "{device_id}") {{
		id
    status {{
      currentState
    }}
  }}
	plannedDispatches(accountNumber: "{account_id}") {{
		start
		end
    delta
    meta {{
			source
      location
		}}
	}}
	completedDispatches(accountNumber: "{account_id}") {{
		start
		end
    delta
    meta {{
			source
      location
		}}
	}}
}}"""

intelligent_settings_query = """query {{
  devices(accountNumber: "{account_id}", deviceId: "{device_id}") {{
		id
    status {{
      isSuspended
    }}
		... on SmartFlexVehicle {{
			chargingPreferences {{
				weekdayTargetTime
				weekdayTargetSoc
				weekendTargetTime
				weekendTargetSoc
				minimumSoc
				maximumSoc
			}}
		}}
		... on SmartFlexChargePoint {{
			chargingPreferences {{
				weekdayTargetTime
				weekdayTargetSoc
				weekendTargetTime
				weekendTargetSoc
				minimumSoc
				maximumSoc
			}}
		}}
	}}
}}"""

octoplus_saving_session_query = """query {{
	savingSessions {{
    events(getDevEvents: false) {{
			id
      code
			rewardPerKwhInOctoPoints
			startAt
			endAt
      devEvent
		}}
		account(accountNumber: "{account_id}") {{
			hasJoinedCampaign
			joinedEvents {{
				eventId
				startAt
				endAt
        rewardGivenInOctoPoints
			}}
		}}
	}}
}}"""

octoplus_saving_session_join_mutation = """mutation {{
	joinSavingSessionsEvent(input: {{
		accountNumber: "{account_id}"
		eventCode: "{event_code}"
	}}) {{
		possibleErrors {{
			message
		}}
	}}
}}
"""

intelligent_settings_mutation = """mutation {{
  setDevicePreferences(input: {{
    deviceId: "{device_id}"
    mode: CHARGE
    unit: PERCENTAGE
    schedules: [{schedules}]
  }}) {{
    id
  }}
}}"""

intelligent_settings_mutation_schedule = """{{
    dayOfWeek: {day_of_week}
    time: "{target_time}"
    max: {target_percentage}
}}"""


class OctopusEnergyApiClient:
    def __init__(self, api_key, log, timeout_in_seconds=20):
        if api_key is None:
            raise Exception("Octopus API KEY is not set")

        self.api_key = api_key
        self.log = log
        self.base_url = "https://api.octopus.energy"

        self.default_headers = {"user-agent": f"{user_agent_value}/1.0"}
        self.timeout = aiohttp.ClientTimeout(total=None, sock_connect=timeout_in_seconds, sock_read=timeout_in_seconds)

        self.session = None
        self.saving_sessions_to_join = []

    async def async_close(self):
        if self.session is not None:
            await self.session.close()

    async def async_create_client_session(self):
        if self.session is not None:
            return self.session

        self.session = aiohttp.ClientSession(headers=self.default_headers, skip_auto_headers=["User-Agent"])
        return self.session


class OctopusAPI:
    def __init__(self, api_key, account_id, automatic, base):
        self.api_key = api_key
        self.base = base
        self.log = base.log
        self.local_tz = base.local_tz
        self.plan_interval_minutes = base.plan_interval_minutes
        self.api = OctopusEnergyApiClient(api_key, self.log)
        self.stop_api = False
        self.account_id = account_id
        self.graphql_token = None
        self.graphql_expiration = None
        self.account_data = {}
        self.now_utc = None
        self.url_cache = {}
        self.tariffs = {}
        self.account_data = {}
        self.saving_sessions = {}
        self.saving_sessions_to_join = []
        self.intelligent_device = {}
        self.api_started = False
        self.cache_path = self.base.config_root + "/cache"
        self.automatic = automatic
        self.commands = []

        # API request metrics for monitoring
        self.requests_total = 0
        self.failures_total = 0
        self.last_success_timestamp = None
        if not os.path.exists(self.cache_path):
            os.makedirs(self.cache_path)
        self.cache_file = self.cache_path + "/octopus.yaml"

    async def select_event(self, entity_id, value):
        if entity_id == self.get_entity_name("select", "intelligent_target_time"):
            self.commands.append({"command": "set_intelligent_target_time", "value": value})
        elif entity_id == self.get_entity_name("select", "saving_session_join"):
            self.commands.append({"command": "join_saving_session_event", "event_code": value})

    async def number_event(self, entity_id, value):
        if entity_id == self.get_entity_name("number", "intelligent_target_soc"):
            # Set the target soc
            try:
                value = int(value)
            except ValueError:
                self.log("Error: Invalid value for intelligent target soc: {}".format(value))
                return
            self.commands.append({"command": "set_intelligent_target_percentage", "value": value})

    async def switch_event(self, entity_id, service):
        pass

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("Octopus API: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: Octopus API: Failed to start")
            return False
        return True

    def is_alive(self):
        return self.api_started and self.account_data

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        return self.last_success_timestamp

    async def start(self):
        """
        Main run loop
        """
        # Load cached data
        await self.load_octopus_cache()

        first = True
        while not self.stop_api:
            try:
                # Update time every minute
                self.now = datetime.now()
                self.now_utc = datetime.now(self.local_tz)
                count_minutes = self.now_utc.minute + self.now_utc.hour * 60

                if first or (count_minutes % 30) == 0:
                    # 30-minute update for tariff
                    self.midnight_utc = self.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                    await self.async_get_account(self.account_id)
                    await self.async_find_tariffs()

                if first or (count_minutes % 10) == 0:
                    # 10-minute update for intelligent device
                    await self.async_update_intelligent_device(self.account_id)
                    await self.fetch_tariffs(self.tariffs)
                    self.saving_sessions = await self.async_get_saving_sessions(self.account_id)
                    self.get_saving_session_data()

                if first or (count_minutes % 2) == 0:
                    # 2-minute update for intelligent device sensor
                    await self.async_intelligent_update_sensor(self.account_id)
                    await self.save_octopus_cache()

                first = False

                # Process any queued commands
                if await self.process_commands(self.account_id):
                    # Trigger a refresh
                    first = True

                if not self.api_started:
                    if self.automatic:
                        self.automatic_config(self.tariffs)
                    print("Octopus API: Started")
                    self.api_started = True

            except Exception as e:
                self.log("Error: Octopus API: {}".format(e))
                self.log("Error: " + traceback.format_exc())

            await asyncio.sleep(10)
        await self.api.async_close()
        print("Octopus API: Stopped")

    async def process_commands(self, account_id):
        """
        Process queued commands
        """
        commands = self.commands[:]
        self.commands = []
        done_command = False
        for command in commands:
            command_name = command.get("command", "")
            if command_name == "set_intelligent_target_percentage":
                value = command.get("value", None)
                await self.async_set_intelligent_target_schedule(account_id, target_percentage=int(value))
                done_command = True
            elif command_name == "set_intelligent_target_time":
                value = command.get("value", None)
                await self.async_set_intelligent_target_schedule(account_id, target_time=value)
                done_command = True
            elif command_name == "join_saving_session_event":
                event_code = command.get("event_code", None)
                await self.async_join_saving_session_events(self.account_id, event_code)
                done_command = True
        return done_command

    async def load_octopus_cache(self):
        """
        Load the octopus cache
        """
        data = {}
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r") as f:
                    data = yaml.safe_load(f)
            except Exception as e:
                self.log("Warn: Octopus API: Failed to load cache from {} - {}".format(self.cache_file, e))

            if data:
                self.account_data = data.get("account_data", {})
                self.tariffs = data.get("tariffs", {})
                self.saving_sessions = data.get("saving_sessions", {})
                self.url_cache = data.get("url_cache", {})
                self.intelligent_device = data.get("intelligent_device", {})

    async def save_octopus_cache(self):
        """
        Save the octopus cache
        """
        octopus_cache = {}
        octopus_cache["account_data"] = self.account_data
        octopus_cache["tariffs"] = self.tariffs
        octopus_cache["saving_sessions"] = self.saving_sessions
        octopus_cache["url_cache"] = self.url_cache
        octopus_cache["intelligent_device"] = self.intelligent_device
        with open(self.cache_file, "w") as f:
            yaml.dump(octopus_cache, f)

    async def stop(self):
        self.stop_api = True

    def get_tariff(self, tariff_type):
        if tariff_type in self.tariffs:
            return self.tariffs[tariff_type]
        return None

    async def async_find_tariffs(self):
        """
        Find the tariffs for the account
        """
        self.log("Find tariffs account data {}".format(self.account_data))
        if not self.account_data:
            return self.tariffs

        tariffs = {}
        gas = self.account_data.get("account", {}).get("gasAgreements", [])
        electric = self.account_data.get("account", {}).get("electricityAgreements", [])
        for agreement in electric + gas:
            meterpoint = agreement.get("meterPoint", {})
            meters = meterpoint.get("meters", [])
            agreements = meterpoint.get("agreements", [])
            isActiveMeter = False
            isImport = False
            isExport = False
            isGas = False
            deviceID_import = None
            deviceID_export = None
            deviceID_gas = None
            for meter in meters:
                activeFrom = parse_date(meter.get("activeFrom", None))
                activeTo = parse_date(meter.get("activeTo", None))
                isActiveMeter = is_active(self.now, activeFrom, activeTo)
                if isActiveMeter:
                    if meter.get("smartImportElectricityMeter", None):
                        isImport = True
                        deviceID_import = meter.get("smartImportElectricityMeter", {}).get("deviceId", None)
                    if meter.get("smartExportElectricityMeter", None):
                        isExport = True
                        deviceID_export = meter.get("smartExportElectricityMeter", {}).get("deviceId", None)
                    if meter.get("smartGasMeter", None):
                        isGas = True
                        deviceID_gas = meter.get("smartGasMeter", {}).get("deviceId", None)
                    break
            isActiveAgreement = False
            tariffCode = None
            productCode = None
            for this_agreement in agreements:
                tariff = this_agreement.get("tariff", {})
                validFrom = parse_date_time(this_agreement.get("validFrom", None))
                validTo = parse_date_time(this_agreement.get("validTo", None))
                isActiveAgreement = is_active(self.now_utc, validFrom, validTo)
                if isActiveAgreement:
                    tariffCode = tariff.get("tariffCode", None)
                    productCode = tariff.get("productCode", None)
                    break
            if isActiveMeter and isActiveAgreement:
                self.log("Octopus API: Found tariff code {} product {} device_id {}".format(tariffCode, productCode, deviceID_import))
                if isImport:
                    tariffs["import"] = {"tariffCode": tariffCode, "productCode": productCode, "deviceID": deviceID_import}
                    tariffs["import"]["data"] = self.tariffs.get("import", {}).get("data", None)
                    tariffs["import"]["standing"] = self.tariffs.get("import", {}).get("standing", None)
                if isExport:
                    tariffs["export"] = {"tariffCode": tariffCode, "productCode": productCode, "deviceID": deviceID_export}
                    tariffs["export"]["data"] = self.tariffs.get("export", {}).get("data", None)
                    tariffs["export"]["standing"] = self.tariffs.get("export", {}).get("standing", None)
                if isGas:
                    tariffs["gas"] = {"tariffCode": tariffCode, "productCode": productCode, "deviceID": deviceID_gas}
                    tariffs["gas"]["data"] = self.tariffs.get("gas", {}).get("data", None)
                    tariffs["gas"]["standing"] = self.tariffs.get("gas", {}).get("standing", None)
        self.tariffs = tariffs
        return self.tariffs

    async def async_update_intelligent_device(self, account_id):
        """
        Update the intelligent device
        """
        import_tariff = self.tariffs.get("import", {})
        tariffCode = import_tariff.get("tariffCode", "")
        if "INTELLI-" not in tariffCode:
            return
        deviceID = import_tariff.get("deviceID", None)
        if deviceID:
            completed_dispatches = self.get_intelligent_completed_dispatches()
            intelligent_device = await self.async_get_intelligent_device(account_id, deviceID, completed_dispatches)
            if intelligent_device is not None:
                if "completed_dispatches" in intelligent_device:
                    self.intelligent_device = intelligent_device
                    await self.fetch_previous_dispatch()
        return self.intelligent_device
    
    async def fetch_previous_dispatch(self):
        entity_id = self.get_entity_name("binary_sensor", "intelligent_dispatch")
        old_dispatches = self.base.get_state_wrapper(entity_id, attribute="completed_dispatches", default=[])
        if old_dispatches and isinstance(old_dispatches, list):
            current_completed = self.intelligent_device.get("completed_dispatches", [])
            for dispatch in old_dispatches:
                if isinstance(dispatch, dict):
                    already_exists = False
                    for current in current_completed:
                        current_start = parse_date_time(current.get("start", None))
                        dispatch_start = parse_date_time(dispatch.get("start", None))
                        if dispatch_start == current_start:
                            already_exists = True
                    if not already_exists:
                        self.log("Info: Adding previous dispatch to completed dispatches: {}".format(dispatch))
                        current_completed.append(dispatch)
            current_completed = sorted(current_completed, key=lambda x: parse_date_time(x["start"]))
            self.intelligent_device["completed_dispatches"] = current_completed

    def join_saving_session_event(self, event_code):
        """
        Join a saving session event
        """
        self.commands.append({"command": "join_saving_session_event", "event_code": event_code})

    async def async_set_intelligent_target_schedule(self, account_id, target_percentage=None, target_time=None):
        """
        Set the intelligent target schedule
        """
        device = self.get_intelligent_device()
        if not device:
            self.log("Warn: Octopus API: Try to set target schedule, but no intelligent device found")
            return
        device_id = device.get("device_id", None)
        if not device_id:
            self.log("Warn: Octopus API: Try to set target schedule, but no intelligent device ID found")
            return
        daysOfWeek = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
        if target_time is None:
            target_time = self.get_intelligent_target_time()
        target_time = target_time[:5]  # HH:MM format
        if target_percentage is None:
            target_percentage = self.get_intelligent_target_soc()
        self.log("Octopus API: Setting intelligent target schedule time {} percentage {}".format(target_time, target_percentage))
        schedule = ", ".join(list(map(lambda day: intelligent_settings_mutation_schedule.format(day_of_week=day, target_percentage=target_percentage, target_time=target_time), daysOfWeek)))
        await self.async_graphql_query(intelligent_settings_mutation.format(device_id=device_id, schedules=schedule), "set-intelligent-target-time", returns_data=False)

        # Update cached data
        device["weekend_target_time"] = target_time
        device["weekend_target_soc"] = target_percentage
        device["weekday_target_time"] = target_time
        device["weekday_target_soc"] = target_percentage

    async def async_join_saving_session_events(self, account_id, event_code):
        """
        Join the saving session events
        """
        if event_code:
            # Join the saving sessions
            self.log("Octopus API: Joining saving session event {}".format(event_code))
            await self.async_graphql_query(octoplus_saving_session_join_mutation.format(account_id=account_id, event_code=event_code), "join-saving-session-event", returns_data=False)
            # Re-fetch the saving sessions if we have joined any
            self.saving_sessions = await self.async_get_saving_sessions(account_id)

    def get_intelligent_device(self):
        """
        Get the intelligent device
        """
        return self.intelligent_device

    def get_intelligent_completed_dispatches(self):
        """
        Get the completed intelligent dispatches
        """
        devices = self.get_intelligent_device()
        if devices:
            return devices.get("completed_dispatches", [])
        else:
            return []

    def get_intelligent_planned_dispatches(self):
        """
        Get the intelligent dispatches
        """
        devices = self.get_intelligent_device()
        if devices:
            return devices.get("planned_dispatches", [])
        else:
            return []

    def get_intelligent_vehicle(self):
        """
        Get the intelligent vehicle
        """
        vehicle = {}

        devices = self.get_intelligent_device()
        if devices:
            vehicle["vehicleBatterySizeInKwh"] = devices.get("vehicle_battery_size_in_kwh", None)
            vehicle["chargePointPowerInKw"] = devices.get("charge_point_power_in_kw", None)
            vehicle["weekdayTargetTime"] = devices.get("weekday_target_time", None)
            vehicle["weekdayTargetSoc"] = devices.get("weekday_target_soc", None)
            vehicle["weekendTargetTime"] = devices.get("weekend_target_time", None)
            vehicle["weekendTargetSoc"] = devices.get("weekend_target_soc", None)
            vehicle["minimumSoc"] = devices.get("minimum_soc", None)
            vehicle["maximumSoc"] = devices.get("maximum_soc", None)
            vehicle["suspended"] = devices.get("suspended", None)
            vehicle["model"] = devices.get("model", None)
            vehicle["provider"] = devices.get("provider", None)
            vehicle["status"] = devices.get("status", None)
            # Remove None's from the dictionary
            vehicle = {k: v for k, v in vehicle.items() if v is not None}

        return vehicle

    def get_intelligent_battery_size(self):
        """
        Get the intelligent battery size
        """
        devices = self.get_intelligent_device()
        if devices:
            return devices.get("vehicle_battery_size_in_kwh", None)
        else:
            return None

    def get_intelligent_target_time(self):
        """
        Get the intelligent target time
        """
        devices = self.get_intelligent_device()
        if devices:
            is_weekend = self.now_utc.weekday() >= 5
            return devices.get("weekday_target_time" if not is_weekend else "weekend_target_time", None)
        else:
            return None

    def get_intelligent_target_soc(self):
        """
        Get the intelligent target soc
        """
        devices = self.get_intelligent_device()
        if devices:
            is_weekend = self.now_utc.weekday() >= 5
            return devices.get("weekday_target_soc" if not is_weekend else "weekend_target_soc", None)
        else:
            return None

    def get_entity_name(self, root, suffix):
        """
        Get the entity name
        """
        entity_name = root + ".predbat_octopus_" + self.account_id.replace("-", "_") + "_" + suffix
        entity_name = entity_name.lower()
        return entity_name

    def get_saving_session_data(self):
        """
        Get the saving sessions data
        """
        return_joined_events = []
        return_available_events = []

        available_events = self.saving_sessions.get("events", [])
        joined_events = self.saving_sessions.get("account", {}).get("joinedEvents", [])
        joined_ids = {}
        event_reward = {}
        event_code = {}

        for event in joined_events:
            event_id = event.get("eventId", None)
            if event_id:
                joined_ids[event_id] = True

        for event in available_events:
            start = event.get("startAt", None)
            end = event.get("endAt", None)
            event_id = event.get("id", None)
            code = event.get("code", None)
            if event_id:
                event_reward[event_id] = event.get("rewardPerKwhInOctoPoints", None)
                event_code[event_id] = code
            if start and end and event_id not in joined_ids:
                endDataTime = parse_date_time(end)
                if endDataTime > self.now_utc:
                    return_available_events.append({"start": start, "end": end, "octopoints_per_kwh": event.get("rewardPerKwhInOctoPoints", None), "code": code, "id": event_id})

        for event in joined_events:
            start = event.get("startAt", None)
            end = event.get("endAt", None)
            event_id = event.get("eventId", None)
            if start and end:
                return_joined_events.append({"start": start, "end": end, "octopoints_per_kwh": event_reward.get(event_id, None), "rewarded_octopoints": event.get("rewardGivenInOctoPoints", None), "id": event_id, "code": event_code.get(event_id, None)})
        saving_attributes = {"friendly_name": "Octopus Intelligent Saving Sessions", "icon": "mdi:currency-usd", "joined_events": return_joined_events, "available_events": return_available_events}
        active_event = False
        for event in joined_events:
            start = event.get("start", None)
            end = event.get("end", None)
            if start and end:
                if start <= self.now_utc and end > self.now_utc:
                    active_event = True
                    break
        self.base.dashboard_item(self.get_entity_name("binary_sensor", "saving_session"), "on" if active_event else "off", attributes=saving_attributes, app="octopus")

        # Create joiner dropdown for available events
        possible_codes = []
        for event in available_events:
            code = event.get("code", None)
            if code:
                possible_codes.append(code)
        self.base.dashboard_item(self.get_entity_name("select", "saving_session_join"), "", attributes={"options": possible_codes, "friendly_name": "Join Octopus Saving Session Event", "icon": "mdi:currency-usd"}, app="octopus")

        return return_available_events, return_joined_events

    def automatic_config(self, tariffs):
        """
        Automatic configuration of entities
        """
        self.log("Octopus API: Automatic configuration of entities")
        self.base.args["octopus_saving_session"] = self.get_entity_name("binary_sensor", "saving_session")
        self.base.args["octopus_saving_session_join"] = self.get_entity_name("select", "saving_session_join")
        for tariff in tariffs:
            self.base.args["metric_octopus_{}".format(tariff)] = self.get_entity_name("sensor", tariff + "_rates")
            if tariff == "import":
                self.base.args["metric_standing_charge"] = self.get_entity_name("sensor", tariff + "_standing")
        device = self.get_intelligent_device()
        if device:
            self.base.args["octopus_intelligent_slot"] = self.get_entity_name("binary_sensor", "intelligent_dispatch")
            self.base.args["octopus_ready_time"] = self.get_entity_name("select", "intelligent_target_time")
            self.base.args["octopus_charge_limit"] = self.get_entity_name("number", "intelligent_target_soc")

    async def async_get_saving_sessions(self, account_id):
        """
        Get the saving sessions
        """
        response_data = await self.async_graphql_query(octoplus_saving_session_query.format(account_id=self.account_id), "get-saving-sessions")
        if response_data is None:
            return self.saving_sessions
        else:
            return response_data.get("savingSessions", {})

    async def async_get_day_night_rates(self, url):
        """
        Get day and night rates from Octopus
        """
        mdata = []
        self.log("Info: Octopus tariff has day and night rates, fetching both")
        url_day = url.replace("standard-unit-rates", "day-unit-rates")
        url_night = url.replace("standard-unit-rates", "night-unit-rates")
        result_day = await self.fetch_url_cached(url_day)
        result_night = await self.fetch_url_cached(url_night)
        # is_night_rate = self.__is_between_times(rate, "00:30:00", "07:30:00", True)
        # Find the current day rate by scanning all the values looking at valid from date and picking the latest that is before now
        current_day_rate = None
        current_night_rate = None
        for rate in result_day:
            valid_from_stamp = rate.get("valid_from", "")
            # Convert from string to datetime
            valid_from_stamp = datetime.strptime(valid_from_stamp, DATE_TIME_STR_FORMAT)
            if valid_from_stamp <= self.now_utc:
                current_day_rate = rate.get("value_inc_vat", None)
        for rate in result_night:
            valid_from_stamp = rate.get("valid_from", "")
            # Convert from string to datetime
            valid_from_stamp = datetime.strptime(valid_from_stamp, DATE_TIME_STR_FORMAT)
            if valid_from_stamp <= self.now_utc:
                current_night_rate = rate.get("value_inc_vat", None)
        self.log("Info: Current day rate {} night rate {}".format(current_day_rate, current_night_rate))
        if current_day_rate is not None and current_night_rate is not None:
            # Now create a combined list of rates, start from 2 days back and go forward 3 days with the day and night rates
            night_start_time = self.now_utc.replace(hour=OCTOPUS_NIGHT_RATE_START_HOUR, minute=OCTOPUS_NIGHT_RATE_START_MINUTE, second=0, microsecond=0) - timedelta(days=2)
            night_end_time = night_start_time.replace(hour=OCTOPUS_NIGHT_RATE_END_HOUR, minute=OCTOPUS_NIGHT_RATE_END_MINUTE)
            day_start_time = night_start_time.replace(hour=OCTOPUS_DAY_RATE_START_HOUR, minute=OCTOPUS_DAY_RATE_START_MINUTE)
            day_end_time = night_start_time.replace(hour=OCTOPUS_DAY_RATE_END_HOUR, minute=OCTOPUS_DAY_RATE_END_MINUTE) + timedelta(days=1)
            for day in range(8):
                # Night rate
                mdata.append({"valid_from": night_start_time.strftime(DATE_TIME_STR_FORMAT), "valid_to": night_end_time.strftime(DATE_TIME_STR_FORMAT), "value_inc_vat": current_night_rate})
                # Day rate
                mdata.append({"valid_from": day_start_time.strftime(DATE_TIME_STR_FORMAT), "valid_to": day_end_time.strftime(DATE_TIME_STR_FORMAT), "value_inc_vat": current_day_rate})
                night_start_time += timedelta(days=1)
                night_end_time += timedelta(days=1)
                day_start_time += timedelta(days=1)
                day_end_time += timedelta(days=1)
        return mdata

    async def async_download_octopus_url(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0
        while url and pages < 3:
            self.requests_total += 1
            r = requests.get(url)
            if r.status_code not in [200, 201, 400]:
                self.failures_total += 1
                self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
                return {}
            try:
                data = r.json()
                self.last_success_timestamp = datetime.now(timezone.utc)
            except requests.exceptions.JSONDecodeError:
                self.failures_total += 1
                self.log("Warn: Error downloading Octopus data from URL {} (JSONDecodeError)".format(url))
                return {}

            if r.status_code == 400:
                detail = data.get("detail", "")
                if "This tariff has day and night rates" in detail:
                    self.log("Info: Octopus tariff has day and night rates, fetching both")
                    mdata = await self.async_get_day_night_rates(url)
                    if mdata:
                        return mdata
                    else:
                        self.failures_total += 1
                        self.log("Warn: Error downloading Octopus data from URL {} (No Results)".format(url))
                        return {}
                else:
                    self.failures_total += 1
                    self.log("Warn: Error downloading Octopus data from URL {} (400) - {}".format(url, detail))
                    return {}

            if "results" in data:
                mdata += data["results"]
            else:
                detail = data.get("detail", "")

                self.failures_total += 1
                self.log("Warn: Error downloading Octopus data from URL {} (No Results)".format(url))
                return {}
            url = data.get("next", None)
            pages += 1

        return mdata

    async def clean_url_cache(self):
        """
        Clean the URL cache
        """
        now = datetime.now()
        for url in list(self.url_cache.keys()):
            stamp = self.url_cache[url]["stamp"]
            age = now - stamp
            if age.seconds > (24 * 60 * 60):
                del self.url_cache[url]

    async def fetch_url_cached(self, url):
        """
        Fetch a URL from the cache or reload it
        """
        if url in self.url_cache:
            stamp = self.url_cache[url]["stamp"]
            pdata = self.url_cache[url]["data"]
            age = datetime.now() - stamp
            if age.seconds < (30 * 60):
                return pdata

        data = await self.async_download_octopus_url(url)
        if data:
            self.url_cache[url] = {}
            self.url_cache[url]["stamp"] = datetime.now()
            self.url_cache[url]["data"] = data
            return data
        else:
            self.log("Warn: Unable to download Octopus data from URL {}".format(url))
            return None

    async def fetch_tariffs(self, tariffs):
        """
        Fetch the tariff data
        """
        await self.clean_url_cache()

        for tariff in tariffs:
            product_code = tariffs[tariff]["productCode"]
            tariff_code = tariffs[tariff]["tariffCode"]

            if tariff == "gas":
                tariff_type = "gas"
            else:
                tariff_type = "electricity"
            tariffs[tariff]["data"] = await self.fetch_url_cached(f"https://api.octopus.energy/v1/products/{product_code}/{tariff_type}-tariffs/{tariff_code}/standard-unit-rates/")
            tariffs[tariff]["standing"] = await self.fetch_url_cached(f"https://api.octopus.energy/v1/products/{product_code}/{tariff_type}-tariffs/{tariff_code}/standing-charges/")

            rates = self.get_octopus_rates_direct(tariff)
            standing = self.get_octopus_rates_direct(tariff, standingCharge=True)

            rates_stamp = []
            for minute in range(0, 60 * 24 * 2, self.plan_interval_minutes):
                time_now = self.midnight_utc + timedelta(minutes=minute)
                rate_value = rates.get(minute, None)
                if rate_value is not None:
                    start_time = time_now.strftime(TIME_FORMAT)
                    end_time = (time_now + timedelta(minutes=self.plan_interval_minutes)).strftime(TIME_FORMAT)
                    rates_stamp.append({"start": start_time, "end": end_time, "value_inc_vat": dp4(rate_value / 100)})
            rate_now = rates.get(self.now_utc.minute + self.now_utc.hour * 60, None)
            if rate_now:
                rate_now = dp4(rate_now / 100)
            standing_now = standing.get(self.now_utc.minute + self.now_utc.hour * 60, None)
            if standing_now:
                standing_now = dp4(standing_now / 100)

            self.base.dashboard_item(
                self.get_entity_name("sensor", tariff + "_rates"),
                rate_now,
                attributes={"friendly_name": "Octopus Tariff Rates " + tariff, "icon": "mdi:currency-gbp", "standing_charge": standing_now, "rates": rates_stamp, "product_code": product_code, "tariff_code": tariff_code},
                app="octopus",
            )
            self.base.dashboard_item(
                self.get_entity_name("sensor", tariff + "_standing"),
                standing_now,
                attributes={"friendly_name": "Octopus Tariff Standing Charge " + tariff, "icon": "mdi:currency-gbp", "product_code": product_code, "tariff_code": tariff_code},
                app="octopus",
            )

    def get_octopus_rates_direct(self, tariff_type, standingCharge=False):
        """
        Get the direct import rates from Octopus
        """
        tariff = self.get_tariff(tariff_type)
        if tariff and ("data" in tariff):
            if standingCharge:
                tariff_data = tariff["standing"]
            else:
                tariff_data = tariff["data"]

            # For Octopus rate data valid to of None means forever
            if tariff_data:
                for rate in tariff_data:
                    valid_to = rate.get("valid_to", None)
                    if valid_to is None:
                        rate["valid_to"] = (self.midnight_utc + timedelta(days=7)).strftime(TIME_FORMAT_OCTOPUS)

            pdata, ignore_io = minute_data(tariff_data, 3, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
            return pdata
        else:
            # No tariff
            self.log("OctopusDirect: tariff {} not available, using zero".format(tariff_type))
            return {n: 0 for n in range(0, 60 * 24)}

    async def async_read_response(self, response, url, ignore_errors=False):
        """Reads the response, logging any json errors"""

        request_context = response.request_info.headers[integration_context_header] if integration_context_header in response.request_info.headers else "Unknown"

        text = await response.text()

        if response.status >= 400:
            if response.status >= 500:
                msg = f"Warn: Octopus API: Response received - {url} ({request_context}) - DO NOT REPORT - Octopus Energy server error ({url}): {response.status}; {text}"
                self.log(msg)
                return None
            elif response.status in [401, 403]:
                msg = f"Warn: Octopus API: Response received - {url} ({request_context}) - Unauthenticated request: {response.status}; {text}"
                self.log(msg)
                return None
            elif response.status not in [404]:
                msg = f"Warn: Octopus API: Response received - {url} ({request_context}) - Unexpected response received: {response.status}; {text}"
                self.log(msg)
                return None

            self.log(f"Warn: Octopus API: Response received - {url} ({request_context}) - Unexpected response received: {response.status}; {text}")
            return None

        data_as_json = None
        try:
            data_as_json = json.loads(text)
        except Exception as e:
            self.log(f"Warn: Octopus API: Failed to extract response json: {e} - {url} - {text}")
            return None

        if "graphql" in url and "errors" in data_as_json and ignore_errors == False:
            msg = f'Warn: Octopus API: Errors in request ({url}): {data_as_json["errors"]}'
            errors = list(map(lambda error: error["message"], data_as_json["errors"]))
            self.log(msg)

            for error in data_as_json["errors"]:
                if error["extensions"]["errorCode"] in ("KT-CT-1139", "KT-CT-1111", "KT-CT-1143"):
                    self.log(f"Warn: Octopus API: Token error - {msg} {errors}")
            return None

        return data_as_json

    async def async_refresh_token(self):
        """
        Refresh the token
        """

        if self.graphql_expiration is not None and (self.graphql_expiration - timedelta(minutes=5)) > datetime.now():
            return self.graphql_token

        client = await self.api.async_create_client_session()
        url = f"{self.api.base_url}/v1/graphql/"
        payload = {"query": api_token_query.format(api_key=self.api_key)}
        headers = {integration_context_header: "refresh-token"}

        try:
            async with client.post(url, headers=headers, json=payload) as token_response:
                token_response_body = await self.async_read_response(token_response, url)
                if (
                    token_response_body is not None
                    and "data" in token_response_body
                    and "obtainKrakenToken" in token_response_body["data"]
                    and token_response_body["data"]["obtainKrakenToken"] is not None
                    and "token" in token_response_body["data"]["obtainKrakenToken"]
                ):
                    self.graphql_token = token_response_body["data"]["obtainKrakenToken"]["token"]
                    self.graphql_expiration = datetime.now() + timedelta(hours=1)
                    return self.graphql_token
                else:
                    self.log("Warn: Octopus API: Failed to retrieve auth token")
                    return None
        except TimeoutError:
            self.log(f"Failed to connect. Timeout of {self.api.timeout} exceeded.")
            return None

    async def async_graphql_query(self, query, request_context, returns_data=True, ignore_errors=False):
        """
        Execute a graphql query
        """
        await self.async_refresh_token()
        try:
            self.requests_total += 1
            client = await self.api.async_create_client_session()
            url = f"{self.api.base_url}/v1/graphql/"
            payload = {"query": query}
            headers = {"Authorization": f"JWT {self.graphql_token}", integration_context_header: request_context}
            async with client.post(url, json=payload, headers=headers) as response:
                response_body = await self.async_read_response(response, url, ignore_errors=ignore_errors)
                if response_body and ("data" in response_body):
                    self.last_success_timestamp = datetime.now(timezone.utc)
                    return response_body["data"]
                else:
                    self.failures_total += 1
                    if returns_data:
                        self.log(f"Warn: Octopus API: Failed to retrieve data from graphql query {request_context}")
                    return None
        except TimeoutError:
            self.failures_total += 1
            self.log(f"Warn: OctopusAPI: Failed to connect. Timeout of {self.timeout} exceeded.")

        return None

    async def async_get_intelligent_device(self, account_id, device_id, completed):
        """
        Get the intelligent dispatches/device
        """
        result = None
        if device_id:
            self.log("Octopus API: Fetching intelligent dispatches for device {}".format(device_id))
            device_result = await self.async_graphql_query(intelligent_device_query.format(account_id=account_id), "get-intelligent-devices", ignore_errors=True)
            intelligent_device = {}

            planned = []
            if device_result:
                dispatch_result = await self.async_graphql_query(intelligent_dispatches_query.format(account_id=account_id, device_id=device_id), "get-intelligent-dispatches", ignore_errors=True)
                chargePointVariants = device_result.get("chargePointVariants", [])
                electricVehicles = device_result.get("electricVehicles", [])
                devices = device_result.get("devices", [])
                if not devices:
                    return None
                for device in device_result["devices"]:
                    deviceType = device.get("deviceType", None)
                    status = device.get("status", {}).get("current", None)
                    deviceTypeName = device.get("__typename", None)
                    if status == "LIVE" and deviceType == "ELECTRIC_VEHICLES":
                        isCharger = deviceTypeName == "SmartFlexChargePoint"
                        make = device.get("make", None)
                        model = device.get("model", None)
                        vehicleBatterySizeInKwh = None
                        chargePointPowerInKw = None
                        IntelligentdeviceID = device.get("id", None)
                        device_setting_result = {}

                        if IntelligentdeviceID:
                            device_setting_data = await self.async_graphql_query(intelligent_settings_query.format(account_id=account_id, device_id=IntelligentdeviceID), "get-intelligent-settings")
                            if device_setting_data:
                                for setting in device_setting_data.get("devices", []):
                                    if setting.get("id", None) == IntelligentdeviceID:
                                        device_setting_result["suspended"] = setting.get("status", {}).get("isSuspended", None)
                                        chargingPreferences = setting.get("chargingPreferences", {})
                                        device_setting_result["weekday_target_time"] = chargingPreferences.get("weekdayTargetTime", None)
                                        device_setting_result["weekday_target_soc"] = chargingPreferences.get("weekdayTargetSoc", None)
                                        device_setting_result["weekend_target_time"] = chargingPreferences.get("weekendTargetTime", None)
                                        device_setting_result["weekend_target_soc"] = chargingPreferences.get("weekendTargetSoc", None)
                                        device_setting_result["minimum_soc"] = chargingPreferences.get("minimumSoc", None)
                                        device_setting_result["maximum_soc"] = chargingPreferences.get("maximumSoc", None)
                            else:
                                return None

                        if isCharger:
                            for charger in chargePointVariants:
                                if charger.get("make", None) == make:
                                    models = charger.get("models", [])
                                    for charger_info in models:
                                        if charger_info.get("model", None) == model:
                                            chargePointPowerInKw = charger_info.get("powerInKw", None)
                        else:
                            for vehicle in electricVehicles:
                                if vehicle.get("make", None) == make:
                                    models = vehicle.get("models", [])
                                    for vehicle_info in models:
                                        if vehicle_info.get("model", None) == model:
                                            vehicleBatterySizeInKwh = vehicle_info.get("batterySize", None)

                        intelligent_device = {
                            "deviceType": deviceType,
                            "status": status,
                            "provider": make,
                            "model": model,
                            "is_charger": isCharger,
                            "charge_point_power_in_kw": chargePointPowerInKw,
                            "vehicle_battery_size_in_kwh": vehicleBatterySizeInKwh,
                            "device_id": IntelligentdeviceID,
                        }
                        if dispatch_result:
                            plannedDispatches = dispatch_result.get("plannedDispatches", [])
                            completedDispatches = dispatch_result.get("completedDispatches", [])
                            for plannedDispatch in plannedDispatches:
                                start = plannedDispatch.get("start", None)
                                end = plannedDispatch.get("end", None)
                                delta = plannedDispatch.get("delta", None)
                                meta = plannedDispatch.get("meta", {})
                                try:
                                    delta = dp4(float(delta))
                                except (ValueError, TypeError):
                                    delta = None

                                dispatch = {"start": start, "end": end, "charge_in_kwh": delta, "source": meta.get("source", None), "location": meta.get("location", None)}
                                keep = True
                                if start and end:
                                    start_date_time = parse_date_time(start)
                                    end_date_time = parse_date_time(end)
                                    minutes_now = (self.now_utc - self.midnight_utc).total_seconds() / 60
                                    if start_date_time and end_date_time and (start_date_time <= self.now_utc):
                                        # This slot has actually started, so move it to completed so its cached if withdrawn later
                                        # Make end be the end of this slot only and scale delta to the relative minutes
                                        start_minutes = (start_date_time - self.midnight_utc).total_seconds() / 60
                                        # Only consider now onwards
                                        start_minutes = max(minutes_now, start_minutes)

                                        # Align start_minutes to 30 minute slot
                                        start_minutes = (start_minutes // self.plan_interval_minutes) * self.plan_interval_minutes

                                        # Work out end of this slot
                                        end_minutes = start_minutes + self.plan_interval_minutes

                                        # End minutes to end of this slot only
                                        if end_date_time > self.now_utc:
                                            end_minutes = max(minutes_now, end_minutes)

                                        # Round up end minutes to the next slot
                                        end_minutes = ((end_minutes + self.plan_interval_minutes - 1) // self.plan_interval_minutes) * self.plan_interval_minutes

                                        # Work out slot end time
                                        completed_start_time = self.midnight_utc + timedelta(minutes=start_minutes)
                                        completed_end_time = self.midnight_utc + timedelta(minutes=end_minutes)
                                        total_minutes = (end_date_time - start_date_time).total_seconds() / 60
                                        elapsed_minutes = (completed_end_time - completed_start_time).total_seconds() / 60
                                        if total_minutes > 0 and delta is not None:
                                            adjusted_delta = dp4((delta * elapsed_minutes) / total_minutes)
                                        else:
                                            adjusted_delta = delta
                                        completed_dispatch = {
                                            "start": completed_start_time.strftime(DATE_TIME_STR_FORMAT),
                                            "end": completed_end_time.strftime(DATE_TIME_STR_FORMAT),
                                            "charge_in_kwh": adjusted_delta,
                                            "source": meta.get("source", None),
                                            "location": meta.get("location", None),
                                        }

                                        # Check if the dispatch is already in the completed list, if its already there then don't add it again
                                        found = False
                                        for cached in completed:
                                            if cached["start"] == completed_start_time.strftime(DATE_TIME_STR_FORMAT):
                                                cached.update(completed_dispatch)
                                                found = True
                                                break
                                        if not found:
                                            completed.append(completed_dispatch)

                                        # Now adjust the start to be only beyond the adjusted end time and scale delta accordingly
                                        # Work out minutes between original start and new start
                                        elapsed_minutes = (completed_end_time - start_date_time).total_seconds() / 60
                                        # Used elapsed minutes as percentage of total_minutes to scale delta
                                        if total_minutes > 0 and delta is not None:
                                            delta = dp4((delta * (total_minutes - elapsed_minutes)) / total_minutes)
                                        else:
                                            delta = None
                                        dispatch["start"] = completed_end_time.strftime(DATE_TIME_STR_FORMAT)
                                        dispatch["charge_in_kwh"] = delta
                                        # Check the remainder is not empty
                                        if completed_end_time >= end_date_time:
                                            keep = False
                                if keep:
                                    planned.append(dispatch)
                            for completedDispatch in completedDispatches:
                                start = completedDispatch.get("start", None)
                                end = completedDispatch.get("end", None)
                                delta = completedDispatch.get("delta", None)
                                meta = completedDispatch.get("meta", {})
                                try:
                                    delta = dp4(float(delta))
                                except (ValueError, TypeError):
                                    delta = None

                                dispatch = {"start": start, "end": end, "charge_in_kwh": delta, "source": meta.get("source", None), "location": meta.get("location", None)}
                                # Check if the dispatch is already in the completed list, if its already there then don't add it again
                                found = False
                                for cached in completed:
                                    if cached["start"] == start:
                                        cached.update(dispatch)
                                        found = True
                                        break
                                if not found:
                                    completed.append(dispatch)

                        # Sort by start time
                        planned = sorted(planned, key=lambda x: parse_date_time(x["start"]))
                        completed = sorted(completed, key=lambda x: parse_date_time(x["start"]))

                        # Prune completed dispatches for results older than 5 days
                        completed = [x for x in completed if parse_date_time(x["start"]) > self.now_utc - timedelta(days=5)]
                        # Store results
                        result = {**intelligent_device, **device_setting_result, "planned_dispatches": planned, "completed_dispatches": completed}
        return result

    async def async_intelligent_update_sensor(self, account_id):
        """
        Update the intelligent device sensor
        """
        intelligent_device = self.get_intelligent_device()
        if not intelligent_device:
            return
        planned = intelligent_device.get("planned_dispatches", [])
        completed = intelligent_device.get("completed_dispatches", [])

        active_event = False
        for dispatch in planned + completed:
            start = dispatch.get("start", None)
            end = dispatch.get("end", None)
            if start and end:
                start = parse_date_time(start)
                end = parse_date_time(end)
                if start <= self.now_utc and end > self.now_utc:
                    active_event = True
        dispatch_attributes = {"friendly_name": "Octopus Intelligent Dispatches", "icon": "mdi:flash", **intelligent_device}
        self.base.dashboard_item(self.get_entity_name("binary_sensor", "intelligent_dispatch"), "on" if active_event else "off", attributes=dispatch_attributes, app="octopus")

        weekday_target_time = intelligent_device.get("weekday_target_time", None)
        weekday_target_soc = intelligent_device.get("weekday_target_soc", None)
        weekend_target_time = intelligent_device.get("weekend_target_time", None)
        weekend_target_soc = intelligent_device.get("weekend_target_soc", None)
        # Check if we are on a weekend?
        if self.now_utc.weekday() >= 5:
            target_time = weekend_target_time
            target_soc = weekend_target_soc
        else:
            target_time = weekday_target_time
            target_soc = weekday_target_soc
        if target_time:
            target_time = target_time[:5]  # Only HH:MM
        self.base.dashboard_item(self.get_entity_name("select", "intelligent_target_time"), target_time, attributes={"friendly_name": "Octopus Intelligent Target Time", "icon": "mdi:clock-outline", "options": OPTIONS_TIME}, app="octopus")
        self.base.dashboard_item(self.get_entity_name("number", "intelligent_target_soc"), target_soc, attributes={"friendly_name": "Octopus Intelligent Target SOC", "icon": "mdi:battery-percent", "min": 0, "max": 100}, app="octopus")

    async def async_get_account(self, account_id):
        """
        Get the user's account
        """

        response_data = await self.async_graphql_query(account_query.format(account_id=account_id), "get-account")
        if response_data is None:
            self.log("Error: OctopusAPI: Failed to retrieve account")
            return self.account_data

        response_account = response_data.get("account", {})
        if response_account:
            self.account_data = response_data
        else:
            self.log("Error: OctopusAPI: Failed to retrieve account data for account {}".format(account_id))

        return self.account_data


class Octopus:
    def octopus_free_line(self, res, free_sessions):
        """
        Parse a line from the octopus free data
        """

        if res:
            dayname = res.group(1)
            daynumber = res.group(2)
            daysymbol = res.group(3)
            month = res.group(4)
            time_from = res.group(5)
            time_to = res.group(6)
            if "pm" in time_to:
                is_pm = True
            else:
                is_pm = False
            if "pm" in time_from:
                is_fpm = True
            elif "am" in time_from:
                is_fpm = False
            else:
                is_fpm = is_pm
            time_from = time_from.replace("am", "")
            time_from = time_from.replace("pm", "")
            time_to = time_to.replace("am", "")
            time_to = time_to.replace("pm", "")
            try:
                time_from = int(time_from)
                time_to = int(time_to)
            except (ValueError, TypeError):
                return
            if is_fpm:
                time_from += 12
            if is_pm:
                time_to += 12
            # Convert into timestamp object
            now = datetime.now()
            year = now.year
            time_from = str(time_from)
            time_to = str(time_to)
            daynumber = str(daynumber)
            if len(time_from) == 1:
                time_from = "0" + time_from
            if len(time_to) == 1:
                time_to = "0" + time_to
            if len(daynumber) == 1:
                daynumber = "0" + daynumber

            try:
                timestamp_start = datetime.strptime("{} {} {} {} {} Z".format(year, month, daynumber, str(time_from), "00"), "%Y %B %d %H %M %z")
                timestamp_end = datetime.strptime("{} {} {} {} {} Z".format(year, month, daynumber, str(time_to), "00"), "%Y %B %d %H %M %z")
                # Change to local timezone, but these times were in local zone so push the hour back to the correct one
                timestamp_start = timestamp_start.astimezone(self.local_tz)
                timestamp_end = timestamp_end.astimezone(self.local_tz)
                timestamp_start = timestamp_start.replace(hour=int(time_from))
                timestamp_end = timestamp_end.replace(hour=int(time_to))
                free_sessions.append({"start": timestamp_start.strftime(TIME_FORMAT), "end": timestamp_end.strftime(TIME_FORMAT), "rate": 0.0})
            except (ValueError, TypeError) as e:
                pass

    def download_octopus_free_func(self, url):
        """
        Download octopus free session data directly from a URL
        """
        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        try:
            r = requests.get(url)
        except requests.exceptions.ConnectionError:
            self.log("Warn: Unable to download Octopus data from URL {} (ConnectionError)".format(url))
            self.record_status("Warn: Unable to download Octopus free session data", debug=url, had_errors=True)
            return None

        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
            self.record_status("Warn: Error downloading Octopus free session data", debug=url, had_errors=True)
            return None

        # Return new data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["data"] = r.text
        return r.text

    def download_octopus_free(self, url):
        """
        Download octopus free session data.
        If response is JSON, parse as Go API response. Otherwise, use legacy HTML parsing.
        """
        free_sessions = []
        pdata = self.download_octopus_free_func(url)
        if not pdata:
            return free_sessions

        # Check if response is JSON (Go API) or HTML (legacy)
        try:
            data = json.loads(pdata)
            sessions = data.get("sessions", [])

            # Convert Go API format to PredBat format
            for session in sessions:
                if "session_start" in session and "session_end" in session:
                    start_time = datetime.fromisoformat(session["session_start"].replace("Z", "+00:00"))
                    end_time = datetime.fromisoformat(session["session_end"].replace("Z", "+00:00"))

                    start_local = start_time.astimezone(self.local_tz)
                    end_local = end_time.astimezone(self.local_tz)

                    predbat_session = {"start": start_local.strftime(TIME_FORMAT), "end": end_local.strftime(TIME_FORMAT), "rate": 0.0}
                    free_sessions.append(predbat_session)

            return free_sessions

        except json.JSONDecodeError:
            # Not JSON, use legacy HTML parsing
            return self.download_octopus_free_legacy(url)

    def download_octopus_free_legacy(self, url):
        """
        Legacy method: Download and parse HTML directly (fallback only).
        Kept for backward compatibility when Go API is unavailable.
        """
        free_sessions = []
        pdata = self.download_octopus_free_func(url)
        if not pdata:
            return free_sessions

        # Legacy parsing logic - basic pattern matching
        for line in pdata.split("\n"):
            # Look for the most common current format
            # "Last Free Electricity Session: DOUBLE Session 12-2pm, Sunday 7th September"
            if "Free Electricity Session:" in line or "Last Free Electricity:" in line:
                # Extract session time and date with regex
                match = re.search(r"(\d{1,2})-(\d{1,2})(am|pm),?\s+(\w+day)\s+(\d{1,2})(?:st|nd|rd|th)\s+(\w+)", line, re.IGNORECASE)
                if match:
                    start_hour = int(match.group(1))
                    end_hour = int(match.group(2))
                    period = match.group(3).lower()
                    day_of_week = match.group(4)
                    day_num = int(match.group(5))
                    month = match.group(6)

                    session = self.create_free_session_simple(start_hour, end_hour, period, day_num, month)
                    if session:
                        free_sessions.append(session)
                        self.log(f"Legacy parser found session: {session['start']} to {session['end']}")

            # Legacy format support for older patterns
            if "Free Electricity:" in line:
                res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)
                self.octopus_free_line(res, free_sessions)

        return free_sessions

    def to_aware_tz(self, dt):
        """
        Converts a naive datetime object to an aware datetime object
        using the pytz library.
        """
        local_tz = pytz.timezone(self.args.get("timezone", "Europe/London"))
        # The .localize() method makes the naive datetime object aware.
        aware_dt = local_tz.localize(dt)
        return aware_dt

    def create_free_session_simple(self, start_hour, end_hour, period, day_num, month):
        """
        Create a free session from basic components (simplified legacy method).
        """
        try:
            # Adjust hours for AM/PM
            if period == "pm" and start_hour != 12:
                start_hour += 12
                end_hour += 12
            elif period == "am" and start_hour == 12:
                start_hour = 0
                if end_hour == 12:
                    end_hour = 0

            # Simple month parsing
            month_names = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
            month_num = None
            for i, month_name in enumerate(month_names, 1):
                if month.lower().startswith(month_name[:3]):
                    month_num = i
                    break

            if not month_num:
                return None

            # Create session

            year = datetime.now().year
            start_time = datetime(year, month_num, day_num, start_hour, 0)
            end_time = datetime(year, month_num, day_num, end_hour, 0)

            # Determine if the specified date is in or out of daylight savings time
            start_time = self.to_aware_tz(start_time)
            end_time = self.to_aware_tz(end_time)
            return {"start": start_time.strftime(TIME_FORMAT), "end": end_time.strftime(TIME_FORMAT), "rate": 0.0}

        except Exception as e:
            self.log(f"Error in create_free_session_simple: {e}")
            return None

    def html_to_text(self, html):
        """
        Convert HTML to human-readable text by removing tags and normalizing whitespace.
        Simple text extraction that preserves line breaks for human-like reading.
        """
        # Remove HTML tags but preserve some structure
        text = html

        # Replace block elements with newlines for better text flow
        block_elements = ["</h1>", "</h2>", "</h3>", "</h4>", "</h5>", "</h6>", "</p>", "</div>", "</section>", "</article>", "</li>", "</br>", "<br/>", "<br>"]
        for element in block_elements:
            text = text.replace(element, "\n")

        # Remove remaining HTML tags
        text = re.sub(r"<[^>]+>", "", text)

        # Decode HTML entities
        text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")

        # Normalize whitespace but preserve line breaks
        lines = []
        for line in text.split("\n"):
            # Clean up each line
            cleaned = re.sub(r"\s+", " ", line.strip())
            if cleaned:  # Only keep non-empty lines
                lines.append(cleaned)

        return "\n".join(lines)

    def create_free_session(self, time_slot, day_of_week, day_num, month, original_line):
        """
        Create a free session dictionary from parsed components.
        Returns None if the session cannot be properly parsed.
        """
        try:
            # Parse the time slot (e.g., "12-2pm", "7-9am")
            time_match = re.match(r"(\d{1,2})(?::(\d{2}))?(?:am|pm)?-(\d{1,2})(?::(\d{2}))?(am|pm)", time_slot.lower())
            if not time_match:
                self.log(f"Warning: Cannot parse time slot '{time_slot}' in: {original_line[:100]}")
                return None

            start_hour = int(time_match.group(1))
            start_min = int(time_match.group(2) or 0)
            end_hour = int(time_match.group(3))
            end_min = int(time_match.group(4) or 0)
            period = time_match.group(5)  # am or pm

            # Adjust for PM times
            if period == "pm" and start_hour != 12:
                start_hour += 12
                end_hour += 12
            elif period == "am" and start_hour == 12:
                start_hour = 0
                if end_hour == 12:
                    end_hour = 0

            # Handle cases like "11pm-1am" (crosses midnight)
            if end_hour < start_hour:
                end_hour += 24

            # Parse date components
            day = int(re.sub(r"[^\d]", "", day_num))  # Remove "st", "nd", "rd", "th"

            # Estimate year (current year or next year if date has passed)
            now = datetime.now()
            year = now.year

            # Try to parse the month
            month_names = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
            month_num = None
            for i, month_name in enumerate(month_names, 1):
                if month.lower().startswith(month_name[:3]):  # Match "sep", "sept", "september"
                    month_num = i
                    break

            if not month_num:
                self.log(f"Warning: Cannot parse month '{month}' in: {original_line[:100]}")
                return None

            # Create datetime objects
            try:
                start_time = datetime(year, month_num, day, start_hour, start_min)
                end_time = datetime(year, month_num, day, end_hour % 24, end_min)

                # If end time crosses midnight, adjust the date
                if end_hour >= 24:
                    end_time += timedelta(days=1)

                # If the date is in the past, assume it's next year
                if start_time < now:
                    start_time = start_time.replace(year=year + 1)
                    end_time = end_time.replace(year=year + 1)

            except ValueError as e:
                self.log(f"Warning: Cannot create datetime from {day}/{month_num}/{year} {start_hour}:{start_min}: {e}")
                return None

            # Format for PredBat (uses TIME_FORMAT from config.py)
            session = {"start": start_time.strftime(TIME_FORMAT), "end": end_time.strftime(TIME_FORMAT), "rate": 0.0}  # Free electricity

            return session

        except Exception as e:
            self.log(f"Error creating free session from '{time_slot}' '{day_of_week}' '{day_num}' '{month}': {e}")
            return None

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        self.log("Download Octopus rates from {}".format(url))

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for retry in range(3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("Warn: Unable to download Octopus data from URL {} (data empty)".format(url))
            self.record_status("Warn: Unable to download Octopus data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.octopus_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        # Cache New Octopus data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["data"] = pdata
        return pdata

    def download_octopus_rates_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0

        while url and pages < 3:
            if self.debug_enable:
                self.log("Download {}".format(url))
            try:
                r = requests.get(url)
            except requests.exceptions.ConnectionError:
                self.log("Warn: Unable to download Octopus data from URL {} (ConnectionError)".format(url))
                self.record_status("Warn: Unable to download Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if r.status_code not in [200, 201]:
                self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                self.failures_total += 1
                self.log("Warn: Error downloading Octopus data from URL {} (JSONDecodeError)".format(url))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if "results" in data:
                mdata += data["results"]
            else:
                self.log("Warn: Error downloading Octopus data from URL {} (No Results)".format(url))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            url = data.get("next", None)
            pages += 1

        pdata, _ = minute_data(mdata, 3, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return pdata

    def add_now_to_octopus_slot(self, octopus_slots, now_utc):
        """
        For intelligent charging, add in if the car is charging now as a low rate slot (workaround for Ohme)
        """
        for car_n in range(self.num_cars):
            if self.car_charging_now[car_n]:
                minutes_start_slot = int(self.minutes_now / 30) * 30
                minutes_end_slot = minutes_start_slot + 30
                slot_start_date = self.midnight_utc + timedelta(minutes=minutes_start_slot)
                slot_end_date = self.midnight_utc + timedelta(minutes=minutes_end_slot)
                slot = {}
                slot["start"] = slot_start_date.strftime(TIME_FORMAT)
                slot["end"] = slot_end_date.strftime(TIME_FORMAT)
                octopus_slots.append(slot)
                self.log("Car is charging now - added new IO slot {}".format(slot))
        return octopus_slots

    def load_free_slot(self, octopus_free_slots, export=False, rate_replicate={}):
        """
        Load octopus free session slot
        """
        start_minutes = 0
        end_minutes = 0

        for octopus_free_slot in octopus_free_slots:
            start = octopus_free_slot["start"]
            end = octopus_free_slot["end"]
            rate = octopus_free_slot["rate"]

            if start and end:
                try:
                    start = str2time(start)
                    end = str2time(end)
                except (ValueError, TypeError):
                    start = None
                    end = None
                    self.log("Warn: Unable to decode Octopus free session start/end time {}".format(octopus_free_slot))

            if start and end:
                start_minutes = minutes_to_time(start, self.midnight_utc)
                end_minutes = min(minutes_to_time(end, self.midnight_utc), self.forecast_minutes)

            if start_minutes >= 0 and end_minutes != start_minutes and start_minutes < self.forecast_minutes:
                self.log("Setting Octopus free session in range {} - {} export {} rate {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), export, rate))
                for minute in range(start_minutes, end_minutes):
                    if export:
                        self.rate_export[minute] = rate
                    else:
                        self.rate_import[minute] = min(rate, self.rate_import[minute])
                        self.load_scaling_dynamic[minute] = self.load_scaling_free
                    rate_replicate[minute] = "saving"

    def load_saving_slot(self, octopus_saving_slots, export=False, rate_replicate={}):
        """
        Load octopus saving session slot
        """
        start_minutes = 0
        end_minutes = 0

        for octopus_saving_slot in octopus_saving_slots:
            start = octopus_saving_slot["start"]
            end = octopus_saving_slot["end"]
            rate = octopus_saving_slot["rate"]
            state = octopus_saving_slot["state"]

            if start and end:
                try:
                    start = str2time(start)
                    end = str2time(end)
                except (ValueError, TypeError):
                    start = None
                    end = None
                    self.log("Warn: Unable to decode Octopus saving session start/end time")
            if state and (not start or not end):
                self.log("Currently in saving session, assume current 30 minute slot")
                start_minutes = int(self.minutes_now / 30) * 30
                end_minutes = start_minutes + 30
            elif start and end:
                start_minutes = minutes_to_time(start, self.midnight_utc)
                end_minutes = min(minutes_to_time(end, self.midnight_utc), self.forecast_minutes)

            if start_minutes < (self.forecast_minutes + self.minutes_now):
                self.log("Setting Octopus saving session in range {} - {} export {} rate {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), export, rate))
                for minute in range(start_minutes, end_minutes):
                    if export:
                        if minute in self.rate_export:
                            self.rate_export[minute] += rate
                            rate_replicate[minute] = "saving"
                    else:
                        if minute in self.rate_import:
                            self.rate_import[minute] += rate
                            self.load_scaling_dynamic[minute] = self.load_scaling_saving
                            rate_replicate[minute] = "saving"

    def decode_octopus_slot(self, slot, raw=False):
        """
        Decode IOG slot
        """
        if "start" in slot:
            start = datetime.strptime(slot["start"], TIME_FORMAT)
            end = datetime.strptime(slot["end"], TIME_FORMAT)
        else:
            start = datetime.strptime(slot["startDtUtc"], TIME_FORMAT_OCTOPUS)
            end = datetime.strptime(slot["endDtUtc"], TIME_FORMAT_OCTOPUS)

        source = slot.get("source", "")
        location = slot.get("location", "")

        start_minutes = minutes_to_time(start, self.midnight_utc)
        end_minutes = minutes_to_time(end, self.midnight_utc)
        org_minutes = end_minutes - start_minutes

        # Cap slot times into the forecast itself
        if not raw:
            start_minutes = max(start_minutes, 0)
            end_minutes = max(min(end_minutes, self.forecast_minutes + self.minutes_now), start_minutes)

        if start_minutes == end_minutes:
            return 0, 0, 0, source, location

        cap_minutes = end_minutes - start_minutes

        # The load expected is stored in chargeKwh for the period in use
        if "charge_in_kwh" in slot:
            kwh = slot.get("charge_in_kwh", None)
        elif "energy" in slot:
            kwh = slot.get("energy", None)
        else:
            kwh = slot.get("chargeKwh", None)

        # Remove empty slots
        if kwh is None and location == "" and source == "":
            return 0, 0, 0, source, location

        # Create kWh if missing
        if kwh is None:
            kwh = org_minutes * self.car_charging_rate[0] / 60.0

        try:
            kwh = abs(float(kwh))
        except (ValueError, TypeError):
            kwh = 0.0

        if org_minutes > 0:
            kwh = kwh * cap_minutes / org_minutes
        else:
            kwh = 0

        return start_minutes, end_minutes, kwh, source, location

    def load_octopus_slots(self, octopus_slots, octopus_intelligent_consider_full):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []
        octopus_slot_low_rate = self.get_arg("octopus_slot_low_rate", True)
        car_soc = self.car_charging_soc[0]
        limit = self.car_charging_limit[0]
        slots_decoded = []

        # Decode the slots
        for slot in octopus_slots:
            start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(slot)
            if kwh > 0:
                # Don't add overlapping slots, bug in Octopus API means that sometimes slots overlap
                for current_slot in slots_decoded:
                    current_start, current_end, current_kwh, current_source, current_location = current_slot
                    if (start_minutes < current_end) and (end_minutes > current_start):
                        if start_minutes < current_start:
                            end_minutes = current_start
                        elif end_minutes > current_end:
                            start_minutes = current_end
                        else:
                            start_minutes = end_minutes  # Remove slot
                # Only add the slot if it has a non-zero duration
                if start_minutes != end_minutes:
                    slots_decoded.append((start_minutes, end_minutes, kwh, source, location))

        # Sort slots by start time
        slots_sorted = sorted(slots_decoded, key=lambda x: x[0])

        # Add in the current charging slot
        for slot in slots_sorted:
            start_minutes, end_minutes, kwh, source, location = slot
            kwh_original = kwh
            end_minutes_original = end_minutes
            if (end_minutes > start_minutes) and (end_minutes > self.minutes_now) and (not location or location == "AT_HOME"):
                kwh_expected = kwh * self.car_charging_loss
                if octopus_intelligent_consider_full:
                    kwh_expected = max(min(kwh_expected, limit - car_soc), 0)
                    kwh = dp2(kwh_expected / self.car_charging_loss)

                # Remove the remaining unused time
                if octopus_intelligent_consider_full and kwh > 0 and (min(car_soc + kwh_expected, limit) >= limit):
                    required_extra_soc = max(limit - car_soc, 0)
                    required_minutes = int(required_extra_soc / (kwh_original * self.car_charging_loss) * (end_minutes - start_minutes) + 0.5)
                    required_minutes = min(required_minutes, end_minutes - start_minutes)
                    end_minutes = start_minutes + required_minutes

                    car_soc = min(car_soc + kwh_expected, limit)
                    new_slot = {}
                    new_slot["start"] = start_minutes
                    new_slot["end"] = end_minutes
                    new_slot["kwh"] = kwh
                    new_slot["average"] = self.rate_import.get(start_minutes, self.rate_min)
                    if octopus_slot_low_rate and source != "bump-charge":
                        new_slot["average"] = self.rate_min  # Assume price in min
                    new_slot["cost"] = new_slot["average"] * kwh
                    new_slot["soc"] = car_soc
                    new_slots.append(new_slot)

                    if end_minutes_original > end_minutes:
                        new_slot = {}
                        new_slot["start"] = end_minutes
                        new_slot["end"] = end_minutes_original
                        new_slot["kwh"] = 0.0
                        new_slot["average"] = self.rate_import.get(start_minutes, self.rate_min)
                        if octopus_slot_low_rate and source != "bump-charge":
                            new_slot["average"] = self.rate_min  # Assume price in min
                        new_slot["cost"] = 0.0
                        new_slot["soc"] = car_soc
                        new_slots.append(new_slot)

                else:
                    car_soc = min(car_soc + kwh_expected, limit)
                    new_slot = {}
                    new_slot["start"] = start_minutes
                    new_slot["end"] = end_minutes
                    new_slot["kwh"] = kwh
                    new_slot["average"] = self.rate_import.get(start_minutes, self.rate_min)
                    if octopus_slot_low_rate and source != "bump-charge":
                        new_slot["average"] = self.rate_min  # Assume price in min
                    new_slot["cost"] = new_slot["average"] * kwh
                    new_slot["soc"] = car_soc
                    new_slots.append(new_slot)
        return new_slots

    def rate_add_io_slots(self, rates, octopus_slots):
        """
        # Add in any planned octopus slots
        """
        octopus_slot_low_rate = self.get_arg("octopus_slot_low_rate", True)
        if octopus_slots:
            # Add in IO slots
            for slot in octopus_slots:
                start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(slot, raw=True)

                # Ignore bump-charge slots as their cost won't change
                if source != "bump-charge" and (not location or location == "AT_HOME"):
                    # Round slots to 30 minute boundary
                    start_minutes = int(round(start_minutes / 30, 0) * 30)
                    end_minutes = int(round(end_minutes / 30, 0) * 30)

                    if octopus_slot_low_rate:
                        assumed_price = self.rate_min
                        for minute in range(start_minutes, end_minutes):
                            if minute >= (-96 * 60) and minute < self.forecast_minutes:
                                rates[minute] = assumed_price
                    else:
                        assumed_price = self.rate_import.get(start_minutes, self.rate_min)

                    self.log(
                        "Octopus Intelligent slot at {}-{} assumed price {} amount {} kWh location {} source {} octopus_slot_low_rate {}".format(
                            self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), assumed_price, kwh, location, source, octopus_slot_low_rate
                        )
                    )

        return rates

    def fetch_octopus_rates(self, entity_id, adjust_key=None):
        """
        Fetch the Octopus rates from the sensor

        :param entity_id: The entity_id of the sensor
        :param adjust_key: The key use to find Octopus Intelligent adjusted rates
        """
        data_all = []
        rate_data = {}

        if entity_id:
            # From 9.0.0 of the Octopus plugin the data is split between previous rate, current rate and next rate
            # and the sensor is replaced with an event - try to support the old settings and find the new events

            if self.debug_enable:
                self.log("Fetch Octopus rates from {}".format(entity_id))

            # Previous rates
            if "_current_rate" in entity_id:
                # Try as event
                prev_rate_id = entity_id.replace("_current_rate", "_previous_day_rates").replace("sensor.", "event.")
                data_import = self.get_state_wrapper(entity_id=prev_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    prev_rate_id = entity_id.replace("_current_rate", "_previous_rate")
                    data_import = self.get_state_wrapper(entity_id=prev_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
                    else:
                        self.log("Warn: No Octopus data in sensor {} attribute 'all_rates'".format(prev_rate_id))

            # Current rates
            if "_current_rate" in entity_id:
                current_rate_id = entity_id.replace("_current_rate", "_current_day_rates").replace("sensor.", "event.")
            else:
                current_rate_id = entity_id

            data_import = (
                self.get_state_wrapper(entity_id=current_rate_id, attribute="rates")
                or self.get_state_wrapper(entity_id=current_rate_id, attribute="all_rates")
                or self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_today")
                or self.get_state_wrapper(entity_id=current_rate_id, attribute="prices")
            )
            if data_import:
                data_all += data_import
            else:
                self.log("Warn: No Octopus data in sensor {} attribute 'all_rates' / 'rates' / 'raw_today' / 'prices'".format(current_rate_id))

            # Next rates
            if "_current_rate" in entity_id:
                next_rate_id = entity_id.replace("_current_rate", "_next_day_rates").replace("sensor.", "event.")
                data_import = self.get_state_wrapper(entity_id=next_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    next_rate_id = entity_id.replace("_current_rate", "_next_rate")
                    data_import = self.get_state_wrapper(entity_id=next_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
            else:
                # Nordpool tomorrow
                data_import = self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_tomorrow")
                if data_import:
                    data_all += data_import

        if data_all:
            rate_key = "rate"
            from_key = "from"
            to_key = "to"
            scale = 1.0
            if rate_key not in data_all[0]:
                rate_key = "value_inc_vat"
                from_key = "valid_from"
                to_key = "valid_to"
            if from_key not in data_all[0]:
                from_key = "start"
                to_key = "end"
                scale = 100.0
            if rate_key not in data_all[0]:
                rate_key = "value"
            if rate_key not in data_all[0]:
                rate_key = "price"
                from_key = "from"
                to_key = "till"
            rate_data, self.io_adjusted = minute_data(data_all, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key, adjust_key=adjust_key, scale=scale)

        return rate_data

    def fetch_octopus_sessions(self):
        """
        Fetch the Octopus saving/free sessions
        """

        # Octopus free session
        octopus_free_slots = []
        if "octopus_free_session" in self.args:
            entity_id = self.get_arg("octopus_free_session", indirect=False)
            if entity_id:
                events = self.get_state_wrapper(entity_id=entity_id, attribute="events")
                if events:
                    for event in events:
                        start = event.get("start", None)
                        end = event.get("end", None)
                        code = event.get("code", None)
                        if start and end and code:
                            start_time = str2time(start)  # reformat the saving session start & end time for improved readability
                            end_time = str2time(end)
                            diff_time = start_time - self.now_utc
                            if abs(diff_time.days) <= 3:
                                self.log("Octopus free events code {} {}-{}".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M")))
                            octopus_free_slot = {}
                            octopus_free_slot["start"] = start
                            octopus_free_slot["end"] = end
                            octopus_free_slot["rate"] = 0
                            octopus_free_slots.append(octopus_free_slot)
        # Direct Octopus URL
        if "octopus_free_url" in self.args:
            free_online = self.download_octopus_free(self.get_arg("octopus_free_url", indirect=False))
            octopus_free_slots.extend(free_online)

        # Octopus saving session
        octopus_saving_slots = []
        if "octopus_saving_session" in self.args:
            saving_rate = 200  # Default rate if not reported
            octopoints_per_penny = self.get_arg("octopus_saving_session_octopoints_per_penny", 8)  # Default 8 octopoints per found

            joined_events = []
            available_events = []
            state = False

            entity_id = self.get_arg("octopus_saving_session", indirect=False)
            if entity_id:
                state = self.get_arg("octopus_saving_session", False)
                joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")
                if not joined_events:
                    entity_id = entity_id.replace("binary_sensor.", "event.").replace("_sessions", "_session_events")
                    joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")

                available_events = self.get_state_wrapper(entity_id=entity_id, attribute="available_events")

            if available_events:
                # Only try to join every 2 hours to avoid spamming if it fails
                if not self.octopus_last_joined_try or (self.now_utc - self.octopus_last_joined_try).total_seconds() > 2 * 60 * 60:
                    for event in available_events:
                        code = event.get("code", None)  # decode the available events structure for code, start/end time & rate
                        start = event.get("start", None)
                        end = event.get("end", None)
                        start_time = str2time(start)  # reformat the saving session start & end time for improved readability
                        end_time = str2time(end)
                        saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                        if code:  # Join the new Octopus saving event and send an alert
                            self.log("Joining Octopus saving event code {} {}-{} at rate {} p/kWh".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))
                            entity_id_join = self.get_arg("octopus_saving_session_join", indirect=False)
                            if entity_id_join:
                                # Join via selector
                                self.call_service_wrapper("select/select_option", entity_id=entity_id_join, option=code)
                            else:
                                # Join via octopus event (Bottle Cap Dave)
                                self.call_service_wrapper("octopus_energy/join_octoplus_saving_session_event", event_code=code, entity_id=entity_id)
                            self.call_notify("Predbat: Joined Octopus saving event {}-{}, {} p/kWh".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))
                            self.octopus_last_joined_try = self.now_utc

            if joined_events:
                for event in joined_events:
                    start = event.get("start", None)
                    end = event.get("end", None)
                    saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                    if start and end and saving_rate > 0:
                        # Save the saving slot?
                        try:
                            start_time = str2time(start)
                            end_time = str2time(end)
                            diff_time = start_time - self.now_utc
                            if abs(diff_time.days) <= 3:
                                self.log("Joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state))

                                # Save the slot
                                octopus_saving_slot = {}
                                octopus_saving_slot["start"] = start
                                octopus_saving_slot["end"] = end
                                octopus_saving_slot["rate"] = saving_rate
                                octopus_saving_slot["state"] = state
                                octopus_saving_slots.append(octopus_saving_slot)
                        except (ValueError, TypeError):
                            self.log("Warn: Bad start time for joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state))

                # In saving session that's not reported, assumed 30-minutes
                if state and not joined_events:
                    octopus_saving_slot = {}
                    octopus_saving_slot["start"] = None
                    octopus_saving_slot["end"] = None
                    octopus_saving_slot["rate"] = saving_rate
                    octopus_saving_slot["state"] = state
                    octopus_saving_slots.append(octopus_saving_slot)
                if state:
                    self.log("Octopus Saving session is active!")
        return octopus_free_slots, octopus_saving_slots

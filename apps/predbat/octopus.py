# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------


"""Octopus Energy API integration.

Provides both REST and GraphQL API access to Octopus Energy for fetching
tariff rates, intelligent dispatch schedules, saving sessions, and account
data. Delegates caching to the StorageComponent with stale-while-revalidate
semantics for multi-pod deployments.
"""

import asyncio
import requests
import re
from datetime import datetime, timedelta, timezone
from predbat_metrics import record_api_call
from const import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2, dp4, minute_data
from component_base import ComponentBase
import aiohttp
import hashlib
import json
import os
import pytz
from ha import run_async

user_agent_value = "predbat-octopus-energy"
integration_context_header = "Ha-Integration-Context"

DATE_STR_FORMAT = "%Y-%m-%d"
DATE_TIME_STR_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Night-rate window definitions: start time, end time, whether the window crosses midnight.
# Keys: "eco7" (Economy 7), "go" (Octopus GO / generic day-night), "iog" (Intelligent GO TOU).
OCTOPUS_NIGHT_RATE_WINDOWS = {
    "eco7": {"start": (0, 30), "end": (7, 30), "cross_midnight": False},
    "go": {"start": (0, 30), "end": (5, 30), "cross_midnight": False},
    "iog": {"start": (23, 30), "end": (5, 30), "cross_midnight": True},
}

OCTOPUS_MAX_RETRIES = 5
OCTOPUS_SLOT_MAX_DEFAULT = 48  # 24 hours with 30-minute slots

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
  flexPlannedDispatches(deviceId:"{device_id}") {{
    start
    end
    type
    energyAddedKwh
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
    events(includeDev: false) {{
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
		joinedEventCodes
	}}
}}
"""

flexibility_campaign_query = """query {{
  customerFlexibilityCampaignEvents(
    accountNumber: "{account_id}"
    supplyPointIdentifier: "{mpan}"
    campaignSlug: "{campaign_slug}"
    last: 50
  ) {{
    edges {{
      node {{
        code
        startAt
        endAt
      }}
    }}
    totalCount
    pageInfo {{
      hasNextPage
      endCursor
    }}
  }}
}}"""

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
    """Low-level async HTTP client for Octopus Energy REST and GraphQL APIs.

    Handles authentication, session management, rate fetching, intelligent
    dispatch queries, and saving session management.
    """

    def __init__(self, api_key, log, timeout_in_seconds=20):
        if api_key is None:
            raise Exception("OctopusAPI: API KEY is not set")

        self.api_key = api_key
        self.log = log
        self.base_url = "https://api.octopus.energy"
        self.backend_url = "https://api.backend.octopus.energy"

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


class OctopusAPI(ComponentBase):
    """Octopus Energy integration component.

    Manages tariff discovery, rate caching, intelligent device tracking,
    saving sessions, and account data via both REST and GraphQL APIs.
    Publishes rate sensors and handles Octopus-specific features.
    """

    def initialize(self, key, account_id, automatic):
        """Initialise the Octopus API component"""
        self.api_key = key
        self.api = OctopusEnergyApiClient(key, self.log)
        self.account_id = account_id
        self.graphql_token = None
        self.graphql_expiration = None
        self.account_data = {}
        self.tariffs = {}
        self.saving_sessions = {}
        self.saving_sessions_to_join = []
        self.intelligent_devices = {}
        self.tariff_fetched_at = None
        self.device_fetched_at = None
        self.automatic = automatic
        self.commands = []
        self.mpan = None
        self.free_electricity_events = []

        # API request metrics for monitoring
        self.requests_total = 0
        self.failures_total = 0

        # In-memory cache for product info (keyed by product_code) to avoid repeated API calls
        self._product_info_cache = {}

        self.log("OctopusAPI: Initialised with account ID {}".format(self.account_id))

    async def select_event(self, entity_id, value):
        suffix = self.get_entity_suffix(entity_id)
        device_id = self.suffix_to_device_id(suffix)
        if entity_id == self.get_entity_name("select", "intelligent_target_time", index=suffix) and device_id:
            self.commands.append({"command": "set_intelligent_target_time", "value": value, "device_id": device_id})
        elif entity_id == self.get_entity_name("select", "saving_session_join"):
            self.commands.append({"command": "join_saving_session_event", "event_code": value})

    def get_entity_suffix(self, entity_id):
        """
        Extract the index suffix from an entity ID
        """
        if "_" in entity_id:
            return entity_id.split("_")[-1]
        else:
            return ""

    async def number_event(self, entity_id, value):
        suffix = self.get_entity_suffix(entity_id)
        device_id = self.suffix_to_device_id(suffix)
        if entity_id == self.get_entity_name("number", "intelligent_target_soc", index=suffix) and device_id:
            # Set the target soc
            try:
                value = int(value)
            except ValueError:
                self.log("Error: OctopusAPI: Invalid value for intelligent target soc: {}".format(value))
                return
            self.commands.append({"command": "set_intelligent_target_percentage", "value": value, "device_id": device_id})

    async def switch_event(self, entity_id, service):
        pass

    def is_alive(self):
        return self.api_started and self.account_data

    def _data_age_minutes(self, fetched_at):
        """Return how many minutes ago fetched_at was, or 9999 if not set."""
        if fetched_at is None:
            return 9999
        return (datetime.now() - fetched_at).total_seconds() / 60

    async def run(self, seconds, first):
        """
        Main run loop
        """
        if first:
            # Load cached data (restores tariff_fetched_at / device_fetched_at timestamps)
            await self.load_octopus_cache()
            self.log("OctopusAPI: Started")

        # Update time every minute
        now = datetime.now()
        count_minutes = now.minute + now.hour * 60

        # Process any queued commands
        refresh = False
        if not first and (await self.process_commands(self.account_id)):
            # Commands processed - will trigger refresh on next cycle
            refresh = True

        # On first run, use the stored fetch timestamps to decide what is stale so that fast
        # restarts skip re-fetching data that was already retrieved recently.  None means the
        # data was never fetched (no cache), so treat as stale.  Sensor data is always pushed
        # on startup so HA entities are populated immediately.

        tariff_due = self._data_age_minutes(self.tariff_fetched_at) >= 30
        device_due = refresh or self._data_age_minutes(self.device_fetched_at) >= 10
        sensor_due = first or refresh or (count_minutes % 2) == 0

        if tariff_due:
            # 30-minute API refresh for account and tariff discovery
            if await self.async_get_account(self.account_id):
                self.tariff_fetched_at = datetime.now()

        if tariff_due or first:
            # Rebuild tariff structure from account_data (no API call, needed after cache load)
            await self.async_find_tariffs()

        if device_due:
            # 10-minute API refresh for intelligent device and saving sessions
            await self.async_update_intelligent_devices(self.account_id)
            self.saving_sessions = await self.async_get_flexibility_events(self.account_id)
            self.get_saving_session_data()
            self.device_fetched_at = datetime.now()

        if device_due or first:
            # Download rate data into tariff structure (uses storage cache, needed after cache load)
            await self.fetch_tariffs(self.tariffs)

        if sensor_due:
            # 2-minute update for intelligent device sensor
            await self.async_intelligent_update_sensor(self.account_id)

        if tariff_due or device_due:
            # Don't save cache every 2 minutes, if we lose it then we re-fresh it anyhow
            await self.save_octopus_cache()

        if first and self.automatic:
            self.automatic_config(self.tariffs)

        return True

    async def final(self):
        """
        Final cleanup before stopping
        """
        await self.api.async_close()

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
                device_id = command.get("device_id", None)
                await self.async_set_intelligent_target_schedule(account_id, target_percentage=int(value), device_id=device_id)
                done_command = True
            elif command_name == "set_intelligent_target_time":
                value = command.get("value", None)
                device_id = command.get("device_id", None)
                await self.async_set_intelligent_target_schedule(account_id, target_time=value, device_id=device_id)
                done_command = True
            elif command_name == "join_saving_session_event":
                event_code = command.get("event_code", None)
                await self.async_join_saving_session_events(self.account_id, event_code)
                done_command = True
        return done_command

    def get_tariff_cache_key(self, tariff_data):
        """
        Generate cache key for a tariff based on product_code and tariff_code
        Returns: filename safe string like "AGILE-FLEX-22-11-25_E-1R-AGILE-FLEX-22-11-25-C"
        """
        product_code = tariff_data.get("productCode", "unknown")
        tariff_code = tariff_data.get("tariffCode", "unknown")
        # Sanitize for filesystem safety
        key = f"{product_code}_{tariff_code}".replace("/", "_").replace("\\", "_")
        return key

    def decode_kraken_token_expiry(self, token):
        """
        Extract expiration timestamp from Kraken JWT token without verification.
        Returns datetime object if successful, None otherwise.
        """
        import base64

        if not token:
            return None

        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None

            # Decode payload (add padding if needed)
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload_decoded = json.loads(base64.urlsafe_b64decode(payload))

            if "exp" in payload_decoded:
                return datetime.fromtimestamp(payload_decoded["exp"])
            return None
        except Exception as e:
            self.log(f"Warn: OctopusAPI: Failed to decode Kraken token expiry: {e}")
            return None

    async def load_octopus_cache(self):
        """Load the octopus user cache via the storage component, normalising missing fields."""
        data = await self.storage.load("octopus_user", "account") if self.storage else None
        if data:
            self.account_data = data.get("account_data", {})
            self.saving_sessions = data.get("saving_sessions", {})
            self.intelligent_devices = data.get("intelligent_devices", {})
            self.graphql_token = data.get("kraken_token")
            self.tariff_fetched_at = data.get("tariff_fetched_at")
            self.device_fetched_at = data.get("device_fetched_at")
            self.update_success_timestamp()

        self.tariffs = {}
        if self.account_data is None:
            self.account_data = {}
        if self.saving_sessions is None:
            self.saving_sessions = {}
        if not isinstance(self.intelligent_devices, dict):
            self.intelligent_devices = {}

    async def save_octopus_cache(self):
        """Save the octopus user cache (account data, tokens, sessions, devices) via the storage component."""
        octopus_cache = {
            "account_data": self.account_data,
            "saving_sessions": self.saving_sessions,
            "intelligent_devices": self.intelligent_devices,
            "kraken_token": self.graphql_token,
            "tariff_fetched_at": self.tariff_fetched_at,
            "device_fetched_at": self.device_fetched_at,
        }
        if self.storage:
            await self.storage.save("octopus_user", "account", octopus_cache, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(days=7))

    def get_tariff(self, tariff_type):
        if tariff_type in self.tariffs:
            return self.tariffs[tariff_type]
        return None

    async def async_find_tariffs(self):
        """
        Find the tariffs for the account
        """
        self.log("OctopusAPI: Find tariffs account data {}".format(self.account_data))
        if not self.account_data:
            return self.tariffs

        now = datetime.now()
        old_tariff_keys = set(self.tariffs.keys())

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
                isActiveMeter = is_active(now, activeFrom, activeTo)
                if isActiveMeter:
                    if meter.get("smartImportElectricityMeter", None):
                        isImport = True
                        deviceID_import = meter.get("smartImportElectricityMeter", {}).get("deviceId", None)
                        self.log("OctopusAPI: Found active import meter with device ID {}".format(deviceID_import))
                        if not self.mpan:
                            self.mpan = meterpoint.get("mpan")
                            if self.mpan:
                                self.log("OctopusAPI: Found MPAN {}".format(self.mpan[:4] + "..." + self.mpan[-4:] if len(self.mpan) > 8 else self.mpan))
                    if meter.get("smartExportElectricityMeter", None):
                        isExport = True
                        deviceID_export = meter.get("smartExportElectricityMeter", {}).get("deviceId", None)
                        self.log("OctopusAPI: Found active export meter with device ID {}".format(deviceID_export))
                    if meter.get("smartGasMeter", None):
                        isGas = True
                        deviceID_gas = meter.get("smartGasMeter", {}).get("deviceId", None)
                        self.log("OctopusAPI: Found active gas meter with device ID {}".format(deviceID_gas))
                    break
            isActiveAgreement = False
            tariffCode = None
            productCode = None
            for this_agreement in agreements:
                tariff = this_agreement.get("tariff", {})
                validFrom = parse_date_time(this_agreement.get("validFrom", None))
                validTo = parse_date_time(this_agreement.get("validTo", None))
                isActiveAgreement = is_active(self.now_utc_exact, validFrom, validTo)
                if isActiveAgreement:
                    tariffCode = tariff.get("tariffCode", None)
                    productCode = tariff.get("productCode", None)
                    break
            if isActiveMeter and isActiveAgreement:
                if not isImport and not isExport and not isGas:
                    if tariffCode and ("OUTGOING" in tariffCode or "EXPORT" in tariffCode):
                        isExport = True
                        deviceID_export = None
                        self.log("OctopusAPI: No export meter found but tariff code indicates export, treating as export tariff with device ID None")
                if isImport:
                    self.log("OctopusAPI: Adding import tariff with code {} product {} device ID {}".format(tariffCode, productCode, deviceID_import))
                    tariffs["import"] = {"tariffCode": tariffCode, "productCode": productCode, "deviceID": deviceID_import}
                    tariffs["import"]["data"] = self.tariffs.get("import", {}).get("data", None)
                    tariffs["import"]["standing"] = self.tariffs.get("import", {}).get("standing", None)
                if isExport:
                    self.log("OctopusAPI: Adding export tariff with code {} product {} device ID {}".format(tariffCode, productCode, deviceID_export))
                    tariffs["export"] = {"tariffCode": tariffCode, "productCode": productCode, "deviceID": deviceID_export}
                    tariffs["export"]["data"] = self.tariffs.get("export", {}).get("data", None)
                    tariffs["export"]["standing"] = self.tariffs.get("export", {}).get("standing", None)
                if isGas:
                    self.log("OctopusAPI: Adding gas tariff with code {} product {} device ID {}".format(tariffCode, productCode, deviceID_gas))
                    tariffs["gas"] = {"tariffCode": tariffCode, "productCode": productCode, "deviceID": deviceID_gas}
                    tariffs["gas"]["data"] = self.tariffs.get("gas", {}).get("data", None)
                    tariffs["gas"]["standing"] = self.tariffs.get("gas", {}).get("standing", None)
        self.tariffs = tariffs

        # Re-run automatic config if tariff structure changed (e.g. export agreement became active)
        new_tariff_keys = set(self.tariffs.keys())
        if old_tariff_keys and new_tariff_keys != old_tariff_keys and self.automatic:
            self.log("OctopusAPI: Tariff structure changed from {} to {}, reconfiguring".format(old_tariff_keys, new_tariff_keys))
            self.automatic_config(self.tariffs)

        return self.tariffs

    async def async_update_intelligent_devices(self, account_id):
        """
        Update the intelligent device
        """
        import_tariff = self.tariffs.get("import", {})
        tariffCode = import_tariff.get("tariffCode", "")
        if "INTELLI-" not in tariffCode:
            return
        deviceID = import_tariff.get("deviceID", None)
        if deviceID:
            intelligent_devices = await self.async_get_intelligent_devices(account_id, deviceID)
            if intelligent_devices:
                # Update existing intelligent devices with new dispatch data.
                # Always call fetch_previous_dispatch when completed dispatches are available to merge historical data.
                for device_id in intelligent_devices:
                    device = intelligent_devices[device_id]
                    if "completed_dispatches" in device:
                        self.intelligent_devices[device_id] = device
                        await self.fetch_previous_dispatch(device_id)
                    elif device_id not in self.intelligent_devices:
                        # First time seeing this device with no completed dispatches yet
                        self.intelligent_devices[device_id] = device
        return self.intelligent_devices

    def suffix_to_device_id(self, suffix):
        """
        Convert an index suffix back to a device ID
        E.g. "12345" -> "smart-meter-12345"
        This is a best-effort approach based on the assumption that the device ID ends with the suffix after a hyphen
        """
        for device_id in self.intelligent_devices:
            if device_id.endswith(suffix):
                return device_id
        return None

    def device_id_to_index_suffix(self, device_id):
        """
        Convert a device ID to an index suffix for entity naming
        E.g. "smart-meter-12345" -> "12345"
        """
        if "-" in device_id:
            return device_id.split("-")[-1]
        else:
            return device_id

    async def fetch_previous_dispatch(self, device_id):
        intelligent_device = self.intelligent_devices.get(device_id, None)
        if intelligent_device is None:
            return

        index_suffix = self.device_id_to_index_suffix(device_id)

        # Get current completed dispatches from the device data
        current_completed = intelligent_device.get("completed_dispatches", [])
        if not current_completed or not isinstance(current_completed, list):
            current_completed = []

        # Merge old dispatches with current completed dispatches, avoiding duplicates based on start time
        entity_id = self.get_entity_name("binary_sensor", "intelligent_dispatch", index=index_suffix)
        old_dispatches = self.get_state_wrapper(entity_id, attribute="completed_dispatches", default=[])
        if old_dispatches and isinstance(old_dispatches, list):
            for dispatch in old_dispatches:
                if isinstance(dispatch, dict):
                    already_exists = False
                    for current in current_completed:
                        current_start = parse_date_time(current.get("start", None))
                        dispatch_start = parse_date_time(dispatch.get("start", None))
                        if dispatch_start == current_start:
                            already_exists = True
                    if not already_exists and dispatch.get("start", None) and dispatch.get("end", None) and dispatch.get("charge_in_kwh", None):
                        current_completed.append(dispatch)

        # Remove any duplicates, give priority to those with a location set
        unique_dispatches = {}
        for dispatch in current_completed:
            start = dispatch.get("start", None)
            if start:
                key = start
                dispatch_location = dispatch.get("location") or dispatch.get("meta", {}).get("location")
                existing_location = unique_dispatches[key].get("location") or unique_dispatches[key].get("meta", {}).get("location") if key in unique_dispatches else None
                if key not in unique_dispatches or (dispatch_location and not existing_location):
                    unique_dispatches[key] = dispatch
        current_completed = list(unique_dispatches.values())
        current_completed = sorted([x for x in current_completed if x.get("start")], key=lambda x: parse_date_time(x.get("start")))
        # Prune completed dispatches for results older than 5 days
        current_completed = [x for x in current_completed if x.get("start") and parse_date_time(x.get("start")) > self.now_utc_exact - timedelta(days=5)]
        intelligent_device["completed_dispatches"] = current_completed

    def join_saving_session_event(self, event_code):
        """
        Join a saving session event
        """
        self.commands.append({"command": "join_saving_session_event", "event_code": event_code})

    async def async_set_intelligent_target_schedule(self, account_id, device_id, target_percentage=None, target_time=None):
        """
        Set the intelligent target schedule
        """
        devices = self.get_intelligent_devices()
        if not devices:
            self.log("Warn: OctopusAPI: Try to set target schedule, but no intelligent device found")
            return
        device = devices.get(device_id, None)
        if device:
            daysOfWeek = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
            if target_time is None:
                target_time = self.get_intelligent_target_time(device_id)
            if target_time and len(target_time) > 5:
                target_time = target_time[:5]  # HH:MM format
            if target_percentage is None:
                target_percentage = self.get_intelligent_target_soc(device_id)
            self.log("OctopusAPI: Setting intelligent target device_id {} schedule time {} percentage {}".format(device_id, target_time, target_percentage))
            schedule = ", ".join(list(map(lambda day: intelligent_settings_mutation_schedule.format(day_of_week=day, target_percentage=target_percentage, target_time=target_time), daysOfWeek)))
            await self.async_graphql_query(intelligent_settings_mutation.format(device_id=device_id, schedules=schedule), "set-intelligent-target-time", returns_data=False)

            # Update cached data
            device["weekend_target_time"] = target_time
            device["weekend_target_soc"] = target_percentage
            device["weekday_target_time"] = target_time
            device["weekday_target_soc"] = target_percentage
        else:
            self.log("Warn: OctopusAPI: Try to set target schedule, but no intelligent device ID {} found".format(device_id))

    async def async_join_saving_session_events(self, account_id, event_code):
        """
        Join the saving session events
        """
        if event_code:
            # Join the saving sessions
            self.log("OctopusAPI: Joining saving session event {}".format(event_code))
            await self.async_graphql_query(octoplus_saving_session_join_mutation.format(account_id=account_id, event_code=event_code), "join-saving-session-event", returns_data=False, use_backend=True)
            # Re-fetch the saving sessions if we have joined any
            self.saving_sessions = await self.async_get_saving_sessions(account_id)

    def get_intelligent_devices(self):
        """
        Get the intelligent device
        """
        return self.intelligent_devices

    def get_intelligent_completed_dispatches(self, device_id):
        """
        Get the completed intelligent dispatches
        """
        devices = self.get_intelligent_devices()
        completed_dispatches = []
        if devices:
            device = devices.get(device_id, None)
            if device:
                completed_dispatches = device.get("completed_dispatches", [])
        return completed_dispatches

    def get_intelligent_planned_dispatches(self, device_id):
        """
        Get the intelligent dispatches
        """
        devices = self.get_intelligent_devices()
        planned_dispatches = []
        if devices:
            device = devices.get(device_id, None)
            if device:
                planned_dispatches = device.get("planned_dispatches", [])
        return planned_dispatches

    def get_intelligent_vehicle(self, device_id):
        """
        Get the intelligent vehicle
        """
        vehicle = {}

        devices = self.get_intelligent_devices()
        if devices:
            device = devices.get(device_id, None)
            if device:
                vehicle["vehicleBatterySizeInKwh"] = device.get("vehicle_battery_size_in_kwh", None)
                vehicle["chargePointPowerInKw"] = device.get("charge_point_power_in_kw", None)
                vehicle["weekdayTargetTime"] = device.get("weekday_target_time", None)
                vehicle["weekdayTargetSoc"] = device.get("weekday_target_soc", None)
                vehicle["weekendTargetTime"] = device.get("weekend_target_time", None)
                vehicle["weekendTargetSoc"] = device.get("weekend_target_soc", None)
                vehicle["minimumSoc"] = device.get("minimum_soc", None)
                vehicle["maximumSoc"] = device.get("maximum_soc", None)
                vehicle["suspended"] = device.get("suspended", None)
                vehicle["model"] = device.get("model", None)
                vehicle["provider"] = device.get("provider", None)
                vehicle["status"] = device.get("status", None)
                # Remove None's from the dictionary
                vehicle = {k: v for k, v in vehicle.items() if v is not None}

        return vehicle

    def get_intelligent_battery_size(self, device_id):
        """
        Get the intelligent battery sizes
        """
        devices = self.get_intelligent_devices()
        if devices:
            device = devices.get(device_id, None)
            if device:
                return device.get("vehicle_battery_size_in_kwh", None)
        return None

    def get_intelligent_target_time(self, device_id):
        """
        Get the intelligent target times
        """
        devices = self.get_intelligent_devices()
        if devices:
            device = devices.get(device_id, None)
            if device:
                is_weekend = self.now_utc_exact.weekday() >= 5
                return device.get("weekday_target_time" if not is_weekend else "weekend_target_time", None)
        else:
            return None

    def get_intelligent_target_soc(self, device_id):
        """
        Get the intelligent target socs
        """
        devices = self.get_intelligent_devices()
        if devices:
            device = devices.get(device_id, None)
            if device:
                is_weekend = self.now_utc_exact.weekday() >= 5
                return device.get("weekday_target_soc" if not is_weekend else "weekend_target_soc", None)
        else:
            return None

    def get_entity_name(self, root, suffix, index=""):
        """
        Get the entity name
        """
        if index:
            entity_name = root + "." + self.prefix + "_octopus_" + self.account_id.replace("-", "_") + "_" + suffix + "_" + index
        else:
            entity_name = root + "." + self.prefix + "_octopus_" + self.account_id.replace("-", "_") + "_" + suffix
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
        has_joined = self.saving_sessions.get("account", {}).get("hasJoinedCampaign", False)
        joined_ids = {}
        event_reward = {}
        event_code = {}

        # Default saving session rate in octopoints/kWh
        # octopus_saving_session_rate is in p/kWh, convert to octopoints
        octopoints_per_penny = self.get_arg("octopus_saving_session_octopoints_per_penny", 8)
        default_rate_pence = self.get_arg("octopus_saving_session_rate", 100)  # 100p/kWh default
        default_octopoints = default_rate_pence * octopoints_per_penny

        if not has_joined:
            self.log("OctopusAPI: User has not joined Octopus saving sessions campaign")
            available_events = []

        for event in joined_events:
            event_id = event.get("eventId", None)
            if event_id:
                joined_ids[event_id] = True

        for event in available_events:
            start = event.get("startAt", None)
            end = event.get("endAt", None)
            event_id = event.get("id", None)
            code = event.get("code", None)
            reward = event.get("rewardPerKwhInOctoPoints", None)
            if reward is None:
                reward = default_octopoints
            if event_id:
                event_reward[event_id] = reward
                event_code[event_id] = code
            if start and end and event_id not in joined_ids:
                endDataTime = parse_date_time(end)
                if endDataTime > self.now_utc_exact:
                    return_available_events.append({"start": start, "end": end, "octopoints_per_kwh": reward, "code": code, "id": event_id})

        for event in joined_events:
            start = event.get("startAt", None)
            end = event.get("endAt", None)
            event_id = event.get("eventId", None)
            reward = event_reward.get(event_id, None)
            if reward is None:
                reward = default_octopoints  # Inject default when API doesn't provide reward
            if start and end:
                return_joined_events.append({"start": start, "end": end, "octopoints_per_kwh": reward, "rewarded_octopoints": event.get("rewardGivenInOctoPoints", None), "id": event_id, "code": event_code.get(event_id, None)})

        saving_attributes = {"friendly_name": "Octopus Intelligent Saving Sessions", "icon": "mdi:currency-usd", "joined_events": return_joined_events, "available_events": return_available_events}

        # Check if currently in an active saving session
        # Handle both old API keys (start/end) and new API keys (startAt/endAt)
        active_event = False
        for event in joined_events:
            start = event.get("startAt", event.get("start", None))
            end = event.get("endAt", event.get("end", None))
            if start and end:
                try:
                    start_dt = parse_date_time(start)
                    end_dt = parse_date_time(end)
                    if start_dt <= self.now_utc_exact and end_dt > self.now_utc_exact:
                        active_event = True
                        break
                except (ValueError, TypeError):
                    pass
        self.dashboard_item(self.get_entity_name("binary_sensor", "saving_session"), "on" if active_event else "off", attributes=saving_attributes, app="octopus")

        # Create joiner dropdown for available events
        possible_codes = []
        for event in available_events:
            code = event.get("code", None)
            if code:
                possible_codes.append(code)
        self.dashboard_item(self.get_entity_name("select", "saving_session_join"), "", attributes={"options": possible_codes, "friendly_name": "Join Octopus Saving Session Event", "icon": "mdi:currency-usd"}, app="octopus")

        # Publish free electricity events from flexibility API
        free_electric_events = []
        for event in self.free_electricity_events:
            start = event.get("startAt")
            end = event.get("endAt")
            code = event.get("code")
            if start and end:
                free_electric_events.append({"start": start, "end": end, "code": code, "rate": 0})
        free_attributes = {"friendly_name": "Octopus Free Electricity Sessions", "icon": "mdi:flash", "events": free_electric_events}
        active_free_event = False
        for event in free_electric_events:
            start = event.get("start")
            end = event.get("end")
            if start and end:
                try:
                    start_dt = parse_date_time(start)
                    end_dt = parse_date_time(end)
                    if start_dt <= self.now_utc_exact and end_dt > self.now_utc_exact:
                        active_free_event = True
                        break
                except (ValueError, TypeError):
                    pass
        self.dashboard_item(self.get_entity_name("sensor", "free_electricity"), "on" if active_free_event else "off", attributes=free_attributes, app="octopus")

        return return_available_events, return_joined_events

    def automatic_config(self, tariffs):
        """
        Automatic configuration of entities
        """
        self.log("OctopusAPI: Automatic configuration of entities")
        self.set_arg("octopus_saving_session", self.get_entity_name("binary_sensor", "saving_session"))
        self.set_arg("octopus_saving_session_join", self.get_entity_name("select", "saving_session_join"))
        self.set_arg("octopus_free_electricity", self.get_entity_name("sensor", "free_electricity"))
        for tariff in tariffs:
            self.set_arg("metric_octopus_{}".format(tariff), self.get_entity_name("sensor", tariff + "_rates"))
            if tariff == "import":
                self.set_arg("metric_standing_charge", self.get_entity_name("sensor", tariff + "_standing"))
        devices = self.get_intelligent_devices()
        if devices:
            slot_list = []
            ready_list = []
            limit_list = []
            for device_id in devices:
                index_suffix = self.device_id_to_index_suffix(device_id)
                slot_list.append(self.get_entity_name("binary_sensor", "intelligent_dispatch", index=index_suffix))
                ready_list.append(self.get_entity_name("select", "intelligent_target_time", index=index_suffix))
                limit_list.append(self.get_entity_name("number", "intelligent_target_soc", index=index_suffix))
            self.set_arg("octopus_intelligent_slot", slot_list)
            self.set_arg("octopus_ready_time", ready_list)
            self.set_arg("octopus_charge_limit", limit_list)
            # Increase number of cars if we have more devices than the current limit to ensure all devices can be configured
            num_cars = self.get_arg("num_cars", 0)
            if num_cars < len(devices):
                self.set_arg("num_cars", len(devices))

    async def async_get_saving_sessions(self, account_id):
        """
        Get the saving sessions
        """
        response_data = await self.async_graphql_query(octoplus_saving_session_query.format(account_id=self.account_id), "get-saving-sessions", ignore_errors=True, use_backend=True)
        if response_data is None:
            return self.saving_sessions
        else:
            self.log("OctopusAPI: Fetched saving sessions data from GraphQL API: {}".format(response_data))
            savingSessions = response_data.get("savingSessions", {})
            if savingSessions is None:
                savingSessions = {}
            if "account" in savingSessions:
                if savingSessions["account"] is None:
                    savingSessions["account"] = {}
            return savingSessions

    async def async_get_flexibility_events(self, account_id):
        """
        Get flexibility campaign events (saving sessions + free electricity)
        using the new customerFlexibilityCampaignEvents API.
        Falls back to legacy savingSessions query if MPAN is not available.
        """
        if not self.mpan:
            self.log("OctopusAPI: No MPAN available, falling back to legacy saving sessions query")
            return await self.async_get_saving_sessions(account_id)

        # Query saving sessions
        saving_events = []
        response_data = await self.async_graphql_query(flexibility_campaign_query.format(account_id=account_id, mpan=self.mpan, campaign_slug="octoplus-saving-sessions"), "get-flexibility-saving-sessions", ignore_errors=True)
        if response_data is not None:
            campaign_data = response_data.get("customerFlexibilityCampaignEvents", {})
            if campaign_data:
                edges = campaign_data.get("edges", [])
                for edge in edges:
                    node = edge.get("node", {})
                    if node:
                        saving_events.append(
                            {
                                "code": node.get("code"),
                                "startAt": node.get("startAt"),
                                "endAt": node.get("endAt"),
                            }
                        )
                self.log("OctopusAPI: Found {} saving session events via flexibility API".format(len(saving_events)))

        # Query free electricity sessions
        free_events = []
        response_data = await self.async_graphql_query(flexibility_campaign_query.format(account_id=account_id, mpan=self.mpan, campaign_slug="free_electricity"), "get-flexibility-free-electricity", ignore_errors=True)
        if response_data is not None:
            campaign_data = response_data.get("customerFlexibilityCampaignEvents", {})
            if campaign_data:
                edges = campaign_data.get("edges", [])
                for edge in edges:
                    node = edge.get("node", {})
                    if node:
                        free_events.append(
                            {
                                "code": node.get("code"),
                                "startAt": node.get("startAt"),
                                "endAt": node.get("endAt"),
                            }
                        )
                self.log("OctopusAPI: Found {} free electricity events via flexibility API".format(len(free_events)))

        # Store free electricity events for use by fetch_octopus_sessions
        self.free_electricity_events = free_events

        # If no saving session events from new API, fall back to legacy query
        # The new flexibility API may not have events populated yet for all accounts
        if not saving_events:
            self.log("OctopusAPI: No saving session events from flexibility API, falling back to legacy query")
            legacy_result = await self.async_get_saving_sessions(account_id)
            return legacy_result

        # Map to existing internal format
        # New API doesn't distinguish available vs joined — treat all as joined
        result = {"events": [], "account": {"hasJoinedCampaign": len(saving_events) > 0, "joinedEvents": []}}  # No "available" events (no separate list in new API)
        for event in saving_events:
            result["account"]["joinedEvents"].append(
                {
                    "eventId": event.get("code"),
                    "startAt": event.get("startAt"),
                    "endAt": event.get("endAt"),
                    "rewardGivenInOctoPoints": None,
                }
            )

        return result

    async def _async_get_product_rate_link_types(self, product_code, tariff_code):
        """
        Fetch the Octopus product info and return the set of rate link rel types for the given tariff_code.

        The product endpoint returns multiple regional entries (e.g. _A, _B, _C) under each tariff type
        section. We search all regions and payment types to find the entry whose code matches tariff_code,
        then extract the rel values from its links list.

        Returns a set of strings such as {"standard_unit_rates"} or {"day_unit_rates", "night_unit_rates"},
        or None if the product info could not be fetched or tariff_code was not found (caller should fall back).
        """
        if product_code not in self._product_info_cache:
            url = f"https://api.octopus.energy/v1/products/{product_code}/"
            product_info = await self.fetch_url_cached(url, json_only=True)
            if not product_info or not isinstance(product_info, dict):
                self.log("Warn: OctopusAPI: Could not fetch product info for {}".format(product_code))
                return None
            self._product_info_cache[product_code] = product_info

        product_info = self._product_info_cache[product_code]
        tariff_sections = ["single_register_electricity_tariffs", "dual_register_electricity_tariffs", "four_rate_ev_electricity_tariffs"]
        payment_types = ["direct_debit_monthly", "varying"]
        for section_key in tariff_sections:
            section = product_info.get(section_key, {})
            if not section:
                continue
            for region_key, region_data in section.items():
                if not isinstance(region_data, dict):
                    continue
                for payment_type in payment_types:
                    entry = region_data.get(payment_type, {})
                    if not entry:
                        continue
                    if entry.get("code") == tariff_code:
                        links = entry.get("links", [])
                        return {link["rel"] for link in links if "rel" in link}
        self.log("Warn: OctopusAPI: Tariff code {} not found in product info for {}".format(tariff_code, product_code))
        return None

    def _get_rate_for_time(self, rates_list, timestamp, now=None):
        """
        Find the rate from rates_list that is valid at the given timestamp.
        A rate is considered valid if valid_from <= timestamp < valid_to.
        Missing/null valid_from is treated as the epoch (always started).
        Missing/null valid_to is treated as far future (never expires).
        Among all matching entries the one with the most recent valid_from wins.
        If now is provided, entries whose valid_from is after now are ignored
        (they represent future rate announcements not yet in effect).
        Returns value_inc_vat or None if no entry covers the timestamp.
        """
        _EPOCH = timestamp - timedelta(days=365 * 50)  # 50 years ago, effectively the epoch for our purposes
        _FAR_FUTURE = timestamp + timedelta(days=365 * 50)  # 50 years in the future, effectively never expires for our purposes
        best_rate = None
        best_valid_from = None
        for rate in rates_list:
            valid_from_str = rate.get("valid_from") or ""
            valid_to_str = rate.get("valid_to") or ""
            try:
                valid_from_stamp = datetime.strptime(valid_from_str, DATE_TIME_STR_FORMAT) if valid_from_str else _EPOCH
            except ValueError:
                valid_from_stamp = _EPOCH
            try:
                valid_to_stamp = datetime.strptime(valid_to_str, DATE_TIME_STR_FORMAT) if valid_to_str else _FAR_FUTURE
            except ValueError:
                valid_to_stamp = _FAR_FUTURE
            if now is not None and valid_from_stamp > now:
                continue  # Ignore rates that haven't become active yet as of now
            if valid_from_stamp <= timestamp < valid_to_stamp:
                if best_valid_from is None or valid_from_stamp > best_valid_from:
                    best_valid_from = valid_from_stamp
                    best_rate = rate.get("value_inc_vat", None)
        return best_rate

    async def async_get_day_night_rates(self, url, product_code="", tariff_code=""):
        """
        Get day and night rates from Octopus.

        Selects the correct night/day window based on the tariff type:
        - IOG TOU (product_code contains INTELLI+IOG+TOU): 23:30 to 05:30 (crosses midnight)
        - Economy 7 (tariff_code starts with E-2R-) or unknown (400-error fallback): 00:30 to 07:30
        - GO and other day/night tariffs: 00:30 to 05:30
        """
        mdata = []
        self.log("Info: OctopusAPI: tariff has day and night rates, fetching both")
        url_day = url.replace("standard-unit-rates", "day-unit-rates")
        url_night = url.replace("standard-unit-rates", "night-unit-rates")
        result_day = await self.fetch_url_cached(url_day)
        result_night = await self.fetch_url_cached(url_night)
        self.log("Info: OctopusAPI: Day rate entries: {} night rate entries: {}".format(len(result_day) if result_day else 0, len(result_night) if result_night else 0))
        if result_day and result_night:
            # Select night window based on tariff type
            if ("INTELLI" in tariff_code) or ("IOG-" in tariff_code):
                window = OCTOPUS_NIGHT_RATE_WINDOWS["iog"]
            elif tariff_code and "GO-" in tariff_code:
                window = OCTOPUS_NIGHT_RATE_WINDOWS["go"]
            elif tariff_code and tariff_code.startswith("E-2R-"):
                window = OCTOPUS_NIGHT_RATE_WINDOWS["eco7"]
            else:
                self.log("Warn: OctopusAPI: Unknown tariff code {}, defaulting to GO night rate window".format(tariff_code))
                window = OCTOPUS_NIGHT_RATE_WINDOWS["go"]

            self.log("Info: OctopusAPI: Using night rate window {} based on tariff code {}".format(window, tariff_code))
            night_start_hour, night_start_minute = window["start"]
            night_end_hour, night_end_minute = window["end"]
            cross_midnight = window["cross_midnight"]

            # Build synthetic 8-day schedule starting 2 days back
            # Look up the correct rate for each individual day to handle mid-window rate changes
            night_start_time = self.now_utc_exact.replace(hour=night_start_hour, minute=night_start_minute, second=0, microsecond=0) - timedelta(days=2)
            if cross_midnight:
                night_end_time = (night_start_time + timedelta(days=1)).replace(hour=night_end_hour, minute=night_end_minute)
            else:
                night_end_time = night_start_time.replace(hour=night_end_hour, minute=night_end_minute)
            day_start_time = night_end_time
            day_end_time = night_start_time + timedelta(days=1)
            current_time = self.now_utc_exact
            for day in range(8):
                night_rate = self._get_rate_for_time(result_night, night_start_time, now=current_time)
                day_rate = self._get_rate_for_time(result_day, day_start_time, now=current_time)
                if night_rate is not None:
                    mdata.append({"valid_from": night_start_time.strftime(DATE_TIME_STR_FORMAT), "valid_to": night_end_time.strftime(DATE_TIME_STR_FORMAT), "value_inc_vat": night_rate})
                if day_rate is not None:
                    mdata.append({"valid_from": day_start_time.strftime(DATE_TIME_STR_FORMAT), "valid_to": day_end_time.strftime(DATE_TIME_STR_FORMAT), "value_inc_vat": day_rate})
                night_start_time += timedelta(days=1)
                night_end_time += timedelta(days=1)
                day_start_time += timedelta(days=1)
                day_end_time += timedelta(days=1)
        return mdata

    async def async_download_octopus_url(self, url, json_only=False):
        """
        Download octopus rates directly from a URL.
        If json_only=True, return the raw JSON dict without results unwrapping or pagination
        (used for product-info endpoints that return a top-level object rather than a paginated list).
        """
        mdata = []

        pages = 0
        while url and pages < 3:
            self.requests_total += 1
            timeout = aiohttp.ClientTimeout(total=20)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers={"accept": "application/json", "user-agent": "predbat/1.0"}) as response:
                        if response.status not in [200, 201]:
                            self.failures_total += 1
                            self.log("Warn: OctopusAPI: Error downloading Octopus data from URL {}, code {}".format(url, response.status))
                            record_api_call("octopus_url", False, "server_error")
                            return {}
                        try:
                            data = await response.json()
                            self.last_success_timestamp = datetime.now(timezone.utc)
                            record_api_call("octopus_url")
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            self.failures_total += 1
                            self.log("Warn: OctopusAPI: Error downloading Octopus data from URL {} (JSONDecodeError)".format(url))
                            record_api_call("octopus_url", False, "decode_error")
                            return {}

                        if json_only:
                            return data

                        if "results" in data:
                            mdata += data["results"]
                        else:
                            detail = data.get("detail", "")
                            self.failures_total += 1
                            self.log("Warn: OctopusAPI: Error downloading Octopus data from URL {} (No Results) - {}".format(url, detail))
                            return {}
                        url = data.get("next", None)
                        pages += 1
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.failures_total += 1
                self.log("Warn: OctopusAPI: Error downloading Octopus data from URL {} - {}".format(url, e))
                return {}

        return mdata

    async def fetch_url_cached(self, url, json_only=False):
        """
        Fetch a URL from the shared cache or reload it via the storage component.

        Uses storage.fetch_cached (stale-while-revalidate). With the default
        single-instance StorageBase the refresh lock is a no-op; a StorageBase
        subclass that implements a real distributed lock (e.g. a multi-instance
        backend) ensures only one instance refreshes while others serve stale.
        If json_only=True, the raw JSON dict is returned without results
        unwrapping or pagination.
        """
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        if not self.storage:
            data = await self.async_download_octopus_url(url, json_only=json_only)
            if not data:
                self.log("Warn: Unable to download Octopus data from URL {}".format(url))
            return data or None

        async def _download():
            result = await self.async_download_octopus_url(url, json_only=json_only)
            return result if result else None

        data = await self.storage.fetch_cached("octopus", url_hash, _download, fresh_minutes=30, stale_minutes=35, format="yaml")
        if not data:
            self.log("Warn: Unable to download Octopus data from URL {}".format(url))
        return data

    async def fetch_tariffs(self, tariffs):
        """
        Fetch the tariff data
        """
        for tariff in sorted(tariffs, key=lambda t: 0 if t == "import" else 1):
            product_code = tariffs[tariff]["productCode"]
            tariff_code = tariffs[tariff]["tariffCode"]

            # Fetch from URL or URL cache
            if tariff == "gas":
                tariff_type = "gas"
            else:
                tariff_type = "electricity"
            standard_url = f"https://api.octopus.energy/v1/products/{product_code}/{tariff_type}-tariffs/{tariff_code}/standard-unit-rates/"
            if tariff_type == "electricity":
                # Always check product links first to determine the correct rate endpoint type,
                # avoiding the 400 round-trip for tariffs that only expose day/night rate endpoints.
                link_types = await self._async_get_product_rate_link_types(product_code, tariff_code)
                if link_types is not None and "standard_unit_rates" not in link_types and "day_unit_rates" in link_types:
                    # Day/night tariff (e.g. IOG-TOU, GO): fetch directly without hitting standard-unit-rates
                    self.log("Info: OctopusAPI: Product {} tariff {} has day/night rate endpoints (no standard-unit-rates), fetching directly".format(product_code, tariff_code))
                    tariffs[tariff]["data"] = await self.async_get_day_night_rates(standard_url, product_code=product_code, tariff_code=tariff_code)
                else:
                    # Standard rates, or product info unavailable — fall back to existing path
                    # (which handles the 400 "This tariff has day and night rates" error for Economy 7)
                    tariffs[tariff]["data"] = await self.fetch_url_cached(standard_url)
            else:
                tariffs[tariff]["data"] = await self.fetch_url_cached(standard_url)
            if not tariffs[tariff]["data"] and tariff == "export" and "INTELLI-FLUX-EXPORT" in product_code:
                # INTELLI-FLUX-EXPORT rates are the same as INTELLI-FLUX-IMPORT rates, but INTELLI-FLUX-EXPORT
                # is not on the public REST API — fetch the equivalent INTELLI-FLUX-IMPORT tariff instead
                flux_import_product = product_code.replace("FLUX-EXPORT", "FLUX-IMPORT")
                flux_import_tariff_code = tariff_code.replace("FLUX-EXPORT", "FLUX-IMPORT")
                flux_import_url = f"https://api.octopus.energy/v1/products/{flux_import_product}/electricity-tariffs/{flux_import_tariff_code}/standard-unit-rates/"
                import_data = await self.fetch_url_cached(flux_import_url)
                if import_data:
                    tariffs[tariff]["data"] = import_data
                    self.log("OctopusAPI: Using FLUX-IMPORT ({}) rates as fallback for {} export tariff".format(flux_import_product, product_code))
                else:
                    import_data = tariffs.get("import", {}).get("data", None)
                    if import_data:
                        tariffs[tariff]["data"] = import_data
                        self.log("OctopusAPI: Using current import rates as fallback for {} export tariff (FLUX-IMPORT also unavailable)".format(product_code))
                    else:
                        self.log("Warn: OctopusAPI: No import data available for INTELLI-FLUX-EXPORT fallback, export rates will be zero")
            tariffs[tariff]["standing"] = await self.fetch_url_cached(f"https://api.octopus.energy/v1/products/{product_code}/{tariff_type}-tariffs/{tariff_code}/standing-charges/")

            rates = self.get_octopus_rates_direct(tariff)
            standing = self.get_octopus_rates_direct(tariff, standingCharge=True)

            rates_stamp = []
            for minute in range(-24 * 60, 60 * 24 * 2, self.plan_interval_minutes):
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

            self.dashboard_item(
                self.get_entity_name("sensor", tariff + "_rates"),
                rate_now,
                attributes={"friendly_name": "Octopus Tariff Rates " + tariff, "icon": "mdi:currency-gbp", "standing_charge": standing_now, "rates": rates_stamp, "product_code": product_code, "tariff_code": tariff_code},
                app="octopus",
            )
            self.dashboard_item(
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
            self.log("OctopusAPI: tariff {} not available, using zero".format(tariff_type))
            return {n: 0 for n in range(0, 60 * 24)}

    async def async_read_response_retry(self, response, url, ignore_errors=False):
        """
        Read response with retry on failure
        """
        max_retries = OCTOPUS_MAX_RETRIES
        for attempt in range(max_retries):
            # Check for shutdown signal
            if self.api_stop:
                self.log("OctopusAPI: Aborting retry loop due to shutdown")
                return None

            data_as_json = await self.async_read_response(response, url, ignore_errors=ignore_errors)
            if data_as_json is not None:
                return data_as_json
            else:
                if attempt < max_retries - 1:
                    self.log(f"OctopusAPI: Retrying read response for {url} (attempt {attempt + 2} of {max_retries})")
                    await asyncio.sleep(2**attempt)  # Exponential backoff
        self.failures_total += 1
        return None

    async def async_read_response(self, response, url, ignore_errors=False):
        """Reads the response, logging any json errors"""

        request_context = response.request_info.headers[integration_context_header] if integration_context_header in response.request_info.headers else "Unknown"

        text = await response.text()

        if response.status >= 400:
            if response.status >= 500:
                msg = f"Warn: OctopusAPI: Response received - {url} ({request_context}) - DO NOT REPORT - Octopus Energy server error ({url}): {response.status}; {text}"
                self.log(msg)
                return None
            elif response.status in [401, 403]:
                msg = f"Warn: OctopusAPI: Response received - {url} ({request_context}) - Unauthenticated request: {response.status}; {text}"
                self.log(msg)
                return None
            elif response.status not in [404]:
                msg = f"Warn: OctopusAPI: Response received - {url} ({request_context}) - Unexpected response received: {response.status}; {text}"
                self.log(msg)
                return None

            self.log(f"Warn: OctopusAPI: Response received - {url} ({request_context}) - Unexpected response received: {response.status}; {text}")
            return None

        data_as_json = None
        try:
            data_as_json = json.loads(text)
        except Exception as e:
            self.log(f"Warn: OctopusAPI: Failed to extract response json: {e} - {url} - {text}")
            return None

        # Check for rate limit errors - these should return None immediately (no retry)
        if ("graphql" in url) and data_as_json and ("errors" in data_as_json):
            for error in data_as_json.get("errors", []):
                error_code = error.get("extensions", {}).get("errorCode")
                if error_code == "KT-CT-1199":
                    msg = f'Warn: OctopusAPI: Rate limit error in request ({url}): {data_as_json["errors"]}'
                    self.log(msg)
                    record_api_call("octopus", False, "rate_limit")
                    # Don't sleep if shutting down
                    if not self.api_stop:
                        await asyncio.sleep(5)  # Sleep briefly to avoid hammering
                    return None

        # Return the response as-is - let caller handle other errors (including auth errors that need retry)
        return data_as_json

    async def async_refresh_token(self):
        """
        Refresh the token using JWT expiry from the token itself
        """
        # Check if we have a valid token by decoding its expiry
        if self.graphql_token:
            expiry = self.decode_kraken_token_expiry(self.graphql_token)
            if expiry and expiry > datetime.now() + timedelta(minutes=5):
                return self.graphql_token

        client = await self.api.async_create_client_session()
        url = f"{self.api.base_url}/v1/graphql/"
        payload = {"query": api_token_query.format(api_key=self.api_key)}
        headers = {integration_context_header: "refresh-token"}

        try:
            async with client.post(url, headers=headers, json=payload) as token_response:
                token_response_body = await self.async_read_response_retry(token_response, url)
                if (
                    token_response_body is not None
                    and "data" in token_response_body
                    and "obtainKrakenToken" in token_response_body["data"]
                    and token_response_body["data"]["obtainKrakenToken"] is not None
                    and "token" in token_response_body["data"]["obtainKrakenToken"]
                ):
                    self.graphql_token = token_response_body["data"]["obtainKrakenToken"]["token"]
                    # Save token to cache immediately
                    await self.save_octopus_cache()
                    return self.graphql_token
                else:
                    self.log("Warn: OctopusAPI: Failed to retrieve auth token")
                    return None
        except TimeoutError:
            self.log(f"Warn: OctopusAPI: Failed to connect. Timeout of {self.api.timeout} exceeded.")
            return None

    async def async_graphql_query(self, query, request_context, returns_data=True, ignore_errors=False, _retry_count=0, use_backend=False):
        """
        Execute a graphql query with automatic token refresh on auth errors.
        If use_backend=True, uses api.backend.octopus.energy with no JWT prefix
        (required for saving sessions since Feb 2026 API migration).
        """
        token = await self.async_refresh_token()
        if token is None:
            self.failures_total += 1
            if returns_data:
                self.log(f"Warn: OctopusAPI: Failed to retrieve data from graphql query {request_context} - token refresh failed")
            return None
        try:
            self.requests_total += 1
            client = await self.api.async_create_client_session()
            base = self.api.backend_url if use_backend else self.api.base_url
            url = f"{base}/v1/graphql/"
            payload = {"query": query}
            auth_prefix = "" if use_backend else "JWT "
            headers = {"Authorization": f"{auth_prefix}{self.graphql_token}", integration_context_header: request_context}
            # Redact the Authorization header so the JWT token is never written to the log
            log_headers = {**headers, "Authorization": f"{auth_prefix}<redacted>"}
            self.log("OctopusAPI: Making GraphQL request to {} payload {} headers {}".format(url, payload, log_headers))
            async with client.post(url, json=payload, headers=headers) as response:
                # Check for HTTP-level 401/403 (transport-level auth failure) and retry once.
                # This handles cases where the JWT has been revoked server-side and the server
                # returns a bare 401/403 status rather than a GraphQL error body — which would
                # otherwise loop forever without ever refreshing the token.
                if response.status in [401, 403] and _retry_count == 0:
                    self.log(f"OctopusAPI: HTTP {response.status} for graphql query {request_context}, forcing token refresh and retry")
                    record_api_call("octopus", False, "auth_error")
                    self.graphql_token = None
                    retry_token = await self.async_refresh_token()
                    if retry_token is None:
                        self.failures_total += 1
                        self.log(f"Warn: OctopusAPI: Failed to refresh token for retry of graphql query {request_context}")
                        return None
                    return await self.async_graphql_query(query, request_context, returns_data=returns_data, ignore_errors=ignore_errors, _retry_count=1, use_backend=use_backend)

                # Process response (which reads the text)
                response_body = await self.async_read_response_retry(response, url, ignore_errors=ignore_errors)
                self.log("OctopusAPI: GraphQL response for {} (status {}): {}".format(request_context, response.status, response_body))

                # Check for auth errors and retry once
                if response_body and "errors" in response_body and _retry_count == 0:
                    for error in response_body.get("errors", []):
                        error_code = error.get("extensions", {}).get("errorCode")
                        if error_code in ("KT-CT-1139", "KT-CT-1111", "KT-CT-1143"):
                            self.log(f"OctopusAPI: Kraken token invalid (error {error_code}), forcing refresh and retry")
                            record_api_call("octopus", False, "auth_error")
                            self.graphql_token = None
                            retry_token = await self.async_refresh_token()
                            if retry_token is None:
                                self.failures_total += 1
                                self.log(f"Warn: OctopusAPI: Failed to refresh token for retry of graphql query {request_context}")
                                return None
                            # Token is now refreshed and cached in self.graphql_token
                            # Retry the query with new token (_retry_count=1 prevents infinite loop)
                            return await self.async_graphql_query(query, request_context, returns_data=returns_data, ignore_errors=ignore_errors, _retry_count=1, use_backend=use_backend)

                # Check for other errors (non-auth)
                if response_body and "errors" in response_body and not ignore_errors:
                    msg = f'Warn: OctopusAPI: Errors in request ({url}): {response_body["errors"]}'
                    self.log(msg)
                    self.failures_total += 1
                    if returns_data:
                        self.log(f"Warn: OctopusAPI: Failed to retrieve data from graphql query {request_context}")
                    return None

                if response_body and ("data" in response_body):
                    self.update_success_timestamp()
                    record_api_call("octopus")
                    return response_body["data"]
                else:
                    if not ignore_errors:
                        self.failures_total += 1
                        if returns_data:
                            self.log(f"Warn: OctopusAPI: Failed to retrieve data from graphql query {request_context}")
                    return None
        except TimeoutError:
            self.failures_total += 1
            self.log(f"Warn: OctopusAPI: Failed to connect, timeout exceeded.")
            record_api_call("octopus", False, "connection_error")

        return None

    async def async_get_intelligent_devices(self, account_id, device_id):
        """
        Get the intelligent dispatches/device
        """
        results = {}
        if device_id:
            self.log("OctopusAPI: Fetching intelligent dispatches for device {}".format(device_id))
            device_result = await self.async_graphql_query(intelligent_device_query.format(account_id=account_id), "get-intelligent-devices", ignore_errors=True)
            intelligent_device = {}
            """
            'devices':
            [
            {'id': '00000000-0002-4000-8020-0000000ea7d3', 'provider': 'JEDLIX_V2', 'deviceType': 'ELECTRIC_VEHICLES', 'status': {'current': 'LIVE'}, '__typename': 'SmartFlexVehicle', 'make': 'Mini', 'model': 'Cooper SE'},
            {'id': '00000000-0002-4000-8020-0000000df503', 'provider': 'JEDLIX_V2', 'deviceType': 'ELECTRIC_VEHICLES', 'status': {'current': 'LIVE'}, '__typename': 'SmartFlexVehicle', 'make': 'BMW', 'model': 'iX3'},
            {'id': '00000000-000a-4000-8020-07ffff4ce519', 'provider': 'OCTOPUS_ENERGY', 'deviceType': 'ELECTRICITY_METERS', 'status': {'current': 'LIVE'}, '__typename': 'SmartFlexDevice'}]}
            ]
            """

            if device_result:
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
                        planned = []
                        # Get previously completed dispatches, as we want to keep the old ones and merge
                        completed = self.get_intelligent_completed_dispatches(IntelligentdeviceID)

                        dispatch_result = await self.async_graphql_query(intelligent_dispatches_query.format(account_id=account_id, device_id=IntelligentdeviceID), "get-intelligent-dispatches", ignore_errors=True)
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
                                continue

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
                            plannedDispatches = dispatch_result.get("flexPlannedDispatches") or []
                            completedDispatches = dispatch_result.get("completedDispatches") or []
                            for plannedDispatch in plannedDispatches:
                                start = plannedDispatch.get("start", None)
                                end = plannedDispatch.get("end", None)
                                if not (start and end):
                                    self.log("Warn: OctopusAPI: Planned dispatch missing start or end time, skipping: {}".format(plannedDispatch))
                                    continue
                                delta = plannedDispatch.get("energyAddedKwh", plannedDispatch.get("delta", None))
                                dispatch_type = plannedDispatch.get("type", "")
                                meta = plannedDispatch.get("meta", {})
                                try:
                                    delta = dp4(float(delta))
                                except (ValueError, TypeError):
                                    delta = None

                                dispatch = {"start": start, "end": end, "charge_in_kwh": delta, "source": meta.get("source", dispatch_type), "location": meta.get("location", None)}
                                # Keep planned (flexPlannedDispatches) entries in the planned list only - do NOT promote
                                # in-progress slots into completed_dispatches (see issue #4114). flexPlannedDispatches is
                                # Octopus's optimiser schedule and includes plug-independent SMART grid-flex events that
                                # Octopus routinely withdraws on its next re-plan. Promoting them immortalised provisional
                                # slots as permanent cheap "completed" slots that never had a matching real dispatch.
                                # Genuine charging is still cached below via the metered completedDispatches feed
                                # (location=AT_HOME).
                                #
                                # If the slot is already in progress, trim the elapsed portion before appending: advance
                                # its start to now and scale charge_in_kwh to the remaining time. decode_octopus_slot does
                                # not trim a started slot when charge_in_kwh > 0, so without this the already-delivered
                                # energy would be double counted, inflating predicted car SoC/cost for the active window.
                                start_date_time = parse_date_time(start)
                                end_date_time = parse_date_time(end)
                                if start_date_time and end_date_time and start_date_time < self.now_utc_exact < end_date_time:
                                    total_minutes = (end_date_time - start_date_time).total_seconds() / 60
                                    remaining_minutes = (end_date_time - self.now_utc_exact).total_seconds() / 60
                                    if total_minutes > 0:
                                        if delta is not None:
                                            delta = dp4(delta * remaining_minutes / total_minutes)
                                            dispatch["charge_in_kwh"] = delta
                                        dispatch["start"] = self.now_utc_exact.strftime(DATE_TIME_STR_FORMAT)
                                planned.append(dispatch)
                            for completedDispatch in completedDispatches:
                                start = completedDispatch.get("start", None)
                                end = completedDispatch.get("end", None)
                                if not (start and end):
                                    self.log("Warn: OctopusAPI: Completed dispatch missing start or end time, skipping: {}".format(completedDispatch))
                                    continue
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
                                    if cached.get("start") == start:
                                        cached.update(dispatch)
                                        found = True
                                        break
                                if not found:
                                    completed.append(dispatch)

                        # Sort by start time
                        planned = sorted([x for x in planned if x.get("start")], key=lambda x: parse_date_time(x.get("start")))
                        completed = sorted([x for x in completed if x.get("start")], key=lambda x: parse_date_time(x.get("start")))

                        # Prune completed dispatches for results older than 5 days
                        completed = [x for x in completed if x.get("start") and parse_date_time(x.get("start")) > self.now_utc_exact - timedelta(days=5)]
                        # Store results
                        result = {**intelligent_device, **device_setting_result, "planned_dispatches": planned, "completed_dispatches": completed}
                        results[IntelligentdeviceID] = result
        return results

    async def async_intelligent_update_sensor(self, account_id):
        """
        Update the intelligent device sensor
        """
        intelligent_devices = self.get_intelligent_devices()
        if not intelligent_devices:
            return

        for device_id in intelligent_devices:
            device = intelligent_devices[device_id]
            device_index = self.device_id_to_index_suffix(device_id)
            planned = device.get("planned_dispatches", [])
            completed = device.get("completed_dispatches", [])

            active_event = False
            for dispatch in planned + completed:
                start = dispatch.get("start", None)
                end = dispatch.get("end", None)
                if start and end:
                    start = parse_date_time(start)
                    end = parse_date_time(end)
                    if start <= self.now_utc_exact and end > self.now_utc_exact:
                        active_event = True
            dispatch_attributes = {"friendly_name": "Octopus Intelligent Dispatches", "icon": "mdi:flash", **device}
            self.dashboard_item(self.get_entity_name("binary_sensor", "intelligent_dispatch", index=device_index), "on" if active_event else "off", attributes=dispatch_attributes, app="octopus")

            weekday_target_time = device.get("weekday_target_time", None)
            weekday_target_soc = device.get("weekday_target_soc", None)
            weekend_target_time = device.get("weekend_target_time", None)
            weekend_target_soc = device.get("weekend_target_soc", None)
            # Check if we are on a weekend?
            if self.now_utc_exact.weekday() >= 5:
                target_time = weekend_target_time
                target_soc = weekend_target_soc
            else:
                target_time = weekday_target_time
                target_soc = weekday_target_soc
            if target_time:
                target_time = target_time[:5]  # Only HH:MM
            self.dashboard_item(
                self.get_entity_name("select", "intelligent_target_time", index=device_index), target_time, attributes={"friendly_name": "Octopus Intelligent Target Time", "icon": "mdi:clock-outline", "options": OPTIONS_TIME}, app="octopus"
            )
            self.dashboard_item(self.get_entity_name("number", "intelligent_target_soc", index=device_index), target_soc, attributes={"friendly_name": "Octopus Intelligent Target SOC", "icon": "mdi:battery-percent", "min": 0, "max": 100}, app="octopus")

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
    """High-level Octopus rate loading mixin used by the Fetch class.

    Provides methods for downloading rates from URLs, loading intelligent
    dispatch slots, applying free/saving sessions, and converting Octopus
    rate data to per-minute dictionaries.
    """

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
        Download octopus free session data directly from a URL, no caching.
        """
        try:
            r = requests.get(url)
        except requests.exceptions.ConnectionError:
            self.log("Warn: Octopus: Unable to download Octopus data from URL {} (ConnectionError)".format(url))
            self.record_status("Warn: Unable to download Octopus free session data", debug=url, had_errors=True)
            return None

        if r.status_code not in [200, 201]:
            self.log("Warn: Octopus: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
            self.record_status("Warn: Error downloading Octopus free session data", debug=url, had_errors=True)
            return None

        return r.text

    def _load_octopus_url_cache_from_storage(self):
        """Pre-warm the entire octopus_url_cache (free sessions and rates) from storage on first call after restart."""
        components = getattr(self, "components", None)
        storage = components.get_component("storage") if components else None
        if self.octopus_url_cache_loaded or not storage:
            return
        try:
            self.octopus_url_cache_loaded = True
            data = run_async(storage.load("octopus_free", "url_cache"))
            if data:
                self.octopus_url_cache = data
                self.log("Octopus: Loaded URL cache from storage ({} entries)".format(len(data)))
        except Exception as e:
            self.log("Warn: Octopus: Failed to load URL cache from storage: {}".format(e))

    def _save_octopus_url_cache_to_storage(self):
        """Persist the entire octopus_url_cache (free sessions and rates) to storage so it survives restarts."""
        components = getattr(self, "components", None)
        storage = components.get_component("storage") if components else None
        if not storage:
            return

        # Prune entries older than 2 days so stale keys (e.g. after a tariff URL change) don't accumulate forever
        now = datetime.now()
        stale = [url for url, entry in self.octopus_url_cache.items() if not isinstance(entry, dict) or not entry.get("stamp") or (now - entry["stamp"]) > timedelta(days=2)]
        for url in stale:
            del self.octopus_url_cache[url]
        if stale:
            self.log("Octopus: Pruned {} stale URL cache entries (older than 2 days)".format(len(stale)))

        try:
            run_async(storage.save("octopus_free", "url_cache", self.octopus_url_cache, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(hours=8)))
        except Exception as e:
            self.log("Warn: Octopus: Failed to save URL cache to storage: {}".format(e))

    def download_octopus_free(self, url):
        """
        Download octopus free session data.
        If response is JSON, parse as Go API response. Otherwise, use legacy HTML parsing.
        Caches the parsed sessions list (not the raw response) to avoid retaining large text bodies.
        On first call after a restart, pre-warms the in-memory cache from storage before checking expiry.
        """
        # Pre-warm the entire cache from storage once per process lifetime
        self._load_octopus_url_cache_from_storage()

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            cached_midnight = self.octopus_url_cache[url].get("midnight_utc")
            age = now - stamp

            # Cache is valid if: age < 30 minutes AND midnight_utc hasn't changed (to avoid stale data after midnight)
            if age.total_seconds() < (30 * 60) and cached_midnight == self.midnight_utc:
                self.log("Octopus: Return cached octopus data for {} age {} minutes".format(url, dp1(age.total_seconds() / 60)))
                return self.octopus_url_cache[url]["data"]
            elif cached_midnight != self.midnight_utc:
                self.log("Octopus: Cached octopus data for {} is stale (midnight crossed), re-downloading".format(url))

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

        except json.JSONDecodeError:
            # Not JSON, use legacy HTML parsing
            free_sessions = self.download_octopus_free_legacy(pdata)

        self.octopus_url_cache[url] = {"stamp": now, "midnight_utc": self.midnight_utc, "data": free_sessions}
        self._save_octopus_url_cache_to_storage()
        return free_sessions

    def download_octopus_free_legacy(self, pdata):
        """
        Legacy method: Parse HTML directly (fallback only).
        Kept for backward compatibility when Go API is unavailable.
        """
        free_sessions = []
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
                        self.log(f"Octopus: Legacy parser found session: {session['start']} to {session['end']}")

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
            self.log(f"Octopus: Error in create_free_session_simple: {e}")
            return None

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        self.log("Octopus: Download Octopus rates from {}".format(url))

        # Pre-warm the entire cache from storage once per process lifetime (shared with free sessions).
        # Done before any in-memory population so the one-shot storage load cannot clobber freshly cached entries.
        self._load_octopus_url_cache_from_storage()

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            cached_midnight = self.octopus_url_cache[url].get("midnight_utc")
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            # Cache is valid if: age < 30 minutes AND midnight_utc hasn't changed (to avoid stale rates after midnight)
            if age.total_seconds() < (30 * 60) and cached_midnight == self.midnight_utc:
                self.log("Octopus: Return cached octopus data for {} age {} minutes".format(url, dp1(age.total_seconds() / 60)))
                return pdata
            elif cached_midnight != self.midnight_utc:
                self.log("Octopus: Cached octopus data for {} is stale (midnight crossed), re-downloading".format(url))

        # Retry up to 3 minutes
        for retry in range(3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("Warn: Octopus: Unable to download Octopus data from URL {} (data empty)".format(url))
            self.record_status("Warn: Octopus: Unable to download Octopus data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.octopus_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        # Cache New Octopus data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["midnight_utc"] = self.midnight_utc
        self.octopus_url_cache[url]["data"] = pdata
        self._save_octopus_url_cache_to_storage()
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
                r = requests.get(url, headers={"accept": "application/json", "user-agent": "predbat/1.0"}, timeout=20)
            except requests.exceptions.ConnectionError:
                self.log("Warn: Octopus: Unable to download Octopus data from URL {} (ConnectionError)".format(url))
                self.record_status("Warn: Unable to download Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if r.status_code not in [200, 201]:
                self.log("Warn: Octopus: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
                self.record_status("Warn: Octopus: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                self.failures_total += 1
                self.log("Warn: Octopus: Error downloading Octopus data from URL {} (JSONDecodeError)".format(url))
                self.record_status("Warn: Octopus: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if "results" in data:
                mdata += data["results"]
            else:
                self.log("Warn: Octopus: Error downloading Octopus data from URL {} (No Results)".format(url))
                self.record_status("Warn: Octopus: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            url = data.get("next", None)
            pages += 1

        pdata, _ = minute_data(mdata, 3, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return pdata

    def add_now_to_octopus_slot(self, car_n, octopus_slots, now_utc):
        """
        For intelligent charging, add in if the car is charging now as a low rate slot (workaround for Ohme)
        """
        if car_n < len(self.car_charging_now) and self.car_charging_now[car_n]:
            minutes_start_slot = int(self.minutes_now / 30) * 30
            minutes_end_slot = minutes_start_slot + 30
            slot_start_date = self.midnight_utc + timedelta(minutes=minutes_start_slot)
            slot_end_date = self.midnight_utc + timedelta(minutes=minutes_end_slot)
            slot = {}
            slot["start"] = slot_start_date.strftime(TIME_FORMAT)
            slot["end"] = slot_end_date.strftime(TIME_FORMAT)
            slot["source"] = "car_charging_now"
            slot["kwh"] = self.car_charging_rate[car_n] * 30 / 60  # Scale to 30 minute slot
            octopus_slots.append(slot)
            self.log("Octopus: Car is charging now - added new IO slot {}".format(slot))
        return octopus_slots

    def load_free_slot(self, octopus_free_slots, export=False, rate_replicate=None):
        """
        Load octopus free session slot
        """
        if rate_replicate is None:
            rate_replicate = {}
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
                    self.log("Warn: Octopus: Unable to decode Octopus free session start/end time {}".format(octopus_free_slot))

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

    def load_saving_slot(self, octopus_saving_slots, export=False, rate_replicate=None):
        """
        Load octopus saving session slot
        """
        if rate_replicate is None:
            rate_replicate = {}
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
                    self.log("Warn: Octopus: Unable to decode Octopus saving session start/end time {}".format(octopus_saving_slot))
            if state and (not start or not end):
                self.log("Octopus: Currently in saving session, assume current 30 minute slot")
                start_minutes = int(self.minutes_now / 30) * 30
                end_minutes = start_minutes + 30
            elif start and end:
                start_minutes = minutes_to_time(start, self.midnight_utc)
                end_minutes = min(minutes_to_time(end, self.midnight_utc), self.forecast_minutes + self.minutes_now)

            if start_minutes < (self.forecast_minutes + self.minutes_now):
                self.log("Octopus: Setting Octopus saving session in range {} - {} export {} rate {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), export, rate))
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

    def decode_octopus_slot(self, car_n, slot, raw=False):
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
            kwh = org_minutes * self.car_charging_rate[car_n] / 60.0

        try:
            kwh = abs(float(kwh))
        except (ValueError, TypeError):
            kwh = 0.0

        if org_minutes > 0:
            kwh = kwh * cap_minutes / org_minutes
        else:
            kwh = 0

        return start_minutes, end_minutes, kwh, source, location

    def load_octopus_slots(self, car_n, octopus_slots, octopus_intelligent_consider_full):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []
        if car_n >= self.num_cars:
            # Car not configured, just return the slots as they are (for export or other non-car use)
            return new_slots
        octopus_slot_low_rate = self.get_arg("octopus_slot_low_rate", True)
        octopus_slot_max = self.get_arg("octopus_slot_max", OCTOPUS_SLOT_MAX_DEFAULT)  # Default to 12 slots (6 hours) per midday-to-midday period
        slots_per_day = {}  # Track 30-min blocks used per midday-to-midday period
        car_soc = self.car_charging_soc[car_n]
        limit = self.car_charging_limit[car_n]
        slots_decoded = []

        # Decode the slots
        for slot in octopus_slots:
            start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(car_n, slot)
            # Octopus zeros chargeKwh once it calculates the car has hit its target SoC, but the
            # dispatch window stays open and the charger may still draw power. Preserve active slots
            # with a duration-based kwh so the "Hold for car" guard in execute.py still fires.
            if kwh == 0 and start_minutes <= self.minutes_now < end_minutes:
                remaining_minutes = end_minutes - self.minutes_now
                kwh = remaining_minutes * self.car_charging_rate[car_n] / 60.0
                start_minutes = self.minutes_now  # align span with the synthesised kwh so downstream rate calculations are consistent
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

            # Determine rate for this slot, applying the midday-to-midday cap
            slot_average = self.rate_import.get(start_minutes, self.rate_min_base)
            if octopus_slot_low_rate and source != "bump-charge" and source != "BOOST" and (not location or location == "AT_HOME"):
                # Count 30-min blocks for this slot against the midday-to-midday cap
                slot_block_start = (start_minutes // 30) * 30
                num_blocks = max(1, (end_minutes - slot_block_start + 29) // 30)
                day_offset = (start_minutes - 720) // (24 * 60)
                if day_offset not in slots_per_day:
                    slots_per_day[day_offset] = 0
                if slots_per_day[day_offset] + num_blocks <= octopus_slot_max:
                    slots_per_day[day_offset] += num_blocks
                    slot_average = self.rate_min_base
                else:
                    slot_average = self.rate_max_base

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
                    new_slot["average"] = slot_average
                    new_slot["cost"] = dp2(new_slot["average"] * kwh)
                    new_slot["soc"] = dp2(car_soc)
                    new_slot["octopus"] = True
                    new_slots.append(new_slot)

                    if end_minutes_original > end_minutes:
                        new_slot = {}
                        new_slot["start"] = end_minutes
                        new_slot["end"] = end_minutes_original
                        new_slot["kwh"] = 0.0
                        new_slot["average"] = slot_average
                        new_slot["cost"] = 0.0
                        new_slot["soc"] = dp2(car_soc)
                        new_slot["octopus"] = True
                        new_slots.append(new_slot)

                else:
                    car_soc = min(car_soc + kwh_expected, limit)
                    new_slot = {}
                    new_slot["start"] = start_minutes
                    new_slot["end"] = end_minutes
                    new_slot["kwh"] = kwh
                    new_slot["average"] = slot_average
                    new_slot["cost"] = dp2(new_slot["average"] * kwh)
                    new_slot["soc"] = dp2(car_soc)
                    new_slot["octopus"] = True
                    new_slots.append(new_slot)
        return new_slots

    def rate_add_io_slots(self, car_n, rates, octopus_slots):
        """
        # Add in any planned octopus slots
        # Octopus limits cheap slots to 6 hours (12 x 30-min slots) per 24-hour period
        """
        octopus_slot_low_rate = self.get_arg("octopus_slot_low_rate", True)
        octopus_slot_max = self.get_arg("octopus_slot_max", OCTOPUS_SLOT_MAX_DEFAULT)

        # Track slots per 24-hour period (keyed by day offset from midday)
        # Period 0 = noon today to 11:59 tomorrow, Period -1 = noon yesterday to 11:59 today, etc.
        slots_per_day = {}

        # Track which 30-min slot starts were actually added (for filling in the rest of the slot)
        slots_added_set = set()
        plan_interval_minutes = self.plan_interval_minutes
        saved_slots = set()  # For logging purposes, track which slots we actually applied as low rate

        if octopus_slots:
            # Add in IO slots
            for slot in octopus_slots:
                start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(car_n, slot, raw=True)

                # Ignore bump-charge slots as their cost won't change
                if source != "bump-charge" and source != "BOOST" and (not location or location == "AT_HOME"):
                    # Round slots to 30 minute boundary
                    # Floor the start (round down) and ceiling the end (round up)
                    # This ensures any partial overlap with a 30-min slot marks the entire slot as off-peak
                    start_minutes = (start_minutes // plan_interval_minutes) * plan_interval_minutes
                    end_minutes = ((end_minutes + plan_interval_minutes - 1) // plan_interval_minutes) * plan_interval_minutes
                    start_minutes = max(start_minutes, -96 * 60)  # Allow for previous 2 days
                    end_minutes = min(end_minutes, self.forecast_minutes)

                    for minute in range(start_minutes, end_minutes):
                        if octopus_slot_low_rate:
                            assumed_price = self.rate_min_base
                        else:
                            assumed_price = self.rate_import.get(start_minutes, self.rate_min)

                        if minute in saved_slots:
                            continue  # Already applied a low rate slot to this minute, skip
                        else:
                            saved_slots.add(minute)

                        # Calculate which day this minute belongs to (day boundary at midday)
                        # Period 0 = noon today (720) to 11:59 tomorrow (2159), etc.
                        # Python's floor division handles negative numbers correctly
                        day_offset = (minute - 720) // (24 * 60)

                        # Initialise counter for this day if needed
                        if day_offset not in slots_per_day:
                            slots_per_day[day_offset] = 0

                        # Calculate the 30-min slot start for this minute
                        slot_start = (minute // 30) * 30

                        # At the start of each 30-min slot, decide if we can add it
                        if minute % 30 == 0:
                            if slots_per_day[day_offset] < octopus_slot_max:
                                slots_per_day[day_offset] += 1
                                slots_added_set.add(slot_start)
                                rates[minute] = assumed_price
                            else:
                                assumed_price = self.rate_max_base
                        else:
                            # For minutes within a 30-min slot, only apply if the slot was added
                            if slot_start in slots_added_set:
                                rates[minute] = assumed_price

                        if minute % 30 == 0 and start_minutes > -24 * 60:
                            self.log(
                                "Octopus: Intelligent slot at {}-{}, assumed price {}, amount {}, kWh location {}, source {}, octopus_slot_low_rate {}".format(
                                    self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), dp2(assumed_price), dp2(kwh), location, source, octopus_slot_low_rate
                                )
                            )

        # Log daily slot counts for debugging
        for day_offset in sorted(slots_per_day.keys()):
            if slots_per_day[day_offset] > 0:
                self.log("Octopus: Intelligent slots for day {}: {} of {} max".format(day_offset, slots_per_day[day_offset], octopus_slot_max))

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
                self.log("Octopus: Fetch Octopus rates from {}".format(entity_id))

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
                        self.log("Warn: Octopus: No Octopus data in sensor {} attribute 'all_rates'".format(prev_rate_id))

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
                self.log("Warn: Octopus: No Octopus data in sensor {} attribute 'all_rates' / 'rates' / 'raw_today' / 'prices'".format(current_rate_id))

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

    def _saving_event_conflicts_axle(self, start_time, end_time, axle_sessions):
        """
        Return True if the saving session [start_time, end_time) overlaps any Axle VPP session
        """
        if not axle_sessions or start_time is None or end_time is None:
            return False
        for axle_session in axle_sessions:
            axle_start = axle_session.get("start_time")
            axle_end = axle_session.get("end_time")
            if not axle_start or not axle_end:
                continue
            try:
                axle_start = str2time(axle_start)
                axle_end = str2time(axle_end)
            except (ValueError, TypeError):
                continue
            # Standard half-open interval overlap test
            if start_time < axle_end and axle_start < end_time:
                return True
        return False

    def fetch_octopus_sessions(self, axle_sessions=None):
        """
        Fetch the Octopus saving/free sessions

        Available saving session events that overlap an Axle VPP session are not auto-joined,
        so Predbat does not commit to two conflicting events for the same period.
        """
        if axle_sessions is None:
            axle_sessions = []

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
                                self.log("Octopus: free events code {} {}-{}".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M")))
                            octopus_free_slot = {}
                            octopus_free_slot["start"] = start
                            octopus_free_slot["end"] = end
                            octopus_free_slot["rate"] = 0
                            octopus_free_slots.append(octopus_free_slot)
        # Direct Octopus URL
        if "octopus_free_url" in self.args:
            free_online = self.download_octopus_free(self.get_arg("octopus_free_url", indirect=False))
            octopus_free_slots.extend(free_online)

        # Load free electricity events from Octopus flexibility API
        if "octopus_free_electricity" in self.args:
            entity_id = self.get_arg("octopus_free_electricity", indirect=False)
            if entity_id:
                events = self.get_state_wrapper(entity_id=entity_id, attribute="events")
                if events:
                    for event in events:
                        start = event.get("start", None)
                        end = event.get("end", None)
                        if start and end:
                            octopus_free_slot = {}
                            octopus_free_slot["start"] = start
                            octopus_free_slot["end"] = end
                            octopus_free_slot["rate"] = 0
                            octopus_free_slots.append(octopus_free_slot)

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

            if available_events and not self.get_arg("octopus_saving_auto_join", True):
                self.log("Octopus: Saving session auto-join is disabled, not joining available events")
                # Clear the 2h throttle so re-enabling auto-join can take effect immediately
                self.octopus_last_joined_try = None
                available_events = []
            if available_events:
                # Only try to join every 2 hours to avoid spamming if it fails
                if not self.octopus_last_joined_try or (self.now_utc - self.octopus_last_joined_try).total_seconds() > 2 * 60 * 60:
                    for event in available_events:
                        code = event.get("code", None)  # decode the available events structure for code, start/end time & rate
                        start = event.get("start", None)
                        end = event.get("end", None)
                        start_time = str2time(start)  # reformat the saving session start & end time for improved readability
                        end_time = str2time(end)
                        octopoints_kwh = event.get("octopoints_per_kwh", None)
                        if octopoints_kwh is not None:
                            saving_rate = octopoints_kwh / octopoints_per_penny  # Octopoints per pence
                        else:
                            saving_rate = saving_rate  # Use default if not specified
                        # Do not auto-join a saving session that overlaps an Axle VPP session - we cannot honour both for the same period
                        if self._saving_event_conflicts_axle(start_time, end_time, axle_sessions):
                            self.log("Octopus: Skipping saving event code {} {}-{} - conflicts with an Axle VPP session".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M")))
                            continue
                        if code:  # Join the new Octopus saving event and send an alert
                            self.log("Octopus: Joining Octopus saving event code {} {}-{} at rate {} p/kWh".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))
                            entity_id_join = self.get_arg("octopus_saving_session_join", indirect=False)
                            if entity_id_join:
                                # Join via selector
                                self.call_service_wrapper("select/select_option", entity_id=entity_id_join, option=code)
                            else:
                                # Join via octopus event (Bottle Cap Dave)
                                self.call_service_wrapper("octopus_energy/join_octoplus_saving_session_event", event_code=code, entity_id=entity_id)
                            if self.get_arg("set_event_notify"):
                                self.call_notify("Predbat: Joined Octopus saving event {}-{}, {} p/kWh".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))
                            self.octopus_last_joined_try = self.now_utc

            # Default saving session rate for when octopoints_per_kwh is not available
            # (e.g. new flexibility API events that don't report reward rates)
            default_rate_pence = self.get_arg("octopus_saving_session_rate", 0)

            if joined_events:
                for event in joined_events:
                    start = event.get("start", None)
                    end = event.get("end", None)
                    octopoints_kwh = event.get("octopoints_per_kwh", None)
                    if octopoints_kwh is not None:
                        saving_rate = octopoints_kwh / octopoints_per_penny  # Octopoints per pence
                    elif default_rate_pence > 0:
                        saving_rate = default_rate_pence  # Use configured default rate
                    # Skip events with no rate info unless default is configured
                    if start and end and (octopoints_kwh is not None or default_rate_pence > 0) and saving_rate > 0:
                        # Save the saving slot?
                        try:
                            start_time = str2time(start)
                            end_time = str2time(end)
                            diff_time = start_time - self.now_utc
                            if abs(diff_time.days) <= 3:
                                self.log("Octopus: Joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state))

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


class MockBase:  # pragma: no cover
    """Mock base class for testing"""

    def __init__(self):
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.now_utc_exact = self.now_utc
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now(self.local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}
        self.config_root = "./temp_octopus"
        self.plan_interval_minutes = 30

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if "options" in attributes:
                attributes["options"] = "..."
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None, indirect=True, attribute=None, combine=False, index=None, domain=None, can_override=False, required_unit=None):
        return default

    def set_arg(self, key, value):
        state = None
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and "." in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")


async def test_fetch_tariffs(product_code, tariff_code):  # pragma: no cover
    """
    Fetch real tariff rates from the Octopus API and print a 5-day rate schedule.

    Calls fetch_tariffs with the given product/tariff codes, then extracts the rates
    attribute from the dashboard item (rates_stamp format) and prints all entries
    covering 5 days starting 2 days before today.
    """
    print(f"\nFetching tariff rates for product={product_code} tariff={tariff_code}")

    class QuietMockBase(MockBase):
        """MockBase variant that silently stores dashboard items without printing them."""

        def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
            self.set_state_wrapper(entity_id, state, attributes)

    mock_base = QuietMockBase()

    # Ensure temp cache dir exists
    os.makedirs(mock_base.config_root, exist_ok=True)

    octopus_api = OctopusAPI(mock_base, key="", account_id="test", automatic=False)
    octopus_api.tariffs = {"import": {"productCode": product_code, "tariffCode": tariff_code}}

    await octopus_api.fetch_tariffs(octopus_api.tariffs)

    # Extract the rates_stamp from the captured dashboard item
    entity_id = octopus_api.get_entity_name("sensor", "import_rates")
    entity = mock_base.entities.get(entity_id, {})
    rates_stamp = entity.get("attributes", {}).get("rates", [])

    if not rates_stamp:
        print("No rate data returned — check product/tariff codes are valid.")
        return

    # Determine 5-day window: 2 days back from today to 3 days ahead
    local_tz = mock_base.local_tz
    today_midnight = mock_base.midnight_utc
    window_start = today_midnight - timedelta(days=2)
    window_end = today_midnight + timedelta(days=3)

    print(f"\nUnit rates from {window_start.strftime('%Y-%m-%d')} to {window_end.strftime('%Y-%m-%d')}:")
    print(f"{'Start':<32}  {'End':<32}  {'p/kWh':>8}")
    print("-" * 76)

    # Collect entries within the 5-day window
    window_entries = []
    for entry in rates_stamp:
        try:
            start_dt = datetime.strptime(entry["start"], TIME_FORMAT)
        except (ValueError, KeyError):
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=local_tz)
        if start_dt >= window_start and start_dt < window_end:
            window_entries.append(entry)

    if not window_entries:
        print("(No entries in 5-day window — printing all available rates instead)")
        window_entries = rates_stamp

    # Merge consecutive entries with the same rate into contiguous blocks.
    # This turns dozens of 30-min same-rate slots (e.g. day/night tariffs) into
    # clean "23:30 -> 05:30 = 8.5p" lines instead of 12 identical 30-min rows.
    merged = []
    for entry in window_entries:
        rate_pence = round(entry["value_inc_vat"] * 100, 4)
        if merged and abs(merged[-1]["rate"] - rate_pence) < 0.0001:
            merged[-1]["end"] = entry["end"]
        else:
            merged.append({"start": entry["start"], "end": entry["end"], "rate": rate_pence})

    for entry in merged:
        print(f"{entry['start']:<32}  {entry['end']:<32}  {entry['rate']:>7.4f}p")

    print(f"\n{len(merged)} rate slot(s) shown.")


async def test_octopus_api(api_key, account_id):  # pragma: no cover
    """
    Test the Octopus API
    """

    print(f"Testing Octopus API with account: {account_id}")

    # Create a mock base object
    mock_base = MockBase()

    # Create OctopusAPI instanceFoxAPI(mock_base, **arg_dict)
    arg_dict = {
        "key": api_key,
        "account_id": account_id,
        "automatic": True,
    }
    octopus_api = OctopusAPI(mock_base, **arg_dict)
    await octopus_api.run(0, True)

    # Fetch data
    planned_dispatches = {}
    completed_dispatches = {}
    vehicles = {}
    for dev_id in octopus_api.get_intelligent_devices():
        planned_dispatches[dev_id] = octopus_api.get_intelligent_planned_dispatches(dev_id)
        completed_dispatches[dev_id] = octopus_api.get_intelligent_completed_dispatches(dev_id)
        vehicles[dev_id] = octopus_api.get_intelligent_vehicle(dev_id)
    available_events, joined_events = octopus_api.get_saving_session_data()

    print("Planned dispatches: {}".format(planned_dispatches))
    print("Completed dispatches: {}".format(completed_dispatches))
    print("Vehicles: {}".format(vehicles))
    print("Saving session available {}".format(available_events))
    print("Saving session joined {}".format(joined_events))

    # Test joining a saving session event
    octopus_api.join_saving_session_event("EVENT_26_270326")
    await octopus_api.run(1, False)
    await octopus_api.final()

    print("Test completed")


def main():  # pragma: no cover
    """
    Main function for command line execution to test Octopus API
    """
    import argparse

    parser = argparse.ArgumentParser(description="Test Octopus API")
    parser.add_argument("--api-key", default="", help="Octopus API key")
    parser.add_argument("--account", default="", help="Octopus account ID")
    parser.add_argument("--product-code", help="Product code for tariff rate test (e.g. AGILE-FLEX-22-11-25)")
    parser.add_argument("--tariff-code", help="Tariff code for tariff rate test (e.g. E-1R-AGILE-FLEX-22-11-25-C)")

    args = parser.parse_args()

    if args.product_code and args.tariff_code:
        # Tariff rate fetch test — no API key required (uses public Octopus REST API)
        asyncio.run(test_fetch_tariffs(args.product_code, args.tariff_code))
    elif args.api_key and args.account:
        # Full account API test
        asyncio.run(test_octopus_api(args.api_key, args.account))
    else:
        parser.error("Provide either --product-code and --tariff-code (tariff rate test) or --api-key and --account (full API test)")


if __name__ == "__main__":
    main()

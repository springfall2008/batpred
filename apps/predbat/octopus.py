# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import requests
import re
from datetime import datetime, timedelta
from config import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2
import aiohttp
import asyncio
import json
from datetime import timezone
import time
import os
import yaml

user_agent_value = "predbat-octopus-energy"
integration_context_header = "Ha-Integration-Context"

DATE_STR_FORMAT = "%Y-%m-%d"
DATE_TIME_STR_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


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


class OctopusEnergyApiClient:
    def __init__(self, api_key, log, timeout_in_seconds=20):
        if api_key is None:
            raise Exception("Octopus API KEY is not set")

        self.api_key = api_key
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
    def __init__(self, api_key, account_id, base):
        self.api_key = api_key
        self.base = base
        self.log = base.log
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
        if not os.path.exists(self.cache_path):
            os.makedirs(self.cache_path)
        self.cache_file = self.cache_path + "/octopus.yaml"

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
                self.now_utc = datetime.now(timezone.utc).astimezone()
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

                if first or (count_minutes % 2) == 0:
                    # 2-minute update for intelligent device sensor
                    await self.async_intelligent_update_sensor(self.account_id)
                    await self.save_octopus_cache()

                await self.async_join_saving_session_events(self.account_id)

                if not self.api_started:
                    print("Octopus API: Started")
                    self.api_started = True
                first = False

            except Exception as e:
                self.log("Error: Octopus API: {}".format(e))

            await asyncio.sleep(60)
        await self.api.async_close()
        print("Octopus API: Stopped")

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

    def stop(self):
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
        deviceID = import_tariff.get("deviceID", None)
        if deviceID:
            completed_dispatches = self.get_intelligent_completed_dispatches()
            intelligent_device = await self.async_get_intelligent_device(account_id, deviceID, completed_dispatches)
            if intelligent_device is not None:
                self.intelligent_device = intelligent_device
        return self.intelligent_device

    def join_saving_session_event(self, event_code):
        """
        Join a saving session event
        """
        self.saving_sessions_to_join.append(event_code)

    async def async_join_saving_session_events(self, account_id):
        """
        Join the saving session events
        """
        sessions_to_join = self.saving_sessions_to_join
        self.saving_sessions_to_join = []

        # Join the saving sessions
        for event_code in sessions_to_join:
            self.log("Octopus API: Joining saving session event {}".format(event_code))
            await self.async_graphql_query(octoplus_saving_session_join_mutation.format(account_id=account_id, event_code=event_code), "join-saving-session-event", returns_data=False)

        # Re-fetch the saving sessions if we have joined any
        if sessions_to_join:
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
            return devices.get("weekday_target_time", None)
        else:
            return None

    def get_intelligent_target_soc(self):
        """
        Get the intelligent target soc
        """
        devices = self.get_intelligent_device()
        if devices:
            return devices.get("weekday_target_soc", None)
        else:
            return None

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
        entity_name = "binary_sensor.predbat_octopus_" + self.account_id.replace("-", "_")
        entity_name = entity_name.lower()
        saving_attributes = {"friendly_name": "Octopus Intelligent Saving Sessions", "icon": "mdi:currency-usd", "joined_events": return_joined_events, "available_events": return_available_events}
        active_event = False
        for event in joined_events:
            start = event.get("start", None)
            end = event.get("end", None)
            if start and end:
                if start <= self.now_utc and end > self.now_utc:
                    active_event = True
                    break
        self.base.dashboard_item(entity_name + "_saving_session", "on" if active_event else "off", attributes=saving_attributes, app="octopus")

        return return_available_events, return_joined_events

    async def async_get_saving_sessions(self, account_id):
        """
        Get the saving sessions
        """
        response_data = await self.async_graphql_query(octoplus_saving_session_query.format(account_id=self.account_id), "get-saving-sessions")
        if response_data is None:
            return self.saving_sessions
        else:
            return response_data.get("savingSessions", {})

    async def async_download_octopus_url(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0
        while url and pages < 3:
            r = requests.get(url)
            if r.status_code not in [200, 201]:
                self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
                return {}
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                self.log("Warn: Error downloading Octopus data from URL {} (JSONDecodeError)".format(url))
                return {}
            if "results" in data:
                mdata += data["results"]
            else:
                self.log("Warn: Error downloading Octopus data from URL {} (No Results)".format(url))
                return {}
            url = data.get("next", None)
            pages += 1

        return mdata

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
        for tariff in tariffs:
            product_code = tariffs[tariff]["productCode"]
            tariff_code = tariffs[tariff]["tariffCode"]

            if tariff == "gas":
                tariff_type = "gas"
            else:
                tariff_type = "electricity"
            tariffs[tariff]["data"] = await self.fetch_url_cached(f"https://api.octopus.energy/v1/products/{product_code}/{tariff_type}-tariffs/{tariff_code}/standard-unit-rates/")
            tariffs[tariff]["standing"] = await self.fetch_url_cached(f"https://api.octopus.energy/v1/products/{product_code}/{tariff_type}-tariffs/{tariff_code}/standing-charges/")

            entity_id = "sensor.predbat_octopus_" + self.account_id.replace("-", "_") + "_" + tariff
            entity_id = entity_id.lower()

            rates = self.base.get_octopus_direct(tariff)
            standing = self.base.get_octopus_direct(tariff, standingCharge=True)

            rates_stamp = {}
            for minute in range(0, 60 * 24 * 2, 30):
                time_now = self.midnight_utc + timedelta(minutes=minute)
                rate_value = rates.get(minute, None)
                if rate_value is not None:
                    rates_stamp[time_now.strftime(TIME_FORMAT_OCTOPUS)] = rate_value
            rate_now = rates.get(self.now_utc.minute + self.now_utc.hour * 60, None)
            standing_now = standing.get(self.now_utc.minute + self.now_utc.hour * 60, None)

            self.base.dashboard_item(
                entity_id,
                rate_now,
                attributes={"friendly_name": "Octopus Tariff " + tariff, "icon": "mdi:currency-gbp", "standing_charge": standing_now, "rates": self.base.filtered_times(rates_stamp), "product_code": product_code, "tariff_code": tariff_code},
                app="octopus",
            )

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
            client = await self.api.async_create_client_session()
            url = f"{self.api.base_url}/v1/graphql/"
            payload = {"query": query}
            headers = {"Authorization": f"JWT {self.graphql_token}", integration_context_header: request_context}
            async with client.post(url, json=payload, headers=headers) as response:
                response_body = await self.async_read_response(response, url, ignore_errors=ignore_errors)
                if response_body and ("data" in response_body):
                    return response_body["data"]
                else:
                    if returns_data:
                        self.log(f"Warn: Octopus API: Failed to retrieve data from graphql query {request_context}")
                    return None
        except TimeoutError:
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
                result = {}
                dispatch_result = await self.async_graphql_query(intelligent_dispatches_query.format(account_id=account_id, device_id=device_id), "get-intelligent-dispatches", ignore_errors=True)
                chargePointVariants = device_result.get("chargePointVariants", [])
                electricVehicles = device_result.get("electricVehicles", [])
                devices = device_result.get("devices", [])
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
                                dispatch = {"start": start, "end": end, "charge_in_kwh": delta, "source": meta.get("source", None), "location": meta.get("location", None)}
                                planned.append(dispatch)
                            for completedDispatch in completedDispatches:
                                start = completedDispatch.get("start", None)
                                end = completedDispatch.get("end", None)
                                delta = completedDispatch.get("delta", None)
                                meta = completedDispatch.get("meta", {})
                                dispatch = {"start": start, "end": end, "charge_in_kwh": delta, "source": meta.get("source", None), "location": meta.get("location", None)}
                                # Check if the dispatch is already in the completed list, if its already there then don't add it again
                                found = False
                                for cached in completed:
                                    if cached["start"] == start:
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

        entity_name = "binary_sensor.predbat_octopus_" + account_id.replace("-", "_")
        entity_name = entity_name.lower()
        active_event = False
        for dispatch in planned:
            start = dispatch.get("start", None)
            end = dispatch.get("end", None)
            if start and end:
                start = parse_date_time(start)
                end = parse_date_time(end)
                if start <= self.now_utc and end > self.now_utc:
                    active_event = True
        dispatch_attributes = {"friendly_name": "Octopus Intelligent Dispatches", "icon": "mdi:flash", **intelligent_device}
        self.base.dashboard_item(entity_name + "_intelligent_dispatch", "on" if active_event else "off", attributes=dispatch_attributes, app="octopus")

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

        r = requests.get(url)
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
        Download octopus free session data directly from a URL and process the data
        """

        free_sessions = []
        pdata = self.download_octopus_free_func(url)
        if not pdata:
            return free_sessions

        for line in pdata.split("\n"):
            if "Past sessions" in line:
                future_line = line.split("<p data-block-key")
                for fline in future_line:
                    res = re.search(r"<i>\s*(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)\s*</i>", fline)
                    self.octopus_free_line(res, free_sessions)
            if "Free Electricity:" in line:
                # Free Electricity: Sunday 24th November 7-9am
                res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)
                self.octopus_free_line(res, free_sessions)
        return free_sessions

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

    def get_standing_charge_direct(self):
        """
        Get standing charge
        """
        pdata = self.get_octopus_direct("import", standingCharge=True)
        charge_now = pdata.get(self.now_utc.minute + self.now_utc.hour * 60, 0)
        return charge_now

    def get_octopus_direct(self, tariff_type, standingCharge=False):
        """
        Get the direct import rates from Octopus
        """
        if self.octopus_api_direct:
            tariff = self.octopus_api_direct.get_tariff(tariff_type)
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

                pdata = self.minute_data(tariff_data, self.forecast_days + 1, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
                return pdata
            else:
                # No tariff
                self.log("Warn: Octopus API direct tariff {} not available, using zero".format(tariff_type))
                return {n: 0 for n in range(0, 60 * 24)}

        self.log("Warn: Octopus API direct not available (get_octopus_direct tariff {})".format(tariff_type))
        return {}

    def download_octopus_rates_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0

        while url and pages < 3:
            if self.debug_enable:
                self.log("Download {}".format(url))
            r = requests.get(url)
            if r.status_code not in [200, 201]:
                self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
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

        pdata = self.minute_data(mdata, self.forecast_days + 1, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
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
                    self.log("Warn: Unable to decode Octopus free session start/end time")

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
        cap_hours = cap_minutes / 60

        # The load expected is stored in chargeKwh for the period in use
        if "charge_in_kwh" in slot:
            kwh = abs(float(slot.get("charge_in_kwh", 0.0)))
        elif "energy" in slot:
            kwh = abs(float(slot.get("energy", 0.0)))
        else:
            kwh = abs(float(slot.get("chargeKwh", 0.0)))

        if not kwh:
            kwh = self.car_charging_rate[0] * cap_hours
        else:
            kwh = kwh * cap_minutes / org_minutes

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
                    end_minutes = int((end_minutes + 29) / 30) * 30  # Round up to 30 minutes

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

            data_import = self.get_state_wrapper(entity_id=current_rate_id, attribute="rates") or self.get_state_wrapper(entity_id=current_rate_id, attribute="all_rates") or self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_today")
            if data_import:
                data_all += data_import
            else:
                self.log("Warn: No Octopus data in sensor {} attribute 'all_rates' / 'rates' / 'raw_today'".format(current_rate_id))

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
            rate_data = self.minute_data(data_all, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key, adjust_key=adjust_key, scale=scale)

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
        if self.octopus_api_direct or ("octopus_saving_session" in self.args):
            saving_rate = 200  # Default rate if not reported
            octopoints_per_penny = self.get_arg("octopus_saving_session_octopoints_per_penny", 8)  # Default 8 octopoints per found

            joined_events = []
            available_events = []
            state = False

            if self.octopus_api_direct:
                available_events, joined_events = self.octopus_api_direct.get_saving_session_data()
            else:
                entity_id = self.get_arg("octopus_saving_session", indirect=False)
                if entity_id:
                    state = self.get_arg("octopus_saving_session", False)
                    joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")
                    if not joined_events:
                        entity_id = entity_id.replace("binary_sensor.", "event.").replace("_sessions", "_session_events")
                        joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")

                    available_events = self.get_state_wrapper(entity_id=entity_id, attribute="available_events")

            if available_events:
                for event in available_events:
                    code = event.get("code", None)  # decode the available events structure for code, start/end time & rate
                    start = event.get("start", None)
                    end = event.get("end", None)
                    start_time = str2time(start)  # reformat the saving session start & end time for improved readability
                    end_time = str2time(end)
                    saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                    if code:  # Join the new Octopus saving event and send an alert
                        self.log("Joining Octopus saving event code {} {}-{} at rate {} p/kWh".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))
                        if self.octopus_api_direct:
                            self.octopus_api_direct.join_saving_session_event(code)
                        else:
                            self.call_service_wrapper("octopus_energy/join_octoplus_saving_session_event", event_code=code, entity_id=entity_id)
                        self.call_notify("Predbat: Joined Octopus saving event {}-{}, {} p/kWh".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))

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

import requests
from datetime import datetime, timezone
import asyncio
import random
import time
import base64
import hashlib
import hmac
import json
import traceback

SOLIS_CLOUD_API_URL = "https://www.soliscloud.com:13333/"
SOLIS_READ_ENDPOINT = "/v2/api/atRead"
SOLIS_READ_BATCH_ENDPOINT = "/v2/api/atReadBatch"
SOLIS_CONTROL_ENDPOINT = "/v2/api/control"
SOLIS_INVERTER_LIST_ENDPOINT = "/v1/api/inverterList"
SOLIS_STATION_DETAIL_ENDPOINT = "/v1/api/stationDetail"

attribute_table = {
    "inverter_power": {"friendly_name": "Inverter Power", "icon": "mdi:meter-electric", "unit_of_measurement": "W", "device_class": "power"},
    "state": {"friendly_name": "State", "icon": "mdi:state-machine", "device_class": "connectivity"},
    "import_today": {"friendly_name": "Import Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "export_today": {"friendly_name": "Export Today", "icon": "mdi:transmission-tower-export", "unit_of_measurement": "kWh", "device_class": "energy"},
    "load_today": {"friendly_name": "Load Today", "icon": "mdi:home", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_power": {"friendly_name": "Battery Power", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power"},
    "pv_energy_today": {"friendly_name": "Energy Today", "icon": "solar-power", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_percent": {"friendly_name": "Battery Percent", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery"},
    "meter_power": {"friendly_name": "Meter Power", "icon": "mdi:transmission-tower", "unit_of_measurement": "W", "device_class": "power"},
}

all_registers = [
    52,
    54,
    56,
    100,
    103,
    109,
    142,
    143,
    144,
    148,
    155,
    156,
    157,
    158,
    160,
    162,
    163,
    166,
    167,
    168,
    171,
    172,
    173,
    636,
    676,
    4611,
    4615,
    4773,
    5916,
    5917,
    5918,
    5919,
    5920,
    5921,
    5922,
    5923,
    5924,
    5925,
    5926,
    5927,
    5946,
    5928,
    5947,
    5948,
    5949,
    5929,
    5950,
    5951,
    5952,
    5930,
    5953,
    5954,
    5955,
    5931,
    5956,
    5957,
    5958,
    5932,
    5959,
    5960,
    5961,
    5933,
    5962,
    5963,
    5964,
    5965,
    5966,
    5967,
    5968,
    5969,
    5970,
    5971,
    5972,
    5973,
    5974,
    5975,
    5976,
    5977,
    5978,
    5979,
    5980,
    5981,
    5982,
    5983,
    5987,
    5984,
    5985,
    5986,
    13,
    14,
    15,
    18,
    463,
    532,
    549,
    52,
    54,
    56,
    284,
    285,
    286,
    287,
    289,
    290,
    291,
    292,
    293,
    294,
    295,
    296,
    297,
    303,
    305,
    307,
    308,
    309,
    310,
    311,
    312,
    316,
    317,
    318,
    319,
    321,
    323,
    324,
    325,
    326,
    327,
    367,
    378,
    538,
    539,
    540,
    541,
    544,
    574,
    575,
    576,
    577,
    578,
    579,
    580,
    634,
]


class SolisCloudDirect:
    def __init__(self, base):
        """
        Setup client
        """
        print(base)
        self.base = base
        self.log = base.log
        self.api_id = self.base.args.get("solis_cloud_api_id", None)
        self.api_secret = self.base.args.get("solis_cloud_api_secret", None)
        self.automatic = self.base.args.get("solis_cloud_automatic", False)
        self.stop_cloud = False
        self.api_started = False
        self.inverter_list = []
        self.inverter_detail = {}
        self.inverter_data = {}
        self.pending_writes = {}
        self.register_data = {}

        if not self.api_id:
            self.log("Error: SolisCloud: No API ID provided")
            raise ValueError("No API ID provided")
        if not self.api_secret:
            self.log("Error: SolisCloud: No API secret provided")
            raise ValueError("No API secret provided")

    def digest(self, body):
        return base64.b64encode(hashlib.md5(body.encode("utf-8")).digest()).decode("utf-8")

    def sign(self, key, message):
        hmac_object = hmac.new(
            key.encode("utf-8"),
            msg=message.encode("utf-8"),
            digestmod=hashlib.sha1,
        )
        return base64.b64encode(hmac_object.digest()).decode("utf-8")

    def wait_api_started(self):
        """
        Return if the API has started
        """
        self.log("SolisCloud: Waiting for API to start")
        count = 0
        while not self.api_started and count < 120:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: SolisCloud: API failed to start in required time")
            return False
        return True

    async def read_inverter_registers(self, device):
        for cid in self.register_data[device]:
            value = await self.read_single_register(device, cid)
            if value is not None:
                self.register_data[device][cid]["value"] = value
                self.log("SolisCloud: Read register {} for device {} value {}".format(cid, device, value))

    async def read_single_register(self, device, cid):
        """
        Read single register
        """
        payload = {"inverterSn": device, "cid": str(cid)}
        result = await self.api_call_retry(SOLIS_READ_ENDPOINT, payload)
        if result and "data" in result and result.get("data", None):
            data = result.get("data", {})
            msg = data.get("msg", None)
            if msg:
                return msg
        return None

    async def get_inverter_registers(self, device, all=False):
        """
        Get inverter registers
        """
        this_model = self.inverter_data.get(device, {}).get("productModel", None)
        cids = []
        if all:
            for cid in all_registers:
                if cid not in cids:
                    cids.append(str(cid))
        else:
            for cid in self.register_data.get(device, {}):
                cids.append(str(cid))

        # Process CIDs in batches of 50
        for start in range(0, len(cids), 50):
            end = start + 50
            batch_cids = cids[start:end]
            payload = {"inverterSn": device, "cids": ",".join(batch_cids), "language": 2}
            result = await self.api_call_retry(SOLIS_READ_BATCH_ENDPOINT, payload)
            if result and "data" in result and result.get("data", None):
                records = result.get("data", [])
                if records:
                    for record in records:
                        for item in record:
                            syscmd = item.get("sysCommand", {})
                            cid = syscmd.get("id", None)
                            if cid:
                                productModels = syscmd.get("productModel", "").split(",")
                                if this_model in productModels:
                                    self.register_data[device][cid] = syscmd
                                    name2 = syscmd.get("name2", None)
                                    value = syscmd.get("value", None)
                                    self.log("SolisCloud: Register {} for device {} name {} value {} cmd {}".format(cid, device, name2, value, syscmd))

    async def get_inverter_list(self):
        """
        Get inverter list
        """
        inverter_list = []
        result = await self.api_call_retry(SOLIS_INVERTER_LIST_ENDPOINT, {})
        if result and "data" in result:
            records = result.get("data").get("page", {}).get("records", [])
            for record in records:
                sn = record.get("sn", None)
                if sn:
                    inverter_list.append(sn)
                    self.inverter_data[sn] = record
        else:
            self.log("Warn: SolisCloud: Failed to get inverter list")
        self.inverter_list = inverter_list
        return inverter_list

    def to_float(self, value):
        """
        Convert value to float
        """
        try:
            value = float(value)
        except (ValueError, TypeError) as e:
            value = 0
        return value

    async def get_inverter_details(self, device):
        record = self.inverter_data.get(device, {})
        stationId = record.get("stationId", None)
        if stationId:
            payload = {
                "id": stationId,
            }
            detail = await self.api_call_retry(SOLIS_STATION_DETAIL_ENDPOINT, payload)
            if detail:
                detail_data = detail.get("data", {})
                if detail_data:
                    self.inverter_detail[device] = detail_data
                else:
                    self.log("Warn: SolisCloud: No data found for device %s", device)
            else:
                self.log("Warn: SolisCloud: Failed to get inverter details for device %s", device)
        else:
            self.log("Warn: SolisCloud: No inverter ID found for device %s", device)

    async def publish_registers(self, device):
        for cid in self.register_data[device]:
            record = self.register_data[device][cid]
            if record:
                cid = record.get("id", None)
                value = record.get("value", None)
                description = record.get("name2", None)
                register_name = description.replace(" ", "_").replace("-", "_").lower()
                entity_name = "solis_cloud_" + device + "_" + register_name
                min = record.get("min", None)
                max = record.get("max", None)
                unit = record.get("unit", None)
                on = record.get("on", None)
                off = record.get("off", None)

                attributes = {
                    "friendly_name": description,
                    "unit_of_measurement": unit,
                }

                if min and max:
                    base_type = "number"
                    attributes["min"] = min
                    attributes["max"] = max
                elif on and off:
                    base_type = "switch"
                    if value == on:
                        value = "on"
                    elif value == off:
                        value = "off"
                    else:
                        value = "unknown"
                else:
                    base_type = "sensor"

                self.base.dashboard_item(base_type + "." + entity_name, value, app="soliscloud", attributes=attributes)

    async def publish_device(self, device):
        detail = self.inverter_detail.get(device, {})
        print("SolisCloud: Publishing device {} detail {}".format(device, detail))
        if detail:
            entity_name = "sensor.soliscloud_" + device
            power_max = self.to_float(detail.get("inverterPower", 0)) * 1000.0
            power_value = self.to_float(detail.get("power", 0)) * 1000.0
            power_attr = attribute_table.get("inverter_power", {})
            power_attr["max"] = power_max
            self.base.dashboard_item(entity_name + "_inverter_power", int(power_value), attributes=power_attr, app="soliscloud")
            state = self.to_float(detail.get("state", 0))
            state_str = "online" if state == 1 else "offline" if state == 2 else "alarm" if state == 3 else "unknown"
            self.base.dashboard_item(entity_name + "_state", state_str, attributes=attribute_table.get("state", {}), app="soliscloud")

            gridPurchasedTodayEnergy = self.to_float(detail.get("gridPurchasedDayEnergy", 0))
            self.base.dashboard_item(entity_name + "_import_today", gridPurchasedTodayEnergy, attributes=attribute_table.get("import_today", {}), app="soliscloud")
            gridSellEnergyToday = self.to_float(detail.get("gridSellDayEnergy", 0))
            self.base.dashboard_item(entity_name + "_export_today", gridSellEnergyToday, attributes=attribute_table.get("export_today", {}), app="soliscloud")
            homeLoadTodayEnergy = self.to_float(detail.get("homeLoadEnergy", 0))
            self.base.dashboard_item(entity_name + "_load_today", homeLoadTodayEnergy, attributes=attribute_table.get("load_today", {}), app="soliscloud")
            batteryPower = self.to_float(detail.get("batteryPower", 0)) * 1000.0
            self.base.dashboard_item(entity_name + "_battery_power", int(batteryPower), attributes=attribute_table.get("battery_power", {}), app="soliscloud")
            pv_energy_today = self.to_float(detail.get("dayEnergy", 0))
            self.base.dashboard_item(entity_name + "_pv_energy_today", pv_energy_today, attributes=attribute_table.get("pv_energy_today", {}), app="soliscloud")
            battery_percent = self.to_float(detail.get("batteryPercent", 0))
            self.base.dashboard_item(entity_name + "_battery_percent", battery_percent, attributes=attribute_table.get("battery_percent"), app="soliscloud")
            meter_power = self.to_float(detail.get("psum", 0)) * 1000.0
            self.base.dashboard_item(entity_name + "_meter_power", int(meter_power), attributes=attribute_table.get("meter_power", {}), app="soliscloud")

    async def start(self):
        """
        Start the client
        """
        self.stop_cloud = False
        self.api_started = False

        # Get devices using the modified auto-detection (returns dict)
        devices = await self.get_inverter_list()

        # Discover registers
        for device in devices:
            self.register_data[device] = {}
            await self.get_inverter_registers(device, all=True)

        self.log("SolisCloud: Starting up, found devices {}".format(devices))
        for device in devices:
            self.pending_writes[device] = []

        # if self.automatic:
        #    await self.async_automatic_config(devices_dict)

        seconds = 0
        while not self.stop_cloud and not self.base.fatal_error:
            try:
                if seconds % 300 == 0:
                    devices = await self.get_inverter_list()
                    for device in devices:
                        await self.read_inverter_registers(device)
                        await self.publish_registers(device)
                if seconds % 60 == 0:
                    for device in devices:
                        self.log("SolisCloud: Getting inverter details for device {}".format(device))
                        # await self.get_inverter_details(device)
                        # await self.publish_device(device)

            except Exception as e:
                self.log("Error: SolisCloud: Exception in main loop {}".format(e))
                self.log("Error: " + traceback.format_exc())

            # Clear pending writes
            for device in devices:
                if device in self.pending_writes:
                    self.pending_writes[device] = []

            if not self.api_started:
                print("SolisCloud API Started")
                self.api_started = True
            await asyncio.sleep(5)
            seconds += 5

    async def stop(self):
        self.stop_cloud = True

    async def api_call_retry(self, endpoint, payload):
        """
        Retry API call
        """
        for i in range(5):
            res = None
            try:
                res = await self.api_call(endpoint, payload)
                if res:
                    code = res.get("code", None)
                    if code == "0":
                        return res
            except Exception as e:
                pass
            self.log("SolisCloud: API call failed: result {}".format(res))
            time.sleep(random.uniform(1, 3))
        return None

    async def api_call(self, endpoint, payload):
        body = json.dumps(payload)

        payload_digest = self.digest(body)
        content_type = "application/json"
        date_formatted = base.now_utc.strftime("%a, %d %b %Y %H:%M:%S GMT")

        authorization_str = "\n".join(["POST", payload_digest, content_type, date_formatted, endpoint])
        authorization_sign = self.sign(self.api_secret, authorization_str)
        authorization = f"API {self.api_id}:{authorization_sign}"

        headers = {
            "Content-MD5": payload_digest,
            "Content-Type": content_type,
            "Date": date_formatted,
            "Authorization": authorization,
        }

        url = SOLIS_CLOUD_API_URL + endpoint

        self.log("SolisCloud: API request '%s': %s", url, json.dumps(payload, indent=2))

        res = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=10,
        )
        if res.status_code != 200:
            raise Exception(f"API call failed with status code {res.status_code}: {res.text}")

        response = res.json()
        return response


class DummyBase:
    def __init__(self):
        self.log = print
        self.args = {
            "solis_cloud_api_id": "1300386381677041724",
            "solis_cloud_api_secret": "05d85967c4804aff9e698498374bf6b3",
            "solis_cloud_automatic": False,
        }
        self.now_utc = datetime.now(timezone.utc)
        self.fatal_error = False

    def dashboard_item(self, entity_name, value, attributes=None, app=None):
        print(f"Dashboard item {entity_name}: {value}, attributes: {attributes}, app: {app}")


if __name__ == "__main__":
    # Example usage
    base = DummyBase()
    solis_cloud = SolisCloudDirect(base)
    asyncio.run(solis_cloud.start())

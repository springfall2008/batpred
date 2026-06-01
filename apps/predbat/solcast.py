# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Solar forecast integration with Solcast and Forecast.Solar APIs.

Fetches PV generation forecasts from multiple sources with HTTP caching,
multi-site aggregation, and request tracking. Supports both free and
personal API tiers.
"""

import hashlib
import math
import random
import aiohttp
import pytz
from datetime import datetime, timedelta, timezone

try:
    from pvlib.temperature import sapm_cell as _pvlib_sapm_cell
    from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS as _PVLIB_TEMP_PARAMS

    _PVLIB_SAPM_PARAMS = _PVLIB_TEMP_PARAMS["sapm"]["open_rack_glass_glass"]
    _HAS_PVLIB = True
except ImportError:
    _HAS_PVLIB = False

# PVWatts / SAPM cell temperature model constants (glass/glass, open rack)
_SAPM_A = -3.47
_SAPM_B = -0.0594
_SAPM_DELTA_T = 3.0


def pvwatts_cell_temperature(poa_global, temp_air, wind_speed):
    """Compute PV cell temperature using the SAPM (PVWatts) model.

    Uses pvlib.temperature.sapm_cell when available; falls back to the
    equivalent inline formula otherwise.  Parameters correspond to a
    glass/glass module on an open rack (the most common residential case).
    """
    if _HAS_PVLIB:
        return float(_pvlib_sapm_cell(poa_global, temp_air, wind_speed, _PVLIB_SAPM_PARAMS["a"], _PVLIB_SAPM_PARAMS["b"], _PVLIB_SAPM_PARAMS["deltaT"]))
    # Inline SAPM formula: T_cell = T_air + GTI * exp(a + b*wind) + (GTI/1000) * deltaT
    return temp_air + poa_global * math.exp(_SAPM_A + _SAPM_B * wind_speed) + (poa_global / 1000.0) * _SAPM_DELTA_T


from const import TIME_FORMAT, TIME_FORMAT_SOLCAST
from utils import dp2, dp4, history_attribute_to_minute_data, minute_data, history_attribute, prune_today
from predbat_metrics import record_api_call, metrics
from component_base import ComponentBase

"""
Solcast class deals with fetching solar predictions, processing the data and publishing the results.
"""

PV_CALIBRATION_LOWEST = 0.20
PV_CALIBRATION_HIGHEST = 4.0


class SolarAPI(ComponentBase):
    """
    SolarAPI is responsible for managing and aggregating solar forecast data from multiple sources,
    including Solcast, Forecast.Solar, and direct sensor inputs. It periodically fetches, processes,
    and publishes solar production forecasts for use by the home battery system. SolarAPI operates
    as an asynchronous background component, ensuring up-to-date solar predictions are available
    for system optimisation and decision-making.
    """

    def initialize(
        self,
        solcast_host,
        solcast_api_key,
        solcast_sites,
        solcast_poll_hours,
        forecast_solar,
        forecast_solar_max_age,
        pv_forecast_today,
        pv_forecast_tomorrow,
        pv_forecast_d3,
        pv_forecast_d4,
        pv_scaling,
        open_meteo_forecast,
        open_meteo_forecast_max_age,
    ):
        """Initialise the Solar API component"""
        self.solcast_host = solcast_host
        self.solcast_api_key = solcast_api_key
        self.solcast_sites = solcast_sites
        self.solcast_poll_hours = solcast_poll_hours
        self.forecast_solar = forecast_solar
        self.forecast_solar_max_age = forecast_solar_max_age
        self.pv_forecast_today = pv_forecast_today
        self.pv_forecast_tomorrow = pv_forecast_tomorrow
        self.pv_forecast_d3 = pv_forecast_d3
        self.pv_forecast_d4 = pv_forecast_d4
        self.pv_scaling = pv_scaling
        self.open_meteo_forecast = open_meteo_forecast
        self.open_meteo_forecast_max_age = open_meteo_forecast_max_age
        self.solcast_requests_total = 0
        self.solcast_failures_total = 0
        self.forecast_solar_requests_total = 0
        self.forecast_solar_failures_total = 0
        self.open_meteo_requests_total = 0
        self.open_meteo_failures_total = 0
        self.solcast_last_success_timestamp = None
        self.forecast_solar_last_success_timestamp = None
        self.open_meteo_last_success_timestamp = None
        self.forecast_solar_rate_limit_until = None
        self.last_fetched_timestamp = None
        self.forecast_days = 4

    async def run(self, seconds, first):
        """
        Run the Solar API
        """
        fetch_age = 9999
        same_day = False
        if self.last_fetched_timestamp:
            fetch_age = (self.now_utc_exact - self.last_fetched_timestamp).total_seconds() / 60
            same_day = self.last_fetched_timestamp.date() == self.now_utc_exact.date()

        if seconds % (self.plan_interval_minutes * 60) == 0:  # Every plan_interval_minutes
            await self.fetch_pv_forecast()
        elif not same_day or (fetch_age > 60):  # If data is older than 60 minutes or it's a new day, fetch new data
            await self.fetch_pv_forecast()
        return True

    async def cache_get_url(self, url, params, max_age=8 * 60):
        # Check if this is a Solcast API call for metrics tracking
        is_solcast_api = "solcast.com" in url.lower() or "api.solcast" in url.lower()
        is_forecast_solar_api = "forecast.solar" in url.lower()
        is_open_meteo_api = "open-meteo.com" in url.lower()

        # Increment request counter for Solcast API calls
        if is_solcast_api:
            self.solcast_requests_total += 1

        # Increment request counter for forecast.solar API calls
        if is_forecast_solar_api:
            self.forecast_solar_requests_total += 1

        # Increment request counter for Open-Meteo API calls
        if is_open_meteo_api:
            self.open_meteo_requests_total += 1

        # Get data from cache
        data = None
        hash_key = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash_key = hash_key.replace("/", "_")
        hash_key = hash_key.replace(":", "_")
        hash_key = hash_key.replace("?", "a")
        hash_key = hash_key.replace("&", "b")
        hash_key = hash_key.replace("*", "c")

        stale_data = None
        if self.storage:
            data = await self.storage.load("solar", hash_key)
            if data is not None:
                age_minutes = await self.storage.age("solar", hash_key)
                if age_minutes is not None and age_minutes < max_age:
                    self.log("SolarAPI: Return cached data for {}".format(url))
                    return data
                # Data exists but is older than max_age - keep as stale fallback
                stale_data = data
        data = None

        # Perform fetch
        self.log("SolarAPI: Fetching {}".format(url))
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params) as response:
                    status_code = response.status

                    if status_code not in [200, 201]:
                        self.log("Warn: SolarAPI: Error downloading data from url {}, code {}".format(url, status_code))
                        if is_solcast_api:
                            self.solcast_failures_total += 1
                            record_api_call("solcast", False, "server_error")
                        if is_forecast_solar_api:
                            self.forecast_solar_failures_total += 1
                            if status_code == 429:
                                retry_minutes = random.randint(60, 120)
                                self.forecast_solar_rate_limit_until = datetime.now(timezone.utc) + timedelta(minutes=retry_minutes)
                                self.log("Warn: SolarAPI: Forecast Solar rate limited (429), will retry after {} minutes (at {})".format(retry_minutes, self.forecast_solar_rate_limit_until.strftime(TIME_FORMAT)))
                                record_api_call("forecast_solar", False, "rate_limit")
                            else:
                                record_api_call("forecast_solar", False, "server_error")
                        if is_open_meteo_api:
                            self.open_meteo_failures_total += 1
                            record_api_call("open_meteo", False, "server_error")
                        return stale_data

                    try:
                        data = await response.json()
                        if is_solcast_api:
                            self.solcast_last_success_timestamp = datetime.now(timezone.utc)
                            record_api_call("solcast")
                        if is_forecast_solar_api:
                            self.forecast_solar_last_success_timestamp = datetime.now(timezone.utc)
                            record_api_call("forecast_solar")
                        if is_open_meteo_api:
                            self.open_meteo_last_success_timestamp = datetime.now(timezone.utc)
                            record_api_call("open_meteo")
                    except (aiohttp.ContentTypeError, Exception) as e:
                        self.log("Warn: SolarAPI: Error downloading data from URL {}, error {} code {}".format(url, e, status_code))
                        if is_solcast_api:
                            self.solcast_failures_total += 1
                            record_api_call("solcast", False, "decode_error")
                        if is_forecast_solar_api:
                            self.forecast_solar_failures_total += 1
                            record_api_call("forecast_solar", False, "decode_error")
                        if is_open_meteo_api:
                            self.open_meteo_failures_total += 1
                            record_api_call("open_meteo", False, "decode_error")
                        if stale_data:
                            self.log("Warn: SolarAPI: Error downloading data from URL {}, using stale cached data".format(url))
                            return stale_data
                        else:
                            self.log("Warn: SolarAPI: Error downloading data from URL {}, no cached data".format(url))
                            return None

        except (aiohttp.ClientError, Exception) as e:
            self.log("Warn: SolarAPI: Error downloading data from URL {}, error {}".format(url, e))
            if is_solcast_api:
                self.solcast_failures_total += 1
                record_api_call("solcast", False, "connection_error")
            if is_forecast_solar_api:
                self.forecast_solar_failures_total += 1
                record_api_call("forecast_solar", False, "connection_error")
            if is_open_meteo_api:
                self.open_meteo_failures_total += 1
                record_api_call("open_meteo", False, "connection_error")
            return stale_data

        # Store data in cache with 7-day expiry
        if self.storage and data:
            expiry = datetime.now(timezone.utc) + timedelta(days=7)
            await self.storage.save("solar", hash_key, data, format="json", expiry=expiry)
        return data

    URL_FREE = "https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}?time=utc"
    URL_PERSONAL = "https://api.forecast.solar/{api_key}/estimate/{lat}/{lon}/{dec}/{az}/{kwp}?time=utc"
    URL_PERSONAL_DUAL = "https://api.forecast.solar/{api_key}/estimate/{lat}/{lon}/{dec1}/{az1}/{kwp1}/{dec2}/{az2}/{kwp2}?time=utc"

    def convert_azimuth(self, az):
        """
        Convert azimuth from Predbat/Solcast convention to Forecast.solar/Open-Meteo convention.
        Predbat/Solcast convention:         0 = North, -90 = East, 90 = West, 180 = South
        Forecast.solar/Open-Meteo convention: 0 = South, -90 = East, 90 = West, ±180 = North
        """
        if az >= 0:
            az = 180 - az
        else:
            az = -180 - az

        return az

    async def download_open_meteo_ensemble_data(self, lat, lon, tilt, az, kwp, system_loss):
        """
        Download Open-Meteo ensemble data for P10 solar estimate.
        Returns a dict mapping ISO timestamp strings to P10 kW values.
        """
        url = "https://ensemble-api.open-meteo.com/v1/ensemble?models=icon_seamless&latitude={lat}&longitude={lon}&hourly=global_tilted_irradiance&tilt={tilt}&azimuth={az}&forecast_days=4&timezone=UTC".format(lat=lat, lon=lon, tilt=tilt, az=az)
        data = await self.cache_get_url(url, params={}, max_age=self.open_meteo_forecast_max_age * 60)
        if not data:
            return {}

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        member_keys = [k for k in hourly if k.startswith("global_tilted_irradiance_member")]
        if not member_keys or not times:
            return {}

        result = {}
        for idx, ts in enumerate(times):
            values = []
            for k in member_keys:
                val = hourly[k][idx] if idx < len(hourly[k]) else None
                if val is not None:
                    values.append(val)
            if not values:
                result[ts] = 0.0
                continue
            values.sort()
            p10_idx = max(0, math.ceil(len(values) * 0.10) - 1)
            gti_p10 = values[p10_idx]
            result[ts] = dp4((gti_p10 / 1000.0) * kwp * (1.0 - system_loss))
        return result

    async def download_open_meteo_data(self):
        """
        Download Open-Meteo forecast data and convert to PV power estimates.
        Uses GTI (global tilted irradiance) with simple temperature derating for P50,
        and ensemble members for P10. Returns (sorted_data, max_kwh).
        """
        period_data = {}
        max_kwh = 0

        configs = self.open_meteo_forecast
        if configs is None:
            raise ValueError("SolarAPI: No Open-Meteo forecast configurations found")
        if not isinstance(configs, list):
            configs = [configs]

        for config in configs:
            lat = config.get("latitude", 51.5072)
            lon = config.get("longitude", -0.1276)
            postcode = config.get("postcode", None)
            tilt = config.get("declination", 35.0)
            az = config.get("azimuth", 180.0)
            if not config.get("azimuth_zero_south", False):
                az = self.convert_azimuth(az)
            kwp = config.get("kwp", 3.0)
            system_loss = 1.0 - config.get("efficiency", 0.95)
            shading_factors = config.get("shading_factors", None)

            if shading_factors and len(shading_factors) == 12:
                self.log("SolarAPI: Open-Meteo: Using per-month shading factors for lat {} lon {}".format(lat, lon))

            max_kwh += kwp * (1.0 - system_loss)

            if postcode:
                postcode_data = await self.cache_get_url("https://api.postcodes.io/postcodes/{}".format(postcode), params={}, max_age=24 * 60 * 30)
                if postcode_data:
                    postcode_result = postcode_data.get("result", {})
                    if "longitude" in postcode_result and "latitude" in postcode_result:
                        lon = postcode_result.get("longitude", lon)
                        lat = postcode_result.get("latitude", lat)
                        self.log("SolarAPI: Postcode {} resolved to latitude {} longitude {}".format(postcode, lat, lon))
                    else:
                        self.log("Warn: SolarAPI: Postcode {} could not be resolved to latitude and longitude, using default".format(postcode))

            url = "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=global_tilted_irradiance,temperature_2m,wind_speed_10m&wind_speed_unit=ms&tilt={tilt}&azimuth={az}&forecast_days=4&timezone=UTC".format(
                lat=lat, lon=lon, tilt=tilt, az=az
            )
            data = await self.cache_get_url(url, params={}, max_age=self.open_meteo_forecast_max_age * 60)
            if not data:
                self.log("Warn: SolarAPI: Open-Meteo data for lat {} lon {} could not be downloaded".format(lat, lon))
                continue

            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            gti_values = hourly.get("global_tilted_irradiance", [])
            temp_values = hourly.get("temperature_2m", [])
            wind_values = hourly.get("wind_speed_10m", [])

            if not times or not gti_values:
                self.log("Warn: SolarAPI: Open-Meteo data for lat {} lon {} has no hourly data".format(lat, lon))
                continue

            ensemble_p10 = await self.download_open_meteo_ensemble_data(lat, lon, tilt, az, kwp, system_loss)

            # Pass 1: compute instantaneous kW at each UTC timestamp sample.
            # Open-Meteo returns point-in-time irradiance (W/m²) at the start of each hour,
            # so we must integrate over the period rather than treating the sample as the period energy.
            instant_kw = {}  # datetime stamp -> (pv50_kw, pv10_kw)
            instant_stamps = []
            for idx, ts in enumerate(times):
                if idx >= len(gti_values):
                    break
                gti = gti_values[idx]
                if gti is None:
                    gti = 0.0
                temp = temp_values[idx] if idx < len(temp_values) and temp_values[idx] is not None else 25.0
                wind = wind_values[idx] if idx < len(wind_values) and wind_values[idx] is not None else 1.0
                # Cell temperature via SAPM/PVWatts model: irradiance heats the cell above ambient
                t_cell = pvwatts_cell_temperature(gti, temp, wind)
                # c-Si temperature coefficient: -0.4%/°C relative to STC (25°C)
                # No lower clamp on (t_cell - 25): cool cells genuinely produce more power.
                # Cap at 1.1 (10% above STC) to prevent unrealistic gains at very cold temperatures.
                eta_temp = max(0.5, min(1.1, 1.0 - 0.004 * (t_cell - 25.0)))
                pv50_inst = dp4((gti / 1000.0) * kwp * eta_temp * (1.0 - system_loss))
                raw_p10 = ensemble_p10.get(ts)
                # ensemble_p10 was computed without temperature derating; apply eta_temp now
                pv10_inst = dp4(min(raw_p10 * eta_temp, pv50_inst) if raw_p10 is not None else pv50_inst * 0.7)
                try:
                    stamp = datetime.strptime(ts, "%Y-%m-%dT%H:%M")
                    stamp = stamp.replace(tzinfo=pytz.utc)
                except (ValueError, TypeError):
                    continue
                instant_kw[stamp] = (pv50_inst, pv10_inst)
                instant_stamps.append(stamp)

            # Pass 2: trapezoidal integration — energy over [T, T+1h] = 0.5*(kW_at_T + kW_at_T+1h).
            # This correctly accounts for sunrise/sunset transitions where irradiance changes rapidly
            # within the hour, e.g. the first post-sunrise hour contains only partial sunshine.
            for i in range(len(instant_stamps) - 1):
                stamp = instant_stamps[i]
                next_stamp = instant_stamps[i + 1]
                if (next_stamp - stamp) != timedelta(hours=1):
                    continue
                pv50_start, pv10_start = instant_kw[stamp]
                pv50_end, pv10_end = instant_kw[next_stamp]
                pv50 = dp4(0.5 * (pv50_start + pv50_end))
                pv10 = dp4(0.5 * (pv10_start + pv10_end))

                # Apply per-month site shading correction from Google Solar API if available
                if shading_factors and len(shading_factors) == 12:
                    shading_month = shading_factors[stamp.month - 1]
                    pv50 = dp4(pv50 * shading_month)
                    pv10 = dp4(pv10 * shading_month)

                data_item = {"period_start": stamp.strftime(TIME_FORMAT), "pv_estimate": pv50, "pv_estimate10": pv10}
                if stamp in period_data:
                    period_data[stamp]["pv_estimate"] = dp4(period_data[stamp]["pv_estimate"] + pv50)
                    period_data[stamp]["pv_estimate10"] = dp4(period_data[stamp]["pv_estimate10"] + pv10)
                else:
                    period_data[stamp] = data_item

        sorted_data = []
        if period_data:
            for key in sorted(period_data.keys()):
                sorted_data.append(period_data[key])

        self.log("SolarAPI: Open-Meteo returned {} data points".format(len(sorted_data)))
        return sorted_data, max_kwh

    async def download_forecast_solar_data(self):
        """
        Download forecast.solar data directly from a URL or return from cache if recent.
        """
        if self.forecast_solar_rate_limit_until is not None:
            now_utc = datetime.now(timezone.utc)
            if now_utc < self.forecast_solar_rate_limit_until:
                self.log("Warn: SolarAPI: Forecast Solar rate limit active, skipping fetch until {}".format(self.forecast_solar_rate_limit_until.strftime(TIME_FORMAT)))
                return [], 0
            else:
                self.forecast_solar_rate_limit_until = None

        self.forecast_solar_data = {}
        if self.storage:
            cached = await self.storage.load("solcast", "forecast_solar_data")
            if isinstance(cached, dict):
                self.forecast_solar_data = cached

        configs = self.forecast_solar
        if configs is None:
            raise ValueError("SolarAPI: No forecast solar configurations found")

        if not isinstance(configs, list):
            configs = [configs]

        period_data = {}
        max_kwh = 0

        # Phase 1: Resolve all configs (postcodes, azimuth conversion) into a flat plane list
        resolved_planes = []
        for config in configs:
            lat = config.get("latitude", 51.5072)
            lon = config.get("longitude", -0.1276)
            postcode = config.get("postcode", None)
            dec = config.get("declination", 35.0)
            az = config.get("azimuth", 180.0)
            if not config.get("azimuth_zero_south", False):
                az = self.convert_azimuth(az)
            kwp = config.get("kwp", 3.0)
            efficiency = config.get("efficiency", 0.95)
            api_key = config.get("api_key", None)

            max_kwh += kwp * efficiency  # Total kWh for this configuration

            if postcode:
                result = await self.cache_get_url("https://api.postcodes.io/postcodes/{}".format(postcode), params={}, max_age=24 * 60 * 30)  # Cache postcode data for 30 days
                if not result:
                    self.log("Warn: SolarAPI: Postcode {} could not be resolved, no postcode lookup data available".format(postcode))
                    result = {}
                result = result.get("result", {})
                if "longitude" not in result or "latitude" not in result:
                    self.log("Warn: SolarAPI: Postcode {} could not be resolved to latitude and longitude, using default".format(postcode))
                else:
                    lon = result.get("longitude", lon)
                    lat = result.get("latitude", lat)
                    self.log("SolarAPI: Postcode {} resolved to latitude {} longitude {}".format(postcode, lat, lon))

            days_data = config.get("days", 3 if api_key else 2)
            resolved_planes.append({"lat": lat, "lon": lon, "dec": dec, "az": az, "kwp": kwp, "efficiency": efficiency, "api_key": api_key, "days_data": days_data})

        # Phase 2: Build fetch groups.
        # Consecutive personal planes (api_key set) at the same lat/lon are paired into a single
        # dual-plane Personal Plus call, halving the number of API requests for those planes.
        # Free-tier planes and personal planes at different locations are fetched individually.
        fetch_groups = []
        i = 0
        while i < len(resolved_planes):
            plane = resolved_planes[i]
            next_plane = resolved_planes[i + 1] if i + 1 < len(resolved_planes) else None
            if plane["api_key"] and next_plane is not None and next_plane["api_key"] == plane["api_key"] and next_plane["lat"] == plane["lat"] and next_plane["lon"] == plane["lon"]:
                fetch_groups.append([plane, next_plane])
                i += 2
            else:
                fetch_groups.append([plane])
                i += 1

        # Phase 3: Fetch and parse each group
        for group in fetch_groups:
            if len(group) == 2:
                # Dual-plane Personal Plus call.
                # Efficiency is baked into the kwp sent to the API so that the combined
                # response (which cannot be split per-plane) is already correctly scaled.
                p1, p2 = group
                url = self.URL_PERSONAL_DUAL.format(
                    api_key=p1["api_key"],
                    lat=p1["lat"],
                    lon=p1["lon"],
                    dec1=p1["dec"],
                    az1=p1["az"],
                    kwp1=p1["kwp"] * p1["efficiency"],
                    dec2=p2["dec"],
                    az2=p2["az"],
                    kwp2=p2["kwp"] * p2["efficiency"],
                )
                days_data = max(p1["days_data"], p2["days_data"])
                self.log("SolarAPI: Fetching dual-plane Forecast Solar for lat {} lon {} (plane1: dec={} az={} kwp={}, plane2: dec={} az={} kwp={})".format(p1["lat"], p1["lon"], p1["dec"], p1["az"], p1["kwp"], p2["dec"], p2["az"], p2["kwp"]))
            else:
                p = group[0]
                if p["api_key"]:
                    url = self.URL_PERSONAL.format(api_key=p["api_key"], lat=p["lat"], lon=p["lon"], dec=p["dec"], az=p["az"], kwp=p["kwp"] * p["efficiency"])
                else:
                    url = self.URL_FREE.format(lat=p["lat"], lon=p["lon"], dec=p["dec"], az=p["az"], kwp=p["kwp"] * p["efficiency"])
                days_data = p["days_data"]

            data = await self.cache_get_url(url, params={}, max_age=self.forecast_solar_max_age * 60)
            if not data:
                self.log("Warn: SolarAPI: Forecast Solar data could not be downloaded, check your Forecast Solar cloud settings")
                return [], 0
            watts = data.get("result", {}).get("watts", {})
            info = data.get("message", {}).get("info", {})
            if not watts or not info:
                self.log("Warn: SolarAPI: Forecast Solar data could not be downloaded, check your Forecast Solar cloud settings, got {}".format(data))
                return [], 0

            period_start_stamp = None
            forecast_watt_data = {}
            for period_end in watts:
                period_end_stamp = datetime.strptime(period_end, TIME_FORMAT)
                pv50 = watts[period_end]  # efficiency already baked into kwp in the URL
                if period_start_stamp:
                    if period_end_stamp - period_start_stamp > timedelta(minutes=60):
                        period_start_stamp = None
                if period_start_stamp is None:
                    period_start_stamp = period_end_stamp.replace(minute=0, second=0, microsecond=0)  # Start at the beginning of the hour
                    if period_start_stamp == period_end_stamp:
                        period_start_stamp = period_start_stamp - timedelta(minutes=60)
                minutes_start = (period_start_stamp - self.midnight_utc).total_seconds() / 60
                minutes_end = (period_end_stamp - self.midnight_utc).total_seconds() / 60
                for minute in range(int(minutes_start), int(minutes_end) + 1):
                    forecast_watt_data[minute] = pv50
                period_start_stamp = period_end_stamp

            for minute in range(0, days_data * 24 * 60, self.plan_interval_minutes):
                pv50 = 0
                for offset in range(0, self.plan_interval_minutes, 1):
                    pv50 += dp4(forecast_watt_data.get(minute + offset, 0) / 1000.0)
                pv50 /= 60
                period_start_stamp = self.midnight_utc + timedelta(minutes=minute)
                data_item = {"period_start": period_start_stamp.strftime(TIME_FORMAT), "pv_estimate": pv50}
                if period_start_stamp in period_data:
                    period_data[period_start_stamp]["pv_estimate"] += pv50
                else:
                    period_data[period_start_stamp] = data_item

        # Merge the new data into the cached data
        new_data = {}
        for key in period_data:
            self.forecast_solar_data[key.strftime(TIME_FORMAT)] = period_data[key]

        # Prune old data from the cache
        for key_txt in self.forecast_solar_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            if key >= self.midnight_utc:
                new_data[key_txt] = self.forecast_solar_data[key_txt]
        self.forecast_solar_data = new_data

        # Save to cache
        if self.storage:
            await self.storage.save("solcast", "forecast_solar_data", self.forecast_solar_data, format="json", expiry=None)

        # Fetch the final cached data as timestamps
        period_data = {}
        for key_txt in self.forecast_solar_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            period_data[key] = self.forecast_solar_data[key_txt]

        # Sort data and return
        sorted_data = []
        if period_data:
            period_keys = list(period_data.keys())
            period_keys.sort()
            for key in period_keys:
                sorted_data.append(period_data[key])

        self.log("Forecast solar returned {} data points".format(len(sorted_data)))
        return sorted_data, max_kwh

    async def download_solcast_data(self):
        """
        Download solcast data directly from a URL or return from cache if recent.
        """
        host = self.solcast_host
        api_keys = self.solcast_api_key
        if not api_keys or not host:
            self.log("Warn: Solcast API key or host not set")
            return None

        # Remove trailing '/' from host URL if necessary to prevent pathnames becoming e.g. https://api.solcast.com.au//rooftop_sites
        if host[-1] == "/":
            host = host[0:-1]

        self.solcast_data = {}
        if self.storage:
            cached = await self.storage.load("solcast", "solcast_data")
            if isinstance(cached, dict):
                self.solcast_data = cached

        if isinstance(api_keys, str):
            api_keys = [api_keys]

        period_data = {}
        max_age = self.solcast_poll_hours * 60

        for api_key in api_keys:
            params = {"format": "json", "api_key": api_key.strip()}

            site_config = self.solcast_sites
            if site_config:
                sites = []
                for site in site_config:
                    sites.append({"resource_id": site})
            else:
                url = f"{host}/rooftop_sites"
                data = await self.cache_get_url(url, params, max_age=max_age)
                if not data:
                    self.log("Warn: Solcast sites could not be downloaded, try setting solcast_sites in apps.yaml instead")
                    continue
                sites = data.get("sites", [])

            for site in sites:
                resource_id = site.get("resource_id", None)
                if resource_id:
                    self.log("SolarAPI: Fetch data for resource id {}".format(resource_id))

                    params = {"format": "json", "api_key": api_key.strip(), "hours": 168}
                    url = f"{host}/rooftop_sites/{resource_id}/forecasts"
                    data = await self.cache_get_url(url, params, max_age=max_age)
                    if not data:
                        self.log("SolarAPI: Warn: Solcast forecast data for site {} could not be downloaded, check your Solcast cloud settings".format(site))
                        continue
                    forecasts = data.get("forecasts", [])

                    for forecast in forecasts:
                        period_end = forecast.get("period_end", None)
                        if period_end:
                            period_end_stamp = datetime.strptime(period_end, TIME_FORMAT_SOLCAST)
                            period_end_stamp.replace(tzinfo=pytz.utc)
                            period_period = forecast.get("period", "PT30M")
                            period_minutes = int(period_period[2:-1])
                            period_start_stamp = period_end_stamp - timedelta(minutes=period_minutes)
                            pv50 = forecast.get("pv_estimate", 0) / 60 * period_minutes
                            pv10 = forecast.get("pv_estimate10", forecast.get("pv_estimate", 0)) / 60 * period_minutes
                            pv90 = forecast.get("pv_estimate90", forecast.get("pv_estimate", 0)) / 60 * period_minutes

                            data_item = {"period_start": period_start_stamp.strftime(TIME_FORMAT), "pv_estimate": pv50, "pv_estimate10": pv10, "pv_estimate90": pv90}
                            if period_start_stamp in period_data:
                                period_data[period_start_stamp]["pv_estimate"] += pv50
                                period_data[period_start_stamp]["pv_estimate10"] += pv10
                                period_data[period_start_stamp]["pv_estimate90"] += pv90
                            else:
                                period_data[period_start_stamp] = data_item

        # Merge the new data into the cached data
        new_data = {}
        for key in period_data:
            self.solcast_data[key.strftime(TIME_FORMAT)] = period_data[key]

        # Prune old data from the cache
        for key_txt in self.solcast_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            if key >= self.midnight_utc:
                new_data[key_txt] = self.solcast_data[key_txt]
        self.solcast_data = new_data

        # Save to cache
        if self.storage:
            await self.storage.save("solcast", "solcast_data", self.solcast_data, format="json", expiry=None)

        # Fetch the final cached data as timestamps
        period_data = {}
        for key_txt in self.solcast_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            period_data[key] = self.solcast_data[key_txt]

        # Sort data and return
        sorted_data = []
        if period_data:
            period_keys = list(period_data.keys())
            period_keys.sort()
            for key in period_keys:
                sorted_data.append(period_data[key])

        self.log("SolarAPI: Solcast returned {} data points".format(len(sorted_data)))
        return sorted_data

    def fetch_pv_datapoints(self, argname, entity_id):
        """
        Get some solcast data from argname argument
        """
        data = []
        total_data = 0
        total_sensor = 0

        if entity_id:
            # Found out if detailedForecast is present or not, then set the attribute name
            # in newer solcast plugins only forecast is used
            attribute = "detailedForecast"
            if entity_id:
                result = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                if not result:
                    attribute = "forecast"
                try:
                    data = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                except (ValueError, TypeError):
                    self.log("Warn: Unable to fetch solar forecast data from sensor {} check your setting of {}".format(entity_id, argname))

            # Solcast new vs old version
            # check the total vs the sum of 30 minute slots and work out scale factor
            if data:
                for entry in data:
                    total_data += entry["pv_estimate"]
                total_data = dp2(total_data)
                total_sensor = self.get_state_wrapper(entity_id=entity_id, default=1.0)
                try:
                    total_sensor = dp2(float(total_sensor))
                except (ValueError, TypeError):
                    total_sensor = 1.0

        return data, total_data, total_sensor

    def publish_pv_stats(self, pv_forecast_data, divide_by, period):
        """
        Publish some PV stats
        """

        total_left_today = 0
        total_left_today10 = 0
        total_left_today90 = 0
        total_left_todayCL = 0
        forecast_day = {}
        total_day = {}
        total_day10 = {}
        total_day90 = {}
        total_dayCL = {}
        days = 0
        for day in range(7):
            total_day[day] = 0
            total_day10[day] = 0
            total_day90[day] = 0
            total_dayCL[day] = 0
            forecast_day[day] = []

        midnight_today = self.midnight_utc
        now = self.now_utc_exact

        power_scale = 60 / period  # Scale kwh to power
        power_now = 0
        power_now10 = 0
        power_now90 = 0
        power_nowCL = 0

        point_gap = period
        for entry in pv_forecast_data:
            if "period_start" not in entry:
                continue
            try:
                this_point = datetime.strptime(entry["period_start"], TIME_FORMAT)
            except (ValueError, TypeError):
                continue

            if this_point >= midnight_today:
                day = (this_point - midnight_today).days
                if day not in total_day:
                    total_day[day] = 0
                    total_day10[day] = 0
                    total_day90[day] = 0
                    total_dayCL[day] = 0
                    forecast_day[day] = []
                days = max(days, day + 1)

                pv_estimate = entry.get("pv_estimate", 0)
                pv_estimate10 = entry.get("pv_estimate10", pv_estimate)
                pv_estimate90 = entry.get("pv_estimate90", pv_estimate)
                pv_estimateCL = entry.get("pv_estimateCL", pv_estimate)

                pv_estimate /= divide_by
                pv_estimate10 /= divide_by
                pv_estimate90 /= divide_by
                pv_estimateCL /= divide_by

                total_day[day] += pv_estimate
                total_day10[day] += pv_estimate10
                total_day90[day] += pv_estimate90
                total_dayCL[day] += pv_estimateCL

                if day == 0 and this_point > now:
                    total_left_today += pv_estimate
                    total_left_today10 += pv_estimate10
                    total_left_today90 += pv_estimate90
                    total_left_todayCL += pv_estimateCL

                next_point = this_point + timedelta(minutes=point_gap)
                if this_point <= now and next_point > now:
                    power_now = pv_estimate * power_scale
                    power_now10 = pv_estimate10 * power_scale
                    power_now90 = pv_estimate90 * power_scale
                    power_nowCL = pv_estimateCL * power_scale

                    # Add this slot into the total left today but scaled for the time since this point
                    if day == 0:
                        left_this_slot_scale = (point_gap - ((now - this_point).total_seconds() / 60)) / point_gap
                        total_left_today += pv_estimate * left_this_slot_scale
                        total_left_today10 += pv_estimate10 * left_this_slot_scale
                        total_left_today90 += pv_estimate90 * left_this_slot_scale
                        total_left_todayCL += pv_estimateCL * left_this_slot_scale

                fentry = {
                    "period_start": entry["period_start"],
                    "pv_estimate": dp2(pv_estimate * power_scale),
                    "pv_estimate10": dp2(pv_estimate10 * power_scale),
                    "pv_estimate90": dp2(pv_estimate90 * power_scale),
                    "pv_estimateCL": dp2(pv_estimateCL * power_scale),
                }
                forecast_day[day].append(fentry)

        calibration_on = self.get_arg("metric_pv_calibration_enable", default=True)

        days = min(days, 7)
        for day in range(days):
            if day == 0:
                self.log(
                    "SolarAPI: PV Forecast for today is {} ({} 10%, {} 90%, {} calibrated) kWh, and PV left today is {} ({} 10%, {} 90%, {} calibrated) kWh".format(
                        dp2(total_day[day]),
                        dp2(total_day10[day]),
                        dp2(total_day90[day]),
                        dp2(total_dayCL[day]),
                        dp2(total_left_today),
                        dp2(total_left_today10),
                        dp2(total_left_today90),
                        dp2(total_left_todayCL),
                    )
                )
                self.dashboard_item(
                    "sensor." + self.prefix + "_pv_today",
                    state=dp2(total_dayCL[day] if calibration_on else total_day[day]),
                    attributes={
                        "friendly_name": "PV Forecast Today",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                        "device_class": "energy",
                        "total": dp2(total_day[day]),
                        "total10": dp2(total_day10[day]),
                        "total90": dp2(total_day90[day]),
                        "totalCL": dp2(total_dayCL[day]),
                        "remaining": dp2(total_left_today),
                        "remaining10": dp2(total_left_today10),
                        "remaining90": dp2(total_left_today90),
                        "remainingCL": dp2(total_left_todayCL),
                        "detailedForecast": forecast_day[day],
                    },
                    app="solar",
                )
                self.dashboard_item(
                    "sensor." + self.prefix + "_pv_forecast_h0",
                    state=dp2(power_nowCL if calibration_on else power_now),
                    attributes={
                        "friendly_name": "PV Forecast Now",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:solar-power",
                        "device_class": "power",
                        "now": dp2(power_now),
                        "now10": dp2(power_now10),
                        "now90": dp2(power_now90),
                        "nowCL": dp2(power_nowCL),
                        "remaining": dp2(total_left_today),
                        "remaining10": dp2(total_left_today10),
                        "remaining90": dp2(total_left_today90),
                        "remainingCL": dp2(total_left_todayCL),
                    },
                    app="solar",
                )
            else:
                day_name = "tomorrow" if day == 1 else "d{}".format(day)
                day_name_long = day_name if day == 1 else "day {}".format(day)
                self.log("SolarAPI: PV Forecast for day {} is {} ({} 10%, {} 90%, {} calibrated) kWh".format(day_name, dp2(total_day[day]), dp2(total_day10[day]), dp2(total_day90[day]), dp2(total_dayCL[day])))

                self.dashboard_item(
                    "sensor." + self.prefix + "_pv_" + day_name,
                    state=dp2(total_dayCL[day] if calibration_on else total_day[day]),
                    attributes={
                        "friendly_name": "PV Forecast " + day_name_long,
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                        "device_class": "energy",
                        "total": dp2(total_day[day]),
                        "total10": dp2(total_day10[day]),
                        "total90": dp2(total_day90[day]),
                        "totalCL": dp2(total_dayCL[day]),
                        "detailedForecast": forecast_day[day],
                    },
                    app="solar",
                )

    def pv_calibration(self, pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10, divide_by, max_kwh, forecast_days, period=None):
        """
        Perform PV calibration based on historical data and forecast data.
        This will adjust the forecast data based on historical PV production and forecast data.
        It will also create pv_estimate10 and pv_estimate90 data if create_pv10 is True.
        """
        # If no period is given, default to the plan interval (backward-compatible for unit tests).
        if period is None:
            period = self.plan_interval_minutes
        # Number of plan-interval slots that span one forecast entry period.
        # For 30-min plan slots with a 60-min forecast (Open-Meteo) this is 2,
        # for 30-min plan slots with a 30-min forecast (Solcast) this is 1.
        # Use ceiling so partial forecast periods are fully covered rather than rounded down.
        if period % self.plan_interval_minutes != 0:
            self.log("Warn: SolarAPI: PV calibration forecast period {} does not divide evenly into plan interval {} - using ceiling slot coverage".format(period, self.plan_interval_minutes))
        slots_per_period = max(1, int(math.ceil(period / self.plan_interval_minutes)))

        self.log("SolarAPI: PV Calibration: Fetching PV data for calibration")

        days = 7
        pv_today_hist = self.base.minute_data_import_export(days + 1, self.now_utc, "pv_today", required_unit="kWh", pad=False)
        pv_today_hist_max_minute = max(pv_today_hist.keys()) if pv_today_hist else 0
        pv_today_hist_days = int(pv_today_hist_max_minute / (24 * 60)) if pv_today_hist else 0
        # turn pv_today_hist into pv_power_hist by working out the increment for each minute, starting
        current_value = None
        pv_power_hist = {}
        for minute in range(pv_today_hist_max_minute - 5, -5, -5):
            current_value = pv_today_hist.get(minute, current_value)
            next_value = pv_today_hist.get(minute - 5, current_value)
            power_amount = max(0, next_value - current_value) * 60.0 / 5.0
            for sub_minute in range(1, 6):
                pv_power_hist[minute + 5 - sub_minute] = power_amount

        # Find the forecast history
        pv_forecast, pv_forecast_hist_days = history_attribute_to_minute_data(
            self.now_utc_exact, prune_today(history_attribute(self.get_history_wrapper("sensor." + self.prefix + "_pv_forecast_h0", days + 1, required=False)), self.now_utc_exact, self.midnight_utc, prune=False, intermediate=True)
        )

        hist_days = min(pv_today_hist_days, pv_forecast_hist_days, days)
        enabled_calibration = True
        if hist_days < 3:
            enabled_calibration = False
            self.log("SolarAPI: PV Calibration: Not enough historical data for calibration, only {} days of history".format(hist_days))

        pv_power_hist_by_slot = {}
        pv_power_hist_by_slot_count = {}
        pv_forecast_by_slot = {}
        pv_forecast_by_slot_count = {}
        past_day_forecast = {}
        past_day_actual = {}
        max_pv_power_hist = 0

        # Work out the history for each slot in the day, and the history for each day, and the max power in the history for scaling purposes
        for minute in pv_power_hist:
            minute_absolute = self.minutes_now - minute
            if minute_absolute < 0:
                days_prev = int(abs(minute_absolute) / (24 * 60)) + 1
                slot_abs = minute_absolute % (24 * 60)
                slot = int(slot_abs / self.plan_interval_minutes) * self.plan_interval_minutes
                pv_power_hist_by_slot[slot] = pv_power_hist_by_slot.get(slot, 0) + pv_power_hist[minute]
                pv_power_hist_by_slot_count[slot] = pv_power_hist_by_slot_count.get(slot, 0) + 1
                past_day_actual[days_prev] = past_day_actual.get(days_prev, 0) + pv_power_hist[minute]
                max_pv_power_hist = max(max_pv_power_hist, pv_power_hist[minute])

        # Average the history for each slot in the day
        for slot in pv_power_hist_by_slot:
            if pv_power_hist_by_slot_count[slot] > 0:
                pv_power_hist_by_slot[slot] = dp4(pv_power_hist_by_slot[slot] / pv_power_hist_by_slot_count[slot])

        # Work out the forecast for each slot in the day, and the forecast for each day, and the max power in the forecast for scaling purposes
        max_pv_power_forecast = 0
        for minute in pv_forecast:
            minute_absolute = self.minutes_now - minute
            if minute_absolute < 0:
                slot_abs = minute_absolute % (24 * 60)
                slot = int(slot_abs / self.plan_interval_minutes) * self.plan_interval_minutes
                pv_forecast_by_slot[slot] = pv_forecast_by_slot.get(slot, 0) + pv_forecast[minute]
                pv_forecast_by_slot_count[slot] = pv_forecast_by_slot_count.get(slot, 0) + 1
                max_pv_power_forecast = max(max_pv_power_forecast, pv_forecast[minute])
                days_prev = int(abs(minute_absolute) / (24 * 60)) + 1
                if days_prev <= hist_days:
                    past_day_forecast[days_prev] = past_day_forecast.get(days_prev, 0) + pv_forecast[minute]

        # Average the forecast for each slot in the day
        for slot in pv_forecast_by_slot:
            if pv_forecast_by_slot_count[slot] > 0:
                pv_forecast_by_slot[slot] = dp4(pv_forecast_by_slot[slot] / pv_forecast_by_slot_count[slot])

        # Work out the scaling factor for the forecast based on the history, looking at each day and each slot, and find the best and worst case day to use as a guide for scaling the forecast.
        # More recent days are weighted higher: weight = max(1.0 - 0.1 * (day - 1), 0.3)
        # so day1=1.0, day2=0.9, day3=0.8 ... day7=0.4, day8+=0.3
        worst_day_scaling = 1.0
        best_day_scaling = 1.0
        average_day_scaling = 0
        total_weight = 0.0
        for day in past_day_forecast:
            past_day_forecast[day] = dp4(past_day_forecast[day] / 60.0)  # Convert to kWh
            past_day_actual[day] = dp4(past_day_actual.get(day, 0) / 60.0)  # Convert to kWh
            scaling_factor = dp4(past_day_actual[day] / past_day_forecast[day] if past_day_forecast[day] > 0 else 1.0)
            worst_day_scaling = min(worst_day_scaling, scaling_factor)
            best_day_scaling = max(best_day_scaling, scaling_factor)
            weight = max(1.0 - 0.1 * (day - 1), 0.3)
            average_day_scaling += scaling_factor * weight
            total_weight += weight
            self.log("SolarAPI: PV Calibration: Past day {} had {} kWh of forecast PV, and actual {} kWh PV generation (weight {})".format(day, dp2(past_day_forecast[day]), dp2(past_day_actual[day]), dp2(weight)))
        average_day_scaling = dp4(average_day_scaling / total_weight) if past_day_forecast else 1.0
        average_day_scaling = min(max(average_day_scaling, 0.1), 2.0)

        # Now adjust worst and best day scaling through by average scaling so they are just a factor on the average day, and clamp to sensible values to prevent extreme outliers from causing crazy forecasts.
        worst_day_scaling = dp4(worst_day_scaling / average_day_scaling)
        best_day_scaling = dp4(best_day_scaling / average_day_scaling)

        # Clamp best and worst day scaling factors to sensible values
        worst_day_scaling = max(worst_day_scaling, 0.5)
        best_day_scaling = min(best_day_scaling, 1.7)
        if not enabled_calibration:
            worst_day_scaling = 0.7
            best_day_scaling = 1.3
        self.log(
            "SolarAPI: PV Calibration: Worst day scaling factor {}, best day scaling factor {} average day scaling factor {} max historical power {} max future predicted power {}".format(
                dp2(worst_day_scaling), dp2(best_day_scaling), dp2(average_day_scaling), dp2(max_pv_power_hist), dp2(max_pv_power_forecast)
            )
        )
        self.pv_calibration_worst_scaling = worst_day_scaling
        self.pv_calibration_best_scaling = best_day_scaling
        self.pv_calibration_average_scaling = average_day_scaling

        # Work out total production across the slot averages.
        total_production = 0
        for slot in range(0, 24 * 60, self.plan_interval_minutes):
            total_production += pv_power_hist_by_slot.get(slot, 0)

        # Work out total forecast across the slot averages.
        total_forecast = 0
        for slot in range(0, 24 * 60, self.plan_interval_minutes):
            total_forecast += pv_forecast_by_slot.get(slot, 0)

        slot_adjustment = {}
        for slot in range(0, 24 * 60, self.plan_interval_minutes):
            # Work out the per-slot scale factor
            slot_adjustment[slot] = dp4(pv_power_hist_by_slot.get(slot, 0) / pv_forecast_by_slot.get(slot, 0) if pv_forecast_by_slot.get(slot, 0) > 0.01 else 1.0)
            slot_adjustment[slot] = max(min(slot_adjustment[slot], PV_CALIBRATION_HIGHEST), PV_CALIBRATION_LOWEST)  # Clamp adjustment factor to sensible values

            # Override if we don't have enough data
            if not enabled_calibration:
                slot_adjustment[slot] = 1.0

        total_adjustment = dp4(total_production / total_forecast if total_forecast > 0 else 1.0)
        total_adjustment = max(min(total_adjustment, PV_CALIBRATION_HIGHEST), PV_CALIBRATION_LOWEST)
        if not enabled_calibration:
            total_adjustment = 1.0
        self.pv_calibration_total_adjustment = total_adjustment
        m = metrics()
        m.pv_scaling_worst.set(worst_day_scaling)
        m.pv_scaling_best.set(best_day_scaling)
        m.pv_scaling_total.set(total_adjustment)

        self.log(
            "SolarAPI: PV Calibration: PV production: {} kWh, Total forecast: {} kWh, adjustment {}x max_hist {}kW max_forecast {}kW slot adjustments {}, max_kwh {}, divide_by {}".format(
                dp2(total_production), dp2(total_forecast), total_adjustment, dp2(max_pv_power_hist), dp2(max_pv_power_forecast), slot_adjustment, max_kwh, divide_by
            )
        )

        # Work out the total forecast for each future day going forward.
        days_forecast_total = {}
        days_forecast_total_scaled_slot = {}
        for minute in range(0, max(pv_forecast_minute.keys()) + 1):
            day = int(minute / (24 * 60))
            if day < forecast_days:
                slot = (int(minute / self.plan_interval_minutes) * self.plan_interval_minutes) % (24 * 60)
                pv_value = pv_forecast_minute.get(minute, 0)
                days_forecast_total[day] = dp2(days_forecast_total.get(day, 0) + pv_value)
                days_forecast_total_scaled_slot[day] = dp2(days_forecast_total_scaled_slot.get(day, 0) + pv_value * slot_adjustment.get(slot, 1.0))

        # Decide on the per day scaling factor
        days_use_scaling = {}
        for day in days_forecast_total:
            total_day = days_forecast_total.get(day, 0)
            total_day_scaled_slot = days_forecast_total_scaled_slot.get(day, 0)
            scaling_applied = dp4(total_day_scaled_slot / total_day if total_day > 0.01 else 1.0)
            days_use_scaling[day] = dp4(total_adjustment / scaling_applied if scaling_applied > 0.01 else 1.0)

        self.log("SolarAPI: PV Calibration: Days forecast total {}, days forecast total scaled by slot adjustments {}, days use scaling {}".format(days_forecast_total, days_forecast_total_scaled_slot, days_use_scaling))

        pv_forecast_minute_adjusted = {}
        for minute in range(0, max(pv_forecast_minute.keys()) + 1):
            day = int(minute / (24 * 60))
            use_scaling_day = days_use_scaling.get(day, 1.0)
            pv_value = pv_forecast_minute.get(minute, 0)
            slot = (int(minute / self.plan_interval_minutes) * self.plan_interval_minutes) % (24 * 60)
            pv_forecast_minute_adjusted[minute] = pv_value * slot_adjustment.get(slot, 1.0) * use_scaling_day

        pv_estimateCL = {}
        pv_estimate10 = {}
        pv_estimate90 = {}
        # The after scaling cap will be applied, but remember that the input data is
        capped_data = max(max_pv_power_hist, max_pv_power_forecast) / 60 * self.plan_interval_minutes
        capped_data = min(max_kwh / 60 * self.plan_interval_minutes, capped_data)
        for minute in range(0, max(pv_forecast_minute.keys()) + 1, self.plan_interval_minutes):
            pv_value = 0
            for offset in range(0, self.plan_interval_minutes, 1):
                pv_value += pv_forecast_minute_adjusted.get(minute + offset, 0)
            # Force timezone to UTC
            pv_estimateCL[minute] = dp4(min(pv_value, capped_data))  # Clamp to max_kwh scaled to 30 minute slots
            pv_estimate10[minute] = dp4(min(pv_value * worst_day_scaling, capped_data))
            pv_estimate90[minute] = dp4(min(pv_value * best_day_scaling, capped_data))

        for entry in pv_forecast_data:
            period_start = entry.get("period_start", "")
            if period_start:
                minutes_since_midnight = (datetime.strptime(period_start, TIME_FORMAT) - self.midnight_utc).total_seconds() / 60
                slot = int(minutes_since_midnight / self.plan_interval_minutes) * self.plan_interval_minutes

                # Sum all plan-interval slots that fall within this forecast entry's period.
                # When the forecast resolution is coarser than plan_interval_minutes (e.g. 60-min
                # Open-Meteo entries with 30-min plan slots) we must accumulate multiple slots so
                # that the annotated value covers the full entry duration, not just the first half.
                calibrated = 0
                calibrated10 = 0
                calibrated90 = 0
                has_calibrated = False
                has_calibrated10 = False
                has_calibrated90 = False
                for i in range(slots_per_period):
                    s = slot + i * self.plan_interval_minutes
                    v = pv_estimateCL.get(s, None)
                    if v is not None:
                        calibrated += v
                        has_calibrated = True
                    v10 = pv_estimate10.get(s, None)
                    if v10 is not None:
                        calibrated10 += v10
                        has_calibrated10 = True
                    v90 = pv_estimate90.get(s, None)
                    if v90 is not None:
                        calibrated90 += v90
                        has_calibrated90 = True

                # When we store the data we have to reverse the divide_by factor
                if has_calibrated:
                    entry["pv_estimateCL"] = calibrated * divide_by
                if create_pv10 and has_calibrated10:
                    entry["pv_estimate10"] = calibrated10 * divide_by
                if create_pv10 and has_calibrated90:
                    entry["pv_estimate90"] = calibrated90 * divide_by

        # Creation of PV10 data using worst day scaling factor
        if create_pv10:
            for minute in range(0, max(pv_forecast_minute_adjusted.keys()) + 1):
                pv_value = pv_forecast_minute_adjusted.get(minute, 0)
                # Use the worst day scaling factor to create pv_estimate10
                pv_forecast_minute10[minute] = dp4(pv_value * worst_day_scaling)
            self.log("SolarAPI: PV Calibration: Created pv_estimate10/pv_estimate90 data using worst day scaling factor {}".format(dp2(worst_day_scaling)))

        # Do we use calibrated or raw data?
        if self.get_arg("metric_pv_calibration_enable", default=True):
            self.log("SolarAPI: PV Calibration: Using calibrated PV data")
            return pv_forecast_minute_adjusted, pv_forecast_minute10, pv_forecast_data
        else:
            return pv_forecast_minute, pv_forecast_minute10, pv_forecast_data

    def pack_and_store_forecast(self, pv_forecast_minute, pv_forecast_minute10):
        pv_forecast_pack = {}
        pv_forecast_pack10 = {}

        prev_value = -1
        prev_value10 = -1

        for minute in range(0, self.forecast_days * 24 * 60):
            current_value = dp4(pv_forecast_minute.get(minute, 0))
            current_value10 = dp4(pv_forecast_minute10.get(minute, 0))
            if current_value != prev_value:
                pv_forecast_pack[minute] = current_value
                prev_value = current_value
            if current_value10 != prev_value10:
                pv_forecast_pack10[minute] = current_value10
                prev_value10 = current_value10

        current_pv_power = dp4(pv_forecast_minute.get(self.minutes_now, 0))

        self.dashboard_item(
            "sensor." + self.prefix + "_pv_forecast_raw",
            state=current_pv_power,
            attributes={
                "friendly_name": "PV Forecast minute data",
                "icon": "mdi:solar-power",
                "relative_time": self.midnight_utc.strftime(TIME_FORMAT),
                "forecast": pv_forecast_pack,
                "forecast10": pv_forecast_pack10,
                "unit_of_measurement": "kW",
                "device_class": "power",
                "state_class": "measurement",
            },
            app="solar",
        )

    async def fetch_pv_forecast(self):
        """
        Fetch the PV Forecast data from Solcast
        either via HA or direct to their cloud
        """
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        pv_forecast_data = []
        pv_forecast_total_data = 0
        pv_forecast_total_sensor = 0
        create_pv10 = False
        max_kwh = 9999
        using_ha_data = False

        if self.forecast_solar:
            self.log("SolarAPI: Obtaining solar forecast from Forecast Solar API")
            pv_forecast_data, max_kwh = await self.download_forecast_solar_data()
            divide_by = 30.0
            create_pv10 = True
        elif self.open_meteo_forecast:
            self.log("SolarAPI: Obtaining solar forecast from Open-Meteo API")
            pv_forecast_data, max_kwh = await self.download_open_meteo_data()
            divide_by = 30.0
            create_pv10 = True
        elif self.solcast_host and self.solcast_api_key:
            self.log("SolarAPI: Obtaining solar forecast from Solcast API")
            pv_forecast_data = await self.download_solcast_data()
            divide_by = 30.0
        else:
            self.log("SolarAPI: Using Solcast integration from inside HA for solar forecast")
            using_ha_data = True

            # Fetch data from each sensor
            for argname in ["pv_forecast_today", "pv_forecast_tomorrow", "pv_forecast_d3", "pv_forecast_d4"]:
                # We have to re-get the arg here as the regexp wouldn't be resolved earlier
                entity_id = getattr(self, argname, None)

                data, total_data, total_sensor = self.fetch_pv_datapoints(argname, entity_id)
                if data:
                    self.log("SolarAPI: PV Data for {} total {} kWh".format(argname, total_sensor))
                    pv_forecast_data += data

                    if argname == "pv_forecast_today":
                        pv_forecast_total_data += total_data
                        pv_forecast_total_sensor += total_sensor

            # Work out data scale factor so it adds up (New Solcast is in kW but old was kWH)
            factor = 1.0
            if pv_forecast_total_data > 0.0 and pv_forecast_total_sensor > 0.0:
                factor = round((pv_forecast_total_data / pv_forecast_total_sensor), 1)
            # We want to divide the data into single minute slots
            divide_by = dp2(30 * factor)

            # Valid factor values: 1.0 = kWh per slot (any interval), 2.0 = kW per 30-min slot, 4.0 = kW per 15-min slot
            if factor not in [1.0, 2.0, 4.0]:
                self.log("Warn: SolarAPI: PV Forecast today adds up to {} kWh, but total sensors add up to {} kWh, this is unexpected and hence data maybe misleading (factor {})".format(pv_forecast_total_data, pv_forecast_total_sensor, factor))
            else:
                self.log("SolarAPI: PV Forecast today adds up to {} kWh, and total sensors add up to {} kWh, factor is {}".format(pv_forecast_total_data, pv_forecast_total_sensor, factor))

        if pv_forecast_data:
            # Detect the actual period of the forecast data (e.g. 15 or 30 minutes)
            # by examining the time difference between consecutive entries.
            # This ensures 15-minute resolution data is handled correctly.
            period = 30  # Default period in minutes
            if len(pv_forecast_data) >= 2:
                try:
                    t0 = datetime.strptime(pv_forecast_data[0]["period_start"], TIME_FORMAT)
                    t1 = datetime.strptime(pv_forecast_data[1]["period_start"], TIME_FORMAT)
                    detected_period = int(abs((t1 - t0).total_seconds() / 60))
                    # Sanity-check: only accept periods in the plausible range for forecast data.
                    # Values outside 5–60 minutes (e.g. 1440 if the first two entries span a day
                    # boundary when multiple sensor days are concatenated) are treated as invalid.
                    if 5 <= detected_period <= 60:
                        period = detected_period
                except (ValueError, TypeError, KeyError):
                    pass

            # For the HA sensor path the divide_by was computed assuming 30-minute periods;
            # recalculate it using the actual detected period so that the per-minute kWh
            # values are correctly scaled regardless of the forecast resolution.
            if not self.forecast_solar and not (self.solcast_host and self.solcast_api_key):
                factor = divide_by / 30.0
                divide_by = dp2(period * factor)

            if period != 30:
                self.log("SolarAPI: PV Forecast data has {} minute resolution, adjusting calculations".format(period))

            pv_forecast_minute, _ = minute_data(
                pv_forecast_data,
                self.forecast_days,
                self.midnight_utc,
                "pv_estimate",
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=period,
            )
            pv_forecast_minute10, _ = minute_data(
                pv_forecast_data,
                self.forecast_days,
                self.midnight_utc,
                "pv_estimate10",
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=period,
            )

            # Run calibration on the data
            pv_forecast_minute, pv_forecast_minute10, pv_forecast_data = self.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10, divide_by / period, max_kwh, self.forecast_days, period)
            self.publish_pv_stats(pv_forecast_data, divide_by / period, period)
            self.pack_and_store_forecast(pv_forecast_minute, pv_forecast_minute10)
            self.update_success_timestamp()
            self.last_fetched_timestamp = self.now_utc_exact
        else:
            if using_ha_data:
                self.log("Warn: SolarAPI: No solar forecast data was returned from HA sensors.")
            else:
                self.log("Warn: SolarAPI: No solar data was returned.")
            self.last_fetched_timestamp = self.now_utc_exact

"""Future energy rate prediction from Nord Pool and similar APIs.

Fetches wholesale energy prices and calibrates them against actual retail
tariff rates to predict future import/export prices beyond the current
Octopus tariff period.
"""

from datetime import datetime, timedelta
import hashlib
import requests
import json
import copy

from const import TIME_FORMAT
from ha import run_async
from utils import dp1, dp2, minute_data, strptime_aware

TIME_FORMAT_NORD = "%d-%m-%YT%H:%M:%S%z"

# AgilePredict (https://agilepredict.com) forecasts Octopus Agile prices per region for up to 14 days
AGILEPREDICT_DEFAULT_URL = "https://prices.fly.dev/api/"
AGILEPREDICT_REGIONS = "ABCDEFGHJKLMNP"
# AgilePredict publishes new forecasts four times a day, so a 30 minute cache is plenty fresh
AGILEPREDICT_CACHE_MINUTES = 30
# Always ask for the same span so that every user of a shared cache asks for the same URL
AGILEPREDICT_DAYS = 5


class FutureRate:
    """Future energy rate prediction from Nord Pool wholesale prices.

    Fetches wholesale prices and calibrates them against actual retail
    rates to predict import/export prices beyond the current tariff period.
    """

    def __init__(self, base):
        self.base = base
        self.record_status = base.record_status
        self.plan_interval_minutes = base.plan_interval_minutes
        self.log = base.log
        self.get_arg = base.get_arg
        self.set_arg = base.set_arg
        self.midnight_utc = base.midnight_utc
        self.forecast_days = base.forecast_days
        self.minutes_now = base.minutes_now
        self.forecast_plan_hours = base.forecast_plan_hours
        self.time_abs_str = base.time_abs_str
        self.get_state_wrapper = base.get_state_wrapper

        self.storage = base.components.get_component("storage") if base.components else None
        self.futurerate_url_cache = {}
        if self.storage:
            cached = run_async(self.storage.load("futurerate", "url_cache"))
            if isinstance(cached, dict):
                self.futurerate_url_cache = cached

        futurerate_adjust_auto = self.get_arg("futurerate_adjust_auto", False)
        if futurerate_adjust_auto:
            import_agile, export_agile = self.import_export_is_agile()
            # Change settings based on auto
            self.set_arg("futurerate_adjust_import", import_agile)
            self.set_arg("futurerate_adjust_export", export_agile)

    def futurerate_calibrate(self, real_mdata, mdata, is_import, peak_start_minutes, peak_end_minutes):
        """
        Calibrate nordpool data
        """
        if is_import:
            best_diff_multiply = 2.2
            best_diff_add_peak = 12.0
            best_diff_add_all = 0
            mult_range = [x / 100 for x in range(180, 240, 2)]
        else:
            best_diff_multiply = 0.95
            best_diff_add_peak = 1.0
            best_diff_add_all = 5.0
            mult_range = [x / 100 for x in range(90, 120, 2)]
        vat = 1.05

        best_diff_diff = 9999999
        if real_mdata:
            for correlate_multiply in mult_range:
                diff = 0
                for minute in real_mdata:
                    minute_mod = minute % (24 * 60)
                    rate_real = real_mdata[minute]
                    rate_nord = mdata.get(minute, None)
                    if minute_mod >= peak_start_minutes and minute_mod < peak_end_minutes:
                        continue

                    if rate_nord is not None:
                        rate_nord *= correlate_multiply

                        if is_import:
                            rate_nord = min(rate_nord, 95)  # Cap
                        else:
                            rate_nord = max(rate_nord, 0)

                        diff += abs(rate_real / vat - rate_nord)

                if diff <= best_diff_diff:
                    best_diff_diff = diff
                    best_diff_multiply = correlate_multiply

        all_range = [x / 10.0 for x in range(-10, 20, 2)]
        peak_range = [x / 10.0 for x in range(0, 160, 5)]

        if real_mdata:
            best_diff_diff = 9999999
            for correlate_add in all_range:
                diff = 0
                for minute in real_mdata:
                    minute_mod = minute % (24 * 60)
                    rate_real = real_mdata[minute]
                    rate_nord = mdata.get(minute, None)

                    if rate_nord is not None:
                        rate_nord *= best_diff_multiply
                        rate_nord += correlate_add

                        if minute_mod >= peak_start_minutes and minute_mod < peak_end_minutes:
                            continue

                        if is_import:
                            rate_nord = min(rate_nord, 95)  # Cap
                        else:
                            rate_nord = max(rate_nord, 0)

                        diff += abs(rate_real / vat - rate_nord)

                if diff <= best_diff_diff:
                    best_diff_diff = diff
                    best_diff_add_all = correlate_add

            best_diff_diff = 9999999
            for correlate_add in peak_range:
                diff = 0
                for minute in real_mdata:
                    minute_mod = minute % (24 * 60)
                    rate_real = real_mdata[minute]
                    rate_nord = mdata.get(minute, None)
                    if rate_nord is not None:
                        rate_nord *= best_diff_multiply
                        rate_nord += best_diff_add_all
                        if minute_mod >= peak_start_minutes and minute_mod < peak_end_minutes:
                            rate_nord += correlate_add

                        if is_import:
                            rate_nord = min(rate_nord, 95)  # Cap
                        else:
                            rate_nord = max(rate_nord, 0)

                        diff += abs(rate_real / vat - rate_nord)

                if diff <= best_diff_diff:
                    best_diff_diff = diff
                    best_diff_add_peak = correlate_add

        # Print calibration results
        # self.log("Calibration for {} best diff {} multiply {} add_peak {} add_all {} ".format("import" if is_import else "export", best_diff_diff, best_diff_multiply, best_diff_add_peak, best_diff_add_all))

        # Perform adjustment
        calibrated_data = {}
        for minute in mdata:
            minute_mod = minute % (24 * 60)
            rate_nord = mdata[minute]
            rate_nord *= best_diff_multiply
            if minute_mod >= peak_start_minutes and minute_mod < peak_end_minutes:
                rate_nord += best_diff_add_peak
            rate_nord += best_diff_add_all
            if is_import:
                rate_nord = min(rate_nord, 95)  # Cap
            else:
                rate_nord = max(rate_nord, 0)

            calibrated_data[minute] = dp2(rate_nord * vat)

        return calibrated_data

    def import_export_is_agile(self):
        # Figure out if we have Agile on import or export before deciding which to adjust
        import_tariff = None
        import_agile = False
        import_entity = self.get_arg("metric_octopus_import", default=None, indirect=False)
        if import_entity:
            import_tariff = self.get_state_wrapper(import_entity, attribute="tariff")
            if not import_tariff:
                import_tariff = self.get_state_wrapper(import_entity, attribute="tariff_code")
        if import_tariff and "agile" in import_tariff.lower():
            import_agile = True

        export_tariff = None
        export_agile = False
        export_entity = self.get_arg("metric_octopus_export", default=None, indirect=False)
        if export_entity:
            export_tariff = self.get_state_wrapper(export_entity, attribute="tariff")
            if not export_tariff:
                export_tariff = self.get_state_wrapper(export_entity, attribute="tariff_code")
        if export_tariff and "agile" in export_tariff.lower():
            export_agile = True
        self.log("FutureRate: Detected import tariff {} agile {} export tariff {} agile {}".format(import_tariff, import_agile, export_tariff, export_agile))
        return import_agile, export_agile

    def futurerate_analysis_new(self, url_template, rate_import_real, rate_export_real):
        """
        Convert new Futurerate data to minute data
        """
        all_data = {}
        extracted_data = {}
        extracted_keys = []
        array_values = []

        peak_start = datetime.strptime(self.get_arg("futurerate_peak_start", "00:00:00"), "%H:%M:%S")
        peak_end = datetime.strptime(self.get_arg("futurerate_peak_end", "00:00:00"), "%H:%M:%S")
        peak_start_minutes = peak_start.minute + peak_start.hour * 60
        peak_end_minutes = peak_end.minute + peak_end.hour * 60
        if peak_end_minutes < peak_start_minutes:
            peak_end_minutes += 24 * 60

        for day in [0, 1]:
            url = url_template.replace("DATE", (datetime.now() + timedelta(days=day)).strftime("%Y-%m-%d"))
            try:
                pdata = self.download_futurerate_data(url)
            except (ValueError, TypeError):
                return {}, {}
            if not pdata and day == 0:
                self.log("Warn: Error downloading futurerate data from URL {}, no data".format(url))
                self.record_status("Warn: Error downloading futurerate data from cloud, no data", debug=url, had_errors=True)
                return {}, {}

            if not all_data:
                all_data = copy.deepcopy(pdata)
            else:
                if "multiAreaEntries" in pdata:
                    all_data["multiAreaEntries"].extend(pdata["multiAreaEntries"])

        if not all_data or ("multiAreaEntries" not in all_data):
            self.log("Warn: Error downloading futurerate data from URL {}, no multiAreaEntries".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud, no multiAreaEntries", debug=url, had_errors=True)
            return {}, {}

        prev_time_date_end = None
        prev_rate_import = 0
        prev_rate_export = 0

        for entry in all_data["multiAreaEntries"]:
            deliveryStart = entry["deliveryStart"]
            deliveryEnd = entry["deliveryEnd"]
            entryPerArea = entry["entryPerArea"]
            areaPrice = 0
            for area in entryPerArea:
                areaPrice = entryPerArea[area]

            time_date_start = strptime_aware(deliveryStart, TIME_FORMAT)
            time_date_end = strptime_aware(deliveryEnd, TIME_FORMAT)
            if time_date_end < time_date_start:
                time_date_end += timedelta(days=1)
            delta_start = time_date_start - self.midnight_utc
            delta_end = time_date_end - self.midnight_utc

            minutes_start = delta_start.seconds / 60
            minutes_end = delta_end.seconds / 60
            if minutes_end < minutes_start:
                minutes_end += 24 * 60

            # Convert to pence with Agile formula, starts in pounds per Megawatt hour
            rate_import = areaPrice / 10
            rate_export = areaPrice / 10

            item = {}
            item["from"] = time_date_start.strftime(TIME_FORMAT)
            item["to"] = time_date_end.strftime(TIME_FORMAT)
            item["rate_import"] = dp2(rate_import)
            item["rate_export"] = dp2(rate_export)

            # Create intermediate 30 minute data points
            if prev_time_date_end == time_date_start and (minutes_end - minutes_start) == 60:
                time_end_intermediate = time_date_start + timedelta(minutes=self.plan_interval_minutes)
                item["to"] = time_end_intermediate.strftime(TIME_FORMAT)
                item["rate_import"] = dp2((rate_import + prev_rate_import) / 2)
                item["rate_export"] = dp2((rate_export + prev_rate_export) / 2)
                if time_date_start not in extracted_keys:
                    extracted_keys.append(time_date_start)
                    extracted_data[time_date_start] = item

                item_intermediate = {}
                item_intermediate["from"] = time_end_intermediate.strftime(TIME_FORMAT)
                item_intermediate["to"] = time_date_end.strftime(TIME_FORMAT)
                item_intermediate["rate_import"] = dp2(rate_import)
                item_intermediate["rate_export"] = dp2(rate_export)
                if time_end_intermediate not in extracted_keys:
                    extracted_keys.append(time_end_intermediate)
                    extracted_data[time_end_intermediate] = item_intermediate
            else:
                if time_date_start not in extracted_keys:
                    extracted_keys.append(time_date_start)
                    extracted_data[time_date_start] = item

            prev_time_date_end = time_date_end
            prev_rate_import = rate_import
            prev_rate_export = rate_export

        if extracted_keys:
            extracted_keys.sort()
            for key in extracted_keys:
                array_values.append(extracted_data[key])
            self.log("Loaded {} datapoints of futurerate analysis".format(len(extracted_keys)))
            mdata_import, _ = minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_import", "from", backwards=False, to_key="to")
            mdata_export, _ = minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_export", "from", backwards=False, to_key="to")

        adjust_import = self.get_arg("futurerate_adjust_import", False)
        adjust_export = self.get_arg("futurerate_adjust_export", False)
        mdata_import = self.futurerate_calibrate(rate_import_real if adjust_import else {}, mdata_import, is_import=True, peak_start_minutes=peak_start_minutes, peak_end_minutes=peak_end_minutes)
        mdata_export = self.futurerate_calibrate(rate_export_real if adjust_export else {}, mdata_export, is_import=False, peak_start_minutes=peak_start_minutes, peak_end_minutes=peak_end_minutes)

        future_data = []
        minute_now_hour = int(self.minutes_now / 60) * 60
        for minute in range(0, self.forecast_plan_hours * 60 + minute_now_hour, 60):
            if mdata_import.get(minute) or mdata_export.get(minute):
                future_data.append("{} => {} / {}".format(self.time_abs_str(minute), mdata_import.get(minute), mdata_export.get(minute)))

        self.log("Predicted future rates: {}".format(future_data))
        return mdata_import, mdata_export

    def futurerate_analysis(self, rate_import_real, rate_export_real):
        """
        Analyse futurerate energy data

        Nord Pool day-ahead prices (futurerate_url) are used where they exist, and the AgilePredict
        forecast (futurerate_agilepredict) fills the minutes beyond them.
        """
        mdata_import, mdata_export = self.futurerate_analysis_nordpool(rate_import_real, rate_export_real)

        if self.get_arg("futurerate_agilepredict", False):
            ap_import, ap_export = self.agilepredict_analysis()
            for minute, rate in ap_import.items():
                mdata_import.setdefault(minute, rate)
            for minute, rate in ap_export.items():
                mdata_export.setdefault(minute, rate)

        return mdata_import, mdata_export

    def futurerate_analysis_nordpool(self, rate_import_real, rate_export_real):
        """
        Analyse Nord Pool futurerate data from futurerate_url
        """
        url = None
        if "futurerate_url" in self.base.args:
            url = self.base.args["futurerate_url"]
        if not url:
            return {}, {}

        if "DATE" in url:
            import_agile = self.get_arg("futurerate_adjust_import", False)
            export_agile = self.get_arg("futurerate_adjust_export", False)
            if not import_agile and not export_agile:
                self.log("FutureRate: No futurerate adjustment enabled, skipping futurerate analysis")
                return {}, {}
            self.log("Fetching futurerate data from {}".format(url))
            return self.futurerate_analysis_new(url, rate_import_real, rate_export_real)
        else:
            print("Warning: Old futurerate URL, you must update this in apps.yaml")
            return {}, {}

    @staticmethod
    def agilepredict_region_from_tariff(tariff_code):
        """
        Return the Octopus region letter at the end of a tariff code (e.g. E-1R-AGILE-24-10-01-G), or None
        """
        if not tariff_code or not isinstance(tariff_code, str):
            return None
        region = tariff_code.strip().split("-")[-1].upper()
        if len(region) == 1 and region in AGILEPREDICT_REGIONS:
            return region
        return None

    def agilepredict_region(self):
        """
        The Octopus region to fetch the AgilePredict forecast for: futurerate_agilepredict_region if set,
        else the region of the configured Octopus import (then export) tariff
        """
        region = self.get_arg("futurerate_agilepredict_region", None, indirect=False)
        if region:
            region = str(region).strip().upper()
            if region in AGILEPREDICT_REGIONS and len(region) == 1:
                return region
            self.log("Warn: FutureRate: futurerate_agilepredict_region {} is not a valid Octopus region".format(region))
            return None

        for arg in ["metric_octopus_import", "metric_octopus_export"]:
            entity = self.get_arg(arg, default=None, indirect=False)
            if not entity:
                continue
            tariff_code = self.get_state_wrapper(entity, attribute="tariff_code")
            if not tariff_code:
                tariff_code = self.get_state_wrapper(entity, attribute="tariff")
            region = self.agilepredict_region_from_tariff(tariff_code)
            if region:
                return region
        return None

    def agilepredict_analysis(self):
        """
        Fetch the AgilePredict forecast and convert it to minute data (p/kWh, minutes from midnight UTC)

        The forecast is already in retail Agile prices, so unlike Nord Pool data it is not calibrated.
        """
        adjust_import = self.get_arg("futurerate_adjust_import", False)
        adjust_export = self.get_arg("futurerate_adjust_export", False)
        if not adjust_import and not adjust_export:
            self.log("FutureRate: No futurerate adjustment enabled, skipping AgilePredict")
            return {}, {}

        region = self.agilepredict_region()
        if not region:
            self.log("Warn: FutureRate: Unable to work out the Octopus region for AgilePredict, set futurerate_agilepredict_region")
            return {}, {}

        base_url = str(self.get_arg("futurerate_agilepredict_url", AGILEPREDICT_DEFAULT_URL, indirect=False))
        if not base_url.endswith("/"):
            base_url += "/"
        url = "{}{}/?days={}&high_low=false".format(base_url, region, AGILEPREDICT_DAYS)

        mdata_import = {}
        mdata_export = {}
        if adjust_import:
            mdata_import = self.agilepredict_to_minutes(self.download_agilepredict_data(url))
        if adjust_export:
            mdata_export = self.agilepredict_to_minutes(self.download_agilepredict_data(url + "&export=true"))

        self.log("FutureRate: AgilePredict region {} gave {} import and {} export minutes".format(region, len(mdata_import), len(mdata_export)))
        return mdata_import, mdata_export

    def agilepredict_to_minutes(self, pdata):
        """
        Convert an AgilePredict API response (a list of forecasts, newest first) to minute data
        """
        mdata = {}
        if not pdata or not isinstance(pdata, list) or not isinstance(pdata[0], dict):
            return mdata
        prices = pdata[0].get("prices", [])
        if not isinstance(prices, list):
            return mdata

        points = []
        for item in prices:
            try:
                stamp = datetime.fromisoformat(str(item["date_time"]).replace("Z", "+00:00"))
                rate = float(item["agile_pred"])
            except (KeyError, TypeError, ValueError):
                continue
            if stamp.tzinfo is None:
                continue
            points.append((int((stamp - self.midnight_utc).total_seconds() / 60), rate))
        points.sort()

        for index, (minute_start, rate) in enumerate(points):
            minute_end = minute_start + 30
            if index + 1 < len(points):
                minute_end = min(points[index + 1][0], minute_end)
            for minute in range(minute_start, minute_end):
                mdata[minute] = dp2(rate)
        return mdata

    def download_agilepredict_data(self, url):
        """
        Download AgilePredict data, through the storage cache when there is one so that instances
        sharing a cache fetch each URL once per refresh rather than once each
        """
        if not self.storage or not hasattr(self.storage, "fetch_cached"):
            return self.download_agilepredict_data_func(url)

        async def _download():
            return self.download_agilepredict_data_func(url)

        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        try:
            return run_async(self.storage.fetch_cached("agilepredict", url_hash, _download, fresh_minutes=AGILEPREDICT_CACHE_MINUTES, stale_minutes=AGILEPREDICT_CACHE_MINUTES + 5, format="json"))
        except Exception as e:  # pylint: disable=broad-except
            self.log("Warn: FutureRate: AgilePredict cache error {}".format(e))
            return None

    def download_agilepredict_data_func(self, url):
        """
        Fetch AgilePredict data from the API, returning None on failure

        Failures are only logged, not recorded as errors: the forecast is optional and without it
        the plan falls back to copying the previous day's rates.
        """
        try:
            r = requests.get(url, timeout=60)
        except Exception as e:  # pylint: disable=broad-except
            self.log("Warn: Error downloading AgilePredict data from URL {}, request exception {}".format(url, e))
            return None

        if r.status_code not in [200]:
            self.log("Warn: Error downloading AgilePredict data from URL {}, code {}".format(url, r.status_code))
            return None

        try:
            struct = r.json()
        except ValueError:
            self.log("Warn: Error decoding AgilePredict data from URL {}".format(url))
            return None

        if not isinstance(struct, list) or not struct:
            self.log("Warn: AgilePredict data from URL {} has no forecasts".format(url))
            return None
        return struct

    def clean_futurerate_cache(self):
        """
        Clean up futurerate data
        """
        # The host's naive clock, matching how the stamps below are written - and how they were
        # written by earlier versions, whose cache is loaded back from storage on upgrade.
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        current_keys = list(self.futurerate_url_cache.keys())
        for url in current_keys[:]:
            stamp = self.futurerate_url_cache[url]["stamp"]
            if stamp < midnight:
                del self.futurerate_url_cache[url]

    def download_futurerate_data(self, url):
        """
        Download futurerate data directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        self.clean_futurerate_cache()

        # Check the cache first
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if url in self.futurerate_url_cache:
            stamp = self.futurerate_url_cache[url]["stamp"]
            pdata = self.futurerate_url_cache[url]["data"]
            update_time_since_midnight = stamp - midnight
            now_since_midnight = now - midnight
            age = now - stamp
            needs_update = False

            # Update if last data was yesterday
            if update_time_since_midnight.seconds < 0:
                needs_update = True

            # data updates at 11am CET so update every 30 minutes during this period
            if now_since_midnight.seconds > (9.5 * 60 * 60) and now_since_midnight.seconds < (11 * 60 * 60) and age.seconds > (0.5 * 60 * 60):
                needs_update = True
            if age.seconds > (12 * 60 * 60):
                needs_update = True

            if not needs_update:
                self.log("Return cached futurerate data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for _retry in range(3):
            pdata = self.download_futurerate_data_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("Warn: Error downloading futurerate data from URL {}".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            if url in self.futurerate_url_cache:
                pdata = self.futurerate_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        if pdata == "empty":
            pdata = {}

        # Cache new futurerate data and persist to storage
        self.futurerate_url_cache[url] = {}
        self.futurerate_url_cache[url]["stamp"] = now
        self.futurerate_url_cache[url]["data"] = pdata
        if self.storage:
            run_async(self.storage.save("futurerate", "url_cache", self.futurerate_url_cache, format="yaml", expiry=None))
        return pdata

    def download_futurerate_data_func(self, url):
        try:
            r = requests.get(url, timeout=120)
        except Exception as e:
            self.log("Warn: Error downloading futurerate data from URL {}, request exception {}".format(url, e))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}

        if r.status_code in [204]:
            return "empty"

        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading futurerate data from URL {}, code {}".format(url, r.status_code))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}

        try:
            struct = json.loads(r.text)
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Error downloading futurerate data from URL {}".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}

        return struct

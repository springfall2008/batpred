# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""Historical tariff resolution for the annual prediction tool.

Resolves import and export rates for a specific past date, either from an
Octopus product URL using period_from/period_to, or from a static basic rates
structure. Fetching is done a month at a time and sliced per sampled day.
"""

import calendar
from datetime import datetime, timedelta

import aiohttp
import pytz

from utils import minute_data

MINUTES_PER_DAY = 24 * 60


def build_period_url(base_url, start_utc, end_utc):
    """Append period_from/period_to to an Octopus rates URL, preserving existing query parameters."""
    separator = "&" if "?" in base_url else "?"
    return "{}{}period_from={}&period_to={}&page_size=1500".format(base_url, separator, start_utc.strftime("%Y-%m-%dT%H:%MZ"), end_utc.strftime("%Y-%m-%dT%H:%MZ"))


class AnnualTariff:
    """Import and export rates for arbitrary historical dates."""

    def __init__(self, config, log, predbat, storage=None, fetch_json=None):
        """Configure the tariff from the annual config's ``tariff`` block.

        Octopus product codes are region-suffixed. ``resolve_arg`` substitutes
        ``{dno_region}`` from ``predbat.args``, so the region is injected there
        first. Without it a URL silently 404s and the month is reported
        unavailable, which looks like an outage rather than a config mistake.
        """
        self.config = config
        self.log = log
        self.predbat = predbat
        self.storage = storage
        self.fetch_json = fetch_json or self._default_fetch_json
        if config.get("dno_region"):
            predbat.args["dno_region"] = config["dno_region"]
        self.import_url = self._resolve_url(config.get("import_octopus_url"), "import_octopus_url")
        self.export_url = self._resolve_url(config.get("export_octopus_url"), "export_octopus_url")
        self.basic_import = config.get("rates_import")
        self.basic_export = config.get("rates_export")
        self.standing_charge_p_per_day = float(config.get("standing_charge_p_per_day", 0.0))
        # Keyed by (year, month); each value is a dict of tz-aware UTC stamp to rate
        self.import_rates = {}
        self.export_rates = {}
        self.available = set()

    def _resolve_url(self, url, name):
        """Substitute templated arguments such as {dno_region} into a tariff URL."""
        if not url:
            return None
        return self.predbat.resolve_arg(name, url, indirect=False)

    async def _default_fetch_json(self, url):
        """Download and decode one JSON document, returning None on any failure."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers={"accept": "application/json", "user-agent": "predbat/1.0"}, timeout=aiohttp.ClientTimeout(total=60)) as response:
                    if response.status not in [200, 201]:
                        self.log("Warn: Annual: Octopus rate request to {} returned {}".format(url, response.status))
                        return None
                    return await response.json()
        except (aiohttp.ClientError, ValueError, TimeoutError) as error:
            self.log("Warn: Annual: Octopus rate request to {} failed: {}".format(url, error))
            return None

    async def _download_octopus(self, base_url, start_utc, end_utc, cache_key):
        """Download every page of Octopus rates for a date range, returning raw result rows."""
        if self.storage:
            cached = await self.storage.load("annual", cache_key)
            if isinstance(cached, list) and cached:
                return cached

        url = build_period_url(base_url, start_utc, end_utc)
        rows = []
        pages = 0
        truncated = False
        while url and pages < 10:
            data = await self.fetch_json(url)
            if not data or "results" not in data:
                # A failed page means we do not know what we are missing. Caching a
                # partial month would permanently pin wrong rates for that month.
                self.log("Warn: Annual: rate download for {} stopped early at page {}; not caching a partial result".format(cache_key, pages))
                truncated = True
                break
            rows += data["results"]
            url = data.get("next", None)
            pages += 1

        if truncated:
            return []
        if rows and self.storage:
            await self.storage.save("annual", cache_key, rows, format="json")
        return rows

    @staticmethod
    def _rows_to_stamped_rates(rows, start_utc, days):
        """Convert Octopus rate rows into a dict of tz-aware UTC stamp to rate.

        Reuses ``minute_data`` exactly as ``octopus.py`` does, then re-keys the
        minute offsets back onto absolute timestamps so a single monthly download
        can be sliced for any day within it.
        """
        parsed, _ = minute_data(rows, days + 1, start_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return {start_utc + timedelta(minutes=minute): rate for minute, rate in parsed.items()}

    async def fetch_month(self, year, month):
        """Fetch (or synthesise) the rates covering one calendar month plus a one day buffer.

        Returns True when usable rates exist for the month. The buffer day lets the
        last sampled day of the month complete its 48 hour plan.
        """
        key = (year, month)
        if self.import_url or self.export_url:
            days_in_month = calendar.monthrange(year, month)[1]
            start_utc = pytz.utc.localize(datetime(year, month, 1))
            end_utc = start_utc + timedelta(days=days_in_month + 2)
            days = days_in_month + 2

            import_rates = {}
            export_rates = {}
            if self.import_url:
                rows = await self._download_octopus(self.import_url, start_utc, end_utc, "rates_import_{}_{:02d}".format(year, month))
                if not rows:
                    self.log("Warn: Annual: no import rates available for {}-{:02d}".format(year, month))
                    return False
                import_rates = self._rows_to_stamped_rates(rows, start_utc, days)
            if self.export_url:
                rows = await self._download_octopus(self.export_url, start_utc, end_utc, "rates_export_{}_{:02d}".format(year, month))
                if rows:
                    export_rates = self._rows_to_stamped_rates(rows, start_utc, days)
                else:
                    self.log("Warn: Annual: no export rates available for {}-{:02d}, treating export as unpaid".format(year, month))

            if not import_rates:
                return False
            self.import_rates[key] = import_rates
            self.export_rates[key] = export_rates
            self.available.add(key)
            return True

        # Basic rates repeat a fixed daily pattern, so nothing needs downloading
        self.available.add(key)
        return True

    def month_available(self, year, month):
        """Return True when usable rates exist for the given month."""
        return (year, month) in self.available

    def _basic_window(self, info, name, minutes):
        """Expand a basic rates structure across the requested window."""
        rates = self.predbat.basic_rates(info, name)
        return {minute: rates.get(minute % MINUTES_PER_DAY, 0.0) for minute in range(minutes)}

    def rates_for(self, midnight_utc, minutes):
        """Return (import, export) rate dicts keyed by absolute minute from ``midnight_utc``."""
        key = (midnight_utc.year, midnight_utc.month)

        if self.import_url or self.export_url:
            import_stamped = self.import_rates.get(key, {})
            export_stamped = self.export_rates.get(key, {})
            # A 48 hour window starting late in a month spills into the next month's download
            next_key = (midnight_utc.year, midnight_utc.month + 1) if midnight_utc.month < 12 else (midnight_utc.year + 1, 1)
            import_stamped = dict(import_stamped)
            import_stamped.update(self.import_rates.get(next_key, {}))
            export_stamped = dict(export_stamped)
            export_stamped.update(self.export_rates.get(next_key, {}))

            rate_import = {}
            rate_export = {}
            for minute in range(minutes):
                stamp = midnight_utc + timedelta(minutes=minute)
                if stamp in import_stamped:
                    rate_import[minute] = import_stamped[stamp]
                if stamp in export_stamped:
                    rate_export[minute] = export_stamped[stamp]
            return rate_import, rate_export

        rate_import = self._basic_window(self.basic_import or [], "rates_import", minutes)
        rate_export = self._basic_window(self.basic_export or [], "rates_export", minutes)
        return rate_import, rate_export

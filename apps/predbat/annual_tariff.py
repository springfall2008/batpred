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
    """Append period_from/period_to to an Octopus rates URL, preserving existing query parameters.

    ``page_size`` is only appended when the caller's URL does not already specify
    one, so an explicit ``page_size=100`` is not silently overridden by a second,
    conflicting ``page_size=1500`` appended after it.
    """
    separator = "&" if "?" in base_url else "?"
    url = "{}{}period_from={}&period_to={}".format(base_url, separator, start_utc.strftime("%Y-%m-%dT%H:%MZ"), end_utc.strftime("%Y-%m-%dT%H:%MZ"))
    if "page_size=" not in base_url:
        url += "&page_size=1500"
    return url


class AnnualTariff:
    """Import and export rates for arbitrary historical dates."""

    def __init__(self, config, log, predbat, storage=None, fetch_json=None):
        """Configure the tariff from the annual config's ``tariff`` block.

        Octopus product codes are region-suffixed. ``resolve_arg``'s ``extra_args``
        substitutes ``{dno_region}`` from the config directly, without writing it
        into ``predbat.args`` first, so a live Octopus component configured for a
        different region is never clobbered by this tariff's region. Without a
        region a URL silently 404s and the month is reported unavailable, which
        looks like an outage rather than a config mistake.
        """
        self.config = config
        self.log = log
        self.predbat = predbat
        self.storage = storage
        self.fetch_json = fetch_json or self._default_fetch_json
        dno_region = config.get("dno_region")
        self.import_url = self._resolve_url(config.get("import_octopus_url"), "import_octopus_url", dno_region)
        self.export_url = self._resolve_url(config.get("export_octopus_url"), "export_octopus_url", dno_region)
        self.basic_import = config.get("rates_import")
        self.basic_export = config.get("rates_export")
        self.standing_charge_p_per_day = float(config.get("standing_charge_p_per_day", 0.0))
        # Keyed by (year, month); each value is a dict of tz-aware UTC stamp to rate
        self.import_rates = {}
        self.export_rates = {}
        self.available = set()
        # Lazily computed, cached 1440-minute basic rate tables (see _basic_table);
        # computing these afresh on every rates_for call would re-run basic_rates's
        # per-entry logging and load_scaling_dynamic mutation roughly 365 times a year.
        self._basic_import_table = None
        self._basic_export_table = None

    def _resolve_url(self, url, name, dno_region=None):
        """Substitute templated arguments such as {dno_region} into a tariff URL, without mutating predbat.args."""
        if not url:
            return None
        extra_args = {"dno_region": dno_region} if dno_region else None
        return self.predbat.resolve_arg(name, url, indirect=False, extra_args=extra_args)

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

        # A run only reaches its natural end when `url` runs out on its own. If a page
        # request failed, or the safety cap was hit while pages remained, `rows` is a
        # truncated download - it must not be parsed, used, or cached as if complete.
        if truncated or url:
            if not truncated:
                self.log("Warn: Annual: rate download for {} hit the {}-page cap with more pages remaining; not caching a partial result".format(cache_key, pages))
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

            # Only import failing makes the month unusable: rates_for's gate is
            # `self.import_url or self.export_url`, so an export-only configuration
            # (no import_url) must not be marked unavailable just because
            # `import_rates` is the empty dict it was initialised to above.
            if self.import_url and not import_rates:
                return False
            self.import_rates[key] = import_rates
            self.export_rates[key] = export_rates
            self.available.add(key)
            return True

        # Basic rates repeat a fixed daily pattern, so nothing needs downloading -
        # but without an import rate source there is nothing to price import with,
        # and reporting the month available would silently price a year at zero.
        if not self.basic_import:
            self.log("Warn: Annual: no rate source configured for {}-{:02d} (no Octopus import URL and no rates_import); refusing to report this month as available".format(year, month))
            return False
        self.available.add(key)
        return True

    def month_available(self, year, month):
        """Return True when usable rates exist for the given month."""
        return (year, month) in self.available

    def _basic_table(self, info, name, cache_attr):
        """Compute and cache the 1440-minute basic rate table for one rate name.

        ``basic_rates`` logs per entry and mutates ``predbat.load_scaling_dynamic``,
        so recomputing it on every ``rates_for`` call (roughly 365 times a year)
        would repeat that work for an identical table. Entries keyed by
        ``day_of_week``/``date`` are stripped before calling it: ``basic_rates``
        anchors those to ``predbat.midnight`` (the day the tool is run), and
        ``rates_for`` then collapses everything to a single repeating day via
        ``minute % MINUTES_PER_DAY`` - so honouring them would make a historical
        replay depend on today's weekday rather than the sampled historical date.
        """
        cached = getattr(self, cache_attr)
        if cached is not None:
            return cached
        usable = []
        ignored = 0
        for entry in info or []:
            if isinstance(entry, dict) and ("day_of_week" in entry or "date" in entry):
                ignored += 1
                continue
            usable.append(entry)
        if ignored:
            self.log("Warn: Annual: {} contains {} day_of_week/date entries, which are anchored to today's date rather than the sampled historical date and cannot be honoured during an annual replay; ignoring them".format(name, ignored))
        table = self.predbat.basic_rates(usable, name)
        setattr(self, cache_attr, table)
        return table

    def _basic_window(self, info, name, minutes, cache_attr):
        """Expand a cached basic rates table across the requested window."""
        rates = self._basic_table(info, name, cache_attr)
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

        rate_import = self._basic_window(self.basic_import or [], "rates_import", minutes, "_basic_import_table")
        rate_export = self._basic_window(self.basic_export or [], "rates_export", minutes, "_basic_export_table")
        return rate_import, rate_export

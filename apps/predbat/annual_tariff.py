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

import pytz

from annual_http import fetch_json
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

    def __init__(self, config, log, predbat, storage=None, fetch_json=None, timezone="UTC"):
        """Configure the tariff from the annual config's ``tariff`` block.

        Octopus product codes are region-suffixed. ``resolve_arg``'s ``extra_args``
        substitutes ``{dno_region}`` from the config directly, without writing it
        into ``predbat.args`` first, so a live Octopus component configured for a
        different region is never clobbered by this tariff's region. Without a
        region a URL silently 404s and the month is reported unavailable, which
        looks like an outage rather than a config mistake.

        ``timezone`` is the annual config's IANA timezone name, used only to key the
        current-rates fallback pattern (see ``_fetch_current_pattern``) by local
        minute-of-day rather than UTC.
        """
        self.config = config
        self.log = log
        self.predbat = predbat
        self.storage = storage
        self.fetch_json = fetch_json or self._default_fetch_json
        self.timezone = pytz.timezone(timezone)
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
        # Lazily fetched, cached current-rates fallback patterns (see
        # _fetch_current_pattern): None means not yet fetched, {} means fetched but
        # empty. fetch_month is called twelve times a run and must not re-download
        # this twelve times.
        self._current_pattern = {"import": None, "export": None}
        # (year, month, "import"/"export") triples where a month's rates were
        # synthesised from the current pattern instead of that month's own download,
        # mirroring AnnualWeather.fallback_months so AnnualPredictor.run() can raise
        # a caveat about it.
        self.fallback_months = set()

    def _resolve_url(self, url, name, dno_region=None):
        """Substitute templated arguments such as {dno_region} into a tariff URL, without mutating predbat.args."""
        if not url:
            return None
        extra_args = {"dno_region": dno_region} if dno_region else None
        return self.predbat.resolve_arg(name, url, indirect=False, extra_args=extra_args)

    async def _default_fetch_json(self, url):
        """Download and decode one JSON document, returning None on any failure."""
        return await fetch_json(url, self.log, "Octopus rate request", {"accept": "application/json", "user-agent": "predbat/1.0"}, 60)

    async def _download_octopus(self, base_url, start_utc, end_utc, cache_key):
        """Download every page of Octopus rates for a date range, returning raw result rows."""
        return await self._download_rows(build_period_url(base_url, start_utc, end_utc), cache_key)

    async def _download_rows(self, url, cache_key):
        """Download every page of Octopus rates starting from the given URL, returning raw result rows.

        Shared by the per-month ranged download and the bare-URL current-rates
        download used by ``_fetch_current_pattern``; the only difference between
        them is the URL each starts from.
        """
        if self.storage:
            cached = await self.storage.load("annual", cache_key)
            if isinstance(cached, list) and cached:
                return cached

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

    def _rows_to_local_pattern(self, rows):
        """Reduce Octopus rate rows to a repeating daily pattern keyed by local minute-of-day.

        Octopus peak windows are defined on the local clock, so each row's
        ``valid_from`` is converted to local time via ``self.timezone`` before its
        minute-of-day is taken as the key - keying by UTC minute-of-day would slide
        the peak by an hour across a DST boundary. A row spanning multiple 30 minute
        slots is expanded across every slot it covers, and where rows overlap the
        slot keeps the rate of whichever row has the most recent ``valid_from``.
        """
        pattern = {}
        sources = {}
        for row in rows:
            try:
                rate = float(row["value_inc_vat"])
                valid_from = pytz.utc.localize(datetime.strptime(row["valid_from"], "%Y-%m-%dT%H:%M:%SZ"))
            except (KeyError, TypeError, ValueError):
                continue
            valid_to_raw = row.get("valid_to")
            try:
                valid_to = pytz.utc.localize(datetime.strptime(valid_to_raw, "%Y-%m-%dT%H:%M:%SZ")) if valid_to_raw else valid_from + timedelta(minutes=30)
            except ValueError:
                valid_to = valid_from + timedelta(minutes=30)
            slots = max(1, int((valid_to - valid_from).total_seconds() // 60 // 30))
            for slot in range(slots):
                local_dt = (valid_from + timedelta(minutes=30 * slot)).astimezone(self.timezone)
                minute_of_day = local_dt.hour * 60 + local_dt.minute
                if sources.get(minute_of_day) is None or valid_from > sources[minute_of_day]:
                    pattern[minute_of_day] = rate
                    sources[minute_of_day] = valid_from
        return pattern

    async def _fetch_current_pattern(self, side):
        """Download a tariff URL's current rates and reduce them to a repeating local-time daily pattern.

        The bare URL (no ``period_from``/``period_to``) returns the tariff's current
        rates, which for a fixed product repeat the same shape every day. Used as a
        fallback when a historical month's own ranged download comes back empty -
        typically because the tariff launched after that historical date. Cached on
        the instance so ``fetch_month``, which runs up to twelve times a year, only
        downloads this once per side.
        """
        cache_attr = "import" if side == "import" else "export"
        cached = self._current_pattern[cache_attr]
        if cached is not None:
            return cached
        url = self.import_url if side == "import" else self.export_url
        rows = await self._download_rows(url, "current_pattern_{}".format(side)) if url else []
        pattern = self._rows_to_local_pattern(rows)
        self._current_pattern[cache_attr] = pattern
        return pattern

    def _stamped_rates_from_pattern(self, pattern, start_utc, days):
        """Repeat a local-time daily rate pattern across a UTC date range.

        Produces the same ``{utc_stamp: rate}`` shape as ``_rows_to_stamped_rates``
        so ``rates_for`` is untouched. Each 30 minute UTC slot in the range is
        converted to local time for that specific historical date before looking up
        its rate, so the same local peak window lands on the correct UTC hour either
        side of a DST boundary.
        """
        stamped = {}
        if not pattern:
            return stamped
        for slot in range(days * 48):
            stamp_utc = start_utc + timedelta(minutes=30 * slot)
            local_dt = stamp_utc.astimezone(self.timezone)
            rate = pattern.get(local_dt.hour * 60 + local_dt.minute)
            if rate is not None:
                stamped[stamp_utc] = rate
        return stamped

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
                if rows:
                    import_rates = self._rows_to_stamped_rates(rows, start_utc, days)
                else:
                    pattern = await self._fetch_current_pattern("import")
                    if pattern:
                        import_rates = self._stamped_rates_from_pattern(pattern, start_utc, days)
                        self.fallback_months.add((year, month, "import"))
                        self.log("Warn: Annual: no import rates available for {}-{:02d}; using today's rates repeated across the month instead".format(year, month))
                    else:
                        self.log("Warn: Annual: no import rates available for {}-{:02d}".format(year, month))
            if self.export_url:
                rows = await self._download_octopus(self.export_url, start_utc, end_utc, "rates_export_{}_{:02d}".format(year, month))
                if rows:
                    export_rates = self._rows_to_stamped_rates(rows, start_utc, days)
                else:
                    pattern = await self._fetch_current_pattern("export")
                    if pattern:
                        export_rates = self._stamped_rates_from_pattern(pattern, start_utc, days)
                        self.fallback_months.add((year, month, "export"))
                        self.log("Warn: Annual: no export rates available for {}-{:02d}; using today's rates repeated across the month instead".format(year, month))
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

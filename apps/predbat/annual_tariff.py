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
import hashlib
from datetime import datetime, timedelta

import pytz

from annual_http import fetch_json
from utils import minute_data

MINUTES_PER_DAY = 24 * 60

# How long a downloaded "current rates" snapshot (see _fetch_current_pattern) stays
# cached to persistent storage. Unlike a per-month ranged download, which covers a
# fixed historical date and can never change, this is a snapshot of "now" - without
# an expiry, re-running the tool after a genuine price change would keep replaying
# the first run's now-stale rates forever.
CURRENT_PATTERN_CACHE_HOURS = 24


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
        # A short hash of each fully-resolved (region already substituted) URL, mixed
        # into every cache key below. The web UI runs every job against one fixed work
        # dir, so its on-disk cache is shared across runs, not scoped to a tariff or a
        # run - without this, switching tariff and re-running would silently reuse the
        # previous tariff's cached rates under the same bare "rates_import_2025_07"-
        # style key: same key, wrong tariff, no error.
        self._import_cache_id = self._url_cache_id(self.import_url) if self.import_url else None
        self._export_cache_id = self._url_cache_id(self.export_url) if self.export_url else None
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
        # (year, month) pairs where export was priced at zero because neither the
        # month's own ranged download nor the current-rates fallback produced any
        # rows. Without this, that fact was only ever a log line - the original bug
        # this module exists to fix, just relocated to the fallback's own failure
        # path - so AnnualPredictor.run() surfaces it as a caveat too.
        self.unpaid_export_months = set()

    def _resolve_url(self, url, name, dno_region=None):
        """Substitute templated arguments such as {dno_region} into a tariff URL, without mutating predbat.args."""
        if not url:
            return None
        extra_args = {"dno_region": dno_region} if dno_region else None
        return self.predbat.resolve_arg(name, url, indirect=False, extra_args=extra_args)

    @staticmethod
    def _url_cache_id(url):
        """Return a short, stable identifier for a resolved tariff URL, for namespacing cache keys.

        Two different tariffs - or two DNO regions of the same product, which resolve
        to different URLs - must never share a cache entry, so this is mixed into every
        cache key built below. A short hex digest of the fully resolved URL (region
        already substituted in) is enough to make an accidental collision between two
        genuinely different URLs practically impossible while keeping cache filenames
        short and filesystem-safe.
        """
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]

    async def _default_fetch_json(self, url):
        """Download and decode one JSON document, returning None on any failure."""
        return await fetch_json(url, self.log, "Octopus rate request", {"accept": "application/json", "user-agent": "predbat/1.0"}, 60)

    async def _download_octopus(self, base_url, start_utc, end_utc, cache_key):
        """Download every page of Octopus rates for a date range, returning (rows, failed)."""
        return await self._download_rows(build_period_url(base_url, start_utc, end_utc), cache_key)

    async def _download_rows(self, url, cache_key, expiry=None):
        """Download every page of Octopus rates starting from the given URL.

        Returns ``(rows, failed)``. ``failed`` is True only when a page request
        errored or the page-cap safety limit was hit with pages still remaining - in
        both cases we do not actually know what rates exist, so this must not be
        confused with a request that completed successfully and genuinely found zero
        rows (``rows == []``, ``failed == False``): only the latter is a real "no
        history for this date" signal that the current-rates fallback should act on.
        Collapsing both cases to a bare empty list, as an earlier version of this
        method did, would let one transient 502 silently trigger the fallback.

        Shared by the per-month ranged download and the bare-URL current-rates
        download used by ``_fetch_current_pattern``; the only difference between
        them is the URL each starts from. ``expiry`` is passed straight through to
        ``storage.save`` - the bare-URL download is a snapshot of "now", so unlike
        the per-month ranged downloads (which cover fixed, unchanging historical
        dates) it must not be cached forever.
        """
        if self.storage:
            cached = await self.storage.load("annual", cache_key)
            if isinstance(cached, list) and cached:
                return cached, False

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
        # truncated download - it must not be parsed, used, or cached as if complete,
        # and the caller must be told this was a failure, not a genuine empty result.
        if truncated or url:
            if not truncated:
                self.log("Warn: Annual: rate download for {} hit the {}-page cap with more pages remaining; not caching a partial result".format(cache_key, pages))
            return [], True
        if rows and self.storage:
            await self.storage.save("annual", cache_key, rows, format="json", expiry=expiry)
        return rows, False

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
        the peak by an hour across a DST boundary.

        A row with no ``valid_to`` is how Octopus publishes a flat product's current,
        open-ended rate - it is not a 30 minute row, it applies to every time of day
        until superseded. The same is true of any row spanning a day or more (a
        historical rate change on a flat product). Both are expanded across every one
        of the 48 daily slots rather than just the 30 minutes after ``valid_from``;
        treating an open-ended row as 30 minutes would leave 47 of 48 slots unrated,
        which ``rates_for`` then silently treats as free. A row shorter than a day
        (the half-hourly shape of a variable/Agile-style product) is still expanded
        slot-by-slot as before. Where rows overlap on a slot, the slot keeps the rate
        of whichever row has the most recent ``valid_from``, so a genuinely
        time-limited row still overrides an open-ended base rate.
        """
        pattern = {}
        sources = {}

        def claim(minute_of_day, rate, valid_from):
            """Give a local minute-of-day slot to a row's rate, unless a more recent row already claimed it."""
            if sources.get(minute_of_day) is None or valid_from > sources[minute_of_day]:
                pattern[minute_of_day] = rate
                sources[minute_of_day] = valid_from

        for row in rows:
            try:
                rate = float(row["value_inc_vat"])
                valid_from = pytz.utc.localize(datetime.strptime(row["valid_from"], "%Y-%m-%dT%H:%M:%SZ"))
            except (KeyError, TypeError, ValueError):
                continue
            valid_to_raw = row.get("valid_to")
            valid_to = None
            if valid_to_raw:
                try:
                    valid_to = pytz.utc.localize(datetime.strptime(valid_to_raw, "%Y-%m-%dT%H:%M:%SZ"))
                except ValueError:
                    valid_to = None

            if valid_to is None or (valid_to - valid_from) >= timedelta(minutes=MINUTES_PER_DAY):
                for minute_of_day in range(0, MINUTES_PER_DAY, 30):
                    claim(minute_of_day, rate, valid_from)
                continue

            slots = max(1, int((valid_to - valid_from).total_seconds() // 60 // 30))
            for slot in range(slots):
                local_dt = (valid_from + timedelta(minutes=30 * slot)).astimezone(self.timezone)
                minute_of_day = local_dt.hour * 60 + local_dt.minute
                claim(minute_of_day, rate, valid_from)
        return pattern

    @staticmethod
    def _pattern_covers_full_day(pattern):
        """Return True when a local-time daily pattern rates every one of the day's 48 standard half-hour slots.

        ``_stamped_rates_from_pattern`` looks up each real minute by flooring its local
        time onto the fixed ``0, 30, 60, ... 1410`` grid, so a pattern is only usable if
        every one of those exact slots is present - not merely "48 rates somewhere in
        the day". Two distinct ways a pattern can fall short of that both matter here:
        a short-row (Agile-style) payload that only covers part of the day (for example
        a bare request answered with a partial trailing window) leaves some grid slots
        with no row at all; and a timezone whose UTC offset is not a whole multiple of
        30 minutes (for example Nepal's +05:45) converts every row onto a grid that
        never lands on these exact slots, however many rows there are. Both must be
        refused the same way - as no usable fallback - rather than one silently pricing
        its gaps at zero while the other happens to fail closed by accident.

        This proves *completeness*, not *coherence*: it confirms every slot has some
        rate in it, not that all 48 came from one mutually consistent day's data. A
        pattern built from rows spanning several different calendar days (or, in
        principle, blended across more than one download) can pass this check while
        having no single day it truly describes. That risk is accepted here rather than
        solved - the current-rates fallback already deliberately projects "roughly now"
        onto a historical month, so this is a difference of degree, not of kind, from
        what the fallback already does by design.
        """
        return all(minute_of_day in pattern for minute_of_day in range(0, MINUTES_PER_DAY, 30))

    async def _fetch_current_pattern(self, side):
        """Download a tariff URL's current rates and reduce them to a repeating local-time daily pattern.

        The bare URL (no ``period_from``/``period_to``) returns the tariff's current
        rates, which for a fixed product repeat the same shape every day. Used as a
        fallback when a historical month's own ranged download comes back empty -
        typically because the tariff launched after that historical date. Cached on
        the instance so ``fetch_month``, which runs up to twelve times a year, only
        downloads this once per side.

        A pattern that does not cover every slot of the local day (see
        ``_pattern_covers_full_day``) is discarded here and treated as no pattern at
        all - a bare request can legitimately answer with a partial trailing window,
        and a partial pattern would otherwise price its uncovered hours at zero, which
        is the exact failure mode this fallback exists to fix, not reproduce.

        A download that genuinely fails (rather than genuinely finding nothing) still
        ends up discarded the same way - there is no third state to fall back to - but
        is logged distinctly, since "the tariff has no current rates" and "we could not
        check because the download failed" are different facts and only one of them is
        actually true here. Whichever it was, the result is cached for the rest of this
        run like any other outcome (see the class docstring on ``fetch_month`` being
        called up to twelve times), so a transient failure is not retried mid-run, but
        it also is not misreported as a settled fact about the tariff.
        """
        cache_attr = "import" if side == "import" else "export"
        cached = self._current_pattern[cache_attr]
        if cached is not None:
            return cached
        url = self.import_url if side == "import" else self.export_url
        if url:
            cache_id = self._import_cache_id if side == "import" else self._export_cache_id
            expiry = datetime.now(pytz.utc) + timedelta(hours=CURRENT_PATTERN_CACHE_HOURS)
            rows, failed = await self._download_rows(url, "current_pattern_{}_{}".format(side, cache_id), expiry=expiry)
            if failed:
                self.log("Warn: Annual: the current-rates snapshot for {} could not be downloaded this run (a transient failure, not evidence the tariff has no current rates); no fallback is available for the rest of this run".format(side))
        else:
            rows = []
        pattern = self._rows_to_local_pattern(rows)
        if pattern and not self._pattern_covers_full_day(pattern):
            # Reports how many of the *standard* grid slots were actually usable, not
            # len(pattern) - a timezone offset that is not a multiple of 30 minutes can
            # produce a pattern with a full 48 entries that still covers zero of the
            # slots _stamped_rates_from_pattern will ever look up (see
            # _pattern_covers_full_day), and "covers 48 of 48" would be a very
            # confusing thing to log about a pattern that is about to be discarded.
            covered = sum(1 for minute_of_day in range(0, MINUTES_PER_DAY, 30) if minute_of_day in pattern)
            self.log("Warn: Annual: the current-rates pattern for {} covers only {} of {} half-hour slots of the local day; treating it as unavailable rather than pricing the gap at zero".format(side, covered, MINUTES_PER_DAY // 30))
            pattern = {}
        self._current_pattern[cache_attr] = pattern
        return pattern

    def _stamped_rates_from_pattern(self, pattern, start_utc, days):
        """Repeat a local-time daily rate pattern across a UTC date range.

        Produces the same ``{utc_stamp: rate}`` shape as ``_rows_to_stamped_rates``
        - crucially, stamped at every minute, not just every 30 minutes: ``rates_for``
        looks up one stamp per minute (``midnight_utc + timedelta(minutes=minute)``
        for every ``minute`` in the window), and a dict only carrying the 48
        half-hour boundaries would leave 1392 of every 1440 minutes unmatched, which
        ``rates_for`` then silently prices at nothing. Each minute's UTC stamp is
        converted to local time for that specific historical date before its
        containing half-hour slot is looked up in the pattern, so the same local peak
        window lands on the correct UTC hour either side of a DST boundary.
        """
        stamped = {}
        if not pattern:
            return stamped
        total_minutes = days * MINUTES_PER_DAY
        for minute in range(total_minutes):
            stamp_utc = start_utc + timedelta(minutes=minute)
            local_dt = stamp_utc.astimezone(self.timezone)
            slot_minute_of_day = ((local_dt.hour * 60 + local_dt.minute) // 30) * 30
            rate = pattern.get(slot_minute_of_day)
            if rate is not None:
                stamped[stamp_utc] = rate
        return stamped

    async def fetch_month(self, year, month):
        """Fetch (or synthesise) the rates covering one calendar month plus a one day buffer.

        Returns True when usable rates exist for the month. The buffer day lets the
        last sampled day of the month complete its 48 hour plan.
        """
        key = (year, month)
        if key in self.available:
            # Rates for a past month cannot change mid-run, and callers ask for the same
            # month more than once: run()'s loop fetches month N and then N+1 for the 48
            # hour plan spill, so every month but the first is requested twice even in a
            # full run, and fast mode's tariff check asks for the anchors ahead of that.
            # Without this, each repeat re-reads the cached rows from storage and rebuilds
            # the whole stamped-rate structure for nothing.
            return True
        if self.import_url or self.export_url:
            days_in_month = calendar.monthrange(year, month)[1]
            start_utc = pytz.utc.localize(datetime(year, month, 1))
            end_utc = start_utc + timedelta(days=days_in_month + 2)
            days = days_in_month + 2

            import_rates = {}
            export_rates = {}
            export_unpaid = False
            if self.import_url:
                rows, failed = await self._download_octopus(self.import_url, start_utc, end_utc, "rates_import_{}_{}_{:02d}".format(self._import_cache_id, year, month))
                if rows:
                    import_rates = self._rows_to_stamped_rates(rows, start_utc, days)
                elif failed:
                    # A download failure tells us nothing about whether history exists;
                    # falling back here would let one transient error silently overwrite
                    # a real historical month with today's rates. Preserve the original
                    # "unavailable" behaviour exactly.
                    self.log("Warn: Annual: no import rates available for {}-{:02d}".format(year, month))
                else:
                    pattern = await self._fetch_current_pattern("import")
                    if pattern:
                        import_rates = self._stamped_rates_from_pattern(pattern, start_utc, days)
                        self.fallback_months.add((year, month, "import"))
                        self.log("Warn: Annual: no import rates available for {}-{:02d}; using today's rates repeated across the month instead".format(year, month))
                    else:
                        self.log("Warn: Annual: no import rates available for {}-{:02d}".format(year, month))
            if self.export_url:
                rows, failed = await self._download_octopus(self.export_url, start_utc, end_utc, "rates_export_{}_{}_{:02d}".format(self._export_cache_id, year, month))
                if rows:
                    export_rates = self._rows_to_stamped_rates(rows, start_utc, days)
                elif failed:
                    self.log("Warn: Annual: no export rates available for {}-{:02d}, treating export as unpaid".format(year, month))
                    export_unpaid = True
                else:
                    pattern = await self._fetch_current_pattern("export")
                    if pattern:
                        export_rates = self._stamped_rates_from_pattern(pattern, start_utc, days)
                        self.fallback_months.add((year, month, "export"))
                        self.log("Warn: Annual: no export rates available for {}-{:02d}; using today's rates repeated across the month instead".format(year, month))
                    else:
                        self.log("Warn: Annual: no export rates available for {}-{:02d}, treating export as unpaid".format(year, month))
                        export_unpaid = True

            # Only a failed import download makes the month unusable: there is then
            # nothing to price import with, whereas a missing export merely means export
            # earns nothing. A tariff with no import URL at all is fine here - it either
            # has a basic import pattern, which rates_for now reads independently of the
            # export side, or no import source whatsoever, which _validate_tariff rejects
            # long before this point.
            if self.import_url and not import_rates:
                return False
            # Recorded only now that the month is confirmed available: a month excluded
            # entirely because import failed contributes nothing to the results at all,
            # so telling the user "export was priced at zero" about it would be reporting
            # a fact about a month that was never billed in the first place.
            if export_unpaid:
                self.unpaid_export_months.add((year, month))
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
        table = self.predbat.basic_rates(usable, name, include_manual_api=False)
        setattr(self, cache_attr, table)
        return table

    def _basic_window(self, info, name, minutes, cache_attr):
        """Expand a cached basic rates table across the requested window."""
        rates = self._basic_table(info, name, cache_attr)
        return {minute: rates.get(minute % MINUTES_PER_DAY, 0.0) for minute in range(minutes)}

    def _side_window(self, midnight_utc, minutes, url, stamped_by_month, basic, name, cache_attr):
        """Return one side's rates, keyed by absolute minute from ``midnight_utc``.

        Each side picks its own source: a downloaded URL if it has one, otherwise its
        basic repeating pattern. This used to be one either/or gate over BOTH sides
        (``if self.import_url or self.export_url``), which silently mispriced any
        mixed tariff - and since import and export became separately selectable, a
        mixed tariff is an ordinary choice rather than a hypothetical. "Price cap
        import with Octopus Outgoing Prime export" took the URL branch and then found
        no import rates to stamp, pricing the entire year's import at zero.
        """
        if not url:
            return self._basic_window(basic or [], name, minutes, cache_attr)

        key = (midnight_utc.year, midnight_utc.month)
        # A 48 hour window starting late in a month spills into the next month's download
        next_key = (midnight_utc.year, midnight_utc.month + 1) if midnight_utc.month < 12 else (midnight_utc.year + 1, 1)
        stamped = dict(stamped_by_month.get(key, {}))
        stamped.update(stamped_by_month.get(next_key, {}))

        rates = {}
        for minute in range(minutes):
            stamp = midnight_utc + timedelta(minutes=minute)
            if stamp in stamped:
                rates[minute] = stamped[stamp]
        return rates

    def rates_for(self, midnight_utc, minutes):
        """Return (import, export) rate dicts keyed by absolute minute from ``midnight_utc``."""
        rate_import = self._side_window(midnight_utc, minutes, self.import_url, self.import_rates, self.basic_import, "rates_import", "_basic_import_table")
        rate_export = self._side_window(midnight_utc, minutes, self.export_url, self.export_rates, self.basic_export, "rates_export", "_basic_export_table")
        return rate_import, rate_export

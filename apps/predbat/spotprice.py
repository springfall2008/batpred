# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Day-ahead spot price tariffs for European bidding zones.

Most European dynamic tariffs are priced the same way: the day-ahead spot price for the bidding
zone, plus a supplier markup, plus grid fees and levies (which in some countries vary by time of
day, e.g. Germany's section 14a module 3 network charges), with VAT on top. This component builds
that price from the spot market itself, so it works for any supplier with a spot-linked tariff
rather than needing a per-supplier integration:

    import rate = (spot EUR/MWh x exchange_rate / 10 + markup + charge_zone(t)) x (1 + vat)

which, with exchange_rate 1, is in euro cents per kWh. Markup and charge zones are entered in the
same minor currency units per kWh and exclude VAT; vat is a fraction (0.19 for 19%).

Price sources, chosen by spotprice_provider:
  - entsoe:       ENTSO-E Transparency Platform, document A44 (day-ahead prices). Needs a free
                  security token. Falls back to Energy-Charts when it fails.
  - energycharts: Fraunhofer ISE Energy-Charts, no key needed.
  - tibber:       Tibber's own end-user price (already includes markup, grid fees and VAT), read
                  with a personal access token. No markup or VAT is added on top.

Export is either a fixed feed-in tariff or spot-linked (spot + export markup, no VAT), with an
optional rule that pays nothing in any interval where the spot price is negative (Germany's
Solarspitzengesetz). Spot data for the export side is fetched from ENTSO-E/Energy-Charts even when
Tibber supplies the import price.

Rates are published in the same shape as the Octopus/Kraken rate sensors (value_inc_vat,
valid_from, valid_to) and wired in through metric_octopus_import / metric_octopus_export, so the
rest of Predbat consumes them unchanged. Intervals keep their native length (15, 30 or 60 minutes).
"""

import argparse
import asyncio
import functools
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aiohttp
import pytz

from component_base import ComponentBase
from const import TIME_FORMAT_HA
from mock_base import MockBase as SharedMockBase
from predbat_metrics import record_api_call

SPOTPRICE_PROVIDERS = ("entsoe", "energycharts", "tibber")
SPOTPRICE_EXPORT_MODES = ("none", "fixed", "spot")

ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
ENERGYCHARTS_URL = "https://api.energy-charts.info/price"
TIBBER_URL = "https://api.tibber.com/v1-beta/gql"

# Bidding zone name -> ENTSO-E EIC area code. Names follow Energy-Charts' bzn codes so one setting
# drives both sources. IE-SEM is ENTSO-E only (Energy-Charts does not publish it).
BIDDING_ZONES = {
    "AT": "10YAT-APG------L",
    "BE": "10YBE----------2",
    "BG": "10YCA-BULGARIA-R",
    "CH": "10YCH-SWISSGRIDZ",  # cspell:disable-line
    "CZ": "10YCZ-CEPS-----N",
    "DE-LU": "10Y1001A1001A82H",
    "DK1": "10YDK-1--------W",
    "DK2": "10YDK-2--------M",
    "EE": "10Y1001A1001A39I",
    "ES": "10YES-REE------0",
    "FI": "10YFI-1--------U",
    "FR": "10YFR-RTE------C",
    "GR": "10YGR-HTSO-----Y",  # cspell:disable-line
    "HR": "10YHR-HEP------M",
    "HU": "10YHU-MAVIR----U",  # cspell:disable-line
    "IE-SEM": "10Y1001A1001A59C",
    "IT-Calabria": "10Y1001C--00096J",
    "IT-Centre-North": "10Y1001A1001A70O",
    "IT-Centre-South": "10Y1001A1001A71M",
    "IT-North": "10Y1001A1001A73I",
    "IT-Sardinia": "10Y1001A1001A74G",
    "IT-Sicily": "10Y1001A1001A75E",
    "IT-South": "10Y1001A1001A788",
    "LT": "10YLT-1001A0008Q",
    "LV": "10YLV-1001A00074",
    "NL": "10YNL----------L",
    "NO1": "10YNO-1--------2",
    "NO2": "10YNO-2--------T",
    "NO3": "10YNO-3--------J",
    "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H",
    "PL": "10YPL-AREA-----S",
    "PT": "10YPT-REN------W",
    "RO": "10YRO-TEL------P",
    "RS": "10YCS-SERBIATSOV",  # cspell:disable-line
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
    "SI": "10YSI-ELES-----O",  # cspell:disable-line
    "SK": "10YSK-SEPS-----K",
}
ENERGYCHARTS_UNSUPPORTED_ZONES = {"IE-SEM"}

# Routine refresh of prices already held, in minutes.
SPOTPRICE_REFRESH_MINUTES = 6 * 60
# Day-ahead prices for tomorrow are published around 12:00-13:00 CET. From this hour on the market clock (CET),
# until tomorrow's prices are held, poll every SPOTPRICE_TOMORROW_RETRY_MINUTES.
SPOTPRICE_TOMORROW_HOUR = 12
SPOTPRICE_TOMORROW_RETRY_MINUTES = 15
# Error back-off: first retry after SPOTPRICE_BACKOFF_MIN_MINUTES, doubling up to SPOTPRICE_BACKOFF_MAX_MINUTES.
SPOTPRICE_BACKOFF_MIN_MINUTES = 5
SPOTPRICE_BACKOFF_MAX_MINUTES = 120
# Status turns "stale" when a source is failing and its prices run out sooner than this.
SPOTPRICE_STALE_HORIZON_HOURS = 12
# The day-ahead delivery day of every zone here runs midnight to midnight CET/CEST - including Ireland's
# SEM and the Iberian MIBEL, whose day is 23:00-23:00 in Irish/Portuguese local time - so "tomorrow's
# prices are in" and the fetch window are judged on this clock, not the user's.
MARKET_TIMEZONE = pytz.timezone("Europe/Brussels")
# Granularity used to detect overlapping intervals between series of different resolutions.
OVERLAP_TICK_MINUTES = 5

# Failures of the request itself, recorded as connection_error. Anything else raised while fetching
# (a body that will not decode, an unexpected shape) is recorded as decode_error.
NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)

ISO_DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?$")


class SpotPriceError(Exception):
    """A price source could not supply prices; the message says why."""


def parse_iso_duration_minutes(text):
    """Parse an ISO 8601 time duration such as PT15M, PT60M or PT1H into minutes, or None."""
    match = ISO_DURATION_RE.match((text or "").strip())
    if not match or not (match.group(1) or match.group(2)):
        return None
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def parse_utc(text):
    """Parse an ISO timestamp (ENTSO-E uses 2025-10-01T22:00Z) into an aware UTC datetime."""
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _local_name(tag):
    """Strip any {uri} prefix from an element tag, leaving its local name."""
    return tag.rsplit("}", 1)[-1]


def _child(element, name):
    """Return the first direct child of element with the given local name, or None."""
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def _children(element, name):
    """Return every direct child of element with the given local name."""
    return [child for child in element if _local_name(child.tag) == name]


def _child_text(element, name):
    """Return the stripped text of a named direct child, or None."""
    child = _child(element, name)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def resolve_overlaps(series):
    """Merge interval series of different resolutions into one non-overlapping list.

    series is a list of (resolution_minutes, sequence, intervals) where intervals is a list of
    (start, end, value). Finer resolutions win, then lower sequence numbers: during the move to a
    15 minute market some zones publish both a 60 and a 15 minute series for the same day, and
    some publish more than one sequence. Returns the accepted intervals sorted by start.
    """
    taken = set()
    accepted = []
    for _resolution, _sequence, intervals in sorted(series, key=lambda item: (item[0], item[1])):
        for start, end, value in intervals:
            ticks = []
            tick = start
            while tick < end:
                ticks.append(tick)
                tick += timedelta(minutes=OVERLAP_TICK_MINUTES)
            if any(t in taken for t in ticks):
                continue
            taken.update(ticks)
            accepted.append((start, end, value))
    accepted.sort(key=lambda item: item[0])
    return accepted


def parse_errors_as(source):
    """Decorate a response parser so any failure surfaces as SpotPriceError.

    A parser meeting a response of an unexpected shape can fail in many ways (KeyError, TypeError,
    AttributeError, UnicodeDecodeError, OverflowError/OSError from fromtimestamp...). Funnelling them
    all into SpotPriceError keeps refresh() on its record_failure()/back-off path instead of letting a
    raw exception escape the component's run loop.
    """

    def decorate(func):
        """Wrap func."""

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            """Call func, converting unexpected exceptions."""
            try:
                return func(*args, **kwargs)
            except SpotPriceError:
                raise
            except Exception as e:
                raise SpotPriceError("{} response could not be read ({}: {})".format(source, type(e).__name__, e))

        return wrapper

    return decorate


@parse_errors_as("ENTSO-E")
def parse_entsoe_xml(text):
    """Parse an ENTSO-E A44 day-ahead price document.

    Returns (intervals, currency), intervals being a sorted list of (start, end, price per MWh) in
    UTC. Curve type A03 (variable sized blocks) omits a Point whose price equals the previous one,
    so a missing position repeats the last price; A01 lists every position and parses the same way.
    An acknowledgement document (ENTSO-E's "no matching data" reply) returns no intervals.
    Raises SpotPriceError on a document that cannot be read.
    """
    if not text:
        raise SpotPriceError("ENTSO-E returned an empty response")
    # A price document never declares entities; refusing any DTD keeps entity expansion out of the parser
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise SpotPriceError("ENTSO-E response contains a DTD, refusing to parse it")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SpotPriceError("ENTSO-E response is not valid XML: {}".format(e))

    root_name = _local_name(root.tag)
    if root_name == "Acknowledgement_MarketDocument":
        reason = None
        for element in root.iter():
            if _local_name(element.tag) == "text" and element.text:
                reason = element.text.strip()
                break
        if reason and "no matching data" in reason.lower():
            return [], None
        raise SpotPriceError("ENTSO-E rejected the request: {}".format(reason or "unknown reason"))
    if root_name != "Publication_MarketDocument":
        raise SpotPriceError("ENTSO-E returned an unexpected document {}".format(root_name))

    currency = None
    series = []
    for timeseries in _children(root, "TimeSeries"):
        currency = _child_text(timeseries, "currency_Unit.name") or currency
        curve_type = _child_text(timeseries, "curveType") or "A01"
        sequence_text = None
        sequence_element = _child(timeseries, "classificationSequence_AttributeInstanceComponent.position")
        if sequence_element is not None and sequence_element.text:
            sequence_text = sequence_element.text.strip()
        try:
            sequence = int(sequence_text) if sequence_text else 1
        except ValueError:
            sequence = 1
        for period in _children(timeseries, "Period"):
            interval = _child(period, "timeInterval")
            resolution = parse_iso_duration_minutes(_child_text(period, "resolution"))
            if interval is None or not resolution:
                continue
            try:
                period_start = parse_utc(_child_text(interval, "start"))
                period_end = parse_utc(_child_text(interval, "end"))
            except (AttributeError, ValueError):
                continue
            points = {}
            for point in _children(period, "Point"):
                try:
                    position = int(_child_text(point, "position"))
                    points[position] = float(_child_text(point, "price.amount"))
                except (TypeError, ValueError):
                    continue
            count = int((period_end - period_start).total_seconds() // (resolution * 60))
            intervals = []
            last_price = None
            for position in range(1, count + 1):
                if position in points:
                    last_price = points[position]
                elif curve_type != "A03":
                    # A01 has no gap filling - a missing point is genuinely missing
                    last_price = None
                if last_price is None:
                    continue
                start = period_start + timedelta(minutes=resolution * (position - 1))
                intervals.append((start, start + timedelta(minutes=resolution), last_price))
            series.append((resolution, sequence, intervals))
    return resolve_overlaps(series), currency


KNOWN_RESOLUTIONS = (timedelta(minutes=15), timedelta(minutes=30), timedelta(minutes=60))


def point_resolutions(starts):
    """Give each start in a sorted list the resolution of the run it belongs to.

    A point takes the spacing to the next point when that is a known market resolution (15, 30 or 60
    minutes) and the run continues at that spacing (the following gap, or the previous one, is the
    same), or the next point is the last. Otherwise - the next gap is longer than any resolution
    (points missing) or the spacing changes and the point could belong to either side - it takes
    the smaller known spacing around it, so it can never be stretched over a missing point. A
    series of mixed resolutions (60 minute hours followed by 15 minute quarters) keeps each part at
    its own length rather than being forced to one series-wide spacing.
    """
    count = len(starts)
    gaps = [starts[i + 1] - starts[i] for i in range(count - 1)]
    known_gaps = [gap for gap in gaps if gap in KNOWN_RESOLUTIONS]
    fallback = min(known_gaps) if known_gaps else timedelta(minutes=60)
    resolutions = []
    for i in range(count):
        forward = gaps[i] if i < count - 1 else None
        backward = gaps[i - 1] if i > 0 else None
        following = gaps[i + 1] if i + 1 < count - 1 else None
        if forward in KNOWN_RESOLUTIONS and (following is None or following == forward or backward == forward):
            resolutions.append(forward)
            continue
        around = [gap for gap in (backward, forward) if gap in KNOWN_RESOLUTIONS]
        if around:
            resolutions.append(min(around))
        else:
            # Isolated by missing points on both sides: carry on at the resolution of the run before it
            resolutions.append(resolutions[-1] if resolutions else fallback)
    return resolutions


def intervals_from_starts(starts_values):
    """Turn (start, value) pairs that carry no end time into (start, end, value) intervals.

    Energy-Charts and Tibber give only interval starts. Each point gets its own run's resolution
    (see point_resolutions), worked out over all published starts including ones whose value is
    missing, and is cut short if the next start comes sooner. An interval is never stretched over a
    missing point: a point with no value (None) or no timestamp at all simply leaves a gap, which
    Predbat fills as it does any other gap in a rate feed. Gap filling for ENTSO-E's A03 curves
    happens only in parse_entsoe_xml, where the format says a missing point repeats the last price.
    """
    starts = sorted({start for start, _value in starts_values})
    if not starts:
        return []
    resolution = dict(zip(starts, point_resolutions(starts)))
    next_start = {starts[i]: starts[i + 1] for i in range(len(starts) - 1)}
    intervals = []
    seen = set()
    for start, value in sorted(starts_values, key=lambda item: item[0]):
        if value is None or start in seen:
            continue
        seen.add(start)
        end = start + resolution[start]
        if start in next_start and next_start[start] < end:
            end = next_start[start]
        intervals.append((start, end, value))
    return intervals


@parse_errors_as("Energy-Charts")
def parse_energycharts_json(data):
    """Parse an Energy-Charts /price response into (start, end, price per MWh) UTC intervals."""
    if not isinstance(data, dict):
        raise SpotPriceError("Energy-Charts returned an unexpected response")
    seconds = data.get("unix_seconds") or []
    prices = data.get("price") or []
    if not isinstance(seconds, list) or not isinstance(prices, list):
        raise SpotPriceError("Energy-Charts returned an unexpected response")
    if len(seconds) != len(prices):
        raise SpotPriceError("Energy-Charts returned {} timestamps but {} prices".format(len(seconds), len(prices)))
    pairs = []
    for stamp, price in zip(seconds, prices):
        if stamp is None:
            continue
        pairs.append((datetime.fromtimestamp(int(stamp), tz=timezone.utc), None if price is None else float(price)))
    return intervals_from_starts(pairs)


@parse_errors_as("Tibber")
def parse_tibber_json(data, home_id=None):
    """Parse a Tibber priceInfo response into (start, end, total per kWh in major units) UTC intervals.

    Picks the home matching home_id, else the first home with a price subscription. Returns
    (intervals, currency, home_id). An entry with a malformed startsAt fails the whole response.
    """
    if not isinstance(data, dict):
        raise SpotPriceError("Tibber returned an unexpected response")
    if data.get("errors"):
        messages = "; ".join(str(err.get("message", err)) if isinstance(err, dict) else str(err) for err in data["errors"])
        raise SpotPriceError("Tibber API error: {}".format(messages))
    homes = ((data.get("data") or {}).get("viewer") or {}).get("homes") or []
    chosen = None
    for home in homes:
        if home_id and home.get("id") != home_id:
            continue
        price_info = ((home.get("currentSubscription") or {}).get("priceInfo")) or None
        if price_info:
            chosen = (home, price_info)
            break
    if not chosen:
        if home_id:
            raise SpotPriceError("Tibber home {} not found or has no active price subscription".format(home_id))
        raise SpotPriceError("No Tibber home with an active price subscription")
    home, price_info = chosen
    pairs = []
    currency = None
    for day in ("today", "tomorrow"):
        for entry in price_info.get(day) or []:
            starts_at = entry["startsAt"]
            if not isinstance(starts_at, str):
                raise SpotPriceError("Tibber returned a startsAt of type {}".format(type(starts_at).__name__))
            total = entry.get("total")
            currency = entry.get("currency") or currency
            pairs.append((parse_utc(starts_at), None if total is None else float(total)))
    return intervals_from_starts(pairs), currency, home.get("id")


def parse_hhmm(text):
    """Parse HH:MM or HH:MM:SS into minutes after midnight (24:00 allowed), or None."""
    if text is None:
        return None
    parts = str(text).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return None
    if hours < 0 or hours > 24 or minutes < 0 or minutes > 59 or (hours == 24 and minutes):
        return None
    return hours * 60 + minutes


DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
CHARGE_ZONE_KEYS = ("from", "to", "charge", "days")


def parse_days(days):
    """Parse a charge zone's days into a set of Python weekdays (Monday = 0), or None for every day.

    Accepts a list or comma separated string of day names ("mon".."sun", or full names) and/or day
    numbers 1-7 with Monday = 1 ... Sunday = 7 - the same numbering as day_of_week in rates_import.
    Raises ValueError on anything else, including 0.
    """
    if days is None:
        return None
    items = days if isinstance(days, (list, tuple, set)) else str(days).split(",")
    result = set()
    for item in items:
        if isinstance(item, bool):
            raise ValueError(item)
        if isinstance(item, int):
            number = item
        else:
            text = str(item).strip().lower()
            if not text:
                continue
            if text.isalpha() and text[:3] in DAY_NAMES:
                result.add(DAY_NAMES[text[:3]])
                continue
            number = int(text)
        if number < 1 or number > 7:
            raise ValueError("day {} is outside 1-7 (Monday = 1)".format(item))
        result.add(number - 1)
    if not result:
        raise ValueError(days)
    return result


def parse_charge_zones(zones, log=None):
    """Validate the spotprice_charge_zones list into (start_minute, end_minute, days, charge) tuples.

    Each entry is {from: "HH:MM", to: "HH:MM", charge: minor units per kWh before VAT, days: optional
    list of "mon".."sun" or 1-7 with Monday = 1}. Times are local. to <= from wraps past midnight, so
    00:00-00:00 covers the whole day and 17:00-00:00 runs to midnight. day_of_week is accepted as an
    alias for days, with a warning. Unknown keys (such as the start/end/rate of rates_import) are
    reported once rather than silently ignored. Bad entries are logged and skipped.
    """
    parsed = []
    unknown_keys = set()
    alias_used = False
    for entry in zones or []:
        if not isinstance(entry, dict):
            if log:
                log("Warn: SpotPrice: ignoring charge zone {}, expected from/to/charge".format(entry))
            continue
        unknown_keys.update(str(key) for key in entry if key not in CHARGE_ZONE_KEYS and key != "day_of_week")
        days_value = entry.get("days")
        if "day_of_week" in entry:
            alias_used = True
            if days_value is None:
                days_value = entry["day_of_week"]
        start = parse_hhmm(entry.get("from", "00:00"))
        end = parse_hhmm(entry.get("to", "00:00"))
        try:
            charge = float(entry.get("charge"))
        except (TypeError, ValueError):
            charge = None
        try:
            days = parse_days(days_value)
        except (TypeError, ValueError):
            days = None
            charge = None
        if start is None or end is None or charge is None:
            if log:
                log("Warn: SpotPrice: ignoring charge zone {}, needs from/to as HH:MM, a numeric charge and days as mon..sun or 1-7 (Monday = 1)".format(entry))
            continue
        parsed.append((start, end, days, charge))
    if log and alias_used:
        log("Warn: SpotPrice: spotprice_charge_zones uses day_of_week, please rename it to days (read as 1-7, Monday = 1)")
    if log and unknown_keys:
        log("Warn: SpotPrice: spotprice_charge_zones has unknown key(s) {} - expected {}".format(", ".join(sorted(unknown_keys)), ", ".join(CHARGE_ZONE_KEYS)))
    return parsed


def charge_zone_rate(zones, local_time):
    """Return the rate of the first charge zone covering local_time (an aware local datetime), else 0."""
    minute = local_time.hour * 60 + local_time.minute
    weekday = local_time.weekday()
    for start, end, days, rate in zones:
        if days is not None and weekday not in days:
            continue
        if end > start:
            inside = start <= minute < end
        else:
            inside = minute >= start or minute < end
        if inside:
            return rate
    return 0.0


def spot_import_rate(spot_mwh, markup, zone_charge, vat, exchange_rate=1.0):
    """Price formula: (spot per MWh x exchange_rate / 10 + markup + zone_charge) x (1 + vat), minor units per kWh; vat is a fraction."""
    return round((spot_mwh * exchange_rate / 10.0 + markup + zone_charge) * (1.0 + vat), 4)


def spot_export_rate(spot_mwh, export_markup, exchange_rate=1.0):
    """Spot-linked export: spot per MWh x exchange_rate / 10 + export markup, minor units per kWh, no VAT."""
    return round(spot_mwh * exchange_rate / 10.0 + export_markup, 4)


def format_rates(intervals):
    """Convert (start, end, rate) intervals into the Octopus-shaped rate list Predbat reads."""
    return [{"value_inc_vat": rate, "valid_from": start.strftime(TIME_FORMAT_HA), "valid_to": end.strftime(TIME_FORMAT_HA)} for start, end, rate in intervals]


def serialise_intervals(intervals):
    """Turn intervals into YAML-friendly [start_iso, end_iso, value] lists for the cache."""
    return [[start.isoformat(), end.isoformat(), value] for start, end, value in intervals or []]


def deserialise_intervals(data):
    """Inverse of serialise_intervals; malformed entries are dropped."""
    intervals = []
    for item in data or []:
        try:
            intervals.append((parse_utc(item[0]), parse_utc(item[1]), float(item[2])))
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
    return intervals


class SpotPriceAPI(ComponentBase):
    """Builds import/export rates from day-ahead spot prices (ENTSO-E, Energy-Charts) or Tibber."""

    def initialize(
        self,
        provider=None,
        zone=None,
        entsoe_token=None,
        tibber_token=None,
        tibber_home_id=None,
        markup=0.0,
        vat=0.0,
        charge_zones=None,
        exchange_rate=1.0,
        export_mode="none",
        export_rate=0.0,
        export_markup=0.0,
        export_zero_on_negative=False,
        automatic=True,
    ):
        """Store configuration and validate it. Problems are logged once here, not on every cycle."""
        if not provider:
            # Unset: any Tibber token means Tibber - even when a zone is also set, since the zone may
            # only be there for spot-linked export - otherwise the keyless default
            provider = "tibber" if tibber_token else "energycharts"
        self.provider = str(provider).strip().lower()
        if self.provider not in SPOTPRICE_PROVIDERS:
            self.log("Warn: SpotPrice: unknown spotprice_provider '{}', expected one of {} - using energycharts".format(provider, ", ".join(SPOTPRICE_PROVIDERS)))
            self.provider = "energycharts"
        self.zone, self.zone_eic = self.resolve_zone(zone)
        self.entsoe_token = entsoe_token
        self.tibber_token = tibber_token
        self.tibber_home_id = tibber_home_id
        self.markup = self.to_float(markup, "spotprice_markup")
        self.vat = self.to_float(vat, "spotprice_vat")
        if self.vat >= 1:
            # spotprice_vat is a fraction (0.19); 1 or more can only have meant a percentage (1 = 1%, not 100%)
            self.log("Warn: SpotPrice: spotprice_vat is a fraction (e.g. 0.19 for 19%), reading {} as {}%".format(self.vat, self.vat))
            self.vat = self.vat / 100.0
        self.exchange_rate = self.to_float(exchange_rate, "spotprice_exchange_rate", default=1.0)
        self.charge_zones = parse_charge_zones(charge_zones, log=self.log)
        self.export_mode = str(export_mode or "none").strip().lower()
        if self.export_mode not in SPOTPRICE_EXPORT_MODES:
            self.log("Warn: SpotPrice: unknown spotprice_export_mode '{}', expected one of {} - export rates disabled".format(export_mode, ", ".join(SPOTPRICE_EXPORT_MODES)))
            self.export_mode = "none"
        self.export_rate = self.to_float(export_rate, "spotprice_export_rate")
        self.export_markup = self.to_float(export_markup, "spotprice_export_markup")
        self.export_zero_on_negative = bool(export_zero_on_negative)
        self.automatic = bool(automatic)

        if self.provider == "entsoe" and not self.entsoe_token:
            self.log("Warn: SpotPrice: spotprice_provider is entsoe but spotprice_entsoe_token is not set - using Energy-Charts")
        if self.provider == "tibber" and not self.tibber_token:
            self.log("Warn: SpotPrice: spotprice_provider is tibber but spotprice_tibber_token is not set")
        if self.provider == "tibber" and self.needs_spot() and not (self.zone or self.zone_eic):
            # Tibber only needs spot prices for the export side; without a zone they cannot be fetched,
            # so switch that export off once here (leaving rates_export in charge) rather than failing
            # every refresh
            self.log(
                "Warn: SpotPrice: spotprice_export_mode {}{} needs spot prices, but spotprice_zone is not set - export rates disabled, set spotprice_zone to enable them".format(
                    self.export_mode, " with spotprice_export_zero_on_negative" if self.export_zero_on_negative else ""
                )
            )
            self.export_mode = "none"
            self.export_zero_on_negative = False
        elif self.needs_spot() and not (self.zone or self.zone_eic):
            self.log("Warn: SpotPrice: spotprice_zone must be set (e.g. DE-LU) to use spot prices")

        # A zone Energy-Charts does not publish (IE-SEM, or a raw EIC code) can only come from ENTSO-E.
        # When ENTSO-E is not usable either there is nothing to fetch, ever: say so once and stop,
        # rather than failing every refresh with "Energy-Charts does not publish zone ...".
        self.config_error = None
        entsoe_usable = bool(self.entsoe_token) and self.provider in ("entsoe", "tibber")
        if self.needs_spot() and (self.zone or self.zone_eic) and not self.energycharts_covers_zone() and not entsoe_usable:
            zone_name = self.zone or self.zone_eic
            if self.provider == "tibber":
                # Only the export side needs spot prices - keep Tibber's import prices and drop that export
                self.log("Warn: SpotPrice: spotprice_entsoe_token is required for spot prices in zone {} - spot-linked export disabled".format(zone_name))
                self.export_mode = "none"
                self.export_zero_on_negative = False
            elif self.provider == "entsoe":
                self.config_error = "spotprice_entsoe_token is required for zone {}".format(zone_name)
            else:
                self.config_error = "Energy-Charts does not publish zone {}, use spotprice_provider entsoe with spotprice_entsoe_token".format(zone_name)
            if self.config_error:
                self.log("Error: SpotPrice: {}".format(self.config_error))

        # Raw source data, cached to storage. Spot intervals are per-MWh in EUR as published;
        # Tibber intervals are end-user totals in major currency units per kWh.
        self.spot_intervals = []
        self.spot_source = None
        self.tibber_intervals = []
        self.tibber_currency = None
        self.fetched = {"spot": None, "tibber": None}
        self.source_errors = {}
        # Back-off is per source: a spot outage must never hold back Tibber's poll for tomorrow
        self.source_failures = {"spot": 0, "tibber": 0}
        self.source_next_attempt = {"spot": None, "tibber": None}
        self.last_error = None
        self.import_rates = []
        self.export_rates = []
        self.entsoe_fallback_logged = False
        self.import_wired = False
        self.export_wired = False

    def to_float(self, value, name, default=0.0):
        """Coerce a numeric setting to float, logging and falling back to default when it is not one."""
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            self.log("Warn: SpotPrice: {} must be a number, got '{}' - using {}".format(name, value, default))
            return default

    def resolve_zone(self, zone):
        """Map spotprice_zone to (zone name, ENTSO-E EIC code). Accepts a zone name or a raw EIC code."""
        if not zone:
            return None, None
        text = str(zone).strip()
        for name, eic in BIDDING_ZONES.items():
            if text.upper() == name.upper() or text.upper() == eic.upper():
                return name, eic
        if text.upper().startswith("10Y"):
            # An EIC code not in the table - usable with ENTSO-E but not with Energy-Charts
            return None, text
        self.log("Warn: SpotPrice: unknown spotprice_zone '{}', known zones are {}".format(zone, ", ".join(BIDDING_ZONES)))
        return text, None

    def energycharts_covers_zone(self):
        """True when Energy-Charts publishes prices for the configured zone."""
        return bool(self.zone) and self.zone in BIDDING_ZONES and self.zone not in ENERGYCHARTS_UNSUPPORTED_ZONES

    def needs_spot(self):
        """True when spot prices are needed: for the import price, spot-linked export, or the negative-price export rule."""
        if self.provider in ("entsoe", "energycharts"):
            return True
        return self.export_mode == "spot" or (self.export_mode == "fixed" and self.export_zero_on_negative)

    def entity_name(self, suffix):
        """Entity id for one of this component's sensors."""
        return "sensor.{}_spotprice_{}".format(self.prefix, suffix)

    def now(self):
        """Current time in UTC; a method so tests can pin it."""
        return datetime.now(timezone.utc)

    def local_midnight(self, when, days=0):
        """Local midnight of the day containing `when`, shifted by `days`, as an aware UTC datetime."""
        tz = self.local_tz if self.local_tz is not None and not isinstance(self.local_tz, str) else timezone.utc
        local_date = when.astimezone(tz).date() + timedelta(days=days)
        naive = datetime(local_date.year, local_date.month, local_date.day)
        if hasattr(tz, "localize"):
            local = tz.localize(naive)
        else:
            local = naive.replace(tzinfo=tz)
        return local.astimezone(timezone.utc)

    def to_local(self, when):
        """Convert an aware datetime to the configured local timezone."""
        tz = self.local_tz if self.local_tz is not None and not isinstance(self.local_tz, str) else timezone.utc
        return when.astimezone(tz)

    def market_midnight(self, when, days=0):
        """Start of the market delivery day containing `when` (CET/CEST), shifted by `days`, in UTC."""
        market_date = when.astimezone(MARKET_TIMEZONE).date() + timedelta(days=days)
        return MARKET_TIMEZONE.localize(datetime(market_date.year, market_date.month, market_date.day)).astimezone(timezone.utc)

    def has_tomorrow(self, data_end, now):
        """True when data_end reaches the end of the next market delivery day."""
        return data_end is not None and data_end >= self.market_midnight(now, 2)

    def fetch_window(self, now):
        """The period to request: yesterday through tomorrow, covering both the local and the market days.

        Taking the wider of the two keeps tomorrow's last market hour for zones east of CET (FI, the
        Baltics, GR) and the local evening for zones west of it.
        """
        start = min(self.local_midnight(now, -1), self.market_midnight(now, -1))
        end = max(self.local_midnight(now, 2), self.market_midnight(now, 2))
        return start, end

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    async def http_get(self, url, params, expect_json):
        """GET a URL, returning (status, body) with body parsed as JSON when expect_json is set."""
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                if expect_json:
                    try:
                        body = await response.json(content_type=None)
                    except Exception:
                        body = None
                else:
                    body = await response.text()
                return response.status, body

    async def http_post_json(self, url, payload, headers):
        """POST JSON, returning (status, parsed JSON body or None)."""
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                try:
                    body = await response.json(content_type=None)
                except Exception:
                    body = None
                return response.status, body

    async def fetch_entsoe(self, start, end):
        """Fetch day-ahead prices from ENTSO-E for [start, end). Returns intervals or raises SpotPriceError."""
        if not self.entsoe_token:
            raise SpotPriceError("no ENTSO-E token configured (spotprice_entsoe_token)")
        if not self.zone_eic:
            raise SpotPriceError("no ENTSO-E area code known for zone {}".format(self.zone))
        params = {
            "securityToken": self.entsoe_token,
            "documentType": "A44",
            # A01 = the day-ahead auction; A44 also carries the intraday auctions (IDA) under other contract types
            "contract_MarketAgreement.type": "A01",
            "in_Domain": self.zone_eic,
            "out_Domain": self.zone_eic,
            "periodStart": start.strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
        }
        try:
            status, body = await self.http_get(ENTSOE_URL, params, expect_json=False)
        except NETWORK_ERRORS as e:
            record_api_call("entsoe", False, "connection_error")
            # The token travels in the query string, so keep it out of anything logged
            raise SpotPriceError("ENTSO-E request failed: {}".format(str(e).replace(self.entsoe_token, "***")))
        except Exception as e:
            # Not a network failure - e.g. a body that will not decode (UnicodeDecodeError)
            record_api_call("entsoe", False, "decode_error")
            raise SpotPriceError("ENTSO-E response could not be read ({}: {})".format(type(e).__name__, str(e).replace(self.entsoe_token, "***")))
        if status == 401:
            record_api_call("entsoe", False, "auth_error")
            raise SpotPriceError("ENTSO-E rejected the security token (HTTP 401)")
        if status == 429:
            record_api_call("entsoe", False, "rate_limit")
            raise SpotPriceError("ENTSO-E rate limit hit (HTTP 429)")
        if status != 200 and not (status == 400 and body and "Acknowledgement_MarketDocument" in body):
            record_api_call("entsoe", False, "server_error" if status >= 500 else "client_error")
            raise SpotPriceError("ENTSO-E returned HTTP {}".format(status))
        try:
            intervals, currency = parse_entsoe_xml(body)
        except SpotPriceError:
            record_api_call("entsoe", False, "decode_error")
            raise
        if not intervals:
            record_api_call("entsoe", False, "client_error")
            raise SpotPriceError("ENTSO-E has no day-ahead prices for {} in this period".format(self.zone or self.zone_eic))
        if currency and currency != "EUR" and self.exchange_rate == 1.0:
            self.log("Warn: SpotPrice: ENTSO-E prices for {} are in {}, set spotprice_exchange_rate if your tariff is in another currency".format(self.zone or self.zone_eic, currency))
        record_api_call("entsoe")
        return intervals

    async def fetch_energycharts(self, start, end):
        """Fetch day-ahead prices from Energy-Charts for [start, end). Returns intervals or raises SpotPriceError."""
        if not self.zone or self.zone in ENERGYCHARTS_UNSUPPORTED_ZONES or self.zone not in BIDDING_ZONES:
            raise SpotPriceError("Energy-Charts does not publish zone {}".format(self.zone or self.zone_eic))
        params = {"bzn": self.zone, "start": start.strftime("%Y-%m-%dT%H:%MZ"), "end": end.strftime("%Y-%m-%dT%H:%MZ")}
        try:
            status, body = await self.http_get(ENERGYCHARTS_URL, params, expect_json=True)
        except NETWORK_ERRORS as e:
            record_api_call("energycharts", False, "connection_error")
            raise SpotPriceError("Energy-Charts request failed: {}".format(e))
        except Exception as e:
            record_api_call("energycharts", False, "decode_error")
            raise SpotPriceError("Energy-Charts response could not be read ({}: {})".format(type(e).__name__, e))
        if status == 429:
            record_api_call("energycharts", False, "rate_limit")
            raise SpotPriceError("Energy-Charts rate limit hit (HTTP 429)")
        if status != 200:
            record_api_call("energycharts", False, "server_error" if status >= 500 else "client_error")
            raise SpotPriceError("Energy-Charts returned HTTP {}".format(status))
        try:
            intervals = parse_energycharts_json(body)
        except SpotPriceError:
            record_api_call("energycharts", False, "decode_error")
            raise
        # The end parameter is inclusive, so trim anything starting at or after the window end
        intervals = [item for item in intervals if item[0] < end]
        if not intervals:
            record_api_call("energycharts", False, "client_error")
            raise SpotPriceError("Energy-Charts has no prices for {} in this period".format(self.zone))
        record_api_call("energycharts")
        return intervals

    async def fetch_spot(self, start, end):
        """Fetch spot prices from the configured source, falling back from ENTSO-E to Energy-Charts.

        The fallback only runs one way. Provider entsoe tries ENTSO-E (when a token is set - initialize()
        has already warned once if not) and then Energy-Charts. Provider energycharts uses Energy-Charts
        only, even if a token is present. Provider tibber, whose spot prices only feed the export side,
        behaves like entsoe when a token is set and like energycharts otherwise. Returns (intervals, source).
        """
        sources = []
        if self.entsoe_token and self.provider in ("entsoe", "tibber"):
            sources.append(("entsoe", self.fetch_entsoe))
        if self.energycharts_covers_zone():
            # Energy-Charts does not publish every zone (IE-SEM); there the ENTSO-E error stands alone
            sources.append(("energycharts", self.fetch_energycharts))
        errors = []
        for name, fetch in sources:
            try:
                intervals = await fetch(start, end)
            except SpotPriceError as e:
                errors.append(str(e))
                continue
            if name == "entsoe" and self.entsoe_fallback_logged:
                self.log("SpotPrice: ENTSO-E is working again")
                self.entsoe_fallback_logged = False
            elif errors and not self.entsoe_fallback_logged:
                # Logged once per outage rather than on every refresh while ENTSO-E stays down
                self.log("Warn: SpotPrice: {}, using {} until ENTSO-E recovers".format("; ".join(errors), name))
                self.entsoe_fallback_logged = True
            return intervals, name
        raise SpotPriceError("; ".join(errors))

    async def fetch_tibber(self):
        """Fetch Tibber's end-user prices for today and tomorrow. Returns intervals or raises SpotPriceError.

        Asks for quarter-hourly prices; retries once without the resolution argument should the API
        reject it.
        """
        if not self.tibber_token:
            raise SpotPriceError("no Tibber token configured (spotprice_tibber_token)")
        headers = {"Authorization": "Bearer {}".format(self.tibber_token), "Content-Type": "application/json"}
        last_error = None
        for resolution in ("(resolution: QUARTER_HOURLY)", ""):
            query = "{ viewer { homes { id currentSubscription { priceInfo%s { today { total currency startsAt } tomorrow { total currency startsAt } } } } } }" % resolution
            try:
                status, body = await self.http_post_json(TIBBER_URL, {"query": query}, headers)
            except NETWORK_ERRORS as e:
                record_api_call("tibber", False, "connection_error")
                raise SpotPriceError("Tibber request failed: {}".format(e))
            except Exception as e:
                record_api_call("tibber", False, "decode_error")
                raise SpotPriceError("Tibber response could not be read ({}: {})".format(type(e).__name__, e))
            if status in (401, 403):
                record_api_call("tibber", False, "auth_error")
                raise SpotPriceError("Tibber rejected the access token (HTTP {})".format(status))
            if status == 429:
                record_api_call("tibber", False, "rate_limit")
                raise SpotPriceError("Tibber rate limit hit (HTTP 429)")
            if status != 200 and not (isinstance(body, dict) and body.get("errors")):
                record_api_call("tibber", False, "server_error" if status >= 500 else "client_error")
                raise SpotPriceError("Tibber returned HTTP {}".format(status))
            try:
                intervals, currency, home_id = parse_tibber_json(body, self.tibber_home_id)
            except SpotPriceError as e:
                last_error = e
                if resolution and "resolution" in str(e).lower():
                    continue
                record_api_call("tibber", False, "decode_error")
                raise
            if not intervals:
                record_api_call("tibber", False, "client_error")
                raise SpotPriceError("Tibber returned no prices")
            self.tibber_currency = currency
            if not self.tibber_home_id:
                self.tibber_home_id = home_id
            record_api_call("tibber")
            return intervals
        record_api_call("tibber", False, "decode_error")
        raise last_error

    # ------------------------------------------------------------------
    # Rate building
    # ------------------------------------------------------------------

    def build_import_rates(self):
        """Compute the import rate intervals from whichever source the provider uses."""
        if self.provider == "tibber":
            # Tibber's total already includes markup, grid fees and VAT - only convert to minor units
            return [(start, end, round(total * 100.0, 4)) for start, end, total in self.tibber_intervals]
        rates = []
        for start, end, spot in self.spot_intervals:
            zone_charge = charge_zone_rate(self.charge_zones, self.to_local(start)) if self.charge_zones else 0.0
            rates.append((start, end, spot_import_rate(spot, self.markup, zone_charge, self.vat, self.exchange_rate)))
        return rates

    def build_export_rates(self):
        """Compute the export rate intervals for the configured export mode (empty when export is off)."""
        if self.export_mode == "none":
            return []
        if self.export_mode == "spot":
            rates = []
            for start, end, spot in self.spot_intervals:
                rate = spot_export_rate(spot, self.export_markup, self.exchange_rate)
                if self.export_zero_on_negative and spot < 0:
                    rate = 0.0
                rates.append((start, end, rate))
            return rates
        # Fixed feed-in tariff
        if self.spot_intervals:
            return [(start, end, 0.0 if (self.export_zero_on_negative and spot < 0) else round(self.export_rate, 4)) for start, end, spot in self.spot_intervals]
        if self.export_zero_on_negative:
            # Without spot prices the negative-price rule cannot be applied, so publish nothing
            # rather than paying a feed-in rate in intervals that may earn nothing
            return []
        start, end = self.fetch_window(self.now())
        return [(start, end, round(self.export_rate, 4))]

    def sources_needed(self):
        """The price sources this configuration fetches: "spot" and/or "tibber"."""
        needed = []
        if self.config_error:
            # Nothing can be fetched until the configuration changes (initialize() has logged why)
            return needed
        if self.needs_spot():
            needed.append("spot")
        if self.provider == "tibber":
            needed.append("tibber")
        return needed

    def intervals_of(self, source):
        """The held intervals of one source."""
        return self.tibber_intervals if source == "tibber" else self.spot_intervals

    def source_data_end(self, source):
        """End of the last held interval of one source, or None."""
        intervals = self.intervals_of(source)
        if not intervals:
            return None
        return max(end for _start, end, _value in intervals)

    def source_intervals(self):
        """The intervals that define how far ahead the import price is known."""
        return self.intervals_of("tibber" if self.provider == "tibber" else "spot")

    def data_end(self):
        """End time of the last known import interval, or None."""
        return self.source_data_end("tibber" if self.provider == "tibber" else "spot")

    @property
    def fetched_at(self):
        """When the oldest needed source was last fetched, or None if any has never been."""
        times = [self.fetched.get(source) for source in self.sources_needed()]
        if not times or any(when is None for when in times):
            return None
        return min(times)

    def current_value(self, rates, now):
        """The rate in force at `now`, or None."""
        for start, end, rate in rates:
            if start <= now < end:
                return rate
        return None

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def source_due(self, source, now):
        """Decide whether one source needs fetching.

        Due when nothing is held, when the data is older than SPOTPRICE_REFRESH_MINUTES, when it no
        longer covers now, or - from SPOTPRICE_TOMORROW_HOUR on the market clock - every
        SPOTPRICE_TOMORROW_RETRY_MINUTES until the next market day's prices are held.
        """
        fetched = self.fetched.get(source)
        data_end = self.source_data_end(source)
        if fetched is None or data_end is None:
            return True
        age = now - fetched
        if age >= timedelta(minutes=SPOTPRICE_REFRESH_MINUTES):
            return True
        if data_end <= now:
            return True
        if now.astimezone(MARKET_TIMEZONE).hour >= SPOTPRICE_TOMORROW_HOUR and not self.has_tomorrow(data_end, now) and age >= timedelta(minutes=SPOTPRICE_TOMORROW_RETRY_MINUTES):
            return True
        return False

    @property
    def failures(self):
        """Consecutive failures of the worst-off source (0 when every source is healthy)."""
        return max(self.source_failures.values())

    @property
    def next_attempt(self):
        """The earliest pending back-off expiry across sources, or None when nothing is backing off."""
        pending = [when for when in self.source_next_attempt.values() if when is not None]
        return min(pending) if pending else None

    @next_attempt.setter
    def next_attempt(self, value):
        """Only clearing is supported: None lifts every source's back-off (the failure counts are kept)."""
        if value is not None:
            raise ValueError("next_attempt is per source; set source_next_attempt instead")
        for source in self.source_next_attempt:
            self.source_next_attempt[source] = None

    def source_blocked(self, source, now):
        """True while this source's own back-off has not expired. Other sources' back-offs never count."""
        when = self.source_next_attempt.get(source)
        return when is not None and now < when

    def refresh_due(self, now):
        """True when any needed source is due and not inside its own back-off."""
        return any(self.source_due(source, now) and not self.source_blocked(source, now) for source in self.sources_needed())

    def record_failure(self, now, source, message):
        """Count a failed fetch of one source and schedule its next attempt with exponential back-off."""
        self.source_failures[source] = self.source_failures.get(source, 0) + 1
        delay = min(SPOTPRICE_BACKOFF_MIN_MINUTES * (2 ** (self.source_failures[source] - 1)), SPOTPRICE_BACKOFF_MAX_MINUTES)
        self.source_next_attempt[source] = now + timedelta(minutes=delay)
        self.log("Warn: SpotPrice: {} refresh failed ({}), retry in {} minutes".format(source, message, delay))

    def record_success(self, now, source):
        """Clear one source's error and back-off after a successful fetch."""
        if self.source_failures.get(source):
            self.log("SpotPrice: {} prices fetched again after {} failed attempt(s)".format(source, self.source_failures[source]))
        self.source_failures[source] = 0
        self.source_next_attempt[source] = None
        self.source_errors.pop(source, None)
        self.fetched[source] = now

    async def fetch_source(self, source, now):
        """Fetch one source and store its intervals. Raises SpotPriceError on failure."""
        if source == "tibber":
            self.tibber_intervals = await self.fetch_tibber()
            return
        if not (self.zone or self.zone_eic):
            raise SpotPriceError("spotprice_zone is not set")
        start, end = self.fetch_window(now)
        self.spot_intervals, self.spot_source = await self.fetch_spot(start, end)

    async def refresh(self, now):
        """Fetch every needed source that is due and outside its own back-off. Returns True when none failed.

        Each source keeps its own fetch time, error and back-off, so with Tibber plus spot-linked export
        a failing spot source neither discards nor delays the Tibber prices - those are stored and cached
        straight away - while the spot failure backs off on its own and shows in the status. Called
        with nothing due (start-up, tests), every source not in back-off is fetched.
        """
        if self.config_error:
            return False
        needed = [source for source in self.sources_needed() if not self.source_blocked(source, now)]
        due = [source for source in needed if self.source_due(source, now)] or needed
        failed = False
        updated = False
        for source in due:
            try:
                await self.fetch_source(source, now)
            except SpotPriceError as e:
                message = str(e)
            except Exception as e:
                # fetch_* convert everything they expect; anything else must still take the back-off path
                message = "{}: {}".format(type(e).__name__, e)
            else:
                self.record_success(now, source)
                updated = True
                continue
            self.source_errors[source] = message
            self.record_failure(now, source, message)
            failed = True
        if updated:
            await self.save_cache()
        self.last_error = "; ".join(self.source_errors[source] for source in sorted(self.source_errors)) or None
        if failed:
            return False
        data_end = self.data_end()
        self.log("SpotPrice: fetched {} prices{}, known until {}".format(self.provider, " (spot from {})".format(self.spot_source) if self.spot_source else "", data_end.isoformat() if data_end else "unknown"))
        return True

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def cache_filename(self):
        """Storage filename for this provider/zone pair."""
        return "{}_{}".format(self.provider, (self.zone or self.zone_eic or "none").replace("-", "_").lower())

    async def load_cache(self):
        """Restore prices from storage so the sensors are populated straight after a restart."""
        if not self.storage:
            return
        data = await self.storage.load("spotprice", self.cache_filename())
        if not isinstance(data, dict):
            return
        self.spot_intervals = deserialise_intervals(data.get("spot_intervals"))
        self.spot_source = data.get("spot_source")
        self.tibber_intervals = deserialise_intervals(data.get("tibber_intervals"))
        self.tibber_currency = data.get("tibber_currency")
        for source in ("spot", "tibber"):
            stamp = data.get("{}_fetched_at".format(source)) or data.get("fetched_at")
            try:
                self.fetched[source] = parse_utc(stamp) if stamp else None
            except (TypeError, ValueError, AttributeError):
                self.fetched[source] = None
        if self.source_intervals():
            self.log("SpotPrice: restored cached prices fetched at {}".format(self.fetched_at.isoformat() if self.fetched_at else "unknown"))

    async def save_cache(self):
        """Persist the raw price data to storage."""
        if not self.storage:
            return
        cache = {
            "spot_intervals": serialise_intervals(self.spot_intervals),
            "spot_source": self.spot_source,
            "tibber_intervals": serialise_intervals(self.tibber_intervals),
            "tibber_currency": self.tibber_currency,
            "spot_fetched_at": self.fetched["spot"].isoformat() if self.fetched.get("spot") else None,
            "tibber_fetched_at": self.fetched["tibber"].isoformat() if self.fetched.get("tibber") else None,
        }
        await self.storage.save("spotprice", self.cache_filename(), cache, format="yaml", expiry=datetime.now(timezone.utc) + timedelta(days=3))

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(self, now):
        """Rebuild the rates from the held data, publish the sensors and wire them into Predbat."""
        import_intervals = self.build_import_rates()
        export_intervals = self.build_export_rates()
        self.import_rates = format_rates(import_intervals)
        self.export_rates = format_rates(export_intervals)
        symbols = self.currency_symbols or "£p"
        unit = "{}/kWh".format(symbols[1] if len(symbols) > 1 else symbols)

        if self.import_rates:
            self.dashboard_item(
                self.entity_name("import_rates"),
                state=self.current_value(import_intervals, now),
                attributes={
                    "friendly_name": "Spot Price Import Rates",
                    "unit_of_measurement": unit,
                    "rates": self.import_rates,
                    "provider": self.provider,
                    "spot_source": self.spot_source,
                    "zone": self.zone or self.zone_eic,
                    "icon": "mdi:currency-eur",
                },
                app="spotprice",
            )
        if self.export_rates:
            self.dashboard_item(
                self.entity_name("export_rates"),
                state=self.current_value(export_intervals, now),
                attributes={
                    "friendly_name": "Spot Price Export Rates",
                    "unit_of_measurement": unit,
                    "rates": self.export_rates,
                    "export_mode": self.export_mode,
                    "zero_on_negative_spot": self.export_zero_on_negative,
                    "icon": "mdi:currency-eur",
                },
                app="spotprice",
            )
        data_end = self.data_end()
        self.dashboard_item(
            self.entity_name("status"),
            state=self.status_state(import_intervals, now),
            attributes={
                "friendly_name": "Spot Price Status",
                "provider": self.provider,
                "spot_source": self.spot_source,
                "zone": self.zone or self.zone_eic,
                "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
                "prices_until": data_end.isoformat() if data_end else None,
                "last_error": self.config_error or self.last_error,
                "source_errors": dict(self.source_errors),
                "failures": dict(self.source_failures),
                "next_attempt": {source: when.isoformat() for source, when in self.source_next_attempt.items() if when},
                "icon": "mdi:chart-bell-curve",
            },
            app="spotprice",
        )

        if self.automatic:
            if self.import_rates and not self.import_wired:
                self.set_arg("metric_octopus_import", self.entity_name("import_rates"))
                self.import_wired = True
            # Only wire export once there are real export rates: wiring an empty sensor would make
            # fetch.py take the metric_octopus_export branch and ignore the user's rates_export.
            if self.export_rates and not self.export_wired:
                self.set_arg("metric_octopus_export", self.entity_name("export_rates"))
                self.export_wired = True

    def status_state(self, import_intervals, now):
        """Status sensor state.

        waiting - nothing fetched yet and nothing has failed
        error   - there is no import price for the interval in force now
        stale   - a source is failing and its prices run out within SPOTPRICE_STALE_HORIZON_HOURS
        ok      - otherwise
        """
        if self.config_error:
            return "error"
        if not import_intervals:
            return "error" if self.source_errors or self.last_error else "waiting"
        if self.current_value(import_intervals, now) is None:
            return "error"
        horizon = now + timedelta(hours=SPOTPRICE_STALE_HORIZON_HOURS)
        for source in self.source_errors:
            source_end = self.source_data_end(source)
            if source_end is None or source_end < horizon:
                return "stale"
        return "ok"

    def health_message(self):
        """Report the last refresh error while prices are failing."""
        if self.config_error:
            return self.config_error
        return self.last_error if self.source_errors else None

    async def run(self, seconds, first):
        """Called by ComponentBase every 60 seconds: refresh when due, then republish."""
        now = self.now()
        if first:
            await self.load_cache()
        if self.refresh_due(now):
            await self.refresh(now)
        # Republish every cycle: the sensor state is the price in force now, and it moves every interval
        self.publish(now)

        data_end = self.data_end()
        if data_end is not None and data_end > now:
            self.update_success_timestamp()
        if first and not self.import_rates and not self.config_error:
            return False
        # After start-up a failed refresh is reported through the status sensor and health_message()
        # rather than by returning False, which would flag an error on every 60 second cycle while
        # the back-off is still waiting
        return True


class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the spot price command-line harness."""

    def __init__(self, currency_symbols="€c", **kwargs):
        """Initialise the shared mock with a spot price cache root and euro currency symbols."""
        super().__init__(config_root="./temp_spotprice", **kwargs)
        self.currency_symbols = currency_symbols

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Store the entity, printing only a summary of the rate list."""
        print("ENTITY: {} = {}".format(entity_id, state))
        self.set_state_wrapper(entity_id, state, attributes)


async def test_spotprice_api(args):  # pragma: no cover
    """Run one fetch cycle against the live APIs and print the first computed rates."""
    import pytz

    base = MockBase(local_tz=pytz.timezone(args.timezone))
    api = SpotPriceAPI(
        base,
        provider=args.provider,
        zone=args.zone,
        entsoe_token=args.entsoe_token,
        tibber_token=args.tibber_token,
        markup=args.markup,
        vat=args.vat,
        export_mode=args.export_mode,
        export_rate=args.export_rate,
        export_zero_on_negative=args.zero_on_negative,
    )
    await api.run(0, True)
    if not api.import_rates:
        print("ERROR: no rates - {}".format(api.last_error))
        return 1
    print("\nSource: provider={} spot_source={} zone={}".format(api.provider, api.spot_source, api.zone))
    print("Import rates ({} intervals), first {}:".format(len(api.import_rates), args.count))
    for rate in api.import_rates[: args.count]:
        print("  {} -> {}  {:.4f}".format(rate["valid_from"], rate["valid_to"], rate["value_inc_vat"]))
    if api.export_rates:
        print("Export rates ({} intervals), first {}:".format(len(api.export_rates), args.count))
        for rate in api.export_rates[: args.count]:
            print("  {} -> {}  {:.4f}".format(rate["valid_from"], rate["valid_to"], rate["value_inc_vat"]))
    return 0


def main():  # pragma: no cover
    """Command line entry point for a live test of the spot price sources."""
    parser = argparse.ArgumentParser(description="Test the day-ahead spot price component")
    parser.add_argument("--provider", default="energycharts", choices=SPOTPRICE_PROVIDERS)
    parser.add_argument("--zone", default="DE-LU")
    parser.add_argument("--timezone", default="Europe/Berlin")
    parser.add_argument("--entsoe-token", dest="entsoe_token")
    parser.add_argument("--tibber-token", dest="tibber_token")
    parser.add_argument("--markup", type=float, default=0.0)
    parser.add_argument("--vat", type=float, default=0.0, help="VAT as a fraction, e.g. 0.19")
    parser.add_argument("--export-mode", dest="export_mode", default="none", choices=SPOTPRICE_EXPORT_MODES)
    parser.add_argument("--export-rate", dest="export_rate", type=float, default=0.0)
    parser.add_argument("--zero-on-negative", dest="zero_on_negative", action="store_true")
    parser.add_argument("--count", type=int, default=8)
    return asyncio.run(test_spotprice_api(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())

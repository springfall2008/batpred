"""Preserve provider rate boundaries independently of expanded minute prices.

Metadata describes source rows, not an observation of a billed price. Long or
open-ended validity ranges are not settlement periods. Inferred cadence is
explicitly labelled and never inferred from equal or changing price values.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

BOUND_KEYS = (("from", "to"), ("valid_from", "valid_to"), ("start", "end"), ("from", "till"))


def period_time(value, timezone_name=None):
    """Parse a source instant as aware UTC, rejecting unanchored local times."""
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            if timezone_name is None:
                return None
            instant = instant.replace(tzinfo=ZoneInfo(timezone_name))
        return instant.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def extract_rate_periods(rows, source_id, from_key=None, to_key=None, interval_minutes=None, period_kind=None, freshness="source", timezone_name=None):
    """Extract original bounds without merging adjacent equal-price periods.

    Explicit intervals up to one hour are interval candidates; longer or
    unbounded rows are validity metadata unless the source declares otherwise
    with ``period_kind``. An explicit null/invalid end is never replaced by a
    guessed end. ``interval_minutes`` is only for feeds with no end field and a
    documented cadence, such as Energi Data Service. Returned dictionaries are
    plain transport data; collectors must not interpret forecasts as observed
    or source-confirmed historical prices.
    """
    periods = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        start_key, end_key = from_key, to_key
        if start_key is None:
            keys = next(((left, right) for left, right in BOUND_KEYS if left in row and right in row), None)
            if keys is None:
                keys = next(((left, right) for left, right in BOUND_KEYS if left in row), (None, None))
            start_key, end_key = keys
        start = period_time(row.get(start_key), timezone_name)
        if start is None:
            continue
        end_present = end_key is not None and end_key in row
        end = period_time(row.get(end_key), timezone_name) if end_present else None
        bounds = row.get("bounds", "explicit")
        if not end_present and interval_minutes is not None:
            if interval_minutes <= 0:
                continue
            end = start + timedelta(minutes=interval_minutes)
            bounds = "cadence"
        if end_present and row.get(end_key) is not None and end is None:
            continue
        if end is not None and end <= start:
            continue
        duration = (end - start).total_seconds() / 60 if end is not None else None
        kind = period_kind or row.get("kind") or ("interval" if duration is not None and duration <= 60 else "validity")
        if end is None:
            kind = "validity"
        row_source_id = row.get("source_id", source_id)
        if isinstance(row_source_id, str) and "://" in row_source_id:
            # Provenance must not copy URL credentials into snapshots or debug dumps.
            try:
                public_source = urlsplit(row_source_id)
                public_source = public_source._replace(netloc=public_source.netloc.rsplit("@", 1)[-1], query="", fragment="").geturl()
                row_source_id = "url:" + hashlib.sha256(public_source.encode("utf-8")).hexdigest()[:16]
            except ValueError:
                row_source_id = "url:unavailable"
        identity = (start, end, row_source_id, kind, bounds)
        if identity in seen:
            continue
        seen.add(identity)
        periods.append({"start": start, "end": end, "source_id": row_source_id, "kind": kind, "bounds": bounds, "freshness": row.get("freshness", freshness)})
    return sorted(periods, key=lambda period: period["start"])


def extend_rate_periods(periods_out, periods, freshness=None):
    """Copy metadata into an optional collector without mutating cached rows."""
    if periods_out is not None:
        for period in periods:
            copied = dict(period)
            if freshness is not None:
                copied["freshness"] = freshness
            periods_out.append(copied)

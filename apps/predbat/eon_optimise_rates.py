"""Validate app periods without copying prices into uncovered intervals."""

from datetime import datetime, timedelta, timezone
import math


def parse_time(value):
    """Require a timezone-aware ISO timestamp and return UTC."""
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Price timestamp has no timezone")
    return result.astimezone(timezone.utc)


def normalise_response(payload):
    """Convert provider cost signs to import cost and export earnings in pence.

    This endpoint's MarketPeriods are half-hourly. Reject other alignment rather
    than silently treating five-minute periods as half-hour prices. Forecasts are
    provider forecasts, not final settlement rates; preserve their provenance.
    """
    if payload.get("errors"):
        raise ValueError("Price service returned GraphQL errors")
    windows = payload["data"]["sitePricing"]["meterWindows"]
    result = {}
    for window in windows:
        direction = {"GENERAL": "import", "FEED_IN": "export"}.get(window["usageType"])
        if direction is None:
            continue
        if direction in result:
            raise ValueError("Ambiguous duplicate price channel")
        periods = {}
        groups = (("previousPeriods", "history"), ("forecastPeriods", "forecast"), ("currentPeriod", "current"))
        for key, quality in groups:
            entries = [window.get(key)] if key == "currentPeriod" else window.get(key, [])
            for period in entries:
                if period is None:
                    continue
                start = parse_time(period["start"])
                if start.minute not in (0, 30) or start.second or start.microsecond:
                    raise ValueError("Price period is not half-hour aligned")
                price = period["kwhPriceInCents"]
                if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
                    raise ValueError("Price is not finite numeric data")
                price = -price if direction == "export" else price
                row = {"valid_from": start.isoformat(), "valid_to": (start + timedelta(minutes=30)).isoformat(), "value_inc_vat": price, "source_quality": quality, "provider_estimate": period.get("estimate")}
                if start in periods and periods[start]["value_inc_vat"] != price:
                    raise ValueError("Conflicting prices for the same interval")
                periods[start] = row
        result[direction] = [periods[key] for key in sorted(periods)]
    if set(result) != {"import", "export"} or not all(result.values()):
        raise ValueError("Both import and export price channels are required")
    return result


def coverage(rates, now):
    """Return the current price and the end of contiguous provider coverage."""
    current = next((r for r in rates if parse_time(r["valid_from"]) <= now < parse_time(r["valid_to"])), None)
    until = parse_time(current["valid_to"]) if current else None
    if until:
        for row in rates:
            if parse_time(row["valid_from"]) == until:
                until = parse_time(row["valid_to"])
    return current, until.isoformat() if until else None


def validate_cached_rates(rates):
    """Validate normalised cache rows without reconstructing an API response."""
    if not isinstance(rates, dict) or set(rates) != {"import", "export"}:
        raise ValueError("Invalid cached channels")
    for rows in rates.values():
        if not isinstance(rows, list) or not rows:
            raise ValueError("Missing cached prices")
        previous = None
        for row in rows:
            start, end = parse_time(row["valid_from"]), parse_time(row["valid_to"])
            price = row["value_inc_vat"]
            if start.minute not in (0, 30) or start.second or start.microsecond or end - start != timedelta(minutes=30) or (previous is not None and start <= previous):
                raise ValueError("Invalid cached interval")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or row["source_quality"] not in ("current", "history", "forecast"):
                raise ValueError("Invalid cached price")
            previous = start
    return rates


GRAPHQL_URL = "https://backend.production.eon-next.amber-international.com.au/graphql"
AUTH_URL = "https://cognito-idp.eu-west-2.amazonaws.com/"
CLIENT_ID = "5tjc4igm29bcmsal7eridtuii6"  # cspell:disable-line
PRICE_QUERY = """query OptimisePrices {
  sitePricing {
    meterWindows {
      usageType
      currentPeriod { start kwhPriceInCents estimate }
      previousPeriods { start kwhPriceInCents estimate }
      forecastPeriods { start kwhPriceInCents estimate }
    }
  }
}"""

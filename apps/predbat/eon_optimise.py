"""Native read-only E.ON Next Optimise prices from its UK Amber app backend."""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import aiohttp

from component_base import ComponentBase
from eon_optimise_client import OptimiseClient, AuthenticationError, PriceFeedError
from eon_optimise_rates import coverage, normalise_response, parse_time

POLL_SECONDS = 300
MAX_AGE_SECONDS = 900


class EonOptimiseAPI(ComponentBase):
    """Use the same rate-sensor contract as Kraken without supplier control APIs."""

    def initialize(self, enabled=False, email=None, password=None):
        """Keep credentials in memory and scope price caches to this login."""
        self.email, self.password = email, password
        self.client = None
        self.rates = {"import": [], "export": []}
        self.fetched_at = None
        self.attempted_at = None
        self.error = None
        self.cache_key = hashlib.sha256((email or "").strip().lower().encode()).hexdigest()
        self.wired = False
        self.restored = False
        self.run_timeout = 120

    def entity(self, suffix):
        """Return stable sensor names without account details."""
        return "sensor.{}_eon_optimise_{}".format(self.prefix, suffix)

    def usable(self, now):
        """Require a recent response and current coverage in both channels."""
        if self.fetched_at is None or not 0 <= (now - self.fetched_at).total_seconds() < MAX_AGE_SECONDS:
            return False
        return all(coverage(self.rates[ch], now)[0] is not None for ch in ("import", "export"))

    async def restore_cache(self, now):
        """Restore price evidence only; never renew timestamps or persist tokens."""
        self.restored = True
        if not self.storage:
            return
        cached = await self.storage.load("eon_optimise", self.cache_key)
        if not isinstance(cached, dict):
            return
        try:
            fetched = parse_time(cached["fetched_at"])
            rates = normalise_response(cached["payload"])
            if not 0 <= (now - fetched).total_seconds() < MAX_AGE_SECONDS:
                return
        except (KeyError, TypeError, ValueError, AttributeError):
            return
        self.rates, self.fetched_at = rates, fetched

    async def save_cache(self):
        """Store only requested price fields, using the existing Storage component."""
        if not self.storage:
            return
        windows = []
        for channel, rows in self.rates.items():
            groups = {"previousPeriods": [], "forecastPeriods": []}
            for row in rows:
                item = dict(start=row["valid_from"], kwhPriceInCents=row["value_inc_vat"] * (-1 if channel == "export" else 1), estimate=row.get("provider_estimate"))
                if row["source_quality"] == "current":
                    groups["currentPeriod"] = item
                else:
                    groups["forecastPeriods" if row["source_quality"] == "forecast" else "previousPeriods"].append(item)
            windows.append(dict(usageType="GENERAL" if channel == "import" else "FEED_IN", **groups))
        await self.storage.save("eon_optimise", self.cache_key, dict(payload={"data": {"sitePricing": {"meterWindows": windows}}}, fetched_at=self.fetched_at.isoformat()), format="yaml", expiry=self.fetched_at + timedelta(seconds=MAX_AGE_SECONDS))

    async def fetch_prices(self):
        """Bound each request and retain the in-memory authentication session."""
        async with aiohttp.ClientSession() as session:
            if self.client is None:
                self.client = OptimiseClient(session, self.email, self.password)
            else:
                self.client.session = session
            return await self.client.fetch(asyncio.to_thread)

    def publish(self, now):
        """Withdraw stale rates explicitly so the existing horizon guard applies."""
        usable = self.usable(now)
        status = "cached" if usable and self.error else "current" if usable else "unavailable"
        for channel in ("import", "export"):
            rows = self.rates[channel] if usable else []
            current, until = coverage(rows, now)
            self.dashboard_item(
                self.entity(channel + "_rates"),
                state=len(rows),
                attributes={
                    "friendly_name": "E.ON Next Optimise " + channel.title() + " Rates",
                    "rates": rows,
                    "tariff_code": "NEXT_OPTIMISE_APP",
                    "source": "eon_optimise_amber_app",
                    "price_unit": "p/kWh",
                    "forecast_is_settlement": False,
                    "last_success": self.fetched_at.isoformat() if self.fetched_at else None,
                    "coverage_status": status,
                    "current_rate_available": current is not None,
                    "confirmed_until": until,
                    "icon": "mdi:currency-gbp",
                },
                app="eon_optimise",
            )
        self.dashboard_item(
            self.entity("status"),
            state=status,
            attributes={"friendly_name": "E.ON Next Optimise Status", "refresh_error": self.error, "last_success": self.fetched_at.isoformat() if self.fetched_at else None, "poll_seconds": POLL_SECONDS, "max_age_seconds": MAX_AGE_SECONDS},
            app="eon_optimise",
        )
        # Opting in makes this the price owner, including when unavailable: never
        # silently revert to an unrelated tariff or retain expired rate sensors.
        if not self.wired:
            self.set_arg("metric_octopus_import", self.entity("import_rates"))
            self.set_arg("metric_octopus_export", self.entity("export_rates"))
            self.wired = True
        return usable

    async def run(self, seconds, first):
        """Poll five-minutely; publish freshness on each component housekeeping tick."""
        now = datetime.now(timezone.utc)
        if not self.restored:
            await self.restore_cache(now)
        due = self.attempted_at is None or (now - self.attempted_at).total_seconds() >= POLL_SECONDS
        if due:
            self.attempted_at = now
            try:
                rates = await self.fetch_prices()
                received = datetime.now(timezone.utc)
                if not all(coverage(rates[ch], received)[0] for ch in ("import", "export")):
                    raise PriceFeedError("Supplier response lacks current import/export coverage")
                self.rates, self.fetched_at, self.error = rates, received, None
                try:
                    await self.save_cache()
                except (OSError, ValueError, TypeError):
                    self.log("Warn: E.ON Optimise price cache could not be saved")
            except (AuthenticationError, PriceFeedError) as exc:
                self.error = str(exc)
                self.log("Warn: E.ON Optimise: " + self.error)
        usable = self.publish(datetime.now(timezone.utc))
        if usable:
            self.update_success_timestamp()
        return usable if first else True

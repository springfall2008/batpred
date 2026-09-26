"""Native read-only E.ON Next Optimise prices from its UK Amber app backend."""

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import aiohttp

from component_base import ComponentBase
from eon_optimise_client import OptimiseClient, AuthenticationError, PriceFeedError
from eon_optimise_rates import coverage, parse_time, validate_cached_rates

POLL_SECONDS = 300
MAX_AGE_SECONDS = 900
CACHE_VERSION = 1


def validate_sources(args):
    """Reject ambiguous price ownership without logging configuration values."""
    conflicts = [key for key in ("rates_import_octopus_url", "rates_export_octopus_url") if key in args]
    if args.get("kraken_provider"):
        conflicts.append("kraken_provider")
    if args.get("octopus_api_key") and args.get("octopus_api_account"):
        conflicts.append("octopus_api_account")
    if conflicts:
        raise ValueError("E.ON Optimise: remove conflicting tariff settings: " + ", ".join(conflicts))


def validate_planner_source(base):
    """Block a new calculation before URL selection or basic-rate fallback."""
    validate_sources(base.args)
    api = base.components.get_component("eon_optimise") if base.components else None
    if api is None or not api.usable(datetime.now(timezone.utc)):
        raise ValueError("E.ON Optimise: current import/export prices unavailable; check the component status")
    for channel in ("import", "export"):
        if base.args.get("metric_octopus_" + channel) != api.entity(channel + "_rates"):
            raise ValueError("E.ON Optimise: tariff sensor binding changed; disable the competing price provider")


class EonOptimiseAPI(ComponentBase):
    """Use the same rate-sensor contract as Kraken without supplier control APIs."""

    def initialize(self, enabled=False, observe_only=False, email=None, password=None):
        """Keep credentials in memory and scope price caches to this login."""
        self.email, self.password = email, password
        self.observe_only = observe_only
        self.client = None
        self.rates = {"import": [], "export": []}
        self.fetched_at = None
        self.attempted_at = None
        self.error = None
        self.cache_key = hashlib.sha256((email or "").strip().lower().encode()).hexdigest()
        self.restored = False
        self.run_timeout = 120

    def entity(self, suffix):
        """Return stable sensor names without account details."""
        return "sensor.{}_eon_optimise_{}".format(self.prefix, suffix)

    def usable(self, now):
        """Require a recent response and current coverage in both channels."""
        return self.price_status(now)[0]

    def price_status(self, now):
        """Calculate freshness and both coverage summaries once per publication."""
        summaries = {ch: coverage(rows, now) for ch, rows in self.rates.items()}
        fresh = self.fetched_at is not None and 0 <= (now - self.fetched_at).total_seconds() < MAX_AGE_SECONDS
        return fresh and all(current is not None for current, until in summaries.values()), summaries

    def bind_sources(self):
        """Validate configuration before binding both planner price inputs."""
        validate_sources(self.base.args)
        for channel in ("import", "export"):
            self.set_arg("metric_octopus_" + channel, self.entity(channel + "_rates"))

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
            if cached.get("version") != CACHE_VERSION:
                return
            rates = validate_cached_rates(cached["rates"])
            if not 0 <= (now - fetched).total_seconds() < MAX_AGE_SECONDS:
                return
        except (KeyError, TypeError, ValueError, AttributeError):
            return
        self.rates, self.fetched_at = rates, fetched

    async def save_cache(self):
        """Store only requested price fields, using the existing Storage component."""
        if not self.storage:
            return
        await self.storage.save("eon_optimise", self.cache_key, dict(version=CACHE_VERSION, rates=self.rates, fetched_at=self.fetched_at.isoformat()), format="yaml", expiry=self.fetched_at + timedelta(seconds=MAX_AGE_SECONDS))

    async def fetch_prices(self):
        """Bound each request and retain the in-memory authentication session."""
        async with aiohttp.ClientSession() as session:
            if self.client is None:
                self.client = OptimiseClient(session, self.email, self.password)
            else:
                self.client.session = session
            return await self.client.fetch(asyncio.to_thread)

    def publish(self, now):
        """Publish supplier evidence separately from planner source binding."""
        usable, summaries = self.price_status(now)
        status = "cached" if usable and self.error else "current" if usable else "unavailable"
        for channel in ("import", "export"):
            rows = self.rates[channel] if usable else []
            current, until = summaries[channel] if usable else (None, None)
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
            attributes={
                "friendly_name": "E.ON Next Optimise Status",
                "mode": "observe_only" if self.observe_only else "planner",
                "refresh_error": self.error,
                "last_success": self.fetched_at.isoformat() if self.fetched_at else None,
                "poll_seconds": POLL_SECONDS,
                "max_age_seconds": MAX_AGE_SECONDS,
            },
            app="eon_optimise",
        )
        return usable

    async def run(self, seconds, first):
        """Poll five-minutely; publish freshness on each component housekeeping tick."""
        if not self.observe_only:
            try:
                self.bind_sources()
            except ValueError as exc:
                self.error = str(exc)
                self.log("Error: " + self.error)
                return False
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
        return (usable or self.observe_only) if first else True

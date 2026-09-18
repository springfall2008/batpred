"""Native E.ON backend contract, credentials, cache and publication regressions."""
import asyncio
import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from eon_optimise import EonOptimiseAPI
from eon_optimise_client import OptimiseClient, AuthenticationError, PriceFeedError
from eon_optimise_rates import normalise_response


def payload(now=None, price=3, export=-2):
    """Return a synthetic UK Amber price response with present and future slots."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    windows = []
    for usage, value in (("GENERAL", price), ("FEED_IN", export)):
        windows.append(
            dict(usageType=usage, currentPeriod=dict(start=start.isoformat(), kwhPriceInCents=value, estimate=False), forecastPeriods=[dict(start=(start + timedelta(minutes=30 * i)).isoformat(), kwhPriceInCents=value, estimate=True) for i in range(1, 49)])
        )
    return {"data": {"sitePricing": {"meterWindows": windows}}}


def component():
    """Create a native component against a non-networked Predbat base."""
    base = MagicMock()
    base.prefix = "predbat"
    base.args = {}
    base.components = None
    return EonOptimiseAPI(base, enabled=True, email="example@example.invalid", password="test-private-password")


class Response:
    """Provide the aiohttp context-manager contract without external requests."""

    def __init__(self, status, body):
        """Store a synthetic response."""
        self.status, self.body = status, body

    async def __aenter__(self):
        """Open the fake response."""
        return self

    async def __aexit__(self, *args):
        """Close the fake response."""
        return None

    async def json(self):
        """Decode the synthetic JSON."""
        return self.body


class Session:
    """Count bounded request attempts."""

    def __init__(self, responses):
        """Install a fixed response sequence."""
        self.responses, self.calls = iter(responses), 0

    def post(self, *args, **kwargs):
        """Consume one response."""
        self.calls += 1
        return next(self.responses)


class EonOptimiseTests(unittest.IsolatedAsyncioTestCase):
    """Cover both rate-channel atomicity and the native component lifecycle."""

    def test_prices_and_secret_registry(self):
        """Negative imports and sign-normalised exports are preserved, once."""
        from components import COMPONENT_LIST, secret_config_names

        rows = normalise_response(payload(price=-3, export=2))
        self.assertEqual(rows["import"][0]["value_inc_vat"], -3)
        self.assertEqual(rows["export"][0]["value_inc_vat"], -2)
        self.assertEqual(len(rows["import"]), 49)
        self.assertTrue({"eon_optimise_email", "eon_optimise_password"} <= secret_config_names())
        self.assertFalse(COMPONENT_LIST["eon_optimise"]["args"]["enabled"]["default"])

    def test_bad_and_duplicate_channels(self):
        """Partial or ambiguous responses cannot silently authorise a tariff."""
        for value in (True, float("nan"), "3"):
            with self.assertRaises(ValueError):
                normalise_response(payload(price=value))
        body = payload()
        body["data"]["sitePricing"]["meterWindows"].pop()
        with self.assertRaises(ValueError):
            normalise_response(body)
        body = payload()
        body["data"]["sitePricing"]["meterWindows"].append(copy.deepcopy(body["data"]["sitePricing"]["meterWindows"][0]))
        with self.assertRaises(ValueError):
            normalise_response(body)

    async def test_success_wires_both_channels_and_leaves_standing_charge(self):
        """Use the same planner inputs as Kraken with no inferred standing fee."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        self.assertTrue(await api.run(0, True))
        self.assertEqual(api.base.set_arg.call_count, 2)
        api.base.set_arg.assert_any_call("metric_octopus_import", "sensor.predbat_eon_optimise_import_rates")
        api.base.set_arg.assert_any_call("metric_octopus_export", "sensor.predbat_eon_optimise_export_rates")
        self.assertTrue(await api.run(60, False))
        api.fetch_prices.assert_awaited_once()

    async def test_failed_fetch_keeps_original_age_then_withdraws(self):
        """A failed refresh may use a fresh cache but cannot make it younger."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        await api.run(0, True)
        original = api.fetched_at
        api.attempted_at -= timedelta(minutes=6)
        api.fetch_prices = AsyncMock(side_effect=PriceFeedError("Price service unavailable"))
        await api.run(360, False)
        self.assertEqual(api.fetched_at, original)
        self.assertEqual(api.base.dashboard_item.call_args.args[1], "cached")
        api.fetched_at -= timedelta(minutes=16)
        await api.run(420, False)
        calls = api.base.dashboard_item.call_args_list[-3:]
        self.assertEqual(calls[0].args[2]["rates"], [])
        self.assertEqual(calls[1].args[2]["rates"], [])
        self.assertEqual(calls[2].args[1], "unavailable")

    async def test_missing_current_does_not_replace_good_pair(self):
        """An otherwise valid schedule without current coverage is rejected."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        await api.run(0, True)
        original = copy.deepcopy(api.rates)
        stamp = api.fetched_at
        api.attempted_at -= timedelta(minutes=6)
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload(datetime.now(timezone.utc) + timedelta(days=1))))
        await api.run(360, False)
        self.assertEqual(api.rates, original)
        self.assertEqual(api.fetched_at, stamp)

    async def test_cache_roundtrip_has_no_auth_material(self):
        """Persist prices only and retain their actual observation timestamp."""
        api = component()
        store = MagicMock()
        store.save = AsyncMock()
        store.load = AsyncMock()
        api.base.components = MagicMock()
        api.base.components.get_component.return_value = store
        api.rates = normalise_response(payload())
        api.fetched_at = datetime.now(timezone.utc)
        await api.save_cache()
        cached = store.save.call_args.args[2]
        self.assertEqual(set(cached), {"payload", "fetched_at"})
        self.assertNotIn(api.password, str(cached))
        self.assertNotIn(api.email, str(cached))
        restored = component()
        restored.base.components = api.base.components
        store.load.return_value = cached
        await restored.restore_cache(datetime.now(timezone.utc))
        self.assertEqual(restored.rates, api.rates)
        self.assertEqual(restored.fetched_at, api.fetched_at)
        await restored.restore_cache(datetime.now(timezone.utc) + timedelta(minutes=16))
        self.assertFalse(restored.usable(datetime.now(timezone.utc) + timedelta(minutes=16)))

    async def test_auth_retry_and_error_redaction(self):
        """Retry a 401 once, never expose authentication exception details."""
        session = Session([Response(401, {}), Response(401, {})])
        client = OptimiseClient(session, "private-email", "private-password")

        def authenticate():
            """Install an ephemeral test token."""
            client.id_token = "private-token"
            client.expires = float("inf")

        client.authenticate = authenticate
        with self.assertRaises(AuthenticationError):
            await client.fetch(asyncio.to_thread)
        self.assertEqual(session.calls, 2)
        client.id_token = None
        client.authenticate = MagicMock(side_effect=RuntimeError("private-password"))
        with self.assertRaises(PriceFeedError) as error:
            await client.fetch(asyncio.to_thread)
        self.assertNotIn("private-password", str(error.exception))

    async def test_shortened_forecast_replaces_old_future(self):
        """A shorter response must not retain the previous forecast's tail."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        await api.run(0, True)
        body = payload(price=1)
        for window in body["data"]["sitePricing"]["meterWindows"]:
            window["forecastPeriods"] = window["forecastPeriods"][:2]
        api.attempted_at -= timedelta(minutes=6)
        api.fetch_prices = AsyncMock(return_value=normalise_response(body))
        await api.run(360, False)
        self.assertEqual(len(api.rates["import"]), 3)
        self.assertEqual(api.rates["import"][0]["value_inc_vat"], 1)


def run_eon_optimise_tests(my_predbat=None):
    """Register these tests with Predbat's standard quick-suite runner."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EonOptimiseTests)
    return not unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite).wasSuccessful()

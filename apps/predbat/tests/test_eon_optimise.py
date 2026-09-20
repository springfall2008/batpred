"""Native E.ON backend contract, credentials, cache and publication regressions."""
import asyncio
import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from eon_optimise import EonOptimiseAPI, validate_planner_source
from eon_optimise_client import OptimiseClient, AuthenticationError, PriceFeedError
from eon_optimise_rates import normalise_response, validate_cached_rates
from fetch import Fetch, EonOptimisePriceUnavailable


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
    base.set_arg.side_effect = base.args.__setitem__
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

    async def test_observe_only_publishes_with_existing_tariff_source(self):
        """Observe-only polling leaves the current planner bindings untouched."""
        base = MagicMock()
        base.prefix = "predbat"
        base.args = {"kraken_provider": "eon", "metric_octopus_import": "sensor.existing_import", "metric_octopus_export": "sensor.existing_export"}
        base.components = None
        api = EonOptimiseAPI(base, enabled=True, observe_only=True, email="example@example.invalid", password="test-private-password")
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        self.assertTrue(await api.run(0, True))
        self.assertEqual(base.args["metric_octopus_import"], "sensor.existing_import")
        self.assertEqual(base.args["metric_octopus_export"], "sensor.existing_export")
        base.set_arg.assert_not_called()
        self.assertEqual(base.dashboard_item.call_args.args[2]["mode"], "observe_only")
        self.assertEqual(base.dashboard_item.call_args_list[-3].args[2]["rates"], api.rates["import"])

        unavailable_api = EonOptimiseAPI(base, enabled=True, observe_only=True, email="example@example.invalid", password="test-private-password")
        unavailable_api.fetch_prices = AsyncMock(side_effect=PriceFeedError("Price service unavailable"))
        self.assertTrue(await unavailable_api.run(0, True))
        self.assertEqual(base.dashboard_item.call_args.args[1], "unavailable")
        self.assertEqual(base.args["metric_octopus_import"], "sensor.existing_import")

    def test_price_guard_skips_cycle_cleanly(self):
        """A stale E.ON feed records status and avoids inverter/planner work."""
        from predbat import PredBat

        base = MagicMock()
        base.get_arg.return_value = False
        base.is_template_mode.return_value = False
        base.fetch_sensor_data.side_effect = EonOptimisePriceUnavailable("E.ON Optimise: current import/export prices unavailable")
        PredBat.update_pred(base)
        base.record_status.assert_called_once_with(message="E.ON Optimise: current import/export prices unavailable", had_errors=True)
        base.fetch_inverter_data.assert_not_called()
        base.calculate_plan.assert_not_called()
        base.execute_plan.assert_not_called()

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
        self.assertEqual(set(cached), {"version", "rates", "fetched_at"})
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

    async def test_conflicts_block_consumer_before_url_download(self):
        """Exercise the real fetch entry point, including conflicts after startup."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        await api.run(0, True)
        base = api.base
        base.args["eon_optimise_enable"] = True
        base.get_arg.side_effect = lambda key, default=None: base.args.get(key, default)
        for conflict in ({"rates_import_octopus_url": "https://example.invalid/private"}, {"rates_export_octopus_url": None}, {"kraken_provider": "eon"}, {"octopus_api_key": "private", "octopus_api_account": "private"}):
            with self.subTest(conflict=list(conflict)):
                base.args.update(conflict)
                with self.assertRaisesRegex(ValueError, "conflicting tariff settings") as error:
                    Fetch.fetch_sensor_data(base)
                self.assertNotIn("private", str(error.exception))
                base.download_octopus_rates.assert_not_called()
                base.basic_rates.assert_not_called()
                for key in conflict:
                    del base.args[key]

    async def test_consumer_rejects_stale_missing_and_rebound_source(self):
        """Validate current evidence at consumption, even between publish ticks."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        await api.run(0, True)
        base = api.base
        base.components = MagicMock()
        base.components.get_component.return_value = api
        validate_planner_source(base)
        base.args["metric_octopus_import"] = "sensor.old_tariff"
        with self.assertRaisesRegex(ValueError, "binding changed"):
            validate_planner_source(base)
        api.bind_sources()
        api.fetched_at -= timedelta(minutes=16)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_planner_source(base)
        api.fetched_at = datetime.now(timezone.utc)
        api.rates["export"] = []
        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_planner_source(base)

    def test_cache_rejects_invalid_normalised_rows(self):
        """Malformed caches cannot bypass response validation or invert signs."""
        for key, value in (("value_inc_vat", float("nan")), ("value_inc_vat", True), ("valid_to", "2020-01-01T00:00:00+00:00"), ("source_quality", "unknown")):
            rates = normalise_response(payload())
            rates["export"][0][key] = value
            with self.assertRaises(ValueError):
                validate_cached_rates(rates)

    async def test_consumer_rejects_missing_sensor_current_price(self):
        """Healthy component memory cannot hide missing published sensor prices."""
        api = component()
        api.fetch_prices = AsyncMock(return_value=normalise_response(payload()))
        await api.run(0, True)
        base = api.base
        base.args["eon_optimise_enable"] = True
        base.get_arg.side_effect = lambda key, default=None, **kwargs: base.args.get(key, default)
        base.components = MagicMock()
        base.components.get_component.side_effect = lambda name: api if name == "eon_optimise" else None
        base.num_cars, base.minutes_now, base.max_days_previous = 0, 600, 7
        base.import_today_now = base.export_today_now = base.pv_today_now = 0
        base.fetch_extra_load_forecast.return_value = ({0: 1}, [])
        base.get_history_wrapper.return_value = []
        base.carbon_enable = False
        for channel in ("import", "export"):
            for missing in ({}, {630: 8}):
                with self.subTest(channel=channel, sensor=missing):
                    base.fetch_octopus_rates.side_effect = lambda entity, **kwargs: missing if entity == api.entity(channel + "_rates") else {600: 3}
                    with self.assertRaisesRegex(ValueError, "current " + channel + " sensor price unavailable"):
                        Fetch.fetch_sensor_data(base)
                    base.basic_rates.assert_not_called()
                    base.rate_replicate.assert_not_called()

    def test_future_gaps_use_shared_kraken_replication(self):
        """Supplier prices win; missing future prices use the common estimates."""
        base = MagicMock()
        base.forecast_minutes = 2 * 1440
        base.metric_future_rate_offset_import = 0
        base.metric_future_rate_offset_export = 0
        base.get_arg.return_value = False
        for is_import in (True, False):
            rates, copied = Fetch.rate_replicate(base, {0: 3.0, 30: 8.0, 1440: 4.0}, is_import=is_import)
            self.assertEqual(rates[1440], 4.0)
            self.assertNotIn(1440, copied)
            self.assertEqual(rates[1470], 8.0)
            self.assertEqual(copied[1470], "copy")
        rates, copied = Fetch.rate_replicate(base, {})
        self.assertEqual(rates, {})

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

    def test_refresh_and_revoked_session(self):
        """Renew valid sessions; use password sign-in once for a revoked session."""
        from botocore.exceptions import ClientError

        for revoked in (False, True):
            with self.subTest(revoked=revoked), patch("pycognito.Cognito") as factory:
                cognito = factory.return_value
                cognito.id_token = None
                cognito.refresh_token = "private-refresh"

                def renew():
                    """Simulate the documented token-renewal outcomes."""
                    if revoked:
                        raise ClientError({"Error": {"Code": "NotAuthorizedException", "Message": "private"}}, "refresh")
                    cognito.id_token = "private-id"

                cognito.renew_access_token.side_effect = renew
                cognito.authenticate.side_effect = lambda password: setattr(cognito, "id_token", "private-id")
                client = OptimiseClient(None, "private-email", "private-password", refresh_token="private-refresh")
                client.authenticate()
                cognito.renew_access_token.assert_called_once()
                self.assertEqual(cognito.authenticate.call_count, int(revoked))
                self.assertEqual(client.id_token, "private-id")

    def test_auth_challenge_and_transient_refresh_are_sanitised(self):
        """Unsupported challenges and transport errors do not leak credentials."""
        for refresh in (None, "private-refresh"):
            with self.subTest(refresh=bool(refresh)), patch("pycognito.Cognito") as factory:
                cognito = factory.return_value
                cognito.id_token = None
                cognito.renew_access_token.side_effect = RuntimeError("private-refresh")
                cognito.authenticate.side_effect = RuntimeError("private-password")
                client = OptimiseClient(None, "private-email", "private-password", refresh_token=refresh)
                with self.assertRaises((AuthenticationError, PriceFeedError)) as error:
                    client.authenticate()
                self.assertNotIn("private", str(error.exception))
                if refresh:
                    cognito.authenticate.assert_not_called()

    async def test_volunteer_probe_reads_twice_without_control(self):
        """The standalone helper uses only the price client and requests renewal."""
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[3] / "tools" / "eon_optimise_probe.py"
        spec = importlib.util.spec_from_file_location("eon_optimise_probe", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        client = MagicMock()
        client.fetch = AsyncMock(return_value=normalise_response(payload()))
        with patch.object(module, "OptimiseClient", return_value=client):
            rates = await module.probe("private-email", "private-password")
        self.assertEqual(client.fetch.await_count, 2)
        self.assertEqual(client.expires, 0)
        self.assertEqual(set(rates), {"import", "export"})
        self.assertNotIn("private", str(rates))

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

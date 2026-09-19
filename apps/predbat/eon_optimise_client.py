"""Read-only Optimise API; never log response bodies or authentication secrets."""

import asyncio
import time

import aiohttp

from eon_optimise_rates import CLIENT_ID, GRAPHQL_URL, PRICE_QUERY
from eon_optimise_rates import normalise_response


class AuthenticationError(Exception):
    """The user's login needs attention."""


class PriceFeedError(Exception):
    """A response cannot safely be used as tariff data."""


class OptimiseClient:
    """Manage SRP sign-in and refresh tokens without browser dependence."""

    def __init__(self, session, username, password, refresh_token=None):
        """Keep credentials private and use only the known production endpoints."""
        self.session = session
        self.username = username
        self.password = password
        self.refresh_token = refresh_token
        self.id_token = None
        self.expires = 0

    def authenticate(self):
        """Run bounded Cognito SRP/refresh in an executor, never the event loop."""
        try:
            from botocore.config import Config
            from botocore.exceptions import ClientError
            from pycognito import Cognito
        except ImportError:
            raise PriceFeedError("E.ON Optimise requires pycognito; install the Predbat requirements") from None
        client = Cognito(
            "eu-west-2_qbjjnayWT",  # cspell:disable-line
            CLIENT_ID,
            user_pool_region="eu-west-2",
            username=self.username,
            refresh_token=self.refresh_token,
            access_key="unused",
            secret_key="unused",
            botocore_config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 1}),
        )
        if self.refresh_token:
            try:
                client.renew_access_token()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "NotAuthorizedException":
                    raise PriceFeedError("Token renewal temporarily unavailable") from None
                # A revoked/expired session may sign in once with the saved login.
                client.refresh_token = None
            except Exception:
                raise PriceFeedError("Token renewal temporarily unavailable") from None
        if not client.id_token:
            try:
                client.authenticate(self.password)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code in ("NotAuthorizedException", "UserNotFoundException", "UserNotConfirmedException"):
                    raise AuthenticationError("Optimise login rejected") from None
                raise PriceFeedError("Optimise login temporarily unavailable") from None
            except Exception:
                raise AuthenticationError("Optimise login requires user attention") from None
        self.id_token = client.id_token
        self.refresh_token = client.refresh_token or self.refresh_token
        self.expires = time.monotonic() + 2700

    async def fetch(self, executor, query=PRICE_QUERY, normalizer=normalise_response):
        """Read prices; permit one token renewal after a 401, no infinite retries."""
        for attempt in range(2):
            if not self.id_token or time.monotonic() >= self.expires:
                try:
                    await executor(self.authenticate)
                except (AuthenticationError, PriceFeedError):
                    raise
                except Exception:
                    raise PriceFeedError("Optimise authentication temporarily unavailable") from None
            try:
                async with self.session.post(GRAPHQL_URL, json={"query": query}, headers={"Authorization": "Bearer " + self.id_token}, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status == 401:
                        self.id_token = None
                        if attempt == 0:
                            continue
                        raise AuthenticationError("Optimise session rejected")
                    if response.status != 200:
                        raise PriceFeedError(f"Price service HTTP {response.status}")
                    payload = await response.json()
                return normalizer(payload)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                raise PriceFeedError("Price service unavailable") from None
            except (KeyError, TypeError, ValueError, AttributeError):
                raise PriceFeedError("Price service returned an invalid schedule") from None
        raise AuthenticationError("Optimise session unavailable")

# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# OAuth Token Refresh Mixin
# -----------------------------------------------------------------------------

"""Generic OAuth token refresh mixin for PredBat components.

Provides automatic token refresh via the oauth-refresh Supabase edge function.
Components using OAuth should call check_and_refresh_oauth_token() before
making API requests. The edge function owns the refresh chain — PredBat never
calls the provider's refresh endpoint directly.
"""

import inspect
import os
import time
import aiohttp
import asyncio
from datetime import datetime

# Refresh when less than this remains (seconds). Kept well under the shortest token lifetime in
# use: EDF's Kraken OAuth token lives one hour, so the old 2-hour threshold made every single
# request refresh first - one oauth-refresh edge-function call per Kraken query.
TOKEN_REFRESH_THRESHOLD = 30 * 60

# The oauth-refresh edge function (and the provider behind it) can fail transiently —
# an HTTP 502 has been seen in the wild — so a refresh is retried before being given up on.
OAUTH_REFRESH_RETRIES = 3
OAUTH_REFRESH_BACKOFF = 2  # seconds for the first retry, doubled each attempt

# A refresh forced by a 401 can come back with the very token the provider just rejected: the
# oauth-refresh service answers from its stored token while that token has not reached its own
# expiry (GH#6284). Retrying with it achieves nothing, so a same-token answer backs off before the
# next forced refresh, and enough of them in a row latch oauth_failed (the user must reconnect).
OAUTH_SAME_TOKEN_BACKOFF = 5 * 60  # seconds before the first repeat forced refresh, doubled each time (so 5 min, then 10 min, then the latch)
OAUTH_SAME_TOKEN_LIMIT = 3  # consecutive same-token answers to a forced refresh before latching oauth_failed


def refresh_hook_kwargs(hook, reason):
    """Keyword arguments that tell a host oauth_refresh_provider hook a refresh is forced.

    The hook was first called with three positional arguments only, so the force flag is passed
    only to a hook whose signature accepts it; an older host keeps working unchanged.
    """
    if not reason:
        return {}
    try:
        params = inspect.signature(hook).parameters
    except (TypeError, ValueError):
        return {}
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()) or ("force" in params and "reason" in params):
        return {"force": True, "reason": reason}
    return {}


class OAuthMixin:
    """Mixin for components that support OAuth token refresh.

    Requires the following attributes on the host class:
        - self.log: logging function
        - self.auth_method: 'api_key' or 'oauth'
        - self.access_token: current access token (when oauth)
        - self.token_expires_at: expiry timestamp as float (epoch seconds)
        - self.provider_name: provider identifier (e.g. 'fox_ess')
    """

    def _init_oauth(self, auth_method, key, token_expires_at, provider_name):
        """Initialise OAuth state. Call from component's initialise()."""
        self.auth_method = auth_method or "api_key"
        self.provider_name = provider_name
        self.oauth_failed = False
        self._refresh_in_progress = False
        self.token_hash = ""  # server-computed hash, echoed to oauth-refresh for dedup
        self.oauth_failed_reason = ""  # "needs_reauth" or "same_token" once oauth_failed is set
        self._same_token_count = 0  # consecutive forced refreshes that returned the rejected token
        self._forced_refresh_after = 0  # epoch seconds before which a 401 does not force another refresh

        if self.auth_method == "oauth":
            self.access_token = key  # In OAuth mode, 'key' config holds the access_token
            self.token_expires_at = self._parse_expiry(token_expires_at)
        else:
            self.access_token = None
            self.token_expires_at = None

    def _parse_expiry(self, token_expires_at):
        """Parse ISO timestamp to epoch seconds."""
        if not token_expires_at:
            return 0
        try:
            if isinstance(token_expires_at, (int, float)):
                return float(token_expires_at)
            dt = datetime.fromisoformat(token_expires_at.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, AttributeError):
            self.log(f"Warn: Could not parse token_expires_at: {token_expires_at}")
            return 0

    def _token_needs_refresh(self):
        """Check if the OAuth token needs refreshing."""
        if self.auth_method != "oauth":
            return False
        if not self.token_expires_at:
            return True  # No expiry known, refresh to be safe
        return time.time() > (self.token_expires_at - TOKEN_REFRESH_THRESHOLD)

    async def check_and_refresh_oauth_token(self):
        """Check if OAuth token needs refresh and refresh if needed.

        Returns True if token is valid (or was successfully refreshed).
        Returns False if refresh failed (caller should stop API calls).
        """
        if self.auth_method != "oauth":
            return True

        if self.oauth_failed:
            # A same-token latch is the one that can heal: once the rejected token reaches its own
            # expiry an ordinary refresh is allowed, and a new token clears the latch. needs_reauth
            # is the provider refusing the refresh token, which only a reconnect fixes.
            if self.oauth_failed_reason != "same_token" or not self._token_needs_refresh():
                return False

        if not self._token_needs_refresh():
            return True

        if self._refresh_in_progress:
            return True  # Another coroutine is refreshing

        if await self._do_refresh() and not self.oauth_failed:
            return True

        # A proactive refresh is an optimisation, not a gate. The edge function can fail
        # transiently (an HTTP 502 has been seen) while the token already held is still
        # valid for weeks, and skipping the whole cycle over that loses control of the
        # battery for no reason. Carry on with the current token: if it really is dead the
        # request fails auth and handle_oauth_401() refreshes reactively. A token that
        # genuinely needs re-authorisation still stops us here, via oauth_failed.
        if self.oauth_failed or not self.access_token:
            return False
        self.log(f"Warn: OAuth refresh failed for {self.provider_name} but the existing token is still held, continuing with it")
        return True

    async def _do_refresh(self, reason=None):
        """Call the oauth-refresh edge function to get a new access token.

        reason is set when the refresh is forced (e.g. "401"), so the service refreshes even though
        the token it holds has not reached its expiry.
        """
        self._refresh_in_progress = True
        try:
            instance_id = getattr(self.base, "args", {}).get("user_id", "") if hasattr(self, "base") else ""

            refresh_provider = getattr(getattr(self, "base", None), "oauth_refresh_provider", None)
            if refresh_provider is not None:
                data = await refresh_provider(instance_id, self.provider_name, self.token_hash, **refresh_hook_kwargs(refresh_provider, reason))
                if data is None:
                    self.log(f"Warn: OAuth refresh provider returned no answer for {self.provider_name}")
                    return False
                return self._apply_refresh_data(data)

            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")

            if not supabase_url or not supabase_key:
                self.log("Warn: OAuth refresh skipped — SUPABASE_URL or SUPABASE_KEY not set")
                return True  # Don't fail, just skip refresh

            if not instance_id:
                self.log("Warn: OAuth refresh skipped — no instance_id (user_id) in config")
                return True

            url = f"{supabase_url}/functions/v1/oauth-refresh"
            headers = {
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "instance_id": instance_id,
                "provider": self.provider_name,
                "token_hash": self.token_hash,
            }
            if reason:
                payload["force"] = True
                payload["reason"] = reason

            self.log(f"Info: Refreshing OAuth token for {self.provider_name}")

            timeout = aiohttp.ClientTimeout(total=30)
            data = None
            last_error = None
            for attempt in range(OAUTH_REFRESH_RETRIES):
                try:
                    async with self.http_session(timeout=timeout) as session:
                        async with session.post(url, headers=headers, json=payload) as response:
                            if response.status == 200:
                                data = await response.json()
                                break
                            last_error = f"HTTP error {response.status}"
                            # 5xx is the edge function or the provider behind it failing
                            # transiently and is worth retrying; 4xx is a real rejection.
                            if response.status < 500:
                                break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_error = f"network error {e}"
                if attempt < OAUTH_REFRESH_RETRIES - 1:
                    self.log(f"Warn: OAuth refresh {last_error} for {self.provider_name}, retrying")
                    await asyncio.sleep(OAUTH_REFRESH_BACKOFF * (2**attempt))

            if data is None:
                self.log(f"Warn: OAuth refresh {last_error} for {self.provider_name}")
                return False

            return self._apply_refresh_data(data)

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: OAuth refresh network error for {self.provider_name}: {e}")
            return False
        except Exception as e:
            self.log(f"Warn: OAuth refresh unexpected error for {self.provider_name}: {e}")
            return False
        finally:
            self._refresh_in_progress = False

    def _apply_refresh_data(self, data):
        """Apply an oauth-refresh JSON reply to the held token. Returns success."""
        if data.get("success"):
            unchanged = data["access_token"] == self.access_token
            self.access_token = data["access_token"]
            self.token_expires_at = self._parse_expiry(data.get("expires_at"))
            self.token_hash = data.get("token_hash", self.token_hash)
            if unchanged:
                # Not a refresh: the service handed back the token already held
                self.log(f"Info: OAuth refresh for {self.provider_name} returned the token already held, expires at {data.get('expires_at')}")
                return True
            self.log(f"Info: OAuth token refreshed for {self.provider_name}, expires at {data.get('expires_at')}")
            self._same_token_count = 0
            self._forced_refresh_after = 0
            if self.oauth_failed and self.oauth_failed_reason == "same_token":
                self.log(f"Info: OAuth for {self.provider_name} recovered with a new token")
                self.oauth_failed = False
                self.oauth_failed_reason = ""
            return True
        else:
            error = data.get("error", "unknown")
            if error == "needs_reauth":
                self.log(f"Warn: OAuth token for {self.provider_name} needs re-authorization. User must reconnect.")
                self.oauth_failed = True
                self.oauth_failed_reason = "needs_reauth"
                return False
            else:
                self.log(f"Warn: OAuth refresh failed for {self.provider_name}: {error}")
                return False

    async def handle_oauth_401(self):
        """Handle a 401 response when using OAuth. Attempts one refresh and retry.

        Returns True if a new token was obtained (caller should retry the request).
        Returns False if the refresh failed or handed back the token that was just rejected.
        """
        if self.auth_method != "oauth":
            return False

        if self.oauth_failed:
            return False

        now = time.time()
        if now < self._forced_refresh_after:
            self.log(f"Warn: Got 401 with OAuth for {self.provider_name}; the last forced refresh did not produce a new token, not asking again for {int(self._forced_refresh_after - now)}s")
            return False

        self.log(f"Info: Got 401 with OAuth for {self.provider_name}, attempting token refresh")
        rejected_token = self.access_token
        # Force refresh regardless of expiry
        self.token_expires_at = 0
        if not await self._do_refresh(reason="401"):
            return False
        if self.access_token != rejected_token:
            return True

        # The service answered with the token the provider just rejected (or no refresh could be
        # made at all). Retrying it now would only fail again, so back off; a run of these means
        # the connection is gone.
        self._same_token_count += 1
        if self._same_token_count >= OAUTH_SAME_TOKEN_LIMIT:
            self.log(f"Warn: OAuth refresh after 401 did not produce a new token for {self.provider_name} {self._same_token_count} times in a row. User must reconnect.")
            self.oauth_failed = True
            self.oauth_failed_reason = "same_token"
            return False
        backoff = OAUTH_SAME_TOKEN_BACKOFF * (2 ** (self._same_token_count - 1))
        self._forced_refresh_after = now + backoff
        self.log(f"Warn: OAuth refresh after 401 did not produce a new token for {self.provider_name} (the token that was just rejected is still held), not retrying; next forced refresh in {backoff}s")
        return False

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

import os
import time
import aiohttp
import asyncio
from datetime import datetime


# 2 hours in seconds — refresh when less than this remaining
TOKEN_REFRESH_THRESHOLD = 2 * 60 * 60


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
        """Initialize OAuth state. Call from component's initialize()."""
        self.auth_method = auth_method or "api_key"
        self.provider_name = provider_name
        self.oauth_failed = False
        self._refresh_in_progress = False
        self.token_hash = ""  # server-computed hash, echoed to oauth-refresh for dedup

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
            return False

        if not self._token_needs_refresh():
            return True

        if self._refresh_in_progress:
            return True  # Another coroutine is refreshing

        return await self._do_refresh()

    async def _do_refresh(self):
        """Call the oauth-refresh edge function to get a new access token."""
        self._refresh_in_progress = True
        try:
            supabase_url = os.environ.get("SUPABASE_URL", "")
            supabase_key = os.environ.get("SUPABASE_KEY", "")
            instance_id = getattr(self.base, "args", {}).get("user_id", "") if hasattr(self, "base") else ""

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

            self.log(f"Info: Refreshing OAuth token for {self.provider_name}")

            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    if response.status != 200:
                        self.log(f"Warn: OAuth refresh HTTP error {response.status} for {self.provider_name}")
                        return False

                    data = await response.json()

            if data.get("success"):
                self.access_token = data["access_token"]
                self.token_expires_at = self._parse_expiry(data.get("expires_at"))
                self.token_hash = data.get("token_hash", self.token_hash)
                self.log(f"Info: OAuth token refreshed for {self.provider_name}, expires at {data.get('expires_at')}")
                return True
            else:
                error = data.get("error", "unknown")
                if error == "needs_reauth":
                    self.log(f"Warn: OAuth token for {self.provider_name} needs re-authorization. User must reconnect.")
                    self.oauth_failed = True
                    return False
                else:
                    self.log(f"Warn: OAuth refresh failed for {self.provider_name}: {error}")
                    return False

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: OAuth refresh network error for {self.provider_name}: {e}")
            return False
        except Exception as e:
            self.log(f"Warn: OAuth refresh unexpected error for {self.provider_name}: {e}")
            return False
        finally:
            self._refresh_in_progress = False

    async def handle_oauth_401(self):
        """Handle a 401 response when using OAuth. Attempts one refresh and retry.

        Returns True if refresh succeeded (caller should retry the request).
        Returns False if refresh failed.
        """
        if self.auth_method != "oauth":
            return False

        self.log(f"Info: Got 401 with OAuth for {self.provider_name}, attempting token refresh")
        # Force refresh regardless of expiry
        self.token_expires_at = 0
        return await self._do_refresh()

"""Tests for GatewayMQTT MQTT-token refresh on broker auth-failure.

Regression cover for the 2026-06-16 fleet incident: the gateway MQTT JWT has a
24h TTL; when it expired, _mqtt_loop reconnected forever with the same rejected
token (EMQX CONNACK code 134 "Bad user name or password") and never refreshed,
so PredBat lost all gateway control until the pod/config was rebuilt. The fix
makes the reconnect loop force a token refresh when the broker rejects auth.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch


def _bare_gateway():
    """A GatewayMQTT instance with no __init__ side effects, log mocked."""
    from gateway import GatewayMQTT

    gw = GatewayMQTT.__new__(GatewayMQTT)
    gw.log = MagicMock()
    # Refresh backoff state normally set in __init__
    gw._same_token_count = 0
    gw._token_refresh_after = 0
    return gw


class TestIsAuthFailure:
    """_is_auth_failure() distinguishes broker auth rejections from other drops."""

    def test_code_134_bad_credentials_is_auth_failure(self):
        from gateway import GatewayMQTT

        assert GatewayMQTT._is_auth_failure("[code:134] Bad user name or password") is True

    def test_code_135_not_authorized_is_auth_failure(self):
        from gateway import GatewayMQTT

        assert GatewayMQTT._is_auth_failure("[code:135] Not authorized") is True

    def test_not_authorised_british_spelling_is_auth_failure(self):
        from gateway import GatewayMQTT

        assert GatewayMQTT._is_auth_failure("Not authorised") is True

    def test_accepts_exception_object_not_just_string(self):
        from gateway import GatewayMQTT

        err = Exception("[code:134] Bad user name or password")
        assert GatewayMQTT._is_auth_failure(err) is True

    def test_message_iteration_drop_is_not_auth_failure(self):
        from gateway import GatewayMQTT

        assert GatewayMQTT._is_auth_failure("Disconnected during message iteration") is False

    def test_network_refused_is_not_auth_failure(self):
        from gateway import GatewayMQTT

        assert GatewayMQTT._is_auth_failure("[Errno 111] Connection refused") is False

    def test_empty_is_not_auth_failure(self):
        from gateway import GatewayMQTT

        assert GatewayMQTT._is_auth_failure("") is False


class TestApplyRefreshResponse:
    """_apply_refresh_response() updates the in-memory token from an oauth-refresh reply."""

    def test_success_updates_token_and_epoch_expiry(self):
        gw = _bare_gateway()
        gw.mqtt_token = "old.jwt.token"
        gw.mqtt_token_expires_at = 1.0

        ok = gw._apply_refresh_response({"success": True, "access_token": "new.jwt.token", "expires_at": 1781700000})

        assert ok is True
        assert gw.mqtt_token == "new.jwt.token"
        assert gw.mqtt_token_expires_at == 1781700000.0

    def test_success_parses_iso_expiry(self):
        gw = _bare_gateway()
        gw.mqtt_token = "old.jwt.token"
        gw.mqtt_token_expires_at = 1.0

        ok = gw._apply_refresh_response({"success": True, "access_token": "new.jwt.token", "expires_at": "2026-06-17T00:00:00Z"})

        assert ok is True
        assert gw.mqtt_token == "new.jwt.token"
        assert gw.mqtt_token_expires_at > 0

    def test_failure_leaves_token_unchanged(self):
        gw = _bare_gateway()
        gw.mqtt_token = "old.jwt.token"
        gw.mqtt_token_expires_at = 1.0

        ok = gw._apply_refresh_response({"success": False, "error": "needs_reauth"})

        assert ok is False
        assert gw.mqtt_token == "old.jwt.token"

    def test_same_token_after_auth_rejection_is_a_failure(self):
        """A forced refresh that hands back the token the broker refused is not a refresh (GH#6284)."""
        gw = _bare_gateway()
        gw.mqtt_token = "dead.jwt.token"
        gw.mqtt_token_expires_at = 1.0

        ok = gw._apply_refresh_response({"success": True, "access_token": "dead.jwt.token", "expires_at": 1781700000}, rejected_token="dead.jwt.token")

        assert ok is False
        assert not any("MQTT token refreshed" in str(call) for call in gw.log.call_args_list)
        assert any("same token" in str(call) for call in gw.log.call_args_list)


class TestMaybeRefreshOnAuthError:
    """_maybe_refresh_on_auth_error() forces a refresh only for auth failures."""

    def test_auth_failure_triggers_refresh(self):
        gw = _bare_gateway()
        gw._do_token_refresh = AsyncMock(return_value=True)

        result = asyncio.run(gw._maybe_refresh_on_auth_error("[code:134] Bad user name or password"))

        gw._do_token_refresh.assert_awaited_once_with(reason="mqtt_auth_rejected")
        assert result is True

    def test_non_auth_failure_does_not_refresh(self):
        gw = _bare_gateway()
        gw._do_token_refresh = AsyncMock(return_value=True)

        result = asyncio.run(gw._maybe_refresh_on_auth_error("Disconnected during message iteration"))

        gw._do_token_refresh.assert_not_awaited()
        assert result is False


class TestTokenRefreshProvider:
    """_do_token_refresh() uses the host's oauth_refresh_provider when set, else the edge function."""

    def _gateway(self, provider):
        gw = _bare_gateway()
        gw.base = MagicMock()
        gw.base.oauth_refresh_provider = provider
        gw.args = {"user_id": "test-instance-id-1234"}
        gw._refresh_in_progress = False
        gw.mqtt_token = "old.jwt.token"
        gw.mqtt_token_expires_at = 1.0
        return gw

    def _refresh_without_env_or_http(self, gw):
        """Run _do_token_refresh with no SUPABASE env and any HTTP call failing the test."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("aiohttp.ClientSession", side_effect=AssertionError("no HTTP call expected")):
                return asyncio.run(gw._do_token_refresh())

    def test_default_without_env_is_skipped(self):
        gw = self._gateway(None)

        ok = self._refresh_without_env_or_http(gw)

        assert ok is False
        assert gw.mqtt_token == "old.jwt.token"
        assert any("skipped" in str(call) for call in gw.log.call_args_list)

    def test_provider_success_updates_token_without_http(self):
        provider = AsyncMock(return_value={"success": True, "access_token": "new.jwt.token", "expires_at": 1781700000})
        gw = self._gateway(provider)

        ok = self._refresh_without_env_or_http(gw)

        assert ok is True
        provider.assert_awaited_once_with("test-instance-id-1234", "predbat_gateway", "")
        assert gw.mqtt_token == "new.jwt.token"
        assert gw.mqtt_token_expires_at == 1781700000.0
        assert gw._refresh_in_progress is False

    def test_forced_refresh_tells_provider_and_detects_same_token(self):
        """An auth-rejection refresh passes force/reason to a hook that takes them, and a same-token reply fails (GH#6284)."""
        calls = []

        async def provider(instance_id, provider_name, token_hash, force=False, reason=None):
            calls.append((force, reason))
            return {"success": True, "access_token": "old.jwt.token", "expires_at": 1781700000}

        gw = self._gateway(provider)
        with patch.dict("os.environ", {}, clear=True):
            ok = asyncio.run(gw._do_token_refresh(reason="mqtt_auth_rejected"))

        assert ok is False
        assert calls == [(True, "mqtt_auth_rejected")]

    def test_forced_refresh_edge_function_payload_is_marked(self):
        """The edge-function request for an auth-rejection refresh carries force/reason (GH#6284)."""
        from tests.test_infra import create_aiohttp_mock_response, create_aiohttp_mock_session

        gw = self._gateway(None)
        gw.http_session = MagicMock(side_effect=lambda **kwargs: mock_session)
        mock_session = create_aiohttp_mock_session(create_aiohttp_mock_response(status=200, json_data={"success": True, "access_token": "new.jwt.token", "expires_at": 1781700000}))
        with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
            ok = asyncio.run(gw._do_token_refresh(reason="mqtt_auth_rejected"))

        assert ok is True
        payload = mock_session.post.call_args.kwargs["json"]
        assert payload["force"] is True and payload["reason"] == "mqtt_auth_rejected"

    def test_provider_no_answer_keeps_token(self):
        provider = AsyncMock(return_value=None)
        gw = self._gateway(provider)

        ok = self._refresh_without_env_or_http(gw)

        assert ok is False
        provider.assert_awaited_once_with("test-instance-id-1234", "predbat_gateway", "")
        assert gw.mqtt_token == "old.jwt.token"
        # A fallback would take the empty-env "skipped" branch with the same result, so pin the path by its log
        assert any("no answer" in str(call) for call in gw.log.call_args_list)
        assert not any("skipped" in str(call) for call in gw.log.call_args_list)

    def test_provider_failure_reply_keeps_token(self):
        provider = AsyncMock(return_value={"success": False, "error": "needs_reauth"})
        gw = self._gateway(provider)

        ok = self._refresh_without_env_or_http(gw)

        assert ok is False
        provider.assert_awaited_once_with("test-instance-id-1234", "predbat_gateway", "")
        assert gw.mqtt_token == "old.jwt.token"

    def test_provider_error_is_caught(self):
        provider = AsyncMock(side_effect=RuntimeError("host refresh failed"))
        gw = self._gateway(provider)

        ok = self._refresh_without_env_or_http(gw)

        assert ok is False
        provider.assert_awaited_once_with("test-instance-id-1234", "predbat_gateway", "")
        assert gw.mqtt_token == "old.jwt.token"
        assert gw._refresh_in_progress is False


class TestSameTokenRefreshBackoff:
    """A reply carrying the MQTT token already held backs off the next refresh until that token expires (GH#6284)."""

    NOW = 1781600000.0

    def _gateway(self, replies):
        """A gateway whose refresh hook returns each of replies in turn, on a fixed wall clock."""
        gw = _bare_gateway()
        gw.base = MagicMock()
        gw.base.oauth_refresh_provider = AsyncMock(side_effect=replies)
        gw.args = {"user_id": "test-instance-id-1234"}
        gw._refresh_in_progress = False
        gw.mqtt_token = "held.jwt.token"
        gw.mqtt_token_expires_at = self.NOW + 1800  # inside token_needs_refresh()'s 1-hour window
        gw.clock_now = self.NOW
        gw.wall_time = lambda: gw.clock_now
        return gw

    def _refresh(self, gw, reason=None):
        """Run _do_token_refresh with no SUPABASE env."""
        with patch.dict("os.environ", {}, clear=True):
            return asyncio.run(gw._do_token_refresh(reason=reason))

    def test_proactive_same_token_is_not_asked_again_every_cycle(self):
        """The near-expiry check waits out the backoff instead of re-asking every 60 s cycle."""
        held = {"success": True, "access_token": "held.jwt.token", "expires_at": self.NOW + 1800}
        gw = self._gateway([held, held])

        self._refresh(gw)
        assert gw._token_refresh_after == self.NOW + 300
        assert any("already held" in str(call) for call in gw.log.call_args_list)
        assert not any("MQTT token refreshed" in str(call) for call in gw.log.call_args_list)

        gw.clock_now = self.NOW + 60  # the next component cycle
        asyncio.run(gw._check_token_refresh())
        assert gw.base.oauth_refresh_provider.await_count == 1, "a same-token reply must not be re-asked on the next cycle"

        gw.clock_now = self.NOW + 301
        asyncio.run(gw._check_token_refresh())
        assert gw.base.oauth_refresh_provider.await_count == 2
        assert gw._token_refresh_after == self.NOW + 301 + 600, "the second repeat waits twice as long"

    def test_backoff_doubles_to_the_cap(self):
        """Each repeat doubles the wait: 5, 10, 20 min, then 30 min from then on."""
        gw = _bare_gateway()
        gw.wall_time = lambda: 0
        waits = [gw._back_off_same_token() for _ in range(6)]
        assert waits == [300, 600, 1200, 1800, 1800, 1800]

    def test_forced_refresh_inside_backoff_is_skipped(self):
        """A broker auth rejection inside the backoff does not ask the service again."""
        gw = self._gateway([])
        gw._token_refresh_after = self.NOW + 300

        ok = self._refresh(gw, reason="mqtt_auth_rejected")

        assert ok is False
        gw.base.oauth_refresh_provider.assert_not_awaited()
        assert any("not asking again" in str(call) for call in gw.log.call_args_list)

    def test_backoff_ends_once_the_held_token_expires(self):
        """Once the held token has expired the backoff no longer applies, and a new token resets it."""
        fresh = {"success": True, "access_token": "new.jwt.token", "expires_at": self.NOW + 86400}
        gw = self._gateway([fresh])
        gw._same_token_count = 2
        gw._token_refresh_after = self.NOW + 600
        gw.mqtt_token_expires_at = self.NOW - 1  # expired, so the service can mint a new one

        ok = self._refresh(gw, reason="mqtt_auth_rejected")

        assert ok is True
        assert gw.mqtt_token == "new.jwt.token"
        assert gw._same_token_count == 0 and gw._token_refresh_after == 0, "a new token clears the backoff"

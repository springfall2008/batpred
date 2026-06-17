"""Tests for GatewayMQTT MQTT-token refresh on broker auth-failure.

Regression cover for the 2026-06-16 fleet incident: the gateway MQTT JWT has a
24h TTL; when it expired, _mqtt_loop reconnected forever with the same rejected
token (EMQX CONNACK code 134 "Bad user name or password") and never refreshed,
so PredBat lost all gateway control until the pod/config was rebuilt. The fix
makes the reconnect loop force a token refresh when the broker rejects auth.
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock


def _bare_gateway():
    """A GatewayMQTT instance with no __init__ side effects, log mocked."""
    from gateway import GatewayMQTT

    gw = GatewayMQTT.__new__(GatewayMQTT)
    gw.log = MagicMock()
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


class TestMaybeRefreshOnAuthError:
    """_maybe_refresh_on_auth_error() forces a refresh only for auth failures."""

    def test_auth_failure_triggers_refresh(self):
        gw = _bare_gateway()
        gw._do_token_refresh = AsyncMock(return_value=True)

        result = asyncio.run(gw._maybe_refresh_on_auth_error("[code:134] Bad user name or password"))

        gw._do_token_refresh.assert_awaited_once()
        assert result is True

    def test_non_auth_failure_does_not_refresh(self):
        gw = _bare_gateway()
        gw._do_token_refresh = AsyncMock(return_value=True)

        result = asyncio.run(gw._maybe_refresh_on_auth_error("Disconnected during message iteration"))

        gw._do_token_refresh.assert_not_awaited()
        assert result is False

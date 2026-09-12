"""OAuth client-credentials tests, using a mock transport (no network)."""

from __future__ import annotations

import json

import httpx
import pytest

from wcl_mplus import redaction
from wcl_mplus.auth import AuthError, Token, TokenProvider
from wcl_mplus.settings import ConfigError, Settings

TOKEN_VALUE = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.payload.signature"


def transport(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/oauth/token"
    body = request.content.decode()
    assert "grant_type=client_credentials" in body
    # Credentials must travel as HTTP Basic auth, not in the body.
    assert "client_secret" not in body
    assert request.headers.get("Authorization", "").startswith("Basic ")
    return httpx.Response(
        200, json={"access_token": TOKEN_VALUE, "expires_in": 3600, "token_type": "Bearer"}
    )


def test_successful_token_fetch(settings):
    provider = TokenProvider(settings, client=transport(ok_handler))
    token = provider.token()
    assert token.value == TOKEN_VALUE
    assert token.seconds_remaining > 3000
    assert provider.auth_header()["Authorization"] == f"Bearer {TOKEN_VALUE}"


def test_token_is_registered_for_redaction(settings):
    TokenProvider(settings, client=transport(ok_handler)).token()
    assert TOKEN_VALUE not in redaction.redact(f"leaked {TOKEN_VALUE}")


def test_token_repr_hides_value(settings):
    token = TokenProvider(settings, client=transport(ok_handler)).token()
    assert TOKEN_VALUE not in repr(token)
    assert TOKEN_VALUE not in str(token)
    assert "seconds_remaining" in repr(token)


def test_token_is_cached_between_calls(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return ok_handler(request)

    provider = TokenProvider(settings, client=transport(handler))
    provider.token()
    provider.token()
    assert len(calls) == 1, "second call served from memory"


def test_force_refresh_refetches(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return ok_handler(request)

    provider = TokenProvider(settings, client=transport(handler))
    provider.token()
    provider.token(force_refresh=True)
    assert len(calls) == 2


def test_expired_token_is_refetched(settings):
    provider = TokenProvider(settings, client=transport(ok_handler))
    token = provider.token()
    token.expires_at = 0  # force expiry
    assert provider.token() is not token


def test_expiry_margin_refreshes_early(settings):
    """A token about to expire must not be handed to a long request."""

    def handler(request):
        return httpx.Response(200, json={"access_token": TOKEN_VALUE, "expires_in": 30})

    provider = TokenProvider(settings, client=transport(handler))
    token = provider.token()
    assert token.expired, "a 30s token is inside the refresh margin"


def test_nonsense_expiry_falls_back_to_an_hour(settings):
    def handler(request):
        return httpx.Response(200, json={"access_token": TOKEN_VALUE, "expires_in": -5})

    token = TokenProvider(settings, client=transport(handler)).token()
    assert 3400 < token.seconds_remaining <= 3600


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_credentials_raise_actionable_error(settings, status):
    def handler(request):
        return httpx.Response(status, json={"error": "invalid_client"})

    with pytest.raises(AuthError) as excinfo:
        TokenProvider(settings, client=transport(handler)).token()
    message = str(excinfo.value)
    assert "rejected the credentials" in message
    assert "api/clients" in message


def test_error_response_body_cannot_leak_a_token(settings):
    """Even a server that echoes a token back must not have it logged."""
    leaked = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    def handler(request):
        return httpx.Response(400, content=json.dumps({"access_token": leaked}))

    with pytest.raises(AuthError) as excinfo:
        TokenProvider(settings, client=transport(handler)).token()
    assert leaked not in str(excinfo.value)


def test_server_error_is_reported_as_transient(settings):
    def handler(request):
        return httpx.Response(503, text="upstream down")

    with pytest.raises(AuthError, match="transient"):
        TokenProvider(settings, client=transport(handler)).token()


def test_missing_access_token_field(settings):
    def handler(request):
        return httpx.Response(200, json={"token": "wrong-field-name"})

    with pytest.raises(AuthError, match="did not contain an 'access_token'"):
        TokenProvider(settings, client=transport(handler)).token()


def test_non_json_token_response(settings):
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(AuthError, match="non-JSON"):
        TokenProvider(settings, client=transport(handler)).token()


def test_network_failure_is_wrapped_and_redacted(settings):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(AuthError, match="Could not reach"):
        TokenProvider(settings, client=transport(handler)).token()


def test_no_credentials_raises_config_error(tmp_path):
    settings = Settings(client_id=None, client_secret=None, data_dir=tmp_path)
    with pytest.raises(ConfigError):
        TokenProvider(settings, client=transport(ok_handler)).token()


def test_token_registers_only_long_values():
    Token(value="short", expires_at=9e18)
    assert redaction.registered_secret_count() == 0

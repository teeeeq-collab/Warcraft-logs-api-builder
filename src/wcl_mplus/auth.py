"""OAuth2 client-credentials authentication against the public WCL v2 API.

Scope note: this is the *public* API client-credentials flow only. It reads
public reports. User-authorization (private reports) is deliberately out of
scope for v1 (brief section 5).

Security properties this module is responsible for:

* the client secret is sent only in the token request body, over HTTPS;
* the access token is never logged, never returned in a repr, and never
  written to the raw cache;
* every error raised is a `RedactedError` subclass, so the secret cannot
  escape through a traceback even if a library embedded the request body.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .redaction import RedactedError, redact, register_secret
from .settings import Settings

logger = logging.getLogger(__name__)

#: Refresh this many seconds before nominal expiry, so a long request cannot
#: start with a token that expires mid-flight.
_EXPIRY_MARGIN_SECONDS = 120


class AuthError(RedactedError):
    """Authentication failed. Not retryable without fixing credentials."""


@dataclass
class Token:
    """An access token and its expiry.

    `value` is registered for redaction on construction, so it is scrubbed
    from any later log line or exception.
    """

    value: str
    expires_at: float
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        register_secret(self.value)

    @property
    def expired(self) -> bool:
        return time.time() >= (self.expires_at - _EXPIRY_MARGIN_SECONDS)

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.expires_at - time.time()))

    def header(self) -> dict[str, str]:
        return {"Authorization": f"{self.token_type} {self.value}"}

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Token(token_type={self.token_type!r}, seconds_remaining={self.seconds_remaining})"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return self.__repr__()


class TokenProvider:
    """Fetches and caches an access token in memory.

    In-memory only, by choice: persisting a bearer token to disk would create
    another artifact that must be kept out of git and out of the raw cache.
    Tokens are cheap to re-request.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._token: Token | None = None

    # -- lifecycle --------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._settings.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> TokenProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- token ------------------------------------------------------------

    def token(self, *, force_refresh: bool = False) -> Token:
        """Return a valid token, fetching one if needed."""
        if force_refresh or self._token is None or self._token.expired:
            self._token = self._fetch()
        return self._token

    def auth_header(self) -> dict[str, str]:
        return self.token().header()

    def _fetch(self) -> Token:
        client_id, client_secret = self._settings.require_credentials()
        logger.debug("Requesting access token from %s", self._settings.token_url)

        try:
            response = self._http().post(
                self._settings.token_url,
                data={"grant_type": "client_credentials"},
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            # Re-raise as a redacted error: httpx exception text can include the
            # request, and we never want the body near a traceback.
            raise AuthError(
                f"Could not reach the Warcraft Logs token endpoint "
                f"({self._settings.token_url}): {type(exc).__name__}: {redact(exc)}"
            ) from None

        if response.status_code in (400, 401, 403):
            raise AuthError(
                f"Warcraft Logs rejected the credentials (HTTP {response.status_code}). "
                "Check WCL_CLIENT_ID / WCL_CLIENT_SECRET in .env, and that the client "
                "exists at https://www.warcraftlogs.com/api/clients/ . "
                f"Response: {redact(_short_body(response))}"
            )
        if response.status_code >= 500:
            raise AuthError(
                f"Warcraft Logs token endpoint returned HTTP {response.status_code}. "
                "This is usually transient; retry shortly."
            )
        if response.status_code != 200:
            raise AuthError(
                f"Unexpected HTTP {response.status_code} from the token endpoint: "
                f"{redact(_short_body(response))}"
            )

        try:
            payload = response.json()
        except ValueError:
            raise AuthError(
                "Token endpoint returned a non-JSON body; cannot authenticate."
            ) from None

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            # Deliberately does not echo the payload: it may contain the token
            # under an unexpected key.
            raise AuthError(
                "Token response did not contain an 'access_token' string. "
                f"Keys present: {_payload_keys(payload)}"
            )

        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            # Conservative fallback rather than trusting an odd value.
            expires_in = 3600
        token = Token(
            value=access_token,
            expires_at=time.time() + float(expires_in),
            token_type=str(payload.get("token_type") or "Bearer"),
        )
        logger.info("Obtained access token (valid ~%ss)", int(expires_in))
        return token


def _short_body(response: httpx.Response, limit: int = 300) -> str:
    """First `limit` characters of a response body, for error context."""
    try:
        text = response.text
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def _payload_keys(payload: object) -> str:
    """Describe a token payload's shape without echoing its values.

    The payload may hold the token under an unexpected key, so only the key
    names (or the type, if it is not an object) are ever reported.
    """
    if isinstance(payload, dict):
        return str(sorted(payload))
    return type(payload).__name__

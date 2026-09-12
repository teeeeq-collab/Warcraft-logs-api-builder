"""Secret redaction.

Every credential this project touches is registered here the moment it is
loaded, and `redact()` is applied to log records, exception text and anything
written to the raw cache. The goal is that a client secret or bearer token
cannot reach a terminal, a log file, a cached response or a crash traceback
even when an unexpected code path stringifies an object that holds one.

Two layers:

1. **Exact-value redaction** — values registered via `register_secret()`.
   This catches the client secret and any access token we obtained.
2. **Pattern redaction** — `Bearer <...>`, `access_token` / `client_secret`
   JSON and form fields, and `Authorization` headers. This catches secrets we
   never saw (e.g. a token inside a third-party error payload).

Pattern redaction matters because layer 1 only works for values we know about.
"""

from __future__ import annotations

import logging
import re
from typing import Any

PLACEHOLDER = "***REDACTED***"

# Values short enough to appear coincidentally in ordinary text are not worth
# redacting globally -- doing so would mangle unrelated output.
_MIN_SECRET_LENGTH = 8

_secrets: set[str] = set()

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer <token>  /  "Bearer <token>"
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-~+/=]{8,}"), f"Bearer {PLACEHOLDER}"),
    # Authorization header in a dict/JSON dump, single or double quoted.
    (
        re.compile(r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*)(['\"])[^'\"]{8,}(['\"])"),
        rf"\1\2{PLACEHOLDER}\3",
    ),
    # JSON-ish secret-bearing keys: "access_token": "...", 'client_secret': '...'
    (
        re.compile(
            r"(?i)(['\"]?(?:access_token|refresh_token|client_secret|wcl_client_secret)"
            r"['\"]?\s*[:=]\s*)(['\"])[^'\"]{4,}(['\"])"
        ),
        rf"\1\2{PLACEHOLDER}\3",
    ),
    # Form-encoded: client_secret=abc&...
    (
        re.compile(
            r"(?i)\b(access_token|refresh_token|client_secret|wcl_client_secret)=[^&\s'\"]{4,}"
        ),
        rf"\1={PLACEHOLDER}",
    ),
)


def register_secret(value: str | None) -> None:
    """Register a literal secret so it is scrubbed from all future output."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _secrets.add(value)


def registered_secret_count() -> int:
    """Number of registered secrets. Never exposes the values themselves."""
    return len(_secrets)


def clear_secrets() -> None:
    """Test helper. Not used in normal operation."""
    _secrets.clear()


def redact(value: Any) -> str:
    """Return ``value`` as a string with known secrets and secret-shaped
    substrings replaced by a placeholder."""
    text = value if isinstance(value, str) else str(value)
    # Longest first, so a token that contains a shorter secret still redacts fully.
    for secret in sorted(_secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, PLACEHOLDER)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Redact a header/param mapping by key name as well as by value.

    Used before anything resembling a request is logged or cached.
    """
    sensitive = {
        "authorization",
        "access_token",
        "refresh_token",
        "client_secret",
        "client_id",
        "cookie",
        "set-cookie",
    }
    out: dict[str, Any] = {}
    for key, val in data.items():
        if key.lower() in sensitive:
            out[key] = PLACEHOLDER
        elif isinstance(val, str):
            out[key] = redact(val)
        elif isinstance(val, dict):
            out[key] = redact_mapping(val)
        else:
            out[key] = val
    return out


class RedactingFilter(logging.Filter):
    """Logging filter that scrubs secrets from messages, args and exc text."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            # Only string args are rewritten. Coercing numbers to strings here
            # would break %d / %f formatting in the message template, and a
            # non-string cannot carry a secret anyway.
            if isinstance(record.args, dict):
                record.args = {
                    k: (redact(v) if isinstance(v, str) else v) for k, v in record.args.items()
                }
            else:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
        # Pre-render the exception so the scrubbed text is what gets emitted.
        if record.exc_info:
            logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_logging_redaction(logger: logging.Logger | None = None) -> None:
    """Attach the redacting filter to a logger and its handlers.

    Filters on a logger do not apply to records propagated from children, so
    the filter is attached to handlers too -- that is where every record for
    this process ends up.
    """
    target = logger or logging.getLogger()
    flt = RedactingFilter()
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(flt)
    for handler in target.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(flt)


class RedactedError(Exception):
    """Base for project errors. Its rendered message is always scrubbed.

    Raised in place of letting a library exception carry a URL, header or body
    that might embed a token.
    """

    def __init__(self, message: str, *args: object) -> None:
        super().__init__(redact(message), *args)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return redact(super().__str__())

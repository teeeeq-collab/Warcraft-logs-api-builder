"""Secret-redaction tests.

These are security tests: a failure here means a credential can reach a
terminal, a log file, or a cached dataset that gets shared with an analyst.
"""

from __future__ import annotations

import logging

from wcl_mplus import redaction
from wcl_mplus.redaction import (
    PLACEHOLDER,
    RedactedError,
    install_logging_redaction,
    redact,
    redact_mapping,
    register_secret,
)

SECRET = "b7f3c9a1e5d24f8091ab6c7d8e9f0011"


def test_registered_secret_is_redacted_everywhere():
    register_secret(SECRET)
    for template in (
        "client_secret={}",
        "oops {} oops",
        '{{"secret": "{}"}}',
        "https://example.test/?s={}",
    ):
        assert SECRET not in redact(template.format(SECRET))


def test_bearer_token_redacted_without_registration():
    """Pattern layer catches tokens we never saw ourselves."""
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc"
    out = redact(text)
    assert "eyJhbGci" not in out
    assert PLACEHOLDER in out


def test_unregistered_secret_keys_redacted_by_pattern():
    assert "swordfish" not in redact('{"access_token": "swordfish1234"}')
    assert "hunter2hunter2" not in redact("client_secret=hunter2hunter2&grant_type=x")


def test_mapping_redacts_by_key_name():
    out = redact_mapping(
        {
            "Authorization": "Bearer abc123xyz",
            "client_id": "public-id",
            "Accept": "application/json",
        }
    )
    assert out["Authorization"] == PLACEHOLDER
    assert out["client_id"] == PLACEHOLDER, "client_id is not secret but is still identifying"
    assert out["Accept"] == "application/json"


def test_nested_mapping_redacted():
    out = redact_mapping({"headers": {"authorization": "Bearer zzz"}, "ok": 1})
    assert out["headers"]["authorization"] == PLACEHOLDER


def test_short_values_are_not_registered():
    """Short strings would mangle unrelated output if globally replaced."""
    register_secret("abc")
    assert redaction.registered_secret_count() == 0
    assert redact("abc def") == "abc def"


def test_redacted_error_message_is_scrubbed():
    register_secret(SECRET)
    err = RedactedError(f"failed with token {SECRET}")
    assert SECRET not in str(err)
    assert SECRET not in repr(err.args)


def test_logging_filter_scrubs_message_args_and_traceback(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("wcl_mplus.test_redaction")
    logger.propagate = True
    install_logging_redaction(logger)

    with caplog.at_level(logging.DEBUG):
        logger.info("token is %s", SECRET)
        try:
            raise ValueError(f"boom {SECRET}")
        except ValueError:
            logger.exception("failed")

    rendered = "\n".join(
        [r.getMessage() for r in caplog.records] + [r.exc_text or "" for r in caplog.records]
    )
    assert SECRET not in rendered
    assert PLACEHOLDER in rendered


def test_count_never_exposes_values():
    register_secret(SECRET)
    assert redaction.registered_secret_count() == 1
    assert SECRET not in str(redaction.registered_secret_count())


def test_longest_secret_wins_when_nested():
    register_secret("short-secret-aaa")
    register_secret("short-secret-aaa-and-more-bbb")
    out = redact("value=short-secret-aaa-and-more-bbb")
    assert "short-secret" not in out


def test_numeric_log_args_are_not_stringified(caplog):
    """Regression: coercing args to str broke %d/%f formatting in log messages."""
    register_secret(SECRET)
    logger = logging.getLogger("wcl_mplus.test_redaction.numeric")
    logger.propagate = True
    install_logging_redaction(logger)
    with caplog.at_level(logging.INFO):
        logger.warning("%d line(s) in %s, %.1f%% done", 3, "file.txt", 42.5)
    assert caplog.records[-1].getMessage() == "3 line(s) in file.txt, 42.5% done"


def test_string_args_are_still_redacted_alongside_numbers(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("wcl_mplus.test_redaction.mixed")
    logger.propagate = True
    install_logging_redaction(logger)
    with caplog.at_level(logging.INFO):
        logger.warning("attempt %d with %s", 2, SECRET)
    message = caplog.records[-1].getMessage()
    assert SECRET not in message
    assert message.startswith("attempt 2 with")


def test_dict_style_log_args_preserve_numbers(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("wcl_mplus.test_redaction.dict")
    logger.propagate = True
    install_logging_redaction(logger)
    with caplog.at_level(logging.INFO):
        logger.warning("%(count)d of %(name)s", {"count": 7, "name": SECRET})
    message = caplog.records[-1].getMessage()
    assert message.startswith("7 of")
    assert SECRET not in message

"""Credential loading and configuration tests."""

from __future__ import annotations

import pytest

from wcl_mplus import redaction
from wcl_mplus.settings import ConfigError, Settings, load_dotenv, parse_dotenv


def test_parse_dotenv_formats():
    parsed = parse_dotenv(
        "\n".join(
            [
                "# a comment",
                "",
                "PLAIN=value",
                "export EXPORTED=value2",
                'QUOTED="has spaces"',
                "SQUOTED='single'",
                "TRAILING=value # trailing comment",
                "EMPTY=",
                "NOEQUALS",
            ]
        )
    )
    assert parsed == {
        "PLAIN": "value",
        "EXPORTED": "value2",
        "QUOTED": "has spaces",
        "SQUOTED": "single",
        "TRAILING": "value",
        "EMPTY": "",
    }


def test_parse_dotenv_keeps_special_characters_in_quoted_secret():
    """A pasted secret containing $ or # must survive verbatim."""
    parsed = parse_dotenv('WCL_CLIENT_SECRET="a$b#c=d"')
    assert parsed["WCL_CLIENT_SECRET"] == "a$b#c=d"


def test_real_environment_wins_over_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WCL_CLIENT_ID=from-file\n")
    monkeypatch.setenv("WCL_CLIENT_ID", "from-environment")
    load_dotenv(env_file)
    import os

    assert os.environ["WCL_CLIENT_ID"] == "from-environment"


def test_dotenv_override_flag(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("WCL_CLIENT_ID=from-file\n")
    monkeypatch.setenv("WCL_CLIENT_ID", "from-environment")
    load_dotenv(env_file, override=True)
    import os

    assert os.environ["WCL_CLIENT_ID"] == "from-file"


def test_missing_dotenv_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == []


def test_secret_is_registered_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "id-value")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "secret-value-long-enough")
    Settings.load(dotenv=False)
    assert redaction.registered_secret_count() == 1
    assert "secret-value-long-enough" not in redaction.redact("x secret-value-long-enough y")


def test_repr_and_summary_never_include_the_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("WCL_CLIENT_ID", "id-value")
    monkeypatch.setenv("WCL_CLIENT_SECRET", "secret-value-long-enough")
    settings = Settings.load(dotenv=False)
    assert "secret-value-long-enough" not in repr(settings)
    assert "secret-value-long-enough" not in str(settings.safe_summary())
    assert settings.safe_summary()["client_secret_present"] is True


def test_require_credentials_error_is_actionable():
    settings = Settings(client_id=None, client_secret=None)
    with pytest.raises(ConfigError) as excinfo:
        settings.require_credentials()
    message = str(excinfo.value)
    assert "WCL_CLIENT_ID" in message and "WCL_CLIENT_SECRET" in message
    assert ".env.example" in message


def test_bad_integer_env_is_rejected(monkeypatch):
    monkeypatch.setenv("WCLMPLUS_CONCURRENCY", "lots")
    with pytest.raises(ConfigError, match="must be an integer"):
        Settings.load(dotenv=False)


def test_relative_data_dir_resolves_under_project_root(monkeypatch):
    monkeypatch.setenv("WCLMPLUS_DATA_DIR", "somewhere/else")
    settings = Settings.load(dotenv=False)
    assert settings.data_dir.is_absolute()
    assert settings.data_dir.parts[-2:] == ("somewhere", "else")


def test_ensure_dirs_creates_layout(settings):
    settings.ensure_dirs()
    assert settings.raw_cache_dir.is_dir()
    assert settings.db_dir.is_dir()
    assert settings.exports_dir.is_dir()

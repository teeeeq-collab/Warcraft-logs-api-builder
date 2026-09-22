"""Shared test fixtures.

Every test in this suite runs with no credentials and no network. Live-API
checks are marked `live` and excluded by default (see pyproject).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from wcl_mplus import redaction  # noqa: E402
from wcl_mplus.settings import Settings  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    """Keep registered secrets from leaking between tests."""
    redaction.clear_secrets()
    yield
    redaction.clear_secrets()


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Ensure a developer's real .env cannot influence a test result."""
    for name in ("WCL_CLIENT_ID", "WCL_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings pointed at a throwaway data directory."""
    return Settings(
        client_id="test-client-id",
        client_secret="test-client-secret-value",
        data_dir=tmp_path / "data",
        max_retries=3,
        timeout_seconds=5,
    )


class FakeTokens:
    """Token provider for offline tests. Never touches the network."""

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}

    def token(self, force_refresh: bool = False):
        from wcl_mplus.auth import Token

        return Token(access_token="test-token", expires_at=9e9)

    def close(self) -> None:
        pass

"""Configuration and credential loading.

Credentials come from the environment, optionally seeded from a git-ignored
`.env` file. They are registered with the redaction layer at load time, before
any network or logging happens, so a later crash cannot leak them.

A deliberately small `.env` parser is used instead of a dependency: the format
is trivial, and it keeps the credential path auditable in one short function.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redaction import RedactedError, register_secret

DEFAULT_API_URL = "https://www.warcraftlogs.com/api/v2/client"
DEFAULT_TOKEN_URL = "https://www.warcraftlogs.com/oauth/token"


class ConfigError(RedactedError):
    """Configuration or credential problem. Never retryable."""


def project_root() -> Path:
    """Repository root, derived from this file's location."""
    return Path(__file__).resolve().parents[2]


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse a minimal `.env` document.

    Supports `KEY=VALUE`, `export KEY=VALUE`, `#` comments, blank lines, and
    single/double quoted values. Values are not shell-expanded -- a literal
    `$VAR` stays literal, which is what a pasted secret needs.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # Strip a trailing inline comment only on unquoted values.
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        if key:
            result[key] = value
    return result


def load_dotenv(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Load `.env` into `os.environ`. Returns the names of keys applied.

    Real environment variables win by default: a CI or shell export should not
    be silently shadowed by a stale file.
    """
    env_path = path or (project_root() / ".env")
    if not env_path.is_file():
        return []
    applied: list[str] = []
    for key, value in parse_dotenv(env_path.read_text(encoding="utf-8")).items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass
class Settings:
    """Resolved runtime settings.

    `client_secret` is held here but is registered for redaction, so even an
    accidental `print(settings)` or repr in a traceback is scrubbed.
    """

    client_id: str | None
    client_secret: str | None
    api_url: str = DEFAULT_API_URL
    token_url: str = DEFAULT_TOKEN_URL
    data_dir: Path = field(default_factory=lambda: project_root() / "data")
    config_dir: Path = field(default_factory=lambda: project_root() / "config")
    queries_dir: Path = field(default_factory=lambda: project_root() / "queries")
    concurrency: int = 2
    min_points_reserve: int = 200
    max_retries: int = 5
    timeout_seconds: int = 60

    @classmethod
    def load(cls, *, dotenv: bool = True, dotenv_path: Path | None = None) -> Settings:
        if dotenv:
            load_dotenv(dotenv_path)
        client_id = os.environ.get("WCL_CLIENT_ID") or None
        client_secret = os.environ.get("WCL_CLIENT_SECRET") or None

        # Register before anything else can fail or log.
        register_secret(client_secret)

        data_dir = Path(os.environ.get("WCLMPLUS_DATA_DIR") or (project_root() / "data"))
        if not data_dir.is_absolute():
            data_dir = project_root() / data_dir

        return cls(
            client_id=client_id,
            client_secret=client_secret,
            api_url=os.environ.get("WCL_API_URL") or DEFAULT_API_URL,
            token_url=os.environ.get("WCL_TOKEN_URL") or DEFAULT_TOKEN_URL,
            data_dir=data_dir,
            concurrency=_env_int("WCLMPLUS_CONCURRENCY", 2),
            min_points_reserve=_env_int("WCLMPLUS_MIN_POINTS_RESERVE", 200),
            max_retries=_env_int("WCLMPLUS_MAX_RETRIES", 5),
            timeout_seconds=_env_int("WCLMPLUS_TIMEOUT_SECONDS", 60),
        )

    # -- credential state -------------------------------------------------

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def require_credentials(self) -> tuple[str, str]:
        """Return (client_id, client_secret) or raise an actionable error."""
        if self.client_id and self.client_secret:
            return self.client_id, self.client_secret
        missing = [
            name
            for name, val in (
                ("WCL_CLIENT_ID", self.client_id),
                ("WCL_CLIENT_SECRET", self.client_secret),
            )
            if not val
        ]
        raise ConfigError(
            "Missing Warcraft Logs credentials: " + ", ".join(missing) + ".\n\nFix:\n"
            "  1. Create a v2 API client at https://www.warcraftlogs.com/api/clients/\n"
            "  2. cp .env.example .env\n"
            "  3. Put your Client ID and Client Secret in .env (it is git-ignored)\n"
            "  4. Re-run: wclmplus auth-check\n"
        )

    # -- derived paths ----------------------------------------------------

    @property
    def raw_cache_dir(self) -> Path:
        return self.data_dir / "raw_cache"

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def fixtures_dir(self) -> Path:
        return project_root() / "tests" / "fixtures"

    def ensure_dirs(self) -> None:
        for path in (self.raw_cache_dir, self.db_dir, self.exports_dir):
            path.mkdir(parents=True, exist_ok=True)

    def safe_summary(self) -> dict[str, Any]:
        """Loggable settings view. Secrets reduced to presence booleans."""
        return {
            "api_url": self.api_url,
            "token_url": self.token_url,
            "client_id_present": bool(self.client_id),
            "client_secret_present": bool(self.client_secret),
            "data_dir": str(self.data_dir),
            "concurrency": self.concurrency,
            "min_points_reserve": self.min_points_reserve,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"Settings({self.safe_summary()})"

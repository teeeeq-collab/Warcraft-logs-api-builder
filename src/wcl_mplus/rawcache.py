"""Sanitized raw-response cache.

Mandatory by design (brief section 12): if normalization logic turns out to be
wrong, the database must be rebuildable from local files without spending API
quota again. Everything the collector learns from the API lands here first.

Guarantees:

* Cache identity includes the report code, the logical query kind, every query
  parameter, and the query-set version -- so bumping `QUERY_VERSION` after
  changing a `.graphql` file cannot silently serve stale responses.
* Nothing is written until the serialized bytes have been scanned for
  registered secrets. A hit raises instead of writing, which turns a
  credential leak into a loud failure rather than a file on disk.
* Request parameters are stored redacted; `Authorization` headers are never
  passed in and are stripped defensively if they ever are.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import RedactedError, redact_mapping
from .version import QUERY_VERSION, provenance

logger = logging.getLogger(__name__)

#: Envelope format version. Bump if the envelope layout itself changes.
CACHE_FORMAT_VERSION = 1

_FORBIDDEN_PARAM_KEYS = {"authorization", "access_token", "client_secret", "token"}


class CacheSecurityError(RedactedError):
    """A cache write was blocked because it would have persisted a secret."""


def canonical_params(params: dict[str, Any]) -> str:
    """Deterministic JSON for cache-key hashing.

    Sorted keys and no whitespace, so logically identical parameter sets always
    produce the same key regardless of dict ordering.
    """
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def cache_key(kind: str, params: dict[str, Any], *, query_version: int = QUERY_VERSION) -> str:
    """Stable cache key for a logical request."""
    payload = canonical_params({"kind": kind, "qv": query_version, "params": params})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _safe_segment(value: str | None, fallback: str = "_global") -> str:
    """Filesystem-safe path segment from an untrusted identifier."""
    if not value:
        return fallback
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in value)
    return cleaned[:64] or fallback


@dataclass
class CacheEntry:
    path: Path
    envelope: dict[str, Any]

    @property
    def response(self) -> Any:
        return self.envelope.get("response")

    @property
    def fetched_at(self) -> float | None:
        value = self.envelope.get("fetched_at")
        return float(value) if isinstance(value, (int, float)) else None


class RawCache:
    """Gzipped JSON cache on local disk."""

    def __init__(self, root: Path, *, query_version: int = QUERY_VERSION) -> None:
        self.root = Path(root)
        self.query_version = query_version

    # -- paths ------------------------------------------------------------

    def path_for(
        self, kind: str, params: dict[str, Any], *, report_code: str | None = None
    ) -> Path:
        key = cache_key(kind, params, query_version=self.query_version)
        return (
            self.root
            / _safe_segment(kind, "unknown")
            / _safe_segment(report_code)
            / f"{key}.json.gz"
        )

    # -- read -------------------------------------------------------------

    def get(
        self, kind: str, params: dict[str, Any], *, report_code: str | None = None
    ) -> CacheEntry | None:
        path = self.path_for(kind, params, report_code=report_code)
        if not path.is_file():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                envelope = json.load(handle)
        except (OSError, ValueError, EOFError) as exc:
            # A truncated file (e.g. killed mid-write) must not poison a rerun.
            logger.warning("Discarding unreadable cache entry %s: %s", path.name, exc)
            return None
        if not isinstance(envelope, dict):
            logger.warning("Discarding malformed cache entry %s", path.name)
            return None
        return CacheEntry(path=path, envelope=envelope)

    # -- write ------------------------------------------------------------

    def put(
        self,
        kind: str,
        params: dict[str, Any],
        response: Any,
        *,
        report_code: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> Path:
        """Write a response to the cache and return its path.

        The write is atomic: a temporary file is fsync-free but renamed into
        place, so a concurrent reader never observes a half-written entry.
        """
        sanitized_params = redact_mapping(
            {k: v for k, v in params.items() if k.lower() not in _FORBIDDEN_PARAM_KEYS}
        )
        envelope: dict[str, Any] = {
            "cache_format_version": CACHE_FORMAT_VERSION,
            "kind": kind,
            "report_code": report_code,
            "params": sanitized_params,
            "query_version": self.query_version,
            "fetched_at": time.time(),
            "provenance": provenance(),
            "response": response,
        }
        if extra:
            envelope["extra"] = redact_mapping(extra)

        text = json.dumps(envelope, ensure_ascii=False, default=str)
        _assert_no_secrets(text, kind)

        path = self.path_for(kind, params, report_code=report_code)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                handle.write(text)
            tmp.replace(path)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return path

    # -- introspection ----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """File counts and byte totals per kind, for validation reports."""
        if not self.root.exists():
            return {"root": str(self.root), "exists": False, "files": 0, "bytes": 0, "kinds": {}}
        kinds: dict[str, dict[str, int]] = {}
        total_files = 0
        total_bytes = 0
        for path in self.root.rglob("*.json.gz"):
            try:
                size = path.stat().st_size
            except OSError:  # pragma: no cover - race with cleanup
                continue
            kind = path.relative_to(self.root).parts[0]
            bucket = kinds.setdefault(kind, {"files": 0, "bytes": 0})
            bucket["files"] += 1
            bucket["bytes"] += size
            total_files += 1
            total_bytes += size
        return {
            "root": str(self.root),
            "exists": True,
            "files": total_files,
            "bytes": total_bytes,
            "kinds": dict(sorted(kinds.items())),
        }

    def audit_for_secrets(self) -> list[str]:
        """Scan every cache file for registered secrets.

        Returns the offending paths. Used by `wclmplus cache-audit` so the user
        can verify before sharing a dataset that it contains no credentials.
        """
        from .redaction import _secrets  # local import: internal by design

        if not _secrets or not self.root.exists():
            return []
        offenders: list[str] = []
        for path in self.root.rglob("*.json.gz"):
            try:
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except (OSError, EOFError):  # pragma: no cover - unreadable file
                continue
            if any(secret in content for secret in _secrets):
                offenders.append(str(path))
        return offenders


def _assert_no_secrets(text: str, kind: str) -> None:
    """Last line of defence before any bytes reach disk."""
    from .redaction import _secrets

    for secret in _secrets:
        if secret in text:
            raise CacheSecurityError(
                f"Refusing to write raw-cache entry for {kind!r}: the payload contains "
                "a registered credential. This is a bug -- report it rather than "
                "disabling the check."
            )

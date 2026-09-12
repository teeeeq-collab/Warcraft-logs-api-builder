"""Version and provenance stamps.

Every collection job records these so a stored row can always be traced back
to the code that produced it (brief section 46).
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

#: Software version. Bump on any behavioural change to collection.
SOFTWARE_VERSION = "0.1.0"

#: Normalizer version. Bump whenever raw -> normalized mapping changes, so a
#: rebuild from raw cache is distinguishable from the original ingest.
NORMALIZER_VERSION = 1

#: GraphQL query-set version. Bump when files in queries/ change meaningfully.
QUERY_VERSION = 1

#: Database schema version. 0 = no schema implemented yet (Phase 0).
SCHEMA_VERSION = 0


@lru_cache(maxsize=1)
def git_commit() -> str | None:
    """Short git commit of the working tree, or None outside a repo.

    Returns None rather than raising: provenance is best-effort and must never
    break a collection run.
    """
    repo_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


@lru_cache(maxsize=1)
def git_dirty() -> bool | None:
    """True if the working tree has uncommitted changes; None if unknown.

    A dirty tree means the recorded commit does not fully describe the code,
    which the validation report should surface.
    """
    repo_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def provenance() -> dict[str, object]:
    """Provenance block embedded in jobs, fixtures and validation reports."""
    return {
        "software_version": SOFTWARE_VERSION,
        "normalizer_version": NORMALIZER_VERSION,
        "query_version": QUERY_VERSION,
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit(),
        "git_dirty": git_dirty(),
    }

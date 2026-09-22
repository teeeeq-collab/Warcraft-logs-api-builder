"""Corpus fingerprints: proof that a derived number still describes the evidence.

A derived analysis is only reproducible if the evidence under it has not moved.
The obvious fingerprint -- run ID, normalizer version, coverage status -- proves
the *labels* are unchanged while the evidence beneath them could differ: a
re-collection at the same normalizer version, a page repaired after a failure, a
`partial` stream completed later. All three leave those values identical and the
events different.

So the fingerprint digests the evidence, at one of two grades:

* **page** -- cache location, counts, statuses and cursors of every page, plus
  the coverage manifest. One indexed scan. Detects every change that arrives
  through the API: a re-fetch, a repair, a completion, a stream newly requested.
  It does **not** detect a payload whose content changed while its event count
  stayed the same, because it never reads the events.

* **deep** -- everything in `page`, plus a digest of every normalized event row
  in page order. A full scan of `events`. Detects the one thing `page` cannot:
  normalization producing different rows from identical payloads without a
  version bump. That should never happen, which is exactly why a published
  result is pinned with it.

Raw payload bytes are deliberately not hashed. The cache is gzipped and may be
recompressed or re-fetched without its content changing; hashing the bytes would
make a compression detail look like an evidence change. The cache location plus
the normalized-row digest covers the same ground without the false positives.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .sanitize import IDENTITY_SCHEME_VERSION, salt_fingerprint
from .version import NORMALIZER_VERSION, QUERY_VERSION, SCHEMA_VERSION

#: Fingerprint construction version. Bump when the digest changes shape, so two
#: fingerprints computed by different code are never compared as equal.
FINGERPRINT_VERSION = 1

GRADES = ("page", "deep")

#: Rows pulled per batch when digesting events. The digest is order-dependent,
#: so batching changes nothing about the result -- only peak memory.
_EVENT_BATCH = 20_000


def _feed(digest: hashlib._Hash, *parts: Any) -> None:
    """Append fields to a digest unambiguously.

    Every field is length-prefixed. Without that, ("ab", "c") and ("a", "bc")
    would hash identically and two different corpora could share a fingerprint.
    """
    for part in parts:
        text = "\x00" if part is None else str(part)
        digest.update(f"{len(text)}:{text}\x1f".encode())


@dataclass
class RunDigest:
    run_id: str
    digest: str
    pages: int
    events: int
    streams: int


@dataclass
class CorpusFingerprint:
    """A fingerprint plus everything needed to interpret it."""

    value: str
    grade: str
    run_count: int
    fingerprint_version: int = FINGERPRINT_VERSION
    normalizer_version: int = NORMALIZER_VERSION
    query_version: int = QUERY_VERSION
    schema_version: int = SCHEMA_VERSION
    runs: dict[str, str] = field(default_factory=dict)

    def as_dict(self, *, include_runs: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "value": self.value,
            "grade": self.grade,
            "run_count": self.run_count,
            "fingerprint_version": self.fingerprint_version,
            "normalizer_version": self.normalizer_version,
            "query_version": self.query_version,
            "schema_version": self.schema_version,
        }
        if include_runs:
            out["runs"] = dict(self.runs)
        return out

    def matches(self, other: CorpusFingerprint) -> bool:
        """Equality that refuses to compare fingerprints of different grades.

        A `page` fingerprint and a `deep` one over the same corpus are different
        strings by construction. Treating a grade mismatch as a difference would
        report false drift; treating it as equal would be worse.
        """
        if self.grade != other.grade:
            raise ValueError(
                f"Cannot compare a '{self.grade}' fingerprint with a '{other.grade}' one. "
                "Recompute at the same grade."
            )
        return self.value == other.value

    def drifted(self, other: CorpusFingerprint) -> list[str]:
        """Run IDs whose evidence differs, when both carry per-run digests."""
        if self.grade != other.grade:
            raise ValueError("Cannot compare fingerprints of different grades.")
        if not self.runs or not other.runs:
            return []
        changed = [r for r, d in self.runs.items() if other.runs.get(r) not in (None, d)]
        gone = [r for r in self.runs if r not in other.runs]
        added = [r for r in other.runs if r not in self.runs]
        return sorted({*changed, *gone, *added})


def _page_component(db: Database, run_id: str) -> tuple[str, int, int]:
    """Digest of what was fetched for one run, in a declared order."""
    digest = hashlib.sha256()
    pages = 0
    events = 0
    for row in db.query(
        "SELECT data_type, hostility, source_id, target_id, page_index, "
        "       cursor_ms, next_cursor_ms, event_count, status, raw_cache_path "
        "  FROM event_pages WHERE run_id = ? "
        " ORDER BY data_type, IFNULL(hostility,''), IFNULL(source_id,-1), "
        "          IFNULL(target_id,-1), page_index",
        (run_id,),
    ):
        _feed(
            digest,
            row["data_type"],
            row["hostility"],
            row["source_id"],
            row["target_id"],
            row["page_index"],
            row["cursor_ms"],
            row["next_cursor_ms"],
            row["event_count"],
            row["status"],
            row["raw_cache_path"],
        )
        pages += 1
        events += int(row["event_count"] or 0)
    return digest.hexdigest(), pages, events


def _coverage_component(db: Database, run_id: str) -> tuple[str, int]:
    """Digest of the coverage manifest for one run.

    Included because "asked, none" and "never asked" are different evidence for
    every statistic downstream, and nothing in the page component distinguishes
    them: neither writes a page.
    """
    digest = hashlib.sha256()
    streams = 0
    for row in db.query(
        "SELECT data_type, hostility, source_id, target_id, scope, pages, events, status "
        "  FROM run_stream_coverage WHERE run_id = ? "
        " ORDER BY data_type, IFNULL(hostility,''), IFNULL(source_id,-1), IFNULL(target_id,-1)",
        (run_id,),
    ):
        _feed(
            digest,
            row["data_type"],
            row["hostility"],
            row["source_id"],
            row["target_id"],
            row["scope"],
            row["pages"],
            row["events"],
            row["status"],
        )
        streams += 1
    return digest.hexdigest(), streams


#: Event columns entering a deep digest, in a fixed order. `event_id` is excluded
#: because it is an autoincrement surrogate: re-collecting identical events
#: renumbers it, which is a storage detail and not an evidence change.
_EVENT_COLUMNS = (
    "page_id",
    "seq_in_page",
    "pull_id",
    "data_type",
    "hostility",
    "rel_ms",
    "abs_ms",
    "run_rel_ms",
    "pull_rel_ms",
    "type",
    "source_id",
    "source_instance",
    "target_id",
    "target_instance",
    "ability_game_id",
    "extra_ability_game_id",
    "amount",
    "absorbed",
    "blocked",
    "mitigated",
    "unmitigated_amount",
    "overkill",
    "hit_type",
    "is_tick",
    "is_aoe",
    "is_buff",
    "stack",
    "hit_points",
    "max_hit_points",
    "buffs",
    "killer_id",
    "killer_instance",
    "killing_ability_game_id",
    "x",
    "y",
    "map_id",
    "extra",
    "normalizer_version",
)


def _event_component(db: Database, run_id: str) -> str:
    """Digest of every normalized event row for one run, in page order.

    Ordered by (page_id, seq_in_page) because that pair is the event's identity
    in this schema -- the same pair `events` is unique on.
    """
    digest = hashlib.sha256()
    columns = ", ".join(_EVENT_COLUMNS)
    cursor = db.execute(
        f"SELECT {columns} FROM events WHERE run_id = ? ORDER BY page_id, seq_in_page",
        (run_id,),
    )
    while True:
        batch = cursor.fetchmany(_EVENT_BATCH)
        if not batch:
            break
        for row in batch:
            _feed(digest, *(row[col] for col in _EVENT_COLUMNS))
    return digest.hexdigest()


def run_digest(db: Database, run_id: str, *, grade: str = "page") -> RunDigest:
    """Evidence digest for one run."""
    if grade not in GRADES:
        raise ValueError(f"Unknown fingerprint grade {grade!r}; expected one of {GRADES}.")

    page_hash, pages, events = _page_component(db, run_id)
    coverage_hash, streams = _coverage_component(db, run_id)

    digest = hashlib.sha256()
    _feed(
        digest,
        FINGERPRINT_VERSION,
        grade,
        run_id,
        NORMALIZER_VERSION,
        QUERY_VERSION,
        SCHEMA_VERSION,
        IDENTITY_SCHEME_VERSION,
        salt_fingerprint(),
        page_hash,
        coverage_hash,
    )
    if grade == "deep":
        _feed(digest, _event_component(db, run_id))

    return RunDigest(
        run_id=run_id, digest=digest.hexdigest(), pages=pages, events=events, streams=streams
    )


def corpus_fingerprint(
    db: Database,
    run_ids: list[str] | None = None,
    *,
    grade: str = "page",
    include_runs: bool = True,
) -> CorpusFingerprint:
    """Fingerprint of a run set.

    Run digests are sorted before combining, so the order runs were named in
    cannot affect the result.
    """
    if grade not in GRADES:
        raise ValueError(f"Unknown fingerprint grade {grade!r}; expected one of {GRADES}.")
    if run_ids is None:
        run_ids = [r["run_id"] for r in db.query("SELECT run_id FROM dungeon_runs ORDER BY run_id")]

    per_run = {rid: run_digest(db, rid, grade=grade).digest for rid in sorted(set(run_ids))}

    digest = hashlib.sha256()
    _feed(digest, FINGERPRINT_VERSION, grade, len(per_run))
    for value in sorted(per_run.values()):
        _feed(digest, value)

    return CorpusFingerprint(
        value=digest.hexdigest(),
        grade=grade,
        run_count=len(per_run),
        runs=per_run if include_runs else {},
    )

"""Report discovery, abstracted from report ingestion.

Discovery is a research problem in its own right (brief section 7). There is
no documented "give me 500 random public +10 logs" call, so the collector must
not be built as if there were. Ingestion consumes `ReportCandidate` objects;
where those come from is pluggable.

`ManualReportSource` is the guaranteed-available implementation: a list of
report codes or URLs. It keeps the whole collector usable even if automatic
global discovery turns out to be impossible.

The API-backed sources are written against **unverified** schema shapes and
refuse to run until `wclmplus recon --discovery` has confirmed the fields
exist. They fail with an explanation rather than a GraphQL error.

Every candidate carries provenance -- source type, seed, discovery timestamp,
rank/page -- because sampling bias cannot be assessed later without knowing
how a run entered the corpus (brief section 8).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .querybuild import load_query
from .redaction import RedactedError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import GraphQLClient
    from .schema import SchemaIntrospector

logger = logging.getLogger(__name__)

#: Report codes are alphanumeric. Length is not pinned: it has changed before,
#: and a length assumption would reject valid codes.
_CODE_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")

#: Pulls the code out of any warcraftlogs.com report URL shape.
_URL_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*warcraftlogs\.com/reports/(?:compare/)?([A-Za-z0-9]{8,32})",
    re.IGNORECASE,
)


class DiscoveryError(RedactedError):
    """Report discovery could not be performed."""


class DiscoveryUnverified(DiscoveryError):
    """A discovery mechanism was used before its schema shape was confirmed."""


@dataclass(frozen=True)
class ReportProvenance:
    """How a report entered the corpus.

    Recorded per candidate and persisted, so a sample can be reweighted or a
    bias source excluded after the fact.
    """

    source_type: str
    seed: str | None = None
    discovered_at: float = field(default_factory=time.time)
    rank: int | None = None
    page: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "seed": self.seed,
            "discovered_at": self.discovered_at,
            "rank": self.rank,
            "page": self.page,
            "extra": self.extra,
        }


@dataclass(frozen=True)
class ReportCandidate:
    """A report code plus the provenance of its discovery."""

    code: str
    provenance: ReportProvenance

    def summary(self) -> dict[str, Any]:
        return {"code": self.code, "provenance": self.provenance.summary()}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def extract_report_code(text: str) -> str | None:
    """Extract a report code from a URL or a bare code.

    Returns None for anything unrecognised, so a malformed line in a
    user-supplied list is reported rather than silently becoming a bad request.
    """
    candidate = (text or "").strip()
    if not candidate:
        return None
    match = _URL_RE.search(candidate)
    if match:
        return match.group(1)
    # A bare code: strip a trailing fragment or query if someone pasted one.
    candidate = candidate.split("#", 1)[0].split("?", 1)[0].strip().strip("/")
    if _CODE_RE.match(candidate):
        return candidate
    return None


def parse_report_list(text: str) -> tuple[list[str], list[str]]:
    """Parse a report list document.

    Accepts one entry per line, `#` comments, blank lines, and a first CSV
    field per line. Returns (codes in order, rejected lines) -- rejects are
    surfaced, never dropped quietly.
    """
    codes: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        first_field = line.split(",", 1)[0].strip().strip('"').strip("'")
        code = extract_report_code(first_field) or extract_report_code(line)
        if code is None:
            rejected.append(line[:120])
            continue
        if code in seen:
            continue  # same report listed twice is not two observations
        seen.add(code)
        codes.append(code)
    return codes, rejected


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class ReportSource(Protocol):
    """Anything that can yield report candidates."""

    source_type: str

    def discover(self, limit: int | None = None) -> Iterator[ReportCandidate]:
        """Yield candidates, at most `limit` of them."""
        ...


@dataclass
class ManualReportSource:
    """Report codes supplied by the user.

    Always available and never rate-limited. This is the fallback that makes
    the collector independent of automatic discovery.
    """

    codes: list[str]
    seed: str | None = None
    rejected: list[str] = field(default_factory=list)
    source_type: str = "manual"

    @classmethod
    def from_iterable(cls, values: Iterable[str], *, seed: str | None = None) -> ManualReportSource:
        codes: list[str] = []
        rejected: list[str] = []
        seen: set[str] = set()
        for value in values:
            code = extract_report_code(value)
            if code is None:
                rejected.append(str(value)[:120])
            elif code not in seen:
                seen.add(code)
                codes.append(code)
        return cls(codes=codes, seed=seed, rejected=rejected)

    @classmethod
    def from_file(cls, path: Path | str) -> ManualReportSource:
        file_path = Path(path)
        if not file_path.is_file():
            raise DiscoveryError(f"Report list not found: {file_path}")
        codes, rejected = parse_report_list(file_path.read_text(encoding="utf-8"))
        if not codes:
            raise DiscoveryError(
                f"No usable report codes in {file_path}. Expected one report code or "
                "warcraftlogs.com report URL per line."
                + (f" Rejected {len(rejected)} line(s), first: {rejected[0]!r}" if rejected else "")
            )
        if rejected:
            logger.warning(
                "%d line(s) in %s were not recognised as report codes: %s",
                len(rejected),
                file_path.name,
                rejected[:5],
            )
        return cls(codes=codes, seed=str(file_path), rejected=rejected, source_type="manual_file")

    def discover(self, limit: int | None = None) -> Iterator[ReportCandidate]:
        for index, code in enumerate(self.codes):
            if limit is not None and index >= limit:
                return
            yield ReportCandidate(
                code=code,
                provenance=ReportProvenance(
                    source_type=self.source_type,
                    seed=self.seed,
                    rank=index,
                    extra={"list_position": index, "list_size": len(self.codes)},
                ),
            )


@dataclass
class _ApiSource:
    """Shared behaviour for API-backed discovery.

    Each subclass names the schema path it depends on. `verify` must pass
    before `discover` will issue a request, so an unverified assumption
    surfaces as an explanation instead of a GraphQL error mid-collection.
    """

    client: GraphQLClient
    introspector: SchemaIntrospector
    source_type: str = "api"

    def _require(self, type_name: str, field_name: str) -> None:
        info = self.introspector.type_info(type_name)
        if info is None or not info.has(field_name):
            raise DiscoveryUnverified(
                f"This discovery method needs {type_name}.{field_name}, which the live "
                "schema does not expose (or which has been renamed). Run "
                "`wclmplus recon --discovery` to record what discovery paths actually "
                "exist, and fall back to `--report-list` in the meantime."
            )


@dataclass
class CharacterReportSource(_ApiSource):
    """Recent reports for a named character.

    Legitimate, supported, and inherently biased toward that character's
    groups -- which is why provenance records the seed.
    """

    name: str = ""
    server_slug: str = ""
    region: str = ""
    source_type: str = "character_recent_reports"

    def discover(self, limit: int | None = None) -> Iterator[ReportCandidate]:
        self._require("Character", "recentReports")
        page = 1
        emitted = 0
        seen: set[str] = set()
        while True:
            data = self.client.execute(
                load_query("character_reports"),
                {
                    "name": self.name,
                    "server": self.server_slug,
                    "region": self.region,
                    "limit": 100,
                    "page": page,
                },
                kind="discover_character_reports",
            )
            character = ((data.get("characterData") or {}).get("character")) or {}
            block = character.get("recentReports") or {}
            rows = block.get("data") or []
            if not rows:
                return
            for row in rows:
                code = row.get("code") if isinstance(row, dict) else None
                if not code or code in seen:
                    continue
                seen.add(code)
                yield ReportCandidate(
                    code=str(code),
                    provenance=ReportProvenance(
                        source_type=self.source_type,
                        seed=f"{self.name}-{self.server_slug}-{self.region}",
                        rank=emitted,
                        page=page,
                        extra={"zone": row.get("zone"), "startTime": row.get("startTime")},
                    ),
                )
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
            if not block.get("has_more_pages"):
                return
            page += 1


@dataclass
class GuildReportSource(_ApiSource):
    """Reports belonging to a guild or team."""

    guild_id: int | None = None
    source_type: str = "guild_reports"

    def discover(self, limit: int | None = None) -> Iterator[ReportCandidate]:
        self._require("ReportData", "reports")
        page = 1
        emitted = 0
        seen: set[str] = set()
        while True:
            data = self.client.execute(
                load_query("guild_reports"),
                {"guildID": self.guild_id, "limit": 100, "page": page},
                kind="discover_guild_reports",
            )
            block = ((data.get("reportData") or {}).get("reports")) or {}
            rows = block.get("data") or []
            if not rows:
                return
            for row in rows:
                code = row.get("code") if isinstance(row, dict) else None
                if not code or code in seen:
                    continue
                seen.add(code)
                yield ReportCandidate(
                    code=str(code),
                    provenance=ReportProvenance(
                        source_type=self.source_type,
                        seed=str(self.guild_id),
                        rank=emitted,
                        page=page,
                        extra={"zone": row.get("zone"), "startTime": row.get("startTime")},
                    ),
                )
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
            if not block.get("has_more_pages"):
                return
            page += 1

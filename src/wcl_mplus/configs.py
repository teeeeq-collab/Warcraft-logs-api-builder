"""Typed access to the YAML configuration files.

Config drives dungeons, sampling strata and hotfix epochs so none of it lives
in source (brief sections 33, 44, 10). Two behaviours matter here:

* An unverified dungeon ID is an error at the point of use, not a silent
  `None` that becomes a bad query. `resolve()` returns the entry; asking for
  its zone ID when it was never discovered raises.
* `epoch_for()` never guesses. A run outside every declared epoch is labelled
  `unclassified` and counted, so validation can report how much of a sample
  has unknown mechanic provenance.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .redaction import RedactedError
from .settings import project_root


class ConfigFileError(RedactedError):
    """A configuration file is missing, malformed, or internally inconsistent."""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigFileError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"{path.name} is not valid YAML: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigFileError(f"{path.name} must contain a mapping at the top level.")
    return data


def config_hash(*paths: Path) -> str:
    """Stable hash of the config files, recorded with every collection job.

    Lets a stored run be tied to the exact sampling rules that produced it.
    """
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dungeons
# ---------------------------------------------------------------------------


def _merge_dungeon_overlay(
    base: dict[str, Any], overlay: dict[str, Any], overlay_path: Path
) -> dict[str, Any]:
    """Merge discovered IDs over the authored dungeon config.

    Only identity fields are merged: a discovery run fills in IDs and flips
    `verified`, but never rewrites display names, aliases, roles or the
    hand-written validation targets.
    """
    merged = dict(base)
    merged["season"] = {**(base.get("season") or {}), **(overlay.get("season") or {})}

    overlay_by_key = {
        str(entry["key"]): entry
        for entry in overlay.get("dungeons") or []
        if isinstance(entry, dict) and entry.get("key")
    }
    identity_fields = ("wcl_zone_id", "encounter_ids", "verified")

    out_dungeons: list[dict[str, Any]] = []
    for entry in base.get("dungeons") or []:
        if not isinstance(entry, dict):
            continue
        merged_entry = dict(entry)
        discovered = overlay_by_key.pop(str(entry.get("key")), None)
        if discovered:
            for name in identity_fields:
                if name in discovered:
                    merged_entry[name] = discovered[name]
        out_dungeons.append(merged_entry)

    # Dungeons found only by discovery (the unknown season slots) are appended.
    for key, entry in overlay_by_key.items():
        appended = dict(entry)
        appended.setdefault("display_name", key)
        appended.setdefault("role", "discovered")
        appended.setdefault(
            "notes", f"Added by discovery overlay {overlay_path.name}; review before use."
        )
        out_dungeons.append(appended)

    merged["dungeons"] = out_dungeons
    return merged


@dataclass
class DungeonEntry:
    key: str
    display_name: str
    aliases: list[str] = field(default_factory=list)
    wcl_zone_id: int | None = None
    encounter_ids: list[int] = field(default_factory=list)
    verified: bool = False
    role: str | None = None
    validation_targets: list[dict[str, Any]] = field(default_factory=list)

    def require_zone_id(self) -> int:
        if self.wcl_zone_id is None or not self.verified:
            raise ConfigFileError(
                f"Dungeon {self.display_name!r} has no verified WCL zone ID. "
                "IDs are never hardcoded in this project -- run "
                "`wclmplus discover-dungeons --write` to fill them in from the live "
                "API, then re-run this command."
            )
        return self.wcl_zone_id

    def matches(self, needle: str) -> bool:
        probe = needle.strip().lower()
        return probe in {
            self.key.lower(),
            self.display_name.lower(),
            *(a.lower() for a in self.aliases),
        }


@dataclass
class DungeonRegistry:
    season_id: str
    season_display_name: str
    wcl_mplus_zone_id: int | None
    expected_dungeon_count: int
    season_verified: bool
    dungeons: list[DungeonEntry]
    path: Path
    overlay_path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None, *, overlay: Path | None = None) -> DungeonRegistry:
        """Load the dungeon registry, applying the discovery overlay if present.

        `config/dungeons.yml` is hand-written and commented. IDs discovered
        from the live API are written to `config/dungeons.discovered.yml`
        instead and merged over it here, so automatic discovery never has to
        rewrite (and strip the comments from) the authored file.
        """
        file_path = path or (project_root() / "config" / "dungeons.yml")
        data = _load_yaml(file_path)
        overlay_path = (
            overlay if overlay is not None else file_path.with_name("dungeons.discovered.yml")
        )
        if overlay_path.is_file():
            data = _merge_dungeon_overlay(data, _load_yaml(overlay_path), overlay_path)
        season = data.get("season") or {}
        entries: list[DungeonEntry] = []
        for raw in data.get("dungeons") or []:
            if not isinstance(raw, dict) or not raw.get("key"):
                raise ConfigFileError(f"{file_path.name}: each dungeon needs a 'key'.")
            entries.append(
                DungeonEntry(
                    key=str(raw["key"]),
                    display_name=str(raw.get("display_name") or raw["key"]),
                    aliases=[str(a) for a in (raw.get("aliases") or [])],
                    wcl_zone_id=raw.get("wcl_zone_id"),
                    encounter_ids=[int(e) for e in (raw.get("encounter_ids") or [])],
                    verified=bool(raw.get("verified")),
                    role=raw.get("role"),
                    validation_targets=list(raw.get("validation_targets") or []),
                )
            )
        keys = [e.key for e in entries]
        duplicates = {k for k in keys if keys.count(k) > 1}
        if duplicates:
            raise ConfigFileError(f"{file_path.name}: duplicate dungeon keys {sorted(duplicates)}")
        return cls(
            season_id=str(season.get("id") or "unknown"),
            season_display_name=str(season.get("display_name") or "unknown"),
            wcl_mplus_zone_id=season.get("wcl_mplus_zone_id"),
            expected_dungeon_count=int(season.get("expected_dungeon_count") or 0),
            season_verified=bool(season.get("verified")),
            dungeons=entries,
            path=file_path,
            overlay_path=overlay_path if overlay_path.is_file() else None,
        )

    def resolve(self, needle: str) -> DungeonEntry:
        for entry in self.dungeons:
            if entry.matches(needle):
                return entry
        known = ", ".join(e.display_name for e in self.dungeons) or "(none configured)"
        raise ConfigFileError(f"Unknown dungeon {needle!r}. Configured: {known}")

    @property
    def verified_count(self) -> int:
        return sum(1 for e in self.dungeons if e.verified and e.wcl_zone_id is not None)

    def status(self) -> dict[str, Any]:
        return {
            "season_id": self.season_id,
            "season_verified": self.season_verified,
            "configured_dungeons": len(self.dungeons),
            "expected_dungeon_count": self.expected_dungeon_count,
            "verified_dungeons": self.verified_count,
            "unverified": [e.display_name for e in self.dungeons if not e.verified],
            "discovery_overlay": str(self.overlay_path) if self.overlay_path else None,
        }


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


@dataclass
class KeyBracket:
    key: str
    min: int
    max: int | None

    def contains(self, level: int | None) -> bool:
        if level is None:
            return False
        if level < self.min:
            return False
        return self.max is None or level <= self.max


@dataclass
class EventProfile:
    name: str
    description: str
    event_types: list[str]
    include_combatant_info: bool = False


@dataclass
class SampleProfile:
    name: str
    description: str
    event_profile: str
    runs_per_bracket: int
    max_runs_total: int
    brackets: list[str]
    require_timed: bool
    deduplicate: bool
    restrict_to_current_hotfix_epoch: bool


@dataclass
class SamplingConfig:
    key_brackets: list[KeyBracket]
    event_profiles: dict[str, EventProfile]
    sample_profiles: dict[str, SampleProfile]
    api: dict[str, Any]
    tracked_biases: list[str]
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> SamplingConfig:
        file_path = path or (project_root() / "config" / "sampling.yml")
        data = _load_yaml(file_path)

        brackets = [
            KeyBracket(
                key=str(b["key"]),
                min=int(b["min"]),
                max=(None if b.get("max") is None else int(b["max"])),
            )
            for b in data.get("key_brackets") or []
            if isinstance(b, dict) and b.get("key") is not None and b.get("min") is not None
        ]
        if not brackets:
            raise ConfigFileError(f"{file_path.name}: at least one key bracket is required.")

        event_profiles = {
            str(name): EventProfile(
                name=str(name),
                description=str((raw or {}).get("description") or ""),
                event_types=[str(t) for t in (raw or {}).get("event_types") or []],
                include_combatant_info=bool((raw or {}).get("include_combatant_info")),
            )
            for name, raw in (data.get("event_profiles") or {}).items()
        }

        sample_profiles: dict[str, SampleProfile] = {}
        bracket_keys = {b.key for b in brackets}
        for name, raw in (data.get("sample_profiles") or {}).items():
            raw = raw or {}
            profile = SampleProfile(
                name=str(name),
                description=str(raw.get("description") or ""),
                event_profile=str(raw.get("event_profile") or "metadata"),
                runs_per_bracket=int(raw.get("runs_per_bracket") or 0),
                max_runs_total=int(raw.get("max_runs_total") or 0),
                brackets=[str(b) for b in raw.get("brackets") or []],
                require_timed=bool(raw.get("require_timed")),
                deduplicate=bool(raw.get("deduplicate", True)),
                restrict_to_current_hotfix_epoch=bool(raw.get("restrict_to_current_hotfix_epoch")),
            )
            if profile.event_profile not in event_profiles:
                raise ConfigFileError(
                    f"{file_path.name}: sample profile {name!r} references unknown "
                    f"event profile {profile.event_profile!r}."
                )
            unknown = [b for b in profile.brackets if b not in bracket_keys]
            if unknown:
                raise ConfigFileError(
                    f"{file_path.name}: sample profile {name!r} references unknown "
                    f"key bracket(s) {unknown}."
                )
            sample_profiles[str(name)] = profile

        return cls(
            key_brackets=brackets,
            event_profiles=event_profiles,
            sample_profiles=sample_profiles,
            api=dict(data.get("api") or {}),
            tracked_biases=[str(b) for b in data.get("tracked_biases") or []],
            path=file_path,
        )

    def bracket_for(self, key_level: int | None) -> str | None:
        """Bracket key for a keystone level, or None if it fits none."""
        for bracket in self.key_brackets:
            if bracket.contains(key_level):
                return bracket.key
        return None

    def event_profile(self, name: str) -> EventProfile:
        if name not in self.event_profiles:
            raise ConfigFileError(
                f"Unknown event profile {name!r}. "
                f"Available: {', '.join(sorted(self.event_profiles))}"
            )
        return self.event_profiles[name]

    def sample_profile(self, name: str) -> SampleProfile:
        if name not in self.sample_profiles:
            raise ConfigFileError(
                f"Unknown sample profile {name!r}. "
                f"Available: {', '.join(sorted(self.sample_profiles))}"
            )
        return self.sample_profiles[name]

    @staticmethod
    def parse_event_type(spec: str) -> tuple[str, str | None]:
        """Split an event-profile entry into (data type, hostility).

        `"Casts@Enemies"` -> `("Casts", "Enemies")`; `"Debuffs"` ->
        `("Debuffs", None)`. Hostility exists because an unfiltered event
        query is dominated by players -- see the note in sampling.yml.
        """
        data_type, _, hostility = str(spec).partition("@")
        return data_type.strip(), (hostility.strip() or None)

    def validate_event_types(self, live_enum: list[str]) -> dict[str, list[str]]:
        """Compare configured event types against the live enum.

        Returns per-profile lists of unsupported values, so `config-check` can
        report them before a run rather than failing mid-collection.
        """
        if not live_enum:
            return {}
        allowed = set(live_enum)
        problems: dict[str, list[str]] = {}
        for name, profile in self.event_profiles.items():
            bad = [
                spec
                for spec in profile.event_types
                if self.parse_event_type(spec)[0] not in allowed
            ]
            if bad:
                problems[name] = bad
        return problems


# ---------------------------------------------------------------------------
# Hotfix epochs
# ---------------------------------------------------------------------------

UNCLASSIFIED_EPOCH = "unclassified"


def _parse_iso(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ConfigFileError(f"Not an ISO-8601 timestamp: {value!r}") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


@dataclass
class HotfixEpoch:
    name: str
    start: dt.datetime | None
    end: dt.datetime | None
    affected_dungeons: list[str] = field(default_factory=list)
    affected_abilities: list[str] = field(default_factory=list)
    note: str = ""

    def contains(self, moment: dt.datetime) -> bool:
        """Half-open window [start, end): an epoch boundary belongs to the new epoch."""
        if self.start is None:
            return False
        if moment < self.start:
            return False
        return self.end is None or moment < self.end


@dataclass
class HotfixEpochs:
    epochs: list[HotfixEpoch]
    path: Path

    @classmethod
    def load(cls, path: Path | None = None) -> HotfixEpochs:
        file_path = path or (project_root() / "config" / "hotfix_epochs.yml")
        data = _load_yaml(file_path)
        epochs = [
            HotfixEpoch(
                name=str(raw.get("name") or "unnamed"),
                start=_parse_iso(raw.get("start")),
                end=_parse_iso(raw.get("end")),
                affected_dungeons=[str(d) for d in raw.get("affected_dungeons") or []],
                affected_abilities=[str(a) for a in raw.get("affected_abilities") or []],
                note=str(raw.get("note") or ""),
            )
            for raw in data.get("epochs") or []
            if isinstance(raw, dict)
        ]
        # Overlaps would make epoch assignment ambiguous; refuse rather than
        # pick arbitrarily.
        dated = sorted(
            ((e.start, e) for e in epochs if e.start is not None), key=lambda pair: pair[0]
        )
        for (_, earlier), (later_start, later) in zip(dated, dated[1:], strict=False):
            if earlier.end is None or earlier.end > later_start:
                raise ConfigFileError(
                    f"{file_path.name}: epochs {earlier.name!r} and {later.name!r} overlap. "
                    "Give the earlier one an 'end' so every run maps to exactly one epoch."
                )
        return cls(epochs=epochs, path=file_path)

    def epoch_for(self, moment: dt.datetime | float | int | None) -> str:
        """Epoch name for an absolute time. Never guesses.

        Accepts a datetime or a Unix timestamp in seconds or milliseconds
        (WCL report times are milliseconds).
        """
        if moment is None:
            return UNCLASSIFIED_EPOCH
        if isinstance(moment, (int, float)):
            seconds = moment / 1000.0 if moment > 1e11 else float(moment)
            moment = dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
        for epoch in self.epochs:
            if epoch.contains(moment):
                return epoch.name
        return UNCLASSIFIED_EPOCH

    @property
    def current(self) -> HotfixEpoch | None:
        """The epoch with no end date, if exactly one is declared."""
        open_ended = [e for e in self.epochs if e.start is not None and e.end is None]
        return open_ended[-1] if open_ended else None


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@dataclass
class RoleMap:
    """Spec -> role, loaded from config rather than hardcoded.

    Warcraft Logs reports a spec but no role, and role is game knowledge that
    changes between expansions. Anything not listed as tank or healer is dps:
    the brief's questions turn on tank and healer experience and on group
    damage in aggregate, so enumerating every dps spec would be maintenance
    with no research value.
    """

    tank: set[str] = field(default_factory=set)
    healer: set[str] = field(default_factory=set)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> RoleMap:
        file_path = path or (project_root() / "config" / "roles.yml")
        if not file_path.is_file():
            return cls()
        data = _load_yaml(file_path)
        return cls(
            tank={str(s) for s in data.get("tank") or []},
            healer={str(s) for s in data.get("healer") or []},
            path=file_path,
        )

    def role_for(self, class_name: str | None, spec: str | None) -> str | None:
        """Role for a class/spec pair, or None when the spec is unknown.

        A "Class-Spec" key wins over the bare spec name, so a future collision
        between two same-named specs of different roles can be resolved without
        changing this code.
        """
        if not spec:
            return None
        qualified = f"{class_name}-{spec}" if class_name else None
        for candidate in (qualified, spec):
            if candidate is None:
                continue
            if candidate in self.tank:
                return "tank"
            if candidate in self.healer:
                return "healer"
        return "dps"


@dataclass
class ProjectConfig:
    """All configuration, loaded together."""

    dungeons: DungeonRegistry
    sampling: SamplingConfig
    hotfixes: HotfixEpochs
    roles: RoleMap = field(default_factory=RoleMap)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> ProjectConfig:
        base = config_dir or (project_root() / "config")
        return cls(
            dungeons=DungeonRegistry.load(base / "dungeons.yml"),
            sampling=SamplingConfig.load(base / "sampling.yml"),
            hotfixes=HotfixEpochs.load(base / "hotfix_epochs.yml"),
            roles=RoleMap.load(base / "roles.yml"),
        )

    def hash(self) -> str:
        paths = [self.dungeons.path, self.sampling.path, self.hotfixes.path]
        if self.dungeons.overlay_path is not None:
            paths.append(self.dungeons.overlay_path)
        return config_hash(*paths)

    def status(self) -> dict[str, Any]:
        return {
            "config_hash": self.hash(),
            "dungeons": self.dungeons.status(),
            "key_brackets": [b.key for b in self.sampling.key_brackets],
            "event_profiles": sorted(self.sampling.event_profiles),
            "sample_profiles": sorted(self.sampling.sample_profiles),
            "hotfix_epochs": [e.name for e in self.hotfixes.epochs],
            "current_hotfix_epoch": (self.hotfixes.current.name if self.hotfixes.current else None),
        }


def as_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)

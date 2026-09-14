"""Command-line interface.

The user should never need to write GraphQL or edit Python (brief sections 43,
53). Commands are grouped by what they need:

* offline (no credentials, no network): `config-check`, `report-list-check`,
  `cache-stats`, `cache-audit`, `version`
* live: `auth-check`, `schema-check`, `inspect-report`, `discover-dungeons`,
  `recon`

Every command installs the logging redaction filter before doing anything, so
no output path can print a credential.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import typer

from .benchmark import (
    HostilityComparison,
    StreamBenchmark,
    StreamMeasurement,
    render_json,
    render_report,
)
from .client import ApiError, GraphQLClient
from .collect import Collector
from .configs import ConfigFileError, ProjectConfig
from .db import Database, DatabaseError
from .dedupe import group_duplicates
from .discover import discover_dungeons
from .normalize import is_mythic_plus
from .querybuild import WANTED_FIGHT_FIELDS, WANTED_REPORT_FIELDS, QueryError, render
from .rawcache import RawCache
from .recon import Recon
from .redaction import RedactedError, install_logging_redaction
from .reportsource import DiscoveryError, ManualReportSource
from .schema import SchemaIntrospector
from .settings import ConfigError, Settings
from .validate import collect_validation, write_reports
from .version import provenance

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Warcraft Logs Mythic+ research dataset collector.\n\n"
        "Start here:\n"
        "  1. cp .env.example .env   and paste your Client ID / Secret into it\n"
        "  2. wclmplus auth-check\n"
        "  3. wclmplus recon --report <PUBLIC_MYTHIC_PLUS_REPORT_CODE>\n"
        "  4. read data/exports/recon/RECON_REPORT.md\n\n"
        "Phase 0 (reconnaissance) is all that is implemented. Collection into a "
        "database comes after the recon findings are reviewed."
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    install_logging_redaction()


def _echo_json(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def _load_settings() -> Settings:
    try:
        return Settings.load()
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _load_config() -> ProjectConfig:
    try:
        return ProjectConfig.load()
    except ConfigFileError as exc:
        typer.secho(f"Configuration problem:\n{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _client(settings: Settings) -> GraphQLClient:
    settings.ensure_dirs()
    return GraphQLClient(settings)


def _database(settings: Settings, *, path: Path | None = None) -> Database:
    """Open a collection database, optionally a named partition.

    Partitioning exists for retrieval, not for space. A corpus of thousands of
    runs is hundreds of millions of events, and while an indexed lookup stays
    fast at any size, an aggregate over the whole table does not. One file per
    season or dungeon keeps each aggregate tractable -- and, unlike thinning a
    stream, costs nothing: every partition is complete in itself.

    A bare name is resolved inside the configured database directory, so
    `--database murder-row` and `--database murder-row.sqlite` mean the same
    file and neither can accidentally write to the working directory.
    """
    settings.ensure_dirs()
    if path is None:
        target = settings.db_dir / "wclmplus.sqlite"
    elif path.parent == Path("."):
        target = settings.db_dir / (path.name if path.suffix else f"{path.name}.sqlite")
    else:
        target = path
    try:
        return Database(target)
    except DatabaseError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _fail(exc: Exception, hint: str = "") -> None:
    """Report a failure without ever printing a secret."""
    typer.secho(f"\n{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
    if hint:
        typer.secho(hint, fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# offline commands
# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print version and provenance stamps."""
    _echo_json(provenance())


@app.command("config-check")
def config_check(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Validate the YAML configuration. Needs no credentials or network.

    Reports which dungeon IDs are still unverified: Phase 0 deliberately
    ships with none of them filled in.
    """
    _setup_logging(False)
    config = _load_config()
    status = config.status()
    if json_output:
        _echo_json(status)
        return

    typer.echo(f"Config hash:        {status['config_hash']}")
    typer.echo(
        f"Season:             {config.dungeons.season_display_name} "
        f"({status['dungeons']['season_id']})"
    )
    typer.echo(
        f"Dungeons:           {status['dungeons']['configured_dungeons']} configured, "
        f"{status['dungeons']['verified_dungeons']} with verified IDs, "
        f"{status['dungeons']['expected_dungeon_count']} expected this season"
    )
    typer.echo(f"Key brackets:       {', '.join(status['key_brackets'])}")
    typer.echo(f"Event profiles:     {', '.join(status['event_profiles'])}")
    typer.echo(f"Sample profiles:    {', '.join(status['sample_profiles'])}")
    typer.echo(f"Hotfix epochs:      {', '.join(status['hotfix_epochs'])}")
    typer.echo(f"Current epoch:      {status['current_hotfix_epoch'] or 'none declared'}")

    unverified = status["dungeons"]["unverified"]
    if unverified:
        typer.secho(
            "\nUnverified dungeons (no WCL zone ID yet): " + ", ".join(unverified),
            fg=typer.colors.YELLOW,
        )
        typer.echo(
            "This is expected before reconnaissance. Zone IDs are never hardcoded;\n"
            "run `wclmplus discover-dungeons --write` to fill them in from the live API."
        )
    else:
        typer.secho("\nAll configured dungeons have verified IDs.", fg=typer.colors.GREEN)


@app.command("report-list-check")
def report_list_check(
    path: Path = typer.Argument(..., help="File with one report code or URL per line."),
) -> None:
    """Validate a report list without contacting the API.

    Use this before a collection run to catch typos and rejected lines.
    """
    _setup_logging(False)
    try:
        source = ManualReportSource.from_file(path)
    except DiscoveryError as exc:
        _fail(exc)
        return
    typer.secho(f"{len(source.codes)} usable report code(s).", fg=typer.colors.GREEN)
    for candidate in source.discover(limit=10):
        typer.echo(f"  {candidate.provenance.rank:>3}. {candidate.code}")
    if len(source.codes) > 10:
        typer.echo(f"  ... and {len(source.codes) - 10} more")
    if source.rejected:
        typer.secho(f"\n{len(source.rejected)} unrecognised line(s):", fg=typer.colors.YELLOW)
        for line in source.rejected[:10]:
            typer.echo(f"  {line!r}")


@app.command("cache-stats")
def cache_stats() -> None:
    """Show raw-cache size and composition."""
    _setup_logging(False)
    settings = _load_settings()
    stats = RawCache(settings.raw_cache_dir).stats()
    if not stats["exists"] or stats["files"] == 0:
        typer.echo(f"Raw cache is empty ({stats['root']}).")
        return
    typer.echo(f"Raw cache: {stats['root']}")
    typer.echo(f"Files:     {stats['files']}")
    typer.echo(f"Size:      {stats['bytes'] / 1_048_576:.2f} MiB")
    typer.echo("\nBy request kind:")
    for kind, bucket in stats["kinds"].items():
        typer.echo(f"  {kind:<34} {bucket['files']:>6} files  {bucket['bytes'] / 1024:>10.1f} KiB")


@app.command("cache-audit")
def cache_audit() -> None:
    """Scan the raw cache for credentials before sharing a dataset.

    Loads your credentials only to know what to search for; nothing is sent
    anywhere and no secret is printed.
    """
    _setup_logging(False)
    settings = _load_settings()
    if not settings.has_credentials:
        typer.secho(
            "No credentials configured, so there is nothing to search for. "
            "The audit is only meaningful with .env present.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=0)
    offenders = RawCache(settings.raw_cache_dir).audit_for_secrets()
    if offenders:
        typer.secho(
            f"{len(offenders)} cache file(s) contain a credential. Do NOT share this data:",
            fg=typer.colors.RED,
        )
        for path in offenders[:20]:
            typer.echo(f"  {path}")
        typer.secho(
            "\nThis should be impossible -- the cache writer blocks it. Please report it.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    typer.secho("No credentials found in the raw cache.", fg=typer.colors.GREEN)


# ---------------------------------------------------------------------------
# live commands
# ---------------------------------------------------------------------------


@app.command("auth-check")
def auth_check(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Verify credentials and read the hourly point budget.

    Run this first. It is the cheapest possible live call and confirms the
    whole credential path works before any collection.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    if not settings.has_credentials:
        try:
            settings.require_credentials()
        except ConfigError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from None

    with _client(settings) as client:
        try:
            token = client.tokens.token()
        except ApiError as exc:
            _fail(exc)
            return
        typer.secho("Authentication OK.", fg=typer.colors.GREEN)
        typer.echo(f"  Token type:        {token.token_type}")
        typer.echo(f"  Valid for:         ~{token.seconds_remaining}s")
        typer.echo("  (the token itself is never printed or stored on disk)")

        try:
            state = client.fetch_rate_limit()
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"\nCould not read the rate-limit budget: {exc}", fg=typer.colors.YELLOW)
            raise typer.Exit(code=0) from None

        typer.echo("\nHourly point budget:")
        if state.known:
            typer.echo(f"  Limit per hour:    {state.limit_per_hour:.0f}")
            typer.echo(f"  Spent this hour:   {state.points_spent:.0f}")
            typer.echo(f"  Remaining:         {state.points_remaining:.0f}")
            if state.reset_in_seconds is not None:
                typer.echo(f"  Resets in:         {state.reset_in_seconds:.0f}s")
            typer.echo(f"  Matched fields:    {state.matched_keys}")
        else:
            typer.secho(
                f"  Shape not recognised. Keys returned: {sorted(state.raw)}",
                fg=typer.colors.YELLOW,
            )


@app.command("schema-check")
def schema_check(
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Introspect the live schema and verify every field this project wants.

    Needs credentials but no report code. Answers most of Gate A on its own.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    config = _load_config()
    with _client(settings) as client:
        recon = Recon(client, settings, config=config, write_fixtures=False)
        if not recon.step_auth():
            _fail(RedactedError(recon.findings.steps["auth"].get("error", "authentication failed")))
            return
        recon.step_schema_types()
        recon.step_field_verification()

    if json_output:
        _echo_json(recon.findings.to_json())
        return

    fields = recon.findings.steps.get("field_verification", {})
    for type_name in ("Report", "ReportFight", "ReportDungeonPull", "ReportDungeonPullNPC"):
        block = fields.get(type_name)
        if not isinstance(block, dict):
            continue
        present, absent = block.get("present") or {}, block.get("absent") or []
        colour = typer.colors.GREEN if not absent else typer.colors.YELLOW
        typer.secho(f"\n{type_name}: {len(present)} present, {len(absent)} absent", fg=colour)
        for name in sorted(present):
            typer.echo(f"  + {name}: {present[name]}")
        for name in absent:
            typer.secho(f"  - {name}: ABSENT", fg=typer.colors.YELLOW)

    enum_block = fields.get("EventDataType") or {}
    if enum_block:
        typer.echo(f"\nEventDataType live values: {', '.join(enum_block.get('live_values') or [])}")
        unsupported = enum_block.get("wanted_but_unsupported") or []
        if unsupported:
            typer.secho(f"Unsupported but wanted: {', '.join(unsupported)}", fg=typer.colors.YELLOW)

    if recon.findings.limitations:
        typer.secho("\nLimitations:", fg=typer.colors.YELLOW)
        for index, text in enumerate(recon.findings.limitations, start=1):
            typer.echo(f"  {index}. {text}")


@app.command("inspect-report")
def inspect_report(
    code: str = typer.Argument(..., help="Public report code, or a full report URL."),
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Summarize one report: metadata plus its Mythic+ runs."""
    _setup_logging(verbose)
    from .reportsource import extract_report_code

    report_code = extract_report_code(code)
    if report_code is None:
        typer.secho(
            f"{code!r} is not a report code or warcraftlogs.com report URL.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    settings = _load_settings()
    with _client(settings) as client:
        introspector = SchemaIntrospector(client)
        try:
            report_fields = introspector.present_fields("Report", WANTED_REPORT_FIELDS)
            fight_fields = introspector.present_fields("ReportFight", WANTED_FIGHT_FIELDS)
            meta = client.execute(
                render("report_metadata", {"REPORT_FIELDS": report_fields}),
                {"code": report_code},
                kind="report_metadata",
                report_code=report_code,
            )
            fights_data = client.execute(
                render("report_fights", {"FIGHT_FIELDS": fight_fields}),
                {"code": report_code},
                kind="report_fights",
                report_code=report_code,
            )
        except (ApiError, QueryError) as exc:
            _fail(exc, "Run `wclmplus schema-check` to see what the live schema exposes.")
            return

    report = ((meta.get("reportData") or {}).get("report")) or {}
    if not report:
        typer.secho(
            f"Report {report_code} returned no data. It may be private, deleted, or the "
            "code may be wrong. Private reports are out of scope for v1.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    fights = [
        f
        for f in ((((fights_data.get("reportData") or {}).get("report")) or {}).get("fights") or [])
        if isinstance(f, dict)
    ]
    keystone = [f for f in fights if f.get("keystoneLevel") is not None]

    if json_output:
        _echo_json({"report": report, "mythic_plus_fights": keystone, "total_fights": len(fights)})
        return

    config = _load_config()
    typer.echo(f"Report:      {report_code}")
    typer.echo(f"Zone:        {(report.get('zone') or {}).get('name', 'unknown')}")
    typer.echo(f"Visibility:  {report.get('visibility', 'unknown')}")
    archive = report.get("archiveStatus")
    if isinstance(archive, dict):
        typer.echo(
            f"Archived:    {archive.get('isArchived')} (accessible: {archive.get('isAccessible')})"
        )
        if archive.get("isArchived"):
            typer.secho(
                "  Archived reports may have no event data. They must never enter an "
                "event-frequency denominator.",
                fg=typer.colors.YELLOW,
            )
    typer.echo(f"Fights:      {len(fights)} total, {len(keystone)} with keystone metadata")

    if not keystone:
        typer.secho("\nNo Mythic+ run found in this report.", fg=typer.colors.YELLOW)
        return

    typer.echo("\nMythic+ runs:")
    for fight in keystone:
        level = fight.get("keystoneLevel")
        bracket = config.sampling.bracket_for(level if isinstance(level, int) else None)
        duration = None
        if isinstance(fight.get("startTime"), (int, float)) and isinstance(
            fight.get("endTime"), (int, float)
        ):
            duration = (fight["endTime"] - fight["startTime"]) / 1000.0
        epoch = config.hotfixes.epoch_for(report.get("startTime"))
        typer.echo(
            f"  fight {fight.get('id')}: "
            f"{(fight.get('gameZone') or {}).get('name', fight.get('name'))} "
            f"+{level} (bracket {bracket or 'outside configured brackets'})"
        )
        typer.echo(
            f"      duration {duration:.0f}s, "
            if duration is not None
            else "      duration unknown, "
        )
        typer.echo(
            f"      timed: {fight.get('keystoneBonus')}, count "
            f"{fight.get('countReached')}/{fight.get('countRequired')}, "
            f"avg ilvl {fight.get('averageItemLevel')}, hotfix epoch {epoch}"
        )


@app.command("discover-dungeons")
def discover_dungeons_cmd(
    expansion: int | None = typer.Option(
        None, "--expansion", help="Limit to one WCL expansion ID (see output of a first run)."
    ),
    write: bool = typer.Option(
        False, "--write", help="Persist verified IDs to config/dungeons.discovered.yml."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Resolve configured dungeon names to live WCL zone and encounter IDs.

    Without `--write` this is a dry run that only reports what it found.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    config = _load_config()
    with _client(settings) as client:
        try:
            result = discover_dungeons(client, config.dungeons, expansion_id=expansion, write=write)
        except ApiError as exc:
            _fail(exc)
            return

    typer.echo(f"Zones inspected: {result['zones_seen']}")
    if result.get("expansions"):
        # Sort by ID rather than slicing: the API returns expansions newest
        # first, so an earlier `[-6:]` printed the six OLDEST and hid the
        # current expansion entirely.
        ordered = sorted(
            result["expansions"], key=lambda e: (e.get("id") is None, e.get("id")), reverse=True
        )
        typer.echo(
            "Expansions seen (newest first): "
            + ", ".join(f"{e['id']}={e['name']}" for e in ordered)
        )
    if result["matched"]:
        typer.secho(f"\nMatched {len(result['matched'])} dungeon(s):", fg=typer.colors.GREEN)
        for item in result["matched"].values():
            typer.echo(
                f"  {item['display_name']:<22} zone {item['wcl_zone_id']}, "
                f"{len(item['encounter_ids'])} encounter(s)"
            )
    if result["ambiguous"]:
        typer.secho("\nAmbiguous (multiple zones share the name):", fg=typer.colors.YELLOW)
        for key, ids in result["ambiguous"].items():
            typer.echo(f"  {key}: zone IDs {ids} -- disambiguate by hand in config/dungeons.yml")
    if result["unmatched"]:
        typer.secho("\nNot found in the live zone list:", fg=typer.colors.YELLOW)
        for name in result["unmatched"]:
            typer.echo(f"  {name}")
        typer.echo(
            "\nA name may be wrong, or the dungeon may not be in the current season. "
            "Check the zone list and correct config/dungeons.yml."
        )
    if write:
        written = result.get("overlay_written")
        if written:
            typer.secho(f"\nWrote {written}", fg=typer.colors.GREEN)
        else:
            typer.secho("\nNothing matched, so no overlay was written.", fg=typer.colors.YELLOW)
    else:
        typer.echo("\nDry run. Re-run with --write to persist these IDs.")


@app.command()
def recon(
    report: str | None = typer.Option(
        None,
        "--report",
        help="Public Mythic+ report code or URL. Strongly recommended: without it, "
        "pulls, NPC identity, event shape and pagination stay unverified.",
    ),
    fixtures: bool = typer.Option(
        True, "--fixtures/--no-fixtures", help="Write sanitized fixtures to tests/fixtures/."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the full Phase 0 reconnaissance and write the evidence files.

    Introspects the schema, verifies every field, measures query cost and
    pagination semantics, samples each event category, and records what the API
    does not support. Writes:

      data/exports/recon/recon_findings.json
      data/exports/recon/RECON_REPORT.md
      tests/fixtures/*.json  (player names pseudonymized)

    Costs roughly a few dozen API requests. Safe to re-run: responses are
    served from the raw cache.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    config = _load_config()

    report_code: str | None = None
    if report:
        from .reportsource import extract_report_code

        report_code = extract_report_code(report)
        if report_code is None:
            typer.secho(
                f"{report!r} is not a report code or warcraftlogs.com report URL.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

    with _client(settings) as client:
        engine = Recon(client, settings, config=config, write_fixtures=fixtures)
        findings = engine.run(report_code=report_code)

    ok = sum(1 for s in findings.steps.values() if s.get("status") == "OK")
    failed = [name for name, s in findings.steps.items() if s.get("status") == "FAILED"]
    typer.echo("")
    typer.secho(
        f"Recon complete: {ok} step(s) OK, {len(failed)} failed.",
        fg=typer.colors.GREEN if not failed else typer.colors.YELLOW,
    )
    for name in failed:
        typer.secho(f"  FAILED: {name} -- {findings.steps[name].get('error')}", fg=typer.colors.RED)

    pagination = findings.steps.get("pagination_probe", {})
    if pagination.get("cursor_semantics"):
        typer.echo(f"\nPagination cursor: {pagination['cursor_semantics']}")
        typer.echo(f"Boundary repeats:  {pagination.get('boundary_events_repeated')}")

    if findings.limitations:
        typer.secho(
            f"\n{len(findings.limitations)} limitation(s) recorded:", fg=typer.colors.YELLOW
        )
        for index, text in enumerate(findings.limitations, start=1):
            typer.echo(f"  {index}. {text}")

    typer.echo(f"\nReport:   {engine.output_dir / 'RECON_REPORT.md'}")
    typer.echo(f"Evidence: {engine.output_dir / 'recon_findings.json'}")
    if fixtures and findings.fixtures:
        typer.echo(f"Fixtures: {len(findings.fixtures)} file(s) in tests/fixtures/")
    typer.echo(
        "\nNext: review the report, copy confirmed behaviour into API_NOTES.md, and only "
        "then proceed to Phase 1."
    )


# ---------------------------------------------------------------------------
# collection
# ---------------------------------------------------------------------------


@app.command()
def collect(
    report_list: Path = typer.Option(
        ...,
        "--report-list",
        help="File with one report code or URL per line. Check it first with `report-list-check`.",
    ),
    profile: str = typer.Option(
        "mechanics", "--profile", help="Event profile from config/sampling.yml."
    ),
    dungeon: str | None = typer.Option(
        None, "--dungeon", help='Only collect runs of this dungeon, e.g. "Murder Row".'
    ),
    focus_player: str | None = typer.Option(
        None,
        "--focus-player",
        help="Character name. Collects the profile's focus_event_types narrowed to "
        "that player only. Runs where the name is absent collect no focus streams "
        "and record a diagnostic saying so.",
    ),
    max_runs: int | None = typer.Option(
        None, "--max-runs-per-report", help="Cap runs taken from each report."
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Re-fetch events for runs already collected. Needed after a normalizer change; "
        "otherwise completed streams are skipped.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be collected and make no API calls."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    database: Path | None = typer.Option(
        None,
        "--database",
        help="Database file to use. A bare name resolves inside the configured "
        "database directory. Partition by season or dungeon to keep aggregates fast.",
    ),
) -> None:
    """Collect reports into the local database.

    Safe to interrupt: pagination checkpoints live in the database, so re-running
    the same command resumes from the last page that actually landed. Safe to
    re-run: a completed run is not collected twice.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    config = _load_config()

    try:
        source = ManualReportSource.from_file(report_list)
    except DiscoveryError as exc:
        _fail(exc)
        return

    candidates = list(source.discover())
    typer.echo(f"Report list:   {report_list} ({len(candidates)} report(s))")
    typer.echo(f"Event profile: {profile}")
    if dungeon:
        typer.echo(f"Dungeon:       {dungeon}")

    if dry_run:
        typer.secho("\nDry run - no API calls made.", fg=typer.colors.YELLOW)
        event_profile = config.sampling.event_profile(profile)
        typer.echo("Would fetch these event streams per run:")
        for spec in event_profile.event_types:
            data_type, hostility = type(config.sampling).parse_event_type(spec)
            typer.echo(f"  - {data_type}" + (f" (hostility: {hostility})" if hostility else ""))
        typer.echo("\nReports:")
        for candidate in candidates[:10]:
            typer.echo(f"  {candidate.code}")
        if len(candidates) > 10:
            typer.echo(f"  ... and {len(candidates) - 10} more")
        return

    db = _database(settings, path=database)
    with _client(settings) as client:
        collector = Collector(client, db, config)
        if refresh:
            for row in db.query("SELECT run_id FROM dungeon_runs"):
                collector.reset_run_events(row["run_id"])
            typer.echo("Cleared existing events; they will be fetched again.")
        try:
            result = collector.collect(
                candidates,
                event_profile=profile,
                dungeon_key=dungeon,
                max_runs_per_report=max_runs,
                focus_player=focus_player,
            )
        except KeyboardInterrupt:
            typer.secho(
                "\nInterrupted. Everything fetched so far is saved - re-run the same "
                "command to resume.",
                fg=typer.colors.YELLOW,
            )
            raise typer.Exit(code=130) from None
        except ApiError as exc:
            _fail(exc)
            return

    summary = result.summary()
    typer.echo("")
    typer.secho(
        f"Collected {summary['runs_complete']} of {summary['runs']} run(s) from "
        f"{summary['reports_completed']} report(s).",
        fg=typer.colors.GREEN if not summary["reports_failed"] else typer.colors.YELLOW,
    )
    typer.echo(f"  Events written: {summary['events_written']:,}")
    typer.echo(f"  Pages fetched:  {summary['pages_fetched']:,}")
    focus = summary["focus"]
    if focus["requested"]:
        typer.echo(
            f"  Focus player:   resolved in {focus['runs_resolved']} of {focus['runs_seen']} run(s)"
        )
    if summary["reports_failed"]:
        typer.secho(f"  Reports failed: {summary['reports_failed']}", fg=typer.colors.RED)
        for error in summary["errors"]:
            typer.echo(f"    {error}")
    typer.echo(f"  Job ID:         {summary['job_id']}")
    # Name the file that was written. With --database there is more than one,
    # and "which database did that go into" should never be a guess.
    typer.echo(f"  Database:       {db.path}")
    typer.echo(
        "\nNext: wclmplus validate"
        + (f" --database {database}" if database else "")
        + "  (writes data/exports/validation/)"
    )
    db.close()

    # A focus player who matched nothing anywhere is a failed request, not a
    # quiet absence: everything the profile promised for that player is
    # missing. Exit non-zero so a scripted collection stops instead of
    # building a corpus that looks complete and has no focus data in it.
    if focus["unresolved"]:
        typer.secho(
            f"\nFocus player {focus_player!r} matched no actor in any of "
            f"{focus['runs_seen']} run(s); no focus stream was collected. "
            "Names are stored pseudonymized, so check the spelling, drop any "
            "-Realm suffix, or pass the player- pseudonym from a validation report.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)


@app.command()
def benchmark(
    report_list: Path = typer.Option(
        ..., "--report-list", help="File with report codes or URLs. 3-5 is enough."
    ),
    streams: str = typer.Option(
        "DamageDone,Healing,Resources,Threat,CombatantInfo",
        "--streams",
        help="Comma-separated EventDataType values to probe.",
    ),
    hostility_check: bool = typer.Option(
        True,
        "--hostility-check/--no-hostility-check",
        help="Also request each stream unfiltered, @Enemies and @Friendlies, and compare.",
    ),
    max_runs: int = typer.Option(1, "--runs-per-report", help="Fights probed per report."),
    max_pages: int = typer.Option(
        12, "--max-pages", help="Page cap per stream. Caps cost; a capped stream is a lower bound."
    ),
    out: Path = typer.Option(
        Path("data/exports/benchmark"), "--out", help="Where the report is written."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Measure what uncollected streams cost, without touching the corpus.

    Writes nothing to the collection database. Five stream categories are
    currently unmeasured and two of them are plausibly larger than everything
    already stored, so this exists to replace an estimate with a number before
    any of them becomes a default.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    client = _client(settings)
    bench = StreamBenchmark(client)

    try:
        candidates = list(ManualReportSource.from_file(report_list).discover())
    except DiscoveryError as exc:
        _fail(exc, "Check the report list path.")
        return

    wanted = [s.strip() for s in streams.split(",") if s.strip()]
    measurements: list[StreamMeasurement] = []
    comparisons: list[HostilityComparison] = []
    probed = 0

    collector = Collector(client, _database(settings), ProjectConfig.load())
    try:
        for candidate in candidates:
            fights = [f for f in collector.fetch_fights(candidate.code) if is_mythic_plus(f)]
            for fight in fights[:max_runs]:
                fight_id = int(fight.get("id") or 0)
                start = int(fight.get("startTime") or 0)
                end = int(fight.get("endTime") or 0)
                probed += 1
                typer.echo(f"Probing {candidate.code} fight {fight_id}…")

                for data_type in wanted:
                    if hostility_check:
                        comparison, runs = bench.compare_hostility(
                            report_code=candidate.code,
                            fight_id=fight_id,
                            rel_start_ms=start,
                            rel_end_ms=end,
                            data_type=data_type,
                            max_pages=max_pages,
                        )
                        comparisons.append(comparison)
                        measurements.extend(runs)
                        typer.echo(f"  {data_type}: {comparison.verdict}")
                    else:
                        m = bench.measure(
                            report_code=candidate.code,
                            fight_id=fight_id,
                            rel_start_ms=start,
                            rel_end_ms=end,
                            data_type=data_type,
                            max_pages=max_pages,
                        )
                        measurements.append(m)
                        typer.echo(f"  {m.label}: {m.events:,} events, {m.pages} pages")
    except (ApiError, DiscoveryError) as exc:
        _fail(exc, "The probe stopped; partial results are still written.")

    out.mkdir(parents=True, exist_ok=True)
    md = out / "STREAM_BENCHMARK.md"
    js = out / "stream_benchmark.json"
    md.write_text(render_report(measurements, comparisons, runs_probed=probed), encoding="utf-8")
    js.write_text(render_json(measurements, comparisons, runs_probed=probed), encoding="utf-8")
    typer.secho(f"\nWrote {md}", fg=typer.colors.GREEN)
    typer.secho(f"Wrote {js}", fg=typer.colors.GREEN)
    client.close()


@app.command()
def dedupe(
    database: Path | None = typer.Option(
        None,
        "--database",
        help="Database file to use. A bare name resolves inside the configured "
        "database directory. Partition by season or dungeon to keep aggregates fast.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Group probable duplicate uploads of the same real run.

    Nothing is deleted. One member of each group is marked canonical; the rest
    stay queryable, because merging two genuinely different runs would corrupt
    the data in a way that cannot be undone.
    """
    _setup_logging(verbose)
    db = _database(_load_settings(), path=database)
    typer.echo(f"Database: {db.path}")
    groups = group_duplicates(db)
    if not groups:
        typer.secho("No probable duplicate runs found.", fg=typer.colors.GREEN)
    else:
        typer.secho(f"{len(groups)} probable duplicate group(s):", fg=typer.colors.YELLOW)
        for members in list(groups.values())[:20]:
            typer.echo(f"  {' = '.join(members)}")
        typer.echo("\nNothing deleted. One run per group is marked canonical.")
    db.close()


@app.command()
def packs(
    dungeon: str | None = typer.Option(None, "--dungeon", help='e.g. "Murder Row".'),
    npc: int | None = typer.Option(
        None, "--npc", help="NPC game ID. Lists every pull anywhere that contained it."
    ),
    exact: bool = typer.Option(
        False, "--exact", help="Group by exact composition (counts) instead of species."
    ),
    limit: int = typer.Option(25, "--limit"),
    database: Path | None = typer.Option(None, "--database", help="Database file to read."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Find packs and NPCs across every collected log.

    A retrieval index, not an analysis. It answers "where is this pack, and how
    often did it occur" so the analyst can go to those pulls; it says nothing
    about what any of it means.

    Species grouping is the default: the same trash group pulled with three of
    something or five is one pack, not two. `--exact` groups by counts instead,
    which is how oversized pulls separate from ordinary ones.
    """
    _setup_logging(verbose)
    db = _database(_load_settings(), path=database)

    if npc is not None:
        rows = db.query(
            "SELECT p.dungeon_key, p.pull_id, p.name, p.is_boss, p.duration_ms, "
            "       n.instance_count, n.instance_count_confidence "
            "  FROM pull_npcs n JOIN pulls p ON p.pull_id = n.pull_id "
            " WHERE n.npc_game_id = ? ORDER BY p.dungeon_key, p.pull_id LIMIT ?",
            (npc, limit),
        )
        if not rows:
            typer.secho(f"NPC {npc} appears in no collected pull.", fg=typer.colors.YELLOW)
        else:
            typer.echo(f"NPC {npc} appears in {len(rows)} pull(s) (showing up to {limit}):\n")
            for r in rows:
                count = r["instance_count"]
                conf = r["instance_count_confidence"]
                shown = "?" if count is None else str(count)
                typer.echo(
                    f"  {r['dungeon_key'] or '?':20} {r['pull_id']:26} "
                    f"x{shown} ({conf})  {r['duration_ms'] / 1000:6.1f}s"
                    + ("  [boss]" if r["is_boss"] else "")
                )
        db.close()
        return

    column = "composition_signature" if exact else "species_signature"
    where = "WHERE dungeon_key = ?" if dungeon else ""
    params: tuple[Any, ...] = (dungeon,) if dungeon else ()
    rows = db.query(
        f"SELECT dungeon_key, {column} AS sig, COUNT(*) AS occurrences, "
        "       COUNT(DISTINCT run_id) AS runs, "
        "       ROUND(AVG(duration_ms) / 1000.0, 1) AS mean_s, "
        "       MAX(is_boss) AS boss, MIN(name) AS a_name "
        f"  FROM pulls {where} "
        f" {'AND' if where else 'WHERE'} {column} IS NOT NULL AND {column} != '' "
        f" GROUP BY dungeon_key, {column} "
        " ORDER BY occurrences DESC LIMIT ?",
        (*params, limit),
    )
    if not rows:
        typer.secho(
            "No packs found. Runs collected before schema 3 carry no species "
            "signature; re-collect to populate it.",
            fg=typer.colors.YELLOW,
        )
        db.close()
        return

    typer.echo(f"{'dungeon':20} {'occ':>5} {'runs':>5} {'mean':>7}  pack")
    for r in rows:
        species = len((r["sig"] or "").split("|"))
        typer.echo(
            f"{(r['dungeon_key'] or '?'):20} {r['occurrences']:5} {r['runs']:5} "
            f"{r['mean_s']:6.1f}s  {species} species"
            + ("  [boss]" if r["boss"] else "")
            + f"  {r['a_name'] or ''}"
        )
    typer.echo("\n  ids: use --npc <gameID> to list every pull containing one NPC.")
    db.close()


@app.command()
def validate(
    dungeon: str | None = typer.Option(
        None, "--dungeon", help="Restrict the report to one dungeon."
    ),
    json_output: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    database: Path | None = typer.Option(
        None,
        "--database",
        help="Database file to use. A bare name resolves inside the configured "
        "database directory. Partition by season or dungeon to keep aggregates fast.",
    ),
) -> None:
    """Write the validation report for the collected corpus.

    Produces `data/exports/validation/VALIDATION_REPORT.md` and
    `validation.json`, including a reconstructed per-NPC-copy mechanic timeline
    as evidence that instance identity survived collection.
    """
    _setup_logging(verbose)
    settings = _load_settings()
    db = _database(settings, path=database)

    dungeon_key = _load_config().dungeons.resolve(dungeon).key if dungeon else None
    report = collect_validation(db, dungeon_key=dungeon_key)

    if json_output:
        _echo_json(report)
        db.close()
        return

    json_path, md_path = write_reports(report, settings.exports_dir / "validation")

    runs, events = report["runs"], report["events"]
    typer.echo(f"Runs analysable:  {runs['analysable']}")
    typer.echo(f"Events:           {events['total']:,}")
    typer.echo(f"Assigned to pull: {events['assigned_to_pulls']:,} ({events['assigned_pct']}%)")
    if report["pages"]["incomplete_streams"]:
        typer.secho(
            f"Incomplete event streams: {report['pages']['incomplete_streams']} "
            "- re-run `collect` to resume.",
            fg=typer.colors.YELLOW,
        )

    evidence = report["npc_instance_evidence"]
    if evidence:
        typer.secho(
            f"\nNPC instance identity demonstrated on {len(evidence)} pull(s):",
            fg=typer.colors.GREEN,
        )
        for item in evidence[:3]:
            copies = ", ".join(
                f"copy {c['instance']}: {c['casts']} cast(s) from {c['first_cast_ms_into_pull']}ms"
                for c in item["per_copy"]
            )
            typer.echo(f"  {item['npc']} / {item['ability']} - {copies}")
    else:
        typer.secho(
            "\nNo pull held two copies of one NPC casting, so instance separation is "
            "UNTESTED in this corpus.",
            fg=typer.colors.YELLOW,
        )

    if report["limitations"]:
        typer.secho(f"\n{len(report['limitations'])} limitation(s):", fg=typer.colors.YELLOW)
        for index, text in enumerate(report["limitations"], start=1):
            typer.echo(f"  {index}. {text}")

    typer.echo(f"\nReport:   {md_path}")
    typer.echo(f"Evidence: {json_path}")
    db.close()


@app.command()
def stats(
    database: Path | None = typer.Option(
        None,
        "--database",
        help="Database file to use. A bare name resolves inside the configured "
        "database directory. Partition by season or dungeon to keep aggregates fast.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show what is in the local database."""
    _setup_logging(verbose)
    settings = _load_settings()
    db = _database(settings, path=database)

    typer.echo(f"Database: {db.path}\n")
    counts = db.table_counts()
    typer.echo(f"Database: {db.path}  ({db.size_bytes() / 1_048_576:.1f} MiB)")
    typer.echo("")
    for table, count in counts.items():
        if count:
            typer.echo(f"  {table:<22} {count:>10,}")

    runs = db.query(
        "SELECT dungeon_key, key_bracket, COUNT(*) AS runs, "
        "       ROUND(AVG(duration_ms)/1000.0) AS mean_s "
        "  FROM dungeon_runs WHERE collection_status = 'complete' "
        " GROUP BY dungeon_key, key_bracket ORDER BY dungeon_key, key_bracket"
    )
    if runs:
        typer.echo("\nComplete runs:")
        typer.echo(f"  {'dungeon':<22} {'bracket':<10} {'runs':>5} {'mean dur':>10}")
        for row in runs:
            typer.echo(
                f"  {str(row['dungeon_key']):<22} {str(row['key_bracket']):<10} "
                f"{row['runs']:>5} {str(row['mean_s']) + 's':>10}"
            )
    else:
        typer.echo("\nNo complete runs yet. Run `wclmplus collect --report-list <file>`.")
    db.close()


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except RedactedError as exc:  # pragma: no cover - top-level safety net
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover
    main()

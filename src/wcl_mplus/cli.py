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

from .client import ApiError, GraphQLClient
from .configs import ConfigFileError, ProjectConfig
from .discover import discover_dungeons
from .querybuild import WANTED_FIGHT_FIELDS, WANTED_REPORT_FIELDS, QueryError, render
from .rawcache import RawCache
from .recon import Recon
from .redaction import RedactedError, install_logging_redaction
from .reportsource import DiscoveryError, ManualReportSource
from .schema import SchemaIntrospector
from .settings import ConfigError, Settings
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


def main() -> None:
    """Console-script entry point."""
    try:
        app()
    except RedactedError as exc:  # pragma: no cover - top-level safety net
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":  # pragma: no cover
    main()

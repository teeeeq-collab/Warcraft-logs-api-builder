"""CLI tests.

Offline commands are tested directly. Live commands are tested by injecting
the simulator in place of the real HTTP client, so no network or credentials
are needed.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner
from wcl_simulator import REPORT_CODE, WclSimulator

from wcl_mplus import cli
from wcl_mplus.auth import Token

runner = CliRunner()

TOKEN = "cli-test-token-aaaaaaaaaaaaaa"


class FakeTokens:
    def auth_header(self):
        return {"Authorization": f"Bearer {TOKEN}"}

    def token(self, force_refresh=False):
        return Token(TOKEN, 9e18)

    def close(self):
        pass


@pytest.fixture
def live(monkeypatch, settings, tmp_path):
    """Point the CLI at the simulator with throwaway credentials."""
    simulator = WclSimulator(event_page_limit=10)

    monkeypatch.setattr(cli, "_load_settings", lambda: settings)

    real_client = cli._client

    def patched(settings_arg):
        client = real_client(settings_arg)
        client.tokens = FakeTokens()
        client._http = simulator.client()
        client._sleep = lambda s: None
        return client

    monkeypatch.setattr(cli, "_client", patched)
    return simulator


# -- offline ---------------------------------------------------------------


def test_help_lists_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("auth-check", "recon", "config-check", "inspect-report", "discover-dungeons"):
        assert command in result.output


def test_every_command_has_help():
    for command in (
        "auth-check",
        "config-check",
        "schema-check",
        "inspect-report",
        "recon",
        "discover-dungeons",
        "cache-stats",
        "cache-audit",
        "report-list-check",
        "version",
    ):
        result = runner.invoke(cli.app, [command, "--help"])
        assert result.exit_code == 0, f"{command} --help failed"
        assert len(result.output.strip()) > 40, f"{command} help is too thin"


def test_version_reports_provenance():
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["software_version"]
    assert payload["schema_version"] == 0, "no database schema exists in Phase 0"


def test_config_check_reports_unverified_dungeons():
    result = runner.invoke(cli.app, ["config-check"])
    assert result.exit_code == 0
    assert "Murder Row" in result.output
    assert "never hardcoded" in result.output


def test_config_check_json():
    result = runner.invoke(cli.app, ["config-check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["dungeons"]["verified_dungeons"] == 0


def test_report_list_check(tmp_path):
    path = tmp_path / "reports.txt"
    path.write_text(
        "AAAAAAAAAAAAAAAA\nhttps://www.warcraftlogs.com/reports/BBBBBBBBBBBBBBBB\nnot a code\n"
    )
    result = runner.invoke(cli.app, ["report-list-check", str(path)])
    assert result.exit_code == 0
    assert "2 usable report code(s)" in result.output
    assert "'not a code'" in result.output


def test_report_list_check_missing_file(tmp_path):
    result = runner.invoke(cli.app, ["report-list-check", str(tmp_path / "nope.txt")])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cache_stats_on_empty_cache(monkeypatch, settings):
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    result = runner.invoke(cli.app, ["cache-stats"])
    assert result.exit_code == 0
    assert "empty" in result.output


def test_cache_audit_without_credentials(monkeypatch, settings):
    from dataclasses import replace

    monkeypatch.setattr(
        cli, "_load_settings", lambda: replace(settings, client_id=None, client_secret=None)
    )
    result = runner.invoke(cli.app, ["cache-audit"])
    assert result.exit_code == 0
    assert "nothing to search for" in result.output


def test_cache_audit_passes_on_clean_cache(monkeypatch, settings, live):
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    runner.invoke(cli.app, ["recon", "--report", REPORT_CODE, "--no-fixtures"])
    result = runner.invoke(cli.app, ["cache-audit"])
    assert result.exit_code == 0
    assert "No credentials found" in result.output


# -- credential errors -----------------------------------------------------


def test_auth_check_without_credentials_is_actionable(monkeypatch, settings):
    from dataclasses import replace

    monkeypatch.setattr(
        cli, "_load_settings", lambda: replace(settings, client_id=None, client_secret=None)
    )
    result = runner.invoke(cli.app, ["auth-check"])
    assert result.exit_code == 2
    assert "cp .env.example .env" in result.output
    assert "api/clients" in result.output


# -- live commands against the simulator -----------------------------------


def test_auth_check_reports_budget(live):
    result = runner.invoke(cli.app, ["auth-check"])
    assert result.exit_code == 0
    assert "Authentication OK" in result.output
    assert "Limit per hour" in result.output
    assert TOKEN not in result.output, "the token must never be printed"


def test_schema_check_lists_present_and_absent_fields(live):
    result = runner.invoke(cli.app, ["schema-check"])
    assert result.exit_code == 0
    assert "keystoneLevel" in result.output
    assert "EventDataType live values" in result.output


def test_schema_check_flags_absent_fields(monkeypatch, settings):
    simulator = WclSimulator(drop_fields={"ReportFight": {"keystoneTime"}})
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    real_client = cli._client

    def patched(settings_arg):
        client = real_client(settings_arg)
        client.tokens = FakeTokens()
        client._http = simulator.client()
        return client

    monkeypatch.setattr(cli, "_client", patched)
    result = runner.invoke(cli.app, ["schema-check"])
    assert result.exit_code == 0
    assert "keystoneTime: ABSENT" in result.output
    assert "Limitations:" in result.output


def test_inspect_report_summarizes_mythic_plus_runs(live):
    result = runner.invoke(cli.app, ["inspect-report", REPORT_CODE])
    assert result.exit_code == 0
    assert "Mythic+ runs:" in result.output
    assert "+10" in result.output
    assert "bracket 10" in result.output


def test_inspect_report_accepts_a_url(live):
    result = runner.invoke(
        cli.app, ["inspect-report", f"https://www.warcraftlogs.com/reports/{REPORT_CODE}#fight=7"]
    )
    assert result.exit_code == 0
    assert "Mythic+ runs:" in result.output


def test_inspect_report_rejects_nonsense_reference(live):
    result = runner.invoke(cli.app, ["inspect-report", "not a code"])
    assert result.exit_code == 2
    assert "not a report code" in result.output


def test_inspect_report_handles_missing_report(monkeypatch, settings):
    monkeypatch.setattr(cli, "_load_settings", lambda: settings)
    base = WclSimulator()

    def handler(request):
        # Dispatch on real introspection markers: a rendered query still
        # carries its template's `__PLACEHOLDER__` comment, so a bare "__"
        # check would misroute it.
        body = json.loads(request.content.decode())
        query = body.get("query", "")
        if "__schema" in query or "__type(" in query:
            return base.handler(request)
        return httpx.Response(200, json={"data": {"reportData": {"report": None}}})

    real_client = cli._client

    def patched(settings_arg):
        client = real_client(settings_arg)
        client.tokens = FakeTokens()
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    monkeypatch.setattr(cli, "_client", patched)
    result = runner.invoke(cli.app, ["inspect-report", "MissingRpt123456"])
    assert result.exit_code == 1
    assert "may be private" in result.output


def test_discover_dungeons_dry_run_writes_nothing(live, tmp_path):
    result = runner.invoke(cli.app, ["discover-dungeons"])
    assert result.exit_code == 0
    assert "Matched 2 dungeon(s)" in result.output
    assert "Dry run" in result.output
    assert not (
        cli.ProjectConfig.load().dungeons.path.with_name("dungeons.discovered.yml")
    ).exists()


def test_recon_writes_reports_and_summary(live, settings):
    result = runner.invoke(cli.app, ["recon", "--report", REPORT_CODE, "--no-fixtures"])
    assert result.exit_code == 0
    assert "Recon complete" in result.output
    assert "Pagination cursor" in result.output
    report = settings.exports_dir / "recon" / "RECON_REPORT.md"
    assert report.is_file()
    assert TOKEN not in report.read_text()


def test_recon_rejects_bad_report_reference(live):
    result = runner.invoke(cli.app, ["recon", "--report", "%%%"])
    assert result.exit_code == 2
    assert "not a report code" in result.output


def test_recon_without_a_report_still_runs(live, settings):
    result = runner.invoke(cli.app, ["recon", "--no-fixtures"])
    assert result.exit_code == 0
    assert "limitation(s) recorded" in result.output
    assert "No report was inspected" in result.output


def test_recon_writes_sanitized_fixtures(live, settings, monkeypatch, tmp_path):
    """Fixtures must contain no player names."""
    fixtures = tmp_path / "fixtures"
    monkeypatch.setattr(type(settings), "fixtures_dir", property(lambda self: fixtures))
    result = runner.invoke(cli.app, ["recon", "--report", REPORT_CODE, "--fixtures"])
    assert result.exit_code == 0
    written = list(fixtures.glob("*.json"))
    assert written, "fixtures were written"
    combined = "\n".join(p.read_text() for p in written)
    for name in ("Tankadin", "Brewhealz", "SomeUploader"):
        assert name not in combined, f"{name} leaked into a fixture"
    assert "Shivan Punisher" in combined, "NPC names are research data and are kept"

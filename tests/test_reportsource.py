"""Report discovery tests."""

from __future__ import annotations

import pytest

from wcl_mplus.reportsource import (
    DiscoveryError,
    DiscoveryUnverified,
    ManualReportSource,
    extract_report_code,
    parse_report_list,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://www.warcraftlogs.com/reports/aBcD1234EfGh5678", "aBcD1234EfGh5678"),
        (
            "https://www.warcraftlogs.com/reports/aBcD1234EfGh5678#fight=3&type=casts",
            "aBcD1234EfGh5678",
        ),
        ("http://warcraftlogs.com/reports/aBcD1234EfGh5678", "aBcD1234EfGh5678"),
        ("www.warcraftlogs.com/reports/aBcD1234EfGh5678", "aBcD1234EfGh5678"),
        ("https://classic.warcraftlogs.com/reports/compare/aBcD1234EfGh5678", "aBcD1234EfGh5678"),
        ("aBcD1234EfGh5678", "aBcD1234EfGh5678"),
        ("  aBcD1234EfGh5678  ", "aBcD1234EfGh5678"),
        ("aBcD1234EfGh5678/", "aBcD1234EfGh5678"),
        ("aBcD1234EfGh5678?x=1", "aBcD1234EfGh5678"),
    ],
)
def test_valid_report_references(text, expected):
    assert extract_report_code(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "short",
        "not a code at all",
        "https://example.com/reports/aBcD1234EfGh5678",
        "https://evil-warcraftlogs.com.attacker.test/reports/aBcD1234EfGh5678",
        "aBcD-1234-EfGh",
    ],
)
def test_invalid_report_references(text):
    assert extract_report_code(text) is None


def test_parse_list_handles_comments_blanks_csv_and_duplicates():
    codes, rejected = parse_report_list(
        "\n".join(
            [
                "# my pilot sample",
                "",
                "https://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA",
                "BBBBBBBBBBBBBBBB,10,timed,EU",
                '"CCCCCCCCCCCCCCCC"',
                "AAAAAAAAAAAAAAAA",
                "   ",
                "garbage",
            ]
        )
    )
    assert codes == ["AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB", "CCCCCCCCCCCCCCCC"]
    assert rejected == ["garbage"]


def test_duplicate_lines_are_not_two_observations():
    codes, _ = parse_report_list("AAAAAAAAAAAAAAAA\nAAAAAAAAAAAAAAAA\n")
    assert codes == ["AAAAAAAAAAAAAAAA"]


def test_manual_source_records_provenance():
    source = ManualReportSource.from_iterable(
        ["AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB"], seed="pilot-list"
    )
    candidates = list(source.discover())
    assert [c.code for c in candidates] == ["AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB"]
    first = candidates[0].provenance
    assert first.source_type == "manual"
    assert first.seed == "pilot-list"
    assert first.rank == 0
    assert first.discovered_at > 0
    assert first.extra["list_size"] == 2


def test_manual_source_limit_is_respected():
    source = ManualReportSource.from_iterable(["A" * 16, "B" * 16, "C" * 16])
    assert len(list(source.discover(limit=2))) == 2


def test_manual_source_reports_rejects():
    source = ManualReportSource.from_iterable(["A" * 16, "nope"])
    assert source.codes == ["A" * 16]
    assert source.rejected == ["nope"]


def test_from_file_roundtrip(tmp_path):
    path = tmp_path / "reports.txt"
    path.write_text(
        "# list\nhttps://www.warcraftlogs.com/reports/AAAAAAAAAAAAAAAA\nBBBBBBBBBBBBBBBB\n"
    )
    source = ManualReportSource.from_file(path)
    assert source.codes == ["AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB"]
    assert source.source_type == "manual_file"
    assert source.seed == str(path)


def test_from_file_missing(tmp_path):
    with pytest.raises(DiscoveryError, match="not found"):
        ManualReportSource.from_file(tmp_path / "nope.txt")


def test_from_file_with_no_usable_codes_explains(tmp_path):
    path = tmp_path / "reports.txt"
    path.write_text("# only comments\nnot a code\n")
    with pytest.raises(DiscoveryError, match="No usable report codes"):
        ManualReportSource.from_file(path)


def test_bare_word_of_code_length_is_accepted_as_a_code():
    """Documented limitation, not an oversight.

    Report codes are alphanumeric and their length is not pinned by this
    project, so any 8-32 character alphanumeric word is indistinguishable from
    a code. Parsing errs toward accepting: a wrong code fails loudly at the API
    with a clear message, whereas rejecting by a guessed length would silently
    drop valid reports from a sample. `wclmplus report-list-check` exists so
    typos are seen before a collection run.
    """
    assert extract_report_code("nonsense") == "nonsense"


def test_api_sources_refuse_to_run_unverified():
    """An unverified discovery path must explain itself, not send a bad query."""
    from wcl_mplus.reportsource import CharacterReportSource, GuildReportSource

    class NoSchema:
        def type_info(self, name):
            return None

    for source in (
        CharacterReportSource(
            client=None, introspector=NoSchema(), name="x", server_slug="y", region="eu"
        ),
        GuildReportSource(client=None, introspector=NoSchema(), guild_id=1),
    ):
        with pytest.raises(DiscoveryUnverified, match="recon --discovery"):
            list(source.discover())

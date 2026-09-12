"""Dungeon discovery tests."""

from __future__ import annotations

import yaml
from wcl_simulator import WclSimulator

from wcl_mplus.auth import Token
from wcl_mplus.client import GraphQLClient
from wcl_mplus.configs import DungeonRegistry
from wcl_mplus.discover import build_overlay, discover_dungeons, match_zones
from wcl_mplus.ratelimit import RateLimiter
from wcl_mplus.rawcache import RawCache


class FakeTokens:
    def auth_header(self):
        return {"Authorization": "Bearer simulator-token-aaaaaaaa"}

    def token(self, force_refresh=False):
        return Token("simulator-token-aaaaaaaa", 9e18)

    def close(self):
        pass


def build_client(settings, simulator):
    return GraphQLClient(
        settings,
        token_provider=FakeTokens(),
        rate_limiter=RateLimiter(min_points_reserve=0),
        cache=RawCache(settings.raw_cache_dir),
        http_client=simulator.client(),
        sleep=lambda s: None,
    )


def test_discovery_matches_known_dungeons(settings):
    registry = DungeonRegistry.load()
    result = discover_dungeons(build_client(settings, WclSimulator()), registry)
    assert set(result["matched"]) == {"murder-row", "ruby-life-pools"}
    assert result["matched"]["murder-row"]["wcl_zone_id"] == 44
    assert result["matched"]["murder-row"]["encounter_ids"] == [12801, 12802]
    assert set(result["unmatched"]) == {"The Blinding Vale", "Den of Nalorakk"}


def test_dry_run_writes_nothing(settings, tmp_path):
    registry = DungeonRegistry.load()
    overlay = tmp_path / "dungeons.discovered.yml"
    discover_dungeons(build_client(settings, WclSimulator()), registry, overlay_path=overlay)
    assert not overlay.exists(), "discovery must not write without --write"


def test_write_produces_a_mergeable_overlay(settings, tmp_path):
    registry = DungeonRegistry.load()
    overlay = tmp_path / "dungeons.discovered.yml"
    result = discover_dungeons(
        build_client(settings, WclSimulator()), registry, write=True, overlay_path=overlay
    )
    assert result["overlay_written"] == str(overlay)
    text = overlay.read_text()
    assert "GENERATED FILE" in text
    parsed = yaml.safe_load(text)
    keys = {entry["key"] for entry in parsed["dungeons"]}
    assert keys == {"murder-row", "ruby-life-pools"}
    assert all(entry["verified"] for entry in parsed["dungeons"])


def test_season_is_not_marked_verified_while_dungeons_are_missing(settings, tmp_path):
    """Two of four matched is not a verified season."""
    registry = DungeonRegistry.load()
    result = discover_dungeons(build_client(settings, WclSimulator()), registry)
    overlay = build_overlay(result, registry)
    assert overlay["season"]["verified"] is False


def test_season_marked_verified_when_all_match():
    class Reg:
        season_id = "s"
        dungeons = [
            type("E", (), {"display_name": "Murder Row", "aliases": [], "key": "murder-row"})()
        ]

    zones = [{"id": 44, "name": "Murder Row", "encounters": [{"id": 1, "name": "b"}]}]
    result = match_zones(Reg(), zones)
    assert build_overlay(result, Reg())["season"]["verified"] is True


def test_ambiguous_zone_names_are_reported_not_guessed():
    class Reg:
        season_id = "s"
        dungeons = [
            type("E", (), {"display_name": "Murder Row", "aliases": [], "key": "murder-row"})()
        ]

    zones = [
        {"id": 44, "name": "Murder Row", "encounters": []},
        {"id": 77, "name": "murder row", "encounters": []},
    ]
    result = match_zones(Reg(), zones)
    assert result["matched"] == {}
    assert result["ambiguous"] == {"murder-row": [44, 77]}


def test_aliases_hitting_the_same_zone_are_not_ambiguous():
    class Reg:
        season_id = "s"
        dungeons = [
            type(
                "E",
                (),
                {
                    "display_name": "Murder Row",
                    "aliases": ["murder row", "mr"],
                    "key": "murder-row",
                },
            )()
        ]

    zones = [{"id": 44, "name": "Murder Row", "encounters": []}]
    result = match_zones(Reg(), zones)
    assert result["matched"]["murder-row"]["wcl_zone_id"] == 44
    assert result["ambiguous"] == {}

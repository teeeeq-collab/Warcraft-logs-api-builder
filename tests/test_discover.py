"""Dungeon discovery tests.

The central case here is a regression: the first live run of
`discover-dungeons` matched **0 of 4** configured dungeons, because Warcraft
Logs models a Mythic+ season as one zone whose *encounters* are the individual
dungeons, and this code was comparing dungeon names against *zone* names.
"""

from __future__ import annotations

import pathlib

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


def fake_registry(*names: str, season_id: str = "midnight-s2"):
    """Minimal stand-in for a DungeonRegistry with the given dungeon names."""

    class Reg:
        pass

    Reg.season_id = season_id
    Reg.path = pathlib.Path("config/dungeons.yml")
    Reg.dungeons = [
        type(
            "Entry",
            (),
            {
                "display_name": name,
                "aliases": [],
                "key": name.lower().replace(" ", "-"),
            },
        )()
        for name in names
    ]
    return Reg()


# -- the regression --------------------------------------------------------


def test_dungeons_are_matched_as_encounters_not_zones(settings):
    """Dungeons live in `Zone.encounters`, not in the zone list.

    Against the real API, matching on zone names found nothing at all.
    """
    registry = DungeonRegistry.load()
    result = discover_dungeons(build_client(settings, WclSimulator()), registry)

    assert set(result["matched"]) == {
        "murder-row",
        "ruby-life-pools",
        "the-blinding-vale",
        "den-of-nalorakk",
        "altar-of-fangs",
        "voidscar-arena",
        "temple-of-sethraliss",
        "kings-rest",
    }
    assert result["unmatched"] == []
    assert result["ambiguous"] == {}

    murder_row = result["matched"]["murder-row"]
    assert murder_row["match_kind"] == "encounter"
    assert murder_row["wcl_zone_id"] == 44, "the season zone containing it"
    assert murder_row["encounter_ids"] == [12813], "its own encounter ID"
    assert murder_row["wcl_zone_name"] == "Mythic+ Season 2"


def test_season_zone_is_derived_when_all_dungeons_agree(settings):
    registry = DungeonRegistry.load()
    result = discover_dungeons(build_client(settings, WclSimulator()), registry)
    assert result["season_zone_id"] == 44


def test_zone_name_fallback_still_works():
    """A dungeon that is its own zone is still matched."""
    zones = [{"id": 42, "name": "A Raid Tier", "encounters": [{"id": 1, "name": "Some Boss"}]}]
    result = match_zones(fake_registry("A Raid Tier"), zones)
    assert result["matched"]["a-raid-tier"]["match_kind"] == "zone"
    assert result["matched"]["a-raid-tier"]["encounter_ids"] == [1]


def test_encounter_match_wins_over_zone_match():
    """When a name is both a zone and an encounter, the encounter wins.

    The encounter is the dungeon inside the season; a same-named zone is
    usually an older standalone listing.
    """
    zones = [
        {"id": 10, "name": "Murder Row", "encounters": []},
        {"id": 44, "name": "Mythic+ Season 2", "encounters": [{"id": 12813, "name": "Murder Row"}]},
    ]
    result = match_zones(fake_registry("Murder Row"), zones)
    assert result["matched"]["murder-row"]["match_kind"] == "encounter"
    assert result["matched"]["murder-row"]["wcl_zone_id"] == 44


# -- ambiguity is reported, never guessed ---------------------------------


def test_same_dungeon_name_in_two_seasons_is_ambiguous():
    """A dungeon reused in a later season must not be silently resolved.

    Choosing one arbitrarily would tie the whole corpus to the wrong season.
    """
    zones = [
        {"id": 43, "name": "Mythic+ Season 1", "encounters": [{"id": 1, "name": "Murder Row"}]},
        {"id": 44, "name": "Mythic+ Season 2", "encounters": [{"id": 2, "name": "Murder Row"}]},
    ]
    result = match_zones(fake_registry("Murder Row"), zones)
    assert result["matched"] == {}
    assert result["ambiguous"] == {"murder-row": [43, 44]}
    assert result["season_zone_id"] is None


def test_ambiguous_zone_names_are_reported_not_guessed():
    zones = [
        {"id": 44, "name": "Murder Row", "encounters": []},
        {"id": 77, "name": "murder row", "encounters": []},
    ]
    result = match_zones(fake_registry("Murder Row"), zones)
    assert result["matched"] == {}
    assert result["ambiguous"] == {"murder-row": [44, 77]}


def test_aliases_hitting_the_same_encounter_are_not_ambiguous():
    class Reg:
        season_id = "s"
        path = pathlib.Path("config/dungeons.yml")
        dungeons = [
            type(
                "Entry",
                (),
                {
                    "display_name": "Murder Row",
                    "aliases": ["murder row", "mr"],
                    "key": "murder-row",
                },
            )()
        ]

    zones = [{"id": 44, "name": "M+ S2", "encounters": [{"id": 12813, "name": "Murder Row"}]}]
    result = match_zones(Reg(), zones)
    assert result["matched"]["murder-row"]["wcl_zone_id"] == 44
    assert result["ambiguous"] == {}


def test_unmatched_names_are_listed():
    zones = [{"id": 44, "name": "M+ S2", "encounters": [{"id": 1, "name": "Murder Row"}]}]
    result = match_zones(fake_registry("Murder Row", "Nowhere At All"), zones)
    assert result["unmatched"] == ["Nowhere At All"]
    assert set(result["matched"]) == {"murder-row"}


# -- overlay writing -------------------------------------------------------


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
    assert keys == {
        "murder-row",
        "ruby-life-pools",
        "the-blinding-vale",
        "den-of-nalorakk",
        "altar-of-fangs",
        "voidscar-arena",
        "temple-of-sethraliss",
        "kings-rest",
    }
    assert all(entry["verified"] for entry in parsed["dungeons"])
    assert parsed["season"]["wcl_mplus_zone_id"] == 44
    assert parsed["season"]["verified"] is True


def test_season_not_verified_while_a_dungeon_is_missing():
    zones = [{"id": 44, "name": "M+ S2", "encounters": [{"id": 1, "name": "Murder Row"}]}]
    registry = fake_registry("Murder Row", "Nowhere")
    result = match_zones(registry, zones)
    assert build_overlay(result, registry)["season"]["verified"] is False


def test_season_verified_when_every_dungeon_matches():
    zones = [{"id": 44, "name": "M+ S2", "encounters": [{"id": 12813, "name": "Murder Row"}]}]
    registry = fake_registry("Murder Row")
    result = match_zones(registry, zones)
    overlay = build_overlay(result, registry)
    assert overlay["season"]["verified"] is True
    assert overlay["season"]["wcl_mplus_zone_id"] == 44


# -- reused dungeon names (live ambiguity regression) ---------------------


def test_reused_dungeon_names_resolve_to_the_current_season(settings):
    """Three Season 2 dungeons share a name with an older season.

    Live data: Ruby Life Pools also exists in Dragonflight S1 (zone 32) and S4
    (zone 37); Kings' Rest and Temple of Sethraliss also exist in Battle for
    Azeroth (zone 20). Name-only matching reported them as ambiguous and
    refused to resolve, which is safe but unhelpful. Two-pass matching infers
    the season zone from the dungeons unique to it, then resolves the rest
    inside that zone.
    """
    registry = DungeonRegistry.load()
    result = discover_dungeons(build_client(settings, WclSimulator()), registry)

    assert result["ambiguous"] == {}
    assert result["season_zone_id"] == 44
    assert result["season_zone_source"] == "inferred"

    for key, expected_encounter in (
        ("ruby-life-pools", 112521),
        ("kings-rest", 61762),
        ("temple-of-sethraliss", 61877),
    ):
        item = result["matched"][key]
        assert item["wcl_zone_id"] == 44, f"{key} must resolve to the season zone"
        assert item["encounter_ids"] == [expected_encounter]
        assert item["resolved_via_season_zone"] is True

    assert set(result["resolved_via_season_zone"]) == {
        "ruby-life-pools",
        "kings-rest",
        "temple-of-sethraliss",
    }


def test_configured_season_zone_overrides_inference():
    """An explicit config pin wins over inference."""
    zones = [
        {"id": 32, "name": "Old", "encounters": [{"id": 1, "name": "Ruby Life Pools"}]},
        {"id": 55, "name": "New", "encounters": [{"id": 2, "name": "Ruby Life Pools"}]},
    ]
    registry = fake_registry("Ruby Life Pools")
    registry.wcl_mplus_zone_id = 32
    result = match_zones(registry, zones)
    assert result["season_zone_source"] == "config"
    assert result["matched"]["ruby-life-pools"]["encounter_ids"] == [1]


def test_ambiguity_survives_when_no_season_zone_can_be_inferred():
    """With nothing to anchor on, the name stays ambiguous rather than guessed."""
    zones = [
        {"id": 32, "name": "Old", "encounters": [{"id": 1, "name": "Ruby Life Pools"}]},
        {"id": 55, "name": "New", "encounters": [{"id": 2, "name": "Ruby Life Pools"}]},
    ]
    result = match_zones(fake_registry("Ruby Life Pools"), zones)
    assert result["matched"] == {}
    assert result["ambiguous"] == {"ruby-life-pools": [32, 55]}
    assert result["season_zone_id"] is None

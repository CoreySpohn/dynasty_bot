"""Tests for shared standings computation."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import lib.standings as standings_module
from lib.standings import compute_standings, get_rank_for_discord_id, ordinal


class TestOrdinal:
    @pytest.mark.parametrize(
        "n,expected",
        [
            (1, "1st"),
            (2, "2nd"),
            (3, "3rd"),
            (4, "4th"),
            (11, "11th"),
            (12, "12th"),
            (13, "13th"),
            (21, "21st"),
            (101, "101st"),
            (112, "112th"),
        ],
    )
    def test_ordinal_formatting(self, n, expected):
        assert ordinal(n) == expected


def _roster(roster_id, owner_id, wins, losses, fpts):
    return {
        "roster_id": roster_id,
        "owner_id": owner_id,
        "settings": {"wins": wins, "losses": losses, "fpts": fpts},
    }


@pytest.fixture
def sleeper():
    client = MagicMock()
    client.get_rosters = AsyncMock(
        return_value=[
            _roster(1, "user_a", wins=8, losses=5, fpts=1200),
            _roster(2, "user_b", wins=10, losses=3, fpts=1300),
            _roster(3, "user_c", wins=8, losses=5, fpts=1250),
        ]
    )
    client.get_users = AsyncMock(
        return_value=[
            {"user_id": "user_a", "display_name": "Alice"},
            {"user_id": "user_b", "display_name": "Bob"},
            {"user_id": "user_c", "display_name": "Cara"},
        ]
    )
    return client


class TestComputeStandings:
    async def test_sorts_by_wins_then_points_for(self, sleeper):
        entries = await compute_standings(sleeper, "league1")

        assert [e.owner for e in entries] == ["Bob", "Cara", "Alice"]
        assert [e.rank for e in entries] == [1, 2, 3]

    async def test_missing_user_falls_back_to_team_label(self, sleeper):
        sleeper.get_users = AsyncMock(return_value=[])

        entries = await compute_standings(sleeper, "league1")

        assert all(e.owner.startswith("Team ") for e in entries)


class TestGetRankForDiscordId:
    async def test_resolves_rank_via_member_registry(self, sleeper, monkeypatch):
        member = MagicMock()
        member.sleeper_id = "user_b"
        registry = MagicMock()
        registry.find_by_discord_id = MagicMock(return_value=member)
        monkeypatch.setattr(standings_module, "get_member_registry", lambda: registry)

        rank = await get_rank_for_discord_id(sleeper, "league1", 12345)

        assert rank == 1
        registry.find_by_discord_id.assert_called_once_with(12345)

    async def test_unknown_discord_id_returns_none(self, sleeper, monkeypatch):
        registry = MagicMock()
        registry.find_by_discord_id = MagicMock(return_value=None)
        monkeypatch.setattr(standings_module, "get_member_registry", lambda: registry)

        rank = await get_rank_for_discord_id(sleeper, "league1", 99999)

        assert rank is None

    async def test_member_without_sleeper_id_returns_none(self, sleeper, monkeypatch):
        member = MagicMock()
        member.sleeper_id = None
        registry = MagicMock()
        registry.find_by_discord_id = MagicMock(return_value=member)
        monkeypatch.setattr(standings_module, "get_member_registry", lambda: registry)

        rank = await get_rank_for_discord_id(sleeper, "league1", 12345)

        assert rank is None

    async def test_member_with_no_matching_roster_returns_none(self, sleeper, monkeypatch):
        member = MagicMock()
        member.sleeper_id = "user_not_in_league"
        registry = MagicMock()
        registry.find_by_discord_id = MagicMock(return_value=member)
        monkeypatch.setattr(standings_module, "get_member_registry", lambda: registry)

        rank = await get_rank_for_discord_id(sleeper, "league1", 12345)

        assert rank is None

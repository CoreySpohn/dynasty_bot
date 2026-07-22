"""Tests for the TradeValues cog."""

from unittest.mock import patch

import pytest
import pytest_asyncio

import cogs.trade_values as trade_values_module
from cogs.trade_values import (
    TradeValues,
    get_team_dynasty_value_trends,
    get_team_dynasty_values,
    normalize_name,
)
from database import Database


class TestNormalizeName:
    def test_strips_suffix(self):
        assert normalize_name("Kenneth Walker III") == normalize_name("Kenneth Walker")

    def test_strips_punctuation_and_case(self):
        assert normalize_name("Ja'Marr Chase") == normalize_name("JAMARR CHASE")

    def test_hyphenated_names(self):
        assert normalize_name("Amon-Ra St. Brown") == normalize_name("amon ra st brown")


@pytest.fixture
def trade_values_cog(mock_bot):
    return TradeValues(mock_bot)


class TestMatchSleeperId:
    async def test_matches_on_name_and_position(self, trade_values_cog):
        index = {normalize_name("Ja'Marr Chase"): [("101", "WR")]}
        ktc_player = {"name": "Ja'Marr Chase", "position": "WR"}

        assert trade_values_cog._match_sleeper_id(ktc_player, index) == "101"

    async def test_prefers_position_match_among_duplicates(self, trade_values_cog):
        index = {
            normalize_name("Josh Allen"): [("999", "LB"), ("101", "QB")],
        }
        ktc_player = {"name": "Josh Allen", "position": "QB"}

        assert trade_values_cog._match_sleeper_id(ktc_player, index) == "101"

    async def test_returns_none_when_no_candidate(self, trade_values_cog):
        assert trade_values_cog._match_sleeper_id(
            {"name": "Nobody Special", "position": "WR"}, {}
        ) is None


@pytest_asyncio.fixture
async def test_db():
    """An in-memory database, patched in as the module-level `db` singleton."""
    database = Database(":memory:")
    await database.connect()
    with patch.object(trade_values_module, "db", database):
        yield database
    await database.close()


class TestGetLatestValueAndTrend:
    async def test_exact_and_trend_lookup(self, trade_values_cog, test_db):
        async with test_db.execute(
            """
            INSERT INTO ktc_values (
                ktc_id, sleeper_id, player_name, position, team, is_rookie,
                value_1qb, rank_1qb, positional_rank_1qb,
                value_sf, rank_sf, positional_rank_sf, recorded_date
            ) VALUES (1004, '13524', "Ja'Marr Chase", 'WR', 'CIN', 0,
                      9999, 1, 1, 9999, 1, 1, '2026-07-15')
            """
        ):
            pass
        async with test_db.execute(
            """
            INSERT INTO ktc_values (
                ktc_id, sleeper_id, player_name, position, team, is_rookie,
                value_1qb, rank_1qb, positional_rank_1qb,
                value_sf, rank_sf, positional_rank_sf, recorded_date
            ) VALUES (1004, '13524', "Ja'Marr Chase", 'WR', 'CIN', 0,
                      9950, 1, 1, 9960, 1, 1, '2026-07-22')
            """
        ):
            pass

        row = await trade_values_cog._get_latest_value("chase")
        assert row is not None
        assert row["player_name"] == "Ja'Marr Chase"
        assert row["recorded_date"] == "2026-07-22"

        trend = await trade_values_cog._get_trend(1004, row["value_sf"], row["recorded_date"])
        assert trend == 9960 - 9999

    async def test_returns_none_for_unknown_player(self, trade_values_cog, test_db):
        row = await trade_values_cog._get_latest_value("totally unknown player xyz")
        assert row is None


async def _insert_snapshot(
    test_db, ktc_id, sleeper_id, name, value_sf, recorded_date
):
    async with test_db.execute(
        """
        INSERT INTO ktc_values (
            ktc_id, sleeper_id, player_name, position, team, is_rookie,
            value_1qb, rank_1qb, positional_rank_1qb,
            value_sf, rank_sf, positional_rank_sf, recorded_date
        ) VALUES (?, ?, ?, 'WR', 'CIN', 0, ?, 1, 1, ?, 1, 1, ?)
        """,
        (ktc_id, sleeper_id, name, value_sf, value_sf, recorded_date),
    ):
        pass


class TestTeamDynastyValues:
    async def test_sums_current_roster_value_ignoring_unmatched_players(self, test_db):
        await _insert_snapshot(test_db, 1, "p1", "Player One", 5000, "2026-07-22")
        await _insert_snapshot(test_db, 2, "p2", "Player Two", 3000, "2026-07-22")

        rosters = [{"roster_id": 1, "players": ["p1", "p2", "p_unmatched"]}]
        values = await get_team_dynasty_values(rosters)

        assert values[1] == 8000

    async def test_missing_roster_gets_zero(self, test_db):
        rosters = [{"roster_id": 99, "players": []}]
        values = await get_team_dynasty_values(rosters)

        assert values[99] == 0


class TestTeamDynastyValueTrends:
    async def test_isolates_market_movement_from_roster_churn(self, test_db):
        # Same player, currently owned both times: value rose from 4000 to 5000.
        await _insert_snapshot(test_db, 1, "p1", "Player One", 4000, "2026-07-15")
        await _insert_snapshot(test_db, 1, "p1", "Player One", 5000, "2026-07-22")

        rosters = [{"roster_id": 1, "players": ["p1"]}]
        trends = await get_team_dynasty_value_trends(rosters)

        current, prior = trends[1]
        assert current == 5000
        assert prior == 4000

    async def test_falls_back_to_current_value_when_no_history(self, test_db):
        await _insert_snapshot(test_db, 1, "p1", "Player One", 5000, "2026-07-22")

        rosters = [{"roster_id": 1, "players": ["p1"]}]
        trends = await get_team_dynasty_value_trends(rosters)

        current, prior = trends[1]
        assert current == prior == 5000

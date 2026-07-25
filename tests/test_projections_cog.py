"""Tests for prediction storage and grading.

The load-bearing property is immutability: a prediction is only meaningful
if it was written before the games and never touched afterwards. If a re-run
could overwrite it, the accuracy record would be self-congratulatory
nonsense.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import cogs.projections as projections_module
from cogs.projections import Projections
from database import Database
from lib.projections import MatchupPrediction
from lib.results import WeekResult, clear_cache


@pytest.fixture(autouse=True)
def _clear_results_cache():
    clear_cache()
    yield
    clear_cache()


@pytest_asyncio.fixture
async def test_db():
    database = Database(":memory:")
    await database.connect()
    with patch.object(projections_module, "db", database):
        yield database
    await database.close()


@pytest.fixture
def cog(mock_bot):
    instance = Projections(mock_bot)
    yield instance
    instance.upkeep_task.cancel()


def _prediction(roster_id=1, opponent_id=2, favorite=1, confidence=0.7, week=3):
    return MatchupPrediction(
        week=week,
        roster_id=roster_id,
        opponent_roster_id=opponent_id,
        favorite_roster_id=favorite,
        confidence=confidence,
        projected_points=120.0,
        opponent_projected_points=100.0,
    )


def _ctx(week=4, season=2026, status="in_season"):
    return {
        "league": {"season": str(season), "status": status,
                   "settings": {"leg": week, "playoff_teams": 6,
                                "playoff_week_start": 15}},
        "rosters": [{"roster_id": 1}, {"roster_id": 2}],
        "players": {},
        "owners": {1: "Corey", 2: "Fuzzy"},
        "season": season,
        "current_week": week,
        "playoff_teams": 6,
        "playoff_week_start": 15,
    }


class TestStorePredictions:
    async def test_stores_a_prediction(self, cog, test_db):
        written = await cog._store_predictions(2026, [_prediction()])

        assert written == 1
        stored = await cog._stored_predictions(2026, 3)
        assert len(stored) == 1
        assert stored[0]["predicted_winner_roster_id"] == 1
        assert stored[0]["confidence"] == 0.7
        assert stored[0]["correct"] is None

    async def test_normalizes_pair_order(self, cog, test_db):
        """Both sides of a matchup must collapse to one row, or the same game
        gets predicted (and graded) twice."""
        await cog._store_predictions(2026, [_prediction(roster_id=2, opponent_id=1)])

        stored = await cog._stored_predictions(2026, 3)
        assert (stored[0]["roster_id"], stored[0]["opponent_roster_id"]) == (1, 2)

    async def test_never_overwrites_an_existing_prediction(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction(favorite=1, confidence=0.7)])

        # A later run with a different opinion must not rewrite history.
        written = await cog._store_predictions(
            2026, [_prediction(favorite=2, confidence=0.95)]
        )

        assert written == 0
        stored = await cog._stored_predictions(2026, 3)
        assert len(stored) == 1
        assert stored[0]["predicted_winner_roster_id"] == 1
        assert stored[0]["confidence"] == 0.7

    async def test_separate_weeks_are_separate_rows(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction(week=3), _prediction(week=4)])

        assert len(await cog._stored_predictions(2026, 3)) == 1
        assert len(await cog._stored_predictions(2026, 4)) == 1


class TestResolveWeek:
    @staticmethod
    def _week_results(winner, loser):
        return [
            WeekResult(2026, 3, winner, 120.0, 120.0, loser, 100.0),
            WeekResult(2026, 3, loser, 100.0, 100.0, winner, 120.0),
        ]

    async def test_marks_a_correct_prediction(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction(favorite=1)])

        with patch.object(
            projections_module, "get_week_results",
            AsyncMock(return_value=self._week_results(winner=1, loser=2)),
        ):
            resolved = await cog._resolve_week(_ctx(), 3)

        assert resolved == 1
        stored = await cog._stored_predictions(2026, 3)
        assert stored[0]["correct"] == 1
        assert stored[0]["actual_winner_roster_id"] == 1
        assert stored[0]["resolved_at"] is not None

    async def test_marks_an_incorrect_prediction(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction(favorite=1)])

        with patch.object(
            projections_module, "get_week_results",
            AsyncMock(return_value=self._week_results(winner=2, loser=1)),
        ):
            await cog._resolve_week(_ctx(), 3)

        stored = await cog._stored_predictions(2026, 3)
        assert stored[0]["correct"] == 0
        assert stored[0]["actual_winner_roster_id"] == 2

    async def test_leaves_a_tie_unresolved(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction(favorite=1)])
        tied = [
            WeekResult(2026, 3, 1, 100.0, 100.0, 2, 100.0),
            WeekResult(2026, 3, 2, 100.0, 100.0, 1, 100.0),
        ]

        with patch.object(
            projections_module, "get_week_results", AsyncMock(return_value=tied)
        ):
            resolved = await cog._resolve_week(_ctx(), 3)

        assert resolved == 0
        assert (await cog._stored_predictions(2026, 3))[0]["correct"] is None

    async def test_does_not_regrade_a_resolved_prediction(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction(favorite=1)])

        with patch.object(
            projections_module, "get_week_results",
            AsyncMock(return_value=self._week_results(winner=1, loser=2)),
        ):
            await cog._resolve_week(_ctx(), 3)

        # A later week with a different (impossible) outcome must not flip it.
        with patch.object(
            projections_module, "get_week_results",
            AsyncMock(return_value=self._week_results(winner=2, loser=1)),
        ) as refetch:
            resolved = await cog._resolve_week(_ctx(), 3)

        assert resolved == 0
        refetch.assert_not_awaited()
        assert (await cog._stored_predictions(2026, 3))[0]["correct"] == 1

    async def test_unplayed_week_resolves_nothing(self, cog, test_db):
        await cog._store_predictions(2026, [_prediction()])
        unplayed = [
            WeekResult(2026, 3, 1, 0.0, 0.0, 2, 0.0),
            WeekResult(2026, 3, 2, 0.0, 0.0, 1, 0.0),
        ]

        with patch.object(
            projections_module, "get_week_results", AsyncMock(return_value=unplayed)
        ):
            assert await cog._resolve_week(_ctx(), 3) == 0


class TestUpkeepGuards:
    def test_only_runs_during_a_live_season(self, cog):
        assert cog._is_live_season({"status": "in_season"}) is True
        assert cog._is_live_season({"status": "complete"}) is False
        assert cog._is_live_season({"status": "pre_draft"}) is False
        assert cog._is_live_season({}) is False

    async def test_upkeep_is_a_noop_out_of_season(self, cog, test_db):
        cog._context = AsyncMock(return_value=_ctx(status="complete"))

        with patch.object(
            projections_module, "get_season_results", AsyncMock()
        ) as fetch:
            await cog.upkeep_task.coro(cog)

        fetch.assert_not_awaited()
        assert await cog._stored_predictions(2026, 4) == []

    async def test_upkeep_records_the_current_week(self, cog, test_db):
        cog._context = AsyncMock(return_value=_ctx(week=3))
        cog._strengths_for = AsyncMock(
            return_value={
                1: MagicMock(mean=120.0, stdev=20.0),
                2: MagicMock(mean=100.0, stdev=20.0),
            }
        )
        schedule_results = [
            WeekResult(2026, 3, 1, 0.0, 0.0, 2, 0.0),
            WeekResult(2026, 3, 2, 0.0, 0.0, 1, 0.0),
        ]

        with patch.object(
            projections_module, "get_season_results",
            AsyncMock(return_value=schedule_results),
        ), patch.object(
            projections_module, "predict_week",
            MagicMock(return_value=[_prediction(week=3)]),
        ), patch.object(
            projections_module, "get_week_results", AsyncMock(return_value=[])
        ):
            await cog.upkeep_task.coro(cog)

        assert len(await cog._stored_predictions(2026, 3)) == 1

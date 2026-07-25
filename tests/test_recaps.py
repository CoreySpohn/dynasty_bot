"""Tests for weekly awards, the shame wall, and the luck index."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import cogs.recaps as recaps_module
from cogs.recaps import Recaps
from database import Database
from lib.results import WeekResult, clear_cache, compute_luck


@pytest.fixture(autouse=True)
def _clear_results_cache():
    clear_cache()
    yield
    clear_cache()


@pytest_asyncio.fixture
async def test_db():
    """In-memory database patched in as cogs.recaps' `db` singleton."""
    database = Database(":memory:")
    await database.connect()
    with patch.object(recaps_module, "db", database):
        yield database
    await database.close()


@pytest.fixture
def recaps_cog(mock_bot):
    cog = Recaps(mock_bot)
    yield cog
    cog.autopost_task.cancel()


OWNERS = {1: "Corey", 2: "Fuzzy", 3: "Noah"}


def _result(roster_id, points, optimal, opponent_id=None, opponent_points=None,
            week=1, starter_points=None):
    return WeekResult(
        season=2026, week=week, roster_id=roster_id, points=points,
        optimal_points=optimal, opponent_roster_id=opponent_id,
        opponent_points=opponent_points,
        starters=tuple((starter_points or {}).keys()),
        starter_points=starter_points or {},
    )


class TestResolveWeek:
    """Awards default to the last *completed* week - mid-week partial scores
    would crown a highest scorer on Sunday afternoon."""

    def test_defaults_to_previous_week(self, recaps_cog):
        assert recaps_cog._resolve_week({"settings": {"leg": 5}}, None) == 4

    def test_explicit_week_wins(self, recaps_cog):
        assert recaps_cog._resolve_week({"settings": {"leg": 5}}, 2) == 2

    def test_never_returns_week_zero(self, recaps_cog):
        assert recaps_cog._resolve_week({"settings": {"leg": 1}}, None) == 1


class TestAwardFields:
    def test_highest_scorer(self, recaps_cog):
        results = [
            _result(1, 150.0, 150.0, 2, 100.0),
            _result(2, 100.0, 100.0, 1, 150.0),
        ]

        fields = dict(recaps_cog._award_fields(results, OWNERS, {1: 120.0, 2: 110.0}))

        assert "Corey" in fields["👑 Highest Scorer"]
        assert "150.0" in fields["👑 Highest Scorer"]

    def test_worst_beat_is_the_highest_scoring_loser(self, recaps_cog):
        results = [
            _result(1, 150.0, 150.0, 2, 140.0),   # W
            _result(2, 140.0, 140.0, 1, 150.0),   # L, high score
            _result(3, 80.0, 80.0, None, None),
        ]

        fields = dict(recaps_cog._award_fields(results, OWNERS, {}))

        assert "Fuzzy" in fields["💔 Worst Beat"]
        assert "140.0" in fields["💔 Worst Beat"]

    def test_best_bench_is_the_largest_optimal_gap(self, recaps_cog):
        results = [
            _result(1, 100.0, 145.0, 2, 90.0),
            _result(2, 90.0, 95.0, 1, 100.0),
        ]

        fields = dict(recaps_cog._award_fields(results, OWNERS, {}))

        assert "Corey" in fields["📈 Best Bench"]
        assert "45.0" in fields["📈 Best Bench"]

    def test_best_bench_omitted_when_everyone_was_optimal(self, recaps_cog):
        results = [
            _result(1, 100.0, 100.0, 2, 90.0),
            _result(2, 90.0, 90.0, 1, 100.0),
        ]

        assert "📈 Best Bench" not in dict(recaps_cog._award_fields(results, OWNERS, {}))

    def test_upset_uses_season_averages_as_the_expectation(self, recaps_cog):
        # Roster 2 wins despite averaging 30 less per week than roster 1.
        results = [
            _result(1, 90.0, 90.0, 2, 95.0),
            _result(2, 95.0, 95.0, 1, 90.0),
        ]

        fields = dict(
            recaps_cog._award_fields(results, OWNERS, {1: 130.0, 2: 100.0})
        )

        upset = fields["😱 Biggest Upset"]
        assert "Fuzzy" in upset and "Corey" in upset
        assert "30.0" in upset

    def test_no_upset_when_every_favorite_won(self, recaps_cog):
        results = [
            _result(1, 130.0, 130.0, 2, 90.0),
            _result(2, 90.0, 90.0, 1, 130.0),
        ]

        fields = dict(
            recaps_cog._award_fields(results, OWNERS, {1: 130.0, 2: 100.0})
        )

        assert "Chalk week" in fields["😱 Biggest Upset"]


class TestShameWallEmbed:
    PLAYERS = {"a": {"full_name": "Star Guy"}, "b": {"full_name": "Bye Guy"}}

    def test_flags_losses_the_best_lineup_would_have_won(self, recaps_cog):
        results = [_result(1, 95.0, 130.0, 2, 100.0)]

        embed = recaps_cog._build_shamewall_embed(3, results, OWNERS, self.PLAYERS)
        names = [f.name for f in embed.fields]

        assert "⚰️ Lost a game they'd already won" in names

    def test_does_not_flag_a_loss_the_best_lineup_still_loses(self, recaps_cog):
        results = [_result(1, 95.0, 99.0, 2, 100.0)]

        embed = recaps_cog._build_shamewall_embed(3, results, OWNERS, self.PLAYERS)
        names = [f.name for f in embed.fields]

        assert "⚰️ Lost a game they'd already won" not in names

    def test_lists_zero_point_starters_by_name(self, recaps_cog):
        results = [
            _result(1, 95.0, 95.0, 2, 100.0, starter_points={"a": 20.0, "b": 0.0})
        ]

        embed = recaps_cog._build_shamewall_embed(3, results, OWNERS, self.PLAYERS)
        zero_field = next(f for f in embed.fields if "scored nothing" in f.name)

        assert "Bye Guy" in zero_field.value
        assert "Star Guy" not in zero_field.value

    def test_caps_bench_leavers_at_three(self, recaps_cog):
        results = [_result(rid, 100.0, 100.0 + rid, 99, 90.0) for rid in range(1, 6)]

        embed = recaps_cog._build_shamewall_embed(3, results, OWNERS, self.PLAYERS)
        bench_field = next(f for f in embed.fields if "bench" in f.name)

        assert len(bench_field.value.splitlines()) == 3

    def test_clean_week_produces_no_fields(self, recaps_cog):
        results = [_result(1, 100.0, 100.0, 2, 90.0, starter_points={"a": 20.0})]

        embed = recaps_cog._build_shamewall_embed(3, results, OWNERS, self.PLAYERS)

        assert embed.fields == []


class TestComputeLuck:
    def test_all_play_expected_wins(self):
        # Roster 1 top-scores, roster 3 bottom-scores. With 3 teams, the top
        # scorer would beat both others: 2/2 == 1.0 expected wins.
        results = [
            _result(1, 150.0, 150.0, 2, 100.0),
            _result(2, 100.0, 100.0, 1, 150.0),
            _result(3, 80.0, 80.0, None, None),
        ]

        luck = compute_luck(results)

        assert luck[1].expected_wins == pytest.approx(1.0)
        assert luck[3].expected_wins == pytest.approx(0.0)

    def test_luck_score_is_actual_minus_expected(self):
        # Roster 2 loses despite outscoring roster 3: unlucky schedule.
        results = [
            _result(1, 150.0, 150.0, 2, 120.0),
            _result(2, 120.0, 120.0, 1, 150.0),
            _result(3, 90.0, 90.0, 4, 80.0),
            _result(4, 80.0, 80.0, 3, 90.0),
        ]

        luck = compute_luck(results)

        # Roster 3 won while only outscoring one of three others.
        assert luck[3].actual_wins == 1
        assert luck[3].expected_wins == pytest.approx(1 / 3)
        assert luck[3].luck_score == pytest.approx(1 - 1 / 3)
        # Roster 2 lost despite outscoring two of three.
        assert luck[2].luck_score < 0

    def test_tracks_close_game_record(self):
        results = [
            _result(1, 100.0, 100.0, 2, 95.0),    # close W
            _result(2, 95.0, 95.0, 1, 100.0),     # close L
            _result(1, 100.0, 100.0, 2, 50.0, week=2),   # blowout W
            _result(2, 50.0, 50.0, 1, 100.0, week=2),
        ]

        luck = compute_luck(results)

        assert (luck[1].close_wins, luck[1].close_losses) == (1, 0)
        assert (luck[2].close_wins, luck[2].close_losses) == (0, 1)

    def test_efficiency_is_actual_over_optimal(self):
        results = [
            _result(1, 90.0, 100.0, 2, 80.0),
            _result(2, 80.0, 80.0, 1, 90.0),
        ]

        luck = compute_luck(results)

        assert luck[1].efficiency == pytest.approx(0.9)
        assert luck[2].efficiency == pytest.approx(1.0)

    def test_ignores_unplayed_weeks(self):
        results = [
            _result(1, 0.0, 0.0, 2, 0.0),
            _result(2, 0.0, 0.0, 1, 0.0),
        ]

        assert compute_luck(results) == {}

    def test_tie_counts_as_half_a_win(self):
        results = [
            _result(1, 100.0, 100.0, 2, 100.0),
            _result(2, 100.0, 100.0, 1, 100.0),
        ]

        luck = compute_luck(results)

        assert luck[1].actual_wins == 0.5
        assert luck[1].expected_wins == pytest.approx(0.5)
        assert luck[1].luck_score == pytest.approx(0.0)


class TestPostedRecapIdempotency:
    """The one thing this cog persists. Without it the 6-hourly auto-post
    loop would re-post the same week's awards on every tick."""

    async def test_unposted_then_posted(self, recaps_cog, test_db):
        assert await recaps_cog._already_posted(2026, 3, "awards") is False

        await recaps_cog._mark_posted(2026, 3, "awards", "msg1")

        assert await recaps_cog._already_posted(2026, 3, "awards") is True

    async def test_marking_twice_is_harmless(self, recaps_cog, test_db):
        await recaps_cog._mark_posted(2026, 3, "awards", "msg1")
        await recaps_cog._mark_posted(2026, 3, "awards", "msg2")

        async with test_db.execute(
            "SELECT COUNT(*) FROM posted_recaps WHERE season=2026 AND week=3"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1

    async def test_types_and_weeks_tracked_separately(self, recaps_cog, test_db):
        await recaps_cog._mark_posted(2026, 3, "awards")

        assert await recaps_cog._already_posted(2026, 3, "shamewall") is False
        assert await recaps_cog._already_posted(2026, 4, "awards") is False


class TestAutopostGuards:
    def test_skips_before_any_week_is_complete(self, recaps_cog):
        assert recaps_cog._in_season({"settings": {"leg": 1}}) is False
        assert recaps_cog._in_season({"settings": {"leg": 2}}) is True
        assert recaps_cog._in_season({}) is False

    async def test_does_not_repost_an_already_posted_week(self, recaps_cog, test_db):
        channel = MagicMock()
        channel.send = AsyncMock(return_value=MagicMock(id=999))
        recaps_cog.bot.get_channel = MagicMock(return_value=channel)
        recaps_cog._league_context = AsyncMock(
            return_value=({"season": "2026", "settings": {"leg": 4}}, OWNERS, {})
        )
        await recaps_cog._mark_posted(2026, 3, "awards")
        await recaps_cog._mark_posted(2026, 3, "shamewall")

        with patch.object(
            recaps_module, "get_week_results",
            AsyncMock(return_value=[_result(1, 100.0, 100.0, 2, 90.0, week=3)]),
        ):
            await recaps_cog.autopost_task.coro(recaps_cog)

        channel.send.assert_not_awaited()

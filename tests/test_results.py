"""Tests for the weekly results derivation layer.

These results are computed from Sleeper rather than stored, so the contract
that matters is that reconstruction is faithful: correct opponent pairing,
correct handling of weeks that haven't been played, and an optimal-lineup
gap that means "what you left on the bench".
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from lib.results import (
    TeamRecord,
    WeekResult,
    build_records,
    build_week_results,
    by_roster,
    by_week,
    championship_week,
    clear_cache,
    playoff_rounds,
    get_history_results,
    get_league_chain,
    get_season_results,
    get_week_results,
    head_to_head,
    played,
)


@pytest.fixture(autouse=True)
def _clear_results_cache():
    clear_cache()
    yield
    clear_cache()


PLAYERS = {
    "qb": {"position": "QB"},
    "rb": {"position": "RB"},
    "wr": {"position": "WR"},
    "bench_rb": {"position": "RB"},
}
POSITIONS = ["QB", "RB", "WR", "BN"]


def _matchup(roster_id, points, matchup_id=1, starters=None, players_points=None):
    return {
        "roster_id": roster_id,
        "matchup_id": matchup_id,
        "points": points,
        "starters": starters if starters is not None else ["qb", "rb", "wr"],
        "players_points": players_points
        or {"qb": 20.0, "rb": 10.0, "wr": 5.0, "bench_rb": 30.0},
    }


class TestWeekResultProperties:
    def _result(self, **kwargs):
        base = dict(
            season=2026, week=1, roster_id=1, points=100.0, optimal_points=100.0,
            opponent_roster_id=2, opponent_points=90.0,
        )
        base.update(kwargs)
        return WeekResult(**base)

    def test_win_loss_tie(self):
        assert self._result(points=100.0, opponent_points=90.0).result == "W"
        assert self._result(points=80.0, opponent_points=90.0).result == "L"
        assert self._result(points=90.0, opponent_points=90.0).result == "T"

    def test_unplayed_week_has_no_result(self):
        # Sleeper reports future weeks as 0-0, which is not a tie.
        unplayed = self._result(points=0.0, opponent_points=0.0)

        assert unplayed.played is False
        assert unplayed.result is None

    def test_bye_week_has_no_opponent_or_result(self):
        bye = self._result(opponent_roster_id=None, opponent_points=None)

        assert bye.result is None
        assert bye.margin is None

    def test_margin_and_closeness(self):
        assert self._result(points=100.0, opponent_points=90.0).margin == 10.0
        assert self._result(points=100.0, opponent_points=90.0).was_close is True
        assert self._result(points=100.0, opponent_points=80.0).was_close is False

    def test_points_left_on_bench_is_the_optimal_gap(self):
        assert self._result(points=100.0, optimal_points=140.0).points_left_on_bench == 40.0

    def test_points_left_on_bench_never_negative(self):
        # Optimal can't be below actual, but don't emit noise if it happens.
        assert self._result(points=100.0, optimal_points=95.0).points_left_on_bench == 0.0

    def test_would_have_won_flags_self_inflicted_losses(self):
        # Lost 95-100, but the best lineup would have scored 120.
        assert self._result(
            points=95.0, opponent_points=100.0, optimal_points=120.0
        ).would_have_won is True

    def test_would_have_won_false_when_optimal_still_loses(self):
        assert self._result(
            points=95.0, opponent_points=100.0, optimal_points=99.0
        ).would_have_won is False

    def test_would_have_won_false_when_they_actually_won(self):
        assert self._result(
            points=100.0, opponent_points=90.0, optimal_points=150.0
        ).would_have_won is False


class TestBuildWeekResults:
    def test_pairs_opponents_by_matchup_id(self):
        results = build_week_results(
            [_matchup(1, 120.0), _matchup(2, 100.0)], PLAYERS, POSITIONS, 2026, 3
        )

        by_id = {r.roster_id: r for r in results}
        assert by_id[1].opponent_roster_id == 2
        assert by_id[1].opponent_points == 100.0
        assert by_id[1].result == "W"
        assert by_id[2].result == "L"

    def test_computes_optimal_lineup_from_all_rostered_players(self):
        # Started qb/rb/wr for 35, but bench_rb scored 30 - optimal swaps it
        # in for the 10-point rb: 20 + 30 + 5 = 55.
        results = build_week_results([_matchup(1, 35.0)], PLAYERS, POSITIONS, 2026, 1)

        assert results[0].optimal_points == 55.0
        assert results[0].points_left_on_bench == 20.0

    def test_unpaired_entry_has_no_opponent(self):
        results = build_week_results([_matchup(1, 120.0)], PLAYERS, POSITIONS, 2026, 1)

        assert results[0].opponent_roster_id is None
        assert results[0].result is None

    def test_null_matchup_id_treated_as_no_opponent(self):
        results = build_week_results(
            [_matchup(1, 120.0, matchup_id=None), _matchup(2, 100.0, matchup_id=None)],
            PLAYERS, POSITIONS, 2026, 1,
        )

        assert all(r.opponent_roster_id is None for r in results)

    def test_filters_sleepers_empty_starter_placeholder(self):
        results = build_week_results(
            [_matchup(1, 30.0, starters=["qb", "0", "wr"])],
            PLAYERS, POSITIONS, 2026, 1,
        )

        assert results[0].starters == ("qb", "wr")

    def test_records_starter_points(self):
        results = build_week_results([_matchup(1, 35.0)], PLAYERS, POSITIONS, 2026, 1)

        assert results[0].starter_points == {"qb": 20.0, "rb": 10.0, "wr": 5.0}

    def test_handles_empty_matchups(self):
        assert build_week_results([], PLAYERS, POSITIONS, 2026, 1) == []

    def test_skips_entries_without_a_roster_id(self):
        assert build_week_results([{"points": 10}], PLAYERS, POSITIONS, 2026, 1) == []


class TestAggregations:
    def _results(self):
        return [
            WeekResult(2026, 1, 1, 100.0, 120.0, 2, 90.0),   # W
            WeekResult(2026, 2, 1, 80.0, 130.0, 2, 95.0),    # L
            WeekResult(2026, 3, 1, 90.0, 90.0, 2, 90.0),     # T
            WeekResult(2026, 4, 1, 0.0, 0.0, 2, 0.0),        # unplayed
        ]

    def test_build_records_counts_outcomes_and_totals(self):
        record = build_records(self._results())[1]

        assert (record.wins, record.losses, record.ties) == (1, 1, 1)
        assert record.games == 3            # unplayed week excluded
        assert record.points_for == 270.0   # unplayed adds 0
        assert record.points_against == 275.0
        assert record.optimal_points == 340.0
        assert record.points_left_on_bench == 70.0

    def test_win_pct_counts_a_tie_as_half(self):
        record = build_records(self._results())[1]

        assert record.win_pct == pytest.approx((1 + 0.5) / 3 * 100)

    def test_record_text_omits_ties_when_there_are_none(self):
        assert TeamRecord(1, wins=3, losses=1).record_text == "3-1"
        assert TeamRecord(1, wins=3, losses=1, ties=1).record_text == "3-1-1"

    def test_empty_record_has_zero_win_pct(self):
        assert TeamRecord(1).win_pct == 0.0

    def test_played_filters_unplayed_weeks(self):
        assert len(played(self._results())) == 3

    def test_grouping_helpers(self):
        results = self._results()

        assert set(by_roster(results)) == {1}
        assert set(by_week(results)) == {1, 2, 3, 4}


class TestHeadToHead:
    def test_accumulates_both_directions(self):
        results = [
            WeekResult(2026, 1, 1, 100.0, 100.0, 2, 90.0),
            WeekResult(2026, 1, 2, 90.0, 90.0, 1, 100.0),
            WeekResult(2025, 5, 1, 80.0, 80.0, 2, 95.0),
            WeekResult(2025, 5, 2, 95.0, 95.0, 1, 80.0),
        ]

        pairs = head_to_head(results)

        assert (pairs[(1, 2)].wins, pairs[(1, 2)].losses) == (1, 1)
        assert (pairs[(2, 1)].wins, pairs[(2, 1)].losses) == (1, 1)

    def test_ignores_byes_and_unplayed(self):
        results = [
            WeekResult(2026, 1, 1, 100.0, 100.0, None, None),
            WeekResult(2026, 2, 1, 0.0, 0.0, 2, 0.0),
        ]

        assert head_to_head(results) == {}


def _sleeper(league, matchups_by_week, extra_leagues=None):
    """Mock Sleeper client. extra_leagues maps league_id -> league dict."""
    leagues = {league["league_id"]: league, **(extra_leagues or {})}
    client = MagicMock()
    client.get_league = AsyncMock(side_effect=lambda lid: leagues[lid])
    client.get_matchups = AsyncMock(
        side_effect=lambda lid, week: matchups_by_week.get((lid, week), [])
    )
    return client


LEAGUE = {
    "league_id": "L2026",
    "season": "2026",
    "settings": {"leg": 3},
    "roster_positions": POSITIONS,
    "previous_league_id": None,
}


class TestFetchAndCache:
    async def test_fetches_and_builds_a_week(self):
        sleeper = _sleeper(
            LEAGUE, {("L2026", 1): [_matchup(1, 120.0), _matchup(2, 100.0)]}
        )

        results = await get_week_results(
            sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE
        )

        assert {r.roster_id for r in results} == {1, 2}
        assert results[0].season == 2026

    async def test_completed_week_is_cached(self):
        sleeper = _sleeper(LEAGUE, {("L2026", 1): [_matchup(1, 120.0)]})

        await get_week_results(sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE)
        await get_week_results(sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE)

        # Week 1 < current week 3, so the second call is served from cache.
        sleeper.get_matchups.assert_awaited_once()

    async def test_current_week_is_never_cached(self):
        sleeper = _sleeper(LEAGUE, {("L2026", 3): [_matchup(1, 120.0)]})

        await get_week_results(sleeper, "L2026", 3, players=PLAYERS, league=LEAGUE)
        await get_week_results(sleeper, "L2026", 3, players=PLAYERS, league=LEAGUE)

        assert sleeper.get_matchups.await_count == 2

    async def test_use_cache_false_forces_refetch(self):
        sleeper = _sleeper(LEAGUE, {("L2026", 1): [_matchup(1, 120.0)]})

        await get_week_results(sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE)
        await get_week_results(
            sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE, use_cache=False
        )

        assert sleeper.get_matchups.await_count == 2

    async def test_season_results_stops_at_current_week(self):
        sleeper = _sleeper(
            LEAGUE,
            {("L2026", w): [_matchup(1, 100.0 + w)] for w in range(1, 6)},
        )

        results = await get_season_results(
            sleeper, "L2026", players=PLAYERS, league=LEAGUE
        )

        assert sorted(r.week for r in results) == [1, 2, 3]


class TestLeagueChain:
    def _chained(self):
        current = {**LEAGUE, "previous_league_id": "L2025"}
        prior = {
            "league_id": "L2025",
            "season": "2025",
            "settings": {"leg": 2},
            "roster_positions": POSITIONS,
            "previous_league_id": None,
        }
        return current, prior

    async def test_follows_previous_league_id_backwards(self):
        current, prior = self._chained()
        sleeper = _sleeper(current, {}, extra_leagues={"L2025": prior})

        chain = await get_league_chain(sleeper, "L2026")

        assert [c["season"] for c in chain] == ["2026", "2025"]

    async def test_stops_on_a_cycle(self):
        a = {**LEAGUE, "league_id": "A", "previous_league_id": "B"}
        b = {**LEAGUE, "league_id": "B", "previous_league_id": "A"}
        sleeper = _sleeper(a, {}, extra_leagues={"B": b})

        chain = await get_league_chain(sleeper, "A")

        assert [c["league_id"] for c in chain] == ["A", "B"]

    async def test_stops_when_a_prior_league_is_unreachable(self):
        current, _ = self._chained()
        sleeper = _sleeper(current, {})
        sleeper.get_league = AsyncMock(
            side_effect=lambda lid: current if lid == "L2026" else (_ for _ in ()).throw(
                RuntimeError("404")
            )
        )

        chain = await get_league_chain(sleeper, "L2026")

        assert [c["league_id"] for c in chain] == ["L2026"]

    async def test_history_spans_every_chained_season(self):
        current, prior = self._chained()
        sleeper = _sleeper(
            current,
            {
                ("L2026", 1): [_matchup(1, 110.0)],
                ("L2026", 2): [_matchup(1, 120.0)],
                ("L2026", 3): [_matchup(1, 130.0)],
                ("L2025", 1): [_matchup(1, 90.0)],
                ("L2025", 2): [_matchup(1, 95.0)],
            },
            extra_leagues={"L2025": prior},
        )

        results = await get_history_results(sleeper, "L2026", players=PLAYERS)

        assert sorted({r.season for r in results}) == [2025, 2026]
        assert len(results) == 5


# =========================================================================
# Cross-season attribution
# =========================================================================

from lib.results import (  # noqa: E402
    champion_from_bracket,
    get_champions,
    head_to_head_owners,
)


class TestOwnerAttribution:
    """Roster ids are only stable within one season's league, so anything
    spanning years has to be attributed by owner."""

    def test_build_week_results_attaches_owner_ids(self):
        results = build_week_results(
            [_matchup(1, 120.0), _matchup(2, 100.0)],
            PLAYERS, POSITIONS, 2026, 1,
            owner_by_roster={1: "userA", 2: "userB"},
        )

        by_id = {r.roster_id: r for r in results}
        assert by_id[1].owner_id == "userA"
        assert by_id[1].opponent_owner_id == "userB"
        assert by_id[2].owner_id == "userB"
        assert by_id[2].opponent_owner_id == "userA"

    def test_owner_ids_are_none_without_a_map(self):
        results = build_week_results(
            [_matchup(1, 120.0), _matchup(2, 100.0)], PLAYERS, POSITIONS, 2026, 1
        )

        assert all(r.owner_id is None for r in results)

    async def test_cache_does_not_mix_attributed_and_plain_results(self):
        sleeper = _sleeper(LEAGUE, {("L2026", 1): [_matchup(1, 120.0)]})

        plain = await get_week_results(
            sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE
        )
        attributed = await get_week_results(
            sleeper, "L2026", 1, players=PLAYERS, league=LEAGUE,
            owner_by_roster={1: "userA"},
        )

        assert plain[0].owner_id is None
        assert attributed[0].owner_id == "userA"


class TestHeadToHeadOwners:
    def test_aggregates_the_same_owner_across_different_roster_ids(self):
        # userA sat on roster 1 in 2025 and roster 4 in 2026; both wins
        # belong to the same person.
        results = [
            WeekResult(2025, 1, 1, 100.0, 100.0, 2, 90.0,
                       owner_id="userA", opponent_owner_id="userB"),
            WeekResult(2026, 1, 4, 110.0, 110.0, 7, 95.0,
                       owner_id="userA", opponent_owner_id="userB"),
        ]

        pairs = head_to_head_owners(results)

        assert pairs[("userA", "userB")].wins == 2
        assert pairs[("userA", "userB")].losses == 0

    def test_skips_results_without_owner_attribution(self):
        results = [WeekResult(2026, 1, 1, 100.0, 100.0, 2, 90.0)]

        assert head_to_head_owners(results) == {}

    def test_skips_unplayed_weeks(self):
        results = [
            WeekResult(2026, 1, 1, 0.0, 0.0, 2, 0.0,
                       owner_id="userA", opponent_owner_id="userB"),
        ]

        assert head_to_head_owners(results) == {}


class TestChampionFromBracket:
    def test_final_round_winner_is_the_champion(self):
        bracket = [
            {"r": 1, "m": 1, "t1": 1, "t2": 4, "w": 1, "l": 4},
            {"r": 1, "m": 2, "t1": 2, "t2": 3, "w": 2, "l": 3},
            {"r": 2, "m": 3, "t1": 1, "t2": 2, "w": 2, "l": 1},
        ]

        assert champion_from_bracket(bracket) == 2

    def test_unfinished_bracket_has_no_champion(self):
        bracket = [{"r": 1, "m": 1, "t1": 1, "t2": 4, "w": None, "l": None}]

        assert champion_from_bracket(bracket) is None

    def test_empty_bracket(self):
        assert champion_from_bracket([]) is None
        assert champion_from_bracket(None) is None

    def test_ignores_unplayed_later_rounds(self):
        # Round 1 done, final not yet played - no champion, not a round-1 winner.
        bracket = [
            {"r": 1, "m": 1, "w": 1, "l": 4},
            {"r": 2, "m": 3, "t1": {"w": 1}, "t2": {"w": 2}, "w": None},
        ]

        assert champion_from_bracket(bracket) == 1


class TestGetChampions:
    async def test_maps_winning_roster_to_its_owner_per_season(self):
        current = {**LEAGUE, "previous_league_id": "L2025"}
        prior = {
            "league_id": "L2025", "season": "2025",
            "settings": {"leg": 17}, "roster_positions": POSITIONS,
            "previous_league_id": None,
        }
        sleeper = _sleeper(current, {}, extra_leagues={"L2025": prior})
        sleeper.get_winners_bracket = AsyncMock(
            side_effect=lambda lid: {
                "L2026": [{"r": 1, "m": 1, "w": 3, "l": 5}],
                "L2025": [{"r": 1, "m": 1, "w": 1, "l": 2}],
            }[lid]
        )
        sleeper.get_rosters = AsyncMock(
            side_effect=lambda lid: {
                "L2026": [{"roster_id": 3, "owner_id": "userC"}],
                "L2025": [{"roster_id": 1, "owner_id": "userA"}],
            }[lid]
        )

        champions = await get_champions(sleeper, "L2026")

        assert champions == [(2026, "userC"), (2025, "userA")]

    async def test_skips_seasons_with_no_bracket(self):
        sleeper = _sleeper(LEAGUE, {})
        sleeper.get_winners_bracket = AsyncMock(side_effect=RuntimeError("404"))
        sleeper.get_rosters = AsyncMock(return_value=[])

        assert await get_champions(sleeper, "L2026") == []


class TestChampionshipWeek:
    """Potential points count through championship weekend, per league rules.

    The number is derived from the league's own playoff settings rather than
    hardcoded, because Sleeper also scores NFL week 18 - a week played after
    the league's season has ended.
    """

    @pytest.mark.parametrize(
        "teams,expected_rounds",
        [(2, 1), (4, 2), (6, 3), (8, 3), (12, 4)],
    )
    def test_rounds_needed_to_crown_a_champion(self, teams, expected_rounds):
        assert playoff_rounds(teams) == expected_rounds

    def test_six_teams_from_week_fifteen_ends_week_seventeen(self):
        """The league's actual setup: 6 playoff teams starting week 15."""
        league = {"settings": {"playoff_week_start": 15, "playoff_teams": 6}}

        assert championship_week(league) == 17

    def test_excludes_nfl_week_eighteen(self):
        """Sleeper scores week 18, but the league's season is over by then."""
        league = {"settings": {"playoff_week_start": 15, "playoff_teams": 6}}

        assert championship_week(league) < 18

    def test_follows_the_league_settings(self):
        """A shorter bracket or a longer regular season moves the deadline."""
        four_teams = {"settings": {"playoff_week_start": 15, "playoff_teams": 4}}
        later_start = {"settings": {"playoff_week_start": 16, "playoff_teams": 6}}

        assert championship_week(four_teams) == 16
        assert championship_week(later_start) == 18

    def test_falls_back_when_settings_are_missing(self):
        """An incomplete payload shouldn't silently truncate the season."""
        assert championship_week({}) == 17
        assert championship_week({"settings": {}}) == 17
        assert championship_week({"settings": {"playoff_teams": None}}) == 17

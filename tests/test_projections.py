"""Tests for the projection model.

The model makes claims that can be checked rather than eyeballed: a stated
confidence should mean what it says, shrinkage should actually shrink, and
"clinched" should only appear when it is genuinely impossible to miss.
"""

import pytest

from lib.projections import (
    DYNASTY_TILT,
    PRIOR_GAMES,
    SimulationInput,
    TeamStrength,
    build_strengths,
    predict_week,
    simulate_finishes,
    split_schedule,
    week_schedule,
    win_probability,
)
from lib.results import WeekResult


def _result(roster_id, points, week=1, opponent_id=2, opponent_points=100.0):
    return WeekResult(
        season=2026, week=week, roster_id=roster_id, points=points,
        optimal_points=points, opponent_roster_id=opponent_id,
        opponent_points=opponent_points,
    )


class TestBuildStrengths:
    def test_projects_every_roster_even_with_no_games(self):
        strengths = build_strengths([], roster_ids=[1, 2, 3])

        assert set(strengths) == {1, 2, 3}
        assert all(s.games_played == 0 for s in strengths.values())

    def test_shrinks_toward_league_mean_early(self):
        # One 200-point week against a league averaging ~100 must not project
        # a 200-point team.
        results = [
            _result(1, 200.0, week=1),
            _result(2, 100.0, week=1, opponent_id=1, opponent_points=200.0),
            _result(3, 100.0, week=1, opponent_id=4, opponent_points=100.0),
            _result(4, 100.0, week=1, opponent_id=3, opponent_points=100.0),
        ]

        strengths = build_strengths(results, [1, 2, 3, 4])

        assert strengths[1].raw_mean == 200.0
        assert strengths[1].mean < 150.0
        assert strengths[1].mean > 100.0
        assert strengths[1].is_projected is True

    def test_shrinkage_weakens_as_games_accumulate(self):
        def spread_after(weeks):
            results = []
            for week in range(1, weeks + 1):
                results.append(_result(1, 140.0, week=week))
                results.append(
                    _result(2, 100.0, week=week, opponent_id=1, opponent_points=140.0)
                )
            strengths = build_strengths(results, [1, 2])
            return strengths[1].mean - strengths[2].mean

        # More evidence for the same gap should widen the projected gap.
        assert spread_after(8) > spread_after(2)

    def test_prior_games_constant_governs_the_blend(self):
        # With exactly PRIOR_GAMES games, the estimate sits halfway between
        # observed and prior.
        games = int(PRIOR_GAMES)
        results = []
        for week in range(1, games + 1):
            results.append(_result(1, 140.0, week=week))
            results.append(
                _result(2, 100.0, week=week, opponent_id=1, opponent_points=140.0)
            )

        strengths = build_strengths(results, [1, 2])
        observed, league_mean = 140.0, 120.0  # league mean of 140 and 100

        assert strengths[1].mean == pytest.approx((observed + league_mean) / 2)

    def test_dynasty_value_tilts_the_prior_with_no_games_played(self):
        strengths = build_strengths(
            [], roster_ids=[1, 2], dynasty_values={1: 100_000, 2: 50_000}
        )

        assert strengths[1].mean > strengths[2].mean
        # Best and worst roster sit at +/-DYNASTY_TILT of the league mean.
        ratio = strengths[1].mean / strengths[2].mean
        assert ratio == pytest.approx(
            (1 + DYNASTY_TILT) / (1 - DYNASTY_TILT), rel=1e-6
        )

    def test_equal_dynasty_values_produce_equal_priors(self):
        strengths = build_strengths(
            [], roster_ids=[1, 2], dynasty_values={1: 50_000, 2: 50_000}
        )

        assert strengths[1].mean == strengths[2].mean

    def test_unranked_roster_falls_back_to_league_mean(self):
        strengths = build_strengths(
            [], roster_ids=[1, 2, 3], dynasty_values={1: 100_000, 2: 50_000}
        )

        assert strengths[3].mean == pytest.approx(
            (strengths[1].mean + strengths[2].mean) / 2, rel=0.02
        )

    def test_stdev_is_never_zero(self):
        # A team scoring identically every week would otherwise imply
        # certainty, making win probabilities 0 or 1.
        results = [_result(1, 100.0, week=w) for w in range(1, 6)]

        assert build_strengths(results, [1])[1].stdev >= 1.0


class TestWinProbability:
    def test_equal_teams_are_a_coin_flip(self):
        a = TeamStrength(1, mean=100.0, stdev=25.0, games_played=5)
        b = TeamStrength(2, mean=100.0, stdev=25.0, games_played=5)

        assert win_probability(a, b) == pytest.approx(0.5)

    def test_better_team_is_favored(self):
        a = TeamStrength(1, mean=130.0, stdev=25.0, games_played=5)
        b = TeamStrength(2, mean=100.0, stdev=25.0, games_played=5)

        assert 0.5 < win_probability(a, b) < 1.0
        assert win_probability(a, b) + win_probability(b, a) == pytest.approx(1.0)

    def test_higher_variance_pulls_toward_a_coin_flip(self):
        favorite = TeamStrength(1, mean=130.0, stdev=10.0, games_played=5)
        underdog = TeamStrength(2, mean=100.0, stdev=10.0, games_played=5)
        wild = TeamStrength(2, mean=100.0, stdev=60.0, games_played=5)

        assert win_probability(favorite, wild) < win_probability(favorite, underdog)

    def test_confidence_is_empirically_calibrated(self):
        """A stated 70% should win about 70% of simulated games.

        This is the claim the /predictionrecord calibration section makes, so
        it's worth pinning rather than trusting the algebra.
        """
        import random

        a = TeamStrength(1, mean=118.0, stdev=25.0, games_played=10)
        b = TeamStrength(2, mean=100.0, stdev=25.0, games_played=10)
        stated = win_probability(a, b)

        rng = random.Random(11)
        trials = 40_000
        wins = sum(
            1
            for _ in range(trials)
            if rng.gauss(a.mean, a.stdev) > rng.gauss(b.mean, b.stdev)
        )

        assert wins / trials == pytest.approx(stated, abs=0.01)


class TestPredictWeek:
    def _strengths(self):
        return {
            1: TeamStrength(1, mean=130.0, stdev=20.0, games_played=6),
            2: TeamStrength(2, mean=100.0, stdev=20.0, games_played=6),
            3: TeamStrength(3, mean=110.0, stdev=20.0, games_played=6),
            4: TeamStrength(4, mean=110.0, stdev=20.0, games_played=6),
        }

    def test_picks_the_stronger_team(self):
        predictions = predict_week([(1, 2)], self._strengths(), week=3)

        assert predictions[0].favorite_roster_id == 1
        assert predictions[0].underdog_roster_id == 2
        assert predictions[0].confidence > 0.5

    def test_confidence_is_always_at_least_half(self):
        # Whichever way round the pair is given, confidence describes the
        # favorite, so it can never be below 50%.
        for pair in [(1, 2), (2, 1)]:
            prediction = predict_week([pair], self._strengths(), week=3)[0]
            assert prediction.confidence >= 0.5
            assert prediction.favorite_roster_id == 1

    def test_even_matchup_is_flagged_as_a_cointoss(self):
        prediction = predict_week([(3, 4)], self._strengths(), week=3)[0]

        assert prediction.is_cointoss is True

    def test_lopsided_matchup_is_not_a_cointoss(self):
        prediction = predict_week([(1, 2)], self._strengths(), week=3)[0]

        assert prediction.is_cointoss is False

    def test_skips_matchups_with_an_unknown_roster(self):
        assert predict_week([(1, 99)], self._strengths(), week=3) == []


class TestWeekSchedule:
    def test_dedupes_both_sides_of_a_matchup(self):
        results = [
            WeekResult(2026, 1, 1, 100.0, 100.0, 2, 90.0),
            WeekResult(2026, 1, 2, 90.0, 90.0, 1, 100.0),
        ]

        assert week_schedule(results) == [(1, 2)]

    def test_orders_pairs_consistently(self):
        results = [WeekResult(2026, 1, 5, 100.0, 100.0, 2, 90.0)]

        assert week_schedule(results) == [(2, 5)]

    def test_ignores_byes(self):
        results = [WeekResult(2026, 1, 1, 100.0, 100.0, None, None)]

        assert week_schedule(results) == []


class TestSplitSchedule:
    def test_separates_played_weeks_from_upcoming_ones(self):
        results = [
            WeekResult(2026, 1, 1, 100.0, 100.0, 2, 90.0),
            WeekResult(2026, 1, 2, 90.0, 90.0, 1, 100.0),
            # Week 2 published but unplayed: Sleeper reports 0-0.
            WeekResult(2026, 2, 1, 0.0, 0.0, 2, 0.0),
            WeekResult(2026, 2, 2, 0.0, 0.0, 1, 0.0),
        ]

        completed, remaining = split_schedule(results, through_week=14)

        assert {r.week for r in completed} == {1}
        assert remaining == {2: [(1, 2)]}

    def test_excludes_weeks_past_the_regular_season(self):
        results = [WeekResult(2026, 15, 1, 0.0, 0.0, 2, 0.0)]

        _, remaining = split_schedule(results, through_week=14)

        assert remaining == {}


class TestSimulateFinishes:
    def _inputs(self, remaining=None, completed=None, playoff_teams=2):
        strengths = {
            1: TeamStrength(1, mean=130.0, stdev=15.0, games_played=6),
            2: TeamStrength(2, mean=120.0, stdev=15.0, games_played=6),
            3: TeamStrength(3, mean=100.0, stdev=15.0, games_played=6),
            4: TeamStrength(4, mean=90.0, stdev=15.0, games_played=6),
        }
        return SimulationInput(
            strengths=strengths,
            completed=completed or [],
            remaining=remaining or {},
            playoff_teams=playoff_teams,
        )

    def test_odds_sum_to_the_number_of_playoff_spots(self):
        inputs = self._inputs(remaining={1: [(1, 2), (3, 4)]})

        odds = simulate_finishes(inputs, simulations=2000, seed=1)

        assert sum(o.playoff_odds for o in odds.values()) == pytest.approx(
            inputs.playoff_teams, abs=0.01
        )

    def test_exactly_one_team_finishes_last(self):
        odds = simulate_finishes(
            self._inputs(remaining={1: [(1, 2), (3, 4)]}),
            simulations=2000,
            seed=1,
        )

        assert sum(o.last_place_odds for o in odds.values()) == pytest.approx(
            1.0, abs=0.01
        )

    def test_stronger_teams_get_better_odds(self):
        odds = simulate_finishes(
            self._inputs(remaining={w: [(1, 2), (3, 4)] for w in (1, 2, 3)}),
            simulations=2000,
            seed=3,
        )

        assert odds[1].playoff_odds > odds[4].playoff_odds
        assert odds[4].last_place_odds > odds[1].last_place_odds

    def test_is_reproducible_with_a_seed(self):
        inputs = self._inputs(remaining={1: [(1, 2), (3, 4)]})

        first = simulate_finishes(inputs, simulations=500, seed=7)
        second = simulate_finishes(inputs, simulations=500, seed=7)

        assert {k: v.playoff_odds for k, v in first.items()} == {
            k: v.playoff_odds for k, v in second.items()
        }

    def test_no_games_left_means_standings_are_final(self):
        completed = [
            WeekResult(2026, 1, 1, 130.0, 130.0, 2, 120.0),
            WeekResult(2026, 1, 2, 120.0, 120.0, 1, 130.0),
            WeekResult(2026, 1, 3, 100.0, 100.0, 4, 90.0),
            WeekResult(2026, 1, 4, 90.0, 90.0, 3, 100.0),
        ]

        odds = simulate_finishes(
            self._inputs(completed=completed), simulations=200, seed=5
        )

        # Rosters 1 and 3 won; with 2 spots they're in, and 2/4 are out.
        assert odds[1].playoff_odds == 1.0
        assert odds[1].clinched is True
        assert odds[4].playoff_odds == 0.0
        assert odds[4].eliminated is True


class TestCertaintyFlags:
    """`clinched` must mean impossible-to-miss, not merely unobserved in
    10,000 simulations. Enumeration proves it; Monte Carlo can't."""

    def _strengths(self, n):
        return {
            i: TeamStrength(i, mean=100.0, stdev=15.0, games_played=6)
            for i in range(1, n + 1)
        }

    def test_flags_are_withheld_when_too_many_games_remain(self):
        # 2 teams x 20 weeks = 20 undecided games, past MAX_ENUMERABLE_GAMES.
        inputs = SimulationInput(
            strengths=self._strengths(2),
            remaining={week: [(1, 2)] for week in range(1, 21)},
            playoff_teams=1,
        )

        odds = simulate_finishes(inputs, simulations=200, seed=1)

        assert all(not o.clinched and not o.eliminated for o in odds.values())

    def test_sacko_clinched_only_when_unavoidable(self):
        completed = [
            WeekResult(2026, 1, 1, 130.0, 130.0, 2, 90.0),
            WeekResult(2026, 1, 2, 90.0, 90.0, 1, 130.0),
        ]
        inputs = SimulationInput(
            strengths=self._strengths(2),
            completed=completed,
            remaining={},
            playoff_teams=1,
        )

        odds = simulate_finishes(inputs, simulations=100, seed=1)

        assert odds[2].sacko_clinched is True
        assert odds[1].sacko_clinched is False

    def test_a_live_race_clinches_nobody(self):
        inputs = SimulationInput(
            strengths=self._strengths(4),
            remaining={1: [(1, 2), (3, 4)]},
            playoff_teams=2,
        )

        odds = simulate_finishes(inputs, simulations=500, seed=2)

        assert not any(o.clinched for o in odds.values())

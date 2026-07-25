"""Scoring projections: win probabilities, playoff odds, and last-place odds.

One model, three consumers. `/predict` needs a per-matchup win probability,
`/playoffodds` needs season-end finishing odds, and `/sacko` needs the same
simulation read from the bottom. Deriving them separately would let them
contradict each other, which is worse than any of them being slightly off.

The model is deliberately simple and explainable, because it has to be
defended in a group chat:

1. Each team's weekly score is treated as normal, with a mean and standard
   deviation taken from the weeks they've actually played.
2. Early in the season those estimates are noise, so both are **shrunk
   toward the league average** with a weight that decays as real games
   accumulate. Dynasty roster value supplies the prior mean, which is the
   only real signal available in week 1.
3. A matchup is the difference of two normals, so
   P(A beats B) = Phi((mu_a - mu_b) / sqrt(sigma_a^2 + sigma_b^2)).
4. Season odds come from simulating the remaining schedule many times and
   counting how often each team lands in a playoff spot or dead last.

Everything is derived from `lib/results.py`, so it agrees with /rankings and
/luckindex by construction rather than by coincidence.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from statistics import NormalDist, fmean, stdev
from typing import Any, Iterable, Optional

from lib.results import WeekResult, build_records, by_week, played

logger = logging.getLogger("dynasty_bot.projections")

# Weight of the prior, in "pseudo-games". At 4, a team with 4 games played
# is half its own scoring and half the league prior. Low enough to react to
# a real hot start, high enough that one 180-point week doesn't make someone
# a juggernaut.
PRIOR_GAMES = 4.0

# Fallback weekly spread when the league has almost no history. Typical
# fantasy weekly standard deviation sits around 25 points.
DEFAULT_STDEV = 25.0

# How far dynasty roster value is allowed to move a team's prior mean, as a
# fraction of the league average. +/-12% keeps the best roster in the league
# a clear favourite over the worst without overwhelming actual results.
DYNASTY_TILT = 0.12

# Simulation count. 10k puts the standard error on a 50% odds estimate at
# about 0.5 points, which is finer than anyone reads off an embed.
DEFAULT_SIMULATIONS = 10_000

# Above this many undecided games, exhaustive clinch/elimination checking
# (2^n) stops being worth it and only Monte Carlo odds are reported.
MAX_ENUMERABLE_GAMES = 14


@dataclass
class TeamStrength:
    """A team's projected weekly scoring distribution."""

    roster_id: int
    mean: float
    stdev: float
    games_played: int
    raw_mean: Optional[float] = None

    @property
    def is_projected(self) -> bool:
        """Whether this is mostly prior rather than observed scoring."""
        return self.games_played < PRIOR_GAMES


def build_strengths(
    results: Iterable[WeekResult],
    roster_ids: Iterable[int],
    dynasty_values: Optional[dict[int, int]] = None,
) -> dict[int, TeamStrength]:
    """Estimate each team's scoring distribution.

    Args:
        results: Weekly results (unplayed weeks are ignored).
        roster_ids: Every roster to project, so teams with no games yet
            still get a prior-only estimate.
        dynasty_values: Optional roster_id -> KTC value, used to tilt the
            prior mean. This is the only differentiating signal in week 1.

    Returns:
        roster_id -> TeamStrength.
    """
    results = played(results)
    scores: dict[int, list[float]] = {}
    for result in results:
        scores.setdefault(result.roster_id, []).append(result.points)

    all_scores = [points for team in scores.values() for points in team]
    league_mean = fmean(all_scores) if all_scores else 100.0
    league_stdev = stdev(all_scores) if len(all_scores) > 1 else DEFAULT_STDEV

    priors = _prior_means(roster_ids, league_mean, dynasty_values)

    strengths: dict[int, TeamStrength] = {}
    for roster_id in roster_ids:
        team_scores = scores.get(roster_id, [])
        games = len(team_scores)
        prior_mean = priors[roster_id]

        if games:
            observed_mean = fmean(team_scores)
            mean = (games * observed_mean + PRIOR_GAMES * prior_mean) / (
                games + PRIOR_GAMES
            )
        else:
            observed_mean = None
            mean = prior_mean

        # Team-level spread needs more data than the mean does, so lean on
        # the league-wide spread until a team has a real sample.
        if games > 2:
            team_stdev = stdev(team_scores)
            spread = (games * team_stdev + PRIOR_GAMES * league_stdev) / (
                games + PRIOR_GAMES
            )
        else:
            spread = league_stdev

        strengths[roster_id] = TeamStrength(
            roster_id=roster_id,
            mean=mean,
            stdev=max(spread, 1.0),
            games_played=games,
            raw_mean=observed_mean,
        )

    return strengths


def _prior_means(
    roster_ids: Iterable[int],
    league_mean: float,
    dynasty_values: Optional[dict[int, int]],
) -> dict[int, float]:
    """Prior mean per roster: league average, tilted by dynasty value."""
    roster_ids = list(roster_ids)
    values = {
        roster_id: (dynasty_values or {}).get(roster_id, 0)
        for roster_id in roster_ids
    }
    live = [value for value in values.values() if value > 0]

    if len(live) < 2:
        return {roster_id: league_mean for roster_id in roster_ids}

    best, worst = max(live), min(live)
    if best == worst:
        return {roster_id: league_mean for roster_id in roster_ids}

    priors = {}
    for roster_id in roster_ids:
        value = values[roster_id]
        if value <= 0:
            priors[roster_id] = league_mean
            continue
        # Map value onto [-1, 1], then onto +/-DYNASTY_TILT of the mean.
        position = (value - worst) / (best - worst) * 2 - 1
        priors[roster_id] = league_mean * (1 + DYNASTY_TILT * position)
    return priors


def win_probability(a: TeamStrength, b: TeamStrength) -> float:
    """P(a outscores b) in a single week.

    The difference of two independent normals is normal, so this is one
    normal CDF evaluation rather than a simulation.
    """
    spread = (a.stdev**2 + b.stdev**2) ** 0.5
    if spread <= 0:
        return 0.5
    return NormalDist().cdf((a.mean - b.mean) / spread)


@dataclass
class MatchupPrediction:
    """A projected result for one head-to-head matchup."""

    week: int
    roster_id: int
    opponent_roster_id: int
    favorite_roster_id: int
    confidence: float
    projected_points: float
    opponent_projected_points: float

    @property
    def underdog_roster_id(self) -> int:
        return (
            self.opponent_roster_id
            if self.favorite_roster_id == self.roster_id
            else self.roster_id
        )

    @property
    def is_cointoss(self) -> bool:
        """Close enough that calling a favorite is noise."""
        return self.confidence < 0.55


def predict_week(
    schedule: list[tuple[int, int]],
    strengths: dict[int, TeamStrength],
    week: int,
) -> list[MatchupPrediction]:
    """Predict every matchup in a week.

    Args:
        schedule: (roster_id, opponent_roster_id) pairs, one per matchup.
        strengths: Output of build_strengths.
        week: Week being predicted.
    """
    predictions = []
    for roster_id, opponent_id in schedule:
        a, b = strengths.get(roster_id), strengths.get(opponent_id)
        if a is None or b is None:
            continue
        probability = win_probability(a, b)
        favorite = roster_id if probability >= 0.5 else opponent_id
        predictions.append(
            MatchupPrediction(
                week=week,
                roster_id=roster_id,
                opponent_roster_id=opponent_id,
                favorite_roster_id=favorite,
                confidence=max(probability, 1 - probability),
                projected_points=a.mean,
                opponent_projected_points=b.mean,
            )
        )
    return predictions


def week_schedule(results: Iterable[WeekResult]) -> list[tuple[int, int]]:
    """Distinct matchups in a week, as (lower roster id, higher roster id).

    Sleeper lists both sides of a matchup, so dedupe to one pair each.
    """
    pairs = set()
    for result in results:
        if result.opponent_roster_id is None:
            continue
        pairs.add(
            (
                min(result.roster_id, result.opponent_roster_id),
                max(result.roster_id, result.opponent_roster_id),
            )
        )
    return sorted(pairs)


@dataclass
class FinishOdds:
    """Simulated season-end outcomes for one team."""

    roster_id: int
    playoff_odds: float = 0.0
    last_place_odds: float = 0.0
    expected_wins: float = 0.0
    mean_seed: float = 0.0
    clinched: bool = False
    eliminated: bool = False
    sacko_clinched: bool = False


@dataclass
class SimulationInput:
    """Everything needed to simulate the rest of a season."""

    strengths: dict[int, TeamStrength]
    # Results already in the books, used for the starting standings.
    completed: list[WeekResult] = field(default_factory=list)
    # week -> [(roster_id, opponent_roster_id)] still to be played.
    remaining: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    playoff_teams: int = 6

    @property
    def games_remaining(self) -> int:
        return sum(len(pairs) for pairs in self.remaining.values())


def simulate_finishes(
    inputs: SimulationInput,
    simulations: int = DEFAULT_SIMULATIONS,
    seed: Optional[int] = None,
) -> dict[int, FinishOdds]:
    """Monte Carlo the remaining schedule and tally finishing positions.

    Seeding follows the common Sleeper default: wins first, then total
    points as the tiebreak.

    Args:
        inputs: Strengths, completed results, and remaining schedule.
        simulations: Number of seasons to simulate.
        seed: Fixed seed for reproducibility (tests).

    Returns:
        roster_id -> FinishOdds.
    """
    rng = random.Random(seed)

    records = build_records(played(inputs.completed))
    roster_ids = sorted(inputs.strengths)

    base_wins = {
        roster_id: (
            records[roster_id].wins + 0.5 * records[roster_id].ties
            if roster_id in records
            else 0.0
        )
        for roster_id in roster_ids
    }
    base_points = {
        roster_id: records[roster_id].points_for if roster_id in records else 0.0
        for roster_id in roster_ids
    }

    odds = {roster_id: FinishOdds(roster_id) for roster_id in roster_ids}
    seed_totals = {roster_id: 0 for roster_id in roster_ids}
    win_totals = {roster_id: 0.0 for roster_id in roster_ids}

    for _ in range(simulations):
        wins = dict(base_wins)
        points = dict(base_points)

        for pairs in inputs.remaining.values():
            for roster_id, opponent_id in pairs:
                a = inputs.strengths[roster_id]
                b = inputs.strengths[opponent_id]
                a_score = rng.gauss(a.mean, a.stdev)
                b_score = rng.gauss(b.mean, b.stdev)
                points[roster_id] += a_score
                points[opponent_id] += b_score
                if a_score > b_score:
                    wins[roster_id] += 1
                elif b_score > a_score:
                    wins[opponent_id] += 1
                else:
                    wins[roster_id] += 0.5
                    wins[opponent_id] += 0.5

        standings = sorted(
            roster_ids, key=lambda rid: (wins[rid], points[rid]), reverse=True
        )
        for position, roster_id in enumerate(standings, 1):
            seed_totals[roster_id] += position
            if position <= inputs.playoff_teams:
                odds[roster_id].playoff_odds += 1
        odds[standings[-1]].last_place_odds += 1

        for roster_id in roster_ids:
            win_totals[roster_id] += wins[roster_id]

    for roster_id, result in odds.items():
        result.playoff_odds /= simulations
        result.last_place_odds /= simulations
        result.expected_wins = win_totals[roster_id] / simulations
        result.mean_seed = seed_totals[roster_id] / simulations

    _apply_certainties(inputs, odds)
    return odds


def _apply_certainties(
    inputs: SimulationInput, odds: dict[int, FinishOdds]
) -> None:
    """Mark clinched/eliminated only where it's mathematically certain.

    Monte Carlo alone can't prove certainty - 10,000 misses is not zero. So
    where few enough games remain to enumerate every outcome (2^n), check
    exhaustively. Beyond that, leave the flags off rather than inferring
    certainty from a simulation, and let the odds speak for themselves.
    """
    remaining_pairs = [
        pair for pairs in inputs.remaining.values() for pair in pairs
    ]
    if len(remaining_pairs) > MAX_ENUMERABLE_GAMES:
        return

    records = build_records(played(inputs.completed))
    roster_ids = sorted(inputs.strengths)
    base_wins = {
        roster_id: (
            records[roster_id].wins + 0.5 * records[roster_id].ties
            if roster_id in records
            else 0.0
        )
        for roster_id in roster_ids
    }
    base_points = {
        roster_id: records[roster_id].points_for if roster_id in records else 0.0
        for roster_id in roster_ids
    }

    made = {roster_id: 0 for roster_id in roster_ids}
    last = {roster_id: 0 for roster_id in roster_ids}
    outcomes = 0

    for mask in range(1 << len(remaining_pairs)):
        wins = dict(base_wins)
        for index, (roster_id, opponent_id) in enumerate(remaining_pairs):
            if mask >> index & 1:
                wins[roster_id] += 1
            else:
                wins[opponent_id] += 1

        # Points are unknowable, so the tiebreak falls back to current
        # points. That makes these flags slightly conservative, which is the
        # right direction for a claim of certainty.
        standings = sorted(
            roster_ids,
            key=lambda rid: (wins[rid], base_points[rid]),
            reverse=True,
        )
        outcomes += 1
        for position, roster_id in enumerate(standings, 1):
            if position <= inputs.playoff_teams:
                made[roster_id] += 1
        last[standings[-1]] += 1

    for roster_id, result in odds.items():
        result.clinched = made[roster_id] == outcomes
        result.eliminated = made[roster_id] == 0
        result.sacko_clinched = last[roster_id] == outcomes


def split_schedule(
    results: Iterable[WeekResult], through_week: int
) -> tuple[list[WeekResult], dict[int, list[tuple[int, int]]]]:
    """Split results into completed weeks and a remaining-schedule map.

    Sleeper publishes future matchups with zero points, which is what makes
    projecting the rest of the season possible at all.

    Args:
        results: All results fetched for the season.
        through_week: Last week of the regular season to include.

    Returns:
        (completed results, {week: [(roster_id, opponent_id)]}).
    """
    results = list(results)
    completed = [r for r in results if r.played]
    completed_weeks = {r.week for r in completed}

    remaining: dict[int, list[tuple[int, int]]] = {}
    for week, week_results in sorted(by_week(results).items()):
        if week in completed_weeks or week > through_week:
            continue
        pairs = week_schedule(week_results)
        if pairs:
            remaining[week] = pairs

    return completed, remaining

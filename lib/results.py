"""Weekly results derivation.

Sleeper keeps every week's matchup data forever, per league, and prior
seasons chain backwards through `previous_league_id`. The payload already
carries everything needed to reconstruct a week - `starters`, `players`,
`players_points` and each team's `points` - so results are **computed here
rather than stored**. Copying them into our own tables would mean keeping a
second source of truth in sync, and buying a backfill, for data that can
always be recomputed. (Contrast `ktc_values` and `roster_snapshots`, which
are stored precisely because upstream won't give them back.)

This module is the single place that reconstruction happens. Power
rankings, weekly awards, the shame wall, the luck index and head-to-head
records all read from `WeekResult` instead of each re-deriving matchup
pairing and optimal lineups from raw Sleeper payloads.

Completed weeks are immutable, so they're cached in-process; the current
week never is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

logger = logging.getLogger("dynasty_bot.results")

# Position eligibility for flex slots. Canonical home for these - cogs and
# scripts import them from cogs.analytics, which re-exports.
FLEX_POSITIONS = {
    "FLEX": ["RB", "WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
    "REC_FLEX": ["WR", "TE"],
    "WRRB_FLEX": ["WR", "RB"],
}

# Slots that don't score.
NON_SCORING_SLOTS = ("BN", "IR")

# Sleeper pads unfilled starter slots with the string "0".
EMPTY_STARTER = "0"

# A margin at or under this counts as a "close" game for luck purposes.
CLOSE_GAME_MARGIN = 10.0

# Fallbacks matching the league's long-standing setup, for the rare payload
# that omits them.
DEFAULT_PLAYOFF_WEEK_START = 15
DEFAULT_PLAYOFF_TEAMS = 6


class SleeperLike(Protocol):
    """The subset of clients.sleeper.SleeperClient this module needs."""

    async def get_league(self, league_id: str) -> dict[str, Any]: ...
    async def get_matchups(
        self, league_id: str, week: int
    ) -> list[dict[str, Any]]: ...
    async def get_rosters(self, league_id: str) -> list[dict[str, Any]]: ...
    async def get_winners_bracket(
        self, league_id: str
    ) -> list[dict[str, Any]]: ...


def playoff_rounds(playoff_teams: int) -> int:
    """How many single-elimination rounds it takes to crown a champion.

    Six teams means two byes plus a four-team wildcard round, so three
    rounds - the same as eight teams would need.
    """
    rounds = 1
    while (1 << rounds) < max(playoff_teams, 2):
        rounds += 1
    return rounds


def championship_week(league: dict[str, Any]) -> int:
    """The week the title game is played - the last week that counts.

    League rules score potential points "all the way through the season
    until the championship weekend", so this is the upper bound for season
    totals feeding the rookie draft order. Derived from the league's own
    playoff settings rather than hardcoded, because NFL week 18 is also
    scored by Sleeper and including it would credit a week played after
    the league's season ended.
    """
    settings = league.get("settings") or {}
    start = settings.get("playoff_week_start") or DEFAULT_PLAYOFF_WEEK_START
    teams = settings.get("playoff_teams") or DEFAULT_PLAYOFF_TEAMS
    return int(start) + playoff_rounds(int(teams)) - 1


def calculate_optimal_lineup(
    roster_players: list[dict],
    roster_positions: list[str],
) -> float:
    """Calculate maximum potential points for an optimal lineup.

    Greedily assigns the highest-scoring players to each roster slot,
    respecting position eligibility rules.

    Args:
        roster_players: List of dicts with 'position' and 'points' keys.
        roster_positions: List of roster slot positions from league settings.

    Returns:
        Maximum potential points for the optimal lineup.
    """
    available = sorted(roster_players, key=lambda x: x.get("points", 0), reverse=True)
    used_indices = set()
    total_points = 0.0

    starter_positions = [p for p in roster_positions if p not in NON_SCORING_SLOTS]

    for slot in starter_positions:
        eligible = FLEX_POSITIONS.get(slot, [slot])

        for idx, player in enumerate(available):
            if idx in used_indices:
                continue
            if player.get("position") in eligible:
                total_points += player.get("points", 0)
                used_indices.add(idx)
                break

    return total_points


@dataclass(frozen=True)
class WeekResult:
    """One team's outcome in one week.

    `optimal_points` is the best lineup that team's roster could have
    fielded, so `points_left_on_bench` is the self-inflicted gap - which is
    what the shame wall and "best bench" award actually mean, rather than
    the sum of bench scoring.
    """

    season: int
    week: int
    roster_id: int
    points: float
    optimal_points: float
    opponent_roster_id: Optional[int] = None
    opponent_points: Optional[float] = None
    starters: tuple[str, ...] = ()
    starter_points: dict[str, float] = field(default_factory=dict)
    # Sleeper user ids, populated when a roster->owner map is supplied.
    # Needed for anything spanning seasons: roster_id 4 in 2024 is not
    # necessarily the same person as roster_id 4 today, so all-time records
    # have to be attributed by owner, not by roster.
    owner_id: Optional[str] = None
    opponent_owner_id: Optional[str] = None

    @property
    def played(self) -> bool:
        """Whether this week actually happened.

        Sleeper returns rostered-but-unplayed weeks with zero points, so a
        matchup where neither side scored is treated as not yet played.
        """
        if self.opponent_points is None:
            return self.points > 0
        return self.points > 0 or self.opponent_points > 0

    @property
    def result(self) -> Optional[str]:
        """'W', 'L', 'T', or None if unplayed or unopposed (bye)."""
        if not self.played or self.opponent_points is None:
            return None
        if self.points > self.opponent_points:
            return "W"
        if self.points < self.opponent_points:
            return "L"
        return "T"

    @property
    def margin(self) -> Optional[float]:
        """Points scored minus points allowed."""
        if self.opponent_points is None:
            return None
        return self.points - self.opponent_points

    @property
    def points_left_on_bench(self) -> float:
        """How much better the optimal lineup would have scored."""
        return max(0.0, self.optimal_points - self.points)

    @property
    def was_close(self) -> bool:
        """Decided by CLOSE_GAME_MARGIN or less."""
        margin = self.margin
        return margin is not None and abs(margin) <= CLOSE_GAME_MARGIN

    @property
    def would_have_won(self) -> bool:
        """Lost, but the optimal lineup would have taken it.

        The precise definition of "this one was your own fault".
        """
        return (
            self.result == "L"
            and self.opponent_points is not None
            and self.optimal_points > self.opponent_points
        )


def build_week_results(
    matchups: list[dict[str, Any]],
    players: dict[str, dict[str, Any]],
    roster_positions: list[str],
    season: int,
    week: int,
    owner_by_roster: Optional[dict[int, str]] = None,
) -> list[WeekResult]:
    """Reconstruct every team's result for one week from Sleeper matchups.

    Args:
        matchups: Sleeper's matchup list for the week.
        players: Sleeper player map, for position lookup.
        roster_positions: League roster slots.
        season: Season year.
        week: Week number.
        owner_by_roster: Optional roster_id -> Sleeper user id for that
            season, needed for cross-season attribution.

    Returns:
        One WeekResult per roster present in `matchups`.
    """
    owner_by_roster = owner_by_roster or {}
    by_matchup: dict[Any, list[dict[str, Any]]] = {}
    for entry in matchups or []:
        by_matchup.setdefault(entry.get("matchup_id"), []).append(entry)

    results: list[WeekResult] = []
    for entry in matchups or []:
        roster_id = entry.get("roster_id")
        if roster_id is None:
            continue

        # An unpaired entry (or matchup_id None) means no opponent this week.
        opponent = next(
            (
                other
                for other in by_matchup.get(entry.get("matchup_id"), [])
                if other.get("roster_id") != roster_id
            ),
            None,
        ) if entry.get("matchup_id") is not None else None

        players_points = entry.get("players_points") or {}
        roster_players = [
            {
                "position": (players.get(player_id) or {}).get("position", ""),
                "points": points or 0,
            }
            for player_id, points in players_points.items()
        ]

        starters = tuple(
            s for s in (entry.get("starters") or []) if s and s != EMPTY_STARTER
        )

        results.append(
            WeekResult(
                season=season,
                week=week,
                roster_id=roster_id,
                points=entry.get("points") or 0.0,
                optimal_points=calculate_optimal_lineup(
                    roster_players, roster_positions
                ),
                opponent_roster_id=opponent.get("roster_id") if opponent else None,
                opponent_points=(opponent.get("points") or 0.0) if opponent else None,
                starters=starters,
                starter_points={s: players_points.get(s) or 0.0 for s in starters},
                owner_id=owner_by_roster.get(roster_id),
                opponent_owner_id=(
                    owner_by_roster.get(opponent.get("roster_id"))
                    if opponent
                    else None
                ),
            )
        )

    return results


# Completed weeks never change, so they're safe to memoize for the life of
# the process. Keyed by (league_id, week); the in-progress week is never
# inserted. Cleared by clear_cache() in tests.
_week_cache: dict[tuple[str, int, bool], list[WeekResult]] = {}


def clear_cache() -> None:
    """Drop memoized week results (tests, or after a league correction)."""
    _week_cache.clear()


async def get_week_results(
    sleeper: SleeperLike,
    league_id: str,
    week: int,
    *,
    players: dict[str, dict[str, Any]],
    league: Optional[dict[str, Any]] = None,
    owner_by_roster: Optional[dict[int, str]] = None,
    use_cache: bool = True,
) -> list[WeekResult]:
    """Results for one week of one league.

    Args:
        sleeper: Sleeper client.
        league_id: League to read.
        week: Week number.
        players: Sleeper player map (callers pass it in; it's a 5MB fetch
            that the client caches, and every caller here needs it).
        league: Pre-fetched league dict, to avoid a redundant call.
        owner_by_roster: Optional roster_id -> owner id for this season.
        use_cache: Set False to force a refetch.

    Returns:
        WeekResults for that week, or [] if the week has no data.
    """
    # Owner attribution changes the shape of the result, so cached entries
    # with and without it must not be confused for one another.
    cache_key = (league_id, week, bool(owner_by_roster))
    if use_cache and cache_key in _week_cache:
        return _week_cache[cache_key]

    league = league or await sleeper.get_league(league_id)
    season = int(league.get("season") or 0)
    current_week = league.get("settings", {}).get("leg", 1)

    matchups = await sleeper.get_matchups(league_id, week)
    results = build_week_results(
        matchups,
        players,
        league.get("roster_positions", []),
        season,
        week,
        owner_by_roster,
    )

    # Only memoize weeks that can no longer change.
    if week < current_week:
        _week_cache[cache_key] = results

    return results


async def get_season_results(
    sleeper: SleeperLike,
    league_id: str,
    *,
    players: dict[str, dict[str, Any]],
    league: Optional[dict[str, Any]] = None,
    owner_by_roster: Optional[dict[int, str]] = None,
    through_week: Optional[int] = None,
) -> list[WeekResult]:
    """Every team's results for weeks 1..through_week of one season.

    `through_week` defaults to the league's current week.
    """
    league = league or await sleeper.get_league(league_id)
    last_week = through_week or league.get("settings", {}).get("leg", 1) or 1

    results: list[WeekResult] = []
    for week in range(1, last_week + 1):
        results.extend(
            await get_week_results(
                sleeper,
                league_id,
                week,
                players=players,
                league=league,
                owner_by_roster=owner_by_roster,
            )
        )
    return results


async def get_league_chain(
    sleeper: SleeperLike, league_id: str, max_seasons: int = 25
) -> list[dict[str, Any]]:
    """League dicts from the current season backwards.

    Follows `previous_league_id`, which is how Sleeper models a dynasty
    league continuing year over year. `max_seasons` is a loop guard against
    a malformed chain, not a real limit.
    """
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    next_id: Optional[str] = league_id

    while next_id and next_id not in seen and len(chain) < max_seasons:
        seen.add(next_id)
        try:
            league = await sleeper.get_league(next_id)
        except Exception as e:
            logger.warning(f"Stopping league chain at {next_id}: {e}")
            break
        if not league:
            break
        chain.append(league)
        next_id = league.get("previous_league_id")

    return chain


async def get_history_results(
    sleeper: SleeperLike,
    league_id: str,
    *,
    players: dict[str, dict[str, Any]],
    max_seasons: int = 25,
    by_owner: bool = False,
) -> list[WeekResult]:
    """Results across every season this league chains back through.

    Args:
        by_owner: Attach owner ids, fetching each season's rosters to do it.
            Required for all-time records, since roster ids are only stable
            within a season.

    Note this walks each season's weeks, so it's the expensive call in this
    module - a handful of API requests per season. Fine for a slash command
    that runs occasionally; don't put it in a loop.
    """
    results: list[WeekResult] = []
    for league in await get_league_chain(sleeper, league_id, max_seasons):
        owner_by_roster = None
        if by_owner:
            try:
                rosters = await sleeper.get_rosters(league["league_id"])
                owner_by_roster = {
                    r["roster_id"]: r.get("owner_id")
                    for r in rosters
                    if r.get("roster_id") is not None
                }
            except Exception as e:
                logger.warning(
                    f"No rosters for {league['league_id']}, "
                    f"skipping owner attribution: {e}"
                )

        # Completed seasons: read the full regular season plus playoffs.
        # In-progress: get_season_results stops at the current week.
        results.extend(
            await get_season_results(
                sleeper,
                league["league_id"],
                players=players,
                league=league,
                owner_by_roster=owner_by_roster,
            )
        )
    return results


async def get_champions(
    sleeper: SleeperLike, league_id: str, max_seasons: int = 25
) -> list[tuple[int, Optional[str]]]:
    """(season, champion owner id) for every completed season in the chain.

    Reads Sleeper's winners bracket, which is authoritative, rather than
    guessing from a championship-week matchup id. Seasons still in progress
    are omitted.
    """
    champions: list[tuple[int, Optional[str]]] = []
    for league in await get_league_chain(sleeper, league_id, max_seasons):
        try:
            bracket = await sleeper.get_winners_bracket(league["league_id"])
        except Exception as e:
            logger.warning(f"No bracket for {league['league_id']}: {e}")
            continue

        winner_roster = champion_from_bracket(bracket)
        if winner_roster is None:
            continue

        try:
            rosters = await sleeper.get_rosters(league["league_id"])
        except Exception as e:
            logger.warning(f"No rosters for {league['league_id']}: {e}")
            continue

        owner_id = next(
            (
                r.get("owner_id")
                for r in rosters
                if r.get("roster_id") == winner_roster
            ),
            None,
        )
        champions.append((int(league.get("season") or 0), owner_id))

    return champions


# =========================================================================
# Aggregations
# =========================================================================

def played(results: Iterable[WeekResult]) -> list[WeekResult]:
    """Filter to weeks that actually happened."""
    return [r for r in results if r.played]


def by_roster(results: Iterable[WeekResult]) -> dict[int, list[WeekResult]]:
    """Group results by roster_id."""
    grouped: dict[int, list[WeekResult]] = {}
    for result in results:
        grouped.setdefault(result.roster_id, []).append(result)
    return grouped


def by_week(results: Iterable[WeekResult]) -> dict[int, list[WeekResult]]:
    """Group results by week number."""
    grouped: dict[int, list[WeekResult]] = {}
    for result in results:
        grouped.setdefault(result.week, []).append(result)
    return grouped


@dataclass
class TeamRecord:
    """A team's aggregate record over some set of weeks."""

    roster_id: int
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    optimal_points: float = 0.0

    @property
    def games(self) -> int:
        return self.wins + self.losses + self.ties

    @property
    def win_pct(self) -> float:
        """Win percentage 0-100, counting a tie as half a win."""
        if not self.games:
            return 0.0
        return (self.wins + 0.5 * self.ties) / self.games * 100

    @property
    def points_left_on_bench(self) -> float:
        return max(0.0, self.optimal_points - self.points_for)

    @property
    def record_text(self) -> str:
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base


def build_records(results: Iterable[WeekResult]) -> dict[int, TeamRecord]:
    """Aggregate WeekResults into a per-roster record."""
    records: dict[int, TeamRecord] = {}
    for result in results:
        record = records.setdefault(result.roster_id, TeamRecord(result.roster_id))
        record.points_for += result.points
        record.optimal_points += result.optimal_points
        if result.opponent_points is not None:
            record.points_against += result.opponent_points

        outcome = result.result
        if outcome == "W":
            record.wins += 1
        elif outcome == "L":
            record.losses += 1
        elif outcome == "T":
            record.ties += 1
    return records


@dataclass
class LuckIndex:
    """How much a team's record owes to schedule rather than scoring.

    The core measure is **all-play expected wins**: what your record would
    be if you played every other team every week. A team can score well and
    lose because it drew the week's high scorer; expected wins strips the
    schedule out, so `luck_score` (actual minus expected) is the part of
    your record you didn't earn.

    `efficiency` is deliberately separate. Leaving points on the bench is a
    decision, not luck, so folding it into the same number would muddle two
    different claims.
    """

    roster_id: int
    actual_wins: float = 0.0
    expected_wins: float = 0.0
    points_for: float = 0.0
    points_against: float = 0.0
    optimal_points: float = 0.0
    close_wins: int = 0
    close_losses: int = 0
    games: int = 0

    @property
    def luck_score(self) -> float:
        """Wins above (or below) what all-play scoring deserved."""
        return self.actual_wins - self.expected_wins

    @property
    def efficiency(self) -> float:
        """Share of the optimal lineup actually started, 0-1."""
        if self.optimal_points <= 0:
            return 0.0
        return min(1.0, self.points_for / self.optimal_points)

    @property
    def points_against_per_game(self) -> float:
        return self.points_against / self.games if self.games else 0.0


def compute_luck(results: Iterable[WeekResult]) -> dict[int, LuckIndex]:
    """Luck index per roster over the given weeks.

    Only played weeks count. Expected wins for a week is the share of the
    other teams that week you outscored, so a week where you'd have beaten
    9 of 11 contributes 9/11 of a win.
    """
    results = played(results)
    indexes: dict[int, LuckIndex] = {}

    for result in results:
        index = indexes.setdefault(result.roster_id, LuckIndex(result.roster_id))
        index.points_for += result.points
        index.optimal_points += result.optimal_points
        if result.opponent_points is not None:
            index.points_against += result.opponent_points

        outcome = result.result
        if outcome is None:
            continue
        index.games += 1
        if outcome == "W":
            index.actual_wins += 1
            if result.was_close:
                index.close_wins += 1
        elif outcome == "L":
            if result.was_close:
                index.close_losses += 1
        else:
            index.actual_wins += 0.5

    # All-play: compare every team's score against every other that week.
    for week_results in by_week(results).values():
        scores = [(r.roster_id, r.points) for r in week_results]
        if len(scores) < 2:
            continue
        opponents = len(scores) - 1
        for roster_id, points in scores:
            beaten = sum(1 for other_id, other in scores if other_id != roster_id and points > other)
            tied = sum(1 for other_id, other in scores if other_id != roster_id and points == other)
            index = indexes.get(roster_id)
            if index:
                index.expected_wins += (beaten + 0.5 * tied) / opponents

    return indexes


def champion_from_bracket(bracket: list[dict[str, Any]]) -> Optional[int]:
    """Roster id that won a season, from Sleeper's winners bracket.

    The championship is the final round's match - the highest `r` value.
    Returns None for a bracket that hasn't been played out yet.
    """
    played_matches = [m for m in bracket or [] if m.get("w") is not None]
    if not played_matches:
        return None
    final = max(played_matches, key=lambda m: m.get("r") or 0)
    return final.get("w")


def head_to_head(results: Iterable[WeekResult]) -> dict[tuple[int, int], TeamRecord]:
    """All-time record for each (roster_id, opponent_roster_id) pair.

    Both directions are present, so a lookup never has to be flipped.
    """
    pairs: dict[tuple[int, int], TeamRecord] = {}
    for result in results:
        if result.opponent_roster_id is None or result.result is None:
            continue
        key = (result.roster_id, result.opponent_roster_id)
        record = pairs.setdefault(key, TeamRecord(result.roster_id))
        _accumulate_h2h(record, result)
    return pairs


def head_to_head_owners(
    results: Iterable[WeekResult],
) -> dict[tuple[str, str], TeamRecord]:
    """All-time record for each (owner_id, opponent_owner_id) pair.

    The cross-season version of head_to_head. Roster ids are only stable
    within a season, so anything spanning years has to key on owner - use
    get_history_results(..., by_owner=True) to populate the ids.

    Results without owner attribution are skipped rather than guessed at.
    """
    pairs: dict[tuple[str, str], TeamRecord] = {}
    for result in results:
        if not result.owner_id or not result.opponent_owner_id:
            continue
        if result.result is None:
            continue
        key = (result.owner_id, result.opponent_owner_id)
        record = pairs.setdefault(key, TeamRecord(result.roster_id))
        _accumulate_h2h(record, result)
    return pairs


def _accumulate_h2h(record: TeamRecord, result: WeekResult) -> None:
    """Fold one result into a head-to-head record."""
    record.points_for += result.points
    record.points_against += result.opponent_points or 0.0
    record.optimal_points += result.optimal_points
    if result.result == "W":
        record.wins += 1
    elif result.result == "L":
        record.losses += 1
    else:
        record.ties += 1

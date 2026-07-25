"""NFL preseason calendar, which nflverse doesn't carry.

nflverse publishes no preseason games at all - verified across every season it
carries (1999-2026, 7,548 games: all REG/WC/DIV/CON/SB). That matters because
one league deadline is defined against them: taxi squad decisions are due "by
the end of the last game of the first week of NFL preseason games".

Until now that date was estimated from the regular-season opener. ESPN does
publish the preseason schedule, and it's released once a year alongside the
regular season, so it only has to be fetched annually - see
`clients/espn.py` and `/sync_nfl`.

The parsing and week-picking live here as pure functions over already-fetched
payloads, so the interesting logic is testable without network.

## Which week is "the first week of preseason games"

ESPN numbers the Hall of Fame Game as preseason **week 1** on its own, then the
full 16-game weeks as 2, 3 and 4. Stable across every year checked:

    2023  wk1: 1 game  Aug 4      wk2: 16 games  Aug 10-13
    2024  wk1: 1 game  Aug 2      wk2: 16 games  Aug  8-11
    2025  wk1: 1 game  Aug 1      wk2: 16 games  Aug  7-10
    2026  wk1: 1 game  Aug 7      wk2: 16 games  Aug 13-16

So the literal reading of "first week" would pin a league-wide roster deadline
to a single exhibition game between two teams - which is almost certainly not
what the rule means, and would cost owners nine days. `first_full_week` instead
takes the earliest week in which more than one game is played. That also
handles years where the Hall of Fame game is cancelled (2020, 2021) without a
special case, because then the first week already has 16 games.

Set `TAXI_DEADLINE_INCLUDES_HOF_GAME = True` for the literal reading; it is the
only edit required.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

logger = logging.getLogger("dynasty_bot.nfl_calendar")

# Where /sync_nfl stores the dates it fetched, so reading them costs nothing.
DEADLINES_PATH = Path(__file__).parent.parent / "config" / "deadlines.yaml"

# Anchor keys written by /sync_nfl.
ANCHOR_TAXI_DEADLINE = "nfl_taxi_deadline"
ANCHOR_PRESEASON_START = "nfl_preseason_start"
ANCHOR_PRESEASON_END = "nfl_preseason_end"
ANCHOR_ROOKIE_DRAFT_END = "rookie_draft_end"

# Whether the Hall of Fame Game counts as "the first week of preseason games".
# See the module docstring - False means the first week every team plays.
TAXI_DEADLINE_INCLUDES_HOF_GAME = False

# ESPN's season type for preseason.
ESPN_SEASON_TYPE_PRESEASON = 1

# Preseason weeks to ask ESPN for. Four covers a Hall of Fame game plus three
# full weeks; extra weeks simply come back empty.
PRESEASON_WEEKS = (1, 2, 3, 4)


@dataclass(frozen=True)
class PreseasonWeek:
    """One numbered week of the preseason, as ESPN reports it."""

    week: int
    first_game: date
    last_game: date
    game_count: int

    @property
    def is_league_wide(self) -> bool:
        """Whether more than one game is played, i.e. not just the HOF game."""
        return self.game_count > 1


def _parse_game_date(value: Any) -> Optional[date]:
    """Parse an ESPN ISO timestamp (e.g. '2026-08-13T23:00Z') to a date.

    ESPN reports kickoff in UTC, so a Saturday night game shows up as Sunday.
    Only the date is kept, and the deadline is a whole-day boundary, so an
    hours-level shift can't change which week a game belongs to.
    """
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        # Fall back to a plain date prefix rather than dropping the game.
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"Could not parse ESPN game date: {value!r}")
            return None


def parse_preseason_week(payload: dict[str, Any]) -> Optional[PreseasonWeek]:
    """Build a PreseasonWeek from one ESPN scoreboard response.

    Returns None for a week with no games, which is how ESPN reports weeks
    that don't exist in a given year.
    """
    events = payload.get("events") or []
    dates = [d for d in (_parse_game_date(e.get("date")) for e in events) if d]
    if not dates:
        return None

    week_number = ((payload.get("week") or {}).get("number")) or 0
    return PreseasonWeek(
        week=int(week_number),
        first_game=min(dates),
        last_game=max(dates),
        game_count=len(dates),
    )


def parse_preseason_weeks(
    payloads: Iterable[dict[str, Any]],
) -> list[PreseasonWeek]:
    """Parse and order every non-empty preseason week."""
    weeks = [w for w in (parse_preseason_week(p) for p in payloads) if w]
    return sorted(weeks, key=lambda w: (w.week, w.first_game))


def first_full_week(weeks: Iterable[PreseasonWeek]) -> Optional[PreseasonWeek]:
    """The first preseason week in which more than one game is played.

    Falls back to the earliest week of any size, so a year that somehow only
    ever has single-game weeks still yields an answer instead of None.
    """
    ordered = sorted(weeks, key=lambda w: (w.week, w.first_game))
    if not ordered:
        return None
    for week in ordered:
        if week.is_league_wide:
            return week
    return ordered[0]


def taxi_deadline(weeks: Iterable[PreseasonWeek]) -> Optional[date]:
    """The date taxi squad decisions are due.

    "The end of the last game of the first week of NFL preseason games" - so
    the last game *date* of that week. Returns None when the preseason
    schedule isn't published yet, which callers must treat as "unknown"
    rather than "no deadline".
    """
    ordered = sorted(weeks, key=lambda w: (w.week, w.first_game))
    if not ordered:
        return None
    if TAXI_DEADLINE_INCLUDES_HOF_GAME:
        return ordered[0].last_game
    week = first_full_week(ordered)
    return week.last_game if week else None


def load_anchors(path: Optional[Path] = None) -> dict[str, Any]:
    """The `nfl_anchors` block from deadlines.yaml, empty if unavailable."""
    try:
        with open(path or DEADLINES_PATH) as f:
            config = yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        logger.warning(f"Could not read NFL anchors: {e}")
        return {}
    return config.get("nfl_anchors") or {}


def _anchor_date(anchors: dict[str, Any], key: str) -> Optional[date]:
    """Parse one stored ISO anchor, or None if absent or malformed."""
    raw = anchors.get(key)
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        logger.warning(f"Malformed {key}: {raw!r}")
        return None


def stored_taxi_deadline(
    season: int, anchors: Optional[dict[str, Any]] = None
) -> Optional[date]:
    """The taxi deadline for `season`, if it can be trusted at all.

    Returns None - meaning "unknown, fall back to season_type" - in three
    cases, each of which would otherwise close the addition window wrongly:

    1. **The stored date is for another season.** Anchors are written by a
       manual (or annual) sync, so a year-old date in the file is normal.
       Comparing today against a previous preseason would report the window
       shut when it isn't.

    2. **This season's rookie draft hasn't finished.** The window exists to let
       owners stash the picks they just made, so it cannot close before the
       draft that supplies them.

    3. **The draft finished after the deadline.** This is not hypothetical: the
       league's rookie draft ran to Aug 19 in 2025 and Aug 18 in 2023, while
       the first full week of NFL preseason ended Aug 10 and Aug 13. Applied
       literally the deadline had already expired before anyone could draft, so
       in those years the written rule cannot be what's enforced.

    Case 3 needs a ruling rather than a heuristic, so the bot declines to
    enforce a deadline it can prove is contradictory instead of picking one.
    """
    anchors = load_anchors() if anchors is None else anchors

    deadline = _anchor_date(anchors, ANCHOR_TAXI_DEADLINE)
    if not deadline:
        return None

    if deadline.year != season:
        logger.info(
            f"Stored taxi deadline {deadline} is not for {season}; "
            "treating as unknown"
        )
        return None

    draft_end = _anchor_date(anchors, ANCHOR_ROOKIE_DRAFT_END)
    if draft_end is None:
        logger.info(
            f"No completed {season} rookie draft on record; not enforcing the "
            f"taxi deadline {deadline}"
        )
        return None

    if draft_end > deadline:
        logger.warning(
            f"{season} rookie draft ended {draft_end}, after the taxi deadline "
            f"{deadline} — the written rule can't apply, so not enforcing it"
        )
        return None

    return deadline


def preseason_bounds(
    weeks: Iterable[PreseasonWeek],
) -> tuple[Optional[date], Optional[date]]:
    """(first game, last game) across the whole preseason.

    Unlike `NFLScheduleClient.get_preseason_dates`, these are observed rather
    than estimated off the regular-season opener.
    """
    ordered = list(weeks)
    if not ordered:
        return None, None
    return (
        min(w.first_game for w in ordered),
        max(w.last_game for w in ordered),
    )

"""NFL calendar dates the league's deadlines depend on.

The taxi squad deadline is the reason this module exists. It is now **the start
of the regular season** - changed from "the end of the last game of the first
week of NFL preseason games" because a preseason-relative deadline was painful
to manage: the rookie draft floats to whatever weekend owners can make and runs
24 hours per pick, so it repeatedly finished *after* the old deadline (2023 and
2025 both), leaving a deadline that had expired before anyone could draft.

Anchoring to the regular season removes that whole class of problem. The draft
has never run past early September, so the deadline can no longer precede the
draft that fills the slots, and the date comes from nflverse - which publishes
the regular-season schedule months ahead - rather than needing a preseason
source at all.

The preseason dates are still fetched and stored, but nothing depends on them
now; they are informational output of `/sync_nfl`. nflverse publishes no
preseason games at all (verified across 1999-2026, 7,548 games: every one is
REG/WC/DIV/CON/SB), which is why they come from ESPN - see `clients/espn.py`.

Parsing and date arithmetic live here as pure functions over already-fetched
payloads, so the interesting logic is testable without network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

logger = logging.getLogger("dynasty_bot.nfl_calendar")

# Where /sync_nfl stores the dates it fetched, so reading them costs nothing.
#
# Its own file, deliberately. These anchors used to live under an `nfl_anchors`
# key inside deadlines.yaml, which the scheduler rewrote wholesale with
# yaml.dump - stripping every comment out of a file that is otherwise
# hand-maintained. That was survivable while only a human running /sync_nfl
# triggered it; once the upkeep loop started syncing automatically it became a
# guaranteed loss twice a day. Bot-written data and hand-written config now live
# in separate files, so neither can clobber the other.
ANCHORS_PATH = Path(__file__).parent.parent / "config" / "nfl_anchors.yaml"

# Read-only fallback for anchors written before the split.
DEADLINES_PATH = Path(__file__).parent.parent / "config" / "deadlines.yaml"

# Anchor keys written by /sync_nfl.
ANCHOR_TAXI_DEADLINE = "nfl_taxi_deadline"
ANCHOR_PRESEASON_START = "nfl_preseason_start"
ANCHOR_PRESEASON_END = "nfl_preseason_end"
ANCHOR_ROOKIE_DRAFT_START = "rookie_draft_start"
ANCHOR_ROOKIE_DRAFT_END = "rookie_draft_end"
ANCHOR_REGULAR_SEASON_START = "nfl_regular_season_start"

# How far before the regular-season opener taxi decisions are due.
#
# The rule is "the start of the regular season", so a move made on kickoff day
# after games began must not count. One day earlier is unambiguous and can never
# permit that; the cost is at most the morning of the opener, and the
# commissioner checks manually anyway.
TAXI_DEADLINE_DAYS_BEFORE_OPENER = 1

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


def taxi_deadline_from_opener(opener: Optional[date]) -> Optional[date]:
    """The taxi deadline for a season, given its regular-season opener.

    "Taxi squad decisions must be made by the start of the regular season."
    Returns None when the opener isn't known, which callers must treat as
    "unknown" rather than "no deadline".
    """
    if opener is None:
        return None
    return opener - timedelta(days=TAXI_DEADLINE_DAYS_BEFORE_OPENER)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Parse a YAML mapping, or an empty dict if unreadable."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        logger.warning(f"Could not parse {path}: {e}")
        return {}


def load_anchors(path: Optional[Path] = None) -> dict[str, Any]:
    """The stored NFL date anchors, empty if none have been synced.

    Reads `config/nfl_anchors.yaml`, falling back to the legacy `nfl_anchors`
    block inside `deadlines.yaml` so anchors synced before the files were split
    still resolve until the next sync rewrites them in the new location.
    """
    anchors = _read_yaml(path or ANCHORS_PATH)
    if anchors:
        # Tolerate either a bare mapping or one nested under `nfl_anchors`.
        return anchors.get("nfl_anchors") or anchors

    if path is None:
        legacy = _read_yaml(DEADLINES_PATH).get("nfl_anchors") or {}
        if legacy:
            logger.info("Using legacy nfl_anchors from deadlines.yaml")
        return legacy
    return {}


def save_anchors(anchors: dict[str, Any], path: Optional[Path] = None) -> bool:
    """Write anchors, returning whether anything actually changed.

    Skipping no-op writes matters because the upkeep loop re-syncs on every tick
    until the rookie draft date lands, which can be weeks - there's no reason to
    rewrite an identical file 60 times.
    """
    target = path or ANCHORS_PATH
    if _read_yaml(target) == anchors:
        return False

    with open(target, "w") as f:
        f.write(
            "# NFL date anchors, written by /sync_nfl and the scheduler's\n"
            "# upkeep loop. Generated - edit deadlines.yaml instead.\n"
        )
        yaml.dump(anchors, f, default_flow_style=False, sort_keys=False,
                  allow_unicode=True)
    logger.info(f"Wrote {len(anchors)} NFL anchor(s) to {target.name}")
    return True


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
    """The taxi deadline for `season`, or None if it can't be determined.

    Always **derived** from the stored regular-season opener rather than stored
    itself. Persisting it would duplicate the opener, and a duplicate can go
    stale: the first version of this change left a `nfl_taxi_deadline` computed
    under the old preseason rule sitting in the anchors file, and because the
    upkeep loop's cheap path only polls for the draft date, nothing would ever
    have recomputed it. Deriving costs one subtraction and can't drift.

    Returns None - meaning "unknown, fall back to season_type" - when the stored
    opener belongs to another season. Anchors are written by the upkeep loop, so
    a year-old date sitting in the file is normal until it re-syncs, and
    comparing today against last season's opener would report the window shut
    when it isn't.

    Unlike the old preseason-based rule, this can't produce a deadline that
    precedes the draft filling the slots: the draft has never run past early
    September. The `draft_end` check remains only as a guard against a genuinely
    broken calendar.
    """
    anchors = load_anchors() if anchors is None else anchors

    deadline = taxi_deadline_from_opener(
        _anchor_date(anchors, ANCHOR_REGULAR_SEASON_START)
    )
    if deadline is None:
        return None

    if deadline.year != season:
        logger.info(
            f"Stored taxi deadline {deadline} is not for {season}; "
            "treating as unknown"
        )
        return None

    draft_end = _anchor_date(anchors, ANCHOR_ROOKIE_DRAFT_END)
    if draft_end and draft_end > deadline:
        logger.warning(
            f"{season} rookie draft ended {draft_end}, after the taxi deadline "
            f"{deadline} — deferring the deadline to the draft's end"
        )
        return draft_end

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

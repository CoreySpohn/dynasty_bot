"""Deriving the league's state instead of setting it by hand.

`config/league_state.yaml` gates which deadline reminders are live, and it has
always been manual - so a stale `current_state` silently sends the wrong set of
reminders, or none. Every signal it needs is observable, so this module works
out what the state *should* be and the scheduler cog applies it.

The three states in `VALID_STATES` and what decides each one:

| State       | Signal                                                        |
|-------------|---------------------------------------------------------------|
| `in_season` | NFL `season_type` is regular/post, or we're within a day of    |
|             | the regular-season opener (the rule the config comments give)  |
| `pre_season`| NFL preseason has started, or this year's rookie draft is      |
|             | complete - whichever comes first                              |
| `off_season`| Otherwise: NFL offseason, rookie draft not yet done            |

Two deliberate non-goals:

- **`pre_draft` is not derived.** The config comments describe it as "after
  rules voted, store closed, league renewed". A rules vote is a human event
  with no API, and `pre_draft` isn't in `VALID_STATES` anyway, so nothing here
  can or should set it.
- **This never moves the state backwards.** Advancing on a bad signal costs a
  wrong set of reminders for a few hours; going backwards could re-open a
  closed window or re-fire deadlines that already passed. Regressions are
  reported for a human to confirm - see `should_apply`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

logger = logging.getLogger("dynasty_bot.league_state")

OFF_SEASON = "off_season"
PRE_SEASON = "pre_season"
IN_SEASON = "in_season"

# The states the scheduler recognises, in the order the season moves through
# them. Index order is what makes "forwards" meaningful.
STATE_ORDER = (OFF_SEASON, PRE_SEASON, IN_SEASON)

# NFL season types that mean real games are being played.
IN_SEASON_TYPES = frozenset({"regular", "post"})

# How far ahead of the opener the league flips to in_season. The config
# comments say "day before NFL season starts".
IN_SEASON_LEAD = timedelta(days=1)


@dataclass(frozen=True)
class DerivedState:
    """A state the observable signals imply, and why."""

    state: str
    reason: str

    def __str__(self) -> str:
        return f"{self.state} ({self.reason})"


def derive_state(
    nfl_state: Optional[dict[str, Any]],
    rookie_draft_complete: bool = False,
    regular_season_start: Optional[date] = None,
    today: Optional[date] = None,
) -> Optional[DerivedState]:
    """Work out the state the league should be in.

    Args:
        nfl_state: Sleeper's `/state/nfl` payload.
        rookie_draft_complete: Whether this year's rookie draft has finished.
        regular_season_start: Opener date, from the stored NFL anchors.
        today: Defaults to today; injectable for tests.

    Returns:
        The implied state, or None when `nfl_state` is unusable - in which case
        the caller must leave the current state alone rather than guess.
    """
    if not nfl_state:
        return None
    season_type = nfl_state.get("season_type")
    if not season_type:
        return None

    today = today or date.today()

    if season_type in IN_SEASON_TYPES:
        return DerivedState(IN_SEASON, f"NFL season_type is {season_type!r}")

    # Still pre/off by NFL reckoning, but the opener is on top of us.
    if regular_season_start and today >= regular_season_start - IN_SEASON_LEAD:
        return DerivedState(
            IN_SEASON, f"NFL week 1 opens {regular_season_start.isoformat()}"
        )

    if season_type == "pre":
        return DerivedState(PRE_SEASON, "NFL preseason has started")

    if rookie_draft_complete:
        return DerivedState(PRE_SEASON, "rookie draft is complete")

    return DerivedState(OFF_SEASON, "NFL offseason, rookie draft not complete")


def is_forward(current: str, target: str) -> bool:
    """Whether `target` is later in the season than `current`.

    Unknown states compare as not-forward, so an unrecognised value in the
    config can never trigger an automatic change.
    """
    try:
        return STATE_ORDER.index(target) > STATE_ORDER.index(current)
    except ValueError:
        return False


def should_apply(current: str, derived: Optional[DerivedState]) -> bool:
    """Whether the scheduler may apply `derived` without asking.

    Only forward moves. The season runs off -> pre -> in and then wraps back to
    off, and that wrap is the one transition this refuses to make on its own:
    the signal for it (`season_type` returning to "off") appears the moment the
    Super Bowl ends, which is also when a human is deciding what the offseason
    calendar looks like. Better to surface it than to reset the league's state
    out from under them.
    """
    if derived is None:
        return False
    return is_forward(current, derived.state)

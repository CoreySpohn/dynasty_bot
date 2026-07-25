"""Following the league across annual renewals.

Renewing a dynasty league on Sleeper creates a **new league with a new ID**; the
old one freezes at the season it finished and never changes again. There is no
forward pointer - `previous_league_id` only walks backwards - so nothing can
follow a renewal from the old league alone.

What can be followed is the *owner*: `/user/<id>/leagues/nfl/<season>` lists the
leagues a user is in for a season, and the renewed league appears there. Match on
name and you have the new ID without touching config.

Name matching is load-bearing, not a nicety. The configured user is in three
leagues for 2025:

    1267592261261078528  '🪓 2025 Epsteins Island Was Never Real League'
    1254970896590839808  '2025 Epsteins Island Was Never Real League'
    1231652068087844864  'The Superflexers'

so picking "the first one" or "the one with 12 teams" would eventually pick
wrong. Every guard here exists to make a wrong match impossible rather than
unlikely - and when it can't be sure, it declines and the configured ID stands.
That's the safe failure: a stale league shows old data, while the wrong league
shows plausible data about strangers.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

logger = logging.getLogger("dynasty_bot.league_resolver")


class SupportsUserLeagues(Protocol):
    async def get_user_leagues(
        self, user_id: str, season: int
    ) -> list[dict[str, Any]]: ...

    async def get_nfl_state(self) -> dict[str, Any]: ...

    async def get_league(self, league_id: str) -> dict[str, Any]: ...


def _normalise(name: Optional[str]) -> str:
    """Casefold and collapse whitespace, so cosmetic edits still match."""
    return " ".join(str(name or "").split()).casefold()


def pick_league(
    leagues: list[dict[str, Any]],
    name: str,
    expected_rosters: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """The one league matching `name`, or None if that isn't unambiguous.

    Args:
        leagues: Candidates from `get_user_leagues`.
        name: Expected league name; compared case- and whitespace-insensitively.
        expected_rosters: If given, a candidate with a different team count is
            rejected. Guards against a same-named league of another size.

    Returns:
        The matching league dict, or None when there is no match or more than
        one - never a guess.
    """
    target = _normalise(name)
    if not target:
        logger.warning("No league name to match against; not resolving")
        return None

    matches = [lg for lg in leagues if _normalise(lg.get("name")) == target]

    if expected_rosters is not None:
        sized = [
            lg for lg in matches if lg.get("total_rosters") == expected_rosters
        ]
        if matches and not sized:
            logger.warning(
                f"Found {len(matches)} league(s) named {name!r} but none with "
                f"{expected_rosters} rosters; not resolving"
            )
            return None
        matches = sized

    if not matches:
        logger.info(f"No league named {name!r} found for that season")
        return None

    if len(matches) > 1:
        logger.warning(
            f"{len(matches)} leagues named {name!r} "
            f"({[lg.get('league_id') for lg in matches]}); refusing to guess"
        )
        return None

    return matches[0]


async def resolve_league_id(
    client: SupportsUserLeagues,
    user_id: Optional[str],
    configured_league_id: str,
    league_name: Optional[str] = None,
) -> str:
    """The current season's league ID for `user_id`, else `configured_league_id`.

    Every failure path returns `configured_league_id`, so a Sleeper outage or an
    unrecognised season can never leave the bot pointing at nothing.

    Args:
        client: Sleeper client.
        user_id: The owner whose leagues to search. None disables resolution.
        configured_league_id: `SLEEPER_LEAGUE_ID`, the fallback and the source
            of the expected name when `league_name` isn't given.
        league_name: Expected name. Defaults to the configured league's own
            name, so renewal needs no extra configuration - the name comes from
            the league already being pointed at.
    """
    if not user_id:
        logger.debug("No SLEEPER_USER_ID set; using the configured league ID")
        return configured_league_id

    try:
        state = await client.get_nfl_state()
        season = int(state.get("league_season") or state.get("season") or 0)
        if not season:
            logger.warning("NFL state carried no season; keeping configured ID")
            return configured_league_id

        current = await client.get_league(configured_league_id)
        expected_name = league_name or current.get("name")
        expected_rosters = current.get("total_rosters")

        # Already pointing at the right season: nothing to resolve, and this
        # avoids a needless lookup on every startup.
        if str(current.get("season")) == str(season):
            return configured_league_id

        leagues = await client.get_user_leagues(user_id, season)
        match = pick_league(leagues, expected_name, expected_rosters)
        if not match:
            logger.info(
                f"Could not resolve a {season} league named {expected_name!r}; "
                f"keeping {configured_league_id}"
            )
            return configured_league_id

        resolved = str(match["league_id"])
        if resolved != configured_league_id:
            logger.warning(
                f"League renewed: following {expected_name!r} from "
                f"{configured_league_id} ({current.get('season')}) to "
                f"{resolved} ({season}). Update SLEEPER_LEAGUE_ID to make this "
                "permanent."
            )
        return resolved

    except Exception as e:
        logger.error(
            f"League resolution failed ({e}); keeping {configured_league_id}",
            exc_info=True,
        )
        return configured_league_id

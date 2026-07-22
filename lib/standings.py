"""Shared standings computation.

Pulled out of the /standings command so other consumers (e.g. the
standings-aware auto-responses) can resolve a team's rank without
duplicating the Sleeper roster math.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from clients.sleeper import SleeperClient
from lib.members import get_member_registry

logger = logging.getLogger("dynasty_bot.standings")


@dataclass
class StandingsEntry:
    """A single team's position in the league standings."""

    rank: int
    owner_id: str
    owner: str
    roster_id: int
    wins: int
    losses: int
    ties: int
    pf: float
    pa: float


def ordinal(n: int) -> str:
    """Format an integer as an ordinal string, e.g. 1 -> '1st', 12 -> '12th'."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


async def compute_standings(
    sleeper: SleeperClient, league_id: str
) -> list[StandingsEntry]:
    """Fetch rosters and return standings sorted by wins, then points for.

    Returns:
        Standings best-to-worst, with `rank` set to each team's 1-indexed
        position (ties in wins/PF share adjacent ranks by list order).
    """
    rosters = await sleeper.get_rosters(league_id)
    users_list = await sleeper.get_users(league_id)
    users = {u["user_id"]: u.get("display_name", "Unknown") for u in users_list}

    entries = []
    for roster in rosters:
        owner_id = roster.get("owner_id", "")
        settings = roster.get("settings", {})
        entries.append(
            StandingsEntry(
                rank=0,
                owner_id=owner_id,
                owner=users.get(owner_id, f"Team {roster['roster_id']}"),
                roster_id=roster["roster_id"],
                wins=settings.get("wins", 0),
                losses=settings.get("losses", 0),
                ties=settings.get("ties", 0),
                pf=settings.get("fpts", 0) + settings.get("fpts_decimal", 0) / 100,
                pa=settings.get("fpts_against", 0)
                + settings.get("fpts_against_decimal", 0) / 100,
            )
        )

    entries.sort(key=lambda e: (e.wins, e.pf), reverse=True)
    for idx, entry in enumerate(entries, 1):
        entry.rank = idx

    return entries


async def get_rank_for_discord_id(
    sleeper: SleeperClient, league_id: str, discord_id: int | str
) -> Optional[int]:
    """Resolve a Discord user's current standings rank, if they're a known owner.

    Returns:
        1-indexed rank, or None if the Discord user isn't a registered league
        member or their Sleeper roster can't be matched.
    """
    member = get_member_registry().find_by_discord_id(discord_id)
    if not member or not member.sleeper_id:
        return None

    standings = await compute_standings(sleeper, league_id)
    for entry in standings:
        if entry.owner_id == member.sleeper_id:
            return entry.rank

    return None

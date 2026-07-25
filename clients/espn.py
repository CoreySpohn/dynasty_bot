"""ESPN scoreboard client, used for the one thing nflverse doesn't publish.

nflverse carries no preseason games, and a league deadline depends on them
(taxi squad decisions are due by the end of the first week of preseason). ESPN's
public scoreboard endpoint does carry them, needs no key, and is released once a
year with the rest of the schedule - so this is fetched annually by `/sync_nfl`,
not per command.

Deliberately narrow: preseason dates only. Everything else the bot needs about
the NFL calendar comes from nflverse via `clients/nfl_schedule.py`, and
everything about the league comes from Sleeper.
"""

import logging
from typing import Any, Optional

import aiohttp

from lib.nfl_calendar import (
    ESPN_SEASON_TYPE_PRESEASON,
    PRESEASON_WEEKS,
    PreseasonWeek,
    parse_preseason_weeks,
)

logger = logging.getLogger("dynasty_bot.espn")

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)


class ESPNClient:
    """Async client for ESPN's public NFL scoreboard.

    Args:
        session: An aiohttp.ClientSession for making HTTP requests.

    Example:
        async with aiohttp.ClientSession() as session:
            weeks = await ESPNClient(session).get_preseason_weeks(2026)
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def _scoreboard(
        self, season: int, season_type: int, week: int
    ) -> Optional[dict[str, Any]]:
        """Fetch one scoreboard page, or None if the request fails.

        A single failed week is not worth failing the whole sync over - the
        caller can still derive a deadline from the weeks that did come back,
        and will report a missing one honestly.
        """
        params = {
            "dates": str(season),
            "seasontype": str(season_type),
            "week": str(week),
        }
        try:
            async with self.session.get(SCOREBOARD_URL, params=params) as response:
                response.raise_for_status()
                return await response.json()
        except (aiohttp.ClientError, ValueError) as e:
            logger.warning(
                f"ESPN scoreboard failed for {season} type={season_type} "
                f"week={week}: {e}"
            )
            return None

    async def get_preseason_weeks(self, season: int) -> list[PreseasonWeek]:
        """Every published preseason week for a season, in order.

        Args:
            season: NFL season year (e.g. 2026).

        Returns:
            Parsed weeks, empty if the preseason schedule isn't out yet.
            Callers must treat empty as "unknown", never "no preseason".
        """
        payloads = []
        for week in PRESEASON_WEEKS:
            payload = await self._scoreboard(
                season, ESPN_SEASON_TYPE_PRESEASON, week
            )
            if payload:
                payloads.append(payload)

        weeks = parse_preseason_weeks(payloads)
        logger.info(
            f"Fetched {len(weeks)} preseason week(s) for {season} from ESPN"
        )
        return weeks

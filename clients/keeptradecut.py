"""KeepTradeCut Client for Dynasty Bot.

KeepTradeCut has no official API. Player dynasty trade values ship
embedded in the dynasty-rankings page as a JS array (`var playersArray =
[...]`), so we fetch that page and parse the array directly instead of
scraping the rendered HTML.
"""

import json
import logging
import re
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("dynasty_bot.keeptradecut")

RANKINGS_URL = "https://keeptradecut.com/dynasty-rankings"

# The page embeds all currently-ranked players (~500) in one JS array.
PLAYERS_ARRAY_RE = re.compile(r"var playersArray = (\[.*?\]);", re.DOTALL)

# KTC returns a non-HTML error page for requests without a browser-like UA.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


class KeepTradeCutClient:
    """Async client for fetching dynasty player trade values from KeepTradeCut.

    Args:
        session: An aiohttp.ClientSession for making HTTP requests.
    """

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_player_values(self) -> list[dict[str, Any]]:
        """Fetch current dynasty trade values for all ranked players.

        Returns:
            List of dicts with ktc_id, name, position, team, age, rookie,
            value_1qb, rank_1qb, positional_rank_1qb, value_sf, rank_sf,
            positional_rank_sf. Empty list on any fetch/parse failure.
        """
        html = await self._fetch_page()
        if html is None:
            return []

        match = PLAYERS_ARRAY_RE.search(html)
        if not match:
            logger.error("Could not find playersArray in KeepTradeCut page")
            return []

        try:
            raw_players = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse KeepTradeCut player data: {e}")
            return []

        return [self._parse_player(p) for p in raw_players]

    async def _fetch_page(self) -> Optional[str]:
        try:
            async with self.session.get(
                RANKINGS_URL, headers=REQUEST_HEADERS
            ) as resp:
                if resp.status != 200:
                    logger.error(f"KeepTradeCut returned status {resp.status}")
                    return None
                return await resp.text()
        except Exception as e:
            logger.error(f"Failed to fetch KeepTradeCut rankings: {e}")
            return None

    def _parse_player(self, p: dict[str, Any]) -> dict[str, Any]:
        oneqb = p.get("oneQBValues") or {}
        sf = p.get("superflexValues") or {}
        return {
            "ktc_id": p.get("playerID"),
            "name": p.get("playerName"),
            "position": p.get("position"),
            "team": p.get("team"),
            "age": p.get("age"),
            "rookie": bool(p.get("rookie", False)),
            "value_1qb": oneqb.get("value"),
            "rank_1qb": oneqb.get("rank"),
            "positional_rank_1qb": oneqb.get("positionalRank"),
            "value_sf": sf.get("value"),
            "rank_sf": sf.get("rank"),
            "positional_rank_sf": sf.get("positionalRank"),
        }

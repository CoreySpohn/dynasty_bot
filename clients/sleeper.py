"""Sleeper API Client for Dynasty Bot.

Provides async methods for fetching league, roster, matchup, and player
data from the Sleeper Fantasy Football API.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp

from config import SLEEPER_API_BASE_URL

logger = logging.getLogger("dynasty_bot.sleeper")

# Cache settings
PLAYERS_CACHE_PATH = Path(__file__).parent.parent / "data" / "players.json"
PLAYERS_CACHE_MAX_AGE = timedelta(hours=24)


class SleeperClient:
    """Async client for the Sleeper Fantasy Football API.
    
    All methods are non-blocking and use aiohttp for HTTP requests.
    The players data is cached locally to avoid repeated 5MB+ downloads.
    
    Args:
        session: An aiohttp.ClientSession for making HTTP requests.
    
    Example:
        async with aiohttp.ClientSession() as session:
            client = SleeperClient(session)
            league = await client.get_league("123456789")
    """
    
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base_url = SLEEPER_API_BASE_URL
    
    async def _get(self, endpoint: str) -> Any:
        """Make a GET request to the Sleeper API.
        
        Args:
            endpoint: API endpoint path (without base URL).
            
        Returns:
            Parsed JSON response.
            
        Raises:
            aiohttp.ClientError: If the request fails.
        """
        url = f"{self.base_url}{endpoint}"
        logger.debug(f"GET {url}")
        
        async with self.session.get(url) as response:
            response.raise_for_status()
            return await response.json()
    
    async def get_nfl_state(self) -> dict[str, Any]:
        """Get the current NFL state (week, season, season_type).

        Returns:
            State data including `week` and `season_type`
            ("pre" | "regular" | "post" | "off").
        """
        return await self._get("/state/nfl")

    async def get_league(self, league_id: str) -> dict[str, Any]:
        """Get league settings and metadata.
        
        Args:
            league_id: The Sleeper league ID.
            
        Returns:
            League data including name, settings, roster positions, etc.
        """
        return await self._get(f"/league/{league_id}")
    
    async def get_rosters(self, league_id: str) -> list[dict[str, Any]]:
        """Get all rosters in a league.
        
        Args:
            league_id: The Sleeper league ID.
            
        Returns:
            List of roster objects containing starters, players, 
            taxi squad, reserve (IR), and owner_id.
        """
        return await self._get(f"/league/{league_id}/rosters")
    
    async def get_users(self, league_id: str) -> list[dict[str, Any]]:
        """Get all users in a league.
        
        Args:
            league_id: The Sleeper league ID.
            
        Returns:
            List of user objects with user_id, display_name, avatar, etc.
        """
        return await self._get(f"/league/{league_id}/users")
    
    async def get_matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        """Get matchups for a specific week.
        
        Args:
            league_id: The Sleeper league ID.
            week: The NFL week number (1-18).
            
        Returns:
            List of matchup objects. Rosters with the same matchup_id
            are playing against each other.
        """
        return await self._get(f"/league/{league_id}/matchups/{week}")
    
    async def get_winners_bracket(self, league_id: str) -> list[dict[str, Any]]:
        """Get the playoff winners bracket for a league.

        This is the authoritative source for who actually won a season -
        better than inferring it from a championship-week matchup_id.

        Args:
            league_id: The Sleeper league ID.

        Returns:
            List of bracket match objects. Each has `r` (round), `m` (match
            id), `t1`/`t2` (roster ids, or a {"w"/"l": match_id} reference
            before that feeder match resolves), and `w`/`l` (winner and
            loser roster ids) once played.
        """
        return await self._get(f"/league/{league_id}/winners_bracket")

    async def get_drafts(self, league_id: str) -> list[dict[str, Any]]:
        """Get all drafts for a league.
        
        Args:
            league_id: The Sleeper league ID.
            
        Returns:
            List of draft objects with draft_id, status, settings, etc.
        """
        return await self._get(f"/league/{league_id}/drafts")
    
    async def get_picks_in_draft(self, draft_id: str) -> list[dict[str, Any]]:
        """Get all picks for a specific draft.
        
        Args:
            draft_id: The Sleeper draft ID.
            
        Returns:
            List of pick objects in order, containing player_id,
            picked_by (user_id), round, pick_no, etc.
        """
        return await self._get(f"/draft/{draft_id}/picks")
    
    async def get_traded_picks(self, league_id: str) -> list[dict[str, Any]]:
        """Get all traded draft picks for a league.
        
        Args:
            league_id: The Sleeper league ID.
            
        Returns:
            List of traded pick objects showing future pick ownership.
        """
        return await self._get(f"/league/{league_id}/traded_picks")
    
    async def get_transactions(
        self, league_id: str, week: int
    ) -> list[dict[str, Any]]:
        """Get all transactions for a specific week.
        
        Args:
            league_id: The Sleeper league ID.
            week: The NFL week number.
            
        Returns:
            List of transaction objects (trades, waivers, free agents).
        """
        return await self._get(f"/league/{league_id}/transactions/{week}")
    
    async def get_all_players(self) -> dict[str, dict[str, Any]]:
        """Get all NFL players with caching.
        
        This endpoint returns 5MB+ of data, so we cache it locally
        and only refresh if the cache is older than 24 hours.
        
        Returns:
            Dictionary mapping player_id to player data including
            name, position, team, injury status, etc.
        """
        # Check if cache exists and is fresh
        if self._is_cache_valid():
            logger.info("Loading players from cache")
            return self._load_players_cache()
        
        # Fetch fresh data from API
        logger.info("Fetching players from Sleeper API (this may take a moment)...")
        players = await self._get("/players/nfl")
        
        # Save to cache
        self._save_players_cache(players)
        logger.info(f"Cached {len(players)} players to {PLAYERS_CACHE_PATH}")
        
        return players
    
    def _is_cache_valid(self) -> bool:
        """Check if the players cache exists and is less than 24 hours old."""
        if not PLAYERS_CACHE_PATH.exists():
            return False
        
        cache_mtime = datetime.fromtimestamp(PLAYERS_CACHE_PATH.stat().st_mtime)
        cache_age = datetime.now() - cache_mtime
        
        if cache_age > PLAYERS_CACHE_MAX_AGE:
            logger.info(f"Players cache is {cache_age} old, refreshing...")
            return False
        
        logger.debug(f"Players cache is {cache_age} old, still valid")
        return True
    
    def _load_players_cache(self) -> dict[str, dict[str, Any]]:
        """Load players data from local cache file."""
        with open(PLAYERS_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    
    def _save_players_cache(self, players: dict[str, dict[str, Any]]) -> None:
        """Save players data to local cache file."""
        # Ensure data directory exists
        PLAYERS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        with open(PLAYERS_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(players, f)

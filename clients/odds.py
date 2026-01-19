"""The Odds API Client for NFL betting lines.

Fetches NFL spreads and game information from The Odds API.
Free tier: 500 requests/month.
"""

import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    pass

logger = logging.getLogger("dynasty_bot.odds")

# API configuration
BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "americanfootball_nfl"


class OddsClient:
    """Client for fetching NFL betting odds."""
    
    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session
    
    async def get_nfl_spreads(
        self,
        regions: str = "us",
        odds_format: str = "american"
    ) -> list[dict]:
        """Fetch current NFL game spreads.
        
        Args:
            regions: Bookmaker regions (us, uk, eu, au)
            odds_format: american or decimal
            
        Returns:
            List of games with spread information.
        """
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": "spreads",
            "oddsFormat": odds_format,
        }
        
        url = f"{BASE_URL}/sports/{SPORT_KEY}/odds"
        
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status == 401:
                    logger.error("Invalid Odds API key")
                    return []
                
                if resp.status == 422:
                    logger.warning("No NFL games currently available")
                    return []
                
                if resp.status != 200:
                    logger.error(f"Odds API error: {resp.status}")
                    return []
                
                # Log remaining requests
                remaining = resp.headers.get("x-requests-remaining", "?")
                logger.info(f"Odds API requests remaining: {remaining}")
                
                data = await resp.json()
                return self._parse_spreads(data)
                
        except Exception as e:
            logger.error(f"Failed to fetch odds: {e}")
            return []
    
    def _parse_spreads(self, raw_data: list) -> list[dict]:
        """Parse raw API response into simplified game data.
        
        Returns:
            List of dicts with:
            - game_id: Unique game identifier
            - home_team: Home team name
            - away_team: Away team name
            - spread: Home team spread (negative = favorite)
            - kickoff: Game start time (ISO format)
        """
        games = []
        
        for game in raw_data:
            game_id = game.get("id")
            home_team = game.get("home_team")
            away_team = game.get("away_team")
            commence_time = game.get("commence_time")
            
            # Get spread from first available bookmaker
            spread = None
            bookmakers = game.get("bookmakers", [])
            
            for book in bookmakers:
                markets = book.get("markets", [])
                for market in markets:
                    if market.get("key") == "spreads":
                        outcomes = market.get("outcomes", [])
                        for outcome in outcomes:
                            if outcome.get("name") == home_team:
                                spread = outcome.get("point")
                                break
                        if spread is not None:
                            break
                if spread is not None:
                    break
            
            if spread is not None:
                games.append({
                    "game_id": game_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "spread": float(spread),
                    "kickoff": commence_time,
                })
        
        return games
    
    async def get_scores(self, days_from: int = 3) -> list[dict]:
        """Fetch recent NFL game scores.
        
        Args:
            days_from: How many days back to fetch scores.
            
        Returns:
            List of games with scores.
        """
        params = {
            "apiKey": self.api_key,
            "daysFrom": days_from,
        }
        
        url = f"{BASE_URL}/sports/{SPORT_KEY}/scores"
        
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"Odds API scores error: {resp.status}")
                    return []
                
                data = await resp.json()
                return self._parse_scores(data)
                
        except Exception as e:
            logger.error(f"Failed to fetch scores: {e}")
            return []
    
    def _parse_scores(self, raw_data: list) -> list[dict]:
        """Parse raw scores response."""
        games = []
        
        for game in raw_data:
            if not game.get("completed"):
                continue
                
            scores = game.get("scores", [])
            home_score = None
            away_score = None
            
            for score in scores:
                if score.get("name") == game.get("home_team"):
                    home_score = int(score.get("score", 0))
                elif score.get("name") == game.get("away_team"):
                    away_score = int(score.get("score", 0))
            
            if home_score is not None and away_score is not None:
                games.append({
                    "game_id": game.get("id"),
                    "home_team": game.get("home_team"),
                    "away_team": game.get("away_team"),
                    "home_score": home_score,
                    "away_score": away_score,
                    "completed": True,
                })
        
        return games
    
    async def get_totals(self, regions: str = "us") -> list[dict]:
        """Fetch game totals (over/under) for NFL games.
        
        Returns:
            List of dicts with game_id, total, home_team, away_team
        """
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": "totals",
            "oddsFormat": "american",
        }
        
        url = f"{BASE_URL}/sports/{SPORT_KEY}/odds"
        
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.error(f"Odds API totals error: {resp.status}")
                    return []
                
                remaining = resp.headers.get("x-requests-remaining", "?")
                logger.info(f"Odds API requests remaining: {remaining}")
                
                data = await resp.json()
                return self._parse_totals(data)
                
        except Exception as e:
            logger.error(f"Failed to fetch totals: {e}")
            return []
    
    def _parse_totals(self, raw_data: list) -> list[dict]:
        """Parse totals from API response."""
        results = []
        
        for game in raw_data:
            game_id = game.get("id")
            home_team = game.get("home_team")
            away_team = game.get("away_team")
            
            bookmakers = game.get("bookmakers", [])
            for book in bookmakers:
                markets = book.get("markets", [])
                for market in markets:
                    if market.get("key") == "totals":
                        outcomes = market.get("outcomes", [])
                        for outcome in outcomes:
                            if outcome.get("name") == "Over":
                                results.append({
                                    "game_id": game_id,
                                    "home_team": home_team,
                                    "away_team": away_team,
                                    "total": outcome.get("point"),
                                })
                                break
                        break
                break
        
        return results
    
    async def get_player_props(
        self,
        event_id: str,
        markets: list[str] = None,
        regions: str = "us"
    ) -> list[dict]:
        """Fetch player props for a specific game.
        
        Args:
            event_id: The game ID from get_nfl_spreads
            markets: List of prop markets (default: pass_yds, rush_yds)
            
        Returns:
            List of player prop dicts
        """
        if markets is None:
            markets = ["player_pass_yds", "player_rush_yds"]
        
        params = {
            "apiKey": self.api_key,
            "regions": regions,
            "markets": ",".join(markets),
            "oddsFormat": "american",
        }
        
        url = f"{BASE_URL}/sports/{SPORT_KEY}/events/{event_id}/odds"
        
        try:
            async with self.session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(f"No props for event {event_id}: {resp.status}")
                    return []
                
                remaining = resp.headers.get("x-requests-remaining", "?")
                logger.info(f"Odds API requests remaining: {remaining}")
                
                data = await resp.json()
                return self._parse_player_props(data, event_id)
                
        except Exception as e:
            logger.error(f"Failed to fetch player props: {e}")
            return []
    
    def _parse_player_props(self, data: dict, game_id: str) -> list[dict]:
        """Parse player props from API response.
        
        Returns top 2 players per market (e.g., 2 QBs for pass yards).
        """
        props = []
        seen_players = {}  # market -> set of player names
        
        bookmakers = data.get("bookmakers", [])
        for book in bookmakers:
            markets = book.get("markets", [])
            for market in markets:
                market_key = market.get("key")
                if market_key not in seen_players:
                    seen_players[market_key] = set()
                
                outcomes = market.get("outcomes", [])
                for outcome in outcomes:
                    player_name = outcome.get("description") or outcome.get("name")
                    point = outcome.get("point")
                    outcome_name = outcome.get("name")  # "Over" or "Under"
                    price = outcome.get("price", -110)
                    
                    # Skip if we already have 4 entries for this market (2 players x over/under)
                    if len(seen_players[market_key]) >= 2 and player_name not in seen_players[market_key]:
                        continue
                    
                    seen_players[market_key].add(player_name)
                    
                    # Create human-readable description
                    market_label = {
                        "player_pass_yds": "Pass Yds",
                        "player_rush_yds": "Rush Yds",
                        "player_reception_yds": "Rec Yds",
                    }.get(market_key, market_key)
                    
                    props.append({
                        "game_id": game_id,
                        "market_key": market_key,
                        "player": player_name,
                        "line": point,
                        "outcome": outcome_name.lower(),  # "over" or "under"
                        "odds": price,
                        "description": f"{player_name} {outcome_name} {point} {market_label}",
                    })
            break  # Only use first bookmaker
        
        return props


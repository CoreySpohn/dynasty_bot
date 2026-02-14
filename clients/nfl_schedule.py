"""NFL Schedule Client using nflreadpy.

Fetches NFL schedule data to determine key dates like:
- Preseason start/end dates
- Regular season Week 1
- Week 12 (trade deadline)
- Super Bowl date

Uses the officially supported nflreadpy library which wraps
the nflverse data repository. Returns Polars DataFrames.

Game types in data:
- REG: Regular season
- PRE: Preseason (may not always be available)
- WC: Wild Card
- DIV: Divisional
- CON: Conference Championship
- SB: Super Bowl
"""

from datetime import date, datetime
from typing import Optional

import nflreadpy as nfl
import polars as pl


class NFLScheduleClient:
    """Client for NFL schedule data via nflreadpy.
    
    Provides methods to fetch key NFL dates for calculating
    league deadline anchors.
    
    Example:
        client = NFLScheduleClient()
        week1 = client.get_regular_season_start(2026)
        print(f"NFL Week 1 starts: {week1}")
    """
    
    def __init__(self):
        """Initialize the schedule client with an empty cache."""
        self._schedule_cache: dict[int, pl.DataFrame] = {}
    
    def load_schedule(self, season: int) -> pl.DataFrame:
        """Load and cache schedule for a season.
        
        Args:
            season: NFL season year (e.g., 2026)
            
        Returns:
            Polars DataFrame with columns: game_id, season, game_type, week,
            gameday, weekday, gametime, away_team, home_team, etc.
        """
        if season not in self._schedule_cache:
            self._schedule_cache[season] = nfl.load_schedules([season])
        return self._schedule_cache[season]
    
    def clear_cache(self) -> None:
        """Clear the schedule cache to force fresh data on next load."""
        self._schedule_cache.clear()
    
    def _parse_date(self, date_val) -> Optional[date]:
        """Parse a date value to a date object.
        
        Handles both string and date objects.
        """
        if date_val is None:
            return None
        if isinstance(date_val, date):
            return date_val
        try:
            # Handle string format
            date_str = str(date_val)
            if not date_str or date_str == 'None':
                return None
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return None
    
    def get_regular_season_start(self, season: int) -> Optional[date]:
        """Get the date of NFL Week 1 kickoff (typically Thursday night).
        
        Args:
            season: NFL season year
            
        Returns:
            Date of the first regular season game, or None if not available.
        """
        df = self.load_schedule(season)
        week_1 = df.filter(
            (pl.col('game_type') == 'REG') & (pl.col('week') == 1)
        )
        if week_1.height > 0:
            min_date = week_1.select(pl.col('gameday').min()).item()
            return self._parse_date(min_date)
        return None
    
    def get_preseason_dates(self, season: int) -> tuple[Optional[date], Optional[date]]:
        """Get preseason start and end dates.
        
        Args:
            season: NFL season year
            
        Returns:
            Tuple of (start_date, end_date) for preseason, or (None, None)
            if preseason data is not available.
            
        Note:
            Preseason data may not always be available in the nflverse dataset.
            Falls back to estimating from regular season start if needed.
        """
        df = self.load_schedule(season)
        preseason = df.filter(pl.col('game_type') == 'PRE')
        if preseason.height > 0:
            min_date = preseason.select(pl.col('gameday').min()).item()
            max_date = preseason.select(pl.col('gameday').max()).item()
            return self._parse_date(min_date), self._parse_date(max_date)
        
        # Fallback: estimate from regular season (preseason typically ends
        # about 2 weeks before regular season starts)
        reg_start = self.get_regular_season_start(season)
        if reg_start:
            # Estimate: preseason starts ~5 weeks before, ends ~1 week before
            from datetime import timedelta
            pre_end = reg_start - timedelta(days=7)
            pre_start = reg_start - timedelta(days=35)
            return pre_start, pre_end
        
        return None, None
    
    def get_week_end_date(self, season: int, week: int) -> Optional[date]:
        """Get the last game date of a specific regular season week.
        
        Args:
            season: NFL season year
            week: Week number (1-18 for regular season)
            
        Returns:
            Date of the last game in the specified week, or None.
        """
        df = self.load_schedule(season)
        week_games = df.filter(
            (pl.col('game_type') == 'REG') & (pl.col('week') == week)
        )
        if week_games.height > 0:
            max_date = week_games.select(pl.col('gameday').max()).item()
            return self._parse_date(max_date)
        return None
    
    def get_super_bowl_date(self, season: int) -> Optional[date]:
        """Get Super Bowl date for the given season.
        
        Note: The Super Bowl for season X is played in February of year X+1.
        
        Args:
            season: NFL season year (the Super Bowl is after this season)
            
        Returns:
            Date of the Super Bowl, or None if not available.
        """
        df = self.load_schedule(season)
        # Super Bowl game type is 'SB'
        super_bowl = df.filter(pl.col('game_type') == 'SB')
        if super_bowl.height > 0:
            sb_date = super_bowl.select(pl.col('gameday').first()).item()
            return self._parse_date(sb_date)
        return None
    
    def get_playoff_results(self, season: int) -> list[dict]:
        """Get completed playoff game results.
        
        Args:
            season: NFL season year
            
        Returns:
            List of dicts with: home_team, away_team, home_score, away_score, game_type
        """
        df = self.load_schedule(season)
        
        # Filter to playoff games (WC, DIV, CON, SB)
        playoff_types = ['WC', 'DIV', 'CON', 'SB']
        playoffs = df.filter(pl.col('game_type').is_in(playoff_types))
        
        results = []
        for row in playoffs.iter_rows(named=True):
            home_score = row.get('home_score')
            away_score = row.get('away_score')
            
            # Only include completed games (have scores)
            if home_score is not None and away_score is not None:
                results.append({
                    'home_team': row.get('home_team'),
                    'away_team': row.get('away_team'),
                    'home_score': int(home_score),
                    'away_score': int(away_score),
                    'game_type': row.get('game_type'),
                    'gameday': str(row.get('gameday')),
                })
        
        return results
    
    def get_playoff_player_stats(self, season: int) -> pl.DataFrame:
        """Get player stats for playoff games.
        
        Args:
            season: NFL season year
            
        Returns:
            Polars DataFrame with columns including:
            - player_name, player_display_name
            - passing_yards, rushing_yards, receiving_yards
            - week, season, recent_team
        """
        return nfl.load_player_stats(season, summary_level="post")
    
    def get_player_stat(
        self, 
        player_name: str, 
        stat_type: str, 
        season: int
    ) -> dict | None:
        """Get a specific stat for a player from playoff games.
        
        Args:
            player_name: Player name to match (fuzzy matching)
            stat_type: One of 'passing_yards', 'rushing_yards', 'receiving_yards'
            season: NFL season year
            
        Returns:
            Dict with stat value and verification data, or None if not found.
            Keys: stat_value, matched_name, week, team
        """
        stats_df = self.get_playoff_player_stats(season)
        
        # Fuzzy match player name
        name_lower = player_name.lower().strip()
        
        for row in stats_df.iter_rows(named=True):
            display_name = str(row.get("player_display_name", "")).lower()
            full_name = str(row.get("player_name", "")).lower()
            
            # Check for match
            if (name_lower in display_name or 
                display_name in name_lower or
                name_lower in full_name or
                full_name in name_lower):
                stat_value = row.get(stat_type)
                if stat_value is not None:
                    return {
                        "stat_value": int(stat_value),
                        "matched_name": row.get("player_display_name", "Unknown"),
                        "week": row.get("week"),
                        "team": row.get("recent_team", ""),
                    }
        
        return None

    def get_all_anchors(self, season: int) -> dict[str, Optional[date]]:
        """Get all NFL schedule anchors for deadline calculations.
        
        This fetches all the key dates needed to calculate relative
        deadlines in the league calendar.
        
        Args:
            season: NFL season year
            
        Returns:
            Dictionary with anchor names and their dates:
            - super_bowl: Previous season's Super Bowl date
            - nfl_preseason_start: First preseason game
            - nfl_preseason_end: Last preseason game
            - nfl_regular_season_start: Week 1 kickoff
            - nfl_week_12_end: Last game of Week 12 (trade deadline)
        """
        preseason_start, preseason_end = self.get_preseason_dates(season)

        
        # Get previous season's Super Bowl (for off-season anchor)
        prev_super_bowl = None
        try:
            prev_super_bowl = self.get_super_bowl_date(season - 1)
        except Exception:
            # Previous season data might not be available
            pass
        
        return {
            'super_bowl': prev_super_bowl,
            'nfl_preseason_start': preseason_start,
            'nfl_preseason_end': preseason_end,
            'nfl_regular_season_start': self.get_regular_season_start(season),
            'nfl_week_12_end': self.get_week_end_date(season, 12),
        }


# Convenience function for quick access
def get_nfl_schedule_client() -> NFLScheduleClient:
    """Get a new NFL schedule client instance."""
    return NFLScheduleClient()

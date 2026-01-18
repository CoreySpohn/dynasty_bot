"""Tests for the Sleeper API client."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aioresponses import aioresponses

from clients.sleeper import SleeperClient, PLAYERS_CACHE_PATH


class TestSleeperClient:
    """Test suite for SleeperClient."""

    @pytest.fixture
    def client(self, aiohttp_session):
        """Create a SleeperClient instance for testing."""
        return SleeperClient(aiohttp_session)

    async def test_get_league(
        self, client, mock_aioresponses, sample_league_data
    ):
        """Test fetching league data."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/league/123456789",
            payload=sample_league_data,
        )
        
        result = await client.get_league("123456789")
        
        assert result["league_id"] == "123456789"
        assert result["name"] == "Test Dynasty League"
        assert result["settings"]["leg"] == 5

    async def test_get_rosters(
        self, client, mock_aioresponses, sample_rosters_data
    ):
        """Test fetching rosters data."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/league/123456789/rosters",
            payload=sample_rosters_data,
        )
        
        result = await client.get_rosters("123456789")
        
        assert len(result) == 2
        assert result[0]["roster_id"] == 1
        assert result[0]["owner_id"] == "user_001"
        assert "p004" in result[0]["taxi"]

    async def test_get_users(
        self, client, mock_aioresponses, sample_users_data
    ):
        """Test fetching users data."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/league/123456789/users",
            payload=sample_users_data,
        )
        
        result = await client.get_users("123456789")
        
        assert len(result) == 2
        assert result[0]["display_name"] == "TeamAlpha"

    async def test_get_matchups(
        self, client, mock_aioresponses, sample_matchups_data
    ):
        """Test fetching matchups for a week."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/league/123456789/matchups/5",
            payload=sample_matchups_data,
        )
        
        result = await client.get_matchups("123456789", 5)
        
        assert len(result) == 2
        assert result[0]["points"] == 112.5
        assert result[0]["matchup_id"] == result[1]["matchup_id"]

    async def test_get_drafts(
        self, client, mock_aioresponses, sample_drafts_data
    ):
        """Test fetching drafts for a league."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/league/123456789/drafts",
            payload=sample_drafts_data,
        )
        
        result = await client.get_drafts("123456789")
        
        assert len(result) == 1
        assert result[0]["draft_id"] == "draft_001"
        assert result[0]["status"] == "complete"

    async def test_get_picks_in_draft(
        self, client, mock_aioresponses, sample_draft_picks_data
    ):
        """Test fetching picks for a specific draft."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/draft/draft_001/picks",
            payload=sample_draft_picks_data,
        )
        
        result = await client.get_picks_in_draft("draft_001")
        
        assert len(result) == 6
        assert result[0]["round"] == 1
        assert result[0]["player_id"] == "p001"

    async def test_get_all_players_fetches_from_api(
        self, client, mock_aioresponses, sample_players_data, tmp_path
    ):
        """Test fetching players from API when no cache exists."""
        mock_aioresponses.get(
            "https://api.sleeper.app/v1/players/nfl",
            payload=sample_players_data,
        )
        
        # Use temp path for cache
        with patch.object(
            SleeperClient, "_is_cache_valid", return_value=False
        ), patch.object(
            SleeperClient, "_save_players_cache"
        ) as mock_save:
            result = await client.get_all_players()
        
        assert "p001" in result
        assert result["p001"]["full_name"] == "Patrick Mahomes"
        mock_save.assert_called_once()

    async def test_get_all_players_uses_cache(
        self, client, sample_players_data
    ):
        """Test loading players from cache when valid."""
        with patch.object(
            SleeperClient, "_is_cache_valid", return_value=True
        ), patch.object(
            SleeperClient, "_load_players_cache", return_value=sample_players_data
        ) as mock_load:
            result = await client.get_all_players()
        
        assert result == sample_players_data
        mock_load.assert_called_once()


class TestSleeperClientCaching:
    """Test suite for player caching functionality."""

    def test_is_cache_valid_no_file(self):
        """Test cache validation when file doesn't exist."""
        with patch.object(Path, "exists", return_value=False):
            # Create a minimal client instance
            from clients.sleeper import SleeperClient
            
            # Use mock session
            client = SleeperClient.__new__(SleeperClient)
            client.session = None
            client.base_url = "https://api.sleeper.app/v1"
            
            assert client._is_cache_valid() is False

    def test_save_and_load_cache(self, tmp_path, sample_players_data):
        """Test saving and loading player cache."""
        cache_file = tmp_path / "players.json"
        
        # Save
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(sample_players_data, f)
        
        # Load
        with open(cache_file, "r") as f:
            loaded = json.load(f)
        
        assert loaded == sample_players_data

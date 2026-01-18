"""Shared test fixtures for Dynasty Bot tests."""

import sys
from pathlib import Path

# Add project root to Python path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import asyncio
import os
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio
from aioresponses import aioresponses

# Set test environment variables before importing config
os.environ["DISCORD_TOKEN"] = "test_token_12345"
os.environ["SLEEPER_LEAGUE_ID"] = "123456789"
os.environ["DATABASE_PATH"] = ":memory:"
os.environ["ALERT_CHANNEL_ID"] = "987654321"


@pytest.fixture
def mock_env(monkeypatch):
    """Provide test environment variables."""
    monkeypatch.setenv("DISCORD_TOKEN", "test_token_12345")
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "123456789")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("ALERT_CHANNEL_ID", "987654321")
    return {
        "DISCORD_TOKEN": "test_token_12345",
        "SLEEPER_LEAGUE_ID": "123456789",
        "DATABASE_PATH": ":memory:",
        "ALERT_CHANNEL_ID": "987654321",
    }


@pytest_asyncio.fixture
async def aiohttp_session() -> AsyncGenerator[aiohttp.ClientSession, None]:
    """Provide an aiohttp session for testing."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def mock_aioresponses():
    """Provide aioresponses mock for HTTP requests."""
    with aioresponses() as m:
        yield m


@pytest.fixture
def sample_league_data() -> dict:
    """Sample league data from Sleeper API."""
    return {
        "league_id": "123456789",
        "name": "Test Dynasty League",
        "season": "2025",
        "previous_league_id": "111111111",
        "settings": {
            "leg": 5,  # Current week
            "playoff_week_start": 15,
            "taxi_slots": 3,
        },
        "roster_positions": [
            "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX",
            "SUPER_FLEX", "K", "DEF", "BN", "BN", "BN", "BN", "BN", "IR"
        ],
        "scoring_settings": {
            "pass_yd": 0.04,
            "pass_td": 4,
            "rush_yd": 0.1,
            "rush_td": 6,
            "rec": 1,
            "rec_yd": 0.1,
            "rec_td": 6,
        },
    }


@pytest.fixture
def sample_rosters_data() -> list[dict]:
    """Sample rosters data from Sleeper API."""
    return [
        {
            "roster_id": 1,
            "owner_id": "user_001",
            "players": ["p001", "p002", "p003", "p004", "p005"],
            "starters": ["p001", "p002", "p003"],
            "taxi": ["p004"],
            "reserve": ["p005"],
            "settings": {
                "wins": 3,
                "losses": 2,
                "ties": 0,
                "fpts": 523,
                "fpts_decimal": 45,
                "fpts_against": 498,
                "fpts_against_decimal": 20,
            },
        },
        {
            "roster_id": 2,
            "owner_id": "user_002",
            "players": ["p010", "p011", "p012", "p013"],
            "starters": ["p010", "p011", "p012"],
            "taxi": ["p013"],
            "reserve": [],
            "settings": {
                "wins": 2,
                "losses": 3,
                "ties": 0,
                "fpts": 489,
                "fpts_decimal": 10,
                "fpts_against": 512,
                "fpts_against_decimal": 30,
            },
        },
    ]


@pytest.fixture
def sample_users_data() -> list[dict]:
    """Sample users data from Sleeper API."""
    return [
        {
            "user_id": "user_001",
            "display_name": "TeamAlpha",
            "avatar": "abc123",
        },
        {
            "user_id": "user_002",
            "display_name": "TeamBeta",
            "avatar": "def456",
        },
    ]


@pytest.fixture
def sample_matchups_data() -> list[dict]:
    """Sample matchups data from Sleeper API."""
    return [
        {
            "roster_id": 1,
            "matchup_id": 1,
            "points": 112.5,
            "starters": ["p001", "p002", "p003"],
            "players": ["p001", "p002", "p003", "p004", "p005"],
            "players_points": {
                "p001": 25.5,
                "p002": 18.2,
                "p003": 22.1,
                "p004": 15.0,
                "p005": 0.0,
            },
        },
        {
            "roster_id": 2,
            "matchup_id": 1,
            "points": 98.3,
            "starters": ["p010", "p011", "p012"],
            "players": ["p010", "p011", "p012", "p013"],
            "players_points": {
                "p010": 28.1,
                "p011": 12.4,
                "p012": 19.8,
                "p013": 8.5,
            },
        },
    ]


@pytest.fixture
def sample_players_data() -> dict[str, dict]:
    """Sample players data from Sleeper API."""
    return {
        "p001": {
            "player_id": "p001",
            "first_name": "Patrick",
            "last_name": "Mahomes",
            "full_name": "Patrick Mahomes",
            "position": "QB",
            "team": "KC",
            "injury_status": None,
        },
        "p002": {
            "player_id": "p002",
            "first_name": "Travis",
            "last_name": "Kelce",
            "full_name": "Travis Kelce",
            "position": "TE",
            "team": "KC",
            "injury_status": None,
        },
        "p003": {
            "player_id": "p003",
            "first_name": "Tyreek",
            "last_name": "Hill",
            "full_name": "Tyreek Hill",
            "position": "WR",
            "team": "MIA",
            "injury_status": "Out",
        },
        "p004": {
            "player_id": "p004",
            "first_name": "Rashee",
            "last_name": "Rice",
            "full_name": "Rashee Rice",
            "position": "WR",
            "team": "KC",
            "injury_status": None,
        },
        "p005": {
            "player_id": "p005",
            "first_name": "Isiah",
            "last_name": "Pacheco",
            "full_name": "Isiah Pacheco",
            "position": "RB",
            "team": "KC",
            "injury_status": "IR",
        },
        "p010": {
            "player_id": "p010",
            "first_name": "Josh",
            "last_name": "Allen",
            "full_name": "Josh Allen",
            "position": "QB",
            "team": "BUF",
            "injury_status": None,
        },
        "p011": {
            "player_id": "p011",
            "first_name": "Stefon",
            "last_name": "Diggs",
            "full_name": "Stefon Diggs",
            "position": "WR",
            "team": "HOU",
            "injury_status": None,
        },
        "p012": {
            "player_id": "p012",
            "first_name": "James",
            "last_name": "Cook",
            "full_name": "James Cook",
            "position": "RB",
            "team": "BUF",
            "injury_status": None,
        },
        "p013": {
            "player_id": "p013",
            "first_name": "Dalton",
            "last_name": "Kincaid",
            "full_name": "Dalton Kincaid",
            "position": "TE",
            "team": "BUF",
            "injury_status": None,
        },
    }


@pytest.fixture
def sample_drafts_data() -> list[dict]:
    """Sample drafts data from Sleeper API."""
    return [
        {
            "draft_id": "draft_001",
            "league_id": "123456789",
            "season": "2025",
            "status": "complete",
            "type": "snake",
            "settings": {
                "rounds": 5,
                "slots_qb": 1,
            },
        },
    ]


@pytest.fixture
def sample_draft_picks_data() -> list[dict]:
    """Sample draft picks data from Sleeper API."""
    return [
        {"round": 1, "pick_no": 1, "player_id": "p001", "picked_by": "user_001"},
        {"round": 1, "pick_no": 2, "player_id": "p010", "picked_by": "user_002"},
        {"round": 2, "pick_no": 3, "player_id": "p002", "picked_by": "user_002"},
        {"round": 2, "pick_no": 4, "player_id": "p011", "picked_by": "user_001"},
        {"round": 3, "pick_no": 5, "player_id": "p003", "picked_by": "user_001"},
        {"round": 3, "pick_no": 6, "player_id": "p012", "picked_by": "user_002"},
    ]


@pytest.fixture
def mock_bot():
    """Create a mock DynastyBot for testing cogs."""
    bot = MagicMock()
    bot.sleeper = MagicMock()
    bot.league_id = "123456789"
    bot.loop = asyncio.get_event_loop()
    bot.wait_until_ready = AsyncMock()
    bot.get_channel = MagicMock(return_value=None)
    return bot

"""Tests for the database module."""

import pytest
import pytest_asyncio

from database import Database


class TestDatabase:
    """Test suite for Database class."""

    @pytest_asyncio.fixture
    async def db(self):
        """Create an in-memory database for testing."""
        database = Database(":memory:")
        await database.connect()
        yield database
        await database.close()

    async def test_connect_creates_tables(self, db):
        """Test that connecting initializes all required tables."""
        # Check that tables exist by querying sqlite_master
        async with db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            tables = await cursor.fetchall()
        
        table_names = {t[0] for t in tables}
        
        assert "raids" in table_names
        assert "player_history" in table_names
        assert "ice_chugs" in table_names
        assert "power_rankings" in table_names

    async def test_insert_and_query_raid(self, db):
        """Test inserting and querying a raid record."""
        # Insert a raid
        async with db.execute(
            """
            INSERT INTO raids (
                raider_user_id, raider_team_name, victim_user_id, victim_team_name,
                player_id, player_name, draft_round, cost_text, raid_date, week, season
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "user_001", "TeamAlpha", "user_002", "TeamBeta",
                "p004", "Rashee Rice", "3", "2nd & 3rd Round Picks",
                "2025-10-15", 5, 2025
            ),
        ):
            pass
        
        # Query the raid
        async with db.connection.execute(
            "SELECT player_name, cost_text FROM raids WHERE player_id = ?",
            ("p004",),
        ) as cursor:
            result = await cursor.fetchone()
        
        assert result[0] == "Rashee Rice"
        assert result[1] == "2nd & 3rd Round Picks"

    async def test_insert_power_ranking(self, db):
        """Test inserting a power ranking record."""
        async with db.execute(
            """
            INSERT INTO power_rankings (user_id, team_name, rank, score, week, season)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("user_001", "TeamAlpha", 1, 185.5, 5, 2025),
        ):
            pass
        
        async with db.connection.execute(
            "SELECT rank, score FROM power_rankings WHERE user_id = ?",
            ("user_001",),
        ) as cursor:
            result = await cursor.fetchone()
        
        assert result[0] == 1
        assert result[1] == 185.5

    async def test_insert_ice_chug(self, db):
        """Test inserting an ice chug punishment record."""
        async with db.execute(
            """
            INSERT INTO ice_chugs (user_id, team_name, reason, season, week)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("user_002", "TeamBeta", "Started injured player", 2025, 5),
        ):
            pass
        
        async with db.connection.execute(
            "SELECT reason, completed FROM ice_chugs WHERE user_id = ?",
            ("user_002",),
        ) as cursor:
            result = await cursor.fetchone()
        
        assert result[0] == "Started injured player"
        assert result[1] == 0  # False

    async def test_connection_property_raises_when_not_connected(self):
        """Test that accessing connection before connect() raises."""
        db = Database(":memory:")
        
        with pytest.raises(RuntimeError, match="Database not connected"):
            _ = db.connection

    async def test_close_sets_connection_to_none(self):
        """Test that closing the database clears the connection."""
        db = Database(":memory:")
        await db.connect()
        
        assert db._connection is not None
        
        await db.close()
        
        assert db._connection is None

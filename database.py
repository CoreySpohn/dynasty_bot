"""Database module for Dynasty Bot.

Provides async SQLite database connection management and schema setup
using aiosqlite for non-blocking database operations.
"""

import aiosqlite
from typing import Optional
from contextlib import asynccontextmanager

from config import DATABASE_PATH


class Database:
    """Async SQLite database manager for Dynasty Bot.
    
    Provides connection pooling and schema initialization for
    storing league data, raid history, and player analytics.
    """
    
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self._connection: Optional[aiosqlite.Connection] = None
    
    async def connect(self) -> None:
        """Establish database connection and initialize schema."""
        self._connection = await aiosqlite.connect(self.db_path)
        # Enable foreign keys for referential integrity
        await self._connection.execute("PRAGMA foreign_keys = ON")
        await self._init_schema()
    
    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None
    
    @property
    def connection(self) -> aiosqlite.Connection:
        """Get the active database connection."""
        if self._connection is None:
            raise RuntimeError(
                "Database not connected. Call connect() first."
            )
        return self._connection
    
    @asynccontextmanager
    async def execute(self, sql: str, parameters: tuple = ()):
        """Execute a SQL statement and yield the cursor."""
        async with self.connection.execute(sql, parameters) as cursor:
            yield cursor
        await self.connection.commit()
    
    async def _init_schema(self) -> None:
        """Initialize database tables if they don't exist."""
        # Raids table - tracks taxi squad raid history
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS raids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raider_user_id TEXT NOT NULL,
                raider_team_name TEXT NOT NULL,
                victim_user_id TEXT NOT NULL,
                victim_team_name TEXT NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                draft_round TEXT,
                cost_text TEXT NOT NULL,
                raid_date TEXT NOT NULL,
                week INTEGER NOT NULL,
                season INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Player history - tracks taxi squad player movements
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS player_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                squad_type TEXT NOT NULL CHECK(squad_type IN ('roster', 'taxi', 'ir')),
                added_date TEXT NOT NULL,
                removed_date TEXT,
                season INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Ice chug punishments - tracks punishment history
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS ice_chugs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                team_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                season INTEGER NOT NULL,
                week INTEGER,
                completed BOOLEAN DEFAULT FALSE,
                completed_date TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Power rankings history
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS power_rankings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                team_name TEXT NOT NULL,
                rank INTEGER NOT NULL,
                score REAL NOT NULL,
                week INTEGER NOT NULL,
                season INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await self.connection.commit()


# Global database instance
db = Database()

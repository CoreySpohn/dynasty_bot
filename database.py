"""Database module for Dynasty Bot.

Provides async SQLite database connection management and schema setup
using aiosqlite for non-blocking database operations.
"""

import logging
import aiosqlite
from typing import Optional
from contextlib import asynccontextmanager

from config import DATABASE_PATH

logger = logging.getLogger("dynasty_bot.database")


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
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Migration: Add status column if it doesn't exist (for older databases)
        try:
            await self.connection.execute("""
                ALTER TABLE raids ADD COLUMN status TEXT DEFAULT 'pending'
            """)
            logger.info("Added 'status' column to raids table")
        except Exception:
            pass  # Column already exists
        
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
        
        # Deadline completion tracking
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS deadline_completions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deadline_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                completed_by TEXT,
                notes TEXT,
                UNIQUE(deadline_id, season)
            )
        """)
        
        # Reminder history (avoid duplicate sends)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS reminder_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                deadline_id TEXT NOT NULL,
                season INTEGER NOT NULL,
                days_before INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                message_id TEXT,
                UNIQUE(deadline_id, season, days_before)
            )
        """)
        
        # =====================================================================
        # Kohl's Cash Tables
        # =====================================================================
        
        # Kohl's Cash balances
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_balances (
                owner_id TEXT PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                season INTEGER NOT NULL,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Playoff games with betting lines
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_games (
                game_id TEXT PRIMARY KEY,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                spread REAL,
                kickoff TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                home_score INTEGER,
                away_score INTEGER,
                thread_id TEXT,
                season INTEGER NOT NULL
            )
        """)
        
        # Bets placed
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                game_id TEXT NOT NULL,
                pick TEXT NOT NULL,
                amount INTEGER NOT NULL,
                result TEXT DEFAULT 'pending',
                payout INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES kohls_games(game_id)
            )
        """)
        
        # Store purchases
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                item_type TEXT NOT NULL,
                target_id TEXT,
                custom_text TEXT,
                remaining_uses INTEGER DEFAULT 0,
                cost INTEGER NOT NULL,
                purchased_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT
            )
        """)
        
        # Targeted responses (secret punishments)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_targeted_responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_discord_id TEXT NOT NULL,
                response_text TEXT NOT NULL,
                chance INTEGER DEFAULT 10,
                remaining_activations INTEGER NOT NULL,
                buyer_id TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Transaction ledger - append-only, single source of truth for balances
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_transactions (
                tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                tx_type TEXT NOT NULL,
                reference_id TEXT,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Prop bets (totals, player props)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_props (
                prop_id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL,
                market_key TEXT NOT NULL,
                description TEXT NOT NULL,
                line REAL,
                outcome TEXT NOT NULL,
                odds INTEGER DEFAULT -110,
                status TEXT DEFAULT 'open',
                result TEXT,
                FOREIGN KEY (game_id) REFERENCES kohls_games(game_id)
            )
        """)
        
        # Prop bets placed
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS kohls_prop_bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                prop_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                result TEXT DEFAULT 'pending',
                payout INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (prop_id) REFERENCES kohls_props(prop_id)
            )
        """)
        
        # Response proposals (voting on new bot responses)
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS response_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                text TEXT NOT NULL,
                chance INTEGER NOT NULL,
                proposer_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # KeepTradeCut dynasty trade value snapshots
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS ktc_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ktc_id INTEGER NOT NULL,
                sleeper_id TEXT,
                player_name TEXT NOT NULL,
                position TEXT NOT NULL,
                team TEXT,
                is_rookie BOOLEAN DEFAULT FALSE,
                value_1qb INTEGER,
                rank_1qb INTEGER,
                positional_rank_1qb INTEGER,
                value_sf INTEGER,
                rank_sf INTEGER,
                positional_rank_sf INTEGER,
                recorded_date TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(ktc_id, recorded_date)
            )
        """)
        await self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ktc_values_sleeper_id
            ON ktc_values(sleeper_id, recorded_date)
        """)
        await self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_ktc_values_player_name
            ON ktc_values(player_name, recorded_date)
        """)

        # Daily roster composition snapshots.
        #
        # Sleeper only serves *current* rosters, so past composition is
        # unrecoverable once the day passes - which is why this is stored
        # rather than derived. Paired with ktc_values (what a player was
        # worth on a date) it's what makes team and trade value history
        # possible. Unchanged days are skipped, so reads are "newest
        # snapshot on or before date D". See lib/roster_history.py.
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS roster_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_date TEXT NOT NULL,
                roster_id INTEGER NOT NULL,
                owner_id TEXT,
                player_id TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(recorded_date, roster_id, player_id)
            )
        """)
        await self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_roster_snapshots_date
            ON roster_snapshots(recorded_date)
        """)
        await self.connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_roster_snapshots_player
            ON roster_snapshots(player_id, recorded_date)
        """)

        # Bot-applied nickname tags (standings rank, draft slot, on-the-clock).
        # base_nickname is tracked here rather than parsed back out of the
        # live Discord nickname, since a member can rename themselves at any
        # time and that should become the new base going forward.
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS nickname_tags (
                discord_id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                base_nickname TEXT,
                tag TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await self.connection.commit()




# Global database instance
db = Database()

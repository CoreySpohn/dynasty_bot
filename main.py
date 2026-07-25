"""Main entry point for Dynasty Bot.

Initializes the Discord bot with command handling and loads
all extension cogs for modular functionality.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands

from clients.sleeper import SleeperClient
import config
from config import DISCORD_TOKEN, SLEEPER_LEAGUE_ID
from lib.league_resolver import resolve_league_id
from database import db


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dynasty_bot")


class DynastyBot(commands.Bot):
    """Custom Bot class for Dynasty Fantasy Football league management.
    
    Attributes:
        sleeper: SleeperClient instance for Sleeper API calls.
        league_id: The configured Sleeper league ID.
    """
    
    sleeper: SleeperClient
    league_id: str
    _session: Optional[aiohttp.ClientSession]
    
    def __init__(self):
        # Set up intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix="!",
            intents=intents,
            description="Dynasty Fantasy Football League Bot",
        )
        
        # Will be initialized in setup_hook
        self._session = None
        self.league_id = SLEEPER_LEAGUE_ID
    
    async def setup_hook(self) -> None:
        """Called when the bot is starting up.
        
        Initializes database, Sleeper client, and loads extensions.
        """
        # Initialize database connection
        logger.info("Connecting to database...")
        await db.connect()
        logger.info("Database connected and schema initialized.")
        
        # Initialize aiohttp session and Sleeper client
        logger.info("Initializing Sleeper API client...")
        self._session = aiohttp.ClientSession()
        self.sleeper = SleeperClient(self._session)
        logger.info("Sleeper client ready.")

        # Follow the league across renewals before any cog loads. Cogs do
        # `from config import SLEEPER_LEAGUE_ID` at import time, so rebinding
        # the module attribute has to happen first to be picked up - which is
        # why this sits here rather than in a cog.
        await self.resolve_league()

        # Load all extension cogs from the cogs directory
        await self.load_extensions()
        
        # Sync slash commands with Discord
        logger.info("Syncing application commands...")
        await self.tree.sync()
        logger.info("Application commands synced.")
    
    async def resolve_league(self) -> str:
        """Point the bot at the current season's league.

        A renewal gives the league a new ID and freezes the old one forever, so
        a config value that was right last year silently serves last year's
        data. This looks up the configured owner's league of the same name for
        the current season and adopts it.

        Falls back to the configured ID on any failure or ambiguity - see
        lib/league_resolver, which refuses to guess between same-named leagues.
        """
        resolved = await resolve_league_id(
            self.sleeper,
            config.SLEEPER_USER_ID,
            config.SLEEPER_LEAGUE_ID,
            config.SLEEPER_LEAGUE_NAME,
        )
        if resolved != config.SLEEPER_LEAGUE_ID:
            logger.warning(
                f"Using resolved league {resolved} instead of configured "
                f"{config.SLEEPER_LEAGUE_ID}"
            )
            # Rebind so cogs importing the constant below get the new value.
            config.SLEEPER_LEAGUE_ID = resolved
        self.league_id = resolved
        logger.info(f"League ID: {self.league_id}")
        return resolved

    async def load_extensions(self) -> None:
        """Load all cog extensions from the cogs directory."""
        cogs_dir = Path(__file__).parent / "cogs"
        
        if not cogs_dir.exists():
            logger.info("No cogs directory found. Creating it...")
            cogs_dir.mkdir(exist_ok=True)
            return
        
        for cog_file in cogs_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue
            
            cog_name = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(cog_name)
                logger.info(f"Loaded extension: {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load extension {cog_name}: {e}")
    
    async def on_ready(self) -> None:
        """Called when the bot has successfully connected to Discord."""
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")
        logger.info("------")
        
        # Set bot status
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name="the dynasty league 🏈",
        )
        await self.change_presence(activity=activity)
    
    async def close(self) -> None:
        """Clean up resources when the bot shuts down."""
        logger.info("Shutting down bot...")
        
        # Close aiohttp session
        if self._session and not self._session.closed:
            await self._session.close()
        
        await db.close()
        await super().close()


async def main() -> None:
    """Main entry point for running the bot."""
    bot = DynastyBot()
    
    try:
        logger.info("Starting Dynasty Bot...")
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt.")
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())

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
from config import DISCORD_TOKEN, SLEEPER_LEAGUE_ID
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
        
        # Load all extension cogs from the cogs directory
        await self.load_extensions()
        
        # Sync slash commands with Discord
        logger.info("Syncing application commands...")
        await self.tree.sync()
        logger.info("Application commands synced.")
    
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

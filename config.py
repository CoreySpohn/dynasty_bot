"""Configuration module for Dynasty Bot.

Loads environment variables from .env file and provides
validated configuration settings for the bot.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _get_required_env(key: str) -> str:
    """Get a required environment variable or raise an error."""
    value = os.getenv(key)
    if value is None:
        raise EnvironmentError(
            f"Missing required environment variable: {key}. "
            f"Please add it to your .env file."
        )
    return value


# Discord Configuration
DISCORD_TOKEN: str = _get_required_env("DISCORD_TOKEN")

# Sleeper API Configuration
SLEEPER_LEAGUE_ID: str = _get_required_env("SLEEPER_LEAGUE_ID")
SLEEPER_API_BASE_URL: str = "https://api.sleeper.app/v1"

# Database Configuration
DATABASE_PATH: str = os.getenv("DATABASE_PATH", "dynasty_bot.db")

# Alert Channel Configuration (for ice chug alerts, etc.)
# Set to None to disable alerts, or provide the Discord channel ID
ALERT_CHANNEL_ID: int | None = (
    int(os.getenv("ALERT_CHANNEL_ID"))
    if os.getenv("ALERT_CHANNEL_ID")
    else None
)

# Scheduler Configuration
# Channel for commissioner alerts and deadline reminders
COMMISH_ALERTS_CHANNEL_ID: int = 1462499662635466987

# Timezone for reminder scheduling (defaults to EST)
REMINDER_TIMEZONE: str = os.getenv("REMINDER_TIMEZONE", "America/New_York")

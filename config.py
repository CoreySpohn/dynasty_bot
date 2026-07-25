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
#
# SLEEPER_LEAGUE_ID is a seed and a fallback rather than the final word: Sleeper
# mints a brand new league on every renewal, so this value goes stale annually.
# When SLEEPER_USER_ID is set, the bot follows the renewal automatically at
# startup by finding that user's league of the same name for the current season
# (lib/league_resolver.py). Without it, this value is used as-is.
SLEEPER_LEAGUE_ID: str = _get_required_env("SLEEPER_LEAGUE_ID")
SLEEPER_USER_ID: str | None = os.getenv("SLEEPER_USER_ID") or None
# Optional override; defaults to the configured league's own name, so a renewal
# needs no extra configuration.
SLEEPER_LEAGUE_NAME: str | None = os.getenv("SLEEPER_LEAGUE_NAME") or None
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

# Timezone for reminder scheduling (defaults to EST)
REMINDER_TIMEZONE: str = os.getenv("REMINDER_TIMEZONE", "America/New_York")

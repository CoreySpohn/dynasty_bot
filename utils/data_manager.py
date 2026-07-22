"""
User mapping management for linking Discord users to Sleeper rosters.

Handles loading and saving the user_map.json file that persists
the relationship between Discord user IDs and Sleeper roster IDs.
"""

import json
import os
from pathlib import Path
from typing import Dict, Optional

# Path to the user mapping file
USER_MAP_PATH = Path(__file__).parent.parent / "data" / "user_map.json"


def ensure_data_directory() -> None:
    """Ensure the data directory exists."""
    USER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_user_map() -> Dict[str, str]:
    """
    Load the user mapping from JSON file.

    Returns:
        Dictionary mapping Discord user IDs to Sleeper roster IDs.
        Returns empty dict if file doesn't exist.
    """
    ensure_data_directory()
    if not USER_MAP_PATH.exists():
        return {}
    try:
        with open(USER_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_user_map(user_map: Dict[str, str]) -> None:
    """
    Save the user mapping to JSON file.

    Args:
        user_map:
            Dictionary mapping Discord user IDs to Sleeper roster IDs.
    """
    ensure_data_directory()
    with open(USER_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(user_map, f, indent=2, ensure_ascii=False)


def get_roster_id_by_discord_id(discord_id: str) -> Optional[str]:
    """
    Get the Sleeper roster ID for a Discord user ID.

    Args:
        discord_id:
            The Discord user ID to look up.

    Returns:
        The Sleeper roster ID if found, None otherwise.
    """
    user_map = load_user_map()
    return user_map.get(str(discord_id))


def set_user_mapping(discord_id: str, roster_id: str) -> None:
    """
    Set the mapping between a Discord user and Sleeper roster.

    Args:
        discord_id:
            The Discord user ID.
        roster_id:
            The Sleeper roster ID.
    """
    user_map = load_user_map()
    user_map[str(discord_id)] = str(roster_id)
    save_user_map(user_map)


def remove_user_mapping(discord_id: str) -> None:
    """
    Remove a user mapping.

    Args:
        discord_id:
            The Discord user ID to remove.
    """
    user_map = load_user_map()
    user_map.pop(str(discord_id), None)
    save_user_map(user_map)



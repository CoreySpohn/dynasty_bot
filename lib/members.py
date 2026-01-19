"""League Members Utility.

Provides lookup functions to find league members across different
identity systems (Discord, Sleeper, nicknames, etc.).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("dynasty_bot.members")

# Config path
MEMBERS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "members.yaml"


@dataclass
class Member:
    """Represents a league member with all known identities."""
    
    name: str
    discord_id: Optional[str] = None
    discord_name: Optional[str] = None
    sleeper_id: Optional[str] = None
    roster_id: Optional[int] = None
    sleeper_usernames: list[str] = field(default_factory=list)
    sleeper_team_names: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)
    notes: str = ""
    
    @property
    def all_names(self) -> list[str]:
        """Get all known names/identifiers for this member."""
        names = [self.name]
        names.extend(self.sleeper_usernames)
        names.extend(self.sleeper_team_names)
        names.extend(self.nicknames)
        if self.discord_name:
            names.append(self.discord_name)
        return names

    
    def matches(self, query: str) -> bool:
        """Check if query matches any of this member's identities.
        
        Args:
            query: Name or identifier to search for.
            
        Returns:
            True if the query matches any identity (case-insensitive).
        """
        query_lower = query.lower().strip()
        
        # Check all names
        for name in self.all_names:
            if name and query_lower == name.lower():
                return True
        
        # Check Discord ID
        if self.discord_id and query_lower == str(self.discord_id):
            return True
        
        return False
    
    def fuzzy_matches(self, query: str) -> bool:
        """Check if query partially matches any identity.
        
        Args:
            query: Partial name to search for.
            
        Returns:
            True if query is contained in any identity.
        """
        query_lower = query.lower().strip()
        
        for name in self.all_names:
            if name and query_lower in name.lower():
                return True
        
        return False


class MemberRegistry:
    """Registry of all league members with lookup capabilities."""
    
    def __init__(self):
        self._members: list[Member] = []
        self._former_members: list[Member] = []
        self._loaded = False
    
    def load(self) -> None:
        """Load members from config file."""
        try:
            with open(MEMBERS_CONFIG_PATH, "r") as f:
                config = yaml.safe_load(f)
            
            self._members = []
            for m in config.get("members", []):
                self._members.append(Member(
                    name=m.get("name", "Unknown"),
                    discord_id=m.get("discord_id"),
                    discord_name=m.get("discord_name"),
                    sleeper_id=m.get("sleeper_id"),
                    roster_id=m.get("roster_id"),
                    sleeper_usernames=m.get("sleeper_usernames", []),
                    sleeper_team_names=m.get("sleeper_team_names", []),
                    nicknames=m.get("nicknames", []),
                    notes=m.get("notes", ""),
                ))
            
            for m in config.get("former_members", []):
                self._former_members.append(Member(
                    name=m.get("name", "Unknown"),
                    discord_id=m.get("discord_id"),
                    discord_name=m.get("discord_name"),
                    sleeper_id=m.get("sleeper_id"),
                    roster_id=m.get("roster_id"),
                    sleeper_usernames=m.get("sleeper_usernames", []),
                    sleeper_team_names=m.get("sleeper_team_names", []),
                    nicknames=m.get("nicknames", []),
                    notes=m.get("notes", ""),
                ))
            
            self._loaded = True
            logger.info(f"Loaded {len(self._members)} members from config")
            
        except Exception as e:
            logger.error(f"Failed to load members config: {e}")
            self._members = []
    
    def reload(self) -> None:
        """Reload members from config file."""
        self.load()
    
    @property
    def members(self) -> list[Member]:
        """Get all active members."""
        if not self._loaded:
            self.load()
        return self._members
    
    @property
    def former_members(self) -> list[Member]:
        """Get all former members."""
        if not self._loaded:
            self.load()
        return self._former_members
    
    def find(self, query: str) -> Optional[Member]:
        """Find a member by any identifier.
        
        Args:
            query: Name, username, team name, or ID to search for.
            
        Returns:
            Member if found, None otherwise.
        """
        if not self._loaded:
            self.load()
        
        for member in self._members:
            if member.matches(query):
                return member
        
        return None
    
    def find_fuzzy(self, query: str) -> list[Member]:
        """Find members by partial match.
        
        Args:
            query: Partial name to search for.
            
        Returns:
            List of matching members.
        """
        if not self._loaded:
            self.load()
        
        matches = []
        for member in self._members:
            if member.fuzzy_matches(query):
                matches.append(member)
        
        return matches
    
    def find_by_discord_id(self, discord_id: int | str) -> Optional[Member]:
        """Find a member by Discord user ID.
        
        Args:
            discord_id: Discord user ID.
            
        Returns:
            Member if found, None otherwise.
        """
        if not self._loaded:
            self.load()
        
        discord_id_str = str(discord_id)
        for member in self._members:
            if member.discord_id and str(member.discord_id) == discord_id_str:
                return member
        
        return None
    
    def find_by_sleeper_username(self, username: str) -> Optional[Member]:
        """Find a member by Sleeper username.
        
        Args:
            username: Sleeper username.
            
        Returns:
            Member if found, None otherwise.
        """
        if not self._loaded:
            self.load()
        
        username_lower = username.lower()
        for member in self._members:
            for sleeper_name in member.sleeper_usernames:
                if sleeper_name.lower() == username_lower:
                    return member
        
        return None
    
    def find_by_team_name(self, team_name: str) -> Optional[Member]:
        """Find a member by Sleeper team name.
        
        Args:
            team_name: Sleeper fantasy team name.
            
        Returns:
            Member if found, None otherwise.
        """
        if not self._loaded:
            self.load()
        
        team_lower = team_name.lower()
        for member in self._members:
            for name in member.sleeper_team_names:
                if name.lower() == team_lower:
                    return member
        
        return None
    
    def get_display_name(self, query: str) -> str:
        """Get the display name for a member.
        
        Args:
            query: Any identifier for the member.
            
        Returns:
            Member's display name, or the original query if not found.
        """
        member = self.find(query)
        return member.name if member else query


# Global singleton instance
_registry: Optional[MemberRegistry] = None


def get_member_registry() -> MemberRegistry:
    """Get the global member registry instance."""
    global _registry
    if _registry is None:
        _registry = MemberRegistry()
    return _registry


# Convenience functions
def find_member(query: str) -> Optional[Member]:
    """Find a member by any identifier."""
    return get_member_registry().find(query)


def get_display_name(query: str) -> str:
    """Get display name for a member."""
    return get_member_registry().get_display_name(query)


def find_member_by_discord(discord_id: int | str) -> Optional[Member]:
    """Find a member by Discord ID."""
    return get_member_registry().find_by_discord_id(discord_id)


def find_member_by_sleeper(username: str) -> Optional[Member]:
    """Find a member by Sleeper username."""
    return get_member_registry().find_by_sleeper_username(username)

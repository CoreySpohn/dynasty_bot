#!/usr/bin/env python3
"""Generate members.yaml from Discord server and Sleeper league data.

This script fetches:
1. Discord server members (names, IDs, nicknames)
2. Sleeper league users (usernames, team names)

And attempts to match them together using fuzzy matching.

Usage:
    uv run python scripts/generate_members.py
    
Requirements:
    - Bot must be in the Discord server
    - DISCORD_TOKEN and SLEEPER_LEAGUE_ID in .env
"""

import asyncio
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
import discord
from discord.ext import commands

from clients.sleeper import SleeperClient
from config import DISCORD_TOKEN, SLEEPER_LEAGUE_ID

# Output path
OUTPUT_PATH = Path(__file__).parent.parent / "config" / "members_generated.yaml"


def fuzzy_match_score(str1: str, str2: str) -> float:
    """Calculate similarity between two strings (0-1)."""
    str1 = str1.lower().strip()
    str2 = str2.lower().strip()
    
    if str1 == str2:
        return 1.0
    
    # Check if one contains the other
    if str1 in str2 or str2 in str1:
        return 0.8
    
    # Check common prefixes
    common_prefix = 0
    for c1, c2 in zip(str1, str2):
        if c1 == c2:
            common_prefix += 1
        else:
            break
    
    if common_prefix >= 3:
        return 0.5 + (common_prefix / max(len(str1), len(str2))) * 0.3
    
    return 0.0


async def fetch_discord_members(guild_id: int) -> list[dict]:
    """Fetch all members from a Discord server."""
    
    intents = discord.Intents.default()
    intents.members = True
    
    class MemberFetcher(commands.Bot):
        def __init__(self):
            super().__init__(command_prefix="!", intents=intents)
            self.members_data = []
            
        async def on_ready(self):
            print(f"✓ Logged in as {self.user}")
            
            for guild in self.guilds:
                print(f"  Guild: {guild.name} (ID: {guild.id})")
                
                async for member in guild.fetch_members(limit=None):
                    if member.bot:
                        continue
                    
                    self.members_data.append({
                        "discord_id": str(member.id),
                        "discord_name": member.name,
                        "discord_display_name": member.display_name,
                        "discord_nick": member.nick,
                        "discord_global_name": member.global_name,
                    })
                    print(f"    - {member.display_name} ({member.name}, ID: {member.id})")
            
            await self.close()
    
    bot = MemberFetcher()
    await bot.start(DISCORD_TOKEN)
    
    return bot.members_data


async def fetch_sleeper_users() -> list[dict]:
    """Fetch all users from the Sleeper league."""
    
    async with aiohttp.ClientSession() as session:
        client = SleeperClient(session)
        
        users = await client.get_users(SLEEPER_LEAGUE_ID)
        rosters = await client.get_rosters(SLEEPER_LEAGUE_ID)
        
        # Build roster lookup
        roster_owners = {}
        for roster in rosters:
            owner_id = roster.get("owner_id")
            roster_id = roster.get("roster_id")
            settings = roster.get("settings", {})
            roster_owners[owner_id] = {
                "roster_id": roster_id,
                "wins": settings.get("wins", 0),
                "losses": settings.get("losses", 0),
            }
        
        sleeper_data = []
        for user in users:
            user_id = user.get("user_id")
            display_name = user.get("display_name", "")
            team_name = user.get("metadata", {}).get("team_name", "")
            
            roster_info = roster_owners.get(user_id, {})
            
            sleeper_data.append({
                "sleeper_user_id": user_id,
                "sleeper_username": display_name,
                "sleeper_team_name": team_name,
                "roster_id": roster_info.get("roster_id"),
                "record": f"{roster_info.get('wins', 0)}-{roster_info.get('losses', 0)}",
            })
            
            team_display = f" ({team_name})" if team_name else ""
            print(f"  - {display_name}{team_display}")
        
        return sleeper_data


def match_members(
    discord_members: list[dict],
    sleeper_users: list[dict],
) -> list[dict]:
    """Attempt to match Discord members with Sleeper users."""
    
    matched = []
    unmatched_discord = list(discord_members)
    unmatched_sleeper = list(sleeper_users)
    
    # Try to match by similar names
    for sleeper in sleeper_users:
        sleeper_name = sleeper["sleeper_username"].lower()
        
        best_match = None
        best_score = 0.3  # Minimum threshold
        
        for discord in unmatched_discord:
            # Check all Discord name variants
            names_to_check = [
                discord["discord_name"],
                discord["discord_display_name"],
                discord.get("discord_nick") or "",
                discord.get("discord_global_name") or "",
            ]
            
            for name in names_to_check:
                if not name:
                    continue
                score = fuzzy_match_score(sleeper_name, name)
                if score > best_score:
                    best_score = score
                    best_match = discord
        
        if best_match:
            matched.append({
                "discord": best_match,
                "sleeper": sleeper,
                "confidence": best_score,
            })
            unmatched_discord.remove(best_match)
            unmatched_sleeper.remove(sleeper)
    
    return matched, unmatched_discord, unmatched_sleeper


def generate_yaml(
    matched: list[dict],
    unmatched_discord: list[dict],
    unmatched_sleeper: list[dict],
) -> str:
    """Generate the members.yaml content."""
    
    lines = [
        "# League Members Configuration",
        "# Auto-generated - review and edit as needed!",
        "#",
        "# Fields:",
        "#   name: Primary display name",
        "#   discord_id: Discord user ID",
        "#   discord_name: Discord username",
        "#   sleeper_usernames: List of Sleeper usernames",
        "#   sleeper_team_names: List of team names",
        "#   nicknames: Alternative names",
        "#   notes: Any relevant notes",
        "",
        "members:",
    ]
    
    # Matched members
    for m in matched:
        discord = m["discord"]
        sleeper = m["sleeper"]
        conf = m["confidence"]
        
        # Use Discord display name as primary name
        name = discord["discord_display_name"]
        
        lines.append(f'  - name: "{name}"')
        lines.append(f'    discord_id: "{discord["discord_id"]}"')
        lines.append(f'    discord_name: "{discord["discord_name"]}"')
        lines.append(f'    sleeper_usernames:')
        lines.append(f'      - "{sleeper["sleeper_username"]}"')
        
        if sleeper["sleeper_team_name"]:
            lines.append(f'    sleeper_team_names:')
            lines.append(f'      - "{sleeper["sleeper_team_name"]}"')
        else:
            lines.append(f'    sleeper_team_names: []')
        
        lines.append(f'    nicknames: []')
        lines.append(f'    notes: "Auto-matched (confidence: {conf:.0%})"')
        lines.append('')
    
    # Unmatched Sleeper users
    if unmatched_sleeper:
        lines.append("  # Sleeper users without Discord match - add discord_id manually:")
        for sleeper in unmatched_sleeper:
            name = sleeper["sleeper_username"]
            lines.append(f'  - name: "{name}"  # TODO: Add real name')
            lines.append(f'    discord_id: null  # TODO: Add Discord ID')
            lines.append(f'    discord_name: null')
            lines.append(f'    sleeper_usernames:')
            lines.append(f'      - "{sleeper["sleeper_username"]}"')
            
            if sleeper["sleeper_team_name"]:
                lines.append(f'    sleeper_team_names:')
                lines.append(f'      - "{sleeper["sleeper_team_name"]}"')
            else:
                lines.append(f'    sleeper_team_names: []')
            
            lines.append(f'    nicknames: []')
            lines.append(f'    notes: "Needs Discord match"')
            lines.append('')
    
    # Note about unmatched Discord members
    if unmatched_discord:
        lines.append("")
        lines.append("# Unmatched Discord members (may not be in the league):")
        for discord in unmatched_discord:
            lines.append(f"#   - {discord['discord_display_name']} ({discord['discord_name']}, ID: {discord['discord_id']})")
    
    lines.append("")
    lines.append("former_members: []")
    lines.append("")
    
    return "\n".join(lines)


async def main():
    print("=" * 60)
    print("League Members Generator")
    print("=" * 60)
    print()
    
    # Fetch Sleeper data first (doesn't need bot)
    print("📊 Fetching Sleeper league users...")
    sleeper_users = await fetch_sleeper_users()
    print(f"  Found {len(sleeper_users)} Sleeper users")
    print()
    
    # Fetch Discord members
    print("🎮 Fetching Discord server members...")
    print("  (Bot will connect briefly to fetch members)")
    discord_members = await fetch_discord_members(None)
    print(f"  Found {len(discord_members)} Discord members")
    print()
    
    # Match them
    print("🔗 Matching Discord ↔ Sleeper...")
    matched, unmatched_discord, unmatched_sleeper = match_members(
        discord_members, sleeper_users
    )
    print(f"  ✓ Matched: {len(matched)}")
    print(f"  ? Unmatched Discord: {len(unmatched_discord)}")
    print(f"  ? Unmatched Sleeper: {len(unmatched_sleeper)}")
    print()
    
    # Generate YAML
    print("📝 Generating members.yaml...")
    yaml_content = generate_yaml(matched, unmatched_discord, unmatched_sleeper)
    
    # Save
    with open(OUTPUT_PATH, "w") as f:
        f.write(yaml_content)
    
    print(f"  Saved to: {OUTPUT_PATH}")
    print()
    print("=" * 60)
    print("DONE! Review the generated file and:")
    print("  1. Fix any incorrect matches")
    print("  2. Add Discord IDs for unmatched Sleeper users")
    print("  3. Add real names where needed")
    print("  4. Copy to config/members.yaml when ready")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

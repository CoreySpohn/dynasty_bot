"""League Rumors Cog for Dynasty Bot.

Handles AI-powered league rumors with configurable reporter personalities.
Users can DM the bot with rumors, which get rewritten and posted to
the league discussion channel. Also posts random unprompted rumors
to keep people on their toes.
"""

import asyncio
import logging
import os
import random
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from config import SLEEPER_LEAGUE_ID
from lib.ai_client import GeminiClient
from lib.members import get_member_registry

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.rumors")

# Load reporters config
CONFIG_PATH = Path(__file__).parent.parent / "config" / "reporters.yaml"


def load_reporters_config() -> dict:
    """Load reporter personalities from YAML config."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load reporters config: {e}")
        return {"reporters": [], "random_topics": []}


def save_reporters_config(config: dict) -> bool:
    """Save reporter personalities to YAML config.
    
    Args:
        config: The config dict to save.
        
    Returns:
        True if saved successfully.
    """
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        return True
    except Exception as e:
        logger.error(f"Failed to save reporters config: {e}")
        return False


class ReporterSelect(discord.ui.Select):
    """Dropdown for selecting a reporter persona."""
    
    def __init__(self, reporters: list[dict], rumor_text: str, cog: "LeagueRumors"):
        self.rumor_text = rumor_text
        self.cog = cog
        
        options = [
            discord.SelectOption(
                label=r.get("name", "Unknown"),
                emoji=r.get("emoji", "📰"),
                description=r.get("name", "")[:50],
            )
            for r in reporters
        ]
        
        # Add random option
        options.insert(0, discord.SelectOption(
            label="🎲 Random Reporter",
            emoji="🎲",
            description="Let fate decide who reports this!",
            value="__random__",
        ))
        
        super().__init__(
            placeholder="Choose who reports this rumor...",
            options=options,
            min_values=1,
            max_values=1,
        )
    
    async def callback(self, interaction: discord.Interaction) -> None:
        """Handle reporter selection."""
        await interaction.response.defer()
        
        selected = self.values[0]
        
        if selected == "__random__":
            reporter_name, reporter_style, emoji = self.cog._get_random_reporter()
        else:
            # Find the selected reporter
            reporter = next(
                (r for r in self.cog.config.get("reporters", []) 
                 if r.get("name") == selected),
                None
            )
            if reporter:
                reporter_name = reporter.get("name", "Reporter")
                reporter_style = reporter.get("style", "Be professional.")
                emoji = reporter.get("emoji", "📰")
            else:
                reporter_name, reporter_style, emoji = self.cog._get_random_reporter()
        
        # Get team names
        team_names = await self.cog._get_team_names()
        member_context = await self.cog._get_roster_context()
        
        # Rewrite the rumor
        try:
            rewritten = await self.cog.ai_client.rewrite_as_reporter(
                rumor=self.rumor_text,
                reporter_name=reporter_name,
                reporter_style=reporter_style,
                team_names=team_names,
                member_context=member_context,
            )
            
            success = await self.cog._post_rumor(
                content=rewritten,
                reporter_name=reporter_name,
                emoji=emoji,
                source=interaction.user.display_name,
            )
            
            if success:
                await interaction.followup.send(
                    f"🗞️ Your rumor has been reported by **{reporter_name}**! "
                    f"Check the league discussion channel."
                )
            else:
                await interaction.followup.send(
                    "❌ Couldn't post the rumor. Is the rumors channel configured?"
                )
                
        except Exception as e:
            logger.error(f"Error processing rumor: {e}")
            await interaction.followup.send(
                "❌ Something went wrong. Try again later!"
            )
        
        # Disable the view after selection
        self.disabled = True
        self.view.stop()


class ReporterSelectView(discord.ui.View):
    """View containing the reporter selection dropdown."""
    
    def __init__(self, reporters: list[dict], rumor_text: str, cog: "LeagueRumors"):
        super().__init__(timeout=300)  # 5 minute timeout
        self.add_item(ReporterSelect(reporters, rumor_text, cog))


class LeagueRumors(commands.Cog):
    """AI-powered league rumors with reporter personalities.
    
    Users can DM the bot with rumors, which get rewritten in their
    chosen reporter's style and posted to the league discussion channel.
    """
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.league_id = SLEEPER_LEAGUE_ID
        self.config = load_reporters_config()
        self.ai_client = GeminiClient()
        
        # Channels for different rumor types
        self.rumors_channel_id = int(os.getenv("RUMORS_CHANNEL_ID", 0))
        self.nfl_channel_id = int(os.getenv("NFL_CHANNEL_ID", 0)) or self.rumors_channel_id
        
        # Cache for team names
        self._team_names: Optional[list[str]] = None
        
        # Start random rumor task
        self.random_rumor_task.start()
    
    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        logger.info("League Rumors cog loaded")
        if not self.rumors_channel_id:
            logger.warning("RUMORS_CHANNEL_ID not configured")
        if self.nfl_channel_id != self.rumors_channel_id:
            logger.info(f"NFL news will go to channel {self.nfl_channel_id}")

    
    async def cog_unload(self) -> None:
        """Called when the cog is unloaded."""
        self.random_rumor_task.cancel()
        logger.info("League Rumors cog unloaded")
    
    async def _get_team_names(self) -> list[str]:
        """Get team names from the league for context."""
        if self._team_names:
            return self._team_names
        
        try:
            users = await self.bot.sleeper.get_users(self.league_id)
            self._team_names = [
                u.get("metadata", {}).get("team_name") or u.get("display_name")
                for u in users
            ]
            return self._team_names
        except Exception as e:
            logger.error(f"Failed to get team names: {e}")
            return []
    
    async def _get_roster_context(self) -> str:
        """Get full roster context for AI rumor generation.
        
        Returns formatted string with member names, teams, and key players.
        """
        registry = get_member_registry()
        
        try:
            # Get roster data from Sleeper
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users = await self.bot.sleeper.get_users(self.league_id)
            players = await self.bot.sleeper.get_all_players()
            
            # Map user_id to display_name
            user_lookup = {u.get("user_id"): u.get("display_name") for u in users}
            
            # Build roster info by Sleeper username
            roster_players = {}
            for roster in rosters:
                owner_id = roster.get("owner_id")
                sleeper_username = user_lookup.get(owner_id, "Unknown")
                starters = roster.get("starters", [])[:5]  # Top 5 starters
                
                player_names = []
                for pid in starters:
                    if pid and pid != "0":
                        player = players.get(pid, {})
                        name = player.get("full_name", "Unknown")
                        pos = player.get("position", "")
                        if name != "Unknown":
                            player_names.append(f"{name} ({pos})")
                
                roster_players[sleeper_username.lower()] = player_names
            
            # Build context with member names and their players
            lines = []
            for member in registry.members:
                # Find this member's players
                member_players = []
                for username in member.sleeper_usernames:
                    if username.lower() in roster_players:
                        member_players = roster_players[username.lower()]
                        break
                
                team = member.sleeper_team_names[0] if member.sleeper_team_names else "Unknown team"
                players_str = ", ".join(member_players[:3]) if member_players else "Unknown roster"
                lines.append(f"- {member.name} owns \"{team}\" with players like: {players_str}")
            
            return "\n".join(lines)
            
        except Exception as e:
            logger.error(f"Failed to get roster context: {e}")
            # Fall back to basic member context
            return self._get_member_context()
    
    def _get_member_context(self) -> str:
        """Get basic member context (fallback if roster fetch fails).
        
        Returns formatted string with member names and their fantasy teams.
        """
        registry = get_member_registry()
        lines = []
        
        for member in registry.members:
            if member.sleeper_team_names:
                team = member.sleeper_team_names[0]
                lines.append(f"- {member.name} owns the team \"{team}\"")
            elif member.sleeper_usernames:
                lines.append(f"- {member.name} (Sleeper username: {member.sleeper_usernames[0]})")
            else:
                lines.append(f"- {member.name}")
        
        return "\n".join(lines)
    
    def _get_random_reporter(self) -> tuple[str, str, str]:
        """Get a random reporter persona.
        
        Returns:
            Tuple of (name, style, emoji)
        """
        reporters = self.config.get("reporters", [])
        if not reporters:
            return ("Unknown Reporter", "Report the news professionally.", "📰")
        
        reporter = random.choice(reporters)
        return (
            reporter.get("name", "Reporter"),
            reporter.get("style", "Be professional."),
            reporter.get("emoji", "📰"),
        )
    
    def _get_reporter_list_text(self) -> str:
        """Get formatted list of available reporters."""
        reporters = self.config.get("reporters", [])
        lines = ["**Available Reporters:**"]
        for r in reporters:
            emoji = r.get("emoji", "📰")
            name = r.get("name", "Unknown")
            lines.append(f"{emoji} {name}")
        return "\n".join(lines)
    
    async def _post_rumor(
        self,
        content: str,
        reporter_name: str,
        emoji: str,
        source: Optional[str] = None,
        channel_id: Optional[int] = None,
    ) -> bool:
        """Post a rumor to the specified channel.
        
        Args:
            content: The rewritten rumor text.
            reporter_name: Name of the reporter.
            emoji: Emoji for the reporter.
            source: Optional source attribution.
            channel_id: Channel to post to (defaults to rumors channel).
            
        Returns:
            True if posted successfully.
        """
        target_channel_id = channel_id or self.rumors_channel_id
        
        if not target_channel_id:
            logger.warning("Cannot post rumor - no channel configured")
            return False
        
        channel = self.bot.get_channel(target_channel_id)
        if not channel:
            logger.error(f"Channel {target_channel_id} not found")
            return False
        
        embed = discord.Embed(
            description=content,
            color=discord.Color.orange(),
            timestamp=datetime.now(),
        )
        embed.set_author(
            name=f"{emoji} {reporter_name}",
        )
        
        embed.set_footer(text="🗞️ League Insider Report")
        
        try:
            await channel.send(embed=embed)
            logger.info(f"Posted rumor as {reporter_name} to channel {target_channel_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to post rumor: {e}")
            return False
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Listen for DMs and process as rumors."""
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Only process DMs
        if not isinstance(message.channel, discord.DMChannel):
            return
        
        # Ignore if message is too short
        if len(message.content.strip()) < 10:
            await message.reply(
                "📰 That's a bit short for a rumor! Give me something juicy to work with."
            )
            return
        
        # Ignore if it looks like a command
        if message.content.startswith(("/", "!", "?")):
            return
        
        logger.info(f"Received rumor DM from {message.author}: {message.content[:50]}...")
        
        # Acknowledge receipt
        await message.add_reaction("📰")
        
        # Create reporter selection view
        reporters = self.config.get("reporters", [])
        view = ReporterSelectView(reporters, message.content, self)
        
        # Build reporter list for display
        reporter_list = "\n".join(
            f"  {r.get('emoji', '📰')} **{r.get('name', 'Unknown')}**"
            for r in reporters
        )
        
        await message.reply(
            "📰 **Who should report this rumor?**\n\n"
            "**Available Reporters:**\n"
            f"{reporter_list}\n\n"
            "Choose from the dropdown below, or pick 🎲 Random!",
            view=view,
        )
    
    @tasks.loop(hours=48)  # Check every 48 hours (~1 per week with randomness)
    async def random_rumor_task(self) -> None:
        """Occasionally post an unprompted random rumor (~1 per week)."""
        # Wait for bot to be ready
        await self.bot.wait_until_ready()
        
        # Random chance to post (roughly 1 per week with 48h loop)
        # 50% chance per 48 hours ≈ 1.75 per week, so ~35% chance for ~1/week
        if random.random() > 0.35:
            logger.debug("Skipping random rumor this cycle")
            return
        
        # Don't post between midnight and 8am
        current_hour = datetime.now().hour
        if current_hour < 8:
            logger.debug("Too early for random rumors")
            return
        
        # Get random topic and reporter
        topics = self.config.get("random_topics", [])
        if not topics:
            return
        
        topic = random.choice(topics)
        reporter_name, reporter_style, emoji = self._get_random_reporter()
        team_names = await self._get_team_names()
        member_context = await self._get_roster_context()
        
        if not team_names:
            return
        
        logger.info(f"Generating random rumor about: {topic}")
        
        try:
            rumor = await self.ai_client.generate_random_rumor(
                topic=topic,
                team_names=team_names,
                reporter_name=reporter_name,
                reporter_style=reporter_style,
                member_context=member_context,
            )
            
            if rumor:
                await self._post_rumor(
                    content=rumor,
                    reporter_name=reporter_name,
                    emoji=emoji,
                )
                
        except Exception as e:
            logger.error(f"Error generating random rumor: {e}")
    
    @random_rumor_task.before_loop
    async def before_random_rumor(self) -> None:
        """Wait for bot to be ready before starting random rumors."""
        await self.bot.wait_until_ready()
        # Add initial delay to avoid immediate post on startup
        await asyncio.sleep(60)  # Wait 1 minute after startup
    
    @app_commands.command(
        name="rumor",
        description="Submit a rumor and choose who reports it"
    )
    @app_commands.describe(
        rumor="The rumor or info to report",
        reporter="Which reporter should break this news?",
        context="Is this about the fantasy league or NFL in general?",
        custom_personality="Describe your own custom reporter (e.g. 'a drunk pirate' or 'Yoda from Star Wars')"
    )
    @app_commands.choices(
        context=[
            app_commands.Choice(name="🏠 Fantasy League", value="league"),
            app_commands.Choice(name="🏈 NFL News", value="nfl"),
        ],
        reporter=[
            app_commands.Choice(name="🎲 Random", value="random"),
            app_commands.Choice(name="🎭 Custom (describe below)", value="custom"),
            app_commands.Choice(name="🧈 Butters Stotch", value="Butters Stotch"),
            app_commands.Choice(name="📱 Adam Schefter", value="Adam Schefter"),
            app_commands.Choice(name="📢 Stephen A. Smith", value="Stephen A. Smith"),
            app_commands.Choice(name="📰 Ian Rapoport", value="Ian Rapoport"),
            app_commands.Choice(name="📺 Ron Burgundy", value="Ron Burgundy"),
            app_commands.Choice(name="📣 Alex Jones", value="Alex Jones"),
            app_commands.Choice(name="🍊 Donald Trump", value="Donald Trump"),
            app_commands.Choice(name="🎤 Joe Rogan", value="Joe Rogan"),
            app_commands.Choice(name="🎬 Morgan Freeman", value="Morgan Freeman"),
            app_commands.Choice(name="👨‍🍳 Gordon Ramsay", value="Gordon Ramsay"),
            app_commands.Choice(name="🇰🇿 Borat", value="Borat"),
        ]
    )
    async def submit_rumor(
        self,
        interaction: discord.Interaction,
        rumor: str,
        context: str = "league",
        reporter: str = "random",
        custom_personality: Optional[str] = None,
    ) -> None:
        """Submit a rumor via slash command with reporter choice."""
        await interaction.response.defer(ephemeral=True)
        
        if len(rumor.strip()) < 10:
            await interaction.followup.send(
                "📰 That's a bit short! Give me something juicy.",
                ephemeral=True,
            )
            return
        
        # Handle custom personality
        if reporter == "custom":
            if not custom_personality:
                await interaction.followup.send(
                    "🎭 You selected Custom reporter but didn't describe the personality!\n"
                    "Fill in the `custom_personality` field (e.g. 'a drunk pirate' or 'Yoda from Star Wars')",
                    ephemeral=True,
                )
                return
            
            # Use AI to parse the custom personality
            reporter_name, emoji, reporter_style = await self.ai_client.parse_custom_reporter(
                custom_personality
            )
        elif reporter == "random":
            reporter_name, reporter_style, emoji = self._get_random_reporter()
        else:
            reporter_data = next(
                (r for r in self.config.get("reporters", []) 
                 if r.get("name") == reporter),
                None
            )
            if reporter_data:
                reporter_name = reporter_data.get("name", "Reporter")
                reporter_style = reporter_data.get("style", "Be professional.")
                emoji = reporter_data.get("emoji", "📰")
            else:
                reporter_name, reporter_style, emoji = self._get_random_reporter()
        
        # Only include league context for fantasy league rumors
        if context == "league":
            team_names = await self._get_team_names()
            member_context = await self._get_roster_context()
        else:
            # NFL news - no league-specific context
            team_names = None
            member_context = None
        
        # Determine which channel to post to
        target_channel = self.nfl_channel_id if context == "nfl" else self.rumors_channel_id
        
        try:
            rewritten = await self.ai_client.rewrite_as_reporter(
                rumor=rumor,
                reporter_name=reporter_name,
                reporter_style=reporter_style,
                team_names=team_names,
                member_context=member_context,
                is_nfl_news=(context == "nfl"),
            )
            
            success = await self._post_rumor(
                content=rewritten,
                reporter_name=reporter_name,
                emoji=emoji,
                source=interaction.user.display_name,
                channel_id=target_channel,
            )
            
            if success:
                context_label = "NFL news" if context == "nfl" else "league rumor"
                await interaction.followup.send(
                    f"🗞️ Your {context_label} has been reported by **{reporter_name}**!",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "❌ Couldn't post the rumor.",
                    ephemeral=True,
                )
                
        except Exception as e:
            logger.error(f"Error in rumor command: {e}")
            await interaction.followup.send(
                "❌ Something went wrong. Try again later!",
                ephemeral=True,
            )
    
    @app_commands.command(
        name="postrumor",
        description="[Admin] Force post a random rumor now"
    )
    @app_commands.default_permissions(administrator=True)
    async def force_random_rumor(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Admin command to force a random rumor post."""
        await interaction.response.defer(ephemeral=True)
        
        topics = self.config.get("random_topics", [])
        if not topics:
            await interaction.followup.send("No random topics configured.", ephemeral=True)
            return
        
        topic = random.choice(topics)
        reporter_name, reporter_style, emoji = self._get_random_reporter()
        team_names = await self._get_team_names()
        member_context = await self._get_roster_context()
        
        try:
            rumor = await self.ai_client.generate_random_rumor(
                topic=topic,
                team_names=team_names,
                reporter_name=reporter_name,
                reporter_style=reporter_style,
                member_context=member_context,
            )
            
            if rumor:
                success = await self._post_rumor(
                    content=rumor,
                    reporter_name=reporter_name,
                    emoji=emoji,
                )
                
                if success:
                    await interaction.followup.send(
                        f"✅ Random rumor posted by {reporter_name}!",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send("❌ Failed to post.", ephemeral=True)
            else:
                await interaction.followup.send("❌ AI failed to generate rumor.", ephemeral=True)
                
        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)


async def setup(bot: "DynastyBot") -> None:
    """Load the League Rumors cog."""
    await bot.add_cog(LeagueRumors(bot))

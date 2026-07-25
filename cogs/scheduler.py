"""Scheduler Cog - League deadline reminders and state management.

Manages league deadlines, sends automated reminders, and handles
state transitions between off_season, pre_season, and in_season modes.

Features:
- YAML-based deadline configuration
- NFL schedule integration via nflreadpy
- Automated reminders at 6 PM EST
- Slash commands for viewing/managing deadlines and state
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import discord
import pytz
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from clients.espn import ESPNClient
from clients.nfl_schedule import NFLScheduleClient
from database import db
from lib.league_state import derive_state, should_apply
from lib.yaml_config import load_config, save_config
from lib.nfl_calendar import (
    ANCHOR_PRESEASON_END,
    ANCHOR_PRESEASON_START,
    ANCHOR_ROOKIE_DRAFT_END,
    load_anchors,
    preseason_bounds,
    save_anchors,
)
from lib.taxi_rules import upcoming_season

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger(__name__)

# Valid league states
VALID_STATES = ['off_season', 'pre_season', 'in_season']

# Config file paths
CONFIG_DIR = Path(__file__).parent.parent / 'config'
LEAGUE_STATE_PATH = CONFIG_DIR / 'league_state.yaml'
DEADLINES_PATH = CONFIG_DIR / 'deadlines.yaml'
PICK_VALUES_PATH = CONFIG_DIR / 'pick_values.yaml'



def _parse_iso(value: Any) -> Optional[date]:
    """Parse a stored ISO date anchor, or None if absent/malformed."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        logger.warning(f"Malformed date anchor: {value!r}")
        return None


class SchedulerCog(commands.Cog):
    """Manages league deadlines, reminders, and state transitions.
    
    This cog handles:
    - Loading and managing deadline configurations
    - Sending automated reminders at 6 PM EST
    - Managing league state (off_season, pre_season, in_season)
    - Syncing NFL schedule data for date calculations
    """
    
    def __init__(self, bot: DynastyBot):
        self.bot = bot
        self.nfl_client = NFLScheduleClient()
        self.timezone = pytz.timezone("America/New_York")
        
        # Load configurations
        self.state_config = self._load_state()
        self.deadlines_config = self._load_deadlines()
        # Bot-written NFL dates, kept out of the hand-maintained deadlines file.
        self.nfl_anchors = load_anchors()
        
        # Start the reminder loop
        self.reminder_loop.start()
        self.upkeep_loop.start()
        logger.info("Scheduler cog initialized")
    
    def cog_unload(self):
        """Clean up when cog is unloaded."""
        self.reminder_loop.cancel()
        self.upkeep_loop.cancel()
    
    # =========================================================================
    # Configuration Loading/Saving
    # =========================================================================
    
    def _load_state(self) -> dict:
        """Load league state, preserving the file's comments for saving."""
        return load_config(LEAGUE_STATE_PATH, default={
                'current_state': 'off_season',
                'season': {'year': datetime.now().year},
                'alerts': {
                    'channel_id': None,
                    'reminder_time': '18:00',
                    'timezone': 'America/New_York'
                },
                'commissioner': {'discord_id': None}
            })
    
    def _load_deadlines(self) -> dict:
        """Load deadlines, preserving the file's comments for saving."""
        return load_config(DEADLINES_PATH, default={'deadlines': []})
    
    def _save_state(self) -> None:
        """Save league state, keeping its comments intact.

        Comment preservation matters here because _advance_state saves
        automatically when the season moves on - nobody is watching.
        """
        save_config(self.state_config, LEAGUE_STATE_PATH)
        logger.info("Saved league state config")
    
    def _save_deadlines(self) -> None:
        """Save deadlines, keeping its comments intact."""
        save_config(self.deadlines_config, DEADLINES_PATH)
        logger.info("Saved deadlines config")
    
    @property
    def current_state(self) -> str:
        """Get the current league state."""
        return self.state_config.get('current_state', 'off_season')
    
    @property
    def current_season(self) -> int:
        """Get the current season year."""
        return self.state_config.get('season', {}).get('year', datetime.now().year)
    
    @property
    def alert_channel_id(self) -> Optional[int]:
        """Get the alert channel ID."""
        return self.state_config.get('alerts', {}).get('channel_id')
    
    @property
    def commissioner_id(self) -> Optional[int]:
        """Get the commissioner's Discord ID."""
        return self.state_config.get('commissioner', {}).get('discord_id')
    
    @property
    def announcements_channel_id(self) -> Optional[int]:
        """Get the announcements channel ID."""
        return self.state_config.get('announcements', {}).get('channel_id')
    
    @property
    def announcements_role_id(self) -> Optional[int]:
        """Get the Active Owner role ID for announcements."""
        return self.state_config.get('announcements', {}).get('role_id')
    
    # =========================================================================
    # Reminder Loop
    # =========================================================================
    
    @tasks.loop(minutes=30)
    async def reminder_loop(self):
        """Check for upcoming deadlines and send reminders at 6 PM EST."""
        now = datetime.now(self.timezone)
        
        # Only run at 6 PM (between 18:00 and 18:29)
        if now.hour != 18 or now.minute >= 30:
            return
        
        logger.info("Running reminder check...")
        await self._check_and_send_reminders()
    
    @reminder_loop.before_loop
    async def before_reminder_loop(self):
        """Wait for bot to be ready before starting loop."""
        await self.bot.wait_until_ready()
        logger.info("Reminder loop starting...")

    # =========================================================================
    # Upkeep Loop - keeps anchors and league state current without a human
    # =========================================================================

    @tasks.loop(hours=12)
    async def upkeep_loop(self):
        """Refresh NFL anchors and advance league state.

        Both jobs used to need a commissioner to remember a slash command, and
        both failed quietly when nobody did: `nfl_anchors` sat a year stale and
        `current_state` gated the wrong reminders.

        Twice a day is enough for either. The anchors change a few times a year
        (schedule release, then whenever the rookie draft finally finishes), and
        the state changes four times.
        """
        try:
            await self._run_upkeep()
        except Exception as e:
            logger.error(f"Upkeep loop failed: {e}", exc_info=True)

    @upkeep_loop.before_loop
    async def before_upkeep_loop(self):
        await self.bot.wait_until_ready()
        logger.info("Upkeep loop starting...")

    async def _run_upkeep(self) -> dict[str, Any]:
        """One upkeep pass. Returns what it did, for logging and tests."""
        season = await self._target_season()
        result: dict[str, Any] = {
            'season': season,
            'anchors_synced': False,
            'state_changed': None,
            'state_suggested': None,
        }

        if self._only_draft_date_missing(season):
            # Everything else is current and the draft simply hasn't finished.
            # Poll just that one date rather than re-downloading the nflverse
            # schedule and re-hitting ESPN four times, twice a day, for the
            # weeks it can take the draft to land.
            draft_end = await self._rookie_draft_end(season)
            if draft_end:
                self.nfl_anchors[ANCHOR_ROOKIE_DRAFT_END] = draft_end.isoformat()
                save_anchors(self.nfl_anchors)
                result['anchors_synced'] = True
                logger.info(f"Recorded {season} rookie draft end {draft_end}")
        elif self._anchors_need_sync(season):
            anchors = await self._fetch_anchors(season)
            self._store_anchors(anchors)
            result['anchors_synced'] = True
            logger.info(f"Upkeep synced NFL anchors for {season}")

        result.update(await self._advance_state())
        return result

    def _schedule_anchors_current(self, season: int) -> bool:
        """Whether the schedule-derived anchors are present and for `season`.

        Excludes the rookie draft date, which comes from Sleeper rather than a
        schedule and lands on its own timetable.
        """
        anchors = self.nfl_anchors or {}
        opener = anchors.get('nfl_regular_season_start')
        if not opener or not str(opener).startswith(str(season)):
            return False
        # Preseason schedule may not have been published at last sync.
        return bool(anchors.get(ANCHOR_PRESEASON_START))

    def _only_draft_date_missing(self, season: int) -> bool:
        """Whether the sole gap is the rookie draft end date."""
        return self._schedule_anchors_current(season) and not (
            self.nfl_anchors or {}
        ).get(ANCHOR_ROOKIE_DRAFT_END)

    def _anchors_need_sync(self, season: int) -> bool:
        """Whether a full anchor re-sync is warranted.

        Re-syncs while the rookie draft is unfinished, since that date is the
        one anchor that lands unpredictably - the draft moves to whatever
        weekend owners can manage and then takes days to play out. That case is
        handled by the cheaper `_only_draft_date_missing` path first.
        """
        if not self.nfl_anchors:
            return True
        if not self._schedule_anchors_current(season):
            return True
        return not self.nfl_anchors.get(ANCHOR_ROOKIE_DRAFT_END)

    async def _advance_state(self) -> dict[str, Any]:
        """Apply the state the observable signals imply, if it's a step forward.

        Backwards moves (the in_season -> off_season wrap) are announced as a
        suggestion instead of applied, because that transition coincides with a
        human deciding the offseason calendar - see lib/league_state.
        """
        try:
            nfl_state = await self.bot.sleeper.get_nfl_state()
        except Exception as e:
            logger.warning(f"Could not fetch NFL state for upkeep: {e}")
            return {'state_changed': None, 'state_suggested': None}

        season = await self._target_season()
        anchors = self.nfl_anchors or {}
        opener = _parse_iso(anchors.get('nfl_regular_season_start'))
        draft_end = _parse_iso(anchors.get(ANCHOR_ROOKIE_DRAFT_END))

        derived = derive_state(
            nfl_state,
            rookie_draft_complete=draft_end is not None,
            regular_season_start=opener,
        )
        if derived is None or derived.state == self.current_state:
            return {'state_changed': None, 'state_suggested': None}

        if not should_apply(self.current_state, derived):
            logger.info(
                f"League state {self.current_state} -> {derived.state} needs "
                f"confirmation ({derived.reason})"
            )
            await self._announce_state_suggestion(derived)
            return {'state_changed': None, 'state_suggested': derived.state}

        previous = self.current_state
        self.state_config['current_state'] = derived.state
        self.state_config.setdefault('season', {})['year'] = season
        self._save_state()
        logger.info(
            f"League state advanced {previous} -> {derived.state} "
            f"({derived.reason})"
        )
        await self._announce_state_change(previous, derived)
        return {'state_changed': derived.state, 'state_suggested': None}

    async def _announce_state_change(self, previous: str, derived) -> None:
        """Tell the commissioner channel the state moved on its own."""
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            return
        embed = discord.Embed(
            title="🔄 League State Advanced",
            description=(
                f"**{previous.replace('_', ' ').title()}** → "
                f"**{derived.state.replace('_', ' ').title()}**"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="Why", value=derived.reason, inline=False)
        embed.set_footer(text="Automatic — override with /state")
        await channel.send(embed=embed)

    async def _announce_state_suggestion(self, derived) -> None:
        """Ask rather than act, for transitions that need a human."""
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            return
        embed = discord.Embed(
            title="❓ League State Change Suggested",
            description=(
                f"Signals point to **{derived.state.replace('_', ' ').title()}**, "
                f"but the league is in **{self.current_state.replace('_', ' ').title()}**."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(name="Why", value=derived.reason, inline=False)
        embed.set_footer(text="Not applied automatically — use /state to confirm")
        await channel.send(embed=embed)
    
    async def _check_and_send_reminders(self) -> None:
        """Check all deadlines and send appropriate reminders."""
        channel = self.bot.get_channel(self.alert_channel_id)
        if not channel:
            logger.warning(f"Alert channel {self.alert_channel_id} not found")
            return
        
        today = datetime.now(self.timezone).date()
        current_state = self.current_state
        season = self.current_season
        
        for deadline in self.deadlines_config.get('deadlines', []):
            # Skip disabled deadlines
            if not deadline.get('enabled', True):
                continue
            
            # Skip deadlines for other states (except weekly ones)
            deadline_state = deadline.get('state')
            is_weekly = deadline.get('recurring') == 'weekly_in_season'
            
            if deadline_state and deadline_state != current_state and not is_weekly:
                continue
            
            # Handle weekly in-season reminders
            if is_weekly and current_state == 'in_season':
                if await self._check_weekly_reminder(channel, deadline, today):
                    continue
            
            # Resolve the deadline date
            deadline_date = self._resolve_deadline_date(deadline)
            if not deadline_date:
                continue
            
            days_until = (deadline_date - today).days
            
            # Check if we should send a reminder
            reminders = deadline.get('reminders', [1, 0])
            if days_until in reminders:
                # Check if we already sent this reminder
                if await self._reminder_already_sent(deadline['id'], season, days_until):
                    continue
                
                # Special handling for draft preview
                if deadline.get('auto_post_preview'):
                    await self._send_draft_preview(channel, deadline)
                else:
                    await self._send_reminder(channel, deadline, days_until, deadline_date)
                
                await self._record_reminder(deadline['id'], season, days_until)

    
    async def _check_weekly_reminder(
        self, 
        channel: discord.TextChannel, 
        deadline: dict, 
        today: date
    ) -> bool:
        """Check and send weekly in-season reminders (e.g., power rankings).
        
        Returns True if a reminder was handled (sent or skipped).
        """
        day_of_week = deadline.get('day_of_week', '').lower()
        if not day_of_week:
            return False
        
        # Map day names to weekday numbers
        day_map = {
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6
        }
        
        target_weekday = day_map.get(day_of_week)
        if target_weekday is None or today.weekday() != target_weekday:
            return False
        
        # It's the right day - send the reminder
        season = self.current_season
        week_key = f"{today.isocalendar()[1]}"  # Week number as key
        
        if await self._reminder_already_sent(f"{deadline['id']}_{week_key}", season, 0):
            return True
        
        await self._send_reminder(channel, deadline, 0, today)
        await self._record_reminder(f"{deadline['id']}_{week_key}", season, 0)
        return True
    
    def _resolve_deadline_date(self, deadline: dict) -> Optional[date]:
        """Resolve a deadline's actual date (handling relative dates).
        
        Args:
            deadline: Deadline configuration dict
            
        Returns:
            Resolved date or None if unable to resolve
        """
        # Absolute date
        if deadline.get('date'):
            try:
                return datetime.strptime(deadline['date'], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass
        
        # Relative to NFL anchor
        relative_to = deadline.get('relative_to')
        if relative_to:
            # self.nfl_anchors, not deadlines_config: generated dates live in
            # their own file now. Reading the old location silently resolved
            # every anchor-relative deadline to None, which disabled seven
            # reminders without erroring.
            anchor_date_str = (self.nfl_anchors or {}).get(relative_to)
            
            if anchor_date_str:
                try:
                    anchor_date = datetime.strptime(anchor_date_str, '%Y-%m-%d').date()
                    offset = deadline.get('offset_days', 0)
                    return anchor_date + timedelta(days=offset)
                except (ValueError, TypeError):
                    pass
        
        # Relative to another deadline (e.g., rookie_draft)
        if relative_to and relative_to not in (self.nfl_anchors or {}):
            # Find the referenced deadline
            for other in self.deadlines_config.get('deadlines', []):
                if other.get('id') == relative_to:
                    other_date = self._resolve_deadline_date(other)
                    if other_date:
                        offset = deadline.get('offset_days', 0)
                        return other_date + timedelta(days=offset)
        
        return None
    
    async def _send_reminder(
        self, 
        channel: discord.TextChannel, 
        deadline: dict, 
        days_until: int,
        deadline_date: date
    ) -> None:
        """Send a reminder message to the appropriate channel.
        
        If deadline has 'announce: true', sends to announcements channel.
        Otherwise sends to the alert channel.
        """
        # Determine which channel to use
        should_announce = deadline.get('announce', False)
        if should_announce and self.announcements_channel_id:
            target_channel = self.bot.get_channel(self.announcements_channel_id)
            if not target_channel:
                logger.warning(f"Announcements channel {self.announcements_channel_id} not found, falling back to alert channel")
                target_channel = channel
        else:
            target_channel = channel
        
        # Determine emoji and prefix based on urgency
        if days_until == 0:
            emoji = "🚨"
            prefix = "TODAY"
            color = discord.Color.red()
        elif days_until == 1:
            emoji = "⏰"
            prefix = "TOMORROW"
            color = discord.Color.orange()
        elif days_until <= 3:
            emoji = "📅"
            prefix = f"In {days_until} days"
            color = discord.Color.gold()
        else:
            emoji = "📆"
            prefix = f"In {days_until} days"
            color = discord.Color.blue()
        
        # Create embed
        embed = discord.Embed(
            title=f"{emoji} **{prefix}**: {deadline['name']}",
            description=deadline.get('description', ''),
            color=color,
            timestamp=datetime.now(self.timezone)
        )
        
        embed.add_field(
            name="Date",
            value=deadline_date.strftime('%A, %B %d, %Y'),
            inline=True
        )
        
        embed.set_footer(text="Use /deadlines to view all upcoming deadlines")
        
        # Build content with mentions
        content_parts = []
        
        # Ping role for announcements if requested
        if deadline.get('ping_role') and self.announcements_role_id and should_announce:
            content_parts.append(f"<@&{self.announcements_role_id}>")
        
        # Ping commissioner if requested
        if deadline.get('ping_commissioner') and self.commissioner_id:
            content_parts.append(f"<@{self.commissioner_id}>")
        
        content = " ".join(content_parts) if content_parts else None
        
        try:
            await target_channel.send(content=content, embed=embed)
            channel_name = getattr(target_channel, 'name', str(target_channel.id))
            logger.info(f"Sent reminder for deadline: {deadline['id']} to #{channel_name}")
        except discord.HTTPException as e:
            logger.error(f"Failed to send reminder: {e}")
    
    async def _send_draft_preview(
        self, 
        channel: discord.TextChannel, 
        deadline: dict
    ) -> None:
        """Generate and send draft capital leaderboard for the upcoming draft.
        
        This is triggered by deadlines with auto_post_preview: true
        """
        # Get the target channel
        should_announce = deadline.get('announce', False)
        if should_announce and self.announcements_channel_id:
            target_channel = self.bot.get_channel(self.announcements_channel_id)
            if not target_channel:
                target_channel = channel
        else:
            target_channel = channel
        
        try:
            # Calculate draft capital
            results = await self._get_draft_capital()
            
            if not results:
                logger.warning("No draft capital data available for preview")
                return
            
            # Create embed
            embed = discord.Embed(
                title="🏈 Rookie Draft Capital Preview",
                description="Who has the most valuable picks heading into the draft?\n"
                           "Values from KeepTradeCut (Superflex)",
                color=discord.Color.gold(),
                timestamp=datetime.now(self.timezone)
            )
            
            # Add leaderboard - show all 12 owners
            leaderboard_lines = []
            medals = ["🥇", "🥈", "🥉"]
            
            for idx, owner in enumerate(results):
                if idx < 3:
                    prefix = medals[idx]
                else:
                    prefix = f"`{idx + 1}.`"
                
                value_str = f"{owner['total_value']:,}"
                pick_count = owner['pick_count']
                
                leaderboard_lines.append(
                    f"{prefix} **{owner['team_name']}** — {value_str} ({pick_count} picks)"
                )
            
            embed.add_field(
                name="📊 Draft Capital Rankings",
                value="\n".join(leaderboard_lines),
                inline=False
            )
            
            # Add picks breakdown for top 3
            for idx, owner in enumerate(results[:3]):
                picks = owner['picks']
                # Group by round
                round_counts = {}
                for p in picks:
                    r = p['round']
                    round_counts[r] = round_counts.get(r, 0) + 1
                
                picks_str = ", ".join(
                    f"{round_counts[r]}× R{r}" if round_counts[r] > 1 else f"R{r}"
                    for r in sorted(round_counts.keys())
                ) if round_counts else "No picks"
                
                embed.add_field(
                    name=f"{medals[idx]} {owner['team_name']}",
                    value=picks_str,
                    inline=True
                )
            
            embed.set_footer(text="Use /draftpreview for detailed breakdown")
            
            # Build content with mentions
            content_parts = []
            if deadline.get('ping_role') and self.announcements_role_id:
                content_parts.append(f"<@&{self.announcements_role_id}>")
            
            content = " ".join(content_parts) if content_parts else None
            
            await target_channel.send(content=content, embed=embed)
            channel_name = getattr(target_channel, 'name', str(target_channel.id))
            logger.info(f"Sent draft preview to #{channel_name}")
            
        except Exception as e:
            logger.error(f"Failed to send draft preview: {e}")


    
    async def _reminder_already_sent(
        self, 
        deadline_id: str, 
        season: int, 
        days_before: int
    ) -> bool:
        """Check if a reminder was already sent."""
        async with db.execute(
            """
            SELECT 1 FROM reminder_history 
            WHERE deadline_id = ? AND season = ? AND days_before = ?
            """,
            (deadline_id, season, days_before)
        ) as cursor:
            return await cursor.fetchone() is not None
    
    async def _record_reminder(
        self, 
        deadline_id: str, 
        season: int, 
        days_before: int
    ) -> None:
        """Record that a reminder was sent."""
        await db.execute(
            """
            INSERT OR IGNORE INTO reminder_history (deadline_id, season, days_before, sent_at)
            VALUES (?, ?, ?, ?)
            """,
            (deadline_id, season, days_before, datetime.now().isoformat())
        )
    
    # =========================================================================
    # Slash Commands
    # =========================================================================
    
    @app_commands.command(name="deadlines")
    @app_commands.describe(
        state="Filter by state (off_season, pre_season, in_season)",
        show_all="Show all deadlines including past ones"
    )
    async def view_deadlines(
        self, 
        interaction: discord.Interaction, 
        state: Optional[str] = None,
        show_all: bool = False
    ):
        """View upcoming league deadlines."""
        await interaction.response.defer()
        
        filter_state = state or self.current_state
        today = datetime.now(self.timezone).date()
        
        # Gather deadlines
        deadlines_list = []
        for deadline in self.deadlines_config.get('deadlines', []):
            if not deadline.get('enabled', True):
                continue
            
            if state and deadline.get('state') != state:
                continue
            
            deadline_date = self._resolve_deadline_date(deadline)
            if not deadline_date:
                deadlines_list.append((None, deadline))
                continue
            
            # Skip past deadlines unless show_all
            if not show_all and deadline_date < today:
                continue
            
            deadlines_list.append((deadline_date, deadline))
        
        # Sort by date (None dates at end)
        deadlines_list.sort(key=lambda x: (x[0] is None, x[0] or date.max))
        
        # Create embed
        embed = discord.Embed(
            title=f"📅 League Deadlines",
            description=f"Current state: **{self.current_state}**\nSeason: **{self.current_season}**",
            color=discord.Color.blue()
        )
        
        if not deadlines_list:
            embed.add_field(
                name="No deadlines found",
                value="Use `/deadline set_date` to set deadline dates.",
                inline=False
            )
        else:
            for deadline_date, deadline in deadlines_list[:10]:  # Limit to 10
                if deadline_date:
                    days_until = (deadline_date - today).days
                    if days_until == 0:
                        date_str = "🚨 **TODAY**"
                    elif days_until == 1:
                        date_str = "⏰ Tomorrow"
                    elif days_until < 0:
                        date_str = f"~~{deadline_date.strftime('%b %d')}~~ (passed)"
                    else:
                        date_str = f"{deadline_date.strftime('%b %d, %Y')} ({days_until} days)"
                else:
                    date_str = "📌 Date not set"
                
                embed.add_field(
                    name=deadline['name'],
                    value=f"{date_str}\n*{deadline.get('description', '')[:50]}...*" 
                          if len(deadline.get('description', '')) > 50 
                          else f"{date_str}\n*{deadline.get('description', '')}*",
                    inline=False
                )
        
        if len(deadlines_list) > 10:
            embed.set_footer(text=f"Showing 10 of {len(deadlines_list)} deadlines")
        
        await interaction.followup.send(embed=embed)
    
    @app_commands.command(name="state")
    @app_commands.describe(new_state="Set new state (off_season, pre_season, in_season)")
    @app_commands.choices(new_state=[
        app_commands.Choice(name="Off Season", value="off_season"),
        app_commands.Choice(name="Pre Season", value="pre_season"),
        app_commands.Choice(name="In Season", value="in_season"),
    ])
    async def manage_state(
        self, 
        interaction: discord.Interaction, 
        new_state: Optional[app_commands.Choice[str]] = None
    ):
        """View or change the current league state."""
        if new_state is None:
            # Show current state with details
            embed = discord.Embed(
                title="🏈 League State",
                color=discord.Color.blue()
            )
            
            state_emoji = {
                'off_season': '❄️',
                'pre_season': '🌱',
                'in_season': '🏈'
            }
            
            current = self.current_state
            embed.add_field(
                name="Current State",
                value=f"{state_emoji.get(current, '📌')} **{current.replace('_', ' ').title()}**",
                inline=False
            )
            embed.add_field(
                name="Season",
                value=str(self.current_season),
                inline=True
            )
            
            # Show state-specific deadlines count
            active_count = sum(
                1 for d in self.deadlines_config.get('deadlines', [])
                if d.get('state') == current and d.get('enabled', True)
            )
            embed.add_field(
                name="Active Deadlines",
                value=str(active_count),
                inline=True
            )
            
            await interaction.response.send_message(embed=embed)
        else:
            # Change state
            old_state = self.current_state
            self.state_config['current_state'] = new_state.value
            self._save_state()
            
            embed = discord.Embed(
                title="✅ League State Changed",
                description=f"**{old_state.replace('_', ' ').title()}** → **{new_state.value.replace('_', ' ').title()}**",
                color=discord.Color.green()
            )
            
            await interaction.response.send_message(embed=embed)
            logger.info(f"League state changed: {old_state} -> {new_state.value}")
    
    async def _target_season(self) -> int:
        """The NFL season anchors should describe.

        Sleeper's `/state/nfl` knows this outright. The month-based guess this
        replaced (`year - 1` before September) meant that running /sync_nfl in
        July 2026 wrote the *2025* opener and preseason - a full year stale,
        and the reason `nfl_anchors` currently holds 2025 dates.
        """
        try:
            nfl_state = await self.bot.sleeper.get_nfl_state()
        except Exception as e:
            logger.warning(f"Could not fetch NFL state for anchor sync: {e}")
            nfl_state = None

        try:
            league = await self.bot.sleeper.get_league(self.bot.league_id)
        except Exception as e:
            logger.warning(f"Could not fetch league for anchor sync: {e}")
            league = {}

        return upcoming_season(league, nfl_state=nfl_state)

    async def _rookie_draft_end(self, season: int) -> Optional[date]:
        """The date the season's rookie draft finished, or None if unfinished.

        Owners get 24 hours per pick, so a draft spans days or weeks - 2021's
        ran Jun 19 to Jul 2. `last_picked` is therefore the only meaningful
        completion date; `start_time` says nothing about when rosters settled.

        None means "no completed draft for this season", which callers must read
        as "the taxi window can't have closed yet" rather than as a date.
        """
        try:
            drafts = await self.bot.sleeper.get_drafts(self.bot.league_id)
        except Exception as e:
            logger.warning(f"Could not fetch drafts for {season}: {e}")
            return None

        for draft in drafts:
            if str(draft.get('season')) != str(season):
                continue
            if draft.get('status') != 'complete':
                logger.info(
                    f"{season} draft status is {draft.get('status')!r}, "
                    "not complete"
                )
                continue
            stamp = draft.get('last_picked') or draft.get('start_time')
            if stamp:
                return datetime.fromtimestamp(stamp / 1000).date()
        return None

    async def _fetch_anchors(self, season: int) -> dict[str, Optional[date]]:
        """Collect every NFL anchor for a season, from both sources.

        nflverse covers the regular season and playoffs. It publishes no
        preseason games at all, so preseason dates and the taxi deadline that
        depends on them come from ESPN - see `lib/nfl_calendar.py`.
        """
        self.nfl_client.clear_cache()
        anchors = self.nfl_client.get_all_anchors(season)

        # get_all_anchors returns the *previous* season's Super Bowl, which is
        # the offseason anchor. This season's is the one that closes it out.
        try:
            current_sb = self.nfl_client.get_super_bowl_date(season)
            if current_sb:
                anchors['super_bowl'] = current_sb
        except Exception as e:
            logger.warning(f"Could not fetch {season} Super Bowl date: {e}")

        # When the rookie draft actually finished. Re-read every sync rather
        # than assumed, because the date moves: the draft goes to whatever
        # weekend owners can make and then runs 24 hours per pick. It also
        # drives the league state (a completed draft means pre_season).
        anchors[ANCHOR_ROOKIE_DRAFT_END] = await self._rookie_draft_end(season)

        # No taxi deadline anchor: it's the regular-season opener minus a day,
        # so storing it would duplicate a value already here and let the copy go
        # stale. stored_taxi_deadline derives it.

        # Preseason dates are informational only now that the taxi deadline no
        # longer depends on them - nothing in deadlines.yaml references them.
        # Fetched anyway because nflverse carries no preseason games at all and
        # they're useful for the commissioner to see.
        weeks = await ESPNClient(self.bot.sleeper.session).get_preseason_weeks(
            season
        )
        if weeks:
            start, end = preseason_bounds(weeks)
            anchors[ANCHOR_PRESEASON_START] = start
            anchors[ANCHOR_PRESEASON_END] = end
        else:
            logger.info(f"No ESPN preseason schedule for {season} yet")

        return anchors

    def _store_anchors(self, anchors: dict[str, Optional[date]]) -> None:
        """Persist anchors as ISO dates in the bot-owned anchors file.

        Deliberately not deadlines.yaml: that file is hand-maintained and
        yaml.dump would strip its comments on every automatic sync.
        """
        self.nfl_anchors = {
            k: v.isoformat() if v else None
            for k, v in anchors.items()
        }
        save_anchors(self.nfl_anchors)

    @app_commands.command(name="sync_nfl")
    @app_commands.describe(
        year="NFL season year to sync (defaults to the season being prepared for)"
    )
    async def sync_nfl_schedule(
        self,
        interaction: discord.Interaction,
        year: Optional[int] = None
    ):
        """Sync deadline anchors from the NFL schedule.

        Regular season and playoffs come from nflreadpy; preseason dates and
        the taxi deadline come from ESPN, which is the only source that
        publishes them. Both are released once a year, so this only needs
        running annually - and `upkeep_loop` now does it automatically.
        """
        await interaction.response.defer()

        try:
            season = year or await self._target_season()
            anchors = await self._fetch_anchors(season)
            season_label = f"{season} NFL season"

            self._store_anchors(anchors)

            embed = discord.Embed(
                title="🏈 NFL Schedule Synced",
                description=f"Updated anchors from **{season_label}**:",
                color=discord.Color.green()
            )
            
            for key, value in anchors.items():
                display_name = key.replace('_', ' ').title()
                if value:
                    embed.add_field(
                        name=display_name,
                        value=value.strftime('%B %d, %Y'),
                        inline=True
                    )
                else:
                    embed.add_field(
                        name=display_name,
                        value="*Not available yet*",
                        inline=True
                    )
            
            await interaction.followup.send(embed=embed)
            logger.info(f"Synced NFL schedule anchors from {season_label}")
            
        except Exception as e:
            logger.error(f"Failed to sync NFL schedule: {e}")
            await interaction.followup.send(
                f"❌ Failed to sync NFL schedule: {e}",
                ephemeral=True
            )

    @app_commands.command(name="sync_sleeper")
    async def sync_sleeper_schedule(self, interaction: discord.Interaction):
        """Sync draft date and league info from Sleeper.
        
        Fetches the rookie draft start time from Sleeper and updates
        the rookie_draft deadline date.
        """
        await interaction.response.defer()
        
        try:
            from config import SLEEPER_LEAGUE_ID
            
            # Get drafts from Sleeper
            drafts = await self.bot.sleeper.get_drafts(SLEEPER_LEAGUE_ID)
            
            if not drafts:
                await interaction.followup.send(
                    "❌ No drafts found for this league.",
                    ephemeral=True
                )
                return
            
            # Find the most recent/upcoming draft
            # Drafts have 'status' (pre_draft, drafting, complete) and 'start_time'
            upcoming_draft = None
            for draft in drafts:
                status = draft.get('status', '')
                if status in ['pre_draft', 'drafting']:
                    upcoming_draft = draft
                    break
            
            # If no upcoming draft, show the most recent one
            if not upcoming_draft and drafts:
                # Sort by start_time (descending) to get most recent
                drafts_with_time = [d for d in drafts if d.get('start_time')]
                if drafts_with_time:
                    drafts_with_time.sort(key=lambda x: x.get('start_time', 0), reverse=True)
                    upcoming_draft = drafts_with_time[0]
            
            embed = discord.Embed(
                title="📋 Sleeper League Synced",
                color=discord.Color.green()
            )
            
            if upcoming_draft:
                start_time = upcoming_draft.get('start_time')
                draft_type = upcoming_draft.get('type', 'unknown')
                status = upcoming_draft.get('status', 'unknown')
                
                if start_time:
                    # Sleeper returns timestamp in milliseconds
                    draft_datetime = datetime.fromtimestamp(start_time / 1000, tz=self.timezone)
                    draft_date = draft_datetime.date()
                    
                    # Update the rookie_draft deadline
                    for idx, deadline in enumerate(self.deadlines_config.get('deadlines', [])):
                        if deadline.get('id') == 'rookie_draft':
                            self.deadlines_config['deadlines'][idx]['date'] = draft_date.isoformat()
                            # Clear relative_to if it was set
                            if 'relative_to' in self.deadlines_config['deadlines'][idx]:
                                del self.deadlines_config['deadlines'][idx]['relative_to']
                            break
                    
                    self._save_deadlines()
                    
                    embed.add_field(
                        name="🏈 Rookie Draft",
                        value=f"{draft_datetime.strftime('%B %d, %Y at %I:%M %p %Z')}\n"
                              f"Status: {status.replace('_', ' ').title()}\n"
                              f"Type: {draft_type.replace('_', ' ').title()}",
                        inline=False
                    )
                    embed.add_field(
                        name="✅ Updated",
                        value="Rookie draft deadline has been set!",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name="🏈 Rookie Draft",
                        value=f"Found draft (status: {status}) but no start time set yet.\n"
                              "Set the draft time in Sleeper, then run this command again.",
                        inline=False
                    )
            else:
                embed.add_field(
                    name="⚠️ No Draft Found",
                    value="No upcoming drafts found. Create your rookie draft in Sleeper first.",
                    inline=False
                )
            
            # Also show league info
            league = await self.bot.sleeper.get_league(SLEEPER_LEAGUE_ID)
            if league:
                embed.add_field(
                    name="League",
                    value=league.get('name', 'Unknown'),
                    inline=True
                )
                embed.add_field(
                    name="Season",
                    value=str(league.get('season', 'Unknown')),
                    inline=True
                )
            
            await interaction.followup.send(embed=embed)
            logger.info("Synced Sleeper league data")
            
        except Exception as e:
            logger.error(f"Failed to sync Sleeper data: {e}")
            await interaction.followup.send(
                f"❌ Failed to sync Sleeper data: {e}",
                ephemeral=True
            )

    
    @app_commands.command(name="deadline")
    @app_commands.describe(
        action="Action to perform",
        deadline_id="ID of the deadline (use /deadlines to see IDs)",
        date="New date in YYYY-MM-DD format (for set_date action)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Set Date", value="set_date"),
        app_commands.Choice(name="Mark Complete", value="complete"),
        app_commands.Choice(name="Enable", value="enable"),
        app_commands.Choice(name="Disable", value="disable"),
        app_commands.Choice(name="Info", value="info"),
    ])
    async def manage_deadline(
        self, 
        interaction: discord.Interaction, 
        action: app_commands.Choice[str],
        deadline_id: str,
        date: Optional[str] = None
    ):
        """Manage a specific deadline."""
        # Find the deadline
        deadline = None
        deadline_idx = None
        for idx, d in enumerate(self.deadlines_config.get('deadlines', [])):
            if d.get('id') == deadline_id:
                deadline = d
                deadline_idx = idx
                break
        
        if not deadline:
            await interaction.response.send_message(
                f"❌ Deadline `{deadline_id}` not found. Use `/deadlines show_all:true` to see all deadline IDs.",
                ephemeral=True
            )
            return
        
        if action.value == "info":
            # Show detailed info
            embed = discord.Embed(
                title=f"📋 {deadline['name']}",
                description=deadline.get('description', ''),
                color=discord.Color.blue()
            )
            
            resolved_date = self._resolve_deadline_date(deadline)
            embed.add_field(
                name="Date",
                value=resolved_date.strftime('%B %d, %Y') if resolved_date else "Not set",
                inline=True
            )
            embed.add_field(
                name="State",
                value=deadline.get('state', 'any').replace('_', ' ').title(),
                inline=True
            )
            embed.add_field(
                name="Enabled",
                value="✅ Yes" if deadline.get('enabled', True) else "❌ No",
                inline=True
            )
            embed.add_field(
                name="Reminders",
                value=', '.join(f"{d} day{'s' if d != 1 else ''}" for d in deadline.get('reminders', [1, 0])),
                inline=True
            )
            embed.add_field(
                name="ID",
                value=f"`{deadline_id}`",
                inline=True
            )
            
            await interaction.response.send_message(embed=embed)
            
        elif action.value == "set_date":
            if not date:
                await interaction.response.send_message(
                    "❌ Please provide a date in YYYY-MM-DD format.",
                    ephemeral=True
                )
                return
            
            try:
                parsed_date = datetime.strptime(date, '%Y-%m-%d').date()
            except ValueError:
                await interaction.response.send_message(
                    "❌ Invalid date format. Please use YYYY-MM-DD (e.g., 2026-06-15).",
                    ephemeral=True
                )
                return
            
            self.deadlines_config['deadlines'][deadline_idx]['date'] = date
            # Clear relative_to if setting absolute date
            if 'relative_to' in self.deadlines_config['deadlines'][deadline_idx]:
                del self.deadlines_config['deadlines'][deadline_idx]['relative_to']
            self._save_deadlines()
            
            await interaction.response.send_message(
                f"✅ Set **{deadline['name']}** to **{parsed_date.strftime('%B %d, %Y')}**"
            )
            logger.info(f"Set deadline {deadline_id} to {date}")
            
        elif action.value == "complete":
            season = self.current_season
            await db.execute(
                """
                INSERT OR REPLACE INTO deadline_completions 
                (deadline_id, season, completed_at, completed_by)
                VALUES (?, ?, ?, ?)
                """,
                (deadline_id, season, datetime.now().isoformat(), str(interaction.user.id))
            )
            
            await interaction.response.send_message(
                f"✅ Marked **{deadline['name']}** as complete for {season} season!"
            )
            logger.info(f"Marked deadline {deadline_id} complete by {interaction.user}")
            
        elif action.value == "enable":
            self.deadlines_config['deadlines'][deadline_idx]['enabled'] = True
            self._save_deadlines()
            await interaction.response.send_message(
                f"✅ Enabled deadline: **{deadline['name']}**"
            )
            
        elif action.value == "disable":
            self.deadlines_config['deadlines'][deadline_idx]['enabled'] = False
            self._save_deadlines()
            await interaction.response.send_message(
                f"✅ Disabled deadline: **{deadline['name']}**"
            )
    
    @manage_deadline.autocomplete('deadline_id')
    async def deadline_id_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete for deadline IDs."""
        choices = []
        for deadline in self.deadlines_config.get('deadlines', []):
            deadline_id = deadline.get('id', '')
            name = deadline.get('name', deadline_id)
            if current.lower() in deadline_id.lower() or current.lower() in name.lower():
                choices.append(app_commands.Choice(
                    name=f"{name} ({deadline_id})",
                    value=deadline_id
                ))
        return choices[:25]  # Discord limit
    
    @app_commands.command(name="set_season")
    @app_commands.describe(year="The season year (e.g., 2026)")
    async def set_season(self, interaction: discord.Interaction, year: int):
        """Set the current fantasy season year."""
        if year < 2020 or year > 2100:
            await interaction.response.send_message(
                "❌ Please provide a valid year between 2020 and 2100.",
                ephemeral=True
            )
            return
        
        old_year = self.current_season
        self.state_config['season']['year'] = year
        self._save_state()
        
        await interaction.response.send_message(
            f"✅ Season year changed: **{old_year}** → **{year}**\n"
            f"Run `/sync_nfl` to fetch NFL schedule data for this season."
        )
        logger.info(f"Season year changed: {old_year} -> {year}")
    
    @app_commands.command(name="set_commissioner")
    @app_commands.describe(user="The commissioner to ping for reminders")
    async def set_commissioner(
        self, 
        interaction: discord.Interaction, 
        user: discord.Member
    ):
        """Set the commissioner for reminder pings."""
        self.state_config['commissioner']['discord_id'] = user.id
        self._save_state()
        
        await interaction.response.send_message(
            f"✅ Commissioner set to {user.mention}. They will be pinged for relevant deadline reminders."
        )
        logger.info(f"Commissioner set to {user.id}")
    
    # =========================================================================
    # Draft Preview
    # =========================================================================
    
    def _load_pick_values(self) -> dict[int, int]:
        """Load KTC pick values from config.
        
        Returns:
            Dict mapping overall pick number (1-60) to KTC value
        """
        try:
            with open(PICK_VALUES_PATH, 'r') as f:
                config = yaml.safe_load(f)
                return config.get('pick_values', {})
        except FileNotFoundError:
            logger.warning("pick_values.yaml not found, using defaults")
            # Fallback values
            return {i: max(7000 - (i * 100), 1000) for i in range(1, 61)}
    
    def _get_pick_value(self, overall_pick: int) -> int:
        """Get the KTC value for a specific overall draft pick.
        
        Args:
            overall_pick: Overall pick number (1-60)
            
        Returns:
            KTC value for the pick
        """
        pick_values = self._load_pick_values()
        return pick_values.get(overall_pick, 1000)
    
    async def _get_draft_capital(self) -> list[dict]:
        """Calculate draft capital for each owner for the upcoming draft.
        
        Uses the draft order calculation from the draft cog combined with
        traded picks to determine who owns which pick.
        
        Returns:
            List of dicts with owner info and pick values, sorted by total value
        """
        from config import SLEEPER_LEAGUE_ID
        from cogs.draft import calculate_payouts, calculate_draft_order, DraftCalculator
        
        # Get league data
        league = await self.bot.sleeper.get_league(SLEEPER_LEAGUE_ID)
        league_season = league.get('season', str(datetime.now().year))
        
        # The upcoming draft is for next year if the league season has completed
        # (League "season" is the year the season started, e.g., 2025 for 2025-26 season)
        # The rookie draft in off-season is for the year after the league season
        upcoming_draft_year = str(int(league_season) + 1)
        logger.debug(f"League season: {league_season}, upcoming draft year: {upcoming_draft_year}")
        
        # Get rosters, users, and traded picks
        rosters = await self.bot.sleeper.get_rosters(SLEEPER_LEAGUE_ID)
        users = await self.bot.sleeper.get_users(SLEEPER_LEAGUE_ID)
        traded_picks = await self.bot.sleeper.get_traded_picks(SLEEPER_LEAGUE_ID)
        
        # Build user lookup: user_id -> user data
        user_lookup = {u.get("user_id"): u for u in users}
        
        # Build roster_id -> owner_id (user_id) mapping
        roster_to_owner = {r.get("roster_id"): r.get("owner_id") for r in rosters}
        
        # Use the DraftCalculator cog's _fetch_team_stats to get proper MaxPF calculation
        # This includes playoff weeks and properly calculates optimal lineup each week
        draft_cog = self.bot.get_cog("DraftCalculator")
        if draft_cog:
            team_list = await draft_cog._fetch_team_stats(season=int(league_season))
        else:
            # Fallback if draft cog not loaded - create minimal team stats
            from cogs.draft import TeamStats
            team_list = []
            for roster in rosters:
                roster_id = roster.get("roster_id")
                owner_id = roster.get("owner_id", "")
                settings = roster.get("settings", {})
                
                user = user_lookup.get(owner_id, {})
                team_name = user.get("metadata", {}).get("team_name") or user.get("display_name", f"Team {roster_id}")
                
                team_list.append(TeamStats(
                    roster_id=roster_id,
                    team_name=team_name,
                    owner_id=owner_id,
                    wins=settings.get("wins", 0),
                    losses=settings.get("losses", 0),
                ))
        
        # Build roster_id -> TeamStats mapping for lookup
        teams = {team.roster_id: team for team in team_list}
        
        # Calculate draft order (who picks in which slot)
        # This gives us a list where index 0 = first pick
        calculate_payouts(team_list)  # Need payouts for draft order calc
        draft_order = calculate_draft_order(team_list)
        
        # Build draft slot -> roster_id mapping
        # draft_order[0] picks first (1.01), draft_order[11] picks 12th (1.12)
        slot_to_roster = {i + 1: team.roster_id for i, team in enumerate(draft_order)}

        
        # Build traded picks lookup: (season, round, original_roster_id) -> new_owner_roster_id
        # Note: Sleeper's traded_picks 'owner_id' field is actually a ROSTER_ID, not a user_id
        traded_lookup = {}
        for pick in traded_picks:
            key = (str(pick.get('season')), pick.get('round'), pick.get('roster_id'))
            # The 'owner_id' in traded picks is the roster_id of the new owner
            traded_lookup[key] = pick.get('owner_id')
        
        logger.debug(f"Traded picks for {upcoming_draft_year}: {[(k, v) for k, v in traded_lookup.items() if k[0] == upcoming_draft_year]}")
        
        # Now calculate who owns each pick
        # Initialize owner picks: user_id -> list of picks
        owner_picks = {roster.get('owner_id'): [] for roster in rosters if roster.get('owner_id')}
        
        num_teams = len(draft_order)
        num_rounds = 5
        
        for round_num in range(1, num_rounds + 1):
            for slot in range(1, num_teams + 1):
                # Get the roster_id that this slot belongs to
                original_roster_id = slot_to_roster.get(slot)
                if not original_roster_id:
                    continue
                
                # Check if this pick was traded
                key = (upcoming_draft_year, round_num, original_roster_id)
                if key in traded_lookup:
                    # traded_lookup returns the new owner's ROSTER_ID
                    new_owner_roster_id = traded_lookup[key]
                    # Convert roster_id to user_id
                    current_owner_id = roster_to_owner.get(new_owner_roster_id)
                else:
                    current_owner_id = roster_to_owner.get(original_roster_id)
                
                if not current_owner_id or current_owner_id not in owner_picks:
                    continue
                
                # Calculate overall pick number
                overall_pick = (round_num - 1) * num_teams + slot
                value = self._get_pick_value(overall_pick)
                
                # Format pick display (e.g., "1.03" for 3rd pick in round 1)
                pick_display = f"{round_num}.{slot:02d}"
                
                owner_picks[current_owner_id].append({
                    'round': round_num,
                    'slot': slot,
                    'overall': overall_pick,
                    'pick_display': pick_display,
                    'value': value,
                    'original_team': teams[original_roster_id].team_name if original_roster_id in teams else "Unknown",
                    'is_own_pick': current_owner_id == roster_to_owner.get(original_roster_id)
                })

        
        # Calculate totals and format results
        results = []
        for owner_id, picks in owner_picks.items():
            user = user_lookup.get(owner_id, {})
            display_name = user.get('display_name', 'Unknown')
            team_name = user.get('metadata', {}).get('team_name', display_name)
            
            total_value = sum(p['value'] for p in picks)
            
            # Sort picks by overall pick number
            picks.sort(key=lambda p: p['overall'])
            
            results.append({
                'owner_id': owner_id,
                'display_name': display_name,
                'team_name': team_name,
                'total_value': total_value,
                'picks': picks,
                'pick_count': len(picks)
            })
        
        # Sort by total value descending
        results.sort(key=lambda x: x['total_value'], reverse=True)
        return results

    
    @app_commands.command(name="draftpreview")
    async def draft_preview(
        self, 
        interaction: discord.Interaction,
    ):
        """Show draft capital leaderboard - who has the most valuable picks!
        
        Uses KeepTradeCut values to rank owners by total draft capital for the upcoming draft.
        """
        await interaction.response.defer()
        
        try:
            results = await self._get_draft_capital()
            
            if not results:
                await interaction.followup.send(
                    "❌ Couldn't calculate draft capital. Is the league set up correctly?",
                    ephemeral=True
                )
                return
            
            # Create embed
            embed = discord.Embed(
                title="🏈 Draft Capital Leaderboard",
                description="**Upcoming Rookie Draft** | Values from KeepTradeCut (Superflex)",
                color=discord.Color.gold()
            )
            
            # Add leaderboard - show ALL 12 owners
            leaderboard_lines = []
            medals = ["🥇", "🥈", "🥉"]
            
            for idx, owner in enumerate(results):
                if idx < 3:
                    prefix = medals[idx]
                else:
                    prefix = f"`{idx + 1}.`"
                
                value_str = f"{owner['total_value']:,}"
                pick_count = owner['pick_count']
                
                leaderboard_lines.append(
                    f"{prefix} **{owner['team_name']}** — {value_str} ({pick_count} picks)"
                )
            
            embed.add_field(
                name="📊 Rankings",
                value="\n".join(leaderboard_lines),
                inline=False
            )
            
            # Add pick breakdown for top 3
            for idx, owner in enumerate(results[:3]):
                picks = owner['picks']
                # Group by round and show which picks
                pick_strs = []
                for p in picks:
                    # Show if acquired through trade
                    if p['is_own_pick']:
                        pick_strs.append(f"**{p['pick_display']}**")
                    else:
                        pick_strs.append(f"{p['pick_display']} (from {p['original_team']})")
                
                picks_display = ", ".join(pick_strs[:10]) if pick_strs else "No picks"
                if len(pick_strs) > 10:
                    picks_display += f" +{len(pick_strs) - 10} more"
                
                embed.add_field(
                    name=f"{medals[idx]} {owner['team_name']}",
                    value=picks_display[:1024],
                    inline=False
                )
            
            embed.set_footer(text="Use /draftcapital to see your detailed breakdown")
            
            await interaction.followup.send(embed=embed)
            logger.info("Generated draft preview")
            
        except Exception as e:
            logger.error(f"Failed to generate draft preview: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(
                f"❌ Failed to generate draft preview: {e}",
                ephemeral=True
            )

    
    @app_commands.command(name="draftcapital")
    @app_commands.describe(
        user="Check specific user's draft capital"
    )
    async def draft_capital_detail(
        self, 
        interaction: discord.Interaction,
        user: Optional[discord.Member] = None
    ):
        """Show detailed draft capital breakdown for a user."""
        await interaction.response.defer()
        
        try:
            results = await self._get_draft_capital()
            
            # Try to find the user by matching Discord ID using member registry
            from lib.members import get_member_registry
            registry = get_member_registry()
            
            target_discord_id = str(user.id if user else interaction.user.id)
            target = None
            
            # First try to match using member registry
            for owner in results:
                owner_id = owner.get('owner_id')
                # Check if any member's sleeper username matches this owner
                for member in registry.members:
                    if str(member.discord_id) == target_discord_id:
                        # Check if this member owns this team
                        for username in member.sleeper_usernames:
                            if username.lower() in owner.get('display_name', '').lower():
                                target = owner
                                break
                    if target:
                        break
                if target:
                    break
            
            # If no match found, just show the first/top user
            if not target and results:
                target = results[0]
            
            if not target:
                await interaction.followup.send(
                    "❌ Couldn't find draft capital data.",
                    ephemeral=True
                )
                return
            
            embed = discord.Embed(
                title=f"📋 Draft Capital: {target['team_name']}",
                description=f"Total Value: **{target['total_value']:,}** KTC points",
                color=discord.Color.blue()
            )
            
            # Group picks by round
            picks_by_round = {}
            for p in target['picks']:
                r = p['round']
                if r not in picks_by_round:
                    picks_by_round[r] = []
                picks_by_round[r].append(p)
            
            # Add picks by round
            for round_num in sorted(picks_by_round.keys()):
                picks = picks_by_round[round_num]
                round_total = sum(p['value'] for p in picks)
                
                pick_lines = []
                for p in picks:
                    if p['is_own_pick']:
                        pick_lines.append(f"**{p['pick_display']}** — {p['value']:,}")
                    else:
                        pick_lines.append(f"{p['pick_display']} (from {p['original_team']}) — {p['value']:,}")
                
                embed.add_field(
                    name=f"Round {round_num} — {round_total:,} total",
                    value="\n".join(pick_lines) or "No picks",
                    inline=False
                )
            
            # Add rank
            rank = next((i + 1 for i, o in enumerate(results) if o['owner_id'] == target['owner_id']), "?")
            embed.set_footer(text=f"Rank: #{rank} of {len(results)} owners")
            
            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Failed to get draft capital detail: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(
                f"❌ Failed to get draft capital: {e}",
                ephemeral=True
            )



async def setup(bot: DynastyBot):
    """Set up the scheduler cog."""
    await bot.add_cog(SchedulerCog(bot))


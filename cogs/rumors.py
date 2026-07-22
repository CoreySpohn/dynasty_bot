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
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import discord
import yaml
from discord import app_commands
from discord.ext import commands, tasks

from cogs.trade_values import (
    get_latest_snapshot,
    get_team_dynasty_values,
    get_value_trend,
    normalize_name,
)
from config import SLEEPER_LEAGUE_ID
from lib.ai_client import GeminiClient
from lib.claude_client import ClaudeClient
from lib.members import get_member_registry
from lib.openai_client import OpenAIClient

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.rumors")

# Load reporters config
CONFIG_PATH = Path(__file__).parent.parent / "config" / "reporters.yaml"

# Occasional delivery notes to break up the "press release" cadence, since
# every rumor otherwise reads like a dramatic 2-4 sentence news report.
STYLE_NOTES = [
    "Keep this VERY casual - like a one-line text to the group chat. "
    "Lowercase is fine, no dramatic buildup, one sentence.",
    "Deliver this as a short, offhand comment someone would drop "
    "mid-conversation, not a formal report.",
]

# What extra context (if any) rides along with a rumor's topic, as a single
# weighted roll - like a D&D random table - rather than a pile of
# independent booleans that all tend to fire together. Every rumor used to
# get the SAME full-roster dump every time, which is exactly why they read
# as formulaic ("mentions these three things, must be random"). Now exactly
# one of these fires per generation, and several of them chain onto the
# specific owner/player the topic's table roll already picked (see
# _build_context_block), so the extra color feels attached to the rumor
# instead of injected wholesale.
CONTEXT_MODULES = [
    ("full_roster", 0.15),   # the old always-on behavior, now just occasional
    ("single_roster", 0.20), # one owner's roster - chains to the topic's owner if there is one
    ("player_value", 0.20),  # a player's KTC dynasty value/rank - chains to the topic's player
    ("team_value", 0.15),    # an owner's total dynasty roster value/rank
    ("value_trend", 0.15),   # 7-day KTC value swing for a player or owner
    ("none", 0.15),          # deliberately no extra context at all
]


def _normalize_for_matching(text: str) -> str:
    """Lowercase + strip punctuation for freeform-text entity matching.

    Deliberately does NOT strip generational suffixes the way
    trade_values.normalize_name does for cross-source player matching -
    "Rob Jr." and "Rob Sr." (both real owners in this league) need to
    stay distinguishable here, not collapse to the same "rob".
    """
    return re.sub(r"[^a-z0-9 ]", "", text.lower())


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

        # Discord hard-caps a select menu at 25 options total. Reserve one
        # slot for "Random Reporter" below and sample the rest so a growing
        # reporter roster doesn't crash this dropdown - "Random Reporter"
        # still draws from the full (weighted) pool regardless of what's
        # shown here.
        shown_reporters = (
            random.sample(reporters, 24) if len(reporters) > 24 else reporters
        )

        options = [
            discord.SelectOption(
                label=r.get("name", "Unknown"),
                emoji=r.get("emoji", "📰"),
                description=r.get("name", "")[:50],
            )
            for r in shown_reporters
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
        subject = await self.cog._extract_subject_from_text(self.rumor_text)
        member_context = await self.cog._build_context_block(subject)

        # Rewrite the rumor
        try:
            rewritten = await self.cog._get_ai_client().rewrite_as_reporter(
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

        # A pool of AI backends to pick from at random for each generation.
        # Different models write in noticeably different voices, so rotating
        # between them adds variety on top of the reporter personas
        # themselves (which alone tend to sound similar since they all run
        # through the same underlying model).
        self.ai_clients = [
            client
            for client in [
                GeminiClient(),
                ClaudeClient(model_name="claude-haiku-4-5"),
                ClaudeClient(model_name="claude-sonnet-5"),
                OpenAIClient(model_name="gpt-5.6-luna"),
                OpenAIClient(model_name="gpt-5.6-terra"),
            ]
            if client.client is not None
        ]
        if not self.ai_clients:
            logger.warning("No AI backends configured (missing API keys); rumors will use fallback text")
            self.ai_clients = [GeminiClient()]

        # Load rumor generation tables
        self.rumor_tables = self._load_rumor_tables()
        
        # Channels for different rumor types
        self.rumors_channel_id = int(os.getenv("RUMORS_CHANNEL_ID", 0))
        self.nfl_channel_id = int(os.getenv("NFL_CHANNEL_ID", 0)) or self.rumors_channel_id
        
        # Cache for team names
        self._team_names: Optional[list[str]] = None

        # Recent picks, so back-to-back rumors don't repeat the same
        # reporter/template, and recently posted rumors we can occasionally
        # follow up on for continuity.
        self._recent_reporter_names: deque[str] = deque(maxlen=2)
        self._recent_templates: deque[str] = deque(maxlen=3)
        self._recent_rumors: deque[tuple[str, str]] = deque(maxlen=3)

        # Start random rumor task
        self.random_rumor_task.start()
    
    def _load_rumor_tables(self) -> dict:
        """Load rumor generation tables from YAML."""
        tables_path = Path(__file__).parent.parent / "config" / "rumor_tables.yaml"
        try:
            with open(tables_path, "r") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("rumor_tables.yaml not found")
            return {}
        except Exception as e:
            logger.error(f"Failed to load rumor tables: {e}")
            return {}
    
    async def _generate_from_tables(
        self, owner_data: list[dict], category: Optional[str] = None
    ) -> tuple[str, dict]:
        """Generate a rumor base by randomly filling a template with table values.

        Args:
            owner_data: List of dicts with 'name', 'team_name', 'players' keys
            category: Optional category to restrict templates to ("trade",
                "draft", "drama", "general"). Falls back to the full mix if
                the category is unknown or omitted.

        Returns:
            Tuple of (filled-in rumor template string, subject). subject is
            {"owner": <owner_data dict or None>, "player_name": <str or None>}
            for whichever owner1/player1 the template happened to roll, if
            any - so a later, independent context roll can chain onto the
            SAME entity the topic is actually about instead of a random one.
        """
        templates_by_category = self.rumor_tables.get("templates") if self.rumor_tables else None
        if not templates_by_category:
            return "a mysterious trade brewing in the league", {}

        if category and category in templates_by_category:
            pool = templates_by_category[category]
        else:
            pool = [t for templates in templates_by_category.values() for t in templates]

        if not pool:
            return "a mysterious trade brewing in the league", {}

        # Pick a random template, avoiding ones used in the last few generations
        candidates = [t for t in pool if t not in self._recent_templates]
        template = random.choice(candidates or pool)
        self._recent_templates.append(template)

        # Find all placeholders in the template
        placeholders = re.findall(r"\{(\w+)\}", template)
        
        # Build replacement dict and track selected owners
        replacements = {}
        owner1_data = None
        owner2_data = None
        
        for placeholder in placeholders:
            if placeholder == "owner1":
                if owner_data:
                    owner1_data = random.choice(owner_data)
                    replacements["owner1"] = owner1_data["name"]
                else:
                    replacements["owner1"] = "An owner"
            elif placeholder == "owner2":
                available = [o for o in owner_data if o != owner1_data]
                if available:
                    owner2_data = random.choice(available)
                    replacements["owner2"] = owner2_data["name"]
                else:
                    replacements["owner2"] = "another owner"
            elif placeholder == "player1":
                # Pick a player from owner1's roster
                if owner1_data and owner1_data.get("players"):
                    replacements["player1"] = random.choice(owner1_data["players"])
                else:
                    replacements["player1"] = "a key player"
            elif placeholder == "player2":
                # Pick a player from owner2's roster (or random)
                if owner2_data and owner2_data.get("players"):
                    replacements["player2"] = random.choice(owner2_data["players"])
                elif owner1_data and owner1_data.get("players"):
                    replacements["player2"] = random.choice(owner1_data["players"])
                else:
                    replacements["player2"] = "a sleeper pickup"
            elif placeholder == "random_player":
                # Pick any player from any roster
                all_players = [p for o in owner_data for p in o.get("players", [])]
                replacements["random_player"] = random.choice(all_players) if all_players else "a breakout candidate"
            elif placeholder in self.rumor_tables:
                # Fill from the corresponding table
                replacements[placeholder] = random.choice(self.rumor_tables[placeholder])
            else:
                # Unknown placeholder, leave empty
                replacements[placeholder] = f"[{placeholder}]"
        
        # Fill in the template
        subject = {"owner": owner1_data, "player_name": replacements.get("player1")}
        try:
            return template.format(**replacements), subject
        except Exception as e:
            logger.error(f"Error filling rumor template: {e}")
            return template, subject

    async def _get_owner_data_for_rumors(self) -> list[dict]:
        """Build owner data with roster player names for rumor generation."""
        registry = get_member_registry()
        
        try:
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users = await self.bot.sleeper.get_users(self.league_id)
            players = await self.bot.sleeper.get_all_players()
            
            # Build user lookup
            user_lookup = {u["user_id"]: u for u in users}
            
            owner_data = []
            for roster in rosters:
                owner_id = roster.get("owner_id")
                user = user_lookup.get(owner_id, {})
                
                # Get member from registry
                member = registry.find_by_sleeper_id(owner_id)
                owner_name = member.name if member else user.get("display_name", "Unknown")
                team_name = user.get("metadata", {}).get("team_name") or user.get("display_name", "")
                
                # Get top player names from roster (starters + some bench)
                roster_players = roster.get("players", [])[:15]  # Top 15 players
                player_names = []
                for pid in roster_players:
                    p = players.get(pid, {})
                    name = p.get("full_name")
                    if name:
                        player_names.append(name)
                
                owner_data.append({
                    "name": owner_name,
                    "team_name": team_name,
                    "players": player_names,
                })
            
            return owner_data
        except Exception as e:
            logger.error(f"Failed to get owner data: {e}")
            return [{"name": m.name, "team_name": "", "players": []} for m in registry.members]

    async def _get_recent_matchup_highlight(self) -> Optional[str]:
        """Look at last week's real matchup results for a rumor to riff on.

        Grounding a rumor in an actual result (a nailbiter, a blowout) reads
        far more like something a real league member would say than a fully
        made-up topic.
        """
        registry = get_member_registry()

        try:
            nfl_state = await self.bot.sleeper.get_nfl_state()
            week = (nfl_state.get("week") or 1) - 1
            if week < 1:
                return None

            matchups = await self.bot.sleeper.get_matchups(self.league_id, week)
            if not matchups:
                return None

            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users = await self.bot.sleeper.get_users(self.league_id)

            user_lookup = {u.get("user_id"): u for u in users}
            roster_owner = {r.get("roster_id"): r.get("owner_id") for r in rosters}

            def name_for(roster_id: int) -> str:
                owner_id = roster_owner.get(roster_id)
                member = registry.find_by_sleeper_id(owner_id)
                if member:
                    return member.name
                return user_lookup.get(owner_id, {}).get("display_name", "someone")

            by_matchup: dict[int, list[dict]] = {}
            for m in matchups:
                by_matchup.setdefault(m.get("matchup_id"), []).append(m)

            results = []
            for pair in by_matchup.values():
                if len(pair) != 2:
                    continue
                team_a, team_b = pair
                pts_a, pts_b = team_a.get("points") or 0, team_b.get("points") or 0
                if not pts_a and not pts_b:
                    continue  # week hasn't been scored yet
                margin = abs(pts_a - pts_b)
                if pts_a >= pts_b:
                    winner, loser, win_pts, lose_pts = team_a, team_b, pts_a, pts_b
                else:
                    winner, loser, win_pts, lose_pts = team_b, team_a, pts_b, pts_a
                results.append((margin, winner["roster_id"], loser["roster_id"], win_pts, lose_pts))

            if not results:
                return None

            results.sort(key=lambda r: r[0])
            closest = results[0]
            blowout = results[-1]
            margin, winner_id, loser_id, win_pts, lose_pts = random.choice([closest, blowout])

            winner_name = name_for(winner_id)
            loser_name = name_for(loser_id)

            if margin <= 3:
                descriptor = "narrowly beat"
            elif margin >= 40:
                descriptor = "demolished"
            else:
                descriptor = "beat"

            return (
                f"{winner_name} {descriptor} {loser_name} {win_pts:.1f} to {lose_pts:.1f} "
                "last week (this is a REAL result, not a rumor - use it as the basis for one)"
            )
        except Exception as e:
            logger.error(f"Failed to get matchup highlight: {e}")
            return None

    def _maybe_get_style_note(self) -> str:
        """Occasionally nudge delivery away from the default dramatic news-report cadence."""
        if random.random() < 0.3:
            return random.choice(STYLE_NOTES)
        return ""

    def _maybe_get_callback_note(self) -> str:
        """Occasionally reference a recently posted rumor for continuity."""
        if not self._recent_rumors or random.random() >= 0.25:
            return ""
        reporter_name, content = random.choice(self._recent_rumors)
        return (
            f'For context, here is a recent report from {reporter_name}: "{content}" '
            "Only reference or follow up on it if it fits naturally - otherwise ignore it."
        )

    async def _extract_subject_from_text(self, text: str) -> dict:
        """Best-effort parse of real freeform text (a user's submitted
        rumor, or an admin's freeform /randomrumor direction) for named
        entities - e.g. "Corey wants to draft Mendoza" resolves to Corey's
        owner data and Fernando Mendoza's KTC row.

        This mirrors the subject the table generator produces for
        auto-generated seeds (see _generate_from_tables), so the SAME
        weighted context roll in _build_context_block can chain onto
        whatever the human actually wrote about instead of always
        defaulting to a full roster dump. It only makes these entities
        ELIGIBLE for that roll - it doesn't force anything to be included,
        which keeps the "don't cram in everything" guarantee intact even
        as we get better at figuring out who a rumor is about.

        Matching is deliberately simple substring matching, not NLP-grade
        entity recognition - but it's careful about ambiguity: a full name
        match always wins outright, and a bare last name (e.g. "Allen") is
        only trusted when it belongs to exactly one KTC-tracked player.
        Plenty of NFL players share a surname (Josh Allen / Keenan Allen,
        every Jr./Sr. pair), so guessing on a common surname alone would
        just as often chain onto the WRONG player as the right one.
        """
        if not text:
            return {}
        normalized_text = _normalize_for_matching(text)
        text_words = set(normalized_text.split())

        owner = None
        owner_data = await self._get_owner_data_for_rumors()
        first_name_counts: dict[str, int] = {}
        for o in owner_data:
            first_name_counts[o["name"].split()[0].lower()] = (
                first_name_counts.get(o["name"].split()[0].lower(), 0) + 1
            )
        for candidate in owner_data:
            full_key = _normalize_for_matching(candidate["name"])
            first_name = candidate["name"].split()[0].lower()
            if len(full_key) >= 3 and full_key in normalized_text:
                owner = candidate
                break
            if (
                len(first_name) >= 3
                and first_name_counts.get(first_name, 0) == 1
                and first_name in text_words
            ):
                owner = candidate
                break

        player_name = None
        try:
            rows = await get_latest_snapshot()
        except Exception as e:
            logger.error(f"Failed to fetch KTC snapshot for subject extraction: {e}")
            rows = []

        if rows:
            last_name_counts: dict[str, int] = {}
            for row in rows:
                last = row["player_name"].split()[-1].lower()
                last_name_counts[last] = last_name_counts.get(last, 0) + 1

            # Pass 1: a full name match is unambiguous no matter how common
            # the surname is elsewhere in the pool - check these first.
            for row in sorted(rows, key=lambda r: len(r["player_name"]), reverse=True):
                full_key = _normalize_for_matching(row["player_name"])
                if len(full_key) >= 5 and full_key in normalized_text:
                    player_name = row["player_name"]
                    break

            # Pass 2: fall back to a bare last name, but only when exactly
            # one tracked player has it - otherwise we'd be guessing.
            if not player_name:
                for row in rows:
                    last = row["player_name"].split()[-1].lower()
                    if len(last) >= 4 and last_name_counts.get(last, 0) == 1 and last in text_words:
                        player_name = row["player_name"]
                        break

        return {"owner": owner, "player_name": player_name}

    async def _build_rumor_seed(
        self, category: Optional[str] = None, context: Optional[str] = None
    ) -> tuple[str, dict]:
        """Build a topic/seed for AI rumor generation.

        Mixes real events, table-generated scenarios grounded in actual
        rosters, and canned topics so unprompted rumors are as specific and
        varied as the ones /randomrumor produces from tables alone.

        Args:
            category: Optional "trade", "draft", "drama", or "general" to
                scope table-generated scenarios to that flavor. Real-event
                grounding and canned topics (which aren't category-tagged)
                are skipped when a category is given, so the result stays
                on-theme.
            context: Optional freeform direction from the caller (e.g. "about
                Corey and David fighting over a QB"). When given, this is
                used directly as the seed instead of a random pick.

        Returns:
            Tuple of (seed_text, subject). subject is {"owner": ..., "player_name": ...}
            when the seed came from the table generator or was parsed out of
            explicit freeform context (so a later, independent context-block
            roll can chain onto the same owner/player), or {} when neither
            produced an identifiable subject (real event, canned topic).
        """
        if context:
            seed, subject = context, await self._extract_subject_from_text(context)
        else:
            candidates: list[tuple[str, int, dict]] = []

            if not category:
                real_event = await self._get_recent_matchup_highlight()
                if real_event:
                    candidates.append((real_event, 2, {}))

            owner_data = await self._get_owner_data_for_rumors()
            table_seed, table_subject = await self._generate_from_tables(
                owner_data, category=category
            )
            if table_seed:
                candidates.append((table_seed, 3, table_subject))

            if not category:
                topics = self.config.get("random_topics", [])
                if topics:
                    candidates.append((random.choice(topics), 2, {}))

            if candidates:
                idx = random.choices(
                    range(len(candidates)), weights=[c[1] for c in candidates], k=1
                )[0]
                seed, _, subject = candidates[idx]
            else:
                seed, subject = "league drama", {}

        for extra in (self._maybe_get_callback_note(), self._maybe_get_style_note()):
            if extra:
                seed = f"{seed}\n\n{extra}"

        return seed, subject

    async def _build_context_block(self, subject: dict) -> Optional[str]:
        """Roll once on CONTEXT_MODULES to decide what extra context (if
        any) accompanies this rumor's topic - never the same shape twice
        in a row, unlike the old "always dump the full roster" behavior.

        Several modules chain onto `subject` (the owner/player the topic's
        own table roll already picked, if any) so the extra color reads as
        specific to this rumor instead of a random unrelated fact.
        """
        modules, weights = zip(*CONTEXT_MODULES)
        module = random.choices(modules, weights=weights, k=1)[0]

        builders = {
            "full_roster": self._get_roster_context,
            "single_roster": lambda: self._ctx_single_roster(subject),
            "player_value": lambda: self._ctx_player_value(subject),
            "team_value": lambda: self._ctx_team_value(subject),
            "value_trend": lambda: self._ctx_value_trend(subject),
        }
        builder = builders.get(module)
        if builder is None:  # "none" rolled - deliberately no extra context
            return None

        try:
            text = await builder()
        except Exception as e:
            logger.error(f"Context module '{module}' failed: {e}")
            return None

        if not text:
            return None
        if module != "full_roster":
            text += " (Optional color - only weave it in if it fits naturally.)"
        return text

    async def _ctx_single_roster(self, subject: dict) -> Optional[str]:
        """One owner's roster - the topic's owner if the seed named one."""
        owner = subject.get("owner")
        if not owner or not owner.get("players"):
            owner_data = await self._get_owner_data_for_rumors()
            with_players = [o for o in owner_data if o.get("players")]
            owner = random.choice(with_players) if with_players else None
        if not owner:
            return None
        players = ", ".join(owner["players"][:5])
        return f"{owner['name']}'s current roster includes: {players}"

    async def _ctx_player_value(self, subject: dict) -> Optional[str]:
        """A player's KTC dynasty value - the topic's player if it named one."""
        rows = await get_latest_snapshot()
        if not rows:
            return None

        row = None
        player_name = subject.get("player_name")
        if player_name:
            key = normalize_name(player_name)
            row = next((r for r in rows if normalize_name(r["player_name"]) == key), None)
        if not row:
            candidates = [r for r in rows if r.get("value_sf")]
            row = random.choice(candidates) if candidates else None
        if not row or not row.get("value_sf"):
            return None

        return (
            f"KeepTradeCut dynasty value for {row['player_name']} ({row['position']}): "
            f"{row['value_sf']:,} pts (Superflex), ranked #{row['rank_sf']} overall, "
            f"#{row['positional_rank_sf']} at {row['position']}."
        )

    async def _ctx_team_value(self, subject: dict) -> Optional[str]:
        """An owner's total dynasty roster value and league rank."""
        try:
            rosters = await self.bot.sleeper.get_rosters(self.league_id)
            users = await self.bot.sleeper.get_users(self.league_id)
        except Exception as e:
            logger.error(f"Failed to fetch rosters for team value context: {e}")
            return None
        if not rosters:
            return None

        values = await get_team_dynasty_values(rosters)
        if not any(values.values()):
            return None

        registry = get_member_registry()
        user_lookup = {u.get("user_id"): u for u in users}

        def owner_name(roster: dict) -> str:
            member = registry.find_by_sleeper_id(roster.get("owner_id"))
            if member:
                return member.name
            return user_lookup.get(roster.get("owner_id"), {}).get("display_name", "Unknown")

        subject_owner_name = (subject.get("owner") or {}).get("name")
        roster = None
        if subject_owner_name:
            roster = next((r for r in rosters if owner_name(r) == subject_owner_name), None)
        if not roster:
            roster = random.choice(rosters)

        value = values.get(roster["roster_id"], 0)
        if not value:
            return None
        ranked = sorted(rosters, key=lambda r: values.get(r["roster_id"], 0), reverse=True)
        rank = next(i for i, r in enumerate(ranked, 1) if r["roster_id"] == roster["roster_id"])

        return (
            f"{owner_name(roster)}'s total dynasty roster value (KeepTradeCut, Superflex): "
            f"{value:,} pts - ranked #{rank} of {len(rosters)} in the league."
        )

    async def _ctx_value_trend(self, subject: dict) -> Optional[str]:
        """7-day KTC value swing for a player - the topic's player if named."""
        rows = await get_latest_snapshot()
        if not rows:
            return None
        latest_date = rows[0]["recorded_date"]

        row = None
        player_name = subject.get("player_name")
        if player_name:
            key = normalize_name(player_name)
            row = next((r for r in rows if normalize_name(r["player_name"]) == key), None)
        if not row:
            candidates = [r for r in rows if r.get("value_sf")]
            row = random.choice(candidates) if candidates else None
        if not row:
            return None

        delta = await get_value_trend(row["ktc_id"], row["value_sf"], latest_date)
        if not delta:  # None (no history yet) or 0 (flat - not interesting)
            return None

        direction = "risen" if delta > 0 else "fallen"
        return (
            f"{row['player_name']}'s KeepTradeCut dynasty value has {direction} "
            f"{abs(delta):,} pts over the last week."
        )

    def _get_ai_client(self):
        """Pick a random AI backend for this generation.

        Rotating models (not just reporter personas) adds real stylistic
        variety, since two personas run through the same model tend to
        converge on that model's own writing quirks.
        """
        return random.choice(self.ai_clients)

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
                
                players_str = ", ".join(member_players[:3]) if member_players else "Unknown roster"
                lines.append(f"- {member.name}'s roster includes: {players_str}")
            
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

        # Avoid picking the same reporter(s) as the last couple of generations
        candidates = [r for r in reporters if r.get("name") not in self._recent_reporter_names]
        pool = candidates or reporters
        weights = [r.get("weight", 1) for r in pool]
        reporter = random.choices(pool, weights=weights, k=1)[0]
        self._recent_reporter_names.append(reporter.get("name", "Reporter"))
        return (
            reporter.get("name", "Reporter"),
            reporter.get("style", "Be professional."),
            reporter.get("emoji", "📰"),
        )
    
    async def _resolve_reporter(
        self, reporter: str, custom_personality: Optional[str] = None
    ) -> Optional[tuple[str, str, str]]:
        """Resolve a reporter selection (from the shared reporter_autocomplete
        list: "random", "custom", or a config reporter name) into
        (name, style, emoji).

        Shared by /rumor and /randomrumor so both commands offer the exact
        same reporter picker instead of /randomrumor being stuck with
        whatever _get_random_reporter() rolls.

        Returns:
            (name, style, emoji), or None only for reporter == "custom"
            with no custom_personality given - the caller should prompt
            for one rather than silently falling back to random.
        """
        if reporter == "custom":
            if not custom_personality:
                return None
            name, emoji, style = await self._get_ai_client().parse_custom_reporter(
                custom_personality
            )
            return (name, style, emoji)

        if reporter == "random":
            return self._get_random_reporter()

        reporter_data = next(
            (r for r in self.config.get("reporters", []) if r.get("name") == reporter),
            None,
        )
        if reporter_data:
            return (
                reporter_data.get("name", "Reporter"),
                reporter_data.get("style", "Be professional."),
                reporter_data.get("emoji", "📰"),
            )
        return self._get_random_reporter()

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
            self._recent_rumors.append((reporter_name, content))
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
        
        reporter_name, reporter_style, emoji = self._get_random_reporter()
        team_names = await self._get_team_names()

        if not team_names:
            return

        # Mix real events, table-generated scenarios, and canned topics for
        # a specific, varied seed - rather than always a vague generic topic.
        topic, subject = await self._build_rumor_seed()
        member_context = await self._build_context_block(subject)

        logger.info(f"Generating random rumor about: {topic}")
        
        try:
            rumor = await self._get_ai_client().generate_random_rumor(
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
    
    async def reporter_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Suggest reporters from the live config as the user types.

        A fixed `@app_commands.choices` list is capped at 25 entries and has
        to be hand-maintained, so it silently went stale (stopped at "Borat")
        as new reporters were added to reporters.yaml. Autocomplete reads the
        config directly, so it's always current and isn't capped by roster
        size - Discord only limits how many suggestions are shown at once.
        """
        choices = [
            app_commands.Choice(name="🎲 Random", value="random"),
            app_commands.Choice(name="🎭 Custom (describe below)", value="custom"),
        ]
        for r in self.config.get("reporters", []):
            name = r.get("name", "")
            choices.append(app_commands.Choice(name=f"{r.get('emoji', '📰')} {name}", value=name))

        current_lower = current.lower()
        matches = [c for c in choices if current_lower in c.name.lower()]
        return matches[:25]

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
    )
    @app_commands.autocomplete(reporter=reporter_autocomplete)
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
        
        resolved = await self._resolve_reporter(reporter, custom_personality)
        if resolved is None:
            await interaction.followup.send(
                "🎭 You selected Custom reporter but didn't describe the personality!\n"
                "Fill in the `custom_personality` field (e.g. 'a drunk pirate' or 'Yoda from Star Wars')",
                ephemeral=True,
            )
            return
        reporter_name, reporter_style, emoji = resolved

        # Only include league context for fantasy league rumors
        if context == "league":
            team_names = await self._get_team_names()
            subject = await self._extract_subject_from_text(rumor)
            member_context = await self._build_context_block(subject)
        else:
            # NFL news - no league-specific context
            team_names = None
            member_context = None
        
        # Determine which channel to post to
        target_channel = self.nfl_channel_id if context == "nfl" else self.rumors_channel_id
        
        try:
            rewritten = await self._get_ai_client().rewrite_as_reporter(
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
        name="randomrumor",
        description="[Admin] Force post a random rumor now"
    )
    @app_commands.describe(
        category="Scope the rumor to a specific flavor instead of the full random mix",
        context="Give specific direction for what the rumor should be about (overrides category)",
        reporter="Which reporter should break this news? Defaults to random.",
        custom_personality="Describe your own custom reporter (e.g. 'a drunk pirate' or 'Yoda from Star Wars')",
    )
    @app_commands.choices(
        category=[
            app_commands.Choice(name="🔄 Trade", value="trade"),
            app_commands.Choice(name="📋 Draft", value="draft"),
            app_commands.Choice(name="🔥 Drama / Trash Talk", value="drama"),
            app_commands.Choice(name="📰 General", value="general"),
        ]
    )
    @app_commands.autocomplete(reporter=reporter_autocomplete)
    @app_commands.default_permissions(administrator=True)
    async def force_random_rumor(
        self,
        interaction: discord.Interaction,
        category: Optional[app_commands.Choice[str]] = None,
        context: Optional[str] = None,
        reporter: str = "random",
        custom_personality: Optional[str] = None,
    ) -> None:
        """Admin command to force a random rumor post using table-based generation."""
        await interaction.response.defer(ephemeral=True)

        resolved = await self._resolve_reporter(reporter, custom_personality)
        if resolved is None:
            await interaction.followup.send(
                "🎭 You selected Custom reporter but didn't describe the personality!\n"
                "Fill in the `custom_personality` field (e.g. 'a drunk pirate' or 'Yoda from Star Wars')",
                ephemeral=True,
            )
            return
        reporter_name, reporter_style, emoji = resolved

        # Same seed mix (real events, table-generated scenarios, canned
        # topics) the auto-post loop uses - optionally scoped to a category
        # or overridden entirely by freeform context.
        rumor_seed, subject = await self._build_rumor_seed(
            category=category.value if category else None,
            context=context,
        )

        member_context = await self._build_context_block(subject)

        try:
            # Have AI expand on the generated seed
            rumor = await self._get_ai_client().generate_random_rumor(
                topic=rumor_seed,  # Pass the filled template as the topic
                team_names=await self._get_team_names(),
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
                        f"✅ Random rumor posted by {reporter_name}!\n"
                        f"**Seed:** {rumor_seed[:100]}...",
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

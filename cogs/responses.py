"""Random Auto-Responses Cog for Dynasty Bot.

Adds a chance for the bot to randomly respond to messages with
pre-configured responses. Users can add their own responses via
slash command.
"""

import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import discord
import yaml
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from main import DynastyBot

logger = logging.getLogger("dynasty_bot.responses")

# Config path
RESPONSES_PATH = Path(__file__).parent.parent / "config" / "responses.yaml"


class RandomResponses(commands.Cog):
    """Cog for random auto-responses to messages."""
    
    def __init__(self, bot: "DynastyBot"):
        self.bot = bot
        self.responses = self._load_responses()
        logger.info(f"Random Responses cog loaded with {len(self.responses)} responses")
    
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Check if a response proposal has reached 6 votes."""
        if str(payload.emoji) != "✅":
            return
        
        if payload.user_id == self.bot.user.id:
            return
        
        from database import db
        
        try:
            # Check if this message is a pending proposal
            async with db.connection.execute("""
                SELECT id, text, chance, proposer_id
                FROM response_proposals
                WHERE message_id = ? AND status = 'pending'
            """, (str(payload.message_id),)) as cursor:
                proposal = await cursor.fetchone()
            
            if not proposal:
                return
            
            proposal_id, text, chance, proposer_id = proposal
            
            # Get the message and count reactions
            channel = self.bot.get_channel(payload.channel_id)
            if not channel:
                return
            
            try:
                message = await channel.fetch_message(payload.message_id)
            except:
                return
            
            # Count ✅ reactions (excluding bot)
            vote_count = 0
            for reaction in message.reactions:
                if str(reaction.emoji) == "✅":
                    vote_count = reaction.count - 1  # Exclude bot's reaction
                    break
            
            if vote_count >= 6:
                # Approved! Add the response
                new_response = {
                    'text': text,
                    'chance': chance,
                    'added_by': f"Community Vote ({vote_count} votes)"
                }
                
                self.responses.append(new_response)
                self._save_responses(self.responses)
                self.responses = self._load_responses()
                
                # Mark as approved
                await db.connection.execute("""
                    UPDATE response_proposals SET status = 'approved' WHERE id = ?
                """, (proposal_id,))
                await db.connection.commit()
                
                # Send confirmation
                await channel.send(
                    f"✅ **Response approved with {vote_count} votes!**\n"
                    f"The bot will now randomly say:\n> {text}"
                )
                logger.info(f"Response approved by vote: '{text[:30]}...' ({vote_count} votes)")
        except Exception as e:
            logger.error(f"Error processing reaction for proposal: {e}")
    
    def _load_responses(self) -> list[dict]:
        """Load responses from YAML config."""
        try:
            with open(RESPONSES_PATH, 'r') as f:
                config = yaml.safe_load(f) or {}
                return config.get('responses', [])
        except FileNotFoundError:
            logger.warning("responses.yaml not found, creating empty config")
            self._save_responses([])
            return []
    
    def _save_responses(self, responses: list[dict]) -> None:
        """Save responses to YAML config."""
        config = {'responses': responses}
        
        # Add header comment
        header = """# Random Auto-Responses Configuration
# Each response has a probability expressed as "1 in X" chance per message
#
# Format:
#   - text: "The response text"
#     chance: 1000  # 1 in 1000 chance (0.1%)
#     added_by: "username"  # Who added this response
#     type: ambient  # optional, defaults to "ambient" - fires on any message
#
# Other types:
#   type: standings
#     text can use "{rank}" (e.g. "big talk from a guy in {rank} place").
#     Resolved against the message author's current Sleeper standing;
#     silently skipped if they aren't a known league member.
#   type: callback
#     Instead of replying to the triggering message, replies to a random
#     earlier message from the same author in the channel (the "this you?"
#     mechanic). Silently skipped if no earlier message is found.

"""
        with open(RESPONSES_PATH, 'w') as f:
            f.write(header)
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
    
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen to all messages and maybe respond randomly."""
        # Ignore bot messages
        if message.author.bot:
            return
        
        # Ignore DMs
        if not message.guild:
            return
        
        # Check for targeted responses (Kohl's Cash punishments - secret!)
        await self._check_targeted_responses(message)
        
        # Roll ALL responses first to be fair (not order-dependent).
        # This is just the dice roll - no async work - so trying a response
        # that turns out to be unsendable (e.g. no standings match) doesn't
        # cost the others a shot at firing this message.
        triggered = []
        for response in self.responses:
            chance = response.get('chance', 1000)
            text = response.get('text', '')

            if not text:
                continue

            # Roll the dice - 1 in {chance} probability
            if random.randint(1, chance) == 1:
                triggered.append(response)

        # If any triggered, randomly pick one to send
        if triggered:
            await self._send_response(message, random.choice(triggered))

    async def _send_response(self, message: discord.Message, response: dict) -> None:
        """Dispatch a triggered response based on its type."""
        response_type = response.get('type', 'ambient')
        text = response['text']
        chance = response.get('chance', 1000)

        try:
            if response_type == 'standings':
                await self._send_standings_response(message, text, chance)
            elif response_type == 'callback':
                await self._send_callback_response(message, text, chance)
            else:
                await message.reply(text, mention_author=False)
                logger.info(f"Random response triggered: '{text[:30]}...' (1/{chance} chance)")
        except discord.HTTPException as e:
            logger.error(f"Failed to send random response: {e}")

    async def _send_standings_response(self, message: discord.Message, text: str, chance: int) -> None:
        """Send a response templated with the author's current standings rank.

        The template uses `{rank}` (e.g. "big talk from a guy in {rank} place").
        Silently skips if the author isn't a known league member or standings
        can't be resolved - a broken template is worse than not firing.
        """
        from lib.standings import get_rank_for_discord_id, ordinal

        try:
            rank = await get_rank_for_discord_id(self.bot.sleeper, self.bot.league_id, message.author.id)
        except Exception as e:
            logger.warning(f"Couldn't resolve standings rank for response: {e}")
            return

        if rank is None:
            return

        filled = text.format(rank=ordinal(rank))
        await message.reply(filled, mention_author=False)
        logger.info(f"Standings response triggered: '{filled[:30]}...' (1/{chance} chance)")

    async def _send_callback_response(self, message: discord.Message, text: str, chance: int) -> None:
        """Reply to one of the author's own earlier messages instead of the current one.

        This is the "this you?" mechanic - it calls back to something the
        same person said earlier in the channel rather than firing on
        whatever unrelated message happened to win the dice roll.
        """
        target = await self._find_past_message(message)
        if target is None:
            return

        await target.reply(text, mention_author=False)
        logger.info(f"Callback response triggered on {message.author}'s earlier message (1/{chance} chance)")

    async def _find_past_message(
        self, message: discord.Message, search_limit: int = 200
    ) -> Optional[discord.Message]:
        """Find a random earlier message from the same author in this channel."""
        candidates = []
        try:
            async for past in message.channel.history(limit=search_limit, before=message):
                if past.author.id != message.author.id:
                    continue
                if not past.content.strip():
                    continue
                candidates.append(past)
        except discord.HTTPException as e:
            logger.warning(f"Couldn't search channel history for callback response: {e}")
            return None

        if not candidates:
            return None

        return random.choice(candidates)
    
    async def _check_targeted_responses(self, message: discord.Message) -> None:
        """Check for secret targeted responses from Kohl's Cash punishments."""
        from database import db
        
        target_id = str(message.author.id)
        
        try:
            async with db.connection.execute("""
                SELECT id, response_text, chance, remaining_activations
                FROM kohls_targeted_responses
                WHERE target_discord_id = ? AND remaining_activations > 0
            """, (target_id,)) as cursor:
                rows = await cursor.fetchall()
            
            for row_id, text, chance, remaining in rows:
                # Roll the dice
                if random.randint(1, chance) == 1:
                    try:
                        await message.reply(text, mention_author=False)
                        logger.info(f"Targeted response triggered for {message.author}: '{text[:30]}...'")
                        
                        # Decrement activations
                        await db.connection.execute("""
                            UPDATE kohls_targeted_responses
                            SET remaining_activations = remaining_activations - 1
                            WHERE id = ?
                        """, (row_id,))
                        await db.connection.commit()
                        
                        # Only trigger one per message
                        break
                    except discord.HTTPException as e:
                        logger.error(f"Failed to send targeted response: {e}")
        except Exception as e:
            # Database might not be connected yet
            pass

    
    @app_commands.command(name="addresponse")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        text="The response text the bot will randomly say",
        chance="1 in X chance per message (e.g., 1000 = 1/1000 or 0.1%)"
    )
    async def add_response(
        self,
        interaction: discord.Interaction,
        text: str,
        chance: int = 1000
    ):
        """(Admin) Force-add a response. Use /proposeresponse for community voting.
        """
        # Validate inputs
        if len(text) > 500:
            await interaction.response.send_message(
                "❌ Response text must be 500 characters or less.",
                ephemeral=True
            )
            return
        
        if chance < 100:
            await interaction.response.send_message(
                "❌ Chance must be at least 100 (1% max probability).",
                ephemeral=True
            )
            return
        
        if chance > 100000:
            await interaction.response.send_message(
                "❌ Chance must be 100,000 or less.",
                ephemeral=True
            )
            return
        
        # Check for duplicates
        for existing in self.responses:
            if existing.get('text', '').lower() == text.lower():
                await interaction.response.send_message(
                    "❌ This response already exists!",
                    ephemeral=True
                )
                return
        
        # Add the new response
        new_response = {
            'text': text,
            'chance': chance,
            'added_by': interaction.user.display_name
        }
        
        self.responses.append(new_response)
        self._save_responses(self.responses)
        # Reload to ensure in-memory list matches saved file
        self.responses = self._load_responses()

        
        # Calculate percentage
        percentage = (1 / chance) * 100
        
        await interaction.response.send_message(
            f"✅ Added new random response!\n"
            f"**Text:** {text}\n"
            f"**Probability:** 1/{chance:,} ({percentage:.4f}%)\n"
            f"**Added by:** {interaction.user.display_name}"
        )
        logger.info(f"New response added by {interaction.user}: '{text[:30]}...' (1/{chance})")
    
    @app_commands.command(name="proposeresponse")
    @app_commands.describe(
        text="The response text the bot will randomly say",
        chance="1 in X chance per message (default: 1000 = 0.1%)"
    )
    async def propose_response(
        self,
        interaction: discord.Interaction,
        text: str,
        chance: int = 1000
    ):
        """Propose a new random response - requires 6 votes to pass!"""
        # Validate inputs
        if len(text) > 500:
            await interaction.response.send_message(
                "❌ Response text must be 500 characters or less.",
                ephemeral=True
            )
            return
        
        if chance < 100:
            await interaction.response.send_message(
                "❌ Chance must be at least 100 (1% max probability).",
                ephemeral=True
            )
            return
        
        # Check for duplicates
        for existing in self.responses:
            if existing.get('text', '').lower() == text.lower():
                await interaction.response.send_message(
                    "❌ This response already exists!",
                    ephemeral=True
                )
                return
        
        percentage = (1 / chance) * 100
        
        embed = discord.Embed(
            title="🗳️ Proposed Bot Response",
            description=f"**Proposed by:** {interaction.user.mention}\n\n"
                        f"**Response:**\n> {text}\n\n"
                        f"**Probability:** 1/{chance:,} ({percentage:.4f}%)\n\n"
                        f"---\n"
                        f"React with ✅ to approve!\n"
                        f"**Needs 6 votes to pass** (48 hour deadline)",
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Proposed at {interaction.created_at.strftime('%Y-%m-%d %H:%M')} UTC")
        
        await interaction.response.send_message(embed=embed)
        
        # Get the message and add reaction
        msg = await interaction.original_response()
        await msg.add_reaction("✅")
        
        # Store in database for tracking
        from database import db
        from datetime import datetime, timedelta
        
        expires_at = datetime.utcnow() + timedelta(hours=48)
        
        await db.connection.execute("""
            INSERT INTO response_proposals (message_id, channel_id, text, chance, proposer_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(msg.id),
            str(interaction.channel_id),
            text,
            chance,
            str(interaction.user.id),
            expires_at.isoformat()
        ))
        await db.connection.commit()
        
        logger.info(f"Response proposed by {interaction.user}: '{text[:30]}...'")
    
    @app_commands.command(name="listresponses")
    async def list_responses(self, interaction: discord.Interaction):
        """List all random auto-responses."""
        if not self.responses:
            await interaction.response.send_message(
                "No random responses configured yet!\n"
                "Add one with `/addresponse`",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="🎲 Random Auto-Responses",
            description="The bot may randomly reply with these messages:",
            color=discord.Color.purple()
        )
        
        for i, response in enumerate(self.responses, 1):
            text = response.get('text', '')[:50]
            if len(response.get('text', '')) > 50:
                text += "..."
            
            chance = response.get('chance', 1000)
            added_by = response.get('added_by', 'unknown')
            percentage = (1 / chance) * 100
            
            embed.add_field(
                name=f"{i}. {text}",
                value=f"**Chance:** 1/{chance:,} ({percentage:.4f}%) • Added by: {added_by}",
                inline=False
            )
        
        embed.set_footer(text="Use /addresponse to add a new random response")
        
        await interaction.response.send_message(embed=embed)
    
    @app_commands.command(name="removeresponse")
    @app_commands.describe(
        number="The response number to remove (from /listresponses)"
    )
    async def remove_response(
        self,
        interaction: discord.Interaction,
        number: int
    ):
        """Remove a random auto-response by its number."""
        if number < 1 or number > len(self.responses):
            await interaction.response.send_message(
                f"❌ Invalid response number. Use `/listresponses` to see valid numbers (1-{len(self.responses)}).",
                ephemeral=True
            )
            return
        
        removed = self.responses.pop(number - 1)
        self._save_responses(self.responses)
        # Reload to ensure in-memory list matches saved file
        self.responses = self._load_responses()

        
        await interaction.response.send_message(
            f"✅ Removed response: \"{removed.get('text', '')[:50]}...\""
        )
        logger.info(f"Response removed by {interaction.user}: '{removed.get('text', '')[:30]}...'")
    
    @app_commands.command(name="reloadresponses")
    async def reload_responses(self, interaction: discord.Interaction):
        """Reload responses from the config file."""
        self.responses = self._load_responses()
        await interaction.response.send_message(
            f"✅ Reloaded {len(self.responses)} responses from config.",
            ephemeral=True
        )
        logger.info(f"Responses reloaded by {interaction.user}")


async def setup(bot: "DynastyBot"):
    """Load the Random Responses cog."""
    await bot.add_cog(RandomResponses(bot))

"""
Admin commands for managing user mappings.

Allows administrators to link Discord users to Sleeper roster IDs.
"""

import discord
from discord.ext import commands

from utils.data_manager import get_roster_id_by_discord_id, set_user_mapping


class Admin(commands.Cog):
    """Admin commands for bot configuration."""

    def __init__(self, bot: commands.Bot) -> None:
        """
        Initialize the Admin cog.

        Args:
            bot:
                The Discord bot instance.
        """
        self.bot = bot

    @commands.command(name="link_user")
    @commands.has_permissions(administrator=True)
    async def link_user(
        self, ctx: commands.Context, discord_user: discord.Member, roster_id: str
    ) -> None:
        """
        Link a Discord user to a Sleeper roster ID.

        Usage: !link_user @DiscordUser <roster_id>

        Args:
            ctx:
                The command context.
            discord_user:
                The Discord user to link.
            roster_id:
                The Sleeper roster ID.
        """
        try:
            # Validate roster_id is numeric
            int(roster_id)
        except ValueError:
            await ctx.send(f"❌ Invalid roster ID: `{roster_id}`. Must be a number.")
            return

        set_user_mapping(str(discord_user.id), roster_id)
        await ctx.send(
            f"✅ Linked {discord_user.mention} to Sleeper roster ID: `{roster_id}`"
        )

    @commands.command(name="unlink_user")
    @commands.has_permissions(administrator=True)
    async def unlink_user(
        self, ctx: commands.Context, discord_user: discord.Member
    ) -> None:
        """
        Unlink a Discord user from their Sleeper roster.

        Usage: !unlink_user @DiscordUser

        Args:
            ctx:
                The command context.
            discord_user:
                The Discord user to unlink.
        """
        from utils.data_manager import remove_user_mapping

        current_roster = get_roster_id_by_discord_id(str(discord_user.id))
        if current_roster is None:
            await ctx.send(f"❌ {discord_user.mention} is not linked to any roster.")
            return

        remove_user_mapping(str(discord_user.id))
        await ctx.send(
            f"✅ Unlinked {discord_user.mention} from Sleeper roster ID: `{current_roster}`"
        )

    @commands.command(name="check_link")
    @commands.has_permissions(administrator=True)
    async def check_link(
        self, ctx: commands.Context, discord_user: discord.Member
    ) -> None:
        """
        Check if a Discord user is linked to a Sleeper roster.

        Usage: !check_link @DiscordUser

        Args:
            ctx:
                The command context.
            discord_user:
                The Discord user to check.
        """
        roster_id = get_roster_id_by_discord_id(str(discord_user.id))
        if roster_id:
            await ctx.send(
                f"✅ {discord_user.mention} is linked to Sleeper roster ID: `{roster_id}`"
            )
        else:
            await ctx.send(f"❌ {discord_user.mention} is not linked to any roster.")


async def setup(bot: commands.Bot) -> None:
    """Load the Admin cog."""
    await bot.add_cog(Admin(bot))



"""Discord nickname tagging.

Applies a short bracketed tag onto a member's existing nickname (e.g.
"Corey [3rd]") without clobbering whatever they already had, and can
later strip just the tag back off. The base nickname is tracked in the
database rather than parsed back out of the live nickname, since a member
can rename themselves at any time and that should become the new base
going forward rather than being fought over.
"""

import logging
from typing import Optional

import discord

logger = logging.getLogger("dynasty_bot.nicknames")

MAX_NICKNAME_LENGTH = 32


def compose_nickname(base: str, tag: Optional[str]) -> str:
    """Build a Discord nickname from a base name plus an optional bracketed tag."""
    if not tag:
        return base[:MAX_NICKNAME_LENGTH]

    suffix = f" [{tag}]"
    max_base_len = MAX_NICKNAME_LENGTH - len(suffix)
    if max_base_len < 1:
        return suffix.strip()[:MAX_NICKNAME_LENGTH]
    return f"{base[:max_base_len]}{suffix}"


async def apply_tag(member: discord.Member, tag: Optional[str]) -> bool:
    """Apply (or clear, if tag is None) a bracketed tag on a member's nickname.

    Reuses the stored base nickname if the member's current nickname still
    matches what was last applied; otherwise treats their current nickname
    as a new base (they, or someone else, changed it - respect that instead
    of fighting it).

    Returns:
        True if the nickname is now correct (including if it already was),
        False if Discord refused the edit (missing permission, role
        hierarchy, or the target is the server owner).
    """
    from database import db

    async with db.connection.execute(
        "SELECT base_nickname, tag FROM nickname_tags WHERE discord_id = ?",
        (str(member.id),),
    ) as cursor:
        row = await cursor.fetchone()

    current_display = member.display_name
    stored_base, stored_tag = row if row else (None, None)

    if row and current_display == compose_nickname(stored_base, stored_tag):
        base = stored_base
    else:
        base = current_display

    new_nick = compose_nickname(base, tag)

    if new_nick != current_display:
        try:
            await member.edit(nick=new_nick, reason="Dynasty Bot nickname tag sync")
        except discord.Forbidden:
            logger.warning(
                f"No permission to rename {member} - likely the server owner "
                "or a role above the bot's."
            )
            return False
        except discord.HTTPException as e:
            logger.warning(f"Failed to rename {member}: {e}")
            return False

    await db.connection.execute(
        """
        INSERT INTO nickname_tags (discord_id, guild_id, base_nickname, tag, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(discord_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            base_nickname = excluded.base_nickname,
            tag = excluded.tag,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(member.id), str(member.guild.id), base, tag),
    )
    await db.connection.commit()
    return True


async def clear_tag(member: discord.Member) -> bool:
    """Remove any tag this module applied, restoring the plain base nickname."""
    return await apply_tag(member, None)


async def find_discord_id_with_tag(tag: str) -> Optional[str]:
    """Find who currently holds a specific tag, if anyone.

    Backed by the database (rather than in-memory state) so a bot restart
    mid-draft doesn't lose track of who's tagged "ON THE CLOCK".
    """
    from database import db

    async with db.connection.execute(
        "SELECT discord_id FROM nickname_tags WHERE tag = ? LIMIT 1", (tag,)
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None

"""Tests for nickname tagging: lib/nicknames.py and cogs/nicknames.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
import pytest_asyncio

import database as database_module
import lib.nicknames as nicknames_module
from cogs.nicknames import NicknameTags
from database import Database
from lib.nicknames import apply_tag, clear_tag, compose_nickname, find_discord_id_with_tag


def _make_response(status=403, reason="Forbidden"):
    response = MagicMock()
    response.status = status
    response.reason = reason
    return response


def _make_member(discord_id=1, display_name="Corey", guild_id=999):
    member = MagicMock(spec=discord.Member)
    member.id = discord_id
    member.display_name = display_name
    member.guild = MagicMock()
    member.guild.id = guild_id
    member.edit = AsyncMock()
    member.__str__ = MagicMock(return_value=display_name)
    return member


@pytest_asyncio.fixture
async def nickname_db():
    """An in-memory database, patched in as the module-level `database.db`.

    lib.nicknames does `from database import db` inside each function, so
    patching database.db directly (not lib.nicknames.db) is what actually
    takes effect - each call re-resolves the attribute fresh.
    """
    db = Database(":memory:")
    await db.connect()
    with patch.object(database_module, "db", db):
        yield db
    await db.close()


class TestComposeNickname:
    def test_no_tag_returns_base_untouched(self):
        assert compose_nickname("Corey", None) == "Corey"

    def test_appends_bracketed_tag(self):
        assert compose_nickname("Corey", "3rd") == "Corey [3rd]"

    def test_truncates_base_to_fit_32_chars(self):
        base = "A" * 40
        result = compose_nickname(base, "ON THE CLOCK")
        assert len(result) <= 32
        assert result.endswith("[ON THE CLOCK]")

    def test_tag_alone_truncated_if_still_too_long(self):
        result = compose_nickname("X", "Y" * 40)
        assert len(result) <= 32


class TestApplyTag:
    async def test_first_time_apply_uses_current_display_as_base(self, nickname_db):
        member = _make_member(display_name="Corey")

        result = await apply_tag(member, "3rd")

        assert result is True
        member.edit.assert_awaited_once()
        assert member.edit.call_args.kwargs["nick"] == "Corey [3rd]"

        async with nickname_db.execute(
            "SELECT base_nickname, tag FROM nickname_tags WHERE discord_id = ?", ("1",)
        ) as cursor:
            row = await cursor.fetchone()
        assert row == ("Corey", "3rd")

    async def test_reapplying_same_tag_does_not_compound(self, nickname_db):
        member = _make_member(display_name="Corey")
        await apply_tag(member, "3rd")
        member.display_name = "Corey [3rd]"  # reflects what Discord now shows
        member.edit.reset_mock()

        await apply_tag(member, "2nd")

        assert member.edit.call_args.kwargs["nick"] == "Corey [2nd]"

    async def test_manual_rename_becomes_new_base(self, nickname_db):
        member = _make_member(display_name="Corey")
        await apply_tag(member, "3rd")
        # Member (or someone else) renamed them outside our tagging.
        member.display_name = "Corey the Great"
        member.edit.reset_mock()

        await apply_tag(member, "2nd")

        assert member.edit.call_args.kwargs["nick"] == "Corey the Great [2nd]"

    async def test_skips_edit_when_already_correct(self, nickname_db):
        member = _make_member(display_name="Corey")
        await apply_tag(member, "3rd")
        member.display_name = "Corey [3rd]"
        member.edit.reset_mock()

        await apply_tag(member, "3rd")

        assert member.edit.await_count == 0

    async def test_forbidden_returns_false_and_does_not_persist(self, nickname_db):
        member = _make_member(display_name="Corey")
        member.edit.side_effect = discord.Forbidden(_make_response(), "missing permissions")

        result = await apply_tag(member, "3rd")

        assert result is False
        async with nickname_db.execute(
            "SELECT * FROM nickname_tags WHERE discord_id = ?", ("1",)
        ) as cursor:
            row = await cursor.fetchone()
        assert row is None

    async def test_clear_tag_restores_base(self, nickname_db):
        member = _make_member(display_name="Corey")
        await apply_tag(member, "3rd")
        member.display_name = "Corey [3rd]"
        member.edit.reset_mock()

        await clear_tag(member)

        assert member.edit.call_args.kwargs["nick"] == "Corey"


class TestFindDiscordIdWithTag:
    async def test_finds_the_holder(self, nickname_db):
        member = _make_member(discord_id=42, display_name="Fuzzy")
        await apply_tag(member, "ON THE CLOCK")

        found = await find_discord_id_with_tag("ON THE CLOCK")

        assert found == "42"

    async def test_returns_none_when_nobody_holds_it(self, nickname_db):
        found = await find_discord_id_with_tag("ON THE CLOCK")
        assert found is None


# =============================================================================
# cogs/nicknames.py
# =============================================================================


def _make_league_member(name, discord_id, sleeper_id):
    m = MagicMock()
    m.name = name
    m.discord_id = discord_id
    m.sleeper_id = sleeper_id
    return m


@pytest.fixture
def nicknames_cog(mock_bot):
    guild = MagicMock()
    mock_bot.guilds = [guild]
    cog = NicknameTags(mock_bot)
    return cog, guild


class TestSyncStandingsNicknames:
    async def test_tags_known_members_with_their_rank(self, nicknames_cog, monkeypatch):
        cog, guild = nicknames_cog

        entry_a = MagicMock(owner_id="user_a", rank=1)
        entry_b = MagicMock(owner_id="user_b", rank=2)
        monkeypatch.setattr(
            "cogs.nicknames.compute_standings", AsyncMock(return_value=[entry_a, entry_b])
        )

        registry = MagicMock()
        registry.members = [
            _make_league_member("Alice", discord_id="1", sleeper_id="user_a"),
            _make_league_member("Bob", discord_id="2", sleeper_id="user_b"),
            _make_league_member("NoDiscord", discord_id=None, sleeper_id="user_c"),
        ]
        monkeypatch.setattr("cogs.nicknames.get_member_registry", lambda: registry)

        member_a, member_b = MagicMock(), MagicMock()
        guild.get_member = MagicMock(side_effect=lambda i: {1: member_a, 2: member_b}.get(i))

        apply_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("cogs.nicknames.apply_tag", apply_mock)

        tagged = await cog._sync_standings_nicknames()

        assert tagged == 2
        apply_mock.assert_any_await(member_a, "1st")
        apply_mock.assert_any_await(member_b, "2nd")

    async def test_skips_members_with_no_resolvable_rank(self, nicknames_cog, monkeypatch):
        cog, guild = nicknames_cog
        monkeypatch.setattr("cogs.nicknames.compute_standings", AsyncMock(return_value=[]))
        registry = MagicMock()
        registry.members = [_make_league_member("Alice", discord_id="1", sleeper_id="user_a")]
        monkeypatch.setattr("cogs.nicknames.get_member_registry", lambda: registry)
        apply_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("cogs.nicknames.apply_tag", apply_mock)

        tagged = await cog._sync_standings_nicknames()

        assert tagged == 0
        apply_mock.assert_not_awaited()


class TestSyncDraftOrderNicknames:
    async def test_tags_owners_with_their_slot(self, nicknames_cog, monkeypatch):
        cog, guild = nicknames_cog

        team1 = MagicMock(owner_id="user_a")
        team2 = MagicMock(owner_id="user_b")
        draft_cog = MagicMock()
        draft_cog._fetch_team_stats = AsyncMock(return_value=[team1, team2])
        cog.bot.get_cog = MagicMock(return_value=draft_cog)

        monkeypatch.setattr("cogs.draft.calculate_payouts", lambda teams: teams)
        monkeypatch.setattr("cogs.draft.calculate_draft_order", lambda teams: teams)

        registry = MagicMock()
        registry.find_by_sleeper_id = MagicMock(
            side_effect=lambda sid: {
                "user_a": _make_league_member("Alice", discord_id="1", sleeper_id="user_a"),
                "user_b": _make_league_member("Bob", discord_id="2", sleeper_id="user_b"),
            }.get(sid)
        )
        monkeypatch.setattr("cogs.nicknames.get_member_registry", lambda: registry)

        member_a, member_b = MagicMock(), MagicMock()
        guild.get_member = MagicMock(side_effect=lambda i: {1: member_a, 2: member_b}.get(i))

        apply_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("cogs.nicknames.apply_tag", apply_mock)

        tagged = await cog._sync_draft_order_nicknames()

        assert tagged == 2
        apply_mock.assert_any_await(member_a, "Pick 1/2")
        apply_mock.assert_any_await(member_b, "Pick 2/2")

    async def test_returns_zero_when_draft_cog_not_loaded(self, nicknames_cog):
        cog, guild = nicknames_cog
        cog.bot.get_cog = MagicMock(return_value=None)

        tagged = await cog._sync_draft_order_nicknames()

        assert tagged == 0


class TestResolveOnTheClockOwnerId:
    async def test_linear_draft_first_pick(self, nicknames_cog):
        cog, guild = nicknames_cog
        cog.bot.sleeper.get_picks_in_draft = AsyncMock(return_value=[])
        cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[{"roster_id": 1, "owner_id": "user_a"}, {"roster_id": 2, "owner_id": "user_b"}]
        )
        draft = {
            "draft_id": "d1",
            "type": "linear",
            "slot_to_roster_id": {"1": 1, "2": 2},
        }

        owner_id = await cog._resolve_on_the_clock_owner_id(draft)

        assert owner_id == "user_a"

    async def test_linear_draft_repeats_same_order_each_round(self, nicknames_cog):
        cog, guild = nicknames_cog
        # 2 slots, 3 picks already made -> next pick is round 2, slot 2
        cog.bot.sleeper.get_picks_in_draft = AsyncMock(return_value=[{}, {}, {}])
        cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[{"roster_id": 1, "owner_id": "user_a"}, {"roster_id": 2, "owner_id": "user_b"}]
        )
        draft = {
            "draft_id": "d1",
            "type": "linear",
            "slot_to_roster_id": {"1": 1, "2": 2},
        }

        owner_id = await cog._resolve_on_the_clock_owner_id(draft)

        assert owner_id == "user_b"

    async def test_snake_draft_reverses_on_even_round(self, nicknames_cog):
        cog, guild = nicknames_cog
        # 2 slots, 2 picks made -> next pick is round 2, snake reverses to slot 2 first
        cog.bot.sleeper.get_picks_in_draft = AsyncMock(return_value=[{}, {}])
        cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[{"roster_id": 1, "owner_id": "user_a"}, {"roster_id": 2, "owner_id": "user_b"}]
        )
        draft = {
            "draft_id": "d1",
            "type": "snake",
            "slot_to_roster_id": {"1": 1, "2": 2},
        }

        owner_id = await cog._resolve_on_the_clock_owner_id(draft)

        assert owner_id == "user_b"

    async def test_missing_slot_data_returns_none(self, nicknames_cog):
        cog, guild = nicknames_cog
        cog.bot.sleeper.get_picks_in_draft = AsyncMock(return_value=[])
        draft = {"draft_id": "d1", "type": "linear", "slot_to_roster_id": {}}

        owner_id = await cog._resolve_on_the_clock_owner_id(draft)

        assert owner_id is None


class TestCheckDraftClock:
    async def test_tags_current_picker_and_clears_previous_holder(
        self, nicknames_cog, monkeypatch
    ):
        cog, guild = nicknames_cog
        monkeypatch.setattr(
            cog, "_get_active_draft", AsyncMock(return_value={"draft_id": "d1"})
        )
        monkeypatch.setattr(
            cog, "_resolve_on_the_clock_owner_id", AsyncMock(return_value="user_b")
        )

        registry = MagicMock()
        registry.find_by_sleeper_id = MagicMock(
            return_value=_make_league_member("Bob", discord_id="2", sleeper_id="user_b")
        )
        monkeypatch.setattr("cogs.nicknames.get_member_registry", lambda: registry)

        find_mock = AsyncMock(return_value="1")  # Alice currently holds the tag
        monkeypatch.setattr("cogs.nicknames.find_discord_id_with_tag", find_mock)

        prev_member, new_member = MagicMock(), MagicMock()
        guild.get_member = MagicMock(side_effect=lambda i: {1: prev_member, 2: new_member}.get(i))

        clear_mock = AsyncMock(return_value=True)
        apply_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("cogs.nicknames.clear_tag", clear_mock)
        monkeypatch.setattr("cogs.nicknames.apply_tag", apply_mock)

        await cog._check_draft_clock()

        clear_mock.assert_awaited_once_with(prev_member)
        apply_mock.assert_awaited_once_with(new_member, "ON THE CLOCK")

    async def test_noop_when_same_person_already_tagged(self, nicknames_cog, monkeypatch):
        cog, guild = nicknames_cog
        monkeypatch.setattr(
            cog, "_get_active_draft", AsyncMock(return_value={"draft_id": "d1"})
        )
        monkeypatch.setattr(
            cog, "_resolve_on_the_clock_owner_id", AsyncMock(return_value="user_a")
        )
        registry = MagicMock()
        registry.find_by_sleeper_id = MagicMock(
            return_value=_make_league_member("Alice", discord_id="1", sleeper_id="user_a")
        )
        monkeypatch.setattr("cogs.nicknames.get_member_registry", lambda: registry)
        monkeypatch.setattr(
            "cogs.nicknames.find_discord_id_with_tag", AsyncMock(return_value="1")
        )
        apply_mock = AsyncMock()
        monkeypatch.setattr("cogs.nicknames.apply_tag", apply_mock)

        await cog._check_draft_clock()

        apply_mock.assert_not_awaited()

    async def test_clears_stale_tag_when_no_draft_active(self, nicknames_cog, monkeypatch):
        cog, guild = nicknames_cog
        monkeypatch.setattr(cog, "_get_active_draft", AsyncMock(return_value=None))
        monkeypatch.setattr(
            "cogs.nicknames.find_discord_id_with_tag", AsyncMock(return_value="1")
        )
        stale_member = MagicMock()
        guild.get_member = MagicMock(return_value=stale_member)
        clear_mock = AsyncMock(return_value=True)
        monkeypatch.setattr("cogs.nicknames.clear_tag", clear_mock)

        await cog._check_draft_clock()

        clear_mock.assert_awaited_once_with(stale_member)

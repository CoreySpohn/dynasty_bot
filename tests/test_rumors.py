"""Tests for the League Rumors cog."""

import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import cogs.trade_values as trade_values_module
from cogs.rumors import STYLE_NOTES, LeagueRumors
from database import Database


@pytest_asyncio.fixture
async def rumors_cog(mock_bot):
    """Create a LeagueRumors cog, cancelling its background task on teardown."""
    cog = LeagueRumors(mock_bot)
    yield cog
    cog.random_rumor_task.cancel()


@pytest_asyncio.fixture
async def ktc_db():
    """An in-memory database, patched in as cogs.trade_values' module-level `db`.

    cogs.rumors imports data-access functions directly from cogs.trade_values
    (get_latest_snapshot, get_team_dynasty_values, get_value_trend), and
    those functions close over trade_values' own `db` global - so that's
    the object that needs patching, not anything in cogs.rumors.
    """
    database = Database(":memory:")
    await database.connect()
    with patch.object(trade_values_module, "db", database):
        yield database
    await database.close()


async def _insert_ktc_row(
    db, ktc_id, sleeper_id, name, position, value_sf, recorded_date
):
    async with db.execute(
        """
        INSERT INTO ktc_values (
            ktc_id, sleeper_id, player_name, position, team, is_rookie,
            value_1qb, rank_1qb, positional_rank_1qb,
            value_sf, rank_sf, positional_rank_sf, recorded_date
        ) VALUES (?, ?, ?, ?, 'CIN', 0, ?, 1, 1, ?, 1, 1, ?)
        """,
        (ktc_id, sleeper_id, name, position, value_sf, value_sf, recorded_date),
    ):
        pass


class TestRandomRumorTaskAutostarts:
    """Regression coverage for the auto-post loop being left disabled."""

    async def test_random_rumor_task_starts_on_init(self, rumors_cog):
        """The loop that posts unprompted rumors must actually be running.

        Previously `self.random_rumor_task.start()` was commented out in
        __init__ ("Disabled for now to avoid spam during testing"), so no
        rumor ever posted without a manual /randomrumor or DM.
        """
        assert rumors_cog.random_rumor_task.is_running()


class TestAIClientRotation:
    """Coverage for picking a random AI backend per generation."""

    async def test_get_ai_client_returns_one_of_the_pool(self, rumors_cog):
        for _ in range(10):
            assert rumors_cog._get_ai_client() in rumors_cog.ai_clients

    async def test_only_includes_backends_with_configured_credentials(self, mock_bot):
        cog = LeagueRumors(mock_bot)
        try:
            for client in cog.ai_clients:
                assert client.client is not None
            assert cog.ai_clients  # never an empty pool
        finally:
            cog.random_rumor_task.cancel()

    async def test_falls_back_to_a_stub_client_when_none_configured(self, mock_bot):
        keyless = lambda **kwargs: MagicMock(client=None)
        with patch("cogs.rumors.GeminiClient", side_effect=keyless), \
             patch("cogs.rumors.ClaudeClient", side_effect=keyless), \
             patch("cogs.rumors.OpenAIClient", side_effect=keyless):
            cog = LeagueRumors(mock_bot)
        try:
            assert len(cog.ai_clients) == 1
        finally:
            cog.random_rumor_task.cancel()


class TestGenerateFromTables:
    """Test suite for table-based rumor seed generation."""

    async def test_fills_owner_and_player_placeholders(self, rumors_cog):
        rumors_cog.rumor_tables = {
            "templates": {
                "general": ["{owner1} wants to trade {player1} to {owner2} for {player2}"]
            }
        }
        owner_data = [
            {"name": "Alice", "team_name": "Team A", "players": ["Player One"]},
            {"name": "Bob", "team_name": "Team B", "players": ["Player Two"]},
        ]

        result, subject = await rumors_cog._generate_from_tables(owner_data)

        assert "Alice" in result or "Bob" in result
        assert "Player One" in result or "Player Two" in result
        assert "{" not in result
        # subject chains onto whichever owner1/player1 the roll actually picked.
        assert subject["owner"]["name"] in ("Alice", "Bob")
        assert subject["player_name"] in ("Player One", "Player Two")

    async def test_falls_back_when_no_templates_configured(self, rumors_cog):
        rumors_cog.rumor_tables = {}

        result, subject = await rumors_cog._generate_from_tables([])

        assert result == "a mysterious trade brewing in the league"
        assert subject == {}

    async def test_handles_no_owner_data(self, rumors_cog):
        rumors_cog.rumor_tables = {"templates": {"general": ["{owner1} is up to something"]}}

        result, subject = await rumors_cog._generate_from_tables([])

        assert "An owner" in result
        assert subject["owner"] is None

    async def test_restricts_to_requested_category(self, rumors_cog):
        rumors_cog.rumor_tables = {
            "templates": {
                "draft": ["draft template about {owner1}"],
                "trade": ["trade template about {owner1}"],
            }
        }

        result, _ = await rumors_cog._generate_from_tables([], category="draft")

        assert result == "draft template about An owner"

    async def test_unknown_category_falls_back_to_full_mix(self, rumors_cog):
        rumors_cog.rumor_tables = {"templates": {"draft": ["only template about {owner1}"]}}

        result, _ = await rumors_cog._generate_from_tables([], category="not-a-real-category")

        assert result == "only template about An owner"


class TestGetRandomReporter:
    """Test suite for reporter persona selection."""

    async def test_returns_reporter_from_config(self, rumors_cog):
        name, style, emoji = rumors_cog._get_random_reporter()

        reporter_names = {r["name"] for r in rumors_cog.config.get("reporters", [])}
        assert name in reporter_names
        assert style
        assert emoji

    async def test_returns_fallback_when_no_reporters_configured(self, rumors_cog):
        rumors_cog.config = {"reporters": []}

        name, style, emoji = rumors_cog._get_random_reporter()

        assert name == "Unknown Reporter"


class TestResolveReporter:
    """Coverage for the reporter picker shared by /rumor and /randomrumor."""

    async def test_random_returns_a_configured_reporter(self, rumors_cog):
        resolved = await rumors_cog._resolve_reporter("random")

        reporter_names = {r["name"] for r in rumors_cog.config.get("reporters", [])}
        assert resolved[0] in reporter_names

    async def test_named_reporter_is_resolved_from_config(self, rumors_cog):
        rumors_cog.config = {
            "reporters": [{"name": "Test Reporter", "style": "sarcastic", "emoji": "🔥"}]
        }

        resolved = await rumors_cog._resolve_reporter("Test Reporter")

        assert resolved == ("Test Reporter", "sarcastic", "🔥")

    async def test_unknown_name_falls_back_to_random(self, rumors_cog):
        resolved = await rumors_cog._resolve_reporter("Not A Real Reporter")

        reporter_names = {r["name"] for r in rumors_cog.config.get("reporters", [])}
        assert resolved[0] in reporter_names

    async def test_custom_without_personality_returns_none(self, rumors_cog):
        resolved = await rumors_cog._resolve_reporter("custom", None)

        assert resolved is None

    async def test_custom_with_personality_uses_ai_client(self, rumors_cog):
        # parse_custom_reporter returns (name, emoji, style) - _resolve_reporter
        # must reorder that to the (name, style, emoji) convention used everywhere else.
        stub_client = MagicMock()
        stub_client.parse_custom_reporter = AsyncMock(
            return_value=("Pirate Pete", "🏴‍☠️", "talks like a pirate")
        )
        rumors_cog._get_ai_client = MagicMock(return_value=stub_client)

        resolved = await rumors_cog._resolve_reporter("custom", "a drunk pirate")

        assert resolved == ("Pirate Pete", "talks like a pirate", "🏴‍☠️")


class TestForceRandomRumorReporterOption:
    """Regression coverage: /randomrumor must offer the same reporter
    picker as /rumor instead of always rolling a random one."""

    async def test_uses_the_requested_reporter(self, rumors_cog):
        rumors_cog.config = {
            "reporters": [{"name": "Test Reporter", "style": "sarcastic", "emoji": "🔥"}]
        }
        rumors_cog.rumor_tables = {"templates": {"general": ["a mysterious trade"]}}
        stub_client = MagicMock()
        stub_client.generate_random_rumor = AsyncMock(return_value="Generated rumor text")
        rumors_cog._get_ai_client = MagicMock(return_value=stub_client)
        rumors_cog._post_rumor = AsyncMock(return_value=True)
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        await rumors_cog.force_random_rumor.callback(
            rumors_cog, interaction, reporter="Test Reporter"
        )

        rumors_cog._post_rumor.assert_awaited_once()
        assert rumors_cog._post_rumor.call_args.kwargs["reporter_name"] == "Test Reporter"

    async def test_custom_without_personality_prompts_instead_of_posting(self, rumors_cog):
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        rumors_cog._post_rumor = AsyncMock()

        await rumors_cog.force_random_rumor.callback(rumors_cog, interaction, reporter="custom")

        rumors_cog._post_rumor.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        assert "Custom reporter" in interaction.followup.send.call_args[0][0]


class TestPostRumor:
    """Test suite for posting rumors to Discord."""

    async def test_returns_false_when_no_channel_configured(self, rumors_cog):
        rumors_cog.rumors_channel_id = 0

        result = await rumors_cog._post_rumor(
            content="test rumor", reporter_name="Reporter", emoji="📰"
        )

        assert result is False

    async def test_successful_post_is_remembered_for_callbacks(self, rumors_cog):
        channel = AsyncMock()
        rumors_cog.bot.get_channel = MagicMock(return_value=channel)
        rumors_cog.rumors_channel_id = 123

        result = await rumors_cog._post_rumor(
            content="Big trade rumor", reporter_name="Test Reporter", emoji="📰"
        )

        assert result is True
        assert ("Test Reporter", "Big trade rumor") in rumors_cog._recent_rumors


class TestAntiRepeat:
    """Coverage for avoiding back-to-back repeats of the same reporter/template."""

    async def test_reporter_avoids_immediate_repeat(self, rumors_cog):
        reporters = rumors_cog.config.get("reporters", [])
        if len(reporters) < 2:
            return  # not enough reporters configured to exercise this

        first_name, _, _ = rumors_cog._get_random_reporter()
        for _ in range(10):
            next_name, _, _ = rumors_cog._get_random_reporter()
            assert next_name != first_name
            first_name = next_name

    async def test_template_avoids_immediate_repeat(self, rumors_cog):
        rumors_cog.rumor_tables = {"templates": {"general": ["Template A", "Template B"]}}

        first = await rumors_cog._generate_from_tables([])
        second = await rumors_cog._generate_from_tables([])

        assert first != second

    async def test_owner_avoids_immediate_repeat(self, rumors_cog):
        rumors_cog.rumor_tables = {"templates": {"general": ["{owner1} is up to something"]}}
        # More owners than the recency deque holds, so exclusion never has
        # to fall back to the full (repeat-eligible) pool mid-test.
        owner_data = [
            {"name": name, "team_name": f"Team {name}", "players": []}
            for name in ["Alice", "Bob", "Carol", "Dave", "Erin", "Frank"]
        ]

        _, first_subject = await rumors_cog._generate_from_tables(owner_data)
        first_name = first_subject["owner"]["name"]
        for _ in range(10):
            _, subject = await rumors_cog._generate_from_tables(owner_data)
            next_name = subject["owner"]["name"]
            assert next_name != first_name
            first_name = next_name


class TestRecentMatchupHighlight:
    """Coverage for grounding rumors in real matchup results."""

    async def test_returns_none_when_sleeper_calls_fail(self, rumors_cog):
        # mock_bot.sleeper is a plain MagicMock; awaiting its methods raises,
        # which should be caught and turned into a None (no highlight) result.
        result = await rumors_cog._get_recent_matchup_highlight()
        assert result is None

    async def test_returns_none_before_week_two(self, rumors_cog):
        rumors_cog.bot.sleeper.get_nfl_state = AsyncMock(return_value={"week": 1})

        result = await rumors_cog._get_recent_matchup_highlight()

        assert result is None

    async def test_describes_a_real_result(self, rumors_cog):
        rumors_cog.bot.sleeper.get_nfl_state = AsyncMock(return_value={"week": 3})
        rumors_cog.bot.sleeper.get_matchups = AsyncMock(
            return_value=[
                {"matchup_id": 1, "roster_id": 1, "points": 120.5},
                {"matchup_id": 1, "roster_id": 2, "points": 80.0},
            ]
        )
        rumors_cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[
                {"roster_id": 1, "owner_id": "u1"},
                {"roster_id": 2, "owner_id": "u2"},
            ]
        )
        rumors_cog.bot.sleeper.get_users = AsyncMock(
            return_value=[
                {"user_id": "u1", "display_name": "Alice"},
                {"user_id": "u2", "display_name": "Bob"},
            ]
        )

        result = await rumors_cog._get_recent_matchup_highlight()

        assert "Alice" in result
        assert "Bob" in result
        assert "120.5" in result


class TestSeedExtras:
    """Coverage for the callback/style-note nudges mixed into rumor seeds."""

    async def test_callback_note_references_recent_rumor_when_forced(self, rumors_cog, monkeypatch):
        rumors_cog._recent_rumors.append(("Reporter X", "Some previous rumor text"))
        monkeypatch.setattr(random, "random", lambda: 0.0)

        note = rumors_cog._maybe_get_callback_note()

        assert "Reporter X" in note
        assert "Some previous rumor text" in note

    async def test_callback_note_empty_when_no_recent_rumors(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.0)

        note = rumors_cog._maybe_get_callback_note()

        assert note == ""

    async def test_style_note_forced(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.0)

        note = rumors_cog._maybe_get_style_note()

        assert note in STYLE_NOTES

    async def test_style_note_suppressed(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "random", lambda: 0.99)

        note = rumors_cog._maybe_get_style_note()

        assert note == ""


class TestBuildRumorSeed:
    """Coverage for the combined seed builder used by both auto-post and /randomrumor."""

    async def test_returns_non_empty_seed_even_when_sleeper_unavailable(self, rumors_cog):
        seed, subject = await rumors_cog._build_rumor_seed()

        assert seed
        assert isinstance(seed, str)
        assert isinstance(subject, dict)

    async def test_context_overrides_category_and_table_generation(self, rumors_cog):
        seed, subject = await rumors_cog._build_rumor_seed(
            category="draft", context="a very specific ask"
        )

        assert seed.startswith("a very specific ask")
        # No owner/player named in that freeform text, so nothing to chain onto -
        # but _extract_subject_from_text always returns owner/player keys.
        assert subject == {"owner": None, "player_name": None}

    async def test_category_scopes_seed_to_that_templates_bucket(self, rumors_cog):
        rumors_cog.rumor_tables = {
            "templates": {
                "draft": ["draft-only seed about {owner1}"],
                "trade": ["trade-only seed about {owner1}"],
            }
        }

        for _ in range(5):
            seed, _ = await rumors_cog._build_rumor_seed(category="draft")
            assert seed.startswith("draft-only seed about")


class TestExtractSubjectFromText:
    """Coverage for parsing named entities out of real user-submitted text,
    e.g. "Corey wants to draft Mendoza" -> Corey's owner data + Mendoza's
    KTC row, so the context roll has something specific to chain onto."""

    async def test_finds_owner_and_player_by_name(self, rumors_cog, ktc_db):
        rumors_cog._get_owner_data_for_rumors = AsyncMock(
            return_value=[{"name": "Corey", "players": [], "team_name": ""}]
        )
        await _insert_ktc_row(ktc_db, 1, "p1", "Fernando Mendoza", "QB", 3000, "2026-07-22")

        subject = await rumors_cog._extract_subject_from_text(
            "Corey wants to draft Mendoza"
        )

        assert subject["owner"]["name"] == "Corey"
        assert subject["player_name"] == "Fernando Mendoza"

    async def test_no_match_returns_none_fields(self, rumors_cog, ktc_db):
        rumors_cog._get_owner_data_for_rumors = AsyncMock(
            return_value=[{"name": "Corey", "players": [], "team_name": ""}]
        )

        subject = await rumors_cog._extract_subject_from_text(
            "something totally unrelated happened"
        )

        assert subject == {"owner": None, "player_name": None}

    async def test_empty_text_short_circuits(self, rumors_cog):
        subject = await rumors_cog._extract_subject_from_text("")

        assert subject == {}

    async def test_short_names_are_not_matched(self, rumors_cog, ktc_db):
        # "Al" is too short to safely match as a substring in freeform text.
        rumors_cog._get_owner_data_for_rumors = AsyncMock(
            return_value=[{"name": "Al", "players": [], "team_name": ""}]
        )
        await _insert_ktc_row(ktc_db, 1, "p1", "AJ Fox", "WR", 1000, "2026-07-22")

        subject = await rumors_cog._extract_subject_from_text("Al thinks his team is great")

        assert subject["owner"] is None
        assert subject["player_name"] is None

    async def test_ambiguous_surname_alone_is_not_matched(self, rumors_cog, ktc_db):
        # Two different real players share the surname "Allen" - a bare
        # "Allen" mention shouldn't guess which one the rumor is about.
        await _insert_ktc_row(ktc_db, 1, "p1", "Josh Allen", "QB", 9000, "2026-07-22")
        await _insert_ktc_row(ktc_db, 2, "p2", "Keenan Allen", "WR", 500, "2026-07-22")

        subject = await rumors_cog._extract_subject_from_text(
            "Allen had a huge game this week"
        )

        assert subject["player_name"] is None

    async def test_full_name_wins_even_with_ambiguous_surname(self, rumors_cog, ktc_db):
        await _insert_ktc_row(ktc_db, 1, "p1", "Josh Allen", "QB", 9000, "2026-07-22")
        await _insert_ktc_row(ktc_db, 2, "p2", "Keenan Allen", "WR", 500, "2026-07-22")

        subject = await rumors_cog._extract_subject_from_text(
            "Josh Allen had a huge game this week"
        )

        assert subject["player_name"] == "Josh Allen"

    async def test_ambiguous_owner_first_name_alone_is_not_matched(self, rumors_cog):
        # Two owners share the first name "Rob" (Rob Jr. / Rob Sr.) - bare
        # "Rob" shouldn't guess which one.
        rumors_cog._get_owner_data_for_rumors = AsyncMock(
            return_value=[
                {"name": "Rob Jr.", "players": [], "team_name": ""},
                {"name": "Rob Sr.", "players": [], "team_name": ""},
            ]
        )

        subject = await rumors_cog._extract_subject_from_text("Rob is on a heater")

        assert subject["owner"] is None

    async def test_full_owner_name_wins_even_with_ambiguous_first_name(self, rumors_cog):
        rumors_cog._get_owner_data_for_rumors = AsyncMock(
            return_value=[
                {"name": "Rob Jr.", "players": [], "team_name": ""},
                {"name": "Rob Sr.", "players": [], "team_name": ""},
            ]
        )

        subject = await rumors_cog._extract_subject_from_text("Rob Jr is on a heater")

        assert subject["owner"]["name"] == "Rob Jr."


class TestBuildContextBlock:
    """Coverage for the single weighted roll that decides what extra
    context (if any) rides along with a rumor - replacing the old
    behavior of always injecting the full roster dump."""

    async def test_none_module_returns_no_context(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "choices", lambda *a, **k: ["none"])

        result = await rumors_cog._build_context_block({})

        assert result is None

    async def test_full_roster_module_has_no_optional_color_suffix(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "choices", lambda *a, **k: ["full_roster"])
        rumors_cog._get_roster_context = AsyncMock(return_value="- Alice's roster includes: X")

        result = await rumors_cog._build_context_block({})

        assert result == "- Alice's roster includes: X"

    async def test_non_full_roster_module_is_marked_optional(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "choices", lambda *a, **k: ["single_roster"])
        subject = {"owner": {"name": "Alice", "players": ["Puka Nacua"], "team_name": ""}}

        result = await rumors_cog._build_context_block(subject)

        assert "Alice's current roster includes: Puka Nacua" in result
        assert "Optional color" in result

    async def test_module_exception_is_caught_and_returns_none(self, rumors_cog, monkeypatch):
        monkeypatch.setattr(random, "choices", lambda *a, **k: ["team_value"])
        rumors_cog._ctx_team_value = AsyncMock(side_effect=RuntimeError("boom"))

        result = await rumors_cog._build_context_block({})

        assert result is None


class TestCtxSingleRoster:
    """Coverage for the single-owner-roster context module."""

    async def test_chains_to_subject_owner(self, rumors_cog):
        subject = {"owner": {"name": "Alice", "players": ["Puka Nacua", "Bijan Robinson"]}}

        result = await rumors_cog._ctx_single_roster(subject)

        assert result == "Alice's current roster includes: Puka Nacua, Bijan Robinson"

    async def test_falls_back_to_random_owner_without_subject(self, rumors_cog):
        rumors_cog._get_owner_data_for_rumors = AsyncMock(
            return_value=[{"name": "Bob", "players": ["Justin Jefferson"], "team_name": ""}]
        )

        result = await rumors_cog._ctx_single_roster({})

        assert result == "Bob's current roster includes: Justin Jefferson"

    async def test_returns_none_when_no_rosters_available(self, rumors_cog):
        rumors_cog._get_owner_data_for_rumors = AsyncMock(return_value=[])

        result = await rumors_cog._ctx_single_roster({})

        assert result is None


class TestCtxPlayerValue:
    """Coverage for the player-KTC-value context module."""

    async def test_chains_to_subject_player(self, rumors_cog, ktc_db):
        await _insert_ktc_row(ktc_db, 1, "p1", "Puka Nacua", "WR", 8500, "2026-07-22")
        await _insert_ktc_row(ktc_db, 2, "p2", "Some Other Guy", "RB", 1000, "2026-07-22")

        result = await rumors_cog._ctx_player_value({"player_name": "Puka Nacua"})

        assert "Puka Nacua" in result
        assert "8,500" in result
        assert "Some Other Guy" not in result

    async def test_falls_back_to_random_player_without_subject(self, rumors_cog, ktc_db):
        await _insert_ktc_row(ktc_db, 1, "p1", "Puka Nacua", "WR", 8500, "2026-07-22")

        result = await rumors_cog._ctx_player_value({})

        assert "Puka Nacua" in result

    async def test_returns_none_with_no_synced_data(self, rumors_cog, ktc_db):
        result = await rumors_cog._ctx_player_value({})

        assert result is None


class TestCtxTeamValue:
    """Coverage for the team-dynasty-value context module."""

    async def test_chains_to_subject_owner_and_ranks_it(self, rumors_cog, ktc_db):
        await _insert_ktc_row(ktc_db, 1, "p1", "Star Player", "WR", 9000, "2026-07-22")
        await _insert_ktc_row(ktc_db, 2, "p2", "Bench Guy", "RB", 1000, "2026-07-22")

        rumors_cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[
                {"roster_id": 1, "owner_id": "u1", "players": ["p1"]},
                {"roster_id": 2, "owner_id": "u2", "players": ["p2"]},
            ]
        )
        rumors_cog.bot.sleeper.get_users = AsyncMock(
            return_value=[
                {"user_id": "u1", "display_name": "Alice"},
                {"user_id": "u2", "display_name": "Bob"},
            ]
        )

        result = await rumors_cog._ctx_team_value({"owner": {"name": "Alice"}})

        assert "Alice" in result
        assert "9,000" in result
        assert "#1 of 2" in result

    async def test_returns_none_with_no_synced_data(self, rumors_cog, ktc_db):
        rumors_cog.bot.sleeper.get_rosters = AsyncMock(
            return_value=[{"roster_id": 1, "owner_id": "u1", "players": ["p1"]}]
        )
        rumors_cog.bot.sleeper.get_users = AsyncMock(return_value=[])

        result = await rumors_cog._ctx_team_value({})

        assert result is None


class TestCtxValueTrend:
    """Coverage for the 7-day KTC value swing context module."""

    async def test_reports_a_rise(self, rumors_cog, ktc_db):
        await _insert_ktc_row(ktc_db, 1, "p1", "Puka Nacua", "WR", 4000, "2026-07-15")
        await _insert_ktc_row(ktc_db, 1, "p1", "Puka Nacua", "WR", 5000, "2026-07-22")

        result = await rumors_cog._ctx_value_trend({"player_name": "Puka Nacua"})

        assert result is not None
        assert "risen" in result
        assert "1,000" in result

    async def test_flat_trend_returns_none(self, rumors_cog, ktc_db):
        await _insert_ktc_row(ktc_db, 1, "p1", "Puka Nacua", "WR", 5000, "2026-07-15")
        await _insert_ktc_row(ktc_db, 1, "p1", "Puka Nacua", "WR", 5000, "2026-07-22")

        result = await rumors_cog._ctx_value_trend({"player_name": "Puka Nacua"})

        assert result is None

    async def test_returns_none_with_no_synced_data(self, rumors_cog, ktc_db):
        result = await rumors_cog._ctx_value_trend({})

        assert result is None


class TestReporterWeighting:
    """Coverage for weighted random reporter selection."""

    async def test_weighted_reporter_favors_higher_weight(self, rumors_cog):
        rumors_cog.config = {
            "reporters": [
                {"name": "Common", "style": "s", "emoji": "x", "weight": 1},
                {"name": "Favored", "style": "s", "emoji": "x", "weight": 20},
            ]
        }
        counts = {"Common": 0, "Favored": 0}
        for _ in range(200):
            rumors_cog._recent_reporter_names.clear()  # isolate weighting from anti-repeat
            name, _, _ = rumors_cog._get_random_reporter()
            counts[name] += 1

        assert counts["Favored"] > counts["Common"]

    async def test_missing_weight_defaults_to_even_odds(self, rumors_cog):
        rumors_cog.config = {
            "reporters": [
                {"name": "NoWeightField", "style": "s", "emoji": "x"},
            ]
        }

        name, _, _ = rumors_cog._get_random_reporter()

        assert name == "NoWeightField"


class TestReporterSelectOptionCap:
    """Coverage for staying under Discord's 25-option select-menu limit."""

    async def test_caps_options_at_25_with_a_large_roster(self):
        from cogs.rumors import ReporterSelect

        reporters = [{"name": f"Reporter {i}", "style": "s", "emoji": "📰"} for i in range(40)]
        select = ReporterSelect(reporters, "some rumor", cog=MagicMock())

        assert len(select.options) <= 25

    async def test_includes_all_reporters_when_pool_is_small(self):
        from cogs.rumors import ReporterSelect

        reporters = [
            {"name": "A", "style": "s", "emoji": "📰"},
            {"name": "B", "style": "s", "emoji": "📰"},
        ]
        select = ReporterSelect(reporters, "some rumor", cog=MagicMock())

        assert len(select.options) == 3  # A, B, and the Random option


class TestReporterAutocomplete:
    """Coverage for /rumor's reporter autocomplete (replaces a hardcoded, stale choice list)."""

    async def test_includes_reporters_from_live_config(self, rumors_cog):
        choices = await rumors_cog.reporter_autocomplete(MagicMock(), "")

        values = {c.value for c in choices}
        assert "random" in values
        assert "custom" in values
        configured_names = {r["name"] for r in rumors_cog.config.get("reporters", [])}
        assert configured_names & values

    async def test_filters_by_current_input(self, rumors_cog):
        choices = await rumors_cog.reporter_autocomplete(MagicMock(), "judy")

        assert any("Judge Judy" in c.name for c in choices)
        assert all("judy" in c.name.lower() for c in choices)

    async def test_caps_results_at_25(self, rumors_cog):
        choices = await rumors_cog.reporter_autocomplete(MagicMock(), "")

        assert len(choices) <= 25

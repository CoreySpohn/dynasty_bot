"""Tests for the League Rumors cog."""

import random
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio

from cogs.rumors import STYLE_NOTES, LeagueRumors


@pytest_asyncio.fixture
async def rumors_cog(mock_bot):
    """Create a LeagueRumors cog, cancelling its background task on teardown."""
    cog = LeagueRumors(mock_bot)
    yield cog
    cog.random_rumor_task.cancel()


class TestRandomRumorTaskAutostarts:
    """Regression coverage for the auto-post loop being left disabled."""

    async def test_random_rumor_task_starts_on_init(self, rumors_cog):
        """The loop that posts unprompted rumors must actually be running.

        Previously `self.random_rumor_task.start()` was commented out in
        __init__ ("Disabled for now to avoid spam during testing"), so no
        rumor ever posted without a manual /postrumor or DM.
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
            "templates": [
                "{owner1} wants to trade {player1} to {owner2} for {player2}"
            ]
        }
        owner_data = [
            {"name": "Alice", "team_name": "Team A", "players": ["Player One"]},
            {"name": "Bob", "team_name": "Team B", "players": ["Player Two"]},
        ]

        result = await rumors_cog._generate_from_tables(owner_data)

        assert "Alice" in result or "Bob" in result
        assert "Player One" in result or "Player Two" in result
        assert "{" not in result

    async def test_falls_back_when_no_templates_configured(self, rumors_cog):
        rumors_cog.rumor_tables = {}

        result = await rumors_cog._generate_from_tables([])

        assert result == "a mysterious trade brewing in the league"

    async def test_handles_no_owner_data(self, rumors_cog):
        rumors_cog.rumor_tables = {"templates": ["{owner1} is up to something"]}

        result = await rumors_cog._generate_from_tables([])

        assert "An owner" in result


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
        rumors_cog.rumor_tables = {"templates": ["Template A", "Template B"]}

        first = await rumors_cog._generate_from_tables([])
        second = await rumors_cog._generate_from_tables([])

        assert first != second


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
    """Coverage for the combined seed builder used by both auto-post and /postrumor."""

    async def test_returns_non_empty_seed_even_when_sleeper_unavailable(self, rumors_cog):
        seed = await rumors_cog._build_rumor_seed()

        assert seed
        assert isinstance(seed, str)

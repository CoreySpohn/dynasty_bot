"""Tests for the League Rumors cog."""

from unittest.mock import MagicMock, patch

import pytest_asyncio

from cogs.rumors import LeagueRumors


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

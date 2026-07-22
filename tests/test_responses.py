"""Tests for the Random Auto-Responses cog's response-type dispatch."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cogs.responses import RandomResponses


def _make_message(author_id=1, content="hello"):
    message = MagicMock()
    message.author = MagicMock()
    message.author.id = author_id
    message.content = content
    message.reply = AsyncMock()
    message.channel = MagicMock()
    return message


def _make_past_message(author_id, content="something dumb"):
    past = MagicMock()
    past.author = MagicMock()
    past.author.id = author_id
    past.content = content
    past.reply = AsyncMock()
    return past


@pytest.fixture
def responses_cog(mock_bot, tmp_path, monkeypatch):
    monkeypatch.setattr("cogs.responses.RESPONSES_PATH", tmp_path / "responses.yaml")
    with patch.object(RandomResponses, "_load_responses", return_value=[]):
        cog = RandomResponses(mock_bot)
    return cog


class TestSendResponseAmbient:
    async def test_ambient_response_replies_to_triggering_message(self, responses_cog):
        message = _make_message()
        response = {"text": "cool story", "chance": 1000}

        await responses_cog._send_response(message, response)

        message.reply.assert_awaited_once_with("cool story", mention_author=False)


class TestStandingsResponse:
    async def test_fills_template_with_resolved_rank(self, responses_cog):
        message = _make_message()
        response = {
            "text": "big talk from a guy in {rank} place",
            "chance": 500,
            "type": "standings",
        }

        with patch(
            "lib.standings.get_rank_for_discord_id", new=AsyncMock(return_value=3)
        ):
            await responses_cog._send_response(message, response)

        message.reply.assert_awaited_once_with(
            "big talk from a guy in 3rd place", mention_author=False
        )

    async def test_skips_silently_when_author_isnt_a_known_member(self, responses_cog):
        message = _make_message()
        response = {
            "text": "big talk from a guy in {rank} place",
            "chance": 500,
            "type": "standings",
        }

        with patch(
            "lib.standings.get_rank_for_discord_id", new=AsyncMock(return_value=None)
        ):
            await responses_cog._send_response(message, response)

        message.reply.assert_not_awaited()

    async def test_skips_silently_on_lookup_error(self, responses_cog):
        message = _make_message()
        response = {
            "text": "big talk from a guy in {rank} place",
            "chance": 500,
            "type": "standings",
        }

        with patch(
            "lib.standings.get_rank_for_discord_id",
            new=AsyncMock(side_effect=RuntimeError("sleeper api down")),
        ):
            await responses_cog._send_response(message, response)

        message.reply.assert_not_awaited()


class TestCallbackResponse:
    async def test_replies_to_random_earlier_message_from_same_author(self, responses_cog):
        message = _make_message(author_id=42)
        past = _make_past_message(author_id=42)

        async def fake_history(limit, before):
            yield past

        message.channel.history = fake_history
        response = {"text": "this you?", "chance": 500, "type": "callback"}

        await responses_cog._send_response(message, response)

        past.reply.assert_awaited_once_with("this you?", mention_author=False)
        message.reply.assert_not_called()

    async def test_ignores_messages_from_other_authors(self, responses_cog):
        message = _make_message(author_id=42)
        other = _make_past_message(author_id=99)

        async def fake_history(limit, before):
            yield other

        message.channel.history = fake_history
        response = {"text": "this you?", "chance": 500, "type": "callback"}

        await responses_cog._send_response(message, response)

        other.reply.assert_not_called()
        message.reply.assert_not_called()

    async def test_ignores_empty_content_messages(self, responses_cog):
        message = _make_message(author_id=42)
        empty = _make_past_message(author_id=42, content="   ")

        async def fake_history(limit, before):
            yield empty

        message.channel.history = fake_history
        response = {"text": "this you?", "chance": 500, "type": "callback"}

        await responses_cog._send_response(message, response)

        empty.reply.assert_not_called()

    async def test_skips_when_no_history_found(self, responses_cog):
        message = _make_message(author_id=42)

        async def fake_history(limit, before):
            return
            yield  # pragma: no cover - makes this an async generator

        message.channel.history = fake_history
        response = {"text": "this you?", "chance": 500, "type": "callback"}

        await responses_cog._send_response(message, response)

        message.reply.assert_not_called()

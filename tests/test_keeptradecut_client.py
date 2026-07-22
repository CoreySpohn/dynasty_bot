"""Tests for the KeepTradeCut client."""

import json

import pytest
from aioresponses import aioresponses

from clients.keeptradecut import KeepTradeCutClient, RANKINGS_URL

# Trimmed but structurally faithful sample of KTC's embedded playersArray,
# based on real field names/shapes from the live dynasty-rankings page.
SAMPLE_PLAYERS = [
    {
        "playerName": "Ja'Marr Chase",
        "playerID": 1004,
        "position": "WR",
        "team": "CIN",
        "rookie": False,
        "age": 26.4,
        "oneQBValues": {"value": 9999, "rank": 1, "positionalRank": 1},
        "superflexValues": {"value": 9999, "rank": 1, "positionalRank": 1},
    },
    {
        "playerName": "Bijan Robinson",
        "playerID": 1414,
        "position": "RB",
        "team": "ATL",
        "rookie": False,
        "age": 23.5,
        "oneQBValues": {"value": 9995, "rank": 2, "positionalRank": 1},
        "superflexValues": {"value": 9998, "rank": 2, "positionalRank": 1},
    },
]


def _sample_html(players=SAMPLE_PLAYERS) -> str:
    return f"<html><body><script>var playersArray = {json.dumps(players)};</script></body></html>"


class TestKeepTradeCutClient:
    @pytest.fixture
    def client(self, aiohttp_session):
        return KeepTradeCutClient(aiohttp_session)

    async def test_get_player_values_parses_players_array(self, client, mock_aioresponses):
        mock_aioresponses.get(RANKINGS_URL, body=_sample_html())

        result = await client.get_player_values()

        assert len(result) == 2
        chase = result[0]
        assert chase["ktc_id"] == 1004
        assert chase["name"] == "Ja'Marr Chase"
        assert chase["position"] == "WR"
        assert chase["team"] == "CIN"
        assert chase["rookie"] is False
        assert chase["value_1qb"] == 9999
        assert chase["rank_1qb"] == 1
        assert chase["value_sf"] == 9999
        assert chase["positional_rank_sf"] == 1

    async def test_get_player_values_handles_non_200(self, client, mock_aioresponses):
        mock_aioresponses.get(RANKINGS_URL, status=503)

        result = await client.get_player_values()

        assert result == []

    async def test_get_player_values_handles_missing_players_array(
        self, client, mock_aioresponses
    ):
        mock_aioresponses.get(RANKINGS_URL, body="<html><body>no data here</body></html>")

        result = await client.get_player_values()

        assert result == []

    async def test_get_player_values_handles_malformed_json(self, client, mock_aioresponses):
        mock_aioresponses.get(
            RANKINGS_URL,
            body="<script>var playersArray = [{broken json}];</script>",
        )

        result = await client.get_player_values()

        assert result == []

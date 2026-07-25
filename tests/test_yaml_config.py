"""Tests for comment-preserving config round-trips.

The regression these guard against actually happened: saving `deadlines.yaml`
through `yaml.dump` stripped all 16 of its comment lines. `league_state.yaml`
was next, since the state advance saves automatically.
"""

import shutil
from pathlib import Path

import pytest

from lib.yaml_config import load_config, save_config

REAL_CONFIG = Path(__file__).parent.parent / "config"

SAMPLE = """\
# Leading comment
current_state: off_season  # inline: off_season | pre_season | in_season

# Section heading
season:
  year: 2026   # the year

deadlines:
- id: first
  # a comment inside a list item
  description: "🗳️ Emoji and quotes survive"
"""


class TestRoundTrip:
    def test_preserves_comments_through_a_save(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        path.write_text(SAMPLE)

        data = load_config(path)
        save_config(data, path)

        after = path.read_text()
        assert "# Leading comment" in after
        assert "# inline: off_season" in after
        assert "# Section heading" in after
        assert "# a comment inside a list item" in after

    def test_preserves_emoji_and_quotes(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        path.write_text(SAMPLE)

        save_config(load_config(path), path)

        after = path.read_text()
        assert "🗳️" in after
        assert "\\U0001F5F3" not in after

    def test_a_mutation_lands_and_comments_stay(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        path.write_text(SAMPLE)

        data = load_config(path)
        data["current_state"] = "pre_season"
        save_config(data, path)

        after = path.read_text()
        assert "current_state: pre_season" in after
        assert "# inline: off_season" in after
        assert load_config(path)["current_state"] == "pre_season"

    def test_behaves_like_a_dict(self):
        data = load_config(REAL_CONFIG / "league_state.yaml")

        assert data["current_state"]
        assert data.get("nope", "fallback") == "fallback"

    @pytest.mark.parametrize("name", ["league_state.yaml", "deadlines.yaml"])
    def test_real_configs_survive_a_round_trip_byte_for_byte(self, name, tmp_path):
        """The strongest form: saving without changing anything should leave the
        file identical, so an automatic save is a genuine no-op."""
        path = tmp_path / name
        shutil.copy(REAL_CONFIG / name, path)
        before = path.read_text()

        save_config(load_config(path), path)

        assert path.read_text() == before

    def test_missing_file_returns_the_default(self, tmp_path):
        default = {"current_state": "off_season"}

        assert load_config(tmp_path / "nope.yaml", default) == default

    def test_malformed_file_returns_the_default(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        path.write_text("{[not: valid: yaml")

        assert load_config(path, {"a": 1}) == {"a": 1}

    def test_empty_file_is_an_empty_mapping(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        path.write_text("")

        assert load_config(path) == {}

    def test_save_leaves_no_temp_file(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        path.write_text(SAMPLE)

        save_config(load_config(path), path)

        assert list(tmp_path.iterdir()) == [path]

    def test_a_plain_dict_still_saves(self, tmp_path):
        """The missing-file default path has no comments to preserve."""
        path = tmp_path / "cfg.yaml"

        save_config({"current_state": "off_season"}, path)

        assert load_config(path)["current_state"] == "off_season"

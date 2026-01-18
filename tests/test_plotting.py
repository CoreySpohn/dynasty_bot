"""Tests for the plotting utilities."""

import io

import pandas as pd
import pytest

from lib.plotting import normalize_dataframe, render_mpl_table, render_power_rankings


class TestNormalizeDataframe:
    """Test suite for DataFrame normalization."""

    def test_normalize_numeric_columns(self):
        """Test normalizing numeric columns to 0-1 range."""
        df = pd.DataFrame({
            "A": [10, 20, 30, 40, 50],
            "B": [100, 200, 300, 400, 500],
        })
        
        result = normalize_dataframe(df)
        
        assert result["A"].min() == 0.0
        assert result["A"].max() == 1.0
        assert result["B"].min() == 0.0
        assert result["B"].max() == 1.0

    def test_normalize_string_columns_with_records(self):
        """Test normalizing string columns like win-loss records."""
        df = pd.DataFrame({
            "Record": ["5-0", "4-1", "3-2", "2-3", "1-4"],
        })
        
        result = normalize_dataframe(df)
        
        # Should extract first number (wins) and normalize
        assert result["Record"].iloc[0] == 1.0  # 5 wins (max)
        assert result["Record"].iloc[4] == 0.0  # 1 win (min)

    def test_normalize_handles_identical_values(self):
        """Test normalizing when all values are the same."""
        df = pd.DataFrame({
            "A": [50, 50, 50],
        })
        
        result = normalize_dataframe(df)
        
        # All values should be 0.5 when identical
        assert all(result["A"] == 0.5)

    def test_normalize_handles_percentage_strings(self):
        """Test normalizing percentage strings."""
        df = pd.DataFrame({
            "WinPct": ["100%", "75%", "50%", "25%", "0%"],
        })
        
        # This should handle the % gracefully or return 0
        result = normalize_dataframe(df)
        
        # Should return numeric values without error
        assert len(result) == 5


class TestRenderMplTable:
    """Test suite for matplotlib table rendering."""

    def test_render_returns_bytesio(self):
        """Test that render_mpl_table returns a BytesIO buffer."""
        df = pd.DataFrame({
            "Col1": [1, 2, 3],
            "Col2": [4, 5, 6],
        })
        
        result = render_mpl_table(df)
        
        assert isinstance(result, io.BytesIO)
        assert result.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes

    def test_render_with_row_labels(self):
        """Test rendering with custom row labels."""
        df = pd.DataFrame({
            "Score": [100, 90, 80],
        })
        row_labels = ["Team A", "Team B", "Team C"]
        
        result = render_mpl_table(df, row_labels=row_labels)
        
        assert isinstance(result, io.BytesIO)
        assert len(result.getvalue()) > 0

    def test_render_with_title(self):
        """Test rendering with a title."""
        df = pd.DataFrame({
            "Value": [1, 2, 3],
        })
        
        result = render_mpl_table(df, title="Test Table")
        
        assert isinstance(result, io.BytesIO)
        assert len(result.getvalue()) > 0

    def test_render_without_colorize(self):
        """Test rendering without cell colorization."""
        df = pd.DataFrame({
            "A": [1, 2, 3],
            "B": [4, 5, 6],
        })
        
        result = render_mpl_table(df, colorize=False)
        
        assert isinstance(result, io.BytesIO)


class TestRenderPowerRankings:
    """Test suite for power rankings rendering."""

    def test_render_power_rankings_returns_bytesio(self):
        """Test that render_power_rankings returns a BytesIO buffer."""
        df = pd.DataFrame({
            "Owner": ["Team A", "Team B", "Team C"],
            "Power Level": [185.5, 172.3, 160.1],
            "Record": ["4-1", "3-2", "2-3"],
            "Average Points": [120.5, 115.2, 108.7],
        })
        
        result = render_power_rankings(df, week=5, season=2025)
        
        assert isinstance(result, io.BytesIO)
        assert result.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_power_rankings_renames_columns(self):
        """Test that long column names are shortened."""
        df = pd.DataFrame({
            "Owner": ["Team A"],
            "Power Level": [100.0],
            "Potential Points": [500.0],
            "Points For": [400.0],
        })
        
        # Should not raise even with columns to rename
        result = render_power_rankings(df, week=1, season=2025)
        
        assert isinstance(result, io.BytesIO)

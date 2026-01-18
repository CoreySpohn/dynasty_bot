"""Plotting utilities for Dynasty Bot.

Provides matplotlib-based visualization functions for generating
Power Rankings tables and other analytics charts.
"""

import io
from typing import Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize numeric columns in a DataFrame to 0-1 range.
    
    Handles string columns that contain numeric-like data
    (e.g., "5-3" records) by extracting the first number.
    
    Args:
        df: DataFrame to normalize.
        
    Returns:
        Normalized DataFrame with all values in [0, 1] range.
    """
    result = df.copy()
    
    for col in df.columns:
        if df[col].dtype == object:
            # Convert string columns to numeric
            def convert_to_numeric(x):
                try:
                    return float(x)
                except (ValueError, TypeError):
                    if isinstance(x, str) and "-" in x:
                        try:
                            return float(x.split("-")[0])
                        except ValueError:
                            return 0.0
                    return 0.0
            
            result[col] = df[col].apply(convert_to_numeric)
        else:
            result[col] = df[col]
        
        # Normalize to 0-1 range
        max_val = result[col].max()
        min_val = result[col].min()
        if max_val != min_val:
            result[col] = (result[col] - min_val) / (max_val - min_val)
        else:
            result[col] = 0.5  # All same value
    
    return result


def render_mpl_table(
    data: pd.DataFrame,
    row_labels: Optional[list[str]] = None,
    col_width: float = 2.5,
    row_height: float = 0.75,
    font_size: int = 24,
    header_color: str = "#40466e",
    edge_color: str = "white",
    title: Optional[str] = None,
    colorize: bool = True,
) -> io.BytesIO:
    """Render a pandas DataFrame as a styled matplotlib table.
    
    Creates a visually appealing table with:
    - Colored headers
    - Row-wise color gradients based on normalized values
    - Automatic column width adjustment
    
    Args:
        data: DataFrame to render as a table.
        row_labels: Optional labels for each row (e.g., team names).
        col_width: Width of each column in inches.
        row_height: Height of each row in inches.
        font_size: Font size for table text.
        header_color: Background color for header row.
        edge_color: Color for cell borders.
        title: Optional title for the table.
        colorize: If True, colorize cells based on normalized values.
        
    Returns:
        BytesIO buffer containing the PNG image.
    """
    # Calculate figure size
    n_cols = len(data.columns)
    n_rows = len(data)
    
    # Add extra width for row labels if provided
    label_width = 2.0 if row_labels else 0
    fig_width = n_cols * col_width + label_width + 0.5
    fig_height = (n_rows + 1) * row_height + (0.75 if title else 0)
    
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    ax.set_axis_off()
    
    # Add title if provided
    if title:
        ax.set_title(title, fontsize=font_size + 4, fontweight="bold", pad=20)
    
    # Create the table
    table = ax.table(
        cellText=data.values,
        cellLoc="center",
        colLabels=data.columns,
        rowLabels=row_labels,
        loc="center",
        bbox=[0, 0, 1, 1],
    )
    
    table.auto_set_font_size(False)
    table.set_fontsize(font_size)
    
    # Get colormap for cell coloring
    cmap = plt.cm.RdYlGn
    
    # Normalize data for coloring
    if colorize:
        df_normalized = normalize_dataframe(data)
    
    # Style each cell
    for (row, col), cell in table._cells.items():
        cell.set_edgecolor(edge_color)
        
        # Header row or row labels
        if row == 0 or col < 0:
            cell.set_text_props(weight="bold", color="white")
            cell.set_facecolor(header_color)
        elif colorize:
            # Color based on normalized value
            try:
                norm_value = df_normalized.iloc[row - 1, col]
                color = cmap(norm_value)
                cell.set_facecolor(color)
                
                # Set text color based on background brightness
                brightness = color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114
                text_color = "white" if brightness < 0.5 else "black"
                cell.set_text_props(weight="bold", color=text_color)
            except (IndexError, KeyError):
                cell.set_facecolor("#f1f1f2")
                cell.set_text_props(weight="bold", color="black")
        else:
            # Alternating row colors
            row_color = "#f1f1f2" if row % 2 == 0 else "white"
            cell.set_facecolor(row_color)
            cell.set_text_props(weight="bold", color="black")
    
    # Auto-adjust column widths
    table.auto_set_column_width(col=list(range(len(data.columns))))
    
    fig.tight_layout()
    
    # Save to BytesIO buffer
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=200, bbox_inches="tight", facecolor="white")
    buffer.seek(0)
    
    plt.close(fig)
    
    return buffer


def render_power_rankings(
    rankings_df: pd.DataFrame,
    week: int,
    season: int,
) -> io.BytesIO:
    """Render a Power Rankings table.
    
    Creates a specialized table for fantasy football power rankings
    with team names as row labels and various stats as columns.
    
    Args:
        rankings_df: DataFrame with power rankings data.
            Expected columns: Owner, Power Level, Record, Avg Points, etc.
        week: Current NFL week.
        season: Current season year.
        
    Returns:
        BytesIO buffer containing the PNG image.
    """
    # Extract owner names for row labels
    if "Owner" in rankings_df.columns:
        row_labels = rankings_df["Owner"].tolist()
        display_df = rankings_df.drop(columns=["Owner"])
    else:
        row_labels = None
        display_df = rankings_df
    
    # Shorten column names for display
    column_renames = {
        "Power Level": "Pwr Lvl",
        "Potential Points": "Max PF",
        "Points For": "PF",
        "Points Against": "PA",
        "Average Points": "Avg Pts",
        "Record": "Rec",
        "Win %": "Win%",
    }
    display_df = display_df.rename(columns=column_renames)
    
    title = f"⚡ Power Rankings - Week {week}, {season}"
    
    return render_mpl_table(
        display_df,
        row_labels=row_labels,
        title=title,
        font_size=20,
        colorize=True,
    )

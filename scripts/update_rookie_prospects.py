#!/usr/bin/env python3
"""Refresh the rookie_prospect table in config/rumor_tables.yaml.

Pulls the current top rookies (by Sleeper's search_rank, a rough proxy
for ADP/relevance) from the cached Sleeper player data and rewrites
just the `rookie_prospect:` block, leaving the rest of the file
(comments, other tables) untouched.

Usage:
    uv run python scripts/update_rookie_prospects.py [--count 12]
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.sleeper import SleeperClient

RUMOR_TABLES_PATH = Path(__file__).parent.parent / "config" / "rumor_tables.yaml"
FANTASY_POSITIONS = ("QB", "RB", "WR", "TE")


async def fetch_top_rookies(count: int) -> list[dict]:
    """Fetch the top `count` rookies from Sleeper's player data, sorted by search_rank."""
    async with aiohttp.ClientSession() as session:
        client = SleeperClient(session)
        players = await client.get_all_players()

    rookies = [
        p
        for p in players.values()
        if p.get("years_exp") == 0
        and p.get("position") in FANTASY_POSITIONS
        and p.get("active")
        and p.get("search_rank") is not None
    ]
    rookies.sort(key=lambda p: p["search_rank"])
    return rookies[:count]


def render_block(rookies: list[dict]) -> str:
    """Render the rookie_prospect YAML block (including its comment header)."""
    lines = [
        "# Top rookie prospects for this year's rookie draft (name + position).",
        "# Regenerate with: uv run python scripts/update_rookie_prospects.py",
        "rookie_prospect:",
    ]
    for p in rookies:
        lines.append(f'  - "{p["full_name"]} ({p["position"]})"')
    return "\n".join(lines) + "\n"


def splice_into_file(new_block: str) -> None:
    """Replace the existing rookie_prospect block, or insert one if absent."""
    content = RUMOR_TABLES_PATH.read_text()

    # Matches from an optional preceding comment block through the
    # rookie_prospect list, up to (but not including) the next top-level key.
    pattern = re.compile(
        r"(?:^#.*\n)*^rookie_prospect:\n(?:^  - .*\n)*",
        re.MULTILINE,
    )

    if pattern.search(content):
        content = pattern.sub(new_block, content, count=1)
    else:
        # No existing block - insert before the first other top-level table.
        insertion_point = content.index("\ndraft_target_type:")
        content = content[:insertion_point] + "\n" + new_block + content[insertion_point:]

    RUMOR_TABLES_PATH.write_text(content)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=12, help="Number of rookies to include")
    args = parser.parse_args()

    print(f"Fetching top {args.count} rookies from Sleeper...")
    rookies = await fetch_top_rookies(args.count)

    for p in rookies:
        print(f"  {p['search_rank']:>5}  {p['full_name']:<25} {p['position']}")

    block = render_block(rookies)
    splice_into_file(block)
    print(f"\nUpdated {RUMOR_TABLES_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
